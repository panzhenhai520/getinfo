import copy
import json
import tempfile
import unittest
from pathlib import Path

from industry_pack_migration import (
    FamilyOfficePackExporter,
    replay_export,
    write_family_office_seed,
)
from industry_packs import IndustryPackLoader
from intel_sources import canonicalize_source_url
from sqlite_database import SQLiteDatabase


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class IndustryPackMigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(
            str(Path(self.temp_dir.name) / "migration-source.sqlite3")
        )
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.loader = IndustryPackLoader(
            str(PROJECT_ROOT / "config" / "industry_packs"),
            use_published_store=False,
        )

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _source(self, url, name, pack_id, ownership):
        source_id = int(
            self.database.connection.execute(
                """
                INSERT INTO intel_sources(
                    canonical_source_url, source_url, source_name,
                    source_type, content_type, authority_level,
                    polling_interval_minutes, is_enabled, metadata_json
                ) VALUES(?, ?, ?, 'website', 'media', 3, 60, 1, '{}')
                """,
                (canonicalize_source_url(url), url, name),
            ).lastrowid
        )
        self.database.connection.execute(
            """
            INSERT INTO intel_source_industries(
                source_id, industry_pack_id, ownership_type, is_active
            ) VALUES(?, ?, ?, 1)
            """,
            (source_id, pack_id, ownership),
        )
        return source_id

    def test_export_deduplicates_assigns_ownership_and_performs_no_source_writes(self):
        family_url = "https://family.example.test/news?utm_source=fixture"
        finance_url = "https://finance.example.test/market"
        protected_url = "https://manual.example.test/private"
        self._source(family_url, "Family source", "family_office", "pack_owned")
        self._source(
            finance_url, "Finance source", "financial_markets", "shared_financial"
        )
        self._source(
            protected_url, "Manual source", "family_office", "protected_manual"
        )
        managed_id = int(
            self.database.connection.execute(
                """
                INSERT INTO managed_urls(url, name, is_active, crawl_frequency)
                VALUES(?, 'duplicate managed', 1, 'daily')
                """,
                ("https://family.example.test/news?utm_medium=test",),
            ).lastrowid
        )
        self.database.connection.execute(
            """
            INSERT INTO scheduled_tasks(
                task_name, task_type, url_id, schedule_type, schedule_time,
                schedule_weekdays, keywords, is_active
            ) VALUES('duplicate schedule','crawl',?,'daily','09:15','',
                     '不应合并到行业关键词',1)
            """,
            (managed_id,),
        )
        self.database.connection.commit()
        before = {
            table: int(
                self.database.connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            )
            for table in (
                "managed_urls",
                "scheduled_tasks",
                "intel_sources",
                "intel_source_industries",
            )
        }

        exported = FamilyOfficePackExporter(
            self.database.connection, pack_loader=self.loader
        ).export()
        after = {
            table: int(
                self.database.connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            )
            for table in before
        }
        self.assertEqual(before, after)
        self.assertTrue(exported["read_only"])
        self.assertEqual(exported["database_writes"], 0)
        family_sources = exported["proposed_manifests"]["family_office"][
            "default_sources"
        ]
        finance_sources = exported["proposed_manifests"]["financial_markets"][
            "default_sources"
        ]
        self.assertEqual(
            sum(
                canonicalize_source_url(item["url"])
                == canonicalize_source_url(family_url)
                for item in family_sources
            ),
            1,
        )
        self.assertIn(
            canonicalize_source_url(finance_url),
            {canonicalize_source_url(item["url"]) for item in finance_sources},
        )
        self.assertIn(
            canonicalize_source_url(protected_url),
            {
                canonicalize_source_url(item["url"])
                for item in exported["protected_manual_sources"]
            },
        )
        self.assertNotIn(
            canonicalize_source_url(protected_url),
            {canonicalize_source_url(item["url"]) for item in family_sources},
        )
        self.assertEqual(
            exported["proposed_manifests"]["family_office"]["core_keywords"],
            self.loader.load("family_office")["core_keywords"],
        )

    def test_fresh_database_replay_is_exact_and_idempotent(self):
        self._source(
            "https://family-replay.example.test/news",
            "Replay family",
            "family_office",
            "pack_owned",
        )
        self._source(
            "https://finance-replay.example.test/news",
            "Replay finance",
            "financial_markets",
            "shared_financial",
        )
        self.database.connection.commit()
        exported = FamilyOfficePackExporter(
            self.database.connection, pack_loader=self.loader
        ).export()
        replay = replay_export(
            exported,
            config_dir=str(PROJECT_ROOT / "config" / "industry_packs"),
        )
        self.assertTrue(replay["passed"], json.dumps(replay, ensure_ascii=False))
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["missing_sources"], [])
        self.assertEqual(replay["unexpected_sources"], [])
        self.assertEqual(
            replay["expected_source_count"], replay["actual_source_count"]
        )
        self.assertTrue(
            all(value == 0 for value in replay["second_plan_counts"].values())
        )

    def test_seed_install_requires_all_migration_gates(self):
        exported = FamilyOfficePackExporter(
            self.database.connection,
            pack_loader=self.loader,
            target_pack_version="2.1.0",
        ).export()
        replay = replay_export(
            exported,
            config_dir=str(PROJECT_ROOT / "config" / "industry_packs"),
        )
        result = {**exported, "replay": replay, "passed": replay["passed"]}
        destination = Path(self.temp_dir.name) / "installed-family-office.json"
        installed = write_family_office_seed(result, destination=str(destination))
        manifest = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(installed["source_count"], len(manifest["default_sources"]))
        self.assertEqual(installed["pack_version"], "2.1.0")
        self.assertEqual(manifest["pack_version"], "2.1.0")

        blocked = copy.deepcopy(result)
        blocked["counts"]["protected_manual_sources"] = 1
        with self.assertRaisesRegex(ValueError, "受保护人工来源"):
            write_family_office_seed(blocked, destination=str(destination))


if __name__ == "__main__":
    unittest.main()

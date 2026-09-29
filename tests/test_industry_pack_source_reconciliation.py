import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from industry_packs import IndustryPackLoader
from intel_sources import IntelSourceRegistry, canonicalize_source_url
from sqlite_database import SQLiteDatabase


def _pack(pack_id, *, kind="primary", includes=None, sources=None):
    return {
        "id": pack_id,
        "name": pack_id,
        "schema_version": 2,
        "pack_version": "1.0.0",
        "enabled": True,
        "default_market": "GLOBAL",
        "timezone": "Asia/Hong_Kong",
        "pack_kind": kind,
        "includes": includes or [],
        "capabilities": [],
        "dashboard_categories": [],
        "core_keywords": [pack_id],
        "expanded_keywords": [],
        "trend_keywords": ["trend"],
        "event_keywords": ["event"],
        "negative_keywords": [],
        "classification": {
            "core_weight": 3,
            "expanded_weight": 1,
            "trend_weight": 2,
            "event_weight": 2,
            "negative_weight": -3,
            "minimum_relevance_score": 2,
            "llm_confidence_threshold": 0.65,
            "tie_break_order": ["trend", "event", "other"],
        },
        "serpapi_queries": [],
        "default_sources": sources or [],
        "fixed_topics": [],
    }


class IndustryPackSourceReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        config_dir = root / "packs"
        config_dir.mkdir()
        primary_source = {
            "name": "Primary source",
            "url": "https://example.test/news",
            "source_type": "list_page",
            "authority_level": 2,
            "polling_interval_minutes": 60,
        }
        finance_source = {
            "name": "Finance feed",
            "url": "https://finance.example.test/feed.xml",
            "source_type": "rss",
            "authority_level": 4,
            "polling_interval_minutes": 1440,
        }
        for pack in (
            _pack(
                "root",
                includes=[{"pack_id": "financial_markets", "required": True}],
                sources=[primary_source],
            ),
            _pack(
                "financial_markets",
                kind="hybrid",
                sources=[finance_source],
            ),
        ):
            (config_dir / f"{pack['id']}.json").write_text(
                json.dumps(pack), encoding="utf-8"
            )
        self.loader = IndustryPackLoader(str(config_dir))
        self.database = SQLiteDatabase(str(root / "reconcile.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.registry = IntelSourceRegistry(self.database, pack_loader=self.loader)

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _source(self, url, name, *, authority=3, manual_authority=False):
        cursor = self.database.connection.execute(
            """
            INSERT INTO intel_sources(
                canonical_source_url, source_url, source_name, source_type,
                authority_level, authority_is_manual, is_enabled, metadata_json
            ) VALUES(?, ?, ?, 'website', ?, ?, 1, '{}')
            """,
            (
                canonicalize_source_url(url),
                url,
                name,
                authority,
                int(manual_authority),
            ),
        )
        return int(cursor.lastrowid)

    def _associate(self, source_id, ownership_type):
        self.database.connection.execute(
            """
            INSERT INTO intel_source_industries(
                source_id, industry_pack_id, ownership_type, is_active
            ) VALUES(?, 'root', ?, 1)
            """,
            (source_id, ownership_type),
        )

    def test_authoritative_plan_apply_is_idempotent_and_preserves_protected_state(self):
        primary_id = self._source(
            "https://example.test/news", "old name", authority=5, manual_authority=True
        )
        stale_id = self._source("https://stale.example.test/news", "stale")
        protected_id = self._source("https://manual.example.test/news", "manual")
        self._associate(primary_id, "pack_owned")
        self._associate(stale_id, "pack_owned")
        self._associate(protected_id, "protected_manual")
        self.database.connection.commit()

        plan = self.registry.plan_source_reconciliation(
            "root", declared_version_id=7
        )
        self.assertEqual(plan["counts"]["add_sources"], 1)
        self.assertEqual(plan["counts"]["deactivate_associations"], 1)
        self.assertEqual(plan["counts"]["upsert_associations"], 2)
        result = self.registry.apply_source_reconciliation(
            "root",
            expected_plan_sha256=plan["plan_sha256"],
            declared_version_id=7,
        )
        self.assertTrue(result["applied"])

        primary = self.database.connection.execute(
            "SELECT source_name, authority_level FROM intel_sources WHERE id=?",
            (primary_id,),
        ).fetchone()
        self.assertEqual(primary["source_name"], "Primary source")
        self.assertEqual(primary["authority_level"], 5)
        stale = self.database.connection.execute(
            "SELECT is_active FROM intel_source_industries WHERE source_id=?",
            (stale_id,),
        ).fetchone()
        protected = self.database.connection.execute(
            "SELECT is_active, ownership_type FROM intel_source_industries WHERE source_id=?",
            (protected_id,),
        ).fetchone()
        self.assertEqual(stale["is_active"], 0)
        self.assertEqual(tuple(protected), (1, "protected_manual"))
        finance = self.database.connection.execute(
            """
            SELECT si.ownership_type, si.is_active, si.declared_version_id
            FROM intel_source_industries si
            JOIN intel_sources s ON s.id=si.source_id
            WHERE s.canonical_source_url=? AND si.industry_pack_id='financial_markets'
            """,
            (canonicalize_source_url("https://finance.example.test/feed.xml"),),
        ).fetchone()
        self.assertEqual(tuple(finance), ("shared_financial", 1, 7))

        repeated = self.registry.plan_source_reconciliation(
            "root", declared_version_id=7
        )
        self.assertEqual(
            repeated["counts"],
            {
                "add_sources": 0,
                "update_sources": 0,
                "upsert_associations": 0,
                "deactivate_associations": 0,
            },
        )
        self.registry.apply_source_reconciliation(
            "root",
            expected_plan_sha256=repeated["plan_sha256"],
            declared_version_id=7,
        )
        visible, _total = self.registry.list_sources(
            industry_pack_id="root", page=1, per_page=100
        )
        visible_ids = {item["id"] for item in visible}
        self.assertNotIn(stale_id, visible_ids)
        self.assertIn(protected_id, visible_ids)

    def test_changed_or_forged_plan_hash_is_rejected_without_writes(self):
        with self.assertRaisesRegex(ValueError, "差异已经变化"):
            self.registry.apply_source_reconciliation(
                "root",
                expected_plan_sha256="0" * 64,
                declared_version_id=1,
            )
        self.assertEqual(
            self.database.connection.execute("SELECT COUNT(*) FROM intel_sources").fetchone()[0],
            0,
        )

    def test_apply_failure_rolls_back_new_physical_source_and_association(self):
        plan = self.registry.plan_source_reconciliation(
            "root", declared_version_id=1
        )
        self.database.connection.execute(
            """
            CREATE TRIGGER reject_reconciled_association
            BEFORE INSERT ON intel_source_industries
            BEGIN SELECT RAISE(ABORT, 'fixture association failure'); END
            """
        )
        with self.assertRaisesRegex(Exception, "fixture association failure"):
            self.registry.apply_source_reconciliation(
                "root",
                expected_plan_sha256=plan["plan_sha256"],
                declared_version_id=1,
            )
        self.assertEqual(
            self.database.connection.execute("SELECT COUNT(*) FROM intel_sources").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM intel_source_industries"
            ).fetchone()[0],
            0,
        )

    def test_deactivated_association_is_not_due(self):
        source_id = self._source("https://stale.example.test/news", "stale")
        self._associate(source_id, "pack_owned")
        self.database.connection.execute(
            "UPDATE intel_source_industries SET is_active=0 WHERE source_id=?",
            (source_id,),
        )
        self.database.connection.commit()
        due = self.registry.due_source_ids(
            "root", datetime(2026, 8, 6, 23, 0, tzinfo=timezone.utc)
        )
        self.assertNotIn(source_id, due)

    def test_full_switch_source_set_excludes_unscoped_legacy_associations(self):
        owned_id = self._source("https://owned.example.test/news", "owned")
        shared_id = self._source("https://shared.example.test/feed", "shared")
        on_demand_id = self._source("https://ondemand.example.test/news", "on-demand")
        legacy_id = self._source("https://legacy.example.test/news", "legacy")
        self._associate(owned_id, "pack_owned")
        self._associate(shared_id, "shared_financial")
        self._associate(on_demand_id, "shared_financial")
        self._associate(legacy_id, "legacy")
        self.database.connection.execute(
            "UPDATE intel_sources SET metadata_json=? WHERE id=?",
            ('{"on_demand_only":true}', on_demand_id),
        )
        self.database.connection.commit()

        source_ids = self.registry.effective_enabled_source_ids("root")

        self.assertIn(owned_id, source_ids)
        self.assertIn(shared_id, source_ids)
        self.assertNotIn(on_demand_id, source_ids)
        self.assertNotIn(legacy_id, source_ids)


if __name__ == "__main__":
    unittest.main()

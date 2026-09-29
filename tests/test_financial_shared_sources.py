import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from candidate_dispatcher import IntelCandidateDispatcher
from industry_packs import IndustryPackLoader
from intel_candidates import IntelCandidateRepository
from intel_light_scanner import IntelLightScanner, source_scan_window_key
from intel_sources import IntelSourceRegistry, canonicalize_source_url
from sqlite_database import SQLiteDatabase


class _CountingRSSScanner:
    def __init__(self):
        self.calls = 0
        self.lock = threading.Lock()

    def scan(self, _source, *, limit):
        with self.lock:
            self.calls += 1
        return [
            {
                "url": "https://example.test/market/one",
                "title": "SFC announces capital market trading rules",
                "summary": "Securities market regulatory framework and market trading update",
                "published_at": "2026-07-31T09:00:00+08:00",
            }
        ][:limit]


class _NoopScanner:
    def scan(self, _source, *, limit):
        return []


class _OneArticleAdapter:
    def __init__(self, database):
        self.database = database
        self.calls = 0

    def dispatch(self, candidate):
        self.calls += 1
        cursor = self.database.connection.execute(
            "INSERT INTO articles(url, canonical_url, title, content, status) VALUES(?, ?, ?, ?, 'active')",
            (
                candidate["original_url"],
                candidate["canonical_url"],
                candidate["title"],
                "one canonical extracted body",
            ),
        )
        return {
            "success": True,
            "article_id": int(cursor.lastrowid),
            "crawler_task_id": f"shared-source-{candidate['id']}",
            "outcome": "crawled",
        }


class FinancialSharedSourceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(
            str(Path(self.temp_dir.name) / "financial-shared-source.sqlite3")
        )
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.registry = IntelSourceRegistry(self.database)

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _insert_financial_source(self):
        metadata = json.dumps(
            {
                "origin_pack_id": "financial_markets",
                "declared_by_pack_ids": ["financial_markets"],
            }
        )
        cursor = self.database.connection.execute(
            """
            INSERT INTO intel_sources(
                canonical_source_url, source_url, source_name, source_type,
                content_type, market, authority_level, polling_interval_minutes,
                is_enabled, metadata_json
            ) VALUES(?, ?, 'Shared finance RSS', 'rss', 'official', 'HK', 5, 1440, 1, ?)
            """,
            ("https://example.test/feed.xml", "https://example.test/feed.xml", metadata),
        )
        source_id = int(cursor.lastrowid)
        self.database.connection.execute(
            "INSERT INTO intel_source_industries(source_id, industry_pack_id) VALUES(?, 'financial_markets')",
            (source_id,),
        )
        self.database.connection.commit()
        return source_id

    def test_origin_migration_keeps_old_association_as_audit_only(self):
        url = "https://www.news.gov.hk/en/categories/finance/html/articlelist.rss.xml"
        cursor = self.database.connection.execute(
            """
            INSERT INTO intel_sources(
                canonical_source_url, source_url, source_name, source_type,
                is_enabled, metadata_json
            ) VALUES(?, ?, 'legacy family finance source', 'rss', 1, ?)
            """,
            (
                canonicalize_source_url(url),
                url,
                json.dumps({"origin_pack_id": "family_office"}),
            ),
        )
        source_id = int(cursor.lastrowid)
        self.database.connection.execute(
            "INSERT INTO intel_source_industries(source_id, industry_pack_id) VALUES(?, 'family_office')",
            (source_id,),
        )
        self.database.connection.commit()

        expected_family_total = len(
            {
                canonicalize_source_url(item["url"])
                for item in self.registry.pack_loader.compose("family_office")[
                    "default_sources"
                ]
            }
        )
        result = self.registry.ensure_pack_default_sources("family_office")

        # The legacy row is one of the composed defaults, so every other
        # physical family/shared-financial source is inserted exactly once.
        self.assertEqual(result["sources_added"], expected_family_total - 1)
        self.assertEqual(
            self.database.connection.execute("SELECT COUNT(*) FROM intel_sources").fetchone()[0],
            expected_family_total,
        )
        migrated = self.registry.get_source(source_id)
        self.assertEqual(migrated["metadata"]["origin_pack_id"], "financial_markets")
        self.assertEqual(migrated["metadata"]["declared_by_pack_ids"], ["financial_markets"])
        self.assertEqual(
            set(migrated["industry_pack_ids"]), {"family_office", "financial_markets"}
        )
        family_office = next(
            item
            for page in range(1, 3)
            for item in self.registry.list_sources(page=page, per_page=100)[0]
            if item["source_name"] == "FamilyOfficeHK"
        )
        self.assertEqual(family_office["industry_pack_ids"], ["family_office"])
        family_effective_sources, family_effective_total = self.registry.list_sources(
            industry_pack_id="family_office", page=1, per_page=100
        )
        financial_sources, financial_total = self.registry.list_sources(
            industry_pack_id="financial_markets", page=1, per_page=100
        )
        self.assertEqual(family_effective_total, expected_family_total)
        self.assertEqual(financial_total, 7)
        self.assertEqual(len(family_effective_sources), min(100, expected_family_total))
        self.assertEqual(len(financial_sources), 7)
        dependency = self.database.connection.execute(
            "SELECT parent_pack_id, dependency_pack_id FROM industry_pack_dependencies"
        ).fetchone()
        self.assertEqual(tuple(dependency), ("family_office", "financial_markets"))

    def test_two_packs_and_duplicate_workers_share_one_scan_and_one_body(self):
        source_id = self._insert_financial_source()
        candidates = IntelCandidateRepository(self.database)
        feed = _CountingRSSScanner()
        scanner = IntelLightScanner(
            candidate_repository=candidates,
            source_registry=self.registry,
            rss_scanner=feed,
            list_scanner=_NoopScanner(),
            url_validator=lambda value: value,
        )

        def scan(pack_id):
            return scanner.scan(
                industry_pack_id=pack_id,
                source_ids=[source_id],
                include_serpapi=False,
                max_sources=1,
                max_items_per_source=10,
                manual=True,
            )

        with patch("intel_light_scanner.time.time", return_value=1785456000.0):
            with ThreadPoolExecutor(max_workers=2) as executor:
                reports = list(executor.map(scan, ("family_office", "financial_markets")))

        self.assertEqual(feed.calls, 1)
        self.assertEqual(sum(item["reused_scan_count"] for item in reports), 1)
        self.assertEqual(
            self.database.connection.execute("SELECT COUNT(*) FROM intel_scan_runs").fetchone()[0],
            1,
        )
        run = self.database.connection.execute(
            "SELECT scan_window_key, requested_pack_ids_json, status FROM intel_scan_runs"
        ).fetchone()
        self.assertTrue(run["scan_window_key"].startswith("source:"))
        self.assertEqual(json.loads(run["requested_pack_ids_json"]), ["financial_markets"])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(
            self.database.connection.execute("SELECT COUNT(*) FROM intel_candidates").fetchone()[0],
            1,
        )
        self.assertEqual(
            [row[0] for row in self.database.connection.execute(
                "SELECT industry_pack_id FROM intel_candidate_industries WHERE should_queue=1"
            )],
            ["financial_markets"],
        )

        adapter = _OneArticleAdapter(self.database)
        dispatcher = IntelCandidateDispatcher(
            repository=candidates,
            adapter=adapter,
            worker_id="shared-source-test",
        )
        first_dispatch = dispatcher.dispatch_once(limit=10, manual=True)
        second_dispatch = dispatcher.dispatch_once(limit=10, manual=True)
        self.assertEqual(first_dispatch["crawled"], 1)
        self.assertEqual(second_dispatch["claimed"], 0)
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(
            self.database.connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
            1,
        )
        associations = [
            row[0]
            for row in self.database.connection.execute(
                "SELECT industry_pack_id FROM content_industry_packs "
                "WHERE content_type='article' AND is_active=1"
            )
        ]
        self.assertEqual(associations, ["financial_markets"])
        loader = IndustryPackLoader()
        self.assertIn("financial_markets", loader.compose("family_office")["effective_pack_ids"])
        self.assertIn("financial_markets", loader.compose("financial_markets")["effective_pack_ids"])

    def test_disabled_source_can_be_reenabled_without_duplicate_identity(self):
        source_id = self._insert_financial_source()
        scanner = IntelLightScanner(
            candidate_repository=IntelCandidateRepository(self.database),
            source_registry=self.registry,
            rss_scanner=_CountingRSSScanner(),
            list_scanner=_NoopScanner(),
            url_validator=lambda value: value,
        )
        self.database.connection.execute(
            "UPDATE intel_sources SET is_enabled=0 WHERE id=?", (source_id,)
        )
        self.assertEqual(scanner._enabled_sources("family_office", [source_id], 1), [])
        self.database.connection.execute(
            "UPDATE intel_sources SET is_enabled=1 WHERE id=?", (source_id,)
        )
        enabled = scanner._enabled_sources("family_office", [source_id], 1)
        self.assertEqual(len(enabled), 1)
        self.assertEqual(enabled[0]["target_pack_ids"], ["financial_markets"])
        self.assertEqual(
            self.database.connection.execute("SELECT COUNT(*) FROM intel_sources").fetchone()[0],
            1,
        )

    def test_one_canonical_article_can_receive_two_content_pack_labels(self):
        candidates = IntelCandidateRepository(self.database)
        item = {
            "url": "https://example.test/family-office-market-policy",
            "title": "Hong Kong Family Office capital market policy",
            "summary": "Family office investment under new securities market regulatory rules",
            "published_at": "2026-07-31T09:00:00+08:00",
        }
        for pack_id in ("family_office", "financial_markets"):
            result = candidates.discover(
                item,
                industry_pack_id=pack_id,
                observation_type="rss",
            )
            self.assertTrue(result["should_queue"])
        self.assertEqual(
            self.database.connection.execute("SELECT COUNT(*) FROM intel_candidates").fetchone()[0],
            1,
        )
        self.assertEqual(candidates.get_candidate_industry_pack_ids(1), ["family_office", "financial_markets"])

        adapter = _OneArticleAdapter(self.database)
        dispatcher = IntelCandidateDispatcher(
            repository=candidates,
            adapter=adapter,
            worker_id="multi-label-test",
        )
        self.assertEqual(dispatcher.dispatch_once(limit=10, manual=True)["crawled"], 1)
        labels = [
            row[0]
            for row in self.database.connection.execute(
                "SELECT industry_pack_id FROM content_industry_packs ORDER BY industry_pack_id"
            )
        ]
        self.assertEqual(labels, ["family_office", "financial_markets"])
        self.assertEqual(adapter.calls, 1)

    def test_scan_window_uses_canonical_url_and_poll_interval(self):
        source_a = {
            "canonical_source_url": "https://example.test/feed.xml",
            "source_url": "https://mirror.invalid/feed.xml",
            "polling_interval_minutes": 5,
        }
        source_b = {
            **source_a,
            "source_url": "https://different.invalid/feed.xml",
        }
        first = source_scan_window_key(source_a, epoch_seconds=600)
        self.assertEqual(first, source_scan_window_key(source_b, epoch_seconds=899))
        self.assertNotEqual(first, source_scan_window_key(source_a, epoch_seconds=900))


if __name__ == "__main__":
    unittest.main()

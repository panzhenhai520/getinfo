#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

_BOOTSTRAP_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = os.path.join(_BOOTSTRAP_TEMP_DIR.name, "bootstrap.sqlite3")
os.environ["INTEL_LLM_ENABLED"] = "false"

import config  # noqa: E402
from intel_candidates import IntelCandidateRepository  # noqa: E402
from intel_http import ExternalFetchError
from intel_light_scanner import IntelLightScanner, RSSScanner
from intel_sources import IntelSourceRegistry
from rss_feed_contract import RSSFeedContractError
from sqlite_database import SQLiteDatabase


class _FakeScanner:
    def __init__(self, items=None, error=None):
        self.items = list(items or [])
        self.error = error

    def scan(self, _source, *, limit):
        if self.error:
            raise self.error
        return self.items[:limit]


class _StaticHTTP:
    def __init__(self, content):
        self.content = content

    def get(self, *_args, **_kwargs):
        from intel_http import HTTPFetchResult

        return HTTPFetchResult(
            "https://official.example/feed.xml",
            200,
            self.content,
            "application/xml",
            "utf-8",
        )


class FinancialRSSScanTests(unittest.TestCase):
    def setUp(self):
        # 金融 RSS 源在扫描前会被两道"与 RSS 解析无关"的闸门过滤：
        #   1) IntelLightScanner._enabled_sources() 对 financial_markets 的 RSS 源
        #      检查灰度能力 rollout_capability_enabled("rss", config)，而本机 .env
        #      是 FINANCIAL_ROLLOUT_STAGE=off（fail-closed）；
        #   2) scan() 末尾的 Tavily 搜索分支（TAVILY_ENABLED=True 且本地已配 Key）
        #      会真的走外网并额外写入 intel_scan_runs，污染本文的审计断言。
        # 两者都不属于本文件要验证的"RSS 扫描审计/健康度"行为，显式关掉。
        #   3) conftest 的 DATABASE_TYPE=sqlite 会被 .env 覆盖（config 里仍是
        #      postgres），SQLiteDatabase(path) 只改路径不改后端，会把本用例的
        #       信源/扫描行写进共享主库。这里强制回到临时 SQLite。
        for item in (
            patch.object(config, "FINANCIAL_ROLLOUT_STAGE", "simulation_backtest"),
            patch.object(config, "TAVILY_ENABLED", False),
            patch.object(config, "DATABASE_TYPE", "sqlite"),
            patch("db_connection.database_type", lambda: "sqlite"),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "financial-scan.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())
        self.candidates = IntelCandidateRepository(self.db)
        self.sources = IntelSourceRegistry(self.db)
        self.sources.ensure_pack_default_sources("financial_markets")
        registered, _ = self.sources.list_sources(
            industry_pack_id="financial_markets", source_type="rss", page=1, per_page=100
        )
        self.source_id = registered[0]["id"]

    def tearDown(self):
        self.db.disconnect()
        self.temp_dir.cleanup()

    def _scanner(self, fake):
        return IntelLightScanner(
            candidate_repository=self.candidates,
            source_registry=self.sources,
            rss_scanner=fake,
            list_scanner=_FakeScanner(),
            url_validator=lambda value: value,
        )

    def test_scan_audit_fields_duplicate_entry_and_missing_publish_time(self):
        entry = {
            "url": "https://official.example/markets/a-share-policy",
            "title": "A股资本市场监管政策发布",
            "summary": "中国证监会发布证券市场监管政策",
            "published_at": "",
        }
        result = self._scanner(_FakeScanner([entry, dict(entry)])).scan(
            industry_pack_id="financial_markets",
            source_ids=[self.source_id],
            include_serpapi=False,
            max_sources=1,
            max_items_per_source=10,
            manual=True,
        )
        self.assertTrue(result["request_id"].startswith("scan-"))
        self.assertGreaterEqual(result["duration_ms"], 0)
        self.assertEqual(result["discovered_count"], 2)
        self.assertEqual(result["runs"][0]["duplicate_count"], 1)
        self.assertEqual(result["runs"][0]["status"], "completed")
        run = self.db.connection.execute(
            "SELECT metadata_json FROM intel_scan_runs WHERE id=?",
            (result["runs"][0]["run_id"],),
        ).fetchone()
        metadata = json.loads(run["metadata_json"])
        self.assertEqual(metadata["request_id"], result["request_id"])
        self.assertIn("duration_ms", metadata)
        self.assertEqual(
            self.db.connection.execute("SELECT COUNT(*) FROM intel_candidates").fetchone()[0],
            1,
        )

    def test_timeout_403_and_malformed_xml_fail_without_fake_candidates(self):
        errors = (
            TimeoutError("feed timed out"),
            ExternalFetchError("HTTP 403"),
            RSSFeedContractError("RSS XML 解析失败"),
        )
        for error in errors:
            result = self._scanner(_FakeScanner(error=error)).scan(
                industry_pack_id="financial_markets",
                source_ids=[self.source_id],
                include_serpapi=False,
                max_sources=1,
                max_items_per_source=10,
                manual=True,
            )
            self.assertEqual(result["failed_count"], 1)
            self.assertEqual(result["runs"][0]["status"], "failed")
        self.assertEqual(
            self.db.connection.execute("SELECT COUNT(*) FROM intel_candidates").fetchone()[0],
            0,
        )
        source = self.sources.get_source(self.source_id)
        self.assertEqual(source["consecutive_scan_failures"], 3)

    def test_rss_parser_accepts_one_missing_date_but_rejects_malformed_document(self):
        no_date = """<rss><channel><item><title>A股市场公告</title>
        <link>https://official.example/a-share</link></item></channel></rss>""".encode("utf-8")
        items = RSSScanner(_StaticHTTP(no_date)).scan(
            {"source_url": "https://official.example/feed.xml"}, limit=10
        )
        self.assertEqual(items[0]["published_at"], "")
        with self.assertRaises(RSSFeedContractError):
            RSSScanner(_StaticHTTP(b"<rss><broken>")).scan(
                {"source_url": "https://official.example/feed.xml"}, limit=10
            )

    def test_temporary_timeout_can_be_retried_and_success_resets_source_health(self):
        failed = self._scanner(_FakeScanner(error=TimeoutError("temporary timeout"))).scan(
            industry_pack_id="financial_markets",
            source_ids=[self.source_id],
            include_serpapi=False,
            max_sources=1,
            manual=True,
        )
        self.assertEqual(failed["failed_count"], 1)
        unhealthy = self.sources.get_source(self.source_id)
        self.assertEqual(unhealthy["last_scan_status"], "failed")
        self.assertEqual(unhealthy["consecutive_scan_failures"], 1)
        self.assertFalse(unhealthy["last_successful_scan_at"])

        recovered = self._scanner(
            _FakeScanner(
                [
                    {
                        "url": "https://official.example/markets/recovered",
                        "title": "SFC capital market regulatory framework update",
                        "summary": "Securities market trading rules",
                        "published_at": "2026-07-31T09:00:00+08:00",
                    }
                ]
            )
        ).scan(
            industry_pack_id="financial_markets",
            source_ids=[self.source_id],
            include_serpapi=False,
            max_sources=1,
            manual=True,
        )
        self.assertEqual(recovered["failed_count"], 0)
        self.assertEqual(recovered["runs"][0]["status"], "completed")
        healthy = self.sources.get_source(self.source_id)
        self.assertEqual(healthy["last_scan_status"], "completed")
        self.assertEqual(healthy["consecutive_scan_failures"], 0)
        self.assertEqual(healthy["last_scan_error"], "")
        self.assertTrue(healthy["last_successful_scan_at"])
        self.assertEqual(
            self.db.connection.execute("SELECT COUNT(*) FROM intel_candidates").fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()

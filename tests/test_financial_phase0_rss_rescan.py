#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


_BOOTSTRAP_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = os.path.join(_BOOTSTRAP_TEMP_DIR.name, "bootstrap.sqlite3")
os.environ["INTEL_LLM_ENABLED"] = "false"
os.environ["CRAWL_REQUIRE_KEYWORD_MATCH"] = "false"

import config
from candidate_crawler_adapter import CandidateCrawlerAdapter
from candidate_dispatcher import IntelCandidateDispatcher
from intel_candidates import IntelCandidateRepository
from intel_classifier import IntelClassificationService
from intel_database import IntelRepository
from intel_light_scanner import IntelLightScanner
from intel_sources import IntelSourceRegistry
from sqlite_database import SQLiteDatabase
from tools.check_intel_worker_health import health_report


class _MutableScanner:
    def __init__(self, items=None):
        self.items = list(items or [])

    def scan(self, _source, *, limit):
        return [dict(item) for item in self.items[:limit]]


class _NoopScanner:
    def scan(self, _source, *, limit):
        return []


class _Extractor:
    def __init__(self):
        self.calls = []

    def crawl_article_content(self, url, timeout):
        self.calls.append(url)
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        return {
            "success": True,
            "url": url,
            "title": f"SFC capital market regulatory framework {slug}",
            "content": (
                f"The Securities and Futures Commission announced {slug} as a capital market "
                "regulatory framework for securities market trading and disclosure. "
            )
            * 24
            # 正文质量闸门（intel_content_quality_gate._content_sanity）要求
            # ≥800 字的内容至少有 2 个真实段落/实质行，单块长文本会被判
            # no_real_paragraphs 并进 retry_wait。这里补上真实分段。
            + "\n\nThe framework covers disclosure duties, market conduct and "
            "supervisory reporting for licensed intermediaries.\n\n"
            "It applies to all regulated securities market participants.",
            "publish_date": "2026-07-31",
            "site_name": "Official SFC",
            "extraction_method": "fixture",
            "quality_score": 95,
        }


class FinancialRSSRescanTests(unittest.TestCase):
    def setUp(self):
        self.original_keyword_guard = config.CRAWL_REQUIRE_KEYWORD_MATCH
        self.original_llm_enabled = config.INTEL_LLM_ENABLED
        config.CRAWL_REQUIRE_KEYWORD_MATCH = False
        config.INTEL_LLM_ENABLED = False
        # 金融 RSS 源会被 rollout_capability_enabled("rss", config) 过滤，
        # 而本机 .env 是 FINANCIAL_ROLLOUT_STAGE=off（fail-closed）；
        # 扫描末尾的 Tavily 搜索分支（本地已配 Key）会额外走外网并写入
        # intel_scan_runs，污染本文的重扫断言。两者都与本文件验证的
        # "重复条目幂等/新条目只流转一次"无关，显式关掉。
        for item in (
            patch.object(config, "FINANCIAL_ROLLOUT_STAGE", "simulation_backtest"),
            patch.object(config, "TAVILY_ENABLED", False),
            # conftest 的 DATABASE_TYPE=sqlite 被 .env 覆盖，config 里仍是
            # postgres；这里让 SQLiteDatabase/db_connection 真正落在临时库，
            # 避免读写共享主库。
            patch.object(config, "DATABASE_TYPE", "sqlite"),
            patch("db_connection.database_type", lambda: "sqlite"),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "financial-rescan.sqlite3")
        self.db = SQLiteDatabase(self.db_path)
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())
        self.db.analyze_article_spacetime_profile = lambda _article_id: None
        self.candidates = IntelCandidateRepository(self.db)
        self.sources = IntelSourceRegistry(self.db)
        self.repository = IntelRepository(self.db)
        self.classifier = IntelClassificationService(self.repository)
        self.source_id = self._insert_source()
        self.feed = _MutableScanner()
        self.scanner = IntelLightScanner(
            candidate_repository=self.candidates,
            source_registry=self.sources,
            rss_scanner=self.feed,
            list_scanner=_NoopScanner(),
            url_validator=lambda value: value,
        )
        self.extractor = _Extractor()
        self.scan_epoch = 1785456000.0
        self.dispatcher = IntelCandidateDispatcher(
            repository=self.candidates,
            adapter=CandidateCrawlerAdapter(
                database=self.db,
                candidate_repository=self.candidates,
                extractor_factory=lambda _db: self.extractor,
                url_validator=lambda value: value,
                redirect_validator=lambda value: value,
            ),
            worker_id="financial-rescan",
        )

    def tearDown(self):
        self.db.disconnect()
        self.temp_dir.cleanup()
        config.CRAWL_REQUIRE_KEYWORD_MATCH = self.original_keyword_guard
        config.INTEL_LLM_ENABLED = self.original_llm_enabled

    def _insert_source(self) -> int:
        cursor = self.db.connection.execute(
            """
            INSERT INTO intel_sources (
                canonical_source_url, source_url, source_name, source_type,
                content_type, market, authority_level, polling_interval_minutes,
                is_enabled, metadata_json
            ) VALUES (?, ?, 'Official financial RSS', 'rss', 'official', 'HK', 5, 1440, 1, '{}')
            """,
            ("https://official.example/feed.xml", "https://official.example/feed.xml"),
        )
        source_id = int(cursor.lastrowid)
        self.db.connection.execute(
            "INSERT INTO intel_source_industries (source_id, industry_pack_id) VALUES (?, 'financial_markets')",
            (source_id,),
        )
        self.db.connection.commit()
        return source_id

    @staticmethod
    def _entry(slug: str) -> dict:
        return {
            "url": f"https://official.example/markets/{slug}",
            "title": f"SFC capital market regulatory framework {slug}",
            "summary": "Securities market trading rules and disclosure",
            "published_at": "Fri, 31 Jul 2026 09:00:00 +0800",
        }

    def _scan(self) -> dict:
        with patch("intel_light_scanner.time.time", return_value=self.scan_epoch):
            return self.scanner.scan(
                industry_pack_id="financial_markets",
                source_ids=[self.source_id],
                include_serpapi=False,
                max_sources=1,
                max_items_per_source=20,
                manual=True,
            )

    def _count(self, table: str, where: str = "") -> int:
        return int(
            self.db.connection.execute(
                f"SELECT COUNT(*) FROM {table} {where}"
            ).fetchone()[0]
        )

    def test_repeat_entry_is_idempotent_and_new_entry_flows_once(self):
        first_entry = self._entry("first")
        self.feed.items = [first_entry]
        first_scan = self._scan()
        self.assertEqual(first_scan["runs"][0]["duplicate_count"], 0)
        first_dispatch = self.dispatcher.dispatch_once(limit=10, manual=True)
        self.assertEqual(first_dispatch["crawled"], 1)
        first_candidate = self.db.connection.execute(
            "SELECT id,article_id,status FROM intel_candidates"
        ).fetchone()
        self.classifier.classify_article_id(
            int(first_candidate["article_id"]), "financial_markets"
        )
        before = {
            "articles": self._count("articles"),
            "tasks": self._count("crawl_tasks"),
            "candidates": self._count("intel_candidates"),
            "observations": self._count("intel_candidate_observations"),
            "classification_jobs": self._count(
                "intel_jobs", "WHERE job_type='classification'"
            ),
        }

        second_scan = self._scan()
        self.assertEqual(second_scan["reused_scan_count"], 1)
        self.assertEqual(second_scan["runs"][0]["status"], "skipped")
        self.assertEqual(self.dispatcher.dispatch_once(limit=10, manual=True)["claimed"], 0)
        after_repeat = {
            "articles": self._count("articles"),
            "tasks": self._count("crawl_tasks"),
            "candidates": self._count("intel_candidates"),
            "observations": self._count("intel_candidate_observations"),
            "classification_jobs": self._count(
                "intel_jobs", "WHERE job_type='classification'"
            ),
        }
        self.assertEqual(after_repeat, before)
        observation = self.db.connection.execute(
            "SELECT seen_count FROM intel_candidate_observations"
        ).fetchone()
        self.assertEqual(observation["seen_count"], 1)

        self.feed.items = [first_entry, self._entry("second")]
        self.scan_epoch += 86400
        third_scan = self._scan()
        self.assertEqual(third_scan["discovered_count"], 2)
        self.assertEqual(third_scan["runs"][0]["duplicate_count"], 1)
        self.assertEqual(self._count("intel_candidates"), 2)
        self.assertEqual(
            self._count("intel_candidates", "WHERE status='queued'"),
            1,
        )
        second_dispatch = self.dispatcher.dispatch_once(limit=10, manual=True)
        self.assertEqual(second_dispatch["crawled"], 1)
        self.assertEqual(self._count("articles"), 2)
        self.assertEqual(self._count("crawl_tasks"), 2)
        self.assertEqual(len(self.extractor.calls), 2)
        health = self.sources.get_source(self.source_id)
        self.assertEqual(health["last_scan_status"], "completed")
        self.assertEqual(health["consecutive_scan_failures"], 0)

    def test_worker_health_probe_checks_process_and_database(self):
        proc_root = Path(self.temp_dir.name) / "proc"
        worker_dir = proc_root / "101"
        worker_dir.mkdir(parents=True)
        (worker_dir / "cmdline").write_bytes(b"python\0/app/intel_worker.py\0")
        report = health_report(self.db_path, proc_root=proc_root, require_process=True)
        self.assertTrue(report["healthy"], report)
        self.assertEqual(report["worker_process_count"], 1)
        (worker_dir / "cmdline").write_bytes(b"python\0other_worker.py\0")
        missing = health_report(self.db_path, proc_root=proc_root, require_process=True)
        self.assertFalse(missing["healthy"])
        self.assertTrue(any("not running" in error for error in missing["errors"]))

    def test_schedule_catches_up_after_busy_minute_and_bounds_failure_retries(self):
        self.sources.update_source_metadata(
            self.source_id,
            {"preferred_scan_time": "08:30", "schedule_rule": "daily"},
        )
        # The method only relies on the supplied aware local clock.  Use a
        # fixed +08:00 instance so this test does not depend on the host TZ.
        ten_hk = datetime.fromisoformat("2026-07-31T10:00:00+08:00")
        self.assertEqual(
            self.sources.due_source_ids("financial_markets", ten_hk),
            [self.source_id],
        )

        self.db.connection.execute(
            """
            UPDATE intel_sources
            SET last_scan_at='2026-07-31T01:00:00Z',
                last_scan_status='completed', consecutive_scan_failures=0
            WHERE id=?
            """,
            (self.source_id,),
        )
        self.db.connection.commit()
        self.assertEqual(
            self.sources.due_source_ids("financial_markets", ten_hk),
            [],
        )

        self.db.connection.execute(
            """
            UPDATE intel_sources
            SET last_scan_at='2026-07-31T01:00:00Z',
                last_scan_status='failed', consecutive_scan_failures=1
            WHERE id=?
            """,
            (self.source_id,),
        )
        self.db.connection.commit()
        too_soon = datetime.fromisoformat("2026-07-31T09:04:00+08:00")
        retry_due = datetime.fromisoformat("2026-07-31T09:05:00+08:00")
        self.assertEqual(
            self.sources.due_source_ids("financial_markets", too_soon),
            [],
        )
        self.assertEqual(
            self.sources.due_source_ids("financial_markets", retry_due),
            [self.source_id],
        )
        self.db.connection.execute(
            "UPDATE intel_sources SET consecutive_scan_failures=3 WHERE id=?",
            (self.source_id,),
        )
        self.db.connection.commit()
        self.assertEqual(
            self.sources.due_source_ids("financial_markets", ten_hk),
            [],
        )


if __name__ == "__main__":
    unittest.main()

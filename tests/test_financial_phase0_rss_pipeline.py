#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

_BOOTSTRAP_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = os.path.join(_BOOTSTRAP_TEMP_DIR.name, "bootstrap.sqlite3")
os.environ["INTEL_LLM_ENABLED"] = "false"
os.environ["CRAWL_REQUIRE_KEYWORD_MATCH"] = "false"

import config  # noqa: E402
from candidate_crawler_adapter import CandidateCrawlerAdapter
from candidate_dispatcher import IntelCandidateDispatcher
from intel_candidates import IntelCandidateRepository
from intel_classifier import IntelClassificationService
from intel_database import IntelRepository
from intel_worker import IntelWorker
from sqlite_database import SQLiteDatabase
from tools.trace_financial_rss_pipeline import trace_pipeline


class _Extractor:
    def __init__(self):
        self.calls = 0

    def crawl_article_content(self, url, timeout):
        self.calls += 1
        return {
            "success": True,
            "url": url,
            "title": "SFC announces capital market regulatory framework",
            "content": (
                "The Securities and Futures Commission announced a capital market "
                "regulatory framework for securities market trading. "
            )
            * 20
            # 正文质量闸门（intel_content_quality_gate._content_sanity）要求
            # ≥800 字的内容至少有 2 个真实段落/实质行，单块长文本会被判
            # no_real_paragraphs。这里给出真实的空行分段正文。
            + "\n\nThe framework covers disclosure duties, market conduct and "
            "supervisory reporting for licensed intermediaries.\n\n"
            "It takes effect after a consultation period and applies to all "
            "regulated securities market participants in Hong Kong.",
            "publish_date": "2026-07-31",
            "site_name": "SFC",
            "extraction_method": "fixture",
            "quality_score": 95,
        }


class FinancialRSSPipelineTests(unittest.TestCase):
    def setUp(self):
        self.original_keyword_guard = config.CRAWL_REQUIRE_KEYWORD_MATCH
        self.original_llm_enabled = config.INTEL_LLM_ENABLED
        config.CRAWL_REQUIRE_KEYWORD_MATCH = False
        # Full-suite module import order may load the production .env before
        # this test module.  Keep the fixture deterministic and offline.
        config.INTEL_LLM_ENABLED = False
        # conftest 把 DATABASE_TYPE/DATABASE_PATH 指向临时 SQLite，但 config 在
        # 导入时就固化了 .env 的 DATABASE_TYPE=postgres，于是 SQLiteDatabase
        # 把 backend 判成 postgres，db_connection.connect_database() 也一律走
        # 共享主库。本用例的候选入库/派发必须落在这台临时库上，否则共享主库
        # 里堆积的候选会抢走认领额度（实测 claim → 0，派发 → 0）。
        for item in (
            patch.object(config, "DATABASE_TYPE", "sqlite"),
            patch("db_connection.database_type", lambda: "sqlite"),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "financial-pipeline.sqlite3")
        self.db = SQLiteDatabase(self.db_path)
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())
        self.db.analyze_article_spacetime_profile = lambda _article_id: None
        self.candidates = IntelCandidateRepository(self.db)
        self.repository = IntelRepository(self.db)
        self.source_id = self._insert_source()

    def tearDown(self):
        self.db.disconnect()
        self.temp_dir.cleanup()
        config.CRAWL_REQUIRE_KEYWORD_MATCH = self.original_keyword_guard
        config.INTEL_LLM_ENABLED = self.original_llm_enabled

    def _insert_source(self):
        cursor = self.db.connection.execute(
            """
            INSERT INTO intel_sources (
                canonical_source_url, source_url, source_name, source_type,
                content_type, market, authority_level, is_enabled, metadata_json
            ) VALUES (
                'https://official.example/feed.xml',
                'https://official.example/feed.xml',
                'Official financial feed', 'rss', 'official', 'HK', 5, 1, '{}'
            )
            """
        )
        source_id = int(cursor.lastrowid)
        self.db.connection.execute(
            "INSERT INTO intel_source_industries (source_id, industry_pack_id) VALUES (?, 'financial_markets')",
            (source_id,),
        )
        self.db.connection.commit()
        return source_id

    def test_source_candidate_article_classification_trace_and_idempotency(self):
        run_id = self.candidates.create_scan_run(
            source_id=self.source_id,
            industry_pack_id="financial_markets",
            scanner_type="rss",
        )
        discovered = self.candidates.discover(
            {
                "url": "https://official.example/news/sfc-framework",
                "title": "SFC announces capital market regulatory framework",
                "summary": "Securities market trading rules",
                "published_at": "Fri, 31 Jul 2026 09:00:00 +0800",
            },
            industry_pack_id="financial_markets",
            source_id=self.source_id,
            scan_run_id=run_id,
            observation_type="rss",
        )
        self.assertTrue(discovered["should_queue"])
        self.candidates.finish_scan_run(
            run_id,
            {"status": "completed", "discovered_count": 1, "queued_count": 1},
        )
        extractor = _Extractor()
        adapter = CandidateCrawlerAdapter(
            database=self.db,
            candidate_repository=self.candidates,
            extractor_factory=lambda _db: extractor,
            url_validator=lambda value: value,
            redirect_validator=lambda value: value,
        )
        dispatcher = IntelCandidateDispatcher(
            repository=self.candidates,
            adapter=adapter,
            worker_id="financial-pipeline",
        )
        first = dispatcher.dispatch_once(limit=1, manual=True)
        self.assertEqual(first["crawled"], 1)
        candidate = self.db.connection.execute(
            "SELECT * FROM intel_candidates WHERE id=?",
            (discovered["candidate_id"],),
        ).fetchone()
        article_id = int(candidate["article_id"])
        jobs = self.db.connection.execute(
            "SELECT payload_json FROM intel_jobs WHERE job_type='classification'"
        ).fetchall()
        self.assertTrue(
            any('"industry_pack_id": "financial_markets"' in row["payload_json"] for row in jobs)
        )

        worker = IntelWorker(
            repository=self.repository,
            classification_service=IntelClassificationService(self.repository),
            worker_id="financial-classifier",
        )
        result = worker.run_once(job_types=["classification"], limit=10)
        self.assertGreaterEqual(result["completed"], 1)
        classified = self.db.connection.execute(
            "SELECT final_category FROM article_intel_classifications WHERE article_id=? AND industry_pack_id='financial_markets'",
            (article_id,),
        ).fetchone()
        self.assertIsNotNone(classified)
        self.assertIn(classified["final_category"], {"trend", "event", "other"})

        rediscovered = self.candidates.discover(
            {
                "url": "https://official.example/news/sfc-framework",
                "title": "SFC announces capital market regulatory framework",
                "summary": "Securities market trading rules",
                "published_at": "Fri, 31 Jul 2026 09:00:00 +0800",
            },
            industry_pack_id="financial_markets",
            source_id=self.source_id,
            scan_run_id=run_id,
            observation_type="rss",
        )
        self.assertEqual(
            rediscovered["classification_job"]["industry_pack_id"],
            "financial_markets",
        )

        before_articles = self.db.connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        before_jobs = self.db.connection.execute(
            "SELECT COUNT(*) FROM intel_jobs WHERE job_type='classification'"
        ).fetchone()[0]
        adapter.dispatch(dict(candidate))
        self.assertEqual(
            self.db.connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
            before_articles,
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM intel_jobs WHERE job_type='classification'"
            ).fetchone()[0],
            before_jobs,
        )

        trace = trace_pipeline(self.db_path)
        self.assertTrue(trace["all_sources_complete"])
        self.assertEqual(trace["sources"][0]["sample_chain"]["article_id"], article_id)

    def test_dispatch_claim_can_be_restricted_to_target_candidate_ids(self):
        run_id = self.candidates.create_scan_run(
            source_id=self.source_id,
            industry_pack_id="financial_markets",
            scanner_type="rss",
        )
        first = self.candidates.discover(
            {
                "url": "https://official.example/news/first",
                "title": "SFC capital market update one",
                "summary": "Securities market announcement",
                "published_at": "Fri, 31 Jul 2026 09:00:00 +0800",
            },
            industry_pack_id="financial_markets",
            source_id=self.source_id,
            scan_run_id=run_id,
            observation_type="rss",
        )
        second = self.candidates.discover(
            {
                "url": "https://official.example/news/second",
                "title": "SFC capital market update two",
                "summary": "Securities market announcement",
                "published_at": "Fri, 31 Jul 2026 09:01:00 +0800",
            },
            industry_pack_id="financial_markets",
            source_id=self.source_id,
            scan_run_id=run_id,
            observation_type="rss",
        )

        claimed = self.candidates.claim_candidates(
            "targeted-news-test",
            limit=10,
            candidate_ids=[second["candidate_id"]],
        )

        self.assertEqual([item["id"] for item in claimed], [second["candidate_id"]])
        remaining = self.db.connection.execute(
            "SELECT status FROM intel_candidates WHERE id=?",
            (first["candidate_id"],),
        ).fetchone()
        self.assertEqual(remaining["status"], "queued")


if __name__ == "__main__":
    unittest.main()

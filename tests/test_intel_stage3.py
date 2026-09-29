#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

_BOOTSTRAP_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = os.path.join(
    _BOOTSTRAP_TEMP_DIR.name,
    "bootstrap.sqlite3",
)
os.environ["INTEL_LLM_ENABLED"] = "false"
os.environ["CRAWL_REQUIRE_KEYWORD_MATCH"] = "false"

from flask import Flask

import intel_api
import config
from candidate_crawler_adapter import CandidateCrawlerAdapter
from candidate_dispatcher import IntelCandidateDispatcher
from intel_api import intel_bp
from intel_candidates import IntelCandidateRepository
from intel_database import IntelRepository
from intel_http import (
    HTTPFetchResult,
    SafeHTTPClient,
    UnsafeExternalURLError,
    sanitize_external_error,
    validate_external_url,
)
from intel_light_scanner import (
    IntelLightScanner,
    ListPageScanner,
    RSSScanner,
    serpapi_preview_gate,
)
from industry_packs import IndustryPackLoader
from intel_sources import IntelSourceRegistry
from intel_worker import IntelWorker
from serpapi_client import SerpAPIClient
from sqlite_database import SQLiteDatabase


class _StaticHTTP:
    def __init__(self, content, url, content_type):
        self.result = HTTPFetchResult(url, 200, content, content_type, "utf-8")

    def get(self, *_args, **_kwargs):
        return self.result


class _FakeScanner:
    def __init__(self, items=None, error=None):
        self.items = items or []
        self.error = error

    def scan(self, _source, *, limit):
        if self.error:
            raise self.error
        return self.items[:limit]


class _FakeExtractor:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def crawl_article_content(self, _url, timeout):
        self.calls += 1
        return dict(self.result)


class _RedirectResponse:
    def __init__(self, status_code, url, location=""):
        self.status_code = status_code
        self.url = url
        self.headers = {"Location": location} if location else {}
        self.encoding = "utf-8"

    def close(self):
        return None

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=65536):
        return iter(())


class IntelStageThreeTests(unittest.TestCase):
    def setUp(self):
        self._original_keyword_guard = config.CRAWL_REQUIRE_KEYWORD_MATCH
        self._original_llm_enabled = config.INTEL_LLM_ENABLED
        config.CRAWL_REQUIRE_KEYWORD_MATCH = False
        # Keep dispatcher tests deterministic even when another test imports config
        # after a developer .env has enabled the production admission LLM.
        config.INTEL_LLM_ENABLED = False
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "stage3.sqlite3")
        self.db = SQLiteDatabase(self.db_path)
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())
        self.db.analyze_article_spacetime_profile = lambda _article_id: None
        self.candidates = IntelCandidateRepository(self.db)
        self.jobs = IntelRepository(self.db)
        self.sources = IntelSourceRegistry(self.db)

    def tearDown(self):
        self.db.disconnect()
        self.temp_dir.cleanup()
        config.CRAWL_REQUIRE_KEYWORD_MATCH = self._original_keyword_guard
        config.INTEL_LLM_ENABLED = self._original_llm_enabled

    def _discover(self, url, title, pack="family_office", observation="rss"):
        return self.candidates.discover(
            {
                "url": url,
                "title": title,
                "summary": "香港家族办公室政策及税务宽免报告",
                "published_at": "2026-07-27",
            },
            industry_pack_id=pack,
            observation_type=observation,
            query_text="test query",
        )

    def _insert_source(self, source_type="rss", url="https://feeds.example.com/rss.xml"):
        cursor = self.db.connection.cursor()
        cursor.execute(
            """
            INSERT INTO intel_sources (
                canonical_source_url, source_url, source_name, source_type,
                content_type, market, authority_level, is_enabled
            ) VALUES (?, ?, ?, ?, 'media', 'HK', 3, 1)
            """,
            (url, url, f"test {source_type}", source_type),
        )
        source_id = int(cursor.lastrowid)
        cursor.execute(
            """
            INSERT INTO intel_source_industries (source_id, industry_pack_id)
            VALUES (?, 'family_office')
            """,
            (source_id,),
        )
        self.db.connection.commit()
        cursor.close()
        return source_id

    def test_serpapi_preview_gate_requires_query_topic_and_industry_anchor(self):
        pack = IndustryPackLoader(use_published_store=False).load("family_office")
        query = '香港 ("家族办公室") 政策'
        pack["serpapi_query_gates"] = {query: ["家族办公室"]}

        self.assertTrue(
            serpapi_preview_gate(
                {
                    "title": "香港家族办公室监管政策更新",
                    "summary": "家办税务宽免安排发布。",
                },
                pack,
                query,
            )
        )
        self.assertFalse(
            serpapi_preview_gate(
                {
                    "title": "香港家族信托监管政策更新",
                    "summary": "财富传承安排发布。",
                },
                pack,
                query,
            )
        )
        self.assertFalse(
            serpapi_preview_gate(
                {
                    "title": "家族办公室软件更新",
                    "summary": "普通软件产品发布。",
                },
                {**pack, "candidate_gate": {"anchor_keywords": ["汽车"]}},
                query,
            )
        )

    def test_candidate_url_dedupe_evidence_multi_industry_and_title_audit(self):
        first = self._discover(
            "https://news.example.com/a?story=1&utm_source=rss#part",
            "香港家族办公室政策发布",
        )
        same = self.candidates.discover(
            {
                "url": "https://news.example.com/a?story=1&utm_medium=search",
                "title": "香港家族办公室政策发布",
                "summary": "OpenAI 与家族办公室合作",
            },
            industry_pack_id="ai_news",
            observation_type="serpapi",
            query_text="AI family office",
        )
        self.assertEqual(first["candidate_id"], same["candidate_id"])
        self.assertFalse(same["created"])
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM intel_candidate_observations"
            ).fetchone()[0],
            2,
        )
        industries = self.db.connection.execute(
            """
            SELECT industry_pack_id FROM intel_candidate_industries
            WHERE candidate_id=? ORDER BY industry_pack_id
            """,
            (first["candidate_id"],),
        ).fetchall()
        self.assertEqual(
            [row["industry_pack_id"] for row in industries],
            ["ai_news", "family_office"],
        )

        suspicious = self._discover(
            "https://news.example.com/print/a?story=1",
            "香港家族办公室政策发布",
        )
        self.assertNotEqual(suspicious["candidate_id"], first["candidate_id"])
        self.assertEqual(suspicious["possible_duplicate_of"], first["candidate_id"])
        row = self.db.connection.execute(
            "SELECT duplicate_reason FROM intel_candidates WHERE id=?",
            (suspicious["candidate_id"],),
        ).fetchone()
        self.assertEqual(row["duplicate_reason"], "same_domain_and_title_hash")

        below = self.candidates.discover(
            {"url": "https://news.example.com/weather", "title": "今日天气"},
            industry_pack_id="family_office",
            observation_type="list_page",
        )
        status = self.db.connection.execute(
            "SELECT status FROM intel_candidates WHERE id=?",
            (below["candidate_id"],),
        ).fetchone()["status"]
        self.assertEqual(status, "discovered")

    def test_atomic_claim_lease_recovery_and_retry_limit(self):
        candidate_id = self._discover(
            "https://news.example.com/policy",
            "香港家族办公室税务政策报告",
        )["candidate_id"]
        first = self.candidates.claim_candidates("worker-a", limit=1, lease_seconds=30)
        self.assertEqual([row["id"] for row in first], [candidate_id])
        self.assertEqual(self.candidates.claim_candidates("worker-b", limit=1), [])
        self.db.connection.execute(
            """
            UPDATE intel_candidates
            SET lease_expires_at='2000-01-01T00:00:00Z'
            WHERE id=?
            """,
            (candidate_id,),
        )
        recovered = self.candidates.claim_candidates("worker-b", limit=1)
        self.assertEqual([row["id"] for row in recovered], [candidate_id])
        status = self.candidates.fail_candidate(candidate_id, "temporary")
        self.assertEqual(status, "retry_wait")
        self.db.connection.execute(
            """
            UPDATE intel_candidates
            SET status='dispatching', attempt_count=max_attempts WHERE id=?
            """,
            (candidate_id,),
        )
        self.assertEqual(
            self.candidates.fail_candidate(candidate_id, "permanent"),
            "failed",
        )

    def test_rss_list_and_serpapi_scan_with_shared_budget_and_health(self):
        rss_xml = """<?xml version="1.0"?>
        <rss><channel><item><title>家族办公室发布税务政策</title>
        <link>https://news.example.com/rss-policy?utm_source=feed</link>
        <description><![CDATA[<p>香港家办政策报告</p>]]></description>
        <pubDate>Mon, 27 Jul 2026 08:00:00 +0800</pubDate>
        </item></channel></rss>""".encode("utf-8")
        rss_items = RSSScanner(
            _StaticHTTP(rss_xml, "https://feeds.example.com/rss.xml", "application/rss+xml")
        ).scan({"source_url": "https://feeds.example.com/rss.xml"}, limit=10)
        self.assertEqual(len(rss_items), 1)
        self.assertEqual(rss_items[0]["summary"], "香港家办政策报告")

        html = b"""
        <html><body><article><a href="/news/family-office-policy">
        Family Office Policy Report</a><p>Hong Kong family office policy.</p></article></body></html>
        """
        list_items = ListPageScanner(
            _StaticHTTP(html, "https://media.example.com/news", "text/html")
        ).scan({"source_url": "https://media.example.com/news"}, limit=10)
        self.assertEqual(
            list_items[0]["url"],
            "https://media.example.com/news/family-office-policy",
        )

        source_id = self._insert_source()
        fake_serp = Mock()
        fake_serp.configured = True
        fake_serp.search.return_value = [
            {
                "url": "https://search.example.com/family-office",
                "title": "香港家族办公室监管政策",
                "summary": "家办监管报告",
            }
        ]
        scanner = IntelLightScanner(
            candidate_repository=self.candidates,
            source_registry=self.sources,
            rss_scanner=_FakeScanner(rss_items),
            list_scanner=_FakeScanner(),
            serpapi_client=fake_serp,
            url_validator=lambda _url: _url,
        )
        with patch("intel_light_scanner.config.SERPAPI_ENABLED", True), patch(
            "intel_light_scanner.config.SERPAPI_DAILY_QUERY_BUDGET", 1
        ), patch("intel_light_scanner.config.SERPAPI_MAX_QUERIES_PER_RUN", 5), patch(
            "intel_light_scanner.time.time", return_value=1785456000.0
        ):
            first = scanner.scan(
                industry_pack_id="family_office",
                source_ids=[source_id],
                include_serpapi=True,
                manual=True,
            )
            second = scanner.scan(
                industry_pack_id="family_office",
                source_ids=[],
                include_serpapi=True,
                max_sources=1,
                manual=True,
            )
            forced = scanner.scan(
                industry_pack_id="family_office",
                source_ids=[source_id],
                include_serpapi=False,
                manual=True,
                activation_id="switch-activation",
                force_rescan=True,
            )
        self.assertGreaterEqual(first["queued_count"], 2)
        self.assertTrue(second["rate_limited"])
        self.assertEqual(forced["reused_scan_count"], 0)
        self.assertEqual(forced["source_count"], 1)
        self.assertEqual(fake_serp.search.call_count, 1)
        health = self.db.connection.execute(
            """
            SELECT last_scan_status, consecutive_scan_failures
            FROM intel_sources WHERE id=?
            """,
            (source_id,),
        ).fetchone()
        self.assertEqual(health["last_scan_status"], "completed")
        self.assertEqual(health["consecutive_scan_failures"], 0)

        failed_scanner = IntelLightScanner(
            candidate_repository=self.candidates,
            source_registry=self.sources,
            rss_scanner=_FakeScanner(
                error=RuntimeError("failed ?api_key=must-not-leak")
            ),
            list_scanner=_FakeScanner(),
            serpapi_client=fake_serp,
            url_validator=lambda _url: _url,
        )
        with patch("intel_light_scanner.time.time", return_value=1785542400.0):
            failed_scanner.scan(
                industry_pack_id="family_office",
                source_ids=[source_id],
                include_serpapi=False,
                manual=True,
            )
        failed_health = self.db.connection.execute(
            """
            SELECT last_scan_status, last_scan_error, consecutive_scan_failures
            FROM intel_sources WHERE id=?
            """,
            (source_id,),
        ).fetchone()
        self.assertEqual(failed_health["last_scan_status"], "failed")
        self.assertEqual(failed_health["consecutive_scan_failures"], 1)
        self.assertNotIn("must-not-leak", failed_health["last_scan_error"])

    def test_dispatcher_reuses_existing_article_then_crawls_new_article(self):
        existing_url = "https://news.example.com/existing?utm_source=old"
        article_id = self.db.insert_article(
            {
                "url": existing_url,
                "canonical_url": "https://news.example.com/existing",
                "title": "已存在的香港家族办公室政策",
                "content": "家族办公室政策正文" * 30,
                "matched_keywords": ["家族办公室"],
            }
        )
        existing_candidate = self._discover(
            "https://news.example.com/existing?utm_medium=new",
            "已存在的香港家族办公室政策",
        )["candidate_id"]
        extractor = _FakeExtractor({"success": False, "error": "must not run"})
        adapter = CandidateCrawlerAdapter(
            database=self.db,
            candidate_repository=self.candidates,
            extractor_factory=lambda _db: extractor,
            url_validator=lambda _url: _url,
            redirect_validator=lambda _url: _url,
        )
        dispatcher = IntelCandidateDispatcher(
            repository=self.candidates,
            adapter=adapter,
            worker_id="dispatch-test",
        )
        result = dispatcher.dispatch_once(limit=1, manual=True)
        self.assertEqual(result["existing_article"], 1)
        self.assertEqual(extractor.calls, 0)
        linked = self.db.connection.execute(
            "SELECT article_id, status FROM intel_candidates WHERE id=?",
            (existing_candidate,),
        ).fetchone()
        self.assertEqual(linked["article_id"], article_id)
        self.assertEqual(linked["status"], "crawled")

        new_candidate = self._discover(
            "https://news.example.com/new-policy",
            "香港家族办公室发布新监管政策",
        )["candidate_id"]
        success_extractor = _FakeExtractor(
            {
                "success": True,
                "url": "https://news.example.com/new-policy",
                "title": "香港家族办公室发布新监管政策",
                "content": "这是由现有正文提取器返回的完整正文。" * 40,
                "publish_date": "2026-07-27",
                "extraction_method": "mock-existing-crawler",
                "quality_score": 90,
            }
        )
        dispatcher.adapter = CandidateCrawlerAdapter(
            database=self.db,
            candidate_repository=self.candidates,
            extractor_factory=lambda _db: success_extractor,
            url_validator=lambda _url: _url,
            redirect_validator=lambda _url: _url,
        )
        crawled = dispatcher.dispatch_once(limit=1, manual=True)
        self.assertEqual(crawled["crawled"], 1)
        row = self.db.connection.execute(
            """
            SELECT c.status, c.article_id, c.crawler_task_id, a.url
            FROM intel_candidates c JOIN articles a ON a.id=c.article_id
            WHERE c.id=?
            """,
            (new_candidate,),
        ).fetchone()
        self.assertEqual(row["status"], "crawled")
        self.assertEqual(row["crawler_task_id"], f"intel_candidate_{new_candidate}")
        self.assertEqual(row["url"], "https://news.example.com/new-policy")
        classification_payload = json.loads(
            self.db.connection.execute(
                """
                SELECT payload_json FROM intel_jobs
                WHERE job_type='classification'
                  AND json_extract(payload_json,'$.article_id')=?
                ORDER BY id DESC LIMIT 1
                """,
                (int(row["article_id"]),),
            ).fetchone()[0]
        )
        self.assertTrue(classification_payload["ragflow_upload"])

    def test_ssrf_redirect_secret_safety_and_intel_api(self):
        public_resolver = lambda *_args: [
            (2, 1, 6, "", ("8.8.8.8", 443))
        ]
        private_resolver = lambda *_args: [
            (2, 1, 6, "", ("127.0.0.1", 80))
        ]
        self.assertEqual(
            validate_external_url(
                "https://public.example/path",
                resolver=public_resolver,
            ),
            "https://public.example/path",
        )
        with self.assertRaises(UnsafeExternalURLError):
            validate_external_url(
                "http://internal.example/admin",
                resolver=private_resolver,
            )
        self.assertEqual(
            validate_external_url(
                "http://internal.example/admin",
                resolver=private_resolver,
                allowlist="internal.example",
            ),
            "http://internal.example/admin",
        )
        redirect_session = Mock()
        redirect_session.get.return_value = _RedirectResponse(
            302,
            "https://public.example/start",
            "http://internal.example/admin",
        )

        def redirect_resolver(host, *_args):
            address = "127.0.0.1" if host == "internal.example" else "8.8.8.8"
            return [(2, 1, 6, "", (address, 443))]

        with self.assertRaises(UnsafeExternalURLError):
            SafeHTTPClient(
                session=redirect_session,
                resolver=redirect_resolver,
            ).get("https://public.example/start")
        self.assertEqual(redirect_session.get.call_count, 1)
        secret = "super-secret-key"
        sanitized = sanitize_external_error(
            f"request failed ?api_key={secret}", secrets=(secret,)
        )
        self.assertNotIn(secret, sanitized)

        self._discover(
            "https://news.example.com/api-candidate",
            "香港家族办公室政策报告",
        )
        run_id = self.candidates.create_scan_run(
            source_id=None,
            industry_pack_id="family_office",
            scanner_type="serpapi",
        )
        self.candidates.finish_scan_run(run_id, {"status": "completed"})
        app = Flask(__name__)
        app.register_blueprint(intel_bp)
        with patch.object(
            intel_api, "intel_candidate_repository", self.candidates
        ), patch.object(intel_api, "intel_repository", self.jobs), patch(
            "decorators.user_db.verify_session",
            return_value={"user_id": 3, "role": "admin"},
        ):
            client = app.test_client()
            headers = {"Authorization": "Bearer token"}
            candidates = client.get(
                "/api/intel/candidates?industry_pack_id=family_office"
                "&status=queued&time_range=30d",
                headers=headers,
            )
            self.assertEqual(candidates.status_code, 200)
            self.assertGreaterEqual(candidates.get_json()["total"], 1)
            runs = client.get(
                "/api/intel/scan-runs?industry_pack_id=family_office",
                headers=headers,
            )
            self.assertEqual(runs.status_code, 200)
            queued = client.post(
                "/api/intel/scan/run",
                headers={**headers, "Idempotency-Key": "scan-api-test"},
                json={
                    "industry_pack_id": "family_office",
                    "source_ids": [],
                    "include_serpapi": False,
                    "max_sources": 1,
                    "max_items_per_source": 1,
                },
            )
            duplicate = client.post(
                "/api/intel/scan/run",
                headers={**headers, "Idempotency-Key": "scan-api-test"},
                json={
                    "industry_pack_id": "family_office",
                    "source_ids": [],
                    "include_serpapi": False,
                },
            )
            self.assertEqual(queued.status_code, 202)
            self.assertEqual(
                queued.get_json()["job_id"],
                duplicate.get_json()["job_id"],
            )
            with patch("intel_api.config.SERPAPI_ENABLED", True), patch(
                "intel_api.config.SERPAPI_API_KEY", "configured"
            ), patch("intel_api.config.SERPAPI_DAILY_QUERY_BUDGET", 0):
                limited = client.post(
                    "/api/intel/scan/run",
                    headers={**headers, "Idempotency-Key": "limited"},
                    json={
                        "industry_pack_id": "family_office",
                        "include_serpapi": True,
                    },
                )
            self.assertEqual(limited.status_code, 429)

    def test_serpapi_disabled_or_missing_key_never_calls_network(self):
        session = Mock()
        client = SerpAPIClient(api_key="", session=session)
        with patch("serpapi_client.config.SERPAPI_ENABLED", True):
            self.assertEqual(client.search("family office"), [])
        with patch("serpapi_client.config.SERPAPI_ENABLED", False):
            configured = SerpAPIClient(api_key="secret", session=session)
            self.assertEqual(configured.search("family office"), [])
        session.get.assert_not_called()

    def test_worker_periodic_scan_and_dispatch_jobs_are_deduped(self):
        source_id = self._insert_source()
        self.sources.update_source_metadata(
            source_id,
            {"preferred_scan_time": "08:30", "schedule_rule": "daily"},
        )
        worker = IntelWorker(repository=self.jobs, worker_id="periodic-test")
        restarted = IntelWorker(repository=self.jobs, worker_id="periodic-restart")
        now_utc = datetime(2026, 7, 31, 0, 30, tzinfo=timezone.utc)
        with patch("intel_worker.config.INTEL_LIGHT_SCANNER_ENABLED", True), patch(
            "intel_worker.config.INTEL_CANDIDATE_DISPATCH_ENABLED", True
        ), patch("intel_worker.config.INTEL_LIGHT_SCAN_DAILY_TIME", "23:59"), patch(
            "intel_worker.utc_now", return_value=now_utc
        ):
            worker.enqueue_due_periodic_jobs()
            worker.enqueue_due_periodic_jobs()
            restarted.enqueue_due_periodic_jobs()
        rows = self.db.connection.execute(
            """
            SELECT job_type, COUNT(*) AS total FROM intel_jobs
            WHERE job_type IN ('light_scan', 'candidate_dispatch')
            GROUP BY job_type
            """
        ).fetchall()
        counts = {row["job_type"]: row["total"] for row in rows}
        self.assertEqual(counts["light_scan"], 1)
        self.assertEqual(counts["candidate_dispatch"], 1)
        scan_job = self.db.connection.execute(
            "SELECT dedupe_key,payload_json FROM intel_jobs WHERE job_type='light_scan'"
        ).fetchone()
        self.assertEqual(json.loads(scan_job["payload_json"])["source_ids"], [source_id])
        self.assertEqual(
            scan_job["dedupe_key"],
            f"light-scan:source:{source_id}:20260731",
        )


if __name__ == "__main__":
    unittest.main()

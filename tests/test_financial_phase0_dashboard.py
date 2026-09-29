#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch


_BOOTSTRAP_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = os.path.join(_BOOTSTRAP_TEMP_DIR.name, "bootstrap.sqlite3")
os.environ["INTEL_LLM_ENABLED"] = "false"
os.environ["CRAWL_REQUIRE_KEYWORD_MATCH"] = "false"

from flask import Flask

import config
import intel_api
from intel_api import intel_bp
from intel_candidates import IntelCandidateRepository
from intel_classifier import IntelClassificationService
from intel_database import IntelRepository
from sqlite_database import SQLiteDatabase
from tools.check_financial_dashboard import inspect_frontend_contract


class FinancialDashboardTests(unittest.TestCase):
    def setUp(self):
        self.original_keyword_guard = config.CRAWL_REQUIRE_KEYWORD_MATCH
        self.original_llm_enabled = config.INTEL_LLM_ENABLED
        config.CRAWL_REQUIRE_KEYWORD_MATCH = False
        config.INTEL_LLM_ENABLED = False
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "financial-dashboard.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())
        self.db.analyze_article_spacetime_profile = lambda _article_id: None
        self.repository = IntelRepository(self.db)
        self.candidates = IntelCandidateRepository(self.db)
        self.classifier = IntelClassificationService(self.repository)
        self.source_id = self._insert_source()

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
                content_type, market, authority_level, is_enabled, metadata_json
            ) VALUES (?, ?, ?, 'rss', 'official', 'HK', 5, 1, '{}')
            """,
            (
                "https://official.example/financial.xml",
                "https://official.example/financial.xml",
                'Official SFC <img src=x onerror="window.__xss=1">',
            ),
        )
        source_id = int(cursor.lastrowid)
        self.db.connection.execute(
            "INSERT INTO intel_source_industries (source_id, industry_pack_id) VALUES (?, 'financial_markets')",
            (source_id,),
        )
        self.db.connection.commit()
        return source_id

    def _financial_article(self, *, url: str, title: str, content: str, publish_date=None) -> int:
        run_id = self.candidates.create_scan_run(
            source_id=self.source_id,
            industry_pack_id="financial_markets",
            scanner_type="rss",
        )
        candidate = self.candidates.discover(
            {
                "url": url,
                "title": title,
                "summary": "SFC capital market securities trading regulatory framework",
                "published_at": publish_date,
            },
            industry_pack_id="financial_markets",
            source_id=self.source_id,
            scan_run_id=run_id,
            observation_type="rss",
        )
        self.candidates.finish_scan_run(
            run_id,
            {"status": "completed", "discovered_count": 1, "queued_count": 1},
        )
        article_id = self.db.insert_article(
            {
                "url": url,
                "title": title,
                "content": content,
                "publish_date": publish_date,
                "matched_keywords": ["SFC", "capital market", "<svg onload=window.__xss=2>"],
            }
        )
        self.assertTrue(article_id)
        self.db.connection.execute(
            "UPDATE intel_candidates SET status='crawled', article_id=? WHERE id=?",
            (int(article_id), int(candidate["candidate_id"])),
        )
        if publish_date is None:
            self.db.connection.execute(
                "UPDATE articles SET publish_date=NULL WHERE id=?",
                (int(article_id),),
            )
        self.db.connection.commit()
        self.classifier.classify_article_id(int(article_id), "financial_markets")
        return int(article_id)

    def _authenticated_client(self):
        app = Flask(__name__)
        app.config.update(TESTING=True)
        app.register_blueprint(intel_bp)
        return app.test_client()

    def test_financial_dashboard_auth_sections_boundaries_and_family_coexistence(self):
        malicious_title = (
            'SFC <script>window.__xss=3</script> announces capital market policy & "review" '
            + "跨市场监管更新" * 35
        )
        first_id = self._financial_article(
            url="https://official.example/news/one",
            title=malicious_title,
            content=(
                "The Securities and Futures Commission announced a capital market "
                "regulatory framework for securities market trading & disclosure. "
            ) * 20,
            publish_date=datetime.now().date().isoformat(),
        )
        missing_date_id = self._financial_article(
            url="https://official.example/news/two",
            title="香港证监会 SFC 发布证券市场 trading 公告",
            content=("香港证监会发布资本市场监管公告，涉及证券交易与上市公司信息披露。") * 30,
            publish_date=None,
        )
        third_id = self._financial_article(
            url="https://official.example/news/three",
            title="SFC market trading announcement 多语言资讯",
            content=("SFC announced securities market trading rules. 香港资本市场监管更新。") * 30,
            publish_date=datetime.now().date().isoformat(),
        )
        duplicate_id = self.db.insert_article(
            {
                "url": "https://official.example/news/three",
                "title": "SFC market trading announcement 多语言资讯",
                "content": ("SFC announced securities market trading rules. 香港资本市场监管更新。") * 30,
                "publish_date": datetime.now().date().isoformat(),
                "matched_keywords": ["SFC"],
            }
        )
        self.assertEqual(int(duplicate_id), third_id)

        family_id = self.db.insert_article(
            {
                "url": "https://family.example/policy",
                "title": "香港家族办公室税务政策更新",
                "content": "香港家族办公室税务宽免与监管政策更新。" * 30,
                "publish_date": datetime.now().date().isoformat(),
                "matched_keywords": ["家族办公室", "税务政策"],
            }
        )
        self.classifier.classify_article_id(int(family_id), "family_office")

        client = self._authenticated_client()
        unauthenticated = client.get(
            "/api/intel/dashboard?industry_pack_id=financial_markets&time_range=730d"
        )
        self.assertEqual(unauthenticated.status_code, 401)
        headers = {"Authorization": "Bearer test-session"}
        with patch.object(intel_api, "intel_repository", self.repository), patch(
            "decorators.user_db.verify_session",
            return_value={"user_id": 1, "role": "admin"},
        ):
            response = client.get(
                "/api/intel/dashboard",
                query_string={
                    "industry_pack_id": "financial_markets",
                    "time_range": "730d",
                    "per_category": 40,
                },
                headers=headers,
            )
            page_one = client.get(
                "/api/intel/articles?industry_pack_id=financial_markets&time_range=730d&page=1&per_page=1",
                headers=headers,
            )
            page_two = client.get(
                "/api/intel/articles?industry_pack_id=financial_markets&time_range=730d&page=2&per_page=1",
                headers=headers,
            )
            family = client.get(
                "/api/intel/dashboard?industry_pack_id=family_office&time_range=730d",
                headers=headers,
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["industry_pack"]["id"], "financial_markets")
        articles = {
            int(article["article_id"]): article
            for section in payload["sections"].values()
            for article in section["articles"]
        }
        self.assertTrue(
            {first_id, missing_date_id, third_id}.issubset(articles),
            {"article_ids": sorted(articles), "payload": payload},
        )
        self.assertIn(articles[first_id]["category"], {"today", "trend", "other"})
        self.assertIn("<script>", articles[first_id]["title"])
        self.assertIn("<img", articles[first_id]["source_display_name"])
        self.assertTrue(articles[first_id]["publish_date"])
        self.assertIsNone(articles[missing_date_id]["publish_date"])
        self.assertTrue(articles[missing_date_id]["effective_time"])
        self.assertGreater(len(articles[first_id]["title"]), 200)

        self.assertEqual(page_one.status_code, 200)
        self.assertEqual(page_two.status_code, 200)
        first_page = page_one.get_json()
        second_page = page_two.get_json()
        self.assertEqual(first_page["per_page"], 1)
        self.assertGreaterEqual(first_page["total_pages"], 3)
        self.assertNotEqual(
            first_page["articles"][0]["article_id"],
            second_page["articles"][0]["article_id"],
        )
        self.assertEqual(family.status_code, 200)
        self.assertGreaterEqual(family.get_json()["total"], 1)

    def test_frontend_renderer_escapes_every_external_card_field(self):
        contract = inspect_frontend_contract()
        self.assertTrue(contract["safe"], contract)
        self.assertEqual(contract["missing_markers"], [])
        self.assertEqual(contract["unsafe_interpolations"], [])


if __name__ == "__main__":
    unittest.main()

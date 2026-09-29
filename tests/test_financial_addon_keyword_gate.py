import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import config
from financial_feed import FinancialFeedService
from industry_packs import IndustryPackLoader
from intel_classifier import IntelClassificationService
from intel_database import IntelRepository
from sqlite_database import SQLiteDatabase


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FinancialAddonKeywordGateTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(
            str(Path(self.temp_dir.name) / "financial-addon.sqlite3")
        )
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.repository = IntelRepository(self.database)
        self.loader = IndustryPackLoader(
            str(PROJECT_ROOT / "config" / "industry_packs"),
            use_published_store=False,
        )
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.article_id = int(
            self.database.connection.execute(
                """
                INSERT INTO articles(
                    url, title, content, domain, publish_date, status,
                    content_hash, content_length
                ) VALUES(
                    'https://finance.example.test/education',
                    '教育上市公司资本市场公告',
                    '教育科技企业发布资本市场公告，涉及学生与校园服务。',
                    'finance.example.test', ?, 'active', 'addon-hash', 32
                )
                """,
                (self.now.date().isoformat(),),
            ).lastrowid
        )
        self.database.connection.commit()

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _set_active(self, pack_id, activation_id, version_id):
        for key, value in (
            ("active_industry_pack_id", pack_id),
            ("active_industry_pack_version_id", version_id),
            ("active_industry_activation_id", activation_id),
        ):
            self.database.connection.execute(
                """
                INSERT INTO intel_runtime_settings(setting_key, setting_value)
                VALUES(?, ?) ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value=excluded.setting_value
                """,
                (key, str(value)),
            )
        self.database.connection.commit()

    def _classify_financial(self, activation_id):
        self.repository.upsert_classification(
            {
                "article_id": self.article_id,
                "industry_pack_id": "financial_markets",
                "activation_id": activation_id,
                "industry_pack_version": "2.0.0",
                "classifier_version": "addon-test",
                "article_content_hash": "addon-hash",
                "rule_category": "event",
                "rule_confidence": 1,
                "rule_reason": "fixture",
                "score_details": {"hits": {"anchor": ["资本市场"]}},
                "matched_keywords": ["资本市场", "上市公司"],
                "final_category": "event",
                "final_confidence": 1,
                "final_reason": "fixture",
            }
        )

    def _build(self, pack_id):
        with patch.object(config, "FINANCIAL_INTELLIGENCE_ENABLED", True), patch.object(
            config, "TRADING_AGENTS_ENABLED", False
        ):
            return FinancialFeedService(
                self.database,
                pack_loader=self.loader,
                clock=lambda: self.now,
            ).build(industry_pack_id=pack_id, time_range="7d")

    def test_same_financial_article_is_visible_only_with_saved_current_pack_match(self):
        self._set_active("education_news", "activation-education", 7)
        self._classify_financial("activation-education")
        saved = self.repository.record_financial_addon_match(
            self.article_id,
            activation_id="activation-education",
            primary_industry_pack_id="education_news",
            matched_keywords=["教育", "教育科技", "学生", "校园"],
        )
        self.assertTrue(saved["is_visible"])
        payload = self._build("education_news")
        self.assertTrue(payload["visible"])
        self.assertEqual(payload["counts"]["source_document"], 1)
        source = next(
            item for item in payload["items"] if item["content_kind"] == "source_document"
        )
        self.assertEqual(
            source["matched_keywords"], ["教育", "教育科技", "学生", "校园"]
        )
        row = self.database.connection.execute(
            """
            SELECT primary_industry_pack_id, matched_keywords_json, is_visible
            FROM financial_addon_article_matches
            WHERE article_id=? AND activation_id='activation-education'
            """,
            (self.article_id,),
        ).fetchone()
        self.assertEqual(row[0], "education_news")
        self.assertEqual(json.loads(row[1]), source["matched_keywords"])
        self.assertEqual(int(row[2]), 1)

        self.repository.record_financial_addon_match(
            self.article_id,
            activation_id="activation-education",
            primary_industry_pack_id="education_news",
            matched_keywords=[],
        )
        self.assertEqual(
            self._build("education_news")["counts"]["source_document"], 0
        )

    def test_financial_classification_automatically_persists_primary_keyword_decision(self):
        self._set_active("education_news", "activation-education", 7)
        service = IntelClassificationService(
            repository=self.repository, pack_loader=self.loader
        )
        with patch.object(config, "INTEL_LLM_ENABLED", False):
            result = service.classify_article_id(
                self.article_id,
                "financial_markets",
                activation_id="activation-education",
            )
        match = result["financial_addon_match"]
        self.assertTrue(match["is_visible"])
        self.assertEqual(match["primary_industry_pack_id"], "education_news")
        self.assertEqual(match["matched_keywords"], ["教育", "教育科技", "学生", "校园"])
        row = self.database.connection.execute(
            """
            SELECT is_visible, matched_keywords_json
            FROM financial_addon_article_matches
            WHERE article_id=? AND activation_id='activation-education'
            """,
            (self.article_id,),
        ).fetchone()
        self.assertEqual(int(row[0]), 1)
        self.assertEqual(json.loads(row[1]), match["matched_keywords"])

    def test_non_family_pack_hides_market_cards_but_keeps_financial_news_channel(self):
        self._set_active("education_news", "activation-education", 7)
        self._classify_financial("activation-education")
        self.repository.record_financial_addon_match(
            self.article_id,
            activation_id="activation-education",
            primary_industry_pack_id="education_news",
            matched_keywords=["教育"],
        )
        education = self._build("education_news")
        self.assertEqual(education["market_overview"], [])
        self.assertEqual(education["counts"]["market_fact"], 0)
        self.assertEqual(
            education["dashboard_card_visibility"],
            {
                "show_financial_news": True,
                "show_market_index_cards": False,
                "show_watched_stock_cards": False,
            },
        )
        self.assertEqual(education["counts"]["source_document"], 1)

        self._set_active("family_office", "activation-family", 8)
        self._classify_financial("activation-family")
        self.repository.record_financial_addon_match(
            self.article_id,
            activation_id="activation-family",
            primary_industry_pack_id="family_office",
            matched_keywords=[],
        )
        family = self._build("family_office")
        self.assertEqual(len(family["market_overview"]), 5)
        self.assertTrue(family["dashboard_card_visibility"]["show_market_index_cards"])
        self.assertTrue(family["dashboard_card_visibility"]["show_watched_stock_cards"])

    def test_all_primary_packs_share_financial_sources_and_frontend_uses_article_cards(self):
        for pack_id in (
            "family_office",
            "ai_news",
            "education_news",
            "healthcare_news",
            "short_video_news",
        ):
            composed = self.loader.compose(pack_id)
            self.assertIn("financial_markets", composed["effective_pack_ids"])
            self.assertTrue(composed["dashboard_capabilities"]["show_financial_news"])
        template = (PROJECT_ROOT / "templates" / "mapindex.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("card.className = 'article-card financial-feed-card'", template)
        self.assertIn("setFinancialHighlightedText(title, titleText, projectMatches)", template)
        self.assertIn("setFinancialHighlightedText(summary, summaryText, projectMatches)", template)
        financial_renderers = template[
            template.index("function renderFinancialFeed(data)"):
            template.index("async function loadIntelDashboard()")
        ]
        self.assertNotIn("innerHTML", financial_renderers)


if __name__ == "__main__":
    unittest.main()

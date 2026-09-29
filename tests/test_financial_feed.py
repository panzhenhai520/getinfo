#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


_BOOTSTRAP_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = os.path.join(_BOOTSTRAP_TEMP_DIR.name, "bootstrap.sqlite3")

from flask import Flask

import config
import intel_api
from financial_feed import FinancialFeedService, _market_movement
from intel_api import intel_bp
from intel_database import IntelRepository
from sqlite_database import SQLiteDatabase
from tools.check_financial_feed import inspect_financial_feed_frontend


UTC = timezone.utc


class FinancialFeedTests(unittest.TestCase):
    def setUp(self):
        self.original_flags = {
            key: getattr(config, key)
            for key in (
                "FINANCIAL_INTELLIGENCE_ENABLED",
                "TRADING_AGENTS_ENABLED",
                "TRADING_SIMULATION_ENABLED",
            )
        }
        config.FINANCIAL_INTELLIGENCE_ENABLED = True
        config.TRADING_AGENTS_ENABLED = True
        config.TRADING_SIMULATION_ENABLED = False
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "feed.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())
        self.repository = IntelRepository(self.db)
        self.now = datetime.now(UTC).replace(microsecond=0)
        self._seed()

    def tearDown(self):
        self.db.disconnect()
        self.temp_dir.cleanup()
        for key, value in self.original_flags.items():
            setattr(config, key, value)

    @staticmethod
    def _utc(value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    def _seed(self):
        connection = self.db.connection
        connection.execute(
            """
            INSERT INTO scheduled_tasks(
                task_name, task_type, target_url, schedule_type, keywords, is_active
            ) VALUES('首页项目关键词','crawl','https://official.example','daily','货币政策',1)
            """
        )
        provider = connection.execute(
            """
            INSERT INTO financial_provider_profiles(
                provider_key, display_name, provider_type, access_tier,
                capabilities_json, is_enabled, health_status
            ) VALUES('akshare_cn','AKShare <img src=x onerror=alert(1)>','market_data',
                     'free','["quote"]',1,'healthy')
            """
        ).lastrowid
        instrument = connection.execute(
            """
            INSERT INTO financial_instruments(
                canonical_symbol, display_name, asset_type, market, exchange, currency
            ) VALUES('000001.SH','上证<script>alert(2)</script>指数','index','CN','XSHG','CNY')
            """
        ).lastrowid
        for number, minutes in enumerate((1, 2, 3), start=1):
            observed = self.now - timedelta(minutes=minutes)
            payload = {
                "last_price": 3000 + number,
                "investment_advice": "<svg onload=alert(3)>立即买入",
            }
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            connection.execute(
                """
                INSERT INTO financial_data_snapshots(
                    snapshot_key, instrument_id, provider_profile_id, data_type,
                    observed_at, fetched_at, market_status, currency, timezone,
                    stale_after, quality_status, payload_json, payload_sha256,
                    source_url
                ) VALUES(?,?,?,'quote',?,?, 'open','CNY','Asia/Shanghai',?,
                         'verified',?,?,?)
                """,
                (
                    f"snapshot-{number}",
                    int(instrument),
                    int(provider),
                    self._utc(observed),
                    self._utc(observed + timedelta(seconds=3)),
                    self._utc(observed + timedelta(minutes=5)),
                    encoded,
                    hashlib.sha256(encoded.encode()).hexdigest(),
                    "javascript:alert(4)" if number == 1 else "https://example.test/quote",
                ),
            )

        watched_equity = connection.execute(
            """
            INSERT INTO financial_instruments(
                canonical_symbol, display_name, asset_type, market, exchange, currency
            ) VALUES('600999.SH','被关注样本股','equity','CN','XSHG','CNY')
            """
        ).lastrowid
        for snapshot_key, data_type, seconds, price in (
            ("watched-bar", "bar", 120, 19.5),
            ("watched-quote", "quote", 30, 20.0),
        ):
            observed = self.now - timedelta(seconds=seconds)
            encoded = json.dumps({"last_price": price}, sort_keys=True)
            connection.execute(
                """
                INSERT INTO financial_data_snapshots(
                    snapshot_key, instrument_id, provider_profile_id, data_type,
                    observed_at, fetched_at, market_status, currency, timezone,
                    quality_status, payload_json, payload_sha256, source_url
                ) VALUES(?,?,?,?,?,?,'open','CNY','Asia/Shanghai','verified',?,?,
                         'https://example.test/watched')
                """,
                (
                    snapshot_key, int(watched_equity), int(provider), data_type,
                    self._utc(observed), self._utc(observed + timedelta(seconds=2)),
                    encoded, hashlib.sha256(encoded.encode()).hexdigest(),
                ),
            )

        hsi_alias = connection.execute(
            """
            INSERT INTO financial_instruments(
                canonical_symbol, display_name, asset_type, market, exchange, currency
            ) VALUES('^HSI','恒生指数 quote','index','HK','XHKG','HKD')
            """
        ).lastrowid
        hsi_payload = json.dumps({"last_price": 26000.0}, sort_keys=True)
        connection.execute(
            """
            INSERT INTO financial_data_snapshots(
                snapshot_key, instrument_id, provider_profile_id, data_type,
                observed_at, fetched_at, market_status, currency, timezone,
                quality_status, payload_json, payload_sha256, source_url
            ) VALUES('hsi-provider-alias',?,?,'quote',?,?,'open','HKD',
                     'Asia/Hong_Kong','verified',?,?, 'https://example.test/hsi')
            """,
            (
                int(hsi_alias), int(provider),
                self._utc(self.now - timedelta(seconds=20)),
                self._utc(self.now - timedelta(seconds=18)),
                hsi_payload, hashlib.sha256(hsi_payload.encode()).hexdigest(),
            ),
        )

        article_id = connection.execute(
            """
            INSERT INTO articles(url,title,content,domain,publish_date,status,content_length)
            VALUES('https://official.example/news','家族办公室<script>alert(5)</script>公告',
                   '家族办公室金融市场原文','official.example',?,'active',12)
            """,
            (self.now.date().isoformat(),),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO article_intel_classifications(
                article_id, industry_pack_id, industry_pack_version,
                classifier_version, article_content_hash, rule_category,
                rule_confidence, rule_reason, score_details_json,
                matched_keywords_json, final_category, final_confidence,
                final_reason, trend_summary, topic_tags_json
            ) VALUES(?, 'financial_markets','1.0.0','test','hash','trend',1,
                     'financial anchor',?, '["金融市场"]','trend',1,
                     'official source','<b>政策摘要</b>','["monetary_policy"]')
            """,
            (int(article_id), json.dumps({"hits": {"anchor": ["金融市场"]}})),
        )

        run_id = "feed-run-1"
        connection.execute(
            """
            INSERT INTO financial_research_runs(
                id, trigger_type, scope_type, instrument_id, status,
                current_stage, requested_at, completed_at
            ) VALUES(?, 'scheduler','instrument',?,'completed','final_report',?,?)
            """,
            (run_id, int(instrument), self._utc(self.now - timedelta(minutes=8)), self._utc(self.now - timedelta(minutes=4))),
        )
        connection.execute(
            """
            INSERT INTO financial_final_reports(
                research_run_id, report_version, report_status, recommendation,
                confidence, title, executive_summary, report_markdown,
                report_json, risk_summary_json, suitability_notice, disclaimer,
                observed_at, fetched_at, verified_at, updated_at
            ) VALUES(?,1,'verified','Hold',0.73,?,?,'# report','{}',?,
                     '研究用途','不构成建议',?,?,?,?)
            """,
            (
                run_id,
                "TradingAgents <iframe>报告</iframe>",
                "多代理研究观点，不是行情事实。<img src=x onerror=alert(6)>",
                json.dumps({"risk_level": "medium", "unsafe_html": "<script>alert(7)</script>"}),
                self._utc(self.now - timedelta(minutes=5)),
                self._utc(self.now - timedelta(minutes=4)),
                self._utc(self.now - timedelta(minutes=3)),
                self._utc(self.now - timedelta(minutes=3)),
            ),
        )

        old_time = self.now - timedelta(days=10)
        payload = '{"last_price":1}'
        connection.execute(
            """
            INSERT INTO financial_data_snapshots(
                snapshot_key, instrument_id, provider_profile_id, data_type,
                observed_at, fetched_at, payload_json, payload_sha256
            ) VALUES('old-snapshot',?,?,'quote',?,?,?,?)
            """,
            (
                int(instrument), int(provider), self._utc(old_time), self._utc(old_time),
                payload, hashlib.sha256(payload.encode()).hexdigest(),
            ),
        )
        connection.commit()

    def _client(self):
        app = Flask(__name__)
        app.config.update(TESTING=True)
        app.register_blueprint(intel_bp)
        return app.test_client()

    def _get(self, query=None):
        return self._get_as(1, query=query)

    def _get_as(self, user_id, query=None):
        client = self._client()
        with patch.object(intel_api, "intel_repository", self.repository), patch(
            "decorators.user_db.verify_session",
            return_value={"user_id": user_id, "role": "admin"},
        ):
            return client.get(
                "/api/intel/financial/feed",
                query_string=query or {"industry_pack_id": "family_office", "time_range": "7d"},
                headers={"Authorization": "Bearer fixture"},
            )

    def _delete_instrument(self, instrument_id, *, user_id=1, authenticated=True):
        client = self._client()
        patches = [patch.object(intel_api, "intel_repository", self.repository)]
        if authenticated:
            patches.append(patch(
                "decorators.user_db.verify_session",
                return_value={"user_id": user_id, "role": "admin"},
            ))
        with patches[0]:
            if authenticated:
                with patches[1]:
                    return client.delete(
                        f"/api/intel/financial/feed/instruments/{instrument_id}",
                        query_string={"industry_pack_id": "family_office"},
                        headers={"Authorization": "Bearer fixture"},
                    )
            return client.delete(
                f"/api/intel/financial/feed/instruments/{instrument_id}",
                query_string={"industry_pack_id": "family_office"},
            )

    def _get_detail(self, item_id="snapshot:1", query=None):
        client = self._client()
        with patch.object(intel_api, "intel_repository", self.repository), patch(
            "decorators.user_db.verify_session",
            return_value={"user_id": 1, "role": "admin"},
        ):
            return client.get(
                f"/api/intel/financial/feed/items/{item_id}/detail",
                query_string=query or {"industry_pack_id": "family_office"},
                headers={"Authorization": "Bearer fixture"},
            )

    def test_three_source_kinds_are_separate_allowlisted_and_time_bounded(self):
        payload = self._get().get_json()
        self.assertTrue(payload["visible"])
        self.assertEqual(payload["effective_pack_ids"], ["family_office", "financial_markets"])
        self.assertEqual(payload["counts"], {
            "market_fact": 1,
            "source_document": 1,
            "research_opinion": 1,
        })
        self.assertEqual(
            [item["overview_key"] for item in payload["market_overview"]],
            ["sse", "szse", "hsi", "nasdaq", "nikkei"],
        )
        self.assertEqual(
            [item["title"] for item in payload["market_overview"]],
            [
                "上证指数（000001.SH）", "深证成指（399001.SZ）",
                "恒生指数（HSI.HK）", "纳斯达克综合指数（IXIC.US）",
                "日经225（N225.JP）",
            ],
        )
        self.assertEqual(payload["market_overview"][0]["snapshot_id"], 1)
        self.assertEqual(
            payload["market_overview"][0]["movement"],
            {
                "status": "ready", "direction": "down", "symbol": "↓",
                "change_percent": -0.0333, "basis": "previous_snapshot",
                "color": "green", "market_convention": "red_up_green_down",
            },
        )
        self.assertEqual(payload["market_overview"][2]["snapshot_id"], 6)
        self.assertEqual(payload["market_overview"][2]["scope"]["symbol"], "HSI.HK")
        self.assertEqual(
            sum(item["item_id"] == "snapshot:5" for item in payload["items"]),
            1,
        )
        self.assertNotIn("snapshot:4", {item["item_id"] for item in payload["items"]})
        self.assertNotIn("snapshot:1", {item["item_id"] for item in payload["items"]})
        self.assertNotIn("snapshot:6", {item["item_id"] for item in payload["items"]})
        self.assertEqual({item["kind_label"] for item in payload["items"]}, {"事实", "原文", "研究观点"})
        by_kind = {item["content_kind"]: item for item in payload["items"]}
        self.assertIn("snapshot_id", by_kind["market_fact"])
        self.assertEqual(by_kind["market_fact"]["title"], "被关注样本股（600999.SH）")
        self.assertEqual(by_kind["market_fact"]["movement"]["status"], "unavailable")
        self.assertNotIn("article_id", by_kind["market_fact"])
        self.assertNotIn("investment_advice", json.dumps(by_kind["market_fact"], ensure_ascii=False))
        self.assertIn("article_id", by_kind["source_document"])
        self.assertEqual(by_kind["source_document"]["matched_keywords"], ["家族办公室"])
        self.assertNotIn("report_id", by_kind["source_document"])
        self.assertIn("report_id", by_kind["research_opinion"])
        self.assertEqual(by_kind["research_opinion"]["report_url"], "/api/financial/reports/1")
        self.assertNotIn("unsafe_html", by_kind["research_opinion"]["risk_summary"])
        self.assertEqual(payload["time_window"]["clock_source"], "application_server")
        self.assertNotIn("old-snapshot", json.dumps(payload))

    def test_auth_pack_feature_and_tradingagents_gates_fail_closed(self):
        client = self._client()
        self.assertEqual(client.get("/api/intel/financial/feed").status_code, 401)
        for key, value in (
            ("active_industry_pack_id", "ai_news"),
            ("active_industry_pack_version_id", "1"),
            ("active_industry_activation_id", "ai-feed-test"),
        ):
            self.db.connection.execute(
                """INSERT INTO intel_runtime_settings(setting_key,setting_value)
                   VALUES(?,?) ON CONFLICT(setting_key) DO UPDATE SET
                   setting_value=excluded.setting_value""",
                (key, value),
            )
        self.db.connection.commit()
        hidden_pack = self._get({"industry_pack_id": "ai_news", "time_range": "7d"}).get_json()
        self.assertTrue(hidden_pack["visible"])
        self.assertEqual(hidden_pack["items"], [])
        self.assertEqual(hidden_pack["market_overview"], [])
        self.assertEqual(
            hidden_pack["dashboard_card_visibility"],
            {
                "show_financial_news": True,
                "show_market_index_cards": False,
                "show_watched_stock_cards": False,
            },
        )
        config.FINANCIAL_INTELLIGENCE_ENABLED = False
        hidden_flag = self._get().get_json()
        self.assertFalse(hidden_flag["visible"])
        config.FINANCIAL_INTELLIGENCE_ENABLED = True
        config.TRADING_AGENTS_ENABLED = False
        no_reports = self._get().get_json()
        self.assertTrue(no_reports["visible"])
        self.assertEqual(no_reports["counts"]["research_opinion"], 0)
        self.assertNotIn("research_opinion", {item["content_kind"] for item in no_reports["items"]})
        self.assertNotIn("tradingagents_report", {item["key"] for item in no_reports["categories"]})

    def test_equity_card_delete_is_persistent_per_user_and_preserves_history(self):
        instrument = self.db.connection.execute(
            "SELECT id FROM financial_instruments WHERE canonical_symbol='600999.SH'"
        ).fetchone()
        index_instrument = self.db.connection.execute(
            "SELECT id FROM financial_instruments WHERE canonical_symbol='000001.SH'"
        ).fetchone()
        instrument_id = int(instrument[0])
        before_snapshots = int(self.db.connection.execute(
            "SELECT COUNT(*) FROM financial_data_snapshots WHERE instrument_id=?",
            (instrument_id,),
        ).fetchone()[0])

        self.assertEqual(
            self._delete_instrument(instrument_id, authenticated=False).status_code,
            401,
        )
        deleted = self._delete_instrument(instrument_id)
        self.assertEqual(deleted.status_code, 200, deleted.get_json())
        self.assertTrue(deleted.get_json()["hidden"])
        self.assertEqual(deleted.get_json()["canonical_symbol"], "600999.SH")
        self.assertEqual(self._delete_instrument(instrument_id).status_code, 200)

        owner_feed = self._get_as(1).get_json()
        self.assertNotIn(
            "600999.SH",
            {item.get("scope", {}).get("symbol") for item in owner_feed["items"]},
        )
        self.assertEqual(owner_feed["counts"]["market_fact"], 0)
        other_feed = self._get_as(2).get_json()
        self.assertIn(
            "600999.SH",
            {item.get("scope", {}).get("symbol") for item in other_feed["items"]},
        )
        self.assertEqual(
            int(self.db.connection.execute(
                "SELECT COUNT(*) FROM financial_data_snapshots WHERE instrument_id=?",
                (instrument_id,),
            ).fetchone()[0]),
            before_snapshots,
        )
        rejected = self._delete_instrument(int(index_instrument[0]))
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("固定指数", rejected.get_json()["error"])

    def test_market_movement_uses_market_specific_color_conventions(self):
        self.assertEqual(_market_movement(1.25, market="CN", basis="reported")["color"], "red")
        self.assertEqual(_market_movement(-1.25, market="XHKG", basis="reported")["color"], "green")
        self.assertEqual(_market_movement(1.25, market="US", basis="reported")["color"], "green")
        self.assertEqual(_market_movement(-1.25, market="XNAS", basis="reported")["color"], "red")
        flat = _market_movement(0, market="US", basis="reported")
        self.assertEqual((flat["direction"], flat["symbol"], flat["color"]), ("flat", "→", "neutral"))
        unavailable = _market_movement(None, market="HK", basis="")
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertEqual(unavailable["symbol"], "—")

    def test_financial_articles_use_active_pack_keywords_and_yield_to_main_categories(self):
        article_id = int(self.db.connection.execute(
            "SELECT id FROM articles WHERE url='https://official.example/news'"
        ).fetchone()[0])
        original = self._get().get_json()
        self.assertEqual(original["counts"]["source_document"], 1)
        source = next(item for item in original["items"] if item["content_kind"] == "source_document")
        self.assertEqual(source["matched_keywords"], ["家族办公室"])

        self.db.connection.execute(
            "UPDATE scheduled_tasks SET keywords='完全不匹配' WHERE task_name='首页项目关键词'"
        )
        self.db.connection.commit()
        # Legacy schedule text is no longer a project-wide admission source.
        self.assertEqual(self._get().get_json()["counts"]["source_document"], 1)

        self.db.connection.execute(
            "UPDATE scheduled_tasks SET keywords='货币政策' WHERE task_name='首页项目关键词'"
        )
        self.db.connection.execute(
            """
            INSERT INTO article_intel_classifications(
                article_id, industry_pack_id, industry_pack_version,
                classifier_version, article_content_hash, rule_category,
                rule_confidence, rule_reason, score_details_json,
                matched_keywords_json, final_category, final_confidence,
                final_reason, trend_summary, topic_tags_json
            ) VALUES(?, 'family_office','1.0.0','test-main','hash-main','event',1,
                     'event analysis',?, '["家族办公室"]','event',1,
                     'main category wins','行业事件摘要','[]')
            """,
            (article_id, json.dumps({"hits": {"anchor": ["家族办公室"]}})),
        )
        self.db.connection.commit()
        self.assertEqual(self._get().get_json()["counts"]["source_document"], 0)

        self.db.connection.execute(
            """
            UPDATE article_intel_classifications
            SET final_category='other'
            WHERE article_id=? AND industry_pack_id='family_office'
            """,
            (article_id,),
        )
        self.db.connection.commit()
        fallback = self._get().get_json()
        self.assertEqual(fallback["counts"]["source_document"], 1)
        fallback_source = next(
            item for item in fallback["items"] if item["content_kind"] == "source_document"
        )
        self.assertEqual(fallback_source["matched_keywords"], ["家族办公室"])

    def test_financial_card_translation_is_authenticated_bounded_and_text_only(self):
        client = self._client()
        endpoint = "/api/intel/financial/feed/translate"
        payload = {
            "item_id": "snapshot:1",
            "title": "上证指数最新快照",
            "summary": "结构化行情快照；价格字段保持原样。",
        }
        self.assertEqual(client.post(endpoint, json=payload).status_code, 401)
        runtime = {
            "base_url": "http://llm.invalid/v1",
            "api_key": "fixture",
            "model_id": "fixture-model",
            "type": "openai",
        }
        with patch(
            "decorators.user_db.verify_session",
            return_value={"user_id": 1, "role": "admin"},
        ), patch.object(
            intel_api, "intel_repository", self.repository
        ), patch.object(
            intel_api.intel_llm_client, "_local_runtime", return_value=runtime
        ), patch.object(
            intel_api,
            "_translate_local_text",
            return_value="SSE Index Snapshot\n\nStructured market snapshot; numeric fields are unchanged.",
        ) as translate:
            response = client.post(
                endpoint,
                json={**payload, "industry_pack_id": "family_office"},
                headers={"Authorization": "Bearer fixture"},
            )
        self.assertEqual(response.status_code, 200, response.get_json())
        result = response.get_json()
        self.assertEqual(result["item_id"], "snapshot:1")
        self.assertEqual(result["target_language"], "英文")
        self.assertEqual(result["translation"].count("\n\n"), 1)
        translated_source = translate.call_args.args[1]
        self.assertIn("上证指数最新快照", translated_source)
        self.assertNotIn("3001", translated_source)

        with patch(
            "decorators.user_db.verify_session",
            return_value={"user_id": 1, "role": "admin"},
        ):
            invalid = client.post(
                endpoint,
                json={**payload, "item_id": "../../arbitrary"},
                headers={"Authorization": "Bearer fixture"},
            )
            oversized = client.post(
                endpoint,
                json={**payload, "summary": "x" * 1801},
                headers={"Authorization": "Bearer fixture"},
            )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(oversized.status_code, 400)

        with patch(
            "decorators.user_db.verify_session",
            return_value={"user_id": 1, "role": "admin"},
        ), patch.object(
            intel_api, "intel_repository", self.repository
        ), patch.object(
            intel_api.intel_llm_client, "_local_runtime", return_value=runtime
        ), patch.object(
            intel_api, "_translate_local_text", return_value="US Market\n\nNo qualified snapshot."
        ):
            overview_translation = client.post(
                endpoint,
                json={
                    "item_id": "overview:nasdaq",
                    "title": "纳斯达克综合指数",
                    "summary": "暂无合格指数快照。",
                },
                headers={"Authorization": "Bearer fixture"},
            )
        self.assertEqual(overview_translation.status_code, 200)

    def test_financial_cards_reuse_article_visual_and_translation_contract(self):
        frontend = inspect_financial_feed_frontend()
        self.assertTrue(frontend["safe"], frontend)
        template = Path(os.path.dirname(os.path.dirname(__file__)), "templates", "mapindex.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("className = 'article-card financial-feed-card'", template)
        self.assertIn("className = 'article-card-action translate financial-feed-card-action'", template)
        self.assertIn('class="intel-grid financial-feed-grid"', template)
        self.assertNotIn('.financial-feed-grid {', template)
        self.assertNotIn('.financial-feed-card-meta,', template)
        self.assertNotIn('.financial-feed-card-footer {', template)

    def test_market_fact_detail_returns_verified_same_series_and_related_news(self):
        connection = self.db.connection
        instrument_id = connection.execute(
            "SELECT id FROM financial_instruments WHERE canonical_symbol='000001.SH'"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO financial_instrument_aliases(
                instrument_id,alias,alias_normalized,market,alias_type,is_official
            ) VALUES(?, '上证指数', '上证指数', 'CN', 'official_short_name', 1)
            """,
            (instrument_id,),
        )
        related_article_id = connection.execute(
            """
            INSERT INTO articles(url,title,content,domain,publish_date,status,content_length)
            VALUES('https://official.example/sse-news','上证指数收盘动态',
                   '上证指数今日市场信息','official.example',?,'active',12)
            """,
            (self.now.date().isoformat(),),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO article_intel_classifications(
                article_id, industry_pack_id, industry_pack_version,
                classifier_version, article_content_hash, rule_category,
                rule_confidence, rule_reason, score_details_json,
                matched_keywords_json, final_category, final_confidence,
                final_reason, trend_summary, topic_tags_json
            ) VALUES(?, 'financial_markets','1.0.0','test','related-hash','trend',1,
                     'financial anchor','{}', '["上证指数"]','trend',1,
                     'official source','指数相关新闻','["market_index"]')
            """,
            (related_article_id,),
        )
        provider_id = connection.execute(
            "SELECT id FROM financial_provider_profiles WHERE provider_key='akshare_cn'"
        ).fetchone()[0]
        future_payload = '{"last_price":9999}'
        connection.execute(
            """
            INSERT INTO financial_data_snapshots(
                snapshot_key,instrument_id,provider_profile_id,data_type,
                observed_at,fetched_at,market_status,currency,quality_status,
                payload_json,payload_sha256
            ) VALUES('future-feed-detail',?,?,'quote',?,?,'closed','CNY','verified',?,?)
            """,
            (
                instrument_id,
                provider_id,
                self._utc(self.now + timedelta(days=1)),
                self._utc(self.now + timedelta(days=1, seconds=2)),
                future_payload,
                hashlib.sha256(future_payload.encode()).hexdigest(),
            ),
        )
        connection.execute(
            "UPDATE financial_data_snapshots SET payload_sha256='bad-hash' WHERE snapshot_key='snapshot-2'"
        )
        connection.commit()

        response = self._get_detail("snapshot:1")
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["item_id"], "snapshot:1")
        self.assertEqual(payload["scope"]["symbol"], "000001.SH")
        history = payload["history"]
        self.assertEqual(history["status"], "ready")
        self.assertEqual(history["value_field"], "last_price")
        self.assertEqual(history["currency"], "CNY")
        self.assertEqual([point["snapshot_id"] for point in history["points"]], [3, 1])
        self.assertEqual(
            [point["observed_at"] for point in history["points"]],
            sorted(point["observed_at"] for point in history["points"]),
        )
        self.assertNotIn("9999", json.dumps(history))
        news = payload["related_news"]
        self.assertEqual(news[0]["article_id"], related_article_id)
        self.assertEqual(news[0]["detail_mode"], "article_modal")
        self.assertIn("上证指数", news[0]["matched_terms"])
        policy = payload["news_status"]["relatedness_policy"]
        self.assertFalse(policy["project_keyword_gate_applied"])
        self.assertEqual(policy["target_type"], "instrument")
        self.assertEqual(
            policy["recency_strategy"],
            "configured_recent_window_then_latest_available",
        )

    def test_overview_detail_empty_state_invalid_id_and_authentication(self):
        client = self._client()
        self.assertEqual(
            client.get("/api/intel/financial/feed/items/overview:hk/detail").status_code,
            401,
        )
        overview = self._get_detail("overview:hsi").get_json()
        self.assertEqual(overview["item_id"], "overview:hsi")
        self.assertEqual(overview["title"], "恒生指数（HSI.HK）")
        self.assertEqual(overview["scope"]["symbol"], "HSI.HK")
        self.assertEqual(overview["history"]["status"], "ready")
        empty = self._get_detail("overview:nasdaq").get_json()
        self.assertEqual(empty["history"]["status"], "unavailable")
        self.assertEqual(empty["history"]["reason"], "no_qualified_history")
        self.assertFalse(
            empty["news_status"]["relatedness_policy"]["project_keyword_gate_applied"]
        )
        self.assertEqual(
            empty["news_status"]["relatedness_policy"]["target_type"],
            "fixed_market_index",
        )
        self.assertEqual(self._get_detail("article:1").status_code, 400)
        self.assertEqual(self._get_detail("snapshot:999999").status_code, 404)

    def test_financial_click_routing_keeps_facts_internal_and_news_in_article_modal(self):
        template = Path(os.path.dirname(os.path.dirname(__file__)), "templates", "mapindex.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("viewFinancialMarketDetail(String(item.item_id || ''))", template)
        self.assertIn("openItem = () => viewArticle(Number(item.article_id))", template)
        self.assertIn('id="financialMarketDetailModal" class="article-modal"', template)
        self.assertIn('class="article-modal-content"', template)
        self.assertNotIn("window.open(sourceHref, '_blank', 'noopener,noreferrer')", template)
        self.assertNotIn("'查看信源'", template)

    def test_pagination_kind_filter_empty_window_and_invalid_kind(self):
        first = self._get({"industry_pack_id": "financial_markets", "time_range": "7d", "page": 1, "per_page": 1}).get_json()
        second = self._get({"industry_pack_id": "financial_markets", "time_range": "7d", "page": 2, "per_page": 1}).get_json()
        self.assertEqual(len(first["items"]), 1)
        self.assertEqual(len(second["items"]), 1)
        self.assertNotEqual(first["items"][0]["item_id"], second["items"][0]["item_id"])
        only_sources = self._get({
            "industry_pack_id": "family_office", "time_range": "7d",
            "content_kind": "source_document",
        }).get_json()
        self.assertEqual([item["content_kind"] for item in only_sources["items"]], ["source_document"])
        empty = FinancialFeedService(
            self.db,
            settings={
                "FINANCIAL_INTELLIGENCE_ENABLED": True,
                "TRADING_AGENTS_ENABLED": True,
                "TRADING_SIMULATION_ENABLED": False,
            },
            clock=lambda: self.now + timedelta(days=30),
        ).build(industry_pack_id="financial_markets", time_range="24h")
        self.assertTrue(empty["visible"])
        self.assertEqual(empty["items"], [])
        self.assertEqual(len(empty["market_overview"]), 5)
        self.assertTrue(empty["availability"]["reason_codes"])
        self.assertTrue(empty["availability"]["message"])
        self.assertEqual(self._get({"industry_pack_id": "financial_markets", "content_kind": "bad"}).status_code, 400)

    def test_simulation_category_only_appears_with_effective_switch(self):
        disabled = self._get().get_json()
        self.assertNotIn("paper_backtest", {item["key"] for item in disabled["categories"]})
        config.TRADING_SIMULATION_ENABLED = True
        enabled = self._get().get_json()
        self.assertTrue(enabled["simulation_enabled"])
        self.assertIn("paper_backtest", {item["key"] for item in enabled["categories"]})

    def test_feed_is_read_only_and_does_not_change_ordinary_dashboard_statistics(self):
        before = self.repository.classification_summary(industry_pack_id="family_office", time_range="730d")
        table_counts_before = {
            table: self.db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("articles", "financial_data_snapshots", "financial_final_reports")
        }
        response = self._get()
        self.assertEqual(response.status_code, 200)
        after = self.repository.classification_summary(industry_pack_id="family_office", time_range="730d")
        table_counts_after = {
            table: self.db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in table_counts_before
        }
        # The summary exposes the request-time window bounds, so two read-only
        # calls can legitimately cross a second boundary.  Compare the stable
        # statistics that this test is intended to protect.
        self.assertEqual(
            {
                key: before[key]
                for key in (
                    "industry_pack_id", "time_range", "timezone", "counts", "total"
                )
            },
            {
                key: after[key]
                for key in (
                    "industry_pack_id", "time_range", "timezone", "counts", "total"
                )
            },
        )
        self.assertEqual(table_counts_before, table_counts_after)

    def test_bounded_query_and_dom_only_frontend_xss_contract(self):
        started = time.perf_counter()
        response = self._get({"industry_pack_id": "family_office", "time_range": "730d", "per_page": 50})
        elapsed = time.perf_counter() - started
        self.assertEqual(response.status_code, 200)
        self.assertLess(elapsed, 1.0)
        contract = inspect_financial_feed_frontend()
        self.assertTrue(contract["safe"], contract)
        self.assertEqual(contract["unsafe_markers"], [])


if __name__ == "__main__":
    unittest.main()

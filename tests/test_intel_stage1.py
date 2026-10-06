#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

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
import intel_classifier
from industry_packs import (
    IndustryPackError,
    IndustryPackLoader,
    normalize_intel_text,
)
from intel_api import intel_bp
from intel_classifier import (
    TOPIC_TAGGING_VERSION,
    IntelClassificationService,
    classify_article,
    fuse_rule_and_llm,
)
from intel_database import IntelRepository
from intel_worker import IntelWorker
from sqlite_database import SQLiteDatabase

# 入库闸门（intel_boilerplate）会判废"去框架后有效正文 < 40 字"的近空内容。
# 本文件的用例验证的是分类/看板/归属行为，不是闸门本身，因此用占位正文补足长度；
# 占位文本刻意不含任何行业关键词、框架特征词与行业强信号词，避免污染命中判定。
GATE_FILLER = "本条正文为单元测试夹具生成的占位内容，仅用于满足入库闸门的正文字数下限。"


def _gate_safe_content(content: str) -> str:
    return content if len(content) >= 40 else content + GATE_FILLER


class IntelStageOneTests(unittest.TestCase):
    def setUp(self):
        self._original_keyword_guard = config.CRAWL_REQUIRE_KEYWORD_MATCH
        config.CRAWL_REQUIRE_KEYWORD_MATCH = False
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "stage1.sqlite3")
        self.db = SQLiteDatabase(self.db_path)
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())
        self.db.analyze_article_spacetime_profile = lambda _article_id: None
        self.repo = IntelRepository(self.db)
        self.loader = IndustryPackLoader()
        self.service = IntelClassificationService(self.repo, self.loader)

    def tearDown(self):
        self.db.disconnect()
        self.temp_dir.cleanup()
        config.CRAWL_REQUIRE_KEYWORD_MATCH = self._original_keyword_guard

    def insert_article(self, title, content, url):
        return self.db.insert_article(
            {
                "url": url,
                "title": title,
                "content": _gate_safe_content(content),
                "publish_date": datetime.now().date().isoformat(),
                "matched_keywords": [],
            }
        )

    def test_six_versioned_packs_load_and_path_escape_fails(self):
        # This contract enumerates installation seeds only. Runtime-created
        # packs such as the automotive package are exercised by admin tests.
        seed_loader = IndustryPackLoader(use_published_store=False)
        metadata = seed_loader.metadata()
        by_id = {item["id"]: item for item in metadata}
        # 出厂种子包快照（与 config/industry_packs/*.json 的 pack_version 一一对应）：
        # 行业包版本升级必须同步更新这里，否则契约失效。
        expected_versions = {
            "ai_news": "2.0.3",
            "automotive_industry": "1.0.0",
            "bolean_security_compute": "1.0.0",
            "education_news": "2.0.3",
            "family_office": "2.2.0",
            "financial_markets": "2.0.0",
            "healthcare_news": "2.0.15",
            "short_video_news": "2.0.0",
        }
        self.assertEqual(set(by_id), set(expected_versions))
        self.assertEqual(
            {pack_id: item["pack_version"] for pack_id, item in by_id.items()},
            expected_versions,
        )
        self.assertTrue(all(item["schema_version"] == 3 for item in by_id.values()))
        with self.assertRaises(IndustryPackError):
            seed_loader.load("../family_office")

    def test_financial_pack_gate_accepts_market_articles_and_rejects_generic_fund(self):
        pack = self.loader.load("financial_markets")
        trend = classify_article(
            {
                "title": "中国证监会发布资本市场监管政策",
                "content": "新政策涉及 A股 市场制度改革与长期资金入市。",
            },
            pack,
        )
        event = classify_article(
            {
                "title": "香港证监会发布证券市场公告",
                "content": "公告涉及港股上市公司信息披露。",
            },
            pack,
        )
        non_financial = classify_article(
            {
                "title": "国家自然科学基金发布人工智能教育研究项目",
                "content": "高校教师可申请科研基金，项目关注课堂教学。",
            },
            pack,
        )
        english = classify_article(
            {
                "title": "SFC enhances the regulatory framework for market trading",
                "content": "The Securities and Futures Commission announced new capital market rules.",
            },
            pack,
        )
        self.assertEqual(trend["final_category"], "trend")
        self.assertEqual(event["final_category"], "event")
        self.assertEqual(non_financial["final_category"], "other")
        # "通用行业过滤器"短路分支固定返回 hits={}（产品自身消费者一律按
        # (score_details.get("hits") or {}).get("anchor") or [] 读取），
        # 这里沿用同一口径，只断言"没有任何锚点命中"，不额外断言字典形状。
        self.assertEqual(
            (non_financial["score_details"].get("hits") or {}).get("anchor") or [], []
        )
        self.assertEqual(english["final_category"], "trend")

    def test_financial_pack_configuration_api_is_read_only(self):
        active_pack_before = self.repo.active_industry_pack_id()
        app = Flask(__name__)
        app.register_blueprint(intel_bp)
        with patch.object(intel_api, "intel_repository", self.repo), patch(
            "decorators.user_db.verify_session",
            return_value={"user_id": 1, "role": "admin"},
        ):
            response = app.test_client().get(
                "/api/intel/industry-packs/financial_markets/configuration",
                headers={"Authorization": "Bearer test-token"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["pack"]["id"], "financial_markets")
        self.assertEqual(payload["pack"]["schema_version"], 3)
        self.assertEqual(self.repo.active_industry_pack_id(), active_pack_before)

    def test_rule_examples_have_deterministic_categories(self):
        pack = self.loader.load("family_office")
        trend = classify_article(
            {"title": "香港家族办公室税务政策变化", "content": "税务宽免新政影响家办落户香港"},
            pack,
        )
        event = classify_article(
            {"title": "家族办公室获得跨境金融奖项", "content": "该家办今日宣布获奖"},
            pack,
        )
        other = classify_article(
            {"title": "普通公司新闻", "content": "文末简单提到家族办公室，没有其他行业信号"},
            pack,
        )
        self.assertEqual(trend["final_category"], "trend")
        self.assertEqual(event["final_category"], "event")
        self.assertEqual(other["final_category"], "other")
        self.assertIn("components", trend["score_details"])

    def test_fixed_topics_are_multi_label_and_do_not_change_content_type(self):
        pack = {
            "id": "automotive",
            "name": "汽车技术",
            "core_keywords": ["汽车技术"],
            "expanded_keywords": ["主动道路噪声控制"],
            "trend_keywords": ["标准"],
            "event_keywords": ["量产"],
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
            "fixed_topics": [
                {
                    "key": "nvh_acoustics",
                    "name": "NVH与声学",
                    "keywords": ["主动道路噪声控制", "声学包"],
                },
                {
                    "key": "active_noise_control",
                    "name": "主动降噪",
                    "keywords": ["主动道路噪声控制", "主动降噪"],
                },
            ],
        }
        # 主题判别力门限：同时出现在半数以上主题里的关键词（这里两个主题都写了
        # "主动道路噪声控制"）不再参与主题归属。多标签归属因此必须由各主题**独有**
        # 的关键词支撑——夹具按该契约给每个主题一个独有关键词。
        result = classify_article(
            {
                "title": "某车型声学包与主动降噪系统正式量产",
                "content": "该汽车技术已进入量产阶段。",
            },
            pack,
        )
        self.assertEqual(result["final_category"], "event")
        self.assertEqual(set(result["topic_tags"]), {"NVH与声学", "主动降噪"})
        self.assertEqual(
            set(result["topic_keys"]), {"nvh_acoustics", "active_noise_control"}
        )
        self.assertEqual(result["topic_tagging_version"], TOPIC_TAGGING_VERSION)
        self.assertEqual(
            len(result["score_details"]["topic_assignments"]), 2
        )

        rejected = classify_article(
            {"title": "普通公司量产新系统", "content": "声学研究取得进展。"},
            pack,
        )
        self.assertEqual(
            (rejected["score_details"].get("hits") or {}).get("anchor") or [], []
        )

    def test_recent_high_relevance_articles_get_light_trend_today_fallback(self):
        pack = {
            "id": "automotive",
            "name": "汽车技术",
            "core_keywords": ["汽车", "智能网联汽车"],
            "expanded_keywords": ["车路协同"],
            "trend_keywords": ["标准"],
            "event_keywords": ["量产"],
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
            "fixed_topics": [],
        }
        now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
        with patch.object(intel_classifier, "datetime") as mock_datetime:
            mock_datetime.now.return_value = now
            mock_datetime.fromisoformat.side_effect = datetime.fromisoformat
            mock_datetime.timezone = timezone

            recent_event = classify_article(
                {
                    "title": "智能网联汽车车路协同平台升级",
                    "content": "该平台完成新一轮系统升级。",
                    "publish_date": (now - timedelta(days=1)).date().isoformat(),
                },
                pack,
            )
            recent_trend = classify_article(
                {
                    "title": "智能网联汽车车路协同平台升级",
                    "content": "该平台完成新一轮系统升级。",
                    "publish_date": (now - timedelta(days=10)).date().isoformat(),
                },
                pack,
            )
            weak_recent = classify_article(
                {
                    "title": "汽车产业观察",
                    "content": "只是一个普通行业标题，没有额外相关信号。",
                    "publish_date": (now - timedelta(days=1)).date().isoformat(),
                },
                pack,
            )

        self.assertEqual(recent_event["final_category"], "event")
        self.assertEqual(recent_trend["final_category"], "trend")
        self.assertEqual(weak_recent["final_category"], "other")
        self.assertEqual(recent_event["score_details"]["temporal_fallback"]["category"], "event")
        self.assertEqual(recent_trend["score_details"]["temporal_fallback"]["category"], "trend")
        self.assertNotIn("temporal_fallback", weak_recent["score_details"])

    def test_recent_fallback_windows_are_pack_configurable(self):
        pack = {
            "id": "automotive",
            "name": "汽车技术",
            "core_keywords": ["汽车", "智能网联汽车"],
            "expanded_keywords": ["车路协同"],
            "trend_keywords": ["标准"],
            "event_keywords": ["量产"],
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
                "recent_today_window_days": 1,
                "recent_trend_window_days": 5,
            },
            "fixed_topics": [],
        }
        now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
        with patch.object(intel_classifier, "datetime") as mock_datetime:
            mock_datetime.now.return_value = now
            mock_datetime.fromisoformat.side_effect = datetime.fromisoformat
            mock_datetime.timezone = timezone

            recent_event = classify_article(
                {
                    "title": "智能网联汽车车路协同平台升级",
                    "content": "该平台完成新一轮系统升级。",
                    "publish_date": (now - timedelta(hours=12)).date().isoformat(),
                },
                pack,
            )
            recent_trend = classify_article(
                {
                    "title": "智能网联汽车车路协同平台升级",
                    "content": "该平台完成新一轮系统升级。",
                    "publish_date": (now - timedelta(days=2)).date().isoformat(),
                },
                pack,
            )
            out_of_window = classify_article(
                {
                    "title": "智能网联汽车车路协同平台升级",
                    "content": "该平台完成新一轮系统升级。",
                    "publish_date": (now - timedelta(days=6)).date().isoformat(),
                },
                pack,
            )

        self.assertEqual(recent_event["final_category"], "event")
        self.assertEqual(recent_event["score_details"]["temporal_fallback"]["window_days"], 1)
        self.assertEqual(recent_trend["final_category"], "trend")
        self.assertEqual(recent_trend["score_details"]["temporal_fallback"]["window_days"], 5)
        self.assertEqual(out_of_window["final_category"], "other")
        self.assertNotIn("temporal_fallback", out_of_window["score_details"])

    def test_llm_topics_are_allowlisted_and_cannot_bypass_industry_gate(self):
        pack = self.loader.load("family_office")
        admitted = classify_article(
            {
                "title": "家族办公室税务政策变化",
                "content": "家族办公室关注税务宽免。",
            },
            pack,
        )
        fused = fuse_rule_and_llm(
            admitted,
            {
                "category": "trend",
                "confidence": 0.9,
                "reason": "政策变化",
                "why_important": "影响行业",
                "trend_summary": "税务政策调整",
                "topic_tags": ["policy_tax", "模型自行发明的标签"],
            },
            pack,
        )
        self.assertIn("政策与税务", fused["topic_tags"])
        self.assertNotIn("模型自行发明的标签", fused["topic_tags"])

        rejected = classify_article(
            {"title": "普通公司新闻", "content": "没有行业锚点。"}, pack
        )
        rejected_fused = fuse_rule_and_llm(
            rejected,
            {
                "category": "trend",
                "confidence": 0.9,
                "reason": "模型判断",
                "why_important": "无",
                "trend_summary": "无",
                "topic_tags": ["政策与税务"],
            },
            pack,
        )
        self.assertEqual(rejected_fused["topic_tags"], [])

    def test_rule_normalization_negative_weight_and_tie_break(self):
        pack = self.loader.load("family_office")
        self.assertEqual(
            normalize_intel_text("  FAMILY   OFFICE 家族辦公室  "),
            "family office 家族办公室",
        )
        traditional = classify_article(
            {
                "title": "家族辦公室稅務寬免政策",
                "content": "香港家辦監管新政",
            },
            pack,
        )
        tied = classify_article(
            {
                "title": "FAMILY OFFICE 政策宣布",
                "content": "家族办公室政策宣布",
            },
            pack,
        )
        negative = classify_article(
            {
                "title": "Family Office home office furniture 政策",
                "content": "家庭办公家具介绍",
            },
            pack,
        )
        self.assertEqual(traditional["final_category"], "trend")
        self.assertEqual(tied["score_details"]["trend_score"], tied["score_details"]["event_score"])
        self.assertEqual(tied["final_category"], "trend")
        self.assertEqual(negative["final_category"], "other")
        self.assertLess(
            negative["score_details"]["relevance_score"],
            negative["score_details"]["minimum_relevance_score"],
        )

    def test_classification_is_idempotent_and_content_hash_refreshes(self):
        url = "https://example.com/intel/hash-refresh"
        article_id = self.insert_article(
            "香港家族办公室政策",
            "家族办公室税务政策。",
            url,
        )
        first = self.service.classify_article_id(article_id, "family_office")
        duplicate = self.service.classify_article_id(article_id, "family_office")
        self.assertEqual(first["classification_id"], duplicate["classification_id"])

        self.db.update_article(
            article_id,
            {
                "url": url,
                "title": "香港家族办公室政策更新",
                "content": "家族办公室税务政策发生重大变化。",
                "matched_keywords": ["家族办公室"],
            },
        )
        refreshed = self.service.classify_article_id(article_id, "family_office")
        self.assertEqual(first["classification_id"], refreshed["classification_id"])
        self.assertNotEqual(
            first["article_content_hash"],
            refreshed["article_content_hash"],
        )
        count = self.db.connection.execute(
            """
            SELECT COUNT(*) FROM article_intel_classifications
            WHERE article_id=? AND industry_pack_id='family_office'
            """,
            (article_id,),
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_insert_enqueues_worker_and_multi_industry_results_coexist(self):
        article_id = self.insert_article(
            "OpenAI 与香港家族办公室发布 AI 财富管理合作",
            "家族办公室宣布采用人工智能和大模型进行跨境财富管理。",
            "https://example.com/intel/multi-pack",
        )
        self.assertTrue(article_id)
        worker = IntelWorker(repository=self.repo, classification_service=self.service, worker_id="test")
        stats = worker.run_once(job_types=["classification"], limit=10)
        self.assertEqual(stats["completed"], 1)

        second = self.service.classify_article_id(article_id, "ai_news")
        self.assertEqual(second["industry_pack_id"], "ai_news")
        cursor = self.db.connection.cursor()
        cursor.execute(
            "SELECT industry_pack_id FROM article_intel_classifications WHERE article_id=? ORDER BY industry_pack_id",
            (article_id,),
        )
        self.assertEqual(
            [row["industry_pack_id"] for row in cursor.fetchall()],
            ["ai_news", "family_office"],
        )
        cursor.close()

    def test_job_dedupe_and_expired_lease_recovery(self):
        job_id, created = self.repo.enqueue_job("classification", "lease-test", {"article_id": 99})
        duplicate_id, duplicate_created = self.repo.enqueue_job(
            "classification", "lease-test", {"article_id": 99}
        )
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(job_id, duplicate_id)

        first = self.repo.claim_jobs("worker-a", job_types=["classification"], limit=1, lease_seconds=30)
        self.assertEqual([item["id"] for item in first], [job_id])
        self.assertEqual(
            self.repo.claim_jobs("worker-b", job_types=["classification"], limit=1),
            [],
        )
        self.db.connection.execute(
            "UPDATE intel_jobs SET lease_expires_at='2000-01-01T00:00:00Z' WHERE id=?",
            (job_id,),
        )
        recovered = self.repo.claim_jobs("worker-b", job_types=["classification"], limit=1)
        self.assertEqual([item["id"] for item in recovered], [job_id])

    def test_enqueue_failure_never_rolls_back_article(self):
        with patch(
            "intel_database.IntelRepository.enqueue_classification",
            side_effect=RuntimeError("queue unavailable"),
        ):
            article_id = self.insert_article(
                "家族办公室测试文章",
                "家族办公室发布测试事件。",
                "https://example.com/intel/nonblocking",
            )
        self.assertTrue(article_id)
        self.assertEqual(self.db.get_article_id_by_url("https://example.com/intel/nonblocking"), article_id)

    def test_recent_category_view_paginates_dashboard_follow_and_ai_stream(self):
        followed = [
            {"article_id": 3, "title": "手动关注三", "content_preview": "汽车"},
            {"article_id": 2, "title": "手动关注二", "content_preview": "汽车"},
        ]
        saved_ai = [
            {"article_id": 2, "title": "重复关注", "content_preview": "汽车"},
            {"article_id": 1, "title": "AI问答一", "content_preview": "智能驾驶"},
        ]
        with patch.object(
            self.repo, "dashboard_followed_articles", return_value=followed
        ), patch.object(
            self.repo, "list_ai_recent_articles", return_value=(saved_ai, 2)
        ):
            first, total = self.repo.list_dashboard_recent_articles(
                page=1, per_page=2, project_keywords=["汽车"]
            )
            second, _ = self.repo.list_dashboard_recent_articles(
                page=2, per_page=2, project_keywords=["汽车"]
            )
            searched, searched_total = self.repo.list_dashboard_recent_articles(
                page=1,
                per_page=20,
                project_keywords=["汽车"],
                search="智能驾驶",
            )
        self.assertEqual(total, 3)
        self.assertEqual([item["article_id"] for item in first], [3, 2])
        self.assertEqual([item["article_id"] for item in second], [1])
        self.assertEqual(searched_total, 1)
        self.assertEqual([item["article_id"] for item in searched], [1])

    def test_authenticated_api_contract_and_dashboard_home(self):
        article_id = self.insert_article(
            "香港家族办公室监管政策报告",
            "家族办公室监管政策与税务宽免报告。",
            "https://example.com/intel/api",
        )
        other_article_id = self.insert_article(
            "香港家族办公室普通观察",
            "这是一条尚未触发分类的普通行业资讯。",
            "https://example.com/intel/api-other",
        )
        self.service.classify_article_id(article_id, "family_office")

        app = Flask(__name__)
        app.register_blueprint(intel_bp)
        with patch.object(intel_api, "intel_repository", self.repo), patch(
            "decorators.user_db.verify_session",
            return_value={"user_id": 1, "role": "admin"},
        ):
            with patch.object(
                self.repo,
                "list_dashboard_other_articles",
                wraps=self.repo.list_dashboard_other_articles,
            ) as other_articles_method:
                client = app.test_client()
                headers = {"Authorization": "Bearer test-token"}
                packs = client.get("/api/intel/industry-packs", headers=headers)
                self.assertEqual(packs.status_code, 200)
                articles = client.get(
                    "/api/intel/articles?industry_pack_id=family_office&category=trend&time_range=7d",
                    headers=headers,
                )
                self.assertEqual(articles.status_code, 200)
                payload = articles.get_json()
                self.assertTrue(payload["success"])
                self.assertGreaterEqual(payload["total"], 1)
                other_articles = client.get(
                    "/api/intel/articles?industry_pack_id=family_office&category=other&time_range=7d&policy_only=true",
                    headers=headers,
                )
                self.assertEqual(other_articles.status_code, 200)
                other_payload = other_articles.get_json()
                self.assertTrue(other_payload["success"])
                self.assertTrue(other_articles_method.called)
                self.assertTrue(other_articles_method.call_args.kwargs["policy_only"])
                summary = client.get(
                    "/api/intel/summary?industry_pack_id=family_office&time_range=7d",
                    headers=headers,
                )
                self.assertEqual(summary.status_code, 200)
                self.assertGreaterEqual(summary.get_json()["counts"]["trend"], 1)
                dashboard = client.get(
                    "/api/intel/dashboard?industry_pack_id=family_office&time_range=30d",
                    headers=headers,
                )
                self.assertEqual(dashboard.status_code, 200)
                dashboard_payload = dashboard.get_json()
                self.assertTrue(dashboard_payload["success"])
                self.assertEqual(dashboard_payload["industry_pack"]["name"], "家族办公室")
                self.assertIn("trend", dashboard_payload["sections"])
                self.assertGreaterEqual(dashboard_payload["sections"]["trend"]["total"], 1)
                self.assertIn("today_crawled_articles", dashboard_payload["statistics"])
                self.assertIn(
                    "today_news_parsed_articles", dashboard_payload["statistics"]
                )
                queued = client.post(
                    f"/api/intel/reclassify/{article_id}",
                    headers={**headers, "Idempotency-Key": "test-key"},
                    json={"industry_pack_id": "family_office"},
                )
                self.assertEqual(queued.status_code, 202)
                self.assertIn("job_id", queued.get_json())

        with open(
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates", "mapindex.html"),
            "r",
            encoding="utf-8",
        ) as template_file:
            template = template_file.read()
        # 首页视图已固定为资讯流 Dashboard：时空地图首页下线后 body 上不再有 map 视图
        # （对应 data-show-spatiotemporal-map="false"），模板里也只保留 dashboard 标记。
        self.assertIn('data-home-view="dashboard"', template)
        self.assertIn('data-show-spatiotemporal-map="false"', template)
        self.assertNotIn('data-home-view="map"', template)
        self.assertIn('id="intelDashboard"', template)
        self.assertIn("function setHomeView", template)
        self.assertIn('data-industry-pack-id="{{ active_industry_pack.id }}"', template)
        # 地图标签由 Jinja 拼接改为前端模板串（时空地图首页下线后仍保留该视图标签）
        self.assertIn("${state.intelIndustryName}时空信息图", template)
        self.assertIn(
            "intelIndustryPackId: document.body.dataset.industryPackId",
            template,
        )
        # 首页统计维度不再是模板里的静态 id，而是 STAT_DIMENSION_MAP 注册 + _setStat 写入；
        # 契约改为断言三个维度名与维度键的映射（严格字面量）。
        self.assertIn("intelIngestedArticles: 'ingested'", template)
        self.assertIn("intelValidArticles730d: 'valid_730d'", template)
        self.assertIn("intelHomepageArticles: 'classified'", template)
        self.assertIn("正在按最新资讯自动分区；无内容的分类会自动隐藏…", template)
        self.assertIn("_setStat('intelTodayLabel', '当日有效文章')", template)
        self.assertIn("_setStat('intelWindowLabel', '当日已分类资讯')", template)
        # 资讯流分区已改为数据驱动渲染，分区文案不再是模板静态文本；
        # 契约改为断言分区键集合与各分区容器 id 映射。
        self.assertIn(
            "['today', 'trend', 'policy', 'recent', 'other'].forEach(key => renderIntelSection(key, [], 0));",
            template,
        )
        self.assertIn("trend: ['intelTrendCards', 'intelTrendCount'],", template)
        self.assertIn("other: ['intelOtherCards', 'intelOtherCount'],", template)
        self.assertIn("section.hidden = true", template)
        self.assertIn("const INTEL_DASHBOARD_TIME_RANGE = '7d'", template)
        self.assertIn("const INTEL_DASHBOARD_PER_CATEGORY = 10", template)
        self.assertIn("time_range: INTEL_DASHBOARD_TIME_RANGE", template)
        self.assertIn("per_category: String(INTEL_DASHBOARD_PER_CATEGORY)", template)
        self.assertIn("<span>分类：${categoryLabel}</span>", template)
        self.assertIn("count > articles.length", template)
        self.assertIn(">查看更多</button>", template)
        category_template = Path(
            os.path.dirname(os.path.dirname(__file__)), "templates", "intel_category.html"
        ).read_text(encoding="utf-8")
        self.assertIn("time_range:'730d'", category_template)
        self.assertGreaterEqual(template.count("cache: 'no-store'"), 2)
        self.assertNotIn('id="intelIndustryPack"', template)
        self.assertNotIn('id="intelTimeRange"', template)
        self.assertNotIn('id="intelTotalArticles"', template)
        self.assertNotIn('id="intelTodayNew"', template)
        self.assertNotIn('id="intelTotalDomains"', template)
        self.assertIn("state.intelRequestController.abort()", template)
        self.assertIn("highlightKeywords(escapeHtml(rawTitle)", template)

    def test_dashboard_other_includes_only_pack_relevant_residual_articles(self):
        classified_other_id = self.insert_article(
            "家族办公室补充观察",
            "家族办公室的一般行业观察，没有明确事件或趋势信号。",
            "https://example.com/intel/relevant-other",
        )
        classified_article = self.repo.get_article(classified_other_id)
        self.repo.upsert_classification({
            "article_id": classified_other_id,
            "industry_pack_id": "family_office",
            "industry_pack_version": "1.0.0",
            "classifier_version": "other-test",
            "article_content_hash": self.repo.article_content_hash(classified_article),
            "rule_category": "other",
            "rule_confidence": 0.8,
            "rule_reason": "relevant residual",
            "score_details": {"hits": {"anchor": ["家族办公室"]}},
            "matched_keywords": ["家族办公室"],
            "final_category": "other",
            "final_confidence": 0.8,
            "final_reason": "relevant residual",
        })
        unclassified_id = self.insert_article(
            "家族办公室尚未分类观察",
            "这条内容尚未产生分类记录。",
            "https://example.com/intel/unclassified",
        )
        self.assertIsNotNone(unclassified_id)
        unrelated_id = self.insert_article(
            "完全无关的社会新闻",
            # 注意：正文不能出现任何行业包锚点词（历史上这里写了"家办"字样，
            # 反而让这条"无关"文章被入库归属兜底判定为家办命中）。
            "这条内容与目标行业没有任何关系，仅用于验证残留资讯的行业相关性过滤。",
            "https://example.com/intel/unrelated",
        )
        self.assertIsNotNone(unrelated_id)

        articles, total, _window = self.repo.list_dashboard_other_articles(
            industry_pack_id="family_office",
            time_range="7d",
        )
        titles = {article["title"] for article in articles}
        self.assertEqual(total, 2)
        self.assertIn("家族办公室补充观察", titles)
        self.assertIn("家族办公室尚未分类观察", titles)
        self.assertNotIn("完全无关的社会新闻", titles)

    def test_dashboard_other_does_not_duplicate_articles_when_classification_stales(self):
        classified_other_id = self.insert_article(
            "家族办公室补充观察",
            "家族办公室的一般行业观察，没有明确事件或趋势信号。",
            "https://example.com/intel/duplicate-check",
        )
        classified_article = self.repo.get_article(classified_other_id)
        self.repo.upsert_classification({
            "article_id": classified_other_id,
            "industry_pack_id": "family_office",
            "industry_pack_version": "1.0.0",
            "classifier_version": "other-test",
            "article_content_hash": self.repo.article_content_hash(classified_article),
            "rule_category": "other",
            "rule_confidence": 0.8,
            "rule_reason": "relevant residual",
            "score_details": {"hits": {"anchor": ["家族办公室"]}},
            "matched_keywords": ["家族办公室"],
            "final_category": "other",
            "final_confidence": 0.8,
            "final_reason": "relevant residual",
        })
        with self.db.lock:
            self.db.connection.execute(
                """
                UPDATE article_intel_classifications
                SET article_content_hash = ?
                WHERE article_id = ? AND industry_pack_id = ?
                """,
                ("stale-hash", classified_other_id, "family_office"),
            )
            self.db.connection.commit()

        articles, total, _window = self.repo.list_dashboard_other_articles(
            industry_pack_id="family_office",
            time_range="7d",
        )
        matching = [article for article in articles if article["article_id"] == classified_other_id]
        self.assertEqual(len(matching), 1)
        self.assertEqual(total, len(articles))

    def test_dashboard_other_does_not_spill_main_categories(self):
        app = Flask(__name__)
        app.register_blueprint(intel_bp)
        window = {"time_range": "7d", "from": "2026-08-01T00:00:00Z", "to": "2026-08-08T00:00:00Z", "timezone": "Asia/Hong_Kong"}

        trend_article = {
            "article_id": 101,
            "title": "趋势文章",
            "final_category": "trend",
            "domain": "example.com",
            "content_preview": "trend",
            "source_display_name": "source",
            "matched_keywords_json": "[]",
            "score_details_json": "{}",
            "topic_tags_json": "[]",
            "ragflow_documents": [],
            "ragflow_status": "not_uploaded",
            "source_evidence": {"evidence_grade": "B"},
        }
        today_article = {
            "article_id": 102,
            "title": "今日文章",
            "final_category": "today",
            "domain": "example.com",
            "content_preview": "today",
            "source_display_name": "source",
            "matched_keywords_json": "[]",
            "score_details_json": "{}",
            "topic_tags_json": "[]",
            "ragflow_documents": [],
            "ragflow_status": "not_uploaded",
            "source_evidence": {"evidence_grade": "B"},
        }
        spillover_article = {
            "article_id": 103,
            "title": "补位文章",
            "final_category": "other",
            "domain": "example.com",
            "content_preview": "spill",
            "source_display_name": "source",
            "matched_keywords_json": "[]",
            "score_details_json": "{}",
            "topic_tags_json": "[]",
            "ragflow_documents": [],
            "ragflow_status": "not_uploaded",
            "source_evidence": {"evidence_grade": "B"},
        }

        def classified_articles_side_effect(*, category="", per_page=20, policy_only=False, **kwargs):
            if policy_only:
                return ([], 0, window)
            if category == "trend":
                return ([trend_article], 1, window)
            if category == "today":
                return ([today_article], 1, window)
            if category == "":
                if per_page == 100:
                    return ([trend_article, today_article, spillover_article], 3, window)
                return ([], 0, window)
            return ([], 0, window)

        with patch.object(intel_api, "intel_repository", self.repo), patch(
            "decorators.user_db.verify_session",
            return_value={"user_id": 1, "role": "admin"},
        ), patch.object(self.repo, "classification_summary", return_value={
            "industry_pack_id": "family_office",
            "time_range": "7d",
            "from": window["from"],
            "to": window["to"],
            "timezone": "Asia/Hong_Kong",
            "counts": {"trend": 1, "today": 1, "other": 0},
            "total": 2,
        }), patch.object(self.repo, "list_classified_articles", side_effect=classified_articles_side_effect), patch.object(self.repo, "list_dashboard_other_articles", return_value=([], 0, window)), patch.object(self.repo, "dashboard_followed_articles", return_value=[]), patch.object(self.repo, "list_ai_recent_articles", return_value=([], 0)), patch.object(self.repo, "dashboard_activity_statistics", return_value={
            # 必须与 IntelRepository.dashboard_activity_statistics 的真实返回结构一致：
            # 看板路由直接取 activity_statistics['today_crawled_articles']，缺键会 500。
            "today_date": "2026-08-08",
            "today_crawled_articles": 0,
            "today_news_parsed_articles": 0,
            "today_google_search_articles": 0,
            "today_google_ingested_articles": 0,
            "today_source_ingested_articles": 0,
            "last_crawl_time": "",
            "news_kb_id": "",
        }), patch.object(self.repo.db, "get_statistics", return_value={"total_articles": 0}):
            client = app.test_client()
            headers = {"Authorization": "Bearer test-token"}
            dashboard = client.get(
                "/api/intel/dashboard?industry_pack_id=family_office&time_range=7d",
                headers=headers,
            )
            self.assertEqual(dashboard.status_code, 200)
            payload = dashboard.get_json()
            self.assertTrue(payload["success"])
            self.assertEqual(payload["sections"]["other"]["total"], 0)
            self.assertEqual(payload["sections"]["other"]["articles"], [])

    def test_final_ingestion_guard_rejects_empty_keyword_matches(self):
        with patch("sqlite_database.config.CRAWL_REQUIRE_KEYWORD_MATCH", True):
            article_id = self.db.insert_article(
                {
                    "url": "https://example.com/intel/no-keyword",
                    "title": "无关键词文章",
                    "content": "正文内容足够长，但未命中任何行业关键词。",
                    "matched_keywords": "",
                }
            )
        self.assertIsNone(article_id)

    def test_schema_initialization_is_idempotent(self):
        self.assertTrue(self.db.create_tables())
        cursor = self.db.connection.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('article_intel_classifications', 'intel_jobs') ORDER BY name"
        )
        self.assertEqual(
            [row["name"] for row in cursor.fetchall()],
            ["article_intel_classifications", "intel_jobs"],
        )
        cursor.close()


if __name__ == "__main__":
    unittest.main()

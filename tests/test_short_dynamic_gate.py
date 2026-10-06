# -*- coding: utf-8 -*-
"""T2.6 分级字数标准单测：短行业动态（30~149 字）允许进入并跳过 LLM；
壳页/会议通知/链接目录仍拒绝；分类器对短动态不调 LLM。"""
import unittest
from unittest.mock import patch

from intel_content_quality_gate import (
    MIN_ARTICLE_CHARS,
    MIN_SHORT_DYNAMIC_CHARS,
    assess_article_quality,
)
from intel_classifier import IntelClassificationService


def _short(n_chars: int, *, title="短讯标题", published="2026-09-18", sentence=True):
    """构造恰好 n 字正文（含一个句号，满足 L2 实质句要求）。"""
    body = "行业动态" + "讯" * max(0, n_chars - 5)
    if sentence:
        body += "。"
    return {"title": title, "content": body[:n_chars], "publish_date": published, "site_name": "example.com"}


class ShortDynamicGateTest(unittest.TestCase):
    def test_150_chars_is_full_tier(self):
        result = assess_article_quality(_short(MIN_ARTICLE_CHARS), {})
        self.assertTrue(result["passed"])
        self.assertEqual(result["tier"], "full")
        self.assertFalse(result["skip_llm"])

    def test_149_chars_is_short_dynamic_and_passes(self):
        result = assess_article_quality(_short(MIN_ARTICLE_CHARS - 1), {})
        self.assertTrue(result["passed"])
        self.assertEqual(result["tier"], "short_dynamic")
        self.assertTrue(result["skip_llm"])
        self.assertNotIn("content_too_short", result["issues"])

    def test_30_chars_boundary_passes(self):
        result = assess_article_quality(_short(MIN_SHORT_DYNAMIC_CHARS), {})
        self.assertTrue(result["passed"])
        self.assertEqual(result["tier"], "short_dynamic")

    def test_29_chars_still_rejected(self):
        result = assess_article_quality(_short(MIN_SHORT_DYNAMIC_CHARS - 1), {})
        self.assertFalse(result["passed"])
        self.assertIn("content_too_short", result["issues"])
        self.assertEqual(result["tier"], "too_short")

    def test_short_dynamic_requires_title(self):
        result = assess_article_quality(_short(100, title=""), {})
        self.assertFalse(result["passed"])
        self.assertIn("short_dynamic_missing_title", result["issues"])

    def test_short_dynamic_requires_publish_date(self):
        result = assess_article_quality(_short(100, published=""), {})
        self.assertFalse(result["passed"])
        self.assertIn("short_dynamic_missing_publish", result["issues"])

    def test_short_dynamic_requires_sentence(self):
        result = assess_article_quality(_short(100, sentence=False), {})
        self.assertFalse(result["passed"])
        self.assertIn("short_dynamic_no_sentence", result["issues"])

    def test_metadata_shell_still_rejected_in_short_range(self):
        shell = ("发文机关：某单位\n标　　题：通知\n成文日期：2026-09-01\n发布日期：2026-09-02\n")
        result = assess_article_quality(
            {"title": "通知", "content": shell, "publish_date": "2026-09-02", "site_name": "example.com"},
            {},
        )
        self.assertFalse(result["passed"])
        self.assertIn("content_is_metadata_shell", result["issues"])

    def test_meeting_notice_still_rejected_in_short_range(self):
        notice = ("会议时间：2026-10-01\n会议地点：北京\n主办单位：某学会\n报名方式：邮件报名。")
        result = assess_article_quality(
            {"title": "会议通知", "content": notice, "publish_date": "2026-09-18", "site_name": "example.com"},
            {},
        )
        self.assertFalse(result["passed"])
        self.assertIn("content_is_meeting_notice", result["issues"])


class ShortDynamicClassifierTest(unittest.TestCase):
    """短行业动态分类：跳过 LLM，直接采用规则结果。"""

    PACK = {
        "id": "auto_test",
        "pack_version": "v1",
        "core_keywords": ["网络安全"],
        "expanded_keywords": [],
        "trend_keywords": [],
        "event_keywords": [],
        "negative_keywords": [],
        "brands": [],
        "candidate_gate": {"anchor_keywords": ["网络安全"]},
        "classification": {
            # 锚点只有"网络安全"一个，规则相关性恒为 core_weight；
            # 归属必填契约要求 relevance >= minimum_relevance_score 才是真实规则分类，
            # 同时本类用例要求"无趋势/事件信号"时规则置信度 0.55+relevance/30 低于
            # llm_confidence_threshold=0.6（否则全量档文章的 LLM 分支不可达）。
            # core_weight=1、minimum_relevance_score=1 同时满足这两个条件。
            "core_weight": 1, "expanded_weight": 1, "trend_weight": 2,
            "event_weight": 2, "negative_weight": -3,
            "minimum_relevance_score": 1,
            "llm_confidence_threshold": 0.6,
            "tie_break_order": ["trend", "event", "other"],
        },
    }

    def setUp(self):
        self.article = {
            "id": 1, "title": "网络安全动态",
            "content": "网络安全" + "讯" * 90 + "。",
            "publish_date": "2026-09-18",
        }
        assert MIN_SHORT_DYNAMIC_CHARS <= len(self.article["content"]) < MIN_ARTICLE_CHARS

    def _service(self, llm_client):
        class Repo:
            def get_article(self, article_id):
                return self.article

            def article_content_hash(self, article):
                return "hash1"

            def upsert_classification(self, result):
                # 归属必填契约：规则分类产出一定会落库（classify_article_id 末尾
                # 必然调用 upsert_classification），桩需支持该调用。
                self.saved.append(dict(result))
                return 1
        repo = Repo()
        repo.article = self.article
        repo.saved = []
        pack_loader = type("Loader", (), {"load": lambda self, pack_id: self.PACK})()
        pack_loader.PACK = self.PACK
        return IntelClassificationService(repository=repo, pack_loader=pack_loader, llm_client=llm_client)

    def test_short_dynamic_skips_llm_classification(self):
        class NoLLM:
            model_id = "mock"
            def classify(self, article, pack):
                raise AssertionError("short_dynamic 不得调用 LLM 分类")
        service = self._service(NoLLM())
        with patch("intel_classifier.config.INTEL_LLM_ENABLED", True):
            result = service.classify_article_id(1, "auto_test")
        self.assertTrue(result.get("admitted"))
        self.assertEqual(result["llm_model_id"], "")
        self.assertEqual(result["result_source"], "rule")

    def test_full_article_still_calls_llm(self):
        calls = []
        class YesLLM:
            model_id = "mock"
            def classify(self, article, pack):
                calls.append(article)
                return {
                    "category": "other", "confidence": 0.9,
                    "reason": "llm", "why_important": "", "trend_summary": "",
                    "topic_tags": [], "in_pack_industry": True,
                }
        self.article["content"] = "网络安全" + "讯" * 300 + "。"
        service = self._service(YesLLM())
        with patch("intel_classifier.config.INTEL_LLM_ENABLED", True):
            result = service.classify_article_id(1, "auto_test")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["llm_model_id"], "mock")


if __name__ == "__main__":
    unittest.main()

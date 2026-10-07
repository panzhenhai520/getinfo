# -*- coding: utf-8 -*-
"""钉住"LLM 分类通道对生产行业包可达"这条性质（family_office 实测为死代码）。

现象（2026-10-06 在 B 机用真实包配置探针复现）：
  family_office 的 classification 配置是 core_weight=3、minimum_relevance_score=2、
  llm_confidence_threshold=0.65，而规则在"命中锚点但没命中趋势/事件"分支上的置信度是
  `0.55 + 相关性/30`；相关性 = 锚点数 × core_weight，锚点至少 1 个 ⇒ 相关性 ≥ 3
  ⇒ 置信度 ≥ 0.65 恰好等于阈值 ⇒ `置信度 < 阈值` 永不成立 ⇒ **带锚点的文章永远不调 LLM**。
  而没锚点的文章虽然会调 LLM，但结论随后被"归属必填兜底"改写成 other，
  于是该包 `llm_override` 永远不会出现。

修复：规则自己给出 no_signal（只判断出"行业相关"、没有趋势/事件信号）时也算"不确定"，
必须交给 LLM 复核。本测试同时守住"有明确信号、置信度够高时不要多花 LLM 调用"。
"""
import unittest
from unittest.mock import patch

from intel_classifier import IntelClassificationService, _rule_needs_llm


class _Repo:
    def __init__(self, article):
        self.article = article
        self.saved = []

    def get_article(self, article_id):
        return self.article

    def article_content_hash(self, article):
        return "hash-family-office"

    def upsert_classification(self, result):
        self.saved.append(dict(result))
        return 1


class _LLM:
    model_id = "mock-llm"

    def __init__(self, *, category="trend", confidence=0.91):
        self.category = category
        self.confidence = confidence
        self.calls = 0

    def classify(self, article, pack):
        self.calls += 1
        return {
            "category": self.category, "confidence": self.confidence,
            "reason": "LLM 判断为行业趋势", "why_important": "影响面较大",
            "trend_summary": "政策推动", "topic_tags": [],
            "in_pack_industry": True,
        }


def _family_office_pack():
    """按 family_office 的真实分类参数构造（core_weight=3、阈值 0.65）。"""
    return {
        "id": "family_office",
        "pack_version": "test-1",
        "classification": {
            "core_weight": 3, "expanded_weight": 2, "trend_weight": 2,
            "event_weight": 2, "negative_weight": -3,
            "minimum_relevance_score": 2,
            "llm_confidence_threshold": 0.65,
            "tie_break_order": ["trend", "event", "other"],
        },
        "core_keywords": ["家族办公室"],
        "expanded_keywords": [],
        "trend_keywords": [],
        "event_keywords": [],
        "negative_keywords": [],
        "fixed_topics": [],
        "default_sources": [],
    }


class LlmChannelReachabilityTest(unittest.TestCase):
    def setUp(self):
        # 命中 1 个锚点（相关性=3 ≥ 最低 2）、正文足够长（不走 short_dynamic 跳过 LLM）、
        # 且不含趋势词/事件词 → 正好落在"no_signal"分支，规则置信度 0.65 == 阈值
        self.article = {
            "id": 7,
            "title": "家族办公室服务动态",
            "content": "家族办公室" + "服务动态" * 60 + "。",
            "publish_date": "2026-10-06",
            "site_name": "example.com",
        }

    def _service(self, llm):
        repo = _Repo(self.article)
        loader = type("Loader", (), {"load": lambda self, pack_id: _family_office_pack()})()
        return IntelClassificationService(repository=repo, pack_loader=loader, llm_client=llm), repo

    def test_rule_confidence_equals_threshold_still_needs_llm(self):
        """纯函数层：置信度恰好等于阈值 + no_signal ⇒ 需要 LLM。"""
        rule_result = {
            "rule_confidence": 0.65,
            "score_details": {"rule_signal": "no_signal"},
        }
        self.assertTrue(_rule_needs_llm(rule_result, 0.65),
                        "置信度等于阈值且规则没有信号时必须交给 LLM 复核")

    def test_signal_bearing_confident_rule_skips_llm(self):
        """有明确信号且置信度高于阈值时不多花 LLM 调用（省成本）。"""
        rule_result = {
            "rule_confidence": 0.85,
            "score_details": {"rule_signal": "signal"},
        }
        self.assertFalse(_rule_needs_llm(rule_result, 0.65))

    def test_family_office_llm_override_is_reachable(self):
        """端到端：该包配置下 LLM 必须真的被调用，且高置信结论能覆盖规则分类。"""
        llm = _LLM(category="trend", confidence=0.91)
        service, repo = self._service(llm)
        with patch("intel_classifier.config.INTEL_LLM_ENABLED", True):
            result = service.classify_article_id(7, "family_office")

        self.assertEqual(1, llm.calls,
                         "family_office 的 LLM 分类通道必须可达（此前恒为 0 次调用）")
        self.assertEqual("mock-llm", result["llm_model_id"])
        self.assertEqual("trend", result["final_category"])
        self.assertEqual("llm_override", result["result_source"])
        self.assertFalse(result.get("_not_classified"),
                         "命中锚点且相关性达标的文章不应被兜底归属覆盖")
        self.assertEqual("no_signal",
                         (result["score_details"] or {}).get("rule_signal"),
                         "规则不确定的原因要能解释清楚")
        self.assertTrue(repo.saved, "分类结果必须落库（归属必填契约）")


if __name__ == "__main__":
    unittest.main()

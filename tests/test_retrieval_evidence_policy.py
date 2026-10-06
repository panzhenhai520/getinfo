#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检索证据池策略守卫：**零行业信号的文章不得进入 AI 证据池**。

这是产品决策（明确要求"不要放开零行业信号的文章这类『其他』进 AI 证据"）：
  - 兜底归属保证每篇入库文章都有包归属、页面上看得到（至少落在该包「其他」）；
  - 但"能不能被 AI 当证据引用"仍由行业相关性闸门决定：
    `intel_topics._classification_admitted(score_details)` 要求命中行业锚点
    （或命中核心/扩展词且分量达标）。
所以本测试同时钉两件事：
  1. 闸门本身对"无命中"的 score_details 返回 False；
  2. 检索侧 `ArticleRetriever._rows` 不得对 `fallback_attribution` 之类的来源开特例
     （否则等于绕开闸门，把零信号文章塞进证据池）。
"""
import ast
import unittest
from pathlib import Path

from intel_topics import _classification_admitted

REPO_ROOT = Path(__file__).resolve().parent.parent


class EvidenceAdmissionGateTests(unittest.TestCase):
    def test_empty_score_details_is_rejected(self):
        self.assertFalse(_classification_admitted({}))
        self.assertFalse(_classification_admitted({"hits": {}}))
        self.assertFalse(_classification_admitted(
            {"hits": {"anchor": [], "core": [], "expanded": []}}))

    def test_relevance_below_minimum_is_rejected(self):
        """命中锚点但相关性低于阈值 → 仍不准入。"""
        self.assertFalse(_classification_admitted({
            "hits": {"anchor": ["家族办公室"]},
            "relevance_score": 1.0,
            "minimum_relevance_score": 5.0,
        }))

    def test_anchor_hit_above_minimum_is_admitted(self):
        self.assertTrue(_classification_admitted({
            "hits": {"anchor": ["家族办公室"]},
            "relevance_score": 6.0,
            "minimum_relevance_score": 5.0,
        }))

    def test_core_or_expanded_need_enough_weight(self):
        """只有扩展/核心词时，分量必须达到阈值一半（与派发准入口径一致）。"""
        self.assertFalse(_classification_admitted({
            "hits": {"expanded": ["税务宽免"]},
            "components": {"expanded": 0.2, "core": 0.0},
            "minimum_relevance_score": 4.0,
        }))
        self.assertTrue(_classification_admitted({
            "hits": {"expanded": ["税务宽免"]},
            "components": {"expanded": 2.5, "core": 0.0},
            "minimum_relevance_score": 4.0,
        }))


class RetrievalPolicySourceTests(unittest.TestCase):
    """源码级守卫：检索侧不得给兜底归属开"免检"后门。"""

    def _rows_source(self) -> str:
        source = (REPO_ROOT / "qa_retrieval.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_rows":
                return ast.get_source_segment(source, node) or ""
        self.fail("qa_retrieval.ArticleRetriever._rows 不存在")

    def test_rows_keeps_quality_gate(self):
        body = self._rows_source()
        self.assertIn("_classification_admitted", body,
                      "检索侧必须保留行业相关性闸门，不能只按归属行取文章")

    def test_rows_does_not_exempt_fallback_attribution(self):
        body = self._rows_source().casefold()
        for marker in ("fallback_attribution", "result_source"):
            self.assertNotIn(
                marker, body,
                "检索侧不得按 result_source 给兜底归属开免检后门"
                "（产品决策：零行业信号文章不进 AI 证据池）",
            )


if __name__ == "__main__":
    unittest.main()

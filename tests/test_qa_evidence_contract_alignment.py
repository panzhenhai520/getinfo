#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""守门测试：**服务端自己造的证据条目必须能过契约**。

真实教训（两次同源）：
  · 阶段 4 给证据条目加了 `published_at_utc / published_precision / published_timezone /
    published_time_note`，但契约是 `additionalProperties: False` → **只要证据来自文章**，
    一级草稿的契约校验就整条失败 → 降级回答。生产上一直存在，直到阶段 6 的验收实测才暴露；
  · 阶段 9 给证据加了 `source_type='graph'` 与图谱字段，同样要放行。

这个测试不测模型，只测"我们自己造的证据"与契约的一致性——任何将来再加字段忘了同步契约，
这里就会红。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qa_contracts import QA_CONTRACT_VERSION, validate_level1_result  # noqa: E402
from qa_retrieval import _article_evidence  # noqa: E402


def _article_row(**overrides):
    row = {
        "id": 12345,
        "title": "测试文章标题",
        "url": "https://example.com/article/1",
        "content": "测试正文" * 50,
        "domain": "example.com",
        "authority_level": 60,
        "publish_date": "2026-10-01",
        "first_crawled": "2026-10-01T00:00:00Z",
        "published_at_utc": "2026-10-01T00:00:00Z",
        "published_timezone": "Asia/Shanghai",
        "published_precision": "date",
        "source_name": "测试来源",
    }
    row.update(overrides)
    return row


class EvidenceContractAlignmentTests(unittest.TestCase):
    def _level1_with(self, evidence):
        """用**真实构造函数**造一级结果：服务端自造的结果本身就必须过契约。"""
        from qa_level1 import empty_level1_result

        return empty_level1_result("测试草稿", list(evidence))

    def test_article_evidence_passes_level1_contract(self):
        item = _article_evidence(_article_row(), score=12.0, method="keyword", reason="命中关键词")
        result = validate_level1_result(self._level1_with([item]))
        self.assertEqual(len(result["evidence"]), 1)
        self.assertEqual(result["evidence"][0]["published_precision"], "date",
                         "证据精度字段必须原样保留（契约放行，不能被吞掉）")

    def test_page_context_evidence_passes_contract(self):
        item = _article_evidence(_article_row(), score=1000, method="page_context",
                                 reason="用户当前页面", source_type="page_context")
        validate_level1_result(self._level1_with([item]))

    def test_evidence_anchored_fallback_passes_contract(self):
        """降级路径（证据锚定草稿）也必须过契约——它同样带着真实证据条目。"""
        from qa_pipeline import _evidence_anchored_level1_fallback

        item = _article_evidence(_article_row(), score=12.0, method="hybrid", reason="混合命中")
        result = _evidence_anchored_level1_fallback("资料已按证据锚定方式整理。", [item], ["测试问题"])
        validate_level1_result(result)

    def test_graph_evidence_passes_contract(self):
        item = {
            "evidence_ref": "edge:abc123",
            "source_type": "graph",
            "title": "图谱属性：某模型 的精度定位：高精度",
            "source_url": "https://example.com/article/1",
            "article_id": None,
            "content_excerpt": "某模型 的精度定位：高精度；原文：最近生产的模型是高精度的模型",
            "published_at": "2026-10-01",
            "fetched_at": None,
            "ragflow_kb_id": None,
            "document_id": None,
            "chunk_id": None,
            "score": 65.0,
            "authority_level": 50,
            "retrieval_method": "graph_attribute",
            "match_reason": "知识图谱属性边（主体命中问题：某模型）",
            "relationship": "supports",
            "metadata": {"graph_edge_key": "abc123", "relation_kind": "attribute",
                         "attr_key": "精度定位", "attr_value": "高精度",
                         "valid_from": "2026", "article_id": 12345},
        }
        validate_level1_result(self._level1_with([item]))


if __name__ == "__main__":
    unittest.main()

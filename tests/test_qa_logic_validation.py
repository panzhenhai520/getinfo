#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 9 · 逻辑校验 + 多跳执行的行为测试。

钉住三件事：
  1. 因果 / 条件 / 时序三类各自的检查项真的会跑（有证据=通过，缺证据=记缺口）；
  2. 多跳里**任何一跳没取到证据**都必须落到 `missing_links` 且状态为 `degraded`
     （不允许"半截结论装作完整"）；
  3. 预算用尽时停在做完的跳上，并写明原因与已用时间。
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from qa_pipeline import _logic_validation, _run_multi_hop  # noqa: E402


def _evidence(*texts, refs=None):
    refs = refs or ["article:%d" % (index + 1) for index in range(len(texts))]
    return [{"evidence_ref": refs[index],
             "title": text[:60], "content_excerpt": text, "source_url": "https://example.com/x"}
            for index, text in enumerate(texts)]


class LogicValidationTests(unittest.TestCase):
    def test_causal_chain_present_passes(self):
        result = _logic_validation(
            "为什么家族办公室数量增长？",
            {"evidence": _evidence("由于香港税制优惠，导致家族办公室数量增长明显。")},
            {"category": {"key": "causal"}},
        )
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["checks"][0]["passed"])
        self.assertFalse(result["missing_links"])

    def test_causal_chain_missing_is_flagged(self):
        result = _logic_validation(
            "为什么家族办公室数量增长？",
            {"evidence": _evidence("香港家族办公室数量在 2025 年达到 2000 家。")},
            {"category": {"key": "causal"}},
        )
        self.assertEqual(result["status"], "degraded")
        self.assertTrue(any(item["type"] == "causal_link" for item in result["missing_links"]))
        self.assertIn("因果", result["note"])

    def test_condition_coverage(self):
        plan = {"category": {"key": "conditional_constraint"},
                "decomposition": {"hops": [{"question": "CRS合规"}]}}
        passed = _logic_validation("在CRS合规条件下是否要申报？",
                                   {"evidence": _evidence("CRS合规要求金融机构申报。")}, plan)
        self.assertEqual(passed["status"], "passed")
        failed = _logic_validation("在CRS合规条件下是否要申报？",
                                   {"evidence": _evidence("香港利得税税率是 16.5%。")}, plan)
        self.assertTrue(any(item["type"] == "condition_gap" for item in failed["missing_links"]))

    def test_temporal_order_needs_two_dates(self):
        plan = {"category": {"key": "temporal_relation"}}
        one = _logic_validation("先出台政策还是先有试点？",
                                {"evidence": _evidence("2026 年 3 月出台政策。")}, plan)
        self.assertTrue(any(item["type"] == "temporal_gap" for item in one["missing_links"]))
        two = _logic_validation("先出台政策还是先有试点？",
                                {"evidence": _evidence("2025 年 3 月开始试点。",
                                                       "2026 年 3 月出台政策。",
                                                       refs=["article:8", "article:9"])}, plan)
        self.assertEqual(two["status"], "passed")

    def test_multi_hop_empty_hop_is_missing_link(self):
        retrieval = {
            "evidence": _evidence("第一跳的证据"),
            "multi_hop": {
                "degraded": True,
                "hops": [
                    {"hop_id": "h1", "question": "A", "evidence": 3, "status": "ok"},
                    {"hop_id": "h2", "question": "B", "evidence": 0, "status": "empty"},
                ],
            },
        }
        result = _logic_validation("A 对 B 有什么影响？", retrieval,
                                   {"category": {"key": "multi_hop"}})
        self.assertEqual(result["status"], "degraded")
        self.assertTrue(result["degraded"])
        self.assertTrue(any(item["type"] == "hop_missing" for item in result["missing_links"]))
        self.assertIn("h2", result["note"])

    def test_no_evidence_is_insufficient(self):
        result = _logic_validation("某问题", {"evidence": []}, {"category": {"key": "fact_check"}})
        self.assertEqual(result["status"], "insufficient")


class _FakeRetriever:
    """按问句返回预设证据的可控检索器（用于验证多跳执行与预算）。"""

    def __init__(self, mapping, delay=0.0):
        import time

        self.mapping = mapping
        self.delay = delay
        self.calls = []
        self._time = time

    def retrieve(self, plan, **kwargs):
        self.calls.append(dict(plan))
        if self.delay:
            self._time.sleep(self.delay)
        question = str(plan.get("question") or "")
        # 命中最长、且位置最靠后的键（传导链里 B 通常在 A 之后出现）
        hits = [key for key in self.mapping if key in question]
        if hits:
            best = max(hits, key=lambda key: (len(key), question.rfind(key)))
            return {"evidence": self.mapping[best], "stats": {}, "queries": [question]}
        return {"evidence": [], "stats": {}, "queries": [question]}


class MultiHopExecutionTests(unittest.TestCase):
    def setUp(self):
        self._saved = (config.QA_MULTI_HOP_BUDGET_SECONDS, config.QA_MULTI_HOP_ENABLED)
        config.QA_MULTI_HOP_ENABLED = True
        config.QA_MULTI_HOP_BUDGET_SECONDS = 25

    def tearDown(self):
        config.QA_MULTI_HOP_BUDGET_SECONDS, config.QA_MULTI_HOP_ENABLED = self._saved

    def _plan(self):
        return {
            "question": "2026年医保新规对民营医院有什么影响？",
            "entities": ["医保"],
            "decomposition": {
                "is_multi_hop": True,
                "pattern": "impact_chain",
                "hops": [
                    {"id": "h1", "question": "2026年医保新规", "depends_on": []},
                    {"id": "h2", "question": "2026年医保新规 民营医院", "depends_on": ["h1"]},
                    {"id": "h3", "question": "民营医院 影响", "depends_on": ["h2"]},
                ],
            },
        }

    def test_collects_evidence_per_hop_and_dedupes(self):
        retriever = _FakeRetriever({
            "医保新规": _evidence("医保新规落地，门诊报销比例提高20%。", refs=["article:1"]),
            "民营医院": _evidence("民营医院经营压力上升，门诊量下降。", refs=["article:2"]),
        })
        first = {"evidence": _evidence("医保新规落地，门诊报销比例提高20%。", refs=["article:1"])}
        merged, receipts = _run_multi_hop(retriever, self._plan(), first, {},
                                         pack_id="health", limit=8)
        self.assertEqual(len(receipts), 3)
        self.assertEqual(receipts[0]["hop_id"], "h1")
        self.assertEqual(receipts[1]["status"], "ok")
        # 第 1 跳的证据与合并结果里的重复项被去掉了（按 evidence_ref 去重）
        self.assertEqual(len(merged["evidence"]), 2)
        self.assertEqual(receipts[1]["added"], 1)
        self.assertTrue(merged["multi_hop"]["enabled"])
        self.assertIn("pattern", merged["multi_hop"])

    def test_empty_hop_marks_degraded(self):
        # h2/h3 都匹配不到证据 → 必须落到 empty 且整体标记降级
        retriever = _FakeRetriever({"医保新规": _evidence("第一跳证据", refs=["article:1"])})
        merged, receipts = _run_multi_hop(retriever, self._plan(), {"evidence": []}, {},
                                          pack_id="health", limit=8)
        statuses = [item["status"] for item in receipts]
        self.assertIn("empty", statuses)
        self.assertTrue(merged["multi_hop"]["degraded"])

    def test_budget_stops_remaining_hops(self):
        config.QA_MULTI_HOP_BUDGET_SECONDS = 0.05
        retriever = _FakeRetriever({"医保新规": _evidence("第一跳证据"),
                                    "民营医院": _evidence("第二跳证据")}, delay=0.12)
        merged, receipts = _run_multi_hop(retriever, self._plan(), {"evidence": []}, {},
                                          pack_id="health", limit=8)
        skipped = [item for item in receipts if item["status"] == "skipped_budget"]
        self.assertTrue(skipped, "预算用尽必须停跳并留回执")
        self.assertIn("预算", skipped[0]["reason"])
        self.assertTrue(merged["multi_hop"]["degraded"])

    def test_single_hop_decomposition_is_noop(self):
        plan = {"question": "某问题", "decomposition": {"is_multi_hop": False, "hops": [{"id": "h1"}]}}
        retriever = _FakeRetriever({})
        merged, receipts = _run_multi_hop(retriever, plan, {"evidence": []}, {}, pack_id="p", limit=8)
        self.assertEqual(receipts, [])
        self.assertEqual(retriever.calls, [])


if __name__ == "__main__":
    unittest.main()

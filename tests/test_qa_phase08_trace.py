#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 08 · P08-06（selection trace）用例。

"Context Planner 选了哪些、扔了哪些、为什么"必须**逐条可复算**：
  1. 每个候选恰好一条决策留痕（候选数 == trace 行数，item_id 唯一）；
  2. 决策与原因只吃冻结枚举（`CONTEXT_DECISIONS` / `CONTEXT_SELECTION_REASONS`）；
  3. 每条留痕都带效用、五个分量、token 成本、决策后余额与序号 —— 能重放决策；
  4. **确定性**：同输入两次构建，trace 逐字相同（含 rank 顺序）；
  5. 聚合口径 `selection_summary()` 与明细一致（入选/淘汰/原因分布/token 合计）；
  6. 被裁的原因分布**真的随预算变化**（预算越大 OVER_TOKEN_BUDGET 越少）。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_context_pack as cp  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
from qa_phase08_fixtures import (  # noqa: E402
    QUESTION, conflict, evidence_item, graph, graph_claim, graph_edge, hop, plan, verification,
)

SUPPORTER_TEXT = "香港家族办公室税收优惠政策对内地高净值客户的申报义务影响要点。"


def _corpus(count=30):
    evidence, edges = [], []
    for index in range(count):
        ref = "article:%d" % (index + 1)
        evidence.append(evidence_item(ref, text=SUPPORTER_TEXT, authority=95 - index % 20,
                                      published="2026-10-0%d" % (1 + index % 9),
                                      verification=verification("SUPPORTED", score=0.9)))
        edges.append(graph_edge("c1", ref))
    evidence.append(evidence_item("article:99", text="不同意见认为影响被高估。" * 6,
                                  authority=5, published="2019-01-01",
                                  verification=verification("REFUTED", score=0.4)))
    edges.append(graph_edge("c1", "article:99", relation="REFUTES", status="REFUTED"))
    claims = [graph_claim("c1", text="香港家族办公室税收优惠政策对内地高净值客户有影响",
                          refs=["article:1"])]
    return graph(claims=claims, evidence=evidence, edges=edges,
                 conflicts=[conflict("k1", ["c1"], evidence_refs=["article:99"])])


def _pack(budget=600):
    return cp.build_context_pack(graph=_corpus(), plan=plan(hops=[hop("h1", "政策事实")]),
                                 request={"question": QUESTION, "mode": "standard"},
                                 run_id="r1", budget_tokens=budget)


class TraceShapeTests(unittest.TestCase):
    def test_every_candidate_has_exactly_one_decision(self):
        pack = _pack()
        trace = pack["selection_trace"]
        self.assertEqual(len(trace), pack["budget"]["candidates"])
        self.assertEqual(len({row["item_id"] for row in trace}), len(trace))

    def test_every_row_validates_against_the_frozen_contract(self):
        pack = _pack()
        for row in pack["selection_trace"]:
            ok, note = cp.validate_contract("context_selection", row)
            self.assertTrue(ok, note)
            self.assertIn(row["decision"], contracts.CONTEXT_DECISIONS)
            self.assertIn(row["reason"], contracts.CONTEXT_SELECTION_REASONS)
            self.assertIn(row["trace_version"], (contracts.CONTEXT_SELECTION_VERSION,))
            self.assertGreaterEqual(row["rank"], 0)
            self.assertGreaterEqual(row["budget_after"], 0)

    def test_rows_carry_the_replayable_facts(self):
        pack = _pack()
        for row in pack["selection_trace"]:
            self.assertEqual(tuple(row["utility_factors"]), contracts.CONTEXT_UTILITY_FACTORS)
            self.assertGreaterEqual(row["utility"], 0.0)
            self.assertGreaterEqual(row["tokens"], 0)

    def test_ranks_are_a_dense_sequence(self):
        pack = _pack()
        ranks = [row["rank"] for row in pack["selection_trace"]]
        self.assertEqual(ranks, list(range(len(ranks))))

    def test_included_rows_account_for_the_used_budget(self):
        pack = _pack()
        included = [row for row in pack["selection_trace"] if row["decision"] == "included"]
        self.assertEqual(sum(row["tokens"] for row in included), pack["budget"]["used"])
        self.assertEqual(len(included), pack["budget"]["included"])


class TraceDeterminismTests(unittest.TestCase):
    def test_same_input_same_trace(self):
        first, second = _pack(), _pack()
        self.assertEqual(first["selection_trace"], second["selection_trace"])
        self.assertEqual(cp.selection_summary(first["selection_trace"]),
                         cp.selection_summary(second["selection_trace"]))

    def test_budget_changes_the_reason_distribution(self):
        tight = _pack(500)["budget"]["trim_reasons"].get("OVER_TOKEN_BUDGET", 0)
        loose = _pack(6000)["budget"]["trim_reasons"].get("OVER_TOKEN_BUDGET", 0)
        self.assertGreater(tight, 0)
        self.assertLess(loose, tight, "预算放大后'预算不足'的淘汰必须变少")

    def test_mandatory_and_reserved_reasons_are_distinguishable(self):
        pack = _pack(600)
        reasons = {row["reason"] for row in pack["selection_trace"]}
        self.assertIn("MANDATORY_SECTION", reasons)
        self.assertIn("COUNTER_EVIDENCE_RESERVED", reasons)
        self.assertIn("OVER_TOKEN_BUDGET", reasons)


class TraceSummaryTests(unittest.TestCase):
    def test_summary_matches_the_detail(self):
        pack = _pack()
        summary = cp.selection_summary(pack["selection_trace"])
        trace = pack["selection_trace"]
        self.assertEqual(summary["rows"], len(trace))
        self.assertEqual(summary["included"],
                         len([row for row in trace if row["decision"] == "included"]))
        self.assertEqual(summary["excluded"],
                         len([row for row in trace if row["decision"] == "excluded"]))
        self.assertEqual(sum(summary["reason_distribution"].values()), len(trace))
        self.assertEqual(summary["tokens_included"], pack["budget"]["used"])
        self.assertEqual(summary["reserved_included"],
                         len([row for row in trace
                              if row["decision"] == "included" and row["reserved"]]))

    def test_summary_handles_an_empty_trace(self):
        summary = cp.selection_summary([])
        self.assertEqual(summary["rows"], 0)
        self.assertIsNone(summary["avg_utility_included"])

    def test_receipt_exposes_the_trim_distribution(self):
        receipt = cp.context_pack_receipt(_pack())
        self.assertIn("trim_reasons", receipt["budget"])
        self.assertEqual(receipt["budget"]["estimator"], cp.ESTIMATOR_VERSION)
        self.assertTrue(receipt["budget"]["trimmed_items"] > 0)
        self.assertEqual(receipt["retrieval_requested"], 0)


if __name__ == "__main__":
    unittest.main()

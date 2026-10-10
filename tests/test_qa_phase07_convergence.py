#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 07 · P07-05（no-gain convergence）+ P07-06（stop reasons）用例。

钉住：
  1. §14 的收敛条件**逐字**落地：连续若干轮 `new_verified_claims == 0`
     且 `resolved_high_priority_gaps == 0` → NO_GAIN；有增益就把连续计数清零；
  2. `resolved` 口径严格："上轮有、本轮没了"才算（同一条件下 gap_id 稳定，不靠感觉）；
  3. **五个停止原因真的都能被产出**（这是 Phase 05 明确留给 Phase 07 的账）：
     ANSWERABLE / BUDGET_EXHAUSTED / MAX_DEPTH / NO_GAIN / UNRESOLVABLE_CONTRADICTION；
     UNRESOLVABLE_CONTRADICTION 必须引用 Phase 06 的 unresolved 裁决，不许凭空写；
  4. 优先级顺序可复算（具体优先于笼统），回执过 `gap_loop` 契约。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_gap_analyzer as gap  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402
from qa_phase07_fixtures import (  # noqa: E402
    contradiction, evidence_item, graph_claim, plan, plan_claim, verification,
)

CLAIM_TEXT = "2026年医保新规要求民营医院按病种付费"
SUPPORTED = verification("SUPPORTED", score=0.8, entailment=0.9)


def _state(**kwargs):
    kwargs.setdefault("budget_seconds", 0.0)
    return gap.GapLoopState(**kwargs)


def _claims():
    return [plan_claim("c1", CLAIM_TEXT)]


class NoGainTests(unittest.TestCase):
    def test_two_barren_rounds_stop_with_no_gain(self):
        state = _state()
        first = state.observe(round_index=0, claims=_claims(), evidence=[])
        self.assertTrue(first["baseline"], "第 0 轮是基线（没有上一轮可比）")
        self.assertEqual(first["no_gain_streak"], 0, "基线轮不计入连续无增益")
        barren = state.observe(round_index=1, claims=_claims(), evidence=[])
        last = state.observe(round_index=2, claims=_claims(), evidence=[])
        self.assertEqual(barren["no_gain_streak"], 1)
        self.assertTrue(last["no_gain"])
        self.assertGreaterEqual(last["no_gain_streak"], 2)
        self.assertTrue(state.no_gain_confirmed())
        decision = state.finalize(depth_exhausted=True)
        self.assertEqual(decision["stop_reason"], contracts.QA_STOP_NO_GAIN)
        self.assertIn("连续", decision["detail"])

    def test_new_verified_claim_resets_the_streak(self):
        state = _state()
        state.observe(round_index=0, claims=_claims(), evidence=[])
        barren = state.observe(round_index=1, claims=_claims(), evidence=[])
        self.assertEqual(barren["no_gain_streak"], 1)
        gain = state.observe(round_index=2, claims=_claims(), evidence=[
            evidence_item("article:1", text=CLAIM_TEXT, verification=SUPPORTED,
                          authority=100)])
        self.assertEqual(gain["new_verified_claims"], 1)
        self.assertEqual(gain["no_gain_streak"], 0, "有增益必须清零")
        self.assertFalse(state.no_gain_confirmed())

    def test_resolving_a_high_priority_gap_counts_as_gain(self):
        """第二轮补上权威证据 → 上一轮的 LOW_RELEVANCE 高优缺口消失 = 有增益。"""
        state = _state(threshold=0.5)
        low = [evidence_item("article:9", text=CLAIM_TEXT, authority=5)]
        state.observe(round_index=0, claims=_claims(), evidence=low)
        before = state.last_round
        self.assertGreaterEqual(before["high_priority_gaps"], 1)
        after = state.observe(round_index=1, claims=_claims(), evidence=low + [
            evidence_item("article:1", text=CLAIM_TEXT, authority=100,
                          verification=SUPPORTED)])
        self.assertGreaterEqual(after["resolved_high_priority_gaps"], 1)
        self.assertFalse(after["no_gain"])

    def test_gap_ids_are_stable_across_rounds(self):
        state = _state()
        first = state.observe(round_index=0, claims=_claims(), evidence=[])
        second = state.observe(round_index=1, claims=_claims(), evidence=[])
        self.assertEqual(first["gap_ids"], second["gap_ids"],
                         "同一条件下 gap_id 必须稳定，否则 resolved 判定会失真")
        self.assertEqual(second["resolved_gaps"], 0)

    def test_no_gain_threshold_is_configurable(self):
        os.environ["QA_GAP_NO_GAIN_ROUNDS"] = "3"
        try:
            state = _state()
            state.observe(round_index=0, claims=_claims(), evidence=[])
            state.observe(round_index=1, claims=_claims(), evidence=[])
            third = state.observe(round_index=2, claims=_claims(), evidence=[])
            self.assertFalse(state.no_gain_confirmed(), "阈值 3 时两轮还不算收敛")
            fourth = state.observe(round_index=3, claims=_claims(), evidence=[])
            self.assertEqual(third["no_gain_streak"], 2)
            self.assertEqual(fourth["no_gain_streak"], 3)
            self.assertTrue(state.no_gain_confirmed())
        finally:
            os.environ.pop("QA_GAP_NO_GAIN_ROUNDS", None)
    def test_new_evidence_count_is_self_computed(self):
        state = _state()
        first = state.observe(round_index=0, claims=_claims(),
                              evidence=[evidence_item("article:1")])
        second = state.observe(round_index=1, claims=_claims(),
                               evidence=[evidence_item("article:1")])
        third = state.observe(round_index=2, claims=_claims(),
                              evidence=[evidence_item("article:1"),
                                        evidence_item("article:2")])
        self.assertEqual(first["new_evidence"], 1)
        self.assertEqual(second["new_evidence"], 0, "同一批证据不算新证据")
        self.assertEqual(third["new_evidence"], 1)


class StopReasonTests(unittest.TestCase):
    def test_all_five_values_are_reachable(self):
        """Phase 05 明确留给 Phase 07 的账：五值必须真的能被产出（不是枚举里躺着）。"""
        produced = {
            gap.decide_stop_reason(no_gain_streak=0, no_gain_threshold=2, high_priority_open=0,
                                   unresolved_contradictions=0, actionable_hops=0,
                                   depth_exhausted=True, budget_exhausted=False)["stop_reason"],
            gap.decide_stop_reason(no_gain_streak=0, no_gain_threshold=2, high_priority_open=2,
                                   unresolved_contradictions=0, actionable_hops=0,
                                   depth_exhausted=False, budget_exhausted=True)["stop_reason"],
            gap.decide_stop_reason(no_gain_streak=0, no_gain_threshold=2, high_priority_open=2,
                                   unresolved_contradictions=0, actionable_hops=0,
                                   depth_exhausted=True, budget_exhausted=False)["stop_reason"],
            gap.decide_stop_reason(no_gain_streak=2, no_gain_threshold=2, high_priority_open=2,
                                   unresolved_contradictions=0, actionable_hops=0,
                                   depth_exhausted=True, budget_exhausted=False)["stop_reason"],
            gap.decide_stop_reason(no_gain_streak=0, no_gain_threshold=2, high_priority_open=1,
                                   unresolved_contradictions=1, actionable_hops=0,
                                   depth_exhausted=True, budget_exhausted=False)["stop_reason"],
        }
        self.assertEqual(produced, set(contracts.QA_STOP_REASONS),
                         "五值必须齐全：%s" % sorted(produced))

    def test_loop_continues_while_hops_remain(self):
        decision = gap.decide_stop_reason(no_gain_streak=0, no_gain_threshold=2,
                                          high_priority_open=3, unresolved_contradictions=0,
                                          actionable_hops=1, depth_exhausted=False,
                                          budget_exhausted=False)
        self.assertEqual(decision["stop_reason"], "")
        self.assertIn("继续", decision["detail"])

    def test_unresolvable_contradiction_needs_both_conditions(self):
        # 有未消解矛盾但还有下一跳 → 继续（先去搜，不许提前认输）
        self.assertEqual(gap.decide_stop_reason(
            no_gain_streak=0, no_gain_threshold=2, high_priority_open=1,
            unresolved_contradictions=1, actionable_hops=1, depth_exhausted=False,
            budget_exhausted=False)["stop_reason"], "")
        # 无矛盾 → 不许写这个原因
        self.assertNotEqual(gap.decide_stop_reason(
            no_gain_streak=0, no_gain_threshold=2, high_priority_open=1,
            unresolved_contradictions=0, actionable_hops=0, depth_exhausted=True,
            budget_exhausted=False)["stop_reason"],
            contracts.QA_STOP_UNRESOLVABLE_CONTRADICTION)

    def test_precedence_is_specific_before_generic(self):
        # 预算与矛盾同时存在 → 矛盾更具体（§15 要求明说"保留不确定性"，不是"钱花完了"）
        decision = gap.decide_stop_reason(no_gain_streak=3, no_gain_threshold=2,
                                          high_priority_open=2, unresolved_contradictions=1,
                                          actionable_hops=0, depth_exhausted=True,
                                          budget_exhausted=True)
        self.assertEqual(decision["stop_reason"], contracts.QA_STOP_UNRESOLVABLE_CONTRADICTION)
        # 无矛盾时预算优先于 no-gain（停止的真实原因就是预算）
        decision = gap.decide_stop_reason(no_gain_streak=3, no_gain_threshold=2,
                                          high_priority_open=2, unresolved_contradictions=0,
                                          actionable_hops=0, depth_exhausted=True,
                                          budget_exhausted=True)
        self.assertEqual(decision["stop_reason"], contracts.QA_STOP_BUDGET_EXHAUSTED)
        self.assertTrue(decision["factors"]["budget_exhausted"])


class UnresolvedContradictionTests(unittest.TestCase):
    def _layer(self, *, resolution="unresolved", claims=None):
        claim = graph_claim("c1", text=CLAIM_TEXT, verified_support_count=1,
                            independent_sources=1, verified_support_mass=0.8)
        decision = contradiction("x1", ["c1"], resolution=resolution)
        return {"graph_version": "qa-evidence-graph-v1", "nodes": [], "edges": [],
                "claims": list(claims if claims is not None else [claim]),
                "contradictions": [decision],
                "coverage": {"claim_coverage": 1.0, "evidence_coverage": 1.0},
                "stats": {}}

    def test_unresolved_decision_becomes_the_stop_reason(self):
        review = gap.review_graph(self._layer(), graph={"evidence": []}, plan=plan())
        self.assertEqual(review["stop_reason"], contracts.QA_STOP_UNRESOLVABLE_CONTRADICTION)
        self.assertEqual(review["stats"]["unresolved_contradictions"], 1)
        self.assertEqual(review["unresolved_contradictions"][0]["reason_code"],
                         "NO_DECISIVE_RULE")
        self.assertIn("evidence_graph_review", review["stop_source"])

    def test_resolved_decision_does_not_claim_unresolvable(self):
        review = gap.review_graph(self._layer(resolution="resolved"), graph={"evidence": []},
                                  plan=plan())
        self.assertNotEqual(review["stop_reason"], contracts.QA_STOP_UNRESOLVABLE_CONTRADICTION)

    def test_review_carries_the_loop_stop_reason_when_nothing_changed(self):
        """仍有高优缺口、又没有新矛盾 → 沿用多跳循环已经给出的停止原因（不两处各判一套）。"""
        starved = graph_claim("c2", text="另一条没有任何证据的结论", scope=())
        review = gap.review_graph(self._layer(resolution="resolved", claims=[starved]),
                                  graph={"evidence": []}, plan=plan(),
                                  previous_stop_reason=contracts.QA_STOP_NO_GAIN)
        self.assertEqual(review["stop_reason"], contracts.QA_STOP_NO_GAIN)
        self.assertEqual(review["stop_source"], "carried_from_gap_loop")
        self.assertGreaterEqual(review["stats"]["high_priority_open"], 1)

    def test_review_upgrades_to_answerable_when_gaps_are_gone(self):
        claim = graph_claim("c1", text=CLAIM_TEXT, verified_support_count=2,
                            independent_sources=2, verified_support_mass=0.95)
        layer = {"graph_version": "qa-evidence-graph-v1", "nodes": [], "edges": [],
                 "claims": [claim], "contradictions": [],
                 "coverage": {"claim_coverage": 1.0, "evidence_coverage": 1.0}, "stats": {}}
        review = gap.review_graph(layer, graph={"evidence": []}, plan=plan())
        self.assertEqual(review["stop_reason"], contracts.QA_STOP_ANSWERABLE)

    def test_state_counts_unresolved_once(self):
        state = _state()
        layer_decision = contradiction("x1", ["c1"])
        state.observe(round_index=0, claims=_claims(), evidence=[],
                      contradictions=[layer_decision])
        self.assertEqual(state.unresolved_contradictions(), 1)
        state.observe(round_index=1, claims=_claims(), evidence=[],
                      contradictions=[layer_decision, layer_decision])
        self.assertEqual(state.unresolved_contradictions(), 1, "同一条矛盾不许重复计数")


class ReceiptTests(unittest.TestCase):
    def test_receipt_passes_the_gap_loop_contract(self):
        state = _state()
        state.observe(round_index=0, claims=_claims(), evidence=[])
        state.next_hops(plan=plan())
        state.finalize(depth_exhausted=True)
        receipt = state.receipt()
        ok, note = validate("gap_loop", receipt)
        self.assertTrue(ok, note)
        self.assertEqual(receipt["analyzer_version"], contracts.GAP_ANALYZER_VERSION)
        self.assertEqual(receipt["no_gain_rounds"], 2)
        for gap_item in receipt["gaps"]:
            ok, note = validate("gap", gap_item)
            self.assertTrue(ok, note)
        for hop in receipt["hops"]:
            ok, note = validate("next_hop", hop)
            self.assertTrue(ok, note)

    def test_round_receipt_passes_the_round_contract(self):
        state = _state()
        state.observe(round_index=0, claims=_claims(), evidence=[])
        ok, note = validate("gap_loop_round", state.rounds[0])
        self.assertTrue(ok, note)

    def test_receipt_records_the_stop_reason_on_the_last_round(self):
        state = _state()
        state.observe(round_index=0, claims=_claims(), evidence=[])
        state.observe(round_index=1, claims=_claims(), evidence=[])
        state.finalize(depth_exhausted=True)
        self.assertEqual(state.rounds[-1]["stop_reason"], state.receipt()["stop_reason"])

    def test_budget_stop_is_reported_honestly(self):
        state = _state(budget_seconds=0.0)
        state.observe(round_index=0, claims=_claims(), evidence=[])
        decision = state.finalize(budget_exhausted=True, depth_exhausted=False)
        self.assertEqual(decision["stop_reason"], contracts.QA_STOP_BUDGET_EXHAUSTED)

    def test_gap_summary_aggregates_distributions(self):
        state = _state()
        state.observe(round_index=0, claims=_claims(), evidence=[])
        state.observe(round_index=1, claims=_claims(), evidence=[])
        state.observe(round_index=2, claims=_claims(), evidence=[])
        state.finalize(depth_exhausted=True)
        summary = gap.gap_summary([state.receipt()])
        self.assertEqual(summary["receipts"], 1)
        self.assertEqual(summary["rounds_total"], 3)
        self.assertEqual(summary["stop_reason_distribution"],
                         {contracts.QA_STOP_NO_GAIN: 1})
        self.assertEqual(summary["stop_reasons_produced"], [contracts.QA_STOP_NO_GAIN])
        self.assertGreaterEqual(summary["gaps_total"], 1)
        self.assertGreaterEqual(summary["no_gain_rounds"], 1)


if __name__ == "__main__":
    unittest.main()

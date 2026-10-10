#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 08 · P08-04（counter-evidence reservation）用例。

钉住（§4 的 `counter_evidence` 段 + §5 的预算竞争）：
  1. 反证有**专属预留额度**：`reserved = floor(证据预算 × 比例)`，比例可配、可回滚到 0；
  2. 预留**真的在起作用**：同一份数据下，比例 0 时低效用的反证会被预算裁掉，
     比例 > 0 时它进包 —— 这就是"支持性证据不得挤占反证"的机器可校验形态；
  3. 预留**花不完就还回去**：反证总额小于预留时，剩余额度被一般候选用掉（不浪费 token），
     并在 `counter_evidence_unfilled*` 里如实记账；
  4. 预留**装不下的反证回到一般池竞争**，不被静默吞掉（有一条一条可查的 trace）；
  5. 反证身份仍然来自 Phase 06 的 REFUTES 关系（本阶段不另判反证）。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_context_pack as cp  # noqa: E402
from qa_phase08_fixtures import (  # noqa: E402
    QUESTION, evidence_item, graph, graph_claim, graph_edge, plan, verification,
)

COUNTER_TEXT = "有研究者持不同意见，认为影响被高估。" * 6
# 支持性证据刻意做成**短句**（span ≈ 31 token）：它们才真的会跟反证抢预算。
# 若写成 300+ 字的长材料，span 会顶到 Phase 02 的 320 字上限、谁都装不下，
# 预算竞争就退化成"只有反证装得下"——那样的对照证明不了预留的作用（实测踩到）。
SUPPORTER_TEXT = "香港家族办公室税收优惠政策对内地高净值客户的申报义务影响要点。"


def _graph_with_counter(*, supporters=40):
    """1 条结论 + 多条**高效用**支持证据 + 1 条**低效用**反证（时间旧、权威低、词面弱）。"""
    evidence, edges = [], []
    for index in range(supporters):
        ref = "article:%d" % (index + 1)
        evidence.append(evidence_item(
            ref, text=SUPPORTER_TEXT, authority=95, published="2026-10-09",
            verification=verification("SUPPORTED", score=0.95)))
        edges.append(graph_edge("c1", ref, relation="SUPPORTS", authority=95))
    counter = evidence_item("article:99", text=COUNTER_TEXT, authority=5,
                            published="2019-01-01",
                            verification=verification("REFUTED", score=0.4))
    evidence.append(counter)
    edges.append(graph_edge("c1", "article:99", relation="REFUTES", status="REFUTED",
                            verified=True, authority=5))
    claims = [graph_claim("c1", text="香港家族办公室税收优惠政策对内地高净值客户有影响",
                          refs=[item["evidence_ref"] for item in evidence[:-1]])]
    return graph(claims=claims, evidence=evidence, edges=edges)


def _pack(*, ratio, budget=500, supporters=40):
    return cp.build_context_pack(
        graph=_graph_with_counter(supporters=supporters), plan=plan(),
        request={"question": QUESTION, "mode": "standard"}, run_id="r1",
        budget_tokens=budget, reserve_ratio=ratio)


def _counter_rows(pack):
    return [item for item in pack["items"] if item["kind"] == "counter_evidence"]


def _trace_row(pack, item_id):
    return [row for row in pack["selection_trace"] if row["item_id"] == item_id][0]


def _counter_item_id(pack):
    """反证候选的 item_id（即使它没进包，也要能从 trace 里按 section 找到）。"""
    rows = [row for row in pack["selection_trace"] if row["section"] == "counter_evidence"]
    return rows[0]["item_id"]


class ReservationTests(unittest.TestCase):
    def test_reserve_budget_is_reported(self):
        pack = _pack(ratio=0.5)
        self.assertEqual(pack["budget"]["reserved_counter_evidence"], 250)
        self.assertGreater(pack["budget"]["counter_evidence_used"], 0)

    def test_reservation_is_what_keeps_the_counter_evidence(self):
        """同一份数据：比例 0 → 反证被预算裁掉；比例 > 0 → 反证进包。"""
        without = _pack(ratio=0.0)
        with_reserve = _pack(ratio=0.5)
        self.assertEqual(_counter_rows(without), [],
                         "比例 0 时反证不该进包（否则这个对照没有意义）")
        self.assertTrue(_counter_rows(with_reserve), "有预留时反证必须在包里")
        self.assertEqual(_counter_rows(with_reserve)[0]["evidence_ref"], "article:99")
        row = _trace_row(with_reserve, _counter_rows(with_reserve)[0]["item_id"])
        self.assertEqual(row["reason"], "COUNTER_EVIDENCE_RESERVED")
        self.assertTrue(row["reserved"])

    def test_lower_value_counter_evidence_beats_a_higher_value_supporter(self):
        """预留的语义：**效用更低**的反证也能进，而效用更高的支持证据反而被裁。"""
        pack = _pack(ratio=0.5)
        counter = _counter_rows(pack)[0]
        counter_utility = _trace_row(pack, counter["item_id"])["utility"]
        excluded = [row for row in pack["selection_trace"] if row["decision"] == "excluded"]
        better_but_dropped = [row for row in excluded
                              if row["utility"] > counter_utility
                              and row["section"] == "evidence_context"]
        self.assertTrue(better_but_dropped,
                        "必须有'效用更高却被裁掉'的支持证据，才能证明预留真的在保护反证")

    def test_ratio_zero_disables_the_reservation(self):
        pack = _pack(ratio=0.0)
        self.assertEqual(pack["budget"]["reserved_counter_evidence"], 0)
        self.assertEqual(pack["budget"]["counter_evidence_used"], 0)

    def test_over_flow_reserve_returns_the_counter_to_the_general_pool(self):
        """预留额度比反证还小：反证必须回到一般池竞争，并且**留下非预留的 trace**。"""
        pack = _pack(ratio=0.02)
        self.assertEqual(pack["budget"]["reserved_counter_evidence"], 10)
        self.assertEqual(pack["budget"]["counter_evidence_overflow"], 1)
        row = _trace_row(pack, _counter_item_id(pack))
        self.assertFalse(row["reserved"], "超出预留的条目按一般候选记账")
        self.assertIn(row["reason"], ("OVER_TOKEN_BUDGET", "TOP_UTILITY", "DIVERSITY_BONUS",
                                      "DUPLICATE_IDENTITY", "LOW_UTILITY"))
        self.assertFalse(_counter_rows(pack), "这个预算下它竞争不过支持性证据（如实裁掉）")

    def test_every_candidate_has_exactly_one_trace_row(self):
        """每个候选恰好一条决策：trace 行数 == 候选数（可复算的对账口径）。"""
        pack = _pack(ratio=0.5)
        self.assertEqual(len(pack["selection_trace"]), pack["budget"]["candidates"])
        self.assertEqual(len({row["item_id"] for row in pack["selection_trace"]}),
                         len(pack["selection_trace"]))
        traced = {row["item_id"] for row in pack["selection_trace"]}
        for item in pack["items"]:
            if item["section"] == "budget":
                continue    # 预算回执条目是裁剪结果本身，不参与竞争、也不该有决策留痕
            self.assertIn(item["item_id"], traced, "包里的每一条都必须有入选留痕")

    def test_unfilled_reserve_returns_to_the_pool(self):
        pack = _pack(ratio=0.9, budget=4000, supporters=4)
        budget = pack["budget"]
        self.assertGreater(budget["counter_evidence_unfilled"], 0,
                           "反证总量小于预留时必须如实记账")
        self.assertTrue(budget["counter_reserve_unfilled_reason"])
        self.assertGreater(budget["used"], budget["counter_evidence_used"],
                           "剩余额度必须被一般候选用掉（不浪费 token）")

    def test_all_evidence_is_accounted_for_in_the_trace(self):
        """每个候选恰好一条决策：trace 行数 == 候选数（可复算的对账口径）。"""
        pack = _pack(ratio=0.5)
        self.assertEqual(len(pack["selection_trace"]), pack["budget"]["candidates"])
        self.assertEqual(len({row["item_id"] for row in pack["selection_trace"]}),
                         len(pack["selection_trace"]))

    def test_counter_evidence_identity_comes_from_phase06_relations(self):
        graph_obj = _graph_with_counter(supporters=2)
        cg = cp.build_context_graph(graph=graph_obj, plan=plan(), run_id="r1")
        counter = [item for item in cg["items"] if item["kind"] == "counter_evidence"]
        supporters = [item for item in cg["items"] if item["kind"] == "evidence"]
        self.assertEqual([item["evidence_ref"] for item in counter], ["article:99"])
        self.assertEqual(len(supporters), 2)

    def test_section_and_grounding_of_counter_items(self):
        pack = _pack(ratio=0.5)
        for item in _counter_rows(pack):
            self.assertEqual(item["section"], "counter_evidence")
            self.assertTrue(item["grounding"]["grounded"])
            self.assertTrue(item["grounding"]["span"]["quote"])


if __name__ == "__main__":
    unittest.main()

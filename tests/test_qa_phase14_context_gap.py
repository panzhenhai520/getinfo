#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 14 · P14-05（Context Gap no-repeat）用例。

钉住 §13 的关键收益原话：
  "若信息已在 Evidence Graph 中但只是 Context Gap，系统应**重组上下文而不是再次询问患者**，
   从而减少无意义追问。"

做法：喂**真实的 Phase 08 Context Gap**（`qa_context_pack.build_context_pack` 的产物，
不是手写 dict），断言：
  1. 该缺口上的**六个动作全部被拦下**，理由码逐条可查（`CONTEXT_GAP_NO_REPEAT`）；
  2. 拦下时给出 Phase 08 的**上下文动作**（`REPACK_CONTEXT` / `EXPAND_EVIDENCE_SPAN` /
     `LOAD_COUNTEREVIDENCE`，四个冻结动作之内），说明"该重组而不是再拿"；
  3. 缺口环里**没有上下文包**时，用本轮已获取证据池做保守判据（词面覆盖 ≥ Phase 07 的
     相关性下界 → 判"已在图上"）；证据不覆盖时**绝不误拦**；
  4. Phase 08 的 `LOAD_SKILL`（能力缺口，不属"信息已在手上"）**不抑制**；
  5. 同一个问诊问题**不重复问**（内容寻址的问诊 id）。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_acquisition as ac  # noqa: E402
import qa_context_pack as cp  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_phase08_fixtures as fx08  # noqa: E402
import qa_phase14_fixtures as fx  # noqa: E402

ALL_SOURCES = {"kb": True, "emr": True, "db": True, "graph": True, "web": True, "patient": True}
PERMISSIONS = {"patient_contact": True, "emr_read": True}
SUPPORTER_TEXT = "香港家族办公室税收优惠政策对内地高净值客户的申报义务影响要点。"
COUNTER_TEXT = "有研究者持不同意见，认为影响被高估。" * 6
SHORT_SPAN_TEXT = ("家族办公室税收优惠。\n"
                   + "背景补充材料，与本次问题没有直接关系。" * 20)


def _many_supporters(count=40):
    evidence, edges = [], []
    for index in range(count):
        ref = "article:%d" % (index + 1)
        evidence.append(fx08.evidence_item(ref, text=SUPPORTER_TEXT, authority=95,
                                           published="2026-10-09",
                                           verification=fx08.verification("SUPPORTED", score=0.95)))
        edges.append(fx08.graph_edge("c1", ref))
    claims = [fx08.graph_claim("c1", text="香港家族办公室税收优惠政策对内地高净值客户有影响",
                               refs=[item["evidence_ref"] for item in evidence])]
    return fx08.graph(claims=claims, evidence=evidence, edges=edges)


def _real_pack(which):
    if which == "OMITTED_EVIDENCE":
        return cp.build_context_pack(graph=_many_supporters(), plan=fx08.plan(),
                                     request={"question": fx08.QUESTION}, run_id="r1",
                                     budget_tokens=500, reserve_ratio=0.2)
    if which == "TRUNCATED_SPAN":
        evidence = [fx08.evidence_item("article:1", text=SHORT_SPAN_TEXT,
                                       verification=fx08.verification("SUPPORTED"))]
        claims = [fx08.graph_claim("c1", text="家族办公室税收优惠的适用范围", refs=["article:1"])]
        return cp.build_context_pack(
            graph=fx08.graph(claims=claims, evidence=evidence,
                             edges=[fx08.graph_edge("c1", "article:1")]),
            plan=fx08.plan(), request={"question": fx08.QUESTION}, run_id="r1")
    if which == "MISSING_COUNTEREVIDENCE":
        evidence = [fx08.evidence_item("article:%d" % (index + 1), text=SUPPORTER_TEXT,
                                       authority=95, published="2026-10-09",
                                       verification=fx08.verification("SUPPORTED", score=0.95))
                    for index in range(40)]
        evidence.append(fx08.evidence_item("article:99", text=COUNTER_TEXT, authority=5,
                                           published="2019-01-01",
                                           verification=fx08.verification("REFUTED", score=0.4)))
        edges = [fx08.graph_edge("c1", "article:%d" % (index + 1)) for index in range(40)]
        edges.append(fx08.graph_edge("c1", "article:99", relation="REFUTES", status="REFUTED"))
        claims = [fx08.graph_claim("c1", text="香港家族办公室税收优惠政策对内地高净值客户有影响",
                                   refs=["article:1"])]
        return cp.build_context_pack(graph=fx08.graph(claims=claims, evidence=evidence,
                                                      edges=edges),
                                     plan=fx08.plan(), request={"question": fx08.QUESTION},
                                     run_id="r1", budget_tokens=500, reserve_ratio=0.0)
    raise AssertionError("unknown pack: %s" % which)


class RealContextGapTests(unittest.TestCase):
    def _gap_of(self, pack, kind):
        return [row for row in pack["context_gaps"] if row["context_gap_type"] == kind]

    def test_phase08_context_gaps_are_real_and_do_not_request_retrieval(self):
        pack = _real_pack("OMITTED_EVIDENCE")
        rows = self._gap_of(pack, "OMITTED_EVIDENCE")
        self.assertTrue(rows, "样例必须真的产出 Context Gap，否则这条守卫是空转")
        for row in rows:
            self.assertIs(row["requires_retrieval"], False)
            self.assertIn(row["action"], contracts.CONTEXT_GAP_ACTIONS)

    def test_omitted_evidence_suppresses_every_action(self):
        pack = _real_pack("OMITTED_EVIDENCE")
        plan = ac.plan_acquisition([fx.gap(claim_id="c1", missing="LOW_RELEVANCE")],
                                   sources=ALL_SOURCES, permissions=PERMISSIONS,
                                   context_gaps=pack["context_gaps"])
        self.assertEqual(plan["selected"], [], "信息已在图上：不该再拿，更不该再问人")
        self.assertEqual(plan["stats"]["no_repeat_suppressed"], 6)
        self.assertEqual(plan["skipped"], {"CONTEXT_GAP_NO_REPEAT": 6})
        no_repeat = plan["no_repeat"]
        self.assertEqual(no_repeat["suppressed_by_reason"], {"EVIDENCE_ALREADY_IN_GRAPH": 6})
        self.assertEqual(no_repeat["context_actions"], ["REPACK_CONTEXT"])
        for action in ("retrieve_document", "retrieve_emr", "graph_traverse",
                       "query_database", "web_search", "ask_patient"):
            self.assertEqual(no_repeat["suppressed_actions"].get(action), 1,
                             "%s 必须也被拦下（否则会重复拿一遍已有证据）" % action)
        ok, note = contracts.validate("context_gap_no_repeat", no_repeat)
        self.assertTrue(ok, note)

    def test_truncated_span_suppresses_with_its_own_reason(self):
        pack = _real_pack("TRUNCATED_SPAN")
        plan = ac.plan_acquisition([fx.gap(claim_id="c1", missing="LOW_RELEVANCE")],
                                   sources=ALL_SOURCES, permissions=PERMISSIONS,
                                   context_gaps=pack["context_gaps"])
        self.assertEqual(plan["no_repeat"]["suppressed_by_reason"], {"SPAN_TRUNCATED_ONLY": 6})
        self.assertEqual(plan["no_repeat"]["context_actions"], ["EXPAND_EVIDENCE_SPAN"])

    def test_missing_counterevidence_suppresses_and_points_at_loading(self):
        pack = _real_pack("MISSING_COUNTEREVIDENCE")
        plan = ac.plan_acquisition([fx.gap(claim_id="c1", missing="MISSING_COUNTEREVIDENCE")],
                                   sources=ALL_SOURCES, permissions=PERMISSIONS,
                                   context_gaps=pack["context_gaps"])
        # 同一 claim 上可能同时有多条 Context Gap（省略 + 缺反证）：主理由逐条计数，
        # 全部命中理由在 `reason_hits` 里可查（不许只报第一条就说完了）
        self.assertEqual(plan["skipped"], {"CONTEXT_GAP_NO_REPEAT": 6})
        self.assertIn("COUNTEREVIDENCE_ALREADY_IN_GRAPH", plan["no_repeat"]["reason_hits"])
        self.assertIn("LOAD_COUNTEREVIDENCE", plan["no_repeat"]["context_actions"])
        for action in plan["no_repeat"]["context_actions"]:
            self.assertIn(action, contracts.CONTEXT_GAP_ACTIONS,
                          "上下文动作只能是 Phase 08 冻结的四个之一")

    def test_skill_gaps_do_not_suppress_anything(self):
        """Phase 08 的 LOAD_SKILL 是"能力不在手上"，不是"信息已在手上" → 不许拦。"""
        pack = _real_pack("OMITTED_EVIDENCE")
        skill_gaps = [{"gap_id": "CG-skill", "claim_id": "c1",
                       "context_gap_type": "SKILL_NOT_AVAILABLE", "action": "LOAD_SKILL",
                       "requires_retrieval": False}]
        plan = ac.plan_acquisition([fx.gap(claim_id="c1", missing="LOW_RELEVANCE")],
                                   sources=ALL_SOURCES, permissions=PERMISSIONS,
                                   context_gaps=skill_gaps)
        self.assertEqual(plan["selected"], ["retrieve_document"],
                         "只有能力缺口时应当照常获取证据")
        self.assertEqual(plan["stats"]["no_repeat_suppressed"], 0)
        del pack

    def test_conflict_that_is_adjudicated_is_not_re_fetched(self):
        plan = ac.plan_acquisition([fx.gap(claim_id="c1", missing="CONTRADICTION")],
                                   sources=ALL_SOURCES, permissions=PERMISSIONS,
                                   resolved_claims=["c1"])
        self.assertEqual(plan["no_repeat"]["suppressed_by_reason"],
                         {"CONFLICT_IS_ADJUDICATED": 6})


class AcquiredPoolTests(unittest.TestCase):
    """缺口环里还没有上下文包时的保守判据：本轮**已获取**的证据是否已经覆盖这条结论。"""

    def _acquired(self, text=SUPPORTER_TEXT):
        return [fx08.evidence_item("article:1", text=text,
                                   verification=fx08.verification("SUPPORTED"))]

    def test_covered_claim_is_suppressed(self):
        gap = fx.gap(claim_id="c1", missing="LOW_RELEVANCE",
                     claim_text="香港家族办公室税收优惠政策对内地高净值客户的申报义务影响")
        signal = ac.context_gap_signal(gap, acquired_evidence=self._acquired())
        self.assertEqual(signal["reason"], "EVIDENCE_ALREADY_IN_GRAPH")
        self.assertEqual(signal["basis"], "ACQUIRED_POOL")
        self.assertEqual(signal["context_action"], "REPACK_CONTEXT")
        plan = ac.plan_acquisition([gap], sources=ALL_SOURCES, permissions=PERMISSIONS,
                                   acquired_evidence=self._acquired())
        self.assertEqual(plan["selected"], [])

    def test_unrelated_claim_is_not_suppressed(self):
        gap = fx.gap(claim_id="c1", missing="LOW_RELEVANCE",
                     claim_text="完全无关的另一件事：南极科考站的供电方案与预算安排")
        signal = ac.context_gap_signal(gap, acquired_evidence=self._acquired())
        self.assertEqual(signal["reason"], "", "证据不覆盖 → 绝不误拦（宁可多搜一次）")
        plan = ac.plan_acquisition([gap], sources=ALL_SOURCES, permissions=PERMISSIONS,
                                   acquired_evidence=self._acquired())
        self.assertEqual(plan["selected"], ["retrieve_document"])

    def test_no_evidence_pool_means_no_suppression(self):
        gap = fx.gap(claim_id="c1", missing="LOW_RELEVANCE", claim_text=SUPPORTER_TEXT)
        self.assertEqual(ac.context_gap_signal(gap, acquired_evidence=[])["reason"], "")

    def test_only_already_held_types_are_checked(self):
        gap = fx.gap(claim_id="c1", missing="NO_EVIDENCE", claim_text=SUPPORTER_TEXT)
        self.assertEqual(ac.context_gap_signal(gap, acquired_evidence=self._acquired())["reason"],
                         "", "NO_EVIDENCE 是真缺事实：不许拿'另一条相关证据'当挡箭牌")


class NoRepeatReceiptTests(unittest.TestCase):
    def test_all_five_suppression_reasons_are_reachable(self):
        """五个抑制理由码都能从**真实 Phase 08 产物**里产出（没有一条是摆设）。"""
        seen: set = set()
        for which in ("OMITTED_EVIDENCE", "TRUNCATED_SPAN", "MISSING_COUNTEREVIDENCE"):
            pack = _real_pack(which)
            plan = ac.plan_acquisition([fx.gap(claim_id="c1", missing="LOW_RELEVANCE")],
                                       sources=ALL_SOURCES, permissions=PERMISSIONS,
                                       context_gaps=pack["context_gaps"])
            seen.update(plan["no_repeat"]["reason_hits"])
        # UNGROUNDED_CLAIM → EVIDENCE_ALREADY_IN_PACK（进了包但没有可回溯 span 的结论）
        evidence = [fx08.evidence_item("article:1", text="",
                                       verification=fx08.verification("SUPPORTED"))]
        claims = [fx08.graph_claim("c1", text="香港家族办公室税收优惠政策对内地高净值客户有影响",
                                   refs=["article:1"])]
        pack = cp.build_context_pack(
            graph=fx08.graph(claims=claims, evidence=evidence,
                             edges=[fx08.graph_edge("c1", "article:1")]),
            plan=fx08.plan(), request={"question": fx08.QUESTION}, run_id="r1")
        self.assertIn("UNGROUNDED_CLAIM",
                      {row["context_gap_type"] for row in pack["context_gaps"]})
        plan = ac.plan_acquisition([fx.gap(claim_id="c1", missing="LOW_RELEVANCE")],
                                   sources=ALL_SOURCES, permissions=PERMISSIONS,
                                   context_gaps=pack["context_gaps"])
        seen.update(plan["no_repeat"]["reason_hits"])
        # CONFLICT_IS_ADJUDICATED（Phase 06 已裁决的矛盾）
        plan = ac.plan_acquisition([fx.gap(claim_id="c1", missing="CONTRADICTION")],
                                   sources=ALL_SOURCES, permissions=PERMISSIONS,
                                   resolved_claims=["c1"])
        seen.update(plan["no_repeat"]["reason_hits"])
        self.assertEqual(seen, set(contracts.CONTEXT_GAP_NO_REPEAT_REASONS), seen)

    def test_empty_candidates_give_a_schema_valid_receipt(self):
        adjusted, receipt = ac.context_gap_no_repeat([], context_gaps=[])
        self.assertEqual(adjusted, [])
        ok, note = contracts.validate("context_gap_no_repeat", receipt)
        self.assertTrue(ok, note)
        self.assertEqual(receipt["suppressed"], 0)

    def test_suppression_only_touches_its_own_evidence(self):
        candidates, _ = ac.context_gap_no_repeat(
            [{"action": "retrieve_document", "decision": "selected",
              "gap": fx.gap(gap_id="G1", claim_id="c9")}],
            context_gaps=[{"gap_id": "CG1", "claim_id": "c1",
                           "context_gap_type": "OMITTED_EVIDENCE", "action": "REPACK_CONTEXT"}])
        self.assertEqual(candidates[0]["decision"], "selected",
                         "别的 claim 的 Context Gap 不许牵连这条缺口")


if __name__ == "__main__":
    unittest.main()

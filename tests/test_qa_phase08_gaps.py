#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 08 · P08-05（Context Gap）用例。

钉住 §6 的三句话：
  1. "证据已存在，但当前 Agent 没拿到" → `OMITTED_EVIDENCE`（动作 REPACK_CONTEXT）；
  2. "摘要不足" → `TRUNCATED_SPAN`（动作 EXPAND_EVIDENCE_SPAN）；
  3. "缺反证" → `MISSING_COUNTEREVIDENCE`（动作 LOAD_COUNTEREVIDENCE）；
  另加两条本仓库口径：`UNGROUNDED_CLAIM`（进了包但没有可回溯 span）、
  `DUPLICATE_SECTION`（同一来源占了两段预算）；
  以及 `SKILL_NOT_AVAILABLE`（§8 能力属 Phase 11，本阶段只能标注）。
  4. **Context Gap 默认不得触发昂贵新检索**（MASTER_RULES 第 13 条）：每一条
     `requires_retrieval` 恒为 False，且四个动作都在 §6 冻结枚举内；
  5. `gap_id` 内容寻址：同输入同 id（可与上一轮逐条比对"哪些缺口被解决了"）。
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
    QUESTION, evidence_item, graph, graph_claim, graph_edge, plan, verification,
)

SUPPORTER_TEXT = "香港家族办公室税收优惠政策对内地高净值客户的申报义务影响要点。"
COUNTER_TEXT = "有研究者持不同意见，认为影响被高估。" * 6
# 命中词在**很短的句子**里，正文却很长 → Phase 02 的最小 span 只有十来个字
SHORT_SPAN_TEXT = ("家族办公室税收优惠。\n"
                   + "背景补充材料，与本次问题没有直接关系。" * 20)


def _many_supporters(count=40):
    evidence, edges = [], []
    for index in range(count):
        ref = "article:%d" % (index + 1)
        evidence.append(evidence_item(ref, text=SUPPORTER_TEXT, authority=95,
                                      published="2026-10-09",
                                      verification=verification("SUPPORTED", score=0.95)))
        edges.append(graph_edge("c1", ref))
    claims = [graph_claim("c1", text="香港家族办公室税收优惠政策对内地高净值客户有影响",
                          refs=[item["evidence_ref"] for item in evidence])]
    return graph(claims=claims, evidence=evidence, edges=edges)


def _types(pack):
    return {gap["context_gap_type"] for gap in pack["context_gaps"]}


def _three_claims():
    """三条结论各带一条可回溯证据：用来触发 `citation_verification` 的 SKILL 缺口。"""
    claims, evidence, edges = [], [], []
    for index in range(3):
        cid = "c%d" % (index + 1)
        ref = "article:%d" % (index + 1)
        claims.append(graph_claim(cid, text="第%d条结论：税收优惠政策影响申报义务" % (index + 1),
                                  refs=[ref]))
        evidence.append(evidence_item(ref, text=SUPPORTER_TEXT, authority=95,
                                      published="2026-10-09",
                                      verification=verification("SUPPORTED", score=0.95)))
        edges.append(graph_edge(cid, ref))
    return graph(claims=claims, evidence=evidence, edges=edges)


class GapDetectionTests(unittest.TestCase):
    def test_omitted_evidence_is_reported_as_a_context_gap(self):
        pack = cp.build_context_pack(graph=_many_supporters(), plan=plan(),
                                     request={"question": QUESTION}, run_id="r1",
                                     budget_tokens=500, reserve_ratio=0.2)
        self.assertIn("OMITTED_EVIDENCE", _types(pack))
        gap = [item for item in pack["context_gaps"]
               if item["context_gap_type"] == "OMITTED_EVIDENCE"][0]
        self.assertEqual(gap["action"], "REPACK_CONTEXT")
        self.assertIs(gap["requires_retrieval"], False)
        self.assertGreater(gap["tokens_recoverable"], 0)
        ok, note = cp.validate_contract("context_gap", gap)
        self.assertTrue(ok, note)

    def test_truncated_span_is_reported_and_asks_to_expand_not_search(self):
        evidence = [evidence_item("article:1", text=SHORT_SPAN_TEXT,
                                  verification=verification("SUPPORTED"))]
        claims = [graph_claim("c1", text="家族办公室税收优惠的适用范围", refs=["article:1"])]
        pack = cp.build_context_pack(
            graph=graph(claims=claims, evidence=evidence,
                        edges=[graph_edge("c1", "article:1")]),
            plan=plan(), request={"question": QUESTION}, run_id="r1")
        self.assertIn("TRUNCATED_SPAN", _types(pack))
        gap = [item for item in pack["context_gaps"]
               if item["context_gap_type"] == "TRUNCATED_SPAN"][0]
        self.assertEqual(gap["action"], "EXPAND_EVIDENCE_SPAN")
        self.assertIs(gap["requires_retrieval"], False)
        item = [row for row in pack["items"] if row["evidence_ref"] == "article:1"][0]
        self.assertLess(item["grounding"]["span"]["chars"], cp.span_min_chars())
        self.assertGreater(item["metadata"]["full_chars"], item["grounding"]["span"]["chars"])

    def test_missing_counter_evidence_is_reported(self):
        evidence = [evidence_item("article:%d" % (index + 1), text=SUPPORTER_TEXT,
                                  authority=95, published="2026-10-09",
                                  verification=verification("SUPPORTED", score=0.95))
                    for index in range(40)]
        evidence.append(evidence_item("article:99", text=COUNTER_TEXT, authority=5,
                                      published="2019-01-01",
                                      verification=verification("REFUTED", score=0.4)))
        edges = [graph_edge("c1", "article:%d" % (index + 1)) for index in range(40)]
        edges.append(graph_edge("c1", "article:99", relation="REFUTES", status="REFUTED"))
        claims = [graph_claim("c1", text="香港家族办公室税收优惠政策对内地高净值客户有影响",
                              refs=["article:1"])]
        pack = cp.build_context_pack(
            graph=graph(claims=claims, evidence=evidence, edges=edges), plan=plan(),
            request={"question": QUESTION}, run_id="r1",
            budget_tokens=500, reserve_ratio=0.0)
        self.assertIn("MISSING_COUNTEREVIDENCE", _types(pack))
        gap = [item for item in pack["context_gaps"]
               if item["context_gap_type"] == "MISSING_COUNTEREVIDENCE"][0]
        self.assertEqual(gap["action"], "LOAD_COUNTEREVIDENCE")
        self.assertEqual(gap["evidence_ref"], "article:99")
        self.assertIs(gap["requires_retrieval"], False)

    def test_ungrounded_claim_is_reported(self):
        evidence = [evidence_item("article:1", text="", verification=verification("SUPPORTED"))]
        claims = [graph_claim("c1", text="香港家族办公室税收优惠政策对内地高净值客户有影响",
                              refs=["article:1"])]
        pack = cp.build_context_pack(
            graph=graph(claims=claims, evidence=evidence,
                        edges=[graph_edge("c1", "article:1")]),
            plan=plan(), request={"question": QUESTION}, run_id="r1")
        self.assertIn("UNGROUNDED_CLAIM", _types(pack))
        gap = [item for item in pack["context_gaps"]
               if item["context_gap_type"] == "UNGROUNDED_CLAIM"][0]
        self.assertEqual(gap["action"], "REPACK_CONTEXT")
        self.assertEqual(gap["claim_id"], "c1")
        excluded = [row for row in pack["selection_trace"] if row["reason"] == "NO_GROUNDING_SPAN"]
        self.assertTrue(excluded, "拿不出 span 的证据必须被挡在包外并留痕")
        self.assertNotIn(evidence[0]["evidence_ref"],
                         {item.get("evidence_ref") for item in pack["items"]})

    def test_duplicate_section_is_reported(self):
        """同一份证据同时占了两个段落 → 建议重组（这条用直接构造的包验证口径本身）。"""
        shared = cp.make_context_item(
            kind="evidence", section="evidence_context", text="同一段引文",
            evidence_ref="article:7", grounding={"grounded": True, "evidence_ref": "article:7",
                                                 "span": {"quote": "同一段引文"}})
        duplicate = cp.make_context_item(
            kind="counter_evidence", section="counter_evidence", text="同一段引文",
            evidence_ref="article:7", grounding={"grounded": True, "evidence_ref": "article:7",
                                                 "span": {"quote": "同一段引文"}})
        pack = {"pack_id": "CPtest", "items": [shared, duplicate], "citation_map": {},
                "sections": {}}
        gaps = cp.detect_context_gaps(pack=pack, graph={})
        kinds = {gap["context_gap_type"] for gap in gaps}
        self.assertIn("DUPLICATE_SECTION", kinds)
        gap = [item for item in gaps if item["context_gap_type"] == "DUPLICATE_SECTION"][0]
        self.assertEqual(gap["action"], "REPACK_CONTEXT")
        self.assertEqual(gap["evidence_ref"], "article:7")

    def test_skill_gap_is_labelled_not_faked(self):
        pack = cp.build_context_pack(graph=_three_claims(), plan=plan(),
                                     request={"question": QUESTION}, run_id="r1")
        self.assertIn("SKILL_NOT_AVAILABLE", _types(pack))
        gap = [item for item in pack["context_gaps"]
               if item["context_gap_type"] == "SKILL_NOT_AVAILABLE"][0]
        self.assertEqual(gap["action"], "LOAD_SKILL")
        self.assertEqual(gap["section"], "skill_context")
        self.assertIn("Phase 11", gap["detail"])
        self.assertIs(gap["requires_retrieval"], False)


class GapActionCoverageTests(unittest.TestCase):
    def test_all_four_spec_actions_are_reachable(self):
        actions = set()
        corpora = [
            dict(graph=_many_supporters(), budget_tokens=500),      # → REPACK_CONTEXT
            dict(graph=graph(claims=[graph_claim("c1", refs=["article:1"])],
                             evidence=[evidence_item("article:1", text=SHORT_SPAN_TEXT,
                                                     verification=verification("SUPPORTED"))],
                             edges=[graph_edge("c1", "article:1")]), budget_tokens=6000),
            dict(graph=_three_claims(), budget_tokens=6000),        # → LOAD_SKILL
        ]
        for kwargs in corpora:
            pack = cp.build_context_pack(plan=plan(), request={"question": QUESTION},
                                         run_id="r1", **kwargs)
            actions.update(gap["action"] for gap in pack["context_gaps"])
        # LOAD_COUNTEREVIDENCE 走反证被裁的那条路径（比例 0 + 紧预算）
        evidence = [evidence_item("article:%d" % (index + 1), text=SUPPORTER_TEXT,
                                  authority=95, published="2026-10-09",
                                  verification=verification("SUPPORTED", score=0.95))
                    for index in range(40)]
        evidence.append(evidence_item("article:99", text=COUNTER_TEXT, authority=5,
                                      published="2019-01-01",
                                      verification=verification("REFUTED", score=0.4)))
        edges = [graph_edge("c1", "article:%d" % (index + 1)) for index in range(40)]
        edges.append(graph_edge("c1", "article:99", relation="REFUTES", status="REFUTED"))
        pack = cp.build_context_pack(
            graph=graph(claims=[graph_claim("c1", text="税收优惠政策的影响", refs=["article:1"])],
                        evidence=evidence, edges=edges),
            plan=plan(), request={"question": QUESTION}, run_id="r1",
            budget_tokens=500, reserve_ratio=0.0)
        actions.update(gap["action"] for gap in pack["context_gaps"])
        self.assertEqual(actions, set(contracts.CONTEXT_GAP_ACTIONS),
                         "§6 的四个动作必须都能被真实产出：%s" % actions)

    def test_every_gap_is_inside_the_frozen_enums_and_never_requests_retrieval(self):
        pack = cp.build_context_pack(graph=_many_supporters(), plan=plan(),
                                     request={"question": QUESTION}, run_id="r1",
                                     budget_tokens=500)
        self.assertTrue(pack["context_gaps"])
        for gap in pack["context_gaps"]:
            self.assertIn(gap["context_gap_type"], contracts.CONTEXT_GAP_TYPES)
            self.assertIn(gap["action"], contracts.CONTEXT_GAP_ACTIONS)
            self.assertIs(gap["requires_retrieval"], False)
        self.assertEqual(pack["stats"]["retrieval_requested"], 0)
        receipt = cp.context_pack_receipt(pack)
        self.assertEqual(receipt["retrieval_requested"], 0)

    def test_gap_ids_are_content_addressed(self):
        first = cp.build_context_pack(graph=_many_supporters(), plan=plan(),
                                      request={"question": QUESTION}, run_id="r1",
                                      budget_tokens=500)
        second = cp.build_context_pack(graph=_many_supporters(), plan=plan(),
                                       request={"question": QUESTION}, run_id="r1",
                                       budget_tokens=500)
        self.assertEqual([gap["gap_id"] for gap in first["context_gaps"]],
                         [gap["gap_id"] for gap in second["context_gaps"]])
        for gap in first["context_gaps"]:
            self.assertTrue(gap["gap_id"].startswith("CG"))

    def test_gaps_are_capped(self):
        pack = cp.build_context_pack(graph=_many_supporters(count=60), plan=plan(),
                                     request={"question": QUESTION}, run_id="r1",
                                     budget_tokens=400)
        self.assertLessEqual(len(pack["context_gaps"]), cp.DEFAULT_MAX_GAPS)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 11 · P11-05（on-demand instruction）用例。

钉住：
  1. **按需**：没选中就没有指令（宁缺勿造）；选了几个就有几条，顺序 = 选择顺序；
  2. **技能不是证据**（MASTER_RULES 第 11 条）：每条指令恒 `is_evidence=False`、
     `requires_revalidation=True`、`in_citation_map=False`，正文带 `SKILL_HINT_MARK` 前缀，
     `grounding.grounded` 恒 False；
  3. **不进 citation_map**：`skill_context` 段的条目一个都不出现在包里的引用索引里；
  4. **确定性**：指令是模板拼装，同输入同输出（连 item_id 都相同）；
  5. **与 Phase 08 对齐**：`skill_context` 段从"空段 + deferred_to"变成
     `implemented_by` + `loaded_skills` + hint 政策；没给候选时逐字回到 Phase 08 行为；
  6. **接上 `LOAD_SKILL`**：能力进了包 → 该 `SKILL_NOT_AVAILABLE` 缺口不再成立；
     没进包 → 缺口仍在，且带**机器可读**的 `skill_id` 与"为什么没加载"。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_context_pack as cp  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_skills as skills  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402

import qa_phase11_fixtures as fx  # noqa: E402


def _plan():
    return {"question": fx.QUESTION, "question_plan": {"output_form": "brief"}}


def _graph_with_claims(count=3, *, counter=False):
    """一张最小图：`count` 条带引用的 claim（+ 可选一条反证）。

    `count>=3` 是 **Phase 08 的判定门槛**（claim 数 ≥3 → 需要 `citation_verification`），
    所以这里默认 3 条 claim —— 不这样的话 `SKILL_NOT_AVAILABLE` 缺口根本不会出现，
    测的就不是真实接线。
    """
    evidence, claims, edges = [], [], []
    for index in range(count):
        ref = "article:%d" % (index + 1)
        claim_id = "c%d" % (index + 1)
        evidence.append(fx.verified_evidence(ref, claim_text=fx.CLAIM_TEXT))
        claims.append({"claim_id": claim_id, "plan_only": False, "text": fx.CLAIM_TEXT,
                       "verification_status": "confirmed",
                       "claim": {"claim_id": claim_id, "text": fx.CLAIM_TEXT,
                                 "claim_type": "policy", "confidence": 0.8,
                                 "evidence_refs": [ref]}})
        edges.append({"edge_id": "e%d" % (index + 1), "claim_id": claim_id,
                      "evidence_ref": ref, "graph_relation": "SUPPORTS",
                      "relationship": "supports", "metadata": {"verified": True},
                      "relevance_score": 0.8})
    if counter:
        counter_evidence = cp.normalize_evidence_layer(
            fx.verified_evidence("article:99", claim_text="不予给予利得税宽免",
                                 relation="contradicts"))
        evidence.append(counter_evidence)
        edges.append({"edge_id": "e99", "claim_id": "c1", "evidence_ref": "article:99",
                      "graph_relation": "REFUTES", "relationship": "contradicts",
                      "metadata": {"verified": True}, "relevance_score": 0.7})
    return {"claims": claims, "evidence": evidence, "edges": edges,
            "verification": {"stats": {"claims": count, "confirmed": count}}}


class InstructionBuildTests(unittest.TestCase):
    def test_no_selection_means_no_instruction(self):
        self.assertEqual(skills.skill_instructions({"selected_detail": []}), [])
        self.assertEqual(skills.skill_context_items({"selected": []}), [])

    def test_one_instruction_per_selected_skill_in_order(self):
        routing = skills.route_skills(gaps=[fx.gap(routes=("keyword",))], task_type="CAUSAL")
        instructions = skills.skill_instructions(routing)
        self.assertEqual([row["skill_id"] for row in instructions], routing["selected"])
        self.assertEqual(len(instructions), len(routing["selected"]))

    def test_every_instruction_declares_itself_as_a_hint(self):
        routing = skills.route_skills(gaps=[fx.gap(routes=("keyword",))], task_type="CAUSAL")
        for row in skills.skill_instructions(routing):
            self.assertIs(row["is_evidence"], False)
            self.assertIs(row["requires_revalidation"], True)
            self.assertIs(row["in_citation_map"], False)
            self.assertEqual(row["section"], "skill_context")
            self.assertTrue(row["text"].startswith(skills.SKILL_HINT_MARK))
            ok, why = validate("skill_instruction", row)
            self.assertTrue(ok, why)

    def test_instruction_text_carries_the_declared_preconditions_and_route(self):
        row = skills.skill_instructions(skills.route_skills(
            gaps=[fx.gap(routes=("keyword",))]))[0]
        skill = fx.registry().get(row["skill_id"])
        for precondition in skill["preconditions"]:
            self.assertIn(precondition, row["text"])
        self.assertIn(skill["route"], row["text"])

    def test_instructions_are_deterministic(self):
        routing = skills.route_skills(gaps=[fx.gap(routes=("keyword",))], task_type="CAUSAL")
        first = skills.skill_instructions(routing)
        second = skills.skill_instructions(routing)
        self.assertEqual(first, second)

    def test_limit_truncates_deterministically(self):
        routing = skills.route_skills(gaps=[fx.gap(routes=("keyword", "semantic"))],
                                      task_type="CAUSAL")
        self.assertGreaterEqual(len(routing["selected"]), 2)
        rows = skills.skill_context_items(routing, limit=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["metadata"]["skill_id"], routing["selected"][0])

    def test_grounding_is_never_grounded(self):
        routing = skills.route_skills(gaps=[fx.gap(routes=("keyword",))])
        for item in skills.skill_context_items(routing):
            self.assertIs(item["grounding"]["grounded"], False)
            self.assertIs(item["grounding"]["hint"], True)
            self.assertIs(item["grounding"]["is_evidence"], False)
            self.assertIs(item["grounding"]["in_citation_map"], False)
            self.assertEqual(item["source_stage"], "skill_registry")
            self.assertIn(item["source_stage"], contracts.CONTEXT_ITEM_SOURCES)

    def test_context_items_validate(self):
        routing = skills.route_skills(gaps=[fx.gap(routes=("keyword",))], task_type="CAUSAL")
        for item in skills.skill_context_items(routing):
            ok, why = validate("context_item", item)
            self.assertTrue(ok, why)


class ContextPackWiringTests(unittest.TestCase):
    def test_default_path_is_unchanged_without_skill_items(self):
        pack = cp.build_context_pack(graph=_graph_with_claims(), plan=_plan(),
                                     request={"question": fx.QUESTION}, run_id="r1")
        section = pack["sections"]["skill_context"]
        self.assertEqual(section["count"], 0)
        self.assertIn("Phase 11", section["deferred_to"])
        self.assertNotIn("loaded_skills", section)
        self.assertNotIn("implemented_by", section)

    def test_skill_items_fill_the_section_and_skip_deferred(self):
        routing = skills.route_skills(gaps=[fx.gap(routes=("keyword",))], task_type="CAUSAL")
        pack = cp.build_context_pack(graph=_graph_with_claims(), plan=_plan(),
                                     request={"question": fx.QUESTION}, run_id="r1",
                                     skill_items=skills.skill_context_items(routing))
        section = pack["sections"]["skill_context"]
        self.assertGreaterEqual(section["count"], 1)
        self.assertNotIn("deferred_to", section)
        self.assertEqual(section["implemented_by"], "Phase 11（Skill Registry & Router）")
        self.assertEqual(section["loaded_skills"], sorted(routing["selected"]))
        self.assertIs(section["is_evidence"], False)
        self.assertIs(section["requires_revalidation"], True)
        self.assertEqual(section["hint_policy"], contracts.SKILL_HINT_POLICY)

    def test_skill_items_never_enter_the_citation_map(self):
        routing = skills.route_skills(gaps=[fx.gap(routes=("keyword",))], task_type="CAUSAL")
        pack = cp.build_context_pack(graph=_graph_with_claims(), plan=_plan(),
                                     request={"question": fx.QUESTION}, run_id="r1",
                                     skill_items=skills.skill_context_items(routing))
        skill_item_ids = {item["item_id"] for item in pack["items"]
                          if item["section"] == "skill_context"}
        self.assertTrue(skill_item_ids)
        indexed = {entry["evidence_ref"] for entry in pack["citation_index"].values()}
        for item in pack["items"]:
            if item["section"] == "skill_context":
                self.assertEqual(item["evidence_ref"], "")
                self.assertNotIn(item["item_id"], set(pack["citation_map"]))
        self.assertEqual(indexed & {""}, set())
        self.assertEqual(pack["stats"]["skill_in_citation_map"], 0)
        self.assertEqual(pack["stats"]["skill_items"], len(skill_item_ids))

    def test_trimmed_skill_items_are_reported_as_trimmed_not_deferred(self):
        routing = skills.route_skills(gaps=[fx.gap(routes=("keyword",))], task_type="CAUSAL")
        pack = cp.build_context_pack(graph=_graph_with_claims(), plan=_plan(),
                                     request={"question": fx.QUESTION}, run_id="r1",
                                     budget_tokens=1,
                                     skill_items=skills.skill_context_items(routing))
        section = pack["sections"]["skill_context"]
        self.assertEqual(section["count"], 0)
        self.assertNotIn("deferred_to", section)
        self.assertEqual(section["supplied"], len(routing["selected"]))
        self.assertEqual(section["trimmed"], len(routing["selected"]))
        self.assertEqual(section["loaded_skills"], [])
        self.assertIn("被预算裁掉", section["note"])

    def test_receipt_reports_the_skill_section(self):
        routing = skills.route_skills(gaps=[fx.gap(routes=("keyword",))], task_type="CAUSAL")
        pack = cp.build_context_pack(graph=_graph_with_claims(), plan=_plan(),
                                     request={"question": fx.QUESTION}, run_id="r1",
                                     skill_items=skills.skill_context_items(routing))
        receipt = cp.context_pack_receipt(pack)
        self.assertGreaterEqual(receipt["sections"]["skill_context"], 1)
        self.assertEqual(receipt["stats"]["skill_supplied"], len(routing["selected"]))

    def test_pack_id_is_stable_for_the_same_skill_selection(self):
        routing = skills.route_skills(gaps=[fx.gap(routes=("keyword",))], task_type="CAUSAL")
        items = skills.skill_context_items(routing)
        first = cp.build_context_pack(graph=_graph_with_claims(), plan=_plan(),
                                      request={"question": fx.QUESTION}, run_id="r1",
                                      skill_items=items)
        second = cp.build_context_pack(graph=_graph_with_claims(), plan=_plan(),
                                       request={"question": fx.QUESTION}, run_id="r1",
                                       skill_items=items)
        self.assertEqual(first["pack_id"], second["pack_id"])
        self.assertEqual(first["items"], second["items"])


class LoadSkillGapTests(unittest.TestCase):
    def test_need_is_labelled_when_skill_is_not_loaded(self):
        pack = cp.build_context_pack(graph=_graph_with_claims(), plan=_plan(),
                                     request={"question": fx.QUESTION}, run_id="r1")
        kinds = {gap["context_gap_type"] for gap in pack["context_gaps"]}
        self.assertIn("SKILL_NOT_AVAILABLE", kinds)
        gap = [row for row in pack["context_gaps"]
               if row["context_gap_type"] == "SKILL_NOT_AVAILABLE"][0]
        self.assertEqual(gap["action"], "LOAD_SKILL")
        self.assertEqual(gap["section"], "skill_context")
        self.assertEqual(gap["skill_id"], "citation_verification")
        self.assertIn("Phase 11", gap["detail"])
        self.assertIs(gap["requires_retrieval"], False)
        ok, why = validate("context_gap", gap)
        self.assertTrue(ok, why)

    def test_gap_disappears_once_the_capability_is_in_the_pack(self):
        routing = skills.route_skills(
            context_gaps=[fx.context_gap(skill="citation_verification")])
        pack = cp.build_context_pack(graph=_graph_with_claims(), plan=_plan(),
                                     request={"question": fx.QUESTION}, run_id="r1",
                                     skill_items=skills.skill_context_items(routing))
        self.assertIn("citation_verification", pack["sections"]["skill_context"]["loaded_skills"])
        kinds = [gap["context_gap_type"] for gap in pack["context_gaps"]]
        self.assertNotIn("SKILL_NOT_AVAILABLE", kinds)

    def test_wrong_skill_loaded_keeps_the_gap(self):
        routing = skills.route_skills(gaps=[fx.gap(routes=("keyword",))])
        pack = cp.build_context_pack(graph=_graph_with_claims(), plan=_plan(),
                                     request={"question": fx.QUESTION}, run_id="r1",
                                     skill_items=skills.skill_context_items(routing))
        self.assertEqual(pack["sections"]["skill_context"]["loaded_skills"], ["bm25_search"])
        gap = [row for row in pack["context_gaps"]
               if row["context_gap_type"] == "SKILL_NOT_AVAILABLE"][0]
        self.assertEqual(gap["skill_id"], "citation_verification")
        self.assertIn("Skill Router 本次没有选中这个能力", gap["detail"])

    def test_counter_evidence_needs_contradiction_resolution(self):
        """反证在包里时，需要的能力是 `contradiction_resolution`（Phase 08 的判定规则）。

        这里**直接喂** `detect_context_gaps` 一个含反证条目的包：反证是否真的进包取决于
        Phase 08 的反证预留（那是 Phase 08 的账，P08-04 已有专门用例），
        Phase 11 要钉的是"缺口 → 需要的技能 → 加载后缺口消失"这条链。
        """
        counter = cp.make_context_item(
            kind="counter_evidence", section="counter_evidence", text="不予给予利得税宽免",
            grounding={"grounded": True, "evidence_ref": "article:99",
                       "span": {"quote": "不予给予利得税宽免"}},
            evidence_ref="article:99")
        claims = [cp.make_context_item(kind="claim", section="evidence_context",
                                       text=fx.CLAIM_TEXT, claim_id="c%d" % index,
                                       grounding={"grounded": True})
                  for index in range(3)]
        base = {"pack_id": "CPtest", "items": claims + [counter], "citation_map": {},
                "sections": {}}
        gap = [row for row in cp.detect_context_gaps(pack=base, graph={})
               if row["context_gap_type"] == "SKILL_NOT_AVAILABLE"][0]
        self.assertEqual(gap["skill_id"], "contradiction_resolution")
        loaded = dict(base, sections={"skill_context": {
            "loaded_skills": ["contradiction_resolution"]}})
        self.assertNotIn("SKILL_NOT_AVAILABLE",
                         [row["context_gap_type"]
                          for row in cp.detect_context_gaps(pack=loaded, graph={})])

    def test_gap_explains_budget_trimming(self):
        # 预算 300：结论还在（所以"需要什么能力"仍可判定），技能指令被裁掉
        routing = skills.route_skills(
            context_gaps=[fx.context_gap(skill="citation_verification")])
        pack = cp.build_context_pack(graph=_graph_with_claims(), plan=_plan(),
                                     request={"question": fx.QUESTION}, run_id="r1",
                                     budget_tokens=300,
                                     skill_items=skills.skill_context_items(routing))
        section = pack["sections"]["skill_context"]
        self.assertEqual(section["count"], 0)
        self.assertEqual(section["supplied"], len(routing["selected"]))
        gap = [row for row in pack["context_gaps"]
               if row["context_gap_type"] == "SKILL_NOT_AVAILABLE"][0]
        self.assertIn("被预算裁掉", gap["detail"])
        self.assertEqual(gap["skill_id"], "citation_verification")

    def test_gap_never_triggers_retrieval(self):
        pack = cp.build_context_pack(graph=_graph_with_claims(), plan=_plan(),
                                     request={"question": fx.QUESTION}, run_id="r1")
        self.assertEqual(pack["stats"]["retrieval_requested"], 0)
        for gap in pack["context_gaps"]:
            self.assertIs(gap["requires_retrieval"], False)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 11 · P11-03（Router）用例。

钉住：
  1. §8 的三个输入真的都接上了：`gap_type`（Phase 07 的缺口 → 通道 → 技能）、
     `task_type`（Phase 05 的意图 → 推理型技能）、Phase 08 的 `LOAD_SKILL` 上下文缺口；
  2. **最小必要集合**：一个 need 只选一个技能；跨 need 命中同一技能只加载一次
     （`MINIMAL_SET_DEDUPE`）；need 满足后其余候选一律 `MINIMAL_SET_SATISFIED`；
  3. **可复算**：同输入同输出（连 trace 都逐字相同），且顺序不依赖输入字典的插入顺序；
  4. **history performance 真的参与**：低成功率换候选（`LOW_SUCCESS_RATE`）、
     高成功率加成（`PERFORMANCE_BOOST`），但样本不足时**不参与**（不许拿 1 次当 100%）；
  5. `requires_retrieval` 恒 False（MASTER_RULES 第 13 条：选技能不是发起新检索）；
  6. 理由码全部落在闭集合内；没有 need 时**一个技能都不加载**（宁缺勿造）。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_graph_contracts as contracts  # noqa: E402
import qa_skills as skills  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402

import qa_phase11_fixtures as fx  # noqa: E402


def _reasons(routing):
    return [(row["skill_id"], row["decision"], row["reason"]) for row in routing["trace"]]


class GapDrivenRoutingTests(unittest.TestCase):
    def test_gap_routes_are_translated_into_skills(self):
        routing = skills.route_skills(gaps=[fx.gap(routes=("keyword", "semantic"))])
        self.assertEqual(routing["selected"], ["bm25_search"])
        self.assertEqual(_reasons(routing)[0], ("bm25_search", "selected", "GAP_ROUTE_MATCH"))
        self.assertEqual(_reasons(routing)[1], ("semantic_search", "skipped",
                                               "MINIMAL_SET_SATISFIED"))

    def test_two_needs_need_two_skills(self):
        routing = skills.route_skills(gaps=[
            fx.gap("GAP-A", routes=("keyword",)),
            fx.gap("GAP-B", routes=("semantic",)),
        ])
        self.assertEqual(routing["selected"], ["bm25_search", "semantic_search"])
        self.assertEqual(routing["stats"]["minimal_set_satisfied"], 0)

    def test_same_skill_from_two_needs_is_loaded_once(self):
        routing = skills.route_skills(gaps=[
            fx.gap("GAP-A", routes=("keyword",)),
            fx.gap("GAP-B", routes=("keyword", "page_context")),
        ])
        self.assertEqual(routing["selected"], ["bm25_search"])
        self.assertEqual(routing["stats"]["minimal_set_dedupe"], 1)

    def test_gap_priority_order_is_deterministic(self):
        low = fx.gap("GAP-L", routes=("web",), priority=0.2)
        high = fx.gap("GAP-H", routes=("keyword",), priority=0.9)
        # 输入顺序反过来，输出必须一致（按优先级降序 + gap_id 升序）
        first = skills.route_skills(gaps=[low, high])
        second = skills.route_skills(gaps=[high, low])
        self.assertEqual(first["selected"], second["selected"])
        self.assertEqual(first["trace"], second["trace"])
        self.assertEqual(first["selected"], ["bm25_search"])

    def test_unaffordable_by_default_web_search_is_reported_not_silently_dropped(self):
        routing = skills.route_skills(gaps=[fx.gap(routes=("web",))])
        self.assertEqual(routing["selected"], [])
        self.assertIn("OVER_LATENCY_BUDGET", routing["skipped"])
        self.assertIn("OVER_LATENCY_BUDGET", routing["stats"]["budget_exhausted"])

    def test_reasoning_gap_types_add_reasoning_skills(self):
        routing = skills.route_skills(gaps=[fx.gap("GAP-C", missing="CONTRADICTION",
                                                  routes=("policy_exact",))])
        self.assertIn("contradiction_resolution", routing["selected"])
        self.assertIn("sql_query", routing["selected"])
        reasons = dict((row["skill_id"], row["reason"]) for row in routing["trace"]
                       if row["decision"] == "selected")
        self.assertEqual(reasons["contradiction_resolution"], "GAP_TYPE_MATCH")

    def test_ambiguous_entity_needs_clarification_but_permission_is_denied_by_default(self):
        routing = skills.route_skills(gaps=[fx.gap("GAP-D", missing="AMBIGUOUS_ENTITY",
                                                  routes=("graph_attribute",))])
        self.assertIn("graph_traversal", routing["selected"])
        self.assertNotIn("patient_inquiry", routing["selected"])
        denied = [row for row in routing["trace"]
                  if row["skill_id"] == "patient_inquiry" and row["reason"] == "PERMISSION_DENIED"]
        self.assertTrue(denied, "默认必须拒绝 patient_inquiry（没有 patient_contact 权限）")

    def test_real_phase07_gaps_are_accepted(self):
        gaps = fx.real_gaps(routes=("keyword",))
        self.assertTrue(gaps, "Phase 07 的真实规则必须能产出缺口")
        routing = skills.route_skills(gaps=gaps)
        self.assertEqual(routing["selected"], ["bm25_search"])
        self.assertTrue(all(row["gap_id"] for row in gaps))


class TaskTypeRoutingTests(unittest.TestCase):
    def test_task_type_selects_reasoning_skills(self):
        routing = skills.route_skills(task_type="CAUSAL")
        self.assertEqual(routing["selected"], ["causal_reasoning"])
        self.assertEqual(routing["stats"]["selected"], 1)

    def test_unknown_task_type_selects_nothing(self):
        routing = skills.route_skills(task_type="NOT_AN_INTENT")
        self.assertEqual(routing["selected"], [])
        # 认不出的任务类型不给任何候选；未触及的技能如实记"本轮不需要"
        for row in routing["trace"]:
            self.assertEqual(row["decision"], "skipped")
            self.assertIn(row["reason"], ("NOT_TASK_RELEVANT", "NO_ROUTE_IN_DEPLOYMENT"))

    def test_no_inputs_means_no_skills(self):
        routing = skills.route_skills()
        self.assertEqual(routing["selected"], [])
        self.assertEqual(routing["budget"]["used_skills"], 0)

    def test_diagnostic_prefers_domain_check_and_denies_emr(self):
        routing = skills.route_skills(task_type="DIAGNOSTIC")
        self.assertEqual(routing["selected"], ["clinical_evidence"])
        emr = [row for row in routing["trace"] if row["skill_id"] == "emr_search"]
        self.assertEqual(emr[0]["reason"], "NO_ROUTE_IN_DEPLOYMENT")


class ContextGapRoutingTests(unittest.TestCase):
    def test_load_skill_context_gap_is_actually_loaded(self):
        routing = skills.route_skills(context_gaps=[fx.context_gap(skill="citation_verification")])
        self.assertEqual(routing["selected"], ["citation_verification"])
        reasons = dict((row["skill_id"], row["reason"]) for row in routing["trace"]
                       if row["decision"] == "selected")
        self.assertEqual(reasons["citation_verification"], "CONTEXT_GAP_LOAD_SKILL")

    def test_context_gap_has_highest_priority_over_evidence_gaps(self):
        routing = skills.route_skills(
            gaps=[fx.gap("GAP-X", routes=("keyword",), priority=1.0)],
            context_gaps=[fx.context_gap(gap_id="CG-9", skill="citation_verification")],
            mandatory=("bm25_search",))
        self.assertEqual(routing["selected"][0], "bm25_search")   # mandatory 在最前
        self.assertIn("citation_verification", routing["selected"])

    def test_skill_from_context_gap_is_read_from_the_machine_field_only(self):
        # 没有 skill_id 字段时只按 Phase 08 的两个已知能力名匹配；匹配不上就不加载
        row = fx.context_gap(skill="")
        row["detail"] = "本任务需要 citation_verification 能力（§8）"
        routing = skills.route_skills(context_gaps=[row])
        self.assertEqual(routing["selected"], ["citation_verification"])
        row = fx.context_gap(skill="")
        row["detail"] = "需要某个说不清的能力"
        self.assertEqual(skills.route_skills(context_gaps=[row])["selected"], [])

    def test_skill_id_field_is_not_parsed_as_free_text(self):
        row = fx.context_gap(skill="patient_inquiry")
        row["detail"] = "本任务需要 citation_verification 能力"
        routing = skills.route_skills(context_gaps=[row])
        # 机器字段优先：detail 里的自由文本不许改写它（且 patient_inquiry 默认被拒）
        self.assertEqual(routing["selected"], [])
        self.assertIn("PERMISSION_DENIED", routing["skipped"])

    def test_needed_skills_is_the_same_need_as_a_context_gap(self):
        # `needed_skills` 与 `context_gaps` 是同一个 need 的两种输入形态（理由码相同）
        first = skills.route_skills(needed_skills=["citation_verification"])
        second = skills.route_skills(context_gaps=[fx.context_gap(skill="citation_verification")])
        self.assertEqual(first["selected"], second["selected"])
        self.assertEqual([row["reason"] for row in first["trace"] if row["decision"] == "selected"],
                         [row["reason"] for row in second["trace"]
                          if row["decision"] == "selected"])

    def test_needed_skills_agrees_with_phase08_judgement(self):
        """判据必须与 Phase 08 的缺口检测**同一个函数**（否则就是两套真源）。"""
        import qa_context_pack as cp

        counter = cp.make_context_item(kind="counter_evidence", section="counter_evidence",
                                       text="反证", grounding={"grounded": True})
        claims = [cp.make_context_item(kind="claim", section="evidence_context",
                                       text="结论 %d" % index, claim_id="c%d" % index,
                                       grounding={"grounded": True}) for index in range(3)]
        self.assertEqual(cp.skill_context_needs(items=claims),
                         "citation_verification")
        self.assertEqual(cp.skill_context_needs(items=claims + [counter]),
                         "contradiction_resolution")
        self.assertEqual(cp.skill_context_needs(items=claims[:2]), "")
        # Phase 08 的缺口判定也走它：同一个输入同一个答案
        pack = {"pack_id": "CPx", "items": claims, "citation_map": {}, "sections": {}}
        gap = [row for row in cp.detect_context_gaps(pack=pack, graph={})
               if row["context_gap_type"] == "SKILL_NOT_AVAILABLE"][0]
        self.assertEqual(gap["skill_id"], cp.skill_context_needs(items=claims))


class MinimalSetTests(unittest.TestCase):
    def test_selected_count_never_exceeds_need_count(self):
        gaps = [fx.gap("G%02d" % index, routes=("keyword", "semantic", "graph", "policy_exact"))
                for index in range(5)]
        routing = skills.route_skills(gaps=gaps)
        self.assertEqual(routing["selected"], ["bm25_search"])
        self.assertLessEqual(len(routing["selected"]), len(routing["needs"]))

    def test_max_skills_caps_the_set(self):
        gaps = [fx.gap("G%02d" % index, missing="CONTRADICTION", routes=(route,))
                for index, route in enumerate(("keyword", "semantic", "graph"))]
        routing = skills.route_skills(gaps=gaps, budget=skills.SkillBudget(max_skills=2))
        self.assertEqual(len(routing["selected"]), 2)
        self.assertIn("OVER_SKILL_COUNT", routing["stats"]["budget_exhausted"])

    def test_no_route_in_deployment_is_not_counted_as_retrieval(self):
        # 要真的加载 `emr_search` 必须**显式**授予 emr_read 并放宽延迟预算（slow=12000ms）
        routing = skills.route_skills(
            context_gaps=[fx.context_gap(skill="emr_search")],
            budget=skills.SkillBudget(granted_permissions=("corpus_read", "emr_read"),
                                      max_latency_ms=20000.0))
        self.assertEqual(routing["selected"], ["emr_search"])
        detail = [row for row in routing["selected_detail"] if row["skill_id"] == "emr_search"][0]
        # 它是**检索型**技能（会产生证据），但本部署没有这条通道：route 空串 → 它不会被记进
        # SearchTrace 的 route，也不会拿到任何 `evidence_yield`（遥测按 route 计数）。
        self.assertEqual(detail["route"], "")
        self.assertTrue(detail["produces_evidence"])
        # 默认权限下同一个技能必须被拒（默认拒绝）
        default = skills.route_skills(context_gaps=[fx.context_gap(skill="emr_search")],
                                      budget=skills.SkillBudget(max_latency_ms=20000.0))
        self.assertEqual(default["selected"], [])
        self.assertIn("PERMISSION_DENIED", default["skipped"])

    def test_trace_covers_every_declared_skill(self):
        routing = skills.route_skills(gaps=[fx.gap()])
        touched = {row["skill_id"] for row in routing["trace"]}
        self.assertEqual(touched, set(contracts.SKILL_IDS))


class PerformanceDrivenRoutingTests(unittest.TestCase):
    def test_low_success_rate_switches_candidate(self):
        performance = {"bm25_search": {"attempts": 10, "success_rate": 0.1},
                       "semantic_search": {"attempts": 10, "success_rate": 0.5}}
        routing = skills.route_skills(gaps=[fx.gap(routes=("keyword", "semantic"))],
                                     performance=performance)
        self.assertEqual(routing["selected"], ["semantic_search"])
        reasons = dict((row["skill_id"], row["reason"]) for row in routing["trace"])
        self.assertEqual(reasons["bm25_search"], "LOW_SUCCESS_RATE")

    def test_boost_reorders_within_a_need_only(self):
        performance = {"bm25_search": {"attempts": 10, "success_rate": 0.2},
                       "semantic_search": {"attempts": 10, "success_rate": 0.9}}
        routing = skills.route_skills(gaps=[fx.gap(routes=("keyword", "semantic"))],
                                     performance=performance)
        self.assertEqual(routing["selected"], ["semantic_search"])
        self.assertEqual(routing["stats"]["performance_boosted"], 1)

    def test_insufficient_samples_do_not_affect_routing(self):
        performance = {"semantic_search": {"attempts": 1, "success_rate": 1.0}}
        routing = skills.route_skills(gaps=[fx.gap(routes=("keyword", "semantic"))],
                                     performance=performance)
        # 1 次成功不算历史：不许因此把 semantic_search 提到 bm25_search 前面
        self.assertEqual(routing["selected"], ["bm25_search"])
        self.assertEqual(routing["stats"]["performance_boosted"], 0)

    def test_none_success_rate_does_not_affect_routing(self):
        performance = {"bm25_search": {"attempts": 10, "success_rate": None}}
        routing = skills.route_skills(gaps=[fx.gap(routes=("keyword", "semantic"))],
                                     performance=performance)
        self.assertEqual(routing["selected"], ["bm25_search"])

    def test_performance_comes_from_real_telemetry_records(self):
        rows = fx.records_for("bm25_search", outcomes=("ok", "degraded", "degraded"))
        table = skills.performance_table(rows)
        routing = skills.route_skills(gaps=[fx.gap(routes=("keyword", "semantic"))],
                                     performance=table)
        self.assertEqual(routing["selected"], ["semantic_search"])
        detail = [row for row in routing["trace"] if row["skill_id"] == "semantic_search"][0]
        self.assertEqual(detail["reason"], "PERFORMANCE_BOOST")


class DeterminismAndContractTests(unittest.TestCase):
    def test_same_input_same_output(self):
        gaps = [fx.gap("G1", routes=("keyword", "semantic")),
                fx.gap("G2", missing="CONTRADICTION", routes=("graph",))]
        first = skills.route_skills(gaps=gaps, task_type="COMPARISON")
        second = skills.route_skills(gaps=gaps, task_type="COMPARISON")
        self.assertEqual(first["selected"], second["selected"])
        self.assertEqual(first["trace"], second["trace"])
        self.assertEqual(first["budget"], second["budget"])
        self.assertEqual(first["stats"], second["stats"])

    def test_trace_rows_validate_against_the_contract(self):
        routing = skills.route_skills(gaps=[fx.gap()], task_type="CAUSAL")
        self.assertTrue(routing["trace"])
        for row in routing["trace"]:
            ok, why = validate("skill_selection", row)
            self.assertTrue(ok, "%s 的 trace 行过不了契约：%s" % (row["skill_id"], why))
        ok, why = validate("skill_routing", routing)
        self.assertTrue(ok, why)

    def test_reason_codes_are_all_from_the_closed_set(self):
        routing = skills.route_skills(gaps=[fx.gap(), fx.gap("G2", missing="CONTRADICTION",
                                                            routes=("web",))],
                                     task_type="SYNTHESIS")
        for row in routing["trace"]:
            self.assertIn(row["reason"], contracts.SKILL_SELECTION_REASONS)

    def test_requires_retrieval_is_false(self):
        routing = skills.route_skills(gaps=[fx.gap(routes=("keyword", "semantic", "web"))])
        self.assertIs(routing["requires_retrieval"], False)
        self.assertEqual(routing["stats"]["retrieval_requested"], 0)

    def test_receipt_is_serialisable_and_small(self):
        import json

        routing = skills.route_skills(gaps=[fx.gap()], task_type="CAUSAL")
        receipt = skills.skill_routing_receipt(routing)
        self.assertEqual(receipt["selected"], routing["selected"])
        self.assertEqual(receipt["instruction_version"], contracts.SKILL_INSTRUCTION_VERSION)
        self.assertEqual(receipt["telemetry_version"], contracts.SKILL_TELEMETRY_VERSION)
        self.assertNotIn("trace", receipt)
        self.assertTrue(json.dumps(receipt, ensure_ascii=False))

    def test_routing_summary_is_recomputable(self):
        routing = skills.route_skills(gaps=[fx.gap()])
        summary = skills.routing_summary(routing)
        self.assertEqual(summary["rows"], len(routing["trace"]))
        self.assertEqual(summary["selected"], len(routing["selected"]))
        self.assertEqual(sum(item for item in summary["reason_distribution"].values()),
                         summary["rows"])

    def test_unknown_registry_entries_are_reported_not_crashed(self):
        registry = skills.SkillRegistry()
        registry._skills.pop("bm25_search")
        routing = skills.route_skills(gaps=[fx.gap(routes=("keyword",))], registry=registry)
        self.assertEqual(routing["selected"], [])
        self.assertIn("UNKNOWN_SKILL", routing["skipped"])


if __name__ == "__main__":
    unittest.main()

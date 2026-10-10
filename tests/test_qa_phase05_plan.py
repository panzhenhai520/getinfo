#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 05 · P05-02 / P05-03 用例（子问题·Claim·依赖·并行组）。

钉住四件事：
  1. **复用而不是另写一套**：有计划里的 `decomposition` 时**不许**再调 `decompose()`
     （否则执行图与实际多跳用的 hops 会分叉）；没有时才现算，并把来源写进 `decomposition.source`；
  2. §7 的四件产物齐全：`sub_questions` / `claims` / `evidence_requirements` / `dependencies`，
     且"证据要求"的分类**复用** `qa_planner._retrieval_strategy` 的 source 口径；
  3. §2.1（无依赖不许串行）与 §2.4/§2.5（独立任务同组并行、汇合点才是 barrier）：
     并行组由依赖层级算出来，组内节点两两无依赖；
  4. DAG 校验**复用** `qa_query_decompose.validate_dag`：非法依赖（指向未来/自环）会被
     decompose 降级成单跳，计划里如实反映（`dag.ok` + 降级原因）。
"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_execution_graph as eg  # noqa: E402
import qa_query_decompose as decompose_module  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402
from qa_query_interpreter import interpret_query  # noqa: E402

MULTI_HOP_QUESTION = "香港家族办公室税收优惠政策对内地高净值客户有什么影响？"
SIMPLE_QUESTION = "2026年医保新规是否适用于民营医院"


def _plan(question=MULTI_HOP_QUESTION, **overrides):
    """一份"规划器输出"的最小形状（与 QaQueryPlanner.plan 的键一致）。"""
    value = {
        "question": question,
        "standalone_question": question,
        "category": {"key": "multi_hop", "label": "多跳传导类"},
        "entities": ["家族办公室"],
        "topics": [],
        "question_plan": {"relationship": "parallel",
                          "categories": [{"key": "multi_hop", "label": "多跳传导类"}],
                          "subquestions": [{"id": "q1", "text": question}]},
        "research_axes": ["official_text"],
    }
    value.update(overrides)
    return value


class ReuseDecompositionTests(unittest.TestCase):
    def test_existing_decomposition_is_reused_not_recomputed(self):
        """有计划里的 hops → 一次 `decompose()` 都不许再调。"""
        plan = _plan(decomposition={
            "is_multi_hop": True, "pattern": "impact_chain", "reason": "既有 DAG",
            "hops": [{"id": "h1", "question": "香港家族办公室税收优惠政策", "depends_on": [],
                      "carry": ["entities"], "purpose": "起点"},
                     {"id": "h2", "question": "政策 内地高净值客户", "depends_on": ["h1"],
                      "carry": ["entities"], "purpose": "连接"}],
        })
        with mock.patch.object(eg, "decompose",
                               side_effect=AssertionError("不许重新分解！")) as spy:
            result = eg.build_research_plan(MULTI_HOP_QUESTION, plan=plan)
            self.assertFalse(spy.called)
        self.assertEqual([item["sub_question_id"] for item in result["sub_questions"]],
                         ["sq:h1", "sq:h2"])
        self.assertIn("reused", result["decomposition"]["source"])

    def test_missing_decomposition_is_computed_by_the_existing_decomposer(self):
        result = eg.build_research_plan(MULTI_HOP_QUESTION, plan=_plan())
        self.assertTrue(result["sub_questions"])
        self.assertIn("qa_query_decompose", result["decomposition"]["source"])
        self.assertEqual(result["decomposition"]["hop_count"], len(result["sub_questions"]))

    def test_sub_questions_pass_the_contract(self):
        result = eg.build_research_plan(MULTI_HOP_QUESTION, plan=_plan())
        for item in result["sub_questions"]:
            ok, note = validate("sub_question", item)
            self.assertTrue(ok, note)
            self.assertEqual(item["plan_node_kind"], "sub_question")
            self.assertTrue(item["required_evidence_types"])


class ClaimAndRequirementTests(unittest.TestCase):
    def test_claims_cover_sub_questions_plus_counter(self):
        result = eg.build_research_plan(MULTI_HOP_QUESTION, plan=_plan())
        self.assertEqual(len(result["claims"]), len(result["sub_questions"]) + 1)
        for claim in result["claims"]:
            ok, note = validate("plan_claim", claim)
            self.assertTrue(ok, note)
        counter = [item for item in result["claims"] if item["role"] == "counter"]
        self.assertEqual(len(counter), 1)
        self.assertTrue(counter[0]["plan_only"], "反证 claim 本轮不执行，必须标 plan_only")

    def test_evidence_requirements_reuse_retrieval_strategy_sources(self):
        from qa_planner import _retrieval_strategy

        result = eg.build_research_plan(MULTI_HOP_QUESTION, plan=_plan())
        expected = [step["source"] for step in _retrieval_strategy(
            "parallel", [{"key": "multi_hop", "label": "多跳传导类"}])]
        self.assertEqual(result["required_evidence_types"], expected)
        for item in result["evidence_requirements"]:
            ok, note = validate("evidence_requirement", item)
            self.assertTrue(ok, note)
            self.assertIn(item["evidence_type"], expected)
            self.assertEqual(item["satisfied_by"], "", "本轮只声明需求，不许声称已满足")

    def test_single_hop_question_still_gets_one_sub_question(self):
        result = eg.build_research_plan(SIMPLE_QUESTION, plan=_plan(
            question=SIMPLE_QUESTION, category={"key": "subject_scope", "label": "适用对象类"},
            decomposition={"is_multi_hop": False, "pattern": "single", "reason": "单跳",
                           "hops": [{"id": "h1", "question": SIMPLE_QUESTION,
                                     "depends_on": [], "purpose": "单跳直接检索"}]}))
        self.assertEqual(len(result["sub_questions"]), 1)
        self.assertFalse(result["decomposition"]["is_multi_hop"])
        self.assertTrue(result["dag"]["ok"])


class DependencyAndParallelGroupTests(unittest.TestCase):
    def test_dependencies_carry_the_data_contract(self):
        plan = _plan(decomposition={
            "is_multi_hop": True, "pattern": "impact_chain", "reason": "既有 DAG",
            "hops": [{"id": "h1", "question": "A", "depends_on": [], "carry": ["entities"]},
                     {"id": "h2", "question": "B", "depends_on": ["h1"], "carry": ["entities"]}],
        })
        result = eg.build_research_plan(MULTI_HOP_QUESTION, plan=plan)
        self.assertEqual(len(result["dependencies"]), 1)
        edge = result["dependencies"][0]
        self.assertEqual((edge["from"], edge["to"]), ("sq:h1", "sq:h2"))
        self.assertIn("entities", edge["carries"])
        self.assertTrue(edge["schema"], "Edge 也是数据契约（§2.3），必须带 schema")

    def test_parallel_groups_follow_dependency_levels(self):
        plan = _plan(decomposition={
            "is_multi_hop": True, "pattern": "fan", "reason": "独立子问题",
            "hops": [{"id": "h1", "question": "A", "depends_on": []},
                     {"id": "h2", "question": "B", "depends_on": []},
                     {"id": "h3", "question": "C", "depends_on": []},
                     {"id": "h4", "question": "合并", "depends_on": ["h1", "h2", "h3"]}],
        })
        result = eg.build_research_plan(MULTI_HOP_QUESTION, plan=plan)
        groups = result["parallel_groups"]
        self.assertEqual(groups[0]["nodes"], ["sq:h1", "sq:h2", "sq:h3"])
        self.assertTrue(groups[0]["parallel"], "互不依赖的节点必须在同一并行组（§2.4）")
        self.assertEqual(groups[1]["nodes"], ["sq:h4"])
        self.assertTrue(groups[1]["barrier"], "入度 ≥2 的汇合点才是 barrier（§2.5）")
        self.assertEqual(groups[1]["size"], 1)
        self.assertFalse(groups[1]["parallel"])
        # 组内两两无依赖：这是"能不能并行"的硬判据（§2.1）
        for group in groups:
            for left in group["nodes"]:
                for right in group["nodes"]:
                    if left == right:
                        continue
                    deps = dict((item["sub_question_id"], item["depends_on"])
                                for item in result["sub_questions"])
                    self.assertNotIn(right, deps.get(left, []),
                                     "%s 与 %s 有依赖，不该同组" % (left, right))

    def test_no_dependency_means_no_chain(self):
        """§2.1：B 不读 A 的结果，就不许写成 A→B。"""
        plan = _plan(decomposition={
            "is_multi_hop": True, "pattern": "fan", "reason": "独立子问题",
            "hops": [{"id": "h1", "question": "A", "depends_on": []},
                     {"id": "h2", "question": "B", "depends_on": []}],
        })
        result = eg.build_research_plan(MULTI_HOP_QUESTION, plan=plan)
        self.assertEqual(result["dependencies"], [])
        self.assertEqual(len(result["parallel_groups"]), 1)
        self.assertTrue(result["parallel_groups"][0]["parallel"])

    def test_parallel_groups_helper_is_generic(self):
        groups = eg.parallel_groups([
            {"node_id": "a", "depends_on": []},
            {"node_id": "b", "depends_on": ["a"]},
            {"node_id": "c", "depends_on": ["a"]},
            {"node_id": "d", "depends_on": ["b", "c"]},
        ])
        self.assertEqual([group["nodes"] for group in groups],
                         [["a"], ["b", "c"], ["d"]])
        self.assertTrue(groups[1]["parallel"])
        self.assertTrue(groups[2]["barrier"])

    def test_dag_validation_is_reused_and_reported(self):
        """非法依赖（指向未来）→ decompose 降级为单跳；计划里必须如实反映。"""
        graph = decompose_module.decompose("A 对 B 有什么影响", category="multi_hop", max_hops=3)
        check = decompose_module.validate_dag(graph["hops"], max_hops=3)
        self.assertTrue(check["ok"])
        bad = decompose_module.validate_dag(
            [{"id": "h1", "depends_on": ["h2"]}, {"id": "h2", "depends_on": []}], max_hops=3)
        self.assertFalse(bad["ok"])
        self.assertTrue(any("更晚" in item or "不存在" in item for item in bad["problems"]))
        result = eg.build_research_plan(MULTI_HOP_QUESTION, plan=_plan())
        self.assertIn("dag", result)
        self.assertTrue(result["dag"]["ok"])
        self.assertEqual(result["dag"]["problems"], [])

    def test_cycle_is_rejected_by_the_reused_validator(self):
        cyclic = decompose_module.validate_dag(
            [{"id": "h1", "depends_on": ["h2"]}, {"id": "h2", "depends_on": ["h1"]}], max_hops=3)
        self.assertFalse(cyclic["ok"])
        self.assertTrue(cyclic["problems"])


class HopCapProbeTests(unittest.TestCase):
    def test_cap_is_not_reported_as_depth_when_nothing_was_cut(self):
        result = eg.build_research_plan(MULTI_HOP_QUESTION, plan=_plan(), max_hops=3)
        self.assertFalse(result["hop_cap"]["truncated"])
        self.assertEqual(result["hop_cap"]["probe_hop_count"], result["hop_cap"]["hop_count"])

    def test_truncation_is_proved_by_relaxing_the_cap(self):
        """QA_MAX_HOPS=2 时放宽到 5 会长出更多跳 → 才能宣布 MAX_DEPTH。"""
        result = eg.build_research_plan(MULTI_HOP_QUESTION, plan=_plan(), max_hops=2)
        self.assertTrue(result["hop_cap"]["truncated"])
        self.assertGreater(result["hop_cap"]["probe_hop_count"],
                           result["hop_cap"]["hop_count"])

    def test_plan_reports_reuse_map(self):
        result = eg.build_research_plan(MULTI_HOP_QUESTION, plan=_plan())
        self.assertIn("qa_query_decompose.validate_dag", result["reuse"]["dag_validation"])
        self.assertIn("qa_planner._retrieval_strategy",
                      result["reuse"]["evidence_requirements"])
        interpretation = interpret_query(MULTI_HOP_QUESTION)
        self.assertIn("qa_planner", interpretation["reuse"]["category_rules"])


if __name__ == "__main__":
    unittest.main()

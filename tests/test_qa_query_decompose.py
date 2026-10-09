#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 9 · 子查询分解器的结构测试（DAG、环检测、降级、四类模式）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qa_query_decompose import decompose, split_impact_subjects, validate_dag  # noqa: E402


class DagValidationTests(unittest.TestCase):
    def test_valid_chain(self):
        check = validate_dag([{"id": "h1", "depends_on": []},
                              {"id": "h2", "depends_on": ["h1"]}])
        self.assertTrue(check["ok"], check["problems"])

    def test_self_dependency_rejected(self):
        check = validate_dag([{"id": "h1", "depends_on": ["h1"]}])
        self.assertFalse(check["ok"])
        self.assertTrue(any("依赖了自己" in item for item in check["problems"]))

    def test_dependency_on_later_hop_rejected(self):
        """只能依赖更早的跳 —— 否则就是环（A→B→A）。"""
        check = validate_dag([{"id": "h1", "depends_on": ["h2"]},
                              {"id": "h2", "depends_on": []}])
        self.assertFalse(check["ok"])

    def test_duplicate_and_missing_id_rejected(self):
        self.assertFalse(validate_dag([{"id": "h1"}, {"id": "h1"}])["ok"])
        self.assertFalse(validate_dag([{"id": ""}])["ok"])

    def test_max_hops_enforced(self):
        hops = [{"id": "h%d" % index, "depends_on": []} for index in range(1, 5)]
        self.assertFalse(validate_dag(hops, max_hops=3)["ok"])
        self.assertTrue(validate_dag(hops[:3], max_hops=3)["ok"])


class ImpactSubjectTests(unittest.TestCase):
    def test_extracts_a_and_b(self):
        self.assertEqual(split_impact_subjects("2026年医保新规对民营医院有什么影响？"),
                         {"a": "2026年医保新规", "b": "民营医院"})
        self.assertEqual(split_impact_subjects("关税调整对供应链的影响"),
                         {"a": "关税调整", "b": "供应链"})

    def test_no_impact_returns_empty(self):
        self.assertEqual(split_impact_subjects("国家药监局最近有什么动态？"), {"a": "", "b": ""})


class DecomposePatternTests(unittest.TestCase):
    def _assert_dag(self, graph):
        self.assertTrue(graph["hops"], "至少要有一跳")
        check = validate_dag(graph["hops"], max_hops=5)
        self.assertTrue(check["ok"], check["problems"])
        for hop in graph["hops"]:
            self.assertTrue(hop["question"], "每一跳都必须有可执行的问句")
            self.assertIn("depends_on", hop)
            self.assertIn("carry", hop)

    def test_multi_hop_impact_chain(self):
        graph = decompose("2026年医保新规对民营医院有什么影响？", category="multi_hop")
        self.assertTrue(graph["is_multi_hop"])
        self.assertEqual(graph["pattern"], "impact_chain")
        self.assertEqual([hop["id"] for hop in graph["hops"]], ["h1", "h2", "h3"])
        self.assertEqual(graph["hops"][1]["depends_on"], ["h1"])
        self.assertEqual(graph["hops"][2]["depends_on"], ["h2"])
        self.assertIn("民营医院", graph["hops"][1]["question"])
        self._assert_dag(graph)

    def test_multi_hop_without_ab_structure_degrades_two_hops(self):
        graph = decompose("供应链传导会影响到谁", category="multi_hop")
        self.assertTrue(graph["is_multi_hop"])
        self.assertEqual(graph["pattern"], "impact_generic")
        self._assert_dag(graph)

    def test_causal_two_hops(self):
        graph = decompose("为什么家族办公室数量增长这么快？", category="causal")
        self.assertTrue(graph["is_multi_hop"])
        self.assertEqual(graph["pattern"], "fact_then_reason")
        self.assertIn("原因", graph["hops"][1]["question"])
        self._assert_dag(graph)

    def test_conditional_two_hops(self):
        graph = decompose("在CRS合规条件下，香港家办是否需要申报？",
                          category="conditional_constraint")
        self.assertTrue(graph["is_multi_hop"])
        self.assertEqual(graph["pattern"], "condition_check")
        self.assertIn("CRS合规", graph["hops"][0]["question"])
        self._assert_dag(graph)

    def test_temporal_pair(self):
        graph = decompose("先出台政策还是先有试点？", category="temporal_relation")
        self.assertTrue(graph["is_multi_hop"])
        self.assertEqual(graph["pattern"], "temporal_pair")
        self.assertEqual(graph["hops"][0]["question"], "出台政策")
        self.assertEqual(graph["hops"][1]["question"], "有试点")
        self._assert_dag(graph)

    def test_plain_question_stays_single_hop(self):
        """单跳问题必须保持单跳（不改变既有路径）。"""
        for question, category in (("国家药监局最近有什么动态？", "fact_check"),
                                   ("家族信托的税务宽免内容是什么？", "policy_content")):
            graph = decompose(question, category=category)
            self.assertFalse(graph["is_multi_hop"])
            self.assertEqual(len(graph["hops"]), 1)
            self.assertIn("单跳", graph["reason"])

    def test_max_hops_one_forces_single(self):
        graph = decompose("2026年医保新规对民营医院有什么影响？",
                          category="multi_hop", max_hops=1)
        self.assertFalse(graph["is_multi_hop"])
        self.assertIn("上限", graph["reason"])

    def test_max_hops_truncates_chain(self):
        graph = decompose("2026年医保新规对民营医院有什么影响？",
                          category="multi_hop", max_hops=2)
        self.assertEqual(len(graph["hops"]), 2)
        self._assert_dag(graph)

    def test_empty_question(self):
        graph = decompose("", category="causal")
        self.assertFalse(graph["is_multi_hop"])


if __name__ == "__main__":
    unittest.main()

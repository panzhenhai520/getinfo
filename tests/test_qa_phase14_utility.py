#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 14 · P14-02（`ExpectedGapReduction` 与动作效用）用例。

钉住 §13/§21 的公式**逐项可手算复现**（用例里独立算一遍期望值，不调被测函数算期望）：
  `EGR = route_fit × (0.5 + 0.5 × severity)`
  `Utility = EGR × Importance × Reliability / (Latency + Cost + UserBurden)`
以及：
  1. 契合度**复用 Phase 07 的 `routes_for`**（离首选通道越远折减越多，有下限）；
  2. 权重全部可配（改一个环境变量 → 结果按公式变化，不是玄学）；
  3. 非法输入（未知缺口类型、NaN 优先级、空缺口列表）**不抛错**且如实给 0；
  4. 同输入同输出（两次运行逐字相同）。
"""
import json
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_acquisition as ac  # noqa: E402
import qa_gap_analyzer as ga  # noqa: E402
import qa_phase14_fixtures as fx  # noqa: E402

FLAGS = ("QA_ACQ_W_SAFETY", "QA_ACQ_W_PRIORITY", "QA_ACQ_ROUTE_FIT_STEP",
         "QA_ACQ_UTILITY_FLOOR", "QA_ACQ_MAX_USER_BURDEN")


class _Base(unittest.TestCase):
    def setUp(self):
        self.saved = {name: os.environ.pop(name, None) for name in FLAGS}

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class RouteFitTests(_Base):
    def test_fit_follows_phase07_route_order(self):
        # Phase 07 对 NO_EVIDENCE 的首选通道是 keyword → semantic → web
        self.assertEqual(ga.routes_for("NO_EVIDENCE"), ["keyword", "semantic", "web"])
        first = ac.route_fit("retrieve_document", "NO_EVIDENCE")
        self.assertEqual(first["value"], 1.0)
        self.assertEqual(first["basis"], "GAP_ROUTE_RULES")
        # 语义通道不在任何动作的落点上（retrieve_document 只落 keyword），所以第二个通道
        # 由 `web_search` 承担：它在列表里排第 3 → 1 - 0.15×2 = 0.70
        web = ac.route_fit("web_search", "NO_EVIDENCE")
        self.assertEqual(web["route"], "web")
        self.assertEqual(web["route_index"], 2)
        self.assertAlmostEqual(web["value"], 0.70, places=6)

    def test_fit_is_zero_when_the_route_is_not_suggested(self):
        # 图谱不是 NO_EVIDENCE 的建议通道 → 契合度 0（判 NOT_APPLICABLE_TO_GAP，不硬凑）
        self.assertEqual(ac.route_fit("graph_traverse", "NO_EVIDENCE")["value"], 0.0)
        self.assertEqual(ac.route_fit("query_database", "NO_EVIDENCE")["value"], 0.0)

    def test_fit_has_a_floor(self):
        gap = fx.gap(missing="MISSING_CAUSAL_BRIDGE")
        # MISSING_CAUSAL_BRIDGE 的建议通道是 graph → semantic → keyword
        self.assertEqual(ga.routes_for("MISSING_CAUSAL_BRIDGE"), ["graph", "semantic", "keyword"])
        self.assertEqual(ac.route_fit("graph_traverse", "MISSING_CAUSAL_BRIDGE")["value"], 1.0)
        self.assertEqual(ac.route_fit("retrieve_document", "MISSING_CAUSAL_BRIDGE")["value"], 0.70)
        del gap

    def test_channel_less_actions_use_their_own_tables(self):
        emr = ac.route_fit("retrieve_emr", "MISSING_TIME_LINK")
        self.assertEqual(emr["basis"], "EMR_GAP_FIT")
        self.assertEqual(emr["value"], 0.90)
        ask = ac.route_fit("ask_patient", "AMBIGUOUS_ENTITY")
        self.assertEqual(ask["basis"], "ASKABLE_GAP_FIT")
        self.assertEqual(ask["value"], 0.95)
        # 知识型缺口问人拿不到 → 契合度 0（不该问人）
        self.assertEqual(ac.route_fit("ask_patient", "LOW_AUTHORITY")["value"], 0.0)


class ExpectedGapReductionTests(_Base):
    def test_egr_is_hand_recomputable(self):
        gap = fx.gap(missing="NO_EVIDENCE", priority=0.9)
        egr = ac.expected_gap_reduction("retrieve_document", gap)
        severity = ga.GAP_SEVERITY["NO_EVIDENCE"]                        # 1.00
        expected = 1.0 * (0.5 + 0.5 * severity)                          # 1.0
        self.assertAlmostEqual(egr["value"], expected, places=6)
        self.assertEqual(egr["factors"]["fit"], 1.0)
        self.assertEqual(egr["factors"]["severity"], 1.0)
        self.assertEqual(egr["factors"]["formula"], "fit × (0.5 + 0.5 × severity)")

    def test_severity_weight_keeps_the_documented_range(self):
        for missing in ("NO_EVIDENCE", "AMBIGUOUS_ENTITY", "MISSING_COUNTEREVIDENCE"):
            weight = ac.severity_weight(missing)
            self.assertAlmostEqual(weight, 0.5 + 0.5 * ga.GAP_SEVERITY[missing], places=6)
            self.assertGreaterEqual(weight, 0.5)
            self.assertLessEqual(weight, 1.0)


class UtilityTests(_Base):
    def test_utility_is_hand_recomputable(self):
        gap = fx.gap(missing="NO_EVIDENCE", priority=0.9)
        utility = ac.action_utility("retrieve_document", gap)
        # 手算：importance = 1.0×0.9 + 0.2×0 + 0.1×(0.9×0.6) + 0.05×0.3
        importance = 1.0 * 0.9 + 0.2 * 0.0 + 0.1 * (0.9 * 0.6) + 0.05 * 0.3
        egr = 1.0
        reliability = ac.SOURCE_RELIABILITY["kb"]
        denominator = ac.ACTION_LATENCY["retrieve_document"] + ac.ACTION_COST["retrieve_document"]
        expected = egr * importance * reliability / denominator
        self.assertAlmostEqual(utility["factors"]["importance"], importance, places=6)
        self.assertAlmostEqual(utility["utility"], expected, places=6)
        self.assertEqual(utility["factors"]["formula"],
                         "EGR × Importance × Reliability / (Latency + Cost + UserBurden)")
        self.assertEqual(utility["denominator"], denominator)

    def test_denominator_never_goes_to_zero(self):
        utility = ac.action_utility("ask_patient", fx.gap(missing="AMBIGUOUS_ENTITY"),
                                    burden="none")
        self.assertGreaterEqual(utility["denominator"], ac.utility_epsilon())

    def test_weights_are_configurable(self):
        gap = fx.gap(missing="NO_EVIDENCE", priority=0.9)
        before = ac.importance_of(gap)["value"]
        os.environ["QA_ACQ_W_SAFETY"] = "0.5"
        after = ac.importance_of(gap)["value"]
        # 非安全缺口的安全权重为 0 → 加权重不改变结果（这本身也是一条口径）
        self.assertAlmostEqual(before, after, places=6)
        os.environ["QA_ACQ_W_PRIORITY"] = "0.5"
        halved = ac.importance_of(gap)["value"]
        self.assertAlmostEqual(halved, 0.5 * 0.9 + 0.1 * 0.54 + 0.05 * 0.3, places=6)
        self.assertLess(halved, after)

    def test_all_six_factors_are_recorded(self):
        utility = ac.action_utility("web_search", fx.gap(missing="NO_EVIDENCE"))
        for key in ("expected_gap_reduction", "importance", "evidence_reliability",
                    "latency", "cost", "user_burden"):
            self.assertIn(key, utility["factors"])
        self.assertEqual(sorted(utility["factors"]["importance_factors"]["weights"]),
                         ["clinical_risk", "diagnostic", "priority", "safety"])


class RobustnessTests(_Base):
    def test_unknown_gap_type_is_not_applicable(self):
        # 未知缺口类型 → 记 NOT_APPLICABLE（来源可用时也不会被兜底成关键词检索）
        plan = ac.plan_acquisition([fx.gap(missing="NOT_A_GAP_TYPE")], sources={
            "kb": True, "emr": False, "db": True, "graph": True, "web": True, "patient": False})
        self.assertEqual(plan["selected"], [])
        self.assertTrue(all(row["skip_reason"] in ("NOT_APPLICABLE_TO_GAP", "SOURCE_UNAVAILABLE")
                            for row in plan["candidates"]),
                        plan["skipped"])

    def test_nan_or_missing_priority_is_clamped(self):
        gap = fx.gap()
        gap["priority"] = float("nan")
        importance = ac.importance_of(gap)
        self.assertEqual(importance["factors"]["priority"], 0.0, "NaN 优先级按 0 处理，不许冒泡")
        # 只剩"区分度"这一项加分（0.05 × 0.3）：这是口径，不是意外
        self.assertAlmostEqual(importance["value"], 0.05 * 0.3, places=6)
        self.assertGreaterEqual(ac.action_utility("retrieve_document", gap)["utility"], 0.0)

    def test_unknown_gap_type_gets_no_action(self):
        plan = ac.plan_acquisition([fx.gap(missing="NOT_A_GAP_TYPE")], sources={
            "kb": True, "emr": True, "db": True, "graph": True, "web": True, "patient": True},
            permissions={"patient_contact": True, "emr_read": True})
        self.assertEqual(plan["selected"], [])
        reasons = {row["skip_reason"] for row in plan["candidates"]}
        self.assertEqual(reasons, {"NOT_APPLICABLE_TO_GAP"},
                         "未知缺口类型不许被当成关键词检索（也不许被悄悄兜底）")

    def test_empty_gaps_give_an_empty_plan(self):
        plan = ac.plan_acquisition([])
        self.assertEqual(plan["candidates"], [])
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["stats"]["gaps"], 0)
        self.assertTrue(plan["contract_ok"], plan.get("contract_error"))

    def test_unknown_action_is_ignored(self):
        plan = ac.plan_acquisition([fx.gap()], actions=["retrieve_document", "teleport"])
        self.assertEqual([row["action"] for row in plan["candidates"]],
                         ["retrieve_document"])

    def test_same_input_same_output(self):
        gaps = [fx.gap(gap_id="G1"), fx.gap(gap_id="G2", missing="LOW_AUTHORITY", priority=0.8)]
        first = json.dumps(ac.plan_acquisition(gaps), ensure_ascii=False, sort_keys=True)
        second = json.dumps(ac.plan_acquisition(gaps), ensure_ascii=False, sort_keys=True)
        self.assertEqual(first, second, "同输入必须同输出（可复算）")


if __name__ == "__main__":
    unittest.main()

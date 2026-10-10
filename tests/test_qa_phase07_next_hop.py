#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 07 · P07-03（Next-hop Planner）+ P07-04（seen dedupe）用例。

钉住：
  1. 下一跳 = 缺口 + 最优检索动作（§13）：route 取缺口建议的第一条，且 route 真的改变
     **检索计划**（queries/entities/terms 三通道各有落点），不是只写个字符串；
  2. 可插拔注入点：注册的后端优先；**未注册 / 抛错 / 返回非法载荷**三条失败路径都保守回落到
     规则实现并如实记账（绝不假装规划过）；后端合法返回空列表是"它的决定"，不许悄悄补跳；
  3. seen 去重（§24 + MASTER_RULES 第 14 条）：同批重复、与已搜指纹相同、来源已见且被拒 —— 三种
     都拦下并留痕；Query Fingerprint 含 route/约束/语料版本/检索配置（任一变化即不同指纹）；
  4. 所有产出的下一跳都过 `next_hop` 契约。
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
from qa_phase07_fixtures import graph_claim, plan, plan_claim  # noqa: E402

CLAIM_TEXT = "2026年医保新规要求民营医院按病种付费"


def _gaps(*, plan_only=True, **kwargs):
    claims = [plan_claim("c1", CLAIM_TEXT) if plan_only else graph_claim("c1", text=CLAIM_TEXT)]
    return gap.detect_gaps(claims, **kwargs)


class PlannerTests(unittest.TestCase):
    def test_rule_planner_turns_a_gap_into_one_hop(self):
        result = _gaps(plan=plan(category="multi_hop"))
        planned = gap.plan_next_hops(result["gaps"], plan=plan(category="multi_hop"),
                                     claims=[plan_claim("c1", CLAIM_TEXT)])
        self.assertEqual(planned["planner"], "rule")
        self.assertFalse(planned["fallback"]["used"])
        hop = planned["hops"][0]
        self.assertEqual(hop["gap_id"], result["gaps"][0]["gap_id"])
        self.assertEqual(hop["route"], result["gaps"][0]["suggested_routes"][0])
        self.assertTrue(hop["queries"])
        self.assertTrue(hop["query_fingerprint"])
        ok, note = validate("next_hop", hop)
        self.assertTrue(ok, note)

    def test_route_really_changes_the_retrieval_plan(self):
        """§13 的 route 不是装饰：结构化/属性通道按实体查，语义通道带词表扩展，图谱通道带实体+关系词。"""
        claim = plan_claim("c1", CLAIM_TEXT)
        baseline = _gaps(plan=plan(category="multi_hop"))
        for route in (contracts.QA_ROUTE_KEYWORD, contracts.QA_ROUTE_SEMANTIC,
                      contracts.QA_ROUTE_GRAPH, contracts.QA_ROUTE_POLICY_EXACT,
                      contracts.QA_ROUTE_WEB, contracts.QA_ROUTE_PAGE_CONTEXT):
            gaps = [dict(item) for item in baseline["gaps"]]
            gaps[0]["suggested_routes"] = [route]
            planned = gap.plan_next_hops(gaps, plan=plan(category="multi_hop"), claims=[claim])
            hop = planned["hops"][0]
            self.assertEqual(hop["route"], route)
            overrides = hop["plan_overrides"]
            self.assertEqual(overrides["route"], route)
            self.assertTrue(overrides["queries"], "%s 没有给出查询" % route)
            if route in (contracts.QA_ROUTE_GRAPH, contracts.QA_ROUTE_GRAPH_ATTRIBUTE,
                         contracts.QA_ROUTE_POLICY_EXACT):
                self.assertTrue(overrides["entities"], "%s 必须带实体（否则结构化/图通道搜不到）"
                                % route)
                for entity in overrides["entities"]:
                    self.assertIn(entity, list(plan(category="multi_hop")["entities"]))
            for hunter in hop["hunters"]:
                self.assertIn(hunter, contracts.QA_HUNTER_IDS)

    def test_limit_and_zero_cap(self):
        result = _gaps(plan=plan(category="multi_hop"))
        self.assertEqual(len(gap.plan_next_hops(result["gaps"], limit=0)["hops"]), 0)
        self.assertEqual(len(gap.plan_next_hops(result["gaps"], limit=1)["hops"]), 1)

    def test_no_gaps_means_no_hops(self):
        planned = gap.plan_next_hops([], limit=1)
        self.assertEqual(planned["hops"], [])
        self.assertEqual(planned["dedupe"]["dropped_count"], 0)

    def test_hop_priority_follows_the_gap_priority(self):
        result = _gaps(plan=plan(category="multi_hop"))
        top = result["gaps"][0]
        planned = gap.plan_next_hops(result["gaps"], limit=1)
        self.assertAlmostEqual(planned["hops"][0]["priority"], top["priority"], places=4)


class InjectionPointTests(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.pop("QA_NEXT_HOP_PLANNER", None)
        self.claim = plan_claim("c1", CLAIM_TEXT)
        self.gaps = _gaps(plan=plan(category="multi_hop"))["gaps"]

    def tearDown(self):
        os.environ.pop("QA_NEXT_HOP_PLANNER", None)
        if self.saved is not None:
            os.environ["QA_NEXT_HOP_PLANNER"] = self.saved

    def test_registered_backend_is_used(self):
        calls = []

        def backend(gaps, **kwargs):
            calls.append(kwargs)
            return [{"gap_id": gaps[0]["gap_id"], "question": "自定义下一跳",
                     "queries": ["自定义查询"], "route": contracts.QA_ROUTE_SEMANTIC}]

        gap.register_next_hop_planner("t07", backend)
        os.environ["QA_NEXT_HOP_PLANNER"] = "t07"
        try:
            planned = gap.plan_next_hops(self.gaps, claims=[self.claim])
        finally:
            os.environ.pop("QA_NEXT_HOP_PLANNER", None)
        self.assertEqual(planned["planner"], "registered:t07")
        self.assertEqual(planned["hops"][0]["question"], "自定义下一跳")
        self.assertEqual(planned["hops"][0]["route"], contracts.QA_ROUTE_SEMANTIC)
        self.assertEqual(calls[0]["round_index"], 0)

    def test_unregistered_backend_falls_back_and_says_so(self):
        os.environ["QA_NEXT_HOP_PLANNER"] = "不存在"
        planned = gap.plan_next_hops(self.gaps, claims=[self.claim])
        self.assertTrue(planned["fallback"]["used"])
        self.assertIn("未注册", planned["fallback"]["reason"])
        self.assertEqual(planned["planner"], "rule")
        self.assertTrue(planned["hops"], "回落之后必须真的规划出下一跳")

    def test_backend_error_falls_back_and_says_so(self):
        def broken(gaps, **kwargs):
            raise RuntimeError("规划器炸了")

        gap.register_next_hop_planner("t07-broken", broken)
        os.environ["QA_NEXT_HOP_PLANNER"] = "t07-broken"
        try:
            planned = gap.plan_next_hops(self.gaps, claims=[self.claim])
        finally:
            os.environ.pop("QA_NEXT_HOP_PLANNER", None)
        self.assertTrue(planned["fallback"]["used"])
        self.assertIn("RuntimeError", planned["fallback"]["reason"])
        self.assertTrue(planned["hops"])

    def test_backend_returning_garbage_falls_back(self):
        gap.register_next_hop_planner("t07-garbage", lambda gaps, **kwargs: "不是列表")
        os.environ["QA_NEXT_HOP_PLANNER"] = "t07-garbage"
        try:
            planned = gap.plan_next_hops(self.gaps, claims=[self.claim])
        finally:
            os.environ.pop("QA_NEXT_HOP_PLANNER", None)
        self.assertTrue(planned["fallback"]["used"])
        self.assertIn("非法载荷", planned["fallback"]["reason"])
        self.assertTrue(planned["hops"])

    def test_backend_missing_required_fields_falls_back(self):
        gap.register_next_hop_planner("t07-partial",
                                      lambda gaps, **kwargs: [{"question": "只有问题"}])
        os.environ["QA_NEXT_HOP_PLANNER"] = "t07-partial"
        try:
            planned = gap.plan_next_hops(self.gaps, claims=[self.claim])
        finally:
            os.environ.pop("QA_NEXT_HOP_PLANNER", None)
        self.assertTrue(planned["fallback"]["used"])
        self.assertTrue(planned["hops"])

    def test_valid_empty_result_is_respected(self):
        """后端合法地说"没有可发的下一跳" → 尊重它（循环据此走向 NO_GAIN），不许悄悄补跳。"""
        gap.register_next_hop_planner("t07-empty", lambda gaps, **kwargs: [])
        os.environ["QA_NEXT_HOP_PLANNER"] = "t07-empty"
        try:
            planned = gap.plan_next_hops(self.gaps, claims=[self.claim])
        finally:
            os.environ.pop("QA_NEXT_HOP_PLANNER", None)
        self.assertEqual(planned["hops"], [])
        self.assertFalse(planned["fallback"]["used"])

    def test_explicit_planner_argument_wins(self):
        planned = gap.plan_next_hops(self.gaps, claims=[self.claim],
                                     planner=lambda gaps, **kwargs: [
                                         {"gap_id": gaps[0]["gap_id"], "question": "注入",
                                          "queries": ["注入查询"], "route": ""}])
        self.assertEqual(planned["planner"], "rule")
        self.assertEqual(planned["hops"][0]["question"], "注入")
        self.assertEqual(planned["hops"][0]["route"], contracts.QA_ROUTE_KEYWORD,
                         "非法 route 必须归一到 keyword（不许写进 SearchTrace）")

    def test_register_rejects_bad_arguments(self):
        with self.assertRaises(ValueError):
            gap.register_next_hop_planner("", lambda gaps, **kwargs: [])
        with self.assertRaises(ValueError):
            gap.register_next_hop_planner("t07-x", None)


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_covers_the_four_spec_components(self):
        base = gap.query_fingerprint("医保新规", route=contracts.QA_ROUTE_KEYWORD,
                                     constraints=["民营医院"], corpus_version="c1",
                                     retrieval_config="v3")
        self.assertEqual(base, gap.query_fingerprint("  医保新规  ",
                                                     route=contracts.QA_ROUTE_KEYWORD,
                                                     constraints=["民营医院"],
                                                     corpus_version="c1",
                                                     retrieval_config="v3"),
                         "空白折叠后必须是同一个指纹")
        self.assertNotEqual(base, gap.query_fingerprint("医保新规",
                                                        route=contracts.QA_ROUTE_SEMANTIC,
                                                        constraints=["民营医院"],
                                                        corpus_version="c1",
                                                        retrieval_config="v3"))
        self.assertNotEqual(base, gap.query_fingerprint("医保新规",
                                                        route=contracts.QA_ROUTE_KEYWORD,
                                                        constraints=["民营医院"],
                                                        corpus_version="c2",
                                                        retrieval_config="v3"))
        self.assertNotEqual(base, gap.query_fingerprint("医保新规",
                                                        route=contracts.QA_ROUTE_KEYWORD,
                                                        constraints=["民营医院"],
                                                        corpus_version="c1",
                                                        retrieval_config="v4"))

    def test_constraint_order_does_not_matter(self):
        one = gap.query_fingerprint("q", route="keyword", constraints=["a", "b"])
        two = gap.query_fingerprint("q", route="keyword", constraints=["b", "a"])
        self.assertEqual(one, two)
        three = gap.query_fingerprint("q", route="keyword", constraints=["a"])
        self.assertNotEqual(one, three)


class DedupeTests(unittest.TestCase):
    def _hop(self, question="医保新规 依据", route=contracts.QA_ROUTE_KEYWORD,
             evidence_refs=None):
        hop = {"hop_id": "g1", "gap_id": "G1", "question": question, "queries": [question],
               "route": route}
        if evidence_refs is not None:
            hop["evidence_refs"] = list(evidence_refs)
        return hop

    def test_same_batch_duplicates_are_dropped(self):
        kept, audit = gap.dedupe_next_hops([self._hop(), self._hop()])
        self.assertEqual(len(kept), 1)
        self.assertEqual(audit["dropped_count"], 1)
        self.assertIn("seen_query", audit["dropped"][0]["reason"])

    def test_already_searched_query_is_dropped(self):
        first, _ = gap.dedupe_next_hops([self._hop()])
        fingerprint = first[0]["query_fingerprint"]
        kept, audit = gap.dedupe_next_hops([self._hop()], seen_queries=[fingerprint])
        self.assertEqual(kept, [])
        self.assertEqual(audit["dropped_count"], 1)
        self.assertEqual(audit["seen_queries"], 1)

    def test_same_question_different_route_is_not_a_duplicate(self):
        first, _ = gap.dedupe_next_hops([self._hop()])
        kept, audit = gap.dedupe_next_hops(
            [self._hop(route=contracts.QA_ROUTE_SEMANTIC)],
            seen_queries=[first[0]["query_fingerprint"]])
        self.assertEqual(len(kept), 1, "换通道是**不同的检索动作**，不该被当成重复")
        self.assertEqual(audit["dropped_count"], 0)

    def test_seen_rejected_source_is_not_searched_again(self):
        """MASTER_RULES 第 14 条：被拒证据仍属于 seen，不许下一跳又去捞同一批。"""
        hop = self._hop()
        hop["evidence_refs"] = ["source:a", "source:b"]
        kept, audit = gap.dedupe_next_hops([hop], seen_sources=["source:a", "source:b"])
        self.assertEqual(kept, [])
        self.assertIn("seen_rejected_source", audit["dropped"][0]["reason"])
        # 只有一部分已见被拒 → 仍然可以搜（别把有效重检也砍掉）
        kept2, _ = gap.dedupe_next_hops([self._hop()], seen_sources=["source:a"])
        self.assertEqual(len(kept2), 1)

    def test_audit_carries_the_shared_mechanism(self):
        _, audit = gap.dedupe_next_hops([self._hop(), self._hop()])
        self.assertIn("qa_evidence_seen", audit["mode"],
                      "去重必须复用 Phase 02 的 seen 机制（不许另造一套）")
        self.assertEqual(audit["checked"], 2)
        self.assertEqual(audit["kept"], 1)

    def test_planner_applies_dedupe(self):
        result = _gaps(plan=plan(category="multi_hop"))
        first = gap.plan_next_hops(result["gaps"], limit=1)
        fingerprint = first["hops"][0]["query_fingerprint"]
        second = gap.plan_next_hops(result["gaps"], limit=1, seen_queries=[fingerprint])
        self.assertEqual(second["hops"], [])
        self.assertEqual(second["dedupe"]["dropped_count"], 1)


if __name__ == "__main__":
    unittest.main()

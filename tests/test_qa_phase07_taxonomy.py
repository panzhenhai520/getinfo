#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 07 · P07-01（Gap taxonomy/priority）+ P07-02（route/证据要求）用例。

钉住：
  1. 十种 Gap 类型**逐字**取自 §12，且每种都有严重度/建议通道/证据类型/查询模板（不留半成品）；
  2. 缺口**不是另算的**：Phase 03 的核验理由码能派生出对应类型（`derived_from_reasons` 可追溯）；
  3. 优先级可复算：`priority_factors` 里的三个分量按权重加权必须等于 `priority`（安全关键走 §21 覆盖）；
  4. `suggested_routes` 只吃既有 7 个通道值（不动冻结枚举）；证据要求带上"缺到什么程度"。
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
    contradiction, evidence_item, graph_claim, graph_edge, plan, plan_claim, verification,
)

TAXONOMY_IN_SPEC = ("NO_EVIDENCE", "LOW_RELEVANCE", "LOW_AUTHORITY", "SINGLE_SOURCE",
                    "MISSING_ENTITY_LINK", "MISSING_TIME_LINK", "CONTRADICTION",
                    "AMBIGUOUS_ENTITY", "MISSING_CAUSAL_BRIDGE", "MISSING_COUNTEREVIDENCE")

CLAIM_TEXT = "2026年医保新规要求民营医院按病种付费"


def _types(gaps):
    return sorted({item["missing"] for item in gaps})


class TaxonomyTests(unittest.TestCase):
    def test_ten_types_are_verbatim_from_the_spec(self):
        self.assertEqual(tuple(contracts.QA_GAP_TYPES), TAXONOMY_IN_SPEC)
        self.assertEqual(len(contracts.QA_GAP_TYPES), 10)

    def test_every_type_has_severity_routes_evidence_type_and_query_template(self):
        for name in contracts.QA_GAP_TYPES:
            self.assertIn(name, gap.GAP_SEVERITY, "%s 没有严重度" % name)
            self.assertTrue(0.0 < gap.GAP_SEVERITY[name] <= 1.0)
            routes = gap.GAP_ROUTE_RULES.get(name)
            self.assertTrue(routes, "%s 没有建议通道" % name)
            for route in routes:
                self.assertIn(route, contracts.QA_RETRIEVAL_ROUTES,
                              "%s 的建议通道 %s 不在冻结枚举里" % (name, route))
            self.assertIn(name, gap.EVIDENCE_TYPE_BY_GAP, "%s 没有证据类型" % name)
            self.assertIn(name, gap.QUERY_SUFFIX_BY_GAP, "%s 没有查询模板" % name)
            requirement = gap.evidence_requirement_for(name)
            ok, note = validate("evidence_requirement", requirement)
            self.assertTrue(ok, note)


class DetectorTests(unittest.TestCase):
    def test_no_evidence(self):
        result = gap.detect_gaps([graph_claim("c1", text=CLAIM_TEXT, scope=())])
        self.assertEqual(_types(result["gaps"]), ["NO_EVIDENCE"])
        item = result["gaps"][0]
        self.assertGreaterEqual(item["priority"], 0.9)
        self.assertEqual(item["band"], "critical")
        self.assertEqual(item["evidence_requirement"]["evidence_type"], "any_evidence")
        self.assertEqual(item["evidence_matched"], 0)
        ok, note = validate("gap", item)
        self.assertTrue(ok, note)

    def test_reason_codes_drive_the_taxonomy(self):
        """Phase 03 说"重叠不足" → 这里就记 LOW_RELEVANCE（口径不许两处打架）。"""
        claim = graph_claim("c1", text=CLAIM_TEXT, pairs=[
            {"evidence_ref": "article:1", "verdict": "UNVERIFIED",
             "reasons": ["insufficient_overlap"], "score": 0.1, "entailment": 0.1}])
        result = gap.detect_gaps([claim], evidence=[evidence_item("article:1")])
        types = _types(result["gaps"])
        self.assertIn("LOW_RELEVANCE", types)
        item = [g for g in result["gaps"] if g["missing"] == "LOW_RELEVANCE"][0]
        self.assertEqual(item["derived_from_reasons"], ["insufficient_overlap"])

    def test_low_authority_needs_a_real_authority_problem(self):
        claim = graph_claim("c1", text=CLAIM_TEXT, pairs=[
            {"evidence_ref": "article:1", "verdict": "UNVERIFIED",
             "reasons": ["low_authority_source"], "score": 0.1, "entailment": 0.6}])
        result = gap.detect_gaps([claim], evidence=[evidence_item("article:1", authority=5)])
        self.assertIn("LOW_AUTHORITY", _types(result["gaps"]))
        # 权威没有问题（官方原文 100）时不许凭空记一条
        clean = graph_claim("c2", text=CLAIM_TEXT, verified_support_count=1,
                            independent_sources=2, verified_support_mass=0.9)
        edges = [graph_edge("c2", "article:2", authority=100, source_id="s1"),
                 graph_edge("c2", "article:3", authority=100, source_id="s2")]
        result2 = gap.detect_gaps([clean], edges=edges,
                                  evidence=[evidence_item("article:2", authority=100),
                                            evidence_item("article:3", authority=100)])
        self.assertNotIn("LOW_AUTHORITY", _types(result2["gaps"]))

    def test_single_source_uses_independent_sources(self):
        claim = graph_claim("c1", text=CLAIM_TEXT, verified_support_count=1,
                            independent_sources=1, verified_support_mass=0.7)
        edges = [graph_edge("c1", "article:1", authority=100, source_id="s1")]
        result = gap.detect_gaps([claim], edges=edges,
                                 evidence=[evidence_item("article:1", authority=100)])
        types = _types(result["gaps"])
        self.assertIn("SINGLE_SOURCE", types)
        item = [g for g in result["gaps"] if g["missing"] == "SINGLE_SOURCE"][0]
        self.assertEqual(item["evidence_requirement"]["min_independent_sources"], 2)
        self.assertEqual(item["evidence_requirement"]["evidence_type"], "independent_source")

    def test_entity_and_time_links_come_from_verifier_reasons(self):
        claim = graph_claim("c1", text=CLAIM_TEXT, pairs=[
            {"evidence_ref": "article:1", "verdict": "UNVERIFIED",
             "reasons": ["entity_not_in_evidence", "evidence_after_window"],
             "score": 0.1, "entailment": 0.5}])
        result = gap.detect_gaps([claim], evidence=[evidence_item("article:1")])
        types = _types(result["gaps"])
        self.assertIn("MISSING_ENTITY_LINK", types)
        self.assertIn("MISSING_TIME_LINK", types)

    def test_contradiction_from_support_and_refute(self):
        claim = graph_claim("c1", text=CLAIM_TEXT, verified_support_count=1,
                            verified_refute_count=1, independent_sources=2,
                            verified_support_mass=0.7, refute_mass=0.6)
        edges = [graph_edge("c1", "article:1", source_id="s1", authority=100),
                 graph_edge("c1", "article:2", relation="REFUTES", status="REFUTED",
                            source_id="s2", authority=100)]
        result = gap.detect_gaps([claim], edges=edges,
                                 evidence=[evidence_item("article:1", authority=100),
                                           evidence_item("article:2", authority=100)])
        self.assertIn("CONTRADICTION", _types(result["gaps"]))

    def test_contradiction_from_phase06_unresolved_decision(self):
        claim = graph_claim("c1", text=CLAIM_TEXT, verified_support_count=2,
                            independent_sources=2, verified_support_mass=0.9)
        edges = [graph_edge("c1", "article:1", source_id="s1", authority=100),
                 graph_edge("c1", "article:2", source_id="s2", authority=100)]
        decision = contradiction("x1", ["c1"])
        result = gap.detect_gaps([claim], edges=edges, contradictions=[decision],
                                 evidence=[evidence_item("article:1", authority=100),
                                           evidence_item("article:2", authority=100)])
        item = [g for g in result["gaps"] if g["missing"] == "CONTRADICTION"][0]
        self.assertEqual(item["contradiction"]["reason_code"], "NO_DECISIVE_RULE")
        self.assertEqual(item["contradiction"]["contradiction_id"], "x1")

    def test_ambiguous_entity_needs_two_planned_entities(self):
        claim = graph_claim("c1", text="医保与卫健部门联合发布新规", scope=())
        result = gap.detect_gaps([claim], plan=plan(entities=("医保", "卫健")),
                                 evidence=[evidence_item("article:1", text="医保与卫健部门联合发布新规",
                                                         authority=100)])
        self.assertIn("AMBIGUOUS_ENTITY", _types(result["gaps"]))
        # 没有计划实体 → 没有依据就不下结论
        bare = gap.detect_gaps([graph_claim("c1", text="医保与卫健部门联合发布新规", scope=())],
                               evidence=[evidence_item("article:1", authority=100)])
        self.assertNotIn("AMBIGUOUS_ENTITY", _types(bare["gaps"]))

    def test_no_evidence_short_circuits_the_other_rules(self):
        """一条证据都没有时只记 NO_EVIDENCE：那条缺口是唯一可下手的地方，别拿噪声凑数。"""
        claim = graph_claim("c1", text="医保与卫健部门联合发布新规", scope=())
        result = gap.detect_gaps([claim], plan=plan(entities=("医保", "卫健"), category="causal"))
        self.assertEqual(_types(result["gaps"]), ["NO_EVIDENCE"])

    def test_causal_bridge_and_counterevidence_follow_the_question_type(self):
        causal = gap.detect_gaps(
            [plan_claim("c1", "医保新规导致民营医院成本上升")],
            evidence=[evidence_item("article:1", text="医保新规要求按病种付费")],
            plan=plan(category="causal"))
        self.assertIn("MISSING_CAUSAL_BRIDGE", _types(causal["gaps"]))
        counter = gap.detect_gaps(
            [plan_claim("c1", "医保新规对民营医院的影响")],
            evidence=[evidence_item("article:1", text="医保新规要求按病种付费",
                                    verification=verification("SUPPORTED"))],
            plan=plan(category="comparison"))
        self.assertIn("MISSING_COUNTEREVIDENCE", _types(counter["gaps"]))

    def test_plan_claim_matching_is_lexical_and_labelled(self):
        """计划 claim 没有边也没有核验 → 按词面相关性现选，并在 origin 里标出来（不冒充核验）。"""
        result = gap.detect_gaps(
            [plan_claim("c1", CLAIM_TEXT)],
            evidence=[evidence_item("article:1", text=CLAIM_TEXT + "，细则另行发布")])
        item = result["gaps"][0]
        self.assertEqual(item["origin"], "lexical_pool")
        self.assertGreater(item["evidence_matched"], 0)


class PriorityTests(unittest.TestCase):
    def test_priority_is_recomputable_from_its_factors(self):
        result = gap.detect_gaps([graph_claim("c1", text=CLAIM_TEXT)])
        item = result["gaps"][0]
        factors = item["priority_factors"]
        weights = factors["weights"]
        expected = (weights["severity"] * factors["severity"]
                    + weights["claim_importance"] * factors["claim_importance"]
                    + weights["evidence_deficit"] * factors["evidence_deficit"])
        self.assertAlmostEqual(item["priority"], min(1.0, expected), places=4)
        self.assertIn("clamp", factors["formula"])

    def test_safety_critical_overrides_priority(self):
        """§21：SafetyCritical → Priority Override（不是加一点点权重，是抬到安全档）。"""
        result = gap.detect_gaps([graph_claim("c1", text="某药企因质量问题被处罚并召回产品",
                                              scope=())])
        item = result["gaps"][0]
        self.assertTrue(item["safety_critical"])
        self.assertGreaterEqual(item["priority"], contracts.SAFETY_OVERRIDE_PRIORITY)
        self.assertEqual(item["band"], "critical")

    def test_bands_are_monotone(self):
        threshold = gap.priority_threshold()
        self.assertEqual(gap.band_of(1.0), "critical")
        self.assertEqual(gap.band_of(threshold), "high")
        self.assertEqual(gap.band_of(threshold - 0.01), "medium")
        self.assertEqual(gap.band_of(0.2), "low")
        self.assertTrue(gap.is_high_priority(threshold))
        self.assertFalse(gap.is_high_priority(threshold - 0.01))

    def test_priority_threshold_is_configurable(self):
        os.environ["QA_GAP_PRIORITY_THRESHOLD"] = "0.2"
        try:
            self.assertEqual(gap.priority_threshold(), 0.2)
            self.assertEqual(gap.band_of(0.3), "high")
        finally:
            os.environ.pop("QA_GAP_PRIORITY_THRESHOLD", None)

    def test_caps_and_truncation_are_accounted(self):
        claims = [graph_claim("c%d" % index, text="%s 第%d条" % (CLAIM_TEXT, index))
                  for index in range(10)]
        os.environ["QA_GAP_MAX_GAPS"] = "3"
        os.environ["QA_GAP_MAX_GAPS_PER_CLAIM"] = "1"
        try:
            result = gap.detect_gaps(claims)
        finally:
            os.environ.pop("QA_GAP_MAX_GAPS", None)
            os.environ.pop("QA_GAP_MAX_GAPS_PER_CLAIM", None)
        self.assertEqual(len(result["gaps"]), 3)
        self.assertGreater(result["stats"]["truncated"], 0)
        self.assertEqual(result["stats"]["gaps_total_before_cap"], 10)

    def test_detection_is_deterministic(self):
        claims = [graph_claim("c1", text=CLAIM_TEXT), graph_claim("c2", text=CLAIM_TEXT + " 二")]
        first = gap.detect_gaps(claims)
        second = gap.detect_gaps(claims)
        self.assertEqual([g["gap_id"] for g in first["gaps"]],
                         [g["gap_id"] for g in second["gaps"]])
        self.assertEqual(first["gaps"], second["gaps"])

    def test_gap_ids_are_content_addressed(self):
        one = gap.detect_gaps([graph_claim("c1", text=CLAIM_TEXT)])["gaps"][0]
        again = gap.detect_gaps([graph_claim("c1", text=CLAIM_TEXT)])["gaps"][0]
        other = gap.detect_gaps([graph_claim("c2", text=CLAIM_TEXT)])["gaps"][0]
        self.assertEqual(one["gap_id"], again["gap_id"])
        self.assertNotEqual(one["gap_id"], other["gap_id"])


class SuggestionTests(unittest.TestCase):
    def test_routes_are_always_inside_the_frozen_enum(self):
        for name in contracts.QA_GAP_TYPES:
            for route in gap.routes_for(name):
                self.assertIn(route, contracts.QA_RETRIEVAL_ROUTES)
        self.assertEqual(gap.routes_for("不存在的类型"), [contracts.QA_ROUTE_KEYWORD])

    def test_hunters_reuse_the_phase04_identities(self):
        self.assertEqual(gap.hunters_for(contracts.QA_ROUTE_KEYWORD), ["bm25"])
        self.assertEqual(gap.hunters_for(contracts.QA_ROUTE_POLICY_EXACT), ["structured"])
        for route in contracts.QA_RETRIEVAL_ROUTES:
            for hunter in gap.hunters_for(route):
                self.assertIn(hunter, contracts.QA_HUNTER_IDS)

    def test_suggested_queries_are_rule_based_and_clean(self):
        queries = gap.suggest_queries({"text": "是否存在反证或替代解释（§7 的 H5：必须独立来源）",
                                       "scope": ["民营医院"]},
                                      contracts.QA_GAP_MISSING_COUNTEREVIDENCE)
        self.assertTrue(queries)
        self.assertNotIn("（§7 的 H5", queries[0], "句尾括注要清掉（它进查询只会污染召回）")
        self.assertIn("风险", queries[1])
        # 空文本 → 不编查询
        self.assertEqual(gap.suggest_queries({"text": ""}, "NO_EVIDENCE"), [])

    def test_evidence_requirement_never_self_claims_satisfaction(self):
        for name in contracts.QA_GAP_TYPES:
            requirement = gap.evidence_requirement_for(name)
            self.assertEqual(requirement["satisfied_by"], "",
                             "满足度由下一轮缺口重算证明，不许在这里自证")
            self.assertEqual(requirement["expected_evaluation"], "next_round_gap_recompute")
            self.assertTrue(requirement["require_verified"])

    def test_evidence_requirement_is_stable_for_the_same_claim(self):
        claim = graph_claim("c1", text=CLAIM_TEXT)
        first = gap.evidence_requirement_for("NO_EVIDENCE", gap_claim=claim)
        second = gap.evidence_requirement_for("NO_EVIDENCE", gap_claim=claim)
        self.assertEqual(first["requirement_id"], second["requirement_id"])


if __name__ == "__main__":
    unittest.main()

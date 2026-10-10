#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 06 · P06-02 四类关系 + P06-03 claim coverage（单元用例）。

钉住的东西：
  1. **关系由核验 verdict 派生**：SUPPORTED→SUPPORTS / REFUTED→REFUTES / QUALIFIED→SUPPORTS
     （带 qualified 标记与强度折扣）/ CONTEXT、UNVERIFIED→MENTIONS；claim 级核验 pairs 优先于
     证据条目上的 relationship；
  2. **口径不与核验层打架**：`claim_status_from_edges()` 与 `qa_verifier._claim_status()`
     对同一批 verdict 必须给出同一个状态，建层时逐 claim 自检（mismatches=0）；
  3. **没核验就不算支持**：只有 relationship=supports、没有任何核验结论时，关系只能是 MENTIONS
     且 `verified=False`（MASTER_RULES 第 11 条：LLM 自由生成内容不能变成已验证证据）；
  4. **claim coverage 口径**：主口径只数已核验支持、分母是非 planned claim；带权口径按
     §11 分数（单条 claim 饱和于 1）；计划 claim（plan_only）不进分母；
  5. **边是机器可校验契约**：每条边都过 `validate("evidence_graph_edge")`，且关系与端点种类
     的组合必须合法（`EVIDENCE_GRAPH_RELATIONS_BY_KIND`）。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_evidence_graph as eg  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_verifier as verifier  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402
from qa_phase06_fixtures import claim_node, evidence, graph_of  # noqa: E402


def qa_contracts_relationship_enum():
    import qa_contracts

    values = qa_contracts.EVIDENCE_SCHEMA["properties"]["relationship"]["enum"]
    return [str(item) for item in values if item]


class RelationMappingTests(unittest.TestCase):
    def test_phase01_evidence_relationship_enum_is_untouched_and_aligned(self):
        """P06-02 的"对齐"是断言出来的：Phase 01 枚举必须与冻结证据契约的枚举一致。"""
        frozen = qa_contracts_relationship_enum()
        self.assertEqual(sorted(contracts.CLAIM_EVIDENCE_RELATIONSHIPS), sorted(frozen))
        self.assertEqual(len(contracts.CLAIM_EVIDENCE_RELATIONSHIPS), 4,
                         "证据条目级枚举不许被 Phase 06 扩张（它镜像冻结的 EVIDENCE_SCHEMA）")

    def test_graph_relation_mapping_covers_every_evidence_status(self):
        for status in contracts.EVIDENCE_STATUSES:
            self.assertIn(status, contracts.EVIDENCE_GRAPH_RELATION_BY_STATUS)
        mapped = set(contracts.EVIDENCE_GRAPH_RELATION_BY_STATUS.values())
        self.assertTrue(mapped <= set(contracts.EVIDENCE_GRAPH_RELATIONSHIPS))

    def test_the_four_required_relations_exist(self):
        for relation in ("SUPPORTS", "REFUTES", "DEPENDS", "CONTRADICTS"):
            self.assertIn(relation, contracts.EVIDENCE_GRAPH_RELATIONSHIPS)

    def test_relations_are_legal_only_on_declared_endpoints(self):
        for kind, allowed in contracts.EVIDENCE_GRAPH_RELATIONS_BY_KIND.items():
            self.assertIn(kind, contracts.EVIDENCE_GRAPH_EDGE_KINDS)
            self.assertTrue(set(allowed) <= set(contracts.EVIDENCE_GRAPH_RELATIONSHIPS))
        self.assertEqual(contracts.EVIDENCE_GRAPH_RELATIONS_BY_KIND["claim-evidence"],
                         ("SUPPORTS", "REFUTES", "MENTIONS"))
        self.assertEqual(contracts.EVIDENCE_GRAPH_RELATIONS_BY_KIND["claim-claim"],
                         ("DEPENDS", "CONTRADICTS"))

    def test_relation_for_status_never_invents_support(self):
        self.assertEqual(eg.relation_for_status("SUPPORTED"), "SUPPORTS")
        self.assertEqual(eg.relation_for_status("REFUTED"), "REFUTES")
        self.assertEqual(eg.relation_for_status("CONTEXT"), "MENTIONS")
        self.assertEqual(eg.relation_for_status("UNVERIFIED"), "MENTIONS")
        self.assertEqual(eg.relation_for_status("something-new"), "MENTIONS")
        self.assertEqual(eg.relation_for_status(""), "MENTIONS")


def qa_contracts_relationship_enum():
    import qa_contracts

    values = qa_contracts.EVIDENCE_SCHEMA["properties"]["relationship"]["enum"]
    return [str(item) for item in values if item]


class EdgeStatusTests(unittest.TestCase):
    def test_claim_level_pairs_win_over_the_evidence_relationship(self):
        """证据条目写着 supports，但 claim 级核验判 REFUTED → 图里的关系必须是 REFUTES。"""
        node = claim_node("c1", refs=["a"], pairs=[
            {"evidence_ref": "a", "verdict": "REFUTED", "score": 0.4, "reasons": ["negation_flip"]}])
        info = eg.edge_status(node, "a", evidence("a", relation="supports"))
        self.assertEqual(info["status"], "REFUTED")
        self.assertEqual(info["verification_basis"], "verifier_pairs")
        self.assertTrue(info["verified"])
        self.assertEqual(eg.relation_for_status(info["status"]), "REFUTES")

    def test_evidence_layer_verification_is_the_second_basis(self):
        node = claim_node("c1", refs=["a"], verification=False)
        item = evidence("a", verification={"verdict": "SUPPORTED", "score": 0.66})
        info = eg.edge_status(node, "a", item)
        self.assertEqual(info["status"], "SUPPORTED")
        self.assertEqual(info["verification_basis"], "evidence_layer_verification")
        self.assertTrue(info["verified"])
        self.assertAlmostEqual(info["score"], 0.66)

    def test_unverified_relationship_is_only_a_claim(self):
        """只有 relationship=supports、没有任何核验 → 关系只能是 MENTIONS + verified=False。"""
        node = claim_node("c1", refs=["a"], verification=False)
        info = eg.edge_status(node, "a", evidence("a", relation="supports"))
        self.assertEqual(info["verification_basis"], "relationship")
        self.assertFalse(info["verified"])
        self.assertEqual(info["claimed_relationship"], "supports")
        self.assertEqual(info["claimed_status"], "SUPPORTED", "声称的关系统统留在元数据里")
        self.assertEqual(info["status"], "UNVERIFIED")
        self.assertEqual(eg.relation_for_status(info["status"]), "MENTIONS")

    def test_qualified_keeps_support_but_is_marked_and_discounted(self):
        node = claim_node("c1", refs=["a"], pairs=[
            {"evidence_ref": "a", "verdict": "QUALIFIED", "score": 0.8}])
        info = eg.edge_status(node, "a", evidence("a"))
        self.assertEqual(eg.relation_for_status(info["status"]), "SUPPORTS")
        self.assertAlmostEqual(eg.edge_strength(info), round(0.8 * eg.qualify_factor(), 4))
        saved = os.environ.get("QA_EVIDENCE_GRAPH_QUALIFY_FACTOR")
        try:
            os.environ["QA_EVIDENCE_GRAPH_QUALIFY_FACTOR"] = "1"
            self.assertAlmostEqual(eg.edge_strength(info), 0.8)
        finally:
            if saved is None:
                os.environ.pop("QA_EVIDENCE_GRAPH_QUALIFY_FACTOR", None)
            else:
                os.environ["QA_EVIDENCE_GRAPH_QUALIFY_FACTOR"] = saved

    def test_strength_is_zero_without_a_verification_score(self):
        node = claim_node("c1", refs=["a"], verification=False)
        info = eg.edge_status(node, "a", evidence("a", relation="supports"))
        self.assertEqual(eg.edge_strength(info), 0.0,
                         "检索原始分（可能上千）不许冒充概率")

    def test_dangling_reference_becomes_an_unverified_edge(self):
        node = claim_node("c1", refs=["missing"], pairs=[
            {"evidence_ref": "missing", "verdict": "UNVERIFIED", "score": 0.0,
             "reasons": ["claim_evidence_missing"]}])
        layer = eg.build_layer(graph_of([node], []))
        edge = layer["edges"][0]
        self.assertEqual(edge["graph_relation"], "MENTIONS")
        self.assertTrue(edge["metadata"]["dangling_ref"])
        self.assertEqual(layer["stats"]["nodes"], 2)


class StatusConsistencyTests(unittest.TestCase):
    """图里的关系复算出的状态 == 核验层写的 verification.status（口径不许打架）。"""

    CASES = (
        (["SUPPORTED"], "confirmed"),
        (["SUPPORTED", "REFUTED"], "confirmed"),
        (["REFUTED"], "conflicted"),
        (["QUALIFIED"], "qualified"),
        (["CONTEXT"], "unverified"),
        (["UNVERIFIED"], "unverified"),
        ([], "insufficient_evidence"),
    )

    def test_claim_status_from_edges_matches_the_verifier(self):
        for verdicts, expected in self.CASES:
            pairs = [{"evidence_ref": "e%d" % index, "verdict": verdict, "score": 0.5}
                     for index, verdict in enumerate(verdicts)]
            refs = [pair["evidence_ref"] for pair in pairs]
            node = claim_node("c1", refs=refs, pairs=pairs, status=expected)
            edge_statuses = [eg.edge_status(node, ref, evidence(ref))["status"] for ref in refs]
            self.assertEqual(eg.claim_status_from_edges([{"status": status}
                                                         for status in edge_statuses]), expected)
            self.assertEqual(verifier._claim_status(pairs), expected,
                             "复用 qa_verifier 的口径（两处必须一致）")

    def test_layer_reports_status_consistency(self):
        node = claim_node("c1", refs=["a", "b"], status="conflicted", pairs=[
            {"evidence_ref": "a", "verdict": "REFUTED", "score": 0.3},
            {"evidence_ref": "b", "verdict": "UNVERIFIED", "score": 0.0}])
        layer = eg.build_layer(graph_of([node], [evidence("a"), evidence("b")]))
        self.assertEqual(layer["stats"]["status_consistency"]["checked"], 1)
        self.assertEqual(layer["stats"]["status_consistency"]["mismatches"], 0)

    def test_a_real_mismatch_is_reported_not_hidden(self):
        """核验层说 confirmed，但边的 verdict 全是 REFUTED → 必须报出来（不许悄悄盖过去）。"""
        node = claim_node("c1", refs=["a"], status="confirmed", pairs=[
            {"evidence_ref": "a", "verdict": "REFUTED", "score": 0.5}])
        layer = eg.build_layer(graph_of([node], [evidence("a")]))
        self.assertEqual(layer["stats"]["status_consistency"]["mismatches"], 1)
        self.assertEqual(layer["stats"]["status_consistency"]["details"][0]["claim_id"], "c1")


class EdgeContractTests(unittest.TestCase):
    def _layer(self):
        good = claim_node("c1", refs=["a", "b"], status="confirmed", pairs=[
            {"evidence_ref": "a", "verdict": "SUPPORTED", "score": 0.7},
            {"evidence_ref": "b", "verdict": "REFUTED", "score": 0.2}])
        weak = claim_node("c2", refs=["c"], status="unverified", pairs=[
            {"evidence_ref": "c", "verdict": "CONTEXT", "score": 0.1}])
        return eg.build_layer(graph_of([good, weak], [evidence("a"), evidence("b"),
                                                      evidence("c", relation="context")]))

    def test_every_edge_passes_the_schema_and_the_relation_matrix(self):
        layer = self._layer()
        for edge in layer["edges"]:
            ok, note = validate("evidence_graph_edge", dict(edge))
            self.assertTrue(ok, note)
            self.assertIn(edge["graph_relation"],
                          contracts.EVIDENCE_GRAPH_RELATIONS_BY_KIND[edge["kind"]])
        self.assertEqual(layer["stats"]["valid_edges"]["schema_failures"], 0)
        self.assertEqual(layer["stats"]["valid_edges"]["relation_failures"], 0)

    def test_every_node_passes_the_schema(self):
        layer = self._layer()
        for node in layer["nodes"]:
            ok, note = validate("evidence_graph_node", dict(node))
            self.assertTrue(ok, note)
            self.assertIn(node["node_type"], contracts.EVIDENCE_GRAPH_NODE_TYPES)

    def test_the_whole_layer_passes_the_graph_contract(self):
        layer = self._layer()
        ok, note = validate("evidence_graph", layer)
        self.assertTrue(ok, note)

    def test_source_and_evidence_nodes_are_created(self):
        layer = self._layer()
        types = {node["node_type"] for node in layer["nodes"]}
        self.assertTrue({"claim", "evidence", "source"} <= types)
        sources = [node for node in layer["nodes"] if node["node_type"] == "source"]
        self.assertEqual(len(sources), 3, "三条证据三个来源，各自一个 Source 节点")

    def test_building_twice_is_byte_identical(self):
        import json

        first = json.dumps(self._layer(), sort_keys=True, ensure_ascii=False, default=str)
        second = json.dumps(self._layer(), sort_keys=True, ensure_ascii=False, default=str)
        self.assertEqual(first, second, "同输入必须同输出（裁决可复算的前提）")


class CoverageTests(unittest.TestCase):
    def _layer(self):
        """4 条结论：c1 两条已核验支持（0.7+0.4，饱和到 1）、c2 一条 0.3、c3 只有声称支持、
        c4 只有已核验反驳。手算口径写死在断言里。"""
        c1 = claim_node("c1", refs=["e1", "e2"], status="confirmed", pairs=[
            {"evidence_ref": "e1", "verdict": "SUPPORTED", "score": 0.7},
            {"evidence_ref": "e2", "verdict": "SUPPORTED", "score": 0.4}])
        c2 = claim_node("c2", refs=["e3"], status="confirmed", pairs=[
            {"evidence_ref": "e3", "verdict": "SUPPORTED", "score": 0.3}])
        c3 = claim_node("c3", refs=["e4"], status="qualified", verification=False)
        c4 = claim_node("c4", refs=["e5"], status="conflicted", pairs=[
            {"evidence_ref": "e5", "verdict": "REFUTED", "score": 0.5}])
        items = [evidence("e1"), evidence("e2"), evidence("e3"),
                 evidence("e4", relation="supports"), evidence("e5", relation="contradicts")]
        return eg.build_layer(graph_of([c1, c2, c3, c4], items))

    def test_coverage_math_is_recomputable_by_hand(self):
        coverage = self._layer()["coverage"]
        self.assertEqual(coverage["total_claims"], 4)
        self.assertEqual(coverage["supported_claims"], 2)
        self.assertEqual(coverage["qualified_claims"], 1, "c3 只有声称的支持，不算已核验支持")
        self.assertEqual(coverage["refuted_claims"], 1)
        self.assertEqual(coverage["claims_with_evidence"], 4)
        self.assertEqual(coverage["claims_without_evidence"], 0)
        self.assertAlmostEqual(coverage["claim_coverage"], 0.5)
        # (min(1, 0.7+0.4) + 0.3 + 0 + 0) / 4 = 0.325
        self.assertAlmostEqual(coverage["weighted_claim_coverage"], 0.325)
        self.assertAlmostEqual(coverage["claimed_claim_coverage"], 0.75)
        self.assertAlmostEqual(coverage["evidence_coverage"], 1.0)
        self.assertAlmostEqual(coverage["refuted_claim_rate"], 0.25)
        self.assertEqual(coverage["support_count_histogram"], {"0": 2, "1": 1, "2": 1})
        self.assertEqual(coverage["coverage_buckets"],
                         {"0": 1, "(0,0.5]": 1, "(0.5,1)": 0, "1": 1, "refuted": 1})
        self.assertEqual(coverage["coverage_version"], eg.COVERAGE_VERSION)
        self.assertIn("已核验 SUPPORTS", coverage["coverage_definition"])
        ok, note = validate("claim_coverage", coverage)
        self.assertTrue(ok, note)

    def test_extra_support_edges_saturate_at_one(self):
        node = claim_node("c1", refs=["a"], status="confirmed", pairs=[
            {"evidence_ref": "a", "verdict": "SUPPORTED", "score": 1.0}])
        layer = eg.build_layer(graph_of([node], [evidence("a")]))
        self.assertEqual(layer["coverage"]["weighted_claim_coverage"], 1.0)

    def test_plan_only_claims_never_enter_the_denominator(self):
        node = claim_node("c1", refs=["a"], status="confirmed", pairs=[
            {"evidence_ref": "a", "verdict": "SUPPORTED", "score": 0.9}])
        plan = {"claims": [{"claim_id": "c:sq1", "statement": "需要证实或证伪：A股为什么涨",
                            "role": "answer", "sub_question_id": "sq:1"}],
                "dependencies": []}
        layer = eg.build_layer(graph_of([node], [evidence("a")]), plan=plan)
        plan_rows = [row for row in layer["claims"] if row["plan_only"]]
        self.assertEqual(len(plan_rows), 1)
        self.assertEqual(layer["coverage"]["total_claims"], 1, "计划不是结论，不进分母")
        self.assertEqual(layer["coverage"]["claim_coverage"], 1.0)
        self.assertEqual(layer["stats"]["plan_claims"], 1)

    def test_empty_graph_is_honest(self):
        layer = eg.build_layer({"claims": [], "evidence": [], "edges": [], "conflicts": []})
        self.assertEqual(layer["coverage"]["total_claims"], 0)
        self.assertIsNone(layer["coverage"]["claim_coverage"])
        self.assertEqual(layer["nodes"], [])
        self.assertEqual(layer["contradictions"], [])
        ok, note = validate("evidence_graph", layer)
        self.assertTrue(ok, note)

    def test_garbage_input_does_not_raise(self):
        layer = eg.build_layer({"claims": [None, 3, "x"], "evidence": ["nope"],
                                "edges": None, "conflicts": [None]})
        self.assertEqual(layer["coverage"]["total_claims"], 0)
        self.assertEqual(layer["stats"]["edges"], 0)

    def test_claimed_coverage_is_reported_separately_from_verified(self):
        node = claim_node("c1", refs=["a"], status="supported", verification=False)
        layer = eg.build_layer(graph_of([node], [evidence("a", relation="supports")]))
        coverage = layer["coverage"]
        self.assertEqual(coverage["claim_coverage"], 0.0, "没有核验就不许算主口径")
        self.assertEqual(coverage["claimed_claim_coverage"], 1.0, "对照口径如实反映声称的支持")
        self.assertEqual(layer["stats"]["verification_basis"], {"relationship": 1})


class DependencyEdgeTests(unittest.TestCase):
    def test_plan_dependencies_become_claim_level_depends_edges(self):
        plan = {
            "claims": [
                {"claim_id": "c:sq1", "statement": "需要证实或证伪：第一跳问题", "role": "link",
                 "sub_question_id": "sq:1"},
                {"claim_id": "c:sq2", "statement": "需要证实或证伪：第二跳问题", "role": "link",
                 "sub_question_id": "sq:2"},
            ],
            "dependencies": [{"from": "sq:1", "to": "sq:2", "carries": ["entities", "evidence"],
                              "schema": "qa.evidence_object", "why": "依赖"}],
        }
        layer = eg.build_layer(graph_of([], []), plan=plan)
        depends = [edge for edge in layer["edges"] if edge["graph_relation"] == "DEPENDS"]
        self.assertEqual(len(depends), 1)
        edge = depends[0]
        self.assertEqual(edge["kind"], "claim-claim")
        self.assertEqual(edge["src"], eg.node_id("claim", "c:sq2"), "后继依赖前置")
        self.assertEqual(edge["dst"], eg.node_id("claim", "c:sq1"))
        self.assertEqual(edge["metadata"]["carries"], ["entities", "evidence"])
        ok, note = validate("evidence_graph_edge", dict(edge))
        self.assertTrue(ok, note)
        self.assertEqual(layer["stats"]["depends_edges"], 1)

    def test_no_dependency_no_edge(self):
        plan = {"claims": [{"claim_id": "c:sq1", "statement": "只有一条", "role": "answer",
                            "sub_question_id": "sq:1"}], "dependencies": []}
        layer = eg.build_layer(graph_of([], []), plan=plan)
        self.assertEqual([edge for edge in layer["edges"]
                          if edge["graph_relation"] == "DEPENDS"], [])

    def test_plan_claim_is_matched_to_the_answer_claim_by_similarity(self):
        node = claim_node("l1-c1", text="A股10月9日大涨3.84%，固态电池与储能订单是主因")
        plan = {"claims": [{"claim_id": "c:sq1",
                            "statement": "需要证实或证伪：A股10月9日大涨3.84%，固态电池与储能订单是主因",
                            "role": "answer", "sub_question_id": "sq:1"}],
                "dependencies": []}
        layer = eg.build_layer(graph_of([node], []), plan=plan)
        row = [item for item in layer["claims"] if item["plan_only"]][0]
        self.assertEqual(row["matched_claim_id"], "l1-c1")
        self.assertGreater(row["match_similarity"], 0.3)


class FlagTests(unittest.TestCase):
    def test_switch_defaults_off(self):
        saved = os.environ.pop("QA_EVIDENCE_GRAPH", None)
        try:
            self.assertFalse(eg.evidence_graph_enabled(), "证据图层开关必须默认关")
            os.environ["QA_EVIDENCE_GRAPH"] = "1"
            self.assertTrue(eg.evidence_graph_enabled())
            os.environ["QA_EVIDENCE_GRAPH"] = "0"
            self.assertFalse(eg.evidence_graph_enabled())
        finally:
            os.environ.pop("QA_EVIDENCE_GRAPH", None)
            if saved is not None:
                os.environ["QA_EVIDENCE_GRAPH"] = saved

    def test_thresholds_are_bounded_and_configurable(self):
        base = eg.thresholds()
        for key in ("authority_gap", "quality_ratio", "independence_gap", "strength_ratio",
                    "plan_match_min", "max_edges"):
            self.assertIn(key, base)
        saved = os.environ.get("QA_EVIDENCE_GRAPH_MAX_EDGES")
        try:
            os.environ["QA_EVIDENCE_GRAPH_MAX_EDGES"] = "1"
            self.assertEqual(eg.thresholds()["max_edges"], 1)
            os.environ["QA_EVIDENCE_GRAPH_MAX_EDGES"] = "999999"
            self.assertLessEqual(eg.thresholds()["max_edges"], 20000, "上限必须被夹住")
            os.environ["QA_EVIDENCE_GRAPH_MAX_EDGES"] = "junk"
            self.assertEqual(eg.thresholds()["max_edges"], eg.DEFAULT_THRESHOLDS["max_edges"])
        finally:
            os.environ.pop("QA_EVIDENCE_GRAPH_MAX_EDGES", None)
            if saved is not None:
                os.environ["QA_EVIDENCE_GRAPH_MAX_EDGES"] = saved

    def test_edge_limit_truncates_instead_of_exploding(self):
        claims = [claim_node("c%d" % index, refs=["e%d" % index], status="confirmed", pairs=[
            {"evidence_ref": "e%d" % index, "verdict": "SUPPORTED", "score": 0.5}])
            for index in range(5)]
        items = [evidence("e%d" % index) for index in range(5)]
        saved = os.environ.get("QA_EVIDENCE_GRAPH_MAX_EDGES")
        try:
            os.environ["QA_EVIDENCE_GRAPH_MAX_EDGES"] = "2"
            layer = eg.build_layer(graph_of(claims, items))
            self.assertEqual(len(layer["edges"]), 2)
            self.assertEqual(layer["stats"]["truncated_edges"], 3)
        finally:
            os.environ.pop("QA_EVIDENCE_GRAPH_MAX_EDGES", None)
            if saved is not None:
                os.environ["QA_EVIDENCE_GRAPH_MAX_EDGES"] = saved


if __name__ == "__main__":
    unittest.main()

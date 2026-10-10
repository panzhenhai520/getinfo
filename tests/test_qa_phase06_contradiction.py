#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 06 · P06-04 矛盾检测与规则裁决（单元用例）。

钉住的东西：
  1. **两族检测**：同一结论既有支持又有反驳（§15 的 `E1 SUPPORTS C / E2 REFUTES C`）与
     两条结论互相冲突（复用 `qa_reasoning` 的检出结果）都会生成矛盾节点 + CONTRADICTS 边；
  2. **九个理由码都能复算**：时间/权威/质量/独立性/强度五条裁决规则 + 三种"保留不确定性"
     + 适用范围差异；理由码与 resolution 必须匹配（resolved/unresolved 两个集合是划分）；
  3. **单调性**：接线前 `qa_reasoning._adjudicate` 判 resolved 的冲突，接线后必须仍然 resolved
     （权威阈值沿用同一口径，新规则只能"多解决"，不能把已解决的又变回未解决）；
  4. **零模型调用**：默认裁决器是规则；注入点未注册/抛错/返回非法载荷一律保守回落规则并记账。
"""
import os
import sys
import unittest
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_evidence_graph as eg  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_reasoning  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402
from qa_phase06_fixtures import claim_node, evidence, graph_of  # noqa: E402


def side(*, mass=0.0, verified_mass=0.0, authority=0, independence=0, latest="",
         claim_type="current_fact"):  # noqa: A002
    """一侧的指标（字段与 `_side_metrics` 输出一致；时间一律 ISO 字符串，可 JSON 序列化）。"""
    moment = (datetime.fromisoformat(latest).replace(tzinfo=timezone.utc) if latest else None)
    return {"edge_count": 1 if mass else 0, "mass": mass, "verified_mass": verified_mass,
            "effective_mass": verified_mass if verified_mass > 0 else mass,
            "authority": authority, "independence": independence,
            "latest": moment.isoformat(timespec="seconds") if moment else "",
            "claim_latest": "", "claim_type": claim_type}


def contradiction(left, right, *, conflict_type="real_conflict", kind="claim_conflict",
                  claim_ids=("c1", "c2"), cid="ctr:test"):
    return {"contradiction_id": cid, "kind": kind, "conflict_type": conflict_type,
            "claim_ids": list(claim_ids), "evidence_refs": ["e1", "e2"],
            "left": left, "right": right,
            "inputs": {"thresholds": eg.thresholds(), "conflict_type": conflict_type},
            "rule_version": contracts.CONTRADICTION_RESOLVER_VERSION}


class RuleEngineTests(unittest.TestCase):
    def _decide(self, left, right, **kwargs):
        return eg._finalize_decision(contradiction(left, right, **kwargs))

    def test_scope_difference_resolves_without_a_winner(self):
        decision = self._decide(side(authority=100), side(authority=100),
                                conflict_type="scope_difference")
        self.assertEqual(decision["resolution"], "resolved")
        self.assertEqual(decision["reason_code"], "SCOPE_DIFFERENCE")
        self.assertEqual(decision["winner"], {})

    def test_newer_version_precedes(self):
        decision = self._decide(side(authority=10, latest="2026-01-01"),
                                side(authority=10, latest="2026-09-01"))
        self.assertEqual(decision["resolution"], "resolved")
        self.assertEqual(decision["reason_code"], "NEWER_VERSION_PRECEDES")
        self.assertEqual(decision["winner"]["side"], "right")
        self.assertEqual(decision["winner"]["claim_id"], "c2")
        self.assertEqual(decision["winner"]["effective_at"], "2026-09-01T00:00:00+00:00")
        self.assertEqual(decision["winner"]["superseded_side"], "left")

    def test_authority_advantage_uses_the_legacy_threshold(self):
        decision = self._decide(side(authority=100), side(authority=50))
        self.assertEqual(decision["reason_code"], "AUTHORITY_ADVANTAGE")
        self.assertEqual(decision["winner"]["side"], "left")
        self.assertEqual(decision["winner"]["authority_gap"], 50)
        tiny = self._decide(side(authority=100), side(authority=99))
        self.assertNotEqual(tiny["reason_code"], "AUTHORITY_ADVANTAGE",
                            "差 1 分不该判权威领先（阈值沿用既有 >=2 口径）")

    def test_quality_advantage(self):
        decision = self._decide(side(authority=50, mass=0.8, verified_mass=0.8),
                                side(authority=50, mass=0.2, verified_mass=0.2))
        self.assertEqual(decision["reason_code"], "EVIDENCE_QUALITY_ADVANTAGE")
        self.assertEqual(decision["winner"]["side"], "left")
        self.assertAlmostEqual(decision["winner"]["quality_ratio"], 4.0, places=3)

    def test_independence_advantage(self):
        decision = self._decide(side(authority=50, mass=0.4, verified_mass=0.4, independence=3),
                                side(authority=50, mass=0.4, verified_mass=0.4, independence=1))
        self.assertEqual(decision["reason_code"], "INDEPENDENCE_ADVANTAGE")
        self.assertEqual(decision["winner"]["independence_gap"], 2)

    def test_strength_advantage_is_the_last_resort(self):
        decision = self._decide(side(authority=50, mass=1.0, verified_mass=0.0, independence=1),
                                side(authority=50, mass=0.2, verified_mass=0.0, independence=1))
        self.assertEqual(decision["reason_code"], "RELATION_STRENGTH_ADVANTAGE")
        self.assertAlmostEqual(decision["winner"]["strength_ratio"], 5.0, places=3)

    def test_method_difference_stays_unresolved(self):
        decision = self._decide(side(authority=100, mass=0.4, verified_mass=0.4),
                                side(authority=100, mass=0.4, verified_mass=0.4),
                                conflict_type="method_difference")
        self.assertEqual(decision["resolution"], "unresolved")
        self.assertEqual(decision["reason_code"], "METHOD_DIFFERENCE_UNDECIDED")

    def test_opinion_only_stays_unresolved(self):
        decision = self._decide(side(authority=1, claim_type="interpretation"),
                                side(authority=1, claim_type="interpretation"),
                                conflict_type="opinion_difference")
        self.assertEqual(decision["resolution"], "unresolved")
        self.assertEqual(decision["reason_code"], "OPINION_ONLY_UNDECIDED")

    def test_no_decisive_rule_is_the_default(self):
        decision = self._decide(side(authority=1), side(authority=1))
        self.assertEqual(decision["resolution"], "unresolved")
        self.assertEqual(decision["reason_code"], "NO_DECISIVE_RULE")

    def test_reason_codes_and_resolutions_are_a_partition(self):
        resolved = set(contracts.CONTRADICTION_RESOLVED_CODES)
        unresolved = set(contracts.CONTRADICTION_UNRESOLVED_CODES)
        self.assertEqual(resolved & unresolved, set())
        self.assertEqual(resolved | unresolved, set(contracts.CONTRADICTION_RESOLUTION_CODES))
        for left, right, kwargs, expected in (
                (side(), side(), {"conflict_type": "scope_difference"}, "resolved"),
                (side(latest="2026-01-01"), side(latest="2026-02-01"), {}, "resolved"),
                (side(authority=90), side(authority=10), {}, "resolved"),
                (side(mass=0.9, verified_mass=0.9), side(mass=0.1, verified_mass=0.1), {}, "resolved"),
                (side(mass=0.4, verified_mass=0.4, independence=4),
                 side(mass=0.4, verified_mass=0.4, independence=1), {}, "resolved"),
                (side(mass=1.0, verified_mass=0.0), side(mass=0.1, verified_mass=0.0), {}, "resolved"),
                (side(authority=5), side(authority=5), {"conflict_type": "method_difference"},
                 "unresolved"),
                (side(authority=5, claim_type="interpretation"),
                 side(authority=5, claim_type="interpretation"),
                 {"conflict_type": "opinion_difference"}, "unresolved"),
                (side(authority=5), side(authority=5), {}, "unresolved")):
            decision = self._decide(left, right, **kwargs)
            self.assertEqual(decision["resolution"], expected, decision["reason_code"])
            self.assertIn(decision["reason_code"], contracts.CONTRADICTION_RESOLUTION_CODES)

    def test_thresholds_are_configurable_and_change_the_decision(self):
        saved = os.environ.get("QA_EVIDENCE_GRAPH_INDEPENDENCE_GAP")
        try:
            os.environ["QA_EVIDENCE_GRAPH_INDEPENDENCE_GAP"] = "1"
            decision = self._decide(side(authority=5, mass=0.4, verified_mass=0.4, independence=2),
                                    side(authority=5, mass=0.4, verified_mass=0.4, independence=1))
            self.assertEqual(decision["reason_code"], "INDEPENDENCE_ADVANTAGE")
            os.environ["QA_EVIDENCE_GRAPH_INDEPENDENCE_GAP"] = "5"
            decision = self._decide(side(authority=5, mass=0.4, verified_mass=0.4, independence=2),
                                    side(authority=5, mass=0.4, verified_mass=0.4, independence=1))
            self.assertEqual(decision["reason_code"], "NO_DECISIVE_RULE")
        finally:
            if saved is None:
                os.environ.pop("QA_EVIDENCE_GRAPH_INDEPENDENCE_GAP", None)
            else:
                os.environ["QA_EVIDENCE_GRAPH_INDEPENDENCE_GAP"] = saved


class DetectorTests(unittest.TestCase):
    def test_evidence_conflict_is_detected(self):
        node = claim_node("c1", refs=["e1", "e2"], status="conflicted", pairs=[
            {"evidence_ref": "e1", "verdict": "SUPPORTED", "score": 0.7},
            {"evidence_ref": "e2", "verdict": "REFUTED", "score": 0.3}])
        layer = eg.build_layer(graph_of([node], [evidence("e1"), evidence("e2")]))
        self.assertEqual(len(layer["contradictions"]), 1)
        decision = layer["contradictions"][0]
        self.assertEqual(decision["kind"], "evidence_conflict")
        self.assertEqual(decision["claim_ids"], ["c1"])
        self.assertEqual(sorted(decision["evidence_refs"]), ["e1", "e2"])
        self.assertEqual(decision["resolution"], "resolved")
        self.assertEqual(decision["reason_code"], "EVIDENCE_QUALITY_ADVANTAGE")
        self.assertEqual(layer["stats"]["contradiction_kinds"], {"evidence_conflict": 1})
        node_types = {item["node_type"] for item in layer["nodes"]}
        self.assertIn("contradiction", node_types)

    def test_no_refutation_no_contradiction(self):
        node = claim_node("c1", refs=["e1"], status="confirmed", pairs=[
            {"evidence_ref": "e1", "verdict": "SUPPORTED", "score": 0.7}])
        layer = eg.build_layer(graph_of([node], [evidence("e1")]))
        self.assertEqual(layer["contradictions"], [])

    def test_claim_conflict_comes_from_the_reasoning_graph(self):
        left = claim_node("c1", text="补贴政策2026年仍然有效，最高补贴2万元",
                          refs=["e1"], authority=100, pairs=[
                              {"evidence_ref": "e1", "verdict": "SUPPORTED", "score": 0.8}])
        right = claim_node("c2", text="补贴政策2026年不再有效，最高补贴2万元",
                           refs=["e2"], authority=20, pairs=[
                               {"evidence_ref": "e2", "verdict": "SUPPORTED", "score": 0.3}])
        conflict = {"conflict_id": "conflict:abc", "subject": "补贴", "conflict_type": "real_conflict",
                    "claim_ids": ["c1", "c2"], "evidence_refs": ["e1", "e2"],
                    "resolution": "unresolved", "rationale": "旧口径", "rule_version": "qa-adjudication-v1"}
        graph = graph_of([left, right], [evidence("e1", authority=100), evidence("e2", authority=20)],
                          [conflict])
        layer = eg.build_layer(graph)
        self.assertEqual(len(layer["contradictions"]), 1)
        decision = layer["contradictions"][0]
        self.assertEqual(decision["kind"], "claim_conflict")
        self.assertEqual(decision["reason_code"], "AUTHORITY_ADVANTAGE")
        self.assertEqual(decision["winner"]["claim_id"], "c1")
        self.assertEqual(decision["inputs"]["legacy_resolution"], "unresolved")

    def test_claim_conflict_also_links_the_two_claims_directly(self):
        """结论级冲突除了经矛盾节点，还要有一条 claim↔claim 的 CONTRADICTS 边。"""
        left = claim_node("c1", refs=["e1"], authority=100)
        right = claim_node("c2", refs=["e2"], authority=20)
        conflict = {"conflict_id": "conflict:abc", "subject": "补贴", "conflict_type": "real_conflict",
                    "claim_ids": ["c1", "c2"], "evidence_refs": ["e1", "e2"],
                    "resolution": "unresolved", "rationale": "旧口径", "rule_version": "qa-adjudication-v1"}
        layer = eg.build_layer(graph_of([left, right],
                                       [evidence("e1"), evidence("e2")], [conflict]))
        direct = [edge for edge in layer["edges"]
                  if edge["kind"] == "claim-claim"
                  and edge["graph_relation"] == "CONTRADICTS"]
        self.assertEqual(len(direct), 1)
        self.assertEqual(direct[0]["src"], eg.node_id("claim", "c1"))
        self.assertEqual(direct[0]["dst"], eg.node_id("claim", "c2"))
        self.assertEqual(direct[0]["metadata"]["counterpart_claim_id"], "c2")
        ok, note = validate("evidence_graph_edge", dict(direct[0]))
        self.assertTrue(ok, note)
        # 证据级冲突不产生 claim-claim 边（同一条结论没有两条结论可比）
        evidence_level = [edge for edge in layer["edges"]
                          if edge["kind"] == "claim-claim"
                          and edge["metadata"].get("conflict_type") == "evidence_conflict"]
        self.assertEqual(evidence_level, [])

    def test_decisions_are_written_back_inside_the_frozen_conflict_schema(self):
        left = claim_node("c1", refs=["e1"], authority=100)
        right = claim_node("c2", refs=["e2"], authority=20)
        conflict = {"conflict_id": "conflict:abc", "subject": "补贴", "conflict_type": "real_conflict",
                    "claim_ids": ["c1", "c2"], "evidence_refs": ["e1", "e2"],
                    "resolution": "unresolved", "rationale": "旧口径", "rule_version": "qa-adjudication-v1"}
        graph = graph_of([left, right], [evidence("e1", authority=100), evidence("e2", authority=20)],
                         [conflict])
        layer = eg.layer_from_graph(graph)
        self.assertEqual(layer["applied_conflicts"], 1)
        self.assertEqual(graph["conflicts"][0]["resolution"], "resolved")
        self.assertEqual(graph["conflicts"][0]["rule_version"],
                         contracts.CONTRADICTION_RESOLVER_VERSION)
        self.assertTrue(graph["conflicts"][0]["rationale"])
        allowed = set(qa_contracts_conflict_keys())
        self.assertEqual(set(graph["conflicts"][0].keys()), allowed,
                         "回写不许新增键（冻结 CONFLICT_SCHEMA 的 additionalProperties=false）")

    def test_monotonicity_against_the_legacy_adjudicator(self):
        """接线前 resolved 的冲突，接线后必须仍然 resolved（裁决只能更细，不能更模糊）。"""
        for left_authority, right_authority, conflict_type, left_date, right_date in (
                (100, 20, "real_conflict", None, None),
                (50, 20, "real_conflict", None, None),
                (100, 100, "scope_difference", None, None),
                (100, 100, "time_change", "2026-01-01", "2026-06-01"),
                (20, 20, "real_conflict", None, None)):
            left = {"text": "补贴政策2026年有效，最高2万元", "claim_id": "c1",
                    "scope": ["内地"], "valid_from": left_date}
            right = {"text": "补贴政策2026年不再有效，最高2万元", "claim_id": "c2",
                     "scope": ["香港"], "valid_from": right_date}
            evidence_by_ref = {
                "e1": {"authority_level": left_authority}, "e2": {"authority_level": right_authority}}
            legacy_resolution, _legacy_rationale = qa_reasoning._adjudicate(
                conflict_type, left, right, evidence_by_ref)
            decision = self._decide_via_layer(left_authority, right_authority, conflict_type,
                                              left_date, right_date)
            if legacy_resolution == "resolved":
                self.assertEqual(decision["resolution"], "resolved",
                                 "旧口径 resolved 的冲突被判回 unresolved：%s / %s"
                                 % (conflict_type, decision["reason_code"]))

    def _decide_via_layer(self, left_authority, right_authority, conflict_type,
                          left_date, right_date):
        left = claim_node("c1", text="补贴政策2026年有效，最高2万元", refs=["e1"],
                          authority=left_authority, valid_from=left_date,
                          pairs=[{"evidence_ref": "e1", "verdict": "SUPPORTED", "score": 0.6}])
        right = claim_node("c2", text="补贴政策2026年不再有效，最高2万元", refs=["e2"],
                           authority=right_authority, valid_from=right_date,
                           pairs=[{"evidence_ref": "e2", "verdict": "SUPPORTED", "score": 0.6}])
        conflict = {"conflict_id": "conflict:mono", "subject": "补贴",
                    "conflict_type": conflict_type, "claim_ids": ["c1", "c2"],
                    "evidence_refs": ["e1", "e2"], "resolution": "unresolved",
                    "rationale": "旧口径", "rule_version": "qa-adjudication-v1"}
        graph = graph_of([left, right], [evidence("e1", authority=left_authority),
                                         evidence("e2", authority=right_authority)], [conflict])
        layer = eg.build_layer(graph)
        return [item for item in layer["contradictions"]
                if item["kind"] == "claim_conflict"][0]


def qa_contracts_conflict_keys():
    import qa_contracts

    return list(qa_contracts.CONFLICT_SCHEMA["properties"].keys())


class ResolverInjectionTests(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.pop("QA_CONTRADICTION_RESOLVER", None)
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.pop("QA_CONTRADICTION_RESOLVER", None)
        if self.saved is not None:
            os.environ["QA_CONTRADICTION_RESOLVER"] = self.saved

    def test_default_resolver_is_the_rule_engine(self):
        self.assertEqual(eg.resolver_name(), "rule")
        decision = eg._finalize_decision(contradiction(side(authority=90), side(authority=10)))
        self.assertEqual(decision["decider"], "rule:%s" % contracts.CONTRADICTION_RESOLVER_VERSION)
        self.assertEqual(eg.resolver_report()["registered"], [])

    def test_unregistered_resolver_falls_back_and_records_it(self):
        os.environ["QA_CONTRADICTION_RESOLVER"] = "llm-adjudicator"
        decision = eg._finalize_decision(contradiction(side(authority=90), side(authority=10)))
        self.assertEqual(decision["resolution"], "resolved")
        self.assertEqual(decision["reason_code"], "AUTHORITY_ADVANTAGE")
        self.assertEqual(decision["resolver_fallback"], "resolver_not_registered:llm-adjudicator")

    def test_registered_resolver_can_decide(self):
        eg.register_contradiction_resolver("stub", lambda item: {
            "resolution": "unresolved", "reason_code": "OPINION_ONLY_UNDECIDED",
            "rationale": "stub 不裁"})
        try:
            os.environ["QA_CONTRADICTION_RESOLVER"] = "stub"
            decision = eg._finalize_decision(contradiction(side(authority=90), side(authority=10)))
            self.assertEqual(decision["resolution"], "unresolved")
            self.assertEqual(decision["reason_code"], "OPINION_ONLY_UNDECIDED")
            self.assertEqual(decision["decider"], "injected:stub")
        finally:
            eg._RESOLVERS.pop("stub", None)

    def test_raising_resolver_falls_back(self):
        eg.register_contradiction_resolver("boom", lambda item: (_ for _ in ()).throw(
            RuntimeError("down")))
        try:
            os.environ["QA_CONTRADICTION_RESOLVER"] = "boom"
            decision = eg._finalize_decision(contradiction(side(authority=90), side(authority=10)))
            self.assertEqual(decision["reason_code"], "AUTHORITY_ADVANTAGE")
            self.assertEqual(decision["resolver_fallback"], "resolver_error:RuntimeError")
        finally:
            eg._RESOLVERS.pop("boom", None)

    def test_invalid_payload_falls_back(self):
        eg.register_contradiction_resolver("junk", lambda item: {"resolution": "maybe"})
        try:
            os.environ["QA_CONTRADICTION_RESOLVER"] = "junk"
            decision = eg._finalize_decision(contradiction(side(authority=90), side(authority=10)))
            self.assertEqual(decision["reason_code"], "AUTHORITY_ADVANTAGE")
            self.assertEqual(decision["resolver_fallback"], "resolver_returned_invalid_payload")
        finally:
            eg._RESOLVERS.pop("junk", None)

    def test_inline_resolver_is_used_for_the_returned_decision(self):
        contradiction_item = contradiction(side(authority=90), side(authority=10))
        decided = eg.resolve(contradiction_item,
                             resolver=lambda item: {"resolution": "unresolved",
                                                    "reason_code": "METHOD_DIFFERENCE_UNDECIDED"})
        self.assertEqual(decided["reason_code"], "METHOD_DIFFERENCE_UNDECIDED")
        self.assertEqual(decided["resolution"], "unresolved")

    def test_register_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            eg.register_contradiction_resolver("", lambda item: {})
        with self.assertRaises(ValueError):
            eg.register_contradiction_resolver("name", "not-callable")

    def test_decision_contract_is_valid(self):
        decision = eg._finalize_decision(contradiction(side(authority=90), side(authority=10)))
        ok, note = validate("contradiction_decision", decision)
        self.assertTrue(ok, note)
        broken = dict(decision, reason_code="NOPE")
        ok, _note = validate("contradiction_decision", broken)
        self.assertFalse(ok, "非法理由码必须被契约拦住")

    def test_illegal_reason_code_from_an_injected_resolver_is_clamped(self):
        eg.register_contradiction_resolver("liar", lambda item: {
            "resolution": "resolved", "reason_code": "MADE_UP"})
        try:
            os.environ["QA_CONTRADICTION_RESOLVER"] = "liar"
            decision = eg._finalize_decision(contradiction(side(authority=90), side(authority=10)))
            self.assertEqual(decision["reason_code"], "NO_DECISIVE_RULE")
            self.assertEqual(decision["resolution"], "unresolved")
            self.assertIn("unknown_reason_code", decision["resolver_fallback"])
        finally:
            eg._RESOLVERS.pop("liar", None)

    def test_resolution_and_code_must_agree(self):
        eg.register_contradiction_resolver("confused", lambda item: {
            "resolution": "resolved", "reason_code": "NO_DECISIVE_RULE"})
        try:
            os.environ["QA_CONTRADICTION_RESOLVER"] = "confused"
            decision = eg._finalize_decision(contradiction(side(authority=90), side(authority=10)))
            self.assertEqual(decision["resolution"], "unresolved",
                             "理由码说这条冲突没法裁，resolution 不许写 resolved")
        finally:
            eg._RESOLVERS.pop("confused", None)


if __name__ == "__main__":
    unittest.main()

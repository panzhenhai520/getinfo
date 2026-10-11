#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 13 · **P13-03 counter-evidence / unresolved gaps**。

钉住：
  1. 反证条目**只来自 Phase 06 的 REFUTES 边或 Phase 03 的 REFUTED 结论**（不新造反证），
     并且带"答案有没有引用它"的可复算标记；
  2. 未解决缺口**只读 Phase 07 的复核产物**（`evidence_graph.gap_review.gaps`，
     退化取 `gap_loop.open_gaps`），已 `resolved` 的不进清单；建议通道落在冻结的 7 个通道值内；
  3. 未解决矛盾只取 `resolution="unresolved"` 的（Phase 06 的裁决是唯一真源）；
  4. 陌生取值（闭集合外的 gap_type / band / route）**记空并计数**，不打爆契约、也不放行。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_final_verifier as fv  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_phase08_fixtures as fx08  # noqa: E402
import qa_phase13_fixtures as fx  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402


def _counter_graph():
    """一条结论 + 一条支持证据 + 一条反驳证据（REFUTED verdict 与 REFUTES 边）。"""
    support = fx08.evidence_item("article:1", verification=fx08.verification("SUPPORTED"))
    refute = fx08.evidence_item("article:2", url="https://example.com/2",
                                verification=fx08.verification("REFUTED", score=0.6))
    node = fx.with_verification(
        fx08.graph_claim("c1", text=fx.CLAIM_TEXT, status="conflicted",
                         refs=("article:1", "article:2"), verification_status="conflicted"),
        [fx.pair("article:1"), fx.pair("article:2", "REFUTED", score=0.6)], "conflicted")
    node["edges"] = [fx08.graph_edge("c1", "article:1", status="SUPPORTED"),
                     fx08.graph_edge("c1", "article:2", relation="REFUTES", verified=False,
                                     status="REFUTED")]
    return fx08.graph(claims=[node], evidence=[support, refute],
                      edges=list(node["edges"]))


class CounterEvidenceTests(unittest.TestCase):
    def test_counter_evidence_comes_from_phase06_refutes_edges(self):
        graph = _counter_graph()
        answer = fx08.final_answer(answer="一句话 [1]。",
                                   claims=[fx08.answer_claim("c1", ("article:1",))],
                                   citations=["article:1"], evidence=graph["evidence"],
                                   citation_map={"[1]": "article:1"})
        composition = fv.compose_answer(graph=graph, answer=answer)
        rows = composition["counter_evidence"]
        self.assertEqual([row["evidence_ref"] for row in rows], ["article:2"])
        self.assertEqual(rows[0]["relation"], "REFUTES")
        self.assertEqual(rows[0]["claim_id"], "c1")
        self.assertTrue(rows[0]["quote"], "反证要带上可读的引文（来自最小 span）")
        self.assertFalse(rows[0]["cited_in_answer"], "答案没引这条反证 → 必须显式判 False")

    def test_counter_evidence_cited_in_answer_is_marked(self):
        graph = _counter_graph()
        answer = fx08.final_answer(answer="正面结论 [1]，但有资料指出相反情况 [2]。",
                                   claims=[fx08.answer_claim("c1", ("article:1", "article:2"))],
                                   citations=["article:1", "article:2"], evidence=graph["evidence"],
                                   citation_map={"[1]": "article:1", "[2]": "article:2"})
        rows = fv.compose_answer(graph=graph, answer=answer)["counter_evidence"]
        self.assertTrue(rows[0]["cited_in_answer"])

    def test_supported_evidence_is_not_reported_as_counter(self):
        graph, answer = fx.verified_graph_and_answer()
        self.assertEqual(fv.compose_answer(graph=graph, answer=answer)["counter_evidence"], [])


class UnresolvedGapTests(unittest.TestCase):
    def _graph(self, gaps):
        graph, answer = fx.verified_graph_and_answer()
        graph["evidence_graph"] = {"gap_review": {"stop_reason": "MAX_DEPTH", "gaps": gaps}}
        return graph, answer

    def test_gaps_come_from_phase07_review_and_skip_resolved(self):
        graph, answer = self._graph([
            {"gap_id": "g1", "gap_type": "MISSING_COUNTEREVIDENCE", "claim_id": "c1",
             "priority": 0.91, "band": "critical", "severity": "high",
             "suggested_routes": ["keyword", "graph"], "status": "open",
             "detail": "缺反证"},
            {"gap_id": "g2", "gap_type": "AUTHORITY_GAP", "claim_id": "c1", "priority": 0.4,
             "band": "medium", "suggested_routes": ["policy_exact"], "status": "resolved"},
        ])
        rows = fv.compose_answer(graph=graph, answer=answer)["unresolved_gaps"]
        self.assertEqual([row["gap_id"] for row in rows], ["g1"])
        self.assertEqual(rows[0]["gap_type"], "MISSING_COUNTEREVIDENCE")
        self.assertEqual(rows[0]["band"], "critical")
        self.assertTrue(set(rows[0]["suggested_routes"]) <= set(contracts.QA_RETRIEVAL_ROUTES))
        self.assertFalse(rows[0]["mentioned_in_answer"])

    def test_gaps_fall_back_to_the_gap_loop_receipt(self):
        graph, answer = fx.verified_graph_and_answer()
        graph["gap_loop"] = {"open_gaps": [{"gap_id": "g9", "gap_type": "TEMPORAL_GAP",
                                            "claim_id": "c1", "priority": 0.3, "band": "low",
                                            "suggested_routes": ["semantic"]}]}
        rows = fv.compose_answer(graph=graph, answer=answer)["unresolved_gaps"]
        self.assertEqual([row["gap_id"] for row in rows], ["g9"])

    def test_unknown_enum_values_are_blanked_and_counted(self):
        graph, answer = self._graph([
            {"gap_id": "g1", "gap_type": "NOT_A_REAL_GAP", "band": "ULTRA",
             "suggested_routes": ["telepathy", "keyword"], "status": "open"},
        ])
        composition = fv.compose_answer(graph=graph, answer=answer)
        row = composition["unresolved_gaps"][0]
        self.assertEqual(row["gap_type"], "")
        self.assertEqual(row["band"], "")
        self.assertEqual(row["suggested_routes"], ["keyword"], "只保留冻结枚举内的通道")
        self.assertEqual(composition["inputs"]["unrecognized"],
                         {"gap_type": 1, "band": 1, "route": 1})
        self.assertTrue(validate("answer_composition", composition)[0],
                        "陌生取值不得打爆契约校验")

    def test_no_graph_means_no_invented_gaps(self):
        graph, answer = fx.verified_graph_and_answer()
        composition = fv.compose_answer(graph=graph, answer=answer)
        self.assertEqual(composition["unresolved_gaps"], [])
        self.assertEqual(composition["contradictions"], [])


class ContradictionTests(unittest.TestCase):
    def test_only_unresolved_conflicts_are_listed(self):
        graph, answer = fx.verified_graph_and_answer()
        graph["conflicts"] = [
            fx08.conflict("cf1", ["c1", "c9"], resolution="unresolved",
                          reason_code="NO_DECISIVE_RULE"),
            fx08.conflict("cf2", ["c1", "c8"], resolution="resolved"),
        ]
        rows = fv.compose_answer(graph=graph, answer=answer)["contradictions"]
        self.assertEqual([row["conflict_id"] for row in rows], ["cf1"])
        self.assertEqual(rows[0]["resolution"], "unresolved")
        self.assertEqual(rows[0]["reason_code"], "v1",
                         "裁决理由码取自 Phase 06 的 rule_version（单一真源）")


if __name__ == "__main__":
    unittest.main()

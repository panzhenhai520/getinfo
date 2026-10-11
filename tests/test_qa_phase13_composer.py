#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 13 · **P13-01 claim-aware composer + P13-02 citation binding**。

钉住（全部零模型、零网络、可复算）：
  1. 绑定链**真的是** `claim_id → evidence_id[] → source/chunk/span`：`evidence_id` 等于
     Phase 02 的证据指纹、`span` 能在正文里逐字对上（`content_excerpt[start:end] == quote`）；
  2. 每条 claim 的绑定判定与**理由码**可复算（绑上/绑不上的六种原因）；
  3. 引用编号（P13-02）**有包用包**（Phase 08 的单一真源）、没包现算且与
     `qa_synthesis._citation_map`（生成端编号）**逐字相等**——两处一分叉引用就指错人；
  4. 组装是**纯函数**（不改入参）且同输入同输出；
  5. 通过 `answer_composition` 契约校验。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_context_pack as cp  # noqa: E402
import qa_final_verifier as fv  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_phase08_fixtures as fx08  # noqa: E402
import qa_phase13_fixtures as fx  # noqa: E402
import qa_synthesis  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402


class BindingChainTests(unittest.TestCase):
    def test_binding_carries_claim_id_evidence_id_source_chunk_span(self):
        graph, answer = fx.verified_graph_and_answer()
        composition = fv.compose_answer(graph=graph, answer=answer, question=fx.QUESTION)
        self.assertTrue(validate("answer_composition", composition)[0])
        row = composition["bindings"][0]
        self.assertEqual(row["claim_id"], "c1")
        self.assertTrue(row["bound"])
        self.assertEqual(row["binding_reason"], "BOUND_SUPPORTED")
        entry = row["evidence"][0]
        self.assertEqual(entry["evidence_ref"], "article:1")
        # evidence_id 是 Phase 02 的真实指纹（不是随手编的 id）
        layer = cp.normalize_evidence_layer(graph["evidence"][0])
        self.assertEqual(entry["evidence_id"], layer["evidence_id"])
        self.assertTrue(entry["evidence_id"])
        self.assertTrue(entry["source_url"])
        self.assertTrue(entry["document_id"] or entry["chunk_id"] or entry["source_url"])
        # span 能在证据正文里逐字对上
        span = entry["span"]
        self.assertTrue(span.get("quote"))
        text = graph["evidence"][0]["content_excerpt"]
        self.assertEqual(text[int(span["start"]):int(span["end"])], span["quote"])

    def test_binding_reason_codes_cover_the_six_values(self):
        refs = ("article:1",)
        graph = fx08.graph(
            claims=[fx13_claim("c1", refs=refs), fx13_claim("c2", refs=()),
                    fx13_claim("c3", refs=("article:404",)),
                    fx13_claim("c4", refs=refs, plan_only=True),
                    fx13_claim("c5", refs=refs, text="一条与正文完全无关的第九条结论")],
            evidence=[fx08.evidence_item("article:1", verification=fx08.verification("QUALIFIED"))],
            edges=[fx08.graph_edge("c1", "article:1", status="QUALIFIED"),
                   fx08.graph_edge("c4", "article:1", status="QUALIFIED"),
                   fx08.graph_edge("c5", "article:1", status="QUALIFIED")])
        answer = fx08.final_answer(
            answer="香港家族办公室税收优惠政策对符合条件的管理人给予利得税宽免 [1]。",
            claims=[fx08.answer_claim("c1", ("article:1",)), fx08.answer_claim("c2", ()),
                    fx08.answer_claim("c3", ("article:404",)), fx08.answer_claim("c4", ("article:1",))],
            citations=["article:1"], evidence=graph["evidence"], citation_map={"[1]": "article:1"})
        composition = fv.compose_answer(graph=graph, answer=answer)
        reasons = {row["claim_id"]: row["binding_reason"] for row in composition["bindings"]}
        self.assertEqual(reasons["c1"], "BOUND_QUALIFIED")
        self.assertEqual(reasons["c2"], "NO_EVIDENCE_REF")
        self.assertEqual(reasons["c3"], "NO_TRACEABLE_SPAN")
        self.assertEqual(reasons["c4"], "PLAN_ONLY")
        self.assertEqual(reasons["c5"], "NOT_IN_ANSWER")
        self.assertEqual(set(reasons.values()) - set(contracts.FINAL_BINDING_REASONS), set(),
                         "理由码必须在闭集合内")
        self.assertEqual(composition["unbound_claims"],
                         [row["claim_id"] for row in composition["bindings"]
                          if not row["bound"] and row["in_answer"]])

    def test_unbound_claim_is_not_reported_as_bound(self):
        graph = fx08.graph(claims=[fx13_claim("c1", refs=())],
                           evidence=[fx08.evidence_item("article:1")])
        answer = fx08.final_answer(answer="一句话 [1]。", claims=[fx08.answer_claim("c1", ())],
                                   citations=[], evidence=graph["evidence"], citation_map={})
        row = fv.compose_answer(graph=graph, answer=answer)["bindings"][0]
        self.assertFalse(row["bound"])
        self.assertEqual(row["evidence"], [])

    def test_composition_is_pure_and_deterministic(self):
        graph, answer = fx.verified_graph_and_answer(extra_claim=True)
        before_graph = repr(graph)
        before_answer = repr(answer)
        first = fv.compose_answer(graph=graph, answer=answer, question=fx.QUESTION)
        second = fv.compose_answer(graph=graph, answer=answer, question=fx.QUESTION)
        self.assertEqual(first, second, "同输入必须同输出")
        self.assertEqual(repr(graph), before_graph, "组装不许改图")
        self.assertEqual(repr(answer), before_answer, "组装不许改答案")

    def test_inputs_counts_match_what_was_read(self):
        graph, answer = fx.verified_graph_and_answer(extra_claim=True)
        composition = fv.compose_answer(graph=graph, answer=answer)
        inputs = composition["inputs"]
        self.assertEqual(inputs["claims"], len(composition["bindings"]))
        self.assertEqual(inputs["evidence"], len(graph["evidence"]))
        self.assertEqual(inputs["citation_labels"], len(composition["citation_index"]))
        self.assertEqual(inputs["claims_in_answer"],
                         len([row for row in composition["bindings"] if row["in_answer"]]))


class CitationBindingTests(unittest.TestCase):
    def test_pack_labels_win(self):
        graph, answer = fx.verified_graph_and_answer()
        pack = cp.build_context_pack(graph=graph, run_id="p13")
        composition = fv.compose_answer(graph=graph, answer=answer, pack=pack)
        self.assertEqual(composition["citation_map"], dict(pack["citation_map"]))
        self.assertEqual(sorted(composition["citation_index"]), sorted(pack["citation_index"]))
        self.assertEqual(answer["citation_map"], {"[1]": "article:1"})

    def test_without_a_pack_labels_match_the_generator(self):
        """没有包时现算的编号必须与**生成端**（`qa_synthesis._citation_map`）逐字相等。"""
        graph, answer = fx.verified_graph_and_answer(extra_claim=True)
        composition = fv.compose_answer(graph=graph, answer=answer)
        expected = qa_synthesis._citation_map(list(graph["evidence"]))
        self.assertEqual(composition["citation_map"], expected,
                         "包外编号与生成端编号一分叉，引用就会指错人")
        self.assertEqual(cp.citation_labels(list(graph["evidence"])), expected)

    def test_every_index_entry_is_traceable(self):
        graph, answer = fx.verified_graph_and_answer()
        composition = fv.compose_answer(graph=graph, answer=answer)
        for label, entry in composition["citation_index"].items():
            self.assertTrue(label.startswith("["))
            self.assertTrue(entry["evidence_ref"])
            self.assertTrue(entry["grounded"])
            self.assertTrue(entry["quote"])


def fx13_claim(cid, *, refs, plan_only=False, text=None):
    return fx08.graph_claim(cid, text=text or fx.CLAIM_TEXT, status="unverified", refs=refs,
                            verification_status="unverified", plan_only=plan_only)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 08 · 生成端 grounding 约束（§16/§17 + MASTER_RULES 第 11 条）用例。

钉住：
  1. **七条违规码都能被真实触发**（不是写在那里好看的）；
  2. 阻断级（CLAIM_WITHOUT_EVIDENCE / CITATION_NOT_IN_MAP / CITATION_WITHOUT_SPAN）
     与告警级（未验证支持 / 数字无据 / 漏反证 / 未标注）分得清；
  3. 校验**只读**：`FINAL_ANSWER_SCHEMA` 冻结不变；拦截只动既有可选字段
     （`status` / `degraded` / `degradation_reasons` / `answer`），改完仍能过 `validate_final_answer`；
  4. **真拦截**：第一次不过 → 抛错触发修复重试（模型被回炉）；第二次仍不过 → 显式标注
     （status 降 partial + 正文【无证据】+ degradation_reasons），绝不静默放行；
  5. 包内编号与生成端编号**同一口径**（`citation_labels` == `qa_synthesis._citation_map`），
     否则引用会指错人；
  6. 开关关掉时 `_messages` 逐字回到接线前（回滚口径）。
"""
import json
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_context_pack as cp  # noqa: E402
import qa_contracts  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_synthesis  # noqa: E402
from qa_phase08_fixtures import (  # noqa: E402
    QUESTION, answer_claim, evidence_item, final_answer, graph, graph_claim, graph_edge, plan,
    verification,
)

SUPPORTED_TEXT = "香港家族办公室税收优惠政策对符合条件的管理人给予利得税宽免，门槛为 200 万港元。"
COUNTER_TEXT = "相反观点认为宽免门槛过高、覆盖面有限。" * 4


def _pack():
    evidence = [
        evidence_item("article:1", text=SUPPORTED_TEXT, doc_type="official_policy",
                      authority=100, verification=verification("SUPPORTED", score=0.95)),
        evidence_item("article:2", text=COUNTER_TEXT, authority=20,
                      verification=verification("REFUTED", score=0.5)),
    ]
    claims = [graph_claim("c1", text="香港家族办公室税收优惠政策对内地高净值客户有影响",
                          refs=["article:1"])]
    edges = [graph_edge("c1", "article:1"),
             graph_edge("c1", "article:2", relation="REFUTES", status="REFUTED")]
    return cp.build_context_pack(graph=graph(claims=claims, evidence=evidence, edges=edges),
                                 plan=plan(),
                                 request={"question": QUESTION, "mode": "standard"}, run_id="r1")


def _answer(**kwargs):
    pack = kwargs.pop("pack", None) or _pack()
    defaults = dict(
        answer="香港家族办公室对符合条件的管理人给予利得税宽免 [1]。",
        claims=[answer_claim("c1", ["article:1"])],
        citations=["article:1", "article:2"], citation_map=pack["citation_map"],
        evidence=[evidence_item("article:1", text=SUPPORTED_TEXT, doc_type="official_policy",
                                authority=100, verification=verification("SUPPORTED", score=0.95))])
    defaults.update(kwargs)
    return final_answer(**defaults), pack


class ViolationCodeTests(unittest.TestCase):
    def test_claim_without_evidence_blocks(self):
        answer, pack = _answer(answer="离岸架构可以完全规避申报义务。",
                               claims=[{"claim_id": "c9", "text": "可以规避",
                                        "evidence_refs": []}], citations=[])
        report = cp.check_grounding(answer, pack=pack)
        self.assertTrue(report["blocking"])
        self.assertIn("CLAIM_WITHOUT_EVIDENCE", report["violation_counts"])
        self.assertIn("CLAIM_WITHOUT_EVIDENCE", report["blocking_codes"])

    def test_citation_not_in_map_blocks(self):
        answer, pack = _answer(answer="结论 [7]。")
        report = cp.check_grounding(answer, pack=pack)
        self.assertIn("CITATION_NOT_IN_MAP", report["violation_counts"])
        self.assertTrue(report["blocking"])

    def test_out_of_pack_evidence_ref_blocks(self):
        answer, pack = _answer(citations=["article:1", "article:404"],
                               claims=[answer_claim("c1", ["article:404"])])
        report = cp.check_grounding(answer, pack=pack)
        self.assertIn("CITATION_NOT_IN_MAP", report["violation_counts"])

    def test_citation_without_span_blocks(self):
        evidence = [evidence_item("article:1", text="", verification=verification("SUPPORTED"))]
        claims = [graph_claim("c1", refs=["article:1"])]
        empty_pack = cp.build_context_pack(
            graph=graph(claims=claims, evidence=evidence, edges=[graph_edge("c1", "article:1")]),
            plan=plan(), request={"question": QUESTION}, run_id="r1")
        answer = final_answer(
            answer="有据可查 [1]。",
            claims=[answer_claim("c1", ["article:1"])],
            citations=["article:1"], citation_map={"[1]": "article:1"})
        report = cp.check_grounding(answer, pack=empty_pack)
        self.assertIn("CITATION_WITHOUT_SPAN", report["violation_counts"])
        self.assertTrue(report["blocking"])

    def test_unverified_support_is_a_warning(self):
        pack = _pack()
        weak = answer_claim("c1", ["article:2"], status="qualified")
        answer = final_answer(answer="有说法认为门槛过高 [2]。", claims=[weak],
                              citations=["article:2"], citation_map=pack["citation_map"])
        report = cp.check_grounding(answer, pack=pack)
        self.assertIn("CLAIM_WITH_UNVERIFIED_EVIDENCE", report["violation_counts"])
        self.assertIn("CLAIM_WITH_UNVERIFIED_EVIDENCE", report["warning_codes"])

    def test_number_without_span_is_flagged(self):
        answer, pack = _answer(answer="门槛为 9999 万港元 [1]。")
        report = cp.check_grounding(answer, pack=pack)
        self.assertIn("NUMBER_WITHOUT_SPAN", report["violation_counts"])
        numbers = [item["number"] for item in report["violations"]
                   if item["code"] == "NUMBER_WITHOUT_SPAN"]
        self.assertIn("9999", numbers)

    def test_number_inside_a_cited_span_is_not_flagged(self):
        answer, pack = _answer(answer="门槛为 200 万港元 [1]。")
        report = cp.check_grounding(answer, pack=pack)
        self.assertNotIn("NUMBER_WITHOUT_SPAN", report["violation_counts"])

    def test_counter_evidence_omitted_is_flagged(self):
        answer, pack = _answer()
        report = cp.check_grounding(answer, pack=pack)
        self.assertIn("COUNTER_EVIDENCE_OMITTED", report["violation_counts"])

    def test_unmarked_ungrounded_text_is_flagged(self):
        """包内结论没有可回溯证据、正文也没标注【无证据】→ 告警（§17 的"把推测写成事实"）。"""
        pack = _pack()
        claim = [item for item in pack["items"] if item["kind"] == "claim"][0]
        data = dict(pack)
        data["items"] = [dict(item) for item in pack["items"]]
        data["items"][data["items"].index(claim)] = {**claim,
                                                     "grounding": {"grounded": False,
                                                                   "reason": "构造：无 span"}}
        answer = final_answer(
            answer="%s 这一点没有任何证据支持。" % claim["text"][:24],
            claims=[answer_claim("c1", ["article:1"])],
            citations=["article:1"], citation_map=pack["citation_map"], status="ready")
        report = cp.check_grounding(answer, pack=data)
        self.assertIn("UNMARKED_UNGROUNDED_TEXT", report["violation_counts"])

    def test_all_seven_codes_are_reachable(self):
        """七条违规码都不是摆设：同一条生成端草稿能把七条全部触发出来。"""
        codes = set(contracts.CONTEXT_GROUNDING_VIOLATIONS)
        pack = _pack()
        claim_item = [item for item in pack["items"] if item["kind"] == "claim"][0]
        answer = final_answer(
            answer=("%s 这一点没有任何证据支持。 [9] 9999 万港元" % claim_item["text"][:24]),
            claims=[answer_claim("c1", ["article:1"]),          # 漏反证（article:2 未引）
                    answer_claim("c2", ["article:2"]),          # 只有未验证支持
                    answer_claim("c3", ["article:404"]),        # 引用不在索引里 → 无 span
                    answer_claim("c4", [], status="insufficient_evidence")],  # 完全无据
            citations=[], citation_map=pack["citation_map"], status="ready")
        report = cp.check_grounding(answer, pack=pack)
        reached = set(report["violation_counts"])
        self.assertEqual(codes - reached, set(),
                         "七条违规码必须都能被真实触发，缺：%s" % (codes - reached))

    def test_report_is_contract_shaped(self):
        answer, pack = _answer()
        report = cp.check_grounding(answer, pack=pack)
        ok, note = cp.validate_contract("grounding_report", report)
        self.assertTrue(ok, note)
        self.assertTrue(report["schema_ok"])
        self.assertEqual(report["grounding_version"], contracts.GROUNDING_VERSION)
        self.assertEqual(report["checked_claims"], 1)

    def test_clean_answer_has_no_violation(self):
        answer, pack = _answer(
            answer="香港家族办公室对符合条件的管理人给予利得税宽免 [1]，"
                   "不过也有反证指出门槛偏高 [2]。",
            citations=["article:1", "article:2"], claims=[
                {"claim_id": "c1", "text": "x", "evidence_refs": ["article:1", "article:2"]}])
        report = cp.check_grounding(answer, pack=pack)
        self.assertEqual(report["violations"], [])
        self.assertFalse(report["blocking"])


class GateActionTests(unittest.TestCase):
    def test_marking_downgrades_status_and_marks_the_text(self):
        answer, pack = _answer(answer="离岸架构可以完全规避申报义务。",
                               claims=[answer_claim("c9", [], status="insufficient_evidence")],
                               citations=[])
        report = cp.check_grounding(answer, pack=pack)
        marked = cp.mark_ungrounded_answer(answer, report, pack=pack)
        self.assertEqual(marked["status"], "partial")
        self.assertTrue(marked["degraded"])
        self.assertIn(cp.UNGROUNDED_MARK, marked["answer"])
        self.assertIn("c9", marked["answer"])
        self.assertTrue(marked["degradation_reasons"])
        self.assertTrue(all(len(item) <= 500 for item in marked["degradation_reasons"]))
        # 只动既有可选字段 —— 改完仍然必须过冻结契约
        qa_contracts.validate_final_answer(marked)
        self.assertIs(qa_contracts.FINAL_ANSWER_SCHEMA["additionalProperties"], False)

    def test_marking_is_idempotent(self):
        answer, pack = _answer(answer="无据断言。",
                               claims=[answer_claim("c9", [], status="insufficient_evidence")],
                               citations=[])
        report = cp.check_grounding(answer, pack=pack)
        once = cp.mark_ungrounded_answer(answer, report, pack=pack)
        twice = cp.mark_ungrounded_answer(once, report, pack=pack)
        self.assertEqual(once["answer"], twice["answer"])
        self.assertEqual(once["degradation_reasons"], twice["degradation_reasons"])

    def test_clean_report_leaves_the_answer_untouched(self):
        answer, pack = _answer(
            answer="宽免适用于符合条件的管理人 [1]，也有反证指出门槛偏高 [2]。",
            citations=["article:1", "article:2"], claims=[
                {"claim_id": "c1", "text": "x", "evidence_refs": ["article:1", "article:2"]}])
        report = cp.check_grounding(answer, pack=pack)
        self.assertEqual(report["violations"], [])
        self.assertEqual(cp.mark_ungrounded_answer(answer, report, pack=pack), answer)

    def test_extra_evidence_is_accepted_as_legitimate(self):
        """包外但本轮合法可见的证据（官方原文优先路径）不该被误判成引用错误。"""
        answer, pack = _answer(citations=["article:9"],
                               claims=[{"claim_id": "c1", "text": "x",
                                        "evidence_refs": ["article:9"]}])
        extra = [evidence_item("article:9", text=SUPPORTED_TEXT,
                               verification=verification("SUPPORTED"))]
        without = cp.check_grounding(answer, pack=pack)
        with_extra = cp.check_grounding(answer, pack=pack, extra_evidence=extra)
        self.assertIn("CITATION_NOT_IN_MAP", without["violation_counts"])
        self.assertNotIn("CITATION_NOT_IN_MAP", with_extra["violation_counts"])


class PromptBlockTests(unittest.TestCase):
    def test_prompt_blocks_carry_index_gaps_and_constraints(self):
        blocks = cp.build_prompt_blocks(_pack())
        self.assertIn("引用索引", blocks)
        self.assertIn("无证据内容", blocks)
        self.assertIn("上下文缺口", blocks)
        self.assertIn("约束", blocks)
        self.assertTrue(any(cp.UNGROUNDED_MARK in rule for rule in blocks["约束"]))
        first = blocks["引用索引"][sorted(blocks["引用索引"])[0]]
        self.assertTrue(first["evidence_ref"])
        self.assertTrue(first["可回溯"])
        self.assertTrue(first["片段"])

    def test_inline_labels_match_the_synthesizer_numbering(self):
        """包内编号必须与生成端编号同一口径，否则引用会指错人。"""
        evidence = [item for item in _pack()["items"]
                    if item["kind"] in ("evidence", "counter_evidence")]
        pack_evidence = [{"evidence_ref": item["evidence_ref"],
                          "title": item["metadata"].get("title") or "",
                          "source_url": item["grounding"].get("source_url") or "",
                          "published_at": item["metadata"].get("published_at") or "",
                          "authority_level": item["metadata"].get("authority_level"),
                          "content_excerpt": item["text"]} for item in evidence]
        self.assertEqual(cp.citation_labels(pack_evidence),
                         qa_synthesis._citation_map(pack_evidence))


class SynthesizerGateTests(unittest.TestCase):
    """生成端闸门的**端到端拦截**：第一次不过 → 回炉修复；第二次不过 → 显式标注。"""

    def _graph(self, *, with_ungrounded_claim=False):
        evidence = [evidence_item("article:1", text=SUPPORTED_TEXT,
                                  doc_type="official_policy", authority=100,
                                  verification=verification("SUPPORTED", score=0.95))]
        claims = [graph_claim("c1", text="香港家族办公室税收优惠政策对内地高净值客户有影响",
                              refs=["article:1"])]
        if with_ungrounded_claim:
            # 没有任何 evidence_ref 的结论（Phase 06 会以 insufficient_evidence 身份入图，
            # 生成端必须能看见它、并且**不许**把它写成已确证事实）→ grounding 的阻断来源
            claims.append(graph_claim("c2", text="离岸架构可以完全规避申报义务",
                                      refs=[], status="insufficient_evidence"))
        return graph(claims=claims, evidence=evidence, edges=[graph_edge("c1", "article:1")])

    def _pack_for(self, graph_obj):
        return cp.build_context_pack(graph=graph_obj, plan=plan(),
                                     request={"question": QUESTION}, run_id="r1")

    def test_repair_retry_is_triggered_by_a_blocking_violation(self):
        graph_obj = self._graph(with_ungrounded_claim=True)
        pack = self._pack_for(graph_obj)
        os.environ["QA_GROUNDING_GATE"] = "1"
        calls = []

        def client(profile, messages, timeout=90):
            calls.append(messages)
            if len(calls) == 1:
                # 第一次：不指定 claims → 选中全部结论（含那条没有证据的 c2）→ 阻断
                return json.dumps({"status": "ready", "answer": "宽免适用于符合条件的管理人 [1]。",
                                   "sections": {}, "claims": [], "conflicts": [],
                                   "citations": ["article:1"]}, ensure_ascii=False)
            # 第二次：模型按违规提示只保留有证据的结论 → 通过
            return json.dumps({"status": "ready", "answer": "宽免适用于符合条件的管理人 [1]。",
                               "sections": {}, "claims": ["c1"], "conflicts": [],
                               "citations": ["article:1"]}, ensure_ascii=False)

        try:
            synthesizer = qa_synthesis.QaFinalSynthesizer(model_client=client)
            result = synthesizer.generate(
                question=QUESTION, graph=graph_obj, level1={}, level2={}, degradation=[],
                profile=_Profile(), models={"synthesis": "stub"}, context_pack=pack)
            self.assertEqual(len(calls), 2, "阻断级违规必须触发一次修复重试（回炉）")
            self.assertIn("grounding", calls[1][-1]["content"],
                          "重试提示必须带上 grounding 的违规原因")
            self.assertIn("c2", calls[1][-1]["content"], "要指名哪条结论没有证据")
            self.assertEqual([item["claim_id"] for item in result["claims"]], ["c1"])
            qa_contracts.validate_final_answer(result)
        finally:
            os.environ.pop("QA_GROUNDING_GATE", None)

    def test_persistent_violation_is_marked_not_silently_passed(self):
        graph_obj = self._graph(with_ungrounded_claim=True)
        pack = self._pack_for(graph_obj)
        os.environ["QA_GROUNDING_GATE"] = "1"

        def client(profile, messages, timeout=90):
            # 两次都无视违规提示：系统必须显式标注，不许静默放行
            return json.dumps({"status": "ready", "answer": "宽免适用于符合条件的管理人 [1]。",
                               "sections": {}, "claims": [], "conflicts": [],
                               "citations": ["article:1"]}, ensure_ascii=False)

        try:
            synthesizer = qa_synthesis.QaFinalSynthesizer(model_client=client)
            result = synthesizer.generate(
                question=QUESTION, graph=graph_obj, level1={}, level2={}, degradation=[],
                profile=_Profile(), models={"synthesis": "stub"}, context_pack=pack)
            qa_contracts.validate_final_answer(result)
            self.assertNotEqual(result["status"], "ready", "无证据断言不许还标成 ready")
            self.assertTrue(result["degraded"])
            self.assertTrue(result["degradation_reasons"])
        finally:
            os.environ.pop("QA_GROUNDING_GATE", None)

    def test_gate_off_keeps_the_pipeline_behaviour(self):
        graph_obj = self._graph()
        pack = self._pack_for(graph_obj)
        calls = []

        def client(profile, messages, timeout=90):
            calls.append(messages)
            return json.dumps({"status": "ready", "answer": "正常回答 [1]。", "sections": {},
                               "claims": ["c1"], "conflicts": [], "citations": ["article:1"]},
                              ensure_ascii=False)

        synthesizer = qa_synthesis.QaFinalSynthesizer(model_client=client)
        result = synthesizer.generate(
            question=QUESTION, graph=graph_obj, level1={}, level2={}, degradation=[],
            profile=_Profile(), models={"synthesis": "stub"}, context_pack=pack)
        self.assertEqual(len(calls), 1, "闸门关掉时不许有额外的回炉调用")
        self.assertEqual(result["status"], "ready")

    def test_messages_without_a_pack_are_byte_identical(self):
        graph_obj = self._graph()
        baseline = qa_synthesis.QaFinalSynthesizer._messages(
            question=QUESTION, graph=graph_obj, level1={}, level2={}, degradation=[])
        explicit_none = qa_synthesis.QaFinalSynthesizer._messages(
            question=QUESTION, graph=graph_obj, level1={}, level2={}, degradation=[],
            context_pack=None)
        self.assertEqual(baseline, explicit_none)
        body = json.loads(baseline[1]["content"])
        self.assertNotIn("context_pack", body, "没给上下文包就不许凭空出现这一段")

    def test_messages_with_a_pack_add_the_constraints(self):
        graph_obj = self._graph()
        pack = self._pack_for(graph_obj)
        messages = qa_synthesis.QaFinalSynthesizer._messages(
            question=QUESTION, graph=graph_obj, level1={}, level2={}, degradation=[],
            context_pack=pack)
        self.assertIn("context_pack", messages[0]["content"])
        self.assertIn(cp.UNGROUNDED_MARK, messages[0]["content"])
        body = json.loads(messages[1]["content"])
        self.assertIn("context_pack", body)
        self.assertEqual(body["citation_map"], pack["citation_map"])
        self.assertEqual(set(body["citation_map"].values()),
                         {item["evidence_ref"] for item in body["evidence"]},
                         "提示里给模型看的证据必须与引用编号完全一致")
        self.assertTrue(body["context_pack"]["约束"])


class _Profile:
    provider_id = "stub"
    model_id = "stub"
    base_url = ""
    api_key = ""


if __name__ == "__main__":
    unittest.main()

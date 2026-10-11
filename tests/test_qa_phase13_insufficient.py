#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 13 · **P13-06 insufficient-evidence output**。

钉住：
  1. 四条判据**逐条可达**且判据输入可复算（`inputs` 里的数字就是判定的依据）；
  2. **只有硬判据能降档**（`NO_EVIDENCE` / `NO_VERIFIED_CLAIM` / `GROUNDING_BLOCKED`）；
     `RESEARCH_NOT_ANSWERABLE` 单独出现时按告警披露（§14 是"带着不确定性交付"，不是"证据不足"）；
  3. 降档**真的改变用户看到的东西**：`status` → `insufficient_evidence`、正文顶部出现显式说明块、
     `degraded` 置真、`degradation_reasons` 追加判据码；幂等（跑两次逐字相同）；
  4. 输出仍过**冻结** `FINAL_ANSWER_SCHEMA`（一个字段都不加）。
"""
import copy
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
from qa_contracts import validate_final_answer  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402

FLAGS = ("QA_FINAL_VERIFIER", "QA_FINAL_VERIFY_RETRY_MAX")


class _Base(unittest.TestCase):
    def setUp(self):
        self.saved = {name: os.environ.pop(name, None) for name in FLAGS}

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _empty_pair():
    answer = fx08.final_answer(answer="本轮没有取到任何可用证据。", claims=[], citations=[],
                               evidence=[], citation_map={})
    return {}, answer


class ReasonCodeTests(_Base):
    def test_no_evidence_and_no_verified_claim(self):
        graph, answer = _empty_pair()
        report = fv.insufficient_evidence_report(composition=fv.compose_answer(graph=graph,
                                                                              answer=answer),
                                                verification={}, answer=answer, graph=graph,
                                                stop_reason="NO_GAIN")
        self.assertEqual(set(report["reason_codes"]),
                         {"NO_EVIDENCE", "NO_VERIFIED_CLAIM", "RESEARCH_NOT_ANSWERABLE"})
        self.assertEqual(set(report["trigger_codes"]), {"NO_EVIDENCE", "NO_VERIFIED_CLAIM"})
        self.assertTrue(report["insufficient"])
        self.assertTrue(validate("insufficient_evidence", report)[0])
        self.assertEqual(report["inputs"]["evidence"], 0)
        self.assertEqual(report["inputs"]["verified_claims"], 0)
        self.assertEqual(report["inputs"]["stop_reason"], "NO_GAIN")

    def test_research_not_answerable_alone_does_not_downgrade(self):
        graph, answer = fx.verified_graph_and_answer()
        report = fv.insufficient_evidence_report(composition=fv.compose_answer(graph=graph,
                                                                              answer=answer),
                                                verification={}, answer=answer, graph=graph,
                                                stop_reason="BUDGET_EXHAUSTED")
        self.assertEqual(report["reason_codes"], ["RESEARCH_NOT_ANSWERABLE"])
        self.assertEqual(report["trigger_codes"], [])
        self.assertFalse(report["insufficient"], "已有已验证证据时不许因为停止原因就降档")
        self.assertEqual(report["status"], "ready")

    def test_grounding_blocked_is_a_hard_trigger(self):
        graph, answer = fx.verified_graph_and_answer()
        report = fv.insufficient_evidence_report(
            composition=fv.compose_answer(graph=graph, answer=answer),
            verification={"blocking_codes": ["CITATION_MISBOUND"]}, answer=answer, graph=graph,
            stop_reason="ANSWERABLE")
        self.assertEqual(report["trigger_codes"], ["GROUNDING_BLOCKED"])
        self.assertTrue(report["insufficient"])

    def test_answerable_verified_answer_is_not_insufficient(self):
        graph, answer = fx.verified_graph_and_answer()
        report = fv.insufficient_evidence_report(composition=fv.compose_answer(graph=graph,
                                                                              answer=answer),
                                                verification={"blocking_codes": []}, answer=answer,
                                                graph=graph, stop_reason="ANSWERABLE")
        self.assertEqual(report["reason_codes"], [])
        self.assertFalse(report["insufficient"])


class OutputShapeTests(_Base):
    def _insufficient(self):
        graph, answer = _empty_pair()
        layer = fv.final_verification_layer(graph=graph, answer=answer, question=fx.QUESTION,
                                            stop_reason="NO_GAIN")
        return layer

    def test_status_and_body_change_for_the_user(self):
        layer = self._insufficient()
        answer = layer["answer"]
        self.assertEqual(answer["status"], "insufficient_evidence")
        self.assertTrue(answer["degraded"])
        self.assertTrue(answer["answer"].startswith(fv.INSUFFICIENT_HEADER))
        self.assertIn("NO_VERIFIED_CLAIM", answer["answer"])
        self.assertIn("NO_GAIN", answer["answer"])
        self.assertTrue(any("证据不足输出" in reason for reason in answer["degradation_reasons"]))
        validate_final_answer(answer)

    def test_blank_message_lists_what_is_missing_and_where_to_look(self):
        graph, answer = fx.verified_graph_and_answer()
        graph["evidence_graph"] = {"gap_review": {"gaps": [
            {"gap_id": "g1", "gap_type": "MISSING_COUNTEREVIDENCE", "claim_id": "c1",
             "priority": 0.9, "band": "critical", "suggested_routes": ["keyword", "graph"],
             "status": "open", "detail": "缺反证"}]}}
        composition = fv.compose_answer(graph=graph, answer=answer)
        report = fv.insufficient_evidence_report(
            composition=composition, verification={"blocking_codes": ["FACT_WITHOUT_EVIDENCE"]},
            answer=answer, graph=graph, stop_reason="MAX_DEPTH")
        marked = fv.apply_insufficient_evidence(answer, report)
        self.assertIn("仍缺", marked["answer"])
        self.assertIn("MISSING_COUNTEREVIDENCE", marked["answer"])
        self.assertIn("建议补齐渠道", marked["answer"])
        self.assertIn("keyword", marked["answer"])
        validate_final_answer(marked)

    def test_apply_is_idempotent_and_pure(self):
        layer = self._insufficient()
        before = copy.deepcopy(layer["answer"])
        report = layer["insufficient"]
        once = fv.apply_insufficient_evidence(before, report)
        twice = fv.apply_insufficient_evidence(once, report)
        self.assertEqual(once, twice, "同一份回执跑两次必须逐字相同")
        self.assertEqual(before, layer["answer"], "原答案对象不许被就地改写")

    def test_not_insufficient_leaves_the_answer_alone(self):
        graph, answer = fx.verified_graph_and_answer()
        report = fv.insufficient_evidence_report(composition=fv.compose_answer(graph=graph,
                                                                              answer=answer),
                                                verification={}, answer=answer, graph=graph,
                                                stop_reason="ANSWERABLE")
        self.assertEqual(fv.apply_insufficient_evidence(answer, report), answer)

    def test_verified_answer_is_delivered_unchanged_by_the_layer(self):
        graph, answer = fx.verified_graph_and_answer()
        layer = fv.final_verification_layer(graph=graph, answer=answer, question=fx.QUESTION,
                                            stop_reason="ANSWERABLE")
        self.assertEqual(layer["verification"]["outcome"], "pass")
        self.assertFalse(layer["insufficient"]["insufficient"])
        self.assertEqual(layer["answer"]["status"], "ready")
        self.assertEqual(layer["answer"]["answer"], answer["answer"])
        self.assertEqual(layer["answer"]["degradation_reasons"], [])

    def test_warning_only_failure_is_disclosed_without_downgrading(self):
        """只剩余告警时：按 Phase 08 的口径**披露但不降档**（status 仍是 ready）。

        刻意 `attempts=0`：默认会走一次局部重试，而重跑相关 Verifier 可能**真的**把这条
        结论重新核验成 SUPPORTED（那是好事，另有专门用例），这里要观察的是"修不掉时的披露口径"。
        也刻意留一条 **SUPPORTED** 的结论：一条都没通过核验时本来就应该走证据不足输出
        （`NO_VERIFIED_CLAIM` 是硬判据），那不是"只剩余告警"的场景。
        """
        graph, answer = fx.verified_graph_and_answer(extra_claim=True)
        layer = fv.final_verification_layer(graph=graph, answer=answer, question=fx.QUESTION,
                                            stop_reason="ANSWERABLE", attempts=0)
        self.assertEqual(layer["verification"]["outcome"], "fail")
        self.assertEqual(layer["verification"]["blocking_codes"], [],
                         "这一轮的失败必须全是告警级")
        self.assertFalse(layer["insufficient"]["insufficient"],
                         "已有已验证结论 → 不许因为告警就降成证据不足")
        self.assertEqual(layer["answer"]["status"], "ready")
        self.assertTrue(layer["answer"]["degradation_reasons"])
        validate_final_answer(layer["answer"])

    def test_verifier_retry_can_really_restore_a_missing_conclusion(self):
        """§17 的"只重跑相关 Verifier"在真实规则下**能修好**缺核验的结论（不是空转）。"""
        graph, answer = fx.verified_graph_and_answer(status="unverified", verdict="UNVERIFIED")
        layer = fv.final_verification_layer(graph=graph, answer=answer, question=fx.QUESTION,
                                            stop_reason="ANSWERABLE")
        self.assertEqual([row["scope"] for row in layer["retries"]], [["composer", "verifier"]])
        self.assertEqual(layer["retries"][0]["reason"], "FIXED")
        self.assertEqual(layer["retries"][0]["retrieval_calls"], 0)
        self.assertEqual(graph["claims"][0]["claim"]["verification_status"], "confirmed")
        self.assertEqual(layer["verification"]["outcome"], "pass")
        self.assertEqual(layer["answer"]["status"], "ready")

    def test_layer_failure_never_loses_the_answer(self):
        graph, answer = fx.verified_graph_and_answer()
        layer = fv.final_verification_layer(graph=graph, answer=answer, question=fx.QUESTION,
                                            verify_fn=None, attempts=0)
        self.assertEqual(layer["error"], "")
        self.assertEqual(layer["answer"]["answer"], answer["answer"])

    def test_contract_and_receipt_identity(self):
        layer = fv.final_verification_layer(graph={}, answer=_empty_pair()[1], attempts=0,
                                            stop_reason="NO_GAIN")
        receipt = layer["receipt"]
        self.assertEqual(receipt["composer_version"], contracts.ANSWER_COMPOSER_VERSION)
        self.assertEqual(receipt["retry_version"], contracts.FINAL_VERIFY_RETRY_VERSION)
        self.assertEqual(receipt["insufficient_version"], contracts.INSUFFICIENT_EVIDENCE_VERSION)
        self.assertTrue(receipt["insufficient"])
        self.assertEqual(receipt["status"], "insufficient_evidence")
        self.assertEqual(len(receipt["checks"]), len(contracts.FINAL_VERIFIER_CHECKS))
        self.assertNotIn("answer", receipt, "回执不带正文（日志与 SSE 只放口径与计数）")

    def test_rounds_expose_each_round(self):
        os.environ["QA_FINAL_VERIFY_RETRY_MAX"] = "1"
        graph, answer = fx.verified_graph_and_answer(status="unverified", verdict="UNVERIFIED")
        layer = fv.final_verification_layer(graph=graph, answer=answer, question=fx.QUESTION)
        self.assertGreaterEqual(len(layer["rounds"]), 1)
        self.assertEqual([row["round"] for row in layer["rounds"]],
                         list(range(1, len(layer["rounds"]) + 1)))
        for row in layer["rounds"]:
            self.assertIn(row["outcome"], contracts.FINAL_VERIFIER_OUTCOMES)
            self.assertTrue(validate("final_retry", {"retry_version": contracts.FINAL_VERIFY_RETRY_VERSION,
                                                    "attempt": 1, "max_attempts": 1,
                                                    "scope": [], "reason": ""})[0] is not None)


if __name__ == "__main__":
    unittest.main()

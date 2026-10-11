#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 13 · **P13-05 local retry（§17 只重跑 Composer / 只重跑相关 Verifier）**。

钉住：
  1. 作用域**只有两个**（`composer` / `verifier`），且完全由失败码表决定（可复算）；
  2. 重跑 Composer **真的改变答案**（摘掉绑不上的结论 / 重绑引用 / 把反证与未解决缺口写进正文），
     改完仍过**冻结** `FINAL_ANSWER_SCHEMA`；
  3. 重跑 Verifier **只覆盖受影响 claim**（没被点名的 claim 一个字不变、证据集被缩到子图），
     并且**零检索**（`retrieval_calls` 恒 0）；
  4. 有界 + 幂等：上限 `QA_FINAL_VERIFY_RETRY_MAX`（默认 1）；重跑后结论一字没变记 `NO_CHANGE`
     并停止；同输入两次运行逐字相同；上限 0 时一次都不重试。
"""
import copy
import json
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


class PlanRetryTests(_Base):
    def test_scope_only_uses_the_two_spec_exits(self):
        for code, scope in fv.RETRY_SCOPE_BY_CODE.items():
            self.assertIn(code, contracts.FINAL_VERIFIER_CHECKS)
            self.assertIn(scope, contracts.FINAL_RETRY_SCOPES)
        planning = fv.plan_retry({"failed_checks": ["FACT_WITHOUT_EVIDENCE",
                                                    "EVIDENCE_NOT_SUPPORTING"]})
        self.assertEqual(planning["scope"], ["composer", "verifier"],
                         "顺序固定（先 verifier 再 composer 在执行时体现）")
        self.assertEqual(planning["scope_by_code"],
                         {"EVIDENCE_NOT_SUPPORTING": "verifier",
                          "FACT_WITHOUT_EVIDENCE": "composer"})

    def test_no_failed_checks_means_no_scope(self):
        self.assertEqual(fv.plan_retry({"failed_checks": []})["scope"], [])
        record = fv.run_local_retry(graph={}, answer={}, composition={},
                                    verification={"failed_checks": []})
        self.assertEqual(record["reason"], "NO_SCOPE")
        self.assertEqual(record["scope"], [])
        self.assertEqual(record["retrieval_calls"], 0)


class ComposerRetryTests(_Base):
    def test_composer_retry_really_changes_the_answer(self):
        graph, answer = fx.verified_graph_and_answer()
        # 一条绑不上的结论 + 一条没被引用的反证 + 一个未解决缺口
        graph["claims"].append(fx08.graph_claim("c9", text="一条没有出处的自由生成结论",
                                                refs=(), verification_status="unverified"))
        answer["claims"].append(fx08.answer_claim("c9", (), text="一条没有出处的自由生成结论"))
        refute = fx08.evidence_item("article:2", url="https://example.com/2",
                                    verification=fx08.verification("REFUTED"))
        graph["claims"][0]["claim"]["evidence_refs"] = ["article:1", "article:2"]
        graph["claims"][0]["edges"] = [
            fx08.graph_edge("c1", "article:1", status="SUPPORTED"),
            fx08.graph_edge("c1", "article:2", relation="REFUTES", verified=False, status="REFUTED")]
        graph["evidence"].append(refute)
        graph["evidence_graph"] = {"gap_review": {"gaps": [
            {"gap_id": "g1", "gap_type": "MISSING_COUNTEREVIDENCE", "claim_id": "c1",
             "priority": 0.9, "band": "critical", "suggested_routes": ["keyword"],
             "status": "open", "detail": "缺反证"}]}}
        composition = fv.compose_answer(graph=graph, answer=answer)
        verification = fv.verify_answer(answer=answer, composition=composition, graph=graph)
        self.assertIn("FACT_WITHOUT_EVIDENCE", verification["failed_checks"])
        outcome = fv.retry_composer(graph=graph, answer=answer, composition=composition)
        fresh = outcome["answer"]
        actions = {row["action"] for row in outcome["changes"]}
        self.assertIn("drop_unbound_claims", actions)
        self.assertIn("present_counter_and_gaps", actions)
        self.assertNotIn("c9", [row.get("claim_id") for row in fresh["claims"]])
        self.assertIn("反证", fresh["answer"])
        self.assertIn("未解决缺口", fresh["answer"])
        self.assertEqual(outcome["dropped_claims"], ["c9"])
        self.assertTrue(outcome["added_counter_evidence"])
        self.assertEqual([row["gap_id"] for row in outcome["added_gaps"]], ["g1"])
        # 改完必须仍过**冻结**契约
        from qa_contracts import validate_final_answer

        validate_final_answer(fresh)

    def test_composer_retry_rebinds_citations(self):
        graph, answer = fx.verified_graph_and_answer()
        answer["citations"] = ["article:1", "article:404"]
        composition = fv.compose_answer(graph=graph, answer=answer)
        outcome = fv.retry_composer(graph=graph, answer=answer, composition=composition)
        self.assertEqual(outcome["answer"]["citations"], ["article:1"])
        self.assertIn("rebind_citations", {row["action"] for row in outcome["changes"]})

    def test_composer_retry_is_a_noop_when_nothing_is_wrong(self):
        graph, answer = fx.verified_graph_and_answer()
        composition = fv.compose_answer(graph=graph, answer=answer)
        outcome = fv.retry_composer(graph=graph, answer=answer, composition=composition)
        self.assertEqual(outcome["changes"], [])
        self.assertEqual(outcome["answer"]["answer"], answer["answer"])


class VerifierRetryTests(_Base):
    def _pair(self):
        graph, answer = fx.verified_graph_and_answer(extra_claim=True)
        # c2 的核验结论缺失（触发第②问），c1 完好
        graph["claims"][1].pop("verification", None)
        graph["claims"][1]["claim"]["verification_status"] = "unverified"
        answer["claims"][1]["verification_status"] = "unverified"
        return graph, answer

    def test_only_the_affected_claim_is_reverified(self):
        graph, answer = self._pair()
        composition = fv.compose_answer(graph=graph, answer=answer)
        verification = fv.verify_answer(answer=answer, composition=composition, graph=graph)
        self.assertIn("EVIDENCE_NOT_SUPPORTING", verification["failed_checks"])
        c1_before = copy.deepcopy(graph["claims"][0])
        c2_before = copy.deepcopy(graph["claims"][1])
        seen = {}
        def _fake(scoped, **kwargs):
            seen["claims"] = [str(node.get("claim_id") or node.get("canonical_id"))
                              for node in scoped["claims"]]
            seen["evidence"] = [str(item.get("evidence_ref")) for item in scoped["evidence"]]
            seen["kwargs"] = sorted(kwargs)
            return {"stats": {"claims": len(scoped["claims"])}}

        outcome = fv.retry_verifier(graph=graph, composition=composition,
                                    verification=verification, verify_fn=_fake)
        self.assertEqual(seen["claims"], ["c2"], "只重跑受影响 claim 的核验")
        self.assertEqual(seen["evidence"], ["article:2"], "证据集被缩到该 claim 引用的子图")
        self.assertEqual(graph["claims"][0], c1_before, "没被点名的 claim 一个字不许变")
        self.assertEqual(graph["claims"][1], c2_before, "核验器只读该 claim，节点本身不被改写")
        self.assertEqual(outcome["before"], {"c2": "unverified"})
        self.assertEqual(outcome["scope_claims"], ["c2"])

    def test_real_verification_changes_only_the_named_claim(self):
        graph, answer = self._pair()
        composition = fv.compose_answer(graph=graph, answer=answer)
        verification = fv.verify_answer(answer=answer, composition=composition, graph=graph)
        c1_before = copy.deepcopy(graph["claims"][0])
        outcome = fv.retry_verifier(graph=graph, composition=composition, verification=verification)
        self.assertEqual(graph["claims"][0], c1_before)
        self.assertTrue(outcome["scope_claims"])
        self.assertIn(graph["claims"][1]["claim"]["verification_status"],
                      ("confirmed", "qualified", "unverified", "conflicted", "insufficient_evidence"))

    def test_missing_scope_is_reported_not_faked(self):
        graph, answer = self._pair()
        composition = fv.compose_answer(graph=graph, answer=answer)
        verification = {"failed_checks": ["EVIDENCE_NOT_SUPPORTING"], "checks": [
            {"check_id": "EVIDENCE_NOT_SUPPORTING", "failures": [{"claim_id": "c404"}]}]}
        outcome = fv.retry_verifier(graph=graph, composition=composition,
                                    verification=verification)
        self.assertEqual(outcome["reason"], "SCOPE_UNAVAILABLE")
        self.assertEqual(outcome["scope_claims"], [])


class BoundedIdempotentTests(_Base):
    def test_zero_limit_never_retries(self):
        os.environ["QA_FINAL_VERIFY_RETRY_MAX"] = "0"
        graph, answer = fx.verified_graph_and_answer(status="unverified", verdict="UNVERIFIED")
        layer = fv.final_verification_layer(graph=graph, answer=answer, question=fx.QUESTION)
        self.assertEqual(layer["retries"], [])
        self.assertEqual(layer["stats"]["retrieval_calls"], 0)
        self.assertEqual(layer["stats"]["retries"], 0)

    def test_retry_limit_is_recorded_when_attempt_exceeds_it(self):
        record = fv.run_local_retry(graph={}, answer={}, composition={},
                                    verification={"failed_checks": ["FACT_WITHOUT_EVIDENCE"]},
                                    attempt=2, max_attempts=1)
        self.assertEqual(record["reason"], "RETRY_LIMIT_REACHED")
        self.assertEqual(record["scope"], ["composer"])

    def test_two_runs_are_byte_identical(self):
        os.environ["QA_FINAL_VERIFY_RETRY_MAX"] = "2"
        graph, answer = fx.verified_graph_and_answer(status="unverified", verdict="UNVERIFIED")
        first = fv.final_verification_layer(graph=copy.deepcopy(graph),
                                            answer=copy.deepcopy(answer), question=fx.QUESTION)
        second = fv.final_verification_layer(graph=copy.deepcopy(graph),
                                             answer=copy.deepcopy(answer), question=fx.QUESTION)
        for key in ("answer", "retries", "receipt"):
            self.assertEqual(json.dumps(first[key], ensure_ascii=False, sort_keys=True),
                             json.dumps(second[key], ensure_ascii=False, sort_keys=True), key)

    def test_retry_stops_when_nothing_changed(self):
        """结论一字没变 → `NO_CHANGE` 并停止（不许无限重试）。"""
        graph, answer = fx.verified_graph_and_answer()
        composition = fv.compose_answer(graph=graph, answer=answer)
        verification = {"failed_checks": ["EVIDENCE_NOT_SUPPORTING"], "checks": [
            {"check_id": "EVIDENCE_NOT_SUPPORTING", "passed": False,
             "failures": [{"claim_id": "c1", "severity": "warning"}]}]}
        record = fv.run_local_retry(graph=graph, answer=answer, composition=composition,
                                    verification=verification, attempt=1, max_attempts=2)
        self.assertEqual(record["scope"], ["verifier"])
        self.assertIn(record["reason"], ("NO_CHANGE", "FIXED"))
        self.assertEqual(record["retrieval_calls"], 0)

    def test_record_passes_its_contract(self):
        graph, answer = fx.verified_graph_and_answer()
        composition = fv.compose_answer(graph=graph, answer=answer)
        record = fv.run_local_retry(graph=graph, answer=answer,
                                    composition=composition,
                                    verification={"failed_checks": ["CITATION_MISBOUND"]},
                                    attempt=1, max_attempts=1)
        clean = {key: value for key, value in record.items() if not key.startswith("_")}
        self.assertTrue(contracts.validate("final_retry", clean)[0])
        self.assertEqual(clean["retrieval_calls"], 0)


if __name__ == "__main__":
    unittest.main()

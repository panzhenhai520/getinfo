#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 13 · **P13-04 Final Verifier（§17 六问）**。

钉住：
  1. 六问**逐字**来自 §17（`FINAL_VERIFIER_QUESTIONS` 与 `FINAL_VERIFIER_CHECKS` 键集相等、
     顺序与规格书一致），每一问都有 `derived_from` 指向**上游单一真源**；
  2. **不另造质量判断**：把 Phase 08 的 grounding 回执换掉，终验结论跟着变（守门用例）；
     分级**永不比 Phase 08 更严**（Phase 08 判告警的，这里不许判阻断）；
  3. 六问**都能真的失败**（每一问一个针对性构造 + 一个全绿通过路径）；
  4. 所有 `derived_from` 里点名的上游符号**真的存在**（机器可校验，不许写空头引用）；
  5. 零模型 / 零网络（AST 守门）。
"""
import ast
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
import qa_verifier  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402

NETWORK_MODULES = {"requests", "urllib", "urllib3", "httpx", "socket", "aiohttp", "http"}
MODEL_CLIENTS = {"openai", "anthropic", "dashscope", "zhipuai", "transformers", "torch"}


def _source(name: str) -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, name), encoding="utf-8") as handle:
        return handle.read()


def _check_map(verification):
    return {row["check_id"]: row for row in verification["checks"]}


class ShapeTests(unittest.TestCase):
    def test_six_checks_match_spec_seventeen(self):
        verification = _verify(fx.verified_graph_and_answer())
        self.assertEqual([row["check_id"] for row in verification["checks"]],
                         list(contracts.FINAL_VERIFIER_CHECKS))
        for row in verification["checks"]:
            self.assertEqual(row["question"], contracts.FINAL_VERIFIER_QUESTIONS[row["check_id"]])
            self.assertTrue(row["derived_from"], "每一问都必须写明派生来源")
        self.assertEqual(set(contracts.FINAL_VERIFIER_QUESTIONS),
                         set(contracts.FINAL_VERIFIER_CHECKS))
        self.assertEqual(sorted(fv.RETRY_SCOPE_BY_CODE), sorted(contracts.FINAL_VERIFIER_CHECKS),
                         "六问每一问都要有重试作用域（否则失败后无处可去）")
        self.assertTrue(validate("final_verification", verification)[0])

    def test_pass_path(self):
        verification = _verify(fx.verified_graph_and_answer())
        self.assertEqual(verification["outcome"], "pass")
        self.assertEqual(verification["failed_checks"], [])
        self.assertEqual(verification["blocking_codes"], [])
        self.assertTrue(all(row["severity"] == "none" for row in verification["checks"]))

    def test_derived_from_points_at_real_upstream_symbols(self):
        """`derived_from` 里点名的上游符号必须真的存在（不许写空头引用）。"""
        known = {"qa-grounding-v1:%s" % code for code in contracts.CONTEXT_GROUNDING_VIOLATIONS}
        known |= {"qa-verifier-v1:%s" % name for name in
                  ("claim_pairs.verdict", "claim_pairs.score", "check_numbers")}
        known |= {"qa-answer-composer-v1:binding", "qa-answer-composer-v1:counter_evidence",
                  "qa-context-pack-v1:citation_index",
                  "qa-evidence-graph-v1:REFUTES",
                  "qa-execution-graph-v1:plan_only"}
        for check_id, sources in fv.DERIVED_FROM.items():
            self.assertIn(check_id, contracts.FINAL_VERIFIER_CHECKS)
            for source in sources:
                self.assertIn(source, known, "%s 的来源 %s 不是已知的上游符号" % (check_id, source))
        # 点名的函数与枚举真的存在
        self.assertTrue(callable(qa_verifier.check_numbers))
        self.assertTrue(callable(qa_verifier.verify_claim_graph))
        self.assertTrue(callable(cp.check_grounding))
        self.assertIn("REFUTES", contracts.EVIDENCE_GRAPH_RELATIONSHIPS)


def _verify(pair, *, grounding=None, pack=None):
    graph, answer = pair
    composition = fv.compose_answer(graph=graph, answer=answer)
    return fv.verify_answer(answer=answer, composition=composition, graph=graph,
                            pack=pack, grounding=grounding)


def _failing(verification, check_id):
    return not _check_map(verification)[check_id]["passed"]


class CheckOneFactWithoutEvidenceTests(unittest.TestCase):
    def test_claim_in_answer_without_any_evidence_fails_blocking(self):
        graph = fx08.graph(claims=[fx08.graph_claim("c1", refs=())],
                           evidence=[fx08.evidence_item("article:1")])
        answer = fx08.final_answer(answer="一句没有出处的断言。",
                                   claims=[fx08.answer_claim("c1", ())], citations=[],
                                   evidence=graph["evidence"], citation_map={})
        verification = _verify((graph, answer))
        row = _check_map(verification)["FACT_WITHOUT_EVIDENCE"]
        self.assertFalse(row["passed"])
        self.assertEqual(row["severity"], "blocking")
        self.assertEqual(row["failures"][0]["binding_reason"], "NO_EVIDENCE_REF")

    def test_phase08_violation_alone_flips_the_check(self):
        """**单一真源守门**：只改 Phase 08 的回执，第①问的结论就必须跟着变。"""
        pair = fx.verified_graph_and_answer()
        passing = _verify(pair, grounding={"grounding_version": cp.GROUNDING_VERSION,
                                           "checked_claims": 1, "violations": [], "blocking": False})
        self.assertTrue(_check_map(passing)["FACT_WITHOUT_EVIDENCE"]["passed"])
        injected = {"grounding_version": cp.GROUNDING_VERSION, "checked_claims": 1,
                    "blocking": True, "violation_counts": {"CITATION_WITHOUT_SPAN": 1},
                    "violations": [{"code": "CITATION_WITHOUT_SPAN", "claim_id": "c1",
                                    "detail": "包内不可回溯"}]}
        failing = _verify(pair, grounding=injected)
        row = _check_map(failing)["FACT_WITHOUT_EVIDENCE"]
        self.assertFalse(row["passed"], "Phase 08 的回执变了，终验必须跟着变")
        self.assertEqual(row["severity"], "blocking")
        self.assertEqual(row["failures"][0]["source"], "qa-grounding-v1:CITATION_WITHOUT_SPAN")


class CheckTwoEvidenceSupportsTests(unittest.TestCase):
    def test_conflicted_claim_is_blocking(self):
        # REFUTED verdict → Phase 03 判 conflicted
        graph = fx08.graph(claims=[fx08.graph_claim("c1", text=fx.CLAIM_TEXT, refs=("article:1",))],
                           evidence=[fx08.evidence_item("article:1",
                                                        verification=fx08.verification("REFUTED"))])
        fx.with_verification(graph["claims"][0], [fx.pair("article:1", "REFUTED")], "conflicted")
        answer = fx08.final_answer(answer=fx.CLAIM_TEXT + " [1]。",
                                   claims=[fx08.answer_claim("c1", ("article:1",), status="conflicted")],
                                   citations=["article:1"], evidence=graph["evidence"],
                                   citation_map={"[1]": "article:1"})
        verification = _verify((graph, answer))
        row = _check_map(verification)["EVIDENCE_NOT_SUPPORTING"]
        self.assertFalse(row["passed"])
        self.assertEqual(row["severity"], "blocking")
        self.assertEqual(row["inputs"]["REFUTED"], 1)

    def test_unverified_claim_is_only_a_warning(self):
        """分级**不许比 Phase 08 更严**：Phase 08 对"引用的证据没通过核验"给的是告警。"""
        graph, answer = fx.verified_graph_and_answer(status="unverified", verdict="UNVERIFIED")
        verification = _verify((graph, answer))
        row = _check_map(verification)["EVIDENCE_NOT_SUPPORTING"]
        self.assertFalse(row["passed"])
        self.assertEqual(row["severity"], "warning", "告警级不许被终验私自升级成阻断")


class CheckThreeOverInferenceTests(unittest.TestCase):
    def test_number_beyond_the_span_is_reported(self):
        graph, answer = fx.verified_graph_and_answer()
        answer["answer"] = "优惠税率为 16.5%，覆盖 300 家企业 [1]。"
        verification = _verify((graph, answer))
        row = _check_map(verification)["OVER_INFERENCE"]
        self.assertFalse(row["passed"])
        # Phase 08 把 NUMBER_WITHOUT_SPAN 定为告警级 → 派生出来的这一问不许升级成阻断
        self.assertEqual(row["severity"], "warning")
        self.assertIn("NUMBER_WITHOUT_SPAN", verification["grounding"]["violation_counts"])

    def test_claim_number_beyond_its_own_span_is_reported(self):
        graph, answer = fx.verified_graph_and_answer()
        graph["claims"][0]["claim"]["text"] = "香港家族办公室税收优惠政策覆盖 300 家企业"
        answer["claims"][0]["text"] = "香港家族办公室税收优惠政策覆盖 300 家企业"
        verification = _verify((graph, answer))
        row = _check_map(verification)["OVER_INFERENCE"]
        self.assertFalse(row["passed"])
        self.assertEqual(row["severity"], "warning")
        self.assertEqual(row["inputs"]["numbers_over_reach"], 1)
        self.assertEqual(row["failures"][0]["source"], "qa-verifier-v1:check_numbers")

    def test_evidence_present_but_not_entailing_is_a_warning(self):
        graph, answer = fx.verified_graph_and_answer(verdict="UNVERIFIED", status="unverified")
        verification = _verify((graph, answer))
        row = _check_map(verification)["OVER_INFERENCE"]
        self.assertFalse(row["passed"])
        self.assertEqual(row["severity"], "warning")
        self.assertEqual(row["inputs"]["claims_not_entailed"], 1)


class CheckFourCounterEvidenceTests(unittest.TestCase):
    def test_omitted_counter_evidence_is_reported(self):
        import test_qa_phase13_counter_gaps as cg

        graph = cg._counter_graph()
        answer = fx08.final_answer(answer="只讲正面结论 [1]。",
                                   claims=[fx08.answer_claim("c1", ("article:1",))],
                                   citations=["article:1"], evidence=graph["evidence"],
                                   citation_map={"[1]": "article:1"})
        verification = _verify((graph, answer))
        row = _check_map(verification)["COUNTER_EVIDENCE_MISSING"]
        self.assertFalse(row["passed"])
        self.assertEqual(row["severity"], "warning")
        self.assertEqual(row["inputs"]["omitted"], 1)

    def test_cited_counter_evidence_passes(self):
        import test_qa_phase13_counter_gaps as cg

        graph = cg._counter_graph()
        answer = fx08.final_answer(answer="正面结论 [1]，但也有相反资料 [2]。",
                                   claims=[fx08.answer_claim("c1", ("article:1", "article:2"))],
                                   citations=["article:1", "article:2"], evidence=graph["evidence"],
                                   citation_map={"[1]": "article:1", "[2]": "article:2"})
        self.assertTrue(_check_map(_verify((graph, answer)))["COUNTER_EVIDENCE_MISSING"]["passed"])


class CheckFiveCitationTests(unittest.TestCase):
    def test_label_pointing_at_a_different_evidence_fails(self):
        graph, answer = fx.verified_graph_and_answer(extra_claim=True)
        answer["citation_map"] = {"[1]": "article:2", "[2]": "article:1"}
        verification = _verify((graph, answer))
        row = _check_map(verification)["CITATION_MISBOUND"]
        self.assertFalse(row["passed"])
        # 没有包（编号本来可重算）→ 告警级；有包时才是阻断级（见下一条）
        self.assertEqual(row["severity"], "warning")
        self.assertEqual(row["inputs"]["index_source"], "derived")
        self.assertEqual(row["inputs"]["mismatched"], 2)

    def test_with_a_pack_the_same_mismatch_is_blocking(self):
        """有上下文包时编号↔证据是**权威**的（Phase 08 与生成端同口径）→ 不一致就是阻断。"""
        graph, answer = fx.verified_graph_and_answer(extra_claim=True)
        pack = cp.build_context_pack(graph=graph, run_id="p13")
        swapped = {"[1]": pack["citation_map"]["[2]"], "[2]": pack["citation_map"]["[1]"]}
        answer["citation_map"] = swapped
        composition = fv.compose_answer(graph=graph, answer=answer, pack=pack)
        verification = fv.verify_answer(answer=answer, composition=composition, graph=graph,
                                        pack=pack)
        row = _check_map(verification)["CITATION_MISBOUND"]
        self.assertFalse(row["passed"])
        self.assertEqual(row["severity"], "blocking")
        self.assertEqual(row["inputs"]["index_source"], "pack")

    def test_dangling_citation_fails(self):
        graph, answer = fx.verified_graph_and_answer()
        answer["citations"] = ["article:1", "article:404"]
        row = _check_map(_verify((graph, answer)))["CITATION_MISBOUND"]
        self.assertFalse(row["passed"])
        self.assertEqual(row["inputs"]["dangling"], 1)
        self.assertEqual(row["severity"], "warning",
                         "citations 里多出的引用：Phase 08 不查这一层，这里也只许告警")


class CheckSixSpeculationTests(unittest.TestCase):
    def test_plan_only_claim_asserted_is_blocking(self):
        graph, answer = fx.verified_graph_and_answer()
        graph["claims"][0]["plan_only"] = True
        verification = _verify((graph, answer))
        row = _check_map(verification)["SPECULATION_AS_FACT"]
        self.assertFalse(row["passed"])
        self.assertEqual(row["severity"], "blocking")
        self.assertEqual(row["inputs"]["plan_only_in_answer"], 1)

    def test_unsupported_claim_without_a_hedge_is_a_warning(self):
        graph, answer = fx.verified_graph_and_answer(status="unverified", verdict="UNVERIFIED")
        verification = _verify((graph, answer))
        row = _check_map(verification)["SPECULATION_AS_FACT"]
        self.assertFalse(row["passed"])
        self.assertEqual(row["severity"], "warning")

    def test_uncertainty_mark_satisfies_the_check(self):
        graph, answer = fx.verified_graph_and_answer(status="unverified", verdict="UNVERIFIED")
        answer["answer"] = "香港家族办公室税收优惠政策的影响尚不确定 [1]。"
        verification = _verify((graph, answer))
        row = _check_map(verification)["SPECULATION_AS_FACT"]
        self.assertTrue(row["passed"], "不确定性表达必须能消掉这一问")

    def test_phase08_unmarked_text_violation_is_reported_not_escalated(self):
        pair = fx.verified_graph_and_answer()
        injected = {"grounding_version": cp.GROUNDING_VERSION, "blocking": True, "checked_claims": 1,
                    "violations": [{"code": "UNMARKED_UNGROUNDED_TEXT", "claim_ids": ["c7"],
                                    "detail": "有结论没有可回溯证据、正文也没标注"}]}
        row = _check_map(_verify(pair, grounding=injected))["SPECULATION_AS_FACT"]
        self.assertFalse(row["passed"])
        self.assertEqual(row["severity"], "warning",
                         "Phase 08 判告警的这一条不许被终验私自升级成阻断")


class NoModelNoNetworkGuardTests(unittest.TestCase):
    def test_no_network_or_model_imports(self):
        tree = ast.parse(_source("qa_final_verifier.py"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported & NETWORK_MODULES, set(), imported & NETWORK_MODULES)
        self.assertEqual(imported & MODEL_CLIENTS, set(), imported & MODEL_CLIENTS)

    def test_no_endpoint_literals(self):
        source = _source("qa_final_verifier.py")
        for token in ("http://", "https://", "/v1/chat", "/v1/embeddings", "embedding_client",
                      "_embed_question", "openai", "api_key"):
            self.assertNotIn(token, source, token)


if __name__ == "__main__":
    unittest.main()

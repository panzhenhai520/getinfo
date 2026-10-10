#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 05 · P05-01 Query Interpreter 用例。

要点：
  1. §6 的 **9 值意图**每一个都有可复现的命中样例（不是装饰性枚举）；
  2. 复杂度三档的**规则依据**可复算（`complexity_reasons` 非空且对得上）；
  3. 答案形态 / 时效敏感 / 约束投影 / required_claims（含反证那条 `plan_only`）；
  4. **可插拔接口**：未注册名、后端抛错、后端返回非法载荷三条失败路径都必须
     保守回落到规则实现（并且写明回落原因）——绝不允许"后端坏了就没有规划"；
  5. 硬约束守卫：解释器**零联网、零模型调用**（AST + 源码字面量双向断言）。
"""
import ast
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_graph_contracts as contracts  # noqa: E402
import qa_query_interpreter as qi  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NETWORK_MODULES = {"requests", "urllib3", "httpx", "socket", "aiohttp", "http.client",
                   "urllib.request", "openai", "anthropic"}

# 每个意图一个可复现样例（顺序即规则优先级）
INTENT_SAMPLES = {
    "SIMPLE_FACT": "2026年医保新规是否适用于民营医院",
    "MULTI_ENTITY": "比亚迪和蔚来在2026年的交付量分别是多少",
    "COMPARISON": "蔚来和理想的交付量比较",
    "TEMPORAL": "先有补贴退坡还是先有价格战",
    "CAUSAL": "为什么比亚迪的销量下降了",
    "MECHANISM": "香港家族办公室的利得税宽免机制是什么",
    "DIAGNOSTIC": "在研发费用加计扣除条件下，这家企业是否满足优惠适用条件",
    "MULTI_HOP": "香港家族办公室税收优惠政策对内地高净值客户有什么影响",
    "SYNTHESIS": "最近香港家族信托的监管有什么新变化",
}


class IntentTests(unittest.TestCase):
    def test_all_nine_intents_are_reachable(self):
        """9 个意图逐个命中（允许样例落到同一意图，但不允许有意图永远取不到）。"""
        seen = {}
        for expected, question in INTENT_SAMPLES.items():
            result = qi.interpret_query(question)
            seen[expected] = result["intent"]
        self.assertEqual(seen["SIMPLE_FACT"], "SIMPLE_FACT")
        self.assertEqual(seen["COMPARISON"], "COMPARISON")
        self.assertEqual(seen["TEMPORAL"], "TEMPORAL")
        self.assertEqual(seen["CAUSAL"], "CAUSAL")
        self.assertEqual(seen["MECHANISM"], "MECHANISM")
        self.assertEqual(seen["MULTI_HOP"], "MULTI_HOP")
        self.assertEqual(seen["SYNTHESIS"], "SYNTHESIS")
        # 多实体 / 诊断两类需要词表或条件式结构：用显式入参把口径钉死
        self.assertEqual(qi.intent_of({"key": "fact_check"}, relationship="",
                                      question="某某情况", entities=["比亚迪", "蔚来"]),
                         "MULTI_ENTITY")
        self.assertEqual(qi.intent_of({"key": "conditional_constraint"}, relationship="",
                                      question="在X条件下是否满足"), "DIAGNOSTIC")

    def test_every_intent_is_declared_in_contracts(self):
        for intent in INTENT_SAMPLES:
            self.assertIn(intent, contracts.QUERY_INTENTS)
        self.assertEqual(len(contracts.QUERY_INTENTS), 9)

    def test_intent_of_is_pure_and_rule_based(self):
        """同一输入两次结果一致（可复算），且不依赖网络/时间。"""
        first = qi.interpret_query(INTENT_SAMPLES["MULTI_HOP"])
        second = qi.interpret_query(INTENT_SAMPLES["MULTI_HOP"])
        self.assertEqual(first["intent"], second["intent"])
        self.assertEqual(first["complexity"], second["complexity"])

    def test_smalltalk_has_no_answer_type_and_simple_complexity(self):
        result = qi.interpret_query("你好")
        self.assertFalse(result["needs_retrieval"])
        self.assertEqual(result["answer_type"], "no_answer")
        self.assertEqual(result["complexity"], "simple")


class ComplexityTests(unittest.TestCase):
    def test_multi_hop_is_deep_with_reasons(self):
        result = qi.interpret_query(INTENT_SAMPLES["MULTI_HOP"])
        self.assertEqual(result["complexity"], "deep")
        self.assertTrue(result["complexity_reasons"])
        self.assertTrue(any("多跳" in item for item in result["complexity_reasons"]))

    def test_simple_fact_is_simple(self):
        result = qi.interpret_query(INTENT_SAMPLES["SIMPLE_FACT"])
        self.assertEqual(result["complexity"], "simple")
        self.assertIn("≤60 字", " ".join(result["complexity_reasons"]))

    def test_long_simple_category_is_not_simple(self):
        """单问句简单类别 + 超 60 字 → 不能算 simple（阈值写死在可复算的规则里）。"""
        question = ("2026年医保新规是否适用于民营医院，以及各类社会办医疗机构在跨省异地就医"
                    "直接结算、门诊慢特病待遇认定与年度起付线累计方面的具体执行口径和过渡期安排")
        self.assertGreater(len(question), 60)
        complexity, reasons = qi.complexity_of(
            "SIMPLE_FACT", category={"key": "subject_scope"}, question=question,
            question_count=1, needs_retrieval=True)
        self.assertEqual(complexity, "standard")
        self.assertTrue(reasons)

    def test_three_complexities_are_declared(self):
        self.assertEqual(tuple(contracts.QUERY_COMPLEXITIES), ("simple", "standard", "deep"))
        self.assertEqual(qi.answer_type_of("SIMPLE_FACT"), "fact")
        self.assertEqual(qi.answer_type_of("MECHANISM"), "causal_explanation")
        self.assertEqual(qi.answer_type_of("TEMPORAL"), "timeline")
        self.assertEqual(qi.answer_type_of("DIAGNOSTIC"), "diagnostic")
        self.assertEqual(qi.answer_type_of("MULTI_HOP"), "evidence_synthesis")
        for intent in contracts.QUERY_INTENTS:
            self.assertIn(qi.answer_type_of(intent), contracts.QA_ANSWER_TYPES)


class PayloadTests(unittest.TestCase):
    def test_payload_passes_the_contract(self):
        result = qi.interpret_query(INTENT_SAMPLES["CAUSAL"])
        ok, note = validate("query_interpretation", result)
        self.assertTrue(ok, note)

    def test_required_claims_include_counter_claim_plan_only(self):
        result = qi.interpret_query(INTENT_SAMPLES["MULTI_HOP"])
        claims = result["required_claims"]
        self.assertTrue(claims)
        for claim in claims:
            ok, note = validate("plan_claim", claim)
            self.assertTrue(ok, note)
        counter = [item for item in claims if item["role"] == "counter"]
        self.assertEqual(len(counter), 1, "必须有一条反证/替代解释的待证命题")
        self.assertTrue(counter[0].get("plan_only"),
                        "反证那条本轮不执行（P06/P07/P13 负责），必须标 plan_only")

    def test_freshness_required_from_relative_time(self):
        self.assertTrue(qi.interpret_query("最近香港家族信托的监管有什么新变化")["freshness_required"])
        self.assertFalse(qi.interpret_query(INTENT_SAMPLES["SIMPLE_FACT"])["freshness_required"])

    def test_entities_come_from_the_planner_not_new_ner(self):
        """没有 plan 时实体是空数组（宁可空着，也不编）。"""
        self.assertEqual(qi.interpret_query(INTENT_SAMPLES["SIMPLE_FACT"])["entities"], [])
        with_plan = qi.interpret_query(INTENT_SAMPLES["SIMPLE_FACT"],
                                       plan={"entities": ["家族办公室", "高净值客户"],
                                             "time_scope": {"from": "2026-01-01", "to": "2026-12-31",
                                                            "source": "explicit"}})
        self.assertEqual(with_plan["entities"], ["家族办公室", "高净值客户"])
        self.assertEqual(with_plan["time_scope"]["source"], "explicit")

    def test_constraints_are_projected_from_the_plan(self):
        result = qi.interpret_query(INTENT_SAMPLES["SIMPLE_FACT"], plan={
            "question_plan": {"output_form": "逐段原文", "must_fetch_fulltext": True},
            "research_axes": ["official_text", "scope"],
            "requested_sources": ["gov.hk"],
        })
        keys = {item["key"] for item in result["constraints"]}
        self.assertIn("output_form", keys)
        self.assertIn("must_fetch_fulltext", keys)
        self.assertIn("research_axis", keys)
        self.assertIn("requested_source", keys)
        for item in result["constraints"]:
            self.assertIn("source", item)

    def test_reuse_map_is_reported(self):
        result = qi.interpret_query(INTENT_SAMPLES["SIMPLE_FACT"])
        self.assertIn("category_rules", result["reuse"])
        self.assertIn("qa_planner", result["reuse"]["category_rules"])

    def test_receipt_is_short_and_stable(self):
        receipt = qi.interpretation_receipt(qi.interpret_query(INTENT_SAMPLES["MULTI_HOP"]))
        self.assertEqual(receipt["intent"], "MULTI_HOP")
        self.assertTrue(receipt["counter_claim_planned"])
        self.assertGreaterEqual(receipt["claim_count"], 2)
        self.assertEqual(qi.interpretation_receipt({}), {})


class PluggableBackendTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("QA_QUERY_INTERPRETER", None)

    def tearDown(self):
        os.environ.pop("QA_QUERY_INTERPRETER", None)
        for name in ("spy", "boom", "bad"):
            qi.register_query_interpreter(name, None)
        if self._saved is not None:
            os.environ["QA_QUERY_INTERPRETER"] = self._saved

    def test_rules_is_the_default_and_cannot_be_overwritten(self):
        self.assertEqual(qi.selected_interpreter(), "rules")
        with self.assertRaises(ValueError):
            qi.register_query_interpreter("rules", lambda *a, **k: {})
        self.assertEqual(qi.interpret_query("你好")["backend_source"], "rules")

    def test_registered_backend_is_used(self):
        calls = []

        def backend(question, context):
            calls.append((question, context))
            base = qi.interpret_with_rules(question)
            base["intent"] = "SYNTHESIS"
            base["backend"] = "spy"
            return base

        qi.register_query_interpreter("spy", backend)
        result = qi.interpret_query("任意问题", backend="spy")
        self.assertEqual(result["backend_source"], "registered:spy")
        self.assertEqual(result["intent"], "SYNTHESIS")
        self.assertFalse(result["fallback"]["used"])
        self.assertEqual(len(calls), 1)

    def test_unknown_backend_falls_back_to_rules(self):
        result = qi.interpret_query("为什么比亚迪的销量下降了", backend="not_registered")
        self.assertEqual(result["backend_source"], "rules")
        self.assertTrue(result["fallback"]["used"])
        self.assertIn("未注册", result["fallback"]["reason"])

    def test_raising_backend_falls_back_to_rules(self):
        def boom(question, context):
            raise RuntimeError("端点不可用（本次不许调模型）")

        qi.register_query_interpreter("boom", boom)
        result = qi.interpret_query("为什么比亚迪的销量下降了", backend="boom")
        self.assertEqual(result["backend_source"], "rules")
        self.assertTrue(result["fallback"]["used"])
        self.assertIn("RuntimeError", result["fallback"]["reason"])
        self.assertEqual(result["intent"], "CAUSAL", "回落结果必须仍然是可用的规则结论")

    def test_invalid_payload_falls_back_to_rules(self):
        qi.register_query_interpreter("bad", lambda question, context: {"intent": "NOT_AN_INTENT"})
        result = qi.interpret_query("为什么比亚迪的销量下降了", backend="bad")
        self.assertEqual(result["backend_source"], "rules")
        self.assertTrue(result["fallback"]["used"])

    def test_env_selection(self):
        os.environ["QA_QUERY_INTERPRETER"] = "rules"
        self.assertEqual(qi.selected_interpreter(), "rules")
        os.environ["QA_QUERY_INTERPRETER"] = "boom"
        self.assertEqual(qi.selected_interpreter(), "boom")
        # 环境变量选中一个**未注册**的名字 → 仍然回落 rules（无后端也要有规划）
        result = qi.interpret_query("为什么比亚迪的销量下降了")
        self.assertEqual(result["backend_source"], "rules")
        self.assertTrue(result["fallback"]["used"])
        self.assertIn("boom", result["fallback"]["from"])


class NoNetworkGuardTests(unittest.TestCase):
    """硬约束：Query Interpreter 零联网、零模型调用（GPU 与语音机器人共用、已停用）。"""

    def test_no_network_imports_and_no_endpoint_literals(self):
        with open(os.path.join(REPO_ROOT, "qa_query_interpreter.py"), encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertEqual(imported & NETWORK_MODULES, set(),
                         "解释器引入了网络库：%s" % (imported & NETWORK_MODULES))
        for token in ("http://", "https://", "openai", "chat/completions", "_embed_question",
                      "requests.get", "requests.post"):
            self.assertNotIn(token, source, "解释器源码里出现外部调用痕迹：%s" % token)

    def test_only_rule_backend_ships(self):
        """内置只有 rules：LLM 版本必须是调用方自己注册（本轮不提供、不调用）。"""
        self.assertEqual(qi.registered_interpreters(), [])


if __name__ == "__main__":
    unittest.main()

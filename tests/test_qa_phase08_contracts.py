#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 08 · 契约守门（不许放宽任何冻结 schema，不许偷偷联网/调模型）。

专测"边界"，不测业务质量：
  1. 七个**冻结契约**指纹复算必须与 P00-02 一字不差（Phase 08 只新增 Context 契约）；
  2. Phase 01–07 的枚举一个字没动：检索通道 7 值、失败策略 5 值、停止原因 5 值、
     证据状态 5 值、证据图关系 5 值、缺口类型 10 值；
  3. Phase 08 新契约自洽：§4 九段**逐字**、§5 五个乘子**逐字**、§6 四个动作**逐字**、
     十个 ContextItem 种类、选择原因码 ⊆ 冻结枚举、grounding 违规码 7 值；
  4. 新 schema 能拦缺字段与越界枚举（context_item/context_edge/context_selection/context_gap/
     context_pack/grounding_report）；
  5. **零联网 / 零模型守卫**：`qa_context_pack.py` 不 import 任何 HTTP/套接字/模型库、
     源码里没有 http(s) 字面量与模型端点痕迹（GPU 与语音机器人共用、已停用）；
  6. 库表结构本阶段不动（`QA_SCHEMA_VERSION` 仍 v7、无新表/新列）；两个开关默认关；
  7. Context Pack 必须 **JSON 可序列化**（阶段输出会被 record_stage 直接 json.dumps 落库）；
  8. **Context Gap 不许触发检索**：每条 gap 的 `requires_retrieval` 恒 False，
     且模块里不出现检索器/下一跳规划器的引用（§6 + MASTER_RULES 第 13 条）。
"""
import ast
import hashlib
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
import qa_schema  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402
from qa_phase08_fixtures import (  # noqa: E402
    QUESTION, conflict, evidence_item, graph, graph_claim, graph_edge, plan, verification,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FROZEN_FINGERPRINTS = {
    "EVIDENCE_SCHEMA": "370301331c02c738",
    "CLAIM_SCHEMA": "06fcdb02441248b2",
    "CONFLICT_SCHEMA": "aabd3259b07f9a3e",
    "LEVEL1_RESULT_SCHEMA": "7e864764429db111",
    "LEVEL2_RESULT_SCHEMA": "a0484f894e7bc9d6",
    "FINAL_ANSWER_SCHEMA": "4d1efa54ca1cbc1a",
    "QA_EVENT_SCHEMA": "59358bfa88a6c6af",
}
SPEC_SECTIONS = ("system_context", "task_context", "evidence_context", "counter_evidence",
                 "memory_context", "skill_context", "working_memory", "constraints", "budget")
SPEC_FACTORS = ("relevance", "evidence_strength", "task_necessity", "freshness", "diversity")
SPEC_ACTIONS = ("REPACK_CONTEXT", "EXPAND_EVIDENCE_SPAN", "LOAD_COUNTEREVIDENCE", "LOAD_SKILL")
PHASE08_MODULES = ("qa_context_pack.py",)
NETWORK_MODULES = {"requests", "urllib3", "httpx", "socket", "aiohttp", "http.client",
                   "urllib.request", "ftplib", "telnetlib", "paramiko", "openai", "anthropic",
                   "transformers", "torch", "sentence_transformers"}
MODEL_CLIENTS = {"qa_gateway", "qa_level1", "qa_provider_registry", "qa_llm_router", "chat_api",
                 "qa_endpoint_profile", "qa_ragflow_client"}
# 检索/规划类模块：Context Gap 只**建议重组上下文**，不许自己去搜（§6 原话）
RETRIEVAL_MODULES = {"qa_retrieval", "qa_hunters", "qa_hunter_fleet", "qa_gap_analyzer",
                     "qa_ragflow_client", "qa_research"}


def _fingerprint(value) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _source(name: str) -> str:
    with open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
        return handle.read()


def _imports(source: str) -> set:
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def _sample_pack():
    evidence = [
        evidence_item("article:1", verification=verification("SUPPORTED"), doc_type="official_policy"),
        evidence_item("article:2", text="相反的观点认为宽免范围有限。" * 3,
                      verification=verification("REFUTED")),
    ]
    claims = [graph_claim("c1", text="税收优惠政策对内地高净值客户有影响", refs=["article:1"])]
    edges = [graph_edge("c1", "article:1"),
             graph_edge("c1", "article:2", relation="REFUTES", status="REFUTED")]
    return cp.build_context_pack(
        graph=graph(claims=claims, evidence=evidence, edges=edges,
                    conflicts=[conflict("k1", ["c1"], evidence_refs=["article:2"])]),
        plan=plan(), request={"question": QUESTION, "mode": "standard"}, run_id="r1")


class FrozenContractTests(unittest.TestCase):
    def test_seven_schema_fingerprints_unchanged(self):
        for name, expected in FROZEN_FINGERPRINTS.items():
            self.assertEqual(_fingerprint(getattr(qa_contracts, name)), expected,
                             "%s 的冻结指纹变了（P00-02 契约被改动）" % name)

    def test_evidence_schema_still_strict(self):
        self.assertIs(qa_contracts.EVIDENCE_SCHEMA.get("additionalProperties"), False)
        self.assertIs(qa_contracts.FINAL_ANSWER_SCHEMA.get("additionalProperties"), False,
                      "FINAL_ANSWER_SCHEMA 不许放宽（生成端约束只能走校验 + 既有可选字段）")

    def test_phase01_to_07_enums_are_untouched(self):
        self.assertEqual(tuple(contracts.QA_RETRIEVAL_ROUTES),
                         ("keyword", "semantic", "graph", "graph_attribute", "page_context",
                          "policy_exact", "web"))
        self.assertEqual(tuple(contracts.QA_FAILURE_POLICIES),
                         ("FAIL_FAST", "RETRY", "SKIP", "FALLBACK", "DEGRADE"))
        self.assertEqual(tuple(contracts.QA_STOP_REASONS),
                         ("ANSWERABLE", "BUDGET_EXHAUSTED", "MAX_DEPTH", "NO_GAIN",
                          "UNRESOLVABLE_CONTRADICTION"))
        self.assertEqual(len(contracts.EVIDENCE_STATUSES), 5)
        self.assertEqual(len(contracts.EVIDENCE_GRAPH_RELATIONSHIPS), 5)
        self.assertEqual(len(contracts.QA_GAP_TYPES), 10)
        self.assertEqual(len(contracts.CONTEXT_UTILITY_FACTORS), 5)

    def test_schema_version_and_columns_unchanged(self):
        self.assertEqual(qa_schema.QA_SCHEMA_VERSION, "unified-qa-schema-v9",
                         "Phase 08 自身零库表变更；v9 = Phase 09 的八张 memory_* 表 + Phase 10 的两张复验/矛盾表，本条仍钉死字面量")
        blob = " ".join(qa_schema.QA_TABLE_DDL)
        for marker in ("qa_context", "qa_context_pack", "qa_context_item"):
            self.assertNotIn(marker, blob, "不许为上下文包新建表（回执免费持久化在既有表）")


class SpecVerbatimTests(unittest.TestCase):
    def test_nine_sections_are_verbatim_from_the_spec(self):
        self.assertEqual(tuple(contracts.CONTEXT_SECTIONS), SPEC_SECTIONS)
        self.assertEqual(len(contracts.CONTEXT_SECTIONS), 9)
        self.assertEqual(tuple(cp._SECTION_ORDER), SPEC_SECTIONS)

    def test_five_utility_factors_are_verbatim_from_the_spec(self):
        self.assertEqual(tuple(contracts.CONTEXT_UTILITY_FACTORS), SPEC_FACTORS)
        self.assertAlmostEqual(sum(contracts.CONTEXT_UTILITY_WEIGHTS.values()), 1.0, places=6)
        for name in SPEC_FACTORS:
            self.assertIn(name, contracts.CONTEXT_UTILITY_WEIGHTS)

    def test_four_context_gap_actions_are_verbatim_from_the_spec(self):
        self.assertEqual(tuple(contracts.CONTEXT_GAP_ACTIONS), SPEC_ACTIONS)
        self.assertEqual(len(contracts.CONTEXT_GAP_TYPES), 6)

    def test_item_kinds_and_decision_reasons_are_closed_sets(self):
        self.assertEqual(len(contracts.CONTEXT_ITEM_KINDS), 10)
        self.assertEqual(len(set(contracts.CONTEXT_ITEM_KINDS)), 10)
        self.assertEqual(set(contracts.CONTEXT_DECISIONS), {"included", "excluded"})
        self.assertEqual(len(contracts.CONTEXT_GROUNDING_VIOLATIONS), 7)
        self.assertTrue(set(contracts.CONTEXT_SELECTION_REASONS) >=
                        {"MANDATORY_SECTION", "COUNTER_EVIDENCE_RESERVED", "TOP_UTILITY",
                         "DIVERSITY_BONUS", "OVER_TOKEN_BUDGET", "DUPLICATE_IDENTITY",
                         "LOW_UTILITY", "NO_GROUNDING_SPAN"})


class SchemaGateTests(unittest.TestCase):
    def test_new_schemas_are_registered_and_reject_bad_payloads(self):
        for name in ("context_item", "context_edge", "context_selection", "context_gap",
                     "context_pack", "grounding_report"):
            ok, _note = validate(name, {})
            self.assertFalse(ok, "%s 必须拦缺字段" % name)

    def test_context_pack_of_a_real_build_passes_its_own_contract(self):
        pack = _sample_pack()
        ok, note = validate("context_pack", pack)
        self.assertTrue(ok, note)
        for item in pack["items"]:
            ok, note = validate("context_item", item)
            self.assertTrue(ok, "%s：%s" % (item.get("item_id"), note))
        for gap in pack["context_gaps"]:
            ok, note = validate("context_gap", gap)
            self.assertTrue(ok, note)
        for row in pack["selection_trace"]:
            ok, note = validate("context_selection", row)
            self.assertTrue(ok, note)

    def test_out_of_enum_values_are_rejected(self):
        pack = _sample_pack()
        item = dict(pack["items"][0])
        item["section"] = "not_a_section"
        ok, _note = validate("context_item", item)
        self.assertFalse(ok, "段落只吃 §4 的九段")
        gap = dict(pack["context_gaps"][0]) if pack["context_gaps"] else {
            "gap_id": "CG1", "context_gap_type": "OMITTED_EVIDENCE",
            "action": "REPACK_CONTEXT", "requires_retrieval": False}
        gap["action"] = "SEARCH_MORE"
        ok, _note = validate("context_gap", gap)
        self.assertFalse(ok, "Context Gap 动作只吃 §6 的四个值（SEARCH_MORE 不存在）")

    def test_pack_is_json_serializable(self):
        body = json.dumps(_sample_pack(), ensure_ascii=False, allow_nan=False)
        self.assertIn(contracts.CONTEXT_PACK_VERSION, body)
        self.assertGreater(len(body), 2000)

    def test_describe_mentions_the_phase08_contract(self):
        text = contracts.describe()
        for token in ("qa-gap-analyzer-v1", "qa-evidence-graph-v1", "qa-execution-graph-v1",
                      "qa-hunter-v1", "Context 段 9", "Context Gap 动作 4"):
            self.assertIn(token, text)


class NoModelNoNetworkGuardTests(unittest.TestCase):
    def test_no_network_imports(self):
        for name in PHASE08_MODULES:
            imported = _imports(_source(name))
            self.assertEqual(imported & NETWORK_MODULES, set(),
                             "%s 引入了网络/模型库：%s" % (name, imported & NETWORK_MODULES))
            self.assertEqual(imported & MODEL_CLIENTS, set(),
                             "%s 引入了模型客户端：%s" % (name, imported & MODEL_CLIENTS))

    def test_context_gap_cannot_trigger_retrieval(self):
        """§6 + MASTER_RULES 第 13 条：Context Gap 默认不得触发昂贵新检索。"""
        for name in PHASE08_MODULES:
            imported = _imports(_source(name))
            self.assertEqual(imported & RETRIEVAL_MODULES, set(),
                             "%s 引用了检索/规划模块：%s" % (name, imported & RETRIEVAL_MODULES))
        pack = _sample_pack()
        self.assertTrue(pack["context_gaps"], "样例必须真的产出 Context Gap，否则这条守卫是空转")
        for gap in pack["context_gaps"]:
            self.assertIs(gap["requires_retrieval"], False)
        self.assertEqual(pack["stats"]["retrieval_requested"], 0)

    def test_no_endpoint_literals(self):
        for name in PHASE08_MODULES:
            source = _source(name)
            for token in ("http://", "https://", "/v1/chat", "/v1/embeddings",
                          "embedding_client", "_embed_question", "openai", "api_key"):
                self.assertNotIn(token, source, "%s 里出现端点/密钥痕迹：%s" % (name, token))

    def test_switches_default_off(self):
        saved = os.environ.pop("QA_CONTEXT_PACK", None)
        saved_gate = os.environ.pop("QA_GROUNDING_GATE", None)
        try:
            self.assertFalse(cp.context_pack_enabled(), "上下文包默认关（回滚口径）")
            self.assertFalse(cp.grounding_gate_enabled(), "grounding 闸门默认关")
            self.assertEqual(cp.total_token_budget(), 6000)
            self.assertEqual(cp.output_reserve_tokens(), 900)
            self.assertAlmostEqual(cp.counter_reserve_ratio(), 0.20, places=6)
            self.assertEqual(cp.span_min_chars(), 40)
        finally:
            if saved is not None:
                os.environ["QA_CONTEXT_PACK"] = saved
            if saved_gate is not None:
                os.environ["QA_GROUNDING_GATE"] = saved_gate

    def test_knobs_are_clamped(self):
        os.environ["QA_CONTEXT_TOKEN_BUDGET"] = "999999999"
        os.environ["QA_CONTEXT_COUNTER_RESERVE"] = "5"
        os.environ["QA_CONTEXT_OUTPUT_RESERVE"] = "-10"
        try:
            self.assertEqual(cp.total_token_budget(), 200000)
            self.assertAlmostEqual(cp.counter_reserve_ratio(), 0.9, places=6)
            self.assertEqual(cp.output_reserve_tokens(), 0)
        finally:
            for name in ("QA_CONTEXT_TOKEN_BUDGET", "QA_CONTEXT_COUNTER_RESERVE",
                         "QA_CONTEXT_OUTPUT_RESERVE"):
                os.environ.pop(name, None)


class EstimatorContractTests(unittest.TestCase):
    def test_estimator_is_deterministic_and_monotonic(self):
        self.assertEqual(cp.estimate_tokens(""), 0)
        self.assertEqual(cp.estimate_tokens(None), 0)
        first = cp.estimate_tokens(QUESTION)
        self.assertEqual(first, cp.estimate_tokens(QUESTION))
        self.assertGreater(first, 0)
        self.assertLessEqual(cp.estimate_tokens("家族"), cp.estimate_tokens("家族办公室"))
        self.assertLess(cp.estimate_tokens("policy"), cp.estimate_tokens("policy documentation"))

    def test_estimator_has_a_documented_relative_unit(self):
        self.assertIn("相对预算单位", cp.ESTIMATOR_NOTE)
        self.assertEqual(cp.ESTIMATOR_VERSION, "qa-token-estimate-v1")
        # 中文一个字 ≈ 1 token、ASCII 4 字符 ≈ 1 token 的口径必须能从数字上看出来
        self.assertEqual(cp.estimate_tokens("中文四个"), 4)
        self.assertEqual(cp.estimate_tokens("abcd"), 1)
        self.assertEqual(cp.estimate_tokens("abcdefgh"), 2)


if __name__ == "__main__":
    unittest.main()

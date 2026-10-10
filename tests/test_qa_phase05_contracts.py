#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 05 · 契约守门（不许放宽任何冻结 schema，不许偷偷联网）。

专测"边界"，不测规划质量：
  1. 七个**冻结契约**指纹复算必须与 P00-02 一字不差（Phase 05 只新增规划/执行图契约）；
  2. Phase 01 冻结的枚举一个字没动：检索通道 7 值、失败策略 5 值、停止原因 5 值，
     且 `EXECUTION_NODE_SCHEMA` 的 required 仍只有 `node_id`（老调用方零改动）；
  3. 新 schema 能拦缺字段与越界枚举（execution_graph / sub_question / plan_claim /
     evidence_requirement / query_interpretation / signal）；
  4. **零联网守卫**：`qa_query_interpreter.py` / `qa_execution_graph.py` 不 import 任何
     HTTP/套接字库、源码里没有 http(s) 字面量与模型调用痕迹；
  5. 库表结构**本阶段不动**：`QA_SCHEMA_VERSION` 仍是 v7、无新表/新列；
  6. 两个新开关默认关（不做隐式行为变更）。
"""
import ast
import hashlib
import json
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_contracts  # noqa: E402
import qa_execution_graph as graph_module  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_query_interpreter as interpreter  # noqa: E402
import qa_schema  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# P00-02 冻结的七个契约指纹（与 baseline/qa-baseline-inventory.json 同口径）
FROZEN_FINGERPRINTS = {
    "EVIDENCE_SCHEMA": "370301331c02c738",
    "CLAIM_SCHEMA": "06fcdb02441248b2",
    "CONFLICT_SCHEMA": "aabd3259b07f9a3e",
    "LEVEL1_RESULT_SCHEMA": "7e864764429db111",
    "LEVEL2_RESULT_SCHEMA": "a0484f894e7bc9d6",
    "FINAL_ANSWER_SCHEMA": "4d1efa54ca1cbc1a",
    "QA_EVENT_SCHEMA": "59358bfa88a6c6af",
}
FROZEN_ROUTES = ("keyword", "semantic", "graph", "graph_attribute", "page_context",
                 "policy_exact", "web")
FROZEN_FAILURE_POLICIES = ("FAIL_FAST", "RETRY", "SKIP", "FALLBACK", "DEGRADE")
FROZEN_STOP_REASONS = ("ANSWERABLE", "BUDGET_EXHAUSTED", "MAX_DEPTH", "NO_GAIN",
                       "UNRESOLVABLE_CONTRADICTION")
PHASE05_MODULES = ("qa_query_interpreter.py", "qa_execution_graph.py")
NETWORK_MODULES = {"requests", "urllib3", "httpx", "socket", "aiohttp", "http.client",
                   "urllib.request", "ftplib", "telnetlib", "paramiko", "openai", "anthropic"}


def _fingerprint(value) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _source(name: str) -> str:
    with open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
        return handle.read()


class FrozenContractTests(unittest.TestCase):
    def test_seven_schema_fingerprints_unchanged(self):
        for name, expected in FROZEN_FINGERPRINTS.items():
            self.assertEqual(_fingerprint(getattr(qa_contracts, name)), expected,
                             "%s 的冻结指纹变了（P00-02 契约被改动）" % name)

    def test_evidence_schema_still_strict(self):
        self.assertIs(qa_contracts.EVIDENCE_SCHEMA.get("additionalProperties"), False,
                      "EVIDENCE_SCHEMA 不许放宽 additionalProperties")

    def test_phase01_enums_are_untouched(self):
        self.assertEqual(list(contracts.QA_RETRIEVAL_ROUTES), list(FROZEN_ROUTES),
                         "阶段 05 不许扩/改检索通道枚举")
        self.assertEqual(list(contracts.QA_FAILURE_POLICIES), list(FROZEN_FAILURE_POLICIES))
        self.assertEqual(list(contracts.QA_STOP_REASONS), list(FROZEN_STOP_REASONS))
        self.assertEqual(list(contracts.SEARCH_TRACE_SCHEMA["properties"]["route"]["enum"]),
                         list(FROZEN_ROUTES) + [""])

    def test_execution_node_required_is_still_only_node_id(self):
        self.assertEqual(contracts.EXECUTION_NODE_SCHEMA["required"], ["node_id"],
                         "Phase 01 的调用方零改动：required 不许加字段")
        ok, _note = validate("execution_node", {"node_id": "plan"})
        self.assertTrue(ok)

    def test_schema_version_and_columns_unchanged(self):
        self.assertEqual(qa_schema.QA_SCHEMA_VERSION, "unified-qa-schema-v7",
                         "阶段 05 没有库表变更，版本号不许动")
        self.assertEqual(len(qa_schema.QA_ADDED_COLUMNS_V6), 15,
                         "ADD COLUMN 清单被改动了（阶段 05 声明零迁移）")
        blob = " ".join(qa_schema.QA_TABLE_DDL)
        for marker in ("qa_execution_graph", "qa_research_plan", "qa_plan_"):
            self.assertNotIn(marker, blob, "不许为执行图新建表")


class NewSchemaTests(unittest.TestCase):
    def test_execution_node_rejects_bad_enums(self):
        ok, note = validate("execution_node", {"node_id": "n", "failure_policy": "IGNORE"})
        self.assertFalse(ok)
        self.assertIn("failure_policy", note)
        ok, note = validate("execution_node", {"node_id": "n", "model_tier": "huge"})
        self.assertFalse(ok)
        ok, note = validate("execution_node", {"node_id": "n", "status": "whatever"})
        self.assertFalse(ok)

    def test_execution_node_accepts_the_full_contract(self):
        payload = {
            "node_id": "deep.verify", "node_kind": "verify", "purpose": "核验",
            "input_schema": {"name": "qa.evidence_object", "fields": ["evidence_ref"]},
            "output_schema": {"name": "qa.evidence_verification", "fields": ["verdict"]},
            "timeout": 3.0, "retry": 1, "model_tier": "rule",
            "allowed_tools": ["qa_verifier"], "validation": ["evidence_verification"],
            "failure_policy": "DEGRADE", "stop_reason": "ANSWERABLE",
        }
        ok, note = validate("execution_node", payload)
        self.assertTrue(ok, note)
        broken = dict(payload, input_schema={"fields": []})
        ok, note = validate("execution_node", broken)
        self.assertFalse(ok, "嵌套对象缺 required 必须被拦")
        self.assertIn("input_schema.name", note)

    def test_execution_graph_schema(self):
        graph = graph_module.build_execution_graph("为什么比亚迪的销量下降了", plan={},
                                                   mode="standard")
        ok, note = validate("execution_graph", graph)
        self.assertTrue(ok, note)
        ok, note = validate("execution_graph", dict(graph, path="ultra"))
        self.assertFalse(ok)
        self.assertIn("path", note)
        ok, note = validate("execution_graph", dict(graph, stop_reason="WHATEVER"))
        self.assertFalse(ok)
        ok, note = validate("execution_graph", {"path": "fast"})
        self.assertFalse(ok, "缺 required 必须被拦")

    def test_sub_question_and_claim_schemas(self):
        plan = graph_module.build_research_plan(
            "香港家族办公室税收优惠政策对内地高净值客户有什么影响？", plan={})
        for item in plan["sub_questions"]:
            ok, note = validate("sub_question", item)
            self.assertTrue(ok, note)
        for claim in plan["claims"]:
            ok, note = validate("plan_claim", claim)
            self.assertTrue(ok, note)
        ok, note = validate("plan_claim", {"claim_id": "c", "statement": "s", "role": "nope"})
        self.assertFalse(ok)
        self.assertIn("role", note)
        ok, note = validate("evidence_requirement", {"requirement_id": "er"})
        self.assertFalse(ok)
        self.assertIn("evidence_type", note)

    def test_query_interpretation_schema(self):
        payload = interpreter.interpret_query("为什么比亚迪的销量下降了")
        ok, note = validate("query_interpretation", payload)
        self.assertTrue(ok, note)
        ok, note = validate("query_interpretation", dict(payload, intent="NOPE"))
        self.assertFalse(ok)
        ok, note = validate("query_interpretation", dict(payload, complexity="ultra"))
        self.assertFalse(ok)

    def test_new_phase05_enums_are_well_formed(self):
        self.assertEqual(len(contracts.QUERY_INTENTS), 9)
        self.assertEqual(len(set(contracts.QUERY_INTENTS)), 9)
        self.assertEqual(tuple(contracts.QA_PATHS), ("fast", "standard", "deep"))
        self.assertEqual(tuple(contracts.QUERY_COMPLEXITIES), ("simple", "standard", "deep"))
        self.assertIn("deferred", contracts.NODE_STATUSES)
        self.assertIn("strong", contracts.MODEL_TIERS)
        for kind in contracts.EXECUTION_NODE_KINDS:
            self.assertIsInstance(kind, str)

    def test_describe_mentions_both_contracts(self):
        text = contracts.describe()
        self.assertIn("qa-hunter-v1", text, "Phase 04 的断言不能被打破")
        self.assertIn(graph_module.EXECUTION_GRAPH_VERSION, text)
        self.assertIn(interpreter.__name__.split(".")[-1], "qa_query_interpreter")


class NoNetworkGuardTests(unittest.TestCase):
    def test_no_network_imports(self):
        for name in PHASE05_MODULES:
            tree = ast.parse(_source(name))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            self.assertEqual(imported & NETWORK_MODULES, set(),
                             "%s 引入了网络库：%s" % (name, imported & NETWORK_MODULES))

    def test_no_endpoint_literals(self):
        for name in PHASE05_MODULES:
            source = _source(name)
            for token in ("http://", "https://", "/v1/chat", "/v1/embeddings",
                          "embedding_client", "_embed_question", "openai"):
                self.assertNotIn(token, source, "%s 里出现端点调用痕迹：%s" % (name, token))

    def test_no_model_client_in_the_planning_path(self):
        """规划链路只允许规则/注入点：不许出现 provider/gateway 客户端调用。"""
        source = _source("qa_execution_graph.py")
        for token in ("QaGateway", "provider_registry", "requests.Session", "chat_api"):
            self.assertNotIn(token, source, "规划路径里出现模型客户端：%s" % token)


class FlagGuardTests(unittest.TestCase):
    def test_both_switches_default_off(self):
        saved = {name: os.environ.pop(name, None)
                 for name in ("QA_EXECUTION_GRAPH", "QA_EXECUTION_GRAPH_NODE_RUNS")}
        try:
            self.assertFalse(graph_module.graph_enabled(), "执行图开关必须默认关")
            self.assertFalse(graph_module.node_runs_enabled(), "节点落库开关必须默认关")
            os.environ["QA_EXECUTION_GRAPH"] = "1"
            self.assertTrue(graph_module.graph_enabled())
            os.environ["QA_EXECUTION_GRAPH"] = "0"
            self.assertFalse(graph_module.graph_enabled())
        finally:
            os.environ.pop("QA_EXECUTION_GRAPH", None)
            os.environ.pop("QA_EXECUTION_GRAPH_NODE_RUNS", None)
            for name, value in saved.items():
                if value is not None:
                    os.environ[name] = value

    def test_path_budgets_are_bounded_and_positive(self):
        for path in contracts.QA_PATHS:
            value = graph_module.path_budget(path)["total_seconds"]
            self.assertGreater(value, 0)
            self.assertLess(value, 3600)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 04 · 契约与守门（不许放宽任何冻结 schema，不许偷偷联网）。

专测"边界"，不测检索质量：
  1. 七个**冻结契约**指纹复算必须与 P00-02 一字不差，`EVIDENCE_SCHEMA` 仍然
     `additionalProperties: False`（Phase 04 只新增了 Hunter 契约，没碰它们）；
  2. **检索通道枚举一个字没动**：`QA_RETRIEVAL_ROUTES` 仍是既有 7 个取值，
     Hunter 身份走新命名空间 `QA_HUNTER_IDS`（`hunter_id` 与 `route` 分离）；
  3. Hunter / 舰队两个新 schema 能拦缺字段与越界枚举；
  4. **零联网守卫**（硬约束：GPU 推理机与语音机器人共用、已停用）：
     `qa_hunters.py` / `qa_hunter_fleet.py` 不 import 任何 HTTP/套接字库、
     源码里没有 http(s) 字面量、语义通道不出现 embedding 客户端调用点；
  5. 库表结构**本阶段不动**：`QA_SCHEMA_VERSION` 仍是 v7、没有新表/新列；
  6. 舰队开关默认关（不做隐式行为变更）。
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
import qa_graph_contracts as contracts  # noqa: E402
import qa_hunter_fleet as fleet_module  # noqa: E402
import qa_hunters as hunters  # noqa: E402
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

# Phase 01 冻结的检索通道（阶段 04 不许扩这个枚举）
FROZEN_ROUTES = ("keyword", "semantic", "graph", "graph_attribute", "page_context",
                 "policy_exact", "web")

PHASE04_MODULES = ("qa_hunters.py", "qa_hunter_fleet.py")
NETWORK_MODULES = {"requests", "urllib3", "httpx", "socket", "aiohttp", "http.client",
                   "urllib.request", "ftplib", "telnetlib", "paramiko"}


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

    def test_retrieval_routes_are_untouched(self):
        self.assertEqual(list(contracts.QA_RETRIEVAL_ROUTES), list(FROZEN_ROUTES),
                         "阶段 04 不许扩/改检索通道枚举（Hunter 身份走 QA_HUNTER_IDS）")
        self.assertEqual(
            list(contracts.SEARCH_TRACE_SCHEMA["properties"]["route"]["enum"]),
            list(FROZEN_ROUTES) + [""], "SearchTrace 的 route 取值域被改动了")


class HunterContractTests(unittest.TestCase):
    def _result(self, **overrides):
        value = hunters.hunter_outcome("bm25", status="ok", evidence=[{"evidence_ref": "article:1"}],
                                       latency_ms=12, stats={"x": 1})
        value.update(overrides)
        return value

    def test_hunter_ids_and_routes_are_consistent(self):
        self.assertEqual(tuple(contracts.QA_HUNTER_IDS),
                         ("bm25", "semantic", "graph", "structured", "query_expansion"))
        for hunter_id in contracts.QA_HUNTER_IDS:
            route = contracts.QA_HUNTER_ROUTE_BY_ID[hunter_id]
            self.assertIn(route, list(contracts.QA_RETRIEVAL_ROUTES) + [""],
                          "%s 的 route 不在既有通道枚举里" % hunter_id)

    def test_schema_accepts_valid_and_rejects_bad_values(self):
        ok, note = validate("hunter_result", self._result())
        self.assertTrue(ok, note)
        ok, note = validate("hunter_result", self._result(hunter_id="not_a_hunter"))
        self.assertFalse(ok)
        self.assertIn("hunter_id", note)
        ok, note = validate("hunter_result", self._result(status="weird"))
        self.assertFalse(ok)
        self.assertIn("status", note)
        ok, note = validate("hunter_result", self._result(route="not_a_route"))
        self.assertFalse(ok)
        ok, note = validate("hunter_result", {"hunter_id": "bm25"})
        self.assertFalse(ok, "缺 required 字段必须被拦")

    def test_status_derives_ok_and_degraded(self):
        self.assertTrue(hunters.hunter_outcome("bm25", status="ok")["ok"])
        self.assertTrue(hunters.hunter_outcome("bm25", status="empty")["ok"])
        for status in ("degraded", "timeout", "error", "skipped"):
            outcome = hunters.hunter_outcome("bm25", status=status)
            self.assertFalse(outcome["ok"], status)
            self.assertTrue(outcome["degraded"], status)

    def test_fleet_result_schema(self):
        payload = {"contract_version": contracts.HUNTER_CONTRACT_VERSION,
                   "hunters": [self._result()], "evidence": [], "stats": {},
                   "partial": True, "stop_reason": "BUDGET_EXHAUSTED"}
        ok, note = validate("hunter_fleet_result", payload)
        self.assertTrue(ok, note)
        ok, note = validate("hunter_fleet_result", dict(payload, stop_reason="NOT_A_REASON"))
        self.assertFalse(ok)
        self.assertIn("stop_reason", note)
        ok, note = validate("hunter_fleet_result", {"hunters": [], "evidence": [], "stats": {}})
        self.assertFalse(ok, "缺 contract_version 必须被拦")

    def test_failure_policy_enum_is_reused(self):
        for policy in (hunters.BM25Hunter.failure_policy, hunters.SemanticHunter.failure_policy,
                       hunters.GraphHunter.failure_policy, hunters.StructuredHunter.failure_policy,
                       hunters.QueryExpansionHunter.failure_policy):
            self.assertIn(policy, contracts.QA_FAILURE_POLICIES)
        self.assertEqual(hunters.SemanticHunter.fallback_hunter, "bm25",
                         "§28：Vector/语义通道超时要回退到 BM25")


class NoNetworkGuardTests(unittest.TestCase):
    """硬约束守卫：本阶段代码零联网（不得调用任何模型/嵌入端点）。"""

    def test_no_network_imports(self):
        for name in PHASE04_MODULES:
            tree = ast.parse(_source(name))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            overlap = imported & NETWORK_MODULES
            self.assertEqual(overlap, set(), "%s 引入了网络库：%s" % (name, overlap))

    def test_no_http_literals(self):
        for name in PHASE04_MODULES:
            source = _source(name)
            for token in ("http://", "https://", "/v1/embeddings", "embedding_client",
                          "_embed_question", "_semantic_top_articles"):
                self.assertNotIn(token, source, "%s 里出现端点调用痕迹：%s" % (name, token))

    def test_semantic_hunter_reads_only_existing_vectors(self):
        """语义通道只读既有向量表；不 import 任何 embedding 客户端。"""
        source = _source("qa_hunters.py")
        self.assertIn("intel_article_embeddings", source)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn("embedding_client", node.module)

    def test_vector_loader_returns_none_instead_of_calling_out(self):
        """行为级：没有向量时返回 (None, None)，绝不尝试外部编码。"""
        ids, matrix = hunters.load_article_vectors(_EmptyDatabase())
        self.assertIsNone(matrix)
        self.assertIsNone(ids)


class _EmptyDatabase:
    backend = "sqlite"

    def __init__(self):
        import sqlite3

        self.lock = __import__("threading").RLock()
        self.connection = sqlite3.connect(":memory:")

    def _ensure_connection(self):
        return None


class SchemaAndFlagGuardTests(unittest.TestCase):
    def test_schema_version_unchanged(self):
        self.assertEqual(qa_schema.QA_SCHEMA_VERSION, "unified-qa-schema-v8",
                         "阶段 04 自身零库表变更；v8 是 Phase 09 的变更（八张 memory_* 表），本条仍钉死字面量")
        self.assertEqual(len(qa_schema.QA_ADDED_COLUMNS_V6), 15,
                         "ADD COLUMN 清单被改动了（阶段 04 声明零迁移）")

    def test_no_new_table_for_hunters(self):
        blob = " ".join(qa_schema.QA_TABLE_DDL)
        for marker in ("qa_hunter", "hunter_fleet", "qa_hunters"):
            self.assertNotIn(marker, blob, "不许为舰队新建表")

    def test_fleet_flag_defaults_to_off(self):
        saved = os.environ.pop("QA_HUNTER_FLEET", None)
        try:
            self.assertFalse(fleet_module.fleet_enabled(), "舰队开关必须默认关（零隐式行为变更）")
            os.environ["QA_HUNTER_FLEET"] = "1"
            self.assertTrue(fleet_module.fleet_enabled())
            os.environ["QA_HUNTER_FLEET"] = "0"
            self.assertFalse(fleet_module.fleet_enabled())
        finally:
            os.environ.pop("QA_HUNTER_FLEET", None)
            if saved is not None:
                os.environ["QA_HUNTER_FLEET"] = saved

    def test_fleet_bounds_are_bounded(self):
        """并发/超时/预算都必须是"有界"的（不许出现 0 或无限）。"""
        self.assertGreaterEqual(fleet_module.max_workers(), 1)
        self.assertLessEqual(fleet_module.max_workers(), 32)
        self.assertGreater(fleet_module.hunter_timeout_seconds(), 0)
        self.assertGreater(fleet_module.total_budget_seconds(), 0)
        self.assertGreaterEqual(fleet_module.retries(), 0)

    def test_hunter_switches_default_on(self):
        for hunter_id in contracts.QA_HUNTER_IDS:
            self.assertTrue(hunters.hunter_enabled(hunter_id),
                            "单个 Hunter 默认必须开（关掉要显式设 QA_HUNTER_*_ENABLED=0）")

    def test_describe_mentions_the_phase04_contract(self):
        self.assertIn("qa-hunter-v1", contracts.describe())


if __name__ == "__main__":
    unittest.main()

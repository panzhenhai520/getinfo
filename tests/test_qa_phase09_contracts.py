#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 09 · 冻结契约 / 默认开关 / 依赖守卫用例。

钉住：
  1. **七个冻结 schema 指纹一字不变**（P00-02 的账；本阶段一个字段都没往冻结契约里加）；
  2. 冻结枚举的取值域不动（7 通道 / 5 失败策略 / 5 停止原因 / 5 证据状态）；
  3. 库表版本 v8 = 八张 memory_* 新表、**零 ADD COLUMN**；Phase 08 的九段/十种 ContextItem 不动，
     `CONTEXT_ITEM_SOURCES` 只做**追加**（`memory_graph`）；
  4. 新开关默认**关**（`QA_MEMORY_GRAPH`）、标定值写在常量里；
  5. 依赖与端点守卫（AST 级）：`qa_memory.py` 不 import 任何网络库、源码零 http(s) 字面量、
     不调用嵌入/模型客户端；`qa_context_pack.py` 不在模块级 import `qa_memory`（避免循环依赖）。
"""
import ast
import hashlib
import json
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_contracts  # noqa: E402
import qa_context_pack as context_pack  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_memory as memory  # noqa: E402
import qa_schema  # noqa: E402

FROZEN_FINGERPRINTS = {
    "EVIDENCE_SCHEMA": "370301331c02c738",
    "CLAIM_SCHEMA": "06fcdb02441248b2",
    "CONFLICT_SCHEMA": "aabd3259b07f9a3e",
    "LEVEL1_RESULT_SCHEMA": "7e864764429db111",
    "LEVEL2_RESULT_SCHEMA": "a0484f894e7bc9d6",
    "FINAL_ANSWER_SCHEMA": "4d1efa54ca1cbc1a",
    "QA_EVENT_SCHEMA": "59358bfa88a6c6af",
}

NETWORK_MODULES = ("requests", "urllib3", "httpx", "aiohttp", "socket", "http.client",
                   "openai", "anthropic", "zhipuai", "dashscope")


def _fingerprint(value) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _imports(path: str) -> set:
    tree = ast.parse(open(path, encoding="utf-8").read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class FrozenContractTests(unittest.TestCase):
    def test_seven_schema_fingerprints_unchanged(self):
        for name, expected in FROZEN_FINGERPRINTS.items():
            self.assertEqual(_fingerprint(getattr(qa_contracts, name)), expected,
                             "%s 的冻结指纹变了（P00-02 契约被改动）" % name)

    def test_evidence_schema_is_still_closed(self):
        self.assertFalse(qa_contracts.EVIDENCE_SCHEMA.get("additionalProperties", True))
        self.assertFalse(qa_contracts.FINAL_ANSWER_SCHEMA.get("additionalProperties", True))

    def test_frozen_enums_untouched(self):
        self.assertEqual(len(contracts.QA_RETRIEVAL_ROUTES), 7)
        self.assertEqual(len(contracts.QA_FAILURE_POLICIES), 5)
        self.assertEqual(len(contracts.QA_STOP_REASONS), 5)
        self.assertEqual(len(contracts.EVIDENCE_STATUSES), 5)

    def test_context_contracts_are_untouched_and_sources_are_additive(self):
        self.assertEqual(len(contracts.CONTEXT_SECTIONS), 9)
        self.assertEqual(len(contracts.CONTEXT_ITEM_KINDS), 10)
        # Phase 08 的 7 个取值必须**逐字且逐位**不变（原来只断言"这 7 个名字出现在列表里"，
        # 顺序与位置都没钉住）。Phase 11 追加 `skill_registry` 之后这条断言**更强**而不是更弱：
        # 前 7 位就是 Phase 08 的原值，第 8 位就是 Phase 09 追加的 `memory_graph`。
        phase08_sources = ("evidence_graph", "evidence", "plan", "verification", "gap_analyzer",
                           "request", "config")
        self.assertEqual(contracts.CONTEXT_ITEM_SOURCES[:7], phase08_sources)
        self.assertEqual(contracts.CONTEXT_ITEM_SOURCES[7], "memory_graph")
        # 追加语义：列表只变长，不出现重复取值
        self.assertGreaterEqual(len(contracts.CONTEXT_ITEM_SOURCES), 8)
        self.assertEqual(len(set(contracts.CONTEXT_ITEM_SOURCES)),
                         len(contracts.CONTEXT_ITEM_SOURCES))

    def test_memory_contract_version_constants(self):
        self.assertEqual(contracts.MEMORY_GRAPH_VERSION, "qa-memory-graph-v1")
        for value in (contracts.MEMORY_WRITE_GATE_VERSION, contracts.MEMORY_RECALL_VERSION,
                      contracts.MEMORY_LIFECYCLE_VERSION, contracts.MEMORY_PROVENANCE_VERSION,
                      contracts.MEMORY_RECALL_HINT_VERSION):
            self.assertTrue(value.startswith("qa-memory-"))
        self.assertIn("记忆类型", contracts.describe())

    def test_new_schemas_validate(self):
        cases = {
            "memory_item": {"memory_id": "M1", "memory_type": "VERIFIED_CLAIM",
                            "canonical_content": "内容", "confidence": 0.5,
                            "freshness_class": "LONG", "status": "ACTIVE", "scope": "SESSION"},
            "memory_write_decision": {"decision_id": "D1", "decision": "PERSIST",
                                      "reason": "UTILITY_ABOVE_FLOOR", "gate_version": "v1"},
            "memory_recall_hit": {"memory_id": "M1", "memory_type": "VERIFIED_CLAIM",
                                  "status": "ACTIVE", "score": 0.3, "channels": ["lexical"],
                                  "hint": True, "requires_revalidation": True,
                                  "verified_evidence": False},
            "memory_recall_receipt": {"recall_version": "v1", "mode": "evidence",
                                      "channels": ["lexical"], "hits": [], "counts": {}},
            "memory_lifecycle_report": {"lifecycle_version": "v1", "checked": 0, "transitions": []},
            "memory_provenance_report": {"provenance_version": "v1", "checked": 0, "traceable": 0,
                                         "links": {}},
        }
        for name, payload in cases.items():
            ok, note = contracts.validate(name, payload)
            self.assertTrue(ok, "%s: %s" % (name, note))

    def test_schema_version_is_v9_with_zero_added_columns(self):
        # Phase 09 钉死的是"八张表 + 零 ADD COLUMN"；Phase 10（D-037）在 v9 里补了
        # §12 的最后两张表（memory_validation / memory_contradiction），因此本条的
        # 字面量随跨阶段契约变更为 v9，但**强度不变**：仍是等值断言 + 零 ADD COLUMN。
        self.assertEqual(qa_schema.QA_SCHEMA_VERSION, "unified-qa-schema-v9")
        self.assertEqual(qa_schema.QA_ADDED_COLUMNS_V8, ())
        self.assertEqual(qa_schema.QA_ADDED_COLUMNS_V9, (), "Phase 10 也只许新建表")
        blob = " ".join(qa_schema.QA_TABLE_DDL)
        for table in ("memory_item", "memory_version", "memory_entity_link",
                      "memory_evidence_link", "memory_relation", "memory_recall_log",
                      "memory_write_decision", "memory_usage_stat"):
            self.assertIn("CREATE TABLE IF NOT EXISTS %s " % table, blob)
        # Phase 10 的两张表（D-033 明确留给 P10 的账）现在必须存在
        for added in ("memory_validation", "memory_contradiction"):
            self.assertIn("CREATE TABLE IF NOT EXISTS %s " % added, blob)
        # Phase 12 的两张表仍然不许提前建（本阶段边界的机器校验）
        for deferred in ("skill_performance_memory", "source_reliability_memory"):
            self.assertNotIn(deferred, blob, "%s 属后续 Phase，本阶段不许建" % deferred)


class DefaultFlagTests(unittest.TestCase):
    def setUp(self):
        self.saved = {name: os.environ.pop(name, None) for name in (
            "QA_MEMORY_GRAPH", "QA_MEMORY_WRITE_MIN_UTILITY", "QA_MEMORY_RECALL_LIMIT",
            "QA_MEMORY_RECALL_MIN_SCORE", "QA_MEMORY_RECALL_COUNTS_REUSE")}

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_switches_default_off(self):
        self.assertFalse(memory.memory_graph_enabled())
        os.environ["QA_MEMORY_GRAPH"] = "1"
        self.assertTrue(memory.memory_graph_enabled())
        os.environ["QA_MEMORY_GRAPH"] = "0"
        self.assertFalse(memory.memory_graph_enabled())

    def test_calibrated_defaults(self):
        self.assertEqual(memory.write_min_utility(), memory.DEFAULT_WRITE_MIN_UTILITY)
        self.assertEqual(memory.recall_limit(), memory.DEFAULT_RECALL_LIMIT)
        self.assertEqual(memory.recall_min_score(), memory.DEFAULT_RECALL_MIN_SCORE)
        self.assertFalse(memory.recall_counts_reuse(), "召回不等于复用（默认不刷 reuse_count）")

    def test_env_overrides_are_clamped(self):
        os.environ["QA_MEMORY_RECALL_LIMIT"] = "9999"
        self.assertLessEqual(memory.recall_limit(), 50)
        os.environ["QA_MEMORY_WRITE_MIN_UTILITY"] = "abc"
        self.assertEqual(memory.write_min_utility(), memory.DEFAULT_WRITE_MIN_UTILITY)


class DependencyGuardTests(unittest.TestCase):
    def test_memory_module_has_no_network_or_model_dependencies(self):
        path = os.path.join(REPO_ROOT, "qa_memory.py")
        imports = _imports(path)
        for name in NETWORK_MODULES:
            self.assertNotIn(name, imports, "qa_memory.py 不许 import %s" % name)
        source = open(path, encoding="utf-8").read()
        for marker in ("http://", "https://", "requests.", "urlopen", "_embed_question",
                       "chat_api.", "embedding_client"):
            self.assertNotIn(marker, source,
                             "qa_memory.py 里出现了 %r（阶段硬约束：零模型/零嵌入端点调用）" % marker)

    def test_context_pack_does_not_import_memory_at_module_level(self):
        path = os.path.join(REPO_ROOT, "qa_context_pack.py")
        self.assertNotIn("qa_memory", _imports(path),
                         "qa_context_pack 不许在模块级 import qa_memory（会成环；memory 侧是延迟 import）")

    def test_memory_hint_mark_is_shared_with_the_context_pack(self):
        self.assertEqual(memory.MEMORY_HINT_MARK.count("记忆提示"), 1)
        self.assertTrue(context_pack.MEMORY_HINT_POLICY.startswith("MEMORY_HINT"))


if __name__ == "__main__":
    unittest.main()

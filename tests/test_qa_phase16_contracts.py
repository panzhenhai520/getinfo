#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 16 · 契约与冻结面守门（P16-01…P16-06）。

钉住五件事：
  1. **七契约指纹复算不变**（P00-02 冻结面一个字不动）；
  2. `QA_SCHEMA_VERSION` 仍是 v11、**零新表零新列**（本阶段只做只读投影）；
  3. Phase 16 契约段自洽：六个视图 / 四种状态 / 理由码闭集合 / 来源白名单；
  4. schema 能校验合法载荷、也能拦住越界取值（含嵌套）；
  5. **只读硬约束**：投影模块与 API 模块的 AST 里没有任何写路径（SQL 写动词、写方法调用），
     蓝图只注册 GET。
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

import qa_contracts  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_observability_ui as ui  # noqa: E402
import qa_schema  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# P00-02 冻结的七个指纹（与 Phase 05/15 的守门用例同一份口径与同一份期望值）
FROZEN_FINGERPRINTS = {
    "EVIDENCE_SCHEMA": "370301331c02c738",
    "CLAIM_SCHEMA": "06fcdb02441248b2",
    "CONFLICT_SCHEMA": "aabd3259b07f9a3e",
    "LEVEL1_RESULT_SCHEMA": "7e864764429db111",
    "LEVEL2_RESULT_SCHEMA": "a0484f894e7bc9d6",
    "FINAL_ANSWER_SCHEMA": "4d1efa54ca1cbc1a",
    "QA_EVENT_SCHEMA": "59358bfa88a6c6af",
}

# 只读守门：这些名字出现在投影/API 模块里就说明"UI 层可能改了东西"
WRITE_METHOD_PREFIXES = ("save_", "write_", "record_", "persist_", "update_", "delete_",
                         "insert_", "drop_", "mark_", "revoke_", "bump_", "apply_write_gate")
WRITE_SQL_MARKERS = ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP TABLE", "CREATE TABLE",
                     "ALTER TABLE", "REPLACE INTO")


def _fingerprint(value) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _module_ast(name: str) -> ast.Module:
    with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=name)


def _iter_strings(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


class FrozenContractTests(unittest.TestCase):
    def test_seven_fingerprints_are_unchanged(self):
        for name, expected in FROZEN_FINGERPRINTS.items():
            self.assertEqual(_fingerprint(getattr(qa_contracts, name)), expected,
                             "%s 契约指纹变了：Phase 16 不许动冻结面" % name)

    def test_evidence_schema_still_closed(self):
        self.assertIs(qa_contracts.EVIDENCE_SCHEMA.get("additionalProperties"), False)
        self.assertIs(qa_contracts.FINAL_ANSWER_SCHEMA.get("additionalProperties"), False)

    def test_schema_version_unchanged(self):
        self.assertEqual(qa_schema.QA_SCHEMA_VERSION, "unified-qa-schema-v11",
                         "P16 零迁移：版本号必须仍是 v11（等值断言，未放宽）")

    def test_no_new_tables_or_columns(self):
        self.assertEqual(qa_schema.QA_ADDED_COLUMNS_V11, (), "v11 只许新建表，不许 ADD COLUMN")
        self.assertFalse(hasattr(qa_schema, "QA_ADDED_COLUMNS_V12"),
                         "P16 不许引入 v12（本阶段是纯只读投影）")
        self.assertIn("qa_reasoning_traces", qa_schema.QA_REQUIRED_TABLES)
        self.assertNotIn("qa_observability", " ".join(qa_schema.QA_REQUIRED_TABLES))


class ContractSectionTests(unittest.TestCase):
    def test_version_and_views_are_exact(self):
        self.assertEqual(contracts.OBSERVABILITY_UI_VERSION, "qa-observability-ui-v1")
        self.assertEqual(contracts.OBSERVABILITY_VIEWS,
                         ("execution", "evidence", "context", "memory", "search_trace",
                          "dashboard"))
        # §15 要求四图 + Search Trace，§32 要求看板：六个视图一个不少、一个不多
        self.assertEqual(len(contracts.OBSERVABILITY_VIEWS), 6)

    def test_states_are_closed_and_reasons_are_closed(self):
        self.assertEqual(contracts.OBSERVABILITY_STATES,
                         ("ok", "empty", "degraded", "unavailable"))
        self.assertEqual(len(contracts.OBSERVABILITY_STATE_REASONS),
                         len(set(contracts.OBSERVABILITY_STATE_REASONS)),
                         "状态理由码不许重复")
        for reason in ("PRODUCT_PRESENT", "SWITCH_OFF", "NO_STAGE_ARTIFACT",
                       "NO_RUN_IN_SCOPE", "NO_MEMORY_IN_SCOPE", "NO_SKILL_TELEMETRY",
                       "NO_CACHE_ENTRY", "PHASE_DEGRADED", "PARTIAL_READ", "STORE_UNAVAILABLE"):
            self.assertIn(reason, contracts.OBSERVABILITY_STATE_REASONS)

    def test_sources_whitelist_names_the_owning_phase(self):
        self.assertTrue(contracts.OBSERVABILITY_SOURCES)
        for item in contracts.OBSERVABILITY_SOURCES:
            self.assertIn("·", item, "来源必须写成 `<阶段>·<载体>`：%s" % item)
            self.assertTrue(item.split("·", 1)[0].startswith("P"), item)
        # 投影层只从契约里取来源，不自己写第二份字面量（同阶段多条载体时首个优先）
        expected: dict = {}
        for item in contracts.OBSERVABILITY_SOURCES:
            expected.setdefault(item.split("·", 1)[0], item)
        self.assertEqual(ui.SRC, expected)
        self.assertIn("qa_events", ui.SRC_CONTEXT_EVENTS,
                      "Phase 08 的 SSE 回执载体必须也在白名单里（快速路径只有它）")

    def test_read_only_flag_is_true(self):
        self.assertIs(contracts.OBSERVABILITY_READ_ONLY, True)

    def test_describe_mentions_phase16_version(self):
        text = contracts.describe()
        self.assertIn("qa-observability-ui-v1", text)
        for token in ("可观测性视图", "视图状态", "口径来源"):
            self.assertIn(token, text)


class SchemaTests(unittest.TestCase):
    def _valid(self, **overrides):
        payload = {"view_version": contracts.OBSERVABILITY_UI_VERSION, "view": "execution",
                   "run_id": "r1", "state": "ok", "state_reason": "PRODUCT_PRESENT",
                   "source": "P05·x", "numbers": {}, "payload": {}, "read_only": True}
        payload.update(overrides)
        return payload

    def test_valid_payload_passes(self):
        ok, note = validate("observability_view", self._valid())
        self.assertTrue(ok, note)

    def test_out_of_range_values_are_rejected(self):
        cases = [
            {"view": "nope"}, {"state": "maybe"}, {"state_reason": "MADE_UP"},
            {"view_version": "qa-observability-ui-v2"},
        ]
        for override in cases:
            ok, note = validate("observability_view", self._valid(**override))
            self.assertFalse(ok, "%s 应该被拦下" % override)
            self.assertTrue(note)

    def test_missing_required_field_is_rejected(self):
        payload = self._valid()
        payload.pop("numbers")
        ok, note = validate("observability_view", payload)
        self.assertFalse(ok)
        self.assertIn("numbers", note)

    def test_number_schema_requires_source_and_definition(self):
        ok, _note = validate("observability_number", {
            "field": "claims", "value": 3,
            "source": "P06·x", "definition": "结论节点数"})
        self.assertTrue(ok)
        for missing in ("source", "definition", "field"):
            payload = {"field": "claims", "value": 3, "source": "P06·x",
                       "definition": "结论节点数"}
            payload.pop(missing)
            ok, _note = validate("observability_number", payload)
            self.assertFalse(ok, "缺 %s 时必须拦下" % missing)

    def test_unknown_schema_is_rejected(self):
        ok, note = validate("observability_nope", {})
        self.assertFalse(ok)
        self.assertIn("未知 schema", note)


class ReadOnlyGuardTests(unittest.TestCase):
    """§15 的只读硬约束：界面不许改动证据/结论/记忆 —— 用 AST 钉死。"""

    MODULES = ("qa_observability_ui.py", "qa_observability_api.py")

    def test_no_write_sql_in_the_ui_layers(self):
        for name in self.MODULES:
            for text in _iter_strings(_module_ast(name)):
                upper = text.upper()
                for marker in WRITE_SQL_MARKERS:
                    self.assertNotIn(marker, upper,
                                     "%s 里出现了写 SQL：%s" % (name, marker))

    def test_no_write_method_calls_in_the_ui_layers(self):
        for name in self.MODULES:
            tree = _module_ast(name)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                attr = getattr(func, "attr", "")
                if attr and str(attr).startswith(WRITE_METHOD_PREFIXES):
                    self.fail("%s 调用了写方法：%s" % (name, attr))

    def test_api_blueprint_registers_get_only(self):
        from qa_observability_api import observability_bp

        tree = _module_ast("qa_observability_api.py")
        methods_seen = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", "") != "route":
                continue
            for keyword in node.keywords:
                if keyword.arg == "methods" and isinstance(keyword.value, ast.List):
                    for element in keyword.value.elts:
                        if isinstance(element, ast.Constant):
                            methods_seen.add(str(element.value))
        self.assertEqual(methods_seen, {"GET"},
                         "可观测性 API 只许注册 GET（实测：%s）" % sorted(methods_seen))
        self.assertEqual(len(observability_bp.deferred_functions), 4,
                         "只读 API 的端点数量写死在守门用例里（增删都要显式改这里）")

    def test_ui_module_imports_no_network_or_write_paths(self):
        tree = _module_ast("qa_observability_ui.py")
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for forbidden in ("requests", "urllib", "urllib3", "httpx", "socket", "openai",
                          "qa_writer", "qa_memory_write"):
            self.assertNotIn(forbidden, imported, "投影层不许碰外部调用：%s" % forbidden)
        for text in _iter_strings(tree):
            self.assertNotIn("http://", text)
            self.assertNotIn("https://", text)


if __name__ == "__main__":
    unittest.main()

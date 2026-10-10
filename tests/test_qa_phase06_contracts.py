#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 06 · 契约守门（不许放宽任何冻结 schema，不许偷偷联网）。

专测"边界"，不测业务质量：
  1. 七个**冻结契约**指纹复算必须与 P00-02 一字不差（Phase 06 只新增证据图契约）；
  2. Phase 01/02 的枚举一个字没动：证据条目级 `CLAIM_EVIDENCE_RELATIONSHIPS` 仍是 4 值，
     且与冻结 `EVIDENCE_SCHEMA.relationship` 枚举**机器对齐**（这是"新增取值走新契约、
     不扩张旧枚举"的可验证证据）；检索通道/失败策略/停止原因/证据状态都不动；
  3. Phase 06 新枚举自洽：四类关系齐全、`MENTIONS` 有明确存在理由、理由码 resolved/unresolved
     是一个划分、关系→端点组合的矩阵合法；
  4. 新 schema 能拦缺字段与越界枚举；
  5. **零联网 / 零模型守卫**：`qa_evidence_graph.py` 不 import 任何 HTTP/套接字库、源码里
     没有 http(s) 字面量、没有模型客户端调用痕迹（GPU 与语音机器人共用、已停用）；
  6. 库表结构本阶段不动（`QA_SCHEMA_VERSION` 仍 v7、无新表/新列）；开关默认关；
  7. 证据图载荷必须 **JSON 可序列化**（阶段输出会被 `record_stage` 直接 json.dumps 落库）。
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
import qa_evidence_graph as eg  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_schema  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402
from qa_phase06_fixtures import claim_node, evidence, graph_of  # noqa: E402

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
FROZEN_ROUTES = ("keyword", "semantic", "graph", "graph_attribute", "page_context",
                 "policy_exact", "web")
FROZEN_FAILURE_POLICIES = ("FAIL_FAST", "RETRY", "SKIP", "FALLBACK", "DEGRADE")
FROZEN_STOP_REASONS = ("ANSWERABLE", "BUDGET_EXHAUSTED", "MAX_DEPTH", "NO_GAIN",
                       "UNRESOLVABLE_CONTRADICTION")
FROZEN_EVIDENCE_RELATIONSHIPS = ("supports", "contradicts", "qualifies", "context")
PHASE06_MODULES = ("qa_evidence_graph.py",)
NETWORK_MODULES = {"requests", "urllib3", "httpx", "socket", "aiohttp", "http.client",
                   "urllib.request", "ftplib", "telnetlib", "paramiko", "openai", "anthropic",
                   "transformers", "torch", "sentence_transformers"}
MODEL_CLIENTS = {"qa_gateway", "qa_level1", "qa_provider_registry", "qa_llm_router", "chat_api",
                 "qa_endpoint_profile", "qa_ragflow_client"}


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

    def test_conflict_schema_resolution_domain_unchanged(self):
        self.assertEqual(qa_contracts.CONFLICT_SCHEMA["properties"]["resolution"]["enum"],
                         ["resolved", "unresolved"], "裁决结果取值域不许扩张")
        self.assertEqual(sorted(qa_contracts.CONFLICT_SCHEMA["properties"]),
                         sorted(["conflict_id", "subject", "conflict_type", "claim_ids",
                                 "evidence_refs", "resolution", "rationale", "rule_version"]))

    def test_evidence_level_relationship_enum_is_untouched(self):
        values = qa_contracts.EVIDENCE_SCHEMA["properties"]["relationship"]["enum"]
        self.assertEqual([item for item in values if item], list(FROZEN_EVIDENCE_RELATIONSHIPS))
        self.assertIn(None, values, "旧契约里的 null = 未判定，必须保留")
        self.assertEqual(list(contracts.CLAIM_EVIDENCE_RELATIONSHIPS),
                         list(FROZEN_EVIDENCE_RELATIONSHIPS))

    def test_phase01_enums_are_untouched(self):
        self.assertEqual(list(contracts.QA_RETRIEVAL_ROUTES), list(FROZEN_ROUTES))
        self.assertEqual(list(contracts.QA_FAILURE_POLICIES), list(FROZEN_FAILURE_POLICIES))
        self.assertEqual(list(contracts.QA_STOP_REASONS), list(FROZEN_STOP_REASONS))
        self.assertEqual(len(contracts.EVIDENCE_STATUSES), 5)
        self.assertEqual(list(contracts.EVIDENCE_GRAPH_RELATIONSHIPS),
                         ["SUPPORTS", "REFUTES", "DEPENDS", "CONTRADICTS", "MENTIONS"])

    def test_schema_version_and_columns_unchanged(self):
        self.assertEqual(qa_schema.QA_SCHEMA_VERSION, "unified-qa-schema-v8",
                         "Phase 06 自身零库表变更；v8 是 Phase 09 的变更（八张 memory_* 表），本条仍钉死字面量")
        self.assertEqual(len(qa_schema.QA_ADDED_COLUMNS_V6), 15,
                         "ADD COLUMN 清单被改动了（Phase 06 声明零迁移）")
        blob = " ".join(qa_schema.QA_TABLE_DDL)
        for marker in ("qa_evidence_graph", "qa_claim_relation", "qa_contradiction",
                       "qa_claim_contradiction"):
            self.assertNotIn(marker, blob, "不许为证据图新建表")

    def test_conflict_schema_is_reused_not_widened(self):
        """裁决细节走新契约；冻结冲突契约的字段集与枚举都不动。"""
        conflict = {"conflict_id": "c1", "subject": "s", "conflict_type": "real_conflict",
                    "claim_ids": ["a", "b"], "evidence_refs": [], "resolution": "resolved",
                    "rationale": "r", "rule_version": "qa-contradiction-resolver-v1"}
        qa_contracts._validate(qa_contracts.CONFLICT_SCHEMA, conflict, "冲突")
        broken = dict(conflict, winner={"side": "left"})
        with self.assertRaises(qa_contracts.QaContractError):
            qa_contracts._validate(qa_contracts.CONFLICT_SCHEMA, broken, "冲突")


class NewContractTests(unittest.TestCase):
    def test_relation_matrix_and_reason_codes_are_well_formed(self):
        for relation in ("SUPPORTS", "REFUTES", "DEPENDS", "CONTRADICTS"):
            self.assertIn(relation, contracts.EVIDENCE_GRAPH_RELATIONSHIPS)
        self.assertIn("MENTIONS", contracts.EVIDENCE_GRAPH_RELATIONSHIPS,
                      "context/未判定证据必须有落脚点（否则只能错记成 SUPPORTS）")
        resolved = set(contracts.CONTRADICTION_RESOLVED_CODES)
        unresolved = set(contracts.CONTRADICTION_UNRESOLVED_CODES)
        self.assertEqual(resolved | unresolved, set(contracts.CONTRADICTION_RESOLUTION_CODES))
        self.assertEqual(resolved & unresolved, set())
        self.assertEqual(len(contracts.CONTRADICTION_RESOLUTION_CODES), 9)
        for kind, allowed in contracts.EVIDENCE_GRAPH_RELATIONS_BY_KIND.items():
            self.assertIn(kind, contracts.EVIDENCE_GRAPH_EDGE_KINDS)
            self.assertTrue(set(allowed) <= set(contracts.EVIDENCE_GRAPH_RELATIONSHIPS))

    def test_evidence_graph_edge_schema_rejects_bad_payloads(self):
        ok, note = validate("evidence_graph_edge", {"edge_id": "e", "kind": "claim-evidence",
                                                    "src": "a", "dst": "b",
                                                    "graph_relation": "SUPPORTS"})
        self.assertTrue(ok, note)
        ok, note = validate("evidence_graph_edge", {"edge_id": "e", "kind": "claim-evidence",
                                                    "src": "a", "dst": "b",
                                                    "graph_relation": "TRUSTS"})
        self.assertFalse(ok)
        self.assertIn("graph_relation", note)
        ok, note = validate("evidence_graph_edge", {"edge_id": "e", "src": "a", "dst": "b"})
        self.assertFalse(ok, "缺 kind 必须被拦")

    def test_node_and_coverage_schemas(self):
        ok, note = validate("evidence_graph_node", {"node_id": "claim:c1", "node_type": "claim"})
        self.assertTrue(ok, note)
        ok, note = validate("evidence_graph_node", {"node_id": "x", "node_type": "gap"})
        self.assertFalse(ok, "Phase 06 只物化 claim/evidence/source/contradiction")

    def test_contradiction_decision_schema(self):
        payload = {"contradiction_id": "ctr:1", "kind": "evidence_conflict",
                   "resolution": "resolved", "reason_code": "AUTHORITY_ADVANTAGE",
                   "rule_version": "qa-contradiction-resolver-v1"}
        ok, note = validate("contradiction_decision", payload)
        self.assertTrue(ok, note)
        for field, value in (("resolution", "maybe"), ("reason_code", "GUESS"),
                             ("kind", "vibes")):
            ok, _note = validate("contradiction_decision", dict(payload, **{field: value}))
            self.assertFalse(ok, "%s 的非法取值必须被拦" % field)

    def test_evidence_graph_schema_accepts_a_real_layer(self):
        node = claim_node("c1", refs=["e1"], status="confirmed", pairs=[
            {"evidence_ref": "e1", "verdict": "SUPPORTED", "score": 0.8}])
        layer = eg.build_layer(graph_of([node], [evidence("e1")]))
        ok, note = validate("evidence_graph", layer)
        self.assertTrue(ok, note)
        ok, note = validate("evidence_graph", dict(layer, graph_version=None))
        self.assertFalse(ok)

    def test_layer_payload_is_json_serializable(self):
        """阶段输出会被 `record_stage` 直接 json.dumps 落库——datetime 之类的会让整条写不进去。"""
        node = claim_node("c1", refs=["e1", "e2"], status="conflicted", authority=90, pairs=[
            {"evidence_ref": "e1", "verdict": "SUPPORTED", "score": 0.8},
            {"evidence_ref": "e2", "verdict": "REFUTED", "score": 0.2}])
        layer = eg.build_layer(graph_of([node],
                                       [evidence("e1"), evidence("e2", relation="contradicts")]))
        body = json.dumps(layer, ensure_ascii=False, allow_nan=False)
        self.assertIn("contradiction", body)
        self.assertGreater(len(body), 100)

    def test_describe_mentions_the_phase06_contract(self):
        text = contracts.describe()
        self.assertIn(contracts.EVIDENCE_GRAPH_VERSION, text)
        self.assertIn("qa-hunter-v1", text, "Phase 04 的断言不能被打破")
        self.assertIn("qa-execution-graph-v1", text, "Phase 05 的断言不能被打破")
        self.assertIn("证据图关系 5", text)


class NoModelNoNetworkGuardTests(unittest.TestCase):
    def test_no_network_imports(self):
        for name in PHASE06_MODULES:
            tree = ast.parse(_source(name))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            self.assertEqual(imported & NETWORK_MODULES, set(),
                             "%s 引入了网络/模型库：%s" % (name, imported & NETWORK_MODULES))
            self.assertEqual(imported & MODEL_CLIENTS, set(),
                             "%s 引入了模型客户端：%s" % (name, imported & MODEL_CLIENTS))

    def test_no_endpoint_literals(self):
        for name in PHASE06_MODULES:
            source = _source(name)
            for token in ("http://", "https://", "/v1/chat", "/v1/embeddings",
                          "embedding_client", "_embed_question", "openai", "api_key"):
                self.assertNotIn(token, source, "%s 里出现端点/密钥痕迹：%s" % (name, token))

    def test_switches_default_off(self):
        saved = os.environ.pop("QA_EVIDENCE_GRAPH", None)
        try:
            self.assertFalse(eg.evidence_graph_enabled())
            self.assertEqual(eg.resolver_name(), "rule")
            self.assertEqual(eg.contradiction_resolvers(), ())
        finally:
            if saved is not None:
                os.environ["QA_EVIDENCE_GRAPH"] = saved

    def test_default_resolver_needs_no_injection(self):
        self.assertEqual(eg.resolver_report()["version"],
                         contracts.CONTRADICTION_RESOLVER_VERSION)
        self.assertEqual(eg.resolver_report()["thresholds"], eg.thresholds())


if __name__ == "__main__":
    unittest.main()

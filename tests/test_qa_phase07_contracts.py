#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 07 · 契约守门（不许放宽任何冻结 schema，不许偷偷联网/调模型）。

专测"边界"，不测业务质量：
  1. 七个**冻结契约**指纹复算必须与 P00-02 一字不差（Phase 07 只新增缺口/下一跳契约）；
  2. Phase 01–06 的枚举一个字没动：检索通道仍是 7 值、失败策略 5 值、**停止原因仍是 5 值**
     （Phase 07 是让后两个值真的被产出，不是新增取值）、证据状态 5 值、证据图关系 5 值；
  3. Phase 07 新契约自洽：十种缺口类型齐全且都有严重度/通道/证据类型；优先级分档是划分；
     建议通道 ⊆ 冻结通道枚举；下一跳 route 也只能是既有通道；
  4. 新 schema 能拦缺字段与越界枚举（`gap` / `next_hop` / `gap_loop` / `gap_loop_round`）；
  5. **零联网 / 零模型守卫**：`qa_gap_analyzer.py` 不 import 任何 HTTP/套接字/模型库、
     源码里没有 http(s) 字面量与模型端点痕迹（GPU 与语音机器人共用、已停用）；
  6. 库表结构本阶段不动（`QA_SCHEMA_VERSION` 仍 v7、无新表/新列）；开关默认关、默认规划器是规则；
  7. 缺口回执必须 **JSON 可序列化**（阶段输出会被 `record_stage` 直接 json.dumps 落库）；
  8. 执行图里 P07 的 `gap_loop` 不再是 deferred 占位（Phase 05 的占位账本阶段结清）。
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
import qa_execution_graph as eg  # noqa: E402
import qa_gap_analyzer as gap  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_schema  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402
from qa_phase07_fixtures import plan_claim  # noqa: E402

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
PHASE07_MODULES = ("qa_gap_analyzer.py",)
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

    def test_phase01_to_06_enums_are_untouched(self):
        self.assertEqual(list(contracts.QA_RETRIEVAL_ROUTES), list(FROZEN_ROUTES))
        self.assertEqual(list(contracts.QA_FAILURE_POLICIES), list(FROZEN_FAILURE_POLICIES))
        self.assertEqual(list(contracts.QA_STOP_REASONS), list(FROZEN_STOP_REASONS),
                         "Phase 07 是**补齐**停止原因的产出路径，不是新增取值")
        self.assertEqual(len(contracts.EVIDENCE_STATUSES), 5)
        self.assertEqual(list(contracts.EVIDENCE_GRAPH_RELATIONSHIPS),
                         ["SUPPORTS", "REFUTES", "DEPENDS", "CONTRADICTS", "MENTIONS"])
        self.assertEqual(len(contracts.CONTRADICTION_RESOLUTION_CODES), 9)
        self.assertEqual(list(contracts.CLAIM_EVIDENCE_RELATIONSHIPS),
                         ["supports", "contradicts", "qualifies", "context"])
        self.assertEqual(list(contracts.QA_HUNTER_IDS),
                         ["bm25", "semantic", "graph", "structured", "query_expansion"])

    def test_schema_version_and_columns_unchanged(self):
        self.assertEqual(qa_schema.QA_SCHEMA_VERSION, "unified-qa-schema-v7",
                         "Phase 07 没有库表变更，版本号不许动")
        self.assertEqual(len(qa_schema.QA_ADDED_COLUMNS_V6), 15,
                         "ADD COLUMN 清单被改动了（Phase 07 声明零迁移）")
        blob = " ".join(qa_schema.QA_TABLE_DDL)
        for marker in ("qa_gap", "qa_next_hop", "qa_gap_loop"):
            self.assertNotIn(marker, blob, "不许为缺口分析新建表（回执免费持久化在既有表）")

    def test_gap_columns_from_phase01_still_exist(self):
        """Phase 07 用的是 Phase 01 就加好的三列，不是新列。"""
        blob = " ".join(qa_schema.QA_TABLE_DDL)
        for column in ("gap_id", "new_claims", "resolved_gap"):
            self.assertIn(column, blob, "%s 是 Phase 01 加的列，不许丢" % column)


class NewContractTests(unittest.TestCase):
    def test_gap_taxonomy_is_complete_and_well_formed(self):
        self.assertEqual(len(contracts.QA_GAP_TYPES), 10)
        self.assertEqual(len(set(contracts.QA_GAP_TYPES)), 10)
        bands = set(contracts.GAP_PRIORITY_BANDS)
        self.assertEqual(len(bands), 4)
        self.assertEqual(set(contracts.GAP_STATUSES), {"open", "resolved", "unactionable"})
        self.assertAlmostEqual(sum(contracts.GAP_PRIORITY_WEIGHTS.values()), 1.0, places=6)
        for name in contracts.QA_GAP_TYPES:
            self.assertIn(name, gap.GAP_SEVERITY)
            self.assertIn(name, gap.EVIDENCE_TYPE_BY_GAP)

    def test_new_schemas_are_registered_and_reject_bad_payloads(self):
        for name in ("gap", "next_hop", "gap_loop", "gap_loop_round"):
            ok, note = validate(name, {} if name == "gap_loop" else {})
            self.assertFalse(ok, "%s 必须拦缺字段" % name)
        ok, note = validate("gap", {"gap_id": "G1", "missing": "NO_EVIDENCE", "priority": 0.9,
                                    "suggested_routes": ["keyword"], "reason": "r"})
        self.assertTrue(ok, note)
        for field, value in (("missing", "VIBES"), ("suggested_routes", ["bm25"])):
            ok, _note = validate("gap", {"gap_id": "G1", "missing": "NO_EVIDENCE",
                                         "priority": 0.9, "suggested_routes": ["keyword"],
                                         "reason": "r", field: value})
            self.assertFalse(ok, "%s 的非法取值必须被拦" % field)

    def test_next_hop_route_is_inside_the_frozen_enum(self):
        ok, note = validate("next_hop", {"hop_id": "g1", "gap_id": "G1", "question": "q",
                                         "queries": ["q"], "route": "keyword", "reason": "r"})
        self.assertTrue(ok, note)
        ok, _note = validate("next_hop", {"hop_id": "g1", "gap_id": "G1", "question": "q",
                                          "queries": ["q"], "route": "bm25", "reason": "r"})
        self.assertFalse(ok, "route 只吃既有 7 个通道值（Hunter 身份是另一层，见 D-017）")

    def test_gap_loop_schema_accepts_a_real_receipt(self):
        state = gap.GapLoopState(budget_seconds=0.0)
        state.observe(round_index=0, claims=[plan_claim("c1", "医保新规要求按病种付费")],
                      evidence=[])
        state.next_hops()
        state.finalize(depth_exhausted=True)
        receipt = state.receipt()
        ok, note = validate("gap_loop", receipt)
        self.assertTrue(ok, note)
        body = json.dumps(receipt, ensure_ascii=False, allow_nan=False)
        self.assertIn(contracts.GAP_ANALYZER_VERSION, body)
        self.assertIn(str(receipt["stop_reason"]), body)
        self.assertGreater(len(body), 200)

    def test_describe_mentions_the_phase07_contract(self):
        text = contracts.describe()
        self.assertIn(contracts.GAP_ANALYZER_VERSION, text)
        self.assertIn("qa-hunter-v1", text, "Phase 04 的断言不能被打破")
        self.assertIn("qa-execution-graph-v1", text, "Phase 05 的断言不能被打破")
        self.assertIn("qa-evidence-graph-v1", text, "Phase 06 的断言不能被打破")
        self.assertIn("缺口类型 10", text)


class ExecutionGraphTests(unittest.TestCase):
    def test_gap_loop_is_no_longer_deferred(self):
        question = "香港家族办公室税收优惠政策对内地高净值客户有什么影响？"
        for mode in ("fast", "standard", "deep"):
            graph = eg.build_execution_graph(question, plan={}, mode=mode)
            node = [item for item in graph["nodes"]
                    if item["node_id"] == "%s.gap_loop" % mode]
            self.assertTrue(node, "%s 缺少 gap_loop 节点" % mode)
            self.assertTrue(node[0]["implemented"], "P07 之后 gap_loop 不再是占位")
            self.assertEqual(node[0]["deferred_to"], "")
            self.assertNotEqual(node[0]["status"], "deferred")
        names = {item["node_id"].split(".")[-1] for item in
                 eg.build_execution_graph(question, plan={}, mode="deep")["nodes"]
                 if item["status"] == "deferred"}
        self.assertEqual(names, {"final_verify"}, "只剩 P13 还是占位")


class NoModelNoNetworkGuardTests(unittest.TestCase):
    def test_no_network_imports(self):
        for name in PHASE07_MODULES:
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
        for name in PHASE07_MODULES:
            source = _source(name)
            for token in ("http://", "https://", "/v1/chat", "/v1/embeddings",
                          "embedding_client", "_embed_question", "openai", "api_key"):
                self.assertNotIn(token, source, "%s 里出现端点/密钥痕迹：%s" % (name, token))

    def test_switches_default_off_and_rule_planner(self):
        saved = os.environ.pop("QA_GAP_ANALYZER", None)
        saved_planner = os.environ.pop("QA_NEXT_HOP_PLANNER", None)
        try:
            self.assertFalse(gap.gap_analyzer_enabled(), "缺口循环默认关（回滚口径）")
            self.assertEqual(gap.planner_name(), "rule")
            self.assertEqual(gap.next_hop_planners(), (), "本轮不注册任何规划器后端")
            self.assertEqual(gap.no_gain_rounds(), 2, "§14：默认连续两轮")
            self.assertEqual(gap.max_next_hops(), 1)
            self.assertEqual(gap.authority_floor(), 50)
        finally:
            if saved is not None:
                os.environ["QA_GAP_ANALYZER"] = saved
            if saved_planner is not None:
                os.environ["QA_NEXT_HOP_PLANNER"] = saved_planner

    def test_knobs_are_clamped(self):
        os.environ["QA_GAP_NO_GAIN_ROUNDS"] = "99"
        os.environ["QA_GAP_MAX_NEXT_HOPS"] = "99"
        os.environ["QA_GAP_PRIORITY_THRESHOLD"] = "5"
        try:
            self.assertEqual(gap.no_gain_rounds(), 5)
            self.assertEqual(gap.max_next_hops(), 4)
            self.assertEqual(gap.priority_threshold(), 1.0)
        finally:
            for name in ("QA_GAP_NO_GAIN_ROUNDS", "QA_GAP_MAX_NEXT_HOPS",
                         "QA_GAP_PRIORITY_THRESHOLD"):
                os.environ.pop(name, None)


if __name__ == "__main__":
    unittest.main()

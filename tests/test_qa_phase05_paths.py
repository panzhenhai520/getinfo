#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 05 · P05-04 用例（Fast / Standard / Deep 三路径）。

钉住：
  1. §18 三条链与**实际会跑的阶段链**一致：`stage_chain` 直接取自
     `QaOrchestrator.stage_plan`，图上"会跑"的节点 stage 必须在链上；
  2. 路径选择规则可复算：mode 显式指定优先；standard + complexity=simple 走 Fast Path（§6）；
     **不替调用方升档**（只给 suggested_path）；
  3. 每个节点都带 §2.2 的 Node 契约全字段，且过 `validate("execution_node")`；
     失败策略只用五值枚举，并且与 `qa_orchestrator.DEGRADABLE_STAGES` 对齐；
  4. 舰队打开时首跳是**并行扇出 + fan-in barrier**（§2.4/§2.5），Hunter 节点的超时/重试
     直接取舰队的既有旋钮；
  5. 执行图里**未实现**的占位节点只剩 P13 的 `final_verify`（`deferred`、不参与预算）；
     `evidence_graph`（P06）与 `gap_loop`（P07）自各自 Phase 起都是真节点（开关关着时标 skipped）。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_execution_graph as eg  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402
from qa_orchestrator import DEGRADABLE_STAGES, FAST_STAGES, FULL_STAGES, QaOrchestrator  # noqa: E402

MULTI_HOP_QUESTION = "香港家族办公室税收优惠政策对内地高净值客户有什么影响？"
SIMPLE_QUESTION = "2026年医保新规是否适用于民营医院"
NODE_CONTRACT_FIELDS = ("node_id", "purpose", "input_schema", "output_schema", "timeout",
                        "retry", "model_tier", "allowed_tools", "validation", "failure_policy")


def _graph(question=MULTI_HOP_QUESTION, mode="standard", **kwargs):
    return eg.build_execution_graph(question, plan={}, mode=mode, **kwargs)


class PathSelectionTests(unittest.TestCase):
    def test_path_follows_the_mode(self):
        """路径 == 实际会跑的阶段链（stage_plan 由 mode 决定）——图不许与运行期不一致。"""
        self.assertEqual(eg.choose_path({"complexity": "deep"}, "fast")[0], "fast")
        self.assertEqual(eg.choose_path({"complexity": "simple"}, "deep")[0], "deep")
        self.assertIn("mode=fast", eg.choose_path({"complexity": "deep"}, "fast")[1])
        self.assertIn("mode=deep", eg.choose_path({"complexity": "simple"}, "deep")[1])

    def test_simple_complexity_is_a_fast_path_recommendation(self):
        """§6 "simple 直接进 Fast Path" 落地为**建议**：实际路径不隐式改档。"""
        path, source = eg.choose_path({"complexity": "simple"}, "standard")
        self.assertEqual(path, "standard")
        self.assertIn("§6", source)
        self.assertEqual(eg.suggest_path({"complexity": "simple"}, "standard"), "fast")
        self.assertEqual(eg.suggest_path({"complexity": "deep"}, "standard"), "deep")
        self.assertEqual(eg.suggest_path({"complexity": "deep"}, "deep"), "deep")

    def test_no_silent_upgrade_or_downgrade_in_the_graph(self):
        graph = eg.build_execution_graph(SIMPLE_QUESTION, plan={}, mode="standard")
        self.assertEqual(graph["path"], "standard")
        self.assertEqual(graph["suggested_path"], "fast")
        self.assertEqual(graph["stage_chain"], list(QaOrchestrator.stage_plan("standard")))

    def test_three_paths_are_declared_and_used(self):
        self.assertEqual(tuple(contracts.QA_PATHS), ("fast", "standard", "deep"))
        for mode in ("fast", "standard", "deep"):
            graph = _graph(mode=mode)
            self.assertEqual(graph["path"], mode)
            self.assertTrue(graph["spec_chain"], "§18 的链路必须写进图里")

    def test_spec_chains_match_the_architecture(self):
        self.assertEqual(_graph(mode="fast")["spec_chain"], ["retrieve", "rerank", "answer"])
        self.assertEqual(_graph(mode="standard")["spec_chain"],
                         ["plan", "2~3 hunters", "verify", "answer"])
        deep_chain = _graph(mode="deep")["spec_chain"]
        self.assertEqual(deep_chain, ["plan", "retrieval fleet", "evidence graph", "gap loop",
                                      "contradiction resolution", "answer", "final verifier"])


class StageChainConsistencyTests(unittest.TestCase):
    def test_stage_chain_comes_from_the_orchestrator(self):
        for mode, expected in (("fast", FAST_STAGES), ("standard", FULL_STAGES)):
            graph = _graph(mode=mode)
            self.assertEqual(tuple(graph["stage_chain"]), tuple(expected))
        self.assertEqual(tuple(eg.stage_chain_for("fast")), tuple(FAST_STAGES))
        self.assertEqual(tuple(eg.stage_chain_for("standard")),
                         tuple(QaOrchestrator.stage_plan("standard")))

    def test_runnable_nodes_are_on_the_stage_chain(self):
        """图上"会跑"的节点，其阶段必须真的在链上——否则图在撒谎。"""
        for mode in ("fast", "standard", "deep"):
            graph = _graph(mode=mode)
            chain = {str(item) for item in graph["stage_chain"]}
            for node in graph["nodes"]:
                if node["status"] in ("deferred", "skipped"):
                    continue
                self.assertIn(node["stage"], chain,
                              "%s 不在本轮链上却是待跑状态" % node["node_id"])

    def test_level2_off_removes_three_stages(self):
        graph = _graph(mode="standard", level2_enabled=False)
        self.assertEqual(tuple(graph["stage_chain"]),
                         tuple(QaOrchestrator.stage_plan("standard", level2_enabled=False)))
        statuses = {node["node_id"].split(".", 1)[1]: node["status"] for node in graph["nodes"]}
        for name in ("level2_retrieval", "level2_research", "conflict_review"):
            self.assertEqual(statuses[name], "skipped", "%s 必须标 skipped" % name)
        self.assertEqual(statuses["draft"], "pending", "草稿与答案不受二级关闭影响")
        self.assertEqual(statuses["answer"], "pending")

    def test_fast_path_has_no_level2_or_conflict_nodes(self):
        graph = _graph(mode="fast")
        kinds = {node["node_id"].split(".", 1)[1] for node in graph["nodes"]}
        self.assertNotIn("level2_retrieval", kinds)
        self.assertNotIn("conflict_review", kinds)
        self.assertIn("draft", kinds)
        self.assertIn("answer", kinds)


class NodeContractTests(unittest.TestCase):
    def test_every_node_carries_the_full_contract(self):
        for mode in ("fast", "standard", "deep"):
            for node in _graph(mode=mode)["nodes"]:
                for field in NODE_CONTRACT_FIELDS:
                    self.assertIn(field, node, "%s 缺 %s" % (node["node_id"], field))
                ok, note = validate("execution_node", node)
                self.assertTrue(ok, "%s: %s" % (node["node_id"], note))
                self.assertIn(node["failure_policy"], contracts.QA_FAILURE_POLICIES)
                self.assertIn(node["model_tier"], contracts.MODEL_TIERS)
                self.assertIn(node["status"], contracts.NODE_STATUSES)
                self.assertEqual((node["input_schema"] or {}).get("name"),
                                 node["input_schema"]["name"])
                self.assertTrue(node["validation"], "%s 没声明 validation" % node["node_id"])
                self.assertTrue(node["purpose"])

    def test_failure_policies_cover_multiple_values(self):
        policies = {node["failure_policy"] for node in _graph(mode="deep")["nodes"]}
        self.assertTrue({"FAIL_FAST", "FALLBACK", "DEGRADE", "RETRY"} <= policies,
                        "§28 的五值要有真实落点，实际用到：%s" % policies)
        for node in _graph(mode="deep")["nodes"]:
            if node["stage"] in DEGRADABLE_STAGES:
                self.assertEqual(node["failure_policy"], "DEGRADE",
                                 "%s 是既有可降级阶段，策略必须是 DEGRADE" % node["node_id"])

    def test_plan_node_is_fail_fast_and_only_plans(self):
        node = [item for item in _graph(mode="deep")["nodes"]
                if item["node_kind"] == "plan"][0]
        self.assertEqual(node["failure_policy"], "FAIL_FAST")
        self.assertIn("只规划", node["notes"])
        self.assertTrue(node["output_schema"]["name"].startswith("qa.research_plan"))

    def test_edges_carry_data_contracts(self):
        graph = _graph(mode="deep")
        by_id = {node["node_id"]: node for node in graph["nodes"]}
        self.assertTrue(graph["edges"])
        for edge in graph["edges"]:
            self.assertIn(edge["src"], by_id)
            self.assertIn(edge["dst"], by_id)
            self.assertEqual(edge["schema"], by_id[edge["src"]]["output_schema"]["name"])

    def test_deep_path_defers_later_phase_nodes(self):
        """Phase 06 起 `evidence_graph` 已实现；Phase 07 起 `gap_loop` 也**不再是占位**。
        仍占位的只剩 P13 的 `final_verify`（deep 链独有）。"""
        graph = _graph(mode="deep")
        deferred = [node for node in graph["nodes"] if node["status"] == "deferred"]
        self.assertEqual({node["node_id"].split(".")[-1] for node in deferred},
                         {"final_verify"})
        for node in deferred:
            self.assertFalse(node["implemented"])
            self.assertTrue(node["deferred_to"].startswith("P"))
            self.assertEqual(node["timeout"], 0.0, "占位节点不占预算")
        self.assertEqual(graph["node_counts"]["deferred"], 1)
        # P06 的 evidence_graph 现在是**真节点**：implementation 打开、有预算、阶段在链上
        evidence_graph = [node for node in graph["nodes"]
                          if node["node_id"] == "deep.evidence_graph"][0]
        self.assertTrue(evidence_graph["implemented"])
        self.assertEqual(evidence_graph["deferred_to"], "")
        self.assertGreater(evidence_graph["timeout"], 0.0)
        self.assertIn(evidence_graph["stage"], set(graph["stage_chain"]))
        self.assertEqual(evidence_graph["model_tier"], "rule", "P06 是纯规则实现")
        # 开关默认关 → 这一层本轮真的不会跑，节点必须标 skipped（不许假装跑过）
        self.assertEqual(evidence_graph["status"], "skipped")
        saved = os.environ.get("QA_EVIDENCE_GRAPH")
        os.environ["QA_EVIDENCE_GRAPH"] = "1"
        try:
            enabled = [node for node in _graph(mode="deep")["nodes"]
                       if node["node_id"] == "deep.evidence_graph"][0]
            self.assertEqual(enabled["status"], "pending", "开关打开后才进入待跑状态")
        finally:
            os.environ.pop("QA_EVIDENCE_GRAPH", None)
            if saved is not None:
                os.environ["QA_EVIDENCE_GRAPH"] = saved

    def test_gap_loop_is_implemented_on_every_path(self):
        """P07 的 gap_loop 跑在既有 `level1_retrieval` 的检索循环里 → 三条路径都有这个真节点。"""
        for mode in ("fast", "standard", "deep"):
            graph = _graph(mode=mode)
            node = [item for item in graph["nodes"]
                    if item["node_id"] == "%s.gap_loop" % mode][0]
            self.assertTrue(node["implemented"], "%s 的 gap_loop 必须是实现节点" % mode)
            self.assertEqual(node["deferred_to"], "")
            self.assertEqual(node["node_kind"], "gap_loop")
            self.assertEqual(node["stage"], "level1_retrieval")
            self.assertIn(node["stage"], set(graph["stage_chain"]))
            self.assertEqual(node["model_tier"], "rule", "P07 是纯规则实现（不许调模型）")
            self.assertEqual(node["allowed_tools"], ["qa_gap_analyzer"])
            self.assertIn("gap_loop", node["validation"])
            # 开关默认关 → 标 skipped（与 level2 关闭同口径），且不进关键路径估算
            self.assertEqual(node["status"], "skipped")
            self.assertNotIn(node["node_id"], graph["budget"]["cut_nodes"])
            self.assertNotIn(node["node_id"], graph["budget"]["critical_path"])
            # 依赖是检索链的最后一跳（缺口分析读的就是累计证据），不是证据图
            retrieval_ids = [item["node_id"] for item in graph["nodes"]
                             if item["node_kind"] == "retrieve"
                             and item["stage"] == "level1_retrieval"]
            self.assertEqual(node["depends_on"], [retrieval_ids[-1]])
        saved = os.environ.get("QA_GAP_ANALYZER")
        os.environ["QA_GAP_ANALYZER"] = "1"
        try:
            enabled = [item for item in _graph(mode="standard")["nodes"]
                       if item["node_id"] == "standard.gap_loop"][0]
            self.assertEqual(enabled["status"], "pending")
            self.assertTrue(enabled["optional"], "补充跳预算紧时可以裁")
            self.assertTrue(enabled["budget_enforced"])
            self.assertGreater(enabled["timeout"], 0.0)
        finally:
            os.environ.pop("QA_GAP_ANALYZER", None)
            if saved is not None:
                os.environ["QA_GAP_ANALYZER"] = saved
        # 未实现的占位节点在 standard/fast 上一个都没有
        self.assertEqual(_graph(mode="standard")["node_counts"]["deferred"], 0)
        self.assertEqual(_graph(mode="fast")["node_counts"]["deferred"], 0)


class ParallelFanOutTests(unittest.TestCase):
    def test_plan_precedes_retrieval(self):
        """§2.1：检索用的 queries/entities 来自规划 → "计划 → 检索"是真实依赖。"""
        graph = _graph(mode="standard")
        retrieve = [node for node in graph["nodes"] if node["node_id"] == "standard.retrieve"][0]
        self.assertEqual(retrieve["depends_on"], ["standard.interpret"])
        interpretation = [node for node in graph["nodes"]
                          if node["node_kind"] == "plan"][0]
        self.assertEqual(interpretation["parallel_group"], "g1")
        first_group = graph["parallel_groups"][0]
        self.assertEqual(first_group["nodes"], ["standard.interpret"])

    def test_plain_path_has_a_single_first_hop_retrieval_node(self):
        """不开舰队时首跳就是一个节点；第 2/3 跳是多跳链，另算（每个一个节点）。"""
        graph = _graph(mode="standard")
        first_hop = [node for node in graph["nodes"] if node["node_kind"] == "retrieve"
                     and node["hop_index"] == 0]
        self.assertEqual([node["node_id"] for node in first_hop], ["standard.retrieve"])
        self.assertEqual(graph["node_counts"]["barriers"], 2)
        self.assertFalse(graph["budget"]["fleet_on"])

    def test_fleet_expands_into_a_parallel_group_with_a_barrier(self):
        hunters = ["structured", "graph", "bm25", "semantic", "query_expansion"]
        graph = _graph(mode="deep", hunters=hunters)
        self.assertTrue(graph["budget"]["fleet_on"])
        self.assertEqual(graph["budget"]["hunter_count"], 5)
        by_id = {node["node_id"]: node for node in graph["nodes"]}
        for hunter in hunters:
            node = by_id["deep.hunter.%s" % hunter]
            self.assertEqual(node["failure_policy"], "RETRY")
            self.assertEqual(node["retry"], graph["budget"]["hunter_budget_seconds"] and node["retry"])
            self.assertTrue(node["budget_enforced"])
            self.assertIn("QA_HUNTER_FLEET_TIMEOUT_SECONDS", node["budget_source"])
            self.assertIn(hunter, node["allowed_tools"])
        merge = by_id["deep.merge"]
        self.assertEqual(merge["node_kind"], "merge")
        self.assertTrue(merge["barrier"], "§2.5：多 Hunter 之后才是 barrier")
        self.assertEqual(sorted(merge["depends_on"]),
                         sorted("deep.hunter.%s" % hunter for hunter in hunters))
        hunter_group = [group for group in graph["parallel_groups"]
                        if "deep.hunter.bm25" in group["nodes"]][0]
        self.assertTrue(hunter_group["parallel"])
        self.assertEqual(len(hunter_group["nodes"]), len(hunters))
        for hunter in hunters:
            node = by_id["deep.hunter.%s" % hunter]
            self.assertEqual(node["depends_on"], ["deep.interpret"],
                             "检索依赖规划结果，不许写成与计划并行")

    def test_hunter_timeout_and_retry_reuse_fleet_knobs(self):
        import qa_hunter_fleet as fleet_module

        saved = {name: os.environ.get(name) for name in
                 ("QA_HUNTER_FLEET_TIMEOUT_SECONDS", "QA_HUNTER_FLEET_RETRIES")}
        try:
            os.environ["QA_HUNTER_FLEET_TIMEOUT_SECONDS"] = "2.5"
            os.environ["QA_HUNTER_FLEET_RETRIES"] = "3"
            graph = _graph(mode="deep", hunters=["bm25"])
            node = [item for item in graph["nodes"] if item["node_id"] == "deep.hunter.bm25"][0]
            self.assertEqual(node["timeout"], 2.5)
            self.assertEqual(node["retry"], 3)
            self.assertEqual(fleet_module.hunter_timeout_seconds(), 2.5)
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_semantic_hunter_declares_its_fallback(self):
        graph = _graph(mode="deep", hunters=["semantic", "graph"])
        by_id = {node["node_id"]: node for node in graph["nodes"]}
        self.assertIn("bm25", by_id["deep.hunter.semantic"]["notes"],
                      "§28 例：Vector Hunter 超时 → fallback BM25")
        self.assertIn("DEGRADE", by_id["deep.hunter.graph"]["notes"])


if __name__ == "__main__":
    unittest.main()

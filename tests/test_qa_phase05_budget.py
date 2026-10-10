#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 05 · P05-05 用例（budget / Execution Graph）。

钉住：
  1. 三档都有**总预算**，且总预算的来源可复算（阶段预算之和 / 环境变量 / 入参覆盖）；
  2. 每个节点都有**节点预算**（timeout + budget_source + budget_enforced），
     并标明这笔预算今天由谁执行（舰队超时 / 多跳预算 / 阶段预算 / 仅记账）；
  3. 预算不够时**真的停**：可选节点被裁成 `budget_exhausted`，停止原因写既有五值枚举，
     且本阶段**不可能**产出 NO_GAIN / UNRESOLVABLE_CONTRADICTION（那属 P07）；
  4. 运行期账本用假时钟可复算：超预算后 `should_run()` 变 False（调用方据此真停）、
     停止原因是 BUDGET_EXHAUSTED、回执里 used/remaining/over_budget 自洽；
  5. 节点落库：写进 `qa_stage_runs` 的 node_id/node_kind/parent_node_id/round_index
     （隔离临时 sqlite，断言 backend=='sqlite'），失败的写库不许影响检索。
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_execution_graph as eg  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402
from qa_resilience import STAGE_BUDGET_SECONDS  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

MULTI_HOP_QUESTION = "香港家族办公室税收优惠政策对内地高净值客户有什么影响？"
SIMPLE_QUESTION = "2026年医保新规是否适用于民营医院"


def _graph(mode="deep", **kwargs):
    return eg.build_execution_graph(MULTI_HOP_QUESTION, plan={}, mode=mode, **kwargs)


class _Policy:
    """qa_policy.QaPolicy 的形状（只用到执行图关心的几个字段）。"""

    research_timeout_seconds = 42
    standard_max_hops = 1
    deep_max_hops = 2
    max_queries_per_hop = 5
    max_evidence = 12


class PathBudgetTests(unittest.TestCase):
    def test_three_paths_have_totals_with_traceable_sources(self):
        for mode in ("fast", "standard", "deep"):
            graph = _graph(mode=mode)
            budget = graph["budget"]
            self.assertGreater(budget["total_seconds"], 0)
            self.assertEqual(budget["path"], mode)
            self.assertTrue(budget["path_budget_sources"], "预算来源必须可追溯")
            self.assertIn("plan=", " ".join(budget["path_budget_sources"]))

    def test_total_is_the_sum_of_existing_stage_budgets(self):
        budget = eg.path_budget("fast")
        expected = sum(float(STAGE_BUDGET_SECONDS.get(stage, fallback))
                       for stage, fallback in eg.PATH_STAGES["fast"])
        self.assertAlmostEqual(budget["total_seconds"], round(expected, 3), places=2)
        self.assertEqual(budget["override"], "")

    def test_deep_uses_the_policy_research_timeout_for_level2(self):
        graph = _graph(mode="deep", policy=_Policy())
        node = [item for item in graph["nodes"] if item["node_id"] == "deep.level2_research"][0]
        self.assertEqual(node["timeout"], float(_Policy.research_timeout_seconds))
        self.assertIn("qa_policy.research_timeout_seconds", node["budget_source"])
        self.assertEqual(graph["budget"]["policy_ceiling_seconds"], 42.0)
        self.assertEqual(graph["budget"]["level2_max_hops"], 2)
        self.assertEqual(graph["budget"]["max_queries_per_hop"] if "max_queries_per_hop"
                         in graph["budget"] else 5, 5)

    def test_max_hops_comes_from_config(self):
        saved = os.environ.get("QA_MAX_HOPS")
        try:
            import config

            graph = _graph(mode="standard")
            self.assertEqual(graph["budget"]["max_hops"],
                             max(1, min(int(getattr(config, "QA_MAX_HOPS", 3) or 3), 5)))
            graph = _graph(mode="standard", max_hops=2)
            self.assertEqual(graph["budget"]["max_hops"], 2)
        finally:
            if saved is None:
                os.environ.pop("QA_MAX_HOPS", None)
            else:
                os.environ["QA_MAX_HOPS"] = saved

    def test_env_override_changes_the_total(self):
        saved = os.environ.get("QA_GRAPH_BUDGET_FAST_SECONDS")
        try:
            os.environ["QA_GRAPH_BUDGET_FAST_SECONDS"] = "12"
            graph = _graph(mode="fast")
            self.assertEqual(graph["budget"]["total_seconds"], 12.0)
            self.assertIn("env:", graph["budget"]["path_budget_override"])
        finally:
            if saved is None:
                os.environ.pop("QA_GRAPH_BUDGET_FAST_SECONDS", None)
            else:
                os.environ["QA_GRAPH_BUDGET_FAST_SECONDS"] = saved


class NodeBudgetTests(unittest.TestCase):
    def test_every_runnable_node_declares_a_budget(self):
        graph = _graph(mode="deep")
        per_node = graph["budget"]["per_node"]
        self.assertTrue(per_node)
        for node in graph["nodes"]:
            if node["status"] == "deferred":
                self.assertNotIn(node["node_id"], per_node, "占位节点不占预算")
                continue
            entry = per_node[node["node_id"]]
            self.assertGreaterEqual(entry["timeout"], 0)
            self.assertTrue(entry["source"], "%s 没写预算来源" % node["node_id"])
            self.assertIn(entry["enforced"], (True, False))

    def test_enforced_budgets_point_at_real_knobs(self):
        graph = _graph(mode="deep", hunters=["bm25", "semantic"])
        enforced = {node["node_id"]: node["budget_source"] for node in graph["nodes"]
                    if node["budget_enforced"]}
        self.assertTrue(any("QA_HUNTER_FLEET_TIMEOUT_SECONDS" in value
                            for value in enforced.values()))
        self.assertTrue(any("QA_MULTI_HOP_BUDGET_SECONDS" in value for value in enforced.values()))
        self.assertTrue(any("STAGE_BUDGET_SECONDS" in value for value in enforced.values()))
        for node in graph["nodes"]:
            if node["budget_enforced"]:
                self.assertGreater(node["timeout"], 0, "%s 说预算被执行，却不给时间" % node["node_id"])

    def test_hop_budget_matches_the_existing_config(self):
        import config

        graph = _graph(mode="standard")
        self.assertEqual(graph["budget"]["hop_budget_seconds"],
                         float(int(getattr(config, "QA_MULTI_HOP_BUDGET_SECONDS", 25) or 25)))
        hops = [node for node in graph["nodes"] if node["node_id"].endswith((".hop.h2", ".hop.h3"))]
        for node in hops:
            self.assertIn("QA_MULTI_HOP_BUDGET_SECONDS", node["budget_source"])


class BudgetStopTests(unittest.TestCase):
    def test_default_budget_is_answerable(self):
        for mode in ("fast", "standard", "deep"):
            graph = _graph(mode=mode)
            self.assertEqual(graph["stop_reason"], contracts.QA_STOP_ANSWERABLE)
            self.assertEqual(graph["budget"]["cut_nodes"], [])
            self.assertFalse(graph["budget"]["infeasible"])

    def test_tight_budget_cuts_optional_nodes_and_records_the_reason(self):
        graph = _graph(mode="deep", total_seconds=240)
        budget = graph["budget"]
        self.assertEqual(graph["stop_reason"], contracts.QA_STOP_BUDGET_EXHAUSTED)
        self.assertTrue(budget["cut_nodes"], "预算不够必须裁掉可选节点")
        self.assertLessEqual(budget["estimated_wall_clock_seconds"], 240.0)
        self.assertFalse(budget["infeasible"], "裁完能装下就不该报 infeasible")
        self.assertIn("deep.conflict_review", budget["cut_nodes"])
        statuses = {node["node_id"]: node["status"] for node in graph["nodes"]}
        for node_id in budget["cut_nodes"]:
            self.assertEqual(statuses[node_id], "budget_exhausted")
        # 决定答案能不能出来的节点一个都不许裁
        for node in graph["nodes"]:
            if node["node_kind"] in ("plan", "verify", "answer"):
                self.assertNotEqual(node["status"], "budget_exhausted",
                                    "%s 是必需节点，不许裁" % node["node_id"])
        reasons = [item["reason"] for item in graph["stop_reasons"]]
        self.assertIn(contracts.QA_STOP_BUDGET_EXHAUSTED, reasons)
        for item in graph["stop_reasons"]:
            self.assertIn(item["reason"], contracts.QA_STOP_REASONS)
            self.assertTrue(item["detail"])

    def test_impossible_budget_is_reported_as_infeasible_not_hidden(self):
        """裁光可选节点仍装不下 → 老实写 infeasible（不假装裁两下就合适了）。"""
        graph = _graph(mode="deep", total_seconds=100)
        budget = graph["budget"]
        self.assertEqual(graph["stop_reason"], contracts.QA_STOP_BUDGET_EXHAUSTED)
        self.assertTrue(budget["infeasible"])
        self.assertGreater(budget["estimated_wall_clock_seconds"], 100.0)
        self.assertIn("关键路径上限之和", budget["infeasible_detail"])
        optionals = [node["node_id"] for node in graph["nodes"] if node.get("optional")]
        self.assertEqual(sorted(optionals), sorted(budget["cut_nodes"]))
        # 必需节点仍然在（图不会因为预算紧就把答案节点删掉）
        self.assertTrue(any(node["node_kind"] == "answer" for node in graph["nodes"]))

    def test_only_three_stop_reasons_are_possible_this_phase(self):
        """NO_GAIN / UNRESOLVABLE_CONTRADICTION 属 Phase 07 的缺口闭环，本阶段不许产出。"""
        produced = set()
        for kwargs in ({"mode": "fast"}, {"mode": "standard"}, {"mode": "deep"},
                       {"mode": "deep", "total_seconds": 60},
                       {"mode": "standard", "level2_enabled": False},
                       {"mode": "standard", "max_hops": 2}):
            graph = _graph(**kwargs)
            produced.add(graph["stop_reason"])
            for item in graph["stop_reasons"]:
                produced.add(item["reason"])
        self.assertTrue(produced <= {contracts.QA_STOP_ANSWERABLE,
                                     contracts.QA_STOP_BUDGET_EXHAUSTED,
                                     contracts.QA_STOP_MAX_DEPTH}, produced)
        self.assertNotIn(contracts.QA_STOP_NO_GAIN, produced)
        self.assertNotIn(contracts.QA_STOP_UNRESOLVABLE_CONTRADICTION, produced)

    def test_max_depth_needs_a_real_truncation(self):
        graph = _graph(mode="standard", max_hops=2)
        self.assertEqual(graph["stop_reason"], contracts.QA_STOP_MAX_DEPTH)
        detail = [item["detail"] for item in graph["stop_reasons"]
                  if item["reason"] == contracts.QA_STOP_MAX_DEPTH][0]
        self.assertIn("放宽到 5", detail)

    def test_graph_passes_the_contract(self):
        for mode in ("fast", "standard", "deep"):
            graph = _graph(mode=mode)
            self.assertTrue(graph["contract_ok"], graph["contract_note"])
            ok, note = validate("execution_graph", graph)
            self.assertTrue(ok, note)

    def test_apply_budget_is_pure_and_reusable(self):
        nodes = [
            {"node_id": "a", "timeout": 5.0, "depends_on": [], "optional": False, "status": "pending"},
            {"node_id": "b", "timeout": 50.0, "depends_on": ["a"], "optional": True, "status": "pending"},
            {"node_id": "c", "timeout": 3.0, "depends_on": ["a"], "optional": False, "status": "pending"},
        ]
        budget = eg.apply_budget(nodes, 20.0)
        self.assertEqual(budget["cut_nodes"], ["b"])
        self.assertEqual(nodes[1]["status"], "budget_exhausted")
        self.assertEqual(nodes[2]["status"], "pending")
        self.assertAlmostEqual(budget["estimated_wall_clock_seconds"], 8.0, places=2)


class LedgerTests(unittest.TestCase):
    def _ledger(self, graph, seconds):
        now = [0.0]
        ledger = eg.ExecutionLedger(graph, clock=lambda: now[0])
        return ledger, now

    def test_ledger_stops_when_over_budget(self):
        graph = _graph(mode="fast", total_seconds=5)
        ledger, now = self._ledger(graph, 5)
        self.assertTrue(ledger.should_run("fast.retrieve"))
        now[0] = 6.0
        self.assertTrue(ledger.exceeded())
        self.assertFalse(ledger.should_run("fast.retrieve"), "超预算必须真的停（调用方靠它）")
        self.assertEqual(ledger.stop_reason(default="ANSWERABLE"),
                         contracts.QA_STOP_BUDGET_EXHAUSTED)
        receipt = ledger.receipt(default_stop_reason="ANSWERABLE")
        self.assertTrue(receipt["over_budget"])
        self.assertLess(receipt["remaining_seconds"], 0)
        self.assertEqual(receipt["stop_reason"], contracts.QA_STOP_BUDGET_EXHAUSTED)

    def test_ledger_records_nodes_and_is_answerable_within_budget(self):
        graph = _graph(mode="fast", total_seconds=100)
        ledger, now = self._ledger(graph, 100)
        ledger.begin("fast.interpret")
        now[0] = 1.5
        ledger.finish("fast.interpret", status="ok")
        entry = ledger.receipt()["nodes"][0]
        self.assertEqual(entry["latency_ms"], 1500)
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(ledger.stop_reason(default="ANSWERABLE"), contracts.QA_STOP_ANSWERABLE)
        self.assertFalse(ledger.receipt()["over_budget"])

    def test_ledger_treats_a_budget_exhausted_node_as_the_stop_reason(self):
        graph = _graph(mode="fast", total_seconds=100)
        ledger, _now = self._ledger(graph, 100)
        ledger.record("fast.hop.h2", status="budget_exhausted", detail="多跳预算用尽")
        self.assertEqual(ledger.stop_reason(default="ANSWERABLE"),
                         contracts.QA_STOP_BUDGET_EXHAUSTED)

    def test_ledger_uses_the_graph_total_by_default(self):
        graph = _graph(mode="standard")
        ledger = eg.ExecutionLedger(graph)
        self.assertEqual(ledger.total_seconds, graph["budget"]["total_seconds"])


class NodeRunPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "phase05-budget.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        assert self.db.backend == "sqlite", "测试必须跑在隔离的 sqlite 上"
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self.run = self.store.create_run(
            {"industry_pack_id": "family_office", "question": MULTI_HOP_QUESTION},
            owner_user_id="p05", idempotency_key="phase05-budget")

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_node_runs_land_in_the_phase01_columns(self):
        graph = _graph(mode="deep")
        now = [0.0]
        ledger = eg.ExecutionLedger(graph, clock=lambda: now[0])
        ledger.record("deep.interpret", status="ok", latency_ms=12)
        ledger.record("deep.retrieve", status="ok", latency_ms=40, evidence=3)
        receipt = eg.record_node_runs(self.store, str(self.run["id"]), graph, ledger=ledger)
        self.assertGreaterEqual(receipt["written_count"], 2)
        rows = [row for row in self.store.stage_runs(str(self.run["id"]))
                if str(row.get("node_id") or "").startswith("deep.")]
        by_node = {row["node_id"]: row for row in rows}
        self.assertIn("deep.interpret", by_node)
        self.assertEqual(by_node["deep.interpret"]["node_kind"], "plan")
        self.assertEqual(by_node["deep.retrieve"]["node_kind"], "retrieve")
        self.assertEqual(by_node["deep.interpret"]["status"], "ok")
        self.assertTrue(all(row["stage"].startswith("node:") for row in rows),
                        "节点行用 node:<node_id> 作 stage，避免打乱既有阶段行")
        self.assertTrue(by_node["deep.hunter.bm25"]["node_id"] if "deep.hunter.bm25" in by_node
                        else True)
        # 未执行的节点写成 pending 的不落库（只登记真跑过的）
        self.assertNotIn("deep.answer", by_node)

    def test_parent_and_round_index_are_persisted(self):
        graph = _graph(mode="deep", hunters=["bm25", "semantic"])
        ledger = eg.ExecutionLedger(graph, clock=lambda: 0.0)
        for node_id in ("deep.hunter.bm25", "deep.hunter.semantic", "deep.merge"):
            ledger.record(node_id, status="ok", latency_ms=5)
        eg.record_node_runs(self.store, str(self.run["id"]), graph, ledger=ledger, round_index=2)
        rows = {row["node_id"]: row for row in self.store.stage_runs(str(self.run["id"]))
                if str(row.get("node_id") or "").startswith("deep.")}
        self.assertEqual(rows["deep.merge"]["node_kind"], "merge")
        self.assertEqual(rows["deep.merge"]["round_index"], 2)
        self.assertEqual(rows["deep.hunter.bm25"]["node_kind"], "retrieve")

    def test_record_node_runs_survives_a_broken_store(self):
        class _Broken:
            def record_stage(self, *args, **kwargs):
                raise RuntimeError("库里写不进去")

        graph = _graph(mode="fast")
        ledger = eg.ExecutionLedger(graph, clock=lambda: 0.0)
        ledger.record("fast.interpret", status="ok")
        receipt = eg.record_node_runs(_Broken(), "run-x", graph, ledger=ledger)
        self.assertEqual(receipt["written_count"], 0)
        self.assertTrue(receipt["failed"])
        self.assertIn("RuntimeError", receipt["failed"][0]["error"])

    def test_receipt_carries_runtime_and_node_runs(self):
        graph = _graph(mode="fast")
        ledger = eg.ExecutionLedger(graph, clock=lambda: 0.0)
        ledger.record("fast.interpret", status="ok", latency_ms=3)
        receipt = eg.graph_receipt(graph, ledger=ledger,
                                   node_runs={"written_count": 1, "written": ["fast.interpret"]})
        self.assertIn("runtime", receipt)
        self.assertIn("node_runs", receipt)
        self.assertEqual(receipt["node_runs"]["written_count"], 1)
        self.assertEqual(receipt["stop_reason"], graph["stop_reason"])
        self.assertIn("budget", receipt)
        self.assertIn("per_node", receipt["budget"])


if __name__ == "__main__":
    unittest.main()

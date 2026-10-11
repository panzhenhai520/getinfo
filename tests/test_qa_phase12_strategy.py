#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 12 · P12-03 `Strategy Memory`（含 `QUERY_PATTERN`）。

钉住：
  1. §3.4 的"检索组合有效率"口径**写死且可复算**：成功 ⟺ 那一次该通道至少产出 1 条
     Phase 03 判 SUPPORTED 的证据；样本少于门槛不参与；组合 = 有效率 ≥ 下限的通道；
  2. **只统计真的试过的通道**（`attempted`），没跑过的通道不许进分母；
  3. 组合成功率的分母 = "这些通道全都被试过"的那些轮（可比较样本），不是全部轮；
  4. 查询形态（`QUERY_PATTERN`）是可复算的特征元组，与 `INVALID_QUERY` 失败互为对偶；
  5. 策略/形态记忆：绑真实证据、`HELPED_RESOLVE` 指向真实 run、样本门槛/下限可配；
  6. 真机式场景：同一任务类型下同一通道有的轮有核验产出、有的轮没有 → 有效率 < 1 时
     仍然可以成为策略（≥ 下限），但样本不足时一条都不产。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_graph_contracts as contracts  # noqa: E402
import qa_memory_experience as exp  # noqa: E402

import qa_phase12_fixtures as fx  # noqa: E402


def _runs(*, good=2, bad=1, task_type="SIMPLE_FACT", routes=("keyword",)):
    """造 good+bad 轮：good 轮该通道有已核验产出，bad 轮零产出。"""
    traces = []
    for index in range(good):
        traces.append(fx.trace(run_id="good-%d" % index,
                               graph=fx.graph_with(refs=("article:%d" % (index + 1),),
                                                   routes=routes),
                               task_type=task_type,
                               gap_receipt=fx.gap_receipt()))
    for index in range(bad):
        empty = {"version": "qa-evidence-graph-v1", "claims": [], "evidence": [], "edges": [],
                 "conflicts": [], "stats": {}}
        traces.append(fx.trace(run_id="bad-%d" % index, graph=empty,
                               level1_receipt=fx.level1(adopted=0),
                               task_type=task_type, gap_receipt=fx.gap_receipt()))
    return traces


class RouteTableTests(unittest.TestCase):
    def test_route_rows_count_only_attempted_successes(self):
        rows = exp.strategy_rows(_runs(good=2, bad=1), min_samples=2)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["task_type"], "SIMPLE_FACT")
        self.assertEqual(row["combination"], ["keyword"])
        detail = row["routes_detail"]["keyword"]
        self.assertEqual(detail["observations"], 3)
        self.assertEqual(detail["successes"], 2)
        self.assertAlmostEqual(detail["success_rate"], 2 / 3, places=6)
        # 组合样本 = 三条通道都被试过的轮 = 3（这里只有一条通道，全部可比）
        self.assertEqual(row["observations"], 3)
        self.assertEqual(row["successes"], 2)
        ok, note = contracts.validate("strategy_memory", row)
        self.assertTrue(ok, note)

    def test_min_samples_gate(self):
        self.assertEqual(exp.strategy_rows(_runs(good=1, bad=0), min_samples=2), [],
                         "一次观测不许当规律")
        self.assertEqual(len(exp.strategy_rows(_runs(good=1, bad=0), min_samples=1)), 1)

    def test_success_floor_is_configurable(self):
        traces = _runs(good=1, bad=2)          # 有效率 1/3
        self.assertEqual(exp.strategy_rows(traces, min_samples=2), [],
                         "1/3 < 默认下限 0.6 → 不成策略")
        self.assertEqual(len(exp.strategy_rows(traces, min_samples=2, success_floor=0.3)), 1)

    def test_unattempted_routes_never_enter_the_table(self):
        trace = fx.trace(level1_receipt=fx.level1(web_adopted=5, graph_adopted=0))
        table = exp._route_table([trace])
        self.assertEqual(sorted(route for _task, route in table), ["graph", "keyword", "web"])
        off = fx.trace(level1_receipt=fx.level1(web_adopted=5, search_status="disabled"))
        table = exp._route_table([off])
        self.assertNotIn("web", {route for _task, route in table})

    def test_task_types_are_separated(self):
        rows = exp.strategy_rows(_runs(good=2, task_type="SIMPLE_FACT")
                                 + _runs(good=2, task_type="COMPARISON"), min_samples=2)
        self.assertEqual([row["task_type"] for row in rows], ["COMPARISON", "SIMPLE_FACT"])

    def test_deterministic(self):
        first = exp.strategy_rows(_runs(good=2, bad=1), min_samples=2)
        second = exp.strategy_rows(_runs(good=2, bad=1), min_samples=2)
        self.assertEqual([exp.strategy_content(row) for row in first],
                         [exp.strategy_content(row) for row in second])


class QueryPatternTests(unittest.TestCase):
    def test_pattern_is_a_recomputable_feature_tuple(self):
        pattern = exp.query_pattern_of(fx.QUESTION)
        self.assertIn("entity=1", pattern)
        self.assertIn("question=1", pattern)
        self.assertIn("len=", pattern)
        self.assertEqual(pattern, exp.query_pattern_of(fx.QUESTION))
        self.assertEqual(exp.query_pattern_of(""), "")

    def test_features_react_to_real_text(self):
        short = exp.query_pattern_of("宁德时代股价")
        self.assertIn("len=SHORT", short)
        compare = exp.query_pattern_of("A 与 B 相比有哪些区别？2026 年最新")
        self.assertIn("compare=1", compare)
        self.assertIn("time=1", compare)

    def test_candidates_are_gated_by_samples(self):
        traces = _runs(good=2, bad=1, task_type="SIMPLE_FACT")
        rows = exp.query_pattern_candidates(traces, min_samples=2)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["samples"], 3)
        self.assertEqual(row["sample_floor"], 2)
        self.assertEqual(row["metadata"]["successes"], 2)
        self.assertIn("实例提问", row["canonical_content"])
        self.assertEqual(exp.query_pattern_candidates(traces, min_samples=9), [])

    def test_pattern_memory_binds_run_and_evidence(self):
        traces = _runs(good=2, bad=1, task_type="SIMPLE_FACT")
        candidates = exp.strategy_candidates(traces, min_samples=2) + \
            exp.query_pattern_candidates(traces, min_samples=2)
        by_type = {row["memory_type"]: row for row in candidates}
        for name in ("STRATEGY", "QUERY_PATTERN"):
            row = by_type[name]
            self.assertTrue(row["evidence"], "%s 必须绑真实证据" % name)
            for link in row["evidence"]:
                self.assertTrue(link["source_fingerprint"])
            self.assertTrue(row["trace_ref"])
        strategy = by_type["STRATEGY"]
        self.assertEqual([item["relation"] for item in strategy["relations"]],
                         ["HELPED_RESOLVE"] * len(strategy["relations"]))
        self.assertTrue({"good-0", "good-1"} <= {item["target_ref"]
                                                for item in strategy["relations"]})


class StrategyWriteTests(unittest.TestCase):
    def test_strategy_and_pattern_land_and_are_recallable(self):
        traces = _runs(good=2, bad=1)
        with fx.temp_store("p12-strategy.sqlite3") as (_database, store):
            receipt = exp.write_experience_memories(store, traces=traces, run_meta=fx.run_meta())
            self.assertEqual(receipt["persisted_by_type"].get("STRATEGY"), 1)
            self.assertEqual(receipt["persisted_by_type"].get("QUERY_PATTERN"), 1)
            for name in ("STRATEGY", "QUERY_PATTERN"):
                items = store.load_memory_items(memory_types=[name], include_all_scopes=True)
                self.assertEqual(len(items), 1, name)
                links = store.memory_evidence(memory_ids=[items[0]["memory_id"]])
                self.assertTrue(links, name)
                self.assertTrue(links[0]["source_fingerprint"], name)
            items = store.load_memory_items(memory_types=["STRATEGY"], include_all_scopes=True)
            self.assertIn("有效率", items[0]["canonical_content"])
            self.assertEqual(items[0]["metadata"]["combination"], ["keyword"])
            self.assertIs(items[0]["metadata"]["hint_only"], True)

    def test_planning_recall_finds_the_strategy_by_topic(self):
        """规划期召回：用同一话题的问题能召回这条经验（否则就是死数据）。"""
        traces = _runs(good=2, bad=1)
        with fx.temp_store("p12-strategy-recall.sqlite3") as (_database, store):
            exp.write_experience_memories(store, traces=traces, run_meta=fx.run_meta())
            from qa_memory import recall
            from qa_verifier import term_set

            hits = recall(store, mode="planning", query=fx.QUESTION,
                          terms=term_set(fx.QUESTION, sizes=(2, 3)),
                          owner_user_id="p12", session_id="s1", industry_pack_id="auto")
            types = {hit["memory_type"] for hit in hits["hits"]}
            self.assertTrue(types & {"STRATEGY", "QUERY_PATTERN"},
                            "规划期召回至少要能看见这两类经验：%s" % types)


if __name__ == "__main__":
    unittest.main()

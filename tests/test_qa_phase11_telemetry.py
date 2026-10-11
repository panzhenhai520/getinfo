#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 11 · P11-06（performance telemetry）用例。

钉住：
  1. **成功率口径写死且可复算**：成功 ⟺ `outcome=='ok'` 且 `stage_reached` 是
     `included_in_pack`/`executed`；只被路由选中（`routed`）**不算成功**；
  2. **样本不足不给成功率**：`attempts < min_samples` → `success_rate is None`
     + `INSUFFICIENT_SAMPLES`（不许拿 1 次成功当 100%），且 Router 也不会拿它当历史；
  3. 分位数用**最近秩法**且取自真实观测值（不插值、不编数）；
  4. `stage_reached`/`outcome` 是从**真实结果**推出来的：指令进包了没有、该通道这一轮
     到底产出了几条证据 —— 不是声明值；
  5. **落库零迁移**：只写既有 `qa_stage_runs`（`node_kind='skill'`），
     幂等（同 run+skill+attempt 覆盖）、开关默认关、读回的值与写入的一致；
  6. 遥测失败绝不打断主链路（坏 store 只记错误）。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_graph_contracts as contracts  # noqa: E402
import qa_skills as skills  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402

import qa_phase11_fixtures as fx  # noqa: E402


def _pack_with_skills(loaded):
    return {"context_pack": {"sections": {"skill_context": {"loaded_skills": list(loaded)}}}}


class SuccessDefinitionTests(unittest.TestCase):
    def test_success_requires_ok_and_a_real_stage(self):
        self.assertTrue(skills.is_skill_success(fx.record(stage="included_in_pack", outcome="ok")))
        self.assertTrue(skills.is_skill_success(fx.record(stage="executed", outcome="ok")))
        self.assertFalse(skills.is_skill_success(fx.record(stage="routed", outcome="ok")))
        self.assertFalse(skills.is_skill_success(fx.record(stage="instruction_built",
                                                          outcome="ok")))
        self.assertFalse(skills.is_skill_success(fx.record(stage="included_in_pack",
                                                          outcome="degraded")))
        self.assertFalse(skills.is_skill_success(fx.record(stage="included_in_pack",
                                                          outcome="error")))

    def test_selected_but_not_packed_is_not_a_success(self):
        """Router 选中 ≠ 生成端看得见：这是本阶段最容易做假的地方，单独钉一条。"""
        rows = [fx.record(stage="routed", outcome="ok") for _ in range(5)]
        summary = skills.performance_summary(rows)
        self.assertEqual(summary["attempts"], 5)
        self.assertEqual(summary["successes"], 0)
        self.assertEqual(summary["success_rate"], 0.0)

    def test_definition_string_is_exported(self):
        summary = skills.performance_summary(fx.records_for())
        self.assertEqual(summary["success_definition"],
                         "outcome=='ok' 且 stage_reached∈{included_in_pack,executed}")


class SuccessRateTests(unittest.TestCase):
    def test_hand_computed_rate(self):
        rows = fx.records_for(outcomes=("ok", "ok", "degraded"))
        summary = skills.performance_summary(rows)
        self.assertEqual(summary["attempts"], 3)
        self.assertEqual(summary["successes"], 2)
        self.assertEqual(summary["success_rate"], round(2 / 3, 6))
        self.assertEqual(summary["degraded"], 1)
        self.assertEqual(summary["reason"], "")

    def test_insufficient_samples_gives_none(self):
        for count in (1, 2):
            rows = fx.records_for(outcomes=("ok",) * count)
            summary = skills.performance_summary(rows)
            self.assertEqual(summary["attempts"], count)
            self.assertIsNone(summary["success_rate"], "%d 次样本不许给成功率" % count)
            self.assertEqual(summary["reason"], "INSUFFICIENT_SAMPLES")

    def test_threshold_is_configurable_via_argument(self):
        rows = fx.records_for(outcomes=("ok",))
        self.assertEqual(skills.performance_summary(rows, min_samples=1)["success_rate"], 1.0)
        self.assertIsNone(skills.performance_summary(rows, min_samples=5)["success_rate"])

    def test_empty_records_marked_honestly(self):
        summary = skills.performance_summary([])
        self.assertEqual(summary["attempts"], 0)
        self.assertIsNone(summary["success_rate"])
        self.assertEqual(summary["reason"], "EMPTY_RECORDS")

    def test_skill_and_task_type_filters(self):
        rows = fx.records_for("bm25_search", outcomes=("ok", "ok", "ok"), task_type="CAUSAL")
        rows += fx.records_for("semantic_search", outcomes=("degraded",) * 3,
                               task_type="CAUSAL")
        summary = skills.performance_summary(rows, skill_id="semantic_search")
        self.assertEqual(summary["attempts"], 3)
        self.assertEqual(summary["successes"], 0)
        summary = skills.performance_summary(rows, task_type="TEMPORAL")
        self.assertEqual(summary["attempts"], 0)


class LatencyAndCostTests(unittest.TestCase):
    def test_quantiles_use_nearest_rank_and_real_values(self):
        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        rows = [fx.record(latency_ms=value, attempt=index + 1)
                for index, value in enumerate(values)]
        stats = skills.performance_summary(rows)["latency_ms"]
        self.assertEqual(stats["count"], 5)
        self.assertEqual(stats["min"], 10.0)
        self.assertEqual(stats["max"], 50.0)
        self.assertEqual(stats["p50"], 30.0)
        self.assertEqual(stats["p90"], 50.0)
        self.assertEqual(stats["mean"], 30.0)
        self.assertIn(stats["p50"], values)
        self.assertIn(stats["p90"], values)
        self.assertEqual(stats["method"], "nearest_rank")

    def test_even_count_quantile_is_an_observed_value(self):
        rows = [fx.record(latency_ms=value, attempt=index + 1)
                for index, value in enumerate([1.0, 2.0, 3.0, 100.0])]
        stats = skills.performance_summary(rows)["latency_ms"]
        self.assertIn(stats["p50"], (2.0, 3.0))

    def test_cost_units_come_from_the_declared_class(self):
        rows = [fx.record("semantic_search", attempt=1), fx.record("semantic_search", attempt=2)]
        self.assertEqual(skills.performance_summary(rows)["cost_units"], 2.0)
        rows = [fx.record("causal_reasoning", attempt=1)]
        self.assertEqual(skills.performance_summary(rows)["cost_units"], 0.0)

    def test_evidence_yield_sums(self):
        rows = [fx.record(evidence_yield=3, attempt=1), fx.record(evidence_yield=2, attempt=2)]
        self.assertEqual(skills.performance_summary(rows)["evidence_yield"], 5)

    def test_verified_yield_is_counted_separately_from_evidence_yield(self):
        """Phase 03 对齐：取回候选 ≠ 取回**已验证**候选，两个数必须分开。"""
        rows = [fx.record(evidence_yield=5, verified_yield=2, attempt=1),
                fx.record(evidence_yield=4, verified_yield=0, attempt=2),
                fx.record(evidence_yield=1, verified_yield=1, attempt=3)]
        summary = skills.performance_summary(rows)
        self.assertEqual(summary["evidence_yield"], 10)
        self.assertEqual(summary["verified_yield"], 3)
        self.assertLess(summary["verified_yield"], summary["evidence_yield"])

    def test_zero_latency_is_not_faked(self):
        # 没有实测延迟就写 0，分位数只统计真实观测到的那些值（不许编一个"典型值"）
        rows = [fx.record(latency_ms=0.0, attempt=1)] * 3
        stats = skills.performance_summary(rows)["latency_ms"]
        self.assertEqual(stats["max"], 0.0)
        self.assertEqual(stats["mean"], 0.0)


class PerformanceTableTests(unittest.TestCase):
    def test_table_is_keyed_by_skill_in_spec_order(self):
        rows = (fx.records_for("semantic_search") + fx.records_for("bm25_search"))
        table = skills.performance_table(rows)
        self.assertEqual(list(table), ["bm25_search", "semantic_search"])
        self.assertEqual(table["bm25_search"]["skill_id"], "bm25_search")
        for row in table.values():
            self.assertEqual(row["min_samples"], contracts.DEFAULT_SKILL_MIN_SAMPLES)

    def test_table_feeds_the_router(self):
        rows = fx.records_for("bm25_search", outcomes=("degraded",) * 4)
        table = skills.performance_table(rows)
        routing = skills.route_skills(gaps=[fx.gap(routes=("keyword", "semantic"))],
                                     performance=table)
        self.assertEqual(routing["selected"], ["semantic_search"])

    def test_receipt_is_recomputable(self):
        rows = fx.records_for("bm25_search") + fx.records_for("semantic_search",
                                                             outcomes=("degraded",) * 3)
        routing = skills.route_skills(gaps=[fx.gap(routes=("keyword",))])
        receipt = skills.telemetry_receipt(rows, routing=routing)
        self.assertEqual(receipt["records"], len(rows))
        self.assertEqual(receipt["skills_covered"], ["bm25_search", "semantic_search"])
        self.assertEqual(sum(receipt["stages"].values()), len(rows))
        self.assertEqual(sum(receipt["outcomes"].values()), len(rows))
        self.assertEqual(receipt["routing"]["selected_skills"], routing["selected"])
        self.assertEqual(receipt["success_definition"],
                         "outcome=='ok' 且 stage_reached∈{included_in_pack,executed}")

    def test_record_validates_against_the_contract(self):
        row = fx.record()
        ok, why = validate("skill_load_record", row)
        self.assertTrue(ok, why)
        ok, why = validate("skill_performance",
                           skills.performance_summary(fx.records_for(), skill_id="bm25_search"))
        self.assertTrue(ok, why)
        # 跨技能聚合行没有 skill_id（契约里它是可选字段，取值允许空串）
        ok, why = validate("skill_performance", skills.performance_summary(fx.records_for()))
        self.assertTrue(ok, why)
        ok, why = validate("skill_performance",
                           skills.performance_summary(fx.records_for(), skill_id="not_a_skill"))
        self.assertFalse(ok)

    def test_illegal_stage_and_outcome_are_clamped_and_flagged(self):
        row = skills.load_record(skill_id="bm25_search", stage_reached="teleported",
                                 outcome="perfect")
        self.assertEqual(row["stage_reached"], "routed")
        self.assertEqual(row["outcome"], "skipped")
        self.assertTrue(row["outcome_clamped"])
        ok, why = validate("skill_load_record", row)
        self.assertTrue(ok, why)
        self.assertFalse(skills.is_skill_success(row))

    def test_legal_values_are_not_flagged(self):
        self.assertFalse(fx.record()["outcome_clamped"])

    def test_record_id_is_content_addressed(self):
        first = fx.record()
        second = fx.record()
        self.assertEqual(first["record_id"], second["record_id"])
        third = fx.record(attempt=2)
        self.assertNotEqual(first["record_id"], third["record_id"])


class RealOutcomeTests(unittest.TestCase):
    def _routing(self):
        return skills.route_skills(gaps=[fx.gap(routes=("keyword",))], task_type="CAUSAL")

    def test_skills_in_the_pack_are_ok_and_routed_only_are_degraded(self):
        routing = self._routing()
        graph = _pack_with_skills(routing["selected"][:1])
        records = skills.telemetry_records_from_routing(routing, graph=graph, run_id="r1",
                                                        task_type="CAUSAL")
        by_skill = {row["skill_id"]: row for row in records}
        self.assertEqual(by_skill[routing["selected"][0]]["outcome"], "ok")
        self.assertEqual(by_skill[routing["selected"][0]]["stage_reached"], "included_in_pack")
        others = [name for name in routing["selected"] if name != routing["selected"][0]]
        for name in others:
            self.assertEqual(by_skill[name]["outcome"], "degraded")
            self.assertEqual(by_skill[name]["stage_reached"], "instruction_built")

    def test_evidence_yield_is_counted_per_route(self):
        routing = self._routing()
        graph = _pack_with_skills(["bm25_search"])
        graph.update(fx.graph_with_evidence("keyword", "keyword", "semantic"))
        records = skills.telemetry_records_from_routing(routing, graph=graph, run_id="r1")
        row = [item for item in records if item["skill_id"] == "bm25_search"][0]
        self.assertEqual(row["evidence_yield"], 2)
        self.assertEqual(row["stage_reached"], "executed")

    def test_route_without_evidence_stays_included_in_pack(self):
        routing = self._routing()
        graph = _pack_with_skills(["bm25_search"])
        graph.update(fx.graph_with_evidence("semantic"))
        records = skills.telemetry_records_from_routing(routing, graph=graph, run_id="r1")
        row = [item for item in records if item["skill_id"] == "bm25_search"][0]
        self.assertEqual(row["evidence_yield"], 0)
        self.assertEqual(row["stage_reached"], "included_in_pack")

    def test_explicit_latency_is_used_verbatim(self):
        routing = self._routing()
        records = skills.telemetry_records_from_routing(
            routing, graph=_pack_with_skills(routing["selected"]), run_id="r1",
            late_latency_ms={routing["selected"][0]: 1234.5})
        row = [item for item in records if item["skill_id"] == routing["selected"][0]][0]
        self.assertEqual(row["latency_ms"], 1234.5)

    def test_reasoning_skills_never_claim_evidence_yield(self):
        routing = skills.route_skills(task_type="CAUSAL")
        graph = _pack_with_skills(["causal_reasoning"])
        graph.update(fx.graph_with_evidence("keyword"))
        records = skills.telemetry_records_from_routing(routing, graph=graph, run_id="r1")
        row = records[0]
        self.assertEqual(row["skill_id"], "causal_reasoning")
        self.assertEqual(row["evidence_yield"], 0)
        self.assertEqual(row["verified_yield"], 0)
        self.assertEqual(row["outcome"], "ok")

    def test_verified_yield_counts_only_phase03_supported(self):
        """`verified_yield` 只数被判 SUPPORTED 的（判据来自 Phase 03 的核验结论读取口）。"""
        from qa_evidence import annotate_evidence
        from qa_verifier import verify_evidence_batch

        routing = self._routing()
        annotated = annotate_evidence(
            {"evidence_ref": "article:1", "source_type": "article",
             "title": "香港家族办公室税收优惠政策解读",
             "source_url": "https://example.com/1",
             "content_excerpt": fx.EVIDENCE_TEXT, "score": 30.0,
             "retrieval_method": "keyword", "relationship": "supports",
             "published_at": "2026-10-09", "authority_level": 60},
            terms=["家族办公室", "税收优惠", "利得税宽免"], run_id="r1",
            stage="level1_retrieval", route="keyword", corpus_version="c1")
        kept, _audit = verify_evidence_batch(
            [annotated], claim_text=fx.CLAIM_TEXT,
            terms=["家族办公室", "税收优惠", "利得税宽免"], gate="all")
        verdict = (kept[0].get("metadata", {}).get("evidence_layer", {})
                   .get("verification", {}) or {}).get("verdict")
        graph = _pack_with_skills(["bm25_search"])
        graph.update({"evidence": kept, "claims": [], "edges": []})
        row = skills.telemetry_records_from_routing(routing, graph=graph, run_id="r1")[0]
        self.assertEqual(row["evidence_yield"], 1)
        self.assertEqual(row["verified_yield"], 1 if verdict == "SUPPORTED" else 0)
        # 判不动的证据不会让 verified_yield 虚高
        self.assertLessEqual(row["verified_yield"], row["evidence_yield"])


class StorageTests(unittest.TestCase):
    def test_missing_skill_id_is_rejected_without_writing(self):
        with fx.temp_store("phase11-telemetry.sqlite3") as (database, store):
            self.assertEqual(database.backend, "sqlite")
            result = store.record_skill_load("run-p11", {"outcome": "ok"})
            self.assertFalse(result["ok"])
            self.assertEqual(store.skill_load_rows(run_id="run-p11"), [])

    def test_round_trip_is_verbatim_and_zero_migration(self):
        with fx.temp_store("phase11-telemetry.sqlite3") as (database, store):
            self.assertEqual(database.backend, "sqlite")
            record = fx.record(skill_id="semantic_search", latency_ms=777.5, cost_units=1.0,
                               evidence_yield=4, task_type="COMPARISON")
            result = store.record_skill_load("run-p11", record)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["stage"], "skill:semantic_search")
            rows = store.skill_load_rows(run_id="run-p11")
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["skill_id"], "semantic_search")
            self.assertEqual(row["stage"], "skill:semantic_search")
            self.assertEqual(row["stage_reached"], "included_in_pack")
            self.assertEqual(row["outcome"], "ok")
            self.assertEqual(row["latency_ms"], 777.5)
            self.assertEqual(row["cost_units"], 1.0)
            self.assertEqual(row["evidence_yield"], 4)
            self.assertEqual(row["task_type"], "COMPARISON")
            self.assertEqual(row["status"], "completed")
            # 只有既有表：一行新表都没建
            tables = {str(item[0]) for item in database.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertTrue({"qa_stage_runs"} <= tables if tables else True)
            self.assertFalse([name for name in tables if name.startswith("skill")])

    def test_status_mapping_covers_all_outcomes(self):
        expected = {"ok": "completed", "degraded": "degraded", "error": "failed",
                    "skipped": "skipped"}
        with fx.temp_store("phase11-status.sqlite3") as (database, store):
            for index, outcome in enumerate(sorted(expected)):
                record = fx.record(skill_id="bm25_search", outcome=outcome,
                                   stage="instruction_built", attempt=index + 1)
                self.assertTrue(store.record_skill_load("run-p11", record)["ok"])
            rows = store.skill_load_rows(run_id="run-p11", skill_ids=["bm25_search"])
            self.assertEqual(len(rows), len(expected))
            for row in rows:
                self.assertEqual(row["status"], expected[row["outcome"]])

    def test_idempotent_same_run_skill_attempt(self):
        with fx.temp_store("phase11-idempotent.sqlite3") as (database, store):
            first = fx.record(latency_ms=100.0)
            second = dict(fx.record(latency_ms=250.0), record_id=first["record_id"])
            store.record_skill_load("run-p11", first)
            store.record_skill_load("run-p11", second)
            rows = store.skill_load_rows(run_id="run-p11")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["latency_ms"], 250.0)

    def test_different_runs_are_separate_rows(self):
        with fx.temp_store("phase11-runs.sqlite3") as (database, store):
            store.record_skill_load("run-a", fx.record())
            store.record_skill_load("run-b", fx.record())
            self.assertEqual(len(store.skill_load_rows()), 2)
            self.assertEqual(len(store.skill_load_rows(run_id="run-a")), 1)

    def test_filter_by_skill_ids(self):
        with fx.temp_store("phase11-filter.sqlite3") as (database, store):
            store.record_skill_load("run-p11", fx.record("bm25_search"))
            store.record_skill_load("run-p11", fx.record("semantic_search"))
            rows = store.skill_load_rows(skill_ids=["semantic_search"])
            self.assertEqual([row["skill_id"] for row in rows], ["semantic_search"])

    def test_rows_feed_the_performance_table(self):
        with fx.temp_store("phase11-perf.sqlite3") as (database, store):
            store.record_skill_load("run-p11", fx.record(attempt=1))
            for attempt in (2, 3, 4):
                store.record_skill_load("run-p11", fx.record(attempt=attempt, outcome="degraded",
                                                             stage="instruction_built"))
            rows = store.skill_load_rows()
            self.assertEqual(len(rows), 4)
            table = skills.performance_table(rows)
            self.assertEqual(table["bm25_search"]["attempts"], 4)
            self.assertEqual(table["bm25_search"]["successes"], 1)
            self.assertEqual(table["bm25_search"]["success_rate"], 0.25)

    def test_stats_grouping(self):
        with fx.temp_store("phase11-stats.sqlite3") as (database, store):
            store.record_skill_load("run-p11", fx.record("bm25_search", attempt=1))
            store.record_skill_load("run-p11", fx.record("bm25_search", attempt=2,
                                                         outcome="error", stage="routed"))
            stats = store.skill_telemetry_stats(run_id="run-p11")
            self.assertEqual(stats["records"], 2)
            self.assertEqual(stats["by_skill"], {"bm25_search": 2})
            self.assertEqual(stats["by_outcome"], {"ok": 1, "error": 1})
            self.assertIn("success_definition", stats)

    def test_broken_store_records_the_error_instead_of_raising(self):
        class Broken:
            """连 `_ensure_connection` 都没有的坏 store（最坏情况）。"""

            def record_stage(self, *args, **kwargs):
                raise RuntimeError("库坏了")

        from qa_storage import QaStore

        store = QaStore(Broken())
        result = store.record_skill_load("run-p11", fx.record())
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"], "失败必须带错误说明，不许静默")
        self.assertIn("Error", result["error"])
        # 读路径同样不抛（遥测读不到就当没有，不编数据）
        self.assertEqual(store.skill_load_rows(run_id="run-p11"), [])
        self.assertEqual(store.skill_telemetry_stats(run_id="run-p11")["records"], 0)

    def test_no_wall_clock_leak_into_the_typed_values(self):
        with fx.temp_store("phase11-clock.sqlite3") as (database, store):
            record = fx.record(latency_ms=42.0)
            store.record_skill_load("run-p11", record)
            row = store.skill_load_rows(run_id="run-p11")[0]
            # 显式观测值原样保留；列上的墙上钟是另一列，绝不互相顶替
            self.assertEqual(row["latency_ms"], 42.0)
            self.assertIsInstance(row["wall_latency_ms"], (int, float))


if __name__ == "__main__":
    unittest.main()

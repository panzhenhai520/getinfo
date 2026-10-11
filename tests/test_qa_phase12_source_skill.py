#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 12 · P12-04 `Source reliability` + P12-05 `Skill performance memory`。

钉住：
  1. §3.5 的**六个因子**逐项可复算（权威/已核验产出/被拒率/时效/多来源印证/样本量），
     可靠性是它们的加权和（权重写死）；样本不足一律 `UNKNOWN`（不猜档位）；
  2. 已知局限只写"能从数据看出来"的（样本不足 / 有被反驳证据 / 权威等级缺失 /
     发布时间缺失 / 更新频率未知），不编；
  3. 跨轮聚合用**总计数**重算已核验率与被拒率（不是多轮均值）；
  4. §3.6 技能表现记忆的**成功率口径零重定义**：`success_definition` 与 Phase 11 的
     `qa_skills.performance_summary` 逐字相同，样本不足不写成功率；
  5. 两张投影表（来源/技能）真的被写，且 `memory_id` 与记忆本体**内容寻址一致**；
  6. 样本不足的来源/技能在写门被如实拒收（`BELOW_SAMPLE_FLOOR`）并记账。
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
import qa_skills as skills  # noqa: E402

import qa_phase12_fixtures as fx  # noqa: E402


class SourceFactorTests(unittest.TestCase):
    def test_authority_score_is_monotone_and_handles_missing(self):
        self.assertEqual(exp.authority_score(1), 1.0)
        self.assertEqual(exp.authority_score(10), 0.85)
        self.assertEqual(exp.authority_score(60), 0.55)
        self.assertEqual(exp.authority_score(999), 0.40)
        self.assertEqual(exp.authority_score(None), 0.50, "权威等级缺失给中性值，不猜")
        self.assertEqual(exp.authority_score(""), 0.50)

    def test_single_trace_rows_carry_all_six_factors(self):
        trace = fx.trace()
        rows = exp.source_reliability_rows(trace)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["domain"], "example.com")
        self.assertEqual(sorted(row["factors"]), sorted(contracts.SOURCE_RELIABILITY_FACTORS))
        self.assertEqual(row["observations"], 1)
        self.assertEqual(row["verified"], 1)
        self.assertAlmostEqual(row["factors"]["verified_yield"], 1.0, places=6)
        self.assertEqual(row["factors"]["rejection_rate"], 0.0)
        self.assertGreater(row["factors"]["recency"], 0.9, "刚发布的内容时效因子接近 1")
        for value in row["factors"].values():
            self.assertTrue(0.0 <= value <= 1.0, row["factors"])

    def test_zero_trace_rows_have_an_honest_recency(self):
        row = exp.source_reliability_rows(fx.trace())[0]
        self.assertGreater(row["factors"]["authority"], 0.0)
        self.assertLessEqual(row["factors"]["volume"], 1.0)

    def test_aggregate_uses_totals_not_means(self):
        good = fx.trace(run_id="run-a", graph=fx.graph_with(refs=("article:1",)))
        refuted = fx.trace(run_id="run-b",
                           graph=fx.graph_with(refs=("article:2",),
                                               evidence_text=fx.fx09.UNVERIFIED_EVIDENCE_TEXT,
                                               verdict="REFUTED"))
        rows = exp.aggregate_source_reliability([good, refuted], min_samples=2)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["observations"], 2)
        self.assertEqual(row["verified"], 1)
        self.assertEqual(row["rejected"], 1)
        self.assertAlmostEqual(row["factors"]["verified_yield"], 0.5, places=6)
        self.assertAlmostEqual(row["factors"]["rejection_rate"], 0.5, places=6)

    def test_bands_and_unknown_for_small_samples(self):
        row = {"observations": 1, "reliability": 0.99}
        self.assertEqual(exp.source_band(row, min_samples=2), "UNKNOWN")
        self.assertEqual(exp.source_band(row, min_samples=1), "HIGH")
        self.assertEqual(exp.source_band({"observations": 3, "reliability": 0.6}, min_samples=2),
                         "MEDIUM")
        self.assertEqual(exp.source_band({"observations": 3, "reliability": 0.4}, min_samples=2),
                         "LOW")

    def test_limitations_only_state_what_the_data_shows(self):
        row = {"observations": 1, "rejected": 2, "authority_level": None,
               "factors": {"recency": 0.5}, "update_frequency": "UNKNOWN"}
        text = "；".join(exp.source_limitations(row, min_samples=2))
        self.assertIn("样本量 1 少于门槛 2", text)
        self.assertIn("被核验判为反驳", text)
        self.assertIn("权威等级缺失", text)
        self.assertIn("发布时间缺失", text)
        self.assertIn("更新频率未知", text)
        clean = {"observations": 5, "rejected": 0, "authority_level": 1,
                 "factors": {"recency": 0.9}, "update_frequency": "WEEKLY"}
        self.assertEqual(exp.source_limitations(clean, min_samples=2), [])

    def test_content_is_canonical_and_informative(self):
        row = exp.aggregate_source_reliability(
            [fx.trace(run_id="a"), fx.trace(run_id="b", graph=fx.graph_with(refs=("article:2",)))],
            min_samples=2)[0]
        content = exp.source_content(row)
        self.assertTrue(content.startswith("经验·来源：域 example.com"))
        self.assertIn("可靠性", content)
        self.assertIn("已核验", content)


class SkillPerformanceTests(unittest.TestCase):
    def _records(self, outcomes=("ok", "ok", "degraded")):
        rows = []
        for index, outcome in enumerate(outcomes):
            stage = "included_in_pack" if outcome == "ok" else "instruction_built"
            rows.append(fx.skill_record("bm25_search", outcome=outcome, stage=stage,
                                        latency_ms=100.0 + 10 * index, attempt=index + 1))
        return rows

    def test_success_definition_is_verbatim_from_phase11(self):
        rows = exp.skill_performance_rows(self._records())
        self.assertEqual(len(rows), 1)
        reference = skills.performance_summary(self._records())
        self.assertEqual(rows[0]["success_definition"], reference["success_definition"],
                         "成功率口径不许在本阶段重定义")
        self.assertEqual(rows[0]["attempts"], 3)
        self.assertEqual(rows[0]["successes"], 2)
        self.assertAlmostEqual(rows[0]["success_rate"], 2 / 3, places=6)
        self.assertEqual(rows[0]["latency_ms"]["p50"], reference["latency_ms"]["p50"])
        self.assertEqual(rows[0]["source"], "qa_skills.performance_table(qa_stage_runs:skill:*)")
        ok, note = contracts.validate("skill_performance_memory", rows[0])
        self.assertTrue(ok, note)

    def test_insufficient_samples_do_not_invent_a_rate(self):
        rows = exp.skill_performance_rows([fx.skill_record(attempt=1)])
        self.assertIsNone(rows[0]["success_rate"])
        candidate = exp.skill_candidates([], records=[fx.skill_record(attempt=1)])[0]
        self.assertEqual(candidate["confidence"], 0.0)
        # 样本门槛**引 Phase 11 的口径**（`skill_min_samples()`），不另立一个数
        from qa_skills import performance_summary

        self.assertEqual(candidate["sample_floor"],
                         performance_summary([])["min_samples"])
        self.assertEqual(candidate["samples"], 1)
        self.assertLess(candidate["samples"], candidate["sample_floor"])

    def test_unknown_skills_are_not_invented(self):
        rows = exp.skill_performance_rows([fx.skill_record("bm25_search")])
        self.assertEqual([row["skill_id"] for row in rows], ["bm25_search"])
        self.assertTrue(set(row["skill_id"] for row in rows) <= set(skills.SKILL_IDS))

    def test_candidate_binds_the_skill_route_evidence(self):
        trace = fx.trace(graph=fx.graph_with(routes=("keyword",)))
        candidates = exp.skill_candidates([trace], records=self._records())
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0]["evidence"], "技能表现要绑它那条通道的真实证据")
        self.assertEqual(candidates[0]["entity_ids"], ["bm25_search"])


class ProjectionWriteTests(unittest.TestCase):
    def test_source_and_skill_projections_are_written_and_content_addressed(self):
        traces = [fx.trace(run_id="run-a"), fx.trace(run_id="run-b",
                                                    graph=fx.graph_with(refs=("article:2",)))]
        records = [fx.skill_record(attempt=index + 1, outcome="ok" if index < 2 else "degraded",
                                   stage="included_in_pack" if index < 2 else "instruction_built")
                   for index in range(3)]
        with fx.temp_store("p12-source-skill.sqlite3") as (_database, store):
            receipt = exp.write_experience_memories(store, traces=traces, run_meta=fx.run_meta(),
                                                    skill_records=records)
            self.assertEqual(receipt["persisted_by_type"].get("SOURCE"), 1)
            self.assertEqual(receipt["persisted_by_type"].get("SKILL_PERFORMANCE"), 1)
            self.assertEqual(receipt["projections"]["source"], 1)
            self.assertEqual(receipt["projections"]["skill"], 1)
            self.assertEqual(receipt["projections"]["errors"], [])
            sources = store.source_reliability(scope_keys=["org:p12"])
            self.assertEqual(len(sources), 1)
            source_memory = store.load_memory_items(memory_types=["SOURCE"],
                                                    include_all_scopes=True)[0]
            self.assertEqual(sources[0]["memory_id"], source_memory["memory_id"],
                             "投影行必须指向同一内容寻址的记忆 id")
            self.assertEqual(sources[0]["band"], source_memory["metadata"]["band"])
            skills_rows = store.skill_performance_memory(scope_keys=["org:p12"])
            self.assertEqual(len(skills_rows), 1)
            skill_memory = store.load_memory_items(memory_types=["SKILL_PERFORMANCE"],
                                                   include_all_scopes=True)[0]
            self.assertEqual(skills_rows[0]["memory_id"], skill_memory["memory_id"])
            self.assertEqual(skills_rows[0]["success_definition"],
                             skill_memory["metadata"]["success_definition"])
            self.assertEqual(skills_rows[0]["telemetry_version"],
                             skill_memory["metadata"]["telemetry_version"])

    def test_insufficient_source_samples_are_rejected_by_the_gate(self):
        with fx.temp_store("p12-source-floor.sqlite3") as (_database, store):
            receipt = exp.write_experience_memories(store, traces=[fx.trace()],
                                                    run_meta=fx.run_meta())
            decisions = [row for row in receipt["decisions"] if row["memory_type"] == "SOURCE"]
            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0]["decision"], "DROP")
            self.assertEqual(decisions[0]["reason"], "BELOW_SAMPLE_FLOOR")
            self.assertEqual(store.source_reliability(scope_keys=["org:p12"]), [])

    def test_no_skill_records_means_no_skill_memory(self):
        with fx.temp_store("p12-no-skill.sqlite3") as (_database, store):
            receipt = exp.write_experience_memories(store, traces=[fx.trace()],
                                                    run_meta=fx.run_meta())
            self.assertNotIn("SKILL_PERFORMANCE", receipt["persisted_by_type"])
            self.assertEqual(store.skill_performance_memory(scope_keys=["org:p12"]), [])


if __name__ == "__main__":
    unittest.main()

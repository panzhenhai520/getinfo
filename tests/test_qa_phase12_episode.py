#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 12 · P12-01 `episode summarizer from trace` 用例。

钉住：
  1. §3.2 的**五要素**齐全（`sections` 的键集 == `EPISODE_SECTIONS`），且每一个都来自
     trace 里的真实字段（任务类型 / 真实计划 / Phase 07 缺口 / 已核验通道 / 停止原因）；
  2. **只能总结 trace，不可制造事实**：trace 里没有的东西一律不出现（缺段进
     `missing_sections`，停止原因空就是空，绝不编）；
  3. 归纳后端可插拔：入参 > `QA_EXPERIENCE_SUMMARIZER` > 内置规则；未注册名 / 抛错 /
     返回非法载荷 / 返回空字典 **四条失败路径全部保守回落规则实现并记账**；
  4. 同输入同输出（两次归纳逐字相同）、trace 指纹不含抓取时刻（时钟不影响指纹）；
  5. 剧集记忆过写门后：正文 == 归纳正文、绑真实证据、`HELPED_RESOLVE` 指向有效通道。
"""
import json
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_graph_contracts as contracts  # noqa: E402
import qa_memory as memory  # noqa: E402
import qa_memory_experience as exp  # noqa: E402

import qa_phase12_fixtures as fx  # noqa: E402


class TraceAssemblyTests(unittest.TestCase):
    def test_eight_sections_and_missing_bookkeeping(self):
        trace = fx.trace(gap_receipt=fx.gap_receipt(), skill_records=[fx.skill_record()])
        self.assertEqual(sorted(trace["missing_sections"]), [], "八段都给了就不该缺")
        bare = exp.build_trace(run_meta=fx.run_meta(), graph={}, level1={})
        for name in ("retrieval", "verification", "gaps", "stop", "sources", "skills", "planning"):
            self.assertIn(name, bare["missing_sections"])
        self.assertEqual(bare["trace_fingerprint"], exp.trace_fingerprint(bare))

    def test_task_type_comes_from_the_existing_interpreter(self):
        trace = fx.trace()
        self.assertTrue(trace["task_type"])
        from qa_query_interpreter import interpret_query

        self.assertEqual(trace["task_type"], interpret_query(fx.QUESTION)["intent"])

    def test_corpus_binding_prefers_the_run_column_then_the_cache(self):
        with_column = fx.trace(corpus_version="corpus-abc")
        binding = exp.corpus_binding(with_column)
        self.assertEqual(binding["token"], "corpus-abc")
        self.assertEqual(binding["source"], "qa_runs.corpus_version")
        without = fx.trace()
        binding = exp.corpus_binding(without)
        self.assertEqual(binding["token"], fx.KB_VERSION)
        self.assertIn("kb_version", binding["source"])
        empty = exp.corpus_binding(exp.build_trace(run_meta=fx.run_meta(), graph={}, level1={}))
        self.assertEqual(empty, {"token": "", "source": ""}, "没版本就如实空，不许编一个")

    def test_config_token_is_rule_derived_and_auditable(self):
        trace = fx.trace()
        token = exp.retrieval_config_token(trace)
        self.assertTrue(token["token"].startswith("rcfg:"))
        self.assertEqual(token["source"], "rule:retrieval-signals-v1")
        # 同一批检索开关 → 同一个令牌；换个开关组合 → 令牌变化（这就是"检索配置"的语义）
        other = fx.trace(level1_receipt=fx.level1(web_adopted=0))
        self.assertNotEqual(exp.retrieval_config_token(other)["token"], token["token"])
        explicit = fx.trace(config_hash="cfg-123")
        self.assertEqual(exp.retrieval_config_token(explicit),
                         {"token": "cfg-123", "source": "qa_runs.config_hash"})

    def test_trace_fingerprint_ignores_the_capture_time(self):
        trace = fx.trace()
        shifted = dict(trace, captured_at="2027-01-01T00:00:00Z")
        self.assertEqual(exp.trace_fingerprint(trace), exp.trace_fingerprint(shifted))


class EpisodeTests(unittest.TestCase):
    def test_five_sections_all_present_and_trace_derived(self):
        trace = fx.trace(gap_receipt=fx.gap_receipt(stop_reason="ANSWERABLE"))
        episode = exp.episode_from_trace(trace)
        self.assertEqual(sorted(episode["sections"]), sorted(contracts.EPISODE_SECTIONS))
        self.assertEqual(episode["sections"]["task_type"], trace["task_type"])
        self.assertEqual(episode["sections"]["stop_reason"], "ANSWERABLE")
        self.assertEqual(episode["sections"]["effective_paths"], ["keyword"])
        self.assertEqual(episode["sections"]["decomposition"]["count"], 0,
                         "trace 里没有子问题就如实 0，不许编一个拆解")
        ok, note = contracts.validate("episode_memory", episode)
        self.assertTrue(ok, note)

    def test_summary_mentions_only_trace_facts(self):
        trace = fx.trace(gap_receipt=fx.gap_receipt(stop_reason="NO_GAIN"))
        episode = exp.episode_from_trace(trace)
        self.assertIn("NO_GAIN", episode["summary"])
        self.assertIn("keyword", episode["summary"], "有效通道来自真实已核验产出")
        self.assertIn("关注点", episode["summary"], "关注点来自计划实体（可召回）")
        self.assertIn("家族办公室", episode["summary"])
        self.assertNotIn("SUPPORTS", episode["summary"])

    def test_ineffective_routes_are_not_effective_paths(self):
        # 取到证据但**没有一条通过核验**（论坛帖子那类），就不算"有效检索路径"
        trace = fx.trace(graph=fx.graph_with(evidence_text=fx.fx09.UNVERIFIED_EVIDENCE_TEXT),
                         gap_receipt=fx.gap_receipt())
        episode = exp.episode_from_trace(trace)
        self.assertGreater(trace["verification"]["evidence"], 0)
        self.assertEqual(trace["verification"]["supported"], 0)
        self.assertEqual(episode["effective_routes"], [],
                         "没有已核验产出就不许把它写成有效路径")
        self.assertIn("无（本轮没有任何通道取到已核验证据）", episode["summary"])

    def test_key_gaps_are_ranked_from_the_real_gap_receipt(self):
        gaps = [fx.gap("G-low", priority=0.3), fx.gap("G-high", priority=0.95)]
        trace = fx.trace(gap_receipt=fx.gap_receipt(*gaps))
        episode = exp.episode_from_trace(trace)
        self.assertEqual([row["gap_id"] for row in episode["key_gaps"]], ["G-high", "G-low"])

    def test_deterministic(self):
        trace = fx.trace(gap_receipt=fx.gap_receipt())
        first = exp.episode_from_trace(trace)
        second = exp.episode_from_trace(trace)
        self.assertEqual(first["summary"], second["summary"])
        self.assertEqual(first["sections"], second["sections"])
        self.assertEqual(json.dumps(first["sections"], sort_keys=True, ensure_ascii=False),
                         json.dumps(second["sections"], sort_keys=True, ensure_ascii=False))


class SummarizerInjectionTests(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.pop("QA_EXPERIENCE_SUMMARIZER", None)
        self.trace = fx.trace(gap_receipt=fx.gap_receipt(stop_reason="ANSWERABLE"))

    def tearDown(self):
        os.environ.pop("QA_EXPERIENCE_SUMMARIZER", None)
        exp._SUMMARIZERS.pop("test:custom", None)
        exp._SUMMARIZERS.pop("test:boom", None)
        exp._SUMMARIZERS.pop("test:junk", None)
        if self.saved is not None:
            os.environ["QA_EXPERIENCE_SUMMARIZER"] = self.saved

    def test_default_is_the_rule_backend(self):
        self.assertEqual(exp.summarizer_name(), "rule:trace-v1")
        self.assertEqual(exp.registered_summarizers(), ("rule:trace-v1",))
        episode = exp.episode_from_trace(self.trace)
        self.assertEqual(episode["summarizer"], "rule:trace-v1")
        self.assertIs(episode["fallback"]["used"], False)

    def test_registered_backend_is_used(self):
        calls = []

        def backend(trace):
            calls.append(trace["trace_fingerprint"])
            return {"summary": "自定义归纳：只总结 trace。", "sections": {},
                    "task_type": "CUSTOM", "stop_reason": "ANSWERABLE",
                    "effective_routes": [], "key_gaps": []}

        exp.register_episode_summarizer("test:custom", backend)
        episode = exp.episode_from_trace(self.trace, summarizer="test:custom")
        self.assertEqual(calls, [self.trace["trace_fingerprint"]])
        self.assertEqual(episode["summary"], "自定义归纳：只总结 trace。")
        self.assertEqual(episode["summarizer"], "test:custom")
        self.assertIs(episode["fallback"]["used"], False)
        # 五要素键集仍被补齐（自定义后端没给的段落用空值，不丢键）
        self.assertEqual(sorted(episode["sections"]), sorted(contracts.EPISODE_SECTIONS))

    def test_env_selected_backend(self):
        exp.register_episode_summarizer("test:custom", lambda trace: {"summary": "env 后端"})
        os.environ["QA_EXPERIENCE_SUMMARIZER"] = "test:custom"
        episode = exp.episode_from_trace(self.trace)
        self.assertEqual(episode["summary"], "env 后端")

    def test_unregistered_name_falls_back_to_rules(self):
        episode = exp.episode_from_trace(self.trace, summarizer="test:missing")
        self.assertEqual(episode["summarizer"], "rule:trace-v1")
        self.assertIs(episode["fallback"]["used"], True)
        self.assertIn("未注册", episode["fallback"]["reason"])
        self.assertTrue(episode["summary"].startswith("经验·剧集："))

    def test_broken_backend_falls_back_to_rules(self):
        def boom(trace):
            raise RuntimeError("后端坏了")

        exp.register_episode_summarizer("test:boom", boom)
        episode = exp.episode_from_trace(self.trace, summarizer="test:boom")
        self.assertEqual(episode["summarizer"], "rule:trace-v1")
        self.assertIn("抛错", episode["fallback"]["reason"])
        self.assertTrue(episode["summary"].startswith("经验·剧集："))

    def test_illegal_payload_falls_back_to_rules(self):
        exp.register_episode_summarizer("test:junk", lambda trace: "这不是字典")
        episode = exp.episode_from_trace(self.trace, summarizer="test:junk")
        self.assertEqual(episode["summarizer"], "rule:trace-v1")
        self.assertIn("非法载荷", episode["fallback"]["reason"])

    def test_empty_name_is_rejected(self):
        with self.assertRaises(ValueError):
            exp.register_episode_summarizer("", lambda trace: {})


class EpisodeWriteTests(unittest.TestCase):
    def test_episode_memory_keeps_the_summary_and_binds_evidence(self):
        trace = fx.trace(gap_receipt=fx.gap_receipt(stop_reason="ANSWERABLE"))
        with fx.temp_store("p12-episode.sqlite3") as (_database, store):
            receipt = exp.write_experience_memories(store, traces=[trace],
                                                    run_meta=fx.run_meta())
            self.assertEqual(receipt["persisted_by_type"].get("EPISODIC_RESEARCH"), 1)
            items = store.load_memory_items(memory_types=["EPISODIC_RESEARCH"],
                                            include_all_scopes=True)
            self.assertEqual(len(items), 1)
            item = items[0]
            self.assertEqual(item["canonical_content"],
                             exp.episode_from_trace(trace)["summary"])
            self.assertEqual(item["source_evidence_ids"], ["article:1"])
            links = store.memory_evidence(memory_ids=[item["memory_id"]])
            self.assertTrue(links[0]["source_fingerprint"])
            self.assertTrue(links[0]["span_fingerprint"] or links[0]["evidence_ref"])
            relations = store.memory_relations(memory_ids=[item["memory_id"]])
            self.assertIn("HELPED_RESOLVE", {row["relation"] for row in relations})
            self.assertIn("keyword", {row["target_ref"] for row in relations})
            self.assertEqual(item["metadata"]["stop_reason"], "ANSWERABLE")
            self.assertEqual(item["metadata"]["summarizer"], "rule:trace-v1")
            self.assertIs(item["metadata"]["hint_only"], True)

    def test_episode_without_evidence_is_rejected_honestly(self):
        """一轮什么都没捞到的"剧集"没有证据出处 → 写门如实拒收并记账（不静默）。"""
        empty_graph = {"version": "qa-evidence-graph-v1", "claims": [], "evidence": [], "edges": [],
                       "conflicts": [], "stats": {}}
        trace = fx.trace(graph=empty_graph, level1_receipt=fx.level1(adopted=0),
                         gap_receipt=fx.gap_receipt(stop_reason="NO_GAIN"))
        with fx.temp_store("p12-episode-empty.sqlite3") as (_database, store):
            receipt = exp.write_experience_memories(store, traces=[trace],
                                                    run_meta=fx.run_meta())
            decisions = [row for row in receipt["decisions"]
                         if row["memory_type"] == "EPISODIC_RESEARCH"]
            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0]["decision"], "DROP")
            self.assertEqual(decisions[0]["reason"], "NO_EVIDENCE_LINK")
            self.assertEqual(store.load_memory_items(
                memory_types=["EPISODIC_RESEARCH"], include_all_scopes=True), [])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P17-01 E2E suite 用例（**离线**：零模型、零网络、零嵌入端点；隔离临时 sqlite）。

覆盖：层开关读真源与可还原、离线替换件记账、检索侧真跑生产阶段处理器（含失败路径）、
回答侧真机 run 重放（含缺 store / 缺答案 / 图层开关）、指标口径委托、无对象标注、基线读取。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import qa_e2e  # noqa: E402
import qa_phase17_fixtures as fx  # noqa: E402


class LayerSwitchTest(unittest.TestCase):
    def test_layer_states_reads_real_defaults(self):
        states = qa_e2e.layer_states()
        self.assertEqual(set(states), set(qa_e2e.LAYER_IDS))
        self.assertTrue(states["evidence_layer"]["runtime_default"])
        self.assertTrue(states["verifier"]["runtime_default"])
        for layer in ("hunter_fleet", "execution_graph", "evidence_graph", "gap_analyzer",
                      "context_pack", "memory_graph", "skill_router", "final_verifier",
                      "acquisition"):
            self.assertFalse(states[layer]["runtime_default"],
                             "%s 生产默认必须是关（回滚口径）" % layer)
        for layer, item in states.items():
            self.assertTrue(item["accessor_available"], layer)
            self.assertIn("wired_at", item)
            self.assertTrue(item["measures"], layer)

    def test_layer_switches_restore_environment_exactly(self):
        name = qa_e2e.LAYER_BY_ID["hunter_fleet"]["switch"]
        self.assertNotIn(name, os.environ)
        with qa_e2e.layer_switches({"hunter_fleet": True}):
            self.assertEqual(os.environ[name], "1")
            self.assertTrue(qa_e2e.layer_states()["hunter_fleet"]["enabled"])
        self.assertNotIn(name, os.environ, "退出后必须逐字还原（未设过就仍然未设）")
        os.environ[name] = "0"
        try:
            with qa_e2e.layer_switches({"hunter_fleet": True}):
                self.assertEqual(os.environ[name], "1")
            self.assertEqual(os.environ[name], "0")
        finally:
            os.environ.pop(name, None)

    def test_unknown_layer_ignored(self):
        with qa_e2e.layer_switches({"not_a_layer": True}):
            self.assertEqual(qa_e2e.layer_states()["verifier"]["enabled"], True)


class OfflineSubstitutionTest(unittest.TestCase):
    def test_web_search_stub_never_returns_evidence(self):
        web = qa_e2e.OfflineWebSearch()
        payload = web.search(["问题"], enabled=True, limit=3)
        self.assertEqual(payload["evidence"], [])
        self.assertEqual(payload["status"], "offline_no_network")
        receipt = web.receipt()
        self.assertEqual(receipt["calls"], 1)
        self.assertEqual(receipt["enabled_calls"], 1)
        self.assertEqual(receipt["reason_code"], "OFFLINE_NO_NETWORK")

    def test_provider_registry_stub_refuses_everything(self):
        registry = qa_e2e.OfflineProviderRegistry()
        with self.assertRaises(RuntimeError):
            registry.resolve("synthesis", "local")
        with self.assertRaises(RuntimeError):
            registry.anything
        self.assertEqual(registry.receipt()["reason_code"], "OFFLINE_NO_MODEL_ENDPOINT")

    def test_reset_caches_clears_verifier_default_cache(self):
        import qa_verifier

        qa_verifier._DEFAULT_CACHE.put("k", {"verdict": "SUPPORTED"})
        self.assertTrue(qa_verifier._DEFAULT_CACHE.get("k"))
        report = qa_e2e.reset_process_caches()
        self.assertTrue(report["verifier_cache"]["ok"])
        self.assertFalse(qa_verifier._DEFAULT_CACHE.get("k"),
                         "消融每格开始前必须清掉进程内核验缓存（否则跨格串味）")

    def test_offline_substitutions_documented(self):
        ports = {item["port"] for item in qa_e2e.OFFLINE_SUBSTITUTIONS}
        self.assertIn("web_search", ports)
        self.assertIn("provider_registry", ports)
        self.assertIn("ragflow_client（level2）", ports)


class RetrievalSuiteTest(unittest.TestCase):
    def setUp(self):
        self.temp = fx.temp_dir()
        self.database = fx.synthetic_corpus_db(self.temp)
        self.questions = fx.tiny_questions()[:2]

    def tearDown(self):
        try:
            self.database.connection.close()
        except Exception:  # noqa: BLE001
            pass
        fx.cleanup_dir(self.temp)

    def test_rows_keep_phase00_row_keys(self):
        with qa_e2e.layer_switches({name: True for name in qa_e2e.LAYER_IDS}):
            outcome = qa_e2e.run_retrieval_suite(self.database, self.questions, limit=6)
        self.assertEqual(len(outcome["rows"]), 2)
        for row in outcome["rows"]:
            for key in qa_e2e.RETRIEVAL_ROW_KEYS:
                self.assertIn(key, row, "逐题行必须与 Phase 00 口径同名同键：%s" % key)
            self.assertNotIn("error", row)
        metrics = qa_e2e.retrieval_metrics(outcome["rows"], limit=6)
        for key in qa_e2e.E2E_METRIC_KEYS:
            self.assertIn(key, metrics)
        self.assertEqual(metrics["errors"], 0)

    def test_evidence_layer_off_records_reason_code(self):
        with qa_e2e.layer_switches({"evidence_layer": False}):
            outcome = qa_e2e.run_retrieval_suite(self.database, self.questions, limit=6)
        for receipt in outcome["receipts"]:
            layer = receipt["evidence_layer"]
            self.assertEqual(layer.get("evidence_layer"), "skipped")
            self.assertIn("QA_EVIDENCE_LAYER_ENABLED=0", str(layer.get("reason")))

    def test_evidence_layer_on_annotates_and_verifies(self):
        with qa_e2e.layer_switches({"evidence_layer": True, "verifier": True}):
            outcome = qa_e2e.run_retrieval_suite(self.database, self.questions, limit=6)
        annotated = [receipt for receipt in outcome["receipts"]
                     if int((receipt["evidence_layer"] or {}).get("annotated") or 0) > 0]
        self.assertTrue(annotated, "打开证据层后必须真的有标注（不是空跑）")
        self.assertTrue(any((receipt.get("verification") or {}).get("verdicts")
                            for receipt in outcome["receipts"]))

    def test_missing_corpus_pack_is_not_an_error(self):
        """没有语料的包：命中为空但**不是错误**（如实记 hit=False）。"""
        with qa_e2e.layer_switches({"evidence_layer": True}):
            outcome = qa_e2e.run_retrieval_suite(self.database, [fx.tiny_questions()[2]],
                                                 limit=6)
        row = outcome["rows"][0]
        self.assertFalse(row["hit"])
        self.assertEqual(row["evidence"], 0)
        self.assertFalse(row.get("error"), "缺语料不是异常，不该记 error")

    def test_broken_pipeline_records_error_per_question(self):
        """失败路径：阶段处理器抛错也不许抛出去，逐题记 error。"""
        from unittest.mock import patch

        def _boom(_context):
            raise RuntimeError("injected: level1_retrieval 崩了")

        real = qa_e2e.build_offline_handlers

        def _patched(database, store, **kwargs):
            handlers, substitutions = real(database, store, **kwargs)
            handlers["level1_retrieval"] = _boom
            return handlers, substitutions

        with patch.object(qa_e2e, "build_offline_handlers", _patched):
            with qa_e2e.layer_switches({"evidence_layer": True}):
                outcome = qa_e2e.run_retrieval_suite(self.database, self.questions[:1], limit=6)
        self.assertIn("injected", str(outcome["rows"][0].get("error")))
        self.assertIn("injected", str(outcome["receipts"][0].get("error")))
        self.assertEqual(outcome["rows"][0]["evidence"], 0)

    def test_substitutions_are_counted(self):
        with qa_e2e.layer_switches({name: False for name in qa_e2e.LAYER_IDS}):
            outcome = qa_e2e.run_retrieval_suite(self.database, self.questions, limit=6)
        self.assertGreaterEqual(outcome["substitutions"]["web_search"]["calls"], 1)
        self.assertGreaterEqual(outcome["substitutions"]["provider_registry"]["calls"], 1)

    def test_corpus_disjointness_marks_out_of_scope(self):
        report = qa_e2e.corpus_disjointness(fx.tiny_questions(), [fx.PACK_ID])
        self.assertEqual(report["in_scope_count"], 2)
        self.assertEqual(report["out_of_scope_count"], 1)
        self.assertEqual(report["out_of_scope"][0]["reason_code"], "NO_CORPUS_IN_SNAPSHOT")


class AnswerSuiteTest(unittest.TestCase):
    def setUp(self):
        self.temp = fx.temp_dir()
        self.database, self.store = fx.store_for(self.temp)
        self.snapshot = fx.tiny_answer_snapshot()

    def tearDown(self):
        try:
            self.database.connection.close()
        except Exception:  # noqa: BLE001
            pass
        fx.cleanup_dir(self.temp)

    def test_runs_scored_with_all_layers_on(self):
        with qa_e2e.layer_switches({name: True for name in qa_e2e.LAYER_IDS}):
            outcome = qa_e2e.run_answer_suite(self.snapshot, store=self.store)
        totals = outcome["totals"]
        self.assertEqual(totals["runs_scored"], 1)
        self.assertEqual(totals["claims"], 2)
        self.assertEqual(totals["evidence"], 2)
        self.assertEqual(totals["retrieval_calls"], 0, "终验的重试绝不许再检索")
        self.assertTrue(totals["citation_traceable_rate"] in (0.0, 1.0))
        row = outcome["rows"][0]
        for layer in ("claim_verification", "evidence_graph", "gap_review", "memory_recall",
                      "skill_routing", "context_pack", "memory_write", "final_verifier"):
            self.assertIn(layer, row["layers"])

    def test_layers_off_record_switch_reason(self):
        with qa_e2e.layer_switches({name: False for name in qa_e2e.LAYER_IDS}):
            outcome = qa_e2e.run_answer_suite(self.snapshot, store=self.store,
                                              write_memories=False)
        layers = outcome["rows"][0]["layers"]
        for layer, prefix in (("evidence_graph", "SWITCH_OFF:QA_EVIDENCE_GRAPH"),
                              ("context_pack", "SWITCH_OFF:QA_CONTEXT_PACK"),
                              ("skill_routing", "SWITCH_OFF:QA_SKILL_ROUTER"),
                              ("final_verifier", "SWITCH_OFF:QA_FINAL_VERIFIER"),
                              ("memory_write", "SWITCH_OFF:QA_MEMORY_GRAPH")):
            self.assertEqual(layers[layer]["state"], "skipped")
            self.assertEqual(layers[layer]["reason"], prefix)
        self.assertEqual(outcome["totals"]["checks_total"], 0)
        self.assertIsNone(outcome["totals"]["checks_pass_rate"])

    def test_run_without_answer_is_skipped_with_reason(self):
        snapshot = fx.tiny_answer_snapshot(with_answer=False)
        with qa_e2e.layer_switches({name: True for name in qa_e2e.LAYER_IDS}):
            outcome = qa_e2e.run_answer_suite(snapshot, store=self.store)
        self.assertEqual(outcome["totals"]["runs_scored"], 0)
        self.assertIn("没有最终答案", outcome["rows"][0]["skipped"])

    def test_run_without_question_is_skipped(self):
        snapshot = fx.tiny_answer_snapshot(question="")
        with qa_e2e.layer_switches({name: True for name in qa_e2e.LAYER_IDS}):
            outcome = qa_e2e.run_answer_suite(snapshot, store=self.store)
        self.assertIn("缺少问题", outcome["rows"][0]["skipped"])

    def test_alias_snapshot_keys_are_normalized(self):
        aliased = fx.alias_answer_snapshot(self.snapshot)
        self.assertNotIn("runs", aliased)
        normalized = qa_e2e.normalize_answer_snapshot(aliased)
        self.assertEqual(len(normalized["runs"]), 1)
        with qa_e2e.layer_switches({name: True for name in qa_e2e.LAYER_IDS}):
            outcome = qa_e2e.run_answer_suite(aliased, store=self.store)
        self.assertEqual(outcome["totals"]["runs_scored"], 1)

    def test_memory_write_persists_when_layer_on(self):
        with qa_e2e.layer_switches({"memory_graph": True}):
            outcome = qa_e2e.run_answer_suite(self.snapshot, store=self.store,
                                              write_memories=True)
        write = outcome["rows"][0]["layers"]["memory_write"]
        self.assertEqual(write["state"], "on")
        self.assertGreaterEqual(write["candidates"], 1)

    def test_metrics_caliber_is_delegated_not_reimplemented(self):
        """拿不到 Phase 00 工具时必须**拒绝算**（不另立一套口径）。"""
        import qa_e2e as module

        saved = module._acceptance_module
        module._acceptance_module = lambda: None
        try:
            metrics = module.retrieval_metrics([], limit=12)
            self.assertTrue(metrics["caliber_missing"])
            self.assertIn("不另算一套", metrics["reason"])
        finally:
            module._acceptance_module = saved


class NoObjectAndBaselineTest(unittest.TestCase):
    def test_no_object_report_marks_offline_channels(self):
        report = qa_e2e.no_object_report([], {"contradictions": 0, "counter_evidence": 0})
        self.assertEqual(report["retrieval"]["web_channel"]["reason_code"],
                         "OFFLINE_NO_NETWORK")
        self.assertEqual(report["retrieval"]["level2_ragflow"]["reason_code"],
                         "OFFLINE_NO_RAGFLOW")
        self.assertEqual(report["answer"]["contradiction_resolution"]["reason_code"],
                         "NO_CONFLICT_ROW_IN_REAL_DATA")
        self.assertEqual(report["answer"]["counter_evidence"]["reason_code"],
                         "NO_REFUTES_EDGE_IN_REAL_DATA")

    def test_no_object_counts_when_objects_exist(self):
        receipts = [{"hunter_fleet": {"runs": 1}}, {"hunter_fleet": {"runs": 1}}]
        report = qa_e2e.no_object_report(receipts, {"contradictions": 2})
        self.assertEqual(report["retrieval"]["hunter_fleet"]["objects"], 2)
        self.assertEqual(report["retrieval"]["hunter_fleet"]["reason_code"], "")
        self.assertEqual(report["answer"]["contradiction_resolution"]["reason_code"], "")

    def test_phase00_baseline_reads_history(self):
        report = qa_e2e.phase00_baseline()
        self.assertEqual(report["reason_code"], "")
        summary = report["frozen"]["summary"]
        self.assertEqual(summary["benchmark_version"], "qa-acceptance-v1")
        self.assertEqual(summary["k"], 12)
        self.assertEqual(summary["recall_at_k"], 0.8333)
        self.assertEqual(summary["citation_precision"], 0.7719)

    def test_phase00_baseline_missing_file(self):
        report = qa_e2e.phase00_baseline(os.path.join("no", "such", "history.jsonl"))
        self.assertEqual(report["reason_code"], "NO_HISTORY_FILE")
        self.assertIsNone(report["frozen"])

    def test_metric_delta_never_uses_zero_for_missing(self):
        delta = qa_e2e.metric_delta({"recall_at_k": 0.5, "avg_ms": None},
                                    {"recall_at_k": 0.75, "avg_ms": 100})
        self.assertEqual(delta["recall_at_k"], 0.25)
        self.assertIsNone(delta["avg_ms"])

    def test_fingerprint_is_content_addressed(self):
        self.assertEqual(qa_e2e.fingerprint({"a": 1}), qa_e2e.fingerprint({"a": 1}))
        self.assertNotEqual(qa_e2e.fingerprint({"a": 1}), qa_e2e.fingerprint({"a": 2}))


class SnapshotLoaderTest(unittest.TestCase):
    def test_build_corpus_template_from_real_snapshot(self):
        snapshot = os.path.join("baseline", "qa-hunter-corpus-snapshot.json")
        if not os.path.isfile(snapshot):
            self.fail("缺少 Phase 04 语料快照（E2E 的语料真源）")
        temp = fx.temp_dir()
        try:
            template = qa_e2e.build_corpus_template(snapshot, workdir=temp)
            self.assertGreater(template["counts"]["articles"], 0)
            self.assertGreater(template["counts"]["embeddings"], 0)
            self.assertTrue(template["packs"])
            cloned = qa_e2e.clone_database(template["template_path"],
                                           os.path.join(temp, "clone.sqlite3"))
            self.assertTrue(os.path.isfile(cloned))
            self.assertNotEqual(os.path.abspath(cloned),
                                os.path.abspath(template["template_path"]))
        finally:
            fx.cleanup_dir(temp)

    def test_build_corpus_template_missing_snapshot(self):
        with self.assertRaises(FileNotFoundError):
            qa_e2e.build_corpus_template(os.path.join("no", "such", "snapshot.json"))


class SuiteAggregationTest(unittest.TestCase):
    def setUp(self):
        self.temp = fx.temp_dir()
        self.database = fx.synthetic_corpus_db(self.temp, name="suite-corpus.sqlite3")

    def tearDown(self):
        try:
            self.database.connection.close()
        except Exception:  # noqa: BLE001
            pass
        fx.cleanup_dir(self.temp)

    def test_run_suite_combines_both_sides(self):
        snapshot = fx.tiny_answer_snapshot()
        with qa_e2e.layer_switches({name: True for name in qa_e2e.LAYER_IDS}):
            outcome = qa_e2e.run_suite(self.database, fx.tiny_questions()[:1], snapshot,
                                       limit=6, benchmark_meta={"benchmark_version": "test"})
        self.assertEqual(outcome["suite_version"], qa_e2e.E2E_SUITE_VERSION)
        self.assertEqual(outcome["retrieval_metrics"]["benchmark_version"], "test")
        self.assertEqual(outcome["answer_totals"]["runs_scored"], 1)
        self.assertIn("layers", outcome)
        self.assertTrue(outcome["cache_reset"]["verifier_cache"]["ok"])


if __name__ == "__main__":
    unittest.main()

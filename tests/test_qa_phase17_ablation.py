#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P17-02 ablation ladder 用例：格子清单、增量数学、组合效应、无对象标注、真跑一格。"""
from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import qa_ablation  # noqa: E402
import qa_e2e  # noqa: E402
import qa_phase17_fixtures as fx  # noqa: E402


class LadderShapeTest(unittest.TestCase):
    def test_cells_cover_every_layer(self):
        cells = qa_ablation.ladder_cells()
        names = [cell["cell"] for cell in cells]
        self.assertEqual(len(names), len(set(names)), "格子名不许重复")
        self.assertIn("phase01_baseline", names)
        self.assertIn("full_stack", names)
        self.assertIn("retrieval_only", names)
        self.assertIn("answer_only", names)
        for layer in qa_e2e.LAYER_IDS:
            self.assertIn("minus_%s" % layer, names)
        self.assertEqual(len(cells), 4 + len(qa_e2e.LAYER_IDS))

    def test_cell_states_semantics(self):
        baseline = qa_ablation.cell_states(all_on=False)
        self.assertFalse(any(baseline.values()), "Phase 01 基线 = 全部可选层关闭")
        full = qa_ablation.cell_states(all_on=True)
        self.assertTrue(all(full.values()))
        minus = qa_ablation.cell_states("memory_graph", all_on=True)
        self.assertEqual([name for name, value in minus.items() if not value], ["memory_graph"])

    def test_combo_cells_split_by_side(self):
        cells = {cell["cell"]: cell for cell in qa_ablation.ladder_cells()}
        retrieval_only = cells["retrieval_only"]["states"]
        answer_only = cells["answer_only"]["states"]
        self.assertTrue(retrieval_only["hunter_fleet"])
        self.assertFalse(retrieval_only["final_verifier"])
        self.assertTrue(answer_only["final_verifier"])
        self.assertFalse(answer_only["hunter_fleet"])


class IncrementMathTest(unittest.TestCase):
    def _cells(self):
        return [
            {"cell": "phase01_baseline", "kind": "baseline",
             "metrics": {"recall_at_k": 0.5, "avg_ms": 100, "context_tokens": 0, "errors": 0}},
            {"cell": "full_stack", "kind": "full",
             "metrics": {"recall_at_k": 0.6, "avg_ms": 150, "context_tokens": 1000, "errors": 0}},
            {"cell": "minus_context_pack", "kind": "leave_one_out", "layer": "context_pack",
             "metrics": {"recall_at_k": 0.6, "avg_ms": 150, "context_tokens": 0, "errors": 0}},
            {"cell": "minus_hunter_fleet", "kind": "leave_one_out", "layer": "hunter_fleet",
             "metrics": {"recall_at_k": 0.55, "avg_ms": 80, "context_tokens": 1000, "errors": 0}},
        ]

    def test_leave_one_out_direction(self):
        """增量口径 = full_stack −（去掉该层）：正数表示该层有正贡献。"""
        report = qa_ablation.increments(self._cells())
        lOO = report["leave_one_out"]
        self.assertIn("context_pack", lOO)
        self.assertEqual(lOO["context_pack"]["contribution"]["context_tokens"], 1000)
        self.assertEqual(lOO["hunter_fleet"]["contribution"]["recall_at_k"], 0.05)

    def test_combination_and_residual(self):
        report = qa_ablation.increments(self._cells())
        combination = report["combination"]
        self.assertEqual(combination["delta_vs_baseline"]["recall_at_k"], 0.1)
        self.assertEqual(combination["delta_vs_baseline"]["avg_ms"], 50)
        # 单层贡献之和 = 0.05（只开舰队追回 0.05）+ 0 = 0.05 → 残差 = 0.1 − 0.05 = 0.05（非可加）
        self.assertAlmostEqual(combination["sum_of_leave_one_out"]["recall_at_k"], 0.05, places=6)
        self.assertAlmostEqual(combination["additivity_residual"]["recall_at_k"], 0.05, places=6)
        self.assertEqual(combination["sum_of_leave_one_out"]["context_tokens"], 1000)

    def test_missing_metric_stays_null(self):
        cells = self._cells()
        cells[1]["metrics"]["checks_pass_rate"] = None
        cells[2]["metrics"]["checks_pass_rate"] = 0.5
        report = qa_ablation.increments(cells)
        self.assertIsNone(report["leave_one_out"]["context_pack"]["contribution"]
                          ["checks_pass_rate"])
        self.assertIsNone(report["combination"]["delta_vs_baseline"]["checks_pass_rate"])

    def test_safe_delta_refuses_bool_and_strings(self):
        self.assertIsNone(qa_ablation._safe_delta(True, 2))
        self.assertIsNone(qa_ablation._safe_delta("a", 2))
        self.assertEqual(qa_ablation._safe_delta(1, 2), 1)

    def test_no_object_summary_marks_default_off_layers(self):
        cells = [{"cell": "full_stack", "no_object": {"retrieval": {
            "web_channel": {"objects": 0, "reason_code": "OFFLINE_NO_NETWORK"}}}}]
        summary = qa_ablation.no_object_summary(cells)
        self.assertEqual(summary["retrieval.web_channel"]["reason_codes"]["OFFLINE_NO_NETWORK"], 1)
        self.assertEqual(summary["layer.hunter_fleet"]["reason_codes"]["SWITCH_OFF_BY_DEFAULT"], 1)
        self.assertEqual(summary["layer.evidence_layer"]["reason_codes"], {})


class RealCellRunTest(unittest.TestCase):
    """真跑一格 + 去掉一层：两次都要有真数据，且落在**不同的独立库**上。"""

    def setUp(self):
        self.temp = fx.temp_dir()
        self.database = fx.synthetic_corpus_db(self.temp, name="ladder-corpus.sqlite3")
        self.questions = fx.tiny_questions()[:1]
        self.snapshot = fx.tiny_answer_snapshot()

    def tearDown(self):
        try:
            self.database.connection.close()
        except Exception:  # noqa: BLE001
            pass
        fx.cleanup_dir(self.temp)

    def test_two_cells_run_and_differ(self):
        cells = {cell["cell"]: cell for cell in qa_ablation.ladder_cells()}
        workdir = os.path.join(self.temp, "cells")
        full = qa_ablation.run_cell(cells["full_stack"], template={
            "template_path": os.path.join(self.temp, "ladder-corpus.sqlite3")},
            questions=self.questions, snapshot=self.snapshot, limit=6, workdir=workdir)
        without = qa_ablation.run_cell(cells["minus_context_pack"], template={
            "template_path": os.path.join(self.temp, "ladder-corpus.sqlite3")},
            questions=self.questions, snapshot=self.snapshot, limit=6, workdir=workdir)
        self.assertTrue(full["metrics"], "格子必须有指标（不是空跑）")
        self.assertGreater(full["metrics"]["context_tokens"], 0)
        self.assertEqual(without["metrics"]["context_tokens"], 0)
        self.assertNotEqual(full["retrieval_receipts"][0]["run_id"],
                            without["retrieval_receipts"][0]["run_id"],
                            "每格独立库：run id 不许相同（否则是同一份状态）")
        self.assertTrue(full["cache_reset"]["verifier_cache"]["ok"])

    def test_format_table_mentions_layers(self):
        cells = {cell["cell"]: cell for cell in qa_ablation.ladder_cells()}
        full = qa_ablation.run_cell(cells["full_stack"], template={
            "template_path": os.path.join(self.temp, "ladder-corpus.sqlite3")},
            questions=self.questions, snapshot=self.snapshot, limit=6,
            workdir=os.path.join(self.temp, "cells2"))
        report = qa_ablation.increments([{"cell": "full_stack", "kind": "full",
                                          "metrics": full["metrics"]}])
        text = qa_ablation.format_table({"increments": report})
        self.assertIn("指标", text)


if __name__ == "__main__":
    unittest.main()

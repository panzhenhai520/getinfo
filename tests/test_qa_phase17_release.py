#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P17-04/05/06 用例：兼容性回归、迁移回滚（真做 前滚→回滚→再前滚）、发布清单、静态守门。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import qa_release  # noqa: E402
import qa_phase17_fixtures as fx  # noqa: E402


class CompatibilityRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = qa_release.compatibility_regression()

    def test_all_checks_pass(self):
        self.assertTrue(self.report["passed"], "兼容性回归失败项：%s" % self.report["failed"])
        self.assertEqual(self.report["failed"], [])

    def test_expected_checks_present(self):
        names = {item["name"] for item in self.report["checks"]}
        for name in ("frozen_inventory_present", "required_modules_present",
                     "dependency_edges_recorded", "seven_fingerprints_unchanged",
                     "evidence_schema_still_closed", "version_anchors_unchanged",
                     "schema_version_is_declared_latest", "no_added_columns_after_v6",
                     "v6_columns_frozen", "enum_sizes_unchanged",
                     "observability_views_unchanged", "switch_defaults_unchanged",
                     "stage_plans_unchanged", "final_answer_contract_still_strict",
                     "observability_view_contract_rejects_extra",
                     "new_modules_zero_network_zero_model"):
            self.assertIn(name, names)

    def test_fingerprints_equal_frozen_values(self):
        self.assertEqual(self.report["fingerprints"],
                         qa_release.FROZEN_SCHEMA_FINGERPRINTS)

    def test_every_check_carries_expected_and_actual(self):
        for item in self.report["checks"]:
            self.assertIn("detail", item)
            self.assertTrue(item["detail"])

    def test_switch_defaults_match_runtime(self):
        mismatched = [name for name, item in self.report["switches"].items()
                      if item["actual"] != item["expected"]]
        self.assertEqual(mismatched, [])
        self.assertEqual(len(self.report["switches"]), len(qa_release.SWITCH_DEFAULTS_V1))

    def test_stage_plans_match_frozen_table(self):
        for mode, chain in qa_release.STAGE_PLANS_V1.items():
            self.assertEqual(tuple(self.report["stage_plans"][mode]), chain)


class AstGuardTest(unittest.TestCase):
    def test_new_modules_have_no_network_or_model_calls(self):
        guard = qa_release._ast_guard(qa_release.ZERO_NETWORK_MODULES)
        self.assertTrue(guard["ok"], "静态守门失败：%s" % guard["violations"])
        self.assertEqual(sorted(guard["scanned"]), sorted(qa_release.ZERO_NETWORK_MODULES))
        for item in guard["url_literals"]:
            self.assertTrue(item["value"].startswith(("https://example.invalid/",
                                                      "http://example.invalid/")),
                            "只允许离线桩占位域：%s" % item["value"])

    def test_guard_detects_forbidden_import(self):
        temp = fx.temp_dir()
        try:
            path = os.path.join(temp, "bad_module.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("import requests\n\n\ndef call():\n    return requests.get('x')\n")
            guard = qa_release._ast_guard([path])
            self.assertFalse(guard["ok"])
            kinds = {item["kind"] for item in guard["violations"]}
            self.assertIn("IMPORT", kinds)
            self.assertIn("CALL", kinds)
        finally:
            fx.cleanup_dir(temp)

    def test_guard_detects_real_endpoint_literal(self):
        temp = fx.temp_dir()
        try:
            path = os.path.join(temp, "endpoint_module.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("URL = 'https://gpu.example.internal:8082/v1/embeddings'\n")
            guard = qa_release._ast_guard([path])
            self.assertFalse(guard["ok"])
            self.assertEqual(guard["violations"][0]["kind"], "URL")
        finally:
            fx.cleanup_dir(temp)

    def test_guard_detects_model_call(self):
        temp = fx.temp_dir()
        try:
            path = os.path.join(temp, "model_module.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("def go(client):\n    return client._embed_question('x')\n")
            guard = qa_release._ast_guard([path])
            self.assertFalse(guard["ok"])
            self.assertEqual(guard["violations"][0]["kind"], "MODEL_CALL")
        finally:
            fx.cleanup_dir(temp)

    def test_missing_module_is_reported(self):
        guard = qa_release._ast_guard(["no_such_module_p17.py"])
        self.assertFalse(guard["ok"])
        self.assertEqual(guard["violations"][0]["kind"], "MISSING")


class MigrationRollbackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workdir = fx.temp_dir()
        cls.report = qa_release.migration_rollback(workdir=cls.workdir)

    @classmethod
    def tearDownClass(cls):
        fx.cleanup_dir(cls.workdir)

    def test_rollback_roundtrip_passes(self):
        self.assertTrue(self.report["passed"])
        steps = {step["step"]: step for step in self.report["steps"]}
        self.assertTrue(steps["forward"]["all_required_present"])
        self.assertTrue(steps["rollback"]["matches_v6_core"])
        self.assertTrue(steps["rollback"]["legacy_rows_intact"])
        self.assertTrue(steps["forward_again"]["restored"])
        self.assertTrue(steps["forward_again"]["legacy_rows_intact"])
        self.assertTrue(steps["idempotent_rereun"]["unchanged"])

    def test_post_v6_tables_are_the_rollback_targets(self):
        self.assertEqual(len(self.report["post_v6_tables"]),
                         len(qa_release.POST_V6_TABLES))
        self.assertIn("qa_realignment_decisions", self.report["post_v6_tables"])
        self.assertIn("memory_item", self.report["post_v6_tables"])
        self.assertNotIn("qa_runs", self.report["post_v6_tables"])
        steps = {step["step"]: step for step in self.report["steps"]}
        self.assertEqual(steps["forward"]["tables"] - steps["rollback"]["tables"],
                         len(qa_release.POST_V6_TABLES))

    def test_rollback_sql_is_executable_and_reversible(self):
        sql = self.report["rollback_sql"]
        drops = sql["steps"][1]["sql"]
        self.assertEqual(len(drops), len(qa_release.POST_V6_TABLES))
        for statement in drops:
            self.assertTrue(statement.startswith("DROP TABLE IF EXISTS "))
        self.assertTrue(sql["no_column_drops_needed"])
        self.assertIn("ensure_qa_tables", sql["forward_sql"])

    def test_rollback_drops_post_v6_data_by_design(self):
        """回滚是**有代价**的：v7…v11 的表连同数据一起消失，这点必须写明并可复算。"""
        import sqlite3

        from qa_schema import ensure_qa_tables

        temp = fx.temp_dir()
        path = os.path.join(temp, "cost.sqlite3")
        connection = sqlite3.connect(path)
        try:
            cursor = connection.cursor()
            ensure_qa_tables(cursor)
            cursor.execute(
                "INSERT INTO memory_item(memory_id,memory_type,scope,scope_key,status,"
                "canonical_content,content_fingerprint,confidence,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("m1", "VERIFIED_CLAIM", "ENCOUNTER", "k", "ACTIVE", "内容", "fp", 0.5,
                 "2026-10-11T00:00:00Z", "2026-10-11T00:00:00Z"))
            connection.commit()
            cursor.execute("SELECT count(*) FROM memory_item")
            self.assertEqual(int(cursor.fetchone()[0]), 1)
            for statement in self.report["rollback_sql"]["steps"][1]["sql"]:
                cursor.execute(statement)
            connection.commit()
            cursor.execute("SELECT count(*) FROM sqlite_master WHERE type='table' "
                           "AND name='memory_item'")
            self.assertEqual(int(cursor.fetchone()[0]), 0,
                             "回滚后 memory_item 必须不存在（数据一并消失，属已知代价）")
            ensure_qa_tables(cursor)
            connection.commit()
            cursor.execute("SELECT count(*) FROM memory_item")
            self.assertEqual(int(cursor.fetchone()[0]), 0, "再前滚不会把被删的记忆找回来")
        finally:
            connection.close()
            fx.cleanup_dir(temp)


class ReleaseManifestTest(unittest.TestCase):
    def _manifest(self, **overrides):
        payload = {
            "e2e": {"acceptance": {"status": "PASS", "numbers": {"recall_at_k": 0.4}}},
            "ablation": {"fingerprint": "abc", "cells": [{"cell": "full_stack"}],
                         "layers": {"evidence_layer": {}}},
            "metric_report": {"report_version": "x",
                              "annotation": {"counts": {"pairs": 101},
                                             "evaluation": {"support_recall": 0.85,
                                                            "false_support_rate": 0.0}}},
            "compatibility": {"passed": True, "checks": [{"name": "x", "ok": True}],
                              "failed": []},
            "rollback": {"passed": True, "steps": [{"step": "forward"}],
                         "post_v6_tables": ["memory_item"], "rollback_sql": {"steps": []}},
        }
        payload.update(overrides)
        return qa_release.release_manifest(**payload)

    def test_matrix_covers_all_six_subtasks(self):
        manifest = self._manifest()
        ids = [row["id"] for row in manifest["acceptance_matrix"]]
        self.assertEqual(ids, ["P17-01", "P17-02", "P17-03", "P17-04", "P17-05", "P17-06"])

    def test_gates_include_unmet_items_truthfully(self):
        manifest = self._manifest()
        gates = {item["gate"]: item for item in manifest["gates"]}
        self.assertFalse(gates["IMPLEMENTATION_STATUS / ACCEPTANCE_MATRIX 已更新"]["ok"])
        self.assertFalse(gates["无未处理高优先级 blocker"]["ok"])
        self.assertFalse(manifest["gates_passed"])

    def test_blocker_b003_recorded(self):
        manifest = self._manifest()
        blocker = manifest["blockers"][0]
        self.assertEqual(blocker["id"], "B-003")
        self.assertEqual(blocker["status"], "OPEN")
        self.assertIn("硬约束", blocker["root_cause"])

    def test_rollback_methods_documented(self):
        manifest = self._manifest()
        self.assertIn("per_layer_switches", manifest["rollback"])
        self.assertTrue(manifest["rollback"]["per_layer_switches"])
        self.assertIn("schema", manifest["rollback"])

    def test_artifacts_hashed_when_present(self):
        manifest = self._manifest()
        present = [item for item in manifest["artifacts"] if item["present"]]
        self.assertTrue(present)
        for item in present:
            self.assertEqual(len(item["sha256"]), 64)

    def test_limitations_listed(self):
        manifest = self._manifest()
        ids = {item["id"] for item in manifest["known_limitations"]}
        self.assertTrue({"L1", "L2", "L3", "L4", "L5"} <= ids)

    def test_failed_compat_marks_release_partial(self):
        manifest = self._manifest(compatibility={"passed": False, "checks": [], "failed": ["x"]})
        row = [item for item in manifest["acceptance_matrix"] if item["id"] == "P17-04"][0]
        self.assertEqual(row["status"], "FAIL")


class ArtifactConsistencyTest(unittest.TestCase):
    """落盘产物与现跑一致（本仓库的既有口径）。"""

    def test_release_artifact_matches_current_regression(self):
        path = os.path.join("baseline", "qa-release-acceptance.json")
        if not os.path.isfile(path):
            self.fail("缺少 %s（跑 tools/qa_phase17_acceptance.py --section release 生成）" % path)
        with open(path, encoding="utf-8") as handle:
            artifact = json.load(handle)
        current = qa_release.compatibility_regression()
        self.assertEqual(artifact["compatibility"]["fingerprints"], current["fingerprints"])
        self.assertEqual(artifact["compatibility"]["passed"], current["passed"])
        self.assertEqual(artifact["version"], qa_release.RELEASE_VERSION)

    def test_ablation_artifact_shape(self):
        path = os.path.join("baseline", "qa-ablation-acceptance.json")
        if not os.path.isfile(path):
            self.fail("缺少 %s（跑 tools/qa_phase17_acceptance.py --section ablation 生成）" % path)
        with open(path, encoding="utf-8") as handle:
            artifact = json.load(handle)
        self.assertEqual(len(artifact["cells"]), 4 + len(artifact["layers"]))
        for layer in artifact["layers"]:
            self.assertIn(layer, artifact["increments"]["leave_one_out"])

    def test_e2e_artifact_shape(self):
        path = os.path.join("baseline", "qa-e2e-acceptance.json")
        if not os.path.isfile(path):
            self.fail("缺少 %s（跑 tools/qa_phase17_acceptance.py --section e2e 生成）" % path)
        with open(path, encoding="utf-8") as handle:
            artifact = json.load(handle)
        self.assertTrue(artifact["checks"]["citations_all_traceable"])
        self.assertEqual(artifact["answer_totals"]["retrieval_calls"], 0)
        self.assertIn("in_scope_metrics", artifact)
        self.assertIn("corpus_scope", artifact)


if __name__ == "__main__":
    unittest.main()

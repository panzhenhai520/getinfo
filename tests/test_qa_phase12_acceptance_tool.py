#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 12 · 验收工具（真机快照 + 离线重建）守门用例。

钉住：
  1. **可复算**：同一份快照跑两次，报告逐字相同（时钟显式取自快照，不用墙上钟）；
  2. **落盘产物与现跑一致**：`baseline/qa-experience-acceptance.json` 必须等于现在重算的结果
     （谁偷偷改了口径，这条立刻红）；
  3. **验收闸门全绿**：六类记忆真的落库、来源/技能两张投影表真有行、
     provenance 100%、前后对比真的改变了通道、版本门真的拦下跨版本压制；
  4. **快照只读口径**：快照自己声明了只读访问与阶段投影口径；
  5. **真机 before 态**：A 机没有两张 Phase 12 投影表（Phase 12 未发布）——
     这条就是"迁移前后"的 before 侧证据（老库升级由 schema 用例证明）。
"""
import json
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))

import qa_phase12_experience_acceptance as acc  # noqa: E402

SNAPSHOT = os.path.join(REPO_ROOT, "baseline", "qa-experience-real-sample.json")
REPORT = os.path.join(REPO_ROOT, "baseline", "qa-experience-acceptance.json")


def _snapshot() -> dict:
    with open(SNAPSHOT, encoding="utf-8") as handle:
        return json.load(handle)


def _report() -> dict:
    with open(REPORT, encoding="utf-8") as handle:
        return json.load(handle)


class AcceptanceToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = _snapshot()
        cls.committed = _report()

    def test_snapshot_is_a_readonly_export_with_a_documented_projection(self):
        source = self.snapshot.get("source") or {}
        self.assertIn("readonly", str(source.get("access") or "").casefold())
        self.assertTrue(source.get("stage_projection_keys"), "阶段投影口径必须随快照留档")
        self.assertEqual(source.get("snapshot_version"), "qa-experience-real-sample-v1")
        self.assertEqual(self.snapshot.get("errors") or [], [])

    def test_snapshot_is_the_real_machine_before_state(self):
        # A 机（Phase 12 未发布）：两张投影表根本不存在，记忆表 0 行 —— 这就是 before 态
        self.assertEqual(self.snapshot.get("phase12_tables_present"), [])
        for name in ("source_reliability_memory", "skill_performance_memory"):
            self.assertIn(name, self.snapshot.get("missing_tables") or [])
        catalog = self.snapshot.get("catalog") or {}
        self.assertEqual(int(catalog.get("memory_item") or 0), 0)
        self.assertGreater(int(catalog.get("qa_runs") or 0), 0, "真机必须有真实 run 才叫真数据")

    def test_recompute_matches_the_committed_report(self):
        fresh = acc.analyze(self.snapshot)
        for key in ("report_version", "clock", "experience_version"):
            self.assertEqual(fresh[key], self.committed[key], key)
        self.assertEqual(fresh["corpus"]["six_type_counts"],
                         self.committed["corpus"]["six_type_counts"])
        self.assertEqual(fresh["corpus"]["gate"], self.committed["corpus"]["gate"])
        self.assertEqual(fresh["corpus"]["failures"], self.committed["corpus"]["failures"])
        self.assertEqual(fresh["corpus"]["strategies"], self.committed["corpus"]["strategies"])
        self.assertEqual(fresh["corpus"]["skills"], self.committed["corpus"]["skills"])
        self.assertEqual(fresh["corpus"]["provenance"], self.committed["corpus"]["provenance"])
        self.assertEqual(fresh["leave_one_out"]["changed"],
                         self.committed["leave_one_out"]["changed"])
        self.assertEqual(fresh["leave_one_out"]["baseline_routes"],
                         self.committed["leave_one_out"]["baseline_routes"])
        self.assertEqual(fresh["leave_one_out"]["after_routes"],
                         self.committed["leave_one_out"]["after_routes"])
        self.assertEqual(fresh["acceptance"], self.committed["acceptance"])

    def test_two_fresh_runs_are_identical(self):
        first = acc.analyze(self.snapshot)
        second = acc.analyze(self.snapshot)
        self.assertEqual(first["corpus"]["six_type_counts"], second["corpus"]["six_type_counts"])
        self.assertEqual(first["leave_one_out"]["changed"], second["leave_one_out"]["changed"])
        self.assertEqual(first["determinism"], second["determinism"])
        for key, value in first["determinism"].items():
            self.assertTrue(value, key)

    def test_every_acceptance_flag_is_true(self):
        for key, value in (self.committed.get("acceptance") or {}).items():
            self.assertTrue(value, "验收闸门 %s 没通过" % key)

    def test_six_types_all_have_rows_on_real_data(self):
        counts = self.committed["corpus"]["six_type_counts"]
        for name in acc.SIX_TYPES:
            self.assertGreater(int(counts.get(name) or 0), 0, name)

    def test_before_after_comparison_is_meaningful(self):
        loop = self.committed["leave_one_out"]
        self.assertGreater(int(loop["gaps"]), 0)
        self.assertGreater(int(loop["changed"]), 0, "经验必须真的改变了通道选择")
        self.assertTrue(loop["changed_by_reason"])
        # 被避免的通道在指导后从建议集合里消失（前后对比的第二个数字）
        self.assertGreater(int(loop["avoided_in_baseline"]), 0)
        self.assertEqual(int(loop["avoided_after"]), 0)
        # 版本门真的拦下了跨版本压制（MASTER_RULES 15 在真数据上生效）
        self.assertGreater(int(loop["blocked_by_version"]), 0)
        reasons = loop["retry_reasons"]
        self.assertIn("CORPUS_VERSION_CHANGED", reasons)

    def test_provenance_is_complete_and_hints_only(self):
        self.assertEqual(int(self.committed["corpus"]["provenance"]["untraceable"]), 0)
        self.assertEqual(int(self.committed["corpus"]["hint_violations"]), 0)
        self.assertEqual(self.committed["corpus"]["scopes"], {"ORGANIZATION":
                                                             self.committed["corpus"]["memory_rows"]})

    def test_notes_state_the_boundaries(self):
        notes = " ".join(self.committed.get("notes") or [])
        self.assertIn("未发布", notes)
        self.assertIn("留一法", notes)
        self.assertIn("corpus_version", notes)


if __name__ == "__main__":
    unittest.main()

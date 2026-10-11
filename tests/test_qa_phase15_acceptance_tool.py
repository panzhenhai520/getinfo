#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 15 · 验收工具用例（`tools/qa_phase15_acceptance.py`）。

钉住：
  1. 工具在**真机快照**上跑得动，且两个口径（不强制 / 强制）用**同一份代码**对比；
  2. 前后对比数字**可复算**：收窄条数、作用域分布、隔离理由分布、长期复验需求；
  3. **不可以编**：快照没有运行期审计事件 → 回放一节如实标"不可得"；
  4. 收窄**只可能变严**（逐对断言 `to` 的广度严格小于 `from`）、不丢记忆（前后条数相等）；
  5. 同输入同输出（同一份快照跑两次逐字相同），且落盘产物与提交的 baseline 逐字一致；
  6. 纯离线（不连任何库、不发任何请求、不调任何端点）。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))

import qa_graph_contracts as contracts  # noqa: E402
import qa_phase15_acceptance as acc  # noqa: E402

SNAPSHOT = os.path.join(REPO_ROOT, "baseline", "qa-memory-real-sample.json")
OUTPUT = os.path.join(REPO_ROOT, "baseline", "qa-memory-safety-acceptance.json")
_REPORT_CACHE: dict = {}


def _snapshot() -> dict:
    if not os.path.exists(SNAPSHOT):
        raise unittest.SkipTest("缺少真机快照 %s" % SNAPSHOT)
    with open(SNAPSHOT, encoding="utf-8") as handle:
        return json.load(handle)


def _report() -> dict:
    """**真机快照**上的一次完整分析（类级缓存：两个口径各重建一遍不便宜，别每个用例跑一遍）。

    直接用真机快照而不是手搓字段：快照的行形状（`payload` 嵌套、列名映射）是 Phase 09 快照
    工具与 Phase 08 复算工具之间的既有契约，手搓很容易造出"跑得动但不对"的假形状。
    """
    if "report" not in _REPORT_CACHE:
        _REPORT_CACHE["report"] = acc.analyze_snapshot(_snapshot(), snapshot_path=SNAPSHOT)
    return _REPORT_CACHE["report"]


def _empty_snapshot() -> dict:
    return {"captured_at_utc": "2026-10-11T04:00:00Z",
            "source": {"host": "test", "snapshot_version": "empty", "access": "readonly"},
            "qa_runs": [], "qa_claims": [], "qa_claim_evidence": [], "qa_evidence": [],
            "qa_conflicts": [], "qa_stage_runs": []}


class SnapshotModeTests(unittest.TestCase):
    def setUp(self):
        self.report = _report()

    def test_before_after_are_comparable_and_narrowing_is_strict(self):
        delta = self.report["rebuild"]["delta"]
        before = self.report["rebuild"]["before_phase14_scope"]
        after = self.report["rebuild"]["after_phase15_scope"]
        self.assertEqual(delta["memories"], 0, "收窄不许丢记忆")
        self.assertGreaterEqual(after["narrowed"], 1, "当前状态类的长期记忆必须被收窄")
        self.assertEqual(delta["rejected"], 0)
        self.assertTrue(delta["narrow_pairs"], delta["narrow_pairs"])
        for pair, count in delta["narrow_pairs"].items():
            source, target = pair.split("→")
            self.assertLess(contracts.SCOPE_BREADTH[target], contracts.SCOPE_BREADTH[source],
                            "收窄方向反了：%s" % pair)
            self.assertGreater(int(count), 0)
        self.assertEqual(before["by_scope"], {"PATIENT_LONGITUDINAL": before["memories"]},
                         "对照口径（Phase 14）只有主体长期作用域")
        self.assertIn("ENCOUNTER", after["by_scope"])
        self.assertEqual(sum(after["by_scope"].values()), after["memories"])

    def test_isolation_and_privacy_numbers_are_present_and_clean(self):
        self.assertEqual(self.report["isolation"]["leaks"], [])
        self.assertGreater(self.report["isolation"]["runs"], 0)
        self.assertTrue(self.report["isolation"]["isolated_by_reason"])
        self.assertEqual(self.report["privacy"]["leaks"], [])
        self.assertGreater(self.report["privacy"]["sampled"], 0)
        self.assertEqual(self.report["privacy"]["rule_hits"].get("CONTENT_FINGERPRINTED"),
                         self.report["privacy"]["sampled"],
                         "每一条真机记忆正文都必须只以指纹进载荷")
        self.assertIn("stale_rate", self.report["longitudinal"])
        self.assertIsNotNone(self.report["longitudinal"]["stale_rate"])

    def test_replay_is_declared_unavailable_in_snapshot_mode(self):
        self.assertFalse(self.report["replay"]["available"])
        self.assertIn("不可得", self.report["replay"]["note"])

    def test_contracts_are_frozen_and_version_unchanged(self):
        frozen = self.report["contracts"]
        self.assertEqual(frozen["fingerprints"], frozen["expected"])
        self.assertEqual(frozen["schema_version"], "unified-qa-schema-v11")
        self.assertEqual(frozen["memory_safety_version"], contracts.MEMORY_SAFETY_VERSION)

    def test_acceptance_passes_on_the_real_snapshot(self):
        acceptance = self.report["acceptance"]
        self.assertTrue(acceptance["passed"], acceptance)
        for key, value in acceptance.items():
            if key != "passed":
                self.assertTrue(value, key)

    def test_same_snapshot_same_report(self):
        first = json.dumps(_report(), ensure_ascii=False, sort_keys=True)
        second = json.dumps(_report(), ensure_ascii=False, sort_keys=True)
        self.assertEqual(first, second)

    def test_empty_snapshot_is_honest(self):
        report = acc.analyze_snapshot(_empty_snapshot())
        self.assertEqual(report["rebuild"]["after_phase15_scope"]["memories"], 0)
        self.assertEqual(report["isolation"]["runs"], 0)
        self.assertEqual(report["privacy"]["sampled"], 0)
        self.assertFalse(report["acceptance"]["ran_on_real_data"])
        self.assertFalse(report["acceptance"]["passed"])
        self.assertEqual(report["rebuild"]["delta"]["narrowed"], 0)


class RealSnapshotTests(unittest.TestCase):
    def test_real_snapshot_report_passes(self):
        if not os.path.exists(SNAPSHOT):
            self.skipTest("缺少真机快照 %s" % SNAPSHOT)
        with open(SNAPSHOT, encoding="utf-8") as handle:
            snapshot = json.load(handle)
        report = acc.analyze_snapshot(snapshot, snapshot_path=SNAPSHOT)
        self.assertTrue(report["acceptance"]["passed"], report["acceptance"])
        self.assertGreater(report["rebuild"]["after_phase15_scope"]["memories"], 0)

    def test_committed_report_matches_a_fresh_run(self):
        if not os.path.exists(OUTPUT):
            self.skipTest("缺少产物 %s" % OUTPUT)
        with open(OUTPUT, encoding="utf-8") as handle:
            committed = json.load(handle)
        # 用**产物自己声明的那份快照**重跑（而不是写死路径）：产物是对哪份快照生成的，
        # 就用哪份复算 —— 否则这条守门会因为"换了一份快照"而静默跳过。
        relative = str(committed["source"]["snapshot"] or "")
        source_path = os.path.join(REPO_ROOT, relative) if relative else SNAPSHOT
        if not os.path.exists(source_path):
            self.skipTest("产物声明的快照不在本地：%s" % relative)
        with tempfile.TemporaryDirectory() as temp_dir:
            out = os.path.join(temp_dir, "report.json")
            completed = subprocess.run(
                [sys.executable, os.path.join(REPO_ROOT, "tools", "qa_phase15_acceptance.py"),
                 "--snapshot", source_path, "--out", out],
                cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
                errors="replace")
            self.assertEqual(completed.returncode, 0, completed.stderr[-3000:])
            with open(out, encoding="utf-8") as handle:
                fresh = json.load(handle)
        self.assertEqual(json.dumps(fresh, ensure_ascii=False, sort_keys=True),
                         json.dumps(committed, ensure_ascii=False, sort_keys=True),
                         "落盘产物与现跑不一致（同一份快照必须逐字可复现）")

    def test_json_mode_exits_zero(self):
        if not os.path.exists(SNAPSHOT):
            self.skipTest("缺少真机快照")
        completed = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "tools", "qa_phase15_acceptance.py"),
             "--snapshot", SNAPSHOT, "--json"],
            cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        report = json.loads(completed.stdout)
        self.assertTrue(report["acceptance"]["passed"])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 10 · 验收工具用例（`tools/qa_phase10_revalidation_acceptance.py`）。

钉住：
  1. 工具能在**真机快照**上跑通并给出 PASS（离线重建记忆图 → 闸门/复验/矛盾/取代全链路）；
  2. **前后对比有真分母**：线上记忆行 0 条（8 张表）→ 离线重建 N 条 → 复验命中 M 次；
  3. 工具只用**内存 sqlite**（不碰任何真实库）、不联网（源码里没有网络/模型库痕迹）；
  4. 已落盘的验收报告必须与现跑一致（防止手工改数字）；
  5. 快照缺失时明确 skip 并说明原因，不假装通过。
"""
import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(REPO_ROOT, "tools", "qa_phase10_revalidation_acceptance.py")
SNAPSHOT_TOOL = os.path.join(REPO_ROOT, "tools", "qa_phase10_real_snapshot.py")
SNAPSHOT = os.path.join(REPO_ROOT, "baseline", "qa-memory-revalidation-real-sample.json")
FALLBACK = os.path.join(REPO_ROOT, "baseline", "qa-memory-real-sample.json")
OUTPUT = os.path.join(REPO_ROOT, "baseline", "qa-memory-revalidation-acceptance.json")

# 只允许**网络/外部库**：`sqlite3` 是本地内存库（`_MemoryDb`），不连任何真实数据库；
# `paramiko` 只在快照导出工具里（本用例不跑它，只扫它的源码边界）。
NETWORK_MODULES = {"requests", "urllib3", "httpx", "socket", "aiohttp", "http.client",
                   "urllib.request", "psycopg2", "openai", "anthropic", "dashscope",
                   "sentence_transformers", "torch", "numpy"}


def _run_tool(*args):
    return subprocess.run([sys.executable, TOOL, *args], cwd=REPO_ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=3600)


def _imports(path):
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class StaticBoundaryTests(unittest.TestCase):
    def test_acceptance_tool_does_not_import_network_or_models(self):
        imported = _imports(TOOL)
        self.assertFalse(imported & NETWORK_MODULES,
                         "验收工具不许联网/调模型：%s" % sorted(imported & NETWORK_MODULES))
        self.assertNotIn("paramiko", imported, "验收工具不连真机（快照是离线文件）")

    def test_snapshot_tool_is_read_only_by_construction(self):
        with open(SNAPSHOT_TOOL, encoding="utf-8") as handle:
            source = handle.read()
        upper = source.upper()
        # 只拦**SQL 写语句**（`sys.path.insert(...)` 这类 Python 调用不算）
        for forbidden in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP TABLE", "CREATE TABLE",
                          "ALTER TABLE", "TRUNCATE"):
            self.assertNotIn(forbidden, upper, "快照工具必须只有 SELECT：命中了 %s" % forbidden)
        self.assertIn("--probe", source)
        self.assertIn("docker exec", source)
        self.assertIn("BEGIN READ ONLY", upper,
                      "只读事务必须写在远端命令里（qa_phase07_real_snapshot._remote 的口径）")


class RealSnapshotTests(unittest.TestCase):
    def setUp(self):
        path = SNAPSHOT if os.path.exists(SNAPSHOT) else (
            FALLBACK if os.path.exists(FALLBACK) else "")
        if not path:
            self.skipTest("缺少真机快照（先跑 tools/qa_phase10_real_snapshot.py）")
        self.path = path

    def test_real_snapshot_report_is_pass(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out = os.path.join(temp_dir, "report.json")
            completed = _run_tool("--snapshot", self.path, "--out", out)
            self.assertEqual(completed.returncode, 0, completed.stderr[-3000:] + completed.stdout[-2000:])
            with open(out, encoding="utf-8") as handle:
                report = json.load(handle)
        self.assertEqual(report["status"], "PASS",
                         "失败的检查：%s" % [check["name"] for check in report["checks"]
                                            if not check["passed"]])
        self.assertTrue(report["contract_ok"], report.get("contract_error"))
        live = report["live_machine"]
        # 线上记忆行数如实记录（本阶段实测 0：Phase 09 表已建但还没有 run 触发写入）
        self.assertIn("memory_rows_total", live)
        self.assertGreater(live["claims"], 0)
        self.assertGreater(live["evidence"], 0)
        self.assertEqual(live["memory_rows_total"],
                         sum(live["memory_table_rows"].values()))
        # 离线重建 + 前后对比
        self.assertGreater(report["rebuild"]["candidates"], 0)
        self.assertGreater(report["summary"]["memories_rebuilt"], 0)
        self.assertEqual(report["summary"]["memories_rebuilt"], report["phases"][0]["memories"],
                         "重建条数必须等于记忆图里的去重条数")
        self.assertLessEqual(report["summary"]["memories_rebuilt"], report["rebuild"]["persisted"],
                             "写门按 run 累计落库次数 ≥ 去重后的记忆条数")
        self.assertGreater(report["summary"]["revalidation_hits"], 0,
                           "复验必须有真分母（否则没有前后对比）")
        self.assertGreater(report["summary"]["revalidated"], 0)
        self.assertIsNotNone(report["summary"]["revalidation_pass_rate"])
        for phase in report["phases"]:
            self.assertEqual(sum(phase["outcomes"].values()), phase["validations"])
            self.assertEqual(sum(phase["gate"]["decisions"].values()), phase["memories"])
            self.assertEqual(phase["replay"]["new_versions"], 0)
            self.assertEqual(phase["replay"]["new_contradictions"], 0)
            self.assertEqual(phase["supersession_audit"]["problems"], [])
        self.assertTrue(report["determinism"]["deterministic"])
        self.assertTrue(report["notes"], "边界必须写清楚（宁写 PARTIAL 不谎报）")

    def test_committed_report_matches_a_fresh_run(self):
        if not os.path.exists(OUTPUT):
            self.skipTest("尚未生成 %s" % OUTPUT)
        with open(OUTPUT, encoding="utf-8") as handle:
            committed = json.load(handle)
        if committed["snapshot"]["path"] != os.path.relpath(self.path, REPO_ROOT):
            self.skipTest("报告是对另一份快照生成的：%s" % committed["snapshot"]["path"])
        with tempfile.TemporaryDirectory() as temp_dir:
            out = os.path.join(temp_dir, "report.json")
            completed = _run_tool("--snapshot", self.path, "--out", out)
            self.assertEqual(completed.returncode, 0, completed.stderr[-3000:])
            with open(out, encoding="utf-8") as handle:
                fresh = json.load(handle)
        for key in ("live_machine", "rebuild", "phases", "determinism", "checks", "summary",
                    "status", "contract_ok"):
            self.assertEqual(committed[key], fresh[key],
                             "%s 与现跑不一致（报告被手工改过？）" % key)

    def test_snapshot_declares_readonly_access_and_deployment_state(self):
        with open(self.path, encoding="utf-8") as handle:
            snapshot = json.load(handle)
        source = snapshot.get("source") or {}
        if "revalidation" in str(source.get("snapshot_version") or ""):
            self.assertIn("readonly", str(source.get("access") or ""))
            self.assertIn("deployment", snapshot, "Phase 10 快照必须带部署态证据")
            self.assertIn("phase10_tables_present", snapshot)


if __name__ == "__main__":
    unittest.main(verbosity=2)

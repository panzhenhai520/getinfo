#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 13 · 验收工具用例（`tools/qa_phase13_final_acceptance.py`）。

钉住：
  1. **可复算**：同一份快照跑两次，报告逐字相同（时钟取快照 `captured_at_utc`，不用墙上钟）；
  2. **落盘产物与现跑一致**：`baseline/qa-final-acceptance.json` 必须等于现在重算的结果
     （谁偷偷改了口径，这条立刻红）；
  3. **验收闸门全绿**：真机最终答案被真的读进来终验、引用 100% 可回溯、六问全部能被触发、
     局部重试零检索、证据不足都有依据；
  4. **前后对比真的有数字**：绑定覆盖率、阻断级 before/after、答案形态迁移、重试作用域分布；
  5. **工具只读快照**：不联网、不连库（源码里没有网络/数据库库与端点痕迹）。
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
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
TOOL = os.path.join(REPO_ROOT, "tools", "qa_phase13_final_acceptance.py")
SNAPSHOT = os.path.join(REPO_ROOT, "baseline", "qa-context-real-sample.json")
OUTPUT = os.path.join(REPO_ROOT, "baseline", "qa-final-acceptance.json")

NETWORK_MODULES = {"requests", "urllib3", "httpx", "socket", "aiohttp", "http.client",
                   "paramiko", "psycopg2", "sqlite3"}

import qa_phase13_final_acceptance as acc  # noqa: E402  （与命令行同一条代码路径）


def _run_tool(*args):
    return subprocess.run([sys.executable, TOOL, *args], cwd=REPO_ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=1800)


def _report(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class SourceGuardTests(unittest.TestCase):
    def test_tool_is_readonly_and_offline(self):
        with open(TOOL, encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported & NETWORK_MODULES, set(),
                         "验收工具不许联网/连库：%s" % (imported & NETWORK_MODULES))
        for token in ("http://", "https://", "docker exec", "password", "psql"):
            self.assertNotIn(token, source, "验收工具里出现 %s" % token)

    def test_tool_reads_the_snapshot_clock(self):
        with open(TOOL, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("captured_at_utc", source)
        self.assertNotIn("datetime.now(", source, "时钟必须取快照，不许用墙上钟")


@unittest.skipUnless(os.path.exists(SNAPSHOT), "缺少真机快照")
class RealSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot_path = SNAPSHOT
        cls.committed = _report(OUTPUT) if os.path.exists(OUTPUT) else {}

    def test_report_is_deterministic(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first = os.path.join(temp_dir, "a.json")
            second = os.path.join(temp_dir, "b.json")
            for path in (first, second):
                completed = _run_tool("--snapshot", self.snapshot_path, "--out", path)
                self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
            with open(first, encoding="utf-8") as handle:
                left = handle.read()
            with open(second, encoding="utf-8") as handle:
                right = handle.read()
        self.assertEqual(left, right, "同一份快照必须产出逐字相同的报告")

    def test_committed_report_matches_a_fresh_run(self):
        self.assertTrue(self.committed, "缺少落盘报告：%s" % OUTPUT)
        fresh = acc.analyze({**_report(self.snapshot_path), "__path__": self.snapshot_path})
        for key in ("report_version", "clock", "params", "totals", "injection", "acceptance"):
            self.assertEqual(json.dumps(fresh[key], ensure_ascii=False, sort_keys=True),
                             json.dumps(self.committed[key], ensure_ascii=False, sort_keys=True),
                             key)

    def test_acceptance_gates_are_green(self):
        acceptance = self.committed.get("acceptance") or {}
        self.assertTrue(acceptance.get("passed"), json.dumps(acceptance, ensure_ascii=False))
        for gate in ("real_answers_scored", "bindings_are_traceable", "six_checks_all_reachable",
                     "retry_never_retrieves", "no_verification_errors",
                     "insufficient_has_a_basis", "no_evidence_is_marked_insufficient"):
            self.assertTrue(acceptance.get(gate), gate)

    def test_before_and_after_numbers_exist(self):
        totals = self.committed["totals"]
        self.assertGreater(totals["runs_scored"], 0)
        self.assertGreater(totals["claims"], 0)
        self.assertGreater(totals["bindings"], 0)
        self.assertEqual(totals["unbound_in_answer"], 0,
                         "真机答案里的结论必须全部绑得上可回溯证据")
        self.assertEqual(totals["citation_traceable_rate"], 1.0)
        self.assertEqual(totals["retrieval_calls"], 0, "局部重试零检索")
        self.assertEqual(totals["verify_errors"], 0)
        # 前后对比：六问失败分布与阻断级 before/after 都要有真实数字
        self.assertEqual(set(self.committed["params"]["checks"]),
                         {"FACT_WITHOUT_EVIDENCE", "EVIDENCE_NOT_SUPPORTING", "OVER_INFERENCE",
                          "COUNTER_EVIDENCE_MISSING", "CITATION_MISBOUND", "SPECULATION_AS_FACT"})
        self.assertIn("check_failures", totals)
        self.assertIn("blocking_before", totals)
        self.assertIn("blocking_after", totals)
        self.assertIn("status_moves", totals)
        self.assertIn("retry_scopes", totals)
        self.assertLessEqual(totals["blocking_after"], totals["blocking_before"] + totals["runs_scored"])

    def test_six_questions_are_all_reachable_on_real_answer_shapes(self):
        injection = self.committed["injection"]
        self.assertEqual(len(injection), 6, json.dumps(injection, ensure_ascii=False))
        for name, row in injection.items():
            self.assertGreater(row["cases"], 0, name)
            self.assertEqual(row["detect_rate"], 1.0, "%s 必须被全部检出" % name)
            self.assertEqual(row["errors"], 0, name)
            self.assertIn("inapplicable", row, "不适用样本要单独记，不许混进分母")
        targets = {row["target_check"] for row in self.committed["injection_rows"]}
        self.assertEqual(targets, set(self.committed["params"]["checks"]))

    def test_insufficient_verdicts_always_carry_a_hard_reason(self):
        hard = {"NO_EVIDENCE", "NO_VERIFIED_CLAIM", "GROUNDING_BLOCKED"}
        rows = [row for row in self.committed["runs"] if (row.get("after") or {}).get("insufficient")]
        self.assertTrue(rows, "真机上应当至少有一条 run 触发证据不足（否则这条门没有分母）")
        for row in rows:
            self.assertTrue(hard & set(row["after"]["insufficient_reasons"]), row["run_id"])


if __name__ == "__main__":
    unittest.main()

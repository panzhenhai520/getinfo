#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 09 验收工具的**时钟可复现性**守门用例（跨天红绿循环的修复证据）。

背景（Phase 10 的 D-038）：`tools/qa_phase09_memory_acceptance.py` 的离线重建原先用**墙上时钟**
给证据打时间（`annotate_evidence` 的 `retrieved_at` 与 `verify_evidence_batch` 的 `now`），
而 Phase 03 的时效维度按"天"变 —— 于是同一份快照在不同天算出的证据分不同，
落盘的 `baseline/qa-memory-acceptance.json` 与现跑差 2 个小数位（0.218473 vs 0.218436），
`test_committed_report_matches_a_fresh_run` 会在**跨天**时变红。

修复：基准时刻 = **快照抓取时刻**（`set_baseline_clock()` 钉死，缺失时退到常量
`FALLBACK_BASELINE_CLOCK`），且记录进产物的 `clock` 段。

本用例钉住三件事（都能在"有人把时钟改回墙上时间"时变红）：
  1. **两个不同的假"现在"**（`QA_P09_FAKE_NOW`）各跑一遍，产物**逐字相同**
     （只有 `generated_at_utc` 与快照路径允许不同）；
  2. 产物仍然**等于已落盘的基线**（若时钟被改回墙上时间，产物会跟着"现在"漂 → 不再等于基线）；
  3. **基准时刻真的进了数字**（控制组：把 `QA_P09_BASELINE_CLOCK` 挪一天，产物必须不同）——
     否则第 1 条会因为"时钟根本不影响结果"而变成空断言。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(REPO_ROOT, "tools", "qa_phase09_memory_acceptance.py")
SNAPSHOT = os.path.join(REPO_ROOT, "baseline", "qa-memory-real-sample.json")
FALLBACK_SNAPSHOT = os.path.join(REPO_ROOT, "baseline", "qa-context-real-sample.json")
BASELINE = os.path.join(REPO_ROOT, "baseline", "qa-memory-acceptance.json")

# 产物里**允许**不同的字段（时间戳与快照路径是元信息，不参与复算）
VOLATILE_KEYS = ("generated_at_utc", "snapshot")


def _run_tool(env_extra: dict, out_path: str, snapshot: str):
    env = dict(os.environ)
    env.update(env_extra)
    completed = subprocess.run([sys.executable, TOOL, "--snapshot", snapshot, "--out", out_path],
                               cwd=REPO_ROOT, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=3600, env=env)
    if completed.returncode != 0:
        raise AssertionError("工具失败：%s\n%s" % (completed.stdout[-2000:],
                                                 completed.stderr[-2000:]))
    with open(out_path, encoding="utf-8") as handle:
        return json.load(handle)


def _comparable(report: dict) -> dict:
    return {key: value for key, value in report.items() if key not in VOLATILE_KEYS}


class BaselineClockTests(unittest.TestCase):
    def setUp(self):
        path = SNAPSHOT if os.path.exists(SNAPSHOT) else (
            FALLBACK_SNAPSHOT if os.path.exists(FALLBACK_SNAPSHOT) else "")
        if not path:
            self.skipTest("缺少真机快照（先跑 tools/qa_phase09_real_snapshot.py）")
        self.snapshot = path
        with open(path, encoding="utf-8") as handle:
            self.raw = json.load(handle)

    def test_two_different_wall_clocks_give_identical_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first = _run_tool({"QA_P09_FAKE_NOW": "2026-10-11T09:00:00Z"},
                              os.path.join(temp_dir, "a.json"), self.snapshot)
            second = _run_tool({"QA_P09_FAKE_NOW": "2027-03-05T18:00:00Z"},
                               os.path.join(temp_dir, "b.json"), self.snapshot)
        self.assertEqual(_comparable(first), _comparable(second),
                         "两个不同的假\"现在\"必须产出逐字相同的产物（时钟不许来自墙上时间）")
        self.assertEqual(first["clock"], second["clock"])

    def test_artifact_still_equals_the_committed_baseline(self):
        if not os.path.exists(BASELINE):
            self.skipTest("尚未生成 %s" % BASELINE)
        with open(BASELINE, encoding="utf-8") as handle:
            committed = json.load(handle)
        with tempfile.TemporaryDirectory() as temp_dir:
            fresh = _run_tool({"QA_P09_FAKE_NOW": "2027-12-31T23:00:00Z"},
                              os.path.join(temp_dir, "c.json"), self.snapshot)
        for key, value in _comparable(committed).items():
            if key == "snapshot":
                continue
            self.assertEqual(value, fresh.get(key),
                             "%s 与已落盘基线不一致（时钟被改回墙上时间？）" % key)

    def test_baseline_clock_is_the_snapshot_time_and_is_recorded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            report = _run_tool({}, os.path.join(temp_dir, "d.json"), self.snapshot)
        clock = report["clock"]
        captured = str(self.raw.get("captured_at_utc") or "").replace("Z", "+00:00")
        self.assertEqual(clock["baseline_clock_source"], "snapshot.captured_at_utc")
        self.assertTrue(clock["baseline_clock"].startswith(str(captured)[:19]))
        self.assertFalse(clock["wall_clock_used"], "工具里不许再读墙上时钟")
        self.assertEqual(clock["wall_clock_env"], "QA_P09_FAKE_NOW")

    def test_tool_has_no_reachable_wall_clock(self):
        """**静态守门**：墙上时钟只允许出现在两处，且两处都不参与"复算出来的数字"。

        允许的两处：
          ① `_wall_clock()` 的定义体 —— 它只服务于"进程看到的现在"，且**全工具无人调用**；
          ② `_utc_now_z()` 的定义体 —— 它只喂 `generated_at_utc`（产物里唯一允许随运行时间变的字段）。
        写成静态检查比"跑两遍比对"更直接：只要有人把任何一处改回 `datetime.now(...)`、
        或让 `_wall_clock()` 进入计算路径，本用例立刻红（不依赖当天是不是同一个"天"）。
        """
        with open(TOOL, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        source = "\n".join(lines)

        def _owner(line: str) -> str:
            """这一行属于哪个函数（往上找最近的 `def`）。"""
            index = lines.index(line)
            for back in range(index, -1, -1):
                if lines[back].startswith("def "):
                    return lines[back].split("(")[0][4:]
            return ""

        clock_lines = [line for line in lines if "datetime.now(" in line]
        self.assertEqual(len(clock_lines), 2,
                         "墙上时钟只允许出现在 _wall_clock() 与 _utc_now_z() 里：%s" % clock_lines)
        self.assertEqual({_owner(line) for line in clock_lines}, {"_wall_clock", "_utc_now_z"},
                         "墙上时钟出现在别的函数里 → 产物会随'现在'漂")
        self.assertEqual(source.count("_wall_clock("), 1,
                         "_wall_clock() 只允许有定义、不许被调用（调用=回到墙上时钟）")
        self.assertIn("baseline_clock()", source, "离线重建必须走 baseline_clock()")
        generated = [line for line in lines if '"generated_at_utc"' in line]
        self.assertTrue(generated and "_utc_now_z()" in generated[0],
                        "唯一允许的时间戳字段就是 generated_at_utc")

    def test_baseline_clock_is_a_real_input(self):
        """控制组：把基准时刻挪一天，**算出来的数字**必须不同（否则第 1 条会变成空断言）。

        比较时剔除 `clock` 元信息段：只比"数字本身"。若有人把基准时刻改成只记录不参与计算，
        或者把时钟改回墙上时间，这里就会红。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            normal = _run_tool({}, os.path.join(temp_dir, "e.json"), self.snapshot)
            shifted = _run_tool({"QA_P09_BASELINE_CLOCK": "2026-10-12T00:00:00Z"},
                                os.path.join(temp_dir, "f.json"), self.snapshot)
        numbers = lambda report: {key: value for key, value in _comparable(report).items()
                                  if key != "clock"}
        self.assertNotEqual(numbers(normal), numbers(shifted),
                            "基准时刻必须真的参与计算（挪一天应当算出不同的证据分）")
        self.assertEqual(shifted["clock"]["baseline_clock_source"], "QA_P09_BASELINE_CLOCK")
        self.assertEqual(shifted["clock"]["baseline_clock"], "2026-10-12T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main(verbosity=2)

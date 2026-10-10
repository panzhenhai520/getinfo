#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 08 · 验收工具用例（`tools/qa_phase08_context_acceptance.py`）。

钉住：
  1. 工具能在**真机快照**上跑通并给出通过结论（引用可回溯率 1.0、Context Gap 不触发检索）；
  2. 预算梯子**单调**：预算越大，保留率越高、被裁条目越少（可复算的前后对比）；
  3. grounding 拦截率有**真实分母**：真机最终答案被真的读进来校验，构造注入 100% 被拦下；
  4. 工具不联网、不连库（源码里没有网络库/端点痕迹），只读快照文件。
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
TOOL = os.path.join(REPO_ROOT, "tools", "qa_phase08_context_acceptance.py")
SNAPSHOT = os.path.join(REPO_ROOT, "baseline", "qa-context-real-sample.json")
FALLBACK = os.path.join(REPO_ROOT, "baseline", "qa-gap-real-sample.json")
OUTPUT = os.path.join(REPO_ROOT, "baseline", "qa-context-acceptance.json")

NETWORK_MODULES = {"requests", "urllib3", "httpx", "socket", "aiohttp", "http.client",
                   "urllib.request", "paramiko", "psycopg2", "sqlite3"}


def _run_tool(*args):
    completed = subprocess.run(
        [sys.executable, TOOL, *args], cwd=REPO_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=900)
    return completed


def _synthetic_snapshot(path):
    """手搓一份小快照（保险：真机快照缺失时用例仍然能跑工具本身）。"""
    evidence = [{
        "run_id": "run-synth", "evidence_ref": "article:1", "source_type": "article",
        "source_url": "https://example.com/1", "source_title": "香港家族办公室税收优惠政策",
        "published_at": "2026-01-10", "authority_level": 90,
        "payload": {"evidence_ref": "article:1", "content_excerpt": "香港家族办公室税收优惠政策对符合条件的管理人给予利得税宽免，门槛为 200 万港元。",
                    "title": "香港家族办公室税收优惠政策", "metadata": {}},
    }]
    claims = [{
        "run_id": "run-synth", "claim_key": "c1", "stage": "conflict_review",
        "claim_text": "香港家族办公室税收优惠政策对内地高净值客户有影响",
        "verification_status": "confirmed",
        "payload": {"claim_id": "c1", "text": "香港家族办公室税收优惠政策对内地高净值客户有影响",
                    "evidence_refs": ["article:1"], "claim_type": "current_fact",
                    "confidence": 0.8, "scope": ["家族办公室"]},
    }]
    edges = [{"run_id": "run-synth", "claim_key": "c1", "evidence_ref": "article:1",
              "relationship": "supports", "relevance_score": 41.0, "published_at": "2026-01-10"}]
    runs = [{
        "id": "run-synth", "question_text": "香港家族办公室税收优惠政策对内地高净值客户有什么影响？",
        "mode": "standard", "status": "completed",
        "final_answer": {"contract_version": "unified-qa-v1", "status": "ready",
                         "answer": "宽免适用于符合条件的管理人 [1]。",
                         "sections": {"summary": "宽免适用于符合条件的管理人 [1]。"},
                         "claims": [{"claim_id": "c1", "text": "税收优惠", "claim_type": "current_fact",
                                     "confidence": 0.8, "valid_from": None, "valid_to": None,
                                     "scope": [], "evidence_refs": ["article:1"],
                                     "needs_verification": False,
                                     "verification_status": "confirmed"}],
                         "conflicts": [], "evidence": [], "citations": ["article:1"],
                         "citation_map": {"[1]": "article:1"}, "cutoff_at": "2026-01-10",
                         "degraded": False, "degradation_reasons": [], "models": {}},
    }]
    payload = {"captured_at_utc": "2026-10-11T00:00:00Z",
               "source": {"host": "synthetic", "access": "readonly",
                          "snapshot_version": "qa-context-real-sample-v1"},
               "runs": runs, "claims": claims, "edges": edges, "conflicts": [],
               "evidence": evidence, "stages": []}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    return path


class ToolGuardTests(unittest.TestCase):
    def test_tool_never_touches_the_network_or_a_database(self):
        with open(TOOL, encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertEqual(imported & NETWORK_MODULES, set(),
                         "验收工具不许联网/连库：%s" % (imported & NETWORK_MODULES))
        for token in ("http://", "https://", "psql", "docker exec", "password"):
            self.assertNotIn(token, source, "验收工具里出现 %s" % token)


class SyntheticRunTests(unittest.TestCase):
    def test_tool_runs_end_to_end_on_a_synthetic_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = _synthetic_snapshot(os.path.join(temp_dir, "snap.json"))
            out = os.path.join(temp_dir, "report.json")
            completed = _run_tool("--snapshot", snapshot, "--out", out)
            self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
            with open(out, encoding="utf-8") as handle:
                report = json.load(handle)
        self.assertEqual(report["report_version"], "qa-context-acceptance-v1")
        totals = report["totals"]
        self.assertEqual(totals["runs_scored"], 1)
        self.assertEqual(totals["citations"], 1)
        self.assertEqual(totals["traceable_rate"], 1.0)
        self.assertEqual(totals["retrieval_requested"], 0)
        self.assertTrue(report["acceptance"]["passed"])
        self.assertEqual(sorted(report["budget_ladder"]), ["1200", "12000", "3000", "600", "6000"])
        injection = report["injection"]
        self.assertTrue(injection)
        for name, row in injection.items():
            self.assertGreater(row["cases"], 0, name)
            self.assertEqual(row["blocked_rate"], 1.0, "%s 的注入必须被拦下" % name)


class RealSnapshotTests(unittest.TestCase):
    """真机快照上的验收（快照缺失时明确 skip 并说明原因，不假装通过）。"""

    def setUp(self):
        path = SNAPSHOT if os.path.exists(SNAPSHOT) else (
            FALLBACK if os.path.exists(FALLBACK) else "")
        if not path:
            self.skipTest("缺少真机快照（先跑 tools/qa_phase08_real_snapshot.py）")
        self.path = path

    def test_real_snapshot_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out = os.path.join(temp_dir, "report.json")
            completed = _run_tool("--snapshot", self.path, "--out", out)
            self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
            with open(out, encoding="utf-8") as handle:
                report = json.load(handle)
        totals = report["totals"]
        self.assertGreater(totals["runs_scored"], 0)
        self.assertGreater(totals["citations"], 0)
        self.assertEqual(totals["traceable_rate"], 1.0,
                         "包内引用必须 100% 可回溯到 Phase 02 的最小 span")
        self.assertEqual(totals["retrieval_requested"], 0,
                         "Context Gap 不许触发新检索（§6 + MASTER_RULES 13）")
        self.assertTrue(report["acceptance"]["traceability_ok"])
        self.assertTrue(report["acceptance"]["budget_respected"])
        # 预算梯子单调：预算越大保留越多、裁得越少
        rungs = sorted(report["budget_ladder"].items(), key=lambda pair: int(pair[0]))
        kept = [value["tokens_after"] for _, value in rungs]
        trimmed = [value["trimmed"] for _, value in rungs]
        self.assertEqual(kept, sorted(kept), "预算越大保留的 token 不许变少")
        self.assertEqual(trimmed, sorted(trimmed, reverse=True), "预算越大裁掉的条目不许变多")
        self.assertLess(rungs[0][1]["tokens_after"], rungs[-1][1]["tokens_after"],
                        "最小预算必须真的裁掉了东西，否则前后对比没有意义")
        self.assertIn("OVER_TOKEN_BUDGET", rungs[0][1]["trim_reasons"])
        # grounding 拦截：真机分母 + 构造注入
        if totals["grounding_checked"]:
            self.assertTrue(report["injection"])
            for name, row in report["injection"].items():
                self.assertEqual(row["detected"], row["cases"],
                                 "%s 的注入必须全部被检出" % name)
        self.assertTrue(report["source"]["note"])

    def test_committed_report_matches_a_fresh_run(self):
        """已落盘的验收报告必须与现跑一致（防止手工改数字）。"""
        if not os.path.exists(OUTPUT):
            self.skipTest("尚未生成 %s" % OUTPUT)
        with open(OUTPUT, encoding="utf-8") as handle:
            committed = json.load(handle)
        if committed["source"]["snapshot"] != os.path.relpath(self.path, REPO_ROOT):
            self.skipTest("报告是对另一份快照生成的：%s" % committed["source"]["snapshot"])
        with tempfile.TemporaryDirectory() as temp_dir:
            out = os.path.join(temp_dir, "report.json")
            completed = _run_tool("--snapshot", self.path, "--out", out)
            self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
            with open(out, encoding="utf-8") as handle:
                fresh = json.load(handle)
        for key in ("params", "totals", "budget_ladder", "injection", "acceptance"):
            self.assertEqual(committed[key], fresh[key],
                             "%s 与现跑不一致（报告被手工改过？）" % key)


if __name__ == "__main__":
    unittest.main()

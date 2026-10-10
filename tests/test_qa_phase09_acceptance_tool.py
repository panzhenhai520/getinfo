#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 09 · 验收工具用例（`tools/qa_phase09_memory_acceptance.py`）。

钉住：
  1. 工具能在**真机快照**上跑通并给出通过结论（写门真的落库、provenance 100%、
     污染率 0、召回 hint 不变量成立、lifecycle 幂等且确定性）；
  2. **前后对比有真分母**：before（记忆 0 条、命中 0）→ after（落库 N 条、命中 M 条）；
  3. 工具只用**内存 sqlite**（不碰任何真实库）、不联网（源码里没有网络库/端点痕迹）；
  4. 已落盘的验收报告必须与现跑一致（防止手工改数字）。
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
TOOL = os.path.join(REPO_ROOT, "tools", "qa_phase09_memory_acceptance.py")
SNAPSHOT = os.path.join(REPO_ROOT, "baseline", "qa-memory-real-sample.json")
FALLBACK = os.path.join(REPO_ROOT, "baseline", "qa-context-real-sample.json")
OUTPUT = os.path.join(REPO_ROOT, "baseline", "qa-memory-acceptance.json")

# 只允许**网络/外部库**：`sqlite3` 是本地内存库（`_MemoryDb`），不连任何真实数据库。
NETWORK_MODULES = {"requests", "urllib3", "httpx", "socket", "aiohttp", "http.client",
                   "urllib.request", "paramiko", "psycopg2"}


def _run_tool(*args):
    return subprocess.run([sys.executable, TOOL, *args], cwd=REPO_ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=1800)


def _synthetic_snapshot(path):
    """手搓一份小快照（真机快照缺失时用例仍然能跑工具本身）。"""
    runs = [{
        "id": "run-synth", "question_text": "香港家族办公室税收优惠政策对内地高净值客户有什么影响？",
        "mode": "standard", "status": "completed", "industry_pack_id": "auto",
        "owner_user_id": "user:1", "session_id": "s-synth", "corpus_version": "corpus-synth",
    }]
    evidence = [
        {"run_id": "run-synth", "evidence_ref": "article:1", "source_type": "article",
         "article_id": 1, "source_url": "https://example.com/1",
         "source_title": "香港家族办公室税收优惠政策",
         "published_at": "2026-04-01", "authority_level": 90,
         "payload": {"evidence_ref": "article:1", "title": "香港家族办公室税收优惠政策",
                     "source_type": "article", "article_id": 1, "authority_level": 90,
                     "published_at": "2026-04-01", "authority": 90, "metadata": {},
                     "content_excerpt": "香港家族办公室税收优惠政策对合资格基金管理人给予利得税宽免，"
                                        "政策自 2026 年 4 月 1 日起生效，门槛为 200 万港元。"}},
        {"run_id": "run-synth", "evidence_ref": "article:9", "source_type": "article",
         "article_id": 9, "source_url": "https://example.com/9",
         "source_title": "某论坛闲谈", "published_at": "2026-04-02", "authority_level": 20,
         "payload": {"evidence_ref": "article:9", "title": "某论坛闲谈", "source_type": "article",
                     "article_id": 9, "authority_level": 20, "published_at": "2026-04-02",
                     "metadata": {},
                     "content_excerpt": "论坛里有人随便聊了几句天气与球赛，没有提到任何政策。"}},
    ]
    claims = [
        {"run_id": "run-synth", "claim_key": "c1", "stage": "conflict_review",
         "claim_text": "香港家族办公室税收优惠政策对合资格基金管理人给予利得税宽免",
         "verification_status": "confirmed",
         "payload": {"claim_id": "c1", "plan_only": False,
                     "text": "香港家族办公室税收优惠政策对合资格基金管理人给予利得税宽免",
                     "claim_type": "policy", "confidence": 0.85, "valid_from": "2026-04-01",
                     "valid_to": None, "scope": ["家族办公室"], "evidence_refs": ["article:1"],
                     "needs_verification": True, "verification_status": "confirmed"}},
        {"run_id": "run-synth", "claim_key": "c2", "stage": "conflict_review",
         "claim_text": "某论坛认为球赛很精彩",
         "verification_status": "unverified",
         "payload": {"claim_id": "c2", "plan_only": False, "text": "某论坛认为球赛很精彩",
                     "claim_type": "background", "confidence": 0.2, "valid_from": None,
                     "valid_to": None, "scope": [], "evidence_refs": ["article:9"],
                     "needs_verification": True, "verification_status": "unverified"}},
    ]
    edges = [
        {"run_id": "run-synth", "claim_key": "c1", "evidence_ref": "article:1",
         "relationship": "supports", "relevance_score": 41.0},
        {"run_id": "run-synth", "claim_key": "c2", "evidence_ref": "article:9",
         "relationship": "supports", "relevance_score": 3.0},
    ]
    payload = {"captured_at_utc": "2026-10-11T00:00:00Z",
               "source": {"host": "synthetic", "access": "readonly",
                          "snapshot_version": "qa-memory-real-sample-v1"},
               "catalog": {"qa_runs": 1, "qa_claims": 2, "qa_evidence": 2},
               "missing_tables": [], "memory_tables_present": [],
               "qa_runs": runs, "qa_claims": claims, "qa_claim_evidence": edges,
               "qa_evidence": evidence, "qa_conflicts": [],
               "qa_evidence_seen": [{"owner_user_id": "user:1", "session_id": "s-synth",
                                     "industry_pack_id": "auto",
                                     "source_fingerprint": "SF-provided",
                                     "status": "confirmed", "seen_count": 1}]}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    return path


class ToolGuardTests(unittest.TestCase):
    def test_tool_never_touches_the_network_or_a_real_database(self):
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
                         "验收工具不许联网：%s" % (imported & NETWORK_MODULES))
        for token in ("http://", "https://", "psql", "docker exec", "password"):
            self.assertNotIn(token, source, "验收工具里出现 %s" % token)
        self.assertIn(":memory:", source, "验收工具必须用内存库（不碰真实库）")


class SyntheticRunTests(unittest.TestCase):
    def test_tool_runs_end_to_end_on_a_synthetic_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = _synthetic_snapshot(os.path.join(temp_dir, "snap.json"))
            out = os.path.join(temp_dir, "report.json")
            completed = _run_tool("--snapshot", snapshot, "--out", out)
            self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
            with open(out, encoding="utf-8") as handle:
                report = json.load(handle)
        self.assertEqual(report["report_version"], "qa-memory-acceptance-v1")
        self.assertEqual(report["real_counts"]["runs"], 1)
        self.assertGreater(report["write_gate"]["candidates"], 0)
        self.assertTrue(report["checks"])
        self.assertTrue(report["acceptance"]["passed"],
                        "构造快照上的验收也该通过：%s" % report["acceptance"]["failed"])
        self.assertEqual(report["provenance"]["traceable"], report["provenance"]["checked"])
        self.assertEqual(report["pollution"]["unsupported_evidence_links"], 0)
        # 没通过核验的那条 claim 只能被 DROP（MASTER_RULES 11）
        self.assertIn("NO_VERIFIED_EVIDENCE", report["write_gate"]["reason_counts"])
        self.assertEqual(report["recall"]["before"]["hits_total"], 0)
        self.assertIn("0.05", report["recall"]["after"]["threshold_sensitivity"])

    def test_report_fails_loudly_when_the_snapshot_is_empty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "empty.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"captured_at_utc": "2026-10-11T00:00:00Z", "source": {},
                           "qa_runs": [], "qa_claims": [], "qa_claim_evidence": [],
                           "qa_evidence": [], "qa_conflicts": [], "qa_evidence_seen": []}, handle)
            out = os.path.join(temp_dir, "report.json")
            completed = _run_tool("--snapshot", path, "--out", out)
            with open(out, encoding="utf-8") as handle:
                report = json.load(handle)
        self.assertEqual(completed.returncode, 1, "空快照必须**如实失败**，不许假装通过")
        self.assertFalse(report["acceptance"]["passed"])
        self.assertIn("write_gate_produces_memories", report["acceptance"]["failed"])


class RealSnapshotTests(unittest.TestCase):
    """真机快照上的验收（快照缺失时明确 skip 并说明原因，不假装通过）。"""

    def setUp(self):
        path = SNAPSHOT if os.path.exists(SNAPSHOT) else (
            FALLBACK if os.path.exists(FALLBACK) else "")
        if not path:
            self.skipTest("缺少真机快照（先跑 tools/qa_phase09_real_snapshot.py）")
        self.path = path

    def test_real_snapshot_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out = os.path.join(temp_dir, "report.json")
            completed = _run_tool("--snapshot", self.path, "--out", out)
            self.assertEqual(completed.returncode, 0, completed.stderr[-3000:])
            with open(out, encoding="utf-8") as handle:
                report = json.load(handle)
        self.assertTrue(report["acceptance"]["passed"],
                        "真机验收失败项：%s" % report["acceptance"]["failed"])
        self.assertGreater(report["real_counts"]["claims"], 0)
        self.assertGreater(report["write_gate"]["candidates"], 0)
        self.assertGreater(report["write_gate"]["persisted"], 0)
        # 前后对比：before 侧必须是"真实机器上现在真的没有记忆"
        self.assertEqual(report["before_after"]["recall_hits_before"], 0)
        after = report["recall"]["after"]
        self.assertGreater(after["hits_total"], 0, "记忆写完之后必须真的能召回（否则没有前后对比）")
        self.assertEqual(after["hint_violations"], 0)
        self.assertEqual(after["non_active_hits"], 0)
        self.assertEqual(after["planning_hits"], 0,
                         "策略/失败类记忆属 Phase 12：规划召回此刻必须为空（如实为 0）")
        self.assertEqual(report["provenance"]["traceable"], report["provenance"]["checked"])
        self.assertEqual(report["pollution"]["unsupported_evidence_links"], 0)
        self.assertEqual(report["pollution"]["memories_without_evidence_link"], 0)
        self.assertEqual(report["pollution"]["sensitive_memories"], 0)
        self.assertEqual(report["lifecycle"]["idempotent"], True)
        self.assertEqual(report["lifecycle"]["deterministic"], True)
        self.assertEqual(report["phase02_alignment"]["match_rate"], 1.0)
        # 衰减分布必须随天数变化（否则 lifecycle 等于没跑）
        bands = report["lifecycle"]["at_days"]
        self.assertNotEqual(bands["0"]["decay_bands"], bands["365"]["decay_bands"])
        self.assertEqual(sum(bands["365"]["status_counts"].values()), bands["365"]["checked"])
        self.assertTrue(report["limits"], "边界必须写清楚（宁写 PARTIAL 不谎报）")

    def test_committed_report_matches_a_fresh_run(self):
        """已落盘的验收报告必须与现跑一致（防止手工改数字）。"""
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
        for key in ("real_counts", "write_gate", "before_after", "recall", "lifecycle",
                    "provenance", "pollution", "phase02_alignment", "determinism", "checks",
                    "acceptance"):
            self.assertEqual(committed[key], fresh[key],
                             "%s 与现跑不一致（报告被手工改过？）" % key)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 11 · 验收工具用例（快照 + 同代码离线重建可复算）。

钉住：
  1. 快照解析与图谱重建走**真实** Phase 02 标注（不手写证据层）；
  2. **确定性**：同一份快照、同样参数，两次分析的 `selected`/`trace`/`records` 逐字相同
     （时钟取自快照的 `captured_at_utc`，**不碰墙上钟**）；
  3. before/after 口径成立：不给 `skill_items` 时 `skill_context` 是空段、
     `SKILL_NOT_AVAILABLE` 只能标注；给了之后能力进包、该缺口归零；
  4. 技能条目**不进 citation_map**；
  5. 工具本身零写库、零端点（源码级断言）；坏元素不炸（如实记账）。
"""
import ast
import json
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_skills as skills  # noqa: E402

import qa_phase11_skill_acceptance as tool  # noqa: E402
import qa_phase11_fixtures as fx  # noqa: E402


def _run(*, run_id="run-a", claims=3, evidence=3, verified=True, counter=False):
    """一个快照 run（字段与 `qa_phase11_real_snapshot.py` 导出的形态一致）。"""
    rows = {"claims": [], "edges": [], "evidence": []}
    for index in range(claims):
        claim_id = "l1-c%d" % (index + 1)
        rows["claims"].append({
            "claim_key": claim_id, "text": fx.CLAIM_TEXT, "claim_type": "policy",
            "confidence": 0.8, "verification_status": "confirmed" if verified else "unverified",
            "payload": {"claim_id": claim_id, "text": fx.CLAIM_TEXT,
                        "evidence_refs": ["article:%d" % (index + 1)],
                        "valid_from": "2026-04-01", "valid_to": None}})
    for index in range(evidence):
        ref = "article:%d" % (index + 1)
        rows["evidence"].append({
            "evidence_ref": ref, "source_type": "article",
            "title": "香港家族办公室税收优惠政策解读",
            "source_url": "https://example.com/%d" % (index + 1),
            "published_at": "2026-10-09", "authority_level": 60,
            "payload": {"evidence_ref": ref, "source_type": "article",
                        "title": "香港家族办公室税收优惠政策解读",
                        "content_excerpt": fx.EVIDENCE_TEXT, "score": 30.0,
                        "retrieval_method": "keyword", "relationship": "supports",
                        "metadata": {"matched_keywords": ["家族办公室"]}}})
    for index in range(min(claims, evidence)):
        rows["edges"].append({"claim_key": "l1-c%d" % (index + 1),
                              "evidence_ref": "article:%d" % (index + 1),
                              "relationship": "supports", "relevance_score": 143.11})
    if counter:
        rows["edges"].append({"claim_key": "l1-c1", "evidence_ref": "article:99",
                              "relationship": "contradicts", "relevance_score": 12.5})
    return {"run_id": run_id, "question": fx.QUESTION, "mode": "standard",
            "status": "completed", "corpus_version": "corpus:x", "created_at": "2026-10-11",
            **rows}


def _snapshot(*runs):
    rows = list(runs) or [_run()]
    return {"snapshot_version": "qa-skill-snapshot-v1",
            "captured_at_utc": "2026-10-11T02:01:50Z",
            "counts": {"runs": len(rows),
                       "claims": sum(len(row["claims"]) for row in rows),
                       "evidence": sum(len(row["evidence"]) for row in rows),
                       "edges": sum(len(row["edges"]) for row in rows)},
            "errors": [], "runs": rows}


class SnapshotRebuildTests(unittest.TestCase):
    def test_graph_is_rebuilt_from_the_snapshot(self):
        graph = tool.rebuild_graph(_run(), annotate=True)
        self.assertEqual(len(graph["claims"]), 3)
        self.assertEqual(len(graph["evidence"]), 3)
        self.assertEqual(len(graph["edges"]), 3)
        # 证据走 Phase 02 的真实标注（有 evidence_layer 与最小 span）
        layer = graph["evidence"][0]["metadata"]["evidence_layer"]
        self.assertIn("evidence_ref", layer)
        self.assertTrue(layer.get("source"))
        self.assertEqual(graph["evidence"][0]["content_excerpt"][
            layer["span"]["start"]:layer["span"]["end"]], layer["span"]["quote"])

    def test_edges_mark_verified_only_for_verified_claims(self):
        graph = tool.rebuild_graph(_run(verified=True), annotate=False)
        self.assertTrue(all(edge["metadata"]["verified"] for edge in graph["edges"]))
        graph = tool.rebuild_graph(_run(verified=False), annotate=False)
        self.assertFalse(any(edge["metadata"]["verified"] for edge in graph["edges"]))

    def test_relationship_mapping(self):
        graph = tool.rebuild_graph(_run(counter=True), annotate=False)
        relations = {edge["evidence_ref"]: edge["graph_relation"] for edge in graph["edges"]}
        self.assertEqual(relations["article:1"], "SUPPORTS")
        self.assertEqual(relations["article:99"], "REFUTES")

    def test_empty_run_does_not_crash(self):
        detail = tool.analyze_run({"run_id": "empty", "question": "空", "mode": "fast",
                                   "claims": [], "edges": [], "evidence": []})
        self.assertEqual(detail["gaps"], 0)
        self.assertEqual(detail["selected"], [])
        self.assertEqual(detail["records"], [])


class AnalyzeTests(unittest.TestCase):
    def test_before_after_is_measured_not_asserted(self):
        report = tool.analyze(_snapshot(_run(claims=3)))
        before = report["before_after"]["before"]
        after = report["before_after"]["after"]
        self.assertEqual(before["skills_loaded"], 0)
        self.assertEqual(before["skill_section_empty_runs"], 1)
        self.assertEqual(after["skill_section_empty_runs"], 0)
        self.assertGreater(after["skills_loaded"], 0)
        self.assertLessEqual(after["skill_not_available_gaps"], before["skill_not_available_gaps"])

    def test_skill_never_enters_the_citation_map(self):
        report = tool.analyze(_snapshot(_run()))
        self.assertEqual(report["skill_in_citation_map"], 0)

    def test_hit_rate_is_recomputable(self):
        report = tool.analyze(_snapshot(_run(), _run(run_id="run-b")))
        routing = report["routing"]
        self.assertEqual(routing["selected_total"], sum(routing["selected_per_skill"].values()))
        self.assertEqual(routing["hit_rate"],
                         round(routing["selected_total"] / routing["needs_total"], 6))

    def test_determinism_flags_are_true(self):
        report = tool.analyze(_snapshot(_run(), _run(run_id="run-b", claims=1, evidence=1)))
        self.assertEqual(report["determinism"],
                         {"same_selection": True, "same_trace": True, "same_records": True})

    def test_clock_comes_from_the_snapshot(self):
        snapshot = _snapshot(_run())
        first = tool.analyze(snapshot)
        snapshot["captured_at_utc"] = "2030-01-01T00:00:00Z"
        second = tool.analyze(snapshot)
        self.assertEqual(first["clock"]["baseline_utc"], "2026-10-11T02:01:50Z")
        self.assertEqual(second["clock"]["baseline_utc"], "2030-01-01T00:00:00Z")
        # 时钟只影响回执的 clock 段，判定结果逐字不变（没有墙上钟泄漏）
        self.assertEqual(first["routing"]["selected_per_skill"],
                         second["routing"]["selected_per_skill"])
        self.assertEqual(first["telemetry"]["per_skill"], second["telemetry"]["per_skill"])

    def test_telemetry_records_use_the_frozen_definition(self):
        report = tool.analyze(_snapshot(_run()))
        overall = report["telemetry"]["overall"]
        self.assertEqual(overall["success_definition"], skills.performance_summary([])["success_definition"])
        self.assertEqual(overall["attempts"],
                         sum(row["attempts"] for row in report["telemetry"]["per_skill"].values()))

    def test_report_is_json_serialisable(self):
        report = tool.analyze(_snapshot(_run()))
        self.assertTrue(json.dumps(report, ensure_ascii=False))

    def test_needed_skill_is_routed_for_a_three_claim_run(self):
        report = tool.analyze(_snapshot(_run(claims=3)))
        self.assertIn("citation_verification", report["routing"]["selected_per_skill"])
        self.assertIn("CONTEXT_GAP_LOAD_SKILL", report["routing"]["reason_distribution"])

    def test_no_need_for_a_single_claim_run(self):
        report = tool.analyze(_snapshot(_run(claims=1, evidence=1)))
        self.assertNotIn("citation_verification", report["routing"]["selected_per_skill"])

    def test_per_skill_rates_are_none_below_min_samples(self):
        report = tool.analyze(_snapshot(_run()), min_samples=99)
        for row in report["telemetry"]["per_skill"].values():
            self.assertIsNone(row["success_rate"])
            self.assertEqual(row["reason"], "INSUFFICIENT_SAMPLES")


class ToolBoundaryTests(unittest.TestCase):
    def test_analyze_writes_nothing(self):
        source = open(os.path.join(REPO_ROOT, "tools", "qa_phase11_skill_acceptance.py"),
                      encoding="utf-8").read()
        for marker in ("INSERT INTO", "CREATE TABLE", "UPDATE ", "DELETE FROM",
                       "commit()", "http://", "https://"):
            self.assertNotIn(marker, source, "验收工具不许写库/联网：%s" % marker)

    def test_snapshot_tool_is_read_only(self):
        source = open(os.path.join(REPO_ROOT, "tools", "qa_phase11_real_snapshot.py"),
                      encoding="utf-8").read()
        for marker in ("INSERT INTO", "CREATE TABLE", "UPDATE ", "DELETE FROM", "commit()"):
            self.assertNotIn(marker, source, "快照工具必须只读：%s" % marker)
        self.assertIn("LIMIT", source)
        self.assertIn("connect_database(read_only=True)", source)

    def test_tools_import_no_network_libraries(self):
        for name in ("qa_phase11_skill_acceptance.py", "qa_phase11_real_snapshot.py"):
            tree = ast.parse(open(os.path.join(REPO_ROOT, "tools", name), encoding="utf-8").read())
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names.add(node.module.split(".")[0])
            for banned in ("requests", "urllib3", "httpx", "openai", "socket"):
                self.assertNotIn(banned, names, "%s 不许 import %s" % (name, banned))

    def test_real_snapshot_is_present_and_parseable(self):
        path = os.path.join(REPO_ROOT, "baseline", "qa-skill-snapshot.json")
        if not os.path.exists(path):
            self.skipTest("未导出真机快照（离线环境）")
        with open(path, encoding="utf-8") as handle:
            snapshot = json.load(handle)
        self.assertEqual(snapshot["snapshot_version"], "qa-skill-snapshot-v1")
        self.assertTrue(snapshot["captured_at_utc"])
        self.assertGreaterEqual(snapshot["counts"]["runs"], 1)
        self.assertEqual(snapshot["errors"], [])

    def test_on_disk_report_matches_a_fresh_run(self):
        """落盘的验收报告必须与现跑一致（防止手工改数字）。"""
        snapshot_path = os.path.join(REPO_ROOT, "baseline", "qa-skill-snapshot.json")
        report_path = os.path.join(REPO_ROOT, "baseline", "qa-skill-acceptance.json")
        if not (os.path.exists(snapshot_path) and os.path.exists(report_path)):
            self.skipTest("未落盘快照/报告（离线环境）")
        with open(snapshot_path, encoding="utf-8") as handle:
            snapshot = json.load(handle)
        with open(report_path, encoding="utf-8") as handle:
            saved = json.load(handle)
        fresh = tool.analyze(snapshot, annotate=saved["parameters"]["annotate"],
                             budget_tokens=saved["parameters"]["budget_tokens"],
                             min_samples=saved["parameters"]["min_samples"])
        for key in ("report_version", "clock", "snapshot", "parameters", "before_after",
                    "routing", "telemetry", "per_skill", "skill_in_citation_map",
                    "determinism"):
            self.assertEqual(fresh[key], saved[key], "%s 与落盘报告不一致" % key)
        self.assertEqual(fresh["runs"], saved["runs"])
        self.assertEqual(saved["determinism"],
                         {"same_selection": True, "same_trace": True, "same_records": True})


if __name__ == "__main__":
    unittest.main()

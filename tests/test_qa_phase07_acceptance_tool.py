#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 07 · 验收工具用例（`tools/qa_phase07_gap_acceptance.py`）。

用一份**合成的真机形状快照**（字段与 `tools/qa_phase07_real_snapshot.py` 导出的一致）跑工具，
钉住：before/after 两份口径、缺口分布与优先级复算、停止原因与逐轮依据、下一跳指纹与 seen 去重、
构造用例（UNRESOLVABLE_CONTRADICTION）、`--no-verify` / `--no-plan` 的语义与"空快照不谎报"。
"""
import json
import os
import sys
import tempfile
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_phase07_gap_acceptance as tool  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402

QUESTION = "香港家族办公室税收优惠政策对内地高净值客户有什么影响？"


def _evidence_payload(ref, text, *, authority=50, published="2026-10-04"):
    return {"evidence_ref": ref, "source_type": "article", "title": text[:40],
            "source_url": "https://example.com/%s" % ref.replace(":", "-"),
            "content_excerpt": text, "published_at": published,
            "authority_level": authority, "score": 12.0, "retrieval_method": "keyword",
            "relationship": "supports", "metadata": {}}


def _snapshot() -> dict:
    """两个 run：run-a 有结论图 + 真实形状的证据；run-b 只剩边（结论图没建成）。"""
    text = "香港家办税收优惠政策自2026年10月起生效，符合条件的家族投资控权工具可享利得税宽免"
    payload = {"claim_id": "l1-c1", "claim_type": "current_fact", "confidence": 0.8,
               "valid_from": "2026-10-01", "valid_to": None, "scope": [], "text": text,
               "evidence_refs": ["article:1"], "needs_verification": True,
               "verification_status": "unverified"}
    return {
        "captured_at_utc": "2026-10-11T00:00:00Z",
        "catalog": {"qa_runs": 2, "qa_claims": 2, "qa_claim_evidence": 2, "qa_conflicts": 0,
                    "qa_evidence": 2, "qa_reasoning_traces": 3},
        "source": {"host": "example", "container": "collectinfo-postgres",
                   "database": "collectinfo", "access": "readonly (BEGIN READ ONLY)"},
        "runs": [
            {"id": "run-a", "question_text": QUESTION, "mode": "standard",
             "industry_pack_id": "family_office", "status": "completed",
             "corpus_version": "corpus-a", "created_at": "2026-10-10T01:00:00Z",
             "completed_at": "2026-10-10T01:01:00Z"},
            {"id": "run-b", "question_text": "另一题", "mode": "fast",
             "industry_pack_id": "family_office", "status": "completed",
             "corpus_version": "corpus-a", "created_at": "2026-10-10T02:00:00Z",
             "completed_at": "2026-10-10T02:01:00Z"},
        ],
        "claims": [
            {"run_id": "run-a", "claim_key": "l1-c1", "stage": "conflict_review",
             "claim_text": text, "verification_status": "unverified", "payload": payload},
        ],
        "edges": [
            {"run_id": "run-a", "claim_key": "l1-c1", "evidence_ref": "article:1",
             "relationship": "supports", "relevance_score": 30.0, "published_at": "2026-10-04"},
            {"run_id": "run-b", "claim_key": "l1-c1", "evidence_ref": "article:9",
             "relationship": "supports", "relevance_score": 10.0, "published_at": "2026-10-04"},
        ],
        "conflicts": [],
        "evidence": [
            {"run_id": "run-a", "evidence_ref": "article:1", "source_type": "article",
             "source_url": "https://example.com/1", "source_title": "标题一",
             "published_at": "2026-10-04", "authority_level": 50,
             "payload": _evidence_payload("article:1", text, authority=50)},
            {"run_id": "run-a", "evidence_ref": "article:2", "source_type": "article",
             "source_url": "https://example.com/2", "source_title": "标题二",
             "published_at": "2026-10-04", "authority_level": 50,
             "payload": _evidence_payload("article:2", text.replace("生效", "没有生效"),
                                          authority=50)},
        ],
        "traces": [
            {"run_id": "run-a", "hop_index": 0, "round_index": 0,
             "sub_query_id": "h1", "sub_query": "香港家族办公室税收优惠政策", "route": "keyword",
             "results": 3, "accepted": 2, "rejected": 1, "new_claims": 0, "resolved_gap": 0,
             "gap_id": "", "status": "ok", "latency_ms": 120},
            {"run_id": "run-a", "hop_index": 1, "round_index": 0,
             "sub_query_id": "h2", "sub_query": "内地高净值客户 影响", "route": "keyword",
             "results": 2, "accepted": 1, "rejected": 1, "new_claims": 0, "resolved_gap": 0,
             "gap_id": "", "status": "ok", "latency_ms": 90},
        ],
        "seen": [
            {"owner_user_id": "u1", "session_id": "s1", "industry_pack_id": "family_office",
             "source_fingerprint": "fp-1", "status": "rejected"},
        ],
    }


class AcceptanceToolTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = _snapshot()

    def test_before_and_after_views(self):
        report = tool.run_acceptance(self.snapshot)
        self.assertEqual(report["totals"]["runs"], 1, "只有 run-a 建立了结论图")
        self.assertEqual(report["totals"]["before_claims"], 1)
        self.assertEqual(report["totals"]["before_conflicts"], 0)
        self.assertEqual(report["totals"]["before_relationship_distribution"], {"supports": 1})
        self.assertIsNone(report["totals"]["before_gaps"],
                          "接线前没有 Gap 概念，必须写 None 而不是 0")
        self.assertGreater(report["totals"]["after_gaps"], 0)
        # run-b 只有边、没有 conflict_review 的 canonical claim → 结论图未建立，如实跳过
        self.assertEqual([item["run_id"] for item in report["skipped_runs"]], ["run-b"])
        self.assertIn("结论图未建立", report["skipped_runs"][0]["reason"])

    def test_gaps_are_typed_and_priorities_recomputable(self):
        report = tool.run_acceptance(self.snapshot)
        row = report["runs"][0]["after"]
        self.assertTrue(row["gaps_detail"])
        for item in row["gaps_detail"]:
            self.assertIn(item["missing"], contracts.QA_GAP_TYPES)
            for route in item["suggested_routes"]:
                self.assertIn(route, contracts.QA_RETRIEVAL_ROUTES)
            factors = item["priority_factors"]
            weights = factors["weights"]
            expected = min(1.0, weights["severity"] * factors["severity"]
                           + weights["claim_importance"] * factors["claim_importance"]
                           + weights["evidence_deficit"] * factors["evidence_deficit"])
            self.assertAlmostEqual(item["priority"], expected, places=3)
        self.assertTrue(report["acceptance"]["checks"]["priority_is_recomputable"]["ok"])

    def test_stop_reason_and_rounds(self):
        report = tool.run_acceptance(self.snapshot)
        row = report["runs"][0]["after"]
        self.assertEqual(row["loop_stop_reason"], contracts.QA_STOP_NO_GAIN)
        self.assertIn(row["review_stop_reason"], contracts.QA_STOP_REASONS)
        streaks = [item["no_gain_streak"] for item in row["loop_rounds"]]
        self.assertGreaterEqual(max(streaks), 2, "NO_GAIN 必须有逐轮依据：%s" % streaks)
        self.assertTrue(row["loop_rounds"][0]["baseline"])
        self.assertTrue(report["acceptance"]["checks"]["no_gain_needs_consecutive_barren_rounds"]["ok"])

    def test_next_hops_carry_fingerprints_and_dedupe(self):
        report = tool.run_acceptance(self.snapshot)
        row = report["runs"][0]["after"]
        self.assertTrue(row["next_hops"], "有高优缺口就该规划出下一跳")
        for hop in row["next_hops"]:
            self.assertTrue(hop["query_fingerprint"])
            self.assertIn(hop["route"], contracts.QA_RETRIEVAL_ROUTES)
        self.assertGreater(row["searched_queries"], 0,
                           "下一跳的去重要有基线：真机留痕或计划 hop 问题")
        self.assertIn("traces", row["searched_basis"].replace("both", "traces plan"),
                      "本快照有真机留痕时就该把真机留痕算进基线（both = 留痕 + 计划问题）")
        self.assertEqual(report["seen"]["rows"], 1, "seen 身份按作用域统计，不按 run 分桶")
        self.assertEqual(report["seen"]["by_status"], {"rejected": 1})

    def test_constructed_case_reproduces_unresolvable_contradiction(self):
        report = tool.run_acceptance(self.snapshot)
        case = report["constructed_case"]
        self.assertTrue(case["constructed"])
        self.assertEqual(case["stop_reason"], contracts.QA_STOP_UNRESOLVABLE_CONTRADICTION)
        self.assertEqual(case["unresolved_contradictions"], 1)
        for code in case["reason_codes"]:
            self.assertIn(code, contracts.CONTRADICTION_UNRESOLVED_CODES)
        self.assertTrue(report["acceptance"]["checks"][
            "unresolvable_contradiction_is_reproducible"]["ok"])
        self.assertIn("真机", case["note"])

    def test_no_plan_removes_the_dedupe_basis_and_fails_the_gate(self):
        """没有计划又没有真机留痕 → 下一跳没有可比对的查询，门必须报出来（不许假装通过）。"""
        snapshot = _snapshot()
        snapshot["traces"] = []
        report = tool.run_acceptance(snapshot, with_plan=False)
        self.assertFalse(report["acceptance"]["checks"]["seen_dedupe_has_a_real_basis"]["ok"])
        self.assertFalse(report["acceptance"]["passed"])
        self.assertEqual(report["runs"][0]["after"]["searched_queries"], 0)

    def test_no_verify_changes_the_gap_profile_honestly(self):
        verified = tool.run_acceptance(self.snapshot, verify=True)
        raw = tool.run_acceptance(self.snapshot, verify=False)
        self.assertTrue(verified["verify_replay"])
        self.assertFalse(raw["verify_replay"])
        self.assertNotEqual(verified["totals"]["gap_types"], raw["totals"]["gap_types"],
                            "复跑核验与不复跑必须给出不同的缺口画像（否则核验没起作用）")

    def test_report_is_json_serializable(self):
        body = json.dumps(tool.run_acceptance(self.snapshot), ensure_ascii=False, allow_nan=False)
        self.assertIn("qa-gap-acceptance", body)
        self.assertIn(contracts.GAP_ANALYZER_VERSION, body)

    def test_cli_writes_the_report(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "snapshot.json")
            out = os.path.join(folder, "report.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(self.snapshot, handle, ensure_ascii=False)
            saved = sys.argv
            sys.argv = ["tool", "--snapshot", path, "--out", out]
            try:
                code = tool.main()
            finally:
                sys.argv = saved
            self.assertEqual(code, 0)
            with open(out, encoding="utf-8") as handle:
                written = json.load(handle)
            self.assertEqual(written["report_version"], tool.REPORT_VERSION)
            self.assertTrue(written["acceptance"]["passed"])
            self.assertIn("constructed_case", written)

    def test_empty_snapshot_is_not_a_false_pass(self):
        report = tool.run_acceptance({"runs": [], "claims": [], "edges": [], "conflicts": [],
                                      "evidence": [], "traces": [], "seen": []})
        self.assertEqual(report["totals"]["runs"], 0)
        self.assertIsNone(report["totals"]["before_gaps"])
        self.assertTrue(report["acceptance"]["passed"],
                        "没有数据时不该谎报失败，也不该编造数字（各项检查都是 0 违规）")
        self.assertTrue(report["constructed_case"].get("skipped"),
                        "没有真机素材时必须写明构造用例被跳过，而不是假装算过")


if __name__ == "__main__":
    unittest.main()

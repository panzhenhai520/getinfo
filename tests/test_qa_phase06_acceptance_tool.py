#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 06 · 验收工具用例（`tools/qa_phase06_evidence_graph_acceptance.py`）。

用一份**合成的真机快照**（形状与 `baseline/qa-evidence-graph-real-sample.json` 一致）跑工具，
钉住：before/after 两份口径、覆盖率复算、理由码合法性、跳过 run 的记账、`--no-verify` 的语义。
"""
import json
import os
import sys
import tempfile
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.sep + "tools")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_phase06_evidence_graph_acceptance as tool  # noqa: E402
from qa_phase06_fixtures import claim_node, evidence, graph_of  # noqa: E402


def _snapshot() -> dict:
    """两个 run：run-a 有结论图（1 条结论、1 条支持边）；run-b 只剩边与证据（结论图没建成）。"""
    left = claim_node("c1", text="A股10月9日大涨3.84%", refs=["e1"], status="confirmed",
                      authority=100, pairs=[
                          {"evidence_ref": "e1", "verdict": "SUPPORTED", "score": 0.7}])
    graph = graph_of([left], [evidence("e1")])
    return {
        "captured_at_utc": "2026-10-10T18:25:28Z",
        "catalog": {"qa_runs": 2, "qa_conflicts": 0},
        "source": {"host": "example", "access": "readonly"},
        "runs": [{"id": "run-a", "question_text": "A股大涨原因", "mode": "standard",
                  "industry_pack_id": "auto"},
                 {"id": "run-b", "question_text": "另一题", "mode": "fast",
                  "industry_pack_id": "auto"}],
        "claims": [
            {"run_id": "run-a", "claim_key": "c1", "stage": "conflict_review",
             "verification_status": "confirmed", "payload": left},
        ],
        "edges": [
            {"run_id": "run-a", "claim_key": "c1", "evidence_ref": "e1",
             "relationship": "supports", "relevance_score": 30.0},
            {"run_id": "run-b", "claim_key": "c1", "evidence_ref": "e9",
             "relationship": "supports", "relevance_score": 10.0},
        ],
        "conflicts": [],
        "evidence": [
            {"run_id": "run-a", "evidence_ref": "e1", "source_type": "article",
             "source_url": "https://example.com/1", "source_title": "标题", "published_at": "2026-10-09",
             "authority_level": 100, "payload": evidence("e1")},
        ],
    }


class AcceptanceToolTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = _snapshot()

    def test_before_and_after_views(self):
        report = tool.run_acceptance(self.snapshot)
        self.assertEqual(report["totals"]["runs"], 1, "只有 run-a 建立了结论图")
        self.assertEqual(report["totals"]["before_edges"], 1)
        self.assertEqual(report["totals"]["before_relationship_distribution"], {"supports": 1})
        self.assertIsNone(report["totals"]["before_claim_coverage"],
                          "接线前没有 coverage 口径，必须写 None 而不是 0")
        self.assertEqual(report["totals"]["after_claim_coverage"], 1.0)
        self.assertEqual(report["totals"]["relation_distribution"]["SUPPORTS"], 1)
        self.assertEqual(len(report["skipped_runs"]), 1)
        self.assertEqual(report["skipped_runs"][0]["run_id"], "run-b")
        self.assertIn("结论图未建立", report["skipped_runs"][0]["reason"])

    def test_gates_pass_on_the_fixture(self):
        report = tool.run_acceptance(self.snapshot)
        self.assertTrue(report["acceptance"]["passed"], report["acceptance"]["checks"])
        self.assertEqual(report["acceptance"]["checks"]["relations_agree_with_verifier"]["detail"],
                         "checked=1 mismatches=0")

    def test_no_verify_means_no_verified_support(self):
        """库里当时没存核验结论（老 run）→ 主口径 coverage 必须是 0，关系只能是 MENTIONS。"""
        snapshot = _snapshot()
        snapshot["claims"][0]["payload"] = claim_node(
            "c1", text="A股10月9日大涨3.84%", refs=["e1"], status="qualified",
            verification=False)
        report = tool.run_acceptance(snapshot, verify=False)
        self.assertEqual(report["totals"]["after_claim_coverage"], 0.0)
        self.assertEqual(report["totals"]["relation_distribution"], {"MENTIONS": 1})
        self.assertEqual(report["totals"]["verification_basis"], {"relationship": 1})
        self.assertTrue(report["acceptance"]["passed"],
                        "关掉复放不该让验收挂掉（该口径本身是诚实的）")
        self.assertNotIn("verify_replay_executed", report["acceptance"]["checks"])

    def test_verify_replay_turns_claims_into_verifier_basis(self):
        snapshot = _snapshot()
        snapshot["claims"][0]["payload"] = claim_node(
            "c1", text="A股10月9日大涨3.84%", refs=["e1"], status="qualified",
            verification=False)
        report = tool.run_acceptance(snapshot, verify=True)
        self.assertTrue(report["runs"][0]["after"]["verify_replay"])
        self.assertEqual(report["totals"]["verification_basis"], {"verifier_pairs": 1})
        self.assertIn("verify_replay_executed", report["acceptance"]["checks"])

    def test_plan_enrichment_produces_real_dependency_edges(self):
        """老 run 没存计划 → 用当前 Phase 05 代码按真实问题现算，DEPENDS 边才有真数据。"""
        report = tool.run_acceptance(self.snapshot, with_plan=True)
        row = report["runs"][0]["after"]
        self.assertGreater(row["plan_claims"], 0)
        self.assertIn("build_research_plan", row["plan_source"])
        self.assertIn("after_plan_claims", report["totals"])
        self.assertGreater(report["totals"]["after_plan_claims"], 0)
        without = tool.run_acceptance(self.snapshot, with_plan=False)
        self.assertEqual(without["totals"]["after_depends_edges"], 0)
        self.assertEqual(without["runs"][0]["after"]["plan_source"], "plan_disabled")
        self.assertGreaterEqual(report["totals"]["after_depends_edges"],
                                without["totals"]["after_depends_edges"])

    def test_report_is_json_serializable(self):
        body = json.dumps(tool.run_acceptance(self.snapshot), ensure_ascii=False, allow_nan=False)
        self.assertIn("qa-evidence-graph-acceptance", body)

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

    def test_empty_snapshot_is_not_a_false_pass(self):
        report = tool.run_acceptance({"runs": [], "claims": [], "edges": [], "conflicts": [],
                                      "evidence": []})
        self.assertEqual(report["totals"]["runs"], 0)
        self.assertTrue(report["acceptance"]["passed"],
                        "没有数据时不该谎报失败，也不该编造数字（各项检查都是 0 违规）")
        self.assertIsNone(report["totals"]["after_claim_coverage"])


if __name__ == "__main__":
    unittest.main()

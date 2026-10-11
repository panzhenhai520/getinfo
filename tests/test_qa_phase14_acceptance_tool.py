#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 14 · 验收工具用例（`tools/qa_phase14_acceptance.py`）。

钉住：
  1. 工具在**真实快照形状**上跑得动（这里用构造快照，字段与 `baseline/qa-context-real-sample.json` 一致）；
  2. 报出来的两个规格书点名数字**可复算**：重复提问检出数（按触发判据分组）+ 交互次数分布；
  3. **不可以编**：快照拿不到的信号（stop_reason / 未解决率）不参与判定，
     口径替代（已核验结论数之差）在 `source.substituted_signals` 里如实写明；
  4. 同一议题最多交互一次、每条交互都有依据、选项表完整 —— 四项验收断言为真；
  5. 同输入同输出（同一份快照跑两次逐字相同）；纯离线（不连任何库、不发任何请求）。
"""
import json
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))

import qa_phase14_fixtures as fx  # noqa: E402
import qa_phase14_acceptance as acc  # noqa: E402


def _snapshot() -> dict:
    """字段与 A 机快照一致的构造快照（两轮同题 + 一轮不同题，且都有真实形状的信号）。"""
    runs = [
        {"id": "r1", "question_text": fx.KEYWORD_QUESTION, "mode": "standard",
         "status": "completed", "created_at": "2026-10-11T01:00:00Z",
         "degradation": [], "final_answer": {"status": "partial", "answer": "答案一",
                                             "degradation_reasons": []}},
        {"id": "r2", "question_text": fx.SIMILAR_QUESTION, "mode": "standard",
         "status": "completed", "created_at": "2026-10-11T02:00:00Z",
         "degradation": [{"code": "evidence_insufficient"}],
         "final_answer": {"status": "insufficient_evidence", "answer": "答案二",
                          "degradation_reasons": ["核验未完成"]}},
        {"id": "r3", "question_text": fx.OTHER_QUESTION, "mode": "standard",
         "status": "completed", "created_at": "2026-10-11T03:00:00Z",
         "degradation": [], "final_answer": {"status": "ready", "answer": "答案三",
                                             "degradation_reasons": []}},
    ]
    claims = [{"run_id": "r1", "verification_status": "confirmed"},
              {"run_id": "r1", "verification_status": "unverified"},
              {"run_id": "r2", "verification_status": "unverified"}]
    return {"captured_at_utc": "2026-10-11T04:00:00Z",
            "source": {"host": "test", "snapshot_version": "qa-context-real-v1",
                       "access": "readonly"},
            "runs": runs, "claims": claims, "edges": [], "evidence": [], "stages": []}


class AcceptanceToolTests(unittest.TestCase):
    def setUp(self):
        self.report = acc.analyze_snapshot(_snapshot())

    def test_two_headline_numbers_are_recomputable(self):
        totals = self.report["totals"]
        self.assertEqual(totals["turns"], 3)
        self.assertEqual(totals["with_question"], 3)
        self.assertEqual(totals["repeat_detected"], 1, "同题第二次才算检出")
        self.assertEqual(sum(totals["repeat_by_trigger"].values()) >= 1, True)
        self.assertEqual(totals["interactions"], 1)
        self.assertEqual(totals["interactions_histogram"], {"2": 1},
                         "这条交互有 2 条可解释依据")
        self.assertIn("ANSWER_DEGRADED", totals["by_evidence"])

    def test_unavailable_signals_are_declared_not_invented(self):
        substituted = self.report["source"]["substituted_signals"]
        self.assertIn("new_verified_claims", substituted)
        self.assertIn("不可得", substituted["stop_reason"])
        self.assertTrue(self.report["source"]["session_assumption"])
        for row in self.report["turns"]:
            self.assertEqual(row["skip_reason"], "NOT_A_REPEAT") if not row["repeat"] else None

    def test_acceptance_booleans(self):
        acceptance = self.report["acceptance"]
        self.assertTrue(acceptance["runs_on_real_data"])
        self.assertTrue(acceptance["every_interaction_has_evidence"])
        self.assertTrue(acceptance["interactions_are_one_per_topic"])
        self.assertTrue(acceptance["options_are_complete"])
        self.assertTrue(acceptance["no_fabricated_evidence"])
        self.assertTrue(acceptance["passed"], acceptance)

    def test_same_snapshot_same_report(self):
        first = json.dumps(acc.analyze_snapshot(_snapshot()), ensure_ascii=False, sort_keys=True)
        second = json.dumps(acc.analyze_snapshot(_snapshot()), ensure_ascii=False, sort_keys=True)
        self.assertEqual(first, second)

    def test_empty_snapshot_is_honest(self):
        report = acc.analyze_snapshot({"runs": [], "claims": []})
        self.assertEqual(report["totals"]["turns"], 0)
        self.assertFalse(report["acceptance"]["runs_on_real_data"])
        self.assertFalse(report["acceptance"]["passed"])


if __name__ == "__main__":
    unittest.main()

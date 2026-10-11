#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P17-03 metric report 用例：排序指标手算、独立标注集（构造/确定性/零重叠/评价）、指标总表。"""
from __future__ import annotations

import json
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import qa_metrics_report as mr  # noqa: E402
import qa_phase17_fixtures as fx  # noqa: E402

REAL_SNAPSHOT = os.path.join("baseline", "qa-context-real-sample.json")


class RankingMetricTest(unittest.TestCase):
    def test_mrr_and_ndcg_hand_computed(self):
        rows = [
            # 第一条证据就命中 → RR = 1/1；相关项在 1 位 → NDCG = 1.0
            {"expect_terms_count": 2, "first_hit_rank": 1, "relevant_at_k": 1,
             "relevant_ranks": [1], "evidence": 3},
            # 第三条才命中 → RR = 1/3；DCG = 1/log2(4) = 0.5，IDCG = 1 → NDCG = 0.5
            {"expect_terms_count": 1, "first_hit_rank": 3, "relevant_at_k": 1,
             "relevant_ranks": [3], "evidence": 3},
        ]
        report = mr.ranking_metrics(rows)
        self.assertAlmostEqual(report["mrr_at_k"], (1.0 + 1.0 / 3.0) / 2, places=6)
        self.assertAlmostEqual(report["ndcg_at_k"], (1.0 + 0.5) / 2, places=6)
        self.assertEqual(report["labeled_questions"], 2)

    def test_ndcg_rewards_top_ranked_hits(self):
        """命中数相同、位置不同 → NDCG 必须区分（这正是 citation_precision 看不到的一面）。"""
        top = mr.ranking_metrics([{"expect_terms_count": 1, "first_hit_rank": 1,
                                   "relevant_at_k": 2, "relevant_ranks": [1, 2],
                                   "evidence": 4}])
        bottom = mr.ranking_metrics([{"expect_terms_count": 1, "first_hit_rank": 3,
                                      "relevant_at_k": 2, "relevant_ranks": [3, 4],
                                      "evidence": 4}])
        self.assertGreater(top["ndcg_at_k"], bottom["ndcg_at_k"])
        self.assertEqual(top["mrr_at_k"], 1.0)

    def test_missing_labels_give_null(self):
        report = mr.ranking_metrics([{"expect_terms_count": 0}])
        self.assertIsNone(report["mrr_at_k"])
        self.assertIsNone(report["ndcg_at_k"])
        self.assertIn("分母为 0", report["mrr_at_k_note"])


class AnnotationConstructionTest(unittest.TestCase):
    def setUp(self):
        self.snapshot = self._snapshot()

    @staticmethod
    def _snapshot():
        """两条 claim：一条与证据高度重合（正例），一条毫无重合（负例）。"""
        text = "香港家族办公室税收优惠政策对符合条件的管理人给予利得税宽免并要求实质经营"
        evidence = {
            "evidence_ref": "article:1", "source_type": "article",
            "title": "家族办公室税收政策解读",
            "content_excerpt": text + "。" + "业内认为该政策影响跨境配置。" * 3,
            "source_url": "https://example.invalid/1", "authority_level": 50,
            "published_at": "2026-01-10", "metadata": {},
        }
        other = {
            "evidence_ref": "article:2", "source_type": "article",
            "title": "完全无关的天气报道",
            "content_excerpt": "今日多云转晴，气温回升，适合户外活动。" * 4,
            "source_url": "https://example.invalid/2", "authority_level": 40,
            "published_at": "2026-01-11", "metadata": {},
        }
        claim = {"claim_id": "c1", "text": text, "claim_type": "current_fact",
                 "confidence": 0.6, "valid_from": "2026-01-01", "valid_to": None,
                 "scope": ["家族办公室"], "evidence_refs": ["article:1"],
                 "needs_verification": True, "verification_status": "confirmed"}
        return {
            "captured_at_utc": "2026-10-11T00:00:00Z",
            "source": {"host": "synthetic", "snapshot_version": "test"},
            "runs": [{"id": "r1", "question_text": "问题", "mode": "standard"}],
            "claims": [{"run_id": "r1", "claim_key": "c1", "claim_text": text,
                        "verification_status": "confirmed",
                        "payload": {"claim_id": "c1", "claim": claim, "plan_only": False}}],
            "evidence": [
                {"run_id": "r1", "evidence_ref": "article:1", "source_type": "article",
                 "source_url": evidence["source_url"], "source_title": evidence["title"],
                 "published_at": evidence["published_at"], "authority_level": 50,
                 "payload": evidence},
                {"run_id": "r1", "evidence_ref": "article:2", "source_type": "article",
                 "source_url": other["source_url"], "source_title": other["title"],
                 "published_at": other["published_at"], "authority_level": 40,
                 "payload": other},
            ],
            "edges": [{"run_id": "r1", "claim_key": "c1", "evidence_ref": "article:1",
                       "relationship": "SUPPORTS", "relevance_score": 1.0,
                       "published_at": "2026-01-10"}],
            "conflicts": [],
        }

    def test_positive_and_negative_pairs_built(self):
        annotation = mr.build_independent_annotation(self.snapshot, golden_path="no-such.json")
        groups = annotation["counts"]["by_group"]
        self.assertGreaterEqual(groups.get("POSITIVE_CITED_HIGH_COVERAGE", 0), 1)
        self.assertGreaterEqual(groups.get("NEGATIVE_UNCITED_LOW_COVERAGE", 0), 1)
        self.assertEqual(annotation["counts"]["min_pairs_per_group"], mr.MIN_PAIRS_PER_GROUP)
        self.assertTrue(annotation["rules"]["preregistered"])

    def test_construction_is_deterministic(self):
        first = mr.build_independent_annotation(self.snapshot, golden_path="no-such.json")
        second = mr.build_independent_annotation(self.snapshot, golden_path="no-such.json")
        self.assertEqual(first["annotation_set_id"], second["annotation_set_id"])
        self.assertEqual([row["pair_id"] for row in first["pairs"]],
                         [row["pair_id"] for row in second["pairs"]])

    def test_golden_overlap_is_excluded_and_detected(self):
        """两条：① 与金标同文的 claim 必须被**排除**（并记在 skipped 里）；
        ② 一旦产物里真的混进金标文本，`overlap_with_golden` 必须报出来（否则独立性是空话）。"""
        import tempfile

        text = self.snapshot["claims"][0]["claim_text"]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as handle:
            json.dump({"pairs": [{"claim": text, "evidence": "x"}]}, handle, ensure_ascii=False)
            path = handle.name
        try:
            annotation = mr.build_independent_annotation(self.snapshot, golden_path=path)
            self.assertGreater(annotation["counts"]["skipped"]["golden_overlap"], 0)
            self.assertEqual([row for row in annotation["pairs"]
                              if row["claim_text"] == text], [],
                             "与金标同文的 claim 一条都不许进标注集")
            injected = dict(annotation)
            injected["pairs"] = list(annotation["pairs"]) + [{
                "pair_id": "INJECTED", "group": "POSITIVE_CITED_HIGH_COVERAGE", "run_id": "r1",
                "claim_id": "c1", "claim_text": text, "evidence_ref": "article:1",
                "evidence_text": "x", "coverage": 1.0, "expected_support": True,
                "construction": "inject"}]
            self.assertFalse(mr.overlap_with_golden(injected, golden_path=path)["disjoint"],
                             "混进金标文本必须被检出")
            normal = mr.overlap_with_golden(annotation)
            self.assertTrue(normal["disjoint"])
        finally:
            os.unlink(path)

    def test_evaluate_annotation_gives_verdicts(self):
        annotation = mr.build_independent_annotation(self.snapshot, golden_path="no-such.json")
        evaluation = mr.evaluate_annotation(annotation)
        self.assertEqual(evaluation["pairs"], len(annotation["pairs"]))
        self.assertIsNotNone(evaluation["support_recall"])
        self.assertEqual(evaluation["false_support_rate"], 0.0,
                         "低覆盖的负例绝不许被判 SUPPORTED（MASTER_RULES 11）")
        self.assertIn("verifier", evaluation["caliber"])

    def test_save_and_load_roundtrip(self):
        annotation = mr.build_independent_annotation(self.snapshot, golden_path="no-such.json")
        path = os.path.join(fx.temp_dir(), "annotation.json")
        try:
            mr.save_annotation(annotation, path)
            self.assertEqual(mr.load_annotation(path)["annotation_set_id"],
                             annotation["annotation_set_id"])
        finally:
            fx.cleanup_dir(os.path.dirname(path))


class RealAnnotationTest(unittest.TestCase):
    """真机快照上的独立标注集：规模、零重叠、方向守住（落盘产物与现跑一致）。"""

    def setUp(self):
        if not os.path.isfile(REAL_SNAPSHOT):
            self.fail("缺少 Phase 08 真机快照（独立标注集的素材真源）")
        with open(REAL_SNAPSHOT, encoding="utf-8") as handle:
            self.snapshot = json.load(handle)

    def test_real_annotation_invariants(self):
        annotation = mr.build_independent_annotation(self.snapshot, source_path=REAL_SNAPSHOT)
        counts = annotation["counts"]
        self.assertGreaterEqual(counts["pairs"], 16)
        self.assertGreaterEqual(counts["positives"], mr.MIN_PAIRS_PER_GROUP)
        self.assertGreaterEqual(counts["negatives"], mr.MIN_PAIRS_PER_GROUP)
        overlap = mr.overlap_with_golden(annotation)
        self.assertTrue(overlap["disjoint"], "独立标注集必须与调参样本（金标 18 对）零重叠")
        self.assertEqual(overlap["claim_text_overlap"], 0)
        self.assertEqual(overlap["evidence_text_overlap"], 0)

    def test_real_annotation_false_support_is_zero(self):
        annotation = mr.build_independent_annotation(self.snapshot, source_path=REAL_SNAPSHOT)
        evaluation = mr.evaluate_annotation(annotation)
        self.assertEqual(evaluation["false_support_rate"], 0.0)
        self.assertIsNotNone(evaluation["support_recall"])
        self.assertGreater(evaluation["support_recall"], 0.5,
                           "正例的支持命中率至少过半（真机实测 0.85）")

    def test_committed_artifact_matches_current_build(self):
        if not os.path.isfile(mr.DEFAULT_ANNOTATION_PATH):
            self.fail("缺少落盘的独立标注集 %s（跑 tools/qa_phase17_acceptance.py "
                      "--section metrics --rebuild-annotation 生成）" % mr.DEFAULT_ANNOTATION_PATH)
        committed = mr.load_annotation()
        rebuilt = mr.build_independent_annotation(self.snapshot, source_path=REAL_SNAPSHOT)
        self.assertEqual(committed["annotation_set_id"], rebuilt["annotation_set_id"],
                         "落盘产物必须与现跑一致（素材或规则变了就必须重建）")
        self.assertTrue(mr.overlap_with_golden(committed)["disjoint"])


class MetricCatalogTest(unittest.TestCase):
    def _catalog(self, **overrides):
        payload = {
            "retrieval_metrics": {"recall_at_k": 0.5, "citation_precision": 0.6,
                                  "citation_recall": 0.4, "hit_rate": 0.7, "avg_ms": 120,
                                  "cost": {"tokens_total": None, "degraded": True, "note": "n"}},
            "ranking": {"mrr_at_k": 0.4, "ndcg_at_k": 0.5, "mrr_at_k_note": "n",
                        "ndcg_at_k_note": "n"},
            "answer_totals": {"citation_traceable_rate": 1.0, "unsupported_claim_rate": 0.5,
                              "claim_coverage": 0.7, "contradictions": 0, "counter_evidence": 0,
                              "context_items": 10, "context_tokens": 1000, "memory_recalled": 0,
                              "memory_written": 3, "checks_pass_rate": 0.6,
                              "acquisition_actions": 0, "context_gap_suppressed": 0},
            "annotation_eval": {"pairs": 10, "support_recall": 0.8, "false_support_rate": 0.0,
                                "negatives": 5},
        }
        payload.update(overrides)
        return mr.metric_catalog(**payload)

    def test_seven_families_present(self):
        catalog = self._catalog()
        for family in ("Retrieval", "Evidence", "Agent", "Context", "Memory", "System",
                       "SafetyMapping（D-002 中性映射：临床→作用域/合规风险）"):
            self.assertIn(family, catalog)

    def test_no_object_entries_are_labelled_not_masqueraded(self):
        catalog = self._catalog()
        contradiction = catalog["Evidence"]["contradiction_detection"]
        self.assertEqual(contradiction["status"], "no_object")
        self.assertEqual(contradiction["value"], 0, "计数为 0 就是 0（确实数过），但状态必须是 no_object")
        self.assertEqual(contradiction["reason_code"], "NO_CONFLICT_ROW_IN_REAL_DATA")
        counter = catalog["Context"]["counter_evidence_coverage"]
        self.assertEqual(counter["status"], "no_object")
        self.assertEqual(counter["value"], 0)
        # 真·缺失（没测到）必须写 null，不许写 0
        self.assertIsNone(catalog["System"]["tokens_recorded_online"]["value"])
        self.assertEqual(catalog["System"]["tokens_recorded_online"]["status"], "no_object")
        self.assertIsNone(catalog["Memory"]["stale_rate"]["value"])
        self.assertEqual(catalog["Memory"]["stale_rate"]["status"], "not_implemented")

    def test_values_present_when_objects_exist(self):
        catalog = self._catalog(answer_totals={"citation_traceable_rate": 1.0,
                                               "unsupported_claim_rate": 0.5,
                                               "claim_coverage": 0.7, "contradictions": 2,
                                               "counter_evidence": 3, "context_items": 10,
                                               "context_tokens": 1000, "checks_pass_rate": 0.6,
                                               "acquisition_actions": 4,
                                               "context_gap_suppressed": 2, "memory_recalled": 5,
                                               "memory_written": 3})
        self.assertEqual(catalog["Evidence"]["contradiction_detection"]["value"], 2)
        self.assertEqual(catalog["Evidence"]["contradiction_detection"]["status"], "ok")
        self.assertEqual(catalog["Context"]["context_gap_false_retrieval"]["value"], 2)

    def test_report_status_counts_and_phase00_note(self):
        catalog = self._catalog()
        report = mr.build_metric_report(
            retrieval_rows=[{"expect_terms_count": 1, "first_hit_rank": 1, "relevant_at_k": 1,
                             "evidence": 2}],
            retrieval_metrics={"recall_at_k": 0.5}, answer_totals={"checks_pass_rate": 0.5},
            annotation={"annotation_version": mr.ANNOTATION_VERSION, "annotation_set_id": "x",
                        "counts": {"pairs": 10}, "rules": {}},
            annotation_eval={"pairs": 10, "support_recall": 0.8, "false_support_rate": 0.0,
                             "negatives": 5},
            phase00={"history_path": "p", "frozen": {"summary": {"recall_at_k": 0.8333}},
                     "reason_code": ""})
        self.assertEqual(report["report_version"], mr.METRIC_REPORT_VERSION)
        self.assertIn("no_object", report["status_counts"])
        self.assertIn("全库语料", report["phase00_baseline"]["note"])
        self.assertEqual(report["annotation"]["evaluation"]["support_recall"], 0.8)


if __name__ == "__main__":
    unittest.main()

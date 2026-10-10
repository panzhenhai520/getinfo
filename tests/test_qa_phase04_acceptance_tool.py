#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 04 验收工具（`tools/qa_hunter_fleet_acceptance.py`）的自测。

工具本身要能自动验证，否则"真跑留档"就成了人工描述：
  1. 快照 → 临时 sqlite 的往返：文章/分类/向量都真的落进去了，既有检索能吃；
  2. 基线侧与舰队侧跑同一批题，`compare` 报出两侧指标 + Δ + 舰队降级/并行统计；
  3. 逐题明细键集与 `tools/qa_retrieval_acceptance.evaluate` 一致（这样 `summarize` 的口径
     才真的被复用，而不是各写一套）。
"""
import json
import os
import sys
import tempfile
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

import tools.qa_hunter_fleet_acceptance as acceptance_tool  # noqa: E402
from qa_hunter_fleet import build_default_fleet  # noqa: E402
from qa_retrieval import ArticleRetriever  # noqa: E402

PACK = "family_office"


def _snapshot() -> dict:
    """两条文章 + 向量 + 分类的小快照（形状与 A 机导出逐字一致）。"""
    def _vector(values):
        array = np.asarray(values, dtype=np.float32)
        import base64

        return base64.b64encode(array.tobytes()).decode("ascii")

    return {
        "snapshot_version": "qa-hunter-corpus-snapshot-v1",
        "source": {"access": "test fixture", "host": "local"},
        "packs": {PACK: {"article_ids": [101, 102]}},
        "articles": [
            {"id": 101, "url": "https://example.com/snap/101",
             "title": "香港家族办公室税收优惠政策落地",
             "content": "香港特区政府公布家族办公室税收优惠政策：家族投资控权工具可享利得税宽免。",
             "domain": "example.com", "publish_date": "2026-10-01",
             "first_crawled": "2026-10-01", "status": "active", "quality_score": 80,
             "content_length": 40, "published_at_utc": None, "published_timezone": None,
             "published_precision": None},
            {"id": 102, "url": "https://example.com/snap/102",
             "title": "离岸信托架构与税务居民身份安排",
             "content": "离岸信托架构与税务居民身份的安排决定整体税负水平，需与豁免安排配套。",
             "domain": "example.com", "publish_date": "2026-09-20",
             "first_crawled": "2026-09-20", "status": "active", "quality_score": 80,
             "content_length": 40, "published_at_utc": None, "published_timezone": None,
             "published_precision": None},
        ],
        "classifications": [
            {"article_id": 101, "industry_pack_id": PACK, "activation_id": "",
             "industry_pack_version": "v1", "classifier_version": "snapshot",
             "article_content_hash": "h1", "rule_category": "event", "rule_confidence": 0.9,
             "score_details_json": json.dumps({
                 "hits": {"anchor": ["家族办公室"]}, "relevance_score": 9.0,
                 "minimum_relevance_score": 5.0}, ensure_ascii=False),
             "matched_keywords_json": json.dumps(["家族办公室"], ensure_ascii=False),
             "topic_tags_json": "[]", "final_category": "event", "result_source": "rule"},
            {"article_id": 102, "industry_pack_id": PACK, "activation_id": "",
             "industry_pack_version": "v1", "classifier_version": "snapshot",
             "article_content_hash": "h2", "rule_category": "event", "rule_confidence": 0.9,
             "score_details_json": json.dumps({
                 "hits": {"anchor": ["离岸信托"]}, "relevance_score": 9.0,
                 "minimum_relevance_score": 5.0}, ensure_ascii=False),
             "matched_keywords_json": json.dumps(["离岸信托"], ensure_ascii=False),
             "topic_tags_json": "[]", "final_category": "event", "result_source": "rule"},
        ],
        "ragflow_documents": [],
        "events": [],
        "attributes": [],
        "embeddings": [
            {"article_id": 101, "model_id": "bge-m3", "embedding_dim": 4,
             "embedding_b64": _vector([1.0, 0.0, 0.0, 0.0]), "status": "ready"},
            {"article_id": 102, "model_id": "bge-m3", "embedding_dim": 4,
             "embedding_b64": _vector([0.98, 0.199, 0.0, 0.0]), "status": "ready"},
        ],
        "counts": {"articles": 2, "classifications": 2, "ragflow_documents": 0, "events": 0,
                   "attributes": 0, "embeddings": 2},
    }


QUESTIONS = [
    {"id": "q1", "question": "香港家族办公室税收优惠政策对内地客户有什么影响？",
     "kind": "event", "industry_pack_id": PACK, "expect_terms": ["家族办公室"]},
    {"id": "q2", "question": "离岸信托架构如何影响整体税负？", "kind": "event",
     "industry_pack_id": PACK, "expect_terms": ["离岸信托"]},
]


class SnapshotReplayTests(unittest.TestCase):
    def setUp(self):
        import intel_database

        self.temp_dir = tempfile.TemporaryDirectory()
        self.snapshot = _snapshot()
        self.db = acceptance_tool.build_db_from_snapshot(
            self.snapshot, os.path.join(self.temp_dir.name, "snapshot.sqlite3"))
        self.saved = intel_database.sqlite_db
        intel_database.sqlite_db = self.db
        self.retriever = ArticleRetriever(self.db)
        self.fleet = build_default_fleet(database=self.db, retriever=self.retriever)

    def tearDown(self):
        import intel_database

        intel_database.sqlite_db = self.saved
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def test_snapshot_round_trip_lands_in_the_temp_database(self):
        rows, _excluded = self.retriever._rows(PACK)
        self.assertEqual(sorted(int(row["id"]) for row in rows), [101, 102])
        with self.db.lock:
            count = self.db.connection.execute(
                "SELECT COUNT(*) FROM intel_article_embeddings WHERE status='ready'"
            ).fetchone()[0]
        self.assertEqual(int(count), 2, "向量必须按 base64 解码后原样入库")
        self.assertEqual(self.db.backend, "sqlite")

    def test_run_side_outputs_match_the_frozen_row_contract(self):
        base_rows, _ = acceptance_tool.run_side(QUESTIONS, side="baseline",
                                                retriever=self.retriever, fleet=self.fleet,
                                                database=self.db, limit=12)
        fleet_rows, receipts = acceptance_tool.run_side(QUESTIONS, side="fleet",
                                                        retriever=self.retriever,
                                                        fleet=self.fleet, database=self.db,
                                                        limit=12)
        self.assertEqual(len(base_rows), 2)
        self.assertEqual(len(fleet_rows), 2)
        for row in base_rows + fleet_rows:
            for key in acceptance_tool.ROW_KEYS:
                self.assertIn(key, row, "逐题明细缺 %s（summarize 的口径会被打断）" % key)
        self.assertEqual(len(receipts), 2, "舰队侧每题都要有回执")
        self.assertIn("by_hunter", receipts[0])

    def test_compare_reports_both_sides_and_deltas(self):
        cost = {"tokens_in": 0, "tokens_out": 0, "tokens_total": 0, "stage_rows": 0,
                "runs_counted": 0, "note": "测试注入"}
        base_rows, base_receipts = acceptance_tool.run_side(
            QUESTIONS, side="baseline", retriever=self.retriever, fleet=self.fleet,
            database=self.db, limit=12)
        fleet_rows, fleet_receipts = acceptance_tool.run_side(
            QUESTIONS, side="fleet", retriever=self.retriever, fleet=self.fleet,
            database=self.db, limit=12)
        report = acceptance_tool.compare(base_rows, fleet_rows, base_receipts, fleet_receipts,
                                         limit=12, meta={"benchmark_version": "test"}, cost=cost)
        self.assertIn("baseline", report)
        self.assertIn("fleet", report)
        self.assertIn("hit_rate", report["delta"])
        self.assertGreaterEqual(report["baseline"]["hit_rate"], 1.0)
        self.assertGreaterEqual(report["fleet"]["hit_rate"], 1.0)
        self.assertEqual(report["fleet"]["errors"], 0)
        self.assertIn("hunter_statuses", report["fleet_metrics"])
        self.assertGreaterEqual(report["fleet_metrics"]["degraded_total"], 0)
        self.assertEqual(report["fleet_metrics"]["runs"], 2)

    def test_fleet_finds_the_semantic_neighbour_the_baseline_misses(self):
        """真差异：q2 只有靠向量才能把 101/102 都召回，用于证明语义通道确实在起作用。"""
        fleet_rows, receipts = acceptance_tool.run_side(
            QUESTIONS, side="fleet", retriever=self.retriever, fleet=self.fleet,
            database=self.db, limit=12)
        semantic = [item["by_hunter"]["semantic"] for item in receipts]
        self.assertTrue(all(item["status"] == "ok" for item in semantic),
                        "快照里有 ready 向量 → 语义通道必须真的跑（不许静默降级）")
        self.assertTrue(any(int(item["evidence"]) >= 1 for item in semantic))
        self.assertTrue(all(row["hit"] for row in fleet_rows))

    def test_snapshot_packs_helper(self):
        self.assertEqual(acceptance_tool.snapshot_packs(self.snapshot), [PACK])


if __name__ == "__main__":
    unittest.main()

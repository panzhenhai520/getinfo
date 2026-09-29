#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""TrendAggregateService 单测：mock repository，验证序列组装/状态计算/落库行。"""

import unittest
from datetime import timedelta

from trend_aggregate_service import TrendAggregateService
from utils import get_china_time


class _MockRepo:
    def __init__(self, raw):
        self.raw = raw
        self.upserted = None
        self.upsert_kwargs = None

    def aggregate_trend_keyword_daily(self, *, industry_pack_id, days_back):
        return self.raw

    def upsert_topic_trend_rows(self, *, industry_pack_id, dimension, activation_id, rows):
        self.upserted = rows
        self.upsert_kwargs = {
            "industry_pack_id": industry_pack_id,
            "dimension": dimension,
            "activation_id": activation_id,
        }
        return len(rows)


class _MockComposition:
    def snapshot(self):
        return {"active_industry_pack_id": "test_pack", "active_industry_activation_id": "act1"}


class TrendAggregateServiceTest(unittest.TestCase):
    def test_run_aggregates_and_writes_rows(self):
        today = get_china_time().date()
        d0 = today.isoformat()
        d1 = (today - timedelta(days=1)).isoformat()
        raw = [
            {"day": d1, "keyword": "AI", "article_count": 2, "distinct_source_count": 2},
            {"day": d0, "keyword": "AI", "article_count": 12, "distinct_source_count": 5},
            {"day": d0, "keyword": "机器人", "article_count": 1, "distinct_source_count": 1},
        ]
        repo = _MockRepo(raw)
        svc = TrendAggregateService(repository=repo, composition=_MockComposition())
        result = svc.run(days_back=3, window=2)

        # 2 个关键词 × 3 天 = 6 行
        self.assertIsNotNone(repo.upserted)
        self.assertEqual(len(repo.upserted), 6)
        # 传递了 pack/dimension/activation
        self.assertEqual(repo.upsert_kwargs["industry_pack_id"], "test_pack")
        self.assertEqual(repo.upsert_kwargs["dimension"], "trend_keyword")
        self.assertEqual(repo.upsert_kwargs["activation_id"], "act1")
        # 每行含完整字段
        for r in repo.upserted:
            for key in ("bucket_date", "keyword", "article_count",
                        "distinct_source_count", "is_burst", "burst_score", "state"):
                self.assertIn(key, r)
        # AI 近窗突增（[2,12]）→ BURSTING
        ai_state = next(r["state"] for r in repo.upserted if r["keyword"] == "AI")
        self.assertEqual(ai_state, "BURSTING")
        self.assertEqual(result["keywords"], 2)
        self.assertEqual(result["rows"], 6)
        self.assertEqual(result["bursts"], 1)

    def test_run_skips_when_no_data(self):
        repo = _MockRepo([])
        svc = TrendAggregateService(repository=repo, composition=_MockComposition())
        result = svc.run(days_back=7, window=7)
        self.assertEqual(result["keywords"], 0)
        self.assertIsNone(repo.upserted)  # 无数据不写

    def test_run_missing_days_padded_with_zero(self):
        # 只有一天有数据，其余天应补 0
        today = get_china_time().date()
        raw = [
            {"day": today.isoformat(), "keyword": "AI",
             "article_count": 3, "distinct_source_count": 2},
        ]
        repo = _MockRepo(raw)
        svc = TrendAggregateService(repository=repo, composition=_MockComposition())
        svc.run(days_back=5, window=3)
        ai_rows = [r for r in repo.upserted if r["keyword"] == "AI"]
        self.assertEqual(len(ai_rows), 5)
        # 其中 4 天补 0
        zeros = [r for r in ai_rows if r["article_count"] == 0]
        self.assertEqual(len(zeros), 4)


if __name__ == "__main__":
    unittest.main()

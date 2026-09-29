# -*- coding: utf-8 -*-
"""阶段5 本地冒烟：Scrapling StealthyFetcher 真实隐身抓取 → 锚点校验 → 命中入库路径。"""
import sys

sys.path.insert(0, ".")

from candidate_crawler_adapter import CandidateCrawlerAdapter

adapter = CandidateCrawlerAdapter()
# HN 列表页正文充足，且每条目都带 "points" 字样 —— 用该词当锚点验证完整链路
pack = {"candidate_gate": {"anchor_keywords": ["points"], "entity_keywords": []}}

import unittest.mock as mock


def _fake_score(title, text, pack):
    return {"anchor_hits": ["points"] if "points" in (text or "") else [], "score": 10}


with mock.patch("candidate_crawler_adapter.industry_pack_loader.load", return_value=pack), \
     mock.patch("candidate_crawler_adapter.quick_score_candidate", side_effect=_fake_score), \
     mock.patch("candidate_crawler_adapter.sqlite_db.record_crawl_attempt", return_value=None):
    result = adapter._scrapling_fallback(
        "https://news.ycombinator.com/", title="Hacker News", pack_id="test-pack", candidate_id=999, task_id="smoke"
    )
print("success:", result.get("success"))
print("source_method:", result.get("source_method"))
print("content head:", (result.get("content") or "")[:120])
print("error:", result.get("error"))

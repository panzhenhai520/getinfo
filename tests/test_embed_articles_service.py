#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""EmbedArticlesService 单测：mock repo + embed_client，验证成功/失败/空跳过。"""

import unittest
from unittest.mock import MagicMock

import numpy as np

from embed_articles_service import EmbedArticlesService


class _MockRepo:
    def __init__(self, articles):
        self.articles = articles
        self.upserts = []

    def list_articles_missing_embeddings(self, **kw):
        return self.articles

    def upsert_article_embedding(self, *, article_id, model_id, dim,
                                 vector_blob, content_hash,
                                 status="ready", error_message=""):
        self.upserts.append({
            "article_id": article_id, "status": status, "dim": dim,
            "blob_len": len(vector_blob), "content_hash": content_hash,
        })


def _make_embed(fail_on=""):
    embed = MagicMock()
    embed.model = "bge-m3"
    embed.batch_size = 16

    def _embed(text):
        if fail_on and fail_on in str(text):
            raise RuntimeError("boom")
        return np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float32)

    embed.embed.side_effect = _embed

    def _embed_batch(texts):
        # 模拟真实服务：批量中任一条失败 → 整批抛错（服务端 4xx/5xx）
        for t in texts:
            if fail_on and fail_on in str(t):
                raise RuntimeError("boom")
        return [_embed(t) for t in texts]

    embed.embed_batch.side_effect = _embed_batch
    return embed


class EmbedArticlesServiceTest(unittest.TestCase):
    def test_run_success(self):
        repo = _MockRepo([
            {"article_id": 1, "content_hash": "h1", "content": "第一篇正文"},
            {"article_id": 2, "content_hash": "h2", "content": "第二篇正文"},
        ])
        svc = EmbedArticlesService(repository=repo, embed_client=_make_embed())
        result = svc.run(limit=10, pack_id="test_pack")
        self.assertEqual(result["succeeded"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["dim"], 4)
        self.assertEqual(len(repo.upserts), 2)
        self.assertTrue(all(u["status"] == "ready" for u in repo.upserts))
        self.assertTrue(all(u["blob_len"] == 16 for u in repo.upserts))  # 4 float32 = 16 字节

    def test_run_empty_skips(self):
        repo = _MockRepo([])
        svc = EmbedArticlesService(repository=repo, embed_client=_make_embed())
        result = svc.run()
        self.assertEqual(result["processed"], 0)
        self.assertEqual(repo.upserts, [])

    def test_run_filters_blank_content(self):
        repo = _MockRepo([
            {"article_id": 1, "content_hash": "h1", "content": "   "},  # 空白被过滤
            {"article_id": 2, "content_hash": "h2", "content": "有效正文"},
        ])
        svc = EmbedArticlesService(repository=repo, embed_client=_make_embed())
        result = svc.run()
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["succeeded"], 1)

    def test_run_partial_failure_records_error(self):
        repo = _MockRepo([
            {"article_id": 1, "content_hash": "h1", "content": "正常文章"},
            {"article_id": 2, "content_hash": "h2", "content": "FAIL这篇"},  # 触发失败
        ])
        svc = EmbedArticlesService(repository=repo, embed_client=_make_embed(fail_on="FAIL"))
        result = svc.run()
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 1)
        statuses = {u["article_id"]: u["status"] for u in repo.upserts}
        self.assertEqual(statuses[1], "ready")
        self.assertEqual(statuses[2], "error")


if __name__ == "__main__":
    unittest.main()

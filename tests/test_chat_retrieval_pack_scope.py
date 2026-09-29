# -*- coding: utf-8 -*-
"""RAG 检索行业包范围限定单测：不同行业包不再召回同一批文章。

覆盖：聚合库行池按包过滤、语义召回 allowed_ids 过滤、上下文注入不跨包。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlite_database import SQLiteDatabase

import chat_api


def _seed(db, aid, pack_id, title):
    with db.lock:
        cur = db.connection.cursor()
        cur.execute(
            "INSERT INTO articles(id, url, title, content, domain, publish_date, first_crawled, status)"
            " VALUES(?,?,?,?,?,?,?,'active')",
            (aid, f"https://example.com/a{aid}", title, "正文" * 40, "example.com",
             "2026-09-15", "2026-09-15"),
        )
        cur.execute(
            "INSERT INTO article_intel_classifications(article_id, industry_pack_id,"
            " industry_pack_version, classifier_version, article_content_hash, rule_category,"
            " final_category) VALUES(?,?,?,?,?,?,?)",
            (aid, pack_id, "v1", "test", f"hash{aid}", "other", "other"),
        )
        db.connection.commit()
        cur.close()


class ChatRetrievalPackScopeTest(unittest.TestCase):
    def setUp(self):
        import config as _config
        self._orig = getattr(_config, 'DATABASE_TYPE', None)
        _config.DATABASE_TYPE = 'sqlite'
        self.tmp = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(str(Path(self.tmp.name) / "retrieval.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())
        _seed(self.db, 1, "embodied_ai", "具身智能产业大会即将召开")
        _seed(self.db, 2, "embodied_ai", "机器人关节技术新进展")
        _seed(self.db, 3, "automotive_industry", "汽车行业会议纪要：智能座舱")
        _seed(self.db, 4, "automotive_industry", "新能源车企扩产动态")
        # chat_api 内部以 `from sqlite_database import sqlite_db` 引用，patch 模块全局即可
        import sqlite_database
        self._orig_sqlite_db = sqlite_database.sqlite_db
        sqlite_database.sqlite_db = self.db

    def tearDown(self):
        import config as _config
        import sqlite_database
        if self._orig is not None:
            _config.DATABASE_TYPE = self._orig
        sqlite_database.sqlite_db = self._orig_sqlite_db
        self.db.disconnect()
        self.tmp.cleanup()

    def test_aggregated_rows_scoped_by_pack(self):
        embodied = chat_api._load_aggregated_articles(industry_pack_id="embodied_ai")
        self.assertEqual({r["id"] for r in embodied}, {1, 2})
        auto = chat_api._load_aggregated_articles(industry_pack_id="automotive_industry")
        self.assertEqual({r["id"] for r in auto}, {3, 4})
        all_rows = chat_api._load_aggregated_articles()
        self.assertEqual({r["id"] for r in all_rows}, {1, 2, 3, 4})

    def test_semantic_top_articles_respects_allowed_ids(self):
        import numpy as np
        ids = [1, 2, 3]
        matrix = np.array([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]], dtype=np.float32)
        with patch("chat_api._load_vector_matrix", return_value=(ids, matrix)), \
                patch("chat_api._embed_question", return_value=np.array([0, 1, 0, 0], dtype=np.float32)):
            with patch("chat_api._cfg.INTEL_CHAT_SEMANTIC_ENABLED", True):
                # 无限制：全库 top = [2]
                self.assertEqual(chat_api._semantic_top_articles("问题", k=8), [2])
                # 限定 allowed_ids={2,3} → 仍只回本包 id
                self.assertEqual(chat_api._semantic_top_articles("问题", k=8, allowed_ids={2, 3}), [2])
                # 限定 allowed_ids={3}（本包没有相关向量）→ 空
                self.assertEqual(chat_api._semantic_top_articles("问题", k=8, allowed_ids={3}), [])

    def test_context_never_crosses_pack_on_semantic_fallback(self):
        # 模拟全库语义命中「packB 文章 + packA 文章」——生产实现会在语义层按 allowed_ids
        # 过滤；这里用尊重 allowed_ids 的假实现，验证注入结果只剩本包
        def _fake_semantic(question, k=8, allowed_ids=None):
            return [i for i in (3, 1) if allowed_ids is None or i in allowed_ids]

        with patch("chat_api._semantic_would_run", return_value=True), \
                patch("chat_api._semantic_top_articles", side_effect=_fake_semantic) as _semantic:
            context, recalled = chat_api._format_aggregated_articles_context(
                "最近开了哪些行业会议", industry_pack_id="embodied_ai", max_articles=5,
            )
            # 语义调用必须带上 allowed_ids（本包文章 id 集合）
            kwargs = _semantic.call_args.kwargs
            self.assertIn("allowed_ids", kwargs)
            self.assertEqual(kwargs["allowed_ids"], {1, 2})
            recalled_ids = {r["id"] for r in recalled}
            self.assertTrue(recalled_ids.issubset({1, 2}), recalled_ids)
            self.assertNotIn("汽车行业会议纪要", context)

    def test_keyword_hits_also_scoped(self):
        # 关键词命中同样只在包内行池里找
        with patch("chat_api._semantic_would_run", return_value=False):
            context, recalled = chat_api._format_aggregated_articles_context(
                "汽车行业会议", industry_pack_id="automotive_industry", max_articles=5,
            )
        recalled_ids = {r["id"] for r in recalled}
        self.assertTrue(recalled_ids.issubset({3, 4}), recalled_ids)
        self.assertIn("汽车行业会议纪要", context)


if __name__ == "__main__":
    unittest.main()

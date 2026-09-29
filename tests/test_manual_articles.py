import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

config.DATABASE_TYPE = "sqlite"

from sqlite_database import SQLiteDatabase  # noqa: E402


class ManualArticlesTests(unittest.TestCase):
    """手动发文：草稿保存/发布直入 articles，不送 LLM 管线"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(str(Path(self.temp_dir.name) / "manual.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())
        self.db.analyze_article_spacetime_profile = lambda _article_id: None
        import manual_articles
        manual_articles.ensure_manual_articles_table(self.db)

    def tearDown(self):
        self.db.disconnect()
        self.temp_dir.cleanup()

    def test_save_and_publish_creates_article_with_classification(self):
        import manual_articles
        draft = manual_articles.save_manual_article(self.db, {
            "industry_pack_id": "healthcare_news",
            "topic_key": "smart_hospital", "topic_name": "智慧医院",
            "url": "https://example.com/manual-1",
            "title": "手动发布的智慧医院资讯",
            "tags": ["智慧医院", "评级"],
            "keywords": ["HIS", "电子病历"],
            "trend_words": ["医疗信息化"],
            "content_html": "<h1>标题</h1><p>正文内容<img src='/static/uploads/editor/x.png'></p>",
        })
        self.assertEqual(draft["status"], "draft")
        result = manual_articles.publish_manual_article(self.db, draft["id"])
        article_id = result["article_id"]
        self.assertGreater(article_id, 0)

        cur = self.db.connection.cursor()
        cur.execute("SELECT content, content_markdown, extraction_method, source_method FROM articles WHERE id=?", (article_id,))
        row = cur.fetchone()
        self.assertEqual(row["extraction_method"], "manual")
        self.assertIn("正文内容", row["content"])
        self.assertIn("<img", row["content_markdown"])  # 图文混排原样保留
        cur.execute(
            "SELECT industry_pack_id, topic_tags_json, final_category, result_source FROM article_intel_classifications WHERE article_id=?",
            (article_id,),
        )
        cls = cur.fetchone()
        self.assertEqual(cls["industry_pack_id"], "healthcare_news")
        self.assertEqual(cls["result_source"], "manual")
        self.assertIn("智慧医院", cls["topic_tags_json"])
        cur.close()

    def test_publish_skips_enrich_pipeline(self):
        import manual_articles
        draft = manual_articles.save_manual_article(self.db, {
            "industry_pack_id": "ai_news",
            "topic_key": "model_releases", "topic_name": "模型发布",
            "url": "", "title": "手动 AI 文章",
            "tags": ["大模型"], "keywords": ["大模型"], "trend_words": ["模型发布"],
            "content_html": "<p>正文。</p>",
        })
        result = manual_articles.publish_manual_article(self.db, draft["id"])
        cur = self.db.connection.cursor()
        cur.execute("SELECT count(*) AS n FROM intel_jobs WHERE job_type='enrich'")
        self.assertEqual(cur.fetchone()["n"], 0)  # 不送 LLM 精炼
        cur.execute(
            "SELECT count(*) AS n FROM intel_jobs WHERE job_type='classification'"
        )
        self.assertEqual(cur.fetchone()["n"], 0)  # 不跑自动分类
        cur.close()

    def test_draft_update_and_delete(self):
        import manual_articles
        draft = manual_articles.save_manual_article(self.db, {
            "industry_pack_id": "education_news", "title": "教育草稿", "content_html": "<p>a</p>",
        })
        updated = manual_articles.save_manual_article(
            self.db, {"industry_pack_id": "education_news", "title": "教育草稿改", "content_html": "<p>b</p>"},
            draft_id=draft["id"],
        )
        self.assertEqual(updated["title"], "教育草稿改")
        self.assertTrue(manual_articles.delete_manual_article(self.db, draft["id"]))
        self.assertIsNone(manual_articles.get_manual_article(self.db, draft["id"]))

    def test_published_draft_cannot_re_publish(self):
        import manual_articles
        draft = manual_articles.save_manual_article(self.db, {
            "industry_pack_id": "ai_news", "title": "发布一次", "content_html": "<p>x</p>",
        })
        manual_articles.publish_manual_article(self.db, draft["id"])
        with self.assertRaises(ValueError):
            manual_articles.publish_manual_article(self.db, draft["id"])


if __name__ == "__main__":
    unittest.main()

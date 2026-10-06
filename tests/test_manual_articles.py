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

    # 入库闸门（intel_boilerplate）会判废"去框架后有效正文 < 40 字"的近空内容，
    # 手动发文的正文同样过闸门；本类用例验证的是发布链路而不是闸门本身，
    # 因此给编辑器正文补足真实长度（占位文本不含行业关键词与框架特征词）。
    BODY_FILLER = (
        "<p>本条正文为单元测试夹具生成的占位内容，用于满足入库闸门对有效正文字数的要求，"
        "不包含任何行业关键词，以免影响本用例对发布链路行为的判定。</p>"
    )

    def _body(self, body_html: str) -> str:
        return body_html + self.BODY_FILLER

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
            "content_html": self._body("<p>正文。</p>"),
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
            # 手动发文同样过入库闸门（CRAWL_REQUIRE_KEYWORD_MATCH=true 时必须有
            # 关键词命中），否则第一次发布就会因闸门拒绝而不是"已发布"报错。
            "industry_pack_id": "ai_news", "title": "发布一次",
            "keywords": ["大模型"], "tags": ["大模型"],
            "content_html": self._body("<p>x</p>"),
        })
        manual_articles.publish_manual_article(self.db, draft["id"])
        with self.assertRaisesRegex(ValueError, "该草稿已发布"):
            manual_articles.publish_manual_article(self.db, draft["id"])


if __name__ == "__main__":
    unittest.main()

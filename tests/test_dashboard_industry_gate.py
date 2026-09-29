import os
import tempfile
import unittest

from intel_database import IntelRepository
from sqlite_database import SQLiteDatabase


class DashboardIndustryGateTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(
            os.path.join(self.temp_dir.name, "dashboard-gate.sqlite3")
        )
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.repository = IntelRepository(self.database)

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _article(self, url, title, content):
        return int(
            self.database.connection.execute(
                """
                INSERT INTO articles(url,title,content,status,content_length)
                VALUES(?,?,?,'active',?)
                """,
                (url, title, content, len(content)),
            ).lastrowid
        )

    def test_recent_focus_and_saved_ai_are_gated_by_active_industry_terms(self):
        automotive_id = self._article(
            "https://example.test/automotive",
            "汽车智能驾驶政策更新",
            "新能源车和自动驾驶行业迎来新规。",
        )
        finance_id = self._article(
            "https://example.test/finance",
            "银行理财产品收益更新",
            "债券和银行存款市场信息。",
        )
        ai_auto_id = self._article(
            "ai://chat/automotive",
            "汽车供应链问答",
            "分析新能源汽车零部件产业链。",
        )
        ai_finance_id = self._article(
            "ai://chat/finance",
            "A股行情问答",
            "分析证券市场成交量。",
        )
        for article_id in (automotive_id, finance_id):
            self.repository.toggle_dashboard_follow(article_id)
        for article_id in (ai_auto_id, ai_finance_id):
            self.database.upsert_article_ragflow_document(
                article_id,
                "news-kb",
                f"doc-{article_id}",
                f"doc-{article_id}.txt",
                "parsed",
            )

        keywords = ["汽车", "新能源汽车"]
        followed = self.repository.dashboard_followed_articles(
            limit=20,
            project_keywords=keywords,
        )
        saved_ai, total = self.repository.list_ai_recent_articles(
            page=1,
            per_page=20,
            project_keywords=keywords,
        )

        self.assertEqual([item["article_id"] for item in followed], [automotive_id])
        self.assertEqual([item["article_id"] for item in saved_ai], [ai_auto_id])
        self.assertEqual(total, 1)
        self.assertEqual(followed[0]["matched_keywords"], ["汽车"])


if __name__ == "__main__":
    unittest.main()

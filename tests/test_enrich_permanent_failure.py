#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""enrich 永久失败判据（阶段 12 待办 2）。

实测教训（A 机 2026-10-09）：331 个 enrich 终态失败里 330 个（99.7%）是**零行业信号**内容
（本包下无分类行 166 篇 + 归入 other 164 篇 + event 仅 1 篇），但旧判据只认下 7 个（2.1%），
多烧了 972 次尝试（占全部尝试 74.6%），每次还占一个 lane 槽位并打一次 VPN。

新判据第 4 条：文章在任何包下都没被分到 trend/event → 永久失败（不再重试）。
安全底线：**查不到就按"有信号"处理**，宁可多试一次，也不把可救的作业判成永久失败。
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intel_database import IntelRepository  # noqa: E402
from intel_worker import IntelWorker, _enrich_failure_is_permanent  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402


class PermanentFailureRuleTests(unittest.TestCase):
    def test_existing_rules_still_hold(self):
        self.assertTrue(_enrich_failure_is_permanent({"relevance": "none"}, "长正文" * 50))
        self.assertTrue(_enrich_failure_is_permanent({"content_type": "list"}, "长正文" * 50))
        self.assertTrue(_enrich_failure_is_permanent({}, "太短"))
        self.assertFalse(_enrich_failure_is_permanent({}, "正常长度的正文内容" * 30))

    def test_no_industry_signal_is_permanent(self):
        self.assertTrue(_enrich_failure_is_permanent(
            {}, "正常长度的正文内容" * 30, has_industry_signal=False),
            "零行业信号必须判成永久失败（精炼了也进不了证据池）")

    def test_with_industry_signal_still_retryable(self):
        self.assertFalse(_enrich_failure_is_permanent(
            {}, "正常长度的正文内容" * 30, has_industry_signal=True))

    def test_default_argument_keeps_old_behavior(self):
        """不传新参数时行为与旧判据一致（调用点漏传也不会误判）。"""
        self.assertFalse(_enrich_failure_is_permanent({}, "正常长度的正文内容" * 30))


class ArticleSignalLookupTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "enrich.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.repo = IntelRepository(self.db)
        self.repo._ensure()
        self.worker = IntelWorker(repository=self.repo)

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def _classify(self, article_id, category):
        # 先建文章行（分类表有外键）
        self.db.connection.execute(
            "INSERT INTO articles(id, url, title, content, domain, publish_date,"
            " first_crawled, status) VALUES(?,?,?,?,?,?,?, 'active')",
            (article_id, "https://example.com/enrich/%d" % article_id,
             "测试文章 %d" % article_id, "正文内容 " * 40, "example.com",
             "2026-10-01", "2026-10-01"),
        )
        self.db.connection.execute(
            "INSERT INTO article_intel_classifications(article_id, industry_pack_id,"
            " industry_pack_version, classifier_version, article_content_hash,"
            " rule_category, matched_keywords_json, topic_tags_json, final_category,"
            " score_details_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (article_id, "family_office", "v1", "test", "h%d" % article_id, "other",
             "[]", "[]", category, "{}"),
        )
        self.db.connection.commit()

    def test_other_category_has_no_signal(self):
        self._classify(1, "other")
        self.assertFalse(self.worker._article_has_industry_signal(1),
                         "归入 other 视为无行业信号")

    def test_event_or_trend_has_signal(self):
        self._classify(2, "event")
        self._classify(3, "trend")
        self.assertTrue(self.worker._article_has_industry_signal(2))
        self.assertTrue(self.worker._article_has_industry_signal(3))

    def test_missing_row_has_no_signal(self):
        self.assertFalse(self.worker._article_has_industry_signal(9999))

    def test_lookup_failure_is_treated_as_signal(self):
        """DB 异常时必须返回 True（宁可重试，也不误判永久失败）。"""

        class _Boom:
            def cursor(self):
                raise RuntimeError("db down")

        original = self.repo.db
        try:
            self.repo.db = _Boom()
            self.assertTrue(self.worker._article_has_industry_signal(1))
        finally:
            self.repo.db = original

    def test_call_site_passes_industry_signal(self):
        """调用点必须真的把行业信号传进判据（漏传等于新判据没生效）。"""
        import inspect

        from intel_worker import IntelWorker as _Worker

        source = inspect.getsource(_Worker._handle_enrich)
        self.assertIn("has_industry_signal=", source,
                      "enrich 调用点必须传 has_industry_signal，否则第 4 条判据形同虚设")


if __name__ == "__main__":
    unittest.main()

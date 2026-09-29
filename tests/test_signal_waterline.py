# -*- coding: utf-8 -*-
"""T3.1 sitemap/RSS 外部信号增量过滤单测。"""
import tempfile
import unittest
from pathlib import Path

from sqlite_database import SQLiteDatabase

import crawl_waterline
from crawl_waterline import filter_stale_signals


class SignalWaterlineTest(unittest.TestCase):
    LIST_URL = "https://www.chinaaeri.com/news/category/aeri/"

    def setUp(self):
        import config as _config
        self._orig = getattr(_config, 'DATABASE_TYPE', None)
        _config.DATABASE_TYPE = 'sqlite'
        self.tmp = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(str(Path(self.tmp.name) / "signal.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())

    def tearDown(self):
        import config as _config
        if self._orig is not None:
            _config.DATABASE_TYPE = self._orig
        self.db.disconnect()
        self.tmp.cleanup()

    def test_no_waterline_keeps_all(self):
        links = [{"url": "https://a.com/1", "source_method": "sitemap", "publish_date": "2026-09-10"}]
        result = filter_stale_signals(links, self.LIST_URL, db=self.db)
        self.assertEqual(len(result["keep"]), 1)
        self.assertEqual(result["stale_dropped"], 0)

    def test_stale_signal_dropped_and_new_kept(self):
        # 先推进水位线到 09-15
        seen = [{"url": "https://a.com/s1", "title": "基线文章", "publish_date": "2026-09-15"}]
        crawl_waterline.mark_items_seen(self.LIST_URL, seen, db=self.db)
        crawl_waterline.mark_items_saved(
            self.LIST_URL, [{"url": "https://a.com/s1", "publish_date": "2026-09-15"}],
            ordered_links=seen, db=self.db,
        )
        links = [
            {"url": "https://a.com/old1", "source_method": "sitemap", "publish_date": "2026-09-10"},
            {"url": "https://a.com/new1", "source_method": "sitemap", "publish_date": "2026-09-16"},
            {"url": "https://a.com/feed1", "source_method": "feed", "publish_date": "2026-09-14"},
            {"url": "https://a.com/feed2", "source_method": "feed", "publish_date": "2026-09-17"},
            {"url": "https://a.com/nodate", "source_method": "sitemap"},
            {"url": "https://a.com/plain", "source_method": "html_static", "publish_date": "2026-09-01"},
        ]
        result = filter_stale_signals(links, self.LIST_URL, db=self.db)
        kept = [item["url"] for item in result["keep"]]
        # 旧 sitemap/feed 丢弃；新信号、无日期、非信号来源保留（详情页日期窗口兜底）
        self.assertEqual(result["stale_dropped"], 2)
        self.assertNotIn("https://a.com/old1", kept)
        self.assertNotIn("https://a.com/feed1", kept)
        for expected in ("https://a.com/new1", "https://a.com/feed2", "https://a.com/nodate", "https://a.com/plain"):
            self.assertIn(expected, kept)


if __name__ == "__main__":
    unittest.main()

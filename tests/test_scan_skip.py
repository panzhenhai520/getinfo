# -*- coding: utf-8 -*-
"""T3.3 调度层零请求跳过单测（信号判定 + 信源级跳过决策）。"""
import tempfile
import unittest
from pathlib import Path

from sqlite_database import SQLiteDatabase

import crawl_waterline
from crawl_waterline import check_source_no_change, latest_external_signal, should_skip_scan


class ScanSkipTest(unittest.TestCase):
    LIST_URL = "https://www.chinaaeri.com/news/category/aeri/"

    def setUp(self):
        import config as _config
        self._orig = getattr(_config, 'DATABASE_TYPE', None)
        _config.DATABASE_TYPE = 'sqlite'
        self.tmp = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(str(Path(self.tmp.name) / "skip.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())

    def tearDown(self):
        import config as _config
        if self._orig is not None:
            _config.DATABASE_TYPE = self._orig
        self.db.disconnect()
        self.tmp.cleanup()

    def _visit(self, at):
        key = crawl_waterline.normalize_list_url(self.LIST_URL)
        with self.db.lock:
            cur = self.db.connection.cursor()
            crawl_waterline._ensure_tables(cur)
            cur.execute(
                "INSERT INTO crawl_waterline(list_url, domain, last_crawl_at, last_new_count,"
                " last_max_publish_time, top_item_fingerprints, updated_at)"
                " VALUES(?,?,?,0,'','[]',?)",
                (key, "www.chinaaeri.com", at, at),
            )
            self.db.connection.commit()
            cur.close()

    def test_should_skip_requires_waterline(self):
        self.assertFalse(should_skip_scan(self.LIST_URL, "2026-09-19 10:00:00", db=self.db))

    def test_signal_newer_than_waterline_keeps_scan(self):
        self._visit("2026-09-18 08:00:00")
        self.assertFalse(should_skip_scan(self.LIST_URL, "2026-09-19 10:00:00", db=self.db))

    def test_signal_not_newer_skips(self):
        self._visit("2026-09-18 08:00:00")
        self.assertTrue(should_skip_scan(self.LIST_URL, "2026-09-18 07:59:59", db=self.db))
        self.assertTrue(should_skip_scan(self.LIST_URL, "2026-09-18 08:00:00", db=self.db))

    def test_latest_external_signal_picks_max(self):
        feed = ('<?xml version="1.0"?><rss><channel>'
                '<item><pubDate>Thu, 17 Sep 2026 10:00:00 +0000</pubDate></item></channel></rss>')
        sitemap = ('<urlset><url><lastmod>2026-09-18</lastmod></url>'
                   '<url><lastmod>2026-09-12</lastmod></url></urlset>')
        def fetch(url):
            return feed if "feed" in url else sitemap
        latest = latest_external_signal("https://a.com/feed", "https://a.com/sitemap.xml", fetch)
        self.assertTrue(latest.startswith("2026-09-18"), latest)

    def test_check_source_no_change_skips_with_fake_fetch(self):
        self._visit("2026-09-18 12:00:00")
        calls = []

        def fetch(url):
            calls.append(url)
            return ('<rss><channel><item><pubDate>Thu, 17 Sep 2026 10:00:00 +0000</pubDate></item>'
                    '</channel></rss>' if "feed" in url else "<urlset></urlset>")
        result = check_source_no_change(
            {"source_url": self.LIST_URL, "metadata": {"rss_url": "https://a.com/feed"}},
            db=self.db, fetch_fn=fetch,
        )
        self.assertTrue(result["skip"])
        self.assertEqual(result["reason"], "skipped_no_change")
        self.assertEqual(len(calls), 2)  # 仅 RSS + sitemap 两个信号请求，无列表页请求

    def test_check_source_no_change_new_signal_keeps(self):
        self._visit("2026-09-18 12:00:00")

        def fetch(url):
            return '<rss><channel><item><pubDate>Sat, 19 Sep 2026 10:00:00 +0000</pubDate></item></channel></rss>'
        result = check_source_no_change(
            {"source_url": self.LIST_URL, "metadata": {"rss_url": "https://a.com/feed"}},
            db=self.db, fetch_fn=fetch,
        )
        self.assertFalse(result["skip"])


if __name__ == "__main__":
    unittest.main()

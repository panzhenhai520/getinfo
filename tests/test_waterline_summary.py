# -*- coding: utf-8 -*-
"""T4.2 水位线汇总查询单测。"""
import tempfile
import unittest
from pathlib import Path

from sqlite_database import SQLiteDatabase

import crawl_waterline
from crawl_waterline import waterline_summary


class WaterlineSummaryTest(unittest.TestCase):
    LIST_URL = "https://www.chinaaeri.com/news/category/aeri/"

    def setUp(self):
        import config as _config
        self._orig = getattr(_config, 'DATABASE_TYPE', None)
        _config.DATABASE_TYPE = 'sqlite'
        self.tmp = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(str(Path(self.tmp.name) / "summary.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())

    def tearDown(self):
        import config as _config
        if self._orig is not None:
            _config.DATABASE_TYPE = self._orig
        self.db.disconnect()
        self.tmp.cleanup()

    def test_summary_reflects_waterline_and_saved_counts(self):
        links = [
            {"url": f"https://www.chinaaeri.com/n{i}", "title": f"文{i}", "publish_date": "2026-09-18"}
            for i in range(3)
        ]
        crawl_waterline.mark_items_seen(self.LIST_URL, links, db=self.db)
        crawl_waterline.mark_items_saved(
            self.LIST_URL,
            [{"url": l["url"], "publish_date": "2026-09-18"} for l in links[:2]],
            ordered_links=links, db=self.db,
        )
        rows = waterline_summary(db=self.db)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["list_url"], crawl_waterline.normalize_list_url(self.LIST_URL))
        self.assertEqual(row["last_new_count"], 2)
        self.assertEqual(row["last_max_publish_time"], "2026-09-18")
        self.assertEqual(row["saved_item_count"], 2)
        self.assertTrue(row["last_crawl_at"])

    def test_summary_empty(self):
        self.assertEqual(waterline_summary(db=self.db), [])


if __name__ == "__main__":
    unittest.main()

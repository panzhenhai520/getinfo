# -*- coding: utf-8 -*-
"""T3.2 ETag/Last-Modified 条件请求（304 跳过）单测。"""
import tempfile
import unittest
from pathlib import Path

from sqlite_database import SQLiteDatabase

import crawl_waterline
from crawl_waterline import apply_conditional_get, get_waterline, update_waterline_validators


class ConditionalGetTest(unittest.TestCase):
    LIST_URL = "https://www.chinaaeri.com/news/category/aeri/"

    def setUp(self):
        import config as _config
        self._orig = getattr(_config, 'DATABASE_TYPE', None)
        _config.DATABASE_TYPE = 'sqlite'
        self.tmp = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(str(Path(self.tmp.name) / "cond.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())

    def tearDown(self):
        import config as _config
        if self._orig is not None:
            _config.DATABASE_TYPE = self._orig
        self.db.disconnect()
        self.tmp.cleanup()

    def test_first_fetch_without_validators_returns_200_and_stores(self):
        def fetch(headers):
            self.assertEqual(headers, {})
            return 200, "<html>列表</html>", {"etag": '"abc123"', "last-modified": "Sat, 19 Sep 2026 10:00:00 GMT"}
        result = apply_conditional_get(self.LIST_URL, fetch, db=self.db)
        self.assertTrue(result["changed"])
        self.assertEqual(result["etag"], '"abc123"')
        waterline = get_waterline(self.LIST_URL, db=self.db)
        self.assertEqual(waterline["etag"], '"abc123"')
        self.assertEqual(waterline["last_modified"], "Sat, 19 Sep 2026 10:00:00 GMT")

    def test_second_fetch_sends_validators_and_304_means_unchanged(self):
        update_waterline_validators(self.LIST_URL, etag='"abc123"',
                                    last_modified="Sat, 19 Sep 2026 10:00:00 GMT", db=self.db)
        captured = {}

        def fetch(headers):
            captured.update(headers)
            return 304, "", {}
        result = apply_conditional_get(self.LIST_URL, fetch, db=self.db)
        self.assertFalse(result["changed"])
        self.assertEqual(result["status"], 304)
        self.assertEqual(captured["If-None-Match"], '"abc123"')
        self.assertEqual(captured["If-Modified-Since"], "Sat, 19 Sep 2026 10:00:00 GMT")

    def test_200_updates_validators(self):
        update_waterline_validators(self.LIST_URL, etag='"old"', db=self.db)
        result = apply_conditional_get(
            self.LIST_URL,
            lambda headers: (200, "<html>新内容</html>", {"etag": '"new"'}),
            db=self.db,
        )
        self.assertTrue(result["changed"])
        self.assertEqual(get_waterline(self.LIST_URL, db=self.db)["etag"], '"new"')


if __name__ == "__main__":
    unittest.main()

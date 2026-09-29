#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import tempfile
import unittest
from datetime import datetime, timezone

_BOOTSTRAP_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = os.path.join(
    _BOOTSTRAP_TEMP_DIR.name,
    "bootstrap.sqlite3",
)
os.environ["INTEL_LLM_ENABLED"] = "false"

from intel_contracts import (
    normalize_internal_category,
    parse_time_range,
    public_category,
)
from sqlite_database import SQLiteDatabase


class IntelContractTests(unittest.TestCase):
    def test_category_alias_is_internal_event(self):
        self.assertEqual(normalize_internal_category("today"), "event")
        self.assertEqual(public_category("event"), "today")
        with self.assertRaises(ValueError):
            normalize_internal_category("unknown")

    def test_time_ranges_are_utc_and_hong_kong_today(self):
        now = datetime(2026, 7, 27, 4, 30, tzinfo=timezone.utc)
        start, end = parse_time_range("24h", now=now)
        self.assertEqual((end - start).total_seconds(), 86400)

        start, end = parse_time_range("30d", now=now)
        self.assertEqual((end - start).total_seconds(), 30 * 86400)

        start, end = parse_time_range("today", now=now)
        self.assertEqual(start.isoformat(), "2026-07-26T16:00:00+00:00")
        self.assertEqual(end, now)

    def test_database_baseline_uses_only_temporary_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "intel-test.sqlite3")
            db = SQLiteDatabase(db_path)
            self.assertTrue(db.connect())
            self.assertTrue(db.create_tables())
            self.assertTrue(os.path.exists(db_path))
            self.assertNotEqual(os.path.abspath(db_path), os.path.abspath("crawler_articles.db"))
            db.disconnect()


if __name__ == "__main__":
    unittest.main()

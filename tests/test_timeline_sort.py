# -*- coding: utf-8 -*-
"""T0.1/T0.2 实时动态实时间排序回归（预告时间参与排序）。

规则：/api/intel/timeline 统一按
COALESCE(published_at_utc, publish_date, first_crawled, created_at) DESC, id DESC 排序；
未来日期（预告日期）按其预告时间参与排序（排最前），date_future 标记保留。
"""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import intel_api
from intel_database import IntelRepository
from sqlite_database import SQLiteDatabase


def _dt(days_offset: int, with_time: bool = False) -> str:
    value = datetime.now() + timedelta(days=days_offset)
    return value.strftime("%Y-%m-%d %H:%M:%S" if with_time else "%Y-%m-%d")


class TimelineSortTest(unittest.TestCase):
    def setUp(self):
        import config as _config
        self._orig_db_type = getattr(_config, 'DATABASE_TYPE', None)
        _config.DATABASE_TYPE = 'sqlite'
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "timeline.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.connection = self.database.connection
        # 时间轴 SQL 引用的最小表
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS article_intel_classifications ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " article_id INTEGER NOT NULL, industry_pack_id TEXT NOT NULL DEFAULT ''"
            ")"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS intel_topics ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " topic_name TEXT NOT NULL DEFAULT '', industry_pack_id TEXT NOT NULL DEFAULT ''"
            ")"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS intel_topic_articles ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " article_id INTEGER NOT NULL, topic_id INTEGER NOT NULL, association_score REAL NOT NULL DEFAULT 0"
            ")"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS dynamic_converted ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL DEFAULT '', markdown TEXT NOT NULL DEFAULT ''"
            ")"
        )
        self.connection.commit()
        self.repository = IntelRepository(self.database)
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True)
        self.app.register_blueprint(intel_api.intel_bp)
        self.client = self.app.test_client()
        self.headers = {'Authorization': 'Bearer test-token'}
        patchers = [
            patch.object(intel_api, 'intel_repository', self.repository),
            patch.object(intel_api, 'industry_pack_loader',
                         type('StubLoader', (), {'load': lambda self, pack_id, enabled_only=True, use_published=True: {'name': 'test'}})()),
            patch('pack_user_gate.visibility_filter', return_value=('', [])),
            patch('decorators.user_db',
                  type('StubUserDb', (), {'verify_session': staticmethod(
                      lambda token: {'user_id': 1, 'username': 'tester', 'role': 'admin'})})()),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        import config as _config
        if self._orig_db_type is not None:
            _config.DATABASE_TYPE = self._orig_db_type
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _seed(self, aid, publish_date, published_at_utc, first_crawled):
        self.connection.execute(
            "INSERT INTO articles(id, url, title, domain, publish_date, published_at_utc,"
            " first_crawled, status) VALUES(?,?,?,?,?,?,?,'active')",
            (aid, f"https://example.com/a{aid}", f"文章{aid}", "example.com",
             publish_date, published_at_utc, first_crawled),
        )
        self.connection.execute(
            "INSERT INTO article_intel_classifications(article_id, industry_pack_id,"
            " industry_pack_version, classifier_version, article_content_hash, rule_category,"
            " final_category) VALUES(?,?,?,?,?,?,?)",
            (aid, "auto_test", "v1", "test", f"hash{aid}", "other", "other"),
        )
        self.connection.commit()

    def _events(self):
        resp = self.client.get("/api/intel/timeline?industry_pack_id=auto_test&per_page=20",
                               headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:200])
        data = resp.get_json()
        self.assertTrue(data["success"])
        return data["events"]

    def test_orders_by_publish_date_desc(self):
        self._seed(1, _dt(-3), None, _dt(-1, True))
        self._seed(2, _dt(-1), None, _dt(-3, True))
        self._seed(3, _dt(-2), None, _dt(-2, True))
        events = self._events()
        self.assertEqual([e["id"] for e in events], [2, 3, 1])

    def test_missing_publish_date_falls_back_to_crawl_time(self):
        self._seed(1, None, None, _dt(-1, True))   # 无发布日期 → 按入库时间参与
        self._seed(2, _dt(-5), None, _dt(-3, True))
        events = self._events()
        self.assertEqual([e["id"] for e in events], [1, 2])

    def test_future_preview_date_sorts_first_with_flag(self):
        self._seed(1, _dt(-1), None, _dt(-1, True))
        self._seed(2, _dt(+5), None, _dt(-1, True))   # 预告日期（未来）
        events = self._events()
        self.assertEqual([e["id"] for e in events], [2, 1])
        self.assertTrue(events[0]["date_future"])
        self.assertFalse(events[1]["date_future"])

    def test_two_future_dates_sorted_by_preview_time_desc(self):
        # 两篇未来日期文章：按预告时间倒序（预告时间参与排序）
        self._seed(1, _dt(+5), None, _dt(-1, True))
        self._seed(2, _dt(+3), None, _dt(-1, True))
        self._seed(3, _dt(-1), None, _dt(-1, True))
        events = self._events()
        self.assertEqual([e["id"] for e in events], [1, 2, 3])
        self.assertTrue(events[0]["date_future"])
        self.assertTrue(events[1]["date_future"])

    def test_display_time_same_source_as_sort_key(self):
        # T0.2：显示时间与排序键同源——未来日期条目的 time 显示其预告日期（MM-DD）
        self._seed(1, _dt(+5), None, _dt(-1, True))
        self._seed(2, _dt(-2), None, _dt(-1, True))
        events = self._events()
        expected_mmdd = (datetime.now() + timedelta(days=5)).strftime("%m-%d")
        self.assertEqual(events[0]["time"], expected_mmdd)
        expected_mmdd2 = (datetime.now() + timedelta(days=-2)).strftime("%m-%d")
        self.assertEqual(events[1]["time"], expected_mmdd2)


if __name__ == "__main__":
    unittest.main()

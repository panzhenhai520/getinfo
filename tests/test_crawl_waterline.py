# -*- coding: utf-8 -*-
"""增量爬取一期：水位线模块单测（crawl_waterline）。

覆盖：URL/指纹归一化、连续命中停止、乱序插入容错、旧文日期丢弃、
seen/saved 落库、水位线推进、无新增访问记录。
"""
import tempfile
import unittest
from pathlib import Path

from sqlite_database import SQLiteDatabase

import crawl_waterline


def _link(url, title="", publish_date=""):
    return {"url": url, "title": title, "publish_date": publish_date}


class CrawlWaterlineTest(unittest.TestCase):
    def setUp(self):
        import config as _config
        self._orig_db_type = getattr(_config, 'DATABASE_TYPE', None)
        _config.DATABASE_TYPE = 'sqlite'
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "waterline.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.list_url = "https://www.chinaaeri.com/news/category/aeri/"

    def tearDown(self):
        import config as _config
        if self._orig_db_type is not None:
            _config.DATABASE_TYPE = self._orig_db_type
        self.database.disconnect()
        self.temp_dir.cleanup()

    # ---- 归一化与指纹 ----
    def test_normalize_url_strips_tracking_and_fragment(self):
        raw = "https://Example.com/news/123/?utm_source=wx&utm_medium=share&a=2#sec"
        self.assertEqual(
            crawl_waterline.normalize_item_url(raw),
            "https://example.com/news/123?a=2",
        )
        self.assertEqual(
            crawl_waterline.normalize_list_url("https://example.com/news/category/aeri/"),
            "https://example.com/news/category/aeri",
        )

    def test_fingerprint_stable_and_title_sensitive(self):
        fp1 = crawl_waterline.item_fingerprint("https://a.com/x?utm_source=1", "标题")
        fp2 = crawl_waterline.item_fingerprint("https://a.com/x?utm_source=2", "标题")
        fp3 = crawl_waterline.item_fingerprint("https://a.com/x", "标题改")
        self.assertEqual(fp1, fp2)
        self.assertNotEqual(fp1, fp3)

    # ---- 新条目判定 ----
    def test_first_crawl_keeps_all(self):
        links = [_link("https://a.com/1", "一"), _link("https://a.com/2", "二")]
        result = crawl_waterline.classify_new_items(self.list_url, links, db=self.database)
        self.assertEqual(len(result["keep"]), 2)
        self.assertEqual(result["seen_skipped"], 0)
        self.assertEqual(result["stop_index"], -1)

    def test_consecutive_saved_run_stops(self):
        links = [
            _link("https://a.com/new1", "新1"),
            _link("https://a.com/old1", "旧1"),
            _link("https://a.com/old2", "旧2"),
            _link("https://a.com/old3", "旧3"),
            _link("https://a.com/old4", "旧4"),
        ]
        # 后 4 条全部「已保存」：命中第 3 条连续即停止（前 1 条保留）
        crawl_waterline.mark_items_seen(self.list_url, links, db=self.database)
        crawl_waterline.mark_items_saved(
            self.list_url,
            [{"url": l["url"]} for l in links[1:]],
            ordered_links=links,
            db=self.database,
        )
        result = crawl_waterline.classify_new_items(self.list_url, links, db=self.database)
        self.assertEqual([l["url"] for l in result["keep"]], ["https://a.com/new1"])
        self.assertEqual(result["seen_skipped"], 3)
        self.assertEqual(result["stop_index"], 3)
        self.assertEqual(result["known_run"], 3)

    def test_out_of_order_insert_keeps_new_middle_item(self):
        links = [
            _link("https://a.com/old1", "旧1"),
            _link("https://a.com/mid", "中间新文"),
            _link("https://a.com/old2", "旧2"),
            _link("https://a.com/old3", "旧3"),
            _link("https://a.com/old4", "旧4"),
        ]
        crawl_waterline.mark_items_seen(self.list_url, links, db=self.database)
        crawl_waterline.mark_items_saved(
            self.list_url,
            [{"url": l["url"]} for l in links if l["url"] != "https://a.com/mid"],
            ordered_links=links,
            db=self.database,
        )
        result = crawl_waterline.classify_new_items(self.list_url, links, db=self.database)
        # 旧1 命中(run=1) → mid 新(保留, run=0) → 旧2,3,4 连续 3 条 → 停止
        self.assertEqual([l["url"] for l in result["keep"]], ["https://a.com/mid"])
        self.assertEqual(result["seen_skipped"], 4)
        self.assertEqual(result["stop_index"], 4)

    def test_stale_publish_date_dropped_with_stop(self):
        # 先推进水位线：已见最新发布时间 = 09-15
        seen_links = [_link("https://a.com/s1", "已存", "2026-09-15")]
        crawl_waterline.mark_items_seen(self.list_url, seen_links, db=self.database)
        crawl_waterline.mark_items_saved(
            self.list_url, [{"url": "https://a.com/s1", "publish_date": "2026-09-15"}],
            ordered_links=seen_links, db=self.database,
        )
        links = [
            _link("https://a.com/stale1", "旧1", "2026-09-10"),
            _link("https://a.com/fresh1", "新1", "2026-09-16"),
            _link("https://a.com/fresh2", "新2", "2026-09-16"),
            _link("https://a.com/stale2", "旧2", "2026-09-11"),
            _link("https://a.com/stale3", "旧3", "2026-09-12"),
            _link("https://a.com/stale4", "旧4", "2026-09-13"),
        ]
        result = crawl_waterline.classify_new_items(self.list_url, links, db=self.database)
        self.assertEqual(
            [l["url"] for l in result["keep"]],
            ["https://a.com/fresh1", "https://a.com/fresh2"],
        )
        self.assertEqual(result["stale_dropped"], 4)
        self.assertEqual(result["stop_index"], 5)

    # ---- 落库与推进 ----
    def test_mark_saved_advances_waterline_and_blocks_rerun(self):
        links = [
            _link("https://a.com/a1", "文章A", "2026-09-18"),
            _link("https://a.com/a2", "文章B", "2026-09-18"),
        ]
        self.assertIsNone(crawl_waterline.get_waterline(self.list_url, db=self.database))
        crawl_waterline.mark_items_seen(self.list_url, links, db=self.database)
        advance = crawl_waterline.mark_items_saved(
            self.list_url,
            [{"url": "https://a.com/a2", "publish_date": "2026-09-18"}],
            ordered_links=links,
            db=self.database,
        )
        self.assertEqual(advance["saved"], 1)
        waterline = crawl_waterline.get_waterline(self.list_url, db=self.database)
        self.assertEqual(waterline["last_max_publish_time"], "2026-09-18")
        self.assertEqual(waterline["last_new_count"], 1)
        self.assertEqual(len(waterline["top_item_fingerprints"]), 2)
        # 同一列表再爬：a2 已保存被跳过；a1 未被保存且日期不早于水位线，允许重试
        result = crawl_waterline.classify_new_items(self.list_url, links, db=self.database)
        self.assertEqual([l["url"] for l in result["keep"]], ["https://a.com/a1"])
        self.assertEqual(result["seen_skipped"], 1)

    def test_saved_marks_whole_list_blocked(self):
        links = [_link(f"https://a.com/x{i}", f"文{i}") for i in range(4)]
        crawl_waterline.mark_items_seen(self.list_url, links, db=self.database)
        crawl_waterline.mark_items_saved(
            self.list_url, [{"url": l["url"]} for l in links],
            ordered_links=links, db=self.database,
        )
        result = crawl_waterline.classify_new_items(self.list_url, links, db=self.database)
        self.assertEqual(result["keep"], [])
        self.assertEqual(result["seen_skipped"], 3)  # 第 3 条连续命中即停
        self.assertEqual(result["stop_index"], 2)

    def test_record_visit_without_new_items(self):
        self.assertIsNone(crawl_waterline.get_waterline(self.list_url, db=self.database))
        crawl_waterline.record_waterline_visit(self.list_url, 0, db=self.database)
        waterline = crawl_waterline.get_waterline(self.list_url, db=self.database)
        self.assertIsNotNone(waterline)
        self.assertTrue(waterline["last_crawl_at"])
        self.assertEqual(waterline["last_new_count"], 0)
        # 空列表 + 已有水位线 → 全部过滤为空，判定为「无新增」正常路径
        result = crawl_waterline.classify_new_items(self.list_url, [], db=self.database)
        self.assertEqual(result["keep"], [])


if __name__ == "__main__":
    unittest.main()

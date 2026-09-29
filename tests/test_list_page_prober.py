# -*- coding: utf-8 -*-
"""T2.1 列表页探测器单测：导航候选过滤、sitemap 合并、落库与读取。"""
import tempfile
import unittest
from pathlib import Path

from sqlite_database import SQLiteDatabase

import crawl_listpage
from crawl_listpage import (
    extract_nav_candidates,
    list_list_pages,
    probe_list_pages,
    save_list_pages,
)

HOMEPAGE = """
<html><body>
<nav>
<a href="/news/category/aeri/">新闻动态</a>
<a href="/news/2026/09/18/10086.html">某新闻详情</a>
<a href="/about">关于我们</a>
<a href="/login">登录</a>
<a href="/products/">产品中心</a>
<a href="/notice/list">公告通知</a>
<a href="https://other-site.com/news">外站</a>
</nav>
</body></html>
"""


class ListPageProberTest(unittest.TestCase):
    BASE = "https://www.chinaaeri.com/"

    def test_nav_candidates_filter_details_and_blacklist(self):
        candidates = extract_nav_candidates(self.BASE, HOMEPAGE)
        urls = [c["list_url"] for c in candidates]
        self.assertIn("https://www.chinaaeri.com/news/category/aeri/", urls)
        self.assertIn("https://www.chinaaeri.com/notice/list", urls)
        # 详情页/登录/关于/外站 均被过滤
        for bad in ("10086", "/login", "/about", "other-site.com"):
            self.assertTrue(all(bad not in u for u in urls), f"{bad} 不应出现在候选: {urls}")
        # 动态/新闻栏目强制纳入
        aeri = next(c for c in candidates if c["list_url"] == "https://www.chinaaeri.com/news/category/aeri/")
        self.assertTrue(aeri["force_include"])

    def test_probe_merges_sitemap_and_orders_by_score(self):
        result = probe_list_pages(
            self.BASE,
            html=HOMEPAGE,
            sitemap_urls=["https://www.chinaaeri.com/news/list",
                          "https://www.chinaaeri.com/news/category/aeri/"],
        )
        candidates = result["candidates"]
        self.assertTrue(candidates)
        self.assertEqual(candidates[0]["list_url"], "https://www.chinaaeri.com/news/category/aeri/")
        scores = [c["score"] for c in candidates]
        self.assertEqual(scores, sorted(scores, reverse=True))
        # 同一 URL 双源合并
        merged = next(c for c in candidates if c["list_url"] == "https://www.chinaaeri.com/news/category/aeri/")
        self.assertIn("nav", merged["source"])

    def test_save_and_list_roundtrip(self):
        import config as _config
        orig = getattr(_config, 'DATABASE_TYPE', None)
        _config.DATABASE_TYPE = 'sqlite'
        try:
            tmp = tempfile.TemporaryDirectory()
            db = SQLiteDatabase(str(Path(tmp.name) / "listpages.sqlite3"))
            self.assertTrue(db.connect())
            self.assertTrue(db.create_tables())
            result = probe_list_pages(self.BASE, html=HOMEPAGE)
            saved = save_list_pages(self.BASE, result["candidates"], db=db)
            self.assertEqual(saved, len(result["candidates"]))
            rows = list_list_pages(self.BASE, db=db)
            self.assertEqual(len(rows), len(result["candidates"]))
            # force_include 落库
            forced = [r for r in rows if r["force_include"]]
            self.assertTrue(forced)
            # 幂等：重复保存不新增
            saved2 = save_list_pages(self.BASE, result["candidates"], db=db)
            self.assertEqual(len(list_list_pages(self.BASE, db=db)), len(rows))
            db.disconnect()
            tmp.cleanup()
        finally:
            if orig is not None:
                _config.DATABASE_TYPE = orig


if __name__ == "__main__":
    unittest.main()

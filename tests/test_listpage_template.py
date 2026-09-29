# -*- coding: utf-8 -*-
"""T2.2 列表页模板学习与持久化单测（autoscraper 三字段：标题/链接/日期）。"""
import tempfile
import unittest
from pathlib import Path

from sqlite_database import SQLiteDatabase

from crawl_listpage import extract_listpage_items, learn_listpage_template

LIST_HTML = """
<html><body>
<div class="news-list">
  <div class="card"><h3 class="ct">行业动态一：新产品发布</h3>
    <a class="cu" href="/news/1001.html">详情</a><span class="cd">2026-09-18</span></div>
  <div class="card"><h3 class="ct">行业动态二：政策出台</h3>
    <a class="cu" href="/news/1002.html">详情</a><span class="cd">2026-09-17</span></div>
  <div class="card"><h3 class="ct">行业动态三：市场回暖</h3>
    <a class="cu" href="/news/1003.html">详情</a><span class="cd">2026-09-16</span></div>
</div>
</body></html>
"""


class ListPageTemplateTest(unittest.TestCase):
    LIST_URL = "https://www.chinaaeri.com/news/category/aeri/"

    def setUp(self):
        import config as _config
        self._orig = getattr(_config, 'DATABASE_TYPE', None)
        _config.DATABASE_TYPE = 'sqlite'
        self.tmp = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(str(Path(self.tmp.name) / "tpl.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())

    def tearDown(self):
        import config as _config
        if self._orig is not None:
            _config.DATABASE_TYPE = self._orig
        self.db.disconnect()
        self.tmp.cleanup()

    SAMPLE_TITLES = ["行业动态一：新产品发布", "行业动态二：政策出台", "行业动态三：市场回暖"]
    SAMPLE_URLS = ["/news/1001.html", "/news/1002.html", "/news/1003.html"]
    SAMPLE_DATES = ["2026-09-18", "2026-09-17", "2026-09-16"]

    def _learn(self, titles=None):
        learn = learn_listpage_template(
            self.db, self.LIST_URL, LIST_HTML,
            titles or self.SAMPLE_TITLES, self.SAMPLE_URLS, self.SAMPLE_DATES,
        )
        self.assertTrue(learn.get("success"), learn)
        return learn

    def test_learn_then_extract_three_fields(self):
        self._learn()
        result = extract_listpage_items(self.db, self.LIST_URL, LIST_HTML)
        self.assertEqual(result["hit"], 3)
        self.assertEqual(result["miss"], 0)
        # autoscraper 可能把全角冒号归一为半角，比较时统一归一
        titles = {item["title"].replace("：", ":").replace(":", "") for item in result["items"]}
        expected = {t.replace("：", ":").replace(":", "") for t in self.SAMPLE_TITLES}
        self.assertTrue(titles >= expected, (titles, expected))
        self.assertTrue(any("/news/1001.html" in item["url"] for item in result["items"]))
        self.assertTrue(any("2026-09-18" in item["date"] for item in result["items"]))

    def test_template_persists_across_calls(self):
        self._learn(titles=["行业动态一：新产品发布", "行业动态二：政策出台"])
        # 第二次提取不再学习，直接套用已持久化模板
        result = extract_listpage_items(self.db, self.LIST_URL, LIST_HTML)
        self.assertTrue(result["hit"] >= 2, result)

    def test_miss_without_template(self):
        result = extract_listpage_items(self.db, self.LIST_URL, LIST_HTML)
        self.assertEqual(result["hit"], 0)
        self.assertEqual(result["miss"], 1)
        self.assertTrue(result["error"])

    def test_miss_increments_and_relearn_recovers(self):
        self._learn(titles=["行业动态一：新产品发布"])
        # 结构变化后的新页面：旧模板命中 0，miss_count+1
        changed = LIST_HTML.replace('class="ct"', 'class="newtitle"').replace('class="cu"', 'class="newlink"')
        miss = extract_listpage_items(self.db, self.LIST_URL, changed)
        self.assertEqual(miss["hit"], 0)
        # 用当前页作为最新样例自动重学 → 恢复命中
        recovered = extract_listpage_items(self.db, self.LIST_URL, changed, relearn_html=changed)
        self.assertTrue(recovered["relearned"])
        self.assertTrue(recovered["hit"] >= 1, recovered)


if __name__ == "__main__":
    unittest.main()

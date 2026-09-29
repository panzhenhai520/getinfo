# -*- coding: utf-8 -*-
"""T2.5 非标准结构兜底抽取 + 「动态/新闻」栏目强制纳入单测。"""
import unittest

from crawl_dynamic_column import extract_nonstandard_items
from crawl_listpage import extract_nav_candidates

TICKER_HTML = """
<html><body>
<div class="ticker">
<span class="t-item">某公司发布新品，股价上涨 <a href="/news/9001.html">阅读</a> 2026-09-18</span>
<span class="t-item">行业标准修订征求意见 <a href="/news/9002.html">阅读</a> 2026-09-17</span>
<span class="t-item">新产能基地开工 <a href="/news/9003.html">阅读</a> 2026-09-16</span>
</div>
</body></html>
"""

JSONLD_LIST_HTML = """
<html><body>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"ItemList","itemListElement":[
 {"@type":"ListItem","item":{"@type":"NewsArticle","name":"动态一：政策发布","url":"/n/1","datePublished":"2026-09-18"}},
 {"@type":"ListItem","item":{"@type":"NewsArticle","name":"动态二：市场回暖","url":"/n/2","datePublished":"2026-09-17"}}
]}
</script>
</body></html>
"""

EMBEDDED_JSON_HTML = """
<html><body><div id="app"></div>
<script>
window.__DATA__ = {"list":[{"bt":"快讯：新车型发布","url_show":"/x/101","fbsj":"2026-09-18 09:30:00"},
                            {"bt":"快讯：召回公告","url_show":"/x/102","fbsj":"2026-09-17 10:00:00"}]};
</script>
</body></html>
"""

TABLE_HTML = """
<html><body>
<table><tr><th>标题</th><th>日期</th></tr>
<tr><td><a href="/g/1">中标公告：某项目</a></td><td>2026-09-18</td></tr>
<tr><td><a href="/g/2">采购意向公示</a></td><td>2026-09-17</td></tr>
</table>
</body></html>
"""

NAV_HTML = """
<html><body>
<nav>
<a href="/news/category/aeri/">新闻动态</a>
<a href="/columns/kx/">行业快讯</a>
<a href="/columns/12345">详情页特征</a>
<a href="/about">关于我们</a>
</nav>
</body></html>
"""


class NonStandardColumnTest(unittest.TestCase):
    def test_ticker_line_pairs(self):
        result = extract_nonstandard_items(TICKER_HTML, "https://a.com/")
        self.assertEqual(result["method"], "line_pairs")
        self.assertEqual(len(result["items"]), 3)
        self.assertTrue(all(item["date"] for item in result["items"]))
        self.assertTrue(any("/news/9001.html" in item["url"] for item in result["items"]))

    def test_jsonld_itemlist(self):
        result = extract_nonstandard_items(JSONLD_LIST_HTML, "https://a.com/")
        self.assertEqual(result["method"], "structured_data")
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["items"][0]["title"], "动态一：政策发布")
        self.assertEqual(result["items"][0]["url"], "https://a.com/n/1")
        self.assertEqual(result["items"][0]["date"], "2026-09-18")

    def test_embedded_json_data(self):
        result = extract_nonstandard_items(EMBEDDED_JSON_HTML, "https://a.com/")
        self.assertEqual(result["method"], "embedded_json")
        titles = {item["title"] for item in result["items"]}
        self.assertIn("快讯：新车型发布", titles)
        self.assertIn("快讯：召回公告", titles)
        self.assertTrue(any(item["date"] == "2026-09-18" for item in result["items"]))

    def test_table_rows(self):
        # 表格行常被 line_pairs（锚文本+父级日期）提前命中，两种方法均可，条目与日期必须齐全
        result = extract_nonstandard_items(TABLE_HTML, "https://a.com/")
        self.assertIn(result["method"], ("table_rows", "line_pairs"))
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(all(item["date"] for item in result["items"]))

    def test_empty_returns_none(self):
        result = extract_nonstandard_items("<html><body>无结构内容</body></html>", "https://a.com/")
        self.assertEqual(result["method"], "none")
        self.assertEqual(result["items"], [])

    def test_dynamic_news_anchor_force_include(self):
        candidates = extract_nav_candidates("https://a.com/", NAV_HTML)
        by_url = {c["list_url"]: c for c in candidates}
        self.assertTrue(by_url["https://a.com/news/category/aeri/"]["force_include"])
        self.assertTrue(by_url["https://a.com/columns/kx/"]["force_include"])
        self.assertNotIn("https://a.com/columns/12345", by_url)
        self.assertNotIn("https://a.com/about", by_url)


if __name__ == "__main__":
    unittest.main()

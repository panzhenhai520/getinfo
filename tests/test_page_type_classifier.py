# -*- coding: utf-8 -*-
"""T2.4 页面类型四级分类器单测。"""
import unittest

from crawl_listpage import (
    PAGE_ARTICLE,
    PAGE_DYNAMIC,
    PAGE_HOMEPAGE,
    PAGE_LISTING,
    classify_page_type,
)

ARTICLE_HTML = """
<html><head><title>某公司发布新一代产品</title></head><body>
<article><h1>某公司发布新一代产品</h1>
<time datetime="2026-09-18">2026-09-18</time>
<p>%s</p>
<p>%s</p>
<p>%s</p>
</article></body></html>
""" % ("正文内容" * 60, "技术细节" * 60, "行业影响" * 60)

LISTING_HTML = """
<html><body>
<ul class="news-list">
<li><a href="/news/1001.html">新闻标题一</a><span>2026-09-18</span></li>
<li><a href="/news/1002.html">新闻标题二</a><span>2026-09-17</span></li>
<li><a href="/news/1003.html">新闻标题三</a><span>2026-09-16</span></li>
<li><a href="/news/1004.html">新闻标题四</a><span>2026-09-15</span></li>
</ul>
<a href="?page=2">下一页</a>
</body></html>
"""

DYNAMIC_HTML = """
<html><body><div id="app"></div>
<script>window.__INIT_STATE__ = {};</script>
<script src="/static/main.js"></script><script src="/static/vendor.js"></script>
<script src="/static/runtime.js"></script><script src="/static/app.js"></script>
<script src="/static/chunk.js"></script><script src="/static/more.js"></script>
<script src="/static/x.js"></script><script src="/static/y.js"></script>
</body></html>
"""

HOMEPAGE_HTML = """
<html><body>
<nav><a href="/about">关于我们</a><a href="/products">产品</a>
<a href="/news">新闻</a><a href="/contact">联系</a><a href="/cases">案例</a></nav>
<div>欢迎来到公司官网</div>
</body></html>
"""


class PageTypeClassifierTest(unittest.TestCase):
    def test_article_by_url_and_content(self):
        result = classify_page_type("https://a.com/news/2026/09/18/10086.html", ARTICLE_HTML)
        self.assertEqual(result["page_type"], PAGE_ARTICLE)
        self.assertGreaterEqual(result["confidence"], 0.7)

    def test_listing_by_cards_and_pagination(self):
        result = classify_page_type("https://www.chinaaeri.com/news/category/aeri/", LISTING_HTML)
        self.assertEqual(result["page_type"], PAGE_LISTING)
        self.assertGreaterEqual(result["confidence"], 0.7)

    def test_dynamic_shell(self):
        result = classify_page_type("https://a.com/news/list", DYNAMIC_HTML)
        self.assertEqual(result["page_type"], PAGE_DYNAMIC)
        self.assertGreaterEqual(result["confidence"], 0.65)

    def test_dynamic_empty_html(self):
        result = classify_page_type("https://a.com/news/list", "")
        self.assertEqual(result["page_type"], PAGE_DYNAMIC)

    def test_homepage_navigation(self):
        result = classify_page_type("https://a.com/", HOMEPAGE_HTML)
        self.assertEqual(result["page_type"], PAGE_HOMEPAGE)

    def test_listing_url_signal_only(self):
        # 无 html 特征、仅列表 URL → 列表页
        result = classify_page_type("https://a.com/category/news/", "<html><body>内容</body></html>")
        self.assertEqual(result["page_type"], PAGE_LISTING)

    def test_low_confidence_default_listing(self):
        result = classify_page_type("https://a.com/weird/path", "<html><body>短文本</body></html>")
        self.assertEqual(result["page_type"], PAGE_LISTING)
        self.assertLess(result["confidence"], 0.6)


if __name__ == "__main__":
    unittest.main()

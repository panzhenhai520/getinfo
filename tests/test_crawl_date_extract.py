# -*- coding: utf-8 -*-
"""T2.3 日期分层提取单测：六层各自命中、优先级、异常降级、相对时间折算。"""
import unittest
from datetime import datetime

from crawl_date_extract import (
    extract_date_from_url,
    extract_layered_date,
    parse_relative_time,
)


class LayeredDateExtractTest(unittest.TestCase):
    def test_jsonld_wins_and_highest_priority(self):
        html = (
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","datePublished":"2026-09-18T10:20:30+08:00"}'
            '</script>'
            '<meta property="article:published_time" content="2026-09-17T09:00:00+08:00">'
        )
        result = extract_layered_date(html, "https://a.com/x")
        self.assertEqual(result["date"], "2026-09-18 10:20:30")
        self.assertEqual(result["source"], "jsonld")
        self.assertEqual(result["precision"], "datetime")
        self.assertEqual(result["confidence"], "high")

    def test_jsonld_array_form(self):
        html = ('<script type="application/ld+json">'
                '[{"@type":"NewsArticle","datePublished":"2026-09-16"}]</script>')
        result = extract_layered_date(html)
        self.assertEqual(result["date"], "2026-09-16")
        self.assertEqual(result["source"], "jsonld")

    def test_meta_article_published_time(self):
        html = '<meta property="article:published_time" content="2026-09-15T08:00:00+08:00">'
        result = extract_layered_date(html)
        self.assertEqual(result["date"], "2026-09-15 08:00:00")
        self.assertEqual(result["source"], "meta")

    def test_meta_swapped_attribute_order(self):
        html = '<meta content="2026-09-14" property="og:published_time">'
        result = extract_layered_date(html)
        self.assertEqual(result["date"], "2026-09-14")

    def test_microdata_itemprop(self):
        html = '<div itemprop="datePublished" content="2026-09-13">正文</div>'
        result = extract_layered_date(html)
        self.assertEqual(result["date"], "2026-09-13")
        self.assertEqual(result["source"], "microdata")

    def test_time_tag(self):
        html = '<time datetime="2026-09-12 12:30">2026年9月12日</time>'
        result = extract_layered_date(html)
        self.assertEqual(result["date"], "2026-09-12 12:30:00")
        self.assertEqual(result["source"], "time_tag")

    def test_time_tag_skips_meeting_labels(self):
        html = '<time datetime="2026-10-01">会议时间</time><time datetime="2026-09-11">正文发布时间</time>'
        result = extract_layered_date(html)
        self.assertEqual(result["date"], "2026-09-11")

    def test_url_date(self):
        result = extract_layered_date("", "https://a.com/news/2026/09/10/abc.html")
        self.assertEqual(result["date"], "2026-09-10")
        self.assertEqual(result["source"], "url")

    def test_relative_time_conversion(self):
        base = datetime(2026, 9, 18, 12, 0, 0)
        html = "<p>发布于 3 小时前</p>"
        result = extract_layered_date(html, fetched_at=base)
        self.assertEqual(result["date"], "2026-09-18 09:00:00")
        self.assertEqual(result["source"], "relative")
        self.assertEqual(parse_relative_time("昨天", base), "2026-09-17 12:00:00")
        self.assertEqual(parse_relative_time("2 天前", base), "2026-09-16 12:00:00")

    def test_all_missing_returns_empty(self):
        self.assertEqual(extract_layered_date("<html><body>无日期</body></html>", "https://a.com/x"), {})

    def test_url_date_invalid_month_ignored(self):
        self.assertIsNone(extract_date_from_url("https://a.com/2026/13/01/x"))
        self.assertEqual(extract_date_from_url("https://a.com/2026-09-01/x"), "2026-09-01")


if __name__ == "__main__":
    unittest.main()

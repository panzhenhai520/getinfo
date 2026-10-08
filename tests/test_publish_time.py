# -*- coding: utf-8 -*-
"""发布时间实时抽取单测。

覆盖生产里真实存在的几种形态：中文列表页日期、ISO、英文月份、相对时间、
URL 推断、页面声明（meta/JSON-LD）、以及"抽不到就不能编"的底线。
"""

import unittest
from datetime import datetime, timezone

import publish_time as pt

NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)


class TextDateTests(unittest.TestCase):
    def test_iso_and_chinese_forms(self):
        cases = [
            ("2026-10-07", "2026-10-07", pt.PRECISION_DATE),
            ("2026-10-07 09:30", "2026-10-07T09:30:00", pt.PRECISION_EXACT),
            ("2026-10-07 09:30:15", "2026-10-07T09:30:15", pt.PRECISION_EXACT),
            ("2026年10月7日", "2026-10-07", pt.PRECISION_DATE),
            ("2026年10月7日 09:30", "2026-10-07T09:30:00", pt.PRECISION_EXACT),
            ("2026/10/07", "2026-10-07", pt.PRECISION_DATE),
            ("Oct 7, 2026", "2026-10-07", pt.PRECISION_DATE),
            ("October 7, 2026 09:30", "2026-10-07T09:30:00", pt.PRECISION_EXACT),
            ("7 Oct 2026", "2026-10-07", pt.PRECISION_DATE),
        ]
        for text, expected, precision in cases:
            with self.subTest(text=text):
                value, got = pt.parse_text_date(text, now=NOW)
                self.assertEqual(value, expected)
                self.assertEqual(got, precision)

    def test_chinese_listing_text_with_surrounding_noise(self):
        """真实列表页形态：日期夹在标题和点击数之间。"""
        text = "关于推动算力互联互通的实施意见 2026-10-07 来源：国家发改委 阅读 1234"
        self.assertEqual(pt.parse_text_date(text, now=NOW)[0], "2026-10-07")

    def test_relative_time_uses_scan_moment(self):
        """中文列表页常见"3天前/昨天"：按扫描时刻回推，不能凭空当成今天。"""
        self.assertEqual(pt.parse_text_date("3天前", now=NOW)[0], "2026-10-05")
        self.assertEqual(pt.parse_text_date("昨天 15:00", now=NOW)[0], "2026-10-07")
        self.assertEqual(pt.parse_text_date("2小时前", now=NOW)[0], "2026-10-08")

    def test_year_less_date_does_not_land_in_the_future(self):
        """只写"10月7日"：补当前年；若跑到未来则退一年。"""
        self.assertEqual(pt.parse_text_date("10月7日", now=NOW)[0], "2026-10-07")
        self.assertEqual(pt.parse_text_date("12月25日", now=NOW)[0], "2025-12-25")

    def test_future_and_absurd_dates_are_rejected(self):
        """明显不合理的未来时间宁可判为"没拿到"，也不能写进库。"""
        self.assertEqual(pt.parse_text_date("2030-01-01", now=NOW), ("", ""))
        self.assertEqual(pt.parse_text_date("1999-01-01", now=NOW), ("", ""))
        self.assertEqual(pt.parse_text_date("2026-13-45", now=NOW), ("", ""))

    def test_no_date_returns_empty_not_a_guess(self):
        self.assertEqual(pt.parse_text_date("关于召开行业年会的通知", now=NOW), ("", ""))


class UrlDateTests(unittest.TestCase):
    def test_common_url_shapes(self):
        for url, expected in (
            ("https://x.com/news/2026/10/07/abc.html", "2026-10-07"),
            ("https://x.com/news/2026-10-07/abc", "2026-10-07"),
            ("https://x.com/a/20261007/b.html", "2026-10-07"),
        ):
            with self.subTest(url=url):
                value, precision = pt.parse_url_date(url)
                self.assertEqual(value, expected)
                self.assertEqual(precision, pt.PRECISION_URL)

    def test_url_without_date(self):
        self.assertEqual(pt.parse_url_date("https://x.com/news/12345.html"), ("", ""))


class HtmlMetaTests(unittest.TestCase):
    def test_og_and_jsonld_and_itemprop(self):
        cases = [
            ('<meta property="article:published_time" content="2026-10-07T09:30:00+08:00">',
             "2026-10-07T01:30:00", pt.PRECISION_EXACT),
            ('<meta property="og:published_time" content="2026-10-07T09:30:00Z">',
             "2026-10-07T09:30:00", pt.PRECISION_EXACT),
            ('<meta itemprop="datePublished" content="2026-10-07">',
             "2026-10-07", pt.PRECISION_DATE),
            ('<script type="application/ld+json">{"datePublished":"2026-10-07T09:30:00Z"}</script>',
             "2026-10-07T09:30:00", pt.PRECISION_EXACT),
        ]
        for html, expected, precision in cases:
            with self.subTest(html=html[:40]):
                value, got = pt.from_html_meta(html, now=NOW)
                self.assertEqual(value, expected)
                self.assertEqual(got, precision)

    def test_content_before_property_order(self):
        html = '<meta content="2026-10-07T09:30:00Z" property="article:published_time">'
        self.assertEqual(pt.from_html_meta(html, now=NOW)[0], "2026-10-07T09:30:00")

    def test_no_meta(self):
        self.assertEqual(pt.from_html_meta("<html><body>正文</body></html>", now=NOW), ("", ""))


class ExtractPriorityTests(unittest.TestCase):
    def test_html_meta_beats_listing_and_url(self):
        result = pt.extract(
            html='<meta property="article:published_time" content="2026-10-07T09:30:00Z">',
            listing_text="2026-10-01",
            url="https://x.com/2026/09/01/a.html",
            now=NOW,
        )
        self.assertEqual(result["source"], "html_meta")
        self.assertEqual(result["published_at"], "2026-10-07T09:30:00")

    def test_listing_beats_url(self):
        result = pt.extract(listing_text="2026-10-07 来源：某协会",
                            url="https://x.com/2026/09/01/a.html", now=NOW)
        self.assertEqual(result["source"], "listing_text")
        self.assertEqual(result["precision"], pt.PRECISION_DATE)

    def test_url_is_last_resort(self):
        result = pt.extract(url="https://x.com/2026/10/07/a.html", now=NOW)
        self.assertEqual(result["source"], "url")
        self.assertEqual(result["precision"], pt.PRECISION_URL)

    def test_nothing_found_marks_discovered_and_does_not_invent(self):
        result = pt.extract(url="https://x.com/news/12345.html", now=NOW)
        self.assertEqual(result["published_at"], "")
        self.assertEqual(result["precision"], pt.PRECISION_DISCOVERED)
        self.assertEqual(result["source"], "none")


class PreferTests(unittest.TestCase):
    def test_higher_precision_overwrites(self):
        self.assertTrue(pt.prefer("", "", {"published_at": "2026-10-07", "precision": pt.PRECISION_DATE}))
        self.assertTrue(pt.prefer("2026-10-07", pt.PRECISION_URL,
                                  {"published_at": "2026-10-07T09:30:00",
                                   "precision": pt.PRECISION_EXACT}))

    def test_lower_precision_never_overwrites(self):
        """已有精确时间，不许被 URL 推的粗糙日期覆盖。"""
        self.assertFalse(pt.prefer("2026-10-07T09:30:00", pt.PRECISION_EXACT,
                                   {"published_at": "2026-10-01", "precision": pt.PRECISION_URL}))

    def test_empty_candidate_never_overwrites(self):
        self.assertFalse(pt.prefer("2026-10-07", pt.PRECISION_DATE,
                                   {"published_at": "", "precision": pt.PRECISION_DISCOVERED}))


if __name__ == "__main__":
    unittest.main()

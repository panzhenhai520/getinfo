#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「信源检查」RSS 分流回归：空 feed 不能再报“结构不支持”。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

EMPTY_FEED = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<rss version="2.0"><channel><title>t</title>'
    b"<pubDate>Sun, 23 Sep 2018 10:55:05 GMT</pubDate>"
    b"<category><![CDATA[]]></category></channel></rss>"
)
FULL_FEED = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<rss version="2.0"><channel>'
    b"<item><title>A</title><link>http://x/a</link>"
    b"<pubDate>Sat, 3 Oct 2026 18:09:14 +0800</pubDate></item>"
    b"<item><title>B</title><link>http://x/b</link>"
    b"<pubDate>Sat, 3 Oct 2026 17:09:14 +0800</pubDate></item>"
    b"</channel></rss>"
)


class FakeResponse:
    def __init__(self, content, content_type="text/xml", status_code=200):
        self.content = content
        self.content_type = content_type
        self.status_code = status_code
        self.url = "http://example.com/feed.xml"


class CheckRssSourceTest(unittest.TestCase):
    def _run(self, response):
        import intel_api
        import intel_light_scanner

        client = type("C", (), {"get": staticmethod(lambda url, headers=None: response)})()
        scanner = type("S", (), {"http_client": client})()
        with mock.patch.object(intel_light_scanner, "RSSScanner", lambda: scanner):
            return intel_api.check_rss_source("http://example.com/feed.xml")

    def test_empty_feed_reports_empty_not_structure_error(self):
        """新浪那种“合法 XML 但没有 item”的订阅：应报空 feed，而不是网页结构不支持。"""
        result = self._run(FakeResponse(EMPTY_FEED))
        self.assertTrue(result["ok"])
        self.assertTrue(result["empty"])
        self.assertEqual(result["entry_count"], 0)
        self.assertIn("不含任何条目", result["message"])
        self.assertNotIn("结构", result["message"])

    def test_full_feed_reports_entries(self):
        result = self._run(FakeResponse(FULL_FEED))
        self.assertTrue(result["ok"])
        self.assertFalse(result["empty"])
        self.assertEqual(result["entry_count"], 2)
        self.assertEqual(result["sample_title"], "A")
        self.assertIn("校验通过", result["message"])

    def test_html_content_type_is_rejected(self):
        result = self._run(FakeResponse(b"<html></html>", content_type="text/html"))
        self.assertFalse(result["ok"])
        self.assertIn("Content-Type", result["message"])

    def test_http_error_status_is_rejected(self):
        result = self._run(FakeResponse(FULL_FEED, status_code=403))
        self.assertFalse(result["ok"])
        self.assertIn("403", result["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

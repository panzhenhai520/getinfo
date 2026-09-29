import os
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

import dynamic_link_converter
from dynamic_link_converter import (
    convert_url_to_markdown,
    get_cached_markdown,
)
from intel_http import HTTPFetchResult, UnsafeExternalURLError, validate_external_url
from sqlite_database import SQLiteDatabase


def _fake_resolver(host, port):
    """IP 字面量按字面量解析（保证内网/环回被识别）；域名统一解析为公网 IP。"""
    import ipaddress
    try:
        address = ipaddress.ip_address(host)
        return [(2, 1, 6, "", (str(address), 0))]
    except ValueError:
        return [(2, 1, 6, "", ("93.184.216.34", 0))]


FAKE_RESOLVER = _fake_resolver


class DynamicLinkConverterTests(unittest.TestCase):
    """阶段3：实时动态 HTML 链接自动转换（SSRF 防护 + 缓存 + 上限）"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.tmp.name, "conv.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())

    def tearDown(self):
        self.db.disconnect()
        self.tmp.cleanup()

    def _html_result(self, url="https://example.com/news/1"):
        return HTTPFetchResult(
            url=url,
            status_code=200,
            content=(
                "<html><head><title>行业动态</title></head><body>"
                "<h1>某公司发布新品</h1><p>近日，某公司发布新一代产品。</p>"
                "<table><tr><th>指标</th><th>数值</th></tr><tr><td>销量</td><td>1000</td></tr></table>"
                "</body></html>"
            ).encode("utf-8"),
            content_type="text/html",
            encoding="utf-8",
        )

    def test_validate_external_url_rejects_ssrf_targets(self):
        for bad in (
            "file:///etc/passwd",
            "ftp://example.com/x",
            "http://127.0.0.1/admin",
            "http://10.0.0.1/admin",
            "http://192.168.1.1/x",
            "http://169.254.169.254/latest/meta-data",
            "http://user:pass@example.com/x",
        ):
            with self.subTest(bad=bad), self.assertRaises(UnsafeExternalURLError):
                validate_external_url(bad, resolver=FAKE_RESOLVER)

    def test_validate_external_url_accepts_public_http(self):
        url = validate_external_url("https://example.com/a?b=1", resolver=FAKE_RESOLVER)
        self.assertEqual(url, "https://example.com/a?b=1")

    def test_convert_html_url_returns_markdown_and_caches(self):
        with patch.object(dynamic_link_converter, "_fetch_safely", return_value=self._html_result()) as fetch_mock:
            result = convert_url_to_markdown(self.db, "https://example.com/news/1")
            self.assertFalse(result["cached"])
            self.assertIn("某公司发布新品", result["markdown"])
            self.assertIn("|", result["markdown"])  # 表格保留
            # 二次调用命中 24h 缓存，不再抓取
            result2 = convert_url_to_markdown(self.db, "https://example.com/news/1")
            self.assertTrue(result2["cached"])
            self.assertEqual(fetch_mock.call_count, 1)
            cached = get_cached_markdown(self.db, "https://example.com/news/1")
            self.assertIn("某公司发布新品", cached or "")

    def test_cache_expires_after_ttl(self):
        with patch.object(dynamic_link_converter, "_fetch_safely", return_value=self._html_result()):
            convert_url_to_markdown(self.db, "https://example.com/news/1")
        # 人为把 converted_at 改旧 → 缓存过期
        self.db._ensure_connection()
        with self.db.lock:
            self.db.connection.execute(
                "UPDATE dynamic_converted SET converted_at='2020-01-01 00:00:00'"
            )
            self.db.connection.commit()
        self.assertIsNone(get_cached_markdown(self.db, "https://example.com/news/1"))

    def test_result_size_capped(self):
        long_html = "<p>" + "内容" * 100000 + "</p>"
        fake = replace(self._html_result(), content=long_html.encode("utf-8"))
        with patch.object(dynamic_link_converter, "MAX_CONVERT_RESULT_BYTES", 400), \
             patch.object(dynamic_link_converter, "_fetch_safely", return_value=fake):
            result = convert_url_to_markdown(self.db, "https://example.com/news/1")
        self.assertIn("已截断", result["markdown"])
        self.assertLessEqual(len(result["markdown"].encode("utf-8")), 400 + 200)

    def test_empty_conversion_raises(self):
        fake = replace(self._html_result(), content=b"<html><body></body></html>")
        with patch.object(dynamic_link_converter, "_fetch_safely", return_value=fake):
            with self.assertRaises(ValueError):
                convert_url_to_markdown(self.db, "https://example.com/news/1")

    def test_missing_url_rejected(self):
        with self.assertRaises(ValueError):
            convert_url_to_markdown(self.db, "")


if __name__ == "__main__":
    unittest.main()

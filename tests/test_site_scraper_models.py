import os
import tempfile
import unittest
from unittest.mock import patch

import site_scraper_models
from intel_http import HTTPFetchResult, UnsafeExternalURLError
from site_scraper_models import (
    delete_site_model,
    extract_with_model,
    get_site_model,
    learn_site_model,
    site_key_of,
)
from sqlite_database import SQLiteDatabase

FIXTURE = """
<html><body>
<header><nav><a href="/about">关于我们</a><a href="/login">登录</a></nav></header>
<div class="news-list">
  <article><h3><a href="/news/1.html">新能源汽车销量创新高</a></h3><span>09-01</span></article>
  <article><h3><a href="/news/2.html">智能驾驶新规正式发布</a></h3><span>09-02</span></article>
  <article><h3><a href="/news/3.html">动力电池回收政策出台</a></h3><span>09-03</span></article>
  <article><h3><a href="/news/4.html">充电桩建设全面提速</a></h3><span>09-04</span></article>
  <article><h3><a href="/news/5.html">车联网数据安全管理强化</a></h3><span>09-05</span></article>
</div>
</body></html>
"""

URL = "https://news.example.com/auto"


class SiteScraperModelTests(unittest.TestCase):
    """阶段4：信源检查一键学习（AutoScraper 简化版）"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["INTEL_SITE_MODEL_DIR"] = os.path.join(self.tmp.name, "models")
        self.db = SQLiteDatabase(os.path.join(self.tmp.name, "models.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())
        # 单测离线：用字面量校验替代真实 DNS 解析（内网/环回依旧拒绝）
        def _offline_validate(url):
            from urllib.parse import urlsplit
            host = (urlsplit(url).hostname or "").casefold()
            if host in {"127.0.0.1", "localhost"} or host.startswith(("10.", "192.168.", "169.254.")):
                from intel_http import UnsafeExternalURLError
                raise UnsafeExternalURLError("外部网址解析到受限网络地址")
            return url
        self._validate_patch = patch.object(site_scraper_models, "validate_external_url", side_effect=_offline_validate)
        self._validate_patch.start()

    def tearDown(self):
        self._validate_patch.stop()
        self.db.disconnect()
        self.tmp.cleanup()
        os.environ.pop("INTEL_SITE_MODEL_DIR", None)

    def test_site_key_normalization(self):
        self.assertEqual(site_key_of("https://www.News.Example.com/a"), "news.example.com")
        self.assertEqual(site_key_of("http://news.example.com:8080/x"), "news.example.com")

    def test_learn_and_extract_roundtrip(self):
        result = learn_site_model(self.db, URL, html=FIXTURE)
        self.assertEqual(result["status"], "active")
        self.assertGreaterEqual(result["sample_count"], 3)
        self.assertGreaterEqual(result["title_avg_len"], 6)
        self.assertEqual(result["site_key"], "news.example.com")
        # 学完即可复用：同一 HTML 上提取出标题+链接
        items = extract_with_model(self.db, URL, FIXTURE)
        self.assertGreaterEqual(len(items), 3)
        titles = [item["title"] for item in items]
        self.assertIn("新能源汽车销量创新高", titles)
        self.assertIn("充电桩建设全面提速", titles)
        self.assertTrue(all(item["url"].startswith("https://news.example.com/news/") for item in items))
        self.assertTrue(all(item.get("source_method") == "site_scraper_model" for item in items))
        # 模型行落库
        row = get_site_model(self.db, "news.example.com")
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "active")

    def test_insufficient_samples_rejected(self):
        html = "<html><body><a href='/a'>第一条标题</a><a href='/b'>第二条标题</a></body></html>"
        with self.assertRaises(ValueError) as ctx:
            learn_site_model(self.db, URL, html=html)
        self.assertIn("结构不支持", str(ctx.exception))

    def test_too_short_titles_rejected(self):
        html = "<html><body>" + "".join(
            f"<a href='/n{i}'>短</a>" for i in range(6)
        ) + "</body></html>"
        with self.assertRaises(ValueError):
            learn_site_model(self.db, URL, html=html)

    def test_ssrf_url_rejected_without_html(self):
        with self.assertRaises((ValueError, UnsafeExternalURLError)):
            learn_site_model(self.db, "http://127.0.0.1/admin")

    def test_invalid_streak_disables_model_and_falls_back(self):
        learn_site_model(self.db, URL, html=FIXTURE)
        # 连续 2 次提取 0 条 → 模型标记失效
        self.assertEqual(extract_with_model(self.db, URL, "<html><body></body></html>"), [])
        self.assertEqual(extract_with_model(self.db, URL, "<html><body></body></html>"), [])
        self.assertIsNone(get_site_model(self.db, "news.example.com"))  # 失效后不再返回模型

    def test_delete_model_then_extract_falls_back(self):
        learn_site_model(self.db, URL, html=FIXTURE)
        self.assertTrue(delete_site_model(self.db, "news.example.com"))
        self.assertIsNone(get_site_model(self.db, "news.example.com"))
        self.assertEqual(extract_with_model(self.db, URL, FIXTURE), [])
        # 模型文件已删除
        import pathlib
        model_file = pathlib.Path(self.tmp.name) / "models" / "news.example.com.json"
        self.assertFalse(model_file.exists())

    def test_extract_without_model_returns_empty(self):
        self.assertEqual(extract_with_model(self.db, URL, FIXTURE), [])

    def test_light_scanner_prefers_model_and_falls_back(self):
        from intel_light_scanner import ListPageScanner
        scanner = ListPageScanner()
        fake_response = HTTPFetchResult(
            url=URL, status_code=200, content=FIXTURE.encode("utf-8"),
            content_type="text/html", encoding="utf-8",
        )
        with patch.object(scanner, "http_client", new=type("FakeClient", (), {"get": lambda self, *a, **k: fake_response})()):
            # 扫描器内部懒加载 sqlite_db 单例 → 替换为测试库
            with patch("sqlite_database.sqlite_db", self.db):
                # 模型命中 → 返回模型条目（source_method=site_scraper_model）
                learn_site_model(self.db, URL, html=FIXTURE)
                items = scanner.scan({"source_url": URL, "metadata": {}}, limit=10)
                self.assertTrue(all(item.get("source_method") == "site_scraper_model" for item in items))
                self.assertIn("新能源汽车销量创新高", [item["title"] for item in items])
                # 模型失效/删除 → 自动回退启发式（scan_html 的普通候选）
                delete_site_model(self.db, "news.example.com")
                fallback_items = scanner.scan({"source_url": URL, "metadata": {}}, limit=10)
                self.assertGreaterEqual(len(fallback_items), 3)
                self.assertTrue(all(item.get("source_method") != "site_scraper_model" for item in fallback_items))


if __name__ == "__main__":
    unittest.main()

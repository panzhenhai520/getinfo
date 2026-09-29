# -*- coding: utf-8 -*-
"""T4.1 LLM 页面识别兜底单测：缓存 30 天、每日限额 5 次、失败降级。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlite_database import SQLiteDatabase

from crawl_page_llm import classify_page_type_with_llm_fallback


class FakeLLMResponse:
    def __init__(self, content):
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class FakeLLMClient:
    configured = True
    calls = 0

    def _local_runtime(self):
        return {"model_id": "fake", "api_key": "x", "base_url": "http://fake", "type": "openai"}

    def _request_local(self, runtime, payload, *, timeout_seconds=None, max_retries=None):
        type(self).calls += 1
        return FakeLLMResponse("listing")


class PageTypeLLMFallbackTest(unittest.TestCase):
    def setUp(self):
        import config as _config
        self._orig = getattr(_config, 'DATABASE_TYPE', None)
        _config.DATABASE_TYPE = 'sqlite'
        self.tmp = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(str(Path(self.tmp.name) / "llm.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())
        FakeLLMClient.calls = 0

    def tearDown(self):
        import config as _config
        if self._orig is not None:
            _config.DATABASE_TYPE = self._orig
        self.db.disconnect()
        self.tmp.cleanup()

    SHORT = "<html><body>短文本</body></html>"

    def _patch(self):
        return patch("crawl_page_llm._ask_llm", side_effect=lambda url, html, llm_client=None: "listing")

    def test_high_confidence_no_llm(self):
        with self._patch() as ask:
            result = classify_page_type_with_llm_fallback(
                "https://a.com/news/2026/09/18/10086.html",
                "<html><article><p>长正文" * 40 + "</article></html>", db=self.db,
            )
        self.assertFalse(result["llm_used"])
        ask.assert_not_called()

    def test_low_confidence_calls_llm_once_then_cache(self):
        with self._patch() as ask:
            first = classify_page_type_with_llm_fallback("https://a.com/weird/123", self.SHORT, db=self.db)
            second = classify_page_type_with_llm_fallback("https://a.com/weird/999", self.SHORT, db=self.db)
        self.assertTrue(first["llm_used"])
        self.assertEqual(first["page_type"], "listing")
        self.assertFalse(second["llm_used"])
        self.assertEqual(second["llm_reason"], "cache_hit")
        self.assertEqual(ask.call_count, 1)

    def test_daily_quota_exhausted(self):
        # 8 个互不相同的路径模式（无数字可归一）→ 前 5 个走 LLM，后 3 个被每日限额拦下
        paths = [f"/{w}" for w in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta")]
        with self._patch() as ask:
            results = [classify_page_type_with_llm_fallback(f"https://b.com{p}", self.SHORT, db=self.db)
                       for p in paths]
        used = [r for r in results if r["llm_used"]]
        self.assertEqual(len(used), 5)  # 每域名每日 ≤5 次
        self.assertIn("daily_quota_exhausted", [r["llm_reason"] for r in results])
        self.assertEqual(ask.call_count, 5)

    def test_llm_unavailable_degrades_to_rule(self):
        with patch("crawl_page_llm._ask_llm", return_value=""):
            result = classify_page_type_with_llm_fallback("https://a.com/weird", self.SHORT, db=self.db)
        self.assertFalse(result["llm_used"])
        self.assertEqual(result["llm_reason"], "llm_unavailable")
        self.assertIn("page_type", result)


if __name__ == "__main__":
    unittest.main()

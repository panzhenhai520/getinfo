# -*- coding: utf-8 -*-
"""确定性硬错误 → 信源自动降级 单测。

背景（A 机实测）：镜像缺 chromium-1091 让 1588 次扫描一秒内失败、长期无人发现。
这类失败重试一万次结果都一样，所以规则是"**第一次出现就止损**"，
而不是像反爬那样累计到阈值。这里钉住三件事：
  1. 只有确定性失败才被判为硬错误，反爬/网络/站点故障不许误判；
  2. 标记后 should_skip_source 立刻为真（沿用既有跳过机制，不用改扫描器）；
  3. 抓到内容后能自动恢复。
"""

import json
import os
import sqlite3
import threading
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

import antibot_detector as ad  # noqa: E402


class HardErrorClassificationTests(unittest.TestCase):
    def test_known_hard_errors_are_classified(self):
        cases = {
            "Error: Executable doesn't exist at /ms-playwright/chromium-1091/chrome-linux/chrome":
                "browser_missing",
            "Please run the following command to download new browsers": "browser_missing",
            "It looks like you are using Playwright Sync API inside the asyncio loop.":
                "sync_api_in_async",
            "No module named 'patchright'": "browser_engine_missing",
            "Read-only file system": "disk_or_permission",
        }
        for text, expected in cases.items():
            with self.subTest(text=text[:40]):
                self.assertEqual(ad.classify_hard_error(text), expected)

    def test_transient_failures_are_not_hard_errors(self):
        """反爬、超时、站点 5xx 都有人管（退避/放弃策略），不许在这里被判死。"""
        for text in (
            "HTTP 403 forbidden",
            "Connection timed out",
            "521 Server Error",
            "外部网址域名解析失败",
            "",
            None,
        ):
            with self.subTest(text=str(text)[:30]):
                self.assertEqual(ad.classify_hard_error(text), "")


class HardErrorDemotionTests(unittest.TestCase):
    def setUp(self):
        self.db = _FakeDb()

    def test_first_occurrence_blocks_the_source(self):
        result = ad.record_source_hard_error(
            1, "browser_missing", "Executable doesn't exist ...chromium-1091",
            db=self.db,
        )
        self.assertTrue(result["recorded"])
        metadata = self.db.metadata(1)
        # 第一次出现就停止派发（不像反爬需要累计到阈值）
        self.assertEqual(metadata[ad.META_STATUS], "blocked")
        self.assertTrue(ad.should_skip_source(metadata))
        self.assertIn("确定性失败", metadata[ad.META_REASON])
        self.assertEqual(metadata[ad.META_HARD_ERROR], "browser_missing")

    def test_hard_error_is_readable_for_the_dashboard(self):
        ad.record_source_hard_error(1, "sync_api_in_async", "detail text", db=self.db)
        info = ad.source_hard_error(self.db.metadata(1))
        self.assertEqual(info["label"], "sync_api_in_async")
        self.assertEqual(info["detail"], "detail text")
        self.assertTrue(info["at"])

    def test_success_clears_the_demotion(self):
        ad.record_source_hard_error(1, "browser_missing", "", db=self.db)
        self.assertTrue(ad.should_skip_source(self.db.metadata(1)))
        ad.record_source_success(1, db=self.db)
        self.assertFalse(ad.should_skip_source(self.db.metadata(1)))

    def test_unknown_source_and_empty_label_are_ignored(self):
        self.assertFalse(ad.record_source_hard_error(999, "browser_missing", db=self.db)["recorded"])
        self.assertFalse(ad.record_source_hard_error(1, "", db=self.db)["recorded"])

    def test_scanner_uses_the_real_hard_error_hooks(self):
        """扫描器必须接上真正的判定与标记函数（而不是 ImportError 兜底的空实现）。"""
        import intel_light_scanner

        self.assertIs(intel_light_scanner._classify_hard_error, ad.classify_hard_error)
        self.assertIs(intel_light_scanner._record_source_hard_error,
                      ad.record_source_hard_error)


class _FakeDb:
    """最小 intel_sources 替身（列名与真实 schema 一致）。"""

    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.connection.execute(
            "CREATE TABLE intel_sources (id INTEGER PRIMARY KEY, source_name TEXT,"
            " source_url TEXT, authority_level INTEGER, is_enabled INTEGER,"
            " metadata_json TEXT, updated_at TEXT)"
        )
        self.connection.execute(
            "INSERT INTO intel_sources(id,source_name,source_url,authority_level,is_enabled,"
            "metadata_json,updated_at) VALUES(1,'源A','https://a.example/feed',3,1,'{}','')"
        )
        self.connection.commit()

    def _ensure_connection(self):
        return None

    def metadata(self, source_id):
        row = self.connection.execute(
            "SELECT metadata_json FROM intel_sources WHERE id=?", (source_id,)
        ).fetchone()
        return json.loads(row["metadata_json"] or "{}")


if __name__ == "__main__":
    unittest.main()

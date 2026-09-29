# -*- coding: utf-8 -*-
"""T4.3 反爬处理宪法单测：状态码识别、域名退避（指数窗口）、成功复位、降级链决策。"""
import tempfile
import unittest
from pathlib import Path

from sqlite_database import SQLiteDatabase

import crawl_policy
from crawl_policy import (
    classify_anti_bot,
    record_backoff,
    record_backoff_success,
    route_fallback_decision,
    should_backoff,
)


class AntiBotPolicyTest(unittest.TestCase):
    def setUp(self):
        import config as _config
        self._orig = getattr(_config, 'DATABASE_TYPE', None)
        _config.DATABASE_TYPE = 'sqlite'
        self.tmp = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(str(Path(self.tmp.name) / "policy.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())

    def tearDown(self):
        import config as _config
        if self._orig is not None:
            _config.DATABASE_TYPE = self._orig
        self.db.disconnect()
        self.tmp.cleanup()

    def test_anti_bot_classification(self):
        for code in (403, 406, 412, 429, 503):
            self.assertTrue(classify_anti_bot(status_code=code), code)
        self.assertFalse(classify_anti_bot(status_code=200))
        self.assertTrue(classify_anti_bot(error_text="HTTP 429 Too Many Requests"))
        self.assertTrue(classify_anti_bot(error_text="出现验证码 captcha"))
        self.assertFalse(classify_anti_bot(error_text="连接超时"))

    def test_backoff_window_grows_exponentially_and_expires(self):
        domain = "blocked.example.com"
        first = record_backoff(domain, 403, db=self.db)
        self.assertTrue(first["backoff"])
        self.assertEqual(first["fail_count"], 1)
        self.assertEqual(first["backoff_seconds"], 60)
        self.assertTrue(should_backoff(domain, db=self.db))
        second = record_backoff(domain, 429, db=self.db)
        self.assertEqual(second["fail_count"], 2)
        self.assertEqual(second["backoff_seconds"], 300)
        # 手动把窗口改为已过期 → 退避解除
        with self.db.lock:
            cur = self.db.connection.cursor()
            crawl_policy.ensure_crawl_domain_backoff_table(cur)
            cur.execute("UPDATE crawl_domain_backoff SET backoff_until='2000-01-01 00:00:00' WHERE domain=?",
                        (domain,))
            self.db.connection.commit()
            cur.close()
        self.assertFalse(should_backoff(domain, db=self.db))

    def test_success_resets_backoff(self):
        domain = "recover.example.com"
        record_backoff(domain, 403, db=self.db)
        self.assertTrue(should_backoff(domain, db=self.db))
        record_backoff_success(domain, db=self.db)
        self.assertFalse(should_backoff(domain, db=self.db))

    def test_fallback_route_decision(self):
        self.assertEqual(route_fallback_decision(status_code=403, have_remote=True), "vpn_list")
        self.assertEqual(route_fallback_decision(status_code=403, have_remote=False), "vpn_ocr")
        # 非反爬失败 → 停止硬攻等下一轮
        self.assertEqual(route_fallback_decision(error_text="timeout", have_remote=True), "give_up")


if __name__ == "__main__":
    unittest.main()

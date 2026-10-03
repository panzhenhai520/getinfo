#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：同名包用户跨行业包时，验证码登录不得“写一行、读另一行”。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlite_database import SQLiteDatabase  # noqa: E402


class PackUserDuplicateLoginTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        import config
        import pack_tenant

        self.config = config
        self._orig_type = getattr(config, "DATABASE_TYPE", None)
        self._orig_smtp = getattr(config, "SMTP_HOST", None)
        config.DATABASE_TYPE = "sqlite"          # 只验证包用户表逻辑，走隔离 SQLite
        config.SMTP_HOST = ""                    # 测试禁止真的发信
        self.pack_tenant = pack_tenant
        self._orig_db = pack_tenant.sqlite_db
        pack_tenant.sqlite_db = SQLiteDatabase(
            os.path.join(self.temp_dir.name, "pack-tenant.sqlite3")
        )
        pack_tenant._pack_uniqueness_done = True  # 由用例自行控制迁移时机
        pack_tenant._ensure()
        # 两个行业包下的同名、同口令用户（UNIQUE 是 (industry_pack_id, username)，合法存在）
        pack_tenant.create_pack_user(
            industry_pack_id="pack_a", username="alice",
            password="Passw0rd", email="alice_a@example.com",
        )
        pack_tenant.create_pack_user(
            industry_pack_id="pack_b", username="alice",
            password="Passw0rd", email="alice_b@example.com",
        )

    def tearDown(self):
        test_db = self.pack_tenant.sqlite_db
        self.pack_tenant.sqlite_db = self._orig_db
        try:
            if getattr(test_db, "connection", None) is not None:
                test_db.connection.close()
        except Exception:
            pass
        self.pack_tenant._pack_uniqueness_done = False
        if self._orig_type is not None:
            self.config.DATABASE_TYPE = self._orig_type
        if self._orig_smtp is not None:
            self.config.SMTP_HOST = self._orig_smtp
        self.temp_dir.cleanup()

    def test_scoped_login_verifies_code(self):
        """按行业包登录：发码写入 pack_b 的行，校验必须命中同一行并激活该行。"""
        pt = self.pack_tenant
        r = pt.begin_login("alice", "Passw0rd", "bind_b@example.com", industry_pack_id="pack_b")
        self.assertEqual(r["industry_pack_id"], "pack_b")
        self.assertFalse(r["activated"])
        code = r.get("dev_code")
        self.assertTrue(code, "未配置 SMTP 时应回传 dev_code")
        v = pt.verify_email_code("alice", code, industry_pack_id="pack_b")
        self.assertEqual(v["step"], "change_password")
        self.assertEqual(int(pt._find_user_row("alice", None, "pack_b")["activated"]), 1)
        self.assertEqual(int(pt._find_user_row("alice", None, "pack_a")["activated"]), 0)

    def test_unscoped_login_verifies_code(self):
        """不传行业包（旧调用方）：校验优先取持有验证码的那一行，不得串行。"""
        pt = self.pack_tenant
        r = pt.begin_login("alice", "Passw0rd", "bind_x@example.com")
        code = r.get("dev_code")
        self.assertTrue(code)
        v = pt.verify_email_code("alice", code)
        self.assertEqual(v["step"], "change_password")

    def test_uniqueness_migration_removes_duplicates(self):
        """模拟老库（无唯一约束）：迁移应清理重复行、保留最有价值的一行并补唯一索引。"""
        pt = self.pack_tenant
        with pt.sqlite_db.lock:
            cur = pt.sqlite_db.connection.cursor()
            cur.execute("DROP TABLE pack_users")
            cur.execute(
                "CREATE TABLE pack_users ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " industry_pack_id TEXT NOT NULL, username TEXT NOT NULL,"
                " password_hash TEXT NOT NULL, email TEXT NOT NULL,"
                " status TEXT NOT NULL DEFAULT 'active',"
                " activated INTEGER NOT NULL DEFAULT 0,"
                " email_verify_code TEXT NOT NULL DEFAULT '',"
                " email_verify_expires TEXT NOT NULL DEFAULT '',"
                " created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '')"
            )
            for email, activated in (("d1@example.com", 0), ("d2@example.com", 1)):
                cur.execute(
                    "INSERT INTO pack_users(industry_pack_id,username,password_hash,email,"
                    " activated,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                    ("pack_a", "bob", "h", email, activated, "x", "x"),
                )
            pt.sqlite_db.connection.commit()
            cur.close()

        result = pt.ensure_pack_user_uniqueness()
        self.assertGreaterEqual(result["removed"], 1)

        with pt.sqlite_db.lock:
            cur = pt.sqlite_db.connection.cursor()
            cur.execute(
                "SELECT id, activated FROM pack_users"
                " WHERE industry_pack_id='pack_a' AND username='bob'"
            )
            rows = [dict(r) for r in cur.fetchall()]
            cur.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["activated"]), 1)  # 保留 activated=1 的那行

    def test_resend_reuses_unexpired_code(self):
        """重发未过期时复用同一枚验证码：新旧邮件码一致，用旧邮件的码也能登录。"""
        pt = self.pack_tenant
        first = pt.begin_login("alice", "Passw0rd", "resend_b@example.com", industry_pack_id="pack_b")
        code1 = first.get("dev_code")
        self.assertTrue(code1)
        second = pt.begin_login("alice", "Passw0rd", "resend_b@example.com", industry_pack_id="pack_b")
        code2 = second.get("dev_code")
        self.assertEqual(code1, code2, "重发不应更换仍在有效期内的验证码")
        # 用第一封邮件里的码也能通过
        result = pt.verify_email_code("alice", code1, industry_pack_id="pack_b")
        self.assertEqual(result["step"], "change_password")

    def test_delete_pack_users(self):
        """按行业包清理用户。"""
        pt = self.pack_tenant
        self.assertEqual(pt.delete_pack_users("pack_b"), 1)
        self.assertIsNone(pt._find_user_row("alice", None, "pack_b"))
        self.assertIsNotNone(pt._find_user_row("alice", None, "pack_a"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

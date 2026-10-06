#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试隔离自检：保证 pytest 跑的是临时库，而不是共享主库或仓库里的真实库。

背景（实测教训）：本地 .env 里 `DATABASE_TYPE=postgres` 且 `SQLITE_BACKUP_PATH=data/crawler_articles.db`。
没有 conftest 隔离时，套件会连到共享 PostgreSQL 主库（测试领取真实作业、结果随主库漂移）；
只 `pop("SQLITE_BACKUP_PATH")` 也不行——config 会从 .env 把它重新注入，而它的优先级高于
DATABASE_PATH，于是测试悄悄打开仓库真实的 data/crawler_articles.db（生成 -wal/-shm，
残留的 QA run 还会让 chat 用例恒返回 429）。这里把"必须隔离成功"钉成断言。
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from sqlite_database import sqlite_db  # noqa: E402


class TestIsolationGuardTests(unittest.TestCase):
    def test_backend_is_not_the_shared_postgres(self):
        self.assertEqual(
            "sqlite", str(getattr(config, "DATABASE_TYPE", "")).lower(),
            "测试必须跑在临时 SQLite 上，不能连共享 PostgreSQL 主库")

    def test_sqlite_path_is_a_temporary_file(self):
        import tempfile

        for key in ("DATABASE_PATH", "SQLITE_BACKUP_PATH"):
            value = str(os.environ.get(key) or "")
            self.assertTrue(value, "%s 必须显式指向临时库" % key)
            self.assertTrue(
                Path(value).is_relative_to(Path(tempfile.gettempdir())),
                "%s=%s 必须是系统临时目录下的文件，否则会污染真实数据" % (key, value))

    def test_loaded_database_points_at_the_temporary_file(self):
        sqlite_db._ensure_connection()
        path = Path(str(sqlite_db.db_path)).resolve()
        repo_dir = Path(__file__).resolve().parent.parent
        self.assertNotEqual("postgres", str(getattr(sqlite_db, "backend", "")),
                            "测试库不允许是共享 PostgreSQL 主库")
        self.assertFalse(
            path.is_relative_to(repo_dir),
            "测试库落到仓库目录里了（%s）——这通常是 SQLITE_BACKUP_PATH 从 .env 被重新注入导致" % path)


if __name__ == "__main__":
    unittest.main()

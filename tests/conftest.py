#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试会话级隔离：强制把主库切到临时 SQLite。

背景（本机 .env 实测）：本地开发机的 `DATABASE_TYPE=postgres`，指向共享 PostgreSQL 主库。
pytest 收集阶段没有隔离时会出现两类问题：
  1. 漂移——测试读写的是真实主库，结果随主库内容变化而变（同一份代码两次跑结果不同）；
  2. 污染——测试真的会领取/写入真实作业、文章与行业包版本；
  3. 崩溃——测试自建的 SQLiteDatabase 实例被 GC 后，共享 PG 连接上的游标报
     `ReferenceError: weakly-referenced object no longer exists`。

conftest 在任何测试模块导入 config 之前执行，因此在这里改环境变量是有效的
（config 在导入时就把 DATABASE_TYPE/DATABASE_PATH 读成常量）。
需要真连 PG 的用例应显式自建连接，而不是依赖默认主库。
"""
import os
import tempfile
from pathlib import Path

_TEMP_DIR = tempfile.mkdtemp(prefix="collectinfo-tests-")
_MAIN_DB = str(Path(_TEMP_DIR) / "test_main.sqlite3")

# SQLiteDatabase 的路径优先级是 SQLITE_BACKUP_PATH > DATABASE_PATH，
# 所以必须把 SQLITE_BACKUP_PATH 清掉，否则会连到备份库而不是本临时库。
os.environ.pop("SQLITE_BACKUP_PATH", None)
os.environ["DATABASE_TYPE"] = "sqlite"
os.environ["DATABASE_PATH"] = _MAIN_DB
# 关掉对外部服务的真实调用（LLM/embedding/爬虫），用例各自按需覆盖
os.environ.setdefault("INTEL_LLM_ENABLED", "false")
os.environ.setdefault("INTEL_EMBEDDING_ENABLED", "false")

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""僵尸问答运行回收回归测试。

生产现象：qa_runs 里留下 queued/running 且永远不会推进的行（worker 没跑、或作业入队后
没被领取），活跃配额把它们一直计入 → 用户之后每次提问都只拿到
`USER_CONCURRENCY_LIMIT` 429「当前已有问答正在研究」，而错误体里连阻塞的 run_id 都没有，
前端无法自助取消（实测本机 2 条僵尸 run 就把提问锁死）。
这里钉住：超过阈值没推进的运行会被回收，不再占用配额；未超阈值的不受影响。
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TEMP = tempfile.TemporaryDirectory()
os.environ["DATABASE_TYPE"] = "sqlite"
os.environ["SQLITE_BACKUP_PATH"] = os.path.join(_TEMP.name, "qa-stale.sqlite3")
os.environ["DATABASE_PATH"] = os.environ["SQLITE_BACKUP_PATH"]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402


def _iso(minutes_ago: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class StaleRunExpiryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "qa.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())
        self.store = QaStore(self.db)
        self.store.ensure_schema()   # 建 qa_runs 等问答表（create_tables 只建主库基础表）

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def _insert_run(self, run_id: str, *, status: str, updated_minutes_ago: float,
                    owner: str = "user:1") -> None:
        with self.db.lock:
            self.db.connection.execute(
                """INSERT INTO qa_runs(
                       id, contract_version, session_id, owner_user_id, industry_pack_id,
                       origin, mode, question_hash, question_text, request_json, status,
                       current_stage, idempotency_key, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, "unified-qa-v1", "", owner, "family_office", "getinfo_ui", "standard",
                 "hash-" + run_id, "问题", "{}", status, "plan", "idem-" + run_id,
                 _iso(updated_minutes_ago), _iso(updated_minutes_ago)),
            )
            self.db.connection.commit()

    def test_zombie_run_is_recycled_and_stops_blocking(self):
        self._insert_run("stale-1", status="queued", updated_minutes_ago=120)
        self.assertEqual(1, self.store.count_active_runs("user:1"))

        expired = self.store.expire_stale_runs(1800, "user:1")

        self.assertEqual(["stale-1"], expired)
        self.assertEqual(0, self.store.count_active_runs("user:1"),
                         "回收后不能再占用活跃配额，否则用户仍被锁在 429")
        run = self.store.get_run("stale-1") or {}
        self.assertEqual("failed", str(run.get("status")))
        codes = [str(item.get("code") or "") for item in (run.get("degradation") or [])]
        self.assertIn("STALE_RUN_EXPIRED", codes, "回收原因要写进降级信息，便于排查")

    def test_fresh_run_is_untouched(self):
        self._insert_run("fresh-1", status="running", updated_minutes_ago=1)
        self.assertEqual([], self.store.expire_stale_runs(1800, "user:1"))
        self.assertEqual(1, self.store.count_active_runs("user:1"))
        self.assertEqual("running", str((self.store.get_run("fresh-1") or {}).get("status")))

    def test_expiry_is_scoped_to_owner(self):
        self._insert_run("stale-mine", status="queued", updated_minutes_ago=120, owner="user:1")
        self._insert_run("stale-other", status="queued", updated_minutes_ago=120, owner="user:2")
        expired = self.store.expire_stale_runs(1800, "user:1")
        self.assertEqual(["stale-mine"], expired)
        self.assertEqual(1, self.store.count_active_runs("user:2"),
                         "不该把别人的运行一起回收")

    def test_completed_runs_are_never_touched(self):
        self._insert_run("done-1", status="completed", updated_minutes_ago=500)
        self.assertEqual([], self.store.expire_stale_runs(1800, "user:1"))
        self.assertEqual("completed", str((self.store.get_run("done-1") or {}).get("status")))


if __name__ == "__main__":
    unittest.main()

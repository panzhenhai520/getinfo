#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作业队列防饿死（饥饿保留名额）回归测试。

生产实测背景（A 机，2026-10-06）：
  * 优先级 + 等待积分有封顶（INTEL_JOB_PRIORITY_AGING_CAP=90），而优先级带宽是 -45~+100，
    所以"高优先级类型只要持续到货，低优先级类型就永远赢不了"。
  * 现象：按真实排序键取出的下 20 条全是 15 小时前的 candidate_dispatch(prio=5, eff=95)；
    topic_cluster(-40, eff=50) 积压 1564 条、trend_aggregate(-45, eff=45) 积压 434 条，
    最久 32 小时且 attempt_count=0（从未被领取）。
  * 修复：每次领活先用少量并发槽（INTEL_WORKER_STARVATION_RESERVED_SLOTS）按 FIFO 领取
    "等待超过 INTEL_WORKER_STARVATION_DEADLINE_SECONDS 的饥饿作业"，其余槽位仍按优先级分配。

本测试用**真实**的 claim_jobs（临时 SQLite 库）验证排序语义，避免只验证复刻出来的公式。
本机/生产主库可能是共享 PostgreSQL，因此用例在 setUp 里把 DATABASE_TYPE 临时切到 sqlite，
并对连接后端做硬断言：一旦不是 sqlite 就直接失败，绝不领取真实作业。
"""
import os
import tempfile
import unittest
from datetime import timedelta

import config

# 必须在导入数据库层之前指定本地临时库
_BOOTSTRAP_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = os.path.join(_BOOTSTRAP_TEMP_DIR.name, "starvation.sqlite3")

from intel_contracts import utc_now, utc_text  # noqa: E402
from intel_database import IntelRepository  # noqa: E402
from intel_worker import IntelWorker  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

HIGH_PRIORITY_TYPE = "classification"
LOW_PRIORITY_TYPE = "topic_cluster"


class StarvationFairnessSqlTests(unittest.TestCase):
    """真实 claim_jobs 的排序语义。"""

    def setUp(self):
        self._original_database_type = getattr(config, "DATABASE_TYPE", "sqlite")
        # 临时切到 sqlite：本用例只能碰临时库，绝不能连到共享主库（生产/A/B 机）
        config.DATABASE_TYPE = "sqlite"
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "starvation.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertEqual("sqlite", self.db.backend,
                         "必须使用临时 SQLite 库；连到主库会领取真实作业")
        self.assertTrue(self.db.create_tables())
        self.repo = IntelRepository(self.db)

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        config.DATABASE_TYPE = self._original_database_type
        self.temp_dir.cleanup()

    def _enqueue(self, job_type, count, *, priority, age_seconds=0, prefix=""):
        """入队 count 条作业，并把 created_at 回拨 age_seconds（模拟等待已久）。"""
        ids = []
        for index in range(count):
            job_id, _inserted = self.repo.enqueue_job(
                job_type,
                "%s-%s-%s-%s" % (prefix or job_type, priority, age_seconds, index),
                {"index": index},
                priority=priority,
            )
            ids.append(int(job_id))
        if age_seconds:
            stamp = utc_text(utc_now() - timedelta(seconds=int(age_seconds)))
            with self.db.lock:
                cursor = self.db.connection.cursor()
                for job_id in ids:
                    cursor.execute(
                        "UPDATE intel_jobs SET created_at = ?, updated_at = ? WHERE id = ?",
                        (stamp, stamp, job_id),
                    )
                self.db.connection.commit()
                cursor.close()
        return ids

    def test_priority_channel_starves_low_priority_backlog(self):
        """反例守护：不启用保留名额时，低优先级积压永远排在新鲜高优先级之后。"""
        old_ids = self._enqueue(LOW_PRIORITY_TYPE, 3, priority=-40, age_seconds=32 * 3600,
                                prefix="old")
        self._enqueue(HIGH_PRIORITY_TYPE, 40, priority=100, prefix="fresh")

        claimed = self.repo.claim_jobs(
            "starvation-test", job_types=[HIGH_PRIORITY_TYPE, LOW_PRIORITY_TYPE], limit=8)
        self.assertEqual(8, len(claimed))
        self.assertEqual({HIGH_PRIORITY_TYPE}, {job["job_type"] for job in claimed},
                         "优先级通道下低优先级积压无法被领取（这正是生产饿死的机制）")
        claimed_ids = {int(job["id"]) for job in claimed}
        self.assertFalse(claimed_ids & set(old_ids))

    def test_starved_channel_claims_oldest_low_priority_first(self):
        """保留名额通道：只取饥饿作业，并按 FIFO（最早入队优先）返回。"""
        old_ids = self._enqueue(LOW_PRIORITY_TYPE, 3, priority=-40, age_seconds=32 * 3600,
                                prefix="old")
        self._enqueue(HIGH_PRIORITY_TYPE, 40, priority=100, prefix="fresh")
        cutoff = utc_text(utc_now() - timedelta(
            seconds=int(config.INTEL_WORKER_STARVATION_DEADLINE_SECONDS)))

        claimed = self.repo.claim_jobs(
            "starvation-test",
            job_types=[HIGH_PRIORITY_TYPE, LOW_PRIORITY_TYPE],
            limit=2,
            starved_before=cutoff,
        )
        self.assertEqual(2, len(claimed))
        self.assertEqual({LOW_PRIORITY_TYPE}, {job["job_type"] for job in claimed})
        self.assertEqual(sorted(old_ids)[:2], sorted(int(job["id"]) for job in claimed),
                         "饥饿通道必须按 FIFO 领取最早入队的作业")

    def test_starved_channel_ignores_jobs_within_deadline(self):
        """未到饥饿阈值的作业不能走保留名额，否则等于把优先级通道关掉。"""
        self._enqueue(HIGH_PRIORITY_TYPE, 5, priority=100, prefix="fresh")
        cutoff = utc_text(utc_now() - timedelta(
            seconds=int(config.INTEL_WORKER_STARVATION_DEADLINE_SECONDS)))
        claimed = self.repo.claim_jobs(
            "starvation-test", job_types=[HIGH_PRIORITY_TYPE], limit=5, starved_before=cutoff)
        self.assertEqual([], claimed)

    def test_starved_channel_respects_type_filter(self):
        """保留名额也必须受 lane 的类型白名单约束（否则会抢别的 lane 的活）。"""
        self._enqueue(LOW_PRIORITY_TYPE, 2, priority=-40, age_seconds=32 * 3600, prefix="old-low")
        cutoff = utc_text(utc_now() - timedelta(
            seconds=int(config.INTEL_WORKER_STARVATION_DEADLINE_SECONDS)))
        claimed = self.repo.claim_jobs(
            "starvation-test", job_types=[HIGH_PRIORITY_TYPE], limit=2, starved_before=cutoff)
        self.assertEqual([], claimed)


class _RecordingRepository:
    """只记录调用参数的仓库替身：验证 worker 两段式领活的顺序与名额。"""

    def __init__(self, starved_jobs=None, priority_jobs=None):
        self.starved_jobs = list(starved_jobs or [])
        self.priority_jobs = list(priority_jobs or [])
        self.calls = []

    def claim_jobs(self, worker_id, *, job_types=None, limit=None, lease_seconds=None,
                   starved_before=None):
        self.calls.append({"limit": limit, "starved_before": starved_before})
        if starved_before:
            take, self.starved_jobs = self.starved_jobs[:limit], self.starved_jobs[limit:]
            return take
        take, self.priority_jobs = self.priority_jobs[:limit], self.priority_jobs[limit:]
        return take


def _job(job_id, job_type):
    return {"id": int(job_id), "job_type": job_type, "payload": {}, "status": "running"}


class StarvationReservedSlotsTests(unittest.TestCase):
    """worker 层：保留名额的数量与轮转（每 N 次领活让 1 次走饥饿通道）。"""

    def _worker(self, repository, *, concurrency=8):
        worker = IntelWorker.__new__(IntelWorker)
        worker.repository = repository
        worker.worker_id = "reserved-slot-test"
        worker.job_lease_seconds = 300
        worker._claim_seq = 0
        return worker

    def test_reserved_slots_respect_free_and_config(self):
        worker = self._worker(_RecordingRepository())
        self.assertEqual(1, worker._starvation_reserved_slots(1), "只有 1 个空槽时也只能用 1 个")
        configured = int(config.INTEL_WORKER_STARVATION_RESERVED_SLOTS)
        self.assertEqual(configured, worker._starvation_reserved_slots(8))
        self.assertEqual(0, worker._starvation_reserved_slots(0))

    def test_starved_channel_is_used_one_turn_in_every_n(self):
        """关键性质：饥饿通道不能每次都抢空槽，否则分类会被反过来饿死（A 机实测 15 分钟 0 条）。"""
        repository = _RecordingRepository(
            starved_jobs=[_job(index, LOW_PRIORITY_TYPE) for index in range(1, 20)],
            priority_jobs=[_job(index, HIGH_PRIORITY_TYPE) for index in range(1, 20)],
        )
        worker = self._worker(repository)
        every = worker._starvation_turn_every()
        for _ in range(every * 3):
            worker._claim_for_pump([HIGH_PRIORITY_TYPE, LOW_PRIORITY_TYPE], 1)

        starved_calls = [call for call in repository.calls if call["starved_before"]]
        total_turns = len(repository.calls)
        self.assertEqual(3, len(starved_calls),
                         "每 %d 次领活只允许 1 次走饥饿通道" % every)
        self.assertEqual(every * 3, total_turns)
        # 每次领活都必须真的领到活（free=1 时优先通道不能被保留名额挤成 0）
        self.assertTrue(all(call["limit"] >= 1 for call in repository.calls))

    def test_starved_channel_then_priority_in_same_turn(self):
        repository = _RecordingRepository(
            starved_jobs=[_job(1, LOW_PRIORITY_TYPE), _job(2, LOW_PRIORITY_TYPE)],
            priority_jobs=[_job(3, HIGH_PRIORITY_TYPE), _job(4, HIGH_PRIORITY_TYPE),
                           _job(5, HIGH_PRIORITY_TYPE), _job(6, HIGH_PRIORITY_TYPE)],
        )
        worker = self._worker(repository)
        every = worker._starvation_turn_every()
        worker._claim_seq = every - 1          # 下一次领活即为饥饿轮次
        jobs = worker._claim_for_pump([HIGH_PRIORITY_TYPE, LOW_PRIORITY_TYPE], 8)

        self.assertEqual([1, 2, 3, 4, 5, 6], [int(job["id"]) for job in jobs])
        first, second = repository.calls
        self.assertIsNotNone(first["starved_before"], "饥饿轮次必须先走保留通道")
        self.assertEqual(int(config.INTEL_WORKER_STARVATION_RESERVED_SLOTS), first["limit"])
        self.assertIsNone(second["starved_before"], "同一轮次剩余的槽位仍按优先级领")
        self.assertEqual(8 - first["limit"], second["limit"],
                         "两段合计不能超过空闲槽位，否则会超并发")

    def test_priority_channel_gets_all_slots_when_nothing_is_starved(self):
        repository = _RecordingRepository(
            priority_jobs=[_job(index, HIGH_PRIORITY_TYPE) for index in range(1, 9)])
        worker = self._worker(repository)
        every = worker._starvation_turn_every()
        worker._claim_seq = every - 1
        jobs = worker._claim_for_pump([HIGH_PRIORITY_TYPE], 8)
        self.assertEqual(8, len(jobs))
        self.assertEqual(8, repository.calls[-1]["limit"],
                         "没有饥饿作业时，优先级通道必须能用满全部空闲槽位")

    def test_reserved_slots_can_be_disabled(self):
        original = config.INTEL_WORKER_STARVATION_RESERVED_SLOTS
        config.INTEL_WORKER_STARVATION_RESERVED_SLOTS = 0
        try:
            repository = _RecordingRepository(priority_jobs=[_job(1, HIGH_PRIORITY_TYPE)])
            worker = self._worker(repository)
            for _ in range(worker._starvation_turn_every() * 2):
                worker._claim_for_pump([HIGH_PRIORITY_TYPE], 8)
            self.assertTrue(all(call["starved_before"] is None for call in repository.calls),
                            "关闭保留名额后不允许再走饥饿通道")
        finally:
            config.INTEL_WORKER_STARVATION_RESERVED_SLOTS = original

    def test_starved_channel_failure_falls_back_to_priority(self):
        """保留通道查询失败不能拖垮 lane（例如主库瞬时不可用）。"""
        class _FailingStarved(_RecordingRepository):
            def claim_jobs(self, worker_id, *, job_types=None, limit=None, lease_seconds=None,
                           starved_before=None):
                if starved_before:
                    raise RuntimeError("主库瞬时不可用")
                return super().claim_jobs(
                    worker_id, job_types=job_types, limit=limit, lease_seconds=lease_seconds,
                    starved_before=starved_before)

        repository = _FailingStarved(priority_jobs=[_job(1, HIGH_PRIORITY_TYPE)])
        worker = self._worker(repository)
        worker._claim_seq = worker._starvation_turn_every() - 1
        jobs = worker._claim_for_pump([HIGH_PRIORITY_TYPE], 8)
        self.assertEqual([1], [int(job["id"]) for job in jobs])


if __name__ == "__main__":
    unittest.main()

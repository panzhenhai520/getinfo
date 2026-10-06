#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""worker 看门狗 / 流式调度 / 熔断 / 巡检 / 心跳 的回归测试。

对应生产事故：一条 candidate_dispatch 作业卡了 **19.3 小时**（心跳持续续租，所以租约回收
救不了它），期间整条 lane 只完成 1 个作业、分类积压纹丝不动 —— 因为老实现是
"领一批 20 条 → 等整批跑完 → 再领下一批"，一条卡死作业即等于整条 lane 停摆。
"""
import os
import threading
import time
import unittest
from unittest import mock

from intel_worker import IntelWorker


class _JobStubRepository:
    """只提供调度循环需要的接口；作业由测试自造，绝不触碰真实队列。"""

    def __init__(self, batches):
        self.batches = [list(batch) for batch in batches]
        self.claimed_total = 0
        self.failed = []
        self.completed = []

    def claim_jobs(self, worker_id, *, job_types=None, limit=None, lease_seconds=None,
                   starved_before=None):
        # 饥饿保留通道的调用（starved_before 非空）在本桩里直接返回空，
        # 让作业仍然由优先级通道按批次给出，保持这些用例原有的批次语义。
        if starved_before:
            return []
        if not self.batches:
            return []
        batch = self.batches.pop(0)
        batch = batch[: max(1, int(limit or len(batch)))]
        self.claimed_total += len(batch)
        return batch

    def complete_job(self, job_id, result, *, lease_owner=None):
        self.completed.append(int(job_id))
        return True

    def fail_job(self, job_id, error, *, lease_owner=None, retryable=True):
        self.failed.append((int(job_id), str(error)))
        return "retry_wait" if retryable else "failed"

    def get_job(self, job_id):
        return {"id": int(job_id), "status": "running"}

    def reap_stuck_jobs(self, *, max_runtime_seconds, limit=50):
        return []

    def record_worker_heartbeat(self, *args, **kwargs):
        return None


def _job(job_id, job_type="classification"):
    return {"id": job_id, "job_type": job_type, "payload": {"job_id": job_id},
            "status": "running", "attempt_count": 1}


def _make_worker(jobs_per_batch, runner, *, hard_timeout=1800, heartbeat=1.0):
    worker = IntelWorker.__new__(IntelWorker)
    worker.repository = _JobStubRepository(jobs_per_batch)
    worker.worker_id = "watchdog-test"
    worker.job_lease_seconds = 60
    worker.heartbeat_seconds = heartbeat
    worker.job_hard_timeout_seconds = hard_timeout
    worker.stop_requested = False
    worker.handlers = {job["job_type"] for batch in jobs_per_batch for job in batch}
    worker.handlers = {name: runner for name in worker.handlers}
    worker._context_handler_types = frozenset()
    worker._active_cancel_events = {}
    worker._active_job_lock = threading.Lock()
    worker._job_context = lambda job: type(
        "Ctx", (), {"cancel_event": threading.Event(), "job_type": job["job_type"]})()
    worker._release_job_context = lambda job_id: None
    worker._heartbeat_job = lambda job_id, cancel_event, stop_event: stop_event.wait(
        max(0.1, worker.heartbeat_seconds * 20))
    worker.enqueue_due_periodic_jobs = lambda: None
    worker.lane_name = "test"
    worker._job_pool = None
    worker._job_pool_size = 0
    worker._in_flight = {}
    worker._in_flight_lock = threading.Lock()
    worker._thread_cleanup_pending = {}
    worker._timeout_streak = 0
    worker._lane_cooldown_until = 0.0
    worker._last_reap_at = time.time()
    worker._heartbeat_thread = None
    worker._worker_started_at = "2026-10-06T00:00:00Z"
    return worker


class StreamingDecouplingTests(unittest.TestCase):
    def test_hung_job_does_not_block_claiming_next_batch(self):
        """卡死作业占一个槽，但调度循环必须继续领下一批（不再等整批完成）。"""
        release = threading.Event()

        def runner(payload):
            if payload["job_id"] == 1:
                release.wait(30)          # 模拟卡死
                return {"status": "completed"}
            return {"status": "completed"}

        worker = _make_worker([[_job(1)], [_job(2)], [_job(3)]], runner, hard_timeout=1800)
        with mock.patch.dict(os.environ, {"INTEL_WORKER_JOB_CONCURRENCY": "2"}):
            first = worker._pump_once(job_types=list(worker.handlers))
            self.assertEqual(1, first["claimed"])
            self.assertEqual(1, first["in_flight"])
            # 卡死作业仍在飞，但还有 1 个空槽 → 必须继续领活
            time.sleep(0.2)
            second = worker._pump_once(job_types=list(worker.handlers))
            self.assertEqual(1, second["claimed"], "有空槽就必须继续领活，不能等卡死作业")
            time.sleep(0.3)
            third = worker._pump_once(job_types=list(worker.handlers))
            # 两个槽都被占用（卡死 + 已完成但尚未结算）或已结算后再领，都不该抛异常
            self.assertGreaterEqual(third["in_flight"], 0)
        release.set()
        worker._settle_in_flight({"timed_out": 0})

    def test_timeout_marks_job_retryable_and_frees_slot(self):
        def runner(payload):
            time.sleep(30)
            return {"status": "completed"}

        worker = _make_worker([[_job(7)]], runner, hard_timeout=1)
        worker._pump_once(job_types=list(worker.handlers))
        time.sleep(1.3)
        stats = {"timed_out": 0}
        worker._settle_in_flight(stats)
        self.assertEqual(1, stats["timed_out"])
        self.assertEqual(1, len(worker.repository.failed))
        self.assertIn("看门狗", worker.repository.failed[0][1])
        self.assertEqual(0, len(worker._in_flight), "超时后必须释放槽位")
        self.assertIn("intel-job", list(worker._thread_cleanup_pending)[0]
                      if worker._thread_cleanup_pending else "")


class PerJobTimeoutTests(unittest.TestCase):
    def test_per_type_timeout_override(self):
        worker = _make_worker([[_job(1)]], lambda payload: {"status": "completed"},
                              hard_timeout=1800)
        with mock.patch.dict(os.environ, {"INTEL_JOB_TIMEOUT_LIGHT_SCAN": "120"}):
            self.assertEqual(120, worker._job_timeout_for("light_scan"))
        self.assertEqual(1800, worker._job_timeout_for("light_scan"))
        self.assertEqual(1800, worker._job_timeout_for("enrich"))


class LaneBreakerTests(unittest.TestCase):
    def test_consecutive_timeouts_trigger_cooldown_and_throttle(self):
        def runner(payload):
            time.sleep(30)
            return {"status": "completed"}

        worker = _make_worker([[_job(i)] for i in range(1, 6)], runner, hard_timeout=1)
        with mock.patch.dict(os.environ, {"INTEL_WORKER_JOB_CONCURRENCY": "4"}):
            for _ in range(3):
                worker._pump_once(job_types=list(worker.handlers))
                time.sleep(1.2)
                worker._settle_in_flight({"timed_out": 0})
        self.assertGreaterEqual(worker._timeout_streak, 3)
        self.assertTrue(worker._lane_paused(), "连续超时应进入限流冷却")
        self.assertEqual(1, worker._effective_concurrency(), "冷却期并发应降为 1")
        # 冷却期不再领新活，避免雪崩
        paused = worker._pump_once(job_types=list(worker.handlers))
        self.assertEqual(0, paused["claimed"])

    def test_breaker_recovers_after_cooldown(self):
        worker = _make_worker([[_job(1)]], lambda payload: {"status": "completed"},
                              hard_timeout=1800)
        worker._timeout_streak = 5
        worker._lane_cooldown_until = time.time() - 1     # 冷却已结束
        with mock.patch.dict(os.environ, {"INTEL_WORKER_JOB_CONCURRENCY": "4"}):
            self.assertFalse(worker._lane_paused())
            self.assertEqual(4, worker._effective_concurrency())


class CleanupHookTests(unittest.TestCase):
    def test_timeout_registers_thread_cleanup_for_reuse(self):
        """超时作业的资源清理必须登记到它所在线程，等该线程下次跑作业时在**该线程内**执行。"""
        cleaned = []

        def runner(payload):
            time.sleep(30)
            return {"status": "completed"}

        worker = _make_worker([[_job(11)], [_job(12)]], runner, hard_timeout=1)
        worker._cleanup_thread_resources = lambda job_id, thread_name: cleaned.append(
            (job_id, thread_name))
        worker._pump_once(job_types=list(worker.handlers))
        time.sleep(1.3)
        worker._settle_in_flight({"timed_out": 0})
        self.assertTrue(worker._thread_cleanup_pending, "应登记待清理线程")
        # 模拟"同一个线程被复用"：把待清理项挂到当前线程名下，再让该线程跑下一个作业
        worker._thread_cleanup_pending[threading.current_thread().name] = 11
        worker._run_job_with_cleanup(
            _job(13),
            {13: (type("C", (), {"cancel_event": threading.Event()})(),
                  threading.Event(), threading.Thread())},
            {})
        self.assertTrue(cleaned, "新作业开始前应先清理上一个超时作业的残留资源")


if __name__ == "__main__":
    unittest.main()

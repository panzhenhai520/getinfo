#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""worker 批次并发执行（INTEL_WORKER_JOB_CONCURRENCY）回归测试。

背景（生产实测，A 机）：core lane 串行处理一个批次（20 条），总吞吐只有 ~53 个作业/小时；
classification 稳态到达约 11 篇/小时、队列里还有 5000+ 一次性积压，串行永远排不空。
作业耗时几乎都在等 LLM/网络，把批次内等待并行起来即可成倍提升吞吐。

隔离说明：本仓库的数据库在配了 PG 时是共享主库（SQLiteDatabase 也会连到 PG），
直接跑 claim_jobs 会领到真实作业，所以这里不改产品代码路径之外的东西，
而是把 repository.claim_jobs 换成只返回本测试自造的作业，绝不触碰真实行。
"""
import threading
import time
import unittest

from intel_worker import IntelWorker

JOB_TYPES = ("classification", "enrich")


class _StubRepository:
    """只提供 run_once 需要的最小接口，作业完全由测试自造。"""

    def __init__(self, jobs):
        self.jobs = list(jobs)
        self.completed = []
        self.failed = []

    def claim_jobs(self, worker_id, *, job_types=None, limit=None, lease_seconds=None,
                   starved_before=None):
        # 饥饿保留通道在本桩里不参与（本用例只关心批次并发执行）
        if starved_before:
            return []
        claimed, self.jobs = self.jobs[:], []
        return claimed

    def complete_job(self, job_id, result, *, lease_owner=None):
        self.completed.append(int(job_id))
        return True

    def fail_job(self, job_id, error, *, lease_owner=None, retryable=True):
        self.failed.append((int(job_id), str(error)))
        return "retry_wait" if retryable else "failed"

    def get_job(self, job_id):
        return {"id": int(job_id), "status": "completed"}


def _job(job_id, index):
    return {
        "id": job_id,
        "job_type": "classification",
        "payload": {"index": index},
        "status": "running",
        "attempt_count": 1,
    }


class WorkerBatchConcurrencyTest(unittest.TestCase):
    def _worker(self, runner, jobs, *, hard_timeout=1800):
        worker = IntelWorker.__new__(IntelWorker)
        worker.repository = _StubRepository(jobs)
        worker.worker_id = "concurrency-test"
        worker.job_lease_seconds = 60
        worker.heartbeat_seconds = 0.05
        worker.job_hard_timeout_seconds = hard_timeout
        worker.stop_requested = False
        worker.handlers = {job_type: runner for job_type in JOB_TYPES}
        worker._context_handler_types = frozenset()
        worker._active_cancel_events = {}
        worker._active_job_lock = threading.Lock()
        worker._job_context = lambda job: type(
            "Ctx", (), {"cancel_event": threading.Event(), "job_type": job["job_type"]}
        )()
        worker._release_job_context = lambda job_id: None
        worker._heartbeat_job = lambda job_id, cancel_event, stop_event: stop_event.wait(
            max(0.1, worker.heartbeat_seconds * 20))
        worker.enqueue_due_periodic_jobs = lambda: None
        return worker

    def test_sequential_by_default(self):
        order = []
        lock = threading.Lock()

        def runner(payload):
            with lock:
                order.append(("start", payload["index"]))
            time.sleep(0.05)
            with lock:
                order.append(("end", payload["index"]))
            return {"status": "completed"}

        jobs = [_job(1, 0), _job(2, 1), _job(3, 2)]
        worker = self._worker(runner, jobs)
        stats = worker.run_once(job_types=JOB_TYPES, limit=3, concurrency=1)
        self.assertEqual(3, stats["completed"])
        # 串行：start/end 严格交错，不会出现两个 start 相邻
        self.assertEqual(["start", "end", "start", "end", "start", "end"],
                         [item[0] for item in order])
        self.assertEqual([1, 2, 3], worker.repository.completed)

    def test_concurrent_when_configured(self):
        running = {"now": 0, "peak": 0}
        lock = threading.Lock()

        def runner(payload):
            with lock:
                running["now"] += 1
                running["peak"] = max(running["peak"], running["now"])
            time.sleep(0.2)
            with lock:
                running["now"] -= 1
            return {"status": "completed"}

        jobs = [_job(index + 1, index) for index in range(4)]
        worker = self._worker(runner, jobs)
        stats = worker.run_once(job_types=JOB_TYPES, limit=4, concurrency=4)
        self.assertEqual(4, stats["completed"])
        self.assertGreaterEqual(running["peak"], 2, "并发配置下批次内应真的并行")

    def test_failure_is_isolated_per_job(self):
        def runner(payload):
            if payload["index"] == 1:
                raise RuntimeError("fixture boom")
            return {"status": "completed"}

        jobs = [_job(index + 1, index) for index in range(3)]
        worker = self._worker(runner, jobs)
        stats = worker.run_once(job_types=JOB_TYPES, limit=3, concurrency=3)
        self.assertEqual(2, stats["completed"])
        self.assertEqual(1, stats.get("failed", 0) + stats.get("retry_wait", 0))
        self.assertEqual([2], [job_id for job_id, _err in worker.repository.failed])

    def test_hung_job_does_not_block_the_batch(self):
        """看门狗：一条卡死的作业不能拖住整条 lane（生产实测有作业卡了 19.3 小时）。"""
        finished = []
        lock = threading.Lock()

        def runner(payload):
            if payload["index"] == 1:
                time.sleep(30)          # 模拟卡死（远超看门狗超时）
                return {"status": "completed"}
            with lock:
                finished.append(payload["index"])
            return {"status": "completed"}

        jobs = [_job(index + 1, index) for index in range(3)]
        worker = self._worker(runner, jobs, hard_timeout=1)
        started = time.time()
        stats = worker.run_once(job_types=JOB_TYPES, limit=3, concurrency=3)
        elapsed = time.time() - started
        self.assertLess(elapsed, 10, "看门狗应按超时返回，而不是等卡死作业自己结束")
        self.assertEqual(sorted([0, 2]), sorted(finished), "正常作业应全部完成")
        self.assertEqual(1, stats.get("retry_wait", 0) + stats.get("failed", 0),
                         "超时作业应被记账为可重试失败")
        self.assertEqual([2], [job_id for job_id, _err in worker.repository.failed])

    def test_handler_success_false_counts_as_failure(self):
        def runner(payload):
            return {"success": False, "error": "handler 明确失败", "retryable": True}

        jobs = [_job(1, 0)]
        worker = self._worker(runner, jobs)
        stats = worker.run_once(job_types=JOB_TYPES, limit=1, concurrency=1)
        self.assertEqual(0, stats["completed"])
        self.assertEqual(1, stats.get("retry_wait", 0))

    def test_concurrency_from_env(self):
        def runner(payload):
            return {"status": "completed"}

        jobs = [_job(1, 0), _job(2, 1)]
        worker = self._worker(runner, jobs)
        import os

        original = os.environ.get("INTEL_WORKER_JOB_CONCURRENCY")
        os.environ["INTEL_WORKER_JOB_CONCURRENCY"] = "2"
        try:
            stats = worker.run_once(job_types=JOB_TYPES, limit=2)
        finally:
            if original is None:
                os.environ.pop("INTEL_WORKER_JOB_CONCURRENCY", None)
            else:
                os.environ["INTEL_WORKER_JOB_CONCURRENCY"] = original
        self.assertEqual(2, stats["completed"])


if __name__ == "__main__":
    unittest.main()

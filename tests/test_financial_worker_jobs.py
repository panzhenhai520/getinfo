import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from financial_worker_jobs import (
    FINANCIAL_JOB_TYPES,
    FinancialJobDispatcher,
)
from intel_database import IntelRepository
from intel_worker import IntelWorker
from sqlite_database import SQLiteDatabase


ENABLED_SETTINGS = {
    "FINANCIAL_INTELLIGENCE_ENABLED": True,
    "TRADING_AGENTS_ENABLED": True,
    "FINANCIAL_AUTO_RESEARCH_ENABLED": True,
    "TRADING_SIMULATION_ENABLED": True,
    # 生产 .env 默认 FINANCIAL_ROLLOUT_STAGE=off（fail-closed），会先于这些
    # 开关把任务判成 rollout_stage_*_not_reached。本文件验证的是"开关打开后
    # 作业队列（优先级/租约/重试/心跳/取消）的既有行为"，因此把灰度阶段显式
    # 放到最高级。
    "FINANCIAL_ROLLOUT_STAGE": "simulation_backtest",
}


class _RetryableFixtureError(RuntimeError):
    retryable = True
    error_code = "fixture_retryable"


class FinancialWorkerJobTest(unittest.TestCase):
    def setUp(self):
        # IntelWorker.__init__ 在 config.FINANCIAL_INTELLIGENCE_ENABLED /
        # TRADING_AGENTS_ENABLED 都为假时会直接丢弃显式传入的
        # financial_dispatcher（生产上的启动优化），本机 .env 默认就是全关，
        # 于是 5 个金融 handler 全都注册不上。这里只把"是否初始化金融模块"
        # 打开；真正的开关判定仍由各用例传给 dispatcher 的 settings 决定。
        self._worker_flags = [
            patch.object(config, "FINANCIAL_INTELLIGENCE_ENABLED", True),
            patch.object(config, "TRADING_AGENTS_ENABLED", True),
            patch.object(config, "TRADING_SIMULATION_ENABLED", True),
            # conftest 的 DATABASE_TYPE=sqlite 会被 .env 覆盖（config 里仍是
            # postgres），SQLiteDatabase(path) 只改路径不改后端，作业行会真的
            # 写进共享主库 intel_jobs。
            patch.object(config, "DATABASE_TYPE", "sqlite"),
            patch("db_connection.database_type", lambda: "sqlite"),
        ]
        for item in self._worker_flags:
            item.start()
            self.addCleanup(item.stop)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(
            str(Path(self.temp_dir.name) / "financial-worker.sqlite3")
        )
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.repository = IntelRepository(self.database)

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _worker(self, runners=None, *, worker_id="financial-worker", heartbeat=0.02):
        dispatcher = FinancialJobDispatcher(
            runners or {}, settings=ENABLED_SETTINGS
        )
        worker = IntelWorker(
            repository=self.repository,
            worker_id=worker_id,
            financial_dispatcher=dispatcher,
            job_lease_seconds=30,
            heartbeat_seconds=heartbeat,
        )
        worker.enqueue_due_periodic_jobs = lambda: None
        return worker

    @staticmethod
    def _success_runner(payload, context):
        context.raise_if_cancelled()
        return {
            "status": "completed",
            "marker": payload.get("marker", context.job_type),
        }

    def test_all_five_handlers_execute_on_existing_worker_and_dedupe(self):
        runners = {job_type: self._success_runner for job_type in FINANCIAL_JOB_TYPES}
        worker = self._worker(runners)
        self.assertTrue(set(FINANCIAL_JOB_TYPES) <= set(worker.handlers))
        job_ids = []
        for job_type in FINANCIAL_JOB_TYPES:
            job_id, created = self.repository.enqueue_job(
                job_type,
                f"financial-job:{job_type}:fixture",
                {"marker": job_type},
            )
            duplicate_id, duplicate_created = self.repository.enqueue_job(
                job_type,
                f"financial-job:{job_type}:fixture",
                {"marker": "duplicate"},
            )
            self.assertTrue(created)
            self.assertFalse(duplicate_created)
            self.assertEqual(duplicate_id, job_id)
            job_ids.append(job_id)

        stats = worker.run_once(job_types=FINANCIAL_JOB_TYPES, limit=10)
        self.assertEqual(stats["completed"], 5)
        for job_id in job_ids:
            job = self.repository.get_job(job_id)
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["result"]["status"], "completed")

    def test_disabled_capability_completes_as_explicit_skip(self):
        dispatcher = FinancialJobDispatcher({}, settings={})
        worker = IntelWorker(
            repository=self.repository,
            worker_id="disabled-financial-worker",
            financial_dispatcher=dispatcher,
            heartbeat_seconds=0.02,
        )
        worker.enqueue_due_periodic_jobs = lambda: None
        job_id, _ = self.repository.enqueue_job(
            "financial_research", "financial-disabled", {}
        )
        stats = worker.run_once(job_types=["financial_research"], limit=1)
        self.assertEqual(stats["completed"], 1)
        result = self.repository.get_job(job_id)["result"]
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "financial_intelligence_disabled")

    def test_enabled_but_missing_runner_fails_closed_without_retry_loop(self):
        worker = self._worker({})
        job_id, _ = self.repository.enqueue_job(
            "financial_verify", "financial-missing-runner", {}, max_attempts=5
        )
        stats = worker.run_once(job_types=["financial_verify"], limit=1)
        self.assertEqual(stats["failed"], 1)
        job = self.repository.get_job(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["attempt_count"], 1)
        self.assertIn("financial_job_runner_unavailable", job["last_error"])

    def test_retry_limit_and_poison_job_isolation_preserve_other_work(self):
        def poison(_payload, _context):
            raise _RetryableFixtureError("private endpoint detail")

        worker = self._worker(
            {
                "financial_research": poison,
                "financial_snapshot": self._success_runner,
            }
        )
        poison_id, _ = self.repository.enqueue_job(
            "financial_research",
            "financial-poison",
            {},
            priority=100,
            max_attempts=1,
        )
        good_id, _ = self.repository.enqueue_job(
            "financial_snapshot", "financial-good-after-poison", {}, priority=0
        )
        stats = worker.run_once(
            job_types=["financial_research", "financial_snapshot"], limit=10
        )
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["completed"], 1)
        self.assertEqual(self.repository.get_job(poison_id)["status"], "failed")
        self.assertEqual(self.repository.get_job(good_id)["status"], "completed")

        retry_worker = self._worker({"financial_research": poison})
        retry_id, _ = self.repository.enqueue_job(
            "financial_research", "financial-retry-limit", {}, max_attempts=2
        )
        first = retry_worker.run_once(job_types=["financial_research"], limit=1)
        self.assertEqual(first["retry_wait"], 1)
        self.database.connection.execute(
            "UPDATE intel_jobs SET next_retry_at='2000-01-01T00:00:00Z' WHERE id=?",
            (retry_id,),
        )
        second = retry_worker.run_once(job_types=["financial_research"], limit=1)
        self.assertEqual(second["failed"], 1)
        self.assertEqual(self.repository.get_job(retry_id)["attempt_count"], 2)

    def test_heartbeat_and_cancellation_propagate_to_running_job(self):
        started = threading.Event()
        heartbeat_count = {"value": 0}
        original_renew = self.repository.renew_job_lease

        def counted_renew(*args, **kwargs):
            heartbeat_count["value"] += 1
            return original_renew(*args, **kwargs)

        self.repository.renew_job_lease = counted_renew

        def cancellable(_payload, context):
            started.set()
            while True:
                context.wait(0.01)

        worker = self._worker({"financial_research": cancellable}, heartbeat=0.01)
        job_id, _ = self.repository.enqueue_job(
            "financial_research", "financial-cancel", {}
        )
        result_holder = {}

        def run():
            result_holder.update(
                worker.run_once(job_types=["financial_research"], limit=1)
            )

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(started.wait(2))
        deadline = time.monotonic() + 2
        while heartbeat_count["value"] == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreater(heartbeat_count["value"], 0)
        self.assertTrue(self.repository.cancel_job(job_id, reason="user_cancelled"))
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result_holder["cancelled"], 1)
        self.assertEqual(self.repository.get_job(job_id)["status"], "cancelled")

    def test_waiting_jobs_in_claimed_batch_are_heartbeated_during_long_research(self):
        started = threading.Event()
        release = threading.Event()

        def long_research(_payload, context):
            started.set()
            while not release.wait(0.01):
                context.raise_if_cancelled()
            return {"status": "completed"}

        worker = self._worker(
            {
                "financial_research": long_research,
                "financial_snapshot": self._success_runner,
            },
            heartbeat=0.01,
        )
        self.repository.enqueue_job(
            "financial_research", "financial-long-first", {}, priority=100
        )
        waiting_id, _ = self.repository.enqueue_job(
            "financial_snapshot", "financial-waiting-second", {}, priority=0
        )
        result_holder = {}
        thread = threading.Thread(
            target=lambda: result_holder.update(
                worker.run_once(
                    job_types=["financial_research", "financial_snapshot"], limit=2
                )
            )
        )
        thread.start()
        try:
            self.assertTrue(started.wait(2))
            waiting = self.repository.get_job(waiting_id)
            self.assertEqual(waiting["status"], "running")
            self.database.connection.execute(
                "UPDATE intel_jobs SET lease_expires_at='2000-01-01T00:00:00Z' WHERE id=?",
                (waiting_id,),
            )
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if self.repository.get_job(waiting_id)["lease_expires_at"] != "2000-01-01T00:00:00Z":
                    break
                time.sleep(0.01)
            self.assertNotEqual(
                self.repository.get_job(waiting_id)["lease_expires_at"],
                "2000-01-01T00:00:00Z",
            )
            self.assertEqual(
                self.repository.claim_jobs(
                    "competing-worker", job_types=["financial_snapshot"], limit=1
                ),
                [],
            )
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result_holder["completed"], 2)

    def test_expired_lease_is_recovered_and_stale_owner_cannot_complete(self):
        job_id, _ = self.repository.enqueue_job(
            "financial_snapshot", "financial-lease-recovery", {}
        )
        first = self.repository.claim_jobs(
            "worker-a",
            job_types=["financial_snapshot"],
            limit=1,
            lease_seconds=30,
        )
        self.assertEqual([item["id"] for item in first], [job_id])
        self.database.connection.execute(
            "UPDATE intel_jobs SET lease_expires_at='2000-01-01T00:00:00Z' WHERE id=?",
            (job_id,),
        )
        second = self.repository.claim_jobs(
            "worker-b", job_types=["financial_snapshot"], limit=1
        )
        self.assertEqual([item["id"] for item in second], [job_id])
        self.assertFalse(
            self.repository.complete_job(
                job_id, {"stale": True}, lease_owner="worker-a"
            )
        )
        self.assertTrue(
            self.repository.complete_job(
                job_id, {"owner": "worker-b"}, lease_owner="worker-b"
            )
        )
        self.assertEqual(self.repository.get_job(job_id)["result"]["owner"], "worker-b")

    def test_failed_research_does_not_block_existing_rss_job_type(self):
        def fail_once(_payload, _context):
            raise _RetryableFixtureError("fixture")

        worker = self._worker({"financial_research": fail_once})
        worker.register_handler(
            "classification", lambda payload: {"article_id": payload["article_id"]}
        )
        self.repository.enqueue_job(
            "financial_research",
            "financial-failure-before-rss",
            {},
            priority=100,
            max_attempts=1,
        )
        rss_id, _ = self.repository.enqueue_job(
            "classification",
            "rss-after-financial-failure",
            {"article_id": 7},
            priority=0,
        )
        stats = worker.run_once(
            job_types=["financial_research", "classification"], limit=10
        )
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["completed"], 1)
        self.assertEqual(self.repository.get_job(rss_id)["status"], "completed")


if __name__ == "__main__":
    unittest.main()

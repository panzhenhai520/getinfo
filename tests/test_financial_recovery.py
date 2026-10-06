import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import financial_schema
from financial_recovery import (
    FINANCIAL_RECOVERY_VERSION,
    RECOVERY_FAULT_POLICIES,
    RECOVERY_RTO_TARGETS_SECONDS,
    FinancialRecoveryError,
    assert_database_restore_allowed,
    database_fingerprint,
    recovery_decision,
    verify_legal_data_preserved,
)
from financial_rollout import rollout_transition_decision
from financial_schema import ensure_financial_tables
from financial_worker_jobs import FinancialJobDispatcher
from intel_database import IntelRepository
from intel_worker import IntelWorker
from sqlite_database import SQLiteDatabase


ENABLED_SETTINGS = {
    "FINANCIAL_INTELLIGENCE_ENABLED": True,
    "TRADING_AGENTS_ENABLED": True,
    "FINANCIAL_AUTO_RESEARCH_ENABLED": True,
    "TRADING_SIMULATION_ENABLED": True,
    "FINANCIAL_ROLLOUT_STAGE": "simulation_backtest",
}


class FinancialProductionRecoveryTest(unittest.TestCase):
    def setUp(self):
        # IntelWorker.__init__ 在 config 的金融开关全关时会直接丢弃显式传入的
        # financial_dispatcher（生产启动优化），本机 .env 默认如此，
        # worker 侧就完全没有金融 handler。这里只打开"是否初始化金融模块"。
        for item in (
            patch.object(config, "FINANCIAL_INTELLIGENCE_ENABLED", True),
            patch.object(config, "TRADING_AGENTS_ENABLED", True),
            patch.object(config, "TRADING_SIMULATION_ENABLED", True),
            # conftest 的 DATABASE_TYPE=sqlite 会被 .env 覆盖（config 里仍是
            # postgres），SQLiteDatabase(path) 只改路径不改后端，本文件的作业
            # 恢复用例会真的写进共享主库 intel_jobs。
            patch.object(config, "DATABASE_TYPE", "sqlite"),
            patch("db_connection.database_type", lambda: "sqlite"),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "recovery.sqlite3"
        self.database = SQLiteDatabase(str(self.database_path))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.connection = self.database.connection
        self.connection.execute(
            """
            INSERT INTO financial_research_runs(
                id, trigger_type, scope_type, status, requested_at
            ) VALUES(
                'legal-post-baseline-run', 'user', 'market', 'completed',
                '2026-08-03T00:00:00.000Z'
            )
            """
        )

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _fingerprint(self):
        return database_fingerprint(
            self.connection,
            protected_tables=("financial_research_runs", "financial_final_reports"),
        )

    def test_provider_and_llm_faults_roll_back_flags_without_database_restore(self):
        scenarios = (
            ("provider_failure", "rss"),
            ("llm_failure", "snapshot_readonly"),
        )
        for fault_type, target_stage in scenarios:
            with self.subTest(fault_type=fault_type):
                before = self._fingerprint()
                started = time.perf_counter()
                policy = recovery_decision(
                    fault_type, database_integrity_ok=before["integrity_ok"]
                )
                rollout = rollout_transition_decision(
                    "simulation_backtest", target_stage
                )
                elapsed = time.perf_counter() - started
                after = self._fingerprint()

                self.assertEqual(policy["reason"], "non_destructive_recovery")
                self.assertFalse(policy["database_restore_allowed"])
                self.assertEqual(policy["rpo"], "zero_database_rows_lost")
                self.assertTrue(rollout["allowed"])
                self.assertEqual(rollout["action"], "rollback")
                self.assertFalse(rollout["rollback_requires_database"])
                self.assertLess(
                    elapsed,
                    RECOVERY_RTO_TARGETS_SECONDS["feature_flag_rollback"],
                )
                self.assertTrue(verify_legal_data_preserved(before, after)["passed"])

    def test_healthy_database_cannot_be_overwritten_even_with_approval_flag(self):
        before = self._fingerprint()
        decision = recovery_decision(
            "database_corruption",
            database_integrity_ok=True,
            database_restore_approved=True,
        )
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], "healthy_database_restore_forbidden")
        with self.assertRaises(FinancialRecoveryError) as raised:
            assert_database_restore_allowed(
                database_integrity_ok=True, database_restore_approved=True
            )
        self.assertEqual(
            raised.exception.error_code, "healthy_database_restore_forbidden"
        )
        self.assertTrue(
            verify_legal_data_preserved(before, self._fingerprint())["passed"]
        )

    def test_corrupt_database_restore_requires_explicit_approval(self):
        held = recovery_decision(
            "database_corruption",
            database_integrity_ok=False,
            database_restore_approved=False,
        )
        approved = recovery_decision(
            "database_corruption",
            database_integrity_ok=False,
            database_restore_approved=True,
        )
        self.assertFalse(held["database_restore_allowed"])
        self.assertEqual(
            held["reason"], "database_restore_requires_explicit_approval"
        )
        self.assertTrue(approved["database_restore_allowed"])
        self.assertEqual(approved["rpo"], "approved_backup_capture_time")

    def test_interrupted_schema_migration_resumes_without_losing_legal_rows(self):
        interrupted_path = Path(self.temp_dir.name) / "interrupted.sqlite3"
        connection = sqlite3.connect(interrupted_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE articles(
                id INTEGER PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL
            );
            CREATE TABLE intel_jobs(id INTEGER PRIMARY KEY);
            INSERT INTO articles VALUES(
                91, 'https://example.test/legal', '基线后合法用户资料'
            );
            """
        )
        before = database_fingerprint(connection, protected_tables=("articles",))
        original_indexes = financial_schema.FINANCIAL_INDEX_DDL
        financial_schema.FINANCIAL_INDEX_DDL = original_indexes + (
            "INVALID STAGE 6.6 MIGRATION",
        )
        started = time.perf_counter()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                ensure_financial_tables(connection.cursor())
        finally:
            financial_schema.FINANCIAL_INDEX_DDL = original_indexes
        interrupted = database_fingerprint(connection, protected_tables=("articles",))
        self.assertTrue(verify_legal_data_preserved(before, interrupted)["passed"])

        ensure_financial_tables(connection.cursor())
        elapsed = time.perf_counter() - started
        recovered = database_fingerprint(connection, protected_tables=("articles",))
        old_reader = connection.execute(
            "SELECT id, url, title FROM articles WHERE id=91"
        ).fetchone()
        self.assertEqual(
            tuple(old_reader),
            (91, "https://example.test/legal", "基线后合法用户资料"),
        )
        self.assertTrue(verify_legal_data_preserved(before, recovered)["passed"])
        self.assertLess(elapsed, RECOVERY_RTO_TARGETS_SECONDS["schema_resume"])
        connection.close()

    def test_worker_stop_retries_persistent_job_and_new_worker_completes_it(self):
        repository = IntelRepository(self.database)
        started = threading.Event()

        def interrupted_runner(_payload, context):
            started.set()
            while True:
                context.wait(0.01)

        first = IntelWorker(
            repository=repository,
            worker_id="recovery-worker-a",
            financial_dispatcher=FinancialJobDispatcher(
                {"financial_research": interrupted_runner},
                settings=ENABLED_SETTINGS,
            ),
            heartbeat_seconds=0.01,
        )
        first.enqueue_due_periodic_jobs = lambda: None
        job_id, created = repository.enqueue_job(
            "financial_research",
            "stage-6.6-worker-restart",
            {"scope_type": "instrument", "marker": "preserved"},
            max_attempts=3,
        )
        self.assertTrue(created)
        result = {}
        started_at = time.perf_counter()
        thread = threading.Thread(
            target=lambda: result.update(
                first.run_once(job_types=["financial_research"], limit=1)
            )
        )
        thread.start()
        self.assertTrue(started.wait(2))
        first.request_stop()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["retry_wait"], 1)
        self.assertEqual(repository.get_job(job_id)["status"], "retry_wait")

        self.connection.execute(
            "UPDATE intel_jobs SET next_retry_at='2000-01-01T00:00:00Z' WHERE id=?",
            (job_id,),
        )

        def completed_runner(payload, context):
            context.raise_if_cancelled()
            return {"status": "completed", "marker": payload["marker"]}

        second = IntelWorker(
            repository=repository,
            worker_id="recovery-worker-b",
            financial_dispatcher=FinancialJobDispatcher(
                {"financial_research": completed_runner}, settings=ENABLED_SETTINGS
            ),
            heartbeat_seconds=0.01,
        )
        second.enqueue_due_periodic_jobs = lambda: None
        completed = second.run_once(job_types=["financial_research"], limit=1)
        elapsed = time.perf_counter() - started_at
        job = repository.get_job(job_id)
        self.assertEqual(completed["completed"], 1)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["attempt_count"], 2)
        self.assertEqual(job["result"]["marker"], "preserved")
        self.assertLess(elapsed, RECOVERY_RTO_TARGETS_SECONDS["worker_restart"])

    def test_recovery_contract_covers_all_required_faults_and_targets(self):
        self.assertEqual(FINANCIAL_RECOVERY_VERSION, "financial-recovery-v1")
        self.assertTrue(
            {
                "provider_failure",
                "llm_failure",
                "schema_interruption",
                "report_corruption",
                "worker_failure",
                "database_corruption",
            }
            <= set(RECOVERY_FAULT_POLICIES)
        )
        self.assertTrue(all(value > 0 for value in RECOVERY_RTO_TARGETS_SECONDS.values()))


if __name__ == "__main__":
    unittest.main()

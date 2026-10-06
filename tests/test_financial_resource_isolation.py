import tempfile
import threading
import time
import unittest
import sqlite3
from pathlib import Path
from unittest.mock import patch

import config
config.DATABASE_TYPE = "sqlite"  # noqa: E402

# 本机 .env 是 DATABASE_TYPE=postgres 且指向共享主库，而 SQLiteDatabase(path) 是按
# config.DATABASE_TYPE 选后端的（传路径并不会改后端）。不隔离时这个文件会真的连主库：
#   * 用例里的 enqueue_job/claim_jobs 写进主库 intel_jobs（实测留下 lane-long /
#     lane-rss / old-rss 三条测试作业，created_at 2026-10-04）；
#   * sqlite_database 的全局单例在导入期就按 postgres 建连，直跑时打印
#     「PostgreSQL主库连接成功: 127.0.0.1:5432/collectinfo」。
# 本类验证的本来就是 SQLite 的锁/WAL/队列语义，所以在导入期（任何业务模块被导入之前）
# 就强制切 sqlite。pytest 下 tests/conftest.py 已经强制过，这一行是对 unittest 直跑
# （python tests/test_financial_resource_isolation.py）的兜底，与仓库里其它用例
# （test_article_noise_clean.py / test_tts_master_switch.py 等）的做法一致。
from financial_artifacts import FinancialArtifactStore, FinancialPersistenceError
from financial_provider_contract import RateLimitedError
from financial_provider_router import FinancialProviderRouter
from financial_resource_isolation import (
    ALL_ISOLATED_WORKER_JOB_TYPES,
    ArtifactIOController,
    ArtifactIOTimeout,
    CORE_WORKER_JOB_TYPES,
    LONG_FINANCIAL_JOB_TYPES,
    QA_WORKER_JOB_TYPES,
    ProviderAdmissionController,
    ProviderCooldown,
    validate_worker_lane_partition,
)
from financial_worker_jobs import FinancialJobDispatcher
from intel_database import IntelRepository
from intel_worker import IntelWorker
from intel_worker_supervisor import worker_lane_commands
from sqlite_database import SQLiteDatabase
from tests.test_financial_provider_router import DummyProvider, NOW
from financial_provider_contract import FinancialDataKind, FinancialDataRequest


ENABLED_SETTINGS = {
    "FINANCIAL_INTELLIGENCE_ENABLED": True,
    "TRADING_AGENTS_ENABLED": True,
    "TRADING_SIMULATION_ENABLED": True,
    "YAHOO_FINANCE_ENABLED": True,
    "FINANCIAL_PROVIDER_ADMISSION_TIMEOUT_SECONDS": 1,
}


class FinancialResourceIsolationTest(unittest.TestCase):
    def test_worker_lane_partition_is_complete_disjoint_and_command_enforced(self):
        partition = validate_worker_lane_partition(ALL_ISOLATED_WORKER_JOB_TYPES)
        self.assertTrue(partition["valid"])
        self.assertFalse(set(CORE_WORKER_JOB_TYPES) & set(LONG_FINANCIAL_JOB_TYPES))
        self.assertFalse(set(QA_WORKER_JOB_TYPES) & set(CORE_WORKER_JOB_TYPES))
        self.assertFalse(set(QA_WORKER_JOB_TYPES) & set(LONG_FINANCIAL_JOB_TYPES))
        commands = worker_lane_commands(
            python_executable="python-fixture",
            worker_script=Path("/tmp/intel_worker.py"),
        )
        self.assertNotIn("--no-periodic-scheduler", commands["core"])
        self.assertIn("--no-periodic-scheduler", commands["long_financial"])
        for job_type in CORE_WORKER_JOB_TYPES:
            self.assertIn(job_type, commands["core"])
            self.assertNotIn(job_type, commands["long_financial"])
        for job_type in LONG_FINANCIAL_JOB_TYPES:
            self.assertIn(job_type, commands["long_financial"])
            self.assertNotIn(job_type, commands["core"])
        for job_type in QA_WORKER_JOB_TYPES:
            self.assertNotIn(job_type, commands["core"])
            self.assertNotIn(job_type, commands["long_financial"])

    def test_slow_research_lane_does_not_block_rss_lane(self):
        temp_dir = tempfile.TemporaryDirectory()
        path = str(Path(temp_dir.name) / "lane.sqlite3")
        core_db = SQLiteDatabase(path)
        long_db = SQLiteDatabase(path)
        self.assertTrue(core_db.connect())
        self.assertTrue(core_db.create_tables())
        self.assertTrue(long_db.connect())
        release = threading.Event()
        research_started = threading.Event()

        def long_research(_payload, context):
            research_started.set()
            while not release.wait(0.01):
                context.raise_if_cancelled()
            return {"status": "completed"}

        # IntelWorker.__init__ 用全局 config 的 FINANCIAL_INTELLIGENCE_ENABLED /
        # TRADING_AGENTS_ENABLED 决定要不要挂上「注入的」financial_dispatcher：
        # 本机 .env 两项都是 false，注入的 dispatcher 会被丢掉，长任务 lane 领不到
        # financial_research（research_started 永远不置位）。构造期显式打开。
        with patch.object(config, "FINANCIAL_INTELLIGENCE_ENABLED", True):
            long_worker = IntelWorker(
                repository=IntelRepository(long_db),
                worker_id="long-lane",
                financial_dispatcher=FinancialJobDispatcher(
                    {"financial_research": long_research}, settings=ENABLED_SETTINGS
                ),
                heartbeat_seconds=0.02,
            )
            core_worker = IntelWorker(
                repository=IntelRepository(core_db),
                worker_id="core-lane",
                financial_dispatcher=FinancialJobDispatcher(
                    {}, settings=ENABLED_SETTINGS
                ),
                heartbeat_seconds=0.02,
            )
        core_worker.register_handler(
            "classification", lambda payload: {"article_id": payload["article_id"]}
        )
        long_worker.enqueue_due_periodic_jobs = lambda: None
        core_worker.enqueue_due_periodic_jobs = lambda: None
        long_worker.repository.enqueue_job(
            "financial_research", "lane-long", {}, priority=100
        )
        rss_id, _ = core_worker.repository.enqueue_job(
            "classification", "lane-rss", {"article_id": 9}, priority=-20
        )
        long_result = {}
        thread = threading.Thread(
            target=lambda: long_result.update(
                long_worker.run_once(job_types=LONG_FINANCIAL_JOB_TYPES, limit=1)
            )
        )
        thread.start()
        try:
            self.assertTrue(research_started.wait(2))
            started = time.monotonic()
            core_result = core_worker.run_once(job_types=CORE_WORKER_JOB_TYPES, limit=1)
            elapsed = time.monotonic() - started
            self.assertEqual(core_result["completed"], 1)
            self.assertEqual(core_worker.repository.get_job(rss_id)["status"], "completed")
            self.assertLess(elapsed, 0.5)
            self.assertTrue(thread.is_alive())
        finally:
            release.set()
            thread.join(2)
            long_db.disconnect()
            core_db.disconnect()
            temp_dir.cleanup()
        self.assertEqual(long_result["completed"], 1)

    def test_queue_priority_aging_prevents_old_rss_starvation(self):
        """等待积分（aging）能把久等的低优先级分类作业顶到新到的高优先级作业前面。

        ⚠️ 优先级差值必须落在等待积分上限 INTEL_JOB_PRIORITY_AGING_CAP 之内：
        该上限是 2026-10-06「优先级语义收口」刻意引入的（config.py 注释 + 
        tests/test_job_priority_semantics.py::test_aging_cap_alone_cannot_prevent_starvation），
        目的就是让「新鲜的高优先级作业」赢过「等得再久的低优先级作业」；
        差值超过上限的场景不再由 aging 兜底，而是由 worker 的饥饿保留名额
        （claim_jobs(starved_before=...)）负责。原夹具用 -20 vs 100（差值 120 > 上限 90），
        无论等多久都不可能翻盘，属于上限引入前的旧语义。
        这里保持断言不变，改用差值 70（-20 vs 50）：不 aging 就是新作业先领，
        aging 生效后老作业以 70 反超 50。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            database = SQLiteDatabase(str(Path(temp_dir) / "aging.sqlite3"))
            try:
                self.assertTrue(database.connect())
                self.assertTrue(database.create_tables())
                repository = IntelRepository(database)
                old_id, _ = repository.enqueue_job(
                    "classification", "old-rss", {"article_id": 1}, priority=-20
                )
                new_id, _ = repository.enqueue_job(
                    "classification", "new-high", {"article_id": 2}, priority=50
                )
                database.connection.execute(
                    "UPDATE intel_jobs SET created_at='2026-01-01T00:00:00Z' WHERE id=?",
                    (old_id,),
                )
                claimed = repository.claim_jobs(
                    "aging-worker", job_types=["classification"], limit=1
                )
                self.assertEqual([item["id"] for item in claimed], [old_id])
                self.assertNotEqual(old_id, new_id)
            finally:
                # Windows 上临时目录清理需要先关连接，否则断言失败时会叠加
                # PermissionError/NotADirectoryError，掩盖真正的失败原因。
                database.disconnect()

    def test_provider_slots_are_bounded_per_source_and_rate_limits_cool_down(self):
        controller = ProviderAdmissionController(2, 1)
        first_started = threading.Event()
        release = threading.Event()
        second_entered = threading.Event()

        def first():
            with controller.slot("yahoo", timeout_seconds=2):
                first_started.set()
                release.wait(2)

        def second():
            with controller.slot("yahoo", timeout_seconds=2):
                second_entered.set()

        first_thread = threading.Thread(target=first)
        second_thread = threading.Thread(target=second)
        first_thread.start()
        self.assertTrue(first_started.wait(1))
        second_thread.start()
        time.sleep(0.05)
        self.assertFalse(second_entered.is_set())
        with controller.slot("fred", timeout_seconds=1):
            self.assertEqual(controller.snapshot()["active_total"], 2)
        release.set()
        first_thread.join(2)
        second_thread.join(2)
        self.assertTrue(second_entered.is_set())
        self.assertEqual(controller.snapshot()["active_total"], 0)

        controller.record_rate_limit("yahoo", 3)
        with self.assertRaises(ProviderCooldown) as caught:
            with controller.slot("yahoo", timeout_seconds=1):
                self.fail("cooldown must fail before provider execution")
        self.assertGreaterEqual(caught.exception.retry_after_seconds, 1)

    def test_router_rate_limit_is_shared_and_second_call_never_hits_provider(self):
        class LimitedProvider(DummyProvider):
            def __init__(self):
                super().__init__("yahoo")
                self.calls = 0

            def fetch(self, request):
                self.calls += 1
                raise RateLimitedError(
                    "fixture limit",
                    provider_id="yahoo",
                    endpoint=request.endpoint,
                    request_id=request.request_id,
                    retry_after_seconds=5,
                )

        controller = ProviderAdmissionController(2, 1)
        provider = LimitedProvider()
        router = FinancialProviderRouter(
            settings=ENABLED_SETTINGS,
            factories={"yahoo": lambda: provider},
            admission_controller=controller,
        )
        request = FinancialDataRequest(
            request_id="limit-fixture",
            endpoint="quote",
            instrument_id="1",
            metric="last_price",
            data_kind=FinancialDataKind.QUOTE,
            requested_as_of=NOW,
            preferred_provider_id="yahoo",
        )
        for _ in range(2):
            with self.assertRaises(RateLimitedError):
                router.fetch(
                    request,
                    candidate_provider_ids=("yahoo",),
                    allow_fallback=False,
                )
        self.assertEqual(provider.calls, 1)

    def test_sqlite_lock_wait_is_bounded_and_wal_reads_remain_interactive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "contention.sqlite3")
            application = SQLiteDatabase(path)
            self.assertTrue(application.connect())
            self.assertTrue(application.create_tables())
            configured = int(
                application.connection.execute("PRAGMA busy_timeout").fetchone()[0]
            )
            self.assertEqual(configured, config.SQLITE_BUSY_TIMEOUT_MS)
            blocker = sqlite3.connect(path, isolation_level=None, timeout=0.1)
            blocker.execute("PRAGMA journal_mode=WAL")
            blocker.execute("BEGIN IMMEDIATE")
            blocker.execute(
                "INSERT INTO intel_runtime_settings(setting_key, setting_value) VALUES('lock-fixture','1')"
            )
            started = time.monotonic()
            application.connection.execute("PRAGMA busy_timeout=100")
            row = application.connection.execute(
                "SELECT COUNT(*) FROM intel_runtime_settings"
            ).fetchone()
            self.assertGreaterEqual(int(row[0]), 0)
            self.assertLess(time.monotonic() - started, 0.5)
            write_started = time.monotonic()
            with self.assertRaises(sqlite3.OperationalError):
                application.connection.execute(
                    "INSERT INTO intel_runtime_settings(setting_key, setting_value) VALUES('blocked-fixture','1')"
                )
            self.assertLess(time.monotonic() - write_started, 0.5)
            blocker.execute("ROLLBACK")
            blocker.close()
            application.connection.execute(
                "INSERT INTO intel_runtime_settings(setting_key, setting_value) VALUES('recovered-fixture','1')"
            )
            application.disconnect()

    def test_artifact_io_is_size_bounded_serial_and_atomic(self):
        gate = ArtifactIOController(1)
        entered = threading.Event()
        release = threading.Event()

        def hold_slot():
            with gate.slot(timeout_seconds=1):
                entered.set()
                release.wait(1)

        thread = threading.Thread(target=hold_slot)
        thread.start()
        self.assertTrue(entered.wait(1))
        with self.assertRaises(ArtifactIOTimeout):
            with gate.slot(timeout_seconds=0.05):
                self.fail("second fsync-heavy writer must not enter")
        release.set()
        thread.join(1)
        self.assertEqual(gate.snapshot()["active"], 0)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "report.md"
            with patch.object(config, "FINANCIAL_ARTIFACT_MAX_BYTES", 1024):
                with self.assertRaises(FinancialPersistenceError) as caught:
                    FinancialArtifactStore._atomic_write(path, b"x" * 1025)
                self.assertEqual(caught.exception.error_code, "artifact_too_large")
                FinancialArtifactStore._atomic_write(path, b"accepted")
            self.assertEqual(path.read_bytes(), b"accepted")
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()

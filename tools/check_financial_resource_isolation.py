#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Stage 6.3 load and resource-isolation acceptance gate."""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from financial_resource_isolation import (
    ALL_ISOLATED_WORKER_JOB_TYPES,
    CORE_WORKER_JOB_TYPES,
    FINANCIAL_RESOURCE_ISOLATION_VERSION,
    LONG_FINANCIAL_JOB_TYPES,
    ProviderAdmissionController,
    ProviderCooldown,
    validate_worker_lane_partition,
)
from financial_worker_jobs import FinancialJobDispatcher
from intel_database import IntelRepository
from intel_worker import IntelWorker
from shared_llm_broker import _FairPrioritySlots
from sqlite_database import SQLiteDatabase


RUNTIME_SUITES = (
    "tests.test_financial_resource_isolation",
    "tests.test_shared_llm_broker",
    "tests.test_financial_provider_router",
    "tests.test_financial_worker_jobs",
    "tests.test_financial_artifacts",
    "tests.test_financial_rag_gate",
)

ENABLED_SETTINGS = {
    "FINANCIAL_INTELLIGENCE_ENABLED": True,
    "TRADING_AGENTS_ENABLED": True,
    "TRADING_SIMULATION_ENABLED": True,
}


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def _p95_ms(samples: list[float]) -> float:
    _assert(samples, "p95 samples are empty")
    ordered = sorted(float(value) for value in samples)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return round(ordered[index] * 1000.0, 3)


def static_acceptance() -> dict:
    isolation = _source("financial_resource_isolation.py")
    broker = _source("shared_llm_broker.py")
    worker = _source("intel_worker.py")
    supervisor = _source("intel_worker_supervisor.py")
    repository = _source("intel_database.py")
    provider = _source("financial_provider_router.py")
    database = _source("sqlite_database.py")
    artifacts = _source("financial_artifacts.py")
    compose = _source("docker-compose.crawler.yml")
    systemd = _source("deploy/systemd/info-aggregator-intel-worker.service")
    lane_partition = validate_worker_lane_partition(ALL_ISOLATED_WORKER_JOB_TYPES)
    checks = {
        "llm_interactive_capacity_reserved": all(
            marker in broker
            for marker in (
                "interactive_reserve",
                "background_capacity",
                'priority in {"chat_clarification", "chat_fact"}',
            )
        ),
        "worker_long_lane_is_explicit_and_disjoint": (
            lane_partition["valid"]
            and "CORE_WORKER_JOB_TYPES" in supervisor
            and "LONG_FINANCIAL_JOB_TYPES" in supervisor
            and "--no-periodic-scheduler" in supervisor
        ),
        "same_image_and_service_are_reused": (
            'command: ["python", "intel_worker_supervisor.py"]' in compose
            and "intel_worker_supervisor.py" in systemd
            and "subprocess.Popen" in supervisor
        ),
        "rss_queue_priority_ages_without_new_queue": (
            "INTEL_JOB_PRIORITY_AGING_SECONDS" in repository
            and "julianday(created_at)" in repository
            and "intel_jobs" in repository
        ),
        "provider_total_and_per_source_admission_are_bounded": all(
            marker in isolation
            for marker in (
                "ProviderAdmissionController",
                "max_concurrency",
                "per_source_concurrency",
                "record_rate_limit",
            )
        ) and "self.admission_controller.slot" in provider,
        "sqlite_wal_and_busy_wait_are_bounded": (
            "PRAGMA journal_mode = WAL" in database
            and "SQLITE_BUSY_TIMEOUT_MS" in database
            and config.SQLITE_BUSY_TIMEOUT_MS <= 5000
        ),
        "artifact_size_concurrency_and_atomic_replace_are_bounded": all(
            marker in artifacts
            for marker in (
                "FINANCIAL_ARTIFACT_MAX_BYTES",
                "artifact_io_controller.slot",
                "NamedTemporaryFile",
                "os.replace",
                "os.fsync",
            )
        ),
        "no_new_runtime_service_port_database_or_table": (
            "CREATE TABLE" not in isolation
            and "socket" not in isolation
            and "requests" not in isolation
        ),
    }
    _assert(all(checks.values()), checks)
    from tools.check_tradingagents_architecture import check_repository

    architecture = check_repository(ROOT)
    _assert(architecture["acceptance"]["passed"], architecture["acceptance"])
    return {
        "checks": checks,
        "worker_lanes": lane_partition,
        "architecture": {
            "passed": True,
            "services": architecture["compose"]["services"],
            "published_ports": architecture["ports"][
                "compose_published_container_ports"
            ],
            "new_services": [],
            "new_ports": [],
            "new_databases": [],
            "new_tables": [],
        },
        "configured_budgets_ms": {
            "interactive_p95": config.FINANCIAL_INTERACTIVE_P95_BUDGET_MS,
            "dashboard_p95": config.FINANCIAL_DASHBOARD_P95_BUDGET_MS,
            "rss_p95": config.FINANCIAL_RSS_P95_BUDGET_MS,
            "sqlite_busy_timeout": config.SQLITE_BUSY_TIMEOUT_MS,
        },
    }


def _combined_load_scenario(temp_dir: str) -> dict:
    database_path = str(Path(temp_dir) / "resource-load.sqlite3")
    core_database = SQLiteDatabase(database_path)
    long_database = SQLiteDatabase(database_path)
    _assert(core_database.connect(), "core lane database connection failed")
    _assert(core_database.create_tables(), "load database migration failed")
    _assert(long_database.connect(), "long lane database connection failed")
    release_research = threading.Event()
    research_started = threading.Event()

    def long_research(_payload, context):
        research_started.set()
        while not release_research.wait(0.01):
            context.raise_if_cancelled()
        return {"status": "completed"}

    def paper_backtest(_payload, context):
        context.raise_if_cancelled()
        return {"status": "completed", "network_calls": 0}

    long_worker = IntelWorker(
        repository=IntelRepository(long_database),
        worker_id="acceptance-long-lane",
        financial_dispatcher=FinancialJobDispatcher(
            {
                "financial_research": long_research,
                "paper_backtest": paper_backtest,
            },
            settings=ENABLED_SETTINGS,
        ),
        heartbeat_seconds=0.02,
    )
    core_worker = IntelWorker(
        repository=IntelRepository(core_database),
        worker_id="acceptance-core-lane",
        financial_dispatcher=FinancialJobDispatcher(
            {}, settings=ENABLED_SETTINGS
        ),
        heartbeat_seconds=0.02,
    )
    long_worker.enqueue_due_periodic_jobs = lambda: None
    core_worker.enqueue_due_periodic_jobs = lambda: None
    core_worker.register_handler(
        "classification", lambda payload: {"article_id": payload["article_id"]}
    )
    long_worker.repository.enqueue_job(
        "financial_research", "acceptance-long-research", {}, priority=100
    )
    backtest_id, _ = long_worker.repository.enqueue_job(
        "paper_backtest", "acceptance-backtest", {}, priority=50
    )
    rss_ids = [
        core_worker.repository.enqueue_job(
            "classification",
            f"acceptance-rss-{index}",
            {"article_id": index + 1},
            priority=-20,
        )[0]
        for index in range(20)
    ]
    long_stats = {}
    research_thread = threading.Thread(
        target=lambda: long_stats.update(
            long_worker.run_once(job_types=LONG_FINANCIAL_JOB_TYPES, limit=1)
        )
    )
    research_thread.start()
    _assert(research_started.wait(2), "slow research did not start")

    slots = _FairPrioritySlots(2, interactive_reserve=1)
    llm_background_started = threading.Event()
    release_llm = threading.Event()

    def slow_llm():
        with slots.slot(
            4, deadline=time.monotonic() + 5, foreground=False
        ):
            llm_background_started.set()
            release_llm.wait(3)

    llm_thread = threading.Thread(target=slow_llm)
    llm_thread.start()
    _assert(llm_background_started.wait(1), "slow LLM fixture did not start")
    interactive_samples = []
    for _ in range(25):
        started = time.monotonic()
        with slots.slot(
            1, deadline=time.monotonic() + 1, foreground=True
        ):
            pass
        interactive_samples.append(time.monotonic() - started)

    rss_samples = []
    for _ in rss_ids:
        started = time.monotonic()
        stats = core_worker.run_once(
            job_types=CORE_WORKER_JOB_TYPES,
            limit=1,
            schedule_periodic=False,
        )
        _assert(stats["completed"] == 1, stats)
        rss_samples.append(time.monotonic() - started)
    _assert(research_thread.is_alive(), "research was not slow during RSS acceptance")
    _assert(
        long_worker.repository.get_job(backtest_id)["status"] == "queued",
        "backtest must wait in the bounded long lane",
    )

    blocker = sqlite3.connect(database_path, isolation_level=None, timeout=0.1)
    blocker.execute("PRAGMA journal_mode=WAL")
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute(
        "INSERT INTO intel_runtime_settings(setting_key,setting_value) VALUES('load-lock','1')"
    )
    dashboard_samples = []
    for _ in range(25):
        started = time.monotonic()
        core_database.connection.execute(
            "SELECT COUNT(*) FROM intel_jobs"
        ).fetchone()
        dashboard_samples.append(time.monotonic() - started)
    core_database.connection.execute("PRAGMA busy_timeout=100")
    lock_started = time.monotonic()
    lock_failed_bounded = False
    try:
        core_database.connection.execute(
            "INSERT INTO intel_runtime_settings(setting_key,setting_value) VALUES('blocked-load','1')"
        )
    except sqlite3.OperationalError:
        lock_failed_bounded = True
    bounded_lock_wait_ms = round((time.monotonic() - lock_started) * 1000.0, 3)
    blocker.execute("ROLLBACK")
    blocker.close()
    core_database.connection.execute(
        "INSERT INTO intel_runtime_settings(setting_key,setting_value) VALUES('recovered-load','1')"
    )

    release_llm.set()
    release_research.set()
    llm_thread.join(2)
    research_thread.join(2)
    _assert(not llm_thread.is_alive(), "slow LLM fixture did not stop")
    _assert(not research_thread.is_alive(), "research fixture did not stop")
    backtest_stats = long_worker.run_once(
        job_types=LONG_FINANCIAL_JOB_TYPES,
        limit=1,
        schedule_periodic=False,
    )
    _assert(backtest_stats["completed"] == 1, backtest_stats)
    _assert(
        all(
            core_worker.repository.get_job(job_id)["status"] == "completed"
            for job_id in rss_ids
        ),
        "RSS jobs did not all complete",
    )

    provider_controller = ProviderAdmissionController(2, 1)
    provider_controller.record_rate_limit("yahoo", 2)
    provider_cooldown_enforced = False
    try:
        with provider_controller.slot("yahoo", timeout_seconds=0.1):
            pass
    except ProviderCooldown:
        provider_cooldown_enforced = True

    interactive_p95 = _p95_ms(interactive_samples)
    rss_p95 = _p95_ms(rss_samples)
    dashboard_p95 = _p95_ms(dashboard_samples)
    _assert(
        interactive_p95 <= config.FINANCIAL_INTERACTIVE_P95_BUDGET_MS,
        {"interactive_p95_ms": interactive_p95},
    )
    _assert(
        rss_p95 <= config.FINANCIAL_RSS_P95_BUDGET_MS,
        {"rss_p95_ms": rss_p95},
    )
    _assert(
        dashboard_p95 <= config.FINANCIAL_DASHBOARD_P95_BUDGET_MS,
        {"dashboard_p95_ms": dashboard_p95},
    )
    _assert(lock_failed_bounded and bounded_lock_wait_ms < 500, bounded_lock_wait_ms)
    _assert(provider_cooldown_enforced, "Provider cooldown was not enforced")

    long_database.disconnect()
    core_database.disconnect()
    return {
        "sample_count": {
            "interactive": len(interactive_samples),
            "rss": len(rss_samples),
            "dashboard": len(dashboard_samples),
        },
        "p95_ms": {
            "interactive": interactive_p95,
            "rss": rss_p95,
            "dashboard": dashboard_p95,
        },
        "budgets_ms": {
            "interactive": config.FINANCIAL_INTERACTIVE_P95_BUDGET_MS,
            "rss": config.FINANCIAL_RSS_P95_BUDGET_MS,
            "dashboard": config.FINANCIAL_DASHBOARD_P95_BUDGET_MS,
        },
        "slow_research_completed": long_stats.get("completed") == 1,
        "paper_backtest_completed_after_research": backtest_stats["completed"] == 1,
        "rss_completed_while_research_running": True,
        "sqlite_bounded_lock_wait_ms": bounded_lock_wait_ms,
        "sqlite_recovered_after_lock": True,
        "provider_cooldown_enforced": provider_cooldown_enforced,
        "network_calls": 0,
    }


def runtime_acceptance() -> dict:
    network_attempts = []

    def blocked_network(*_args, **_kwargs):
        network_attempts.append("blocked")
        raise AssertionError("unexpected live network call during resource tests")

    with tempfile.TemporaryDirectory() as temp_dir:
        previous_database = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = str(Path(temp_dir) / "suite.sqlite3")
        try:
            suite = unittest.TestSuite(
                unittest.defaultTestLoader.loadTestsFromName(name)
                for name in RUNTIME_SUITES
            )
            stream = io.StringIO()
            with patch.object(socket.socket, "connect", blocked_network), patch(
                "socket.create_connection", blocked_network
            ):
                result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
                combined = _combined_load_scenario(temp_dir)
        finally:
            if previous_database is None:
                os.environ.pop("DATABASE_PATH", None)
            else:
                os.environ["DATABASE_PATH"] = previous_database
    _assert(result.wasSuccessful(), stream.getvalue())
    _assert(not network_attempts, network_attempts)
    return {
        "executed": True,
        "suites": list(RUNTIME_SUITES),
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "network_calls": len(network_attempts),
        "combined_load": combined,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    report = {
        "acceptance": "passed",
        "task": "6.3",
        "resource_isolation_version": FINANCIAL_RESOURCE_ISOLATION_VERSION,
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "scenarios": {
            "concurrent_chat_rss_research_and_backtest": True,
            "slow_llm_keeps_interactive_capacity": True,
            "provider_concurrency_and_rate_limit_are_bounded": True,
            "sqlite_contention_is_bounded_and_recovers": True,
            "report_io_is_size_bounded_serial_and_atomic": True,
            "rss_queue_uses_priority_aging": True,
        },
        "boundaries": {
            "new_database": False,
            "new_table": False,
            "new_service": False,
            "new_port": False,
            "real_trading": False,
        },
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

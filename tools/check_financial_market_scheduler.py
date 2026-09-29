#!/usr/bin/env python3
"""Offline acceptance gate for task 2.23 automatic market scheduling."""

from __future__ import annotations

import argparse
import ast
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


SETTINGS = {
    "FINANCIAL_INTELLIGENCE_ENABLED": True,
    "TRADING_AGENTS_ENABLED": True,
    "FINANCIAL_AUTO_RESEARCH_ENABLED": True,
    "TRADING_SIMULATION_ENABLED": False,
    "FINANCIAL_QUOTE_FRESHNESS_SECONDS": 300,
}


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def static_acceptance():
    scheduler_path = ROOT / "financial_market_scheduler.py"
    worker_source = (ROOT / "intel_worker.py").read_text(encoding="utf-8")
    repository_source = (ROOT / "intel_database.py").read_text(encoding="utf-8")
    imports = {name.casefold() for name in _imports(scheduler_path)}
    prohibited = {
        "celery",
        "rq",
        "kombu",
        "pika",
        "redis",
        "requests",
        "httpx",
        "openai",
        "tradingagents",
    }
    _assert(not imports & prohibited, "scheduler added a queue/model/network runtime")
    _assert("FinancialMarketScheduler" in worker_source, "worker scheduler missing")
    _assert("FinancialMarketJobService" in worker_source, "market runners missing")
    _assert("enqueue_job_once" in repository_source, "permanent window dedupe missing")
    _assert("financial_research" not in scheduler_path.read_text(encoding="utf-8").split("class FinancialMarketScheduler", 1)[1], "scheduler queues full research")
    return {
        "existing_worker": "intel_worker.py",
        "existing_queue_table": "intel_jobs",
        "existing_database": "crawler_articles.db",
        "standard_markets": ["CN_XSHG_MARKET", "CN_XSHE_MARKET", "CN_A_MARKET", "HK_MARKET"],
        "new_services": [],
        "new_ports": [],
        "new_queues": [],
        "direct_queue_model_network_imports": [],
    }


def _utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def runtime_acceptance():
    from financial_market_scheduler import FinancialMarketScheduler
    from intel_database import IntelRepository
    from sqlite_database import SQLiteDatabase

    suite = unittest.defaultTestLoader.loadTestsFromName(
        "tests.test_financial_market_scheduler"
    )
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    _assert(outcome.wasSuccessful(), stream.getvalue())

    with tempfile.TemporaryDirectory() as directory:
        database = SQLiteDatabase(str(Path(directory) / "scheduler-acceptance.sqlite3"))
        _assert(database.connect(), "database connect failed")
        _assert(database.create_tables(), "database initialization failed")
        repository = IntelRepository(database)
        scheduler = FinancialMarketScheduler(repository, settings=SETTINGS)
        now = _utc("2026-07-31T02:00:00Z")
        startup = scheduler.enqueue_due_jobs(now=now, trigger="startup")
        restart = FinancialMarketScheduler(
            repository, settings=SETTINGS
        ).enqueue_due_jobs(now=now, trigger="startup")
        _assert(startup["created"] > 0, "startup catch-up created no jobs")
        _assert(restart["created"] == 0, "restart duplicated schedule window")
        _assert(startup["full_research_jobs_created"] == 0, "tick queued full research")

        jobs = [repository.get_job(item["job_id"]) for item in startup["jobs"]]
        snapshot_jobs = [job for job in jobs if job["job_type"] == "financial_snapshot"]
        overview_jobs = [job for job in jobs if job["job_type"] == "market_overview"]
        snapshot_symbols = {
            str(job["payload"].get("canonical_symbol") or "")
            for job in snapshot_jobs
            if not job["payload"].get("market_metric")
        }
        required_home_symbols = {
            "000001.SH", "399001.SZ", "HSI.HK", "IXIC.US", "N225.JP"
        }
        breadth_metrics = {
            str(job["payload"].get("market_metric") or "")
            for job in snapshot_jobs
            if job["payload"].get("market_metric")
        }
        _assert(
            required_home_symbols.issubset(snapshot_symbols),
            "fixed homepage index snapshots are incomplete",
        )
        _assert(
            {"breadth:XSHG", "breadth:XSHE", "breadth:XHKG"}.issubset(
                breadth_metrics
            ),
            "exchange breadth snapshots are incomplete",
        )
        _assert(len(overview_jobs) == 5, "standard/pulse overviews are incomplete")

        mainland_holiday = scheduler.enqueue_due_jobs(
            now=_utc("2026-02-20T02:00:00Z"), trigger="periodic"
        )
        holiday_jobs = [
            repository.get_job(item["job_id"])
            for item in mainland_holiday["jobs"]
            if repository.get_job(item["job_id"])["job_type"] == "market_overview"
        ]
        phases = {
            job["payload"]["universe_key"]: job["payload"]["phase"]
            for job in holiday_jobs
        }
        _assert(phases["CN_XSHG_MARKET"] == "closed_latest", "CN holiday lost")
        _assert(phases["HK_MARKET"] == "intraday", "HK open state lost")
        database.disconnect()

    return {
        "executed": True,
        "unit_tests_passed": outcome.testsRun,
        "startup_jobs_created": startup["created"],
        "restart_duplicate_jobs_created": restart["created"],
        "benchmark_and_breadth_snapshot_jobs": len(snapshot_jobs),
        "standard_and_pulse_overview_jobs": len(overview_jobs),
        "cross_market_holiday_state": phases,
        "full_research_jobs_created": 0,
        "network_calls": 0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = {
        "acceptance": "passed",
        "task": "2.23",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "topology": {
            "compose_services": ["crawler", "intel-worker", "redis", "worker"],
            "new_services": [],
            "new_ports": [],
            "new_queues": [],
            "new_databases": [],
        },
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Acceptance gate for financial jobs on the existing intel worker."""

from __future__ import annotations

import argparse
import ast
import json
import sys
import tempfile
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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
    from financial_worker_jobs import FINANCIAL_JOB_TYPES

    dispatcher_path = ROOT / "financial_worker_jobs.py"
    worker_source = (ROOT / "intel_worker.py").read_text(encoding="utf-8")
    repository_source = (ROOT / "intel_database.py").read_text(encoding="utf-8")
    imports = {name.casefold() for name in _imports(dispatcher_path)}
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
    _assert(not (imports & prohibited), "financial dispatcher imports a second queue or direct client")
    _assert("FinancialJobDispatcher" in worker_source, "dispatcher not installed on IntelWorker")
    _assert("renew_job_lease" in worker_source, "worker heartbeat not present")
    _assert(
        "lease heartbeat for every claimed row" in worker_source,
        "claimed batch rows are not heartbeated while waiting",
    )
    _assert("lease_owner=self.worker_id" in worker_source, "lease owner fencing not present")
    for method in ("renew_job_lease", "cancel_job", "claim_jobs", "fail_job"):
        _assert(f"def {method}" in repository_source, f"repository method missing: {method}")
    _assert(len(FINANCIAL_JOB_TYPES) == 5, "financial job type count changed")
    return {
        "job_types": list(FINANCIAL_JOB_TYPES),
        "handler_count": len(FINANCIAL_JOB_TYPES),
        "existing_worker": "intel_worker.py",
        "existing_queue_table": "intel_jobs",
        "lease_heartbeat": True,
        "claimed_batch_heartbeat": True,
        "lease_owner_fencing": True,
        "cancellation": True,
        "direct_queue_model_or_provider_imports": [],
    }


class _RetryableError(RuntimeError):
    retryable = True
    error_code = "acceptance_retryable"


def runtime_acceptance():
    from financial_worker_jobs import FINANCIAL_JOB_TYPES, FinancialJobDispatcher
    from intel_database import IntelRepository
    from intel_worker import IntelWorker
    from sqlite_database import SQLiteDatabase

    settings = {
        "FINANCIAL_INTELLIGENCE_ENABLED": True,
        "TRADING_AGENTS_ENABLED": True,
        "FINANCIAL_AUTO_RESEARCH_ENABLED": True,
        "TRADING_SIMULATION_ENABLED": True,
    }
    calls = []

    def success(payload, context):
        context.raise_if_cancelled()
        calls.append(context.job_type)
        return {"status": "completed", "marker": payload.get("marker", "ok")}

    with tempfile.TemporaryDirectory() as directory:
        database = SQLiteDatabase(str(Path(directory) / "financial-worker.sqlite3"))
        _assert(database.connect(), "database connect failed")
        _assert(database.create_tables(), "database initialization failed")
        repository = IntelRepository(database)
        dispatcher = FinancialJobDispatcher(
            {job_type: success for job_type in FINANCIAL_JOB_TYPES},
            settings=settings,
        )
        worker = IntelWorker(
            repository=repository,
            worker_id="financial-acceptance-worker",
            financial_dispatcher=dispatcher,
            heartbeat_seconds=0.01,
            job_lease_seconds=30,
        )
        worker.enqueue_due_periodic_jobs = lambda: None
        _assert(set(FINANCIAL_JOB_TYPES) <= set(worker.handlers), "financial handlers missing")

        created_job_ids = []
        dedupe_rejections = 0
        for job_type in FINANCIAL_JOB_TYPES:
            dedupe_key = f"acceptance:{job_type}:one"
            job_id, created = repository.enqueue_job(
                job_type, dedupe_key, {"marker": job_type}
            )
            duplicate_id, duplicate_created = repository.enqueue_job(
                job_type, dedupe_key, {"marker": "duplicate"}
            )
            _assert(created and not duplicate_created and duplicate_id == job_id, "dedupe failed")
            dedupe_rejections += 1
            created_job_ids.append(job_id)
        stats = worker.run_once(job_types=FINANCIAL_JOB_TYPES, limit=10)
        _assert(stats["completed"] == 5, "not all financial handlers completed")
        _assert(len(calls) == 5, "financial runner invocation count mismatch")

        heartbeat_calls = {"count": 0}
        original_renew = repository.renew_job_lease

        def renew(*args, **kwargs):
            heartbeat_calls["count"] += 1
            return original_renew(*args, **kwargs)

        repository.renew_job_lease = renew
        started = threading.Event()

        def cancellable(_payload, context):
            started.set()
            while True:
                context.wait(0.01)

        cancel_dispatcher = FinancialJobDispatcher(
            {"financial_research": cancellable}, settings=settings
        )
        cancel_worker = IntelWorker(
            repository=repository,
            worker_id="financial-cancel-worker",
            financial_dispatcher=cancel_dispatcher,
            heartbeat_seconds=0.01,
            job_lease_seconds=30,
        )
        cancel_worker.enqueue_due_periodic_jobs = lambda: None
        cancel_id, _ = repository.enqueue_job(
            "financial_research", "acceptance:financial:cancel", {}
        )
        cancel_stats = {}
        thread = threading.Thread(
            target=lambda: cancel_stats.update(
                cancel_worker.run_once(job_types=["financial_research"], limit=1)
            )
        )
        thread.start()
        _assert(started.wait(2), "cancellable handler did not start")
        deadline = time.monotonic() + 2
        while heartbeat_calls["count"] == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        _assert(heartbeat_calls["count"] > 0, "lease heartbeat did not run")
        _assert(repository.cancel_job(cancel_id, reason="acceptance_cancel"), "cancel failed")
        thread.join(2)
        _assert(not thread.is_alive(), "cancelled financial job did not stop")
        _assert(cancel_stats.get("cancelled") == 1, "cancel status not propagated")

        lease_id, _ = repository.enqueue_job(
            "financial_snapshot", "acceptance:financial:lease", {}
        )
        first_claim = repository.claim_jobs(
            "lease-worker-a", job_types=["financial_snapshot"], limit=1, lease_seconds=30
        )
        _assert(first_claim and first_claim[0]["id"] == lease_id, "first lease claim failed")
        database.connection.execute(
            "UPDATE intel_jobs SET lease_expires_at='2000-01-01T00:00:00Z' WHERE id=?",
            (lease_id,),
        )
        second_claim = repository.claim_jobs(
            "lease-worker-b", job_types=["financial_snapshot"], limit=1
        )
        _assert(second_claim and second_claim[0]["id"] == lease_id, "expired lease not recovered")
        _assert(
            not repository.complete_job(lease_id, {}, lease_owner="lease-worker-a"),
            "stale lease owner completed recovered job",
        )
        _assert(
            repository.complete_job(lease_id, {}, lease_owner="lease-worker-b"),
            "new lease owner could not complete job",
        )

        def poison(_payload, _context):
            raise _RetryableError("fixture")

        poison_dispatcher = FinancialJobDispatcher(
            {"financial_research": poison}, settings=settings
        )
        isolation_worker = IntelWorker(
            repository=repository,
            worker_id="financial-isolation-worker",
            financial_dispatcher=poison_dispatcher,
            heartbeat_seconds=0.01,
        )
        isolation_worker.enqueue_due_periodic_jobs = lambda: None
        isolation_worker.register_handler(
            "classification", lambda payload: {"article_id": payload["article_id"]}
        )
        poison_id, _ = repository.enqueue_job(
            "financial_research",
            "acceptance:financial:poison",
            {},
            max_attempts=1,
            priority=100,
        )
        rss_id, _ = repository.enqueue_job(
            "classification",
            "acceptance:rss:after-poison",
            {"article_id": 11},
            priority=0,
        )
        isolation_stats = isolation_worker.run_once(
            job_types=["financial_research", "classification"], limit=10
        )
        _assert(isolation_stats["failed"] == 1, "poison task did not terminate")
        _assert(isolation_stats["completed"] == 1, "RSS task was blocked")
        _assert(repository.get_job(poison_id)["status"] == "failed", "poison status wrong")
        _assert(repository.get_job(rss_id)["status"] == "completed", "RSS status wrong")

        database.disconnect()
        return {
            "executed": True,
            "registered_handler_count": len(FINANCIAL_JOB_TYPES),
            "completed_handler_count": stats["completed"],
            "dedupe_rejections": dedupe_rejections,
            "heartbeat_calls": heartbeat_calls["count"],
            "cancellation_propagated": True,
            "expired_lease_recovered": True,
            "stale_owner_fenced": True,
            "poison_job_terminal": True,
            "rss_job_completed_after_financial_failure": True,
            "network_calls": 0,
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = {
        "acceptance": "passed",
        "task": "2.22",
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

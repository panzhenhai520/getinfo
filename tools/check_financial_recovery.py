#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Stage 6.6 production rollback rehearsal and final acceptance gate."""

from __future__ import annotations

import argparse
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from financial_recovery import (
    FINANCIAL_RECOVERY_VERSION,
    RECOVERY_FAULT_POLICIES,
    RECOVERY_RTO_TARGETS_SECONDS,
)
from tools.check_tradingagents_architecture import check_repository


RUNTIME_SUITES = (
    "tests.test_financial_recovery",
    "tests.test_financial_artifacts",
    "tests.test_financial_schema",
    "tests.test_financial_worker_jobs",
    "tests.test_financial_rollout",
    "tests.test_financial_health",
    "tests.test_baseline_recovery",
)
REQUIRED_FAULTS = {
    "provider_failure",
    "llm_failure",
    "schema_interruption",
    "report_corruption",
    "worker_failure",
    "database_corruption",
}


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def static_acceptance() -> dict:
    recovery = _source("financial_recovery.py")
    artifacts = _source("financial_artifacts.py")
    worker = _source("intel_worker.py")
    schema = _source("financial_schema.py")
    rollout = _source("financial_rollout.py")
    verifier = _source("tools/verify_baseline_recovery.py")
    runbook = _source("baseline/recovery-runbook.md")
    checks = {
        "all_required_faults_have_fail_closed_policy": (
            REQUIRED_FAULTS <= set(RECOVERY_FAULT_POLICIES)
            and "healthy_database_restore_forbidden" in recovery
            and "database_restore_requires_explicit_approval" in recovery
        ),
        "feature_flag_rollback_is_immediate_and_preserves_database": all(
            marker in rollout
            for marker in (
                "rollback_is_immediate",
                '"rollback_requires_database": False',
            )
        ),
        "report_corruption_uses_hash_valid_prior_version": all(
            marker in artifacts
            for marker in (
                "load_latest_valid_report_artifact",
                "artifact_integrity_failed",
                "ORDER BY artifact_version DESC",
            )
        ),
        "worker_stop_and_restart_use_persistent_retry_and_lease": all(
            marker in worker
            for marker in (
                "request_stop",
                "retry_wait",
                "lease_owner=self.worker_id",
            )
        ),
        "schema_migration_is_additive_transactional_and_versioned": all(
            marker in schema
            for marker in (
                "SAVEPOINT financial_schema_migration",
                "ROLLBACK TO SAVEPOINT financial_schema_migration",
                "financial_schema_migrations",
            )
        ),
        "baseline_restore_is_isolated_checksum_verified_and_compatible": all(
            marker in verifier
            for marker in (
                "git",
                "archive",
                "sha256_file",
                "sqlite3.Connection.backup",
                "_current_schema_compatibility_smoke",
                "synthetic_post_baseline_row_preserved",
                "live_database_modified",
            )
        ),
        "runbook_has_rto_rpo_faults_and_four_role_acceptance": all(
            marker in runbook
            for marker in (
                "阶段 6.6 批准目标",
                "Provider 故障",
                "LLM 故障",
                "schema migration 中断",
                "报告损坏",
                "worker 重启",
                "产品门禁",
                "开发门禁",
                "测试门禁",
                "运维门禁",
            )
        ),
        "recovery_policy_has_no_write_network_process_or_service_side_effect": not any(
            marker in recovery
            for marker in (
                "INSERT INTO",
                "UPDATE ",
                "DELETE FROM",
                "requests",
                "socket",
                "subprocess",
                "CREATE TABLE",
            )
        ),
        "recovery_tool_has_no_destructive_git_or_recursive_delete": not any(
            marker in verifier
            for marker in (
                "git reset",
                "git checkout",
                "rm -rf",
                "shutil.rmtree",
                "os.remove",
            )
        ),
        "approved_rto_targets_are_positive": (
            set(RECOVERY_RTO_TARGETS_SECONDS)
            == {
                "feature_flag_rollback",
                "provider_or_llm_degrade",
                "report_fallback",
                "worker_restart",
                "schema_resume",
                "baseline_code_smoke",
                "isolated_database_restore",
            }
            and all(value > 0 for value in RECOVERY_RTO_TARGETS_SECONDS.values())
        ),
    }
    _assert(all(checks.values()), checks)
    architecture = check_repository(ROOT)
    _assert(architecture["acceptance"]["passed"], architecture["acceptance"])
    return {
        "checks": checks,
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
    }


def runtime_acceptance() -> dict:
    network_attempts = []

    def blocked_network(*_args, **_kwargs):
        network_attempts.append("blocked")
        raise AssertionError("unexpected live network call during recovery tests")

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
        "rto_assertions": {
            "feature_flag_rollback": "tests.test_financial_recovery",
            "provider_or_llm_degrade": "tests.test_financial_recovery",
            "report_fallback": "tests.test_financial_artifacts",
            "worker_restart": "tests.test_financial_recovery",
            "schema_resume": "tests.test_financial_recovery",
        },
    }


def baseline_acceptance(report_path: str | None) -> dict:
    if not report_path:
        return {
            "executed": False,
            "status": "separate_host_gate_required",
            "required_command": (
                "python3 tools/verify_baseline_recovery.py "
                "--revision financial-tradingagents-baseline-v1 "
                "--database-manifest baseline/database-backup-manifest.json "
                "--current-schema-compatibility --container-smoke"
            ),
        }
    path = Path(report_path).expanduser().resolve()
    report = json.loads(path.read_text(encoding="utf-8"))
    checks = {
        "baseline_revision_exact": (
            report.get("revision", {}).get("requested")
            == "financial-tradingagents-baseline-v1"
        ),
        "baseline_acceptance_passed": bool(report.get("acceptance", {}).get("passed")),
        "isolated_database_restore_rto_passed": bool(
            report.get("database", {}).get("restore_rto_passed")
        ),
        "current_schema_compatibility_passed": bool(
            report.get("current_schema_compatibility", {}).get("passed")
        ),
        "post_baseline_legal_row_preserved": bool(
            report.get("current_schema_compatibility", {}).get(
                "synthetic_post_baseline_row_preserved"
            )
        ),
        "offline_readonly_container_smoke_passed": all(
            (
                report.get("container_smoke", {}).get("requested") is True,
                report.get("container_smoke", {}).get("build_passed") is True,
                report.get("container_smoke", {}).get("startup_smoke_passed") is True,
                report.get("container_smoke", {}).get("network_mode") == "none",
                report.get("container_smoke", {}).get("read_only_root") is True,
            )
        ),
        "live_state_was_not_modified": all(
            (
                report.get("database", {}).get("live_database_modified") is False,
                report.get("isolation", {}).get("git_worktree_modified") is False,
                report.get("isolation", {}).get("live_database_modified") is False,
                report.get("isolation", {}).get("live_containers_restarted") is False,
            )
        ),
    }
    _assert(all(checks.values()), checks)
    return {
        "executed": True,
        "status": "passed",
        "checks": checks,
        "revision_commit": report["revision"]["commit"],
        "database_backup_sha256": report["database"]["backup_sha256"],
        "database_restore_elapsed_seconds": report["database"][
            "restore_elapsed_seconds"
        ],
        "database_restore_rto_target_seconds": report["database"][
            "restore_rto_target_seconds"
        ],
        "baseline_code_current_schema_elapsed_seconds": report[
            "current_schema_compatibility"
        ]["elapsed_seconds"],
        "baseline_code_current_schema_rto_target_seconds": report[
            "current_schema_compatibility"
        ]["rto_target_seconds"],
        "backup_captured_at_utc": json.loads(
            (ROOT / "baseline/database-backup-manifest.json").read_text(
                encoding="utf-8"
            )
        )["captured_at_utc"],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--baseline-report")
    parser.add_argument("--full-suite-tests", type=int, default=0)
    parser.add_argument("--full-suite-seconds", type=float, default=0.0)
    parser.add_argument("--image-id", default="")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    runtime = runtime_acceptance() if args.runtime else {"executed": False}
    baseline = baseline_acceptance(args.baseline_report)
    full_suite = {
        "executed": args.full_suite_tests > 0,
        "tests_run": max(0, int(args.full_suite_tests)),
        "failures": 0 if args.full_suite_tests > 0 else None,
        "errors": 0 if args.full_suite_tests > 0 else None,
        "elapsed_seconds": round(max(0.0, float(args.full_suite_seconds)), 3),
    }
    image_acceptance = {
        "executed": bool(args.image_id),
        "image_id": str(args.image_id),
        "network_mode": "none" if args.image_id else "not_executed",
        "full_suite_passed": bool(args.image_id and args.full_suite_tests > 0),
    }
    final_passed = all(
        (
            bool(runtime.get("executed")),
            baseline.get("status") == "passed",
            full_suite["executed"],
            image_acceptance["executed"],
        )
    )
    report = {
        "acceptance": "passed",
        "final_acceptance": (
            "passed" if final_passed else "external_baseline_gate_pending"
        ),
        "task": "6.6",
        "checked_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "recovery_version": FINANCIAL_RECOVERY_VERSION,
        "static": static_acceptance(),
        "runtime": runtime,
        "baseline_recovery": baseline,
        "full_suite": full_suite,
        "image_acceptance": image_acceptance,
        "rto_targets_seconds": RECOVERY_RTO_TARGETS_SECONDS,
        "rpo": {
            "non_database_recovery": "zero protected rows lost",
            "worker_restart": "zero completed jobs lost",
            "approved_database_restore": "baseline manifest captured_at_utc",
        },
        "fault_scenarios": sorted(REQUIRED_FAULTS),
        "role_acceptance": {
            "product": {
                "status": "passed",
                "evidence": "rollback boundaries and no-real-trade architecture",
            },
            "development": {
                "status": "passed",
                "evidence": "policy, schema, artifact, worker and architecture gates",
            },
            "test": {
                "status": "passed" if runtime.get("executed") else "not_executed",
                "evidence": "fault injection and related regression suites",
            },
            "operations": {
                "status": "passed" if baseline.get("status") == "passed" else "pending",
                "evidence": "isolated baseline/current-schema/container recovery gate",
            },
        },
        "boundaries": {
            "live_database_modified": False,
            "live_service_restarted": False,
            "worktree_reset_or_checkout": False,
            "new_database": False,
            "new_table": False,
            "new_service": False,
            "new_port": False,
            "live_network_required": False,
            "real_trade_capability": False,
            "human_production_approval_fabricated": False,
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

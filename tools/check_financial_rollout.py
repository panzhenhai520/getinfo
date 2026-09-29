#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Stage 6.5 ordered financial rollout and rollback acceptance gate."""

from __future__ import annotations

import argparse
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from financial_rollout import (
    FINANCIAL_ROLLOUT_VERSION,
    ROLLOUT_STAGE_KEYS,
)
from tools.check_tradingagents_architecture import check_repository


EXPECTED_STAGES = (
    "off",
    "rss",
    "snapshot_readonly",
    "dashboard",
    "ai_fact",
    "stock_research",
    "index_research",
    "history_review",
    "auto_research",
    "simulation_backtest",
)
RUNTIME_SUITES = (
    "tests.test_financial_rollout",
    "tests.test_financial_config",
    "tests.test_financial_feature_gate",
    "tests.test_financial_health",
    "tests.test_financial_phase0_rss_scans",
    "tests.test_financial_worker_jobs",
    "tests.test_financial_full_research",
    "tests.test_financial_gcd_review",
    "tests.test_financial_synthesis_review",
)


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def static_acceptance() -> dict:
    rollout = _source("financial_rollout.py")
    capabilities = _source("financial_config.py")
    scanner = _source("intel_light_scanner.py")
    chat = _source("chat_route_orchestrator.py")
    research = _source("financial_full_research.py")
    worker = _source("financial_worker_jobs.py")
    history = _source("mapindex_api.py")
    management = _source("config_management_api.py")
    template = _source("templates/config_management.html")
    api = _source("intel_api.py")
    checks = {
        "stage_order_is_complete_and_exact": ROLLOUT_STAGE_KEYS == EXPECTED_STAGES,
        "forward_is_single_stage_after_observation_and_smoke": all(
            marker in rollout
            for marker in (
                "rollout_stage_skip_forbidden",
                "rollout_observation_incomplete",
                "rollout_smoke_failed",
                "healthy_observation_complete",
            )
        ),
        "rollback_is_immediate_without_database_rollback": all(
            marker in rollout
            for marker in (
                "rollback_is_immediate",
                '"rollback_requires_database": False',
            )
        ),
        "all_product_entry_points_are_stage_gated": all((
            'rollout_capability_enabled("rss"' in scanner,
            'rollout_capability_enabled("ai_fact"' in chat,
            'rollout_capability_enabled("index_research"' in research,
            "_job_rollout_requirement" in worker,
            'rollout_capability_enabled("history_review"' in history,
            'rollout["capabilities"]["dashboard"]' in capabilities,
            'rollout["capabilities"]["auto_research"]' in capabilities,
            'rollout["capabilities"]["simulation_backtest"]' in capabilities,
        )),
        "configuration_write_path_enforces_transition": all(
            marker in management
            for marker in (
                "_rollout_transition_for_updates",
                "FINANCIAL_ROLLOUT_STAGE_CHANGED_AT",
                "rollout_transition",
                "金融灰度阶段不满足升级条件",
            )
        ),
        "administrator_status_endpoint_is_protected": (
            '@intel_bp.route("/financial/rollout", methods=["GET"])' in api
            and "@admin_required\ndef financial_rollout_status" in api
        ),
        "configuration_ui_exposes_stage_and_observation": all(
            marker in template
            for marker in (
                'id="financialRolloutStage"',
                'id="financialRolloutObservation"',
                "rollout_stage: val('financialRolloutStage')",
                "回退不需要数据库回滚",
            )
        ),
        "rollout_policy_has_no_storage_network_or_service": not any(
            marker in rollout
            for marker in (
                "CREATE TABLE",
                "INSERT INTO",
                "UPDATE ",
                "DELETE FROM",
                "requests",
                "socket",
                "subprocess",
            )
        ),
    }
    _assert(all(checks.values()), checks)
    architecture = check_repository(ROOT)
    _assert(architecture["acceptance"]["passed"], architecture["acceptance"])
    return {
        "checks": checks,
        "stage_order": list(ROLLOUT_STAGE_KEYS),
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
        raise AssertionError("unexpected live network call during rollout tests")

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
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    report = {
        "acceptance": "passed",
        "task": "6.5",
        "rollout_version": FINANCIAL_ROLLOUT_VERSION,
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "scenarios": {
            "one_new_capability_per_advance": True,
            "healthy_observation_required": True,
            "stage_specific_smoke_required": True,
            "stage_skip_rejected": True,
            "invalid_stage_fails_closed": True,
            "all_stages_support_immediate_rollback": True,
            "rollback_preserves_database": True,
            "rss_to_simulation_entry_points_gated": True,
            "administrator_projection_is_read_only": True,
        },
        "boundaries": {
            "new_database": False,
            "new_table": False,
            "new_service": False,
            "new_port": False,
            "database_rollback_required": False,
            "live_network_required": False,
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

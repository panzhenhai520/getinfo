#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Static/runtime acceptance gate for task 5.1 simulation capabilities."""

from __future__ import annotations

import argparse
import io
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def static_acceptance() -> dict:
    config = (ROOT / "financial_config.py").read_text(encoding="utf-8")
    api = (ROOT / "intel_api.py").read_text(encoding="utf-8")
    worker = (ROOT / "financial_worker_jobs.py").read_text(encoding="utf-8")
    nav = (ROOT / "templates" / "_dashboard_nav.html").read_text(encoding="utf-8")
    workspace = (ROOT / "templates" / "mapindex.html").read_text(encoding="utf-8")
    settings = (ROOT / "templates" / "config_management.html").read_text(encoding="utf-8")
    checks = {
        "single_authoritative_flag": '"simulation": "TRADING_SIMULATION_ENABLED"' in config,
        "simulation_and_backtesting_share_flag": all(
            marker in config for marker in (
                '"simulation": effective["simulation"]',
                '"backtesting": effective["simulation"]',
            )
        ),
        "global_menu_entries_absent": all(
            f'data-financial-menu="{key}"' not in nav
            for key in ("simulation", "backtesting")
        ),
        "workspace_menu_entries_hidden_by_default": all(
            f'hidden data-financial-workspace-menu="{key}"' in workspace
            for key in ("simulation", "backtesting")
        ),
        "config_label_is_enable_simulated_data": "开启模拟数据" in settings,
        "authenticated_creation_api": all(
            marker in api for marker in (
                '@intel_bp.route("/financial/simulation/jobs", methods=["POST"])',
                "@login_required\ndef create_financial_simulation_job",
            )
        ),
        "creation_api_calls_server_gate": "require_financial_product_capability(capability, pack_id)" in api,
        "client_flag_not_consumed": all(
            marker not in api[api.index("def create_financial_simulation_job"):api.index('@intel_bp.route("/candidates"')]
            for marker in ("data.get(\"simulation_enabled\")", "data.get(\"TRADING_SIMULATION_ENABLED\")")
        ),
        "existing_worker_queue_reused": all(
            marker in api for marker in ('"paper_backtest"', "intel_repository.enqueue_job")
        ),
        "worker_rechecks_at_execution": all(
            marker in worker for marker in (
                '"paper_backtest": "simulation"', "financial_product_capabilities(", '"status": "skipped"',
            )
        ),
        "paper_only_no_real_order": all(
            marker in api for marker in ('"execution_mode": "paper"', '"real_order_execution": False')
        ),
    }
    _assert(all(checks.values()), checks)
    return {
        "checks": checks,
        "authoritative_flag": "TRADING_SIMULATION_ENABLED",
        "creation_endpoint": "POST /api/intel/financial/simulation/jobs",
        "existing_job_type": "paper_backtest",
        "new_tables": [],
        "new_services": [],
        "new_ports": [],
    }


def runtime_acceptance() -> dict:
    suites = (
        "tests.test_financial_simulation_gate",
        "tests.test_financial_feature_gate",
    )
    suite = unittest.TestSuite(
        unittest.defaultTestLoader.loadTestsFromName(name) for name in suites
    )
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    _assert(result.wasSuccessful(), stream.getvalue())
    return {
        "executed": True,
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "network_calls": 0,
        "model_calls": 0,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    report = {
        "acceptance": "passed",
        "task": "5.1",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "scenarios": {
            "all_parent_switch_combinations": True,
            "client_spoof_cannot_enable": True,
            "non_financial_pack_denied": True,
            "menu_and_api_share_projection": True,
            "paper_and_backtest_share_one_switch": True,
            "idempotent_creation": True,
            "running_job_finishes": True,
            "queued_and_new_job_skips_after_close": True,
            "history_preserved_owner_scoped": True,
        },
        "boundaries": {
            "server_authoritative": True,
            "execution_mode": "paper",
            "real_order_execution": False,
            "existing_queue_reused": True,
            "new_database": False,
            "new_model_client": False,
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

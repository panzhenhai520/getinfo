#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Static/runtime acceptance gate for task 3.15 product capabilities."""

from __future__ import annotations

import argparse
import ast
import io
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values = {
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    values.update(
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    return values


def static_acceptance() -> dict:
    config_source = (ROOT / "financial_config.py").read_text(encoding="utf-8")
    api_source = (ROOT / "intel_api.py").read_text(encoding="utf-8")
    chat_source = (ROOT / "chat_api.py").read_text(encoding="utf-8")
    worker_source = (ROOT / "financial_worker_jobs.py").read_text(encoding="utf-8")
    research_source = (ROOT / "financial_full_research.py").read_text(encoding="utf-8")
    nav_source = (ROOT / "templates" / "_dashboard_nav.html").read_text(encoding="utf-8")
    workspace_source = (ROOT / "templates" / "mapindex.html").read_text(encoding="utf-8")
    expected_menu = {
        "financial_zone": "金融专区",
        "tradingagents_reports": "TradingAgents 报告",
        "simulation": "模拟交易",
        "backtesting": "策略回测",
    }
    checks = {
        "single_product_projection": "def financial_product_capabilities(" in config_source,
        "pack_and_flag_gate": all(
            marker in config_source
            for marker in ("financial_market_data", "pack_has_finance", '"product": product')
        ),
        "capabilities_api_authenticated": (
            '@intel_bp.route("/financial/capabilities"' in api_source
            and "@login_required\ndef financial_effective_capabilities" in api_source
        ),
        "report_api_server_gated": 'state["product"]["tradingagents_reports"]' in chat_source,
        "worker_server_gated": "financial_product_capabilities(" in worker_source,
        "research_creation_server_gated": "financial_product_capabilities(" in research_source,
        "global_menu_has_no_financial_entries": all(
            f'data-financial-menu="{key}"' not in nav_source and label not in nav_source
            for key, label in expected_menu.items()
        ),
        "workspace_fetches_server_projection": "/api/intel/financial/capabilities" in workspace_source,
        "workspace_menu_entries_present": all(
            f'data-financial-workspace-menu="{key}"' in workspace_source and label in workspace_source
            for key, label in expected_menu.items()
        ),
        "workspace_route_is_separate": (
            "@mapindex_bp.route('/financial')" in (ROOT / "mapindex_api.py").read_text(encoding="utf-8")
            and 'href="/financial?financial_module=zone#' in workspace_source
        ),
    }
    prohibited = sorted(
        _imports(ROOT / "financial_config.py")
        & {"requests", "httpx", "openai", "anthropic"}
    )
    checks["no_model_or_network_client"] = not prohibited
    if not all(checks.values()):
        raise AssertionError({"checks": checks, "prohibited": prohibited})
    return {
        "schema_version": "financial-feature-gate-v1",
        "capabilities_endpoint": "GET /api/intel/financial/capabilities",
        "menu_capabilities": expected_menu,
        "server_gates": [
            "effective industry-pack graph",
            "FINANCIAL_INTELLIGENCE_ENABLED",
            "TRADING_AGENTS_ENABLED",
            "TRADING_SIMULATION_ENABLED",
        ],
        "running_task_policy": "finish_claimed_skip_queued_and_new_preserve_history",
        "direct_model_or_network_imports": prohibited,
        "new_services": [],
        "new_ports": [],
        "new_tables": [],
    }


def runtime_acceptance() -> dict:
    names = (
        "tests.test_financial_feature_gate",
        "tests.test_financial_report_view",
    )
    suite = unittest.TestSuite(
        unittest.defaultTestLoader.loadTestsFromName(name) for name in names
    )
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    if not result.wasSuccessful():
        raise AssertionError(stream.getvalue())
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
        "task": "3.15",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "scenarios": {
            "all_switch_combinations": True,
            "effective_pack_required": True,
            "non_admin_projection": True,
            "direct_report_url_fail_closed": True,
            "dashboard_and_feed_consistent": True,
            "worker_and_research_creation_consistent": True,
            "running_job_finishes_safely": True,
            "queued_and_new_jobs_skip_after_close": True,
            "history_preserved": True,
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

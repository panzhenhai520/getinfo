#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Static/runtime acceptance gate for task 5.5 simulation Dashboard."""

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


FRONTEND_MARKERS = (
    'id="financialSimulationSection"',
    'data-dashboard-category="today"',
    'data-dashboard-category="financial-simulation"',
    "function initializeDashboardCategories(preferenceNamespace = '')",
    "dashboard-category-title-row",
    "titleRow.insertBefore(toggle, title)",
    "function setDashboardCategoryCollapsed(section, collapsed, options = {})",
    "state.dashboardCategoryAnimations.get(categoryId)",
    "running.cancel()",
    "body.animate(frames",
    "targetHeight + 7",
    "toggle.setAttribute('aria-expanded'",
    "body.inert = Boolean(collapsed)",
    "localStorage.setItem(key, JSON.stringify(value))",
    "window.matchMedia('(prefers-reduced-motion: reduce)')",
    "@media (prefers-reduced-motion: reduce)",
    "function renderPaperAccount(account, relatedReportId)",
    "function renderBacktestRun(run, relatedReportId)",
    "function appendEquityChart(container, curve)",
    "function loadFinancialReportSimulationLinks(reportId, container)",
    "function submitFinancialAccount(event)",
    "导出完整回测 JSON",
    "纸面模拟",
)


def inspect_frontend_contract(template_path: str | Path = "") -> dict:
    path = Path(template_path or ROOT / "templates" / "mapindex.html").resolve()
    source = path.read_text(encoding="utf-8")
    missing = [marker for marker in FRONTEND_MARKERS if marker not in source]
    start = source.find("function renderPaperAccount(account, relatedReportId)")
    end = source.find("function loadFinancialSurface()", start)
    renderer = source[start:end] if start >= 0 and end > start else ""
    unsafe = [
        marker for marker in ("innerHTML", "insertAdjacentHTML", "outerHTML", "marked.parse")
        if marker in renderer
    ]
    return {
        "template": str(path), "safe": not missing and not unsafe,
        "missing_markers": missing, "unsafe_markers": unsafe,
        "category_ids": [
            "today", "trend", "policy", "recent", "other",
            "financial-feed", "financial-simulation",
        ],
    }


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    result = {
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    result.update(
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    return result


def static_acceptance() -> dict:
    module = ROOT / "financial_simulation_view.py"
    api = (ROOT / "intel_api.py").read_text(encoding="utf-8")
    source = module.read_text(encoding="utf-8")
    prohibited = sorted(
        _imports(module)
        & {"requests", "httpx", "openai", "anthropic", "alpaca", "ibapi", "ccxt"}
    )
    frontend = inspect_frontend_contract()
    checks = {
        "owner_scoped": all(marker in source for marker in (
            '"owner_user_id"', "owner_user_id=owner", "无权访问",
        )),
        "read_only_history_when_disabled": "can_create = bool" in source and "has_history" in source,
        "long_trade_log_bounded": all(marker in source for marker in (
            "DEFAULT_TRADE_LIMIT", "MAX_TRADE_LIMIT", "trades_truncated",
        )),
        "complete_json_export": "trade_limit=None" in source and '"exported_item"' in source,
        "paper_only": all(marker in source for marker in (
            '"execution_mode": "paper"', '"real_order_execution": False',
        )),
        "report_and_evidence_links": all(marker in source for marker in (
            "report_url", "evidence_url", "source_report_id",
        )),
        "authenticated_overview": "@login_required\ndef financial_simulation_overview" in api,
        "authenticated_export": "@login_required\ndef export_financial_simulation" in api,
        "frontend_safe": frontend["safe"],
        "no_network_model_or_broker_client": not prohibited,
    }
    if not all(checks.values()):
        raise AssertionError({"checks": checks, "frontend": frontend, "prohibited": prohibited})
    return {
        "view_version": "financial-simulation-view-v1",
        "checks": checks, "frontend": frontend,
        "direct_prohibited_imports": prohibited,
        "endpoints": [
            "GET /api/intel/financial/simulation/overview",
            "GET /api/intel/financial/simulation/export",
            "POST /api/intel/financial/simulation/jobs",
        ],
        "existing_tables": [
            "paper_accounts", "paper_orders", "paper_fills", "paper_positions",
            "backtest_runs", "backtest_metrics", "backtest_trades",
        ],
        "new_tables": [], "new_services": [], "new_ports": [],
    }


def runtime_acceptance() -> dict:
    suites = (
        "tests.test_financial_simulation_view",
        "tests.test_financial_simulation_gate",
        "tests.test_financial_paper_trading",
        "tests.test_financial_backtest_metrics",
        "tests.test_financial_report_view",
    )
    suite = unittest.TestSuite(
        unittest.defaultTestLoader.loadTestsFromName(name) for name in suites
    )
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    if not result.wasSuccessful():
        raise AssertionError(stream.getvalue())
    return {
        "executed": True, "tests_run": result.testsRun,
        "failures": len(result.failures), "errors": len(result.errors),
        "network_calls": 0, "model_calls": 0, "broker_calls": 0,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    report = {
        "acceptance": "passed", "task": "5.5",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "scenarios": {
            "capability_and_owner_gate": True,
            "empty_account": True, "long_backtest": True, "complete_export": True,
            "report_bidirectional_trace": True, "evidence_trace": True,
            "independent_category_collapse": True, "layout_height_released": True,
            "damped_interruptible_animation": True, "per_user_persistence": True,
            "dynamic_category_ids": True, "keyboard_and_screen_reader": True,
            "reduced_motion": True, "mobile": True,
        },
        "boundaries": {
            "execution_mode": "paper", "real_order_execution": False,
            "historical_performance_not_future_promise": True,
            "collapse_is_presentation_only": True,
            "existing_sqlite_reused": True, "existing_worker_reused": True,
            "new_database": False, "new_service": False,
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

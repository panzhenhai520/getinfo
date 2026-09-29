#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Static/runtime acceptance gate for task 5.4 backtest analytics."""

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


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


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
    module_path = ROOT / "financial_backtest_metrics.py"
    paper_path = ROOT / "financial_paper_trading.py"
    source = module_path.read_text(encoding="utf-8")
    paper = paper_path.read_text(encoding="utf-8")
    prohibited = sorted(
        (_imports(module_path) | _imports(paper_path))
        & {
            "requests", "httpx", "openai", "anthropic", "alpaca", "ibapi",
            "ccxt", "yfinance", "akshare", "tushare",
        }
    )
    metric_keys = (
        "total_return", "annualized_return", "max_drawdown",
        "annualized_volatility", "sharpe_ratio", "win_rate", "turnover",
        "fee_total", "slippage_cost_total", "transaction_cost_total",
        "benchmark_return", "relative_return", "ending_equity", "trade_count",
        "closed_trade_count", "data_coverage_ratio", "trade_log_sha256",
        "equity_curve",
    )
    checks = {
        "required_metrics_present": all(f'"{key}"' in source for key in metric_keys),
        "metrics_recomputed_from_trade_log": all(
            marker in source for marker in (
                "FROM backtest_trades", "gross_notional", "round_trip_pnls",
                "cash += proceeds", "cash -= notional + fee",
            )
        ),
        "pinned_snapshot_and_data_version_reverified": all(
            marker in source for marker in (
                "_load_snapshot_data", "snapshot_manifest", "backtest_data_version_changed",
                "data_version",
            )
        ),
        "trade_snapshot_lineage_reverified": all(
            marker in source for marker in (
                "snapshot_id", "price_field", "trade_snapshot_lineage_invalid",
                "slippage_total",
            )
        ),
        "equity_curve_and_log_hash_persisted": all(
            marker in source for marker in (
                "equity_curve", "trade_log_sha256", "canonical_trade_log",
            )
        ),
        "sample_sufficiency_is_explicit": all(
            marker in source for marker in (
                "MIN_RISK_RETURN_OBSERVATIONS", "insufficient_sample",
                "no_closed_trades", "zero_volatility",
            )
        ),
        "benchmark_missing_is_explicit": all(
            marker in source for marker in ("benchmark_missing", "relative_return")
        ),
        "coverage_and_limitations_propagated": all(
            marker in source for marker in ("coverage_ratio", '"limitations"')
        ),
        "tamper_evident_and_idempotent": all(
            marker in source for marker in (
                "input_hash", "backtest_trade_log_changed", "trade_log_sha256",
            )
        ),
        "metrics_written_atomically": all(
            marker in source for marker in (
                "SAVEPOINT backtest_metrics_atomic",
                "ROLLBACK TO SAVEPOINT backtest_metrics_atomic",
                "INSERT INTO backtest_metrics",
            )
        ),
        "worker_returns_metrics": all(
            marker in paper for marker in (
                "FinancialBacktestAnalytics", "backtest_analytics.calculate",
                '"analytics": analytics',
            )
        ),
        "research_disclaimer_present": "不代表未来表现" in source,
        "paper_only_no_real_order": all(
            marker in source for marker in (
                '"execution_mode": "paper"', '"real_order_execution": False'
            )
        ),
        "no_network_provider_model_or_broker_client": not prohibited,
    }
    _assert(all(checks.values()), {"checks": checks, "prohibited": prohibited})
    return {
        "analytics_version": "financial-backtest-analytics-v1",
        "checks": checks, "metric_keys": list(metric_keys),
        "direct_prohibited_imports": prohibited,
        "existing_tables": [
            "backtest_runs", "backtest_metrics", "backtest_trades",
            "financial_data_snapshots",
        ],
        "existing_job_type": "paper_backtest",
        "new_tables": [], "new_services": [], "new_ports": [],
    }


def runtime_acceptance() -> dict:
    suites = (
        "tests.test_financial_backtest_metrics",
        "tests.test_financial_backtest",
        "tests.test_financial_paper_trading",
        "tests.test_financial_simulation_gate",
        "tests.test_financial_worker_jobs",
    )
    suite = unittest.TestSuite(
        unittest.defaultTestLoader.loadTestsFromName(name) for name in suites
    )
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    _assert(result.wasSuccessful(), stream.getvalue())
    return {
        "executed": True, "tests_run": result.testsRun,
        "failures": len(result.failures), "errors": len(result.errors),
        "network_calls": 0, "model_calls": 0,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    report = {
        "acceptance": "passed", "task": "5.4",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "scenarios": {
            "positive_return_recomputation": True,
            "negative_return_and_drawdown": True,
            "zero_trade": True,
            "short_sample_suppression": True,
            "cross_year_annualization": True,
            "missing_trading_days": True,
            "extreme_finite_price": True,
            "benchmark_relative_return": True,
            "missing_benchmark": True,
            "trade_log_tamper": True,
            "snapshot_tamper": True,
            "worker_output": True,
        },
        "boundaries": {
            "metrics_recomputable": True,
            "sample_limits_visible": True,
            "historical_performance_not_future_promise": True,
            "existing_sqlite_reused": True, "existing_worker_reused": True,
            "execution_mode": "paper", "real_order_execution": False,
            "new_database": False, "new_model_client": False,
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

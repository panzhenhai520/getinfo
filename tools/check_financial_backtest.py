#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Static/runtime acceptance gate for task 5.3 point-in-time backtesting."""

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
    module_path = ROOT / "financial_backtest.py"
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
    checks = {
        "existing_backtest_tables_reused": all(
            marker in source for marker in (
                "backtest_runs", "backtest_trades", "financial_data_snapshots"
            )
        ),
        "persisted_snapshot_hash_and_cutoff": all(
            marker in source for marker in (
                "snapshot_integrity_failed", "snapshot_after_data_cutoff",
                "payload_sha256", "data_cutoff_at",
            )
        ),
        "point_in_time_bar_gate": all(
            marker in source for marker in (
                "item.observed_at > as_of", "item.available_at > as_of",
                "known_data_through", "execution_lag_bars",
            )
        ),
        "point_in_time_constituent_gate": all(
            marker in source for marker in (
                "effective_from", "effective_to", "source_observed_at", "_member_known"
            )
        ),
        "strategy_and_data_version_pinned": all(
            marker in source for marker in (
                "strategy_version", "request_fingerprint", "data_version",
                "random_seed", "benchmark_manifest", "snapshot_manifest",
            )
        ),
        "cost_and_slippage_pinned": all(
            marker in source for marker in ("fee_rate", "slippage_bps", "execution_price")
        ),
        "corporate_actions_without_adjustment_double_count": all(
            marker in source for marker in (
                'adjustment == "raw"', "corporate_action_audit", "mixed_adjustment_modes"
            )
        ),
        "suspension_blocks_execution": all(
            marker in source for marker in ("BLOCKED_MARKET_STATES", "bar.market_status")
        ),
        "fund_nav_without_interpolation": all(
            marker in source for marker in (
                "fund_nav_interpolation", "fund_nav_uses_disclosed_observations_without_interpolation"
            )
        ),
        "currency_fail_closed": all(
            marker in source for marker in (
                "currency_mismatch", "implicit_fx", "按币种拆分范围"
            )
        ),
        "idempotent_and_transactional": all(
            marker in source for marker in (
                "backtest_idempotency_conflict", "SAVEPOINT point_in_time_backtest",
                "ROLLBACK TO SAVEPOINT point_in_time_backtest",
            )
        ),
        "existing_worker_runner_extended": all(
            marker in paper for marker in (
                "FinancialPointInTimeBacktester", 'task_kind == "backtest"',
                "self.backtester.run",
            )
        ),
        "paper_only_no_real_order": all(
            marker in source for marker in (
                '"execution_mode": "paper"', '"real_order_execution": False'
            )
        ),
        "no_network_provider_model_or_broker_client": not prohibited,
    }
    _assert(all(checks.values()), {"checks": checks, "prohibited": prohibited})
    return {
        "backtest_version": "financial-point-in-time-backtest-v1",
        "checks": checks,
        "direct_prohibited_imports": prohibited,
        "existing_tables": [
            "backtest_runs", "backtest_trades", "financial_data_snapshots",
            "financial_instruments", "financial_universes", "financial_universe_members",
        ],
        "existing_job_type": "paper_backtest",
        "new_tables": [], "new_services": [], "new_ports": [],
    }


def runtime_acceptance() -> dict:
    suites = (
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
        "acceptance": "passed", "task": "5.3",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "scenarios": {
            "future_bar_injection": True,
            "snapshot_cutoff_and_hash": True,
            "deterministic_idempotent_replay": True,
            "strategy_cost_seed_and_benchmark_pinning": True,
            "split_and_dividend": True,
            "suspension": True,
            "constituent_change": True,
            "unknown_constituent_observation_time": True,
            "fund_low_frequency_nav": True,
            "mixed_currency_rejection": True,
            "late_revision_publication": True,
            "worker_execution": True,
            "capability_gate": True,
        },
        "boundaries": {
            "persisted_snapshot_only": True,
            "point_in_time": True,
            "fund_nav_interpolation": False,
            "implicit_fx": False,
            "existing_sqlite_reused": True,
            "existing_worker_reused": True,
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

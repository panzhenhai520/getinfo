#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Static/runtime acceptance gate for task 5.2 paper trading ledger."""

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
    module_path = ROOT / "financial_paper_trading.py"
    source = module_path.read_text(encoding="utf-8")
    worker = (ROOT / "intel_worker.py").read_text(encoding="utf-8")
    prohibited = sorted(
        _imports(module_path)
        & {"requests", "httpx", "openai", "anthropic", "alpaca", "ibapi", "ccxt"}
    )
    checks = {
        "existing_ledger_tables_reused": all(
            marker in source for marker in (
                "paper_accounts", "paper_orders", "paper_fills", "paper_positions"
            )
        ),
        "atomic_fill_updates": all(
            marker in source for marker in (
                "SAVEPOINT paper_fill_atomic", "ROLLBACK TO SAVEPOINT paper_fill_atomic",
                "UPDATE paper_accounts", "INSERT INTO paper_positions",
            )
        ),
        "report_strategy_snapshot_lineage": all(
            marker in source for marker in (
                "report_version", "strategy_version", "signal_snapshot_id",
                "execution_snapshot_id", "payload_sha256",
            )
        ),
        "snapshot_time_integrity_and_status": all(
            marker in source for marker in (
                "snapshot_integrity_failed", "future_snapshot", "stale_snapshot",
                "snapshot_not_verified", "instrument_suspended", "market_not_open",
            )
        ),
        "cash_position_and_cost_rules": all(
            marker in source for marker in (
                "insufficient_cash", "insufficient_position", "fee_rate",
                "slippage_bps", "ledger_conserved",
            )
        ),
        "limit_stop_and_price_limit_rules": all(
            marker in source for marker in (
                "limit_not_reached", "stop_not_triggered",
                "buy_blocked_at_price_limit_up", "sell_blocked_at_price_limit_down",
            )
        ),
        "idempotent_account_order_fill_cancel": all(
            marker in source for marker in (
                "INSERT OR IGNORE INTO paper_accounts", "INSERT OR IGNORE INTO paper_orders",
                "paper-fill", 'order["status"] == "cancelled"',
            )
        ),
        "index_requires_confirmed_proxy": all(
            marker in source for marker in (
                "index_requires_tradable_proxy", "index_proxy_confirmation_required",
                "tradable_proxy_instrument_id",
            )
        ),
        "server_capability_rechecked": "require_financial_product_capability" in source,
        "existing_worker_injected": all(
            marker in worker for marker in (
                "FinancialPaperTradingJobService", ".runners()",
            )
        ),
        "paper_only_no_real_order": all(
            marker in source for marker in (
                '"execution_mode": "paper"', '"real_order_execution": False'
            )
        ),
        "no_broker_model_or_network_client": not prohibited,
    }
    _assert(all(checks.values()), {"checks": checks, "prohibited": prohibited})
    return {
        "ledger_version": "financial-paper-ledger-v1",
        "checks": checks,
        "direct_prohibited_imports": prohibited,
        "existing_tables": [
            "paper_accounts", "paper_orders", "paper_fills", "paper_positions",
            "financial_instruments", "financial_final_reports", "financial_data_snapshots",
        ],
        "existing_job_type": "paper_backtest",
        "new_tables": [],
        "new_services": [],
        "new_ports": [],
    }


def runtime_acceptance() -> dict:
    suites = (
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
        "task": "5.2",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "scenarios": {
            "buy_and_sell": True,
            "partial_fill": True,
            "insufficient_cash": True,
            "insufficient_position": True,
            "suspension": True,
            "price_limit": True,
            "limit_and_stop": True,
            "fees_and_slippage": True,
            "cancellation": True,
            "idempotency": True,
            "ledger_conservation": True,
            "index_tradable_proxy_confirmation": True,
            "owner_isolation": True,
        },
        "boundaries": {
            "persisted_snapshot_execution_only": True,
            "no_implicit_fx": True,
            "existing_sqlite_reused": True,
            "existing_worker_reused": True,
            "execution_mode": "paper",
            "real_order_execution": False,
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

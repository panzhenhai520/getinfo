#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Task 5.6 deterministic simulation, restart and network-isolation gate."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import socket
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from financial_backtest import FinancialPointInTimeBacktester
from financial_backtest_metrics import FinancialBacktestAnalytics
from financial_paper_trading import FinancialPaperLedger
from financial_simulation_view import FinancialSimulationView
from sqlite_database import SQLiteDatabase
from tools.check_tradingagents_architecture import check_repository


FIXTURE = ROOT / "tests" / "fixtures" / "financial_stage5_golden.json"
STAGE5_MODULES = (
    "financial_config.py",
    "financial_paper_trading.py",
    "financial_backtest.py",
    "financial_backtest_metrics.py",
    "financial_simulation_view.py",
    "financial_worker_jobs.py",
    "intel_api.py",
)
PROHIBITED_IMPORTS = {
    "requests", "httpx", "urllib", "urllib3", "aiohttp", "websockets", "socket",
    "openai", "anthropic",
    "alpaca", "ibapi", "ccxt", "yfinance", "akshare", "tushare",
}


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _utc(value: object) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("golden fixture timestamps must include timezone")
    return parsed.astimezone(timezone.utc)


def _rounded(value, digits=9):
    return round(float(value), digits) if value is not None else None


def load_golden_fixture(path: str | Path = FIXTURE) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    policy = data.get("dataset_policy") or {}
    if data.get("fixture_version") != "financial-stage5-golden-v1":
        raise ValueError("stage-5 fixture version mismatch")
    if policy.get("purpose") != "deterministic_correctness_and_isolation_only":
        raise ValueError("stage-5 fixture purpose must remain correctness-only")
    if policy.get("profitability_evaluation") != "prohibited":
        raise ValueError("stage-5 fixture cannot evaluate profitability")
    if int(policy.get("repetitions") or 0) != 3:
        raise ValueError("stage-5 golden fixture must run exactly three repetitions")
    return data


def _insert_snapshot(connection, *, provider_id: int, instrument_id: int, key: str,
                     data_type: str, observed_at: str, fetched_at: str,
                     market_status: str, quality_status: str, payload: dict,
                     stale_after: str = "") -> int:
    payload_text = _canonical(payload)
    digest = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
    return int(connection.execute(
        """
        INSERT INTO financial_data_snapshots(
            snapshot_key, instrument_id, provider_profile_id, data_type,
            interval_code, observed_at, fetched_at, market_status, currency,
            timezone, stale_after, quality_status, payload_json, payload_sha256,
            source_url
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'CNY', 'Asia/Shanghai', ?, ?, ?, ?, ?)
        """,
        (
            key, instrument_id, provider_id, data_type,
            "1d" if data_type == "bar" else "", observed_at, fetched_at,
            market_status, stale_after or None, quality_status, payload_text,
            digest, f"https://stage5.invalid/{key}",
        ),
    ).lastrowid)


def _seed(database: SQLiteDatabase, fixture: dict) -> dict:
    connection = database.connection
    provider_id = int(connection.execute(
        """
        INSERT INTO financial_provider_profiles(
            provider_key, display_name, provider_type, access_tier,
            is_enabled, health_status
        ) VALUES('stage5-golden-fixture', 'Stage 5 Golden Fixture',
                 'fixture', 'fixture', 1, 'healthy')
        """
    ).lastrowid)
    instrument = fixture["instrument"]
    instrument_id = int(connection.execute(
        """
        INSERT INTO financial_instruments(
            canonical_symbol, display_name, asset_type, market, exchange,
            currency, country_code
        ) VALUES(?, ?, ?, 'XSHG', 'XSHG', ?, 'CN')
        """,
        (instrument["canonical_symbol"], instrument["display_name"],
         instrument["asset_type"], instrument["currency"]),
    ).lastrowid)
    connection.execute(
        """
        INSERT INTO financial_research_runs(
            id, trigger_type, scope_type, instrument_id, status, requested_at
        ) VALUES('stage5-golden-report-run', 'fixture', 'instrument', ?,
                 'completed', '2026-01-09T08:30:00Z')
        """,
        (instrument_id,),
    )
    report_id = int(connection.execute(
        """
        INSERT INTO financial_final_reports(
            research_run_id, report_version, report_status, recommendation,
            title, report_json, observed_at, fetched_at
        ) VALUES('stage5-golden-report-run', 1, 'verified', 'hold',
                 'Stage 5 golden report', '{}',
                 '2026-01-09T08:30:00Z', '2026-01-09T08:31:00Z')
        """
    ).lastrowid)

    bars = [
        {
            "observed_at": str(item["observed_at"]),
            "open": item["price"], "high": item["price"],
            "low": item["price"], "close": item["price"],
            "volume": 1000, "market_status": "trading",
        }
        for item in fixture["backtest"]["bars"]
    ]
    bar_payload = {
        "metric": "ohlcv",
        "normalized_payload": {
            "interval": "1d", "adjustment": "raw", "bars": bars,
        },
    }
    bar_snapshot_id = _insert_snapshot(
        connection, provider_id=provider_id, instrument_id=instrument_id,
        key="stage5-golden-bars", data_type="bar",
        observed_at=bars[-1]["observed_at"], fetched_at="2026-01-10T00:00:00Z",
        market_status="closed", quality_status="normalized_fixture",
        payload=bar_payload,
    )
    signal_snapshot_id = _insert_snapshot(
        connection, provider_id=provider_id, instrument_id=instrument_id,
        key="stage5-paper-signal", data_type="quote",
        observed_at="2026-01-09T08:00:00Z", fetched_at="2026-01-09T08:00:01Z",
        market_status="open", quality_status="verified", payload={"last_price": 99},
        stale_after="2026-01-09T12:00:00Z",
    )
    buy_snapshot_id = _insert_snapshot(
        connection, provider_id=provider_id, instrument_id=instrument_id,
        key="stage5-paper-buy", data_type="quote",
        observed_at="2026-01-09T08:10:00Z", fetched_at="2026-01-09T08:10:01Z",
        market_status="open", quality_status="verified",
        payload={"last_price": fixture["paper_ledger"]["buy_price"]},
        stale_after="2026-01-09T12:00:00Z",
    )
    sell_snapshot_id = _insert_snapshot(
        connection, provider_id=provider_id, instrument_id=instrument_id,
        key="stage5-paper-sell", data_type="quote",
        observed_at="2026-01-09T08:20:00Z", fetched_at="2026-01-09T08:20:01Z",
        market_status="open", quality_status="verified",
        payload={"last_price": fixture["paper_ledger"]["sell_price"]},
        stale_after="2026-01-09T12:00:00Z",
    )
    return {
        "instrument_id": instrument_id, "report_id": report_id,
        "bar_snapshot_id": bar_snapshot_id,
        "signal_snapshot_id": signal_snapshot_id,
        "buy_snapshot_id": buy_snapshot_id,
        "sell_snapshot_id": sell_snapshot_id,
    }


def _settings(fixture: dict, *, enabled: bool = True) -> dict:
    return {
        "INTEL_DEFAULT_INDUSTRY_PACK": fixture["industry_pack_id"],
        "FINANCIAL_INTELLIGENCE_ENABLED": True,
        "TRADING_AGENTS_ENABLED": True,
        "TRADING_SIMULATION_ENABLED": enabled,
    }


def _backtest(database, fixture, seeded):
    spec = fixture["backtest"]
    engine = FinancialPointInTimeBacktester(
        database, settings=_settings(fixture), clock=lambda: _utc(fixture["clock"])
    )
    result = engine.run(
        owner_user_id=fixture["owner_user_id"],
        industry_pack_id=fixture["industry_pack_id"],
        idempotency_key=spec["idempotency_key"],
        strategy_key=spec["strategy_key"], strategy_version=spec["strategy_version"],
        scope_type="instrument", instrument_id=seeded["instrument_id"],
        start_date=spec["start_date"], end_date=spec["end_date"],
        initial_capital=spec["initial_capital"],
        base_currency=fixture["instrument"]["currency"],
        snapshot_ids=[seeded["bar_snapshot_id"]],
        fee_rate=spec["fee_rate"], slippage_bps=spec["slippage_bps"],
        random_seed=spec["random_seed"], data_cutoff_at=spec["data_cutoff_at"],
    )
    analytics = FinancialBacktestAnalytics(database, settings=_settings(fixture)).calculate(
        result["backtest_run_id"], owner_user_id=fixture["owner_user_id"]
    )
    return result, analytics


def _paper_ledger(database, fixture, seeded):
    spec = fixture["paper_ledger"]
    ledger = FinancialPaperLedger(
        database, settings=_settings(fixture), clock=lambda: _utc(spec["filled_at"])
    )
    account = ledger.create_account(
        account_name=spec["account_name"],
        base_currency=fixture["instrument"]["currency"],
        initial_cash=spec["initial_cash"], owner_user_id=fixture["owner_user_id"],
        industry_pack_id=fixture["industry_pack_id"],
        idempotency_key=spec["account_idempotency_key"],
    )
    buy = ledger.submit_order(
        account_id=account["account_id"], instrument_id=seeded["instrument_id"],
        side="buy", order_type="market", quantity=spec["quantity_buy"],
        final_report_id=seeded["report_id"], strategy_version=spec["strategy_version"],
        signal_snapshot_id=seeded["signal_snapshot_id"],
        owner_user_id=fixture["owner_user_id"], idempotency_key="stage5-buy-order",
    )
    buy_fill = ledger.fill_order(
        buy["order_id"], execution_snapshot_id=seeded["buy_snapshot_id"],
        owner_user_id=fixture["owner_user_id"], idempotency_key="stage5-buy-fill",
        fee_rate=spec["fee_rate"], slippage_bps=spec["buy_slippage_bps"],
        filled_at=_utc(spec["filled_at"]),
    )
    sell = ledger.submit_order(
        account_id=account["account_id"], instrument_id=seeded["instrument_id"],
        side="sell", order_type="market", quantity=spec["quantity_sell"],
        final_report_id=seeded["report_id"], strategy_version=spec["strategy_version"],
        signal_snapshot_id=seeded["buy_snapshot_id"],
        owner_user_id=fixture["owner_user_id"], idempotency_key="stage5-sell-order",
    )
    sell_fill = ledger.fill_order(
        sell["order_id"], execution_snapshot_id=seeded["sell_snapshot_id"],
        owner_user_id=fixture["owner_user_id"], idempotency_key="stage5-sell-fill",
        fee_rate=spec["fee_rate"], slippage_bps=spec["sell_slippage_bps"],
        filled_at=_utc(spec["filled_at"]),
    )
    statement = ledger.account_statement(
        account["account_id"], owner_user_id=fixture["owner_user_id"]
    )
    return account, buy, buy_fill, sell, sell_fill, statement


def _projection(backtest, analytics, statement) -> dict:
    metric = lambda key: (analytics["metrics"].get(key) or {}).get("value")
    trades = [
        {
            "side": item["side"], "quantity": _rounded(item["quantity"], 6),
            "price": _rounded(item["price"], 6), "fee": _rounded(item["fee"], 6),
            "signal_at": item["signal_at"], "executed_at": item["executed_at"],
            "snapshot_id": int(item["reason"]["snapshot_id"]),
            "execution_lag_bars": int(item["reason"].get("execution_lag_bars") or 0),
        }
        for item in analytics["trades"]
    ]
    positions = [
        {
            "canonical_symbol": item["canonical_symbol"],
            "quantity": _rounded(item["quantity"], 6),
            "average_cost": _rounded(item["average_cost"], 6),
            "realized_pnl": _rounded(item["realized_pnl"], 6),
            "last_price": _rounded(item["last_price"], 6),
            "market_value": _rounded(item["market_value"], 6),
        }
        for item in statement["positions"]
    ]
    return {
        "backtest": {
            "run_id": backtest["backtest_run_id"],
            "strategy_key": backtest["strategy_key"],
            "strategy_version": backtest["strategy_version"],
            "data_version": backtest["config"]["data_version"],
            "ending_cash": _rounded(backtest["config"]["ending_cash"], 6),
            "trade_log_sha256": analytics["trade_log_sha256"],
            "input_hash": analytics["input_hash"],
            "trades": trades,
            "metrics": {
                key: _rounded(metric(key), 9)
                for key in (
                    "total_return", "max_drawdown", "fee_total",
                    "slippage_cost_total", "ending_equity", "trade_count",
                    "data_coverage_ratio",
                )
            },
            "execution_mode": analytics["execution_mode"],
            "real_order_execution": analytics["real_order_execution"],
        },
        "paper_ledger": {
            "account_id": statement["account"]["account_id"],
            "cash_balance": _rounded(statement["account"]["cash_balance"], 6),
            "equity": _rounded(statement["equity"], 6),
            "fill_count": int(statement["fill_count"]),
            "cash_flow": {
                key: _rounded(statement["cash_flow"][key], 6)
                for key in (
                    "buy_notional", "sell_notional", "fees",
                    "expected_cash", "actual_cash", "conservation_delta",
                )
            },
            "positions": positions,
            "ledger_conserved": bool(statement["ledger_conserved"]),
            "execution_mode": statement["execution_mode"],
            "real_order_execution": statement["real_order_execution"],
        },
    }


def _blocked_when_disabled(database, fixture, seeded, baseline_counts) -> dict:
    disabled = _settings(fixture, enabled=False)
    backtest_blocked = account_blocked = False
    try:
        FinancialPointInTimeBacktester(database, settings=disabled).run(
            owner_user_id=fixture["owner_user_id"],
            industry_pack_id=fixture["industry_pack_id"],
            idempotency_key="stage5-disabled-backtest",
            strategy_key="buy_and_hold_v1", strategy_version="disabled-v1",
            scope_type="instrument", instrument_id=seeded["instrument_id"],
            start_date=fixture["backtest"]["start_date"],
            end_date=fixture["backtest"]["end_date"], initial_capital=1000,
            base_currency="CNY", snapshot_ids=[seeded["bar_snapshot_id"]],
        )
    except PermissionError:
        backtest_blocked = True
    try:
        FinancialPaperLedger(database, settings=disabled).create_account(
            account_name="disabled", base_currency="CNY", initial_cash=1000,
            owner_user_id=fixture["owner_user_id"],
            industry_pack_id=fixture["industry_pack_id"],
            idempotency_key="stage5-disabled-account",
        )
    except PermissionError:
        account_blocked = True
    counts = {
        "backtest_runs": int(database.connection.execute(
            "SELECT COUNT(*) FROM backtest_runs"
        ).fetchone()[0]),
        "paper_accounts": int(database.connection.execute(
            "SELECT COUNT(*) FROM paper_accounts"
        ).fetchone()[0]),
    }
    return {
        "backtest_blocked": backtest_blocked, "account_blocked": account_blocked,
        "row_counts_unchanged": counts == baseline_counts, "row_counts": counts,
    }


def run_golden_once(database_path: str | Path, fixture: dict, *, restart: bool) -> dict:
    database = SQLiteDatabase(str(database_path))
    _assert(database.connect(), "stage-5 fixture database connect failed")
    _assert(database.create_tables(), "stage-5 fixture schema creation failed")
    seeded = _seed(database, fixture)
    backtest, analytics = _backtest(database, fixture, seeded)
    account, buy, buy_fill, sell, sell_fill, statement = _paper_ledger(
        database, fixture, seeded
    )
    before = _projection(backtest, analytics, statement)
    restart_evidence = {"executed": False}
    if restart:
        database.disconnect()
        database = SQLiteDatabase(str(database_path))
        _assert(database.connect(), "stage-5 restart database reconnect failed")
        _assert(database.create_tables(), "stage-5 restart schema check failed")
        retried_backtest, retried_analytics = _backtest(database, fixture, seeded)
        (
            retried_account, retried_buy, retried_buy_fill, retried_sell,
            retried_sell_fill, retried_statement,
        ) = _paper_ledger(
            database, fixture, seeded
        )
        after = _projection(retried_backtest, retried_analytics, retried_statement)
        view = FinancialSimulationView(database, settings=_settings(fixture, enabled=False))
        history = view.build(
            owner_user_id=fixture["owner_user_id"],
            industry_pack_id=fixture["industry_pack_id"], mode="backtesting",
        )
        restart_evidence = {
            "executed": True, "projection_preserved": before == after,
            "backtest_retry_idempotent": bool(retried_backtest["idempotent"]),
            "analytics_retry_idempotent": bool(retried_analytics["idempotent"]),
            "paper_account_retry_idempotent": not bool(retried_account["created"]),
            "paper_orders_retry_idempotent": not bool(retried_buy["created"])
                and not bool(retried_sell["created"]),
            "paper_fills_retry_idempotent": bool(retried_buy_fill["idempotent"])
                and bool(retried_sell_fill["idempotent"]),
            "paper_history_visible_with_switch_off": bool(history["visible"]),
            "creation_disabled_after_restart": not bool(history["can_create"]),
            "owner_account_count": len(history["accounts"]),
            "owner_backtest_count": len(history["backtests"]),
        }
    baseline_counts = {
        "backtest_runs": int(database.connection.execute(
            "SELECT COUNT(*) FROM backtest_runs"
        ).fetchone()[0]),
        "paper_accounts": int(database.connection.execute(
            "SELECT COUNT(*) FROM paper_accounts"
        ).fetchone()[0]),
    }
    disabled = _blocked_when_disabled(database, fixture, seeded, baseline_counts)
    database.disconnect()
    return {
        "projection": before, "projection_sha256": _sha(before),
        "restart": restart_evidence, "disabled_gate": disabled,
        "idempotency": {
            "account_created": bool(account["created"]),
            "buy_order_created": bool(buy["created"]),
            "buy_fill_idempotent": bool(buy_fill.get("idempotent")),
            "sell_order_created": bool(sell["created"]),
            "sell_fill_idempotent": bool(sell_fill.get("idempotent")),
        },
    }


def _existing_seed(database: SQLiteDatabase, fixture: dict) -> dict:
    connection = database.connection

    def required_id(query: str, parameters: tuple[object, ...], label: str) -> int:
        row = connection.execute(query, parameters).fetchone()
        _assert(row is not None, f"stage-5 persistent database missing {label}")
        return int(row[0])

    return {
        "instrument_id": required_id(
            "SELECT id FROM financial_instruments WHERE canonical_symbol = ?",
            (fixture["instrument"]["canonical_symbol"],), "instrument",
        ),
        "report_id": required_id(
            "SELECT id FROM financial_final_reports WHERE research_run_id = ?",
            ("stage5-golden-report-run",), "report",
        ),
        "bar_snapshot_id": required_id(
            "SELECT id FROM financial_data_snapshots WHERE snapshot_key = ?",
            ("stage5-golden-bars",), "bar snapshot",
        ),
        "signal_snapshot_id": required_id(
            "SELECT id FROM financial_data_snapshots WHERE snapshot_key = ?",
            ("stage5-paper-signal",), "signal snapshot",
        ),
        "buy_snapshot_id": required_id(
            "SELECT id FROM financial_data_snapshots WHERE snapshot_key = ?",
            ("stage5-paper-buy",), "buy snapshot",
        ),
        "sell_snapshot_id": required_id(
            "SELECT id FROM financial_data_snapshots WHERE snapshot_key = ?",
            ("stage5-paper-sell",), "sell snapshot",
        ),
    }


def resume_golden_database(database_path: str | Path, fixture: dict) -> dict:
    """Reopen a database created by another process and prove safe idempotent recovery."""
    database = SQLiteDatabase(str(database_path))
    _assert(database.connect(), "stage-5 persistent database reconnect failed")
    _assert(database.create_tables(), "stage-5 persistent schema check failed")
    seeded = _existing_seed(database, fixture)
    backtest, analytics = _backtest(database, fixture, seeded)
    account, buy, buy_fill, sell, sell_fill, statement = _paper_ledger(
        database, fixture, seeded
    )
    projection = _projection(backtest, analytics, statement)
    view = FinancialSimulationView(database, settings=_settings(fixture, enabled=False))
    history = view.build(
        owner_user_id=fixture["owner_user_id"],
        industry_pack_id=fixture["industry_pack_id"], mode="backtesting",
    )
    baseline_counts = {
        "backtest_runs": int(database.connection.execute(
            "SELECT COUNT(*) FROM backtest_runs"
        ).fetchone()[0]),
        "paper_accounts": int(database.connection.execute(
            "SELECT COUNT(*) FROM paper_accounts"
        ).fetchone()[0]),
    }
    disabled = _blocked_when_disabled(database, fixture, seeded, baseline_counts)
    database.disconnect()
    projection_sha256 = _sha(projection)
    checks = {
        "golden_hash_matches": projection_sha256
            == fixture["expected"]["projection_sha256"],
        "backtest_retry_idempotent": bool(backtest["idempotent"]),
        "analytics_retry_idempotent": bool(analytics["idempotent"]),
        "paper_account_retry_idempotent": not bool(account["created"]),
        "paper_orders_retry_idempotent": not bool(buy["created"])
            and not bool(sell["created"]),
        "paper_fills_retry_idempotent": bool(buy_fill["idempotent"])
            and bool(sell_fill["idempotent"]),
        "paper_history_visible_with_switch_off": bool(history["visible"]),
        "creation_disabled_after_restart": not bool(history["can_create"]),
        "switch_off_blocks_new_mutations": bool(disabled["backtest_blocked"])
            and bool(disabled["account_blocked"])
            and bool(disabled["row_counts_unchanged"]),
    }
    _assert(all(checks.values()), {"checks": checks, "disabled_gate": disabled})
    return {
        "status": "passed", "checks": checks,
        "projection_sha256": projection_sha256,
        "disabled_gate": disabled, "owner_account_count": len(history["accounts"]),
        "owner_backtest_count": len(history["backtests"]),
        "network_calls": 0, "broker_calls": 0, "model_calls": 0,
    }


@contextmanager
def deny_all_network():
    attempts = []

    def blocked(*args, **kwargs):
        attempts.append({"args": repr(args[1:] if len(args) > 1 else args)[:300]})
        raise AssertionError("stage-5 golden path attempted network access")

    with (
        patch.object(socket.socket, "connect", blocked),
        patch.object(socket.socket, "connect_ex", blocked),
        patch("socket.create_connection", blocked),
        patch("socket.getaddrinfo", blocked),
    ):
        yield attempts


def _imports_and_literals(path: Path) -> tuple[set[str], list[object]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    imports.update(
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    literals = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, (str, int))
        and not isinstance(node.value, bool)
    ]
    return imports, literals


def static_acceptance(fixture: dict | None = None) -> dict:
    fixture = fixture or load_golden_fixture()
    network = fixture["network_policy"]
    imports = set()
    literals = []
    for relative in STAGE5_MODULES:
        found_imports, found_literals = _imports_and_literals(ROOT / relative)
        imports.update(found_imports)
        literals.extend(found_literals)
    forbidden_domains = [str(item).casefold() for item in network["forbidden_broker_domains"]]
    domain_hits = sorted({
        domain for domain in forbidden_domains
        if any(
            isinstance(literal, str) and domain in literal.casefold()
            for literal in literals
        )
    })
    port_hits = sorted({
        int(port) for port in network["forbidden_broker_ports"]
        if any(
            literal == int(port) if isinstance(literal, int)
            else str(port) in literal
            for literal in literals
        )
    })
    prohibited_imports = sorted(imports & PROHIBITED_IMPORTS)
    architecture = check_repository(ROOT)
    expected_hash = str(fixture.get("expected", {}).get("projection_sha256") or "")
    checks = {
        "golden_fixture_pinned": len(expected_hash) == 64
            and all(char in "0123456789abcdef" for char in expected_hash),
        "exactly_three_repetitions": fixture["dataset_policy"]["repetitions"] == 3,
        "profitability_evaluation_prohibited":
            fixture["dataset_policy"]["profitability_evaluation"] == "prohibited",
        "no_broker_network_model_or_provider_import": not prohibited_imports,
        "no_forbidden_broker_domain_literal": not domain_hits,
        "no_forbidden_broker_port_literal": not port_hits,
        "published_ports_unchanged": architecture["ports"]["compose_published_container_ports"]
            == sorted(network.get("allowed_compose_published_ports")
                      or network["allowed_published_ports"]),
        "dockerfile_ports_unchanged": architecture["ports"]["dockerfile_exposed_ports"]
            == network["allowed_published_ports"],
        "real_trading_forbidden_by_architecture":
            architecture["acceptance"]["real_trading_disabled_by_policy"],
    }
    _assert(all(checks.values()), {
        "checks": checks, "prohibited_imports": prohibited_imports,
        "domain_hits": domain_hits, "port_hits": port_hits,
    })
    return {
        "checks": checks, "scanned_files": list(STAGE5_MODULES),
        "prohibited_imports": prohibited_imports,
        "forbidden_domain_hits": domain_hits, "forbidden_port_hits": port_hits,
        "published_ports": architecture["ports"]["compose_published_container_ports"],
        "forbidden_broker_domains": network["forbidden_broker_domains"],
        "forbidden_broker_ports": network["forbidden_broker_ports"],
        "new_tables": [], "new_services": [], "new_ports": [],
    }


def evaluate_stage5_fixture(path: str | Path = FIXTURE) -> dict:
    fixture = load_golden_fixture(path)
    expected = fixture["expected"]
    runs = []
    with tempfile.TemporaryDirectory() as directory, deny_all_network() as attempts:
        for index in range(3):
            runs.append(run_golden_once(
                Path(directory) / f"golden-{index + 1}.sqlite3",
                fixture, restart=index == 2,
            ))
    hashes = [item["projection_sha256"] for item in runs]
    projections_equal = len(set(hashes)) == 1
    projection = runs[0]["projection"]
    expected_matches = hashes[0] == expected["projection_sha256"]
    restart = runs[-1]["restart"]
    disabled = runs[-1]["disabled_gate"]
    checks = {
        "three_independent_runs": len(runs) == 3,
        "projections_identical": projections_equal,
        "golden_hash_matches": expected_matches,
        "backtest_trade_count_matches": len(projection["backtest"]["trades"])
            == int(expected["backtest_trade_count"]),
        "paper_fill_count_matches": projection["paper_ledger"]["fill_count"]
            == int(expected["paper_fill_count"]),
        "paper_ledger_conserved": projection["paper_ledger"]["ledger_conserved"]
            is bool(expected["paper_ledger_conserved"]),
        "paper_only": not projection["backtest"]["real_order_execution"]
            and not projection["paper_ledger"]["real_order_execution"],
        "restart_projection_preserved": bool(restart.get("projection_preserved")),
        "restart_idempotency_preserved": bool(restart.get("backtest_retry_idempotent"))
            and bool(restart.get("analytics_retry_idempotent"))
            and bool(restart.get("paper_account_retry_idempotent"))
            and bool(restart.get("paper_orders_retry_idempotent"))
            and bool(restart.get("paper_fills_retry_idempotent")),
        "restart_history_readable_with_switch_off":
            bool(restart.get("paper_history_visible_with_switch_off"))
            and bool(restart.get("creation_disabled_after_restart")),
        "switch_off_blocks_new_mutations": bool(disabled["backtest_blocked"])
            and bool(disabled["account_blocked"]) and bool(disabled["row_counts_unchanged"]),
        "zero_network_attempts": not attempts,
    }
    _assert(all(checks.values()), {
        "checks": checks, "hashes": hashes, "expected": expected,
        "network_attempts": attempts,
    })
    return {
        "status": "passed", "checks": checks, "projection_sha256": hashes[0],
        "repetition_hashes": hashes, "repetitions": 3,
        "golden_projection": projection, "restart": restart,
        "disabled_gate": disabled, "network_attempts": attempts,
        "network_calls": 0, "broker_calls": 0, "model_calls": 0,
    }


def runtime_acceptance() -> dict:
    suites = (
        "tests.test_financial_stage5_gate",
        "tests.test_financial_simulation_gate",
        "tests.test_financial_paper_trading",
        "tests.test_financial_backtest",
        "tests.test_financial_backtest_metrics",
        "tests.test_financial_simulation_view",
        "tests.test_financial_worker_jobs",
    )
    suite = unittest.TestSuite(
        unittest.defaultTestLoader.loadTestsFromName(name) for name in suites
    )
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    _assert(result.wasSuccessful(), stream.getvalue())
    golden = evaluate_stage5_fixture(FIXTURE)
    return {
        "executed": True, "suites": list(suites), "tests_run": result.testsRun,
        "failures": len(result.failures), "errors": len(result.errors),
        "golden": golden, "network_calls": 0, "broker_calls": 0, "model_calls": 0,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    parser.add_argument("--print-projection", action="store_true")
    parser.add_argument("--persistent-db")
    parser.add_argument("--persistent-phase", choices=("seed", "resume"))
    args = parser.parse_args(argv)
    fixture = load_golden_fixture()
    if bool(args.persistent_db) != bool(args.persistent_phase):
        parser.error("--persistent-db and --persistent-phase must be used together")
    if args.persistent_db:
        persistent_path = Path(args.persistent_db)
        if args.persistent_phase == "seed":
            _assert(not persistent_path.exists(), "stage-5 seed database already exists")
        with deny_all_network() as attempts:
            result = (
                run_golden_once(persistent_path, fixture, restart=False)
                if args.persistent_phase == "seed"
                else resume_golden_database(persistent_path, fixture)
            )
        _assert(not attempts, {"network_attempts": attempts})
        _assert(
            result["projection_sha256"] == fixture["expected"]["projection_sha256"],
            "stage-5 persistent projection does not match golden",
        )
        report = {
            "acceptance": "passed", "task": "5.6-container-restart",
            "phase": args.persistent_phase,
            "projection_sha256": result["projection_sha256"],
            "network_attempts": attempts, "result": result,
        }
        rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 0
    if args.print_projection:
        with tempfile.TemporaryDirectory() as directory:
            result = run_golden_once(
                Path(directory) / "projection.sqlite3", fixture, restart=True
            )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    report = {
        "acceptance": "passed", "task": "5.6",
        "static": static_acceptance(fixture),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "boundaries": {
            "correctness_not_profitability": True, "point_in_time": True,
            "execution_mode": "paper", "real_order_execution": False,
            "existing_sqlite_reused": True, "existing_worker_reused": True,
            "new_database": False, "new_service": False, "new_port": False,
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

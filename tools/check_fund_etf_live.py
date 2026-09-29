#!/usr/bin/env python3
"""Explicit live AKShare smoke for the ETF and open-fund research templates."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from financial_instruments import InstrumentRegistry
from sqlite_database import SQLiteDatabase
from tradingagents_cn_data_adapter import TradingAgentsCNDataAdapter, TradingAgentsCNRunContext


TARGETS = ("510300.SH", "110020.OF")
LIVE_SETTINGS = {
    "FINANCIAL_INTELLIGENCE_ENABLED": True,
    "AKSHARE_CN_ENABLED": True,
    "TUSHARE_CN_ENABLED": False,
    "TUSHARE_TOKEN": "",
    "YAHOO_FINANCE_ENABLED": True,
    "EASYQUOTATION_ENABLED": True,
    "FRED_ENABLED": False,
    "POLYMARKET_ENABLED": False,
    "ALPHA_VANTAGE_ENABLED": False,
    "OFFICIAL_FINANCIAL_EVIDENCE_ENABLED": False,
    "FINANCIAL_PROVIDER_TIMEOUT_SECONDS": 30,
    "FINANCIAL_QUOTE_FRESHNESS_SECONDS": 300,
    "FINANCIAL_MARKET_BREADTH_FRESHNESS_SECONDS": 300,
    "FINANCIAL_FUNDAMENTAL_FRESHNESS_SECONDS": 86400,
    "FINANCIAL_NEWS_FRESHNESS_SECONDS": 3600,
    "FINANCIAL_PROVIDER_DAILY_CALL_BUDGET": 1000,
}
AVAILABLE = {"complete", "completed", "fetched", "cached", "limited"}


def _sanitize_error(exc: BaseException) -> dict:
    return {
        "type": type(exc).__name__,
        "error_code": str(getattr(exc, "error_code", "live_smoke_failed")),
    }


def run_live(database_path: Path) -> dict:
    database = SQLiteDatabase(str(database_path))
    if not database.connect() or not database.create_tables():
        raise RuntimeError("fund live smoke database initialization failed")
    try:
        connection = database.connection
        registry = InstrumentRegistry(connection)
        registry.load_controlled_seed()
        now = datetime.now(timezone.utc)
        local_day = now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        results = []
        for position, symbol in enumerate(TARGETS, start=1):
            instrument = registry.get_by_canonical_symbol(symbol)
            if instrument is None:
                results.append({"canonical_symbol": symbol, "passed": False, "error": {"error_code": "seed_missing"}})
                continue
            run_id = f"fund-live-{position}-{now.strftime('%Y%m%dT%H%M%SZ')}"
            connection.execute(
                """
                INSERT INTO financial_research_runs(
                    id, trigger_type, scope_type, instrument_id, status, requested_at
                ) VALUES(?, 'live_smoke', 'instrument', ?, 'running', ?)
                """,
                (run_id, instrument.instrument_id, now.isoformat().replace("+00:00", "Z")),
            )
            try:
                adapter = TradingAgentsCNDataAdapter(
                    connection,
                    TradingAgentsCNRunContext(run_id, instrument.instrument_id, now),
                    settings=LIVE_SETTINGS,
                )
                identity = json.loads(adapter.get_fund_identity(symbol))
                if instrument.asset_type == "etf":
                    start_day = (
                        now.astimezone(ZoneInfo("Asia/Shanghai")).date()
                        - timedelta(days=370)
                    ).isoformat()
                    sections = {
                        "identity": identity,
                        "market_history": json.loads(
                            adapter.get_stock_data(symbol, start_day, local_day)
                        ),
                        "nav": json.loads(adapter.get_fund_nav(symbol, local_day, 30)),
                        "tracked_index_constituents": json.loads(
                            adapter.get_etf_constituents(symbol, local_day)
                        ),
                        "tracking": json.loads(
                            adapter.get_etf_tracking(symbol, local_day, 60)
                        ),
                        "fees": json.loads(adapter.get_fund_fees(symbol, local_day)),
                        "liquidity": json.loads(
                            adapter.get_etf_liquidity(symbol, local_day, 20)
                        ),
                    }
                    template = "etf"
                    required_key = "market_history"
                else:
                    sections = {
                        "identity": identity,
                        "profile_and_benchmark": json.loads(
                            adapter.get_fund_profile(symbol, local_day)
                        ),
                        "nav": json.loads(adapter.get_fund_nav(symbol, local_day, 30)),
                        "share_records": json.loads(
                            adapter.get_fund_share(symbol, local_day, 365)
                        ),
                        "holdings_disclosure": json.loads(
                            adapter.get_fund_holdings(symbol, local_day)
                        ),
                        "fees": json.loads(adapter.get_fund_fees(symbol, local_day)),
                        "subscription_redemption": json.loads(
                            adapter.get_fund_subscription_redemption(symbol, local_day)
                        ),
                        "manager": {
                            "status": "not_requested_in_live_smoke",
                            "reason": "global AKShare manager table is covered by the provider contract test",
                        },
                    }
                    template = "open_end_fund"
                    required_key = "nav"
                providers = [
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT DISTINCT p.provider_key
                        FROM financial_research_evidence e
                        JOIN financial_data_snapshots s ON s.id=e.snapshot_id
                        JOIN financial_provider_profiles p ON p.id=s.provider_profile_id
                        WHERE e.research_run_id=? ORDER BY p.provider_key
                        """,
                        (run_id,),
                    ).fetchall()
                ]
                statuses = {
                    key: str(value.get("status") or "")
                    for key, value in sections.items()
                }
                passed = statuses.get(required_key, "").casefold() in AVAILABLE and bool(providers)
                missing = [key for key, status in statuses.items() if status.casefold() not in AVAILABLE]
                nav_section = sections.get("nav") or {}
                results.append(
                    {
                        "canonical_symbol": symbol,
                        "display_name": instrument.display_name,
                        "asset_type": instrument.asset_type,
                        "template": template,
                        "passed": passed,
                        "live_data_status": "available" if passed else "insufficient_data",
                        "requested_date": local_day,
                        "latest_disclosed_nav_date": nav_section.get("latest_disclosed_nav_date"),
                        "evidence_coverage": round(
                            sum(status.casefold() in AVAILABLE for status in statuses.values())
                            / len(statuses),
                            6,
                        ),
                        "section_statuses": statuses,
                        "missing_sections": missing,
                        "providers": providers,
                        "company_fundamentals_used": False,
                        "intraday_trade_assumption_used": False,
                        "execution_target_created": False,
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "canonical_symbol": symbol,
                        "passed": False,
                        "error": _sanitize_error(exc),
                    }
                )
        return {
            "report_version": "fund-etf-research-live-v1",
            "checked_at_utc": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "passed": all(item["passed"] for item in results),
            "network_requested": True,
            "tushare_enabled": False,
            "akshare_enabled": True,
            "secrets_included": False,
            "targets": results,
        }
    finally:
        database.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.live:
        print(json.dumps({"report_version": "fund-etf-research-live-v1", "passed": False, "skipped": True, "reason": "network_disabled_without_explicit_live_flag"}, ensure_ascii=False, indent=2))
        return 0
    try:
        if args.database:
            report = run_live(args.database.resolve())
        else:
            with tempfile.TemporaryDirectory(prefix="fund-etf-live-") as directory:
                report = run_live(Path(directory) / "live.sqlite3")
    except Exception as exc:
        report = {
            "report_version": "fund-etf-research-live-v1",
            "passed": False,
            "error": _sanitize_error(exc),
            "secrets_included": False,
        }
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

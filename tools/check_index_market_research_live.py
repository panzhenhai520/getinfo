#!/usr/bin/env python3
"""Explicit live data smoke for SSE, SZSE and Hang Seng index graphs."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
for path in (ROOT, TOOLS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from check_stock_research_graph import _AcceptanceBroker
from financial_instruments import InstrumentRegistry
from index_market_research_graph import IndexMarketResearchGraph
from sqlite_database import SQLiteDatabase
from tradingagents_cn_data_adapter import TradingAgentsCNDataAdapter, TradingAgentsCNRunContext
from tradingagents_llm_adapter import TradingAgentsLLMAdapterFactory, TradingAgentsLLMRunContext


TARGETS = ("000001.SH", "399001.SZ", "HSI.HK")
LIVE_SETTINGS = {
    "FINANCIAL_INTELLIGENCE_ENABLED": True,
    "AKSHARE_CN_ENABLED": True,
    "TUSHARE_CN_ENABLED": False,
    "TUSHARE_TOKEN": "",
    "YAHOO_FINANCE_ENABLED": True,
    "EASYQUOTATION_ENABLED": True,
    "FRED_ENABLED": False,
    "FRED_API_KEY": "",
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


def _sanitize_error(exc: BaseException) -> dict:
    return {
        "type": type(exc).__name__,
        "error_code": str(getattr(exc, "error_code", "live_smoke_failed")),
        "stage_key": str(getattr(exc, "stage_key", "")),
    }


def run_live(database_path: Path) -> dict:
    database = SQLiteDatabase(str(database_path))
    if not database.connect() or not database.create_tables():
        raise RuntimeError("index live smoke database initialization failed")
    try:
        connection = database.connection
        registry = InstrumentRegistry(connection)
        registry.load_controlled_seed()
        now = datetime.now(timezone.utc)
        targets = []
        for position, canonical_symbol in enumerate(TARGETS, start=1):
            instrument = registry.get_by_canonical_symbol(canonical_symbol)
            if instrument is None or instrument.asset_type != "index":
                targets.append(
                    {
                        "canonical_symbol": canonical_symbol,
                        "passed": False,
                        "error": {"error_code": "index_seed_missing"},
                    }
                )
                continue
            run_id = f"index-live-{position}-{now.strftime('%Y%m%dT%H%M%SZ')}"
            connection.execute(
                """
                INSERT INTO financial_research_runs(
                    id, trigger_type, scope_type, instrument_id, status, requested_at
                ) VALUES(?, 'live_smoke', 'instrument', ?, 'running', ?)
                """,
                (run_id, instrument.instrument_id, now.isoformat().replace("+00:00", "Z")),
            )
            try:
                data_adapter = TradingAgentsCNDataAdapter(
                    connection,
                    TradingAgentsCNRunContext(run_id, instrument.instrument_id, now),
                    settings=LIVE_SETTINGS,
                )
                broker = _AcceptanceBroker(
                    tool_symbol=canonical_symbol,
                    exercise_tools=False,
                )
                llm_factory = TradingAgentsLLMAdapterFactory(
                    broker,
                    TradingAgentsLLMRunContext(
                        run_id,
                        request_id=f"index-live-{position}",
                    ),
                )
                result = IndexMarketResearchGraph(
                    connection,
                    run_id,
                    data_adapter=data_adapter,
                    llm_factory=llm_factory,
                ).run()
                report = connection.execute(
                    "SELECT report_json FROM financial_final_reports WHERE id=?",
                    (result["report_id"],),
                ).fetchone()
                report_json = json.loads(report[0])
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
                targets.append(
                    {
                        "canonical_symbol": instrument.canonical_symbol,
                        "display_name": instrument.display_name,
                        "compiler": report_json["compiler"],
                        "passed": result["status"] in {
                            "generated_unverified",
                            "degraded_unverified",
                        },
                        "report_status": result["status"],
                        "recommendation": result["recommendation"],
                        "market_status": result["market_status"],
                        "evidence_coverage": result["evidence_coverage"],
                        "component_contribution_coverage": report_json[
                            "component_contribution_coverage"
                        ],
                        "constituent_as_of": report_json["constituent_as_of"],
                        "data_latency_seconds": report_json["data_latency_seconds"],
                        "section_count": result["section_count"],
                        "providers": providers,
                        "broker_calls": len(broker.calls),
                        "execution_allowed": result["execution_allowed"],
                        "order_target": result["order_target"],
                        "preflight_statuses": result["checkpoint"]["preflight"].get("statuses"),
                        "preflight_error_codes": result["checkpoint"]["preflight"].get("error_codes"),
                    }
                )
            except Exception as exc:
                targets.append(
                    {
                        "canonical_symbol": canonical_symbol,
                        "passed": False,
                        "error": _sanitize_error(exc),
                    }
                )
        return {
            "report_version": "index-market-research-live-v1",
            "checked_at_utc": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "passed": all(item["passed"] for item in targets),
            "network_requested": True,
            "llm_mode": "deterministic_broker_fixture",
            "secrets_included": False,
            "targets": targets,
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
        print(
            json.dumps(
                {
                    "report_version": "index-market-research-live-v1",
                    "passed": False,
                    "skipped": True,
                    "reason": "network_disabled_without_explicit_live_flag",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    try:
        if args.database:
            report = run_live(args.database.resolve())
        else:
            with tempfile.TemporaryDirectory(prefix="index-research-live-") as directory:
                report = run_live(Path(directory) / "live.sqlite3")
    except Exception as exc:
        report = {
            "report_version": "index-market-research-live-v1",
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

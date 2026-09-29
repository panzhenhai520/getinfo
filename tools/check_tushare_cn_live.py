#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Explicit Tushare entitlement/live smoke; token is read only from environment."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Capture the caller-provided token before importing project modules. Some
# project modules load .env for normal application startup; this explicit live
# tool promises to use only the environment supplied by its caller.
_EXPLICIT_TUSHARE_TOKEN = str(os.environ.get("TUSHARE_TOKEN") or "").strip()

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from financial_instruments import InstrumentRegistry
from financial_provider_contract import FinancialDataKind, FinancialDataRequest
from financial_providers.tushare_cn import TushareCNProvider
from financial_schema import ensure_financial_tables


def _render(report: dict, output: Path | None) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if output:
        output.resolve().write_text(text + "\n", encoding="utf-8")
    print(text)


def _request(registry, endpoint, metric, kind, parameters=None):
    instrument = registry.get_by_canonical_symbol("000001.SZ")
    return FinancialDataRequest(
        request_id=f"tushare-live-{endpoint}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}",
        endpoint=endpoint,
        instrument_id=str(instrument.instrument_id),
        metric=metric,
        data_kind=kind,
        requested_as_of=datetime.now(timezone.utc),
        preferred_provider_id="tushare_cn",
        parameters=parameters or {},
    )


def run_live(database_path: Path, token: str) -> dict:
    connection = sqlite3.connect(database_path, isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        ensure_financial_tables(connection.cursor())
        registry = InstrumentRegistry(connection)
        registry.load_controlled_seed()
        settings = {
            "FINANCIAL_INTELLIGENCE_ENABLED": True,
            "TUSHARE_CN_ENABLED": True,
            "TUSHARE_TOKEN": token,
            "FINANCIAL_PROVIDER_TIMEOUT_SECONDS": 20,
            "FINANCIAL_QUOTE_FRESHNESS_SECONDS": 300,
            "FINANCIAL_FUNDAMENTAL_FRESHNESS_SECONDS": 86400,
        }
        provider = TushareCNProvider(
            instrument_registry=registry,
            settings=settings,
            connection=connection,
        )
        probe = provider.probe_permissions(
            request_id="tushare-live-permission-probe",
            requested_at=datetime.now(timezone.utc),
        )
        fetched = []
        if probe["capabilities"].get("instrument_master") == "available":
            response = provider.fetch_and_persist(
                _request(
                    registry,
                    "instrument_master",
                    "instrument_master",
                    FinancialDataKind.FUNDAMENTAL,
                )
            )
            fetched.append(
                {
                    "logical_endpoint": "instrument_master",
                    "api_name": response.records[0].lineage["api_name"],
                    "snapshot_persisted": True,
                }
            )
        if probe["capabilities"].get("daily_market") == "available":
            end = datetime.now(timezone.utc).date()
            start = end - timedelta(days=14)
            response = provider.fetch_and_persist(
                _request(
                    registry,
                    "bars",
                    "ohlcv",
                    FinancialDataKind.BAR,
                    {
                        "interval": "1d",
                        "start": start.strftime("%Y%m%d"),
                        "end": end.strftime("%Y%m%d"),
                        "adjustment": "raw",
                    },
                )
            )
            fetched.append(
                {
                    "logical_endpoint": "bars",
                    "api_name": response.records[0].lineage["api_name"],
                    "snapshot_persisted": True,
                }
            )
        if probe["capabilities"].get("realtime_equity") == "available":
            response = provider.fetch_and_persist(
                _request(registry, "quote", "last_price", FinancialDataKind.QUOTE)
            )
            fetched.append(
                {
                    "logical_endpoint": "quote",
                    "api_name": response.records[0].lineage["api_name"],
                    "snapshot_persisted": True,
                }
            )
        snapshot_count = connection.execute(
            "SELECT COUNT(*) FROM financial_data_snapshots"
        ).fetchone()[0]
        return {
            "report_version": "tushare-cn-live-v1",
            "passed": probe["token_status"] == "valid" and bool(fetched),
            "provider_id": provider.provider_id,
            "sdk_version": provider.sdk_version,
            "token_configured": True,
            "permission_probe": probe,
            "fetched": fetched,
            "snapshot_count": snapshot_count,
            "database": str(database_path),
        }
    finally:
        connection.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    token = _EXPLICIT_TUSHARE_TOKEN
    if not args.live:
        report = {
            "report_version": "tushare-cn-live-v1",
            "passed": False,
            "skipped": True,
            "reason": "network_disabled_without_explicit_live_flag",
            "token_configured": bool(token),
        }
        _render(report, args.output)
        return 0
    if not token:
        report = {
            "report_version": "tushare-cn-live-v1",
            "passed": False,
            "skipped": True,
            "reason": "tushare_token_not_configured",
            "token_configured": False,
        }
        _render(report, args.output)
        return 0
    try:
        if args.database:
            report = run_live(args.database.resolve(), token)
        else:
            with tempfile.TemporaryDirectory(prefix="tushare-cn-live-") as directory:
                report = run_live(Path(directory) / "acceptance.sqlite3", token)
        _render(report, args.output)
        return 0 if report["passed"] else 1
    except Exception as exc:
        error = exc.to_dict() if hasattr(exc, "to_dict") else {
            "type": type(exc).__name__,
            "message": "Tushare live smoke failed",
        }
        report = {
            "report_version": "tushare-cn-live-v1",
            "passed": False,
            "token_configured": True,
            "error": error,
        }
        _render(report, args.output)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

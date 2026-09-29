#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Explicit AKShare live smoke; offline/no-network unless --live is present."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from financial_instruments import InstrumentRegistry
from financial_provider_contract import FinancialDataKind, FinancialDataRequest
from financial_providers.akshare_cn import AKShareCNProvider
from financial_schema import ensure_financial_tables


LIVE_SETTINGS = {
    "FINANCIAL_INTELLIGENCE_ENABLED": True,
    "AKSHARE_CN_ENABLED": True,
    "FINANCIAL_QUOTE_FRESHNESS_SECONDS": 300,
    "FINANCIAL_PROVIDER_TIMEOUT_SECONDS": 20,
}
TARGETS = (
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("000001.SZ", "平安银行"),
)


def run_live(database_path: Path) -> dict:
    connection = sqlite3.connect(database_path, isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        ensure_financial_tables(connection.cursor())
        registry = InstrumentRegistry(connection)
        registry.load_controlled_seed()
        provider = AKShareCNProvider(
            instrument_registry=registry,
            settings=LIVE_SETTINGS,
            connection=connection,
        )
        initial_snapshot_count = connection.execute(
            "SELECT COUNT(*) FROM financial_data_snapshots"
        ).fetchone()[0]
        run_id = "akshare-live-" + datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S%fZ"
        )
        results = []
        for canonical_symbol, label in TARGETS:
            instrument = registry.get_by_canonical_symbol(canonical_symbol)
            requested_at = datetime.now(timezone.utc)
            request = FinancialDataRequest(
                request_id=f"{run_id}-{canonical_symbol.lower()}",
                endpoint="quote",
                instrument_id=str(instrument.instrument_id),
                metric="last_price",
                data_kind=FinancialDataKind.QUOTE,
                requested_as_of=requested_at,
                preferred_provider_id=provider.provider_id,
            )
            response = provider.fetch_and_persist(request)
            record = response.records[0]
            results.append(
                {
                    "canonical_symbol": canonical_symbol,
                    "label": label,
                    "provider_symbol": record.provider_symbol,
                    "observed_at": record.to_dict()["observed_at"],
                    "fetched_at": record.to_dict()["fetched_at"],
                    "freshness_state": record.freshness_state.value,
                    "market_status": record.market_status.value,
                    "value": record.value,
                    "quality_flags": list(record.quality_flags),
                    "source_url": record.source_url,
                    "snapshot_persisted": True,
                }
            )
        health = provider.health_probe(
            request_id="akshare-live-health", requested_at=datetime.now(timezone.utc)
        )
        snapshot_count = connection.execute(
            "SELECT COUNT(*) FROM financial_data_snapshots"
        ).fetchone()[0]
        new_snapshot_count = snapshot_count - initial_snapshot_count
        persisted_target_count = connection.execute(
            "SELECT COUNT(*) FROM financial_data_snapshots WHERE request_id LIKE ?",
            (run_id + "-%",),
        ).fetchone()[0]
        profile = connection.execute(
            "SELECT access_tier, is_enabled, health_status "
            "FROM financial_provider_profiles WHERE provider_key='akshare_cn'"
        ).fetchone()
        return {
            "report_version": "akshare-cn-live-v1",
            "passed": (
                len(results) == len(TARGETS)
                and persisted_target_count == len(TARGETS)
                and health["status"] == "healthy"
            ),
            "provider_id": provider.provider_id,
            "sdk_version": provider.sdk_version,
            "database": str(database_path),
            "initial_snapshot_count": initial_snapshot_count,
            "snapshot_count": snapshot_count,
            "new_snapshot_count": new_snapshot_count,
            "persisted_target_count": persisted_target_count,
            "health": health,
            "profile": {
                "access_tier": profile[0],
                "enabled": bool(profile[1]),
                "health_status": profile[2],
            },
            "targets": results,
        }
    finally:
        connection.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live",
        action="store_true",
        help="explicitly allow network calls to AKShare upstream endpoints",
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="optional acceptance database; default is an isolated temporary file",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="optional path for the JSON acceptance report",
    )
    args = parser.parse_args(argv)
    if not args.live:
        print(
            json.dumps(
                {
                    "report_version": "akshare-cn-live-v1",
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
            with tempfile.TemporaryDirectory(prefix="akshare-cn-live-") as directory:
                report = run_live(Path(directory) / "acceptance.sqlite3")
        rendered = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            args.output.resolve().write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 0 if report["passed"] else 1
    except Exception as exc:
        error = exc.to_dict() if hasattr(exc, "to_dict") else {
            "type": type(exc).__name__,
            "message": str(exc),
        }
        print(
            json.dumps(
                {
                    "report_version": "akshare-cn-live-v1",
                    "passed": False,
                    "error": error,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

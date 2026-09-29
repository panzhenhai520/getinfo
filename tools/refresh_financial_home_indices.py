#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Refresh the five fixed homepage indices through approved provider chains.

The report intentionally excludes credentials, raw response bodies and full
exception messages.  It is safe to use against the existing WAL database.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from financial_feed import _snapshot_values
from financial_market_clock import MarketClockService, RequestTimeContext
from financial_market_scheduler import (
    FIXED_HOME_INDEX_SCOPES,
    FinancialMarketJobService,
    FinancialMarketScheduler,
)
from intel_database import IntelRepository
from sqlite_database import SQLiteDatabase


UTC = timezone.utc


class _ExecutionContext:
    @staticmethod
    def raise_if_cancelled() -> None:
        return None


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _error_summary(error: BaseException) -> dict:
    code = getattr(error, "code", None)
    if hasattr(code, "value"):
        code = code.value
    return {
        "exception_type": type(error).__name__,
        "error_code": str(code or getattr(error, "error_code", "refresh_failed"))[:100],
    }


def run(database_path: Path) -> dict:
    database = SQLiteDatabase(str(database_path))
    if not database.connect():
        raise RuntimeError("database_connection_failed")
    try:
        repository = IntelRepository(database)
        service = FinancialMarketJobService(repository, settings=config)
        market_clock = MarketClockService()
        now = datetime.now(UTC)
        request_context = RequestTimeContext(
            server_now_utc=now,
            server_timezone="Asia/Hong_Kong",
            user_timezone="Asia/Hong_Kong",
        )
        results = []
        for position, scope in enumerate(FIXED_HOME_INDEX_SCOPES, start=1):
            instrument = service.instruments.get_by_canonical_symbol(
                scope.canonical_symbol
            )
            if instrument is None:
                results.append({
                    "canonical_symbol": scope.canonical_symbol,
                    "status": "unavailable",
                    "exception_type": "MissingControlledInstrument",
                    "error_code": "controlled_index_seed_missing",
                })
                continue
            session = market_clock.market_state(scope.calendar_id, request_context)
            phase, window = FinancialMarketScheduler._query_phase_and_window(
                session, now
            )
            identity = f"home-index-refresh-{now.strftime('%Y%m%dT%H%M%SZ')}-{position}"
            payload = {
                "canonical_symbol": scope.canonical_symbol,
                "phase": phase,
                "schedule_window": f"manual:{scope.calendar_id}:{window}:{identity}",
                "market_session": session.to_dict(),
                "requested_at_utc": _utc_text(now),
                "research_run_id": identity,
                "request_id": f"request-{identity}",
                "trigger": "manual_home_index_refresh",
                "provider_chain": ["yahoo"],
            }
            try:
                outcome = service.run_snapshot(payload, _ExecutionContext())
            except BaseException as error:
                results.append({
                    "canonical_symbol": scope.canonical_symbol,
                    "display_name": instrument.display_name,
                    "status": "unavailable",
                    **_error_summary(error),
                })
                continue
            snapshot_ids = [int(value) for value in outcome.get("snapshot_ids") or []]
            if not snapshot_ids:
                results.append({
                    "canonical_symbol": scope.canonical_symbol,
                    "display_name": instrument.display_name,
                    "status": "unavailable",
                    "exception_type": "FinancialProviderError",
                    "error_code": str(outcome.get("error_code") or "no_snapshot_persisted"),
                })
                continue
            placeholders = ",".join("?" for _ in snapshot_ids)
            row = database.connection.execute(
                f"""
                SELECT s.id, s.observed_at, s.currency, s.quality_status,
                       s.payload_json, p.provider_key, p.display_name
                FROM financial_data_snapshots s
                JOIN financial_provider_profiles p ON p.id=s.provider_profile_id
                WHERE s.id IN ({placeholders})
                ORDER BY datetime(s.observed_at) DESC, s.id DESC LIMIT 1
                """,
                tuple(snapshot_ids),
            ).fetchone()
            stored = json.loads(str(row[4]))
            results.append({
                "canonical_symbol": scope.canonical_symbol,
                "display_name": instrument.display_name,
                "status": "ready" if _snapshot_values(stored) else "unavailable",
                "snapshot_id": int(row[0]),
                "observed_at": str(row[1]),
                "currency": str(row[2] or instrument.currency),
                "quality_status": str(row[3]),
                "values": _snapshot_values(stored),
                "provider_key": str(row[5]),
                "provider_name": str(row[6]),
                "exception_type": "",
                "error_code": "",
            })
        return {
            "check_version": "financial-home-index-refresh-v1",
            "passed": len(results) == 5 and all(item["status"] == "ready" for item in results),
            "database": str(database_path),
            "requested_at_utc": _utc_text(now),
            "credentials_reported": False,
            "raw_response_bodies_reported": False,
            "results": results,
        }
    finally:
        database.disconnect()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=str(ROOT / "data" / "crawler_articles.db"))
    args = parser.parse_args(argv)
    report = run(Path(args.database).expanduser().resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

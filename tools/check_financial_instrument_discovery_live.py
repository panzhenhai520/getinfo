#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Read-only A/HK/US instrument-discovery smoke using an isolated database."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from financial_instrument_discovery import FinancialInstrumentDiscoveryService
from financial_instrument_sources import (
    AKShareAInstrumentDiscoverySource,
    AKShareHKInstrumentDiscoverySource,
    AlphaVantageInstrumentDiscoverySource,
    HKEXInstrumentDiscoverySource,
    NasdaqTraderInstrumentDiscoverySource,
    SSEInstrumentDiscoverySource,
    SZSEInstrumentDiscoverySource,
)
from financial_schema import ensure_financial_tables
from intel_http import SafeHTTPClient


UTC = timezone.utc
SAMPLES = (
    ("CN", "601919.SH 股票最新信息", "601919.SH", "akshare_cn"),
    ("HK", "3119.HK 基金走势", "3119.HK", "akshare_cn"),
    ("US", "NUVB.US 股票怎么样", "NUVB.US", "alpha_vantage"),
)


def _endpoint(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    return f"{parsed.hostname or 'unknown'}{parsed.path or '/'}"


def _failure_summary(error: Exception) -> str:
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code is not None:
        return f"http_status_{int(status_code)}"
    reason = str(getattr(error, "reason", "") or "").strip()
    if reason and all(character.isalnum() or character in "_-" for character in reason):
        return reason[:80]
    name = type(error).__name__.casefold()
    if "timeout" in name:
        return "source_timeout"
    if "json" in name or isinstance(error, (json.JSONDecodeError, UnicodeError)):
        return "response_decode_failed"
    if isinstance(error, PermissionError):
        return "source_authorization_denied"
    if isinstance(error, ValueError):
        return "response_or_url_validation_failed"
    return "source_call_failed"


class RecordingHTTPClient:
    """Capture endpoint-level outcomes without retaining query strings or bodies."""

    def __init__(self, source_key: str, events: list[dict]):
        self.source_key = source_key
        self.events = events
        self.http = SafeHTTPClient()

    def get(self, url: str, *, headers=None):
        endpoint = _endpoint(url)
        try:
            result = self.http.get(url, headers=headers)
        except Exception as error:
            self.events.append(
                {
                    "source": self.source_key,
                    "endpoint": endpoint,
                    "status": "failed",
                    "exception_type": type(error).__name__,
                    "summary": _failure_summary(error),
                }
            )
            raise
        self.events.append(
            {
                "source": self.source_key,
                "endpoint": endpoint,
                "status": "ok",
                "status_code": int(result.status_code),
            }
        )
        return result


def _source(source_type, source_key: str, events: list[dict], **kwargs):
    return source_type(
        http_client=RecordingHTTPClient(source_key, events),
        **kwargs,
    )


def _sources(market: str, events: list[dict]) -> tuple[object, ...]:
    if market == "CN":
        return (
            _source(SSEInstrumentDiscoverySource, "sse", events),
            _source(SZSEInstrumentDiscoverySource, "szse", events),
            _source(
                AKShareAInstrumentDiscoverySource,
                "akshare_a_identity",
                events,
                settings=config,
            ),
        )
    if market == "HK":
        return (
            _source(HKEXInstrumentDiscoverySource, "hkex", events),
            _source(
                AKShareHKInstrumentDiscoverySource,
                "akshare_hk_identity",
                events,
                settings=config,
            ),
        )
    return (
        _source(NasdaqTraderInstrumentDiscoverySource, "nasdaq_trader", events),
        _source(
            AlphaVantageInstrumentDiscoverySource,
            "alpha_vantage",
            events,
            settings=config,
        ),
    )


def run_live(database_path: Path) -> dict:
    connection = sqlite3.connect(database_path, isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    source_calls: list[dict] = []
    results = []
    try:
        ensure_financial_tables(connection.cursor())
        for market, question, expected_symbol, expected_provider in SAMPLES:
            before = connection.execute(
                "SELECT COUNT(*) FROM financial_instruments"
            ).fetchone()[0]
            service = FinancialInstrumentDiscoveryService(
                connection,
                sources=_sources(market, source_calls),
                settings=config,
            )
            try:
                result = service.discover_and_promote(
                    question,
                    requested_at=datetime.now(UTC),
                    request_id=f"live-smoke-{market.casefold()}",
                )
            except Exception as error:
                after = connection.execute(
                    "SELECT COUNT(*) FROM financial_instruments"
                ).fetchone()[0]
                results.append(
                    {
                        "market": market,
                        "status": "error",
                        "canonical_symbol": "",
                        "provider_keys": [],
                        "registry_write_delta": after - before,
                        "reason_codes": ["source_or_admission_exception"],
                        "attempted_sources": [],
                        "failed_sources": [],
                        "exception_type": type(error).__name__,
                        "summary": _failure_summary(error),
                        "passed": False,
                    }
                )
                continue
            after = connection.execute(
                "SELECT COUNT(*) FROM financial_instruments"
            ).fetchone()[0]
            target = result.get("promoted_target") or {}
            provider_keys = sorted((target.get("provider_mappings") or {}).keys())
            passed = (
                result.get("status") == "promoted"
                and target.get("canonical_symbol") == expected_symbol
                and expected_provider in provider_keys
                and after - before == 1
            )
            results.append(
                {
                    "market": market,
                    "status": str(result.get("status") or ""),
                    "canonical_symbol": str(target.get("canonical_symbol") or ""),
                    "provider_keys": provider_keys,
                    "registry_write_delta": after - before,
                    "reason_codes": list(result.get("reason_codes") or []),
                    "attempted_sources": list(result.get("attempted_sources") or []),
                    "failed_sources": list(result.get("failed_sources") or []),
                    "passed": passed,
                }
            )
        return {
            "report_version": "financial-instrument-discovery-live-v1",
            "passed": all(item["passed"] for item in results),
            "database_scope": "isolated_temporary_deleted_after_run",
            "network_policy": "read_only_structured_sources",
            "response_bodies_retained": False,
            "credential_values_reported": False,
            "results": results,
            "source_calls": source_calls,
        }
    finally:
        connection.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live",
        action="store_true",
        help="explicitly allow read-only calls to structured financial sources",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not args.live:
        report = {
            "report_version": "financial-instrument-discovery-live-v1",
            "passed": False,
            "skipped": True,
            "reason": "network_disabled_without_explicit_live_flag",
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    try:
        with tempfile.TemporaryDirectory(
            prefix="financial-instrument-discovery-live-"
        ) as directory:
            report = run_live(Path(directory) / "acceptance.sqlite3")
    except Exception as error:
        report = {
            "report_version": "financial-instrument-discovery-live-v1",
            "passed": False,
            "error": {
                "exception_type": type(error).__name__,
                "summary": _failure_summary(error),
            },
        }
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.resolve().write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())

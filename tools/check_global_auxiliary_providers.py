#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Sanitized acceptance probe for Stage 2.12 auxiliary providers."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config
from financial_config import financial_capabilities
from financial_instruments import InstrumentRegistry
from financial_provider_router import provider_policy_summary
from financial_providers import (
    AlphaVantageProvider,
    EasyQuotationProvider,
    FREDProvider,
    OfficialEvidenceProvider,
    PolymarketProvider,
    YahooFinanceProvider,
)
from financial_schema import ensure_financial_tables


PROVIDER_IDS = (
    "yahoo",
    "alpha_vantage",
    "fred",
    "polymarket",
    "easyquotation",
    "official_evidence",
)
SETTING_NAMES = (
    "FINANCIAL_INTELLIGENCE_ENABLED",
    "YAHOO_FINANCE_ENABLED",
    "ALPHA_VANTAGE_ENABLED",
    "ALPHA_VANTAGE_API_KEY",
    "ALPHA_VANTAGE_REALTIME_ENTITLED",
    "FRED_ENABLED",
    "FRED_API_KEY",
    "POLYMARKET_ENABLED",
    "EASYQUOTATION_ENABLED",
    "OFFICIAL_FINANCIAL_EVIDENCE_ENABLED",
    "FINANCIAL_PROVIDER_TIMEOUT_SECONDS",
    "FINANCIAL_PROVIDER_DAILY_CALL_BUDGET",
    "FINANCIAL_NEWS_FRESHNESS_SECONDS",
)


def _settings():
    return {name: getattr(config, name, "") for name in SETTING_NAMES}


def _utc_text(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _offline_acceptance():
    summary = provider_policy_summary()
    providers = summary["providers"]
    checks = {
        "all_profiles_registered": all(item in providers for item in PROVIDER_IDS),
        "all_default_disabled": all(
            providers[item]["default_enabled"] is False for item in PROVIDER_IDS
        ),
        "all_have_license_profile": all(
            bool(providers[item]["license_profile"]) for item in PROVIDER_IDS
        ),
        "all_have_daily_budget": all(
            int(providers[item]["daily_call_budget"]) >= 0 for item in PROVIDER_IDS
        ),
        "all_have_fallback_rank": all(
            int(providers[item]["fallback_rank"]) >= 0 for item in PROVIDER_IDS
        ),
        "sdk_packages_exact_and_hashed": all(
            bool(PROJECT_ROOT.joinpath(
                "config", "financial_providers", f"{item}.json"
            ).is_file())
            for item in ("yahoo", "easyquotation")
        ) and all(
            bool(profile.get("package_wheel_sha256"))
            and bool(profile.get("package_version"))
            and not any(
                character in str(profile.get("package_version"))
                for character in "<>=~*"
            )
            for profile in (
                json.loads(
                    PROJECT_ROOT.joinpath(
                        "config", "financial_providers", f"{item}.json"
                    ).read_text(encoding="utf-8")
                )
                for item in ("yahoo", "easyquotation")
            )
        ),
        "unauthorized_social_sources_hard_disabled": set(summary["hard_disabled"])
        == {"reddit", "stocktwits"},
    }
    return {"passed": all(checks.values()), "checks": checks, "policy": summary}


def _live_acceptance(now):
    settings = _settings()
    state = financial_capabilities(settings)
    enabled = [item for item in PROVIDER_IDS if state["effective"].get(item)]
    if not enabled:
        return {
            "requested": True,
            "skipped": True,
            "reason": "no_auxiliary_provider_effectively_enabled",
            "enabled_provider_ids": [],
            "results": {},
        }
    database_path = Path(str(config.DATABASE_PATH)).expanduser().resolve()
    connection = sqlite3.connect(database_path, isolation_level=None, timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        ensure_financial_tables(connection.cursor())
        registry = InstrumentRegistry(connection)
        registry.load_controlled_seed()
        factories = {
            "yahoo": YahooFinanceProvider,
            "alpha_vantage": AlphaVantageProvider,
            "fred": FREDProvider,
            "polymarket": PolymarketProvider,
            "easyquotation": EasyQuotationProvider,
            "official_evidence": OfficialEvidenceProvider,
        }
        results = {}
        for provider_id in enabled:
            provider = factories[provider_id](
                instrument_registry=registry,
                settings=settings,
                connection=connection,
                clock=lambda: now,
            )
            result = provider.health_probe(
                request_id=f"stage212-live-{provider_id}", requested_at=now
            )
            results[provider_id] = result
        return {
            "requested": True,
            "skipped": False,
            "reason": "",
            "enabled_provider_ids": enabled,
            "results": results,
        }
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "architecture" / "global-auxiliary-provider-acceptance.json"),
    )
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    offline = _offline_acceptance()
    live = (
        _live_acceptance(now)
        if args.live
        else {
            "requested": False,
            "skipped": True,
            "reason": "live_network_probe_requires_explicit_live_flag",
            "enabled_provider_ids": [],
            "results": {},
        }
    )
    unhealthy = {
        provider_id: result["status"]
        for provider_id, result in live.get("results", {}).items()
        if result.get("status") in {"unhealthy", "permission_denied"}
    }
    report = {
        "report_version": "global-auxiliary-providers-v1",
        "generated_at": _utc_text(now),
        "passed": bool(offline["passed"] and not unhealthy),
        "offline_acceptance": offline,
        "live_acceptance": live,
        "unhealthy_enabled_providers": unhealthy,
        "secrets_included": False,
    }
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

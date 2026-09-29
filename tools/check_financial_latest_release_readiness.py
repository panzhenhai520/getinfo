#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Offline release-readiness and non-destructive rollback rehearsal."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from financial_instruments import InstrumentRegistry
from financial_latest_observability import METRIC_DEFINITIONS
from financial_recovery import (
    database_fingerprint,
    recovery_decision,
    verify_legal_data_preserved,
)
from sqlite_database import SQLiteDatabase
from tools.check_financial_latest_information import DEFAULT_GOLDEN, build_report


RELEASE_READINESS_VERSION = "financial-latest-release-readiness-v1"
PROTECTED_TABLES = (
    "financial_instrument_candidates",
    "financial_instruments",
    "financial_data_snapshots",
    "articles",
    "chat_financial_routes",
)
CHILD_SWITCHES = (
    "FINANCIAL_INFORMATION_NEEDS_ENABLED",
    "FINANCIAL_INSTRUMENT_DISCOVERY_ENABLED",
    "FINANCIAL_INSTRUMENT_AUTO_PROMOTION_ENABLED",
    "FINANCIAL_LATEST_NEWS_ENABLED",
    "FINANCIAL_LATEST_BUNDLE_ENABLED",
)
ROLLOUT_SEQUENCE = (
    "information_needs_only",
    "candidate_discovery_without_auto_promotion",
    "internal_auto_promotion",
    "quote_only",
    "news_only",
    "quote_and_news_bundle",
)


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def migration_rehearsal() -> dict:
    with tempfile.TemporaryDirectory() as directory:
        database = SQLiteDatabase(str(Path(directory) / "release-rehearsal.sqlite3"))
        _assert(database.connect(), "temporary database connect failed")
        try:
            _assert(database.create_tables(), "first schema initialization failed")
            registry = InstrumentRegistry(database.connection)
            registry.load_controlled_seed()
            database.connection.execute(
                "INSERT INTO articles(url,title,status) "
                "VALUES('https://fixture.invalid/preserved','preserved fixture','active')"
            )
            database.connection.commit()
            before = database_fingerprint(
                database.connection, protected_tables=PROTECTED_TABLES
            )

            _assert(database.create_tables(), "repeated schema initialization failed")
            registry.load_controlled_seed()
            database.connection.commit()
            after = database_fingerprint(
                database.connection, protected_tables=PROTECTED_TABLES
            )
            preservation = verify_legal_data_preserved(before, after)
            checks = {
                "integrity_ok": before["integrity_ok"] and after["integrity_ok"],
                "schema_hash_stable": before["schema_sha256"] == after["schema_sha256"],
                "protected_row_counts_stable": (
                    before["protected_row_counts"] == after["protected_row_counts"]
                ),
                "legal_data_preserved": bool(preservation["passed"]),
            }
            _assert(all(checks.values()), checks)
            return {
                "passed": True,
                "checks": checks,
                "schema_sha256": after["schema_sha256"],
                "protected_row_counts": after["protected_row_counts"],
            }
        finally:
            database.disconnect()


def rollback_rehearsal() -> dict:
    decisions = {
        fault: recovery_decision(fault, database_integrity_ok=True)
        for fault in ("provider_failure", "llm_failure", "schema_interruption")
    }
    checks = {
        "all_child_switches_declared": all(
            switch in (ROOT / "config.py").read_text(encoding="utf-8")
            for switch in CHILD_SWITCHES
        ),
        "rollback_preserves_database": all(
            item["database_action"] == "preserve_current_database"
            and item["database_restore_allowed"] is False
            for item in decisions.values()
        ),
        "rollout_sequence_is_ordered": len(ROLLOUT_SEQUENCE) == len(set(ROLLOUT_SEQUENCE)),
        "all_core_metrics_declared": {
            item[0] for item in METRIC_DEFINITIONS
        } == {
            "financial_information_needs_total",
            "financial_instrument_discovery_total",
            "financial_instrument_promotion_total",
            "financial_latest_channel_latency_ms",
            "financial_latest_bundle_total",
            "financial_future_evidence_rejected_total",
            "financial_generic_model_blocked_total",
            "financial_timezone_fallback_total",
        },
    }
    _assert(all(checks.values()), checks)
    return {
        "passed": True,
        "checks": checks,
        "child_switches": list(CHILD_SWITCHES),
        "protected_tables": list(PROTECTED_TABLES),
        "fault_decisions": decisions,
        "destructive_database_action_executed": False,
    }


def build_release_report(*, golden_path: Path) -> dict:
    latest_gate = build_report(runtime=True, golden_path=golden_path)
    _assert(latest_gate["acceptance"] == "passed", latest_gate)
    return {
        "acceptance": "passed",
        "acceptance_version": RELEASE_READINESS_VERSION,
        "task": "latest-information-offline-release-readiness",
        "network_policy": "none",
        "latest_information_gate": latest_gate,
        "migration_rehearsal": migration_rehearsal(),
        "rollback_rehearsal": rollback_rehearsal(),
        "rollout": {
            "sequence": list(ROLLOUT_SEQUENCE),
            "production_changes_executed": False,
            "production_authorization": "not_granted",
            "next_action": "explicit_operator_authorization_required",
        },
        "boundaries": {
            "read_only_observability": True,
            "feature_flag_rollback_only": True,
            "historical_rows_preserved": True,
            "database_restore_executed": False,
            "live_provider_smoke_executed": False,
        },
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--network", choices=("none",), required=True)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    fixture = args.fixture.expanduser().resolve()
    if not fixture.is_file():
        parser.error(f"fixture not found: {fixture}")
    report = build_release_report(golden_path=fixture)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

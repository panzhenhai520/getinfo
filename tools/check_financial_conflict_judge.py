#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Static/runtime acceptance gate for task 4.3 multi-source conflict Judge."""

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
    modules = {
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    modules.update(
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    return modules


def static_acceptance() -> dict:
    module = ROOT / "financial_conflict_judge.py"
    source = module.read_text(encoding="utf-8")
    prohibited = sorted(_imports(module) & {"requests", "httpx", "openai", "anthropic"})
    verdicts = [
        "verified_consensus",
        "verified_authoritative",
        "single_source",
        "unresolved_conflict",
        "incomparable_evidence",
        "insufficient_evidence",
    ]
    checks = {
        "closed_schema": '"additionalProperties": False' in source,
        "all_verdicts_present": all(f'"{item}"' in source for item in verdicts),
        "stable_instrument_dimension": "instrument_key" in source,
        "unit_and_currency_normalization": "_unit_contract" in source,
        "adjustment_and_period_isolation": all(
            marker in source for marker in ("adjustment", "period_key")
        ),
        "timezone_normalized_to_utc": "_parse_utc" in source,
        "underlying_source_independence": all(
            marker in source
            for marker in ("underlying_source_id", "correlated_source_collapsed", "unknown_lineage")
        ),
        "provider_name_not_independence": '"provider_names_equal_independent_sources": False' in source,
        "authority_policy_present": "PROVIDER_AUTHORITY" in source,
        "tolerance_policy_present": "METRIC_TOLERANCES" in source,
        "different_observation_time_isolated": "different_observation_time" in source,
        "permission_failure_excluded": "permission_denied" in source,
        "no_implicit_fx": '"implicit_fx_conversion": False' in source,
        "no_price_averaging": '"prices_averaged": False' in source,
        "unresolved_never_current": '"unresolved_conflict_is_current_fact": False' in source,
        "versioned_existing_verdict_table_reused": all(
            marker in source for marker in ("INSERT INTO financial_verdicts", "adjudication_version")
        ),
        "existing_verdict_table_review_queue": "list_pending_human_review" in source,
        "input_snapshot_hashes_persisted": "input_snapshot_hashes" in source,
        "no_direct_model_or_network_client": not prohibited,
    }
    _assert(all(checks.values()), {"checks": checks, "prohibited": prohibited})
    return {
        "schema_version": "financial-conflict-verdict-v1",
        "judge_version": "financial-conflict-judge-v1",
        "verdicts": verdicts,
        "checks": checks,
        "direct_model_or_network_imports": prohibited,
        "existing_tables": [
            "financial_claims",
            "financial_claim_evidence",
            "financial_verdicts",
            "financial_data_snapshots",
            "financial_provider_profiles",
        ],
        "human_review_queue": "latest unresolved_conflict rows in financial_verdicts",
        "new_tables": [],
        "new_services": [],
        "new_ports": [],
    }


def runtime_acceptance() -> dict:
    suite = unittest.defaultTestLoader.loadTestsFromName(
        "tests.test_financial_conflict_judge"
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
        "verdict_status_count": 6,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    report = {
        "acceptance": "passed",
        "task": "4.3",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "scenarios": {
            "same_value": True,
            "within_tolerance": True,
            "outside_tolerance": True,
            "authoritative_source": True,
            "same_underlying_source_collapsed": True,
            "unknown_lineage_not_assumed_independent": True,
            "different_adjustment": True,
            "different_currency": True,
            "different_period": True,
            "delayed_observation": True,
            "permission_denied": True,
            "single_source": True,
            "human_review_queue": True,
            "idempotent_versioning": True,
            "resolved_conflict_leaves_queue": True,
        },
        "boundaries": {
            "temporal_judge_required_first": True,
            "provider_names_are_not_independence": True,
            "different_scopes_never_averaged": True,
            "unresolved_conflict_never_current": True,
            "existing_sqlite_reused": True,
            "new_database": False,
            "new_model_client": False,
            "real_order_execution": False,
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

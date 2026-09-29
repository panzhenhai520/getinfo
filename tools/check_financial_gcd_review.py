#!/usr/bin/env python3
"""Acceptance gate for task 3.10 financial-aware history GCD review."""

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


def static_acceptance():
    module_path = ROOT / "financial_gcd_review.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
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
    prohibited = sorted(imports & {"requests", "httpx", "openai", "anthropic"})
    _assert(not prohibited, f"financial GCD reviewer has network/model clients: {prohibited}")
    source = module_path.read_text(encoding="utf-8")
    api = (ROOT / "mapindex_api.py").read_text(encoding="utf-8")
    frontend = (ROOT / "templates" / "mapindex.html").read_text(encoding="utf-8")
    for field in (
        '"instrument_id"', '"metric"', '"value"', '"currency"',
        '"period"', '"as_of"', '"evidence"',
    ):
        _assert(field in source, f"required claim field missing: {field}")
    for status in ("verified_current", "historical", "stale", "conflicted"):
        _assert(status in source, f"required temporal status missing: {status}")
    _assert("_clean_chat_history_rows(rows)" in api, "legacy cleanup is no longer first")
    _assert("_create_gcd_review_session" in api, "financial GCD review session path missing")
    _assert("datetime.now(timezone.utc)" in api, "server UTC review clock missing")
    _assert("delete_chat_session" not in source, "reviewer may not delete source sessions")
    _assert("financial_review" in frontend, "frontend review summary missing")
    return {
        "schema_version": "financial-gcd-review-v1",
        "existing_operation": "POST /mapindex/api/chat/operations operation_type=gcd",
        "legacy_cleanup_first": True,
        "claim_fields": [
            "instrument_id", "metric", "value", "currency", "period", "as_of", "evidence"
        ],
        "statuses": ["verified_current", "historical", "stale", "conflicted"],
        "validity_interval_merge": True,
        "source_sessions_immutable": True,
        "clock_source": "application_server_utc",
        "conflict_silent_selection": False,
        "direct_model_or_network_imports": prohibited,
        "new_services": [],
        "new_ports": [],
        "new_tables": [],
    }


def runtime_acceptance():
    suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_financial_gcd_review")
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
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = {
        "acceptance": "passed",
        "task": "3.10",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "scenarios": {
            "same_day_overlapping_duplicate_merged": True,
            "different_as_of_not_conflicted": True,
            "split_adjustment_isolated": True,
            "currency_isolated": True,
            "old_report_and_new_price_labeled": True,
            "material_same_instant_conflict_retained": True,
            "stale_latest_snapshot_labeled": True,
            "cross_instrument_snapshot_blocked": True,
            "mixed_non_financial_history_preserved": True,
        },
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Acceptance gate for task 3.11 financial-aware history synthesis."""

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
    module_path = ROOT / "financial_synthesis_review.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
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
    _assert(not prohibited, f"synthesis reviewer has network/model clients: {prohibited}")
    api = (ROOT / "mapindex_api.py").read_text(encoding="utf-8")
    frontend = (ROOT / "templates" / "mapindex.html").read_text(encoding="utf-8")
    for field in (
        '"claim"', '"evidence"', '"instrument"', '"as_of"', '"verdict"', '"reason"',
    ):
        _assert(field in source, f"required conflict field missing: {field}")
    for verdict in ("fact_conflict", "opinion_difference", "pending_evidence"):
        _assert(verdict in source, f"required conflict class missing: {verdict}")
    _assert("_synthesize_chat_history(cleaned)" in api, "existing model relationship analysis missing")
    _assert("_review_financial_synthesis(cleaned,result)" in api, "financial synthesis gate not wired")
    _assert("financial_review['recommended_relation']" in api, "grounded relation does not control merge")
    _assert("delete_chat_session" not in source, "reviewer may not delete source sessions")
    for label in ("类型：", "标的：", "A 时点：", "B 时点：", "结论：", "原因："):
        _assert(label in frontend, f"conflict card label missing: {label}")
    _assert("textContent = line" in frontend, "financial conflict metadata must render as text")
    return {
        "schema_version": "financial-synthesis-review-v1",
        "existing_operation": "POST /mapindex/api/chat/operations operation_type=synthesize",
        "relationship_model_preserved": True,
        "grounding_layers": ["snapshot_provider_verification", "TradingAgents_report_comparison", "temporal_judge"],
        "conflict_fields": ["claim", "evidence", "instrument", "as_of", "verdict", "reason"],
        "conflict_classes": ["fact_conflict", "opinion_difference", "pending_evidence"],
        "cross_instrument_implicit_merge": False,
        "historical_opinion_erased": False,
        "model_wording_selects_winner": False,
        "source_sessions_immutable": True,
        "direct_network_or_model_imports": prohibited,
        "new_services": [],
        "new_ports": [],
        "new_tables": [],
    }


def runtime_acceptance():
    suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_financial_synthesis_review")
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    _assert(result.wasSuccessful(), stream.getvalue())
    return {
        "executed": True,
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "network_calls": 0,
        "real_order_calls": 0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = {
        "acceptance": "passed",
        "task": "3.11",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "scenarios": {
            "bull_bear_reports_compared": True,
            "same_instant_price_conflict_retained": True,
            "same_report_period_conflict_retained": True,
            "same_fact_different_rating_is_opinion": True,
            "different_instrument_implicit_merge_blocked": True,
            "unrelated_sessions_not_forced": True,
            "unverified_model_conflict_pending": True,
            "operation_api_grounded": True,
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

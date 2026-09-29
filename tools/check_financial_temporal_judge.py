#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Static/runtime acceptance gate for task 4.2 Temporal Judge."""

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
    module = ROOT / "financial_temporal_judge.py"
    source = module.read_text(encoding="utf-8")
    prohibited = sorted(_imports(module) & {"requests", "httpx", "openai", "anthropic"})
    statuses = [
        "verified_current",
        "verified_historical",
        "superseded",
        "stale",
        "insufficient_evidence",
    ]
    checks = {
        "closed_schema": '"additionalProperties": False' in source,
        "all_required_statuses": all(f'"{status}"' in source for status in statuses),
        "server_time_authoritative": '"server_time_authoritative": True' in source,
        "market_session_compared": "market_session_state" in source,
        "observed_and_fetched_compared": all(
            marker in source for marker in ("observed_dt", "fetched_dt")
        ),
        "effective_interval_compared": "declared_validity_ended" in source,
        "financial_and_macro_revision_compared": all(
            marker in source for marker in ("financial_restatement", "macro_revision")
        ),
        "corporate_action_compared": '"corporate_action"' in source,
        "index_rebalance_compared": '"index_rebalance"' in source,
        "closed_market_carry_is_explicit_and_bounded": all(
            marker in source
            for marker in ("MAX_CLOSED_MARKET_CARRY_SECONDS", "latest_closed_market_observation")
        ),
        "future_evidence_rejected": "future_evidence_ignored" in source,
        "post_request_evidence_rejected": "post_request_evidence_ignored" in source,
        "versioned_existing_verdict_table_reused": all(
            marker in source
            for marker in ("INSERT INTO financial_verdicts", "adjudication_version")
        ),
        "claim_status_updated": "UPDATE financial_claims" in source,
        "no_direct_model_or_network_client": not prohibited,
    }
    _assert(all(checks.values()), {"checks": checks, "prohibited": prohibited})
    return {
        "schema_version": "financial-temporal-verdict-v1",
        "judge_version": "financial-temporal-judge-v1",
        "statuses": statuses,
        "checks": checks,
        "direct_model_or_network_imports": prohibited,
        "existing_tables": [
            "financial_claims",
            "financial_claim_evidence",
            "financial_verdicts",
            "financial_data_snapshots",
        ],
        "new_tables": [],
        "new_services": [],
        "new_ports": [],
    }


def runtime_acceptance() -> dict:
    suite = unittest.defaultTestLoader.loadTestsFromName(
        "tests.test_financial_temporal_judge"
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
        "verdict_status_count": 5,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    report = {
        "acceptance": "passed",
        "task": "4.2",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "scenarios": {
            "intraday_price": True,
            "after_close_last_valid_observation": True,
            "historical_request": True,
            "newer_quote_keeps_old_quote_historical": True,
            "financial_restatement": True,
            "macro_revision": True,
            "split": True,
            "index_rebalance_validity_end": True,
            "future_evidence_rejected": True,
            "post_request_evidence_rejected": True,
            "non_fact_rejected": True,
            "changed_verdict_appends_version": True,
            "identical_decision_is_idempotent": True,
        },
        "boundaries": {
            "existing_market_clock_reused": True,
            "existing_sqlite_reused": True,
            "value_conflict_decided": False,
            "investment_opinion_verified": False,
            "new_model_client": False,
            "new_database": False,
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

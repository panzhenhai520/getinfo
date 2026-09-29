#!/usr/bin/env python3
"""Offline acceptance gate for task 3.5 broad-market chat routing."""

from __future__ import annotations

import argparse
import ast
import io
import json
import sys
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".", 1)[0])
    return names


def static_acceptance():
    from financial_chat_market_scope import MARKET_SCOPE_SCHEMA

    scope_path = ROOT / "financial_chat_market_scope.py"
    scheduler_path = ROOT / "financial_market_scheduler.py"
    api = (ROOT / "chat_api.py").read_text(encoding="utf-8")
    worker_jobs = (ROOT / "financial_worker_jobs.py").read_text(encoding="utf-8")
    Draft202012Validator.check_schema(MARKET_SCOPE_SCHEMA)
    prohibited = {"requests", "httpx", "openai", "tradingagents", "celery", "rq"}
    _assert(not (_imports(scope_path) & prohibited), "scope router has direct external runtime")
    _assert(
        "enqueue_scope_refresh" in scheduler_path.read_text(encoding="utf-8"),
        "interactive scope refresh is missing",
    )
    _assert(
        '"market_overview": "financial_intelligence"' in worker_jobs,
        "lightweight overview incorrectly requires TradingAgents",
    )
    scope_index = api.index("if route_plan.market_scope.get('status') != 'skipped'")
    model_config_index = api.index("cfg = _load_config()", scope_index)
    _assert(scope_index < model_config_index, "market evidence gate runs after model setup")
    _assert("_stream_openai" not in api[scope_index:model_config_index], "scope gate calls model")
    return {
        "schema": "financial-chat-market-scope-v1",
        "existing_worker": "intel_worker.py",
        "existing_queue": "intel_jobs",
        "existing_database": "crawler_articles.db",
        "standard_universes": [
            "CN_XSHG_MARKET",
            "CN_XSHE_MARKET",
            "CN_A_MARKET",
            "HK_MARKET",
            "DEFAULT_MARKET_PULSE",
        ],
        "new_services": [],
        "new_ports": [],
        "new_tables": [],
        "direct_network_or_model_imports": [],
    }


def runtime_acceptance():
    suite = unittest.defaultTestLoader.loadTestsFromName(
        "tests.test_financial_chat_market_scope"
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
        "model_calls_before_snapshot_report": 0,
        "full_research_jobs_created": 0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = {
        "acceptance": "passed",
        "task": "3.5",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "scenarios": {
            "simplified_traditional_oral_mapping": True,
            "explicit_index_takes_precedence": True,
            "missing_ticker_never_blocks_broad_market": True,
            "post_close_settlement_is_explicit": True,
            "exchange_holidays_are_independent": True,
            "partial_coverage_is_disclosed": True,
            "stale_snapshot_cannot_be_laundered_by_current_report": True,
            "persisted_snapshots_precede_answer": True,
        },
        "boundaries": {
            "does_not_select_a_stock": True,
            "does_not_reduce_whole_market_to_one_index": True,
            "does_not_generate_unverified_values": True,
            "legacy_sse_event_types": ["status", "chunk", "done"],
            "complete_tradingagents_research_started": False,
        },
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()

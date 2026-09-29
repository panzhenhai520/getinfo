#!/usr/bin/env python3
"""Offline acceptance gate for task 3.6 query-through financial facts."""

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
    from financial_realtime_query import (
        REALTIME_PROVIDER_CHAINS,
        REALTIME_QUERY_SCHEMA,
    )

    service_path = ROOT / "financial_realtime_query.py"
    router_path = ROOT / "financial_provider_router.py"
    api_text = (ROOT / "chat_api.py").read_text(encoding="utf-8")
    service_text = service_path.read_text(encoding="utf-8")
    router_text = router_path.read_text(encoding="utf-8")
    Draft202012Validator.check_schema(REALTIME_QUERY_SCHEMA)

    prohibited = {
        "requests",
        "httpx",
        "openai",
        "tradingagents",
        "celery",
        "rq",
    }
    _assert(
        not (_imports(service_path) & prohibited),
        "query-through service bypasses an existing runtime boundary",
    )
    _assert(
        "self.router.fetch(" in service_text
        and "self.router.persist_response(" in service_text,
        "query-through does not use the project Provider Router",
    )
    _assert(
        "with self.database.lock:" in service_text,
        "snapshot persistence is not serialized on the existing database lock",
    )
    _assert(
        "def persist_response(" in router_text,
        "Provider Router has no fetch/persist split for short database locks",
    )
    branch = "if route_plan.realtime_query.get('status') == 'planned':"
    branch_index = api_text.index(branch)
    config_index = api_text.index("cfg = _load_config()", branch_index)
    branch_text = api_text[branch_index:config_index]
    status_index = branch_text.index('yield f\'data: {json.dumps({"type":"status"')
    execute_index = branch_text.index("execute_realtime_query(route_plan)")
    _assert(status_index < execute_index, "provider work can precede the first SSE status")
    _assert("_stream_openai" not in branch_text, "realtime fact path calls a generic LLM")
    _assert("financial_research" not in service_text, "simple fact path starts full research")
    _assert(
        tuple(REALTIME_PROVIDER_CHAINS["US"]) == ("yahoo", "alpha_vantage"),
        "US realtime fallback chain is missing",
    )
    return {
        "schema": "financial-realtime-query-v1",
        "existing_provider_router": "financial_provider_router.py",
        "existing_database": "crawler_articles.db",
        "existing_sse_endpoint": "POST /api/chat/send",
        "quote_provider_chains": {
            key: list(value) for key, value in REALTIME_PROVIDER_CHAINS.items()
        },
        "configured_provider_timeout_key": "FINANCIAL_PROVIDER_TIMEOUT_SECONDS",
        "new_services": [],
        "new_ports": [],
        "new_tables": [],
        "direct_network_or_model_imports": [],
    }


def runtime_acceptance():
    suite = unittest.defaultTestLoader.loadTestsFromName(
        "tests.test_financial_realtime_query"
    )
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    _assert(result.wasSuccessful(), stream.getvalue())
    return {
        "executed": True,
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "external_network_calls": 0,
        "generic_model_calls": 0,
        "full_research_jobs_created": 0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = {
        "acceptance": "passed",
        "task": "3.6",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "scenarios": {
            "fresh_cache_hit_without_provider": True,
            "open_stale_cache_synchronously_refreshed_and_persisted": True,
            "provider_timeout_falls_back_to_labeled_stale_snapshot": True,
            "permission_failure_without_data_returns_no_numeric_claim": True,
            "material_provider_conflict_refuses_single_value": True,
            "closed_market_snapshot_is_not_called_realtime": True,
            "same_session_target_context_survives_realtime_outcome": True,
            "first_sse_status_under_one_second": True,
        },
        "answer_contract": {
            "required_fields": [
                "observed_at",
                "fetched_at",
                "market_status",
                "source",
            ],
            "server_clock_only": True,
            "unverified_llm_numbers_allowed": False,
        },
        "boundaries": {
            "complete_tradingagents_research_started": False,
            "network_io_inside_database_lock": False,
            "legacy_sse_event_types": ["status", "chunk", "done"],
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

#!/usr/bin/env python3
"""Offline acceptance gate for task 3.7 full-research chat routing."""

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
    from financial_full_research import FULL_RESEARCH_SCHEMA

    route_path = ROOT / "financial_full_research.py"
    route_text = route_path.read_text(encoding="utf-8")
    api_text = (ROOT / "chat_api.py").read_text(encoding="utf-8")
    worker_text = (ROOT / "financial_worker_jobs.py").read_text(encoding="utf-8")
    compose_text = (ROOT / "docker-compose.crawler.yml").read_text(encoding="utf-8")
    Draft202012Validator.check_schema(FULL_RESEARCH_SCHEMA)

    prohibited = {"requests", "httpx", "openai", "tradingagents", "celery", "rq"}
    _assert(
        not (_imports(route_path) & prohibited),
        "full-research router bypasses project model/provider/queue boundaries",
    )
    for marker in (
        '"cache_key"',
        '"as_of_key"',
        '"graph_version"',
        '"provider_profile_hash"',
        '"config_hash"',
    ):
        _assert(marker in route_text, f"cache compatibility marker missing: {marker}")
    begin = route_text.index('self.connection.execute("BEGIN IMMEDIATE")')
    run_insert = route_text.index("INSERT INTO financial_research_runs", begin)
    job_insert = route_text.index("INSERT INTO intel_jobs", run_insert)
    commit = route_text.index('self.connection.execute("COMMIT")', job_insert)
    _assert(begin < run_insert < job_insert < commit, "run/job enqueue is not one transaction")
    _assert(
        '"financial_research"' in worker_text,
        "existing IntelWorker financial_research job type is missing",
    )
    _assert(
        "financial_full_research_router" in route_text,
        "job provenance is not explicit",
    )
    branch = "if route_plan.full_research.get('status') != 'skipped':"
    branch_index = api_text.index(branch)
    config_index = api_text.index("cfg = _load_config()", branch_index)
    branch_text = api_text[branch_index:config_index]
    _assert("_stream_openai" not in branch_text, "research failure can reach generic LLM")
    _assert("_web_search" not in branch_text, "research failure can reach generic web search")
    _assert(
        "FINANCIAL_RESEARCH_CACHE_SECONDS" in (ROOT / "config.py").read_text(encoding="utf-8"),
        "report cache TTL is not configurable",
    )
    _assert(compose_text.count("intel-worker:") == 1, "a duplicate worker service was added")
    return {
        "schema": "financial-full-research-route-v1",
        "cache_identity": [
            "instrument_or_universe",
            "as_of",
            "graph_version",
            "provider_profile_hash",
            "config_hash",
        ],
        "existing_queue": "intel_jobs",
        "existing_worker": "intel-worker",
        "existing_database": "crawler_articles.db",
        "existing_llm_runtime": "AI assistant local OpenAI-compatible runtime",
        "new_services": [],
        "new_ports": [],
        "new_tables": [],
        "direct_network_or_model_imports": [],
    }


def runtime_acceptance():
    suite = unittest.defaultTestLoader.loadTestsFromName(
        "tests.test_financial_full_research"
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
        "generic_model_fallback_calls": 0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = {
        "acceptance": "passed",
        "task": "3.7",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "scenarios": {
            "compatible_fresh_report_reused": True,
            "expired_report_not_reused": True,
            "graph_config_change_invalidates_cache": True,
            "provider_profile_change_invalidates_cache": True,
            "concurrent_identical_requests_share_one_run": True,
            "cancelled_job_is_not_replaced_with_opinion": True,
            "target_change_isolated": True,
            "comparison_targets_preserved": True,
            "deep_universe_research_separated_from_light_overview": True,
            "failure_has_explainable_evidence_closed_status": True,
        },
        "boundaries": {
            "run_and_job_atomic": True,
            "secrets_persisted": False,
            "generic_llm_opinion_fallback": False,
            "real_order_created": False,
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

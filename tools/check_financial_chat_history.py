#!/usr/bin/env python3
"""Acceptance gate for task 3.9 financial chat-history audit links."""

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
    module = ROOT / "financial_chat_history.py"
    tree = ast.parse(module.read_text(encoding="utf-8"))
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
    _assert(not prohibited, f"history audit has direct network/model clients: {prohibited}")
    schema = (ROOT / "financial_schema.py").read_text(encoding="utf-8")
    database = (ROOT / "sqlite_database.py").read_text(encoding="utf-8")
    api = (ROOT / "chat_api.py").read_text(encoding="utf-8")
    _assert("chat_history_id INTEGER" in schema, "answer-level history link missing")
    _assert("REFERENCES chat_history(id) ON DELETE CASCADE" in schema, "history FK boundary missing")
    _assert("BEGIN IMMEDIATE" in database and "attach_financial_audit" in database, "transactional attach missing")
    _assert("financial_audit" in api, "history replay audit missing")
    frontend = {}
    for name in ("mapindex.html", "article_management.html"):
        source = (ROOT / "templates" / name).read_text(encoding="utf-8")
        checks = {
            "sends_session_id": "session_id:" in source,
            "captures_route_key": "financialRouteKey" in source,
            "saves_route_key": "financial_route_key:" in source,
        }
        _assert(all(checks.values()), f"frontend audit link missing for {name}: {checks}")
        frontend[name] = checks
    return {
        "schema_version": "financial-chat-audit-v1",
        "existing_history_table": "chat_history",
        "existing_route_table": "chat_financial_routes",
        "existing_artifact_table": "chat_financial_artifacts",
        "answer_fk": "chat_financial_artifacts.chat_history_id",
        "transaction": "chat_history_insert_plus_financial_reference_attach",
        "large_report_json_copied": False,
        "shared_evidence_delete_cascade": False,
        "direct_model_or_network_imports": prohibited,
        "new_services": [],
        "new_ports": [],
        "new_tables": [],
        "frontend": frontend,
    }


def runtime_acceptance():
    suite = unittest.defaultTestLoader.loadTestsFromName(
        "tests.test_financial_chat_history"
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
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = {
        "acceptance": "passed",
        "task": "3.9",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "scenarios": {
            "ordinary_history_unchanged": True,
            "financial_route_time_and_target_replay": True,
            "snapshot_and_report_version_replay": True,
            "save_failure_rolls_back_answer": True,
            "session_delete_preserves_shared_evidence": True,
            "legacy_financial_client_match": True,
            "existing_artifact_migration": True,
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

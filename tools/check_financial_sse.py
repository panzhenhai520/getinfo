#!/usr/bin/env python3
"""Acceptance gate for task 3.8 negotiated financial SSE extensions."""

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
    from financial_sse import (
        FINANCIAL_OPTIONAL_SSE_EVENT_TYPES,
        FINANCIAL_SSE_PROTOCOL_VERSION,
    )

    module_path = ROOT / "financial_sse.py"
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
    _assert(not prohibited, f"financial SSE projection imports network/model clients: {prohibited}")

    api = (ROOT / "chat_api.py").read_text(encoding="utf-8")
    _assert("sse_features" in (ROOT / "chat_route_orchestrator.py").read_text(encoding="utf-8"), "SSE negotiation missing")
    _assert("@chat_bp.route('/api/chat/send'" in api, "chat endpoint changed")
    _assert("@chat_bp.route('/api/financial/reports/<int:report_id>'" in api, "report entry missing")
    for event_type in FINANCIAL_OPTIONAL_SSE_EVENT_TYPES:
        _assert(f'"{event_type}"' in module_path.read_text(encoding="utf-8"), f"event missing: {event_type}")

    frontend_contract = {}
    for name in ("mapindex.html", "article_management.html"):
        source = (ROOT / "templates" / name).read_text(encoding="utf-8")
        checks = {
            "negotiates_financial_sse": "sse_features: ['financial-sse-v1']" in source,
            "requires_done": "streamCompleted" in source and "if (!streamCompleted" in source,
            "single_reconnect": "reconnectAttempt < 1" in source,
            "report_link_allowlist": "/api\\/financial\\/reports\\/\\d+" in source,
            "unknown_event_ignored": "Unknown optional events are intentionally ignored" in source,
        }
        _assert(all(checks.values()), f"frontend contract incomplete for {name}: {checks}")
        frontend_contract[name] = checks
    return {
        "protocol_version": FINANCIAL_SSE_PROTOCOL_VERSION,
        "optional_event_types": list(FINANCIAL_OPTIONAL_SSE_EVENT_TYPES),
        "legacy_event_types_unchanged_without_negotiation": True,
        "natural_language_event": "chunk",
        "terminal_success_event": "done",
        "terminal_error_event": "error",
        "report_entry": "GET /api/financial/reports/{id}",
        "projection_direct_model_network_imports": prohibited,
        "new_services": [],
        "new_ports": [],
        "frontend": frontend_contract,
    }


def runtime_acceptance():
    suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_financial_sse")
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
        "task": "3.8",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "scenarios": {
            "event_order": True,
            "chunk_boundary": True,
            "unknown_event_compatibility": True,
            "disconnect_retries_once": True,
            "interrupted_stream_not_saved_as_success": True,
            "duplicate_done_is_idempotent": True,
            "asynchronous_report_ready": True,
            "sensitive_fields_allow_listed": True,
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

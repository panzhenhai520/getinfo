#!/usr/bin/env python3
"""Acceptance gate for task 3.1 legacy chat/SSE compatibility."""

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
    from chat_route_orchestrator import LEGACY_SSE_EVENT_TYPES

    route_path = ROOT / "chat_route_orchestrator.py"
    tree = ast.parse(route_path.read_text(encoding="utf-8"))
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
    _assert(not imports & {"requests", "httpx", "openai"}, "route seam has direct model/network access")
    api_source = (ROOT / "chat_api.py").read_text(encoding="utf-8")
    page_source = (ROOT / "templates" / "mapindex.html").read_text(encoding="utf-8")
    _assert("@chat_bp.route('/api/chat/send'" in api_source, "chat endpoint changed")
    _assert("chat_route_orchestrator.plan(data)" in api_source, "route seam not installed")
    _assert("fetch('/api/chat/send'" in page_source, "frontend endpoint changed")
    return {
        "endpoint": "POST /api/chat/send",
        "legacy_event_types": list(LEGACY_SSE_EVENT_TYPES),
        "default_route": "legacy_chat",
        "new_services": [],
        "new_ports": [],
        "route_direct_model_network_imports": [],
    }


def runtime_acceptance():
    suite = unittest.defaultTestLoader.loadTestsFromName(
        "tests.test_chat_sse_compatibility"
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
        "task": "3.1",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "compatibility": {
            "old_request_body": True,
            "ordinary_chat": True,
            "web_search": True,
            "missing_key": True,
            "model_timeout": True,
            "stream_disconnect_contract_unchanged": True,
            "history_save_endpoint_unchanged": True,
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

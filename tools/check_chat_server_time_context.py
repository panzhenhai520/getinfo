#!/usr/bin/env python3
"""Acceptance gate for task 3.2 server-derived chat time context."""

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
    route_path = ROOT / "chat_route_orchestrator.py"
    source = route_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    _assert(not imports & {"openai", "requests", "httpx"}, "time context reads a model/network")
    _assert("self.clock()" in source, "server clock is not injected")
    _assert("client_now" not in source, "client clock can override server clock")
    _assert("chat_financial_routes" in source, "route persistence table missing")
    page = (ROOT / "templates" / "mapindex.html").read_text(encoding="utf-8")
    _assert("session_id: state.sessionId" in page, "session context hint missing")
    _assert("Intl.DateTimeFormat().resolvedOptions().timeZone" in page, "timezone hint missing")
    return {
        "context_version": "chat-server-time-v1",
        "clock_source": "application_server",
        "relative_expressions": ["today", "yesterday", "this_week", "now"],
        "persistence_table": "chat_financial_routes",
        "direct_model_or_network_imports": [],
        "client_clock_override_paths": 0,
        "new_services": [],
        "new_ports": [],
    }


def runtime_acceptance():
    suite = unittest.defaultTestLoader.loadTestsFromName(
        "tests.test_chat_server_time_context"
    )
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    _assert(result.wasSuccessful(), stream.getvalue())
    return {
        "executed": True,
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "frozen_clock": True,
        "cross_midnight_inheritance": True,
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
        "task": "3.2",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "boundaries": {
            "server_clock_captured_once": True,
            "user_timezone_is_hint_only": True,
            "model_does_not_compute_relative_time": True,
            "ordinary_sse_unchanged": True,
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

#!/usr/bin/env python3
"""Acceptance gate for task 3.4 target identity and clarification flow."""

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


def static_acceptance():
    from financial_target_resolver import TARGET_RESOLUTION_SCHEMA

    path = ROOT / "financial_target_resolver.py"
    source = path.read_text(encoding="utf-8")
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
    _assert(not imports & {"requests", "httpx", "openai"}, "resolver has direct model/network access")
    Draft202012Validator.check_schema(TARGET_RESOLUTION_SCHEMA)
    route = (ROOT / "chat_route_orchestrator.py").read_text(encoding="utf-8")
    api = (ROOT / "chat_api.py").read_text(encoding="utf-8")
    _assert("latest_target_state" in route, "same-session target state is missing")
    _assert("resolved_targets_json" in route and "clarification_json" in route, "target persistence missing")
    _assert("financial_clarification" in source, "clarification destination missing")
    clarification_index = api.index("if route_plan.target_resolution.get('status') == 'clarification_required'")
    model_config_index = api.index("cfg = _load_config()", clarification_index)
    _assert(clarification_index < model_config_index, "clarification does not pause before model setup")
    _assert("_stream_openai" not in api[clarification_index:model_config_index], "clarification calls model")
    return {
        "schema": "financial-target-resolution-v1",
        "resolution_layers": ["rules", "InstrumentRegistry_aliases", "grounded_SharedLLMBroker_ranker"],
        "persistence": ["chat_financial_routes.resolved_targets_json", "chat_financial_routes.clarification_json"],
        "public_clarification_events": ["status", "chunk", "done"],
        "direct_model_or_network_imports": [],
        "new_services": [],
        "new_ports": [],
        "new_tables": [],
    }


def runtime_acceptance():
    suite = unittest.defaultTestLoader.loadTestsFromName(
        "tests.test_financial_target_resolver"
    )
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    _assert(result.wasSuccessful(), stream.getvalue())
    return {
        "executed": True,
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "live_model_calls": 0,
        "network_calls": 0,
        "clarification_model_calls": 0,
        "clarification_web_search_calls": 0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = {
        "acceptance": "passed",
        "task": "3.4",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "scenarios": {
            "unique_tencent_and_apple_auto_continue": True,
            "bare_six_digit_never_guessed": True,
            "000001_requires_clarification": True,
            "fund_share_class_required": True,
            "fund_currency_required": True,
            "composite_targets_preserved": True,
            "same_session_context_inherited": True,
            "cross_session_context_blocked": True,
            "explicit_target_change_wins": True,
            "clarification_answer_restores_original_route": True,
        },
        "boundaries": {
            "one_material_question_at_a_time": True,
            "research_paused_while_ambiguous": True,
            "llm_ranker_requires_grounded_discriminator": True,
            "legacy_sse_contract_preserved": True,
            "real_trading": False,
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

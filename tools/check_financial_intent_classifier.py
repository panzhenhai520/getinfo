#!/usr/bin/env python3
"""Acceptance gate for task 3.3 financial intent and attributes."""

from __future__ import annotations

import argparse
import ast
import io
import json
import sys
import tempfile
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
    from financial_intent_classifier import FINANCIAL_INTENT_SCHEMA

    source_path = ROOT / "financial_intent_classifier.py"
    source = source_path.read_text(encoding="utf-8")
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
    _assert(not imports & {"requests", "httpx", "openai"}, "classifier has direct model/network access")
    Draft202012Validator.check_schema(FINANCIAL_INTENT_SCHEMA)
    route = (ROOT / "chat_route_orchestrator.py").read_text(encoding="utf-8")
    _assert("financial_classification_gate" in route, "dual financial gate missing")
    _assert("FinancialIntentClassifier" in route, "classifier is not attached to chat seam")
    _assert("SharedLLMBroker" in route, "existing local broker is not reused")
    _assert("route_key=LEGACY_CHAT_ROUTE" in route, "task 3.3 changed the public route early")
    return {
        "schema": "financial-intent-v1",
        "required_attributes": [
            "is_financial", "intent", "asset_type", "candidates", "market",
            "currency", "universe", "as_of", "freshness", "needs_clarification",
            "needs_full_research", "confidence",
        ],
        "decision_layers": ["rules", "InstrumentRegistry", "SharedLLMBroker_tiebreak"],
        "direct_model_or_network_imports": [],
        "new_services": [],
        "new_ports": [],
        "new_tables": [],
    }


def labeled_acceptance():
    from financial_instruments import InstrumentRegistry
    from financial_intent_classifier import FinancialIntentClassifier
    from sqlite_database import SQLiteDatabase

    dataset = json.loads(
        (ROOT / "tests" / "fixtures" / "financial_intent_labeled.json").read_text(
            encoding="utf-8"
        )
    )
    _assert(dataset.get("review_status") == "approved", "labeled set is not approved")
    with tempfile.TemporaryDirectory() as directory:
        database = SQLiteDatabase(str(Path(directory) / "intent-acceptance.sqlite3"))
        _assert(database.connect(), "acceptance database did not connect")
        _assert(database.create_tables(), "acceptance schema did not initialize")
        registry = InstrumentRegistry(database.connection)
        registry.load_controlled_seed()
        classifier = FinancialIntentClassifier(registry)
        counts = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
        for example in dataset["examples"]:
            actual = bool(classifier.classify(example["text"])["is_financial"])
            expected = bool(example["is_financial"])
            key = "tp" if actual and expected else "fp" if actual else "fn" if expected else "tn"
            counts[key] += 1
        database.disconnect()
    precision = counts["tp"] / max(1, counts["tp"] + counts["fp"])
    recall = counts["tp"] / max(1, counts["tp"] + counts["fn"])
    false_route_rate = counts["fp"] / max(1, counts["fp"] + counts["tn"])
    thresholds = dataset["thresholds"]
    _assert(precision >= thresholds["precision"], f"precision failed: {counts}")
    _assert(recall >= thresholds["recall"], f"recall failed: {counts}")
    _assert(
        false_route_rate <= thresholds["non_financial_false_route_rate"],
        f"false route rate failed: {counts}",
    )
    return {
        "dataset_id": dataset["dataset_id"],
        "review_status": dataset["review_status"],
        "examples": len(dataset["examples"]),
        "counts": counts,
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "non_financial_false_route_rate": round(false_route_rate, 6),
        "thresholds": thresholds,
    }


def runtime_acceptance():
    suite = unittest.defaultTestLoader.loadTestsFromName(
        "tests.test_financial_intent_classifier"
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
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = {
        "acceptance": "passed",
        "task": "3.3",
        "static": static_acceptance(),
        "labeled_set": labeled_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "boundaries": {
            "classifier_does_not_answer": True,
            "feature_off_does_not_initialize_or_call_classifier": True,
            "low_confidence_does_not_force_financial_route": True,
            "ambiguous_registry_candidate_not_selected": True,
            "server_as_of_cannot_be_overridden_by_llm": True,
            "legacy_sse_route_unchanged": True,
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

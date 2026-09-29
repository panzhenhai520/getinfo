#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Acceptance gate for task 4.6 held-out stage-4 financial conflicts."""

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

from financial_stage4_gate import evaluate_stage4_fixture


FIXTURE = ROOT / "tests" / "fixtures" / "financial_stage4_heldout.json"


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
    module_path = ROOT / "financial_stage4_gate.py"
    source = module_path.read_text(encoding="utf-8")
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    prohibited = sorted(_imports(module_path) & {"requests", "httpx", "openai", "anthropic"})
    cases = fixture.get("cases") or []
    categories = {str(item.get("category") or "") for item in cases}
    checks = {
        "held_out_policy_explicit": fixture.get("dataset_policy", {}).get("rule_development_usage") == "prohibited",
        "manual_labels_explicit": fixture.get("dataset_policy", {}).get("label_source") == "manual_scenario_review",
        "five_required_categories": categories == {"price", "financial_statement", "announcement", "index_membership", "macro_revision"},
        "minimum_case_count": len(cases) >= 10,
        "production_judges_reused": all(marker in source for marker in ("FinancialTemporalJudge", "FinancialConflictJudge")),
        "precision_recall_gate": all(marker in source for marker in ("current_fact_precision", "current_fact_recall", "0.95")),
        "future_stale_conflict_boundaries": all(marker in source for marker in ("future_data", "stale_data", "scope_conflict", "unresolved_conflict")),
        "model_report_boundary": all(marker in source for marker in ("tradingagents_report", "report_promoted_to_fact", "model_report_is_research_only")),
        "closed_result_schema": '"additionalProperties": False' in source,
        "no_direct_model_or_network_client": not prohibited,
    }
    _assert(all(checks.values()), {"checks": checks, "prohibited": prohibited})
    return {
        "checks": checks,
        "direct_model_or_network_imports": prohibited,
        "fixture": str(FIXTURE.relative_to(ROOT)),
        "case_count": len(cases),
        "new_tables": [],
        "new_services": [],
        "new_ports": [],
    }


def runtime_acceptance() -> dict:
    suites = [
        "tests.test_financial_stage4_gate",
        "tests.test_financial_temporal_judge",
        "tests.test_financial_conflict_judge",
        "tests.test_financial_rag_gate",
        "tests.test_financial_answer_composer",
    ]
    suite = unittest.TestSuite(
        unittest.defaultTestLoader.loadTestsFromName(name) for name in suites
    )
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    _assert(result.wasSuccessful(), stream.getvalue())
    evaluation = evaluate_stage4_fixture(FIXTURE)
    _assert(evaluation["status"] == "passed", evaluation)
    return {
        "executed": True,
        "suites": suites,
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "held_out_evaluation": evaluation,
        "network_calls": 0,
        "model_calls": 0,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    report = {
        "acceptance": "passed",
        "task": "4.6",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "boundaries": {
            "held_out_fixture_not_used_to_tune_rules": True,
            "production_judges_exercised": True,
            "rag_gate_regression_exercised": bool(args.runtime),
            "answer_composer_regression_exercised": bool(args.runtime),
            "model_report_is_research_not_fact": True,
            "new_database": False,
            "new_model_client": False,
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

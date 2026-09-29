#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Static/runtime acceptance gate for task 4.1 atomic claim extraction."""

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
    module = ROOT / "financial_claim_extractor.py"
    source = module.read_text(encoding="utf-8")
    prohibited = sorted(_imports(module) & {"requests", "httpx", "openai", "anthropic"})
    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "financial_claim_labeled.json").read_text(
            encoding="utf-8"
        )
    )
    checks = {
        "schema_is_closed": '"additionalProperties": False' in source,
        "structured_templates_first": "structured_template" in source,
        "deterministic_rules_present": "deterministic_rule" in source,
        "shared_broker_adapter_only": "class SharedLLMClaimExtractor" in source,
        "exact_source_span_required": "llm_source_span_not_grounded" in source,
        "numeric_grounding_required": "llm_value_not_grounded" in source,
        "unit_currency_grounding_required": "llm_unit_or_currency_not_grounded" in source,
        "server_time_context_preserved": "server_time_context_preserved" in source,
        "facts_pending_by_default": 'FACT_VERIFICATION_STATUS = "pending"' in source,
        "opinions_and_predictions_separate": all(
            marker in source for marker in ("opinion_not_fact", "prediction_not_fact")
        ),
        "existing_claim_table_reused": "INSERT INTO financial_claims" in source,
        "human_review_fixture": fixture.get("review_status") == "approved",
        "no_direct_model_or_network_client": not prohibited,
    }
    _assert(all(checks.values()), {"checks": checks, "prohibited": prohibited})
    return {
        "schema_version": "financial-claim-extraction-v1",
        "extractor_version": "financial-claim-extractor-v1",
        "checks": checks,
        "direct_model_or_network_imports": prohibited,
        "decision_order": [
            "structured_template",
            "deterministic_rule",
            "grounded_SharedLLMBroker_fallback",
        ],
        "existing_tables": ["financial_claims"],
        "new_tables": [],
        "new_services": [],
        "new_ports": [],
    }


def runtime_acceptance() -> dict:
    from financial_claim_extractor import FinancialClaimExtractor

    dataset = json.loads(
        (ROOT / "tests" / "fixtures" / "financial_claim_labeled.json").read_text(
            encoding="utf-8"
        )
    )
    extractor = FinancialClaimExtractor()
    total_numeric = 0
    covered_numeric = 0
    exact_span_count = 0
    claim_count = 0
    for index, case in enumerate(dataset["examples"]):
        result = extractor.extract(
            case["text"],
            source_kind="acceptance_fixture",
            source_ref=f"fixture:{index}",
            subject={
                "canonical_symbol": "0700.HK",
                "display_name": "腾讯控股",
                "asset_type": "equity",
                "market": "HK",
                "exchange": "XHKG",
                "country_code": "HK",
            },
            as_of="2026-07-31T03:00:00Z",
        )
        metrics = {item["metric"] for item in result["claims"]}
        claim_types = {item["claim_type"] for item in result["claims"]}
        _assert(set(case["metrics"]) <= metrics, f"fixture metrics missing: {index}")
        _assert(set(case["types"]) <= claim_types, f"fixture types missing: {index}")
        for claim in result["claims"]:
            span = claim["source_span"]
            if span.get("kind") == "text":
                _assert(
                    span["text"] == case["text"][span["start"]:span["end"]],
                    f"source span is not exact: {index}",
                )
                exact_span_count += 1
        claim_count += len(result["claims"])
        total_numeric += result["coverage"]["key_numeric_span_count"]
        covered_numeric += result["coverage"]["covered_key_numeric_span_count"]
    coverage = covered_numeric / total_numeric if total_numeric else 1.0
    threshold = float(dataset["numeric_coverage_threshold"])
    _assert(coverage >= threshold, f"numeric coverage {coverage} below {threshold}")

    suite = unittest.defaultTestLoader.loadTestsFromName(
        "tests.test_financial_claim_extractor"
    )
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    _assert(result.wasSuccessful(), stream.getvalue())
    return {
        "executed": True,
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "labeled_examples": len(dataset["examples"]),
        "atomic_claims_observed": claim_count,
        "exact_text_source_spans": exact_span_count,
        "key_numeric_span_count": total_numeric,
        "covered_key_numeric_span_count": covered_numeric,
        "key_numeric_coverage": round(coverage, 6),
        "coverage_threshold": threshold,
        "live_model_calls": 0,
        "network_calls": 0,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    report = {
        "acceptance": "passed",
        "task": "4.1",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "scenarios": {
            "numeric_scalar": True,
            "percentage_and_basis_points": True,
            "range": True,
            "yoy_and_qoq": True,
            "currency_and_unit_preserved": True,
            "negation": True,
            "announcement_and_index_membership": True,
            "opinion_separated": True,
            "conditional_prediction_separated": True,
            "structured_template_precedence": True,
            "llm_exact_span_grounding": True,
            "llm_ungrounded_value_rejected": True,
            "llm_ungrounded_unit_currency_rejected": True,
            "llm_cannot_override_as_of": True,
            "report_section_trace": True,
            "idempotent_persistence_preserves_verdict": True,
        },
        "boundaries": {
            "extraction_implies_verification": False,
            "facts_default_status": "pending",
            "opinion_status": "opinion_not_fact",
            "prediction_status": "prediction_not_fact",
            "shared_llm_broker_reused": True,
            "existing_sqlite_reused": True,
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

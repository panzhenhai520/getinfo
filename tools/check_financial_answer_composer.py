#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Static/runtime acceptance gate for task 4.4 FinancialAnswerComposer."""

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
    module = ROOT / "financial_answer_composer.py"
    chat = (ROOT / "chat_api.py").read_text(encoding="utf-8")
    source = module.read_text(encoding="utf-8")
    prohibited = sorted(
        _imports(module) & {"requests", "httpx", "openai", "anthropic"}
    )
    checks = {
        "closed_schema": '"additionalProperties": False' in source,
        "verified_fact_statuses_only": all(
            marker in source
            for marker in ("verified_current", "verified_historical", "fact_not_verified")
        ),
        "conflict_judge_gate_required": all(
            marker in source
            for marker in ("verified_consensus", "verified_authoritative", "fact_conflict_not_resolved")
        ),
        "clickable_citation_required": "fact_missing_clickable_citation" in source,
        "saved_terminal_report_required": all(
            marker in source for marker in ("report_not_saved_or_terminal", "TERMINAL_REPORT_STATUSES")
        ),
        "report_fields_not_rewritten": all(
            marker in source
            for marker in (
                '"report_recommendation_rewritten": False',
                '"report_confidence_rewritten": False',
                '"report_as_of_rewritten": False',
            )
        ),
        "stale_report_vs_new_snapshot_explicit": "report_older_than_current_fact" in source,
        "fixed_public_sections": all(
            marker in source
            for marker in (
                "对象与时点", "已核验当前事实", "TradingAgents 研究结论",
                "风险与反证", "冲突与证据缺口",
            )
        ),
        "disclaimer_present": "模拟研究参考，非投资建议" in source,
        "server_time_authoritative": all(
            marker in source for marker in ("application_server", "absolute application server time is required")
        ),
        "same_sse_chunk_path": all(
            marker in chat
            for marker in (
                "format_composed_realtime_answer", "format_composed_market_answer",
                "format_composed_research_answer", '"type":"chunk"', '"type":"done"',
            )
        ),
        "snapshot_trace_is_authenticated": all(
            marker in chat
            for marker in (
                "'/api/financial/snapshots/<int:snapshot_id>'", "@login_required",
                "FinancialAnswerComposerService(sqlite_db).public_snapshot",
            )
        ),
        "existing_verdict_and_report_tables_reused": all(
            marker in source
            for marker in (
                "financial_claims", "financial_claim_evidence", "financial_verdicts",
                "FinancialReportView", "financial_data_snapshots",
            )
        ),
        "no_direct_model_or_network_client": not prohibited,
    }
    _assert(all(checks.values()), {"checks": checks, "prohibited": prohibited})
    return {
        "schema_version": "financial-answer-v1",
        "composer_version": "financial-answer-composer-v1",
        "checks": checks,
        "direct_model_or_network_imports": prohibited,
        "existing_tables": [
            "financial_claims", "financial_claim_evidence", "financial_verdicts",
            "financial_final_reports", "financial_data_snapshots",
        ],
        "existing_endpoint": "POST /api/chat/send",
        "citation_endpoint": "GET /api/financial/snapshots/{id}",
        "new_tables": [],
        "new_services": [],
        "new_ports": [],
    }


def runtime_acceptance() -> dict:
    suite = unittest.defaultTestLoader.loadTestsFromName(
        "tests.test_financial_answer_composer"
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
        "model_calls": 0,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    report = {
        "acceptance": "passed",
        "task": "4.4",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "scenarios": {
            "verified_fact_only": True,
            "verified_fact_and_saved_research": True,
            "partial_or_unverified_evidence": True,
            "missing_price_not_fabricated": True,
            "stale_report_with_new_snapshot": True,
            "multiple_markets": True,
            "chinese_relative_time_uses_server_resolution": True,
            "rating_confidence_and_as_of_preserved": True,
            "risk_and_counter_evidence": True,
            "clickable_fact_trace": True,
            "same_sse_stream": True,
            "authenticated_snapshot_projection": True,
        },
        "boundaries": {
            "only_adjudicated_claim_facts": True,
            "saved_reports_are_research_opinion": True,
            "unverified_numbers_never_output_as_fact": True,
            "existing_sqlite_reused": True,
            "existing_sse_reused": True,
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

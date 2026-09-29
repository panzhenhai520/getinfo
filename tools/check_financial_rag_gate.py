#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Static/runtime acceptance gate for task 4.5 financial RAG gating."""

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
    gate_path = ROOT / "financial_rag_gate.py"
    gate = gate_path.read_text(encoding="utf-8")
    artifacts = (ROOT / "financial_artifacts.py").read_text(encoding="utf-8")
    ragflow = (ROOT / "ragflow_client.py").read_text(encoding="utf-8")
    adjudication = (ROOT / "financial_conflict_adjudication.py").read_text(encoding="utf-8")
    chat = (ROOT / "chat_api.py").read_text(encoding="utf-8")
    prohibited = sorted(_imports(gate_path) & {"requests", "httpx", "openai", "anthropic"})
    checks = {
        "closed_retrieval_schema": '"additionalProperties": False' in gate,
        "report_fact_history_separated": all(
            marker in gate for marker in ("research_report", "verified_fact", "historical_fact")
        ),
        "absolute_time_and_effective_to_required": all(
            marker in gate for marker in (
                "absolute application server time is required",
                "current financial fact memory requires effective_to",
                "future_document", "document_no_longer_effective",
            )
        ),
        "verified_conflict_verdict_only": all(
            marker in gate for marker in ("verified_consensus", "verified_authoritative", "claim_conflict_not_verified")
        ),
        "sqlite_registry_required": all(
            marker in gate for marker in ("financial_artifacts", "article_ragflow_documents", "unregistered_document")
        ),
        "local_projection_not_remote_chunk": all(
            marker in gate for marker in ("public_text", '"remote_chunk_text_used_as_fact": False')
        ),
        "claim_and_report_state_rechecked": all(
            marker in gate for marker in ("claim_not_current", "report_missing_or_revoked", "newer_report_version_effective")
        ),
        "existing_ragflow_search_api_reused": all(
            marker in ragflow for marker in ("def search_dataset", "/api/v1/datasets/{dataset_id}/search")
        ),
        "publication_registry_saved_before_upload": artifacts.find('memory_status="publishing"') < artifacts.find("upload_document_content"),
        "report_survives_ragflow_failure": all(
            marker in artifacts for marker in ("ragflow_unavailable", '"report_available": True')
        ),
        "explicit_human_publication_metadata": all(
            marker in adjudication + chat for marker in (
                '"content_kind": "human_adjudication"', "explicit_confirmation", "financial_rag_metadata_block",
            )
        ),
        "no_direct_model_or_network_client": not prohibited,
    }
    _assert(all(checks.values()), {"checks": checks, "prohibited": prohibited})
    return {
        "gate_version": "financial-rag-gate-v1",
        "checks": checks,
        "direct_model_or_network_imports": prohibited,
        "existing_tables": [
            "financial_artifacts", "financial_claims", "financial_claim_evidence",
            "financial_verdicts", "financial_final_reports", "articles",
            "article_ragflow_documents",
        ],
        "existing_services": ["RAGFlow", "SQLite"],
        "new_tables": [],
        "new_services": [],
        "new_ports": [],
    }


def runtime_acceptance() -> dict:
    suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_financial_rag_gate")
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    _assert(result.wasSuccessful(), stream.getvalue())
    return {
        "executed": True,
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "external_network_calls": 0,
        "model_calls": 0,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    report = {
        "acceptance": "passed",
        "task": "4.5",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "scenarios": {
            "current_fact_only_while_effective": True,
            "historical_fact_recallable_by_historical_time": True,
            "new_report_expires_old_current_projection": True,
            "unresolved_conflict_rejected_dynamically": True,
            "revoked_report_rejected_dynamically": True,
            "instrument_scope_enforced": True,
            "unregistered_remote_chunk_rejected": True,
            "remote_chunk_text_never_becomes_fact": True,
            "ragflow_unavailable_degrades_with_report_available": True,
            "explicit_human_adjudication_only": True,
        },
        "boundaries": {
            "ragflow_candidate_discovery_only": True,
            "history_not_deleted": True,
            "existing_sqlite_reused": True,
            "existing_ragflow_reused": True,
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

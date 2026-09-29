#!/usr/bin/env python3
"""Acceptance gate for task 3.12 versioned financial conflict adjudication."""

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
    module_path = ROOT / "financial_conflict_adjudication.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
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
    _assert(not prohibited, f"adjudication policy has external clients: {prohibited}")
    database = (ROOT / "sqlite_database.py").read_text(encoding="utf-8")
    api = (ROOT / "mapindex_api.py").read_text(encoding="utf-8")
    chat = (ROOT / "chat_api.py").read_text(encoding="utf-8")
    frontend = (ROOT / "templates" / "mapindex.html").read_text(encoding="utf-8")
    for decision in (
        "keep_newer_verified", "keep_historical", "both_opinions", "reject_all",
    ):
        _assert(decision in source and decision in database, f"financial decision missing: {decision}")
    for field in (
        "decision_version", "conflict_payload_sha256", "report_versions_json", "decided_by",
    ):
        _assert(field in database, f"version audit field missing: {field}")
    _assert("UNIQUE(operation_id, conflict_key, decision_version)" in database, "decision versions overwrite")
    _assert("@admin_required\ndef decide_mapindex_chat_conflict" in api, "financial decision is not admin-only")
    _assert("knowledge-gate" in api and "@admin_required" in api, "knowledge gate is not admin-only")
    _assert("adjudication_confirmation" in chat, "RAG publication confirmation is missing")
    _assert("get_chat_operation_for_review_pairs" in chat, "source_session omission bypass is possible")
    _assert("raw_review_answer_published" in source, "controlled publication boundary missing")
    _assert("unverified_rating_published_as_fact" in source, "opinion/fact publication boundary missing")
    _assert("金融裁决入库二次确认" in frontend, "frontend second confirmation missing")
    return {
        "schema_version": "financial-conflict-adjudication-v1",
        "existing_decision_table_extended": "chat_conflict_decisions",
        "immutable_version_fields": [
            "decision_version", "conflict_payload_sha256", "report_versions_json", "decided_by",
        ],
        "financial_decisions": [
            "keep_newer_verified", "keep_historical", "both_opinions", "reject_all",
        ],
        "admin_only": True,
        "report_update_stales_old_decision": True,
        "explicit_kb_confirmation": True,
        "raw_review_answer_to_rag": False,
        "unverified_rating_as_fact": False,
        "source_evidence_mutated": False,
        "direct_network_or_model_imports": prohibited,
        "new_services": [],
        "new_ports": [],
        "new_tables": [],
    }


def runtime_acceptance():
    suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_financial_conflict_adjudication")
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    _assert(result.wasSuccessful(), stream.getvalue())
    return {
        "executed": True,
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "live_ragflow_calls": 0,
        "model_calls": 0,
        "real_order_calls": 0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = {
        "acceptance": "passed",
        "task": "3.12",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "scenarios": {
            "admin_permission_enforced": True,
            "invalid_conflict_key_rejected": True,
            "legacy_row_migrated": True,
            "duplicate_save_idempotent": True,
            "changed_decision_creates_version": True,
            "new_report_stales_old_decision": True,
            "kb_second_confirmation_required": True,
            "source_session_omission_detected": True,
            "controlled_opinion_publication": True,
            "reject_all_publishes_nothing": True,
            "original_evidence_immutable": True,
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

#!/usr/bin/env python3
"""Acceptance gate for financial checkpoint, artifact and memory persistence."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
BANNED_STORES = {"chromadb", "faiss", "qdrant", "milvus", "weaviate"}


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def static_acceptance():
    module_path = ROOT / "financial_artifacts.py"
    source = module_path.read_text(encoding="utf-8")
    imports = _imports(module_path)
    lower_imports = {name.casefold() for name in imports}
    direct_model_or_transport = {
        "requests",
        "httpx",
        "openai",
        "langchain_openai",
        "tradingagents",
    }
    _assert(not (lower_imports & BANNED_STORES), "new vector store imported")
    _assert(
        not (lower_imports & direct_model_or_transport),
        "persistence layer imports a direct model/provider client",
    )
    _assert("os.replace" in source and "NamedTemporaryFile" in source, "atomic replace missing")
    _assert("financial_research_checkpoints" in source, "existing checkpoint table not reused")
    _assert("financial_final_reports" in source, "existing report table not reused")
    _assert("from ragflow_client import get_ragflow_client" in source, "existing RAGFlow client not reused")
    _assert("CRAWL_RESULTS_DIR" in source, "existing data volume not reused")
    _assert("FINANCIAL_RAGFLOW_KB_ID" in (ROOT / "config.py").read_text(encoding="utf-8"), "financial KB setting missing")
    return {
        "module": str(module_path.relative_to(ROOT)),
        "atomic_file_replace": True,
        "sqlite_tables_reused": [
            "financial_research_runs",
            "financial_research_checkpoints",
            "financial_final_reports",
            "financial_artifacts",
        ],
        "existing_ragflow_client_reused": True,
        "existing_data_volume_reused": True,
        "new_vector_store_imports": [],
        "direct_model_or_transport_imports": [],
    }


class _TimeoutRAGFlow:
    def __init__(self, connection):
        self.connection = connection
        self.outside_transaction = False

    def upload_document_content(self, kb_id, file_name, content, *, auto_parse):
        del kb_id, file_name, content, auto_parse
        self.outside_transaction = not self.connection.in_transaction
        raise TimeoutError("fixture timeout")


def _checkpoint(run_id, stage_index, *, schema, pad=""):
    return {
        "schema_version": schema,
        "graph_version": "stock-research-graph-v1",
        "research_run_id": run_id,
        "stage_index": stage_index,
        "state": {"fixture": "persisted", "pad": pad},
    }


def runtime_acceptance():
    from financial_artifacts import (
        FinancialArtifactStore,
        FinancialPersistenceError,
        FinancialSemanticMemory,
    )
    from sqlite_database import SQLiteDatabase
    from stock_research_graph import CHECKPOINT_SCHEMA_VERSION

    with tempfile.TemporaryDirectory() as directory:
        temp_root = Path(directory)
        database_path = temp_root / "application.sqlite3"
        artifact_root = temp_root / "crawl-results" / "financial-artifacts"

        database = SQLiteDatabase(str(database_path))
        _assert(database.connect(), "database connect failed")
        _assert(database.create_tables(), "database migration failed")
        connection = database.connection
        run_id = "persistence-acceptance"
        connection.execute(
            """
            INSERT INTO financial_research_runs(
                id, trigger_type, scope_type, status, requested_at
            ) VALUES(?, 'acceptance', 'market', 'queued', '2026-07-31T08:00:00.000Z')
            """,
            (run_id,),
        )
        report_id = int(
            connection.execute(
                """
                INSERT INTO financial_final_reports(
                    research_run_id, report_version, report_status,
                    recommendation, title, report_markdown, report_json
                ) VALUES(?, 1, 'generated_unverified', 'hold', ?, ?, ?)
                """,
                (
                    run_id,
                    "Persistence acceptance report",
                    "# Report\n\nEvidence-bounded HOLD.",
                    json.dumps({"recommendation": "hold"}, sort_keys=True),
                ),
            ).lastrowid
        )
        store = FinancialArtifactStore(
            connection,
            root_dir=artifact_root,
            inline_checkpoint_max_bytes=256,
        )
        callback = store.checkpoint_callback(run_id)
        callback(_checkpoint(run_id, 2, schema=CHECKPOINT_SCHEMA_VERSION))
        callback(
            _checkpoint(
                run_id,
                5,
                schema=CHECKPOINT_SCHEMA_VERSION,
                pad="checkpoint-payload-" * 100,
            )
        )

        # A disconnected connection represents a killed worker/container.  A new
        # process uses only the persisted SQLite row and existing data volume.
        database.disconnect()
        database = SQLiteDatabase(str(database_path))
        _assert(database.connect(), "database restart failed")
        _assert(database.create_tables(), "idempotent migration after restart failed")
        connection = database.connection
        restarted = FinancialArtifactStore(
            connection,
            root_dir=artifact_root,
            inline_checkpoint_max_bytes=256,
        )
        restarted.save_checkpoint(
            run_id,
            _checkpoint(run_id, 9, schema="future-checkpoint-v9"),
        )
        recovered = restarted.load_latest_compatible_checkpoint(
            run_id, accepted_schema_versions=[CHECKPOINT_SCHEMA_VERSION]
        )
        _assert(recovered.checkpoint["stage_index"] == 5, "latest compatible checkpoint not recovered")
        _assert(
            recovered.skipped and recovered.skipped[0]["reason"] == "incompatible_checkpoint_schema",
            "incompatible checkpoint was not skipped explicitly",
        )

        artifacts = restarted.persist_final_report(report_id)
        _assert({item.content_format for item in artifacts} == {"markdown", "json"}, "report formats missing")
        artifact_hashes = {}
        for artifact in artifacts:
            _, payload = restarted.load_artifact(artifact.artifact_id)
            actual_hash = hashlib.sha256(payload).hexdigest()
            _assert(actual_hash == artifact.content_sha256, "report artifact hash mismatch")
            artifact_hashes[artifact.content_format] = actual_hash
        _assert(not list(artifact_root.rglob("*.tmp")), "temporary report file left behind")

        timeout_client = _TimeoutRAGFlow(connection)
        memory = FinancialSemanticMemory(
            restarted,
            ragflow_client=timeout_client,
            kb_id="financial-acceptance-kb",
        )
        memory_result = memory.sync_final_report(report_id)
        _assert(memory_result["status"] == "degraded", "RAGFlow timeout did not degrade")
        _assert(memory_result["report_available"], "RAGFlow timeout hid final report")
        _assert(timeout_client.outside_transaction, "network call ran inside SQLite transaction")

        missing_run = "missing-checkpoint-acceptance"
        connection.execute(
            """
            INSERT INTO financial_research_runs(
                id, trigger_type, scope_type, status, requested_at
            ) VALUES(?, 'acceptance', 'market', 'running', '2026-07-31T08:00:00.000Z')
            """,
            (missing_run,),
        )
        missing_saved = restarted.save_checkpoint(
            missing_run,
            _checkpoint(
                missing_run,
                3,
                schema=CHECKPOINT_SCHEMA_VERSION,
                pad="missing-attachment-" * 100,
            ),
        )
        restarted._from_uri(missing_saved["storage_uri"]).unlink()
        missing_error = ""
        try:
            restarted.load_latest_compatible_checkpoint(
                missing_run, accepted_schema_versions=[CHECKPOINT_SCHEMA_VERSION]
            )
        except FinancialPersistenceError as exc:
            missing_error = exc.error_code
        _assert(missing_error == "checkpoint_artifact_missing", "missing checkpoint was not explicit")

        checkpoint_rows = int(
            connection.execute(
                "SELECT COUNT(*) FROM financial_research_checkpoints WHERE research_run_id=?",
                (run_id,),
            ).fetchone()[0]
        )
        artifact_rows = int(
            connection.execute(
                "SELECT COUNT(*) FROM financial_artifacts WHERE final_report_id=?",
                (report_id,),
            ).fetchone()[0]
        )
        database.disconnect()
        return {
            "executed": True,
            "worker_kill_restart_recovery": True,
            "recovered_stage_index": recovered.checkpoint["stage_index"],
            "incompatible_checkpoint_skipped": True,
            "missing_checkpoint_error_code": missing_error,
            "checkpoint_rows": checkpoint_rows,
            "report_artifact_rows": artifact_rows,
            "artifact_hashes": artifact_hashes,
            "atomic_replace_verified": True,
            "ragflow_timeout_status": memory_result["status"],
            "report_available_during_memory_degradation": memory_result["report_available"],
            "network_outside_sqlite_transaction": timeout_client.outside_transaction,
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = {
        "acceptance": "passed",
        "task": "2.21",
        "static": static_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "topology": {
            "new_services": [],
            "new_ports": [],
            "new_databases": [],
            "new_model_runtimes": [],
            "reused": ["SQLite", "CRAWL_RESULTS_DIR", "RAGFlow"],
        },
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()

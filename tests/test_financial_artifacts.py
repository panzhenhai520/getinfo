import json
import tempfile
import time
import unittest
from pathlib import Path

from financial_artifacts import (
    FinancialArtifactStore,
    FinancialPersistenceError,
    FinancialSemanticMemory,
)
from sqlite_database import SQLiteDatabase


class _RAGFlowTimeout:
    def __init__(self, connection):
        self.connection = connection
        self.called_outside_transaction = False

    def upload_document_content(self, kb_id, file_name, content, *, auto_parse):
        del kb_id, file_name, content, auto_parse
        self.called_outside_transaction = not self.connection.in_transaction
        raise TimeoutError("fixture provider endpoint must not leak")


class _RAGFlowSuccess:
    def __init__(self, connection):
        self.connection = connection
        self.calls = []

    def upload_document_content(self, kb_id, file_name, content, *, auto_parse):
        self.calls.append(
            {
                "kb_id": kb_id,
                "file_name": file_name,
                "content": content,
                "auto_parse": auto_parse,
                "outside_transaction": not self.connection.in_transaction,
            }
        )
        return {"data": [{"id": "ragflow-document-1"}]}


class FinancialArtifactStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.database_path = root / "financial-persistence.sqlite3"
        self.artifact_root = root / "crawl-results" / "financial-artifacts"
        self.database = self._open_database()
        self.connection = self.database.connection
        self._insert_run("persist-run")
        self.report_id = self._insert_report("persist-run")
        self.store = FinancialArtifactStore(
            self.connection,
            root_dir=self.artifact_root,
            inline_checkpoint_max_bytes=256,
        )

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _open_database(self):
        database = SQLiteDatabase(str(self.database_path))
        self.assertTrue(database.connect())
        self.assertTrue(database.create_tables())
        return database

    def _insert_run(self, run_id):
        self.connection.execute(
            """
            INSERT INTO financial_research_runs(
                id, trigger_type, scope_type, status, requested_at
            ) VALUES(?, 'test', 'market', 'queued', '2026-07-31T08:00:00.000Z')
            """,
            (run_id,),
        )

    def _insert_report(self, run_id):
        cursor = self.connection.execute(
            """
            INSERT INTO financial_final_reports(
                research_run_id, report_version, report_status,
                recommendation, title, report_markdown, report_json
            ) VALUES(?, 1, 'generated_unverified', 'hold', ?, ?, ?)
            """,
            (
                run_id,
                "测试金融终极报告",
                "# 测试金融终极报告\n\n结论：证据不足，保持观察。",
                json.dumps(
                    {"title": "测试金融终极报告", "recommendation": "hold"},
                    ensure_ascii=False,
                ),
            ),
        )
        return int(cursor.lastrowid)

    def _insert_report_version(self, run_id, version, title):
        cursor = self.connection.execute(
            """
            INSERT INTO financial_final_reports(
                research_run_id, report_version, report_status,
                recommendation, title, report_markdown, report_json
            ) VALUES(?, ?, 'generated_unverified', 'hold', ?, ?, ?)
            """,
            (
                run_id,
                int(version),
                title,
                f"# {title}\n\n合法版本 {version}",
                json.dumps(
                    {"title": title, "version": int(version)}, ensure_ascii=False
                ),
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _checkpoint(run_id, stage_index, *, schema="stock-research-checkpoint-v1", pad=""):
        return {
            "schema_version": schema,
            "research_run_id": run_id,
            "stage_index": stage_index,
            "state": {"fixture": True, "pad": pad},
        }

    def test_worker_kill_and_container_restart_restore_latest_checkpoint(self):
        first = self.store.save_checkpoint("persist-run", self._checkpoint("persist-run", 2))
        latest = self.store.save_checkpoint(
            "persist-run", self._checkpoint("persist-run", 5, pad="大" * 500)
        )
        self.assertFalse(first["externalized"])
        self.assertTrue(latest["externalized"])

        self.database.disconnect()
        self.database = self._open_database()
        self.connection = self.database.connection
        restarted = FinancialArtifactStore(
            self.connection,
            root_dir=self.artifact_root,
            inline_checkpoint_max_bytes=256,
        )
        recovered = restarted.load_latest_compatible_checkpoint(
            "persist-run", accepted_schema_versions=["stock-research-checkpoint-v1"]
        )
        self.assertEqual(recovered.checkpoint["stage_index"], 5)
        self.assertEqual(recovered.checkpoint_id, latest["checkpoint_id"])
        self.assertEqual(recovered.skipped, ())

    def test_incompatible_and_corrupt_newer_checkpoints_fall_back(self):
        compatible = self.store.save_checkpoint(
            "persist-run", self._checkpoint("persist-run", 3)
        )
        corrupt = self.store.save_checkpoint(
            "persist-run", self._checkpoint("persist-run", 4, pad="坏" * 500)
        )
        corrupt_path = self.store._from_uri(corrupt["storage_uri"])
        corrupt_path.write_bytes(b"corrupt")
        self.store.save_checkpoint(
            "persist-run",
            self._checkpoint("persist-run", 8, schema="future-checkpoint-v9"),
        )

        recovered = self.store.load_latest_compatible_checkpoint(
            "persist-run", accepted_schema_versions=["stock-research-checkpoint-v1"]
        )
        self.assertEqual(recovered.checkpoint_id, compatible["checkpoint_id"])
        self.assertEqual(
            [item["reason"] for item in recovered.skipped],
            ["incompatible_checkpoint_schema", "checkpoint_integrity_failed"],
        )

    def test_missing_external_checkpoint_is_explicit(self):
        saved = self.store.save_checkpoint(
            "persist-run", self._checkpoint("persist-run", 4, pad="缺" * 500)
        )
        self.store._from_uri(saved["storage_uri"]).unlink()
        with self.assertRaises(FinancialPersistenceError) as raised:
            self.store.load_latest_compatible_checkpoint(
                "persist-run", accepted_schema_versions=["stock-research-checkpoint-v1"]
            )
        self.assertEqual(raised.exception.error_code, "checkpoint_artifact_missing")

    def test_report_files_are_atomic_and_hash_verified(self):
        artifacts = self.store.persist_final_report(self.report_id)
        self.assertEqual({item.content_format for item in artifacts}, {"markdown", "json"})
        for artifact in artifacts:
            loaded, payload = self.store.load_artifact(artifact.artifact_id)
            self.assertEqual(loaded.status, "ready")
            self.assertEqual(len(payload), artifact.byte_count)
            self.assertEqual(list(self.artifact_root.rglob("*.tmp")), [])

        markdown = next(item for item in artifacts if item.content_format == "markdown")
        self.store._from_uri(markdown.storage_uri).write_bytes(b"tampered")
        with self.assertRaises(FinancialPersistenceError) as raised:
            self.store.load_artifact(markdown.artifact_id)
        self.assertEqual(raised.exception.error_code, "artifact_integrity_failed")
        status = self.connection.execute(
            "SELECT status FROM financial_artifacts WHERE id=?", (markdown.artifact_id,)
        ).fetchone()[0]
        self.assertEqual(status, "corrupt")

    def test_missing_report_file_is_recorded(self):
        artifacts = self.store.persist_final_report(self.report_id)
        json_artifact = next(item for item in artifacts if item.content_format == "json")
        self.store._from_uri(json_artifact.storage_uri).unlink()
        with self.assertRaises(FinancialPersistenceError) as raised:
            self.store.load_artifact(json_artifact.artifact_id)
        self.assertEqual(raised.exception.error_code, "artifact_missing")
        status = self.connection.execute(
            "SELECT status FROM financial_artifacts WHERE id=?", (json_artifact.artifact_id,)
        ).fetchone()[0]
        self.assertEqual(status, "missing")

    def test_corrupt_latest_report_falls_back_to_previous_hash_valid_version(self):
        first_artifacts = self.store.persist_final_report(self.report_id)
        second_report_id = self._insert_report_version(
            "persist-run", 2, "测试金融终极报告第二版"
        )
        second_artifacts = self.store.persist_final_report(second_report_id)
        latest = next(
            item for item in second_artifacts if item.content_format == "markdown"
        )
        self.store._from_uri(latest.storage_uri).write_bytes(b"damaged-report")

        started = time.perf_counter()
        loaded = self.store.load_latest_valid_report_artifact(
            "persist-run", content_format="markdown"
        )
        elapsed = time.perf_counter() - started

        previous = next(
            item for item in first_artifacts if item.content_format == "markdown"
        )
        self.assertEqual(loaded.artifact.artifact_id, previous.artifact_id)
        self.assertEqual(loaded.artifact.artifact_version, 1)
        self.assertIn("结论：证据不足", loaded.payload.decode("utf-8"))
        self.assertEqual(
            loaded.skipped,
            (
                {
                    "artifact_id": latest.artifact_id,
                    "artifact_version": 2,
                    "reason": "artifact_integrity_failed",
                },
            ),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM financial_artifacts WHERE id=?",
                (latest.artifact_id,),
            ).fetchone()[0],
            "corrupt",
        )
        self.assertLess(elapsed, 5)

    def test_ragflow_timeout_degrades_memory_but_keeps_report(self):
        client = _RAGFlowTimeout(self.connection)
        memory = FinancialSemanticMemory(
            self.store, ragflow_client=client, kb_id="financial-kb"
        )
        result = memory.sync_final_report(self.report_id)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["error_code"], "ragflow_timeout")
        self.assertTrue(result["report_available"])
        self.assertTrue(client.called_outside_transaction)
        artifact = self.store.report_artifact(self.report_id, "markdown")
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact.memory_status, "degraded")
        self.store.load_artifact(artifact.artifact_id)

    def test_ragflow_success_uses_dedicated_kb_and_records_document(self):
        client = _RAGFlowSuccess(self.connection)
        memory = FinancialSemanticMemory(
            self.store, ragflow_client=client, kb_id="financial-kb"
        )
        result = memory.sync_final_report(self.report_id)
        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(result["document_id"], "ragflow-document-1")
        self.assertEqual(client.calls[0]["kb_id"], "financial-kb")
        self.assertTrue(client.calls[0]["outside_transaction"])
        artifact = self.store.report_artifact(self.report_id, "markdown")
        self.assertEqual(artifact.ragflow_kb_id, "financial-kb")
        self.assertEqual(artifact.ragflow_document_id, "ragflow-document-1")
        self.assertEqual(artifact.memory_status, "uploaded")

    def test_missing_financial_kb_is_explicit_degradation(self):
        memory = FinancialSemanticMemory(
            self.store, ragflow_client=_RAGFlowSuccess(self.connection), kb_id=""
        )
        result = memory.sync_final_report(self.report_id)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(
            result["error_code"], "financial_ragflow_kb_not_configured"
        )
        artifact = self.store.report_artifact(self.report_id, "markdown")
        self.assertEqual(artifact.memory_status, "not_configured")


if __name__ == "__main__":
    unittest.main()

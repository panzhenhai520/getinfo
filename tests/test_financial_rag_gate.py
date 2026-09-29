import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from financial_artifacts import FinancialArtifactStore, FinancialSemanticMemory
from financial_conflict_judge import FINANCIAL_CONFLICT_JUDGE_VERSION
from financial_rag_gate import (
    FinancialRAGPublicationPlanner,
    FinancialRAGRetrievalGate,
    extract_financial_rag_metadata,
    validate_financial_rag_metadata,
)
from ragflow_client import RagflowClient
from sqlite_database import SQLiteDatabase


INSTRUMENT_KEY = "HK:XHKG:EQUITY:00700"
OTHER_INSTRUMENT_KEY = "HK:XHKG:EQUITY:00005"
NOW = {
    "server_now_utc": "2026-07-31T03:05:00Z",
    "requested_as_of": "2026-07-31T03:05:00Z",
}


class _MemoryRAGFlow:
    def __init__(self, connection):
        self.connection = connection
        self.uploads = []
        self.remote_extra = []
        self.fail_search = False

    @staticmethod
    def extract_document_ids(result):
        return [item["id"] for item in result.get("data") or []]

    def upload_document_content(self, kb_id, file_name, content, *, auto_parse):
        rows = self.connection.execute(
            "SELECT metadata_json FROM financial_artifacts"
        ).fetchall()
        metadata_was_saved = any(
            (json.loads(row[0]).get("ragflow_documents") or []) for row in rows
        )
        document_id = f"doc-{len(self.uploads) + 1}"
        self.uploads.append(
            {
                "kb_id": kb_id,
                "name": file_name,
                "content": content,
                "document_id": document_id,
                "outside_transaction": not self.connection.in_transaction,
                "metadata_was_saved": metadata_was_saved,
                "auto_parse": auto_parse,
            }
        )
        return {"data": [{"id": document_id}]}

    def search_dataset(self, kb_id, question, *, top_n):
        del kb_id, question, top_n
        if self.fail_search:
            raise TimeoutError("fixture")
        chunks = [
            {
                "document_id": item["document_id"],
                "document_name": item["name"],
                "content": "REMOTE TEXT MUST NEVER BE TRUSTED",
                "similarity": 0.99 - index * 0.01,
            }
            for index, item in enumerate(self.uploads)
        ]
        return {"chunks": chunks + list(self.remote_extra)}


class FinancialRAGGateTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.database = SQLiteDatabase(str(root / "financial-rag.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.connection = self.database.connection
        self.instrument_id = int(
            self.connection.execute(
                """
                INSERT INTO financial_instruments(
                    canonical_symbol, display_name, asset_type, market,
                    exchange, currency, country_code
                ) VALUES('0700.HK', '腾讯控股', 'equity', 'XHKG', 'XHKG', 'HKD', 'HK')
                """
            ).lastrowid
        )
        self.run_id = "financial-rag-run"
        self.connection.execute(
            """
            INSERT INTO financial_research_runs(
                id, trigger_type, scope_type, instrument_id, status,
                requested_at, completed_at
            ) VALUES(?, 'fixture', 'instrument', ?, 'completed',
                     '2026-07-31T03:00:00Z', '2026-07-31T03:01:00Z')
            """,
            (self.run_id, self.instrument_id),
        )
        self.report_id = int(
            self.connection.execute(
                """
                INSERT INTO financial_final_reports(
                    research_run_id, report_version, report_status,
                    recommendation, title, executive_summary, report_markdown,
                    report_json, observed_at, fetched_at, verified_at
                ) VALUES(?, 1, 'verified', 'hold', '腾讯终极报告', '审慎观察',
                         '# 腾讯终极报告\n\n研究结论。', ?,
                         '2026-07-31T03:00:00Z', '2026-07-31T03:01:00Z',
                         '2026-07-31T03:02:00Z')
                """,
                (
                    self.run_id,
                    json.dumps({"as_of": "2026-07-31T03:00:00Z"}),
                ),
            ).lastrowid
        )
        self.current_claim = self._claim(
            "current-price", "腾讯当前价格为500港元", "verified_current",
            "2026-07-31T03:00:00Z", "verified_consensus",
            effective_to="2026-07-31T03:10:00Z",
        )
        self.historical_claim = self._claim(
            "historical-price", "腾讯去年价格为400港元", "verified_historical",
            "2025-07-31T03:00:00Z", "verified_authoritative",
            effective_to="2025-08-01T00:00:00Z",
        )
        self.unverified_claim = self._claim(
            "unverified-price", "未经核验的价格", "pending",
            "2026-07-31T03:00:00Z", "single_source",
        )
        self.conflicted_claim = self._claim(
            "conflicted-price", "来源冲突的价格", "verified_current",
            "2026-07-31T03:00:00Z", "unresolved_conflict",
        )
        self.store = FinancialArtifactStore(
            self.connection, root_dir=root / "financial-artifacts"
        )
        self.ragflow = _MemoryRAGFlow(self.connection)
        self.memory = FinancialSemanticMemory(
            self.store, ragflow_client=self.ragflow, kb_id="financial-kb"
        )

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _claim(self, key, statement, status, as_of, verdict, effective_to=""):
        normalized = {
            "subject": {"instrument_key": INSTRUMENT_KEY},
            "metric": "last_price",
            "value": {"kind": "scalar", "number": 500},
            "as_of": as_of,
            "effective_to": effective_to,
        }
        claim_id = int(
            self.connection.execute(
                """
                INSERT INTO financial_claims(
                    research_run_id, final_report_id, claim_key, claim_type,
                    subject, statement, normalized_value_json, unit, currency,
                    effective_at, observed_at, verification_status
                ) VALUES(?, ?, ?, 'fact', ?, ?, ?, '港元', 'HKD', ?, ?, ?)
                """,
                (
                    self.run_id, self.report_id, key, INSTRUMENT_KEY, statement,
                    json.dumps(normalized), as_of, as_of, status,
                ),
            ).lastrowid
        )
        evidence_id = int(
            self.connection.execute(
                """
                INSERT INTO financial_claim_evidence(
                    claim_id, evidence_type, relationship, source_url,
                    source_title, evidence_json, observed_at, fetched_at
                ) VALUES(?, 'structured_snapshot', 'supports', ?, ?, '{}', ?, ?)
                """,
                (
                    claim_id, f"https://evidence.example/{key}",
                    f"{key} evidence", as_of, as_of,
                ),
            ).lastrowid
        )
        self.connection.execute(
            """
            INSERT INTO financial_verdicts(
                claim_id, adjudication_version, verdict, rationale,
                selected_evidence_ids_json, conflicting_evidence_ids_json,
                adjudicator, decided_at
            ) VALUES(?, 1, ?, ?, ?, '[]', ?, '2026-07-31T03:02:00Z')
            """,
            (
                claim_id, verdict,
                json.dumps({"selected_evidence_ids": [evidence_id]}),
                json.dumps([evidence_id]), FINANCIAL_CONFLICT_JUDGE_VERSION,
            ),
        )
        return claim_id

    def _sync(self):
        result = self.memory.sync_final_report(self.report_id)
        self.assertEqual(result["status"], "uploaded")
        return result

    def test_metadata_contract_rejects_unverified_fact_and_naive_time(self):
        with self.assertRaises(ValueError):
            validate_financial_rag_metadata(
                {
                    "content_kind": "verified_fact",
                    "instrument_key": INSTRUMENT_KEY,
                    "as_of": "2026-07-31 03:00:00",
                    "claim_id": 1,
                    "verdict": "single_source",
                }
            )

    def test_publication_separates_report_current_history_and_omits_unsafe_claims(self):
        documents = FinancialRAGPublicationPlanner(self.connection).build_documents(
            self.report_id
        )
        kinds = [item["metadata"]["content_kind"] for item in documents]
        self.assertEqual(kinds, ["research_report", "verified_fact", "historical_fact"])
        self.assertNotIn("未经核验", "\n".join(item["content"] for item in documents))
        self.assertNotIn("来源冲突", "\n".join(item["content"] for item in documents))
        fact = next(item for item in documents if item["metadata"]["claim_id"] == self.current_claim)
        self.assertIn("https://evidence.example/current-price", fact["content"])
        self.assertEqual(extract_financial_rag_metadata(fact["content"]), fact["metadata"])

    def test_sync_saves_registry_before_network_and_is_idempotent(self):
        first = self._sync()
        self.assertEqual(first["documents"], 3)
        self.assertTrue(all(item["outside_transaction"] for item in self.ragflow.uploads))
        self.assertTrue(all(item["metadata_was_saved"] for item in self.ragflow.uploads))
        artifact = self.store.report_artifact(self.report_id, "markdown")
        records = artifact.metadata["ragflow_documents"]
        self.assertTrue(all(item["status"] == "uploaded" for item in records))
        upload_count = len(self.ragflow.uploads)
        self._sync()
        self.assertEqual(len(self.ragflow.uploads), upload_count)

    def test_current_and_historical_routes_are_time_gated_and_use_local_projection(self):
        self._sync()
        current = self.memory.search(
            "腾讯现在价格", route="current_fact",
            server_time_context=NOW, instrument_keys=[INSTRUMENT_KEY],
        )
        self.assertEqual([item["claim_id"] for item in current["chunks"]], [self.current_claim])
        self.assertNotIn("REMOTE TEXT", current["chunks"][0]["content"])
        historical = self.memory.search(
            "腾讯去年价格", route="historical_fact",
            server_time_context={
                "server_now_utc": NOW["server_now_utc"],
                "requested_as_of": "2025-07-31T12:00:00Z",
            },
            time_range={
                "start_utc": "2025-07-31T00:00:00Z",
                "end_utc": "2025-07-31T23:59:59Z",
            },
            instrument_keys=[INSTRUMENT_KEY],
        )
        self.assertEqual([item["claim_id"] for item in historical["chunks"]], [self.historical_claim])
        self.assertEqual(historical["chunks"][0]["report_version"], 1)
        self.assertTrue(historical["chunks"][0]["public_url"].startswith("https://"))

    def test_dynamic_conflict_and_revocation_are_fail_closed(self):
        self._sync()
        self.connection.execute(
            "UPDATE financial_verdicts SET verdict='unresolved_conflict' WHERE claim_id=?",
            (self.current_claim,),
        )
        rejected = self.memory.search(
            "腾讯现在价格", route="current_fact",
            server_time_context=NOW, instrument_keys=[INSTRUMENT_KEY],
        )
        self.assertEqual(rejected["chunks"], [])
        self.assertIn("claim_conflict_not_verified", {item["reason"] for item in rejected["excluded"]})
        self.connection.execute(
            "UPDATE financial_final_reports SET report_status='draft' WHERE id=?",
            (self.report_id,),
        )
        research = self.memory.search(
            "腾讯研究", route="research", server_time_context=NOW,
            instrument_keys=[INSTRUMENT_KEY],
        )
        self.assertEqual(research["chunks"], [])
        self.assertIn("report_missing_or_revoked", {item["reason"] for item in research["excluded"]})

    def test_new_report_expires_old_current_memory_but_history_remains_recallable(self):
        self._sync()
        self.connection.execute(
            """
            INSERT INTO financial_final_reports(
                research_run_id, report_version, report_status, recommendation,
                title, executive_summary, report_markdown, report_json,
                observed_at, fetched_at
            ) VALUES(?, 2, 'verified', 'sell', '新版报告', '新版', '# 新版', '{}',
                     '2026-07-31T03:04:00Z', '2026-07-31T03:04:30Z')
            """,
            (self.run_id,),
        )
        current = self.memory.search(
            "腾讯现在价格", route="current_fact", server_time_context=NOW,
            instrument_keys=[INSTRUMENT_KEY],
        )
        self.assertEqual(current["chunks"], [])
        self.assertIn("newer_report_version_effective", {item["reason"] for item in current["excluded"]})
        historical = self.memory.search(
            "旧报告", route="research",
            server_time_context={
                "server_now_utc": NOW["server_now_utc"],
                "requested_as_of": "2026-07-31T03:03:00Z",
            },
            instrument_keys=[INSTRUMENT_KEY],
        )
        self.assertEqual(historical["status"], "ready")

    def test_instrument_scope_and_unregistered_remote_chunks_are_rejected(self):
        self._sync()
        self.ragflow.remote_extra.append(
            {"document_id": "attacker-doc", "content": "伪造实时价格", "similarity": 1}
        )
        result = self.memory.search(
            "汇丰价格", route="current_fact", server_time_context=NOW,
            instrument_keys=[OTHER_INSTRUMENT_KEY],
        )
        self.assertEqual(result["chunks"], [])
        reasons = {item["reason"] for item in result["excluded"]}
        self.assertIn("instrument_scope_mismatch", reasons)
        self.assertIn("unregistered_document", reasons)

    def test_ragflow_unavailable_degrades_without_hiding_persisted_report(self):
        self._sync()
        self.ragflow.fail_search = True
        result = self.memory.search(
            "腾讯价格", route="current_fact", server_time_context=NOW,
            instrument_keys=[INSTRUMENT_KEY],
        )
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["excluded"][0]["reason"], "ragflow_unavailable")
        artifact = self.store.report_artifact(self.report_id, "markdown")
        self.store.load_artifact(artifact.artifact_id)


class RagflowSearchClientTest(unittest.TestCase):
    def test_search_dataset_uses_existing_api_and_returns_candidate_identity(self):
        client = RagflowClient(base_url="http://ragflow.test", api_key="secret", retries=0)
        response = MagicMock()
        response.json.return_value = {
            "code": 0,
            "data": {"total": 1, "chunks": [{"document_id": "doc-1", "similarity": 0.9}]},
        }
        client._request = MagicMock(return_value=response)
        result = client.search_dataset("financial-kb", "腾讯现在价格", top_n=7)
        self.assertEqual(result["chunks"][0]["document_id"], "doc-1")
        args, kwargs = client._request.call_args
        self.assertEqual(args, ("POST", "/api/v1/datasets/financial-kb/search"))
        self.assertEqual(kwargs["json"]["size"], 7)
        self.assertNotIn("dataset_ids", kwargs["json"])


if __name__ == "__main__":
    unittest.main()

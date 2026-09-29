import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from flask import Flask

import chat_api
import mapindex_api
from financial_rag_gate import extract_financial_rag_metadata
from sqlite_database import SQLiteDatabase


class FinancialConflictAdjudicationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "financial-adjudication.sqlite3"))
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
        self.old_report, self.old_run = self._report("buy", "2026-07-30T03:00:00Z", "old")
        self.new_report, self.new_run = self._report("sell", "2026-07-31T03:00:00Z", "new")
        self.operation_id = "financial-adjudication-operation"
        self.review_session_id = "financial-adjudication-review"
        self.conflict_key = "opinion_0700"
        self.conflict = {
            "key": self.conflict_key,
            "conflict_type": "opinion_difference",
            "source_a": "h11",
            "source_b": "h12",
            "claim_a": "TradingAgents 评级：buy",
            "claim_b": "TradingAgents 评级：sell",
            "evidence_a": f"report#{self.old_report}/v1；verified",
            "evidence_b": f"report#{self.new_report}/v1；verified",
            "status": "待人工裁决",
            "claim": {"a": "buy", "b": "sell"},
            "evidence": {
                "a": [
                    {
                        "report_id": self.old_report,
                        "report_version": 1,
                        "research_run_id": self.old_run,
                        "recommendation": "buy",
                    }
                ],
                "b": [
                    {
                        "report_id": self.new_report,
                        "report_version": 1,
                        "research_run_id": self.new_run,
                        "recommendation": "sell",
                    }
                ],
            },
            "instrument": {
                "instrument_id": self.instrument_id,
                "canonical_symbol": "0700.HK",
                "display_name": "腾讯控股",
            },
            "as_of": {"a": "2026-07-30T03:00:00Z", "b": "2026-07-31T03:00:00Z"},
            "verdict": "opinion_difference",
            "reason": "两份终极报告的时点和评级不同。",
        }
        self.database.create_chat_operation(self.operation_id, "synthesize", ["source-a", "source-b"])
        self.database.update_chat_operation(
            self.operation_id,
            status="awaiting_decision",
            stage="review",
            progress=100,
            result={
                "session_id": self.review_session_id,
                "relation": "oppositional",
                "conflicts": [self.conflict],
                "financial_review": {"schema_version": "financial-synthesis-review-v1"},
            },
        )
        self.review_question = "金融冲突审阅问题"
        self.review_answer = "这是包含原始多空冲突内容的审阅草案，不应原样写入知识库。"
        self.assertIsNotNone(
            self.database.save_chat_qa(
                self.review_session_id,
                "local",
                "历史会话关联审阅",
                self.review_question,
                self.review_answer,
            )
        )
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY="fixture")
        self.app.register_blueprint(mapindex_api.mapindex_bp)
        self.app.register_blueprint(chat_api.chat_bp)

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _report(self, recommendation, as_of, suffix):
        run_id = f"adjudication-run-{suffix}"
        self.connection.execute(
            """
            INSERT INTO financial_research_runs(
                id, trigger_type, scope_type, instrument_id, status,
                current_stage, requested_at, completed_at
            ) VALUES(?, 'fixture', 'instrument', ?, 'completed', 'complete', ?, ?)
            """,
            (run_id, self.instrument_id, as_of, as_of),
        )
        report_id = int(
            self.connection.execute(
                """
                INSERT INTO financial_final_reports(
                    research_run_id, report_version, report_status, recommendation,
                    title, executive_summary, report_json, observed_at, fetched_at, verified_at
                ) VALUES(?, 1, 'verified', ?, 'fixture', 'fixture', '{}', ?, ?, ?)
                """,
                (run_id, recommendation, as_of, as_of, as_of),
            ).lastrowid
        )
        return report_id, run_id

    @staticmethod
    def _admin():
        return {"user_id": 1, "username": "admin-fixture", "role": "admin"}

    @contextmanager
    def _client_context(self, user=None):
        with (
            patch("mapindex_api.sqlite_db", self.database),
            patch("sqlite_database.sqlite_db", self.database),
            patch("decorators.user_db.verify_session", return_value=user or self._admin()),
        ):
            yield

    def _post_decision(self, decision="both_opinions", rationale="fixture"):
        with self._client_context():
            return self.app.test_client().post(
                f"/mapindex/api/chat/operations/{self.operation_id}/conflicts/{self.conflict_key}/decision",
                headers={"Authorization": "Bearer fixture"},
                json={"decision": decision, "rationale": rationale},
            )

    def test_financial_decision_requires_admin_and_valid_conflict_key(self):
        with self._client_context({"user_id": 2, "username": "reader", "role": "user"}):
            forbidden = self.app.test_client().post(
                f"/mapindex/api/chat/operations/{self.operation_id}/conflicts/{self.conflict_key}/decision",
                headers={"Authorization": "Bearer fixture"},
                json={"decision": "both_opinions"},
            )
        self.assertEqual(forbidden.status_code, 403)
        with self._client_context():
            missing = self.app.test_client().post(
                f"/mapindex/api/chat/operations/{self.operation_id}/conflicts/not-present/decision",
                headers={"Authorization": "Bearer fixture"},
                json={"decision": "both_opinions"},
            )
        self.assertEqual(missing.status_code, 404)

    def test_legacy_decision_row_migrates_to_version_one(self):
        self.connection.execute("DROP TABLE chat_conflict_decisions")
        self.connection.execute(
            """
            CREATE TABLE chat_conflict_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT NOT NULL,
                conflict_key TEXT NOT NULL,
                decision TEXT NOT NULL CHECK(decision IN ('keep_a','keep_b','keep_both_pending')),
                rationale TEXT NOT NULL DEFAULT '',
                applied_at TEXT NOT NULL,
                UNIQUE(operation_id, conflict_key)
            )
            """
        )
        self.connection.execute(
            """
            INSERT INTO chat_conflict_decisions(
                operation_id, conflict_key, decision, rationale, applied_at
            ) VALUES(?, 'legacy', 'keep_a', 'legacy rationale', '2026-07-31 03:00:00')
            """,
            (self.operation_id,),
        )
        self.database._ensure_chat_conflict_decisions_v2(self.connection.cursor())
        row = self.connection.execute(
            """
            SELECT decision, rationale, decision_version, conflict_payload_sha256
            FROM chat_conflict_decisions WHERE conflict_key='legacy'
            """
        ).fetchone()
        self.assertEqual(tuple(row), ("keep_a", "legacy rationale", 1, ""))
        ddl = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='chat_conflict_decisions'"
        ).fetchone()[0]
        self.assertIn("keep_newer_verified", ddl)

    def test_duplicate_save_is_idempotent_and_changed_decision_creates_version(self):
        first = self._post_decision()
        second = self._post_decision()
        changed = self._post_decision("keep_historical", "保留历史上下文")
        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(first.get_json()["decision_version"], 1)
        self.assertFalse(first.get_json()["idempotent"])
        self.assertEqual(second.get_json()["decision_version"], 1)
        self.assertTrue(second.get_json()["idempotent"])
        self.assertEqual(changed.get_json()["decision_version"], 2)
        with self._client_context():
            detail = self.app.test_client().get(
                f"/mapindex/api/chat/operations/{self.operation_id}",
                headers={"Authorization": "Bearer fixture"},
            ).get_json()["operation"]
        self.assertEqual(len(detail["conflict_decision_history"]), 2)
        self.assertEqual(detail["conflict_decisions"][0]["decision_version"], 2)
        self.assertEqual(detail["result"]["conflicts"][0]["claim_a"], self.conflict["claim_a"])

    def test_invalid_financial_semantics_are_rejected(self):
        fact = dict(self.conflict)
        fact.update(
            key="fact-price",
            conflict_type="fact_conflict",
            verdict="conflicted",
            as_of={"a": "2026-07-31T03:00:00Z", "b": "2026-07-31T03:00:00Z"},
        )
        operation = self.database.get_chat_operation(self.operation_id)
        operation["result"]["conflicts"].append(fact)
        self.database.update_chat_operation(self.operation_id, result=operation["result"])
        with self._client_context():
            response = self.app.test_client().post(
                f"/mapindex/api/chat/operations/{self.operation_id}/conflicts/fact-price/decision",
                headers={"Authorization": "Bearer fixture"},
                json={"decision": "both_opinions"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("观点差异", response.get_json()["error"])

    def test_new_report_marks_old_decision_stale_and_blocks_kb_gate(self):
        self.assertEqual(self._post_decision().status_code, 200)
        self.connection.execute(
            """
            INSERT INTO financial_final_reports(
                research_run_id, report_version, report_status, recommendation,
                title, executive_summary, report_json, observed_at, fetched_at, verified_at
            ) VALUES(?, 2, 'verified', 'hold', 'updated', 'updated', '{}',
                     '2026-07-31T03:04:00Z', '2026-07-31T03:04:00Z', '2026-07-31T03:04:00Z')
            """,
            (self.new_run,),
        )
        with self._client_context():
            client = self.app.test_client()
            detail = client.get(
                f"/mapindex/api/chat/operations/{self.operation_id}",
                headers={"Authorization": "Bearer fixture"},
            ).get_json()["operation"]
            gate = client.get(
                f"/mapindex/api/chat/sessions/{self.review_session_id}/knowledge-gate",
                headers={"Authorization": "Bearer fixture"},
            ).get_json()
        self.assertTrue(detail["conflict_decisions"][0]["is_stale"])
        self.assertIn(self.conflict_key, detail["financial_decision_review"]["stale_conflict_keys"])
        self.assertFalse(gate["ready"])

    def test_financial_kb_write_requires_separate_confirmation_and_admin(self):
        self.assertEqual(self._post_decision().status_code, 200)
        payload = {
            "pairs": [{"q": self.review_question, "a": self.review_answer}],
            "topic": "腾讯裁决",
            "model_name": "local",
            "kb_id": "financial-kb",
            "chunk_method": "naive",
            "source_session_id": self.review_session_id,
        }
        with self._client_context({"user_id": 2, "username": "reader", "role": "user"}):
            forbidden = self.app.test_client().post(
                "/api/chat/save-qa-batch",
                headers={"Authorization": "Bearer fixture"},
                json=payload,
            )
        self.assertEqual(forbidden.status_code, 403)
        with self._client_context():
            unconfirmed = self.app.test_client().post(
                "/api/chat/save-qa-batch",
                headers={"Authorization": "Bearer fixture"},
                json=payload,
            )
        self.assertEqual(unconfirmed.status_code, 409)
        self.assertTrue(unconfirmed.get_json()["requires_confirmation"])
        bypass_payload = dict(payload)
        bypass_payload.pop("source_session_id")
        with self._client_context():
            detected_copy = self.app.test_client().post(
                "/api/chat/save-qa-batch",
                headers={"Authorization": "Bearer fixture"},
                json=bypass_payload,
            )
        self.assertEqual(detected_copy.status_code, 409)
        self.assertTrue(detected_copy.get_json()["requires_confirmation"])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM articles WHERE url LIKE 'ai://chat-batch/%'").fetchone()[0],
            0,
        )

    def test_confirmed_publication_uses_controlled_opinion_content_and_preserves_evidence(self):
        decision = self._post_decision().get_json()
        payload = {
            "pairs": [{"q": self.review_question, "a": self.review_answer}],
            "topic": "腾讯裁决",
            "model_name": "local",
            "kb_id": "financial-kb",
            "chunk_method": "naive",
            "source_session_id": self.review_session_id,
            "adjudication_confirmation": {
                "confirmed": True,
                "operation_id": self.operation_id,
                "decision_versions": {self.conflict_key: decision["decision_version"]},
            },
        }
        reports_before = self.connection.execute("SELECT COUNT(*) FROM financial_final_reports").fetchone()[0]
        conflict_before = json.dumps(self.database.get_chat_operation(self.operation_id)["result"]["conflicts"], sort_keys=True)
        fake_client = MagicMock()
        fake_client.base_url = "http://ragflow.test"
        fake_client._headers.return_value = {"Authorization": "Bearer fixture"}
        fake_client.upload_document_content.return_value = {"data": [{"id": "doc-1"}]}
        fake_client.extract_document_ids.return_value = ["doc-1"]
        fake_client.parse_documents.return_value = {"success": True}
        with (
            self._client_context(),
            patch("ragflow_client.RagflowClient", return_value=fake_client),
            patch("requests.put", return_value=MagicMock(status_code=200)),
        ):
            response = self.app.test_client().post(
                "/api/chat/save-qa-batch",
                headers={"Authorization": "Bearer fixture"},
                json=payload,
            )
        result = response.get_json()
        self.assertEqual(response.status_code, 200, result)
        self.assertTrue(result["financial_adjudication"]["boundaries"]["explicit_confirmation"])
        self.assertEqual(result["saved_pairs"], 1)
        content = self.connection.execute(
            "SELECT content FROM articles WHERE id=?", (result["article_id"],)
        ).fetchone()[0]
        self.assertIn("两种研究观点并存", content)
        self.assertIn("不是已核验事实", content)
        self.assertNotIn(self.review_answer, content)
        rag_metadata = extract_financial_rag_metadata(content)
        self.assertEqual(rag_metadata["content_kind"], "human_adjudication")
        self.assertEqual(rag_metadata["instrument_key"], "HK:XHKG:EQUITY:00700")
        self.assertTrue(rag_metadata["explicit_confirmation"])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM financial_final_reports").fetchone()[0],
            reports_before,
        )
        self.assertEqual(
            json.dumps(self.database.get_chat_operation(self.operation_id)["result"]["conflicts"], sort_keys=True),
            conflict_before,
        )

    def test_reject_all_never_creates_kb_material(self):
        decision = self._post_decision("reject_all", "全部拒绝").get_json()
        with self._client_context():
            response = self.app.test_client().post(
                "/api/chat/save-qa-batch",
                headers={"Authorization": "Bearer fixture"},
                json={
                    "pairs": [{"q": self.review_question, "a": self.review_answer}],
                    "kb_id": "financial-kb",
                    "source_session_id": self.review_session_id,
                    "adjudication_confirmation": {
                        "confirmed": True,
                        "operation_id": self.operation_id,
                        "decision_versions": {self.conflict_key: decision["decision_version"]},
                    },
                },
            )
        self.assertEqual(response.status_code, 409)
        self.assertIn("没有可写入", response.get_json()["message"])


if __name__ == "__main__":
    unittest.main()

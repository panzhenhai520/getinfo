import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import chat_api
from financial_schema import FINANCIAL_SCHEMA_VERSION, ensure_financial_tables, get_financial_schema_version
from sqlite_database import SQLiteDatabase


QUESTION = "腾讯现在怎么看？"


class FinancialChatHistoryTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(
            str(Path(self.temp_dir.name) / "financial-chat-history.sqlite3")
        )
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.connection = self.database.connection
        self.connection.execute(
            """
            INSERT INTO financial_instruments(
                canonical_symbol, display_name, asset_type, market, exchange,
                currency, country_code
            ) VALUES('0700.HK', '腾讯控股', 'stock', 'XHKG', 'XHKG', 'HKD', 'HK')
            """
        )
        self.instrument_id = int(
            self.connection.execute(
                "SELECT id FROM financial_instruments WHERE canonical_symbol='0700.HK'"
            ).fetchone()[0]
        )
        self.connection.execute(
            """
            INSERT INTO financial_provider_profiles(
                provider_key, display_name, provider_type, is_enabled
            ) VALUES('easyquotation', 'EasyQuotation', 'market_data', 1)
            """
        )
        provider_id = int(
            self.connection.execute(
                "SELECT id FROM financial_provider_profiles WHERE provider_key='easyquotation'"
            ).fetchone()[0]
        )
        payload = '{"price":500}'
        snapshot = self.connection.execute(
            """
            INSERT INTO financial_data_snapshots(
                snapshot_key, instrument_id, provider_profile_id, data_type,
                observed_at, fetched_at, market_status, currency, payload_json,
                payload_sha256, source_url
            ) VALUES(?, ?, ?, 'quote', ?, ?, 'open', 'HKD', ?, ?, ?)
            """,
            (
                "chat-history-snapshot",
                self.instrument_id,
                provider_id,
                "2026-07-31T02:59:58Z",
                "2026-07-31T02:59:59Z",
                payload,
                hashlib.sha256(payload.encode()).hexdigest(),
                "https://example.test/quote/0700",
            ),
        )
        self.snapshot_id = int(snapshot.lastrowid)
        self.run_id = "research-chat-history-fixture"
        self.connection.execute(
            """
            INSERT INTO financial_research_runs(
                id, trigger_type, scope_type, instrument_id, status,
                requested_at, completed_at
            ) VALUES(?, 'chat', 'instrument', ?, 'completed', ?, ?)
            """,
            (
                self.run_id,
                self.instrument_id,
                "2026-07-31T02:58:00Z",
                "2026-07-31T03:00:00Z",
            ),
        )
        report = self.connection.execute(
            """
            INSERT INTO financial_final_reports(
                research_run_id, report_version, report_status, title,
                executive_summary, report_markdown, report_json,
                observed_at, fetched_at, verified_at
            ) VALUES(?, 3, 'verified', '腾讯终极报告', '摘要', ?, ?, ?, ?, ?)
            """,
            (
                self.run_id,
                "# 大型报告正文不应复制到聊天审计\n" + "x" * 5000,
                '{"large_internal_report":"' + "y" * 5000 + '"}',
                "2026-07-31T02:59:00Z",
                "2026-07-31T02:59:30Z",
                "2026-07-31T03:00:00Z",
            ),
        )
        self.report_id = int(report.lastrowid)

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _route(self, session_id, route_key, *, question=QUESTION):
        target = {
            "instrument_id": self.instrument_id,
            "canonical_symbol": "0700.HK",
            "display_name": "腾讯控股",
            "asset_type": "stock",
            "market": "XHKG",
            "exchange": "XHKG",
            "currency": "HKD",
            "share_class": None,
        }
        attributes = {
            "financial_intent": {
                "is_financial": True,
                "intent": "research",
                "freshness": "latest",
            },
            "target_resolution": {"status": "resolved", "targets": [target]},
            "realtime_query": {
                "status": "ready",
                "evidence": [{"snapshot_id": self.snapshot_id}],
            },
            "market_scope": {"status": "skipped", "report": {}},
            "full_research": {
                "status": "cache_hit",
                "research_run_ids": [self.run_id],
                "jobs": [],
                "reports": [
                    {
                        "report_id": self.report_id,
                        "research_run_id": self.run_id,
                        "source_refs": [{"snapshot_id": self.snapshot_id}],
                    }
                ],
            },
        }
        cursor = self.connection.execute(
            """
            INSERT INTO chat_financial_routes(
                route_key, session_id, question_sha256, raw_question, intent,
                financial_attributes_json, resolved_targets_json,
                route_status, route_destination, server_now, server_timezone
            ) VALUES(?, ?, ?, ?, 'research', ?, ?, 'full_research_cache_hit',
                     'financial_full_research', ?, 'Asia/Hong_Kong')
            """,
            (
                route_key,
                session_id,
                hashlib.sha256(question.encode()).hexdigest(),
                question,
                json.dumps(attributes, ensure_ascii=False),
                json.dumps([target], ensure_ascii=False),
                "2026-07-31T03:00:00Z",
            ),
        )
        return int(cursor.lastrowid)

    def test_ordinary_history_keeps_legacy_shape_without_audit(self):
        row_id = self.database.save_chat_qa(
            "ordinary-session", "local", "general", "你好", "你好"
        )
        self.assertIsNotNone(row_id)
        rows = self.database.get_chat_session_messages("ordinary-session")
        self.assertEqual(len(rows), 1)
        self.assertNotIn("financial_audit", rows[0])
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM chat_financial_artifacts"
            ).fetchone()[0],
            0,
        )

    def test_financial_answer_transactionally_links_route_target_snapshot_run_and_report_version(self):
        session_id = "financial-session"
        route_key = "chat-route-history-1"
        self._route(session_id, route_key)
        row_id = self.database.save_chat_qa(
            session_id,
            "local",
            "general",
            QUESTION,
            "已核验的金融回答",
            financial_route_key=route_key,
        )
        self.assertIsNotNone(row_id)
        rows = self.connection.execute(
            """
            SELECT artifact_type, artifact_ref, chat_history_id, payload_json
            FROM chat_financial_artifacts ORDER BY id
            """
        ).fetchall()
        self.assertEqual(
            {str(row[0]) for row in rows},
            {"route_context", "instrument", "snapshot", "research_run", "final_report"},
        )
        self.assertEqual({int(row[2]) for row in rows}, {int(row_id)})
        report_payload = next(
            json.loads(row[3]) for row in rows if str(row[0]) == "final_report"
        )
        self.assertEqual(report_payload["report_version"], 3)
        self.assertEqual(report_payload["report_url"], f"/api/financial/reports/{self.report_id}")
        rendered_artifacts = "".join(str(row[3]) for row in rows)
        self.assertNotIn("大型报告正文", rendered_artifacts)
        self.assertNotIn("large_internal_report", rendered_artifacts)

        replay = self.database.get_chat_session_messages(session_id)[0]["financial_audit"]
        self.assertEqual(replay["route"]["server_now"], "2026-07-31T03:00:00Z")
        self.assertEqual(replay["targets"][0]["canonical_symbol"], "0700.HK")
        self.assertIn("snapshot", {item["artifact_type"] for item in replay["artifacts"]})
        self.assertIn("final_report", {item["artifact_type"] for item in replay["artifacts"]})

    def test_artifact_failure_rolls_back_history_answer(self):
        session_id = "rollback-session"
        route_key = "chat-route-rollback"
        self._route(session_id, route_key)
        self.connection.execute(
            """
            CREATE TRIGGER reject_financial_history_artifact
            BEFORE INSERT ON chat_financial_artifacts
            BEGIN SELECT RAISE(ABORT, 'fixture artifact failure'); END
            """
        )
        row_id = self.database.save_chat_qa(
            session_id,
            "local",
            "general",
            QUESTION,
            "不得留下的回答",
            financial_route_key=route_key,
        )
        self.assertIsNone(row_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM chat_history WHERE session_id=?", (session_id,)
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM chat_financial_artifacts").fetchone()[0],
            0,
        )

    def test_delete_one_session_preserves_shared_snapshot_report_and_other_audit(self):
        sessions = ("shared-session-a", "shared-session-b")
        for index, session_id in enumerate(sessions, 1):
            key = f"chat-route-shared-{index}"
            self._route(session_id, key)
            self.assertIsNotNone(
                self.database.save_chat_qa(
                    session_id,
                    "local",
                    "general",
                    QUESTION,
                    f"共享报告回答 {index}",
                    financial_route_key=key,
                )
            )
        self.assertTrue(self.database.delete_chat_session(sessions[0]))
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM financial_final_reports WHERE id=?", (self.report_id,)
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM financial_data_snapshots WHERE id=?", (self.snapshot_id,)
            ).fetchone()[0],
            1,
        )
        self.assertEqual(len(self.database.get_chat_session_messages(sessions[0])), 0)
        self.assertIn(
            "financial_audit",
            self.database.get_chat_session_messages(sessions[1])[0],
        )
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_route_key_must_match_session_and_question_but_legacy_match_is_supported(self):
        route_key = "chat-route-strict-match"
        self._route("strict-session", route_key)
        wrong = self.database.save_chat_qa(
            "other-session",
            "local",
            "general",
            QUESTION,
            "不应误关联",
            financial_route_key=route_key,
        )
        self.assertIsNotNone(wrong)
        self.assertNotIn(
            "financial_audit",
            self.database.get_chat_session_messages("other-session")[0],
        )
        legacy = self.database.save_chat_qa(
            "strict-session", "local", "general", QUESTION, "旧客户端回答"
        )
        self.assertIsNotNone(legacy)
        self.assertIn(
            "financial_audit",
            self.database.get_chat_session_messages("strict-session")[0],
        )

    def test_api_save_and_history_replay_exposes_audit_only_on_assistant_message(self):
        session_id = "api-audit-session"
        route_key = "chat-route-api-audit"
        self._route(session_id, route_key)
        app = Flask(__name__)
        app.config.update(TESTING=True)
        app.register_blueprint(chat_api.chat_bp)
        client = app.test_client()
        with patch("sqlite_database.sqlite_db", self.database):
            saved = client.post(
                "/api/chat/history/save",
                json={
                    "session_id": session_id,
                    "model_id": "local",
                    "topic": "general",
                    "question": QUESTION,
                    "answer": "API 金融回答",
                    "financial_route_key": route_key,
                },
            )
            replay = client.get(f"/api/chat/history/session/{session_id}")
        self.assertTrue(saved.get_json()["success"])
        messages = replay.get_json()["messages"]
        self.assertNotIn("financial_audit", messages[0])
        self.assertEqual(messages[1]["financial_audit"]["route"]["route_key"], route_key)

    def test_v4_artifact_rows_migrate_to_nullable_history_link(self):
        route_key = "chat-route-v4-migration"
        route_id = self._route("migration-session", route_key)
        self.connection.execute("PRAGMA foreign_keys=OFF")
        self.connection.execute(
            "ALTER TABLE chat_financial_artifacts RENAME TO chat_financial_artifacts_v5_backup"
        )
        self.connection.execute(
            """
            CREATE TABLE chat_financial_artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_route_id INTEGER NOT NULL,
                artifact_type TEXT NOT NULL,
                artifact_ref TEXT NOT NULL,
                research_run_id TEXT,
                final_report_id INTEGER,
                snapshot_id INTEGER,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT '2026-07-31T00:00:00Z',
                UNIQUE (chat_route_id, artifact_type, artifact_ref)
            )
            """
        )
        self.connection.execute(
            "INSERT INTO chat_financial_artifacts(chat_route_id, artifact_type, artifact_ref) "
            "VALUES(?, 'route_context', 'legacy-route')",
            (route_id,),
        )
        self.connection.execute("DROP TABLE chat_financial_artifacts_v5_backup")
        self.connection.execute("PRAGMA foreign_keys=ON")

        ensure_financial_tables(self.connection.cursor())

        columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(chat_financial_artifacts)")
        }
        self.assertIn("chat_history_id", columns)
        row = self.connection.execute(
            "SELECT artifact_ref, chat_history_id FROM chat_financial_artifacts"
        ).fetchone()
        self.assertEqual(tuple(row), ("legacy-route", None))
        self.assertEqual(
            get_financial_schema_version(self.connection.cursor()), FINANCIAL_SCHEMA_VERSION
        )


if __name__ == "__main__":
    unittest.main()

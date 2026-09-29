import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import mapindex_api
from financial_gcd_review import render_financial_gcd_review, review_financial_history
from sqlite_database import SQLiteDatabase


NOW = datetime(2026, 7, 31, 3, 5, tzinfo=timezone.utc)


class FinancialGCDReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "financial-gcd.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.connection = self.database.connection
        self.connection.execute(
            """
            INSERT INTO financial_instruments(
                canonical_symbol, display_name, asset_type, market, exchange,
                currency, country_code
            ) VALUES('0700.HK', '腾讯控股', 'equity', 'XHKG', 'XHKG', 'HKD', 'HK')
            """
        )
        self.instrument_id = int(
            self.connection.execute(
                "SELECT id FROM financial_instruments WHERE canonical_symbol='0700.HK'"
            ).fetchone()[0]
        )
        self.connection.execute(
            """
            INSERT INTO financial_instruments(
                canonical_symbol, display_name, asset_type, market, exchange,
                currency, country_code
            ) VALUES('9988.HK', '阿里巴巴', 'equity', 'XHKG', 'XHKG', 'HKD', 'HK')
            """
        )
        self.other_instrument_id = int(
            self.connection.execute(
                "SELECT id FROM financial_instruments WHERE canonical_symbol='9988.HK'"
            ).fetchone()[0]
        )

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _snapshot(
        self,
        provider,
        value,
        observed,
        *,
        currency="HKD",
        adjustment="raw",
        stale_after="2026-07-31T03:10:00Z",
        market_status="open",
        quality_status="normalized_current",
        instrument_id=None,
    ):
        self.connection.execute(
            """
            INSERT INTO financial_provider_profiles(
                provider_key, display_name, provider_type, is_enabled
            ) VALUES(?, ?, 'fixture', 1)
            ON CONFLICT(provider_key) DO UPDATE SET display_name=excluded.display_name
            """,
            (provider, provider),
        )
        provider_id = int(
            self.connection.execute(
                "SELECT id FROM financial_provider_profiles WHERE provider_key=?", (provider,)
            ).fetchone()[0]
        )
        payload = {
            "metric": "last_price",
            "value": float(value),
            "unit": "price",
            "currency": currency,
            "adjustment": adjustment,
            "normalized_payload": {"last_price": float(value)},
        }
        payload_text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload_text.encode()).hexdigest()
        snapshot_key = hashlib.sha256(
            f"{provider}|{value}|{observed}|{currency}|{adjustment}".encode()
        ).hexdigest()
        cursor = self.connection.execute(
            """
            INSERT INTO financial_data_snapshots(
                snapshot_key, instrument_id, provider_profile_id, data_type,
                observed_at, fetched_at, market_status, currency, timezone,
                stale_after, quality_status, payload_json, payload_sha256, source_url
            ) VALUES(?, ?, ?, 'quote', ?, ?, ?, ?, 'Asia/Hong_Kong', ?, ?, ?, ?, ?)
            """,
            (
                snapshot_key,
                int(instrument_id or self.instrument_id),
                provider_id,
                observed,
                observed,
                market_status,
                currency,
                stale_after,
                quality_status,
                payload_text,
                digest,
                f"https://example.test/{provider}/{snapshot_key[:8]}",
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _audit(snapshot_ids, *, report=None):
        artifacts = [
            {
                "artifact_type": "snapshot",
                "artifact_ref": f"snapshot:{snapshot_id}",
                "payload": {"snapshot_id": snapshot_id},
            }
            for snapshot_id in snapshot_ids
        ]
        if report:
            artifacts.append(
                {
                    "artifact_type": "final_report",
                    "artifact_ref": f"final_report:{report[0]}:v{report[1]}",
                    "payload": {
                        "report_id": report[0],
                        "report_version": report[1],
                        "report_status": "verified",
                        "report_url": f"/api/financial/reports/{report[0]}",
                    },
                }
            )
        return {
            "schema_version": "financial-chat-audit-v1",
            "route": {"route_key": "fixture", "server_now": "2026-07-31T03:05:00Z"},
            "targets": [{"instrument_id": 1, "canonical_symbol": "0700.HK"}],
            "artifacts": artifacts,
        }

    def _row(self, row_id, snapshot_ids, *, session="source", report=None):
        return {
            "source_chat_history_id": row_id,
            "source_session_id": session,
            "question": "腾讯当时价格是多少？",
            "answer": "这是一条具有足够长度的已核验金融回答。",
            "model_id": "local",
            "topic": "financial",
            "financial_audit": self._audit(snapshot_ids, report=report),
        }

    def test_same_day_duplicate_with_overlapping_validity_merges_evidence(self):
        first = self._snapshot("provider-a", 500, "2026-07-31T03:00:00Z")
        second = self._snapshot("provider-b", 500, "2026-07-31T03:01:00Z")
        review = review_financial_history(
            self.connection,
            [self._row(11, [first]), self._row(12, [second], session="source-b")],
            server_now=NOW,
        )
        self.assertEqual(len(review["claims"]), 1)
        claim = review["claims"][0]
        self.assertEqual(claim["status"], "verified_current")
        self.assertEqual(claim["value"], 500.0)
        self.assertEqual(claim["period"]["kind"], "interval")
        self.assertEqual({item["snapshot_id"] for item in claim["evidence"]}, {first, second})
        self.assertEqual(claim["source_chat_history_ids"], [11, 12])

    def test_different_observation_times_are_historical_and_current_not_conflict(self):
        old = self._snapshot("provider-old", 480, "2026-07-30T08:00:00Z")
        current = self._snapshot("provider-current", 500, "2026-07-31T03:00:00Z")
        review = review_financial_history(
            self.connection,
            [self._row(21, [old]), self._row(22, [current])],
            server_now=NOW,
        )
        self.assertEqual(
            {(item["value"], item["status"]) for item in review["claims"]},
            {(480.0, "historical"), (500.0, "verified_current")},
        )
        self.assertEqual(review["status_counts"]["conflicted"], 0)

    def test_split_adjustment_and_currency_are_separate_dimensions(self):
        raw = self._snapshot("raw-source", 500, "2026-07-31T03:00:00Z", adjustment="raw")
        adjusted = self._snapshot(
            "adjusted-source", 250, "2026-07-31T03:00:00Z", adjustment="qfq"
        )
        usd = self._snapshot(
            "usd-source", 64, "2026-07-31T03:00:00Z", currency="USD"
        )
        review = review_financial_history(
            self.connection,
            [self._row(31, [raw, adjusted, usd])],
            server_now=NOW,
        )
        self.assertEqual(len(review["claims"]), 3)
        self.assertEqual({item["status"] for item in review["claims"]}, {"verified_current"})
        self.assertEqual(review["status_counts"]["conflicted"], 0)
        self.assertEqual(
            {(item["currency"], item["adjustment"]) for item in review["claims"]},
            {("HKD", "raw"), ("HKD", "qfq"), ("USD", "raw")},
        )

    def test_old_report_fact_stays_historical_when_new_price_exists(self):
        old = self._snapshot("report-source", 450, "2026-07-01T08:00:00Z")
        current = self._snapshot("live-source", 500, "2026-07-31T03:00:00Z")
        review = review_financial_history(
            self.connection,
            [self._row(41, [old], report=(7, 3)), self._row(42, [current])],
            server_now=NOW,
        )
        historical = next(item for item in review["claims"] if item["status"] == "historical")
        self.assertEqual(historical["report_refs"][0]["report_version"], 3)
        self.assertEqual(
            next(item for item in review["claims"] if item["status"] == "verified_current")["value"],
            500.0,
        )

    def test_material_same_instant_difference_is_conflicted_without_selection(self):
        first = self._snapshot("conflict-a", 500, "2026-07-31T03:00:00Z")
        second = self._snapshot("conflict-b", 530, "2026-07-31T03:00:00Z")
        review = review_financial_history(
            self.connection,
            [self._row(51, [first])],
            server_now=NOW,
        )
        claim = review["claims"][0]
        self.assertEqual(claim["status"], "conflicted")
        self.assertEqual(claim["value"]["alternatives"], [500.0, 530.0])
        self.assertEqual(len(claim["evidence"]), 2)
        self.assertIn("500.0 / 530.0", render_financial_gcd_review(review))

    def test_latest_open_snapshot_past_stale_after_is_stale(self):
        snapshot = self._snapshot(
            "stale-source",
            500,
            "2026-07-31T02:00:00Z",
            stale_after="2026-07-31T02:05:00Z",
        )
        review = review_financial_history(
            self.connection, [self._row(61, [snapshot])], server_now=NOW
        )
        self.assertEqual(review["claims"][0]["status"], "stale")

    def test_snapshot_cannot_cross_the_audited_instrument_boundary(self):
        wrong = self._snapshot(
            "wrong-instrument",
            90,
            "2026-07-31T03:00:00Z",
            instrument_id=self.other_instrument_id,
        )
        review = review_financial_history(
            self.connection, [self._row(71, [wrong])], server_now=NOW
        )
        self.assertEqual(review["claims"], [])
        self.assertEqual(review["unverified_source_answer_count"], 1)

    def test_gcd_api_unrelated_sessions_keep_originals_and_return_none(self):
        """新 ÷ 语义：内容无关时不建新会话、不删原会话，只返回 relation=none。"""
        financial_session = "financial-source-session"
        ordinary_session = "ordinary-source-session"
        self.connection.execute(
            """
            INSERT INTO chat_financial_routes(
                route_key, session_id, question_sha256, raw_question, intent,
                financial_attributes_json, resolved_targets_json, route_status,
                route_destination, server_now, server_timezone
            ) VALUES('gcd-route', ?, ?, ?, 'market_fact', '{}', '[]', 'ready',
                     'financial_realtime_snapshot', '2026-07-31T03:05:00Z', 'Asia/Hong_Kong')
            """,
            (
                financial_session,
                hashlib.sha256("腾讯现在的价格是多少？".encode()).hexdigest(),
                "腾讯现在的价格是多少？",
            ),
        )
        self.assertIsNotNone(
            self.database.save_chat_qa(
                financial_session,
                "local",
                "financial",
                "腾讯现在的价格是多少？",
                "腾讯当前已核验价格是 500 港元，来源见快照。",
            )
        )
        self.assertIsNotNone(
            self.database.save_chat_qa(
                ordinary_session,
                "local",
                "general",
                "项目中如何整理文档？",
                "请先按模块划分文档，再使用统一标题和版本约定。",
            )
        )
        app = Flask(__name__)
        app.config.update(TESTING=True, SECRET_KEY="fixture")
        app.register_blueprint(mapindex_api.mapindex_bp)
        with (
            patch("mapindex_api.sqlite_db", self.database),
            patch("mapindex_api.active_industry_identity", return_value={"id": ""}),
            patch("mapindex_api._server_now_utc", return_value=NOW),
            patch("mapindex_api._llm_gcd", side_effect=mapindex_api.IntelLLMError("本地 LLM 未配置")),
            patch("decorators.user_db.verify_session", return_value={"user_id": 1, "role": "admin"}),
        ):
            response = app.test_client().post(
                "/mapindex/api/chat/operations",
                headers={"Authorization": "Bearer fixture"},
                json={
                    "operation_type": "gcd",
                    "session_ids": [financial_session, ordinary_session],
                },
            )
        payload = response.get_json()
        self.assertEqual(response.status_code, 200, payload)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["status"], "completed")
        result = payload["result"]
        self.assertEqual(result["relation"], "none")
        self.assertNotIn("session_id", result)
        # 无关 → 原会话原封不动
        self.assertEqual(
            len(self.database.get_chat_session_messages(financial_session)), 1
        )
        self.assertEqual(
            len(self.database.get_chat_session_messages(ordinary_session)), 1
        )

    def test_gcd_merges_related_sessions_creates_named_session_and_deletes_originals(self):
        """新 ÷ 语义：相关会话 → 生成《公约：xxx》新会话，原会话物理删除。"""
        session_a = "related-a"
        session_b = "related-b"
        self.database.save_chat_qa(session_a, "local", "general", "腾讯的近期业务进展？", "腾讯公布了新一季财报，收入稳健增长。")
        self.database.save_chat_qa(session_b, "local", "general", "腾讯财报有哪些亮点？", "腾讯财报显示游戏与广告业务双增长，符合行业共识。")
        app = Flask(__name__)
        app.config.update(TESTING=True, SECRET_KEY="fixture")
        app.register_blueprint(mapindex_api.mapindex_bp)
        verdict = {
            "relation": "same_topic",
            "title": "腾讯财报动态",
            "question": "腾讯近期财报的共识结论",
            "answer": "腾讯财报显示游戏与广告业务双增长，收入稳健。",
        }
        with (
            patch("mapindex_api.sqlite_db", self.database),
            patch("mapindex_api.active_industry_identity", return_value={"id": ""}),
            patch("mapindex_api._server_now_utc", return_value=NOW),
            patch("mapindex_api._llm_gcd", return_value=verdict),
            patch("decorators.user_db.verify_session", return_value={"user_id": 1, "role": "admin"}),
        ):
            response = app.test_client().post(
                "/mapindex/api/chat/operations",
                headers={"Authorization": "Bearer fixture"},
                json={"operation_type": "gcd", "session_ids": [session_a, session_b]},
            )
        payload = response.get_json()
        self.assertEqual(response.status_code, 200, payload)
        self.assertTrue(payload["success"])
        result = payload["result"]
        self.assertEqual(result["relation"], "same_topic")
        self.assertEqual(result["title"], "公约：腾讯财报动态")
        self.assertEqual(result["deleted_source_sessions"], [session_a, session_b])
        # 新会话落库，命名 topic 为「公约：xxx」
        rows = self.database.get_chat_session_messages(result["session_id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["topic"], "公约：腾讯财报动态")
        self.assertIn("双增长", rows[0]["answer"])
        # 原会话已被物理删除
        self.assertEqual(self.database.get_chat_session_messages(session_a), [])
        self.assertEqual(self.database.get_chat_session_messages(session_b), [])

    def test_gcd_containment_drops_contained_session(self):
        """包含关系：B 的内容被 A 包含 → ÷ 结果只保留 A（超集）的内容。"""
        from mapindex_api import _drop_contained_sessions
        items = [
            {"source_session_id": "a", "question": "腾讯财报如何？", "answer": "腾讯财报显示游戏与广告业务双增长，收入稳健增长，符合行业共识。"},
            {"source_session_id": "a", "question": "广告业务呢？", "answer": "广告业务同样实现双位数增长。"},
            {"source_session_id": "b", "question": "腾讯财报如何？", "answer": "腾讯财报显示游戏与广告业务双增长，收入稳健增长，符合行业共识。"},
        ]
        kept, dropped = _drop_contained_sessions(items)
        self.assertEqual(dropped, ["b"])
        self.assertEqual([item["source_session_id"] for item in kept], ["a", "a"])


if __name__ == "__main__":
    unittest.main()

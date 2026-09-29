import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import mapindex_api
from financial_synthesis_review import review_financial_synthesis
from sqlite_database import SQLiteDatabase


NOW = datetime(2026, 7, 31, 3, 5, tzinfo=timezone.utc)


class FinancialSynthesisReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "financial-synthesis.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.connection = self.database.connection
        self.tencent_id = self._instrument("0700.HK", "腾讯控股")
        self.alibaba_id = self._instrument("9988.HK", "阿里巴巴")

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _instrument(self, symbol, name):
        cursor = self.connection.execute(
            """
            INSERT INTO financial_instruments(
                canonical_symbol, display_name, asset_type, market, exchange,
                currency, country_code
            ) VALUES(?, ?, 'equity', 'XHKG', 'XHKG', 'HKD', 'HK')
            """,
            (symbol, name),
        )
        return int(cursor.lastrowid)

    def _snapshot(
        self,
        provider,
        value,
        observed="2026-07-31T03:00:00Z",
        *,
        instrument_id=None,
        metric="last_price",
        period=None,
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
            "metric": metric,
            "value": float(value),
            "unit": "price" if metric == "last_price" else "amount",
            "currency": "HKD",
            "adjustment": "raw",
            "normalized_payload": {metric: float(value)},
        }
        data_type = "quote" if metric == "last_price" else "fundamental"
        if period:
            payload["period"] = period
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(text.encode()).hexdigest()
        key = hashlib.sha256(f"{provider}|{value}|{observed}|{metric}|{period}".encode()).hexdigest()
        cursor = self.connection.execute(
            """
            INSERT INTO financial_data_snapshots(
                snapshot_key, instrument_id, provider_profile_id, data_type,
                observed_at, fetched_at, market_status, currency, timezone,
                stale_after, quality_status, payload_json, payload_sha256, source_url
            ) VALUES(?, ?, ?, ?, ?, ?, 'open', 'HKD', 'Asia/Hong_Kong',
                     '2026-07-31T03:10:00Z', 'normalized_current', ?, ?, ?)
            """,
            (
                key,
                int(instrument_id or self.tencent_id),
                provider_id,
                data_type,
                observed,
                observed,
                text,
                digest,
                f"https://example.test/{provider}",
            ),
        )
        return int(cursor.lastrowid)

    def _report(self, recommendation, as_of, snapshot_ids=None, *, instrument_id=None):
        instrument_id = int(instrument_id or self.tencent_id)
        run_id = f"run-{instrument_id}-{recommendation}-{as_of}-{len(snapshot_ids or [])}"
        self.connection.execute(
            """
            INSERT INTO financial_research_runs(
                id, trigger_type, scope_type, instrument_id, status,
                current_stage, requested_at, completed_at
            ) VALUES(?, 'fixture', 'instrument', ?, 'completed', 'complete', ?, ?)
            """,
            (run_id, instrument_id, as_of, as_of),
        )
        report_json = {
            "as_of": as_of,
            "snapshot_ids": list(snapshot_ids or []),
            "evidence_coverage": 0.8,
            "market_status": "open",
        }
        cursor = self.connection.execute(
            """
            INSERT INTO financial_final_reports(
                research_run_id, report_version, report_status, recommendation,
                confidence, title, executive_summary, report_json,
                observed_at, fetched_at, verified_at
            ) VALUES(?, 1, 'verified', ?, 0.78, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                recommendation,
                f"{recommendation} fixture",
                "具有足够长度的 TradingAgents 研究报告摘要。",
                json.dumps(report_json, ensure_ascii=False),
                as_of,
                as_of,
                as_of,
            ),
        )
        return int(cursor.lastrowid)

    def _audit(self, instrument_id, *, snapshots=None, reports=None, intent="market_fact"):
        symbol, name = self.connection.execute(
            "SELECT canonical_symbol, display_name FROM financial_instruments WHERE id=?",
            (instrument_id,),
        ).fetchone()
        artifacts = [
            {
                "artifact_type": "snapshot",
                "artifact_ref": f"snapshot:{snapshot_id}",
                "payload": {"snapshot_id": snapshot_id},
            }
            for snapshot_id in snapshots or []
        ]
        artifacts.extend(
            {
                "artifact_type": "final_report",
                "artifact_ref": f"final_report:{report_id}:v1",
                "payload": {"report_id": report_id, "report_version": 1, "report_status": "verified"},
            }
            for report_id in reports or []
        )
        return {
            "schema_version": "financial-chat-audit-v1",
            "route": {"intent": intent, "server_now": "2026-07-31T03:05:00Z"},
            "targets": [
                {
                    "instrument_id": instrument_id,
                    "canonical_symbol": symbol,
                    "display_name": name,
                    "asset_type": "equity",
                    "market": "XHKG",
                    "currency": "HKD",
                }
            ],
            "artifacts": artifacts,
        }

    def _row(self, row_id, audit, question="腾讯的市场观点是什么？"):
        return {
            "source_chat_history_id": row_id,
            "source_session_id": f"session-{row_id}",
            "question": question,
            "answer": "这是一条具有足够长度的已核验金融回答内容。",
            "model_id": "local",
            "topic": "financial",
            "financial_audit": audit,
        }

    @staticmethod
    def _base(relation="oppositional", conflicts=None):
        return {
            "relation": relation,
            "reason": "fixture",
            "differences": "fixture",
            "summary_question": "fixture",
            "summary_answer": "fixture",
            "conflicts": list(conflicts or []),
            "model_id": "local",
        }

    def test_bull_bear_reports_are_opinion_difference_with_temporal_context(self):
        older = self._report("buy", "2026-07-30T03:00:00Z")
        current = self._report("sell", "2026-07-31T03:00:00Z")
        rows = [
            self._row(11, self._audit(self.tencent_id, reports=[older])),
            self._row(12, self._audit(self.tencent_id, reports=[current])),
        ]
        review = review_financial_synthesis(self.connection, rows, self._base(), server_now=NOW)
        self.assertEqual(review["recommended_relation"], "oppositional")
        conflict = review["conflicts"][0]
        self.assertEqual(conflict["conflict_type"], "opinion_difference")
        self.assertEqual(conflict["verdict"], "opinion_difference")
        self.assertEqual(
            {conflict["temporal_context"]["a"], conflict["temporal_context"]["b"]},
            {"historical_opinion", "current_opinion"},
        )
        for key in ("claim", "evidence", "instrument", "as_of", "verdict", "reason"):
            self.assertIn(key, conflict)

    def test_same_instant_price_conflict_retains_all_provider_values(self):
        first = self._snapshot("price-a", 500)
        second = self._snapshot("price-b", 530)
        rows = [self._row(21, self._audit(self.tencent_id, snapshots=[first, second]))]
        review = review_financial_synthesis(self.connection, rows, self._base("parallel"), server_now=NOW)
        conflict = review["conflicts"][0]
        self.assertEqual(conflict["conflict_type"], "fact_conflict")
        values = {
            item["value"] for side in ("a", "b") for item in conflict["evidence"][side]
        }
        self.assertEqual(values, {500.0, 530.0})
        self.assertIn("不自动选择", conflict["reason"])

    def test_same_financial_period_conflict_is_detected_across_observation_times(self):
        first = self._snapshot(
            "fundamental-a", 1000, "2026-07-29T03:00:00Z", metric="revenue", period="2026Q1"
        )
        second = self._snapshot(
            "fundamental-b", 1300, "2026-07-30T03:00:00Z", metric="revenue", period="2026Q1"
        )
        rows = [self._row(31, self._audit(self.tencent_id, snapshots=[first, second]))]
        review = review_financial_synthesis(self.connection, rows, self._base("parallel"), server_now=NOW)
        conflict = review["conflicts"][0]
        self.assertEqual(conflict["conflict_type"], "fact_conflict")
        self.assertEqual(conflict["as_of"]["period"]["label"], "2026Q1")

    def test_same_fact_basis_different_ratings_stays_opinion_not_fact_conflict(self):
        snapshot = self._snapshot("shared-fact", 500)
        buy = self._report("buy", "2026-07-31T02:58:00Z", [snapshot])
        sell = self._report("sell", "2026-07-31T03:00:00Z", [snapshot])
        rows = [
            self._row(41, self._audit(self.tencent_id, snapshots=[snapshot], reports=[buy])),
            self._row(42, self._audit(self.tencent_id, snapshots=[snapshot], reports=[sell])),
        ]
        review = review_financial_synthesis(self.connection, rows, self._base(), server_now=NOW)
        self.assertEqual(review["conflict_counts"]["fact_conflict"], 0)
        self.assertTrue(review["conflicts"][0]["temporal_context"]["same_fact_basis"])
        self.assertIn("风险偏好差异", review["conflicts"][0]["reason"])

    def test_different_instruments_do_not_implicitly_merge(self):
        rows = [
            self._row(51, self._audit(self.tencent_id)),
            self._row(52, self._audit(self.alibaba_id), question="阿里巴巴的市场观点是什么？"),
        ]
        review = review_financial_synthesis(self.connection, rows, self._base("progressive"), server_now=NOW)
        self.assertEqual(review["recommended_relation"], "none")
        self.assertTrue(review["scope"]["cross_instrument_merge_blocked"])
        self.assertEqual(review["conflicts"], [])

    def test_unrelated_sessions_stay_none_and_unverified_model_conflict_stays_pending(self):
        unrelated = review_financial_synthesis(
            self.connection,
            [{"source_chat_history_id": 61, "question": "如何整理文档？", "answer": "这是一条完整的非金融回答。"}],
            self._base("none"),
            server_now=NOW,
        )
        self.assertEqual(unrelated["recommended_relation"], "none")
        self.assertEqual(unrelated["conflicts"], [])
        rows = [self._row(62, self._audit(self.tencent_id)), self._row(63, self._audit(self.tencent_id))]
        model_conflict = {
            "key": "text-only",
            "source_a": "h62",
            "source_b": "h63",
            "claim_a": "评级 A",
            "claim_b": "评级 B",
            "evidence_a": "文本描述",
            "evidence_b": "文本描述",
        }
        pending = review_financial_synthesis(
            self.connection, rows, self._base(conflicts=[model_conflict]), server_now=NOW
        )
        self.assertEqual(pending["conflicts"][0]["verdict"], "pending_evidence")
        self.assertEqual(pending["conflicts"][0]["status"], "待补充证据")

    def _save_report_session(self, session_id, question, report_id, recommendation):
        symbol, name = self.connection.execute(
            "SELECT canonical_symbol, display_name FROM financial_instruments WHERE id=?",
            (self.tencent_id,),
        ).fetchone()
        target = {
            "instrument_id": self.tencent_id,
            "canonical_symbol": symbol,
            "display_name": name,
            "asset_type": "equity",
            "market": "XHKG",
            "exchange": "XHKG",
            "currency": "HKD",
        }
        route_key = f"route-{session_id}"
        attributes = {
            "financial_intent": {"is_financial": True, "intent": "investment_research"},
            "full_research": {
                "reports": [
                    {
                        "report_id": report_id,
                        "research_run_id": self.connection.execute(
                            "SELECT research_run_id FROM financial_final_reports WHERE id=?", (report_id,)
                        ).fetchone()[0],
                        "source_refs": [],
                    }
                ]
            },
        }
        self.connection.execute(
            """
            INSERT INTO chat_financial_routes(
                route_key, session_id, question_sha256, raw_question, intent,
                financial_attributes_json, resolved_targets_json, route_status,
                route_destination, server_now, server_timezone
            ) VALUES(?, ?, ?, ?, 'investment_research', ?, ?, 'ready',
                     'financial_full_research', '2026-07-31T03:05:00Z', 'Asia/Hong_Kong')
            """,
            (
                route_key,
                session_id,
                hashlib.sha256(question.encode()).hexdigest(),
                question,
                json.dumps(attributes, ensure_ascii=False),
                json.dumps([target], ensure_ascii=False),
            ),
        )
        self.assertIsNotNone(
            self.database.save_chat_qa(
                session_id,
                "local",
                "financial",
                question,
                f"TradingAgents 终报评级为 {recommendation}，证据详见报告。",
                financial_route_key=route_key,
            )
        )

    def test_operation_api_uses_grounded_reports_and_preserves_required_payload(self):
        """新 × 语义：整合成功生成《整合：xxx》新会话并物理删除原会话。"""
        buy = self._report("buy", "2026-07-31T02:58:00Z")
        sell = self._report("sell", "2026-07-31T03:00:00Z")
        self._save_report_session("buy-session", "腾讯的看多研究结论？", buy, "buy")
        self._save_report_session("sell-session", "腾讯的看空研究结论？", sell, "sell")
        app = Flask(__name__)
        app.config.update(TESTING=True, SECRET_KEY="fixture")
        app.register_blueprint(mapindex_api.mapindex_bp)
        base = self._base("parallel")
        base["title"] = "腾讯多空观点"
        base["summary_question"] = "腾讯多空观点对比"
        with (
            patch("mapindex_api.sqlite_db", self.database),
            patch("mapindex_api.active_industry_identity", return_value={"id": ""}),
            patch("mapindex_api.rollout_capability_enabled", return_value=True),
            patch("mapindex_api._server_now_utc", return_value=NOW),
            patch("mapindex_api._synthesize_chat_history", return_value=base),
            patch("mapindex_api._enrich_conflicts_with_library_evidence", side_effect=lambda conflicts, pack_id: conflicts),
            patch("decorators.user_db.verify_session", return_value={"user_id": 1, "role": "admin"}),
        ):
            response = app.test_client().post(
                "/mapindex/api/chat/operations",
                headers={"Authorization": "Bearer fixture"},
                json={"operation_type": "synthesize", "session_ids": ["buy-session", "sell-session"]},
            )
        payload = response.get_json()
        self.assertEqual(response.status_code, 200, payload)
        result = payload["result"]
        self.assertEqual(result["relation"], "oppositional")
        self.assertEqual(result["financial_review"]["conflict_counts"]["opinion_difference"], 1)
        conflict = result["conflicts"][0]
        self.assertEqual(conflict["instrument"]["canonical_symbol"], "0700.HK")
        self.assertTrue(conflict["as_of"]["a"])
        self.assertEqual(result["title"], "整合：腾讯多空观点")
        review_rows = self.database.get_chat_session_messages(result["session_id"])
        self.assertEqual(len(review_rows), 1)
        self.assertEqual(review_rows[0]["topic"], "整合：腾讯多空观点")
        self.assertIn("领域核验", review_rows[0]["answer"])
        self.assertIn("opinion_difference", review_rows[0]["answer"])
        # 原会话已物理删除
        self.assertEqual(self.database.get_chat_session_messages("buy-session"), [])
        self.assertEqual(self.database.get_chat_session_messages("sell-session"), [])


if __name__ == "__main__":
    unittest.main()

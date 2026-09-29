import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from financial_latest_observability import (
    FinancialLatestObservabilityService,
    METRIC_DEFINITIONS,
)
from sqlite_database import SQLiteDatabase


UTC = timezone.utc
NOW = datetime(2026, 8, 3, 15, 0, tzinfo=UTC)


class FinancialLatestObservabilityTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(
            str(Path(self.temp_dir.name) / "latest-observability.sqlite3")
        )
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _insert_route(self, route_key, attributes):
        self.database.connection.execute(
            """
            INSERT INTO chat_financial_routes(
                route_key,session_id,question_sha256,raw_question,intent,
                financial_attributes_json,route_status,route_destination,
                server_now,server_timezone
            ) VALUES(?,?,'fixture-hash','private question','market_fact',?,
                     'fixture','financial_latest_bundle',?,'UTC')
            """,
            (
                route_key,
                f"private-{route_key}",
                json.dumps(attributes, ensure_ascii=False),
                "2026-08-03T15:00:00Z",
            ),
        )

    @staticmethod
    def _counter(snapshot, name, **labels):
        return next(
            item["value"]
            for item in snapshot["counters"]
            if item["name"] == name and item["labels"] == labels
        )

    def test_metrics_are_complete_read_only_and_contain_only_stable_labels(self):
        self._insert_route("ready", {
            "server_time_context": {
                "user_timezone_source": "invalid_hint_fallback_server"
            },
            "information_needs": {
                "status": "planned", "channels": ["quote", "news"]
            },
            "target_resolution": {"status": "resolved"},
            "instrument_discovery": {
                "status": "promoted",
                "attempted_sources": ["catalog"],
                "reason_codes": ["verified_candidate_promoted"],
            },
            "realtime_query": {"status": "ready", "reason_codes": []},
            "news_query": {
                "status": "ready", "reason_codes": ["future_articles_rejected"]
            },
            "latest_bundle": {
                "status": "partial",
                "execution": {"channels": {
                    "quote": {"elapsed_ms": 12},
                    "news": {"elapsed_ms": 34},
                }},
                "latest_available": {"future_records_rejected": 1},
            },
        })
        self._insert_route("blocked", {
            "server_time_context": {"user_timezone_source": "client_hint"},
            "information_needs": {"status": "planned", "channels": ["news"]},
            "target_resolution": {"status": "no_target"},
            "instrument_discovery": {
                "status": "not_found", "attempted_sources": ["catalog"],
                "reason_codes": ["no_verified_candidate"],
            },
            "realtime_query": {"status": "skipped", "reason_codes": []},
            "news_query": {"status": "skipped", "reason_codes": []},
            "latest_bundle": {
                "status": "skipped", "execution": {"channels": {}},
                "latest_available": {},
            },
        })
        self.database.connection.commit()
        before = self.database.connection.total_changes

        snapshot = FinancialLatestObservabilityService(
            self.database, clock=lambda: NOW
        ).snapshot()

        self.assertEqual(self.database.connection.total_changes, before)
        self.assertEqual(snapshot["window"]["route_count"], 2)
        self.assertEqual(snapshot["window"]["malformed_route_count"], 0)
        self.assertEqual(
            {item["name"] for item in snapshot["metric_definitions"]},
            {item[0] for item in METRIC_DEFINITIONS},
        )
        self.assertEqual(self._counter(
            snapshot, "financial_information_needs_total",
            channels="news+quote", status="planned"
        ), 1)
        self.assertEqual(self._counter(
            snapshot, "financial_instrument_promotion_total",
            reason="verified_candidate_promoted", status="promoted"
        ), 1)
        self.assertEqual(self._counter(
            snapshot, "financial_future_evidence_rejected_total", kind="news"
        ), 1)
        self.assertEqual(self._counter(
            snapshot, "financial_generic_model_blocked_total", reason="unresolved_target"
        ), 1)
        self.assertEqual(self._counter(
            snapshot, "financial_timezone_fallback_total",
            reason="invalid_hint_fallback_server"
        ), 1)
        self.assertEqual(
            {item["labels"]["channel"]: item["p95_ms"] for item in snapshot["histograms"]},
            {"news": 34, "quote": 12},
        )
        encoded = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn("private question", encoded)
        self.assertNotIn("private-ready", encoded)

    def test_malformed_audit_is_counted_without_breaking_health_metrics(self):
        self._insert_route("malformed", {})
        self.database.connection.execute(
            "UPDATE chat_financial_routes SET financial_attributes_json='[' "
            "WHERE route_key='malformed'"
        )
        self.database.connection.commit()

        snapshot = FinancialLatestObservabilityService(
            self.database, clock=lambda: NOW
        ).snapshot(limit=1)

        self.assertEqual(snapshot["window"]["route_count"], 1)
        self.assertEqual(snapshot["window"]["malformed_route_count"], 1)
        self.assertEqual(snapshot["counters"], [])
        self.assertEqual(snapshot["histograms"], [])


if __name__ == "__main__":
    unittest.main()

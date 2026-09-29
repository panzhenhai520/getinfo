import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from financial_instruments import InstrumentRegistry
from financial_temporal_judge import (
    FinancialTemporalJudge,
    FinancialTemporalJudgeService,
    TEMPORAL_VERDICTS,
    validate_temporal_verdict,
)
from sqlite_database import SQLiteDatabase


NOW = datetime(2026, 7, 31, 3, 2, tzinfo=timezone.utc)
OPEN_SESSION = {
    "market_calendar_id": "XHKG",
    "market_session_state": "open",
    "trading_date": "2026-07-31",
    "session_open_utc": "2026-07-31T01:30:00Z",
    "session_close_utc": "2026-07-31T08:00:00Z",
    "calendar_source": "official_calendar",
    "calendar_version": "2026-v1",
}


def claim(metric="last_price", **overrides):
    result = {
        "claim_key": f"claim-{metric}",
        "claim_type": "fact",
        "metric": metric,
        "as_of": "2026-07-31T03:00:00Z",
        "period": {"kind": "instant", "start": "2026-07-31T03:00:00Z"},
    }
    result.update(overrides)
    return result


def evidence(evidence_id=1, **overrides):
    result = {
        "evidence_id": evidence_id,
        "relationship": "supports",
        "observed_at": "2026-07-31T03:00:00Z",
        "fetched_at": "2026-07-31T03:00:10Z",
        "freshness_threshold_seconds": 300,
        "freshness_state": "current",
        "quality_status": "verified",
    }
    result.update(overrides)
    return result


class FinancialTemporalJudgeTests(unittest.TestCase):
    def setUp(self):
        self.temporal_judge = FinancialTemporalJudge()

    def evaluate(self, claim_value, evidence_values, *, now=NOW, requested_as_of=None, session=None):
        context = {
            "server_now_utc": now.isoformat(),
            "requested_as_of": (requested_as_of or now).isoformat(),
            "server_timezone": "Asia/Hong_Kong",
            "user_timezone": "Asia/Hong_Kong",
        }
        result = self.temporal_judge.judge(
            claim_value,
            evidence_values,
            request_context=context,
            market_session=session or OPEN_SESSION,
        )
        self.assertEqual(validate_temporal_verdict(result), result)
        self.assertIn(result["verdict"], TEMPORAL_VERDICTS)
        return result

    def test_intraday_price_current_and_stale_use_request_time(self):
        current = self.evaluate(claim(), [evidence()])
        self.assertEqual(current["verdict"], "verified_current")
        self.assertIn("within_freshness_threshold", current["reason_codes"])
        self.assertTrue(current["boundaries"]["server_time_authoritative"])

        stale = self.evaluate(
            claim(as_of="2026-07-31T02:00:00Z"),
            [
                evidence(
                    observed_at="2026-07-31T02:00:00Z",
                    fetched_at="2026-07-31T02:00:10Z",
                    freshness_state="delayed",
                )
            ],
        )
        self.assertEqual(stale["verdict"], "stale")
        self.assertIn("freshness_threshold_exceeded", stale["reason_codes"])

    def test_closed_market_last_close_can_remain_current_but_is_explicit(self):
        now = datetime(2026, 7, 31, 8, 30, tzinfo=timezone.utc)
        session = {
            **OPEN_SESSION,
            "market_session_state": "closed",
            "reason": "after_close",
        }
        result = self.evaluate(
            claim(metric="close", as_of="2026-07-31T08:00:00Z"),
            [
                evidence(
                    observed_at="2026-07-31T08:00:00Z",
                    fetched_at="2026-07-31T08:00:10Z",
                    market_status="closed",
                )
            ],
            now=now,
            session=session,
        )
        self.assertEqual(result["verdict"], "verified_current")
        self.assertIn("market_closed", result["reason_codes"])
        self.assertTrue(result["boundaries"]["closed_market_is_explicit"])
        self.assertEqual(result["market_session"]["market_session_state"], "closed")

    def test_historical_request_and_older_quote_are_historical(self):
        historical_request = self.evaluate(
            claim(),
            [
                evidence(),
                evidence(
                    2,
                    observed_at="2026-07-31T04:00:00Z",
                    fetched_at="2026-07-31T04:00:10Z",
                ),
            ],
            now=datetime(2026, 7, 31, 8, 30, tzinfo=timezone.utc),
            requested_as_of=NOW,
        )
        self.assertEqual(historical_request["verdict"], "verified_historical")
        self.assertIn("historical_request_time", historical_request["reason_codes"])
        self.assertIn("post_request_evidence_ignored", historical_request["reason_codes"])
        self.assertEqual(historical_request["ignored_evidence_ids"], [2])

        older_quote = self.evaluate(
            claim(),
            [
                evidence(),
                evidence(
                    2,
                    observed_at="2026-07-31T03:01:00Z",
                    fetched_at="2026-07-31T03:01:05Z",
                ),
            ],
        )
        self.assertEqual(older_quote["verdict"], "verified_historical")
        self.assertIn("newer_observation_exists", older_quote["reason_codes"])

    def test_financial_restatement_and_macro_revision_supersede_same_period(self):
        period = {"kind": "reported", "label": "2025Q4", "end": "2025-12-31T00:00:00Z"}
        base = evidence(
            period=period,
            revision=1,
            observed_at="2026-03-20T01:00:00Z",
            fetched_at="2026-03-20T01:01:00Z",
        )
        revised = evidence(
            2,
            relationship="revises",
            event_type="financial_restatement",
            period=period,
            revision=2,
            effective_at="2026-07-01T00:00:00Z",
            observed_at="2026-07-01T00:00:00Z",
            fetched_at="2026-07-01T00:01:00Z",
        )
        financial = self.evaluate(
            claim(
                metric="revenue",
                as_of="2026-03-20T01:00:00Z",
                period=period,
                revision=1,
            ),
            [base, revised],
        )
        self.assertEqual(financial["verdict"], "superseded")
        self.assertEqual(financial["selected_evidence_ids"], [2])

        original_period = self.evaluate(
            claim(
                metric="revenue",
                as_of="2026-03-20T01:00:00Z",
                period=period,
                revision=1,
            ),
            [base],
        )
        self.assertEqual(original_period["verdict"], "verified_historical")
        self.assertIn("reported_period_fact", original_period["reason_codes"])

        macro = self.evaluate(
            claim(
                metric="gdp",
                as_of="2026-05-01T00:00:00Z",
                period={"kind": "reported", "label": "2026Q1"},
                revision="preliminary",
            ),
            [
                evidence(
                    observed_at="2026-05-01T00:00:00Z",
                    fetched_at="2026-05-01T00:01:00Z",
                    period={"kind": "reported", "label": "2026Q1"},
                    revision="preliminary",
                ),
                evidence(
                    3,
                    relationship="supersedes",
                    event_type="macro_revision",
                    effective_at="2026-06-01T00:00:00Z",
                    observed_at="2026-06-01T00:00:00Z",
                    fetched_at="2026-06-01T00:01:00Z",
                    period={"kind": "reported", "label": "2026Q1"},
                    revision="final",
                ),
            ],
        )
        self.assertEqual(macro["verdict"], "superseded")

    def test_split_and_index_rebalance_end_prior_validity(self):
        split = self.evaluate(
            claim(as_of="2026-06-01T00:00:00Z"),
            [
                evidence(
                    observed_at="2026-06-01T00:00:00Z",
                    fetched_at="2026-06-01T00:01:00Z",
                ),
                evidence(
                    2,
                    relationship="supersedes",
                    event_type="split",
                    effective_at="2026-07-01T00:00:00Z",
                    observed_at="2026-07-01T00:00:00Z",
                    fetched_at="2026-07-01T00:01:00Z",
                    affects_metrics=["last_price"],
                ),
            ],
        )
        self.assertEqual(split["verdict"], "superseded")

        membership = self.evaluate(
            claim(
                metric="index_membership",
                period={
                    "kind": "validity",
                    "start": "2026-01-01T00:00:00Z",
                    "end": "2026-06-30T23:59:59Z",
                },
            ),
            [evidence()],
        )
        self.assertEqual(membership["verdict"], "superseded")
        self.assertIn("declared_validity_ended", membership["reason_codes"])

    def test_future_missing_and_non_fact_inputs_fail_closed(self):
        future = self.evaluate(
            claim(),
            [
                evidence(
                    observed_at="2026-07-31T03:03:00Z",
                    fetched_at="2026-07-31T03:03:01Z",
                )
            ],
        )
        self.assertEqual(future["verdict"], "insufficient_evidence")
        self.assertIn("future_evidence_ignored", future["reason_codes"])
        self.assertEqual(future["ignored_evidence_ids"], [1])

        missing = self.evaluate(claim(), [])
        self.assertEqual(missing["verdict"], "insufficient_evidence")
        self.assertIn("no_supporting_temporal_evidence", missing["reason_codes"])

        opinion = self.evaluate(
            claim(metric="investment_recommendation", claim_type="opinion"),
            [evidence()],
        )
        self.assertEqual(opinion["verdict"], "insufficient_evidence")
        self.assertIn("non_fact_not_temporally_verified", opinion["reason_codes"])


class FinancialTemporalJudgeServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "temporal.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        registry = InstrumentRegistry(self.database.connection)
        registry.load_controlled_seed()
        instrument = registry.get_by_canonical_symbol("0700.HK")
        self.instrument_id = instrument.instrument_id
        self.database.connection.execute(
            """
            INSERT INTO financial_provider_profiles(
                provider_key, display_name, provider_type, access_tier, is_enabled
            ) VALUES('temporal-fixture', 'Temporal Fixture', 'fixture', 'free', 1)
            """
        )
        provider_id = int(self.database.connection.execute(
            "SELECT id FROM financial_provider_profiles WHERE provider_key='temporal-fixture'"
        ).fetchone()[0])
        self.database.connection.execute(
            """
            INSERT INTO financial_research_runs(
                id, trigger_type, scope_type, instrument_id, status
            ) VALUES('temporal-run', 'chat', 'instrument', ?, 'completed')
            """,
            (self.instrument_id,),
        )
        normalized = json.dumps(
            {
                "metric": "close",
                "value": {"kind": "scalar", "number": 500},
                "period": {"kind": "instant", "start": "2026-07-31T08:00:00Z"},
                "as_of": "2026-07-31T08:00:00Z",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        cursor = self.database.connection.execute(
            """
            INSERT INTO financial_claims(
                research_run_id, claim_key, claim_type, subject, statement,
                normalized_value_json, unit, currency, observed_at
            ) VALUES('temporal-run', 'persisted-close', 'fact',
                     'HK:XHKG:EQUITY:00700', '收盘价为500港元', ?, '港元', 'HKD',
                     '2026-07-31T08:00:00Z')
            """,
            (normalized,),
        )
        self.claim_id = int(cursor.lastrowid)
        payload = json.dumps(
            {
                "metric": "close",
                "value": 500,
                "unit": "港元",
                "currency": "HKD",
                "normalized_payload": {"close": 500},
                "freshness_threshold_seconds": 300,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        cursor = self.database.connection.execute(
            """
            INSERT INTO financial_data_snapshots(
                snapshot_key, instrument_id, provider_profile_id, data_type,
                observed_at, fetched_at, market_status, currency, timezone,
                quality_status, payload_json, payload_sha256, source_url
            ) VALUES('temporal-close', ?, ?, 'quote',
                     '2026-07-31T08:00:00Z', '2026-07-31T08:00:10Z',
                     'closed', 'HKD', 'Asia/Hong_Kong', 'verified', ?, ?,
                     'https://fixture.invalid/close')
            """,
            (self.instrument_id, provider_id, payload, digest),
        )
        snapshot_id = int(cursor.lastrowid)
        self.database.connection.execute(
            """
            INSERT INTO financial_claim_evidence(
                claim_id, snapshot_id, evidence_type, relationship,
                evidence_json, observed_at, fetched_at
            ) VALUES(?, ?, 'structured_snapshot', 'supports', '{}',
                     '2026-07-31T08:00:00Z', '2026-07-31T08:00:10Z')
            """,
            (self.claim_id, snapshot_id),
        )
        self.service = FinancialTemporalJudgeService(self.database)

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def test_service_reuses_sqlite_appends_changed_verdict_and_is_idempotent(self):
        now = datetime(2026, 7, 31, 8, 30, tzinfo=timezone.utc)
        first = self.service.judge_and_persist_claim(self.claim_id, server_now=now)
        self.assertEqual(first["verdict"], "verified_current")
        self.assertEqual(first["adjudication_version"], 1)
        self.assertTrue(first["persisted"])
        self.assertEqual(first["market_session"]["market_session_state"], "closed")

        duplicate = self.service.judge_and_persist_claim(self.claim_id, server_now=now)
        self.assertEqual(duplicate["verdict_id"], first["verdict_id"])
        self.assertFalse(duplicate["persisted"])
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM financial_verdicts WHERE claim_id=?",
                (self.claim_id,),
            ).fetchone()[0],
            1,
        )

        self.database.connection.execute(
            """
            UPDATE financial_data_snapshots
            SET quality_status='provider_stale'
            WHERE snapshot_key='temporal-close'
            """
        )
        changed = self.service.judge_and_persist_claim(
            self.claim_id,
            server_now=datetime(2026, 7, 31, 8, 31, tzinfo=timezone.utc),
        )
        self.assertEqual(changed["verdict"], "stale")
        self.assertEqual(changed["adjudication_version"], 2)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT verification_status FROM financial_claims WHERE id=?",
                (self.claim_id,),
            ).fetchone()[0],
            "stale",
        )
        rationales = self.database.connection.execute(
            """
            SELECT rationale FROM financial_verdicts
            WHERE claim_id=? ORDER BY adjudication_version
            """,
            (self.claim_id,),
        ).fetchall()
        self.assertEqual(len(rationales), 2)
        for row in rationales:
            payload = json.loads(row[0])
            self.assertEqual(payload["judge_version"], "financial-temporal-judge-v1")
            self.assertIn("decision_hash", payload)

    def test_missing_claim_fails_closed_without_verdict(self):
        result = self.service.judge_and_persist_claim(
            999999,
            server_now=datetime(2026, 7, 31, 8, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "claim_not_found")


if __name__ == "__main__":
    unittest.main()

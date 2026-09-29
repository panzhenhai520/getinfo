import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from financial_conflict_judge import (
    CONFLICT_VERDICTS,
    FinancialConflictJudge,
    FinancialConflictJudgeService,
    validate_conflict_verdict,
)
from financial_instruments import InstrumentRegistry
from sqlite_database import SQLiteDatabase


SUBJECT = "HK:XHKG:EQUITY:00700"
PERIOD = {"kind": "instant", "start": "2026-07-31T03:00:00Z"}


def claim(value=500.0, **overrides):
    result = {
        "claim_key": "tencent-price",
        "claim_type": "fact",
        "subject": SUBJECT,
        "metric": "last_price",
        "value": {"kind": "scalar", "number": value},
        "unit": "港元",
        "currency": "HKD",
        "adjustment": "raw",
        "period": PERIOD,
    }
    result.update(overrides)
    return result


def evidence(evidence_id, provider_id, value, **overrides):
    result = {
        "evidence_id": evidence_id,
        "snapshot_id": evidence_id + 100,
        "payload_sha256": f"{evidence_id:064x}",
        "provider_id": provider_id,
        "evidence_type": "structured_snapshot",
        "instrument_key": SUBJECT,
        "metric": "last_price",
        "value": value,
        "unit": "港元",
        "currency": "HKD",
        "adjustment": "raw",
        "period": PERIOD,
        "observed_at": "2026-07-31T03:00:00Z",
        "temporal_status": "verified_current",
        "availability_status": "available",
        "integrity_valid": True,
        "source_url": f"https://{provider_id}.example/quote",
    }
    result.update(overrides)
    return result


class FinancialConflictJudgeTests(unittest.TestCase):
    def setUp(self):
        self.judge = FinancialConflictJudge()

    def evaluate(self, claim_value, evidence_values):
        result = self.judge.judge(claim_value, evidence_values)
        self.assertEqual(validate_conflict_verdict(result), result)
        self.assertIn(result["verdict"], CONFLICT_VERDICTS)
        self.assertFalse(result["boundaries"]["prices_averaged"])
        self.assertFalse(result["boundaries"]["implicit_fx_conversion"])
        return result

    def test_independent_identical_and_within_tolerance_sources_confirm_without_averaging(self):
        identical = self.evaluate(
            claim(),
            [
                evidence(1, "akshare_cn", 500.0, underlying_source_id="eastmoney"),
                evidence(2, "tushare_cn", 500.0, underlying_source_id="tushare"),
            ],
        )
        self.assertEqual(identical["verdict"], "verified_consensus")
        self.assertEqual(identical["selected_evidence_ids"], [1, 2])
        self.assertEqual(identical["decision_value"]["number"], 500.0)
        self.assertIn(
            "highest_authority_value_selected_without_averaging",
            identical["reason_codes"],
        )

        within = self.evaluate(
            claim(500.3),
            [
                evidence(1, "akshare_cn", 500.0, underlying_source_id="eastmoney"),
                evidence(2, "tushare_cn", 500.3, underlying_source_id="tushare"),
            ],
        )
        self.assertEqual(within["verdict"], "verified_consensus")
        self.assertGreaterEqual(within["tolerance"]["effective_absolute"], 0.5003)
        self.assertEqual(within["decision_value"]["source_evidence_id"], 2)

    def test_over_tolerance_conflict_requires_human_review(self):
        result = self.evaluate(
            claim(),
            [
                evidence(1, "akshare_cn", 500.0, underlying_source_id="eastmoney"),
                evidence(2, "tushare_cn", 510.0, underlying_source_id="tushare"),
            ],
        )
        self.assertEqual(result["verdict"], "unresolved_conflict")
        self.assertTrue(result["human_review_required"])
        self.assertEqual(result["conflicting_evidence_ids"], [1, 2])
        self.assertEqual(result["decision_value"], {})
        self.assertFalse(result["boundaries"]["unresolved_conflict_is_current_fact"])

    def test_official_authority_can_decide_non_quote_but_official_article_cannot_decide_quote(self):
        financial_claim = claim(
            value=100.0,
            claim_key="revenue",
            metric="revenue",
            unit="亿元",
            currency="CNY",
            period={"kind": "reported", "label": "2026Q2"},
        )
        official = evidence(
            1,
            "issuer_official",
            100.0,
            metric="revenue",
            unit="亿元",
            currency="CNY",
            period={"kind": "reported", "label": "2026Q2"},
            authority_score=1.0,
            underlying_source_id="issuer_filing",
        )
        fallback = evidence(
            2,
            "yahoo",
            110.0,
            metric="revenue",
            unit="亿元",
            currency="CNY",
            period={"kind": "reported", "label": "2026Q2"},
            authority_score=0.5,
            underlying_source_id="yahoo",
        )
        decided = self.evaluate(financial_claim, [official, fallback])
        self.assertEqual(decided["verdict"], "verified_authoritative")
        self.assertEqual(decided["selected_evidence_ids"], [1])
        self.assertEqual(decided["conflicting_evidence_ids"], [2])

        quote = self.evaluate(
            claim(),
            [
                evidence(
                    1,
                    "official_evidence",
                    500.0,
                    evidence_type="source_document",
                    authority_score=1.0,
                    underlying_source_id="issuer_article",
                ),
                evidence(
                    2,
                    "yahoo",
                    510.0,
                    authority_score=0.5,
                    underlying_source_id="yahoo",
                ),
            ],
        )
        self.assertEqual(quote["verdict"], "unresolved_conflict")

    def test_same_underlying_source_is_collapsed_even_with_two_provider_names(self):
        result = self.evaluate(
            claim(),
            [
                evidence(1, "akshare_cn", 500.0, underlying_source_id="eastmoney"),
                evidence(2, "wrapper_b", 500.0, underlying_source_id="eastmoney"),
            ],
        )
        self.assertEqual(result["verdict"], "single_source")
        self.assertEqual(len(result["independence_groups"]), 1)
        self.assertIn(
            "correlated_source_collapsed",
            {item["reason"] for item in result["ignored_evidence"]},
        )
        self.assertFalse(result["boundaries"]["provider_names_equal_independent_sources"])

        unknown_lineage = self.evaluate(
            claim(),
            [
                evidence(3, "wrapper_a", 500.0, source_url=""),
                evidence(4, "wrapper_b", 500.0, source_url=""),
            ],
        )
        self.assertEqual(unknown_lineage["verdict"], "single_source")
        self.assertEqual(
            unknown_lineage["independence_groups"][0]["independence_key"],
            "unknown_lineage",
        )

    def test_currency_adjustment_and_period_are_never_compared_or_converted(self):
        mixed = self.evaluate(
            claim(),
            [
                evidence(1, "akshare_cn", 500.0, underlying_source_id="eastmoney"),
                evidence(
                    2,
                    "tushare_cn",
                    250.0,
                    adjustment="qfq",
                    underlying_source_id="tushare",
                ),
                evidence(
                    3,
                    "yahoo",
                    64.0,
                    unit="美元",
                    currency="USD",
                    underlying_source_id="yahoo",
                ),
            ],
        )
        self.assertEqual(mixed["verdict"], "single_source")
        reasons = {item["reason"] for item in mixed["ignored_evidence"]}
        self.assertIn("incomparable_currency_unit_adjustment_or_period", reasons)
        self.assertFalse(mixed["boundaries"]["different_currency_compared"])
        self.assertFalse(mixed["boundaries"]["different_adjustment_compared"])

        no_match = self.evaluate(
            claim(),
            [
                evidence(
                    2,
                    "tushare_cn",
                    250.0,
                    adjustment="qfq",
                    underlying_source_id="tushare",
                )
            ],
        )
        self.assertEqual(no_match["verdict"], "incomparable_evidence")

        unit_normalized = self.evaluate(
            claim(
                value=1.0,
                claim_key="yield-change",
                metric="yield_change",
                unit="percent",
                currency="",
            ),
            [
                evidence(
                    4,
                    "issuer_official",
                    100.0,
                    metric="yield_change",
                    unit="bps",
                    currency="",
                    underlying_source_id="issuer",
                ),
                evidence(
                    5,
                    "tushare_cn",
                    1.0,
                    metric="yield_change",
                    unit="percent",
                    currency="",
                    underlying_source_id="tushare",
                ),
            ],
        )
        self.assertEqual(unit_normalized["verdict"], "verified_consensus")

    def test_delayed_observation_is_not_treated_as_same_instant_conflict(self):
        result = self.evaluate(
            claim(),
            [
                evidence(1, "akshare_cn", 500.0, underlying_source_id="eastmoney"),
                evidence(
                    2,
                    "tushare_cn",
                    510.0,
                    observed_at="2026-07-31T02:55:00Z",
                    underlying_source_id="tushare",
                ),
            ],
        )
        self.assertEqual(result["verdict"], "single_source")
        self.assertIn(
            "different_observation_time",
            {item["reason"] for item in result["ignored_evidence"]},
        )

    def test_permission_denied_and_single_source_fail_closed(self):
        result = self.evaluate(
            claim(),
            [
                evidence(1, "akshare_cn", 500.0, underlying_source_id="eastmoney"),
                evidence(
                    2,
                    "tushare_cn",
                    500.0,
                    availability_status="permission_denied",
                    underlying_source_id="tushare",
                ),
            ],
        )
        self.assertEqual(result["verdict"], "single_source")
        self.assertIn(
            "provider_permission_denied",
            {item["reason"] for item in result["ignored_evidence"]},
        )
        self.assertFalse(result["human_review_required"])

        nonfact = self.evaluate(
            claim(claim_type="opinion", metric="investment_recommendation", value=None),
            [],
        )
        self.assertEqual(nonfact["verdict"], "insufficient_evidence")


class FinancialConflictJudgeServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "conflict.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        registry = InstrumentRegistry(self.database.connection)
        registry.load_controlled_seed()
        instrument = registry.get_by_canonical_symbol("0700.HK")
        self.instrument_id = instrument.instrument_id
        self.database.connection.execute(
            """
            INSERT INTO financial_research_runs(
                id, trigger_type, scope_type, instrument_id, status
            ) VALUES('conflict-run', 'chat', 'instrument', ?, 'completed')
            """,
            (self.instrument_id,),
        )
        normalized = json.dumps(
            {
                "metric": "last_price",
                "value": {"kind": "scalar", "number": 500.0},
                "period": PERIOD,
                "as_of": "2026-07-31T03:00:00Z",
                "adjustment": "raw",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        cursor = self.database.connection.execute(
            """
            INSERT INTO financial_claims(
                research_run_id, claim_key, claim_type, subject, statement,
                normalized_value_json, unit, currency, observed_at,
                verification_status
            ) VALUES('conflict-run', 'persisted-conflict', 'fact', ?,
                     '腾讯股价为500港元', ?, '港元', 'HKD',
                     '2026-07-31T03:00:00Z', 'verified_current')
            """,
            (SUBJECT, normalized),
        )
        self.claim_id = int(cursor.lastrowid)
        self.evidence_ids = []
        for index, (provider, value, authority, source) in enumerate(
            (
                ("akshare_cn", 500.0, 0.72, "eastmoney"),
                ("tushare_cn", 510.0, 0.85, "tushare"),
            ),
            start=1,
        ):
            self.database.connection.execute(
                """
                INSERT INTO financial_provider_profiles(
                    provider_key, display_name, provider_type, access_tier,
                    priority, is_enabled, health_status, metadata_json
                ) VALUES(?, ?, 'fixture', 'fixture', ?, 1, 'healthy', ?)
                """,
                (
                    provider,
                    provider,
                    20 if provider == "akshare_cn" else 10,
                    json.dumps({"source_family": source}),
                ),
            )
            provider_id = int(self.database.connection.execute(
                "SELECT id FROM financial_provider_profiles WHERE provider_key=?",
                (provider,),
            ).fetchone()[0])
            payload = json.dumps(
                {
                    "metric": "last_price",
                    "value": value,
                    "unit": "港元",
                    "currency": "HKD",
                    "adjustment": "raw",
                    "period": PERIOD,
                    "underlying_source_id": source,
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
                ) VALUES(?, ?, ?, 'quote', '2026-07-31T03:00:00Z',
                         '2026-07-31T03:00:10Z', 'open', 'HKD',
                         'Asia/Hong_Kong', 'verified', ?, ?, ?)
                """,
                (
                    f"conflict-{index}",
                    self.instrument_id,
                    provider_id,
                    payload,
                    digest,
                    f"https://{source}.example/quote",
                ),
            )
            snapshot_id = int(cursor.lastrowid)
            cursor = self.database.connection.execute(
                """
                INSERT INTO financial_claim_evidence(
                    claim_id, snapshot_id, provider_profile_id, evidence_type,
                    relationship, evidence_json, authority_score, observed_at,
                    fetched_at
                ) VALUES(?, ?, ?, 'structured_snapshot', 'supports', '{}', ?,
                         '2026-07-31T03:00:00Z', '2026-07-31T03:00:10Z')
                """,
                (self.claim_id, snapshot_id, provider_id, authority),
            )
            self.evidence_ids.append(int(cursor.lastrowid))
        rationale = json.dumps(
            {
                "judge_version": "financial-temporal-judge-v1",
                "selected_evidence_ids": self.evidence_ids,
                "ignored_evidence_ids": [],
            },
            sort_keys=True,
        )
        self.database.connection.execute(
            """
            INSERT INTO financial_verdicts(
                claim_id, adjudication_version, verdict, rationale,
                selected_evidence_ids_json, adjudicator, decided_at
            ) VALUES(?, 1, 'verified_current', ?, ?,
                     'financial-temporal-judge-v1', '2026-07-31T03:02:00Z')
            """,
            (self.claim_id, rationale, json.dumps(self.evidence_ids)),
        )
        self.service = FinancialConflictJudgeService(self.database)

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def test_unresolved_conflict_is_versioned_and_exposed_as_existing_table_review_queue(self):
        first = self.service.judge_and_persist_claim(self.claim_id)
        self.assertEqual(first["verdict"], "unresolved_conflict")
        self.assertEqual(first["adjudication_version"], 2)
        self.assertEqual(first["claim_verification_status"], "conflicted")
        self.assertTrue(first["persisted"])
        queue = self.service.list_pending_human_review()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["claim_id"], self.claim_id)
        self.assertTrue(queue[0]["decision"]["human_review_required"])

        duplicate = self.service.judge_and_persist_claim(self.claim_id)
        self.assertFalse(duplicate["persisted"])
        self.assertEqual(duplicate["verdict_id"], first["verdict_id"])

    def test_resolved_consensus_removes_item_from_review_queue_and_restores_temporal_status(self):
        self.service.judge_and_persist_claim(self.claim_id)
        row = self.database.connection.execute(
            """
            SELECT snapshot.id, snapshot.payload_json
            FROM financial_data_snapshots snapshot
            JOIN financial_provider_profiles profile ON profile.id=snapshot.provider_profile_id
            WHERE profile.provider_key='tushare_cn'
            """
        ).fetchone()
        payload = json.loads(row[1])
        payload["value"] = 500.2
        payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
        self.database.connection.execute(
            "UPDATE financial_data_snapshots SET payload_json=?, payload_sha256=? WHERE id=?",
            (payload_text, digest, int(row[0])),
        )
        resolved = self.service.judge_and_persist_claim(self.claim_id)
        self.assertEqual(resolved["verdict"], "verified_consensus")
        self.assertEqual(resolved["claim_verification_status"], "verified_current")
        self.assertEqual(resolved["adjudication_version"], 3)
        self.assertEqual(self.service.list_pending_human_review(), [])
        self.assertEqual(
            self.database.connection.execute(
                "SELECT verification_status FROM financial_claims WHERE id=?",
                (self.claim_id,),
            ).fetchone()[0],
            "verified_current",
        )

    def test_missing_claim_fails_closed(self):
        result = self.service.judge_and_persist_claim(999999)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "claim_not_found")


if __name__ == "__main__":
    unittest.main()

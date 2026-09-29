import json
import tempfile
import unittest
from pathlib import Path

from financial_claim_extractor import (
    FinancialClaimExtractor,
    FinancialClaimService,
    NUMERIC_COVERAGE_THRESHOLD,
    validate_claim_extraction,
)
from financial_instruments import InstrumentRegistry
from sqlite_database import SQLiteDatabase


FIXTURE = Path(__file__).parent / "fixtures" / "financial_claim_labeled.json"
TARGET = {
    "instrument_id": 1,
    "canonical_symbol": "0700.HK",
    "display_name": "腾讯控股",
    "asset_type": "equity",
    "market": "HK",
    "exchange": "XHKG",
    "country_code": "HK",
}


class FinancialClaimExtractorTests(unittest.TestCase):
    def extract(self, text, **kwargs):
        result = FinancialClaimExtractor(**kwargs.pop("extractor_options", {})).extract(
            text,
            source_kind="chat_answer",
            source_ref="chat_history:7:answer",
            subject=TARGET,
            as_of="2026-07-31T03:00:00Z",
            **kwargs,
        )
        self.assertEqual(validate_claim_extraction(result), result)
        return result

    def test_numeric_percentage_range_period_and_exact_source_spans(self):
        result = self.extract(
            "腾讯股价为500港元，营收为100亿元，同比增长8%，净利润环比下降3.5%。"
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["coverage"]["key_numeric_coverage"], 1.0)
        by_metric = {claim["metric"]: claim for claim in result["claims"]}
        self.assertEqual(by_metric["last_price"]["value"]["number"], 500.0)
        self.assertEqual(by_metric["last_price"]["currency"], "HKD")
        self.assertEqual(by_metric["revenue"]["unit"], "亿元")
        self.assertEqual(by_metric["revenue_yoy"]["value"]["number"], 8.0)
        self.assertEqual(by_metric["net_income_qoq"]["value"]["number"], -3.5)
        for claim in result["claims"]:
            span = claim["source_span"]
            self.assertEqual(span["text"], result_text(span, "腾讯股价为500港元，营收为100亿元，同比增长8%，净利润环比下降3.5%。"))
            self.assertEqual(claim["subject"]["instrument_key"], "HK:XHKG:EQUITY:00700")

    def test_prediction_opinion_negation_announcement_and_membership_are_separate(self):
        result = self.extract(
            "如果盈利改善，目标价为480至520港元。我们建议持有，估值偏高。"
            "公司尚未宣布分红。腾讯仍为恒生指数成分股。"
        )
        types = {claim["claim_type"] for claim in result["claims"]}
        self.assertIn("conditional_prediction", types)
        self.assertIn("opinion", types)
        self.assertIn("fact", types)
        target = next(claim for claim in result["claims"] if claim["metric"] == "target_price")
        self.assertEqual(target["value"], {"kind": "range", "min": 480.0, "max": 520.0})
        self.assertEqual(target["verification_status"], "prediction_not_fact")
        negative = next(
            claim for claim in result["claims"]
            if claim["metric"] == "announced_event" and "尚未" in claim["statement"]
        )
        self.assertEqual(negative["polarity"], "negative")
        membership = next(claim for claim in result["claims"] if claim["metric"] == "index_membership")
        self.assertTrue(membership["value"]["boolean"])

    def test_structured_template_wins_over_duplicate_text_rule(self):
        result = self.extract(
            "股价为500港元。",
            structured_claims=[{
                "statement": "股价为500港元",
                "metric": "last_price",
                "value": {"kind": "scalar", "number": 500.0},
                "unit": "港元",
                "claim_type": "fact",
            }],
        )
        matching = [claim for claim in result["claims"] if claim["metric"] == "last_price"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["extraction_method"], "structured_template")

    def test_llm_fallback_accepts_only_exact_spans_and_grounded_numbers(self):
        calls = []

        def grounded(text, **_kwargs):
            calls.append(text)
            return [
                {
                    "source_text": "自由现金流转正",
                    "claim_type": "fact",
                    "metric": "free_cash_flow_positive",
                    "value": {"kind": "boolean", "boolean": True},
                    "unit": "",
                    "currency": "",
                    "period": {"kind": "reported", "label": "2099"},
                    "as_of": "2099-01-01T00:00:00Z",
                },
                {
                    "source_text": "不存在的现金流为30亿元",
                    "claim_type": "fact",
                    "metric": "free_cash_flow",
                    "value": {"kind": "scalar", "number": 30},
                    "unit": "亿元",
                    "currency": "CNY",
                    "period": {},
                    "as_of": "2026-07-31T03:00:00Z",
                },
                {
                    "source_text": "自由现金流转正",
                    "claim_type": "fact",
                    "metric": "free_cash_flow_currency",
                    "value": {"kind": "boolean", "boolean": True},
                    "unit": "USD",
                    "currency": "USD",
                    "period": {},
                    "as_of": "2026-07-31T03:00:00Z",
                },
            ]

        result = self.extract(
            "自由现金流转正。",
            extractor_options={"llm_extractor": grounded},
        )
        self.assertTrue(result["llm_used"])
        self.assertEqual(calls, ["自由现金流转正。"])
        self.assertEqual(
            [claim["metric"] for claim in result["claims"]],
            ["free_cash_flow_positive"],
        )
        self.assertEqual(result["claims"][0]["as_of"], "2026-07-31T03:00:00Z")
        self.assertEqual(result["claims"][0]["period"]["kind"], "instant")
        self.assertIn("llm_source_span_not_grounded", result["errors"])
        self.assertIn("llm_unit_or_currency_not_grounded", result["errors"])

        ungrounded_value = FinancialClaimExtractor(
            llm_extractor=lambda _text, **_kwargs: [{
                "source_text": "自由现金流为20亿元",
                "claim_type": "fact",
                "metric": "free_cash_flow",
                "value": {"kind": "scalar", "number": 30},
                "unit": "亿元",
                "currency": "CNY",
                "period": {},
                "as_of": "2026-07-31T03:00:00Z",
            }]
        ).extract(
            "自由现金流为20亿元。",
            source_kind="chat_answer",
            source_ref="chat_history:8:answer",
            subject=TARGET,
        )
        self.assertEqual(ungrounded_value["claims"], [])
        self.assertEqual(ungrounded_value["status"], "partial")
        self.assertIn("llm_value_not_grounded", ungrounded_value["errors"])

    def test_approved_labeled_set_reaches_numeric_coverage_threshold(self):
        dataset = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(dataset["review_status"], "approved")
        total_numeric = 0
        covered_numeric = 0
        for case in dataset["examples"]:
            with self.subTest(text=case["text"]):
                result = self.extract(case["text"])
                metrics = {claim["metric"] for claim in result["claims"]}
                types = {claim["claim_type"] for claim in result["claims"]}
                self.assertTrue(set(case["metrics"]) <= metrics)
                self.assertTrue(set(case["types"]) <= types)
                total_numeric += result["coverage"]["key_numeric_span_count"]
                covered_numeric += result["coverage"]["covered_key_numeric_span_count"]
        coverage = covered_numeric / total_numeric
        self.assertGreaterEqual(coverage, NUMERIC_COVERAGE_THRESHOLD)


def result_text(span, text):
    return text[span["start"]:span["end"]]


class FinancialClaimServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "claims.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        registry = InstrumentRegistry(self.database.connection)
        registry.load_controlled_seed()
        instrument = registry.get_by_canonical_symbol("0700.HK")
        self.database.connection.execute(
            """
            INSERT INTO financial_research_runs(
                id, trigger_type, scope_type, instrument_id, status
            ) VALUES('claim-run', 'chat', 'instrument', ?, 'completed')
            """,
            (instrument.instrument_id,),
        )
        cursor = self.database.connection.execute(
            """
            INSERT INTO financial_final_reports(
                research_run_id, report_version, report_status, recommendation,
                confidence, title, executive_summary, report_markdown, observed_at
            ) VALUES('claim-run', 1, 'completed', 'hold', 0.72, '腾讯报告',
                     '腾讯股价为500港元，建议持有。', '完整报告',
                     '2026-07-31T03:00:00Z')
            """
        )
        self.report_id = int(cursor.lastrowid)
        self.database.connection.execute(
            """
            INSERT INTO financial_report_sections(
                research_run_id, role_key, section_type, sequence_no, status,
                content_markdown
            ) VALUES('claim-run', 'fundamentals_analyst', 'analysis', 1,
                     'completed', '营收为100亿元，同比增长8%。')
            """
        )
        self.service = FinancialClaimService(self.database)

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def test_report_claims_persist_idempotently_with_section_trace_and_nonfacts(self):
        first = self.service.extract_and_persist_report(self.report_id)
        self.assertEqual(first["status"], "complete")
        self.assertGreaterEqual(first["persisted_claim_count"], 5)
        rows = self.database.connection.execute(
            """
            SELECT claim_key, claim_type, subject, normalized_value_json,
                   verification_status
            FROM financial_claims WHERE research_run_id='claim-run'
            """
        ).fetchall()
        self.assertTrue(all(row[2] == "HK:XHKG:EQUITY:00700" for row in rows))
        payloads = [json.loads(row[3]) for row in rows]
        self.assertTrue(any(
            payload["source_span"].get("source_ref", "").startswith(
                f"report:{self.report_id}:section:"
            )
            for payload in payloads
        ))
        opinion = next(row for row in rows if row[1] == "opinion")
        self.assertEqual(opinion[4], "opinion_not_fact")
        fact = next(row for row in rows if row[1] == "fact")
        self.assertEqual(fact[4], "pending")

        self.database.connection.execute(
            "UPDATE financial_claims SET verification_status='verified_current' WHERE claim_key=?",
            (fact[0],),
        )
        second = self.service.extract_and_persist_report(self.report_id)
        self.assertEqual(second["persisted_claim_count"], first["persisted_claim_count"])
        self.assertEqual(
            self.database.connection.execute(
                "SELECT verification_status FROM financial_claims WHERE claim_key=?",
                (fact[0],),
            ).fetchone()[0],
            "verified_current",
        )

    def test_missing_and_nonterminal_reports_fail_closed_without_rows(self):
        missing = self.service.extract_and_persist_report(999999)
        self.assertEqual(missing["errors"], ["report_not_found"])
        self.database.connection.execute(
            "UPDATE financial_final_reports SET report_status='draft' WHERE id=?",
            (self.report_id,),
        )
        draft = self.service.extract_and_persist_report(self.report_id)
        self.assertEqual(draft["errors"], ["report_not_terminal"])
        self.assertEqual(
            self.database.connection.execute("SELECT COUNT(*) FROM financial_claims").fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()

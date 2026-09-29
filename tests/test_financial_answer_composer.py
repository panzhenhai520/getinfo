import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import chat_api
from financial_answer_composer import (
    FINANCIAL_ANSWER_DISCLAIMER,
    FinancialAnswerComposer,
    FinancialAnswerComposerService,
    compose_full_research_answer,
    compose_market_scope_answer,
    compose_realtime_query_answer,
)
from financial_chat_market_scope import skipped_market_scope
from financial_full_research import skipped_full_research
from financial_realtime_query import skipped_realtime_query
from sqlite_database import SQLiteDatabase


TIME_CONTEXT = {
    "server_now_utc": "2026-07-31T03:00:00Z",
    "requested_as_of": "2026-07-31T03:00:00Z",
    "server_timezone": "Asia/Hong_Kong",
    "user_timezone": "Asia/Hong_Kong",
    "resolved_time_expression": "今天→2026-07-31T00:00:00+08:00/2026-08-01T00:00:00+08:00",
}


def _target(symbol="0700.HK", name="腾讯控股", market="XHKG", currency="HKD"):
    return {
        "instrument_id": 1,
        "instrument_key": f"{'HK' if market == 'XHKG' else 'CN'}:{market}:EQUITY:{symbol.split('.')[0]}",
        "canonical_symbol": symbol,
        "display_name": name,
        "asset_type": "equity",
        "market": market,
        "exchange": market,
        "currency": currency,
        "country_code": "HK" if market == "XHKG" else "CN",
    }


def _fact(*, target=None, value=500.0, as_of="2026-07-31T02:59:58Z", status="verified_current", citation=True):
    return {
        "claim_id": 11,
        "metric": "last_price",
        "value": {"kind": "scalar", "number": value},
        "unit": "",
        "currency": (target or _target())["currency"],
        "statement": f"当前价格为 {value:g}",
        "as_of": as_of,
        "verification_status": status,
        "conflict_verdict": "verified_consensus",
        "target": target or _target(),
        "citations": (
            [{"snapshot_id": 41, "title": "交易所行情", "observed_at": as_of}]
            if citation else []
        ),
    }


def _report(*, as_of="2026-07-31T02:58:00Z"):
    return {
        "report_id": 7,
        "research_run_id": "research-run-7",
        "report_status": "verified",
        "recommendation": "Hold / 观察",
        "confidence": 0.73,
        "as_of": as_of,
        "observed_at": as_of,
        "verified_at": "2026-07-31T02:59:00Z",
        "title": "腾讯控股 TradingAgents 终极报告",
        "executive_summary": "多空讨论后维持原评级。",
        "risk_summary": {"summary": "估值与监管风险仍在。"},
        "counter_evidence": "空方指出盈利预期可能下修。",
        "target": _target(),
        "saved": True,
    }


class FinancialAnswerComposerTest(unittest.TestCase):
    def test_fact_only_requires_clickable_trace_and_uses_server_time(self):
        answer = FinancialAnswerComposer().compose(
            server_time_context=TIME_CONTEXT,
            targets=[_target()],
            facts=[_fact()],
        )
        self.assertEqual(answer["status"], "ready")
        self.assertEqual(answer["request_time"]["clock_source"], "application_server")
        self.assertEqual(answer["request_time"]["server_now_utc"], "2026-07-31T03:00:00Z")
        self.assertEqual(answer["current_facts"][0]["citations"][0]["url"], "/api/financial/snapshots/41")
        self.assertIn("[交易所行情](/api/financial/snapshots/41)", answer["answer_markdown"])
        self.assertIn(FINANCIAL_ANSWER_DISCLAIMER, answer["answer_markdown"])
        self.assertEqual(answer["boundaries"]["model_calls"], 0)

    def test_fact_and_report_preserve_rating_confidence_asof_risk_and_counter_evidence(self):
        report = _report()
        answer = FinancialAnswerComposer().compose(
            server_time_context=TIME_CONTEXT,
            targets=[_target()],
            facts=[_fact(as_of="2026-07-31T02:59:58Z")],
            reports=[report],
        )
        projected = answer["research_reports"][0]
        self.assertEqual(projected["recommendation"], report["recommendation"])
        self.assertEqual(projected["confidence"], report["confidence"])
        self.assertEqual(projected["as_of"], report["as_of"])
        self.assertEqual(projected["freshness"], "older_than_current_facts")
        self.assertIn("报告时点 2026-07-31T02:58:00Z 早于最新事实", answer["answer_markdown"])
        self.assertIn("评级=Hold / 观察", answer["answer_markdown"])
        self.assertIn("估值与监管风险仍在", answer["answer_markdown"])
        self.assertIn("盈利预期可能下修", answer["answer_markdown"])

    def test_unverified_partial_or_uncited_fact_never_leaks_number(self):
        unverified = _fact(value=999999.0, status="insufficient_evidence")
        uncited = _fact(value=888888.0, citation=False)
        unadjudicated = _fact(value=777777.0)
        unadjudicated["conflict_verdict"] = ""
        answer = FinancialAnswerComposer().compose(
            server_time_context=TIME_CONTEXT,
            targets=[_target()],
            facts=[unverified, uncited, unadjudicated],
        )
        self.assertEqual(answer["status"], "insufficient_evidence")
        self.assertEqual(answer["current_facts"], [])
        self.assertNotIn("999999", answer["answer_markdown"])
        self.assertNotIn("888888", answer["answer_markdown"])
        self.assertNotIn("777777", answer["answer_markdown"])
        self.assertEqual(
            {item["code"] for item in answer["conflicts_and_gaps"]},
            {
                "fact_not_verified", "fact_missing_clickable_citation",
                "fact_conflict_not_resolved",
            },
        )

        nonterminal = _report()
        nonterminal["report_status"] = "running"
        report_answer = FinancialAnswerComposer().compose(
            server_time_context=TIME_CONTEXT,
            reports=[nonterminal],
        )
        self.assertEqual(report_answer["research_reports"], [])
        self.assertEqual(
            report_answer["conflicts_and_gaps"][0]["code"],
            "report_not_saved_or_terminal",
        )

    def test_multiple_markets_and_historical_fact_remain_separate(self):
        cn = _target("600000.SH", "浦发银行", "XSHG", "CNY")
        hk = _target()
        cn_fact = _fact(target=cn, value=10.25)
        cn_fact["claim_id"] = 12
        hk_history = _fact(target=hk, value=499.0, status="verified_historical", as_of="2026-07-30T08:00:00Z")
        hk_history["claim_id"] = 13
        answer = FinancialAnswerComposer().compose(
            server_time_context=TIME_CONTEXT,
            targets=[cn, hk],
            facts=[cn_fact, hk_history],
        )
        self.assertEqual(len(answer["targets"]), 2)
        self.assertEqual(len(answer["current_facts"]), 1)
        self.assertEqual(len(answer["historical_facts"]), 1)
        self.assertIn("浦发银行", answer["answer_markdown"])
        self.assertIn("腾讯控股", answer["answer_markdown"])
        self.assertIn("历史有效", answer["answer_markdown"])

    def test_relative_chinese_time_is_pre_resolved_not_computed_by_model(self):
        answer = FinancialAnswerComposer().compose(
            server_time_context=TIME_CONTEXT,
            targets=[_target()],
            facts=[_fact()],
        )
        self.assertTrue(answer["request_time"]["resolved_time_expression"].startswith("今天→"))
        self.assertEqual(answer["request_time"]["requested_as_of"], TIME_CONTEXT["requested_as_of"])
        with self.assertRaises(ValueError):
            FinancialAnswerComposer().compose(
                server_time_context={"server_now_utc": "今天", "requested_as_of": "现在"}
            )

    def test_route_adapters_preserve_stream_answer_boundaries(self):
        realtime = {
            **skipped_realtime_query("fixture"),
            "status": "ready",
            "target": _target(),
            "requested_at_utc": TIME_CONTEXT["server_now_utc"],
            "completed_at_utc": TIME_CONTEXT["server_now_utc"],
            "market_session": {"market_session_state": "open"},
            "evidence": [{
                "snapshot_id": 41, "provider_id": "easyquotation",
                "provider_display_name": "EasyQuotation", "price": 500.0,
                "currency": "HKD", "observed_at": "2026-07-31T02:59:58Z",
                "fetched_at": "2026-07-31T02:59:59Z", "market_status": "open",
                "source_url": "https://qt.gtimg.cn/q=hk00700",
            }],
            "answer_allowed": True,
            "numeric_claims_allowed": True,
        }
        answer = compose_realtime_query_answer(realtime, TIME_CONTEXT)
        self.assertIn("snapshot #41", answer["answer_markdown"])
        self.assertIn("## 已核验当前事实", answer["answer_markdown"])
        self.assertEqual(answer["current_facts"], [])
        self.assertNotIn("价格为 500", answer["answer_markdown"])

        consensus = {
            **realtime,
            "evidence": [
                *realtime["evidence"],
                {
                    "snapshot_id": 42, "provider_id": "yahoo",
                    "provider_display_name": "Yahoo Finance", "price": 500.02,
                    "currency": "HKD", "observed_at": "2026-07-31T02:59:58Z",
                    "fetched_at": "2026-07-31T02:59:59Z", "market_status": "open",
                    "source_url": "https://finance.yahoo.com/quote/0700.HK",
                },
            ],
        }
        consensus_answer = compose_realtime_query_answer(consensus, TIME_CONTEXT)
        self.assertEqual(len(consensus_answer["current_facts"]), 1)
        self.assertEqual(
            consensus_answer["current_facts"][0]["conflict_verdict"],
            "verified_consensus",
        )

        conflict = {**realtime, "status": "conflict", "numeric_claims_allowed": False}
        conflict_answer = compose_realtime_query_answer(conflict, TIME_CONTEXT)
        self.assertEqual(conflict_answer["current_facts"], [])
        self.assertIn("不选择单一实时价格", conflict_answer["answer_markdown"])

        pending_market = skipped_market_scope("fixture")
        pending_market.update({
            "status": "refresh_queued", "universe": {"display_name": "A股市场"},
            "refresh": {"server_now_utc": TIME_CONTEXT["server_now_utc"]},
            "route_destination": "financial_market_scope",
        })
        self.assertEqual(compose_market_scope_answer(pending_market, TIME_CONTEXT)["status"], "pending")

        pending_research = skipped_full_research("fixture")
        pending_research.update({
            "status": "queued", "requested_at_utc": TIME_CONTEXT["server_now_utc"],
            "completed_at_utc": TIME_CONTEXT["server_now_utc"],
            "route_destination": "financial_full_research",
            "jobs": [],
        })
        self.assertEqual(compose_full_research_answer(pending_research, TIME_CONTEXT)["status"], "pending")


class FinancialAnswerComposerServiceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.directory.name) / "answer.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self._seed()
        self.service = FinancialAnswerComposerService(self.database)

    def tearDown(self):
        self.database.disconnect()
        self.directory.cleanup()

    def _seed(self):
        connection = self.database.connection
        instrument_id = int(connection.execute(
            """
            INSERT INTO financial_instruments(
                canonical_symbol, display_name, asset_type, market, exchange,
                currency, country_code
            ) VALUES('0700.HK','腾讯控股','equity','XHKG','XHKG','HKD','HK')
            """
        ).lastrowid)
        provider_id = int(connection.execute(
            """
            INSERT INTO financial_provider_profiles(
                provider_key, display_name, provider_type, access_tier,
                capabilities_json, is_enabled, health_status
            ) VALUES('exchange_official','交易所官方','official','free','["quote"]',1,'healthy')
            """
        ).lastrowid)
        connection.execute(
            """
            INSERT INTO financial_research_runs(
                id, trigger_type, scope_type, instrument_id, status, completed_at
            ) VALUES('run-answer','chat','instrument',?,'completed','2026-07-31T03:00:00Z')
            """,
            (instrument_id,),
        )
        report_id = int(connection.execute(
            """
            INSERT INTO financial_final_reports(
                research_run_id, report_status, recommendation, confidence,
                title, executive_summary, report_json, risk_summary_json,
                disclaimer, observed_at, fetched_at, verified_at
            ) VALUES(
                'run-answer','verified','Hold / 观察',0.73,'腾讯终极报告',
                '原样执行摘要','{"as_of":"2026-07-31T02:58:00Z"}',
                '{"summary":"估值风险"}','不构成投资建议',
                '2026-07-31T02:58:00Z','2026-07-31T02:58:30Z','2026-07-31T02:59:00Z'
            )
            """
        ).lastrowid)
        connection.execute(
            """
            INSERT INTO financial_report_sections(
                research_run_id, role_key, section_type, sequence_no, status,
                content_markdown
            ) VALUES('run-answer','bear_researcher','analysis',1,'completed','盈利可能低于市场预期。')
            """
        )
        payload = {
            "value": 500.0,
            "currency": "HKD",
            "normalized_payload": {"last_price": 500.0, "change_percent": 0.2},
        }
        payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        snapshot_id = int(connection.execute(
            """
            INSERT INTO financial_data_snapshots(
                snapshot_key, instrument_id, provider_profile_id, data_type,
                observed_at, fetched_at, market_status, currency, timezone,
                quality_status, payload_json, payload_sha256, source_url
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "answer-snapshot", instrument_id, provider_id, "quote",
                "2026-07-31T02:59:58Z", "2026-07-31T02:59:59Z", "open",
                "HKD", "Asia/Hong_Kong", "normalized", payload_text,
                hashlib.sha256(payload_text.encode("utf-8")).hexdigest(),
                "https://example.test/exchange/0700",
            ),
        ).lastrowid)
        normalized = {
            "metric": "last_price", "value": {"kind": "scalar", "number": 500.0},
            "as_of": "2026-07-31T02:59:58Z",
            "subject": _target(),
        }
        claim_id = int(connection.execute(
            """
            INSERT INTO financial_claims(
                research_run_id, final_report_id, claim_key, claim_type,
                subject, statement, normalized_value_json, unit, currency,
                observed_at, verification_status
            ) VALUES('run-answer',?,'price-claim','fact','0700.HK',
                     '当前价格为 500',?,'','HKD','2026-07-31T02:59:58Z','verified_current')
            """,
            (report_id, json.dumps(normalized, ensure_ascii=False)),
        ).lastrowid)
        evidence_id = int(connection.execute(
            """
            INSERT INTO financial_claim_evidence(
                claim_id, snapshot_id, provider_profile_id, evidence_type,
                relationship, source_url, source_title, evidence_json,
                authority_score, observed_at, fetched_at
            ) VALUES(?,?,?,'structured_snapshot','supports',?,?,'{}',1.0,?,?)
            """,
            (
                claim_id, snapshot_id, provider_id,
                "https://example.test/exchange/0700", "交易所官方行情",
                "2026-07-31T02:59:58Z", "2026-07-31T02:59:59Z",
            ),
        ).lastrowid)
        connection.execute(
            """
            INSERT INTO financial_verdicts(
                claim_id, adjudication_version, verdict, rationale,
                selected_evidence_ids_json, adjudicator, decided_at
            ) VALUES(?,1,'verified_current',? ,?,'financial-temporal-judge-v1','2026-07-31T03:00:00Z')
            """,
            (
                claim_id,
                json.dumps({"selected_evidence_ids": [evidence_id]}),
                json.dumps([evidence_id]),
            ),
        )
        connection.execute(
            """
            INSERT INTO financial_verdicts(
                claim_id, adjudication_version, verdict, rationale,
                selected_evidence_ids_json, adjudicator, decided_at
            ) VALUES(?,2,'verified_authoritative',?,?,'financial-conflict-judge-v1','2026-07-31T03:00:01Z')
            """,
            (
                claim_id,
                json.dumps({
                    "selected_evidence_ids": [evidence_id],
                    "decision_value": {"number": 500.0, "unit": "", "currency": "HKD"},
                }),
                json.dumps([evidence_id]),
            ),
        )
        self.report_id = report_id
        self.snapshot_id = snapshot_id

    def test_service_loads_only_dual_judged_fact_and_public_report(self):
        facts = self.service.load_verified_facts([self.report_id])
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["verification_status"], "verified_current")
        self.assertEqual(facts[0]["conflict_verdict"], "verified_authoritative")
        self.assertEqual(facts[0]["citations"][0]["snapshot_id"], self.snapshot_id)
        answer = self.service.compose_reports(
            [self.report_id], server_time_context=TIME_CONTEXT
        )
        self.assertEqual(answer["research_reports"][0]["recommendation"], "Hold / 观察")
        self.assertEqual(answer["current_facts"][0]["claim_id"], facts[0]["claim_id"])

    def test_public_snapshot_is_allow_listed_and_integrity_checked(self):
        snapshot = self.service.public_snapshot(self.snapshot_id)
        self.assertEqual(snapshot["values"]["last_price"], 500.0)
        self.assertEqual(snapshot["provider"]["provider_id"], "exchange_official")
        self.assertNotIn("payload_json", snapshot)
        self.database.connection.execute(
            "UPDATE financial_data_snapshots SET payload_json='{}' WHERE id=?",
            (self.snapshot_id,),
        )
        self.assertIsNone(self.service.public_snapshot(self.snapshot_id))

    def test_authenticated_snapshot_citation_endpoint(self):
        app = Flask(__name__)
        app.config.update(TESTING=True)
        app.register_blueprint(chat_api.chat_bp)
        client = app.test_client()
        with patch("sqlite_database.sqlite_db", self.database), patch(
            "decorators.user_db.verify_session",
            return_value={"user_id": 1, "role": "admin"},
        ), patch(
            "intel_database.intel_repository.active_industry_pack_id",
            return_value="family_office",
        ), patch.object(
            chat_api._cfg, "FINANCIAL_INTELLIGENCE_ENABLED", True,
        ):
            response = client.get(
                f"/api/financial/snapshots/{self.snapshot_id}",
                headers={"Authorization": "Bearer fixture"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()["snapshot"]
        self.assertEqual(payload["snapshot_id"], self.snapshot_id)
        self.assertNotIn("payload_json", payload)


if __name__ == "__main__":
    unittest.main()

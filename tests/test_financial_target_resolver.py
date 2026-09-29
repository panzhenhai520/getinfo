import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import chat_api
from chat_route_orchestrator import ChatFinancialRouteStore, ChatRouteOrchestrator
from financial_instruments import InstrumentRegistry
from financial_intent_classifier import FinancialIntentClassifier
from financial_target_resolver import (
    FinancialTargetResolver,
    TARGET_RESOLUTION_SCHEMA,
    validate_target_resolution,
)
from sqlite_database import SQLiteDatabase


UTC = timezone.utc
ENABLED = {"FINANCIAL_INTELLIGENCE_ENABLED": True}


def _at(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _payload(question, *, session_id="target-session", messages=None):
    return {
        "session_id": session_id,
        "model": "local",
        "messages": messages or [{"role": "user", "content": question}],
        "web_search": False,
        "user_timezone": "Asia/Hong_Kong",
    }


class FinancialTargetResolverTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "target.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.registry = InstrumentRegistry(self.database.connection)
        self.registry.load_controlled_seed()
        self.classifier = FinancialIntentClassifier(self.registry)
        self.resolver = FinancialTargetResolver(self.registry)
        self.store = ChatFinancialRouteStore(self.database)

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def intent(self, question):
        return self.classifier.classify(question)

    def resolve(self, question, **kwargs):
        result = self.resolver.resolve(question, self.intent(question), **kwargs)
        self.assertEqual(validate_target_resolution(result), result)
        return result

    def orchestrator(self):
        return ChatRouteOrchestrator(
            clock=lambda: _at("2026-07-31T02:00:00Z"),
            store=self.store,
            financial_settings=ENABLED,
            intent_classifier=self.classifier,
            target_resolver=self.resolver,
        )

    def test_schema_and_unique_tencent_apple_auto_continue_with_echo(self):
        self.assertEqual(TARGET_RESOLUTION_SCHEMA["additionalProperties"], False)
        for question, symbol in (
            ("分析腾讯控股的风险", "0700.HK"),
            ("Apple 股票最新价格", "AAPL.US"),
        ):
            with self.subTest(question=question):
                result = self.resolve(question, request_id="unique-target")
                self.assertEqual(result["status"], "resolved")
                self.assertFalse(result["needs_clarification"])
                self.assertEqual(result["targets"][0]["canonical_symbol"], symbol)
                self.assertTrue(result["targets"][0]["instrument_key"])
                self.assertIn(symbol, result["target_echo"])

    def test_bare_six_digit_never_guesses_even_when_registry_has_one_candidate(self):
        ambiguous = self.resolve("000001 怎么样", request_id="bare-ambiguous")
        self.assertEqual(ambiguous["status"], "clarification_required")
        self.assertEqual(
            {item["canonical_symbol"] for item in ambiguous["clarification"]["options"]},
            {"000001.SH", "000001.SZ"},
        )
        unique_today = self.resolve("600000 怎么样", request_id="bare-unique")
        self.assertEqual(unique_today["status"], "clarification_required")
        self.assertEqual(unique_today["clarification"]["field"], "instrument")

        qualified = self.resolve("上交所的 000001 指数", request_id="qualified")
        self.assertEqual(qualified["status"], "resolved")
        self.assertEqual(qualified["targets"][0]["canonical_symbol"], "000001.SH")
        self.assertEqual(
            qualified["targets"][0]["instrument_key"],
            "CN:XSHG:INDEX:000001",
        )

    def test_old_persisted_target_is_enriched_with_stable_key(self):
        current = self.resolve("腾讯股价", request_id="backward-compatible")
        old_target = dict(current["targets"][0])
        old_target.pop("instrument_key")
        inherited = self.resolver.resolve(
            "那它现在呢？",
            self.intent("那它现在呢？"),
            prior_state={
                "route_status": "target_resolved",
                "route_key": "old-route",
                "raw_question": "腾讯股价",
                "resolved_targets": [old_target],
            },
            request_id="old-history",
        )
        self.assertEqual(
            inherited["targets"][0]["instrument_key"],
            "HK:XHKG:EQUITY:00700",
        )

    def test_fund_share_class_and_currency_are_mandatory(self):
        shares = self.resolve("易方达沪深300ETF联接怎么样", request_id="fund-shares")
        self.assertEqual(shares["status"], "clarification_required")
        self.assertEqual(shares["clarification"]["field"], "share_class")
        share_c = self.resolve("易方达沪深300ETF联接C怎么样", request_id="fund-c")
        self.assertEqual(share_c["targets"][0]["share_class"], "C")

        for symbol, market, exchange, currency in (
            ("FUND-CNY.TEST", "CN_FUND", "", "CNY"),
            ("FUND-HKD.TEST", "XHKG", "XHKG", "HKD"),
        ):
            self.registry.upsert_instrument(
                {
                    "canonical_symbol": symbol,
                    "display_name": "同名基金",
                    "asset_type": "fund",
                    "market": market,
                    "exchange": exchange,
                    "currency": currency,
                    "country_code": "CN" if currency == "CNY" else "HK",
                    "listed_at": "2020-01-01",
                    "aliases": ["同名基金份额"],
                }
            )
        currency = self.resolve("同名基金份额净值", request_id="fund-currency")
        self.assertEqual(currency["status"], "clarification_required")
        self.assertEqual(currency["clarification"]["field"], "currency")
        hkd = self.resolve("港币同名基金份额净值", request_id="fund-hkd")
        self.assertEqual(hkd["targets"][0]["currency"], "HKD")

    def test_llm_ranker_cannot_guess_bare_code_fund_share_or_ungrounded_candidate(self):
        calls = []

        def ranker(question, candidates, **_kwargs):
            calls.append((question, candidates))
            return {
                "selected_instrument_id": int(candidates[0]["instrument_id"]),
                "confidence": 0.99,
                "attribute": "asset_type",
                "evidence_text": "ETF",
            }

        resolver = FinancialTargetResolver(self.registry, llm_ranker=ranker)
        bare = resolver.resolve("000001 怎么样", self.intent("000001 怎么样"), request_id="llm-bare")
        fund = resolver.resolve(
            "易方达沪深300ETF联接怎么样",
            self.intent("易方达沪深300ETF联接怎么样"),
            request_id="llm-fund",
        )
        self.assertEqual(bare["status"], "clarification_required")
        self.assertEqual(fund["status"], "clarification_required")
        self.assertEqual(calls, [])

        ambiguous = resolver.resolve(
            "沪深300怎么样", self.intent("沪深300怎么样"), request_id="llm-grounding"
        )
        self.assertEqual(len(calls), 1)
        self.assertTrue(ambiguous["llm_used"])
        self.assertEqual(ambiguous["status"], "clarification_required")
        self.assertNotIn("ETF", "沪深300怎么样")

    def test_same_session_confirmed_target_inherits_but_other_session_does_not(self):
        orchestrator = self.orchestrator()
        first_payload = _payload("分析腾讯控股的基本面", session_id="same-session")
        first = orchestrator.plan(first_payload)
        self.assertEqual(first.target_resolution["status"], "resolved")
        orchestrator.persist(first, first_payload)

        follow_payload = _payload("那它现在呢？", session_id="same-session")
        follow = orchestrator.plan(follow_payload)
        self.assertTrue(follow.target_resolution["context_inherited"])
        self.assertTrue(follow.financial_intent["is_financial"])
        self.assertEqual(follow.target_resolution["targets"][0]["canonical_symbol"], "0700.HK")

        isolated = orchestrator.plan(_payload("那它现在呢？", session_id="other-session"))
        self.assertFalse(isolated.target_resolution["context_inherited"])
        self.assertEqual(isolated.target_resolution["targets"], [])

    def test_explicit_new_target_overrides_confirmed_context_and_comparison_keeps_both(self):
        orchestrator = self.orchestrator()
        first_payload = _payload("腾讯控股股价", session_id="change-target")
        first = orchestrator.plan(first_payload)
        orchestrator.persist(first, first_payload)
        changed = orchestrator.plan(_payload("改为分析 Apple 股票", session_id="change-target"))
        self.assertFalse(changed.target_resolution["context_inherited"])
        self.assertEqual(
            [item["canonical_symbol"] for item in changed.target_resolution["targets"]],
            ["AAPL.US"],
        )

        comparison = self.resolve("比较腾讯和 Apple 的估值", request_id="composite")
        self.assertEqual(comparison["status"], "resolved")
        self.assertEqual(
            {item["canonical_symbol"] for item in comparison["targets"]},
            {"0700.HK", "AAPL.US"},
        )

    def test_pending_clarification_answer_restores_original_route_in_same_session(self):
        orchestrator = self.orchestrator()
        original_payload = _payload("分析 000001 的风险", session_id="clarify-session")
        original = orchestrator.plan(original_payload)
        self.assertEqual(original.target_resolution["status"], "clarification_required")
        orchestrator.persist(original, original_payload)

        answer_payload = _payload("上交所", session_id="clarify-session")
        answer = orchestrator.plan(answer_payload)
        self.assertEqual(answer.target_resolution["status"], "resolved")
        self.assertEqual(answer.target_resolution["resolution_source"], "clarification_answer")
        self.assertEqual(answer.target_resolution["resumed_from_route_key"], original.audit_route_key)
        self.assertEqual(answer.target_resolution["targets"][0]["canonical_symbol"], "000001.SH")
        self.assertEqual(answer.financial_intent["intent"], "research")
        saved = orchestrator.persist(answer, answer_payload)
        row = self.database.connection.execute(
            "SELECT route_status, route_destination, resolved_targets_json FROM chat_financial_routes WHERE id=?",
            (saved["route_id"],),
        ).fetchone()
        self.assertEqual(tuple(row[:2]), ("clarification_resolved", "financial_target_resolved"))
        self.assertEqual(json.loads(row[2])[0]["canonical_symbol"], "000001.SH")

    def test_pending_fund_answer_resumes_with_one_share_class(self):
        orchestrator = self.orchestrator()
        original_payload = _payload("分析易方达沪深300ETF联接", session_id="fund-resume")
        original = orchestrator.plan(original_payload)
        self.assertEqual(original.target_resolution["clarification"]["field"], "share_class")
        orchestrator.persist(original, original_payload)
        answer = orchestrator.plan(_payload("C类", session_id="fund-resume"))
        self.assertEqual(answer.target_resolution["targets"][0]["share_class"], "C")
        self.assertEqual(answer.target_resolution["resumed_from_route_key"], original.audit_route_key)

    def test_clarification_sse_pauses_before_model_or_web_search(self):
        orchestrator = self.orchestrator()
        app = Flask(__name__)
        app.config.update(TESTING=True)
        app.register_blueprint(chat_api.chat_bp)
        client = app.test_client()
        with patch.object(chat_api, "chat_route_orchestrator", orchestrator), patch.object(
            chat_api, "_stream_openai"
        ) as model_stream, patch.object(chat_api, "_web_search") as web_search, patch.object(
            chat_api, "_load_config"
        ) as load_config:
            response = client.post(
                "/api/chat/send",
                json=_payload("000001 怎么样", session_id="api-clarification"),
            )
            body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/event-stream")
        event_types = [
            json.loads(block[5:].strip())["type"]
            for block in body.split("\n\n")
            if block.startswith("data:")
        ]
        self.assertEqual(event_types, ["status", "chunk", "done"])
        self.assertIn("000001.SH", body)
        self.assertIn("000001.SZ", body)
        model_stream.assert_not_called()
        web_search.assert_not_called()
        load_config.assert_not_called()


if __name__ == "__main__":
    unittest.main()

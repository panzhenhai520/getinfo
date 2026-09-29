#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import chat_api
from chat_route_orchestrator import ChatFinancialRouteStore, ChatRouteOrchestrator
from financial_full_research import (
    FinancialFullResearchRouter,
    format_full_research_answer,
    validate_full_research,
)
from financial_instrument_discovery import (
    CatalogInstrumentDiscoverySource,
    FinancialInstrumentDiscoveryService,
)
from financial_instruments import InstrumentRegistry
from financial_intent_classifier import FinancialIntentClassifier
from financial_target_resolver import FinancialTargetResolver
from intel_database import IntelRepository
from sqlite_database import SQLiteDatabase


UTC = timezone.utc
NOW = datetime(2026, 7, 31, 2, 0, tzinfo=UTC)
RUNTIME = {
    "provider_id": "local",
    "type": "openai",
    "base_url": "http://local-llm.test/v1",
    "api_key": "configured-test-key",
    "model_id": "local-fixture-model",
    "use_proxy": False,
}
SETTINGS = {
    "FINANCIAL_INTELLIGENCE_ENABLED": True,
    "TRADING_AGENTS_ENABLED": True,
    "FINANCIAL_RESEARCH_CACHE_SECONDS": 3600,
    "FINANCIAL_RESEARCH_MAX_LLM_CALLS": 30,
    "FINANCIAL_RESEARCH_MAX_TOKENS": 120000,
    "FINANCIAL_RESEARCH_MAX_DEBATE_ROUNDS": 2,
    "FINANCIAL_RESEARCH_TIMEOUT_SECONDS": 1800,
    "FINANCIAL_QUOTE_FRESHNESS_SECONDS": 300,
    "FINANCIAL_MARKET_BREADTH_FRESHNESS_SECONDS": 300,
    "FINANCIAL_NEWS_FRESHNESS_SECONDS": 3600,
    "FINANCIAL_FUNDAMENTAL_FRESHNESS_SECONDS": 86400,
}


def _payload(question, session_id="full-research-session"):
    return {
        "session_id": session_id,
        "model": "local",
        "messages": [{"role": "user", "content": question}],
        "web_search": True,
        "user_timezone": "Asia/Hong_Kong",
    }


def _decode(block):
    text = block.decode() if isinstance(block, bytes) else str(block)
    return json.loads(text.split("data:", 1)[1].strip())


class FinancialFullResearchTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(
            str(Path(self.temp_dir.name) / "financial-full-research.sqlite3")
        )
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.repository = IntelRepository(self.database)
        self.registry = InstrumentRegistry(self.database.connection)
        self.registry.load_controlled_seed()
        self.classifier = FinancialIntentClassifier(self.registry)
        self.resolver = FinancialTargetResolver(self.registry)
        self.router, self.orchestrator = self._build(SETTINGS)

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _build(self, settings, *, instrument_discovery_service=None):
        router = FinancialFullResearchRouter(
            self.repository,
            settings=settings,
            clock=lambda: NOW,
            runtime_config_loader=lambda: dict(RUNTIME),
        )
        orchestrator = ChatRouteOrchestrator(
            clock=lambda: NOW,
            store=ChatFinancialRouteStore(self.database),
            intent_classifier=self.classifier,
            target_resolver=self.resolver,
            instrument_discovery_service=instrument_discovery_service,
            full_research_router=router,
            financial_settings=settings,
        )
        return router, orchestrator

    def _unknown_stock_discovery_service(self):
        identity = {
            "canonical_symbol": "NUVB.US",
            "display_name": "Nuvation Bio Inc.",
            "asset_type": "equity",
            "market": "US",
            "exchange": "XNAS",
            "currency": "USD",
            "country_code": "US",
            "listing_status": "active",
            "aliases": ["NUVB", "NUVB.US", "Nuvation Bio"],
            "observed_at": "2026-07-31T01:00:00Z",
            "expires_at": "2026-08-01T01:00:00Z",
        }
        source = CatalogInstrumentDiscoverySource(
            [
                {
                    **identity,
                    "source_key": "nasdaq_directory_fixture",
                    "source_type": "exchange",
                    "source_role": "authoritative",
                    "source_url": "https://example.test/nasdaq/nuvb",
                },
                {
                    **identity,
                    "source_key": "provider_identity_fixture",
                    "source_type": "provider",
                    "source_role": "corroborating",
                    "source_url": "https://example.test/provider/nuvb",
                },
                {
                    **identity,
                    "source_key": "provider_mapping_fixture",
                    "source_type": "provider",
                    "source_role": "provider",
                    "source_url": "https://example.test/provider/nuvb",
                    "approved": True,
                    "provider_key": "yahoo",
                    "provider_symbol": "NUVB",
                },
            ]
        )
        return FinancialInstrumentDiscoveryService(
            self.database.connection,
            sources=(source,),
            settings=SETTINGS,
        )

    def _plan(self, question="分析腾讯的基本面和风险", session_id="full-research-session"):
        payload = _payload(question, session_id)
        plan = self.orchestrator.plan(payload)
        self.assertEqual(plan.full_research["status"], "planned")
        return payload, plan

    def _insert_report(
        self,
        scope,
        *,
        fetched_at=NOW - timedelta(minutes=1),
        request_key=None,
    ):
        run_id = "fixture-report-" + scope["cache_key"][:24]
        now_text = fetched_at.isoformat().replace("+00:00", "Z")
        config = {
            "cache_key": scope["cache_key"],
            "request_key": request_key or scope["request_key"],
            "graph_version": scope["graph_version"],
        }
        self.database.connection.execute(
            """
            INSERT INTO financial_research_runs(
                id, trigger_type, scope_type, instrument_id, universe_id,
                status, current_stage, config_json, requested_at, completed_at
            ) VALUES(?, 'fixture', ?, ?, ?, 'completed', 'complete', ?, ?, ?)
            """,
            (
                run_id,
                scope["scope_type"],
                scope["instrument_id"],
                scope["universe_id"],
                json.dumps(config, sort_keys=True),
                now_text,
                now_text,
            ),
        )
        report_json = {
            "graph_version": scope["graph_version"],
            "output_classification": "research_opinion",
            "execution_allowed": False,
        }
        cursor = self.database.connection.execute(
            """
            INSERT INTO financial_final_reports(
                research_run_id, report_version, report_status,
                recommendation, confidence, title, executive_summary,
                report_markdown, report_json, risk_summary_json,
                suitability_notice, disclaimer, observed_at, fetched_at
            ) VALUES(?, 1, 'generated_unverified', 'Hold', 0.72,
                     'Fixture TradingAgents report', 'Fixture evidence-backed summary',
                     '# fixture', ?, '{}', 'research only', 'not advice', ?, ?)
            """,
            (
                run_id,
                json.dumps(report_json, sort_keys=True),
                fetched_at.date().isoformat(),
                now_text,
            ),
        )
        return run_id, int(cursor.lastrowid)

    def test_compatible_fresh_report_is_reused_without_duplicate_job(self):
        payload, plan = self._plan()
        run_id, report_id = self._insert_report(plan.full_research["scopes"][0])
        completed = self.orchestrator.activate_full_research(plan, payload)
        route = validate_full_research(completed.full_research)
        self.assertEqual(route["status"], "cache_hit")
        self.assertTrue(route["answer_allowed"])
        self.assertEqual(route["reports"][0]["report_id"], report_id)
        self.assertEqual(route["reports"][0]["research_run_id"], run_id)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM intel_jobs WHERE job_type='financial_research'"
            ).fetchone()[0],
            0,
        )
        answer = format_full_research_answer(route)
        self.assertIn("Fixture evidence-backed summary", answer)
        self.assertIn("report_id=", answer)
        self.assertIn("不构成投资建议", answer)

    def test_unknown_stock_is_discovered_then_queued_into_tradingagents(self):
        _router, orchestrator = self._build(
            SETTINGS,
            instrument_discovery_service=self._unknown_stock_discovery_service(),
        )
        payload = _payload(
            "分析 NUVB.US 的基本面和风险",
            "unknown-stock-research",
        )

        initial = orchestrator.plan(payload)
        self.assertEqual(initial.information_needs["channels"], ["research"])
        self.assertEqual(initial.target_resolution["status"], "no_target")
        self.assertEqual(initial.instrument_discovery["status"], "planned")

        app = Flask(__name__)
        app.register_blueprint(chat_api.chat_bp)
        with patch.object(chat_api, "chat_route_orchestrator", orchestrator), patch.object(
            chat_api, "_load_config"
        ) as config_loader, patch.object(chat_api, "_web_search") as web, patch.object(
            chat_api, "_stream_openai"
        ) as model:
            response = app.test_client().post(
                "/api/chat/send",
                json=payload,
                buffered=True,
            )

        events = [_decode(item) for item in response.response]
        self.assertEqual(
            [item["type"] for item in events],
            ["status", "status", "chunk", "done"],
        )
        self.assertIn("外部金融信源核验", events[0]["message"])
        self.assertIn("TradingAgents", events[1]["message"])
        self.assertIn("TradingAgents", events[2]["content"])
        promoted = self.registry.get_by_canonical_symbol("NUVB.US")
        self.assertIsNotNone(promoted)
        self.assertEqual(promoted.provider_mappings["yahoo"], "NUVB")
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM intel_jobs "
                "WHERE job_type='financial_research' AND status='queued'"
            ).fetchone()[0],
            1,
        )
        row = self.database.connection.execute(
            "SELECT route_status, route_destination FROM chat_financial_routes "
            "WHERE session_id='unknown-stock-research' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(tuple(row), ("full_research_queued", "financial_full_research"))
        config_loader.assert_not_called()
        web.assert_not_called()
        model.assert_not_called()

    def test_expired_report_and_changed_config_or_provider_profile_do_not_reuse(self):
        payload, plan = self._plan()
        original_scope = plan.full_research["scopes"][0]
        self._insert_report(
            original_scope,
            fetched_at=NOW - timedelta(hours=2),
            request_key="expired-prior-request-window",
        )
        expired = self.orchestrator.activate_full_research(plan, payload).full_research
        self.assertEqual(expired["status"], "queued")
        self.assertNotEqual(expired["research_run_ids"][0], "")

        changed_settings = {**SETTINGS, "FINANCIAL_RESEARCH_MAX_DEBATE_ROUNDS": 3}
        _changed_router, changed_orchestrator = self._build(changed_settings)
        changed_plan = changed_orchestrator.plan(_payload("分析腾讯的基本面和风险", "config-change"))
        changed_scope = changed_plan.full_research["scopes"][0]
        self.assertNotEqual(changed_scope["config_hash"], original_scope["config_hash"])
        self.assertNotEqual(changed_scope["cache_key"], original_scope["cache_key"])

        provider_settings = {**SETTINGS, "YAHOO_FINANCE_ENABLED": True}
        _provider_router, provider_orchestrator = self._build(provider_settings)
        provider_plan = provider_orchestrator.plan(
            _payload("分析腾讯的基本面和风险", "provider-change")
        )
        provider_scope = provider_plan.full_research["scopes"][0]
        self.assertNotEqual(
            provider_scope["provider_profile_hash"],
            original_scope["provider_profile_hash"],
        )
        self.assertNotEqual(provider_scope["cache_key"], original_scope["cache_key"])

    def test_concurrent_identical_requests_share_one_run_and_job(self):
        payload, plan = self._plan(session_id="concurrent")

        def activate(_index):
            return self.router.activate(plan.full_research, payload)

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(activate, range(12)))
        run_ids = {tuple(result["research_run_ids"]) for result in results}
        self.assertEqual(len(run_ids), 1)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM intel_jobs WHERE job_type='financial_research'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM financial_research_runs WHERE trigger_type='chat'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            sum(bool(item["jobs"][0].get("created")) for item in results), 1
        )

    def test_cancelled_job_is_reported_and_not_replaced_or_filled_by_opinion(self):
        payload, plan = self._plan(session_id="cancelled")
        queued = self.router.activate(plan.full_research, payload)
        job_id = queued["jobs"][0]["job_id"]
        self.assertTrue(self.repository.cancel_job(job_id, reason="user_cancelled"))
        cancelled = self.router.activate(plan.full_research, payload)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse(cancelled["answer_allowed"])
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM intel_jobs WHERE job_type='financial_research'"
            ).fetchone()[0],
            1,
        )
        self.assertIn("不会改用通用模型", format_full_research_answer(cancelled))

    def test_user_switch_and_comparison_keep_targets_isolated(self):
        first_payload, first_plan = self._plan(session_id="switch-target")
        first = self.router.activate(first_plan.full_research, first_payload)
        changed_payload = _payload("分析Apple的基本面和风险", "switch-target")
        changed_plan = self.orchestrator.plan(changed_payload)
        changed = self.router.activate(changed_plan.full_research, changed_payload)
        self.assertNotEqual(first["research_run_ids"], changed["research_run_ids"])
        self.assertEqual(
            changed_plan.target_resolution["targets"][0]["canonical_symbol"],
            "AAPL.US",
        )

        compare_payload = _payload("比较腾讯和浦发银行的估值与风险", "compare-targets")
        compare_plan = self.orchestrator.plan(compare_payload)
        self.assertEqual(len(compare_plan.full_research["scopes"]), 2)
        compare = self.router.activate(compare_plan.full_research, compare_payload)
        self.assertEqual(compare["status"], "queued")
        self.assertEqual(len(compare["research_run_ids"]), 2)
        self.assertFalse(compare["answer_allowed"])

    def test_deep_market_question_routes_universe_while_bare_overview_stays_lightweight(self):
        deep_payload = _payload("深入分析A股市场的风险", "universe-deep")
        deep = self.orchestrator.plan(deep_payload)
        self.assertEqual(deep.financial_intent["intent"], "research")
        self.assertEqual(deep.market_scope["status"], "skipped")
        self.assertEqual(deep.full_research["status"], "planned")
        self.assertEqual(deep.full_research["scopes"][0]["scope_type"], "universe")
        self.assertEqual(
            deep.full_research["scopes"][0]["target"]["universe_key"],
            "CN_A_MARKET",
        )

        overview = self.orchestrator.plan(_payload("今天A股怎么样", "universe-light"))
        self.assertEqual(overview.financial_intent["intent"], "market_overview")
        self.assertEqual(overview.full_research["status"], "skipped")

    def test_disabled_or_unconfigured_runtime_is_evidence_closed(self):
        _disabled_router, disabled = self._build(
            {**SETTINGS, "TRADING_AGENTS_ENABLED": False}
        )
        disabled_plan = disabled.plan(_payload("分析腾讯的风险", "disabled"))
        self.assertEqual(disabled_plan.full_research["status"], "unavailable")
        self.assertIn("trading_agents_disabled", disabled_plan.full_research["reason_codes"])

        no_model_router = FinancialFullResearchRouter(
            self.repository,
            settings=SETTINGS,
            clock=lambda: NOW,
            runtime_config_loader=lambda: {**RUNTIME, "api_key": ""},
        )
        no_model = ChatRouteOrchestrator(
            clock=lambda: NOW,
            intent_classifier=self.classifier,
            target_resolver=self.resolver,
            full_research_router=no_model_router,
            financial_settings=SETTINGS,
        ).plan(_payload("分析腾讯的风险", "no-model"))
        self.assertEqual(no_model.full_research["status"], "unavailable")
        self.assertFalse(no_model.full_research["answer_allowed"])

    def test_legacy_sse_returns_research_state_without_model_or_web_fallback(self):
        payload, plan = self._plan(session_id="failed-sse")
        queued = self.router.activate(plan.full_research, payload)
        job_id = queued["jobs"][0]["job_id"]
        self.database.connection.execute(
            "UPDATE intel_jobs SET status='failed', last_error='fixture failure' WHERE id=?",
            (job_id,),
        )
        app = Flask(__name__)
        app.register_blueprint(chat_api.chat_bp)
        with patch.object(chat_api, "chat_route_orchestrator", self.orchestrator), patch.object(
            chat_api, "_load_config"
        ) as config_loader, patch.object(chat_api, "_web_search") as web, patch.object(
            chat_api, "_stream_openai"
        ) as model:
            response = app.test_client().post(
                "/api/chat/send",
                json=_payload("分析腾讯的基本面和风险", "failed-sse"),
                buffered=True,
            )
        events = [_decode(item) for item in response.response]
        self.assertEqual([item["type"] for item in events], ["status", "chunk", "done"])
        self.assertIn("不会改用通用模型", events[1]["content"])
        config_loader.assert_not_called()
        web.assert_not_called()
        model.assert_not_called()
        row = self.database.connection.execute(
            "SELECT route_status, route_destination FROM chat_financial_routes "
            "WHERE session_id='failed-sse' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(tuple(row), ("full_research_failed", "financial_full_research"))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import chat_api
from chat_route_orchestrator import ChatFinancialRouteStore, ChatRouteOrchestrator
from financial_chat_market_scope import FinancialMarketScopeRouter
from financial_instruments import InstrumentRegistry
from financial_intent_classifier import FinancialIntentClassifier
from financial_market_scheduler import FinancialMarketScheduler
from financial_target_resolver import FinancialTargetResolver
from intel_database import IntelRepository
from sqlite_database import SQLiteDatabase


UTC = timezone.utc
FROZEN_NOW = datetime(2026, 7, 31, 2, 0, tzinfo=UTC)
SETTINGS = {
    "INTEL_DEFAULT_INDUSTRY_PACK": "family_office",
    "FINANCIAL_INTELLIGENCE_ENABLED": True,
    "TRADING_AGENTS_ENABLED": True,
    "FINANCIAL_AUTO_RESEARCH_ENABLED": False,
    "TRADING_SIMULATION_ENABLED": False,
    "FINANCIAL_QUOTE_FRESHNESS_SECONDS": 300,
}


def _payload(question: str, session_id: str) -> dict:
    return {
        "session_id": session_id,
        "model": "local",
        "messages": [{"role": "user", "content": question}],
        "web_search": False,
        "user_timezone": "Asia/Hong_Kong",
        "industry_pack_id": "family_office",
    }


def _events(response) -> list[dict]:
    return [
        json.loads(block[5:].strip())
        for block in response.get_data(as_text=True).split("\n\n")
        if block.startswith("data:")
    ]


class FinancialStage3EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "stage3-e2e.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.repository = IntelRepository(self.database)
        registry = InstrumentRegistry(self.database.connection)
        registry.load_controlled_seed()
        scheduler = FinancialMarketScheduler(self.repository, settings=SETTINGS)
        self.orchestrator = ChatRouteOrchestrator(
            clock=lambda: FROZEN_NOW,
            store=ChatFinancialRouteStore(self.database),
            intent_classifier=FinancialIntentClassifier(registry),
            target_resolver=FinancialTargetResolver(registry),
            market_scope_router=FinancialMarketScopeRouter(
                self.repository,
                scheduler,
                settings=SETTINGS,
            ),
            financial_settings=SETTINGS,
        )

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def test_one_product_routes_market_index_stock_ambiguity_and_fund(self):
        ordinary = self.orchestrator.plan(_payload("写一首关于春天的诗", "ordinary"))
        self.assertEqual(ordinary.route_key, "legacy_chat")
        self.assertFalse(ordinary.financial_intent["is_financial"])

        broad = self.orchestrator.plan(_payload("今天大盘怎么样", "broad-market"))
        self.assertEqual(broad.financial_intent["intent"], "market_overview")
        self.assertEqual(
            broad.market_scope["universe"]["universe_key"],
            "DEFAULT_MARKET_PULSE",
        )
        self.assertEqual(
            broad.server_time_context["server_now_utc"],
            "2026-07-31T02:00:00Z",
        )

        index = self.orchestrator.plan(_payload("上证指数现在怎么样", "index"))
        self.assertEqual(index.target_resolution["status"], "resolved")
        self.assertEqual(
            index.target_resolution["targets"][0]["instrument_key"],
            "CN:XSHG:INDEX:000001",
        )

        ambiguous = self.orchestrator.plan(_payload("000001 怎么样", "ambiguous"))
        self.assertEqual(ambiguous.target_resolution["status"], "clarification_required")
        self.assertEqual(
            {item["canonical_symbol"] for item in ambiguous.target_resolution["clarification"]["options"]},
            {"000001.SH", "000001.SZ"},
        )

        quote = self.orchestrator.plan(_payload("腾讯多少钱", "quote"))
        self.assertEqual(quote.financial_intent["intent"], "market_fact")
        self.assertEqual(quote.target_resolution["targets"][0]["canonical_symbol"], "0700.HK")
        self.assertFalse(quote.financial_intent["needs_full_research"])

        research = self.orchestrator.plan(_payload("腾讯现在怎么看", "research"))
        self.assertEqual(research.target_resolution["targets"][0]["canonical_symbol"], "0700.HK")
        self.assertTrue(research.financial_intent["needs_full_research"])

        fund = self.orchestrator.plan(
            _payload("易方达沪深300ETF联接怎么样", "fund")
        )
        self.assertEqual(fund.target_resolution["status"], "clarification_required")
        self.assertEqual(fund.target_resolution["clarification"]["field"], "share_class")

    def test_same_chat_endpoint_keeps_legacy_sse_and_pauses_ambiguous_finance(self):
        app = Flask(__name__)
        app.config.update(TESTING=True)
        app.register_blueprint(chat_api.chat_bp)
        client = app.test_client()
        runtime = {
            "models": {
                "local": {
                    "api_key": "fixture",
                    "model_id": "fixture-model",
                    "base_url": "http://local-model.invalid/v1",
                    "use_proxy": False,
                }
            }
        }
        with patch.object(chat_api, "chat_route_orchestrator", self.orchestrator), patch.object(
            chat_api, "_load_config", return_value=runtime
        ) as config_loader, patch.object(
            chat_api,
            "_chat_industry_identity",
            return_value=({"id": "family_office", "name": "家族办公室"}, False),
        ), patch.object(
            chat_api, "_stream_openai", return_value=iter(("普通", "回答"))
        ) as model, patch.object(
            chat_api, "_web_search"
        ) as web, patch.object(
            chat_api, "_save_chat_metric", return_value=None
        ):
            ordinary = _events(
                client.post("/api/chat/send", json=_payload("写一首诗", "http-ordinary"))
            )
            self.assertEqual(
                [item["type"] for item in ordinary],
                ["status", "chunk", "chunk", "done"],
            )
            self.assertEqual("".join(item.get("content", "") for item in ordinary), "普通回答")
            model.reset_mock()
            config_loader.reset_mock()
            ambiguous = _events(
                client.post("/api/chat/send", json=_payload("000001 怎么样", "http-finance"))
            )
        self.assertEqual([item["type"] for item in ambiguous], ["status", "chunk", "done"])
        self.assertIn("000001.SH", ambiguous[1]["content"])
        self.assertIn("000001.SZ", ambiguous[1]["content"])
        model.assert_not_called()
        config_loader.assert_not_called()
        web.assert_not_called()


if __name__ == "__main__":
    unittest.main()

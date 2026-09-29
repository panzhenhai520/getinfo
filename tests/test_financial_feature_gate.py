#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import itertools
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import config
import intel_api
import mapindex_api
from financial_config import (
    PRODUCT_CAPABILITY_SCHEMA_VERSION,
    financial_product_capabilities,
)
from financial_worker_jobs import FinancialJobContext, FinancialJobDispatcher
from intel_api import intel_bp
from intel_database import IntelRepository
from sqlite_database import SQLiteDatabase


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _settings(financial: bool, agents: bool, simulation: bool) -> dict:
    return {
        "INTEL_DEFAULT_INDUSTRY_PACK": "family_office",
        "FINANCIAL_INTELLIGENCE_ENABLED": financial,
        "TRADING_AGENTS_ENABLED": agents,
        "FINANCIAL_AUTO_RESEARCH_ENABLED": False,
        "TRADING_SIMULATION_ENABLED": simulation,
    }


class FinancialFeatureGateTests(unittest.TestCase):
    def test_all_switch_combinations_and_shared_financial_addon_contract(self):
        for financial, agents, simulation in itertools.product((False, True), repeat=3):
            with self.subTest(financial=financial, agents=agents, simulation=simulation):
                state = financial_product_capabilities(
                    "family_office",
                    settings=_settings(financial, agents, simulation),
                )
                self.assertEqual(state["schema_version"], PRODUCT_CAPABILITY_SCHEMA_VERSION)
                self.assertIn("financial_markets", state["effective_pack_ids"])
                self.assertEqual(state["product"]["financial_zone"], financial)
                self.assertEqual(
                    state["product"]["tradingagents_reports"],
                    financial and agents,
                )
                self.assertEqual(state["product"]["simulation"], financial and simulation)
                self.assertEqual(state["product"]["backtesting"], financial and simulation)
                self.assertEqual(
                    state["dashboard_capabilities"],
                    {
                        "show_financial_news": True,
                        "show_market_index_cards": True,
                        "show_watched_stock_cards": True,
                        "show_spatiotemporal_map": True,
                    },
                )

        shared = financial_product_capabilities(
            "ai_news",
            settings=_settings(True, True, True),
        )
        self.assertIn("financial_markets", shared["effective_pack_ids"])
        self.assertTrue(shared["pack_has_financial_markets"])
        self.assertFalse(shared["pack_financial_products_enabled"])
        self.assertFalse(any(shared["product"].values()))
        self.assertEqual(
            shared["product_reasons"]["financial_zone"],
            "financial_products_hidden_for_primary_pack",
        )
        self.assertEqual(
            shared["dashboard_capabilities"],
            {
                "show_financial_news": True,
                "show_market_index_cards": False,
                "show_watched_stock_cards": False,
                "show_spatiotemporal_map": True,
            },
        )

        denied = financial_product_capabilities(
            "not_installed",
            settings=_settings(True, True, True),
        )
        self.assertFalse(any(denied["product"].values()))
        self.assertEqual(
            denied["product_reasons"]["financial_zone"],
            "invalid_or_disabled_industry_pack",
        )

    def test_authenticated_non_admin_gets_only_effective_navigation_projection(self):
        app = Flask(__name__)
        app.config.update(TESTING=True)
        app.register_blueprint(intel_bp)
        client = app.test_client()
        self.assertEqual(client.get("/api/intel/financial/capabilities").status_code, 401)
        with patch(
            "decorators.user_db.verify_session",
            return_value={"user_id": 8, "role": "user"},
        ), patch.object(
            intel_api.intel_repository,
            "active_industry_pack_id",
            return_value="family_office",
        ), patch.object(
            config, "FINANCIAL_INTELLIGENCE_ENABLED", True,
        ), patch.object(
            config, "TRADING_AGENTS_ENABLED", True,
        ), patch.object(
            config, "TRADING_SIMULATION_ENABLED", False,
        ):
            response = client.get(
                "/api/intel/financial/capabilities",
                headers={"Authorization": "Bearer fixture"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(
            payload["effective_capabilities"],
            {
                "financial_zone": True,
                "tradingagents_reports": True,
                "simulation": False,
                "backtesting": False,
            },
        )
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("configured", rendered)
        self.assertNotIn("TOKEN", rendered)
        self.assertEqual(
            payload["running_task_policy"],
            "finish_claimed_skip_queued_and_new_preserve_history",
        )

    def test_global_menu_has_no_financial_entries_and_workspace_owns_them(self):
        navigation = (PROJECT_ROOT / "templates" / "_dashboard_nav.html").read_text(
            encoding="utf-8"
        )
        workspace = (PROJECT_ROOT / "templates" / "mapindex.html").read_text(
            encoding="utf-8"
        )
        for capability, label in (
            ("financial_zone", "金融专区"),
            ("tradingagents_reports", "TradingAgents 报告"),
            ("simulation", "模拟交易"),
            ("backtesting", "策略回测"),
        ):
            self.assertNotIn(f'data-financial-menu="{capability}"', navigation)
            self.assertNotIn(label, navigation)
            self.assertIn(f'data-financial-workspace-menu="{capability}"', workspace)
            self.assertIn(label, workspace)
        self.assertNotIn("financial_module=", navigation)
        self.assertIn('href="/financial?financial_module=zone#', workspace)
        self.assertIn("/api/intel/financial/capabilities", workspace)
        self.assertIn("const IS_FINANCIAL_WORKSPACE", workspace)

    def test_financial_workspace_is_a_separate_authenticated_route(self):
        app = Flask(
            __name__,
            template_folder=str(PROJECT_ROOT / "templates"),
        )
        app.config.update(TESTING=True, SECRET_KEY="financial-workspace-test")
        app.add_url_rule("/login", "login_page", lambda: "login")
        app.register_blueprint(mapindex_api.mapindex_bp)
        client = app.test_client()
        self.assertEqual(client.get("/financial").status_code, 302)
        with patch(
            "decorators.user_db.verify_session",
            return_value={"user_id": 8, "role": "user"},
        ):
            financial = client.get(
                "/financial?financial_module=simulation",
                headers={"Authorization": "Bearer fixture"},
            )
            homepage_alias = client.get(
                "/mapindex?financial_module=simulation",
                headers={"Authorization": "Bearer fixture"},
            )
        self.assertEqual(financial.status_code, 200)
        self.assertIn(b'data-financial-workspace="true"', financial.data)
        self.assertIn(b'href="/financial?financial_module=simulation#', financial.data)
        self.assertEqual(homepage_alias.status_code, 200)
        self.assertIn(b'data-financial-workspace="false"', homepage_alias.data)

    def test_worker_pack_gate_and_running_switch_policy(self):
        settings = _settings(True, True, True)
        with tempfile.TemporaryDirectory() as directory:
            database = SQLiteDatabase(str(Path(directory) / "feature-gate.sqlite3"))
            self.assertTrue(database.connect())
            self.assertTrue(database.create_tables())
            repository = IntelRepository(database)
            context = FinancialJobContext(
                job_id=1,
                job_type="paper_backtest",
                worker_id="fixture",
                repository=repository,
                cancel_event=threading.Event(),
            )

            started = threading.Event()
            release = threading.Event()

            def running(_payload, _context):
                started.set()
                self.assertTrue(release.wait(2))
                return {"status": "completed", "safe": True}

            dispatcher = FinancialJobDispatcher(
                {"paper_backtest": running},
                settings=settings,
            )
            holder = {}
            thread = threading.Thread(
                target=lambda: holder.update(
                    dispatcher.execute(
                        "paper_backtest",
                        {"industry_pack_id": "family_office"},
                        context,
                    )
                )
            )
            thread.start()
            self.assertTrue(started.wait(2))
            settings["TRADING_SIMULATION_ENABLED"] = False
            release.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(holder["status"], "completed")

            skipped = dispatcher.execute(
                "paper_backtest",
                {"industry_pack_id": "family_office"},
                context,
            )
            self.assertEqual(skipped["status"], "skipped")
            self.assertEqual(skipped["reason"], "trading_simulation_enabled_disabled")

            settings["TRADING_SIMULATION_ENABLED"] = True
            shared_pack = dispatcher.execute(
                "paper_backtest",
                {"industry_pack_id": "ai_news"},
                context,
            )
            self.assertEqual(shared_pack["status"], "skipped")
            self.assertEqual(
                shared_pack["reason"],
                "financial_products_hidden_for_primary_pack",
            )

            wrong_pack = dispatcher.execute(
                "paper_backtest",
                {"industry_pack_id": "not_installed"},
                context,
            )
            self.assertEqual(wrong_pack["status"], "skipped")
            self.assertEqual(
                wrong_pack["reason"],
                "invalid_or_disabled_industry_pack",
            )
            database.disconnect()


if __name__ == "__main__":
    unittest.main()

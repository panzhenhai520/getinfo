#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import tempfile
import json
import shutil
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone

from flask import Flask

import config_management_api
from config_management_api import (
    BOOL_KEYS,
    _build_updates,
    _invalidate_tushare_probe_after_token_change,
    _public_config,
    _runtime_env_or_config,
    config_management_bp,
)
from financial_config import (
    FinancialCapabilityDisabled,
    financial_capabilities,
    require_financial_capability,
)
from financial_schema import ensure_financial_tables
from sqlite_database import SQLiteDatabase


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 本机 Werkzeug 2.2 的 test_client.set_cookie() 会把 cookie 写成
# host-only（domain=session_token.local），请求时不会被带回，导致
# admin_required 直接 401。decorators.login_required/admin_required 同时
# 接受 Authorization: Bearer，因此这里统一用请求头做认证。
AUTH_HEADERS = {"Authorization": "Bearer fixture"}


class FinancialConfigurationTests(unittest.TestCase):
    def setUp(self):
        # conftest 的 DATABASE_TYPE=sqlite 会被 .env 覆盖（config 里仍是 postgres），
        # 而 _public_config()/_open_database_connection() 走的是
        # db_connection.connect_database()（忽略传入 path），会连共享主库；
        # 本文件里 _public_config({}) 这类调用还会触发 tushare 探针的写路径。
        # 这里强制回到临时 SQLite。
        for item in (
            patch.object(config_management_api.config, "DATABASE_TYPE", "sqlite"),
            patch("db_connection.database_type", lambda: "sqlite"),
        ):
            item.start()
            self.addCleanup(item.stop)

    def _isolated_main_db(self):
        """返回一个空的临时主库句柄，避免读到宿主 data/crawler_articles.db 的
        intel_runtime_settings（本机残留了 education_news 激活包）。"""

        directory = tempfile.TemporaryDirectory()
        database = SQLiteDatabase(str(Path(directory.name) / "config-main.sqlite3"))
        self.assertTrue(database.connect())
        self.assertTrue(database.create_tables())

        def _release():
            # Windows 上必须先断开 SQLite 再删目录，否则 rmtree 会因为
            # 文件仍被占用而抛 PermissionError（进而退化成 NotADirectoryError）。
            database.disconnect()
            directory.cleanup()

        self.addCleanup(_release)
        return database

    def test_new_install_defaults_all_financial_capabilities_off(self):
        with patch.multiple(
            config_management_api.config,
            TUSHARE_TOKEN="",
            ALPHA_VANTAGE_API_KEY="",
            FRED_API_KEY="",
        ), patch.dict(
            config_management_api.os.environ,
            {
                "TUSHARE_TOKEN": "",
                "ALPHA_VANTAGE_API_KEY": "",
                "FRED_API_KEY": "",
            },
        ):
            payload = _public_config({})
        financial = payload["financial"]
        for key in (
            "financial_intelligence_enabled",
            "trading_agents_enabled",
            "auto_research_enabled",
            "simulation_enabled",
            "akshare_cn_enabled",
            "tushare_cn_enabled",
            "tushare_token_configured",
            "yahoo_finance_enabled",
            "alpha_vantage_enabled",
            "alpha_vantage_api_key_configured",
            "alpha_vantage_realtime_entitled",
            "fred_enabled",
            "fred_api_key_configured",
            "polymarket_enabled",
            "easyquotation_enabled",
            "official_evidence_enabled",
        ):
            self.assertFalse(financial[key], key)
        self.assertFalse(any(financial["effective_capabilities"].values()))
        self.assertEqual(financial["alpha_vantage_quote_entitlement"], "none")
        self.assertNotIn("tushare_token", financial)
        example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        for key in (
            "FINANCIAL_INTELLIGENCE_ENABLED",
            "TRADING_AGENTS_ENABLED",
            "FINANCIAL_AUTO_RESEARCH_ENABLED",
            "TRADING_SIMULATION_ENABLED",
            "AKSHARE_CN_ENABLED",
            "TUSHARE_CN_ENABLED",
            "YAHOO_FINANCE_ENABLED",
            "ALPHA_VANTAGE_ENABLED",
            "ALPHA_VANTAGE_REALTIME_ENTITLED",
            "FRED_ENABLED",
            "POLYMARKET_ENABLED",
            "EASYQUOTATION_ENABLED",
            "OFFICIAL_FINANCIAL_EVIDENCE_ENABLED",
        ):
            self.assertIn(f"{key}=false", example)
        self.assertIn("ALPHA_VANTAGE_QUOTE_ENTITLEMENT=none", example)

    def test_capability_parent_chain_and_tushare_token_gate(self):
        settings = {
            "FINANCIAL_INTELLIGENCE_ENABLED": "true",
            "TRADING_AGENTS_ENABLED": "true",
            "FINANCIAL_AUTO_RESEARCH_ENABLED": "true",
            "TRADING_SIMULATION_ENABLED": "true",
            "AKSHARE_CN_ENABLED": "true",
            "TUSHARE_CN_ENABLED": "true",
            "TUSHARE_TOKEN": "",
        }
        state = financial_capabilities(settings)
        self.assertTrue(state["effective"]["trading_agents"])
        self.assertTrue(state["effective"]["auto_research"])
        self.assertTrue(state["effective"]["simulation"])
        self.assertTrue(state["effective"]["akshare_cn"])
        self.assertFalse(state["effective"]["tushare_cn"])
        self.assertEqual(state["reasons"]["tushare_cn"], "tushare_token_missing")
        settings["FINANCIAL_INTELLIGENCE_ENABLED"] = "false"
        with self.assertRaises(FinancialCapabilityDisabled):
            require_financial_capability("trading_agents", settings)

    def test_build_updates_preserves_secret_and_rejects_invalid_limits(self):
        current = {"TUSHARE_TOKEN": "stored-secret"}
        updates = _build_updates(
            {
                "financial": {
                    "financial_intelligence_enabled": True,
                    "trading_agents_enabled": True,
                    "simulation_enabled": True,
                    "latest_news_enabled": True,
                    "latest_bundle_enabled": False,
                    "latest_quote_timeout_seconds": "6",
                    "latest_news_timeout_seconds": "8",
                    "latest_bundle_timeout_seconds": "12",
                    "news_lookback_days": "7",
                    "provider_timeout_seconds": "30",
                    "research_max_llm_calls": "40",
                    "tushare_token": "",
                    "clear_tushare_token": False,
                }
            },
            current,
        )
        self.assertEqual(updates["TUSHARE_TOKEN"], "stored-secret")
        self.assertEqual(updates["FINANCIAL_INTELLIGENCE_ENABLED"], "true")
        self.assertEqual(updates["TRADING_SIMULATION_ENABLED"], "true")
        self.assertEqual(updates["FINANCIAL_LATEST_NEWS_ENABLED"], "true")
        self.assertEqual(updates["FINANCIAL_LATEST_BUNDLE_ENABLED"], "false")
        self.assertEqual(updates["FINANCIAL_LATEST_BUNDLE_TIMEOUT_SECONDS"], "12")
        self.assertEqual(updates["FINANCIAL_NEWS_LOOKBACK_DAYS"], "7")
        self.assertEqual(updates["FINANCIAL_PROVIDER_TIMEOUT_SECONDS"], "30")
        self.assertNotIn("FLASK_PORT", updates)
        self.assertNotIn("RAGFLOW_UPLOAD_ENABLED", updates)
        with self.assertRaisesRegex(ValueError, "FINANCIAL_PROVIDER_TIMEOUT_SECONDS"):
            _build_updates(
                {"financial": {"provider_timeout_seconds": "999"}}, current
            )
        with self.assertRaisesRegex(ValueError, "FINANCIAL_RESEARCH_MAX_LLM_CALLS"):
            _build_updates(
                {"financial": {"research_max_llm_calls": "not-a-number"}}, current
            )

    def test_partial_runtime_update_preserves_implicit_rollout_stage(self):
        with patch.dict(
            config_management_api.os.environ,
            {},
            clear=True,
        ), patch.multiple(
            config_management_api.config,
            FINANCIAL_ROLLOUT_STAGE="simulation_backtest",
            FINANCIAL_ROLLOUT_STAGE_CHANGED_AT="2026-08-04T03:00:00Z",
            FINANCIAL_ROLLOUT_OBSERVATION_SECONDS=900,
        ):
            self.assertEqual(
                _runtime_env_or_config(
                    "FINANCIAL_ROLLOUT_STAGE", "simulation_backtest"
                ),
                "simulation_backtest",
            )
            self.assertEqual(
                _runtime_env_or_config(
                    "FINANCIAL_ROLLOUT_STAGE_CHANGED_AT", ""
                ),
                "2026-08-04T03:00:00Z",
            )
            self.assertEqual(
                _runtime_env_or_config(
                    "FINANCIAL_ROLLOUT_OBSERVATION_SECONDS", 3600
                ),
                900,
            )

            config_management_api.os.environ["FINANCIAL_ROLLOUT_STAGE"] = "ai_fact"
            self.assertEqual(
                _runtime_env_or_config(
                    "FINANCIAL_ROLLOUT_STAGE", "simulation_backtest"
                ),
                "ai_fact",
            )

    def test_only_one_simulation_switch_and_page_uses_exact_label(self):
        simulation_keys = sorted(key for key in BOOL_KEYS if "SIMULAT" in key)
        self.assertEqual(simulation_keys, ["TRADING_SIMULATION_ENABLED"])
        template = (PROJECT_ROOT / "templates/config_management.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("开启模拟数据", template)
        self.assertIn('id="tradingSimulationEnabled"', template)
        self.assertIn("const payload = {", template)
        self.assertIn("financial: {", template)

    def test_auxiliary_provider_keys_are_preserved_or_cleared_without_public_exposure(self):
        current = {
            "ALPHA_VANTAGE_API_KEY": "stored-alpha-secret",
            "FRED_API_KEY": "stored-fred-secret",
        }
        updates = _build_updates(
            {
                "financial": {
                    "yahoo_finance_enabled": True,
                    "alpha_vantage_enabled": True,
                    "alpha_vantage_api_key": "",
                    "clear_alpha_vantage_api_key": False,
                    "alpha_vantage_realtime_entitled": False,
                    "alpha_vantage_quote_entitlement": "delayed",
                    "fred_enabled": True,
                    "fred_api_key": "",
                    "clear_fred_api_key": True,
                    "polymarket_enabled": True,
                    "easyquotation_enabled": False,
                    "official_evidence_enabled": True,
                }
            },
            current,
        )
        self.assertEqual(updates["ALPHA_VANTAGE_API_KEY"], "stored-alpha-secret")
        self.assertEqual(updates["FRED_API_KEY"], "")
        self.assertEqual(updates["YAHOO_FINANCE_ENABLED"], "true")
        self.assertEqual(updates["ALPHA_VANTAGE_QUOTE_ENTITLEMENT"], "delayed")
        self.assertEqual(updates["POLYMARKET_ENABLED"], "true")
        public = _public_config(
            {
                **updates,
                "ALPHA_VANTAGE_API_KEY": "public-alpha-secret",
                "FRED_API_KEY": "public-fred-secret",
            }
        )
        encoded = json.dumps(public, ensure_ascii=False)
        self.assertNotIn("public-alpha-secret", encoded)
        self.assertNotIn("public-fred-secret", encoded)
        self.assertTrue(public["financial"]["alpha_vantage_api_key_configured"])
        self.assertEqual(
            public["financial"]["alpha_vantage_quote_entitlement"], "delayed"
        )
        self.assertTrue(public["financial"]["fred_api_key_configured"])
        template = (PROJECT_ROOT / "templates/config_management.html").read_text(
            encoding="utf-8"
        )
        for element_id in (
            "yahooFinanceEnabled",
            "alphaVantageApiKey",
            "alphaVantageRealtimeEntitled",
            "alphaVantageQuoteEntitlement",
            "fredApiKey",
            "polymarketEnabled",
            "easyquotationEnabled",
            "officialEvidenceEnabled",
        ):
            self.assertIn(f'id="{element_id}"', template)

        with self.assertRaisesRegex(
            ValueError, "ALPHA_VANTAGE_QUOTE_ENTITLEMENT"
        ):
            _build_updates(
                {
                    "financial": {
                        "alpha_vantage_quote_entitlement": "unlicensed_magic",
                    }
                },
                current,
            )

    def test_tushare_public_status_reports_unconfigured_and_persisted_partial_probe(self):
        with patch.object(config_management_api.config, "TUSHARE_TOKEN", ""):
            unconfigured = _public_config({})["financial"]["tushare_status"]
        self.assertEqual(unconfigured["availability"], "not_configured")
        # 用 addCleanup 而不是 with：Windows 上 SQLite 文件句柄未释放时
        # TemporaryDirectory.__exit__ 会删不掉文件（PermissionError）。
        temp = Path(tempfile.mkdtemp(prefix="financial-config-probe-"))
        self.addCleanup(shutil.rmtree, temp, ignore_errors=True)
        database_path = temp / "provider-status.sqlite3"
        connection = sqlite3.connect(database_path, isolation_level=None)
        ensure_financial_tables(connection.cursor())
        probe = {
            "probe_version": "tushare-permissions-v1",
            "checked_at": "2026-07-31T02:00:00Z",
            "overall": "partial",
            "token_status": "valid",
            "capabilities": {
                "daily_market": "available",
                "realtime_equity": "no_permission",
            },
        }
        connection.execute(
            "INSERT INTO financial_provider_profiles("
            "provider_key, display_name, provider_type, access_tier, "
            "capabilities_json, metadata_json) VALUES(?, ?, ?, ?, ?, ?)",
            (
                "tushare_cn", "Tushare", "sdk_adapter", "account",
                "[]", json.dumps({"permission_probe": probe}),
            ),
        )
        connection.close()
        self._assert_persisted_probe_status(database_path)

    def _assert_persisted_probe_status(self, database_path):
        # 已知产品缺陷（db_connection.connect_database 忽略 path 参数）：
        # _tushare_public_status 会传 path=str(database_path)，但实现里
        # 永远用 config.SQLITE_BACKUP_PATH / config.DATABASE_PATH
        # （两者都被 .env 固化成 data/crawler_articles.db），因此这里必须
        # 同时改这两个常量，用例才能真正读到自己的临时库。
        # 同时把 db_connection.database_type 固定为 sqlite：否则它会连共享
        # PostgreSQL 主库，_invalidate_tushare_probe_after_token_change()
        # 会真的 UPDATE 主库的 financial_provider_profiles。
        with patch("sqlite_database.sqlite_db", self._isolated_main_db()), patch.object(
            config_management_api.config, "SQLITE_BACKUP_PATH", ""
        ), patch.object(
            config_management_api.config, "DATABASE_PATH", str(database_path)
        ), patch("db_connection.database_type", lambda: "sqlite"):
            payload = _public_config(
                {
                    "DATABASE_PATH": str(database_path),
                    "FINANCIAL_INTELLIGENCE_ENABLED": "true",
                    "TUSHARE_CN_ENABLED": "true",
                    "TUSHARE_TOKEN": "status-secret-never-return",
                    # _managed_rollout_stage() 在缺少 FINANCIAL_ROLLOUT_STAGE 时
                    # 回退到 'off'（fail-closed），会把 tushare_cn 判为 disabled；
                    # 本用例验证的是探针状态投影，因此显式给出阶段。
                    "FINANCIAL_ROLLOUT_STAGE": "snapshot_readonly",
                    "INTEL_DEFAULT_INDUSTRY_PACK": "family_office",
                }
            )
            status = payload["financial"]["tushare_status"]
            self.assertEqual(status["availability"], "partial")
            self.assertEqual(status["token_status"], "valid")
            self.assertEqual(status["capabilities"]["daily_market"], "available")
            self.assertEqual(
                status["capabilities"]["realtime_equity"], "no_permission"
            )
            self.assertNotIn(
                "status-secret-never-return", json.dumps(payload, ensure_ascii=False)
            )

            _invalidate_tushare_probe_after_token_change(
                {
                    "DATABASE_PATH": str(database_path),
                    "TUSHARE_TOKEN": "replacement-secret-never-persist",
                }
            )
            reset = _public_config(
                {
                    "DATABASE_PATH": str(database_path),
                    "FINANCIAL_INTELLIGENCE_ENABLED": "true",
                    "TUSHARE_CN_ENABLED": "true",
                    "TUSHARE_TOKEN": "replacement-secret-never-persist",
                    "FINANCIAL_ROLLOUT_STAGE": "snapshot_readonly",
                    "INTEL_DEFAULT_INDUSTRY_PACK": "family_office",
                }
            )["financial"]["tushare_status"]
        self.assertEqual(reset["availability"], "configured_unverified")
        with sqlite3.connect(database_path) as verification:
            stored = verification.execute(
                "SELECT metadata_json FROM financial_provider_profiles "
                "WHERE provider_key='tushare_cn'"
            ).fetchone()[0]
        self.assertNotIn("replacement-secret-never-persist", stored)

    def test_non_admin_cannot_update_and_admin_response_never_returns_token(self):
        app = Flask("financial-config-test")
        app.register_blueprint(config_management_bp)
        app.config["TESTING"] = True
        with tempfile.TemporaryDirectory() as temp:
            env_path = Path(temp) / ".env"
            env_path.write_text("TUSHARE_TOKEN=existing-secret\n", encoding="utf-8")
            with patch.object(config_management_api, "ENV_PATH", env_path), patch(
                "decorators.user_db.verify_session",
                return_value={"user_id": 2, "role": "user"},
            ):
                client = app.test_client()
                denied = client.put(
                    "/api/config-management/config",
                    json={"financial": {"financial_intelligence_enabled": True}},
                    headers=AUTH_HEADERS,
                )
                self.assertEqual(denied.status_code, 403)
                self.assertEqual(
                    env_path.read_text(encoding="utf-8"),
                    "TUSHARE_TOKEN=existing-secret\n",
                )

            with patch.object(config_management_api, "ENV_PATH", env_path), patch(
                "decorators.user_db.verify_session",
                return_value={"user_id": 1, "role": "admin"},
            ), patch.object(config_management_api, "_apply_runtime_values"):
                client = app.test_client()
                response = client.put(
                    "/api/config-management/config",
                    json={
                        "financial": {
                            "financial_intelligence_enabled": True,
                            "trading_agents_enabled": False,
                            "auto_research_enabled": False,
                            "simulation_enabled": False,
                            "akshare_cn_enabled": False,
                            "tushare_cn_enabled": True,
                            "provider_timeout_seconds": 20,
                            "provider_max_retries": 2,
                            "provider_max_concurrency": 4,
                            "provider_daily_call_budget": 1000,
                            "quote_freshness_seconds": 300,
                            "market_breadth_freshness_seconds": 300,
                            "news_freshness_seconds": 3600,
                            "fundamental_freshness_seconds": 86400,
                            "research_max_llm_calls": 30,
                            "research_max_tokens": 120000,
                            "research_max_debate_rounds": 2,
                            "research_timeout_seconds": 1800,
                            "research_cache_seconds": 3600,
                            "auto_research_daily_budget": 6,
                            "tushare_token": "",
                            "clear_tushare_token": False,
                        }
                    },
                    headers=AUTH_HEADERS,
                )
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                body = response.get_json()
                self.assertTrue(body["financial"]["tushare_token_configured"])
                self.assertNotIn("existing-secret", response.get_data(as_text=True))
                self.assertNotIn("tushare_token", body["financial"])
                invalid = client.put(
                    "/api/config-management/config",
                    json={"financial": {"provider_timeout_seconds": 999}},
                    headers=AUTH_HEADERS,
                )
                self.assertEqual(invalid.status_code, 400)
                self.assertIn("FINANCIAL_PROVIDER_TIMEOUT_SECONDS", invalid.get_json()["error"])
                invalid_cache = client.put(
                    "/api/config-management/config",
                    json={"financial": {"research_cache_seconds": 30}},
                    headers=AUTH_HEADERS,
                )
                self.assertEqual(invalid_cache.status_code, 400)
                self.assertIn(
                    "FINANCIAL_RESEARCH_CACHE_SECONDS",
                    invalid_cache.get_json()["error"],
                )

    def test_admin_tushare_probe_uses_server_secret_and_returns_only_status(self):
        app = Flask("tushare-probe-test")
        app.register_blueprint(config_management_bp)
        app.config["TESTING"] = True

        class FakeProvider:
            received_settings = None

            def __init__(self, *, instrument_registry, settings, connection):
                self.__class__.received_settings = dict(settings)

            def probe_permissions(self, *, request_id, requested_at):
                self.assertions = (request_id, requested_at)
                return {
                    "overall": "partial",
                    "token_status": "valid",
                    "checked_at": "2026-07-31T02:00:00Z",
                    "capabilities": {
                        "daily_market": "available",
                        "realtime_equity": "no_permission",
                    },
                }

        with tempfile.TemporaryDirectory() as temp:
            database_path = Path(temp) / "probe.sqlite3"
            env_path = Path(temp) / ".env"
            env_path.write_text(
                "\n".join(
                    (
                        f"DATABASE_PATH={database_path}",
                        "FINANCIAL_INTELLIGENCE_ENABLED=true",
                        "TUSHARE_CN_ENABLED=true",
                        "TUSHARE_TOKEN=probe-secret-never-return",
                        "FINANCIAL_PROVIDER_TIMEOUT_SECONDS=20",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            with patch.object(config_management_api, "ENV_PATH", env_path), patch.object(
                config_management_api, "TushareCNProvider", FakeProvider
            ), patch(
                "decorators.user_db.verify_session",
                return_value={"user_id": 1, "role": "admin"},
            ):
                client = app.test_client()
                response = client.post(
                    "/api/config-management/financial/tushare/probe",
                    headers=AUTH_HEADERS,
                )
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                body = response.get_json()
                self.assertEqual(body["tushare_status"]["availability"], "partial")
                self.assertNotIn("probe-secret-never-return", response.get_data(as_text=True))
                self.assertEqual(
                    FakeProvider.received_settings["TUSHARE_TOKEN"],
                    "probe-secret-never-return",
                )


if __name__ == "__main__":
    unittest.main()

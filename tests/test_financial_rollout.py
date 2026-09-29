#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from flask import Flask

import config
import intel_api
import mapindex_api
from chat_route_orchestrator import ChatRouteOrchestrator
from config_management_api import _rollout_transition_for_updates
from financial_config import financial_capabilities, financial_product_capabilities
from financial_rollout import (
    ROLLOUT_STAGE_INDEX,
    ROLLOUT_STAGE_KEYS,
    financial_rollout_state,
    rollout_transition_decision,
)
from financial_worker_jobs import _job_rollout_requirement
from intel_api import intel_bp


UTC = timezone.utc
NOW = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)


def _settings(stage: str) -> dict:
    return {
        "INTEL_DEFAULT_INDUSTRY_PACK": "family_office",
        "FINANCIAL_ROLLOUT_STAGE": stage,
        "FINANCIAL_ROLLOUT_STAGE_CHANGED_AT": "2026-08-03T06:00:00Z",
        "FINANCIAL_ROLLOUT_OBSERVATION_SECONDS": 60,
        "FINANCIAL_INTELLIGENCE_ENABLED": True,
        "TRADING_AGENTS_ENABLED": True,
        "FINANCIAL_AUTO_RESEARCH_ENABLED": True,
        "TRADING_SIMULATION_ENABLED": True,
        "AKSHARE_CN_ENABLED": True,
        "TUSHARE_CN_ENABLED": True,
        "TUSHARE_TOKEN": "fixture-token",
        "YAHOO_FINANCE_ENABLED": True,
        "ALPHA_VANTAGE_ENABLED": True,
        "ALPHA_VANTAGE_API_KEY": "fixture-alpha",
        "FRED_ENABLED": True,
        "FRED_API_KEY": "fixture-fred",
        "POLYMARKET_ENABLED": True,
        "EASYQUOTATION_ENABLED": True,
        "OFFICIAL_FINANCIAL_EVIDENCE_ENABLED": True,
    }


def _healthy_metrics() -> dict:
    return {
        "status": "healthy",
        "metrics": {
            "sources": {
                "enabled": 1,
                "items": [{"enabled": True, "last_success_age_seconds": 10}],
            },
            "providers": {"enabled": 1},
            "snapshots": {"total": 2, "stale": 0},
            "reports": {
                "scope_coverage": {"equity": 1, "index": 1},
                "failed_research_24h": 0,
            },
            "verification": {"pending_conflicts": 0},
            "budgets": {
                "provider_calls_today": 4,
                "provider_daily_limit": 100,
            },
        },
    }


class FinancialRolloutPolicyTests(unittest.TestCase):
    def test_all_stages_are_cumulative_and_product_gates_match_order(self):
        for index, stage in enumerate(ROLLOUT_STAGE_KEYS):
            with self.subTest(stage=stage):
                rollout = financial_rollout_state(_settings(stage), now=NOW)
                self.assertEqual(rollout["stage_index"], index)
                for capability, enabled in rollout["capabilities"].items():
                    self.assertEqual(
                        enabled,
                        index >= ROLLOUT_STAGE_INDEX[capability],
                        capability,
                    )
                low_level = financial_capabilities(_settings(stage))
                self.assertEqual(
                    low_level["effective"]["financial_intelligence"],
                    index >= ROLLOUT_STAGE_INDEX["snapshot_readonly"],
                )
                self.assertEqual(
                    low_level["effective"]["trading_agents"],
                    index >= ROLLOUT_STAGE_INDEX["stock_research"],
                )
                self.assertEqual(
                    low_level["effective"]["auto_research"],
                    index >= ROLLOUT_STAGE_INDEX["auto_research"],
                )
                self.assertEqual(
                    low_level["effective"]["simulation"],
                    index >= ROLLOUT_STAGE_INDEX["simulation_backtest"],
                )
                product = financial_product_capabilities(
                    "family_office", settings=_settings(stage)
                )
                self.assertEqual(
                    product["product"]["financial_zone"],
                    index >= ROLLOUT_STAGE_INDEX["dashboard"],
                )

    def test_invalid_stage_fails_closed_without_database_rollback(self):
        state = financial_rollout_state(
            {"FINANCIAL_ROLLOUT_STAGE": "unknown"}, now=NOW
        )
        self.assertEqual(state["stage"], "off")
        self.assertFalse(state["valid"])
        self.assertFalse(any(state["capabilities"].values()))
        self.assertFalse(state["rollback_requires_database"])

    def test_forward_progress_requires_one_level_observation_and_smoke(self):
        bootstrap = rollout_transition_decision("off", "rss", now=NOW)
        self.assertTrue(bootstrap["allowed"])
        self.assertEqual(bootstrap["reason"], "bootstrap_rss_stage")

        skipped = rollout_transition_decision("off", "dashboard", now=NOW)
        self.assertFalse(skipped["allowed"])
        self.assertEqual(skipped["reason"], "rollout_stage_skip_forbidden")

        missing = rollout_transition_decision(
            "rss", "snapshot_readonly", health=_healthy_metrics(), now=NOW
        )
        self.assertEqual(missing["reason"], "rollout_observation_start_missing")
        incomplete = rollout_transition_decision(
            "rss",
            "snapshot_readonly",
            changed_at="2026-08-03T07:59:30Z",
            observation_seconds=60,
            health=_healthy_metrics(),
            now=NOW,
        )
        self.assertEqual(incomplete["reason"], "rollout_observation_incomplete")

        degraded = _healthy_metrics()
        degraded["status"] = "degraded"
        rejected = rollout_transition_decision(
            "rss",
            "snapshot_readonly",
            changed_at="2026-08-03T06:00:00Z",
            observation_seconds=60,
            health=degraded,
            now=NOW,
        )
        self.assertEqual(rejected["reason"], "rollout_smoke_failed")

        for index in range(1, len(ROLLOUT_STAGE_KEYS) - 1):
            current = ROLLOUT_STAGE_KEYS[index]
            target = ROLLOUT_STAGE_KEYS[index + 1]
            with self.subTest(current=current, target=target):
                decision = rollout_transition_decision(
                    current,
                    target,
                    changed_at="2026-08-03T06:00:00Z",
                    observation_seconds=60,
                    health=_healthy_metrics(),
                    now=NOW,
                )
                self.assertTrue(decision["allowed"], decision)
                self.assertEqual(decision["reason"], "healthy_observation_complete")

    def test_every_enabled_stage_can_roll_back_immediately(self):
        for stage in ROLLOUT_STAGE_KEYS[1:]:
            with self.subTest(stage=stage):
                decision = rollout_transition_decision(
                    stage, "off", health={"status": "critical"}, now=NOW
                )
                self.assertTrue(decision["allowed"])
                self.assertEqual(decision["action"], "rollback")
                self.assertFalse(decision["rollback_requires_database"])

    def test_config_transition_stamps_window_only_for_real_stage_change(self):
        updates = {
            "FINANCIAL_ROLLOUT_STAGE": "rss",
            "FINANCIAL_ROLLOUT_OBSERVATION_SECONDS": "60",
        }
        decision = _rollout_transition_for_updates(
            {}, updates, health={}, now=NOW
        )
        self.assertTrue(decision["allowed"])
        self.assertEqual(
            updates["FINANCIAL_ROLLOUT_STAGE_CHANGED_AT"],
            "2026-08-03T08:00:00Z",
        )

        skipped_updates = {"FINANCIAL_ROLLOUT_STAGE": "dashboard"}
        skipped = _rollout_transition_for_updates(
            {}, skipped_updates, health=_healthy_metrics(), now=NOW
        )
        self.assertFalse(skipped["allowed"])
        self.assertNotIn("FINANCIAL_ROLLOUT_STAGE_CHANGED_AT", skipped_updates)

        legacy_updates = {"FINANCIAL_ROLLOUT_STAGE": "simulation_backtest"}
        legacy = _rollout_transition_for_updates(
            {"FINANCIAL_INTELLIGENCE_ENABLED": "true"},
            legacy_updates,
            health={},
            now=NOW,
        )
        self.assertTrue(legacy["allowed"])
        self.assertEqual(legacy["action"], "no_change")
        self.assertNotIn("FINANCIAL_ROLLOUT_STAGE_CHANGED_AT", legacy_updates)

    def test_ai_worker_and_history_entry_points_fail_closed_at_their_stage(self):
        classifier = Mock()
        plan = ChatRouteOrchestrator(
            clock=lambda: NOW,
            intent_classifier=classifier,
            financial_settings=_settings("dashboard"),
        ).plan({
            "session_id": "rollout-chat",
            "model": "local",
            "messages": [{"role": "user", "content": "腾讯现在股价"}],
            "web_search": False,
            "user_timezone": "Asia/Hong_Kong",
        })
        classifier.classify.assert_not_called()
        self.assertIn(
            "rollout_stage_ai_fact_not_reached",
            plan.financial_intent["reason_codes"],
        )

        self.assertEqual(
            _job_rollout_requirement(
                "financial_research",
                {"scope_type": "instrument", "asset_type": "equity"},
            ),
            "stock_research",
        )
        self.assertEqual(
            _job_rollout_requirement(
                "financial_research",
                {"scope_type": "universe", "asset_type": "index"},
            ),
            "index_research",
        )

        audited = [{
            "source_chat_history_id": 1,
            "question": "当时价格？",
            "answer": "历史答案",
            "model_id": "fixture",
            "source_session_id": "old-session",
            "financial_audit": {"route_key": "fixture"},
        }]
        with patch.object(config, "FINANCIAL_ROLLOUT_STAGE", "index_research"), patch.object(
            mapindex_api.sqlite_db, "save_chat_qa", return_value=1
        ) as save:
            result = mapindex_api._create_gcd_review_session(audited, ["old-session"])
        self.assertEqual(result["financial_review"]["status"], "skipped")
        self.assertEqual(result["kept"], 1)
        save.assert_called_once()

    def test_admin_rollout_status_is_safe_and_read_only(self):
        app = Flask(__name__)
        app.config.update(TESTING=True)
        app.register_blueprint(intel_bp)
        client = app.test_client()
        self.assertEqual(client.get("/api/intel/financial/rollout").status_code, 401)
        health = {**_healthy_metrics(), "checked_at": "2026-08-03T08:00:00Z", "alert_count": 0}
        with patch(
            "decorators.user_db.verify_session",
            return_value={"user_id": 1, "role": "admin"},
        ), patch.object(
            intel_api.FinancialHealthService, "build", return_value=health
        ), patch.object(
            config, "FINANCIAL_ROLLOUT_STAGE", "rss"
        ), patch.object(
            config, "FINANCIAL_ROLLOUT_STAGE_CHANGED_AT", "2026-08-03T06:00:00Z"
        ), patch.object(
            config, "FINANCIAL_ROLLOUT_OBSERVATION_SECONDS", 60
        ):
            response = client.get(
                "/api/intel/financial/rollout",
                headers={"Authorization": "Bearer fixture"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["rollout"]["stage"], "rss")
        self.assertFalse(payload["rollout"]["rollback_requires_database"])
        self.assertNotIn("configured", str(payload.get("health")))


if __name__ == "__main__":
    unittest.main()

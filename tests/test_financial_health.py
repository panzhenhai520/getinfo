#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


_BOOTSTRAP_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = os.path.join(
    _BOOTSTRAP_TEMP_DIR.name, "financial-health-bootstrap.sqlite3"
)

from flask import Flask

import intel_api
from financial_health import FinancialHealthService
from intel_api import intel_bp
from intel_database import IntelRepository
from sqlite_database import SQLiteDatabase


UTC = timezone.utc
NOW = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)
SETTINGS = {
    "FINANCIAL_INTELLIGENCE_ENABLED": True,
    "FINANCIAL_HEALTH_PROVIDER_MAX_AGE_SECONDS": 3600,
    "FINANCIAL_HEALTH_SOURCE_MAX_AGE_SECONDS": 3600,
    "FINANCIAL_HEALTH_JOB_MAX_AGE_SECONDS": 300,
    "FINANCIAL_HEALTH_LLM_P95_MS": 1000,
    "FINANCIAL_HEALTH_REPORT_MAX_AGE_SECONDS": 3600,
    "FINANCIAL_PROVIDER_DAILY_CALL_BUDGET": 100,
}


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


class FinancialHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(
            str(Path(self.temp_dir.name) / "financial-health.sqlite3")
        )
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.connection = self.database.connection
        self.repository = IntelRepository(self.database)
        self.ids = self._seed_degraded_state()

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _seed_degraded_state(self):
        old = _utc(NOW - timedelta(hours=4))
        expired = _utc(NOW - timedelta(minutes=1))
        provider_id = int(self.connection.execute(
            """
            INSERT INTO financial_provider_profiles(
                provider_key,display_name,provider_type,is_enabled,health_status,
                last_health_check_at,metadata_json
            ) VALUES('akshare_cn','AKShare','market_data',1,'permission_denied',?,?)
            """,
            (old, '{"credential":"TOP-SECRET-provider"}'),
        ).lastrowid)
        source_id = int(self.connection.execute(
            """
            INSERT INTO intel_sources(
                canonical_source_url,source_url,source_name,source_type,
                polling_interval_minutes,is_enabled,last_scan_at,
                last_successful_scan_at,last_scan_status,last_scan_error,
                consecutive_scan_failures
            ) VALUES('https://source.invalid/feed','https://TOP-SECRET-source.invalid',
                     'Fixture','rss',5,1,?,?,'failed','TOP-SECRET-source-error',2)
            """,
            (old, old),
        ).lastrowid)
        self.connection.execute(
            "INSERT INTO intel_source_industries(source_id,industry_pack_id) VALUES(?,'financial_markets')",
            (source_id,),
        )
        instrument_id = int(self.connection.execute(
            """
            INSERT INTO financial_instruments(
                canonical_symbol,display_name,asset_type,market,exchange,currency
            ) VALUES('000001.SH','上证指数','index','CN','XSHG','CNY')
            """
        ).lastrowid)
        payload = '{"secret":"TOP-SECRET-snapshot"}'
        self.connection.execute(
            """
            INSERT INTO financial_data_snapshots(
                snapshot_key,instrument_id,provider_profile_id,data_type,
                observed_at,fetched_at,stale_after,quality_status,payload_json,
                payload_sha256,source_url
            ) VALUES('health-snapshot',?,?,'quote',?,?,?,'unverified',?,
                     'fixture-hash','https://TOP-SECRET-snapshot.invalid')
            """,
            (instrument_id, provider_id, old, old, expired, payload),
        )
        for run_id, status in (("health-failed", "failed"), ("health-completed", "completed")):
            self.connection.execute(
                """
                INSERT INTO financial_research_runs(
                    id,trigger_type,scope_type,instrument_id,chat_session_id,
                    user_question,status,current_stage,llm_call_budget,token_budget,
                    last_error,requested_at,updated_at,completed_at
                ) VALUES(?,'chat','instrument',?,'TOP-SECRET-session',
                         'TOP-SECRET-question',?,'final_report',3,400,
                         'TOP-SECRET-run-error',?,?,?)
                """,
                (run_id, instrument_id, status, old, old, old),
            )
        queued_id = int(self.connection.execute(
            """
            INSERT INTO intel_jobs(
                job_type,dedupe_key,payload_json,status,created_at,updated_at,last_error
            ) VALUES('financial_snapshot','health-queued',?,'queued',?,?,?)
            """,
            ('{"secret":"TOP-SECRET-job-payload"}', old, old, 'TOP-SECRET-job-error'),
        ).lastrowid)
        running_id = int(self.connection.execute(
            """
            INSERT INTO intel_jobs(
                job_type,dedupe_key,payload_json,status,lease_owner,
                lease_expires_at,created_at,updated_at,last_error
            ) VALUES('financial_research','health-running',?,'running',
                     'TOP-SECRET-worker',?,?,?,?)
            """,
            ('{"secret":"TOP-SECRET-running-payload"}', expired, old, old, 'TOP-SECRET-lease-error'),
        ).lastrowid)
        self.connection.execute(
            """
            INSERT INTO llm_call_audit(
                call_id,role_key,profile_key,model_id,prompt_sha256,
                response_sha256,input_tokens,output_tokens,latency_ms,status,
                error_code,request_id,started_at,completed_at
            ) VALUES('health-call','TOP-SECRET-role','fast','TOP-SECRET-model',
                     'TOP-SECRET-prompt','',100,20,2500,'failed',
                     'request_timeout','TOP-SECRET-request',?,?)
            """,
            (old, old),
        )
        claim_id = int(self.connection.execute(
            """
            INSERT INTO financial_claims(
                research_run_id,claim_key,claim_type,subject,statement,
                verification_status,created_at,updated_at
            ) VALUES('health-completed','health-claim','metric','fixture',
                     'TOP-SECRET-statement','pending',?,?)
            """,
            (old, old),
        ).lastrowid)
        verdict_id = int(self.connection.execute(
            """
            INSERT INTO financial_verdicts(
                claim_id,verdict,rationale,adjudicator,model_id,decided_at
            ) VALUES(?,'unresolved_conflict','TOP-SECRET-rationale',
                     'TOP-SECRET-adjudicator','TOP-SECRET-verdict-model',?)
            """,
            (claim_id, old),
        ).lastrowid)
        self.connection.execute(
            """
            INSERT INTO intel_api_usage(usage_date,service,usage_count,updated_at)
            VALUES('2026-08-03','akshare_cn',95,?)
            """,
            (old,),
        )
        self.connection.commit()
        return {
            "provider": provider_id,
            "source": source_id,
            "instrument": instrument_id,
            "queued_job": queued_id,
            "running_job": running_id,
            "claim": claim_id,
            "verdict": verdict_id,
        }

    def _service(self):
        return FinancialHealthService(
            self.database, settings=SETTINGS, clock=lambda: NOW
        )

    def test_faults_have_metrics_and_actionable_alerts_without_sensitive_data(self):
        before = self.connection.total_changes
        health = self._service().build()
        self.assertEqual(self.connection.total_changes, before)
        self.assertEqual(health["status"], "critical")
        codes = {item["code"] for item in health["alerts"]}
        self.assertTrue({
            "provider_permission_denied",
            "provider_health_check_stale",
            "financial_source_scan_failed",
            "financial_source_stale",
            "financial_snapshot_stale",
            "financial_market_scheduler_inactive",
            "financial_worker_lease_expired",
            "financial_job_backlog_stale",
            "llm_timeout_failures",
            "llm_latency_p95_high",
            "financial_research_failed",
            "completed_research_missing_report",
            "verification_conflicts_pending",
            "provider_daily_budget_near_limit",
        }.issubset(codes), codes)
        self.assertEqual(health["metrics"]["jobs"]["expired_running_leases"], 1)
        self.assertEqual(health["metrics"]["llm"]["timeouts_24h"], 1)
        self.assertEqual(health["metrics"]["verification"]["pending_conflicts"], 1)
        self.assertEqual(health["metrics"]["budgets"]["provider_calls_today"], 95)
        encoded = json.dumps(health, ensure_ascii=False)
        self.assertNotIn("TOP-SECRET", encoded)
        forbidden_keys = {
            "payload_json", "last_error", "user_question", "chat_session_id",
            "prompt_sha256", "response_sha256", "model_id", "request_id",
            "source_url", "last_scan_error", "statement", "rationale",
        }
        self.assertTrue(forbidden_keys.isdisjoint(set(_keys(health))))

    def test_recovery_clears_alerts_and_public_availability_is_deterministic(self):
        degraded = self._service().build()
        public = FinancialHealthService.public_availability(degraded)
        self.assertEqual(public["status"], "unavailable")
        self.assertIn("worker", public["message"])

        now_text = _utc(NOW)
        fresh_until = _utc(NOW + timedelta(hours=1))
        self.connection.execute(
            """UPDATE financial_provider_profiles
               SET health_status='healthy',last_health_check_at=? WHERE id=?""",
            (now_text, self.ids["provider"]),
        )
        self.connection.execute(
            """UPDATE intel_sources SET last_scan_at=?,last_successful_scan_at=?,
               last_scan_status='completed',last_scan_error='',consecutive_scan_failures=0
               WHERE id=?""",
            (now_text, now_text, self.ids["source"]),
        )
        self.connection.execute(
            """UPDATE financial_data_snapshots SET fetched_at=?,stale_after=?,
               quality_status='verified' WHERE snapshot_key='health-snapshot'""",
            (now_text, fresh_until),
        )
        self.connection.execute(
            """UPDATE intel_jobs SET status='completed',lease_owner=NULL,
               lease_expires_at=NULL,updated_at=?,completed_at=?
               WHERE id IN (?,?)""",
            (now_text, now_text, self.ids["queued_job"], self.ids["running_job"]),
        )
        self.connection.execute("DELETE FROM llm_call_audit")
        self.connection.execute(
            "UPDATE financial_research_runs SET status='completed',updated_at=?,completed_at=?",
            (now_text, now_text),
        )
        for version, run_id in enumerate(("health-failed", "health-completed"), start=1):
            self.connection.execute(
                """
                INSERT INTO financial_final_reports(
                    research_run_id,report_version,report_status,recommendation,
                    title,observed_at,fetched_at,verified_at,created_at,updated_at
                ) VALUES(?,?,'verified','Hold','fixture',?,?,?,?,?)
                """,
                (run_id, version, now_text, now_text, now_text, now_text, now_text),
            )
        self.connection.execute(
            "UPDATE financial_verdicts SET verdict='verified_current',decided_at=? WHERE id=?",
            (now_text, self.ids["verdict"]),
        )
        self.connection.execute(
            "UPDATE intel_api_usage SET usage_count=10,updated_at=? WHERE usage_date='2026-08-03'",
            (now_text,),
        )
        self.connection.commit()

        recovered = self._service().build()
        self.assertEqual(recovered["status"], "healthy", recovered["alerts"])
        self.assertEqual(recovered["alert_count"], 0)
        self.assertEqual(
            FinancialHealthService.public_availability(recovered)["message"],
            "金融数据链路正常。",
        )

    def test_health_endpoint_is_admin_only(self):
        app = Flask(__name__)
        app.config.update(TESTING=True)
        app.register_blueprint(intel_bp)
        client = app.test_client()
        self.assertEqual(client.get("/api/intel/financial/health").status_code, 401)
        with patch("decorators.user_db.verify_session", return_value={"user_id": 2, "role": "user"}):
            response = client.get(
                "/api/intel/financial/health",
                headers={"Authorization": "Bearer user-fixture"},
            )
            self.assertEqual(response.status_code, 403)
        with patch.object(intel_api, "intel_repository", self.repository), patch(
            "decorators.user_db.verify_session",
            return_value={"user_id": 1, "role": "admin"},
        ):
            response = client.get(
                "/api/intel/financial/health",
                headers={"Authorization": "Bearer admin-fixture"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        self.assertIn("metrics", response.get_json())

    def test_dashboard_uses_server_availability_message_without_html_injection(self):
        template = Path("templates/mapindex.html").read_text(encoding="utf-8")
        self.assertIn("data.availability?.message", template)
        self.assertIn("empty.textContent", template)
        relevant = template[
            template.index("data.availability?.message") - 120:
            template.index("data.availability?.message") + 240
        ]
        self.assertNotIn("innerHTML", relevant)

    def test_dashboard_refreshes_financial_cards_periodically(self):
        template = Path("templates/mapindex.html").read_text(encoding="utf-8")
        self.assertIn("FINANCIAL_FEED_REFRESH_INTERVAL_MS = 5 * 60 * 1000", template)
        self.assertIn("function startFinancialFeedAutoRefresh()", template)
        self.assertIn("loadFinancialFeed(state.financialFeedPage || 1)", template)


if __name__ == "__main__":
    unittest.main()

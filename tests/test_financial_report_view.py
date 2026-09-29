#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_BOOTSTRAP_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = os.path.join(_BOOTSTRAP_TEMP_DIR.name, "bootstrap.sqlite3")

from flask import Flask

import config
import chat_api
from financial_report_view import STOCK_EXPECTED_ROLES, FinancialReportView
from sqlite_database import SQLiteDatabase
from tools.check_financial_report_view import inspect_report_frontend


ROLE_TYPES = {
    "market_analyst": "market_report",
    "sentiment_analyst": "sentiment_report",
    "news_analyst": "news_report",
    "fundamentals_analyst": "fundamentals_report",
    "bull_researcher": "debate_argument",
    "bear_researcher": "debate_argument",
    "investment_debate": "debate_transcript",
    "research_manager": "investment_plan",
    "trader": "transaction_proposal",
    "aggressive_risk_analyst": "risk_argument",
    "conservative_risk_analyst": "risk_argument",
    "neutral_risk_analyst": "risk_argument",
    "risk_debate": "debate_transcript",
    "portfolio_manager": "final_decision",
}


class FinancialReportViewTests(unittest.TestCase):
    def setUp(self):
        self.original_flags = (
            config.FINANCIAL_INTELLIGENCE_ENABLED,
            config.TRADING_AGENTS_ENABLED,
        )
        config.FINANCIAL_INTELLIGENCE_ENABLED = True
        config.TRADING_AGENTS_ENABLED = True
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "report-view.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.report_id = self._seed_report()
        app = Flask(__name__)
        app.config.update(TESTING=True)
        app.register_blueprint(chat_api.chat_bp)
        self.client = app.test_client()

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()
        (
            config.FINANCIAL_INTELLIGENCE_ENABLED,
            config.TRADING_AGENTS_ENABLED,
        ) = self.original_flags

    def _seed_report(self, *, version=1, status="verified", run_id="stock-run") -> int:
        connection = self.database.connection
        instrument_row = connection.execute(
            "SELECT id FROM financial_instruments WHERE canonical_symbol='0700.HK'"
        ).fetchone()
        if instrument_row is None:
            instrument_id = connection.execute(
                """
                INSERT INTO financial_instruments(
                    canonical_symbol, display_name, asset_type, market,
                    exchange, currency
                ) VALUES('0700.HK','腾讯<script>alert(1)</script>','stock','XHKG','XHKG','HKD')
                """
            ).lastrowid
        else:
            instrument_id = int(instrument_row[0])
        connection.execute(
            """
            INSERT OR IGNORE INTO financial_research_runs(
                id, trigger_type, scope_type, instrument_id, status,
                current_stage, requested_at, completed_at
            ) VALUES(?, 'chat','instrument',?,'completed','stock_research_complete',
                     '2026-07-31T02:00:00Z','2026-07-31T03:00:00Z')
            """,
            (run_id, int(instrument_id)),
        )
        report_json = {
            "schema_version": 1,
            "graph_version": "stock-research-graph-v1",
            "output_classification": "research_opinion",
            "execution_allowed": False,
            "as_of": "2026-07-31T02:59:00Z",
            "server_now_utc": "2026-07-31T03:00:00Z",
            "evidence_coverage": 0.75,
            "category_scores": {"market": 1, "sentiment": 0, "news": 1, "fundamentals": 1},
            "degraded_categories": ["sentiment"],
            "role_trace": [{"internal_prompt": "SECRET_PROMPT", "role_key": "bear_researcher"}],
        }
        risk = {
            "aggressive": "进取观点<script>alert(2)</script>",
            "conservative": "反证：估值和波动风险",
            "neutral": "中性风险：等待更多证据",
            "internal_prompt": "SECRET_RISK_PROMPT",
        }
        cursor = connection.execute(
            """
            INSERT INTO financial_final_reports(
                research_run_id, report_version, report_status,
                recommendation, confidence, title, executive_summary,
                report_markdown, report_json, risk_summary_json,
                suitability_notice, disclaimer, observed_at, fetched_at,
                verified_at, updated_at
            ) VALUES(?,?,?,?,0.73,?,?,?,?,?,?,?,
                     '2026-07-31T02:59:00Z','2026-07-31T03:00:00Z',
                     '2026-07-31T03:01:00Z','2026-07-31T03:01:00Z')
            """,
            (
                run_id,
                int(version),
                status,
                "Hold",
                f"腾讯终极报告 v{version}<img src=x onerror=alert(3)>",
                "核心理由：基本面稳定，但估值需要安全边际。",
                "# 完整终极报告\n\n公开角色产物",
                json.dumps(report_json, ensure_ascii=False),
                json.dumps(risk, ensure_ascii=False),
                "仅供研究参考",
                "不构成投资建议",
            ),
        )
        if version == 1:
            for sequence, role in enumerate(STOCK_EXPECTED_ROLES):
                content = (
                    "空方反证：竞争和估值压力。<svg onload=alert(4)>"
                    if role == "bear_researcher"
                    else f"{role} 的公开结构化研究产物。"
                )
                citations = [{
                    "evidence_id": sequence + 1,
                    "kind": "structured_snapshot",
                    "snapshot_id": sequence + 10,
                    "role": role,
                    "observed_at": "2026-07-31T02:59:00Z",
                    "metadata": {"api_key": "SECRET_KEY"},
                }]
                connection.execute(
                    """
                    INSERT INTO financial_report_sections(
                        research_run_id, role_key, section_type, sequence_no,
                        status, content_markdown, content_json, citations_json,
                        model_id, prompt_version
                    ) VALUES(?,?,?,?, 'completed', ?, ?, ?, 'secret-model-id',
                             'secret-prompt-version')
                    """,
                    (
                        run_id, role, ROLE_TYPES[role], sequence, content,
                        json.dumps({"internal_prompt": "SECRET_SECTION_PROMPT"}),
                        json.dumps(citations),
                    ),
                )
        connection.commit()
        return int(cursor.lastrowid)

    def _get(self, report_id=None, *, authenticated=True):
        headers = {"Authorization": "Bearer fixture"} if authenticated else {}
        with patch("sqlite_database.sqlite_db", self.database), patch(
            "decorators.user_db.verify_session",
            return_value={"user_id": 1, "role": "user"},
        ), patch(
            "intel_database.intel_repository.active_industry_pack_id",
            return_value="family_office",
        ):
            return self.client.get(
                f"/api/financial/reports/{int(report_id or self.report_id)}",
                headers=headers,
            )

    def test_authenticated_report_has_one_screen_overview_and_complete_role_groups(self):
        response = self._get()
        self.assertEqual(response.status_code, 200)
        report = response.get_json()["report"]
        self.assertEqual(report["output_classification"], "research_opinion")
        self.assertEqual(report["overview"]["as_of"], "2026-07-31T02:59:00Z")
        self.assertEqual(report["overview"]["evidence_coverage"], 0.75)
        self.assertIn("sentiment", report["overview"]["data_gaps"])
        self.assertIn("空方反证", report["overview"]["counter_evidence"])
        self.assertEqual(report["section_count"], 14)
        self.assertEqual(report["missing_roles"], [])
        self.assertEqual({item["group"] for item in report["sections"]}, {
            "analysis", "bull_bear", "decision", "risk", "final",
        })
        self.assertIn("多空研究动态讨论", {item["name"] for item in report["section_groups"]})

    def test_prompt_model_and_arbitrary_json_fields_never_leave_allow_list(self):
        rendered = json.dumps(self._get().get_json(), ensure_ascii=False)
        self.assertNotIn("SECRET_PROMPT", rendered)
        self.assertNotIn("SECRET_RISK_PROMPT", rendered)
        self.assertNotIn("SECRET_SECTION_PROMPT", rendered)
        self.assertNotIn("SECRET_KEY", rendered)
        self.assertNotIn("secret-model-id", rendered)
        self.assertNotIn("secret-prompt-version", rendered)
        self.assertNotIn('"report_json"', rendered)
        self.assertNotIn('"content_json"', rendered)

    def test_configured_secret_values_are_redacted_from_public_report_content(self):
        secret = "runtime-financial-secret-6-2"
        self.database.connection.execute(
            "UPDATE financial_final_reports SET title=?, executive_summary=? WHERE id=?",
            (
                f"泄漏尝试 api_key={secret}",
                f"Authorization: Bearer {secret}",
                self.report_id,
            ),
        )
        self.database.connection.execute(
            "UPDATE financial_report_sections SET content_markdown=? "
            "WHERE research_run_id='stock-run' AND role_key='news_analyst'",
            (f"外部内容携带 token={secret}",),
        )
        self.database.connection.commit()
        with patch.object(config, "SERPAPI_API_KEY", secret):
            rendered = json.dumps(self._get().get_json(), ensure_ascii=False)
        self.assertNotIn(secret, rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_authentication_draft_and_missing_report_fail_closed(self):
        self.assertEqual(self._get(authenticated=False).status_code, 401)
        draft_id = self._seed_report(version=1, status="draft", run_id="draft-run")
        self.assertEqual(self._get(draft_id).status_code, 404)
        self.assertEqual(self._get(999999).status_code, 404)

    def test_direct_report_url_is_hidden_when_tradingagents_is_disabled(self):
        config.TRADING_AGENTS_ENABLED = False
        response = self._get()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["reason"], "trading_agents_disabled")

    def test_missing_sections_and_report_versions_are_explicit_and_immutable(self):
        self.database.connection.execute(
            "DELETE FROM financial_report_sections WHERE research_run_id='stock-run' AND role_key='sentiment_analyst'"
        )
        self.database.connection.commit()
        missing = FinancialReportView(self.database).get(self.report_id)
        self.assertIn("sentiment_analyst", missing["missing_roles"])
        v2_id = self._seed_report(version=2)
        v1 = self._get(self.report_id).get_json()["report"]
        v2 = self._get(v2_id).get_json()["report"]
        self.assertEqual(v1["report_version"], 1)
        self.assertEqual(v2["report_version"], 2)
        self.assertNotEqual(v1["report_id"], v2["report_id"])

    def test_long_public_role_output_is_preserved(self):
        long_text = "完整角色内容" * 30000
        self.database.connection.execute(
            "UPDATE financial_report_sections SET content_markdown=? "
            "WHERE research_run_id='stock-run' AND role_key='portfolio_manager'",
            (long_text,),
        )
        self.database.connection.commit()
        report = FinancialReportView(self.database).get(self.report_id)
        section = next(item for item in report["sections"] if item["role_key"] == "portfolio_manager")
        self.assertEqual(section["content_markdown"], long_text)

    def test_frontend_uses_text_nodes_expandable_groups_and_mobile_layout(self):
        contract = inspect_report_frontend()
        self.assertTrue(contract["safe"], contract)
        self.assertEqual(contract["unsafe_markers"], [])


if __name__ == "__main__":
    unittest.main()

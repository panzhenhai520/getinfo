import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import chat_api
from chat_route_orchestrator import (
    FINANCIAL_SSE_EVENT_TYPES,
    LEGACY_CHAT_ROUTE,
    ChatRoutePlan,
)
from financial_chat_market_scope import skipped_market_scope
from financial_full_research import skipped_full_research
from financial_realtime_query import skipped_realtime_query
from financial_sse import (
    FINANCIAL_SSE_PROTOCOL_VERSION,
    encode_sse_event,
    report_ready_event,
    route_event,
    sources_event,
)
from sqlite_database import SQLiteDatabase


UTC = timezone.utc
NOW = datetime(2026, 7, 31, 3, 0, tzinfo=UTC)


def _events(response):
    result = []
    for block in response.get_data(as_text=True).split("\n\n"):
        if block.startswith("data:"):
            result.append(json.loads(block[5:].strip()))
    return result


def _target():
    return {
        "instrument_id": 1,
        "canonical_symbol": "0700.HK",
        "display_name": "腾讯控股",
        "asset_type": "stock",
        "market": "XHKG",
        "exchange": "XHKG",
        "currency": "HKD",
        "country_code": "HK",
        "share_class": None,
    }


def _intent():
    return {
        "schema_version": "financial-intent-v1",
        "classification_status": "classified",
        "is_financial": True,
        "intent": "market_fact",
        "asset_type": "stock",
        "candidates": [],
        "market": "XHKG",
        "currency": "HKD",
        "universe": None,
        "as_of": None,
        "freshness": "realtime",
        "needs_clarification": False,
        "needs_full_research": False,
        "confidence": 1.0,
        "reason_codes": ["SECRET_INTERNAL_PROMPT_SENTINEL"],
        "llm_used": False,
        "context_inherited": False,
    }


def _resolution(*, clarification=False):
    if clarification:
        return {
            "status": "clarification_required",
            "targets": [],
            "clarification": {
                "clarification_id": "target-clarification-fixture",
                "field": "exchange",
                "question": "请确认您指的是哪一个腾讯标的？",
                "options": [_target()],
            },
            "route_destination": "financial_clarification",
        }
    return {
        "status": "resolved",
        "targets": [_target()],
        "clarification": {},
        "route_destination": "financial_target_resolved",
    }


def _realtime(status="planned"):
    return {
        **skipped_realtime_query("fixture"),
        "status": status,
        "target": _target(),
        "requested_at_utc": "2026-07-31T03:00:00.000Z",
        "completed_at_utc": "" if status == "planned" else "2026-07-31T03:00:00.100Z",
        "market_session": {
            "market_session_state": "open",
            "market_calendar_id": "XHKG",
        },
        "route_destination": "financial_realtime_snapshot",
        "reason_codes": ["fixture"],
    }


def _completed_realtime():
    return {
        **_realtime("ready"),
        "cache": {"status": "fresh_hit"},
        "refresh": {"status": "not_needed"},
        "evidence": [
            {
                "snapshot_id": 41,
                "provider_id": "yahoo",
                "provider_display_name": "Yahoo Finance",
                "source_url": "https://finance.yahoo.com/quote/0700.HK/",
                "observed_at": "2026-07-31T02:59:58.000Z",
                "fetched_at": "2026-07-31T02:59:59.000Z",
                "market_status": "open",
                "currency": "HKD",
                "price": 500.0,
                "change": 1.0,
                "change_percent": 0.2,
                "freshness": "current",
            },
            {
                "snapshot_id": 42,
                "provider_id": "alpha_vantage",
                "provider_display_name": "Alpha Vantage",
                "source_url": "https://www.alphavantage.co/query?symbol=0700.HKG",
                "observed_at": "2026-07-31T02:59:58.000Z",
                "fetched_at": "2026-07-31T02:59:59.500Z",
                "market_status": "open",
                "currency": "HKD",
                "price": 500.0,
                "change": 1.0,
                "change_percent": 0.2,
                "freshness": "current",
            }
        ],
        "answer_allowed": True,
        "numeric_claims_allowed": True,
        "elapsed_ms": 100,
    }


def _full(status="skipped", *, report=False):
    route = {
        **skipped_full_research("fixture"),
        "status": status,
        "requested_at_utc": "2026-07-31T03:00:00.000Z",
        "completed_at_utc": "2026-07-31T03:00:00.100Z",
        "route_destination": "financial_full_research",
        "reason_codes": ["fixture"],
    }
    if report:
        route.update(
            {
                "status": "cache_hit",
                "reports": [
                    {
                        "report_id": 7,
                        "research_run_id": "research-run-fixture",
                        "report_status": "verified",
                        "recommendation": "hold",
                        "confidence": 0.7,
                        "title": "腾讯控股终极报告",
                        "executive_summary": "多源核验后的测试摘要。",
                        "observed_at": "2026-07-31T02:59:00Z",
                        "fetched_at": "2026-07-31T02:59:30Z",
                        "verified_at": "2026-07-31T03:00:00Z",
                        "target": _target(),
                        "source_refs": [
                            {
                                "source_kind": "source_document",
                                "article_id": 3,
                                "title": "港交所公告",
                                "url": "https://example.test/filing",
                                "observed_at": "2026-07-31",
                                "fetched_at": "2026-07-31T02:50:00Z",
                            }
                        ],
                    }
                ],
                "research_run_ids": ["research-run-fixture"],
                "answer_allowed": True,
            }
        )
    elif status == "queued":
        route.update(
            {
                "jobs": [
                    {
                        "job_id": 9,
                        "status": "queued",
                        "research_run_id": "research-run-fixture",
                        "target": _target(),
                    }
                ],
                "research_run_ids": ["research-run-fixture"],
            }
        )
    return route


def _plan(*, protocol=FINANCIAL_SSE_PROTOCOL_VERSION, clarification=False, realtime=None, full=None, bundle=None):
    return ChatRoutePlan(
        audit_route_key="chat-route-financial-sse-fixture",
        route_key=LEGACY_CHAT_ROUTE,
        stream_protocol_version=protocol,
        public_event_types=FINANCIAL_SSE_EVENT_TYPES,
        requested_model="local",
        web_search=False,
        server_time_context={
            "server_now_utc": "2026-07-31T03:00:00Z",
            "server_timezone": "Asia/Hong_Kong",
            "user_timezone": "Asia/Hong_Kong",
            "clock_source": "application_server",
        },
        time_resolution={"primary_range": None},
        financial_intent=_intent(),
        target_resolution=_resolution(clarification=clarification),
        market_scope=skipped_market_scope("fixture"),
        realtime_query=realtime or skipped_realtime_query("fixture"),
        full_research=full or skipped_full_research("fixture"),
        latest_bundle=bundle or {},
    )


class _FixtureOrchestrator:
    def __init__(self, plans, completed_realtime=None, completed_bundle=None):
        self.plans = list(plans)
        self.completed_realtime = completed_realtime
        self.completed_bundle = completed_bundle
        self.persisted = []

    def plan(self, _payload):
        return self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]

    def activate_market_scope(self, plan):
        return plan

    def activate_instrument_discovery(self, plan, payload):
        return plan

    def activate_full_research(self, plan, _payload):
        return plan

    def execute_realtime_query(self, plan):
        return replace(plan, realtime_query=self.completed_realtime)

    def execute_latest_bundle(self, plan):
        return replace(
            plan,
            latest_bundle=self.completed_bundle,
            realtime_query=self.completed_bundle["quote"],
            news_query=self.completed_bundle["news"],
        )

    def persist(self, plan, payload):
        self.persisted.append((plan, payload))
        return {"status": "persisted", "route_id": len(self.persisted)}


class FinancialSSETest(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.config.update(TESTING=True)
        app.register_blueprint(chat_api.chat_bp)
        self.client = app.test_client()
        self.payload = {
            "model": "local",
            "messages": [{"role": "user", "content": "腾讯现在股价"}],
            "sse_features": [FINANCIAL_SSE_PROTOCOL_VERSION],
        }

    def test_negotiated_realtime_event_order_sources_chunk_boundary_and_one_done(self):
        orchestrator = _FixtureOrchestrator(
            [_plan(realtime=_realtime())], completed_realtime=_completed_realtime()
        )
        with patch.object(chat_api, "chat_route_orchestrator", orchestrator):
            events = _events(self.client.post("/api/chat/send", json=self.payload))
        self.assertEqual(
            [item["type"] for item in events],
            ["status", "route", "research_status", "research_status", "sources", "chunk", "done"],
        )
        self.assertEqual(sum(item["type"] == "done" for item in events), 1)
        self.assertEqual(sum(item["type"] == "chunk" for item in events), 1)
        self.assertLess(
            next(i for i, item in enumerate(events) if item["type"] == "sources"),
            next(i for i, item in enumerate(events) if item["type"] == "chunk"),
        )
        self.assertIn("snapshot #41", events[-2]["content"])
        self.assertNotIn("SECRET_INTERNAL_PROMPT_SENTINEL", json.dumps(events))

    def test_latest_bundle_streams_quote_and_news_sources_before_answer(self):
        news = {
            "status": "ready",
            "target": _target(),
            "requested_at_utc": "2026-07-31T03:00:00.000Z",
            "completed_at_utc": "2026-07-31T03:00:00.100Z",
            "answer_allowed": True,
            "evidence": [
                {
                    "article_id": 71,
                    "source_kind": "source_document",
                    "title": "腾讯发布最新公告",
                    "source_url": "https://example.test/news/71",
                    "domain": "example.test",
                    "observed_at": "2026-07-31T02:30:00.000Z",
                    "fetched_at": "2026-07-31T02:31:00.000Z",
                    "published_at": "2026-07-31T02:30:00Z",
                    "published_precision": "instant",
                }
            ],
        }
        planned = {
            "status": "planned",
            "channels": ["quote", "news"],
            "route_destination": "financial_latest_bundle",
            "quote": _realtime(),
            "news": {**news, "status": "planned", "evidence": [], "answer_allowed": False},
        }
        completed = {
            **planned,
            "status": "ready",
            "requested_at_utc": "2026-07-31T03:00:00.000Z",
            "completed_at_utc": "2026-07-31T03:00:00.100Z",
            "quote": _completed_realtime(),
            "news": news,
            "latest_available": {
                "cutoff_at_utc": "2026-07-31T03:00:00.000Z",
                "user_timezone": "Asia/Hong_Kong",
                "market_timezone": "Asia/Hong_Kong",
            },
            "answer_allowed": True,
        }
        orchestrator = _FixtureOrchestrator(
            [_plan(bundle=planned)], completed_bundle=completed
        )
        with patch.object(chat_api, "chat_route_orchestrator", orchestrator):
            events = _events(self.client.post("/api/chat/send", json=self.payload))

        self.assertEqual(
            [item["type"] for item in events],
            ["status", "route", "research_status", "research_status", "sources", "chunk", "done"],
        )
        source_kinds = {item["source_kind"] for item in events[4]["sources"]}
        self.assertEqual(source_kinds, {"financial_snapshot", "source_document"})
        self.assertIn("500 HKD", events[5]["content"])
        self.assertIn("腾讯发布最新公告", events[5]["content"])

    def test_old_client_does_not_receive_optional_events(self):
        legacy = _plan(protocol="legacy-sse-v1", realtime=_realtime())
        orchestrator = _FixtureOrchestrator(
            [legacy], completed_realtime=_completed_realtime()
        )
        with patch.object(chat_api, "chat_route_orchestrator", orchestrator):
            events = _events(
                self.client.post(
                    "/api/chat/send",
                    json={key: value for key, value in self.payload.items() if key != "sse_features"},
                )
            )
        self.assertEqual([item["type"] for item in events], ["status", "chunk", "done"])

    def test_clarification_event_precedes_natural_language_question(self):
        orchestrator = _FixtureOrchestrator([_plan(clarification=True)])
        with patch.object(chat_api, "chat_route_orchestrator", orchestrator):
            events = _events(self.client.post("/api/chat/send", json=self.payload))
        self.assertEqual(
            [item["type"] for item in events],
            ["status", "route", "clarification", "chunk", "done"],
        )
        self.assertEqual(events[2]["question"], events[3]["content"])

    def test_async_research_then_later_report_ready_uses_same_run(self):
        queued = _plan(full=_full("queued"))
        ready = _plan(full=_full(report=True))
        orchestrator = _FixtureOrchestrator([queued, ready])
        with patch.object(chat_api, "chat_route_orchestrator", orchestrator):
            first = _events(self.client.post("/api/chat/send", json=self.payload))
            second = _events(self.client.post("/api/chat/send", json=self.payload))
        self.assertNotIn("report_ready", [item["type"] for item in first])
        self.assertEqual(first[-1]["type"], "done")
        report_event = next(item for item in second if item["type"] == "report_ready")
        self.assertEqual(report_event["reports"][0]["research_run_id"], "research-run-fixture")
        self.assertEqual(report_event["reports"][0]["report_url"], "/api/financial/reports/7")
        self.assertEqual(second[-1]["type"], "done")

    def test_event_projection_rejects_unknown_and_strips_unsafe_urls_and_secrets(self):
        plan = _plan(full=_full(report=True))
        route = route_event(plan)
        sources = sources_event(
            plan,
            [
                {
                    "source_kind": "source_document",
                    "article_id": 1,
                    "title": "fixture",
                    "url": "javascript:alert(1)",
                    "api_key": "SECRET_API_KEY_SENTINEL",
                    "internal_prompt": "SECRET_PROMPT_SENTINEL",
                }
            ],
        )
        report = report_ready_event(plan, plan.full_research["reports"])
        rendered = json.dumps([route, sources, report], ensure_ascii=False)
        self.assertNotIn("SECRET_", rendered)
        self.assertEqual(sources["sources"][0]["url"], "")
        with self.assertRaises(ValueError):
            encode_sse_event({"type": "unknown_event"})

    def test_frontends_require_done_ignore_unknown_dedupe_done_and_retry_once(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("mapindex.html", "article_management.html"):
            source = (root / "templates" / name).read_text(encoding="utf-8")
            self.assertIn("sse_features: ['financial-sse-v1']", source)
            self.assertIn("Unknown optional events are intentionally ignored", source)
            self.assertIn("reconnectAttempt < 1", source)
            self.assertIn("if (!streamCompleted", source)
            self.assertIn("streamCompleted", source)
            self.assertIn("event.type !== 'done'" if name == "mapindex.html" else "ev.type !== 'done'", source)

    def test_report_ready_url_serves_allow_listed_terminal_report(self):
        with tempfile.TemporaryDirectory() as directory:
            database = SQLiteDatabase(str(Path(directory) / "financial-report.sqlite3"))
            self.assertTrue(database.connect())
            self.assertTrue(database.create_tables())
            database.connection.execute(
                "INSERT INTO financial_research_runs(id, trigger_type, scope_type, status) "
                "VALUES('research-run-fixture', 'chat', 'market', 'completed')"
            )
            cursor = database.connection.execute(
                """
                INSERT INTO financial_final_reports(
                    research_run_id, report_status, recommendation, confidence,
                    title, executive_summary, report_markdown, risk_summary_json,
                    suitability_notice, disclaimer, observed_at, fetched_at, verified_at
                ) VALUES(?, 'verified', 'hold', 0.7, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "research-run-fixture",
                    "终极报告",
                    "测试摘要",
                    "# 终极报告\n\n正文",
                    '{"risk":"medium"}',
                    "仅适用于测试范围",
                    "不构成投资建议",
                    "2026-07-31T02:59:00Z",
                    "2026-07-31T02:59:30Z",
                    "2026-07-31T03:00:00Z",
                ),
            )
            report_id = int(cursor.lastrowid)
            with patch("sqlite_database.sqlite_db", database), patch(
                "decorators.user_db.verify_session",
                return_value={"user_id": 1, "role": "admin"},
            ), patch(
                "intel_database.intel_repository.active_industry_pack_id",
                return_value="family_office",
            ), patch.object(
                chat_api._cfg, "FINANCIAL_INTELLIGENCE_ENABLED", True,
            ), patch.object(
                chat_api._cfg, "TRADING_AGENTS_ENABLED", True,
            ):
                response = self.client.get(
                    f"/api/financial/reports/{report_id}",
                    headers={"Authorization": "Bearer fixture"},
                )
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()["report"]
            self.assertEqual(payload["report_id"], report_id)
            self.assertEqual(payload["report_status"], "verified")
            self.assertNotIn("report_json", payload)
            database.disconnect()


if __name__ == "__main__":
    unittest.main()

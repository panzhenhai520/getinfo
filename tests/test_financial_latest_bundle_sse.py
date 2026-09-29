import json
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from flask import Flask

import chat_api
from chat_route_orchestrator import ChatRoutePlan
from financial_full_research import skipped_full_research
from financial_latest_bundle import skipped_latest_bundle
from financial_news_query import skipped_news_query
from financial_realtime_query import skipped_realtime_query
from financial_sse import FINANCIAL_SSE_PROTOCOL_VERSION


CONTEXT = {
    "server_now_utc": "2026-08-03T04:00:00Z",
    "server_timezone": "Asia/Hong_Kong",
    "user_timezone": "Asia/Hong_Kong",
}
TARGET = {
    "instrument_id": 77,
    "canonical_symbol": "SPCX.US",
    "display_name": "Space Exploration Technologies Corp.",
    "asset_type": "equity",
    "market": "US",
    "exchange": "XNAS",
    "currency": "USD",
    "country_code": "US",
}


def _quote(status="ready"):
    result = skipped_realtime_query("fixture")
    evidence = [] if status == "unavailable" else [{
        "snapshot_id": 91,
        "provider_id": "fixture_quote",
        "provider_display_name": "Fixture Quote",
        "source_url": "https://quote.example.test/SPCX",
        "observed_at": "2026-08-03T03:59:00Z",
        "fetched_at": "2026-08-03T03:59:01Z",
        "market_status": "open",
        "currency": "USD",
        "price": 88.25,
        "change": 1.25,
        "change_percent": 1.44,
    }]
    result.update({
        "status": status,
        "target": dict(TARGET),
        "requested_at_utc": "2026-08-03T04:00:00Z",
        "completed_at_utc": "2026-08-03T04:00:00Z",
        "market_session": {
            "market_calendar_id": "XNAS",
            "market_timezone": "America/New_York",
            "market_session_state": "open",
        },
        "evidence": evidence,
        "answer_allowed": bool(evidence),
        "numeric_claims_allowed": bool(evidence),
        "route_destination": "financial_realtime_snapshot",
    })
    return result


def _news(status="ready"):
    result = skipped_news_query("fixture")
    evidence = [] if status == "unavailable" else [{
        "article_id": 51,
        "source_kind": "source_document",
        "title": "SpaceX 发布业务更新",
        "source_url": "https://news.example.test/spacex",
        "domain": "news.example.test",
        "observed_at": "2026-08-03T03:30:00Z",
        "fetched_at": "2026-08-03T03:31:00Z",
        "published_at": "2026-08-03T03:30:00Z",
        "published_precision": "instant",
        "published_timezone": "UTC",
    }]
    result.update({
        "status": status,
        "target": dict(TARGET),
        "requested_at_utc": "2026-08-03T04:00:00Z",
        "completed_at_utc": "2026-08-03T04:00:00Z",
        "evidence": evidence,
        "answer_allowed": bool(evidence),
        "route_destination": "financial_latest_news",
    })
    return result


def _bundle(status="ready"):
    quote = _quote("unavailable" if status == "unavailable" else "ready")
    news = _news("unavailable" if status == "unavailable" else "ready")
    return {
        "schema_version": "financial-latest-bundle-v1",
        "status": status,
        "channels": ["quote", "news"],
        "requested_at_utc": "2026-08-03T04:00:00Z",
        "completed_at_utc": "2026-08-03T04:00:00Z",
        "quote": quote,
        "news": news,
        "latest_available": {"cutoff_at_utc": "2026-08-03T04:00:00Z"},
        "execution": {"strategy": "parallel", "elapsed_ms": 10},
        "answer_allowed": status != "unavailable",
        "route_destination": "financial_latest_bundle",
        "reason_codes": [f"bundle_{status}"],
    }


def _plan(*, negotiated=True, bundle_status="planned", target_status="resolved"):
    quote = {**_quote(), "status": "planned", "evidence": [], "answer_allowed": False,
             "numeric_claims_allowed": False, "completed_at_utc": ""}
    news = {**_news(), "status": "planned", "evidence": [], "answer_allowed": False,
            "completed_at_utc": ""}
    planned_bundle = _bundle()
    planned_bundle.update({
        "status": bundle_status,
        "quote": quote,
        "news": news,
        "answer_allowed": False,
    })
    if bundle_status == "skipped":
        planned_bundle = skipped_latest_bundle("fixture_skipped")
    target_resolution = {
        "status": target_status,
        "targets": [dict(TARGET)] if target_status == "resolved" else [],
        "route_destination": (
            "financial_latest_bundle" if target_status == "resolved"
            else "financial_instrument_discovery"
        ),
    }
    return ChatRoutePlan(
        audit_route_key="latest-bundle-sse-fixture",
        route_key="legacy_chat",
        stream_protocol_version=(
            FINANCIAL_SSE_PROTOCOL_VERSION if negotiated else "legacy-sse-v1"
        ),
        public_event_types=(),
        requested_model="local",
        web_search=False,
        server_time_context=dict(CONTEXT),
        time_resolution={},
        financial_intent={
            "is_financial": True,
            "intent": "financial_general",
            "freshness": "realtime",
        },
        target_resolution=target_resolution,
        market_scope={"status": "skipped", "route_destination": "normal_chat"},
        realtime_query=quote if bundle_status == "planned" else skipped_realtime_query("fixture"),
        full_research=skipped_full_research("fixture"),
        information_needs={
            "status": "planned",
            "channels": ["quote", "news"],
        },
        instrument_discovery={
            "status": "verification_required" if target_status != "resolved" else "skipped",
            "route_destination": "financial_instrument_discovery",
        },
        news_query=news if bundle_status == "planned" else skipped_news_query("fixture"),
        latest_bundle=planned_bundle,
    )


class _Orchestrator:
    def __init__(self, plan, *, completed_bundle=None, delay=0.0, news=None):
        self.route_plan = plan
        self.completed_bundle = completed_bundle or _bundle()
        self.delay = delay
        self.completed_news = news
        self.persist_calls = 0
        self.calls = []

    def plan(self, _payload):
        return self.route_plan

    def activate_instrument_discovery(self, plan, _payload):
        return plan

    def activate_market_scope(self, plan):
        return plan

    def activate_full_research(self, plan, _payload):
        self.calls.append("activate_full_research")
        return plan

    def persist(self, _plan, _payload):
        self.persist_calls += 1
        return {"status": "persisted"}

    def execute_latest_bundle(self, plan):
        self.calls.append("execute_latest_bundle")
        time.sleep(self.delay)
        return replace(
            plan,
            latest_bundle=self.completed_bundle,
            realtime_query=self.completed_bundle["quote"],
            news_query=self.completed_bundle["news"],
        )

    def execute_news_query(self, plan):
        return replace(plan, news_query=self.completed_news or _news())


class _SlowDiscoveryOrchestrator(_Orchestrator):
    def __init__(self, initial_plan, completed_plan, *, delay=0.15):
        super().__init__(initial_plan)
        self.completed_plan = completed_plan
        self.discovery_delay = delay

    def activate_instrument_discovery(self, plan, _payload):
        self.calls.append("activate_instrument_discovery")
        time.sleep(self.discovery_delay)
        return self.completed_plan


def _events(raw):
    events = []
    for block in raw.decode("utf-8").split("\n\n"):
        if block.startswith("data:"):
            events.append(json.loads(block[5:].strip()))
    return events


class FinancialLatestBundleSSETest(unittest.TestCase):
    def setUp(self):
        app = Flask("financial-latest-bundle-sse")
        app.config.update(TESTING=True, SECRET_KEY="fixture")
        app.register_blueprint(chat_api.chat_bp)
        self.client = app.test_client()
        self.payload = {
            "model": "local",
            "messages": [{"role": "user", "content": "SpaceX 股票最新信息"}],
            "sse_features": [FINANCIAL_SSE_PROTOCOL_VERSION],
        }

    def _post(self, orchestrator, *, buffered=True):
        with patch.object(chat_api, "chat_route_orchestrator", orchestrator), patch.object(
            chat_api,
            "_stream_openai",
            side_effect=AssertionError("latest financial route must not call general model"),
        ):
            return self.client.post(
                "/api/chat/send", json=self.payload, buffered=buffered
            )

    def test_p4_first_status_precedes_slow_channels_and_sources_precede_answer(self):
        orchestrator = _Orchestrator(_plan(), delay=0.15)
        with patch.object(chat_api, "chat_route_orchestrator", orchestrator), patch.object(
            chat_api,
            "_stream_openai",
            side_effect=AssertionError("latest financial route must not call general model"),
        ):
            started = time.monotonic()
            response = self.client.post(
                "/api/chat/send", json=self.payload, buffered=False
            )
            iterator = iter(response.response)
            first = next(iterator)
            first_elapsed = time.monotonic() - started
            raw = first + b"".join(iterator)
        events = _events(raw)

        self.assertLess(first_elapsed, 0.1)
        self.assertEqual(events[0]["type"], "status")
        self.assertLess(
            next(i for i, item in enumerate(events) if item["type"] == "sources"),
            next(i for i, item in enumerate(events) if item["type"] == "chunk"),
        )
        sources = next(item["sources"] for item in events if item["type"] == "sources")
        news = next(item for item in sources if item["source_kind"] == "source_document")
        self.assertEqual(news["published_at"], "2026-08-03T03:30:00Z")
        self.assertEqual(events[-1]["type"], "done")

    def test_p4_latest_evidence_precedes_full_research_activation(self):
        orchestrator = _Orchestrator(_plan())

        response = self._post(orchestrator)

        self.assertEqual(response.status_code, 200)
        self.assertLess(
            orchestrator.calls.index("execute_latest_bundle"),
            orchestrator.calls.index("activate_full_research"),
        )

    def test_p4_legacy_client_receives_only_legacy_events(self):
        plan = _plan(negotiated=False)
        response = self._post(_Orchestrator(plan))
        event_types = [item["type"] for item in _events(response.data)]

        self.assertEqual(event_types, ["status", "chunk", "done"])

    def test_p4_all_channels_unavailable_never_fall_back_to_model(self):
        response = self._post(
            _Orchestrator(_plan(), completed_bundle=_bundle("unavailable"))
        )
        events = _events(response.data)
        answer = next(item["content"] for item in events if item["type"] == "chunk")

        self.assertIn("暂时没有可核验的价格快照", answer)
        self.assertIn("不会用通用模型补造", answer)

    def test_p5_unverified_discovery_is_closed_before_model_or_web_search(self):
        plan = _plan(bundle_status="skipped", target_status="no_target")
        response = self._post(_Orchestrator(plan))
        events = _events(response.data)
        answer = next(item["content"] for item in events if item["type"] == "chunk")

        self.assertIn("未能从受控来源唯一核验", answer)
        self.assertIn("不会调用通用模型猜测", answer)
        self.assertEqual(events[-1]["type"], "done")

    def test_p5_discovery_status_is_streamed_before_external_lookup(self):
        initial = replace(
            _plan(bundle_status="skipped", target_status="no_target"),
            instrument_discovery={
                "status": "planned",
                "route_destination": "financial_instrument_discovery",
            },
        )
        completed = replace(
            _plan(),
            instrument_discovery={
                "status": "promoted",
                "route_destination": "financial_instrument_discovery",
            },
        )
        orchestrator = _SlowDiscoveryOrchestrator(initial, completed)
        with patch.object(chat_api, "chat_route_orchestrator", orchestrator), patch.object(
            chat_api,
            "_stream_openai",
            side_effect=AssertionError("instrument discovery must not call general model"),
        ):
            started = time.monotonic()
            response = self.client.post(
                "/api/chat/send", json=self.payload, buffered=False
            )
            iterator = iter(response.response)
            first = next(iterator)
            first_elapsed = time.monotonic() - started
            raw = first + b"".join(iterator)

        events = _events(raw)
        self.assertLess(first_elapsed, 0.1)
        self.assertEqual(events[0]["type"], "status")
        self.assertIn("本地尚未登记", events[0]["message"])
        self.assertIn("外部金融信源", events[0]["message"])
        self.assertLess(
            next(
                index
                for index, item in enumerate(events)
                if item.get("stage") == "discover" and item.get("status") == "running"
            ),
            next(
                index
                for index, item in enumerate(events)
                if item.get("stage") == "discover" and item.get("status") == "promoted"
            ),
        )
        self.assertEqual(events[-1]["type"], "done")


if __name__ == "__main__":
    unittest.main()

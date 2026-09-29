import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import chat_api
from chat_route_orchestrator import ChatFinancialRouteStore, ChatRouteOrchestrator
from financial_information_needs import FinancialInformationNeedsPlanner
from financial_instrument_discovery import FinancialInstrumentDiscoveryService
from financial_instruments import InstrumentRegistry
from financial_intent_classifier import FinancialIntentClassifier
from financial_news_query import FinancialNewsQueryService
from financial_realtime_query import FinancialRealtimeQueryService
from financial_sse import FINANCIAL_SSE_PROTOCOL_VERSION
from financial_target_resolver import FinancialTargetResolver
from sqlite_database import SQLiteDatabase


UTC = timezone.utc
NOW = datetime(2026, 8, 3, 15, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]
SETTINGS = {
    "FINANCIAL_INTELLIGENCE_ENABLED": True,
    "FINANCIAL_INFORMATION_NEEDS_ENABLED": True,
    "FINANCIAL_INSTRUMENT_DISCOVERY_ENABLED": True,
    "FINANCIAL_INSTRUMENT_AUTO_PROMOTION_ENABLED": True,
    "FINANCIAL_QUOTE_FRESHNESS_SECONDS": 300,
    "FINANCIAL_NEWS_LOOKBACK_DAYS": 7,
}


class _NoProviderFetch:
    def fetch(self, *args, **kwargs):
        raise AssertionError("fresh persisted fixture must prevent provider fetch")


class _SpacexE2EOrchestrator(ChatRouteOrchestrator):
    def __init__(self, *args, database, **kwargs):
        super().__init__(*args, **kwargs)
        self.database = database

    def activate_instrument_discovery(self, plan, payload):
        completed = super().activate_instrument_discovery(plan, payload)
        if completed.instrument_discovery.get("status") == "promoted":
            self._install_quote_fixture(
                int(completed.target_resolution["targets"][0]["instrument_id"])
            )
        return completed

    def _install_quote_fixture(self, instrument_id):
        connection = self.database.connection
        payload = {
            "metric": "last_price",
            "value": 88.25,
            "normalized_payload": {
                "last_price": 88.25,
                "previous_close": 87.0,
                "change": 1.25,
                "change_percent": 1.4368,
            },
        }
        payload_text = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        for provider_key, display_name, source_url, fetched_at in (
            (
                "yahoo",
                "Yahoo Finance fixture",
                "https://finance.yahoo.com/quote/SPCX/",
                "2026-08-03T14:59:59.000Z",
            ),
            (
                "alpha_vantage",
                "Alpha Vantage fixture",
                "https://example.test/alpha-vantage/SPCX",
                "2026-08-03T14:59:59.500Z",
            ),
        ):
            connection.execute(
                """
                INSERT INTO financial_provider_profiles(
                    provider_key, display_name, provider_type, capabilities_json,
                    is_enabled, attribution_text
                ) VALUES(?, ?, 'market_data', '["quote"]', 1, ?)
                ON CONFLICT(provider_key) DO UPDATE SET is_enabled=1
                """,
                (provider_key, display_name, display_name),
            )
            provider_id = int(
                connection.execute(
                    "SELECT id FROM financial_provider_profiles WHERE provider_key=?",
                    (provider_key,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO financial_data_snapshots(
                    snapshot_key, instrument_id, provider_profile_id, data_type,
                    observed_at, fetched_at, market_status, currency, timezone,
                    stale_after, quality_status, payload_json, payload_sha256,
                    source_url, request_id
                ) VALUES(?, ?, ?, 'quote',
                         '2026-08-03T14:59:58.000Z', ?,
                         'open', 'USD', 'America/New_York',
                         '2026-08-03T15:04:58.000Z', 'normalized_current', ?, ?,
                         ?, 'spacex-e2e')
                """,
                (
                    f"spacex-e2e-quote-{provider_key}",
                    instrument_id,
                    provider_id,
                    fetched_at,
                    payload_text,
                    hashlib.sha256(payload_text.encode("utf-8")).hexdigest(),
                    source_url,
                ),
            )


def _events(response):
    result = []
    for block in response.get_data(as_text=True).split("\n\n"):
        if block.startswith("data:"):
            result.append(json.loads(block[5:].strip()))
    return result


class FinancialSpacexLatestE2ETest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(
            str(Path(self.temp_dir.name) / "spacex-latest-e2e.sqlite3")
        )
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        registry = InstrumentRegistry(self.database.connection)
        registry.load_controlled_seed()
        self.assertIsNone(registry.get_by_canonical_symbol("SPCX.US"))
        self.database.connection.execute(
            """
            INSERT INTO articles(
                url, canonical_url, title, content, domain, publish_date,
                published_at_utc, published_timezone, published_precision,
                published_time_source, first_crawled, status
            ) VALUES(
                'https://news.example.test/spacex-update',
                'https://news.example.test/spacex-update',
                'SpaceX 完成首次公开募股后的首项业务更新',
                'SpaceX 宣布业务进展，本文数字不得用作行情。',
                'news.example.test', '2026-08-03', '2026-08-03T14:30:00Z',
                'America/New_York', 'instant', 'publisher_metadata',
                '2026-08-03T14:31:00Z', 'active'
            )
            """
        )
        classifier = FinancialIntentClassifier(registry)
        resolver = FinancialTargetResolver(registry)
        realtime = FinancialRealtimeQueryService(
            self.database,
            settings=SETTINGS,
            router=_NoProviderFetch(),
            clock=lambda: NOW,
        )
        news = FinancialNewsQueryService(
            self.database,
            settings=SETTINGS,
            clock=lambda: NOW,
        )
        self.orchestrator = _SpacexE2EOrchestrator(
            database=self.database,
            clock=lambda: NOW,
            store=ChatFinancialRouteStore(self.database),
            financial_settings=SETTINGS,
            intent_classifier=classifier,
            target_resolver=resolver,
            information_needs_planner=FinancialInformationNeedsPlanner(settings=SETTINGS),
            instrument_discovery_service=FinancialInstrumentDiscoveryService(
                self.database.connection,
                settings=SETTINGS,
            ),
            realtime_query_service=realtime,
            news_query_service=news,
        )
        app = Flask(__name__)
        app.config.update(TESTING=True, SECRET_KEY="fixture")
        app.register_blueprint(chat_api.chat_bp)
        self.client = app.test_client()
        self.golden = json.loads(
            (ROOT / "tests" / "fixtures" / "financial_latest_information_golden.json").read_text(
                encoding="utf-8"
            )
        )

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def test_empty_registry_discovers_spacex_then_returns_quote_and_news_sse(self):
        request_payload = {
            "session_id": "spacex-e2e-session",
            "model": "local",
            "user_timezone": "Asia/Hong_Kong",
            "messages": [{"role": "user", "content": self.golden["question"]}],
            "sse_features": [FINANCIAL_SSE_PROTOCOL_VERSION],
        }
        with patch.object(chat_api, "chat_route_orchestrator", self.orchestrator), patch.object(
            chat_api,
            "_stream_openai",
            side_effect=AssertionError("financial bundle must not call the general model"),
        ):
            response = self.client.post("/api/chat/send", json=request_payload)
            events = _events(response)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["type"] for item in events],
            self.golden["expected_event_types"],
        )
        route = next(item for item in reversed(events) if item["type"] == "route")
        self.assertEqual(
            route["route_destination"], self.golden["expected_route_destination"]
        )
        self.assertEqual(route["targets"][0]["canonical_symbol"], "SPCX.US")
        sources = next(item for item in events if item["type"] == "sources")
        self.assertEqual(
            sorted({item["source_kind"] for item in sources["sources"]}),
            sorted(self.golden["expected_source_kinds"]),
        )
        answer = next(item["content"] for item in events if item["type"] == "chunk")
        for fragment in self.golden["required_answer_fragments"]:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, answer)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM financial_instruments WHERE canonical_symbol='SPCX.US'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.database.connection.execute(
                "SELECT status FROM financial_instrument_candidates WHERE canonical_symbol='SPCX.US'"
            ).fetchone()[0],
            "promoted",
        )
        latest_route = self.database.connection.execute(
            """
            SELECT route_status, route_destination
            FROM chat_financial_routes
            WHERE session_id='spacex-e2e-session'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        self.assertEqual(tuple(latest_route), ("latest_bundle_ready", "financial_latest_bundle"))

    def test_same_session_follow_up_reuses_spcx_and_queries_quote_only(self):
        first_payload = {
            "session_id": "spacex-follow-up-session",
            "model": "local",
            "user_timezone": "Asia/Hong_Kong",
            "messages": [{"role": "user", "content": self.golden["question"]}],
            "sse_features": [FINANCIAL_SSE_PROTOCOL_VERSION],
        }
        follow_up = {
            **first_payload,
            "messages": [{"role": "user", "content": "那最新股价呢"}],
        }
        with patch.object(chat_api, "chat_route_orchestrator", self.orchestrator), patch.object(
            chat_api,
            "_stream_openai",
            side_effect=AssertionError("financial route must not call the general model"),
        ):
            first_events = _events(self.client.post("/api/chat/send", json=first_payload))
            second_events = _events(self.client.post("/api/chat/send", json=follow_up))

        self.assertEqual(first_events[-1]["type"], "done")
        route = next(item for item in second_events if item["type"] == "route")
        self.assertEqual(route["route_destination"], "financial_realtime_snapshot")
        self.assertEqual(route["targets"][0]["canonical_symbol"], "SPCX.US")
        answer = next(item["content"] for item in second_events if item["type"] == "chunk")
        self.assertIn("88.25 USD", answer)
        self.assertNotIn("SpaceX 完成首次公开募股后的首项业务更新", answer)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM financial_instrument_candidates WHERE canonical_symbol='SPCX.US'"
            ).fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()

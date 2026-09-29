import hashlib
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import chat_api
from chat_route_orchestrator import ChatFinancialRouteStore, ChatRouteOrchestrator
from financial_instruments import InstrumentRegistry
from financial_intent_classifier import FinancialIntentClassifier
from financial_provider_contract import (
    AdjustmentMode,
    FinancialDataKind,
    FinancialDataRecord,
    FinancialProviderResponse,
    FreshnessState,
    InvalidSymbolError,
    MarketStatus,
    PermissionDeniedError,
    TemporarilyUnavailableError,
    raw_response_hash,
)
from financial_realtime_query import (
    FinancialRealtimeQueryService,
    format_realtime_query_answer,
    validate_realtime_query,
)
from financial_target_resolver import FinancialTargetResolver
from sqlite_database import SQLiteDatabase


UTC = timezone.utc
NOW = datetime(2026, 7, 31, 2, 0, tzinfo=UTC)
SETTINGS = {
    "FINANCIAL_INTELLIGENCE_ENABLED": True,
    "FINANCIAL_QUOTE_FRESHNESS_SECONDS": 300,
}


def _utc_text(value):
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _payload(question, session_id="realtime-session"):
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


class _FixtureQuoteRouter:
    def __init__(
        self,
        connection,
        *,
        price=123.45,
        previous_close=120.0,
        observed_at=NOW,
        provider_id="",
        error="",
        delay_seconds=0.0,
    ):
        self.connection = connection
        self.price = float(price)
        self.previous_close = previous_close
        self.observed_at = observed_at
        self.provider_id = provider_id
        self.error = error
        self.delay_seconds = float(delay_seconds)
        self.fetch_calls = []
        self.persist_calls = 0

    def fetch(self, request, *, candidate_provider_ids, allow_fallback):
        self.fetch_calls.append(
            {
                "request": request,
                "candidate_provider_ids": tuple(candidate_provider_ids),
                "allow_fallback": bool(allow_fallback),
            }
        )
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        provider_id = self.provider_id or candidate_provider_ids[0]
        if self.error == "timeout":
            raise TemporarilyUnavailableError(
                "fixture timeout",
                provider_id=provider_id,
                endpoint=request.endpoint,
                request_id=request.request_id,
            )
        if self.error == "permission":
            raise PermissionDeniedError(
                "fixture permission denied",
                provider_id=provider_id,
                endpoint=request.endpoint,
                request_id=request.request_id,
            )
        normalized = {
            "last_price": self.price,
            "previous_close": self.previous_close,
            "open": self.previous_close,
            "high": self.price + 1,
            "low": self.price - 1,
            "volume": 1000.0,
        }
        record = FinancialDataRecord(
            instrument_id=request.instrument_id,
            metric=request.metric,
            value=self.price,
            unit="price",
            currency="HKD" if provider_id == "easyquotation" else "CNY",
            market_status=MarketStatus.OPEN,
            observed_at=self.observed_at,
            fetched_at=request.requested_as_of,
            timezone="Asia/Hong_Kong",
            freshness_state=FreshnessState.CURRENT,
            requested_as_of=request.requested_as_of,
            raw_response_hash=raw_response_hash(normalized),
            normalized_payload=normalized,
            adjustment=AdjustmentMode.RAW,
            source_url=f"https://source.example/{provider_id}",
            provider_symbol="fixture-symbol",
            normalizer_version="realtime-fixture-v1",
            lineage={"freshness_threshold_seconds": 300},
        )
        return FinancialProviderResponse(
            provider_id=provider_id,
            endpoint=request.endpoint,
            license_profile="fixture-research",
            request_id=request.request_id,
            data_kind=FinancialDataKind.QUOTE,
            records=(record,),
        )

    def persist_response(self, response):
        self.persist_calls += 1
        provider_id = response.provider_id
        self.connection.execute(
            """
            INSERT INTO financial_provider_profiles(
                provider_key, display_name, provider_type, access_tier,
                capabilities_json, is_enabled, attribution_text
            ) VALUES(?, ?, 'fixture', 'test', '["quote"]', 1, 'Fixture attribution')
            ON CONFLICT(provider_key) DO UPDATE SET is_enabled=1
            """,
            (provider_id, f"{provider_id} fixture"),
        )
        profile_id = int(
            self.connection.execute(
                "SELECT id FROM financial_provider_profiles WHERE provider_key=?",
                (provider_id,),
            ).fetchone()[0]
        )
        ids = []
        for record in response.records:
            payload = {
                "provider_id": provider_id,
                "endpoint": response.endpoint,
                "data_kind": response.data_kind.value,
                "metric": record.metric,
                "value": record.value,
                "normalized_payload": dict(record.normalized_payload),
                "adjustment": record.adjustment.value,
                "quality_flags": list(record.quality_flags),
                "lineage": dict(record.lineage),
            }
            payload_text = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            payload_hash = hashlib.sha256(payload_text.encode()).hexdigest()
            snapshot_key = hashlib.sha256(
                f"{provider_id}|{response.request_id}|{record.observed_at.isoformat()}".encode()
            ).hexdigest()
            self.connection.execute(
                """
                INSERT INTO financial_data_snapshots(
                    snapshot_key, instrument_id, provider_profile_id, data_type,
                    interval_code, observed_at, fetched_at, market_status,
                    currency, timezone, stale_after, quality_status, payload_json,
                    payload_sha256, source_url, request_id
                ) VALUES(?, ?, ?, 'quote', 'snapshot', ?, ?, ?, ?, ?, ?,
                         'normalized_current', ?, ?, ?, ?)
                """,
                (
                    snapshot_key,
                    int(record.instrument_id),
                    profile_id,
                    _utc_text(record.observed_at),
                    _utc_text(record.fetched_at),
                    record.market_status.value,
                    record.currency,
                    record.timezone,
                    _utc_text(record.observed_at + timedelta(seconds=300)),
                    payload_text,
                    payload_hash,
                    record.source_url,
                    response.request_id,
                ),
            )
            ids.append(
                int(
                    self.connection.execute(
                        "SELECT id FROM financial_data_snapshots WHERE snapshot_key=?",
                        (snapshot_key,),
                    ).fetchone()[0]
                )
            )
        return tuple(ids)


class _NoEligibleQuoteRouter(_FixtureQuoteRouter):
    def provider_availability(self, provider_ids):
        return {
            provider_id: {
                "available": False,
                "reason": f"{provider_id}_disabled",
            }
            for provider_id in provider_ids
        }


class _AlphaInvalidQuoteRouter(_FixtureQuoteRouter):
    def provider_availability(self, provider_ids):
        return {
            provider_id: {
                "available": provider_id != "yahoo",
                "reason": "enabled" if provider_id != "yahoo" else "fixture_disabled",
            }
            for provider_id in provider_ids
        }

    def fetch(self, request, *, candidate_provider_ids, allow_fallback):
        provider_id = candidate_provider_ids[0]
        if provider_id == "alpha_vantage":
            self.fetch_calls.append(
                {
                    "request": request,
                    "candidate_provider_ids": tuple(candidate_provider_ids),
                    "allow_fallback": bool(allow_fallback),
                }
            )
            raise InvalidSymbolError(
                "fixture invalid Alpha symbol",
                provider_id=provider_id,
                endpoint=request.endpoint,
                request_id=request.request_id,
            )
        return super().fetch(
            request,
            candidate_provider_ids=candidate_provider_ids,
            allow_fallback=allow_fallback,
        )


class FinancialRealtimeQueryTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(
            str(Path(self.temp_dir.name) / "financial-realtime-query.sqlite3")
        )
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.registry = InstrumentRegistry(self.database.connection)
        self.registry.load_controlled_seed()
        self.router = _FixtureQuoteRouter(self.database.connection)
        self.service = FinancialRealtimeQueryService(
            self.database,
            settings=SETTINGS,
            router=self.router,
            clock=lambda: NOW,
        )
        self.classifier = FinancialIntentClassifier(self.registry)
        self.resolver = FinancialTargetResolver(self.registry)
        self.store = ChatFinancialRouteStore(self.database)
        self.orchestrator = ChatRouteOrchestrator(
            clock=lambda: NOW,
            store=self.store,
            intent_classifier=self.classifier,
            target_resolver=self.resolver,
            realtime_query_service=self.service,
            financial_settings=SETTINGS,
        )

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _plan(self, question="腾讯今天股价多少", session_id="realtime-session"):
        plan = self.orchestrator.plan(_payload(question, session_id))
        self.assertEqual(plan.realtime_query["status"], "planned")
        return plan

    def _insert_quote(
        self,
        *,
        provider_id="akshare_cn",
        price=100.0,
        observed_at=NOW,
        fetched_at=NOW,
        quality_status="normalized_current",
        instrument_symbol="0700.HK",
    ):
        instrument = self.registry.get_by_canonical_symbol(instrument_symbol)
        self.database.connection.execute(
            """
            INSERT INTO financial_provider_profiles(
                provider_key, display_name, provider_type, access_tier,
                capabilities_json, is_enabled, attribution_text
            ) VALUES(?, ?, 'fixture', 'test', '["quote"]', 1, 'Fixture attribution')
            ON CONFLICT(provider_key) DO UPDATE SET is_enabled=1
            """,
            (provider_id, f"{provider_id} source"),
        )
        profile_id = int(
            self.database.connection.execute(
                "SELECT id FROM financial_provider_profiles WHERE provider_key=?",
                (provider_id,),
            ).fetchone()[0]
        )
        normalized = {
            "last_price": float(price),
            "previous_close": float(price) - 1,
        }
        payload = {
            "provider_id": provider_id,
            "endpoint": "quote",
            "data_kind": "quote",
            "metric": "last_price",
            "value": float(price),
            "normalized_payload": normalized,
            "adjustment": "raw",
            "quality_flags": [],
            "lineage": {"freshness_threshold_seconds": 300},
        }
        payload_text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload_text.encode()).hexdigest()
        key = hashlib.sha256(
            f"{provider_id}|{instrument.instrument_id}|{observed_at.isoformat()}|{price}".encode()
        ).hexdigest()
        self.database.connection.execute(
            """
            INSERT INTO financial_data_snapshots(
                snapshot_key, instrument_id, provider_profile_id, data_type,
                observed_at, fetched_at, market_status, currency, timezone,
                stale_after, quality_status, payload_json, payload_sha256,
                source_url, request_id
            ) VALUES(?, ?, ?, 'quote', ?, ?, 'open', 'HKD', 'Asia/Hong_Kong',
                     ?, ?, ?, ?, ?, 'fixture-cache')
            """,
            (
                key,
                instrument.instrument_id,
                profile_id,
                _utc_text(observed_at),
                _utc_text(fetched_at),
                _utc_text(observed_at + timedelta(seconds=300)),
                quality_status,
                payload_text,
                digest,
                f"https://source.example/{provider_id}",
            ),
        )
        return int(self.database.connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    def test_fresh_cache_returns_without_provider_and_cites_complete_time_source(self):
        snapshot_id = self._insert_quote(
            provider_id="alpha_vantage",
            price=101.5,
            observed_at=NOW - timedelta(seconds=30),
        )
        self._insert_quote(
            provider_id="easyquotation",
            price=101.5,
            observed_at=NOW - timedelta(seconds=30),
        )
        completed = self.orchestrator.execute_realtime_query(self._plan())
        query = validate_realtime_query(completed.realtime_query)
        self.assertEqual(query["status"], "ready")
        self.assertEqual(query["cache"]["status"], "fresh_hit")
        self.assertEqual(self.router.fetch_calls, [])
        self.assertIn(
            snapshot_id,
            {item["snapshot_id"] for item in query["evidence"]},
        )
        self.assertEqual(len(query["evidence"]), 2)
        answer = format_realtime_query_answer(query)
        for field in ("observed_at=", "fetched_at=", "market_status=", "来源="):
            self.assertIn(field, answer)
        self.assertIn("snapshot #", answer)

    def test_open_stale_cache_refreshes_synchronously_and_persists(self):
        old_id = self._insert_quote(
            price=99.0,
            observed_at=NOW - timedelta(hours=2),
            fetched_at=NOW - timedelta(hours=2),
            quality_status="normalized_stale",
        )
        completed = self.orchestrator.execute_realtime_query(self._plan())
        query = completed.realtime_query
        self.assertEqual(query["status"], "ready")
        self.assertEqual(query["refresh"]["status"], "completed")
        self.assertEqual(len(self.router.fetch_calls), 3)
        self.assertTrue(
            all(not item["allow_fallback"] for item in self.router.fetch_calls)
        )
        self.assertEqual(
            {item["candidate_provider_ids"] for item in self.router.fetch_calls},
            {("alpha_vantage",), ("easyquotation",), ("yahoo",)},
        )
        self.assertEqual(self.router.persist_calls, 3)
        self.assertNotEqual(query["evidence"][0]["snapshot_id"], old_id)
        self.assertEqual(query["evidence"][0]["price"], 123.45)
        self.assertEqual(len(query["evidence"]), 3)
        self.assertEqual(query["refresh"]["independent_source_count"], 3)
        self.assertEqual(
            {item["provider_id"] for item in query["refresh"]["source_observations"]},
            {"alpha_vantage", "easyquotation", "yahoo"},
        )
        self.assertEqual(
            int(
                self.database.connection.execute(
                    "SELECT COUNT(*) FROM intel_jobs WHERE job_type='financial_research'"
                ).fetchone()[0]
            ),
            0,
        )

    def test_timeout_falls_back_to_stale_and_permission_without_cache_has_no_numbers(self):
        self._insert_quote(
            price=98.0,
            observed_at=NOW - timedelta(hours=4),
            fetched_at=NOW - timedelta(hours=4),
            quality_status="normalized_stale",
        )
        self.router.error = "timeout"
        stale = self.orchestrator.execute_realtime_query(self._plan()).realtime_query
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["refresh"]["error_code"], "temporarily_unavailable")
        self.assertTrue(stale["numeric_claims_allowed"])
        self.assertIn("不能称为实时行情", format_realtime_query_answer(stale))

        other_router = _FixtureQuoteRouter(self.database.connection, error="permission")
        other_service = FinancialRealtimeQueryService(
            self.database,
            settings=SETTINGS,
            router=other_router,
            clock=lambda: NOW,
        )
        other = ChatRouteOrchestrator(
            clock=lambda: NOW,
            intent_classifier=self.classifier,
            target_resolver=self.resolver,
            realtime_query_service=other_service,
            financial_settings=SETTINGS,
        )
        apple = other.plan(_payload("Apple 现在股价", "permission-apple"))
        unavailable = other.execute_realtime_query(apple).realtime_query
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertEqual(unavailable["refresh"]["error_code"], "permission_denied")
        self.assertFalse(unavailable["answer_allowed"])
        self.assertFalse(unavailable["numeric_claims_allowed"])
        self.assertEqual(len(other_router.fetch_calls), 2)
        self.assertEqual(
            {item["candidate_provider_ids"] for item in other_router.fetch_calls},
            {("yahoo",), ("alpha_vantage",)},
        )
        self.assertTrue(
            all(not item["allow_fallback"] for item in other_router.fetch_calls)
        )
        self.assertIn("不会由通用模型补造", format_realtime_query_answer(unavailable))

    def test_no_eligible_hk_provider_reports_precise_preflight_error(self):
        router = _NoEligibleQuoteRouter(self.database.connection)
        service = FinancialRealtimeQueryService(
            self.database,
            settings=SETTINGS,
            router=router,
            clock=lambda: NOW,
        )
        orchestrator = ChatRouteOrchestrator(
            clock=lambda: NOW,
            intent_classifier=self.classifier,
            target_resolver=self.resolver,
            realtime_query_service=service,
            financial_settings=SETTINGS,
        )

        result = orchestrator.execute_realtime_query(
            orchestrator.plan(_payload("腾讯现在股价", "hk-no-eligible"))
        ).realtime_query

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(
            result["refresh"]["error_code"],
            "no_eligible_quote_provider",
        )
        self.assertNotEqual(result["refresh"]["error_code"], "permission_denied")
        self.assertEqual(router.fetch_calls, [])
        self.assertEqual(
            {item["provider_id"] for item in result["refresh"]["provider_eligibility"]},
            {"alpha_vantage", "easyquotation", "yahoo"},
        )

    def test_invalid_alpha_symbol_is_negatively_cached_without_blocking_fallback(self):
        current_time = [NOW]
        router = _AlphaInvalidQuoteRouter(self.database.connection)
        settings = {
            **SETTINGS,
            "FINANCIAL_INVALID_SYMBOL_NEGATIVE_CACHE_SECONDS": 1,
        }
        service = FinancialRealtimeQueryService(
            self.database,
            settings=settings,
            router=router,
            clock=lambda: current_time[0],
        )
        orchestrator = ChatRouteOrchestrator(
            clock=lambda: current_time[0],
            intent_classifier=self.classifier,
            target_resolver=self.resolver,
            realtime_query_service=service,
            financial_settings=settings,
        )

        first = orchestrator.execute_realtime_query(
            orchestrator.plan(_payload("腾讯现在股价", "alpha-negative-first"))
        ).realtime_query
        self.assertEqual(first["status"], "ready")
        self.assertEqual(
            {item["provider_id"] for item in first["refresh"]["provider_errors"]},
            {"alpha_vantage"},
        )
        self.assertEqual(
            {item["provider_id"] for item in first["evidence"]},
            {"easyquotation"},
        )
        calls_after_first = len(router.fetch_calls)

        second = orchestrator.execute_realtime_query(
            orchestrator.plan(_payload("腾讯现在股价", "alpha-negative-second"))
        ).realtime_query
        self.assertEqual(len(router.fetch_calls), calls_after_first)
        alpha_eligibility = next(
            item
            for item in second["refresh"]["provider_eligibility"]
            if item["provider_id"] == "alpha_vantage"
        )
        self.assertFalse(alpha_eligibility["eligible"])
        self.assertEqual(
            alpha_eligibility["reason"], "invalid_symbol_negative_cache"
        )
        self.assertTrue(alpha_eligibility["retry_after_utc"])

        current_time[0] = NOW + timedelta(seconds=2)
        orchestrator.execute_realtime_query(
            orchestrator.plan(_payload("腾讯现在股价", "alpha-negative-expired"))
        )
        self.assertEqual(len(router.fetch_calls), calls_after_first + 1)
        self.assertEqual(
            router.fetch_calls[-1]["candidate_provider_ids"],
            ("alpha_vantage",),
        )

    def test_current_provider_conflict_refuses_single_price(self):
        self._insert_quote(provider_id="akshare_cn", price=100.0)
        self._insert_quote(provider_id="tushare_cn", price=110.0)
        conflict = self.orchestrator.execute_realtime_query(self._plan()).realtime_query
        self.assertEqual(conflict["status"], "conflict")
        self.assertTrue(conflict["answer_allowed"])
        self.assertFalse(conflict["numeric_claims_allowed"])
        self.assertEqual(len(conflict["evidence"]), 2)
        self.assertEqual(self.router.fetch_calls, [])
        answer = format_realtime_query_answer(conflict)
        self.assertIn("存在实质冲突", answer)
        self.assertIn("不选择单一实时价格", answer)
        for field in ("observed_at=", "fetched_at=", "market_status=", "来源="):
            self.assertIn(field, answer)

    def test_closed_market_uses_latest_as_stale_without_refresh(self):
        closed_now = datetime(2026, 8, 1, 2, 0, tzinfo=UTC)
        snapshot_id = self._insert_quote(
            price=105.0,
            observed_at=NOW - timedelta(minutes=1),
            fetched_at=NOW - timedelta(minutes=1),
        )
        closed_service = FinancialRealtimeQueryService(
            self.database,
            settings=SETTINGS,
            router=self.router,
            clock=lambda: closed_now,
        )
        closed_orchestrator = ChatRouteOrchestrator(
            clock=lambda: closed_now,
            intent_classifier=self.classifier,
            target_resolver=self.resolver,
            realtime_query_service=closed_service,
            financial_settings=SETTINGS,
        )
        result = closed_orchestrator.execute_realtime_query(
            closed_orchestrator.plan(_payload("腾讯现在股价", "weekend"))
        ).realtime_query
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["market_session"]["market_session_state"], "closed")
        self.assertEqual(result["evidence"][0]["snapshot_id"], snapshot_id)
        self.assertEqual(result["refresh"]["reason"], "market_not_active")
        self.assertEqual(self.router.fetch_calls, [])

    def test_first_sse_status_precedes_slow_provider_and_model_is_never_called(self):
        slow_router = _FixtureQuoteRouter(
            self.database.connection,
            delay_seconds=1.05,
        )
        slow_service = FinancialRealtimeQueryService(
            self.database,
            settings=SETTINGS,
            router=slow_router,
            clock=lambda: NOW,
        )
        orchestrator = ChatRouteOrchestrator(
            clock=lambda: NOW,
            store=self.store,
            intent_classifier=self.classifier,
            target_resolver=self.resolver,
            realtime_query_service=slow_service,
            financial_settings=SETTINGS,
        )
        app = Flask(__name__)
        app.config.update(TESTING=True)
        app.register_blueprint(chat_api.chat_bp)
        client = app.test_client()
        started = time.monotonic()
        with patch.object(chat_api, "chat_route_orchestrator", orchestrator), patch.object(
            chat_api, "_stream_openai"
        ) as model, patch.object(chat_api, "_web_search") as web, patch.object(
            chat_api, "_load_config"
        ) as config_loader:
            response = client.post(
                "/api/chat/send",
                json=_payload("腾讯今天股价多少", "slow-sse"),
                buffered=False,
            )
            iterator = iter(response.response)
            first = _decode(next(iterator))
            first_elapsed = time.monotonic() - started
            remainder = [_decode(block) for block in iterator]
        self.assertLess(first_elapsed, 1.0)
        self.assertEqual(first["type"], "status")
        self.assertEqual([item["type"] for item in remainder], ["chunk", "done"])
        content = remainder[0]["content"]
        self.assertIn("observed_at=", content)
        self.assertIn("fetched_at=", content)
        self.assertIn("market_status=", content)
        self.assertIn("来源=", content)
        model.assert_not_called()
        web.assert_not_called()
        config_loader.assert_not_called()
        row = self.database.connection.execute(
            "SELECT route_status, route_destination FROM chat_financial_routes WHERE session_id='slow-sse'"
        ).fetchone()
        self.assertEqual(tuple(row), ("realtime_query_ready", "financial_realtime_snapshot"))

    def test_completed_realtime_route_keeps_same_session_target_context(self):
        first_payload = _payload("腾讯今天股价多少", "follow-realtime")
        first = self.orchestrator.execute_realtime_query(
            self.orchestrator.plan(first_payload)
        )
        self.orchestrator.persist(first, first_payload)
        follow = self.orchestrator.plan(_payload("那现在呢？", "follow-realtime"))
        self.assertTrue(follow.target_resolution["context_inherited"])
        self.assertEqual(
            follow.target_resolution["targets"][0]["canonical_symbol"], "0700.HK"
        )
        self.assertEqual(follow.realtime_query["status"], "planned")


if __name__ == "__main__":
    unittest.main()

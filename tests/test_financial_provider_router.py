#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sqlite3
import unittest
from datetime import datetime, timezone

from financial_config import financial_capabilities
from financial_instruments import InstrumentRegistry
from financial_provider_contract import (
    AdjustmentMode,
    FinancialDataKind,
    FinancialDataProvider,
    FinancialDataRecord,
    FinancialDataRequest,
    FinancialProviderResponse,
    FreshnessState,
    MarketStatus,
    PermissionDeniedError,
    raw_response_hash,
)
from financial_provider_router import (
    FinancialProviderRouter,
    HARD_DISABLED_IDS,
    provider_policy_summary,
)
from financial_schema import ensure_financial_tables


NOW = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)


class DummyProvider(FinancialDataProvider):
    def __init__(self, provider_id):
        self._provider_id = provider_id

    @property
    def provider_id(self):
        return self._provider_id

    @property
    def license_profile(self):
        return f"{self.provider_id}_test_license"

    @property
    def capabilities(self):
        return (FinancialDataKind.QUOTE,)

    def fetch(self, request):
        record = FinancialDataRecord(
            instrument_id=request.instrument_id,
            metric=request.metric,
            value=123.0,
            unit="price",
            currency="USD",
            market_status=MarketStatus.UNKNOWN,
            observed_at=NOW,
            fetched_at=NOW,
            timezone="UTC",
            freshness_state=FreshnessState.UNKNOWN,
            requested_as_of=request.requested_as_of,
            raw_response_hash=raw_response_hash({"value": 123.0}),
            normalized_payload={"value": 123.0},
            adjustment=AdjustmentMode.RAW,
        )
        return FinancialProviderResponse(
            provider_id=self.provider_id,
            endpoint=request.endpoint,
            license_profile=self.license_profile,
            request_id=request.request_id,
            data_kind=request.data_kind,
            records=(record,),
        )

    def persist_response(self, response):
        self.persisted_response = response
        return (41,)


class FinancialProviderRouterTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        ensure_financial_tables(self.connection.cursor())
        self.registry = InstrumentRegistry(self.connection)
        self.registry.load_controlled_seed()
        self.instrument = self.registry.get_by_canonical_symbol("AAPL.US")
        self.request = FinancialDataRequest(
            request_id="router-1",
            endpoint="quote",
            instrument_id=str(self.instrument.instrument_id),
            metric="last_price",
            data_kind=FinancialDataKind.QUOTE,
            requested_as_of=NOW,
            preferred_provider_id="yahoo",
        )

    def tearDown(self):
        self.connection.close()

    def test_all_profiles_are_opt_in_and_have_license_health_budget_metadata(self):
        summary = provider_policy_summary()
        self.assertEqual(set(summary["hard_disabled"]), {"reddit", "stocktwits"})
        for provider_id, profile in summary["providers"].items():
            self.assertFalse(profile["default_enabled"], provider_id)
            self.assertTrue(profile["license_profile"], provider_id)
            self.assertGreaterEqual(profile["daily_call_budget"], 0, provider_id)
            self.assertGreaterEqual(profile["fallback_rank"], 0, provider_id)
            self.assertTrue(profile["capabilities"], provider_id)

    def test_secret_gates_and_parent_gate_cover_auxiliary_providers(self):
        settings = {
            "FINANCIAL_INTELLIGENCE_ENABLED": True,
            "YAHOO_FINANCE_ENABLED": True,
            "ALPHA_VANTAGE_ENABLED": True,
            "ALPHA_VANTAGE_API_KEY": "",
            "FRED_ENABLED": True,
            "FRED_API_KEY": "",
            "POLYMARKET_ENABLED": True,
            "EASYQUOTATION_ENABLED": True,
            "OFFICIAL_FINANCIAL_EVIDENCE_ENABLED": True,
        }
        state = financial_capabilities(settings)
        self.assertTrue(state["effective"]["yahoo"])
        self.assertFalse(state["effective"]["alpha_vantage"])
        self.assertFalse(state["effective"]["fred"])
        self.assertTrue(state["effective"]["polymarket"])
        settings["FINANCIAL_INTELLIGENCE_ENABLED"] = False
        self.assertFalse(any(financial_capabilities(settings)["effective"].values()))

    def test_disabled_preferred_provider_is_not_constructed_and_fallback_is_explicit(self):
        calls = {"yahoo": 0, "alpha": 0}

        def yahoo_factory():
            calls["yahoo"] += 1
            return DummyProvider("yahoo")

        def alpha_factory():
            calls["alpha"] += 1
            return DummyProvider("alpha_vantage")

        router = FinancialProviderRouter(
            settings={
                "FINANCIAL_INTELLIGENCE_ENABLED": True,
                "YAHOO_FINANCE_ENABLED": False,
                "ALPHA_VANTAGE_ENABLED": True,
                "ALPHA_VANTAGE_API_KEY": "configured-for-test",
            },
            factories={"yahoo": yahoo_factory, "alpha_vantage": alpha_factory},
        )
        response = router.fetch(
            self.request,
            candidate_provider_ids=("yahoo", "alpha_vantage"),
            allow_fallback=True,
        )
        self.assertEqual(calls, {"yahoo": 0, "alpha": 1})
        self.assertEqual(response.provider_id, "alpha_vantage")
        self.assertTrue(response.degradation.degraded)
        self.assertEqual(response.degradation.requested_provider_id, "yahoo")
        self.assertEqual(
            response.degradation.reason, "preferred_provider_disabled_or_unavailable"
        )

    def test_no_fallback_and_hard_disabled_sources_never_call_factories(self):
        factory_calls = []
        router = FinancialProviderRouter(
            settings={
                "FINANCIAL_INTELLIGENCE_ENABLED": True,
                "YAHOO_FINANCE_ENABLED": False,
            },
            factories={"yahoo": lambda: factory_calls.append("called")},
        )
        with self.assertRaises(PermissionDeniedError):
            router.fetch(
                self.request,
                candidate_provider_ids=("yahoo",),
                allow_fallback=False,
            )
        self.assertEqual(factory_calls, [])
        self.assertIn("reddit", HARD_DISABLED_IDS)
        with self.assertRaises(ValueError):
            FinancialProviderRouter(
                settings={}, factories={"reddit": lambda: object()}
            )

    def test_fetch_and_persist_uses_only_the_selected_provider_boundary(self):
        selected = DummyProvider("yahoo")
        unused = DummyProvider("alpha_vantage")
        router = FinancialProviderRouter(
            settings={
                "FINANCIAL_INTELLIGENCE_ENABLED": True,
                "YAHOO_FINANCE_ENABLED": True,
                "ALPHA_VANTAGE_ENABLED": True,
                "ALPHA_VANTAGE_API_KEY": "configured-for-test",
            },
            factories={"yahoo": lambda: selected, "alpha_vantage": lambda: unused},
        )

        response, snapshot_ids = router.fetch_and_persist(
            self.request,
            candidate_provider_ids=("yahoo", "alpha_vantage"),
            allow_fallback=True,
        )

        self.assertEqual(response.provider_id, "yahoo")
        self.assertEqual(snapshot_ids, (41,))
        self.assertIs(selected.persisted_response, response)
        self.assertFalse(hasattr(unused, "persisted_response"))

    def test_fetch_many_calls_each_provider_without_cross_provider_fallback(self):
        yahoo = DummyProvider("yahoo")
        alpha = DummyProvider("alpha_vantage")
        router = FinancialProviderRouter(
            settings={
                "FINANCIAL_INTELLIGENCE_ENABLED": True,
                "YAHOO_FINANCE_ENABLED": True,
                "ALPHA_VANTAGE_ENABLED": True,
                "ALPHA_VANTAGE_API_KEY": "configured-for-test",
            },
            factories={
                "yahoo": lambda: yahoo,
                "alpha_vantage": lambda: alpha,
            },
        )

        responses, errors = router.fetch_many(
            self.request,
            candidate_provider_ids=("yahoo", "alpha_vantage"),
        )

        self.assertEqual(
            tuple(response.provider_id for response in responses),
            ("yahoo", "alpha_vantage"),
        )
        self.assertEqual(errors, ())
        self.assertTrue(all(not response.degradation.degraded for response in responses))
        self.assertTrue(all(response.request_id.endswith(response.provider_id) for response in responses))
        self.assertFalse(hasattr(yahoo, "persisted_response"))
        self.assertFalse(hasattr(alpha, "persisted_response"))


if __name__ == "__main__":
    unittest.main()

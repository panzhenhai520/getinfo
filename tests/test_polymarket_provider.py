#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from financial_instruments import InstrumentRegistry
from financial_provider_contract import (
    FinancialDataKind,
    FinancialDataRequest,
    InvalidSymbolError,
    PermissionDeniedError,
)
from financial_providers.polymarket import PolymarketProvider
from financial_schema import ensure_financial_tables


NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


class FakeResult:
    def __init__(self, payload):
        self.text = json.dumps(payload)


class FakeHTTP:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResult(self.payloads.pop(0))


class PolymarketProviderTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.execute("PRAGMA foreign_keys=ON")
        ensure_financial_tables(self.connection.cursor())
        self.registry = InstrumentRegistry(self.connection)
        self.registry.load_controlled_seed()
        self.settings = {
            "FINANCIAL_INTELLIGENCE_ENABLED": True,
            "POLYMARKET_ENABLED": True,
            "FINANCIAL_PROVIDER_DAILY_CALL_BUDGET": 100,
        }

    def tearDown(self):
        self.connection.close()

    def _request(self, endpoint, parameters, *, requested_at=NOW):
        instrument = self.registry.get_by_canonical_symbol(
            "MARKET-EXPECTATION.POLY"
        )
        return FinancialDataRequest(
            request_id=f"poly-{endpoint}",
            endpoint=endpoint,
            instrument_id=str(instrument.instrument_id),
            metric="implied_probability",
            data_kind=FinancialDataKind.MACRO,
            requested_as_of=requested_at,
            preferred_provider_id="polymarket",
            parameters=parameters,
        )

    def test_market_price_is_always_expectation_not_fact_and_read_only(self):
        http = FakeHTTP(
            [
                {
                    "id": "123",
                    "conditionId": "condition-1",
                    "question": "Will a policy rate be cut?",
                    "outcomes": '["Yes", "No"]',
                    "outcomePrices": '["0.62", "0.38"]',
                    "clobTokenIds": '["111", "222"]',
                    "active": True,
                    "closed": False,
                    "liquidityNum": 10000,
                    "volumeNum": 50000,
                    "updatedAt": "2026-07-31T11:55:00Z",
                    "slug": "policy-rate-cut",
                }
            ]
        )
        provider = PolymarketProvider(
            instrument_registry=self.registry,
            settings=self.settings,
            http_client=http,
            clock=lambda: NOW,
            connection=self.connection,
        )
        response = provider.fetch_and_persist(
            self._request("market_expectation", {"market_id": "123", "outcome": "Yes"})
        )
        record = response.records[0]
        self.assertEqual(record.value, 0.62)
        self.assertEqual(record.unit, "probability")
        self.assertIn("expectation_not_fact", record.quality_flags)
        self.assertEqual(record.normalized_payload["semantic_role"], "expectation_not_fact")
        self.assertFalse(record.lineage["trading_endpoints_used"])
        self.assertEqual(urlsplit(http.calls[0][0]).hostname, "gamma-api.polymarket.com")
        self.assertTrue(urlsplit(http.calls[0][0]).path.startswith("/markets/"))

    def test_current_market_newer_than_request_is_rejected_as_lookahead(self):
        http = FakeHTTP(
            [
                {
                    "id": "123",
                    "question": "Future state",
                    "outcomes": ["Yes", "No"],
                    "outcomePrices": [0.7, 0.3],
                    "updatedAt": "2026-07-31T11:55:00Z",
                }
            ]
        )
        provider = PolymarketProvider(
            instrument_registry=self.registry,
            settings=self.settings,
            http_client=http,
            clock=lambda: NOW,
        )
        with self.assertRaises(InvalidSymbolError) as rejected:
            provider.fetch_validated(
                self._request(
                    "market_expectation",
                    {"market_id": "123"},
                    requested_at=datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc),
                )
            )
        self.assertEqual(
            rejected.exception.details["failure"], "lookahead_current_market_rejected"
        )

    def test_history_caps_end_timestamp_and_filters_future_points(self):
        cutoff = int(NOW.timestamp())
        http = FakeHTTP(
            [
                {
                    "history": [
                        {"t": cutoff - 60, "p": 0.55},
                        {"t": cutoff + 60, "p": 0.99},
                    ]
                }
            ]
        )
        provider = PolymarketProvider(
            instrument_registry=self.registry,
            settings=self.settings,
            http_client=http,
            clock=lambda: NOW,
        )
        response = provider.fetch_validated(
            self._request(
                "expectation_history",
                {"token_id": "123456789", "end_ts": cutoff + 999, "interval": "1h"},
            )
        )
        self.assertEqual([item.value for item in response.records], [0.55])
        query = parse_qs(urlsplit(http.calls[0][0]).query)
        self.assertEqual(query["endTs"], [str(cutoff)])
        self.assertTrue(
            all("expectation_not_fact" in item.quality_flags for item in response.records)
        )

    def test_disabled_or_unsafe_identifier_never_reaches_http(self):
        http = FakeHTTP([])
        disabled = PolymarketProvider(
            instrument_registry=self.registry,
            settings={
                "FINANCIAL_INTELLIGENCE_ENABLED": True,
                "POLYMARKET_ENABLED": False,
            },
            http_client=http,
            clock=lambda: NOW,
        )
        with self.assertRaises(PermissionDeniedError):
            disabled.fetch_validated(
                self._request("market_expectation", {"market_id": "123"})
            )
        enabled = PolymarketProvider(
            instrument_registry=self.registry,
            settings=self.settings,
            http_client=http,
            clock=lambda: NOW,
        )
        with self.assertRaises(InvalidSymbolError):
            enabled.fetch_validated(
                self._request("market_expectation", {"market_id": "../orders"})
            )
        self.assertEqual(http.calls, [])

    def test_health_probe_uses_only_public_market_listing(self):
        http = FakeHTTP([[{"id": "123"}]])
        provider = PolymarketProvider(
            instrument_registry=self.registry,
            settings=self.settings,
            http_client=http,
            clock=lambda: NOW,
        )
        result = provider.health_probe(request_id="poly-health", requested_at=NOW)
        self.assertEqual(result["status"], "healthy_public_read_only")
        self.assertTrue(result["read_only"])
        url = http.calls[0][0]
        self.assertEqual(urlsplit(url).path, "/markets")
        self.assertNotIn("order", url.casefold())
        self.assertNotIn("trade", url.casefold())


if __name__ == "__main__":
    unittest.main()

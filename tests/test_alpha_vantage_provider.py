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
    PermissionDeniedError,
    RateLimitedError,
)
from financial_providers.alpha_vantage import AlphaVantageProvider
from financial_schema import ensure_financial_tables


NOW = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
SECRET = "alpha-secret-never-persist"


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


class AlphaVantageProviderTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.execute("PRAGMA foreign_keys=ON")
        ensure_financial_tables(self.connection.cursor())
        self.registry = InstrumentRegistry(self.connection)
        self.registry.load_controlled_seed()
        self.settings = {
            "FINANCIAL_INTELLIGENCE_ENABLED": True,
            "ALPHA_VANTAGE_ENABLED": True,
            "ALPHA_VANTAGE_API_KEY": SECRET,
            "ALPHA_VANTAGE_REALTIME_ENTITLED": False,
            "FINANCIAL_PROVIDER_DAILY_CALL_BUDGET": 100,
        }

    def tearDown(self):
        self.connection.close()

    def _request(self, *, endpoint="quote", parameters=None):
        instrument = self.registry.get_by_canonical_symbol("AAPL.US")
        return FinancialDataRequest(
            request_id=f"alpha-{endpoint}",
            endpoint=endpoint,
            instrument_id=str(instrument.instrument_id),
            metric="last_price" if endpoint == "quote" else "close",
            data_kind=(
                FinancialDataKind.QUOTE if endpoint == "quote" else FinancialDataKind.BAR
            ),
            requested_as_of=NOW,
            preferred_provider_id="alpha_vantage",
            parameters=parameters or {},
        )

    def test_free_quote_is_end_of_day_not_realtime_and_secret_is_not_persisted(self):
        http = FakeHTTP(
            [
                {
                    "Global Quote": {
                        "01. symbol": "AAPL",
                        "02. open": "210.0",
                        "03. high": "215.0",
                        "04. low": "208.0",
                        "05. price": "214.5",
                        "06. volume": "12345",
                        "07. latest trading day": "2026-07-31",
                        "08. previous close": "209.0",
                        "09. change": "5.5",
                        "10. change percent": "2.63%",
                    }
                }
            ]
        )
        provider = AlphaVantageProvider(
            instrument_registry=self.registry,
            settings=self.settings,
            http_client=http,
            clock=lambda: NOW,
            connection=self.connection,
        )
        response = provider.fetch_and_persist(self._request())
        record = response.records[0]
        self.assertEqual(record.value, 214.5)
        self.assertEqual(record.freshness_state.value, "stale")
        self.assertIn("free_tier_end_of_day_default", record.quality_flags)
        self.assertNotIn(SECRET, record.source_url)
        stored = self.connection.execute(
            "SELECT metadata_json FROM financial_provider_profiles "
            "WHERE provider_key='alpha_vantage'"
        ).fetchone()[0]
        self.assertNotIn(SECRET, stored)
        self.assertEqual(json.loads(stored)["daily_call_budget"], 25)

    def test_daily_bars_apply_raw_and_requested_as_of_cutoff(self):
        http = FakeHTTP(
            [
                {
                    "Time Series (Daily)": {
                        "2026-07-30": {
                            "1. open": "200",
                            "2. high": "205",
                            "3. low": "199",
                            "4. close": "204",
                            "5. volume": "1000",
                        },
                        "2026-07-31": {
                            "1. open": "205",
                            "2. high": "216",
                            "3. low": "204",
                            "4. close": "214.5",
                            "5. volume": "2000",
                        },
                        "2026-08-03": {
                            "1. open": "999",
                            "2. high": "999",
                            "3. low": "999",
                            "4. close": "999",
                            "5. volume": "1",
                        },
                    }
                }
            ]
        )
        provider = AlphaVantageProvider(
            instrument_registry=self.registry,
            settings=self.settings,
            http_client=http,
            clock=lambda: NOW,
        )
        response = provider.fetch_validated(self._request(endpoint="bars"))
        self.assertEqual([item.value for item in response.records], [204.0, 214.5])
        self.assertTrue(all(item.adjustment.value == "raw" for item in response.records))
        self.assertTrue(
            all(item.lineage["requested_as_of_cutoff_applied"] for item in response.records)
        )

    def test_missing_key_and_unapproved_realtime_entitlement_never_call_http(self):
        http = FakeHTTP([])
        missing = AlphaVantageProvider(
            instrument_registry=self.registry,
            settings={
                "FINANCIAL_INTELLIGENCE_ENABLED": True,
                "ALPHA_VANTAGE_ENABLED": True,
                "ALPHA_VANTAGE_API_KEY": "",
            },
            http_client=http,
            clock=lambda: NOW,
        )
        with self.assertRaises(PermissionDeniedError):
            missing.fetch_validated(self._request())

        provider = AlphaVantageProvider(
            instrument_registry=self.registry,
            settings=self.settings,
            http_client=http,
            clock=lambda: NOW,
        )
        with self.assertRaises(PermissionDeniedError) as denied:
            provider.fetch_validated(
                self._request(parameters={"entitlement": "realtime"})
            )
        self.assertEqual(
            denied.exception.details["gate_reason"],
            "alpha_vantage_realtime_entitlement_missing",
        )
        self.assertEqual(http.calls, [])

    def test_configured_quote_entitlement_is_transmitted_only_when_acknowledged(self):
        payload = {
            "Global Quote": {
                "01. symbol": "AAPL",
                "02. open": "210.0",
                "03. high": "215.0",
                "04. low": "208.0",
                "05. price": "214.5",
                "06. volume": "12345",
                "07. latest trading day": "2026-07-31",
                "08. previous close": "209.0",
                "09. change": "5.5",
                "10. change percent": "2.63%",
            }
        }
        denied_http = FakeHTTP([])
        denied = AlphaVantageProvider(
            instrument_registry=self.registry,
            settings={
                **self.settings,
                "ALPHA_VANTAGE_QUOTE_ENTITLEMENT": "delayed",
            },
            http_client=denied_http,
            clock=lambda: NOW,
        )
        with self.assertRaises(PermissionDeniedError) as rejected:
            denied.fetch_validated(self._request())
        self.assertEqual(
            rejected.exception.details["gate_reason"],
            "alpha_vantage_realtime_entitlement_missing",
        )
        self.assertEqual(denied_http.calls, [])

        entitled_http = FakeHTTP([payload])
        entitled = AlphaVantageProvider(
            instrument_registry=self.registry,
            settings={
                **self.settings,
                "ALPHA_VANTAGE_QUOTE_ENTITLEMENT": "delayed",
                "ALPHA_VANTAGE_REALTIME_ENTITLED": True,
            },
            http_client=entitled_http,
            clock=lambda: NOW,
        )
        response = entitled.fetch_validated(self._request())
        query = parse_qs(urlsplit(entitled_http.calls[0][0]).query)
        self.assertEqual(query["entitlement"], ["delayed"])
        self.assertIn("delayed_entitlement_requested", response.records[0].quality_flags)

    def test_configured_quote_entitlement_never_leaks_into_daily_request(self):
        http = FakeHTTP(
            [
                {
                    "Time Series (Daily)": {
                        "2026-07-31": {
                            "1. open": "205",
                            "2. high": "216",
                            "3. low": "204",
                            "4. close": "214.5",
                            "5. volume": "2000",
                        }
                    }
                }
            ]
        )
        provider = AlphaVantageProvider(
            instrument_registry=self.registry,
            settings={
                **self.settings,
                "ALPHA_VANTAGE_QUOTE_ENTITLEMENT": "realtime",
                "ALPHA_VANTAGE_REALTIME_ENTITLED": True,
            },
            http_client=http,
            clock=lambda: NOW,
        )
        provider.fetch_validated(self._request(endpoint="bars"))
        query = parse_qs(urlsplit(http.calls[0][0]).query)
        self.assertNotIn("entitlement", query)

    def test_provider_rate_and_auth_messages_are_classified_without_secret(self):
        limited_http = FakeHTTP([{"Note": "frequency limit includes no secret"}])
        limited = AlphaVantageProvider(
            instrument_registry=self.registry,
            settings=self.settings,
            http_client=limited_http,
            clock=lambda: NOW,
        )
        with self.assertRaises(RateLimitedError):
            limited.fetch_validated(self._request())

        auth_http = FakeHTTP([{"Information": f"Invalid API key {SECRET}"}])
        auth = AlphaVantageProvider(
            instrument_registry=self.registry,
            settings=self.settings,
            http_client=auth_http,
            clock=lambda: NOW,
        )
        with self.assertRaises(PermissionDeniedError) as denied:
            auth.fetch_validated(self._request())
        self.assertNotIn(SECRET, json.dumps(denied.exception.to_dict()))


if __name__ == "__main__":
    unittest.main()

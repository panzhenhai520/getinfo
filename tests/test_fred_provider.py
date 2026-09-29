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
    RateLimitedError,
)
from financial_providers.fred import FREDProvider
from financial_schema import ensure_financial_tables


NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
SECRET = "fred-secret-never-persist"


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


class FREDProviderTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.execute("PRAGMA foreign_keys=ON")
        ensure_financial_tables(self.connection.cursor())
        self.registry = InstrumentRegistry(self.connection)
        self.registry.load_controlled_seed()
        self.settings = {
            "FINANCIAL_INTELLIGENCE_ENABLED": True,
            "FRED_ENABLED": True,
            "FRED_API_KEY": SECRET,
            "FINANCIAL_PROVIDER_DAILY_CALL_BUDGET": 1000,
        }

    def tearDown(self):
        self.connection.close()

    def _request(self, *, endpoint="observations", parameters=None, requested_as_of=NOW):
        instrument = self.registry.get_by_canonical_symbol("CPIAUCSL.FRED")
        return FinancialDataRequest(
            request_id=f"fred-{endpoint}",
            endpoint=endpoint,
            instrument_id=str(instrument.instrument_id),
            metric="value" if endpoint == "observations" else "vintage_date",
            data_kind=FinancialDataKind.MACRO,
            requested_as_of=requested_as_of,
            preferred_provider_id="fred",
            parameters=parameters or {},
        )

    def test_observations_lock_alfred_vintage_and_filter_future_or_later_revisions(self):
        http = FakeHTTP(
            [
                {
                    "observations": [
                        {
                            "realtime_start": "2026-07-15",
                            "realtime_end": "2026-08-10",
                            "date": "2026-06-01",
                            "value": "321.5",
                        },
                        {
                            "realtime_start": "2026-08-02",
                            "realtime_end": "2026-08-10",
                            "date": "2026-06-01",
                            "value": "999.0",
                        },
                        {
                            "realtime_start": "2026-07-15",
                            "realtime_end": "2026-08-10",
                            "date": "2026-08-01",
                            "value": "888.0",
                        },
                        {
                            "realtime_start": "2026-07-15",
                            "realtime_end": "2026-08-10",
                            "date": "2026-07-01",
                            "value": ".",
                        },
                    ]
                }
            ]
        )
        provider = FREDProvider(
            instrument_registry=self.registry,
            settings=self.settings,
            http_client=http,
            clock=lambda: NOW,
            connection=self.connection,
        )
        response = provider.fetch_and_persist(self._request())
        self.assertEqual([item.value for item in response.records], [321.5])
        record = response.records[0]
        self.assertIn("alfred_vintage_locked", record.quality_flags)
        self.assertEqual(record.lineage["vintage_date"], "2026-07-31")
        query = parse_qs(urlsplit(http.calls[0][0]).query)
        self.assertEqual(query["realtime_start"], ["2026-07-31"])
        self.assertEqual(query["realtime_end"], ["2026-07-31"])
        stored = self.connection.execute(
            "SELECT metadata_json FROM financial_provider_profiles WHERE provider_key='fred'"
        ).fetchone()[0]
        snapshots = self.connection.execute(
            "SELECT payload_json, source_url FROM financial_data_snapshots"
        ).fetchall()
        self.assertNotIn(SECRET, stored)
        self.assertTrue(all(SECRET not in str(item) for row in snapshots for item in row))

    def test_explicit_future_vintage_is_rejected_before_http(self):
        http = FakeHTTP([])
        provider = FREDProvider(
            instrument_registry=self.registry,
            settings=self.settings,
            http_client=http,
            clock=lambda: NOW,
        )
        with self.assertRaises(InvalidSymbolError) as rejected:
            provider.fetch_validated(
                self._request(parameters={"vintage_date": "2026-08-01"})
            )
        self.assertEqual(
            rejected.exception.details["failure"], "lookahead_vintage_rejected"
        )
        self.assertEqual(http.calls, [])

    def test_vintage_cap_uses_fred_business_date_at_utc_midnight_boundary(self):
        boundary = datetime(2026, 8, 3, 2, 51, tzinfo=timezone.utc)
        http = FakeHTTP(
            [{
                "observations": [{
                    "realtime_start": "2026-08-02",
                    "realtime_end": "2026-08-02",
                    "date": "2026-08-01",
                    "value": "1.5",
                }]
            }]
        )
        provider = FREDProvider(
            instrument_registry=self.registry,
            settings=self.settings,
            http_client=http,
            clock=lambda: boundary,
        )

        response = provider.fetch_validated(
            self._request(requested_as_of=boundary)
        )

        self.assertEqual(len(response.records), 1)
        self.assertEqual(response.records[0].lineage["vintage_date"], "2026-08-02")
        query = parse_qs(urlsplit(http.calls[0][0]).query)
        self.assertEqual(query["realtime_start"], ["2026-08-02"])
        self.assertEqual(query["realtime_end"], ["2026-08-02"])

    def test_vintage_calendar_filters_dates_after_request(self):
        http = FakeHTTP(
            [{"vintage_dates": ["2026-07-01", "2026-07-31", "2026-08-01"]}]
        )
        provider = FREDProvider(
            instrument_registry=self.registry,
            settings=self.settings,
            http_client=http,
            clock=lambda: NOW,
        )
        response = provider.fetch_validated(self._request(endpoint="vintages"))
        self.assertEqual(
            [item.value for item in response.records], ["2026-07-01", "2026-07-31"]
        )
        self.assertTrue(
            all("lookahead_filtered" in item.quality_flags for item in response.records)
        )

    def test_missing_key_rate_and_auth_errors_are_safe(self):
        http = FakeHTTP([])
        missing = FREDProvider(
            instrument_registry=self.registry,
            settings={
                "FINANCIAL_INTELLIGENCE_ENABLED": True,
                "FRED_ENABLED": True,
                "FRED_API_KEY": "",
            },
            http_client=http,
            clock=lambda: NOW,
        )
        with self.assertRaises(PermissionDeniedError):
            missing.fetch_validated(self._request())
        self.assertEqual(http.calls, [])

        limited = FREDProvider(
            instrument_registry=self.registry,
            settings=self.settings,
            http_client=FakeHTTP([{"error_code": 429, "error_message": SECRET}]),
            clock=lambda: NOW,
        )
        with self.assertRaises(RateLimitedError) as rate:
            limited.fetch_validated(self._request())
        self.assertNotIn(SECRET, json.dumps(rate.exception.to_dict()))

        denied = FREDProvider(
            instrument_registry=self.registry,
            settings=self.settings,
            http_client=FakeHTTP([{"error_code": 400, "error_message": SECRET}]),
            clock=lambda: NOW,
        )
        with self.assertRaises(PermissionDeniedError) as auth:
            denied.fetch_validated(self._request())
        self.assertNotIn(SECRET, json.dumps(auth.exception.to_dict()))


if __name__ == "__main__":
    unittest.main()

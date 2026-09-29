#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from financial_instruments import InstrumentRegistry
from financial_provider_contract import (
    FinancialDataKind,
    FinancialDataRequest,
    PermissionDeniedError,
    RateLimitedError,
)
from financial_providers.easyquotation import EasyQuotationProvider
from financial_providers.yahoo import YahooFinanceProvider
from financial_schema import ensure_financial_tables


NOW = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)


class FakeYahooSDK:
    __version__ = "1.5.1-test"

    def __init__(self):
        self.calls = []

    def download(self, symbol, **kwargs):
        self.calls.append((symbol, kwargs))
        return {
            "rows": [
                (
                    "2026-07-31T15:59:00+08:00",
                    {"Open": 540, "High": 552, "Low": 538, "Close": 550, "Volume": 10},
                ),
                (
                    "2026-07-31T16:01:00+08:00",
                    {"Open": 550, "High": 999, "Low": 550, "Close": 999, "Volume": 1},
                ),
            ]
        }


class FakeEasyClient:
    def __init__(self, source, calls):
        self.source = source
        self.calls = calls

    def real(self, symbols, **kwargs):
        self.calls.append((self.source, tuple(symbols), kwargs))
        symbol = symbols[0]
        if self.source == "hkquote":
            return {
                symbol: {
                    "price": "550.00",
                    "lastPrice": "545.00",
                    "openPrice": "548.00",
                    "high": "552.00",
                    "low": "540.00",
                    "amount": "123456",
                    "time": "2026/07/31 15:59:00",
                }
            }
        return {
            symbol: {
                "now": 12.3,
                "upstream_placeholder": float("nan"),
                "close": 12.0,
                "open": 12.1,
                "high": 12.5,
                "low": 11.9,
                "turnover": 1000,
                "date": "2026-07-31",
                "time": "23:59:59",
            }
        }


class FakeEasySDK:
    __version__ = "0.7.7-test"

    def __init__(self):
        self.calls = []

    def use(self, source):
        return FakeEasyClient(source, self.calls)


class YahooEasyQuotationProviderTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.execute("PRAGMA foreign_keys=ON")
        ensure_financial_tables(self.connection.cursor())
        self.registry = InstrumentRegistry(self.connection)
        self.registry.load_controlled_seed()
        self.yahoo_sdk = FakeYahooSDK()
        self.easy_sdk = FakeEasySDK()

    def tearDown(self):
        self.connection.close()

    def _request(self, symbol, provider, *, endpoint="quote", kind=FinancialDataKind.QUOTE):
        instrument = self.registry.get_by_canonical_symbol(symbol)
        return FinancialDataRequest(
            request_id=f"{provider}-{symbol}-{endpoint}",
            endpoint=endpoint,
            instrument_id=str(instrument.instrument_id),
            metric="last_price" if endpoint == "quote" else "close",
            data_kind=kind,
            requested_as_of=NOW,
            preferred_provider_id=provider,
        )

    def _ohlcv_request(self, symbol):
        instrument = self.registry.get_by_canonical_symbol(symbol)
        return FinancialDataRequest(
            request_id=f"yahoo-{symbol}-ohlcv",
            endpoint="bars",
            instrument_id=str(instrument.instrument_id),
            metric="ohlcv",
            data_kind=FinancialDataKind.BAR,
            requested_as_of=NOW,
            preferred_provider_id="yahoo",
            parameters={
                "start": "2026-07-01",
                "end": "2026-07-31",
                "interval": "1d",
                "adjustment": "raw",
            },
        )

    def test_yahoo_filters_future_rows_and_marks_research_semantics(self):
        provider = YahooFinanceProvider(
            instrument_registry=self.registry,
            sdk=self.yahoo_sdk,
            settings={
                "FINANCIAL_INTELLIGENCE_ENABLED": True,
                "YAHOO_FINANCE_ENABLED": True,
                "FINANCIAL_PROVIDER_DAILY_CALL_BUDGET": 100,
            },
            clock=lambda: NOW,
            connection=self.connection,
        )
        response = provider.fetch_and_persist(self._request("0700.HK", "yahoo"))
        self.assertEqual(response.records[0].value, 550.0)
        self.assertIn("personal_research_only", response.records[0].quality_flags)
        self.assertNotEqual(response.records[0].value, 999.0)
        self.assertEqual(response.records[0].adjustment.value, "raw")
        self.assertFalse(self.yahoo_sdk.calls[0][1]["auto_adjust"])
        profile = self.connection.execute(
            "SELECT is_enabled, metadata_json FROM financial_provider_profiles "
            "WHERE provider_key='yahoo'"
        ).fetchone()
        self.assertEqual(profile[0], 1)
        self.assertEqual(json.loads(profile[1])["daily_call_budget"], 100)

    def test_yahoo_a_share_is_visibly_research_fallback_and_zero_budget_never_calls_sdk(self):
        provider = YahooFinanceProvider(
            instrument_registry=self.registry,
            sdk=self.yahoo_sdk,
            settings={
                "FINANCIAL_INTELLIGENCE_ENABLED": True,
                "YAHOO_FINANCE_ENABLED": True,
                "FINANCIAL_PROVIDER_DAILY_CALL_BUDGET": 100,
            },
            clock=lambda: NOW + timedelta(seconds=1),
        )
        response = provider.fetch_validated(self._request("000001.SZ", "yahoo"))
        self.assertIn("a_share_research_fallback", response.records[0].quality_flags)

        blocked_sdk = FakeYahooSDK()
        blocked = YahooFinanceProvider(
            instrument_registry=self.registry,
            sdk=blocked_sdk,
            settings={
                "FINANCIAL_INTELLIGENCE_ENABLED": True,
                "YAHOO_FINANCE_ENABLED": True,
                "YAHOO_DAILY_CALL_BUDGET": 0,
                "FINANCIAL_PROVIDER_DAILY_CALL_BUDGET": 100,
            },
            clock=lambda: NOW,
        )
        with self.assertRaises(RateLimitedError):
            blocked.fetch_validated(self._request("0700.HK", "yahoo"))
        self.assertEqual(blocked_sdk.calls, [])

    def test_yahoo_ohlcv_contract_returns_one_bounded_series_snapshot(self):
        provider = YahooFinanceProvider(
            instrument_registry=self.registry,
            sdk=self.yahoo_sdk,
            settings={
                "FINANCIAL_INTELLIGENCE_ENABLED": True,
                "YAHOO_FINANCE_ENABLED": True,
                "FINANCIAL_PROVIDER_DAILY_CALL_BUDGET": 100,
            },
            clock=lambda: NOW,
            connection=self.connection,
        )

        response = provider.fetch_and_persist(self._ohlcv_request("0700.HK"))

        self.assertEqual(len(response.records), 1)
        record = response.records[0]
        self.assertEqual(record.metric, "ohlcv")
        self.assertEqual(record.unit, "ohlcv_series")
        self.assertEqual(len(record.value), 1)
        self.assertEqual(record.value[0]["close"], 550.0)
        self.assertEqual(record.normalized_payload["bars"], record.value)
        self.assertEqual(self.yahoo_sdk.calls[0][1]["end"], "2026-08-01")
        self.assertEqual(record.observed_at.isoformat(), "2026-07-31T07:59:00+00:00")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM financial_data_snapshots WHERE request_id=?",
                (response.request_id,),
            ).fetchone()[0],
            1,
        )

    def test_disabled_yahoo_and_easyquotation_never_call_sdks(self):
        yahoo = YahooFinanceProvider(
            instrument_registry=self.registry,
            sdk=self.yahoo_sdk,
            settings={"FINANCIAL_INTELLIGENCE_ENABLED": True, "YAHOO_FINANCE_ENABLED": False},
            clock=lambda: NOW,
        )
        with self.assertRaises(PermissionDeniedError):
            yahoo.fetch_validated(self._request("0700.HK", "yahoo"))
        easy = EasyQuotationProvider(
            instrument_registry=self.registry,
            sdk=self.easy_sdk,
            settings={"FINANCIAL_INTELLIGENCE_ENABLED": True, "EASYQUOTATION_ENABLED": False},
            clock=lambda: NOW,
        )
        with self.assertRaises(PermissionDeniedError):
            easy.fetch_validated(self._request("0700.HK", "easyquotation"))
        self.assertEqual(self.yahoo_sdk.calls, [])
        self.assertEqual(self.easy_sdk.calls, [])

    def test_easyquotation_hk_and_cn_are_explicit_unknown_freshness_fallbacks(self):
        provider = EasyQuotationProvider(
            instrument_registry=self.registry,
            sdk=self.easy_sdk,
            settings={
                "FINANCIAL_INTELLIGENCE_ENABLED": True,
                "EASYQUOTATION_ENABLED": True,
                "FINANCIAL_PROVIDER_DAILY_CALL_BUDGET": 100,
            },
            clock=lambda: NOW,
        )
        hk = provider.fetch_validated(self._request("0700.HK", "easyquotation"))
        cn = provider.fetch_validated(self._request("000001.SZ", "easyquotation"))
        self.assertEqual(hk.records[0].value, 550.0)
        self.assertEqual(cn.records[0].value, 12.3)
        self.assertEqual(cn.records[0].observed_at, NOW)
        self.assertEqual(hk.records[0].freshness_state.value, "unknown")
        self.assertIn("unofficial_lightweight_fallback", hk.records[0].quality_flags)
        self.assertEqual(self.easy_sdk.calls[0][0], "hkquote")
        self.assertEqual(self.easy_sdk.calls[1][0], "tencent")
        health = provider.health_probe(
            request_id="easy-health", requested_at=NOW
        )
        self.assertEqual(health["status"], "healthy_fallback_only")
        self.assertTrue(health["fallback_only"])


if __name__ == "__main__":
    unittest.main()

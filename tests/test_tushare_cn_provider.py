import json
import sqlite3
import time
import unittest
from datetime import datetime, timezone

from financial_instruments import InstrumentRegistry
from financial_provider_contract import (
    FinancialDataKind,
    FinancialDataRequest,
    FreshnessState,
    PermissionDeniedError,
    RateLimitedError,
    TemporarilyUnavailableError,
    UnsupportedAssetError,
)
from financial_providers.tushare_cn import TushareCNProvider
from financial_schema import ensure_financial_tables


REQUESTED_AT = datetime(2026, 7, 31, 2, 0, 0, tzinfo=timezone.utc)
FETCHED_AT = datetime(2026, 7, 31, 2, 0, 2, tzinfo=timezone.utc)
TEST_TOKEN = "unit-secret-token-never-persist"
ENABLED_SETTINGS = {
    "FINANCIAL_INTELLIGENCE_ENABLED": True,
    "TUSHARE_CN_ENABLED": True,
    "TUSHARE_TOKEN": TEST_TOKEN,
    "FINANCIAL_QUOTE_FRESHNESS_SECONDS": 300,
    "FINANCIAL_NEWS_FRESHNESS_SECONDS": 3600,
    "FINANCIAL_FUNDAMENTAL_FRESHNESS_SECONDS": 86400,
    "FINANCIAL_PROVIDER_TIMEOUT_SECONDS": 20,
}


class FakeTushareClient:
    _DataApi__http_url = "http://api.waditu.com/dataapi"

    def __init__(self):
        self.calls = []
        self.overrides = {}

    def _call(self, name, default, kwargs):
        self.calls.append((name, kwargs))
        value = self.overrides.get(name, default)
        if isinstance(value, Exception):
            raise value
        return value

    def stock_basic(self, **kwargs):
        return self._call(
            "stock_basic",
            [
                {
                    "ts_code": "000001.SZ",
                    "symbol": "000001",
                    "name": "平安银行",
                    "industry": "银行",
                    "market": "主板",
                    "exchange": "SZSE",
                    "list_status": "L",
                    "list_date": "19910403",
                    "secret_marker": "must-not-persist",
                }
            ],
            kwargs,
        )

    def index_basic(self, **kwargs):
        return self._call(
            "index_basic",
            [{"ts_code": "000001.SH", "name": "上证指数", "market": "SSE"}],
            kwargs,
        )

    def fund_basic(self, **kwargs):
        return self._call(
            "fund_basic",
            [
                {"ts_code": "510300.SH", "name": "沪深300ETF", "status": "L"},
                {"ts_code": "110020.OF", "name": "易方达300联接A", "status": "L"},
            ],
            kwargs,
        )

    def rt_min(self, **kwargs):
        return self._call(
            "rt_min",
            [
                {
                    "code": "000001.SZ",
                    "time": "2026-07-31 09:59:55",
                    "open": 10.3,
                    "close": 10.5,
                    "high": 10.6,
                    "low": 10.2,
                    "vol": 10000,
                    "amount": 105000,
                    "secret_marker": "must-not-persist",
                }
            ],
            kwargs,
        )

    def rt_etf_min(self, **kwargs):
        return self._call(
            "rt_etf_min",
            [
                {
                    "ts_code": "510300.SH",
                    "time": "2026-07-31 09:59:55",
                    "open": 4.18,
                    "close": 4.2,
                    "high": 4.22,
                    "low": 4.15,
                    "vol": 2000,
                    "amount": 8400,
                }
            ],
            kwargs,
        )

    def rt_idx_min(self, **kwargs):
        return self._call(
            "rt_idx_min",
            [
                {
                    "code": "000001.SH",
                    "time": "2026-07-31 09:59:55",
                    "open": 3590,
                    "close": 3600,
                    "high": 3610,
                    "low": 3580,
                    "vol": 1000,
                    "amount": 2000,
                }
            ],
            kwargs,
        )

    @staticmethod
    def _daily(symbol, close):
        return [
            {
                "ts_code": symbol,
                "trade_date": "20260730",
                "open": close - 0.2,
                "high": close + 0.1,
                "low": close - 0.3,
                "close": close,
                "pre_close": close - 0.1,
                "vol": 100,
                "amount": 1000,
            }
        ]

    def daily(self, **kwargs):
        return self._call("daily", self._daily("000001.SZ", 10.3), kwargs)

    def fund_daily(self, **kwargs):
        return self._call("fund_daily", self._daily("510300.SH", 4.2), kwargs)

    def index_daily(self, **kwargs):
        return self._call("index_daily", self._daily("000001.SH", 3600), kwargs)

    def index_weight(self, **kwargs):
        return self._call(
            "index_weight",
            [
                {
                    "index_code": "000300.SH",
                    "con_code": "000001.SZ",
                    "trade_date": "20260730",
                    "weight": 0.8,
                },
                {
                    "index_code": "000300.SH",
                    "con_code": "600001.SH",
                    "trade_date": "20260730",
                    "weight": 0.5,
                },
            ],
            kwargs,
        )

    def fund_nav(self, **kwargs):
        return self._call(
            "fund_nav",
            [
                {
                    "ts_code": "110020.OF",
                    "ann_date": "20260730",
                    "end_date": "20260730",
                    "unit_nav": 1.3,
                    "accum_nav": 2.1,
                }
            ],
            kwargs,
        )

    def fund_portfolio(self, **kwargs):
        return self._call(
            "fund_portfolio",
            [
                {
                    "ts_code": "110020.OF",
                    "ann_date": "20260720",
                    "end_date": "20260630",
                    "symbol": "600519.SH",
                    "mkv": 1800,
                    "amount": 1.2,
                    "stk_mkv_ratio": 5.1,
                }
            ],
            kwargs,
        )

    def fund_manager(self, **kwargs):
        return self._call(
            "fund_manager",
            [
                {
                    "ts_code": "110020.OF",
                    "ann_date": "20260720",
                    "name": "测试经理",
                    "begin_date": "20200101",
                    "end_date": None,
                }
            ],
            kwargs,
        )

    def fund_share(self, **kwargs):
        return self._call(
            "fund_share",
            [{"ts_code": "110020.OF", "trade_date": "20260730", "fd_share": 100.0}],
            kwargs,
        )

    @staticmethod
    def _financial():
        return [
            {
                "ts_code": "000001.SZ",
                "ann_date": "20260730",
                "end_date": "20260630",
                "revenue": 1000,
                "netprofit_margin": 12.5,
            }
        ]

    def income(self, **kwargs):
        return self._call("income", self._financial(), kwargs)

    def balancesheet(self, **kwargs):
        return self._call("balancesheet", self._financial(), kwargs)

    def cashflow(self, **kwargs):
        return self._call("cashflow", self._financial(), kwargs)

    def fina_indicator(self, **kwargs):
        return self._call("fina_indicator", self._financial(), kwargs)

    def anns_d(self, **kwargs):
        return self._call(
            "anns_d",
            [
                {
                    "ts_code": "000001.SZ",
                    "ann_date": "20260730",
                    "name": "平安银行",
                    "title": "2026年半年度报告",
                    "url": "https://example.invalid/announcement.pdf",
                    "rec_time": "2026-07-30 18:00:00",
                }
            ],
            kwargs,
        )


class FakeTushareSDK:
    __version__ = "1.4.29-fixture"

    def __init__(self, client):
        self.client = client
        self.received_tokens = []

    def pro_api(self, token):
        self.received_tokens.append(token)
        return self.client


class TushareCNProviderTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.execute("PRAGMA foreign_keys=ON")
        ensure_financial_tables(self.connection.cursor())
        self.registry = InstrumentRegistry(self.connection)
        self.registry.load_controlled_seed()
        self.client = FakeTushareClient()
        self.provider = TushareCNProvider(
            instrument_registry=self.registry,
            client=self.client,
            settings=ENABLED_SETTINGS,
            clock=lambda: FETCHED_AT,
            connection=self.connection,
        )

    def tearDown(self):
        self.connection.close()

    def _id(self, symbol):
        return self.registry.get_by_canonical_symbol(symbol).instrument_id

    def _request(
        self,
        symbol,
        *,
        endpoint="quote",
        kind=FinancialDataKind.QUOTE,
        metric="last_price",
        parameters=None,
        request_id="request-1",
    ):
        return FinancialDataRequest(
            request_id=request_id,
            endpoint=endpoint,
            instrument_id=str(self._id(symbol)),
            metric=metric,
            data_kind=kind,
            requested_as_of=REQUESTED_AT,
            preferred_provider_id="tushare_cn",
            parameters=parameters or {},
        )

    def test_missing_token_and_disabled_gate_never_initialize_or_call_client(self):
        missing = TushareCNProvider(
            instrument_registry=self.registry,
            client=self.client,
            settings={
                "FINANCIAL_INTELLIGENCE_ENABLED": True,
                "TUSHARE_CN_ENABLED": True,
                "TUSHARE_TOKEN": "",
            },
            clock=lambda: FETCHED_AT,
        )
        with self.assertRaises(PermissionDeniedError) as denied:
            missing.fetch_validated(self._request("000001.SZ"))
        self.assertEqual(denied.exception.details["gate_reason"], "tushare_token_missing")
        self.assertEqual(self.client.calls, [])

        disabled = TushareCNProvider(
            instrument_registry=self.registry,
            client=self.client,
            settings={
                "FINANCIAL_INTELLIGENCE_ENABLED": False,
                "TUSHARE_CN_ENABLED": True,
                "TUSHARE_TOKEN": TEST_TOKEN,
            },
            clock=lambda: FETCHED_AT,
        )
        with self.assertRaises(PermissionDeniedError):
            disabled.fetch_validated(self._request("000001.SZ"))
        self.assertEqual(self.client.calls, [])

    def test_realtime_equity_etf_and_index_use_exact_entitled_endpoint(self):
        stock = self.provider.fetch_validated(self._request("000001.SZ"))
        etf = self.provider.fetch_validated(
            self._request("510300.SH", request_id="etf")
        )
        index = self.provider.fetch_validated(
            self._request("000001.SH", request_id="index")
        )
        self.assertEqual(stock.records[0].value, 10.5)
        self.assertEqual(etf.records[0].value, 4.2)
        self.assertEqual(index.records[0].value, 3600)
        self.assertEqual(
            [name for name, _ in self.client.calls],
            ["rt_min", "rt_etf_min", "rt_idx_min"],
        )
        self.assertEqual(stock.records[0].freshness_state, FreshnessState.CURRENT)
        self.assertIn(
            "account_realtime_entitlement_required", stock.records[0].quality_flags
        )

    def test_stock_etf_and_index_daily_bars_have_no_implicit_adjustment(self):
        for number, (symbol, api_name, price) in enumerate(
            (
                ("000001.SZ", "daily", 10.3),
                ("510300.SH", "fund_daily", 4.2),
                ("000001.SH", "index_daily", 3600),
            )
        ):
            response = self.provider.fetch_validated(
                self._request(
                    symbol,
                    endpoint="bars",
                    kind=FinancialDataKind.BAR,
                    metric="ohlcv",
                    parameters={
                        "interval": "1d",
                        "start": "20260729",
                        "end": "20260730",
                        "adjustment": "raw",
                    },
                    request_id=f"bar-{number}",
                )
            )
            self.assertEqual(response.records[0].value[0]["close"], price)
            self.assertEqual(self.client.calls[-1][0], api_name)

        with self.assertRaises(UnsupportedAssetError):
            self.provider.fetch_validated(
                self._request(
                    "000001.SZ",
                    endpoint="bars",
                    kind=FinancialDataKind.BAR,
                    metric="ohlcv",
                    parameters={
                        "interval": "1d",
                        "start": "20260729",
                        "end": "20260730",
                        "adjustment": "qfq",
                    },
                    request_id="adjusted",
                )
            )
        with self.assertRaisesRegex(ValueError, "later than requested_as_of"):
            self.provider.fetch_validated(
                self._request(
                    "000001.SZ",
                    endpoint="bars",
                    kind=FinancialDataKind.BAR,
                    metric="ohlcv",
                    parameters={
                        "interval": "1d",
                        "start": "20260731",
                        "end": "20260801",
                    },
                    request_id="lookahead",
                )
            )

    def test_master_constituent_fund_financial_and_announcement_contracts(self):
        master = self.provider.fetch_validated(
            self._request(
                "000001.SZ",
                endpoint="instrument_master",
                kind=FinancialDataKind.FUNDAMENTAL,
                metric="instrument_master",
            )
        )
        self.assertEqual(master.records[0].value["industry"], "银行")

        constituents = self.provider.fetch_validated(
            self._request(
                "000300.SH",
                endpoint="constituents",
                kind=FinancialDataKind.CONSTITUENT,
                metric="constituents",
                parameters={"start": "20260701", "end": "20260731"},
                request_id="constituents",
            )
        )
        self.assertEqual(len(constituents.records[0].value), 2)

        fund = self.provider.fetch_validated(
            self._request(
                "110020.OF",
                endpoint="fund",
                kind=FinancialDataKind.FUNDAMENTAL,
                metric="fund_nav",
                parameters={"start": "20260701", "end": "20260731"},
                request_id="fund",
            )
        )
        self.assertEqual(fund.records[0].value[0]["unit_nav"], 1.3)

        for number, metric in enumerate(("fund_holdings", "fund_manager", "fund_share")):
            parameters = {}
            if metric in {"fund_holdings", "fund_share"}:
                parameters = {"start": "20260701", "end": "20260731"}
            detail = self.provider.fetch_validated(
                self._request(
                    "110020.OF",
                    endpoint="fund",
                    kind=FinancialDataKind.FUNDAMENTAL,
                    metric=metric,
                    parameters=parameters,
                    request_id=f"fund-detail-{number}",
                )
            )
            self.assertEqual(detail.records[0].metric, metric)

        financial = self.provider.fetch_validated(
            self._request(
                "000001.SZ",
                endpoint="financials",
                kind=FinancialDataKind.FUNDAMENTAL,
                metric="financial_statement",
                parameters={
                    "statement": "income",
                    "start": "20260101",
                    "end": "20260731",
                },
                request_id="financial",
            )
        )
        self.assertEqual(financial.records[0].value[0]["revenue"], 1000)
        self.assertEqual(
            financial.records[0].lineage["lookahead_guard"],
            "ann_date_used_instead_of_report_end_date",
        )

        news = self.provider.fetch_validated(
            self._request(
                "000001.SZ",
                endpoint="announcements",
                kind=FinancialDataKind.NEWS,
                metric="company_announcements",
                parameters={"start": "20260701", "end": "20260731"},
                request_id="news",
            )
        )
        self.assertEqual(news.records[0].value[0]["title"], "2026年半年度报告")
        self.assertTrue(news.records[0].value[0]["url"].endswith(".pdf"))

    def test_permission_probe_reports_full_partial_invalid_and_never_token(self):
        full = self.provider.probe_permissions(
            request_id="permission-full", requested_at=REQUESTED_AT
        )
        self.assertEqual(full["overall"], "available")
        self.assertTrue(
            all(value == "available" for value in full["capabilities"].values())
        )

        self.client.overrides.update(
            {
                "rt_min": RuntimeError("抱歉，您没有访问该接口的权限"),
                "anns_d": RuntimeError("no permission"),
                "rt_idx_min": RuntimeError("每分钟最多访问该接口10次"),
                "fina_indicator": ConnectionError("network unavailable"),
            }
        )
        partial = self.provider.probe_permissions(
            request_id="permission-partial", requested_at=REQUESTED_AT
        )
        self.assertEqual(partial["overall"], "partial")
        self.assertEqual(partial["capabilities"]["realtime_equity"], "no_permission")
        self.assertEqual(partial["capabilities"]["announcements"], "no_permission")
        self.assertEqual(partial["capabilities"]["realtime_index"], "rate_limited")
        self.assertEqual(
            partial["capabilities"]["financials"], "temporarily_unavailable"
        )

        self.client.calls.clear()
        self.client.overrides.clear()
        self.client.overrides["stock_basic"] = RuntimeError(
            f"TOKEN无效 {TEST_TOKEN}"
        )
        invalid = self.provider.probe_permissions(
            request_id="permission-invalid", requested_at=REQUESTED_AT
        )
        self.assertEqual(invalid["overall"], "invalid_token")
        self.assertEqual(invalid["token_status"], "invalid")
        self.assertEqual(len(self.client.calls), 1)
        rendered = json.dumps(invalid, ensure_ascii=False)
        self.assertNotIn(TEST_TOKEN, rendered)
        metadata = self.connection.execute(
            "SELECT metadata_json FROM financial_provider_profiles "
            "WHERE provider_key='tushare_cn'"
        ).fetchone()[0]
        self.assertNotIn(TEST_TOKEN, metadata)
        self.assertEqual(
            self.connection.execute(
                "SELECT health_status FROM financial_provider_profiles "
                "WHERE provider_key='tushare_cn'"
            ).fetchone()[0],
            "auth_failed",
        )

    def test_sdk_receives_token_only_during_client_construction(self):
        sdk_client = FakeTushareClient()
        sdk = FakeTushareSDK(sdk_client)
        provider = TushareCNProvider(
            instrument_registry=self.registry,
            sdk=sdk,
            settings=ENABLED_SETTINGS,
            clock=lambda: FETCHED_AT,
        )
        response = provider.fetch_validated(self._request("000001.SZ"))
        self.assertEqual(response.records[0].value, 10.5)
        self.assertEqual(sdk.received_tokens, [TEST_TOKEN])
        self.assertEqual(
            sdk_client._DataApi__http_url, "https://api.waditu.com/dataapi"
        )
        self.assertNotIn(
            TEST_TOKEN,
            json.dumps(response.records[0].to_dict(), ensure_ascii=False),
        )
        self.assertNotIn(TEST_TOKEN, json.dumps(sdk_client.calls, ensure_ascii=False))

    def test_sdk_transport_is_rejected_when_endpoint_cannot_be_verified(self):
        sdk_client = FakeTushareClient()
        sdk_client._DataApi__http_url = "http://untrusted.invalid/dataapi"
        provider = TushareCNProvider(
            instrument_registry=self.registry,
            sdk=FakeTushareSDK(sdk_client),
            settings=ENABLED_SETTINGS,
            clock=lambda: FETCHED_AT,
        )
        with self.assertRaises(TemporarilyUnavailableError) as unsafe:
            provider.fetch_validated(self._request("000001.SZ"))
        self.assertEqual(unsafe.exception.details["failure"], "unsafe_sdk_transport")
        self.assertEqual(sdk_client.calls, [])

    def test_field_drift_empty_rate_network_timestamp_and_timeout_are_stable(self):
        request = self._request("000001.SZ")
        self.client.overrides["rt_min"] = [{"time": "2026-07-31 09:59:55"}]
        with self.assertRaises(TemporarilyUnavailableError) as drift:
            self.provider.fetch_validated(request)
        self.assertEqual(drift.exception.details["failure"], "field_drift")

        self.client.overrides["rt_min"] = []
        with self.assertRaises(TemporarilyUnavailableError) as empty:
            self.provider.fetch_validated(request)
        self.assertEqual(empty.exception.details["failure"], "empty_response")

        self.client.overrides["rt_min"] = RuntimeError("HTTP 429 too many")
        with self.assertRaises(RateLimitedError):
            self.provider.fetch_validated(request)

        self.client.overrides["rt_min"] = Exception(
            "抱歉，您没有接口(rt_min)访问权限"
        )
        with self.assertRaises(PermissionDeniedError):
            self.provider.fetch_validated(request)

        self.client.overrides["rt_min"] = ConnectionError("network down")
        with self.assertRaises(TemporarilyUnavailableError):
            self.provider.fetch_validated(request)

        bad_time = dict(FakeTushareClient().rt_min()[0])
        bad_time["time"] = "not-a-time"
        self.client.overrides["rt_min"] = [bad_time]
        with self.assertRaises(TemporarilyUnavailableError) as timestamp:
            self.provider.fetch_validated(request)
        self.assertEqual(timestamp.exception.details["failure"], "invalid_timestamp")

        def slow(**kwargs):
            time.sleep(0.1)
            return []

        self.client.rt_min = slow
        provider = TushareCNProvider(
            instrument_registry=self.registry,
            client=self.client,
            settings={**ENABLED_SETTINGS, "FINANCIAL_PROVIDER_TIMEOUT_SECONDS": 0},
            clock=lambda: FETCHED_AT,
        )
        with self.assertRaises(TemporarilyUnavailableError) as timeout:
            provider.fetch_validated(request)
        self.assertEqual(timeout.exception.details["failure"], "hard_timeout")

    def test_snapshot_is_idempotent_and_contains_no_raw_secret_marker_or_token(self):
        response = self.provider.fetch_and_persist(self._request("000001.SZ"))
        self.provider.persist_response(response)
        rows = self.connection.execute(
            "SELECT payload_json, quality_status FROM financial_data_snapshots"
        ).fetchall()
        profile = self.connection.execute(
            "SELECT access_tier, is_enabled, metadata_json "
            "FROM financial_provider_profiles WHERE provider_key='tushare_cn'"
        ).fetchone()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("secret_marker", rows[0][0])
        self.assertNotIn(TEST_TOKEN, rows[0][0])
        self.assertEqual(rows[0][1], "normalized_current")
        self.assertEqual(profile[:2], ("account_token_points_or_paid_entitlement", 1))
        self.assertNotIn(TEST_TOKEN, profile[2])

    def test_no_token_permission_probe_is_not_configured_and_never_calls_sdk(self):
        provider = TushareCNProvider(
            instrument_registry=self.registry,
            client=self.client,
            settings={
                "FINANCIAL_INTELLIGENCE_ENABLED": True,
                "TUSHARE_CN_ENABLED": True,
                "TUSHARE_TOKEN": "",
            },
            clock=lambda: FETCHED_AT,
            connection=self.connection,
        )
        result = provider.probe_permissions(
            request_id="unconfigured", requested_at=REQUESTED_AT
        )
        self.assertEqual(result["overall"], "not_configured")
        self.assertEqual(result["token_status"], "not_configured")
        self.assertEqual(self.client.calls, [])
        self.assertEqual(
            self.connection.execute(
                "SELECT is_enabled, health_status FROM financial_provider_profiles "
                "WHERE provider_key='tushare_cn'"
            ).fetchone(),
            (0, "not_configured"),
        )

    def test_hk_and_wrong_kind_are_explicitly_unsupported(self):
        with self.assertRaises(UnsupportedAssetError):
            self.provider.fetch_validated(self._request("0700.HK"))
        with self.assertRaises(UnsupportedAssetError):
            self.provider.fetch_validated(
                self._request("000001.SZ", kind=FinancialDataKind.BAR)
            )
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()

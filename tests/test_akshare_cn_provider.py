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
    InvalidSymbolError,
    PermissionDeniedError,
    RateLimitedError,
    TemporarilyUnavailableError,
    UnsupportedAssetError,
)
from financial_providers.akshare_cn import AKShareCNProvider
from financial_schema import ensure_financial_tables


REQUESTED_AT = datetime(2026, 7, 31, 2, 0, 0, tzinfo=timezone.utc)
FETCHED_AT = datetime(2026, 7, 31, 2, 0, 2, tzinfo=timezone.utc)
ENABLED_SETTINGS = {
    "FINANCIAL_INTELLIGENCE_ENABLED": True,
    "AKSHARE_CN_ENABLED": True,
    "FINANCIAL_QUOTE_FRESHNESS_SECONDS": 300,
    "FINANCIAL_MARKET_BREADTH_FRESHNESS_SECONDS": 300,
    "FINANCIAL_FUNDAMENTAL_FRESHNESS_SECONDS": 86400,
    "FINANCIAL_PROVIDER_TIMEOUT_SECONDS": 20,
}


class FakeAKShare:
    __version__ = "1.18.72-fixture"

    def __init__(self):
        self.calls = []
        self.overrides = {}

    def _result(self, name, default, kwargs):
        self.calls.append((name, kwargs))
        value = self.overrides.get(name, default)
        if isinstance(value, Exception):
            raise value
        return value

    def stock_zh_a_spot(self, **kwargs):
        return self._result(
            "stock_zh_a_spot",
            [
                {
                    "代码": "000001",
                    "名称": "平安银行",
                    "最新价": 10.5,
                    "涨跌幅": 1.2,
                    "涨跌额": 0.12,
                    "成交量": 10000,
                    "成交额": 105000,
                    "最高": 10.6,
                    "最低": 10.2,
                    "今开": 10.3,
                    "昨收": 10.38,
                    "secret_marker": "must-not-be-persisted",
                },
                {"代码": "600001", "名称": "沪股", "最新价": 8, "涨跌幅": -1},
                {"代码": "300001", "名称": "深股", "最新价": 9, "涨跌幅": 0},
                {"代码": "830001", "名称": "北股", "最新价": 7, "涨跌幅": 2},
            ],
            kwargs,
        )

    def stock_bid_ask_em(self, **kwargs):
        return self._result(
            "stock_bid_ask_em",
            [
                {"item": "最新", "value": 10.5},
                {"item": "涨幅", "value": 1.2},
                {"item": "涨跌", "value": 0.12},
                {"item": "总手", "value": 10000},
                {"item": "金额", "value": 105000},
                {"item": "最高", "value": 10.6},
                {"item": "最低", "value": 10.2},
                {"item": "今开", "value": 10.3},
                {"item": "昨收", "value": 10.38},
                {"item": "secret_marker", "value": "must-not-be-persisted"},
            ],
            kwargs,
        )

    def stock_individual_spot_xq(self, **kwargs):
        symbol = kwargs.get("symbol")
        values = {
            "SZ000001": ("平安银行", 10.5, None),
            "SH000001": ("上证指数", 3600, None),
            "SZ399001": ("深证成指", 11100, None),
            "SH510300": ("沪深300ETF", 4.2, "2026-07-31T10:00:00+08:00"),
            "SH000300": ("沪深300", 4200, None),
        }
        name, price, observed = values[symbol]
        rows = [
            {"item": "代码", "value": symbol},
            {"item": "名称", "value": name},
            {"item": "现价", "value": price},
            {"item": "涨幅", "value": 1.2},
            {"item": "涨跌", "value": 0.12},
            {"item": "成交量", "value": 10000},
            {"item": "成交额", "value": 105000},
            {"item": "最高", "value": price + 0.1},
            {"item": "最低", "value": price - 0.1},
            {"item": "今开", "value": price - 0.05},
            {"item": "昨收", "value": price - 0.12},
            {"item": "secret_marker", "value": "must-not-be-persisted"},
        ]
        if observed:
            rows.append({"item": "时间", "value": observed})
        return self._result("stock_individual_spot_xq", rows, kwargs)

    def fund_etf_spot_em(self, **kwargs):
        return self._result(
            "fund_etf_spot_em",
            [
                {
                    "代码": "510300",
                    "名称": "沪深300ETF",
                    "最新价": 4.2,
                    "涨跌幅": 0.5,
                    "涨跌额": 0.02,
                    "成交量": 2000,
                    "成交额": 8400,
                    "最高": 4.22,
                    "最低": 4.15,
                    "开盘价": 4.18,
                    "昨收": 4.18,
                    "更新时间": "2026-07-31T10:00:00+08:00",
                }
            ],
            kwargs,
        )

    def stock_zh_index_spot_sina(self, **kwargs):
        rows = [
                {
                    "代码": "sz399001",
                    "名称": "深证成指",
                    "最新价": 11100,
                    "涨跌幅": 0.8,
                    "涨跌额": 88,
                    "成交量": 1200,
                    "成交额": 3000,
                    "最高": 11120,
                    "最低": 10900,
                    "今开": 11000,
                    "昨收": 11012,
                },
                {"代码": "sh000300", "名称": "沪深300", "最新价": 4200},
                {
                    "代码": "sh000001",
                    "名称": "上证指数",
                    "最新价": 3600,
                    "涨跌幅": 0.3,
                    "涨跌额": 10,
                    "成交量": 1000,
                    "成交额": 2000,
                    "最高": 3610,
                    "最低": 3580,
                    "今开": 3590,
                    "昨收": 3590,
                },
            ]
        return self._result("stock_zh_index_spot_sina", rows, kwargs)

    def stock_zh_a_hist(self, **kwargs):
        return self._result(
            "stock_zh_a_hist",
            [
                {
                    "日期": "2026-07-29",
                    "开盘": 10,
                    "收盘": 10.1,
                    "最高": 10.2,
                    "最低": 9.9,
                    "成交量": 100,
                    "成交额": 1000,
                },
                {
                    "日期": "2026-07-30",
                    "开盘": 10.1,
                    "收盘": 10.3,
                    "最高": 10.4,
                    "最低": 10,
                    "成交量": 120,
                    "成交额": 1230,
                },
            ],
            kwargs,
        )

    def stock_zh_a_hist_min_em(self, **kwargs):
        return self._result(
            "stock_zh_a_hist_min_em",
            [
                {
                    "时间": "2026-07-31 09:59:55",
                    "开盘": 10.3,
                    "收盘": 10.31,
                    "最高": 10.32,
                    "最低": 10.29,
                    "成交量": 10,
                    "成交额": 103,
                    "secret_marker": "must-not-be-persisted",
                }
            ],
            kwargs,
        )

    def stock_zh_a_hist_tx(self, **kwargs):
        return self._result(
            "stock_zh_a_hist_tx",
            [
                {
                    "date": "2026-07-30",
                    "open": 10.3,
                    "close": 10.31,
                    "high": 10.32,
                    "low": 10.29,
                    "amount": 10,
                    "secret_marker": "must-not-be-persisted",
                }
            ],
            kwargs,
        )

    def fund_etf_hist_em(self, **kwargs):
        return self.stock_zh_a_hist(**kwargs)

    def fund_etf_hist_min_em(self, **kwargs):
        return self._result(
            "fund_etf_hist_min_em",
            [
                {
                    "时间": "2026-07-31 09:59:55",
                    "开盘": 4.18,
                    "收盘": 4.2,
                    "最高": 4.22,
                    "最低": 4.15,
                    "成交量": 2000,
                    "成交额": 8400,
                }
            ],
            kwargs,
        )

    def fund_etf_fund_info_em(self, **kwargs):
        return self._result(
            "fund_etf_fund_info_em",
            [
                {
                    "净值日期": "2026-07-30",
                    "单位净值": 4.19,
                    "累计净值": 2.18,
                    "日增长率": 0.4,
                    "申购状态": "场内交易",
                    "赎回状态": "场内交易",
                }
            ],
            kwargs,
        )

    def fund_open_fund_info_em(self, **kwargs):
        return self._result(
            "fund_open_fund_info_em",
            [
                {"净值日期": "2026-07-29", "单位净值": 1.29, "日增长率": -0.1},
                {"净值日期": "2026-07-30", "单位净值": 1.30, "日增长率": 0.8},
            ],
            kwargs,
        )

    def fund_portfolio_hold_em(self, **kwargs):
        return self._result(
            "fund_portfolio_hold_em",
            [
                {
                    "股票代码": "600519",
                    "股票名称": "贵州茅台",
                    "占净值比例": 5.1,
                    "持股数": 1.2,
                    "持仓市值": 1800,
                    "季度": "2026年2季度股票投资明细",
                }
            ],
            kwargs,
        )

    def fund_manager_em(self, **kwargs):
        return self._result(
            "fund_manager_em",
            [
                {
                    "姓名": "测试经理",
                    "所属公司": "测试基金",
                    "现任基金代码": "110020",
                    "现任基金": "易方达300联接A",
                    "累计从业时间": 1000,
                    "现任基金资产总规模": 80,
                    "现任基金最佳回报": 20,
                }
            ],
            kwargs,
        )

    def fund_fee_em(self, **kwargs):
        indicator = kwargs.get("indicator")
        row = (
            {"费用类别": "管理费", "费率": "0.15%"}
            if indicator == "运作费用"
            else {"项目": "申购状态", "状态": "开放申购"}
        )
        return self._result("fund_fee_em", [row], kwargs)

    def index_zh_a_hist(self, **kwargs):
        return self.stock_zh_a_hist(**kwargs)

    def index_stock_cons_weight_csindex(self, **kwargs):
        return self._result(
            "index_stock_cons_weight_csindex",
            [
                {
                    "日期": "2026-07-30",
                    "成分券代码": "000001",
                    "成分券名称": "平安银行",
                    "交易所": "深圳证券交易所",
                    "权重": 0.5,
                },
                {
                    "日期": "2026-07-30",
                    "成分券代码": "600001",
                    "成分券名称": "沪股",
                    "交易所": "上海证券交易所",
                    "权重": 0.3,
                },
            ],
            kwargs,
        )

    def stock_individual_info_em(self, **kwargs):
        return self._result(
            "stock_individual_info_em",
            [
                {"item": "股票简称", "value": "平安银行"},
                {"item": "行业", "value": "银行"},
                {"item": "时间", "value": "2026-07-31 10:00:00"},
            ],
            kwargs,
        )

    def stock_board_industry_name_em(self, **kwargs):
        return self._result(
            "stock_board_industry_name_em",
            [
                {
                    "板块名称": "银行",
                    "板块代码": "BK0475",
                    "涨跌幅": 1.2,
                    "换手率": 0.8,
                    "上涨家数": 30,
                    "下跌家数": 10,
                    "领涨股票": "平安银行",
                },
                {
                    "板块名称": "计算机设备",
                    "板块代码": "BK0737",
                    "涨跌幅": -0.5,
                    "换手率": 1.6,
                    "上涨家数": 8,
                    "下跌家数": 24,
                    "领涨股票": "测试股份",
                },
            ],
            kwargs,
        )


class AKShareCNProviderTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.execute("PRAGMA foreign_keys=ON")
        ensure_financial_tables(self.connection.cursor())
        self.registry = InstrumentRegistry(self.connection)
        self.registry.load_controlled_seed()
        self.sdk = FakeAKShare()
        self.provider = AKShareCNProvider(
            instrument_registry=self.registry,
            sdk=self.sdk,
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
            preferred_provider_id="akshare_cn",
            parameters=parameters or {},
        )

    def test_stock_index_and_etf_quotes_follow_one_exact_sdk_endpoint(self):
        stock = self.provider.fetch_validated(self._request("000001.SZ"))
        shanghai = self.provider.fetch_validated(
            self._request("000001.SH", request_id="request-sh")
        )
        shenzhen = self.provider.fetch_validated(
            self._request("399001.SZ", request_id="request-sz")
        )
        etf = self.provider.fetch_validated(
            self._request("510300.SH", request_id="request-etf")
        )

        self.assertEqual(stock.records[0].value, 10.31)
        self.assertEqual(
            stock.records[0].observed_at.isoformat(), "2026-07-30T07:00:00+00:00"
        )
        self.assertEqual(shanghai.records[0].value, 3600)
        self.assertEqual(shenzhen.records[0].value, 11100)
        self.assertEqual(etf.records[0].value, 4.2)
        self.assertEqual(
            [name for name, _ in self.sdk.calls],
            [
                "stock_zh_a_hist_tx",
                "stock_zh_index_spot_sina",
                "stock_zh_index_spot_sina",
                "fund_etf_hist_min_em",
            ],
        )
        self.assertEqual(self.sdk.calls[1][1], {})
        self.assertEqual(self.sdk.calls[2][1], {})

    def test_missing_provider_timestamp_is_unknown_not_current(self):
        self.sdk.overrides["stock_zh_a_hist_tx"] = [
            {
                "date": None,
                "open": 10.3,
                "close": 10.31,
                "high": 10.32,
                "low": 10.29,
                "amount": 10,
            }
        ]
        response = self.provider.fetch_validated(self._request("000001.SZ"))
        record = response.records[0]
        self.assertEqual(record.freshness_state, FreshnessState.UNKNOWN)
        self.assertIn("provider_timestamp_missing", record.quality_flags)
        self.assertEqual(record.observed_at, REQUESTED_AT)
        self.assertIn(
            "observed_at_bounded_by_requested_as_of", record.quality_flags
        )
        self.sdk.overrides.pop("stock_zh_a_hist_tx")

        etf = self.provider.fetch_validated(
            self._request("510300.SH", request_id="timestamp-etf")
        )
        self.assertEqual(etf.records[0].freshness_state, FreshnessState.CURRENT)
        self.assertEqual(
            etf.records[0].observed_at.isoformat(), "2026-07-31T01:59:55+00:00"
        )

    def test_daily_and_minute_bars_normalize_adjustment_and_dates(self):
        daily = self.provider.fetch_validated(
            self._request(
                "000001.SZ",
                endpoint="bars",
                kind=FinancialDataKind.BAR,
                metric="ohlcv",
                parameters={
                    "interval": "1d",
                    "start": "2026-07-29",
                    "end": "2026-07-30",
                    "adjustment": "qfq",
                },
            )
        )
        self.assertEqual(len(daily.records[0].value), 2)
        self.assertEqual(daily.records[0].adjustment.value, "qfq")
        self.assertEqual(self.sdk.calls[-1][1]["adjust"], "qfq")

        minute = self.provider.fetch_validated(
            self._request(
                "000001.SZ",
                endpoint="bars",
                kind=FinancialDataKind.BAR,
                metric="ohlcv",
                parameters={
                    "interval": "1m",
                    "start": "20260731",
                    "end": "20260731",
                },
                request_id="minute-bars",
            )
        )
        self.assertEqual(minute.records[0].value[0]["close"], 10.31)
        self.assertEqual(self.sdk.calls[-1][0], "stock_zh_a_hist_min_em")

    def test_etf_and_open_fund_use_distinct_nav_holdings_manager_and_fee_contracts(self):
        etf_nav = self.provider.fetch_validated(
            self._request(
                "510300.SH",
                endpoint="fund",
                kind=FinancialDataKind.FUNDAMENTAL,
                metric="fund_nav",
                parameters={"start": "20260701", "end": "20260731"},
                request_id="etf-nav",
            )
        )
        fund_nav = self.provider.fetch_validated(
            self._request(
                "110020.OF",
                endpoint="fund",
                kind=FinancialDataKind.FUNDAMENTAL,
                metric="fund_nav",
                parameters={"start": "20260701", "end": "20260731"},
                request_id="fund-nav",
            )
        )
        holdings = self.provider.fetch_validated(
            self._request(
                "110020.OF",
                endpoint="fund",
                kind=FinancialDataKind.FUNDAMENTAL,
                metric="fund_holdings",
                parameters={"year": "2026"},
                request_id="fund-holdings",
            )
        )
        manager = self.provider.fetch_validated(
            self._request(
                "110020.OF",
                endpoint="fund",
                kind=FinancialDataKind.FUNDAMENTAL,
                metric="fund_manager",
                request_id="fund-manager",
            )
        )
        fees = self.provider.fetch_validated(
            self._request(
                "110020.OF",
                endpoint="fund",
                kind=FinancialDataKind.FUNDAMENTAL,
                metric="fund_operating_fees",
                request_id="fund-fees",
            )
        )
        trading = self.provider.fetch_validated(
            self._request(
                "110020.OF",
                endpoint="fund",
                kind=FinancialDataKind.FUNDAMENTAL,
                metric="fund_subscription_redemption",
                request_id="fund-trading",
            )
        )

        self.assertEqual(etf_nav.records[0].value[0]["accum_nav"], 2.18)
        self.assertEqual(fund_nav.records[0].value[-1]["unit_nav"], 1.3)
        self.assertIsNone(fund_nav.records[0].value[-1]["accum_nav"])
        self.assertIn("holdings_disclosure_lag_applies", holdings.records[0].quality_flags)
        self.assertFalse(holdings.records[0].lineage["point_in_time_backtest_safe"])
        self.assertEqual(manager.records[0].value[0]["manager_name"], "测试经理")
        self.assertEqual(fees.records[0].lineage["indicator"], "运作费用")
        self.assertEqual(trading.records[0].lineage["indicator"], "交易状态")
        self.assertEqual(
            [name for name, _ in self.sdk.calls[-6:]],
            [
                "fund_etf_fund_info_em",
                "fund_open_fund_info_em",
                "fund_portfolio_hold_em",
                "fund_manager_em",
                "fund_fee_em",
                "fund_fee_em",
            ],
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
                    request_id="lookahead-bars",
                )
            )

    def test_constituents_industry_and_breadth_are_factual_contract_records(self):
        constituents = self.provider.fetch_validated(
            self._request(
                "000300.SH",
                endpoint="constituents",
                kind=FinancialDataKind.CONSTITUENT,
                metric="constituents",
            )
        )
        self.assertEqual(len(constituents.records[0].value), 2)
        self.assertEqual(constituents.records[0].value[0]["exchange"], "XSHE")

        industry = self.provider.fetch_validated(
            self._request(
                "000001.SZ",
                endpoint="industry",
                kind=FinancialDataKind.FUNDAMENTAL,
                metric="industry_classification",
                request_id="industry",
            )
        )
        self.assertEqual(industry.records[0].value, "银行")

        breadth = self.provider.fetch_validated(
            self._request(
                "000001.SH",
                endpoint="market_breadth",
                kind=FinancialDataKind.MACRO,
                metric="market_breadth",
                parameters={"exchange": "XSHG"},
                request_id="breadth",
            )
        )
        self.assertEqual(
            breadth.records[0].value,
            {
                "exchange": "XSHG",
                "instrument_count": 1,
                "priced_count": 1,
                "advancing": 0,
                "declining": 1,
                "unchanged": 0,
            },
        )
        self.assertEqual(breadth.records[0].freshness_state, FreshnessState.UNKNOWN)

        rotation = self.provider.fetch_validated(
            self._request(
                "000001.SH",
                endpoint="sector_rotation",
                kind=FinancialDataKind.MACRO,
                metric="sector_rotation",
                parameters={"limit": 10},
                request_id="sector-rotation",
            )
        )
        self.assertEqual(
            [item["sector_name"] for item in rotation.records[0].value],
            ["银行", "计算机设备"],
        )
        self.assertEqual(
            rotation.records[0].normalized_payload["ranking_basis"],
            "same_snapshot_change_percent_descending",
        )

    def test_field_drift_empty_table_network_and_rate_limit_are_stable_errors(self):
        request = self._request("000001.SZ")
        self.sdk.overrides["stock_zh_a_hist_tx"] = [{"date": "2026-07-30"}]
        with self.assertRaises(TemporarilyUnavailableError) as drift:
            self.provider.fetch_validated(request)
        self.assertEqual(drift.exception.details["failure"], "field_drift")
        self.assertIn("close", drift.exception.details["missing_columns"])

        self.sdk.overrides["stock_zh_a_hist_tx"] = []
        with self.assertRaises(TemporarilyUnavailableError) as empty:
            self.provider.fetch_validated(request)
        self.assertEqual(empty.exception.details["failure"], "empty_response")

        self.sdk.overrides["stock_zh_a_hist_tx"] = ConnectionError("network down")
        with self.assertRaises(TemporarilyUnavailableError) as network:
            self.provider.fetch_validated(request)
        self.assertTrue(network.exception.retryable)

        self.sdk.overrides["stock_zh_a_hist_tx"] = RuntimeError("HTTP 429 too many")
        with self.assertRaises(RateLimitedError) as limited:
            self.provider.fetch_validated(request)
        self.assertTrue(limited.exception.retryable)

    def test_failure_never_silently_calls_an_alternative_sdk_source(self):
        self.sdk.overrides["stock_zh_index_spot_sina"] = ConnectionError("offline")
        with self.assertRaises(TemporarilyUnavailableError):
            self.provider.fetch_validated(self._request("000001.SH"))
        self.assertEqual([name for name, _ in self.sdk.calls], ["stock_zh_index_spot_sina"])

    def test_hard_timeout_returns_stable_degradation_without_waiting_for_sdk(self):
        def slow_minute(**kwargs):
            time.sleep(0.1)
            return []

        self.sdk.stock_zh_a_hist_tx = slow_minute
        provider = AKShareCNProvider(
            instrument_registry=self.registry,
            sdk=self.sdk,
            settings={**ENABLED_SETTINGS, "FINANCIAL_PROVIDER_TIMEOUT_SECONDS": 0},
            clock=lambda: FETCHED_AT,
        )
        with self.assertRaises(TemporarilyUnavailableError) as timed_out:
            provider.fetch_validated(self._request("000001.SZ"))
        self.assertEqual(timed_out.exception.details["failure"], "hard_timeout")
        self.assertEqual(timed_out.exception.details["timeout_seconds"], 0)

    def test_disabled_gate_and_unsupported_assets_never_call_sdk(self):
        disabled = AKShareCNProvider(
            instrument_registry=self.registry,
            sdk=self.sdk,
            settings={
                "FINANCIAL_INTELLIGENCE_ENABLED": False,
                "AKSHARE_CN_ENABLED": True,
            },
            clock=lambda: FETCHED_AT,
        )
        with self.assertRaises(PermissionDeniedError) as denied:
            disabled.fetch_validated(self._request("000001.SZ"))
        self.assertEqual(
            denied.exception.details["gate_reason"], "financial_intelligence_disabled"
        )
        self.assertEqual(self.sdk.calls, [])

        with self.assertRaises(UnsupportedAssetError):
            self.provider.fetch_validated(self._request("0700.HK"))
        self.assertEqual(self.sdk.calls, [])

    def test_snapshot_profile_and_idempotent_persistence_store_no_raw_marker(self):
        request = self._request("000001.SZ", metric="quote")
        response = self.provider.fetch_and_persist(request)
        first_id = self.connection.execute(
            "SELECT id FROM financial_data_snapshots"
        ).fetchone()[0]
        self.provider.persist_response(response)
        rows = self.connection.execute(
            "SELECT id, payload_json, quality_status FROM financial_data_snapshots"
        ).fetchall()
        profile = self.connection.execute(
            "SELECT provider_key, access_tier, is_enabled, metadata_json "
            "FROM financial_provider_profiles"
        ).fetchone()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], first_id)
        self.assertNotIn("secret_marker", rows[0][1])
        self.assertEqual(rows[0][2], "normalized_stale")
        self.assertEqual(profile[:3], ("akshare_cn", "free_no_api_key", 1))
        metadata = json.loads(profile[3])
        self.assertEqual(metadata["package_version_required"], "1.18.72")
        self.assertEqual(len(metadata["package_wheel_sha256"]), 64)

    def test_invalid_symbol_kind_timestamp_and_health_are_explicit(self):
        bad_request = FinancialDataRequest(
            request_id="missing-id",
            endpoint="quote",
            instrument_id="999999",
            metric="last_price",
            data_kind=FinancialDataKind.QUOTE,
            requested_as_of=REQUESTED_AT,
            preferred_provider_id="akshare_cn",
        )
        with self.assertRaises(InvalidSymbolError):
            self.provider.fetch_validated(bad_request)

        with self.assertRaises(UnsupportedAssetError):
            self.provider.fetch_validated(
                self._request(
                    "000001.SZ",
                    endpoint="quote",
                    kind=FinancialDataKind.BAR,
                    request_id="kind-mismatch",
                )
            )

        self.sdk.overrides["fund_etf_hist_min_em"] = [
            {
                "时间": "not-a-time",
                "开盘": 4.18,
                "收盘": 4.2,
                "最高": 4.22,
                "最低": 4.15,
                "成交量": 2000,
                "成交额": 8400,
            },
        ]
        with self.assertRaises(TemporarilyUnavailableError) as timestamp:
            self.provider.fetch_validated(
                self._request("510300.SH", request_id="bad-time")
            )
        self.assertEqual(timestamp.exception.details["failure"], "invalid_timestamp")

        self.sdk.overrides.pop("fund_etf_hist_min_em")
        health = self.provider.health_probe(
            request_id="health-check", requested_at=REQUESTED_AT
        )
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(health["sdk_version"], "1.18.72-fixture")
        self.assertEqual(
            self.connection.execute(
                "SELECT health_status FROM financial_provider_profiles"
            ).fetchone()[0],
            "healthy",
        )


if __name__ == "__main__":
    unittest.main()

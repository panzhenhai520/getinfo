import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from financial_instruments import InstrumentRegistry
from fund_etf_research import FundETFResearch, FundETFResearchError, resolve_fund_target
from sqlite_database import SQLiteDatabase
from tradingagents_cn_data_adapter import TradingAgentsCNRunContext


NOW = datetime(2026, 8, 2, 8, 0, tzinfo=timezone.utc)


class _FundDataFixture:
    def __init__(self, run_id, instrument_id, *, fail_nav=False):
        self.context = TradingAgentsCNRunContext(run_id, instrument_id, NOW)
        self.fail_nav = fail_nav
        self.calls = []

    def _value(self, tool, status="fetched", **values):
        self.calls.append(tool)
        payload = {"status": status, "tool": tool, "snapshot_ids": [len(self.calls)]}
        payload.update(values)
        return json.dumps(payload, ensure_ascii=False)

    def get_fund_identity(self, symbol):
        return self._value("get_fund_identity", "complete", symbol=symbol)

    def get_stock_data(self, symbol, start_date, end_date):
        return self._value("get_stock_data", symbol=symbol, start=start_date, end=end_date)

    def get_verified_market_snapshot(self, symbol, curr_date, look_back):
        return self._value("get_verified_market_snapshot", symbol=symbol, requested_date=curr_date)

    def get_fund_nav(self, symbol, curr_date, look_back):
        return self._value(
            "get_fund_nav",
            "unavailable" if self.fail_nav else "fetched",
            latest_disclosed_nav_date="2026-07-31",
            requested_valuation_date=curr_date,
            valuation_basis="last_disclosed_nav_not_intraday_quote",
        )

    def get_etf_tracking(self, symbol, curr_date, look_back):
        return self._value("get_etf_tracking", "completed", benchmark_symbol="000300.SH")

    def get_etf_constituents(self, symbol, curr_date):
        return self._value("get_etf_constituents")

    def get_fund_fees(self, symbol, curr_date):
        return self._value("get_fund_fees", "complete")

    def get_etf_liquidity(self, symbol, curr_date, look_back):
        return self._value("get_etf_liquidity", "completed")

    def get_fund_profile(self, symbol, curr_date):
        return self._value("get_fund_profile")

    def get_fund_share(self, symbol, curr_date, look_back):
        return self._value("get_fund_share")

    def get_fund_holdings(self, symbol, curr_date):
        return self._value("get_fund_holdings", latest_disclosure_period="2026Q2")

    def get_fund_manager(self, symbol, curr_date):
        return self._value("get_fund_manager")

    def get_fund_subscription_redemption(self, symbol, curr_date):
        return self._value("get_fund_subscription_redemption")


class FundETFResearchTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "fund.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.connection = self.database.connection
        self.registry = InstrumentRegistry(self.connection)
        self.registry.load_controlled_seed()

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _research(self, run_id, symbol, **fixture):
        instrument = self.registry.get_by_canonical_symbol(symbol)
        self.connection.execute(
            """
            INSERT INTO financial_research_runs(
                id, trigger_type, scope_type, instrument_id, status, requested_at
            ) VALUES(?, 'test', 'instrument', ?, 'running', '2026-08-02T08:00:00.000Z')
            """,
            (run_id, instrument.instrument_id),
        )
        adapter = _FundDataFixture(run_id, instrument.instrument_id, **fixture)
        return FundETFResearch(self.connection, run_id, data_adapter=adapter), adapter

    def test_etf_uses_exchange_tracking_template_without_company_statements(self):
        research, adapter = self._research("run-etf-template", "510300.SH")
        report = research.build_report("2026-08-01")
        self.assertEqual(report["template"], "etf")
        self.assertEqual(report["status"], "complete")
        self.assertIn("market_snapshot", report["sections"])
        self.assertIn("market_history", report["sections"])
        self.assertIn("tracking", report["sections"])
        self.assertIn("tracked_index_constituents", report["sections"])
        self.assertNotIn("holdings_disclosure", report["sections"])
        self.assertFalse(report["company_fundamentals_used"])
        self.assertEqual(report["company_statement_sections"], [])
        self.assertFalse(report["execution_target_created"])
        self.assertNotIn("get_fund_holdings", adapter.calls)

    def test_open_fund_uses_nav_share_manager_and_disclosure_template_on_weekend(self):
        research, adapter = self._research("run-fund-template", "110020.OF")
        report = research.build_report("2026-08-01")
        self.assertEqual(report["template"], "open_end_fund")
        self.assertEqual(report["share_class"], "A")
        self.assertEqual(report["currency"], "CNY")
        self.assertEqual(report["latest_disclosed_nav_date"], "2026-07-31")
        self.assertIn("last disclosed NAV", report["valuation_semantics"])
        self.assertIn("holdings_disclosure", report["sections"])
        self.assertIn("manager", report["sections"])
        self.assertIn("subscription_redemption", report["sections"])
        self.assertNotIn("market_snapshot", report["sections"])
        self.assertNotIn("get_verified_market_snapshot", adapter.calls)
        self.assertFalse(report["intraday_trade_assumption_used"])

    def test_missing_nav_returns_insufficient_data_and_non_fund_is_rejected(self):
        research, _ = self._research("run-fund-no-nav", "007339.OF", fail_nav=True)
        report = research.build_report("2026-08-01")
        self.assertEqual(report["status"], "insufficient_data")
        self.assertEqual(report["error_code"], "insufficient_data")

        stock = self.registry.get_by_canonical_symbol("000001.SZ")
        self.connection.execute(
            """
            INSERT INTO financial_research_runs(
                id, trigger_type, scope_type, instrument_id, status, requested_at
            ) VALUES('run-stock-wrong', 'test', 'instrument', ?, 'running', '2026-08-02T08:00:00Z')
            """,
            (stock.instrument_id,),
        )
        fixture = _FundDataFixture("run-stock-wrong", stock.instrument_id)
        with self.assertRaises(FundETFResearchError) as caught:
            FundETFResearch(self.connection, "run-stock-wrong", data_adapter=fixture)
        self.assertEqual(caught.exception.error_code, "unsupported_asset")

    def test_resolution_requires_asset_share_class_and_currency_instead_of_guessing(self):
        index_or_etf = resolve_fund_target(
            self.registry, "沪深300", as_of="2026-08-01"
        )
        self.assertEqual(index_or_etf["status"], "resolved")
        self.assertEqual(index_or_etf["candidates"][0]["asset_type"], "etf")

        shares = resolve_fund_target(
            self.registry, "易方达沪深300ETF联接", as_of="2026-08-01"
        )
        self.assertEqual(shares["status"], "clarification_required")
        self.assertIn("share_class", shares["required_clarifications"])
        share_c = resolve_fund_target(
            self.registry,
            "易方达沪深300ETF联接",
            as_of="2026-08-01",
            asset_type="fund",
            share_class="C",
            currency="CNY",
        )
        self.assertEqual(share_c["status"], "resolved")
        self.assertEqual(share_c["candidates"][0]["canonical_symbol"], "007339.OF")

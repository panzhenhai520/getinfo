import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from financial_instruments import (
    AmbiguousInstrumentError,
    InstrumentNotFoundError,
    InstrumentRegistry,
    normalize_alias,
    stable_instrument_key,
)
from financial_schema import ensure_financial_tables
from sqlite_database import SQLiteDatabase


class InstrumentRegistryTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.execute("PRAGMA foreign_keys=ON")
        ensure_financial_tables(self.connection.cursor())
        self.registry = InstrumentRegistry(self.connection)
        self.seed_records = self.registry.load_controlled_seed()

    def tearDown(self):
        self.connection.close()

    def test_controlled_seed_is_complete_and_idempotent(self):
        instrument_count = self.connection.execute(
            "SELECT COUNT(*) FROM financial_instruments"
        ).fetchone()[0]
        alias_count = self.connection.execute(
            "SELECT COUNT(*) FROM financial_instrument_aliases"
        ).fetchone()[0]
        second = self.registry.load_controlled_seed()

        self.assertEqual(len(self.seed_records), 21)
        self.assertEqual(len(second), 21)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM financial_instruments").fetchone()[0],
            instrument_count,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM financial_instrument_aliases"
            ).fetchone()[0],
            alias_count,
        )

    def test_six_digit_code_is_ambiguous_until_exchange_is_known(self):
        ambiguous = self.registry.resolve("000001")
        self.assertEqual(ambiguous.status, "ambiguous")
        self.assertEqual(
            {item.instrument.canonical_symbol for item in ambiguous.candidates},
            {"000001.SH", "000001.SZ"},
        )
        self.assertIn("exchange", ambiguous.required_clarifications)
        with self.assertRaises(AmbiguousInstrumentError):
            self.registry.require_instrument_id("000001")

        shanghai = self.registry.resolve("000001", market="上交所")
        shenzhen = self.registry.resolve("000001", market="XSHE")
        self.assertEqual(shanghai.candidates[0].instrument.asset_type, "index")
        self.assertEqual(shenzhen.candidates[0].instrument.display_name, "平安银行")

    def test_tencent_code_name_and_full_width_input_resolve_one_identity(self):
        queries = ["00700", "0700.HK", "腾讯", "騰訊", "０７００．ＨＫ"]
        instrument_ids = {
            self.registry.require_instrument_id(query) for query in queries
        }
        self.assertEqual(len(instrument_ids), 1)
        record = self.registry.get(instrument_ids.pop())
        self.assertEqual(record.canonical_symbol, "0700.HK")
        self.assertEqual(record.instrument_key, "HK:XHKG:EQUITY:00700")
        provider_resolution = self.registry.resolve(
            "00700", provider_key="akshare_cn"
        )
        self.assertEqual(provider_resolution.status, "resolved")
        self.assertIn(
            "akshare_cn", provider_resolution.candidates[0].matched_provider_keys
        )

    def test_stable_key_covers_planned_index_equity_and_fallback_venues(self):
        expected = {
            "000001.SH": "CN:XSHG:INDEX:000001",
            "399001.SZ": "CN:XSHE:INDEX:399001",
            "000001.SZ": "CN:XSHE:EQUITY:000001",
            "0700.HK": "HK:XHKG:EQUITY:00700",
            "IXIC.US": "US:XNAS:INDEX:IXIC",
            "N225.JP": "JP:XTKS:INDEX:N225",
        }
        for symbol, instrument_key in expected.items():
            with self.subTest(symbol=symbol):
                self.assertEqual(
                    self.registry.get_by_canonical_symbol(symbol).instrument_key,
                    instrument_key,
                )
        self.assertEqual(
            stable_instrument_key(
                canonical_symbol="110020.OF",
                asset_type="fund",
                market="CN_FUND",
                country_code="CN",
            ),
            "CN:CN_FUND:FUND:110020",
        )

    def test_same_name_etf_and_index_return_asset_type_clarification(self):
        ambiguous = self.registry.resolve("沪深300")
        self.assertEqual(ambiguous.status, "ambiguous")
        self.assertEqual(
            {item.instrument.asset_type for item in ambiguous.candidates},
            {"index", "etf"},
        )
        self.assertIn("asset_type", ambiguous.required_clarifications)
        self.assertEqual(
            self.registry.resolve("沪深300", asset_type="index")
            .candidates[0]
            .instrument.canonical_symbol,
            "000300.SH",
        )
        self.assertEqual(
            self.registry.resolve("沪深300", asset_type="etf")
            .candidates[0]
            .instrument.canonical_symbol,
            "510300.SH",
        )

        # Schema v2 permits a genuinely identical alias/date/market on two IDs.
        index_id = self.registry.require_instrument_id("000300.SH")
        etf_id = self.registry.require_instrument_id("510300.SH")
        self.registry.add_alias(
            index_id, "共同名称", market="XSHG", valid_from="2020-01-01"
        )
        self.registry.add_alias(
            etf_id, "共同名称", market="XSHG", valid_from="2020-01-01"
        )
        self.assertEqual(len(self.registry.resolve("共同名称").candidates), 2)

    def test_fund_a_and_c_share_classes_remain_separate(self):
        ambiguous = self.registry.resolve("易方达沪深300ETF联接")
        self.assertEqual(ambiguous.status, "ambiguous")
        self.assertEqual(
            {item.instrument.share_class for item in ambiguous.candidates}, {"A", "C"}
        )
        self.assertIn("share_class", ambiguous.required_clarifications)

        share_a = self.registry.resolve("易方达沪深300ETF联接A")
        share_c = self.registry.resolve("易方达沪深300ETF联接C")
        self.assertNotEqual(share_a.instrument_id, share_c.instrument_id)
        self.assertEqual(share_a.candidates[0].instrument.share_class, "A")
        self.assertEqual(share_c.candidates[0].instrument.share_class, "C")
        self.assertEqual(
            self.registry.resolve(
                "易方达沪深300ETF联接", share_class="A", currency="cny"
            ).instrument_id,
            share_a.instrument_id,
        )

    def test_fund_currency_is_a_required_clarification_and_filter(self):
        for symbol, market, exchange, currency in (
            ("FUND-CNY.TEST", "CN_FUND", "", "CNY"),
            ("FUND-HKD.TEST", "XHKG", "XHKG", "HKD"),
        ):
            self.registry.upsert_instrument(
                {
                    "canonical_symbol": symbol,
                    "display_name": "同名基金",
                    "asset_type": "fund",
                    "market": market,
                    "exchange": exchange,
                    "currency": currency,
                    "country_code": "CN" if currency == "CNY" else "HK",
                    "listed_at": "2020-01-01",
                    "aliases": ["同名基金份额"],
                }
            )
        ambiguous = self.registry.resolve("同名基金份额", asset_type="fund")
        self.assertEqual(ambiguous.status, "ambiguous")
        self.assertIn("currency", ambiguous.required_clarifications)
        hkd = self.registry.resolve("同名基金份额", asset_type="fund", currency="HKD")
        self.assertEqual(hkd.status, "resolved")
        self.assertEqual(hkd.candidates[0].instrument.currency, "HKD")

    def test_rename_and_delisting_keep_stable_historical_identity(self):
        original = self.registry.upsert_instrument(
            {
                "canonical_symbol": "600001.SH",
                "display_name": "旧公司名称",
                "asset_type": "equity",
                "market": "CN",
                "exchange": "XSHG",
                "currency": "CNY",
                "country_code": "CN",
                "listed_at": "2010-01-01",
                "aliases": ["600001"],
            }
        )
        renamed = self.registry.upsert_instrument(
            {
                "canonical_symbol": "600001.SH",
                "display_name": "新公司名称",
                "asset_type": "equity",
                "market": "CN",
                "exchange": "XSHG",
                "effective_from": "2020-01-01",
            }
        )
        self.assertEqual(original.instrument_id, renamed.instrument_id)
        self.assertEqual(
            self.registry.resolve("旧公司名称", as_of="2019-12-31").instrument_id,
            original.instrument_id,
        )
        self.assertEqual(self.registry.resolve("旧公司名称").status, "not_found")
        self.assertEqual(
            self.registry.resolve("新公司名称").instrument_id, original.instrument_id
        )

        delisted = self.registry.upsert_instrument(
            {
                "canonical_symbol": "600001.SH",
                "display_name": "新公司名称",
                "asset_type": "equity",
                "market": "CN",
                "exchange": "XSHG",
                "listing_status": "delisted",
                "delisted_at": "2022-06-01",
                "effective_from": "2022-06-01",
            }
        )
        self.assertEqual(delisted.instrument_id, original.instrument_id)
        self.assertEqual(
            self.registry.resolve("600001.SH", as_of="2021-12-31").instrument_id,
            original.instrument_id,
        )
        self.assertEqual(
            self.registry.resolve("600001.SH", as_of="2022-06-01").status,
            "not_found",
        )
        current = self.registry.resolve("600001.SH")
        self.assertEqual(current.status, "resolved")
        self.assertIn("listing_status:delisted", current.candidates[0].reasons)

        with self.assertRaisesRegex(ValueError, "cannot be reactivated"):
            self.registry.upsert_instrument(
                {
                    "canonical_symbol": "600001.SH",
                    "display_name": "再次上市",
                    "asset_type": "equity",
                    "market": "CN",
                    "exchange": "XSHG",
                    "effective_from": "2024-01-01",
                }
            )

    def test_provider_master_import_maps_symbol_without_changing_identity(self):
        payload = {
            "canonical_symbol": "600002.SH",
            "provider_symbol": "600002",
            "display_name": "提供方主数据公司",
            "asset_type": "equity",
            "market": "CN",
            "exchange": "XSHG",
            "currency": "CNY",
            "country_code": "CN",
            "listed_at": "2001-01-01",
        }
        first = self.registry.import_master_records(
            [payload], provider_key="akshare_cn", observed_on="2026-07-31"
        )[0]
        second = self.registry.import_master_records(
            [payload], provider_key="akshare_cn", observed_on="2026-08-01"
        )[0]
        self.assertEqual(first.instrument_id, second.instrument_id)
        self.assertEqual(second.provider_mappings["akshare_cn"], "600002")
        self.assertEqual(second.metadata["master_source"], "akshare_cn")
        self.assertEqual(
            self.registry.resolve("600002", provider_key="akshare_cn").instrument_id,
            first.instrument_id,
        )

        changed_market = dict(payload)
        changed_market.update({"provider_symbol": "600002.SZ", "exchange": "XSHE"})
        with self.assertRaisesRegex(ValueError, "stable identity fields"):
            self.registry.import_master_records(
                [changed_market], provider_key="akshare_cn", observed_on="2026-08-02"
            )

    def test_research_run_uses_registry_instrument_id_and_fk(self):
        instrument_id = self.registry.require_instrument_id("0700.HK")
        self.connection.execute(
            """
            INSERT INTO financial_research_runs(
                id, trigger_type, scope_type, instrument_id, status
            ) VALUES('run-tencent', 'chat', 'instrument', ?, 'queued')
            """,
            (instrument_id,),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT instrument_id FROM financial_research_runs WHERE id='run-tencent'"
            ).fetchone()[0],
            instrument_id,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO financial_research_runs(id, trigger_type, scope_type, instrument_id) "
                "VALUES('bad-run', 'chat', 'instrument', 999999)"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO financial_research_runs(id, trigger_type, scope_type) "
                "VALUES('missing-run', 'chat', 'instrument')"
            )

    def test_validation_and_not_found_are_explicit(self):
        self.assertEqual(normalize_alias(" ０７００．ＨＫ "), "0700.hk")
        self.assertEqual(self.registry.resolve("不存在标的").status, "not_found")
        with self.assertRaises(InstrumentNotFoundError):
            self.registry.require_instrument_id("不存在标的")
        with self.assertRaisesRegex(ValueError, "later than"):
            self.registry.add_alias(
                self.registry.require_instrument_id("0700.HK"),
                "错误有效期",
                valid_from="2026-07-31",
                valid_to="2026-07-30",
            )
        with self.assertRaisesRegex(ValueError, "unsupported asset_type"):
            self.registry.resolve("腾讯", asset_type="company")


class InstrumentRegistrySchemaBoundaryTest(unittest.TestCase):
    def test_registry_requires_explicit_startup_schema_initialization(self):
        connection = sqlite3.connect(":memory:")
        try:
            with self.assertRaisesRegex(RuntimeError, "not initialized"):
                InstrumentRegistry(connection)
        finally:
            connection.close()


class InstrumentRegistryConcurrencyTest(unittest.TestCase):
    def test_shared_connection_concurrent_upserts_do_not_corrupt_savepoints(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = SQLiteDatabase(str(Path(temp_dir) / "instrument-concurrency.sqlite3"))
            self.assertTrue(database.connect())
            self.assertTrue(database.create_tables())
            try:
                def write(index):
                    registry = InstrumentRegistry(database.connection)
                    return registry.upsert_instrument(
                        {
                            "canonical_symbol": f"T{index:04d}.US",
                            "display_name": f"并发测试标的{index}",
                            "asset_type": "equity",
                            "market": "US",
                            "exchange": "XNAS",
                            "currency": "USD",
                            "country_code": "US",
                            "listed_at": "2020-01-01",
                            "aliases": [f"TEST-{index}"],
                        }
                    ).instrument_id

                with ThreadPoolExecutor(max_workers=12) as executor:
                    instrument_ids = list(executor.map(write, range(40)))

                self.assertEqual(len(set(instrument_ids)), 40)
                count = database.connection.execute(
                    "SELECT COUNT(*) FROM financial_instruments WHERE canonical_symbol LIKE 'T%.US'"
                ).fetchone()[0]
                self.assertEqual(count, 40)
            finally:
                database.disconnect()


if __name__ == "__main__":
    unittest.main()

import hashlib
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from financial_backtest import BacktestError, FinancialPointInTimeBacktester
from financial_paper_trading import FinancialPaperTradingJobService
from financial_worker_jobs import FinancialJobContext
from intel_database import IntelRepository
from sqlite_database import SQLiteDatabase


NOW = datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc)
SETTINGS = {
    "INTEL_DEFAULT_INDUSTRY_PACK": "family_office",
    "FINANCIAL_INTELLIGENCE_ENABLED": True,
    "TRADING_AGENTS_ENABLED": True,
    "TRADING_SIMULATION_ENABLED": True,
}


class FinancialPointInTimeBacktestTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "backtest.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.connection = self.database.connection
        self.repository = IntelRepository(self.database)
        self.provider_id = int(
            self.connection.execute(
                """
                INSERT INTO financial_provider_profiles(
                    provider_key, display_name, provider_type, access_tier,
                    is_enabled, health_status
                ) VALUES('backtest-fixture', 'Backtest Fixture', 'fixture',
                         'fixture', 1, 'healthy')
                """
            ).lastrowid
        )
        self.equity_id = self._instrument("600000.SH", "浦发银行", "equity", "CNY")
        self.other_id = self._instrument("000001.SZ", "平安银行", "equity", "CNY")
        self.hkd_id = self._instrument("0700.HK", "腾讯控股", "equity", "HKD")
        self.fund_id = self._instrument("000001.OF", "测试基金", "fund", "CNY")
        self.backtester = FinancialPointInTimeBacktester(
            self.database, settings=dict(SETTINGS), clock=lambda: NOW
        )

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _instrument(self, symbol, name, asset_type, currency):
        exchange = "XHKG" if currency == "HKD" else "XSHG"
        return int(
            self.connection.execute(
                """
                INSERT INTO financial_instruments(
                    canonical_symbol, display_name, asset_type, market,
                    exchange, currency, country_code
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol, name, asset_type, exchange, exchange, currency,
                    "HK" if currency == "HKD" else "CN",
                ),
            ).lastrowid
        )

    @staticmethod
    def _bars(prices, *, start_day=1, statuses=None, available=None, nav=False):
        rows = []
        for offset, price in enumerate(prices):
            day = start_day + offset
            row = {
                "observed_at": f"2026-01-{day:02d}T08:00:00Z",
                "open": price, "high": price + 1, "low": price - 1,
                "close": price, "volume": 1000,
            }
            if nav:
                row = {
                    "observed_at": f"2026-01-{day:02d}T08:00:00Z",
                    "nav": price,
                }
            if statuses and offset < len(statuses):
                row["market_status"] = statuses[offset]
            if available and offset < len(available) and available[offset]:
                row["available_at"] = available[offset]
            rows.append(row)
        return rows

    def _snapshot(
        self, instrument_id, rows, *, key, currency="CNY",
        fetched_at="2026-02-01T00:00:00Z", actions=None, nav=False,
    ):
        normalized = {
            "interval": "1d", "adjustment": "raw",
            "navs" if nav else "bars": rows,
        }
        if actions is not None:
            normalized["corporate_actions"] = actions
        payload = {
            "metric": "nav" if nav else "ohlcv",
            "normalized_payload": normalized,
        }
        payload_text = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(payload_text.encode()).hexdigest()
        return int(
            self.connection.execute(
                """
                INSERT INTO financial_data_snapshots(
                    snapshot_key, instrument_id, provider_profile_id, data_type,
                    interval_code, observed_at, fetched_at, market_status,
                    currency, timezone, quality_status, payload_json,
                    payload_sha256, source_url
                ) VALUES(?, ?, ?, ?, '1d', ?, ?, 'closed', ?, 'Asia/Shanghai',
                         'normalized_fixture', ?, ?, ?)
                """,
                (
                    key, instrument_id, self.provider_id, "fund_nav" if nav else "bar",
                    rows[-1]["observed_at"], fetched_at, currency, payload_text,
                    digest, f"https://fixture.example/{key}",
                ),
            ).lastrowid
        )

    def _run(
        self, snapshot_ids, *, key="run-1", instrument_id=None,
        universe_id=None, **overrides
    ):
        scope_type = str(overrides.get("scope_type") or "instrument")
        values = {
            "owner_user_id": "user-1", "industry_pack_id": "family_office",
            "idempotency_key": key, "strategy_key": "buy_and_hold_v1",
            "strategy_version": "strategy-v1", "scope_type": scope_type,
            "instrument_id": (
                None if scope_type == "universe" else instrument_id or self.equity_id
            ),
            "universe_id": universe_id,
            "start_date": "2026-01-01", "end_date": "2026-01-10",
            "initial_capital": 10000, "base_currency": "CNY",
            "snapshot_ids": snapshot_ids, "fee_rate": 0.001,
            "slippage_bps": 10, "random_seed": 7,
            "data_cutoff_at": "2026-03-01T00:00:00Z",
        }
        values.update(overrides)
        return self.backtester.run(**values)

    def _universe(self, key="TEST_INDEX"):
        return int(
            self.connection.execute(
                """
                INSERT INTO financial_universes(
                    universe_key, display_name, universe_type, market,
                    definition_json, source_provider_key, constituent_as_of
                ) VALUES(?, '测试指数', 'index', 'CN', '{}', 'fixture', '2026-01-01')
                """,
                (key,),
            ).lastrowid
        )

    def _member(self, universe_id, instrument_id, start, end=None, observed="2025-12-31T08:00:00Z"):
        self.connection.execute(
            """
            INSERT INTO financial_universe_members(
                universe_id, instrument_id, effective_from, effective_to,
                source_observed_at, metadata_json
            ) VALUES(?, ?, ?, ?, ?, '{}')
            """,
            (universe_id, instrument_id, start, end, observed),
        )

    def test_future_bar_injection_cannot_change_earlier_trade(self):
        original = self._snapshot(
            self.equity_id, self._bars([10, 11, 12, 13]), key="original"
        )
        first = self._run([original], key="future-probe-a", end_date="2026-01-04")
        injected = self._snapshot(
            self.equity_id, self._bars([10, 11, 12, 13, 999]), key="injected"
        )
        second = self._run([injected], key="future-probe-b", end_date="2026-01-05")
        first_buy = next(item for item in first["trades"] if item["side"] == "buy")
        second_buy = next(item for item in second["trades"] if item["side"] == "buy")
        self.assertEqual(
            {key: first_buy[key] for key in ("side", "quantity", "price", "signal_at", "executed_at")},
            {key: second_buy[key] for key in ("side", "quantity", "price", "signal_at", "executed_at")},
        )
        self.assertLessEqual(first_buy["signal_at"], first_buy["executed_at"])
        self.assertEqual(first_buy["reason"]["execution_lag_bars"], 1)

    def test_idempotent_run_pins_strategy_data_cost_seed_and_benchmark(self):
        snapshot = self._snapshot(self.equity_id, self._bars([10, 11, 12]), key="stable")
        benchmark = self._snapshot(self.other_id, self._bars([100, 101, 102]), key="benchmark")
        first = self._run([snapshot], key="stable-run", benchmark_snapshot_id=benchmark)
        second = self._run([snapshot], key="stable-run", benchmark_snapshot_id=benchmark)
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["trades"], second["trades"])
        config = second["config"]
        self.assertEqual(config["strategy_version"], "strategy-v1")
        self.assertEqual(config["random_seed"], 7)
        self.assertEqual(config["fee_rate"], "0.001")
        self.assertEqual(config["benchmark_manifest"]["snapshot_id"], benchmark)
        self.assertEqual(len(config["data_version"]), 64)
        self.assertFalse(config["point_in_time_policy"]["implicit_fx"])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM backtest_metrics").fetchone()[0], 0
        )

    def test_same_idempotency_key_with_changed_request_is_rejected(self):
        snapshot = self._snapshot(self.equity_id, self._bars([10, 11]), key="conflict")
        self._run([snapshot], key="same-key")
        with self.assertRaises(BacktestError) as raised:
            self._run([snapshot], key="same-key", initial_capital=20000)
        self.assertEqual(raised.exception.error_code, "backtest_idempotency_conflict")

    def test_data_cutoff_and_hash_integrity_are_fail_closed(self):
        late = self._snapshot(
            self.equity_id, self._bars([10, 11]), key="late",
            fetched_at="2026-04-01T00:00:00Z",
        )
        with self.assertRaises(BacktestError) as raised:
            self._run([late], key="late-run", data_cutoff_at="2026-03-01T00:00:00Z")
        self.assertEqual(raised.exception.error_code, "snapshot_after_data_cutoff")
        self.connection.execute(
            "UPDATE financial_data_snapshots SET payload_json='{}' WHERE id=?", (late,)
        )
        with self.assertRaises(BacktestError) as raised:
            self._run([late], key="hash-run", data_cutoff_at="2026-05-01T00:00:00Z")
        self.assertEqual(raised.exception.error_code, "snapshot_integrity_failed")

    def test_split_and_dividend_are_applied_only_from_available_actions(self):
        actions = [
            {
                "id": "split-2x", "type": "split", "ratio": 2,
                "effective_at": "2026-01-03T08:00:00Z",
                "announced_at": "2025-12-20T08:00:00Z",
            },
            {
                "id": "cash-dividend", "type": "dividend", "amount": 1,
                "currency": "CNY", "effective_at": "2026-01-04T08:00:00Z",
                "announced_at": "2025-12-21T08:00:00Z",
            },
        ]
        snapshot = self._snapshot(
            self.equity_id, self._bars([10, 11, 6, 7]), key="actions", actions=actions
        )
        result = self._run([snapshot], key="actions-run", end_date="2026-01-04", slippage_bps=0)
        buy = next(item for item in result["trades"] if item["side"] == "buy")
        sell = result["trades"][-1]
        self.assertEqual(sell["quantity"], buy["quantity"] * 2)
        audit = result["config"]["corporate_action_audit"]
        self.assertEqual({item["action_type"] for item in audit}, {"split", "dividend"})
        self.assertGreater(result["config"]["ending_cash"], 10000)

    def test_suspended_bar_delays_pending_execution_without_lookahead(self):
        snapshot = self._snapshot(
            self.equity_id,
            self._bars([10, 11, 12, 13], statuses=["trading", "suspended", "trading", "trading"]),
            key="suspension",
        )
        result = self._run([snapshot], key="suspension-run", end_date="2026-01-04")
        buy = next(item for item in result["trades"] if item["side"] == "buy")
        self.assertEqual(buy["signal_at"], "2026-01-01T08:00:00Z")
        self.assertEqual(buy["executed_at"], "2026-01-03T08:00:00Z")

    def test_universe_membership_uses_effective_and_observed_time(self):
        universe_id = self._universe()
        self._member(universe_id, self.equity_id, "2026-01-01", "2026-01-03")
        self._member(
            universe_id, self.other_id, "2026-01-03", None,
            observed="2026-01-02T08:00:00Z",
        )
        first = self._snapshot(self.equity_id, self._bars([10, 11, 12, 13]), key="member-a")
        second = self._snapshot(self.other_id, self._bars([20, 21, 22, 23]), key="member-b")
        result = self._run(
            [first, second], key="universe-run", scope_type="universe",
            instrument_id=None, universe_id=universe_id, end_date="2026-01-04",
        )
        buys = [item for item in result["trades"] if item["side"] == "buy"]
        self.assertEqual({item["instrument_id"] for item in buys}, {self.equity_id, self.other_id})
        second_buy = next(item for item in buys if item["instrument_id"] == self.other_id)
        self.assertEqual(second_buy["signal_at"], "2026-01-03T08:00:00Z")
        self.assertEqual(second_buy["executed_at"], "2026-01-04T08:00:00Z")

    def test_unknown_constituent_version_is_excluded_and_disclosed(self):
        universe_id = self._universe("UNKNOWN_INDEX")
        self._member(universe_id, self.equity_id, "2026-01-01", observed="")
        snapshot = self._snapshot(self.equity_id, self._bars([10, 11, 12]), key="unknown-member")
        result = self._run(
            [snapshot], key="unknown-member-run", scope_type="universe",
            instrument_id=None, universe_id=universe_id, end_date="2026-01-03",
        )
        self.assertEqual(result["trades"], [])
        coverage = result["config"]["coverage"]
        self.assertEqual(coverage["unknown_member_versions"], 1)
        self.assertIn("constituents_without_observed_time_excluded", coverage["limitations"])

    def test_fund_nav_is_not_interpolated_and_low_coverage_is_explicit(self):
        rows = [
            {"observed_at": "2026-01-02T08:00:00Z", "nav": 1.0},
            {"observed_at": "2026-01-09T08:00:00Z", "nav": 1.1},
        ]
        snapshot = self._snapshot(self.fund_id, rows, key="fund-nav", nav=True)
        result = self._run(
            [snapshot], key="fund-run", instrument_id=self.fund_id,
            start_date="2026-01-01", end_date="2026-01-10", slippage_bps=0,
        )
        coverage = result["config"]["coverage"]
        self.assertEqual(coverage["usable_point_in_time_observations"], 2)
        self.assertLess(coverage["coverage_ratio"], 1)
        self.assertIn(
            "fund_nav_uses_disclosed_observations_without_interpolation",
            coverage["limitations"],
        )
        self.assertEqual(
            {item["executed_at"] for item in result["trades"]}, {"2026-01-09T08:00:00Z"}
        )

    def test_mixed_currency_universe_requires_separate_runs(self):
        universe_id = self._universe("MIXED")
        self._member(universe_id, self.equity_id, "2026-01-01")
        self._member(universe_id, self.hkd_id, "2026-01-01")
        cny = self._snapshot(self.equity_id, self._bars([10, 11]), key="mixed-cny")
        hkd = self._snapshot(
            self.hkd_id, self._bars([100, 101]), key="mixed-hkd", currency="HKD"
        )
        with self.assertRaises(BacktestError) as raised:
            self._run(
                [cny, hkd], key="mixed-run", scope_type="universe",
                instrument_id=None, universe_id=universe_id,
            )
        self.assertEqual(raised.exception.error_code, "currency_mismatch")

    def test_future_available_revision_is_not_used_before_publication(self):
        rows = self._bars(
            [10, 11, 12],
            available=[None, "2026-01-20T08:00:00Z", None],
        )
        snapshot = self._snapshot(self.equity_id, rows, key="late-revision")
        result = self._run([snapshot], key="late-revision-run", end_date="2026-01-03")
        self.assertEqual(
            result["config"]["coverage"]["future_or_late_versions_ignored"], 1
        )
        self.assertTrue(
            all(item["reason"]["known_data_through"] <= item["signal_at"] for item in result["trades"])
        )

    def test_existing_worker_runner_executes_backtest_without_new_service(self):
        snapshot = self._snapshot(self.equity_id, self._bars([10, 11, 12]), key="worker")
        service = FinancialPaperTradingJobService(self.repository, settings=dict(SETTINGS))
        context = FinancialJobContext(
            job_id=77, job_type="paper_backtest", worker_id="fixture",
            repository=self.repository, cancel_event=threading.Event(),
        )
        result = service.run(
            {
                "task_kind": "backtest", "industry_pack_id": "family_office",
                "parameters": {
                    "action": "run", "owner_user_id": "worker-user",
                    "idempotency_key": "worker-backtest", "strategy_key": "buy_and_hold_v1",
                    "strategy_version": "worker-v1", "scope_type": "instrument",
                    "instrument_id": self.equity_id, "start_date": "2026-01-01",
                    "end_date": "2026-01-03", "initial_capital": 5000,
                    "base_currency": "CNY", "snapshot_ids": [snapshot],
                    "data_cutoff_at": "2026-03-01T00:00:00Z", "random_seed": 19,
                },
            },
            context,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["execution_mode"], "paper")
        self.assertFalse(result["real_order_execution"])

    def test_capability_gate_blocks_mutation(self):
        snapshot = self._snapshot(self.equity_id, self._bars([10, 11]), key="gate")
        disabled = FinancialPointInTimeBacktester(
            self.database,
            settings={**SETTINGS, "TRADING_SIMULATION_ENABLED": False},
            clock=lambda: NOW,
        )
        with self.assertRaises(Exception):
            disabled.run(
                owner_user_id="user-1", industry_pack_id="family_office",
                idempotency_key="disabled", strategy_key="buy_and_hold_v1",
                strategy_version="v1", scope_type="instrument",
                instrument_id=self.equity_id, start_date="2026-01-01",
                end_date="2026-01-02", initial_capital=1000,
                base_currency="CNY", snapshot_ids=[snapshot],
            )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0], 0
        )


if __name__ == "__main__":
    unittest.main()

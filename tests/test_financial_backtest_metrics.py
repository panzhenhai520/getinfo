import hashlib
import json
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from financial_backtest import BacktestError, FinancialPointInTimeBacktester
from financial_backtest_metrics import (
    FINANCIAL_BACKTEST_ANALYTICS_VERSION,
    FinancialBacktestAnalytics,
)
from financial_paper_trading import FinancialPaperTradingJobService
from financial_worker_jobs import FinancialJobContext
from intel_database import IntelRepository
from sqlite_database import SQLiteDatabase


NOW = datetime(2027, 2, 1, 0, 0, tzinfo=timezone.utc)
SETTINGS = {
    "INTEL_DEFAULT_INDUSTRY_PACK": "family_office",
    "FINANCIAL_INTELLIGENCE_ENABLED": True,
    "TRADING_AGENTS_ENABLED": True,
    "TRADING_SIMULATION_ENABLED": True,
}


class FinancialBacktestMetricsTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "metrics.sqlite3"))
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
                ) VALUES('metrics-fixture', 'Metrics Fixture', 'fixture',
                         'fixture', 1, 'healthy')
                """
            ).lastrowid
        )
        self.instrument_id = self._instrument("600000.SH", "浦发银行")
        self.benchmark_id = self._instrument("000300.SH", "沪深300", asset_type="index")
        self.backtester = FinancialPointInTimeBacktester(
            self.database, settings=dict(SETTINGS), clock=lambda: NOW
        )
        self.analytics = FinancialBacktestAnalytics(
            self.database, settings=dict(SETTINGS)
        )

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _instrument(self, symbol, name, *, asset_type="equity"):
        return int(
            self.connection.execute(
                """
                INSERT INTO financial_instruments(
                    canonical_symbol, display_name, asset_type, market,
                    exchange, currency, country_code
                ) VALUES(?, ?, ?, 'XSHG', 'XSHG', 'CNY', 'CN')
                """,
                (symbol, name, asset_type),
            ).lastrowid
        )

    @staticmethod
    def _rows(prices, *, start=date(2026, 1, 1)):
        result = []
        for offset, price in enumerate(prices):
            observed = datetime.combine(
                start + timedelta(days=offset), datetime.min.time(), tzinfo=timezone.utc
            ).replace(hour=8)
            result.append(
                {
                    "observed_at": observed.isoformat().replace("+00:00", "Z"),
                    "open": price, "high": price * 1.01, "low": price * 0.99,
                    "close": price, "volume": 1000,
                }
            )
        return result

    def _snapshot(self, instrument_id, rows, *, key, actions=None):
        normalized = {"interval": "1d", "adjustment": "raw", "bars": rows}
        if actions is not None:
            normalized["corporate_actions"] = actions
        payload = {"metric": "ohlcv", "normalized_payload": normalized}
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(text.encode()).hexdigest()
        return int(
            self.connection.execute(
                """
                INSERT INTO financial_data_snapshots(
                    snapshot_key, instrument_id, provider_profile_id, data_type,
                    interval_code, observed_at, fetched_at, market_status,
                    currency, timezone, quality_status, payload_json,
                    payload_sha256, source_url
                ) VALUES(?, ?, ?, 'bar', '1d', ?, '2026-07-01T00:00:00Z',
                         'closed', 'CNY', 'Asia/Shanghai', 'normalized_fixture',
                         ?, ?, ?)
                """,
                (
                    key, instrument_id, self.provider_id, rows[-1]["observed_at"],
                    text, digest, f"https://metrics.example/{key}",
                ),
            ).lastrowid
        )

    def _run(
        self, prices, *, key="metrics-run", start=date(2026, 1, 1),
        strategy="buy_and_hold_v1", strategy_parameters=None,
        benchmark_prices=None, initial_capital=10000, actions=None,
        end=None,
    ):
        rows = self._rows(prices, start=start)
        snapshot = self._snapshot(self.instrument_id, rows, key=f"{key}-data", actions=actions)
        benchmark_snapshot = None
        if benchmark_prices is not None:
            benchmark_snapshot = self._snapshot(
                self.benchmark_id, self._rows(benchmark_prices, start=start),
                key=f"{key}-benchmark",
            )
        end = end or start + timedelta(days=len(prices) - 1)
        return self.backtester.run(
            owner_user_id="user-1", industry_pack_id="family_office",
            idempotency_key=key, strategy_key=strategy, strategy_version="metrics-v1",
            strategy_parameters=strategy_parameters or {}, scope_type="instrument",
            instrument_id=self.instrument_id, start_date=start.isoformat(),
            end_date=end.isoformat(), initial_capital=initial_capital,
            base_currency="CNY", snapshot_ids=[snapshot],
            benchmark_snapshot_id=benchmark_snapshot,
            data_cutoff_at="2027-01-15T00:00:00Z", fee_rate="0.001",
            slippage_bps="10", random_seed=23,
        )

    def test_metrics_are_persisted_recomputable_and_idempotent(self):
        run = self._run([10, 11, 12, 13], key="positive")
        first = self.analytics.calculate(run["backtest_run_id"], owner_user_id="user-1")
        second = self.analytics.calculate(run["backtest_run_id"], owner_user_id="user-1")
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["metrics"], second["metrics"])
        self.assertEqual(first["analytics_version"], FINANCIAL_BACKTEST_ANALYTICS_VERSION)
        metrics = first["metrics"]
        self.assertGreater(metrics["total_return"]["value"], 0)
        self.assertGreater(metrics["turnover"]["value"], 0)
        self.assertGreater(metrics["transaction_cost_total"]["value"], 0)
        self.assertEqual(metrics["trade_count"]["value"], len(first["trades"]))
        self.assertEqual(metrics["trade_log_sha256"]["text"], first["trade_log_sha256"])
        self.assertEqual(len(first["equity_curve"]), 4)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM backtest_metrics WHERE backtest_run_id=?",
                (run["backtest_run_id"],),
            ).fetchone()[0],
            len(metrics),
        )
        cash = 10000.0
        for trade in first["trades"]:
            flow = trade["quantity"] * trade["price"]
            cash += flow - trade["fee"] if trade["side"] == "sell" else -flow - trade["fee"]
        self.assertAlmostEqual(metrics["ending_equity"]["value"], cash, places=5)

    def test_negative_return_drawdown_and_win_rate(self):
        run = self._run([10, 10, 7, 5], key="negative")
        result = self.analytics.calculate(run["backtest_run_id"], owner_user_id="user-1")
        metrics = result["metrics"]
        self.assertLess(metrics["total_return"]["value"], 0)
        self.assertLess(metrics["max_drawdown"]["value"], 0)
        self.assertEqual(metrics["win_rate"]["value"], 0)
        self.assertEqual(metrics["closed_trade_count"]["value"], 1)

    def test_zero_trade_and_short_sample_do_not_publish_misleading_ratios(self):
        run = self._run(
            [10, 11], key="zero", strategy="sma_cross_v1",
            strategy_parameters={"short_window": 2, "long_window": 3},
        )
        result = self.analytics.calculate(run["backtest_run_id"], owner_user_id="user-1")
        metrics = result["metrics"]
        self.assertEqual(metrics["trade_count"]["value"], 0)
        self.assertEqual(metrics["total_return"]["value"], 0)
        self.assertIsNone(metrics["win_rate"]["value"])
        self.assertEqual(metrics["win_rate"]["metadata"]["status"], "no_closed_trades")
        self.assertIsNone(metrics["sharpe_ratio"]["value"])
        self.assertEqual(metrics["sharpe_ratio"]["metadata"]["status"], "insufficient_sample")
        self.assertIsNone(metrics["annualized_return"]["value"])

    def test_cross_year_long_sample_has_volatility_sharpe_and_annualization(self):
        prices = [100 + offset * 0.4 + (2 if offset % 2 else -1) for offset in range(46)]
        run = self._run(prices, key="cross-year", start=date(2026, 12, 1))
        result = self.analytics.calculate(run["backtest_run_id"], owner_user_id="user-1")
        metrics = result["metrics"]
        self.assertIsNotNone(metrics["annualized_return"]["value"])
        self.assertIsNotNone(metrics["annualized_volatility"]["value"])
        self.assertIsNotNone(metrics["sharpe_ratio"]["value"])
        self.assertGreaterEqual(metrics["sharpe_ratio"]["metadata"]["sample_count"], 20)

    def test_sparse_history_carries_coverage_and_limitations(self):
        run = self._run([10, 11], key="sparse", end=date(2026, 1, 10))
        result = self.analytics.calculate(run["backtest_run_id"], owner_user_id="user-1")
        metric = result["metrics"]["data_coverage_ratio"]
        self.assertLess(metric["value"], 1)
        self.assertIn("limitations", metric["metadata"])
        self.assertIn(
            "historical_calendar_coverage_below_full", metric["metadata"]["limitations"]
        )

    def test_extreme_finite_prices_stay_finite(self):
        run = self._run(
            [1_000_000_000, 1_100_000_000, 900_000_000],
            key="extreme", initial_capital=10_000_000_000,
        )
        result = self.analytics.calculate(run["backtest_run_id"], owner_user_id="user-1")
        for key, item in result["metrics"].items():
            if item["value"] is not None:
                self.assertTrue(item["value"] == item["value"], key)

    def test_benchmark_and_relative_return_use_pinned_benchmark(self):
        run = self._run(
            [10, 11, 12, 13], key="with-benchmark",
            benchmark_prices=[100, 101, 102, 105],
        )
        result = self.analytics.calculate(run["backtest_run_id"], owner_user_id="user-1")
        metrics = result["metrics"]
        self.assertAlmostEqual(metrics["benchmark_return"]["value"], 0.05)
        self.assertAlmostEqual(
            metrics["relative_return"]["value"],
            metrics["total_return"]["value"] - 0.05,
        )
        self.assertEqual(metrics["benchmark_return"]["metadata"]["status"], "available")

    def test_missing_benchmark_is_explicit_not_fabricated(self):
        run = self._run([10, 11, 12], key="without-benchmark")
        result = self.analytics.calculate(run["backtest_run_id"], owner_user_id="user-1")
        metric = result["metrics"]["benchmark_return"]
        self.assertIsNone(metric["value"])
        self.assertEqual(metric["metadata"]["status"], "benchmark_missing")

    def test_saved_metrics_detect_later_trade_log_mutation(self):
        run = self._run([10, 11, 12], key="trade-tamper")
        self.analytics.calculate(run["backtest_run_id"], owner_user_id="user-1")
        self.connection.execute(
            "UPDATE backtest_trades SET price=price+1 WHERE backtest_run_id=? AND id=(SELECT MIN(id) FROM backtest_trades WHERE backtest_run_id=?)",
            (run["backtest_run_id"], run["backtest_run_id"]),
        )
        with self.assertRaises(BacktestError) as raised:
            self.analytics.calculate(run["backtest_run_id"], owner_user_id="user-1")
        self.assertEqual(raised.exception.error_code, "backtest_trade_log_changed")

    def test_snapshot_mutation_invalidates_metric_reconstruction(self):
        run = self._run([10, 11, 12], key="snapshot-tamper")
        snapshot_id = run["config"]["snapshot_ids"][0]
        self.connection.execute(
            "UPDATE financial_data_snapshots SET payload_json='{}' WHERE id=?",
            (snapshot_id,),
        )
        with self.assertRaises(BacktestError) as raised:
            self.analytics.calculate(run["backtest_run_id"], owner_user_id="user-1")
        self.assertEqual(raised.exception.error_code, "snapshot_integrity_failed")

    def test_owner_isolation_and_disabled_gate(self):
        run = self._run([10, 11, 12], key="owner")
        with self.assertRaises(PermissionError):
            self.analytics.calculate(run["backtest_run_id"], owner_user_id="other")
        disabled = FinancialBacktestAnalytics(
            self.database,
            settings={**SETTINGS, "TRADING_SIMULATION_ENABLED": False},
        )
        with self.assertRaises(Exception):
            disabled.calculate(run["backtest_run_id"], owner_user_id="user-1")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM backtest_metrics WHERE backtest_run_id=?",
                (run["backtest_run_id"],),
            ).fetchone()[0], 0
        )

    def test_worker_backtest_returns_metrics_and_full_trade_log(self):
        rows = self._rows([10, 11, 12, 13])
        snapshot = self._snapshot(self.instrument_id, rows, key="worker-data")
        service = FinancialPaperTradingJobService(self.repository, settings=dict(SETTINGS))
        context = FinancialJobContext(
            job_id=88, job_type="paper_backtest", worker_id="fixture",
            repository=self.repository, cancel_event=threading.Event(),
        )
        result = service.run(
            {
                "task_kind": "backtest", "industry_pack_id": "family_office",
                "parameters": {
                    "action": "run", "owner_user_id": "worker-user",
                    "idempotency_key": "worker-metrics", "strategy_key": "buy_and_hold_v1",
                    "strategy_version": "worker-v1", "scope_type": "instrument",
                    "instrument_id": self.instrument_id, "start_date": "2026-01-01",
                    "end_date": "2026-01-04", "initial_capital": 5000,
                    "base_currency": "CNY", "snapshot_ids": [snapshot],
                    "data_cutoff_at": "2026-08-01T00:00:00Z",
                },
            },
            context,
        )
        self.assertIn("analytics", result)
        self.assertEqual(len(result["analytics"]["trades"]), len(result["trades"]))
        self.assertTrue(all("trade_id" in item for item in result["analytics"]["trades"]))
        self.assertIn("max_drawdown", result["analytics"]["metrics"])
        self.assertIn("历史回测仅供研究参考", result["analytics"]["disclaimer"])
        self.assertFalse(result["analytics"]["real_order_execution"])


if __name__ == "__main__":
    unittest.main()

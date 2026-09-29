import hashlib
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from financial_paper_trading import (
    FinancialPaperLedger,
    FinancialPaperTradingJobService,
    PaperTradingError,
)
from financial_worker_jobs import FinancialJobContext
from intel_database import IntelRepository
from sqlite_database import SQLiteDatabase


NOW = datetime(2026, 9, 18, 3, 2, tzinfo=timezone.utc)
SETTINGS = {
    "INTEL_DEFAULT_INDUSTRY_PACK": "family_office",
    "FINANCIAL_INTELLIGENCE_ENABLED": True,
    "TRADING_AGENTS_ENABLED": True,
    "TRADING_SIMULATION_ENABLED": True,
}


class FinancialPaperTradingTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "paper-ledger.sqlite3"))
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
                ) VALUES('paper-fixture', 'Paper Fixture', 'fixture', 'fixture', 1, 'healthy')
                """
            ).lastrowid
        )
        self.instrument_id = self._instrument("600000.SH", "浦发银行", "CNY")
        self.other_instrument_id = self._instrument("0700.HK", "腾讯控股", "HKD")
        self.report_id = self._report(self.instrument_id)
        self.signal_snapshot = self._snapshot(self.instrument_id, 100.0, key="signal")
        self.execution_snapshot = self._snapshot(self.instrument_id, 100.0, key="execution")
        self.ledger = FinancialPaperLedger(
            self.database, settings=dict(SETTINGS), clock=lambda: NOW
        )
        self.account = self.ledger.create_account(
            account_name="人民币纸面账户", base_currency="CNY", initial_cash=10000,
            owner_user_id="user-1", industry_pack_id="family_office",
            idempotency_key="account-1",
        )

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _instrument(self, symbol, name, currency):
        return int(
            self.connection.execute(
                """
                INSERT INTO financial_instruments(
                    canonical_symbol, display_name, asset_type, market,
                    exchange, currency, country_code
                ) VALUES(?, ?, 'equity', ?, ?, ?, ?)
                """,
                (
                    symbol, name, "XHKG" if currency == "HKD" else "XSHG",
                    "XHKG" if currency == "HKD" else "XSHG", currency,
                    "HK" if currency == "HKD" else "CN",
                ),
            ).lastrowid
        )

    def _typed_instrument(self, symbol, name, currency, asset_type):
        return int(
            self.connection.execute(
                """
                INSERT INTO financial_instruments(
                    canonical_symbol, display_name, asset_type, market,
                    exchange, currency, country_code
                ) VALUES(?, ?, ?, 'XSHG', 'XSHG', ?, 'CN')
                """,
                (symbol, name, asset_type, currency),
            ).lastrowid
        )

    def _report(self, instrument_id, *, status="verified"):
        run_id = f"paper-run-{instrument_id}-{status}"
        self.connection.execute(
            """
            INSERT INTO financial_research_runs(
                id, trigger_type, scope_type, instrument_id, status, requested_at
            ) VALUES(?, 'fixture', 'instrument', ?, 'completed', '2026-09-18T03:00:00Z')
            """,
            (run_id, instrument_id),
        )
        return int(
            self.connection.execute(
                """
                INSERT INTO financial_final_reports(
                    research_run_id, report_version, report_status,
                    recommendation, title, report_json, observed_at, fetched_at
                ) VALUES(?, 3, ?, 'hold', '纸面交易信号报告', '{}',
                         '2026-09-18T03:00:00Z', '2026-09-18T03:00:30Z')
                """,
                (run_id, status),
            ).lastrowid
        )

    def _snapshot(
        self, instrument_id, price, *, key, currency=None, market_status="open",
        quality="verified", observed_at="2026-09-18T03:01:00Z",
        stale_after="2026-09-18T03:10:00Z", payload_extra=None,
    ):
        instrument_currency = self.connection.execute(
            "SELECT currency FROM financial_instruments WHERE id=?", (instrument_id,)
        ).fetchone()[0]
        payload = {"last_price": price, **(payload_extra or {})}
        payload_text = json.dumps(payload, sort_keys=True)
        digest = hashlib.sha256(payload_text.encode()).hexdigest()
        return int(
            self.connection.execute(
                """
                INSERT INTO financial_data_snapshots(
                    snapshot_key, instrument_id, provider_profile_id, data_type,
                    observed_at, fetched_at, market_status, currency, timezone,
                    stale_after, quality_status, payload_json, payload_sha256,
                    source_url
                ) VALUES(?, ?, ?, 'quote', ?, ?, ?, ?, 'Asia/Hong_Kong',
                         ?, ?, ?, ?, ?)
                """,
                (
                    f"{key}-{instrument_id}", instrument_id, self.provider_id,
                    observed_at, observed_at, market_status,
                    currency or instrument_currency, stale_after, quality,
                    payload_text, digest, f"https://paper.example/{key}",
                ),
            ).lastrowid
        )

    def _order(self, *, side="buy", order_type="market", quantity=10, key="order-1", **kwargs):
        return self.ledger.submit_order(
            account_id=self.account["account_id"], instrument_id=self.instrument_id,
            side=side, order_type=order_type, quantity=quantity,
            final_report_id=self.report_id, strategy_version="strategy-v7",
            signal_snapshot_id=self.signal_snapshot, owner_user_id="user-1",
            idempotency_key=key, **kwargs,
        )

    def _fill(self, order, *, key="fill-1", snapshot=None, **kwargs):
        return self.ledger.fill_order(
            order["order_id"], execution_snapshot_id=snapshot or self.execution_snapshot,
            owner_user_id="user-1", idempotency_key=key, filled_at=NOW, **kwargs,
        )

    def test_account_is_idempotent_owner_scoped_and_capability_gated(self):
        duplicate = self.ledger.create_account(
            account_name="人民币纸面账户", base_currency="CNY", initial_cash=10000,
            owner_user_id="user-1", industry_pack_id="family_office",
            idempotency_key="account-1",
        )
        self.assertFalse(duplicate["created"])
        with self.assertRaises(PermissionError):
            self.ledger.account_statement(self.account["account_id"], owner_user_id="user-2")
        disabled = FinancialPaperLedger(
            self.database,
            settings={**SETTINGS, "TRADING_SIMULATION_ENABLED": False},
            clock=lambda: NOW,
        )
        with self.assertRaises(PermissionError):
            disabled.create_account(
                account_name="disabled", base_currency="CNY", initial_cash=1,
                owner_user_id="user-1", industry_pack_id="family_office",
            )

    def test_order_requires_report_strategy_and_signal_snapshot_lineage(self):
        order = self._order()
        self.assertTrue(order["created"])
        self.assertEqual(order["final_report_id"], self.report_id)
        self.assertEqual(order["metadata"]["report_version"], 3)
        self.assertEqual(order["metadata"]["strategy_version"], "strategy-v7")
        self.assertEqual(order["metadata"]["signal_snapshot_id"], self.signal_snapshot)
        duplicate = self._order()
        self.assertFalse(duplicate["created"])
        self.assertEqual(duplicate["order_id"], order["order_id"])
        with self.assertRaises(PaperTradingError) as raised:
            self.ledger.submit_order(
                account_id=self.account["account_id"], instrument_id=self.instrument_id,
                side="buy", order_type="market", quantity=1,
                final_report_id=self.report_id, strategy_version="",
                signal_snapshot_id=self.signal_snapshot, owner_user_id="user-1",
                idempotency_key="bad-lineage",
            )
        self.assertEqual(raised.exception.error_code, "strategy_version_required")

    def test_partial_buy_fills_update_cash_position_and_conserve_ledger(self):
        order = self._order(quantity=10)
        first = self._fill(order, quantity=4, key="partial-1", fee_rate="0.001", slippage_bps="10")
        self.assertEqual(first["status"], "partial")
        second = self._fill(order, quantity=6, key="partial-2", fee_rate="0.001", slippage_bps="10")
        self.assertEqual(second["status"], "completed")
        self.assertAlmostEqual(second["position_quantity"], 10)
        statement = self.ledger.account_statement(
            self.account["account_id"], owner_user_id="user-1"
        )
        self.assertTrue(statement["ledger_conserved"])
        self.assertAlmostEqual(statement["cash_flow"]["conservation_delta"], 0)
        self.assertEqual(statement["fill_count"], 2)
        self.assertGreater(statement["cash_flow"]["fees"], 0)
        self.assertGreater(statement["positions"][0]["average_cost"], 100.1)

    def test_insufficient_cash_and_position_roll_back_atomically(self):
        buy = self._order(quantity=1000, key="too-large")
        with self.assertRaises(PaperTradingError) as cash_error:
            self._fill(buy, key="too-large-fill")
        self.assertEqual(cash_error.exception.error_code, "insufficient_cash")
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0], 0
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT cash_balance FROM paper_accounts WHERE id=?",
                (self.account["account_id"],),
            ).fetchone()[0],
            10000,
        )
        sell = self._order(side="sell", quantity=1, key="naked-sell")
        with self.assertRaises(PaperTradingError) as position_error:
            self._fill(sell, key="naked-sell-fill")
        self.assertEqual(position_error.exception.error_code, "insufficient_position")

    def test_sell_updates_realized_pnl_and_cash(self):
        buy = self._order(quantity=10, key="buy-before-sell")
        self._fill(buy, key="buy-fill", fee_rate=0, slippage_bps=0)
        higher = self._snapshot(self.instrument_id, 110, key="higher")
        sell = self._order(side="sell", quantity=4, key="sell-profit")
        result = self._fill(sell, key="sell-fill", snapshot=higher, fee_rate="0.001")
        self.assertEqual(result["position_quantity"], 6)
        statement = self.ledger.account_statement(
            self.account["account_id"], owner_user_id="user-1"
        )
        self.assertTrue(statement["ledger_conserved"])
        self.assertGreater(statement["positions"][0]["realized_pnl"], 39)

    def test_limit_stop_price_limit_and_slippage_rules(self):
        limit = self._order(order_type="limit", limit_price=99, key="limit-order")
        pending = self._fill(limit, key="limit-pending")
        self.assertFalse(pending["filled"])
        self.assertEqual(pending["reason"], "limit_not_reached")
        stop = self._order(order_type="stop", stop_price=101, key="stop-order")
        not_triggered = self._fill(stop, key="stop-pending")
        self.assertEqual(not_triggered["reason"], "stop_not_triggered")
        at_limit = self._snapshot(
            self.instrument_id, 110, key="at-limit",
            payload_extra={"price_limit_up": 110},
        )
        blocked = self._fill(self._order(key="limit-up-buy"), key="limit-up-fill", snapshot=at_limit)
        self.assertEqual(blocked["reason"], "buy_blocked_at_price_limit_up")
        tight = self._order(order_type="limit", limit_price=100.05, key="slippage-limit")
        slipped = self._fill(tight, key="slippage-limit-fill", slippage_bps=10)
        self.assertEqual(slipped["reason"], "slippage_exceeds_limit")

    def test_suspension_future_stale_and_unverified_snapshots_cannot_fill(self):
        cases = [
            (self._snapshot(self.instrument_id, 100, key="suspended", market_status="suspended"), "instrument_suspended"),
            (self._snapshot(self.instrument_id, 100, key="future", observed_at="2026-09-18T03:03:00Z"), "future_snapshot"),
            (self._snapshot(self.instrument_id, 100, key="stale", stale_after="2026-09-18T03:01:59Z"), "stale_snapshot"),
            (self._snapshot(self.instrument_id, 100, key="unverified", quality="unverified"), "snapshot_not_verified"),
        ]
        for index, (snapshot, error_code) in enumerate(cases):
            with self.subTest(error_code=error_code):
                order = self._order(key=f"blocked-{index}")
                with self.assertRaises(PaperTradingError) as raised:
                    self._fill(order, key=f"blocked-fill-{index}", snapshot=snapshot)
                self.assertEqual(raised.exception.error_code, error_code)

        tampered = self._snapshot(self.instrument_id, 100, key="tampered")
        self.connection.execute(
            "UPDATE financial_data_snapshots SET payload_json='{}' WHERE id=?",
            (tampered,),
        )
        with self.assertRaises(PaperTradingError) as integrity_error:
            self._fill(self._order(key="tampered-order"), key="tampered-fill", snapshot=tampered)
        self.assertEqual(integrity_error.exception.error_code, "snapshot_integrity_failed")

    def test_currency_report_and_snapshot_scope_never_cross(self):
        with self.assertRaises(PaperTradingError) as currency_error:
            self.ledger.submit_order(
                account_id=self.account["account_id"], instrument_id=self.other_instrument_id,
                side="buy", order_type="market", quantity=1,
                final_report_id=self.report_id, strategy_version="v1",
                signal_snapshot_id=self.signal_snapshot, owner_user_id="user-1",
                idempotency_key="currency-cross",
            )
        self.assertEqual(currency_error.exception.error_code, "currency_mismatch")
        other_report = self._report(self.other_instrument_id)
        with self.assertRaises(PaperTradingError) as report_error:
            self.ledger.submit_order(
                account_id=self.account["account_id"], instrument_id=self.instrument_id,
                side="buy", order_type="market", quantity=1,
                final_report_id=other_report, strategy_version="v1",
                signal_snapshot_id=self.signal_snapshot, owner_user_id="user-1",
                idempotency_key="report-cross",
            )
        self.assertEqual(report_error.exception.error_code, "report_instrument_mismatch")

    def test_index_order_requires_user_confirmed_tradable_proxy(self):
        index_id = self._typed_instrument("000300.SH", "沪深300", "CNY", "index")
        etf_id = self._typed_instrument("510300.SH", "沪深300ETF", "CNY", "etf")
        index_report = self._report(index_id)
        index_signal = self._snapshot(index_id, 4688, key="index-signal")
        with self.assertRaises(PaperTradingError) as direct:
            self.ledger.submit_order(
                account_id=self.account["account_id"], instrument_id=index_id,
                side="buy", order_type="market", quantity=1,
                final_report_id=index_report, strategy_version="index-v1",
                signal_snapshot_id=index_signal, owner_user_id="user-1",
                idempotency_key="direct-index",
            )
        self.assertEqual(direct.exception.error_code, "index_requires_tradable_proxy")
        with self.assertRaises(PaperTradingError) as unconfirmed:
            self.ledger.submit_order(
                account_id=self.account["account_id"], instrument_id=etf_id,
                research_instrument_id=index_id, index_proxy_confirmed=False,
                side="buy", order_type="market", quantity=1,
                final_report_id=index_report, strategy_version="index-v1",
                signal_snapshot_id=index_signal, owner_user_id="user-1",
                idempotency_key="unconfirmed-index",
            )
        self.assertEqual(
            unconfirmed.exception.error_code, "index_proxy_confirmation_required"
        )
        accepted = self.ledger.submit_order(
            account_id=self.account["account_id"], instrument_id=etf_id,
            research_instrument_id=index_id, index_proxy_confirmed=True,
            side="buy", order_type="market", quantity=1,
            final_report_id=index_report, strategy_version="index-v1",
            signal_snapshot_id=index_signal, owner_user_id="user-1",
            idempotency_key="confirmed-index",
        )
        self.assertEqual(accepted["metadata"]["research_instrument_id"], index_id)
        self.assertEqual(accepted["metadata"]["tradable_proxy_instrument_id"], etf_id)
        self.assertTrue(accepted["metadata"]["index_proxy_confirmed"])

    def test_cancel_is_idempotent_and_completed_order_cannot_cancel(self):
        pending = self._order(key="cancel-pending")
        cancelled = self.ledger.cancel_order(pending["order_id"], owner_user_id="user-1")
        duplicate = self.ledger.cancel_order(pending["order_id"], owner_user_id="user-1")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(duplicate["idempotent"])
        completed = self._order(key="completed-order")
        self._fill(completed, key="completed-fill")
        with self.assertRaises(PaperTradingError) as raised:
            self.ledger.cancel_order(completed["order_id"], owner_user_id="user-1")
        self.assertEqual(raised.exception.error_code, "order_not_cancellable")

    def test_fill_idempotency_prevents_double_spend(self):
        order = self._order(quantity=2, key="idempotent-order")
        first = self._fill(order, key="same-fill")
        second = self._fill(order, key="same-fill")
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0], 1
        )

    def test_existing_worker_runner_creates_paper_account_without_new_service(self):
        service = FinancialPaperTradingJobService(self.repository, settings=dict(SETTINGS))
        self.assertEqual(set(service.runners()), {"paper_backtest"})
        context = FinancialJobContext(
            job_id=42, job_type="paper_backtest", worker_id="fixture",
            repository=self.repository, cancel_event=threading.Event(),
        )
        result = service.run(
            {
                "task_kind": "paper_trade", "industry_pack_id": "family_office",
                "parameters": {
                    "action": "create_account", "account_name": "worker account",
                    "base_currency": "CNY", "initial_cash": 2000,
                    "owner_user_id": "user-worker", "idempotency_key": "worker-1",
                },
            },
            context,
        )
        self.assertTrue(result["created"])
        self.assertEqual(result["execution_mode"], "paper")


if __name__ == "__main__":
    unittest.main()

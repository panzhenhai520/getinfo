import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import config
import intel_api
from financial_simulation_view import FinancialSimulationView
from intel_api import intel_bp
from intel_database import IntelRepository
from sqlite_database import SQLiteDatabase
from tools.check_financial_simulation_view import inspect_frontend_contract


class FinancialSimulationViewTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "simulation-view.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.connection = self.database.connection
        self.repository = IntelRepository(self.database)
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True)
        self.app.register_blueprint(intel_bp)
        self.provider_id = int(
            self.connection.execute(
                """
                INSERT INTO financial_provider_profiles(
                    provider_key, display_name, provider_type, access_tier,
                    is_enabled, health_status
                ) VALUES('simulation-view-fixture', 'Simulation View Fixture',
                         'fixture', 'fixture', 1, 'healthy')
                """
            ).lastrowid
        )
        self.instrument_id = int(
            self.connection.execute(
                """
                INSERT INTO financial_instruments(
                    canonical_symbol, display_name, asset_type, market,
                    exchange, currency, country_code
                ) VALUES('600000.SH', '浦发银行', 'equity', 'XSHG', 'XSHG', 'CNY', 'CN')
                """
            ).lastrowid
        )
        self.report_id = self._report()
        self.snapshot_id = self._snapshot()
        self.account_id = self._account("owner-1", "主纸面账户")
        self._account("owner-2", "其他用户账户")
        self._paper_history()
        self.run_id = self._backtest("owner-1", "owner-run", trade_count=145)
        self._backtest("owner-2", "other-run", trade_count=1)
        self.connection.commit()

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _report(self):
        self.connection.execute(
            """
            INSERT INTO financial_research_runs(
                id, trigger_type, scope_type, instrument_id, status, requested_at
            ) VALUES('view-report-run', 'fixture', 'instrument', ?, 'completed',
                     '2026-07-01T00:00:00Z')
            """,
            (self.instrument_id,),
        )
        return int(
            self.connection.execute(
                """
                INSERT INTO financial_final_reports(
                    research_run_id, report_version, report_status,
                    recommendation, title, report_json, observed_at, fetched_at
                ) VALUES('view-report-run', 1, 'verified', 'hold',
                         '模拟来源报告', '{}', '2026-07-01T00:00:00Z',
                         '2026-07-01T00:01:00Z')
                """
            ).lastrowid
        )

    def _snapshot(self):
        return int(
            self.connection.execute(
                """
                INSERT INTO financial_data_snapshots(
                    snapshot_key, instrument_id, provider_profile_id, data_type, observed_at, fetched_at,
                    market_status, currency, quality_status, payload_json,
                    payload_sha256, source_url
                ) VALUES('view-snapshot', ?, ?, 'quote', '2026-07-01T00:00:00Z',
                         '2026-07-01T00:00:10Z', 'open', 'CNY', 'verified',
                         '{"last_price":10}', 'fixture-sha',
                         'https://evidence.example/snapshot')
                """,
                (self.instrument_id, self.provider_id),
            ).lastrowid
        )

    def _account(self, owner, name):
        account_id = f"account-{owner}"
        self.connection.execute(
            """
            INSERT INTO paper_accounts(
                id, account_name, base_currency, initial_cash, cash_balance,
                config_json, created_at, updated_at
            ) VALUES(?, ?, 'CNY', 10000, 8999, ?,
                     '2026-07-01T00:00:00Z', '2026-07-02T00:00:00Z')
            """,
            (account_id, name, json.dumps({"owner_user_id": owner})),
        )
        return account_id

    def _paper_history(self):
        self.connection.execute(
            """
            INSERT INTO paper_positions(
                account_id, instrument_id, quantity, average_cost,
                realized_pnl, last_price, market_value, as_of
            ) VALUES(?, ?, 100, 10.01, 0, 10, 1000, '2026-07-01T00:00:00Z')
            """,
            (self.account_id, self.instrument_id),
        )
        self.connection.execute(
            """
            INSERT INTO paper_orders(
                id, account_id, instrument_id, research_run_id,
                final_report_id, side, order_type, quantity, status,
                submitted_at, completed_at, metadata_json
            ) VALUES('paper-order-1', ?, ?, 'view-report-run', ?, 'buy',
                     'market', 100, 'completed', '2026-07-01T00:00:00Z',
                     '2026-07-01T00:00:10Z', '{"strategy_version":"ui-v1"}')
            """,
            (self.account_id, self.instrument_id, self.report_id),
        )
        self.connection.execute(
            """
            INSERT INTO paper_fills(
                id, order_id, quantity, price, fee, currency, snapshot_id, filled_at
            ) VALUES('paper-fill-1', 'paper-order-1', 100, 10, 1, 'CNY', ?,
                     '2026-07-01T00:00:10Z')
            """,
            (self.snapshot_id,),
        )

    def _backtest(self, owner, run_id, *, trade_count):
        config_value = {
            "owner_user_id": owner, "industry_pack_id": "family_office",
            "base_currency": "CNY", "data_version": f"data-{run_id}",
            "strategy_parameters": {"final_report_id": self.report_id},
            "coverage": {
                "coverage_ratio": 0.8,
                "limitations": ["historical_calendar_coverage_below_full"],
            },
            "snapshot_manifest": [
                {
                    "snapshot_id": self.snapshot_id,
                    "payload_sha256": "fixture-sha",
                    "observed_at": "2026-07-01T00:00:00Z",
                }
            ],
        }
        self.connection.execute(
            """
            INSERT INTO backtest_runs(
                id, strategy_key, model_version, scope_type, instrument_id,
                start_date, end_date, initial_capital, config_json, status,
                data_cutoff_at, created_at, started_at, completed_at
            ) VALUES(?, 'buy_and_hold_v1', 'ui-v1', 'instrument', ?,
                     '2026-01-01', '2026-07-01', 10000, ?, 'completed',
                     '2026-07-02T00:00:00Z', '2026-07-02T00:00:00Z',
                     '2026-07-02T00:00:01Z', '2026-07-02T00:00:02Z')
            """,
            (run_id, self.instrument_id, json.dumps(config_value)),
        )
        curve = [
            {"date": f"2026-01-{(index % 28) + 1:02d}", "equity": 10000 + index}
            for index in range(180)
        ]
        metrics = (
            ("total_return", 0.12, "", "ratio", {"status": "available"}),
            ("max_drawdown", -0.08, "", "ratio", {"status": "available"}),
            ("data_coverage_ratio", 0.8, "", "ratio", {
                "status": "available", "limitations": ["historical_calendar_coverage_below_full"]
            }),
            ("equity_curve", None, json.dumps(curve), "json", {"status": "available"}),
        )
        for key, value, text, unit, metadata in metrics:
            self.connection.execute(
                """
                INSERT INTO backtest_metrics(
                    backtest_run_id, metric_key, metric_value,
                    metric_text, unit, metadata_json
                ) VALUES(?,?,?,?,?,?)
                """,
                (run_id, key, value, text, unit, json.dumps(metadata)),
            )
        for index in range(trade_count):
            self.connection.execute(
                """
                INSERT INTO backtest_trades(
                    backtest_run_id, instrument_id, side, quantity,
                    price, fee, signal_at, executed_at, reason_json
                ) VALUES(?, ?, ?, 1, ?, 0.01, ?, ?, ?)
                """,
                (
                    run_id, self.instrument_id, "buy" if index % 2 == 0 else "sell",
                    10 + index / 100,
                    "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z",
                    json.dumps({"snapshot_id": self.snapshot_id, "signal": "fixture"}),
                ),
            )
        return run_id

    @contextmanager
    def _client(self, *, user="owner-1", simulation=True):
        with (
            patch.object(intel_api, "intel_repository", self.repository),
            patch(
                "decorators.user_db.verify_session",
                return_value={"user_id": user, "username": user, "role": "user"},
            ),
            patch.object(config, "FINANCIAL_INTELLIGENCE_ENABLED", True),
            patch.object(config, "TRADING_SIMULATION_ENABLED", simulation),
        ):
            yield self.app.test_client()

    def test_owner_projection_contains_paper_labels_report_and_evidence_links(self):
        self.connection.executemany(
            """
            INSERT INTO backtest_runs(
                id, strategy_key, model_version, scope_type, instrument_id,
                start_date, end_date, initial_capital, config_json, status,
                data_cutoff_at, created_at
            ) VALUES(?, 'fixture', 'v1', 'instrument', ?, '2026-01-01',
                     '2026-01-02', 10000, ?, 'completed',
                     '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z')
            """,
            [
                (
                    f"newer-other-run-{index}", self.instrument_id,
                    json.dumps({"owner_user_id": "owner-2"}),
                )
                for index in range(101)
            ],
        )
        self.connection.commit()
        result = FinancialSimulationView(self.database, settings={
            "FINANCIAL_INTELLIGENCE_ENABLED": True,
            "TRADING_SIMULATION_ENABLED": True,
            "INTEL_DEFAULT_INDUSTRY_PACK": "family_office",
        }).build(
            owner_user_id="owner-1", industry_pack_id="family_office",
            mode="simulation", report_id=self.report_id, trade_limit=20,
        )
        self.assertTrue(result["visible"])
        self.assertTrue(result["can_create"])
        self.assertEqual(result["execution_mode"], "paper")
        self.assertFalse(result["real_order_execution"])
        self.assertEqual(result["counts"], {"accounts": 1, "backtests": 1})
        account = result["accounts"][0]
        self.assertEqual(account["account_name"], "主纸面账户")
        self.assertEqual(account["equity"], 9999)
        self.assertEqual(account["orders"][0]["final_report_id"], self.report_id)
        self.assertIn(str(self.snapshot_id), account["fills"][0]["evidence_url"])
        backtest = result["backtests"][0]
        self.assertEqual(backtest["source_report_id"], self.report_id)
        self.assertEqual(backtest["trade_count"], 145)
        self.assertEqual(len(backtest["trades"]), 20)
        self.assertTrue(backtest["trades_truncated"])
        self.assertEqual(len(result["related"]["paper_orders"]), 1)
        self.assertEqual(len(result["related"]["backtests"]), 1)
        self.assertNotIn("其他用户账户", json.dumps(result, ensure_ascii=False))

    def test_switch_off_preserves_owner_history_but_disables_creation(self):
        with self._client(simulation=False) as client:
            response = client.get(
                "/api/intel/financial/simulation/overview",
                query_string={"industry_pack_id": "family_office", "mode": "backtesting"},
                headers={"Authorization": "Bearer fixture"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload["can_create"])
        self.assertTrue(payload["visible"])
        self.assertEqual(payload["counts"]["accounts"], 1)
        self.assertEqual(payload["counts"]["backtests"], 1)

    def test_dashboard_collapse_preference_namespace_is_stable_per_user(self):
        with self._client(user="owner-1") as client:
            first = client.get(
                "/api/intel/dashboard?industry_pack_id=family_office&time_range=7d",
                headers={"Authorization": "Bearer fixture"},
            ).get_json()["dashboard_preference_namespace"]
            second = client.get(
                "/api/intel/dashboard?industry_pack_id=family_office&time_range=7d",
                headers={"Authorization": "Bearer fixture"},
            ).get_json()["dashboard_preference_namespace"]
        with self._client(user="owner-2") as client:
            other = client.get(
                "/api/intel/dashboard?industry_pack_id=family_office&time_range=7d",
                headers={"Authorization": "Bearer fixture"},
            ).get_json()["dashboard_preference_namespace"]
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertNotIn("owner-1", first)

    def test_empty_owner_has_no_history_and_cannot_see_other_users(self):
        with self._client(user="empty-owner", simulation=False) as client:
            response = client.get(
                "/api/intel/financial/simulation/overview?industry_pack_id=family_office",
                headers={"Authorization": "Bearer fixture"},
            )
        payload = response.get_json()
        self.assertFalse(payload["visible"])
        self.assertEqual(payload["accounts"], [])
        self.assertEqual(payload["backtests"], [])

    def test_long_backtest_page_is_bounded_and_export_is_complete(self):
        with self._client() as client:
            page = client.get(
                "/api/intel/financial/simulation/overview",
                query_string={
                    "industry_pack_id": "family_office", "mode": "backtesting",
                    "trade_limit": 25,
                },
                headers={"Authorization": "Bearer fixture"},
            )
            exported = client.get(
                "/api/intel/financial/simulation/export",
                query_string={"kind": "backtest", "id": self.run_id},
                headers={"Authorization": "Bearer fixture"},
            )
        self.assertEqual(page.status_code, 200)
        self.assertEqual(len(page.get_json()["backtests"][0]["trades"]), 25)
        self.assertEqual(exported.status_code, 200)
        self.assertIn("attachment", exported.headers["Content-Disposition"])
        exported_run = exported.get_json()["exported_item"]
        self.assertEqual(exported_run["trade_count"], 145)
        self.assertEqual(len(exported_run["trades"]), 145)
        self.assertEqual(len(exported_run["equity_curve"]), 180)

    def test_long_account_history_is_bounded_on_page_and_complete_in_export(self):
        for index in range(105):
            order_id = f"paper-order-extra-{index}"
            self.connection.execute(
                """
                INSERT INTO paper_orders(
                    id, account_id, instrument_id, research_run_id,
                    final_report_id, side, order_type, quantity, status,
                    submitted_at, completed_at, metadata_json
                ) VALUES(?, ?, ?, 'view-report-run', ?, 'buy', 'market', 1,
                         'completed', '2026-07-03T00:00:00Z',
                         '2026-07-03T00:00:01Z', '{}')
                """,
                (order_id, self.account_id, self.instrument_id, self.report_id),
            )
            self.connection.execute(
                """
                INSERT INTO paper_fills(
                    id, order_id, quantity, price, fee, currency,
                    snapshot_id, filled_at
                ) VALUES(?, ?, 1, 10, 0.01, 'CNY', ?, '2026-07-03T00:00:01Z')
                """,
                (f"paper-fill-extra-{index}", order_id, self.snapshot_id),
            )
        self.connection.commit()
        view = FinancialSimulationView(self.database, settings={
            "FINANCIAL_INTELLIGENCE_ENABLED": True,
            "TRADING_SIMULATION_ENABLED": True,
            "INTEL_DEFAULT_INDUSTRY_PACK": "family_office",
        })
        page = view.build(
            owner_user_id="owner-1", industry_pack_id="family_office",
            mode="simulation",
        )["accounts"][0]
        exported = view.export(
            owner_user_id="owner-1", kind="account", item_id=self.account_id,
        )["exported_item"]
        self.assertEqual(page["order_count"], 106)
        self.assertEqual(len(page["orders"]), 100)
        self.assertTrue(page["orders_truncated"])
        self.assertEqual(page["fill_count"], 106)
        self.assertEqual(len(exported["orders"]), 106)
        self.assertEqual(len(exported["fills"]), 106)
        self.assertFalse(exported["orders_truncated"])
        self.assertFalse(exported["fills_truncated"])

    def test_authentication_validation_and_export_permission_fail_closed(self):
        unauthenticated = self.app.test_client().get(
            "/api/intel/financial/simulation/overview?industry_pack_id=family_office"
        )
        self.assertEqual(unauthenticated.status_code, 401)
        with self._client(user="owner-2") as client:
            invalid_mode = client.get(
                "/api/intel/financial/simulation/overview",
                query_string={"industry_pack_id": "family_office", "mode": "live"},
                headers={"Authorization": "Bearer fixture"},
            )
            forbidden = client.get(
                "/api/intel/financial/simulation/export",
                query_string={"kind": "backtest", "id": self.run_id},
                headers={"Authorization": "Bearer fixture"},
            )
        self.assertEqual(invalid_mode.status_code, 400)
        self.assertEqual(forbidden.status_code, 403)
        with self.assertRaises(PermissionError):
            FinancialSimulationView(self.database).export(
                owner_user_id="", kind="backtest", item_id=self.run_id,
            )

    def test_frontend_contract_is_safe_accessible_interruptible_and_responsive(self):
        frontend = inspect_frontend_contract()
        self.assertTrue(frontend["safe"], frontend)
        self.assertEqual(frontend["unsafe_markers"], [])
        self.assertIn("financial-simulation", frontend["category_ids"])


if __name__ == "__main__":
    unittest.main()

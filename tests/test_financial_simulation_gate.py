import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import config
import intel_api
from intel_api import intel_bp
from intel_database import IntelRepository
from sqlite_database import SQLiteDatabase


class FinancialSimulationGateTest(unittest.TestCase):
    def setUp(self):
        # conftest 的 DATABASE_TYPE=sqlite 会被 .env 覆盖（config 里仍是 postgres），
        # 而 SQLiteDatabase(path) 只改路径不改后端：不强制切 sqlite，本文件入队的
        # 作业会真的写进共享主库 intel_jobs。
        for item in (
            patch.object(config, "DATABASE_TYPE", "sqlite"),
            patch("db_connection.database_type", lambda: "sqlite"),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "simulation-gate.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.repository = IntelRepository(self.database)
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True)
        self.app.register_blueprint(intel_bp)

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    @contextmanager
    def _context(self, *, financial=True, simulation=True, user_id=8):
        with (
            patch("intel_api.intel_repository", self.repository),
            patch(
                "decorators.user_db.verify_session",
                return_value={"user_id": user_id, "username": f"user-{user_id}", "role": "user"},
            ),
            patch.object(config, "FINANCIAL_INTELLIGENCE_ENABLED", financial),
            patch.object(config, "TRADING_SIMULATION_ENABLED", simulation),
            # 生产 .env 默认 FINANCIAL_ROLLOUT_STAGE=off（fail-closed），
            # 本用例只验证开关与行业包闸门，因此把灰度阶段放到最高级，
            # 避免灰度闸门提前短路成 rollout_stage_*_not_reached。
            patch.object(config, "FINANCIAL_ROLLOUT_STAGE", "simulation_backtest"),
        ):
            yield self.app.test_client()

    def _post(self, client, *, kind="backtest", pack="family_office", headers=None, extra=None):
        payload = {
            "task_kind": kind,
            "industry_pack_id": pack,
            "parameters": {"symbol": "0700.HK"},
        }
        payload.update(extra or {})
        return client.post(
            "/api/intel/financial/simulation/jobs",
            headers={"Authorization": "Bearer fixture", **(headers or {})},
            json=payload,
        )

    def test_direct_post_is_denied_when_simulation_switch_is_off_despite_client_spoof(self):
        with self._context(financial=True, simulation=False) as client:
            response = self._post(
                client,
                extra={"simulation_enabled": True, "TRADING_SIMULATION_ENABLED": True},
            )
        self.assertEqual(response.status_code, 403)
        payload = response.get_json()
        self.assertEqual(payload["capability"], "backtesting")
        self.assertEqual(payload["reason"], "trading_simulation_enabled_disabled")
        self.assertEqual(
            self.database.connection.execute("SELECT COUNT(*) FROM intel_jobs").fetchone()[0],
            0,
        )

    def test_parent_and_industry_pack_gates_fail_closed(self):
        with self._context(financial=False, simulation=True) as client:
            parent = self._post(client)
        self.assertEqual(parent.status_code, 403)
        self.assertEqual(parent.get_json()["reason"], "financial_intelligence_disabled")
        with self._context(financial=True, simulation=True) as client:
            pack = self._post(client, pack="ai_news")
        self.assertEqual(pack.status_code, 403)
        self.assertEqual(
            pack.get_json()["reason"],
            "financial_products_hidden_for_primary_pack",
        )

    def test_enabled_switch_creates_only_existing_paper_worker_job(self):
        with self._context() as client:
            response = self._post(
                client,
                headers={"Idempotency-Key": "fixture-backtest-1"},
            )
        self.assertEqual(response.status_code, 202, response.get_json())
        result = response.get_json()
        self.assertTrue(result["created"])
        self.assertEqual(result["execution_mode"], "paper")
        self.assertFalse(result["real_order_execution"])
        job = self.repository.get_job(result["job_id"])
        self.assertEqual(job["job_type"], "paper_backtest")
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["created_by"], "8")
        self.assertEqual(job["payload"]["task_kind"], "backtest")
        self.assertEqual(job["payload"]["parameters"]["owner_user_id"], "8")
        self.assertEqual(job["payload"]["execution_mode"], "paper")
        self.assertFalse(job["payload"]["real_order_execution"])

    def test_paper_trade_and_backtest_use_same_authoritative_switch(self):
        with self._context() as client:
            paper = self._post(
                client, kind="paper_trade",
                headers={"Idempotency-Key": "fixture-paper-1"},
            )
            backtest = self._post(
                client, kind="backtest",
                headers={"Idempotency-Key": "fixture-backtest-2"},
            )
        self.assertEqual(paper.status_code, 202)
        self.assertEqual(backtest.status_code, 202)
        self.assertNotEqual(paper.get_json()["job_id"], backtest.get_json()["job_id"])

    def test_idempotency_and_read_only_history_survive_switch_close(self):
        headers = {"Idempotency-Key": "stable-request-1"}
        with self._context() as client:
            first = self._post(client, headers=headers)
            duplicate = self._post(client, headers=headers)
        self.assertEqual(first.get_json()["job_id"], duplicate.get_json()["job_id"])
        self.assertTrue(first.get_json()["created"])
        self.assertFalse(duplicate.get_json()["created"])
        job_id = first.get_json()["job_id"]
        with self._context(simulation=False) as client:
            history = client.get(
                f"/api/intel/jobs/{job_id}",
                headers={"Authorization": "Bearer fixture"},
            )
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.get_json()["job"]["id"], job_id)

    def test_job_history_remains_owner_scoped(self):
        with self._context(user_id=8) as client:
            created = self._post(
                client, headers={"Idempotency-Key": "owner-scope-1"}
            ).get_json()
        with self._context(simulation=False, user_id=9) as client:
            forbidden = client.get(
                f"/api/intel/jobs/{created['job_id']}",
                headers={"Authorization": "Bearer fixture"},
            )
        self.assertEqual(forbidden.status_code, 403)

    def test_request_contract_is_bounded(self):
        with self._context() as client:
            unknown = self._post(client, kind="live_trade")
            bad_parameters = self._post(
                client, extra={"parameters": ["not", "an", "object"]}
            )
            bad_key = self._post(client, headers={"Idempotency-Key": "bad key"})
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(bad_parameters.status_code, 400)
        self.assertEqual(bad_key.status_code, 400)
        self.assertEqual(
            self.database.connection.execute("SELECT COUNT(*) FROM intel_jobs").fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()

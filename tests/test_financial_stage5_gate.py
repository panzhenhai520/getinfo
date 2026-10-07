import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config

# conftest 的 DATABASE_TYPE=sqlite 会被 .env 覆盖（config 里仍是 postgres），
# 而 tools.check_financial_stage5_gate 里的 SQLiteDatabase(path) 只改路径不改
# 后端，会把黄金用例的建表/回测/纸面账本真的写进共享主库；这里强制回到
# 各用例自己声明的临时 SQLite 文件。必须在导入该工具模块之前改。
config.DATABASE_TYPE = "sqlite"
patch("db_connection.database_type", lambda: "sqlite").start()

from tools.check_financial_stage5_gate import (  # noqa: E402
    FIXTURE,
    evaluate_stage5_fixture,
    load_golden_fixture,
    resume_golden_database,
    run_golden_once,
    static_acceptance,
)


class FinancialStage5GateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = load_golden_fixture(FIXTURE)
        cls.result = evaluate_stage5_fixture(FIXTURE)

    def test_fixture_is_fixed_correctness_only_and_runs_three_times(self):
        policy = self.fixture["dataset_policy"]
        self.assertEqual(policy["repetitions"], 3)
        self.assertEqual(policy["profitability_evaluation"], "prohibited")
        self.assertEqual(policy["purpose"], "deterministic_correctness_and_isolation_only")

    def test_fixed_strategy_matches_golden_three_times(self):
        self.assertEqual(self.result["status"], "passed")
        self.assertEqual(len(set(self.result["repetition_hashes"])), 1)
        self.assertEqual(
            self.result["projection_sha256"],
            self.fixture["expected"]["projection_sha256"],
        )
        self.assertTrue(self.result["checks"]["three_independent_runs"])
        self.assertTrue(self.result["checks"]["projections_identical"])

    def test_backtest_is_point_in_time_paper_and_recomputable(self):
        backtest = self.result["golden_projection"]["backtest"]
        self.assertEqual(len(backtest["trades"]), 2)
        self.assertEqual([item["side"] for item in backtest["trades"]], ["buy", "sell"])
        self.assertEqual(backtest["trades"][0]["execution_lag_bars"], 1)
        self.assertLessEqual(
            backtest["trades"][0]["signal_at"], backtest["trades"][0]["executed_at"]
        )
        self.assertEqual(backtest["execution_mode"], "paper")
        self.assertFalse(backtest["real_order_execution"])
        self.assertEqual(len(backtest["trade_log_sha256"]), 64)
        self.assertEqual(len(backtest["input_hash"]), 64)

    def test_paper_ledger_is_conserved_and_has_no_real_execution(self):
        ledger = self.result["golden_projection"]["paper_ledger"]
        self.assertTrue(ledger["ledger_conserved"])
        self.assertEqual(ledger["cash_flow"]["conservation_delta"], 0)
        self.assertEqual(ledger["fill_count"], 2)
        self.assertEqual(ledger["positions"][0]["quantity"], 6)
        self.assertEqual(ledger["execution_mode"], "paper")
        self.assertFalse(ledger["real_order_execution"])

    def test_restart_preserves_results_idempotency_and_read_only_history(self):
        restart = self.result["restart"]
        self.assertTrue(restart["executed"])
        self.assertTrue(restart["projection_preserved"])
        self.assertTrue(restart["backtest_retry_idempotent"])
        self.assertTrue(restart["analytics_retry_idempotent"])
        self.assertTrue(restart["paper_account_retry_idempotent"])
        self.assertTrue(restart["paper_orders_retry_idempotent"])
        self.assertTrue(restart["paper_fills_retry_idempotent"])
        self.assertTrue(restart["paper_history_visible_with_switch_off"])
        self.assertTrue(restart["creation_disabled_after_restart"])
        self.assertEqual(restart["owner_account_count"], 1)
        self.assertEqual(restart["owner_backtest_count"], 1)

    def test_persistent_database_resumes_across_process_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "persistent.sqlite3"
            seeded = run_golden_once(database_path, self.fixture, restart=False)
            resumed = resume_golden_database(database_path, self.fixture)
        self.assertEqual(seeded["projection_sha256"], resumed["projection_sha256"])
        self.assertEqual(
            resumed["projection_sha256"],
            self.fixture["expected"]["projection_sha256"],
        )
        self.assertTrue(all(resumed["checks"].values()))
        self.assertEqual(resumed["owner_account_count"], 1)
        self.assertEqual(resumed["owner_backtest_count"], 1)

    def test_switch_off_blocks_new_mutations_without_changing_rows(self):
        disabled = self.result["disabled_gate"]
        self.assertTrue(disabled["backtest_blocked"])
        self.assertTrue(disabled["account_blocked"])
        self.assertTrue(disabled["row_counts_unchanged"])

    def test_network_domain_port_and_runtime_audits_are_closed(self):
        static = static_acceptance(self.fixture)
        self.assertEqual(static["prohibited_imports"], [])
        self.assertEqual(static["forbidden_domain_hits"], [])
        self.assertEqual(static["forbidden_port_hits"], [])
        # 发布端口取 fixture 里已批准的清单（compose 里含 postgres 的 5432，
        # Dockerfile 只暴露应用自己的 8003），避免把基线抄死在用例里
        network = self.fixture["network_policy"]
        self.assertEqual(
            static["published_ports"],
            sorted(network.get("allowed_compose_published_ports")
                   or network["allowed_published_ports"]),
        )
        self.assertEqual(self.result["network_attempts"], [])
        self.assertEqual(self.result["broker_calls"], 0)

    def test_changed_input_cannot_silently_match_pinned_golden(self):
        changed = copy.deepcopy(self.fixture)
        changed["backtest"]["bars"][-1]["price"] = 170
        with tempfile.TemporaryDirectory() as directory:
            actual = run_golden_once(
                Path(directory) / "changed.sqlite3", changed, restart=False
            )
        self.assertNotEqual(
            actual["projection_sha256"], self.fixture["expected"]["projection_sha256"]
        )

    def test_fixture_policy_fails_closed_if_used_for_profitability(self):
        changed = copy.deepcopy(self.fixture)
        changed["dataset_policy"]["profitability_evaluation"] = "allowed"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "changed.json"
            path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_golden_fixture(path)


if __name__ == "__main__":
    unittest.main()

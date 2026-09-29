import sqlite3
import unittest
from datetime import datetime, timezone

from financial_instruments import InstrumentRegistry
from financial_schema import ensure_financial_tables
from financial_universe_planner import (
    DEFAULT_UNIVERSE_KEYS,
    FinancialUniversePlanner,
)


OBSERVED = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)


class FinancialUniversePlannerTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.execute("PRAGMA foreign_keys=ON")
        ensure_financial_tables(self.connection.cursor())
        self.instruments = InstrumentRegistry(self.connection)
        self.planner = FinancialUniversePlanner(
            self.connection, instrument_registry=self.instruments
        )
        self.seed = self.planner.load_controlled_seed()

    def tearDown(self):
        self.connection.close()

    def _create_universe(self, key="TEST_WATCHLIST", kind="watchlist"):
        return self.planner.upsert_universe(
            universe_key=key,
            display_name="测试范围",
            universe_type=kind,
            market="CN+HK",
            definition_version="test-v1",
            definition={
                "constituent_basis": "用户测试观察列表",
                "scope_basis": "explicit_members",
                "sampling_policy": "priority_then_symbol",
                "currency_policy": "group_without_implicit_conversion",
            },
            effective_from="2020-01-01",
            source_provider_key="test_source",
        )

    def test_default_scopes_and_seed_are_complete_and_idempotent(self):
        self.assertEqual(tuple(item.universe_key for item in self.seed), DEFAULT_UNIVERSE_KEYS)
        first_counts = (
            self.connection.execute("SELECT COUNT(*) FROM financial_universes").fetchone()[0],
            self.connection.execute(
                "SELECT COUNT(*) FROM financial_universe_members"
            ).fetchone()[0],
        )
        self.planner.load_controlled_seed()
        second_counts = (
            self.connection.execute("SELECT COUNT(*) FROM financial_universes").fetchone()[0],
            self.connection.execute(
                "SELECT COUNT(*) FROM financial_universe_members"
            ).fetchone()[0],
        )
        self.assertEqual(first_counts, second_counts)
        self.assertEqual(first_counts, (5, 15))

        for key in DEFAULT_UNIVERSE_KEYS:
            universe = self.planner.get_universe(key, as_of="2026-07-31")
            self.assertIn("breadth:", " ".join(universe.definition["required_market_metrics"]))
            self.assertTrue(
                universe.definition["must_not_represent_market_with_single_equity"]
            )

    def test_all_section_3_4_default_expressions_are_deterministic(self):
        cases = {
            "今天市场怎么样": "DEFAULT_MARKET_PULSE",
            "今天大盘怎么样": "DEFAULT_MARKET_PULSE",
            "A股怎么样": "CN_A_MARKET",
            "上海大盘": "CN_XSHG_MARKET",
            "深圳大盘": "CN_XSHE_MARKET",
            "港股怎么样": "HK_MARKET",
        }
        for expression, expected in cases.items():
            with self.subTest(expression=expression):
                route = self.planner.resolve_expression(expression)
                self.assertEqual(route.status, "resolved")
                self.assertEqual(route.universe_key, expected)
                self.assertFalse(route.requires_clarification)

        code = self.planner.resolve_expression("000001 怎么样")
        self.assertEqual(code.status, "instrument_resolution_required")
        self.assertTrue(code.requires_clarification)

        for expression in ("港股 9969.HK 最新情况", "3119.HK 基金走势"):
            with self.subTest(expression=expression):
                explicit = self.planner.resolve_expression(expression)
                self.assertEqual(explicit.status, "instrument_resolution_required")
                self.assertFalse(explicit.universe_key)

        selection = self.planner.resolve_expression("帮我选一只股票")
        self.assertEqual(selection.universe_key, "DEFAULT_MARKET_PULSE")
        self.assertTrue(selection.requires_clarification)
        self.assertIn("investment_scope", selection.clarification_fields)

    def test_constituent_in_and_out_preserve_historical_snapshots(self):
        self._create_universe()
        ping_an = self.instruments.require_instrument_id("000001", market="XSHE")
        tencent = self.instruments.require_instrument_id("0700.HK")
        hsi = self.instruments.require_instrument_id("HSI.HK")
        self.planner.sync_members(
            "TEST_WATCHLIST",
            [
                {"instrument_id": ping_an, "priority": 10},
                {"instrument_id": tencent, "priority": 20},
            ],
            effective_from="2020-01-01",
            source_observed_at=OBSERVED,
            source_provider_key="test_source",
        )
        self.planner.sync_members(
            "TEST_WATCHLIST",
            [
                {"instrument_id": tencent, "priority": 10},
                {"instrument_id": hsi, "priority": 20},
            ],
            effective_from="2021-06-01",
            source_observed_at=OBSERVED,
            source_provider_key="test_source_v2",
        )

        historical = self.planner.members_as_of(
            "TEST_WATCHLIST", as_of="2020-12-31"
        )
        current = self.planner.members_as_of(
            "TEST_WATCHLIST", as_of="2021-06-01"
        )
        self.assertEqual(
            {item.instrument.instrument_id for item in historical}, {ping_an, tencent}
        )
        self.assertEqual(
            {item.instrument.instrument_id for item in current}, {tencent, hsi}
        )
        historical_plan = self.planner.build_plan(
            "TEST_WATCHLIST", as_of="2020-12-31", member_budget=10
        )
        current_plan = self.planner.build_plan(
            "TEST_WATCHLIST", as_of="2021-06-01", member_budget=10
        )
        self.assertEqual(historical_plan.universe.constituent_as_of, "2020-01-01")
        self.assertEqual(current_plan.universe.constituent_as_of, "2021-06-01")

        with self.assertRaisesRegex(ValueError, "chronologically"):
            self.planner.sync_members(
                "TEST_WATCHLIST",
                [{"instrument_id": ping_an}],
                effective_from="2020-06-01",
                source_observed_at=OBSERVED,
                source_provider_key="late_backfill",
            )

    def test_definition_versions_are_historical_and_same_date_is_immutable(self):
        self._create_universe("VERSIONED_INDEX", "index")
        self.planner.upsert_universe(
            universe_key="VERSIONED_INDEX",
            display_name="版本化指数范围",
            universe_type="index",
            market="CN",
            definition_version="test-v2",
            definition={
                "constituent_basis": "新版指数成分",
                "scope_basis": "provider_constituents",
                "sampling_policy": "weight_descending",
                "currency_policy": "group_without_implicit_conversion",
            },
            effective_from="2022-01-01",
            source_provider_key="provider_v2",
        )
        old = self.planner.get_universe("VERSIONED_INDEX", as_of="2021-12-31")
        new = self.planner.get_universe("VERSIONED_INDEX", as_of="2022-01-01")
        self.assertEqual(old.definition_version, "test-v1")
        self.assertEqual(new.definition_version, "test-v2")
        self.assertEqual(old.definition_effective_to, "2022-01-01")

        with self.assertRaisesRegex(ValueError, "immutable"):
            self.planner.upsert_universe(
                universe_key="VERSIONED_INDEX",
                display_name="版本化指数范围",
                universe_type="index",
                market="CN",
                definition_version="changed-same-date",
                definition={"constituent_basis": "mutated"},
                effective_from="2022-01-01",
                source_provider_key="provider_v2",
            )

    def test_empty_scope_stays_empty_and_never_falls_back_to_one_stock(self):
        self._create_universe("EMPTY_INDUSTRY", "industry")
        self.planner.sync_members(
            "EMPTY_INDUSTRY",
            [],
            effective_from="2020-01-01",
            source_observed_at=OBSERVED,
            source_provider_key="test_source",
        )
        plan = self.planner.build_plan(
            "EMPTY_INDUSTRY", as_of="2020-01-01", member_budget=10
        )
        self.assertEqual(plan.status, "empty")
        self.assertEqual(plan.selected_members, ())
        self.assertEqual(plan.omitted_members, ())
        self.assertEqual(plan.universe.constituent_as_of, "2020-01-01")

    def test_budget_truncation_is_stable_and_reports_omitted_items(self):
        first = self.planner.build_plan(
            "DEFAULT_MARKET_PULSE", as_of="2026-07-31", member_budget=3
        )
        second = self.planner.build_plan(
            "DEFAULT_MARKET_PULSE", as_of="2026-07-31", member_budget=3
        )
        self.assertEqual(first.plan_id, second.plan_id)
        self.assertEqual(len(first.selected_members), 3)
        self.assertEqual(len(first.omitted_members), 3)
        payload = first.to_dict()
        self.assertTrue(payload["sampling"]["sampled"])
        self.assertEqual(payload["sampling"]["omitted_member_count"], 3)
        self.assertEqual(
            {item["reason"] for item in payload["coverage"]["uncovered_items"]},
            {"member_budget_truncated"},
        )

    def test_cross_currency_scope_is_grouped_without_implicit_conversion(self):
        plan = self.planner.build_plan(
            "DEFAULT_MARKET_PULSE", as_of="2026-07-31", member_budget=10
        )
        self.assertEqual(set(plan.currency_groups), {"CNY", "HKD"})
        self.assertEqual(
            plan.universe.definition["currency_policy"],
            "group_without_implicit_conversion",
        )
        self.assertEqual(len(plan.selected_members), 6)

    def test_provider_coverage_reports_counts_metrics_and_uncovered(self):
        members = self.planner.members_as_of(
            "CN_A_MARKET", as_of="2026-07-31"
        )
        available = {members[0].instrument.instrument_id, members[1].instrument.instrument_id}
        plan = self.planner.build_plan(
            "CN_A_MARKET",
            as_of="2026-07-31",
            member_budget=10,
            available_instrument_ids=available,
            available_market_metrics={"breadth:XSHG"},
        )
        payload = plan.to_dict()
        self.assertEqual(payload["coverage"]["status"], "evaluated")
        self.assertEqual(payload["coverage"]["covered_count"], 2)
        self.assertEqual(payload["coverage"]["coverage_ratio"], 0.5)
        reasons = [item["reason"] for item in payload["coverage"]["uncovered_items"]]
        self.assertEqual(reasons.count("provider_coverage_missing"), 3)
        self.assertIn("constituent_basis", payload)

    def test_automatic_detection_cannot_expand_scope(self):
        tencent = self.instruments.require_instrument_id("腾讯")
        automatic = self.planner.evaluate_candidate(
            tencent, admission_source="auto_detected"
        )
        self.assertEqual(automatic.decision, "candidate_only")
        for source in ("watchlist", "strategy", "user_request"):
            with self.subTest(source=source):
                explicit = self.planner.evaluate_candidate(
                    tencent, admission_source=source
                )
                self.assertEqual(explicit.decision, "eligible")

    def test_market_scope_validation_and_snapshot_boundaries(self):
        with self.assertRaisesRegex(ValueError, "market breadth"):
            self.planner.upsert_universe(
                universe_key="INVALID_MARKET",
                display_name="错误市场",
                universe_type="market",
                market="CN",
                definition_version="bad-v1",
                definition={"constituent_basis": "一只股票"},
                effective_from="2020-01-01",
                source_provider_key="test",
            )
        self._create_universe("BOUNDARY_WATCHLIST", "watchlist")
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.planner.sync_members(
                "BOUNDARY_WATCHLIST",
                [],
                effective_from="2020-01-01",
                source_observed_at=datetime(2020, 1, 1),
                source_provider_key="test",
            )
        with self.assertRaisesRegex(ValueError, "positive"):
            self.planner.build_plan(
                "DEFAULT_MARKET_PULSE", as_of="2026-07-31", member_budget=0
            )
        with self.assertRaises(LookupError):
            self.planner.get_universe("DEFAULT_MARKET_PULSE", as_of="2025-12-31")

    def test_universe_research_run_references_versioned_universe_id(self):
        plan = self.planner.build_plan(
            "HK_MARKET", as_of="2026-07-31", member_budget=10
        )
        self.connection.execute(
            """
            INSERT INTO financial_research_runs(
                id, trigger_type, scope_type, universe_id, status, config_json
            ) VALUES('run-hk-market', 'auto', 'universe', ?, 'queued', ?)
            """,
            (
                plan.universe.universe_id,
                '{"plan_id":"' + plan.plan_id + '"}',
            ),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT universe_id FROM financial_research_runs WHERE id='run-hk-market'"
            ).fetchone()[0],
            plan.universe.universe_id,
        )


if __name__ == "__main__":
    unittest.main()

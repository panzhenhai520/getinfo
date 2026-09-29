import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from chat_route_orchestrator import ChatRouteOrchestrator
from financial_information_needs import (
    FinancialInformationNeedsPlanner,
    validate_financial_information_needs,
)
from financial_instruments import InstrumentRegistry
from financial_intent_classifier import FinancialIntentClassifier
from financial_target_resolver import FinancialTargetResolver
from sqlite_database import SQLiteDatabase


ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
ENABLED = {
    "FINANCIAL_INTELLIGENCE_ENABLED": True,
    "FINANCIAL_INFORMATION_NEEDS_ENABLED": True,
    "FINANCIAL_INSTRUMENT_DISCOVERY_ENABLED": True,
    "FINANCIAL_INSTRUMENT_AUTO_PROMOTION_ENABLED": True,
}


class _MarketScopeRouter:
    def __init__(self):
        self.activation_count = 0

    def plan(self, *_args):
        return {
            "status": "planned",
            "route_destination": "financial_market_scope",
            "reason_codes": ["fixture_market_scope_planned"],
        }

    def activate(self, plan):
        self.activation_count += 1
        return {**dict(plan), "status": "queued"}


class FinancialInformationNeedsTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(
            str(Path(self.temp_dir.name) / "financial-information-needs.sqlite3")
        )
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.registry = InstrumentRegistry(self.database.connection)
        self.registry.load_controlled_seed()
        self.classifier = FinancialIntentClassifier(self.registry)
        self.resolver = FinancialTargetResolver(self.registry)
        self.planner = FinancialInformationNeedsPlanner(settings=ENABLED)

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def plan_needs(self, text):
        intent = self.classifier.classify(text)
        result = self.planner.plan(text, intent)
        self.assertEqual(validate_financial_information_needs(result), result)
        return result

    def test_labeled_financial_information_needs_are_deterministic(self):
        dataset = json.loads(
            (
                ROOT
                / "tests"
                / "fixtures"
                / "financial_information_needs_labeled.json"
            ).read_text(encoding="utf-8")
        )
        for item in dataset["examples"]:
            with self.subTest(text=item["text"]):
                result = self.plan_needs(item["text"])
                self.assertEqual(result["channels"], item["channels"])
                self.assertEqual(result["freshness_mode"], item["freshness_mode"])

    def test_non_financial_and_general_financial_questions_do_not_trigger_bundle(self):
        non_financial = self.plan_needs("这个软件的最新信息")
        self.assertEqual(non_financial["status"], "skipped")
        self.assertEqual(non_financial["channels"], [])

        general = self.plan_needs("股票市场是什么")
        self.assertEqual(general["status"], "skipped")
        self.assertEqual(general["channels"], [])

    def test_generic_unknown_stock_question_requests_research_and_discovery(self):
        needs = self.plan_needs("NUVB.US 怎么样")
        self.assertEqual(needs["channels"], ["research"])

        orchestrator = ChatRouteOrchestrator(
            clock=lambda: datetime(2026, 8, 3, 4, 0, tzinfo=UTC),
            financial_settings=ENABLED,
            intent_classifier=self.classifier,
            target_resolver=self.resolver,
            information_needs_planner=self.planner,
        )
        plan = orchestrator.plan(
            {
                "model": "local",
                "messages": [{"role": "user", "content": "NUVB.US 怎么样"}],
            }
        )
        self.assertEqual(plan.target_resolution["status"], "no_target")
        self.assertEqual(plan.instrument_discovery["status"], "planned")
        self.assertEqual(
            plan.target_resolution["route_destination"],
            "financial_instrument_discovery",
        )

    def test_feature_switch_disables_planner_without_changing_intent(self):
        intent = self.classifier.classify("SpaceX 股票最新信息")
        result = FinancialInformationNeedsPlanner(
            settings={
                "FINANCIAL_INTELLIGENCE_ENABLED": True,
                "FINANCIAL_INFORMATION_NEEDS_ENABLED": False,
            }
        ).plan("SpaceX 股票最新信息", intent)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason_codes"], ["information_needs_disabled"])

    def test_orchestrator_sends_unknown_latest_stock_to_discovery(self):
        orchestrator = ChatRouteOrchestrator(
            clock=lambda: datetime(2026, 8, 3, 4, 0, tzinfo=UTC),
            financial_settings=ENABLED,
            intent_classifier=self.classifier,
            target_resolver=self.resolver,
            information_needs_planner=self.planner,
        )
        plan = orchestrator.plan(
            {
                "model": "local",
                "user_timezone": "Asia/Hong_Kong",
                "messages": [
                    {"role": "user", "content": "SpaceX 股票最新信息"}
                ],
            }
        )
        self.assertEqual(plan.information_needs["channels"], ["quote", "news"])
        self.assertEqual(plan.target_resolution["status"], "no_target")
        self.assertEqual(
            plan.target_resolution["route_destination"],
            "financial_instrument_discovery",
        )
        self.assertEqual(
            plan.realtime_query["reason_codes"], ["instrument_discovery_required"]
        )
        self.assertEqual(plan.instrument_discovery["status"], "planned")

    def test_discovery_promotes_spacex_and_replans_quote_once(self):
        from financial_instrument_discovery import FinancialInstrumentDiscoveryService

        discovery = FinancialInstrumentDiscoveryService(
            self.database.connection,
            settings=ENABLED,
        )
        orchestrator = ChatRouteOrchestrator(
            clock=lambda: datetime(2026, 8, 3, 4, 0, tzinfo=UTC),
            financial_settings=ENABLED,
            intent_classifier=self.classifier,
            target_resolver=self.resolver,
            information_needs_planner=self.planner,
            instrument_discovery_service=discovery,
        )
        payload = {
            "model": "local",
            "user_timezone": "Asia/Hong_Kong",
            "messages": [{"role": "user", "content": "SpaceX 股票最新信息"}],
        }
        initial = orchestrator.plan(payload)
        completed = orchestrator.activate_instrument_discovery(initial, payload)

        self.assertEqual(completed.instrument_discovery["status"], "promoted")
        self.assertEqual(completed.target_resolution["status"], "resolved")
        self.assertEqual(
            completed.target_resolution["targets"][0]["canonical_symbol"],
            "SPCX.US",
        )
        self.assertEqual(completed.realtime_query["status"], "skipped")
        self.assertEqual(
            completed.realtime_query["reason_codes"],
            ["realtime_query_service_not_configured"],
        )

    def test_unknown_hk_instrument_discovery_blocks_market_scope_activation(self):
        market_router = _MarketScopeRouter()
        orchestrator = ChatRouteOrchestrator(
            clock=lambda: datetime(2026, 8, 3, 4, 0, tzinfo=UTC),
            financial_settings=ENABLED,
            intent_classifier=self.classifier,
            target_resolver=self.resolver,
            information_needs_planner=self.planner,
            market_scope_router=market_router,
        )
        payload = {
            "model": "local",
            "user_timezone": "Asia/Hong_Kong",
            "messages": [
                {"role": "user", "content": "港股陌生生物科技股票最新情况"}
            ],
        }

        initial = orchestrator.plan(payload)
        self.assertEqual(initial.instrument_discovery["status"], "planned")
        self.assertEqual(initial.market_scope["status"], "planned")

        discovered = orchestrator.activate_instrument_discovery(initial, payload)
        completed = orchestrator.activate_market_scope(discovered)

        self.assertEqual(discovered.instrument_discovery["status"], "verification_required")
        self.assertEqual(completed.market_scope["status"], "skipped")
        self.assertIn(
            "instrument_discovery_takes_precedence",
            completed.market_scope["reason_codes"],
        )
        self.assertEqual(market_router.activation_count, 0)


if __name__ == "__main__":
    unittest.main()

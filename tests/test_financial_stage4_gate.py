import copy
import unittest
from pathlib import Path

from financial_stage4_gate import (
    REQUIRED_CATEGORIES,
    evaluate_stage4_fixture,
    load_stage4_fixture,
)


FIXTURE = Path(__file__).parent / "fixtures" / "financial_stage4_heldout.json"


class FinancialStage4GateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = load_stage4_fixture(FIXTURE)
        cls.result = evaluate_stage4_fixture(FIXTURE)

    def test_fixture_is_held_out_manual_and_has_unique_cases(self):
        policy = self.fixture["dataset_policy"]
        self.assertEqual(policy["rule_development_usage"], "prohibited")
        self.assertEqual(policy["label_source"], "manual_scenario_review")
        ids = [item["case_id"] for item in self.fixture["cases"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 10)

    def test_fixture_policy_fails_closed_if_repurposed_for_rule_development(self):
        changed = copy.deepcopy(self.fixture)
        changed["dataset_policy"]["rule_development_usage"] = "allowed"
        from tempfile import TemporaryDirectory
        import json

        with TemporaryDirectory() as directory:
            path = Path(directory) / "changed.json"
            path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_stage4_fixture(path)

    def test_all_required_business_categories_are_covered(self):
        self.assertEqual(
            set(self.result["coverage"]["categories"]), REQUIRED_CATEGORIES
        )
        self.assertTrue(self.result["coverage"]["future_data"])
        self.assertTrue(self.result["coverage"]["stale_data"])
        self.assertTrue(self.result["coverage"]["scope_conflict"])
        self.assertTrue(self.result["coverage"]["unresolved_conflict"])

    def test_current_fact_precision_and_recall_exceed_gate(self):
        metrics = self.result["metrics"]
        self.assertEqual(self.result["status"], "passed")
        self.assertGreaterEqual(metrics["current_fact_precision"], 0.95)
        self.assertGreaterEqual(metrics["current_fact_recall"], 0.95)
        self.assertEqual(metrics["false_positive"], 0)
        self.assertEqual(metrics["false_negative"], 0)
        self.assertEqual(metrics["exact_case_matches"], len(self.fixture["cases"]))

    def test_future_stale_scope_conflict_and_unresolved_never_become_current(self):
        prohibited_temporal = {"insufficient_evidence", "stale", "superseded"}
        prohibited_conflict = {
            "insufficient_evidence", "single_source", "unresolved_conflict",
            "incomparable_evidence",
        }
        for item in self.result["cases"]:
            if (
                item["actual"]["temporal_verdict"] in prohibited_temporal
                or item["actual"]["conflict_verdict"] in prohibited_conflict
            ):
                self.assertFalse(item["actual"]["current_fact"], item["case_id"])
        self.assertFalse(
            self.result["boundaries"]["future_stale_conflicted_or_incomparable_current_fact"]
        )

    def test_tradingagents_report_only_evidence_never_becomes_fact(self):
        report_cases = [
            item for item in self.result["cases"] if item["report_only_evidence"]
        ]
        self.assertGreaterEqual(len(report_cases), 1)
        self.assertTrue(all(not item["report_promoted_to_fact"] for item in report_cases))
        self.assertTrue(self.result["boundaries"]["model_report_is_research_only"])
        self.assertFalse(self.result["boundaries"]["model_report_promoted_to_fact"])

    def test_positive_controls_preserve_verified_current_and_history(self):
        current = [item for item in self.result["cases"] if item["actual"]["current_fact"]]
        historical = [item for item in self.result["cases"] if item["actual"]["historical_fact"]]
        self.assertGreaterEqual(len(current), 3)
        self.assertGreater(len(historical), len(current))
        self.assertTrue(
            all(
                item["actual"]["conflict_verdict"]
                in {"verified_consensus", "verified_authoritative"}
                for item in historical
            )
        )


if __name__ == "__main__":
    unittest.main()

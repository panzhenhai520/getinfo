#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Held-out stage-4 evaluation for financial fact publication boundaries."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

from jsonschema import Draft202012Validator

from financial_conflict_judge import FinancialConflictJudge
from financial_temporal_judge import FinancialTemporalJudge


FINANCIAL_STAGE4_GATE_VERSION = "financial-stage4-gate-v1"
FINANCIAL_STAGE4_FIXTURE_VERSION = "financial-stage4-heldout-v1"
REQUIRED_CATEGORIES = frozenset(
    {"price", "financial_statement", "announcement", "index_membership", "macro_revision"}
)
FACT_VERDICTS = frozenset({"verified_consensus", "verified_authoritative"})
TEMPORALLY_USABLE = frozenset({"verified_current", "verified_historical"})

FINANCIAL_STAGE4_RESULT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "schema_version", "gate_version", "status", "dataset", "metrics",
        "coverage", "cases", "boundaries",
    ],
    "properties": {
        "schema_version": {"const": "financial-stage4-gate-result-v1"},
        "gate_version": {"const": FINANCIAL_STAGE4_GATE_VERSION},
        "status": {"enum": ["passed", "failed"]},
        "dataset": {"type": "object"},
        "metrics": {"type": "object"},
        "coverage": {"type": "object"},
        "cases": {"type": "array", "items": {"type": "object"}},
        "boundaries": {"type": "object"},
    },
    "additionalProperties": False,
}
_RESULT_VALIDATOR = Draft202012Validator(FINANCIAL_STAGE4_RESULT_SCHEMA)


def _safe_ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def load_stage4_fixture(path: str | Path) -> dict:
    fixture = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(fixture, Mapping):
        raise ValueError("stage 4 fixture must be an object")
    if fixture.get("schema_version") != FINANCIAL_STAGE4_FIXTURE_VERSION:
        raise ValueError("unsupported stage 4 fixture version")
    policy = fixture.get("dataset_policy")
    if not isinstance(policy, Mapping):
        raise ValueError("stage 4 fixture requires dataset policy")
    if policy.get("rule_development_usage") != "prohibited":
        raise ValueError("stage 4 fixture must be held out from rule development")
    if policy.get("label_source") != "manual_scenario_review":
        raise ValueError("stage 4 fixture labels must be manually reviewed")
    cases = fixture.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("stage 4 fixture requires labeled cases")
    ids = set()
    for case in cases:
        if not isinstance(case, Mapping):
            raise ValueError("stage 4 case must be an object")
        case_id = str(case.get("case_id") or "")
        if not case_id or case_id in ids:
            raise ValueError("stage 4 case ids must be unique")
        ids.add(case_id)
        if case.get("category") not in REQUIRED_CATEGORIES:
            raise ValueError(f"unsupported stage 4 category: {case.get('category')}")
        if case.get("case_origin") != "held_out_manual":
            raise ValueError("each stage 4 case must declare held-out origin")
        expected = case.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError("stage 4 case requires expected labels")
        for key in ("temporal_verdict", "conflict_verdict", "current_fact", "historical_fact"):
            if key not in expected:
                raise ValueError(f"stage 4 expected label missing: {key}")
    return dict(fixture)


class FinancialStage4Gate:
    """Run production judges over a manually labelled held-out scenario set."""

    def __init__(self, *, temporal_judge=None, conflict_judge=None):
        self.temporal_judge = temporal_judge or FinancialTemporalJudge()
        self.conflict_judge = conflict_judge or FinancialConflictJudge()

    @staticmethod
    def _conflict_evidence(
        evidence: Sequence[Mapping[str, object]], temporal: Mapping[str, object]
    ) -> list[dict]:
        verdict = str(temporal.get("verdict") or "")
        selected = {int(item) for item in temporal.get("selected_evidence_ids") or []}
        ignored = {int(item) for item in temporal.get("ignored_evidence_ids") or []}
        result = []
        for raw in evidence:
            item = dict(raw)
            try:
                evidence_id = int(item.get("evidence_id") or 0)
            except (TypeError, ValueError):
                evidence_id = 0
            eligible = verdict in TEMPORALLY_USABLE and (
                evidence_id in selected or evidence_id not in ignored
            )
            item["temporal_status"] = verdict if eligible else "unknown"
            result.append(item)
        return result

    def evaluate_case(self, case: Mapping[str, object]) -> dict:
        temporal = self.temporal_judge.judge(
            case["claim"],
            case.get("temporal_evidence") or [],
            request_context=case["request_context"],
            market_session=case.get("market_session") or {},
        )
        conflict = self.conflict_judge.judge(
            case["claim"],
            self._conflict_evidence(
                case.get("conflict_evidence") or [], temporal
            ),
        )
        current_fact = (
            temporal["verdict"] == "verified_current"
            and conflict["verdict"] in FACT_VERDICTS
        )
        historical_fact = (
            temporal["verdict"] in TEMPORALLY_USABLE
            and conflict["verdict"] in FACT_VERDICTS
        )
        expected = dict(case["expected"])
        checks = {
            "temporal_verdict": temporal["verdict"] == expected["temporal_verdict"],
            "conflict_verdict": conflict["verdict"] == expected["conflict_verdict"],
            "current_fact": current_fact is bool(expected["current_fact"]),
            "historical_fact": historical_fact is bool(expected["historical_fact"]),
        }
        providers = {
            str(item.get("provider_id") or "").casefold()
            for item in case.get("conflict_evidence") or []
        }
        report_only = bool(providers) and providers == {"tradingagents_report"}
        return {
            "case_id": str(case["case_id"]),
            "category": str(case["category"]),
            "passed": all(checks.values()),
            "checks": checks,
            "expected": expected,
            "actual": {
                "temporal_verdict": temporal["verdict"],
                "conflict_verdict": conflict["verdict"],
                "current_fact": current_fact,
                "historical_fact": historical_fact,
                "human_review_required": bool(conflict["human_review_required"]),
            },
            "reason_codes": {
                "temporal": list(temporal["reason_codes"]),
                "conflict": list(conflict["reason_codes"]),
                "conflict_ignored": sorted(
                    {
                        str(item.get("reason") or "")
                        for item in conflict.get("ignored_evidence") or []
                        if item.get("reason")
                    }
                ),
            },
            "report_only_evidence": report_only,
            "report_promoted_to_fact": bool(report_only and historical_fact),
        }

    def evaluate(self, fixture: Mapping[str, object]) -> dict:
        cases = [self.evaluate_case(item) for item in fixture["cases"]]
        matrix = Counter()
        for result in cases:
            expected = bool(result["expected"]["current_fact"])
            actual = bool(result["actual"]["current_fact"])
            matrix[(expected, actual)] += 1
        tp = matrix[(True, True)]
        fp = matrix[(False, True)]
        fn = matrix[(True, False)]
        tn = matrix[(False, False)]
        precision = _safe_ratio(tp, tp + fp)
        recall = _safe_ratio(tp, tp + fn)
        categories = sorted({item["category"] for item in cases})
        report_cases = [item for item in cases if item["report_only_evidence"]]
        thresholds = dict(fixture.get("thresholds") or {})
        min_precision = float(thresholds.get("current_fact_precision", 0.95))
        min_recall = float(thresholds.get("current_fact_recall", 0.95))
        passed = bool(
            all(item["passed"] for item in cases)
            and precision >= min_precision
            and recall >= min_recall
            and set(categories) == REQUIRED_CATEGORIES
            and report_cases
            and not any(item["report_promoted_to_fact"] for item in report_cases)
        )
        result = {
            "schema_version": "financial-stage4-gate-result-v1",
            "gate_version": FINANCIAL_STAGE4_GATE_VERSION,
            "status": "passed" if passed else "failed",
            "dataset": {
                "fixture_id": str(fixture.get("fixture_id") or ""),
                "schema_version": str(fixture.get("schema_version") or ""),
                "case_count": len(cases),
                "held_out": True,
                "label_source": "manual_scenario_review",
                "rule_development_usage": "prohibited",
            },
            "metrics": {
                "current_fact_precision": precision,
                "current_fact_recall": recall,
                "true_positive": tp,
                "false_positive": fp,
                "false_negative": fn,
                "true_negative": tn,
                "exact_case_matches": sum(item["passed"] for item in cases),
                "minimum_precision": min_precision,
                "minimum_recall": min_recall,
            },
            "coverage": {
                "categories": categories,
                "required_categories": sorted(REQUIRED_CATEGORIES),
                "future_data": any("future_evidence_ignored" in item["reason_codes"]["temporal"] for item in cases),
                "stale_data": any(item["actual"]["temporal_verdict"] == "stale" for item in cases),
                "scope_conflict": any(
                    item["actual"]["conflict_verdict"] == "incomparable_evidence"
                    or bool(
                        {"period_mismatch", "instrument_mismatch", "metric_mismatch"}
                        & set(item["reason_codes"]["conflict_ignored"])
                    )
                    for item in cases
                ),
                "unresolved_conflict": any(item["actual"]["conflict_verdict"] == "unresolved_conflict" for item in cases),
                "report_only_cases": len(report_cases),
            },
            "cases": cases,
            "boundaries": {
                "production_temporal_judge_used": True,
                "production_conflict_judge_used": True,
                "model_report_is_research_only": True,
                "model_report_promoted_to_fact": any(item["report_promoted_to_fact"] for item in report_cases),
                "future_stale_conflicted_or_incomparable_current_fact": any(
                    item["actual"]["current_fact"]
                    for item in cases
                    if item["actual"]["temporal_verdict"] in {"stale", "superseded", "insufficient_evidence"}
                    or item["actual"]["conflict_verdict"] in {"unresolved_conflict", "incomparable_evidence", "single_source", "insufficient_evidence"}
                ),
                "model_calls": 0,
                "network_calls": 0,
                "real_order_execution": False,
            },
        }
        _RESULT_VALIDATOR.validate(result)
        return result


def evaluate_stage4_fixture(path: str | Path) -> dict:
    return FinancialStage4Gate().evaluate(load_stage4_fixture(path))


__all__ = [
    "FINANCIAL_STAGE4_FIXTURE_VERSION",
    "FINANCIAL_STAGE4_GATE_VERSION",
    "FINANCIAL_STAGE4_RESULT_SCHEMA",
    "FinancialStage4Gate",
    "evaluate_stage4_fixture",
    "load_stage4_fixture",
]

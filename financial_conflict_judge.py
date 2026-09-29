#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Evidence-lineage-aware multi-source conflict adjudication.

This deterministic judge runs after the Temporal Judge.  It normalises only
declared, compatible dimensions and never averages prices, performs implicit
FX conversion, or counts two wrappers over one upstream source as independent.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator


FINANCIAL_CONFLICT_JUDGE_VERSION = "financial-conflict-judge-v1"
FINANCIAL_CONFLICT_VERDICT_SCHEMA_VERSION = "financial-conflict-verdict-v1"
CONFLICT_VERDICTS = (
    "verified_consensus",
    "verified_authoritative",
    "single_source",
    "unresolved_conflict",
    "incomparable_evidence",
    "insufficient_evidence",
)
ELIGIBLE_TEMPORAL_STATUSES = frozenset({"verified_current", "verified_historical"})
REALTIME_METRICS = frozenset(
    {
        "last_price",
        "close",
        "open",
        "high",
        "low",
        "index_level",
        "price_change",
        "market_breadth",
        "volume",
        "turnover",
    }
)
METRIC_ALIASES = {
    "price": "last_price",
    "last": "last_price",
    "lastprice": "last_price",
    "收盘价": "close",
    "现价": "last_price",
    "股价": "last_price",
    "指数点位": "index_level",
}
DEFAULT_TOLERANCE = {"relative": 0.005, "absolute": 1e-8}
METRIC_TOLERANCES = {
    "last_price": {"relative": 0.001, "absolute": 0.01},
    "close": {"relative": 0.001, "absolute": 0.01},
    "open": {"relative": 0.001, "absolute": 0.01},
    "high": {"relative": 0.001, "absolute": 0.01},
    "low": {"relative": 0.001, "absolute": 0.01},
    "index_level": {"relative": 0.001, "absolute": 0.1},
    "nav": {"relative": 0.0001, "absolute": 0.0001},
    "revenue": {"relative": 0.001, "absolute": 1.0},
    "net_income": {"relative": 0.001, "absolute": 1.0},
    "cpi": {"relative": 0.0, "absolute": 0.05},
    "gdp": {"relative": 0.0, "absolute": 0.05},
}
PROVIDER_AUTHORITY = {
    "official_evidence": 1.0,
    "exchange_official": 1.0,
    "issuer_official": 1.0,
    "fred": 0.98,
    "tushare_cn": 0.85,
    "akshare_cn": 0.72,
    "yahoo": 0.58,
    "easyquotation": 0.55,
    "alpha_vantage": 0.62,
    "tradingagents_report": 0.35,
}
DIRECT_SOURCE_FAMILY = {
    "tushare_cn": "tushare",
    "yahoo": "yahoo",
    "fred": "fred_alfred",
    "alpha_vantage": "alpha_vantage",
    "polymarket": "polymarket",
    "issuer_official": "issuer_official",
    "exchange_official": "exchange_official",
}
AUTHORITATIVE_THRESHOLD = 0.95
AUTHORITY_MARGIN = 0.15
AUTHORITATIVE_PROVIDERS = frozenset(
    {"official_evidence", "exchange_official", "issuer_official", "fred"}
)
DEFAULT_SAME_OBSERVATION_SECONDS = 60
UNAVAILABLE_STATES = frozenset(
    {
        "permission_denied",
        "not_entitled",
        "not_configured",
        "rate_limited",
        "temporarily_unavailable",
        "unsupported",
        "missing",
    }
)


CONFLICT_VERDICT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "schema_version",
        "judge_version",
        "rule_version",
        "claim_key",
        "claim_dimension",
        "verdict",
        "reason_codes",
        "tolerance",
        "decision_value",
        "selected_evidence_ids",
        "conflicting_evidence_ids",
        "ignored_evidence",
        "independence_groups",
        "input_evidence",
        "input_snapshot_hashes",
        "human_review_required",
        "boundaries",
    ],
    "properties": {
        "schema_version": {"const": FINANCIAL_CONFLICT_VERDICT_SCHEMA_VERSION},
        "judge_version": {"const": FINANCIAL_CONFLICT_JUDGE_VERSION},
        "rule_version": {"const": FINANCIAL_CONFLICT_JUDGE_VERSION},
        "claim_key": {"type": "string"},
        "claim_dimension": {"type": "object"},
        "verdict": {"enum": list(CONFLICT_VERDICTS)},
        "reason_codes": {"type": "array", "items": {"type": "string"}},
        "tolerance": {"type": "object"},
        "decision_value": {"type": "object"},
        "selected_evidence_ids": {"type": "array", "items": {"type": "integer"}},
        "conflicting_evidence_ids": {"type": "array", "items": {"type": "integer"}},
        "ignored_evidence": {"type": "array", "items": {"type": "object"}},
        "independence_groups": {"type": "array", "items": {"type": "object"}},
        "input_evidence": {"type": "array", "items": {"type": "object"}},
        "input_snapshot_hashes": {"type": "array", "items": {"type": "object"}},
        "human_review_required": {"type": "boolean"},
        "boundaries": {"type": "object"},
    },
    "additionalProperties": False,
}
_VERDICT_VALIDATOR = Draft202012Validator(CONFLICT_VERDICT_SCHEMA)


def _mapping(value: object) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _json_object(value: object) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _json_array(value: object) -> list:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _parse_utc(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _utc_text(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _evidence_id(value: Mapping[str, object]) -> int | None:
    raw = value.get("evidence_id") if "evidence_id" in value else value.get("id")
    try:
        result = int(raw)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _metric(value: object) -> str:
    normalized = str(value or "").strip().casefold()
    return METRIC_ALIASES.get(normalized, normalized)


def _period_key(value: object) -> str:
    period = _mapping(value)
    return _json(
        {
            key: str(period.get(key) or "")
            for key in ("kind", "start", "end", "label")
            if str(period.get(key) or "")
        }
    )


def _scalar(value: object) -> float | None:
    if isinstance(value, Mapping):
        kind = str(value.get("kind") or "").casefold()
        if kind == "scalar" or "number" in value:
            return _finite(value.get("number"))
        return None
    return _finite(value)


def _unit_contract(unit: object, currency: object) -> tuple[str, str, float]:
    raw = str(unit or "").strip()
    normalized = raw.casefold()
    upper = raw.upper()
    curr = str(currency or "").strip().upper()
    if raw in {"港元", "港币"} or upper == "HKD":
        return "currency_amount", curr or "HKD", 1.0
    if raw in {"万港元"}:
        return "currency_amount", curr or "HKD", 10_000.0
    if raw in {"亿港元"}:
        return "currency_amount", curr or "HKD", 100_000_000.0
    if raw in {"人民币", "元"} or upper in {"CNY", "RMB"}:
        return "currency_amount", curr or "CNY", 1.0
    if raw in {"万元"}:
        return "currency_amount", curr or "CNY", 10_000.0
    if raw in {"亿元"}:
        return "currency_amount", curr or "CNY", 100_000_000.0
    if raw == "美元" or upper == "USD":
        return "currency_amount", curr or "USD", 1.0
    if raw in {"亿美元"}:
        return "currency_amount", curr or "USD", 100_000_000.0
    if raw in {"%", "％", "percent"} or normalized == "percentage_point":
        return "percent", "", 1.0
    if raw in {"基点", "个基点"} or normalized in {"bps", "basis_point"}:
        return "percent", "", 0.01
    if raw in {"点", "point", "points", "index_point"}:
        return "point", "", 1.0
    if raw in {"股", "万股", "亿股", "share", "shares"}:
        if raw in {"share", "shares"}:
            return "share", "", 1.0
        multiplier = {"股": 1.0, "万股": 10_000.0, "亿股": 100_000_000.0}[raw]
        return "share", "", multiplier
    if normalized in {"price", "currency", "currency_amount"}:
        return "currency_amount", curr, 1.0
    return normalized or "number", curr, 1.0


def _host(value: object) -> str:
    try:
        hostname = (urlsplit(str(value or "")).hostname or "").casefold().removeprefix("www.")
        for suffix, family in (
            ("eastmoney.com", "eastmoney"),
            ("qq.com", "tencent"),
            ("yahoo.com", "yahoo"),
            ("waditu.com", "tushare"),
            ("tushare.pro", "tushare"),
            ("sse.com.cn", "sse_official"),
            ("szse.cn", "szse_official"),
            ("hkex.com.hk", "hkex_official"),
        ):
            if hostname == suffix or hostname.endswith(f".{suffix}"):
                return family
        return hostname
    except ValueError:
        return ""


def _nested_lineage(item: Mapping[str, object]) -> dict:
    lineage = _mapping(item.get("lineage"))
    details = _mapping(lineage.get("details"))
    return {**lineage, **details}


def _independence_key(item: Mapping[str, object]) -> str:
    lineage = _nested_lineage(item)
    for source in (item, lineage, _mapping(item.get("provider_metadata"))):
        for key in (
            "independence_key",
            "underlying_source_id",
            "underlying_source",
            "source_family",
            "upstream_source",
        ):
            value = str(source.get(key) or "").strip().casefold()
            if value:
                return value
    hostname = _host(item.get("source_url"))
    if hostname:
        return hostname
    provider = str(item.get("provider_id") or item.get("provider_key") or "").casefold()
    return DIRECT_SOURCE_FAMILY.get(provider, "unknown_lineage")


def _authority(item: Mapping[str, object]) -> float:
    explicit = _finite(item.get("authority_score"))
    if explicit is not None:
        return max(0.0, min(1.0, explicit))
    provider = str(item.get("provider_id") or item.get("provider_key") or "").casefold()
    if provider in PROVIDER_AUTHORITY:
        return PROVIDER_AUTHORITY[provider]
    try:
        priority = max(0, int(item.get("provider_priority")))
    except (TypeError, ValueError):
        return 0.4
    return max(0.2, min(0.9, 1.0 - priority / 125.0))


def _official_can_decide(item: Mapping[str, object], metric: str) -> bool:
    provider = str(item.get("provider_id") or item.get("provider_key") or "").casefold()
    evidence_type = str(item.get("evidence_type") or "").casefold()
    if _authority(item) < AUTHORITATIVE_THRESHOLD:
        return False
    authority_class = str(item.get("authority_class") or "").casefold()
    if provider not in AUTHORITATIVE_PROVIDERS and authority_class not in {
        "official",
        "exchange",
        "issuer",
        "regulator",
        "central_bank",
    }:
        return False
    if metric in REALTIME_METRICS and (
        provider == "official_evidence" or evidence_type in {"article", "source_document"}
    ):
        return False
    return True


class FinancialConflictJudge:
    """Resolve only compatible evidence dimensions with explicit lineage."""

    def __init__(
        self,
        *,
        metric_tolerances: Mapping[str, Mapping[str, float]] | None = None,
        same_observation_seconds: int = DEFAULT_SAME_OBSERVATION_SECONDS,
    ):
        self.tolerances = {key: dict(value) for key, value in METRIC_TOLERANCES.items()}
        for metric, value in (metric_tolerances or {}).items():
            relative = float(value.get("relative", DEFAULT_TOLERANCE["relative"]))
            absolute = float(value.get("absolute", DEFAULT_TOLERANCE["absolute"]))
            if relative < 0 or absolute < 0:
                raise ValueError("tolerances cannot be negative")
            self.tolerances[_metric(metric)] = {"relative": relative, "absolute": absolute}
        self.same_observation_seconds = int(same_observation_seconds)
        if self.same_observation_seconds < 0:
            raise ValueError("same_observation_seconds cannot be negative")

    @staticmethod
    def _claim(value: Mapping[str, object]) -> dict:
        source = dict(value)
        normalized = _json_object(source.get("normalized_value_json"))
        result = {**normalized, **source}
        result["claim_key"] = str(result.get("claim_key") or "")
        result["claim_type"] = str(result.get("claim_type") or "fact").casefold()
        result["metric"] = _metric(result.get("metric"))
        result["subject"] = str(
            result.get("subject")
            or _mapping(normalized.get("subject")).get("instrument_key")
            or ""
        )
        result["period"] = _mapping(result.get("period"))
        result["adjustment"] = str(result.get("adjustment") or "raw").casefold()
        return result

    @staticmethod
    def _ignore(ignored: list[dict], item: Mapping[str, object], reason: str) -> None:
        ignored.append(
            {
                "evidence_id": _evidence_id(item),
                "snapshot_id": _evidence_id({"id": item.get("snapshot_id")}),
                "provider_id": str(item.get("provider_id") or item.get("provider_key") or ""),
                "reason": reason,
            }
        )

    def _normalise_evidence(
        self,
        claim: Mapping[str, object],
        values: Sequence[Mapping[str, object]],
    ) -> tuple[list[dict], list[dict]]:
        accepted = []
        ignored: list[dict] = []
        claim_period = _period_key(claim.get("period"))
        for raw in values:
            item = dict(raw)
            availability = str(item.get("availability_status") or "available").casefold()
            if availability in UNAVAILABLE_STATES:
                self._ignore(ignored, item, f"provider_{availability}")
                continue
            if item.get("integrity_valid") is False:
                self._ignore(ignored, item, "evidence_integrity_failed")
                continue
            temporal = str(item.get("temporal_status") or "").casefold()
            if temporal not in ELIGIBLE_TEMPORAL_STATUSES:
                self._ignore(ignored, item, "temporally_ineligible")
                continue
            instrument_key = str(item.get("instrument_key") or claim.get("subject") or "")
            if claim.get("subject") and instrument_key != claim["subject"]:
                self._ignore(ignored, item, "instrument_mismatch")
                continue
            metric = _metric(item.get("metric"))
            if metric != claim["metric"]:
                self._ignore(ignored, item, "metric_mismatch")
                continue
            evidence_period = _period_key(item.get("period"))
            if claim_period and evidence_period and evidence_period != claim_period:
                self._ignore(ignored, item, "period_mismatch")
                continue
            number = _scalar(item.get("value"))
            if number is None:
                self._ignore(ignored, item, "non_scalar_value")
                continue
            unit, currency, multiplier = _unit_contract(
                item.get("unit"), item.get("currency")
            )
            observed = _parse_utc(item.get("observed_at"))
            if observed is None:
                self._ignore(ignored, item, "observation_time_missing")
                continue
            item.update(
                {
                    "evidence_id": _evidence_id(item),
                    "instrument_key": instrument_key,
                    "metric": metric,
                    "canonical_value": number * multiplier,
                    "canonical_unit": unit,
                    "canonical_currency": currency,
                    "adjustment": str(item.get("adjustment") or "raw").casefold(),
                    "period_key": evidence_period or claim_period,
                    "observed_dt": observed,
                    "provider_id": str(
                        item.get("provider_id") or item.get("provider_key") or ""
                    ).casefold(),
                    "independence_key": _independence_key(item),
                    "authority": _authority(item),
                }
            )
            accepted.append(item)
        return accepted, ignored

    def _tolerance(self, metric: str, values: Sequence[float]) -> dict:
        policy = dict(self.tolerances.get(metric, DEFAULT_TOLERANCE))
        scale = max((abs(value) for value in values), default=0.0)
        effective = max(float(policy["absolute"]), scale * float(policy["relative"]))
        return {
            "metric": metric,
            "relative": float(policy["relative"]),
            "absolute": float(policy["absolute"]),
            "effective_absolute": effective,
        }

    @staticmethod
    def _scope(item: Mapping[str, object]) -> tuple:
        return (
            str(item.get("canonical_unit") or ""),
            str(item.get("canonical_currency") or ""),
            str(item.get("adjustment") or ""),
            str(item.get("period_key") or ""),
        )

    @staticmethod
    def _public_input(item: Mapping[str, object]) -> dict:
        return {
            "evidence_id": item.get("evidence_id"),
            "snapshot_id": item.get("snapshot_id"),
            "provider_id": str(item.get("provider_id") or ""),
            "independence_key": str(item.get("independence_key") or ""),
            "authority_score": float(item.get("authority") or 0.0),
            "observed_at": _utc_text(item.get("observed_dt")),
            "temporal_status": str(item.get("temporal_status") or ""),
            "value": float(item.get("canonical_value")),
            "unit": str(item.get("canonical_unit") or ""),
            "currency": str(item.get("canonical_currency") or ""),
            "adjustment": str(item.get("adjustment") or ""),
            "period_key": str(item.get("period_key") or ""),
            "source_url": str(item.get("source_url") or ""),
        }

    def judge(
        self,
        claim: Mapping[str, object],
        evidence: Sequence[Mapping[str, object]],
    ) -> dict:
        normalized_claim = self._claim(claim)
        metric = normalized_claim["metric"]
        claim_number = _scalar(normalized_claim.get("value"))
        claim_unit, claim_currency, claim_multiplier = _unit_contract(
            normalized_claim.get("unit"), normalized_claim.get("currency")
        )
        if claim_number is not None:
            claim_number *= claim_multiplier
        claim_scope = (
            claim_unit,
            claim_currency,
            normalized_claim["adjustment"],
            _period_key(normalized_claim.get("period")),
        )
        accepted, ignored = self._normalise_evidence(normalized_claim, evidence)
        reason_codes = []
        verdict = "insufficient_evidence"
        selected: list[dict] = []
        conflicts: list[dict] = []
        decision_value = {}

        if normalized_claim["claim_type"] != "fact":
            reason_codes.append("non_fact_not_conflict_verified")
            accepted = []
        elif claim_number is None:
            reason_codes.append("claim_value_not_scalar")

        if accepted and claim_number is not None:
            if metric in REALTIME_METRICS:
                newest = max(item["observed_dt"] for item in accepted)
                timely = []
                for item in accepted:
                    lag = (newest - item["observed_dt"]).total_seconds()
                    if lag > self.same_observation_seconds:
                        self._ignore(ignored, item, "different_observation_time")
                    else:
                        timely.append(item)
                accepted = timely

            scopes: dict[tuple, list[dict]] = defaultdict(list)
            for item in accepted:
                scopes[self._scope(item)].append(item)
            comparable = scopes.get(claim_scope, [])
            if not comparable and len(scopes) == 1 and not any(claim_scope[:2]):
                comparable = next(iter(scopes.values()))
                claim_scope = self._scope(comparable[0])
            for scope, items in scopes.items():
                if scope == claim_scope:
                    continue
                for item in items:
                    self._ignore(ignored, item, "incomparable_currency_unit_adjustment_or_period")
            if not comparable:
                verdict = "incomparable_evidence" if accepted else "insufficient_evidence"
                reason_codes.append(
                    "no_evidence_matches_claim_scope" if accepted else "no_eligible_evidence"
                )
            else:
                independence: dict[str, list[dict]] = defaultdict(list)
                for item in comparable:
                    independence[item["independence_key"]].append(item)
                representatives = []
                for key, items in independence.items():
                    ordered = sorted(
                        items,
                        key=lambda item: (
                            float(item["authority"]),
                            item["observed_dt"],
                            int(item.get("evidence_id") or 0),
                        ),
                        reverse=True,
                    )
                    representatives.append(ordered[0])
                    for duplicate in ordered[1:]:
                        self._ignore(ignored, duplicate, "correlated_source_collapsed")
                representatives.sort(
                    key=lambda item: (
                        float(item["authority"]),
                        item["observed_dt"],
                        int(item.get("evidence_id") or 0),
                    ),
                    reverse=True,
                )
                values = [float(item["canonical_value"]) for item in representatives]
                tolerance = self._tolerance(metric, values)
                spread = max(values) - min(values) if values else 0.0
                claim_matches = bool(
                    values
                    and min(values) - tolerance["effective_absolute"]
                    <= claim_number
                    <= max(values) + tolerance["effective_absolute"]
                )
                if len(representatives) >= 2 and spread <= tolerance["effective_absolute"]:
                    if claim_matches:
                        verdict = "verified_consensus"
                        selected = representatives
                        decision_value = {
                            "number": float(representatives[0]["canonical_value"]),
                            "unit": claim_scope[0],
                            "currency": claim_scope[1],
                            "adjustment": claim_scope[2],
                            "source_evidence_id": representatives[0].get("evidence_id"),
                        }
                        reason_codes.extend(
                            ["independent_sources_within_tolerance", "highest_authority_value_selected_without_averaging"]
                        )
                    else:
                        verdict = "unresolved_conflict"
                        conflicts = representatives
                        reason_codes.append("claim_value_outside_consensus_tolerance")
                elif len(representatives) >= 2:
                    top, second = representatives[0], representatives[1]
                    authoritative = _official_can_decide(top, metric) and (
                        float(top["authority"]) - float(second["authority"]) >= AUTHORITY_MARGIN
                    )
                    top_matches = abs(float(top["canonical_value"]) - claim_number) <= tolerance[
                        "effective_absolute"
                    ]
                    if authoritative and top_matches:
                        verdict = "verified_authoritative"
                        selected = [top]
                        conflicts = representatives[1:]
                        decision_value = {
                            "number": float(top["canonical_value"]),
                            "unit": claim_scope[0],
                            "currency": claim_scope[1],
                            "adjustment": claim_scope[2],
                            "source_evidence_id": top.get("evidence_id"),
                        }
                        reason_codes.extend(
                            ["authoritative_source_overrides_conflict", "conflicting_values_not_averaged"]
                        )
                    else:
                        verdict = "unresolved_conflict"
                        conflicts = representatives
                        reason_codes.append("independent_sources_exceed_tolerance")
                elif representatives:
                    top = representatives[0]
                    top_matches = abs(float(top["canonical_value"]) - claim_number) <= tolerance[
                        "effective_absolute"
                    ]
                    if _official_can_decide(top, metric) and top_matches:
                        verdict = "verified_authoritative"
                        selected = [top]
                        decision_value = {
                            "number": float(top["canonical_value"]),
                            "unit": claim_scope[0],
                            "currency": claim_scope[1],
                            "adjustment": claim_scope[2],
                            "source_evidence_id": top.get("evidence_id"),
                        }
                        reason_codes.append("single_authoritative_source")
                    else:
                        verdict = "single_source"
                        selected = [top]
                        reason_codes.append(
                            "single_independent_source" if top_matches else "single_source_disagrees_with_claim"
                        )
        else:
            tolerance = self._tolerance(metric, [])
            if not reason_codes:
                reason_codes.append("no_eligible_evidence")

        if "tolerance" not in locals():
            tolerance = self._tolerance(metric, [])
        independence_groups = []
        grouped = defaultdict(list)
        for item in accepted:
            grouped[str(item.get("independence_key") or "unknown")].append(item)
        for key in sorted(grouped):
            independence_groups.append(
                {
                    "independence_key": key,
                    "evidence_ids": sorted(
                        int(item["evidence_id"])
                        for item in grouped[key]
                        if item.get("evidence_id") is not None
                    ),
                    "provider_ids": sorted(
                        {str(item.get("provider_id") or "") for item in grouped[key]}
                    ),
                }
            )
        input_evidence = [self._public_input(item) for item in accepted]
        snapshot_hashes = sorted(
            [
                {
                    "snapshot_id": int(item["snapshot_id"]),
                    "payload_sha256": str(item["payload_sha256"]),
                }
                for item in accepted
                if item.get("snapshot_id") is not None and item.get("payload_sha256")
            ],
            key=lambda item: item["snapshot_id"],
        )
        result = {
            "schema_version": FINANCIAL_CONFLICT_VERDICT_SCHEMA_VERSION,
            "judge_version": FINANCIAL_CONFLICT_JUDGE_VERSION,
            "rule_version": FINANCIAL_CONFLICT_JUDGE_VERSION,
            "claim_key": normalized_claim["claim_key"],
            "claim_dimension": {
                "instrument_key": normalized_claim["subject"],
                "metric": metric,
                "value": claim_number,
                "unit": claim_scope[0],
                "currency": claim_scope[1],
                "adjustment": claim_scope[2],
                "period_key": claim_scope[3],
            },
            "verdict": verdict,
            "reason_codes": list(dict.fromkeys(reason_codes)),
            "tolerance": tolerance,
            "decision_value": decision_value,
            "selected_evidence_ids": sorted(
                int(item["evidence_id"])
                for item in selected
                if item.get("evidence_id") is not None
            ),
            "conflicting_evidence_ids": sorted(
                int(item["evidence_id"])
                for item in conflicts
                if item.get("evidence_id") is not None
            ),
            "ignored_evidence": sorted(
                ignored,
                key=lambda item: (
                    item.get("evidence_id") is None,
                    item.get("evidence_id") or 0,
                    item["reason"],
                ),
            ),
            "independence_groups": independence_groups,
            "input_evidence": input_evidence,
            "input_snapshot_hashes": snapshot_hashes,
            "human_review_required": verdict == "unresolved_conflict",
            "boundaries": {
                "different_currency_compared": False,
                "different_adjustment_compared": False,
                "implicit_fx_conversion": False,
                "prices_averaged": False,
                "provider_names_equal_independent_sources": False,
                "unresolved_conflict_is_current_fact": False,
                "model_calls": 0,
                "network_calls": 0,
            },
        }
        _VERDICT_VALIDATOR.validate(result)
        return result


class FinancialConflictJudgeService:
    """Resolve persisted evidence and expose unresolved verdicts as a review queue."""

    def __init__(self, database, *, judge: FinancialConflictJudge | None = None):
        self.database = database
        self.judge = judge or FinancialConflictJudge()

    @property
    def connection(self):
        self.database._ensure_connection()
        return self.database.connection

    def _claim(self, claim_id: int):
        with self.database.lock:
            row = self.connection.execute(
                """
                SELECT claim.id, claim.claim_key, claim.claim_type, claim.subject,
                       claim.statement, claim.normalized_value_json, claim.unit,
                       claim.currency, claim.verification_status,
                       instrument.canonical_symbol, instrument.asset_type,
                       instrument.market, instrument.exchange, instrument.country_code
                FROM financial_claims claim
                JOIN financial_research_runs run ON run.id=claim.research_run_id
                LEFT JOIN financial_instruments instrument ON instrument.id=run.instrument_id
                WHERE claim.id=?
                """,
                (int(claim_id),),
            ).fetchone()
        if row is None:
            return None
        keys = (
            "id", "claim_key", "claim_type", "subject", "statement",
            "normalized_value_json", "unit", "currency", "verification_status",
            "canonical_symbol", "asset_type", "market", "exchange", "country_code",
        )
        return dict(zip(keys, row))

    def _temporal_context(self, claim_id: int) -> tuple[str, set[int], set[int]]:
        with self.database.lock:
            row = self.connection.execute(
                """
                SELECT verdict, rationale
                FROM financial_verdicts
                WHERE claim_id=? AND adjudicator='financial-temporal-judge-v1'
                ORDER BY adjudication_version DESC LIMIT 1
                """,
                (int(claim_id),),
            ).fetchone()
        if row is None:
            return "", set(), set()
        rationale = _json_object(row[1])
        selected = {
            int(value)
            for value in rationale.get("selected_evidence_ids") or []
            if str(value).isdigit()
        }
        ignored = {
            int(value)
            for value in rationale.get("ignored_evidence_ids") or []
            if str(value).isdigit()
        }
        return str(row[0] or ""), selected, ignored

    def _evidence(self, claim_id: int) -> list[dict]:
        temporal_status, temporal_ids, temporal_ignored_ids = self._temporal_context(claim_id)
        with self.database.lock:
            rows = self.connection.execute(
                """
                SELECT evidence.id, evidence.evidence_type, evidence.relationship,
                       evidence.evidence_json, evidence.authority_score,
                       evidence.observed_at, evidence.fetched_at, evidence.snapshot_id,
                       evidence.source_url, profile.provider_key, profile.provider_type,
                       profile.priority, profile.access_tier, profile.health_status,
                       profile.metadata_json, snapshot.observed_at, snapshot.fetched_at,
                       snapshot.currency, snapshot.timezone, snapshot.quality_status,
                       snapshot.payload_json, snapshot.payload_sha256,
                       instrument.canonical_symbol, instrument.asset_type,
                       instrument.market, instrument.exchange, instrument.country_code,
                       snapshot.source_url
                FROM financial_claim_evidence evidence
                LEFT JOIN financial_data_snapshots snapshot ON snapshot.id=evidence.snapshot_id
                LEFT JOIN financial_provider_profiles profile
                       ON profile.id=COALESCE(evidence.provider_profile_id, snapshot.provider_profile_id)
                LEFT JOIN financial_instruments instrument ON instrument.id=snapshot.instrument_id
                WHERE evidence.claim_id=?
                ORDER BY evidence.id
                """,
                (int(claim_id),),
            ).fetchall()
        result = []
        for row in rows:
            item = _json_object(row[3])
            item.update(
                {
                    "evidence_id": int(row[0]),
                    "evidence_type": str(row[1] or ""),
                    "relationship": str(row[2] or ""),
                    "authority_score": row[4],
                    "snapshot_id": int(row[7]) if row[7] is not None else None,
                    "source_url": str(row[8] or row[27] or item.get("source_url") or ""),
                    "provider_id": str(row[9] or item.get("provider_id") or ""),
                    "provider_type": str(row[10] or ""),
                    "provider_priority": row[11],
                    "access_tier": str(row[12] or ""),
                    "provider_health": str(row[13] or ""),
                    "provider_metadata": _json_object(row[14]),
                    "temporal_status": (
                        temporal_status
                        if int(row[0]) in temporal_ids
                        or (
                            temporal_status in ELIGIBLE_TEMPORAL_STATUSES
                            and int(row[0]) not in temporal_ignored_ids
                        )
                        else "unknown"
                    ),
                }
            )
            if row[7] is not None:
                payload_text = str(row[20] or "")
                payload = _json_object(payload_text)
                normalized = _mapping(payload.get("normalized_payload"))
                item["integrity_valid"] = (
                    hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
                    == str(row[21] or "")
                )
                item.update(
                    {
                        "observed_at": str(row[15] or row[5] or ""),
                        "fetched_at": str(row[16] or row[6] or ""),
                        "currency": str(payload.get("currency") or row[17] or ""),
                        "timezone": str(payload.get("timezone") or row[18] or ""),
                        "availability_status": str(
                            payload.get("availability_status")
                            or ("available" if not str(row[19] or "").casefold().startswith("permission") else "permission_denied")
                        ),
                        "payload_sha256": str(row[21] or ""),
                        "instrument_key": str(item.get("instrument_key") or ""),
                    }
                )
                for key in (
                    "metric", "value", "unit", "adjustment", "period", "lineage",
                    "underlying_source_id", "source_family",
                ):
                    if item.get(key) in (None, "", {}):
                        item[key] = payload.get(key, normalized.get(key))
            else:
                item["observed_at"] = str(row[5] or item.get("observed_at") or "")
                item["fetched_at"] = str(row[6] or item.get("fetched_at") or "")
                item.setdefault("availability_status", "available")
                item.setdefault("integrity_valid", True)
            result.append(item)
        return result

    @staticmethod
    def _claim_status(result: Mapping[str, object], temporal_status: str) -> str:
        verdict = str(result.get("verdict") or "")
        if verdict in {"verified_consensus", "verified_authoritative"}:
            return temporal_status if temporal_status in ELIGIBLE_TEMPORAL_STATUSES else "insufficient_evidence"
        if verdict == "unresolved_conflict":
            return "conflicted"
        return "insufficient_evidence"

    def judge_and_persist_claim(self, claim_id: int) -> dict:
        claim = self._claim(int(claim_id))
        if claim is None:
            return {"status": "failed", "claim_id": int(claim_id), "error": "claim_not_found"}
        temporal_status, _temporal_ids, _temporal_ignored_ids = self._temporal_context(
            int(claim_id)
        )
        result = self.judge.judge(claim, self._evidence(int(claim_id)))
        decision_payload = {
            key: result[key]
            for key in (
                "judge_version", "rule_version", "claim_dimension", "verdict",
                "reason_codes", "tolerance", "decision_value",
                "selected_evidence_ids", "conflicting_evidence_ids", "ignored_evidence",
                "independence_groups", "input_evidence", "input_snapshot_hashes",
                "human_review_required",
            )
        }
        decision_hash = hashlib.sha256(_json(decision_payload).encode("utf-8")).hexdigest()
        rationale = _json({**decision_payload, "decision_hash": decision_hash})
        decided_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.database.lock:
            self.connection.execute("SAVEPOINT financial_conflict_judge")
            try:
                previous = self.connection.execute(
                    """
                    SELECT id, adjudication_version, rationale
                    FROM financial_verdicts
                    WHERE claim_id=? AND adjudicator=?
                    ORDER BY adjudication_version DESC LIMIT 1
                    """,
                    (int(claim_id), FINANCIAL_CONFLICT_JUDGE_VERSION),
                ).fetchone()
                previous_payload = _json_object(previous[2]) if previous else {}
                if previous and previous_payload.get("decision_hash") == decision_hash:
                    verdict_id, version, persisted = int(previous[0]), int(previous[1]), False
                else:
                    version = int(
                        self.connection.execute(
                            "SELECT COALESCE(MAX(adjudication_version),0)+1 FROM financial_verdicts WHERE claim_id=?",
                            (int(claim_id),),
                        ).fetchone()[0]
                    )
                    cursor = self.connection.execute(
                        """
                        INSERT INTO financial_verdicts(
                            claim_id, adjudication_version, verdict, rationale,
                            selected_evidence_ids_json, conflicting_evidence_ids_json,
                            adjudicator, model_id, decided_at
                        ) VALUES(?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            int(claim_id), version, result["verdict"], rationale,
                            _json(result["selected_evidence_ids"]),
                            _json(result["conflicting_evidence_ids"]),
                            FINANCIAL_CONFLICT_JUDGE_VERSION, "", decided_at,
                        ),
                    )
                    verdict_id, persisted = int(cursor.lastrowid), True
                final_status = self._claim_status(result, temporal_status)
                if str(claim.get("claim_type") or "").casefold() == "fact":
                    self.connection.execute(
                        "UPDATE financial_claims SET verification_status=?, updated_at=? WHERE id=?",
                        (final_status, decided_at, int(claim_id)),
                    )
                self.connection.execute("RELEASE SAVEPOINT financial_conflict_judge")
            except Exception:
                self.connection.execute("ROLLBACK TO SAVEPOINT financial_conflict_judge")
                self.connection.execute("RELEASE SAVEPOINT financial_conflict_judge")
                raise
        return {
            **result,
            "claim_id": int(claim_id),
            "verdict_id": verdict_id,
            "adjudication_version": version,
            "claim_verification_status": final_status,
            "persisted": persisted,
        }

    def list_pending_human_review(self, *, limit: int = 100) -> list[dict]:
        bounded = max(1, min(int(limit), 500))
        with self.database.lock:
            rows = self.connection.execute(
                """
                WITH latest AS (
                    SELECT claim_id, MAX(adjudication_version) AS version
                    FROM financial_verdicts
                    WHERE adjudicator=?
                    GROUP BY claim_id
                )
                SELECT verdict.id, verdict.claim_id, verdict.adjudication_version,
                       verdict.rationale, verdict.decided_at, claim.claim_key,
                       claim.subject, claim.statement
                FROM latest
                JOIN financial_verdicts verdict
                  ON verdict.claim_id=latest.claim_id
                 AND verdict.adjudication_version=latest.version
                JOIN financial_claims claim ON claim.id=verdict.claim_id
                WHERE verdict.adjudicator=? AND verdict.verdict='unresolved_conflict'
                ORDER BY verdict.decided_at, verdict.id
                LIMIT ?
                """,
                (FINANCIAL_CONFLICT_JUDGE_VERSION, FINANCIAL_CONFLICT_JUDGE_VERSION, bounded),
            ).fetchall()
        return [
            {
                "verdict_id": int(row[0]),
                "claim_id": int(row[1]),
                "adjudication_version": int(row[2]),
                "decision": _json_object(row[3]),
                "decided_at": str(row[4]),
                "claim_key": str(row[5]),
                "subject": str(row[6]),
                "statement": str(row[7]),
            }
            for row in rows
        ]


def validate_conflict_verdict(payload: Mapping[str, object]) -> dict:
    result = dict(payload)
    _VERDICT_VALIDATOR.validate(result)
    return result


__all__ = [
    "CONFLICT_VERDICTS",
    "CONFLICT_VERDICT_SCHEMA",
    "FINANCIAL_CONFLICT_JUDGE_VERSION",
    "FINANCIAL_CONFLICT_VERDICT_SCHEMA_VERSION",
    "FinancialConflictJudge",
    "FinancialConflictJudgeService",
    "validate_conflict_verdict",
]

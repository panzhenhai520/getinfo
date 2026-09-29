#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Read-only metrics derived from persisted latest-information route audits."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Mapping

from jsonschema import Draft202012Validator


UTC = timezone.utc
FINANCIAL_LATEST_OBSERVABILITY_VERSION = "financial-latest-observability-v1"
METRIC_DEFINITIONS = (
    ("financial_information_needs_total", "counter", ("channels", "status")),
    ("financial_instrument_discovery_total", "counter", ("status", "source")),
    ("financial_instrument_promotion_total", "counter", ("status", "reason")),
    ("financial_latest_channel_latency_ms", "histogram", ("channel",)),
    ("financial_latest_bundle_total", "counter", ("status",)),
    ("financial_future_evidence_rejected_total", "counter", ("kind",)),
    ("financial_generic_model_blocked_total", "counter", ("reason",)),
    ("financial_timezone_fallback_total", "counter", ("reason",)),
)
FINANCIAL_LATEST_OBSERVABILITY_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "schema_version", "generated_at_utc", "window", "metric_definitions",
        "counters", "histograms",
    ],
    "properties": {
        "schema_version": {"const": FINANCIAL_LATEST_OBSERVABILITY_VERSION},
        "generated_at_utc": {"type": "string"},
        "window": {
            "type": "object",
            "required": ["requested_limit", "route_count", "malformed_route_count"],
            "properties": {
                "requested_limit": {"type": "integer", "minimum": 1},
                "route_count": {"type": "integer", "minimum": 0},
                "malformed_route_count": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": False,
        },
        "metric_definitions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "type", "label_names"],
                "properties": {
                    "name": {"type": "string"},
                    "type": {"enum": ["counter", "histogram"]},
                    "label_names": {
                        "type": "array", "items": {"type": "string"},
                    },
                },
                "additionalProperties": False,
            },
        },
        "counters": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "labels", "value"],
                "properties": {
                    "name": {"type": "string"},
                    "labels": {"type": "object"},
                    "value": {"type": "integer", "minimum": 0},
                },
                "additionalProperties": False,
            },
        },
        "histograms": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "name", "labels", "count", "min_ms", "p50_ms", "p95_ms",
                    "max_ms",
                ],
                "properties": {
                    "name": {"type": "string"},
                    "labels": {"type": "object"},
                    "count": {"type": "integer", "minimum": 1},
                    "min_ms": {"type": "integer", "minimum": 0},
                    "p50_ms": {"type": "integer", "minimum": 0},
                    "p95_ms": {"type": "integer", "minimum": 0},
                    "max_ms": {"type": "integer", "minimum": 0},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}
_VALIDATOR = Draft202012Validator(FINANCIAL_LATEST_OBSERVABILITY_SCHEMA)


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("latest observability clock must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_label(value: object, default: str = "unknown") -> str:
    text = str(value or "").strip().casefold()
    if not text:
        return default
    return "".join(character for character in text[:80] if character.isalnum() or character in "_+.-") or default


def _reason_codes(payload: object) -> tuple[str, ...]:
    if not isinstance(payload, Mapping):
        return ()
    return tuple(
        _safe_label(item)
        for item in payload.get("reason_codes") or []
        if str(item or "").strip()
    )


def _percentile(values: list[int], percentile: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percentile) - 1)]


def validate_financial_latest_observability(payload: Mapping[str, object]) -> dict:
    result = dict(payload)
    _VALIDATOR.validate(result)
    return result


class FinancialLatestObservabilityService:
    """Aggregate stable labels only; questions, sessions and evidence stay private."""

    def __init__(self, database, *, clock=None):
        self.database = database
        self.clock = clock or (lambda: datetime.now(UTC))

    @property
    def connection(self):
        ensure = getattr(self.database, "_ensure_connection", None)
        if callable(ensure):
            ensure()
        return getattr(self.database, "connection", self.database)

    def _lock(self):
        lock = getattr(self.database, "lock", None)
        return lock if lock is not None else nullcontext()

    @staticmethod
    def _increment(counter: Counter, name: str, **labels) -> None:
        counter[(name, tuple(sorted((key, _safe_label(value)) for key, value in labels.items())))] += 1

    def snapshot(self, *, limit: int = 1000) -> dict:
        requested_limit = max(1, min(int(limit), 10000))
        captured = self.clock()
        generated_at = _utc_text(captured)
        with self._lock():
            rows = self.connection.execute(
                "SELECT financial_attributes_json FROM chat_financial_routes "
                "ORDER BY id DESC LIMIT ?",
                (requested_limit,),
            ).fetchall()

        counters = Counter()
        latencies: dict[str, list[int]] = defaultdict(list)
        malformed = 0
        for row in rows:
            try:
                attributes = json.loads(str(row[0] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                malformed += 1
                continue
            if not isinstance(attributes, Mapping):
                malformed += 1
                continue

            needs = attributes.get("information_needs") or {}
            channels = "+".join(
                sorted(_safe_label(item) for item in needs.get("channels") or [])
            ) or "none"
            self._increment(
                counters,
                "financial_information_needs_total",
                channels=channels,
                status=needs.get("status") or "missing",
            )

            discovery = attributes.get("instrument_discovery") or {}
            discovery_status = _safe_label(discovery.get("status"), "missing")
            sources = "+".join(
                sorted(_safe_label(item) for item in discovery.get("attempted_sources") or [])
            ) or "none"
            self._increment(
                counters,
                "financial_instrument_discovery_total",
                status=discovery_status,
                source=sources,
            )
            if discovery_status not in {"missing", "planned", "skipped"}:
                promotion_status = "promoted" if discovery_status == "promoted" else "not_promoted"
                reasons = _reason_codes(discovery)
                self._increment(
                    counters,
                    "financial_instrument_promotion_total",
                    status=promotion_status,
                    reason=reasons[0] if reasons else discovery_status,
                )

            bundle = attributes.get("latest_bundle") or {}
            bundle_status = _safe_label(bundle.get("status"), "missing")
            self._increment(
                counters,
                "financial_latest_bundle_total",
                status=bundle_status,
            )
            execution_channels = (bundle.get("execution") or {}).get("channels") or {}
            if isinstance(execution_channels, Mapping):
                for channel, timing in execution_channels.items():
                    if not isinstance(timing, Mapping):
                        continue
                    elapsed = timing.get("elapsed_ms")
                    if isinstance(elapsed, bool):
                        continue
                    try:
                        elapsed_ms = max(0, int(elapsed))
                    except (TypeError, ValueError):
                        continue
                    latencies[_safe_label(channel)].append(elapsed_ms)

            news = attributes.get("news_query") or {}
            realtime = attributes.get("realtime_query") or {}
            future_kinds = set()
            if any("future" in reason for reason in _reason_codes(news)):
                future_kinds.add("news")
            if any("future" in reason for reason in _reason_codes(realtime)):
                future_kinds.add("quote")
            if not future_kinds:
                latest = bundle.get("latest_available") or {}
                try:
                    rejected = int(latest.get("future_records_rejected") or 0)
                except (TypeError, ValueError):
                    rejected = 0
                if rejected > 0:
                    future_kinds.add("combined")
            for kind in future_kinds:
                self._increment(
                    counters,
                    "financial_future_evidence_rejected_total",
                    kind=kind,
                )

            target = attributes.get("target_resolution") or {}
            actionable = set(needs.get("channels") or []).intersection({"quote", "news"})
            blocked_reason = ""
            if str(needs.get("status") or "") == "planned" and actionable:
                if str(target.get("status") or "") != "resolved":
                    blocked_reason = "unresolved_target"
                elif bundle_status == "unavailable":
                    blocked_reason = "bundle_unavailable"
                elif "news" in actionable and str(news.get("status") or "") == "unavailable" and "quote" not in actionable:
                    blocked_reason = "news_unavailable"
                elif "quote" in actionable and str(realtime.get("status") or "") in {"conflict", "unavailable"} and "news" not in actionable:
                    blocked_reason = "quote_unavailable"
            if blocked_reason:
                self._increment(
                    counters,
                    "financial_generic_model_blocked_total",
                    reason=blocked_reason,
                )

            timezone_source = _safe_label(
                (attributes.get("server_time_context") or {}).get("user_timezone_source"),
                "missing",
            )
            if timezone_source != "client_hint":
                self._increment(
                    counters,
                    "financial_timezone_fallback_total",
                    reason=timezone_source,
                )

        counter_items = [
            {"name": name, "labels": dict(labels), "value": int(value)}
            for (name, labels), value in sorted(counters.items())
        ]
        histogram_items = []
        for channel, values in sorted(latencies.items()):
            histogram_items.append({
                "name": "financial_latest_channel_latency_ms",
                "labels": {"channel": channel},
                "count": len(values),
                "min_ms": min(values),
                "p50_ms": _percentile(values, 0.50),
                "p95_ms": _percentile(values, 0.95),
                "max_ms": max(values),
            })
        return validate_financial_latest_observability({
            "schema_version": FINANCIAL_LATEST_OBSERVABILITY_VERSION,
            "generated_at_utc": generated_at,
            "window": {
                "requested_limit": requested_limit,
                "route_count": len(rows),
                "malformed_route_count": malformed,
            },
            "metric_definitions": [
                {"name": name, "type": metric_type, "label_names": list(labels)}
                for name, metric_type, labels in METRIC_DEFINITIONS
            ],
            "counters": counter_items,
            "histograms": histogram_items,
        })


__all__ = [
    "FINANCIAL_LATEST_OBSERVABILITY_SCHEMA",
    "FINANCIAL_LATEST_OBSERVABILITY_VERSION",
    "FinancialLatestObservabilityService",
    "METRIC_DEFINITIONS",
    "validate_financial_latest_observability",
]

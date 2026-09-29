#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Safe optional SSE events for financial chat routes.

The existing ``status/chunk/done/error`` vocabulary remains the transport
contract for natural-language answers.  This module only projects allow-listed
route, progress, evidence and report references.  It deliberately never
serializes an internal route plan wholesale, so model credentials, prompts and
provider configuration cannot leak into the browser stream.
"""

from __future__ import annotations

import json
from typing import Mapping, Optional, Sequence

from financial_security import (
    redact_public_payload,
    redact_sensitive_text,
    safe_public_url,
)


FINANCIAL_SSE_PROTOCOL_VERSION = "financial-sse-v1"
FINANCIAL_OPTIONAL_SSE_EVENT_TYPES = (
    "route",
    "clarification",
    "research_status",
    "sources",
    "report_ready",
)
_ALL_EVENT_TYPES = frozenset(
    {
        "status",
        "searching",
        "search_done",
        "chunk",
        "done",
        "error",
        *FINANCIAL_OPTIONAL_SSE_EVENT_TYPES,
    }
)
_TARGET_FIELDS = (
    "instrument_id",
    "canonical_symbol",
    "display_name",
    "asset_type",
    "market",
    "exchange",
    "currency",
    "country_code",
    "share_class",
)
_PUBLIC_EVENT_FIELDS = {
    "status": frozenset({"type", "message", "request_id", "stage"}),
    "searching": frozenset({"type", "message", "request_id", "query"}),
    "search_done": frozenset({"type", "message", "request_id", "count"}),
    "chunk": frozenset({"type", "content"}),
    "done": frozenset({"type", "message", "request_id", "financial_route_key"}),
    "error": frozenset({"type", "message", "code", "request_id"}),
    "route": frozenset({
        "type", "protocol_version", "request_id", "route_destination",
        "intent", "freshness", "targets", "universe", "server_time",
    }),
    "clarification": frozenset({
        "type", "protocol_version", "request_id", "clarification_id",
        "field", "question", "options",
    }),
    "research_status": frozenset({
        "type", "protocol_version", "request_id", "stage", "status",
        "message", "research_run_ids", "job_ids",
    }),
    "sources": frozenset({"type", "protocol_version", "request_id", "sources"}),
    "report_ready": frozenset({"type", "protocol_version", "request_id", "reports"}),
}


def _text(value: object, maximum: int = 500) -> str:
    return redact_sensitive_text(value, maximum=maximum).strip()


def _safe_url(value: object, *, local_allowed: bool = False) -> str:
    prefixes = ("/api/financial/",) if local_allowed else ()
    return safe_public_url(value, local_prefixes=prefixes)


def _target(value: object) -> dict:
    if not isinstance(value, Mapping):
        return {}
    result = {}
    for key in _TARGET_FIELDS:
        item = value.get(key)
        if item is None or item == "":
            continue
        if key == "instrument_id":
            try:
                result[key] = int(item)
            except (TypeError, ValueError):
                continue
        else:
            result[key] = _text(item, 200)
    return result


def _route_destination(plan) -> str:
    for field in (
        "full_research", "latest_bundle", "realtime_query", "news_query",
        "market_scope", "instrument_discovery", "target_resolution",
    ):
        value = getattr(plan, field, {})
        if not isinstance(value, Mapping):
            continue
        status = _text(value.get("status"), 80)
        destination = _text(value.get("route_destination"), 120)
        if status not in {"", "skipped", "no_target"} and destination:
            return destination
    return "financial_intent"


def is_financial_plan(plan) -> bool:
    intent = getattr(plan, "financial_intent", {})
    return isinstance(intent, Mapping) and bool(intent.get("is_financial"))


def route_event(plan) -> Optional[dict]:
    if not is_financial_plan(plan):
        return None
    intent = plan.financial_intent
    resolution = plan.target_resolution
    clock = plan.server_time_context
    targets = [
        safe
        for safe in (_target(item) for item in resolution.get("targets") or [])
        if safe
    ]
    universe = _text(intent.get("universe"), 120)
    if not universe:
        universe = _text((plan.market_scope.get("universe") or {}).get("universe_key"), 120)
    return {
        "type": "route",
        "protocol_version": FINANCIAL_SSE_PROTOCOL_VERSION,
        "request_id": _text(plan.audit_route_key, 100),
        "route_destination": _route_destination(plan),
        "intent": _text(intent.get("intent"), 80),
        "freshness": _text(intent.get("freshness"), 40),
        "targets": targets,
        "universe": universe,
        "server_time": {
            "server_now_utc": _text(clock.get("server_now_utc"), 80),
            "server_timezone": _text(clock.get("server_timezone"), 80),
            "user_timezone": _text(clock.get("user_timezone"), 80),
            "clock_source": "application_server",
        },
    }


def clarification_event(plan) -> Optional[dict]:
    resolution = plan.target_resolution
    if _text(resolution.get("status")) != "clarification_required":
        return None
    clarification = resolution.get("clarification") or {}
    options = [
        safe
        for safe in (_target(item) for item in clarification.get("options") or [])
        if safe
    ]
    return {
        "type": "clarification",
        "protocol_version": FINANCIAL_SSE_PROTOCOL_VERSION,
        "request_id": _text(plan.audit_route_key, 100),
        "clarification_id": _text(clarification.get("clarification_id"), 160),
        "field": _text(clarification.get("field"), 80),
        "question": _text(clarification.get("question"), 1000),
        "options": options,
    }


def research_status_event(
    plan,
    *,
    stage: str,
    status: str,
    message: str,
    result: Optional[Mapping[str, object]] = None,
) -> dict:
    current = result if isinstance(result, Mapping) else {}
    run_ids = sorted(
        {
            _text(item, 160)
            for item in current.get("research_run_ids") or []
            if _text(item, 160)
        }
    )
    job_ids = []
    for item in current.get("jobs") or []:
        if not isinstance(item, Mapping) or item.get("job_id") is None:
            continue
        try:
            job_ids.append(int(item["job_id"]))
        except (TypeError, ValueError):
            continue
    return {
        "type": "research_status",
        "protocol_version": FINANCIAL_SSE_PROTOCOL_VERSION,
        "request_id": _text(plan.audit_route_key, 100),
        "stage": _text(stage, 80),
        "status": _text(status, 80),
        "message": _text(message, 500),
        "research_run_ids": run_ids,
        "job_ids": sorted(set(job_ids)),
    }


def _source_record(item: Mapping[str, object]) -> Optional[dict]:
    kind = _text(item.get("source_kind") or item.get("evidence_kind"), 80)
    if not kind:
        return None
    record = {
        "source_kind": kind,
        "reference_id": _text(
            item.get("reference_id") or item.get("snapshot_id") or item.get("article_id"),
            100,
        ),
        "provider": _text(
            item.get("provider") or item.get("provider_id") or item.get("provider_key"),
            120,
        ),
        "title": _text(
            item.get("title") or item.get("provider_display_name") or item.get("attribution_text"),
            500,
        ),
        "url": _safe_url(item.get("url") or item.get("source_url")),
        "observed_at": _text(item.get("observed_at"), 80),
        "fetched_at": _text(item.get("fetched_at"), 80),
        "published_at": _text(item.get("published_at"), 80),
        "published_precision": _text(item.get("published_precision"), 40),
        "published_timezone": _text(item.get("published_timezone"), 80),
        "market_status": _text(item.get("market_status"), 80),
    }
    return record


def sources_event(plan, records: Sequence[Mapping[str, object]]) -> Optional[dict]:
    sources = []
    seen = set()
    for item in records:
        if not isinstance(item, Mapping):
            continue
        record = _source_record(item)
        if record is None:
            continue
        identity = (record["source_kind"], record["reference_id"], record["url"])
        if identity in seen:
            continue
        seen.add(identity)
        sources.append(record)
        if len(sources) >= 30:
            break
    if not sources:
        return None
    return {
        "type": "sources",
        "protocol_version": FINANCIAL_SSE_PROTOCOL_VERSION,
        "request_id": _text(plan.audit_route_key, 100),
        "sources": sources,
    }


def realtime_source_records(query: Mapping[str, object]) -> list[dict]:
    return [
        {**dict(item), "source_kind": "financial_snapshot"}
        for item in query.get("evidence") or []
        if isinstance(item, Mapping)
    ]


def market_source_records(scope: Mapping[str, object]) -> list[dict]:
    report = scope.get("report") or {}
    return [
        {
            "source_kind": "financial_snapshot",
            "snapshot_id": snapshot_id,
            "title": "市场概览入库快照",
            "observed_at": report.get("observed_at"),
            "fetched_at": report.get("fetched_at"),
        }
        for snapshot_id in report.get("snapshot_ids") or []
    ]


def full_research_source_records(route: Mapping[str, object]) -> list[dict]:
    records = []
    for report in route.get("reports") or []:
        if not isinstance(report, Mapping):
            continue
        records.extend(
            dict(item)
            for item in report.get("source_refs") or []
            if isinstance(item, Mapping)
        )
    return records


def report_ready_event(plan, reports: Sequence[Mapping[str, object]]) -> Optional[dict]:
    result = []
    for item in reports:
        if not isinstance(item, Mapping):
            continue
        try:
            report_id = int(item.get("report_id"))
        except (TypeError, ValueError):
            continue
        if report_id < 1:
            continue
        target = _target(item.get("target"))
        result.append(
            {
                "report_id": report_id,
                "research_run_id": _text(item.get("research_run_id"), 160),
                "report_status": _text(item.get("report_status"), 80),
                "title": _text(item.get("title") or "TradingAgents 终极报告", 500),
                "observed_at": _text(item.get("observed_at"), 80),
                "verified_at": _text(item.get("verified_at"), 80),
                "target": target,
                "report_url": f"/api/financial/reports/{report_id}",
            }
        )
    if not result:
        return None
    return {
        "type": "report_ready",
        "protocol_version": FINANCIAL_SSE_PROTOCOL_VERSION,
        "request_id": _text(plan.audit_route_key, 100),
        "reports": result,
    }


def encode_sse_event(event: Mapping[str, object]) -> str:
    source = dict(event)
    event_type = _text(source.get("type"), 80)
    if event_type not in _ALL_EVENT_TYPES:
        raise ValueError("unsupported SSE event type")
    payload = {
        key: value
        for key, value in source.items()
        if key in _PUBLIC_EVENT_FIELDS[event_type]
    }
    payload["type"] = event_type
    payload = redact_public_payload(payload)
    return "data: " + json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n\n"


__all__ = [
    "FINANCIAL_OPTIONAL_SSE_EVENT_TYPES",
    "FINANCIAL_SSE_PROTOCOL_VERSION",
    "clarification_event",
    "encode_sse_event",
    "full_research_source_records",
    "is_financial_plan",
    "market_source_records",
    "realtime_source_records",
    "report_ready_event",
    "research_status_event",
    "route_event",
    "sources_event",
]

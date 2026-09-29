#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Backward-compatible route seam in front of the existing chat generator.

Task 3.1 deliberately keeps every request on ``legacy_chat``.  Later tasks may
attach financial classification to this in-process seam without changing the
public endpoint or replacing the current SSE generator.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Callable, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from financial_market_clock import (
    MarketClockService,
    NOW_EXPRESSIONS,
    RequestTimeContext,
    THIS_WEEK_EXPRESSIONS,
    TODAY_EXPRESSIONS,
    YESTERDAY_EXPRESSIONS,
)
from financial_intent_classifier import financial_classification_gate
from financial_rollout import rollout_capability_enabled, rollout_capability_reason
from financial_chat_market_scope import skipped_market_scope
from financial_realtime_query import skipped_realtime_query
from financial_full_research import (
    skipped_full_research,
    unavailable_full_research,
)
from financial_sse import (
    FINANCIAL_OPTIONAL_SSE_EVENT_TYPES,
    FINANCIAL_SSE_PROTOCOL_VERSION,
)


LEGACY_CHAT_ROUTE = "legacy_chat"
LEGACY_SSE_EVENT_TYPES: Tuple[str, ...] = (
    "status",
    "searching",
    "search_done",
    "chunk",
    "done",
    "error",
)
FINANCIAL_SSE_EVENT_TYPES: Tuple[str, ...] = (
    *LEGACY_SSE_EVENT_TYPES,
    *FINANCIAL_OPTIONAL_SSE_EVENT_TYPES,
)
SERVER_TIME_CONTEXT_VERSION = "chat-server-time-v1"
UTC = timezone.utc
_TIME_EXPRESSIONS = tuple(
    sorted(
        TODAY_EXPRESSIONS
        | YESTERDAY_EXPRESSIONS
        | THIS_WEEK_EXPRESSIONS
        | NOW_EXPRESSIONS,
        key=lambda item: (-len(item), item),
    )
)


@dataclass(frozen=True)
class ChatRoutePlan:
    audit_route_key: str
    route_key: str
    stream_protocol_version: str
    public_event_types: Tuple[str, ...]
    requested_model: str
    web_search: bool
    server_time_context: Mapping[str, object]
    time_resolution: Mapping[str, object]
    financial_intent: Mapping[str, object]
    target_resolution: Mapping[str, object]
    market_scope: Mapping[str, object]
    realtime_query: Mapping[str, object]
    full_research: Mapping[str, object]
    information_needs: Mapping[str, object] = field(default_factory=dict)
    instrument_discovery: Mapping[str, object] = field(default_factory=dict)
    news_query: Mapping[str, object] = field(default_factory=dict)
    latest_bundle: Mapping[str, object] = field(default_factory=dict)

    def to_internal_dict(self) -> dict:
        """Return non-secret diagnostics; this is not emitted to old clients."""

        return {
            "audit_route_key": self.audit_route_key,
            "route_key": self.route_key,
            "stream_protocol_version": self.stream_protocol_version,
            "public_event_types": list(self.public_event_types),
            "requested_model": self.requested_model,
            "web_search": self.web_search,
            "server_time_context": dict(self.server_time_context),
            "time_resolution": dict(self.time_resolution),
            "financial_intent": dict(self.financial_intent),
            "target_resolution": dict(self.target_resolution),
            "market_scope": dict(self.market_scope),
            "realtime_query": dict(self.realtime_query),
            "full_research": dict(self.full_research),
            "information_needs": dict(self.information_needs),
            "instrument_discovery": dict(self.instrument_discovery),
            "news_query": dict(self.news_query),
            "latest_bundle": dict(self.latest_bundle),
        }


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _utc_text(value: datetime) -> str:
    return _utc(value, "datetime").isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _last_user_message(payload: Mapping) -> tuple[str, int]:
    messages = payload.get("messages") or []
    if not isinstance(messages, list):
        return "", -1
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, Mapping) and message.get("role") == "user":
            return str(message.get("content") or ""), index
    return "", -1


def _expression_positions(text: str) -> list[tuple[int, str]]:
    normalized = str(text or "").casefold()
    matches = []
    for expression in _TIME_EXPRESSIONS:
        pattern = re.escape(expression.casefold())
        if expression.isascii():
            pattern = rf"(?<![a-z]){pattern}(?![a-z])"
        match = re.search(pattern, normalized)
        if match:
            matches.append((match.start(), expression))
    return sorted(matches)


class ChatFinancialRouteStore:
    """Short-transaction persistence in the existing application SQLite."""

    def __init__(self, database):
        self.database = database

    def _ensure(self):
        self.database._ensure_connection()

    def latest_time_resolution(self, session_id: str) -> Optional[dict]:
        normalized = str(session_id or "").strip()
        if not normalized:
            return None
        self._ensure()
        with self.database.lock:
            row = self.database.connection.execute(
                """
                SELECT route_key, financial_attributes_json
                FROM chat_financial_routes
                WHERE session_id=?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (normalized,),
            ).fetchone()
        if row is None:
            return None
        try:
            attributes = json.loads(str(row[1] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        resolution = attributes.get("time_resolution")
        if not isinstance(resolution, Mapping) or not resolution.get("primary_range"):
            return None
        return {**dict(resolution), "source_route_key": str(row[0])}

    def latest_target_state(self, session_id: str) -> Optional[dict]:
        """Return only the latest same-session route when it carries target state."""

        normalized = str(session_id or "").strip()
        if not normalized:
            return None
        self._ensure()
        with self.database.lock:
            row = self.database.connection.execute(
                """
                SELECT route_key, raw_question, intent, financial_attributes_json,
                       resolved_targets_json, clarification_json, route_status,
                       route_destination
                FROM chat_financial_routes
                WHERE session_id=?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (normalized,),
            ).fetchone()
        if row is None or str(row[6]) not in {
            "clarification_required", "target_resolved", "clarification_resolved",
            "realtime_query_planned", "realtime_query_ready", "realtime_query_stale",
            "realtime_query_conflict", "realtime_query_unavailable",
            "full_research_planned", "full_research_cache_hit",
            "full_research_queued", "full_research_running", "full_research_mixed",
            "full_research_failed", "full_research_cancelled",
            "full_research_unavailable",
            "latest_bundle_planned", "latest_bundle_ready",
            "latest_bundle_partial", "latest_bundle_unavailable",
        }:
            return None
        try:
            attributes = json.loads(str(row[3] or "{}"))
            targets = json.loads(str(row[4] or "[]"))
            clarification = json.loads(str(row[5] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(attributes, Mapping) or not isinstance(targets, list):
            return None
        if not isinstance(clarification, Mapping):
            clarification = {}
        return {
            "route_key": str(row[0]),
            "raw_question": str(row[1]),
            "intent": str(row[2]),
            "financial_intent": dict(attributes.get("financial_intent") or {}),
            "resolved_targets": [dict(item) for item in targets if isinstance(item, Mapping)],
            "clarification": dict(clarification),
            "route_status": str(row[6]),
            "route_destination": str(row[7]),
        }

    def persist(self, plan: ChatRoutePlan, payload: Mapping) -> int:
        self._ensure()
        question, _ = _last_user_message(payload)
        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            session_id = f"legacy:{plan.audit_route_key}"
        attributes = {
            "server_time_context": dict(plan.server_time_context),
            "time_resolution": dict(plan.time_resolution),
            "financial_intent": dict(plan.financial_intent),
            "target_resolution": dict(plan.target_resolution),
            "market_scope": dict(plan.market_scope),
            "realtime_query": dict(plan.realtime_query),
            "full_research": dict(plan.full_research),
            "information_needs": dict(plan.information_needs),
            "instrument_discovery": dict(plan.instrument_discovery),
            "news_query": dict(plan.news_query),
            "latest_bundle": dict(plan.latest_bundle),
        }
        classification_status = str(
            plan.financial_intent.get("classification_status") or "skipped"
        )
        route_status = (
            "financial_intent_classified"
            if classification_status in {"classified", "degraded"}
            and bool(plan.financial_intent.get("is_financial"))
            else "time_context_captured"
        )
        intent = str(plan.financial_intent.get("intent") or "unknown")
        target_status = str(plan.target_resolution.get("status") or "skipped")
        resumed_from = plan.target_resolution.get("resumed_from_route_key")
        if target_status == "clarification_required":
            route_status = "clarification_required"
        elif target_status == "resolved":
            route_status = "clarification_resolved" if resumed_from else "target_resolved"
            original_intent = (plan.target_resolution.get("resume_context") or {}).get(
                "original_intent"
            )
            if original_intent:
                intent = str(original_intent)
        elif target_status == "no_target":
            route_status = (
                "instrument_discovery_required"
                if str(plan.target_resolution.get("route_destination"))
                == "financial_instrument_discovery"
                else "financial_scope_pending"
            )
        elif target_status == "degraded":
            route_status = "target_resolution_degraded"
        discovery_status = str(plan.instrument_discovery.get("status") or "skipped")
        if discovery_status in {
            "planned", "verified", "promoted", "rejected", "not_found",
            "verification_required",
        }:
            route_status = f"instrument_discovery_{discovery_status}"
        news_status = str(plan.news_query.get("status") or "skipped")
        if news_status in {"planned", "ready", "unavailable", "degraded"}:
            route_status = f"news_query_{news_status}"
        bundle_status = str(plan.latest_bundle.get("status") or "skipped")
        if bundle_status in {"planned", "ready", "partial", "unavailable"}:
            route_status = f"latest_bundle_{bundle_status}"
        market_scope_status = str(plan.market_scope.get("status") or "skipped")
        if market_scope_status == "planned":
            route_status = "market_scope_planned"
        elif market_scope_status == "refresh_queued":
            route_status = "market_scope_refresh_queued"
        elif market_scope_status == "ready":
            route_status = "market_scope_ready"
        elif market_scope_status == "degraded":
            route_status = "market_scope_degraded"
        realtime_status = str(plan.realtime_query.get("status") or "skipped")
        if realtime_status == "planned":
            route_status = "realtime_query_planned"
        elif realtime_status in {"ready", "stale", "conflict", "unavailable"}:
            route_status = f"realtime_query_{realtime_status}"
        elif realtime_status == "degraded":
            route_status = "realtime_query_degraded"
        if bundle_status in {"planned", "ready", "partial", "unavailable"}:
            route_status = f"latest_bundle_{bundle_status}"
        full_research_status = str(plan.full_research.get("status") or "skipped")
        if full_research_status != "skipped":
            route_status = f"full_research_{full_research_status}"
        route_destination = str(
            plan.full_research.get("route_destination")
            if full_research_status != "skipped"
            else (
                plan.latest_bundle.get("route_destination")
                if bundle_status != "skipped"
                else (
                    plan.realtime_query.get("route_destination")
                    if realtime_status != "skipped"
                    else (
                        plan.news_query.get("route_destination")
                        if news_status != "skipped"
                        else (
                            plan.market_scope.get("route_destination")
                            if market_scope_status != "skipped"
                            else (
                                "financial_instrument_discovery"
                                if discovery_status not in {"", "skipped"}
                                else plan.target_resolution.get("route_destination") or "normal_chat"
                            )
                        )
                    )
                )
            )
        )
        targets_json = _canonical_json(plan.target_resolution.get("targets") or [])
        clarification_json = _canonical_json(
            plan.target_resolution.get("clarification") or {}
        )
        with self.database.lock:
            cursor = self.database.connection.execute(
                """
                INSERT INTO chat_financial_routes(
                    route_key, session_id, message_id, question_sha256,
                    raw_question, intent, financial_attributes_json,
                    resolved_targets_json, clarification_json, route_status,
                    route_destination, server_now, server_timezone
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(route_key) DO UPDATE SET
                    intent=excluded.intent,
                    financial_attributes_json=excluded.financial_attributes_json,
                    resolved_targets_json=excluded.resolved_targets_json,
                    clarification_json=excluded.clarification_json,
                    route_status=excluded.route_status,
                    route_destination=excluded.route_destination,
                    server_now=excluded.server_now,
                    server_timezone=excluded.server_timezone,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (
                    plan.audit_route_key,
                    session_id,
                    str(payload.get("message_id") or ""),
                    hashlib.sha256(question.encode("utf-8")).hexdigest(),
                    question,
                    intent,
                    _canonical_json(attributes),
                    targets_json,
                    clarification_json,
                    route_status,
                    route_destination,
                    str(plan.server_time_context["server_now_utc"]),
                    str(plan.server_time_context["server_timezone"]),
                ),
            )
            row = self.database.connection.execute(
                "SELECT id FROM chat_financial_routes WHERE route_key=?",
                (plan.audit_route_key,),
            ).fetchone()
            self.database.connection.commit()
        return int(row[0] if row else cursor.lastrowid)


class ChatRouteOrchestrator:
    """Capture time and plan chat without network or model side effects.

    When a route store is configured, planning may read the prior saved route
    to inherit an absolute range; writes occur only through ``persist``.
    """

    def __init__(
        self,
        *,
        clock: Optional[Callable[[], datetime]] = None,
        store: Optional[ChatFinancialRouteStore] = None,
        store_factory: Optional[Callable[[], ChatFinancialRouteStore]] = None,
        intent_classifier=None,
        intent_classifier_factory: Optional[Callable[[], object]] = None,
        target_resolver=None,
        target_resolver_factory: Optional[Callable[[], object]] = None,
        information_needs_planner=None,
        information_needs_planner_factory: Optional[Callable[[], object]] = None,
        instrument_discovery_service=None,
        instrument_discovery_service_factory: Optional[Callable[[], object]] = None,
        market_scope_router=None,
        market_scope_router_factory: Optional[Callable[[], object]] = None,
        realtime_query_service=None,
        realtime_query_service_factory: Optional[Callable[[], object]] = None,
        news_query_service=None,
        news_query_service_factory: Optional[Callable[[], object]] = None,
        full_research_router=None,
        full_research_router_factory: Optional[Callable[[], object]] = None,
        financial_settings=None,
        pack_loader=None,
        industry_pack_id: Optional[str] = None,
        server_timezone: str = "Asia/Hong_Kong",
    ):
        self.clock = clock or (lambda: datetime.now(UTC))
        self.store = store
        self.store_factory = store_factory
        self.intent_classifier = intent_classifier
        self.intent_classifier_factory = intent_classifier_factory
        self.target_resolver = target_resolver
        self.target_resolver_factory = target_resolver_factory
        self.information_needs_planner = information_needs_planner
        self.information_needs_planner_factory = information_needs_planner_factory
        self.instrument_discovery_service = instrument_discovery_service
        self.instrument_discovery_service_factory = instrument_discovery_service_factory
        self.market_scope_router = market_scope_router
        self.market_scope_router_factory = market_scope_router_factory
        self.realtime_query_service = realtime_query_service
        self.realtime_query_service_factory = realtime_query_service_factory
        self.news_query_service = news_query_service
        self.news_query_service_factory = news_query_service_factory
        self.full_research_router = full_research_router
        self.full_research_router_factory = full_research_router_factory
        self.financial_settings = financial_settings
        self.pack_loader = pack_loader
        self.industry_pack_id = industry_pack_id
        self.server_timezone = str(server_timezone)
        self.market_clock = MarketClockService()

    def _store(self) -> Optional[ChatFinancialRouteStore]:
        if self.store is None and self.store_factory is not None:
            self.store = self.store_factory()
        return self.store

    def _intent_classifier(self):
        if self.intent_classifier is None and self.intent_classifier_factory is not None:
            self.intent_classifier = self.intent_classifier_factory()
        return self.intent_classifier

    def _target_resolver(self):
        if self.target_resolver is None and self.target_resolver_factory is not None:
            self.target_resolver = self.target_resolver_factory()
        return self.target_resolver

    def _information_needs_planner(self):
        if (
            self.information_needs_planner is None
            and self.information_needs_planner_factory is not None
        ):
            self.information_needs_planner = self.information_needs_planner_factory()
        if self.information_needs_planner is None:
            from financial_information_needs import FinancialInformationNeedsPlanner

            self.information_needs_planner = FinancialInformationNeedsPlanner(
                settings=self.financial_settings
            )
        return self.information_needs_planner

    def _market_scope_router(self):
        if (
            self.market_scope_router is None
            and self.market_scope_router_factory is not None
        ):
            self.market_scope_router = self.market_scope_router_factory()
        return self.market_scope_router

    def _instrument_discovery_service(self):
        if (
            self.instrument_discovery_service is None
            and self.instrument_discovery_service_factory is not None
        ):
            self.instrument_discovery_service = self.instrument_discovery_service_factory()
        return self.instrument_discovery_service

    def _realtime_query_service(self):
        if (
            self.realtime_query_service is None
            and self.realtime_query_service_factory is not None
        ):
            self.realtime_query_service = self.realtime_query_service_factory()
        return self.realtime_query_service

    def _full_research_router(self):
        if (
            self.full_research_router is None
            and self.full_research_router_factory is not None
        ):
            self.full_research_router = self.full_research_router_factory()
        return self.full_research_router

    def _news_query_service(self):
        if (
            self.news_query_service is None
            and self.news_query_service_factory is not None
        ):
            self.news_query_service = self.news_query_service_factory()
        return self.news_query_service

    @staticmethod
    def _unclassified_intent(status: str, reason: str, time_resolution: Mapping) -> dict:
        primary = time_resolution.get("primary_range")
        return {
            "schema_version": "financial-intent-v1",
            "classification_status": str(status),
            "is_financial": False,
            "intent": "unknown",
            "asset_type": None,
            "candidates": [],
            "market": None,
            "currency": None,
            "universe": None,
            "as_of": dict(primary) if isinstance(primary, Mapping) else None,
            "freshness": "unspecified",
            "needs_clarification": False,
            "needs_full_research": False,
            "confidence": 0.0,
            "reason_codes": [str(reason)],
            "llm_used": False,
            "context_inherited": False,
        }

    @staticmethod
    def _unresolved_target(status: str, reason: str, destination: str = "normal_chat") -> dict:
        return {
            "schema_version": "financial-target-resolution-v1",
            "status": str(status),
            "targets": [],
            "candidate_count": 0,
            "needs_clarification": False,
            "clarification": {},
            "resolution_source": "none",
            "target_echo": "",
            "context_inherited": False,
            "resumed_from_route_key": None,
            "resume_context": {},
            "route_destination": str(destination),
            "llm_used": False,
            "reason_codes": [str(reason)],
        }

    def _prior_target_state(self, payload: Mapping) -> Optional[dict]:
        store = self._store()
        if store is None:
            return None
        try:
            return store.latest_target_state(str(payload.get("session_id") or ""))
        except Exception:
            return None

    @staticmethod
    def _restore_financial_context(
        current: Mapping[str, object],
        prior_state: Optional[Mapping[str, object]],
        target_resolution: Mapping[str, object],
        time_resolution: Mapping[str, object],
    ) -> dict:
        if not target_resolution.get("context_inherited") or not prior_state:
            return dict(current)
        prior_intent = prior_state.get("financial_intent")
        if not isinstance(prior_intent, Mapping) or not prior_intent.get("is_financial"):
            return dict(current)
        restored = dict(prior_intent)
        targets = target_resolution.get("targets") or []
        restored["candidates"] = [
            {
                "instrument_id": int(item["instrument_id"]),
                "canonical_symbol": str(item["canonical_symbol"]),
                "display_name": str(item["display_name"]),
                "asset_type": str(item["asset_type"]),
                "market": str(item["market"]),
                "exchange": str(item["exchange"]),
                "currency": str(item["currency"]),
                "resolution_status": "resolved",
                "matched_query": "same_session_context",
            }
            for item in targets
        ]
        restored["needs_clarification"] = False
        restored["context_inherited"] = True
        restored["classification_status"] = "classified"
        restored["is_financial"] = True
        primary = time_resolution.get("primary_range")
        if isinstance(primary, Mapping):
            restored["as_of"] = dict(primary)
        restored["reason_codes"] = list(restored.get("reason_codes") or []) + [
            "same_session_target_context_restored"
        ]
        return restored

    def _resolve_financial_target(
        self,
        payload: Mapping,
        financial_intent: Mapping[str, object],
        time_resolution: Mapping[str, object],
        audit_route_key: str,
    ) -> tuple[dict, dict]:
        prior = self._prior_target_state(payload)
        classification_status = str(
            financial_intent.get("classification_status") or "skipped"
        )
        eligible = classification_status in {"classified", "degraded"} and (
            financial_intent.get("is_financial") or prior is not None
        )
        if not eligible:
            return dict(financial_intent), self._unresolved_target(
                "skipped", "financial_target_resolution_not_applicable"
            )
        resolver = self._target_resolver()
        if resolver is None:
            return dict(financial_intent), self._unresolved_target(
                "skipped", "financial_target_resolver_not_configured"
            )
        question, _ = _last_user_message(payload)
        try:
            target = resolver.resolve(
                question,
                financial_intent,
                prior_state=prior,
                request_id=audit_route_key,
            )
        except Exception:
            target = self._unresolved_target(
                "degraded", "financial_target_resolution_failed"
            )
        restored = self._restore_financial_context(
            financial_intent, prior, target, time_resolution
        )
        return restored, target

    def _classify_financial_intent(
        self,
        payload: Mapping,
        time_resolution: Mapping,
        audit_route_key: str,
    ) -> dict:
        import config
        from industry_packs import industry_pack_loader

        if not rollout_capability_enabled("ai_fact", self.financial_settings):
            return self._unclassified_intent(
                "skipped",
                rollout_capability_reason("ai_fact", self.financial_settings),
                time_resolution,
            )

        pack_id = str(
            self.industry_pack_id
            or getattr(config, "INTEL_DEFAULT_INDUSTRY_PACK", "family_office")
        )
        gate = financial_classification_gate(
            pack_id,
            settings=self.financial_settings,
            pack_loader=self.pack_loader or industry_pack_loader,
        )
        if not gate["enabled"]:
            return self._unclassified_intent("skipped", str(gate["reason"]), time_resolution)
        classifier = self._intent_classifier()
        if classifier is None:
            return self._unclassified_intent(
                "degraded", "financial_intent_classifier_unavailable", time_resolution
            )
        question, _ = _last_user_message(payload)
        try:
            return classifier.classify(
                question,
                messages=payload.get("messages") or [],
                time_resolution=time_resolution,
                request_id=audit_route_key,
            )
        except Exception:
            # Financial classification degrades closed; ordinary chat keeps its
            # established route and public SSE contract.
            return self._unclassified_intent(
                "degraded", "financial_intent_classification_failed", time_resolution
            )

    def _plan_information_needs(
        self,
        payload: Mapping,
        financial_intent: Mapping[str, object],
    ) -> dict:
        from financial_information_needs import skipped_financial_information_needs

        question, _ = _last_user_message(payload)
        try:
            return self._information_needs_planner().plan(
                question, financial_intent
            )
        except Exception:
            return skipped_financial_information_needs(
                "information_needs_planning_failed"
            )

    @staticmethod
    def _route_unknown_target_to_discovery(
        target_resolution: Mapping[str, object],
        information_needs: Mapping[str, object],
    ) -> dict:
        result = dict(target_resolution)
        channels = set(information_needs.get("channels") or [])
        if (
            result.get("status") == "no_target"
            and channels.intersection({"quote", "news", "research"})
        ):
            result["route_destination"] = "financial_instrument_discovery"
            result["reason_codes"] = list(result.get("reason_codes") or []) + [
                "instrument_discovery_required"
            ]
        return result

    @staticmethod
    def _plan_instrument_discovery(
        payload: Mapping[str, object],
        target_resolution: Mapping[str, object],
    ) -> dict:
        from financial_instrument_discovery import (
            planned_instrument_discovery,
            skipped_instrument_discovery,
        )

        if str(target_resolution.get("route_destination") or "") != "financial_instrument_discovery":
            return skipped_instrument_discovery("instrument_discovery_not_required")
        question, _ = _last_user_message(payload)
        return planned_instrument_discovery(question)

    def _plan_market_scope(
        self,
        payload: Mapping,
        financial_intent: Mapping[str, object],
        target_resolution: Mapping[str, object],
        server_time_context: Mapping[str, object],
    ) -> dict:
        if str(financial_intent.get("intent") or "") != "market_overview":
            return skipped_market_scope("not_a_market_overview_intent")
        if str(target_resolution.get("status") or "") in {
            "resolved", "clarification_required"
        }:
            return skipped_market_scope("instrument_route_takes_precedence")
        router = self._market_scope_router()
        if router is None:
            return skipped_market_scope("market_scope_router_not_configured")
        question, _ = _last_user_message(payload)
        try:
            return router.plan(
                question,
                financial_intent,
                target_resolution,
                server_time_context,
            )
        except Exception:
            return skipped_market_scope("market_scope_planning_failed")

    def _plan_realtime_query(
        self,
        financial_intent: Mapping[str, object],
        target_resolution: Mapping[str, object],
        server_time_context: Mapping[str, object],
        information_needs: Optional[Mapping[str, object]] = None,
    ) -> dict:
        channels = set((information_needs or {}).get("channels") or [])
        if "quote" in channels and str(target_resolution.get("status") or "") != "resolved":
            return skipped_realtime_query("instrument_discovery_required")
        if str(financial_intent.get("intent") or "") != "market_fact" and "quote" not in channels:
            return skipped_realtime_query("not_a_market_fact_intent")
        service = self._realtime_query_service()
        if service is None:
            return skipped_realtime_query("realtime_query_service_not_configured")
        try:
            planned_intent = dict(financial_intent)
            if "quote" in channels:
                planned_intent["intent"] = "market_fact"
                if str(planned_intent.get("freshness") or "") == "unspecified":
                    planned_intent["freshness"] = "latest"
            return service.plan(
                planned_intent,
                target_resolution,
                server_time_context,
            )
        except Exception:
            return skipped_realtime_query("realtime_query_planning_failed")

    def _plan_news_query(
        self,
        target_resolution: Mapping[str, object],
        server_time_context: Mapping[str, object],
        information_needs: Mapping[str, object],
    ) -> dict:
        from financial_news_query import skipped_news_query, unavailable_news_query

        if "news" not in set(information_needs.get("channels") or []):
            return skipped_news_query("news_channel_not_requested")
        service = self._news_query_service()
        if service is None:
            targets = list(target_resolution.get("targets") or [])
            return unavailable_news_query(
                "news_query_service_not_configured",
                target=targets[0] if len(targets) == 1 else {},
                requested_at_utc=str(server_time_context.get("server_now_utc") or ""),
            )
        try:
            return service.plan(
                target_resolution,
                server_time_context,
                information_needs,
            )
        except Exception:
            return skipped_news_query("news_query_planning_failed")

    def _plan_full_research(
        self,
        payload: Mapping,
        financial_intent: Mapping[str, object],
        target_resolution: Mapping[str, object],
        server_time_context: Mapping[str, object],
    ) -> dict:
        if not financial_intent.get("needs_full_research"):
            return skipped_full_research("full_research_not_requested")
        if str(target_resolution.get("status") or "") == "clarification_required":
            return skipped_full_research("financial_target_clarification_required")
        requested_at = str(server_time_context.get("server_now_utc") or "")
        if self.full_research_router is None and self.full_research_router_factory is None:
            return skipped_full_research("full_research_router_not_configured")
        try:
            router = self._full_research_router()
        except Exception:
            return unavailable_full_research(
                "full_research_router_initialization_failed", requested_at=requested_at
            )
        if router is None:
            return skipped_full_research("full_research_router_not_configured")
        try:
            return router.plan(
                payload,
                financial_intent,
                target_resolution,
                server_time_context,
            )
        except Exception:
            return unavailable_full_research(
                "full_research_planning_failed", requested_at=requested_at
            )

    def _user_timezone(self, payload: Mapping) -> tuple[str, str]:
        hint = str(payload.get("user_timezone") or "").strip()
        if not hint:
            return self.server_timezone, "server_default"
        try:
            ZoneInfo(hint)
        except (ZoneInfoNotFoundError, ValueError):
            return self.server_timezone, "invalid_hint_fallback_server"
        return hint, "client_hint"

    def _resolve_question(
        self,
        payload: Mapping,
        context: RequestTimeContext,
    ) -> dict:
        question, message_index = _last_user_message(payload)
        positions = _expression_positions(question)
        inherited = None
        if not positions:
            store = self._store()
            if store is not None:
                inherited = store.latest_time_resolution(
                    str(payload.get("session_id") or "")
                )
            if inherited is not None:
                return {
                    **dict(inherited),
                    "inherited": True,
                    "inherited_from_route_key": inherited.get("source_route_key"),
                    "message_index": message_index,
                }
            messages = payload.get("messages") or []
            if isinstance(messages, list):
                for index in range(message_index - 1, -1, -1):
                    message = messages[index]
                    if not isinstance(message, Mapping) or message.get("role") != "user":
                        continue
                    previous = _expression_positions(str(message.get("content") or ""))
                    if previous:
                        positions = previous
                        inherited = {"history_message_index": index}
                        break
        ranges = []
        for _position, expression in positions:
            resolved = self.market_clock.resolve_time_range(expression, context)
            ranges.append(resolved.to_dict())
        return {
            "expressions": [expression for _position, expression in positions],
            "resolved_ranges": ranges,
            "primary_range": ranges[0] if ranges else None,
            "inherited": inherited is not None,
            "inherited_from_history_index": (
                inherited.get("history_message_index")
                if isinstance(inherited, Mapping)
                else None
            ),
            "message_index": message_index,
        }

    def plan(self, payload: Mapping) -> ChatRoutePlan:
        if not isinstance(payload, Mapping):
            raise TypeError("chat payload must be an object")
        captured = _utc(self.clock(), "server clock")
        user_timezone, timezone_source = self._user_timezone(payload)
        time_context = RequestTimeContext(
            server_now_utc=captured,
            server_timezone=self.server_timezone,
            user_timezone=user_timezone,
        )
        audit_route_key = f"chat-route-{uuid.uuid4().hex}"
        time_resolution = self._resolve_question(payload, time_context)
        financial_intent = self._classify_financial_intent(
            payload, time_resolution, audit_route_key
        )
        information_needs = self._plan_information_needs(
            payload, financial_intent
        )
        financial_intent, target_resolution = self._resolve_financial_target(
            payload, financial_intent, time_resolution, audit_route_key
        )
        target_resolution = self._route_unknown_target_to_discovery(
            target_resolution, information_needs
        )
        instrument_discovery = self._plan_instrument_discovery(
            payload, target_resolution
        )
        server_time_context = {
            **time_context.to_dict(),
            "context_version": SERVER_TIME_CONTEXT_VERSION,
            "clock_source": "application_server",
            "user_timezone_source": timezone_source,
        }
        market_scope = self._plan_market_scope(
            payload,
            financial_intent,
            target_resolution,
            server_time_context,
        )
        realtime_query = self._plan_realtime_query(
            financial_intent,
            target_resolution,
            server_time_context,
            information_needs,
        )
        news_query = self._plan_news_query(
            target_resolution,
            server_time_context,
            information_needs,
        )
        from financial_latest_bundle import plan_latest_bundle
        latest_bundle = plan_latest_bundle(
            information_needs,
            realtime_query,
            news_query,
            settings=self.financial_settings,
        )
        full_research = self._plan_full_research(
            payload,
            financial_intent,
            target_resolution,
            server_time_context,
        )
        requested_sse_features = payload.get("sse_features") or []
        if not isinstance(requested_sse_features, (list, tuple, set)):
            requested_sse_features = []
        financial_stream = bool(financial_intent.get("is_financial")) and (
            FINANCIAL_SSE_PROTOCOL_VERSION
            in {str(item) for item in requested_sse_features}
        )
        return ChatRoutePlan(
            audit_route_key=audit_route_key,
            route_key=LEGACY_CHAT_ROUTE,
            stream_protocol_version=(
                FINANCIAL_SSE_PROTOCOL_VERSION if financial_stream else "legacy-sse-v1"
            ),
            public_event_types=(
                FINANCIAL_SSE_EVENT_TYPES if financial_stream else LEGACY_SSE_EVENT_TYPES
            ),
            requested_model=str(payload.get("model") or ""),
            web_search=bool(payload.get("web_search", False)),
            server_time_context=server_time_context,
            time_resolution=time_resolution,
            financial_intent=financial_intent,
            target_resolution=target_resolution,
            market_scope=market_scope,
            realtime_query=realtime_query,
            full_research=full_research,
            information_needs=information_needs,
            instrument_discovery=instrument_discovery,
            news_query=news_query,
            latest_bundle=latest_bundle,
        )

    def activate_instrument_discovery(
        self, plan: ChatRoutePlan, payload: Mapping
    ) -> ChatRoutePlan:
        """公开参数校验后执行受控发现；成功时只重跑一次解析和下游规划。"""

        if str(plan.instrument_discovery.get("status") or "") != "planned":
            return plan
        service = self._instrument_discovery_service()
        if service is None:
            completed = {
                **dict(plan.instrument_discovery),
                "status": "verification_required",
                "reason_codes": ["instrument_discovery_service_not_configured"],
            }
            return replace(plan, instrument_discovery=completed)
        question, _ = _last_user_message(payload)
        try:
            requested_at = datetime.fromisoformat(
                str(plan.server_time_context["server_now_utc"]).replace("Z", "+00:00")
            )
            completed = service.discover_and_promote(
                question,
                requested_at=requested_at,
                request_id=plan.audit_route_key,
            )
        except Exception:
            completed = {
                **dict(plan.instrument_discovery),
                "status": "verification_required",
                "reason_codes": ["instrument_discovery_failed"],
            }
        if str(completed.get("status") or "") != "promoted":
            return replace(plan, instrument_discovery=completed)

        financial_intent = self._classify_financial_intent(
            payload, plan.time_resolution, plan.audit_route_key
        )
        information_needs = self._plan_information_needs(payload, financial_intent)
        financial_intent, target_resolution = self._resolve_financial_target(
            payload,
            financial_intent,
            plan.time_resolution,
            plan.audit_route_key,
        )
        if str(target_resolution.get("status") or "") != "resolved":
            completed = {
                **dict(completed),
                "status": "verification_required",
                "reason_codes": list(completed.get("reason_codes") or [])
                + ["promoted_target_reresolution_failed"],
            }
            return replace(
                plan,
                financial_intent=financial_intent,
                target_resolution=target_resolution,
                information_needs=information_needs,
                instrument_discovery=completed,
            )
        realtime_query = self._plan_realtime_query(
            financial_intent,
            target_resolution,
            plan.server_time_context,
            information_needs,
        )
        news_query = self._plan_news_query(
            target_resolution,
            plan.server_time_context,
            information_needs,
        )
        from financial_latest_bundle import plan_latest_bundle

        return replace(
            plan,
            financial_intent=financial_intent,
            target_resolution=target_resolution,
            information_needs=information_needs,
            instrument_discovery=completed,
            market_scope=self._plan_market_scope(
                payload,
                financial_intent,
                target_resolution,
                plan.server_time_context,
            ),
            realtime_query=realtime_query,
            news_query=news_query,
            latest_bundle=plan_latest_bundle(
                information_needs,
                realtime_query,
                news_query,
                settings=self.financial_settings,
            ),
            full_research=self._plan_full_research(
                payload,
                financial_intent,
                target_resolution,
                plan.server_time_context,
            ),
        )

    def activate_market_scope(self, plan: ChatRoutePlan) -> ChatRoutePlan:
        """Perform the queued write only after public request validation."""

        if str(plan.market_scope.get("status") or "") != "planned":
            return plan
        discovery_status = str(plan.instrument_discovery.get("status") or "")
        if discovery_status in {"planned", "verification_required"}:
            return replace(
                plan,
                market_scope=skipped_market_scope(
                    "instrument_discovery_takes_precedence"
                ),
            )
        router = self._market_scope_router()
        if router is None:
            return replace(
                plan,
                market_scope={
                    **skipped_market_scope("market_scope_router_not_configured"),
                    "status": "degraded",
                    "route_destination": "financial_market_scope",
                },
            )
        try:
            activated = router.activate(plan.market_scope)
        except Exception:
            activated = {
                **plan.market_scope,
                "status": "degraded",
                "refresh": {"status": "failed"},
                "answer_allowed": False,
                "reason_codes": list(plan.market_scope.get("reason_codes") or [])
                + ["market_scope_activation_failed"],
            }
        return replace(plan, market_scope=activated)

    def execute_realtime_query(self, plan: ChatRoutePlan) -> ChatRoutePlan:
        """Run synchronous query-through after the first SSE status is yielded."""

        if str(plan.realtime_query.get("status") or "") != "planned":
            return plan
        service = self._realtime_query_service()
        if service is None:
            completed = {
                **plan.realtime_query,
                "status": "unavailable",
                "refresh": {
                    "status": "failed",
                    "error_code": "realtime_query_service_not_configured",
                },
                "reason_codes": list(plan.realtime_query.get("reason_codes") or [])
                + ["realtime_query_service_not_configured"],
            }
            return replace(plan, realtime_query=completed)
        try:
            completed = service.execute(plan.realtime_query)
        except Exception:
            completed = {
                **plan.realtime_query,
                "status": "unavailable",
                "refresh": {
                    "status": "failed",
                    "error_code": "realtime_query_execution_failed",
                },
                "answer_allowed": False,
                "numeric_claims_allowed": False,
                "reason_codes": list(plan.realtime_query.get("reason_codes") or [])
                + ["realtime_query_execution_failed"],
            }
        return replace(plan, realtime_query=completed)

    def execute_latest_bundle(self, plan: ChatRoutePlan) -> ChatRoutePlan:
        """在首个 SSE 状态之后执行报价和新闻两个独立通道。"""

        if str(plan.latest_bundle.get("status") or "") != "planned":
            return plan
        from financial_latest_bundle import FinancialLatestBundleService

        service = FinancialLatestBundleService(
            realtime_service=self._realtime_query_service(),
            news_service=self._news_query_service(),
            settings=self.financial_settings,
        )
        try:
            completed = service.execute(
                plan.latest_bundle,
                plan.server_time_context,
            )
        except Exception:
            completed = {
                **dict(plan.latest_bundle),
                "status": "unavailable",
                "answer_allowed": False,
                "reason_codes": list(plan.latest_bundle.get("reason_codes") or [])
                + ["latest_bundle_execution_failed"],
            }
        return replace(
            plan,
            latest_bundle=completed,
            realtime_query=dict(completed.get("quote") or plan.realtime_query),
            news_query=dict(completed.get("news") or plan.news_query),
        )

    def execute_news_query(self, plan: ChatRoutePlan) -> ChatRoutePlan:
        """执行单独新闻请求；失败时保持在金融链路内，不回落通用模型。"""

        if str(plan.news_query.get("status") or "") != "planned":
            return plan
        service = self._news_query_service()
        if service is None:
            from financial_news_query import unavailable_news_query

            completed = unavailable_news_query(
                "news_query_service_not_configured",
                target=plan.news_query.get("target") or {},
                requested_at_utc=str(
                    plan.news_query.get("requested_at_utc")
                    or plan.server_time_context.get("server_now_utc")
                    or ""
                ),
                lookback_days=int(plan.news_query.get("lookback_days") or 7),
            )
            return replace(plan, news_query=completed)
        try:
            from financial_latest_bundle import (
                FinancialLatestBundleService,
                plan_latest_bundle,
            )

            source = self.financial_settings

            def setting(name, default):
                if isinstance(source, Mapping):
                    return source.get(name, default)
                return getattr(source, name, default)

            # Standalone news remains available when only the combined-bundle
            # switch is rolled back, but it still uses the same bounded runner.
            runner_settings = {
                "FINANCIAL_INTELLIGENCE_ENABLED": True,
                "FINANCIAL_LATEST_BUNDLE_ENABLED": True,
                "FINANCIAL_LATEST_QUOTE_TIMEOUT_SECONDS": setting(
                    "FINANCIAL_LATEST_QUOTE_TIMEOUT_SECONDS", 6.0
                ),
                "FINANCIAL_LATEST_NEWS_TIMEOUT_SECONDS": setting(
                    "FINANCIAL_LATEST_NEWS_TIMEOUT_SECONDS", 40.0
                ),
                "FINANCIAL_LATEST_BUNDLE_TIMEOUT_SECONDS": setting(
                    "FINANCIAL_LATEST_BUNDLE_TIMEOUT_SECONDS", 45.0
                ),
            }
            standalone = plan_latest_bundle(
                {"channels": ["news"]},
                skipped_realtime_query("quote_channel_not_requested"),
                plan.news_query,
            )
            bounded = FinancialLatestBundleService(
                news_service=service,
                settings=runner_settings,
            ).execute(standalone, plan.server_time_context)
            completed = dict(bounded.get("news") or plan.news_query)
        except Exception:
            from financial_news_query import unavailable_news_query

            completed = unavailable_news_query(
                "news_query_execution_failed",
                target=plan.news_query.get("target") or {},
                requested_at_utc=str(plan.news_query.get("requested_at_utc") or ""),
                lookback_days=int(plan.news_query.get("lookback_days") or 7),
            )
        return replace(plan, news_query=completed)

    def activate_full_research(
        self, plan: ChatRoutePlan, payload: Mapping
    ) -> ChatRoutePlan:
        """Check compatible reports and queue through the existing intel_jobs table."""

        if str(plan.full_research.get("status") or "") != "planned":
            return plan
        router = self._full_research_router()
        if router is None:
            completed = unavailable_full_research(
                "full_research_router_not_configured",
                requested_at=str(plan.server_time_context.get("server_now_utc") or ""),
            )
            return replace(plan, full_research=completed)
        try:
            completed = router.activate(plan.full_research, payload)
        except Exception:
            completed = unavailable_full_research(
                "full_research_activation_failed",
                requested_at=str(plan.server_time_context.get("server_now_utc") or ""),
            )
        return replace(plan, full_research=completed)

    def persist(self, plan: ChatRoutePlan, payload: Mapping) -> dict:
        store = self._store()
        if store is None:
            return {"status": "skipped", "reason": "route_store_not_configured"}
        try:
            return {"status": "persisted", "route_id": store.persist(plan, payload)}
        except Exception:
            # Ordinary chat remains available if route audit persistence is
            # unavailable.  No database detail is sent to the client.
            return {"status": "degraded", "reason": "route_audit_unavailable"}


def _default_route_store() -> ChatFinancialRouteStore:
    from sqlite_database import sqlite_db

    return ChatFinancialRouteStore(sqlite_db)


def _default_intent_classifier():
    from financial_instruments import InstrumentRegistry
    from financial_intent_classifier import FinancialIntentClassifier, SharedLLMIntentJudge
    from shared_llm_broker import SharedLLMBroker
    from sqlite_database import sqlite_db

    sqlite_db._ensure_connection()
    registry = InstrumentRegistry(sqlite_db.connection)
    registry.load_controlled_seed()
    broker = SharedLLMBroker(connection=sqlite_db.connection)
    return FinancialIntentClassifier(registry, llm_judge=SharedLLMIntentJudge(broker))


def _default_target_resolver():
    from financial_target_resolver import FinancialTargetResolver, SharedLLMTargetRanker

    classifier = chat_route_orchestrator._intent_classifier()
    if classifier is None:
        raise RuntimeError("financial intent classifier is unavailable")
    judge = getattr(classifier, "llm_judge", None)
    broker = getattr(judge, "broker", None)
    ranker = SharedLLMTargetRanker(broker) if broker is not None else None
    return FinancialTargetResolver(classifier.instruments, llm_ranker=ranker)


def _default_market_scope_router():
    import config
    from financial_chat_market_scope import FinancialMarketScopeRouter
    from financial_market_scheduler import FinancialMarketScheduler
    from intel_database import IntelRepository
    from sqlite_database import sqlite_db

    sqlite_db._ensure_connection()
    repository = IntelRepository(sqlite_db)
    scheduler = FinancialMarketScheduler(repository, settings=config)
    return FinancialMarketScopeRouter(repository, scheduler, settings=config)


def _default_instrument_discovery_service():
    import config
    from financial_instrument_discovery import FinancialInstrumentDiscoveryService
    from sqlite_database import sqlite_db

    sqlite_db._ensure_connection()
    return FinancialInstrumentDiscoveryService(sqlite_db.connection, settings=config)


def _default_realtime_query_service():
    import config
    from financial_realtime_query import FinancialRealtimeQueryService
    from sqlite_database import sqlite_db

    sqlite_db._ensure_connection()
    return FinancialRealtimeQueryService(sqlite_db, settings=config)


def _default_news_query_service():
    import config
    from financial_news_refresh import FinancialNewsRefreshCoordinator
    from financial_news_query import FinancialNewsQueryService
    from sqlite_database import sqlite_db

    sqlite_db._ensure_connection()
    refresher = FinancialNewsRefreshCoordinator(sqlite_db, settings=config)
    return FinancialNewsQueryService(
        sqlite_db,
        settings=config,
        refresher=refresher,
    )


def _default_full_research_router():
    import config
    from financial_full_research import FinancialFullResearchRouter
    from intel_database import intel_repository

    return FinancialFullResearchRouter(intel_repository, settings=config)


import config as _runtime_financial_settings


chat_route_orchestrator = ChatRouteOrchestrator(
    store_factory=_default_route_store,
    intent_classifier_factory=_default_intent_classifier,
    target_resolver_factory=_default_target_resolver,
    instrument_discovery_service_factory=_default_instrument_discovery_service,
    market_scope_router_factory=_default_market_scope_router,
    realtime_query_service_factory=_default_realtime_query_service,
    news_query_service_factory=_default_news_query_service,
    full_research_router_factory=_default_full_research_router,
    # Keep one live module object so config-management updates and process
    # startup environment flags govern planning as well as service execution.
    financial_settings=_runtime_financial_settings,
)

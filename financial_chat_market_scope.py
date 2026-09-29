#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Deterministic broad-market routing for financial chat questions.

The router plans without network access, activates one bounded refresh on the
existing intel queue, and only marks an answer as evidence-ready after a fresh
overview report references persisted snapshot rows.  It never selects a stock
for the user and never asks an LLM to invent a missing quote.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Mapping, Optional
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator

from financial_provider_contract import MarketStatus
from financial_universe_planner import DEFAULT_UNIVERSE_KEYS


UTC = timezone.utc
MARKET_SCOPE_SCHEMA_VERSION = "financial-chat-market-scope-v1"
MARKET_SCOPE_STATUSES = (
    "skipped",
    "planned",
    "refresh_queued",
    "ready",
    "degraded",
)
MARKET_SCOPE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "schema_version", "status", "universe", "members",
        "required_market_metrics", "market_sessions", "refresh", "report",
        "answer_allowed", "route_destination", "reason_codes",
    ],
    "properties": {
        "schema_version": {"const": MARKET_SCOPE_SCHEMA_VERSION},
        "status": {"enum": list(MARKET_SCOPE_STATUSES)},
        "universe": {"type": "object"},
        "members": {"type": "array", "items": {"type": "object"}},
        "required_market_metrics": {
            "type": "array", "items": {"type": "string"}
        },
        "market_sessions": {"type": "object"},
        "refresh": {"type": "object"},
        "report": {"type": "object"},
        "answer_allowed": {"type": "boolean"},
        "route_destination": {"type": "string"},
        "reason_codes": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}
_VALIDATOR = Draft202012Validator(MARKET_SCOPE_SCHEMA)


def validate_market_scope(payload: Mapping[str, object]) -> dict:
    result = dict(payload)
    _VALIDATOR.validate(result)
    return result


def skipped_market_scope(reason: str) -> dict:
    return validate_market_scope(
        {
            "schema_version": MARKET_SCOPE_SCHEMA_VERSION,
            "status": "skipped",
            "universe": {},
            "members": [],
            "required_market_metrics": [],
            "market_sessions": {},
            "refresh": {},
            "report": {},
            "answer_allowed": False,
            "route_destination": "normal_chat",
            "reason_codes": [str(reason)],
        }
    )


def _parse_utc(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("server_now_utc must be timezone-aware")
    return parsed.astimezone(UTC)


def _freshness_seconds(market_sessions: Mapping[str, object], default: int) -> int:
    """Apply the strictest active exchange threshold to a composite scope."""

    thresholds = []
    for raw in market_sessions.values():
        session = raw if isinstance(raw, Mapping) else {}
        state = str(session.get("market_session_state") or "unknown")
        trading_day = bool(session.get("is_trading_day"))
        if state == MarketStatus.OPEN.value:
            thresholds.append(max(1, int(default)))
        elif state in {MarketStatus.PRE_OPEN.value, MarketStatus.LUNCH_BREAK.value}:
            thresholds.append(max(900, int(default)))
        elif trading_day:
            thresholds.append(86_400)
        else:
            thresholds.append(259_200)
    return min(thresholds) if thresholds else max(1, int(default))


def _session_freshness_seconds(session: Mapping[str, object], default: int) -> int:
    return _freshness_seconds({"exchange": dict(session or {})}, default)


class FinancialMarketScopeRouter:
    """Resolve, queue and verify one standard broad-market universe."""

    def __init__(self, repository, scheduler, *, settings=None):
        self.repository = repository
        self.scheduler = scheduler
        self.settings = settings if settings is not None else scheduler.settings
        self.repository.db._ensure_connection()
        self.connection = self.repository.db.connection
        self.universes = scheduler.universes

    def plan(
        self,
        question: str,
        financial_intent: Mapping[str, object],
        target_resolution: Mapping[str, object],
        server_time_context: Mapping[str, object],
    ) -> dict:
        if not financial_intent.get("is_financial"):
            return skipped_market_scope("non_financial_question")
        if str(target_resolution.get("status") or "") in {
            "resolved", "clarification_required"
        }:
            return skipped_market_scope("instrument_route_takes_precedence")
        universe_key = str(financial_intent.get("universe") or "").strip().upper()
        if not universe_key:
            route = self.universes.resolve_expression(question)
            if route.status == "resolved" and not route.requires_clarification:
                universe_key = route.universe_key
        if universe_key not in DEFAULT_UNIVERSE_KEYS:
            return skipped_market_scope("no_standard_market_scope")
        if str(financial_intent.get("intent") or "") != "market_overview":
            return skipped_market_scope("not_a_market_overview_intent")

        now = _parse_utc(server_time_context.get("server_now_utc"))
        as_of = now.astimezone(ZoneInfo("Asia/Hong_Kong")).date().isoformat()
        universe = self.universes.get_universe(universe_key, as_of=as_of)
        members = self.universes.members_as_of(universe_key, as_of=as_of)
        definition = dict(universe.definition)
        return validate_market_scope(
            {
                "schema_version": MARKET_SCOPE_SCHEMA_VERSION,
                "status": "planned",
                "universe": {
                    "universe_id": universe.universe_id,
                    "universe_key": universe.universe_key,
                    "display_name": universe.display_name,
                    "definition_version": universe.definition_version,
                    "constituent_as_of": universe.constituent_as_of,
                    "constituent_basis": str(
                        definition.get("constituent_basis") or ""
                    ),
                    "scope_basis": str(definition.get("scope_basis") or ""),
                    "default_scope_notice": str(
                        definition.get("default_scope_notice") or ""
                    ),
                },
                "members": [
                    {
                        "instrument_id": member.instrument.instrument_id,
                        "canonical_symbol": member.instrument.canonical_symbol,
                        "display_name": member.instrument.display_name,
                        "exchange": member.instrument.exchange,
                        "currency": member.instrument.currency,
                        "role": str(member.metadata.get("role") or "benchmark"),
                    }
                    for member in members
                ],
                "required_market_metrics": [
                    str(item)
                    for item in definition.get("required_market_metrics", ())
                ],
                "market_sessions": {},
                "refresh": {
                    "requested_at_utc": now.isoformat(timespec="seconds").replace(
                        "+00:00", "Z"
                    )
                },
                "report": {},
                "answer_allowed": False,
                "route_destination": "financial_market_scope",
                "reason_codes": ["standard_market_scope_resolved"],
            }
        )

    def _fresh_report(
        self,
        universe_id: int,
        universe_key: str,
        *,
        now: datetime,
        market_sessions: Mapping[str, object],
        preferred_run_id: str = "",
    ) -> Optional[dict]:
        order = "CASE WHEN run.id=? THEN 0 ELSE 1 END, run.requested_at DESC, r.id DESC"
        rows = self.connection.execute(
            f"""
            SELECT r.id, r.research_run_id, r.report_status, r.report_json,
                   r.observed_at, r.fetched_at, run.requested_at
            FROM financial_final_reports r
            JOIN financial_research_runs run ON run.id=r.research_run_id
            WHERE run.universe_id=?
            ORDER BY {order}
            LIMIT 20
            """,
            (int(universe_id), str(preferred_run_id or "")),
        ).fetchall()
        default = 300
        if isinstance(self.settings, Mapping):
            default = int(
                self.settings.get("FINANCIAL_QUOTE_FRESHNESS_SECONDS", default)
            )
        else:
            default = int(
                getattr(self.settings, "FINANCIAL_QUOTE_FRESHNESS_SECONDS", default)
            )
        max_age = _freshness_seconds(market_sessions, default)
        for row in rows:
            try:
                body = json.loads(str(row[3] or "{}"))
                requested_at = _parse_utc(row[6])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if requested_at > now or (now - requested_at).total_seconds() > max_age:
                continue
            if body.get("report_type") != "lightweight_market_overview":
                continue
            report_universe = body.get("universe") or {}
            if str(report_universe.get("universe_key") or "") != universe_key:
                continue
            try:
                snapshot_ids = [
                    int(item)
                    for item in body.get("snapshot_ids") or []
                    if int(item) > 0
                ]
            except (TypeError, ValueError):
                continue
            if not snapshot_ids:
                continue
            placeholders = ",".join("?" for _ in snapshot_ids)
            evidence_rows = self.connection.execute(
                f"""
                SELECT s.id, s.fetched_at, i.exchange
                FROM financial_data_snapshots s
                JOIN financial_instruments i ON i.id=s.instrument_id
                WHERE s.id IN ({placeholders})
                """,
                snapshot_ids,
            ).fetchall()
            if len(evidence_rows) != len(set(snapshot_ids)):
                continue
            evidence_is_fresh = True
            for evidence in evidence_rows:
                try:
                    fetched_at = _parse_utc(evidence[1])
                except (TypeError, ValueError):
                    evidence_is_fresh = False
                    break
                exchange = str(evidence[2] or "")
                session = market_sessions.get(exchange)
                if not isinstance(session, Mapping):
                    evidence_is_fresh = False
                    break
                threshold = _session_freshness_seconds(session, default)
                if (
                    fetched_at > now
                    or (now - fetched_at).total_seconds() > threshold
                ):
                    evidence_is_fresh = False
                    break
            if not evidence_is_fresh:
                continue
            return {
                "report_id": int(row[0]),
                "research_run_id": str(row[1]),
                "report_status": str(row[2]),
                "requested_at_utc": str(row[6]),
                "observed_at": str(row[4] or ""),
                "fetched_at": str(row[5] or ""),
                "coverage": float(body.get("coverage") or 0.0),
                "snapshot_ids": snapshot_ids,
                "missing_symbols": list(body.get("missing_symbols") or []),
                "missing_market_metrics": list(
                    body.get("missing_market_metrics") or []
                ),
                "schedule_window": str(body.get("schedule_window") or ""),
                "phase": str(body.get("phase") or ""),
                "boundary": str(body.get("boundary") or ""),
            }
        return None

    def activate(self, scope: Mapping[str, object]) -> dict:
        planned = validate_market_scope(scope)
        if planned["status"] != "planned":
            return planned
        universe = dict(planned["universe"])
        now = _parse_utc((planned.get("refresh") or {}).get("requested_at_utc"))
        try:
            scheduled = self.scheduler.enqueue_scope_refresh(
                str(universe["universe_key"]),
                now=now,
                request_id=str((planned.get("refresh") or {}).get("request_id") or ""),
            )
        except Exception:
            return validate_market_scope(
                {
                    **planned,
                    "status": "degraded",
                    "refresh": {"status": "failed"},
                    "answer_allowed": False,
                    "reason_codes": list(planned["reason_codes"])
                    + ["market_scope_refresh_failed"],
                }
            )
        if scheduled.get("status") != "scheduled":
            return validate_market_scope(
                {
                    **planned,
                    "status": "degraded",
                    "refresh": dict(scheduled),
                    "answer_allowed": False,
                    "reason_codes": list(planned["reason_codes"])
                    + [str(scheduled.get("reason") or "market_scope_refresh_skipped")],
                }
            )

        sessions = dict(scheduled.get("market_sessions") or {})
        report = self._fresh_report(
            int(universe["universe_id"]),
            str(universe["universe_key"]),
            now=now,
            market_sessions=sessions,
            preferred_run_id=str(scheduled.get("overview_research_run_id") or ""),
        )
        refresh = {
            "status": "scheduled",
            "trigger": "chat_query",
            "server_now_utc": str(scheduled.get("server_now_utc") or ""),
            "phase": str(scheduled.get("phase") or ""),
            "schedule_window": str(scheduled.get("schedule_window") or ""),
            "created": int(scheduled.get("created") or 0),
            "existing": int(scheduled.get("existing") or 0),
            "jobs": [
                {
                    "job_id": int(item["job_id"]),
                    "job_type": str(item["job_type"]),
                    "created": bool(item["created"]),
                }
                for item in scheduled.get("jobs") or []
            ],
            "overview_research_run_id": str(
                scheduled.get("overview_research_run_id") or ""
            ),
            "full_research_jobs_created": int(
                scheduled.get("full_research_jobs_created") or 0
            ),
        }
        if report is None:
            return validate_market_scope(
                {
                    **planned,
                    "status": "refresh_queued",
                    "market_sessions": sessions,
                    "refresh": refresh,
                    "report": {},
                    "answer_allowed": False,
                    "reason_codes": list(planned["reason_codes"])
                    + ["persisted_fresh_snapshot_report_not_ready"],
                }
            )
        reasons = list(planned["reason_codes"]) + ["persisted_snapshot_report_ready"]
        if report["coverage"] < 1.0:
            reasons.append("partial_market_coverage_disclosed")
        return validate_market_scope(
            {
                **planned,
                "status": "ready",
                "market_sessions": sessions,
                "refresh": refresh,
                "report": report,
                "answer_allowed": True,
                "reason_codes": reasons,
            }
        )


def format_market_scope_answer(scope: Mapping[str, object]) -> str:
    """Create a value-free, evidence-gated response using the legacy SSE path."""

    current = validate_market_scope(scope)
    universe = current.get("universe") or {}
    refresh = current.get("refresh") or {}
    name = str(universe.get("display_name") or "市场概览")
    basis = str(universe.get("constituent_basis") or "")
    notice = str(universe.get("default_scope_notice") or "")
    sessions = current.get("market_sessions") or {}
    states = "、".join(
        f"{exchange}={str((raw or {}).get('market_session_state') or 'unknown')}"
        for exchange, raw in sorted(sessions.items())
        if isinstance(raw, Mapping)
    ) or "尚未取得交易时段状态"
    now_text = str(refresh.get("server_now_utc") or "")
    if not current.get("answer_allowed"):
        created = int(refresh.get("created") or 0)
        return (
            f"已识别为{name}，服务器时间 {now_text}，交易时段：{states}。"
            f"已在现有金融队列中安排 {created} 个新任务；当前尚无满足时效要求且已入库的"
            "市场快照报告，因此本轮不输出行情数值，也不会由通用模型补造。请稍后重试。"
            + (f"覆盖口径：{basis}。" if basis else "")
            + (f"范围说明：{notice}。" if notice else "")
        )
    report = current.get("report") or {}
    members = current.get("members") or []
    metrics = current.get("required_market_metrics") or []
    denominator = len(members) + len(metrics)
    snapshot_count = len(report.get("snapshot_ids") or [])
    missing = list(report.get("missing_symbols") or []) + list(
        report.get("missing_market_metrics") or []
    )
    missing_text = "、".join(str(item) for item in missing) if missing else "无"
    return (
        f"{name}的轻量市场概览已由已入库快照生成。服务器时间 {now_text}，"
        f"交易时段：{states}；证据覆盖 {snapshot_count}/{denominator} "
        f"({float(report.get('coverage') or 0.0):.0%})，缺失项：{missing_text}。"
        f"覆盖口径：{basis}。报告编号 #{int(report.get('report_id') or 0)}。"
        + (f"范围说明：{notice}。" if notice else "")
        + "这是确定性的快照范围与覆盖报告，不是完整多代理研究，也不构成交易指令。"
    )

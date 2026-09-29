#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Cache-safe chat routing into the existing TradingAgents worker job type."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from jsonschema import Draft202012Validator

from financial_config import financial_capabilities, financial_product_capabilities
from financial_rollout import rollout_capability_enabled, rollout_capability_reason
from financial_provider_router import PROVIDER_POLICIES
from financial_universe_planner import FinancialUniversePlanner
from tradingagents_cn_data_adapter import ADAPTER_VERSION, DEFAULT_PROVIDER_CHAINS


UTC = timezone.utc
FULL_RESEARCH_SCHEMA_VERSION = "financial-full-research-route-v1"
FULL_RESEARCH_ROUTER_VERSION = "financial-full-research-router-v1"
FULL_RESEARCH_STATUSES = (
    "skipped",
    "planned",
    "cache_hit",
    "queued",
    "running",
    "mixed",
    "failed",
    "cancelled",
    "unavailable",
    "degraded",
)
TERMINAL_REPORT_DENYLIST = frozenset({"", "draft", "failed", "cancelled"})
PROVIDER_CAPABILITIES = (
    "akshare_cn",
    "tushare_cn",
    "yahoo",
    "alpha_vantage",
    "fred",
    "polymarket",
    "easyquotation",
    "official_evidence",
)
RESEARCH_CONFIG_KEYS = (
    "FINANCIAL_RESEARCH_MAX_LLM_CALLS",
    "FINANCIAL_RESEARCH_MAX_TOKENS",
    "FINANCIAL_RESEARCH_MAX_DEBATE_ROUNDS",
    "FINANCIAL_RESEARCH_TIMEOUT_SECONDS",
    "FINANCIAL_QUOTE_FRESHNESS_SECONDS",
    "FINANCIAL_MARKET_BREADTH_FRESHNESS_SECONDS",
    "FINANCIAL_NEWS_FRESHNESS_SECONDS",
    "FINANCIAL_FUNDAMENTAL_FRESHNESS_SECONDS",
)
PROVIDER_CONFIG_KEYS = (
    "FINANCIAL_PROVIDER_TIMEOUT_SECONDS",
    "FINANCIAL_PROVIDER_MAX_RETRIES",
    "FINANCIAL_PROVIDER_MAX_CONCURRENCY",
    "FINANCIAL_PROVIDER_DAILY_CALL_BUDGET",
)

FULL_RESEARCH_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "schema_version",
        "status",
        "requested_at_utc",
        "completed_at_utc",
        "cache_ttl_seconds",
        "scopes",
        "reports",
        "jobs",
        "research_run_ids",
        "answer_allowed",
        "route_destination",
        "reason_codes",
        "error",
    ],
    "additionalProperties": False,
    "properties": {
        "schema_version": {"const": FULL_RESEARCH_SCHEMA_VERSION},
        "status": {"enum": list(FULL_RESEARCH_STATUSES)},
        "requested_at_utc": {"type": "string"},
        "completed_at_utc": {"type": "string"},
        "cache_ttl_seconds": {"type": "integer", "minimum": 60},
        "scopes": {"type": "array", "items": {"type": "object"}},
        "reports": {"type": "array", "items": {"type": "object"}},
        "jobs": {"type": "array", "items": {"type": "object"}},
        "research_run_ids": {"type": "array", "items": {"type": "string"}},
        "answer_allowed": {"type": "boolean"},
        "route_destination": {"type": "string"},
        "reason_codes": {"type": "array", "items": {"type": "string"}},
        "error": {"type": "object"},
    },
}
_VALIDATOR = Draft202012Validator(FULL_RESEARCH_SCHEMA)


def validate_full_research(payload: Mapping[str, object]) -> dict:
    result = dict(payload)
    _VALIDATOR.validate(result)
    return result


def skipped_full_research(reason: str) -> dict:
    return validate_full_research(
        {
            "schema_version": FULL_RESEARCH_SCHEMA_VERSION,
            "status": "skipped",
            "requested_at_utc": "",
            "completed_at_utc": "",
            "cache_ttl_seconds": 60,
            "scopes": [],
            "reports": [],
            "jobs": [],
            "research_run_ids": [],
            "answer_allowed": False,
            "route_destination": "normal_chat",
            "reason_codes": [str(reason)],
            "error": {},
        }
    )


def unavailable_full_research(reason: str, *, requested_at: str = "") -> dict:
    return validate_full_research(
        {
            **skipped_full_research(reason),
            "status": "unavailable",
            "requested_at_utc": str(requested_at),
            "completed_at_utc": str(requested_at),
            "route_destination": "financial_full_research",
            "error": {"error_code": str(reason)},
        }
    )


def _setting(settings: object, name: str, default: object) -> object:
    if isinstance(settings, Mapping):
        return settings.get(name, default)
    return getattr(settings, name, default)


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _parse_utc(value: object) -> datetime:
    return _utc(
        datetime.fromisoformat(str(value or "").replace("Z", "+00:00")),
        "datetime",
    )


def _utc_text(value: datetime) -> str:
    return _utc(value, "datetime").isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _finite(value: object) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _observed_not_future(value: object, now: datetime) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    if len(text) == 10:
        try:
            return datetime.fromisoformat(text).date() <= now.date()
        except ValueError:
            return False
    try:
        return _parse_utc(text) <= now
    except (TypeError, ValueError):
        return False


def _graph_version(asset_type: str, scope_type: str) -> str:
    if scope_type == "universe" or asset_type == "index":
        from index_market_research_graph import INDEX_GRAPH_VERSION

        return INDEX_GRAPH_VERSION
    if asset_type in {"fund", "etf"}:
        from fund_etf_research import FUND_ETF_RESEARCH_VERSION

        return FUND_ETF_RESEARCH_VERSION
    from stock_research_graph import STOCK_GRAPH_VERSION

    return STOCK_GRAPH_VERSION


def _safe_runtime_identity(runtime: Mapping[str, object]) -> dict:
    base_url = str(runtime.get("base_url") or "").strip().rstrip("/")
    return {
        "provider_id": str(runtime.get("provider_id") or ""),
        "model_id": str(runtime.get("model_id") or ""),
        "base_url_sha256": hashlib.sha256(base_url.encode("utf-8")).hexdigest()
        if base_url
        else "",
        "api_key_configured": bool(str(runtime.get("api_key") or "").strip()),
        "runtime_source": "chat_api.get_chat_model_runtime_config(local)",
    }


class FinancialFullResearchRouter:
    """Plan, cache-check and atomically enqueue existing research jobs."""

    def __init__(
        self,
        repository,
        *,
        settings,
        clock=None,
        runtime_config_loader=None,
    ):
        self.repository = repository
        self.database = repository.db
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(UTC))
        self.runtime_config_loader = runtime_config_loader or self._default_runtime
        self.database._ensure_connection()
        self.connection = self.database.connection
        self.universes = FinancialUniversePlanner(self.connection)
        with self.database.lock:
            self.universes.load_controlled_seed()

    @staticmethod
    def _default_runtime() -> Mapping[str, object]:
        from chat_api import get_chat_model_runtime_config

        return get_chat_model_runtime_config("local")

    def _cache_ttl(self) -> int:
        value = _setting(
            self.settings,
            "FINANCIAL_RESEARCH_CACHE_SECONDS",
            _setting(self.settings, "FINANCIAL_NEWS_FRESHNESS_SECONDS", 3600),
        )
        return max(60, min(86400, int(value)))

    def _provider_profile(self) -> dict:
        state = financial_capabilities(self.settings)
        policies = PROVIDER_POLICIES
        enabled = [key for key in PROVIDER_CAPABILITIES if state["effective"].get(key)]
        permission_profiles: dict[str, object] = {}
        with self.database.lock:
            rows = self.connection.execute(
                "SELECT provider_key, metadata_json FROM financial_provider_profiles"
            ).fetchall()
        for row in rows:
            provider_id = str(row[0])
            if provider_id not in enabled:
                continue
            try:
                metadata = json.loads(str(row[1] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
            probe = metadata.get("permission_probe") if isinstance(metadata, Mapping) else None
            if isinstance(probe, Mapping):
                permission_profiles[provider_id] = {
                    "probe_version": str(probe.get("probe_version") or ""),
                    "overall": str(probe.get("overall") or ""),
                    "token_status": str(probe.get("token_status") or ""),
                    "capabilities": dict(probe.get("capabilities") or {}),
                }
        profile = {
            "adapter_version": ADAPTER_VERSION,
            "enabled_provider_ids": enabled,
            "provider_policies": {
                key: {
                    "schema_version": policies[key].get("schema_version"),
                    "package_version": policies[key].get("package_version", ""),
                    "package_wheel_sha256": policies[key].get(
                        "package_wheel_sha256", ""
                    ),
                    "access_tier": policies[key]["access_tier"],
                    "license_profile": policies[key]["license_profile"],
                    "capabilities": list(policies[key]["capabilities"]),
                    "daily_call_budget": policies[key]["daily_call_budget"],
                    "fallback_rank": policies[key]["fallback_rank"],
                }
                for key in enabled
            },
            "provider_runtime_settings": {
                key: int(_setting(self.settings, key, 0))
                for key in PROVIDER_CONFIG_KEYS
            },
            "alpha_vantage_realtime_entitled": bool(
                _setting(self.settings, "ALPHA_VANTAGE_REALTIME_ENTITLED", False)
            ),
            "provider_chains": {
                market: {kind: list(values) for kind, values in chains.items()}
                for market, chains in DEFAULT_PROVIDER_CHAINS.items()
            },
            "permission_profiles": permission_profiles,
        }
        return {**profile, "provider_profile_hash": _digest(profile)}

    def _research_config(self, graph_version: str, runtime: Mapping[str, object]) -> dict:
        safe_runtime = _safe_runtime_identity(runtime)
        values = {
            key: int(_setting(self.settings, key, 0)) for key in RESEARCH_CONFIG_KEYS
        }
        config = {
            "router_version": FULL_RESEARCH_ROUTER_VERSION,
            "graph_version": graph_version,
            "research_settings": values,
            "local_llm": safe_runtime,
        }
        return {**config, "config_hash": _digest(config)}

    @staticmethod
    def _as_of(financial_intent: Mapping[str, object]) -> tuple[str, dict]:
        value = financial_intent.get("as_of")
        as_of = dict(value) if isinstance(value, Mapping) else {}
        freshness = str(financial_intent.get("freshness") or "unspecified")
        if freshness == "historical" and as_of:
            return "historical:" + _digest(as_of), as_of
        return "latest", as_of

    def _scope_from_universe(self, universe_key: str, now: datetime) -> dict:
        record = self.universes.get_universe(universe_key, as_of=now.date())
        return {
            "scope_type": "universe",
            "instrument_id": None,
            "universe_id": record.universe_id,
            "asset_type": "index",
            "market": record.market,
            "target": record.to_dict(),
        }

    @staticmethod
    def _scope_from_target(target: Mapping[str, object]) -> dict:
        return {
            "scope_type": "instrument",
            "instrument_id": int(target["instrument_id"]),
            "universe_id": None,
            "asset_type": str(target.get("asset_type") or ""),
            "market": str(target.get("market") or ""),
            "target": dict(target),
        }

    def plan(
        self,
        payload: Mapping[str, object],
        financial_intent: Mapping[str, object],
        target_resolution: Mapping[str, object],
        server_time_context: Mapping[str, object],
    ) -> dict:
        if not financial_intent.get("needs_full_research"):
            return skipped_full_research("full_research_not_requested")
        requested_text = str(server_time_context.get("server_now_utc") or "")
        try:
            requested_at = _parse_utc(requested_text)
        except (TypeError, ValueError):
            return unavailable_full_research("invalid_server_time", requested_at=requested_text)
        state = financial_product_capabilities(
            str(payload.get("industry_pack_id") or ""),
            settings=self.settings,
        )
        if not state["effective"]["trading_agents"]:
            return unavailable_full_research(
                state["reasons"]["trading_agents"], requested_at=requested_text
            )
        try:
            runtime = dict(self.runtime_config_loader())
        except Exception:
            return unavailable_full_research(
                "local_llm_configuration_unavailable", requested_at=requested_text
            )
        safe_runtime = _safe_runtime_identity(runtime)
        if (
            safe_runtime["provider_id"] != "local"
            or not safe_runtime["model_id"]
            or not safe_runtime["base_url_sha256"]
            or not safe_runtime["api_key_configured"]
        ):
            return unavailable_full_research(
                "local_llm_not_configured", requested_at=requested_text
            )
        scopes = []
        targets = list(target_resolution.get("targets") or [])
        if str(target_resolution.get("status") or "") == "resolved" and targets:
            scopes.extend(self._scope_from_target(item) for item in targets)
        elif financial_intent.get("universe"):
            try:
                scopes.append(
                    self._scope_from_universe(
                        str(financial_intent["universe"]), requested_at
                    )
                )
            except (LookupError, TypeError, ValueError):
                return unavailable_full_research(
                    "research_universe_not_resolved", requested_at=requested_text
                )
        else:
            return unavailable_full_research(
                "stable_research_scope_required", requested_at=requested_text
            )

        if any(
            scope["scope_type"] == "universe" or scope["asset_type"] == "index"
            for scope in scopes
        ) and not rollout_capability_enabled("index_research", self.settings):
            return unavailable_full_research(
                rollout_capability_reason("index_research", self.settings),
                requested_at=requested_text,
            )

        as_of_key, as_of = self._as_of(financial_intent)
        ttl = self._cache_ttl()
        provider = self._provider_profile()
        prepared = []
        for original in scopes:
            scope = dict(original)
            scope["industry_pack_id"] = state["industry_pack_id"]
            graph_version = _graph_version(scope["asset_type"], scope["scope_type"])
            config = self._research_config(graph_version, runtime)
            identity = {
                "scope_type": scope["scope_type"],
                "instrument_id": scope["instrument_id"],
                "universe_id": scope["universe_id"],
                "as_of_key": as_of_key,
                "graph_version": graph_version,
                "provider_profile_hash": provider["provider_profile_hash"],
                "config_hash": config["config_hash"],
            }
            cache_key = _digest(identity)
            request_window = int(requested_at.timestamp() // ttl)
            request_key = _digest({"cache_key": cache_key, "window": request_window})
            prepared.append(
                {
                    **scope,
                    "graph_version": graph_version,
                    "as_of_key": as_of_key,
                    "as_of": as_of,
                    "provider_profile_hash": provider["provider_profile_hash"],
                    "provider_profile": provider,
                    "config_hash": config["config_hash"],
                    "research_config": config,
                    "cache_key": cache_key,
                    "request_window": request_window,
                    "request_key": request_key,
                }
            )
        return validate_full_research(
            {
                "schema_version": FULL_RESEARCH_SCHEMA_VERSION,
                "status": "planned",
                "requested_at_utc": requested_text,
                "completed_at_utc": "",
                "cache_ttl_seconds": ttl,
                "scopes": prepared,
                "reports": [],
                "jobs": [],
                "research_run_ids": [],
                "answer_allowed": False,
                "route_destination": "financial_full_research",
                "reason_codes": ["full_research_scope_and_cache_identity_resolved"],
                "error": {},
            }
        )

    @staticmethod
    def _report_version(payload: Mapping[str, object]) -> str:
        return str(
            payload.get("graph_version")
            or payload.get("component_version")
            or ""
        )

    def _valid_report(
        self,
        scope: Mapping[str, object],
        *,
        now: datetime,
        ttl: int,
    ) -> Optional[dict]:
        clause = "run.instrument_id=?" if scope["scope_type"] == "instrument" else "run.universe_id=?"
        identity = scope["instrument_id"] if scope["scope_type"] == "instrument" else scope["universe_id"]
        rows = self.connection.execute(
            f"""
            SELECT report.id, report.research_run_id, report.report_status,
                   report.recommendation, report.confidence, report.title,
                   report.executive_summary, report.report_json,
                   report.observed_at, report.fetched_at, report.verified_at,
                   report.updated_at, run.config_json, run.completed_at
            FROM financial_final_reports report
            JOIN financial_research_runs run ON run.id=report.research_run_id
            WHERE {clause} AND run.status='completed'
            ORDER BY report.updated_at DESC, report.id DESC LIMIT 100
            """,
            (int(identity),),
        ).fetchall()
        for row in rows:
            try:
                run_config = json.loads(str(row[12] or "{}"))
                report_json = json.loads(str(row[7] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(run_config, Mapping) or not isinstance(report_json, Mapping):
                continue
            if str(run_config.get("cache_key") or "") != str(scope["cache_key"]):
                continue
            if self._report_version(report_json) != str(scope["graph_version"]):
                continue
            report_status = str(row[2] or "").casefold()
            if report_status in TERMINAL_REPORT_DENYLIST:
                continue
            timestamp_text = row[9] or row[13] or row[11]
            try:
                timestamp = _parse_utc(timestamp_text)
            except (TypeError, ValueError):
                continue
            age = (now - timestamp).total_seconds()
            if age < 0 or age > ttl:
                continue
            observed_at = str(row[8] or "")
            if not _observed_not_future(observed_at, now):
                continue
            return {
                "report_id": int(row[0]),
                "research_run_id": str(row[1]),
                "report_status": str(row[2]),
                "recommendation": str(row[3] or "insufficient_evidence"),
                "confidence": _finite(row[4]),
                "title": str(row[5] or ""),
                "executive_summary": str(row[6] or "")[:4000],
                "observed_at": observed_at,
                "fetched_at": str(row[9] or ""),
                "verified_at": str(row[10] or ""),
                "graph_version": str(scope["graph_version"]),
                "cache_key": str(scope["cache_key"]),
                "age_seconds": round(age, 3),
                "target": dict(scope["target"]),
                "source_refs": self._source_refs(str(row[1])),
            }
        return None

    def _source_refs(self, research_run_id: str) -> list[dict]:
        """Project public evidence metadata, never payloads or document bodies."""

        rows = self.connection.execute(
            """
            SELECT evidence.evidence_kind, evidence.snapshot_id, evidence.article_id,
                   evidence.observed_at,
                   snapshot.fetched_at, snapshot.source_url,
                   profile.provider_key,
                   article.title, article.url, article.publish_date,
                   article.first_crawled
            FROM financial_research_evidence evidence
            LEFT JOIN financial_data_snapshots snapshot
              ON snapshot.id=evidence.snapshot_id
            LEFT JOIN financial_provider_profiles profile
              ON profile.id=snapshot.provider_profile_id
            LEFT JOIN articles article
              ON article.id=evidence.article_id AND article.status='active'
            WHERE evidence.research_run_id=?
            ORDER BY evidence.observed_at DESC, evidence.id DESC
            LIMIT 30
            """,
            (str(research_run_id),),
        ).fetchall()
        result = []
        for row in rows:
            kind = str(row[0] or "")
            if kind == "structured_snapshot" and row[1] is not None:
                result.append(
                    {
                        "source_kind": kind,
                        "snapshot_id": int(row[1]),
                        "provider_key": str(row[6] or ""),
                        "source_url": str(row[5] or ""),
                        "title": str(row[6] or "金融数据快照"),
                        "observed_at": str(row[3] or ""),
                        "fetched_at": str(row[4] or ""),
                    }
                )
            elif kind == "source_document" and row[2] is not None and row[7] is not None:
                result.append(
                    {
                        "source_kind": kind,
                        "article_id": int(row[2]),
                        "title": str(row[7] or "原始金融资料"),
                        "url": str(row[8] or ""),
                        "observed_at": str(row[9] or row[3] or ""),
                        "fetched_at": str(row[10] or ""),
                    }
                )
        return result

    @staticmethod
    def _safe_question(payload: Mapping[str, object]) -> str:
        for item in reversed(list(payload.get("messages") or [])):
            if isinstance(item, Mapping) and item.get("role") == "user":
                return str(item.get("content") or "")[:12000]
        return ""

    def _existing_attempt(self, scope: Mapping[str, object]) -> Optional[dict]:
        rows = self.connection.execute(
            """
            SELECT run.id, run.status, run.config_json, job.id, job.status
            FROM financial_research_runs run
            LEFT JOIN intel_jobs job ON job.id=CAST(
                json_extract(run.config_json, '$.job_id') AS INTEGER
            )
            WHERE ((?='instrument' AND run.instrument_id=?)
                OR (?='universe' AND run.universe_id=?))
            ORDER BY run.requested_at DESC, run.id DESC LIMIT 100
            """,
            (
                scope["scope_type"],
                scope["instrument_id"],
                scope["scope_type"],
                scope["universe_id"],
            ),
        ).fetchall()
        for row in rows:
            try:
                config = json.loads(str(row[2] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if str(config.get("request_key") or "") != str(scope["request_key"]):
                continue
            job_status = str(row[4] or "")
            run_status = str(row[1] or "")
            effective = job_status or run_status
            if effective in {"queued", "retry_wait", "running"}:
                status = "running" if effective == "running" else "queued"
            elif effective == "cancelled":
                status = "cancelled"
            elif effective in {"failed", "completed"}:
                status = "failed"
            else:
                status = "failed"
            return {
                "status": status,
                "job_id": int(row[3]) if row[3] is not None else None,
                "research_run_id": str(row[0]),
                "created": False,
                "error_code": (
                    "research_job_cancelled"
                    if status == "cancelled"
                    else "research_report_missing_after_terminal_job"
                    if status == "failed"
                    else ""
                ),
                "target": dict(scope["target"]),
            }
        return None

    def _enqueue_scope(
        self,
        scope: Mapping[str, object],
        payload: Mapping[str, object],
        *,
        now: datetime,
        ttl: int,
    ) -> dict:
        with self.database.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                report = self._valid_report(scope, now=now, ttl=ttl)
                if report is not None:
                    self.connection.execute("COMMIT")
                    return {"status": "cache_hit", "report": report}
                existing = self._existing_attempt(scope)
                if existing is not None:
                    self.connection.execute("COMMIT")
                    return existing

                run_id = "financial-research-" + _digest(
                    {"request_key": scope["request_key"]}
                )[:40]
                request_id = "chat-research-" + _digest(
                    {"run_id": run_id, "requested_at": _utc_text(now)}
                )[:32]
                config_json = {
                    "router_version": FULL_RESEARCH_ROUTER_VERSION,
                    "graph_version": scope["graph_version"],
                    "cache_key": scope["cache_key"],
                    "request_key": scope["request_key"],
                    "request_window": scope["request_window"],
                    "as_of_key": scope["as_of_key"],
                    "provider_profile_hash": scope["provider_profile_hash"],
                    "config_hash": scope["config_hash"],
                    "research_config": scope["research_config"],
                    "provider_profile": scope["provider_profile"],
                    "job_id": None,
                }
                session_id = str(payload.get("session_id") or "")[:200]
                question = self._safe_question(payload)
                self.connection.execute(
                    """
                    INSERT INTO financial_research_runs(
                        id, trigger_type, scope_type, instrument_id, universe_id,
                        chat_session_id, user_question, status, current_stage,
                        time_context_json, config_json, llm_call_budget,
                        token_budget, debate_round_budget, requested_at
                    ) VALUES(?, 'chat', ?, ?, ?, ?, ?, 'queued',
                             'waiting_for_worker', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        scope["scope_type"],
                        scope["instrument_id"],
                        scope["universe_id"],
                        session_id,
                        question,
                        _canonical(
                            {
                                "requested_at_utc": _utc_text(now),
                                "as_of_key": scope["as_of_key"],
                                "as_of": scope["as_of"],
                            }
                        ),
                        _canonical(config_json),
                        int(
                            _setting(
                                self.settings, "FINANCIAL_RESEARCH_MAX_LLM_CALLS", 30
                            )
                        ),
                        int(
                            _setting(
                                self.settings, "FINANCIAL_RESEARCH_MAX_TOKENS", 120000
                            )
                        ),
                        int(
                            _setting(
                                self.settings,
                                "FINANCIAL_RESEARCH_MAX_DEBATE_ROUNDS",
                                2,
                            )
                        ),
                        _utc_text(now),
                    ),
                )
                job_payload = {
                    "research_run_id": run_id,
                    "industry_pack_id": scope["industry_pack_id"],
                    "scope_type": scope["scope_type"],
                    "instrument_id": scope["instrument_id"],
                    "universe_id": scope["universe_id"],
                    "asset_type": scope["asset_type"],
                    "market": scope["market"],
                    "graph_version": scope["graph_version"],
                    "as_of_key": scope["as_of_key"],
                    "as_of": scope["as_of"],
                    "requested_at_utc": _utc_text(now),
                    "cache_key": scope["cache_key"],
                    "config_hash": scope["config_hash"],
                    "provider_profile_hash": scope["provider_profile_hash"],
                    "trigger": "chat_full_research",
                }
                cursor = self.connection.execute(
                    """
                    INSERT INTO intel_jobs(
                        job_type, dedupe_key, payload_json, status, priority,
                        max_attempts, request_id, created_by, created_at, updated_at
                    ) VALUES('financial_research', ?, ?, 'queued', 50, 3, ?,
                             'financial_full_research_router', ?, ?)
                    """,
                    (
                        f"financial-research:chat:{scope['request_key']}",
                        _canonical(job_payload),
                        request_id,
                        _utc_text(now),
                        _utc_text(now),
                    ),
                )
                job_id = int(cursor.lastrowid)
                config_json["job_id"] = job_id
                self.connection.execute(
                    "UPDATE financial_research_runs SET config_json=? WHERE id=?",
                    (_canonical(config_json), run_id),
                )
                self.connection.execute("COMMIT")
                return {
                    "status": "queued",
                    "job_id": job_id,
                    "research_run_id": run_id,
                    "created": True,
                    "error_code": "",
                    "target": dict(scope["target"]),
                }
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _aggregate_status(items: Sequence[Mapping[str, object]]) -> str:
        states = {str(item.get("status") or "failed") for item in items}
        if states == {"cache_hit"}:
            return "cache_hit"
        if len(states) > 1:
            return "mixed"
        status = next(iter(states), "failed")
        return status if status in FULL_RESEARCH_STATUSES else "failed"

    def activate(self, planned: Mapping[str, object], payload: Mapping[str, object]) -> dict:
        route = validate_full_research(planned)
        if route["status"] != "planned":
            return route
        now = max(_parse_utc(route["requested_at_utc"]), _utc(self.clock(), "clock"))
        ttl = int(route["cache_ttl_seconds"])
        items = []
        reasons = list(route["reason_codes"])
        for scope in route["scopes"]:
            try:
                items.append(
                    self._enqueue_scope(scope, payload, now=now, ttl=ttl)
                )
            except Exception:
                items.append(
                    {
                        "status": "failed",
                        "job_id": None,
                        "research_run_id": "",
                        "created": False,
                        "error_code": "research_queue_transaction_failed",
                        "target": dict(scope.get("target") or {}),
                    }
                )
        reports = [dict(item["report"]) for item in items if item.get("report")]
        jobs = [dict(item) for item in items if not item.get("report")]
        run_ids = sorted(
            {
                str(item.get("research_run_id") or "")
                for item in items
                if item.get("research_run_id")
            }
        )
        status = self._aggregate_status(items)
        if status == "cache_hit":
            reasons.append("compatible_fresh_report_reused")
        elif status in {"queued", "running", "mixed"}:
            reasons.append("existing_worker_financial_research_job_selected")
        elif status in {"failed", "cancelled"}:
            reasons.append("research_terminal_state_no_opinion_fallback")
        return validate_full_research(
            {
                **route,
                "status": status,
                "completed_at_utc": _utc_text(now),
                "reports": reports,
                "jobs": jobs,
                "research_run_ids": run_ids,
                "answer_allowed": status == "cache_hit" and len(reports) == len(items),
                "reason_codes": reasons,
                "error": (
                    {"error_code": "research_not_ready_or_failed"}
                    if status in {"failed", "cancelled"}
                    else {}
                ),
            }
        )


def format_full_research_answer(route: Mapping[str, object]) -> str:
    current = validate_full_research(route)
    if current["status"] == "cache_hit":
        blocks = []
        for report in current["reports"]:
            target = report.get("target") or {}
            name = str(
                target.get("display_name")
                or target.get("universe_key")
                or "金融研究标的"
            )
            symbol = str(target.get("canonical_symbol") or "")
            label = f"{name}（{symbol}）" if symbol else name
            summary = str(report.get("executive_summary") or "暂无执行摘要")
            blocks.append(
                f"{label}：{summary}\n"
                f"研究观点={report.get('recommendation') or 'insufficient_evidence'}，"
                f"report_status={report.get('report_status')}，"
                f"observed_at={report.get('observed_at') or 'unknown'}，"
                f"fetched_at={report.get('fetched_at') or 'unknown'}，"
                f"research_run_id={report.get('research_run_id')}，"
                f"report_id={report.get('report_id')}。"
            )
        return "\n\n".join(blocks) + "\n以上为已入库的 TradingAgents 研究观点，不构成投资建议或真实交易指令。"

    if current["status"] in {"queued", "running", "mixed"}:
        descriptions = []
        for item in current["jobs"]:
            target = item.get("target") or {}
            name = str(
                target.get("display_name")
                or target.get("universe_key")
                or "金融研究标的"
            )
            descriptions.append(
                f"{name}: status={item.get('status')}，"
                f"research_run_id={item.get('research_run_id') or 'pending'}，"
                f"job_id={item.get('job_id') or 'pending'}"
            )
        return (
            "TradingAgents 完整研究尚未全部就绪："
            + "；".join(descriptions)
            + "。本轮不回退为无来源观点；研究完成后复用同一报告。"
        )

    reason = str((current.get("error") or {}).get("error_code") or "research_unavailable")
    return (
        f"TradingAgents 完整研究当前不可用（error_code={reason}）。"
        "本轮不会改用通用模型生成无来源的投资观点，也不会创建真实订单。"
    )


__all__ = [
    "FULL_RESEARCH_SCHEMA",
    "FULL_RESEARCH_SCHEMA_VERSION",
    "FinancialFullResearchRouter",
    "format_full_research_answer",
    "skipped_full_research",
    "unavailable_full_research",
    "validate_full_research",
]

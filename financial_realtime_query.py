#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Evidence-gated query-through snapshots for simple financial facts."""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from jsonschema import Draft202012Validator

from financial_config import financial_capabilities
from financial_instruments import InstrumentRegistry
from financial_market_clock import MarketClockService, RequestTimeContext
from financial_provider_contract import (
    FinancialDataKind,
    FinancialDataRequest,
    FinancialProviderError,
    MarketStatus,
)
from tradingagents_cn_data_adapter import (
    DEFAULT_PROVIDER_CHAINS,
    build_default_financial_provider_router,
)


UTC = timezone.utc
REALTIME_QUERY_SCHEMA_VERSION = "financial-realtime-query-v1"
REALTIME_QUERY_STATUSES = (
    "skipped",
    "planned",
    "ready",
    "stale",
    "conflict",
    "unavailable",
    "degraded",
)
ACTIVE_REFRESH_STATES = frozenset(
    {MarketStatus.OPEN.value, MarketStatus.LUNCH_BREAK.value, MarketStatus.UNKNOWN.value}
)
# Query-through supports the project's existing US quote providers without
# widening the deliberately A/H-only TradingAgentsCNDataAdapter contract.
REALTIME_PROVIDER_CHAINS: Mapping[str, tuple[str, ...]] = {
    **{
        market: tuple(chains.get("quote", ()))
        for market, chains in DEFAULT_PROVIDER_CHAINS.items()
    },
    "XHKG": (
        "alpha_vantage",
        *tuple(DEFAULT_PROVIDER_CHAINS["XHKG"].get("quote", ())),
    ),
    "US": ("yahoo", "alpha_vantage"),
    "JP": ("yahoo",),
}
QUOTE_PROVIDER_MARKETS: Mapping[str, frozenset[str]] = {
    "akshare_cn": frozenset({"CN", "CN_FUND"}),
    "tushare_cn": frozenset({"CN", "CN_FUND"}),
    "easyquotation": frozenset({"CN", "CN_FUND", "XHKG"}),
    "yahoo": frozenset({"CN", "CN_FUND", "XHKG", "US", "JP"}),
    "alpha_vantage": frozenset({"CN", "CN_FUND", "XHKG", "US"}),
}
CONFLICT_RELATIVE_TOLERANCE = 0.005
DEFAULT_INVALID_SYMBOL_NEGATIVE_CACHE_SECONDS = 86400
REALTIME_QUERY_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "schema_version", "status", "target", "metric", "requested_at_utc",
        "completed_at_utc", "market_session", "cache", "refresh", "evidence",
        "answer_allowed", "numeric_claims_allowed", "route_destination",
        "reason_codes", "elapsed_ms",
    ],
    "properties": {
        "schema_version": {"const": REALTIME_QUERY_SCHEMA_VERSION},
        "status": {"enum": list(REALTIME_QUERY_STATUSES)},
        "target": {"type": "object"},
        "metric": {"type": "string"},
        "requested_at_utc": {"type": "string"},
        "completed_at_utc": {"type": "string"},
        "market_session": {"type": "object"},
        "cache": {"type": "object"},
        "refresh": {"type": "object"},
        "evidence": {"type": "array", "items": {"type": "object"}},
        "answer_allowed": {"type": "boolean"},
        "numeric_claims_allowed": {"type": "boolean"},
        "route_destination": {"type": "string"},
        "reason_codes": {"type": "array", "items": {"type": "string"}},
        "elapsed_ms": {"type": "integer", "minimum": 0},
    },
    "additionalProperties": False,
}
_VALIDATOR = Draft202012Validator(REALTIME_QUERY_SCHEMA)


def validate_realtime_query(payload: Mapping[str, object]) -> dict:
    result = dict(payload)
    _VALIDATOR.validate(result)
    return result


def skipped_realtime_query(reason: str) -> dict:
    return validate_realtime_query(
        {
            "schema_version": REALTIME_QUERY_SCHEMA_VERSION,
            "status": "skipped",
            "target": {},
            "metric": "last_price",
            "requested_at_utc": "",
            "completed_at_utc": "",
            "market_session": {},
            "cache": {},
            "refresh": {},
            "evidence": [],
            "answer_allowed": False,
            "numeric_claims_allowed": False,
            "route_destination": "normal_chat",
            "reason_codes": [str(reason)],
            "elapsed_ms": 0,
        }
    )


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _parse_utc(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    return _utc(parsed, "datetime")


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


def _setting(settings: object, name: str, default: object) -> object:
    if isinstance(settings, Mapping):
        return settings.get(name, default)
    return getattr(settings, name, default)


def _calendar_id(target: Mapping[str, object]) -> str:
    exchange = str(target.get("exchange") or "").upper()
    market = str(target.get("market") or "").upper()
    if exchange in {"XSHG", "XSHE", "XHKG", "XNAS", "XTKS"}:
        return exchange
    if market == "CN":
        return "XSHG"
    if market == "XHKG":
        return "XHKG"
    if market == "US":
        return "XNAS"
    if market == "JP":
        return "XTKS"
    return ""


def _quote_market(target: Mapping[str, object]) -> str:
    exchange = str(target.get("exchange") or "").strip().upper()
    market = str(target.get("market") or "").strip().upper()
    if exchange in {"XSHG", "XSHE"}:
        return "CN_FUND" if market == "CN_FUND" else "CN"
    if exchange == "XHKG" or market in {"HK", "XHKG"}:
        return "XHKG"
    if exchange in {"US", "XNAS", "XNYS", "ARCX"} or market == "US":
        return "US"
    return market


def _unknown_session(target: Mapping[str, object], now: datetime) -> dict:
    return {
        "market_calendar_id": _calendar_id(target) or "unconfigured",
        "market_timezone": "",
        "market_session_state": MarketStatus.UNKNOWN.value,
        "trading_date": now.date().isoformat(),
        "is_trading_day": False,
        "is_half_day": False,
        "calendar_source": "unconfigured",
        "calendar_version": "",
        "holiday_source_url": "",
        "hours_source_url": "",
        "session_open_utc": None,
        "session_close_utc": None,
        "next_transition_utc": None,
        "reason": "market_calendar_not_configured",
    }


class FinancialRealtimeQueryService:
    """Read cache, synchronously refresh when allowed, and return cited facts."""

    def __init__(
        self,
        database,
        *,
        settings,
        router=None,
        market_clock: Optional[MarketClockService] = None,
        clock=None,
    ):
        self.database = database
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(UTC))
        self.market_clock = market_clock or MarketClockService()
        self.database._ensure_connection()
        self.connection = self.database.connection
        self.instruments = InstrumentRegistry(self.connection)
        with self.database.lock:
            self.instruments.load_controlled_seed()
        self.router = router or build_default_financial_provider_router(
            self.connection,
            settings=settings,
            clock=self.clock,
        )
        self._locks_guard = threading.Lock()
        self._target_locks: dict[int, threading.Lock] = {}
        self._negative_quote_cache_lock = threading.Lock()
        self._negative_quote_cache: dict[tuple[str, int, str], datetime] = {}

    def _target_lock(self, instrument_id: int) -> threading.Lock:
        with self._locks_guard:
            return self._target_locks.setdefault(int(instrument_id), threading.Lock())

    def _negative_quote_cache_expiry(
        self,
        provider_id: str,
        instrument_id: int,
        *,
        endpoint: str = "quote",
        now: Optional[datetime] = None,
    ) -> Optional[datetime]:
        """返回仍有效的无效代码缓存，并惰性清理已过期记录。"""

        checked_at = _utc(now or self.clock(), "negative cache clock")
        key = (str(provider_id), int(instrument_id), str(endpoint))
        with self._negative_quote_cache_lock:
            expires_at = self._negative_quote_cache.get(key)
            if expires_at is not None and expires_at <= checked_at:
                self._negative_quote_cache.pop(key, None)
                return None
            return expires_at

    def _record_invalid_symbol_failures(
        self,
        instrument_id: int,
        errors: Sequence[Mapping[str, object]],
        *,
        observed_at: datetime,
        endpoint: str = "quote",
    ) -> None:
        ttl_seconds = max(
            1,
            int(
                _setting(
                    self.settings,
                    "FINANCIAL_INVALID_SYMBOL_NEGATIVE_CACHE_SECONDS",
                    DEFAULT_INVALID_SYMBOL_NEGATIVE_CACHE_SECONDS,
                )
            ),
        )
        expires_at = _utc(observed_at, "negative cache observation") + timedelta(
            seconds=ttl_seconds
        )
        keys = {
            (str(item.get("provider_id") or ""), int(instrument_id), str(endpoint))
            for item in errors
            if str(item.get("error_code") or "") == "invalid_symbol"
            and str(item.get("provider_id") or "")
        }
        if not keys:
            return
        with self._negative_quote_cache_lock:
            for key in keys:
                self._negative_quote_cache[key] = expires_at

    def _market_session(self, target: Mapping[str, object], now: datetime) -> dict:
        calendar_id = _calendar_id(target)
        if not calendar_id:
            return _unknown_session(target, now)
        context = RequestTimeContext(
            server_now_utc=now,
            server_timezone="Asia/Hong_Kong",
            user_timezone="Asia/Hong_Kong",
        )
        try:
            return self.market_clock.market_state(calendar_id, context).to_dict()
        except (KeyError, RuntimeError, TypeError, ValueError):
            return _unknown_session(target, now)

    def plan(
        self,
        financial_intent: Mapping[str, object],
        target_resolution: Mapping[str, object],
        server_time_context: Mapping[str, object],
    ) -> dict:
        if str(financial_intent.get("intent") or "") != "market_fact":
            return skipped_realtime_query("not_a_market_fact_intent")
        if str(financial_intent.get("freshness") or "") not in {"realtime", "latest"}:
            return skipped_realtime_query("historical_fact_not_query_through")
        if str(target_resolution.get("status") or "") != "resolved":
            return skipped_realtime_query("stable_instrument_required")
        targets = list(target_resolution.get("targets") or [])
        if len(targets) != 1:
            return skipped_realtime_query("single_instrument_fact_required")
        target = dict(targets[0])
        if str(target.get("asset_type") or "") not in {"equity", "index", "etf"}:
            return skipped_realtime_query("asset_has_no_realtime_quote_contract")
        now = _parse_utc(server_time_context.get("server_now_utc"))
        return validate_realtime_query(
            {
                "schema_version": REALTIME_QUERY_SCHEMA_VERSION,
                "status": "planned",
                "target": target,
                "metric": "last_price",
                "requested_at_utc": _utc_text(now),
                "completed_at_utc": "",
                "market_session": self._market_session(target, now),
                "cache": {},
                "refresh": {},
                "evidence": [],
                "answer_allowed": False,
                "numeric_claims_allowed": False,
                "route_destination": "financial_realtime_snapshot",
                "reason_codes": ["simple_realtime_fact_query"],
                "elapsed_ms": 0,
            }
        )

    @staticmethod
    def _price_fields(payload: Mapping[str, object]) -> dict:
        normalized = payload.get("normalized_payload")
        normalized = normalized if isinstance(normalized, Mapping) else {}
        value = payload.get("value")
        price = _finite(value) if not isinstance(value, Mapping) else None
        if price is None:
            for key in ("last_price", "price", "close"):
                price = _finite(normalized.get(key))
                if price is not None:
                    break
        previous_close = _finite(
            normalized.get("previous_close") or normalized.get("pre_close")
        )
        change = _finite(normalized.get("change"))
        change_percent = _finite(
            normalized.get("change_percent") or normalized.get("pct_change")
        )
        derived = False
        if price is not None and previous_close not in {None, 0.0}:
            if change is None:
                change = price - previous_close
                derived = True
            if change_percent is None:
                change_percent = (price - previous_close) / previous_close * 100.0
                derived = True
        return {
            "price": price,
            "previous_close": previous_close,
            "change": change,
            "change_percent": change_percent,
            "open": _finite(normalized.get("open")),
            "high": _finite(normalized.get("high")),
            "low": _finite(normalized.get("low")),
            "volume": _finite(normalized.get("volume")),
            "turnover": _finite(normalized.get("turnover")),
            "change_derived_from_price_and_previous_close": derived,
        }

    def _load_evidence(
        self,
        instrument_id: int,
        *,
        now: datetime,
        market_session: Mapping[str, object],
        snapshot_ids: Sequence[int] = (),
    ) -> list[dict]:
        parameters: list[object] = [int(instrument_id)]
        id_filter = ""
        if snapshot_ids:
            placeholders = ",".join("?" for _ in snapshot_ids)
            id_filter = f" AND s.id IN ({placeholders})"
            parameters.extend(int(item) for item in snapshot_ids)
        with self.database.lock:
            rows = self.connection.execute(
                f"""
                SELECT s.id, s.observed_at, s.fetched_at, s.market_status,
                       s.currency, s.timezone, s.stale_after, s.quality_status,
                       s.payload_json, s.payload_sha256, s.source_url,
                       p.provider_key, p.display_name, p.attribution_text
                FROM financial_data_snapshots s
                JOIN financial_provider_profiles p ON p.id=s.provider_profile_id
                WHERE s.instrument_id=? AND s.data_type='quote' {id_filter}
                ORDER BY s.observed_at DESC, s.fetched_at DESC, s.id DESC
                LIMIT 40
                """,
                parameters,
            ).fetchall()
        threshold = int(
            _setting(self.settings, "FINANCIAL_QUOTE_FRESHNESS_SECONDS", 300)
        )
        latest_by_provider: dict[str, dict] = {}
        for row in rows:
            payload_text = str(row[8] or "")
            if hashlib.sha256(payload_text.encode("utf-8")).hexdigest() != str(row[9]):
                continue
            try:
                payload = json.loads(payload_text)
                observed = _parse_utc(row[1])
                fetched = _parse_utc(row[2])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if observed > now or fetched > now:
                continue
            values = self._price_fields(payload)
            if values["price"] is None:
                continue
            stale_after = None
            if row[6]:
                try:
                    stale_after = _parse_utc(row[6])
                except (TypeError, ValueError):
                    stale_after = None
            state = str(market_session.get("market_session_state") or "unknown")
            age_seconds = max(0.0, (now - observed).total_seconds())
            quality_status = str(row[7] or "")
            forced_stale = quality_status.endswith(("_stale", "_historical"))
            current = (
                state in ACTIVE_REFRESH_STATES
                and age_seconds <= threshold
                and not forced_stale
                and (stale_after is None or stale_after > now)
            )
            provider_id = str(row[11])
            latest_by_provider.setdefault(
                provider_id,
                {
                    "snapshot_id": int(row[0]),
                    "provider_id": provider_id,
                    "provider_display_name": str(row[12] or provider_id),
                    "attribution_text": str(row[13] or ""),
                    "source_url": str(row[10] or ""),
                    "observed_at": _utc_text(observed),
                    "fetched_at": _utc_text(fetched),
                    "market_status": str(row[3] or "unknown"),
                    "server_market_session": state,
                    "currency": str(row[4] or ""),
                    "timezone": str(row[5] or ""),
                    "quality_status": quality_status,
                    "freshness": "current" if current else "stale",
                    "age_seconds": round(age_seconds, 3),
                    **values,
                },
            )
        return sorted(
            latest_by_provider.values(),
            key=lambda item: (
                item["observed_at"], item["fetched_at"], item["snapshot_id"]
            ),
            reverse=True,
        )

    @staticmethod
    def _conflicting(values: Sequence[Mapping[str, object]]) -> bool:
        prices = [float(item["price"]) for item in values if item.get("price") is not None]
        if len(prices) >= 2:
            low, high = min(prices), max(prices)
            tolerance = max(
                0.0001,
                max(abs(low), abs(high)) * CONFLICT_RELATIVE_TOLERANCE,
            )
            if high - low > tolerance:
                return True
        statuses = {
            str(item.get("market_status") or "").casefold()
            for item in values
            if str(item.get("market_status") or "").casefold()
            not in {"", MarketStatus.UNKNOWN.value}
        }
        return len(statuses) > 1

    @staticmethod
    def _result(
        planned: Mapping[str, object],
        *,
        status: str,
        completed_at: datetime,
        cache: Mapping[str, object],
        refresh: Mapping[str, object],
        evidence: Sequence[Mapping[str, object]],
        reasons: Sequence[str],
        elapsed_ms: int,
    ) -> dict:
        has_evidence = bool(evidence)
        return validate_realtime_query(
            {
                **dict(planned),
                "status": status,
                "completed_at_utc": _utc_text(completed_at),
                "cache": dict(cache),
                "refresh": dict(refresh),
                "evidence": [dict(item) for item in evidence],
                "answer_allowed": has_evidence,
                "numeric_claims_allowed": has_evidence and status in {"ready", "stale"},
                "reason_codes": list(planned.get("reason_codes") or [])
                + [str(item) for item in reasons],
                "elapsed_ms": max(0, int(elapsed_ms)),
            }
        )

    def execute(self, query: Mapping[str, object]) -> dict:
        planned = validate_realtime_query(query)
        if planned["status"] != "planned":
            return planned
        started = time.monotonic()
        target = dict(planned["target"])
        instrument_id = int(target["instrument_id"])
        with self._target_lock(instrument_id):
            return self._execute_locked(planned, started=started)

    def _fetch_independent_sources(
        self,
        request: FinancialDataRequest,
        provider_ids: Sequence[str],
    ) -> tuple[tuple[object, ...], tuple[dict, ...]]:
        """Collect independent responses without allowing provider fallback."""

        candidates = tuple(dict.fromkeys(str(item or "").strip() for item in provider_ids))
        candidates = tuple(item for item in candidates if item)
        fetch_many = getattr(self.router, "fetch_many", None)
        if callable(fetch_many):
            return fetch_many(
                request,
                candidate_provider_ids=candidates,
                max_workers=int(
                    _setting(
                        self.settings,
                        "FINANCIAL_QUOTE_SOURCE_MAX_WORKERS",
                        4,
                    )
                ),
            )

        def fetch_one(provider_id: str):
            provider_request = replace(
                request,
                request_id=f"{request.request_id}-{provider_id}",
                preferred_provider_id=provider_id,
            )
            try:
                response = self.router.fetch(
                    provider_request,
                    candidate_provider_ids=(provider_id,),
                    allow_fallback=False,
                )
            except FinancialProviderError as exc:
                return None, {
                    "provider_id": provider_id,
                    "error_code": exc.code.value,
                    "error_type": type(exc).__name__,
                    "retryable": bool(exc.retryable),
                    "gate_reason": str(exc.details.get("gate_reason") or ""),
                }
            except Exception as exc:
                return None, {
                    "provider_id": provider_id,
                    "error_code": "provider_contract_failed",
                    "error_type": type(exc).__name__,
                    "retryable": False,
                    "gate_reason": "",
                }
            if str(response.provider_id) != provider_id:
                return None, {
                    "provider_id": provider_id,
                    "error_code": "provider_identity_mismatch",
                    "error_type": "ProviderIdentityMismatch",
                    "retryable": False,
                    "gate_reason": "independent_provider_required",
                }
            return response, None

        responses_by_provider = {}
        errors_by_provider = {}
        workers = max(
            1,
            min(
                int(
                    _setting(
                        self.settings,
                        "FINANCIAL_QUOTE_SOURCE_MAX_WORKERS",
                        4,
                    )
                ),
                len(candidates),
            ),
        )
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="financial-quote-source",
        ) as executor:
            futures = {
                executor.submit(fetch_one, provider_id): provider_id
                for provider_id in candidates
            }
            for future in as_completed(futures):
                provider_id = futures[future]
                response, error = future.result()
                if response is not None:
                    responses_by_provider[provider_id] = response
                elif error is not None:
                    errors_by_provider[provider_id] = error
        return (
            tuple(
                responses_by_provider[item]
                for item in candidates
                if item in responses_by_provider
            ),
            tuple(
                errors_by_provider[item]
                for item in candidates
                if item in errors_by_provider
            ),
        )

    @staticmethod
    def _batch_error_code(errors: Sequence[Mapping[str, object]]) -> str:
        codes = [str(item.get("error_code") or "") for item in errors]
        for preferred in (
            "temporarily_unavailable",
            "rate_limited",
            "invalid_symbol",
            "unsupported_asset",
            "permission_denied",
            "provider_contract_failed",
        ):
            if preferred in codes:
                return preferred
        return codes[0] if codes else "provider_refresh_failed"

    def _eligible_quote_providers(
        self,
        target: Mapping[str, object],
        chain: Sequence[str],
    ) -> tuple[tuple[str, ...], tuple[dict, ...]]:
        instrument = self.instruments.get(int(target["instrument_id"]))
        mappings = dict(instrument.provider_mappings) if instrument is not None else {}
        market = _quote_market(target)
        availability_method = getattr(self.router, "provider_availability", None)
        availability = (
            dict(availability_method(chain)) if callable(availability_method) else {}
        )
        eligible = []
        diagnostics = []
        for provider_id in chain:
            reason = "eligible"
            negative_cache_expiry = None
            if provider_id not in mappings:
                reason = "provider_mapping_missing"
            elif market not in QUOTE_PROVIDER_MARKETS.get(provider_id, frozenset()):
                reason = "provider_market_unsupported"
            elif availability and not bool(
                availability.get(provider_id, {}).get("available", False)
            ):
                reason = str(
                    availability.get(provider_id, {}).get("reason")
                    or "provider_unavailable"
                )
            else:
                negative_cache_expiry = self._negative_quote_cache_expiry(
                    provider_id,
                    int(target["instrument_id"]),
                    now=_utc(self.clock(), "query clock"),
                )
                if negative_cache_expiry is not None:
                    reason = "invalid_symbol_negative_cache"
            if reason == "eligible":
                eligible.append(provider_id)
            diagnostic = {
                "provider_id": provider_id,
                "eligible": reason == "eligible",
                "reason": reason,
            }
            if negative_cache_expiry is not None:
                diagnostic["retry_after_utc"] = _utc_text(negative_cache_expiry)
            diagnostics.append(diagnostic)
        return tuple(eligible), tuple(diagnostics)

    def _execute_locked(self, planned: Mapping[str, object], *, started: float) -> dict:
        target = dict(planned["target"])
        instrument_id = int(target["instrument_id"])
        planned_now = _parse_utc(planned["requested_at_utc"])
        execution_now = max(planned_now, _utc(self.clock(), "query clock"))
        market_session = self._market_session(target, execution_now)
        working = {**dict(planned), "market_session": market_session}
        cached = self._load_evidence(
            instrument_id,
            now=execution_now,
            market_session=market_session,
        )
        current = [item for item in cached if item["freshness"] == "current"]
        cache_state = {
            "candidate_count": len(cached),
            "current_count": len(current),
            "checked_at_utc": _utc_text(execution_now),
        }
        market = _quote_market(target)
        configured_chain = tuple(REALTIME_PROVIDER_CHAINS.get(market, ()))
        eligible_chain, provider_eligibility = self._eligible_quote_providers(
            target,
            configured_chain,
        ) if configured_chain else ((), ())
        if self._conflicting(current):
            return self._result(
                working,
                status="conflict",
                completed_at=execution_now,
                cache=cache_state,
                refresh={"status": "not_started", "reason": "current_cache_conflict"},
                evidence=current,
                reasons=["current_provider_values_conflict"],
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        current_provider_ids = {
            str(item.get("provider_id") or "") for item in current
        }
        missing_current_sources = tuple(
            provider_id
            for provider_id in eligible_chain
            if provider_id not in current_provider_ids
        )
        if current and (len(current_provider_ids) >= 2 or not missing_current_sources):
            return self._result(
                working,
                status="ready",
                completed_at=execution_now,
                cache={**cache_state, "status": "fresh_hit"},
                refresh={
                    "status": "not_needed",
                    "provider_eligibility": [
                        dict(item) for item in provider_eligibility
                    ],
                },
                evidence=current,
                reasons=["fresh_persisted_quote_cache_hit"],
                elapsed_ms=(time.monotonic() - started) * 1000,
            )

        session_state = str(market_session.get("market_session_state") or "unknown")
        refresh_allowed = session_state in ACTIVE_REFRESH_STATES or not cached
        if not refresh_allowed and cached:
            return self._result(
                working,
                status="stale",
                completed_at=execution_now,
                cache={**cache_state, "status": "latest_closed_snapshot"},
                refresh={"status": "not_started", "reason": "market_not_active"},
                evidence=cached,
                reasons=["latest_snapshot_not_realtime_market_closed"],
                elapsed_ms=(time.monotonic() - started) * 1000,
            )

        state = financial_capabilities(self.settings)
        if not state["effective"]["financial_intelligence"]:
            return self._result(
                working,
                status="stale" if cached else "unavailable",
                completed_at=execution_now,
                cache=cache_state,
                refresh={
                    "status": "failed",
                    "error_code": state["reasons"]["financial_intelligence"],
                },
                evidence=cached[:1],
                reasons=["financial_intelligence_disabled"],
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        if not configured_chain:
            return self._result(
                working,
                status="stale" if cached else "unavailable",
                completed_at=execution_now,
                cache=cache_state,
                refresh={"status": "failed", "error_code": "no_quote_provider_chain"},
                evidence=cached[:1],
                reasons=["no_quote_provider_chain_for_market"],
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        if not eligible_chain:
            return self._result(
                working,
                status="stale" if cached else "unavailable",
                completed_at=execution_now,
                cache=cache_state,
                refresh={
                    "status": "failed",
                    "error_code": "no_eligible_quote_provider",
                    "provider_eligibility": [
                        dict(item) for item in provider_eligibility
                    ],
                },
                evidence=cached,
                reasons=["no_enabled_mapped_market_compatible_quote_provider"],
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        chain = missing_current_sources if current else eligible_chain

        request_identity = {
            "schema": REALTIME_QUERY_SCHEMA_VERSION,
            "instrument_id": instrument_id,
            "metric": planned["metric"],
            "bucket": int(execution_now.timestamp() // 5),
        }
        request_id = "chat-quote-" + hashlib.sha256(
            json.dumps(request_identity, sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]
        request = FinancialDataRequest(
            request_id=request_id,
            endpoint="quote",
            instrument_id=str(instrument_id),
            metric=str(planned["metric"]),
            data_kind=FinancialDataKind.QUOTE,
            requested_as_of=execution_now,
            preferred_provider_id=chain[0],
            parameters={"interval": "1m", "period": "1d"},
        )
        try:
            responses, provider_errors = self._fetch_independent_sources(
                request,
                chain,
            )
        except Exception as exc:
            responses = ()
            provider_errors = (
                {
                    "provider_id": "batch",
                    "error_code": "provider_contract_failed",
                    "error_type": type(exc).__name__,
                    "retryable": False,
                    "gate_reason": "",
                },
            )
        self._record_invalid_symbol_failures(
            instrument_id,
            provider_errors,
            observed_at=execution_now,
        )

        snapshot_ids = []
        persisted_provider_ids = []
        persistence_errors = []
        for response in responses:
            try:
                with self.database.lock:
                    persisted = tuple(self.router.persist_response(response))
                if len(persisted) != len(response.records):
                    raise RuntimeError("provider snapshot count mismatch")
            except Exception as exc:
                persistence_errors.append(
                    {
                        "provider_id": str(response.provider_id),
                        "error_code": "provider_persistence_failed",
                        "error_type": type(exc).__name__,
                        "retryable": False,
                        "gate_reason": "",
                    }
                )
                continue
            snapshot_ids.extend(int(item) for item in persisted)
            persisted_provider_ids.append(str(response.provider_id))

        all_errors = tuple(provider_errors) + tuple(persistence_errors)
        completed_at = max(execution_now, _utc(self.clock(), "query clock"))
        completed_session = self._market_session(target, completed_at)
        completed_working = {**working, "market_session": completed_session}
        if snapshot_ids:
            refreshed = self._load_evidence(
                instrument_id,
                now=completed_at,
                market_session=completed_session,
                snapshot_ids=snapshot_ids,
            )
            all_evidence = self._load_evidence(
                instrument_id,
                now=completed_at,
                market_session=completed_session,
            )
            all_current = [
                item for item in all_evidence if item["freshness"] == "current"
            ]
            refresh = {
                "status": "completed_partial" if all_errors else "completed",
                "request_id": request_id,
                "preferred_provider_id": chain[0],
                "actual_provider_ids": persisted_provider_ids,
                "attempted_provider_ids": list(chain),
                "provider_eligibility": [
                    dict(item) for item in provider_eligibility
                ],
                "provider_errors": [dict(item) for item in all_errors],
                "independent_source_count": len(persisted_provider_ids),
                "degraded": bool(all_errors),
                "degradation_reason": (
                    "one_or_more_independent_sources_failed" if all_errors else ""
                ),
                "snapshot_ids": list(snapshot_ids),
                # Keep every successfully persisted provider observation available
                # for a transparent, non-consensus projection.  The adjudicated
                # evidence list below remains restricted to current observations.
                "source_observations": [dict(item) for item in refreshed],
            }
            if self._conflicting(all_current):
                return self._result(
                    completed_working,
                    status="conflict",
                    completed_at=completed_at,
                    cache=cache_state,
                    refresh=refresh,
                    evidence=all_current,
                    reasons=["refreshed_provider_values_conflict"],
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
            if any(item["freshness"] == "current" for item in refreshed):
                return self._result(
                    completed_working,
                    status="ready",
                    completed_at=completed_at,
                    cache=cache_state,
                    refresh=refresh,
                    evidence=all_current,
                    reasons=["independent_provider_refresh_persisted"],
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
            fallback = refreshed or all_evidence or cached
            return self._result(
                completed_working,
                status="stale" if fallback else "unavailable",
                completed_at=completed_at,
                cache=cache_state,
                refresh={**refresh, "status": "completed_stale"},
                evidence=fallback,
                reasons=["provider_returned_non_realtime_snapshot"],
                elapsed_ms=(time.monotonic() - started) * 1000,
            )

        error_code = self._batch_error_code(all_errors)
        fallback = self._load_evidence(
            instrument_id,
            now=completed_at,
            market_session=completed_session,
        ) or cached
        fallback_current = [
            item for item in fallback if item.get("freshness") == "current"
        ]
        return self._result(
            completed_working,
            status=(
                "ready"
                if fallback_current
                else "stale"
                if fallback
                else "unavailable"
            ),
            completed_at=completed_at,
            cache=cache_state,
            refresh={
                "status": "failed",
                "error_code": error_code,
                "attempted_provider_ids": list(chain),
                "provider_eligibility": [
                    dict(item) for item in provider_eligibility
                ],
                "provider_errors": [dict(item) for item in all_errors],
            },
            evidence=fallback_current or fallback,
            reasons=[
                "provider_refresh_failed_stale_fallback"
                if fallback
                else "provider_refresh_failed_without_evidence"
            ],
            elapsed_ms=(time.monotonic() - started) * 1000,
        )


def format_realtime_query_answer(query: Mapping[str, object]) -> str:
    current = validate_realtime_query(query)
    target = current.get("target") or {}
    name = str(target.get("display_name") or "金融标的")
    symbol = str(target.get("canonical_symbol") or "")
    session = current.get("market_session") or {}
    session_state = str(session.get("market_session_state") or "unknown")
    evidence = list(current.get("evidence") or [])
    if current["status"] == "conflict":
        rows = "；".join(
            f"{item['provider_id']}={float(item['price']):g} {item.get('currency') or ''}"
            f"（snapshot #{item['snapshot_id']}，observed_at={item['observed_at']}，"
            f"fetched_at={item['fetched_at']}，market_status={item.get('market_status') or 'unknown'}，"
            f"来源={item.get('provider_display_name') or item['provider_id']}"
            + (f" {item['source_url']}" if item.get("source_url") else "")
            + "）"
            for item in evidence
        )
        return (
            f"{name}（{symbol}）的当前来源存在实质冲突：{rows}。"
            "本轮不选择单一实时价格，等待后续冲突核验；"
            f"server market_status={session_state}。"
        )
    if not evidence:
        error_code = str((current.get("refresh") or {}).get("error_code") or "no_data")
        return (
            f"{name}（{symbol}）暂时没有可核验的价格快照。"
            f"market_status={session_state}，error_code={error_code}。"
            "本轮不会由通用模型补造行情数字。"
        )
    item = evidence[0]
    stale = current["status"] == "stale"
    prefix = "最近一次（非实时）" if stale else "当前"
    change_text = ""
    if item.get("change") is not None:
        change_text += f"，涨跌 {float(item['change']):g}"
    if item.get("change_percent") is not None:
        change_text += f"（{float(item['change_percent']):g}%）"
    source = str(item.get("provider_display_name") or item.get("provider_id") or "")
    source_url = str(item.get("source_url") or "")
    return (
        f"{name}（{symbol}）{prefix}价格为 {float(item['price']):g} "
        f"{item.get('currency') or ''}{change_text}。"
        f"observed_at={item['observed_at']}，fetched_at={item['fetched_at']}，"
        f"market_status={item.get('market_status') or session_state}，"
        f"server_market_session={session_state}，来源={source}"
        + (f"（{source_url}）" if source_url else "")
        + f"，snapshot #{item['snapshot_id']}。"
        + ("该快照已过实时阈值，不能称为实时行情。" if stale else "")
    )

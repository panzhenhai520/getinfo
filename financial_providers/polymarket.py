#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Read-only Polymarket implied-expectation adapter; never a fact or trade API."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

import requests

from financial_provider_contract import (
    AdjustmentMode,
    FinancialDataKind,
    FinancialDataRecord,
    FinancialDataRequest,
    FinancialProviderResponse,
    FreshnessState,
    InvalidSymbolError,
    MarketStatus,
    PermissionDeniedError,
    RateLimitedError,
    TemporarilyUnavailableError,
    raw_response_hash,
)
from financial_providers.base import RegisteredFinancialProvider, utc_text
from intel_http import ExternalFetchError, SafeHTTPClient, UnsafeExternalURLError


ENDPOINT_KINDS = {
    "market_expectation": FinancialDataKind.MACRO,
    "expectation_history": FinancialDataKind.MACRO,
}
MARKET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
TOKEN_ID_RE = re.compile(r"^[0-9]{1,100}$")
HISTORY_INTERVALS = frozenset({"max", "all", "1m", "1w", "1d", "6h", "1h"})


def _list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _timestamp(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _status(error) -> int:
    try:
        return int(getattr(getattr(error, "response", None), "status_code", 0) or 0)
    except (TypeError, ValueError):
        return 0


class PolymarketProvider(RegisteredFinancialProvider):
    provider_key = "polymarket"

    def __init__(self, *, http_client=None, **kwargs):
        super().__init__(**kwargs)
        self.http = http_client or SafeHTTPClient()

    def _validate_read_url(self, request, url):
        parsed = urlsplit(url)
        gamma_host = urlsplit(self.profile["gamma_base_url"]).hostname
        clob_host = urlsplit(self.profile["clob_base_url"]).hostname
        allowed = (
            parsed.scheme == "https"
            and (
                (parsed.hostname == gamma_host and parsed.path.startswith("/markets"))
                or (parsed.hostname == clob_host and parsed.path == "/prices-history")
            )
        )
        if not allowed:
            raise PermissionDeniedError(
                "Polymarket adapter permits only public read-only market endpoints",
                **self._error_details(request, gate_reason="non_read_only_endpoint_rejected"),
            )

    def _get_json(self, request, url):
        self._validate_read_url(request, url)
        try:
            result = self.http.get(
                url, headers={"User-Agent": "CollectInfo-FinancialProvider/1.0"}
            )
        except Exception as exc:
            status = _status(exc)
            if status == 404:
                raise InvalidSymbolError(
                    "Polymarket market or token was not found",
                    **self._error_details(request, http_status=status),
                ) from exc
            if status == 429:
                raise RateLimitedError(
                    "Polymarket public API rate limit reached",
                    **self._error_details(request, http_status=status),
                    retry_after_seconds=60,
                ) from exc
            if isinstance(
                exc,
                (
                    requests.RequestException,
                    ExternalFetchError,
                    UnsafeExternalURLError,
                    TimeoutError,
                    OSError,
                ),
            ):
                raise TemporarilyUnavailableError(
                    "Polymarket public read-only request failed",
                    **self._error_details(
                        request,
                        failure="https_request_failed",
                        exception_type=type(exc).__name__,
                        http_status=status or None,
                    ),
                ) from exc
            raise
        try:
            return json.loads(result.text)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TemporarilyUnavailableError(
                "Polymarket returned invalid JSON",
                **self._error_details(request, failure="invalid_json"),
            ) from exc

    def _fetch_market(self, request, fetched_at):
        market_id = str(request.parameters.get("market_id") or "").strip()
        if not MARKET_ID_RE.fullmatch(market_id):
            raise InvalidSymbolError(
                "market_id is required and must be a safe public market identifier",
                **self._error_details(request, parameter="market_id"),
            )
        url = f"{self.profile['gamma_base_url'].rstrip('/')}/markets/{market_id}"
        raw = self._get_json(request, url)
        if not isinstance(raw, dict):
            raise TemporarilyUnavailableError(
                "Polymarket market response schema changed",
                **self._error_details(request, failure="schema_drift"),
            )
        outcomes = [str(item) for item in _list(raw.get("outcomes"))]
        prices = [_number(item) for item in _list(raw.get("outcomePrices"))]
        token_ids = [str(item) for item in _list(raw.get("clobTokenIds"))]
        outcome = str(request.parameters.get("outcome") or "Yes").strip()
        try:
            index = next(
                position
                for position, candidate in enumerate(outcomes)
                if candidate.casefold() == outcome.casefold()
            )
        except StopIteration as exc:
            raise InvalidSymbolError(
                "requested outcome is not present in Polymarket market",
                **self._error_details(request, outcome=outcome),
            ) from exc
        if index >= len(prices) or prices[index] is None or not 0 <= prices[index] <= 1:
            raise TemporarilyUnavailableError(
                "Polymarket outcome price is missing or outside probability bounds",
                **self._error_details(request, failure="schema_drift"),
            )
        try:
            observed_at = _timestamp(raw.get("updatedAt") or raw.get("createdAt"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise TemporarilyUnavailableError(
                "Polymarket market response omitted update time",
                **self._error_details(request, failure="schema_drift"),
            ) from exc
        cutoff = min(request.requested_as_of.astimezone(timezone.utc), fetched_at)
        if observed_at > cutoff:
            raise InvalidSymbolError(
                "current Polymarket market state is newer than requested_as_of; use history",
                **self._error_details(request, failure="lookahead_current_market_rejected"),
            )
        normalized = {
            "market_id": str(raw.get("id") or market_id),
            "condition_id": str(raw.get("conditionId") or ""),
            "question": str(raw.get("question") or ""),
            "outcome": outcomes[index],
            "implied_probability": prices[index],
            "token_id": token_ids[index] if index < len(token_ids) else "",
            "active": bool(raw.get("active")),
            "closed": bool(raw.get("closed")),
            "liquidity": _number(raw.get("liquidityNum") or raw.get("liquidity")),
            "volume": _number(raw.get("volumeNum") or raw.get("volume")),
            "interval": "market_snapshot",
            "semantic_role": "expectation_not_fact",
        }
        slug = str(raw.get("slug") or "").strip()
        source_url = f"https://polymarket.com/event/{slug}" if slug else url
        return (
            FinancialDataRecord(
                instrument_id=request.instrument_id,
                metric=request.metric,
                value=prices[index],
                unit="probability",
                currency="",
                market_status=MarketStatus.UNKNOWN,
                observed_at=observed_at,
                fetched_at=fetched_at,
                timezone="UTC",
                freshness_state=FreshnessState.UNKNOWN,
                requested_as_of=request.requested_as_of,
                raw_response_hash=raw_response_hash(raw),
                normalized_payload=normalized,
                adjustment=AdjustmentMode.NOT_APPLICABLE,
                quality_flags=(
                    "expectation_not_fact",
                    "prediction_market_price",
                    "public_read_only_api",
                ),
                source_url=source_url,
                provider_symbol=market_id,
                normalizer_version="polymarket-v1",
                lineage={
                    "api": "gamma",
                    "endpoint": "/markets/{id}",
                    "trading_endpoints_used": False,
                    "requested_as_of_cutoff_applied": True,
                    "freshness_threshold_seconds": 900,
                },
            ),
        )

    def _fetch_history(self, request, fetched_at):
        token_id = str(request.parameters.get("token_id") or "").strip()
        if not TOKEN_ID_RE.fullmatch(token_id):
            raise InvalidSymbolError(
                "token_id is required and must be numeric",
                **self._error_details(request, parameter="token_id"),
            )
        interval = str(request.parameters.get("interval") or "1d").strip()
        if interval not in HISTORY_INTERVALS:
            raise InvalidSymbolError(
                "unsupported Polymarket history interval",
                **self._error_details(request, interval=interval),
            )
        cutoff = min(request.requested_as_of.astimezone(timezone.utc), fetched_at)
        end_ts = min(float(request.parameters.get("end_ts") or cutoff.timestamp()), cutoff.timestamp())
        start_ts = float(request.parameters.get("start_ts") or max(0, end_ts - 86400 * 30))
        if start_ts < 0 or start_ts > end_ts:
            raise InvalidSymbolError(
                "Polymarket history start_ts must be between zero and effective end_ts",
                **self._error_details(request, failure="invalid_time_range"),
            )
        query = urlencode(
            {
                "market": token_id,
                "startTs": int(start_ts),
                "endTs": int(end_ts),
                "interval": interval,
            }
        )
        url = f"{self.profile['clob_base_url'].rstrip('/')}/prices-history?{query}"
        payload = self._get_json(request, url)
        if not isinstance(payload, dict) or not isinstance(payload.get("history"), list):
            raise TemporarilyUnavailableError(
                "Polymarket price history response schema changed",
                **self._error_details(request, failure="schema_drift"),
            )
        records = []
        for raw in payload["history"]:
            if not isinstance(raw, dict):
                continue
            try:
                observed_at = _timestamp(raw.get("t"))
            except (TypeError, ValueError, OverflowError):
                continue
            probability = _number(raw.get("p"))
            if observed_at > cutoff or probability is None or not 0 <= probability <= 1:
                continue
            normalized = {
                "token_id": token_id,
                "implied_probability": probability,
                "interval": interval,
                "semantic_role": "expectation_not_fact",
            }
            records.append(
                FinancialDataRecord(
                    instrument_id=request.instrument_id,
                    metric=request.metric,
                    value=probability,
                    unit="probability",
                    currency="",
                    market_status=MarketStatus.UNKNOWN,
                    observed_at=observed_at,
                    fetched_at=fetched_at,
                    timezone="UTC",
                    freshness_state=FreshnessState.HISTORICAL,
                    requested_as_of=request.requested_as_of,
                    raw_response_hash=raw_response_hash(raw),
                    normalized_payload=normalized,
                    adjustment=AdjustmentMode.NOT_APPLICABLE,
                    quality_flags=(
                        "expectation_not_fact",
                        "prediction_market_price",
                        "lookahead_filtered",
                    ),
                    source_url="https://polymarket.com/",
                    provider_symbol=token_id,
                    normalizer_version="polymarket-v1",
                    lineage={
                        "api": "clob",
                        "endpoint": "/prices-history",
                        "trading_endpoints_used": False,
                        "requested_as_of_cutoff_applied": True,
                        "freshness_threshold_seconds": 900,
                    },
                )
            )
        return tuple(records)

    def fetch(self, request: FinancialDataRequest) -> FinancialProviderResponse:
        instrument, _symbol, fetched_at = self._prepare_request(
            request, ENDPOINT_KINDS
        )
        if instrument.asset_type != "prediction":
            raise InvalidSymbolError(
                "Polymarket adapter requires the registered prediction scope",
                **self._error_details(request, asset_type=instrument.asset_type),
            )
        records = (
            self._fetch_market(request, fetched_at)
            if request.endpoint == "market_expectation"
            else self._fetch_history(request, fetched_at)
        )
        if not records:
            raise InvalidSymbolError(
                "Polymarket returned no expectation values valid at requested_as_of",
                **self._error_details(request, failure="no_valid_history"),
            )
        return FinancialProviderResponse(
            provider_id=self.provider_id,
            endpoint=request.endpoint,
            license_profile=self.license_profile,
            request_id=request.request_id,
            data_kind=request.data_kind,
            records=records,
        )

    def health_probe(self, *, request_id: str, requested_at: datetime):
        instrument = self.instruments.get_by_canonical_symbol("MARKET-EXPECTATION.POLY")
        request = FinancialDataRequest(
            request_id=request_id,
            endpoint="market_expectation",
            instrument_id=str(instrument.instrument_id),
            metric="implied_probability",
            data_kind=FinancialDataKind.MACRO,
            requested_as_of=requested_at,
            preferred_provider_id=self.provider_id,
            parameters={"market_id": "health-list-only"},
        )
        status, error_type, count = "unhealthy", None, 0
        try:
            _instrument, _symbol, _fetched_at = self._prepare_request(
                request, ENDPOINT_KINDS
            )
            query = urlencode({"limit": 1, "active": "true", "closed": "false"})
            url = f"{self.profile['gamma_base_url'].rstrip('/')}/markets?{query}"
            payload = self._get_json(request, url)
            if isinstance(payload, list) and payload:
                status, count = "healthy_public_read_only", 1
            else:
                status = "degraded_no_active_market"
        except PermissionDeniedError as exc:
            status, error_type = "permission_denied", exc.code.value
        except RateLimitedError as exc:
            status, error_type = "rate_limited", exc.code.value
        except Exception as exc:
            error_type = type(exc).__name__
        self.update_health(status, requested_at)
        return {
            "provider_id": self.provider_id,
            "status": status,
            "checked_at": utc_text(requested_at),
            "record_count": count,
            "error_type": error_type,
            "read_only": True,
        }

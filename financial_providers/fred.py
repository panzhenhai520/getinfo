#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""FRED/ALFRED macro adapter with mandatory real-time-period locking."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

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
from financial_providers.base import RegisteredFinancialProvider, setting_value, utc_text
from intel_http import ExternalFetchError, SafeHTTPClient, UnsafeExternalURLError


ENDPOINT_KINDS = {
    "observations": FinancialDataKind.MACRO,
    "vintages": FinancialDataKind.MACRO,
}
FRED_SERVICE_TIMEZONE = ZoneInfo("America/Chicago")


def _number(value):
    if str(value or "").strip() in {"", "."}:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _http_status(error) -> int:
    try:
        return int(getattr(getattr(error, "response", None), "status_code", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _day(value, label: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO date") from exc


class FREDProvider(RegisteredFinancialProvider):
    provider_key = "fred"

    def __init__(self, *, http_client=None, **kwargs):
        super().__init__(**kwargs)
        self.http = http_client or SafeHTTPClient()

    def _get_json(self, request, path, parameters):
        api_key = str(setting_value(self.settings, "FRED_API_KEY", "") or "").strip()
        if not api_key:
            raise PermissionDeniedError(
                "FRED API key is not configured",
                **self._error_details(request, gate_reason="fred_api_key_missing"),
            )
        query = urlencode({**parameters, "api_key": api_key, "file_type": "json"})
        url = f"{self.profile['api_base_url'].rstrip('/')}/{path}?{query}"
        try:
            result = self.http.get(
                url, headers={"User-Agent": "CollectInfo-FinancialProvider/1.0"}
            )
        except Exception as exc:
            status = _http_status(exc)
            if status in {400, 401, 403}:
                raise PermissionDeniedError(
                    "FRED authentication or request authorization was rejected",
                    **self._error_details(request, http_status=status),
                ) from exc
            if status == 429:
                raise RateLimitedError(
                    "FRED API rate limit reached",
                    **self._error_details(request, http_status=status),
                    retry_after_seconds=3600,
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
                    "FRED API request failed",
                    **self._error_details(
                        request,
                        failure="https_request_failed",
                        exception_type=type(exc).__name__,
                        http_status=status or None,
                    ),
                ) from exc
            raise
        try:
            payload = json.loads(result.text)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TemporarilyUnavailableError(
                "FRED API returned invalid JSON",
                **self._error_details(request, failure="invalid_json"),
            ) from exc
        if not isinstance(payload, dict):
            raise TemporarilyUnavailableError(
                "FRED API response schema changed",
                **self._error_details(request, failure="schema_drift"),
            )
        if "error_code" in payload or "error_message" in payload:
            code = int(payload.get("error_code") or 0)
            if code == 429:
                raise RateLimitedError(
                    "FRED API rate limit reached",
                    **self._error_details(request, provider_error_code=code),
                    retry_after_seconds=3600,
                )
            raise PermissionDeniedError(
                "FRED authentication or request authorization was rejected",
                **self._error_details(request, provider_error_code=code or None),
            )
        return payload

    def _vintage_day(self, request, fetched_at):
        # FRED validates real-time periods against its St. Louis business date.
        # During the UTC-midnight boundary, the UTC calendar can already be one
        # day ahead and FRED rejects that otherwise valid request as a future
        # realtime_start.
        server_cap = fetched_at.astimezone(FRED_SERVICE_TIMEZONE).date()
        requested_cap = request.requested_as_of.astimezone(timezone.utc).date()
        cap = min(server_cap, requested_cap)
        explicit = request.parameters.get("vintage_date")
        if not explicit:
            return cap
        try:
            vintage = _day(explicit, "vintage_date")
        except ValueError as exc:
            raise InvalidSymbolError(
                str(exc), **self._error_details(request, parameter="vintage_date")
            ) from exc
        if vintage > cap:
            raise InvalidSymbolError(
                "vintage_date cannot be later than requested_as_of or server time",
                **self._error_details(request, failure="lookahead_vintage_rejected"),
            )
        return vintage

    def _fetch_observations(self, request, instrument, series_id, fetched_at):
        vintage = self._vintage_day(request, fetched_at)
        observation_start = request.parameters.get("observation_start")
        observation_end = request.parameters.get("observation_end")
        if observation_start:
            try:
                observation_start = _day(observation_start, "observation_start")
            except ValueError as exc:
                raise InvalidSymbolError(
                    str(exc), **self._error_details(request, parameter="observation_start")
                ) from exc
        else:
            observation_start = date(1776, 7, 4)
        if observation_end:
            try:
                observation_end = _day(observation_end, "observation_end")
            except ValueError as exc:
                raise InvalidSymbolError(
                    str(exc), **self._error_details(request, parameter="observation_end")
                ) from exc
        else:
            observation_end = vintage
        observation_end = min(observation_end, vintage)
        if observation_start > observation_end:
            raise InvalidSymbolError(
                "observation_start cannot be later than the effective observation_end",
                **self._error_details(request, failure="invalid_date_range"),
            )
        limit = max(1, min(int(request.parameters.get("limit") or 10000), 10000))
        payload = self._get_json(
            request,
            "series/observations",
            {
                "series_id": series_id,
                "realtime_start": vintage.isoformat(),
                "realtime_end": vintage.isoformat(),
                "observation_start": observation_start.isoformat(),
                "observation_end": observation_end.isoformat(),
                "sort_order": "asc",
                "limit": limit,
            },
        )
        values = payload.get("observations")
        if not isinstance(values, list):
            raise TemporarilyUnavailableError(
                "FRED observations response schema changed",
                **self._error_details(request, failure="schema_drift"),
            )
        records = []
        for raw in values:
            if not isinstance(raw, dict):
                continue
            try:
                observation_day = _day(raw.get("date"), "observation date")
                realtime_start = _day(raw.get("realtime_start"), "realtime_start")
                realtime_end = _day(raw.get("realtime_end"), "realtime_end")
            except ValueError:
                continue
            value = _number(raw.get("value"))
            if (
                value is None
                or observation_day > vintage
                or realtime_start > vintage
            ):
                continue
            observed_at = datetime.combine(
                observation_day, time.min, tzinfo=timezone.utc
            )
            effective_from = datetime.combine(
                realtime_start, time.min, tzinfo=timezone.utc
            )
            effective_to = datetime.combine(
                realtime_end, time.max, tzinfo=timezone.utc
            )
            normalized = {
                "series_id": series_id,
                "observation_date": observation_day.isoformat(),
                "value": value,
                "realtime_start": realtime_start.isoformat(),
                "realtime_end": realtime_end.isoformat(),
                "requested_vintage_date": vintage.isoformat(),
                "interval": "macro_observation",
            }
            unit_hint = str(instrument.metadata.get("unit_hint") or "value")
            records.append(
                FinancialDataRecord(
                    instrument_id=request.instrument_id,
                    metric=request.metric,
                    value=value,
                    unit=unit_hint,
                    currency=instrument.currency,
                    market_status=MarketStatus.UNKNOWN,
                    observed_at=min(observed_at, fetched_at),
                    fetched_at=fetched_at,
                    timezone="UTC",
                    freshness_state=(
                        FreshnessState.HISTORICAL
                        if vintage < fetched_at.date()
                        else FreshnessState.UNKNOWN
                    ),
                    requested_as_of=request.requested_as_of,
                    effective_from=effective_from,
                    effective_to=effective_to,
                    raw_response_hash=raw_response_hash(raw),
                    normalized_payload=normalized,
                    adjustment=AdjustmentMode.NOT_APPLICABLE,
                    quality_flags=(
                        "alfred_vintage_locked",
                        "macro_release_not_market_quote",
                    ),
                    source_url=f"https://fred.stlouisfed.org/series/{series_id}",
                    provider_symbol=series_id,
                    normalizer_version="fred-alfred-v1",
                    lineage={
                        "api_endpoint": "series/observations",
                        "requested_as_of_cutoff_applied": True,
                        "vintage_date": vintage.isoformat(),
                        "freshness_threshold_seconds": 86400,
                    },
                )
            )
        return tuple(records)

    def _fetch_vintages(self, request, instrument, series_id, fetched_at):
        cap = self._vintage_day(request, fetched_at)
        payload = self._get_json(
            request,
            "series/vintagedates",
            {
                "series_id": series_id,
                "realtime_start": "1776-07-04",
                "realtime_end": cap.isoformat(),
                "sort_order": "asc",
                "limit": max(1, min(int(request.parameters.get("limit") or 10000), 10000)),
            },
        )
        values = payload.get("vintage_dates")
        if not isinstance(values, list):
            raise TemporarilyUnavailableError(
                "FRED vintage response schema changed",
                **self._error_details(request, failure="schema_drift"),
            )
        records = []
        for raw in values:
            try:
                vintage = _day(raw, "vintage date")
            except ValueError:
                continue
            if vintage > cap:
                continue
            observed_at = datetime.combine(vintage, time.min, tzinfo=timezone.utc)
            normalized = {
                "series_id": series_id,
                "vintage_date": vintage.isoformat(),
                "interval": "macro_vintage",
            }
            records.append(
                FinancialDataRecord(
                    instrument_id=request.instrument_id,
                    metric=request.metric,
                    value=vintage.isoformat(),
                    unit="date",
                    currency="",
                    market_status=MarketStatus.UNKNOWN,
                    observed_at=min(observed_at, fetched_at),
                    fetched_at=fetched_at,
                    timezone="UTC",
                    freshness_state=FreshnessState.HISTORICAL,
                    requested_as_of=request.requested_as_of,
                    effective_from=observed_at,
                    raw_response_hash=raw_response_hash({"vintage_date": raw}),
                    normalized_payload=normalized,
                    adjustment=AdjustmentMode.NOT_APPLICABLE,
                    quality_flags=("revision_calendar", "lookahead_filtered"),
                    source_url=f"https://fred.stlouisfed.org/series/{series_id}",
                    provider_symbol=series_id,
                    normalizer_version="fred-alfred-v1",
                    lineage={
                        "api_endpoint": "series/vintagedates",
                        "requested_as_of_cutoff_applied": True,
                        "freshness_threshold_seconds": 86400,
                    },
                )
            )
        return tuple(records)

    def fetch(self, request: FinancialDataRequest) -> FinancialProviderResponse:
        instrument, series_id, fetched_at = self._prepare_request(
            request, ENDPOINT_KINDS
        )
        if instrument.asset_type != "macro":
            raise InvalidSymbolError(
                "FRED adapter requires a registered macro series",
                **self._error_details(request, asset_type=instrument.asset_type),
            )
        if request.endpoint == "observations":
            records = self._fetch_observations(
                request, instrument, series_id, fetched_at
            )
        else:
            records = self._fetch_vintages(request, instrument, series_id, fetched_at)
        if not records:
            raise InvalidSymbolError(
                "FRED returned no values valid at requested_as_of",
                **self._error_details(request, provider_symbol=series_id),
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
        instrument = self.instruments.get_by_canonical_symbol("DFF.FRED")
        request = FinancialDataRequest(
            request_id=request_id,
            endpoint="observations",
            instrument_id=str(instrument.instrument_id),
            metric="value",
            data_kind=FinancialDataKind.MACRO,
            requested_as_of=requested_at,
            preferred_provider_id=self.provider_id,
            parameters={"limit": 1},
        )
        try:
            response = self.fetch_validated(request)
            status, error_type = "healthy_vintage_locked", None
        except PermissionDeniedError as exc:
            response, status, error_type = None, "permission_denied", exc.code.value
        except RateLimitedError as exc:
            response, status, error_type = None, "budget_or_rate_limited", exc.code.value
        except Exception as exc:
            response, status, error_type = None, "unhealthy", type(exc).__name__
        self.update_health(status, requested_at)
        return {
            "provider_id": self.provider_id,
            "status": status,
            "checked_at": utc_text(requested_at),
            "record_count": len(response.records) if response else 0,
            "error_type": error_type,
        }

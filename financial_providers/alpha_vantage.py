#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Entitlement-aware Alpha Vantage global quote and raw daily-bar adapter."""

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
from financial_providers.base import (
    RegisteredFinancialProvider,
    bool_setting,
    setting_value,
    utc_text,
)
from intel_http import ExternalFetchError, SafeHTTPClient, UnsafeExternalURLError


ENDPOINT_KINDS = {
    "quote": FinancialDataKind.QUOTE,
    "bars": FinancialDataKind.BAR,
}
TIMEZONES = {
    "CN": "Asia/Shanghai",
    "XHKG": "Asia/Hong_Kong",
    "US": "America/New_York",
}
QUOTE_FIELDS = {
    "last_price": "05. price",
    "close": "05. price",
    "open": "02. open",
    "high": "03. high",
    "low": "04. low",
    "volume": "06. volume",
}
DAILY_FIELDS = {
    "close": "4. close",
    "last_price": "4. close",
    "open": "1. open",
    "high": "2. high",
    "low": "3. low",
    "volume": "5. volume",
}


def _number(value):
    try:
        result = float(str(value).replace("%", "").replace(",", ""))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _response_status(error) -> int:
    response = getattr(error, "response", None)
    try:
        return int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        return 0


class AlphaVantageProvider(RegisteredFinancialProvider):
    provider_key = "alpha_vantage"

    def __init__(self, *, http_client=None, **kwargs):
        super().__init__(**kwargs)
        self.http = http_client or SafeHTTPClient()

    def _get_json(self, request, parameters):
        api_key = str(setting_value(self.settings, "ALPHA_VANTAGE_API_KEY", "") or "").strip()
        if not api_key:
            raise PermissionDeniedError(
                "Alpha Vantage API key is not configured",
                **self._error_details(request, gate_reason="alpha_vantage_api_key_missing"),
            )
        query = urlencode({**parameters, "apikey": api_key, "datatype": "json"})
        url = f"{self.profile['api_base_url']}?{query}"
        try:
            result = self.http.get(
                url, headers={"User-Agent": "CollectInfo-FinancialProvider/1.0"}
            )
        except Exception as exc:
            status = _response_status(exc)
            if status in {401, 403}:
                raise PermissionDeniedError(
                    "Alpha Vantage authentication or entitlement was rejected",
                    **self._error_details(request, http_status=status),
                ) from exc
            if status == 429:
                raise RateLimitedError(
                    "Alpha Vantage rate limit reached",
                    **self._error_details(request, http_status=status),
                    retry_after_seconds=86400,
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
                    "Alpha Vantage request failed",
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
                "Alpha Vantage returned invalid JSON",
                **self._error_details(request, failure="invalid_json"),
            ) from exc
        if not isinstance(payload, dict):
            raise TemporarilyUnavailableError(
                "Alpha Vantage response schema changed",
                **self._error_details(request, failure="schema_drift"),
            )
        self._raise_payload_error(request, payload)
        return payload

    def _raise_payload_error(self, request, payload):
        if "Note" in payload:
            raise RateLimitedError(
                "Alpha Vantage rate limit reached",
                **self._error_details(request, provider_status="rate_limited"),
                retry_after_seconds=86400,
            )
        information = str(payload.get("Information") or "").casefold()
        error = str(payload.get("Error Message") or "").casefold()
        combined = f"{information} {error}"
        if not combined.strip():
            return
        if any(word in combined for word in ("api key", "apikey", "premium", "entitlement")):
            raise PermissionDeniedError(
                "Alpha Vantage authentication or entitlement was rejected",
                **self._error_details(request, provider_status="permission_denied"),
            )
        if any(word in combined for word in ("frequency", "rate limit", "call volume")):
            raise RateLimitedError(
                "Alpha Vantage rate limit reached",
                **self._error_details(request, provider_status="rate_limited"),
                retry_after_seconds=86400,
            )
        raise InvalidSymbolError(
            "Alpha Vantage rejected the symbol or request parameters",
            **self._error_details(request, provider_status="invalid_request"),
        )

    def _entitlement(self, request):
        requested = str(request.parameters.get("entitlement") or "").strip().casefold()
        if requested not in {"", "realtime", "delayed"}:
            raise InvalidSymbolError(
                "unsupported Alpha Vantage entitlement value",
                **self._error_details(request, entitlement=requested),
            )
        configured = str(
            setting_value(
                self.settings, "ALPHA_VANTAGE_QUOTE_ENTITLEMENT", "none"
            )
            or "none"
        ).strip().casefold()
        if configured not in {"none", "realtime", "delayed"}:
            raise PermissionDeniedError(
                "Alpha Vantage quote entitlement configuration is invalid",
                **self._error_details(
                    request,
                    gate_reason="alpha_vantage_quote_entitlement_invalid",
                ),
            )
        # Configured quote freshness never leaks into TIME_SERIES_DAILY. An
        # explicitly requested bars entitlement remains visible to the scope
        # guard in fetch(), preserving the existing fail-closed behaviour.
        entitlement = requested
        if request.endpoint == "quote" and not entitlement and configured != "none":
            entitlement = configured
        if entitlement and not bool_setting(
            self.settings, "ALPHA_VANTAGE_REALTIME_ENTITLED", False
        ):
            raise PermissionDeniedError(
                "Alpha Vantage realtime/delayed entitlement is not configured",
                **self._error_details(
                    request, gate_reason="alpha_vantage_realtime_entitlement_missing"
                ),
            )
        return entitlement

    @staticmethod
    def _observed_at(day_text: str, timezone_name: str) -> datetime:
        day = date.fromisoformat(str(day_text))
        local_close = datetime.combine(day, time(16, 0), tzinfo=ZoneInfo(timezone_name))
        return local_close.astimezone(timezone.utc)

    def fetch(self, request: FinancialDataRequest) -> FinancialProviderResponse:
        instrument, symbol, fetched_at = self._prepare_request(request, ENDPOINT_KINDS)
        if instrument.asset_type not in {"equity", "etf", "index"}:
            raise InvalidSymbolError(
                "Alpha Vantage market adapter does not support this asset type",
                **self._error_details(request, asset_type=instrument.asset_type),
            )
        timezone_name = TIMEZONES.get(instrument.market, "UTC")
        entitlement = self._entitlement(request)
        if request.endpoint == "quote":
            parameters = {"function": "GLOBAL_QUOTE", "symbol": symbol}
            if entitlement:
                parameters["entitlement"] = entitlement
            payload = self._get_json(request, parameters)
            quote_payload = payload.get("Global Quote")
            if not isinstance(quote_payload, dict) or not quote_payload:
                raise InvalidSymbolError(
                    "Alpha Vantage returned no quote",
                    **self._error_details(request, provider_symbol=symbol),
                )
            field = QUOTE_FIELDS.get(request.metric)
            if field is None or _number(quote_payload.get(field)) is None:
                raise InvalidSymbolError(
                    "Alpha Vantage quote did not contain the requested metric",
                    **self._error_details(request, metric=request.metric),
                )
            day_text = quote_payload.get("07. latest trading day")
            try:
                observed_at = self._observed_at(day_text, timezone_name)
            except (TypeError, ValueError):
                raise TemporarilyUnavailableError(
                    "Alpha Vantage quote omitted latest trading day",
                    **self._error_details(request, failure="schema_drift"),
                )
            requested_day = request.requested_as_of.astimezone(
                ZoneInfo(timezone_name)
            ).date()
            provider_day = date.fromisoformat(str(day_text))
            if provider_day > requested_day:
                raise TemporarilyUnavailableError(
                    "Alpha Vantage quote is newer than requested_as_of",
                    **self._error_details(
                        request, failure="lookahead_data_rejected"
                    ),
                )
            observed_at = min(observed_at, fetched_at)
            normalized = {
                "open": _number(quote_payload.get("02. open")),
                "high": _number(quote_payload.get("03. high")),
                "low": _number(quote_payload.get("04. low")),
                "price": _number(quote_payload.get("05. price")),
                "volume": _number(quote_payload.get("06. volume")),
                "previous_close": _number(quote_payload.get("08. previous close")),
                "change": _number(quote_payload.get("09. change")),
                "change_percent": _number(quote_payload.get("10. change percent")),
                "interval": "snapshot",
            }
            rows = (
                (observed_at, normalized, quote_payload, _number(quote_payload.get(field))),
            )
        else:
            if entitlement:
                raise PermissionDeniedError(
                    "TIME_SERIES_DAILY does not use the quote entitlement switch",
                    **self._error_details(request, gate_reason="invalid_entitlement_scope"),
                )
            payload = self._get_json(
                request,
                {"function": "TIME_SERIES_DAILY", "symbol": symbol, "outputsize": "compact"},
            )
            series = payload.get("Time Series (Daily)")
            if not isinstance(series, dict):
                raise InvalidSymbolError(
                    "Alpha Vantage returned no daily series",
                    **self._error_details(request, provider_symbol=symbol),
                )
            field = DAILY_FIELDS.get(request.metric)
            if field is None:
                raise InvalidSymbolError(
                    "unsupported Alpha Vantage daily metric",
                    **self._error_details(request, metric=request.metric),
                )
            cutoff = min(request.requested_as_of.astimezone(timezone.utc), fetched_at)
            rows = []
            for day_text, raw in sorted(series.items()):
                try:
                    observed_at = self._observed_at(day_text, timezone_name)
                except (TypeError, ValueError):
                    continue
                if observed_at > cutoff or not isinstance(raw, dict):
                    continue
                value = _number(raw.get(field))
                if value is None:
                    continue
                normalized = {
                    "open": _number(raw.get("1. open")),
                    "high": _number(raw.get("2. high")),
                    "low": _number(raw.get("3. low")),
                    "close": _number(raw.get("4. close")),
                    "volume": _number(raw.get("5. volume")),
                    "interval": "1d",
                }
                rows.append((observed_at, normalized, raw, value))
            rows = tuple(rows)
        if not rows:
            raise InvalidSymbolError(
                "Alpha Vantage returned no observations at or before requested_as_of",
                **self._error_details(request, provider_symbol=symbol),
            )

        records = []
        for observed_at, normalized, raw, value in rows:
            flags = ["observed_time_date_only"]
            if entitlement:
                flags.append(f"{entitlement}_entitlement_requested")
                freshness = FreshnessState.DELAYED if entitlement == "delayed" else FreshnessState.UNKNOWN
            elif request.endpoint == "quote":
                flags.append("free_tier_end_of_day_default")
                freshness = FreshnessState.STALE
            else:
                freshness = FreshnessState.HISTORICAL
            records.append(
                FinancialDataRecord(
                    instrument_id=request.instrument_id,
                    metric=request.metric,
                    value=value,
                    unit="shares" if request.metric == "volume" else "price",
                    currency=instrument.currency,
                    market_status=MarketStatus.UNKNOWN,
                    observed_at=observed_at,
                    fetched_at=fetched_at,
                    timezone=timezone_name,
                    freshness_state=freshness,
                    requested_as_of=request.requested_as_of,
                    raw_response_hash=raw_response_hash(raw),
                    normalized_payload=normalized,
                    adjustment=AdjustmentMode.RAW,
                    quality_flags=tuple(flags),
                    source_url=self.profile["documentation_url"],
                    provider_symbol=symbol,
                    normalizer_version="alpha-vantage-v1",
                    lineage={
                        "api_function": "GLOBAL_QUOTE" if request.endpoint == "quote" else "TIME_SERIES_DAILY",
                        "entitlement": entitlement or "default_end_of_day",
                        "requested_as_of_cutoff_applied": True,
                        "freshness_threshold_seconds": 900,
                    },
                )
            )
        return FinancialProviderResponse(
            provider_id=self.provider_id,
            endpoint=request.endpoint,
            license_profile=self.license_profile,
            request_id=request.request_id,
            data_kind=request.data_kind,
            records=tuple(records),
        )

    def health_probe(self, *, request_id: str, requested_at: datetime):
        instrument = self.instruments.get_by_canonical_symbol("AAPL.US")
        request = FinancialDataRequest(
            request_id=request_id,
            endpoint="quote",
            instrument_id=str(instrument.instrument_id),
            metric="last_price",
            data_kind=FinancialDataKind.QUOTE,
            requested_as_of=requested_at,
            preferred_provider_id=self.provider_id,
        )
        try:
            response = self.fetch_validated(request)
            status, error_type = "healthy_end_of_day_default", None
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

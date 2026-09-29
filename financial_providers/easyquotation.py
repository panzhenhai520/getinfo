#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Explicit, disabled-by-default easyquotation quote fallback."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from financial_market_clock import MarketClockService, RequestTimeContext
from financial_provider_contract import (
    AdjustmentMode,
    FinancialDataKind,
    FinancialDataRecord,
    FinancialDataRequest,
    FinancialProviderResponse,
    FreshnessState,
    InvalidSymbolError,
    MarketStatus,
    TemporarilyUnavailableError,
    raw_response_hash,
)
from financial_providers.base import RegisteredFinancialProvider, utc_text


ENDPOINT_KINDS = {"quote": FinancialDataKind.QUOTE}
MARKET_TIMEZONES = {"CN": "Asia/Shanghai", "XHKG": "Asia/Hong_Kong"}
MARKET_CALENDARS = {"CN": "XSHG", "XHKG": "XHKG"}


def _number(value):
    try:
        result = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _json_safe(value):
    """移除上游字典中的 NaN/Inf，避免原始证据绕过 JSON 合同。"""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except (TypeError, ValueError):
            pass
    return str(value)


class EasyQuotationProvider(RegisteredFinancialProvider):
    provider_key = "easyquotation"

    def __init__(self, *, sdk=None, market_clock=None, **kwargs):
        super().__init__(**kwargs)
        self._sdk = sdk
        self.market_clock = market_clock or MarketClockService()

    def _load_sdk(self, request):
        if self._sdk is not None:
            return self._sdk
        try:
            import easyquotation as sdk
        except ImportError as exc:
            raise TemporarilyUnavailableError(
                "easyquotation dependency is not installed",
                **self._error_details(
                    request,
                    dependency="easyquotation",
                    required_version=self.profile["package_version"],
                ),
            ) from exc
        self._sdk = sdk
        return sdk

    @staticmethod
    def _observed_at(payload, fetched_at, timezone_name):
        raw = str(payload.get("time") or "").strip()
        raw_date = str(payload.get("date") or "").strip()
        candidates = []
        if raw_date and raw:
            candidates.append(f"{raw_date} {raw}")
        candidates.append(raw)
        for value in candidates:
            for pattern in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    parsed = datetime.strptime(value, pattern).replace(
                        tzinfo=ZoneInfo(timezone_name)
                    )
                    return min(parsed.astimezone(timezone.utc), fetched_at), False
                except ValueError:
                    continue
        return fetched_at, True

    def fetch(self, request: FinancialDataRequest) -> FinancialProviderResponse:
        instrument, symbol, fetched_at = self._prepare_request(request, ENDPOINT_KINDS)
        if request.metric not in {"last_price", "close"}:
            raise InvalidSymbolError(
                "easyquotation fallback supports only last_price/close",
                **self._error_details(request, metric=request.metric),
            )
        if instrument.market not in MARKET_TIMEZONES or instrument.asset_type not in {
            "equity", "index", "etf"
        }:
            raise InvalidSymbolError(
                "easyquotation fallback supports only configured CN/HK listed instruments",
                **self._error_details(
                    request, market=instrument.market, asset_type=instrument.asset_type
                ),
            )
        sdk = self._load_sdk(request)
        source = "hkquote" if instrument.market == "XHKG" else "tencent"
        try:
            client = sdk.use(source)
            if instrument.market == "XHKG":
                payloads = client.real([symbol])
            else:
                payloads = client.real([symbol], prefix=True)
        except Exception as exc:
            raise TemporarilyUnavailableError(
                "easyquotation fallback request failed",
                **self._error_details(
                    request,
                    failure="sdk_request_failed",
                    exception_type=type(exc).__name__,
                    upstream=source,
                ),
            ) from exc
        payload = (payloads or {}).get(symbol)
        if payload is None and isinstance(payloads, dict) and len(payloads) == 1:
            payload = next(iter(payloads.values()))
        if not isinstance(payload, dict):
            raise InvalidSymbolError(
                "easyquotation returned no quote for symbol",
                **self._error_details(request, provider_symbol=symbol),
            )
        price = _number(
            payload.get("price") if instrument.market == "XHKG" else payload.get("now")
        )
        if price is None:
            raise InvalidSymbolError(
                "easyquotation quote did not contain a finite price",
                **self._error_details(request, provider_symbol=symbol),
            )
        timezone_name = MARKET_TIMEZONES[instrument.market]
        observation_cutoff = min(
            fetched_at,
            request.requested_as_of.astimezone(timezone.utc),
        )
        observed_at, observed_unknown = self._observed_at(
            payload, observation_cutoff, timezone_name
        )
        context = RequestTimeContext(
            server_now_utc=fetched_at,
            server_timezone="Asia/Hong_Kong",
            user_timezone="Asia/Hong_Kong",
        )
        market_status = self.market_clock.market_state(
            MARKET_CALENDARS[instrument.market], context
        ).market_session_state
        quality_flags = [
            "unofficial_lightweight_fallback",
            "upstream_terms_apply",
            "non_exchange_grade",
        ]
        if observed_unknown:
            quality_flags.append("observed_time_unknown")
        normalized = {
            "price": price,
            "open": _number(payload.get("openPrice") or payload.get("open")),
            "previous_close": _number(payload.get("lastPrice") or payload.get("close")),
            "high": _number(payload.get("high")),
            "low": _number(payload.get("low")),
            "volume": _number(payload.get("amount") or payload.get("turnover")),
            "interval": "snapshot",
            "upstream": source,
        }
        record = FinancialDataRecord(
            instrument_id=request.instrument_id,
            metric=request.metric,
            value=price,
            unit="price",
            currency=instrument.currency,
            market_status=market_status,
            observed_at=observed_at,
            fetched_at=fetched_at,
            timezone=timezone_name,
            freshness_state=FreshnessState.UNKNOWN,
            requested_as_of=request.requested_as_of,
            raw_response_hash=raw_response_hash(_json_safe(payload)),
            normalized_payload=normalized,
            adjustment=AdjustmentMode.RAW,
            quality_flags=tuple(quality_flags),
            provider_symbol=symbol,
            normalizer_version="easyquotation-v1",
            lineage={
                "upstream": source,
                "fallback_only": True,
                "freshness_threshold_seconds": 300,
            },
        )
        return FinancialProviderResponse(
            provider_id=self.provider_id,
            endpoint=request.endpoint,
            license_profile=self.license_profile,
            request_id=request.request_id,
            data_kind=request.data_kind,
            records=(record,),
        )

    def health_probe(self, *, request_id: str, requested_at: datetime):
        instrument = self.instruments.get_by_canonical_symbol("0700.HK")
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
            status, error_type = "healthy_fallback_only", None
        except Exception as exc:
            response, status, error_type = None, "unhealthy", type(exc).__name__
        self.update_health(status, requested_at)
        return {
            "provider_id": self.provider_id,
            "status": status,
            "checked_at": utc_text(requested_at),
            "record_count": len(response.records) if response else 0,
            "error_type": error_type,
            "fallback_only": True,
        }

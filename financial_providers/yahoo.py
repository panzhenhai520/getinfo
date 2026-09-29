#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""yfinance adapter for HK/global research-grade quote and raw bar fallback."""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from typing import Mapping, Sequence, Tuple
from urllib.parse import quote
from zoneinfo import ZoneInfo

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
from financial_providers.base import RegisteredFinancialProvider, setting_value, utc_text


ENDPOINT_KINDS = {
    "quote": FinancialDataKind.QUOTE,
    "bars": FinancialDataKind.BAR,
}
MARKET_TIMEZONES = {
    "CN": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
    "XHKG": "Asia/Hong_Kong",
    "US": "America/New_York",
    "JP": "Asia/Tokyo",
}
PRICE_FIELDS = {
    "last_price": "Close",
    "close": "Close",
    "open": "Open",
    "high": "High",
    "low": "Low",
    "volume": "Volume",
}


def _timestamp(value, timezone_name: str) -> datetime:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, time.min)
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=ZoneInfo(timezone_name))
    return value.astimezone(timezone.utc)


def _row_value(row, field: str, symbol: str):
    candidates = (field, field.casefold(), (field, symbol), (symbol, field))
    for key in candidates:
        try:
            value = row.get(key)
        except AttributeError:
            try:
                value = row[key]
            except (KeyError, TypeError):
                continue
        if value is not None:
            return value
    index = getattr(row, "index", ())
    for key in index:
        if isinstance(key, tuple) and field in key:
            return row[key]
    return None


def _finite(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


class YahooFinanceProvider(RegisteredFinancialProvider):
    provider_key = "yahoo"

    def __init__(self, *, sdk=None, **kwargs):
        super().__init__(**kwargs)
        self._sdk = sdk

    @property
    def sdk_version(self) -> str:
        return str(getattr(self._sdk, "__version__", "not_loaded"))

    def _load_sdk(self, request: FinancialDataRequest):
        if self._sdk is not None:
            return self._sdk
        try:
            import yfinance as sdk
        except ImportError as exc:
            raise TemporarilyUnavailableError(
                "yfinance dependency is not installed",
                **self._error_details(
                    request,
                    dependency="yfinance",
                    required_version=self.profile["package_version"],
                ),
            ) from exc
        self._sdk = sdk
        return sdk

    @staticmethod
    def _rows(frame, symbol: str, timezone_name: str) -> Sequence[Tuple[datetime, Mapping]]:
        if frame is None or bool(getattr(frame, "empty", False)):
            return ()
        if isinstance(frame, Mapping) and "rows" in frame:
            values = frame["rows"]
        elif hasattr(frame, "iterrows"):
            values = tuple(frame.iterrows())
        else:
            values = frame
        rows = []
        for item in values or ():
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                continue
            observed_at = _timestamp(item[0], timezone_name)
            rows.append((observed_at, item[1]))
        return tuple(sorted(rows, key=lambda item: item[0]))

    def _download(self, request, symbol, *, endpoint, timezone_name, fetched_at):
        sdk = self._load_sdk(request)
        timeout = int(setting_value(self.settings, "FINANCIAL_PROVIDER_TIMEOUT_SECONDS", 20))
        parameters = dict(request.parameters)
        if endpoint == "quote":
            interval = str(parameters.get("interval") or "1m")
            kwargs = {"period": str(parameters.get("period") or "1d")}
        else:
            interval = str(parameters.get("interval") or "1d")
            start = parameters.get("start")
            end = parameters.get("end")
            requested_local = request.requested_as_of.astimezone(ZoneInfo(timezone_name))
            exclusive_cap = (requested_local.date() + timedelta(days=1)).isoformat()
            inclusive_end = None
            if end:
                try:
                    inclusive_end = (
                        date.fromisoformat(str(end)) + timedelta(days=1)
                    ).isoformat()
                except ValueError:
                    inclusive_end = str(end)
            kwargs = {
                "start": str(start) if start else None,
                # The provider contract treats ``end`` as an inclusive local
                # trading date, while yfinance treats it as exclusive.
                "end": min(inclusive_end, exclusive_cap) if inclusive_end else exclusive_cap,
            }
        try:
            frame = sdk.download(
                symbol,
                interval=interval,
                progress=False,
                auto_adjust=False,
                actions=False,
                threads=False,
                timeout=timeout,
                multi_level_index=False,
                **kwargs,
            )
        except Exception as exc:
            raise TemporarilyUnavailableError(
                "Yahoo Finance research data request failed",
                **self._error_details(
                    request,
                    failure="sdk_request_failed",
                    exception_type=type(exc).__name__,
                    sdk_version=self.sdk_version,
                ),
            ) from exc
        rows = self._rows(frame, symbol, timezone_name)
        cutoff = min(request.requested_as_of.astimezone(timezone.utc), fetched_at)
        return tuple(item for item in rows if item[0] <= cutoff), interval

    def fetch(self, request: FinancialDataRequest) -> FinancialProviderResponse:
        instrument, symbol, fetched_at = self._prepare_request(request, ENDPOINT_KINDS)
        if instrument.asset_type not in {"equity", "index", "etf", "fund"}:
            raise InvalidSymbolError(
                "Yahoo adapter supports listed market instruments only",
                **self._error_details(request, asset_type=instrument.asset_type),
            )
        timezone_name = MARKET_TIMEZONES.get(instrument.market, "UTC")
        rows, interval = self._download(
            request,
            symbol,
            endpoint=request.endpoint,
            timezone_name=timezone_name,
            fetched_at=fetched_at,
        )
        if not rows:
            raise InvalidSymbolError(
                "Yahoo Finance returned no observations at or before requested_as_of",
                **self._error_details(request, provider_symbol=symbol),
            )
        if request.endpoint == "quote":
            rows = rows[-1:]
        if request.endpoint == "bars" and request.metric == "ohlcv":
            normalized_rows = []
            for observed_at, row in rows:
                normalized = {
                    name.casefold(): _finite(_row_value(row, name, symbol))
                    for name in ("Open", "High", "Low", "Close", "Volume")
                }
                if any(normalized[key] is None for key in ("open", "high", "low", "close")):
                    continue
                normalized_rows.append(
                    {
                        "observed_at": utc_text(observed_at),
                        **normalized,
                    }
                )
            if not normalized_rows:
                raise InvalidSymbolError(
                    "Yahoo observations did not contain usable OHLCV rows",
                    **self._error_details(request, metric=request.metric),
                )
            payload = {
                "symbol": symbol,
                "interval": interval,
                "adjustment": AdjustmentMode.RAW.value,
                "bars": normalized_rows,
            }
            quality_flags = ["personal_research_only", "non_exchange_grade"]
            if instrument.market == "CN":
                quality_flags.append("a_share_research_fallback")
            latest_observed = _timestamp(
                normalized_rows[-1]["observed_at"], timezone_name
            )
            return FinancialProviderResponse(
                provider_id=self.provider_id,
                endpoint=request.endpoint,
                license_profile=self.license_profile,
                request_id=request.request_id,
                data_kind=request.data_kind,
                records=(
                    FinancialDataRecord(
                        instrument_id=request.instrument_id,
                        metric=request.metric,
                        value=normalized_rows,
                        unit="ohlcv_series",
                        currency=instrument.currency,
                        market_status=MarketStatus.UNKNOWN,
                        observed_at=latest_observed,
                        fetched_at=fetched_at,
                        timezone=timezone_name,
                        freshness_state=FreshnessState.HISTORICAL,
                        requested_as_of=request.requested_as_of,
                        raw_response_hash=raw_response_hash(payload),
                        normalized_payload=payload,
                        adjustment=AdjustmentMode.RAW,
                        quality_flags=tuple(quality_flags),
                        source_url=f"https://finance.yahoo.com/quote/{quote(symbol, safe='^.')}",
                        provider_symbol=symbol,
                        normalizer_version="yahoo-v1",
                        lineage={
                            "sdk": "yfinance",
                            "sdk_version": self.sdk_version,
                            "source_semantics": "personal_research_fallback",
                            "freshness_threshold_seconds": 900,
                            "requested_as_of_cutoff_applied": True,
                            "row_count": len(normalized_rows),
                        },
                    ),
                ),
            )
        field = PRICE_FIELDS.get(request.metric)
        if field is None:
            raise InvalidSymbolError(
                "unsupported Yahoo metric",
                **self._error_details(request, metric=request.metric),
            )
        records = []
        for observed_at, row in rows:
            normalized = {
                name.casefold(): _finite(_row_value(row, name, symbol))
                for name in ("Open", "High", "Low", "Close", "Volume")
            }
            value = normalized[field.casefold()]
            if value is None:
                continue
            quality_flags = ["personal_research_only", "non_exchange_grade"]
            if instrument.market == "CN":
                quality_flags.append("a_share_research_fallback")
            age = max(0.0, (fetched_at - observed_at).total_seconds())
            if request.endpoint == "bars":
                freshness = FreshnessState.HISTORICAL
            elif age <= 1800:
                freshness = FreshnessState.DELAYED
            else:
                freshness = FreshnessState.STALE
            normalized["interval"] = interval
            normalized["provider_symbol"] = symbol
            raw = {"observed_at": utc_text(observed_at), **normalized}
            records.append(
                FinancialDataRecord(
                    instrument_id=request.instrument_id,
                    metric=request.metric,
                    value=value,
                    unit="shares" if field == "Volume" else "price",
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
                    quality_flags=tuple(quality_flags),
                    source_url=f"https://finance.yahoo.com/quote/{quote(symbol, safe='^.')} ".strip(),
                    provider_symbol=symbol,
                    normalizer_version="yahoo-v1",
                    lineage={
                        "sdk": "yfinance",
                        "sdk_version": self.sdk_version,
                        "source_semantics": "personal_research_fallback",
                        "freshness_threshold_seconds": 900,
                        "requested_as_of_cutoff_applied": True,
                    },
                )
            )
        if not records:
            raise InvalidSymbolError(
                "Yahoo observations did not contain the requested metric",
                **self._error_details(request, metric=request.metric),
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
        instrument = self.instruments.get_by_canonical_symbol("HSI.HK")
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
            status, error_type = "healthy_research_only", None
        except Exception as exc:
            response, status, error_type = None, "unhealthy", type(exc).__name__
        self.update_health(status, requested_at)
        return {
            "provider_id": self.provider_id,
            "status": status,
            "checked_at": utc_text(requested_at),
            "record_count": len(response.records) if response else 0,
            "error_type": error_type,
            "sdk_version": self.sdk_version,
        }

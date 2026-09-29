#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Vendor-neutral contract between financial providers and research services."""

from __future__ import annotations

import hashlib
import json
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class FinancialDataKind(str, Enum):
    QUOTE = "quote"
    BAR = "bar"
    FUNDAMENTAL = "fundamental"
    MACRO = "macro"
    CONSTITUENT = "constituent"
    NEWS = "news"


class MarketStatus(str, Enum):
    PRE_OPEN = "pre_open"
    OPEN = "open"
    LUNCH_BREAK = "lunch_break"
    CLOSED = "closed"
    HALTED = "halted"
    AUCTION = "auction"
    AFTER_HOURS = "after_hours"
    UNKNOWN = "unknown"


class FreshnessState(str, Enum):
    CURRENT = "current"
    DELAYED = "delayed"
    STALE = "stale"
    HISTORICAL = "historical"
    UNKNOWN = "unknown"


class AdjustmentMode(str, Enum):
    RAW = "raw"
    QFQ = "qfq"
    HFQ = "hfq"
    SPLIT_ADJUSTED = "split_adjusted"
    TOTAL_RETURN_ADJUSTED = "total_return_adjusted"
    NOT_APPLICABLE = "not_applicable"


class ProviderErrorCode(str, Enum):
    PERMISSION_DENIED = "permission_denied"
    RATE_LIMITED = "rate_limited"
    UNSUPPORTED_ASSET = "unsupported_asset"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    INVALID_SYMBOL = "invalid_symbol"
    STALE = "stale"


_FORBIDDEN_CONCLUSION_KEYS = {
    "investment_conclusion",
    "trading_decision",
    "recommendation_text",
    "investment_advice",
    "buy_sell_advice",
}


def _aware_datetime(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be a timezone-aware datetime")
    return value


def _utc_text(value: datetime) -> str:
    return _aware_datetime(value, "datetime").astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _enum_value(value: Any, enum_type, label: str):
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"{label} must be one of: {allowed}") from exc


def _validate_timezone(value: str) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError("timezone is required")
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {name}") from exc
    return name


def _validate_json(value: Any, label: str) -> None:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON-compatible and finite") from exc


def _contains_forbidden_conclusion(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).casefold() in _FORBIDDEN_CONCLUSION_KEYS:
                return True
            if _contains_forbidden_conclusion(nested):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_forbidden_conclusion(item) for item in value)
    return False


def raw_response_hash(raw_response: Any) -> str:
    """Return a reproducible SHA-256 without persisting a provider's raw body."""
    if isinstance(raw_response, bytes):
        payload = raw_response
    elif isinstance(raw_response, str):
        payload = raw_response.encode("utf-8")
    else:
        _validate_json(raw_response, "raw_response")
        payload = json.dumps(
            raw_response,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class FinancialDataRequest:
    request_id: str
    endpoint: str
    instrument_id: str
    metric: str
    data_kind: FinancialDataKind | str
    requested_as_of: datetime
    preferred_provider_id: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        for label in ("request_id", "endpoint", "instrument_id", "metric"):
            if not str(getattr(self, label) or "").strip():
                raise ValueError(f"{label} is required")
        object.__setattr__(self, "data_kind", _enum_value(self.data_kind, FinancialDataKind, "data_kind"))
        _aware_datetime(self.requested_as_of, "requested_as_of")
        _validate_json(self.parameters, "parameters")


@dataclass(frozen=True)
class DegradationInfo:
    degraded: bool = False
    reason: str = ""
    requested_provider_id: str = ""
    actual_provider_id: str = ""
    attempted_provider_ids: Tuple[str, ...] = ()

    def __post_init__(self):
        if self.degraded:
            if not str(self.reason or "").strip():
                raise ValueError("degradation reason is required")
            if not self.actual_provider_id:
                raise ValueError("actual_provider_id is required for degradation")
        elif any(
            (self.reason, self.requested_provider_id, self.actual_provider_id, self.attempted_provider_ids)
        ):
            raise ValueError("non-degraded responses cannot carry fallback metadata")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "degraded": self.degraded,
            "reason": self.reason,
            "requested_provider_id": self.requested_provider_id,
            "actual_provider_id": self.actual_provider_id,
            "attempted_provider_ids": list(self.attempted_provider_ids),
        }


@dataclass(frozen=True)
class FinancialDataRecord:
    instrument_id: str
    metric: str
    value: Any
    unit: str
    currency: str
    market_status: MarketStatus | str
    observed_at: datetime
    fetched_at: datetime
    timezone: str
    freshness_state: FreshnessState | str
    requested_as_of: datetime
    raw_response_hash: str
    normalized_payload: Mapping[str, Any]
    effective_from: Optional[datetime] = None
    effective_to: Optional[datetime] = None
    adjustment: AdjustmentMode | str = AdjustmentMode.NOT_APPLICABLE
    quality_flags: Tuple[str, ...] = ()
    source_url: str = ""
    provider_symbol: str = ""
    normalizer_version: str = "v1"
    lineage: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        for label in ("instrument_id", "metric", "unit", "normalizer_version"):
            if not str(getattr(self, label) or "").strip():
                raise ValueError(f"{label} is required")
        currency = str(self.currency or "").strip().upper()
        if currency and (len(currency) != 3 or not currency.isalpha()):
            raise ValueError("currency must be an ISO-like three-letter code or empty")
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "market_status", _enum_value(self.market_status, MarketStatus, "market_status"))
        object.__setattr__(self, "freshness_state", _enum_value(self.freshness_state, FreshnessState, "freshness_state"))
        object.__setattr__(self, "adjustment", _enum_value(self.adjustment, AdjustmentMode, "adjustment"))
        object.__setattr__(self, "timezone", _validate_timezone(self.timezone))
        observed = _aware_datetime(self.observed_at, "observed_at")
        fetched = _aware_datetime(self.fetched_at, "fetched_at")
        requested = _aware_datetime(self.requested_as_of, "requested_as_of")
        if observed.astimezone(timezone.utc) > fetched.astimezone(timezone.utc):
            raise ValueError("observed_at cannot be later than fetched_at")
        if self.effective_from is not None:
            _aware_datetime(self.effective_from, "effective_from")
        if self.effective_to is not None:
            _aware_datetime(self.effective_to, "effective_to")
        if self.effective_from and self.effective_to and self.effective_from > self.effective_to:
            raise ValueError("effective_from cannot be later than effective_to")
        digest = str(self.raw_response_hash or "")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("raw_response_hash must be a lowercase SHA-256 hex digest")
        _validate_json(self.value, "value")
        _validate_json(self.normalized_payload, "normalized_payload")
        _validate_json(self.lineage, "lineage")
        if self.value is None and "missing" not in self.quality_flags:
            raise ValueError("null value requires a missing quality flag")
        if _contains_forbidden_conclusion(self.normalized_payload):
            raise ValueError("provider payload cannot contain an investment conclusion")
        if _contains_forbidden_conclusion(self.lineage):
            raise ValueError("provider lineage cannot contain an investment conclusion")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("value must be finite")
        # Keep the local variables referenced so static analyzers catch future
        # changes that accidentally stop validating the request time.
        _ = requested

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "metric": self.metric,
            "value": self.value,
            "unit": self.unit,
            "currency": self.currency,
            "market_status": self.market_status.value,
            "observed_at": _utc_text(self.observed_at),
            "fetched_at": _utc_text(self.fetched_at),
            "timezone": self.timezone,
            "freshness_state": self.freshness_state.value,
            "requested_as_of": _utc_text(self.requested_as_of),
            "effective_from": _utc_text(self.effective_from) if self.effective_from else None,
            "effective_to": _utc_text(self.effective_to) if self.effective_to else None,
            "raw_response_hash": self.raw_response_hash,
            "normalized_payload": dict(self.normalized_payload),
            "adjustment": self.adjustment.value,
            "quality_flags": list(self.quality_flags),
            "source_url": self.source_url,
            "provider_symbol": self.provider_symbol,
            "normalizer_version": self.normalizer_version,
            "lineage": {
                "source_url": self.source_url,
                "provider_symbol": self.provider_symbol,
                "normalizer_version": self.normalizer_version,
                "details": dict(self.lineage),
            },
        }


@dataclass(frozen=True)
class FinancialProviderResponse:
    provider_id: str
    endpoint: str
    license_profile: str
    request_id: str
    data_kind: FinancialDataKind | str
    records: Tuple[FinancialDataRecord, ...]
    degradation: DegradationInfo = field(default_factory=DegradationInfo)

    def __post_init__(self):
        for label in ("provider_id", "endpoint", "license_profile", "request_id"):
            if not str(getattr(self, label) or "").strip():
                raise ValueError(f"{label} is required")
        object.__setattr__(self, "data_kind", _enum_value(self.data_kind, FinancialDataKind, "data_kind"))
        if not isinstance(self.records, tuple) or not self.records:
            raise ValueError("records must be a non-empty tuple")
        if any(not isinstance(record, FinancialDataRecord) for record in self.records):
            raise ValueError("records must contain FinancialDataRecord values")

    def validate_for(self, request: FinancialDataRequest) -> "FinancialProviderResponse":
        if self.request_id != request.request_id:
            raise ValueError("response request_id does not match request")
        if self.endpoint != request.endpoint:
            raise ValueError("response endpoint does not match request")
        if self.data_kind != request.data_kind:
            raise ValueError("response data_kind does not match request")
        for record in self.records:
            if record.instrument_id != request.instrument_id:
                raise ValueError("record instrument_id does not match request")
            if record.metric != request.metric:
                raise ValueError("record metric does not match request")
            if record.requested_as_of.astimezone(timezone.utc) != request.requested_as_of.astimezone(timezone.utc):
                raise ValueError("record requested_as_of does not match request")
            if record.observed_at.astimezone(timezone.utc) > request.requested_as_of.astimezone(timezone.utc):
                raise ValueError("record observed_at cannot be later than requested_as_of")
        preferred = str(request.preferred_provider_id or "")
        if preferred and preferred != self.provider_id:
            if not self.degradation.degraded:
                raise ValueError("provider fallback requires explicit degradation metadata")
            if self.degradation.requested_provider_id != preferred:
                raise ValueError("degradation requested_provider_id does not match request")
            if self.degradation.actual_provider_id != self.provider_id:
                raise ValueError("degradation actual_provider_id does not match response")
        return self

    def evidence(self) -> Tuple[Dict[str, Any], ...]:
        common = {
            "provider_id": self.provider_id,
            "endpoint": self.endpoint,
            "license_profile": self.license_profile,
            "request_id": self.request_id,
            "data_kind": self.data_kind.value,
            "degradation": self.degradation.to_dict(),
        }
        return tuple({**common, **record.to_dict()} for record in self.records)


class FinancialProviderError(Exception):
    code: ProviderErrorCode
    default_retryable = False

    def __init__(
        self,
        message: str,
        *,
        provider_id: str,
        endpoint: str,
        request_id: str,
        retryable: Optional[bool] = None,
        retry_after_seconds: Optional[int] = None,
        details: Optional[Mapping[str, Any]] = None,
    ):
        super().__init__(str(message or self.code.value))
        self.provider_id = str(provider_id or "")
        self.endpoint = str(endpoint or "")
        self.request_id = str(request_id or "")
        self.retryable = self.default_retryable if retryable is None else bool(retryable)
        self.retry_after_seconds = retry_after_seconds
        self.details = dict(details or {})
        if not all((self.provider_id, self.endpoint, self.request_id)):
            raise ValueError("provider_id, endpoint and request_id are required for provider errors")
        if retry_after_seconds is not None and int(retry_after_seconds) < 0:
            raise ValueError("retry_after_seconds cannot be negative")
        _validate_json(self.details, "error details")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code.value,
            "message": str(self),
            "provider_id": self.provider_id,
            "endpoint": self.endpoint,
            "request_id": self.request_id,
            "retryable": self.retryable,
            "retry_after_seconds": self.retry_after_seconds,
            "details": self.details,
        }


class PermissionDeniedError(FinancialProviderError):
    code = ProviderErrorCode.PERMISSION_DENIED


class RateLimitedError(FinancialProviderError):
    code = ProviderErrorCode.RATE_LIMITED
    default_retryable = True


class UnsupportedAssetError(FinancialProviderError):
    code = ProviderErrorCode.UNSUPPORTED_ASSET


class TemporarilyUnavailableError(FinancialProviderError):
    code = ProviderErrorCode.TEMPORARILY_UNAVAILABLE
    default_retryable = True


class InvalidSymbolError(FinancialProviderError):
    code = ProviderErrorCode.INVALID_SYMBOL


class StaleDataError(FinancialProviderError):
    code = ProviderErrorCode.STALE
    default_retryable = True


PROVIDER_ERROR_TYPES = {
    error_type.code.value: error_type
    for error_type in (
        PermissionDeniedError,
        RateLimitedError,
        UnsupportedAssetError,
        TemporarilyUnavailableError,
        InvalidSymbolError,
        StaleDataError,
    )
}


class FinancialDataProvider(ABC):
    """Provider adapters normalize facts only; they never form investment advice."""

    @property
    @abstractmethod
    def provider_id(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def license_profile(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def capabilities(self) -> Sequence[FinancialDataKind]:
        raise NotImplementedError

    @abstractmethod
    def fetch(self, request: FinancialDataRequest) -> FinancialProviderResponse:
        raise NotImplementedError

    def fetch_validated(self, request: FinancialDataRequest) -> FinancialProviderResponse:
        response = self.fetch(request)
        if not isinstance(response, FinancialProviderResponse):
            raise TypeError("provider must return FinancialProviderResponse")
        if response.provider_id != self.provider_id:
            raise ValueError("provider adapter returned a different provider_id")
        if response.license_profile != self.license_profile:
            raise ValueError("provider adapter returned a different license_profile")
        if request.data_kind not in set(self.capabilities):
            raise UnsupportedAssetError(
                f"provider does not support {request.data_kind.value}",
                provider_id=self.provider_id,
                endpoint=request.endpoint,
                request_id=request.request_id,
            )
        return response.validate_for(request)

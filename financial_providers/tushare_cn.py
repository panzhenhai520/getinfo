#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Tushare Pro adapter with per-capability entitlement detection.

The adapter never treats a configured token as proof that a paid endpoint is
available.  Permission probes and provider errors retain only stable status
codes; the token and raw exception text never enter logs, snapshots, or the
configuration response.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from datetime import date, datetime, time as wall_time, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import config
from financial_config import (
    FinancialCapabilityDisabled,
    financial_capabilities,
    require_financial_capability,
)
from financial_instruments import InstrumentRecord, InstrumentRegistry
from financial_market_clock import MarketClockService, RequestTimeContext
from financial_provider_contract import (
    AdjustmentMode,
    FinancialDataKind,
    FinancialDataProvider,
    FinancialDataRecord,
    FinancialDataRequest,
    FinancialProviderResponse,
    FreshnessState,
    InvalidSymbolError,
    MarketStatus,
    PermissionDeniedError,
    RateLimitedError,
    TemporarilyUnavailableError,
    UnsupportedAssetError,
    raw_response_hash,
)


PROFILE_PATH = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "financial_providers"
    / "tushare_cn.json"
)
PROFILE = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
PROVIDER_ID = "tushare_cn"
NORMALIZER_VERSION = "tushare-cn-v1"
MARKET_TIMEZONE = "Asia/Shanghai"
TUSHARE_API_URL = "https://api.waditu.com/dataapi/"
SDK_URL_ATTRIBUTE = "_DataApi__http_url"
ALLOWED_API_HOSTS = frozenset({"api.waditu.com", "api.tushare.pro"})
ENDPOINT_KINDS = {
    "instrument_master": FinancialDataKind.FUNDAMENTAL,
    "quote": FinancialDataKind.QUOTE,
    "bars": FinancialDataKind.BAR,
    "constituents": FinancialDataKind.CONSTITUENT,
    "fund": FinancialDataKind.FUNDAMENTAL,
    "financials": FinancialDataKind.FUNDAMENTAL,
    "announcements": FinancialDataKind.NEWS,
}
FINANCIAL_STATEMENTS = frozenset(
    {"income", "balancesheet", "cashflow", "fina_indicator"}
)
PERMISSION_PROBES = (
    (
        "instrument_master",
        "stock_basic",
        {"ts_code": "000001.SZ", "fields": "ts_code,name,list_status,list_date"},
    ),
    (
        "daily_market",
        "daily",
        {"ts_code": "000001.SZ", "start_date": "20260731", "end_date": "20260731"},
    ),
    (
        "realtime_equity",
        "rt_min",
        {"ts_code": "000001.SZ", "freq": "1MIN"},
    ),
    (
        "realtime_etf",
        "rt_etf_min",
        {"ts_code": "510300.SH", "freq": "1MIN"},
    ),
    (
        "realtime_index",
        "rt_idx_min",
        {"ts_code": "000001.SH", "freq": "1MIN"},
    ),
    (
        "index_constituents",
        "index_weight",
        {"index_code": "000300.SH", "start_date": "20260701", "end_date": "20260731"},
    ),
    (
        "fund",
        "fund_basic",
        {"market": "E", "status": "L", "fields": "ts_code,name,status"},
    ),
    (
        "financials",
        "fina_indicator",
        {"ts_code": "000001.SZ", "start_date": "20250101", "end_date": "20251231"},
    ),
    (
        "announcements",
        "anns_d",
        {"ts_code": "000001.SZ", "start_date": "20260731", "end_date": "20260731"},
    ),
)


def _setting(settings, name: str, default=None):
    if isinstance(settings, Mapping):
        return settings.get(name, default)
    return getattr(settings, name, default)


def _aware_utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return _aware_utc(value, "datetime").isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _json_safe(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except (TypeError, ValueError):
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except (TypeError, ValueError):
            pass
    return str(value)


def _records(frame) -> list[Dict[str, object]]:
    if frame is None:
        return []
    if isinstance(frame, Mapping):
        return [{str(key): _json_safe(value) for key, value in frame.items()}]
    if isinstance(frame, (list, tuple)):
        if any(not isinstance(item, Mapping) for item in frame):
            raise ValueError("provider rows must be mappings")
        return [
            {str(key): _json_safe(value) for key, value in item.items()}
            for item in frame
        ]
    if hasattr(frame, "to_dict"):
        try:
            return _records(frame.to_dict(orient="records"))
        except TypeError:
            return _records(frame.to_dict("records"))
    raise ValueError(f"unsupported Tushare response type: {type(frame).__name__}")


def _number(value, *, allow_missing: bool = False):
    if value in (None, "", "-", "--"):
        if allow_missing:
            return None
        raise ValueError("required numeric value is missing")
    result = float(value)
    if not math.isfinite(result):
        if allow_missing:
            return None
        raise ValueError("required numeric value is not finite")
    return result


def _date_parameter(value, label: str) -> str:
    text = str(value or "").strip().replace("-", "")
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"{label} must be YYYYMMDD or YYYY-MM-DD") from exc
    return text


def _looks_invalid_token(message: str) -> bool:
    lowered = message.casefold()
    return any(
        marker in lowered
        for marker in ("token无效", "token 无效", "invalid token", "token is invalid")
    )


def _looks_permission_denied(message: str) -> bool:
    lowered = message.casefold()
    return any(
        marker in lowered
        for marker in (
            "没有访问该接口的权限",
            "没有接口",
            "访问权限",
            "无权限",
            "permission denied",
            "no permission",
        )
    )


def _looks_rate_limited(message: str) -> bool:
    lowered = message.casefold()
    return any(
        marker in lowered
        for marker in ("每分钟", "访问频率", "最多访问", "rate limit", "too many", "429")
    )


class TushareCNProvider(FinancialDataProvider):
    def __init__(
        self,
        *,
        instrument_registry: InstrumentRegistry,
        sdk=None,
        client=None,
        settings=None,
        market_clock: Optional[MarketClockService] = None,
        clock: Optional[Callable[[], datetime]] = None,
        connection=None,
    ):
        self.instruments = instrument_registry
        self._sdk = sdk
        self._client = client
        self.settings = config if settings is None else settings
        self.market_clock = market_clock or MarketClockService()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.connection = connection
        self._last_permission_probe: Optional[Mapping[str, object]] = None

    @property
    def provider_id(self) -> str:
        return PROVIDER_ID

    @property
    def license_profile(self) -> str:
        return str(PROFILE["license_profile"])

    @property
    def capabilities(self) -> Sequence[FinancialDataKind]:
        return (
            FinancialDataKind.QUOTE,
            FinancialDataKind.BAR,
            FinancialDataKind.CONSTITUENT,
            FinancialDataKind.FUNDAMENTAL,
            FinancialDataKind.NEWS,
        )

    @property
    def sdk_version(self) -> str:
        if self._sdk is None:
            return "injected_client" if self._client is not None else "not_loaded"
        return str(getattr(self._sdk, "__version__", "injected"))

    def _error_kwargs(self, request: FinancialDataRequest, **details):
        return {
            "provider_id": self.provider_id,
            "endpoint": request.endpoint,
            "request_id": request.request_id,
            "details": details,
        }

    def _require_enabled(self, request: FinancialDataRequest) -> None:
        try:
            require_financial_capability("tushare_cn", self.settings)
        except FinancialCapabilityDisabled as exc:
            raise PermissionDeniedError(
                "Tushare provider is disabled or its token is not configured",
                **self._error_kwargs(request, gate_reason=exc.reason),
            ) from exc

    def _load_client(self, request: FinancialDataRequest):
        if self._client is not None:
            return self._client
        token = str(_setting(self.settings, "TUSHARE_TOKEN", "") or "").strip()
        if not token:
            raise PermissionDeniedError(
                "Tushare token is not configured",
                **self._error_kwargs(request, gate_reason="tushare_token_missing"),
            )
        sdk = self._sdk
        if sdk is None:
            try:
                import tushare as tushare_sdk
            except ImportError as exc:
                raise TemporarilyUnavailableError(
                    "Tushare dependency is not installed in this runtime",
                    **self._error_kwargs(
                        request,
                        dependency="tushare",
                        required_version=PROFILE["package_version"],
                    ),
                ) from exc
            sdk = tushare_sdk
            self._sdk = sdk
        try:
            self._client = sdk.pro_api(token)
        except Exception as exc:
            raise TemporarilyUnavailableError(
                "Tushare SDK client initialization failed",
                **self._error_kwargs(
                    request,
                    failure="sdk_client_initialization",
                    exception_type=type(exc).__name__,
                ),
            ) from exc
        configured_url = str(getattr(self._client, SDK_URL_ATTRIBUTE, "") or "").strip()
        parsed = urlsplit(configured_url)
        if parsed.hostname not in ALLOWED_API_HOSTS or parsed.scheme not in {"http", "https"}:
            self._client = None
            raise TemporarilyUnavailableError(
                "Tushare SDK transport endpoint cannot be safely verified",
                **self._error_kwargs(
                    request,
                    failure="unsafe_sdk_transport",
                    sdk_version=self.sdk_version,
                ),
            )
        secure_url = parsed._replace(scheme="https").geturl().rstrip("/")
        try:
            setattr(self._client, SDK_URL_ATTRIBUTE, secure_url)
        except (AttributeError, TypeError) as exc:
            self._client = None
            raise TemporarilyUnavailableError(
                "Tushare SDK transport cannot be forced to HTTPS",
                **self._error_kwargs(
                    request,
                    failure="unsafe_sdk_transport",
                    sdk_version=self.sdk_version,
                ),
            ) from exc
        verified = urlsplit(str(getattr(self._client, SDK_URL_ATTRIBUTE, "") or ""))
        if verified.scheme != "https" or verified.hostname not in ALLOWED_API_HOSTS:
            self._client = None
            raise TemporarilyUnavailableError(
                "Tushare SDK transport HTTPS enforcement failed",
                **self._error_kwargs(
                    request,
                    failure="unsafe_sdk_transport",
                    sdk_version=self.sdk_version,
                ),
            )
        return self._client

    def _invoke(self, api_name: str, request: FinancialDataRequest, **parameters):
        client = self._load_client(request)
        function = getattr(client, api_name, None)
        if not callable(function):
            raise TemporarilyUnavailableError(
                f"Tushare endpoint is missing: {api_name}",
                **self._error_kwargs(
                    request,
                    api_name=api_name,
                    failure="sdk_api_drift",
                    sdk_version=self.sdk_version,
                ),
            )
        timeout_seconds = int(
            _setting(self.settings, "FINANCIAL_PROVIDER_TIMEOUT_SECONDS", 20)
        )
        state: Dict[str, object] = {}

        def invoke_client():
            try:
                state["result"] = function(**parameters)
            except Exception as exc:
                state["error"] = exc

        thread = threading.Thread(
            target=invoke_client,
            name=f"tushare-{api_name}",
            daemon=True,
        )
        thread.start()
        thread.join(timeout_seconds)
        if thread.is_alive():
            raise TemporarilyUnavailableError(
                "Tushare request exceeded the hard timeout",
                **self._error_kwargs(
                    request,
                    api_name=api_name,
                    failure="hard_timeout",
                    timeout_seconds=timeout_seconds,
                ),
            )
        if "error" not in state:
            return state.get("result")
        exc = state["error"]
        message = str(exc)
        details = self._error_kwargs(
            request,
            api_name=api_name,
            exception_type=type(exc).__name__,
        )
        if _looks_invalid_token(message):
            details["details"]["authentication"] = "invalid_token"
            raise PermissionDeniedError(
                "Tushare rejected the configured token", **details
            ) from exc
        if _looks_permission_denied(message):
            details["details"]["permission"] = "denied"
            raise PermissionDeniedError(
                "Tushare account has no permission for this endpoint", **details
            ) from exc
        if _looks_rate_limited(message):
            raise RateLimitedError("Tushare rate limited the request", **details) from exc
        raise TemporarilyUnavailableError(
            "Tushare request failed", **details
        ) from exc

    def _checked_rows(
        self,
        frame,
        request: FinancialDataRequest,
        *,
        api_name: str,
        required_columns: Iterable[str],
        allow_empty: bool = False,
    ) -> list[Dict[str, object]]:
        try:
            rows = _records(frame)
        except (TypeError, ValueError) as exc:
            raise TemporarilyUnavailableError(
                "Tushare returned an unsupported table shape",
                **self._error_kwargs(
                    request, api_name=api_name, failure="response_type_drift"
                ),
            ) from exc
        if not rows:
            if allow_empty:
                return []
            raise TemporarilyUnavailableError(
                "Tushare returned an empty table",
                **self._error_kwargs(
                    request, api_name=api_name, failure="empty_response"
                ),
            )
        columns = {str(key) for row in rows for key in row}
        missing = sorted(set(required_columns) - columns)
        if missing:
            raise TemporarilyUnavailableError(
                "Tushare response schema changed",
                **self._error_kwargs(
                    request,
                    api_name=api_name,
                    failure="field_drift",
                    missing_columns=missing,
                    observed_columns=sorted(columns),
                ),
            )
        return rows

    def _instrument(self, request: FinancialDataRequest) -> InstrumentRecord:
        try:
            instrument_id = int(request.instrument_id)
        except (TypeError, ValueError) as exc:
            raise InvalidSymbolError(
                "instrument_id must reference the project instrument registry",
                **self._error_kwargs(request, instrument_id=str(request.instrument_id)),
            ) from exc
        instrument = self.instruments.get(instrument_id)
        if instrument is None:
            raise InvalidSymbolError(
                "instrument_id is not registered",
                **self._error_kwargs(request, instrument_id=instrument_id),
            )
        if instrument.market not in {"CN", "CN_FUND"}:
            raise UnsupportedAssetError(
                "TushareCNProvider only accepts registered mainland instruments",
                **self._error_kwargs(
                    request, market=instrument.market, asset_type=instrument.asset_type
                ),
            )
        return instrument

    def _provider_symbol(
        self, request: FinancialDataRequest, instrument: InstrumentRecord
    ) -> str:
        symbol = str(instrument.provider_mappings.get(self.provider_id) or "").strip()
        if not symbol:
            raise InvalidSymbolError(
                "instrument has no tushare_cn provider mapping",
                **self._error_kwargs(request, instrument_id=instrument.instrument_id),
            )
        return symbol

    def _fetched_at(self) -> datetime:
        return _aware_utc(self._clock(), "provider clock")

    @staticmethod
    def _parse_observed(
        value,
        *,
        fetched_at: datetime,
        default_time: wall_time = wall_time(15, 0),
    ) -> Tuple[datetime, Tuple[str, ...], bool]:
        if value in (None, "", "-", "--"):
            return fetched_at, ("provider_timestamp_missing",), False
        zone = ZoneInfo(MARKET_TIMEZONE)
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, date):
            parsed = datetime.combine(value, default_time)
        else:
            text = str(value).strip()
            if len(text) == 8 and text.isdigit():
                parsed = datetime.combine(datetime.strptime(text, "%Y%m%d").date(), default_time)
            elif len(text) == 10:
                try:
                    parsed = datetime.combine(date.fromisoformat(text), default_time)
                except ValueError as exc:
                    raise ValueError("unparseable provider timestamp") from exc
            elif len(text) in {5, 8} and text.count(":") in {1, 2}:
                return fetched_at, ("provider_date_missing",), False
            else:
                try:
                    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("unparseable provider timestamp") from exc
        flags = []
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=zone)
            flags.append("provider_timezone_assumed_asia_shanghai")
        observed = parsed.astimezone(timezone.utc)
        if observed > fetched_at:
            raise ValueError("provider observed timestamp is later than fetch time")
        return observed, tuple(flags), True

    def _market_state(
        self, instrument: InstrumentRecord, fetched_at: datetime
    ) -> MarketStatus:
        calendar_id = instrument.exchange if instrument.exchange in {"XSHG", "XSHE"} else ""
        if not calendar_id:
            return MarketStatus.UNKNOWN
        context = RequestTimeContext(
            server_now_utc=fetched_at,
            server_timezone="Asia/Hong_Kong",
            user_timezone=MARKET_TIMEZONE,
        )
        return self.market_clock.market_state(calendar_id, context).market_session_state

    def _freshness(
        self,
        observed_at: datetime,
        fetched_at: datetime,
        *,
        threshold: int,
        timestamp_available: bool,
    ) -> FreshnessState:
        if not timestamp_available:
            return FreshnessState.UNKNOWN
        context = RequestTimeContext(
            server_now_utc=fetched_at,
            server_timezone="Asia/Hong_Kong",
            user_timezone=MARKET_TIMEZONE,
        )
        return self.market_clock.assess_freshness(
            observed_at,
            fetched_at,
            current_threshold_seconds=threshold,
            context=context,
        ).state

    def _response(
        self,
        request: FinancialDataRequest,
        *,
        instrument: InstrumentRecord,
        value,
        unit: str,
        normalized_payload: Mapping[str, object],
        raw_row,
        observed_value,
        fetched_at: datetime,
        provider_symbol: str,
        api_name: str,
        adjustment: AdjustmentMode = AdjustmentMode.NOT_APPLICABLE,
        quality_flags: Sequence[str] = (),
        threshold: Optional[int] = None,
        default_time: wall_time = wall_time(15, 0),
        lineage: Optional[Mapping[str, object]] = None,
    ) -> FinancialProviderResponse:
        try:
            observed_at, time_flags, timestamp_available = self._parse_observed(
                observed_value,
                fetched_at=fetched_at,
                default_time=default_time,
            )
        except ValueError as exc:
            raise TemporarilyUnavailableError(
                "Tushare returned an invalid observation timestamp",
                **self._error_kwargs(
                    request, api_name=api_name, failure="invalid_timestamp"
                ),
            ) from exc
        if not timestamp_available and observed_at > request.requested_as_of:
            observed_at = request.requested_as_of
            time_flags = tuple(time_flags) + (
                "observed_at_bounded_by_requested_as_of",
            )
        freshness_threshold = int(
            threshold
            or _setting(self.settings, "FINANCIAL_QUOTE_FRESHNESS_SECONDS", 300)
        )
        record = FinancialDataRecord(
            instrument_id=str(instrument.instrument_id),
            metric=request.metric,
            value=_json_safe(value),
            unit=unit,
            currency=instrument.currency,
            market_status=self._market_state(instrument, fetched_at),
            observed_at=observed_at,
            fetched_at=fetched_at,
            timezone=MARKET_TIMEZONE,
            freshness_state=self._freshness(
                observed_at,
                fetched_at,
                threshold=freshness_threshold,
                timestamp_available=timestamp_available,
            ),
            requested_as_of=request.requested_as_of,
            raw_response_hash=raw_response_hash(_json_safe(raw_row)),
            normalized_payload=_json_safe(normalized_payload),
            adjustment=adjustment,
            quality_flags=tuple(dict.fromkeys(tuple(quality_flags) + time_flags)),
            source_url=TUSHARE_API_URL,
            provider_symbol=provider_symbol,
            normalizer_version=NORMALIZER_VERSION,
            lineage={
                "sdk_version": self.sdk_version,
                "api_name": api_name,
                "account_entitlement_required": True,
                "freshness_threshold_seconds": freshness_threshold,
                **dict(lineage or {}),
            },
        )
        return FinancialProviderResponse(
            provider_id=self.provider_id,
            endpoint=request.endpoint,
            license_profile=self.license_profile,
            request_id=request.request_id,
            data_kind=request.data_kind,
            records=(record,),
        ).validate_for(request)

    def _fetch_quote(self, request: FinancialDataRequest) -> FinancialProviderResponse:
        instrument = self._instrument(request)
        symbol = self._provider_symbol(request, instrument)
        api_by_asset = {
            "equity": "rt_min",
            "etf": "rt_etf_min",
            "index": "rt_idx_min",
        }
        api_name = api_by_asset.get(instrument.asset_type)
        if not api_name:
            raise UnsupportedAssetError(
                f"Tushare realtime quote does not support {instrument.asset_type}",
                **self._error_kwargs(request, asset_type=instrument.asset_type),
            )
        rows = self._checked_rows(
            self._invoke(api_name, request, ts_code=symbol, freq="1MIN"),
            request,
            api_name=api_name,
            required_columns=("time", "open", "close", "high", "low", "vol", "amount"),
        )
        matching = [
            row
            for row in rows
            if not (row.get("ts_code") or row.get("code"))
            or str(row.get("ts_code") or row.get("code")) == symbol
        ]
        if not matching:
            raise InvalidSymbolError(
                "Tushare realtime table did not contain the requested symbol",
                **self._error_kwargs(request, provider_symbol=symbol),
            )
        latest = max(matching, key=lambda row: str(row.get("time") or ""))
        try:
            price = _number(latest.get("close"))
        except (TypeError, ValueError) as exc:
            raise TemporarilyUnavailableError(
                "Tushare realtime quote has no usable close",
                **self._error_kwargs(request, api_name=api_name, failure="missing_price"),
            ) from exc
        normalized = {
            "symbol": symbol,
            "name": instrument.display_name,
            "last_price": price,
            "open": _number(latest.get("open"), allow_missing=True),
            "high": _number(latest.get("high"), allow_missing=True),
            "low": _number(latest.get("low"), allow_missing=True),
            "volume": _number(latest.get("vol"), allow_missing=True),
            "turnover": _number(latest.get("amount"), allow_missing=True),
        }
        value = normalized if request.metric == "quote" else price
        return self._response(
            request,
            instrument=instrument,
            value=value,
            unit="quote" if request.metric == "quote" else "price",
            normalized_payload=normalized,
            raw_row=matching,
            observed_value=latest.get("time"),
            fetched_at=self._fetched_at(),
            provider_symbol=symbol,
            api_name=api_name,
            quality_flags=("account_realtime_entitlement_required",),
            lineage={"calculation": "latest_entitled_one_minute_close"},
        )

    def _fetch_bars(self, request: FinancialDataRequest) -> FinancialProviderResponse:
        instrument = self._instrument(request)
        symbol = self._provider_symbol(request, instrument)
        interval = str(request.parameters.get("interval") or "1d").lower()
        if interval != "1d":
            raise UnsupportedAssetError(
                "Tushare historical adapter currently accepts raw daily bars only",
                **self._error_kwargs(request, interval=interval),
            )
        adjustment = str(request.parameters.get("adjustment") or "raw").lower()
        if adjustment != "raw":
            raise UnsupportedAssetError(
                "Tushare daily endpoints are unadjusted; adjusted pro_bar is not used implicitly",
                **self._error_kwargs(request, adjustment=adjustment),
            )
        start = _date_parameter(request.parameters.get("start"), "start")
        end = _date_parameter(request.parameters.get("end"), "end")
        requested_date = request.requested_as_of.astimezone(
            ZoneInfo(MARKET_TIMEZONE)
        ).date()
        if datetime.strptime(end, "%Y%m%d").date() > requested_date:
            raise ValueError("bar end date cannot be later than requested_as_of")
        api_by_asset = {"equity": "daily", "etf": "fund_daily", "index": "index_daily"}
        api_name = api_by_asset.get(instrument.asset_type)
        if not api_name:
            raise UnsupportedAssetError(
                f"Tushare daily bars do not support {instrument.asset_type}",
                **self._error_kwargs(request, asset_type=instrument.asset_type),
            )
        rows = self._checked_rows(
            self._invoke(
                api_name,
                request,
                ts_code=symbol,
                start_date=start,
                end_date=end,
            ),
            request,
            api_name=api_name,
            required_columns=("ts_code", "trade_date", "open", "high", "low", "close", "vol"),
        )
        rows = [row for row in rows if str(row.get("ts_code")) == symbol]
        if not rows:
            raise InvalidSymbolError(
                "Tushare daily table did not contain the requested symbol",
                **self._error_kwargs(request, provider_symbol=symbol),
            )
        normalized_rows = [
            {
                "time": row.get("trade_date"),
                "open": _number(row.get("open")),
                "high": _number(row.get("high")),
                "low": _number(row.get("low")),
                "close": _number(row.get("close")),
                "volume": _number(row.get("vol"), allow_missing=True),
                "turnover": _number(row.get("amount"), allow_missing=True),
            }
            for row in sorted(rows, key=lambda item: str(item.get("trade_date") or ""))
        ]
        latest_date = max(str(row.get("trade_date") or "") for row in rows)
        payload = {
            "symbol": symbol,
            "interval": interval,
            "adjustment": AdjustmentMode.RAW.value,
            "bars": normalized_rows,
        }
        return self._response(
            request,
            instrument=instrument,
            value=normalized_rows,
            unit="ohlcv",
            normalized_payload=payload,
            raw_row=rows,
            observed_value=latest_date,
            fetched_at=self._fetched_at(),
            provider_symbol=symbol,
            api_name=api_name,
            adjustment=AdjustmentMode.RAW,
            quality_flags=("historical_daily_bar",),
            lineage={"row_count": len(rows), "field_mapping": "tushare_daily_ohlcv"},
        )

    def _fetch_instrument_master(
        self, request: FinancialDataRequest
    ) -> FinancialProviderResponse:
        instrument = self._instrument(request)
        symbol = self._provider_symbol(request, instrument)
        api_by_asset = {
            "equity": "stock_basic",
            "index": "index_basic",
            "etf": "fund_basic",
            "fund": "fund_basic",
        }
        api_name = api_by_asset.get(instrument.asset_type)
        if not api_name:
            raise UnsupportedAssetError(
                f"Tushare master data does not support {instrument.asset_type}",
                **self._error_kwargs(request, asset_type=instrument.asset_type),
            )
        parameters = {"ts_code": symbol}
        rows = self._checked_rows(
            self._invoke(api_name, request, **parameters),
            request,
            api_name=api_name,
            required_columns=("ts_code", "name"),
        )
        matching = [row for row in rows if str(row.get("ts_code")) == symbol]
        if len(matching) != 1:
            raise InvalidSymbolError(
                "Tushare master table did not contain one exact symbol",
                **self._error_kwargs(
                    request, provider_symbol=symbol, match_count=len(matching)
                ),
            )
        row = matching[0]
        normalized = {
            "ts_code": symbol,
            "name": row.get("name"),
            "fullname": row.get("fullname"),
            "exchange": row.get("exchange"),
            "market": row.get("market"),
            "industry": row.get("industry"),
            "list_status": row.get("list_status") or row.get("status"),
            "list_date": row.get("list_date") or row.get("found_date"),
            "delist_date": row.get("delist_date") or row.get("due_date"),
        }
        return self._response(
            request,
            instrument=instrument,
            value=normalized,
            unit="instrument_master_record",
            normalized_payload=normalized,
            raw_row=row,
            observed_value=None,
            fetched_at=self._fetched_at(),
            provider_symbol=symbol,
            api_name=api_name,
            threshold=int(
                _setting(self.settings, "FINANCIAL_FUNDAMENTAL_FRESHNESS_SECONDS", 86400)
            ),
        )

    def _fetch_constituents(
        self, request: FinancialDataRequest
    ) -> FinancialProviderResponse:
        instrument = self._instrument(request)
        if instrument.asset_type != "index":
            raise UnsupportedAssetError(
                "Tushare index_weight requires an index instrument",
                **self._error_kwargs(request, asset_type=instrument.asset_type),
            )
        symbol = self._provider_symbol(request, instrument)
        start = _date_parameter(request.parameters.get("start"), "start")
        end = _date_parameter(request.parameters.get("end"), "end")
        api_name = "index_weight"
        rows = self._checked_rows(
            self._invoke(
                api_name,
                request,
                index_code=symbol,
                start_date=start,
                end_date=end,
            ),
            request,
            api_name=api_name,
            required_columns=("index_code", "con_code", "trade_date", "weight"),
        )
        matching = [row for row in rows if str(row.get("index_code")) == symbol]
        if not matching:
            raise InvalidSymbolError(
                "Tushare index_weight returned no exact index",
                **self._error_kwargs(request, provider_symbol=symbol),
            )
        normalized = [
            {
                "instrument_provider_symbol": str(row.get("con_code")),
                "weight": _number(row.get("weight"), allow_missing=True),
                "effective_on": row.get("trade_date"),
            }
            for row in matching
        ]
        observed = max(str(row.get("trade_date") or "") for row in matching)
        payload = {"index_code": symbol, "constituents": normalized}
        return self._response(
            request,
            instrument=instrument,
            value=normalized,
            unit="constituent_weights",
            normalized_payload=payload,
            raw_row=matching,
            observed_value=observed,
            fetched_at=self._fetched_at(),
            provider_symbol=symbol,
            api_name=api_name,
            quality_flags=("monthly_index_weight_source",),
            threshold=int(
                _setting(self.settings, "FINANCIAL_FUNDAMENTAL_FRESHNESS_SECONDS", 86400)
            ),
            lineage={"row_count": len(matching)},
        )

    def _fetch_fund(self, request: FinancialDataRequest) -> FinancialProviderResponse:
        instrument = self._instrument(request)
        if instrument.asset_type not in {"fund", "etf"}:
            raise UnsupportedAssetError(
                "Tushare fund endpoint requires a fund or ETF instrument",
                **self._error_kwargs(request, asset_type=instrument.asset_type),
            )
        symbol = self._provider_symbol(request, instrument)
        metric = str(request.metric or "")
        if metric == "fund_basic":
            api_name = "fund_basic"
            parameters = {"ts_code": symbol}
            required = ("ts_code", "name")
            observed_field = None
        elif metric == "fund_nav":
            api_name = "fund_nav"
            parameters = {
                "ts_code": symbol,
                "start_date": _date_parameter(request.parameters.get("start"), "start"),
                "end_date": _date_parameter(request.parameters.get("end"), "end"),
            }
            required = ("ts_code", "end_date", "unit_nav", "accum_nav")
            observed_field = "ann_date"
        elif metric == "fund_holdings":
            api_name = "fund_portfolio"
            parameters = {
                "ts_code": symbol,
                "start_date": _date_parameter(request.parameters.get("start"), "start"),
                "end_date": _date_parameter(request.parameters.get("end"), "end"),
            }
            required = ("ts_code", "ann_date", "end_date", "symbol", "mkv")
            observed_field = "ann_date"
        elif metric == "fund_manager":
            api_name = "fund_manager"
            parameters = {"ts_code": symbol}
            required = ("ts_code", "ann_date", "name", "begin_date")
            observed_field = "ann_date"
        elif metric == "fund_share":
            api_name = "fund_share"
            parameters = {
                "ts_code": symbol,
                "start_date": _date_parameter(request.parameters.get("start"), "start"),
                "end_date": _date_parameter(request.parameters.get("end"), "end"),
            }
            required = ("ts_code", "trade_date", "fd_share")
            observed_field = "trade_date"
        else:
            raise UnsupportedAssetError(
                "Tushare does not implement the requested fund metric",
                **self._error_kwargs(request, metric=metric),
            )
        requested_date = request.requested_as_of.astimezone(
            ZoneInfo(MARKET_TIMEZONE)
        ).date()
        parameter_end = parameters.get("end_date")
        if parameter_end and datetime.strptime(str(parameter_end), "%Y%m%d").date() > requested_date:
            raise ValueError("fund endpoint end date cannot be later than requested_as_of")
        rows = self._checked_rows(
            self._invoke(api_name, request, **parameters),
            request,
            api_name=api_name,
            required_columns=required,
        )
        matching = [row for row in rows if str(row.get("ts_code")) == symbol]
        if observed_field:
            safe_matching = []
            for row in matching:
                observed_text = str(
                    row.get(observed_field)
                    or row.get("end_date")
                    or row.get("trade_date")
                    or ""
                ).replace("-", "")
                if not observed_text:
                    continue
                try:
                    observed_date = datetime.strptime(observed_text[:8], "%Y%m%d").date()
                except ValueError:
                    continue
                if observed_date <= requested_date:
                    safe_matching.append(row)
            matching = safe_matching
        if not matching:
            raise InvalidSymbolError(
                "Tushare fund table did not contain the requested symbol",
                **self._error_kwargs(request, provider_symbol=symbol),
            )
        normalized = _json_safe(matching)
        observed_value = None
        if observed_field:
            observed_value = max(
                str(row.get(observed_field) or row.get("end_date") or "")
                for row in matching
            )
        return self._response(
            request,
            instrument=instrument,
            value=normalized,
            unit="fund_records",
            normalized_payload={"symbol": symbol, "records": normalized},
            raw_row=matching,
            observed_value=observed_value,
            fetched_at=self._fetched_at(),
            provider_symbol=symbol,
            api_name=api_name,
            threshold=int(
                _setting(self.settings, "FINANCIAL_FUNDAMENTAL_FRESHNESS_SECONDS", 86400)
            ),
            default_time=wall_time(0, 0),
            lineage={"row_count": len(matching)},
        )

    def _fetch_financials(
        self, request: FinancialDataRequest
    ) -> FinancialProviderResponse:
        instrument = self._instrument(request)
        if instrument.asset_type != "equity":
            raise UnsupportedAssetError(
                "Tushare financial statements require an equity instrument",
                **self._error_kwargs(request, asset_type=instrument.asset_type),
            )
        statement = str(request.parameters.get("statement") or "").strip()
        if statement not in FINANCIAL_STATEMENTS:
            raise ValueError(
                "statement must be income, balancesheet, cashflow, or fina_indicator"
            )
        symbol = self._provider_symbol(request, instrument)
        parameters = {"ts_code": symbol}
        for key in ("start", "end"):
            if request.parameters.get(key):
                parameters[f"{key}_date"] = _date_parameter(
                    request.parameters[key], key
                )
        if request.parameters.get("period"):
            parameters["period"] = _date_parameter(
                request.parameters["period"], "period"
            )
        rows = self._checked_rows(
            self._invoke(statement, request, **parameters),
            request,
            api_name=statement,
            required_columns=("ts_code", "end_date"),
        )
        matching = [row for row in rows if str(row.get("ts_code")) == symbol]
        if not matching:
            raise InvalidSymbolError(
                "Tushare financial table did not contain the requested symbol",
                **self._error_kwargs(request, provider_symbol=symbol),
            )
        normalized = _json_safe(matching)
        announced = [str(row.get("ann_date")) for row in matching if row.get("ann_date")]
        observed_value = max(announced) if announced else None
        flags = () if announced else ("financial_announcement_timestamp_missing",)
        return self._response(
            request,
            instrument=instrument,
            value=normalized,
            unit="financial_statement_records",
            normalized_payload={
                "symbol": symbol,
                "statement": statement,
                "records": normalized,
            },
            raw_row=matching,
            observed_value=observed_value,
            fetched_at=self._fetched_at(),
            provider_symbol=symbol,
            api_name=statement,
            quality_flags=flags,
            threshold=int(
                _setting(self.settings, "FINANCIAL_FUNDAMENTAL_FRESHNESS_SECONDS", 86400)
            ),
            default_time=wall_time(0, 0),
            lineage={
                "row_count": len(matching),
                "lookahead_guard": "ann_date_used_instead_of_report_end_date",
            },
        )

    def _fetch_announcements(
        self, request: FinancialDataRequest
    ) -> FinancialProviderResponse:
        instrument = self._instrument(request)
        if instrument.asset_type != "equity":
            raise UnsupportedAssetError(
                "Tushare company announcements require an equity instrument",
                **self._error_kwargs(request, asset_type=instrument.asset_type),
            )
        symbol = self._provider_symbol(request, instrument)
        parameters = {"ts_code": symbol}
        if request.parameters.get("start"):
            parameters["start_date"] = _date_parameter(
                request.parameters["start"], "start"
            )
        if request.parameters.get("end"):
            parameters["end_date"] = _date_parameter(
                request.parameters["end"], "end"
            )
        api_name = "anns_d"
        rows = self._checked_rows(
            self._invoke(api_name, request, **parameters),
            request,
            api_name=api_name,
            required_columns=("ts_code", "ann_date", "title", "url"),
        )
        matching = [row for row in rows if str(row.get("ts_code")) == symbol]
        if not matching:
            raise InvalidSymbolError(
                "Tushare announcements did not contain the requested symbol",
                **self._error_kwargs(request, provider_symbol=symbol),
            )
        normalized = [
            {
                "ts_code": symbol,
                "name": row.get("name"),
                "title": row.get("title"),
                "url": row.get("url"),
                "ann_date": row.get("ann_date"),
                "published_at": row.get("rec_time"),
            }
            for row in matching
        ]
        observation_values = [
            str(row.get("rec_time") or row.get("ann_date") or "") for row in matching
        ]
        return self._response(
            request,
            instrument=instrument,
            value=normalized,
            unit="announcement_records",
            normalized_payload={"symbol": symbol, "announcements": normalized},
            raw_row=matching,
            observed_value=max(observation_values),
            fetched_at=self._fetched_at(),
            provider_symbol=symbol,
            api_name=api_name,
            quality_flags=("account_announcement_entitlement_required",),
            threshold=int(
                _setting(self.settings, "FINANCIAL_NEWS_FRESHNESS_SECONDS", 3600)
            ),
            default_time=wall_time(0, 0),
            lineage={"row_count": len(matching), "original_document_urls_retained": True},
        )

    def fetch(self, request: FinancialDataRequest) -> FinancialProviderResponse:
        self._require_enabled(request)
        expected_kind = ENDPOINT_KINDS.get(request.endpoint)
        if expected_kind is None:
            raise UnsupportedAssetError(
                f"unsupported Tushare logical endpoint: {request.endpoint}",
                **self._error_kwargs(request, supported_endpoints=sorted(ENDPOINT_KINDS)),
            )
        if request.data_kind != expected_kind:
            raise UnsupportedAssetError(
                "request data_kind does not match the logical endpoint",
                **self._error_kwargs(
                    request,
                    expected_data_kind=expected_kind.value,
                    actual_data_kind=request.data_kind.value,
                ),
            )
        handlers = {
            "instrument_master": self._fetch_instrument_master,
            "quote": self._fetch_quote,
            "bars": self._fetch_bars,
            "constituents": self._fetch_constituents,
            "fund": self._fetch_fund,
            "financials": self._fetch_financials,
            "announcements": self._fetch_announcements,
        }
        return handlers[request.endpoint](request)

    def _default_permission_summary(self) -> Mapping[str, object]:
        state = financial_capabilities(self.settings)
        if not state["tushare_token_configured"]:
            overall = "not_configured"
            token_status = "not_configured"
        elif not state["effective"]["tushare_cn"]:
            overall = "disabled"
            token_status = "configured_unverified"
        else:
            overall = "configured_unverified"
            token_status = "configured_unverified"
        return {
            "probe_version": "tushare-permissions-v1",
            "checked_at": None,
            "overall": overall,
            "token_status": token_status,
            "capabilities": {
                capability: "not_checked" for capability, _, _ in PERMISSION_PROBES
            },
        }

    def probe_permissions(
        self, *, request_id: str, requested_at: datetime
    ) -> Mapping[str, object]:
        requested_at = _aware_utc(requested_at, "requested_at")
        probe_instrument = self.instruments.get_by_canonical_symbol("000001.SZ")
        if probe_instrument is None:
            raise RuntimeError("controlled instrument seed is required for permission probe")
        state = financial_capabilities(self.settings)
        if not state["tushare_token_configured"] or not state["effective"]["tushare_cn"]:
            result = dict(self._default_permission_summary())
            result["checked_at"] = _utc_text(requested_at)
            self._last_permission_probe = result
            if self.connection is not None:
                self.ensure_profile()
            return result
        request = FinancialDataRequest(
            request_id=request_id,
            endpoint="instrument_master",
            instrument_id=str(probe_instrument.instrument_id),
            metric="permission_probe",
            data_kind=FinancialDataKind.FUNDAMENTAL,
            requested_as_of=requested_at,
            preferred_provider_id=self.provider_id,
        )
        statuses: Dict[str, str] = {}
        token_status = "valid"
        invalid_token = False
        for capability, api_name, parameters in PERMISSION_PROBES:
            if invalid_token:
                statuses[capability] = "not_checked_invalid_token"
                continue
            try:
                self._invoke(api_name, request, **parameters)
                statuses[capability] = "available"
            except PermissionDeniedError as exc:
                if exc.details.get("authentication") == "invalid_token":
                    statuses[capability] = "invalid_token"
                    token_status = "invalid"
                    invalid_token = True
                else:
                    statuses[capability] = "no_permission"
            except RateLimitedError:
                statuses[capability] = "rate_limited"
            except TemporarilyUnavailableError:
                statuses[capability] = "temporarily_unavailable"
        available_count = sum(value == "available" for value in statuses.values())
        denied_count = sum(value == "no_permission" for value in statuses.values())
        if invalid_token:
            overall = "invalid_token"
        elif available_count == len(PERMISSION_PROBES):
            overall = "available"
        elif available_count:
            overall = "partial"
        elif denied_count:
            overall = "no_permissions"
        else:
            overall = "unavailable"
        result = {
            "probe_version": "tushare-permissions-v1",
            "checked_at": _utc_text(self._fetched_at()),
            "overall": overall,
            "token_status": token_status,
            "capabilities": statuses,
        }
        self._last_permission_probe = result
        if self.connection is not None:
            self.ensure_profile()
        return result

    def ensure_profile(self) -> int:
        if self.connection is None:
            raise RuntimeError("database connection is required to register a provider profile")
        state = financial_capabilities(self.settings)
        permission_probe = self._last_permission_probe or self._default_permission_summary()
        health_by_overall = {
            "available": "healthy",
            "partial": "degraded",
            "no_permissions": "permission_denied",
            "invalid_token": "auth_failed",
            "not_configured": "not_configured",
            "disabled": "disabled",
            "configured_unverified": "unknown",
            "unavailable": "unavailable",
        }
        metadata = {
            "package_version_required": PROFILE["package_version"],
            "package_wheel_sha256": PROFILE["package_wheel_sha256"],
            "package_url": PROFILE["package_url"],
            "documentation_url": PROFILE["documentation_url"],
            "token_url": PROFILE["token_url"],
            "api_base_url": PROFILE["api_base_url"],
            "sdk_transport_override": PROFILE["sdk_transport_override"],
            "terms_note": PROFILE["terms_note"],
            "sla": PROFILE["sla"],
            "token_configured": state["tushare_token_configured"],
            "permission_probe": permission_probe,
        }
        health = health_by_overall.get(str(permission_probe.get("overall")), "unknown")
        self.connection.execute(
            """
            INSERT INTO financial_provider_profiles(
                provider_key, display_name, provider_type, access_tier,
                capabilities_json, priority, is_enabled, health_status,
                terms_url, attribution_text, metadata_json, last_health_check_at
            ) VALUES(?, ?, 'sdk_adapter', ?, ?, 10, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider_key) DO UPDATE SET
                display_name=excluded.display_name,
                access_tier=excluded.access_tier,
                capabilities_json=excluded.capabilities_json,
                is_enabled=excluded.is_enabled,
                health_status=excluded.health_status,
                terms_url=excluded.terms_url,
                attribution_text=excluded.attribution_text,
                metadata_json=excluded.metadata_json,
                last_health_check_at=excluded.last_health_check_at,
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (
                self.provider_id,
                PROFILE["display_name"],
                PROFILE["access_tier"],
                json.dumps([item.value for item in self.capabilities]),
                int(state["effective"]["tushare_cn"]),
                health,
                PROFILE["documentation_url"],
                "Data supplied by Tushare Pro; account entitlements and terms apply.",
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                permission_probe.get("checked_at"),
            ),
        )
        row = self.connection.execute(
            "SELECT id FROM financial_provider_profiles WHERE provider_key=?",
            (self.provider_id,),
        ).fetchone()
        return int(row[0])

    def persist_response(self, response: FinancialProviderResponse) -> Tuple[int, ...]:
        if self.connection is None:
            raise RuntimeError("database connection is required to persist provider data")
        profile_id = self.ensure_profile()
        snapshot_ids = []
        self.connection.execute("SAVEPOINT tushare_snapshot_write")
        try:
            for record in response.records:
                instrument_id = int(record.instrument_id)
                payload = {
                    "provider_id": response.provider_id,
                    "endpoint": response.endpoint,
                    "license_profile": response.license_profile,
                    "data_kind": response.data_kind.value,
                    "metric": record.metric,
                    "value": record.value,
                    "normalized_payload": record.normalized_payload,
                    "adjustment": record.adjustment.value,
                    "quality_flags": list(record.quality_flags),
                    "lineage": record.lineage,
                }
                payload_text = json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                payload_digest = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
                identity = "|".join(
                    (
                        response.provider_id,
                        response.endpoint,
                        record.instrument_id,
                        record.metric,
                        _utc_text(record.observed_at),
                        record.raw_response_hash,
                    )
                )
                snapshot_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                threshold = int(record.lineage.get("freshness_threshold_seconds") or 0)
                stale_after = (
                    _utc_text(record.observed_at + timedelta(seconds=threshold))
                    if threshold
                    else ""
                )
                self.connection.execute(
                    """
                    INSERT INTO financial_data_snapshots(
                        snapshot_key, instrument_id, provider_profile_id, data_type,
                        interval_code, observed_at, fetched_at, market_status,
                        currency, timezone, stale_after, quality_status, payload_json,
                        payload_sha256, source_url, request_id
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULLIF(?, ''), ?, ?, ?, ?, ?)
                    ON CONFLICT(snapshot_key) DO UPDATE SET
                        fetched_at=excluded.fetched_at,
                        quality_status=excluded.quality_status,
                        request_id=excluded.request_id
                    """,
                    (
                        snapshot_key,
                        instrument_id,
                        profile_id,
                        response.data_kind.value,
                        str(record.normalized_payload.get("interval") or ""),
                        _utc_text(record.observed_at),
                        _utc_text(record.fetched_at),
                        record.market_status.value,
                        record.currency,
                        record.timezone,
                        stale_after,
                        f"normalized_{record.freshness_state.value}",
                        payload_text,
                        payload_digest,
                        record.source_url,
                        response.request_id,
                    ),
                )
                snapshot_id = self.connection.execute(
                    "SELECT id FROM financial_data_snapshots WHERE snapshot_key=?",
                    (snapshot_key,),
                ).fetchone()[0]
                snapshot_ids.append(int(snapshot_id))
            self.connection.execute("RELEASE SAVEPOINT tushare_snapshot_write")
        except Exception:
            self.connection.execute("ROLLBACK TO SAVEPOINT tushare_snapshot_write")
            self.connection.execute("RELEASE SAVEPOINT tushare_snapshot_write")
            raise
        return tuple(snapshot_ids)

    def fetch_and_persist(self, request: FinancialDataRequest) -> FinancialProviderResponse:
        response = self.fetch_validated(request)
        self.persist_response(response)
        return response

    def health_probe(
        self, *, request_id: str, requested_at: datetime
    ) -> Mapping[str, object]:
        probe_instrument = self.instruments.get_by_canonical_symbol("000001.SZ")
        if probe_instrument is None:
            raise RuntimeError("controlled instrument seed is required for health probe")
        request = FinancialDataRequest(
            request_id=request_id,
            endpoint="instrument_master",
            instrument_id=str(probe_instrument.instrument_id),
            metric="instrument_master",
            data_kind=FinancialDataKind.FUNDAMENTAL,
            requested_as_of=requested_at,
            preferred_provider_id=self.provider_id,
        )
        try:
            response = self.fetch_validated(request)
            status = "healthy"
            error_type = None
            record_count = len(response.records)
        except PermissionDeniedError as exc:
            status = (
                "auth_failed"
                if exc.details.get("authentication") == "invalid_token"
                else "permission_denied"
            )
            error_type = exc.code.value
            record_count = 0
        except (RateLimitedError, TemporarilyUnavailableError) as exc:
            status = "degraded"
            error_type = exc.code.value
            record_count = 0
        result = {
            "provider_id": self.provider_id,
            "status": status,
            "sdk_version": self.sdk_version,
            "checked_at": _utc_text(self._fetched_at()),
            "record_count": record_count,
            "error_type": error_type,
        }
        if self.connection is not None:
            profile_id = self.ensure_profile()
            self.connection.execute(
                "UPDATE financial_provider_profiles SET health_status=?, "
                "last_health_check_at=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE id=?",
                (status, result["checked_at"], profile_id),
            )
        return result

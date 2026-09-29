#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""AKShare adapter normalized to the project's financial provider contract.

AKShare is an SDK over multiple upstream websites and has no data SLA.  This
adapter calls exactly one declared SDK endpoint per request, records lineage,
and never silently substitutes a different website or stale cache.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from datetime import date, datetime, time as wall_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import config
from financial_config import FinancialCapabilityDisabled, require_financial_capability
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
    / "akshare_cn.json"
)
PROFILE = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
PROVIDER_ID = "akshare_cn"
NORMALIZER_VERSION = "akshare-cn-v1"
MARKET_TIMEZONE = "Asia/Shanghai"
EASTMONEY_URL = "https://quote.eastmoney.com/"
TENCENT_URL = "https://gu.qq.com/"
CSINDEX_URL = "https://www.csindex.com.cn/"
SINA_INDEX_URL = "https://vip.stock.finance.sina.com.cn/mkt/#hs_s"
EASTMONEY_FUND_URL = "https://fund.eastmoney.com/"
ENDPOINT_KINDS = {
    "quote": FinancialDataKind.QUOTE,
    "bars": FinancialDataKind.BAR,
    "constituents": FinancialDataKind.CONSTITUENT,
    "industry": FinancialDataKind.FUNDAMENTAL,
    "market_breadth": FinancialDataKind.MACRO,
    "sector_rotation": FinancialDataKind.MACRO,
    "fund": FinancialDataKind.FUNDAMENTAL,
}


def _setting(settings, name: str, default):
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
            values = frame.to_dict(orient="records")
        except TypeError:
            values = frame.to_dict("records")
        return _records(values)
    raise ValueError(f"unsupported AKShare response type: {type(frame).__name__}")


def _code_text(value: object) -> str:
    text = str(value or "").strip()
    if len(text) == 8 and text[:2].casefold() in {"sh", "sz", "bj"} and text[2:].isdigit():
        text = text[2:]
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text.zfill(6) if text.isdigit() and len(text) <= 6 else text.upper()


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


class AKShareCNProvider(FinancialDataProvider):
    def __init__(
        self,
        *,
        instrument_registry: InstrumentRegistry,
        sdk=None,
        settings=None,
        market_clock: Optional[MarketClockService] = None,
        clock: Optional[Callable[[], datetime]] = None,
        connection=None,
    ):
        self.instruments = instrument_registry
        self._sdk = sdk
        self.settings = config if settings is None else settings
        self.market_clock = market_clock or MarketClockService()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.connection = connection

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
            FinancialDataKind.MACRO,
        )

    @property
    def sdk_version(self) -> str:
        sdk = self._sdk
        if sdk is None:
            return "not_loaded"
        return str(getattr(sdk, "__version__", "injected"))

    def _error_kwargs(self, request: FinancialDataRequest, **details):
        return {
            "provider_id": self.provider_id,
            "endpoint": request.endpoint,
            "request_id": request.request_id,
            "details": details,
        }

    def _require_enabled(self, request: FinancialDataRequest) -> None:
        try:
            require_financial_capability("akshare_cn", self.settings)
        except FinancialCapabilityDisabled as exc:
            raise PermissionDeniedError(
                "AKShare provider is disabled by the financial capability gate",
                **self._error_kwargs(request, gate_reason=exc.reason),
            ) from exc

    def _load_sdk(self, request: FinancialDataRequest):
        if self._sdk is not None:
            return self._sdk
        try:
            import akshare as akshare_sdk
        except ImportError as exc:
            raise TemporarilyUnavailableError(
                "AKShare dependency is not installed in this runtime",
                **self._error_kwargs(
                    request,
                    dependency="akshare",
                    required_version=PROFILE["package_version"],
                ),
            ) from exc
        self._sdk = akshare_sdk
        return self._sdk

    def _invoke(self, function_name: str, request: FinancialDataRequest, **parameters):
        sdk = self._load_sdk(request)
        function = getattr(sdk, function_name, None)
        if not callable(function):
            raise TemporarilyUnavailableError(
                f"AKShare endpoint is missing: {function_name}",
                **self._error_kwargs(
                    request,
                    sdk_version=self.sdk_version,
                    function=function_name,
                    failure="sdk_api_drift",
                ),
            )
        timeout_seconds = int(
            _setting(self.settings, "FINANCIAL_PROVIDER_TIMEOUT_SECONDS", 20)
        )
        state: Dict[str, object] = {}

        def invoke_sdk():
            try:
                state["result"] = function(**parameters)
            except BaseException as exc:  # transported to the request thread
                state["error"] = exc

        thread = threading.Thread(
            target=invoke_sdk,
            name=f"akshare-{function_name}",
            daemon=True,
        )
        thread.start()
        thread.join(timeout_seconds)
        if thread.is_alive():
            raise TemporarilyUnavailableError(
                "AKShare upstream request exceeded the hard timeout",
                **self._error_kwargs(
                    request,
                    function=function_name,
                    failure="hard_timeout",
                    timeout_seconds=timeout_seconds,
                ),
            )
        try:
            if "error" in state:
                raise state["error"]
            return state.get("result")
        except (PermissionDeniedError, RateLimitedError, TemporarilyUnavailableError):
            raise
        except Exception as exc:
            message = str(exc)
            lowered = message.casefold()
            details = self._error_kwargs(
                request,
                function=function_name,
                exception_type=type(exc).__name__,
            )
            if "429" in lowered or "rate limit" in lowered or "too many" in lowered:
                raise RateLimitedError("AKShare upstream rate limited the request", **details) from exc
            if "403" in lowered or "forbidden" in lowered:
                raise PermissionDeniedError("AKShare upstream denied the request", **details) from exc
            raise TemporarilyUnavailableError(
                "AKShare upstream request failed", **details
            ) from exc

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
                "AKShareCNProvider only accepts registered mainland instruments",
                **self._error_kwargs(
                    request, market=instrument.market, asset_type=instrument.asset_type
                ),
            )
        return instrument

    @staticmethod
    def _provider_symbol(instrument: InstrumentRecord) -> str:
        symbol = str(instrument.provider_mappings.get(PROVIDER_ID) or "").strip()
        if not symbol:
            raise ValueError("instrument has no akshare_cn provider mapping")
        return symbol

    def _checked_rows(
        self,
        frame,
        request: FinancialDataRequest,
        *,
        function_name: str,
        required_columns: Iterable[str],
    ) -> list[Dict[str, object]]:
        try:
            rows = _records(frame)
        except (TypeError, ValueError) as exc:
            raise TemporarilyUnavailableError(
                "AKShare returned an unsupported table shape",
                **self._error_kwargs(
                    request, function=function_name, failure="response_type_drift"
                ),
            ) from exc
        if not rows:
            raise TemporarilyUnavailableError(
                "AKShare returned an empty table",
                **self._error_kwargs(
                    request, function=function_name, failure="empty_response"
                ),
            )
        columns = {key for row in rows for key in row}
        missing = sorted(set(required_columns) - columns)
        if missing:
            raise TemporarilyUnavailableError(
                "AKShare response schema changed",
                **self._error_kwargs(
                    request,
                    function=function_name,
                    failure="field_drift",
                    missing_columns=missing,
                    observed_columns=sorted(columns),
                ),
            )
        return rows

    def _fetched_at(self) -> datetime:
        return _aware_utc(self._clock(), "provider clock")

    @staticmethod
    def _parse_observed(
        value,
        *,
        fetched_at: datetime,
        default_close: wall_time = wall_time(15, 0),
    ) -> Tuple[datetime, Tuple[str, ...], bool]:
        if value in (None, "", "-", "--"):
            return fetched_at, ("provider_timestamp_missing",), False
        zone = ZoneInfo(MARKET_TIMEZONE)
        parsed = None
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, date):
            parsed = datetime.combine(value, default_close)
        else:
            text = str(value).strip()
            if len(text) in {5, 8} and text.count(":") in {1, 2}:
                return fetched_at, ("provider_date_missing",), False
            if len(text) == 10:
                try:
                    parsed = datetime.combine(date.fromisoformat(text), default_close)
                except ValueError as exc:
                    raise ValueError(f"unparseable provider timestamp: {text}") from exc
            else:
                try:
                    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError(f"unparseable provider timestamp: {text}") from exc
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
        source_url: str,
        provider_symbol: str,
        adjustment: AdjustmentMode = AdjustmentMode.NOT_APPLICABLE,
        lineage: Optional[Mapping[str, object]] = None,
        quality_flags: Sequence[str] = (),
        default_close: wall_time = wall_time(15, 0),
        threshold: Optional[int] = None,
    ) -> FinancialProviderResponse:
        try:
            observed_at, time_flags, timestamp_available = self._parse_observed(
                observed_value, fetched_at=fetched_at, default_close=default_close
            )
        except ValueError as exc:
            raise TemporarilyUnavailableError(
                "AKShare returned an invalid observation timestamp",
                **self._error_kwargs(
                    request,
                    failure="invalid_timestamp",
                    function=(lineage or {}).get("sdk_function", ""),
                ),
            ) from exc
        if not timestamp_available and observed_at > request.requested_as_of:
            observed_at = request.requested_as_of
            time_flags = tuple(time_flags) + (
                "observed_at_bounded_by_requested_as_of",
            )
        freshness_threshold = int(
            threshold
            or _setting(
                self.settings,
                "FINANCIAL_QUOTE_FRESHNESS_SECONDS",
                300,
            )
        )
        flags = tuple(dict.fromkeys(tuple(quality_flags) + time_flags))
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
            quality_flags=flags,
            source_url=source_url,
            provider_symbol=provider_symbol,
            normalizer_version=NORMALIZER_VERSION,
            lineage={
                "sdk_version": self.sdk_version,
                "upstream_sla": "none",
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
        try:
            symbol = self._provider_symbol(instrument)
        except ValueError as exc:
            raise InvalidSymbolError(
                str(exc), **self._error_kwargs(request, instrument_id=instrument.instrument_id)
            ) from exc
        if instrument.asset_type not in {"equity", "etf", "index"}:
            raise UnsupportedAssetError(
                f"quote endpoint does not support {instrument.asset_type}",
                **self._error_kwargs(request, asset_type=instrument.asset_type),
            )
        if instrument.asset_type == "index":
            function = "stock_zh_index_spot_sina"
            rows = self._checked_rows(
                self._invoke(function, request),
                request,
                function_name=function,
                required_columns=("代码", "名称", "最新价"),
            )
            matches = [
                item
                for item in rows
                if _code_text(item.get("代码")) == _code_text(symbol)
            ]
            if len(matches) != 1:
                raise InvalidSymbolError(
                    "AKShare index quote table did not contain one exact symbol",
                    **self._error_kwargs(
                        request, provider_symbol=symbol, match_count=len(matches)
                    ),
                )
            row = matches[0]
            raw_rows = row
            invocation_symbol = symbol
            source_url = SINA_INDEX_URL
            price_field_mapping = {"最新价": "last_price", "涨跌幅": "change_percent"}
        elif instrument.asset_type == "equity":
            invocation_symbol = _code_text(symbol)
            function = "stock_zh_a_hist_tx"
            requested_local = request.requested_as_of.astimezone(
                ZoneInfo(MARKET_TIMEZONE)
            )
            start_local = requested_local - timedelta(days=14)
            rows = self._checked_rows(
                self._invoke(
                    function,
                    request,
                    symbol=invocation_symbol,
                    start_date=start_local.strftime("%Y%m%d"),
                    end_date=requested_local.strftime("%Y%m%d"),
                    adjust="",
                    timeout=int(
                        _setting(
                            self.settings, "FINANCIAL_PROVIDER_TIMEOUT_SECONDS", 20
                        )
                    ),
                ),
                request,
                function_name=function,
                required_columns=("date", "open", "close", "high", "low", "amount"),
            )
            latest = max(rows, key=lambda item: str(item.get("date") or ""))
            session_date = str(latest.get("date") or "")[:10] or None
            row = {
                "代码": invocation_symbol,
                "名称": instrument.display_name,
                "最新价": latest.get("close"),
                "成交量": latest.get("amount"),
                "最高": latest.get("high"),
                "最低": latest.get("low"),
                "今开": latest.get("open"),
                "数据日期": session_date,
            }
            raw_rows = rows
            source_url = TENCENT_URL
            price_field_mapping = {"close": "last_price", "date": "session_date"}
        else:
            invocation_symbol = _code_text(symbol)
            function = "fund_etf_hist_min_em"
            requested_local = request.requested_as_of.astimezone(
                ZoneInfo(MARKET_TIMEZONE)
            )
            start_local = requested_local - timedelta(days=7)
            rows = self._checked_rows(
                self._invoke(
                    function,
                    request,
                    symbol=invocation_symbol,
                    start_date=start_local.strftime("%Y-%m-%d 09:00:00"),
                    end_date=requested_local.strftime("%Y-%m-%d 15:30:00"),
                    period="1",
                    adjust="",
                ),
                request,
                function_name=function,
                required_columns=("时间", "开盘", "收盘", "最高", "最低", "成交量"),
            )
            latest = max(rows, key=lambda item: str(item.get("时间") or ""))
            row = {
                "代码": invocation_symbol,
                "名称": instrument.display_name,
                "最新价": latest.get("收盘"),
                "成交量": latest.get("成交量"),
                "成交额": latest.get("成交额"),
                "最高": latest.get("最高"),
                "最低": latest.get("最低"),
                "今开": latest.get("开盘"),
                "时间": latest.get("时间"),
            }
            raw_rows = rows
            source_url = EASTMONEY_URL
            price_field_mapping = {"收盘": "last_price", "时间": "observed_at"}
        open_field = "今开"
        try:
            last_price = _number(row.get("最新价"))
        except (TypeError, ValueError) as exc:
            raise TemporarilyUnavailableError(
                "AKShare quote has no usable latest price",
                **self._error_kwargs(request, function=function, failure="missing_price"),
            ) from exc
        normalized = {
            "symbol": symbol,
            "name": row.get("名称"),
            "last_price": last_price,
            "open": _number(row.get(open_field), allow_missing=True),
            "high": _number(row.get("最高"), allow_missing=True),
            "low": _number(row.get("最低"), allow_missing=True),
            "previous_close": _number(row.get("昨收"), allow_missing=True),
            "change": _number(row.get("涨跌额"), allow_missing=True),
            "change_percent": _number(row.get("涨跌幅"), allow_missing=True),
            "volume": _number(row.get("成交量"), allow_missing=True),
            "turnover": _number(row.get("成交额"), allow_missing=True),
        }
        observed_value = (
            row.get("更新时间")
            or row.get("时间")
            or row.get("数据日期")
            or row.get("时间戳")
        )
        fetched = self._fetched_at()
        value = normalized if request.metric == "quote" else last_price
        return self._response(
            request,
            instrument=instrument,
            value=value,
            unit="quote" if request.metric == "quote" else "price",
            normalized_payload=normalized,
            raw_row=raw_rows,
            observed_value=observed_value,
            fetched_at=fetched,
            source_url=source_url,
            provider_symbol=invocation_symbol,
            lineage={
                "sdk_function": function,
                "field_mapping": {
                    "代码": "symbol",
                    **price_field_mapping,
                },
                "registry_provider_symbol": symbol,
                "calculation": (
                    "direct_index_snapshot"
                    if instrument.asset_type == "index"
                    else (
                        "latest_daily_close_used_as_quote_for_research"
                        if instrument.asset_type == "equity"
                        else "latest_one_minute_close_used_as_quote_for_research"
                    )
                ),
            },
            quality_flags=(
                ("quote_derived_from_latest_daily_bar", "not_realtime_quote")
                if instrument.asset_type == "equity"
                else (
                    ("quote_derived_from_latest_minute_bar",)
                    if instrument.asset_type == "etf"
                    else ()
                )
            ),
        )

    def _fetch_bars(self, request: FinancialDataRequest) -> FinancialProviderResponse:
        instrument = self._instrument(request)
        try:
            symbol = self._provider_symbol(instrument)
        except ValueError as exc:
            raise InvalidSymbolError(
                str(exc), **self._error_kwargs(request, instrument_id=instrument.instrument_id)
            ) from exc
        interval = str(request.parameters.get("interval") or "1d").lower()
        start = _date_parameter(request.parameters.get("start"), "start")
        end = _date_parameter(request.parameters.get("end"), "end")
        requested_local_date = request.requested_as_of.astimezone(
            ZoneInfo(MARKET_TIMEZONE)
        ).date()
        if date.fromisoformat(end[:4] + "-" + end[4:6] + "-" + end[6:]) > requested_local_date:
            raise ValueError("bar end date cannot be later than requested_as_of")
        adjustment_text = str(request.parameters.get("adjustment") or "raw").lower()
        adjustment_map = {
            "raw": ("", AdjustmentMode.RAW),
            "qfq": ("qfq", AdjustmentMode.QFQ),
            "hfq": ("hfq", AdjustmentMode.HFQ),
        }
        if adjustment_text not in adjustment_map:
            raise ValueError("adjustment must be raw, qfq, or hfq")
        sdk_adjust, adjustment = adjustment_map[adjustment_text]
        timeout = int(_setting(self.settings, "FINANCIAL_PROVIDER_TIMEOUT_SECONDS", 20))
        minute = interval.endswith("m")
        if minute:
            period = interval[:-1]
            if period not in {"1", "5", "15", "30", "60"}:
                raise ValueError("minute interval must be 1m, 5m, 15m, 30m, or 60m")
            if instrument.asset_type == "equity":
                function = "stock_zh_a_hist_min_em"
            elif instrument.asset_type == "etf":
                function = "fund_etf_hist_min_em"
            else:
                raise UnsupportedAssetError(
                    "minute bars are supported for mainland equities and ETFs only",
                    **self._error_kwargs(request, asset_type=instrument.asset_type),
                )
            frame = self._invoke(
                function,
                request,
                symbol=symbol,
                start_date=f"{start[:4]}-{start[4:6]}-{start[6:]} 09:00:00",
                end_date=f"{end[:4]}-{end[4:6]}-{end[6:]} 15:30:00",
                period=period,
                adjust=sdk_adjust,
            )
            time_field = "时间"
        else:
            if interval not in {"1d", "1w", "1mo"}:
                raise ValueError("daily interval must be 1d, 1w, or 1mo")
            period = {"1d": "daily", "1w": "weekly", "1mo": "monthly"}[interval]
            if instrument.asset_type == "equity":
                function = "stock_zh_a_hist"
                parameters = dict(
                    symbol=symbol,
                    period=period,
                    start_date=start,
                    end_date=end,
                    adjust=sdk_adjust,
                    timeout=timeout,
                )
            elif instrument.asset_type == "etf":
                function = "fund_etf_hist_em"
                parameters = dict(
                    symbol=symbol,
                    period=period,
                    start_date=start,
                    end_date=end,
                    adjust=sdk_adjust,
                )
            elif instrument.asset_type == "index":
                if interval != "1d" or adjustment != AdjustmentMode.RAW:
                    raise UnsupportedAssetError(
                        "AKShare index bars support raw daily data only",
                        **self._error_kwargs(
                            request, interval=interval, adjustment=adjustment.value
                        ),
                    )
                function = "index_zh_a_hist"
                parameters = dict(
                    symbol=symbol,
                    period="daily",
                    start_date=start,
                    end_date=end,
                )
            else:
                raise UnsupportedAssetError(
                    f"bar endpoint does not support {instrument.asset_type}",
                    **self._error_kwargs(request, asset_type=instrument.asset_type),
                )
            frame = self._invoke(function, request, **parameters)
            time_field = "日期"
        rows = self._checked_rows(
            frame,
            request,
            function_name=function,
            required_columns=(time_field, "开盘", "收盘", "最高", "最低", "成交量"),
        )
        normalized_bars = []
        for row in rows:
            try:
                observed, _, _ = self._parse_observed(
                    row.get(time_field),
                    fetched_at=datetime.max.replace(tzinfo=timezone.utc),
                )
                normalized_bars.append(
                    {
                        "observed_at": _utc_text(observed),
                        "open": _number(row.get("开盘")),
                        "close": _number(row.get("收盘")),
                        "high": _number(row.get("最高")),
                        "low": _number(row.get("最低")),
                        "volume": _number(row.get("成交量"), allow_missing=True),
                        "turnover": _number(row.get("成交额"), allow_missing=True),
                    }
                )
            except (TypeError, ValueError) as exc:
                raise TemporarilyUnavailableError(
                    "AKShare bar row failed normalization",
                    **self._error_kwargs(
                        request, function=function, failure="invalid_bar_row"
                    ),
                ) from exc
        fetched = self._fetched_at()
        observed_value = rows[-1].get(time_field)
        return self._response(
            request,
            instrument=instrument,
            value=normalized_bars,
            unit="ohlcv_series",
            normalized_payload={
                "symbol": symbol,
                "interval": interval,
                "adjustment": adjustment.value,
                "bars": normalized_bars,
            },
            raw_row=rows,
            observed_value=observed_value,
            fetched_at=fetched,
            source_url=EASTMONEY_URL,
            provider_symbol=symbol,
            adjustment=adjustment,
            lineage={
                "sdk_function": function,
                "requested_start": start,
                "requested_end": end,
                "row_count": len(rows),
            },
            threshold=int(
                _setting(self.settings, "FINANCIAL_FUNDAMENTAL_FRESHNESS_SECONDS", 86400)
            ),
        )

    def _fetch_constituents(self, request: FinancialDataRequest) -> FinancialProviderResponse:
        instrument = self._instrument(request)
        if instrument.asset_type != "index":
            raise UnsupportedAssetError(
                "constituents require an index instrument",
                **self._error_kwargs(request, asset_type=instrument.asset_type),
            )
        symbol = self._provider_symbol(instrument)
        function = "index_stock_cons_weight_csindex"
        rows = self._checked_rows(
            self._invoke(function, request, symbol=symbol),
            request,
            function_name=function,
            required_columns=("日期", "成分券代码", "成分券名称", "交易所", "权重"),
        )
        members = []
        for row in rows:
            exchange_text = str(row.get("交易所") or "")
            exchange = "XSHG" if "上海" in exchange_text else "XSHE" if "深圳" in exchange_text else ""
            members.append(
                {
                    "provider_symbol": _code_text(row.get("成分券代码")),
                    "name": row.get("成分券名称"),
                    "exchange": exchange,
                    "weight_percent": _number(row.get("权重"), allow_missing=True),
                }
            )
        fetched = self._fetched_at()
        return self._response(
            request,
            instrument=instrument,
            value=members,
            unit="constituent_list",
            normalized_payload={"index_symbol": symbol, "members": members},
            raw_row=rows,
            observed_value=rows[0].get("日期"),
            fetched_at=fetched,
            source_url=CSINDEX_URL,
            provider_symbol=symbol,
            lineage={"sdk_function": function, "row_count": len(rows)},
            threshold=int(
                _setting(self.settings, "FINANCIAL_FUNDAMENTAL_FRESHNESS_SECONDS", 86400)
            ),
        )

    def _fetch_industry(self, request: FinancialDataRequest) -> FinancialProviderResponse:
        instrument = self._instrument(request)
        if instrument.asset_type != "equity":
            raise UnsupportedAssetError(
                "industry classification requires an equity instrument",
                **self._error_kwargs(request, asset_type=instrument.asset_type),
            )
        symbol = self._provider_symbol(instrument)
        function = "stock_individual_info_em"
        rows = self._checked_rows(
            self._invoke(
                function,
                request,
                symbol=symbol,
                timeout=int(_setting(self.settings, "FINANCIAL_PROVIDER_TIMEOUT_SECONDS", 20)),
            ),
            request,
            function_name=function,
            required_columns=("item", "value"),
        )
        values = {str(row.get("item") or "").strip(): row.get("value") for row in rows}
        industry = str(values.get("行业") or "").strip()
        if not industry:
            raise TemporarilyUnavailableError(
                "AKShare instrument profile omitted industry classification",
                **self._error_kwargs(request, function=function, failure="missing_industry"),
            )
        fetched = self._fetched_at()
        return self._response(
            request,
            instrument=instrument,
            value=industry,
            unit="classification",
            normalized_payload={"symbol": symbol, "industry": industry},
            raw_row=rows,
            observed_value=values.get("时间"),
            fetched_at=fetched,
            source_url=EASTMONEY_URL,
            provider_symbol=symbol,
            lineage={"sdk_function": function, "field_mapping": {"行业": "industry"}},
            threshold=int(
                _setting(self.settings, "FINANCIAL_FUNDAMENTAL_FRESHNESS_SECONDS", 86400)
            ),
        )

    @staticmethod
    def _row_exchange(code: str) -> str:
        raw_code = str(code or "").strip().casefold()
        if raw_code.startswith("sh"):
            return "XSHG"
        if raw_code.startswith("sz"):
            return "XSHE"
        if raw_code.startswith("bj"):
            return "OTHER"
        code = _code_text(raw_code)
        if code.startswith(("5", "6", "9")):
            return "XSHG"
        if code.startswith(("0", "2", "3")):
            return "XSHE"
        return "OTHER"

    def _fetch_market_breadth(self, request: FinancialDataRequest) -> FinancialProviderResponse:
        instrument = self._instrument(request)
        requested_exchange = str(
            request.parameters.get("exchange") or instrument.exchange or "ALL"
        ).upper()
        if requested_exchange not in {"XSHG", "XSHE", "ALL"}:
            raise ValueError("market breadth exchange must be XSHG, XSHE, or ALL")
        function = "stock_zh_a_spot"
        rows = self._checked_rows(
            self._invoke(function, request),
            request,
            function_name=function,
            required_columns=("代码", "涨跌幅"),
        )
        selected = [
            row
            for row in rows
            if requested_exchange == "ALL"
            or self._row_exchange(row.get("代码")) == requested_exchange
        ]
        if not selected:
            raise TemporarilyUnavailableError(
                "AKShare market breadth scope returned no securities",
                **self._error_kwargs(
                    request, function=function, exchange=requested_exchange
                ),
            )
        changes = [_number(row.get("涨跌幅"), allow_missing=True) for row in selected]
        usable = [value for value in changes if value is not None]
        if not usable:
            raise TemporarilyUnavailableError(
                "AKShare market breadth has no usable change percentages",
                **self._error_kwargs(request, function=function),
            )
        breadth = {
            "exchange": requested_exchange,
            "instrument_count": len(selected),
            "priced_count": len(usable),
            "advancing": sum(value > 0 for value in usable),
            "declining": sum(value < 0 for value in usable),
            "unchanged": sum(value == 0 for value in usable),
        }
        fetched = self._fetched_at()
        return self._response(
            request,
            instrument=instrument,
            value=breadth,
            unit="security_counts",
            normalized_payload=breadth,
            raw_row=selected,
            observed_value=None,
            fetched_at=fetched,
            source_url=EASTMONEY_URL,
            provider_symbol=requested_exchange,
            lineage={
                "sdk_function": function,
                "calculation": "count_by_change_percent_sign",
                "row_count": len(selected),
            },
            threshold=int(
                _setting(
                    self.settings,
                    "FINANCIAL_MARKET_BREADTH_FRESHNESS_SECONDS",
                    300,
                )
            ),
        )

    def _fetch_sector_rotation(self, request: FinancialDataRequest) -> FinancialProviderResponse:
        instrument = self._instrument(request)
        if instrument.market != "CN" or instrument.asset_type != "index":
            raise UnsupportedAssetError(
                "sector rotation currently supports mainland index context only",
                **self._error_kwargs(
                    request,
                    market=instrument.market,
                    asset_type=instrument.asset_type,
                ),
            )
        function = "stock_board_industry_name_em"
        rows = self._checked_rows(
            self._invoke(function, request),
            request,
            function_name=function,
            required_columns=("板块名称", "涨跌幅"),
        )
        sectors = []
        for row in rows:
            change_percent = _number(row.get("涨跌幅"), allow_missing=True)
            if change_percent is None:
                continue
            sectors.append(
                {
                    "sector_name": str(row.get("板块名称") or "").strip(),
                    "sector_code": _code_text(row.get("板块代码")),
                    "change_percent": change_percent,
                    "turnover_rate_percent": _number(
                        row.get("换手率"), allow_missing=True
                    ),
                    "advancing_count": _number(
                        row.get("上涨家数"), allow_missing=True
                    ),
                    "declining_count": _number(
                        row.get("下跌家数"), allow_missing=True
                    ),
                    "leader": str(row.get("领涨股票") or "").strip(),
                }
            )
        sectors = [item for item in sectors if item["sector_name"]]
        if not sectors:
            raise TemporarilyUnavailableError(
                "AKShare industry board table has no usable sector changes",
                **self._error_kwargs(request, function=function),
            )
        sectors.sort(key=lambda item: item["change_percent"], reverse=True)
        limit = int(request.parameters.get("limit") or 20)
        if not 1 <= limit <= 100:
            raise ValueError("sector rotation limit must be between 1 and 100")
        selected = sectors[:limit]
        payload = {
            "market": "CN",
            "ranking_basis": "same_snapshot_change_percent_descending",
            "sector_count": len(sectors),
            "sectors": selected,
        }
        fetched = self._fetched_at()
        return self._response(
            request,
            instrument=instrument,
            value=selected,
            unit="sector_performance_ranking",
            normalized_payload=payload,
            raw_row=rows,
            observed_value=None,
            fetched_at=fetched,
            source_url=EASTMONEY_URL,
            provider_symbol="CN_INDUSTRY_BOARDS",
            lineage={
                "sdk_function": function,
                "ranking_calculation": "sort_by_change_percent_descending",
                "source_row_count": len(rows),
                "returned_row_count": len(selected),
            },
            threshold=int(
                _setting(
                    self.settings,
                    "FINANCIAL_MARKET_BREADTH_FRESHNESS_SECONDS",
                    300,
                )
            ),
        )

    def _fetch_fund(self, request: FinancialDataRequest) -> FinancialProviderResponse:
        """Fetch one declared Eastmoney fund dataset without cross-site substitution."""
        instrument = self._instrument(request)
        if instrument.asset_type not in {"fund", "etf"}:
            raise UnsupportedAssetError(
                "AKShare fund endpoint requires a fund or ETF instrument",
                **self._error_kwargs(request, asset_type=instrument.asset_type),
            )
        try:
            symbol = _code_text(self._provider_symbol(instrument))
        except ValueError as exc:
            raise InvalidSymbolError(
                str(exc), **self._error_kwargs(request, instrument_id=instrument.instrument_id)
            ) from exc
        metric = str(request.metric or "")
        raw_rows = []
        observed_value = None
        quality_flags: tuple[str, ...] = ()
        lineage: Dict[str, object] = {}

        if metric == "fund_nav":
            start = _date_parameter(request.parameters.get("start"), "start")
            end = _date_parameter(request.parameters.get("end"), "end")
            cutoff_date = request.requested_as_of.astimezone(
                ZoneInfo(MARKET_TIMEZONE)
            ).date()
            if datetime.strptime(end, "%Y%m%d").date() > cutoff_date:
                raise ValueError("fund NAV end date cannot be later than requested_as_of")
            if instrument.asset_type == "etf":
                function = "fund_etf_fund_info_em"
                rows = self._checked_rows(
                    self._invoke(
                        function,
                        request,
                        fund=symbol,
                        start_date=start,
                        end_date=end,
                    ),
                    request,
                    function_name=function,
                    required_columns=("净值日期", "单位净值", "累计净值"),
                )
                normalized = [
                    {
                        "nav_date": str(row.get("净值日期") or ""),
                        "unit_nav": _number(row.get("单位净值"), allow_missing=True),
                        "accum_nav": _number(row.get("累计净值"), allow_missing=True),
                        "daily_growth_percent": _number(row.get("日增长率"), allow_missing=True),
                        "subscription_status": row.get("申购状态"),
                        "redemption_status": row.get("赎回状态"),
                    }
                    for row in rows
                    if start <= str(row.get("净值日期") or "").replace("-", "") <= end
                ]
            else:
                function = "fund_open_fund_info_em"
                rows = self._checked_rows(
                    self._invoke(
                        function,
                        request,
                        symbol=symbol,
                        indicator="单位净值走势",
                        period="成立来",
                    ),
                    request,
                    function_name=function,
                    required_columns=("净值日期", "单位净值"),
                )
                normalized = [
                    {
                        "nav_date": str(row.get("净值日期") or ""),
                        "unit_nav": _number(row.get("单位净值"), allow_missing=True),
                        "accum_nav": None,
                        "daily_growth_percent": _number(row.get("日增长率"), allow_missing=True),
                    }
                    for row in rows
                    if start <= str(row.get("净值日期") or "").replace("-", "") <= end
                ]
            if not normalized:
                raise TemporarilyUnavailableError(
                    "AKShare fund NAV has no rows inside the requested window",
                    **self._error_kwargs(request, function=function, failure="empty_window"),
                )
            raw_rows = rows
            observed_value = max(item["nav_date"] for item in normalized)
            unit = "fund_nav_records"
            quality_flags = ("valuation_data_not_intraday_quote",)
            lineage = {
                "sdk_function": function,
                "requested_start": start,
                "requested_end": end,
                "row_count": len(normalized),
            }
        elif metric == "fund_holdings":
            function = "fund_portfolio_hold_em"
            requested_year = str(request.parameters.get("year") or "").strip()
            if len(requested_year) != 4 or not requested_year.isdigit():
                raise ValueError("fund holdings year must be YYYY")
            rows = self._checked_rows(
                self._invoke(function, request, symbol=symbol, date=requested_year),
                request,
                function_name=function,
                required_columns=("股票代码", "股票名称", "占净值比例", "季度"),
            )
            normalized = [
                {
                    "holding_symbol": _code_text(row.get("股票代码")),
                    "holding_name": row.get("股票名称"),
                    "net_asset_ratio_percent": _number(row.get("占净值比例"), allow_missing=True),
                    "shares_10k": _number(row.get("持股数"), allow_missing=True),
                    "market_value_10k_cny": _number(row.get("持仓市值"), allow_missing=True),
                    "disclosure_period": row.get("季度"),
                    "published_at": None,
                }
                for row in rows
            ]
            raw_rows = rows
            unit = "fund_holding_disclosures"
            quality_flags = (
                "holdings_disclosure_lag_applies",
                "publication_timestamp_unavailable",
                "not_point_in_time_backtest_safe",
            )
            lineage = {
                "sdk_function": function,
                "requested_year": requested_year,
                "row_count": len(normalized),
                "point_in_time_backtest_safe": False,
            }
        elif metric == "fund_manager":
            function = "fund_manager_em"
            rows = self._checked_rows(
                self._invoke(function, request),
                request,
                function_name=function,
                required_columns=("姓名", "所属公司", "现任基金代码", "现任基金"),
            )
            matching = [row for row in rows if _code_text(row.get("现任基金代码")) == symbol]
            if not matching:
                raise InvalidSymbolError(
                    "AKShare manager table did not contain the requested fund",
                    **self._error_kwargs(request, provider_symbol=symbol),
                )
            normalized = [
                {
                    "manager_name": row.get("姓名"),
                    "management_company": row.get("所属公司"),
                    "fund_name": row.get("现任基金"),
                    "tenure_days": _number(row.get("累计从业时间"), allow_missing=True),
                    "current_assets_100m_cny": _number(row.get("现任基金资产总规模"), allow_missing=True),
                    "best_return_percent": _number(row.get("现任基金最佳回报"), allow_missing=True),
                    "effective_from": None,
                    "effective_to": None,
                }
                for row in matching
            ]
            raw_rows = matching
            unit = "current_fund_manager_records"
            quality_flags = ("current_manager_snapshot", "manager_history_unavailable")
            lineage = {"sdk_function": function, "row_count": len(normalized)}
        elif metric in {"fund_operating_fees", "fund_subscription_redemption"}:
            function = "fund_fee_em"
            indicator = "运作费用" if metric == "fund_operating_fees" else "交易状态"
            rows = self._checked_rows(
                self._invoke(function, request, symbol=symbol, indicator=indicator),
                request,
                function_name=function,
                required_columns=(),
            )
            normalized = _json_safe(rows)
            raw_rows = rows
            unit = "fund_fee_records" if metric == "fund_operating_fees" else "fund_trading_status_records"
            quality_flags = ("current_terms_snapshot", "effective_timestamp_unavailable")
            lineage = {
                "sdk_function": function,
                "indicator": indicator,
                "row_count": len(normalized),
            }
        else:
            raise UnsupportedAssetError(
                "AKShare does not implement the requested fund metric",
                **self._error_kwargs(request, metric=metric),
            )

        fetched = self._fetched_at()
        return self._response(
            request,
            instrument=instrument,
            value=normalized,
            unit=unit,
            normalized_payload={"symbol": symbol, "records": normalized},
            raw_row=raw_rows,
            observed_value=observed_value,
            fetched_at=fetched,
            source_url=EASTMONEY_FUND_URL,
            provider_symbol=symbol,
            lineage=lineage,
            quality_flags=quality_flags,
            default_close=wall_time(0, 0),
            threshold=int(
                _setting(self.settings, "FINANCIAL_FUNDAMENTAL_FRESHNESS_SECONDS", 86400)
            ),
        )

    def fetch(self, request: FinancialDataRequest) -> FinancialProviderResponse:
        self._require_enabled(request)
        expected_kind = ENDPOINT_KINDS.get(request.endpoint)
        if expected_kind is None:
            raise UnsupportedAssetError(
                f"unsupported AKShare logical endpoint: {request.endpoint}",
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
            "quote": self._fetch_quote,
            "bars": self._fetch_bars,
            "constituents": self._fetch_constituents,
            "industry": self._fetch_industry,
            "market_breadth": self._fetch_market_breadth,
            "sector_rotation": self._fetch_sector_rotation,
            "fund": self._fetch_fund,
        }
        return handlers[request.endpoint](request)

    def ensure_profile(self) -> int:
        if self.connection is None:
            raise RuntimeError("database connection is required to register a provider profile")
        try:
            require_financial_capability("akshare_cn", self.settings)
            enabled = 1
        except FinancialCapabilityDisabled:
            enabled = 0
        metadata = {
            "package_version_required": PROFILE["package_version"],
            "package_wheel_sha256": PROFILE["package_wheel_sha256"],
            "package_license_url": PROFILE["package_license_url"],
            "documentation_url": PROFILE["documentation_url"],
            "terms_note": PROFILE["terms_note"],
            "sla": PROFILE["sla"],
        }
        self.connection.execute(
            """
            INSERT INTO financial_provider_profiles(
                provider_key, display_name, provider_type, access_tier,
                capabilities_json, priority, is_enabled, health_status,
                terms_url, attribution_text, metadata_json
            ) VALUES(?, ?, 'sdk_adapter', ?, ?, 20, ?, 'unknown', ?, ?, ?)
            ON CONFLICT(provider_key) DO UPDATE SET
                display_name=excluded.display_name,
                access_tier=excluded.access_tier,
                capabilities_json=excluded.capabilities_json,
                is_enabled=excluded.is_enabled,
                terms_url=excluded.terms_url,
                attribution_text=excluded.attribution_text,
                metadata_json=excluded.metadata_json,
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (
                self.provider_id,
                PROFILE["display_name"],
                PROFILE["access_tier"],
                json.dumps([item.value for item in self.capabilities]),
                enabled,
                PROFILE["documentation_url"],
                "Data normalized through AKShare; upstream source terms apply.",
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
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
        self.connection.execute("SAVEPOINT akshare_snapshot_write")
        try:
            for record in response.records:
                try:
                    instrument_id = int(record.instrument_id)
                except ValueError as exc:
                    raise ValueError("snapshot instrument_id must be a registry integer") from exc
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
            self.connection.execute("RELEASE SAVEPOINT akshare_snapshot_write")
        except Exception:
            self.connection.execute("ROLLBACK TO SAVEPOINT akshare_snapshot_write")
            self.connection.execute("RELEASE SAVEPOINT akshare_snapshot_write")
            raise
        return tuple(snapshot_ids)

    def fetch_and_persist(self, request: FinancialDataRequest) -> FinancialProviderResponse:
        response = self.fetch_validated(request)
        self.persist_response(response)
        return response

    def health_probe(self, *, request_id: str, requested_at: datetime) -> Mapping[str, object]:
        probe_instrument = self.instruments.get_by_canonical_symbol("000001.SH")
        if probe_instrument is None:
            raise RuntimeError("controlled instrument seed is required for health probe")
        request = FinancialDataRequest(
            request_id=request_id,
            endpoint="quote",
            instrument_id=str(probe_instrument.instrument_id),
            metric="last_price",
            data_kind=FinancialDataKind.QUOTE,
            requested_as_of=requested_at,
            preferred_provider_id=self.provider_id,
        )
        try:
            response = self.fetch_validated(request)
            status = "healthy"
            error = None
        except Exception as exc:
            status = "unhealthy"
            response = None
            error = type(exc).__name__
        if self.connection is not None:
            profile_id = self.ensure_profile()
            self.connection.execute(
                """
                UPDATE financial_provider_profiles
                SET health_status=?, last_health_check_at=?,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id=?
                """,
                (status, _utc_text(requested_at), profile_id),
            )
        return {
            "provider_id": self.provider_id,
            "status": status,
            "sdk_version": self.sdk_version,
            "checked_at": _utc_text(requested_at),
            "record_count": len(response.records) if response else 0,
            "error_type": error,
        }

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Explicit A-share/HK data tools for the embedded TradingAgents graph.

The adapter never mutates upstream TradingAgents modules.  It creates a fresh
set of same-name LangChain tools that call the project's provider router,
instrument registry, existing article store and SQLite snapshot/evidence
tables.  External providers remain opt-in; every returned number carries a
snapshot id, and technical values are derived deterministically from persisted
OHLCV only.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from financial_evidence import DOCUMENT_KIND, STRUCTURED_KIND, EvidenceResolver
from financial_instruments import InstrumentRecord, InstrumentRegistry
from financial_provider_contract import (
    FinancialDataKind,
    FinancialDataRequest,
    FinancialProviderError,
)
from financial_provider_router import (
    EMBEDDED_PROVIDER_IDS,
    HARD_DISABLED_IDS,
    FinancialProviderRouter,
)
from financial_security import untrusted_external_content_policy


UTC = timezone.utc
ADAPTER_VERSION = "tradingagents-cn-data-v1"
DERIVED_PROVIDER_KEY = "tradingagents_cn_derived"
SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
SUPPORTED_TARGET_MARKETS = frozenset({"CN", "CN_FUND", "XHKG"})
SUPPORTED_INDICATORS = frozenset(
    {
        "close_50_sma",
        "close_200_sma",
        "close_10_ema",
        "macd",
        "macds",
        "macdh",
        "rsi",
        "boll",
        "boll_ub",
        "boll_lb",
        "atr",
        "vwma",
    }
)
MACRO_ALIASES = {
    "cpi": "CPIAUCSL.FRED",
    "cpiaucsl": "CPIAUCSL.FRED",
    "unemployment": "UNRATE.FRED",
    "unrate": "UNRATE.FRED",
    "fed_funds_rate": "DFF.FRED",
    "fed funds rate": "DFF.FRED",
    "dff": "DFF.FRED",
}

DEFAULT_PROVIDER_CHAINS: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    "CN": {
        "quote": ("tushare_cn", "akshare_cn", "easyquotation", "yahoo"),
        "bars": ("tushare_cn", "akshare_cn", "yahoo"),
        "financials": ("tushare_cn",),
        "industry": ("akshare_cn",),
        "announcements": ("tushare_cn",),
        "constituents": ("akshare_cn",),
        "market_breadth": ("akshare_cn",),
        "sector_rotation": ("akshare_cn",),
        "fund_basic": ("tushare_cn",),
        "fund_nav": ("tushare_cn", "akshare_cn"),
        "fund_holdings": ("tushare_cn", "akshare_cn"),
        "fund_manager": ("tushare_cn", "akshare_cn"),
        "fund_share": ("tushare_cn",),
        "fund_fees": ("akshare_cn",),
        "fund_trading": ("akshare_cn",),
    },
    "CN_FUND": {
        "quote": ("tushare_cn", "akshare_cn", "yahoo"),
        "bars": ("tushare_cn", "akshare_cn", "yahoo"),
        "fund_basic": ("tushare_cn",),
        "fund_nav": ("tushare_cn", "akshare_cn"),
        "fund_holdings": ("tushare_cn", "akshare_cn"),
        "fund_manager": ("tushare_cn", "akshare_cn"),
        "fund_share": ("tushare_cn",),
        "fund_fees": ("akshare_cn",),
        "fund_trading": ("akshare_cn",),
    },
    "XHKG": {
        "quote": ("easyquotation", "yahoo"),
        "bars": ("yahoo",),
        "constituents": (),
        "market_breadth": (),
        "sector_rotation": (),
    },
    "US": {
        "quote": ("yahoo", "alpha_vantage"),
        "bars": ("yahoo", "alpha_vantage"),
    },
    "JP": {
        "quote": ("yahoo",),
        "bars": ("yahoo",),
    },
    "MACRO": {"macro": ("fred",)},
}


class TradingAgentsCNDataError(RuntimeError):
    """Stable, non-secret data-boundary error."""

    def __init__(self, message: str, *, error_code: str):
        super().__init__(message)
        self.error_code = str(error_code)


@dataclass(frozen=True)
class TradingAgentsCNRunContext:
    research_run_id: str
    instrument_id: int
    server_now_utc: datetime
    cutoff_at_utc: Optional[datetime] = None
    allow_provider_fallback: bool = True
    provider_chains: Mapping[str, Sequence[str]] = field(default_factory=dict)
    max_tool_records: int = 512
    max_news_items: int = 30

    def __post_init__(self):
        if not SAFE_RUN_ID.fullmatch(str(self.research_run_id or "")):
            raise TradingAgentsCNDataError(
                "research_run_id 格式无效", error_code="invalid_data_request"
            )
        if isinstance(self.instrument_id, bool) or int(self.instrument_id) < 1:
            raise TradingAgentsCNDataError(
                "instrument_id 无效", error_code="invalid_data_request"
            )
        now = _aware_utc(self.server_now_utc, "server_now_utc")
        cutoff = _aware_utc(self.cutoff_at_utc or now, "cutoff_at_utc")
        if cutoff > now:
            raise TradingAgentsCNDataError(
                "cutoff_at_utc 不得晚于服务器时间", error_code="future_data_blocked"
            )
        if not 1 <= int(self.max_tool_records) <= 2000:
            raise TradingAgentsCNDataError(
                "max_tool_records 超出边界", error_code="invalid_data_request"
            )
        if not 1 <= int(self.max_news_items) <= 100:
            raise TradingAgentsCNDataError(
                "max_news_items 超出边界", error_code="invalid_data_request"
            )
        normalized: dict[str, tuple[str, ...]] = {}
        for key, values in dict(self.provider_chains or {}).items():
            chain = tuple(dict.fromkeys(str(value).strip() for value in values))
            if not str(key).strip() or not chain:
                raise TradingAgentsCNDataError(
                    "provider chain 无效", error_code="invalid_data_request"
                )
            unknown = set(chain) - set(EMBEDDED_PROVIDER_IDS)
            if unknown or set(chain) & set(HARD_DISABLED_IDS):
                raise TradingAgentsCNDataError(
                    "provider chain 包含未授权来源", error_code="provider_not_authorized"
                )
            normalized[str(key).strip()] = chain
        object.__setattr__(self, "server_now_utc", now)
        object.__setattr__(self, "cutoff_at_utc", cutoff)
        object.__setattr__(self, "provider_chains", normalized)


@dataclass(frozen=True)
class _FetchResult:
    status: str
    request_id: str
    endpoint: str
    metric: str
    preferred_provider_id: str
    actual_provider_id: str
    attempted_provider_ids: tuple[str, ...]
    degraded: bool
    degradation_reason: str
    snapshots: tuple[Mapping[str, Any], ...]
    error_code: str = ""


class _FallbackTool:
    """Host-test substitute for LangChain StructuredTool."""

    def __init__(self, name: str, description: str, func: Callable[..., str]):
        self.name = name
        self.description = description
        self.func = func
        self.args_schema = None

    def invoke(self, value: Any, config: Any = None) -> str:
        del config
        if isinstance(value, Mapping):
            return self.func(**dict(value))
        if isinstance(value, (tuple, list)):
            return self.func(*value)
        return self.func(value)


def _aware_utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TradingAgentsCNDataError(
            f"{label} 必须包含时区", error_code="invalid_data_request"
        )
    return value.astimezone(UTC)


def _utc_text(value: datetime) -> str:
    return _aware_utc(value, "datetime").isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _stored_datetime(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise TradingAgentsCNDataError(
            "数据快照时间无效", error_code="snapshot_integrity_failed"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _day(value: object, label: str) -> date:
    try:
        return date.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise TradingAgentsCNDataError(
            f"{label} 必须为 YYYY-MM-DD", error_code="invalid_data_request"
        ) from exc


def _finite(value: object) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _json_text(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def _tool(name: str, description: str, func: Callable[..., str]):
    try:
        from langchain_core.tools import StructuredTool
    except ImportError:
        return _FallbackTool(name, description, func)
    return StructuredTool.from_function(func=func, name=name, description=description)


def build_default_financial_provider_router(
    connection,
    *,
    settings,
    clock: Callable[[], datetime],
) -> FinancialProviderRouter:
    """Build lazy factories for existing embedded providers; no new service."""
    from financial_providers import (
        AKShareCNProvider,
        AlphaVantageProvider,
        EasyQuotationProvider,
        FREDProvider,
        PolymarketProvider,
        TushareCNProvider,
        YahooFinanceProvider,
    )

    instruments = InstrumentRegistry(connection)
    common = {
        "instrument_registry": instruments,
        "settings": settings,
        "connection": connection,
        "clock": clock,
    }
    return FinancialProviderRouter(
        settings=settings,
        clock=clock,
        factories={
            "akshare_cn": lambda: AKShareCNProvider(**common),
            "tushare_cn": lambda: TushareCNProvider(**common),
            "yahoo": lambda: YahooFinanceProvider(**common),
            "easyquotation": lambda: EasyQuotationProvider(**common),
            "alpha_vantage": lambda: AlphaVantageProvider(**common),
            "fred": lambda: FREDProvider(**common),
            "polymarket": lambda: PolymarketProvider(**common),
        },
    )


class TradingAgentsCNDataAdapter:
    """Project-owned tool registry and evidence-preserving data boundary."""

    def __init__(
        self,
        connection,
        run_context: TradingAgentsCNRunContext,
        *,
        settings,
        router: Optional[FinancialProviderRouter] = None,
    ):
        self.connection = connection
        self.context = run_context
        self.settings = settings
        self.instruments = InstrumentRegistry(connection)
        self.instrument = self.instruments.get(run_context.instrument_id)
        if self.instrument is None:
            raise TradingAgentsCNDataError(
                "研究标的不存在", error_code="instrument_not_found"
            )
        if self.instrument.market not in SUPPORTED_TARGET_MARKETS:
            raise TradingAgentsCNDataError(
                "TradingAgentsCNDataAdapter 仅接受 A 股、境内基金和港股",
                error_code="unsupported_market",
            )
        self._assert_run_scope()
        self.router = router or build_default_financial_provider_router(
            connection,
            settings=settings,
            clock=lambda: self.context.server_now_utc,
        )
        self._lock = threading.RLock()
        self._tools: Optional[dict[str, Any]] = None

    def _assert_run_scope(self) -> None:
        run = self.connection.execute(
            "SELECT scope_type, instrument_id, universe_id FROM financial_research_runs WHERE id=?",
            (self.context.research_run_id,),
        ).fetchone()
        if run is None:
            raise TradingAgentsCNDataError(
                "金融研究任务不存在", error_code="research_run_not_found"
            )
        scope_type, run_instrument_id, universe_id = run
        if run_instrument_id is not None and int(run_instrument_id) != int(
            self.context.instrument_id
        ):
            raise TradingAgentsCNDataError(
                "工具标的超出研究任务范围", error_code="instrument_scope_mismatch"
            )
        if universe_id is not None:
            local_day = self.context.cutoff_at_utc.astimezone(
                ZoneInfo(self._timezone_name(self.instrument))
            ).date().isoformat()
            member = self.connection.execute(
                """
                SELECT 1 FROM financial_universe_members
                WHERE universe_id=? AND instrument_id=?
                  AND (effective_from='' OR effective_from<=?)
                  AND (effective_to IS NULL OR effective_to>?)
                """,
                (int(universe_id), self.context.instrument_id, local_day, local_day),
            ).fetchone()
            if member is None:
                raise TradingAgentsCNDataError(
                    "工具标的不属于研究 Universe", error_code="instrument_scope_mismatch"
                )
        if run_instrument_id is None and universe_id is None and str(scope_type) != "market":
            raise TradingAgentsCNDataError(
                "研究任务没有有效标的范围", error_code="instrument_scope_mismatch"
            )

    @staticmethod
    def _timezone_name(instrument: InstrumentRecord) -> str:
        if instrument.market == "XHKG":
            return "Asia/Hong_Kong"
        if instrument.market in {"CN", "CN_FUND"}:
            return "Asia/Shanghai"
        return "UTC"

    def _validate_symbol(self, symbol: str) -> InstrumentRecord:
        local_as_of = self.context.cutoff_at_utc.astimezone(
            ZoneInfo(self._timezone_name(self.instrument))
        ).date()
        try:
            resolution = self.instruments.resolve(
                str(symbol or "").strip(),
                as_of=local_as_of,
            )
        except ValueError as exc:
            raise TradingAgentsCNDataError(
                "工具股票代码无效", error_code="invalid_symbol"
            ) from exc
        if resolution.status != "resolved" or resolution.instrument_id != self.instrument.instrument_id:
            code = "ambiguous_symbol" if resolution.status == "ambiguous" else "instrument_scope_mismatch"
            raise TradingAgentsCNDataError(
                "工具股票代码未唯一匹配当前研究标的", error_code=code
            )
        return self.instrument

    def _validate_window(self, start_value: object, end_value: object) -> tuple[date, date]:
        start = _day(start_value, "start_date")
        end = _day(end_value, "end_date")
        if start > end:
            raise TradingAgentsCNDataError(
                "开始日期不得晚于结束日期", error_code="invalid_data_request"
            )
        cutoff_day = self.context.cutoff_at_utc.astimezone(
            ZoneInfo(self._timezone_name(self.instrument))
        ).date()
        if end > cutoff_day:
            raise TradingAgentsCNDataError(
                "工具请求包含服务器时点之后的数据", error_code="future_data_blocked"
            )
        if (end - start).days > 3660:
            raise TradingAgentsCNDataError(
                "工具时间窗口超过十年边界", error_code="invalid_data_request"
            )
        return start, end

    def _chain(self, key: str, instrument: InstrumentRecord) -> tuple[str, ...]:
        override = self.context.provider_chains.get(f"{instrument.market}.{key}")
        if override is None:
            override = self.context.provider_chains.get(key)
        default = DEFAULT_PROVIDER_CHAINS.get(instrument.market, {}).get(key, ())
        chain = tuple(override or default)
        allowed = {
            provider
            for providers in DEFAULT_PROVIDER_CHAINS.get(instrument.market, {}).values()
            for provider in providers
        }
        if set(chain) - allowed:
            raise TradingAgentsCNDataError(
                "所选 Provider 不支持当前市场或工具", error_code="provider_not_authorized"
            )
        return chain

    def _request_id(
        self,
        tool_name: str,
        target: InstrumentRecord,
        endpoint: str,
        metric: str,
        parameters: Mapping[str, Any],
    ) -> str:
        identity = {
            "adapter": ADAPTER_VERSION,
            "run": self.context.research_run_id,
            "tool": tool_name,
            "instrument_id": target.instrument_id,
            "endpoint": endpoint,
            "metric": metric,
            "parameters": dict(parameters),
            "cutoff_at": _utc_text(self.context.cutoff_at_utc),
        }
        return f"ta-cn-{_digest(identity)[:40]}"

    def _load_snapshots(
        self,
        *,
        snapshot_ids: Sequence[int] = (),
        request_id: str = "",
    ) -> tuple[Mapping[str, Any], ...]:
        if snapshot_ids:
            placeholders = ",".join("?" for _ in snapshot_ids)
            where = f"s.id IN ({placeholders})"
            parameters: tuple[Any, ...] = tuple(int(value) for value in snapshot_ids)
        elif request_id:
            where = "s.request_id=?"
            parameters = (request_id,)
        else:
            return ()
        rows = self.connection.execute(
            f"""
            SELECT s.id, s.instrument_id, s.data_type, s.interval_code,
                   s.observed_at, s.fetched_at, s.market_status, s.currency,
                   s.timezone, s.stale_after, s.quality_status, s.payload_json,
                   s.payload_sha256, s.source_url, s.request_id, p.provider_key
            FROM financial_data_snapshots s
            JOIN financial_provider_profiles p ON p.id=s.provider_profile_id
            WHERE {where}
            ORDER BY s.observed_at, s.id
            """,
            parameters,
        ).fetchall()
        snapshots = []
        for row in rows:
            payload_text = str(row[11])
            if hashlib.sha256(payload_text.encode("utf-8")).hexdigest() != str(row[12]):
                raise TradingAgentsCNDataError(
                    "数据快照 hash 校验失败", error_code="snapshot_integrity_failed"
                )
            observed = _stored_datetime(row[4])
            if observed > self.context.cutoff_at_utc:
                raise TradingAgentsCNDataError(
                    "数据快照晚于研究截止时点", error_code="future_data_blocked"
                )
            fetched = _stored_datetime(row[5])
            if fetched > self.context.server_now_utc:
                raise TradingAgentsCNDataError(
                    "数据快照聚合时间晚于服务器时间", error_code="future_data_blocked"
                )
            try:
                payload = json.loads(payload_text)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise TradingAgentsCNDataError(
                    "数据快照 JSON 无效", error_code="snapshot_integrity_failed"
                ) from exc
            snapshots.append(
                {
                    "snapshot_id": int(row[0]),
                    "instrument_id": int(row[1]) if row[1] is not None else None,
                    "data_type": str(row[2]),
                    "interval": str(row[3] or ""),
                    "observed_at": _utc_text(observed),
                    "fetched_at": _utc_text(fetched),
                    "market_status": str(row[6]),
                    "currency": str(row[7]),
                    "timezone": str(row[8]),
                    "stale_after": str(row[9] or ""),
                    "quality_status": str(row[10]),
                    "payload": payload,
                    "payload_sha256": str(row[12]),
                    "source_url": str(row[13] or ""),
                    "request_id": str(row[14]),
                    "provider_id": str(row[15]),
                }
            )
        return tuple(snapshots)

    def _pin_snapshots(
        self,
        snapshots: Sequence[Mapping[str, Any]],
        *,
        tool_name: str,
        request_id: str,
        preferred_provider_id: str,
        attempted_provider_ids: Sequence[str],
        degraded: bool,
    ) -> None:
        for snapshot in snapshots:
            snapshot_id = int(snapshot["snapshot_id"])
            instrument_id = int(snapshot["instrument_id"])
            metadata = {
                "adapter": ADAPTER_VERSION,
                "tool_name": tool_name,
                "request_id": request_id,
                "provider_id": snapshot["provider_id"],
                "preferred_provider_id": preferred_provider_id,
                "attempted_provider_ids": list(attempted_provider_ids),
                "degraded": bool(degraded),
                "payload_copied": False,
                "all_numeric_claims_require_snapshot_id": True,
            }
            self.connection.execute(
                """
                INSERT INTO financial_research_evidence(
                    research_run_id, evidence_key, evidence_kind, snapshot_id,
                    article_id, instrument_id, universe_id, evidence_role,
                    match_method, match_score, observed_at, metadata_json
                ) VALUES(?, ?, 'structured_snapshot', ?, NULL, ?, NULL,
                         'tradingagents_registered_tool', 'direct_tool_output',
                         1.0, ?, ?)
                ON CONFLICT(research_run_id, evidence_key) DO UPDATE SET
                    evidence_role=excluded.evidence_role,
                    match_method=excluded.match_method,
                    observed_at=excluded.observed_at,
                    metadata_json=excluded.metadata_json,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (
                    self.context.research_run_id,
                    f"snapshot:{snapshot_id}:instrument:{instrument_id}",
                    snapshot_id,
                    instrument_id,
                    snapshot["observed_at"],
                    _json_text(metadata),
                ),
            )

    def _validate_tool_snapshots(
        self,
        snapshots: Sequence[Mapping[str, Any]],
        *,
        target: InstrumentRecord,
    ) -> None:
        normalized_record_count = 0
        for snapshot in snapshots:
            if int(snapshot.get("instrument_id") or 0) != int(target.instrument_id):
                raise TradingAgentsCNDataError(
                    "Provider 快照标的与工具请求不一致",
                    error_code="snapshot_integrity_failed",
                )
            normalized = snapshot["payload"].get("normalized_payload") or {}
            nested = None
            for field_name in (
                "bars",
                "records",
                "announcements",
                "constituents",
                "members",
                "sectors",
            ):
                if isinstance(normalized.get(field_name), list):
                    nested = normalized[field_name]
                    break
            normalized_record_count += len(nested) if nested is not None else 1
        if normalized_record_count > self.context.max_tool_records:
            raise TradingAgentsCNDataError(
                "Provider 返回记录数超过工具边界，未静默截断",
                error_code="tool_record_limit",
            )

    def _fetch_provider(
        self,
        *,
        tool_name: str,
        target: InstrumentRecord,
        chain_key: str,
        endpoint: str,
        metric: str,
        data_kind: FinancialDataKind,
        parameters: Mapping[str, Any],
    ) -> _FetchResult:
        chain = self._chain(chain_key, target)
        request_id = self._request_id(
            tool_name, target, endpoint, metric, parameters
        )
        cached = self._load_snapshots(request_id=request_id)
        if cached:
            self._validate_tool_snapshots(cached, target=target)
            provider_ids = tuple(dict.fromkeys(item["provider_id"] for item in cached))
            cached_degraded = bool(chain and provider_ids[0] != chain[0])
            self._pin_snapshots(
                cached,
                tool_name=tool_name,
                request_id=request_id,
                preferred_provider_id=chain[0] if chain else "",
                attempted_provider_ids=provider_ids,
                degraded=cached_degraded,
            )
            return _FetchResult(
                "cached",
                request_id,
                endpoint,
                metric,
                chain[0] if chain else "",
                provider_ids[0],
                provider_ids,
                cached_degraded,
                "cached_prior_fallback" if cached_degraded else "",
                cached,
            )
        if not chain:
            return _FetchResult(
                "unavailable",
                request_id,
                endpoint,
                metric,
                "",
                "",
                (),
                False,
                "",
                (),
                "no_authorized_provider_for_market",
            )
        request = FinancialDataRequest(
            request_id=request_id,
            endpoint=endpoint,
            instrument_id=str(target.instrument_id),
            metric=metric,
            data_kind=data_kind,
            requested_as_of=self.context.cutoff_at_utc,
            preferred_provider_id=chain[0],
            parameters=dict(parameters),
        )
        try:
            response, snapshot_ids = self.router.fetch_and_persist(
                request,
                candidate_provider_ids=chain,
                allow_fallback=self.context.allow_provider_fallback,
            )
        except FinancialProviderError as exc:
            return _FetchResult(
                "unavailable",
                request_id,
                endpoint,
                metric,
                chain[0],
                "",
                chain,
                False,
                "",
                (),
                exc.code.value,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            raise TradingAgentsCNDataError(
                "Provider 输出未通过项目数据边界",
                error_code="provider_contract_failed",
            ) from exc
        snapshots = self._load_snapshots(snapshot_ids=snapshot_ids)
        if len(snapshots) != len(snapshot_ids):
            raise TradingAgentsCNDataError(
                "Provider 快照持久化不完整", error_code="snapshot_integrity_failed"
            )
        self._validate_tool_snapshots(snapshots, target=target)
        degradation = response.degradation
        attempted = (
            degradation.attempted_provider_ids
            if degradation.degraded
            else (response.provider_id,)
        )
        self._pin_snapshots(
            snapshots,
            tool_name=tool_name,
            request_id=request_id,
            preferred_provider_id=chain[0],
            attempted_provider_ids=attempted,
            degraded=degradation.degraded,
        )
        return _FetchResult(
            "fetched",
            request_id,
            endpoint,
            metric,
            chain[0],
            response.provider_id,
            tuple(attempted),
            degradation.degraded,
            degradation.reason,
            snapshots,
        )

    def _fetch_envelope(self, tool_name: str, result: _FetchResult) -> Mapping[str, Any]:
        return {
            "schema_version": 1,
            "adapter": ADAPTER_VERSION,
            "tool": tool_name,
            "status": result.status,
            "target": {
                "instrument_id": self.instrument.instrument_id,
                "canonical_symbol": self.instrument.canonical_symbol,
                "display_name": self.instrument.display_name,
                "market": self.instrument.market,
                "asset_type": self.instrument.asset_type,
            },
            "server_now_utc": _utc_text(self.context.server_now_utc),
            "cutoff_at_utc": _utc_text(self.context.cutoff_at_utc),
            "request_id": result.request_id,
            "endpoint": result.endpoint,
            "metric": result.metric,
            "preferred_provider_id": result.preferred_provider_id,
            "actual_provider_id": result.actual_provider_id,
            "attempted_provider_ids": list(result.attempted_provider_ids),
            "degraded": result.degraded,
            "degradation_reason": result.degradation_reason,
            "error_code": result.error_code or None,
            "snapshots": list(result.snapshots),
            "citation_rule": "Every numeric claim must cite snapshot_id and observed_at.",
        }

    def get_stock_data(self, symbol: str, start_date: str, end_date: str) -> str:
        with self._lock:
            target = self._validate_symbol(symbol)
            start, end = self._validate_window(start_date, end_date)
            if (end - start).days > self.context.max_tool_records * 3:
                raise TradingAgentsCNDataError(
                    "OHLCV 请求可能超过工具记录上限，请缩短时间窗口",
                    error_code="tool_record_limit",
                )
            result = self._fetch_provider(
                tool_name="get_stock_data",
                target=target,
                chain_key="bars",
                endpoint="bars",
                metric="ohlcv",
                data_kind=FinancialDataKind.BAR,
                parameters={
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "interval": "1d",
                    "adjustment": "raw",
                },
            )
            return _json_text(self._fetch_envelope("get_stock_data", result))

    @staticmethod
    def _bar_timestamp(value: object, timezone_name: str) -> datetime:
        raw = str(value or "").strip()
        if re.fullmatch(r"\d{8}", raw):
            raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise TradingAgentsCNDataError(
                "OHLCV 行时间无效", error_code="snapshot_integrity_failed"
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
        return parsed.astimezone(UTC)

    def _stored_bars(
        self,
        end_day: date,
        *,
        look_back_days: int,
        target: Optional[InstrumentRecord] = None,
    ) -> list[dict[str, Any]]:
        target_record = target or self.instrument
        rows = self.connection.execute(
            """
            SELECT s.id, s.observed_at, s.payload_json, s.payload_sha256,
                   p.provider_key
            FROM financial_data_snapshots s
            JOIN financial_provider_profiles p ON p.id=s.provider_profile_id
            WHERE s.instrument_id=? AND s.data_type='bar'
            ORDER BY s.observed_at DESC, s.id DESC
            """,
            (target_record.instrument_id,),
        ).fetchall()
        timezone_name = self._timezone_name(target_record)
        selected: dict[str, dict[str, Any]] = {}
        source_ids: dict[str, int] = {}
        earliest = end_day - timedelta(days=max(look_back_days * 3, 30))
        for snapshot_id, observed_at, payload_text, payload_sha, _provider in rows:
            if hashlib.sha256(str(payload_text).encode("utf-8")).hexdigest() != str(payload_sha):
                raise TradingAgentsCNDataError(
                    "OHLCV 快照 hash 校验失败", error_code="snapshot_integrity_failed"
                )
            payload = json.loads(str(payload_text))
            normalized = payload.get("normalized_payload") or {}
            bars = normalized.get("bars")
            if not isinstance(bars, list):
                bars = [
                    {
                        "observed_at": observed_at,
                        "open": normalized.get("open"),
                        "high": normalized.get("high"),
                        "low": normalized.get("low"),
                        "close": normalized.get("close"),
                        "volume": normalized.get("volume"),
                        "turnover": normalized.get("turnover"),
                    }
                ]
            for raw in bars:
                if not isinstance(raw, Mapping):
                    continue
                timestamp = self._bar_timestamp(
                    raw.get("observed_at") or raw.get("time") or observed_at,
                    timezone_name,
                )
                local_day = timestamp.astimezone(ZoneInfo(timezone_name)).date()
                if local_day > end_day or timestamp > self.context.cutoff_at_utc:
                    continue
                if local_day < earliest:
                    continue
                normalized_row = {
                    "observed_at": _utc_text(timestamp),
                    "open": _finite(raw.get("open")),
                    "high": _finite(raw.get("high")),
                    "low": _finite(raw.get("low")),
                    "close": _finite(raw.get("close")),
                    "volume": _finite(raw.get("volume")),
                    "turnover": _finite(raw.get("turnover")),
                    "source_snapshot_id": int(snapshot_id),
                }
                if any(normalized_row[key] is None for key in ("open", "high", "low", "close")):
                    continue
                key = normalized_row["observed_at"]
                if key not in selected or int(snapshot_id) > source_ids[key]:
                    selected[key] = normalized_row
                    source_ids[key] = int(snapshot_id)
        result = [selected[key] for key in sorted(selected)]
        return result[-min(self.context.max_tool_records, max(look_back_days, 1)) :]

    @staticmethod
    def _ema(values: Sequence[float], period: int) -> list[float]:
        alpha = 2.0 / (period + 1.0)
        result = [float(values[0])]
        for value in values[1:]:
            result.append(alpha * float(value) + (1.0 - alpha) * result[-1])
        return result

    @classmethod
    def _indicator_value(cls, indicator: str, bars: Sequence[Mapping[str, Any]]) -> tuple[Optional[float], int]:
        closes = [float(row["close"]) for row in bars]
        if not closes:
            return None, 1
        if indicator == "close_50_sma":
            return (sum(closes[-50:]) / 50, 50) if len(closes) >= 50 else (None, 50)
        if indicator == "close_200_sma":
            return (sum(closes[-200:]) / 200, 200) if len(closes) >= 200 else (None, 200)
        if indicator == "close_10_ema":
            return (cls._ema(closes, 10)[-1], 10) if len(closes) >= 10 else (None, 10)
        if indicator in {"macd", "macds", "macdh"}:
            if len(closes) < 35:
                return None, 35
            fast = cls._ema(closes, 12)
            slow = cls._ema(closes, 26)
            line = [left - right for left, right in zip(fast, slow)]
            signal = cls._ema(line, 9)
            values = {"macd": line[-1], "macds": signal[-1], "macdh": line[-1] - signal[-1]}
            return values[indicator], 35
        if indicator == "rsi":
            if len(closes) < 15:
                return None, 15
            changes = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
            gains = [max(value, 0.0) for value in changes[-14:]]
            losses = [max(-value, 0.0) for value in changes[-14:]]
            average_gain = sum(gains) / 14
            average_loss = sum(losses) / 14
            if average_loss == 0:
                return 100.0, 15
            rs = average_gain / average_loss
            return 100.0 - (100.0 / (1.0 + rs)), 15
        if indicator in {"boll", "boll_ub", "boll_lb"}:
            if len(closes) < 20:
                return None, 20
            window = closes[-20:]
            middle = sum(window) / 20
            deviation = statistics.pstdev(window)
            values = {"boll": middle, "boll_ub": middle + 2 * deviation, "boll_lb": middle - 2 * deviation}
            return values[indicator], 20
        if indicator == "atr":
            if len(bars) < 15:
                return None, 15
            ranges = []
            for index in range(len(bars) - 14, len(bars)):
                row = bars[index]
                previous_close = float(bars[index - 1]["close"])
                ranges.append(
                    max(
                        float(row["high"]) - float(row["low"]),
                        abs(float(row["high"]) - previous_close),
                        abs(float(row["low"]) - previous_close),
                    )
                )
            return sum(ranges) / 14, 15
        if indicator == "vwma":
            if len(bars) < 20:
                return None, 20
            window = bars[-20:]
            volumes = [row.get("volume") for row in window]
            if any(value is None for value in volumes) or sum(float(value) for value in volumes) <= 0:
                return None, 20
            denominator = sum(float(value) for value in volumes)
            numerator = sum(float(row["close"]) * float(row["volume"]) for row in window)
            return numerator / denominator, 20
        raise TradingAgentsCNDataError(
            "不支持的技术指标", error_code="unsupported_indicator"
        )

    def _ensure_derived_profile(self) -> int:
        self.connection.execute(
            """
            INSERT INTO financial_provider_profiles(
                provider_key, display_name, provider_type, access_tier,
                capabilities_json, priority, is_enabled, health_status,
                attribution_text, metadata_json
            ) VALUES(?, 'TradingAgents 本地确定性派生', 'internal_derived', 'internal',
                     '["bar"]', 0, 1, 'healthy',
                     'Derived only from persisted project snapshots.', ?)
            ON CONFLICT(provider_key) DO UPDATE SET
                display_name=excluded.display_name,
                is_enabled=1,
                health_status='healthy',
                metadata_json=excluded.metadata_json,
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (
                DERIVED_PROVIDER_KEY,
                _json_text(
                    {
                        "adapter_version": ADAPTER_VERSION,
                        "external_network_calls": 0,
                        "source_requirement": "persisted_ohlcv_snapshots",
                    }
                ),
            ),
        )
        return int(
            self.connection.execute(
                "SELECT id FROM financial_provider_profiles WHERE provider_key=?",
                (DERIVED_PROVIDER_KEY,),
            ).fetchone()[0]
        )

    def _persist_derived(
        self,
        *,
        tool_name: str,
        data_type: str,
        observed_at: datetime,
        payload: Mapping[str, Any],
        request_id: str,
        quality_status: str,
        target: Optional[InstrumentRecord] = None,
    ) -> Mapping[str, Any]:
        target_record = target or self.instrument
        profile_id = self._ensure_derived_profile()
        payload_text = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload_sha = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
        snapshot_key = _digest(
            {
                "provider": DERIVED_PROVIDER_KEY,
                "tool": tool_name,
                "instrument_id": target_record.instrument_id,
                "observed_at": _utc_text(observed_at),
                "payload_sha256": payload_sha,
            }
        )
        self.connection.execute(
            """
            INSERT INTO financial_data_snapshots(
                snapshot_key, instrument_id, provider_profile_id, data_type,
                interval_code, observed_at, fetched_at, market_status, currency,
                timezone, quality_status, payload_json, payload_sha256, request_id
            ) VALUES(?, ?, ?, ?, 'derived', ?, ?, 'unknown', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_key) DO UPDATE SET
                fetched_at=excluded.fetched_at,
                request_id=excluded.request_id,
                quality_status=excluded.quality_status
            """,
            (
                snapshot_key,
                target_record.instrument_id,
                profile_id,
                data_type,
                _utc_text(observed_at),
                _utc_text(self.context.server_now_utc),
                target_record.currency,
                self._timezone_name(target_record),
                quality_status,
                payload_text,
                payload_sha,
                request_id,
            ),
        )
        snapshot_id = int(
            self.connection.execute(
                "SELECT id FROM financial_data_snapshots WHERE snapshot_key=?",
                (snapshot_key,),
            ).fetchone()[0]
        )
        snapshot = self._load_snapshots(snapshot_ids=(snapshot_id,))[0]
        self._pin_snapshots(
            (snapshot,),
            tool_name=tool_name,
            request_id=request_id,
            preferred_provider_id=DERIVED_PROVIDER_KEY,
            attempted_provider_ids=(DERIVED_PROVIDER_KEY,),
            degraded=False,
        )
        return snapshot

    def get_indicators(
        self,
        symbol: str,
        indicator: str,
        curr_date: str,
        look_back_days: int = 30,
    ) -> str:
        with self._lock:
            self._validate_symbol(symbol)
            current, _ = self._validate_window(curr_date, curr_date)
            names = tuple(
                dict.fromkeys(
                    item.strip().lower()
                    for item in str(indicator or "").split(",")
                    if item.strip()
                )
            )
            if not names or set(names) - set(SUPPORTED_INDICATORS):
                raise TradingAgentsCNDataError(
                    "指标名称不在允许列表", error_code="unsupported_indicator"
                )
            if isinstance(look_back_days, bool) or not 1 <= int(look_back_days) <= 1000:
                raise TradingAgentsCNDataError(
                    "look_back_days 超出边界", error_code="invalid_data_request"
                )
            required_window = max(
                200 if "close_200_sma" in names else 0,
                50 if "close_50_sma" in names else 0,
                35 if set(names) & {"macd", "macds", "macdh"} else 0,
                int(look_back_days),
            )
            bars = self._stored_bars(current, look_back_days=required_window)
            if not bars:
                return _json_text(
                    {
                        "schema_version": 1,
                        "adapter": ADAPTER_VERSION,
                        "tool": "get_indicators",
                        "status": "unavailable",
                        "error_code": "persisted_ohlcv_required",
                        "instruction": "Call get_stock_data first; indicators never fetch a second vendor.",
                    }
                )
            values = {}
            required = {}
            for name in names:
                value, count = self._indicator_value(name, bars)
                values[name] = round(value, 10) if value is not None else None
                required[name] = count
            source_ids = sorted({int(row["source_snapshot_id"]) for row in bars})
            request_id = self._request_id(
                "get_indicators",
                self.instrument,
                "derived_indicator",
                ",".join(names),
                {
                    "curr_date": current.isoformat(),
                    "look_back_days": int(look_back_days),
                    "source_snapshot_ids": source_ids,
                },
            )
            payload = {
                "adapter": ADAPTER_VERSION,
                "calculation": "deterministic_local_from_persisted_ohlcv",
                "external_network_calls": 0,
                "instrument_id": self.instrument.instrument_id,
                "canonical_symbol": self.instrument.canonical_symbol,
                "as_of": current.isoformat(),
                "values": values,
                "required_bar_counts": required,
                "available_bar_count": len(bars),
                "source_snapshot_ids": source_ids,
                "latest_bar": bars[-1],
            }
            snapshot = self._persist_derived(
                tool_name="get_indicators",
                data_type="derived_indicator",
                observed_at=_stored_datetime(bars[-1]["observed_at"]),
                payload=payload,
                request_id=request_id,
                quality_status=(
                    "derived_verified"
                    if all(value is not None for value in values.values())
                    else "derived_insufficient_history"
                ),
            )
            return _json_text(
                {
                    "schema_version": 1,
                    "adapter": ADAPTER_VERSION,
                    "tool": "get_indicators",
                    "status": "completed",
                    "snapshot_id": snapshot["snapshot_id"],
                    "observed_at": snapshot["observed_at"],
                    **payload,
                    "citation_rule": "Cite this derived snapshot and its source_snapshot_ids.",
                }
            )

    def get_verified_market_snapshot(
        self,
        symbol: str,
        curr_date: str,
        look_back_days: int = 30,
    ) -> str:
        with self._lock:
            self._validate_symbol(symbol)
            current, _ = self._validate_window(curr_date, curr_date)
            if isinstance(look_back_days, bool) or not 1 <= int(look_back_days) <= 365:
                raise TradingAgentsCNDataError(
                    "look_back_days 超出边界", error_code="invalid_data_request"
                )
            bars = self._stored_bars(current, look_back_days=max(200, int(look_back_days)))
            if not bars:
                return _json_text(
                    {
                        "schema_version": 1,
                        "adapter": ADAPTER_VERSION,
                        "tool": "get_verified_market_snapshot",
                        "status": "unavailable",
                        "error_code": "persisted_ohlcv_required",
                    }
                )
            cutoff_local_day = self.context.cutoff_at_utc.astimezone(
                ZoneInfo(self._timezone_name(self.instrument))
            ).date()
            quote_result = None
            if current == cutoff_local_day:
                quote_result = self._fetch_provider(
                    tool_name="get_verified_market_snapshot",
                    target=self.instrument,
                    chain_key="quote",
                    endpoint="quote",
                    metric="last_price",
                    data_kind=FinancialDataKind.QUOTE,
                    parameters={"interval": "1m", "period": "1d"},
                )
            indicators = {}
            required = {}
            for name in sorted(SUPPORTED_INDICATORS):
                value, count = self._indicator_value(name, bars)
                indicators[name] = round(value, 10) if value is not None else None
                required[name] = count
            bar_source_ids = sorted({int(row["source_snapshot_id"]) for row in bars})
            quote_ids = (
                [int(item["snapshot_id"]) for item in quote_result.snapshots]
                if quote_result is not None
                else []
            )
            request_id = self._request_id(
                "get_verified_market_snapshot",
                self.instrument,
                "verified_snapshot",
                "ohlcv_and_indicators",
                {
                    "curr_date": current.isoformat(),
                    "look_back_days": int(look_back_days),
                    "bar_snapshot_ids": bar_source_ids,
                    "quote_snapshot_ids": quote_ids,
                },
            )
            payload = {
                "adapter": ADAPTER_VERSION,
                "calculation": "deterministic_local_verification",
                "external_network_calls_for_indicators": 0,
                "instrument_id": self.instrument.instrument_id,
                "canonical_symbol": self.instrument.canonical_symbol,
                "as_of": current.isoformat(),
                "latest_ohlcv": bars[-1],
                "recent_closes": [
                    {"observed_at": row["observed_at"], "close": row["close"]}
                    for row in bars[-int(look_back_days) :]
                ],
                "indicators": indicators,
                "required_bar_counts": required,
                "available_bar_count": len(bars),
                "source_snapshot_ids": bar_source_ids,
                "quote_snapshot_ids": quote_ids,
                "quote_snapshots": list(quote_result.snapshots) if quote_result else [],
                "quote_status": quote_result.status if quote_result else "historical_not_requested",
            }
            snapshot = self._persist_derived(
                tool_name="get_verified_market_snapshot",
                data_type="verified_market_snapshot",
                observed_at=_stored_datetime(bars[-1]["observed_at"]),
                payload=payload,
                request_id=request_id,
                quality_status="derived_verified",
            )
            return _json_text(
                {
                    "schema_version": 1,
                    "adapter": ADAPTER_VERSION,
                    "tool": "get_verified_market_snapshot",
                    "status": "completed",
                    "snapshot_id": snapshot["snapshot_id"],
                    "observed_at": snapshot["observed_at"],
                    **payload,
                    "citation_rule": "This snapshot is the exact-claim source of truth.",
                }
            )

    def _validate_index(self, symbol: str) -> InstrumentRecord:
        target = self._validate_symbol(symbol)
        if target.asset_type != "index":
            raise TradingAgentsCNDataError(
                "指数工具只接受指数标的", error_code="unsupported_index_asset"
            )
        return target

    def get_index_identity(self, ticker: str) -> str:
        with self._lock:
            target = self._validate_index(ticker)
            metadata = dict(target.metadata)
            compiler = str(metadata.get("compiler") or "").strip()
            official_url = str(metadata.get("official_url") or "").strip()
            if not compiler or not official_url:
                raise TradingAgentsCNDataError(
                    "指数身份缺少编制方或官方网址",
                    error_code="index_identity_incomplete",
                )
            return _json_text(
                {
                    "schema_version": 1,
                    "adapter": ADAPTER_VERSION,
                    "tool": "get_index_identity",
                    "status": "complete",
                    "target": target.to_dict(),
                    "compiler": compiler,
                    "official_url": official_url,
                    "benchmark_family": str(metadata.get("benchmark_family") or ""),
                    "directly_tradeable": bool(metadata.get("directly_tradeable", False)),
                    "identity_source": "project_controlled_instrument_registry",
                    "boundary": "An index is a benchmark identity, not a directly executable security.",
                }
            )

    def get_index_constituents(self, ticker: str, curr_date: str) -> str:
        with self._lock:
            target = self._validate_index(ticker)
            current, _ = self._validate_window(curr_date, curr_date)
            result = self._fetch_provider(
                tool_name="get_index_constituents",
                target=target,
                chain_key="constituents",
                endpoint="constituents",
                metric="constituents",
                data_kind=FinancialDataKind.CONSTITUENT,
                parameters={"as_of": current.isoformat()},
            )
            envelope = dict(self._fetch_envelope("get_index_constituents", result))
            member_count = 0
            composition_dates = []
            for snapshot in result.snapshots:
                normalized = snapshot.get("payload", {}).get("normalized_payload") or {}
                members = normalized.get("members")
                if isinstance(members, list):
                    member_count += len(members)
                if snapshot.get("observed_at"):
                    composition_dates.append(str(snapshot["observed_at"])[:10])
            envelope.update(
                {
                    "constituent_as_of": max(composition_dates) if composition_dates else None,
                    "member_count": member_count,
                    "component_contribution": {
                        "status": "unavailable",
                        "coverage": 0.0,
                        "reason": "component return snapshots are not present in this index-scoped run",
                    },
                    "boundary": (
                        "Constituent weights never imply contribution without same-as-of component returns."
                    ),
                }
            )
            return _json_text(envelope)

    def get_market_breadth(self, ticker: str, curr_date: str) -> str:
        with self._lock:
            target = self._validate_index(ticker)
            current, _ = self._validate_window(curr_date, curr_date)
            result = self._fetch_provider(
                tool_name="get_market_breadth",
                target=target,
                chain_key="market_breadth",
                endpoint="market_breadth",
                metric="market_breadth",
                data_kind=FinancialDataKind.MACRO,
                parameters={
                    "as_of": current.isoformat(),
                    "exchange": target.exchange if target.exchange in {"XSHG", "XSHE"} else "ALL",
                },
            )
            envelope = dict(self._fetch_envelope("get_market_breadth", result))
            envelope["semantic_role"] = "market_participation_not_index_direction"
            return _json_text(envelope)

    def get_sector_rotation(self, ticker: str, curr_date: str, limit: int = 20) -> str:
        with self._lock:
            target = self._validate_index(ticker)
            current, _ = self._validate_window(curr_date, curr_date)
            if isinstance(limit, bool) or not 1 <= int(limit) <= 100:
                raise TradingAgentsCNDataError(
                    "sector limit 超出边界", error_code="invalid_data_request"
                )
            result = self._fetch_provider(
                tool_name="get_sector_rotation",
                target=target,
                chain_key="sector_rotation",
                endpoint="sector_rotation",
                metric="sector_rotation",
                data_kind=FinancialDataKind.MACRO,
                parameters={"as_of": current.isoformat(), "limit": int(limit)},
            )
            envelope = dict(self._fetch_envelope("get_sector_rotation", result))
            envelope["semantic_role"] = "same_snapshot_sector_ranking_not_forecast"
            return _json_text(envelope)

    def get_market_liquidity(
        self,
        ticker: str,
        curr_date: str,
        look_back_days: int = 20,
    ) -> str:
        with self._lock:
            target = self._validate_index(ticker)
            current, _ = self._validate_window(curr_date, curr_date)
            if isinstance(look_back_days, bool) or not 5 <= int(look_back_days) <= 120:
                raise TradingAgentsCNDataError(
                    "liquidity look_back_days 超出边界",
                    error_code="invalid_data_request",
                )
            bars = self._stored_bars(current, look_back_days=int(look_back_days))
            window = bars[-int(look_back_days) :]
            if len(window) < 5:
                return _json_text(
                    {
                        "schema_version": 1,
                        "adapter": ADAPTER_VERSION,
                        "tool": "get_market_liquidity",
                        "status": "unavailable",
                        "error_code": "persisted_ohlcv_required",
                    }
                )

            def ratio(field: str) -> Optional[float]:
                values = [
                    value
                    for value in (_finite(row.get(field)) for row in window[:-1])
                    if value is not None and value > 0
                ]
                latest = _finite(window[-1].get(field))
                if not values or latest is None or latest < 0:
                    return None
                baseline = statistics.median(values)
                return latest / baseline if baseline > 0 else None

            latest = window[-1]
            close = _finite(latest.get("close"))
            high = _finite(latest.get("high"))
            low = _finite(latest.get("low"))
            intraday_range_percent = (
                ((high - low) / close) * 100
                if close is not None and close > 0 and high is not None and low is not None
                else None
            )
            values = {
                "volume_vs_prior_median": ratio("volume"),
                "turnover_vs_prior_median": ratio("turnover"),
                "latest_intraday_range_percent": intraday_range_percent,
            }
            values = {
                key: round(value, 10) if value is not None else None
                for key, value in values.items()
            }
            source_ids = sorted({int(row["source_snapshot_id"]) for row in window})
            request_id = self._request_id(
                "get_market_liquidity",
                target,
                "derived_liquidity",
                "index_volume_turnover",
                {
                    "curr_date": current.isoformat(),
                    "look_back_days": int(look_back_days),
                    "source_snapshot_ids": source_ids,
                },
            )
            payload = {
                "adapter": ADAPTER_VERSION,
                "calculation": "deterministic_local_from_persisted_index_ohlcv",
                "external_network_calls": 0,
                "instrument_id": target.instrument_id,
                "canonical_symbol": target.canonical_symbol,
                "as_of": current.isoformat(),
                "available_bar_count": len(window),
                "values": values,
                "source_snapshot_ids": source_ids,
                "latest_bar": latest,
            }
            snapshot = self._persist_derived(
                tool_name="get_market_liquidity",
                data_type="derived_market_liquidity",
                observed_at=_stored_datetime(latest["observed_at"]),
                payload=payload,
                request_id=request_id,
                quality_status=(
                    "derived_verified"
                    if any(value is not None for value in values.values())
                    else "derived_insufficient_history"
                ),
            )
            return _json_text(
                {
                    "schema_version": 1,
                    "adapter": ADAPTER_VERSION,
                    "tool": "get_market_liquidity",
                    "status": (
                        "completed"
                        if any(value is not None for value in values.values())
                        else "limited"
                    ),
                    "snapshot_id": snapshot["snapshot_id"],
                    "observed_at": snapshot["observed_at"],
                    **payload,
                    "boundary": "This is an OHLCV liquidity proxy, not order-book depth or fund flow.",
                    "citation_rule": "Cite this derived snapshot and its source_snapshot_ids.",
                }
            )

    def _fundamental_fetch(
        self,
        tool_name: str,
        *,
        statement: str,
        curr_date: str,
        start: Optional[date] = None,
    ) -> _FetchResult:
        current, _ = self._validate_window(curr_date, curr_date)
        parameters: dict[str, Any] = {"statement": statement, "end": current.isoformat()}
        if start is not None:
            parameters["start"] = start.isoformat()
        return self._fetch_provider(
            tool_name=tool_name,
            target=self.instrument,
            chain_key="financials",
            endpoint="financials",
            metric=statement,
            data_kind=FinancialDataKind.FUNDAMENTAL,
            parameters=parameters,
        )

    def _validate_fund_asset(self, ticker: str, *, required_type: str = "") -> InstrumentRecord:
        target = self._validate_symbol(ticker)
        if target.asset_type not in {"etf", "fund"}:
            raise TradingAgentsCNDataError(
                "基金工具只接受 ETF 或开放式基金标的",
                error_code="unsupported_asset",
            )
        if required_type and target.asset_type != required_type:
            raise TradingAgentsCNDataError(
                f"该工具只接受 {required_type} 标的",
                error_code="unsupported_asset",
            )
        return target

    def _tracked_index(self, target: InstrumentRecord) -> Optional[InstrumentRecord]:
        raw = str(
            target.metadata.get("benchmark_instrument")
            or target.metadata.get("tracks_instrument")
            or ""
        ).strip()
        if not raw:
            return None
        resolution = self.instruments.resolve(raw, asset_type="index")
        if resolution.status != "resolved" or resolution.instrument_id is None:
            return None
        tracked = self.instruments.get(resolution.instrument_id)
        return tracked if tracked is not None and tracked.asset_type == "index" else None

    def get_fund_identity(self, ticker: str) -> str:
        with self._lock:
            target = self._validate_fund_asset(ticker)
            metadata = dict(target.metadata)
            return _json_text(
                {
                    "schema_version": 1,
                    "adapter": ADAPTER_VERSION,
                    "tool": "get_fund_identity",
                    "status": "complete",
                    "target": target.to_dict(),
                    "template": "etf" if target.asset_type == "etf" else "open_end_fund",
                    "share_class": target.share_class or None,
                    "currency": target.currency,
                    "tracks_instrument": metadata.get("tracks_instrument"),
                    "benchmark_instrument": metadata.get("benchmark_instrument"),
                    "exchange_traded": target.asset_type == "etf",
                    "intraday_market_data_applicable": target.asset_type == "etf",
                    "valuation_basis": (
                        "exchange_price_and_disclosed_nav"
                        if target.asset_type == "etf"
                        else "last_disclosed_nav_only"
                    ),
                    "identity_source": "project_controlled_instrument_registry",
                    "boundary": "This identity is not company fundamentals and does not imply a trade.",
                }
            )

    def _fund_fetch(
        self,
        tool_name: str,
        target: InstrumentRecord,
        *,
        chain_key: str,
        metric: str,
        parameters: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        result = self._fetch_provider(
            tool_name=tool_name,
            target=target,
            chain_key=chain_key,
            endpoint="fund",
            metric=metric,
            data_kind=FinancialDataKind.FUNDAMENTAL,
            parameters=parameters,
        )
        envelope = dict(self._fetch_envelope(tool_name, result))
        envelope["target"] = {
            "instrument_id": target.instrument_id,
            "canonical_symbol": target.canonical_symbol,
            "display_name": target.display_name,
            "market": target.market,
            "asset_type": target.asset_type,
            "currency": target.currency,
            "share_class": target.share_class or None,
        }
        return envelope

    def get_fund_profile(self, ticker: str, curr_date: str) -> str:
        with self._lock:
            target = self._validate_fund_asset(ticker)
            current, _ = self._validate_window(curr_date, curr_date)
            envelope = dict(
                self._fund_fetch(
                    "get_fund_profile",
                    target,
                    chain_key="fund_basic",
                    metric="fund_basic",
                    parameters={"as_of": current.isoformat()},
                )
            )
            envelope.update(
                {
                    "share_class": target.share_class or None,
                    "currency": target.currency,
                    "benchmark_instrument": target.metadata.get("benchmark_instrument"),
                    "boundary": "Profile and fee fields are fund records, never issuer financial statements.",
                }
            )
            return _json_text(envelope)

    def get_fund_nav(self, ticker: str, curr_date: str, look_back_days: int = 30) -> str:
        with self._lock:
            target = self._validate_fund_asset(ticker)
            current, _ = self._validate_window(curr_date, curr_date)
            if isinstance(look_back_days, bool) or not 1 <= int(look_back_days) <= 730:
                raise TradingAgentsCNDataError(
                    "NAV look_back_days 超出边界", error_code="invalid_data_request"
                )
            envelope = dict(
                self._fund_fetch(
                    "get_fund_nav",
                    target,
                    chain_key="fund_nav",
                    metric="fund_nav",
                    parameters={
                        "start": (current - timedelta(days=int(look_back_days))).isoformat(),
                        "end": current.isoformat(),
                    },
                )
            )
            nav_dates = []
            for snapshot in envelope.get("snapshots") or []:
                records = (snapshot.get("payload", {}).get("normalized_payload") or {}).get("records")
                if isinstance(records, list):
                    nav_dates.extend(
                        str(item.get("nav_date") or item.get("end_date") or "")
                        for item in records
                        if isinstance(item, Mapping)
                    )
            latest = max((value for value in nav_dates if value), default=None)
            envelope.update(
                {
                    "requested_valuation_date": current.isoformat(),
                    "latest_disclosed_nav_date": latest,
                    "non_trading_day_policy": "use_last_disclosed_nav; never synthesize an intraday fund price",
                    "valuation_basis": "last_disclosed_nav_not_intraday_quote",
                }
            )
            return _json_text(envelope)

    def get_fund_holdings(self, ticker: str, curr_date: str) -> str:
        with self._lock:
            target = self._validate_fund_asset(ticker)
            current, _ = self._validate_window(curr_date, curr_date)
            envelope = dict(
                self._fund_fetch(
                    "get_fund_holdings",
                    target,
                    chain_key="fund_holdings",
                    metric="fund_holdings",
                    parameters={
                        "year": str(current.year),
                        "start": date(current.year, 1, 1).isoformat(),
                        "end": current.isoformat(),
                    },
                )
            )
            periods = []
            publication_times = []
            for snapshot in envelope.get("snapshots") or []:
                records = (snapshot.get("payload", {}).get("normalized_payload") or {}).get("records")
                if isinstance(records, list):
                    for item in records:
                        if not isinstance(item, Mapping):
                            continue
                        period = item.get("disclosure_period") or item.get("end_date")
                        publication = item.get("published_at") or item.get("ann_date")
                        if period:
                            periods.append(str(period))
                        if publication:
                            publication_times.append(str(publication))
            envelope.update(
                {
                    "latest_disclosure_period": max(periods) if periods else None,
                    "latest_published_at": max(publication_times) if publication_times else None,
                    "disclosure_lag": True,
                    "point_in_time_safe": bool(publication_times),
                    "boundary": "Holdings are periodic disclosures, not a live portfolio. Missing publication timestamps prohibit point-in-time backtests.",
                }
            )
            return _json_text(envelope)

    def get_fund_manager(self, ticker: str, curr_date: str) -> str:
        with self._lock:
            target = self._validate_fund_asset(ticker)
            current, _ = self._validate_window(curr_date, curr_date)
            envelope = self._fund_fetch(
                "get_fund_manager",
                target,
                chain_key="fund_manager",
                metric="fund_manager",
                parameters={"as_of": current.isoformat()},
            )
            return _json_text(
                {
                    **dict(envelope),
                    "boundary": "A current-manager snapshot is not manager history unless effective dates are present.",
                }
            )

    def get_fund_share(self, ticker: str, curr_date: str, look_back_days: int = 365) -> str:
        with self._lock:
            target = self._validate_fund_asset(ticker)
            current, _ = self._validate_window(curr_date, curr_date)
            if isinstance(look_back_days, bool) or not 30 <= int(look_back_days) <= 1825:
                raise TradingAgentsCNDataError(
                    "fund share look_back_days 超出边界", error_code="invalid_data_request"
                )
            envelope = self._fund_fetch(
                "get_fund_share",
                target,
                chain_key="fund_share",
                metric="fund_share",
                parameters={
                    "start": (current - timedelta(days=int(look_back_days))).isoformat(),
                    "end": current.isoformat(),
                },
            )
            return _json_text(
                {
                    **dict(envelope),
                    "boundary": "Fund shares are periodic records and are not intraday creations/redemptions.",
                }
            )

    def get_fund_fees(self, ticker: str, curr_date: str) -> str:
        with self._lock:
            target = self._validate_fund_asset(ticker)
            current, _ = self._validate_window(curr_date, curr_date)
            profile = self._fund_fetch(
                "get_fund_fees",
                target,
                chain_key="fund_basic",
                metric="fund_basic",
                parameters={"as_of": current.isoformat()},
            )
            operating = self._fund_fetch(
                "get_fund_fees",
                target,
                chain_key="fund_fees",
                metric="fund_operating_fees",
                parameters={"as_of": current.isoformat()},
            )
            available = any(item.get("snapshots") for item in (profile, operating))
            return _json_text(
                {
                    "schema_version": 1,
                    "adapter": ADAPTER_VERSION,
                    "tool": "get_fund_fees",
                    "status": "complete" if available else "insufficient_data",
                    "target": profile["target"],
                    "sources": [profile, operating],
                    "boundary": "Fees may vary by share class and channel; cite the exact record and effective date when available.",
                }
            )

    def get_fund_subscription_redemption(self, ticker: str, curr_date: str) -> str:
        with self._lock:
            target = self._validate_fund_asset(ticker)
            current, _ = self._validate_window(curr_date, curr_date)
            envelope = self._fund_fetch(
                "get_fund_subscription_redemption",
                target,
                chain_key="fund_trading",
                metric="fund_subscription_redemption",
                parameters={"as_of": current.isoformat()},
            )
            return _json_text(
                {
                    **dict(envelope),
                    "boundary": "Subscription/redemption status is a current terms snapshot, not an exchange quote.",
                }
            )

    def get_etf_constituents(self, ticker: str, curr_date: str) -> str:
        with self._lock:
            target = self._validate_fund_asset(ticker, required_type="etf")
            current, _ = self._validate_window(curr_date, curr_date)
            tracked = self._tracked_index(target)
            if tracked is None:
                return _json_text(
                    {
                        "schema_version": 1,
                        "adapter": ADAPTER_VERSION,
                        "tool": "get_etf_constituents",
                        "status": "insufficient_data",
                        "error_code": "tracked_index_not_resolved",
                        "target": target.to_dict(),
                    }
                )
            result = self._fetch_provider(
                tool_name="get_etf_constituents",
                target=tracked,
                chain_key="constituents",
                endpoint="constituents",
                metric="constituents",
                data_kind=FinancialDataKind.CONSTITUENT,
                parameters={"as_of": current.isoformat()},
            )
            envelope = dict(self._fetch_envelope("get_etf_constituents", result))
            envelope.update(
                {
                    "target": target.to_dict(),
                    "tracked_index": tracked.to_dict(),
                    "boundary": "These are tracked-index constituents, not necessarily the ETF's live creation basket or disclosed holdings.",
                }
            )
            return _json_text(envelope)

    def get_etf_tracking(self, ticker: str, curr_date: str, look_back_days: int = 60) -> str:
        with self._lock:
            target = self._validate_fund_asset(ticker, required_type="etf")
            current, _ = self._validate_window(curr_date, curr_date)
            if isinstance(look_back_days, bool) or not 20 <= int(look_back_days) <= 365:
                raise TradingAgentsCNDataError(
                    "tracking look_back_days 超出边界", error_code="invalid_data_request"
                )
            tracked = self._tracked_index(target)
            if tracked is None:
                return _json_text(
                    {
                        "schema_version": 1,
                        "adapter": ADAPTER_VERSION,
                        "tool": "get_etf_tracking",
                        "status": "insufficient_data",
                        "error_code": "tracked_index_not_resolved",
                        "target": target.to_dict(),
                    }
                )
            start = current - timedelta(days=int(look_back_days) * 2 + 10)
            fetched = []
            for role, instrument in (("etf", target), ("benchmark", tracked)):
                fetched.append(
                    self._fetch_provider(
                        tool_name="get_etf_tracking",
                        target=instrument,
                        chain_key="bars",
                        endpoint="bars",
                        metric="ohlcv",
                        data_kind=FinancialDataKind.BAR,
                        parameters={
                            "start": start.isoformat(),
                            "end": current.isoformat(),
                            "interval": "1d",
                            "adjustment": "raw",
                            "tracking_role": role,
                        },
                    )
                )
            etf_bars = self._stored_bars(current, look_back_days=int(look_back_days) + 1, target=target)
            index_bars = self._stored_bars(current, look_back_days=int(look_back_days) + 1, target=tracked)

            def returns(rows: Sequence[Mapping[str, Any]], timezone_name: str) -> dict[str, float]:
                values: dict[str, float] = {}
                for position in range(1, len(rows)):
                    previous = _finite(rows[position - 1].get("close"))
                    latest = _finite(rows[position].get("close"))
                    if previous is None or latest is None or previous <= 0:
                        continue
                    observed = _stored_datetime(rows[position]["observed_at"])
                    key = observed.astimezone(ZoneInfo(timezone_name)).date().isoformat()
                    values[key] = latest / previous - 1.0
                return values

            left = returns(etf_bars, self._timezone_name(target))
            right = returns(index_bars, self._timezone_name(tracked))
            overlap = sorted(set(left) & set(right))[-int(look_back_days) :]
            differences = [left[key] - right[key] for key in overlap]
            tracking_difference = sum(differences) / len(differences) if differences else None
            tracking_error = (
                statistics.stdev(differences) * math.sqrt(252)
                if len(differences) >= 2
                else None
            )
            source_ids = sorted(
                {
                    int(row["source_snapshot_id"])
                    for row in etf_bars + index_bars
                    if row.get("source_snapshot_id")
                }
            )
            status = "completed" if len(overlap) >= 10 else "insufficient_data"
            request_id = self._request_id(
                "get_etf_tracking",
                target,
                "derived_tracking",
                "tracking_error",
                {
                    "benchmark_instrument_id": tracked.instrument_id,
                    "look_back_days": int(look_back_days),
                    "overlap_count": len(overlap),
                    "source_snapshot_ids": source_ids,
                },
            )
            payload = {
                "adapter": ADAPTER_VERSION,
                "calculation": "daily_raw_close_return_difference_annualized_sqrt_252",
                "external_network_calls": 0,
                "instrument_id": target.instrument_id,
                "benchmark_instrument_id": tracked.instrument_id,
                "benchmark_symbol": tracked.canonical_symbol,
                "as_of": current.isoformat(),
                "overlap_observation_count": len(overlap),
                "tracking_difference_daily": round(tracking_difference, 12) if tracking_difference is not None else None,
                "tracking_error_annualized": round(tracking_error, 12) if tracking_error is not None else None,
                "source_snapshot_ids": source_ids,
                "provider_fetches": [self._fetch_envelope("get_etf_tracking", item) for item in fetched],
            }
            snapshot = self._persist_derived(
                tool_name="get_etf_tracking",
                data_type="derived_etf_tracking",
                observed_at=(
                    _stored_datetime(etf_bars[-1]["observed_at"])
                    if etf_bars
                    else self.context.cutoff_at_utc
                ),
                payload=payload,
                request_id=request_id,
                quality_status=(
                    "derived_verified" if status == "completed" else "derived_insufficient_history"
                ),
                target=target,
            )
            return _json_text(
                {
                    "schema_version": 1,
                    "adapter": ADAPTER_VERSION,
                    "tool": "get_etf_tracking",
                    "status": status,
                    "error_code": None if status == "completed" else "insufficient_data",
                    "snapshot_id": snapshot["snapshot_id"],
                    "observed_at": snapshot["observed_at"],
                    **payload,
                    "boundary": "Raw-close tracking statistics exclude distributions and do not equal official total-return tracking error.",
                    "citation_rule": "Cite this derived snapshot and all source_snapshot_ids.",
                }
            )

    def get_etf_liquidity(self, ticker: str, curr_date: str, look_back_days: int = 20) -> str:
        with self._lock:
            target = self._validate_fund_asset(ticker, required_type="etf")
            current, _ = self._validate_window(curr_date, curr_date)
            if isinstance(look_back_days, bool) or not 5 <= int(look_back_days) <= 120:
                raise TradingAgentsCNDataError(
                    "ETF liquidity look_back_days 超出边界", error_code="invalid_data_request"
                )
            start = current - timedelta(days=int(look_back_days) * 3)
            fetched = self._fetch_provider(
                tool_name="get_etf_liquidity",
                target=target,
                chain_key="bars",
                endpoint="bars",
                metric="ohlcv",
                data_kind=FinancialDataKind.BAR,
                parameters={
                    "start": start.isoformat(),
                    "end": current.isoformat(),
                    "interval": "1d",
                    "adjustment": "raw",
                },
            )
            bars = self._stored_bars(current, look_back_days=int(look_back_days), target=target)
            window = bars[-int(look_back_days) :]
            if len(window) < 5:
                return _json_text(
                    {
                        "schema_version": 1,
                        "adapter": ADAPTER_VERSION,
                        "tool": "get_etf_liquidity",
                        "status": "insufficient_data",
                        "error_code": "persisted_ohlcv_required",
                        "provider_fetch": self._fetch_envelope("get_etf_liquidity", fetched),
                    }
                )

            def ratio(field: str) -> Optional[float]:
                history = [
                    value
                    for value in (_finite(row.get(field)) for row in window[:-1])
                    if value is not None and value > 0
                ]
                latest = _finite(window[-1].get(field))
                if not history or latest is None or latest < 0:
                    return None
                baseline = statistics.median(history)
                return latest / baseline if baseline > 0 else None

            latest = window[-1]
            values = {
                "volume_vs_prior_median": ratio("volume"),
                "turnover_vs_prior_median": ratio("turnover"),
            }
            values = {key: round(value, 10) if value is not None else None for key, value in values.items()}
            source_ids = sorted({int(row["source_snapshot_id"]) for row in window})
            request_id = self._request_id(
                "get_etf_liquidity",
                target,
                "derived_liquidity",
                "etf_volume_turnover",
                {
                    "curr_date": current.isoformat(),
                    "look_back_days": int(look_back_days),
                    "source_snapshot_ids": source_ids,
                },
            )
            payload = {
                "adapter": ADAPTER_VERSION,
                "calculation": "deterministic_local_from_persisted_etf_ohlcv",
                "external_network_calls": 0,
                "instrument_id": target.instrument_id,
                "as_of": current.isoformat(),
                "available_bar_count": len(window),
                "values": values,
                "source_snapshot_ids": source_ids,
                "latest_bar": latest,
            }
            snapshot = self._persist_derived(
                tool_name="get_etf_liquidity",
                data_type="derived_etf_liquidity",
                observed_at=_stored_datetime(latest["observed_at"]),
                payload=payload,
                request_id=request_id,
                quality_status="derived_verified",
                target=target,
            )
            return _json_text(
                {
                    "schema_version": 1,
                    "adapter": ADAPTER_VERSION,
                    "tool": "get_etf_liquidity",
                    "status": "completed",
                    "snapshot_id": snapshot["snapshot_id"],
                    "observed_at": snapshot["observed_at"],
                    **payload,
                    "boundary": "OHLCV liquidity proxies are not bid-ask spread, order-book depth, or creation basket liquidity.",
                }
            )

    def get_fundamentals(self, ticker: str, curr_date: str) -> str:
        with self._lock:
            self._validate_symbol(ticker)
            current, _ = self._validate_window(curr_date, curr_date)
            results = []
            if self.instrument.market == "CN" and self.instrument.asset_type == "equity":
                results.append(
                    self._fundamental_fetch(
                        "get_fundamentals",
                        statement="fina_indicator",
                        curr_date=curr_date,
                        start=current - timedelta(days=730),
                    )
                )
                results.append(
                    self._fetch_provider(
                        tool_name="get_fundamentals",
                        target=self.instrument,
                        chain_key="industry",
                        endpoint="industry",
                        metric="industry_profile",
                        data_kind=FinancialDataKind.FUNDAMENTAL,
                        parameters={},
                    )
                )
            elif self.instrument.market == "CN_FUND":
                results.append(
                    self._fetch_provider(
                        tool_name="get_fundamentals",
                        target=self.instrument,
                        chain_key="fund_basic",
                        endpoint="fund",
                        metric="fund_basic",
                        data_kind=FinancialDataKind.FUNDAMENTAL,
                        parameters={},
                    )
                )
            envelopes = [self._fetch_envelope("get_fundamentals", item) for item in results]
            status = "complete" if envelopes and any(item["snapshots"] for item in envelopes) else "unavailable"
            return _json_text(
                {
                    "schema_version": 1,
                    "adapter": ADAPTER_VERSION,
                    "tool": "get_fundamentals",
                    "status": status,
                    "target": self.instrument.to_dict(),
                    "sources": envelopes,
                    "boundary": (
                        "No authorized structured HK fundamentals provider is configured; "
                        "do not estimate missing statement values."
                        if self.instrument.market == "XHKG"
                        else "Only persisted normalized records may support numeric claims."
                    ),
                }
            )

    def _statement_tool(self, tool_name: str, ticker: str, freq: str, curr_date: Optional[str], statement: str) -> str:
        with self._lock:
            self._validate_symbol(ticker)
            if str(freq or "quarterly").lower() not in {"annual", "quarterly"}:
                raise TradingAgentsCNDataError(
                    "freq 必须为 annual 或 quarterly", error_code="invalid_data_request"
                )
            effective_date = curr_date or self.context.cutoff_at_utc.astimezone(
                ZoneInfo(self._timezone_name(self.instrument))
            ).date().isoformat()
            result = self._fundamental_fetch(
                tool_name,
                statement=statement,
                curr_date=effective_date,
                start=_day(effective_date, "curr_date") - timedelta(days=1825),
            )
            envelope = dict(self._fetch_envelope(tool_name, result))
            envelope["reporting_frequency_requested"] = str(freq or "quarterly").lower()
            envelope["lookahead_guard"] = "provider announcement time, never report end date alone"
            return _json_text(envelope)

    def get_balance_sheet(self, ticker: str, freq: str = "quarterly", curr_date: Optional[str] = None) -> str:
        return self._statement_tool("get_balance_sheet", ticker, freq, curr_date, "balancesheet")

    def get_cashflow(self, ticker: str, freq: str = "quarterly", curr_date: Optional[str] = None) -> str:
        return self._statement_tool("get_cashflow", ticker, freq, curr_date, "cashflow")

    def get_income_statement(self, ticker: str, freq: str = "quarterly", curr_date: Optional[str] = None) -> str:
        return self._statement_tool("get_income_statement", ticker, freq, curr_date, "income")

    def _pin_documents(self, items: Sequence[Mapping[str, Any]], *, role: str) -> None:
        for item in items:
            article_id = int(item["reference_id"])
            instrument_id = int(item.get("instrument_id") or self.instrument.instrument_id)
            metadata = {
                "adapter": ADAPTER_VERSION,
                "tool_name": role,
                "source_key": item.get("source_key"),
                "canonical_source_url": item.get("source_url"),
                "content_copied": False,
                "payload_copied": False,
                "untrusted_external_text": True,
            }
            self.connection.execute(
                """
                INSERT INTO financial_research_evidence(
                    research_run_id, evidence_key, evidence_kind, snapshot_id,
                    article_id, instrument_id, universe_id, evidence_role,
                    match_method, match_score, observed_at, metadata_json
                ) VALUES(?, ?, 'source_document', NULL, ?, ?, NULL,
                         'tradingagents_news_document', ?, ?, ?, ?)
                ON CONFLICT(research_run_id, evidence_key) DO UPDATE SET
                    evidence_role=excluded.evidence_role,
                    match_method=excluded.match_method,
                    match_score=excluded.match_score,
                    observed_at=excluded.observed_at,
                    metadata_json=excluded.metadata_json,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (
                    self.context.research_run_id,
                    f"article:{article_id}:instrument:{instrument_id}",
                    article_id,
                    instrument_id,
                    str(item.get("match_method") or "direct_tool_news"),
                    float(item.get("match_score") or 0.8),
                    str(item["observed_at"]),
                    _json_text(metadata),
                ),
            )

    def _target_documents(
        self, start: date, end: date, *, tool_name: str
    ) -> list[Mapping[str, Any]]:
        timezone_name = self._timezone_name(self.instrument)
        zone = ZoneInfo(timezone_name)
        start_at = datetime.combine(start, time.min, tzinfo=zone).astimezone(UTC)
        end_at = min(
            datetime.combine(end, time.max, tzinfo=zone).astimezone(UTC),
            self.context.cutoff_at_utc,
        )
        result = EvidenceResolver(
            self.connection, clock=lambda: self.context.server_now_utc
        ).resolve_for_run(
            self.context.research_run_id,
            start_at=start_at,
            end_at=end_at,
            instrument_ids=(self.instrument.instrument_id,),
            persist=False,
        )
        items = list(result["document_evidence"][: self.context.max_news_items])
        self._pin_documents(items, role=tool_name)
        return items

    @staticmethod
    def _document_output(item: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "article_id": int(item["reference_id"]),
            "title": str(item.get("source_title") or ""),
            "source_key": str(item.get("source_key") or ""),
            "source_url": str(item.get("source_url") or ""),
            "observed_at": str(item.get("observed_at") or ""),
            "fetched_at": str(item.get("fetched_at") or ""),
            "match_method": str(item.get("match_method") or ""),
            "match_score": float(item.get("match_score") or 0),
            "content_excerpt": str(item.get("content") or "")[:600],
            "content_is_untrusted_external_text": True,
            "external_content_policy": untrusted_external_content_policy(),
            "numeric_claim_boundary": "document is not a quote or structured market metric",
        }

    def get_news(self, ticker: str, start_date: str, end_date: str) -> str:
        with self._lock:
            self._validate_symbol(ticker)
            start, end = self._validate_window(start_date, end_date)
            documents = self._target_documents(start, end, tool_name="get_news")
            structured = None
            if self.instrument.market == "CN" and self.instrument.asset_type == "equity":
                structured = self._fetch_provider(
                    tool_name="get_news",
                    target=self.instrument,
                    chain_key="announcements",
                    endpoint="announcements",
                    metric="company_announcements",
                    data_kind=FinancialDataKind.NEWS,
                    parameters={"start": start.isoformat(), "end": end.isoformat()},
                )
            return _json_text(
                {
                    "schema_version": 1,
                    "adapter": ADAPTER_VERSION,
                    "tool": "get_news",
                    "status": "complete" if documents or (structured and structured.snapshots) else "unavailable",
                    "target": self.instrument.to_dict(),
                    "time_range": {"start_date": start.isoformat(), "end_date": end.isoformat()},
                    "rss_and_web_documents": [self._document_output(item) for item in documents],
                    "structured_announcements": (
                        self._fetch_envelope("get_news", structured) if structured else None
                    ),
                    "stores_remain_separate": True,
                }
            )

    def get_global_news(
        self,
        curr_date: str,
        look_back_days: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> str:
        with self._lock:
            current, _ = self._validate_window(curr_date, curr_date)
            days = 7 if look_back_days is None else int(look_back_days)
            item_limit = self.context.max_news_items if limit is None else int(limit)
            if not 1 <= days <= 365 or not 1 <= item_limit <= self.context.max_news_items:
                raise TradingAgentsCNDataError(
                    "global news 窗口或条数超出边界", error_code="invalid_data_request"
                )
            start = current - timedelta(days=days)
            rows = self.connection.execute(
                """
                SELECT id, title, content, url, canonical_url, domain,
                       publish_date, first_crawled, created_at
                FROM articles WHERE status='active'
                ORDER BY COALESCE(publish_date, first_crawled, created_at) DESC, id DESC
                LIMIT 500
                """
            ).fetchall()
            items = []
            for row in rows:
                raw_time = row[6] or row[7] or row[8]
                try:
                    observed = _stored_datetime(raw_time)
                except TradingAgentsCNDataError:
                    try:
                        observed = datetime.combine(
                            date.fromisoformat(str(raw_time)[:10]), time.min, tzinfo=UTC
                        )
                    except ValueError:
                        continue
                observed_day = observed.astimezone(
                    ZoneInfo(self._timezone_name(self.instrument))
                ).date()
                if observed > self.context.cutoff_at_utc or not start <= observed_day <= current:
                    continue
                item = {
                    "reference_id": int(row[0]),
                    "instrument_id": self.instrument.instrument_id,
                    "source_title": str(row[1] or ""),
                    "content": str(row[2] or ""),
                    "source_url": str(row[4] or row[3] or ""),
                    "source_key": str(row[5] or ""),
                    "observed_at": _utc_text(observed),
                    "fetched_at": str(row[7] or row[8] or _utc_text(observed)),
                    "match_method": "global_financial_context",
                    "match_score": 0.5,
                }
                items.append(item)
                if len(items) >= item_limit:
                    break
            self._pin_documents(items, role="get_global_news")
            return _json_text(
                {
                    "schema_version": 1,
                    "adapter": ADAPTER_VERSION,
                    "tool": "get_global_news",
                    "status": "complete" if items else "unavailable",
                    "time_range": {"start_date": start.isoformat(), "end_date": current.isoformat()},
                    "documents": [self._document_output(item) for item in items],
                    "source": "existing_project_articles_only",
                }
            )

    def get_insider_transactions(self, ticker: str) -> str:
        self._validate_symbol(ticker)
        return _json_text(
            {
                "schema_version": 1,
                "adapter": ADAPTER_VERSION,
                "tool": "get_insider_transactions",
                "status": "unavailable",
                "error_code": "no_authorized_cn_hk_insider_provider",
                "instruction": "Do not infer insider transactions from news or price movement.",
            }
        )

    def get_sentiment_inputs(self, ticker: str, start_date: str, end_date: str) -> str:
        with self._lock:
            self._validate_symbol(ticker)
            start, end = self._validate_window(start_date, end_date)
            documents = self._target_documents(
                start, end, tool_name="get_sentiment_inputs"
            )
            return _json_text(
                {
                    "schema_version": 1,
                    "adapter": ADAPTER_VERSION,
                    "tool": "get_sentiment_inputs",
                    "status": "complete" if documents else "limited",
                    "target": self.instrument.to_dict(),
                    "news_documents": [self._document_output(item) for item in documents],
                    "social_sources": {
                        "reddit": {
                            "status": "hard_disabled",
                            "reason": "authorization_terms_not_approved_and_not_cn_hk_representative",
                        },
                        "stocktwits": {
                            "status": "hard_disabled",
                            "reason": "authorization_terms_not_approved_and_not_cn_hk_representative",
                        },
                    },
                    "sentiment_boundary": "Analyze only supplied source text; absence is not neutral sentiment.",
                }
            )

    def get_macro_indicators(
        self,
        indicator: str,
        curr_date: str,
        look_back_days: Optional[int] = None,
    ) -> str:
        with self._lock:
            current, _ = self._validate_window(curr_date, curr_date)
            days = 365 if look_back_days is None else int(look_back_days)
            if not 1 <= days <= 3650:
                raise TradingAgentsCNDataError(
                    "macro look_back_days 超出边界", error_code="invalid_data_request"
                )
            key = str(indicator or "").strip().casefold()
            canonical = MACRO_ALIASES.get(key)
            if canonical is None and re.fullmatch(r"[A-Za-z0-9._-]{1,32}", str(indicator or "")):
                candidate = f"{str(indicator).upper()}.FRED"
                canonical = candidate if self.instruments.get_by_canonical_symbol(candidate) else None
            target = self.instruments.get_by_canonical_symbol(canonical or "")
            if target is None:
                return _json_text(
                    {
                        "schema_version": 1,
                        "adapter": ADAPTER_VERSION,
                        "tool": "get_macro_indicators",
                        "status": "unavailable",
                        "error_code": "macro_series_not_registered",
                    }
                )
            result = self._fetch_provider(
                tool_name="get_macro_indicators",
                target=target,
                chain_key="macro",
                endpoint="observations",
                metric=str(indicator),
                data_kind=FinancialDataKind.MACRO,
                parameters={
                    "observation_start": (current - timedelta(days=days)).isoformat(),
                    "observation_end": current.isoformat(),
                    "vintage_date": current.isoformat(),
                    "limit": min(self.context.max_tool_records, 10000),
                },
            )
            envelope = dict(self._fetch_envelope("get_macro_indicators", result))
            envelope["semantic_role"] = "macro_context_not_target_quote"
            return _json_text(envelope)

    def get_prediction_markets(self, topic: str, limit: int = 10) -> str:
        if not str(topic or "").strip() or not 1 <= int(limit) <= 50:
            raise TradingAgentsCNDataError(
                "prediction market 参数无效", error_code="invalid_data_request"
            )
        return _json_text(
            {
                "schema_version": 1,
                "adapter": ADAPTER_VERSION,
                "tool": "get_prediction_markets",
                "status": "unavailable",
                "error_code": "topic_to_controlled_market_id_not_resolved",
                "semantic_role": "expectation_not_fact",
                "instruction": "Do not substitute a guessed market or probability.",
            }
        )

    def registered_tools(self) -> Mapping[str, Any]:
        if self._tools is None:
            def get_stock_data(symbol: str, start_date: str, end_date: str) -> str:
                return self.get_stock_data(symbol, start_date, end_date)

            def get_indicators(symbol: str, indicator: str, curr_date: str, look_back_days: int = 30) -> str:
                return self.get_indicators(symbol, indicator, curr_date, look_back_days)

            def get_verified_market_snapshot(symbol: str, curr_date: str, look_back_days: int = 30) -> str:
                return self.get_verified_market_snapshot(symbol, curr_date, look_back_days)

            def get_index_identity(ticker: str) -> str:
                return self.get_index_identity(ticker)

            def get_index_constituents(ticker: str, curr_date: str) -> str:
                return self.get_index_constituents(ticker, curr_date)

            def get_market_breadth(ticker: str, curr_date: str) -> str:
                return self.get_market_breadth(ticker, curr_date)

            def get_sector_rotation(ticker: str, curr_date: str, limit: int = 20) -> str:
                return self.get_sector_rotation(ticker, curr_date, limit)

            def get_market_liquidity(ticker: str, curr_date: str, look_back_days: int = 20) -> str:
                return self.get_market_liquidity(ticker, curr_date, look_back_days)

            def get_fund_identity(ticker: str) -> str:
                return self.get_fund_identity(ticker)

            def get_fund_profile(ticker: str, curr_date: str) -> str:
                return self.get_fund_profile(ticker, curr_date)

            def get_fund_nav(ticker: str, curr_date: str, look_back_days: int = 30) -> str:
                return self.get_fund_nav(ticker, curr_date, look_back_days)

            def get_fund_holdings(ticker: str, curr_date: str) -> str:
                return self.get_fund_holdings(ticker, curr_date)

            def get_fund_manager(ticker: str, curr_date: str) -> str:
                return self.get_fund_manager(ticker, curr_date)

            def get_fund_share(ticker: str, curr_date: str, look_back_days: int = 365) -> str:
                return self.get_fund_share(ticker, curr_date, look_back_days)

            def get_fund_fees(ticker: str, curr_date: str) -> str:
                return self.get_fund_fees(ticker, curr_date)

            def get_fund_subscription_redemption(ticker: str, curr_date: str) -> str:
                return self.get_fund_subscription_redemption(ticker, curr_date)

            def get_etf_constituents(ticker: str, curr_date: str) -> str:
                return self.get_etf_constituents(ticker, curr_date)

            def get_etf_tracking(ticker: str, curr_date: str, look_back_days: int = 60) -> str:
                return self.get_etf_tracking(ticker, curr_date, look_back_days)

            def get_etf_liquidity(ticker: str, curr_date: str, look_back_days: int = 20) -> str:
                return self.get_etf_liquidity(ticker, curr_date, look_back_days)

            def get_fundamentals(ticker: str, curr_date: str) -> str:
                return self.get_fundamentals(ticker, curr_date)

            def get_balance_sheet(ticker: str, freq: str = "quarterly", curr_date: Optional[str] = None) -> str:
                return self.get_balance_sheet(ticker, freq, curr_date)

            def get_cashflow(ticker: str, freq: str = "quarterly", curr_date: Optional[str] = None) -> str:
                return self.get_cashflow(ticker, freq, curr_date)

            def get_income_statement(ticker: str, freq: str = "quarterly", curr_date: Optional[str] = None) -> str:
                return self.get_income_statement(ticker, freq, curr_date)

            def get_news(ticker: str, start_date: str, end_date: str) -> str:
                return self.get_news(ticker, start_date, end_date)

            def get_global_news(curr_date: str, look_back_days: Optional[int] = None, limit: Optional[int] = None) -> str:
                return self.get_global_news(curr_date, look_back_days, limit)

            def get_insider_transactions(ticker: str) -> str:
                return self.get_insider_transactions(ticker)

            def get_macro_indicators(indicator: str, curr_date: str, look_back_days: Optional[int] = None) -> str:
                return self.get_macro_indicators(indicator, curr_date, look_back_days)

            def get_prediction_markets(topic: str, limit: int = 10) -> str:
                return self.get_prediction_markets(topic, limit)

            def get_sentiment_inputs(ticker: str, start_date: str, end_date: str) -> str:
                return self.get_sentiment_inputs(ticker, start_date, end_date)

            specs = {
                "get_stock_data": ("Persisted project OHLCV for the exact A/H target.", get_stock_data),
                "get_indicators": ("Deterministic indicators from persisted OHLCV only.", get_indicators),
                "get_verified_market_snapshot": ("Exact-claim snapshot with quote, OHLCV and deterministic indicators.", get_verified_market_snapshot),
                "get_index_identity": ("Controlled index identity, compiler and direct-trading boundary.", get_index_identity),
                "get_index_constituents": ("Point-in-time index members and weights without inferred contribution.", get_index_constituents),
                "get_market_breadth": ("Exchange-scoped advancing and declining security counts.", get_market_breadth),
                "get_sector_rotation": ("Same-snapshot sector performance ranking through project providers.", get_sector_rotation),
                "get_market_liquidity": ("Deterministic index OHLCV liquidity proxies from persisted bars.", get_market_liquidity),
                "get_fund_identity": ("Controlled ETF/open-fund identity, share class, currency and valuation boundary.", get_fund_identity),
                "get_fund_profile": ("Registered fund profile and benchmark fields without issuer statements.", get_fund_profile),
                "get_fund_nav": ("Disclosed ETF/open-fund NAV history; never an invented intraday fund price.", get_fund_nav),
                "get_fund_holdings": ("Periodic fund portfolio disclosures with publication-lag boundary.", get_fund_holdings),
                "get_fund_manager": ("Fund manager records with explicit history availability.", get_fund_manager),
                "get_fund_share": ("Periodic fund share records, not live creations or redemptions.", get_fund_share),
                "get_fund_fees": ("Share-class-specific fund profile and operating-fee records.", get_fund_fees),
                "get_fund_subscription_redemption": ("Current subscription/redemption terms without quote semantics.", get_fund_subscription_redemption),
                "get_etf_constituents": ("Constituents of the controlled tracked index, not a live ETF basket.", get_etf_constituents),
                "get_etf_tracking": ("Deterministic raw-close ETF/benchmark tracking statistics.", get_etf_tracking),
                "get_etf_liquidity": ("Deterministic ETF OHLCV liquidity proxies.", get_etf_liquidity),
                "get_fundamentals": ("Normalized registered fundamentals for the exact target.", get_fundamentals),
                "get_balance_sheet": ("Normalized balance-sheet records with announcement cutoff.", get_balance_sheet),
                "get_cashflow": ("Normalized cash-flow records with announcement cutoff.", get_cashflow),
                "get_income_statement": ("Normalized income-statement records with announcement cutoff.", get_income_statement),
                "get_news": ("Current project RSS/web evidence plus registered announcements.", get_news),
                "get_global_news": ("Bounded global context from the existing article store.", get_global_news),
                "get_insider_transactions": ("Explicit availability boundary for insider records.", get_insider_transactions),
                "get_macro_indicators": ("Registered point-in-time macro series through project providers.", get_macro_indicators),
                "get_prediction_markets": ("Controlled expectation data boundary; never a fact.", get_prediction_markets),
                "get_sentiment_inputs": ("A/H sentiment inputs without Reddit/StockTwits proxying.", get_sentiment_inputs),
            }
            self._tools = {
                name: _tool(name, description, function)
                for name, (description, function) in specs.items()
            }
        return dict(self._tools)

    def tool_categories(self) -> Mapping[str, tuple[Any, ...]]:
        tools = self.registered_tools()
        return {
            "core_stock_apis": (tools["get_stock_data"],),
            "technical_indicators": (
                tools["get_indicators"],
                tools["get_verified_market_snapshot"],
            ),
            "fundamental_data": tuple(
                tools[name]
                for name in (
                    "get_fundamentals",
                    "get_balance_sheet",
                    "get_cashflow",
                    "get_income_statement",
                )
            ),
            "news_data": tuple(
                tools[name]
                for name in ("get_news", "get_global_news", "get_insider_transactions")
            ),
            "macro_data": (tools["get_macro_indicators"],),
            "prediction_markets": (tools["get_prediction_markets"],),
            "sentiment": (tools["get_sentiment_inputs"],),
            "index_identity": (tools["get_index_identity"],),
            "index_constituents": (tools["get_index_constituents"],),
            "market_breadth": (tools["get_market_breadth"],),
            "sector_rotation": (tools["get_sector_rotation"],),
            "market_liquidity": (tools["get_market_liquidity"],),
            "fund_identity": (tools["get_fund_identity"],),
            "fund_valuation": (tools["get_fund_profile"], tools["get_fund_nav"]),
            "fund_disclosures": (
                tools["get_fund_holdings"],
                tools["get_fund_manager"],
                tools["get_fund_share"],
            ),
            "fund_terms": (
                tools["get_fund_fees"],
                tools["get_fund_subscription_redemption"],
            ),
            "etf_tracking": (
                tools["get_etf_constituents"],
                tools["get_etf_tracking"],
                tools["get_etf_liquidity"],
            ),
        }

    def market_analyst_tools(self) -> tuple[Any, ...]:
        tools = self.registered_tools()
        return tuple(
            tools[name]
            for name in (
                "get_stock_data",
                "get_indicators",
                "get_verified_market_snapshot",
            )
        )

    def news_analyst_tools(self) -> tuple[Any, ...]:
        tools = self.registered_tools()
        return tuple(
            tools[name]
            for name in (
                "get_news",
                "get_global_news",
                "get_macro_indicators",
                "get_prediction_markets",
            )
        )

    def fundamentals_analyst_tools(self) -> tuple[Any, ...]:
        return self.tool_categories()["fundamental_data"]

    def index_identity_tools(self) -> tuple[Any, ...]:
        return self.tool_categories()["index_identity"]

    def index_technical_tools(self) -> tuple[Any, ...]:
        return self.market_analyst_tools()

    def index_participation_tools(self) -> tuple[Any, ...]:
        tools = self.registered_tools()
        return tuple(
            tools[name]
            for name in ("get_market_breadth", "get_market_liquidity")
        )

    def index_composition_tools(self) -> tuple[Any, ...]:
        tools = self.registered_tools()
        return tuple(
            tools[name]
            for name in ("get_index_constituents", "get_sector_rotation")
        )

    def index_macro_policy_tools(self) -> tuple[Any, ...]:
        tools = self.registered_tools()
        return tuple(
            tools[name]
            for name in ("get_news", "get_global_news", "get_macro_indicators")
        )


__all__ = [
    "ADAPTER_VERSION",
    "DEFAULT_PROVIDER_CHAINS",
    "DERIVED_PROVIDER_KEY",
    "SUPPORTED_INDICATORS",
    "TradingAgentsCNDataAdapter",
    "TradingAgentsCNDataError",
    "TradingAgentsCNRunContext",
    "build_default_financial_provider_router",
]

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Exchange-aware automatic market snapshots and lightweight overviews."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

import config
from financial_config import financial_capabilities
from financial_instruments import InstrumentRegistry
from financial_market_clock import MarketClockService, RequestTimeContext
from financial_provider_contract import (
    FinancialDataKind,
    FinancialDataRequest,
    FinancialProviderError,
    MarketStatus,
)
from financial_universe_planner import FinancialUniversePlanner
from financial_worker_jobs import FinancialJobExecutionError
from tradingagents_cn_data_adapter import (
    DEFAULT_PROVIDER_CHAINS,
    build_default_financial_provider_router,
)


UTC = timezone.utc
SCHEDULER_VERSION = "financial-market-scheduler-v2"
SAFE_EVENT_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,120}$")


@dataclass(frozen=True)
class StandardMarketScope:
    universe_key: str
    calendar_id: str


@dataclass(frozen=True)
class FixedHomeIndexScope:
    canonical_symbol: str
    calendar_id: str


STANDARD_MARKET_SCOPES = (
    StandardMarketScope("CN_XSHG_MARKET", "XSHG"),
    StandardMarketScope("CN_XSHE_MARKET", "XSHE"),
    StandardMarketScope("CN_A_MARKET", "XSHG"),
    StandardMarketScope("HK_MARKET", "XHKG"),
)
FIXED_HOME_INDEX_SCOPES = (
    FixedHomeIndexScope("000001.SH", "XSHG"),
    FixedHomeIndexScope("399001.SZ", "XSHE"),
    FixedHomeIndexScope("HSI.HK", "XHKG"),
    FixedHomeIndexScope("IXIC.US", "XNAS"),
    FixedHomeIndexScope("N225.JP", "XTKS"),
)
DEFAULT_PULSE_UNIVERSE = "DEFAULT_MARKET_PULSE"


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _utc_text(value: datetime) -> str:
    return _utc(value, "datetime").isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _stable_run_id(prefix: str, identity: object) -> str:
    return f"{prefix}-{_digest(identity)[:32]}"


def _setting(settings: object, name: str, default: object) -> object:
    if isinstance(settings, Mapping):
        return settings.get(name, default)
    return getattr(settings, name, default)


class FinancialMarketJobService:
    """Concrete snapshot/overview runners injected into the existing worker."""

    def __init__(
        self,
        repository,
        *,
        settings=None,
        router=None,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self.repository = repository
        self.settings = config if settings is None else settings
        self.repository.db._ensure_connection()
        self.connection = self.repository.db.connection
        self.instruments = InstrumentRegistry(self.connection)
        self.universes = FinancialUniversePlanner(
            self.connection, instrument_registry=self.instruments
        )
        self.universes.load_controlled_seed()
        self.router = router or build_default_financial_provider_router(
            self.connection,
            settings=self.settings,
            clock=clock or (lambda: datetime.now(UTC)),
        )

    def runners(self):
        return {
            "financial_snapshot": self.run_snapshot,
            "market_overview": self.run_overview,
        }

    def _ensure_instrument_run(
        self,
        run_id: str,
        instrument_id: int,
        payload: Mapping[str, Any],
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO financial_research_runs(
                id, trigger_type, scope_type, instrument_id, status,
                current_stage, time_context_json, config_json, requested_at,
                started_at, updated_at
            ) VALUES(?, 'auto_market', 'instrument', ?, 'running',
                     'financial_snapshot', ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status='running', current_stage='financial_snapshot',
                started_at=COALESCE(financial_research_runs.started_at, excluded.started_at),
                updated_at=excluded.updated_at
            """,
            (
                run_id,
                int(instrument_id),
                _canonical_json(payload.get("market_session") or {}),
                _canonical_json(
                    {
                        "scheduler_version": SCHEDULER_VERSION,
                        "schedule_window": payload.get("schedule_window"),
                    }
                ),
                str(payload["requested_at_utc"]),
                str(payload["requested_at_utc"]),
                str(payload["requested_at_utc"]),
            ),
        )

    def run_snapshot(self, payload: Mapping[str, Any], context) -> Mapping[str, Any]:
        context.raise_if_cancelled()
        symbol = str(payload.get("canonical_symbol") or "").strip()
        instrument = self.instruments.get_by_canonical_symbol(symbol)
        if instrument is None or instrument.asset_type != "index":
            raise FinancialJobExecutionError(
                "automatic market snapshot requires a registered index",
                error_code="invalid_automatic_market_instrument",
                retryable=False,
            )
        requested_at = _parse_utc(payload.get("requested_at_utc"))
        run_id = str(payload.get("research_run_id") or "").strip()
        if not run_id:
            run_id = _stable_run_id(
                "auto-snapshot",
                {"symbol": symbol, "window": payload.get("schedule_window")},
            )
        self._ensure_instrument_run(run_id, instrument.instrument_id, payload)
        phase = str(payload.get("phase") or "")
        snapshot_kind = str(payload.get("snapshot_kind") or "benchmark")
        live_phase = phase in {"pre_open", "open_initial", "intraday", "lunch"}
        if snapshot_kind == "market_breadth":
            data_kind = FinancialDataKind.MACRO
            endpoint = "market_breadth"
            metric = "market_breadth"
            chain_key = "market_breadth"
        else:
            data_kind = FinancialDataKind.QUOTE if live_phase else FinancialDataKind.BAR
            endpoint = "quote" if live_phase else "bars"
            metric = "last_price" if live_phase else "ohlcv"
            chain_key = "quote" if live_phase else "bars"
        default_chain = tuple(
            DEFAULT_PROVIDER_CHAINS.get(instrument.market, {}).get(chain_key, ())
        )
        requested_chain = tuple(
            str(value) for value in payload.get("provider_chain") or () if str(value)
        )
        if requested_chain and not set(requested_chain).issubset(default_chain):
            raise FinancialJobExecutionError(
                "automatic snapshot provider chain exceeds the authorized market chain",
                error_code="provider_not_authorized",
                retryable=False,
            )
        chain = requested_chain or default_chain
        local_zone = ZoneInfo(
            {
                "CN": "Asia/Shanghai",
                "CN_FUND": "Asia/Shanghai",
                "HK": "Asia/Hong_Kong",
                "XHKG": "Asia/Hong_Kong",
                "US": "America/New_York",
                "JP": "Asia/Tokyo",
            }.get(instrument.market, "Asia/Hong_Kong")
        )
        local_day = requested_at.astimezone(local_zone).date()
        if snapshot_kind == "market_breadth":
            parameters = {
                "as_of": local_day.isoformat(),
                "exchange": str(payload.get("market_metric_exchange") or instrument.exchange),
            }
        elif live_phase:
            parameters = {"interval": "1m", "period": "1d"}
        else:
            parameters = {
                "start": (local_day - timedelta(days=14)).isoformat(),
                "end": local_day.isoformat(),
                "interval": "1d",
                "adjustment": "raw",
            }
        request_id = str(payload.get("request_id") or _stable_run_id("request", run_id))
        if not chain:
            result = {
                "status": "unavailable",
                "error_code": "no_authorized_provider_for_market",
                "snapshot_ids": [],
            }
        else:
            request = FinancialDataRequest(
                request_id=request_id,
                endpoint=endpoint,
                instrument_id=str(instrument.instrument_id),
                metric=metric,
                data_kind=data_kind,
                requested_as_of=requested_at,
                preferred_provider_id=chain[0],
                parameters=parameters,
            )
            try:
                response, snapshot_ids = self.router.fetch_and_persist(
                    request,
                    candidate_provider_ids=chain,
                    allow_fallback=True,
                )
                result = {
                    "status": "completed",
                    "error_code": "",
                    "snapshot_ids": list(snapshot_ids),
                    "provider_id": response.provider_id,
                    "degraded": response.degradation.degraded,
                    "degradation_reason": response.degradation.reason,
                }
            except FinancialProviderError as exc:
                result = {
                    "status": "unavailable",
                    "error_code": exc.code.value,
                    "snapshot_ids": [],
                    "retryable": exc.retryable,
                }
        completion_status = "completed" if result["snapshot_ids"] else "degraded"
        self.connection.execute(
            """
            UPDATE financial_research_runs
            SET status=?, current_stage='financial_snapshot_complete', last_error=?,
                completed_at=?, updated_at=? WHERE id=?
            """,
            (
                completion_status,
                str(result.get("error_code") or ""),
                _utc_text(requested_at),
                _utc_text(requested_at),
                run_id,
            ),
        )
        context.raise_if_cancelled()
        if not result["snapshot_ids"] and bool(result.get("retryable")):
            raise FinancialJobExecutionError(
                "automatic market snapshot provider is temporarily unavailable",
                error_code=str(result.get("error_code") or "provider_temporarily_unavailable"),
                retryable=True,
            )
        return {
            **result,
            "research_run_id": run_id,
            "canonical_symbol": symbol,
            "snapshot_kind": snapshot_kind,
            "market_metric": str(payload.get("market_metric") or ""),
            "data_kind": data_kind.value,
            "market_session_state": (payload.get("market_session") or {}).get(
                "market_session_state", "unknown"
            ),
            "requested_at_utc": _utc_text(requested_at),
        }

    def _latest_snapshot(self, instrument_id: int):
        return self.connection.execute(
            """
            SELECT id, data_type, observed_at, fetched_at, market_status,
                   quality_status, payload_json, payload_sha256
            FROM financial_data_snapshots
            WHERE instrument_id=?
              AND data_type IN ('quote', 'bar', 'verified_market_snapshot')
            ORDER BY observed_at DESC, fetched_at DESC, id DESC LIMIT 1
            """,
            (int(instrument_id),),
        ).fetchone()

    def _latest_market_metric(self, requirement: str):
        metric_name, _, exchange = str(requirement or "").partition(":")
        if metric_name != "breadth" or not exchange:
            return None
        rows = self.connection.execute(
            """
            SELECT s.id, s.data_type, s.observed_at, s.fetched_at,
                   s.market_status, s.quality_status, s.payload_json,
                   s.payload_sha256
            FROM financial_data_snapshots s
            JOIN financial_instruments i ON i.id=s.instrument_id
            WHERE s.data_type='macro' AND i.exchange=?
            ORDER BY s.observed_at DESC, s.fetched_at DESC, s.id DESC
            """,
            (exchange,),
        ).fetchall()
        for row in rows:
            payload_text = str(row[6])
            if hashlib.sha256(payload_text.encode("utf-8")).hexdigest() != str(row[7]):
                continue
            try:
                stored = json.loads(payload_text)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if stored.get("metric") != "market_breadth":
                continue
            normalized = stored.get("normalized_payload") or {}
            stored_exchange = str(normalized.get("exchange") or exchange).upper()
            if stored_exchange not in {exchange, "ALL"}:
                continue
            return row
        return None

    def run_overview(self, payload: Mapping[str, Any], context) -> Mapping[str, Any]:
        context.raise_if_cancelled()
        universe_key = str(payload.get("universe_key") or "").strip()
        requested_at = _parse_utc(payload.get("requested_at_utc"))
        local_day = requested_at.astimezone(ZoneInfo("Asia/Hong_Kong")).date().isoformat()
        universe = self.universes.get_universe(universe_key, as_of=local_day)
        members = self.universes.members_as_of(universe_key, as_of=local_day)
        run_id = str(payload.get("research_run_id") or "").strip() or _stable_run_id(
            "auto-overview",
            {"universe": universe_key, "window": payload.get("schedule_window")},
        )
        self.connection.execute(
            """
            INSERT INTO financial_research_runs(
                id, trigger_type, scope_type, universe_id, status, current_stage,
                time_context_json, config_json, requested_at, started_at, updated_at
            ) VALUES(?, 'auto_market', 'universe', ?, 'running', 'market_overview',
                     ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status='running', current_stage='market_overview',
                started_at=COALESCE(financial_research_runs.started_at, excluded.started_at),
                updated_at=excluded.updated_at
            """,
            (
                run_id,
                universe.universe_id,
                _canonical_json(payload.get("market_sessions") or {}),
                _canonical_json(
                    {
                        "scheduler_version": SCHEDULER_VERSION,
                        "schedule_window": payload.get("schedule_window"),
                    }
                ),
                _utc_text(requested_at),
                _utc_text(requested_at),
                _utc_text(requested_at),
            ),
        )
        snapshots = []
        missing = []
        for member in members:
            row = self._latest_snapshot(member.instrument.instrument_id)
            if row is None:
                missing.append(member.instrument.canonical_symbol)
                continue
            payload_text = str(row[6])
            if hashlib.sha256(payload_text.encode("utf-8")).hexdigest() != str(row[7]):
                missing.append(member.instrument.canonical_symbol)
                continue
            snapshots.append(
                {
                    "snapshot_id": int(row[0]),
                    "canonical_symbol": member.instrument.canonical_symbol,
                    "display_name": member.instrument.display_name,
                    "data_type": str(row[1]),
                    "observed_at": str(row[2]),
                    "fetched_at": str(row[3]),
                    "market_status": str(row[4]),
                    "quality_status": str(row[5]),
                }
            )
        required_market_metrics = tuple(
            str(item) for item in universe.definition.get("required_market_metrics", ())
        )
        market_metrics = []
        missing_market_metrics = []
        for requirement in required_market_metrics:
            row = self._latest_market_metric(requirement)
            if row is None:
                missing_market_metrics.append(requirement)
                continue
            market_metrics.append(
                {
                    "requirement": requirement,
                    "snapshot_id": int(row[0]),
                    "data_type": str(row[1]),
                    "observed_at": str(row[2]),
                    "fetched_at": str(row[3]),
                    "market_status": str(row[4]),
                    "quality_status": str(row[5]),
                }
            )
        total = len(members)
        coverage_denominator = total + len(required_market_metrics)
        coverage_numerator = len(snapshots) + len(market_metrics)
        coverage = (
            coverage_numerator / coverage_denominator
            if coverage_denominator
            else 0.0
        )
        status = "market_overview" if coverage == 1.0 else "degraded_unverified"
        report_json = {
            "schema_version": 1,
            "report_type": "lightweight_market_overview",
            "scheduler_version": SCHEDULER_VERSION,
            "universe": {
                "universe_id": universe.universe_id,
                "universe_key": universe.universe_key,
                "display_name": universe.display_name,
                "definition_version": universe.definition_version,
                "constituent_as_of": universe.constituent_as_of,
            },
            "requested_at_utc": _utc_text(requested_at),
            "schedule_window": str(payload.get("schedule_window") or ""),
            "phase": str(payload.get("phase") or ""),
            "market_sessions": dict(payload.get("market_sessions") or {}),
            "snapshots": snapshots,
            "market_metrics": market_metrics,
            "required_market_metrics": list(required_market_metrics),
            "snapshot_ids": [item["snapshot_id"] for item in snapshots]
            + [item["snapshot_id"] for item in market_metrics],
            "missing_symbols": missing,
            "missing_market_metrics": missing_market_metrics,
            "coverage": coverage,
            "boundary": (
                "This is a deterministic snapshot overview, not a full TradingAgents "
                "research report and not an order instruction."
            ),
        }
        title = f"{universe.display_name} · {str(payload.get('phase') or 'latest')}"
        lines = [
            f"# {title}",
            "",
            f"- 服务器时间：{_utc_text(requested_at)}",
            f"- 证据覆盖：{coverage_numerator}/{coverage_denominator} ({coverage:.0%})",
            f"- 调度窗口：{str(payload.get('schedule_window') or '')}",
            "",
            "## 最新基准快照",
            "",
        ]
        lines.extend(
            f"- {item['display_name']}（{item['canonical_symbol']}）："
            f"snapshot #{item['snapshot_id']}，观测时间 {item['observed_at']}，"
            f"状态 {item['market_status']}"
            for item in snapshots
        )
        if missing:
            lines.extend(("", "## 数据缺口", "", "- " + "、".join(missing)))
        if market_metrics:
            lines.extend(("", "## 市场宽度", ""))
            lines.extend(
                f"- {item['requirement']}：snapshot #{item['snapshot_id']}，"
                f"观测时间 {item['observed_at']}"
                for item in market_metrics
            )
        if missing_market_metrics:
            lines.extend(
                (
                    "",
                    "## 市场指标缺口",
                    "",
                    "- " + "、".join(missing_market_metrics),
                )
            )
        lines.extend(
            (
                "",
                "## 边界",
                "",
                "这是轻量市场快照概览，不是完整 TradingAgents 研究报告，也不是交易指令。",
            )
        )
        report_markdown = "\n".join(lines)
        self.connection.execute(
            """
            INSERT INTO financial_final_reports(
                research_run_id, report_version, report_status, recommendation,
                confidence, title, executive_summary, report_markdown,
                report_json, suitability_notice, disclaimer,
                observed_at, fetched_at
            ) VALUES(?, 1, ?, 'insufficient_evidence', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(research_run_id, report_version) DO UPDATE SET
                report_status=excluded.report_status,
                confidence=excluded.confidence,
                title=excluded.title,
                executive_summary=excluded.executive_summary,
                report_markdown=excluded.report_markdown,
                report_json=excluded.report_json,
                observed_at=excluded.observed_at,
                fetched_at=excluded.fetched_at,
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (
                run_id,
                status,
                coverage,
                title,
                f"最新证据覆盖 {coverage_numerator}/{coverage_denominator}；缺口已显式标注。",
                report_markdown,
                _canonical_json(report_json),
                "仅供市场研究参考。",
                "不构成投资建议、要约或交易指令。",
                max((item["observed_at"] for item in snapshots), default=None),
                _utc_text(requested_at),
            ),
        )
        report_row = self.connection.execute(
            "SELECT id FROM financial_final_reports WHERE research_run_id=? AND report_version=1",
            (run_id,),
        ).fetchone()
        report_id = int(report_row[0])
        self.connection.execute(
            """
            UPDATE financial_research_runs
            SET status='completed', current_stage='market_overview_complete',
                completed_at=?, updated_at=? WHERE id=?
            """,
            (_utc_text(requested_at), _utc_text(requested_at), run_id),
        )
        context.raise_if_cancelled()
        return {
            "status": "completed" if coverage else "degraded",
            "research_run_id": run_id,
            "report_id": report_id,
            "universe_key": universe_key,
            "coverage": coverage,
            "snapshot_ids": report_json["snapshot_ids"],
            "missing_symbols": missing,
            "missing_market_metrics": missing_market_metrics,
            "report_type": "lightweight_market_overview",
        }


class FinancialMarketScheduler:
    """Produces bounded schedule windows from server time and exchange clocks."""

    def __init__(
        self,
        repository,
        *,
        settings=None,
        market_clock: Optional[MarketClockService] = None,
    ):
        self.repository = repository
        self.settings = config if settings is None else settings
        self.market_clock = market_clock or MarketClockService()
        self.repository.db._ensure_connection()
        self.connection = self.repository.db.connection
        self.instruments = InstrumentRegistry(self.connection)
        self.universes = FinancialUniversePlanner(
            self.connection, instrument_registry=self.instruments
        )
        self.universes.load_controlled_seed()

    @staticmethod
    def _phase_and_window(session, now_utc: datetime):
        day = session.trading_date.isoformat()
        state = session.market_session_state
        if state == MarketStatus.PRE_OPEN:
            return "pre_open", f"{day}:pre-open"
        if state == MarketStatus.OPEN:
            if session.session_open_utc and now_utc < session.session_open_utc + timedelta(minutes=3):
                return "open_initial", f"{day}:open-initial"
            bucket = int(now_utc.timestamp() // 300)
            return "intraday", f"{day}:intraday:{bucket}"
        if state == MarketStatus.LUNCH_BREAK:
            return "lunch", f"{day}:lunch"
        if session.reason == "after_close":
            if session.session_close_utc and now_utc < session.session_close_utc + timedelta(minutes=5):
                return None, None
            return "post_close", f"{day}:post-close"
        if not session.is_trading_day:
            return "closed_latest", f"{day}:closed"
        return "overnight", f"{day}:overnight"

    def _latest_snapshot_fetched_at(
        self,
        instrument_id: int,
        *,
        snapshot_kind: str = "benchmark",
    ) -> Optional[datetime]:
        data_types = (
            ("macro",)
            if snapshot_kind == "market_breadth"
            else ("quote", "bar", "verified_market_snapshot")
        )
        placeholders = ",".join("?" for _ in data_types)
        row = self.connection.execute(
            f"""
            SELECT fetched_at FROM financial_data_snapshots
            WHERE instrument_id=?
              AND data_type IN ({placeholders})
            ORDER BY fetched_at DESC, id DESC LIMIT 1
            """,
            (int(instrument_id), *data_types),
        ).fetchone()
        return _parse_utc(row[0]) if row else None

    def _snapshot_is_fresh(
        self,
        instrument_id: int,
        session,
        now_utc: datetime,
        *,
        snapshot_kind: str = "benchmark",
    ) -> bool:
        fetched_at = self._latest_snapshot_fetched_at(
            instrument_id,
            snapshot_kind=snapshot_kind,
        )
        if fetched_at is None or fetched_at > now_utc:
            return False
        state = session.market_session_state
        if state == MarketStatus.OPEN:
            threshold = int(
                _setting(self.settings, "FINANCIAL_QUOTE_FRESHNESS_SECONDS", 300)
            )
        elif state in {MarketStatus.PRE_OPEN, MarketStatus.LUNCH_BREAK}:
            threshold = max(
                900,
                int(
                    _setting(
                        self.settings,
                        "FINANCIAL_QUOTE_FRESHNESS_SECONDS",
                        300,
                    )
                ),
            )
        elif session.is_trading_day:
            threshold = 86_400
        else:
            threshold = 259_200
        return (now_utc - fetched_at).total_seconds() <= threshold

    @classmethod
    def _query_phase_and_window(cls, session, now_utc: datetime):
        """Return a query-safe window, including the post-close settle buffer.

        Periodic scheduling intentionally waits five minutes after the close.
        An interactive broad-market question must not disappear during that
        interval, so it gets an explicit settling state and a daily-bar fetch.
        """

        phase, window = cls._phase_and_window(session, now_utc)
        if phase is not None:
            return phase, window
        day = session.trading_date.isoformat()
        return "post_close_settling", f"{day}:post-close-settling"

    def enqueue_scope_refresh(
        self,
        universe_key: str,
        *,
        now: Optional[datetime] = None,
        request_id: str = "",
    ) -> Mapping[str, Any]:
        """Queue one bounded interactive market scope on the existing worker.

        This path is deliberately smaller than ``enqueue_due_jobs``: it needs
        only Financial Intelligence, refreshes only the requested standard
        universe, never creates a full TradingAgents research run, and uses a
        one-minute idempotency bucket so concurrent chat requests coalesce.
        """

        normalized = str(universe_key or "").strip().upper()
        allowed = {scope.universe_key for scope in STANDARD_MARKET_SCOPES} | {
            DEFAULT_PULSE_UNIVERSE
        }
        if normalized not in allowed:
            raise ValueError("unsupported interactive market universe")
        state = financial_capabilities(self.settings)
        if not state["effective"]["financial_intelligence"]:
            return {
                "status": "skipped",
                "reason": state["reasons"]["financial_intelligence"],
                "universe_key": normalized,
                "created": 0,
                "existing": 0,
                "jobs": [],
                "full_research_jobs_created": 0,
            }

        now_utc = _utc(now or datetime.now(UTC), "now")
        local_as_of = now_utc.astimezone(ZoneInfo("Asia/Hong_Kong")).date().isoformat()
        context = RequestTimeContext(
            server_now_utc=now_utc,
            server_timezone="Asia/Hong_Kong",
            user_timezone="Asia/Hong_Kong",
        )
        universe = self.universes.get_universe(normalized, as_of=local_as_of)
        members = self.universes.members_as_of(normalized, as_of=local_as_of)
        required_metrics = tuple(
            str(item)
            for item in universe.definition.get("required_market_metrics", ())
        )
        calendar_ids = {
            str(member.instrument.exchange or "XSHG") for member in members
        }
        calendar_ids.update(
            metric.partition(":")[2]
            for metric in required_metrics
            if metric.startswith("breadth:") and metric.partition(":")[2]
        )
        sessions = {
            calendar_id: self.market_clock.market_state(calendar_id, context)
            for calendar_id in sorted(calendar_ids)
        }
        session_payload = {
            calendar_id: session.to_dict()
            for calendar_id, session in sessions.items()
        }
        phase_windows = {
            calendar_id: self._query_phase_and_window(session, now_utc)
            for calendar_id, session in sessions.items()
        }
        phases = {item[0] for item in phase_windows.values()}
        phase = next(iter(phases)) if len(phases) == 1 else "cross_market_scope"
        query_bucket = int(now_utc.timestamp() // 60)
        window_digest = _digest(
            {
                "universe": normalized,
                "windows": {
                    key: value[1] for key, value in phase_windows.items()
                },
            }
        )[:16]
        overview_window = f"chat:{query_bucket}:{window_digest}"
        safe_request_id = str(request_id or "").strip()
        if not safe_request_id:
            safe_request_id = _stable_run_id(
                "request", {"universe": normalized, "bucket": query_bucket}
            )

        snapshot_requests: Dict[tuple[str, str], Dict[str, Any]] = {}
        for member in members:
            calendar_id = str(member.instrument.exchange or "XSHG")
            session = sessions[calendar_id]
            member_phase, member_window = phase_windows[calendar_id]
            if self._snapshot_is_fresh(
                member.instrument.instrument_id, session, now_utc
            ):
                continue
            effective_window = f"{calendar_id}:{member_window}:chat:{query_bucket}"
            snapshot_requests[(member.instrument.canonical_symbol, effective_window)] = {
                "canonical_symbol": member.instrument.canonical_symbol,
                "phase": member_phase,
                "schedule_window": effective_window,
                "market_session": session.to_dict(),
                "universe_keys": [normalized],
            }

        for requirement in required_metrics:
            metric_name, _, metric_exchange = requirement.partition(":")
            if metric_name != "breadth" or metric_exchange not in sessions:
                continue
            representative = next(
                (
                    member
                    for member in members
                    if member.instrument.exchange == metric_exchange
                ),
                None,
            )
            if representative is None:
                continue
            metric_session = sessions[metric_exchange]
            metric_phase, metric_window = phase_windows[metric_exchange]
            if self._snapshot_is_fresh(
                representative.instrument.instrument_id,
                metric_session,
                now_utc,
                snapshot_kind="market_breadth",
            ):
                continue
            effective_window = (
                f"{metric_exchange}:{metric_window}:chat:{query_bucket}"
            )
            snapshot_requests[(f"metric:{requirement}", effective_window)] = {
                "canonical_symbol": representative.instrument.canonical_symbol,
                "snapshot_kind": "market_breadth",
                "market_metric": requirement,
                "market_metric_exchange": metric_exchange,
                "phase": metric_phase,
                "schedule_window": effective_window,
                "market_session": metric_session.to_dict(),
                "universe_keys": [normalized],
            }

        jobs = []
        for request in snapshot_requests.values():
            payload = {
                **request,
                "requested_at_utc": _utc_text(now_utc),
                "trigger": "chat_query",
                "scheduler_version": SCHEDULER_VERSION,
            }
            identity = str(
                payload.get("market_metric") or payload["canonical_symbol"]
            )
            payload["research_run_id"] = _stable_run_id(
                "chat-snapshot",
                {
                    "identity": identity,
                    "window": payload["schedule_window"],
                },
            )
            payload["request_id"] = _stable_run_id(
                "request", {"chat": safe_request_id, "run": payload["research_run_id"]}
            )
            job_id, created = self.repository.enqueue_job_once(
                "financial_snapshot",
                f"financial-snapshot:chat:{identity}:{payload['schedule_window']}",
                payload,
                priority=60,
                max_attempts=3,
                request_id=payload["request_id"],
                created_by="financial_chat_market_scope",
            )
            jobs.append(
                {"job_id": job_id, "job_type": "financial_snapshot", "created": created}
            )

        overview_payload = {
            "universe_key": normalized,
            "phase": phase,
            "schedule_window": overview_window,
            "market_sessions": session_payload,
            "symbols": [
                member.instrument.canonical_symbol for member in members
            ],
            "requested_at_utc": _utc_text(now_utc),
            "trigger": "chat_query",
            "scheduler_version": SCHEDULER_VERSION,
        }
        overview_payload["research_run_id"] = _stable_run_id(
            "chat-overview",
            {"universe": normalized, "window": overview_window},
        )
        overview_job_id, overview_created = self.repository.enqueue_job_once(
            "market_overview",
            f"market-overview:chat:{normalized}:{overview_window}",
            overview_payload,
            priority=40,
            max_attempts=2,
            request_id=_stable_run_id(
                "request",
                {
                    "chat": safe_request_id,
                    "run": overview_payload["research_run_id"],
                },
            ),
            created_by="financial_chat_market_scope",
        )
        jobs.append(
            {
                "job_id": overview_job_id,
                "job_type": "market_overview",
                "created": overview_created,
            }
        )
        return {
            "status": "scheduled",
            "trigger": "chat_query",
            "universe_key": normalized,
            "server_now_utc": _utc_text(now_utc),
            "phase": phase,
            "schedule_window": overview_window,
            "market_sessions": session_payload,
            "symbols": overview_payload["symbols"],
            "required_market_metrics": list(required_metrics),
            "overview_research_run_id": overview_payload["research_run_id"],
            "created": sum(1 for item in jobs if item["created"]),
            "existing": sum(1 for item in jobs if not item["created"]),
            "jobs": jobs,
            "full_research_jobs_created": 0,
        }

    def enqueue_due_jobs(
        self,
        *,
        now: Optional[datetime] = None,
        trigger: str = "periodic",
        event_key: str = "",
    ) -> Mapping[str, Any]:
        now_utc = _utc(now or datetime.now(UTC), "now")
        trigger_name = str(trigger or "periodic").strip()
        if trigger_name not in {"startup", "periodic", "event"}:
            raise ValueError("unsupported market schedule trigger")
        if trigger_name == "event":
            if not SAFE_EVENT_KEY.fullmatch(str(event_key or "")):
                raise ValueError("event_key is required for event trigger")
        state = financial_capabilities(self.settings)
        # These are deterministic snapshot/overview jobs, not TradingAgents
        # research.  Keep homepage market data refreshing when the optional
        # automatic-research product is disabled.
        if not state["effective"]["financial_intelligence"]:
            return {
                "status": "skipped",
                "reason": state["reasons"]["financial_intelligence"],
                "required_capability": "financial_intelligence",
                "created": 0,
                "existing": 0,
                "jobs": [],
                "full_research_jobs_created": 0,
            }
        context = RequestTimeContext(
            server_now_utc=now_utc,
            server_timezone="Asia/Hong_Kong",
            user_timezone="Asia/Hong_Kong",
        )
        snapshot_requests: Dict[tuple[str, str], Dict[str, Any]] = {}
        overview_requests = []
        pulse_sessions = {}
        pulse_windows = []
        local_as_of = now_utc.astimezone(ZoneInfo("Asia/Hong_Kong")).date().isoformat()

        for scope in STANDARD_MARKET_SCOPES:
            session = self.market_clock.market_state(scope.calendar_id, context)
            phase, window = self._phase_and_window(session, now_utc)
            pulse_sessions[scope.universe_key] = session.to_dict()
            if phase is None:
                continue
            event_suffix = (
                f":event:{_digest(event_key)[:12]}" if trigger_name == "event" else ""
            )
            effective_window = f"{scope.calendar_id}:{window}{event_suffix}"
            pulse_windows.append(f"{scope.universe_key}={effective_window}")
            members = self.universes.members_as_of(scope.universe_key, as_of=local_as_of)
            symbols = [item.instrument.canonical_symbol for item in members]
            for member in members:
                member_calendar = member.instrument.exchange or scope.calendar_id
                member_session = (
                    session
                    if member_calendar == scope.calendar_id
                    else self.market_clock.market_state(member_calendar, context)
                )
                member_phase, member_window = self._phase_and_window(
                    member_session, now_utc
                )
                if member_phase is None:
                    continue
                member_effective_window = (
                    f"{member_calendar}:{member_window}{event_suffix}"
                )
                milestone = member_phase in {
                    "overnight",
                    "closed_latest",
                    "pre_open",
                    "open_initial",
                    "lunch",
                    "post_close",
                }
                key = (
                    member.instrument.canonical_symbol,
                    member_effective_window,
                )
                fresh = self._snapshot_is_fresh(
                    member.instrument.instrument_id, member_session, now_utc
                )
                if milestone or trigger_name == "event" or not fresh:
                    request = snapshot_requests.setdefault(
                        key,
                        {
                            "canonical_symbol": member.instrument.canonical_symbol,
                            "phase": member_phase,
                            "schedule_window": member_effective_window,
                            "market_session": member_session.to_dict(),
                            "universe_keys": set(),
                        },
                    )
                    request["universe_keys"].add(scope.universe_key)
            for requirement in self.universes.get_universe(
                scope.universe_key, as_of=local_as_of
            ).definition.get("required_market_metrics", ()):
                metric_name, _, metric_exchange = str(requirement).partition(":")
                if metric_name != "breadth" or not metric_exchange:
                    continue
                representative = next(
                    (
                        item
                        for item in members
                        if item.instrument.exchange == metric_exchange
                    ),
                    members[0] if members else None,
                )
                if representative is None:
                    continue
                metric_session = self.market_clock.market_state(
                    metric_exchange, context
                )
                metric_phase, metric_window = self._phase_and_window(
                    metric_session, now_utc
                )
                if metric_phase is None:
                    continue
                metric_effective_window = (
                    f"{metric_exchange}:{metric_window}{event_suffix}"
                )
                key = (f"metric:{requirement}", metric_effective_window)
                metric_fresh = self._snapshot_is_fresh(
                    representative.instrument.instrument_id,
                    metric_session,
                    now_utc,
                    snapshot_kind="market_breadth",
                )
                metric_milestone = metric_phase in {
                    "overnight",
                    "closed_latest",
                    "pre_open",
                    "open_initial",
                    "lunch",
                    "post_close",
                }
                if metric_milestone or trigger_name == "event" or not metric_fresh:
                    request = snapshot_requests.setdefault(
                        key,
                        {
                            "canonical_symbol": representative.instrument.canonical_symbol,
                            "snapshot_kind": "market_breadth",
                            "market_metric": str(requirement),
                            "market_metric_exchange": metric_exchange,
                            "phase": metric_phase,
                            "schedule_window": metric_effective_window,
                            "market_session": metric_session.to_dict(),
                            "universe_keys": set(),
                        },
                    )
                    request["universe_keys"].add(scope.universe_key)
            overview_requests.append(
                {
                    "universe_key": scope.universe_key,
                    "phase": phase,
                    "schedule_window": effective_window,
                    "market_sessions": {scope.calendar_id: session.to_dict()},
                    "symbols": symbols,
                }
            )

        # These exact indices back the five fixed cards on the homepage.  They
        # are scheduled independently of broad-market universe membership so
        # a card cannot silently disappear when a universe definition changes.
        for fixed_scope in FIXED_HOME_INDEX_SCOPES:
            instrument = self.instruments.get_by_canonical_symbol(
                fixed_scope.canonical_symbol
            )
            if instrument is None or instrument.asset_type != "index":
                continue
            session = self.market_clock.market_state(fixed_scope.calendar_id, context)
            phase, window = self._phase_and_window(session, now_utc)
            if phase is None:
                continue
            event_suffix = (
                f":event:{_digest(event_key)[:12]}" if trigger_name == "event" else ""
            )
            effective_window = f"{fixed_scope.calendar_id}:{window}{event_suffix}"
            milestone = phase in {
                "overnight",
                "closed_latest",
                "pre_open",
                "open_initial",
                "lunch",
                "post_close",
            }
            fresh = self._snapshot_is_fresh(
                instrument.instrument_id, session, now_utc
            )
            if milestone or trigger_name == "event" or not fresh:
                request = snapshot_requests.setdefault(
                    (instrument.canonical_symbol, effective_window),
                    {
                        "canonical_symbol": instrument.canonical_symbol,
                        "phase": phase,
                        "schedule_window": effective_window,
                        "market_session": session.to_dict(),
                        "universe_keys": set(),
                    },
                )
                request["universe_keys"].add("HOME_INDEXES")
                # All five controlled mappings are covered by the already
                # approved Yahoo research adapter.  Pinning these homepage
                # snapshots avoids repeatedly waiting on a known-unavailable
                # premium source before the safe fallback is attempted.
                request["provider_chain"] = ["yahoo"]

        if pulse_windows:
            pulse_identity = _digest(sorted(pulse_windows))[:20]
            overview_requests.append(
                {
                    "universe_key": DEFAULT_PULSE_UNIVERSE,
                    "phase": "cross_market_pulse",
                    "schedule_window": f"pulse:{pulse_identity}",
                    "market_sessions": pulse_sessions,
                    "symbols": [
                        item.instrument.canonical_symbol
                        for item in self.universes.members_as_of(
                            DEFAULT_PULSE_UNIVERSE, as_of=local_as_of
                        )
                    ],
                }
            )

        jobs = []
        for request in snapshot_requests.values():
            payload = {
                **request,
                "universe_keys": sorted(request["universe_keys"]),
                "requested_at_utc": _utc_text(now_utc),
                "trigger": trigger_name,
                "scheduler_version": SCHEDULER_VERSION,
            }
            snapshot_identity = str(
                payload.get("market_metric") or payload["canonical_symbol"]
            )
            payload["research_run_id"] = _stable_run_id(
                "auto-snapshot",
                {
                    "snapshot_identity": snapshot_identity,
                    "window": payload["schedule_window"],
                },
            )
            payload["request_id"] = _stable_run_id("request", payload["research_run_id"])
            dedupe_key = (
                f"financial-snapshot:{snapshot_identity}:"
                f"{payload['schedule_window']}"
            )
            job_id, created = self.repository.enqueue_job_once(
                "financial_snapshot",
                dedupe_key,
                payload,
                priority=30,
                max_attempts=3,
                request_id=payload["request_id"],
                created_by="financial_market_scheduler",
            )
            jobs.append(
                {"job_id": job_id, "job_type": "financial_snapshot", "created": created}
            )

        for request in overview_requests:
            payload = {
                **request,
                "requested_at_utc": _utc_text(now_utc),
                "trigger": trigger_name,
                "scheduler_version": SCHEDULER_VERSION,
            }
            payload["research_run_id"] = _stable_run_id(
                "auto-overview",
                {
                    "universe": payload["universe_key"],
                    "window": payload["schedule_window"],
                },
            )
            dedupe_key = (
                f"market-overview:{payload['universe_key']}:"
                f"{payload['schedule_window']}"
            )
            job_id, created = self.repository.enqueue_job_once(
                "market_overview",
                dedupe_key,
                payload,
                priority=10,
                max_attempts=2,
                request_id=_stable_run_id("request", payload["research_run_id"]),
                created_by="financial_market_scheduler",
            )
            jobs.append(
                {"job_id": job_id, "job_type": "market_overview", "created": created}
            )
        return {
            "status": "scheduled",
            "trigger": trigger_name,
            "server_now_utc": _utc_text(now_utc),
            "created": sum(1 for item in jobs if item["created"]),
            "existing": sum(1 for item in jobs if not item["created"]),
            "jobs": jobs,
            "full_research_jobs_created": 0,
        }

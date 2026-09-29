#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Timezone-aware request clock, exchange sessions, and freshness decisions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time as wall_time, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, Mapping, Optional, Tuple

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from financial_provider_contract import FreshnessState, MarketStatus


DEFAULT_SERVER_TIMEZONE = "Asia/Hong_Kong"
DEFAULT_CALENDAR_DIR = Path(__file__).resolve().parent / "config" / "market_calendars"
TODAY_EXPRESSIONS = {"today", "今日", "今天", "当天", "當天", "当日", "當日"}
YESTERDAY_EXPRESSIONS = {"yesterday", "昨日", "昨天"}
THIS_WEEK_EXPRESSIONS = {"this week", "本周", "本週", "这周", "這週"}
NOW_EXPRESSIONS = {"now", "此时", "此時", "现在", "現在"}


def _zone(name: str) -> ZoneInfo:
    value = str(name or "").strip()
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone: {value}") from exc


def _aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


def _utc(value: datetime) -> datetime:
    return _aware(value, "datetime").astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return _utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_wall_time(value: str) -> wall_time:
    try:
        hour_text, minute_text = str(value).split(":", 1)
        parsed = wall_time(int(hour_text), int(minute_text))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid session time: {value}") from exc
    return parsed


def _parse_sessions(values: Iterable[Iterable[str]]) -> Tuple[Tuple[wall_time, wall_time], ...]:
    sessions = []
    previous_end = None
    for item in values or []:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("each market session must contain start and end")
        start, end = _parse_wall_time(item[0]), _parse_wall_time(item[1])
        if start >= end or (previous_end is not None and start < previous_end):
            raise ValueError("market sessions must be ordered and non-overlapping")
        sessions.append((start, end))
        previous_end = end
    if not sessions:
        raise ValueError("at least one market session is required")
    return tuple(sessions)


@dataclass(frozen=True)
class CalendarDefinition:
    calendar_id: str
    name: str
    timezone_name: str
    coverage_start: date
    coverage_end: date
    calendar_version: str
    pre_open_start: wall_time
    regular_sessions: Tuple[Tuple[wall_time, wall_time], ...]
    holidays: frozenset[date]
    half_days: Mapping[date, Tuple[Tuple[wall_time, wall_time], ...]]
    holiday_source_url: str
    hours_source_url: str

    @property
    def timezone(self) -> ZoneInfo:
        return _zone(self.timezone_name)

    def covers(self, value: date) -> bool:
        return self.coverage_start <= value <= self.coverage_end


@dataclass(frozen=True)
class RequestTimeContext:
    server_now_utc: datetime
    server_timezone: str
    user_timezone: str

    def __post_init__(self):
        object.__setattr__(self, "server_now_utc", _utc(self.server_now_utc))
        _zone(self.server_timezone)
        _zone(self.user_timezone)

    def to_dict(self) -> Dict[str, str]:
        return {
            "server_now_utc": _utc_text(self.server_now_utc),
            "server_timezone": self.server_timezone,
            "user_timezone": self.user_timezone,
        }


@dataclass(frozen=True)
class MarketSession:
    calendar_id: str
    market_timezone: str
    market_session_state: MarketStatus
    trading_date: date
    is_trading_day: bool
    is_half_day: bool
    calendar_source: str
    calendar_version: str
    holiday_source_url: str
    hours_source_url: str
    session_open_utc: Optional[datetime]
    session_close_utc: Optional[datetime]
    next_transition_utc: Optional[datetime]
    reason: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "market_calendar_id": self.calendar_id,
            "market_timezone": self.market_timezone,
            "market_session_state": self.market_session_state.value,
            "trading_date": self.trading_date.isoformat(),
            "is_trading_day": self.is_trading_day,
            "is_half_day": self.is_half_day,
            "calendar_source": self.calendar_source,
            "calendar_version": self.calendar_version,
            "holiday_source_url": self.holiday_source_url,
            "hours_source_url": self.hours_source_url,
            "session_open_utc": _utc_text(self.session_open_utc) if self.session_open_utc else None,
            "session_close_utc": _utc_text(self.session_close_utc) if self.session_close_utc else None,
            "next_transition_utc": _utc_text(self.next_transition_utc) if self.next_transition_utc else None,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ResolvedTimeRange:
    expression: str
    timezone_name: str
    start_utc: datetime
    end_utc: datetime
    resolved_at_utc: datetime

    def to_dict(self) -> Dict[str, str]:
        return {
            "expression": self.expression,
            "timezone": self.timezone_name,
            "start_utc": _utc_text(self.start_utc),
            "end_utc": _utc_text(self.end_utc),
            "resolved_at_utc": _utc_text(self.resolved_at_utc),
        }


@dataclass(frozen=True)
class FreshnessAssessment:
    state: FreshnessState
    age_seconds: float
    current_threshold_seconds: int
    judged_at_utc: datetime

    def to_dict(self) -> Dict[str, object]:
        return {
            "freshness_state": self.state.value,
            "age_seconds": self.age_seconds,
            "current_threshold_seconds": self.current_threshold_seconds,
            "judged_at_utc": _utc_text(self.judged_at_utc),
        }


class MarketClockService:
    def __init__(
        self,
        calendar_dir: Path | str = DEFAULT_CALENDAR_DIR,
        *,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self.calendar_dir = Path(calendar_dir)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._definitions = self._load_definitions()

    def _load_definitions(self) -> Dict[str, Tuple[CalendarDefinition, ...]]:
        result: Dict[str, list[CalendarDefinition]] = {}
        files = sorted(self.calendar_dir.glob("*.json"))
        if not files:
            raise ValueError(f"no market calendar files found in {self.calendar_dir}")
        for path in files:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("schema_version") != 1:
                raise ValueError(f"unsupported calendar schema in {path.name}")
            coverage_start = date.fromisoformat(raw["coverage_start"])
            coverage_end = date.fromisoformat(raw["coverage_end"])
            if coverage_start > coverage_end:
                raise ValueError(f"invalid calendar coverage in {path.name}")
            version = str(raw.get("calendar_version") or "").strip()
            if not version:
                raise ValueError(f"calendar_version is required in {path.name}")
            for calendar_id, item in (raw.get("calendars") or {}).items():
                timezone_name = str(item.get("timezone") or "")
                _zone(timezone_name)
                sessions = _parse_sessions(item.get("regular_sessions") or [])
                holidays = frozenset(date.fromisoformat(value) for value in item.get("holidays") or [])
                half_days = {
                    date.fromisoformat(day): _parse_sessions(day_sessions)
                    for day, day_sessions in (item.get("half_days") or {}).items()
                }
                if any(not coverage_start <= day <= coverage_end for day in holidays | set(half_days)):
                    raise ValueError(f"calendar date outside coverage for {calendar_id}")
                definition = CalendarDefinition(
                    calendar_id=str(calendar_id),
                    name=str(item.get("name") or calendar_id),
                    timezone_name=timezone_name,
                    coverage_start=coverage_start,
                    coverage_end=coverage_end,
                    calendar_version=version,
                    pre_open_start=_parse_wall_time(item.get("pre_open_start") or sessions[0][0].strftime("%H:%M")),
                    regular_sessions=sessions,
                    holidays=holidays,
                    half_days=half_days,
                    holiday_source_url=str(item.get("holiday_source_url") or ""),
                    hours_source_url=str(item.get("hours_source_url") or ""),
                )
                result.setdefault(definition.calendar_id, []).append(definition)
        for calendar_id, definitions in result.items():
            definitions.sort(key=lambda item: item.coverage_start)
            for previous, current in zip(definitions, definitions[1:]):
                if current.coverage_start <= previous.coverage_end:
                    raise ValueError(f"overlapping calendar coverage for {calendar_id}")
        return {key: tuple(value) for key, value in result.items()}

    def capture_request(
        self,
        *,
        server_timezone: str = DEFAULT_SERVER_TIMEZONE,
        user_timezone: str = "",
    ) -> RequestTimeContext:
        server_now = self._clock()
        # The injected/system clock is invoked exactly once per request.
        return RequestTimeContext(
            server_now_utc=_aware(server_now, "server_now_utc"),
            server_timezone=server_timezone,
            user_timezone=user_timezone or server_timezone,
        )

    def _definition_for(self, calendar_id: str, trading_date: date) -> Tuple[CalendarDefinition, bool]:
        definitions = self._definitions.get(str(calendar_id))
        if not definitions:
            raise ValueError(f"unsupported market calendar: {calendar_id}")
        for definition in definitions:
            if definition.covers(trading_date):
                return definition, True
        # Outside authoritative coverage, only the normal weekday/session
        # template may be used and must remain visibly degraded.
        return min(
            definitions,
            key=lambda item: min(
                abs((trading_date - item.coverage_start).days),
                abs((trading_date - item.coverage_end).days),
            ),
        ), False

    @staticmethod
    def _local_datetime(day: date, value: wall_time, zone: ZoneInfo) -> datetime:
        return datetime.combine(day, value, tzinfo=zone)

    def _trading_day_details(
        self, calendar_id: str, trading_date: date
    ) -> Tuple[CalendarDefinition, bool, bool, Tuple[Tuple[wall_time, wall_time], ...]]:
        definition, official = self._definition_for(calendar_id, trading_date)
        is_trading_day = trading_date.weekday() < 5 and (
            not official or trading_date not in definition.holidays
        )
        sessions = definition.half_days.get(trading_date, definition.regular_sessions)
        return definition, official, is_trading_day, sessions

    def _next_trading_pre_open(self, calendar_id: str, after_date: date) -> Optional[datetime]:
        for offset in range(1, 371):
            candidate = after_date + timedelta(days=offset)
            definition, _official, trading, _sessions = self._trading_day_details(
                calendar_id, candidate
            )
            if trading:
                return self._local_datetime(candidate, definition.pre_open_start, definition.timezone)
        return None

    def market_state(
        self,
        calendar_id: str,
        context: RequestTimeContext,
    ) -> MarketSession:
        definition_hint = self._definitions.get(str(calendar_id))
        if not definition_hint:
            raise ValueError(f"unsupported market calendar: {calendar_id}")
        market_zone = definition_hint[0].timezone
        local_now = context.server_now_utc.astimezone(market_zone)
        trading_date = local_now.date()
        definition, official, trading, sessions = self._trading_day_details(
            calendar_id, trading_date
        )
        half_day = trading_date in definition.half_days if official else False
        calendar_source = "official_calendar" if official else "template_fallback"
        calendar_version = definition.calendar_version if official else f"{definition.calendar_version}:template"
        session_open = session_close = next_transition = None
        reason = "trading_day"
        state = MarketStatus.CLOSED

        if trading:
            session_open = self._local_datetime(trading_date, sessions[0][0], market_zone)
            session_close = self._local_datetime(trading_date, sessions[-1][1], market_zone)
            local_time = local_now.timetz().replace(tzinfo=None)
            if local_time < definition.pre_open_start:
                state = MarketStatus.CLOSED
                reason = "before_pre_open"
                next_transition = self._local_datetime(
                    trading_date, definition.pre_open_start, market_zone
                )
            elif local_time < sessions[0][0]:
                state = MarketStatus.PRE_OPEN
                reason = "pre_open"
                next_transition = session_open
            else:
                for index, (start, end) in enumerate(sessions):
                    if start <= local_time < end:
                        state = MarketStatus.OPEN
                        reason = "half_day_open" if half_day else "regular_open"
                        next_transition = self._local_datetime(trading_date, end, market_zone)
                        break
                    if index + 1 < len(sessions) and end <= local_time < sessions[index + 1][0]:
                        state = MarketStatus.LUNCH_BREAK
                        reason = "lunch_break"
                        next_transition = self._local_datetime(
                            trading_date, sessions[index + 1][0], market_zone
                        )
                        break
                else:
                    state = MarketStatus.CLOSED
                    reason = "after_close"
                    next_transition = self._next_trading_pre_open(calendar_id, trading_date)
        else:
            reason = "weekend" if trading_date.weekday() >= 5 else "exchange_holiday"
            next_transition = self._next_trading_pre_open(calendar_id, trading_date)

        return MarketSession(
            calendar_id=str(calendar_id),
            market_timezone=definition.timezone_name,
            market_session_state=state,
            trading_date=trading_date,
            is_trading_day=trading,
            is_half_day=half_day,
            calendar_source=calendar_source,
            calendar_version=calendar_version,
            holiday_source_url=definition.holiday_source_url,
            hours_source_url=definition.hours_source_url,
            session_open_utc=_utc(session_open) if session_open else None,
            session_close_utc=_utc(session_close) if session_close else None,
            next_transition_utc=_utc(next_transition) if next_transition else None,
            reason=reason,
        )

    def resolve_time_range(
        self,
        expression: str,
        context: RequestTimeContext,
    ) -> ResolvedTimeRange:
        normalized = str(expression or "").strip().casefold()
        user_zone = _zone(context.user_timezone)
        local_now = context.server_now_utc.astimezone(user_zone)
        if normalized in TODAY_EXPRESSIONS:
            local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            start, end = _utc(local_start), context.server_now_utc
        elif normalized in YESTERDAY_EXPRESSIONS:
            local_end = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            local_start = local_end - timedelta(days=1)
            start, end = _utc(local_start), _utc(local_end)
        elif normalized in THIS_WEEK_EXPRESSIONS:
            local_start = (
                local_now - timedelta(days=local_now.weekday())
            ).replace(hour=0, minute=0, second=0, microsecond=0)
            start, end = _utc(local_start), context.server_now_utc
        elif normalized in NOW_EXPRESSIONS:
            start = end = context.server_now_utc
        elif normalized == "24h":
            start, end = context.server_now_utc - timedelta(hours=24), context.server_now_utc
        else:
            raise ValueError(f"unsupported financial time expression: {expression}")
        return ResolvedTimeRange(
            expression=str(expression),
            timezone_name=context.user_timezone,
            start_utc=start,
            end_utc=end,
            resolved_at_utc=context.server_now_utc,
        )

    def assess_freshness(
        self,
        observed_at: datetime,
        fetched_at: datetime,
        *,
        current_threshold_seconds: int,
        context: RequestTimeContext,
        requested_as_of: Optional[datetime] = None,
    ) -> FreshnessAssessment:
        observed = _utc(observed_at)
        fetched = _utc(fetched_at)
        if observed > fetched:
            raise ValueError("observed_at cannot be later than fetched_at")
        threshold = int(current_threshold_seconds)
        if threshold <= 0:
            raise ValueError("current_threshold_seconds must be positive")
        if requested_as_of is not None:
            requested = _utc(requested_as_of)
            if requested < context.server_now_utc - timedelta(seconds=threshold):
                state = FreshnessState.HISTORICAL
                age = max(0.0, (requested - observed).total_seconds())
                return FreshnessAssessment(state, age, threshold, context.server_now_utc)
        age = max(0.0, (context.server_now_utc - observed).total_seconds())
        if age <= threshold:
            state = FreshnessState.CURRENT
        elif age <= threshold * 2:
            state = FreshnessState.DELAYED
        else:
            state = FreshnessState.STALE
        return FreshnessAssessment(state, age, threshold, context.server_now_utc)

    def time_contract(
        self,
        context: RequestTimeContext,
        *,
        market: Optional[MarketSession] = None,
        resolved_range: Optional[ResolvedTimeRange] = None,
    ) -> Dict[str, object]:
        payload: Dict[str, object] = context.to_dict()
        payload.update(
            {
                "market_timezone": market.market_timezone if market else "",
                "market_calendar_id": market.calendar_id if market else "",
                "market_session_state": (
                    market.market_session_state.value if market else "unknown"
                ),
                "resolved_time_range": resolved_range.to_dict() if resolved_range else None,
            }
        )
        return payload

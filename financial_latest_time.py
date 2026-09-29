#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""统一选择不晚于一次请求截止时刻的最新行情和新闻。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Mapping, Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from financial_market_clock import MarketSession, RequestTimeContext


UTC = timezone.utc
LATEST_AVAILABLE_TIME_VERSION = "financial-latest-time-v1"


def _aware_utc(value: object, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return parsed.astimezone(UTC)


def _utc_text(value: datetime) -> str:
    return _aware_utc(value, "datetime").isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _empty_channel() -> dict:
    return {
        "status": "unavailable",
        "record": {},
        "selected_at_utc": None,
        "selected_date": None,
        "precision": "unknown",
    }


class LatestAvailableTimeResolver:
    """按渠道选择最大有效时点，并显式拒绝未来记录。"""

    @staticmethod
    def _quote_candidate(
        record: Mapping[str, object], cutoff: datetime
    ) -> tuple[Optional[datetime], bool]:
        try:
            observed = _aware_utc(record.get("observed_at"), "observed_at")
        except (TypeError, ValueError):
            return None, False
        return observed, observed <= cutoff

    @staticmethod
    def _news_candidate(
        record: Mapping[str, object], cutoff: datetime
    ) -> tuple[Optional[datetime], str, Optional[str], bool]:
        raw = str(record.get("published_at") or "").strip()
        precision = str(record.get("published_precision") or "instant").strip()
        if precision == "date" or (
            len(raw) == 10 and raw.count("-") == 2 and "T" not in raw
        ):
            try:
                day = date.fromisoformat(raw[:10])
                timezone_name = str(
                    record.get("published_timezone") or "UTC"
                ).strip()
                zone = ZoneInfo(timezone_name)
            except (ValueError, ZoneInfoNotFoundError):
                return None, "date", None, False
            start = datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)
            # 日期粒度只用于资格和稳定排序，不能公开成虚构发布时间。
            return start, "date", day.isoformat(), start <= cutoff
        try:
            published = _aware_utc(raw, "published_at")
        except (TypeError, ValueError):
            return None, precision or "unknown", None, False
        return published, "instant", None, published <= cutoff

    def resolve(
        self,
        context: RequestTimeContext,
        *,
        market: Optional[MarketSession] = None,
        quote_records: Sequence[Mapping[str, object]] = (),
        news_records: Sequence[Mapping[str, object]] = (),
    ) -> dict:
        cutoff = _aware_utc(context.server_now_utc, "server_now_utc")
        rejected = 0

        quotes = []
        for original in quote_records:
            record = dict(original)
            selected_at, eligible = self._quote_candidate(record, cutoff)
            if selected_at is None:
                continue
            if not eligible:
                rejected += 1
                continue
            quotes.append((selected_at, record))
        quote = _empty_channel()
        if quotes:
            selected_at, record = max(
                quotes,
                key=lambda item: (
                    item[0],
                    int(item[1].get("snapshot_id") or 0),
                ),
            )
            quote = {
                "status": "selected",
                "record": record,
                "selected_at_utc": _utc_text(selected_at),
                "selected_date": None,
                "precision": str(record.get("observed_precision") or "instant"),
            }

        news_candidates = []
        for original in news_records:
            record = dict(original)
            sort_at, precision, selected_date, eligible = self._news_candidate(
                record, cutoff
            )
            if sort_at is None:
                continue
            if not eligible:
                rejected += 1
                continue
            news_candidates.append((sort_at, precision, selected_date, record))
        news = _empty_channel()
        if news_candidates:
            sort_at, precision, selected_date, record = max(
                news_candidates,
                key=lambda item: (
                    item[0],
                    int(item[3].get("article_id") or 0),
                ),
            )
            news = {
                "status": "selected",
                "record": record,
                "selected_at_utc": (
                    _utc_text(sort_at) if precision == "instant" else None
                ),
                "selected_date": selected_date,
                "precision": precision,
            }

        return {
            "schema_version": LATEST_AVAILABLE_TIME_VERSION,
            "mode": "latest_available",
            "cutoff_at_utc": _utc_text(cutoff),
            "server_timezone": context.server_timezone,
            "user_timezone": context.user_timezone,
            "market_timezone": market.market_timezone if market else "",
            "market_calendar_id": market.calendar_id if market else "",
            "market_session_state": (
                market.market_session_state.value if market else "unknown"
            ),
            "calendar_version": market.calendar_version if market else "",
            "calendar_source": market.calendar_source if market else "",
            "quote": quote,
            "news": news,
            "future_records_rejected": rejected,
        }


__all__ = ["LATEST_AVAILABLE_TIME_VERSION", "LatestAvailableTimeResolver"]

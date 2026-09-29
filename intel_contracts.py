#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Shared contracts for the market intelligence radar."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

try:
    from zoneinfo import ZoneInfo
    from zoneinfo import ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python < 3.9 fallback
    from backports.zoneinfo import ZoneInfo
    from backports.zoneinfo import ZoneInfoNotFoundError


DEFAULT_INDUSTRY_PACK_ID = "family_office"
DISPLAY_TIMEZONE = "Asia/Hong_Kong"

INTERNAL_CATEGORIES = ("trend", "event", "other")
PUBLIC_CATEGORY_ALIASES = {"today": "event"}
PUBLIC_CATEGORY_VALUES = ("trend", "today", "other")

JOB_STATUSES = (
    "queued",
    "running",
    "retry_wait",
    "completed",
    "failed",
    "cancelled",
)

CANDIDATE_STATUSES = (
    "discovered",
    "queued",
    "dispatching",
    "crawled",
    "retry_wait",
    "discarded",
    "failed",
)


def normalize_internal_category(value: str, *, allow_empty: bool = False) -> str:
    normalized = str(value or "").strip().lower()
    normalized = PUBLIC_CATEGORY_ALIASES.get(normalized, normalized)
    if not normalized and allow_empty:
        return ""
    if normalized not in INTERNAL_CATEGORIES:
        raise ValueError(f"unsupported category: {value}")
    return normalized


def public_category(value: str) -> str:
    normalized = normalize_internal_category(value)
    return "today" if normalized == "event" else normalized


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_text(value: Optional[datetime] = None) -> str:
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time_range(
    value: str,
    *,
    now: Optional[datetime] = None,
    display_timezone: str = DISPLAY_TIMEZONE,
) -> Tuple[datetime, datetime]:
    """Return a UTC [start, end] range for supported product shortcuts."""
    end = now or utc_now()
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    end = end.astimezone(timezone.utc)
    normalized = str(value or "24h").strip().lower()
    if normalized == "24h":
        return end - timedelta(hours=24), end
    if normalized == "7d":
        return end - timedelta(days=7), end
    if normalized == "30d":
        return end - timedelta(days=30), end
    if normalized.endswith("d") and normalized[:-1].isdigit():
        days = int(normalized[:-1])
        if 1 <= days <= 730:
            return end - timedelta(days=days), end
    if normalized == "today":
        try:
            local_zone = ZoneInfo(display_timezone)
        except ZoneInfoNotFoundError:
            # The application is deployed in slim containers where tzdata may
            # be absent.  Asia/Hong_Kong is permanently UTC+8.
            local_zone = timezone(timedelta(hours=8))
        local_now = end.astimezone(local_zone)
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return local_start.astimezone(timezone.utc), end
    if normalized == "all":
        return end - timedelta(days=36500), end  # 100 年，等同无时间窗（用于“已入库文章”全量口径）
    raise ValueError(f"unsupported time_range: {value}")

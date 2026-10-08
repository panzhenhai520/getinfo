#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布时间的实时抽取与精度标注。

目标（产品要求）：**采集那一刻就把时间写正确**，不靠事后回填。
生产现状：候选表 `published_at` 只有 2% 有值——因为绝大多数信源是列表页，
而列表页扫描过去只抽标题+链接，没抽日期；正文抽取虽然用 htmldate 抽到了时间，
却是另一条链路、存在 articles 表里，没有回流到候选。

本模块提供统一的抽取口径，供各扫描器在解析层直接调用：
  · 正文/详情页：og:published_time、article:published_time、JSON-LD datePublished、htmldate
  · 列表页：链接附近的日期文本（2026-10-07 / 2026年10月7日 / 10-07 / 昨天 / 3天前 / Oct 7, 2026）
  · URL：/2026/10/07/ 、/2026-10-07/ 、/20261007/ 等常见形态

同时标注**精度**，因为它决定数据可不可信：
  exact      带时分秒（来自 meta/JSON-LD/htmldate）
  date       只有日期
  url        从 URL 推断（可能被站点的栏目路径干扰）
  discovered 拿不到 —— 由调用方填 first_seen_at，绝不冒充发布时间
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

PRECISION_EXACT = "exact"
PRECISION_DATE = "date"
PRECISION_URL = "url"
# 由调用方填发现时间时使用；本模块不会凭空造时间
PRECISION_DISCOVERED = "discovered"

_PRECISION_RANK = {
    PRECISION_EXACT: 3,
    PRECISION_DATE: 2,
    PRECISION_URL: 1,
    PRECISION_DISCOVERED: 0,
}

# ── 文本日期形态 ──────────────────────────────────────────────────────────
_RE_ISO = re.compile(
    r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)
_RE_CN = re.compile(
    r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日(?:\s*(\d{1,2}):(\d{2}))?"
)
_RE_CN_SHORT = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日(?:\s*(\d{1,2}):(\d{2}))?")
_RE_MONTH_EN = re.compile(
    r"(?i)\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})"
    r"(?:\s+(\d{1,2}):(\d{2}))?"
)
_RE_DAY_EN = re.compile(
    r"(?i)\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s+(\d{4})"
)
_RE_RELATIVE = re.compile(r"(?<!\d)(\d{1,3})\s*(分钟|小时|天|周|个月)前")
_RE_URL_DATE = re.compile(r"/(20\d{2})[-/]?(\d{2})[-/]?(\d{2})(?:/|[-_.]|$)")

_MONTHS = {name: index + 1 for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
)}


def _now(now: Optional[datetime] = None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _iso(moment: datetime, *, with_time: bool) -> str:
    text = moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S" if with_time else "%Y-%m-%d")
    return text


def _safe_date(year: int, month: int, day: int, hour: int = 0, minute: int = 0,
               second: int = 0, *, now: datetime) -> Optional[datetime]:
    try:
        moment = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        return None
    # 明显不合理的未来时间（>1 天）视为解析错误，宁可判为"没拿到"
    if moment > now + timedelta(days=1):
        return None
    if moment.year < 2000:
        return None
    return moment


def parse_text_date(text: str, *, now: Optional[datetime] = None) -> Tuple[str, str]:
    """从任意文本里抽第一个像发布时间的日期。返回 (ISO 串, 精度)；抽不到返回 ("", "")。"""
    raw = str(text or "")
    if not raw:
        return "", ""
    moment_now = _now(now)

    match = _RE_CN.search(raw)
    if match:
        has_time = bool(match.group(4))
        moment = _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)),
                            int(match.group(4) or 0), int(match.group(5) or 0), now=moment_now)
        if moment:
            return _iso(moment, with_time=has_time), (PRECISION_EXACT if has_time else PRECISION_DATE)

    match = _RE_ISO.search(raw)
    if match:
        has_time = bool(match.group(4))
        moment = _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)),
                            int(match.group(4) or 0), int(match.group(5) or 0),
                            int(match.group(6) or 0), now=moment_now)
        if moment:
            return _iso(moment, with_time=has_time), (PRECISION_EXACT if has_time else PRECISION_DATE)

    match = _RE_MONTH_EN.search(raw)
    if match:
        has_time = bool(match.group(4))
        moment = _safe_date(int(match.group(3)), _MONTHS[match.group(1).casefold()[:3]],
                            int(match.group(2)), int(match.group(4) or 0),
                            int(match.group(5) or 0), now=moment_now)
        if moment:
            return _iso(moment, with_time=has_time), (PRECISION_EXACT if has_time else PRECISION_DATE)

    match = _RE_DAY_EN.search(raw)
    if match:
        moment = _safe_date(int(match.group(3)), _MONTHS[match.group(2).casefold()[:3]],
                            int(match.group(1)), now=moment_now)
        if moment:
            return _iso(moment, with_time=False), PRECISION_DATE

    # 相对时间（中文列表页很常见）：按扫描时刻往回推
    match = _RE_RELATIVE.search(raw)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        delta = {
            "分钟": timedelta(minutes=amount),
            "小时": timedelta(hours=amount),
            "天": timedelta(days=amount),
            "周": timedelta(weeks=amount),
            "个月": timedelta(days=30 * amount),
        }[unit]
        return _iso(moment_now - delta, with_time=False), PRECISION_DATE
    if "昨天" in raw:
        return _iso(moment_now - timedelta(days=1), with_time=False), PRECISION_DATE
    if "前天" in raw:
        return _iso(moment_now - timedelta(days=2), with_time=False), PRECISION_DATE
    if "今天" in raw or "刚刚" in raw:
        return _iso(moment_now, with_time=False), PRECISION_DATE

    # 只写了"10月7日"这种没年份的：按当前年份补齐，若因此跑到未来则退一年
    match = _RE_CN_SHORT.search(raw)
    if match:
        moment = _safe_date(moment_now.year, int(match.group(1)), int(match.group(2)),
                            int(match.group(3) or 0), int(match.group(4) or 0), now=moment_now)
        if moment is None:
            moment = _safe_date(moment_now.year - 1, int(match.group(1)), int(match.group(2)),
                                now=moment_now)
        if moment:
            return _iso(moment, with_time=False), PRECISION_DATE
    return "", ""


def parse_url_date(url: str) -> Tuple[str, str]:
    """从 URL 路径里推日期（/2026/10/07/、/2026-10-07/、/20261007/）。"""
    match = _RE_URL_DATE.search(str(url or ""))
    if not match:
        return "", ""
    moment = _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)),
                        now=_now())
    if not moment:
        return "", ""
    return _iso(moment, with_time=False), PRECISION_URL


def from_html_meta(html: str, *, now: Optional[datetime] = None) -> Tuple[str, str]:
    """从页面头部/结构化数据里抽发布时间：og / article:published_time / JSON-LD。

    这些是站点自己声明的发布时间，比任何推断都可信，精度记为 exact。
    """
    raw = str(html or "")
    if not raw:
        return "", ""
    head = raw[:120000]
    patterns = (
        r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']article:published_time["\']',
        r'<meta[^>]+property=["\']og:published_time["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+itemprop=["\']datePublished["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+name=["\'](?:pubdate|publishdate|date)["\'][^>]+content=["\']([^"\']+)',
    )
    for pattern in patterns:
        match = re.search(pattern, head, re.IGNORECASE)
        if not match:
            continue
        value = str(match.group(1)).strip()
        parsed = parse_iso_datetime(value, now=now)
        if parsed[0]:
            return parsed
    # JSON-LD：datePublished 通常带完整时分秒
    for block in re.findall(
        r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', head, re.IGNORECASE | re.DOTALL
    ):
        match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', block, re.IGNORECASE)
        if match:
            parsed = parse_iso_datetime(str(match.group(1)), now=now)
            if parsed[0]:
                return parsed
    return "", ""


def parse_iso_datetime(value: str, *, now: Optional[datetime] = None) -> Tuple[str, str]:
    """解析标准时间串（含时区），拿不准就走通用文本解析。"""
    text = str(value or "").strip()
    if not text:
        return "", ""
    candidate = text.replace("Z", "+00:00")
    for fmt in (None, "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.fromisoformat(candidate) if fmt is None else datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        has_time = bool(re.search(r"\d{1,2}:\d{2}", text))
        return _iso(parsed, with_time=has_time), (PRECISION_EXACT if has_time else PRECISION_DATE)
    return parse_text_date(text, now=now)


def extract(
    *,
    html: str = "",
    listing_text: str = "",
    url: str = "",
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """按可信度从高到低依次尝试，返回 {published_at, precision, source}。

    顺序（这是"写正确"的关键）：
      1. 页面声明的发布时间（meta / JSON-LD）—— 站点自己说的，最可信
      2. 列表页里链接旁边的日期文本
      3. URL 里的日期
      4. 抽不到 → published_at 为空、precision='discovered'，由调用方填发现时间
    """
    parsed = from_html_meta(html, now=now)
    if parsed[0]:
        return {"published_at": parsed[0], "precision": parsed[1], "source": "html_meta"}
    parsed = parse_text_date(listing_text, now=now)
    if parsed[0]:
        return {"published_at": parsed[0], "precision": parsed[1], "source": "listing_text"}
    parsed = parse_url_date(url)
    if parsed[0]:
        return {"published_at": parsed[0], "precision": parsed[1], "source": "url"}
    return {"published_at": "", "precision": PRECISION_DISCOVERED, "source": "none"}


# ── 落到 articles 的四个时间列（published_at_utc / timezone / precision / source）──
# 时区必须**显式声明**（config/source_timezones.json），不做猜测：
# 声明的时区决定"这一天的 00:00 对应哪个 UTC 瞬间"，实际用到的时区会写进
# published_timezone，所以任何一条都能反推回源站本地日期。
_TIMEZONE_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "config", "source_timezones.json"
)
_TIMEZONE_CONFIG: Optional[Dict[str, Any]] = None

# 历史值 → 本模块的精度口径（DB 里既有 day/datetime，也有 exact/date/url）
_PRECISION_ALIASES = {
    "exact": PRECISION_EXACT,
    "datetime": PRECISION_EXACT,
    "second": PRECISION_EXACT,
    "date": PRECISION_DATE,
    "day": PRECISION_DATE,
    "url": PRECISION_URL,
    "discovered": PRECISION_DISCOVERED,
    "unknown": PRECISION_DISCOVERED,
    "none": PRECISION_DISCOVERED,
    "": "",
}


def _timezone_config() -> Dict[str, Any]:
    global _TIMEZONE_CONFIG
    if _TIMEZONE_CONFIG is None:
        try:
            with open(_TIMEZONE_CONFIG_PATH, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            _TIMEZONE_CONFIG = loaded if isinstance(loaded, dict) else {}
        except Exception:
            _TIMEZONE_CONFIG = {}
    return _TIMEZONE_CONFIG


def normalize_precision(value: Any) -> str:
    """把各种历史写法归一到 exact/date/url/discovered；不认识的一律当作未知。"""
    return _PRECISION_ALIASES.get(str(value or "").strip().casefold(), "")


def declared_timezone(*, publisher_key: str = "", domain: str = "", market: str = "") -> str:
    """查显式声明的源站时区（publishers → markets → fallback）。查不到返回内核默认。"""
    config = _timezone_config()
    publishers = config.get("publishers") or {}
    suffixes = config.get("domain_suffixes") or {}
    for raw in (publisher_key, domain):
        host = str(raw or "").strip().casefold().lstrip(".")
        if host.startswith("www."):
            host = host[4:]
        if not host:
            continue
        if host in publishers:
            return str(publishers[host])
        for key, name in publishers.items():
            key = str(key).casefold()
            if host == key or host.endswith("." + key):
                return str(name)
        for suffix, name in suffixes.items():
            if host.endswith(str(suffix).casefold()):
                return str(name)
    markets = config.get("markets") or {}
    market_key = str(market or "").strip().upper()
    if market_key and market_key in markets:
        return str(markets[market_key])
    return str(config.get("fallback_timezone") or "Asia/Hong_Kong")


def _zone(name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(str(name))
    except Exception:
        pass
    try:  # Windows 缺 tzdata 时退回 pytz
        import pytz

        return pytz.timezone(str(name))
    except Exception:
        return None


def to_utc(value: Any, *, timezone_name: str = "") -> str:
    """把"源站本地时间"归一成 published_at_utc。

    与既有读取方约定一致（financial_evidence._article_time_interval /
    financial_news_query._published_fields）：
      · 只有日期（precision=date）→ 写成 `<本地日期>T00:00:00Z`：**前 10 位必须仍是源站本地日期**，
        读取方配合 published_timezone 才能拼出"这一天的本地区间"；若换算成 UTC 会整体错一天。
      · 带时分且带时区（`+08:00`/`Z`）→ 直接用自身时区换成真实 UTC 瞬间。
      · 带时分但没写时区 → 按**显式声明**的源站时区解释，换算成真实 UTC 瞬间。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text.replace("/", "-").replace(" ", "T")
    match = re.match(
        r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T](\d{1,2}):(\d{2})(?::(\d{2}))?)?"
        r"(Z|[+-]\d{2}:?\d{2})?",
        normalized,
    )
    if not match:
        return ""
    try:
        day = "%04d-%02d-%02d" % (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return ""
    if not match.group(4):
        # 只有日期：保留本地日期字面量
        return f"{day}T00:00:00Z"
    explicit_zone = match.group(7)
    if explicit_zone:
        moment = parse_iso_datetime(text)
        if not moment[0]:
            return ""
        return moment[0] if moment[0].endswith("Z") else moment[0] + "Z"
    zone = _zone(timezone_name)
    if zone is None:
        return ""
    try:
        local = datetime(
            int(match.group(1)), int(match.group(2)), int(match.group(3)),
            int(match.group(4)), int(match.group(5)), int(match.group(6) or 0),
            tzinfo=zone,
        )
    except ValueError:
        return ""
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def article_time_fields(
    *,
    published_at: Any,
    precision: Any = "",
    source: Any = "",
    publisher_key: str = "",
    domain: str = "",
    market: str = "",
) -> Dict[str, str]:
    """把抽到的发布时间归一到 articles 的四个时间列（入库唯一收口调用）。

    入参 published_at 允许是 `2026-10-07`、`2026-10-07T14:30:00`、
    `2026-10-07T14:30:00+08:00`、`2026/10/07 14:30` 等形态。
    """
    text = str(published_at or "").strip()
    fields = {
        "published_at_utc": "",
        "published_timezone": "",
        "published_precision": normalize_precision(precision),
        "published_time_source": str(source or "").strip(),
    }
    if not text:
        return fields
    has_time = bool(re.search(r"\d{1,2}:\d{2}", text))
    if not fields["published_precision"]:
        fields["published_precision"] = PRECISION_EXACT if has_time else PRECISION_DATE
    zone_name = declared_timezone(
        publisher_key=publisher_key, domain=domain, market=market
    )
    utc_text = to_utc(text, timezone_name=zone_name)
    if utc_text:
        fields["published_at_utc"] = utc_text
        fields["published_timezone"] = zone_name
        if not fields["published_time_source"]:
            fields["published_time_source"] = "published_at"
    return fields


def prefer(current_at: str, current_precision: str, candidate: Dict[str, Any]) -> bool:
    """是否有必要用新抽到的结果覆盖已有值：精度更高才覆盖。

    防止"事后回填"把精确时间覆盖成粗糙时间（例如已有时分秒，又被 URL 推断的日期覆盖）。
    """
    new_at = str((candidate or {}).get("published_at") or "")
    if not new_at:
        return False
    if not str(current_at or "").strip():
        return True
    old_rank = _PRECISION_RANK.get(str(current_precision or ""), 0)
    new_rank = _PRECISION_RANK.get(str((candidate or {}).get("precision") or ""), 0)
    return new_rank > old_rank

# -*- coding: utf-8 -*-

"""T2.3 日期分层提取（规则实现，0 次 LLM 调用）。

按可信度分层取文章发布时间，结果写回 articles 既有五字段：
  published_at_utc / published_timezone / published_precision / published_time_source
（不新增列）。层级与置信度：
  1. JSON-LD datePublished/dateModified            → high / datetime
  2. meta article:published_time / og:published_time / DC.date.issued / datePublished → high / datetime
  3. 微数据 itemprop="datePublished"                → medium / datetime|date
  4. <time datetime=...> 语义标签                    → medium / datetime|date
  5. URL 内日期（/2026/09/18/ 等）                  → medium / date
  6. 相对时间（"3小时前"，按抓取时刻折算）            → medium / datetime
"""

import json
import re
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

from bs4 import BeautifulSoup

_JSONLD_RE = re.compile(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)
_META_DATE_RE = re.compile(
    r"<meta[^>]+(?:name|property)=[\"'](?:article:published_time|og:published_time|"
    r"dcterms\.date|DC\.date\.issued|datePublished|pubdate|publishdate|"
    r"article:modified_time|date)[\"'][^>]+content=[\"']([^\"']+)[\"'][^>]*/?>",
    re.IGNORECASE,
)
_META_DATE_RE_SWAPPED = re.compile(
    r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+(?:name|property)=[\"']"
    r"(?:article:published_time|og:published_time|dcterms\.date|DC\.date\.issued|"
    r"datePublished|pubdate|publishdate|date)[\"'][^>]*/?>",
    re.IGNORECASE,
)
_MICRODATA_RE = re.compile(
    r"itemprop=[\"']datePublished[\"'][^>]*content=[\"']([^\"']+)[\"']"
    r"|<time[^>]+itemprop=[\"']datePublished[\"'][^>]*datetime=[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)
_TIME_TAG_RE = re.compile(r"<time[^>]+datetime=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)
_URL_DATE_RE = re.compile(r"/(20\d{2})[/\-.](\d{1,2})[/\-.](\d{1,2})(?:[/?#]|$)")
_RELATIVE_RE = re.compile(
    r"(刚刚|(\d+)\s*分钟前|(\d+)\s*小时前|昨天|前天|(\d+)\s*天前)", re.IGNORECASE
)
_ACTIVITY_LABELS = (
    "会议时间", "会议日期", "举办时间", "活动时间", "召开时间",
    "报名时间", "展会时间", "开始时间", "结束时间", "截止时间",
)


def _normalize_datetime(value: str) -> Tuple[Optional[str], str]:
    """把常见日期串归一化为 (iso_text, precision)；无法解析返回 (None, '')。

    precision: 'datetime'（含时间）或 'date'（仅日期）。
    """
    text = str(value or "").strip()
    if not text:
        return None, ""
    text = text.replace("Z", "+00:00")
    # 中文日期 2026年9月18日 → 2026-09-18
    m = re.match(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}", "date"
    m = re.match(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?", text)
    if not m:
        return None, ""
    base = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    if m.group(4):
        return f"{base} {m.group(4)}:{m.group(5)}:{m.group(6) or '00'}", "datetime"
    return base, "date"


def extract_from_jsonld(html: str) -> Tuple[Optional[str], str, str]:
    """JSON-LD 里的 datePublished/dateModified（最可靠）。"""
    for raw_block in _JSONLD_RE.findall(str(html or "")):
        for block in (raw_block,):
            text = block.strip()
            if not text:
                continue
            try:
                data = json.loads(text)
            except (TypeError, ValueError):
                # 有的页面把注释/数组混在块里，取首个 {…} 再试一次
                start = text.find("{")
                if start < 0:
                    continue
                try:
                    data = json.loads(text[start:])
                except (TypeError, ValueError):
                    continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                for key in ("datePublished", "dateModified"):
                    raw = item.get(key)
                    if isinstance(raw, dict):
                        raw = raw.get("@value") or raw.get("value")
                    if raw:
                        normalized, precision = _normalize_datetime(str(raw))
                        if normalized:
                            return normalized, precision, "high"
    return None, "", ""


def extract_from_meta(html: str) -> Tuple[Optional[str], str, str]:
    """meta 标签里的发布时间。"""
    text = str(html or "")
    for pattern in (_META_DATE_RE, _META_DATE_RE_SWAPPED):
        for raw in pattern.findall(text):
            for candidate in raw if isinstance(raw, tuple) else (raw,):
                normalized, precision = _normalize_datetime(str(candidate))
                if normalized:
                    return normalized, precision, "high"
    return None, "", ""


def extract_from_microdata(html: str) -> Tuple[Optional[str], str, str]:
    for match in _MICRODATA_RE.finditer(str(html or "")):
        raw = next((g for g in match.groups() if g), None)
        if raw:
            normalized, precision = _normalize_datetime(str(raw))
            if normalized:
                return normalized, precision, "medium"
    return None, "", ""


def extract_from_time_tag(html: str) -> Tuple[Optional[str], str, str]:
    """<time datetime> 语义标签（跳过会议/报名等非发布时间行）。"""
    try:
        soup = BeautifulSoup(str(html or ""), "html.parser")
    except Exception:
        return None, "", ""
    for tag in soup.find_all("time"):
        raw = tag.get("datetime") or ""
        if not raw:
            raw = tag.get_text(strip=True)
        if not raw:
            continue
        # 活动类字段行不是发布时间
        context = tag.get_text(" ", strip=True)
        if any(label in context for label in _ACTIVITY_LABELS):
            continue
        normalized, precision = _normalize_datetime(str(raw))
        if normalized:
            return normalized, precision, "medium"
    return None, "", ""


def extract_date_from_url(url: str) -> Optional[str]:
    """URL 路径内日期，如 /2026/09/18/、/2026-09-18/。"""
    for match in _URL_DATE_RE.finditer(str(url or "")):
        year, month, day = match.groups()
        if 2000 <= int(year) <= 2100 and 1 <= int(month) <= 12 and 1 <= int(day) <= 31:
            return f"{year}-{int(month):02d}-{int(day):02d}"
    return None


def parse_relative_time(text: str, fetched_at: Optional[datetime] = None) -> Optional[str]:
    """相对时间折算为具体日期（按抓取时刻）。"""
    base = fetched_at or datetime.now()
    match = _RELATIVE_RE.search(str(text or ""))
    if not match:
        return None
    if match.group(1) == "刚刚":
        dt = base
    elif match.group(2):
        dt = base - timedelta(minutes=int(match.group(2)))
    elif match.group(3):
        dt = base - timedelta(hours=int(match.group(3)))
    elif match.group(1) == "昨天":
        dt = base - timedelta(days=1)
    elif match.group(1) == "前天":
        dt = base - timedelta(days=2)
    elif match.group(4):
        dt = base - timedelta(days=int(match.group(4)))
    else:
        return None
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def extract_layered_date(html: str, url: str = "", fetched_at: Optional[datetime] = None) -> Dict:
    """六层依次尝试，返回 {date, precision, source, confidence}；全空返回空 dict。"""
    source_html = str(html or "")
    for extractor, source in (
        (extract_from_jsonld, "jsonld"),
        (extract_from_meta, "meta"),
        (extract_from_microdata, "microdata"),
        (extract_from_time_tag, "time_tag"),
    ):
        try:
            date, precision, confidence = extractor(source_html)
        except Exception:
            date, precision, confidence = None, "", ""
        if date:
            return {"date": date, "precision": precision, "source": source, "confidence": confidence}
    url_date = extract_date_from_url(url or "")
    if url_date:
        return {"date": url_date, "precision": "date", "source": "url", "confidence": "medium"}
    # 相对时间：在去标签后的文本里找「x小时前/昨天」等
    try:
        text = BeautifulSoup(source_html, "html.parser").get_text(" ", strip=True)
    except Exception:
        text = source_html
    relative = parse_relative_time(text, fetched_at=fetched_at)
    if relative:
        return {"date": relative, "precision": "datetime", "source": "relative", "confidence": "medium"}
    return {}

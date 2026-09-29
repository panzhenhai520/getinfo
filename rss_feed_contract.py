#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Strict, reusable parsing and acceptance checks for external RSS/Atom feeds."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, Iterable, List
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

import config


ALLOWED_RSS_CONTENT_TYPES = {
    "application/atom+xml",
    "application/rdf+xml",
    "application/rss+xml",
    "application/xml",
    "text/xml",
}
FORBIDDEN_XML_DECLARATIONS = (b"<!doctype", b"<!entity")


class RSSFeedContractError(ValueError):
    """Raised when a remote response is not an acceptable RSS/Atom feed."""


def _local_name(tag: str) -> str:
    return str(tag or "").rsplit("}", 1)[-1].casefold()


def _element_text(element, names: Iterable[str]) -> str:
    wanted = {name.casefold() for name in names}
    for child in list(element):
        if _local_name(child.tag) in wanted:
            return " ".join("".join(child.itertext()).split())
    return ""


def parse_feed_datetime(value: str) -> datetime:
    """Parse common RSS or Atom timestamps and normalize them to UTC."""
    raw = str(value or "").strip()
    if not raw:
        raise RSSFeedContractError("RSS 条目缺少发布时间")
    parsed = None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        pass
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RSSFeedContractError(f"RSS 发布时间无法解析：{raw[:80]}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_rss_feed(response, *, limit: int) -> List[Dict]:
    """Validate a bounded HTTP response and return normalized feed entries."""
    status_code = int(getattr(response, "status_code", 0) or 0)
    if not 200 <= status_code < 300:
        raise RSSFeedContractError(f"RSS HTTP 状态异常：{status_code}")
    content = bytes(getattr(response, "content", b"") or b"")
    if not content:
        raise RSSFeedContractError("RSS 响应为空")
    if len(content) > int(config.INTEL_SCAN_MAX_RESPONSE_BYTES):
        raise RSSFeedContractError("RSS 响应超过大小上限")
    media_type = str(getattr(response, "content_type", "") or "").split(";", 1)[0].strip().casefold()
    if media_type not in ALLOWED_RSS_CONTENT_TYPES:
        raise RSSFeedContractError(f"RSS Content-Type 不受支持：{media_type or 'missing'}")
    lowered_prefix = content[:65536].lower()
    if any(marker in lowered_prefix for marker in FORBIDDEN_XML_DECLARATIONS):
        raise RSSFeedContractError("RSS XML 不允许 DTD 或实体声明")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise RSSFeedContractError("RSS XML 解析失败") from exc

    base_url = str(getattr(response, "url", "") or "")
    results = []
    for element in root.iter():
        if _local_name(element.tag) not in {"item", "entry"}:
            continue
        title = _element_text(element, ("title",))
        summary = _element_text(element, ("description", "summary", "content"))
        published = _element_text(element, ("pubdate", "published", "updated", "date"))
        link = _element_text(element, ("link",))
        if not link:
            for child in list(element):
                if _local_name(child.tag) == "link" and child.attrib.get("href"):
                    link = child.attrib["href"]
                    break
        if link:
            results.append(
                {
                    "url": urljoin(base_url, link),
                    "title": title,
                    "summary": BeautifulSoup(summary, "html.parser").get_text(" ", strip=True),
                    "published_at": published,
                }
            )
        if len(results) >= max(1, int(limit)):
            break
    return results


def validate_rss_feed_response(response, *, limit: int = 5000) -> Dict:
    """Return acceptance metadata or fail if entries, links, or dates are invalid."""
    items = parse_rss_feed(response, limit=limit)
    if not items:
        raise RSSFeedContractError("RSS 未包含可用 item/entry")
    parsed_dates = []
    for item in items:
        parsed = urlsplit(str(item.get("url") or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise RSSFeedContractError("RSS 条目链接不是绝对 HTTP(S) URL")
        if not str(item.get("title") or "").strip():
            raise RSSFeedContractError("RSS 条目缺少标题")
        parsed_dates.append(parse_feed_datetime(item.get("published_at")))
    return {
        "entry_count": len(items),
        "dated_entry_count": len(parsed_dates),
        "latest_published_at": max(parsed_dates).isoformat().replace("+00:00", "Z"),
        "sample_url": items[0]["url"],
        "sample_title": items[0]["title"],
    }

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Deterministic official-news adapters and audited metadata persistence."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Mapping, Optional
from urllib.parse import urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from intel_http import SafeHTTPClient


UTC = timezone.utc
HONG_KONG = timezone(timedelta(hours=8))
HKEXNEWS_PREFIX_URL = "https://www1.hkexnews.hk/search/prefix.do"
HKEXNEWS_TITLE_URL = "https://www1.hkexnews.hk/search/titlesearch.xhtml"
_HKEX_DOCUMENT_PATH = "/listedco/listconews/"


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class HKEXNewsTitleSearchClient:
    """Read exact-stock announcement metadata from HKEXnews title search."""

    def __init__(self, *, http_client=None):
        self.http = http_client or SafeHTTPClient()

    @staticmethod
    def _headers() -> dict:
        return {
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            "Referer": "https://www.hkexnews.hk/search/titlesearch.xhtml?lang=en",
            "User-Agent": "CollectInfo-FinancialOfficialNews/1.0",
        }

    @staticmethod
    def _stock_code(canonical_symbol: object) -> str:
        symbol = str(canonical_symbol or "").strip().upper()
        if not symbol.endswith(".HK"):
            return ""
        code = symbol[:-3]
        return code.zfill(5) if code.isdigit() and 1 <= len(code) <= 5 else ""

    @staticmethod
    def _jsonp_object(text: object) -> dict:
        match = re.fullmatch(r"\s*[A-Za-z_$][\w$]*\((\{.*\})\);?\s*", str(text or ""), re.S)
        if not match:
            raise ValueError("HKEXnews stock lookup schema changed")
        payload = json.loads(match.group(1))
        if not isinstance(payload, Mapping):
            raise ValueError("HKEXnews stock lookup payload is not an object")
        return dict(payload)

    @staticmethod
    def _document_url(href: object) -> str:
        url = urljoin("https://www1.hkexnews.hk", str(href or "").strip())
        parsed = urlsplit(url)
        if (
            parsed.scheme.casefold() != "https"
            or (parsed.hostname or "").casefold() != "www1.hkexnews.hk"
            or not parsed.path.casefold().startswith(_HKEX_DOCUMENT_PATH)
        ):
            return ""
        return url

    def _resolve_stock(self, stock_code: str) -> tuple[str, str]:
        url = HKEXNEWS_PREFIX_URL + "?" + urlencode(
            {
                "callback": "callback",
                "lang": "EN",
                "type": "A",
                "name": stock_code.lstrip("0") or "0",
                "market": "SEHK",
            }
        )
        payload = self._jsonp_object(self.http.get(url, headers=self._headers()).text)
        for item in payload.get("stockInfo") or []:
            if not isinstance(item, Mapping):
                continue
            code = str(item.get("code") or "").strip().zfill(5)
            stock_id = str(item.get("stockId") or "").strip()
            if code == stock_code and stock_id.isdigit():
                return stock_id, str(item.get("name") or "").strip()
        return "", ""

    def search(
        self,
        canonical_symbol: object,
        *,
        cutoff: datetime,
        start: Optional[datetime] = None,
        max_items: int = 20,
    ) -> list[dict]:
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("HKEXnews cutoff must be timezone-aware")
        stock_code = self._stock_code(canonical_symbol)
        if not stock_code:
            return []
        stock_id, resolved_name = self._resolve_stock(stock_code)
        if not stock_id:
            return []
        source_url = HKEXNEWS_TITLE_URL + "?" + urlencode(
            {
                "category": 0,
                "lang": "EN",
                "market": "SEHK",
                "stockId": stock_id,
            }
        )
        html = self.http.get(source_url, headers=self._headers()).text
        soup = BeautifulSoup(html, "html.parser")
        cutoff_utc = cutoff.astimezone(UTC)
        start_utc = start.astimezone(UTC) if start is not None else None
        results = []
        for row in soup.select("tbody tr"):
            release_cell = row.select_one("td.release-time")
            code_cell = row.select_one("td.stock-short-code")
            link = row.select_one("div.doc-link a[href]")
            if release_cell is None or code_cell is None or link is None:
                continue
            code_text = re.sub(r"\D", "", code_cell.get_text(" ", strip=True))[-5:]
            if code_text.zfill(5) != stock_code:
                continue
            release_text = release_cell.get_text(" ", strip=True)
            release_text = re.sub(r"^Release\s+Time:\s*", "", release_text, flags=re.I)
            try:
                published = datetime.strptime(release_text, "%d/%m/%Y %H:%M").replace(
                    tzinfo=HONG_KONG
                ).astimezone(UTC)
            except ValueError:
                continue
            if published > cutoff_utc or (start_utc is not None and published < start_utc):
                continue
            document_url = self._document_url(link.get("href"))
            title = link.get_text(" ", strip=True)
            if not document_url or not title:
                continue
            name_cell = row.select_one("td.stock-short-name")
            stock_name = (
                re.sub(
                    r"^Stock\s+Short\s+Name:\s*",
                    "",
                    name_cell.get_text(" ", strip=True),
                    flags=re.I,
                )
                if name_cell is not None
                else resolved_name
            )
            headline = row.select_one("div.headline")
            category = headline.get_text(" ", strip=True) if headline is not None else ""
            results.append(
                {
                    "url": document_url,
                    "source_url": source_url,
                    "title": title,
                    "summary": "；".join(
                        item
                        for item in (
                            f"Stock Code: {stock_code}",
                            f"Stock Short Name: {stock_name or resolved_name}",
                            category,
                        )
                        if item
                    ),
                    "stock_code": stock_code,
                    "stock_name": stock_name or resolved_name,
                    "published_at": _utc_text(published),
                    "published_timezone": "Asia/Hong_Kong",
                    "source_kind": "hkexnews_title_search",
                }
            )
        results.sort(key=lambda item: (item["published_at"], item["url"]), reverse=True)
        return results[: max(1, min(int(max_items), 50))]


class FinancialOfficialNewsRepository:
    """Persist verified official index metadata separately from crawled articles."""

    def __init__(self, database):
        self.database = database
        self.database._ensure_connection()
        self.connection = self.database.connection

    def upsert(
        self,
        *,
        instrument_id: int,
        source_id: Optional[int],
        source_key: str,
        item: Mapping[str, object],
        fetched_at: datetime,
    ) -> tuple[int, bool]:
        document_url = HKEXNewsTitleSearchClient._document_url(item.get("url"))
        source_url = str(item.get("source_url") or "").strip()
        source_host = (urlsplit(source_url).hostname or "").casefold()
        title = str(item.get("title") or "").strip()
        if (
            int(instrument_id) <= 0
            or not document_url
            or source_host not in {"www.hkexnews.hk", "www1.hkexnews.hk", "www2.hkexnews.hk"}
            or not title
        ):
            raise ValueError("official news metadata failed source validation")
        published = datetime.fromisoformat(
            str(item.get("published_at") or "").replace("Z", "+00:00")
        )
        if published.tzinfo is None or published.utcoffset() is None:
            raise ValueError("official news publication time must include timezone")
        payload = {
            "document_url": document_url,
            "source_url": source_url,
            "title": title,
            "summary": str(item.get("summary") or "").strip(),
            "stock_code": str(item.get("stock_code") or "").strip(),
            "stock_name": str(item.get("stock_name") or "").strip(),
            "published_at_utc": _utc_text(published),
            "published_timezone": str(item.get("published_timezone") or "Asia/Hong_Kong"),
        }
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        fetched_text = _utc_text(fetched_at)
        with self.database.lock:
            existing = self.connection.execute(
                "SELECT id FROM financial_official_news_items WHERE instrument_id=? AND document_url=?",
                (int(instrument_id), document_url),
            ).fetchone()
            self.connection.execute(
                """
                INSERT INTO financial_official_news_items(
                    instrument_id, source_id, source_key, source_url, document_url,
                    title, summary, stock_code, stock_name, published_at_utc,
                    published_timezone, payload_json, payload_sha256,
                    quality_status, fetched_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                         'verified_official_metadata', ?)
                ON CONFLICT(instrument_id, document_url) DO UPDATE SET
                    source_id=excluded.source_id,
                    source_key=excluded.source_key,
                    source_url=excluded.source_url,
                    title=excluded.title,
                    summary=excluded.summary,
                    stock_code=excluded.stock_code,
                    stock_name=excluded.stock_name,
                    published_at_utc=excluded.published_at_utc,
                    published_timezone=excluded.published_timezone,
                    payload_json=excluded.payload_json,
                    payload_sha256=excluded.payload_sha256,
                    quality_status=excluded.quality_status,
                    fetched_at=excluded.fetched_at,
                    updated_at=(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (
                    int(instrument_id),
                    int(source_id) if source_id else None,
                    str(source_key or "hkexnews_title_search"),
                    source_url,
                    document_url,
                    title,
                    payload["summary"],
                    payload["stock_code"],
                    payload["stock_name"],
                    payload["published_at_utc"],
                    payload["published_timezone"],
                    payload_json,
                    payload_sha256,
                    fetched_text,
                ),
            )
            row = self.connection.execute(
                "SELECT id FROM financial_official_news_items WHERE instrument_id=? AND document_url=?",
                (int(instrument_id), document_url),
            ).fetchone()
            self.connection.commit()
        return int(row[0]), existing is None


__all__ = [
    "FinancialOfficialNewsRepository",
    "HKEXNEWS_PREFIX_URL",
    "HKEXNEWS_TITLE_URL",
    "HKEXNewsTitleSearchClient",
]

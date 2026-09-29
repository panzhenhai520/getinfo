#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Small SerpAPI client with bounded retries and secret-safe errors."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from zoneinfo import ZoneInfo

import requests

import config
from intel_http import sanitize_external_error


class SerpAPIError(RuntimeError):
    pass


class SerpAPIClient:
    ENDPOINT = "https://serpapi.com/search.json"

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        session=None,
        sleep=time.sleep,
    ):
        configured_key = config.SERPAPI_API_KEY if api_key is None else api_key
        self.api_key = str(configured_key or "")
        self.session = session or requests.Session()
        self.sleep = sleep

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @staticmethod
    def _recency_tbs(days: int) -> str:
        """Return an exact Hong-Kong-calendar Google date range.

        ``qdr:w`` is only an approximate week.  The radar instead uses a
        three-day overlapping window so late-indexed news is picked up while
        canonical URL de-duplication prevents repeat crawling.
        """
        # Minimal worker images may not ship the IANA tzdata database.  The
        # radar uses Hong Kong's fixed UTC+08:00 civil time, so retain the
        # correct date range even in those images instead of failing a whole
        # Google discovery pass.
        try:
            hk_timezone = ZoneInfo("Asia/Hong_Kong")
        except (KeyError, OSError):
            hk_timezone = timezone(timedelta(hours=8))
        today = datetime.now(hk_timezone).date()
        start = today - timedelta(days=max(1, int(days)) - 1)
        return (
            f"cdr:1,cd_min:{start.month}/{start.day}/{start.year},"
            f"cd_max:{today.month}/{today.day}/{today.year}"
        )

    @staticmethod
    def _language_restriction(language: str) -> str:
        return {"zh": "lang_zh-CN", "en": "lang_en"}.get(
            str(language or "").casefold(), ""
        )

    def search(
        self, query: str, *, recency_days: Optional[int] = None
    ) -> List[Dict]:
        if not config.SERPAPI_ENABLED:
            return []
        if not self.configured:
            return []
        last_error = None
        for attempt in range(config.SERPAPI_MAX_RETRIES + 1):
            try:
                params = {
                    "api_key": self.api_key,
                    "engine": config.SERPAPI_ENGINE,
                    "q": str(query or "")[:500],
                    "gl": config.SERPAPI_DEFAULT_REGION,
                    "hl": config.SERPAPI_DEFAULT_LANGUAGE,
                }
                effective_recency = (
                    config.SERPAPI_RECENCY_DAYS
                    if recency_days is None
                    else int(recency_days)
                )
                if effective_recency > 0:
                    params["tbs"] = self._recency_tbs(effective_recency)
                language_restriction = self._language_restriction(
                    config.SERPAPI_RESULT_LANGUAGE
                )
                if language_restriction:
                    params["lr"] = language_restriction
                response = self.session.get(
                    self.ENDPOINT,
                    params=params,
                    timeout=config.SERPAPI_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("error"):
                    error_message = str(payload.get("error") or "").strip()
                    # SerpAPI returns HTTP 200 with this message when Google
                    # has no result in the requested date/language window.
                    # It is a valid completed scan with zero discoveries,
                    # not an infrastructure failure.
                    if "hasn't returned any results" in error_message.casefold():
                        return []
                    raise SerpAPIError(f"SerpAPI 返回错误：{error_message[:240]}")
                results = []
                for item in payload.get("organic_results") or []:
                    url = str(item.get("link") or "").strip()
                    if not url:
                        continue
                    results.append(
                        {
                            "url": url,
                            "title": str(item.get("title") or ""),
                            "summary": str(item.get("snippet") or ""),
                            "published_at": item.get("date"),
                        }
                    )
                return results
            except Exception as exc:
                last_error = exc
                if attempt < config.SERPAPI_MAX_RETRIES:
                    self.sleep(min(4, 2**attempt))
        raise SerpAPIError(
            sanitize_external_error(last_error, secrets=(self.api_key,))
            or "SerpAPI 请求失败"
        )

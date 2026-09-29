# -*- coding: utf-8 -*-

"""T4.1 LLM 页面识别兜底：规则分类低置信（<0.6）才调一次本地 LLM，
结果按「域名+URL 模式」缓存 30 天、每域名每日最多 5 次（省 LLM 消耗）。
LLM 失败/不可用 → 降级返回规则结果（低置信），不阻塞爬取。
"""

import re
from datetime import datetime, timedelta
from typing import Dict
from urllib.parse import urlparse

from crawl_listpage import PAGE_ARTICLE, classify_page_type

_CACHE_DAYS = 30
_DAILY_QUOTA = 5
_ALLOWED_TYPES = {PAGE_ARTICLE, "listing", "dynamic", "homepage"}


def ensure_page_type_cache_tables(cursor) -> None:
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS crawl_page_type_cache ("
        "  domain TEXT NOT NULL,"
        "  url_pattern TEXT NOT NULL,"
        "  page_type TEXT NOT NULL,"
        "  confidence REAL NOT NULL DEFAULT 0,"
        "  judged_at TEXT NOT NULL DEFAULT '',"
        "  PRIMARY KEY(domain, url_pattern)"
        ")"
    )
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS crawl_page_llm_quota ("
        "  domain TEXT NOT NULL,"
        "  day TEXT NOT NULL,"
        "  calls INTEGER NOT NULL DEFAULT 0,"
        "  PRIMARY KEY(domain, day)"
        ")"
    )


def _url_pattern(url: str) -> str:
    """URL 模式：路径段中连续数字归一为 N，供同域名同模式缓存复用。"""
    path = (urlparse(url or "").path or "/")
    return re.sub(r"\d+", "N", path)


def _cache_get(db, domain: str, pattern: str):
    from utils import get_china_time
    db._ensure_connection()
    with db.lock:
        cursor = db.connection.cursor()
        try:
            ensure_page_type_cache_tables(cursor)
            row = cursor.execute(
                "SELECT page_type, confidence, judged_at FROM crawl_page_type_cache"
                " WHERE domain=? AND url_pattern=?", (domain, pattern),
            ).fetchone()
            if not row:
                return None
            judged = str(row["judged_at"] or "")[:10]
            if not judged:
                return None
            cutoff = (get_china_time() - timedelta(days=_CACHE_DAYS)).strftime("%Y-%m-%d")
            if judged >= cutoff:
                return {"page_type": str(row["page_type"]), "confidence": float(row["confidence"] or 0)}
            return None
        finally:
            cursor.close()


def _cache_put(db, domain: str, pattern: str, page_type: str, confidence: float) -> None:
    from utils import get_china_time
    now = get_china_time().strftime("%Y-%m-%d %H:%M:%S")
    db._ensure_connection()
    with db.lock:
        cursor = db.connection.cursor()
        try:
            ensure_page_type_cache_tables(cursor)
            cursor.execute(
                "INSERT INTO crawl_page_type_cache(domain, url_pattern, page_type, confidence, judged_at)"
                " VALUES(?,?,?,?,?) "
                "ON CONFLICT(domain, url_pattern) DO UPDATE SET page_type=excluded.page_type,"
                " confidence=excluded.confidence, judged_at=excluded.judged_at",
                (domain, pattern, page_type, confidence, now),
            )
            db.connection.commit()
        finally:
            cursor.close()


def _quota_available(db, domain: str) -> bool:
    from utils import get_china_time
    today = get_china_time().strftime("%Y-%m-%d")
    db._ensure_connection()
    with db.lock:
        cursor = db.connection.cursor()
        try:
            ensure_page_type_cache_tables(cursor)
            row = cursor.execute(
                "SELECT calls FROM crawl_page_llm_quota WHERE domain=? AND day=?", (domain, today)
            ).fetchone()
            return int(row["calls"]) < _DAILY_QUOTA if row else True
        finally:
            cursor.close()


def _quota_inc(db, domain: str) -> None:
    from utils import get_china_time
    today = get_china_time().strftime("%Y-%m-%d")
    db._ensure_connection()
    with db.lock:
        cursor = db.connection.cursor()
        try:
            ensure_page_type_cache_tables(cursor)
            cursor.execute(
                "INSERT INTO crawl_page_llm_quota(domain, day, calls) VALUES(?,?,1) "
                "ON CONFLICT(domain, day) DO UPDATE SET calls=calls+1",
                (domain, today),
            )
            db.connection.commit()
        finally:
            cursor.close()


def _ask_llm(url: str, html: str, llm_client=None) -> str:
    """本地 LLM 判定页面类型（四级之一），失败返回 ''。"""
    import config as _config
    if not getattr(_config, "INTEL_LLM_ENABLED", False):
        return ""
    if llm_client is None:
        from intel_llm_client import intel_llm_client as llm_client
    if not llm_client.configured:
        return ""
    runtime = llm_client._local_runtime()
    text = re.sub(r"<[^>]+>", " ", str(html or "")).strip()
    text = re.sub(r"\s+", " ", text)[:1500]
    payload = {
        "model": runtime["model_id"],
        "stream": False,
        "temperature": 0.0,
        "max_tokens": 60,
        "enable_thinking": False,
        "messages": [
            {"role": "system", "content": "只输出一个词：article、listing、dynamic 或 homepage。"},
            {"role": "user", "content":
             f"判断 URL 与页面文本属于哪类页面。\nURL: {url}\n页面文本: {text}"},
        ],
    }
    try:
        response = llm_client._request_local(runtime, payload,
                                             timeout_seconds=max(30, int(getattr(_config, "INTEL_LLM_TIMEOUT_SECONDS", 120))))
        body = response.json()
        content = str((body.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
    except Exception:
        return ""
    for allowed in _ALLOWED_TYPES:
        if allowed in content.casefold():
            return allowed
    return ""


def classify_page_type_with_llm_fallback(url: str, html: str = "", db=None) -> Dict:
    """规则优先；低置信（<0.6）才按「缓存→限额→LLM」兜底一次。"""
    if db is None:
        from sqlite_database import sqlite_db
        db = sqlite_db
    base = classify_page_type(url=url, html=html)
    if float(base.get("confidence") or 0) >= 0.6:
        return {**base, "llm_used": False, "llm_reason": "rule_confidence_ok"}
    domain = urlparse(url or "").netloc.lower()
    pattern = _url_pattern(url or "")
    if domain and pattern:
        cached = _cache_get(db, domain, pattern)
        if cached:
            return {**base, "page_type": cached["page_type"], "confidence": cached["confidence"],
                    "llm_used": False, "llm_reason": "cache_hit"}
        if not _quota_available(db, domain):
            return {**base, "llm_used": False, "llm_reason": "daily_quota_exhausted"}
    judged = _ask_llm(url or "", html or "")
    if not judged:
        return {**base, "llm_used": False, "llm_reason": "llm_unavailable"}
    result = {**base, "page_type": judged, "confidence": 0.85, "llm_used": True, "llm_reason": "llm_judged"}
    if domain and pattern:
        _cache_put(db, domain, pattern, judged, 0.85)
        _quota_inc(db, domain)
    return result

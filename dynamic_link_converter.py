#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段3：实时动态 HTML 链接 → Markdown 自动转换（只读展示，不写回 articles.content）。

- SSRF 防护复用 intel_http.validate_external_url + SafeHTTPClient（重定向每一跳都校验）；
- markitdown 把已安全抓取的响应（HTML/PDF）转 Markdown；
- 15s 超时、结果 ≤200KB、按 URL 缓存 24h（dynamic_converted 表）；
- 转换失败静默降级：调用方显示原文链接，不报错打扰用户。
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from utils import get_china_time

CONVERT_TIMEOUT_SECONDS = 15
MAX_CONVERT_RESULT_BYTES = 200 * 1024       # 转换结果 ≤ 200KB（超长截断并标记）
CONVERT_CACHE_TTL_SECONDS = 24 * 3600       # 按 URL 缓存 24 小时
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def ensure_dynamic_converted_table(cursor):
    """dynamic_converted 表（幂等）：URL → 转换结果缓存。"""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS dynamic_converted (
            url TEXT PRIMARY KEY,
            markdown TEXT NOT NULL DEFAULT '',
            source_format TEXT NOT NULL DEFAULT '',
            converted_at TEXT NOT NULL,
            last_error TEXT NOT NULL DEFAULT ''
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_dynamic_converted_time ON dynamic_converted(converted_at)"
    )


def get_cached_markdown(db, url: str):
    """命中 24h 缓存返回 Markdown 文本；未命中/过期返回 None。"""
    if not url:
        return None
    try:
        db._ensure_connection()
        with db.lock:
            row = db.connection.execute(
                "SELECT markdown, converted_at FROM dynamic_converted WHERE url=?", (str(url),)
            ).fetchone()
        if not row or not str(row["markdown"] or "").strip():
            return None
        converted_at = str(row["converted_at"] or "")
        try:
            parsed = datetime.fromisoformat(converted_at.replace("Z", "+00:00"))
            age = (get_china_time().replace(tzinfo=None) - parsed.replace(tzinfo=None)).total_seconds()
        except Exception:
            age = CONVERT_CACHE_TTL_SECONDS + 1
        if age > CONVERT_CACHE_TTL_SECONDS:
            return None
        return str(row["markdown"])
    except Exception:
        return None


def _store(db, url: str, markdown: str, source_format: str = "", error: str = ""):
    try:
        db._ensure_connection()
        with db.lock:
            db.connection.execute(
                """
                INSERT INTO dynamic_converted(url, markdown, source_format, converted_at, last_error)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    markdown=excluded.markdown,
                    source_format=excluded.source_format,
                    converted_at=excluded.converted_at,
                    last_error=excluded.last_error
                """,
                (str(url), markdown or "", str(source_format or "")[:200],
                 get_china_time().strftime("%Y-%m-%d %H:%M:%S"), str(error or "")[:400]),
            )
            db.connection.commit()
    except Exception:
        pass


def _fetch_safely(url: str):
    """SSRF 安全抓取：仅 http(s)、禁内网/凭据，重定向每一跳都校验。"""
    from intel_http import SafeHTTPClient, validate_external_url
    validate_external_url(url)
    return SafeHTTPClient().get(url, headers={"User-Agent": USER_AGENT})


def _response_to_markdown(result) -> str:
    """把已安全抓取的响应字节交给 markitdown 转 Markdown（15s 超时）。"""
    import io
    import requests
    from markitdown import MarkItDown
    response = requests.Response()
    # 同时填充 _content 与 raw：requests 的 .text/.content 走 _content，
    # 而部分转换器走 iter_content() → 需要真实可读的 raw 流
    response._content = result.content
    response.raw = io.BytesIO(result.content)
    response.status_code = 200
    response.url = result.url
    response.headers["Content-Type"] = result.content_type
    response.encoding = result.encoding or "utf-8"
    converter = MarkItDown(enable_plugins=False)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(converter.convert_response, response)
        converted = future.result(timeout=CONVERT_TIMEOUT_SECONDS)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return str(getattr(converted, "text_content", "") or "").strip()


def convert_url_to_markdown(db, url: str, *, use_cache: bool = True) -> dict:
    """URL → Markdown（带缓存）。成功返回 {markdown, source_format, cached}；失败抛异常。"""
    url = str(url or "").strip()
    if not url:
        raise ValueError("缺少原文链接")
    if use_cache:
        cached = get_cached_markdown(db, url)
        if cached:
            return {"markdown": cached, "source_format": "cache", "cached": True}
    result = _fetch_safely(url)
    text = _response_to_markdown(result)
    if not text:
        raise ValueError("转换结果为空")
    if len(text.encode("utf-8")) > MAX_CONVERT_RESULT_BYTES:
        cut = MAX_CONVERT_RESULT_BYTES
        while cut > 0:
            try:
                text = text.encode("utf-8")[:cut].decode("utf-8", errors="ignore")
                break
            except Exception:
                cut -= 1
        text = text.strip() + "\n\n*（转换结果过长，已截断，请打开原文查看）*"
    _store(db, url, text, str(result.content_type or "")[:200])
    return {"markdown": text, "source_format": str(result.content_type or ""), "cached": False}


def record_failure(db, url: str, error: str):
    """转换失败记录：只追加错误信息，绝不覆盖已有的成功缓存。"""
    try:
        db._ensure_connection()
        with db.lock:
            db.connection.execute(
                "UPDATE dynamic_converted SET last_error=? WHERE url=?",
                (str(error or "")[:400], str(url)),
            )
            db.connection.commit()
    except Exception:
        pass

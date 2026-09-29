#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""按正式证券身份查询已有 RSS/网页文章，不把文章数字当作行情。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Mapping, Optional

from jsonschema import Draft202012Validator

import config
from financial_evidence import (
    _article_time_interval,
    _canonical_document_url,
    _normalize_text,
    _parse_stored_datetime,
    _safe_json_object,
    _term_occurs,
)


UTC = timezone.utc
NEWS_QUERY_SCHEMA_VERSION = "financial-news-query-v1"
NEWS_QUERY_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "schema_version", "status", "target", "requested_at_utc",
        "completed_at_utc", "lookback_days", "evidence", "answer_allowed",
        "refresh", "route_destination", "reason_codes",
    ],
    "properties": {
        "schema_version": {"const": NEWS_QUERY_SCHEMA_VERSION},
        "status": {"enum": ["skipped", "planned", "ready", "unavailable", "degraded"]},
        "target": {"type": "object"},
        "requested_at_utc": {"type": "string"},
        "completed_at_utc": {"type": "string"},
        "lookback_days": {"type": "integer", "minimum": 1, "maximum": 30},
        "evidence": {"type": "array", "items": {"type": "object"}},
        "answer_allowed": {"type": "boolean"},
        "refresh": {"type": "object"},
        "route_destination": {"type": "string"},
        "reason_codes": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}
_VALIDATOR = Draft202012Validator(NEWS_QUERY_SCHEMA)


def _setting(settings: object, name: str, default: object) -> object:
    if isinstance(settings, Mapping):
        return settings.get(name, default)
    return getattr(settings, name, default)


def _bool_setting(settings: object, name: str, default: bool) -> bool:
    value = _setting(settings, name, default)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _parse_utc(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("datetime must include timezone")
    return parsed.astimezone(UTC)


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def validate_news_query(payload: Mapping[str, object]) -> dict:
    result = dict(payload)
    _VALIDATOR.validate(result)
    return result


def skipped_news_query(reason: str) -> dict:
    return validate_news_query(
        {
            "schema_version": NEWS_QUERY_SCHEMA_VERSION,
            "status": "skipped",
            "target": {},
            "requested_at_utc": "",
            "completed_at_utc": "",
            "lookback_days": 7,
            "evidence": [],
            "answer_allowed": False,
            "refresh": {"status": "skipped", "reason_codes": [str(reason)]},
            "route_destination": "normal_chat",
            "reason_codes": [str(reason)],
        }
    )


def unavailable_news_query(
    reason: str,
    *,
    target: Optional[Mapping[str, object]] = None,
    requested_at_utc: str = "",
    lookback_days: int = 7,
) -> dict:
    return validate_news_query(
        {
            "schema_version": NEWS_QUERY_SCHEMA_VERSION,
            "status": "unavailable",
            "target": dict(target or {}),
            "requested_at_utc": str(requested_at_utc or ""),
            "completed_at_utc": str(requested_at_utc or ""),
            "lookback_days": max(1, min(int(lookback_days), 30)),
            "evidence": [],
            "answer_allowed": False,
            "refresh": {"status": "skipped", "reason_codes": [str(reason)]},
            "route_destination": "financial_latest_news",
            "reason_codes": [str(reason)],
        }
    )


class FinancialNewsQueryService:
    def __init__(
        self,
        database,
        *,
        settings=None,
        clock=None,
        max_items: int = 5,
        refresher=None,
    ):
        self.database = database
        self.settings = config if settings is None else settings
        self.clock = clock or (lambda: datetime.now(UTC))
        self.max_items = max(1, min(int(max_items), 10))
        # Optional seam for an already-authorized RSS/web refresh coordinator.
        # The query service never performs arbitrary web search itself.
        self.refresher = refresher
        self.database._ensure_connection()
        self.connection = self.database.connection

    def plan(
        self,
        target_resolution: Mapping[str, object],
        server_time_context: Mapping[str, object],
        information_needs: Mapping[str, object],
    ) -> dict:
        if "news" not in set(information_needs.get("channels") or []):
            return skipped_news_query("news_channel_not_requested")
        if str(target_resolution.get("status") or "") != "resolved":
            return skipped_news_query("stable_instrument_required")
        targets = list(target_resolution.get("targets") or [])
        if len(targets) != 1:
            return skipped_news_query("single_instrument_news_required")
        now = _parse_utc(server_time_context.get("server_now_utc"))
        lookback = int(_setting(self.settings, "FINANCIAL_NEWS_LOOKBACK_DAYS", 7))
        lookback = max(1, min(lookback, 30))
        if not _bool_setting(
            self.settings, "FINANCIAL_INTELLIGENCE_ENABLED", False
        ) or not _bool_setting(
            self.settings, "FINANCIAL_LATEST_NEWS_ENABLED", True
        ):
            return unavailable_news_query(
                "latest_news_disabled",
                target=targets[0],
                requested_at_utc=_utc_text(now),
                lookback_days=lookback,
            )
        return validate_news_query(
            {
                "schema_version": NEWS_QUERY_SCHEMA_VERSION,
                "status": "planned",
                "target": dict(targets[0]),
                "requested_at_utc": _utc_text(now),
                "completed_at_utc": "",
                "lookback_days": lookback,
                "evidence": [],
                "answer_allowed": False,
                "refresh": {"status": "not_started", "reason_codes": []},
                "route_destination": "financial_latest_news",
                "reason_codes": ["latest_instrument_news_query"],
            }
        )

    def _terms(self, instrument_id: int, start: datetime, end: datetime) -> tuple[str, ...]:
        row = self.connection.execute(
            """
            SELECT canonical_symbol, display_name, provider_mappings_json
            FROM financial_instruments WHERE id=?
            """,
            (int(instrument_id),),
        ).fetchone()
        if row is None:
            return ()
        terms = {str(row[0]), str(row[1])}
        terms.update(str(value) for value in _safe_json_object(row[2]).values() if value)
        rows = self.connection.execute(
            """
            SELECT alias FROM financial_instrument_aliases
            WHERE instrument_id=?
            """,
            (int(instrument_id),),
        ).fetchall()
        # The target is already resolved to one admitted instrument. Historical
        # aliases remain useful entity terms for current filings that mention a
        # former company name; their validity dates still prevent identity
        # resolution from treating an expired name as a current alias.
        terms.update(str(item[0]) for item in rows)
        return tuple(sorted((item for item in terms if item), key=lambda item: (-len(item), item)))

    @staticmethod
    def _published_fields(row: Mapping[str, object], observed: datetime, method: str) -> dict:
        raw = str(row.get("published_at_utc") or row.get("publish_date") or "").strip()
        precision = str(row.get("published_precision") or "").strip().casefold()
        timezone_name = str(row.get("published_timezone") or "UTC").strip() or "UTC"
        source = str(row.get("published_time_source") or method).strip()
        if precision in {"date", "day"} or (len(raw) == 10 and "T" not in raw):
            return {
                "published_at": raw[:10],
                "published_precision": "date",
                "published_timezone": timezone_name,
                "published_time_source": source,
            }
        return {
            "published_at": raw or _utc_text(observed),
            "published_precision": "instant",
            "published_timezone": timezone_name if raw else "UTC",
            "published_time_source": source,
        }

    def _select_evidence(
        self,
        planned: Mapping[str, object],
        cutoff: datetime,
        *,
        latest_available: bool = False,
    ) -> tuple[list[dict], int]:
        recent_start = cutoff - timedelta(days=int(planned["lookback_days"]))
        start = None if latest_available else recent_start
        instrument_id = int(planned["target"]["instrument_id"])
        terms = self._terms(instrument_id, recent_start, cutoff)
        scan_limit = 500
        if latest_available:
            scan_limit = max(
                500,
                min(
                    int(
                        _setting(
                            self.settings,
                            "FINANCIAL_NEWS_LATEST_FALLBACK_SCAN_LIMIT",
                            5000,
                        )
                    ),
                    20000,
                ),
            )
        like_terms = []
        for term in terms[:20]:
            escaped = (
                str(term)
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            if escaped:
                like_terms.append(f"%{escaped}%")
        if not like_terms:
            return [], 0
        predicates = " OR ".join(
            "(title LIKE ? ESCAPE '\\' OR matched_keywords LIKE ? ESCAPE '\\' "
            "OR content LIKE ? ESCAPE '\\')"
            for _ in like_terms
        )
        parameters = []
        for pattern in like_terms:
            parameters.extend((pattern, pattern, pattern))
        parameters.append(scan_limit)
        with self.database.lock:
            rows = [
                dict(row)
                for row in self.connection.execute(
                    f"""
                    SELECT id, url, canonical_url, title, content, domain,
                           publish_date, published_at_utc, published_timezone,
                           published_precision, published_time_source,
                           first_crawled, created_at, updated_at, matched_keywords,
                           source_task_name
                    FROM articles
                    WHERE status='active' AND ({predicates})
                    ORDER BY COALESCE(published_at_utc, publish_date, first_crawled, created_at) DESC, id DESC
                    LIMIT ?
                    """,
                    tuple(parameters),
                ).fetchall()
            ]
        selected = []
        seen_urls = set()
        future_rejected = 0
        for row in rows:
            interval = _article_time_interval(row)
            if interval is None:
                continue
            observed_start, observed_end, method = interval
            fetched = _parse_stored_datetime(row.get("first_crawled"))
            # Fetch time is lineage, not publication time. A document fetched
            # after the request may still prove an event published before the
            # cutoff; only its publication interval decides eligibility.
            if observed_start > cutoff:
                future_rejected += 1
                continue
            if start is not None and observed_end < start:
                continue
            fields = (
                ("title_entity", 1.0, _normalize_text(row.get("title"))),
                ("keyword_entity", 0.95, _normalize_text(row.get("matched_keywords"))),
                ("content_entity", 0.8, _normalize_text(row.get("content"))),
            )
            match = None
            for method_name, score, haystack in fields:
                matched = tuple(term for term in terms if _term_occurs(term, haystack))
                if matched:
                    match = (method_name, score, matched)
                    break
            if match is None:
                continue
            canonical_url = _canonical_document_url(row)
            if canonical_url in seen_urls:
                continue
            seen_urls.add(canonical_url)
            method_name, score, matched = match
            age_days = max(0, int((cutoff - observed_start).total_seconds() // 86400))
            selected.append(
                {
                    "article_id": int(row["id"]),
                    "source_kind": "source_document",
                    "title": str(row.get("title") or ""),
                    "source_url": str(row.get("url") or canonical_url),
                    "domain": str(row.get("domain") or row.get("source_task_name") or ""),
                    "observed_at": _utc_text(observed_start),
                    "fetched_at": _utc_text(fetched or observed_start),
                    "match_method": method_name,
                    "match_score": score,
                    "match_terms": list(matched),
                    "recency_status": (
                        "recent" if observed_end >= recent_start else "latest_available"
                    ),
                    "age_days": age_days,
                    "recent_window_days": int(planned["lookback_days"]),
                    "content_excerpt": str(row.get("content") or "")[:600],
                    "numeric_claim_boundary": "document_is_not_a_quote",
                    **self._published_fields(row, observed_start, method),
                }
            )
        with self.database.lock:
            official_table = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='financial_official_news_items'"
            ).fetchone()
            official_rows = (
                [
                    dict(row)
                    for row in self.connection.execute(
                        """
                        SELECT id, document_url, title, summary, stock_code,
                               stock_name, published_at_utc, published_timezone,
                               payload_json, payload_sha256, quality_status,
                               fetched_at, source_key
                        FROM financial_official_news_items
                        WHERE instrument_id=?
                        ORDER BY published_at_utc DESC, id DESC
                        LIMIT 100
                        """,
                        (instrument_id,),
                    ).fetchall()
                ]
                if official_table is not None
                else []
            )
        for row in official_rows:
            if str(row.get("quality_status") or "") != "verified_official_metadata":
                continue
            payload_text = str(row.get("payload_json") or "")
            if hashlib.sha256(payload_text.encode("utf-8")).hexdigest() != str(
                row.get("payload_sha256") or ""
            ):
                continue
            try:
                observed_start = _parse_utc(row.get("published_at_utc"))
            except ValueError:
                continue
            if observed_start > cutoff:
                future_rejected += 1
                continue
            if start is not None and observed_start < start:
                continue
            document_url = str(row.get("document_url") or "").strip()
            if not document_url or document_url in seen_urls:
                continue
            seen_urls.add(document_url)
            fetched = _parse_stored_datetime(row.get("fetched_at")) or observed_start
            age_days = max(0, int((cutoff - observed_start).total_seconds() // 86400))
            matched_terms = [
                value
                for value in (
                    str(planned["target"].get("canonical_symbol") or ""),
                    str(row.get("stock_code") or ""),
                    str(row.get("stock_name") or ""),
                )
                if value
            ]
            selected.append(
                {
                    "official_news_id": int(row["id"]),
                    "source_kind": "official_announcement_metadata",
                    "title": str(row.get("title") or ""),
                    "source_url": document_url,
                    "domain": str(row.get("source_key") or "HKEXnews"),
                    "observed_at": _utc_text(observed_start),
                    "fetched_at": _utc_text(fetched),
                    "match_method": "official_stock_code",
                    "match_score": 1.0,
                    "match_terms": matched_terms,
                    "recency_status": (
                        "recent" if observed_start >= recent_start else "latest_available"
                    ),
                    "age_days": age_days,
                    "recent_window_days": int(planned["lookback_days"]),
                    "content_excerpt": str(row.get("summary") or "")[:600],
                    "numeric_claim_boundary": "document_is_not_a_quote",
                    "published_at": _utc_text(observed_start),
                    "published_precision": "instant",
                    "published_timezone": str(
                        row.get("published_timezone") or "Asia/Hong_Kong"
                    ),
                    "published_time_source": "hkexnews_release_time",
                }
            )
        selected.sort(
            key=lambda item: (
                item["observed_at"],
                int(item.get("article_id") or item.get("official_news_id") or 0),
            ),
            reverse=True,
        )
        limit = 1 if latest_available else self.max_items
        return selected[:limit], future_rejected

    def _refresh(
        self,
        planned: Mapping[str, object],
        cutoff: datetime,
        *,
        latest_available_only: bool = False,
    ) -> dict:
        if self.refresher is None:
            return {
                "status": "skipped",
                "inserted_count": 0,
                "reason_codes": ["news_refresher_not_configured"],
            }
        try:
            refresh = getattr(self.refresher, "refresh", self.refresher)
            raw = refresh(
                target=dict(planned.get("target") or {}),
                cutoff_at_utc=_utc_text(cutoff),
                lookback_days=int(planned.get("lookback_days") or 7),
                latest_available_only=latest_available_only,
            )
            result = dict(raw) if isinstance(raw, Mapping) else {}
            status = str(result.get("status") or "completed")
            if status not in {"completed", "skipped", "failed", "timed_out"}:
                status = "failed"
            try:
                inserted = max(0, int(result.get("inserted_count") or 0))
            except (TypeError, ValueError):
                inserted = 0
            reasons = [
                str(item)[:160]
                for item in result.get("reason_codes") or []
                if str(item).strip()
            ][:10]
            metrics = {}
            for key in (
                "scanned_source_count",
                "reused_source_count",
                "failed_source_count",
                "matched_candidate_count",
                "dispatched_candidate_count",
                "official_search_source_count",
                "official_search_failed_source_count",
                "official_search_candidate_count",
                "official_search_reused_source_count",
            ):
                try:
                    metrics[key] = max(0, int(result.get(key) or 0))
                except (TypeError, ValueError):
                    metrics[key] = 0
            return {
                "status": status,
                "inserted_count": inserted,
                "reason_codes": reasons or [f"news_refresh_{status}"],
                **metrics,
            }
        except Exception:
            return {
                "status": "failed",
                "inserted_count": 0,
                "reason_codes": ["news_refresh_failed"],
            }

    @staticmethod
    def _merge_refresh(first: Mapping[str, object], second: Mapping[str, object]) -> dict:
        merged = dict(second)
        reasons = []
        for item in list(first.get("reason_codes") or []) + list(
            second.get("reason_codes") or []
        ):
            value = str(item or "")[:160]
            if value and value not in reasons:
                reasons.append(value)
        merged["reason_codes"] = reasons[:20]
        for key in (
            "inserted_count",
            "scanned_source_count",
            "reused_source_count",
            "failed_source_count",
            "matched_candidate_count",
            "dispatched_candidate_count",
            "official_search_source_count",
            "official_search_failed_source_count",
            "official_search_candidate_count",
            "official_search_reused_source_count",
        ):
            try:
                merged[key] = max(0, int(first.get(key) or 0)) + max(
                    0, int(second.get(key) or 0)
                )
            except (TypeError, ValueError):
                merged[key] = 0
        return merged

    def execute(self, query: Mapping[str, object]) -> dict:
        planned = validate_news_query(query)
        if planned["status"] != "planned":
            return planned
        cutoff = _parse_utc(planned["requested_at_utc"])
        selected, future_rejected = self._select_evidence(planned, cutoff)
        refresh = {"status": "not_needed", "inserted_count": 0, "reason_codes": []}
        if not selected:
            refresh = self._refresh(planned, cutoff)
            if refresh["status"] == "completed":
                refreshed, refreshed_future = self._select_evidence(planned, cutoff)
                selected = refreshed
                future_rejected = max(future_rejected, refreshed_future)
        if not selected:
            latest, latest_future = self._select_evidence(
                planned,
                cutoff,
                latest_available=True,
            )
            selected = latest
            future_rejected = max(future_rejected, latest_future)
        latest_search_already_attempted = any(
            "latest_available" in str(reason)
            for reason in refresh.get("reason_codes") or []
        )
        if (
            not selected
            and refresh.get("status") == "completed"
            and not latest_search_already_attempted
        ):
            latest_refresh = self._refresh(
                planned,
                cutoff,
                latest_available_only=True,
            )
            refresh = self._merge_refresh(refresh, latest_refresh)
            if latest_refresh.get("status") == "completed":
                latest, latest_future = self._select_evidence(
                    planned,
                    cutoff,
                    latest_available=True,
                )
                selected = latest
                future_rejected = max(future_rejected, latest_future)
        execution_now = self.clock()
        if execution_now.tzinfo is None or execution_now.utcoffset() is None:
            raise ValueError("news query clock must be timezone-aware")
        completed = max(cutoff, execution_now.astimezone(UTC))
        return validate_news_query(
            {
                **dict(planned),
                "status": "ready" if selected else "unavailable",
                "completed_at_utc": _utc_text(completed),
                "evidence": selected,
                "answer_allowed": bool(selected),
                "refresh": refresh,
                "reason_codes": list(planned.get("reason_codes") or [])
                + (
                    [
                        "matched_latest_available_article"
                        if any(
                            item.get("recency_status") == "latest_available"
                            for item in selected
                        )
                        else "matched_latest_articles"
                    ]
                    if selected
                    else ["no_matching_news_available"]
                )
                + (["future_articles_rejected"] if future_rejected else []),
            }
        )


def format_news_query_answer(
    query: Mapping[str, object],
    server_time_context: Optional[Mapping[str, object]] = None,
) -> str:
    target = query.get("target") or {}
    name = str(target.get("display_name") or "金融标的")
    symbol = str(target.get("canonical_symbol") or "")
    parts = [f"{name}（{symbol}）最新可核验新闻："]
    evidence = list(query.get("evidence") or [])
    if not evidence:
        parts.append("\n截至请求时间没有匹配且可核验的新闻或公告消息；不会调用通用模型补造标题、事件或价格。")
    else:
        lines = []
        for item in evidence:
            precision = str(item.get("published_precision") or "")
            published = str(item.get("published_at") or "")
            when = (
                f"发布日期={published}（仅日期精度，原时区={item.get('published_timezone') or '未知'}）"
                if precision == "date"
                else f"发布时间={published}"
            )
            source = str(item.get("domain") or "")
            url = str(item.get("source_url") or "")
            recency = ""
            if item.get("recency_status") == "latest_available":
                recency = (
                    f"；最近可得（已超出{int(item.get('recent_window_days') or 7)}天近期窗口，"
                    f"距请求截止约{int(item.get('age_days') or 0)}天）"
                )
            lines.append(
                f"- {item.get('title') or '未命名文章'}；{when}{recency}；来源={source}"
                + (f"（{url}）" if url else "")
            )
        parts.append("\n" + "\n".join(lines))
    context = server_time_context or {}
    parts.append(
        "\n时间口径：请求截止="
        + str(query.get("requested_at_utc") or context.get("server_now_utc") or "")
        + "；新闻发布时间与聚合时间分别保存。"
    )
    parts.append("\n新闻文档不是结构化行情，文中数字不会被当作当前股价。")
    return "".join(parts)


__all__ = [
    "FinancialNewsQueryService",
    "NEWS_QUERY_SCHEMA_VERSION",
    "format_news_query_answer",
    "skipped_news_query",
    "unavailable_news_query",
    "validate_news_query",
]

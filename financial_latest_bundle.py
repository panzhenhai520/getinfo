#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""把同一请求下的行情与新闻作为独立证据通道组合，不混用时间或数字。"""

from __future__ import annotations

import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from typing import Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import Draft202012Validator

from financial_answer_composer import (
    FINANCIAL_ANSWER_DISCLAIMER,
    compose_realtime_query_answer,
)
from financial_latest_time import LatestAvailableTimeResolver
from financial_market_clock import RequestTimeContext
from financial_news_query import skipped_news_query
from financial_realtime_query import skipped_realtime_query


LATEST_BUNDLE_SCHEMA_VERSION = "financial-latest-bundle-v1"
LATEST_BUNDLE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "schema_version", "status", "channels", "requested_at_utc",
        "completed_at_utc", "quote", "news", "latest_available",
        "execution", "answer_allowed", "route_destination", "reason_codes",
    ],
    "properties": {
        "schema_version": {"const": LATEST_BUNDLE_SCHEMA_VERSION},
        "status": {
            "enum": ["skipped", "planned", "ready", "partial", "unavailable"]
        },
        "channels": {
            "type": "array",
            "items": {"enum": ["quote", "news"]},
            "uniqueItems": True,
            "maxItems": 2,
        },
        "requested_at_utc": {"type": "string"},
        "completed_at_utc": {"type": "string"},
        "quote": {"type": "object"},
        "news": {"type": "object"},
        "latest_available": {"type": "object"},
        "execution": {
            "type": "object",
            "required": ["strategy", "deadline_seconds", "elapsed_ms", "channels"],
            "properties": {
                "strategy": {"enum": ["not_started", "bounded", "parallel"]},
                "deadline_seconds": {"type": "number", "minimum": 0},
                "elapsed_ms": {"type": "integer", "minimum": 0},
                "channels": {"type": "object"},
            },
            "additionalProperties": False,
        },
        "answer_allowed": {"type": "boolean"},
        "route_destination": {"type": "string"},
        "reason_codes": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}
_VALIDATOR = Draft202012Validator(LATEST_BUNDLE_SCHEMA)


def validate_latest_bundle(payload: Mapping[str, object]) -> dict:
    result = dict(payload)
    _VALIDATOR.validate(result)
    return result


def _setting(settings: object, name: str, default: object) -> object:
    if isinstance(settings, Mapping):
        return settings.get(name, default)
    return getattr(settings, name, default)


def _bool_setting(settings: object, name: str, default: bool) -> bool:
    value = _setting(settings, name, default)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _float_setting(
    settings: object,
    name: str,
    default: float,
    *,
    minimum: float = 0.01,
) -> float:
    try:
        value = float(_setting(settings, name, default))
    except (TypeError, ValueError):
        value = float(default)
    return max(minimum, value)


def skipped_latest_bundle(reason: str) -> dict:
    return validate_latest_bundle({
        "schema_version": LATEST_BUNDLE_SCHEMA_VERSION,
        "status": "skipped",
        "channels": [],
        "requested_at_utc": "",
        "completed_at_utc": "",
        "quote": skipped_realtime_query(reason),
        "news": skipped_news_query(reason),
        "latest_available": {},
        "execution": {
            "strategy": "not_started",
            "deadline_seconds": 0.0,
            "elapsed_ms": 0,
            "channels": {},
        },
        "answer_allowed": False,
        "route_destination": "normal_chat",
        "reason_codes": [str(reason)],
    })


def plan_latest_bundle(
    information_needs: Mapping[str, object],
    quote_query: Mapping[str, object],
    news_query: Mapping[str, object],
    *,
    settings=None,
) -> dict:
    channels = list(information_needs.get("channels") or [])
    if "news" not in channels:
        return skipped_latest_bundle("latest_bundle_not_required")
    if settings is not None and (
        not _bool_setting(settings, "FINANCIAL_INTELLIGENCE_ENABLED", False)
        or not _bool_setting(settings, "FINANCIAL_LATEST_BUNDLE_ENABLED", True)
    ):
        return skipped_latest_bundle("latest_bundle_disabled")
    requested_at = str(
        quote_query.get("requested_at_utc")
        or news_query.get("requested_at_utc")
        or ""
    )
    if not requested_at:
        return skipped_latest_bundle("latest_bundle_target_not_ready")
    return validate_latest_bundle({
        "schema_version": LATEST_BUNDLE_SCHEMA_VERSION,
        "status": "planned",
        "channels": [item for item in ("quote", "news") if item in channels],
        "requested_at_utc": requested_at,
        "completed_at_utc": "",
        "quote": dict(quote_query),
        "news": dict(news_query),
        "latest_available": {},
        "execution": {
            "strategy": "not_started",
            "deadline_seconds": 0.0,
            "elapsed_ms": 0,
            "channels": {},
        },
        "answer_allowed": False,
        "route_destination": "financial_latest_bundle",
        "reason_codes": [
            "parallel_quote_news_bundle_planned"
            if len(set(channels).intersection({"quote", "news"})) > 1
            else "single_news_bundle_planned"
        ],
    })


class FinancialLatestBundleService:
    def __init__(
        self,
        *,
        realtime_service=None,
        news_service=None,
        latest_time_resolver: Optional[LatestAvailableTimeResolver] = None,
        settings=None,
        monotonic=None,
    ):
        self.realtime_service = realtime_service
        self.news_service = news_service
        self.latest_time_resolver = latest_time_resolver or LatestAvailableTimeResolver()
        self.settings = settings
        self.monotonic = monotonic or time.monotonic

    @staticmethod
    def _unavailable_channel(
        query: Mapping[str, object],
        reason: str,
    ) -> dict:
        result = dict(query)
        result.update(
            {
                "status": "unavailable",
                "completed_at_utc": str(query.get("requested_at_utc") or ""),
                "answer_allowed": False,
                "reason_codes": list(query.get("reason_codes") or []) + [reason],
            }
        )
        if "numeric_claims_allowed" in result:
            result["numeric_claims_allowed"] = False
        if "refresh" in result and not isinstance(result.get("refresh"), Mapping):
            result["refresh"] = {}
        if "refresh" in result and str(reason).endswith("_timeout"):
            result["refresh"] = {
                "status": "timed_out",
                "inserted_count": 0,
                "reason_codes": [str(reason)],
            }
        return result

    @staticmethod
    def _execute_channel(service, query: Mapping[str, object], unavailable_reason: str) -> dict:
        if str(query.get("status") or "") != "planned":
            return dict(query)
        if service is None:
            return FinancialLatestBundleService._unavailable_channel(
                query, unavailable_reason
            )
        try:
            return service.execute(query)
        except Exception:
            return FinancialLatestBundleService._unavailable_channel(
                query, unavailable_reason
            )

    def _timeouts(self) -> tuple[dict[str, float], float]:
        settings = self.settings or {}
        channels = {
            "quote": _float_setting(
                settings, "FINANCIAL_LATEST_QUOTE_TIMEOUT_SECONDS", 6.0
            ),
            "news": _float_setting(
                settings, "FINANCIAL_LATEST_NEWS_TIMEOUT_SECONDS", 40.0
            ),
        }
        total = _float_setting(
            settings, "FINANCIAL_LATEST_BUNDLE_TIMEOUT_SECONDS", 45.0
        )
        return channels, total

    def _execute_channels(
        self,
        bundle: Mapping[str, object],
    ) -> tuple[dict[str, dict], dict]:
        requested = [
            item for item in ("quote", "news")
            if item in set(bundle.get("channels") or [])
        ]
        queries = {
            "quote": dict(bundle.get("quote") or {}),
            "news": dict(bundle.get("news") or {}),
        }
        services = {
            "quote": self.realtime_service,
            "news": self.news_service,
        }
        failure_reasons = {
            "quote": "quote_channel_execution_failed",
            "news": "news_channel_execution_failed",
        }
        results = {
            channel: dict(query)
            for channel, query in queries.items()
            if str(query.get("status") or "") != "planned"
        }
        planned = [
            channel for channel in requested
            if str(queries[channel].get("status") or "") == "planned"
        ]
        channel_timeouts, total_timeout = self._timeouts()
        started = self.monotonic()
        timings: dict[str, dict] = {
            channel: {
                "status": "not_requested" if channel not in requested else "not_planned",
                "timeout_seconds": channel_timeouts[channel],
                "elapsed_ms": 0,
            }
            for channel in ("quote", "news")
        }
        if not planned:
            return results, {
                "strategy": "bounded",
                "deadline_seconds": total_timeout,
                "elapsed_ms": max(0, int((self.monotonic() - started) * 1000)),
                "channels": timings,
            }

        executor = ThreadPoolExecutor(
            max_workers=len(planned),
            thread_name_prefix="financial-latest",
        )

        def run(channel: str):
            channel_started = self.monotonic()
            result = self._execute_channel(
                services[channel], queries[channel], failure_reasons[channel]
            )
            return result, channel_started, self.monotonic()

        futures = {executor.submit(run, channel): channel for channel in planned}
        pending = set(futures)
        deadlines = {
            channel: started + min(channel_timeouts[channel], total_timeout)
            for channel in planned
        }
        total_deadline = started + total_timeout
        try:
            while pending:
                done_now = {future for future in pending if future.done()}
                if not done_now:
                    now = self.monotonic()
                    expired = [
                        future
                        for future in pending
                        if now >= deadlines[futures[future]] or now >= total_deadline
                    ]
                    if expired:
                        for future in expired:
                            channel = futures[future]
                            results[channel] = self._unavailable_channel(
                                queries[channel], f"{channel}_channel_timeout"
                            )
                            timings[channel] = {
                                "status": "timed_out",
                                "timeout_seconds": channel_timeouts[channel],
                                "elapsed_ms": max(0, int((now - started) * 1000)),
                            }
                            future.cancel()
                            pending.remove(future)
                        continue
                    next_deadline = min(
                        [total_deadline]
                        + [deadlines[futures[future]] for future in pending]
                    )
                    done_now, _ = wait(
                        pending,
                        timeout=max(0.0, next_deadline - now),
                        return_when=FIRST_COMPLETED,
                    )
                    if not done_now:
                        continue
                for future in done_now:
                    if future not in pending:
                        continue
                    channel = futures[future]
                    try:
                        result, channel_started, finished = future.result()
                    except Exception:
                        result = self._unavailable_channel(
                            queries[channel], failure_reasons[channel]
                        )
                        channel_started = started
                        finished = self.monotonic()
                    if finished > deadlines[channel] or finished > total_deadline:
                        result = self._unavailable_channel(
                            queries[channel], f"{channel}_channel_timeout"
                        )
                        status = "timed_out"
                    else:
                        status = "completed"
                    results[channel] = result
                    timings[channel] = {
                        "status": status,
                        "timeout_seconds": channel_timeouts[channel],
                        "elapsed_ms": max(
                            0, int((finished - channel_started) * 1000)
                        ),
                    }
                    pending.remove(future)
        finally:
            for future in pending:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
        return results, {
            "strategy": "parallel" if len(planned) > 1 else "bounded",
            "deadline_seconds": total_timeout,
            "elapsed_ms": max(0, int((self.monotonic() - started) * 1000)),
            "channels": timings,
        }

    def execute(
        self,
        bundle: Mapping[str, object],
        server_time_context: Mapping[str, object],
    ) -> dict:
        planned = validate_latest_bundle(bundle)
        if str(planned.get("status") or "") != "planned":
            return planned
        bundle = planned
        if self.settings is not None and (
            not _bool_setting(
                self.settings, "FINANCIAL_INTELLIGENCE_ENABLED", False
            )
            or not _bool_setting(
                self.settings, "FINANCIAL_LATEST_BUNDLE_ENABLED", True
            )
        ):
            return validate_latest_bundle({
                **dict(bundle),
                "status": "unavailable",
                "quote": self._unavailable_channel(
                    bundle.get("quote") or {}, "latest_bundle_disabled"
                ),
                "news": self._unavailable_channel(
                    bundle.get("news") or {}, "latest_bundle_disabled"
                ),
                "answer_allowed": False,
                "reason_codes": list(bundle.get("reason_codes") or [])
                + ["latest_bundle_disabled"],
            })
        channel_results, execution = self._execute_channels(bundle)
        quote = channel_results.get("quote", dict(bundle.get("quote") or {}))
        news = channel_results.get("news", dict(bundle.get("news") or {}))
        now = datetime.fromisoformat(
            str(server_time_context.get("server_now_utc") or bundle["requested_at_utc"]).replace(
                "Z", "+00:00"
            )
        )
        context = RequestTimeContext(
            server_now_utc=now,
            server_timezone=str(server_time_context.get("server_timezone") or "UTC"),
            user_timezone=str(server_time_context.get("user_timezone") or "UTC"),
        )
        latest = self.latest_time_resolver.resolve(
            context,
            quote_records=quote.get("evidence") or [],
            news_records=news.get("evidence") or [],
        )
        market = quote.get("market_session") or {}
        latest.update(
            {
                "market_timezone": str(market.get("market_timezone") or ""),
                "market_calendar_id": str(market.get("market_calendar_id") or ""),
                "market_session_state": str(market.get("market_session_state") or "unknown"),
                "calendar_version": str(market.get("calendar_version") or ""),
                "calendar_source": str(market.get("calendar_source") or ""),
            }
        )
        requested = list(bundle.get("channels") or [])
        available = {
            # A conflict may carry traceable snapshots for explanation while
            # still forbidding a single numeric quote. It is therefore not a
            # successful quote channel for bundle completeness.
            "quote": bool(
                quote.get("answer_allowed")
                and quote.get("numeric_claims_allowed")
            ),
            "news": bool(news.get("answer_allowed")),
        }
        available_count = sum(1 for channel in requested if available.get(channel))
        status = (
            "ready"
            if available_count == len(requested)
            else ("partial" if available_count else "unavailable")
        )
        completed_values = [
            str(item.get("completed_at_utc") or "")
            for item in (quote, news)
            if str(item.get("completed_at_utc") or "")
        ]
        return validate_latest_bundle({
            **dict(bundle),
            "status": status,
            "completed_at_utc": max(completed_values) if completed_values else str(bundle["requested_at_utc"]),
            "quote": quote,
            "news": news,
            "latest_available": latest,
            "execution": execution,
            "answer_allowed": bool(available_count),
            "reason_codes": list(bundle.get("reason_codes") or [])
            + [
                f"{channel}_channel_timeout"
                for channel, item in execution.get("channels", {}).items()
                if item.get("status") == "timed_out"
            ]
            + [f"bundle_{status}"],
        })


def format_latest_bundle_answer(
    bundle: Mapping[str, object],
    server_time_context: Optional[Mapping[str, object]] = None,
) -> str:
    quote = bundle.get("quote") or {}
    news = bundle.get("news") or {}
    target = quote.get("target") or news.get("target") or {}
    name = str(target.get("display_name") or "金融标的")
    symbol = str(target.get("canonical_symbol") or "")
    parts = [f"{name}（{symbol}）最新情况："]

    def provider_gap_summaries(refresh: Mapping[str, object]) -> list[str]:
        summaries = []
        seen = set()
        for item in refresh.get("provider_errors") or []:
            provider_id = str(item.get("provider_id") or "provider")
            seen.add(provider_id)
            summaries.append(
                f"{provider_id}:"
                f"{item.get('error_type') or item.get('error_code') or 'failed'}"
            )
        for item in refresh.get("provider_eligibility") or []:
            provider_id = str(item.get("provider_id") or "provider")
            if provider_id in seen or item.get("eligible"):
                continue
            summaries.append(
                f"{provider_id}:{item.get('reason') or 'ineligible'}"
            )
        return summaries[:8]

    def source_observations() -> list[dict]:
        refresh = quote.get("refresh") or {}
        observations = list(refresh.get("source_observations") or [])
        if not observations:
            observations = list(quote.get("evidence") or [])
        selected = []
        seen = set()
        for item in observations[:8]:
            provider_id = str(item.get("provider_id") or "provider")
            snapshot_id = item.get("snapshot_id")
            unique_key = (provider_id, snapshot_id, str(item.get("observed_at") or ""))
            if unique_key in seen:
                continue
            seen.add(unique_key)
            try:
                price = float(item.get("price"))
            except (TypeError, ValueError):
                continue
            observed_at = str(item.get("observed_at") or "")
            if not observed_at:
                continue
            selected.append({**dict(item), "price": price})
        return selected

    def latest_source_observation_text() -> str:
        observations = source_observations()
        if not observations:
            return ""
        latest = max(observations, key=lambda item: str(item.get("observed_at") or ""))
        provider = str(
            latest.get("provider_display_name") or latest.get("provider_id") or "provider"
        )
        return (
            f"最新来源报价（单源观察，未交叉核验）：{latest['price']:g} "
            f"{latest.get('currency') or ''}；来源={provider}；"
            f"observed_at={latest.get('observed_at') or ''}；"
            f"时效={latest.get('freshness') or 'unknown'}。"
        )

    def source_observation_lines() -> list[str]:
        lines = []
        for item in source_observations():
            provider_id = str(item.get("provider_id") or "provider")
            snapshot_id = item.get("snapshot_id")
            provider = str(
                item.get("provider_display_name") or provider_id
            )
            freshness = str(item.get("freshness") or "unknown")
            snapshot = f"；snapshot #{snapshot_id}" if snapshot_id else ""
            lines.append(
                f"- {provider}：{item['price']:g} {item.get('currency') or ''}；"
                f"observed_at={item.get('observed_at') or ''}；"
                f"fetched_at={item.get('fetched_at') or ''}；"
                f"时效={freshness}{snapshot}"
            )
        return lines

    if "quote" in set(bundle.get("channels") or []):
        composed = compose_realtime_query_answer(quote, server_time_context)
        facts = list(composed.get("current_facts") or []) + list(
            composed.get("historical_facts") or []
        )
        if facts:
            fact = facts[0]
            value = fact.get("value") or {}
            number = value.get("number") if isinstance(value, Mapping) else value
            try:
                number_text = f"{float(number):g}"
            except (TypeError, ValueError):
                number_text = ""
            label = (
                "核验价格"
                if fact.get("verification_status") == "verified_current"
                else "最近一次（非实时）价格"
            )
            citations = []
            for citation in fact.get("citations") or []:
                source = str(citation.get("label") or citation.get("provider") or "来源")
                snapshot_id = citation.get("snapshot_id")
                url = str(citation.get("url") or "")
                detail = source
                if snapshot_id:
                    detail += f" snapshot #{snapshot_id}"
                if url:
                    detail += f" {url}"
                citations.append(detail)
            source_text = "；".join(citations)
            parts.append(
                f"\n行情：{label} {number_text} {fact.get('currency') or ''}"
                f"（{fact.get('conflict_verdict') or 'verified'}；截至 {fact.get('as_of') or ''}）"
                + (f"。来源：{source_text}" if source_text else "")
            )
        elif str(quote.get("status") or "") == "conflict":
            observations = source_observation_lines()
            parts.append(
                "\n行情：当前来源存在实质冲突，本轮不选择单一实时价格。"
                + (
                    "\n" + latest_source_observation_text()
                    + "\n来源观察（不代表当前价共识）：\n" + "\n".join(observations)
                    if observations else ""
                )
            )
        elif quote.get("evidence"):
            gaps = list(composed.get("conflicts_and_gaps") or [])
            source_gaps = provider_gap_summaries(quote.get("refresh") or {})
            observations = source_observation_lines()
            verdict = ""
            for gap in gaps:
                for detail in gap.get("details") or []:
                    if str(detail).startswith("conflict_verdict="):
                        verdict = str(detail).split("=", 1)[1]
                        break
            parts.append(
                f"\n行情：已取得 {len(quote.get('evidence') or [])} 个可追溯快照，"
                "但未达到双来源一致或官方来源门槛，未形成可发布的单一当前价格"
                + (f"（{verdict}）" if verdict else "")
                + (f"；未成功来源={'；'.join(source_gaps)}" if source_gaps else "")
                + "。"
                + (
                    "\n" + latest_source_observation_text()
                    + "\n来源观察（不代表当前价共识）：\n" + "\n".join(observations)
                    if observations else ""
                )
            )
        else:
            refresh = quote.get("refresh") or {}
            error_code = str(refresh.get("error_code") or "no_data")
            summaries = provider_gap_summaries(refresh)
            parts.append(
                f"\n行情：暂时没有可核验的价格快照，error_code={error_code}"
                + (f"；来源摘要={'；'.join(summaries[:5])}" if summaries else "")
                + "。不会由通用模型补造行情数字。"
            )
    if "news" in set(bundle.get("channels") or []):
        evidence = list(news.get("evidence") or [])
        if not evidence:
            refresh = news.get("refresh") or {}
            refresh_status = str(refresh.get("status") or "not_started")
            reasons = ",".join(str(item) for item in refresh.get("reason_codes") or [])
            parts.append(
                "\n新闻：截至请求时间没有匹配且可核验的新闻或公告消息"
                f"（刷新={refresh_status}"
                + (f"，原因={reasons}" if reasons else "")
                + "）；不会用通用模型补造标题或事件。"
            )
        else:
            lines = []
            for item in evidence:
                precision = str(item.get("published_precision") or "")
                published = str(item.get("published_at") or "")
                time_text = (
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
                    f"- {item.get('title') or '未命名文章'}；{time_text}{recency}；来源={source}"
                    + (f"（{url}）" if url else "")
                )
            parts.append("\n新闻：\n" + "\n".join(lines))
    latest = bundle.get("latest_available") or {}
    context = server_time_context or {}
    cutoff = str(latest.get("cutoff_at_utc") or context.get("server_now_utc") or "")
    user_timezone = str(
        latest.get("user_timezone") or context.get("user_timezone") or "UTC"
    )
    local_cutoff = cutoff
    try:
        parsed_cutoff = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
        if parsed_cutoff.tzinfo is not None:
            local_cutoff = parsed_cutoff.astimezone(ZoneInfo(user_timezone)).isoformat(
                timespec="seconds"
            )
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        local_cutoff = cutoff
    parts.append(
        "\n时间：请求截止="
        + cutoff
        + "；用户时区="
        + user_timezone
        + "；本地时间="
        + local_cutoff
        + "；市场时区="
        + str(latest.get("market_timezone") or "未知")
        + "。"
    )
    parts.append("\n边界：新闻文档不是结构化行情，文中数字不会被当作当前股价。")
    parts.append("\n免责声明：" + FINANCIAL_ANSWER_DISCLAIMER)
    return "".join(parts)


def latest_bundle_source_records(bundle: Mapping[str, object]) -> list[dict]:
    records = [
        {**dict(item), "source_kind": "financial_snapshot"}
        for item in (bundle.get("quote") or {}).get("evidence") or []
        if isinstance(item, Mapping)
    ]
    records.extend(
        dict(item)
        for item in (bundle.get("news") or {}).get("evidence") or []
        if isinstance(item, Mapping)
    )
    return records


__all__ = [
    "FinancialLatestBundleService",
    "LATEST_BUNDLE_SCHEMA",
    "LATEST_BUNDLE_SCHEMA_VERSION",
    "format_latest_bundle_answer",
    "latest_bundle_source_records",
    "plan_latest_bundle",
    "skipped_latest_bundle",
    "validate_latest_bundle",
]

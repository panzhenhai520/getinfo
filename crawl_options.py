#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Shared crawl-strategy option normalization.

These options belong to the Playwright-first article crawler. They deliberately
do not expose or call Firecrawl.
"""

import os
from typing import Any, Dict

import config


TRUE_VALUES = {"1", "true", "yes", "on", "y", "enabled"}
FALSE_VALUES = {"0", "false", "no", "off", "n", "disabled"}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return _to_bool(value, default)


def _env_int(name: str, default: int, min_value: int, max_value: int) -> int:
    return _to_int(os.getenv(name), default, min_value, max_value)


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    return default


def _to_int(value: Any, default: int, min_value: int, max_value: int) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(parsed, max_value))


def _raw_options(raw: Dict[str, Any] = None) -> Dict[str, Any]:
    raw = raw or {}
    nested = raw.get("crawl_options")
    if isinstance(nested, dict):
        combined = dict(nested)
        for key, value in raw.items():
            if key != "crawl_options" and key not in combined:
                combined[key] = value
        return combined
    return dict(raw)


def proxy_is_configured() -> bool:
    return bool(
        getattr(config, "PLAYWRIGHT_PROXY", None)
        or getattr(config, "PROXY_HTTP", None)
        or getattr(config, "PROXY_HTTPS", None)
        or getattr(config, "PROXY_SOCKS5", None)
    )


def default_crawl_options() -> Dict[str, Any]:
    return {
        "wait_for_ms": _env_int("CRAWL_RENDER_WAIT_MS", 8000, 1000, 60000),
        "max_pages": _env_int("CRAWL_LINK_DISCOVERY_MAX_PAGES", 30, 1, 1000),
        "max_empty_pages": _env_int("CRAWL_PLAYWRIGHT_MAX_EMPTY_PAGES", 5, 1, 50),
        "detail_max_retries": _env_int("CRAWL_DETAIL_MAX_RETRIES", 2, 1, 5),
        "date_range_priority": _env_bool("CRAWL_DATE_RANGE_PRIORITY", True),
        "candidate_date_prefilter": _env_bool("CRAWL_PREFILTER_CANDIDATE_DATES", True),
        "network_json_enabled": _env_bool("CRAWL_NETWORK_JSON_ENABLED", True),
        "supplemental_enabled": _env_bool("CRAWL_SUPPLEMENTAL_ENABLED", True),
        "supplemental_html": _env_bool("CRAWL_SUPPLEMENTAL_HTML_ENABLED", True),
        "supplemental_attributes": _env_bool("CRAWL_SUPPLEMENTAL_ATTRIBUTES_ENABLED", True),
        "supplemental_structured": _env_bool("CRAWL_SUPPLEMENTAL_STRUCTURED_ENABLED", True),
        "supplemental_scripts": _env_bool("CRAWL_SUPPLEMENTAL_SCRIPTS_ENABLED", True),
        "supplemental_static_pagination": _env_bool("CRAWL_SUPPLEMENTAL_STATIC_PAGINATION_ENABLED", True),
        "supplemental_feeds": _env_bool("CRAWL_SUPPLEMENTAL_FEEDS_ENABLED", True),
        "supplemental_sitemaps": _env_bool("CRAWL_SUPPLEMENTAL_SITEMAPS_ENABLED", True),
        "supplemental_cache_enabled": _env_bool("CRAWL_SUPPLEMENTAL_CACHE_ENABLED", True),
        "supplemental_cache_ttl_seconds": _env_int("CRAWL_SUPPLEMENTAL_CACHE_TTL_SECONDS", 900, 60, 86400),
        "supplemental_retry_attempts": _env_int("CRAWL_SUPPLEMENTAL_RETRY_ATTEMPTS", 3, 1, 8),
        "supplemental_max_per_source": _env_int("CRAWL_SUPPLEMENTAL_MAX_PER_SOURCE", 500, 100, 5000),
        "supplemental_max_sitemaps": _env_int("CRAWL_SUPPLEMENTAL_MAX_SITEMAPS", 25, 1, 200),
        "supplemental_max_static_pages": _env_int("CRAWL_SUPPLEMENTAL_MAX_STATIC_PAGES", 8, 1, 100),
        "max_articles_per_task": _env_int("CRAWL_MAX_ARTICLES_PER_TASK", 100, 1, 1000),
        "browserforge_enabled": _env_bool("CRAWL_BROWSERFORGE_HEADERS_ENABLED", True),
        "proxy_enabled": _env_bool("CRAWL_USE_PROXY_DEFAULT", False),
    }


def normalize_crawl_options(raw: Dict[str, Any] = None) -> Dict[str, Any]:
    defaults = default_crawl_options()
    options = _raw_options(raw)

    normalized = {
        "wait_for_ms": _to_int(
            options.get("wait_for_ms", options.get("wait_for", options.get("render_wait_ms"))),
            defaults["wait_for_ms"],
            1000,
            60000,
        ),
        "max_pages": _to_int(options.get("max_pages"), defaults["max_pages"], 1, 1000),
        "max_empty_pages": _to_int(options.get("max_empty_pages"), defaults["max_empty_pages"], 1, 50),
        "detail_max_retries": _to_int(
            options.get("detail_max_retries", options.get("max_extract_attempts")),
            defaults["detail_max_retries"],
            1,
            5,
        ),
        "date_range_priority": _to_bool(options.get("date_range_priority"), defaults["date_range_priority"]),
        "candidate_date_prefilter": _to_bool(
            options.get("candidate_date_prefilter"),
            defaults["candidate_date_prefilter"],
        ),
        "network_json_enabled": _to_bool(options.get("network_json_enabled"), defaults["network_json_enabled"]),
        "supplemental_enabled": _to_bool(options.get("supplemental_enabled"), defaults["supplemental_enabled"]),
        "supplemental_html": _to_bool(options.get("supplemental_html"), defaults["supplemental_html"]),
        "supplemental_attributes": _to_bool(
            options.get("supplemental_attributes"),
            defaults["supplemental_attributes"],
        ),
        "supplemental_structured": _to_bool(
            options.get("supplemental_structured"),
            defaults["supplemental_structured"],
        ),
        "supplemental_scripts": _to_bool(options.get("supplemental_scripts"), defaults["supplemental_scripts"]),
        "supplemental_static_pagination": _to_bool(
            options.get("supplemental_static_pagination"),
            defaults["supplemental_static_pagination"],
        ),
        "supplemental_feeds": _to_bool(options.get("supplemental_feeds"), defaults["supplemental_feeds"]),
        "supplemental_sitemaps": _to_bool(options.get("supplemental_sitemaps"), defaults["supplemental_sitemaps"]),
        "supplemental_cache_enabled": _to_bool(
            options.get("supplemental_cache_enabled"),
            defaults["supplemental_cache_enabled"],
        ),
        "supplemental_cache_ttl_seconds": _to_int(
            options.get("supplemental_cache_ttl_seconds"),
            defaults["supplemental_cache_ttl_seconds"],
            60,
            86400,
        ),
        "supplemental_retry_attempts": _to_int(
            options.get("supplemental_retry_attempts"),
            defaults["supplemental_retry_attempts"],
            1,
            8,
        ),
        "supplemental_max_per_source": _to_int(
            options.get("supplemental_max_per_source"),
            defaults["supplemental_max_per_source"],
            100,
            5000,
        ),
        "supplemental_max_sitemaps": _to_int(
            options.get("supplemental_max_sitemaps"),
            defaults["supplemental_max_sitemaps"],
            1,
            200,
        ),
        "supplemental_max_static_pages": _to_int(
            options.get("supplemental_max_static_pages"),
            defaults["supplemental_max_static_pages"],
            1,
            100,
        ),
        "max_articles_per_task": _to_int(
            options.get("max_articles_per_task", options.get("max_detail_articles")),
            defaults["max_articles_per_task"],
            1,
            1000,
        ),
        "browserforge_enabled": _to_bool(options.get("browserforge_enabled"), defaults["browserforge_enabled"]),
        "proxy_enabled": _to_bool(options.get("proxy_enabled", options.get("use_proxy")), defaults["proxy_enabled"]),
    }

    # When date range priority is on, a UI/page limit is only a safety display
    # value and must not truncate candidates before detail-page date checks.
    normalized["respect_limit_with_date_range"] = not normalized["date_range_priority"]
    return normalized


def public_runtime_config() -> Dict[str, Any]:
    defaults = default_crawl_options()
    return {
        "success": True,
        "defaults": defaults,
        "proxy": {
            "configured": proxy_is_configured(),
            "enabled": bool(getattr(config, "PROXY_ENABLED", False)),
            "playwright_configured": bool(getattr(config, "PLAYWRIGHT_PROXY", None) or getattr(config, "PROXY_HTTP", None)),
            "requests_configured": bool(
                getattr(config, "PROXY_HTTP", None)
                or getattr(config, "PROXY_HTTPS", None)
                or getattr(config, "PROXY_SOCKS5", None)
            ),
        },
    }

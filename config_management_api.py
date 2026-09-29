#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Configuration management API for customer-facing deployment settings."""

from __future__ import annotations

import os
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable

from flask import Blueprint, current_app, jsonify, request

import config
from decorators import admin_required
from financial_config import financial_product_capabilities
from financial_health import FinancialHealthService
from financial_instruments import InstrumentRegistry
from financial_provider_router import provider_policy_summary
from financial_providers.tushare_cn import TushareCNProvider
from financial_schema import ensure_financial_tables
from platform_sources import parse_updates, public_sources
from agent_reach_providers import platform_test
from financial_rollout import (
    ROLLOUT_STAGE_INDEX,
    normalize_rollout_stage,
    rollout_transition_decision,
    utc_text,
)
from industry_packs import industry_pack_loader
from logger_utils import log_error, log_info


config_management_bp = Blueprint(
    'config_management',
    __name__,
    url_prefix='/api/config-management',
)

ENV_PATH = Path('.env')

TRUE_VALUES = {'1', 'true', 'yes', 'on'}


def _open_database_connection(read_only: bool = False, path: str | None = None):
    from db_connection import connect_database
    return connect_database(read_only=read_only, path=path)

BOOL_KEYS = {
    'FLASK_DEBUG',
    'PROXY_ENABLED',
    'RAGFLOW_UPLOAD_ENABLED',
    'RAGFLOW_AUTO_PARSE',
    'RAGFLOW_REUPLOAD_EXISTING',
    'RAGFLOW_TTS_ENABLED',
    'RAGFLOW_PROXY_ENABLED',
    'CRAWL_DATE_RANGE_PRIORITY',
    'CRAWL_PREFILTER_CANDIDATE_DATES',
    'CRAWL_NETWORK_JSON_ENABLED',
    'CRAWL_SUPPLEMENTAL_ENABLED',
    'CRAWL_SUPPLEMENTAL_HTML_ENABLED',
    'CRAWL_SUPPLEMENTAL_ATTRIBUTES_ENABLED',
    'CRAWL_SUPPLEMENTAL_STRUCTURED_ENABLED',
    'CRAWL_SUPPLEMENTAL_SCRIPTS_ENABLED',
    'CRAWL_SUPPLEMENTAL_STATIC_PAGINATION_ENABLED',
    'CRAWL_SUPPLEMENTAL_FEEDS_ENABLED',
    'CRAWL_SUPPLEMENTAL_SITEMAPS_ENABLED',
    'CRAWL_USE_PROXY_DEFAULT',
    'SERPAPI_ENABLED',
    'FINANCIAL_INTELLIGENCE_ENABLED',
    'FINANCIAL_INFORMATION_NEEDS_ENABLED',
    'FINANCIAL_INSTRUMENT_DISCOVERY_ENABLED',
    'FINANCIAL_EXTERNAL_INSTRUMENT_DISCOVERY_ENABLED',
    'FINANCIAL_INSTRUMENT_SEARCH_FALLBACK_ENABLED',
    'FINANCIAL_INSTRUMENT_AUTO_PROMOTION_ENABLED',
    'FINANCIAL_LATEST_NEWS_ENABLED',
    'FINANCIAL_LATEST_BUNDLE_ENABLED',
    'TRADING_AGENTS_ENABLED',
    'FINANCIAL_AUTO_RESEARCH_ENABLED',
    'TRADING_SIMULATION_ENABLED',
    'AKSHARE_CN_ENABLED',
    'TUSHARE_CN_ENABLED',
    'YAHOO_FINANCE_ENABLED',
    'ALPHA_VANTAGE_ENABLED',
    'ALPHA_VANTAGE_REALTIME_ENTITLED',
    'FRED_ENABLED',
    'POLYMARKET_ENABLED',
    'EASYQUOTATION_ENABLED',
    'OFFICIAL_FINANCIAL_EVIDENCE_ENABLED',
}

CHOICE_VALUES = {
    'ALPHA_VANTAGE_QUOTE_ENTITLEMENT': {'none', 'delayed', 'realtime'},
    'REMOTE_PIPELINE_TTS_LANGUAGE': {'zh', 'en'},
    'REMOTE_PIPELINE_TTS_DIALECT': {'mandarin', 'cantonese'},
    'REMOTE_PIPELINE_TTS_GENDER': {'male', 'female'},
}

INT_LIMITS = {
    'FLASK_PORT': (1, 65535),
    'SESSION_LIFETIME': (300, 2_592_000),
    'AUTH_CHECK_INTERVAL': (60, 86_400),
    'REDIS_PORT': (1, 65535),
    'REDIS_DB': (0, 15),
    'CRAWL_TIMEOUT': (5, 600),
    'CRAWL_WAIT_TIME': (0, 120),
    'CRAWL_RENDER_WAIT_MS': (1000, 60000),
    'CRAWL_PLAYWRIGHT_CLICK_TIMEOUT_MS': (250, 5000),
    'CRAWL_LINK_DISCOVERY_MAX_PAGES': (1, 1000),
    'CRAWL_LINK_DISCOVERY_MAX_PAGES_WITH_LIMIT': (1, 1000),
    'CRAWL_PLAYWRIGHT_MAX_EMPTY_PAGES': (1, 50),
    'CRAWL_DETAIL_MAX_RETRIES': (1, 5),
    'CRAWL_SUPPLEMENTAL_MAX_PER_SOURCE': (100, 5000),
    'CRAWL_SUPPLEMENTAL_MAX_SITEMAPS': (1, 200),
    'CRAWL_SUPPLEMENTAL_MAX_STATIC_PAGES': (1, 100),
    'CRAWL_SCHEDULER_MAX_CONCURRENT': (1, 32),
    'CRAWL_SCHEDULER_MAX_PER_DOMAIN': (1, 8),
    'CRAWL_SCHEDULER_RETRIES': (0, 5),
    'CRAWL_SCHEDULER_RETRY_BACKOFF': (0, 600),
    'CRAWL_SCHEDULER_DOMAIN_COOLDOWN': (0, 300),
    'CRAWL_SCHEDULER_TASK_TIMEOUT': (300, 86400),
    'CRAWL_SCHEDULER_COMPLETED_RETENTION': (60, 86400),
    'RAGFLOW_TIMEOUT': (5, 600),
    'RAGFLOW_UPLOAD_RETRIES': (0, 5),
    'RAGFLOW_TTS_TIMEOUT': (5, 300),
    'RAGFLOW_TTS_MAX_CHARS': (100, 1000),
    'RAGFLOW_TTS_PREBUFFER_SENTENCES': (1, 10),
    'RAGFLOW_TTS_CACHE_DAYS': (1, 365),
    'RAGFLOW_TTS_CACHE_MAX_MB': (64, 10240),
    'SERPAPI_TIMEOUT_SECONDS': (5, 120),
    'SERPAPI_MAX_RETRIES': (0, 5),
    'SERPAPI_MAX_QUERIES_PER_RUN': (1, 100),
    'SERPAPI_DAILY_QUERY_BUDGET': (0, 100000),
    'SERPAPI_RECENCY_DAYS': (1, 31),
    # 搜索引擎统一入口（SerpAPI / Tavily）
    'SEARCH_RUNS_PER_DAY': (0, 6),
    'SEARCH_PACKS_PER_RUN': (1, 9),
    'SEARCH_KEYWORDS_PER_PACK': (1, 10),
    'TAVILY_MAX_RESULTS': (1, 20),
    'TAVILY_TIMEOUT_SECONDS': (5, 120),
    'TAVILY_MAX_RETRIES': (0, 5),
    'TAVILY_MAX_CALLS_PER_RUN': (1, 50),
    'TAVILY_MAX_CALLS_PER_MONTH': (1, 10000),
    'SERPAPI_MAX_CALLS_PER_RUN': (1, 50),
    'SERPAPI_MAX_CALLS_PER_MONTH': (1, 10000),
    'FINANCIAL_PROVIDER_TIMEOUT_SECONDS': (5, 120),
    'FINANCIAL_PROVIDER_MAX_RETRIES': (0, 5),
    'FINANCIAL_PROVIDER_MAX_CONCURRENCY': (1, 16),
    'FINANCIAL_PROVIDER_DAILY_CALL_BUDGET': (0, 100000),
    'FINANCIAL_QUOTE_FRESHNESS_SECONDS': (15, 3600),
    'FINANCIAL_LATEST_QUOTE_TIMEOUT_SECONDS': (1, 120),
    'FINANCIAL_LATEST_NEWS_TIMEOUT_SECONDS': (1, 120),
    'FINANCIAL_LATEST_BUNDLE_TIMEOUT_SECONDS': (1, 180),
    'FINANCIAL_NEWS_LOOKBACK_DAYS': (1, 30),
    'FINANCIAL_MARKET_BREADTH_FRESHNESS_SECONDS': (60, 7200),
    'FINANCIAL_NEWS_FRESHNESS_SECONDS': (60, 86400),
    'FINANCIAL_FUNDAMENTAL_FRESHNESS_SECONDS': (3600, 31536000),
    'FINANCIAL_RESEARCH_MAX_LLM_CALLS': (1, 200),
    'FINANCIAL_RESEARCH_MAX_TOKENS': (1000, 1000000),
    'FINANCIAL_RESEARCH_MAX_DEBATE_ROUNDS': (1, 10),
    'FINANCIAL_RESEARCH_TIMEOUT_SECONDS': (60, 7200),
    'FINANCIAL_RESEARCH_CACHE_SECONDS': (60, 86400),
    'FINANCIAL_AUTO_RESEARCH_DAILY_BUDGET': (0, 100),
    'FINANCIAL_ROLLOUT_OBSERVATION_SECONDS': (60, 604800),
}

SECRET_KEYS = {
    'SECRET_KEY',
    'DEFAULT_ADMIN_PASSWORD',
    'REDIS_PASSWORD',
    'RAGFLOW_API_KEY',
    'SERPAPI_API_KEY',
    'TUSHARE_TOKEN',
    'ALPHA_VANTAGE_API_KEY',
    'FRED_API_KEY',
}

RESTART_KEYS = {
    'FLASK_HOST',
    'FLASK_PORT',
    'FLASK_DEBUG',
    'SECRET_KEY',
    'DATABASE_PATH',
    'CRAWL_RESULTS_DIR',
    'AUTH_STORAGE_DIR',
    'REDIS_HOST',
    'REDIS_PORT',
    'REDIS_DB',
    'REDIS_PASSWORD',
    'CRAWL_SCHEDULER_MAX_CONCURRENT',
    'SERPAPI_API_KEY',
    'SERPAPI_ENABLED',
    'SERPAPI_ENGINE',
    'SERPAPI_DEFAULT_REGION',
    'SERPAPI_DEFAULT_LANGUAGE',
    'SERPAPI_TIMEOUT_SECONDS',
    'SERPAPI_MAX_RETRIES',
    'SERPAPI_MAX_QUERIES_PER_RUN',
    'SERPAPI_DAILY_QUERY_BUDGET',
    'SERPAPI_RECENCY_DAYS',
    'SERPAPI_RESULT_LANGUAGE',
    # Tavily（密钥同样只返回"是否已配置"，不回显明文）
    'TAVILY_API_KEY',
    'TAVILY_ENABLED',
    'TAVILY_BASE_URL',
    'TAVILY_MAX_RESULTS',
    'TAVILY_SEARCH_DEPTH',
    'TAVILY_TIMEOUT_SECONDS',
    'TAVILY_MAX_RETRIES',
    'TAVILY_QUERY_MODE',
    'TAVILY_MAX_CALLS_PER_RUN',
    'TAVILY_MAX_CALLS_PER_MONTH',
    # 搜索引擎调度与配额
    'SEARCH_ENABLED',
    'SEARCH_PROVIDER',
    'SEARCH_RUNS_PER_DAY',
    'SEARCH_RUN_HOURS',
    'SEARCH_PACKS_PER_RUN',
    'SEARCH_KEYWORDS_PER_PACK',
    'SERPAPI_QUERY_MODE',
    'SERPAPI_MAX_CALLS_PER_RUN',
    'SERPAPI_MAX_CALLS_PER_MONTH',
    'FINANCIAL_NEWS_REFRESH_ENABLED',
    'FINANCIAL_INTELLIGENCE_ENABLED',
    'FINANCIAL_INFORMATION_NEEDS_ENABLED',
    'FINANCIAL_INSTRUMENT_DISCOVERY_ENABLED',
    'FINANCIAL_INSTRUMENT_AUTO_PROMOTION_ENABLED',
    'FINANCIAL_LATEST_NEWS_ENABLED',
    'FINANCIAL_LATEST_BUNDLE_ENABLED',
    'TRADING_AGENTS_ENABLED',
    'FINANCIAL_AUTO_RESEARCH_ENABLED',
    'TRADING_SIMULATION_ENABLED',
    'AKSHARE_CN_ENABLED',
    'TUSHARE_CN_ENABLED',
    'TUSHARE_TOKEN',
    'YAHOO_FINANCE_ENABLED',
    'ALPHA_VANTAGE_ENABLED',
    'ALPHA_VANTAGE_API_KEY',
    'ALPHA_VANTAGE_QUOTE_ENTITLEMENT',
    'ALPHA_VANTAGE_REALTIME_ENTITLED',
    'FRED_ENABLED',
    'FRED_API_KEY',
    'POLYMARKET_ENABLED',
    'EASYQUOTATION_ENABLED',
    'OFFICIAL_FINANCIAL_EVIDENCE_ENABLED',
    'FINANCIAL_PROVIDER_TIMEOUT_SECONDS',
    'FINANCIAL_PROVIDER_MAX_RETRIES',
    'FINANCIAL_PROVIDER_MAX_CONCURRENCY',
    'FINANCIAL_PROVIDER_DAILY_CALL_BUDGET',
    'FINANCIAL_QUOTE_FRESHNESS_SECONDS',
    'FINANCIAL_LATEST_QUOTE_TIMEOUT_SECONDS',
    'FINANCIAL_LATEST_NEWS_TIMEOUT_SECONDS',
    'FINANCIAL_LATEST_BUNDLE_TIMEOUT_SECONDS',
    'FINANCIAL_NEWS_LOOKBACK_DAYS',
    'FINANCIAL_MARKET_BREADTH_FRESHNESS_SECONDS',
    'FINANCIAL_NEWS_FRESHNESS_SECONDS',
    'FINANCIAL_FUNDAMENTAL_FRESHNESS_SECONDS',
    'FINANCIAL_RESEARCH_MAX_LLM_CALLS',
    'FINANCIAL_RESEARCH_MAX_TOKENS',
    'FINANCIAL_RESEARCH_MAX_DEBATE_ROUNDS',
    'FINANCIAL_RESEARCH_TIMEOUT_SECONDS',
    'FINANCIAL_RESEARCH_CACHE_SECONDS',
    'FINANCIAL_AUTO_RESEARCH_DAILY_BUDGET',
    'FINANCIAL_ROLLOUT_STAGE',
    'FINANCIAL_ROLLOUT_STAGE_CHANGED_AT',
    'FINANCIAL_ROLLOUT_OBSERVATION_SECONDS',
}

MANAGED_KEYS = [
    # System
    'FLASK_HOST',
    'FLASK_PORT',
    'FLASK_DEBUG',
    'SECRET_KEY',
    'SESSION_LIFETIME',
    'AUTH_CHECK_INTERVAL',
    'LOG_LEVEL',
    'LOG_FILE',
    # Login bootstrap
    'DEFAULT_ADMIN_USERNAME',
    'DEFAULT_ADMIN_PASSWORD',
    'DEFAULT_ADMIN_EMAIL',
    'DEFAULT_ADMIN_FULL_NAME',
    # Storage
    'DATABASE_PATH',
    'CRAWL_RESULTS_DIR',
    'AUTH_STORAGE_DIR',
    # Redis
    'REDIS_HOST',
    'REDIS_PORT',
    'REDIS_DB',
    'REDIS_PASSWORD',
    # Scheduler
    'CRAWL_SCHEDULER_MAX_CONCURRENT',
    'CRAWL_SCHEDULER_MAX_PER_DOMAIN',
    'CRAWL_SCHEDULER_RETRIES',
    'CRAWL_SCHEDULER_RETRY_BACKOFF',
    'CRAWL_SCHEDULER_DOMAIN_COOLDOWN',
    'CRAWL_SCHEDULER_TASK_TIMEOUT',
    'CRAWL_SCHEDULER_COMPLETED_RETENTION',
    # Crawl behavior
    'USER_AGENT',
    'CRAWL_TIMEOUT',
    'CRAWL_WAIT_TIME',
    'CRAWL_RENDER_WAIT_MS',
    'CRAWL_PLAYWRIGHT_CLICK_TIMEOUT_MS',
    'CRAWL_LINK_DISCOVERY_MAX_PAGES',
    'CRAWL_LINK_DISCOVERY_MAX_PAGES_WITH_LIMIT',
    'CRAWL_PLAYWRIGHT_MAX_EMPTY_PAGES',
    'CRAWL_DETAIL_MAX_RETRIES',
    'CRAWL_DATE_RANGE_PRIORITY',
    'CRAWL_PREFILTER_CANDIDATE_DATES',
    'CRAWL_NETWORK_JSON_ENABLED',
    'CRAWL_SUPPLEMENTAL_ENABLED',
    'CRAWL_SUPPLEMENTAL_HTML_ENABLED',
    'CRAWL_SUPPLEMENTAL_ATTRIBUTES_ENABLED',
    'CRAWL_SUPPLEMENTAL_STRUCTURED_ENABLED',
    'CRAWL_SUPPLEMENTAL_SCRIPTS_ENABLED',
    'CRAWL_SUPPLEMENTAL_STATIC_PAGINATION_ENABLED',
    'CRAWL_SUPPLEMENTAL_FEEDS_ENABLED',
    'CRAWL_SUPPLEMENTAL_SITEMAPS_ENABLED',
    'CRAWL_SUPPLEMENTAL_MAX_PER_SOURCE',
    'CRAWL_SUPPLEMENTAL_MAX_SITEMAPS',
    'CRAWL_SUPPLEMENTAL_MAX_STATIC_PAGES',
    'CRAWL_USE_PROXY_DEFAULT',
    # Proxy
    'PROXY_ENABLED',
    'PROXY_HTTP',
    'PROXY_HTTPS',
    'PROXY_SOCKS5',
    'PLAYWRIGHT_PROXY',
    # RAGFlow
    'RAGFLOW_BASE_URL',
    'RAGFLOW_API_KEY',
    'RAGFLOW_UPLOAD_ENABLED',
    'RAGFLOW_AUTO_PARSE',
    'RAGFLOW_REUPLOAD_EXISTING',
    'RAGFLOW_TIMEOUT',
    'RAGFLOW_UPLOAD_RETRIES',
    'RAGFLOW_TTS_ENABLED',
    'RAGFLOW_TTS_TIMEOUT',
    'RAGFLOW_TTS_MAX_CHARS',
    'RAGFLOW_TTS_MODEL',
    'RAGFLOW_TTS_VOICE_ZH',
    'RAGFLOW_TTS_VOICE_EN',
    'RAGFLOW_TTS_LANGUAGE_MODE',
    'RAGFLOW_TTS_PREBUFFER_SENTENCES',
    'RAGFLOW_TTS_CACHE_DAYS',
    'RAGFLOW_TTS_CACHE_MAX_MB',
    'RAGFLOW_TTS_VOICE_CATALOG',
    'REMOTE_PIPELINE_URL',
    'REMOTE_PIPELINE_TOKEN',
    'REMOTE_PIPELINE_TTS_LANGUAGE',
    'REMOTE_PIPELINE_TTS_DIALECT',
    'REMOTE_PIPELINE_TTS_GENDER',
    'REMOTE_PIPELINE_TTS_VOICE',
    # 资讯平台独立 CosyVoice3 配置；仅作为每次朗读请求的参数传递。
    'INTEL_TTS_ENGINE',
    'INTEL_TTS_VOICE_PROFILE',
    'INTEL_TTS_DIALECT',
    'INTEL_TTS_GENDER',
    'INTEL_TTS_EMOTION',
    'INTEL_TTS_SPEED',
    'INTEL_TTS_BUFFER_MS',
    'INTEL_TTS_SEGMENT_MAX_CHARS_ZH',
    'INTEL_TTS_SEGMENT_MAX_WORDS_EN',
    'RAGFLOW_PROXY_ENABLED',
    'RAGFLOW_PROXY_HTTP',
    'RAGFLOW_PROXY_HTTPS',
    'RAGFLOW_PROXY_SOCKS5',
    # Financial/TradingAgents. These values are applied to this Web process
    # immediately, while intel-worker must restart to observe the new env.
    'FINANCIAL_INTELLIGENCE_ENABLED',
    'FINANCIAL_INFORMATION_NEEDS_ENABLED',
    'FINANCIAL_INSTRUMENT_DISCOVERY_ENABLED',
    'FINANCIAL_INSTRUMENT_AUTO_PROMOTION_ENABLED',
    'FINANCIAL_LATEST_NEWS_ENABLED',
    'FINANCIAL_LATEST_BUNDLE_ENABLED',
    'TRADING_AGENTS_ENABLED',
    'FINANCIAL_AUTO_RESEARCH_ENABLED',
    'TRADING_SIMULATION_ENABLED',
    'FINANCIAL_ROLLOUT_STAGE',
    'FINANCIAL_ROLLOUT_STAGE_CHANGED_AT',
    'FINANCIAL_ROLLOUT_OBSERVATION_SECONDS',
    'AKSHARE_CN_ENABLED',
    'TUSHARE_CN_ENABLED',
    'TUSHARE_TOKEN',
    'YAHOO_FINANCE_ENABLED',
    'ALPHA_VANTAGE_ENABLED',
    'ALPHA_VANTAGE_API_KEY',
    'ALPHA_VANTAGE_QUOTE_ENTITLEMENT',
    'ALPHA_VANTAGE_REALTIME_ENTITLED',
    'FRED_ENABLED',
    'FRED_API_KEY',
    'POLYMARKET_ENABLED',
    'EASYQUOTATION_ENABLED',
    'OFFICIAL_FINANCIAL_EVIDENCE_ENABLED',
    'FINANCIAL_PROVIDER_TIMEOUT_SECONDS',
    'FINANCIAL_PROVIDER_MAX_RETRIES',
    'FINANCIAL_PROVIDER_MAX_CONCURRENCY',
    'FINANCIAL_PROVIDER_DAILY_CALL_BUDGET',
    'FINANCIAL_QUOTE_FRESHNESS_SECONDS',
    'FINANCIAL_LATEST_QUOTE_TIMEOUT_SECONDS',
    'FINANCIAL_LATEST_NEWS_TIMEOUT_SECONDS',
    'FINANCIAL_LATEST_BUNDLE_TIMEOUT_SECONDS',
    'FINANCIAL_NEWS_LOOKBACK_DAYS',
    'FINANCIAL_MARKET_BREADTH_FRESHNESS_SECONDS',
    'FINANCIAL_NEWS_FRESHNESS_SECONDS',
    'FINANCIAL_FUNDAMENTAL_FRESHNESS_SECONDS',
    'FINANCIAL_RESEARCH_MAX_LLM_CALLS',
    'FINANCIAL_RESEARCH_MAX_TOKENS',
    'FINANCIAL_RESEARCH_MAX_DEBATE_ROUNDS',
    'FINANCIAL_RESEARCH_TIMEOUT_SECONDS',
    'FINANCIAL_RESEARCH_CACHE_SECONDS',
    'FINANCIAL_AUTO_RESEARCH_DAILY_BUDGET',
]

DEFAULTS = {
    'FLASK_HOST': '0.0.0.0',
    'FLASK_PORT': '8003',
    'FLASK_DEBUG': 'false',
    'SESSION_LIFETIME': '86400',
    'AUTH_CHECK_INTERVAL': '3600',
    'LOG_LEVEL': 'INFO',
    'LOG_FILE': 'app.log',
    'DEFAULT_ADMIN_USERNAME': 'admin',
    'DEFAULT_ADMIN_EMAIL': 'admin@example.com',
    'DEFAULT_ADMIN_FULL_NAME': '系统管理员',
    'DATABASE_PATH': 'crawler_articles.db',
    'CRAWL_RESULTS_DIR': 'crawl_results',
    'AUTH_STORAGE_DIR': 'auth_storage',
    'REDIS_HOST': 'localhost',
    'REDIS_PORT': '6379',
    'REDIS_DB': '1',
    'USER_AGENT': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    ),
    'CRAWL_TIMEOUT': '30',
    'CRAWL_WAIT_TIME': '3',
    'CRAWL_RENDER_WAIT_MS': '8000',
    'CRAWL_PLAYWRIGHT_CLICK_TIMEOUT_MS': '1200',
    'CRAWL_LINK_DISCOVERY_MAX_PAGES': '30',
    'CRAWL_LINK_DISCOVERY_MAX_PAGES_WITH_LIMIT': '10',
    'CRAWL_PLAYWRIGHT_MAX_EMPTY_PAGES': '5',
    'CRAWL_DETAIL_MAX_RETRIES': '2',
    'CRAWL_DATE_RANGE_PRIORITY': 'true',
    'CRAWL_PREFILTER_CANDIDATE_DATES': 'true',
    'CRAWL_NETWORK_JSON_ENABLED': 'true',
    'CRAWL_SUPPLEMENTAL_ENABLED': 'true',
    'CRAWL_SUPPLEMENTAL_HTML_ENABLED': 'true',
    'CRAWL_SUPPLEMENTAL_ATTRIBUTES_ENABLED': 'true',
    'CRAWL_SUPPLEMENTAL_STRUCTURED_ENABLED': 'true',
    'CRAWL_SUPPLEMENTAL_SCRIPTS_ENABLED': 'true',
    'CRAWL_SUPPLEMENTAL_STATIC_PAGINATION_ENABLED': 'true',
    'CRAWL_SUPPLEMENTAL_FEEDS_ENABLED': 'true',
    'CRAWL_SUPPLEMENTAL_SITEMAPS_ENABLED': 'true',
    'CRAWL_SUPPLEMENTAL_MAX_PER_SOURCE': '500',
    'CRAWL_SUPPLEMENTAL_MAX_SITEMAPS': '25',
    'CRAWL_SUPPLEMENTAL_MAX_STATIC_PAGES': '8',
    'CRAWL_USE_PROXY_DEFAULT': '',
    'CRAWL_SCHEDULER_MAX_CONCURRENT': '4',
    'CRAWL_SCHEDULER_MAX_PER_DOMAIN': '1',
    'CRAWL_SCHEDULER_RETRIES': '2',
    'CRAWL_SCHEDULER_RETRY_BACKOFF': '20',
    'CRAWL_SCHEDULER_DOMAIN_COOLDOWN': '5',
    'CRAWL_SCHEDULER_TASK_TIMEOUT': '7200',
    'CRAWL_SCHEDULER_COMPLETED_RETENTION': '3600',
    'PROXY_ENABLED': 'false',
    'RAGFLOW_UPLOAD_ENABLED': 'false',
    'RAGFLOW_AUTO_PARSE': 'true',
    'RAGFLOW_REUPLOAD_EXISTING': 'true',
    'RAGFLOW_TIMEOUT': '45',
    'RAGFLOW_UPLOAD_RETRIES': '1',
    'RAGFLOW_PROXY_ENABLED': 'false',
    'REMOTE_PIPELINE_URL': 'http://10.88.0.1:11236',
    'REMOTE_PIPELINE_TOKEN': '',
    'REMOTE_PIPELINE_TTS_LANGUAGE': 'zh',
    'REMOTE_PIPELINE_TTS_DIALECT': 'mandarin',
    'REMOTE_PIPELINE_TTS_GENDER': 'female',
    'REMOTE_PIPELINE_TTS_VOICE': 'default',
    'SERPAPI_ENABLED': 'false',
    'SERPAPI_ENGINE': 'google',
    'SERPAPI_DEFAULT_REGION': 'hk',
    'SERPAPI_DEFAULT_LANGUAGE': 'zh-cn',
    'SERPAPI_TIMEOUT_SECONDS': '20',
    'SERPAPI_MAX_RETRIES': '2',
    'SERPAPI_MAX_QUERIES_PER_RUN': '6',
    'SERPAPI_DAILY_QUERY_BUDGET': '100',
    'SERPAPI_RECENCY_DAYS': '3',
    'SERPAPI_RESULT_LANGUAGE': 'zh',
    # ── 搜索引擎统一入口（SerpAPI / Tavily）──────────────────────────
    # 定位：搜索只发现 URL；正文走现有「候选门禁 → 抓取 → 分类 → 主题归属」链路
    'SEARCH_ENABLED': 'false',
    'SEARCH_PROVIDER': 'serpapi',
    'SEARCH_RUNS_PER_DAY': '1',
    'SEARCH_RUN_HOURS': '08:20',
    'SEARCH_PACKS_PER_RUN': '2',
    'SEARCH_KEYWORDS_PER_PACK': '3',
    'TAVILY_ENABLED': 'false',
    'TAVILY_API_KEY': '',
    'TAVILY_BASE_URL': 'https://api.tavily.com/search',
    'TAVILY_MAX_RESULTS': '5',
    'TAVILY_SEARCH_DEPTH': 'basic',
    'TAVILY_TIMEOUT_SECONDS': '20',
    'TAVILY_MAX_RETRIES': '1',
    'TAVILY_QUERY_MODE': 'separate',
    'TAVILY_MAX_CALLS_PER_RUN': '6',
    'TAVILY_MAX_CALLS_PER_MONTH': '180',
    'SERPAPI_QUERY_MODE': 'merged',
    'SERPAPI_MAX_CALLS_PER_RUN': '2',
    'SERPAPI_MAX_CALLS_PER_MONTH': '60',
    'FINANCIAL_NEWS_REFRESH_ENABLED': 'false',
    'FINANCIAL_INTELLIGENCE_ENABLED': 'false',
    'FINANCIAL_INFORMATION_NEEDS_ENABLED': 'true',
    'FINANCIAL_INSTRUMENT_DISCOVERY_ENABLED': 'true',
    'FINANCIAL_EXTERNAL_INSTRUMENT_DISCOVERY_ENABLED': 'true',
    'FINANCIAL_INSTRUMENT_SEARCH_FALLBACK_ENABLED': 'true',
    'FINANCIAL_INSTRUMENT_AUTO_PROMOTION_ENABLED': 'true',
    'FINANCIAL_LATEST_NEWS_ENABLED': 'true',
    'FINANCIAL_LATEST_BUNDLE_ENABLED': 'true',
    'TRADING_AGENTS_ENABLED': 'false',
    'FINANCIAL_AUTO_RESEARCH_ENABLED': 'false',
    'TRADING_SIMULATION_ENABLED': 'false',
    'FINANCIAL_ROLLOUT_STAGE': 'off',
    'FINANCIAL_ROLLOUT_STAGE_CHANGED_AT': '',
    'FINANCIAL_ROLLOUT_OBSERVATION_SECONDS': '3600',
    'AKSHARE_CN_ENABLED': 'false',
    'TUSHARE_CN_ENABLED': 'false',
    'YAHOO_FINANCE_ENABLED': 'false',
    'ALPHA_VANTAGE_ENABLED': 'false',
    'ALPHA_VANTAGE_QUOTE_ENTITLEMENT': 'none',
    'ALPHA_VANTAGE_REALTIME_ENTITLED': 'false',
    'FRED_ENABLED': 'false',
    'POLYMARKET_ENABLED': 'false',
    'EASYQUOTATION_ENABLED': 'false',
    'OFFICIAL_FINANCIAL_EVIDENCE_ENABLED': 'false',
    'FINANCIAL_PROVIDER_TIMEOUT_SECONDS': '20',
    'FINANCIAL_PROVIDER_MAX_RETRIES': '2',
    'FINANCIAL_PROVIDER_MAX_CONCURRENCY': '4',
    'FINANCIAL_PROVIDER_DAILY_CALL_BUDGET': '1000',
    'FINANCIAL_QUOTE_FRESHNESS_SECONDS': '300',
    'FINANCIAL_LATEST_QUOTE_TIMEOUT_SECONDS': '6',
    'FINANCIAL_LATEST_NEWS_TIMEOUT_SECONDS': '40',
    'FINANCIAL_LATEST_BUNDLE_TIMEOUT_SECONDS': '45',
    'FINANCIAL_NEWS_LOOKBACK_DAYS': '7',
    'FINANCIAL_MARKET_BREADTH_FRESHNESS_SECONDS': '300',
    'FINANCIAL_NEWS_FRESHNESS_SECONDS': '3600',
    'FINANCIAL_FUNDAMENTAL_FRESHNESS_SECONDS': '86400',
    'FINANCIAL_RESEARCH_MAX_LLM_CALLS': '30',
    'FINANCIAL_RESEARCH_MAX_TOKENS': '120000',
    'FINANCIAL_RESEARCH_MAX_DEBATE_ROUNDS': '2',
    'FINANCIAL_RESEARCH_TIMEOUT_SECONDS': '1800',
    'FINANCIAL_RESEARCH_CACHE_SECONDS': '3600',
    'FINANCIAL_AUTO_RESEARCH_DAILY_BUDGET': '6',
}


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text == '':
        return default
    return text in TRUE_VALUES


def _clean_value(value: Any) -> str:
    value = '' if value is None else str(value).strip()
    if '\n' in value or '\r' in value:
        raise ValueError('配置值不能包含换行')
    return value


def _to_int_string(key: str, value: Any) -> str:
    min_value, max_value = INT_LIMITS[key]
    try:
        parsed = int(float(_clean_value(value)))
    except (TypeError, ValueError):
        parsed = int(DEFAULTS.get(key, min_value))
    return str(max(min_value, min(max_value, parsed)))


def _strict_int_string(key: str, value: Any) -> str:
    """Reject invalid financial limits instead of silently changing intent."""
    min_value, max_value = INT_LIMITS[key]
    if isinstance(value, bool):
        raise ValueError(f'{key} 必须是 {min_value} 至 {max_value} 之间的整数')
    text = _clean_value(value)
    if not re.fullmatch(r'-?\d+', text):
        raise ValueError(f'{key} 必须是 {min_value} 至 {max_value} 之间的整数')
    parsed = int(text)
    if not min_value <= parsed <= max_value:
        raise ValueError(f'{key} 必须是 {min_value} 至 {max_value} 之间的整数')
    return str(parsed)


def _normalize_env_value(key: str, value: Any) -> str:
    if key == 'FINANCIAL_ROLLOUT_STAGE':
        return normalize_rollout_stage(_clean_value(value), strict=True)
    if key in CHOICE_VALUES:
        normalized = _clean_value(value).casefold()
        if normalized not in CHOICE_VALUES[key]:
            allowed = ', '.join(sorted(CHOICE_VALUES[key]))
            raise ValueError(f'{key} 必须是以下值之一: {allowed}')
        return normalized
    if key in BOOL_KEYS:
        return 'true' if _to_bool(value, _to_bool(DEFAULTS.get(key), False)) else 'false'
    if key in INT_LIMITS:
        return _to_int_string(key, value)
    value = _clean_value(value)
    if key in {'RAGFLOW_BASE_URL'}:
        return value.rstrip('/')
    return value


def _read_env_values() -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    with ENV_PATH.open('r', encoding='utf-8') as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            if line.startswith('export '):
                line = line[7:].strip()
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _value(values: Dict[str, str], key: str) -> str:
    if key in values:
        return values.get(key) or ''
    if key in DEFAULTS:
        return DEFAULTS[key]
    runtime = getattr(config, key, '')
    return '' if runtime is None else str(runtime)


def _choice_value(values: Dict[str, str], key: str) -> str:
    value = str(_value(values, key)).strip().casefold()
    return value if value in CHOICE_VALUES[key] else DEFAULTS[key]


def _managed_rollout_stage(values: Dict[str, str]) -> str:
    if 'FINANCIAL_ROLLOUT_STAGE' in values:
        return normalize_rollout_stage(
            values.get('FINANCIAL_ROLLOUT_STAGE'), strict=False
        )
    legacy_flags_present = any(
        key in values
        for key in (
            'FINANCIAL_INTELLIGENCE_ENABLED',
            'TRADING_AGENTS_ENABLED',
            'FINANCIAL_AUTO_RESEARCH_ENABLED',
            'TRADING_SIMULATION_ENABLED',
        )
    )
    return 'simulation_backtest' if legacy_flags_present else 'off'


def _int_value(values: Dict[str, str], key: str) -> int:
    return int(_normalize_env_value(key, _value(values, key)))


def _bool_value(values: Dict[str, str], key: str, default: bool = False) -> bool:
    return _to_bool(_value(values, key), default)


def _format_env_line(key: str, value: str) -> str:
    return f'{key}={value}'


def _write_env_values(updates: Dict[str, str]) -> None:
    existing_lines = []
    if ENV_PATH.exists():
        existing_lines = ENV_PATH.read_text(encoding='utf-8').splitlines()

    pending = dict(updates)
    written_keys = set()
    output = []
    for raw_line in existing_lines:
        stripped = raw_line.strip()
        candidate = stripped[7:].strip() if stripped.startswith('export ') else stripped
        if candidate and not candidate.startswith('#') and '=' in candidate:
            key = candidate.split('=', 1)[0].strip()
            if key in updates and key in written_keys:
                continue
            if key in pending:
                output.append(_format_env_line(key, pending.pop(key)))
                written_keys.add(key)
                continue
        output.append(raw_line)

    if pending:
        if output and output[-1].strip():
            output.append('')
        output.append('# ==================== 配置管理页面维护 ====================')
        for key in MANAGED_KEYS:
            if key in pending:
                output.append(_format_env_line(key, pending.pop(key)))
        for key, value in pending.items():
            output.append(_format_env_line(key, value))

    ENV_PATH.write_text('\n'.join(output).rstrip() + '\n', encoding='utf-8')


def _changed_keys(current_values: Dict[str, str], updates: Dict[str, str]) -> set:
    changed = set()
    for key, new_value in updates.items():
        if (current_values.get(key) or '') != (new_value or ''):
            changed.add(key)
    return changed


def _runtime_env_or_config(key: str, default: Any = None) -> Any:
    """Preserve the imported runtime value when a partial update omits a key."""

    if key in os.environ:
        return os.environ.get(key)
    return getattr(config, key, default)


def _apply_runtime_values(updates: Dict[str, str]) -> None:
    for key, value in updates.items():
        os.environ[key] = value

    config.FLASK_HOST = os.environ.get('FLASK_HOST', '0.0.0.0')
    config.FLASK_PORT = int(_normalize_env_value('FLASK_PORT', os.environ.get('FLASK_PORT') or 8003))
    config.FLASK_DEBUG = _to_bool(os.environ.get('FLASK_DEBUG'), False)
    config.SECRET_KEY = os.environ.get('SECRET_KEY') or None
    config.PERMANENT_SESSION_LIFETIME = int(
        _normalize_env_value('SESSION_LIFETIME', os.environ.get('SESSION_LIFETIME') or 86400)
    )
    config.AUTH_CHECK_INTERVAL = int(
        _normalize_env_value('AUTH_CHECK_INTERVAL', os.environ.get('AUTH_CHECK_INTERVAL') or 3600)
    )
    current_app.config['PERMANENT_SESSION_LIFETIME'] = config.PERMANENT_SESSION_LIFETIME

    config.REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
    config.REDIS_PORT = int(_normalize_env_value('REDIS_PORT', os.environ.get('REDIS_PORT') or 6379))
    config.REDIS_DB = int(_normalize_env_value('REDIS_DB', os.environ.get('REDIS_DB') or 1))
    config.REDIS_PASSWORD = os.environ.get('REDIS_PASSWORD') or None

    config.DATABASE_PATH = os.environ.get('DATABASE_PATH', 'crawler_articles.db')
    config.CRAWL_RESULTS_DIR = os.environ.get('CRAWL_RESULTS_DIR', 'crawl_results')
    config.AUTH_STORAGE_DIR = os.environ.get('AUTH_STORAGE_DIR', 'auth_storage')
    config.SCREENSHOT_DIR = os.path.join(config.AUTH_STORAGE_DIR, 'screenshots')
    config.DEFAULT_USER_AGENT = os.environ.get('USER_AGENT') or DEFAULTS['USER_AGENT']
    config.DEFAULT_TIMEOUT = int(_normalize_env_value('CRAWL_TIMEOUT', os.environ.get('CRAWL_TIMEOUT') or 30))
    config.DEFAULT_WAIT_TIME = int(_normalize_env_value('CRAWL_WAIT_TIME', os.environ.get('CRAWL_WAIT_TIME') or 3))

    config.PROXY_ENABLED = _to_bool(os.environ.get('PROXY_ENABLED'), False)
    config.PROXY_HTTP = os.environ.get('PROXY_HTTP') or None
    config.PROXY_HTTPS = os.environ.get('PROXY_HTTPS') or None
    config.PROXY_SOCKS5 = os.environ.get('PROXY_SOCKS5') or None
    config.PLAYWRIGHT_PROXY = os.environ.get('PLAYWRIGHT_PROXY') or None

    config.RAGFLOW_BASE_URL = (os.environ.get('RAGFLOW_BASE_URL') or '').rstrip('/')
    config.RAGFLOW_API_KEY = os.environ.get('RAGFLOW_API_KEY') or ''
    config.RAGFLOW_UPLOAD_ENABLED = _to_bool(
        os.environ.get('RAGFLOW_UPLOAD_ENABLED'),
        bool(config.RAGFLOW_BASE_URL and config.RAGFLOW_API_KEY),
    )
    config.RAGFLOW_AUTO_PARSE = _to_bool(os.environ.get('RAGFLOW_AUTO_PARSE'), True)
    config.RAGFLOW_REUPLOAD_EXISTING = _to_bool(os.environ.get('RAGFLOW_REUPLOAD_EXISTING'), True)
    config.RAGFLOW_TIMEOUT = int(_normalize_env_value('RAGFLOW_TIMEOUT', os.environ.get('RAGFLOW_TIMEOUT') or 45))
    config.RAGFLOW_UPLOAD_RETRIES = int(
        _normalize_env_value('RAGFLOW_UPLOAD_RETRIES', os.environ.get('RAGFLOW_UPLOAD_RETRIES') or 1)
    )
    config.RAGFLOW_TTS_ENABLED = _to_bool(
        os.environ.get('RAGFLOW_TTS_ENABLED'),
        bool(config.RAGFLOW_BASE_URL and config.RAGFLOW_API_KEY),
    )
    config.RAGFLOW_TTS_TIMEOUT = int(_normalize_env_value('RAGFLOW_TTS_TIMEOUT', os.environ.get('RAGFLOW_TTS_TIMEOUT') or 60))
    config.RAGFLOW_TTS_MAX_CHARS = int(_normalize_env_value('RAGFLOW_TTS_MAX_CHARS', os.environ.get('RAGFLOW_TTS_MAX_CHARS') or 500))
    config.RAGFLOW_TTS_MODEL = os.environ.get('RAGFLOW_TTS_MODEL') or ''
    config.RAGFLOW_TTS_VOICE_ZH = os.environ.get('RAGFLOW_TTS_VOICE_ZH') or ''
    config.RAGFLOW_TTS_VOICE_EN = os.environ.get('RAGFLOW_TTS_VOICE_EN') or ''
    config.RAGFLOW_TTS_LANGUAGE_MODE = (os.environ.get('RAGFLOW_TTS_LANGUAGE_MODE') or 'auto').casefold()
    if config.RAGFLOW_TTS_LANGUAGE_MODE not in {'auto', 'zh', 'en'}:
        config.RAGFLOW_TTS_LANGUAGE_MODE = 'auto'
    config.RAGFLOW_TTS_PREBUFFER_SENTENCES = int(_normalize_env_value('RAGFLOW_TTS_PREBUFFER_SENTENCES', os.environ.get('RAGFLOW_TTS_PREBUFFER_SENTENCES') or 3))
    config.RAGFLOW_TTS_CACHE_DAYS = int(_normalize_env_value('RAGFLOW_TTS_CACHE_DAYS', os.environ.get('RAGFLOW_TTS_CACHE_DAYS') or 30))
    config.RAGFLOW_TTS_CACHE_MAX_MB = int(_normalize_env_value('RAGFLOW_TTS_CACHE_MAX_MB', os.environ.get('RAGFLOW_TTS_CACHE_MAX_MB') or 1024))
    config.INTEL_TTS_ENGINE = os.environ.get('INTEL_TTS_ENGINE') or 'CosyVoice3'
    config.INTEL_TTS_VOICE_PROFILE = os.environ.get('INTEL_TTS_VOICE_PROFILE') or 'male_mandarin_01'
    config.INTEL_TTS_DIALECT = os.environ.get('INTEL_TTS_DIALECT') or 'mandarin'
    config.INTEL_TTS_GENDER = os.environ.get('INTEL_TTS_GENDER') or 'male'
    config.INTEL_TTS_EMOTION = os.environ.get('INTEL_TTS_EMOTION') or 'lively'
    try:
        config.INTEL_TTS_SPEED = max(0.5, min(2.0, float(os.environ.get('INTEL_TTS_SPEED') or 1.15)))
    except (TypeError, ValueError):
        config.INTEL_TTS_SPEED = 1.15
    config.INTEL_TTS_BUFFER_MS = max(400, min(5000, int(os.environ.get('INTEL_TTS_BUFFER_MS') or 1400)))
    config.INTEL_TTS_SEGMENT_MAX_CHARS_ZH = max(20, min(200, int(os.environ.get('INTEL_TTS_SEGMENT_MAX_CHARS_ZH') or 50)))
    config.INTEL_TTS_SEGMENT_MAX_WORDS_EN = max(8, min(100, int(os.environ.get('INTEL_TTS_SEGMENT_MAX_WORDS_EN') or 20)))
    raw_voice_catalog = os.environ.get('RAGFLOW_TTS_VOICE_CATALOG') or '[]'
    try:
        config.RAGFLOW_TTS_VOICE_CATALOG = json.loads(raw_voice_catalog)
    except (TypeError, ValueError):
        config.RAGFLOW_TTS_VOICE_CATALOG = []
    if not isinstance(config.RAGFLOW_TTS_VOICE_CATALOG, list):
        config.RAGFLOW_TTS_VOICE_CATALOG = []
    config.RAGFLOW_PROXY_ENABLED = _to_bool(os.environ.get('RAGFLOW_PROXY_ENABLED'), False)
    config.RAGFLOW_PROXY_HTTP = os.environ.get('RAGFLOW_PROXY_HTTP') or None
    config.RAGFLOW_PROXY_HTTPS = os.environ.get('RAGFLOW_PROXY_HTTPS') or None
    config.RAGFLOW_PROXY_SOCKS5 = os.environ.get('RAGFLOW_PROXY_SOCKS5') or None
    config.REMOTE_PIPELINE_TTS_LANGUAGE = (
        os.environ.get('REMOTE_PIPELINE_TTS_LANGUAGE') or DEFAULTS['REMOTE_PIPELINE_TTS_LANGUAGE']
    ).strip().casefold()
    config.REMOTE_PIPELINE_TTS_DIALECT = (
        os.environ.get('REMOTE_PIPELINE_TTS_DIALECT') or DEFAULTS['REMOTE_PIPELINE_TTS_DIALECT']
    ).strip().casefold()
    config.REMOTE_PIPELINE_TTS_GENDER = (
        os.environ.get('REMOTE_PIPELINE_TTS_GENDER') or DEFAULTS['REMOTE_PIPELINE_TTS_GENDER']
    ).strip().casefold()
    if 'REMOTE_PIPELINE_TTS_VOICE' in updates and not (
        'REMOTE_PIPELINE_TTS_LANGUAGE' in updates
        or 'REMOTE_PIPELINE_TTS_DIALECT' in updates
        or 'REMOTE_PIPELINE_TTS_GENDER' in updates
    ):
        config.REMOTE_PIPELINE_TTS_VOICE = updates['REMOTE_PIPELINE_TTS_VOICE']
    else:
        config.REMOTE_PIPELINE_TTS_VOICE = config.resolve_remote_tts_voice(
            config.REMOTE_PIPELINE_TTS_LANGUAGE,
            config.REMOTE_PIPELINE_TTS_DIALECT,
            config.REMOTE_PIPELINE_TTS_GENDER,
        )
    config.REMOTE_PIPELINE_URL = (
        os.environ.get('REMOTE_PIPELINE_URL') or DEFAULTS.get('REMOTE_PIPELINE_URL', 'http://10.88.0.1:11236')
    ).rstrip('/')
    config.REMOTE_PIPELINE_TOKEN = os.environ.get('REMOTE_PIPELINE_TOKEN') or ''
    config.SERPAPI_API_KEY = os.environ.get('SERPAPI_API_KEY') or ''
    config.SERPAPI_ENABLED = _to_bool(os.environ.get('SERPAPI_ENABLED'), False)
    config.SERPAPI_ENGINE = os.environ.get('SERPAPI_ENGINE') or 'google'
    config.SERPAPI_DEFAULT_REGION = os.environ.get('SERPAPI_DEFAULT_REGION') or 'hk'
    config.SERPAPI_DEFAULT_LANGUAGE = os.environ.get('SERPAPI_DEFAULT_LANGUAGE') or 'zh-cn'
    config.SERPAPI_TIMEOUT_SECONDS = int(
        _normalize_env_value('SERPAPI_TIMEOUT_SECONDS', os.environ.get('SERPAPI_TIMEOUT_SECONDS') or 20)
    )
    config.SERPAPI_MAX_RETRIES = int(
        _normalize_env_value('SERPAPI_MAX_RETRIES', os.environ.get('SERPAPI_MAX_RETRIES') or 2)
    )
    config.SERPAPI_MAX_QUERIES_PER_RUN = int(
        _normalize_env_value('SERPAPI_MAX_QUERIES_PER_RUN', os.environ.get('SERPAPI_MAX_QUERIES_PER_RUN') or 6)
    )
    config.SERPAPI_DAILY_QUERY_BUDGET = int(
        _normalize_env_value('SERPAPI_DAILY_QUERY_BUDGET', os.environ.get('SERPAPI_DAILY_QUERY_BUDGET') or 100)
    )
    config.SERPAPI_RECENCY_DAYS = int(
        _normalize_env_value('SERPAPI_RECENCY_DAYS', os.environ.get('SERPAPI_RECENCY_DAYS') or 3)
    )
    config.SERPAPI_RESULT_LANGUAGE = (os.environ.get('SERPAPI_RESULT_LANGUAGE') or 'zh').casefold()
    # ── 搜索引擎统一入口（SerpAPI / Tavily）──
    # 搜索只发现 URL；正文走现有「候选门禁 → 抓取 → 分类 → 主题归属」链路。
    config.SEARCH_ENABLED = _to_bool(os.environ.get('SEARCH_ENABLED'), False)
    config.SEARCH_PROVIDER = os.environ.get('SEARCH_PROVIDER') or 'serpapi'
    config.SEARCH_RUNS_PER_DAY = int(_normalize_env_value('SEARCH_RUNS_PER_DAY', os.environ.get('SEARCH_RUNS_PER_DAY') or 1))
    config.SEARCH_RUN_HOURS = os.environ.get('SEARCH_RUN_HOURS') or '08:20'
    config.SEARCH_PACKS_PER_RUN = int(_normalize_env_value('SEARCH_PACKS_PER_RUN', os.environ.get('SEARCH_PACKS_PER_RUN') or 2))
    config.SEARCH_KEYWORDS_PER_PACK = int(_normalize_env_value('SEARCH_KEYWORDS_PER_PACK', os.environ.get('SEARCH_KEYWORDS_PER_PACK') or 3))
    config.SERPAPI_QUERY_MODE = (os.environ.get('SERPAPI_QUERY_MODE') or 'merged').casefold()
    config.SERPAPI_MAX_CALLS_PER_RUN = int(_normalize_env_value('SERPAPI_MAX_CALLS_PER_RUN', os.environ.get('SERPAPI_MAX_CALLS_PER_RUN') or 2))
    config.SERPAPI_MAX_CALLS_PER_MONTH = int(_normalize_env_value('SERPAPI_MAX_CALLS_PER_MONTH', os.environ.get('SERPAPI_MAX_CALLS_PER_MONTH') or 60))
    config.TAVILY_API_KEY = os.environ.get('TAVILY_API_KEY') or ''
    config.TAVILY_ENABLED = _to_bool(os.environ.get('TAVILY_ENABLED'), False)
    config.TAVILY_BASE_URL = os.environ.get('TAVILY_BASE_URL') or 'https://api.tavily.com/search'
    config.TAVILY_MAX_RESULTS = int(_normalize_env_value('TAVILY_MAX_RESULTS', os.environ.get('TAVILY_MAX_RESULTS') or 5))
    config.TAVILY_SEARCH_DEPTH = (os.environ.get('TAVILY_SEARCH_DEPTH') or 'basic').casefold()
    config.TAVILY_TIMEOUT_SECONDS = int(_normalize_env_value('TAVILY_TIMEOUT_SECONDS', os.environ.get('TAVILY_TIMEOUT_SECONDS') or 20))
    config.TAVILY_MAX_RETRIES = int(_normalize_env_value('TAVILY_MAX_RETRIES', os.environ.get('TAVILY_MAX_RETRIES') or 1))
    config.TAVILY_QUERY_MODE = (os.environ.get('TAVILY_QUERY_MODE') or 'separate').casefold()
    config.TAVILY_MAX_CALLS_PER_RUN = int(_normalize_env_value('TAVILY_MAX_CALLS_PER_RUN', os.environ.get('TAVILY_MAX_CALLS_PER_RUN') or 6))
    config.TAVILY_MAX_CALLS_PER_MONTH = int(_normalize_env_value('TAVILY_MAX_CALLS_PER_MONTH', os.environ.get('TAVILY_MAX_CALLS_PER_MONTH') or 180))
    config.FINANCIAL_NEWS_REFRESH_ENABLED = _to_bool(os.environ.get('FINANCIAL_NEWS_REFRESH_ENABLED'), False)

    config.FINANCIAL_INTELLIGENCE_ENABLED = _to_bool(
        os.environ.get('FINANCIAL_INTELLIGENCE_ENABLED'), False
    )
    config.FINANCIAL_INFORMATION_NEEDS_ENABLED = _to_bool(
        os.environ.get('FINANCIAL_INFORMATION_NEEDS_ENABLED'), True
    )
    config.FINANCIAL_INSTRUMENT_DISCOVERY_ENABLED = _to_bool(
        os.environ.get('FINANCIAL_INSTRUMENT_DISCOVERY_ENABLED'), True
    )
    config.FINANCIAL_EXTERNAL_INSTRUMENT_DISCOVERY_ENABLED = _to_bool(
        os.environ.get('FINANCIAL_EXTERNAL_INSTRUMENT_DISCOVERY_ENABLED'), True
    )
    config.FINANCIAL_INSTRUMENT_SEARCH_FALLBACK_ENABLED = _to_bool(
        os.environ.get('FINANCIAL_INSTRUMENT_SEARCH_FALLBACK_ENABLED'), True
    )
    config.FINANCIAL_INSTRUMENT_AUTO_PROMOTION_ENABLED = _to_bool(
        os.environ.get('FINANCIAL_INSTRUMENT_AUTO_PROMOTION_ENABLED'), True
    )
    config.FINANCIAL_LATEST_NEWS_ENABLED = _to_bool(
        os.environ.get('FINANCIAL_LATEST_NEWS_ENABLED'), True
    )
    config.FINANCIAL_LATEST_BUNDLE_ENABLED = _to_bool(
        os.environ.get('FINANCIAL_LATEST_BUNDLE_ENABLED'), True
    )
    config.TRADING_AGENTS_ENABLED = _to_bool(
        os.environ.get('TRADING_AGENTS_ENABLED'), False
    )
    config.FINANCIAL_AUTO_RESEARCH_ENABLED = _to_bool(
        os.environ.get('FINANCIAL_AUTO_RESEARCH_ENABLED'), False
    )
    config.TRADING_SIMULATION_ENABLED = _to_bool(
        os.environ.get('TRADING_SIMULATION_ENABLED'), False
    )
    config.FINANCIAL_ROLLOUT_STAGE = normalize_rollout_stage(
        _runtime_env_or_config(
            'FINANCIAL_ROLLOUT_STAGE', 'simulation_backtest'
        )
        or 'simulation_backtest',
        strict=True,
    )
    config.FINANCIAL_ROLLOUT_STAGE_CHANGED_AT = (
        _runtime_env_or_config('FINANCIAL_ROLLOUT_STAGE_CHANGED_AT', '') or ''
    ).strip()
    config.FINANCIAL_ROLLOUT_OBSERVATION_SECONDS = int(
        _normalize_env_value(
            'FINANCIAL_ROLLOUT_OBSERVATION_SECONDS',
            _runtime_env_or_config(
                'FINANCIAL_ROLLOUT_OBSERVATION_SECONDS', 3600
            )
            or 3600,
        )
    )
    config.AKSHARE_CN_ENABLED = _to_bool(
        os.environ.get('AKSHARE_CN_ENABLED'), False
    )
    config.TUSHARE_CN_ENABLED = _to_bool(
        os.environ.get('TUSHARE_CN_ENABLED'), False
    )
    config.TUSHARE_TOKEN = os.environ.get('TUSHARE_TOKEN') or ''
    config.YAHOO_FINANCE_ENABLED = _to_bool(
        os.environ.get('YAHOO_FINANCE_ENABLED'), False
    )
    config.ALPHA_VANTAGE_ENABLED = _to_bool(
        os.environ.get('ALPHA_VANTAGE_ENABLED'), False
    )
    config.ALPHA_VANTAGE_API_KEY = os.environ.get('ALPHA_VANTAGE_API_KEY') or ''
    quote_entitlement = (
        os.environ.get('ALPHA_VANTAGE_QUOTE_ENTITLEMENT') or 'none'
    ).strip().casefold()
    config.ALPHA_VANTAGE_QUOTE_ENTITLEMENT = (
        quote_entitlement
        if quote_entitlement in CHOICE_VALUES['ALPHA_VANTAGE_QUOTE_ENTITLEMENT']
        else 'none'
    )
    config.ALPHA_VANTAGE_REALTIME_ENTITLED = _to_bool(
        os.environ.get('ALPHA_VANTAGE_REALTIME_ENTITLED'), False
    )
    config.FRED_ENABLED = _to_bool(os.environ.get('FRED_ENABLED'), False)
    config.FRED_API_KEY = os.environ.get('FRED_API_KEY') or ''
    config.POLYMARKET_ENABLED = _to_bool(
        os.environ.get('POLYMARKET_ENABLED'), False
    )
    config.EASYQUOTATION_ENABLED = _to_bool(
        os.environ.get('EASYQUOTATION_ENABLED'), False
    )
    config.OFFICIAL_FINANCIAL_EVIDENCE_ENABLED = _to_bool(
        os.environ.get('OFFICIAL_FINANCIAL_EVIDENCE_ENABLED'), False
    )
    for key in (
        'FINANCIAL_PROVIDER_TIMEOUT_SECONDS',
        'FINANCIAL_PROVIDER_MAX_RETRIES',
        'FINANCIAL_PROVIDER_MAX_CONCURRENCY',
        'FINANCIAL_PROVIDER_DAILY_CALL_BUDGET',
        'FINANCIAL_QUOTE_FRESHNESS_SECONDS',
        'FINANCIAL_LATEST_QUOTE_TIMEOUT_SECONDS',
        'FINANCIAL_LATEST_NEWS_TIMEOUT_SECONDS',
        'FINANCIAL_LATEST_BUNDLE_TIMEOUT_SECONDS',
        'FINANCIAL_NEWS_LOOKBACK_DAYS',
        'FINANCIAL_MARKET_BREADTH_FRESHNESS_SECONDS',
        'FINANCIAL_NEWS_FRESHNESS_SECONDS',
        'FINANCIAL_FUNDAMENTAL_FRESHNESS_SECONDS',
        'FINANCIAL_RESEARCH_MAX_LLM_CALLS',
        'FINANCIAL_RESEARCH_MAX_TOKENS',
        'FINANCIAL_RESEARCH_MAX_DEBATE_ROUNDS',
        'FINANCIAL_RESEARCH_TIMEOUT_SECONDS',
        'FINANCIAL_RESEARCH_CACHE_SECONDS',
        'FINANCIAL_AUTO_RESEARCH_DAILY_BUDGET',
    ):
        setattr(config, key, int(_normalize_env_value(key, os.environ.get(key))))
    config.LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')
    config.LOG_FILE = os.environ.get('LOG_FILE', 'app.log')

    config.ensure_directories()

    try:
        import ragflow_client

        ragflow_client._client_instance = None
    except Exception:
        pass

    try:
        from scheduler import scheduler

        scheduler.max_concurrent_tasks = int(os.environ.get('CRAWL_SCHEDULER_MAX_CONCURRENT', '4'))
        scheduler.max_tasks_per_domain = int(os.environ.get('CRAWL_SCHEDULER_MAX_PER_DOMAIN', '1'))
        scheduler.retry_attempts = int(os.environ.get('CRAWL_SCHEDULER_RETRIES', '2'))
        scheduler.retry_backoff_seconds = int(os.environ.get('CRAWL_SCHEDULER_RETRY_BACKOFF', '20'))
        scheduler.domain_cooldown_seconds = int(os.environ.get('CRAWL_SCHEDULER_DOMAIN_COOLDOWN', '5'))
        scheduler.task_timeout_seconds = int(os.environ.get('CRAWL_SCHEDULER_TASK_TIMEOUT', '7200'))
        scheduler.completed_task_retention_seconds = int(os.environ.get('CRAWL_SCHEDULER_COMPLETED_RETENTION', '3600'))
    except Exception:
        pass


def _secret_status(values: Dict[str, str], key: str) -> bool:
    return bool(values.get(key) or os.environ.get(key) or getattr(config, key, ''))


def _tushare_public_status(
    values: Dict[str, str], financial_state: Dict[str, Any]
) -> Dict[str, Any]:
    """Return only persisted probe states; never return or fingerprint the token."""
    capability_names = (
        'instrument_master', 'daily_market', 'realtime_equity',
        'realtime_etf', 'realtime_index', 'index_constituents',
        'fund', 'financials', 'announcements',
    )
    token_configured = bool(financial_state.get('tushare_token_configured'))
    if not token_configured:
        return {
            'availability': 'not_configured',
            'token_status': 'not_configured',
            'checked_at': None,
            'capabilities': {name: 'not_checked' for name in capability_names},
        }
    if not financial_state.get('effective', {}).get('tushare_cn'):
        return {
            'availability': 'disabled',
            'token_status': 'configured_unverified',
            'checked_at': None,
            'capabilities': {name: 'not_checked' for name in capability_names},
        }
    fallback = {
        'availability': 'configured_unverified',
        'token_status': 'configured_unverified',
        'checked_at': None,
        'capabilities': {name: 'not_checked' for name in capability_names},
    }
    database_path = str(
        values.get('DATABASE_PATH') or getattr(config, 'DATABASE_PATH', '') or ''
    ).strip()
    if not database_path:
        return fallback
    allowed_availability = {
        'available', 'partial', 'no_permissions', 'invalid_token',
        'unavailable', 'configured_unverified', 'disabled', 'not_configured',
    }
    allowed_token = {'valid', 'invalid', 'configured_unverified', 'not_configured'}
    allowed_capability = {
        'available', 'no_permission', 'rate_limited', 'temporarily_unavailable',
        'invalid_token', 'not_checked', 'not_checked_invalid_token',
    }
    try:
        path = Path(database_path).expanduser().resolve()
        connection = _open_database_connection(read_only=True, path=str(path))
        try:
            connection.execute('PRAGMA query_only=ON')
            row = connection.execute(
                "SELECT metadata_json FROM financial_provider_profiles "
                "WHERE provider_key='tushare_cn'"
            ).fetchone()
        finally:
            connection.close()
        if not row:
            return fallback
        metadata = json.loads(row[0])
        probe = metadata.get('permission_probe') or {}
        availability = str(probe.get('overall') or '')
        token_status = str(probe.get('token_status') or '')
        if availability not in allowed_availability or token_status not in allowed_token:
            return fallback
        raw_capabilities = probe.get('capabilities') or {}
        capabilities = {
            name: (
                str(raw_capabilities.get(name))
                if str(raw_capabilities.get(name)) in allowed_capability
                else 'not_checked'
            )
            for name in capability_names
        }
        checked_at = probe.get('checked_at')
        return {
            'availability': availability,
            'token_status': token_status,
            'checked_at': str(checked_at) if checked_at else None,
            'capabilities': capabilities,
        }
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _invalidate_tushare_probe_after_token_change(values: Dict[str, str]) -> None:
    """Prevent an old token's entitlement result from being shown for a new token."""
    database_path = str(values.get('DATABASE_PATH') or '').strip()
    if not database_path:
        return
    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        return
    connection = _open_database_connection(path=str(path))
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='financial_provider_profiles'"
        ).fetchone()
        if not table:
            return
        row = connection.execute(
            "SELECT metadata_json FROM financial_provider_profiles "
            "WHERE provider_key='tushare_cn'"
        ).fetchone()
        if not row:
            return
        try:
            metadata = json.loads(row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        token_configured = bool(str(values.get('TUSHARE_TOKEN') or '').strip())
        overall = 'configured_unverified' if token_configured else 'not_configured'
        token_status = overall
        metadata['token_configured'] = token_configured
        metadata['permission_probe'] = {
            'probe_version': 'tushare-permissions-v1',
            'checked_at': None,
            'overall': overall,
            'token_status': token_status,
            'capabilities': {},
        }
        connection.execute(
            "UPDATE financial_provider_profiles SET metadata_json=?, "
            "health_status=?, last_health_check_at=NULL, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE provider_key='tushare_cn'",
            (
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                'unknown' if token_configured else 'not_configured',
            ),
        )
    finally:
        connection.close()


def _platform_public_config() -> dict:
    """返回当前平台品牌设置（公司名称 + Logo）。"""
    try:
        from platform_settings import get_platform_settings
        settings = get_platform_settings()
    except Exception:
        settings = {'name': '资讯情报系统', 'logo_url': ''}
    return {
        'name': settings.get('name') or '资讯情报系统',
        'logo_url': settings.get('logo_url') or '',
        'logo_configured': bool(settings.get('logo_url')),
    }


def _public_config(values: Dict[str, str]) -> Dict[str, Any]:
    # SerpAPI 搜索词/金融能力应跟随“当前生效的行业包”，而不是写死的默认包。
    # 老库可能没有 active_industry_pack_id 设置，则回退到 INTEL_DEFAULT_INDUSTRY_PACK。
    try:
        from sqlite_database import sqlite_db
        from project_keyword_gate import configured_project_keyword_snapshot

        sqlite_db._ensure_connection()
        active_snapshot = configured_project_keyword_snapshot(sqlite_db.connection)
        default_pack_id = str(
            active_snapshot.get("industry_pack_id")
            or getattr(config, 'INTEL_DEFAULT_INDUSTRY_PACK', 'family_office')
        )
    except Exception:
        default_pack_id = getattr(config, 'INTEL_DEFAULT_INDUSTRY_PACK', 'family_office')
    try:
        serpapi_queries = list(
            industry_pack_loader.load(default_pack_id).get('serpapi_queries') or []
        )
    except Exception:
        serpapi_queries = []
    financial_setting_keys = (
                'FINANCIAL_INTELLIGENCE_ENABLED',
                'TRADING_AGENTS_ENABLED',
                'FINANCIAL_AUTO_RESEARCH_ENABLED',
                'TRADING_SIMULATION_ENABLED',
                'FINANCIAL_ROLLOUT_STAGE',
                'FINANCIAL_ROLLOUT_STAGE_CHANGED_AT',
                'FINANCIAL_ROLLOUT_OBSERVATION_SECONDS',
                'AKSHARE_CN_ENABLED',
                'TUSHARE_CN_ENABLED',
                'TUSHARE_TOKEN',
                'YAHOO_FINANCE_ENABLED',
                'ALPHA_VANTAGE_ENABLED',
                'ALPHA_VANTAGE_API_KEY',
                'ALPHA_VANTAGE_QUOTE_ENTITLEMENT',
                'ALPHA_VANTAGE_REALTIME_ENTITLED',
                'FRED_ENABLED',
                'FRED_API_KEY',
                'POLYMARKET_ENABLED',
                'EASYQUOTATION_ENABLED',
                'OFFICIAL_FINANCIAL_EVIDENCE_ENABLED',
    )
    financial_settings = {
        key: _value(values, key) for key in financial_setting_keys
    }
    financial_settings['FINANCIAL_ROLLOUT_STAGE'] = _managed_rollout_stage(values)
    financial_state = financial_product_capabilities(
        default_pack_id,
        settings=financial_settings,
    )
    tushare_status = _tushare_public_status(values, financial_state)
    return {
        'success': True,
        'env_path': str(ENV_PATH.resolve()),
        'restart_keys': sorted(RESTART_KEYS),
        'system': {
            'flask_host': _value(values, 'FLASK_HOST'),
            'flask_port': _int_value(values, 'FLASK_PORT'),
            'flask_debug': _bool_value(values, 'FLASK_DEBUG', False),
            'secret_key_configured': _secret_status(values, 'SECRET_KEY'),
            'session_lifetime': _int_value(values, 'SESSION_LIFETIME'),
            'auth_check_interval': _int_value(values, 'AUTH_CHECK_INTERVAL'),
            'log_level': _value(values, 'LOG_LEVEL'),
            'log_file': _value(values, 'LOG_FILE'),
        },
        'admin': {
            'username': _value(values, 'DEFAULT_ADMIN_USERNAME'),
            'password_configured': _secret_status(values, 'DEFAULT_ADMIN_PASSWORD'),
            'email': _value(values, 'DEFAULT_ADMIN_EMAIL'),
            'full_name': _value(values, 'DEFAULT_ADMIN_FULL_NAME'),
        },
        'storage': {
            'database_type': _value(values, 'DATABASE_TYPE') or 'sqlite',
            'database_path': _value(values, 'DATABASE_PATH'),
            'postgres_host': _value(values, 'POSTGRES_HOST'),
            'postgres_port': _int_value(values, 'POSTGRES_PORT'),
            'postgres_db': _value(values, 'POSTGRES_DB'),
            'crawl_results_dir': _value(values, 'CRAWL_RESULTS_DIR'),
            'auth_storage_dir': _value(values, 'AUTH_STORAGE_DIR'),
        },
        'redis': {
            'host': _value(values, 'REDIS_HOST'),
            'port': _int_value(values, 'REDIS_PORT'),
            'db': _int_value(values, 'REDIS_DB'),
            'password_configured': _secret_status(values, 'REDIS_PASSWORD'),
        },
        'scheduler': {
            'max_concurrent': _int_value(values, 'CRAWL_SCHEDULER_MAX_CONCURRENT'),
            'max_per_domain': _int_value(values, 'CRAWL_SCHEDULER_MAX_PER_DOMAIN'),
            'retries': _int_value(values, 'CRAWL_SCHEDULER_RETRIES'),
            'retry_backoff': _int_value(values, 'CRAWL_SCHEDULER_RETRY_BACKOFF'),
            'domain_cooldown': _int_value(values, 'CRAWL_SCHEDULER_DOMAIN_COOLDOWN'),
            'task_timeout': _int_value(values, 'CRAWL_SCHEDULER_TASK_TIMEOUT'),
            'completed_retention': _int_value(values, 'CRAWL_SCHEDULER_COMPLETED_RETENTION'),
        },
        'crawl': {
            'user_agent': _value(values, 'USER_AGENT'),
            'timeout': _int_value(values, 'CRAWL_TIMEOUT'),
            'wait_time': _int_value(values, 'CRAWL_WAIT_TIME'),
            'render_wait_ms': _int_value(values, 'CRAWL_RENDER_WAIT_MS'),
            'click_timeout_ms': _int_value(values, 'CRAWL_PLAYWRIGHT_CLICK_TIMEOUT_MS'),
            'max_pages': _int_value(values, 'CRAWL_LINK_DISCOVERY_MAX_PAGES'),
            'max_pages_with_limit': _int_value(values, 'CRAWL_LINK_DISCOVERY_MAX_PAGES_WITH_LIMIT'),
            'max_empty_pages': _int_value(values, 'CRAWL_PLAYWRIGHT_MAX_EMPTY_PAGES'),
            'detail_max_retries': _int_value(values, 'CRAWL_DETAIL_MAX_RETRIES'),
            'date_range_priority': _bool_value(values, 'CRAWL_DATE_RANGE_PRIORITY', True),
            'candidate_date_prefilter': _bool_value(values, 'CRAWL_PREFILTER_CANDIDATE_DATES', True),
            'network_json_enabled': _bool_value(values, 'CRAWL_NETWORK_JSON_ENABLED', True),
            'supplemental_enabled': _bool_value(values, 'CRAWL_SUPPLEMENTAL_ENABLED', True),
            'supplemental_html': _bool_value(values, 'CRAWL_SUPPLEMENTAL_HTML_ENABLED', True),
            'supplemental_attributes': _bool_value(values, 'CRAWL_SUPPLEMENTAL_ATTRIBUTES_ENABLED', True),
            'supplemental_structured': _bool_value(values, 'CRAWL_SUPPLEMENTAL_STRUCTURED_ENABLED', True),
            'supplemental_scripts': _bool_value(values, 'CRAWL_SUPPLEMENTAL_SCRIPTS_ENABLED', True),
            'supplemental_static_pagination': _bool_value(values, 'CRAWL_SUPPLEMENTAL_STATIC_PAGINATION_ENABLED', True),
            'supplemental_feeds': _bool_value(values, 'CRAWL_SUPPLEMENTAL_FEEDS_ENABLED', True),
            'supplemental_sitemaps': _bool_value(values, 'CRAWL_SUPPLEMENTAL_SITEMAPS_ENABLED', True),
            'supplemental_max_per_source': _int_value(values, 'CRAWL_SUPPLEMENTAL_MAX_PER_SOURCE'),
            'supplemental_max_sitemaps': _int_value(values, 'CRAWL_SUPPLEMENTAL_MAX_SITEMAPS'),
            'supplemental_max_static_pages': _int_value(values, 'CRAWL_SUPPLEMENTAL_MAX_STATIC_PAGES'),
            'use_proxy_default': _bool_value(values, 'CRAWL_USE_PROXY_DEFAULT', False),
        },
        'proxy': {
            'enabled': _bool_value(values, 'PROXY_ENABLED', getattr(config, 'PROXY_ENABLED', False)),
            'http': _value(values, 'PROXY_HTTP'),
            'https': _value(values, 'PROXY_HTTPS'),
            'socks5': _value(values, 'PROXY_SOCKS5'),
            'playwright': _value(values, 'PLAYWRIGHT_PROXY'),
        },
        'ragflow': {
            'base_url': _value(values, 'RAGFLOW_BASE_URL'),
            'api_key_configured': _secret_status(values, 'RAGFLOW_API_KEY'),
            'upload_enabled': _bool_value(values, 'RAGFLOW_UPLOAD_ENABLED', False),
            'auto_parse': _bool_value(values, 'RAGFLOW_AUTO_PARSE', True),
            'reupload_existing': _bool_value(values, 'RAGFLOW_REUPLOAD_EXISTING', True),
            'timeout': _int_value(values, 'RAGFLOW_TIMEOUT'),
            'upload_retries': _int_value(values, 'RAGFLOW_UPLOAD_RETRIES'),
            'tts_enabled': _bool_value(values, 'RAGFLOW_TTS_ENABLED', False),
            'tts_timeout': _int_value(values, 'RAGFLOW_TTS_TIMEOUT'),
            'tts_max_chars': _int_value(values, 'RAGFLOW_TTS_MAX_CHARS'),
            'tts_model': _value(values, 'RAGFLOW_TTS_MODEL'),
            'tts_voice_zh': _value(values, 'RAGFLOW_TTS_VOICE_ZH'),
            'tts_voice_en': _value(values, 'RAGFLOW_TTS_VOICE_EN'),
            'tts_language_mode': _value(values, 'RAGFLOW_TTS_LANGUAGE_MODE') or 'auto',
            'tts_prebuffer_sentences': _int_value(values, 'RAGFLOW_TTS_PREBUFFER_SENTENCES'),
            'tts_cache_days': _int_value(values, 'RAGFLOW_TTS_CACHE_DAYS'),
            'tts_cache_max_mb': _int_value(values, 'RAGFLOW_TTS_CACHE_MAX_MB'),
            'tts_voice_catalog': _value(values, 'RAGFLOW_TTS_VOICE_CATALOG') or '[]',
            'intel_tts_engine': _value(values, 'INTEL_TTS_ENGINE') or 'CosyVoice3',
            'intel_tts_voice_profile': _value(values, 'INTEL_TTS_VOICE_PROFILE') or 'male_mandarin_01',
            'intel_tts_dialect': _value(values, 'INTEL_TTS_DIALECT') or 'mandarin',
            'intel_tts_gender': _value(values, 'INTEL_TTS_GENDER') or 'male',
            'intel_tts_emotion': _value(values, 'INTEL_TTS_EMOTION') or 'lively',
            'intel_tts_speed': _value(values, 'INTEL_TTS_SPEED') or '1.15',
            'intel_tts_buffer_ms': _value(values, 'INTEL_TTS_BUFFER_MS') or '1400',
            'intel_tts_segment_max_chars_zh': _value(values, 'INTEL_TTS_SEGMENT_MAX_CHARS_ZH') or '50',
            'intel_tts_segment_max_words_en': _value(values, 'INTEL_TTS_SEGMENT_MAX_WORDS_EN') or '20',
            'proxy_enabled': _bool_value(values, 'RAGFLOW_PROXY_ENABLED', False),
            'proxy_http': _value(values, 'RAGFLOW_PROXY_HTTP'),
            'proxy_https': _value(values, 'RAGFLOW_PROXY_HTTPS'),
            'proxy_socks5': _value(values, 'RAGFLOW_PROXY_SOCKS5'),
        },
        'remote_pipeline': {
            'url': _value(values, 'REMOTE_PIPELINE_URL'),
            'tts_language': _value(values, 'REMOTE_PIPELINE_TTS_LANGUAGE') or 'zh',
            'tts_dialect': _value(values, 'REMOTE_PIPELINE_TTS_DIALECT') or 'mandarin',
            'tts_gender': _value(values, 'REMOTE_PIPELINE_TTS_GENDER') or 'female',
            'tts_voice': config.resolve_remote_tts_voice(
                _value(values, 'REMOTE_PIPELINE_TTS_LANGUAGE') or 'zh',
                _value(values, 'REMOTE_PIPELINE_TTS_DIALECT') or 'mandarin',
                _value(values, 'REMOTE_PIPELINE_TTS_GENDER') or 'female',
            ),
        },
        'serpapi': {
            'api_key_configured': _secret_status(values, 'SERPAPI_API_KEY'),
            'enabled': _bool_value(values, 'SERPAPI_ENABLED', False),
            'engine': _value(values, 'SERPAPI_ENGINE'),
            'default_region': _value(values, 'SERPAPI_DEFAULT_REGION'),
            'default_language': _value(values, 'SERPAPI_DEFAULT_LANGUAGE'),
            'timeout_seconds': _int_value(values, 'SERPAPI_TIMEOUT_SECONDS'),
            'max_retries': _int_value(values, 'SERPAPI_MAX_RETRIES'),
            'max_queries_per_run': _int_value(values, 'SERPAPI_MAX_QUERIES_PER_RUN'),
            'daily_query_budget': _int_value(values, 'SERPAPI_DAILY_QUERY_BUDGET'),
            'recency_days': _int_value(values, 'SERPAPI_RECENCY_DAYS'),
            'result_language': _value(values, 'SERPAPI_RESULT_LANGUAGE') or 'zh',
            'industry_pack_id': default_pack_id,
            'queries': serpapi_queries,
        },
        # Tavily：与 serpapi 同构；api_key 只返回"是否已配置"，不回显明文
        'tavily': {
            'api_key_configured': _secret_status(values, 'TAVILY_API_KEY'),
            'enabled': _bool_value(values, 'TAVILY_ENABLED', False),
            'base_url': _value(values, 'TAVILY_BASE_URL') or 'https://api.tavily.com/search',
            'max_results': _int_value(values, 'TAVILY_MAX_RESULTS'),
            'search_depth': _value(values, 'TAVILY_SEARCH_DEPTH') or 'basic',
            'timeout_seconds': _int_value(values, 'TAVILY_TIMEOUT_SECONDS'),
            'max_retries': _int_value(values, 'TAVILY_MAX_RETRIES'),
            'query_mode': _value(values, 'TAVILY_QUERY_MODE') or 'separate',
            'max_calls_per_run': _int_value(values, 'TAVILY_MAX_CALLS_PER_RUN'),
            'max_calls_per_month': _int_value(values, 'TAVILY_MAX_CALLS_PER_MONTH'),
            'industry_pack_id': default_pack_id,
            'queries': serpapi_queries,
        },
        # 搜索引擎调度（全局）：只搜"当前生效行业包"的关键词精选；配额按引擎各自计数
        'search_engine': {
            'enabled': _bool_value(values, 'SEARCH_ENABLED', False),
            'provider': _value(values, 'SEARCH_PROVIDER') or 'serpapi',
            'runs_per_day': _int_value(values, 'SEARCH_RUNS_PER_DAY'),
            'run_hours': _value(values, 'SEARCH_RUN_HOURS') or '08:20',
            'packs_per_run': _int_value(values, 'SEARCH_PACKS_PER_RUN'),
            'keywords_per_pack': _int_value(values, 'SEARCH_KEYWORDS_PER_PACK'),
            'serpapi_query_mode': _value(values, 'SERPAPI_QUERY_MODE') or 'merged',
            'serpapi_max_calls_per_run': _int_value(values, 'SERPAPI_MAX_CALLS_PER_RUN'),
            'serpapi_max_calls_per_month': _int_value(values, 'SERPAPI_MAX_CALLS_PER_MONTH'),
        },
        'platform_sources': public_sources(values),
        'platform': _platform_public_config(),
        'financial': {
            'rollout': financial_state['rollout'],
            'financial_intelligence_enabled': _bool_value(
                values, 'FINANCIAL_INTELLIGENCE_ENABLED', False
            ),
            'information_needs_enabled': _bool_value(
                values, 'FINANCIAL_INFORMATION_NEEDS_ENABLED', True
            ),
            'instrument_discovery_enabled': _bool_value(
                values, 'FINANCIAL_INSTRUMENT_DISCOVERY_ENABLED', True
            ),
            'external_instrument_discovery_enabled': _bool_value(
                values, 'FINANCIAL_EXTERNAL_INSTRUMENT_DISCOVERY_ENABLED', True
            ),
            'instrument_search_fallback_enabled': _bool_value(
                values, 'FINANCIAL_INSTRUMENT_SEARCH_FALLBACK_ENABLED', True
            ),
            'instrument_auto_promotion_enabled': _bool_value(
                values, 'FINANCIAL_INSTRUMENT_AUTO_PROMOTION_ENABLED', True
            ),
            'latest_news_enabled': _bool_value(
                values, 'FINANCIAL_LATEST_NEWS_ENABLED', True
            ),
            'latest_bundle_enabled': _bool_value(
                values, 'FINANCIAL_LATEST_BUNDLE_ENABLED', True
            ),
            'trading_agents_enabled': _bool_value(
                values, 'TRADING_AGENTS_ENABLED', False
            ),
            'auto_research_enabled': _bool_value(
                values, 'FINANCIAL_AUTO_RESEARCH_ENABLED', False
            ),
            'simulation_enabled': _bool_value(
                values, 'TRADING_SIMULATION_ENABLED', False
            ),
            'akshare_cn_enabled': _bool_value(values, 'AKSHARE_CN_ENABLED', False),
            'tushare_cn_enabled': _bool_value(values, 'TUSHARE_CN_ENABLED', False),
            'tushare_token_configured': _secret_status(values, 'TUSHARE_TOKEN'),
            'tushare_status': tushare_status,
            'yahoo_finance_enabled': _bool_value(
                values, 'YAHOO_FINANCE_ENABLED', False
            ),
            'alpha_vantage_enabled': _bool_value(
                values, 'ALPHA_VANTAGE_ENABLED', False
            ),
            'alpha_vantage_api_key_configured': _secret_status(
                values, 'ALPHA_VANTAGE_API_KEY'
            ),
            'alpha_vantage_quote_entitlement': _choice_value(
                values, 'ALPHA_VANTAGE_QUOTE_ENTITLEMENT'
            ),
            'alpha_vantage_realtime_entitled': _bool_value(
                values, 'ALPHA_VANTAGE_REALTIME_ENTITLED', False
            ),
            'fred_enabled': _bool_value(values, 'FRED_ENABLED', False),
            'fred_api_key_configured': _secret_status(values, 'FRED_API_KEY'),
            'polymarket_enabled': _bool_value(values, 'POLYMARKET_ENABLED', False),
            'easyquotation_enabled': _bool_value(
                values, 'EASYQUOTATION_ENABLED', False
            ),
            'official_evidence_enabled': _bool_value(
                values, 'OFFICIAL_FINANCIAL_EVIDENCE_ENABLED', False
            ),
            'provider_policies': provider_policy_summary(),
            'provider_timeout_seconds': _int_value(
                values, 'FINANCIAL_PROVIDER_TIMEOUT_SECONDS'
            ),
            'provider_max_retries': _int_value(
                values, 'FINANCIAL_PROVIDER_MAX_RETRIES'
            ),
            'provider_max_concurrency': _int_value(
                values, 'FINANCIAL_PROVIDER_MAX_CONCURRENCY'
            ),
            'provider_daily_call_budget': _int_value(
                values, 'FINANCIAL_PROVIDER_DAILY_CALL_BUDGET'
            ),
            'quote_freshness_seconds': _int_value(
                values, 'FINANCIAL_QUOTE_FRESHNESS_SECONDS'
            ),
            'latest_quote_timeout_seconds': _int_value(
                values, 'FINANCIAL_LATEST_QUOTE_TIMEOUT_SECONDS'
            ),
            'latest_news_timeout_seconds': _int_value(
                values, 'FINANCIAL_LATEST_NEWS_TIMEOUT_SECONDS'
            ),
            'latest_bundle_timeout_seconds': _int_value(
                values, 'FINANCIAL_LATEST_BUNDLE_TIMEOUT_SECONDS'
            ),
            'news_lookback_days': _int_value(
                values, 'FINANCIAL_NEWS_LOOKBACK_DAYS'
            ),
            'market_breadth_freshness_seconds': _int_value(
                values, 'FINANCIAL_MARKET_BREADTH_FRESHNESS_SECONDS'
            ),
            'news_freshness_seconds': _int_value(
                values, 'FINANCIAL_NEWS_FRESHNESS_SECONDS'
            ),
            'fundamental_freshness_seconds': _int_value(
                values, 'FINANCIAL_FUNDAMENTAL_FRESHNESS_SECONDS'
            ),
            'research_max_llm_calls': _int_value(
                values, 'FINANCIAL_RESEARCH_MAX_LLM_CALLS'
            ),
            'research_max_tokens': _int_value(
                values, 'FINANCIAL_RESEARCH_MAX_TOKENS'
            ),
            'research_max_debate_rounds': _int_value(
                values, 'FINANCIAL_RESEARCH_MAX_DEBATE_ROUNDS'
            ),
            'research_timeout_seconds': _int_value(
                values, 'FINANCIAL_RESEARCH_TIMEOUT_SECONDS'
            ),
            'research_cache_seconds': _int_value(
                values, 'FINANCIAL_RESEARCH_CACHE_SECONDS'
            ),
            'auto_research_daily_budget': _int_value(
                values, 'FINANCIAL_AUTO_RESEARCH_DAILY_BUDGET'
            ),
            'effective_capabilities': financial_state['effective'],
            'capability_reasons': financial_state['reasons'],
            'product_capabilities': financial_state['product'],
            'effective_pack_ids': financial_state['effective_pack_ids'],
            'running_task_policy': financial_state['running_task_policy'],
            'worker_restart_required_after_change': True,
        },
    }


def _secret_update(section: Dict[str, Any], key: str, clear_key: str, value_key: str, current_values: Dict[str, str]) -> Dict[str, str]:
    if clear_key not in section and value_key not in section:
        return {}
    if section.get(clear_key):
        return {key: ''}
    value = _clean_value(section.get(value_key))
    if value:
        return {key: value}
    if key in current_values:
        return {key: current_values.get(key, '')}
    # A deployment may inject secrets through the process/container instead
    # of .env. An empty UI field means "leave it alone", never copy or erase it.
    return {}


def _collect(updates: Dict[str, str], mapping: Iterable[tuple], source: Dict[str, Any]) -> None:
    for env_key, input_key in mapping:
        if input_key in source:
            updates[env_key] = _normalize_env_value(env_key, source.get(input_key))


def _build_updates(data: Dict[str, Any], current_values: Dict[str, str]) -> Dict[str, str]:
    system = data.get('system') if isinstance(data.get('system'), dict) else {}
    admin = data.get('admin') if isinstance(data.get('admin'), dict) else {}
    storage = data.get('storage') if isinstance(data.get('storage'), dict) else {}
    redis_cfg = data.get('redis') if isinstance(data.get('redis'), dict) else {}
    scheduler_cfg = data.get('scheduler') if isinstance(data.get('scheduler'), dict) else {}
    crawl = data.get('crawl') if isinstance(data.get('crawl'), dict) else {}
    proxy = data.get('proxy') if isinstance(data.get('proxy'), dict) else {}
    ragflow = data.get('ragflow') if isinstance(data.get('ragflow'), dict) else {}
    remote_pipeline = data.get('remote_pipeline') if isinstance(data.get('remote_pipeline'), dict) else {}
    serpapi = data.get('serpapi') if isinstance(data.get('serpapi'), dict) else {}
    tavily = data.get('tavily') if isinstance(data.get('tavily'), dict) else {}
    search_engine = data.get('search_engine') if isinstance(data.get('search_engine'), dict) else {}
    financial = data.get('financial') if isinstance(data.get('financial'), dict) else {}
    platform_sources = data.get('platform_sources') if isinstance(data.get('platform_sources'), dict) else {}

    updates: Dict[str, str] = {}
    _collect(updates, [
        ('FLASK_HOST', 'flask_host'),
        ('FLASK_PORT', 'flask_port'),
        ('FLASK_DEBUG', 'flask_debug'),
        ('SESSION_LIFETIME', 'session_lifetime'),
        ('AUTH_CHECK_INTERVAL', 'auth_check_interval'),
        ('LOG_LEVEL', 'log_level'),
        ('LOG_FILE', 'log_file'),
    ], system)
    updates.update(_secret_update(system, 'SECRET_KEY', 'clear_secret_key', 'secret_key', current_values))

    _collect(updates, [
        ('DEFAULT_ADMIN_USERNAME', 'username'),
        ('DEFAULT_ADMIN_EMAIL', 'email'),
        ('DEFAULT_ADMIN_FULL_NAME', 'full_name'),
    ], admin)
    updates.update(_secret_update(admin, 'DEFAULT_ADMIN_PASSWORD', 'clear_password', 'password', current_values))

    _collect(updates, [
        ('DATABASE_PATH', 'database_path'),
        ('CRAWL_RESULTS_DIR', 'crawl_results_dir'),
        ('AUTH_STORAGE_DIR', 'auth_storage_dir'),
    ], storage)

    _collect(updates, [
        ('REDIS_HOST', 'host'),
        ('REDIS_PORT', 'port'),
        ('REDIS_DB', 'db'),
    ], redis_cfg)
    updates.update(_secret_update(redis_cfg, 'REDIS_PASSWORD', 'clear_password', 'password', current_values))

    _collect(updates, [
        ('CRAWL_SCHEDULER_MAX_CONCURRENT', 'max_concurrent'),
        ('CRAWL_SCHEDULER_MAX_PER_DOMAIN', 'max_per_domain'),
        ('CRAWL_SCHEDULER_RETRIES', 'retries'),
        ('CRAWL_SCHEDULER_RETRY_BACKOFF', 'retry_backoff'),
        ('CRAWL_SCHEDULER_DOMAIN_COOLDOWN', 'domain_cooldown'),
        ('CRAWL_SCHEDULER_TASK_TIMEOUT', 'task_timeout'),
        ('CRAWL_SCHEDULER_COMPLETED_RETENTION', 'completed_retention'),
    ], scheduler_cfg)

    _collect(updates, [
        ('USER_AGENT', 'user_agent'),
        ('CRAWL_TIMEOUT', 'timeout'),
        ('CRAWL_WAIT_TIME', 'wait_time'),
        ('CRAWL_RENDER_WAIT_MS', 'render_wait_ms'),
        ('CRAWL_PLAYWRIGHT_CLICK_TIMEOUT_MS', 'click_timeout_ms'),
        ('CRAWL_LINK_DISCOVERY_MAX_PAGES', 'max_pages'),
        ('CRAWL_LINK_DISCOVERY_MAX_PAGES_WITH_LIMIT', 'max_pages_with_limit'),
        ('CRAWL_PLAYWRIGHT_MAX_EMPTY_PAGES', 'max_empty_pages'),
        ('CRAWL_DETAIL_MAX_RETRIES', 'detail_max_retries'),
        ('CRAWL_DATE_RANGE_PRIORITY', 'date_range_priority'),
        ('CRAWL_PREFILTER_CANDIDATE_DATES', 'candidate_date_prefilter'),
        ('CRAWL_NETWORK_JSON_ENABLED', 'network_json_enabled'),
        ('CRAWL_SUPPLEMENTAL_ENABLED', 'supplemental_enabled'),
        ('CRAWL_SUPPLEMENTAL_HTML_ENABLED', 'supplemental_html'),
        ('CRAWL_SUPPLEMENTAL_ATTRIBUTES_ENABLED', 'supplemental_attributes'),
        ('CRAWL_SUPPLEMENTAL_STRUCTURED_ENABLED', 'supplemental_structured'),
        ('CRAWL_SUPPLEMENTAL_SCRIPTS_ENABLED', 'supplemental_scripts'),
        ('CRAWL_SUPPLEMENTAL_STATIC_PAGINATION_ENABLED', 'supplemental_static_pagination'),
        ('CRAWL_SUPPLEMENTAL_FEEDS_ENABLED', 'supplemental_feeds'),
        ('CRAWL_SUPPLEMENTAL_SITEMAPS_ENABLED', 'supplemental_sitemaps'),
        ('CRAWL_SUPPLEMENTAL_MAX_PER_SOURCE', 'supplemental_max_per_source'),
        ('CRAWL_SUPPLEMENTAL_MAX_SITEMAPS', 'supplemental_max_sitemaps'),
        ('CRAWL_SUPPLEMENTAL_MAX_STATIC_PAGES', 'supplemental_max_static_pages'),
        ('CRAWL_USE_PROXY_DEFAULT', 'use_proxy_default'),
    ], crawl)

    _collect(updates, [
        ('PROXY_ENABLED', 'enabled'),
        ('PROXY_HTTP', 'http'),
        ('PROXY_HTTPS', 'https'),
        ('PROXY_SOCKS5', 'socks5'),
        ('PLAYWRIGHT_PROXY', 'playwright'),
    ], proxy)

    _collect(updates, [
        ('RAGFLOW_UPLOAD_ENABLED', 'upload_enabled'),
        ('RAGFLOW_AUTO_PARSE', 'auto_parse'),
        ('RAGFLOW_REUPLOAD_EXISTING', 'reupload_existing'),
        ('RAGFLOW_TIMEOUT', 'timeout'),
        ('RAGFLOW_UPLOAD_RETRIES', 'upload_retries'),
        ('RAGFLOW_TTS_ENABLED', 'tts_enabled'),
        ('RAGFLOW_TTS_TIMEOUT', 'tts_timeout'),
        ('RAGFLOW_TTS_MAX_CHARS', 'tts_max_chars'),
        ('RAGFLOW_TTS_MODEL', 'tts_model'),
        ('RAGFLOW_TTS_VOICE_ZH', 'tts_voice_zh'),
        ('RAGFLOW_TTS_VOICE_EN', 'tts_voice_en'),
        ('RAGFLOW_TTS_LANGUAGE_MODE', 'tts_language_mode'),
        ('RAGFLOW_TTS_PREBUFFER_SENTENCES', 'tts_prebuffer_sentences'),
        ('RAGFLOW_TTS_CACHE_DAYS', 'tts_cache_days'),
        ('RAGFLOW_TTS_CACHE_MAX_MB', 'tts_cache_max_mb'),
        ('RAGFLOW_TTS_VOICE_CATALOG', 'tts_voice_catalog'),
        ('INTEL_TTS_ENGINE', 'intel_tts_engine'),
        ('INTEL_TTS_VOICE_PROFILE', 'intel_tts_voice_profile'),
        ('INTEL_TTS_DIALECT', 'intel_tts_dialect'),
        ('INTEL_TTS_GENDER', 'intel_tts_gender'),
        ('INTEL_TTS_EMOTION', 'intel_tts_emotion'),
        ('INTEL_TTS_SPEED', 'intel_tts_speed'),
        ('INTEL_TTS_BUFFER_MS', 'intel_tts_buffer_ms'),
        ('INTEL_TTS_SEGMENT_MAX_CHARS_ZH', 'intel_tts_segment_max_chars_zh'),
        ('INTEL_TTS_SEGMENT_MAX_WORDS_EN', 'intel_tts_segment_max_words_en'),
        ('RAGFLOW_PROXY_ENABLED', 'proxy_enabled'),
        ('RAGFLOW_PROXY_HTTP', 'proxy_http'),
        ('RAGFLOW_PROXY_HTTPS', 'proxy_https'),
        ('RAGFLOW_PROXY_SOCKS5', 'proxy_socks5'),
    ], ragflow)
    layered_tts_keys = ('tts_language', 'tts_dialect', 'tts_gender')
    if any(key in remote_pipeline for key in layered_tts_keys):
        language = _normalize_env_value(
            'REMOTE_PIPELINE_TTS_LANGUAGE',
            remote_pipeline.get('tts_language') or _value(current_values, 'REMOTE_PIPELINE_TTS_LANGUAGE') or 'zh',
        )
        dialect = _normalize_env_value(
            'REMOTE_PIPELINE_TTS_DIALECT',
            remote_pipeline.get('tts_dialect') or _value(current_values, 'REMOTE_PIPELINE_TTS_DIALECT') or 'mandarin',
        )
        gender = _normalize_env_value(
            'REMOTE_PIPELINE_TTS_GENDER',
            remote_pipeline.get('tts_gender') or _value(current_values, 'REMOTE_PIPELINE_TTS_GENDER') or 'female',
        )
        updates['REMOTE_PIPELINE_TTS_LANGUAGE'] = language
        updates['REMOTE_PIPELINE_TTS_DIALECT'] = dialect
        updates['REMOTE_PIPELINE_TTS_GENDER'] = gender
        updates['REMOTE_PIPELINE_TTS_VOICE'] = config.resolve_remote_tts_voice(language, dialect, gender)
    elif 'tts_voice' in remote_pipeline:
        selected_voice = _clean_value(remote_pipeline.get('tts_voice')) or 'default'
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', selected_voice):
            raise ValueError('VPN 远端朗读音色只能包含字母、数字、点、下划线和连字符')
        updates['REMOTE_PIPELINE_TTS_VOICE'] = selected_voice
    if _clean_value(remote_pipeline.get('url')):
        updates['REMOTE_PIPELINE_URL'] = _normalize_env_value(
            'REMOTE_PIPELINE_URL', remote_pipeline.get('url')
        )
    if 'intel_tts_speed' in ragflow:
        try:
            speed = float(ragflow.get('intel_tts_speed'))
        except (TypeError, ValueError) as exc:
            raise ValueError('资讯平台朗读语速必须是 0.5 至 2.0 之间的数字') from exc
        if not 0.5 <= speed <= 2.0:
            raise ValueError('资讯平台朗读语速必须在 0.5 至 2.0 之间')
        updates['INTEL_TTS_SPEED'] = f'{speed:g}'
    if 'tts_voice_catalog' in ragflow:
        try:
            catalog = json.loads(str(ragflow.get('tts_voice_catalog') or '[]'))
        except (TypeError, ValueError) as exc:
            raise ValueError('音色库必须是有效的 JSON 数组') from exc
        if not isinstance(catalog, list):
            raise ValueError('音色库必须是 JSON 数组')
        cleaned_catalog = []
        for item in catalog:
            if not isinstance(item, dict) or not str(item.get('id') or '').strip():
                raise ValueError('每个音色必须包含非空 id')
            language = str(item.get('language') or 'all').casefold()
            if language not in {'zh', 'en', 'all'}:
                raise ValueError('音色语言只能是 zh、en 或 all')
            voice_id = str(item.get('id')).strip()
            cleaned_catalog.append({
                'id': voice_id,
                'label': str(item.get('label') or voice_id).strip(),
                'language': language,
            })
        updates['RAGFLOW_TTS_VOICE_CATALOG'] = json.dumps(cleaned_catalog, ensure_ascii=False, separators=(',', ':'))
    if not _clean_value(current_values.get('RAGFLOW_BASE_URL')) and _clean_value(ragflow.get('base_url')):
        updates['RAGFLOW_BASE_URL'] = _normalize_env_value('RAGFLOW_BASE_URL', ragflow.get('base_url'))
    if not _clean_value(current_values.get('RAGFLOW_API_KEY')):
        updates.update(_secret_update(ragflow, 'RAGFLOW_API_KEY', 'clear_api_key', 'api_key', current_values))

    if serpapi:
        _collect(updates, [
            ('SERPAPI_ENABLED', 'enabled'),
            ('SERPAPI_ENGINE', 'engine'),
            ('SERPAPI_DEFAULT_REGION', 'default_region'),
            ('SERPAPI_DEFAULT_LANGUAGE', 'default_language'),
            ('SERPAPI_TIMEOUT_SECONDS', 'timeout_seconds'),
            ('SERPAPI_MAX_RETRIES', 'max_retries'),
            ('SERPAPI_MAX_QUERIES_PER_RUN', 'max_queries_per_run'),
            ('SERPAPI_DAILY_QUERY_BUDGET', 'daily_query_budget'),
            ('SERPAPI_RECENCY_DAYS', 'recency_days'),
            ('SERPAPI_RESULT_LANGUAGE', 'result_language'),
        ], serpapi)
        updates.update(_secret_update(serpapi, 'SERPAPI_API_KEY', 'clear_api_key', 'api_key', current_values))

    # Tavily：与 serpapi 完全同构；密钥走 _secret_update —— 页面不回显明文，空值表示"保持原值"，
    # 只有显式 clear_api_key 才清空（避免每次保存把 key 清掉）
    if tavily:
        _collect(updates, [
            ('TAVILY_ENABLED', 'enabled'),
            ('TAVILY_BASE_URL', 'base_url'),
            ('TAVILY_MAX_RESULTS', 'max_results'),
            ('TAVILY_SEARCH_DEPTH', 'search_depth'),
            ('TAVILY_TIMEOUT_SECONDS', 'timeout_seconds'),
            ('TAVILY_MAX_RETRIES', 'max_retries'),
            ('TAVILY_QUERY_MODE', 'query_mode'),
            ('TAVILY_MAX_CALLS_PER_RUN', 'max_calls_per_run'),
            ('TAVILY_MAX_CALLS_PER_MONTH', 'max_calls_per_month'),
        ], tavily)
        updates.update(_secret_update(tavily, 'TAVILY_API_KEY', 'clear_api_key', 'api_key', current_values))

    # 搜索引擎调度与配额（含 SerpAPI 的模式/上限，因为按引擎分别设置）
    if search_engine:
        _collect(updates, [
            ('SEARCH_ENABLED', 'enabled'),
            ('SEARCH_PROVIDER', 'provider'),
            ('SEARCH_RUNS_PER_DAY', 'runs_per_day'),
            ('SEARCH_RUN_HOURS', 'run_hours'),
            ('SEARCH_PACKS_PER_RUN', 'packs_per_run'),
            ('SEARCH_KEYWORDS_PER_PACK', 'keywords_per_pack'),
            ('SERPAPI_QUERY_MODE', 'serpapi_query_mode'),
            ('SERPAPI_MAX_CALLS_PER_RUN', 'serpapi_max_calls_per_run'),
            ('SERPAPI_MAX_CALLS_PER_MONTH', 'serpapi_max_calls_per_month'),
        ], search_engine)

    if financial:
        financial_fields = (
            ('FINANCIAL_ROLLOUT_STAGE', 'rollout_stage'),
            (
                'FINANCIAL_ROLLOUT_OBSERVATION_SECONDS',
                'rollout_observation_seconds',
            ),
            ('FINANCIAL_INTELLIGENCE_ENABLED', 'financial_intelligence_enabled'),
            ('FINANCIAL_INFORMATION_NEEDS_ENABLED', 'information_needs_enabled'),
            ('FINANCIAL_INSTRUMENT_DISCOVERY_ENABLED', 'instrument_discovery_enabled'),
            (
                'FINANCIAL_EXTERNAL_INSTRUMENT_DISCOVERY_ENABLED',
                'external_instrument_discovery_enabled',
            ),
            (
                'FINANCIAL_INSTRUMENT_SEARCH_FALLBACK_ENABLED',
                'instrument_search_fallback_enabled',
            ),
            ('FINANCIAL_INSTRUMENT_AUTO_PROMOTION_ENABLED', 'instrument_auto_promotion_enabled'),
            ('FINANCIAL_LATEST_NEWS_ENABLED', 'latest_news_enabled'),
            ('FINANCIAL_LATEST_BUNDLE_ENABLED', 'latest_bundle_enabled'),
            ('TRADING_AGENTS_ENABLED', 'trading_agents_enabled'),
            ('FINANCIAL_AUTO_RESEARCH_ENABLED', 'auto_research_enabled'),
            ('TRADING_SIMULATION_ENABLED', 'simulation_enabled'),
            ('AKSHARE_CN_ENABLED', 'akshare_cn_enabled'),
            ('TUSHARE_CN_ENABLED', 'tushare_cn_enabled'),
            ('YAHOO_FINANCE_ENABLED', 'yahoo_finance_enabled'),
            ('ALPHA_VANTAGE_ENABLED', 'alpha_vantage_enabled'),
            (
                'ALPHA_VANTAGE_QUOTE_ENTITLEMENT',
                'alpha_vantage_quote_entitlement',
            ),
            (
                'ALPHA_VANTAGE_REALTIME_ENTITLED',
                'alpha_vantage_realtime_entitled',
            ),
            ('FRED_ENABLED', 'fred_enabled'),
            ('POLYMARKET_ENABLED', 'polymarket_enabled'),
            ('EASYQUOTATION_ENABLED', 'easyquotation_enabled'),
            (
                'OFFICIAL_FINANCIAL_EVIDENCE_ENABLED',
                'official_evidence_enabled',
            ),
            ('FINANCIAL_PROVIDER_TIMEOUT_SECONDS', 'provider_timeout_seconds'),
            ('FINANCIAL_PROVIDER_MAX_RETRIES', 'provider_max_retries'),
            ('FINANCIAL_PROVIDER_MAX_CONCURRENCY', 'provider_max_concurrency'),
            ('FINANCIAL_PROVIDER_DAILY_CALL_BUDGET', 'provider_daily_call_budget'),
            ('FINANCIAL_QUOTE_FRESHNESS_SECONDS', 'quote_freshness_seconds'),
            ('FINANCIAL_LATEST_QUOTE_TIMEOUT_SECONDS', 'latest_quote_timeout_seconds'),
            ('FINANCIAL_LATEST_NEWS_TIMEOUT_SECONDS', 'latest_news_timeout_seconds'),
            ('FINANCIAL_LATEST_BUNDLE_TIMEOUT_SECONDS', 'latest_bundle_timeout_seconds'),
            ('FINANCIAL_NEWS_LOOKBACK_DAYS', 'news_lookback_days'),
            ('FINANCIAL_MARKET_BREADTH_FRESHNESS_SECONDS', 'market_breadth_freshness_seconds'),
            ('FINANCIAL_NEWS_FRESHNESS_SECONDS', 'news_freshness_seconds'),
            ('FINANCIAL_FUNDAMENTAL_FRESHNESS_SECONDS', 'fundamental_freshness_seconds'),
            ('FINANCIAL_RESEARCH_MAX_LLM_CALLS', 'research_max_llm_calls'),
            ('FINANCIAL_RESEARCH_MAX_TOKENS', 'research_max_tokens'),
            ('FINANCIAL_RESEARCH_MAX_DEBATE_ROUNDS', 'research_max_debate_rounds'),
            ('FINANCIAL_RESEARCH_TIMEOUT_SECONDS', 'research_timeout_seconds'),
            ('FINANCIAL_RESEARCH_CACHE_SECONDS', 'research_cache_seconds'),
            ('FINANCIAL_AUTO_RESEARCH_DAILY_BUDGET', 'auto_research_daily_budget'),
        )
        for env_key, input_key in financial_fields:
            if input_key not in financial:
                continue
            updates[env_key] = (
                _strict_int_string(env_key, financial[input_key])
                if env_key in INT_LIMITS
                else _normalize_env_value(env_key, financial[input_key])
            )
        updates.update(
            _secret_update(
                financial,
                'TUSHARE_TOKEN',
                'clear_tushare_token',
                'tushare_token',
                current_values,
            )
        )
        updates.update(
            _secret_update(
                financial,
                'ALPHA_VANTAGE_API_KEY',
                'clear_alpha_vantage_api_key',
                'alpha_vantage_api_key',
                current_values,
            )
        )
        updates.update(
            _secret_update(
                financial,
                'FRED_API_KEY',
                'clear_fred_api_key',
                'fred_api_key',
                current_values,
            )
        )

    updates.update(parse_updates(platform_sources))

    return updates


def _restart_messages(changed_keys: set) -> list:
    reasons = []
    if changed_keys & {'FLASK_HOST', 'FLASK_PORT', 'FLASK_DEBUG'}:
        reasons.append('Web监听地址/端口需要重启服务后生效')
    if 'SECRET_KEY' in changed_keys:
        reasons.append('登录密钥需要重启后完全生效，已有登录会话可能失效')
    if changed_keys & {'DATABASE_PATH', 'CRAWL_RESULTS_DIR', 'AUTH_STORAGE_DIR'}:
        reasons.append('数据库或存储目录需要重启后让所有模块使用新路径')
    if changed_keys & {'REDIS_HOST', 'REDIS_PORT', 'REDIS_DB', 'REDIS_PASSWORD'}:
        reasons.append('Redis连接参数需要重启后重新连接')
    if 'CRAWL_SCHEDULER_MAX_CONCURRENT' in changed_keys:
        reasons.append('线程池最大并发需要重启调度器后完全生效')
    if changed_keys & {
        'SERPAPI_API_KEY', 'SERPAPI_ENABLED', 'SERPAPI_ENGINE', 'SERPAPI_DEFAULT_REGION',
        'SERPAPI_DEFAULT_LANGUAGE', 'SERPAPI_TIMEOUT_SECONDS', 'SERPAPI_MAX_RETRIES',
        'SERPAPI_MAX_QUERIES_PER_RUN', 'SERPAPI_DAILY_QUERY_BUDGET',
    }:
        reasons.append('SerpAPI 配置需要重启资讯工作进程后完全生效')
    if changed_keys & {
        'TAVILY_API_KEY', 'TAVILY_ENABLED', 'TAVILY_QUERY_MODE', 'TAVILY_BASE_URL',
        'SEARCH_ENABLED', 'SEARCH_PROVIDER', 'SEARCH_RUN_HOURS', 'SEARCH_RUNS_PER_DAY',
        'SEARCH_PACKS_PER_RUN', 'SEARCH_KEYWORDS_PER_PACK',
        'SERPAPI_QUERY_MODE', 'SERPAPI_MAX_CALLS_PER_RUN', 'SERPAPI_MAX_CALLS_PER_MONTH',
    }:
        reasons.append('搜索引擎支持（Tavily/SerpAPI 调度与配额）需要重启资讯工作进程后完全生效')
    if changed_keys & {
        'FINANCIAL_INTELLIGENCE_ENABLED', 'TRADING_AGENTS_ENABLED',
        'FINANCIAL_INFORMATION_NEEDS_ENABLED',
        'FINANCIAL_INSTRUMENT_DISCOVERY_ENABLED',
        'FINANCIAL_INSTRUMENT_AUTO_PROMOTION_ENABLED',
        'FINANCIAL_LATEST_NEWS_ENABLED', 'FINANCIAL_LATEST_BUNDLE_ENABLED',
        'FINANCIAL_AUTO_RESEARCH_ENABLED', 'TRADING_SIMULATION_ENABLED',
        'FINANCIAL_ROLLOUT_STAGE', 'FINANCIAL_ROLLOUT_STAGE_CHANGED_AT',
        'FINANCIAL_ROLLOUT_OBSERVATION_SECONDS',
        'AKSHARE_CN_ENABLED', 'TUSHARE_CN_ENABLED', 'TUSHARE_TOKEN',
        'YAHOO_FINANCE_ENABLED', 'ALPHA_VANTAGE_ENABLED',
        'ALPHA_VANTAGE_API_KEY', 'ALPHA_VANTAGE_QUOTE_ENTITLEMENT',
        'ALPHA_VANTAGE_REALTIME_ENTITLED',
        'FRED_ENABLED', 'FRED_API_KEY', 'POLYMARKET_ENABLED',
        'EASYQUOTATION_ENABLED', 'OFFICIAL_FINANCIAL_EVIDENCE_ENABLED',
        'FINANCIAL_PROVIDER_TIMEOUT_SECONDS', 'FINANCIAL_PROVIDER_MAX_RETRIES',
        'FINANCIAL_PROVIDER_MAX_CONCURRENCY', 'FINANCIAL_PROVIDER_DAILY_CALL_BUDGET',
        'FINANCIAL_QUOTE_FRESHNESS_SECONDS',
        'FINANCIAL_LATEST_QUOTE_TIMEOUT_SECONDS',
        'FINANCIAL_LATEST_NEWS_TIMEOUT_SECONDS',
        'FINANCIAL_LATEST_BUNDLE_TIMEOUT_SECONDS',
        'FINANCIAL_NEWS_LOOKBACK_DAYS',
        'FINANCIAL_MARKET_BREADTH_FRESHNESS_SECONDS',
        'FINANCIAL_NEWS_FRESHNESS_SECONDS',
        'FINANCIAL_FUNDAMENTAL_FRESHNESS_SECONDS',
        'FINANCIAL_RESEARCH_MAX_LLM_CALLS', 'FINANCIAL_RESEARCH_MAX_TOKENS',
        'FINANCIAL_RESEARCH_MAX_DEBATE_ROUNDS',
        'FINANCIAL_RESEARCH_TIMEOUT_SECONDS',
        'FINANCIAL_RESEARCH_CACHE_SECONDS',
        'FINANCIAL_AUTO_RESEARCH_DAILY_BUDGET',
    }:
        reasons.append('金融与 TradingAgents 配置已应用到 Web；资讯工作进程需重启后生效')
    if 'REMOTE_PIPELINE_TTS_VOICE' in changed_keys:
        reasons.append('VPN 远端朗读音色已保存；资讯调度 Worker 需重启后对新聚合任务生效')
    return reasons


def _rollout_transition_for_updates(
    current_values: Dict[str, str],
    updates: Dict[str, str],
    *,
    health: Dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Dict[str, Any] | None:
    """Validate a stage write and stamp its observation window atomically."""

    if 'FINANCIAL_ROLLOUT_STAGE' not in updates:
        return None
    current_stage = _managed_rollout_stage(current_values)
    target_stage = normalize_rollout_stage(
        updates['FINANCIAL_ROLLOUT_STAGE'], strict=True
    )
    captured = now or datetime.now(timezone.utc)
    decision = rollout_transition_decision(
        current_stage,
        target_stage,
        changed_at=_value(current_values, 'FINANCIAL_ROLLOUT_STAGE_CHANGED_AT'),
        observation_seconds=int(
            updates.get('FINANCIAL_ROLLOUT_OBSERVATION_SECONDS')
            or _value(current_values, 'FINANCIAL_ROLLOUT_OBSERVATION_SECONDS')
            or 3600
        ),
        health=health or {},
        now=captured,
    )
    if decision['allowed'] and decision['action'] != 'no_change':
        updates['FINANCIAL_ROLLOUT_STAGE_CHANGED_AT'] = utc_text(captured)
    return decision


@config_management_bp.route('/config', methods=['GET'])
def get_config():
    try:
        return jsonify(_public_config(_read_env_values()))
    except Exception as exc:
        log_error(exc, '读取配置管理')
        return jsonify({'success': False, 'error': f'读取配置失败: {exc}'}), 500


@config_management_bp.route('/financial/tushare/probe', methods=['POST'])
@admin_required
def probe_tushare_permissions():
    """Explicit admin action; reads the server-side token and returns no secret."""
    connection = None
    try:
        values = _read_env_values()
        database_path = Path(_value(values, 'DATABASE_PATH')).expanduser().resolve()
        settings = {
            'FINANCIAL_INTELLIGENCE_ENABLED': _bool_value(
                values, 'FINANCIAL_INTELLIGENCE_ENABLED', False
            ),
            'TUSHARE_CN_ENABLED': _bool_value(values, 'TUSHARE_CN_ENABLED', False),
            'TUSHARE_TOKEN': (
                values.get('TUSHARE_TOKEN')
                or getattr(config, 'TUSHARE_TOKEN', '')
                or ''
            ),
            'FINANCIAL_PROVIDER_TIMEOUT_SECONDS': _int_value(
                values, 'FINANCIAL_PROVIDER_TIMEOUT_SECONDS'
            ),
        }
        connection = _open_database_connection(path=str(database_path))
        connection.execute('PRAGMA foreign_keys=ON')
        ensure_financial_tables(connection.cursor())
        registry = InstrumentRegistry(connection)
        registry.load_controlled_seed()
        provider = TushareCNProvider(
            instrument_registry=registry,
            settings=settings,
            connection=connection,
        )
        probe = provider.probe_permissions(
            request_id='config-tushare-permission-probe',
            requested_at=datetime.now(timezone.utc),
        )
        return jsonify({
            'success': True,
            'tushare_status': {
                'availability': probe['overall'],
                'token_status': probe['token_status'],
                'checked_at': probe['checked_at'],
                'capabilities': probe['capabilities'],
            },
        })
    except Exception as exc:
        log_error(type(exc).__name__, 'Tushare权限探测')
        return jsonify({
            'success': False,
            'error': 'Tushare 权限探测失败，请检查服务状态和后台日志中的错误类型',
        }), 503
    finally:
        if connection is not None:
            connection.close()


@config_management_bp.route('/config', methods=['PUT'])
@admin_required
def update_config():
    try:
        data = request.get_json(silent=True) or {}
        current_values = _read_env_values()
        updates = _build_updates(data, current_values)
        rollout_health = {}
        if 'FINANCIAL_ROLLOUT_STAGE' in updates:
            current_stage = _managed_rollout_stage(current_values)
            target_stage = normalize_rollout_stage(
                updates['FINANCIAL_ROLLOUT_STAGE'], strict=True
            )
            needs_health = (
                ROLLOUT_STAGE_INDEX[target_stage]
                > ROLLOUT_STAGE_INDEX[current_stage]
                and not (current_stage == 'off' and target_stage == 'rss')
            )
            if needs_health:
                from sqlite_database import sqlite_db

                health_settings = dict(current_values)
                health_settings.update(updates)
                health_settings['FINANCIAL_ROLLOUT_STAGE'] = current_stage
                health_settings['FINANCIAL_ROLLOUT_STAGE_CHANGED_AT'] = _value(
                    current_values, 'FINANCIAL_ROLLOUT_STAGE_CHANGED_AT'
                )
                rollout_health = FinancialHealthService(
                    sqlite_db, settings=health_settings
                ).build()
        rollout_transition = _rollout_transition_for_updates(
            current_values, updates, health=rollout_health
        )
        if rollout_transition and not rollout_transition['allowed']:
            return jsonify({
                'success': False,
                'error': '金融灰度阶段不满足升级条件',
                'rollout_transition': rollout_transition,
            }), 409
        changed = _changed_keys(current_values, updates)
        _write_env_values(updates)
        _apply_runtime_values(updates)
        if 'TUSHARE_TOKEN' in changed:
            _invalidate_tushare_probe_after_token_change(updates)
        restart_reasons = _restart_messages(changed)
        message = '配置已保存'
        if restart_reasons:
            message += '；部分配置需要重启服务后完全生效'
        else:
            message += '并应用到当前进程'
        log_info(f'配置管理已更新: {", ".join(sorted(changed)) or "无变化"}', '配置管理')
        return jsonify({
            **_public_config(_read_env_values()),
            'message': message,
            'restart_required': bool(restart_reasons),
            'restart_reasons': restart_reasons,
            'rollout_transition': rollout_transition,
        })
    except ValueError as exc:
        return jsonify({'success': False, 'error': f'配置值无效: {exc}'}), 400
    except Exception as exc:
        log_error(exc, '更新配置管理')
        return jsonify({'success': False, 'error': f'保存配置失败: {exc}'}), 500


@config_management_bp.route('/platform-sources/test/<platform_id>', methods=['POST'])
@admin_required
def test_platform_source(platform_id: str):
    """测试单个信源：检查工具可用性 + 凭据 + 尽力取一条结果。"""
    try:
        values = _read_env_values()
        result = platform_test(platform_id, values)
        return jsonify({'success': result.get('ok', False), **result})
    except Exception as exc:
        log_error(exc, f'测试信源 {platform_id}')
        return jsonify({'success': False, 'message': f'测试失败: {exc}', 'got_info': False}), 500

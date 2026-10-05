#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
配置文件
包含应用的各种配置参数
所有可配置的值都应该通过环境变量或配置文件设置
"""

import json
import os
import sys


def _load_dotenv_file(path='.env'):
    if not os.path.exists(path):
        return

    try:
        with open(path, 'r', encoding='utf-8') as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                if line.startswith('export '):
                    line = line[7:].strip()
                key, value = line.split('=', 1)
                key = key.strip()
                if not key or key in os.environ:
                    continue
                value = value.strip().strip('"').strip("'")
                os.environ[key] = value
    except Exception as exc:
        print(f"Warning: failed to load .env: {exc}")


_load_dotenv_file()
_load_dotenv_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'remote_pipeline.client.env'))

APP_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', os.path.join(APP_BASE_DIR, 'data', 'ms-playwright'))
os.environ.setdefault('CRAWL4_AI_BASE_DIRECTORY', os.path.join(APP_BASE_DIR, 'data', 'crawl4ai'))

# ── SMTP（发登录/验证码邮件）：私有邮箱可用，如 QQ/163/Gmail/Outlook ──
SMTP_HOST = os.getenv('SMTP_HOST', '').strip()
SMTP_PORT = int(os.getenv('SMTP_PORT', '465') or 465)
SMTP_USER = os.getenv('SMTP_USER', '').strip()
SMTP_PASS = os.getenv('SMTP_PASS', '').strip()

# ── 生产健康告警（worker未跑/远程不可达/候选堆积 → 邮件） ──
SYSTEM_ALERT_EMAIL = os.getenv('SYSTEM_ALERT_EMAIL', '').strip()
SYSTEM_ALERT_BACKLOG = int(os.getenv('SYSTEM_ALERT_BACKLOG', '60') or '60')
SYSTEM_ALERT_INTERVAL = int(os.getenv('SYSTEM_ALERT_INTERVAL', '300') or '300')


def _configure_stdio():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except Exception:
                pass


_configure_stdio()


def _env_bool(name, default=False):
    """Parse a boolean environment variable."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


def _env_int(name, default, min_value=None, max_value=None):
    """Parse a bounded integer environment variable."""
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _env_float(name, default, min_value=None, max_value=None):
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = float(default)
    if min_value is not None:
        value = max(float(min_value), value)
    if max_value is not None:
        value = min(float(max_value), value)
    return value


def _env_str(name, default=''):
    value = os.getenv(name, default)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


# ==================== Ragflow API配置 ====================
# 默认指向内部 Ragflow 服务器，若部署到其他环境可通过环境变量覆盖
RAGFLOW_BASE_URL = (_env_str('RAGFLOW_BASE_URL', '') or '').rstrip('/')
RAGFLOW_API_KEY = _env_str('RAGFLOW_API_KEY', '')
RAGFLOW_UPLOAD_ENABLED = _env_bool('RAGFLOW_UPLOAD_ENABLED', bool(RAGFLOW_BASE_URL and RAGFLOW_API_KEY))
RAGFLOW_AUTO_PARSE = _env_bool('RAGFLOW_AUTO_PARSE', True)
RAGFLOW_REUPLOAD_EXISTING = _env_bool('RAGFLOW_REUPLOAD_EXISTING', True)
RAGFLOW_TIMEOUT = _env_int('RAGFLOW_TIMEOUT', 45, 5, 600)
RAGFLOW_UPLOAD_RETRIES = _env_int('RAGFLOW_UPLOAD_RETRIES', 1, 0, 5)
RAGFLOW_TTS_ENABLED = _env_bool('RAGFLOW_TTS_ENABLED', bool(RAGFLOW_BASE_URL and RAGFLOW_API_KEY))
# 🔥 TTS 总闸：TTS 引擎已更换，全面改进完成前所有涉及 TTS 的代码逻辑必须关闭。
# 两条 TTS 链路（VPN 精炼音频 / 本机按需朗读合成）都在使用点与本开关合取，
# 即使 REMOTE_PIPELINE_TTS / RAGFLOW_TTS_ENABLED 被误开也不会执行合成。
SYSTEM_TTS_ENABLED = _env_bool('SYSTEM_TTS_ENABLED', False)
RAGFLOW_TTS_TIMEOUT = _env_int('RAGFLOW_TTS_TIMEOUT', 60, 5, 300)
RAGFLOW_TTS_MAX_CHARS = _env_int('RAGFLOW_TTS_MAX_CHARS', 500, 100, 10000)
RAGFLOW_TTS_MODEL = _env_str('RAGFLOW_TTS_MODEL', '') or ''
RAGFLOW_TTS_VOICE_ZH = _env_str('RAGFLOW_TTS_VOICE_ZH', '') or ''
RAGFLOW_TTS_VOICE_EN = _env_str('RAGFLOW_TTS_VOICE_EN', '') or ''
RAGFLOW_TTS_LANGUAGE_MODE = (_env_str('RAGFLOW_TTS_LANGUAGE_MODE', 'auto') or 'auto').casefold()
if RAGFLOW_TTS_LANGUAGE_MODE not in {'auto', 'zh', 'en'}:
    RAGFLOW_TTS_LANGUAGE_MODE = 'auto'
RAGFLOW_TTS_PREBUFFER_SENTENCES = _env_int('RAGFLOW_TTS_PREBUFFER_SENTENCES', 3, 1, 10)
RAGFLOW_TTS_CACHE_DAYS = _env_int('RAGFLOW_TTS_CACHE_DAYS', 30, 1, 365)
RAGFLOW_TTS_CACHE_MAX_MB = _env_int('RAGFLOW_TTS_CACHE_MAX_MB', 1024, 64, 10240)
RAGFLOW_TTS_VOICE_CATALOG = _env_str('RAGFLOW_TTS_VOICE_CATALOG', '[]') or '[]'
try:
    RAGFLOW_TTS_VOICE_CATALOG = json.loads(RAGFLOW_TTS_VOICE_CATALOG)
except (TypeError, ValueError):
    RAGFLOW_TTS_VOICE_CATALOG = []
if not isinstance(RAGFLOW_TTS_VOICE_CATALOG, list):
    RAGFLOW_TTS_VOICE_CATALOG = []
# Article aggregation has an independent CosyVoice3 profile. These values are
# sent only with this application's synthesis requests; they never update the
# Panython/RAGFlow global TTS settings endpoint.
INTEL_TTS_ENGINE = _env_str('INTEL_TTS_ENGINE', 'CosyVoice3') or 'CosyVoice3'
INTEL_TTS_VOICE_PROFILE = _env_str('INTEL_TTS_VOICE_PROFILE', 'male_mandarin_01') or 'male_mandarin_01'
INTEL_TTS_DIALECT = _env_str('INTEL_TTS_DIALECT', 'mandarin') or 'mandarin'
INTEL_TTS_GENDER = _env_str('INTEL_TTS_GENDER', 'male') or 'male'
INTEL_TTS_EMOTION = _env_str('INTEL_TTS_EMOTION', 'lively') or 'lively'
INTEL_TTS_SPEED = _env_float('INTEL_TTS_SPEED', 1.15, 0.5, 2.0)
INTEL_TTS_BUFFER_MS = _env_int('INTEL_TTS_BUFFER_MS', 1400, 400, 5000)
INTEL_TTS_SEGMENT_MAX_CHARS_ZH = _env_int('INTEL_TTS_SEGMENT_MAX_CHARS_ZH', 50, 20, 200)
INTEL_TTS_SEGMENT_MAX_WORDS_EN = _env_int('INTEL_TTS_SEGMENT_MAX_WORDS_EN', 20, 8, 100)
# 'ragflow' keeps the legacy Panython /api/v1/chat/audio/speech envelope;
# 'openai' targets a CosyVoice OpenAI-compatible /v1/audio/speech endpoint.
INTEL_TTS_PROTOCOL = (_env_str('INTEL_TTS_PROTOCOL', 'ragflow') or 'ragflow').casefold()
if INTEL_TTS_PROTOCOL not in {'ragflow', 'openai'}:
    INTEL_TTS_PROTOCOL = 'ragflow'
# Independent TTS endpoint. Falls back to the legacy RAGFlow values when the
# new variables are absent so existing deployments keep working unchanged.
INTEL_TTS_BASE_URL = (_env_str('INTEL_TTS_BASE_URL', '') or RAGFLOW_BASE_URL or '').rstrip('/')
INTEL_TTS_API_KEY = _env_str('INTEL_TTS_API_KEY', '') or RAGFLOW_API_KEY or ''
RAGFLOW_PROXY_ENABLED = _env_bool('RAGFLOW_PROXY_ENABLED', False)
RAGFLOW_PROXY_HTTP = _env_str('RAGFLOW_PROXY_HTTP')
RAGFLOW_PROXY_HTTPS = _env_str('RAGFLOW_PROXY_HTTPS')
RAGFLOW_PROXY_SOCKS5 = _env_str('RAGFLOW_PROXY_SOCKS5')

# ==================== 市场资讯雷达配置 ====================
# This is the final ingestion guard.  Individual crawlers may pre-filter
# earlier, but no article without a keyword hit may reach the article store.
CRAWL_REQUIRE_KEYWORD_MATCH = _env_bool('CRAWL_REQUIRE_KEYWORD_MATCH', True)
# Dual-channel crawler. Browser automation is forbidden on the development
# machine; JavaScript rendering belongs exclusively to the VPN pipeline.
PIPELINE_MODE = (_env_str('PIPELINE_MODE', 'auto') or 'auto').casefold()
if PIPELINE_MODE not in {'auto', 'required', 'local'}:
    PIPELINE_MODE = 'auto'
REMOTE_PIPELINE_URL = (_env_str('REMOTE_PIPELINE_URL', 'http://10.88.0.1:11236') or '').rstrip('/')
REMOTE_PIPELINE_TOKEN = _env_str('REMOTE_PIPELINE_TOKEN', '') or ''
REMOTE_PIPELINE_HEALTH_TIMEOUT_SECONDS = _env_float('REMOTE_PIPELINE_HEALTH_TIMEOUT_SECONDS', 2.0, 0.2, 30.0)
REMOTE_PIPELINE_JOB_TIMEOUT_SECONDS = _env_int('REMOTE_PIPELINE_JOB_TIMEOUT_SECONDS', 900, 30, 7200)
REMOTE_PIPELINE_POLL_SECONDS = _env_float('REMOTE_PIPELINE_POLL_SECONDS', 1.0, 0.2, 30.0)
REMOTE_PIPELINE_CIRCUIT_SECONDS = _env_int('REMOTE_PIPELINE_CIRCUIT_SECONDS', 30, 1, 600)
REMOTE_PIPELINE_ENRICH = _env_bool('REMOTE_PIPELINE_ENRICH', True)
REMOTE_PIPELINE_TTS = _env_bool('REMOTE_PIPELINE_TTS', True)
REMOTE_PIPELINE_TTS_LANGUAGE = (_env_str('REMOTE_PIPELINE_TTS_LANGUAGE', 'zh') or 'zh').casefold()
REMOTE_PIPELINE_TTS_DIALECT = (_env_str('REMOTE_PIPELINE_TTS_DIALECT', 'mandarin') or 'mandarin').casefold()
REMOTE_PIPELINE_TTS_GENDER = (_env_str('REMOTE_PIPELINE_TTS_GENDER', 'female') or 'female').casefold()


def resolve_remote_tts_voice(language: str | None = None, dialect: str | None = None, gender: str | None = None) -> str:
    """Map the layered user profile to a concrete CosyVoice2 voice id.

    The VPN TTS service only accepts a concrete voice id.  The profile is kept
    separate from the id so the system-management page can show 语言 -> 方言 ->
    性别 without coupling those choices to whatever ids are registered on the
    Ubuntu TTS service.  Unknown/unsupported combinations still resolve to a
    stable id so the pipeline can report a clear TTS-side error instead of
    silently reusing the wrong voice.
    """

    language = (language or REMOTE_PIPELINE_TTS_LANGUAGE or 'zh').strip().casefold()
    dialect = (dialect or REMOTE_PIPELINE_TTS_DIALECT or 'mandarin').strip().casefold()
    gender = (gender or REMOTE_PIPELINE_TTS_GENDER or 'female').strip().casefold()
    if language == 'en':
        return 'english_male' if gender == 'male' else 'english_female'
    if language != 'zh':
        return 'default'
    if dialect == 'cantonese':
        return 'cantonese_male' if gender == 'male' else 'cantonese_female'
    return 'mandarin_male' if gender == 'male' else 'default'


# Kept for backwards compatibility with callers that previously read the single
# voice id directly.  The value is derived, not an independent setting.
REMOTE_PIPELINE_TTS_VOICE = resolve_remote_tts_voice()
# Hard safety invariant for this application deployment.  Browser automation
# exists only in remote_pipeline; setting an environment variable cannot turn
# it back on in the development/web process.
LOCAL_BROWSER_ENABLED = False
ZYTE_ENABLED = False
INTEL_DEFAULT_INDUSTRY_PACK = _env_str('INTEL_DEFAULT_INDUSTRY_PACK', 'family_office') or 'family_office'
INTEL_CLASSIFICATION_ENABLED = _env_bool('INTEL_CLASSIFICATION_ENABLED', True)
INTEL_SOURCE_SYNC_ENABLED = _env_bool('INTEL_SOURCE_SYNC_ENABLED', False)
INTEL_SOURCE_SYNC_INTERVAL_HOURS = _env_int('INTEL_SOURCE_SYNC_INTERVAL_HOURS', 24, 1, 168)
INTEL_LIGHT_SCANNER_ENABLED = _env_bool('INTEL_LIGHT_SCANNER_ENABLED', False)
INTEL_CANDIDATE_DISPATCH_ENABLED = _env_bool('INTEL_CANDIDATE_DISPATCH_ENABLED', False)
INTEL_TOPIC_CLUSTER_ENABLED = _env_bool('INTEL_TOPIC_CLUSTER_ENABLED', False)
# BERTopic 动态主题趋势（复用 bge-m3 预计算嵌入聚类 + LLM 命名）
INTEL_BERTOPIC_ENABLED = _env_bool('INTEL_BERTOPIC_ENABLED', False)
INTEL_BERTOPIC_INTERVAL_MINUTES = _env_int('INTEL_BERTOPIC_INTERVAL_MINUTES', 360, 30, 1440)
INTEL_BERTOPIC_MIN_ARTICLES = _env_int('INTEL_BERTOPIC_MIN_ARTICLES', 30, 5, 2000)
INTEL_BERTOPIC_MIN_TOPIC_SIZE = _env_int('INTEL_BERTOPIC_MIN_TOPIC_SIZE', 3, 2, 50)
INTEL_LIGHT_SCAN_INTERVAL_MINUTES = _env_int('INTEL_LIGHT_SCAN_INTERVAL_MINUTES', 15, 1, 1440)
# Hong Kong local wall-clock time for the once-daily discovery scan (HH:MM).
INTEL_LIGHT_SCAN_DAILY_TIME = _env_str('INTEL_LIGHT_SCAN_DAILY_TIME', '08:20') or '08:20'
INTEL_SCAN_MAX_SOURCES_PER_RUN = _env_int('INTEL_SCAN_MAX_SOURCES_PER_RUN', 150, 1, 1000)
INTEL_SCAN_MAX_ITEMS_PER_SOURCE = _env_int('INTEL_SCAN_MAX_ITEMS_PER_SOURCE', 100, 1, 1000)
INTEL_SCAN_CONNECT_TIMEOUT_SECONDS = _env_int('INTEL_SCAN_CONNECT_TIMEOUT_SECONDS', 5, 1, 60)
INTEL_SCAN_READ_TIMEOUT_SECONDS = _env_int('INTEL_SCAN_READ_TIMEOUT_SECONDS', 20, 1, 180)
INTEL_SCAN_MAX_RESPONSE_BYTES = _env_int('INTEL_SCAN_MAX_RESPONSE_BYTES', 5000000, 10000, 50000000)
INTEL_SCAN_MAX_REDIRECTS = _env_int('INTEL_SCAN_MAX_REDIRECTS', 5, 0, 10)
INTEL_PRIVATE_NETWORK_ALLOWLIST = _env_str('INTEL_PRIVATE_NETWORK_ALLOWLIST', '')
INTEL_CANDIDATE_BATCH_SIZE = _env_int('INTEL_CANDIDATE_BATCH_SIZE', 10, 1, 100)
INTEL_CANDIDATE_LEASE_SECONDS = _env_int('INTEL_CANDIDATE_LEASE_SECONDS', 600, 30, 3600)
INTEL_CANDIDATE_MAX_RETRIES = _env_int('INTEL_CANDIDATE_MAX_RETRIES', 3, 0, 10)
INTEL_WORKER_POLL_SECONDS = _env_int('INTEL_WORKER_POLL_SECONDS', 5, 1, 300)
INTEL_WORKER_BATCH_SIZE = _env_int('INTEL_WORKER_BATCH_SIZE', 20, 1, 500)
INTEL_JOB_LEASE_SECONDS = _env_int('INTEL_JOB_LEASE_SECONDS', 300, 30, 3600)
INTEL_JOB_MAX_RETRIES = _env_int('INTEL_JOB_MAX_RETRIES', 3, 0, 10)
INTEL_JOB_PRIORITY_AGING_SECONDS = _env_int(
    'INTEL_JOB_PRIORITY_AGING_SECONDS', 30, 1, 3600
)

SERPAPI_API_KEY = _env_str('SERPAPI_API_KEY', '')
SERPAPI_ENABLED = _env_bool('SERPAPI_ENABLED', False)
SERPAPI_ENGINE = _env_str('SERPAPI_ENGINE', 'google') or 'google'
SERPAPI_DEFAULT_REGION = _env_str('SERPAPI_DEFAULT_REGION', 'hk') or 'hk'
SERPAPI_DEFAULT_LANGUAGE = _env_str('SERPAPI_DEFAULT_LANGUAGE', 'zh-cn') or 'zh-cn'
SERPAPI_TIMEOUT_SECONDS = _env_int('SERPAPI_TIMEOUT_SECONDS', 20, 5, 120)
SERPAPI_MAX_RETRIES = _env_int('SERPAPI_MAX_RETRIES', 2, 0, 5)
SERPAPI_MAX_QUERIES_PER_RUN = _env_int('SERPAPI_MAX_QUERIES_PER_RUN', 6, 1, 100)
SERPAPI_DAILY_QUERY_BUDGET = _env_int('SERPAPI_DAILY_QUERY_BUDGET', 100, 0, 100000)
# Google discovery is incremental.  Restrict each daily query to this rolling
# period; URL de-duplication makes the overlap safe while keeping results fresh.
SERPAPI_RECENCY_DAYS = _env_int('SERPAPI_RECENCY_DAYS', 3, 1, 31)
SERPAPI_RESULT_LANGUAGE = (_env_str('SERPAPI_RESULT_LANGUAGE', 'zh') or 'zh').casefold()
if SERPAPI_RESULT_LANGUAGE not in {'zh', 'en', 'all'}:
    SERPAPI_RESULT_LANGUAGE = 'zh'

# ── 搜索引擎统一入口（SerpAPI / Tavily）────────────────────────────────
# 定位：搜索只负责"发现 URL"，正文仍走现有链路（候选门禁 → 正文抓取 → 分类 → 主题归属），
# 因此搜索结果不会绕过门禁，也不会把摘要当正文入库。
SEARCH_PROVIDER = (_env_str('SEARCH_PROVIDER', 'serpapi') or 'serpapi').strip().casefold()
if SEARCH_PROVIDER not in {'serpapi', 'tavily', 'both'}:
    SEARCH_PROVIDER = 'serpapi'
SEARCH_ENABLED = _env_bool('SEARCH_ENABLED', False)
# 调用时机：每天固定几轮，格式 "HH:MM,HH:MM"。
# 已定：每天 1 轮，08:20。搜索到的 URL 不需要等凌晨批次——生产的候选派发
# （candidate_dispatch，实测约每分钟一次）会很快把它消费掉。
SEARCH_RUNS_PER_DAY = _env_int('SEARCH_RUNS_PER_DAY', 1, 0, 6)
SEARCH_RUN_HOURS = _env_str('SEARCH_RUN_HOURS', '08:20') or '08:20'
# 每轮搜索的行业包数量（每个包取其"关键词精选"最多 3 个，合并成 1 次查询 → 每包 1 次调用）
SEARCH_PACKS_PER_RUN = _env_int('SEARCH_PACKS_PER_RUN', 2, 1, 9)
# 每个行业包的关键词数量上限（3 个用 OR 合并成一次查询，避免 AND 语义导致 0 结果）
SEARCH_KEYWORDS_PER_PACK = _env_int('SEARCH_KEYWORDS_PER_PACK', 3, 1, 10)
# 主题级搜索词：为"文章少的窄主题"单独配查询词（行业包 manifest 里的 topic_search_queries），
# 按轮转分摊到多轮，避免一轮把配额打满。
SEARCH_TOPIC_QUERIES_PER_PACK = _env_int('SEARCH_TOPIC_QUERIES_PER_PACK', 6, 0, 30)
SEARCH_TOPIC_QUERIES_PER_RUN = _env_int('SEARCH_TOPIC_QUERIES_PER_RUN', 3, 0, 20)
SEARCH_TOPIC_QUERY_ROTATION = _env_bool('SEARCH_TOPIC_QUERY_ROTATION', True)
# 合并/分开：**按引擎分别设置**（两个引擎免费额度差 10 倍，不能用同一个开关）
#   separate = 每个关键词各自 1 次调用（每词独占结果，覆盖全，额度 ×3）
#   merged   = 每包 3 个关键词用 OR 合并成 1 次调用（省额度，结果被稀释）
# 已定：Tavily 分开搜（2 包×3 词×1 轮 = 6 次/天 ≈ 180/月，在 ~1000/月 内）
#       SerpAPI 合并搜（2 包×1 次×1 轮 = 2 次/天 ≈ 60/月，在 100/月 内）
TAVILY_QUERY_MODE = (_env_str('TAVILY_QUERY_MODE', 'separate') or 'separate').strip().casefold()
if TAVILY_QUERY_MODE not in {'merged', 'separate'}:
    TAVILY_QUERY_MODE = 'separate'
SERPAPI_QUERY_MODE = (_env_str('SERPAPI_QUERY_MODE', 'merged') or 'merged').strip().casefold()
if SERPAPI_QUERY_MODE not in {'merged', 'separate'}:
    SERPAPI_QUERY_MODE = 'merged'
# 配额上限：**每个引擎各自计数、各自设限**，到顶即跳过（不硬跑），互不拖累。
TAVILY_MAX_CALLS_PER_RUN = _env_int('TAVILY_MAX_CALLS_PER_RUN', 9, 1, 50)
TAVILY_MAX_CALLS_PER_MONTH = _env_int('TAVILY_MAX_CALLS_PER_MONTH', 270, 1, 10000)
SERPAPI_MAX_CALLS_PER_RUN = _env_int('SERPAPI_MAX_CALLS_PER_RUN', 2, 1, 50)
SERPAPI_MAX_CALLS_PER_MONTH = _env_int('SERPAPI_MAX_CALLS_PER_MONTH', 60, 1, 10000)

TAVILY_API_KEY = _env_str('TAVILY_API_KEY', '')
TAVILY_ENABLED = _env_bool('TAVILY_ENABLED', False)
TAVILY_BASE_URL = _env_str('TAVILY_BASE_URL', 'https://api.tavily.com/search') or 'https://api.tavily.com/search'
TAVILY_MAX_RESULTS = _env_int('TAVILY_MAX_RESULTS', 5, 1, 20)
TAVILY_SEARCH_DEPTH = (_env_str('TAVILY_SEARCH_DEPTH', 'basic') or 'basic').strip().casefold()
if TAVILY_SEARCH_DEPTH not in {'basic', 'advanced'}:
    TAVILY_SEARCH_DEPTH = 'basic'
TAVILY_TIMEOUT_SECONDS = _env_int('TAVILY_TIMEOUT_SECONDS', 20, 5, 120)
TAVILY_MAX_RETRIES = _env_int('TAVILY_MAX_RETRIES', 1, 0, 5)

# 财经新闻刷新：默认关闭；即使开启也只提示"高级版本目前不支持"，不产生任何外部调用。
FINANCIAL_NEWS_REFRESH_ENABLED = _env_bool('FINANCIAL_NEWS_REFRESH_ENABLED', False)

# 候选派发频率（聚合调度的总控，仅 admin 可改）
#   CONTINUOUS=true  → 保持现状：每分钟派发一次（随时在派发）
#   CONTINUOUS=false → 只在 DISPATCH_TIMES 指定的时间点各派发一次（定点，低频）
# 无论哪种模式，以下事件触发仍然保留：搜索采集完成后、信源同步后、页面手动触发
#   —— 否则会发现"信源新增了 URL 却一直不抓"的情况。
INTEL_CANDIDATE_DISPATCH_CONTINUOUS = _env_bool('INTEL_CANDIDATE_DISPATCH_CONTINUOUS', True)
INTEL_CANDIDATE_DISPATCH_TIMES = _env_str('INTEL_CANDIDATE_DISPATCH_TIMES', '') or ''

# The radar can reuse the homepage AI assistant's configured local model.
# "ragflow" remains available for installations that already use a RAGFlow chat app.
INTEL_LLM_ENABLED = _env_bool('INTEL_LLM_ENABLED', False)
INTEL_LLM_PROVIDER = (
    _env_str('INTEL_LLM_PROVIDER', 'local') or 'local'
).casefold()
if INTEL_LLM_PROVIDER not in {'local', 'ragflow'}:
    INTEL_LLM_PROVIDER = 'local'
INTEL_LLM_CHAT_MODEL_ID = (
    _env_str('INTEL_LLM_CHAT_MODEL_ID', 'local') or 'local'
).casefold()
INTEL_LLM_TIMEOUT_SECONDS = _env_int('INTEL_LLM_TIMEOUT_SECONDS', 90, 5, 600)
INTEL_LLM_MAX_RETRIES = _env_int('INTEL_LLM_MAX_RETRIES', 1, 0, 5)
INTEL_LLM_MAX_INPUT_CHARS = _env_int(
    'INTEL_LLM_MAX_INPUT_CHARS',
    12000,
    1000,
    200000,
)
INTEL_LLM_MAX_CONCURRENCY = _env_int('INTEL_LLM_MAX_CONCURRENCY', 2, 1, 20)
INTEL_LLM_INTERACTIVE_RESERVED = _env_int(
    'INTEL_LLM_INTERACTIVE_RESERVED', 1, 0, 19
)

RAGFLOW_LLM_ENABLED = _env_bool('RAGFLOW_LLM_ENABLED', False)
RAGFLOW_LLM_MODEL_ID = _env_str('RAGFLOW_LLM_MODEL_ID', '')
RAGFLOW_LLM_APP_ID = _env_str('RAGFLOW_LLM_APP_ID', '')
RAGFLOW_LLM_TIMEOUT_SECONDS = _env_int('RAGFLOW_LLM_TIMEOUT_SECONDS', 45, 5, 600)
RAGFLOW_LLM_MAX_RETRIES = _env_int('RAGFLOW_LLM_MAX_RETRIES', 1, 0, 5)
RAGFLOW_LLM_MAX_INPUT_CHARS = _env_int('RAGFLOW_LLM_MAX_INPUT_CHARS', 20000, 1000, 200000)
RAGFLOW_LLM_MAX_CONCURRENCY = _env_int('RAGFLOW_LLM_MAX_CONCURRENCY', 2, 1, 20)
INTEL_TOPIC_CLUSTER_INTERVAL_MINUTES = _env_int('INTEL_TOPIC_CLUSTER_INTERVAL_MINUTES', 60, 5, 1440)
INTEL_TOPIC_SUMMARY_MIN_ARTICLES = _env_int('INTEL_TOPIC_SUMMARY_MIN_ARTICLES', 2, 1, 100)

# ---- 行业演化趋势（第一阶段 baseline）+ 文章向量 Embedding（为第三阶段铺路）----
INTEL_TREND_AGGREGATE_ENABLED = _env_bool('INTEL_TREND_AGGREGATE_ENABLED', False)
INTEL_TREND_AGGREGATE_INTERVAL_MINUTES = _env_int('INTEL_TREND_AGGREGATE_INTERVAL_MINUTES', 360, 30, 1440)
INTEL_TREND_WINDOW_DAYS = _env_int('INTEL_TREND_WINDOW_DAYS', 180, 14, 730)
INTEL_TREND_BURST_WINDOW = _env_int('INTEL_TREND_BURST_WINDOW', 7, 3, 30)
INTEL_EMBEDDING_ENABLED = _env_bool('INTEL_EMBEDDING_ENABLED', False)
INTEL_EMBEDDING_BASE_URL = _env_str('INTEL_EMBEDDING_BASE_URL', 'http://192.168.0.64:9997')
INTEL_EMBEDDING_MODEL = _env_str('INTEL_EMBEDDING_MODEL', 'bge-m3') or 'bge-m3'
INTEL_EMBEDDING_BATCH_SIZE = _env_int('INTEL_EMBEDDING_BATCH_SIZE', 16, 1, 64)
INTEL_EMBEDDING_INTERVAL_MINUTES = _env_int('INTEL_EMBEDDING_INTERVAL_MINUTES', 60, 10, 1440)
INTEL_EMBEDDING_MAX_ARTICLES_PER_RUN = _env_int('INTEL_EMBEDDING_MAX_ARTICLES_PER_RUN', 200, 1, 5000)
INTEL_EMBEDDING_TIMEOUT_SECONDS = _env_int('INTEL_EMBEDDING_TIMEOUT_SECONDS', 30, 5, 120)
INTEL_EMBEDDING_MAX_INPUT_CHARS = _env_int('INTEL_EMBEDDING_MAX_INPUT_CHARS', 4000, 500, 16000)

# ---- 聊天检索语义增强：关键词命中弱时用 bge-m3 语义召回补充（依赖 INTEL_EMBEDDING 服务）----
# 打开后：关键词打分低于阈值（弱命中）的问题，追加一次语义检索（余弦 top-K）补充候选文章；
# 语义服务不可用/超时自动降级为纯关键词 + 最新文章兜底，不影响聊天可用性。
INTEL_CHAT_SEMANTIC_ENABLED = _env_bool('INTEL_CHAT_SEMANTIC_ENABLED', True)
# ---- 聊天检索一级门禁：身份/寒暄等无需文章库的问题直接跳过聚合库检索，
# 不注入文章、不下发 retrieval 事件（前端不显示"本次回答参考库内文章"）----
INTEL_CHAT_RETRIEVAL_GATE_ENABLED = _env_bool('INTEL_CHAT_RETRIEVAL_GATE_ENABLED', True)
INTEL_CHAT_SEMANTIC_TIMEOUT_SECONDS = _env_int('INTEL_CHAT_SEMANTIC_TIMEOUT_SECONDS', 25, 5, 180)
# 弱命中判定：最好文章覆盖的问题 bigram 不足 50% 视为弱命中（相对），
# 且绝对命中数 ≤ 阈值（2）也视为弱命中（兜底）
INTEL_CHAT_SEMANTIC_WEAK_PERCENT = _env_int('INTEL_CHAT_SEMANTIC_WEAK_PERCENT', 50, 10, 100)
INTEL_CHAT_SEMANTIC_WEAK_THRESHOLD = _env_int('INTEL_CHAT_SEMANTIC_WEAK_THRESHOLD', 2, 0, 10)
INTEL_CHAT_SEMANTIC_TOP_K = _env_int('INTEL_CHAT_SEMANTIC_TOP_K', 8, 1, 20)
# embedding 请求的 keep_alive（Ollama 专用：bge-m3 常驻显存，热调用 ~2s 而非冷加载 ~13s；
# 非 Ollama 服务可留空）
INTEL_EMBEDDING_KEEP_ALIVE = _env_str('INTEL_EMBEDDING_KEEP_ALIVE', '')

# ---- 事件抽取（第二阶段：LLM 抽结构化事件 + event_hash 聚类）----
INTEL_EVENT_EXTRACT_ENABLED = _env_bool('INTEL_EVENT_EXTRACT_ENABLED', False)
INTEL_EVENT_EXTRACT_INTERVAL_MINUTES = _env_int('INTEL_EVENT_EXTRACT_INTERVAL_MINUTES', 720, 60, 1440)
INTEL_EVENT_EXTRACT_MAX_ARTICLES_PER_RUN = _env_int('INTEL_EVENT_EXTRACT_MAX_ARTICLES_PER_RUN', 20, 1, 200)
INTEL_LLM_EVENT_TIMEOUT_SECONDS = _env_int('INTEL_LLM_EVENT_TIMEOUT_SECONDS', 60, 10, 300)
INTEL_LLM_EVENT_MAX_INPUT_CHARS = _env_int('INTEL_LLM_EVENT_MAX_INPUT_CHARS', 8000, 1000, 50000)
INTEL_EVENT_TREND_WINDOW_DAYS = _env_int('INTEL_EVENT_TREND_WINDOW_DAYS', 90, 14, 730)
INTEL_EVENT_TREND_BURST_WINDOW = _env_int('INTEL_EVENT_TREND_BURST_WINDOW', 7, 3, 30)

# ---- 主体话题归并（T5：嵌入聚类合并同主体不同写法）----
INTEL_SUBJECT_NORMALIZE_ENABLED = _env_bool('INTEL_SUBJECT_NORMALIZE_ENABLED', False)
INTEL_SUBJECT_NORMALIZE_INTERVAL_MINUTES = _env_int('INTEL_SUBJECT_NORMALIZE_INTERVAL_MINUTES', 360, 60, 1440)
INTEL_SUBJECT_MERGE_THRESHOLD = _env_float('INTEL_SUBJECT_MERGE_THRESHOLD', 0.62, 0.3, 0.95)

# ==================== 金融研究与 TradingAgents 配置 ====================
# Every capability is opt-in.  Child switches may be stored while the parent
# is off, but effective capability checks always require the parent chain.
FINANCIAL_LICENSE_ENVIRONMENT = (
    _env_str('FINANCIAL_LICENSE_ENVIRONMENT', 'development') or 'development'
).casefold()
FINANCIAL_SOURCE_LICENSE_APPROVALS = (
    _env_str('FINANCIAL_SOURCE_LICENSE_APPROVALS', '') or ''
)
FINANCIAL_INTELLIGENCE_ENABLED = _env_bool('FINANCIAL_INTELLIGENCE_ENABLED', False)
# 金融总开关开启后默认启用多标签需求规划；仍可独立关闭以快速回退。
FINANCIAL_INFORMATION_NEEDS_ENABLED = _env_bool(
    'FINANCIAL_INFORMATION_NEEDS_ENABLED', True
)
# 未知证券先进入有来源审计的候选区；正式证券自动准入可单独关闭。
FINANCIAL_INSTRUMENT_DISCOVERY_ENABLED = _env_bool(
    'FINANCIAL_INSTRUMENT_DISCOVERY_ENABLED', True
)
FINANCIAL_EXTERNAL_INSTRUMENT_DISCOVERY_ENABLED = _env_bool(
    'FINANCIAL_EXTERNAL_INSTRUMENT_DISCOVERY_ENABLED', True
)
FINANCIAL_INSTRUMENT_SEARCH_FALLBACK_ENABLED = _env_bool(
    'FINANCIAL_INSTRUMENT_SEARCH_FALLBACK_ENABLED', True
)
FINANCIAL_INSTRUMENT_AUTO_PROMOTION_ENABLED = _env_bool(
    'FINANCIAL_INSTRUMENT_AUTO_PROMOTION_ENABLED', True
)
# “最新新闻”和“行情 + 新闻”组合可分别回滚；两者始终受金融总开关约束。
FINANCIAL_LATEST_NEWS_ENABLED = _env_bool(
    'FINANCIAL_LATEST_NEWS_ENABLED', True
)
FINANCIAL_LATEST_BUNDLE_ENABLED = _env_bool(
    'FINANCIAL_LATEST_BUNDLE_ENABLED', True
)
TRADING_AGENTS_ENABLED = _env_bool('TRADING_AGENTS_ENABLED', False)
FINANCIAL_AUTO_RESEARCH_ENABLED = _env_bool('FINANCIAL_AUTO_RESEARCH_ENABLED', False)
TRADING_SIMULATION_ENABLED = _env_bool('TRADING_SIMULATION_ENABLED', False)
# A missing stage preserves the behavior of an existing upgraded deployment;
# new managed installations write ``off`` explicitly and advance one layer at
# a time through the configuration API.
FINANCIAL_ROLLOUT_STAGE = _env_str(
    'FINANCIAL_ROLLOUT_STAGE', 'simulation_backtest'
)
FINANCIAL_ROLLOUT_STAGE = (
    FINANCIAL_ROLLOUT_STAGE or 'simulation_backtest'
).strip().casefold()
FINANCIAL_ROLLOUT_STAGE_CHANGED_AT = _env_str(
    'FINANCIAL_ROLLOUT_STAGE_CHANGED_AT', ''
) or ''
FINANCIAL_ROLLOUT_OBSERVATION_SECONDS = _env_int(
    'FINANCIAL_ROLLOUT_OBSERVATION_SECONDS', 3600, 60, 604800
)
AKSHARE_CN_ENABLED = _env_bool('AKSHARE_CN_ENABLED', False)
TUSHARE_CN_ENABLED = _env_bool('TUSHARE_CN_ENABLED', False)
TUSHARE_TOKEN = _env_str('TUSHARE_TOKEN', '') or ''
YAHOO_FINANCE_ENABLED = _env_bool('YAHOO_FINANCE_ENABLED', False)
ALPHA_VANTAGE_ENABLED = _env_bool('ALPHA_VANTAGE_ENABLED', False)
ALPHA_VANTAGE_API_KEY = _env_str('ALPHA_VANTAGE_API_KEY', '') or ''
ALPHA_VANTAGE_REALTIME_ENTITLED = _env_bool('ALPHA_VANTAGE_REALTIME_ENTITLED', False)
ALPHA_VANTAGE_QUOTE_ENTITLEMENT = (
    _env_str('ALPHA_VANTAGE_QUOTE_ENTITLEMENT', 'none') or 'none'
).casefold()
if ALPHA_VANTAGE_QUOTE_ENTITLEMENT not in {'none', 'delayed', 'realtime'}:
    ALPHA_VANTAGE_QUOTE_ENTITLEMENT = 'none'
FRED_ENABLED = _env_bool('FRED_ENABLED', False)
FRED_API_KEY = _env_str('FRED_API_KEY', '') or ''
POLYMARKET_ENABLED = _env_bool('POLYMARKET_ENABLED', False)
EASYQUOTATION_ENABLED = _env_bool('EASYQUOTATION_ENABLED', False)
OFFICIAL_FINANCIAL_EVIDENCE_ENABLED = _env_bool(
    'OFFICIAL_FINANCIAL_EVIDENCE_ENABLED', False
)

FINANCIAL_PROVIDER_TIMEOUT_SECONDS = _env_int(
    'FINANCIAL_PROVIDER_TIMEOUT_SECONDS', 20, 5, 120
)
FINANCIAL_PROVIDER_MAX_RETRIES = _env_int(
    'FINANCIAL_PROVIDER_MAX_RETRIES', 2, 0, 5
)
FINANCIAL_PROVIDER_MAX_CONCURRENCY = _env_int(
    'FINANCIAL_PROVIDER_MAX_CONCURRENCY', 4, 1, 16
)
FINANCIAL_PROVIDER_MAX_CONCURRENCY_PER_SOURCE = _env_int(
    'FINANCIAL_PROVIDER_MAX_CONCURRENCY_PER_SOURCE', 2, 1, 8
)
FINANCIAL_PROVIDER_ADMISSION_TIMEOUT_SECONDS = _env_int(
    'FINANCIAL_PROVIDER_ADMISSION_TIMEOUT_SECONDS', 5, 1, 60
)
FINANCIAL_ARTIFACT_MAX_BYTES = _env_int(
    'FINANCIAL_ARTIFACT_MAX_BYTES', 8 * 1024 * 1024, 1024, 64 * 1024 * 1024
)
FINANCIAL_ARTIFACT_IO_MAX_CONCURRENCY = _env_int(
    'FINANCIAL_ARTIFACT_IO_MAX_CONCURRENCY', 1, 1, 4
)
FINANCIAL_ARTIFACT_IO_TIMEOUT_SECONDS = _env_int(
    'FINANCIAL_ARTIFACT_IO_TIMEOUT_SECONDS', 5, 1, 60
)
FINANCIAL_INTERACTIVE_P95_BUDGET_MS = _env_int(
    'FINANCIAL_INTERACTIVE_P95_BUDGET_MS', 500, 50, 10000
)
FINANCIAL_DASHBOARD_P95_BUDGET_MS = _env_int(
    'FINANCIAL_DASHBOARD_P95_BUDGET_MS', 250, 25, 5000
)
FINANCIAL_RSS_P95_BUDGET_MS = _env_int(
    'FINANCIAL_RSS_P95_BUDGET_MS', 1000, 50, 10000
)
FINANCIAL_HEALTH_PROVIDER_MAX_AGE_SECONDS = _env_int(
    'FINANCIAL_HEALTH_PROVIDER_MAX_AGE_SECONDS', 86400, 60, 604800
)
FINANCIAL_HEALTH_SOURCE_MAX_AGE_SECONDS = _env_int(
    'FINANCIAL_HEALTH_SOURCE_MAX_AGE_SECONDS', 86400, 60, 604800
)
FINANCIAL_HEALTH_JOB_MAX_AGE_SECONDS = _env_int(
    'FINANCIAL_HEALTH_JOB_MAX_AGE_SECONDS', 900, 30, 86400
)
FINANCIAL_HEALTH_LLM_P95_MS = _env_int(
    'FINANCIAL_HEALTH_LLM_P95_MS', 90000, 1000, 600000
)
FINANCIAL_HEALTH_REPORT_MAX_AGE_SECONDS = _env_int(
    'FINANCIAL_HEALTH_REPORT_MAX_AGE_SECONDS', 86400, 300, 604800
)

# SQLite writers fail in a bounded interval instead of holding request/worker
# threads for the historical 30-second driver default. WAL keeps Dashboard
# reads available while a short worker write is in progress.
SQLITE_BUSY_TIMEOUT_MS = _env_int('SQLITE_BUSY_TIMEOUT_MS', 2000, 100, 30000)
FINANCIAL_PROVIDER_DAILY_CALL_BUDGET = _env_int(
    'FINANCIAL_PROVIDER_DAILY_CALL_BUDGET', 1000, 0, 100000
)
FINANCIAL_QUOTE_FRESHNESS_SECONDS = _env_int(
    'FINANCIAL_QUOTE_FRESHNESS_SECONDS', 300, 15, 3600
)
FINANCIAL_LATEST_QUOTE_TIMEOUT_SECONDS = _env_float(
    'FINANCIAL_LATEST_QUOTE_TIMEOUT_SECONDS', 6.0, 0.1, 120.0
)
FINANCIAL_LATEST_NEWS_TIMEOUT_SECONDS = _env_float(
    'FINANCIAL_LATEST_NEWS_TIMEOUT_SECONDS', 40.0, 0.1, 120.0
)
FINANCIAL_LATEST_BUNDLE_TIMEOUT_SECONDS = _env_float(
    'FINANCIAL_LATEST_BUNDLE_TIMEOUT_SECONDS', 45.0, 0.1, 180.0
)
FINANCIAL_MARKET_BREADTH_FRESHNESS_SECONDS = _env_int(
    'FINANCIAL_MARKET_BREADTH_FRESHNESS_SECONDS', 300, 60, 7200
)
FINANCIAL_NEWS_FRESHNESS_SECONDS = _env_int(
    'FINANCIAL_NEWS_FRESHNESS_SECONDS', 3600, 60, 86400
)
FINANCIAL_NEWS_LOOKBACK_DAYS = _env_int(
    'FINANCIAL_NEWS_LOOKBACK_DAYS', 7, 1, 30
)
FINANCIAL_NEWS_LATEST_FALLBACK_SCAN_LIMIT = _env_int(
    'FINANCIAL_NEWS_LATEST_FALLBACK_SCAN_LIMIT', 5000, 500, 20000
)
FINANCIAL_NEWS_REFRESH_MAX_SOURCES = _env_int(
    'FINANCIAL_NEWS_REFRESH_MAX_SOURCES', 5, 1, 8
)
FINANCIAL_NEWS_REFRESH_ITEMS_PER_SOURCE = _env_int(
    'FINANCIAL_NEWS_REFRESH_ITEMS_PER_SOURCE', 20, 1, 50
)
FINANCIAL_NEWS_REFRESH_TIMEOUT_SECONDS = _env_float(
    'FINANCIAL_NEWS_REFRESH_TIMEOUT_SECONDS', 10.0, 1.0, 30.0
)
FINANCIAL_NEWS_DISCOVERY_MAX_SOURCES = _env_int(
    'FINANCIAL_NEWS_DISCOVERY_MAX_SOURCES', 4, 1, 8
)
FINANCIAL_NEWS_DISCOVERY_MAX_CANDIDATES = _env_int(
    'FINANCIAL_NEWS_DISCOVERY_MAX_CANDIDATES', 12, 1, 25
)
FINANCIAL_FUNDAMENTAL_FRESHNESS_SECONDS = _env_int(
    'FINANCIAL_FUNDAMENTAL_FRESHNESS_SECONDS', 86400, 3600, 31536000
)
FINANCIAL_RESEARCH_MAX_LLM_CALLS = _env_int(
    'FINANCIAL_RESEARCH_MAX_LLM_CALLS', 30, 1, 200
)
FINANCIAL_RESEARCH_MAX_TOKENS = _env_int(
    'FINANCIAL_RESEARCH_MAX_TOKENS', 120000, 1000, 1000000
)
FINANCIAL_RESEARCH_MAX_DEBATE_ROUNDS = _env_int(
    'FINANCIAL_RESEARCH_MAX_DEBATE_ROUNDS', 2, 1, 10
)
FINANCIAL_RESEARCH_TIMEOUT_SECONDS = _env_int(
    'FINANCIAL_RESEARCH_TIMEOUT_SECONDS', 1800, 60, 7200
)
FINANCIAL_RESEARCH_CACHE_SECONDS = _env_int(
    'FINANCIAL_RESEARCH_CACHE_SECONDS', 3600, 60, 86400
)
FINANCIAL_AUTO_RESEARCH_DAILY_BUDGET = _env_int(
    'FINANCIAL_AUTO_RESEARCH_DAILY_BUDGET', 6, 0, 100
)
FINANCIAL_RAGFLOW_KB_ID = os.getenv('FINANCIAL_RAGFLOW_KB_ID', '').strip()


def is_ragflow_configured():
    return bool(RAGFLOW_BASE_URL and RAGFLOW_API_KEY)

# ==================== Redis配置 ====================
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_DB = int(os.getenv('REDIS_DB', 1))
REDIS_PASSWORD = os.getenv('REDIS_PASSWORD', None)

# ==================== 数据库配置 ====================
# primary=postgres 表示应用运行时直接读写 PostgreSQL；sqlite 仅作为备份/回退。
DATABASE_TYPE = (os.getenv('DATABASE_TYPE', 'sqlite') or 'sqlite').strip().lower()
# 回退开关：true 时 PG 连不上会静默改用 SQLite 文件库，方言差异（boolean/json/主键类型）
# 会在本机被掩盖、到生产才暴露。缺省改为 false，让连不上就明确失败，而不是悄悄换库。
DATABASE_FALLBACK_TO_SQLITE = os.getenv('DATABASE_FALLBACK_TO_SQLITE', 'false').strip().lower() in ('1', 'true', 'yes', 'on')
POSTGRES_HOST = os.getenv('POSTGRES_HOST', '127.0.0.1').strip()
POSTGRES_PORT = int(os.getenv('POSTGRES_PORT', '5432'))
POSTGRES_DB = os.getenv('POSTGRES_DB', 'collectinfo').strip()
POSTGRES_USER = os.getenv('POSTGRES_USER', 'postgres').strip()
POSTGRES_PASSWORD = os.getenv('POSTGRES_PASSWORD', '')
SQLITE_BACKUP_PATH = os.getenv('SQLITE_BACKUP_PATH', 'data/crawler_articles.db').strip()
DATABASE_PATH = os.getenv('DATABASE_PATH', 'crawler_articles.db')

# ==================== 存储目录配置 ====================
CRAWL_RESULTS_DIR = os.getenv('CRAWL_RESULTS_DIR', 'crawl_results')
AUTH_STORAGE_DIR = os.getenv('AUTH_STORAGE_DIR', 'auth_storage')
SCREENSHOT_DIR = os.path.join(AUTH_STORAGE_DIR, 'screenshots')

# ==================== Flask应用配置 ====================
FLASK_HOST = os.getenv('FLASK_HOST', '0.0.0.0')
FLASK_PORT = int(os.getenv('FLASK_PORT', 8003))
FLASK_DEBUG = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
SECRET_KEY = os.getenv('SECRET_KEY', None)  # 生产环境必须设置
PERMANENT_SESSION_LIFETIME = int(os.getenv('SESSION_LIFETIME', 86400))  # 24小时

# ==================== 认证监控配置 ====================
AUTH_CHECK_INTERVAL = int(os.getenv('AUTH_CHECK_INTERVAL', 3600))  # 1小时

# ==================== 聚合配置 ====================
DEFAULT_USER_AGENT = os.getenv('USER_AGENT', 
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
DEFAULT_TIMEOUT = int(os.getenv('CRAWL_TIMEOUT', 30))  # 30秒
DEFAULT_WAIT_TIME = int(os.getenv('CRAWL_WAIT_TIME', 3))  # 3秒

# ==================== 代理配置 ====================
# 是否启用代理（设置为 'true' 启用）。生产默认直连，避免客户环境误连内网代理。
PROXY_ENABLED = _env_bool('PROXY_ENABLED', False)

# 域名黑名单：这些域的候选一律不建（境内服务器不可达/DNS 污染的境外源等）。
# 逗号分隔，例如 CRAWL_DENY_DOMAINS=cn.investing.com,www.investing.com
CRAWL_DENY_DOMAINS = tuple(
    domain.strip().casefold()
    for domain in (_env_str('CRAWL_DENY_DOMAINS', '') or '').split(',')
    if domain.strip()
)

# HTTP代理地址，例如 http://127.0.0.1:7890
PROXY_HTTP = _env_str('PROXY_HTTP')

# HTTPS代理地址
PROXY_HTTPS = _env_str('PROXY_HTTPS')

# SOCKS5代理地址（例如：socks5://127.0.0.1:7891）
PROXY_SOCKS5 = _env_str('PROXY_SOCKS5')

# Playwright代理地址（用于浏览器），例如 http://127.0.0.1:7890
PLAYWRIGHT_PROXY = _env_str('PLAYWRIGHT_PROXY')

def has_proxy_configured():
    """Return whether any public-site proxy endpoint is configured."""
    return bool(PROXY_HTTP or PROXY_HTTPS or PROXY_SOCKS5 or PLAYWRIGHT_PROXY)


def get_proxies(enabled=None):
    """
    获取代理配置（用于requests库）
    用户级隔离：包用户设了自己代理→用其代理；包用户没设→直连(不用全局)；无活跃用户(管理员/系统)→全局。
    Returns:
        dict: 代理配置字典，如果未启用则返回None
    """
    has_user = False; user_proxy = ''
    try:
        from pack_tenant import current_pack_user_id, get_user_settings
        uid = current_pack_user_id()
        if uid:
            has_user = True
            user_proxy = str(get_user_settings(uid).get('proxy_http') or '').strip()
    except Exception:
        pass
    if has_user:
        if user_proxy:
            return {'http': user_proxy, 'https': user_proxy}
        return None  # 用户没设代理 → 直连（隔离，不用全局）

    if enabled is None:
        enabled = PROXY_ENABLED
    if not enabled:
        return None

    proxies = {}
    if PROXY_HTTP:
        proxies['http'] = PROXY_HTTP
    if PROXY_HTTPS:
        proxies['https'] = PROXY_HTTPS
    if PROXY_SOCKS5 and not proxies:
        proxies['http'] = PROXY_SOCKS5
        proxies['https'] = PROXY_SOCKS5
    return proxies if proxies else None


def get_requests_proxy(enabled=None):
    """
    获取单个代理地址（兼容旧代码）。

    新代码优先使用 get_proxies()，旧代码如果只接受字符串则使用该函数。
    """
    proxies = get_proxies(enabled=enabled)
    if not proxies:
        return None
    return proxies.get('https') or proxies.get('http')


def get_playwright_proxy(enabled=None):
    """
    获取Playwright代理配置
    
    Returns:
        dict: Playwright代理配置字典，如果未启用则返回None
        格式: {"server": "http://127.0.0.1:7890"}
    """
    if enabled is None:
        enabled = PROXY_ENABLED

    if not enabled:
        return None
    
    if PLAYWRIGHT_PROXY:
        return {"server": PLAYWRIGHT_PROXY}
    
    # 回退到HTTP代理
    if PROXY_HTTP:
        return {"server": PROXY_HTTP}
    
    return None


def get_ragflow_proxies():
    """
    获取RAGFlow专用代理配置。

    默认不对RAGFlow使用代理，因为RAGFlow常部署在内网；如需代理，显式设置
    RAGFLOW_PROXY_ENABLED=true，并可单独配置 RAGFLOW_PROXY_HTTP/HTTPS/SOCKS5。
    """
    if not RAGFLOW_PROXY_ENABLED:
        return None

    proxies = {}
    http_proxy = RAGFLOW_PROXY_HTTP or PROXY_HTTP
    https_proxy = RAGFLOW_PROXY_HTTPS or PROXY_HTTPS
    socks_proxy = RAGFLOW_PROXY_SOCKS5 or PROXY_SOCKS5

    if http_proxy:
        proxies['http'] = http_proxy
    if https_proxy:
        proxies['https'] = https_proxy
    if socks_proxy and not proxies:
        proxies['http'] = socks_proxy
        proxies['https'] = socks_proxy

    return proxies if proxies else None

# ==================== 日志配置 ====================
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
LOG_FILE = os.getenv('LOG_FILE', 'app.log')

# ==================== 创建必要的目录 ====================
def ensure_directories():
    """确保所有必要的目录存在"""
    directories = [
        CRAWL_RESULTS_DIR,
        AUTH_STORAGE_DIR,
        SCREENSHOT_DIR,
    ]
    
    for directory in directories:
        if not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)
            print(f"[OK] created directory: {directory}")

# 初始化时创建目录
ensure_directories()


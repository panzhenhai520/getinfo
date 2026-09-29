from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from flask import Flask, Response, abort, jsonify, request, send_file


def _bool_env(name: str, default: bool) -> bool:
    value = str(os.getenv(name, "true" if default else "false")).strip().lower()
    return value in {"1", "true", "yes", "on"}


DATA_DIR = Path(os.getenv("PIPELINE_DATA_DIR", "./data")).resolve()
ARTIFACT_DIR = DATA_DIR / "artifacts"
DB_PATH = DATA_DIR / "pipeline_jobs.db"
API_TOKEN = os.getenv("PIPELINE_API_TOKEN", "")
# 并行度：source 级爬取、LLM(提炼/翻译)、语音 相互独立，构成多条队列。
CRAWL_MAX_WORKERS = max(1, min(8, int(os.getenv("PIPELINE_CRAWL_WORKERS", "3"))))
LLM_MAX_WORKERS = max(1, min(8, int(os.getenv("PIPELINE_LLM_WORKERS", "4"))))
TTS_MAX_WORKERS = max(1, min(8, int(os.getenv("PIPELINE_TTS_WORKERS", "2"))))
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3-coder:latest")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "120"))
OLLAMA_MAX_INPUT = int(os.getenv("OLLAMA_MAX_INPUT_CHARS", "24000"))
# ===================== LLM 后端路由 =====================
# 文章解析/总结的大模型不写死某个引擎：ollama / llama.cpp / vLLM / shim 都可能是
# 当前加载模型的推理后端。启动后按顺序探测，选择「有常驻模型」的后端调用；
# 调用失败自动降级到下一个后端（failover），每 30 秒重新评估一次。
# 环境变量可覆盖：
#   LLM_BACKENDS_JSON = [{"name","api","url","model"}, ...]（api: ollama|openai）
# 默认候选覆盖 VPN 主机常见布局：shim(→llama.cpp 27B)、llama.cpp、vLLM(8B no-thinking)、ollama。
def _llm_backend_defaults() -> list[dict[str, str]]:
    try:
        raw = os.getenv("LLM_BACKENDS_JSON", "").strip()
        if raw:
            import json as _json
            parsed = _json.loads(raw)
            if isinstance(parsed, list) and parsed:
                out = []
                for item in parsed:
                    if isinstance(item, dict) and item.get("url"):
                        out.append({
                            "name": str(item.get("name") or "backend"),
                            "api": str(item.get("api") or "openai").casefold(),
                            "url": str(item["url"]).rstrip("/"),
                            "model": str(item.get("model") or OLLAMA_MODEL),
                        })
                return out
    except Exception:
        pass
    return [
        {"name": "shim", "api": "ollama",
         "url": os.getenv("LLM_SHIM_URL", "http://10.88.0.1:11435"),
         "model": os.getenv("LLM_SHIM_MODEL", "qwen3.8-27b-uncensored")},
        {"name": "llamacpp", "api": "openai",
         "url": os.getenv("LLM_LLAMACPP_URL", "http://10.88.0.1:8081"),
         "model": os.getenv("LLM_LLAMACPP_MODEL", "qwen3.8-27b-uncensored")},
        {"name": "vllm", "api": "openai",
         "url": os.getenv("LLM_VLLM_URL", "http://10.88.0.1:8008"),
         "model": os.getenv("LLM_VLLM_MODEL", "qwen3-8b")},
        {"name": "ollama", "api": "ollama",
         "url": OLLAMA_BASE_URL,
         "model": OLLAMA_MODEL},
    ]


_llm_state = {
    "backends": _llm_backend_defaults(),
    "active": None,          # 当前选中的后端（dict）
    "last_probe": 0.0,
    "lock": threading.Lock(),
}


def _probe_llm_backend(b: dict[str, str]) -> bool:
    """后端是否有可用模型：ollama 看 /api/ps 常驻模型；openai 看 /v1/models 非空。"""
    try:
        if b["api"] == "ollama":
            r = requests.get(f"{b['url']}/api/ps", timeout=3)
            if not r.ok:
                return False
            models = (r.json() or {}).get("models") or []
            return bool(models)
        r = requests.get(f"{b['url']}/v1/models", timeout=3)
        if not r.ok:
            return False
        data = (r.json() or {}).get("data") or []
        return bool(data)
    except Exception:
        return False


def pick_llm_backend(force: bool = False) -> dict[str, str]:
    """选择当前 LLM 后端：优先「有常驻模型」的；否则第一个可达的；全挂时用第一个（请求再失败）。"""
    now = time.time()
    with _llm_state["lock"]:
        if not force and _llm_state["active"] and now - _llm_state["last_probe"] < 30:
            return _llm_state["active"]
        resident = None
        reachable = None
        for b in _llm_state["backends"]:
            if _probe_llm_backend(b):
                if reachable is None:
                    reachable = b
                if b["api"] == "ollama":
                    # ollama 型：/api/ps 有模型才是常驻
                    try:
                        r = requests.get(f"{b['url']}/api/ps", timeout=3)
                        if r.ok and (r.json() or {}).get("models"):
                            resident = b
                            break
                    except Exception:
                        pass
                else:
                    # openai 型：有模型列表即可用
                    resident = b
                    break
        _llm_state["active"] = resident or reachable or _llm_state["backends"][0]
        _llm_state["last_probe"] = now
        return _llm_state["active"]


def _llm_request_json(b: dict[str, str], messages: list, max_tokens: int) -> str:
    """按后端类型构造请求并返回 message content 字符串。无条件关闭思维链。"""
    if b["api"] == "ollama":
        payload = {
            "model": b["model"], "stream": False, "think": False, "format": "json",
            "messages": messages,
            "options": {"temperature": 0, "num_predict": max_tokens, "think": False},
        }
        resp = requests.post(f"{b['url']}/api/chat", json=payload, timeout=OLLAMA_TIMEOUT)
        resp.raise_for_status()
        return str((resp.json() or {}).get("message", {}).get("content") or "")
    payload = {
        "model": b["model"], "messages": messages, "stream": False,
        "max_tokens": max_tokens, "temperature": 0.0,
        # 无条件关闭思维链：openai 兼容端点（llama.cpp/vLLM）忽略未知字段不报错
        "enable_thinking": False, "think": False,
    }
    resp = requests.post(f"{b['url']}/v1/chat/completions", json=payload, timeout=OLLAMA_TIMEOUT)
    resp.raise_for_status()
    choices = (resp.json() or {}).get("choices") or []
    return str((choices[0].get("message") or {}).get("content") or "") if choices else ""


def _llm_json(messages: list, max_tokens: int = 1800) -> dict:
    """LLM JSON 调用（带 failover）：依次尝试各后端，成功后缓存选择。"""
    order = [pick_llm_backend()] + [b for b in _llm_state["backends"] if b is not pick_llm_backend()]
    last_error = None
    for b in order:
        try:
            content = _llm_request_json(b, messages, max_tokens)
            with _llm_state["lock"]:
                _llm_state["active"] = b
                _llm_state["last_probe"] = time.time()
            parsed = _extract_json_object(content)
            if not isinstance(parsed, dict):
                raise ValueError("LLM returned a non-object JSON value")
            return parsed
        except Exception as exc:
            last_error = exc
            continue
    raise ValueError(f"所有 LLM 后端调用失败: {str(last_error)[:200]}")
TTS_BASE_URL = os.getenv("TTS_BASE_URL", "http://127.0.0.1:8005").rstrip("/")
TTS_MODEL = os.getenv("TTS_MODEL", "cosyvoice2")
TTS_VOICE = os.getenv("TTS_VOICE", "default")
TTS_TIMEOUT = int(os.getenv("TTS_TIMEOUT_SECONDS", "120"))
# TTS 总闸：引擎已更换，全面改进完成前 TTS_ENABLED 必须为 "1" 才允许合成。
# 即使客户端请求 tts=true，本开关关闭时 _process_article 也不会生成任何音频。
TTS_ENABLED = os.getenv("TTS_ENABLED", "0") == "1"
PAGE_TIMEOUT_MS = int(os.getenv("CRAWL_PAGE_TIMEOUT_MS", "60000"))
MAX_ARTICLES = int(os.getenv("CRAWL_MAX_ARTICLES", "30"))
DOMAIN_DELAY = float(os.getenv("CRAWL_DOMAIN_DELAY_SECONDS", "1.0"))
ROBOTS_USER_AGENT = os.getenv("ROBOTS_USER_AGENT", "CollectInfoBot")
ROBOTS_FAIL_CLOSED = _bool_env("ROBOTS_FAIL_CLOSED", False)

ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

# 多条队列：crawl / llm(提炼+翻译) / tts
_crawl_pool = ThreadPoolExecutor(max_workers=CRAWL_MAX_WORKERS, thread_name_prefix="crawl")
_llm_pool = ThreadPoolExecutor(max_workers=LLM_MAX_WORKERS, thread_name_prefix="llm")
_tts_pool = ThreadPoolExecutor(max_workers=TTS_MAX_WORKERS, thread_name_prefix="tts")
# 翻译、语音各自独立管线（两条队列），失败自动重试续跑（等服务恢复）
_translate_pool = ThreadPoolExecutor(
    max_workers=max(1, min(8, int(os.getenv("PIPELINE_TRANSLATE_WORKERS", "2")))), thread_name_prefix="translate")
_voice_pool = ThreadPoolExecutor(
    max_workers=max(1, min(8, int(os.getenv("PIPELINE_VOICE_WORKERS", "2")))), thread_name_prefix="voice")


def _retry_until_success(fn, *args, max_attempts=8, base_delay=10, **kwargs):
    """失败指数退避重试，直到成功或服务恢复；用于翻译/语音等可能临时不可用环节。"""
    delay = base_delay
    for attempt in range(max_attempts):
        try:
            return fn(*args, **kwargs)
        except Exception:
            if attempt >= max_attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 300)

_domain_lock = threading.Lock()
_domain_last_request: dict[str, float] = {}
_recovery_lock = threading.Lock()
_recovery_done = False

app = Flask(__name__)


def _normalize_voice(value: Any) -> str:
    voice = str(value or "").strip()
    if not voice:
        return TTS_VOICE
    if len(voice) > 64 or not re.fullmatch(r"[A-Za-z0-9_.-]+", voice):
        raise ValueError("invalid TTS voice id")
    return voice


def _list_tts_voices() -> list[dict[str, Any]]:
    try:
        response = requests.get(f"{TTS_BASE_URL}/v1/voices", timeout=3)
        response.raise_for_status()
        voices = (response.json() or {}).get("voices") or []
        if isinstance(voices, list):
            normalized = [item for item in voices if isinstance(item, dict) and item.get("id")]
            if normalized:
                return normalized
    except (requests.RequestException, ValueError):
        pass
    return [{"id": TTS_VOICE, "display_name": TTS_VOICE, "is_custom": False, "type": "zero_shot"}]


def _db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, status TEXT NOT NULL,
        phase TEXT NOT NULL, request_json TEXT NOT NULL, result_json TEXT,
        industry_pack_id TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
        )"""
    )
    try:
        connection.execute("ALTER TABLE jobs ADD COLUMN industry_pack_id TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass
    connection.execute(
        """CREATE TABLE IF NOT EXISTS job_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, event TEXT NOT NULL,
        data_json TEXT NOT NULL, created_at REAL NOT NULL
        )"""
    )
    # 中间结果：原始文章/精炼后文章/翻译/音频 的渐进式落地
    connection.execute(
        """CREATE TABLE IF NOT EXISTS job_articles (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
        article_index INTEGER NOT NULL, stage TEXT NOT NULL, data_json TEXT NOT NULL,
        created_at REAL NOT NULL, UNIQUE(job_id, article_index, stage)
        )"""
    )
    connection.commit()
    return connection


def _event(job_id: str, event: str, data: dict[str, Any] | None = None) -> None:
    now = time.time()
    with _db() as connection:
        connection.execute(
            "INSERT INTO job_events(job_id,event,data_json,created_at) VALUES(?,?,?,?)",
            (job_id, event, json.dumps(data or {}, ensure_ascii=False), now),
        )
        connection.execute("UPDATE jobs SET phase=?,updated_at=? WHERE id=?", (event, now, job_id))


def _set_job(job_id: str, *, status: str, phase: str, result: dict | None = None, error: str = "") -> None:
    with _db() as connection:
        connection.execute(
            "UPDATE jobs SET status=?,phase=?,result_json=?,error=?,updated_at=? WHERE id=?",
            (status, phase, json.dumps(result, ensure_ascii=False) if result is not None else None,
             error[:1000], time.time(), job_id),
        )


def _put_article_stage(job_id: str, index: int, stage: str, data: dict[str, Any]) -> None:
    with _db() as connection:
        connection.execute(
            "INSERT INTO job_articles(job_id,article_index,stage,data_json,created_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(job_id,article_index,stage) DO UPDATE SET "
            "data_json=excluded.data_json,created_at=excluded.created_at",
            (job_id, int(index), stage, json.dumps(data, ensure_ascii=False), time.time()),
        )


def _authorized() -> bool:
    if len(API_TOKEN) < 32:
        return False
    supplied = request.headers.get("Authorization", "")
    if supplied.lower().startswith("bearer "):
        supplied = supplied[7:].strip()
    return hmac.compare_digest(supplied, API_TOKEN)


@app.before_request
def _require_auth():
    if request.path == "/v1/health":
        return None
    if not _authorized():
        abort(401)
    return None


def _resolved_public_ips(hostname: str) -> list[str]:
    results = []
    for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM):
        value = item[4][0]
        if value not in results:
            results.append(value)
    if not results:
        raise ValueError("hostname did not resolve")
    for value in results:
        address = ipaddress.ip_address(value.split("%", 1)[0])
        if not address.is_global:
            raise ValueError("URL resolves to a non-public address")
    return results


def validate_public_url(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("only public http/https URLs are allowed")
    if parsed.port and parsed.port not in {80, 443}:
        raise ValueError("non-standard destination ports are not allowed")
    _resolved_public_ips(parsed.hostname)
    return parsed.geturl()


def _manual_redirect_check(url: str) -> str:
    current = validate_public_url(url)
    for _ in range(6):
        response = requests.get(current, headers={"User-Agent": ROBOTS_USER_AGENT}, timeout=(5, 15),
                                allow_redirects=False, stream=True)
        if response.status_code not in {301, 302, 303, 307, 308}:
            response.close()
            return current
        location = response.headers.get("Location", "")
        response.close()
        if not location:
            raise ValueError("redirect response has no location")
        current = validate_public_url(urljoin(current, location))
    raise ValueError("too many redirects")


def _robots_allows(url: str) -> tuple[bool, str]:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        response = requests.get(robots_url, headers={"User-Agent": ROBOTS_USER_AGENT}, timeout=(5, 10))
        if response.status_code == 404:
            return True, "robots_absent"
        if response.status_code in {401, 403}:
            return False, f"robots_http_{response.status_code}"
        response.raise_for_status()
        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(response.text.splitlines())
        allowed = bool(parser.can_fetch(ROBOTS_USER_AGENT, url))
        return allowed, "robots_allowed" if allowed else "robots_disallowed"
    except requests.RequestException:
        return (False, "robots_unavailable") if ROBOTS_FAIL_CLOSED else (True, "robots_unavailable_allowed")


def _rate_limit(url: str) -> None:
    domain = (urlparse(url).hostname or "").lower()
    with _domain_lock:
        remaining = DOMAIN_DELAY - (time.monotonic() - _domain_last_request.get(domain, 0.0))
        if remaining > 0:
            time.sleep(remaining)
        _domain_last_request[domain] = time.monotonic()


def _markdown_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    for name in ("fit_markdown", "raw_markdown", "markdown"):
        found = getattr(value, name, None)
        if found:
            return str(found)
    return ""


def _published_at(metadata: Any, html: str) -> str:
    metadata = metadata if isinstance(metadata, dict) else {}
    for key in ("article:published_time", "og:published_time", "published_time",
                "publish_date", "datePublished", "date"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value[:80]
    for pattern in (
        r"<meta[^>]+(?:property|name)=['\"](?:article:published_time|datePublished|publish_date)['\"][^>]+content=['\"]([^'\"]+)",
        r"<meta[^>]+content=['\"]([^'\"]+)['\"][^>]+(?:property|name)=['\"](?:article:published_time|datePublished|publish_date)['\"]",
        r"<time[^>]+datetime=['\"]([^'\"]+)",
    ):
        match = re.search(pattern, html, re.I)
        if match:
            return match.group(1).strip()[:80]
    return ""


# ===================== 独立 OCR 管线（截图识别 → OCR → LLM 总结）=====================
# 说明：这是一条完全独立、用户主动发起的「阅读识别」管线。
# 输入 URL → 打开页面整页截图/快照 → Tesseract 识别图上文字 → Ollama 总结 → 输出 OCR 文本+总结。
# 关键点：
#   - 不参与正文抓取、不往 crawl4ai 抽取的正文里拼任何内容（零污染）。
#   - 不执行 robots.txt 检查（独立的人为阅读动作，不按爬虫策略走）。
#   - 仍做 URL 安全校验（仅公网 http/https、拒绝私网/非标端口）以防 SSRF。
#   - pytesseract / pillow 延迟导入，环境缺依赖时不会导致整个模块无法加载。
#   - 用 Tesseract(chi_sim) 而非 PaddleOCR：Paddle 2.6.1 依赖 AVX512，
#     本机 Xeon E5-2686 v4(Broadwell) 无 AVX512，Paddle 推理会 SIGILL。

_ocr_engine = None
_TESSERACT_LANG = "chi_sim+eng"  # 简体中文 + 英文


def _get_ocr():
    """懒加载 Tesseract 配置（校验语言包可用）。返回语言字符串。"""
    global _ocr_engine
    if _ocr_engine is None:
        # 延迟导入，仅真正用到 OCR 时才需要 pytesseract / pillow
        import pytesseract
        from PIL import Image  # noqa: F401  仅确认 pillow 可用
        # 校验 chi_sim 语言包是否安装，未安装则退化到英文。
        # get_languages() 返回 list（如 ['chi_sim','eng','osd']）；注意不要用 config=""，
        # 后者返回字符串 repr，split 会把带引号的项拆错导致匹配失败。
        try:
            langs = set(str(x).strip() for x in pytesseract.get_languages())
        except Exception:
            langs = set()
        if "chi_sim" in langs:
            _ocr_engine = "chi_sim+eng"
        elif "chi_sim_vert" in langs:
            _ocr_engine = "chi_sim_vert"
        else:
            _ocr_engine = "eng"
    return _ocr_engine


def _perform_ocr_bytes(image_bytes: bytes) -> str:
    """对一张图片的字节流做 OCR（Tesseract），返回识别出的文本。失败返回空串（不终止流程）。"""
    try:
        import io
        import pytesseract
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        lang = _get_ocr()
        text = pytesseract.image_to_string(img, lang=lang)
        # 规整：合并空白行、保留换行
        lines = [ln.rstrip() for ln in str(text).splitlines()]
        return "\n".join(ln for ln in lines if ln.strip())
    except Exception as exc:  # noqa: BLE001
        print(f"[OCR] 图片识别失败: {type(exc).__name__}: {str(exc)[:200]}")
        return ""


async def _snapshot_page(url: str) -> dict[str, Any]:
    """打开页面并截取整页快照，返回 {url, screenshot(bytes), title}。

    crawl4ai 0.8.0 的 screenshot 行为（已核实源码）：
      无 screenshot_type 参数；screenshot=True 后由 take_screenshot() 判断——
      若 scrollHeight <= viewportHeight（无需滚动）则截当前视口；
      否则按 screenshot_height_threshold(默认10000) 放大视口/分段滚动拼接，尽可能完整。
    """
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

    browser = BrowserConfig(headless=True, java_script_enabled=True, verbose=False)
    run = CrawlerRunConfig(
        wait_until="domcontentloaded",
        page_timeout=PAGE_TIMEOUT_MS,
        delay_before_return_html=1.0,
        remove_overlay_elements=True,
        screenshot=True,
        screenshot_height_threshold=10000,
        verbose=False,
    )
    async with AsyncWebCrawler(config=browser) as crawler:
        result = await crawler.arun(url=url, config=run)
    final_url = validate_public_url(str(getattr(result, "url", "") or url))
    screenshot_b64 = str(getattr(result, "screenshot", "") or "")
    screenshot = b""
    if screenshot_b64.startswith("data:"):
        # data:image/png;base64,...
        screenshot_b64 = screenshot_b64.split(",", 1)[-1]
    if screenshot_b64:
        try:
            import base64
            screenshot = base64.b64decode(screenshot_b64)
        except Exception:  # noqa: BLE001
            screenshot = b""
    html = str(getattr(result, "html", "") or "")
    metadata = getattr(result, "metadata", {}) or {}
    metadata_title = metadata.get("title") if isinstance(metadata, dict) else ""
    title = str(getattr(result, "title", "") or metadata_title or "").strip()
    if not title:
        match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", match.group(1))).strip() if match else final_url
    return {"url": final_url, "screenshot": screenshot, "title": title[:500]}


def _ocr_summarize(ocr_text: str, title: str = "") -> dict[str, Any]:
    """用 Ollama 对 OCR 识别文本做 LLM 总结。返回 {summary, model, source_language}。"""
    sample = str(ocr_text or "").strip()
    lang = _language(sample)
    if not sample:
        return {"summary": "", "model": OLLAMA_MODEL, "source_language": lang}
    window = sample[:OLLAMA_MAX_INPUT]
    result = _ollama_json(
        [
            {"role": "system", "content":
             "你是信息提取助手。下面是一张网页截图上 OCR 识别出的文字。"
             "请提炼成简洁、结构化的中文总结，保留关键事实（时间、主体、事件、数据），"
             "忽略 OCR 噪声、广告、导航文字。只输出 JSON，键为 summary_summary。"},
            {"role": "user", "content": f"标题:{title or '(无)'}\nOCR文字:\n{window}"},
        ],
        max_tokens=600,
    )
    summary = str(result.get("summary_summary") or "").strip()
    return {"summary": summary, "model": OLLAMA_MODEL, "source_language": lang}


def run_ocr_pipeline(url: str) -> dict[str, Any]:
    """独立 OCR 管线入口：URL安全校验 → 截图 → OCR → LLM 总结。

    不检查 robots.txt（独立的人为阅读动作）。
    """
    try:
        # 仅做 URL/重定向安全校验（防 SSRF），不做 robots 检查
        checked = _manual_redirect_check(url)
        snap = asyncio.run(_snapshot_page(checked))
        if not snap.get("screenshot"):
            return {"success": False, "url": snap["url"], "title": snap["title"],
                    "error": "screenshot is empty"}
        ocr_text = _perform_ocr_bytes(snap["screenshot"])
        fin = _ocr_summarize(ocr_text, snap["title"])
        return {
            "success": True,
            "url": snap["url"],
            "title": snap["title"],
            "ocr_text": ocr_text,
            "ocr_length": len(ocr_text),
            "summary": fin["summary"],
            "summary_model": fin["model"],
            "source_language": fin["source_language"],
        }
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "url": url, "error": str(exc)[:500]}


def render_page_links(url: str) -> dict[str, Any]:
    """真实浏览器渲染页面，返回内部链接 + .pdf 链接（不检查 robots.txt）。

    用于报告 URL 探测：对信源用普通请求被反爬/412/403 抓不到时，走 crawl4ai 用真实
    浏览器打开，再从渲染后的真实 DOM 里提取《.pdf 链接》和《报告类内部链接》。
    """
    try:
        checked = _manual_redirect_check(url)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "url": url, "error": str(exc)[:300]}
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

    browser = BrowserConfig(headless=True, java_script_enabled=True, verbose=False)
    run = CrawlerRunConfig(
        wait_until="domcontentloaded",
        page_timeout=PAGE_TIMEOUT_MS,
        delay_before_return_html=1.0,
        remove_overlay_elements=True,
        verbose=False,
    )

    async def _render():
        async with AsyncWebCrawler(config=browser) as crawler:
            return await crawler.arun(url=checked, config=run)

    try:
        result = asyncio.run(_render())
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "url": checked, "error": str(exc)[:300]}
    final_url = validate_public_url(str(getattr(result, "url", "") or checked))
    html = str(getattr(result, "html", "") or "")
    links_data = getattr(result, "links", {}) or {}
    internal = links_data.get("internal", []) if isinstance(links_data, dict) else []
    hrefs: list[str] = []
    for item in internal:
        href = item.get("href") if isinstance(item, dict) else str(item)
        if href:
            hrefs.append(urljoin(final_url, str(href)))
    pdf_links = [
        urljoin(final_url, m.group(1).strip())
        for m in re.finditer(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', html, re.I)
    ]
    return {
        "success": True,
        "url": final_url,
        "links": sorted(set(hrefs)),
        "pdf_links": sorted(set(pdf_links)),
    }


async def _crawl_async(url: str) -> dict[str, Any]:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

    browser = BrowserConfig(headless=True, java_script_enabled=True, verbose=False)
    # 改进3：SPA（如 mittrchina）正文由 JS 异步加载，domcontentloaded 时还是空壳，
    # 抽到的是导航/链接目录。改为 networkidle（等所有网络请求安静下来，含正文 XHR）
    # 并额外延迟 2.5s，让正文真正渲染进 DOM 后再取 HTML/markdown。
    run = CrawlerRunConfig(
        wait_until="networkidle",
        page_timeout=PAGE_TIMEOUT_MS,
        delay_before_return_html=2.5,
        remove_overlay_elements=True,
        remove_forms=True,
        word_count_threshold=1,
        verbose=False,
    )
    async with AsyncWebCrawler(config=browser) as crawler:
        result = await crawler.arun(url=url, config=run)
    final_url = validate_public_url(str(getattr(result, "url", "") or url))
    markdown = _markdown_text(getattr(result, "markdown", ""))
    html = str(getattr(result, "html", "") or "")
    metadata = getattr(result, "metadata", {}) or {}
    metadata_title = metadata.get("title") if isinstance(metadata, dict) else ""
    title = str(getattr(result, "title", "") or metadata_title or "").strip()
    if not title:
        match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", match.group(1))).strip() if match else final_url
    content = markdown.strip() or re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()

    links = getattr(result, "links", {}) or {}
    return {
        "success": bool(getattr(result, "success", False)) and bool(content),
        "url": final_url, "title": title[:500], "content": content, "html": html,
        "links": links, "publish_date": _published_at(metadata, html),
        "error": str(getattr(result, "error_message", "") or getattr(result, "error", "")),
    }


def _crawl(url: str) -> dict[str, Any]:
    checked = _manual_redirect_check(url)
    allowed, reason = _robots_allows(checked)
    if not allowed:
        return {"success": False, "url": checked, "error": reason, "robots": reason, "permanent": True}
    _rate_limit(checked)
    result = asyncio.run(_crawl_async(checked))
    result["robots"] = reason
    return result


def _candidate_links(base_url: str, links: Any, limit: int) -> list[str]:
    base_host = (urlparse(base_url).hostname or "").lower()
    values = links.get("internal", []) if isinstance(links, dict) else []
    found = []
    blocked = (".jpg", ".png", ".gif", ".svg", ".pdf", ".zip", "/tag/", "/category/", "javascript:", "mailto:")
    for item in values:
        href = item.get("href") if isinstance(item, dict) else str(item)
        candidate = urljoin(base_url, str(href or "").strip())
        parsed = urlparse(candidate)
        if parsed.hostname and parsed.hostname.lower() == base_host and not any(x in candidate.lower() for x in blocked):
            canonical = candidate.split("#", 1)[0]
            if canonical not in found and canonical != base_url:
                found.append(canonical)
        if len(found) >= limit:
            break
    return found


def _language(text: str) -> str:
    sample = str(text or "")[:4000]
    chinese = sum(1 for char in sample if "\u4e00" <= char <= "\u9fff")
    return "zh" if chinese >= max(2, len(sample) // 20) else "en"


def _extract_json_object(content: str) -> Any:
    """Parse an Ollama 'format=json' response, tolerating markdown fences."""
    text = str(content or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S | re.I)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1:
        raise ValueError("LLM response does not contain a JSON object")
    if end < start:
        text = text[start:]
    else:
        text = text[start:end + 1]
    return json.loads(text)


def _ollama_json(messages: list[dict[str, str]], max_tokens: int = 1800) -> dict[str, Any]:
    """兼容别名：统一走 LLM 后端路由（ollama/llama.cpp/vLLM/shim 自动探测 + failover）。"""
    return _llm_json(messages, max_tokens)


def _check_industry(title: str, content: str, industry_pack_name: str, industry_pack_id: str) -> dict[str, Any]:
    """Tier2 行业门禁：用一句话概括所属行业，判定是否属于当前行业包。
    不属于 → in_pack=False，调用方跳过提炼/翻译/语音（节省 LLM/翻译算力）。失败时保守放行。"""
    text = (str(title or "") + "\n" + str(content or "")[:4000])
    messages = [
        {"role": "system", "content": "你是行业分类器。只输出 JSON，不输出解释或 Markdown。"},
        {"role": "user", "content": (
            f"判断下面这篇文章属于哪个行业。用不超过50字的一句话概括其主要行业/领域；"
            f"并判定它是否确实属于当前行业包【{industry_pack_name}】（行业包id：{industry_pack_id}）。"
            "若实际属于其它行业、或仅为共用泛词（如基础设施/中标/AI/政策）命中、或与行业包无关，in_pack 必须为 false。"
            '只输出 JSON：{"industry":"...","in_pack":true|false}\n'
            f"<ARTICLE>\n{text}\n</ARTICLE>"
        )},
    ]
    try:
        parsed = _ollama_json(messages, max_tokens=300)
        return {"industry": str(parsed.get("industry") or "")[:60], "in_pack": bool(parsed.get("in_pack"))}
    except Exception as exc:
        return {"industry": "", "in_pack": True, "error": str(exc)[:120]}  # 融错：默认放行


def _split_text(text: str, size: int = 4000) -> list[str]:
    remaining = str(text or "").strip()
    parts = []
    while len(remaining) > size:
        window = remaining[:size]
        points = [m.end() for m in re.finditer(r"[。！？!?;；]\s*|\n+", window)]
        cut = next((point for point in reversed(points) if point >= size // 2), size)
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts


def _refine_article(article: dict[str, Any], *, industry_pack_name: str = "", industry_topics: list | None = None) -> dict[str, Any]:
    """LLM 二次加工/提炼总结，产出用于展示的『精炼后文章』。

    分层提炼（collectinfo-refine-v3）：先判定输入是连贯正文(A)、标题列表(B)还是噪声(C)，
    再按行业包相关性锚定，只输出与行业包相关的内容；B/C 类输出 1~3 条要点甚至一句话，
    不再把聚合列表全文总结成"正文"。
    """
    src_lang = _language(f"{article.get('title', '')}\n{article.get('content', '')}")
    content = str(article.get("content") or "")
    # 提炼总结只需处理一个合理窗口，避免超长输入导致 LLM 超时。
    window = content[:6000]
    pack_line = ""
    if industry_pack_name:
        topics_text = "、".join(
            str(t.get("name") or "") for t in (industry_topics or []) if t.get("name")
        ) or "（无）"
        pack_line = f"当前行业包为【{industry_pack_name}】，允许参考的固定主题为：{topics_text}。"
    else:
        pack_line = "当前行业包信息缺失：以文章标题与正文自身主题为准提炼，不要引入无关领域内容。"
    result = _ollama_json(
        [
            {"role": "system", "content":
             "你是行业资讯提炼器。输入可能混有网页导航、浏览器兼容提示、登录区、作者卡片、"
             "推荐列表、评论等噪声，甚至输入本身可能不是文章而是标题列表。"
             "严禁把噪声或与行业包无关的内容写进输出；只输出 JSON，不要任何解释。\n"
             "第一步——内容判定：输入属于 A.连贯正文文章；B.标题列表（作者主页/聚合页/栏目页）；"
             "C.噪声为主、几乎无实质内容。\n"
             "第二步——相关性锚定：只有与行业包相关的条目/段落才进入输出，无关条目全部丢弃。\n"
             "第三步——分层输出，JSON 键固定："
             '{"content_type":"A或B或C","relevance":"high或low或none",'
             '"refined_title":"…","refined_content":"…","core_facts":["…", "…"]}\n"'
             "- A 类：refined_content 为 300~600 字结构化中文精炼文，按原文逻辑分段，"
             "保留关键事实与数字（年份、百分比、型号、人名、地名、机构名），剔除全部噪声与无关段落。\n"
             "- B 类：refined_content 只写与行业包相关的 1~3 条要点，每条一句话，总计不超过 150 字；"
             "若没有任何相关条目，refined_content 写\"本文为聚合列表，与行业包无实质相关内容\"。\n"
             "- C 类：refined_content 写\"本文无实质正文内容\"。\n"
             "- relevance：A 类且正文主体相关为 high；只有部分相关为 low；无相关内容为 none。\n"
             "- core_facts：不超过 5 条短语，列出与行业包相关的关键事实/数字/主体；无关内容不得进入。"},
            {"role": "user", "content": f"{pack_line}\n标题:{article.get('title','')}\n文章:\n{window}"},
        ],
        max_tokens=900,
    )
    refined_content = str(result.get("refined_content") or "").strip()
    refined_title = str(result.get("refined_title") or "").strip() or str(article.get("title") or "")
    content_type = str(result.get("content_type") or "A")
    relevance = str(result.get("relevance") or "high")
    if not refined_content:
        # 模型偶发不遵守 B/C 写死文本约定（输出空字符串）：按分层兜底，避免上层把
        # 「与行业包无相关内容」误判成精炼失败反复重试。B/C 类本就不应产出正文。
        if content_type == "C":
            refined_content = "本文无实质正文内容"
            relevance = "none"
        elif content_type == "B":
            refined_content = "本文为聚合列表，与行业包无实质相关内容"
            relevance = "none"
        else:
            raise ValueError("refine produced empty content")
    return {
        "source_language": src_lang,
        "refined_title": refined_title,
        "refined_content": refined_content,
        "content_type": content_type,
        "relevance": relevance,
        "core_facts": [str(x) for x in (result.get("core_facts") or []) if str(x).strip()][:5],
        "model": OLLAMA_MODEL,
        "prompt_version": "collectinfo-refine-v3",
        "source_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def _translate_refined(refined_content: str, src_lang: str) -> dict[str, Any]:
    target_language = "en" if src_lang == "zh" else "zh"
    target_name = "English" if target_language == "en" else "Simplified Chinese"
    translated_parts = []
    chunk_size = 1600 if src_lang == "zh" else 1800
    for index, part in enumerate(_split_text(refined_content, size=chunk_size)):
        result = _ollama_json(
            [
                {"role": "system", "content": f"把下面内容忠实翻译成{target_name}。保留专名/数字/段落顺序。仅返回 JSON，键为 translated_text。"},
                {"role": "user", "content": part},
            ],
            max_tokens=1800,
        )
        translated = str(result.get("translated_text") or "").strip()
        if not translated:
            raise ValueError(f"translation chunk {index} is empty")
        translated_parts.append(translated)
    return {
        "target_language": target_language,
        "translated_content": "\n\n".join(translated_parts),
        "model": OLLAMA_MODEL,
        "prompt_version": "collectinfo-refine-translate-v1",
    }


def _speech_parts(text: str, language: str) -> list[str]:
    size = 120 if language == "zh" else 300
    return _split_text(text, size=size)


def _generate_audio(job_id: str, article_index: int, text: str, language: str, kind: str, voice: str = TTS_VOICE, max_parts: int = 0) -> list[dict[str, Any]]:
    # TTS 总闸：引擎更换改进完成前禁止合成（双保险，即使调用方绕过 _process_article）
    if not TTS_ENABLED:
        raise ValueError("TTS disabled (engine under maintenance)")
    directory = ARTIFACT_DIR / job_id
    directory.mkdir(parents=True, exist_ok=True)
    manifest = []
    parts = _speech_parts(text, language)
    # 分级生成：max_parts>0 时只生成前 max_parts 段（其余由读取端按需生成），避免全量 TTS 浪费
    if max_parts and len(parts) > max_parts:
        parts = parts[:max_parts]
    for index, part in enumerate(parts):
        response = requests.post(
            f"{TTS_BASE_URL}/v1/audio/speech",
            json={"model": TTS_MODEL, "input": part, "voice": voice, "response_format": "wav", "speed": 1.0},
            timeout=TTS_TIMEOUT,
        )
        response.raise_for_status()
        if not response.content.startswith(b"RIFF"):
            raise ValueError("TTS returned a non-WAV response")
        name = f"article-{article_index:03d}-{kind}-{index:03d}.wav"
        path = directory / name
        path.write_bytes(response.content)
        item = {"index": index, "kind": kind, "text_hash": hashlib.sha256(part.encode()).hexdigest(),
                "bytes": len(response.content), "artifact": name}
        manifest.append(item)
        _event(job_id, "audio_chunk_ready", {"article_index": article_index, **item})
    return manifest


def _process_article(job_id: str, index: int, raw: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """单篇文章：原稿先落 stage=raw(未展示)，再 二次提炼 -> refined(展示)，再 翻译+语音(针对二次加工后文章)。"""
    voice = _normalize_voice(payload.get("voice"))
    item = {
        "url": raw["url"], "canonical_url": raw["url"], "title": raw["title"],
        "content": raw["content"], "content_length": len(raw["content"]),
        "raw_content": raw["content"],
        "publish_date": raw.get("publish_date", ""), "domain": urlparse(raw["url"]).netloc,
        "crawler_engine_used": "remote_crawl4ai", "crawler_engines": ["remote_crawl4ai"], "crawler_attempts": 1,
        "source_method": "vpn_remote", "extraction_method": "crawl4ai",
        "configured_url": payload["url"], "resolved_target_url": raw["url"],
        "robots": raw.get("robots", ""), "remote_job_id": job_id,
        "stage": "raw", "display_status": "processing",  # 未展示
    }
    _put_article_stage(job_id, index, "raw", item)
    _event(job_id, "raw_ready", {"article_index": index, "display_status": "processing"})

    # Tier2 行业门禁：先一句话判定所属行业，非当前行业包 → 跳过提炼/翻译/语音（省算力）
    _ip_name = str(payload.get("industry_pack_name") or "").strip()
    _ip_id = str(payload.get("industry_pack_id") or "").strip()
    if payload.get("enrich", True) and _ip_name:
        try:
            _ind = _check_industry(item.get("title", ""), item.get("content", ""), _ip_name, _ip_id)
            item["industry"] = _ind.get("industry", "")
            item["industry_admitted"] = _ind.get("in_pack", True)
            if not _ind.get("in_pack", True):
                item["stage"] = "industry_rejected"
                item["display_status"] = "rejected"
                _put_article_stage(job_id, index, "industry_rejected", item)
                _event(job_id, "industry_rejected", {"article_index": index, "industry": _ind.get("industry", "")})
                return item
        except Exception as exc:
            item["industry_check_error"] = str(exc)[:200]

    # 二次提炼（LLM，走 _llm_pool 即LLM队列）——产出用于展示的精炼文
    if payload.get("enrich", True):
        try:
            refined = _refine_article(
                item,
                industry_pack_name=str(payload.get("industry_pack_name") or "").strip(),
                industry_topics=list(payload.get("industry_topics") or []),
            )
            item["stage"] = "refined"
            item["display_status"] = "ready"  # 二次加工后可展示
            item["refined_content"] = refined["refined_content"]
            item["refined_title"] = refined["refined_title"]
            item["source_language"] = refined["source_language"]
            item["target_language"] = "en" if refined["source_language"] == "zh" else "zh"
            item["refine_model"] = refined["model"]
            item["content"] = refined["refined_content"]  # 展示内容=二次加工后
            # 分层提炼元数据（供展示层降级策略使用）
            item["content_type"] = refined.get("content_type") or "A"
            item["relevance"] = refined.get("relevance") or "high"
            item["core_facts"] = refined.get("core_facts") or []
            _put_article_stage(job_id, index, "refined", item)
            _event(job_id, "refined_ready", {"article_index": index, "display_status": "ready"})
        except Exception as exc:
            item["refine_error"] = str(exc)[:500]
            _put_article_stage(job_id, index, "refined", item)
            _event(job_id, "refine_error", {"article_index": index, "error": str(exc)[:500]})
            return item

    # 翻译 + 语音：针对【二次加工后的文章】，两条独立管线（翻译池/语音池）+ 失败重试续跑
    derivative = {}
    if payload.get("enrich", True) and item.get("refined_content"):
        fut = _translate_pool.submit(
            _retry_until_success, _translate_refined, item["refined_content"],
            item.get("source_language") or "zh")
        try:
            derivative = fut.result()
            item["translated_content"] = derivative["translated_content"]
            item["target_language"] = derivative["target_language"]
            item["derivative"] = derivative
            _put_article_stage(job_id, index, "translated", item)
            _event(job_id, "translated_ready", {"article_index": index})
        except Exception as exc:
            item["translation_error"] = str(exc)[:500]
            _put_article_stage(job_id, index, "translated", item)
            _event(job_id, "translation_error", {"article_index": index, "error": str(exc)[:500]})

    if TTS_ENABLED and payload.get("tts", True) and item.get("refined_content"):
        lang = item.get("source_language") or "zh"
        fut = _voice_pool.submit(
            _retry_until_success, _generate_audio, job_id, index, item["refined_content"],
            lang, "refined", voice=voice, max_parts=2)
        try:
            audio = fut.result()
            item["audio_manifest"] = {"refined": audio}
            item["audio_manifest_legacy"] = audio
            _put_article_stage(job_id, index, "audio", item)
            _event(job_id, "audio_ready", {"article_index": index})
        except Exception as exc:
            item["audio_error"] = str(exc)[:500]
            _put_article_stage(job_id, index, "audio", item)
            _event(job_id, "audio_error", {"article_index": index, "error": str(exc)[:500]})
    return item


def _run_job(job_id: str, payload: dict[str, Any]) -> None:
    try:
        _set_job(job_id, status="running", phase="crawling")
        _event(job_id, "crawling", {"url": payload["url"]})
        root = _crawl(payload["url"])
        if not root.get("success"):
            raise RuntimeError(root.get("error") or "crawl failed")
        limit = max(1, min(int(payload.get("limit") or 10), MAX_ARTICLES))
        articles = [root]
        if payload.get("mode") == "list":
            articles = []
            for candidate in _candidate_links(root["url"], root.get("links"), limit):
                current = _crawl(candidate)
                if current.get("success"):
                    articles.append(current)
            if not articles and root.get("content"):
                articles = [root]

        # 逐篇：拿到原稿(该 job 的爬取队列已产出)，然后分别进入 LLM 队列与 TTS 队列做二次处理
        normalized = []
        futures = {}
        for index, art in enumerate(articles[:limit]):
            futures[index] = _llm_pool.submit(_process_article, job_id, index, art, payload)
        for index, fut in futures.items():
            try:
                normalized.append(fut.result())
            except Exception as exc:
                normalized.append({"article_index": index, "error": str(exc)[:300]})
        if not normalized:
            raise RuntimeError("no articles were extracted")
        status = "completed" if all("refine_error" not in x and "error" not in x for x in normalized) else "partial_success"
        result = {"success": True, "status": status, "job_id": job_id, "articles": normalized, "articles_found": len(normalized)}
        _set_job(job_id, status=status, phase="completed", result=result)
        _event(job_id, "completed", {"status": status, "articles_found": len(normalized)})
    except Exception as exc:
        _set_job(job_id, status="failed", phase="failed", error=str(exc))
        _event(job_id, "failed", {"error": str(exc)[:500]})


def _recover_jobs_once() -> None:
    """容器重启后，把仍在 queued/running 的任务改为 failed（不阻塞新任务）。"""
    global _recovery_done
    with _recovery_lock:
        if _recovery_done:
            return
        _recovery_done = True
        with _db() as connection:
            connection.execute(
                "UPDATE jobs SET status='failed', phase='recovered', error='interrupted by rebuild' "
                "WHERE status IN ('queued','running')"
            )


def _component_health(url: str) -> dict[str, Any]:
    started = time.monotonic()
    try:
        response = requests.get(url, timeout=3)
        return {"ok": response.status_code < 400, "status": response.status_code,
                "latency_ms": round((time.monotonic() - started) * 1000)}
    except requests.RequestException as exc:
        return {"ok": False, "error": type(exc).__name__}


@app.get("/v1/health")
def health():
    _recover_jobs_once()
    configured = len(API_TOKEN) >= 32
    return jsonify({"ok": configured, "service": "collectinfo-pipeline", "version": "2.0.0",
                    "auth_configured": configured}), 200 if configured else 503


@app.get("/v1/capabilities")
def capabilities():
    _recover_jobs_once()
    # LLM 组件健康按路由选中的后端报告（不再写死 ollama）
    _backend = pick_llm_backend(force=True)
    _llm_url = f"{_backend['url']}/api/tags" if _backend["api"] == "ollama" else f"{_backend['url']}/v1/models"
    components = {"ollama": _component_health(_llm_url),
                  "tts": _component_health(f"{TTS_BASE_URL}/health")}
    return jsonify({
        "ok": all(item["ok"] for item in components.values()), "components": components,
        "llm_backend": {"name": _backend["name"], "api": _backend["api"], "model": _backend["model"], "url": _backend["url"]},
        "tts_voices": _list_tts_voices(), "tts_default_voice": TTS_VOICE,
        "capabilities": {"crawl4ai": True, "javascript": True, "summary": True,
                         "translation": ["zh-en", "en-zh"], "tts_streaming": True,
                         "refine_first": True, "queues": ["crawl", "llm", "tts"]},
        "model": _backend["model"],
    })


@app.post("/v1/pipeline/jobs")
def submit_job():
    _recover_jobs_once()
    payload = request.get_json(silent=True) or {}
    try:
        payload["url"] = validate_public_url(payload.get("url", ""))
    except (ValueError, OSError, socket.gaierror) as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    payload["mode"] = "list" if payload.get("mode") == "list" else "article"
    try:
        payload["voice"] = _normalize_voice(payload.get("voice"))
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    key = str(payload.get("idempotency_key") or hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest())[:128]
    now = time.time()
    with _db() as connection:
        existing = connection.execute("SELECT id,status FROM jobs WHERE idempotency_key=?", (key,)).fetchone()
        if existing:
            return jsonify({"success": True, "job_id": existing["id"], "status": existing["status"], "deduplicated": True}), 200
        job_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO jobs(id,idempotency_key,status,phase,request_json,industry_pack_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (job_id, key, "queued", "queued", json.dumps(payload, ensure_ascii=False), str(payload.get('industry_pack_id') or ''), now, now),
        )
    _event(job_id, "queued", {})
    _crawl_pool.submit(_run_job, job_id, payload)
    return jsonify({"success": True, "job_id": job_id, "status": "queued", "deduplicated": False}), 202


@app.post("/v1/pipeline/enrich")
def enrich_pipeline():
    """纯加工（不重新抓取）：对已抓取正文做 提炼/翻译/语音。

    复用 _process_article 的精炼/翻译/音频管线，把音频（max_parts=2 前两段）存到
    ARTIFACT_DIR/<job_id>，并返回 job_id —— 客户端可据此 fetch_artifact 预生成音频。
    """
    payload = request.get_json(silent=True) or {}
    content = str(payload.get("content") or "").strip()
    if not content:
        return jsonify({"success": False, "error": "缺少 content"}), 400
    try:
        voice = _normalize_voice(payload.get("voice"))
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    url = str(payload.get("url") or "memory://").strip()
    title = str(payload.get("title") or "待精炼").strip()
    publish_date = str(payload.get("publish_date") or "")
    job_id = uuid.uuid4().hex
    raw = {
        "url": url, "title": title, "content": content,
        "publish_date": publish_date,
    }
    try:
        item = _process_article(
            job_id, 0, raw,
            {
                "enrich": True,
                "tts": bool(payload.get("tts", True)),
                "voice": voice,
                "url": url,
                # 分层提炼上下文：行业包名/固定主题（客户端注入）
                "industry_pack_name": str(payload.get("industry_pack_name") or "").strip(),
                "industry_pack_id": str(payload.get("industry_pack_id") or "").strip(),
                "industry_topics": list(payload.get("industry_topics") or []),
            },
        )
    except Exception as exc:
        _event(job_id, "completed", {"status": "failed", "error": str(exc)[:300]})
        return jsonify({"success": False, "error": str(exc)[:300]}), 500
    _event(job_id, "completed", {"status": "completed", "article_index": 0})
    return jsonify({
        "success": True, "job_id": job_id,
        "url": str(item.get("url") or url),
        "title": str(item.get("refined_title") or title),
        "raw_content": str(item.get("raw_content") or content),
        "refined_content": str(item.get("refined_content") or ""),
        "translated_content": str(item.get("translated_content") or ""),
        "audio_manifest": item.get("audio_manifest") or {},
        "source_language": str(item.get("source_language") or "zh"),
        "target_language": str(item.get("target_language") or ("en" if (item.get("source_language") or "zh") == "zh" else "zh")),
        # 分层提炼元数据（content_type/relevance/core_facts）
        "content_type": str(item.get("content_type") or "A"),
        "relevance": str(item.get("relevance") or "high"),
        "core_facts": list(item.get("core_facts") or []),
    }), 200


@app.post("/v1/pipeline/ocr")
def ocr_read_page():
    """独立 OCR 阅读管线：对给定 URL 打开页面 → 整页截图 → OCR → LLM 总结。

    与 /v1/pipeline/jobs 完全隔离：不参与文章正文抓取、不写 job_articles、
    不检查 robots.txt（独立的人为阅读动作）。返回 OCR 文本 + LLM 总结。同步执行。
    """
    payload = request.get_json(silent=True) or {}
    try:
        url = validate_public_url(payload.get("url", ""))
    except (ValueError, OSError, socket.gaierror) as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    result = run_ocr_pipeline(url)
    if not result.get("success"):
        return jsonify(result), 422
    return jsonify(result), 200


@app.post("/v1/pipeline/links")
def render_links_endpoint():
    """真实浏览器渲染页面并返回链接（内部链接 + .pdf 链接），不检查 robots.txt。

    供报告 URL 探测使用：普通请求被反爬/412/403 拒掉时报错，由信源探测层走到这里
    拿渲染后的真实链接。同步执行。
    """
    payload = request.get_json(silent=True) or {}
    try:
        url = validate_public_url(payload.get("url", ""))
    except (ValueError, OSError, socket.gaierror) as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    result = render_page_links(url)
    if not result.get("success"):
        return jsonify(result), 422
    return jsonify(result), 200


@app.get("/v1/pipeline/jobs/<job_id>")
def get_job(job_id: str):
    with _db() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        abort(404)
    result = json.loads(row["result_json"]) if row["result_json"] else None
    if not result:
        # 结果未完成时，渐进式回填到 result（保持旧客户端兼容）
        with _db() as connection:
            rows = connection.execute(
                "SELECT article_index, data_json FROM job_articles WHERE job_id=? ORDER BY article_index",
                (job_id,)).fetchall()
        if rows:
            normalized = []
            seen = {}
            for r in rows:
                item = json.loads(r["data_json"])
                idx = r["article_index"]
                if idx not in seen:
                    seen[idx] = {"article_index": idx}
                # 取 stage 最全的一份（audio>translated>refined>raw）
                if item.get("stage") == "audio" or "audio_manifest" in item:
                    seen[idx] = item
                elif "translated_content" in item and seen[idx].get("stage") != "audio":
                    seen[idx] = item
                elif "refined_content" in item and seen[idx].get("stage") not in ("audio", "translated"):
                    seen[idx] = item
            normalized = [seen[k] for k in sorted(seen)]
            result = {"success": True, "status": row["status"], "job_id": job_id,
                      "articles": normalized, "articles_found": len(normalized)}
    return jsonify({
        "success": True, "job_id": row["id"], "status": row["status"], "phase": row["phase"],
        "result": result, "error": row["error"], "created_at": row["created_at"], "updated_at": row["updated_at"],
    })


@app.get("/v1/pipeline/jobs/<job_id>/events")
def get_events(job_id: str):
    after = max(0, int(request.args.get("after", "0")))
    with _db() as connection:
        rows = connection.execute(
            "SELECT id,event,data_json,created_at FROM job_events WHERE job_id=? AND id>? ORDER BY id LIMIT 500",
            (job_id, after)).fetchall()
    return jsonify({"success": True, "events": [{"id": row["id"], "event": row["event"],
                    "data": json.loads(row["data_json"]), "created_at": row["created_at"]} for row in rows]})


@app.get("/v1/pipeline/jobs/<job_id>/artifacts/<name>")
def artifact(job_id: str, name: str):
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        abort(400)
    path = ARTIFACT_DIR / job_id / name
    if not path.is_file():
        abort(404)
    return send_file(path, mimetype="audio/wav", conditional=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=11236, threaded=True)

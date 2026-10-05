#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 LLM 端点「当前实际能用哪个模型」的自适应解析。

为什么需要：本地推理机受显存限制，同一时间只会加载一个模型
（例如 gemma431b-32k 与 qwen3.8-27b-uncensored 二选一），而助手与统一 QA
用的是配置里写死的 model_id。配置说 A、显存里装的是 B 时会出现两种坏事：

* 请求 A → Ollama 先把 B 挤掉再加载 A（实测冷加载约 50s），首包超时→整段降级；
* 若 A 恰好加载失败（显存不足等），接口会返回空内容，答案直接是空的。

所以每次解析本地 provider 时问一下端点「现在装的是谁」，跟随当前可用的那个：

    1) Ollama:    GET {base}/api/ps     → 已加载进显存的模型（才是"当前可用"）
    2) OpenAI 兼容: GET {base}/v1/models → 已安装的模型列表

选择规则（保守，任何异常都不改变原行为）：

* 配置的模型就在「已加载」里            → 用配置的；
* 「已加载」里只有一个别的模型          → 跟随它（避免重新加载/互挤）；
* 拿不到 /api/ps 时，如果 /v1/models 只有一个模型且不是配置的 → 用那个；
* 其余情况（探测失败、多模型、不认识）  → 原样返回配置值。

结果按 base_url 缓存（默认 20s），探测超时默认 1.5s，绝不阻塞问答。

环境变量：
    LOCAL_MODEL_AUTOSELECT=0         关闭自适应（默认开）
    LOCAL_MODEL_PROBE_TIMEOUT=0.8    单次探测超时秒数
    LOCAL_MODEL_PROBE_TTL_SECONDS=20 结果缓存秒数
"""
from __future__ import annotations

import os
import threading
import time

import requests

_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_FAIL_TTL_SECONDS = 5.0


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().casefold() not in {"0", "false", "no", "off"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def enabled() -> bool:
    return _env_bool("LOCAL_MODEL_AUTOSELECT", True)


def probe_timeout() -> float:
    # 内网探测正常在毫秒级；端点挂掉时这一步最多拖两次超时，所以默认压到 0.8s。
    return max(0.3, _env_float("LOCAL_MODEL_PROBE_TIMEOUT", 0.8))


def cache_ttl() -> float:
    return max(1.0, _env_float("LOCAL_MODEL_PROBE_TTL_SECONDS", 20.0))


def same_model(left: str, right: str) -> bool:
    """模型名比较：忽略大小写与 :latest 后缀差异。"""
    def norm(value: str) -> str:
        text = str(value or "").strip().casefold()
        if text.endswith(":latest"):
            text = text[: -len(":latest")]
        return text

    a, b = norm(left), norm(right)
    return bool(a) and a == b


def _names(payload, *keys) -> list:
    """从 /api/ps 或 /v1/models 的返回里取模型名。"""
    if not isinstance(payload, dict):
        return []
    for key in keys:
        items = payload.get(key)
        if isinstance(items, list):
            names = []
            for item in items:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("model") or item.get("id")
                else:
                    name = item
                text = str(name or "").strip()
                if text:
                    names.append(text)
            return names
    return []


def endpoint_urls(base_url: str) -> tuple:
    """把配置里的 base_url 拆成 (原生 /api/ps, OpenAI 兼容 /v1/models)。

    配置里的本地端点通常写成 http://host:11434/v1（OpenAI 兼容风格），
    但 Ollama 的原生接口在根路径下，所以要先把结尾的 /v1 去掉再用。
    """
    base = str(base_url or "").strip().rstrip("/")
    if base.endswith("/v1"):
        root, openai = base[: -len("/v1")], base + "/models"
    else:
        root, openai = base, base + "/v1/models"
    return (root + "/api/ps" if root else ""), openai


def probe_models(base_url: str, *, api_key: str = "", timeout: float | None = None) -> tuple:
    """返回 (已加载到显存的模型, 端点已安装的模型)。任一失败就是空列表。"""
    ps_url, models_url = endpoint_urls(base_url)
    if not ps_url and not models_url:
        return [], []
    wait = probe_timeout() if timeout is None else max(0.3, float(timeout))
    headers = {}
    if str(api_key or "").strip():
        headers["Authorization"] = "Bearer %s" % str(api_key).strip()
    loaded, installed = [], []
    session = requests.Session()
    session.trust_env = False          # 内网端点不要走本机代理
    try:
        try:
            response = session.get(ps_url, headers=headers, timeout=wait)
            if response.status_code == 200:
                loaded = _names(response.json(), "models")
        except Exception:
            loaded = []
        try:
            response = session.get(models_url, headers=headers, timeout=wait)
            if response.status_code == 200:
                installed = _names(response.json(), "data", "models")
        except Exception:
            installed = []
    finally:
        session.close()
    return loaded, installed


def choose_model(configured: str, loaded: list, installed: list) -> str:
    """按上面的规则挑选要用的模型 id；拿不准就返回配置值。"""
    target = str(configured or "").strip()
    loaded = [str(item or "").strip() for item in (loaded or []) if str(item or "").strip()]
    installed = [str(item or "").strip() for item in (installed or []) if str(item or "").strip()]
    if target and any(same_model(target, item) for item in loaded):
        return target
    if len(loaded) == 1:
        return loaded[0]
    if not loaded and len(installed) == 1 and target and not same_model(target, installed[0]):
        return installed[0]
    return target


def resolve_local_model(base_url: str, configured_model: str, *, api_key: str = "",
                        timeout: float | None = None) -> str:
    """解析本地端点当前该用的模型 id（带缓存）。异常时一律返回配置值。"""
    target = str(configured_model or "").strip()
    base = str(base_url or "").rstrip("/")
    if not enabled() or not base:
        return target
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(base)
    if cached and now - cached[0] < cached[2]:
        return cached[1] or target

    loaded, installed = probe_models(base, api_key=api_key, timeout=timeout)
    chosen = choose_model(target, loaded, installed)
    ok = bool(loaded or installed)
    with _CACHE_LOCK:
        _CACHE[base] = (now, chosen, cache_ttl() if ok else _FAIL_TTL_SECONDS)
    return chosen


def resolve_local_model_quiet(base_url: str, configured_model: str, *, api_key: str = "",
                              timeout: float | None = None) -> str:
    """给调用方用的安全版本：任何异常都退回配置值。"""
    try:
        return resolve_local_model(base_url, configured_model, api_key=api_key, timeout=timeout)
    except Exception:
        return str(configured_model or "").strip()


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


__all__ = [
    "choose_model", "clear_cache", "enabled", "probe_models",
    "resolve_local_model", "resolve_local_model_quiet", "same_model",
]

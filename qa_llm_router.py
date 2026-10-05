#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""部署级 LLM 端点自适应：按「是否连通 RAGFlow」在两种部署形态间自动选择。

背景：同一份代码要同时跑在两台形态不同的机器上

  * A 型：只有本地推理机（llama.cpp），没有 RAGFlow，LLM 就在本地推理机；
  * B 型：有 RAGFlow 知识库，LLM 也在 RAGFlow 那台机器上。

两型的 LLM 接入点不同，只在 data/chat_config.json 里逐台写死容易漏配，
换镜像或新装环境时会退化成代码里写死的那个地址（写死的那台在另一型机器上不可达）。
所以这里按连通性选择：

    连通 RAGFlow   → QA_LLM_BASE_URL_RAGFLOW（RAGFlow 那台的 LLM）
    未连通 RAGFlow → QA_LLM_BASE_URL_LOCAL  （本地推理机）
    两个都没配     → 原样保留调用方传入的配置（默认行为，零影响、零探测）

环境变量：
    QA_LLM_SELECTION=auto|local|ragflow  默认 auto（按连通性自动）
    QA_LLM_BASE_URL_RAGFLOW / QA_LLM_MODEL_RAGFLOW
    QA_LLM_BASE_URL_LOCAL   / QA_LLM_MODEL_LOCAL
    QA_LLM_FAILOVER=1                    首选端点探测不通时回退另一个（默认开）
    QA_LLM_PROBE_TIMEOUT=1.2             单次探测超时秒数
    QA_LLM_PROBE_TTL_SECONDS=60          探测结果缓存秒数（失败缓存 15 秒）

「连通」= RAGFLOW_BASE_URL 与 RAGFLOW_API_KEY 都配了，且 GET /api/v1/datasets 返回 200。
探测结果按地址缓存，稳态下每个端点每分钟最多一次探测；两个 QA_LLM_BASE_URL_* 都没配时
一次探测都不会发（直接返回叫用方传入的配置）。

模型名：QA_LLM_MODEL_* 没配时沿用调用方传入的 model_id；之后仍会经过
local_model_router 的「跟随端点唯一模型」逻辑，单模型服务端不会被写死的名字带偏。
"""
from __future__ import annotations

import logging
import os
import threading
import time

import requests

_LOG = logging.getLogger(__name__)

_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_FAIL_TTL_SECONDS = 15.0
_LAST_DECISION: dict = {}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().casefold() not in {"0", "false", "no", "off", ""}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def selection_mode() -> str:
    """auto（默认，按连通性）/ local（强制本地）/ ragflow（强制 RAGFlow 那台）。"""
    raw = str(os.getenv("QA_LLM_SELECTION", "auto") or "").strip().casefold()
    return raw if raw in {"auto", "local", "ragflow"} else "auto"


def failover_enabled() -> bool:
    return _env_bool("QA_LLM_FAILOVER", True)


def probe_timeout() -> float:
    # 内网探测正常在毫秒级；两个端点都挂时这一步最多拖两次超时，所以压到 1.2s。
    return max(0.3, _env_float("QA_LLM_PROBE_TIMEOUT", 1.2))


def cache_ttl() -> float:
    return max(5.0, _env_float("QA_LLM_PROBE_TTL_SECONDS", 60.0))


def _peek(key: str):
    """只读缓存，不触发探测：给纯配置读取路径用（pack_runtime 等）。"""
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit and now - hit[0] < hit[1]:
        return hit[2]
    return None


def _cached(key: str, producer):
    """producer() 返回 (是否成功, 结果)；成功缓存 cache_ttl()，失败缓存 15s。"""
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit and now - hit[0] < hit[1]:
        return hit[2]
    ok, value = producer()
    with _CACHE_LOCK:
        _CACHE[key] = (now, cache_ttl() if ok else _FAIL_TTL_SECONDS, value)
    return value


def _env_endpoint(suffix: str) -> tuple:
    url = str(os.getenv("QA_LLM_BASE_URL_" + suffix, "") or "").strip().rstrip("/")
    model = str(os.getenv("QA_LLM_MODEL_" + suffix, "") or "").strip()
    return url, model


def ragflow_credentials() -> tuple:
    """RAGFlow 接入地址与密钥（.env 优先，其次 config 模块）。"""
    base = str(os.getenv("RAGFLOW_BASE_URL", "") or "").strip().rstrip("/")
    key = str(os.getenv("RAGFLOW_API_KEY", "") or "").strip()
    if not base or not key:
        try:
            import config

            base = base or str(getattr(config, "RAGFLOW_BASE_URL", "") or "").strip().rstrip("/")
            key = key or str(getattr(config, "RAGFLOW_API_KEY", "") or "").strip()
        except Exception:
            pass
    return base, key


def _probe_ragflow(base: str, key: str) -> bool:
    session = requests.Session()
    session.trust_env = False          # 内网端点不要走本机代理
    try:
        response = session.get(
            base + "/api/v1/datasets",
            headers={"Authorization": "Bearer %s" % key},
            params={"page": 1, "page_size": 1},
            timeout=probe_timeout(),
        )
        return response.status_code == 200
    except Exception:
        return False
    finally:
        session.close()


def ragflow_reachable(*, probe: bool = True) -> bool:
    base, key = ragflow_credentials()
    if not base or not key:
        return False
    key_name = "ragflow:" + base
    if not probe:
        hit = _peek(key_name)
        return bool(hit) if hit is not None else False

    def check() -> tuple:
        ok = _probe_ragflow(base, key)
        return ok, ok

    return bool(_cached(key_name, check))


def ragflow_connected(*, probe: bool = True) -> bool:
    """当前部署是否连通 RAGFlow（决定用哪台的 LLM）。"""
    mode = selection_mode()
    if mode == "ragflow":
        return True
    if mode == "local":
        return False
    return ragflow_reachable(probe=probe)


def endpoint_alive(base_url: str, *, api_key: str = "") -> bool:
    """LLM 端点是否可用：探测 /v1/models（llama.cpp/Ollama 都支持）。"""
    url = str(base_url or "").strip().rstrip("/")
    if not url:
        return False

    def probe() -> tuple:
        from local_model_router import probe_models

        loaded, installed = probe_models(url, api_key=api_key, timeout=probe_timeout())
        alive = bool(loaded or installed)
        return alive, alive

    return bool(_cached("llm:" + url, probe))


def _preferred_key(*, probe: bool = True) -> str:
    """先选哪个端点：'ragflow' 或 'local'。"""
    mode = selection_mode()
    if mode == "ragflow":
        return "ragflow"
    if mode == "local":
        return "local"
    return "ragflow" if ragflow_connected(probe=probe) else "local"


def _pick(primary_key: str) -> tuple:
    """取首选端点 (名字, 地址, 模型)；首选没配就用备选，两个都没配返回空地址。"""
    ragflow = ("ragflow",) + _env_endpoint("RAGFLOW")
    local = ("local",) + _env_endpoint("LOCAL")
    first, second = (ragflow, local) if primary_key == "ragflow" else (local, ragflow)
    if first[1]:
        return first
    return second if second[1] else first


def static_endpoint(default_base_url: str = "", default_model: str = "") -> tuple:
    """不主动探测的部署级默认端点，供纯配置读取路径使用（pack_runtime、关键词提炼等）。

    只读已有的连通性缓存：没探测过就按「未连通」处理——本地推理机是两种形态的公共底线。
    """
    try:
        _, url, model = _pick(_preferred_key(probe=False))
    except Exception:
        url, model = "", ""
    return (url or default_base_url, model or default_model)


def _record(source: str, base_url: str, model_id: str) -> None:
    with _CACHE_LOCK:
        changed = _LAST_DECISION.get("source") != source or _LAST_DECISION.get("base_url") != base_url
        _LAST_DECISION.clear()
        _LAST_DECISION.update({"source": source, "base_url": base_url, "model_id": model_id})
    if changed:
        _LOG.info("LLM 端点自适应: source=%s base_url=%s model=%s", source, base_url, model_id)


def resolve_llm_endpoint(configured_base_url: str, configured_model: str, *, api_key: str = "") -> tuple:
    """返回 (base_url, model_id, source)。任何异常都退回调用方传入的配置。"""
    configured_base = str(configured_base_url or "").strip().rstrip("/")
    configured_model = str(configured_model or "").strip()
    try:
        if not _env_endpoint("RAGFLOW")[0] and not _env_endpoint("LOCAL")[0]:
            return configured_base, configured_model, "configured"   # 没开自适应，零探测
        primary_key = _preferred_key()
        backup_key = "local" if primary_key == "ragflow" else "ragflow"
        key, url, model = _pick(primary_key)
        model = model or configured_model
        if url and endpoint_alive(url, api_key=api_key):
            _record(key, url, model)
            return url, model, key
        if failover_enabled():
            alt_key, alt_url, alt_model = _pick(backup_key)
            if alt_url and alt_url != url and endpoint_alive(alt_url, api_key=api_key):
                source = ("failover:" + alt_key) if url else alt_key
                _record(source, alt_url, alt_model or configured_model)
                return alt_url, alt_model or configured_model, source
        if url:
            _record(key, url, model)     # 首选没配/不通也按首选走，让故障暴露在原位
            return url, model, key
        return configured_base, configured_model, "configured"
    except Exception:
        return configured_base, configured_model, "configured"


def last_decision() -> dict:
    with _CACHE_LOCK:
        return dict(_LAST_DECISION)


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
        _LAST_DECISION.clear()


__all__ = [
    "cache_ttl", "clear_cache", "endpoint_alive", "failover_enabled", "last_decision",
    "probe_timeout", "ragflow_connected", "ragflow_credentials", "ragflow_reachable",
    "resolve_llm_endpoint", "selection_mode", "static_endpoint",
]

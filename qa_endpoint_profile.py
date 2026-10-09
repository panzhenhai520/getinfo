#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 6-5 · 按**端点能力**推导超时（不再写死 8 / 120 / 180 秒）。

为什么必须做：默认值写死 8 秒时，本地大模型（共享 GPU、llama.cpp 只有 2 个槽位）
几乎必然超时 → 每个 stage 都降级。B 机的事故就是这样：`.env` 没设
`QA_LEVEL1_LOCAL_TIMEOUT_SECONDS`，落到代码默认 8s，**该机器此前 AI 回答从未真正调用过模型**。

推导口径（可解释、可复现）：
  1. 端点类型：私网/回环地址 → `local`；公网 https → `remote`；
  2. 探测能力：llama.cpp 问 `/props`（拿 `total_slots`、`n_ctx`），否则问 `/v1/models` 并计时；
  3. 惩罚系数：槽位越少越慢（1 槽 ×2.0、2 槽 ×1.5、≥4 槽 ×1.0），上下文越大越慢（<8k ×1.5）；
  4. 上限下限夹住，任何异常都退回"按类型的保守默认"，绝不让推导失败把请求变成无超时。

显式环境变量**永远优先**（`QA_ENDPOINT_*` 或调用点自己的 env），所以生产上已经调好的机器
行为不变；只是"没配的机器"不再踩 8 秒的坑。
"""
from __future__ import annotations

import json
import time
from typing import Dict, Optional
from urllib.parse import urlparse

# 类型默认（探测失败时的兜底）
_KIND_DEFAULTS = {
    "local": {"first_token_seconds": 45, "request_seconds": 180, "repair_seconds": 90},
    "remote": {"first_token_seconds": 15, "request_seconds": 60, "repair_seconds": 30},
}

_PROBE_TTL_SECONDS = 600
_PROBE_CACHE: Dict[str, tuple] = {}
_PROBE_TIMEOUT_SECONDS = 2.5


def _env_int(name: str, default: int, low: int, high: int) -> int:
    import os

    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return int(default)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return int(default)
    return max(int(low), min(int(high), value))


def classify_endpoint(base_url: str, model_id: str = "") -> str:
    """端点类型：私网/本地端口算 local，其余算 remote。"""
    text = str(base_url or "").strip()
    model = str(model_id or "").casefold()
    try:
        host = (urlparse(text).hostname or "").strip().casefold()
    except Exception:
        host = ""
    if host in ("localhost", "127.0.0.1", "::1", "host.docker.internal"):
        return "local"
    if host.startswith(("10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
                        "172.2", "172.30.", "172.31.")):
        return "local"
    if host.endswith(".local") or host.startswith("llama") or host.startswith("ollama"):
        return "local"
    if any(marker in model for marker in ("uncensored", "gguf", "qwen3.8", "cosyvoice")):
        # 名字带本地权重特征的模型，即使走公网反代也按本地能力估算
        return "local"
    return "remote"


def _probe_capability(base_url: str) -> Dict:
    """探测端点能力：llama.cpp 的 /props（槽位/上下文），失败则退回 /v1/models 计时。"""
    base = str(base_url or "").rstrip("/")
    if not base:
        return {}
    cache_key = base
    cached = _PROBE_CACHE.get(cache_key)
    now = time.monotonic()
    if cached and (now - cached[0]) < _PROBE_TTL_SECONDS:
        return dict(cached[1])

    evidence: Dict = {}
    root = base[:-3] if base.endswith("/v1") else base  # http://host:port/v1 → http://host:port
    try:
        import requests

        session = requests.Session()
        session.trust_env = False
        started = time.perf_counter()
        try:
            response = session.get(root + "/props", timeout=_PROBE_TIMEOUT_SECONDS)
            evidence["round_trip_ms"] = round((time.perf_counter() - started) * 1000, 1)
            if response.status_code == 200:
                props = response.json()
                if isinstance(props, dict):
                    evidence["engine"] = "llama.cpp"
                    if props.get("total_slots") is not None:
                        evidence["total_slots"] = int(props.get("total_slots") or 0)
                    settings = props.get("default_generation_settings") or {}
                    if isinstance(settings, dict) and settings.get("n_ctx"):
                        evidence["n_ctx"] = int(settings["n_ctx"])
                    if props.get("model_path"):
                        evidence["model"] = str(props["model_path"]).split("/")[-1]
        except Exception:
            pass
        if not evidence.get("engine"):
            started = time.perf_counter()
            response = session.get(base + "/models", timeout=_PROBE_TIMEOUT_SECONDS)
            evidence["round_trip_ms"] = round((time.perf_counter() - started) * 1000, 1)
            if response.status_code == 200:
                body = response.json()
                data = body.get("data") if isinstance(body, dict) else None
                if isinstance(data, list) and data:
                    evidence["engine"] = "openai-compatible"
                    evidence["models"] = len(data)
    except Exception:
        pass

    _PROBE_CACHE[cache_key] = (now, dict(evidence))
    return evidence


def _penalties(kind: str, evidence: Dict) -> tuple:
    slots = evidence.get("total_slots")
    if slots is None:
        slot_penalty = 1.5 if kind == "local" else 1.0
    elif slots <= 1:
        slot_penalty = 2.0
    elif slots == 2:
        slot_penalty = 1.5
    elif slots <= 3:
        slot_penalty = 1.2
    else:
        slot_penalty = 1.0

    ctx = evidence.get("n_ctx")
    if ctx is None:
        ctx_penalty = 1.0
    elif ctx < 8192:
        ctx_penalty = 1.5
    elif ctx < 32768:
        ctx_penalty = 1.25
    else:
        ctx_penalty = 1.0
    return slot_penalty, ctx_penalty


def endpoint_profile(base_url: str, model_id: str = "", *, probe: bool = True) -> Dict:
    """返回该端点的超时配置：{kind, first_token_seconds, request_seconds, repair_seconds, source, evidence}。

    优先级：显式 `QA_ENDPOINT_*` 环境变量 > 端点探测推导 > 按类型的保守默认。
    """
    kind = classify_endpoint(base_url, model_id)
    evidence: Dict = {}
    source = "kind_default"
    if probe:
        try:
            evidence = _probe_capability(base_url)
        except Exception:
            evidence = {}

    defaults = _KIND_DEFAULTS[kind]
    base_request = 120 if kind == "local" else 60
    slot_penalty, ctx_penalty = _penalties(kind, evidence)
    request_seconds = int(round(base_request * slot_penalty * ctx_penalty))
    if evidence:
        source = "probe"
    first_token_seconds = int(round(request_seconds * 0.35))
    repair_seconds = int(round(request_seconds * 0.5))

    request_seconds = max(defaults["request_seconds"] if source == "kind_default" else 30,
                          min(600, request_seconds))
    first_token_seconds = max(8, min(240, first_token_seconds))
    repair_seconds = max(5, min(300, repair_seconds))

    if source == "kind_default":
        request_seconds = defaults["request_seconds"]
        first_token_seconds = defaults["first_token_seconds"]
        repair_seconds = defaults["repair_seconds"]

    profile = {
        "kind": kind,
        "first_token_seconds": first_token_seconds,
        "request_seconds": request_seconds,
        "repair_seconds": repair_seconds,
        "source": source,
        "evidence": evidence,
    }
    # 显式环境变量永远优先（生产上已调好的机器行为不变）
    explicit_request = _env_int("QA_ENDPOINT_REQUEST_TIMEOUT_SECONDS", 0, 0, 3600)
    if explicit_request:
        profile["request_seconds"] = explicit_request
        profile["source"] = "env"
    explicit_first = _env_int("QA_ENDPOINT_FIRST_TOKEN_TIMEOUT_SECONDS", 0, 0, 3600)
    if explicit_first:
        profile["first_token_seconds"] = explicit_first
        profile["source"] = "env"
    explicit_repair = _env_int("QA_ENDPOINT_REPAIR_TIMEOUT_SECONDS", 0, 0, 3600)
    if explicit_repair:
        profile["repair_seconds"] = explicit_repair
        profile["source"] = "env"
    return profile


def describe(profile: Optional[Dict]) -> str:
    """给人看的超时说明（写日志/诊断用）。"""
    if not profile:
        return "端点超时=未知"
    evidence = profile.get("evidence") or {}
    detail = ""
    if evidence.get("total_slots") is not None:
        detail = "，槽位 %s" % evidence["total_slots"]
    if evidence.get("n_ctx") is not None:
        detail += "，n_ctx %s" % evidence["n_ctx"]
    return "端点类型=%s（%s%s）→ 首字 %ss / 整体 %ss / 修复 %ss" % (
        profile.get("kind"), profile.get("source"), detail,
        profile.get("first_token_seconds"), profile.get("request_seconds"),
        profile.get("repair_seconds"))


__all__ = ["endpoint_profile", "classify_endpoint", "describe"]

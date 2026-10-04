#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stable, actionable errors for model, proxy, and RAGFlow failures."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import requests


@dataclass(frozen=True)
class QaAction:
    label: str
    href: str = ""
    action: str = ""


@dataclass(frozen=True)
class QaPublicError:
    code: str
    message: str
    retryable: bool
    provider_id: str
    stage: str
    actions: tuple[QaAction, ...]

    def to_event_payload(self, *, trace_id: str = "") -> dict:
        value = asdict(self)
        value["actions"] = [asdict(item) for item in self.actions]
        value["trace_id"] = str(trace_id or "")
        return value


def _settings(provider_id: str, section: str = "providers") -> QaAction:
    return QaAction(
        "打开 AI 与代理设置" if section == "network" else "打开模型设置",
        href=f"/my-ai-settings?section={section}&provider={provider_id}",
    )


def _status(exc: BaseException) -> int:
    try:
        direct = int(getattr(exc, "status_code", 0) or 0)
        if direct:
            return direct
    except (TypeError, ValueError):
        pass
    response = getattr(exc, "response", None)
    try:
        return int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        return 0


def classify_qa_error(
    exc: BaseException,
    *,
    provider_id: str = "",
    stage: str = "",
    use_proxy: bool = False,
    proxy_configured: bool = False,
) -> QaPublicError:
    provider = str(provider_id or "模型")
    status = _status(exc)
    text = str(exc or "").casefold()

    if isinstance(exc, PermissionError) or status in {401, 403}:
        return QaPublicError(
            "API_KEY_INVALID",
            f"{provider} 的 API Key 无效、已过期或无权访问当前模型。",
            False,
            provider,
            stage,
            (_settings(provider), QaAction("切换模型", action="open_model_picker")),
        )
    if status == 404 or "model not found" in text or "unknown model" in text:
        return QaPublicError(
            "MODEL_NOT_FOUND",
            f"{provider} 的模型 ID 不存在或当前账号无权使用。",
            False,
            provider,
            stage,
            (_settings(provider), QaAction("切换模型", action="open_model_picker")),
        )
    if status == 429:
        quota = any(marker in text for marker in ("quota", "balance", "credit", "insufficient"))
        return QaPublicError(
            "QUOTA_EXHAUSTED" if quota else "RATE_LIMITED",
            "API 配额或余额不足，请检查供应商账户。" if quota else "请求过于频繁，请稍后重试。",
            not quota,
            provider,
            stage,
            (_settings(provider), QaAction("稍后重试", action="retry_stage")),
        )
    if isinstance(exc, requests.exceptions.ProxyError) or "socks" in text or "proxy" in text:
        configured = bool(use_proxy and proxy_configured)
        return QaPublicError(
            "PROXY_UNAVAILABLE" if configured else "PROXY_REQUIRED",
            "已配置的代理无法连接，请检查代理地址或认证信息。"
            if configured
            else f"{provider} 当前无法从此服务器直接访问，可能需要配置代理。",
            True,
            provider,
            stage,
            (_settings(provider, "network"), QaAction("切换模型", action="open_model_picker")),
        )
    if isinstance(exc, requests.exceptions.Timeout) or "timed out" in text or "timeout" in text:
        code = "RESEARCH_TIMEOUT" if stage.startswith("level2") else "NETWORK_UNREACHABLE"
        message = (
            "RAG增强检索响应超时，系统已保留现有证据并继续生成回答。"
            if code == "RESEARCH_TIMEOUT"
            else f"{provider} 响应较慢，系统将自动改用证据约束结果继续。"
        )
        return QaPublicError(code, message, True, provider, stage, (QaAction("重试当前阶段", action="retry_stage"),))
    if "ragflow" in text:
        return QaPublicError(
            "RAGFLOW_UNAVAILABLE",
            "RAG增强检索暂不可用，系统将保留现有证据继续回答。",
            True,
            provider,
            stage,
            (QaAction("重试二级研究", action="retry_stage"),),
        )
    return QaPublicError(
        "NETWORK_UNREACHABLE" if isinstance(exc, requests.exceptions.ConnectionError) else "INTERNAL_ERROR",
        f"暂时无法连接 {provider}，请检查网络或服务地址后重试。"
        if isinstance(exc, requests.exceptions.ConnectionError)
        else "系统暂时无法完成本次问答，请稍后重试并向管理员提供追踪编号。",
        True,
        provider,
        stage,
        (QaAction("重试当前阶段", action="retry_stage"),),
    )


def missing_api_key_error(provider_id: str, *, stage: str = "") -> QaPublicError:
    provider = str(provider_id or "模型")
    return QaPublicError(
        "API_KEY_MISSING",
        f"尚未配置 {provider} 的 API Key。",
        False,
        provider,
        stage,
        (_settings(provider), QaAction("切换模型", action="open_model_picker")),
    )


__all__ = ["QaAction", "QaPublicError", "classify_qa_error", "missing_api_key_error"]

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unified-QA trust-boundary validation and sensitive-data filtering."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from typing import Any, Iterable
from urllib.parse import urlsplit


SENSITIVE_KEYS = frozenset({
    "api_key", "apikey", "authorization", "cookie", "password", "proxy_password",
    "secret", "token", "access_token", "refresh_token", "qa_bridge_session",
})
SAFE_TOOL_NAMES = frozenset({"article_search", "web_search", "ragflow_search", "ragflow_research"})
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_KEY_RE = re.compile(r"(?i)\b(api[_-]?key|token|password|secret|cookie)\s*[:=]\s*([^\s,;]{4,})")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class QaSecurityError(ValueError):
    pass


def sanitize_log_text(value: Any, *, limit: int = 2000, secrets: Iterable[str] = ()) -> str:
    text = _CONTROL_RE.sub(" ", str(value or ""))
    for secret in secrets:
        clean = str(secret or "")
        if len(clean) >= 4:
            text = text.replace(clean, "[REDACTED]")
    text = _BEARER_RE.sub(r"\1[REDACTED]", text)
    text = _KEY_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    return text[: max(1, int(limit))]


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if str(key).casefold() in SENSITIVE_KEYS else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    if isinstance(value, str):
        return sanitize_log_text(value)
    return value


def _allowed_hosts(extra: Iterable[str] = ()) -> set[str]:
    env = os.getenv("QA_ALLOWED_OUTBOUND_HOSTS", "")
    return {
        str(item).strip().casefold().rstrip(".")
        for item in [*extra, *env.split(",")]
        if str(item).strip()
    }


def validate_outbound_url(
    value: str,
    *,
    allowed_hosts: Iterable[str] = (),
    allow_private_for_allowlist: bool = False,
    resolver=socket.getaddrinfo,
) -> str:
    """Reject unsafe schemes, credentials and private/dynamic destinations.

    A server-owned allowlist may explicitly permit a private model endpoint;
    untrusted URLs never receive that exemption.
    """
    raw = str(value or "").strip()
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise QaSecurityError("服务地址格式无效。") from exc
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        raise QaSecurityError("服务地址只允许 HTTP/HTTPS。")
    if parts.username or parts.password:
        raise QaSecurityError("服务地址不能包含用户名或密码。")
    host = parts.hostname.casefold().rstrip(".")
    allowlist = _allowed_hosts(allowed_hosts)
    explicitly_allowed = host in allowlist
    if host in {"localhost", "localhost.localdomain"} and not (explicitly_allowed and allow_private_for_allowlist):
        raise QaSecurityError("服务地址不能指向本机或内网地址。")
    try:
        port = parts.port or (443 if parts.scheme.casefold() == "https" else 80)
    except ValueError as exc:
        raise QaSecurityError("服务端口无效。") from exc
    addresses = set()
    try:
        addresses.add(ipaddress.ip_address(host))
    except ValueError:
        try:
            for record in resolver(host, port, type=socket.SOCK_STREAM):
                addresses.add(ipaddress.ip_address(record[4][0]))
        except (OSError, ValueError) as exc:
            raise QaSecurityError("服务地址无法解析。") from exc
    if not addresses:
        raise QaSecurityError("服务地址没有可用 IP。")
    if any(not address.is_global for address in addresses):
        if not (explicitly_allowed and allow_private_for_allowlist):
            raise QaSecurityError("服务地址解析到本机、内网或保留地址，已阻止访问。")
    return raw.rstrip("/")


def validate_proxy_url(value: str, *, allowed_hosts: Iterable[str] = ()) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise QaSecurityError("代理地址格式无效。") from exc
    if parts.scheme.casefold() not in {"http", "https", "socks5", "socks5h"} or not parts.hostname:
        raise QaSecurityError("代理地址只允许 HTTP、HTTPS 或 SOCKS5。")
    allowed = {
        item.strip().casefold() for item in os.getenv("QA_ALLOWED_PROXY_HOSTS", "127.0.0.1,localhost").split(",") if item.strip()
    }
    allowed.update(str(item).strip().casefold() for item in allowed_hosts if str(item).strip())
    if parts.hostname.casefold().rstrip(".") not in allowed:
        raise QaSecurityError("代理主机不在管理员允许列表中。")
    try:
        if not parts.port:
            raise QaSecurityError("代理地址必须包含端口。")
    except ValueError as exc:
        raise QaSecurityError("代理端口无效。") from exc
    return raw


def untrusted_block(value: Any, *, limit: int = 12000) -> str:
    clean = _CONTROL_RE.sub(" ", str(value or ""))[:limit]
    return "<UNTRUSTED_DATA>\n" + clean + "\n</UNTRUSTED_DATA>"


def ensure_allowed_tool(name: str) -> str:
    normalized = str(name or "").strip().casefold()
    if normalized not in SAFE_TOOL_NAMES:
        raise QaSecurityError("模型请求了未授权工具。")
    return normalized


__all__ = [
    "QaSecurityError", "SAFE_TOOL_NAMES", "ensure_allowed_tool", "redact_sensitive",
    "sanitize_log_text", "untrusted_block", "validate_outbound_url", "validate_proxy_url",
]

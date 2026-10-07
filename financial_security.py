#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Shared public-output and untrusted-content controls for financial features."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import config


FINANCIAL_SECURITY_VERSION = "financial-security-v1"
SENSITIVE_QUERY_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_key",
        "key",
        "token",
        "access_token",
        "auth_token",
        "password",
        "passwd",
        "secret",
        "signature",
        "sig",
        "session_token",
    }
)
UNTRUSTED_EXTERNAL_CONTENT_POLICY = {
    "classification": "untrusted_external_data",
    "instructions_allowed": False,
    "configuration_mutation_allowed": False,
    "tool_calls_allowed": False,
    "order_execution_allowed": False,
}
_SECRET_SETTING_SUFFIXES = (
    "_API_KEY",
    "_TOKEN",
    "_PASSWORD",
    "_SECRET",
    "SECRET_KEY",
)
_SENSITIVE_NAME = (
    r"api[_-]?key|apikey|access[_-]?key|key|token|access[_-]?token|"
    r"auth[_-]?token|password|passwd|secret|signature|sig|session[_-]?token"
)


def configured_secret_values(settings=None) -> tuple[str, ...]:
    source = config if settings is None else settings
    names = source.keys() if isinstance(source, Mapping) else dir(source)
    values = []
    for name in names:
        normalized = str(name or "").upper()
        if not any(normalized.endswith(suffix) for suffix in _SECRET_SETTING_SUFFIXES):
            continue
        try:
            value = source.get(name) if isinstance(source, Mapping) else getattr(source, name)
        except Exception:
            continue
        if isinstance(value, str) and len(value.strip()) >= 6:
            values.append(value.strip())
    return tuple(sorted(set(values), key=len, reverse=True))


def redact_sensitive_text(
    value: object,
    *,
    secrets: Sequence[object] = (),
    settings=None,
    configured: Sequence[str] | None = None,
    maximum: int = 20000,
    collapse_controls: bool = False,
) -> str:
    """脱敏单段文本。

    ``configured`` 用于传入"已经算好的已配置密钥值集合"：`configured_secret_values()` 要遍历
    settings 的全部键名并逐个做后缀匹配，单次约 1.5ms；一次 feed 构建里有上千个字符串字段，
    每个字段都重算一遍会白烧掉大半构建时间（实测 120 条记录的有效载荷 3.0 秒里几乎全是它）。
    批处理入口（redact_public_payload）因此只算一次、往下传；单段调用不传时行为不变。
    """
    text = str(value or "")
    explicit = [str(item) for item in secrets if str(item or "")]
    known = configured if configured is not None else configured_secret_values(settings)
    for secret in sorted(set((*explicit, *known)), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(
        rf"([?&](?:{_SENSITIVE_NAME})=)[^&#\s\"']+",
        r"\1[REDACTED]",
        text,
        flags=re.I,
    )
    text = re.sub(
        rf"((?:\"|')?(?:{_SENSITIVE_NAME})(?:\"|')?\s*[:=]\s*(?:\"|')?)[^\"'\s,;&}}]+",
        r"\1[REDACTED]",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"(authorization\s*[:=]\s*(?:bearer|basic)\s+)[^\s,;\"']+",
        r"\1[REDACTED]",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"(\bbearer\s+)[A-Za-z0-9._~+/=-]{6,}",
        r"\1[REDACTED]",
        text,
        flags=re.I,
    )
    if collapse_controls:
        text = re.sub(r"[\r\n\t]+", " ", text)
    return text[: max(0, int(maximum))]


def redact_public_payload(value, *, settings=None, depth: int = 0, configured=None):
    """递归脱敏任意 JSON 结构。

    密钥值集合只在最外层算一次再往下传（见 redact_sensitive_text 的说明）：
    不做跨调用缓存，避免管理员中途改了密钥、缓存却还留着旧值导致漏脱敏。
    """
    if depth > 12:
        return "[TRUNCATED]"
    if depth == 0 and configured is None:
        configured = configured_secret_values(settings)
    if isinstance(value, Mapping):
        return {
            str(key): redact_public_payload(item, settings=settings, depth=depth + 1, configured=configured)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            redact_public_payload(item, settings=settings, depth=depth + 1, configured=configured)
            for item in value
        ]
    if isinstance(value, str):
        return redact_sensitive_text(value, settings=settings, configured=configured)
    return value


def safe_public_url(value: object, *, local_prefixes: Sequence[str] = ()) -> str:
    candidate = str(value or "").strip()[:2000]
    if not candidate:
        return ""
    parsed = urlsplit(candidate)
    if not parsed.scheme and not parsed.netloc:
        if (
            parsed.path.startswith("/")
            and not parsed.path.startswith("//")
            and any(parsed.path.startswith(prefix) for prefix in local_prefixes)
        ):
            query = urlencode(
                [
                    (key, item)
                    for key, item in parse_qsl(parsed.query, keep_blank_values=True)
                    if key.casefold() not in SENSITIVE_QUERY_KEYS
                ]
            )
            return urlunsplit(("", "", parsed.path, query, ""))
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username or parsed.password:
        return ""
    host = parsed.hostname.rstrip(".").casefold()
    if host == "localhost" or host.endswith(".localhost"):
        return ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    display_host = f"[{host}]" if ":" in host else host
    netloc = f"{display_host}:{port}" if port else display_host
    query = urlencode(
        [
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if key.casefold() not in SENSITIVE_QUERY_KEYS
        ]
    )
    return urlunsplit((parsed.scheme.casefold(), netloc, parsed.path or "/", query, ""))


def untrusted_external_content_policy() -> dict:
    return dict(UNTRUSTED_EXTERNAL_CONTENT_POLICY)


__all__ = [
    "FINANCIAL_SECURITY_VERSION",
    "SENSITIVE_QUERY_KEYS",
    "UNTRUSTED_EXTERNAL_CONTENT_POLICY",
    "configured_secret_values",
    "redact_public_payload",
    "redact_sensitive_text",
    "safe_public_url",
    "untrusted_external_content_policy",
]

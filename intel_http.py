#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Bounded HTTP fetching and SSRF controls for intelligence scanners."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Iterable, List, Optional
from urllib.parse import urljoin, urlsplit

import requests

import config
from financial_security import redact_sensitive_text


class UnsafeExternalURLError(ValueError):
    pass


class ExternalFetchError(RuntimeError):
    pass


def _allowlist_items(value: str = "") -> List[str]:
    raw = value if value != "" else config.INTEL_PRIVATE_NETWORK_ALLOWLIST
    return [item.strip().casefold() for item in str(raw or "").split(",") if item.strip()]


def _host_allowlisted(host: str, address: ipaddress._BaseAddress, allowlist: Iterable[str]) -> bool:
    normalized_host = host.casefold().rstrip(".")
    for item in allowlist:
        if "/" in item:
            try:
                if address in ipaddress.ip_network(item, strict=False):
                    return True
            except ValueError:
                continue
        if item == str(address) or normalized_host == item:
            return True
        if item.startswith(".") and normalized_host.endswith(item):
            return True
    return False


def validate_external_url(
    value: str,
    *,
    allowlist: str = "",
    resolver=socket.getaddrinfo,
) -> str:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise UnsafeExternalURLError("外部网址只允许完整 HTTP(S) URL")
    if parsed.username or parsed.password:
        raise UnsafeExternalURLError("外部网址不得包含用户凭据")
    host = parsed.hostname.rstrip(".").casefold()
    try:
        addresses = {
            item[4][0]
            for item in resolver(host, parsed.port or (443 if parsed.scheme == "https" else 80))
        }
    except (OSError, socket.gaierror) as exc:
        raise UnsafeExternalURLError("外部网址域名解析失败") from exc
    if not addresses:
        raise UnsafeExternalURLError("外部网址未解析到 IP")
    allowed = _allowlist_items(allowlist)
    for raw_address in addresses:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as exc:
            raise UnsafeExternalURLError("外部网址解析到无效 IP") from exc
        prohibited = (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        )
        if prohibited and not _host_allowlisted(host, address, allowed):
            raise UnsafeExternalURLError("外部网址解析到受限网络地址")
    return parsed.geturl()


def sanitize_external_error(error, *, secrets: Iterable[str] = ()) -> str:
    return redact_sensitive_text(
        error,
        secrets=tuple(secrets),
        maximum=500,
        collapse_controls=True,
    )


@dataclass(frozen=True)
class HTTPFetchResult:
    url: str
    status_code: int
    content: bytes
    content_type: str
    encoding: str

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8", errors="replace")


class SafeHTTPClient:
    def __init__(
        self,
        *,
        session=None,
        resolver=socket.getaddrinfo,
        allowlist: str = "",
    ):
        self.session = session or requests.Session()
        self.resolver = resolver
        self.allowlist = allowlist

    def get(
        self,
        url: str,
        *,
        headers: Optional[dict] = None,
        timeout: Optional[tuple] = None,
    ) -> HTTPFetchResult:
        """抓取一个外部 URL。

        timeout=(连接秒, 读取秒)，默认走扫描器口径（INTEL_SCAN_*_TIMEOUT_SECONDS）。
        海外慢站（实测 A 机拉 finance.buzzing.cc 要 26 秒，默认 20 秒读取会直接超时）
        可以传更宽的自有预算，而不必为个别来源放宽全局扫描超时。
        """
        connect_timeout, read_timeout = timeout or (
            config.INTEL_SCAN_CONNECT_TIMEOUT_SECONDS,
            config.INTEL_SCAN_READ_TIMEOUT_SECONDS,
        )
        current = str(url or "").strip()
        max_redirects = config.INTEL_SCAN_MAX_REDIRECTS
        for redirect_index in range(max_redirects + 1):
            validate_external_url(
                current,
                allowlist=self.allowlist,
                resolver=self.resolver,
            )
            response = self.session.get(
                current,
                headers=headers or {},
                timeout=(connect_timeout, read_timeout),
                allow_redirects=False,
                stream=True,
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location", "")
                response.close()
                if not location:
                    raise ExternalFetchError("外部响应重定向缺少 Location")
                if redirect_index >= max_redirects:
                    raise ExternalFetchError("外部响应重定向次数超限")
                current = urljoin(current, location)
                continue
            try:
                response.raise_for_status()
                declared_length = int(response.headers.get("Content-Length") or 0)
                if declared_length > config.INTEL_SCAN_MAX_RESPONSE_BYTES:
                    raise ExternalFetchError("外部响应超过大小上限")
                chunks = []
                total = 0
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > config.INTEL_SCAN_MAX_RESPONSE_BYTES:
                        raise ExternalFetchError("外部响应超过大小上限")
                    chunks.append(chunk)
                encoding = response.encoding or "utf-8"
                return HTTPFetchResult(
                    url=str(response.url or current),
                    status_code=int(response.status_code),
                    content=b"".join(chunks),
                    content_type=str(response.headers.get("Content-Type") or ""),
                    encoding=encoding,
                )
            finally:
                response.close()
        raise ExternalFetchError("外部请求未完成")

    def validate_redirect_chain(self, url: str, *, headers: Optional[dict] = None) -> str:
        """Validate every hop without downloading the final response body."""
        current = str(url or "").strip()
        max_redirects = config.INTEL_SCAN_MAX_REDIRECTS
        for redirect_index in range(max_redirects + 1):
            validate_external_url(
                current,
                allowlist=self.allowlist,
                resolver=self.resolver,
            )
            try:
                response = self.session.get(
                    current,
                    headers=headers or {},
                    timeout=(
                        config.INTEL_SCAN_CONNECT_TIMEOUT_SECONDS,
                        config.INTEL_SCAN_READ_TIMEOUT_SECONDS,
                    ),
                    allow_redirects=False,
                    stream=True,
                )
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, OSError):
                # 连接被 WAF/防火墙/超时关闭（例如部分政务站对 requests 默认 TLS 指纹拒连）。
                # 这不是"危险重定向"，而是校验器本身连不上；URL 格式与 allowlist 已通过，
                # 交给上层真实抓取器（browserforge/curl-cffi 真指纹）处理，这里放行。
                return current
            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location", "")
                    if not location:
                        raise ExternalFetchError("外部响应重定向缺少 Location")
                    if redirect_index >= max_redirects:
                        raise ExternalFetchError("外部响应重定向次数超限")
                    current = urljoin(current, location)
                    continue
                # 4xx/5xx（403/406/412/429 反爬、404、500 等）不是"危险重定向"，URL 本身可达、
                # 链安全（每一跳都已过 validate_external_url）。这里放行，交给上层真实抓取器
                # （browserforge/curl-cffi 真指纹，再不行走 VPN crawl4ai→OCR 兜底）处理，
                # 而不是 raise_for_status 把反爬候选在聚合前直接判死。
                return str(response.url or current)
            finally:
                response.close()
        raise ExternalFetchError("外部重定向校验未完成")

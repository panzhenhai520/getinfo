"""Central outbound URL policy for crawler traffic.

The crawler accepts URLs from authenticated users and industry-pack data.  A
login boundary is not an SSRF boundary, so every outbound route must reject
local/private destinations and re-check redirects.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import requests


_TRUE = {"1", "true", "yes", "on"}
_REDIRECT_CODES = {301, 302, 303, 307, 308}


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in _TRUE


def _csv_env(name: str) -> set[str]:
    return {
        item.strip().casefold().rstrip(".")
        for item in os.getenv(name, "").split(",")
        if item.strip()
    }


def max_response_bytes() -> int:
    try:
        value = int(os.getenv("CRAWL_MAX_RESPONSE_BYTES", "8388608"))
    except (TypeError, ValueError):
        value = 8388608
    return max(65536, min(value, 50 * 1024 * 1024))


def read_response_bytes_limited(response: requests.Response, limit: Optional[int] = None) -> bytes:
    maximum = max_response_bytes() if limit is None else max(1, int(limit))
    raw_length = response.headers.get("Content-Length")
    if raw_length:
        try:
            if int(raw_length) > maximum:
                raise ValueError(f"response_too_large:{raw_length}>{maximum}")
        except ValueError as exc:
            if str(exc).startswith("response_too_large:"):
                raise
    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > maximum:
            raise ValueError(f"response_too_large:{total}>{maximum}")
        chunks.append(chunk)
    return b"".join(chunks)


def read_response_text_limited(response: requests.Response, limit: Optional[int] = None) -> str:
    payload = read_response_bytes_limited(response, limit=limit)
    encoding = response.encoding or "utf-8"
    return payload.decode(encoding, errors="replace")


@dataclass(frozen=True)
class UrlPolicyResult:
    allowed: bool
    normalized_url: str
    reason: str = ""
    addresses: tuple[str, ...] = ()


def _is_forbidden_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


@lru_cache(maxsize=2048)
def _resolve_host(hostname: str) -> tuple[str, ...]:
    results = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    return tuple(sorted({item[4][0] for item in results if item and item[4]}))


def clear_dns_policy_cache() -> None:
    _resolve_host.cache_clear()


def validate_outbound_url(
    url: str,
    *,
    resolve_dns: bool = True,
    allowed_private_hosts: Optional[Iterable[str]] = None,
) -> UrlPolicyResult:
    value = str(url or "").strip()
    if not value:
        return UrlPolicyResult(False, "", "empty_url")

    parsed = urlparse(value)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return UrlPolicyResult(False, value, "unsupported_scheme_or_missing_host")
    if parsed.username or parsed.password:
        return UrlPolicyResult(False, value, "userinfo_not_allowed")

    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if not hostname:
        return UrlPolicyResult(False, value, "missing_hostname")

    normalized = parsed._replace(
        scheme=parsed.scheme.casefold(),
        netloc=parsed.netloc.casefold(),
        fragment="",
    ).geturl()
    allow_private = _env_bool("CRAWL_ALLOW_PRIVATE_NETWORKS", False)
    private_hosts = _csv_env("CRAWL_ALLOWED_PRIVATE_HOSTS")
    private_hosts.update(str(item).casefold().rstrip(".") for item in (allowed_private_hosts or ()))
    explicitly_allowed = hostname in private_hosts

    if hostname == "localhost" or hostname.endswith(".localhost"):
        if not (allow_private or explicitly_allowed):
            return UrlPolicyResult(False, normalized, "localhost_not_allowed")

    try:
        literal = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None and _is_forbidden_address(str(literal)) and not (allow_private or explicitly_allowed):
        return UrlPolicyResult(False, normalized, "private_or_reserved_address", (str(literal),))

    addresses: tuple[str, ...] = ()
    if resolve_dns and literal is None:
        try:
            addresses = _resolve_host(hostname)
        except OSError as exc:
            return UrlPolicyResult(False, normalized, f"dns_resolution_failed:{type(exc).__name__}")
        if not addresses:
            return UrlPolicyResult(False, normalized, "dns_resolution_empty")
        if not (allow_private or explicitly_allowed) and any(_is_forbidden_address(item) for item in addresses):
            return UrlPolicyResult(False, normalized, "hostname_resolves_to_private_or_reserved_address", addresses)

    return UrlPolicyResult(True, normalized, addresses=addresses)


def safe_request_get(
    url: str,
    *,
    session: Optional[requests.Session] = None,
    max_redirects: int = 5,
    verify: Optional[bool] = None,
    **kwargs,
) -> requests.Response:
    """GET a URL while validating the initial and every redirect destination."""
    client = session or requests
    current = url
    # TLS verification is a deployment security policy, not a per-call escape
    # hatch.  Keep ``verify`` in the signature for compatibility with legacy
    # call sites, but only the explicit environment policy may disable it.
    verify_tls = _env_bool("CRAWL_TLS_VERIFY", True)
    kwargs.pop("allow_redirects", None)
    kwargs.pop("verify", None)

    for redirect_index in range(max_redirects + 1):
        verdict = validate_outbound_url(current, resolve_dns=True)
        if not verdict.allowed:
            raise ValueError(f"outbound_url_blocked:{verdict.reason}")
        response = client.get(
            verdict.normalized_url,
            allow_redirects=False,
            verify=verify_tls,
            **kwargs,
        )
        if response.status_code not in _REDIRECT_CODES:
            return response
        if redirect_index >= max_redirects:
            response.close()
            raise requests.TooManyRedirects(f"more than {max_redirects} redirects")
        location = response.headers.get("Location")
        if not location:
            return response
        next_url = urljoin(verdict.normalized_url, location)
        next_verdict = validate_outbound_url(next_url, resolve_dns=True)
        response.close()
        if not next_verdict.allowed:
            raise ValueError(f"outbound_redirect_blocked:{next_verdict.reason}")
        current = next_verdict.normalized_url

    raise requests.TooManyRedirects(f"more than {max_redirects} redirects")


def safe_request_head(
    url: str,
    *,
    session: Optional[requests.Session] = None,
    max_redirects: int = 5,
    verify: Optional[bool] = None,
    **kwargs,
) -> requests.Response:
    """HEAD a URL while validating every redirect destination."""
    client = session or requests
    current = url
    verify_tls = _env_bool("CRAWL_TLS_VERIFY", True)
    kwargs.pop("allow_redirects", None)
    kwargs.pop("verify", None)
    for redirect_index in range(max_redirects + 1):
        verdict = validate_outbound_url(current, resolve_dns=True)
        if not verdict.allowed:
            raise ValueError(f"outbound_url_blocked:{verdict.reason}")
        response = client.head(
            verdict.normalized_url,
            allow_redirects=False,
            verify=verify_tls,
            **kwargs,
        )
        if response.status_code not in _REDIRECT_CODES:
            return response
        if redirect_index >= max_redirects:
            response.close()
            raise requests.TooManyRedirects(f"more than {max_redirects} redirects")
        location = response.headers.get("Location")
        if not location:
            return response
        next_url = urljoin(verdict.normalized_url, location)
        next_verdict = validate_outbound_url(next_url, resolve_dns=True)
        response.close()
        if not next_verdict.allowed:
            raise ValueError(f"outbound_redirect_blocked:{next_verdict.reason}")
        current = next_verdict.normalized_url
    raise requests.TooManyRedirects(f"more than {max_redirects} redirects")


async def install_playwright_url_guard(context) -> None:
    """Block Playwright requests whose hosts resolve to local/private networks."""

    async def _guard(route):
        request_url = route.request.url
        scheme = urlparse(request_url).scheme.casefold()
        if scheme not in {"http", "https"}:
            await route.continue_()
            return
        verdict = await asyncio.to_thread(validate_outbound_url, request_url, resolve_dns=True)
        if verdict.allowed:
            await route.continue_()
        else:
            await route.abort("blockedbyclient")

    await context.route("**/*", _guard)


def install_sync_playwright_url_guard(context) -> None:
    """Synchronous Playwright equivalent of :func:`install_playwright_url_guard`."""

    def _guard(route):
        request_url = route.request.url
        scheme = urlparse(request_url).scheme.casefold()
        if scheme not in {"http", "https"}:
            route.continue_()
            return
        verdict = validate_outbound_url(request_url, resolve_dns=True)
        if verdict.allowed:
            route.continue_()
        else:
            route.abort("blockedbyclient")

    context.route("**/*", _guard)

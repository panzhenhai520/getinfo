#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Supplemental article-link discovery.

Playwright remains the primary source because it sees rendered DOM. This
module adds low-risk discovery channels that often catch articles hidden from
plain link traversal: RSS/Atom feeds, sitemap entries, robots.txt sitemap
declarations, static HTML anchors, and structured metadata.
"""

import json
import os
import re
from collections import deque
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import (
    parse_qsl,
    urlencode,
    urljoin,
    urlparse,
    urlunparse,
    unquote,
)

import requests
from outbound_url_policy import read_response_text_limited, safe_request_get
from bs4 import BeautifulSoup

import config
from crawl_options import normalize_crawl_options

try:
    import extruct

    HAS_EXTRUCT = True
except ImportError:
    extruct = None
    HAS_EXTRUCT = False

try:
    from selectolax.parser import HTMLParser

    HAS_SELECTOLAX = True
except ImportError:
    HTMLParser = None
    HAS_SELECTOLAX = False

try:
    import requests_cache

    HAS_REQUESTS_CACHE = True
except ImportError:
    requests_cache = None
    HAS_REQUESTS_CACHE = False

try:
    from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

    HAS_TENACITY = True
except ImportError:
    Retrying = None
    retry_if_exception_type = None
    stop_after_attempt = None
    wait_exponential_jitter = None
    HAS_TENACITY = False


REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

TRACKING_QUERY_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
}

BAD_EXTENSIONS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".bmp",
    ".tiff",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".zip",
    ".rar",
    ".mp3",
    ".mp4",
    ".avi",
    ".mov",
    ".wmv",
)

BAD_PATH_PARTS = (
    "/login",
    "/register",
    "/signup",
    "/search",
    "/tag/",
    "/tags/",
    "/author/",
    "/authors/",
    "/category/",
    "/categories/",
    "/privacy",
    "/terms",
    "/about",
    "/contact",
    "/static/",
    "/assets/",
    "/images/",
    "/img/",
    "/css/",
    "/js/",
)

SOFT_BAD_PATH_PARTS = (
    "/tag/",
    "/tags/",
    "/author/",
    "/authors/",
    "/category/",
    "/categories/",
)

SHORT_ACTION_TEXTS = {
    "more",
    "read more",
    "learn more",
    "view more",
    "view all",
    "details",
    "全文",
    "更多",
    "更多内容",
    "阅读更多",
    "阅读全文",
    "查看详情",
    "了解更多",
    "下一页",
    "上一页",
    "next",
    "previous",
}

TITLE_EXCLUDE_RE = re.compile(
    r"^(首页|首頁|主页|主頁|登录|登入|注册|註冊|订阅|訂閱|搜索|搜尋|菜单|導航|导航|"
    r"更多|全文|阅读全文|閱讀全文|查看详情|了解更多|上一页|下一页|next|previous|"
    r"home|login|register|subscribe|search|menu|more|read more|view all)$",
    re.IGNORECASE,
)

DATE_HINT_RE = re.compile(
    r"("
    r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}"
    r"|\d{1,2}[-/.]\d{1,2}[-/.]20\d{2}"
    r"|20\d{2}\s*/\s*\d{1,2}\s*/\s*\d{1,2}"
    r"|今日|今天|昨日|昨天|前日|前天|\d+\s*(?:天|小[時时]|分钟|分鐘)前"
    r")",
    re.IGNORECASE,
)

ARTICLE_PATH_RE = re.compile(
    r"("
    r"/article[s]?/"
    r"|/news/"
    r"|/post[s]?/"
    r"|/story/"
    r"|/stories/"
    r"|/detail[s]?/"
    r"|/content/"
    r"|/insight[s]?/"
    r"|/publication[s]?/"
    r"|/alert[s]?/"
    r"|/press[-_]release[s]?/"
    r"|/20\d{2}[-_/]\d{1,2}[-_/]\d{1,2}"
    r"|/20\d{2}/\d{1,2}/"
    r"|/\d{5,}(?:[/?#.-]|$)"
    r"|[?&](?:id|article_id|newsid|aid|sid)=\d+"
    r"|\.html?(?:[?#]|$)"
    r")",
    re.IGNORECASE,
)

GENERIC_COLUMN_SEGMENTS = {
    "news",
    "article",
    "articles",
    "posts",
    "post",
    "category",
    "categories",
    "list",
    "archive",
    "page",
    "tag",
    "tags",
}


def canonicalize_candidate_url(url: str) -> str:
    """Normalize a URL for dedupe without destroying meaningful query ids."""
    if not url:
        return ""
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return ""

    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS
    ]
    normalized_query = urlencode(query_pairs, doseq=True)
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")

    fragment = parsed.fragment if parsed.fragment.startswith("network-article-") else ""

    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            "",
            normalized_query,
            fragment,
        )
    )


def merge_link_candidates(primary_links: List[Dict], supplemental_links: List[Dict]) -> List[Dict]:
    """Merge candidates while preserving source-method provenance."""
    merged: List[Dict] = []
    seen: Dict[str, Dict] = {}

    for raw_link in list(primary_links or []) + list(supplemental_links or []):
        url = raw_link.get("url") or ""
        key = canonicalize_candidate_url(url)
        if not key:
            continue

        link = dict(raw_link)
        link["url"] = key
        method = link.get("source_method") or link.get("source_type") or "unknown"

        if key in seen:
            existing = seen[key]
            methods = _split_methods(existing.get("source_method"))
            for item in _split_methods(method):
                if item not in methods:
                    methods.append(item)
            existing["source_method"] = ",".join(methods)

            if not existing.get("title") and link.get("title"):
                existing["title"] = link.get("title")
                existing["text"] = link.get("text") or link.get("title")
            if not existing.get("publish_date") and link.get("publish_date"):
                existing["publish_date"] = link.get("publish_date")
            for field in ("content_hint", "content_hint_source", "discovery_source_url", "authors"):
                if not existing.get(field) and link.get(field):
                    existing[field] = link.get(field)
            continue

        if not link.get("text"):
            link["text"] = link.get("title", "")
        link["source_method"] = ",".join(_split_methods(method))
        seen[key] = link
        merged.append(link)

    return merged


def _read_int_env(name: str, default: int, min_value: int = None, max_value: int = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def discover_supplemental_article_links(
    list_url: str,
    timeout: int = 12,
    max_per_source: int = None,
    max_sitemaps: int = None,
    max_static_pages: int = None,
    crawl_options: Dict = None,
) -> Dict:
    """Discover article candidates from non-Playwright supplemental channels."""
    options = normalize_crawl_options(crawl_options)
    if not options.get("supplemental_enabled", True):
        return {
            "success": True,
            "links": [],
            "stats": _empty_supplemental_stats(disabled=True),
        }

    discovery = SupplementalLinkDiscovery(
        timeout=timeout,
        max_per_source=max_per_source or options.get("supplemental_max_per_source") or _read_int_env("CRAWL_SUPPLEMENTAL_MAX_PER_SOURCE", 500, 100, 5000),
        max_sitemaps=max_sitemaps or options.get("supplemental_max_sitemaps") or _read_int_env("CRAWL_SUPPLEMENTAL_MAX_SITEMAPS", 25, 1, 200),
        max_static_pages=max_static_pages or options.get("supplemental_max_static_pages") or _read_int_env("CRAWL_SUPPLEMENTAL_MAX_STATIC_PAGES", 8, 1, 100),
        crawl_options=options,
    )
    return discovery.discover(list_url)


def _empty_supplemental_stats(disabled: bool = False) -> Dict:
    return {
        "html_static_candidates": 0,
        "attribute_candidates": 0,
        "structured_candidates": 0,
        "embedded_script_candidates": 0,
        "feed_candidates": 0,
        "feed_inline_candidates": 0,
        "sitemap_candidates": 0,
        "static_pagination_candidates": 0,
        "selectolax_candidates": 0,
        "static_pages_checked": 0,
        "sitemaps_checked": 0,
        "feeds_checked": 0,
        "cache_enabled": False,
        "cache_hits": 0,
        "retry_enabled": False,
        "supplemental_disabled": disabled,
        "errors": [],
    }


class SupplementalLinkDiscovery:
    def __init__(
        self,
        timeout: int = 12,
        max_per_source: int = 500,
        max_sitemaps: int = 25,
        max_static_pages: int = 8,
        crawl_options: Dict = None,
    ):
        self.crawl_options = normalize_crawl_options(crawl_options)
        self.timeout = timeout
        self.max_per_source = max_per_source
        self.max_sitemaps = max_sitemaps
        self.max_static_pages = max_static_pages
        self.session = requests.Session()
        self.session.trust_env = bool(self.crawl_options.get("proxy_enabled"))
        self.session.headers.update(REQUEST_HEADERS)
        self.cache_session = None
        self.cache_enabled = bool(self.crawl_options.get("supplemental_cache_enabled")) and HAS_REQUESTS_CACHE
        self.cache_hits = 0
        self.retry_attempts = int(self.crawl_options.get("supplemental_retry_attempts") or 1)
        self.errors: List[str] = []
        if getattr(config, "DEFAULT_USER_AGENT", None):
            self.session.headers["User-Agent"] = config.DEFAULT_USER_AGENT
        proxies = config.get_proxies(enabled=self.crawl_options.get("proxy_enabled"))
        if proxies:
            self.session.proxies.update(proxies)
        if self.cache_enabled:
            try:
                results_dir = getattr(config, "CRAWL_RESULTS_DIR", "crawl_results")
                os.makedirs(results_dir, exist_ok=True)
                cache_name = os.path.join(results_dir, "supplemental_http_cache")
                self.cache_session = requests_cache.CachedSession(
                    cache_name=cache_name,
                    backend="sqlite",
                    expire_after=int(self.crawl_options.get("supplemental_cache_ttl_seconds") or 900),
                    allowable_methods=("GET",),
                    allowable_codes=(200,),
                )
                self.cache_session.trust_env = self.session.trust_env
                self.cache_session.headers.update(self.session.headers)
                if proxies:
                    self.cache_session.proxies.update(proxies)
            except Exception as exc:
                self.cache_enabled = False
                self.cache_session = None
                self.errors.append(f"cache_init: {exc}")
        self.sitemaps_checked = 0
        self.feeds_checked = 0
        self.static_pages_checked = 0

    def discover(self, list_url: str) -> Dict:
        stats = _empty_supplemental_stats()

        html = ""
        final_list_url = list_url
        try:
            html, final_list_url = self._fetch_text(list_url)
        except Exception as exc:
            self.errors.append(f"list_html: {exc}")

        html_links: List[Dict] = []
        attribute_links: List[Dict] = []
        selectolax_links: List[Dict] = []
        structured_links: List[Dict] = []
        embedded_links: List[Dict] = []
        paginated_html_links: List[Dict] = []
        feed_links: List[Dict] = []
        sitemap_links: List[Dict] = []

        if html:
            soup = BeautifulSoup(html, "html.parser")
            if HAS_SELECTOLAX and (
                self.crawl_options.get("supplemental_html", True)
                or self.crawl_options.get("supplemental_attributes", True)
            ):
                selectolax_links = self._discover_selectolax_links(html, final_list_url)
            if self.crawl_options.get("supplemental_html", True):
                html_links = self._discover_static_html_links(soup, final_list_url)
            if self.crawl_options.get("supplemental_attributes", True):
                attribute_links = self._discover_attribute_links(soup, final_list_url)
            if self.crawl_options.get("supplemental_structured", True):
                structured_links = self._discover_structured_links(html, final_list_url)
            if self.crawl_options.get("supplemental_scripts", True):
                embedded_links = self._discover_embedded_script_links(html, final_list_url)
            if self.crawl_options.get("supplemental_static_pagination", True):
                pagination_urls = self._discover_pagination_urls(soup, final_list_url)
                paginated_html_links = self._discover_static_pagination_links(pagination_urls, final_list_url)
            if self.crawl_options.get("supplemental_feeds", True):
                feed_urls = self._discover_feed_urls(soup, final_list_url)
        else:
            feed_urls = []

        if self.crawl_options.get("supplemental_feeds", True) and not feed_urls:
            feed_urls = self._common_feed_urls(final_list_url)

        if self.crawl_options.get("supplemental_feeds", True):
            feed_links = self._discover_feed_links(feed_urls, final_list_url)
        if self.crawl_options.get("supplemental_sitemaps", True):
            sitemap_links = self._discover_sitemap_links(final_list_url)

        stats["html_static_candidates"] = len(html_links)
        stats["attribute_candidates"] = len(attribute_links)
        stats["selectolax_candidates"] = len(selectolax_links)
        stats["structured_candidates"] = len(structured_links)
        stats["embedded_script_candidates"] = len(embedded_links)
        stats["feed_candidates"] = len(feed_links)
        stats["feed_inline_candidates"] = sum(1 for link in feed_links if link.get("content_hint"))
        stats["sitemap_candidates"] = len(sitemap_links)
        stats["static_pagination_candidates"] = len(paginated_html_links)
        stats["static_pages_checked"] = self.static_pages_checked
        stats["sitemaps_checked"] = self.sitemaps_checked
        stats["feeds_checked"] = self.feeds_checked
        stats["cache_enabled"] = self.cache_enabled
        stats["cache_hits"] = self.cache_hits
        stats["retry_enabled"] = HAS_TENACITY and self.retry_attempts > 1
        stats["errors"] = self.errors[:20]

        links = merge_link_candidates(
            [],
            (
                selectolax_links
                + html_links
                + attribute_links
                + structured_links
                + embedded_links
                + paginated_html_links
                + feed_links
                + sitemap_links
            ),
        )

        return {
            "success": True,
            "links": links,
            "stats": stats,
        }

    def _fetch_text(self, url: str, cacheable: bool = False) -> Tuple[str, str]:
        session = self.cache_session if cacheable and self.cache_session is not None else self.session

        def _send_request():
            response = safe_request_get(url, session=session, timeout=self.timeout, stream=True)
            if response.status_code in (429, 500, 502, 503, 504):
                response.raise_for_status()
            return response

        if HAS_TENACITY and self.retry_attempts > 1:
            retryer = Retrying(
                stop=stop_after_attempt(self.retry_attempts),
                wait=wait_exponential_jitter(initial=0.5, max=4.0),
                retry=retry_if_exception_type(requests.RequestException),
                reraise=True,
            )
            response = retryer(_send_request)
        else:
            response = _send_request()

        response.raise_for_status()
        if not response.encoding:
            response.encoding = response.apparent_encoding or "utf-8"
        if getattr(response, "from_cache", False):
            self.cache_hits += 1
        return read_response_text_limited(response), response.url

    def _discover_selectolax_links(self, html: str, list_url: str) -> List[Dict]:
        links: List[Dict] = []
        seen: Set[str] = set()
        if not HAS_SELECTOLAX or not html:
            return links

        def _node_text(node, limit=800) -> str:
            try:
                return re.sub(r"\s+", " ", node.text(separator=" ", strip=True) or "")[:limit]
            except Exception:
                return ""

        try:
            tree = HTMLParser(html)
        except Exception as exc:
            self.errors.append(f"selectolax: {exc}")
            return links

        for anchor in tree.css("a[href]"):
            if len(links) >= self.max_per_source:
                break
            attrs = dict(anchor.attributes or {})
            url = urljoin(list_url, str(attrs.get("href", "")).strip())
            key = canonicalize_candidate_url(url)
            if not key or key in seen:
                continue
            title = _clean_title(_node_text(anchor, limit=240)) or _guess_title_from_url(url)
            parent_text = _node_text(anchor.parent, limit=800) if anchor.parent else title
            accepted_by_url = self._accept_article_candidate(url, list_url, source="selectolax")
            accepted_by_text = self._accept_textual_article_candidate(url, title, parent_text, list_url)
            if not (accepted_by_url or accepted_by_text):
                continue
            seen.add(key)
            links.append(
                {
                    "url": url,
                    "title": title,
                    "text": title,
                    "publish_date": _parse_date(parent_text),
                    "source_method": "selectolax_html" if accepted_by_url else "selectolax_text",
                }
            )

        url_attrs = (
            "data-url",
            "data-href",
            "data-link",
            "data-target",
            "data-permalink",
            "data-share-url",
            "data-article-url",
        )
        for node in tree.css("*"):
            if len(links) >= self.max_per_source:
                break
            attrs = dict(node.attributes or {})
            raw_urls = [str(attrs.get(attr, "")).strip() for attr in url_attrs if attrs.get(attr)]
            onclick = attrs.get("onclick") or ""
            raw_urls.extend(_extract_urls_from_js(str(onclick)))
            if not raw_urls:
                continue
            context = _node_text(node, limit=800)
            title = _clean_title(context[:240]) or ""
            for raw_url in raw_urls:
                if len(links) >= self.max_per_source:
                    break
                url = urljoin(list_url, raw_url)
                key = canonicalize_candidate_url(url)
                if not key or key in seen:
                    continue
                link_title = title or _guess_title_from_url(url)
                accepted_by_url = self._accept_article_candidate(url, list_url, source="selectolax")
                accepted_by_text = self._accept_textual_article_candidate(url, link_title, context, list_url)
                if not (accepted_by_url or accepted_by_text):
                    continue
                seen.add(key)
                links.append(
                    {
                        "url": url,
                        "title": link_title,
                        "text": link_title,
                        "publish_date": _parse_date(context),
                        "source_method": "selectolax_attribute" if accepted_by_url else "selectolax_attribute_text",
                    }
                )

        return merge_link_candidates([], links)

    def _discover_static_html_links(self, soup: BeautifulSoup, list_url: str) -> List[Dict]:
        links: List[Dict] = []
        for anchor in soup.find_all("a", href=True):
            if len(links) >= self.max_per_source:
                break
            url = urljoin(list_url, anchor.get("href", "").strip())
            title = _extract_link_title(anchor, url)
            parent_text = _extract_parent_text(anchor)
            publish_date = _parse_date(parent_text)

            accepted_by_url = self._accept_article_candidate(url, list_url, source="html_static")
            accepted_by_text = self._accept_textual_article_candidate(url, title, parent_text, list_url)

            if accepted_by_url or accepted_by_text:
                source_method = "html_static" if accepted_by_url else "html_static_text"
                links.append(
                    {
                        "url": url,
                        "title": title or _guess_title_from_url(url),
                        "text": title or _guess_title_from_url(url),
                        "publish_date": publish_date,
                        "source_method": source_method,
                    }
                )
        return links

    def _discover_attribute_links(self, soup: BeautifulSoup, list_url: str) -> List[Dict]:
        links: List[Dict] = []
        seen: Set[str] = set()
        url_attrs = (
            "data-url",
            "data-href",
            "data-link",
            "data-target",
            "data-permalink",
            "data-share-url",
            "data-article-url",
        )

        for tag in soup.find_all(True):
            if len(links) >= self.max_per_source:
                break
            raw_urls = []
            for attr in url_attrs:
                value = tag.get(attr)
                if value:
                    raw_urls.append(str(value).strip())

            onclick = tag.get("onclick") or ""
            raw_urls.extend(_extract_urls_from_js(str(onclick)))

            for raw_url in raw_urls:
                url = urljoin(list_url, raw_url)
                key = canonicalize_candidate_url(url)
                if not key or key in seen:
                    continue
                title = _clean_title(tag.get_text(" ", strip=True)) or _guess_title_from_url(url)
                context = re.sub(r"\s+", " ", tag.get_text(" ", strip=True))[:600]
                accepted_by_url = self._accept_article_candidate(url, list_url, source="attribute")
                accepted_by_text = self._accept_textual_article_candidate(url, title, context, list_url)
                if not (accepted_by_url or accepted_by_text):
                    continue
                seen.add(key)
                links.append(
                    {
                        "url": url,
                        "title": title,
                        "text": title,
                        "publish_date": _parse_date(context),
                        "source_method": "attribute" if accepted_by_url else "attribute_text",
                    }
                )

        return merge_link_candidates([], links)

    def _discover_static_pagination_links(self, pagination_urls: List[str], list_url: str) -> List[Dict]:
        links: List[Dict] = []
        seen_pages: Set[str] = set()

        queue = deque(pagination_urls)

        while queue:
            if self.static_pages_checked >= self.max_static_pages:
                break
            page_url = queue.popleft()
            page_key = canonicalize_candidate_url(page_url)
            if not page_key or page_key in seen_pages:
                continue
            seen_pages.add(page_key)

            try:
                html, final_page_url = self._fetch_text(page_url, cacheable=True)
                self.static_pages_checked += 1
            except Exception:
                continue

            soup = BeautifulSoup(html, "html.parser")
            for next_url in self._discover_pagination_urls(soup, final_page_url):
                next_key = canonicalize_candidate_url(next_url)
                if next_key and next_key not in seen_pages and len(queue) < self.max_static_pages * 3:
                    queue.append(next_url)

            page_links = self._discover_static_html_links(soup, final_page_url)
            for link in page_links:
                if len(links) >= self.max_per_source:
                    break
                link = dict(link)
                methods = _split_methods(link.get("source_method"))
                if "static_pagination" not in methods:
                    methods.append("static_pagination")
                link["source_method"] = ",".join(methods)
                links.append(link)

        return merge_link_candidates([], links)

    def _discover_pagination_urls(self, soup: BeautifulSoup, list_url: str) -> List[str]:
        urls: List[str] = []
        seen: Set[str] = set()
        list_key = canonicalize_candidate_url(list_url)

        for tag in soup.find_all("link", href=True):
            rel = tag.get("rel", [])
            rel_text = " ".join(rel).lower() if isinstance(rel, list) else str(rel).lower()
            if "next" in rel_text:
                self._append_url(urls, seen, urljoin(list_url, tag.get("href", "")))

        for anchor in soup.find_all("a", href=True):
            href = anchor.get("href", "").strip()
            page_url = urljoin(list_url, href)
            page_key = canonicalize_candidate_url(page_url)
            if not page_key or page_key == list_key or page_key in seen:
                continue
            if not _same_site(page_key, list_url):
                continue
            text = anchor.get_text(" ", strip=True)
            if _is_probable_pagination_link(page_url, text):
                seen.add(page_key)
                urls.append(page_url)
            if len(urls) >= self.max_static_pages * 3:
                break

        return urls

    def _discover_structured_links(self, html: str, list_url: str) -> List[Dict]:
        links: List[Dict] = []
        links.extend(self._discover_jsonld_links(html, list_url))

        if not HAS_EXTRUCT:
            return merge_link_candidates([], links)

        try:
            data = extruct.extract(
                html,
                base_url=list_url,
                syntaxes=["json-ld", "microdata", "opengraph"],
                uniform=True,
            )
        except Exception as exc:
            self.errors.append(f"structured: {exc}")
            return merge_link_candidates([], links)

        for item in self._walk_structured_items(data):
            if len(links) >= self.max_per_source:
                break
            url = _first_structured_url(item)
            if not url:
                continue
            url = urljoin(list_url, url)
            if not self._accept_article_candidate(url, list_url, source="structured_data"):
                continue
            title = _clean_title(
                _first_string(item, ("headline", "name", "title", "og:title"))
                or _guess_title_from_url(url)
            )
            links.append(
                {
                    "url": url,
                    "title": title,
                    "text": title,
                    "publish_date": _parse_date(
                        _first_string(item, ("datePublished", "dateCreated", "dateModified"))
                    ),
                    "source_method": "structured_data",
                }
            )
        return merge_link_candidates([], links)

    def _discover_embedded_script_links(self, html: str, list_url: str) -> List[Dict]:
        links: List[Dict] = []
        seen: Set[str] = set()

        try:
            soup = BeautifulSoup(html, "html.parser")
            script_texts = [script.get_text(" ", strip=True) for script in soup.find_all("script")]
        except Exception:
            script_texts = []

        combined_text = "\n".join(text for text in script_texts if text)
        if not combined_text:
            return links

        raw_candidates = set()
        json_candidates = self._discover_json_script_links(soup, list_url) if "soup" in locals() else []
        links.extend(json_candidates)
        for item in json_candidates:
            key = canonicalize_candidate_url(item.get("url", ""))
            if key:
                seen.add(key)

        for match in re.finditer(r"https?:\\?/\\?/[^\"'\\<>\s]+", combined_text):
            raw_candidates.add(match.group(0).replace("\\/", "/"))
        for match in re.finditer(r'["\']((?:/|\.{1,2}/)[^"\']{6,220})["\']', combined_text):
            raw_candidates.add(match.group(1).replace("\\/", "/"))

        for raw_url in raw_candidates:
            if len(links) >= self.max_per_source:
                break
            url = urljoin(list_url, raw_url)
            key = canonicalize_candidate_url(url)
            if not key or key in seen:
                continue
            if not self._accept_article_candidate(url, list_url, source="embedded_script"):
                continue
            seen.add(key)
            title = _guess_title_from_url(url)
            links.append(
                {
                    "url": url,
                    "title": title,
                    "text": title,
                    "publish_date": None,
                    "source_method": "embedded_script",
                }
            )

        return merge_link_candidates([], links)

    def _discover_json_script_links(self, soup: BeautifulSoup, list_url: str) -> List[Dict]:
        links: List[Dict] = []
        scripts = soup.find_all(
            "script",
            attrs={
                "type": re.compile(r"(application/json|application/ld\+json|application/activity\+json)", re.I)
            },
        )
        scripts.extend(soup.find_all("script", id=re.compile(r"(__NEXT_DATA__|__NUXT__|apollo|initial|state)", re.I)))

        seen_script_nodes = set()
        for script in scripts:
            if id(script) in seen_script_nodes:
                continue
            seen_script_nodes.add(id(script))
            if len(links) >= self.max_per_source:
                break
            json_text = script.string or script.get_text()
            if not json_text:
                continue
            try:
                data = json.loads(json_text)
            except json.JSONDecodeError:
                continue

            for item in self._walk_structured_items(data):
                if len(links) >= self.max_per_source:
                    break
                url = _first_structured_url(item)
                if not url:
                    continue
                url = urljoin(list_url, url)
                title = _clean_title(
                    _first_string(item, ("headline", "title", "name", "seoTitle"))
                    or _guess_title_from_url(url)
                )
                context = " ".join(
                    part
                    for part in [
                        title,
                        _first_string(item, ("description", "summary", "excerpt")),
                        _first_string(item, ("datePublished", "publishedAt", "createdAt", "date")),
                    ]
                    if part
                )
                if not (
                    self._accept_article_candidate(url, list_url, source="embedded_script")
                    or self._accept_textual_article_candidate(url, title, context, list_url)
                ):
                    continue
                links.append(
                    {
                        "url": url,
                        "title": title,
                        "text": title,
                        "publish_date": _parse_date(
                            _first_string(item, ("datePublished", "publishedAt", "createdAt", "date"))
                        ),
                        "source_method": "embedded_json",
                    }
                )

        return merge_link_candidates([], links)

    def _discover_jsonld_links(self, html: str, list_url: str) -> List[Dict]:
        links: List[Dict] = []
        try:
            soup = BeautifulSoup(html, "html.parser")
            scripts = soup.select('script[type="application/ld+json"]')
        except Exception:
            return links

        for script in scripts:
            if len(links) >= self.max_per_source:
                break
            json_text = script.string or script.get_text()
            if not json_text:
                continue
            try:
                data = json.loads(json_text)
            except json.JSONDecodeError:
                continue
            for item in self._walk_structured_items(data):
                if len(links) >= self.max_per_source:
                    break
                url = _first_structured_url(item)
                if not url:
                    continue
                url = urljoin(list_url, url)
                if not self._accept_article_candidate(url, list_url, source="structured_data"):
                    continue
                title = _clean_title(
                    _first_string(item, ("headline", "name", "title")) or _guess_title_from_url(url)
                )
                links.append(
                    {
                        "url": url,
                        "title": title,
                        "text": title,
                        "publish_date": _parse_date(
                            _first_string(item, ("datePublished", "dateCreated", "dateModified"))
                        ),
                        "source_method": "structured_data",
                    }
                )
        return links

    def _walk_structured_items(self, obj) -> Iterable[Dict]:
        if isinstance(obj, dict):
            yield obj
            for key, value in obj.items():
                if key in ("@context",):
                    continue
                yield from self._walk_structured_items(value)
        elif isinstance(obj, list):
            for item in obj:
                yield from self._walk_structured_items(item)

    def _discover_feed_urls(self, soup: BeautifulSoup, list_url: str) -> List[str]:
        urls: List[str] = []
        seen: Set[str] = set()

        for tag in soup.find_all("link", href=True):
            rel = " ".join(tag.get("rel", [])).lower() if isinstance(tag.get("rel"), list) else str(tag.get("rel", "")).lower()
            type_value = str(tag.get("type", "")).lower()
            href = tag.get("href", "").strip()
            if "alternate" in rel and ("rss" in type_value or "atom" in type_value or "json" in type_value):
                self._append_url(urls, seen, urljoin(list_url, href))

        for anchor in soup.find_all("a", href=True):
            href = anchor.get("href", "").strip()
            text = anchor.get_text(" ", strip=True).lower()
            if any(token in href.lower() for token in ("/rss", "feed", "atom.xml")) or any(
                token in text for token in ("rss", "feed", "订阅", "訂閱")
            ):
                self._append_url(urls, seen, urljoin(list_url, href))

        for url in self._common_feed_urls(list_url):
            self._append_url(urls, seen, url)
        return urls[:20]

    def _common_feed_urls(self, list_url: str) -> List[str]:
        parsed = urlparse(list_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path.rstrip("/")
        urls = [
            urljoin(origin, "/feed"),
            urljoin(origin, "/feed.xml"),
            urljoin(origin, "/feed.json"),
            urljoin(origin, "/rss"),
            urljoin(origin, "/rss.xml"),
            urljoin(origin, "/atom.xml"),
            urljoin(origin, "/index.xml"),
        ]
        if path:
            urls.extend(
                [
                    urljoin(origin, f"{path}/feed"),
                    urljoin(origin, f"{path}/feed.json"),
                    urljoin(origin, f"{path}/rss"),
                    urljoin(origin, f"{path}/rss.xml"),
                    urljoin(origin, f"{path}.rss"),
                    urljoin(origin, f"{path}.xml"),
                ]
            )
        return urls

    def _discover_feed_links(self, feed_urls: List[str], list_url: str) -> List[Dict]:
        links: List[Dict] = []
        seen_feeds: Set[str] = set()

        for feed_url in feed_urls:
            if len(links) >= self.max_per_source:
                break
            feed_key = canonicalize_candidate_url(feed_url)
            if not feed_key or feed_key in seen_feeds:
                continue
            seen_feeds.add(feed_key)

            try:
                xml, final_feed_url = self._fetch_text(feed_url, cacheable=True)
                self.feeds_checked += 1
            except Exception:
                continue

            for item in self._parse_feed_items(xml, final_feed_url, list_url):
                if len(links) >= self.max_per_source:
                    break
                links.append(item)

        return merge_link_candidates([], links)

    def _parse_feed_items(self, xml: str, feed_url: str, list_url: str) -> List[Dict]:
        stripped = (xml or "").lstrip()
        if stripped.startswith("{"):
            return self._parse_json_feed_items(stripped, feed_url, list_url)

        soup = BeautifulSoup(xml, "xml")
        items = soup.find_all(["item", "entry"])
        links: List[Dict] = []

        for item in items:
            url = ""
            link_tag = item.find("link")
            if link_tag:
                url = link_tag.get("href") or link_tag.get_text(strip=True)
            if not url:
                guid_tag = item.find("guid")
                url = guid_tag.get_text(strip=True) if guid_tag else ""
            if not url:
                continue
            url = urljoin(feed_url, url)
            if not self._accept_article_candidate(url, list_url, source="feed"):
                continue

            title = _clean_title(_tag_text(item, ("title",)) or _guess_title_from_url(url))
            publish_date = _parse_date(
                _tag_text(
                    item,
                    (
                        "pubDate",
                        "published",
                        "updated",
                        "dc:date",
                        "date",
                    ),
                )
            )
            link = {
                "url": url,
                "title": title,
                "text": title,
                "publish_date": publish_date,
                "source_method": "feed",
                "discovery_source_url": feed_url,
            }
            content_hint = _first_feed_content(item)
            if content_hint:
                link["content_hint"] = content_hint
                link["content_hint_source"] = "feed"
            links.append(link)
        return links

    def _parse_json_feed_items(self, feed_text: str, feed_url: str, list_url: str) -> List[Dict]:
        links: List[Dict] = []
        try:
            data = json.loads(feed_text)
        except json.JSONDecodeError:
            return links

        items = data.get("items", []) if isinstance(data, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or item.get("external_url") or item.get("id") or ""
            if not url:
                continue
            url = urljoin(feed_url, url)
            if not self._accept_article_candidate(url, list_url, source="feed"):
                continue
            title = _clean_title(item.get("title") or _guess_title_from_url(url))
            publish_date = _parse_date(item.get("date_published") or item.get("date_modified"))
            link = {
                "url": url,
                "title": title,
                "text": title,
                "publish_date": publish_date,
                "source_method": "feed_json",
                "discovery_source_url": feed_url,
            }
            content_hint = (
                item.get("content_html")
                or item.get("content_text")
                or item.get("summary")
                or item.get("description")
            )
            if content_hint and len(str(content_hint).strip()) >= 80:
                link["content_hint"] = str(content_hint)
                link["content_hint_source"] = "feed_json"
            links.append(link)
        return links

    def _discover_sitemap_links(self, list_url: str) -> List[Dict]:
        sitemap_urls = self._discover_sitemap_urls(list_url)
        queue = deque(sitemap_urls)
        seen_sitemaps: Set[str] = set()
        links: List[Dict] = []
        sitemap_collection_limit = self.max_per_source * 3

        while queue and self.sitemaps_checked < self.max_sitemaps and len(links) < sitemap_collection_limit:
            sitemap_url = queue.popleft()
            sitemap_key = canonicalize_candidate_url(sitemap_url)
            if not sitemap_key or sitemap_key in seen_sitemaps:
                continue
            seen_sitemaps.add(sitemap_key)

            try:
                xml, final_sitemap_url = self._fetch_text(sitemap_url, cacheable=True)
                self.sitemaps_checked += 1
            except Exception:
                continue

            child_sitemaps, url_links = self._parse_sitemap(xml, final_sitemap_url, list_url)
            for child in child_sitemaps:
                if len(seen_sitemaps) + len(queue) >= self.max_sitemaps:
                    break
                queue.append(child)
            links.extend(url_links)

        ranked_links = _rank_links_for_list(merge_link_candidates([], links), list_url)
        return ranked_links[: self.max_per_source]

    def _discover_sitemap_urls(self, list_url: str) -> List[str]:
        parsed = urlparse(list_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        urls: List[str] = []
        seen: Set[str] = set()

        robots_url = urljoin(origin, "/robots.txt")
        try:
            robots_text, _ = self._fetch_text(robots_url, cacheable=True)
            for line in robots_text.splitlines():
                if line.lower().startswith("sitemap:"):
                    self._append_url(urls, seen, line.split(":", 1)[1].strip())
        except Exception:
            pass

        common = (
            "/sitemap.xml",
            "/sitemap_index.xml",
            "/sitemap-index.xml",
            "/sitemap-news.xml",
            "/news-sitemap.xml",
            "/post-sitemap.xml",
            "/posts-sitemap.xml",
            "/article-sitemap.xml",
            "/articles-sitemap.xml",
            "/page-sitemap.xml",
            "/publication-sitemap.xml",
            "/publications-sitemap.xml",
            "/content-sitemap.xml",
        )
        for path in common:
            self._append_url(urls, seen, urljoin(origin, path))
        return urls

    def _parse_sitemap(self, xml: str, sitemap_url: str, list_url: str) -> Tuple[List[str], List[Dict]]:
        soup = BeautifulSoup(xml, "xml")
        child_sitemaps: List[str] = []
        links: List[Dict] = []

        for node in soup.find_all("sitemap"):
            loc = _tag_text(node, ("loc",))
            if loc:
                child_sitemaps.append(urljoin(sitemap_url, str(loc).strip()).strip())

        for node in soup.find_all("url"):
            loc = _tag_text(node, ("loc",))
            if not loc:
                continue
            url = urljoin(sitemap_url, str(loc).strip()).strip()
            if not self._accept_article_candidate(url, list_url, source="sitemap"):
                continue
            title = _clean_title(_tag_text(node, ("news:title", "title")) or _guess_title_from_url(url))
            publish_date = _parse_date(
                _tag_text(node, ("news:publication_date", "publication_date", "lastmod"))
            )
            links.append(
                {
                    "url": url,
                    "title": title,
                    "text": title,
                    "publish_date": publish_date,
                    "source_method": "sitemap",
                }
            )

        # Some sites omit <url> wrappers. Treat standalone loc entries as a fallback.
        if not child_sitemaps and not links:
            for loc_tag in soup.find_all("loc"):
                url = urljoin(sitemap_url, loc_tag.get_text(strip=True).strip()).strip()
                if url.lower().endswith(".xml") or "sitemap" in url.lower():
                    child_sitemaps.append(url)
                    continue
                if self._accept_article_candidate(url, list_url, source="sitemap"):
                    links.append(
                        {
                            "url": url,
                            "title": _guess_title_from_url(url),
                            "text": _guess_title_from_url(url),
                            "publish_date": None,
                            "source_method": "sitemap",
                        }
                    )

        return child_sitemaps, links

    def _accept_article_candidate(self, url: str, list_url: str, source: str) -> bool:
        canonical = canonicalize_candidate_url(url)
        list_canonical = canonicalize_candidate_url(list_url)
        if not canonical or canonical == list_canonical:
            return False
        if not _same_site(canonical, list_url):
            return False

        parsed = urlparse(canonical)
        url_lower = canonical.lower()
        path_lower = parsed.path.lower()
        if any(path_lower.endswith(ext) for ext in BAD_EXTENSIONS):
            return False
        if any(part in url_lower for part in BAD_PATH_PARTS):
            return False
        if "javascript:" in url_lower or "mailto:" in url_lower or "tel:" in url_lower:
            return False

        likely_article = bool(ARTICLE_PATH_RE.search(canonical))
        if not likely_article:
            return False

        if source == "sitemap" and not _is_related_to_column(canonical, list_url):
            return False
        return True

    def _accept_textual_article_candidate(self, url: str, title: str, context: str, list_url: str) -> bool:
        canonical = canonicalize_candidate_url(url)
        list_canonical = canonicalize_candidate_url(list_url)
        if not canonical or canonical == list_canonical:
            return False
        if not _same_site(canonical, list_url):
            return False

        parsed = urlparse(canonical)
        url_lower = canonical.lower()
        path_lower = parsed.path.lower()
        if any(path_lower.endswith(ext) for ext in BAD_EXTENSIONS):
            return False
        hard_bad_parts = tuple(part for part in BAD_PATH_PARTS if part not in SOFT_BAD_PATH_PARTS)
        if any(part in url_lower for part in hard_bad_parts):
            return False
        soft_bad_hit = any(part in url_lower for part in SOFT_BAD_PATH_PARTS)
        if "javascript:" in url_lower or "mailto:" in url_lower or "tel:" in url_lower:
            return False

        if _is_probable_pagination_link(canonical, title):
            return False
        if _looks_like_article_title(title):
            return True

        if soft_bad_hit:
            return False

        context = context or ""
        if DATE_HINT_RE.search(context) and _looks_like_article_title(_best_title_from_context(context)):
            return True

        return False

    def _append_url(self, urls: List[str], seen: Set[str], url: str):
        key = canonicalize_candidate_url(url)
        if key and key not in seen:
            seen.add(key)
            urls.append(url)


def _split_methods(value) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        parts = value
    else:
        parts = str(value).split(",")
    return [part.strip() for part in parts if part and part.strip()]


def _same_site(url: str, base_url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    base_host = (urlparse(base_url).hostname or "").lower()
    if not host or not base_host:
        return False
    root = _strip_common_subdomain(host)
    base_root = _strip_common_subdomain(base_host)
    return (
        root == base_root
        or host.endswith(f".{base_root}")
        or base_host.endswith(f".{root}")
    )


def _strip_common_subdomain(host: str) -> str:
    for prefix in ("www.", "m.", "mobile.", "amp."):
        if host.startswith(prefix):
            return host[len(prefix):]
    return host


def _extract_link_title(anchor, url: str) -> str:
    title = _clean_title(anchor.get_text(" ", strip=True))
    if _looks_like_article_title(title):
        return title

    parent = anchor.find_parent(["article", "li", "div", "section"])
    if parent:
        for heading in parent.find_all(["h1", "h2", "h3", "h4"], limit=3):
            heading_text = _clean_title(heading.get_text(" ", strip=True))
            if _looks_like_article_title(heading_text):
                return heading_text

    return title or _guess_title_from_url(url)


def _extract_parent_text(anchor) -> str:
    parent = anchor.find_parent(["article", "li", "div", "section"])
    if not parent:
        return anchor.get_text(" ", strip=True)
    text = parent.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)[:600]


def _looks_like_article_title(text: str) -> bool:
    text = _clean_title(text)
    if not text:
        return False

    normalized = text.strip().lower()
    if normalized in SHORT_ACTION_TEXTS or TITLE_EXCLUDE_RE.match(text):
        return False
    if len(text) > 180:
        return False

    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin_words = len(re.findall(r"\b[A-Za-z][A-Za-z0-9'-]{2,}\b", text))
    digit_ratio = len(re.findall(r"\d", text)) / max(len(text), 1)

    if digit_ratio > 0.65:
        return False
    if cjk_count >= 8 and len(text) >= 10:
        return True
    if latin_words >= 4 and len(text) >= 22:
        return True
    if DATE_HINT_RE.search(text) and (cjk_count >= 4 or latin_words >= 3):
        return True
    if re.search(r"[。！？!?：:]", text) and (cjk_count >= 6 or latin_words >= 3):
        return True

    return False


def _best_title_from_context(context: str) -> str:
    if not context:
        return ""
    parts = re.split(r"[\n\r。！？!?|丨]", context)
    for part in parts:
        candidate = _clean_title(part)
        if _looks_like_article_title(candidate):
            return candidate
    return _clean_title(context[:120])


def _is_probable_pagination_link(url: str, text: str = "") -> bool:
    text_value = (text or "").strip().lower()
    url_lower = (url or "").lower()
    if text_value in {"下一页", "下一頁", "下页", "下頁", "更多", "加载更多", "載入更多", "next", "more", "older", ">" }:
        return True
    if re.fullmatch(r"\d{1,3}", text_value or ""):
        return True
    return bool(
        re.search(r"([?&](page|paged|p|start|offset)=\d+)", url_lower)
        or re.search(r"/page/\d+/?(?:[?#]|$)", url_lower)
        or re.search(r"[_-]p\d+\.html?(?:[?#]|$)", url_lower)
        or re.search(r"/list[_-]?\d+\.html?(?:[?#]|$)", url_lower)
    )


def _is_related_to_column(url: str, list_url: str) -> bool:
    list_segments = _meaningful_segments(urlparse(list_url).path)
    if not list_segments:
        return True
    url_segments = set(_meaningful_segments(urlparse(url).path))
    if url_segments.intersection(list_segments):
        return True

    # Common article paths often move away from the column path, especially on
    # law-firm and corporate sites. Keep sitemap recall high; detail extraction
    # and audit statuses will remove non-articles later.
    url_lower = url.lower()
    if re.search(r"/\d{4,}(?:[/?#._-]|$)|[?&](?:id|article_id|newsid|aid|sid)=\d+", url_lower):
        return True
    if re.search(r"/(?:content|details?|publication|publications|insights?|alerts?|newsroom|press[-_]release)[/_-]?", url_lower):
        return True
    if re.search(r"\.html?(?:[?#]|$)", url_lower):
        last_segment = urlparse(url_lower).path.rstrip("/").split("/")[-1]
        if last_segment not in ("index.html", "index.htm"):
            return True

    return False


def _meaningful_segments(path: str) -> List[str]:
    segments = []
    for segment in path.split("/"):
        segment = segment.strip().lower()
        if not segment or segment in GENERIC_COLUMN_SEGMENTS:
            continue
        if segment.isdigit():
            continue
        segments.append(segment)
    return segments[:3]


def _clean_title(title: str) -> str:
    if not title:
        return ""
    title = re.sub(r"\s+", " ", title).strip()
    return title[:180]


def _guess_title_from_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    segment = path.split("/")[-1] if path else parsed.netloc
    segment = re.sub(r"\.html?$", "", segment, flags=re.IGNORECASE)
    segment = unquote(segment)
    segment = re.sub(r"[-_]+", " ", segment).strip()
    return _clean_title(segment or url)


def _tag_text(node, names: Iterable[str]) -> str:
    wanted = {name.lower() for name in names}
    wanted_local = {name.split(":")[-1].lower() for name in names}

    for child in node.find_all():
        child_name = (child.name or "").lower()
        child_local = child_name.split(":")[-1]
        if child_name in wanted or child_local in wanted_local:
            text = child.get_text(" ", strip=True)
            if text:
                return text
    return ""


def _first_feed_content(item) -> str:
    """Return substantial inline article text/html carried by a feed item."""
    for names in (
        ("content:encoded", "encoded"),
        ("content",),
        ("description",),
        ("summary",),
    ):
        text = _tag_text(item, names)
        if text and len(text.strip()) >= 80:
            return text.strip()
    return ""


def _first_string(item: Dict, keys: Iterable[str]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, str) and entry.strip():
                    return entry.strip()
    return ""


def _first_structured_url(item: Dict) -> str:
    for key in (
        "url",
        "@id",
        "href",
        "link",
        "permalink",
        "canonical",
        "path",
        "slug",
        "mainEntityOfPage",
        "item",
        "sameAs",
    ):
        value = item.get(key)
        url = _extract_url_value(value)
        if url:
            return url
    return ""


def _extract_url_value(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("@id", "url", "id"):
            url = _extract_url_value(value.get(key))
            if url:
                return url
    if isinstance(value, list):
        for entry in value:
            url = _extract_url_value(entry)
            if url:
                return url
    return ""


def _extract_urls_from_js(js_text: str) -> List[str]:
    if not js_text:
        return []
    urls = []
    patterns = [
        r"(?:location\.href|window\.location|document\.location)\s*=\s*['\"]([^'\"]+)['\"]",
        r"(?:open|navigate|push|replace)\(\s*['\"]([^'\"]+)['\"]",
        r"['\"]((?:https?:\\?/\\?/|/)[^'\"\s<>]{6,220})['\"]",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, js_text, flags=re.IGNORECASE):
            urls.append(match.group(1).replace("\\/", "/"))
    return urls


def _rank_links_for_list(links: List[Dict], list_url: str) -> List[Dict]:
    def rank_key(link: Dict):
        url = link.get("url", "")
        publish_date = _parse_date(link.get("publish_date"))
        date_score = 0
        if publish_date:
            try:
                date_score = int(publish_date.replace("-", ""))
            except ValueError:
                date_score = 0
        relation_score = 1 if _is_related_to_column(url, list_url) else 0
        source_score = 1 if "sitemap" not in str(link.get("source_method", "")) else 0
        return (date_score, relation_score, source_score)

    return sorted(links, key=rank_key, reverse=True)


def _parse_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = str(value).strip()

    match = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", value)
    if match:
        year, month, day = match.groups()
        try:
            return datetime(int(year), int(month), int(day)).strftime("%Y-%m-%d")
        except ValueError:
            return None

    match = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日?", value)
    if match:
        year, month, day = match.groups()
        try:
            return datetime(int(year), int(month), int(day)).strftime("%Y-%m-%d")
        except ValueError:
            return None

    try:
        parsed = parsedate_to_datetime(value)
        if parsed:
            return parsed.strftime("%Y-%m-%d")
    except Exception:
        return None

    return None

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""RSS, list-page, website, and SerpAPI candidate discovery service."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import requests
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

import config
from industry_packs import industry_pack_loader
from financial_source_license import rss_authorization_decision
from financial_rollout import rollout_capability_enabled

try:
    # 反爬放弃策略：被拦截达阈值的信源不再派发扫描任务（识别器只做止损，不做绕过）
    from antibot_detector import should_skip_source as _should_skip_blocked_source
except ImportError:  # pragma: no cover
    def _should_skip_blocked_source(metadata, settings=None) -> bool:
        return False


try:
    # Agent-Reach 关键词聚焦检索（与 SerpAPI / Tavily 并列的第三个"发现 URL"来源）
    from agent_reach_search import preview_gate as _agent_reach_preview_gate
    from agent_reach_search import search_pack as _agent_reach_search_pack
except ImportError:  # pragma: no cover - 未部署 Agent-Reach 时整条分支静默失效
    def _agent_reach_preview_gate(item: Dict, pack: Dict, query_text: str = "") -> bool:
        return False

    def _agent_reach_search_pack(queries, **kwargs) -> Dict:
        return {"items": [], "platforms": [], "errors": [], "elapsed_seconds": 0.0,
                "timeout": False, "queries": [], "unavailable": []}

from intel_candidates import (
    IntelCandidateRepository,
    intel_candidate_repository,
    quick_score_candidate,
)
from intel_contracts import utc_now
from intel_http import SafeHTTPClient, sanitize_external_error, validate_external_url
from rss_feed_contract import parse_rss_feed
from intel_sources import IntelSourceRegistry, intel_source_registry
from project_keyword_gate import matched_project_keywords
from serpapi_client import SerpAPIClient
from tavily_client import TavilyClient
from utils import coerce_int

# 列表/聚合页 URL 模式（tag/栏目/作者/搜索/分页）：链接抽取阶段即排除，不进候选。
# 例外：以文章扩展名结尾的 URL 是文章页（如 leiphone.com/category/academic/xxx.html），放行。
_LISTING_PATH_RE = re.compile(
    r"/(tag|tags|category|categories|author|authors|search)(/|$)",
    re.IGNORECASE,
)
_LISTING_PAGE_RE = re.compile(r"/page/\d+(/|$)", re.IGNORECASE)
_LISTING_QUERY_RE = re.compile(r"(^|[&?])(p|page)=\d+", re.IGNORECASE)
_ARTICLE_EXT_RE = re.compile(r"\.(html?|php|aspx?|jspx?|shtml?)([?#]|$)", re.IGNORECASE)


USER_AGENT = "MarketIntelRadar/1.0 (+source-discovery)"


def serpapi_preview_gate(item: Dict, pack: Dict, query_text: str) -> bool:
    """Require both industry evidence and the configured query-topic evidence."""
    title = str(item.get("title") or "")
    summary = str(item.get("summary") or item.get("snippet") or "")
    preview_score = quick_score_candidate(title, summary, pack)
    required_query_terms = (
        pack.get("serpapi_query_gates") or {}
    ).get(query_text, [])
    query_hits = matched_project_keywords(
        required_query_terms,
        title,
        summary,
    )
    brand_hits = matched_project_keywords(pack.get("brands") or [], title, summary)
    return bool(
        (preview_score.get("anchor_hits") or brand_hits)
        and (not required_query_terms or query_hits)
        and _is_preferred_serp_language(item)
    )


def _listing_publish_time(text: str, url: str = "") -> str:
    """从链接附近的文本 / URL 里抽发布时间（采集那一刻就写，不等事后回填）。

    过去列表页扫描把 published_at 一律写成 None，而绝大多数信源是列表页——
    这就是候选表里发布时间只有 2% 有值的直接原因。这里统一走 publish_time.py：
    列表页日期（2026-10-07 / 2026年10月7日 / 3天前）> URL 里的日期 > 拿不到就留空。
    """
    try:
        from publish_time import extract

        return str(extract(listing_text=text, url=url).get("published_at") or "")
    except Exception:
        return ""


def _filter_items_to_window(items: Iterable[Dict], start: str = '', end: str = '') -> List[Dict]:
    """Keep dated candidates inside an initialization window.

    Undated candidates are retained for the full crawler, which may obtain a
    date from the article detail page. This prevents a list page without date
    metadata from silently losing all of its historical candidates.
    """
    if not str(start or '').strip() and not str(end or '').strip():
        return list(items or [])

    def parse(value):
        raw = str(value or '').strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        except ValueError:
            return None
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)

    start_value = parse(start)
    end_value = parse(end)
    filtered = []
    for item in items or []:
        published = parse(
            item.get('published_at') or item.get('publish_date') or item.get('published')
        )
        if published is None or ((not start_value or published >= start_value) and (not end_value or published <= end_value)):
            filtered.append(item)
    return filtered


def source_scan_window_key(source: Dict, *, epoch_seconds: Optional[float] = None) -> str:
    """Canonical source identity plus its configured polling-time bucket."""
    canonical = str(
        source.get("canonical_source_url") or source.get("source_url") or ""
    ).strip()
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    interval_minutes = coerce_int(source.get("polling_interval_minutes"), 1440, 5, 10080)
    bucket_seconds = int(interval_minutes) * 60
    timestamp = time.time() if epoch_seconds is None else float(epoch_seconds)
    return f"source:{digest}:{int(timestamp // bucket_seconds)}"


def _attention_watch_queries(industry_pack_id: str, limit: int = 6) -> List[str]:
    """周报「下周关注」生成的追踪词 → 本轮的额外搜索词（专门盯这几条线索）。

    只负责"多搜几个词"，不参与任何门禁：搜到的 URL 依旧走候选门禁 → 抓正文 →
    分类 → 主题归属，与既有链路完全一致。
    """
    try:
        from pack_attention import active_watch_keywords
        keywords = active_watch_keywords(industry_pack_id, limit=limit)
    except Exception as exc:
        print(f"⚠️ 读取本周追踪词失败，跳过（{exc}）")
        return []
    if not keywords:
        return []
    print("🎯 本周追踪词进入搜索采集: %s" % "、".join(keywords))
    return [f"{keyword} 最新" for keyword in keywords]


def _is_preferred_serp_language(item: Dict) -> bool:
    """A defensive result-level check in addition to Google's ``lr`` filter."""
    preference = str(config.SERPAPI_RESULT_LANGUAGE or "zh").casefold()
    if preference == "all":
        return True
    text = f"{item.get('title') or ''} {item.get('summary') or item.get('snippet') or ''}"
    if preference == "zh":
        return sum("\u4e00" <= char <= "\u9fff" for char in text) >= 2
    if preference == "en":
        latin = sum(char.isascii() and char.isalpha() for char in text)
        return latin >= 3
    return False


class RSSScanner:
    scanner_type = "rss"

    def __init__(self, http_client: SafeHTTPClient = None):
        self.http_client = http_client or SafeHTTPClient()

    def scan(self, source: Dict, *, limit: int) -> List[Dict]:
        response = self.http_client.get(
            source["source_url"],
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml"},
        )
        return parse_rss_feed(response, limit=limit)


def _direct_link_extract(content: bytes | str, base_url: str, pattern: str, limit: int) -> List[Dict]:
    """正则直提：从原始 HTML/XML 文本提取 href 匹配 pattern 的链接。

    用于政府站把文章列表放在 <recordset><![CDATA[...]]> 等 BeautifulSoup
    解析不到的模板块里的情况。href 附近的 title="..." 作为标题。
    """
    if isinstance(content, bytes):
        # 政府站常为 GBK/GB18030 编码：先 utf-8，失败回退 gb18030，避免标题乱码
        text = None
        for enc in ("utf-8", "gb18030"):
            try:
                text = content.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            text = content.decode("utf-8", errors="replace")
    else:
        text = str(content or "")
    source_host = (urlsplit(base_url).hostname or "").casefold()
    try:
        compiled = re.compile(pattern)
    except re.error:
        return []
    results: List[Dict] = []
    seen = set()
    # href 可能带引号也可能不带（CDATA 内常见 href="..."）
    for m in re.finditer(r'href\s*=\s*["\']?([^"\'\s>]+)["\']?', text):
        raw = m.group(1)
        if not compiled.search(raw):
            continue
        target = urljoin(base_url, raw)
        parsed = urlsplit(target)
        host = (parsed.hostname or "").casefold()
        if parsed.scheme not in {"http", "https"} or not host:
            continue
        if host != source_host and not host.endswith(f".{source_host}"):
            continue
        identity = target.split("#", 1)[0]
        if identity in seen:
            continue
        seen.add(identity)
        # 在 href 附近找 title（前后 300 字符内），取第一个非空标题
        window = text[max(0, m.start() - 60): m.end() + 400]
        tm = re.search(r'title\s*=\s*"([^"]{4,200})"', window)
        title = tm.group(1).strip() if tm else ""
        if not title:
            continue
        results.append({
            "url": target,
            "title": title[:1000],
            "summary": "",
            # 采集时就写发布时间：href 附近那段文本（链接列表里日期常与标题同行）
            "published_at": _listing_publish_time(window, target),
        })
        if len(results) >= limit:
            break
    return results


class ListPageScanner:
    scanner_type = "list_page"

    def __init__(self, http_client: SafeHTTPClient = None):
        self.http_client = http_client or SafeHTTPClient()

    def scan(self, source: Dict, *, limit: int) -> List[Dict]:
        response = self.http_client.get(
            source["source_url"],
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        )
        # 阶段4：该站点已学习过模型 → 优先用模型提取（标题+链接）；未命中/失效自动回退启发式
        try:
            from site_scraper_models import extract_with_model
            from sqlite_database import sqlite_db
            _html = response.content.decode(response.encoding or "utf-8", errors="replace")
            model_items = extract_with_model(sqlite_db, response.url, _html, limit=limit)
            if model_items:
                return model_items
        except Exception:
            pass
        include_pattern = str((source.get("metadata") or {}).get("link_include_pattern") or "").strip() or None
        items = self.scan_html(
            response.url, response.content, limit=limit, include_pattern=include_pattern
        )
        if bool((source.get("metadata") or {}).get("scan_page_as_article")):
            items = self.include_page_as_article(response.url, response.content, items, limit=limit)
        return items

    @staticmethod
    def include_page_as_article(
        base_url: str, content: bytes | str, items: List[Dict], *, limit: int
    ) -> List[Dict]:
        """Include a configured direct-report URL as a candidate itself.

        Most sources are listing pages, where only links should be discovered.
        A report/news URL, however, is already the article and otherwise gets
        lost among its related-links.  This flag is explicit in source metadata
        so a normal home/list page can never be promoted accidentally.
        """
        soup = BeautifulSoup(content, "html.parser")
        title_tag = soup.find("meta", property="og:title") or soup.find("title")
        title = ""
        if title_tag:
            title = str(title_tag.get("content") or title_tag.get_text(" ", strip=True) or "").strip()
        description_tag = (
            soup.find("meta", property="og:description")
            or soup.find("meta", attrs={"name": "description"})
        )
        summary = str(description_tag.get("content") or "").strip() if description_tag else ""
        if not title:
            return items
        page_item = {
            "url": base_url,
            "title": title[:1000],
            "summary": summary[:5000],
            # 采集时就写发布时间（列表页本身也常是文章页：能抽到就带上）
            "published_at": _listing_publish_time(summary, base_url),
        }
        canonical = base_url.split("#", 1)[0]
        remaining = [item for item in items if str(item.get("url") or "").split("#", 1)[0] != canonical]
        return [page_item, *remaining][:limit]

    def scan_html(
        self,
        base_url: str,
        content: bytes | str,
        *,
        limit: int,
        include_pattern: str | None = None,
    ) -> List[Dict]:
        """从列表页抽取候选链接。

        ``include_pattern``（可选）：仅保留目标 URL 匹配该正则的链接。用于像
        公共资源交易平台这类首页导航链接很多、真实公告链接靠后（容易把 limit 撑满）
        的网站——只放行公告链接，避免被导航/辅助页占满候选名额。

        指定 include_pattern 时先走「正则直提」：直接从原始 HTML 文本提取
        href 匹配的链接（含 <recordset><![CDATA[...]]> 这类 BeautifulSoup
        解析不到的模板数据块，如医保局政府站的文章列表）。
        """
        if include_pattern:
            direct = _direct_link_extract(content, base_url, include_pattern, limit)
            if direct:
                return direct
        soup = BeautifulSoup(content, "html.parser")
        source_host = (urlsplit(base_url).hostname or "").casefold()
        results = []
        seen = set()
        for anchor in soup.select("a[href]"):
            target = urljoin(base_url, anchor.get("href") or "")
            parsed = urlsplit(target)
            host = (parsed.hostname or "").casefold()
            if parsed.scheme not in {"http", "https"} or not host:
                continue
            if host != source_host and not host.endswith(f".{source_host}"):
                continue
            # 列表/聚合页链接（tag/栏目/作者/搜索/分页）不是文章：不进入候选，
            # 避免派发后把列表页当详情抓（正文会变成标题条目列表）。
            if (
                (not _ARTICLE_EXT_RE.search(parsed.path))
                and (
                    _LISTING_PATH_RE.search(parsed.path)
                    or _LISTING_PAGE_RE.search(parsed.path)
                    or _LISTING_QUERY_RE.search(parsed.query)
                )
            ):
                continue
            if include_pattern and not re.search(include_pattern, target):
                continue
            title = " ".join(anchor.get_text(" ", strip=True).split())
            if len(title) < 4:
                continue
            identity = target.split("#", 1)[0]
            if identity in seen:
                continue
            seen.add(identity)
            parent_text = ""
            parent = anchor.find_parent(["article", "li", "div"])
            if parent:
                parent_text = " ".join(parent.get_text(" ", strip=True).split())
            results.append(
                {
                    "url": target,
                    "title": title[:1000],
                    "summary": parent_text[:5000],
                    # 采集时就写发布时间：链接所在节点（article/li/div）的文本里几乎都带日期
                    "published_at": _listing_publish_time(parent_text, target),
                }
            )
            if len(results) >= limit:
                break
        return results


class PlaywrightListPageScanner:
    """Browser rendering used only for sources flagged after an HTTP 403."""

    def __init__(self, list_scanner: ListPageScanner):
        self.list_scanner = list_scanner

    def scan(self, source: Dict, *, limit: int) -> List[Dict]:
        from playwright.sync_api import sync_playwright
        from crawler_resource_manager import sync_playwright_slot

        url = validate_external_url(source["source_url"])
        with sync_playwright_slot(f"intel-list:{url}"):
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
                try:
                    context = browser.new_context(
                        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"),
                    )
                    page = context.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(1500)
                    _rendered_html = page.content()
                    # 阶段4：动态页先用已学模型提取（模型是在渲染后 HTML 上学的）；未命中自动回退启发式
                    try:
                        from site_scraper_models import extract_with_model
                        from sqlite_database import sqlite_db
                        model_items = extract_with_model(sqlite_db, page.url, _rendered_html, limit=limit)
                        if model_items:
                            return model_items
                    except Exception:
                        pass
                    include_pattern = str(
                        (source.get("metadata") or {}).get("link_include_pattern") or ""
                    ).strip() or None
                    items = self.list_scanner.scan_html(
                        page.url, _rendered_html, limit=limit, include_pattern=include_pattern
                    )
                    if bool((source.get("metadata") or {}).get("scan_page_as_article")):
                        items = self.list_scanner.include_page_as_article(
                            page.url, page.content(), items, limit=limit
                        )
                    return items
                finally:
                    browser.close()


class GgzyTradingApiScanner:
    """全国公共资源交易平台公告列表接口扫描器。

    直接 POST /information/pubTradingInfo/getTradList（无需 key），拿到结构化公告记录，
    含 informationType（0101=招标/资审公告、0104=交易结果公示/中标、0105=澄清、开标记录），
    从而**同时覆盖"招标"与"中标"**。记录 url 是 a 页，正文由候选适配器切到 b 页 .detail。
    """

    scanner_type = "ggzy_api"
    ENDPOINT = "https://www.ggzy.gov.cn/information/pubTradingInfo/getTradList"

    def __init__(self, http_client: SafeHTTPClient = None):
        self.http_client = http_client or SafeHTTPClient()

    def scan(self, source: Dict, *, limit: int) -> List[Dict]:
        metadata = source.get("metadata") or {}
        endpoint = str(metadata.get("api_endpoint") or self.ENDPOINT)
        classifies = [str(value) for value in (metadata.get("deal_classify") or ["01", "02"])]
        # 数据来源：1=省平台，2=央企招投标（国家电网/中石油/中国通号等，最对口工控/能源/基础设施）
        source_types = [
            str(value) for value in (metadata.get("api_source_types") or ["1", "2"])
        ]
        deal_time = str(metadata.get("api_deal_time", "02"))  # 02=近十天
        today = time.strftime("%Y-%m-%d")
        window_days = int(coerce_int(metadata.get("api_window_days"), 10, 1, 90))
        from datetime import datetime as _dt, timedelta as _td
        begin = (_dt.strptime(today, "%Y-%m-%d") - _td(days=window_days)).strftime("%Y-%m-%d")
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        items = []
        # 行业聚焦：若源配置了 title_include_keywords，只保留标题命中任一关键词的公告，
        # 避免省平台里"农田/医院/文物"等非工控-安全-算力主题污染 customers。
        include_kws = [str(v).strip() for v in (metadata.get("title_include_keywords") or []) if str(v).strip()]
        for source_type in source_types:
            for classify in classifies:
                if len(items) >= limit:
                    break
                data = {
                    "DEAL_CLASSIFY": classify,
                    "SOURCE_TYPE": source_type,
                    "DEAL_TIME": deal_time,
                    "TIMEBEGIN": begin,
                    "TIMEEND": today,
                    "PAGENUMBER": "1",
                }
                try:
                    resp = requests.post(endpoint, headers=headers, data=data, timeout=25, verify=False)
                    payload = resp.json()
                except Exception as exc:
                    print(f"ggzy_api 请求失败 st={source_type} dept={classify}: {exc}")
                    continue
                records = ((payload.get("data") or {}).get("records") or [])
                for rec in records:
                    if len(items) >= limit:
                        break
                    item_url = urljoin("https://www.ggzy.gov.cn/", str(rec.get("url") or ""))
                    title = str(rec.get("title") or "").strip()
                    if not item_url or not title or "/information/deal/html/" not in item_url:
                        continue
                    if include_kws and not any(kw in title for kw in include_kws):
                        continue
                    info_type = str(rec.get("informationTypeText") or "")
                    business = str(rec.get("businessTypeText") or "")
                    province = str(rec.get("provinceText") or "")
                    items.append(
                        {
                            "url": item_url,
                            "title": title[:1000],
                            "summary": f"{info_type} {business} {province}".strip(),
                            "published_at": str(rec.get("publishTime") or ""),
                        }
                    )
        return items


class IntelLightScanner:
    def __init__(
        self,
        *,
        candidate_repository: IntelCandidateRepository = None,
        source_registry: IntelSourceRegistry = None,
        rss_scanner: RSSScanner = None,
        list_scanner: ListPageScanner = None,
        serpapi_client: SerpAPIClient = None,
        tavily_client: TavilyClient = None,
        url_validator=validate_external_url,
    ):
        self.candidates = candidate_repository or intel_candidate_repository
        self.sources = source_registry or intel_source_registry
        self.rss_scanner = rss_scanner or RSSScanner()
        self.list_scanner = list_scanner or ListPageScanner()
        self.playwright_list_scanner = PlaywrightListPageScanner(self.list_scanner)
        self.ggzy_api_scanner = GgzyTradingApiScanner()
        self.serpapi = serpapi_client or SerpAPIClient()
        # Tavily：与 serpapi 并列的搜索 provider（只负责发现 URL，后续同样走候选门禁）
        self.tavily = tavily_client or TavilyClient()
        self.url_validator = url_validator

    def _enabled_sources(
        self,
        industry_pack_id: str,
        source_ids: Optional[Iterable[int]],
        max_sources: int,
    ) -> List[Dict]:
        self.sources._ensure()
        effective_pack_ids = [
            pack["id"] for pack in industry_pack_loader.effective_pack_set(industry_pack_id)
        ]
        placeholders = ",".join("?" for _ in effective_pack_ids)
        filters = [
            "s.is_enabled=1",
            "si.is_active=1",
            f"si.industry_pack_id IN ({placeholders})",
        ]
        params: List = list(effective_pack_ids)
        ids = [coerce_int(value, 0, 1) for value in (source_ids or [])]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            filters.append(f"s.id IN ({placeholders})")
            params.extend(ids)
        params.append(max_sources)
        cursor = self.sources.db.connection.cursor()
        try:
            cursor.execute(
                f"""
                SELECT s.*, GROUP_CONCAT(DISTINCT si.industry_pack_id) AS attached_pack_ids
                FROM intel_sources s
                JOIN intel_source_industries si ON si.source_id=s.id
                WHERE {' AND '.join(filters)}
                GROUP BY s.id
                ORDER BY s.authority_level DESC, s.id
                LIMIT ?
                """,
                params,
            )
            sources = [dict(row) for row in cursor.fetchall()]
            for source in sources:
                try:
                    source["metadata"] = json.loads(source.get("metadata_json") or "{}")
                except (TypeError, ValueError):
                    source["metadata"] = {}
                attached = {
                    value
                    for value in str(source.get("attached_pack_ids") or "").split(",")
                    if value
                }
                declared = set(source["metadata"].get("declared_by_pack_ids") or attached)
                source["target_pack_ids"] = [
                    pack_id
                    for pack_id in effective_pack_ids
                    if pack_id in attached and pack_id in declared
                ]
            authorized_sources = []
            blocked_skipped: List[int] = []
            for source in sources:
                metadata = source.get("metadata") or {}
                if bool(metadata.get("on_demand_only")):
                    continue
                if (
                    source.get("source_type") == "rss"
                    and metadata.get("origin_pack_id") == "financial_markets"
                ):
                    if not rollout_capability_enabled("rss", config):
                        continue
                    decision = rss_authorization_decision(
                        str(source.get("source_url") or ""),
                        str(metadata.get("license_profile") or ""),
                        config,
                    )
                    source["license_decision"] = decision
                    if not decision["authorized"]:
                        continue
                # 反爬放弃策略：被同一厂商拦够次数（验证码型 2 次 / JS 传感器型 3 次 /
                # 其余 5-8 次）后，不再派发扫描任务——把爬取槽位让给能出正文的信源。
                if _should_skip_blocked_source(metadata):
                    blocked_skipped.append(int(source["id"]))
                    continue
                authorized_sources.append(source)
            if blocked_skipped:
                print(
                    f"🚫 跳过 {len(blocked_skipped)} 个「被反爬拦截达阈值」的信源: {blocked_skipped[:10]}",
                    flush=True,
                )
            self._blocked_skipped_source_ids = blocked_skipped
            return authorized_sources
        finally:
            cursor.close()

    def _record_items(
        self,
        items: Iterable[Dict],
        *,
        industry_pack_id: str,
        source_id: Optional[int],
        run_id: int,
        observation_type: str,
        query_text: str = "",
        bypass_industry_gate: bool = False,
        industry_pack_ids: Optional[Iterable[str]] = None,
        activation_id: str = "",
    ) -> Dict:
        stats = {
            "discovered_count": 0,
            "queued_count": 0,
            "duplicate_count": 0,
            "below_threshold_count": 0,
        }
        target_pack_ids = list(
            dict.fromkeys(str(value) for value in (industry_pack_ids or [industry_pack_id]))
        )
        for item in items:
            self.url_validator(item.get("url") or "")
            # SerpAPI query text is constrained by the industry pack, but a
            # general Google result page can still contain unrelated SEO noise.
            # Keep that result in the discovery audit, while only allowing a
            # direct industry-anchor hit to skip the old score threshold and
            # consume a full-page crawler slot.
            item_bypass_gate = bypass_industry_gate
            force_unqueued = False
            if observation_type == "serpapi" and bypass_industry_gate:
                pack = industry_pack_loader.load(industry_pack_id)
                item_bypass_gate = serpapi_preview_gate(item, pack, query_text)
                force_unqueued = not item_bypass_gate
            elif observation_type == "agent_reach":
                # 社媒/社区噪声大：只有标题或摘要命中行业锚点时，才允许它绕过打分
                # 直接占一个全文抓取槽；没命中的不强行作废，交给正常打分流程判断
                # （避免把"标题短但确实是行业内容"的线索一刀切掉）。
                pack = industry_pack_loader.load(industry_pack_id)
                item_bypass_gate = _agent_reach_preview_gate(item, pack, query_text)
            results = [
                self.candidates.discover(
                    item,
                    industry_pack_id=target_pack_id,
                    activation_id=activation_id,
                    source_id=source_id,
                    scan_run_id=run_id,
                    observation_type=observation_type,
                    query_text=query_text,
                    bypass_industry_gate=item_bypass_gate,
                    force_unqueued=force_unqueued,
                )
                for target_pack_id in target_pack_ids
            ]
            result = results[0]
            stats["discovered_count"] += 1
            if any(item_result["should_queue"] for item_result in results):
                stats["queued_count"] += 1
            else:
                stats["below_threshold_count"] += 1
            if not result["created"] or result["duplicate_observation"]:
                stats["duplicate_count"] += 1
        return stats

    def scan(
        self,
        *,
        industry_pack_id: str,
        source_ids: Optional[Iterable[int]] = None,
        scan_sources: bool = True,
        include_serpapi: bool = True,
        include_agent_reach: bool = True,
        max_sources: Optional[int] = None,
        max_items_per_source: Optional[int] = None,
        manual: bool = False,
        activation_id: str = "",
        force_rescan: bool = False,
        force_rescan_key: str = "",
        initialization_from: str = "",
        initialization_to: str = "",
    ) -> Dict:
        if not config.INTEL_LIGHT_SCANNER_ENABLED and not manual:
            return {"skipped": True, "reason": "automatic light scanner disabled", "runs": []}
        request_id = f"scan-{uuid.uuid4().hex}"
        scan_started = time.monotonic()
        pack = industry_pack_loader.load(industry_pack_id)
        explicit_source_ids = [
            coerce_int(value, 0, 1) for value in (source_ids or [])
        ]
        source_limit_ceiling = max(
            config.INTEL_SCAN_MAX_SOURCES_PER_RUN,
            len(explicit_source_ids) if manual else 0,
        )
        max_sources = coerce_int(
            max_sources,
            config.INTEL_SCAN_MAX_SOURCES_PER_RUN,
            1,
            source_limit_ceiling,
        )
        max_items = coerce_int(
            max_items_per_source,
            config.INTEL_SCAN_MAX_ITEMS_PER_SOURCE,
            1,
            config.INTEL_SCAN_MAX_ITEMS_PER_SOURCE,
        )
        # Google/SerpAPI has its own daily schedule.  It must be possible to
        # execute that discovery pass without accidentally re-scanning one of
        # the registered websites merely because no source id was supplied.
        sources = (
            self._enabled_sources(
                industry_pack_id,
                explicit_source_ids,
                max_sources,
            )
            if scan_sources
            else []
        )
        report = {
            "request_id": request_id,
            "industry_pack_id": industry_pack_id,
            "source_count": len(sources),
            "runs": [],
            "discovered_count": 0,
            "queued_count": 0,
            "failed_count": 0,
            "rate_limited": False,
            "reused_scan_count": 0,
        }
        for source in sources:
            run_started = time.monotonic()
            scanner_type = str(source.get("source_type") or "website")
            browser_enabled = bool((source.get("metadata") or {}).get("browser_fetch_enabled"))
            # 反爬/反爬策略 → 优先 RSS：只要信源提供了 RSS 订阅(source_type=rss 或 metadata.rss_url)，
            # 就用 RSS 抓取（更轻、更稳），而不是强刮列表页。
            rss_url = str((source.get("metadata") or {}).get("rss_url") or "").strip()
            if scanner_type == "ggzy_api":
                scanner = self.ggzy_api_scanner
            elif scanner_type == "rss" or rss_url:
                scanner = self.rss_scanner
            else:
                scanner = self.playwright_list_scanner if browser_enabled else self.list_scanner
            target_pack_ids = list(source.get("target_pack_ids") or [industry_pack_id])
            window_key = source_scan_window_key(source)
            if force_rescan:
                window_key = (
                    f"{window_key}:activation:"
                    f"{str(force_rescan_key or activation_id or request_id)}"
                )
            run_id, acquired = self.candidates.claim_scan_run(
                source_id=int(source["id"]),
                industry_pack_id=industry_pack_id,
                scanner_type=scanner_type,
                scan_window_key=window_key,
                requested_pack_ids=target_pack_ids,
                activation_id=activation_id,
                metadata={
                    "request_id": request_id,
                    "scan_window_key": window_key,
                    "target_pack_ids": target_pack_ids,
                    "force_rescan": bool(force_rescan),
                    "force_rescan_key": str(force_rescan_key or ''),
                    "initialization_from": str(initialization_from or ''),
                    "initialization_to": str(initialization_to or ''),
                },
            )
            if not acquired:
                report["reused_scan_count"] += 1
                report["runs"].append(
                    {
                        "run_id": run_id,
                        "request_id": request_id,
                        "source_id": source["id"],
                        "status": "skipped",
                        "reason": "source scan window already claimed",
                        "scan_window_key": window_key,
                        "target_pack_ids": target_pack_ids,
                    }
                )
                continue
            # ── T3.3 零请求跳过：sitemap/RSS 最新信号不晚于水位线 → 整源跳过（不发列表页请求）──
            if (not force_rescan and not initialization_from and not initialization_to
                    and getattr(config, "INTEL_SCAN_SKIP_NO_CHANGE_ENABLED", True)):
                try:
                    from crawl_waterline import check_source_no_change
                    _no_change = check_source_no_change(source)
                    if _no_change.get("skip"):
                        stats = {
                            "status": "skipped_no_change", "request_count": 0,
                            "metadata": {"latest_signal": _no_change.get("latest_signal") or "",
                                         "reason": "sitemap/RSS 信号不晚于水位线，无新内容"},
                        }
                        stats["metadata"] = {**stats["metadata"], "request_id": request_id,
                                             "duration_ms": round((time.monotonic() - run_started) * 1000),
                                             "scan_window_key": window_key,
                                             "target_pack_ids": target_pack_ids}
                        self.candidates.finish_scan_run(run_id, stats)
                        report["runs"].append({
                            "run_id": run_id, "request_id": request_id,
                            "source_id": source["id"], **stats,
                        })
                        report["skipped_no_change_count"] = report.get("skipped_no_change_count", 0) + 1
                        continue
                except Exception as exc:
                    print(f"⚠️ 零请求跳过判定失败（继续正常扫描）: {exc}")
            stats = {"status": "completed", "request_count": 1}
            try:
                items = _filter_items_to_window(
                    scanner.scan(source, limit=max_items),
                    initialization_from,
                    initialization_to,
                )
                # 政府监管信源（医保局/卫健委等官方栏目）：栏目本身即行业主题，
                # 标题级评分常因栏目名/短标题偏低而卡在 discovered；这里直接派发，
                # 正文层的行业锚点门禁仍会把无关内容拦下。
                _src_meta = source.get("metadata") or {}
                _bypass_gate = (
                    str(_src_meta.get("source_role") or "") == "government_regulator"
                    or int(source.get("authority_level") or 0) >= 5
                )
                stats.update(
                    self._record_items(
                        items,
                        industry_pack_id=industry_pack_id,
                        source_id=int(source["id"]),
                        run_id=run_id,
                        observation_type=scanner_type,
                        industry_pack_ids=target_pack_ids,
                        activation_id=activation_id,
                        bypass_industry_gate=_bypass_gate,
                    )
                )
            except Exception as exc:
                error_text = sanitize_external_error(exc)
                # 被反爬/动态页（HTTP 403/406/412/429、SSL 证书等）也标记为需要浏览器渲染，
                # 下次扫描改用 PlaywrightListPageScanner（本地真实浏览器）抓列表页。
                _browser_worthy = (
                    "403" in error_text
                    or "406" in error_text
                    or "412" in error_text
                    or "429" in error_text
                    or "ssl" in error_text.casefold()
                    or "certificate" in error_text.casefold()
                )
                if scanner_type != "rss" and not browser_enabled and _browser_worthy:
                    self.sources.update_source_metadata(
                        int(source["id"]),
                        {"browser_fetch_enabled": True, "browser_fetch_reason": "anti_bot"},
                    )
                stats.update(
                    {
                        "status": "failed",
                        "error_type": type(exc).__name__[:100],
                        "error_message": error_text,
                    }
                )
                report["failed_count"] += 1
            duration_ms = round((time.monotonic() - run_started) * 1000)
            stats["metadata"] = {
                "request_id": request_id,
                "duration_ms": duration_ms,
                "scan_window_key": window_key,
                "target_pack_ids": target_pack_ids,
            }
            self.candidates.finish_scan_run(run_id, stats)
            report["runs"].append(
                {
                    "run_id": run_id,
                    "request_id": request_id,
                    "duration_ms": duration_ms,
                    "source_id": source["id"],
                    **stats,
                }
            )
            report["discovered_count"] += stats.get("discovered_count", 0)
            report["queued_count"] += stats.get("queued_count", 0)

        if include_serpapi and config.SERPAPI_ENABLED and self.serpapi.configured:
            run_started = time.monotonic()
            # 追踪词优先：周报「下周关注」生成的注意力方向是本周期最该主动搜的线索，
            # 排在静态查询前面，保证被 SERPAPI_MAX_QUERIES_PER_RUN 截断时不会被挤掉。
            watch_queries = _attention_watch_queries(industry_pack_id)
            queries = watch_queries + list(pack.get("serpapi_queries") or [])
            # 重点品牌动态查询（采品牌新闻），与静态查询合并后截断
            queries += [f"{b} 新闻" for b in (pack.get("brands") or [])[:12]]
            queries = queries[: config.SERPAPI_MAX_QUERIES_PER_RUN]
            allocated = self.candidates.reserve_api_usage(
                "serpapi",
                len(queries),
                config.SERPAPI_DAILY_QUERY_BUDGET,
            )
            run_id = self.candidates.create_scan_run(
                source_id=None,
                industry_pack_id=industry_pack_id,
                scanner_type="serpapi",
                metadata={"query_count_requested": len(queries), "request_id": request_id},
                activation_id=activation_id,
            )
            serp_stats = {
                "status": "completed",
                "request_count": 0,
                "discovered_count": 0,
                "queued_count": 0,
                "duplicate_count": 0,
                "below_threshold_count": 0,
            }
            if allocated == 0 and queries:
                serp_stats.update(
                    {
                        "status": "rate_limited",
                        "error_type": "daily_budget_exhausted",
                        "error_message": "SerpAPI 每日额度已用完",
                    }
                )
                report["rate_limited"] = True
            else:
                failures = 0
                for query in queries[:allocated]:
                    try:
                        items = _filter_items_to_window(
                            self.serpapi.search(query)[:max_items],
                            initialization_from,
                            initialization_to,
                        )
                        serp_stats["request_count"] += 1
                        item_stats = self._record_items(
                            items,
                            industry_pack_id=industry_pack_id,
                            source_id=None,
                            run_id=run_id,
                            observation_type="serpapi",
                            query_text=query,
                            bypass_industry_gate=True,
                            activation_id=activation_id,
                        )
                        for field, value in item_stats.items():
                            serp_stats[field] += value
                    except Exception as exc:
                        failures += 1
                        serp_stats["error_type"] = type(exc).__name__[:100]
                        serp_stats["error_message"] = sanitize_external_error(
                            exc,
                            secrets=(config.SERPAPI_API_KEY,),
                        )
                if failures:
                    serp_stats["status"] = (
                        "failed" if failures == allocated else "partial"
                    )
                    report["failed_count"] += failures
            duration_ms = round((time.monotonic() - run_started) * 1000)
            serp_stats["metadata"] = {
                "query_count_requested": len(queries),
                "request_id": request_id,
                "duration_ms": duration_ms,
            }
            self.candidates.finish_scan_run(run_id, serp_stats)
            report["runs"].append(
                {
                    "run_id": run_id,
                    "request_id": request_id,
                    "duration_ms": duration_ms,
                    "source_id": None,
                    **serp_stats,
                }
            )
            report["discovered_count"] += serp_stats["discovered_count"]
            report["queued_count"] += serp_stats["queued_count"]

        # ── Tavily 分支（与上面 SerpAPI 完全对称）──
        # 只负责"发现 URL"：结果先入候选队列，后续仍走候选门禁（锚点/机构词 + 质量）→
        # 抓正文 → 分类 → 主题归属（由正文关键词决定，与搜索词无关）。
        # 模式：separate=每个关键词各搜一次（结果更全）；merged=多词 OR 合并成一次（省额度）。
        if config.TAVILY_ENABLED and self.tavily.configured:
            run_started = time.monotonic()
            # 追踪词同样进 Tavily：与 SerpAPI 对称，保证"上周追踪"的线索一定有搜索覆盖
            queries = (_attention_watch_queries(industry_pack_id)
                       + list(pack.get("serpapi_queries") or []))[: config.SEARCH_KEYWORDS_PER_PACK]
            # 主题级查询词（为"文章少的窄主题"单独配置，见行业包 manifest.topic_search_queries）：
            # 每个主题现在存 3 个搜索词（列表），取这 3 个词拼成一条查询（不再按一行拆分）；
            # 兼容旧格式（单个字符串）。按【当天日期】轮转取 SEARCH_TOPIC_QUERIES_PER_RUN 个主题
            # → 不引入额外状态，几天内覆盖全部窄主题 ✓
            _topic_q = pack.get("topic_search_queries") or {}
            _topic_list = []
            if isinstance(_topic_q, dict):
                for _v in _topic_q.values():
                    if isinstance(_v, (list, tuple)):
                        _terms = [str(x).strip() for x in _v if str(x).strip()]
                        if _terms:
                            _topic_list.append(" ".join(_terms))
                    elif str(_v or "").strip():
                        _topic_list.append(str(_v).strip())
            else:
                _topic_list = [str(v) for v in (_topic_q or []) if str(v or "").strip()]
            if _topic_list:
                _per_topic = max(0, int(getattr(config, "SEARCH_TOPIC_QUERIES_PER_RUN", 3) or 0))
                if _per_topic:
                    _start = utc_now().timetuple().tm_yday % len(_topic_list)
                    _picked = [_topic_list[(_start + _i) % len(_topic_list)] for _i in range(min(_per_topic, len(_topic_list)))]
                    queries += _picked
            if config.TAVILY_QUERY_MODE == "merged" and queries:
                queries = [" OR ".join(queries)]
            allocated = self.candidates.reserve_api_usage(
                "tavily", len(queries), config.TAVILY_MAX_CALLS_PER_RUN
            )
            run_id = self.candidates.create_scan_run(
                source_id=None,
                industry_pack_id=industry_pack_id,
                scanner_type="tavily",
                metadata={"query_count_requested": len(queries), "request_id": request_id,
                          "query_mode": config.TAVILY_QUERY_MODE},
                activation_id=activation_id,
            )
            tav_stats = {
                "status": "completed",
                "request_count": 0,
                "discovered_count": 0,
                "queued_count": 0,
                "duplicate_count": 0,
                "below_threshold_count": 0,
            }
            if allocated == 0 and queries:
                tav_stats.update({
                    "status": "rate_limited",
                    "error_type": "run_budget_exhausted",
                    "error_message": "Tavily 每轮调用上限已用完（到顶跳过，不硬跑）",
                })
                report["rate_limited"] = True
            else:
                failures = 0
                for query in queries[:allocated]:
                    try:
                        items = _filter_items_to_window(
                            self.tavily.search(query)[:max_items],
                            initialization_from,
                            initialization_to,
                        )
                        tav_stats["request_count"] += 1
                        item_stats = self._record_items(
                            items,
                            industry_pack_id=industry_pack_id,
                            source_id=None,
                            run_id=run_id,
                            observation_type="tavily",
                            query_text=query,
                            bypass_industry_gate=True,
                            activation_id=activation_id,
                        )
                        for field, value in item_stats.items():
                            tav_stats[field] += value
                    except Exception as exc:
                        failures += 1
                        tav_stats["error_type"] = type(exc).__name__[:100]
                        tav_stats["error_message"] = sanitize_external_error(
                            exc, secrets=(config.TAVILY_API_KEY,)
                        )
                if failures:
                    tav_stats["status"] = "failed" if failures == allocated else "partial"
                    report["failed_count"] += failures
            duration_ms = round((time.monotonic() - run_started) * 1000)
            tav_stats["metadata"] = {
                "query_count_requested": len(queries),
                "request_id": request_id,
                "duration_ms": duration_ms,
            }
            self.candidates.finish_scan_run(run_id, tav_stats)
            report["runs"].append({
                "run_id": run_id,
                "request_id": request_id,
                "duration_ms": duration_ms,
                "source_id": None,
                **tav_stats,
            })
            report["discovered_count"] += tav_stats["discovered_count"]
            report["queued_count"] += tav_stats["queued_count"]
            # 搜索跑完立即触发一次候选派发：让新 URL 秒级进入抓取（仍走候选门禁，不绕过）。
            # 仓库对象所在模块随重构变动，这里按候选模块探测，避免硬编码导入路径出错。
            try:
                _search_repo = None
                for _mod_name in ("intel_repository", "intel_database"):
                    try:
                        _mod = __import__(_mod_name, fromlist=["intel_repository"])
                    except Exception:
                        continue
                    _search_repo = getattr(_mod, "intel_repository", None)
                    if _search_repo is not None and hasattr(_search_repo, "enqueue_job"):
                        break
                    _search_repo = None
                if _search_repo is not None:
                    _search_repo.enqueue_job(
                        "candidate_dispatch",
                        f"search-dispatch:{industry_pack_id}:{request_id}",
                        {"industry_pack_id": industry_pack_id, "manual": True},
                        priority=5,
                        request_id=request_id,
                        created_by="tavily-search",
                    )
            except Exception:
                pass
        # ── Agent-Reach：社媒/社区平台的"关键词聚焦检索" ──────────────────────
        # 与 SerpAPI / Tavily 完全对称：只负责发现 URL → 入候选队列（仍走候选门禁）
        # → 抓正文 → 分类 → 主题归属。默认关闭（AGENT_REACH_ENABLED），先在单机验证。
        # 预算：平台数 × 关键词数 × 单次结果数 + 总耗时上限，全部硬边界，宁可少跑。
        _ar_queries = [
            str(value).strip() for value in (
                _attention_watch_queries(industry_pack_id)
                + list(pack.get("serpapi_queries") or [])
            ) if str(value or "").strip()
        ][: config.SEARCH_KEYWORDS_PER_PACK]
        if (
            include_agent_reach
            and getattr(config, "AGENT_REACH_ENABLED", False)
            and _ar_queries
        ):
            run_started = time.monotonic()
            _ar_budget = max(1, int(getattr(config, "AGENT_REACH_MAX_CALLS_PER_RUN", 6) or 6))
            allocated = self.candidates.reserve_api_usage(
                "agent_reach", min(len(_ar_queries), _ar_budget), _ar_budget
            )
            run_id = self.candidates.create_scan_run(
                source_id=None,
                industry_pack_id=industry_pack_id,
                scanner_type="agent_reach",
                metadata={
                    "request_id": request_id,
                    "queries": _ar_queries,
                    "platforms": str(getattr(config, "AGENT_REACH_PLATFORMS", "") or ""),
                },
                activation_id=activation_id,
            )
            ar_stats = {
                "status": "completed",
                "request_count": 0,
                "discovered_count": 0,
                "queued_count": 0,
                "duplicate_count": 0,
                "below_threshold_count": 0,
                "platforms": [],
                "unavailable_platforms": [],
                "error_type": "",
                "error_message": "",
            }
            if allocated == 0:
                ar_stats.update({
                    "status": "rate_limited",
                    "error_type": "run_budget_exhausted",
                    "error_message": "Agent-Reach 每轮检索预算已用完（到顶跳过，不硬跑）",
                })
                report["rate_limited"] = True
            else:
                try:
                    _ar = _agent_reach_search_pack(_ar_queries)
                    ar_stats["platforms"] = list(_ar.get("platforms") or [])
                    ar_stats["unavailable_platforms"] = [
                        f"{item.get('platform')}:{item.get('reason')}"
                        for item in (_ar.get("unavailable") or [])
                    ]
                    ar_stats["request_count"] = len(ar_stats["platforms"]) * len(_ar_queries)
                    if _ar.get("timeout"):
                        ar_stats["status"] = "partial"
                        ar_stats["error_message"] = "超出总耗时预算，已提前停止"
                    _ar_items = _filter_items_to_window(
                        _ar.get("items") or [], initialization_from, initialization_to
                    )
                    if _ar_items:
                        item_stats = self._record_items(
                            _ar_items,
                            industry_pack_id=industry_pack_id,
                            source_id=None,
                            run_id=run_id,
                            observation_type="agent_reach",
                            query_text="",
                            bypass_industry_gate=True,
                            activation_id=activation_id,
                            industry_pack_ids=[industry_pack_id],
                        )
                        for field, value in item_stats.items():
                            ar_stats[field] += value
                except Exception as exc:
                    ar_stats["status"] = "failed"
                    ar_stats["error_type"] = type(exc).__name__[:100]
                    ar_stats["error_message"] = sanitize_external_error(exc)
                    report["failed_count"] += 1
            duration_ms = round((time.monotonic() - run_started) * 1000)
            ar_stats["metadata"] = {
                "request_id": request_id,
                "duration_ms": duration_ms,
                "queries": _ar_queries,
            }
            self.candidates.finish_scan_run(run_id, ar_stats)
            report["runs"].append({
                "run_id": run_id,
                "request_id": request_id,
                "duration_ms": duration_ms,
                "source_id": None,
                **ar_stats,
            })
            report["discovered_count"] += ar_stats["discovered_count"]
            report["queued_count"] += ar_stats["queued_count"]
            print(
                f"🔎 Agent-Reach 聚焦检索：平台={ar_stats['platforms']} "
                f"查询={len(_ar_queries)} 发现={ar_stats['discovered_count']} "
                f"入队={ar_stats['queued_count']} 耗时={duration_ms}ms",
                flush=True,
            )
            # 与 Tavily 一样：检索完立即催一次候选派发，让新 URL 秒级进入抓取
            # （依然要过候选门禁，不绕过）。
            try:
                _ar_repo = None
                for _mod_name in ("intel_repository", "intel_database"):
                    try:
                        _mod = __import__(_mod_name, fromlist=["intel_repository"])
                    except Exception:
                        continue
                    _ar_repo = getattr(_mod, "intel_repository", None)
                    if _ar_repo is not None and hasattr(_ar_repo, "enqueue_job"):
                        break
                    _ar_repo = None
                if _ar_repo is not None:
                    _ar_repo.enqueue_job(
                        "candidate_dispatch",
                        f"agent-reach-dispatch:{industry_pack_id}:{request_id}",
                        {"industry_pack_id": industry_pack_id, "manual": True},
                        priority=5,
                        request_id=request_id,
                        created_by="agent-reach-search",
                    )
            except Exception:
                pass
        report["duration_ms"] = round((time.monotonic() - scan_started) * 1000)
        return report


intel_light_scanner = IntelLightScanner()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one market-intelligence source scan")
    parser.add_argument("--industry", default=config.INTEL_DEFAULT_INDUSTRY_PACK)
    parser.add_argument("--source-id", action="append", type=int, default=[])
    parser.add_argument("--without-serpapi", action="store_true")
    parser.add_argument("--without-agent-reach", action="store_true")
    parser.add_argument("--max-sources", type=int, default=config.INTEL_SCAN_MAX_SOURCES_PER_RUN)
    parser.add_argument("--max-items", type=int, default=config.INTEL_SCAN_MAX_ITEMS_PER_SOURCE)
    args = parser.parse_args()
    result = intel_light_scanner.scan(
        industry_pack_id=args.industry,
        source_ids=args.source_id or None,
        include_serpapi=not args.without_serpapi,
        include_agent_reach=not args.without_agent_reach,
        max_sources=args.max_sources,
        max_items_per_source=args.max_items,
        manual=True,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("failed_count", 0) == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

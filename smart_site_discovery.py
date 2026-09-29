#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能站点栏目发现模块

给定一个网站首页 URL，自动分析导航结构，找出与关键词最相关的内容栏目，
避免用户手动配置具体子路径（如 /news、/legal-updates 等）。

核心逻辑：
1. 抓取首页，提取导航/菜单中的所有内链
2. 过滤出看起来像内容列表页的链接（排除登录/搜索/关于等）
3. 对每个候选栏目：抓取列表页，采样几篇文章正文，检查关键词匹配率
4. 按匹配率排序，返回 TOP-N
"""

from __future__ import annotations

import re
import time
import logging
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from keyword_filter import KeywordFilter
from outbound_url_policy import read_response_text_limited, safe_request_get

logger = logging.getLogger(__name__)

# 排除链接的关键路径片段（登录/注册/搜索/社媒/管理等）
_EXCLUDE_PATH_PATTERNS = re.compile(
    r'(login|register|signup|search|cart|checkout|account|admin|dashboard'
    r'|contact|about|privacy|terms|sitemap|feed|rss|cdn|static|assets'
    r'|javascript:|mailto:|tel:|#'
    r'|\.(pdf|doc|xls|jpg|png|gif|zip|exe)$)',
    re.IGNORECASE,
)

# 看起来像内容列表页的路径特征
_CONTENT_PATH_HINTS = re.compile(
    r'(news|article|post|blog|insight|update|report|research|analysis'
    r'|publication|resource|press|media|legal|law|policy|regulation'
    r'|view|read|topic|column|category|tag|section|channel|review|commentary'
    r'|新闻|文章|资讯|洞察|观点|研究|报告|动态|公告|政策|法规'
    r'|法评|法律|合规|监管|评论|更新|解读|分析|发布|专栏|专题)',
    re.IGNORECASE,
)

_DEFAULT_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
}


def _clean_raw_url(value: str) -> str:
    """Trim raw URLs before joining/parsing; some sites emit padded href values."""
    return str(value or '').strip()


def _clean_absolute_url(base_url: str, raw_url: str) -> Optional[tuple[str, object]]:
    """Return a stripped absolute URL and parsed object for consistent analysis."""
    raw_url = _clean_raw_url(raw_url)
    if not raw_url:
        return None
    abs_url = urljoin(base_url, raw_url).strip()
    return abs_url, urlparse(abs_url)


def _fetch(url: str, timeout: int = 10) -> Optional[str]:
    """抓取页面；直连失败时使用系统代理重试，失败返回 None。"""
    attempts = (
        {'proxies': {'http': None, 'https': None}},
        {},
    )
    last_error = None
    for options in attempts:
        try:
            resp = safe_request_get(
                url,
                headers=_DEFAULT_HEADERS,
                timeout=timeout,
                stream=True,
                **options,
            )
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or 'utf-8'
            return read_response_text_limited(resp)
        except Exception as e:
            last_error = e
    logger.debug('fetch %s 失败: %s', url, last_error)
    return None


def _same_domain_links(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """提取首页所有同域内链（含 <a> 标签 + 脚本内嵌路由，兼容 SPA）"""
    parsed_base = urlparse(base_url)
    base_domain = parsed_base.netloc.lower()
    scheme = parsed_base.scheme
    seen: set[str] = set()
    links = []

    def _add(path: str, text: str = '') -> None:
        path = _clean_raw_url(path)
        if not path or path == '/':
            return
        cleaned = _clean_absolute_url(base_url, path)
        if not cleaned:
            return
        _, parsed = cleaned
        if parsed.netloc.lower() != base_domain:
            return
        clean_path = _clean_raw_url(parsed.path) or '/'
        clean = f'{scheme}://{parsed_base.netloc}{clean_path}'.rstrip('/')
        if clean == base_url.rstrip('/') or clean in seen:
            return
        if _EXCLUDE_PATH_PATTERNS.search(parsed.path):
            return
        seen.add(clean)
        links.append({'url': clean, 'text': text[:80], 'path': parsed.path})

    # 1. <a href> 标签
    for a in soup.find_all('a', href=True):
        href = _clean_raw_url(a['href'])
        _add(href, a.get_text(strip=True))

    # 2. 脚本内嵌路由（SPA 常见："/legal-updates" 或 path:"/xxx"）
    script_path_re = re.compile(r'["\'](/[a-zA-Z][a-zA-Z0-9\-]{2,40})["\']')
    for script in soup.find_all('script'):
        for m in script_path_re.finditer(script.get_text()):
            _add(_clean_raw_url(m.group(1)))
    return links


def _score_link(link: dict) -> float:
    """给单个链接打"像内容列表页"的初步分"""
    path = link['path'].lower()
    text = link['text']
    score = 0.0
    # 路径含内容提示词加分
    if _CONTENT_PATH_HINTS.search(path):
        score += 3.0
    # 链接文字含内容提示词加分
    if _CONTENT_PATH_HINTS.search(text):
        score += 2.0
    # 路径层级：/foo 算1级，/foo/bar 算2级（用 rstrip('/').count('/') 修正）
    depth = path.rstrip('/').count('/')
    if depth == 1:
        score += 2.0   # 一级路径（列表页）最优
    elif depth == 2:
        score += 1.0
    elif depth == 0:
        score -= 0.5   # 根路径
    else:
        score -= 1.0   # 三级以上往往是具体文章
    # 路径含数字 ID 减分（可能是具体文章）
    if re.search(r'/\d{3,}', path):
        score -= 2.0
    return score


def _extract_article_links_from_listing(html: str, section_url: str, limit: int = 5) -> list[str]:
    """从列表页中提取文章链接（简单启发式）"""
    soup = BeautifulSoup(html, 'html.parser')
    parsed_base = urlparse(section_url)
    base_domain = parsed_base.netloc
    seen: set[str] = set()
    candidates: list[tuple[float, str]] = []

    for a in soup.find_all('a', href=True):
        cleaned = _clean_absolute_url(section_url, a['href'])
        if not cleaned:
            continue
        _, parsed = cleaned
        if parsed.netloc != base_domain:
            continue
        path = _clean_raw_url(parsed.path) or '/'
        # 文章 URL 通常有数字 ID 或较深路径
        has_id = bool(re.search(r'/\d{3,}', path))
        depth = path.strip('/').count('/')
        if depth < 1:
            continue
        if _EXCLUDE_PATH_PATTERNS.search(path):
            continue
        clean = f'{parsed.scheme}://{parsed.netloc}{path}'.strip()
        if clean in seen or clean == section_url:
            continue
        seen.add(clean)
        score = float(has_id) * 2.0 + min(depth, 4) * 0.5
        candidates.append((score, clean))

    # 按分数排序取 top-N
    candidates.sort(reverse=True)
    return [url for _, url in candidates[:limit]]


def _keyword_matches(text: str, kw_filter: KeywordFilter) -> bool:
    """简单检查文本中是否有任意关键词命中"""
    if not text:
        return False
    return kw_filter.match_article({'title': '', 'content': text})


def discover_site_sections(
    homepage_url: str,
    keywords: list[str],
    max_candidates: int = 20,
    max_results: int = 5,
    article_sample_size: int = 5,
) -> list[dict]:
    """
    分析网站首页，返回与关键词最相关的内容栏目列表。

    Parameters
    ----------
    homepage_url : str
        网站首页地址（也可是域名，如 www.junhe.com）
    keywords : list[str]
        关键词列表
    max_candidates : int
        最多分析多少个候选栏目
    max_results : int
        最终返回数量
    article_sample_size : int
        每个栏目采样几篇文章

    Returns
    -------
    list[dict]
        [{'url': ..., 'title': ..., 'score': float, 'match_count': int, 'sample_total': int}]
    """
    # 规范化 URL
    if not homepage_url.startswith('http'):
        homepage_url = 'https://' + homepage_url.lstrip('/')

    # KeywordFilter 接收逗号分隔字符串
    kw_filter = KeywordFilter(','.join(keywords) if isinstance(keywords, list) else keywords)

    # 1. 抓取首页
    html = _fetch(homepage_url)
    if not html:
        logger.warning('无法抓取首页: %s', homepage_url)
        return []

    soup = BeautifulSoup(html, 'html.parser')

    # 2. 提取导航链接并初步过滤
    links = _same_domain_links(soup, homepage_url)

    # 优先从 <nav>/<header> 中提取（导航结构更可靠）
    nav_links: list[dict] = []
    non_nav_links: list[dict] = []
    nav_elements = soup.find_all(['nav', 'header', '[class*="menu"]', '[class*="nav"]'])
    nav_hrefs: set[str] = set()
    for nav in nav_elements:
        for a in nav.find_all('a', href=True):
            cleaned = _clean_absolute_url(homepage_url, a['href'])
            if cleaned:
                _, parsed = cleaned
                nav_hrefs.add((_clean_raw_url(parsed.path) or '/').rstrip('/'))

    for lk in links:
        path = lk['path'].rstrip('/')
        if path in nav_hrefs:
            nav_links.append(lk)
        else:
            non_nav_links.append(lk)

    # 导航链接优先，补充非导航链接
    ordered = nav_links + non_nav_links

    # 3. 按初步分数排序，取 top max_candidates
    scored = sorted(ordered, key=_score_link, reverse=True)
    candidates = scored[:max_candidates]

    if not candidates:
        logger.info('首页 %s 未发现候选栏目', homepage_url)
        return []

    # 4. 对每个候选栏目：抓列表页 → 采样文章 → 关键词匹配
    results: list[dict] = []
    for lk in candidates:
        section_url = lk['url']
        section_html = _fetch(section_url, timeout=8)
        if not section_html:
            continue

        article_urls = _extract_article_links_from_listing(
            section_html, section_url, limit=article_sample_size
        )
        match_count = 0
        sample_total = len(article_urls)

        # 先检查列表页自身文本（含标题摘要），快速预判相关性
        section_soup_pre = BeautifulSoup(section_html, 'html.parser')
        for tag in section_soup_pre(['script', 'style', 'noscript']):
            tag.decompose()
        listing_text = section_soup_pre.get_text(separator=' ', strip=True)
        listing_hit = _keyword_matches(listing_text, kw_filter)

        for art_url in article_urls:
            art_html = _fetch(art_url, timeout=8)
            if not art_html:
                continue
            art_soup = BeautifulSoup(art_html, 'html.parser')
            for tag in art_soup(['script', 'style', 'noscript']):
                tag.decompose()
            text = art_soup.get_text(separator=' ', strip=True)
            if _keyword_matches(text, kw_filter):
                match_count += 1

        # 综合分：关键词匹配率 × 权重 + 路径结构分 + 列表页自身命中奖励
        kw_rate = match_count / max(sample_total, 1)
        struct_score = _score_link(lk)
        listing_bonus = 2.0 if listing_hit else 0.0
        final_score = kw_rate * 10 + struct_score + listing_bonus

        # 取页面标题作为栏目名称
        section_soup = BeautifulSoup(section_html, 'html.parser')
        title_tag = section_soup.find('title')
        title = title_tag.get_text(strip=True)[:60] if title_tag else lk['text']

        results.append({
            'url': section_url,
            'title': title,
            'link_text': lk['text'],
            'score': round(final_score, 2),
            'match_count': match_count,
            'sample_total': sample_total,
        })

        time.sleep(0.3)  # 礼貌性延迟，避免对目标站造成压力

    # 5. 按综合分排序
    results.sort(key=lambda x: x['score'], reverse=True)
    return results[:max_results]

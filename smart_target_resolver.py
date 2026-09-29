#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Smart target URL resolver for scheduled crawl tasks.

The resolver turns a maintained task URL, often a homepage, into high-value
derived crawl targets. It does not update scheduled_tasks directly; callers can
decide whether to use the targets for the current run, show them in the UI, or
persist them as derived tasks.
"""

from __future__ import annotations

import re
from typing import Dict, List
from urllib.parse import quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from smart_site_discovery import discover_site_sections
from url_validation_helper import normalize_task_url
from utils import coerce_int


DEFAULT_SMART_RESOLVER_KEYWORDS = (
    '家族办公室,家族辦公室,家庭办公室,家庭辦公室,'
    '家族信托,家族信託,家庭信托,家庭信託,'
    'family office,family offices,family trust,family wealth'
)


def split_keyword_text(value) -> List[str]:
    """Split configured keywords while preserving phrases with spaces."""
    seen = set()
    keywords = []
    for item in str(value or '').replace('，', ',').replace('；', ';').split(','):
        for token in item.replace(';', '\n').splitlines():
            keyword = token.strip()
            if keyword and keyword not in seen:
                seen.add(keyword)
                keywords.append(keyword)
    return keywords


def canonical_url(url: str) -> str:
    """Normalize enough for URL de-dupe without dropping meaningful queries."""
    try:
        parsed = urlparse(str(url or '').strip())
        if not parsed.scheme or not parsed.netloc:
            return ''
        path = (parsed.path or '/').strip().rstrip('/') or '/'
        query = f'?{parsed.query}' if parsed.query else ''
        return f'{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}{query}'
    except Exception:
        return ''


def is_probable_article_detail_url(url: str) -> bool:
    """Return True for likely article detail pages, after trimming URL text."""
    try:
        path = urlparse(str(url or '').strip()).path.strip().rstrip('/')
    except Exception:
        return False
    if not path or path == '/':
        return False
    if path.lower().endswith(('.pdf', '.doc', '.docx', '.xls', '.xlsx', '.jpg', '.png', '.zip')):
        return False
    return bool(
        re.search(r'/\d{2,}$', path)
        or re.search(r'/\d{4}/\d{1,2}/', path)
        or path.lower().endswith(('.html', '.htm'))
    )


def parent_section_url(article_url: str) -> str:
    """Infer the section/list URL for a detail page."""
    parsed = urlparse(str(article_url or '').strip())
    parts = [part for part in parsed.path.split('/') if part]
    if len(parts) >= 2:
        return f'{parsed.scheme}://{parsed.netloc}/' + '/'.join(parts[:-1])
    return f'{parsed.scheme}://{parsed.netloc}'


def matched_keywords_in_text(text: str, keywords: List[str]) -> List[str]:
    if not text or not keywords:
        return []
    try:
        from keyword_filter import KeywordFilter
        return KeywordFilter(','.join(keywords)).get_matched_keywords(text)
    except Exception:
        low = text.lower()
        return [kw for kw in keywords if kw and kw.lower() in low]


def fetch_html(url: str, timeout: int = 15) -> tuple[str, str]:
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36'
        ),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    }
    response = requests.get(url, headers=headers, timeout=timeout, verify=False, allow_redirects=True)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding or 'utf-8'
    return response.text, response.url


def extract_visible_text_and_title(html: str) -> tuple[str, str, BeautifulSoup]:
    soup = BeautifulSoup(html or '', 'html.parser')
    for tag in soup(['script', 'style', 'noscript']):
        tag.decompose()
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    return title, soup.get_text(' ', strip=True), soup


def looks_like_error_page(title: str, text: str, url: str = '') -> bool:
    combined = f'{title or ""} {text or ""}'.lower()
    error_markers = (
        '404',
        '无法访问此页面',
        '页面不存在',
        'not found',
        'page not found',
        'the page you are looking for',
    )
    if any(marker in combined for marker in error_markers):
        return True
    if url and '/404' in urlparse(url).path.lower():
        return True
    return False


def discover_verified_keyword_urls(
    homepage_url: str,
    keywords: List[str],
    max_articles_per_keyword: int = 8,
) -> Dict:
    """Use site search to find and verify real keyword-matching articles."""
    parsed_home = urlparse(homepage_url)
    origin = f'{parsed_home.scheme}://{parsed_home.netloc}'
    section_map = {}
    search_suggestions = []
    verified_articles = []
    seen_articles = set()

    for keyword in keywords:
        search_url = f'{origin}/search?q={quote(keyword)}'
        try:
            html, final_search_url = fetch_html(search_url)
            _, _, soup = extract_visible_text_and_title(html)
        except Exception:
            continue

        article_candidates = []
        seen_candidate_urls = set()
        for link in soup.find_all('a', href=True):
            candidate_url = urljoin(final_search_url, (link.get('href') or '').strip()).strip()
            parsed_candidate = urlparse(candidate_url)
            if parsed_candidate.netloc.lower() != parsed_home.netloc.lower():
                continue
            clean_path = (parsed_candidate.path or '/').strip().rstrip('/') or '/'
            clean_url = f'{parsed_candidate.scheme}://{parsed_candidate.netloc}{clean_path}'
            if parsed_candidate.query:
                clean_url += f'?{parsed_candidate.query}'
            key = canonical_url(clean_url)
            if not key or key in seen_candidate_urls:
                continue
            if not is_probable_article_detail_url(clean_url):
                continue
            text = link.get_text(' ', strip=True)
            if not text and not re.search(r'/(news|legal-updates|deals|humanities)/', parsed_candidate.path):
                continue
            seen_candidate_urls.add(key)
            article_candidates.append({'url': clean_url, 'title': text[:120]})
            if len(article_candidates) >= max_articles_per_keyword:
                break

        keyword_verified_articles = []
        for candidate in article_candidates:
            article_key = canonical_url(candidate['url'])
            try:
                article_html, final_article_url = fetch_html(candidate['url'])
                title, text, _ = extract_visible_text_and_title(article_html)
            except Exception:
                continue
            if looks_like_error_page(title, text, final_article_url):
                continue
            matched = matched_keywords_in_text(f'{title} {text}', keywords)
            if not matched:
                continue

            article_info = {
                'url': final_article_url,
                'title': title or candidate.get('title') or final_article_url,
                'matched_keywords': matched,
            }
            keyword_verified_articles.append(article_info)
            if article_key not in seen_articles:
                seen_articles.add(article_key)
                verified_articles.append(article_info)

            section_url = parent_section_url(final_article_url)
            section = section_map.setdefault(section_url, {
                'url': section_url,
                'title': '关键词命中文章所在栏目',
                'link_text': '关键词命中文章所在栏目',
                'score': 20,
                'match_count': 0,
                'sample_total': 0,
                'matched_keywords': [],
                'verified_articles': [],
                'source': 'site_search_verified_section',
                'auto_checked': True,
            })
            section['match_count'] += 1
            section['sample_total'] = max(section['sample_total'], len(article_candidates))
            section['verified_articles'].append(article_info)
            section['matched_keywords'] = sorted(set(section['matched_keywords'] + matched))

        if keyword_verified_articles:
            search_suggestions.append({
                'url': final_search_url,
                'title': f'站内搜索：{keyword}',
                'link_text': f'站内搜索：{keyword}',
                'score': 19,
                'match_count': len(keyword_verified_articles),
                'sample_total': len(article_candidates),
                'matched_keywords': sorted({kw for article in keyword_verified_articles for kw in article['matched_keywords']}),
                'verified_articles': keyword_verified_articles,
                'source': 'site_search_verified',
                'auto_checked': True,
            })

    return {
        'search_suggestions': search_suggestions,
        'section_suggestions': list(section_map.values()),
        'verified_articles': verified_articles,
    }


def _serialize_suggestion(item: Dict, current_path: str = '') -> Dict:
    item_url = item.get('url', '')
    item_path = urlparse(item_url).path.rstrip('/')
    return {
        'url': item_url,
        'title': item.get('title', ''),
        'link_text': item.get('link_text', ''),
        'score': item.get('score', 0),
        'match_count': item.get('match_count', 0),
        'sample_total': item.get('sample_total', 0),
        'is_current': item_path == current_path,
        'matched_keywords': item.get('matched_keywords', []),
        'verified_articles': item.get('verified_articles', []),
        'source': item.get('source', 'section_sampling'),
        'auto_checked': item.get('auto_checked', False),
        'target_type': item.get('target_type', 'section'),
    }


def resolve_smart_targets(
    current_url: str,
    keywords,
    max_candidates: int = 30,
    max_results: int = 12,
    article_sample_size: int = 5,
    max_articles_per_keyword: int = 8,
) -> Dict:
    """Resolve one configured URL into current-run target URL suggestions."""
    normalized_url = normalize_task_url(str(current_url or '').strip())
    parsed = urlparse(normalized_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError('目标URL格式不正确')

    keyword_list = split_keyword_text(keywords)
    if not keyword_list:
        raise ValueError('关键词不能为空')

    homepage_url = f'{parsed.scheme}://{parsed.netloc}'
    current_path = parsed.path.rstrip('/')

    verified = discover_verified_keyword_urls(
        homepage_url,
        keyword_list,
        max_articles_per_keyword=max_articles_per_keyword,
    )
    sections = discover_site_sections(
        homepage_url,
        keyword_list,
        max_candidates=coerce_int(max_candidates, 30, 5, 100),
        max_results=coerce_int(max_results, 12, 1, 20),
        article_sample_size=coerce_int(article_sample_size, 5, 1, 10),
    )

    current_match = next(
        (s for s in sections if urlparse(s.get('url', '')).path.rstrip('/') == current_path),
        None
    )
    current_match_count = current_match.get('match_count', 0) if current_match else 0
    best_match_count = max((s.get('match_count', 0) for s in sections), default=0)

    suggestions = []
    exploration_urls = []
    seen_urls = set()

    for item in verified['search_suggestions']:
        item = dict(item)
        item['target_type'] = 'site_search'
        key = canonical_url(item.get('url', ''))
        if key and key not in seen_urls:
            seen_urls.add(key)
            suggestions.append(_serialize_suggestion(item, current_path))

    for item in verified['section_suggestions']:
        item = dict(item)
        item['target_type'] = 'verified_section'
        key = canonical_url(item.get('url', ''))
        if key and key not in seen_urls:
            seen_urls.add(key)
            suggestions.append(_serialize_suggestion(item, current_path))

    for section in sections:
        section_url = section.get('url', '')
        if is_probable_article_detail_url(section_url):
            continue
        section = dict(section)
        section['target_type'] = 'section'
        key = canonical_url(section_url)
        serialized = _serialize_suggestion(section, current_path)
        exploration_urls.append(serialized)
        if not key or key in seen_urls:
            continue
        if section.get('match_count', 0) > 0 or section.get('score', 0) >= 6.0:
            seen_urls.add(key)
            suggestions.append(serialized)

    if current_match_count > 0:
        status = 'ok'
    elif suggestions:
        status = 'suggest_change'
    elif best_match_count > 0:
        status = 'suggest_change'
    else:
        status = 'no_match_found'

    suggestions.sort(
        key=lambda x: (
            1 if x.get('auto_checked') else 0,
            coerce_int(x.get('match_count'), 0),
            float(x.get('score') or 0),
        ),
        reverse=True,
    )

    return {
        'current_url': normalized_url,
        'homepage': homepage_url,
        'status': status,
        'keywords': keyword_list,
        'current_match_count': current_match_count,
        'best_match_count': best_match_count,
        'suggestions': suggestions[:5],
        'exploration_urls': (suggestions or exploration_urls)[:8],
        'all_top5': [
            {
                'url': s.get('url', ''),
                'score': s.get('score', 0),
                'match_count': s.get('match_count', 0),
                'sample_total': s.get('sample_total', 0),
            }
            for s in sections if not is_probable_article_detail_url(s.get('url', ''))
        ],
        'verified_articles': verified.get('verified_articles', []),
    }


def resolve_task_smart_targets(task: Dict, keywords=None, **kwargs) -> Dict:
    """Resolve smart targets for a scheduled task dictionary."""
    task_url = (task.get('target_url') or '').strip()
    task_keywords = keywords if keywords is not None else task.get('keywords') or DEFAULT_SMART_RESOLVER_KEYWORDS
    result = resolve_smart_targets(task_url, task_keywords, **kwargs)
    result.update({
        'task_id': task.get('id'),
        'task_name': task.get('task_name') or task_url,
    })
    return result

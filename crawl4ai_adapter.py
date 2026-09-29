#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility adapter for the authenticated remote Crawl4AI pipeline."""

from __future__ import annotations

import asyncio
import os
import re
from typing import Dict, List
from urllib.parse import urlparse

from smart_target_resolver import looks_like_error_page, matched_keywords_in_text


def is_crawl4ai_available() -> tuple[bool, str]:
    try:
        from remote_pipeline_client import remote_pipeline_client
        route = remote_pipeline_client.choose_route()
        if route.channel != 'remote_crawl4ai':
            return False, route.reason
        return True, ''
    except Exception as exc:
        return False, str(exc)


def should_escalate_to_crawl4ai(primary_result: Dict, smart_targets: List[str] = None) -> tuple[bool, str]:
    """Decide whether Crawl4AI fallback should run after the primary crawler."""
    if not isinstance(primary_result, dict):
        return True, 'primary_invalid_result'
    if primary_result.get('success') is False:
        return True, 'primary_failed'

    stats = {}
    data = primary_result.get('data') if isinstance(primary_result.get('data'), dict) else {}
    if isinstance(data.get('stats'), dict):
        stats.update(data.get('stats'))
    if isinstance(primary_result.get('stats'), dict):
        stats.update(primary_result.get('stats'))

    articles_found = primary_result.get('articles_found')
    if articles_found is None:
        articles_found = stats.get('success') or stats.get('articles_found') or 0
    try:
        articles_found = int(articles_found or 0)
    except Exception:
        articles_found = 0

    if articles_found <= 0:
        return True, 'primary_zero_articles'
    if smart_targets and primary_result.get('smart_target_queue_size') and articles_found <= 0:
        return True, 'smart_resolver_found_hits_primary_missed'
    if stats.get('keyword_hits') == 0:
        return True, 'primary_zero_keyword_hits'
    if stats.get('avg_content_length') is not None:
        try:
            if float(stats.get('avg_content_length') or 0) < 300:
                return True, 'primary_content_too_short'
        except Exception:
            pass
    return False, ''


def _markdown_to_text(markdown_value) -> str:
    if markdown_value is None:
        return ''
    if isinstance(markdown_value, str):
        return markdown_value
    for attr in ('fit_markdown', 'raw_markdown', 'markdown'):
        value = getattr(markdown_value, attr, None)
        if value:
            return str(value)
    return str(markdown_value)


def _html_title(html: str) -> str:
    match = re.search(r'<title[^>]*>(.*?)</title>', html or '', re.I | re.S)
    if not match:
        return ''
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', match.group(1))).strip()


def _normalize_crawl4ai_result(result, url: str, keywords: List[str], fallback_reason: str = '') -> Dict:
    success = bool(getattr(result, 'success', False))
    final_url = getattr(result, 'url', None) or url
    html = getattr(result, 'html', '') or ''
    markdown = _markdown_to_text(getattr(result, 'markdown', ''))
    title = getattr(result, 'title', '') or _html_title(html) or final_url
    text = markdown.strip() or re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html)).strip()
    is_error_page = looks_like_error_page(title, text, final_url)
    matched = matched_keywords_in_text(f'{title} {text}', keywords)
    domain = urlparse(final_url).netloc

    article = {
        'url': final_url,
        'title': title[:300],
        'content': text,
        'domain': domain,
        'matched_keywords': matched,
        'crawler_engine_used': 'crawl4ai',
        'crawler_engines': ['crawl4ai'],
        'crawler_attempts': 1,
        'fallback_trigger_reason': fallback_reason,
        'source_method': 'crawl4ai_fallback',
        'configured_url': url,
        'resolved_target_url': final_url,
        'canonical_url': final_url,
        'extraction_method': 'crawl4ai',
        'quality_score': min(100, max(0, len(text) // 20)),
    }

    return {
        'success': success and bool(text) and not is_error_page,
        'url': final_url,
        'article': article,
        'articles': [article] if success and text and matched and not is_error_page else [],
        'articles_found': 1 if success and text and matched and not is_error_page else 0,
        'matched_keywords': matched,
        'content_length': len(text),
        'error': 'error_page_detected' if is_error_page else (getattr(result, 'error_message', '') or getattr(result, 'error', '')),
    }


async def crawl_with_crawl4ai_async(
    url: str,
    keywords: List[str],
    fallback_reason: str = '',
    wait_for_ms: int = 3000,
    page_timeout: int = 60000,
) -> Dict:
    from remote_pipeline_client import remote_pipeline_client

    remote = await asyncio.to_thread(
        remote_pipeline_client.run,
        url=url,
        mode='article',
        keywords=keywords,
        limit=1,
        enrich=True,
        tts=True,
    )
    articles = remote.get('articles') or []
    article = articles[0] if articles else {}
    matched = matched_keywords_in_text(
        f"{article.get('title', '')} {article.get('content', '')}", keywords
    )
    if article:
        article['matched_keywords'] = matched
        article['fallback_trigger_reason'] = fallback_reason
    return {
        'success': bool(article),
        'url': article.get('url') or url,
        'article': article,
        'articles': [article] if article and matched else [],
        'articles_found': 1 if article and matched else 0,
        'matched_keywords': matched,
        'content_length': len(str(article.get('content') or '')),
        'remote_job_id': remote.get('job_id') or '',
        'error': '' if article else 'remote pipeline returned no article',
    }


def crawl_with_crawl4ai(
    url: str,
    keywords: List[str],
    fallback_reason: str = '',
    wait_for_ms: int = 3000,
    page_timeout: int = 60000,
) -> Dict:
    return asyncio.run(
        crawl_with_crawl4ai_async(
            url=url,
            keywords=keywords,
            fallback_reason=fallback_reason,
            wait_for_ms=wait_for_ms,
            page_timeout=page_timeout,
        )
    )

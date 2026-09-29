#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Legacy crawler compatibility wrapper.

The project now uses the Playwright-first article crawler. This class remains
only so older imports do not break; it must not call external Firecrawl APIs.
"""

from typing import Dict, List

from utils import get_china_time


class WebCrawler:
    """Disabled legacy Firecrawl wrapper."""

    def __init__(self, api_url: str = "", api_key: str = None):
        self.api_url = api_url
        self.api_key = api_key

    def crawl(self, url: str, **options) -> Dict:
        return {
            "success": False,
            "disabled": True,
            "url": url,
            "error": "Legacy Firecrawl crawler is disabled. Use the Playwright article crawler.",
            "replacement": "/api/start-article-crawl",
            "timestamp": get_china_time().isoformat(),
        }

    def batch_crawl(self, urls: List[str], **options) -> List[Dict]:
        return [self.crawl(url, **options) for url in urls]

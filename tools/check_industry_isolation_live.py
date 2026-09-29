#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Browser/API acceptance for current industry projection and task isolation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from industry_pack_runtime import active_industry_composition_service
from user_database import UserDatabase


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:8003')
    parser.add_argument('--expected-pack-id', default='')
    args = parser.parse_args()
    active = active_industry_composition_service.snapshot()
    expected = str(args.expected_pack_id or active['active_industry_pack_id'])
    chromium = sorted(
        (ROOT / 'data' / 'ms-playwright').glob('chromium-*/chrome-linux*/chrome'),
        reverse=True,
    )[0]
    users = UserDatabase()
    users.connect()
    admin = users.connection.execute(
        "SELECT id FROM users WHERE role='admin' AND is_active=1 ORDER BY id LIMIT 1"
    ).fetchone()
    token = users.create_session(int(admin[0]), '127.0.0.1', 'industry-isolation-live', 1)
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(chromium), headless=True)
            context = browser.new_context(viewport={'width': 1440, 'height': 1000})
            context.add_cookies([{
                'name': 'session_token', 'value': token, 'url': args.base_url,
                'httpOnly': True, 'sameSite': 'Lax',
            }])
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'{args.base_url}/article-management', wait_until='networkidle', timeout=30000)
            page_heading = page.locator('main h1').first.text_content().strip()
            payload = page.evaluate(
                """async () => {
                    const get = async path => {
                        const response = await fetch(path);
                        return {status: response.status, body: await response.json()};
                    };
                    return {
                        stats: await get('/article-management/api/statistics'),
                        graph: await get('/article-management/api/keyword-map?limit=500'),
                        articles: await get('/article-management/api/articles?page=1&per_page=500'),
                        schedules: await get('/api/schedule-management/tasks?page=1&per_page=500'),
                        crawls: await get('/api/crawl-tasks/tasks?page=1&per_page=500'),
                        sources: await get('/api/intel/source-tasks?page=1&per_page=500'),
                        dashboard: await get('/api/intel/dashboard?time_range=730d&per_category=40'),
                        map: await get('/article-management/api/spacetime?limit=5000&min_confidence=0.1'),
                    };
                }"""
            )
            browser.close()
    finally:
        users.delete_session(token)

    schedules = payload['schedules']['body'].get('tasks') or []
    crawls = payload['crawls']['body'].get('tasks') or []
    sources = payload['sources']['body'].get('source_tasks') or []
    articles = payload['articles']['body'].get('articles') or []
    dashboard = payload['dashboard']['body']
    dashboard_articles = [
        article
        for section in (dashboard.get('sections') or {}).values()
        for article in (section.get('articles') or [])
        if article.get('industry_pack_id')
    ]
    map_points = payload['map']['body'].get('points') or []
    checks = {
        'ssr_heading_current': str(active['primary_pack'].get('name') or '') in page_heading,
        'all_http_success': all(item['status'] == 200 for item in payload.values()),
        'keyword_graph_current': payload['graph']['body'].get('industry_pack_id') == expected,
        'scheduled_tasks_current': bool(schedules) and all(item.get('industry_pack_id') == expected for item in schedules),
        'crawl_tasks_current': bool(crawls) and all(item.get('industry_pack_id') == expected for item in crawls),
        'dashboard_current': dashboard.get('industry_pack', {}).get('id') == expected,
        'dashboard_articles_current': all(item.get('industry_pack_id') == expected for item in dashboard_articles),
        'map_current': all(item.get('industry_pack_id') == expected for item in map_points),
        'no_page_errors': not errors,
    }
    report = {
        'expected_pack_id': expected,
        'page_heading': page_heading,
        'passed': all(checks.values()),
        'checks': checks,
        'counts': {
            'articles': len(articles),
            'keyword_nodes': len(payload['graph']['body'].get('keywords') or []),
            'scheduled_tasks': len(schedules),
            'active_scheduled_tasks': sum(bool(item.get('is_active')) for item in schedules),
            'crawl_tasks': len(crawls),
            'completed_crawl_tasks': sum(item.get('status') == 'completed' for item in crawls),
            'cancelled_crawl_tasks': sum(item.get('status') == 'cancelled' for item in crawls),
            'source_tasks': len(sources),
            'dashboard_articles': len(dashboard_articles),
            'map_points': len(map_points),
        },
        'page_errors': errors,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report['passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Authenticated loopback browser smoke for the live financial homepage.

The existing session token is read only in memory and is never printed.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.check_financial_stage3_browser import _find_chromium


def _active_session_token(database_path: Path) -> str:
    connection = sqlite3.connect(str(database_path))
    try:
        row = connection.execute(
            """
            SELECT session_token
            FROM user_sessions
            WHERE expires_at > datetime('now','localtime')
            ORDER BY expires_at DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        connection.close()
    if row is None or not str(row[0] or ""):
        raise RuntimeError("没有可用于只读浏览器验收的有效登录会话")
    return str(row[0])


def authenticated_health(*, base_url: str, database_path: str) -> dict:
    """Read the protected health endpoint without exposing the session token."""

    token = _active_session_token(Path(database_path).expanduser().resolve())
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/system/health",
        headers={"Cookie": f"session_token={token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
    running_request = urllib.request.Request(
        base_url.rstrip("/") + "/api/schedule-management/running-tasks",
        headers={"Cookie": f"session_token={token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(running_request, timeout=15) as running_response:
        running_payload = json.loads(
            running_response.read(1024 * 1024).decode("utf-8")
        )
    return {
        "http_status": int(response.status),
        "success": bool(payload.get("success")),
        "status": str(payload.get("status") or ""),
        "running_tasks": int(payload.get("running_tasks") or 0),
        "oldest_task_seconds": int(payload.get("oldest_task_seconds") or 0),
        "chrome_process_count": int(payload.get("chrome_process_count") or 0),
        "zombie_count": int(payload.get("zombie_count") or 0),
        "tasks": [
            {
                "schedule_id": item.get("schedule_id"),
                "task_name": str(item.get("task_name") or ""),
                "started_at": str(item.get("started_at") or ""),
                "stop_flag": bool(item.get("stop_flag")),
            }
            for item in running_payload.get("running_tasks") or []
        ],
        "session_token_exposed": False,
    }


def run(*, base_url: str, database_path: str, chromium_path: str = "") -> dict:
    token = _active_session_token(Path(database_path).expanduser().resolve())
    executable = _find_chromium(chromium_path)
    page_errors: list[str] = []
    popup_urls: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.add_cookies([{
            "name": "session_token",
            "value": token,
            "url": base_url.rstrip("/") + "/",
            "httpOnly": True,
            "sameSite": "Lax",
        }])
        page = context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("popup", lambda popup: popup_urls.append(popup.url))
        response = page.goto(
            base_url.rstrip("/") + "/?home_view=dashboard",
            wait_until="domcontentloaded",
        )
        page.wait_for_function(
            "document.querySelector('#financialFeedSection')?.hidden === false"
        )
        page.wait_for_function(
            "document.querySelectorAll('#financialFeedCards [data-financial-item-id^=\"overview:\"]').length === 5"
        )
        homepage = page.evaluate(
            """async () => {
                const response = await fetch('/api/intel/financial/feed?industry_pack_id=family_office&time_range=7d');
                const payload = await response.json();
                const dashboardResponse = await fetch('/api/intel/dashboard?industry_pack_id=family_office&time_range=90d&per_category=15', {cache:'no-store'});
                const dashboard = await dashboardResponse.json();
                const keywordsResponse = await fetch('/api/schedule-management/all-keywords', {cache:'no-store'});
                const keywordsPayload = await keywordsResponse.json();
                const detailResponse = await fetch('/api/intel/financial/feed/items/overview:sse/detail?industry_pack_id=family_office');
                const detailPayload = await detailResponse.json();
                const overviewCards = [...document.querySelectorAll('#financialFeedCards [data-financial-item-id^="overview:"]')];
                const snapshotCards = [...document.querySelectorAll('#financialFeedCards [data-financial-item-id^="snapshot:"]')];
                const sourceCards = [...document.querySelectorAll('#financialFeedCards [data-financial-item-id^="article:"]')];
                const todayCards = [...document.querySelectorAll('#intelTodayCards [data-article-id]')];
                const trendCards = [...document.querySelectorAll('#intelTrendCards [data-article-id]')];
                return {
                description: document.querySelector('#financialFeedDescription')?.textContent || '',
                today_ids: todayCards.map(card => Number(card.dataset.articleId || 0)),
                trend_ids: trendCards.map(card => Number(card.dataset.articleId || 0)),
                today_titles: todayCards.map(card => card.querySelector('[data-card-title]')?.textContent || ''),
                today_total: Number(dashboard.sections?.today?.total || 0),
                today_api: (dashboard.sections?.today?.articles || []).map(item => ({
                    article_id: Number(item.article_id || 0), title: item.title || '',
                    effective_time: item.effective_time || '', classified_at: item.classified_at || '',
                    final_category: item.final_category || '',
                })),
                dashboard_cache_control: dashboardResponse.headers.get('cache-control') || '',
                overview_ids: overviewCards.map(card => card.dataset.financialItemId),
                overview_titles: overviewCards.map(card => card.querySelector('[data-card-title]')?.textContent || ''),
                overview_tops: overviewCards.map(card => Math.round(card.getBoundingClientRect().top)),
                overview_api_count: (payload.market_overview || []).length,
                overview_api: (payload.market_overview || []).map(item => ({
                    item_id: item.item_id, title: item.title, symbol: item.scope?.symbol || '',
                    market: item.scope?.market || '',
                    values: item.values || {}, observed_at: item.observed_at || '',
                    provider: item.source?.provider_name || item.source?.provider_key || '',
                    status: item.availability?.status || '',
                    movement: item.movement || {},
                })),
                snapshot_ids: snapshotCards.map(card => card.dataset.financialItemId),
                snapshot_api: (payload.items || []).filter(item => item.content_kind === 'market_fact').map(item => ({
                    item_id: item.item_id, snapshot_id: item.snapshot_id, title: item.title,
                    symbol: item.scope?.symbol || '', values: item.values || {},
                    market: item.scope?.market || '',
                    observed_at: item.observed_at || '',
                    provider: item.source?.provider_name || item.source?.provider_key || '',
                    asset_type: item.scope?.asset_type || '',
                    instrument_id: Number(item.scope?.instrument_id || 0),
                    movement: item.movement || {},
                })),
                source_api: (payload.items || []).filter(item => item.content_kind === 'source_document').map(item => ({
                    item_id: item.item_id, article_id: Number(item.article_id || 0),
                    matched_keywords: item.matched_keywords || [],
                    keyword_gate_source: item.keyword_gate_source || '',
                })),
                source_dom: sourceCards.map(card => ({
                    item_id: card.dataset.financialItemId || '',
                    mark_count: card.querySelectorAll('mark').length,
                    keyword_badges: [...card.querySelectorAll('.keyword-badge-title')]
                        .map(node => node.textContent || ''),
                })),
                movement_dom: [...overviewCards, ...snapshotCards].map(card => ({
                    item_id: card.dataset.financialItemId || '',
                    text: card.querySelector('.financial-market-movement')?.textContent || '',
                    class_name: card.querySelector('.financial-market-movement')?.className || '',
                })),
                index_display_dom: overviewCards.map(card => {
                    const direction = card.querySelector('.financial-market-direction');
                    const style = direction ? getComputedStyle(direction) : null;
                    return {
                        item_id: card.dataset.financialItemId || '',
                        value_texts: [...card.querySelectorAll('.financial-feed-card-values .keyword-badge-unknown')]
                            .map(node => node.textContent || ''),
                        direction: {
                            text: direction?.textContent || '',
                            class_name: direction?.className || '',
                            width: style?.width || '',
                            height: style?.height || '',
                            border_style: style?.borderTopStyle || '',
                        },
                    };
                }),
                stock_delete_dom: snapshotCards.map(card => ({
                    item_id: card.dataset.financialItemId || '',
                    count: card.querySelectorAll(
                        '.article-card-action.delete[aria-label="删除个股卡片"]'
                    ).length,
                })),
                project_keyword_gate: payload.project_keyword_gate || {},
                batch_schedule_keywords: keywordsPayload.keywords || [],
                relatedness_policy: detailPayload.news_status?.relatedness_policy || {},
                snapshot_count: snapshotCards.length,
                source_fallback_present: document.documentElement.innerHTML.includes('查看信源'),
                };
            }"""
        )
        category_toggles = page.evaluate(
            """() => [...document.querySelectorAll('#intelDashboard > [data-dashboard-category]')]
                .filter(section => !section.hidden && getComputedStyle(section).display !== 'none')
                .map(section => {
                    const toggle = section.querySelector('.dashboard-category-toggle');
                    const title = section.querySelector('.intel-section-title');
                    const row = section.querySelector('.dashboard-category-title-row');
                    const toggleRect = toggle?.getBoundingClientRect();
                    const titleRect = title?.getBoundingClientRect();
                    return {
                        category: section.dataset.dashboardCategory || '',
                        title: title?.textContent || '',
                        has_toggle: Boolean(toggle),
                        same_row: Boolean(toggle && row && toggle.parentElement === row && title?.parentElement === row),
                        immediately_before: Boolean(toggle && toggle.nextElementSibling === title),
                        left_of_title: Boolean(toggleRect && titleRect && toggleRect.right <= titleRect.left),
                        expanded: toggle?.getAttribute('aria-expanded') || '',
                    };
                })"""
        )
        today_toggle = page.locator(
            '[data-dashboard-category="today"] .dashboard-category-toggle'
        )
        today_toggle.click()
        page.wait_for_function(
            "document.querySelector('[data-dashboard-category=today] .intel-section-body')?.hidden"
        )
        category_collapse = page.evaluate(
            """() => { const section=document.querySelector('[data-dashboard-category=today]');
                const toggle=section.querySelector('.dashboard-category-toggle');
                const body=section.querySelector('.intel-section-body');
                return {expanded:toggle.getAttribute('aria-expanded'), hidden:body.hidden,
                    inert:body.inert, title_visible:!!section.querySelector('.intel-section-title')?.offsetParent}; }"""
        )
        today_toggle.click()
        page.wait_for_function(
            "!document.querySelector('[data-dashboard-category=today] .intel-section-body')?.hidden"
        )

        market_card = page.locator(
            '#financialFeedCards [data-financial-item-id="overview:sse"]'
        )
        market_item_id = market_card.get_attribute("data-financial-item-id") or ""
        market_card.locator(".article-card-action.view").click()
        page.wait_for_function(
            "document.querySelector('#financialMarketDetailModal')?.classList.contains('active')"
        )
        page.wait_for_function(
            "document.querySelector('#financialHistoryChartStatus')?.textContent !== '加载中…'"
        )
        market_detail = page.evaluate(
            """() => ({
                open: document.querySelector('#financialMarketDetailModal')?.classList.contains('active'),
                chart_svg_count: document.querySelectorAll('#financialHistoryChart svg').length,
                chart_status: document.querySelector('#financialHistoryChartStatus')?.textContent || '',
                related_news_count: document.querySelectorAll('#financialRelatedNewsList .financial-related-news-item').length,
                external_link_count: document.querySelectorAll('#financialMarketDetailModal a[target="_blank"]').length,
                current_url: location.href,
                grid_class: document.querySelector('#financialMarketDetailGrid')?.className || '',
                history_width: Math.round(document.querySelector('.financial-market-detail-panel.is-history')?.getBoundingClientRect().width || 0),
                news_width: Math.round(document.querySelector('.financial-market-detail-panel.is-news')?.getBoundingClientRect().width || 0),
                related_list: (() => { const list=document.querySelector('#financialRelatedNewsList');
                    return {client_height:list?.clientHeight || 0,scroll_height:list?.scrollHeight || 0}; })(),
                content_size: (() => { const rect=document.querySelector('#financialMarketDetailModal > .article-modal-content').getBoundingClientRect();
                    return {width:Math.round(rect.width),height:Math.round(rect.height)}; })(),
            })"""
        )
        related_return = {"available": False}
        related_view = page.locator(
            '#financialRelatedNewsList .financial-related-news-item .article-card-action.view'
        ).first
        if related_view.count():
            related_return["available"] = True
            related_view.click()
            page.wait_for_function(
                "document.querySelector('#articleModal')?.classList.contains('active')"
            )
            page.evaluate("closeArticleModal()")
            page.wait_for_function(
                "document.querySelector('#financialMarketDetailModal')?.classList.contains('active')"
            )
            page.wait_for_function(
                "document.activeElement?.classList.contains('financial-related-news-item')"
            )
            related_return.update(page.evaluate(
                """() => ({
                    article_closed: !document.querySelector('#articleModal')?.classList.contains('active'),
                    market_reopened: document.querySelector('#financialMarketDetailModal')?.classList.contains('active'),
                    focus_returned: document.activeElement?.classList.contains('financial-related-news-item') || false,
                    chart_preserved: document.querySelectorAll('#financialHistoryChart svg').length === 1,
                })"""
            ))
        page.evaluate("closeFinancialMarketDetail()")

        article_card = page.locator('#financialFeedCards [data-financial-item-id^="article:"]').first
        article_item_id = ""
        article_detail = {"available": False}
        if article_card.count():
            article_item_id = article_card.get_attribute("data-financial-item-id") or ""
            article_card.locator(".article-card-action.view").click()
            page.wait_for_function(
                "document.querySelector('#articleModal')?.classList.contains('active')"
                " && document.querySelector('#modalTitle')?.textContent !== '加载中...'"
            )
            article_detail = page.evaluate(
                """() => ({
                    available: true,
                    open: document.querySelector('#articleModal')?.classList.contains('active'),
                    modal_class: document.querySelector('#articleModal')?.className || '',
                    content_class: document.querySelector('#articleModal > .article-modal-content')?.className || '',
                    title: document.querySelector('#modalTitle')?.textContent || '',
                    translation_button: Boolean(document.querySelector('#modalTranslateBtn')),
                    speech_button: Boolean(document.querySelector('#articleModal .article-speech-action')),
                    content_size: (() => { const rect=document.querySelector('#articleModal > .article-modal-content').getBoundingClientRect();
                        return {width:Math.round(rect.width),height:Math.round(rect.height)}; })(),
                })"""
            )
        browser.close()

    expected_description = "行情事实、金融原文与研究观点，不混入普通行业资讯统计"
    expected_overview_ids = [
        "overview:sse", "overview:szse", "overview:hsi",
        "overview:nasdaq", "overview:nikkei",
    ]
    expected_overview_titles = [
        "上证指数（000001.SH）", "深证成指（399001.SZ）",
        "恒生指数（HSI.HK）", "纳斯达克综合指数（IXIC.US）",
        "日经225（N225.JP）",
    ]
    required_symbols = ["000001.SH", "399001.SZ", "HSI.HK", "IXIC.US", "N225.JP"]
    snapshot_api = homepage["snapshot_api"]
    snapshot_symbols = [item["symbol"] for item in snapshot_api if item["symbol"]]
    source_api = homepage["source_api"]
    source_dom = {item["item_id"]: item for item in homepage["source_dom"]}
    primary_article_ids = set(homepage["today_ids"]) | set(homepage["trend_ids"])
    movement_api = homepage["overview_api"] + snapshot_api
    movement_dom = {item["item_id"]: item for item in homepage["movement_dom"]}
    index_display_dom = {
        item["item_id"]: item for item in homepage["index_display_dom"]
    }
    index_price_prefixes = ("价格：", "收盘：", "close：", "数值：", "昨收：", "开：", "高：", "低：")
    two_decimal_value = re.compile(r"^-?[0-9][0-9,]*\.[0-9]{2}$")

    def index_display_is_valid(item: dict) -> bool:
        dom = index_display_dom.get(item["item_id"], {})
        price_values = [
            text.split("：", 1)[1]
            for text in dom.get("value_texts", [])
            if text.startswith(index_price_prefixes) and "：" in text
        ]
        direction = dom.get("direction", {})
        movement = item.get("movement", {})
        expected_direction_class = {
            "up": "is-up", "down": "is-down", "flat": "is-flat",
        }.get(movement.get("direction"))
        direction_ok = (
            movement.get("status") != "ready"
            or (
                direction.get("text") in {"↑", "↓", "→"}
                and expected_direction_class in direction.get("class_name", "").split()
                and direction.get("width") == "18px"
                and direction.get("height") == "24px"
                and direction.get("border_style") == "none"
            )
        )
        return bool(price_values) and all(
            two_decimal_value.fullmatch(value) for value in price_values
        ) and direction_ok

    stock_delete_dom = {
        item["item_id"]: item["count"] for item in homepage["stock_delete_dom"]
    }
    today_api = homepage["today_api"]
    today_times = [item["effective_time"] for item in today_api]
    expected_categories = ["today", "trend", "policy", "recent", "other", "financial-feed"]
    passed = bool(
        response is not None
        and response.status == 200
        and homepage["description"] == expected_description
        and homepage["today_ids"] == [item["article_id"] for item in today_api]
        and homepage["today_titles"] == [item["title"] for item in today_api]
        and today_times == sorted(today_times, reverse=True)
        and all(item["final_category"] == "event" for item in today_api)
        and homepage["overview_ids"] == expected_overview_ids
        and homepage["overview_titles"] == expected_overview_titles
        and len(set(homepage["overview_tops"])) == 1
        and homepage["overview_api_count"] == 5
        and [item["item_id"] for item in homepage["overview_api"]] == expected_overview_ids
        and [item["title"] for item in homepage["overview_api"]] == expected_overview_titles
        and [item["symbol"] for item in homepage["overview_api"]] == required_symbols
        and all(index_display_is_valid(item) for item in homepage["overview_api"])
        and len(homepage["snapshot_ids"]) == len(set(homepage["snapshot_ids"]))
        and [item["item_id"] for item in snapshot_api] == homepage["snapshot_ids"]
        and not set(required_symbols).intersection(snapshot_symbols)
        and len(snapshot_symbols) == len(set(snapshot_symbols))
        and all(
            any(key in item["values"] for key in ("last_price", "close", "value"))
            for item in homepage["overview_api"]
        )
        and all(
            item["observed_at"] and item["provider"] and item["status"] == "ready"
            for item in homepage["overview_api"]
        )
        and all(not item["title"].casefold().endswith((" quote", " bar")) for item in snapshot_api)
        and homepage["project_keyword_gate"].get("source") == "batch_schedule"
        and homepage["project_keyword_gate"].get("enabled") is True
        and bool(homepage["project_keyword_gate"].get("keywords"))
        and homepage["project_keyword_gate"].get("keywords")
        == homepage["batch_schedule_keywords"]
        and all(
            item["matched_keywords"] and item["keyword_gate_source"] == "batch_schedule"
            for item in source_api
        )
        and not primary_article_ids.intersection(
            item["article_id"] for item in source_api
        )
        and all(
            item["item_id"] in source_dom
            and source_dom[item["item_id"]]["keyword_badges"] == item["matched_keywords"]
            for item in source_api
        )
        and all(
            item["movement"].get("status") in {"ready", "unavailable"}
            and item["item_id"] in movement_dom
            and f"is-{item['movement'].get('color', 'neutral')}"
            in movement_dom[item["item_id"]]["class_name"]
            and (
                item["movement"].get("symbol", "")
                in movement_dom[item["item_id"]]["text"]
                if item["movement"].get("status") == "ready"
                else "暂无可比涨跌" in movement_dom[item["item_id"]]["text"]
            )
            for item in movement_api
        )
        and all(
            stock_delete_dom.get(item["item_id"]) == 1
            for item in snapshot_api
            if item["instrument_id"]
            and item["asset_type"].casefold() in {"equity", "stock"}
        )
        and homepage["relatedness_policy"].get("project_keyword_gate_applied") is False
        and homepage["relatedness_policy"].get("version") == "financial-related-news-policy-v1"
        and [item["category"] for item in category_toggles] == expected_categories
        and all(
            item["has_toggle"] and item["same_row"]
            and item["immediately_before"] and item["left_of_title"]
            for item in category_toggles
        )
        and category_collapse == {
            "expanded": "false", "hidden": True,
            "inert": True, "title_visible": True,
        }
        and not homepage["source_fallback_present"]
        and market_item_id == "overview:sse"
        and market_detail["open"]
        and market_detail["chart_svg_count"] == 1
        and market_detail["external_link_count"] == 0
        and market_detail["current_url"].startswith(base_url.rstrip("/") + "/")
        and market_detail["content_size"]["height"] >= 800
        and market_detail["history_width"] > 0
        and market_detail["news_width"] > 0
        and market_detail["history_width"] <= market_detail["news_width"] * 1.15
        and (
            not related_return["available"]
            or all(related_return.get(key) for key in (
                "article_closed", "market_reopened", "focus_returned", "chart_preserved",
            ))
        )
        and (
            not source_api
            or (
                article_item_id.startswith("article:")
                and article_detail["available"]
                and article_detail["open"]
                and article_detail["modal_class"] == "article-modal active"
                and article_detail["content_class"] == "article-modal-content"
                and article_detail["translation_button"]
                and article_detail["speech_button"]
                and market_detail["content_size"] == article_detail["content_size"]
            )
        )
        and not popup_urls
        and not page_errors
    )
    return {
        "check_version": "financial-home-live-v1",
        "passed": passed,
        "base_url": base_url,
        "browser": str(executable),
        "homepage": homepage,
        "category_toggles": category_toggles,
        "category_collapse": category_collapse,
        "market_item_id": market_item_id,
        "market_detail": market_detail,
        "related_return": related_return,
        "article_item_id": article_item_id,
        "article_detail": article_detail,
        "popup_urls": popup_urls,
        "page_errors": page_errors,
        "session_token_exposed": False,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8003")
    parser.add_argument("--database", default=str(ROOT / "data" / "crawler_articles.db"))
    parser.add_argument("--chromium", default="")
    parser.add_argument("--health-only", action="store_true")
    args = parser.parse_args(argv)
    if args.health_only:
        health = authenticated_health(base_url=args.base_url, database_path=args.database)
        print(json.dumps(health, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if health["success"] and health["status"] == "ok" else 2
    report = run(
        base_url=args.base_url,
        database_path=args.database,
        chromium_path=args.chromium,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

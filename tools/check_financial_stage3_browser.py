#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Loopback-only Chromium E2E for the stage-3 financial product surface."""

from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from playwright.sync_api import sync_playwright
from werkzeug.serving import WSGIRequestHandler, make_server


ROOT = Path(__file__).resolve().parents[1]


def _find_chromium(explicit_path: str = "") -> Path:
    if explicit_path:
        candidate = Path(explicit_path).expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise FileNotFoundError(f"Chromium 不存在：{candidate}")
    candidates = sorted(
        (ROOT / "data" / "ms-playwright").glob("chromium-*/chrome-linux*/chrome"),
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError("未找到项目内 Playwright Chromium")
    return candidates[0].resolve()


class _QuietHandler(WSGIRequestHandler):
    def log_request(self, *args, **kwargs):
        return None


def _fixture_app(calls: list[dict]) -> Flask:
    app = Flask(
        "financial-stage3-browser",
        template_folder=str(ROOT / "templates"),
        static_folder=str(ROOT / "static"),
    )
    hidden_instruments: set[int] = set()

    @app.get("/")
    def home():
        return render_template("mapindex.html", financial_workspace=False)

    @app.get("/financial")
    def financial():
        return render_template("mapindex.html", financial_workspace=True)

    @app.get("/api/intel/industry-packs")
    def packs():
        return jsonify({
            "success": True,
            "default_industry_pack_id": "family_office",
            "industry_packs": [{"id": "family_office", "name": "家族办公室"}],
        })

    @app.get("/api/intel/dashboard")
    def dashboard():
        return jsonify({
            "success": True,
            "industry_pack": {"id": "family_office", "name": "家族办公室"},
            "sections": {
                key: {
                    "articles": ([{
                        "article_id": 101,
                        "title": "行业动态标准卡片",
                        "trend_summary": "用于与金融行业信息卡片逐项比较尺寸和共享视觉。",
                        "domain": "fixture.example",
                        "source_display_name": "Browser Fixture",
                        "publish_date": "2026-08-05",
                        "content_length": 88,
                        "matched_keywords": ["家族办公室"],
                    }] if key == "today" else []),
                    "total": 1 if key == "today" else 0,
                }
                for key in ("today", "trend", "policy", "recent", "other")
            },
            "total": 1,
            "statistics": {
                "total_articles": 0,
                "industry_valid_articles": 0,
                "window_articles": 0,
                "window_days": 7,
            },
        })

    @app.get("/api/intel/financial/capabilities")
    def capabilities():
        calls.append({"endpoint": "capabilities"})
        return jsonify({
            "success": True,
            "schema_version": "financial-product-capabilities-v1",
            "industry_pack_id": "family_office",
            "effective_pack_ids": ["family_office", "financial_markets"],
            "effective_capabilities": {
                "financial_zone": True,
                "tradingagents_reports": True,
                "simulation": True,
                "backtesting": True,
            },
        })

    @app.get("/api/intel/financial/feed")
    def feed():
        calls.append({
            "endpoint": "feed",
            "content_kind": str(request.args.get("content_kind") or ""),
        })
        payload = {
            "success": True,
            "visible": True,
            "effective_pack_ids": ["family_office", "financial_markets"],
            "effective_capabilities": {
                "financial_zone": True,
                "tradingagents_reports": True,
                "simulation": True,
                "backtesting": True,
            },
            "counts": {"market_fact": 1, "source_document": 1, "research_opinion": 1},
            "total": 3,
            "page": 1,
            "per_page": 15,
            "total_pages": 1,
            "categories": [
                {"key": "market_index", "name": "市场与指数"},
                {"key": "tradingagents_report", "name": "TradingAgents 终极报告"},
                {"key": "paper_backtest", "name": "模拟组合与回测"},
            ],
            "market_overview": [{
                "content_kind": "market_fact", "kind_label": "大盘",
                "item_id": "overview:sse", "overview_key": "sse", "fixed": True,
                "snapshot_id": 100, "title": "上证指数（000001.SH）", "subcategory": "market_index",
                "summary": "固定大盘卡仅展示最新合格行情。",
                "observed_at": "2026-08-05T07:00:00Z", "market_status": "closed",
                "currency": "CNY", "quality_status": "verified", "is_stale": False,
                "values": {"last_price": 3617.6, "change_percent": 0.22},
                "movement": {
                    "status": "ready", "direction": "up", "symbol": "↑",
                    "change_percent": 0.22, "basis": "reported_change_percent",
                    "color": "red", "market_convention": "red_up_green_down",
                },
                "scope": {"display_name": "上证指数", "symbol": "000001.SH", "asset_type": "index", "market": "CN"},
                "source": {"provider_name": "结构化指数源", "url": "https://official.example/sse"},
                "availability": {"status": "ready", "reason": "latest_qualified_snapshot"},
            }, {
                "content_kind": "market_fact", "kind_label": "大盘",
                "item_id": "overview:szse", "overview_key": "szse", "fixed": True,
                "title": "深证成指（399001.SZ）", "subcategory": "market_index", "summary": "固定大盘卡等待合格行情。",
                "observed_at": "", "market_status": "unknown", "currency": "", "quality_status": "unavailable",
                "values": {}, "movement": {"status": "unavailable", "color": "neutral"},
                "scope": {"display_name": "深证成指", "symbol": "399001.SZ", "asset_type": "index", "market": "CN"},
                "source": {"provider_name": "结构化指数源", "url": ""},
            }, {
                "content_kind": "market_fact", "kind_label": "大盘",
                "item_id": "overview:hsi", "overview_key": "hsi", "fixed": True,
                "title": "恒生指数（HSI.HK）", "subcategory": "market_index", "summary": "固定大盘卡等待合格行情。",
                "observed_at": "", "market_status": "unknown", "currency": "", "quality_status": "unavailable",
                "values": {}, "movement": {"status": "unavailable", "color": "neutral"},
                "scope": {"display_name": "恒生指数", "symbol": "HSI.HK", "asset_type": "index", "market": "HK"},
                "source": {"provider_name": "结构化指数源", "url": ""},
            }, {
                "content_kind": "market_fact", "kind_label": "大盘",
                "item_id": "overview:nasdaq", "overview_key": "nasdaq", "fixed": True,
                "title": "纳斯达克综合指数（IXIC.US）", "subcategory": "market_index", "summary": "固定大盘卡等待合格行情。",
                "observed_at": "", "market_status": "unknown", "currency": "", "quality_status": "unavailable",
                "values": {}, "movement": {"status": "unavailable", "color": "neutral"},
                "scope": {"display_name": "纳斯达克综合指数", "symbol": "IXIC.US", "asset_type": "index", "market": "US"},
                "source": {"provider_name": "结构化指数源", "url": ""},
            }, {
                "content_kind": "market_fact", "kind_label": "大盘",
                "item_id": "overview:nikkei", "overview_key": "nikkei", "fixed": True,
                "title": "日经225（N225.JP）", "subcategory": "market_index", "summary": "固定大盘卡等待合格行情。",
                "observed_at": "", "market_status": "unknown", "currency": "", "quality_status": "unavailable",
                "values": {}, "movement": {"status": "unavailable", "color": "neutral"},
                "scope": {"display_name": "日经225", "symbol": "N225.JP", "asset_type": "index", "market": "JP"},
                "source": {"provider_name": "结构化指数源", "url": ""},
            }],
            "items": [{
                "content_kind": "market_fact",
                "kind_label": "事实",
                "item_id": "snapshot:101",
                "snapshot_id": 101,
                "title": "腾讯控股（0700.HK）",
                "summary": "结构化行情快照；请结合质量、时点和市场状态判断实时性。",
                "subcategory": "stock_issuer",
                "observed_at": "2026-08-05T07:00:00Z",
                "market_status": "closed",
                "currency": "HKD",
                "quality_status": "verified",
                "values": {"last_price": 543.0, "change_percent": -0.31},
                "movement": {
                    "status": "ready", "direction": "down", "symbol": "↓",
                    "change_percent": -0.31, "basis": "reported_change_percent",
                    "color": "green", "market_convention": "red_up_green_down",
                },
                "scope": {"instrument_id": 77, "display_name": "腾讯控股", "symbol": "0700.HK", "asset_type": "equity", "market": "HK"},
                "source": {"provider_name": "结构化行情源", "url": "https://official.example/0700"},
            }, {
                "content_kind": "source_document",
                "kind_label": "原文",
                "item_id": "article:202",
                "article_id": 202,
                "title": "家族办公室关注上证指数相关新闻",
                "summary": "这是一条匹配家族办公室关键词并用于验证统一文章详情弹窗的金融新闻摘要。",
                "matched_keywords": ["家族办公室"],
                "keyword_gate_source": "batch_schedule",
                "subcategory": "financial_news",
                "observed_at": "2026-08-05T02:00:00Z",
                "source": {"provider_name": "official.example", "url": "https://official.example/sse-news"},
            }, {
                "content_kind": "research_opinion",
                "kind_label": "研究观点",
                "item_id": "report:7:v1",
                "report_id": 7,
                "report_url": "/api/financial/reports/7",
                "title": "腾讯 TradingAgents 终极报告",
                "summary": "基本面稳定，但仍需审视反证。",
                "subcategory": "tradingagents_report",
                "recommendation": "Hold",
                "confidence": 0.72,
                "observed_at": "2026-07-31T03:00:00Z",
                "overview": {
                    "as_of": "2026-07-31T03:00:00Z",
                    "evidence_coverage": 0.75,
                    "data_gaps": ["sentiment"],
                    "counter_evidence": "估值与波动风险",
                    "risk_excerpt": "中等风险",
                },
                "scope": {"display_name": "腾讯", "symbol": "0700.HK"},
                "source": {"provider_name": "TradingAgents"},
            }],
            "project_keyword_gate": {
                "source": "batch_schedule", "enabled": True,
                "keywords": ["家族办公室"],
                "classification_precedence": ["event", "trend", "financial_fallback"],
            },
        }
        if 77 in hidden_instruments:
            payload["items"] = [
                item for item in payload["items"]
                if int((item.get("scope") or {}).get("instrument_id") or 0) != 77
            ]
            payload["counts"]["market_fact"] = 0
            payload["total"] = 2
        return jsonify(payload)

    @app.delete("/api/intel/financial/feed/instruments/<int:instrument_id>")
    def hide_instrument(instrument_id: int):
        calls.append({"endpoint": "hide_instrument", "instrument_id": instrument_id})
        hidden_instruments.add(instrument_id)
        return jsonify({"success": True, "instrument_id": instrument_id, "hidden": True})

    @app.get("/api/intel/financial/feed/items/<path:item_id>/detail")
    def market_detail(item_id: str):
        calls.append({"endpoint": "market_detail", "item_id": item_id})
        empty_history = item_id == "overview:nasdaq"
        history = ({
            "status": "unavailable", "reason": "no_qualified_history",
            "value_field": "", "currency": "USD",
            "provider": {}, "point_count": 0, "from": "", "to": "", "points": [],
        } if empty_history else {
            "status": "ready", "reason": "qualified_history",
            "value_field": "last_price", "currency": "CNY",
            "provider": {"provider_key": "fixture", "provider_name": "结构化指数源"},
            "point_count": 3,
            "from": "2026-08-01T07:00:00Z", "to": "2026-08-05T07:00:00Z",
            "points": [
                {"snapshot_id": 1, "observed_at": "2026-08-01T07:00:00Z", "fetched_at": "2026-08-01T07:00:02Z", "market_status": "closed", "value": 3580.1},
                {"snapshot_id": 2, "observed_at": "2026-08-04T07:00:00Z", "fetched_at": "2026-08-04T07:00:02Z", "market_status": "closed", "value": 3609.2},
                {"snapshot_id": 3, "observed_at": "2026-08-05T07:00:00Z", "fetched_at": "2026-08-05T07:00:02Z", "market_status": "closed", "value": 3617.6},
            ],
        })
        related_news = [{
            "article_id": 202 + number, "detail_mode": "article_modal",
            "title": f"上证指数相关新闻 {number + 1}",
            "summary": "这是一条用于验证统一文章详情弹窗和固定高度新闻卡片的金融新闻摘要。",
            "published_at": "2026-08-05", "observed_at": "2026-08-05T00:00:00Z",
            "recency_status": "recent", "age_days": 0,
            "matched_terms": ["上证指数", "000001.SH"],
            "source": {"provider_name": "official.example", "url": "https://official.example/sse-news"},
        } for number in range(6)]
        return jsonify({
            "success": True,
            "item_id": item_id,
            "content_kind": "market_fact",
            "title": "纳斯达克综合指数" if empty_history else "上证指数",
            "scope": {
                "scope_type": "instrument", "instrument_id": 1,
                "symbol": "IXIC.US" if empty_history else "000001.SH",
                "display_name": "纳斯达克综合指数" if empty_history else "上证指数",
                "asset_type": "index", "market": "US" if empty_history else "CN",
            },
            "current": {
                "snapshot_id": 3, "observed_at": "2026-08-05T07:00:00Z",
                "market_status": "closed", "quality_status": "verified",
                "values": {"last_price": 3617.6, "change_percent": 0.22},
            },
            "history": history,
            "related_news": related_news,
            "news_status": {
                "status": "ready", "reason_codes": ["matched_market_news"],
                "relatedness_policy": {
                    "version": "financial-related-news-policy-v1",
                    "project_keyword_gate_applied": False,
                    "target_type": "fixed_market_index",
                },
            },
        })

    @app.get("/article-management/api/article/<int:article_id>")
    def article_detail(article_id: int):
        calls.append({"endpoint": "article_detail", "article_id": article_id})
        return jsonify({
            "success": True,
            "article": {
                "id": article_id,
                "title": "上证指数相关新闻",
                "url": "https://official.example/sse-news",
                "publish_date": "2026-08-05",
                "source_url_name": "官方测试信源",
                "domain": "official.example",
                "last_crawled": "2026-08-05T02:01:00Z",
                "matched_keywords": ["上证指数"],
                "content": "这是文章详情正文；金融新闻卡与行情详情中的相关新闻都必须复用这个窗口。",
            },
        })

    @app.post("/api/intel/financial/feed/translate")
    def translate_financial_card():
        payload = request.get_json(silent=True) or {}
        calls.append({
            "endpoint": "financial_card_translation",
            "item_id": str(payload.get("item_id") or ""),
            "keys": sorted(payload),
        })
        return jsonify({
            "success": True,
            "item_id": str(payload.get("item_id") or ""),
            "target_language": "英文",
            "translation": "Tencent TradingAgents Final Report\n\nFundamentals are stable, but counter-evidence still requires review.",
        })

    @app.get("/api/financial/reports/<int:report_id>")
    def report(report_id: int):
        calls.append({"endpoint": "report", "report_id": report_id})
        groups = [
            ("analysis", "分析师报告", "market_analyst"),
            ("bull_bear", "多空研究动态讨论", "bear_researcher"),
            ("decision", "Trader 决策", "trader"),
            ("risk", "风险评估", "neutral_risk_analyst"),
            ("final", "Portfolio Manager 终极结论", "portfolio_manager"),
        ]
        return jsonify({
            "success": True,
            "report": {
                "report_id": report_id,
                "report_version": 1,
                "title": "腾讯 TradingAgents 终极报告",
                "recommendation": "Hold",
                "confidence": 0.72,
                "executive_summary": "终极结论",
                "overview": {
                    "as_of": "2026-07-31T03:00:00Z",
                    "core_reason": "核心理由",
                    "counter_evidence": "主要反证",
                    "evidence_coverage": 0.75,
                    "data_gaps": ["sentiment"],
                },
                "target": {"display_name": "腾讯", "canonical_symbol": "0700.HK"},
                "risk_summary": {"summary": "中等风险"},
                "section_groups": [
                    {"key": key, "name": name} for key, name, _role in groups
                ],
                "sections": [
                    {
                        "group": key,
                        "role_key": role,
                        "role_label": name,
                        "section_type": "fixture",
                        "status": "completed",
                        "content_markdown": f"{name}的公开产物",
                        "citations": [],
                    }
                    for key, name, role in groups
                ],
                "suitability_notice": "模拟研究参考",
                "disclaimer": "不构成投资建议",
            },
        })

    @app.get("/api/chat/config")
    def chat_config():
        return jsonify({"success": True, "config": {"models": {}}})

    @app.get("/api/chat/history/sessions")
    def sessions():
        return jsonify({"success": True, "sessions": []})

    @app.get("/mapindex/api/points")
    def points():
        return jsonify({"success": True, "points": []})

    @app.errorhandler(404)
    def missing_fixture_route(_error):
        # The real template starts auxiliary widgets in parallel.  Keep their
        # fixture failures as JSON so a missing optional route cannot create a
        # misleading HTML-as-JSON browser exception.
        return jsonify({"success": False, "error": "fixture_route_not_implemented"}), 404

    return app


def run_browser_e2e(chromium_path: str = "") -> dict:
    browser_executable = _find_chromium(chromium_path)
    calls: list[dict] = []
    app = _fixture_app(calls)
    server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=_QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    page_errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(browser_executable),
                headless=True,
            )
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            popup_urls = []
            page.on("popup", lambda popup: popup_urls.append(popup.url))
            page.goto(
                f"http://127.0.0.1:{server.server_port}/?financial_module=simulation&home_view=dashboard",
                wait_until="domcontentloaded",
            )
            page.wait_for_selector("#intelTodayCards .article-card")
            page.wait_for_selector("#financialFeedCards .article-card")
            homepage = page.evaluate(
                """() => ({
                    workspace: document.body.dataset.financialWorkspace,
                    financial_nav_count: document.querySelectorAll(
                        '.dashboard-global-nav [data-financial-menu], .dashboard-global-nav a[href*="financial_module"]'
                    ).length,
                    financial_module: state.financialModule,
                    simulation_hidden: document.querySelector('#financialSimulationSection')?.hidden,
                })"""
            )
            homepage_cards = page.evaluate(
                """() => {
                    const describe = selector => {
                        const card = document.querySelector(selector);
                        const style = getComputedStyle(card);
                        const rect = card.getBoundingClientRect();
                        return {
                            width: Math.round(rect.width), height: Math.round(rect.height),
                            display: style.display, padding: style.padding,
                            gap: style.gap, borderRadius: style.borderRadius,
                            backgroundColor: style.backgroundColor,
                        };
                    };
                    return {
                        ordinary: describe('#intelTodayCards .article-card'),
                        financial: describe('#financialFeedCards .article-card'),
                        financial_class: document.querySelector('#financialFeedCards .article-card')?.className || '',
                    };
                }"""
            )
            homepage_description = page.locator("#financialFeedDescription").text_content()
            financial_card_contract = page.evaluate(
                """() => {
                    const market = document.querySelector(
                        '#financialFeedCards [data-financial-item-id="overview:sse"] .financial-market-movement'
                    );
                    const stock = document.querySelector(
                        '#financialFeedCards [data-financial-item-id="snapshot:101"]'
                    );
                    const stockMovement = stock?.querySelector('.financial-market-movement');
                    const marketDirection = market?.querySelector('.financial-market-direction');
                    const article = document.querySelector(
                        '#financialFeedCards [data-financial-item-id="article:202"]'
                    );
                    return {
                        market_movement: {text: market?.textContent || '', class_name: market?.className || ''},
                        market_direction: {
                            text: marketDirection?.textContent || '',
                            class_name: marketDirection?.className || '',
                            width: Math.round(marketDirection?.getBoundingClientRect().width || 0),
                            height: Math.round(marketDirection?.getBoundingClientRect().height || 0),
                            border_style: marketDirection ? getComputedStyle(marketDirection).borderStyle : '',
                            arrowhead_width: marketDirection
                                ? getComputedStyle(marketDirection, '::before').borderBottomWidth : '',
                            stem_height: marketDirection
                                ? getComputedStyle(marketDirection, '::after').height : '',
                        },
                        market_value_badges: [...document.querySelectorAll(
                            '#financialFeedCards [data-financial-item-id="overview:sse"] .keyword-badge-unknown'
                        )].map(node => node.textContent),
                        stock_movement: {text: stockMovement?.textContent || '', class_name: stockMovement?.className || ''},
                        stock_delete_count: stock?.querySelectorAll(
                            '.article-card-action.delete[aria-label="删除个股卡片"]'
                        ).length || 0,
                        article_title_marks: article?.querySelectorAll('[data-card-title] mark').length || 0,
                        article_summary_marks: article?.querySelectorAll('[data-card-preview] mark').length || 0,
                        article_keyword_badges: [...(article?.querySelectorAll('.keyword-badge-title') || [])]
                            .map(node => node.textContent),
                    };
                }"""
            )
            market_card_selector = '#financialFeedCards [data-financial-item-id="overview:sse"]'
            page.locator(f"{market_card_selector} .article-card-action.view").click()
            page.wait_for_function(
                "document.querySelector('#financialMarketDetailModal')?.classList.contains('active')"
                " && document.querySelectorAll('#financialHistoryChart path').length === 1"
                " && document.querySelectorAll('#financialRelatedNewsList .financial-related-news-item').length === 6"
            )
            market_detail = page.evaluate(
                """() => ({
                    modal_open: document.querySelector('#financialMarketDetailModal')?.classList.contains('active'),
                    modal_class: document.querySelector('#financialMarketDetailModal')?.className || '',
                    content_class: document.querySelector('#financialMarketDetailModal > .article-modal-content')?.className || '',
                    chart_paths: document.querySelectorAll('#financialHistoryChart path').length,
                    chart_points: document.querySelectorAll('#financialHistoryChart circle').length,
                    news_count: document.querySelectorAll('#financialRelatedNewsList .financial-related-news-item').length,
                    source_link_count: document.querySelectorAll('#financialMarketDetailModal a[target="_blank"]').length,
                    current_text: document.querySelector('#financialMarketDetailCurrent')?.textContent || '',
                })"""
            )
            market_layout = page.evaluate(
                """() => {
                    const grid = document.querySelector('#financialMarketDetailGrid');
                    const history = grid.querySelector('.financial-market-detail-panel.is-history');
                    const news = grid.querySelector('.financial-market-detail-panel.is-news');
                    const list = document.querySelector('#financialRelatedNewsList');
                    const cards = [...list.querySelectorAll('.financial-related-news-item')];
                    return {
                        grid_class: grid.className,
                        history_width: Math.round(history.getBoundingClientRect().width),
                        news_width: Math.round(news.getBoundingClientRect().width),
                        list_client_height: list.clientHeight,
                        list_scroll_height: list.scrollHeight,
                        card_widths: [...new Set(cards.map(card => Math.round(card.getBoundingClientRect().width)))],
                        card_heights: [...new Set(cards.map(card => Math.round(card.getBoundingClientRect().height)))],
                        article_card_count: cards.filter(card => card.classList.contains('article-card')).length,
                        first_row_count: cards.filter(card => Math.round(card.getBoundingClientRect().top)
                            === Math.round(cards[0].getBoundingClientRect().top)).length,
                    };
                }"""
            )
            market_modal_size = page.evaluate(
                """() => { const rect=document.querySelector('#financialMarketDetailModal > .article-modal-content').getBoundingClientRect();
                    return {width:Math.round(rect.width),height:Math.round(rect.height)}; }"""
            )
            page.locator("#financialRelatedNewsList .financial-related-news-item .article-card-action.view").first.click()
            page.wait_for_function(
                "document.querySelector('#articleModal')?.classList.contains('active')"
                " && document.querySelector('#modalTitle')?.textContent.includes('上证指数相关新闻')"
            )
            related_news_article = page.evaluate(
                """() => ({
                    market_modal_open: document.querySelector('#financialMarketDetailModal')?.classList.contains('active'),
                    article_modal_open: document.querySelector('#articleModal')?.classList.contains('active'),
                    modal_class: document.querySelector('#articleModal')?.className || '',
                    content_class: document.querySelector('#articleModal > .article-modal-content')?.className || '',
                    title: document.querySelector('#modalTitle')?.textContent || '',
                    content: document.querySelector('#modalContent')?.textContent || '',
                    translation_button: Boolean(document.querySelector('#modalTranslateBtn')),
                    speech_button: Boolean(document.querySelector('#articleModal .article-speech-action')),
                })"""
            )
            article_modal_size = page.evaluate(
                """() => { const rect=document.querySelector('#articleModal > .article-modal-content').getBoundingClientRect();
                    return {width:Math.round(rect.width),height:Math.round(rect.height)}; }"""
            )
            page.evaluate("closeArticleModal()")
            page.wait_for_function(
                "document.querySelector('#financialMarketDetailModal')?.classList.contains('active')"
                " && !document.querySelector('#articleModal')?.classList.contains('active')"
                " && document.activeElement?.classList.contains('financial-related-news-item')"
            )
            returned_market = page.evaluate(
                """() => ({
                    market_open: document.querySelector('#financialMarketDetailModal')?.classList.contains('active'),
                    article_open: document.querySelector('#articleModal')?.classList.contains('active'),
                    chart_paths: document.querySelectorAll('#financialHistoryChart path').length,
                    focused_related_card: document.activeElement?.classList.contains('financial-related-news-item') || false,
                })"""
            )
            page.locator("#financialRelatedNewsList .financial-related-news-item .article-card-action.view").nth(1).click()
            page.wait_for_function(
                "document.querySelector('#articleModal')?.classList.contains('active')"
            )
            page.keyboard.press("Escape")
            page.wait_for_function(
                "document.querySelector('#financialMarketDetailModal')?.classList.contains('active')"
                " && !document.querySelector('#articleModal')?.classList.contains('active')"
                " && document.activeElement?.classList.contains('financial-related-news-item')"
            )
            second_news_return = page.evaluate(
                """() => ({
                    market_open: document.querySelector('#financialMarketDetailModal')?.classList.contains('active'),
                    chart_paths: document.querySelectorAll('#financialHistoryChart path').length,
                    focused_title: document.activeElement?.querySelector('.article-card-title')?.textContent || '',
                })"""
            )
            page.evaluate("closeFinancialMarketDetail()")
            news_card_selector = '#financialFeedCards [data-financial-item-id="article:202"]'
            page.locator(f"{news_card_selector} .article-card-action.view").click()
            page.wait_for_function(
                "document.querySelector('#articleModal')?.classList.contains('active')"
                " && document.querySelector('#modalContent')?.textContent.includes('文章详情正文')"
            )
            direct_news_article = page.evaluate(
                """() => ({
                    modal_open: document.querySelector('#articleModal')?.classList.contains('active'),
                    title: document.querySelector('#modalTitle')?.textContent || '',
                    content: document.querySelector('#modalContent')?.textContent || '',
                })"""
            )
            page.evaluate("closeArticleModal()")
            page.locator('#financialFeedCards [data-financial-item-id="overview:nasdaq"] .article-card-action.view').click()
            page.wait_for_function(
                "document.querySelector('#financialMarketDetailGrid')?.classList.contains('has-no-history')"
                " && document.querySelectorAll('#financialRelatedNewsList .financial-related-news-item').length === 6"
            )
            empty_history_layout = page.evaluate(
                """() => {
                    const grid = document.querySelector('#financialMarketDetailGrid');
                    const history = grid.querySelector('.financial-market-detail-panel.is-history');
                    const news = grid.querySelector('.financial-market-detail-panel.is-news');
                    const chart = document.querySelector('#financialHistoryChart');
                    return {
                        grid_class: grid.className,
                        history_width: Math.round(history.getBoundingClientRect().width),
                        news_width: Math.round(news.getBoundingClientRect().width),
                        chart_height: Math.round(chart.getBoundingClientRect().height),
                        empty_visible: Boolean(chart.querySelector('.financial-history-empty')),
                        chart_svg_count: chart.querySelectorAll('svg').length,
                    };
                }"""
            )
            page.evaluate("closeFinancialMarketDetail()")
            page.goto(
                f"http://127.0.0.1:{server.server_port}/financial?financial_module=reports",
                wait_until="domcontentloaded",
            )
            page.wait_for_function(
                "document.querySelector('#financialFeedSection')?.hidden === false"
            )
            page.wait_for_function(
                "document.querySelectorAll('#financialFeedCards .financial-feed-card').length === 8"
            )
            menu = page.evaluate(
                """() => Object.fromEntries([...document.querySelectorAll('[data-financial-workspace-menu]')]
                    .map(node => [node.dataset.financialWorkspaceMenu, !node.hidden]))"""
            )
            report_card_selector = '#financialFeedCards [data-financial-item-id="report:7:v1"]'
            page.locator(f"{report_card_selector} .article-card-action.translate").click()
            page.wait_for_function(
                "document.querySelector('#financialFeedCards [data-financial-item-id=\"report:7:v1\"] [data-card-title]')?.textContent"
                " === 'Tencent TradingAgents Final Report'"
            )
            translated_card = page.evaluate(
                """() => {
                    const card = document.querySelector('#financialFeedCards [data-financial-item-id="report:7:v1"]');
                    return {
                        class_name: card?.className || '',
                        height: Math.round(card?.getBoundingClientRect().height || 0),
                        title: card?.querySelector('[data-card-title]')?.textContent || '',
                        preview: card?.querySelector('[data-card-preview]')?.textContent || '',
                        translated: card?.dataset.translated || '',
                    };
                }"""
            )
            page.locator(f"{report_card_selector} .article-card-action.view").click()
            page.wait_for_function(
                "document.querySelector('#financialReportModal')?.classList.contains('open')"
                " && document.querySelectorAll('.financial-report-groups > details').length === 5"
            )
            report = page.evaluate(
                """() => ({
                    home_view: document.body.dataset.homeView,
                    feed_badge: document.querySelector('#financialFeedCards .keyword-badge')?.textContent,
                    overview_keys: [...document.querySelectorAll('#financialFeedCards [data-financial-item-id^="overview:"]')]
                        .map(card => card.dataset.financialItemId),
                    modal_open: document.querySelector('#financialReportModal')?.classList.contains('open'),
                    group_count: document.querySelectorAll('.financial-report-groups > details').length,
                    report_text: document.querySelector('#financialReportBody')?.textContent || '',
                    active_scripts: document.querySelectorAll('#financialReportBody script').length,
                })"""
            )
            page.evaluate("closeFinancialReport()")
            stock_card_selector = '#financialFeedCards [data-financial-item-id="snapshot:101"]'
            page.locator(
                f'{stock_card_selector} .article-card-action.delete[aria-label="删除个股卡片"]'
            ).click()
            page.locator(".app-confirm-ok").click()
            page.wait_for_function(
                "document.querySelector('#financialFeedCards [data-financial-item-id=\"snapshot:101\"]') === null"
            )
            deleted_in_place = page.locator(stock_card_selector).count() == 0
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(
                f"http://127.0.0.1:{server.server_port}/?home_view=dashboard",
                wait_until="domcontentloaded",
            )
            page.wait_for_selector("#intelTodayCards .article-card")
            page.wait_for_selector("#financialFeedCards .article-card")
            deleted_after_reload = page.locator(stock_card_selector).count() == 0
            mobile_cards = page.evaluate(
                """() => {
                    const shape = selector => {
                        const rect = document.querySelector(selector).getBoundingClientRect();
                        return {width: Math.round(rect.width), height: Math.round(rect.height)};
                    };
                    return {
                        ordinary: shape('#intelTodayCards .article-card'),
                        financial: shape('#financialFeedCards .article-card'),
                    };
                }"""
            )
            page.locator('#financialFeedCards [data-financial-item-id="overview:sse"] .article-card-action.view').click()
            page.wait_for_function(
                "document.querySelector('#financialMarketDetailModal')?.classList.contains('active')"
            )
            mobile_market_modal = page.evaluate(
                """() => { const rect=document.querySelector('#financialMarketDetailModal > .article-modal-content').getBoundingClientRect();
                    return {left:Math.round(rect.left),top:Math.round(rect.top),right:Math.round(rect.right),
                        bottom:Math.round(rect.bottom),width:Math.round(rect.width),height:Math.round(rect.height),
                        viewport_width:innerWidth,viewport_height:innerHeight}; }"""
            )
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=2)

    report_filter_used = any(
        item.get("endpoint") == "feed" and item.get("content_kind") == "research_opinion"
        for item in calls
    )
    passed = bool(
        menu == {
            "financial_zone": True,
            "tradingagents_reports": True,
            "simulation": True,
            "backtesting": True,
        }
        and homepage == {
            "workspace": "false",
            "financial_nav_count": 0,
            "financial_module": "",
            "simulation_hidden": True,
        }
        and homepage_cards["ordinary"] == homepage_cards["financial"]
        and homepage_cards["financial"]["height"] == 230
        and mobile_cards["ordinary"] == mobile_cards["financial"]
        and mobile_cards["financial"]["height"] == 230
        and homepage_cards["financial_class"] == "article-card financial-feed-card"
        and homepage_description == "行情事实、金融原文与研究观点，不混入普通行业资讯统计"
        and financial_card_contract == {
            "market_movement": {
                "text": "↑ +0.22%",
                "class_name": "financial-market-movement is-red",
            },
            "market_direction": {
                "text": "↑", "class_name": "financial-market-direction is-up",
                "width": 18, "height": 24, "border_style": "none",
                "arrowhead_width": "9px", "stem_height": "13px",
            },
            "market_value_badges": ["涨跌幅：0.22%", "价格：3,617.60"],
            "stock_movement": {
                "text": "↓ -0.31%",
                "class_name": "financial-market-movement is-green",
            },
            "stock_delete_count": 1,
            "article_title_marks": 1,
            "article_summary_marks": 1,
            "article_keyword_badges": ["家族办公室"],
        }
        and deleted_in_place
        and deleted_after_reload
        and market_modal_size == article_modal_size
        and market_modal_size["height"] >= 800
        and mobile_market_modal["left"] >= 0
        and mobile_market_modal["top"] >= 0
        and mobile_market_modal["right"] <= mobile_market_modal["viewport_width"]
        and mobile_market_modal["bottom"] <= mobile_market_modal["viewport_height"]
        and market_detail == {
            "modal_open": True,
            "modal_class": "article-modal active",
            "content_class": "article-modal-content",
            "chart_paths": 1,
            "chart_points": 3,
            "news_count": 6,
            "source_link_count": 0,
            "current_text": "涨跌幅：0.22% · 价格：3617.6 · 2026-08-05 15:00:00",
        }
        and market_layout["grid_class"] == "financial-market-detail-grid"
        and market_layout["history_width"] <= market_layout["news_width"] * 1.15
        and market_layout["list_scroll_height"] > market_layout["list_client_height"]
        and market_layout["card_heights"] == [230]
        and all(240 <= width <= 280 for width in market_layout["card_widths"])
        and market_layout["article_card_count"] == 6
        and market_layout["first_row_count"] == 2
        and related_news_article == {
            "market_modal_open": False,
            "article_modal_open": True,
            "modal_class": "article-modal active",
            "content_class": "article-modal-content",
            "title": "上证指数相关新闻",
            "content": "这是文章详情正文；金融新闻卡与行情详情中的相关新闻都必须复用这个窗口。",
            "translation_button": True,
            "speech_button": True,
        }
        and returned_market == {
            "market_open": True,
            "article_open": False,
            "chart_paths": 1,
            "focused_related_card": True,
        }
        and second_news_return == {
            "market_open": True,
            "chart_paths": 1,
            "focused_title": "上证指数相关新闻 2",
        }
        and "has-no-history" in empty_history_layout["grid_class"].split()
        and empty_history_layout["news_width"] > empty_history_layout["history_width"] * 2
        and 220 <= empty_history_layout["chart_height"] <= 250
        and empty_history_layout["empty_visible"]
        and empty_history_layout["chart_svg_count"] == 0
        and direct_news_article == {
            "modal_open": True,
            "title": "上证指数相关新闻",
            "content": "这是文章详情正文；金融新闻卡与行情详情中的相关新闻都必须复用这个窗口。",
        }
        and not popup_urls
        and report["home_view"] == "dashboard"
        and report["feed_badge"] == "大盘"
        and report["overview_keys"] == [
            "overview:sse", "overview:szse", "overview:hsi",
            "overview:nasdaq", "overview:nikkei",
        ]
        and report["modal_open"]
        and report["group_count"] == 5
        and "主要反证" in report["report_text"]
        and "不构成投资建议" in report["report_text"]
        and report["active_scripts"] == 0
        and translated_card == {
            "class_name": "article-card financial-feed-card",
            "height": 230,
            "title": "Tencent TradingAgents Final Report",
            "preview": "Fundamentals are stable, but counter-evidence still requires review.",
            "translated": "1",
        }
        and any(
            item.get("endpoint") == "financial_card_translation"
            and item.get("item_id") == "report:7:v1"
            and item.get("keys") == ["industry_pack_id", "item_id", "summary", "title"]
            for item in calls
        )
        and report_filter_used
        and any(item.get("endpoint") == "report" for item in calls)
        and any(item.get("endpoint") == "market_detail" and item.get("item_id") == "overview:sse" for item in calls)
        and sum(item.get("endpoint") == "market_detail" and item.get("item_id") == "overview:sse" for item in calls) == 2
        and any(item.get("endpoint") == "market_detail" and item.get("item_id") == "overview:nasdaq" for item in calls)
        and any(item.get("endpoint") == "hide_instrument" and item.get("instrument_id") == 77 for item in calls)
        and sum(item.get("endpoint") == "article_detail" and item.get("article_id") == 202 for item in calls) >= 2
        and any(item.get("endpoint") == "article_detail" and item.get("article_id") == 203 for item in calls)
        and not page_errors
    )
    return {
        "check_version": "financial-stage3-browser-v1",
        "passed": passed,
        "browser": str(browser_executable),
        "network_scope": "loopback_fixture_only",
        "menu": menu,
        "homepage": homepage,
        "homepage_cards": homepage_cards,
        "homepage_description": homepage_description,
        "financial_card_contract": financial_card_contract,
        "deleted_in_place": deleted_in_place,
        "deleted_after_reload": deleted_after_reload,
        "market_detail": market_detail,
        "market_layout": market_layout,
        "market_modal_size": market_modal_size,
        "article_modal_size": article_modal_size,
        "mobile_market_modal": mobile_market_modal,
        "related_news_article": related_news_article,
        "returned_market": returned_market,
        "second_news_return": second_news_return,
        "empty_history_layout": empty_history_layout,
        "direct_news_article": direct_news_article,
        "popup_urls": popup_urls,
        "mobile_cards": mobile_cards,
        "report": report,
        "translated_card": translated_card,
        "calls": calls,
        "report_filter_used": report_filter_used,
        "page_errors": page_errors,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chromium", default="")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    report = run_browser_e2e(args.chromium)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

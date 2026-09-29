#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Desktop/mobile Chromium acceptance for non-family financial-addon cards."""

from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template
from playwright.sync_api import sync_playwright
from werkzeug.serving import WSGIRequestHandler, make_server


ROOT = Path(__file__).resolve().parents[1]


def _chromium(explicit: str = "") -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if path.is_file():
            return path
        raise FileNotFoundError(f"Chromium 不存在：{path}")
    paths = sorted(
        (ROOT / "data" / "ms-playwright").glob("chromium-*/chrome-linux*/chrome"),
        reverse=True,
    )
    if not paths:
        raise FileNotFoundError("未找到项目内 Playwright Chromium")
    return paths[0].resolve()


class _Quiet(WSGIRequestHandler):
    def log_request(self, *args, **kwargs):
        return None


def _app() -> Flask:
    app = Flask(
        "financial-addon-browser",
        template_folder=str(ROOT / "templates"),
        static_folder=str(ROOT / "static"),
    )

    @app.get("/")
    def home():
        return render_template("mapindex.html", financial_workspace=False)

    @app.get("/api/intel/industry-packs")
    def packs():
        return jsonify({
            "success": True,
            "default_industry_pack_id": "education_news",
            "industry_packs": [{"id": "education_news", "name": "教育"}],
        })

    @app.get("/api/intel/dashboard")
    def dashboard():
        sections = {
            key: {"articles": [], "total": 0}
            for key in ("today", "trend", "policy", "recent", "other")
        }
        sections["today"] = {
            "articles": [{
                "article_id": 1,
                "title": "教育行业动态标准卡片",
                "content_preview": "普通行业卡片用于比较金融附包卡片的视觉尺寸。",
                "source_display_name": "fixture",
                "publish_date": "2026-08-06",
                "matched_keywords": ["教育"],
            }],
            "total": 1,
        }
        return jsonify({
            "success": True,
            "industry_pack": {"id": "education_news", "name": "教育"},
            "sections": sections,
            "total": 1,
            "statistics": {
                "total_articles": 1,
                "industry_valid_articles": 1,
                "window_articles": 1,
                "window_days": 90,
            },
        })

    @app.get("/api/intel/financial/feed")
    def feed():
        return jsonify({
            "success": True,
            "visible": True,
            "industry_pack_id": "education_news",
            "effective_pack_ids": ["education_news", "financial_markets"],
            "market_overview": [],
            "items": [{
                "content_kind": "source_document",
                "kind_label": "原文",
                "item_id": "article:2",
                "article_id": 2,
                "title": "教育上市公司发布资本市场公告",
                "summary": "教育科技企业公告涉及学生与校园服务。",
                "subcategory": "financial_news",
                "observed_at": "2026-08-06T02:00:00Z",
                "matched_keywords": ["教育", "教育科技", "学生", "校园"],
                "source": {"provider_name": "finance.fixture", "url": "https://example.test"},
            }],
            "counts": {"market_fact": 0, "source_document": 1, "research_opinion": 0},
            "total": 1,
            "page": 1,
            "per_page": 15,
            "total_pages": 1,
            "categories": [{"key": "financial_news", "name": "金融资讯与公告"}],
            "dashboard_card_visibility": {
                "show_financial_news": True,
                "show_market_index_cards": False,
                "show_watched_stock_cards": False,
            },
            "availability": {"status": "healthy", "message": "ok"},
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
    def missing(_error):
        return jsonify({"success": False, "error": "fixture route"}), 404

    return app


def run(chromium_path: str = "") -> dict:
    executable = _chromium(chromium_path)
    server = make_server("127.0.0.1", 0, _app(), threaded=True, request_handler=_Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors = []
    measurements = {}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(executable), headless=True
            )
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            for name, viewport in (
                ("desktop", {"width": 1440, "height": 1000}),
                ("mobile", {"width": 390, "height": 844}),
            ):
                page.set_viewport_size(viewport)
                page.goto(
                    f"http://127.0.0.1:{server.server_port}/?home_view=dashboard",
                    wait_until="domcontentloaded",
                )
                page.wait_for_selector("#intelTodayCards .article-card")
                page.wait_for_selector("#financialFeedCards .article-card")
                measurements[name] = page.evaluate(
                    """() => {
                        const ordinary=document.querySelector('#intelTodayCards .article-card');
                        const financial=document.querySelector('#financialFeedCards .article-card');
                        const shape=node=>{const r=node.getBoundingClientRect();return {width:Math.round(r.width),height:Math.round(r.height)}};
                        return {
                            ordinary:shape(ordinary), financial:shape(financial),
                            financial_class:financial.className,
                            overview_count:document.querySelectorAll('#financialFeedCards [data-financial-item-id^="overview:"]').length,
                            market_fact_count:document.querySelectorAll('#financialFeedCards [data-content-kind="market_fact"]').length,
                            source_count:document.querySelectorAll('#financialFeedCards [data-content-kind="source_document"]').length,
                            title_marks:financial.querySelectorAll('[data-card-title] mark').length,
                            summary_marks:financial.querySelectorAll('[data-card-preview] mark').length,
                            keyword_badges:[...financial.querySelectorAll('.keyword-badge-title')].map(node=>node.textContent),
                        };
                    }"""
                )
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=2)
    passed = not errors and all(
        item["ordinary"] == item["financial"]
        and item["financial"]["height"] == 230
        and item["financial_class"] == "article-card financial-feed-card"
        and item["overview_count"] == 0
        and item["market_fact_count"] == 0
        and item["source_count"] == 1
        and item["title_marks"] >= 1
        and item["summary_marks"] >= 1
        and item["keyword_badges"] == ["教育", "教育科技", "学生", "校园"]
        for item in measurements.values()
    )
    return {
        "check_version": "financial-addon-browser-v1",
        "passed": passed,
        "browser": str(executable),
        "measurements": measurements,
        "page_errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chromium-path", default="")
    args = parser.parse_args()
    result = run(args.chromium_path)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

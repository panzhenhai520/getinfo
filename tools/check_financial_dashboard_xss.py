#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Render hostile financial dashboard content and verify no active DOM is created."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from flask import Flask, render_template
from playwright.sync_api import sync_playwright


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _find_chromium(explicit_path: str = "") -> Path:
    if explicit_path:
        candidate = Path(explicit_path).expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise FileNotFoundError(f"Chromium 不存在：{candidate}")
    candidates = sorted(
        [
            *(PROJECT_ROOT / "data" / "ms-playwright").glob("chromium-*/chrome-linux*/chrome"),
            *Path("/ms-playwright").glob("chromium-*/chrome-linux*/chrome"),
        ],
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError("未找到项目内 Playwright Chromium；请先安装浏览器运行时")
    return candidates[0].resolve()


def check_xss(chromium_path: str = "") -> dict:
    browser_executable = _find_chromium(chromium_path)
    app = Flask(
        "financial-dashboard-xss",
        template_folder=str(PROJECT_ROOT / "templates"),
        static_folder=str(PROJECT_ROOT / "static"),
    )
    with app.app_context():
        html = render_template("mapindex.html")

    console_errors = []
    intercepted_requests = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(browser_executable),
            headless=True,
        )
        page = browser.new_page()
        page.on("pageerror", lambda error: console_errors.append(str(error)))
        def fulfill_local(route):
            intercepted_requests.append(route.request.url)
            if route.request.url.rstrip("/") == "http://financial.test":
                route.fulfill(status=200, content_type="text/html", body=html)
            else:
                route.fulfill(status=404, content_type="application/json", body='{"success":false}')

        page.route("**/*", fulfill_local)
        page.goto("http://financial.test/", wait_until="domcontentloaded")
        # Let the template's initial dashboard request settle before replacing
        # fetch and rendering the hostile fixtures below.
        page.wait_for_timeout(150)
        result = page.evaluate(
            """
            () => {
                window.__financialTitleXss = 0;
                window.__financialSourceXss = 0;
                window.__financialKeywordXss = 0;
                window.__financialFeedXss = 0;
                window.__financialReportXss = 0;
                window.fetch = async () => ({ok: false, json: async () => ({success: false})});
                const hostile = {
                    article_id: '7\" onmouseover=window.__financialTitleXss=1',
                    title: 'SFC <script>window.__financialTitleXss=1</script> & policy',
                    content_preview: '<img src=x onerror=window.__financialTitleXss=1> market update',
                    source_display_name: '<img src=x onerror=window.__financialSourceXss=1>',
                    domain: '\"><svg onload=window.__financialSourceXss=1>',
                    publish_date: '<img src=x onerror=window.__financialSourceXss=1>',
                    content_length: '9\" onmouseover=window.__financialTitleXss=1',
                    matched_keywords: ['SFC', '<svg onload=window.__financialKeywordXss=1>'],
                };
                const host = document.createElement('div');
                host.id = 'financial-xss-host';
                host.innerHTML = renderIntelCard(hostile);
                document.body.appendChild(host);
                renderFinancialFeed({
                    visible: true,
                    total: 2,
                    page: 1,
                    total_pages: 1,
                    counts: {market_fact: 0, source_document: 2, research_opinion: 0},
                    categories: [{key: 'financial_news', name: '<svg onload=window.__financialFeedXss=1>'}],
                    items: [
                        {
                            content_kind: 'source_document', kind_label: '<img src=x onerror=window.__financialFeedXss=1>',
                            title: 'Feed <script>window.__financialFeedXss=1</script>',
                            summary: '<img src=x onerror=window.__financialFeedXss=1>',
                            subcategory: 'financial_news', observed_at: '2026-08-03T01:00:00Z',
                            source: {
                                provider_name: '<svg onload=window.__financialFeedXss=1>',
                                url: 'https://public.example/story?id=7&API_KEY=leak-me#secret',
                            },
                        },
                        {
                            content_kind: 'source_document', kind_label: '原文',
                            title: 'Unsafe URL', summary: 'javascript must not become a link',
                            subcategory: 'financial_news', observed_at: '2026-08-03T01:00:00Z',
                            source: {provider_name: 'hostile', url: 'javascript:window.__financialFeedXss=1'},
                        },
                    ],
                });
                renderFinancialReport({
                    report_id: 7,
                    report_version: 1,
                    title: 'Report <script>window.__financialReportXss=1</script>',
                    executive_summary: '<img src=x onerror=window.__financialReportXss=1>',
                    recommendation: '<svg onload=window.__financialReportXss=1>',
                    target: {display_name: '<img src=x onerror=window.__financialReportXss=1>'},
                    overview: {
                        as_of: '2026-08-03T01:00:00Z',
                        core_reason: '<script>window.__financialReportXss=1</script>',
                        counter_evidence: '<svg onload=window.__financialReportXss=1>',
                        data_gaps: ['<img src=x onerror=window.__financialReportXss=1>'],
                    },
                    risk_summary: {summary: '<img src=x onerror=window.__financialReportXss=1>'},
                    section_groups: [{key: 'analysis', name: '<svg onload=window.__financialReportXss=1>'}],
                    sections: [{
                        group: 'analysis', role_label: '<img src=x onerror=window.__financialReportXss=1>',
                        section_type: 'news', status: 'completed', citations: [],
                        content_markdown: '<script>window.__financialReportXss=1</script>',
                    }],
                    disclaimer: '<svg onload=window.__financialReportXss=1>',
                });
                const actions = document.createElement('div');
                actions.id = 'financial-xss-actions';
                actions.appendChild(simulationAction('unsafe link', null, 'javascript:window.__financialReportXss=1'));
                actions.appendChild(simulationAction('private link', null, 'http://127.0.0.1/admin'));
                document.body.appendChild(actions);
                return {
                    title_text: host.querySelector('[data-card-title]')?.textContent || '',
                    preview_text: host.querySelector('[data-card-preview]')?.textContent || '',
                    host_text: host.textContent || '',
                    feed_text: document.getElementById('financialFeedCards')?.textContent || '',
                    report_text: document.getElementById('financialReportBody')?.textContent || '',
                };
            }
            """
        )
        page.wait_for_timeout(150)
        active = page.evaluate(
            """
            () => {
                const host = document.getElementById('financial-xss-host');
                const dynamic = [
                    host,
                    document.getElementById('financialFeedCards'),
                    document.getElementById('financialFeedCategories'),
                    document.getElementById('financialReportModalTitle'),
                    document.getElementById('financialReportMeta'),
                    document.getElementById('financialReportBody'),
                    document.getElementById('financial-xss-actions'),
                ].filter(Boolean);
                const nodes = dynamic.flatMap(node => [node, ...node.querySelectorAll('*')]);
                const links = dynamic.flatMap(node => Array.from(node.querySelectorAll('a')));
                return {
                    script_nodes: dynamic.reduce((count, node) => count + node.querySelectorAll('script').length, 0),
                    image_nodes: dynamic.reduce((count, node) => count + node.querySelectorAll('img').length, 0),
                    svg_nodes: dynamic.reduce((count, node) => count + node.querySelectorAll('svg').length, 0),
                    event_attributes: nodes
                        .flatMap(node => Array.from(node.attributes))
                        .filter(attribute => attribute.name.toLowerCase().startsWith('on'))
                        .map(attribute => ({name: attribute.name, value: attribute.value})),
                    link_hrefs: links.map(link => link.href),
                    secret_link_count: links.filter(link => /api_key|leak-me|#secret/i.test(link.href)).length,
                    unsafe_link_count: links.filter(link => /^(?:javascript:|http:\/\/(?:127\.|10\.|localhost))/i.test(link.href)).length,
                    title_xss: window.__financialTitleXss,
                    source_xss: window.__financialSourceXss,
                    keyword_xss: window.__financialKeywordXss,
                    feed_xss: window.__financialFeedXss,
                    report_xss: window.__financialReportXss,
                };
            }
            """
        )
        browser.close()

    passed = bool(
        active["script_nodes"] == 0
        and active["image_nodes"] == 0
        and active["svg_nodes"] == 0
        and not any(
            "__financial" in str(attribute.get("value") or "")
            for attribute in active["event_attributes"]
        )
        and active["title_xss"] == 0
        and active["source_xss"] == 0
        and active["keyword_xss"] == 0
        and active["feed_xss"] == 0
        and active["report_xss"] == 0
        and active["secret_link_count"] == 0
        and active["unsafe_link_count"] == 0
        and "<script>" in result["title_text"]
        and "<img" in result["preview_text"]
        and "<svg" in result["host_text"]
        and "<script>" in result["feed_text"]
        and "<img" in result["feed_text"]
        and "<script>" in result["report_text"]
        and "<img" in result["report_text"]
    )
    return {
        "check_version": "financial-dashboard-xss-v2",
        "passed": passed,
        "browser": str(browser_executable),
        "active_dom": active,
        "dangerous_text_preserved_as_text": {
            "title": "<script>" in result["title_text"],
            "preview": "<img" in result["preview_text"],
            "keyword": "<svg" in result["host_text"],
            "financial_feed": "<script>" in result["feed_text"] and "<img" in result["feed_text"],
            "financial_report": "<script>" in result["report_text"] and "<img" in result["report_text"],
        },
        # Network/static-load errors are expected for set_content().  They are
        # reported for audit but do not affect the hostile-card assertion.
        "page_error_count": len(console_errors),
        "intercepted_request_count": len(intercepted_requests),
        "external_network_requests": [
            url for url in intercepted_requests if not url.startswith("http://financial.test")
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the financial card XSS browser smoke")
    parser.add_argument("--chromium", default="")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    report = check_xss(args.chromium)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

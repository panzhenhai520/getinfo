#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Loopback-only Chromium acceptance for task 5.5 Dashboard behavior."""

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


def _article(article_id: int, category: str) -> dict:
    return {
        "article_id": article_id, "title": f"{category} 分类浏览器夹具",
        "content": (f"{category} 分类内容用于验证折叠后真实释放布局高度。" * 18),
        "domain": "fixture.example", "source_display_name": "Browser Fixture",
        "publish_date": "2026-08-02", "effective_time": "2026-08-02T01:00:00Z",
        "matched_keywords": ["家族办公室"], "title_keywords": ["家族办公室"],
        "content_keywords": [], "category": category, "confidence": 0.9,
    }


def _fixture_app(calls: list[dict]) -> Flask:
    app = Flask(
        "financial-simulation-browser", template_folder=str(ROOT / "templates"),
        static_folder=str(ROOT / "static"),
    )

    @app.get("/")
    def home():
        return render_template("mapindex.html", financial_workspace=False)

    @app.get("/financial")
    def financial():
        return render_template("mapindex.html", financial_workspace=True)

    @app.get("/api/intel/industry-packs")
    def packs():
        return jsonify({
            "success": True, "default_industry_pack_id": "family_office",
            "industry_packs": [{"id": "family_office", "name": "家族办公室"}],
        })

    @app.get("/api/intel/dashboard")
    def dashboard():
        return jsonify({
            "success": True,
            "industry_pack": {"id": "family_office", "name": "家族办公室"},
            "dashboard_preference_namespace": "browser-user-a",
            "sections": {
                key: {"articles": [_article(index + 1, key)], "total": 1}
                for index, key in enumerate(("today", "trend", "policy", "recent", "other"))
            },
            "total": 5,
            "statistics": {
                "total_articles": 5, "industry_valid_articles": 5,
                "window_articles": 5, "window_days": 7,
            },
        })

    @app.get("/api/intel/financial/capabilities")
    def capabilities():
        return jsonify({
            "success": True,
            "effective_capabilities": {
                "financial_zone": True, "tradingagents_reports": True,
                "simulation": True, "backtesting": True,
            },
        })

    @app.get("/api/intel/financial/feed")
    def feed():
        return jsonify({
            "success": True, "visible": True, "counts": {}, "total": 0,
            "page": 1, "total_pages": 0, "categories": [], "items": [],
        })

    @app.get("/api/intel/financial/simulation/overview")
    def overview():
        mode = str(request.args.get("mode") or "simulation")
        report_id = int(request.args.get("report_id") or 0) or None
        calls.append({"endpoint": "overview", "mode": mode, "report_id": report_id})
        account = {
            "account_id": "account-browser", "account_name": "浏览器纸面账户",
            "base_currency": "CNY", "initial_cash": 100000, "cash_balance": 88000,
            "equity": 101500, "position_count": 1, "order_count": 1, "fill_count": 1,
            "positions": [{
                "display_name": "浦发银行", "canonical_symbol": "600000.SH",
                "quantity": 1000, "market_value": 13500, "realized_pnl": 50,
            }],
            "orders": [{
                "order_id": "order-browser", "display_name": "浦发银行",
                "canonical_symbol": "600000.SH", "side": "buy", "order_type": "market",
                "quantity": 1000, "filled_quantity": 1000, "status": "completed",
                "final_report_id": 7,
            }],
            "fills": [{
                "fill_id": "fill-browser", "display_name": "浦发银行",
                "quantity": 1000, "price": 12, "fee": 3, "currency": "CNY",
                "filled_at": "2026-08-02T01:00:00Z", "snapshot_id": 11,
                "evidence_url": "/api/financial/snapshots/11",
            }],
            "export_url": "/api/intel/financial/simulation/export?kind=account&id=account-browser",
        }
        curve = [
            {"date": f"2026-07-{index + 1:02d}", "equity": 100000 + index * 400 + (-900 if index % 4 == 0 else 0)}
            for index in range(20)
        ]
        backtest = {
            "backtest_run_id": "run-browser", "scope_name": "浦发银行",
            "strategy_key": "buy_and_hold_v1", "start_date": "2026-07-01",
            "end_date": "2026-07-20", "data_cutoff_at": "2026-07-21T00:00:00Z",
            "status": "completed", "source_report_id": 7,
            "metrics": {
                "total_return": {"value": 0.09}, "max_drawdown": {"value": -0.04},
                "sharpe_ratio": {"value": 1.12}, "win_rate": {"value": 0.6},
                "data_coverage_ratio": {"value": 0.9},
            },
            "equity_curve": curve,
            "limitations": ["historical_calendar_coverage_below_full"],
            "trade_count": 2, "trades_truncated": False,
            "trades": [{
                "trade_id": 1, "display_name": "浦发银行", "side": "buy",
                "quantity": 1000, "price": 12, "fee": 3,
                "executed_at": "2026-07-02T01:00:00Z", "snapshot_id": 12,
                "evidence_url": "/api/financial/snapshots/12",
            }],
            "evidence": [{"snapshot_id": 12, "url": "/api/financial/snapshots/12"}],
            "export_url": "/api/intel/financial/simulation/export?kind=backtest&id=run-browser",
            "disclaimer": "历史回测仅供研究参考，不代表未来表现。",
        }
        return jsonify({
            "success": True, "visible": True, "mode": mode, "can_create": True,
            "execution_mode": "paper", "real_order_execution": False,
            "accounts": [account], "backtests": [backtest],
            "counts": {"accounts": 1, "backtests": 1}, "report_id": report_id,
            "related": {
                "paper_orders": [{**account["orders"][0], "account_name": account["account_name"]}],
                "backtests": [backtest],
            },
            "disclaimer": "全部账户、成交和回测均为纸面模拟。",
        })

    @app.get("/api/financial/reports/<int:report_id>")
    def report(report_id: int):
        calls.append({"endpoint": "report", "report_id": report_id})
        return jsonify({
            "success": True,
            "report": {
                "report_id": report_id, "report_version": 1,
                "title": "浦发银行终极报告", "recommendation": "hold",
                "executive_summary": "报告结论", "overview": {
                    "as_of": "2026-08-02T00:00:00Z", "core_reason": "核心理由",
                    "counter_evidence": "主要反证", "data_gaps": [],
                },
                "target": {"display_name": "浦发银行"}, "risk_summary": {},
                "section_groups": [], "sections": [],
                "disclaimer": "不构成投资建议",
            },
        })

    @app.post("/api/intel/financial/simulation/jobs")
    def create_job():
        return jsonify({"success": True, "job_url": "/api/intel/jobs/9"}), 202

    @app.get("/api/intel/jobs/9")
    def job():
        return jsonify({"success": True, "job": {"status": "completed"}})

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
        return jsonify({"success": False, "error": "fixture_route_not_implemented"}), 404

    return app


def run_browser_e2e(chromium_path: str = "") -> dict:
    executable = _find_chromium(chromium_path)
    calls: list[dict] = []
    app = _fixture_app(calls)
    server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=_QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    page_errors: list[str] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            context = browser.new_context(viewport={"width": 1100, "height": 650})
            page = context.new_page()
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            base = f"http://127.0.0.1:{server.server_port}"
            page.goto(f"{base}/?home_view=dashboard", wait_until="domcontentloaded")
            page.wait_for_selector("[data-dashboard-category='today'] .dashboard-category-toggle")
            page.wait_for_timeout(100)
            page.evaluate("document.getElementById('intelDashboard').scrollTop = 0")

            today = page.locator("[data-dashboard-category='today']")
            trend = page.locator("[data-dashboard-category='trend']")
            today_toggle = today.locator(".dashboard-category-toggle")
            toggle_position = page.evaluate(
                """() => { const section=document.querySelector('[data-dashboard-category=today]');
                    const row=section.querySelector('.dashboard-category-title-row');
                    const toggle=section.querySelector('.dashboard-category-toggle');
                    const title=section.querySelector('.intel-section-title');
                    const tr=toggle.getBoundingClientRect(), hr=title.getBoundingClientRect();
                    return {sameRow:toggle.parentElement===row && title.parentElement===row,
                        immediatelyBefore:toggle.nextElementSibling===title,
                        leftOfTitle:tr.right <= hr.left, ariaExpanded:toggle.getAttribute('aria-expanded')}; }"""
            )
            before = page.evaluate("document.querySelector('[data-dashboard-category=trend]').getBoundingClientRect().top")
            body_id = today_toggle.get_attribute("aria-controls")
            today_toggle.click()
            page.wait_for_function("document.querySelector('[data-dashboard-category=today] .intel-section-body').hidden")
            after = page.evaluate("document.querySelector('[data-dashboard-category=trend]').getBoundingClientRect().top")
            accessibility = page.evaluate(
                """id => { const button=document.querySelector('[data-dashboard-category=today] .dashboard-category-toggle');
                    const body=document.getElementById(id); return {expanded:button.getAttribute('aria-expanded'),
                    controls:button.getAttribute('aria-controls'), hidden:body.hidden, inert:body.inert,
                    ariaHidden:body.getAttribute('aria-hidden')}; }""", body_id,
            )

            today_toggle.click()
            page.wait_for_timeout(45)
            today_toggle.click()
            page.wait_for_function("document.querySelector('[data-dashboard-category=today] .intel-section-body').hidden")
            rapid_collapsed = today_toggle.get_attribute("aria-expanded") == "false"
            today_toggle.click()
            page.wait_for_function("!document.querySelector('[data-dashboard-category=today] .intel-section-body').hidden")
            rapid_expanded = today_toggle.get_attribute("aria-expanded") == "true"

            for category in ("today", "trend", "policy", "recent"):
                toggle = page.locator(f"[data-dashboard-category='{category}'] .dashboard-category-toggle")
                if toggle.get_attribute("aria-expanded") == "true":
                    toggle.click()
            page.wait_for_function(
                "['today','trend','policy','recent'].every(id => document.querySelector(`[data-dashboard-category=${id}] .intel-section-body`).hidden)"
            )
            other_top = page.evaluate("document.querySelector('[data-dashboard-category=other]').getBoundingClientRect().top")

            other_toggle = page.locator("[data-dashboard-category='other'] .dashboard-category-toggle")
            other_toggle.focus()
            page.keyboard.press("Enter")
            page.wait_for_function("document.querySelector('[data-dashboard-category=other] .intel-section-body').hidden")
            keyboard_collapsed = other_toggle.get_attribute("aria-expanded") == "false"

            dynamic = page.evaluate(
                """() => { const section=document.createElement('section'); section.className='intel-section';
                    section.dataset.dashboardCategory='dynamic-fixture'; section.innerHTML='<div class="intel-section-head"><div><h2>动态分类</h2></div><div class="intel-section-count">1</div></div><div>内容</div>';
                    document.getElementById('intelDashboard').appendChild(section); initializeDashboardCategories();
                    const toggle=section.querySelector('.dashboard-category-toggle'); const initiallyExpanded=toggle.getAttribute('aria-expanded')==='true';
                    toggle.click(); return {initiallyExpanded}; }"""
            )
            page.wait_for_function("document.querySelector('[data-dashboard-category=dynamic-fixture] .intel-section-body').hidden")
            dynamic.update(page.evaluate(
                """() => { const key=dashboardCategoryStorageKey(); const section=document.querySelector('[data-dashboard-category=dynamic-fixture]');
                    const persisted=JSON.parse(localStorage.getItem(key)||'{}')['dynamic-fixture']===true; section.remove();
                    initializeDashboardCategories(); const cleaned=!Object.prototype.hasOwnProperty.call(JSON.parse(localStorage.getItem(key)||'{}'),'dynamic-fixture');
                    return {persisted, cleaned, storageKey:key}; }"""
            ))

            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("[data-dashboard-category='today'] .dashboard-category-toggle")
            page.wait_for_function("document.querySelector('[data-dashboard-category=policy] .intel-section-body').hidden")
            persisted_after_reload = page.locator("[data-dashboard-category='policy'] .dashboard-category-toggle").get_attribute("aria-expanded") == "false"

            page.emulate_media(reduced_motion="reduce")
            reduced_toggle = page.locator("[data-dashboard-category='other'] .dashboard-category-toggle")
            reduced_toggle.click()
            reduced_motion = page.evaluate(
                """() => { const body=document.querySelector('[data-dashboard-category=other] .intel-section-body');
                    return {hidden:body.hidden, animations:body.getAnimations().length,
                    media:matchMedia('(prefers-reduced-motion: reduce)').matches}; }"""
            )

            page.goto(f"{base}/financial?financial_module=simulation", wait_until="domcontentloaded")
            page.wait_for_selector("#financialSimulationCards .financial-simulation-card[data-kind='account']")
            page.goto(f"{base}/financial?financial_module=backtesting&report_id=7", wait_until="domcontentloaded")
            page.wait_for_selector("#financialSimulationCards .financial-simulation-card[data-kind='backtest']")
            backtest = page.evaluate(
                """() => ({badge:document.querySelector('#financialSimulationCards .financial-paper-badge')?.textContent,
                    chart:document.querySelectorAll('#financialSimulationCards .financial-equity-chart').length,
                    exportText:[...document.querySelectorAll('#financialSimulationCards a')].map(x=>x.textContent).join('|'),
                    text:document.getElementById('financialSimulationCards').textContent})"""
            )
            page.get_by_text("查看来源报告 #7", exact=True).click()
            page.wait_for_function(
                "document.querySelector('#financialReportModal')?.classList.contains('open') && document.querySelector('#financialReportBody')?.textContent.includes('纸面订单')"
            )
            report_trace = "纸面回测" in page.locator("#financialReportBody").inner_text()
            page.locator("#financialReportClose").click()

            page.set_viewport_size({"width": 390, "height": 844})
            mobile = page.evaluate(
                """() => { const section=document.getElementById('financialSimulationSection');
                    return {viewport:innerWidth, sectionWidth:section.getBoundingClientRect().width,
                    overflow:section.scrollWidth > section.clientWidth + 1,
                    metricColumns:getComputedStyle(document.querySelector('.financial-simulation-metrics')).gridTemplateColumns.split(' ').length}; }"""
            )
            context.close()
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=2)

    passed = bool(
        toggle_position == {
            "sameRow": True, "immediatelyBefore": True,
            "leftOfTitle": True, "ariaExpanded": "true",
        }
        and before - after > 80
        and accessibility == {
            "expanded": "false", "controls": body_id, "hidden": True,
            "inert": True, "ariaHidden": "true",
        }
        and rapid_collapsed and rapid_expanded
        and other_top < 650 and keyboard_collapsed
        and dynamic["initiallyExpanded"] and dynamic["persisted"] and dynamic["cleaned"]
        and "browser-user-a" in dynamic["storageKey"] and persisted_after_reload
        and reduced_motion == {"hidden": False, "animations": 0, "media": True}
        and backtest["badge"] == "纸面回测" and backtest["chart"] == 1
        and "导出完整回测 JSON" in backtest["exportText"]
        and "数据限制" in backtest["text"] and "不代表未来表现" in backtest["text"]
        and report_trace and not mobile["overflow"] and mobile["metricColumns"] == 2
        and not page_errors
    )
    return {
        "check_version": "financial-simulation-browser-v1", "passed": passed,
        "browser": str(executable), "network_scope": "loopback_fixture_only",
        "layout": {"trend_top_before": before, "trend_top_after": after, "other_top_after_fold": other_top},
        "toggle_position": toggle_position,
        "accessibility": accessibility,
        "rapid_toggle": {"collapsed": rapid_collapsed, "expanded": rapid_expanded},
        "keyboard_collapsed": keyboard_collapsed, "dynamic_category": dynamic,
        "persisted_after_reload": persisted_after_reload,
        "reduced_motion": reduced_motion, "backtest": backtest,
        "report_bidirectional_trace": report_trace, "mobile": mobile,
        "calls": calls, "page_errors": page_errors,
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

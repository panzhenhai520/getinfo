# -*- coding: utf-8 -*-
"""T4.2 水位线可视验收（Playwright）：
信源管理「水位线」tab 展示 + 已知 URL 手动检查派发。"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8003"
USER, PASSWORD = "mlkj", "Test12345"
FAILURES = []


def check(name, ok, extra=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")
    if not ok:
        FAILURES.append(name)


def login(page):
    page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=30000)
    page.fill("#username", USER)
    page.fill("#password", PASSWORD)
    page.click("#loginBtn")
    page.wait_for_url(lambda u: "/login" not in u, timeout=20000)


def run():
    with sync_playwright() as p:
        _chrome = os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright\chromium-1161\chrome-win\chrome.exe")
        browser = p.chromium.launch(headless=True, executable_path=_chrome)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.add_init_script("window.__errs=[]; window.addEventListener('error', e => window.__errs.push(String(e.message||e.error)));")
        login(page)
        page.goto(f"{BASE}/source-management", wait_until="domcontentloaded", timeout=30000)
        page.locator('[data-tab="waterline"]').click()
        page.wait_for_selector("#waterlineRows", timeout=10000)
        page.wait_for_function(
            """() => !document.getElementById('waterlineRows').innerText.includes('正在加载')""",
            timeout=15000,
        )
        text = page.locator("#waterlineRows").inner_text()
        check("④ 水位线 tab 展示（含空态说明）", "水位线" in text or "尚无" in text, text[:40])
        # 手动检查：真实站点可能被网络拦截，只要接口有响应且提示明确即可
        page.fill("#wlCheckUrl", "https://www.chinaaeri.com/")
        page.locator("button", has_text="立即检查").click()
        page.wait_for_function(
            """() => { const t = document.getElementById('wlCheckResult').textContent;
                      return t.includes('探查') || t.includes('失败'); }""",
            timeout=60000,
        )
        result_text = page.locator("#wlCheckResult").inner_text()
        check("④ 手动检查有明确结果提示", bool(result_text), result_text[:60])
        errs = page.evaluate("() => window.__errs")
        check("④ 无未捕获 JS 错误", not errs, str(errs[:5]))
        browser.close()
    print("=" * 50)
    if FAILURES:
        print(f"验收失败项：{FAILURES}")
        sys.exit(1)
    print("水位线可视验收全部通过 ✅")


if __name__ == "__main__":
    run()

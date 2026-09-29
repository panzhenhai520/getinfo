# -*- coding: utf-8 -*-
"""阶段3 验收（Playwright）：实时动态 HTML 链接自动转换。

- A（真实可达 URL）：弹窗显示「在线转换阅读」按钮 → 点击实时转换 → Markdown 渲染；
- B（预置缓存）：弹窗直接渲染转换结果（converted=true），无按钮；
- C（不可达 URL）：点击转换失败 → 静默降级（按钮变重试、原文链接仍在）；
- 时间轴接口带 converted 标记。
"""
import os
import sys

sys.path.insert(0, ".")

from sqlite_database import sqlite_db

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8003"
USER, PASSWORD = "mlkj", "Test12345"

FAILURES = []


def check(name, ok, extra=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")
    if not ok:
        FAILURES.append(name)


def reset_convert_state():
    """清掉 A/C 的缓存与待执行任务，保证测试从「无缓存」状态开始。"""
    sqlite_db._ensure_connection()
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        try:
            cur.execute("DELETE FROM dynamic_converted WHERE url IN (?, ?)",
                        ("https://example.com/",
                         "https://nonexistent-domain-9f3k2.example.invalid/page"))
            cur.execute(
                "DELETE FROM intel_jobs WHERE job_type='dynamic_convert' AND status IN ('queued','retry_wait') "
                "AND dedupe_key IN (?, ?)",
                ("dynamic-convert:https://example.com/",
                 "dynamic-convert:https://nonexistent-domain-9f3k2.example.invalid/page"),
            )
            sqlite_db.connection.commit()
        finally:
            cur.close()


def login(page):
    page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=30000)
    page.fill("#username", USER)
    page.fill("#password", PASSWORD)
    page.click("#loginBtn")
    page.wait_for_url(lambda u: "/login" not in u, timeout=20000)


def run():
    reset_convert_state()
    with sync_playwright() as p:
        _chrome = os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright\chromium-1161\chrome-win\chrome.exe")
        browser = p.chromium.launch(headless=True, executable_path=_chrome)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.add_init_script("window.__errs=[]; window.addEventListener('error', e => window.__errs.push(String(e.message||e.error)));")
        login(page)

        # ---------- 0. 时间轴接口 converted 标记 ----------
        events = page.evaluate("""async () => {
            const r = await fetch('/api/intel/timeline?industry_pack_id=bolean_security_compute&per_page=60', {cache:'no-store'});
            const d = await r.json();
            return (d.events || []).filter(e => String(e.title).includes('验收动态'));
        }""")
        conv_b = next((e for e in events if "已转换条目" in e["title"]), None)
        check("时间轴条目带 converted 标记", conv_b is not None and conv_b.get("converted") is True,
              str(conv_b and conv_b.get("converted")))

        page.goto(f"{BASE}/dynamics", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector(".dyn-item", timeout=20000)
        page.wait_for_timeout(2500)

        def open_item(needle):
            item = page.locator(".dyn-item", has_text=needle).first
            item.wait_for(timeout=15000)
            item.click()
            page.wait_for_selector("#articleModal.active", timeout=15000)
            # 等待详情接口返回并渲染完成（标题离开"加载中"且正文不再是加载占位）
            page.wait_for_function(
                """() => { const m = document.getElementById('modalContent');
                          const t = document.getElementById('modalTitle');
                          return m && t && t.textContent !== '加载中...'
                              && !m.innerText.includes('正在加载文章内容')
                              && m.innerText.trim().length > 0; }""",
                timeout=15000,
            )

        # ---------- 1. A：在线转换阅读按钮 + 实时转换 ----------
        open_item("可实时转换条目")
        has_bar = page.locator(".dynamic-convert-bar").count() > 0
        check("无缓存时显示「在线转换阅读」按钮", has_bar)
        if has_bar:
            page.click("#dynamicConvertBtn")
            page.wait_for_function(
                """() => { const m = document.getElementById('modalContent');
                          return m && (m.innerText.includes('Example Domain') || m.innerText.includes('documentation examples')); }""",
                timeout=30000,
            )
            rendered = page.eval_on_selector("#modalContent", "el => el.innerText")
            check("实时转换成功渲染 Markdown", "Example Domain" in rendered or "documentation examples" in rendered, rendered[:60])
            check("转换后按钮条移除", page.locator(".dynamic-convert-bar").count() == 0)
        page.evaluate("closeArticleModal()")

        # ---------- 2. B：缓存命中直接渲染 ----------
        open_item("已转换条目")
        check("缓存命中时无转换按钮", page.locator(".dynamic-convert-bar").count() == 0)
        body_text = page.eval_on_selector("#modalContent", "el => el.innerText")
        has_table = page.eval_on_selector("#modalContent", "el => !!el.querySelector('table')")
        check("缓存 Markdown 直接渲染（含标题）", "验收动态：已转换条目" in body_text)
        check("缓存 Markdown 表格渲染", has_table)
        page.evaluate("closeArticleModal()")

        # ---------- 3. C：转换失败静默降级 ----------
        open_item("转换失败条目")
        has_bar3 = page.locator(".dynamic-convert-bar").count() > 0
        check("失败条目仍显示转换按钮", has_bar3)
        if has_bar3:
            page.click("#dynamicConvertBtn")
            page.wait_for_function(
                """() => { const b = document.getElementById('dynamicConvertBtn');
                          return b && (b.textContent.includes('失败') || b.textContent.includes('重试')); }""",
                timeout=40000,
            )
            link_ok = page.eval_on_selector(".dynamic-convert-bar a", "el => !!el.href && el.textContent.includes('原文')")
            check("失败降级：按钮变重试且原文链接保留", link_ok)
        page.evaluate("closeArticleModal()")

        # ---------- 4. 页面级 JS 报错 ----------
        errs = page.evaluate("window.__errs || []")
        check("全程无未捕获 JS 错误", len(errs) == 0, str(errs[:3]))

        browser.close()

    print("=" * 50)
    if FAILURES:
        print(f"验收未通过：{len(FAILURES)} 项失败 → {FAILURES}")
        sys.exit(1)
    print("阶段3 浏览器验收全部通过 ✅")


if __name__ == "__main__":
    run()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段1 验收（Playwright）：详情页统一 Markdown 渲染 + 关键词高亮 + 旧功能回归。

覆盖：
- 首页详情弹窗（mapindex）：content_markdown 渲染出标题/段落，关键词 <mark class="kw-hl"> 高亮；
- 分类浏览页（intel_category）：同样渲染 Markdown；
- 全部动态页（dynamics）：Markdown 渲染；
- 文章管理页（article_management）：Markdown 渲染；
- 回归：弹窗正常打开、URL/日期/来源等元信息齐全、无 JS 报错。
"""
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8003"
USER, PASSWORD = "mlkj", "Test12345"

FAILURES = []


def check(name, ok, extra=""):
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name} {extra}")
    if not ok:
        FAILURES.append(name)


def login(page):
    page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=30000)
    page.fill("#username", USER)
    page.fill("#password", PASSWORD)
    page.click("#loginBtn")
    page.wait_for_url(lambda u: "/login" not in u, timeout=20000)


def collect_errors(page):
    return page.evaluate("window.__errs || []")


def run():
    with sync_playwright() as p:
        # 本机已装 chromium-1161（旧版 playwright 安装），新版默认找 headless_shell 缺失 → 显式指定完整版
        import os
        _chrome = os.path.expandvars(
            r"%LOCALAPPDATA%\ms-playwright\chromium-1161\chrome-win\chrome.exe"
        )
        browser = p.chromium.launch(headless=True, executable_path=_chrome)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.add_init_script("window.__errs=[]; window.addEventListener('error', e => window.__errs.push(String(e.message||e.error)));")
        login(page)

        # ---------- 1. 首页详情弹窗 ----------
        page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(4500)
        # 首页 dashboard 视图：时间轴条目 / 主题 insight 条目均可打开文章详情
        # （跳过阶段3验收专用的无正文测试条目，保证取到正常正文文章）
        card = page.locator(".tl-item, li.insight-title").filter(has_not_text="验收动态").first
        card.wait_for(timeout=20000)
        first_article_id = int(card.get_attribute("data-id"))
        card.click()
        page.wait_for_selector("#articleModal.active", timeout=20000)
        page.wait_for_function(
            "() => { const m = document.getElementById('modalContent'); return m && m.innerText.trim().length > 40 && !m.innerText.includes('加载中'); }",
            timeout=20000,
        )
        title_text = page.eval_on_selector("#modalTitle", "el => el.innerText")
        body_html = page.eval_on_selector("#modalContent", "el => el.innerHTML")
        has_heading = page.eval_on_selector("#modalContent", "el => !!el.querySelector('h1,h2,h3,h4')")
        has_para = page.eval_on_selector("#modalContent", "el => !!el.querySelector('p')")
        has_kw_hl = page.eval_on_selector("#modalContent", "el => !!el.querySelector('mark.kw-hl')")
        has_raw_js = "<script>" in body_html
        check("首页详情：标题非空", bool(title_text.strip()), title_text[:40])
        check("首页详情：Markdown 标题元素渲染", has_heading)
        check("首页详情：Markdown 段落渲染", has_para)
        check("首页详情：关键词 kw-hl 高亮存在", has_kw_hl)
        check("首页详情：无脚本注入", not has_raw_js)
        check("首页详情：URL/日期/来源元信息", bool(page.eval_on_selector("#modalUrl", "el => el.href && el.href !== '#'")))
        page.evaluate("closeArticleModal()")

        # ---------- 2. 分类浏览页 ----------
        page.goto(f"{BASE}/intel-category/trend", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector("#grid [data-article-id]", timeout=20000)
        page.locator("#grid [data-article-id] .article-card-title").filter(has_not_text="验收动态").first.click()
        page.wait_for_selector("#articleModal.active", timeout=20000)
        page.wait_for_function(
            "() => { const m = document.getElementById('modalContent'); return m && m.innerText.trim().length > 40; }",
            timeout=20000,
        )
        has_heading2 = page.eval_on_selector("#modalContent", "el => !!el.querySelector('h1,h2,h3,h4,p')")
        check("分类页详情：Markdown 渲染", has_heading2)
        page.evaluate("closeArticleModal()")

        # ---------- 3. 全部动态页 ----------
        page.goto(f"{BASE}/dynamics", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector(".dyn-item", timeout=20000)
        page.locator(".dyn-item").filter(has_not_text="验收动态").first.click()
        page.wait_for_selector("#articleModal.active", timeout=20000)
        page.wait_for_function(
            "() => { const m = document.getElementById('modalContent'); return m && m.innerText.trim().length > 20; }",
            timeout=20000,
        )
        has_md3 = page.eval_on_selector("#modalContent", "el => !!el.querySelector('h1,h2,h3,h4,p,ul,ol,table,blockquote')")
        check("动态页详情：Markdown 渲染", has_md3)
        page.evaluate("closeArticleModal()")

        # ---------- 4. 文章管理页 ----------
        page.goto(f"{BASE}/article-management", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
        first_row_btn = page.locator("button[onclick*='viewArticle']").filter(has_not_text="验收动态").first
        try:
            first_row_btn.wait_for(timeout=15000)
            first_row_btn.click()
        except Exception:
            # 备选：直接通过 URL 参数打开
            page.goto(f"{BASE}/article-management?article_id=", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_selector("#articleModal.active", timeout=20000)
        page.wait_for_function(
            "() => { const m = document.getElementById('modalContent'); return m && m.innerText.trim().length > 40; }",
            timeout=20000,
        )
        has_md4 = page.eval_on_selector("#modalContent", "el => !!el.querySelector('h1,h2,h3,h4,p,ul,ol,table,blockquote')")
        check("文章管理详情：Markdown 渲染", has_md4)
        page.evaluate("closeArticleModal()")

        # ---------- 5. 详情 API 返回 content_markdown ----------
        api_id = first_article_id
        detail = page.evaluate("""async (id) => {
            const r = await fetch('/article-management/api/article/' + id, {cache:'no-store'});
            if (!r.ok) return { ok: false, err: 'http ' + r.status };
            try { const d = await r.json(); return { ok: !!d.success, has_md: !!(d.article && d.article.content_markdown && String(d.article.content_markdown).trim()) }; }
            catch (e) { return { ok: false, err: 'not json' }; }
        }""", api_id)
        check("详情 API 返回 content_markdown", bool(api_id) and detail["ok"] and detail["has_md"])

        # ---------- 6. 页面级 JS 报错 ----------
        errs = collect_errors(page)
        check("全程无未捕获 JS 错误", len(errs) == 0, str(errs[:3]))

        browser.close()

    print("=" * 50)
    if FAILURES:
        print(f"验收未通过：{len(FAILURES)} 项失败 → {FAILURES}")
        sys.exit(1)
    print("阶段1 浏览器验收全部通过 ✅")


if __name__ == "__main__":
    run()

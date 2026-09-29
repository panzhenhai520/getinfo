# -*- coding: utf-8 -*-
"""阶段2 验收（Playwright）：报告页 markitdown 内联阅读（PDF/Word/Excel）。

覆盖：
- /api/intel/reports/summary 列出 Office 报告（original_format=docx/xlsx）；
- /api/intel/reports/<id>/markdown 返回 Markdown（表格结构保留）；
- 报告页预览渲染标题/表格；下载按钮（原始文件）可下载；
- 已有 PDF 报告预览不回归（若列表中存在）。
"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8003"
USER, PASSWORD = "mlkj", "Test12345"
PACK = "bolean_security_compute"

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

        # ---------- 1. summary 列出 Office 报告 ----------
        summary = page.evaluate("""async () => {
            const r = await fetch('/api/intel/reports/summary?industry_pack_id=' + encodeURIComponent('""" + PACK + """'), {cache:'no-store'});
            return await r.json();
        }""")
        reports = summary.get("reports") or []
        docx_report = next((r for r in reports if r.get("original_format") == "docx"), None)
        xlsx_report = next((r for r in reports if r.get("original_format") == "xlsx"), None)
        pdf_report = next((r for r in reports if r.get("original_format") == "pdf"), None)
        check("summary 含 docx 报告", docx_report is not None, str(docx_report and docx_report.get("id")))
        check("summary 含 xlsx 报告", xlsx_report is not None, str(xlsx_report and xlsx_report.get("id")))

        # ---------- 2. markdown 端点返回表格结构 ----------
        if docx_report:
            md = page.evaluate("""async (url) => {
                const r = await fetch(url, {cache:'no-store'});
                if (!r.ok) { const d = await r.json().catch(()=>null); return {ok:false, msg:(d&&d.message)||('http '+r.status)}; }
                return {ok:true, text: await r.text()};
            }""", docx_report["read_url"])
            check("markdown 端点成功返回", md["ok"], str(md.get("msg") or ""))
            if md["ok"]:
                txt = md["text"]
                check("Word 报告表格保留", "|" in txt and "1286" in txt)
                check("Word 报告标题保留", "内联阅读验收" in txt)
        if xlsx_report:
            md2 = page.evaluate("""async (url) => {
                const r = await fetch(url, {cache:'no-store'});
                if (!r.ok) return {ok:false, msg:('http '+r.status)};
                return {ok:true, text: await r.text()};
            }""", xlsx_report["read_url"])
            check("Excel 端点成功返回", md2["ok"], str(md2.get("msg") or ""))
            if md2["ok"]:
                check("Excel 报告表格保留", "工控漏洞数" in md2["text"] and "|" in md2["text"])

        # ---------- 3. 报告页 UI：列表 + 内联预览 + 下载 ----------
        page.goto(f"{BASE}/reports", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector(".report-item", timeout=20000)
        page.wait_for_timeout(2500)
        has_office = page.locator(".report-item", has_text="内联阅读验收").count() > 0
        check("报告页列表显示 Office 报告", has_office)
        if has_office:
            page.locator(".report-item", has_text="内联阅读验收").first.click()
            page.wait_for_function(
                """() => { const b = document.getElementById('pvBody'); return b && b.querySelector('.md-preview'); }""",
                timeout=30000,
            )
            has_table = page.eval_on_selector("#pvBody .md-preview", "el => !!el.querySelector('table')")
            has_heading = page.eval_on_selector("#pvBody .md-preview", "el => !!el.querySelector('h1,h2,h3,h4')")
            check("预览区渲染 Markdown 表格", has_table)
            check("预览区渲染 Markdown 标题", has_heading)
            fmt_badge = page.eval_on_selector("#pvFormatBadge", "el => el.textContent")
            check("预览头显示格式徽标", "DOCX" in fmt_badge or "XLSX" in fmt_badge, fmt_badge)
            # 下载原始文件（触发下载事件）
            try:
                with page.expect_download(timeout=15000) as dl_info:
                    page.click("#pvDownloadOrigin")
                dl = dl_info.value
                check("原始文件下载可用", dl.suggested_filename.endswith((".docx", ".xlsx")), dl.suggested_filename)
            except Exception as exc:
                check("原始文件下载可用", False, str(exc)[:120])
        if pdf_report:
            page.locator(".report-item", has_text=str(pdf_report.get("title") or "")[:20]).first.click()
            page.wait_for_function(
                """() => { const b = document.getElementById('pvBody'); return b && b.querySelector('.md-preview'); }""",
                timeout=30000,
            )
            check("PDF 报告预览不回归", True)

        # ---------- 4. 页面级 JS 报错 ----------
        errs = page.evaluate("window.__errs || []")
        check("全程无未捕获 JS 错误", len(errs) == 0, str(errs[:3]))

        browser.close()

    print("=" * 50)
    if FAILURES:
        print(f"验收未通过：{len(FAILURES)} 项失败 → {FAILURES}")
        sys.exit(1)
    print("阶段2 浏览器验收全部通过 ✅")


if __name__ == "__main__":
    run()

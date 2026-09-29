# -*- coding: utf-8 -*-
"""上一批 4 项改动验收（Playwright + requests）：

ui 模式：
  ① 参考抽屉文章标题点击 → 打开项目文章详情弹窗（不开新网页）
  ② 趋势页主题词展开列表的文章标题 → 链接指向 /article-management?article_id=（项目详情页）
  ③ 信源管理「AI 周报」tab：提示词加载/保存/恢复默认/测试派发
  ④ 邮箱重复注册 → 中文提示「邮箱已注册，请勿重复注册」
view 模式（在 worker 跑完 pack_report 后执行）：
  ③ 首页「报告」页显示 AI 周报并可预览 Markdown
"""
import json
import os
import sys

import requests
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8003"
USER, PASSWORD = "mlkj", "Test12345"
ADMIN, ADMIN_PASSWORD = "admin", "admin123"
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


def open_chat(page):
    page.locator("#chatToggle").click()
    page.wait_for_selector("#chatDrawer.open", timeout=10000)


def ask(page, question, wait_done=180000):
    page.wait_for_function(
        """() => { const i = document.getElementById('chatInput'); return i && !i.disabled; }""",
        timeout=20000,
    )
    page.locator("#chatInput").fill(question)
    page.click("#chatForm .send-button")
    try:
        page.wait_for_selector("#modelSelectOverlay:not([hidden])", timeout=3000)
        page.locator(".model-select-item").first.click()
    except Exception:
        pass
    try:
        page.wait_for_function(
            """() => { const s = document.getElementById('chatStatus');
                      const i = document.getElementById('chatInput');
                      return s && !s.textContent.trim() && i && !i.disabled; }""",
            timeout=wait_done,
        )
    except Exception:
        pass


def item1_reference_drawer(page):
    """① 参考抽屉：点击文章标题打开项目详情弹窗。"""
    page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)
    open_chat(page)
    ask(page, "网络安全防护有哪些常见手段？")
    # 等待召回触发条出现（有库内文章时）
    try:
        page.wait_for_selector(".retrieval-trigger.open button", timeout=120000)
        check("① 召回触发条出现（本次回答参考的库内文章）", True)
    except Exception:
        check("① 召回触发条出现（本次回答参考的库内文章）", False, "（本次问答未召回文章，抽屉无法验证）")
        return
    page.locator(".retrieval-trigger.open button").click()
    page.wait_for_selector("#retrievalDrawer.open", timeout=8000)
    page.wait_for_selector("#retrievalBody a", timeout=8000)
    links = page.locator("#retrievalBody a").count()
    check("① 抽屉内有参考文章条目", links > 0, f"共 {links} 条")
    pages_before = len(page.context.pages)
    page.locator("#retrievalBody a").first.click()
    page.wait_for_timeout(1500)
    pages_after = len(page.context.pages)
    modal_open = page.evaluate(
        """() => { const m = document.getElementById('articleModal'); return !!m && m.classList.contains('active'); }"""
    )
    check("① 点击标题没有新开网页标签", pages_after == pages_before, f"{pages_before} -> {pages_after}")
    check("① 点击标题打开项目文章详情弹窗", bool(modal_open))


def item2_trend_inline(page):
    """② 趋势页展开列表的标题链接 → /article-management?article_id=。"""
    page.goto(f"{BASE}/trends", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_selector("#intelTrendReadings", timeout=20000)
    # 本地无趋势数据：注入一行测试条目 + 拦截 articles API，直接验收渲染代码路径
    page.route(
        "**/api/intel/trends/articles**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "success": True,
                "articles": [{
                    "article_id": 12, "title": "测试文章详情跳转",
                    "domain": "example.com", "publish_date": "2026-09-17",
                    "url": "https://example.com/raw/12",
                }],
                "total": 1, "total_pages": 1,
            }, ensure_ascii=False),
        ),
    )
    page.evaluate(
        """() => {
            const box = document.getElementById('intelTrendReadings');
            const row = document.createElement('div');
            row.className = 'intel-trend-reading';
            row.setAttribute('data-kw', '__test_kw');
            row.innerHTML = '<span class="intel-trend-reading-kw">测试词</span>' +
                '<button type="button" class="intel-trend-content-toggle"><i class="fa fa-list"></i> 内容</button>' +
                '<div class="intel-trend-reading-articles" hidden></div>';
            box.appendChild(row);
        }"""
    )
    page.locator(".intel-trend-content-toggle").first.click()
    page.wait_for_selector(".intel-trend-inline-title", timeout=10000)
    href = page.locator(".intel-trend-inline-title").first.get_attribute("href")
    target = page.locator(".intel-trend-inline-title").first.get_attribute("target")
    check("② 趋势展开文章标题指向项目详情页", href == "/article-management?article_id=12", f"href={href}")
    check("② 趋势展开文章标题不新开网页", not target, f"target={target}")
    page.unroute("**/api/intel/trends/articles**")


def item3_report_settings(page):
    """③ 信源管理 AI 周报 tab：加载/保存/恢复默认/测试派发。"""
    page.goto(f"{BASE}/source-management", wait_until="domcontentloaded", timeout=30000)
    page.locator('[data-tab="reports"]').click()
    page.wait_for_selector("#reportPrompt", timeout=10000)
    # 提示词与最近报告信息都是异步加载，等它们就绪再断言
    page.wait_for_function(
        """() => { const p = document.getElementById('reportPrompt');
                  const l = document.getElementById('reportLastInfo');
                  return p && p.value.length > 0 && l && !l.textContent.includes('正在加载'); }""",
        timeout=15000,
    )
    default_prompt = page.locator("#reportPrompt").input_value()
    check("③ AI 周报 tab 显示默认提示词", "行业周报" in default_prompt and "行业热点" in default_prompt,
          f"长度 {len(default_prompt)}")
    last_info = page.locator("#reportLastInfo").inner_text()
    check("③ 显示最近一次报告生成信息", "生成" in last_info or "尚未" in last_info, last_info[:40])
    # 保存自定义提示词
    page.locator("#reportPrompt").fill("自定义提示词测试：{行业名称} {时间窗}")
    page.locator("button", has_text="保存提示词").click()
    page.wait_for_function(
        """() => document.getElementById('reportPromptResult').textContent.includes('已保存')""",
        timeout=10000,
    )
    page.reload()
    page.locator('[data-tab="reports"]').click()
    page.wait_for_selector("#reportPrompt", timeout=10000)
    page.wait_for_function(
        """() => document.getElementById('reportPrompt').value === '自定义提示词测试：{行业名称} {时间窗}'""",
        timeout=15000,
    )
    saved = page.locator("#reportPrompt").input_value()
    check("③ 保存的提示词重新加载一致", saved == "自定义提示词测试：{行业名称} {时间窗}")
    # 恢复默认并保存
    page.locator("button", has_text="恢复默认").click()
    page.wait_for_function(
        """() => document.getElementById('reportPrompt').value.includes('行业周报')""",
        timeout=10000,
    )
    restored = page.locator("#reportPrompt").input_value()
    check("③ 恢复默认提示词", "行业周报" in restored)
    page.locator("button", has_text="保存提示词").click()
    page.wait_for_function(
        """() => document.getElementById('reportPromptResult').textContent.includes('已保存')""",
        timeout=10000,
    )
    # 测试派发
    page.locator("button", has_text="测试提示词效果").click()
    page.wait_for_function(
        """() => { const t = document.getElementById('reportPromptResult').textContent;
                  return t.includes('任务') || t.includes('派发'); }""",
        timeout=15000,
    )
    status = page.locator("#reportPromptResult").inner_text()
    check("③ 测试提示词效果派发任务", ("任务" in status) or ("排队" in status), status[:60])


def item4_duplicate_email():
    """④ 邮箱重复注册 → 中文提示。"""
    s = requests.Session()
    r = s.post(f"{BASE}/api/user/login", json={"username": ADMIN, "password": ADMIN_PASSWORD})
    ok_login = r.status_code == 200 and r.json().get("success")
    check("④ 管理员登录（注册场景）", ok_login)
    if not ok_login:
        return
    created_id = None
    try:
        r = s.post(f"{BASE}/api/pack-users", json={
            "industry_pack_id": PACK, "username": "accdup1", "password": "Test12345",
            "email": "acc-dup-test@example.com",
        })
        d = r.json()
        check("④ 首次注册邮箱成功", d.get("success"), str(d)[:80])
        created_id = d.get("user_id")
        r = s.post(f"{BASE}/api/pack-users", json={
            "industry_pack_id": PACK, "username": "accdup2", "password": "Test12345",
            "email": "acc-dup-test@example.com",
        })
        d = r.json()
        check("④ 重复邮箱返回中文提示", d.get("message") == "邮箱已注册，请勿重复注册", str(d)[:100])
    finally:
        if created_id:
            try:
                s.delete(f"{BASE}/api/pack-users/{created_id}")
            except Exception:
                pass


def view_reports_page(page):
    """view 模式：首页「报告」页列出 AI 周报并可预览。"""
    page.goto(f"{BASE}/reports", wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_selector(".report-item", timeout=20000)
        titles = page.evaluate(
            """() => [...document.querySelectorAll('.report-item .title')].map(e => e.textContent.trim())"""
        )
        check("③ 报告页显示 AI 周报列表", len(titles) > 0, "；".join(titles[:3]))
        page.locator(".report-item").first.click()
        page.wait_for_function(
            """() => { const b = document.getElementById('pvBody');
                      return b && b.querySelector('.md-preview') && b.innerText.length > 100; }""",
            timeout=15000,
        )
        body_text = page.locator("#pvBody").inner_text()
        check("③ 周报 Markdown 预览渲染", "周报" in body_text or "本周" in body_text or "热点" in body_text,
              body_text[:60])
    except Exception as exc:
        check("③ 报告页显示 AI 周报列表", False, f"{exc}")


def run():
    mode = sys.argv[1] if len(sys.argv) > 1 else "ui"
    with sync_playwright() as p:
        _chrome = os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright\chromium-1161\chrome-win\chrome.exe")
        browser = p.chromium.launch(headless=True, executable_path=_chrome)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.add_init_script("window.__errs=[]; window.addEventListener('error', e => window.__errs.push(String(e.message||e.error)));")
        login(page)
        if mode == "view":
            view_reports_page(page)
        elif mode == "item3":
            item3_report_settings(page)
            item4_duplicate_email()
        else:
            item1_reference_drawer(page)
            item2_trend_inline(page)
            item3_report_settings(page)
            item4_duplicate_email()
            errs = page.evaluate("() => window.__errs")
            check("无未捕获 JS 错误", not errs, str(errs[:5]))
        browser.close()
    print("=" * 50)
    if FAILURES:
        print(f"验收失败项：{FAILURES}")
        sys.exit(1)
    print("验收全部通过 ✅")


if __name__ == "__main__":
    run()

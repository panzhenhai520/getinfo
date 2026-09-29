# -*- coding: utf-8 -*-
"""历史会话 ÷/× 新语义验收（Playwright）：

- 手机端 +−÷× 四个按钮全部可见（× 不再被截断）
- 删除确认弹窗精简：「确定删除？」+ 是/否，且不被窄列挤扁
- ÷ 执行前不弹确认；相关会话 → 生成《公约：xxx》新会话、原会话删除；无关 → 原会话未动
- × 执行前不弹确认；相关会话 → 生成《整合：xxx》新会话、原会话删除
"""
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


def open_chat(page):
    page.locator("#chatToggle").click()
    page.wait_for_selector("#chatDrawer.open", timeout=10000)


def open_history(page):
    if page.locator("#chatDrawer.open").count() == 0:
        open_chat(page)
    if page.locator("#chatDrawer.history-open").count() == 0:
        page.locator("#toggleHistory").click()
    page.wait_for_selector(".session-tools", timeout=10000)
    # 等历史列表加载完成（有会话项或"暂无历史会话"提示），避免竞态读到空列表
    page.wait_for_function(
        """() => { const list = document.getElementById('sessionList');
                  return list && (list.querySelectorAll('.session-item').length > 0 || list.innerText.includes('暂无历史会话')); }""",
        timeout=20000,
    )


def session_names(page):
    return page.evaluate("""() => [...document.querySelectorAll('#sessionList .session-item strong')].map(el => el.textContent.trim())""")


def ask(page, question, wait_done=180000):
    page.wait_for_function(
        """() => { const i = document.getElementById('chatInput'); return i && !i.disabled; }""",
        timeout=20000,
    )
    page.locator("#chatInput").fill(question)
    page.click("#chatForm .send-button")
    # 若弹出模型选择（新会话首问），选第一个模型
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


def select_sessions(page, names):
    """按显示名勾选会话（点击复选框，避免触发 selectSession）。"""
    for name in names:
        item = page.locator("#sessionList .session-item", has_text=name).first
        item.locator("input[type=checkbox]").check()


def run():
    with sync_playwright() as p:
        _chrome = os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright\chromium-1161\chrome-win\chrome.exe")
        browser = p.chromium.launch(headless=True, executable_path=_chrome)

        # ── 桌面端准备：建立两个相关会话 ──
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.add_init_script("window.__errs=[]; window.addEventListener('error', e => window.__errs.push(String(e.message||e.error)));")
        login(page)
        page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3500)
        page.evaluate("() => { try { localStorage.setItem('chatModelDefault', 'local'); } catch(e){} }")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(3500)
        open_chat(page)

        # ── 清空历史会话，保证断言精确（分批删除，等待勾选异步完成） ──
        open_history(page)
        for _round in range(6):
            if page.locator("#sessionList .session-item").count() == 0:
                break
            page.locator("#selectAllSessions").check()
            # 勾选是按动画帧分批异步完成的，等全部复选框落定再点删除
            page.wait_for_function(
                """() => { const items = document.querySelectorAll('#sessionList .session-item').length;
                          const checked = document.querySelectorAll('#sessionList .session-item input:checked').length;
                          return items > 0 && checked === items; }""",
                timeout=15000,
            )
            page.locator("#deleteSessions").click()
            try:
                page.wait_for_selector(".history-processing", timeout=8000)
                page.locator(".history-processing button", has_text="是").click()
                page.wait_for_selector(".history-processing", state="detached", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
        page.locator("#newSession").click()
        page.wait_for_timeout(1200)
        ask(page, "网络安全是什么？")
        open_history(page)
        page.locator("#newSession").click()
        page.wait_for_timeout(1200)
        ask(page, "网络安全防护有哪些常见手段？")

        # ── 桌面 ÷ 无确认、产出公约新会话 ──
        open_history(page)
        names = session_names(page)
        related = [n for n in names if "网络安全" in n][:2]
        check("桌面端已有两个相关会话", len(related) >= 2, str(names[:6]))
        if len(related) >= 2:
            select_sessions(page, related)
            page.locator("#cleanupSession").click()
            # 不应出现确认弹窗
            confirm_appeared = page.locator(".history-processing").count() > 0
            check("÷ 执行前不弹确认", not confirm_appeared)
            page.wait_for_function(
                """() => { const s = document.getElementById('chatStatus');
                          return s && s.textContent.trim() && !s.textContent.includes('正在求公约'); }""",
                timeout=300000,
            )
            status = page.eval_on_selector("#chatStatus", "el => el.textContent")
            new_names = session_names(page)
            if "公约完成" in status:
                check("÷ 生成公约新会话（原会话已删除）",
                      all(n not in new_names for n in related), f"status={status[:60]} names={new_names[:6]}")
            else:
                check("÷ 无关时原会话未动", all(n in new_names for n in related), f"status={status[:60]}")

        # ── 桌面 × 无确认、产出整合新会话 ──
        open_history(page)
        names = session_names(page)
        related = [n for n in names if "网络安全" in n][:2]
        if len(related) < 2:
            # ÷ 已把原会话合并删除 → 再造两个同主题会话供 × 使用
            page.locator("#newSession").click()
            page.wait_for_timeout(1000)
            ask(page, "工控安全的核心挑战有哪些？")
            open_history(page)
            page.locator("#newSession").click()
            page.wait_for_timeout(1000)
            ask(page, "工控安全体系如何建设？")
            open_history(page)
            names = session_names(page)
            related = [n for n in names if "工控安全" in n][:2]
        check("× 前有两个相关会话", len(related) >= 2, str(related))
        if len(related) >= 2:
            select_sessions(page, related)
            page.locator("#synthesizeSessions").click()
            confirm_appeared = page.locator(".history-processing").count() > 0
            check("× 执行前不弹确认", not confirm_appeared)
            # 等待状态离开「正在整合…」即认为运算结束（成功/无关/失败都可能）
            page.wait_for_function(
                """() => { const s = document.getElementById('chatStatus');
                          return s && s.textContent.trim() && !s.textContent.includes('正在整合'); }""",
                timeout=420000,
            )
            status = page.eval_on_selector("#chatStatus", "el => el.textContent")
            new_names = session_names(page)
            if "整合完成" in status:
                check("× 生成整合新会话（原会话已删除）",
                      all(n not in new_names for n in related), f"status={status[:60]} names={new_names[:6]}")
            else:
                check("× 无关/失败时原会话未动", all(n in new_names for n in related), f"status={status[:60]}")

        # ── 桌面删除确认弹窗：精简「确定删除？」+ 是/否 ──
        open_history(page)
        names = session_names(page)
        if names:
            select_sessions(page, [names[0]])
            page.locator("#deleteSessions").click()
            page.wait_for_selector(".history-processing", timeout=5000)
            dialog_text = page.locator(".history-processing").inner_text()
            check("删除弹窗文案精简（确定删除？/是/否）",
                  "确定删除？" in dialog_text and "是" in dialog_text and "否" in dialog_text,
                  dialog_text[:80])
            page.locator(".history-processing button", has_text="否").click()
            page.wait_for_selector(".history-processing", state="detached", timeout=5000)
            check("点「否」取消删除", True)

        errs = page.evaluate("window.__errs || []")
        check("桌面端无未捕获 JS 错误", len(errs) == 0, str(errs[:3]))
        ctx.close()

        # ── 手机端：+−÷× 四个按钮全部可见 ──
        mctx = browser.new_context(viewport={"width": 390, "height": 844})
        mpage = mctx.new_page()
        login(mpage)
        mpage.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=30000)
        mpage.wait_for_timeout(3500)
        open_chat(mpage)
        open_history(mpage)
        visibility = mpage.evaluate("""() => {
            const btns = ['newSession', 'deleteSessions', 'cleanupSession', 'synthesizeSessions'];
            const out = {};
            btns.forEach(id => {
                const el = document.getElementById(id);
                const r = el.getBoundingClientRect();
                out[id] = { visible: r.width > 0 && r.right <= window.innerWidth + 1, left: Math.round(r.left) };
            });
            return out;
        }""")
        all_visible = all(item["visible"] for item in visibility.values())
        check("⑧ 手机端 +−÷× 四个按钮全部可见", all_visible, str(visibility))
        mctx.close()

        browser.close()

    print("=" * 50)
    if FAILURES:
        print(f"验收未通过：{len(FAILURES)} 项 → {FAILURES}")
        sys.exit(1)
    print("历史会话 ÷/× 新语义验收全部通过 ✅")


if __name__ == "__main__":
    run()

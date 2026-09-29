# -*- coding: utf-8 -*-
"""补充验收：①勾选"记住默认选择"后跨会话不再弹 ②mini 聊天抽屉召回触发条/无 JS 报错。"""
import os
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8003"
FAIL = []


def check(name, ok, extra=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")
    if not ok:
        FAIL.append(name)


with sync_playwright() as p:
    _chrome = os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright\chromium-1161\chrome-win\chrome.exe")
    browser = p.chromium.launch(headless=True, executable_path=_chrome)
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.add_init_script("window.__errs=[]; window.addEventListener('error', e => window.__errs.push(String(e.message||e.error)));")
    page.goto(f"{BASE}/login", wait_until="domcontentloaded")
    page.fill("#username", "mlkj")
    page.fill("#password", "Test12345")
    page.click("#loginBtn")
    page.wait_for_url(lambda u: "/login" not in u, timeout=20000)
    page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3500)
    page.evaluate("() => { try { localStorage.removeItem('chatModelDefault'); } catch(e){} }")
    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(3500)
    page.locator("#chatToggle").click()
    page.wait_for_selector("#chatDrawer.open", timeout=10000)

    # 打开模型弹窗 → 勾选记住 → 选模型
    page.locator("#chatModelSelect").click()
    page.wait_for_selector("#modelSelectOverlay:not([hidden])", timeout=5000)
    page.check("#modelRememberDefault")
    page.locator(".model-select-item").first.click()
    stored = page.evaluate("() => localStorage.getItem('chatModelDefault')")
    check("① 勾选记住后写入 localStorage", bool(stored), str(stored))
    # 等待完成发送
    try:
        page.wait_for_function(
            """() => { const s = document.getElementById('chatStatus'); return s && !s.textContent.trim(); }""",
            timeout=120000,
        )
    except Exception:
        pass

    # 新建会话（通过历史面板的"新会话"按钮）→ 发送不应再弹
    page.locator("#toggleHistory").click()
    page.wait_for_timeout(1200)
    page.locator("#newSession").click()
    page.wait_for_timeout(1500)
    page.locator("#chatInput").fill("记住默认后的新会话问题")
    page.click("#chatForm .send-button")
    try:
        page.wait_for_selector("#modelSelectOverlay:not([hidden])", timeout=3000)
        popped = True
    except Exception:
        popped = False
    check("① 勾选记住后新会话发送不再弹模型选择", not popped)
    try:
        page.wait_for_function(
            """() => { const s = document.getElementById('chatStatus'); return s && !s.textContent.trim(); }""",
            timeout=120000,
        )
    except Exception:
        pass

    # ── mini 聊天（/dynamics）：无 JS 错误、召回抽屉标记存在、身份问题无参考文章 ──
    page.goto(f"{BASE}/dynamics", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_selector("#miniChatFab", timeout=15000)
    page.click("#miniChatFab")
    page.wait_for_selector("#miniChatDrawer.open", timeout=10000)
    page.fill("#miniChatInput", "你是什么模型？")
    page.click(".mini-chat-send")
    try:
        page.wait_for_function(
            """() => { const s = document.getElementById('miniChatStatus'); return s && !s.textContent.trim(); }""",
            timeout=120000,
        )
    except Exception:
        pass
    mini_trigger = page.locator(".mini-chat-recall-trigger").count()
    check("⑨ mini聊天身份问题无参考文章触发条", mini_trigger == 0)
    mini_errs = page.evaluate("window.__errs || []")
    check("mini聊天无未捕获 JS 错误", len(mini_errs) == 0, str(mini_errs[:3]))

    browser.close()

print("=" * 50)
print("补充验收" + ("全部通过 ✅" if not FAIL else f"失败 {len(FAIL)} 项 → {FAIL}"))

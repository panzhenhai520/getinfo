# -*- coding: utf-8 -*-
"""AI 助手聊天界面 9 点优化验收（Playwright）：

①选模型弹窗关闭不得隐藏会话窗口；会话内首次发送才弹
②直接点模型选择后关闭也不得隐藏会话窗口
③清空按钮为[-]且在输入框内（不在发送右侧）；发送前有模型选择框；弹窗含"记住默认选择"
④模态 ×/取消 均可关闭
⑤无会话ID小串数字
⑥召回改为右侧滑出抽屉
⑦联网搜索图标为地球
⑧手机布局：输入行两行、无横向溢出
⑨身份类问题不出现"本次回答参考库内文章"；实质问题召回精确文章
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


def run():
    with sync_playwright() as p:
        _chrome = os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright\chromium-1161\chrome-win\chrome.exe")
        browser = p.chromium.launch(headless=True, executable_path=_chrome)

        # ============ 桌面端 ============
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.add_init_script("window.__errs=[]; window.addEventListener('error', e => window.__errs.push(String(e.message||e.error)));")
        login(page)
        page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3500)
        page.evaluate("() => { try { localStorage.removeItem('chatModelDefault'); } catch(e){} }")
        page.reload(wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3500)

        # ⑤ 会话ID不显示
        has_session_id = page.evaluate("""() => {
            const bad = document.getElementById('activeSession');
            const hex = [...document.querySelectorAll('#chatDrawer span, #chatDrawer div')]
                .some(el => /^[0-9a-f]{8}$/.test((el.textContent || '').trim()));
            return !bad && !hex;
        }""")
        check("⑤ 会话ID小串不再显示", has_session_id)

        # 打开会话窗口
        open_chat(page)

        # ③ 清空按钮为[-]，位于输入框内（不在发送右侧）
        clear_btn = page.locator(".chat-input-clear").first
        clear_ok = clear_btn.count() > 0 and clear_btn.inner_text().strip() == "−"
        clear_inside_box = page.evaluate("""() => {
            const btn = document.querySelector('.chat-input-clear');
            const box = document.querySelector('.chat-input-box');
            const send = document.querySelector('#chatForm .send-button');
            if (!btn || !box || !send) return false;
            const inBox = box.contains(btn);
            const btnX = btn.getBoundingClientRect().x;
            const sendX = send.getBoundingClientRect().x;
            return inBox && btnX < sendX;   // 不在发送按钮右侧
        }""")
        check("③ 清空按钮为[-]且在输入框内", clear_ok and clear_inside_box)

        # ③ 发送前有模型选择框
        model_select = page.locator("#chatModelSelect")
        check("③ 发送前可见模型选择框", model_select.count() > 0 and model_select.is_visible())

        # ③ 点模型选择框 → 弹窗含"记住默认选择"
        model_select.click()
        page.wait_for_selector("#modelSelectOverlay:not([hidden])", timeout=5000)
        check("③ 弹窗含记住默认勾选", page.locator("#modelRememberDefault").count() > 0)

        # ④ × 关闭弹窗
        page.click("#modelSelectClose")
        page.locator("#modelSelectOverlay").wait_for(state="hidden", timeout=5000)
        check("④ 点×关闭弹窗", True)

        # ①/② 关闭弹窗后会话窗口仍在
        check("①/② 关闭弹窗后会话窗口未隐藏", page.locator("#chatDrawer.open").count() > 0)

        # ④ 取消关闭弹窗（先打开再取消）
        model_select.click()
        page.wait_for_selector("#modelSelectOverlay:not([hidden])", timeout=5000)
        page.click("#modelSelectCancel")
        page.locator("#modelSelectOverlay").wait_for(state="hidden", timeout=5000)
        check("④ 点取消关闭弹窗", True)
        check("② 直接点模型选择再取消，会话窗口未隐藏", page.locator("#chatDrawer.open").count() > 0)

        # ⑦ 地球联网图标
        check("⑦ 联网搜索为地球图标", page.locator("#onlineSearchToggle i.fa-globe").count() > 0)

        # ① 会话第一次发送弹模型选择；选中后发送；第二次发送不再弹
        input_box = page.locator("#chatInput")
        input_box.fill("你好")
        page.click("#chatForm .send-button")
        page.wait_for_selector("#modelSelectOverlay:not([hidden])", timeout=8000)
        check("① 首次发送弹出模型选择", True)
        page.locator(".model-select-item").first.click()
        # 选中即发送 → 等待进入流式（等待状态行变化或用户消息出现）
        page.wait_for_function(
            """() => { const m = document.querySelectorAll('#messages .message.user'); return m.length >= 1; }""",
            timeout=15000,
        )
        check("① 选中模型后自动发送", True)
        # 等本次回答结束（状态行清空）
        try:
            page.wait_for_function(
                """() => { const s = document.getElementById('chatStatus'); return s && !s.textContent.trim(); }""",
                timeout=120000,
            )
        except Exception:
            pass
        # 第二次发送：不应再弹
        input_box.fill("今天天气怎么样")
        page.click("#chatForm .send-button")
        try:
            page.wait_for_selector("#modelSelectOverlay:not([hidden])", timeout=3000)
            popped_again = True
        except Exception:
            popped_again = False
        check("① 同会话第二次发送不再弹模型选择", not popped_again)
        # 若没弹，说明第二次发送已开始；等待结束后继续
        try:
            page.wait_for_function(
                """() => { const s = document.getElementById('chatStatus'); return s && !s.textContent.trim(); }""",
                timeout=120000,
            )
        except Exception:
            pass

        # ⑥/⑨ 实质问题：出现召回触发条 → 右侧抽屉
        input_box.fill("网络安全领域最近有什么动态")
        page.click("#chatForm .send-button")
        trigger = page.locator("#retrievalSources.open button")
        try:
            trigger.wait_for(timeout=60000)
            trigger_ok = True
        except Exception:
            trigger_ok = False
        check("⑥ 实质问题出现召回触发条", trigger_ok)
        if trigger_ok:
            trigger.click()
            page.wait_for_selector("#retrievalDrawer.open", timeout=5000)
            links = page.locator("#retrievalBody a").count()
            check("⑥ 召回内容从右侧抽屉滑出", links > 0, f"links={links}")
            page.click("#retrievalClose")
            page.wait_for_function(
                """() => { const d = document.getElementById('retrievalDrawer'); return d && !d.classList.contains('open'); }""",
                timeout=5000,
            )
            check("⑥ 抽屉可关闭", True)
            # 抽屉不占输入区常驻空间：触发条只占一行
            trigger_height = trigger.evaluate("el => el.offsetHeight")
            check("⑥ 触发条只占一行不占空间", trigger_height <= 44, f"h={trigger_height}")
        try:
            page.wait_for_function(
                """() => { const s = document.getElementById('chatStatus'); return s && !s.textContent.trim(); }""",
                timeout=120000,
            )
        except Exception:
            pass

        # ⑨ 身份类问题：不应出现召回触发条
        input_box.fill("你是什么模型？")
        page.click("#chatForm .send-button")
        try:
            page.wait_for_function(
                """() => { const s = document.getElementById('chatStatus'); return s && !s.textContent.trim(); }""",
                timeout=120000,
            )
        except Exception:
            pass
        identity_trigger = page.locator("#retrievalSources.open button").count()
        check("⑨ 身份类问题不显示参考文章", identity_trigger == 0)
        # 回答文本里没有参考文章标记
        last_answer = page.evaluate("""() => {
            const msgs = [...document.querySelectorAll('#messages .message.answer')];
            return msgs.length ? msgs[msgs.length-1].innerText : '';
        }""")
        check("⑨ 回答无假召回内容", "参考的库内文章" not in last_answer, last_answer[:60])

        errs = page.evaluate("window.__errs || []")
        check("桌面端无未捕获 JS 错误", len(errs) == 0, str(errs[:3]))
        ctx.close()

        # ============ 手机端布局 ============
        mctx = browser.new_context(viewport={"width": 390, "height": 844})
        mpage = mctx.new_page()
        login(mpage)
        mpage.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=30000)
        mpage.wait_for_timeout(3500)
        open_chat(mpage)
        layout = mpage.evaluate("""() => {
            const drawer = document.getElementById('chatDrawer');
            const row = document.querySelector('.chat-input-row');
            const input = document.querySelector('.chat-input-box');
            const model = document.getElementById('chatModelSelect');
            const send = document.querySelector('#chatForm .send-button');
            const globe = document.getElementById('onlineSearchToggle');
            if (!drawer || !row || !input || !model || !send || !globe) return {ok:false};
            const rect = el => el.getBoundingClientRect();
            const noHScroll = drawer.scrollWidth <= drawer.clientWidth + 1;
            const secondRow = model.offsetTop > input.offsetTop;      // 模型选择在第二行
            const inView = rect(model).bottom <= window.innerHeight && rect(send).bottom <= window.innerHeight;
            return { ok: noHScroll && secondRow && inView,
                     modelTop: Math.round(rect(model).top), inputTop: Math.round(rect(input).top),
                     drawerW: drawer.clientWidth, vw: window.innerWidth };
        }""")
        check("⑧ 手机输入行两行排布无横向溢出", bool(layout.get("ok")), str(layout))
        mpage.locator("#chatModelSelect").click()
        mpage.wait_for_selector("#modelSelectOverlay:not([hidden])", timeout=5000)
        mpage.click("#modelSelectCancel")
        check("⑧ 手机模型选择弹窗可关闭且会话不隐藏", mpage.locator("#chatDrawer.open").count() > 0)
        mctx.close()

        browser.close()

    print("=" * 50)
    if FAILURES:
        print(f"验收未通过：{len(FAILURES)} 项 → {FAILURES}")
        sys.exit(1)
    print("AI 助手聊天界面 9 点优化验收全部通过 ✅")


if __name__ == "__main__":
    run()

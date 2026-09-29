# -*- coding: utf-8 -*-
"""阶段4 验收（Playwright + 服务集成）：信源「检查」一键学习 + 爬取自动应用。

- UI：信源管理页来源行点「检查学习」→ 状态提示学到 N 条；
- API：/api/intel/sources/check-learn 对真实站点学习成功（≥3 条、平均标题长度达标）；
- 模型列表接口可见 active 模型；
- 爬取应用：ListPageScanner 扫描该站时返回 source_method=site_scraper_model 的条目；
- 模型删除后自动回退启发式（scanner 返回普通候选）。
"""
import os
import sys

sys.path.insert(0, ".")

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8003"
USER, PASSWORD = "mlkj", "Test12345"
LEARN_URL = "https://news.ycombinator.com/"

FAILURES = []


def check(name, ok, extra=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")
    if not ok:
        FAILURES.append(name)


def login(page, username=USER, password=PASSWORD):
    page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=30000)
    page.fill("#username", username)
    page.fill("#password", password)
    page.click("#loginBtn")
    page.wait_for_url(lambda u: "/login" not in u, timeout=20000)


def ensure_accept_admin():
    """本地验收用管理员账号（打包管理页 admin-only；仅本地测试环境创建）。"""
    from user_database import UserDatabase
    db = UserDatabase()
    db.connect()
    user_id = db.create_user("phase4_accept_admin", "Accept12345", full_name="阶段4验收管理员", role="admin")
    if not user_id:
        # 已存在 → 重置口令为已知值
        cur = db.connection.cursor()
        cur.execute(
            "UPDATE users SET password_hash=?, is_active=TRUE, role='admin' WHERE username='phase4_accept_admin'",
            (db._hash_password("Accept12345"),),
        )
        db.connection.commit()
        cur.close()
    return "phase4_accept_admin", "Accept12345"


def run():
    admin_user, admin_password = ensure_accept_admin()
    with sync_playwright() as p:
        _chrome = os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright\chromium-1161\chrome-win\chrome.exe")
        browser = p.chromium.launch(headless=True, executable_path=_chrome)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.add_init_script("window.__errs=[]; window.addEventListener('error', e => window.__errs.push(String(e.message||e.error)));")
        login(page)

        # ---------- 1. check-learn API：真实站点一键学习 ----------
        learn = page.evaluate("""async (url) => {
            const r = await fetch('/api/intel/sources/check-learn', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({url}),
            });
            return await r.json();
        }""", LEARN_URL)
        ok_learn = learn.get("success") and learn.get("sample_count", 0) >= 3
        check("检查学习 API 成功（≥3 条样例）", ok_learn,
              f"sample_count={learn.get('sample_count')}, avg={learn.get('title_avg_len')}")
        if ok_learn:
            check("样例标题平均长度达标", learn.get("title_avg_len", 0) >= 6)

        # ---------- 2. 模型列表可见 ----------
        models = page.evaluate("""async () => {
            const r = await fetch('/api/intel/sources/scraper-models', {cache:'no-store'});
            const d = await r.json();
            return d.models || [];
        }""")
        site_key = "news.ycombinator.com"
        learned = next((m for m in models if m.get("site_key") == site_key), None)
        check("模型列表含已学站点", learned is not None and learned.get("status") == "active",
              str(learned and learned.get("status")))

        # ---------- 3. 爬取自动应用 + 删除回退（服务级，本地库直连） ----------
        from intel_light_scanner import ListPageScanner
        from site_scraper_models import delete_site_model, extract_with_model
        from sqlite_database import sqlite_db
        scanner = ListPageScanner()
        try:
            items = scanner.scan({"source_url": LEARN_URL, "metadata": {}}, limit=10)
            from_model = [item for item in items if item.get("source_method") == "site_scraper_model"]
            check("爬取自动应用模型提取", len(from_model) >= 3,
                  f"model_items={len(from_model)}/{len(items)}")
            # 删除模型 → 回退启发式
            delete_site_model(sqlite_db, site_key)
            fallback = scanner.scan({"source_url": LEARN_URL, "metadata": {}}, limit=10)
            check("删除模型后回退启发式", len(fallback) >= 3
                  and all(item.get("source_method") != "site_scraper_model" for item in fallback),
                  f"fallback_items={len(fallback)}")
        except Exception as exc:
            check("服务级扫描不抛异常", False, str(exc)[:160])

        # ---------- 4. UI：信源管理页「检查学习」按钮（管理员会话） ----------
        ctx2 = browser.new_context(viewport={"width": 1440, "height": 900})
        page2 = ctx2.new_page()
        login(page2, admin_user, admin_password)
        page2.goto(f"{BASE}/panython/admin", wait_until="domcontentloaded", timeout=30000)
        page2.wait_for_selector(".pack-card", timeout=20000)
        page2.locator(".pack-card").first.click()
        page2.wait_for_timeout(1500)
        page2.click("button[data-tab='sources']")
        page2.wait_for_timeout(800)
        rows = page2.locator("#sources .source-row")
        if rows.count() == 0:
            try:
                page2.locator("button:has-text('＋ 添加信源')").first.click()
                page2.wait_for_timeout(600)
            except Exception:
                pass
        rows = page2.locator("#sources .source-row")
        check("来源行存在", rows.count() > 0, f"rows={rows.count()}")
        if rows.count():
            row = rows.first
            row.locator("input[data-key='url']").fill(LEARN_URL)
            row.locator("button.source-learn-button").click()
            try:
                page2.wait_for_function(
                    "() => { const el = document.getElementById('status'); return el && el.textContent.includes('检查学习'); }",
                    timeout=45000,
                )
                status = page2.eval_on_selector("#status", "el => el.textContent || ''")
            except Exception:
                status = page2.eval_on_selector("#status", "el => el.textContent || ''")
            check("检查学习按钮触发状态提示", "检查学习" in status and "学到" in status, status[:140])
        else:
            check("检查学习按钮触发状态提示", False, "无来源行")
        ctx2.close()

        # ---------- 5. 页面级 JS 报错 ----------
        errs = page.evaluate("window.__errs || []")
        check("全程无未捕获 JS 错误", len(errs) == 0, str(errs[:3]))

        browser.close()

    print("=" * 50)
    if FAILURES:
        print(f"验收未通过：{len(FAILURES)} 项失败 → {FAILURES}")
        sys.exit(1)
    print("阶段4 浏览器验收全部通过 ✅")


if __name__ == "__main__":
    run()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建期自检：两个浏览器都必须真的能启动，否则镜像不许出厂。

为什么需要（2026-10-08 生产事故）：
A 机镜像里 playwright 是 1.40.0、它期望 /ms-playwright/chromium-1091，
但镜像里只有 patchright 的 chromium-1234 —— 于是所有走原生 playwright 的抓取
（267 个 website 信源依赖这条路）**一秒内失败**，累计 1588 次扫描记录被浪费，
而构建过程完全没有任何报错。这里把"能不能启动"变成构建的门槛：
启动不了 → 构建失败 → 根本发不出去。

用法（Dockerfile 里调用）：python tools/check_browsers.py
"""
from __future__ import annotations

import sys

ENGINES = (
    # (显示名, 模块路径)；playwright 与 patchright 各自管理自己的浏览器版本
    ("playwright", "playwright.sync_api"),
    ("patchright", "patchright.sync_api"),
)

failed = []
for name, module_path in ENGINES:
    try:
        module = __import__(module_path, fromlist=["sync_playwright"])
        sync_playwright = module.sync_playwright
    except Exception as exc:
        failed.append(f"{name}: 模块导入失败 {type(exc).__name__}: {exc}")
        print(f"❌ {name}: 模块导入失败 {type(exc).__name__}: {str(exc)[:120]}")
        continue
    try:
        with sync_playwright() as play:
            browser = play.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                print(f"✅ {name}: 浏览器可启动，版本 {browser.version}")
            finally:
                browser.close()
    except Exception as exc:
        # 典型错误信息："Executable doesn't exist at /ms-playwright/chromium-1091/..."
        failed.append(f"{name}: 启动失败 {type(exc).__name__}: {str(exc)[:160]}")
        print(f"❌ {name}: 启动失败 {type(exc).__name__}: {str(exc)[:200]}")

if failed:
    print("\n浏览器自检未通过，禁止打包该镜像：")
    for item in failed:
        print("  -", item)
    print("修复：在 Dockerfile 里补 `python -m playwright install chromium` / "
          "`python -m patchright install chromium`，并确认 PLAYWRIGHT_BROWSERS_PATH 指向同一目录。")
    raise SystemExit(1)
print("\n浏览器自检通过。")

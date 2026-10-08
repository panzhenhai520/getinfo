# -*- coding: utf-8 -*-

"""T4.3 反爬处理宪法（2026-10-08 按产品决策修订）。

降级链（统一决策，任何新增抓取路径都必须走这里）：
  1. 常规解析（限速、退避、robots 尊重、条件请求）；
  2. 动态列表页 → VPN run(mode='list')；
  3. 详情页解析失败/403/验证码 → VPN OCR（截图→OCR→LLM，extraction_method='vpn_ocr'）；
  4. 仍失败 → 记录 last_error，停止硬攻，等下一轮。
反爬状态码（403/406/412/429/503 等）自动对域名降频退避（指数退避窗口）。

【当前边界（产品负责人 2026-10-08 决策，取代原「绝不启用隐身/指纹伪装/验证码破解」表述）】
原表述与代码现状早已不符（cloudflare_bypass 一直在轮换 curl_cffi 浏览器指纹；
Scrapling 的隐身后端就是 patchright——一个专门去除自动化痕迹的 Chromium），
按现状重新划界如下：

  ✅ 允许：
    · 隐身浏览器渲染（Scrapling/patchright，即现有本地第一梯队）；
    · HTTP/TLS 指纹（curl_cffi impersonate）；
    · Cloudflare 挑战自动通行（Scrapling solve_cloudflare）；
    · 页面改版自愈（adaptive selector）。
  ⛔ 仍不允许：
    · 轮换 IP 绕过封禁（被封就降频等待，靠已有的指数退避 + 信源级放弃止损）；
    · 加载第三方对抗类浏览器插件。
  ⚠️ 人机验证（reCAPTCHA / hCaptcha / 极验 等）：
    产品口径为「只要不违反 robots.txt，能破就破」。但技术上目前无实现路径——
    这些需要真实人工行为或付费打码服务，项目内没有也不打算内置；
    antibot_detector 仍把它们判为「需要人工」并停止重试（因为没有可用手段，
    空跑只是浪费爬取槽位）。若将来引入付费打码服务，需单独评估成本与合规。

  robots 说明：robots.txt 与验证码是两回事——robots 是爬虫礼貌约定，
  验证码是技术访问控制。本项目现状是「第一层尊重 robots，被 robots 拒绝时
  走 VPN+OCR 兜底（该路径不检查 robots）」，此处如实记录，不再声称"尊重 robots"。

2026-10-07 补充：antibot_detector.py + config/antibot_rules.json 用来**识别是谁拦了我们**
并据此止损（跳过无效引擎、把一直抓不到的信源从轮询里摘出去、把爬取槽位还给能出正文的信源）：
  · JS 传感器型（DataDome/Kasada/PerimeterX/瑞数…）用于跳过无效的 curl_cffi 梯队；
  · stealth_self_check() 只做指纹漏点自检，用于发现问题。
"""

import time
from typing import Dict, Optional

# 与 intel_light_scanner 的反爬判定口径保持一致
ANTI_BOT_STATUS_CODES = frozenset({403, 406, 412, 429, 503})
# 连续失败次数 → 退避窗口（秒），指数递增封顶 24h
_BACKOFF_WINDOWS = (60, 300, 1800, 10800, 43200, 86400)


def ensure_crawl_domain_backoff_table(cursor) -> None:
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS crawl_domain_backoff ("
        "  domain TEXT PRIMARY KEY,"
        "  fail_count INTEGER NOT NULL DEFAULT 0,"
        "  last_status INTEGER NOT NULL DEFAULT 0,"
        "  backoff_until TEXT NOT NULL DEFAULT '',"
        "  updated_at TEXT NOT NULL DEFAULT ''"
        ")"
    )


def classify_anti_bot(status_code=None, error_text: str = "") -> bool:
    """是否命中反爬信号（状态码或错误文本）。"""
    if status_code in ANTI_BOT_STATUS_CODES:
        return True
    text = str(error_text or "")
    return any(token in text.casefold() for token in
               ("403", "429", "forbidden", "captcha", "验证码", "访问过于频繁", "anti-bot", "antibot"))


def record_backoff(domain: str, status_code: int = 403, db=None) -> Dict:
    """记录一次反爬命中：失败计数 +1，退避窗口指数递增。"""
    if db is None:
        from sqlite_database import sqlite_db
        db = sqlite_db
    domain = str(domain or "").strip().lower()
    if not domain:
        return {"backoff": False}
    from utils import get_china_time
    from datetime import timedelta
    now = get_china_time()
    db._ensure_connection()
    with db.lock:
        cursor = db.connection.cursor()
        try:
            ensure_crawl_domain_backoff_table(cursor)
            row = cursor.execute(
                "SELECT fail_count FROM crawl_domain_backoff WHERE domain=?", (domain,)
            ).fetchone()
            fail_count = int(row["fail_count"]) + 1 if row else 1
            window = _BACKOFF_WINDOWS[min(fail_count - 1, len(_BACKOFF_WINDOWS) - 1)]
            until = (now + timedelta(seconds=window)).strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute(
                "INSERT INTO crawl_domain_backoff(domain, fail_count, last_status, backoff_until, updated_at)"
                " VALUES(?,?,?,?,?) "
                "ON CONFLICT(domain) DO UPDATE SET fail_count=excluded.fail_count,"
                " last_status=excluded.last_status, backoff_until=excluded.backoff_until,"
                " updated_at=excluded.updated_at",
                (domain, fail_count, int(status_code or 403), until,
                 now.strftime("%Y-%m-%d %H:%M:%S")),
            )
            db.connection.commit()
            return {"backoff": True, "domain": domain, "fail_count": fail_count,
                    "backoff_seconds": window, "backoff_until": until}
        finally:
            cursor.close()


def record_backoff_success(domain: str, db=None) -> None:
    """一次成功抓取 → 清空该域名退避计数（验证站点恢复）。"""
    if db is None:
        from sqlite_database import sqlite_db
        db = sqlite_db
    domain = str(domain or "").strip().lower()
    if not domain:
        return
    db._ensure_connection()
    with db.lock:
        cursor = db.connection.cursor()
        try:
            ensure_crawl_domain_backoff_table(cursor)
            cursor.execute(
                "UPDATE crawl_domain_backoff SET fail_count=0, backoff_until='', updated_at=? WHERE domain=?",
                (time.strftime("%Y-%m-%d %H:%M:%S"), domain),
            )
            db.connection.commit()
        finally:
            cursor.close()


def should_backoff(domain: str, db=None) -> bool:
    """该域名当前是否处于退避窗口（退避期间不再发起请求，避免以量压反爬）。"""
    if db is None:
        from sqlite_database import sqlite_db
        db = sqlite_db
    domain = str(domain or "").strip().lower()
    if not domain:
        return False
    from utils import get_china_time
    now_text = get_china_time().strftime("%Y-%m-%d %H:%M:%S")
    db._ensure_connection()
    with db.lock:
        cursor = db.connection.cursor()
        try:
            ensure_crawl_domain_backoff_table(cursor)
            row = cursor.execute(
                "SELECT backoff_until FROM crawl_domain_backoff WHERE domain=?", (domain,)
            ).fetchone()
            if not row:
                return False
            return bool(str(row["backoff_until"] or "")[:19] > now_text[:19])
        finally:
            cursor.close()


def route_fallback_decision(status_code=None, error_text: str = "", *, have_remote: bool = True) -> str:
    """降级链决策：常规失败后下一步走哪。

    返回：'vpn_list'（列表页交 VPN）| 'vpn_ocr'（详情页交 VPN OCR）| 'give_up'（停止硬攻）。
    """
    if not classify_anti_bot(status_code=status_code, error_text=error_text):
        return "give_up"  # 非反爬类失败：记录后停止，等下一轮
    return "vpn_list" if have_remote else "vpn_ocr"

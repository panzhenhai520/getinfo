# -*- coding: utf-8 -*-

"""T4.3 反爬处理宪法：简单朴实、不绕开反爬。

降级链（统一决策，任何新增抓取路径都必须走这里）：
  1. 常规解析（限速、退避、robots 尊重、条件请求）；
  2. 动态列表页 → VPN run(mode='list')；
  3. 详情页解析失败/403/验证码 → VPN OCR（截图→OCR→LLM，extraction_method='vpn_ocr'）；
  4. 仍失败 → 记录 last_error，停止硬攻，等下一轮。
反爬状态码（403/406/412/429/503 等）自动对域名降频退避（指数退避窗口），
绝不开发/启用隐身、指纹伪装、验证码破解、代理轮换等绕过手段。
patchright 只作普通浏览器渲染（新代码不得加载对抗类插件）。
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

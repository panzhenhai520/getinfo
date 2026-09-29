#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段4：信源「检查」一键学习（AutoScraper 简化版）。

流程：输入 URL → 抓列表页（静态 requests，必要时 Playwright 取 HTML）→
      自动挑选样例（最长的一组标题/链接节点）→ AutoScraper 学出 (标题, 链接) 模板 →
      自动保存模型（按站点归一化域名，DB 表 + 模型文件）→ 爬取列表页时自动应用。

边界：
- 学习质量门槛：至少 3 条样例且标题平均长度 ≥6 字，否则提示改用人工栏目配置；
- 动态页：静态抓取失败时用 Playwright 取渲染后 HTML 再喂 AutoScraper（html= 参数）；
- 模型失效自动检测：连续 2 次提取 0 条 → 标记失效并回退启发式，绝不阻塞爬取主链路；
- SSRF 防护复用 intel_http.validate_external_url。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from intel_http import SafeHTTPClient, validate_external_url
from utils import get_china_time

MIN_SAMPLES = 3                 # 至少学到 3 条样例
MAX_SAMPLES = 20                # 最多取 20 条样例学习
TITLE_MIN_LEN = 4               # 标题长度下限（字符）
TITLE_MAX_LEN = 80              # 标题长度上限（字符）
MIN_AVG_TITLE_LEN = 6           # 标题平均长度门槛
INVALID_STREAK_LIMIT = 2        # 连续提取 0 条的失效阈值
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def ensure_site_scraper_models_table(cursor):
    """site_scraper_models 表（幂等）：按站点归一化域名存模型文件路径与状态。"""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS site_scraper_models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_key TEXT NOT NULL UNIQUE,
            model_path TEXT NOT NULL DEFAULT '',
            sample_count INTEGER NOT NULL DEFAULT 0,
            title_avg_len REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            invalid_streak INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            learned_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_site_scraper_models_status ON site_scraper_models(status)"
    )


def site_key_of(url: str) -> str:
    """按站点归一化：小写主机名（去 www.），不带端口。"""
    host = (urlsplit(str(url or "")).hostname or "").casefold()
    return host[4:] if host.startswith("www.") else host


def _model_directory() -> Path:
    directory = Path(os.getenv("INTEL_SITE_MODEL_DIR", "data/site_scraper_models"))
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _title_link_candidates(url: str, html: str) -> list:
    """启发式挑选样例：取页面上「最长的一组标题/链接节点」。

    先收集全部同站、标题长度合理的锚点；再按「包含最多候选锚点的祖先容器」分组，
    选最大一组作为学习样例——真实栏目列表（ul/li、div 列表）自然胜出，导航条被排除。
    """
    from collections import defaultdict
    soup = BeautifulSoup(html or "", "html.parser")
    source_host = (urlsplit(url).hostname or "").casefold()
    anchors = []
    seen = set()
    for anchor in soup.select("a[href]"):
        target = urljoin(url, anchor.get("href") or "")
        parsed = urlsplit(target)
        host = (parsed.hostname or "").casefold()
        if parsed.scheme not in {"http", "https"} or not host:
            continue
        if host != source_host and not host.endswith(f".{source_host}"):
            continue
        title = " ".join(anchor.get_text(" ", strip=True).split())
        if not (TITLE_MIN_LEN <= len(title) <= TITLE_MAX_LEN):
            continue
        identity = target.split("#", 1)[0]
        if identity in seen:
            continue
        seen.add(identity)
        # href_raw：学习用原始 href（AutoScraper 按 href 属性原始值匹配）；target 为绝对地址供最终输出
        anchors.append((anchor, title, target, str(anchor.get("href") or "")))

    def _ancestors(node, depth=6):
        out = []
        for _ in range(depth):
            node = node.parent
            if node is None:
                break
            if getattr(node, "name", None) in (None, "html", "body", "[document]"):
                continue
            out.append(node)
        return out

    # 每个候选容器包含多少候选锚点
    container_counts = defaultdict(int)
    for anchor, _title, _target, _href in anchors:
        for container in _ancestors(anchor):
            container_counts[id(container)] += 1
    # 每个锚点归入包含锚点最多的祖先容器
    grouped = defaultdict(list)
    for anchor, title, target, href_raw in anchors:
        containers = _ancestors(anchor)
        if not containers:
            continue
        best = max(containers, key=lambda container: container_counts[id(container)])
        grouped[id(best)].append({"title": title, "url": target, "href_raw": href_raw})
    if not grouped:
        return []
    best_group = max(grouped.values(), key=len)
    return best_group[: MAX_SAMPLES * 2]


def fetch_list_html(url: str, *, timeout: int = 20) -> str:
    """抓列表页 HTML：静态请求优先，反爬/失败时 Playwright 渲染兜底。"""
    validate_external_url(url)
    try:
        result = SafeHTTPClient().get(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
        text = result.content.decode(result.encoding or "utf-8", errors="replace")
        if len(text.strip()) > 200:
            return text
    except Exception:
        pass
    # 静态请求拿不到（403/412/超时/空壳）→ Playwright 真浏览器渲染
    try:
        from crawler_resource_manager import sync_playwright_slot
        from playwright.sync_api import sync_playwright
        with sync_playwright_slot(f"site-learn:{url}"):
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
                try:
                    context = browser.new_context(user_agent=USER_AGENT)
                    page = context.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(1500)
                    return page.content()
                finally:
                    browser.close()
    except Exception:
        pass
    raise ValueError("列表页抓取失败（静态请求与浏览器渲染均未取得内容）")


def _build_scraper_model(html: str, candidates: list):
    """AutoScraper 学出 (title, url) 模板并验证可复用，返回 (scraper, 样例列表)。

    AutoScraper 的规则 id 是自动生成的（rule_xxx），这里按「首条值匹配」把
    title/url 两类规则分别打上别名，后续可用 group_by_alias=True 直接取分组结果。
    """
    from autoscraper import AutoScraper
    samples = candidates[:MAX_SAMPLES]
    titles = [item["title"] for item in samples]
    # 学习用原始 href（AutoScraper 按 href 属性原始值匹配，绝对 URL 学不出 url 规则）
    urls = [item.get("href_raw") or item["url"] for item in samples]
    scraper = AutoScraper()
    scraper.build(html=html, wanted_dict={"title": titles, "url": urls})
    result = scraper.get_result_similar(html=html, grouped=True)
    if isinstance(result, dict):
        aliases = {}
        for rule_id, values in result.items():
            values = list(values or [])
            if not values:
                continue
            first = str(values[0])
            if first == titles[0] and "title" not in aliases.values():
                aliases[rule_id] = "title"
            elif first == urls[0] and "url" not in aliases.values():
                aliases[rule_id] = "url"
        if "title" in aliases.values() and "url" in aliases.values():
            try:
                scraper.set_rule_aliases(aliases)
            except Exception:
                pass
    # 学习后立即自检：同一 HTML 上能提取回 ≥MIN_SAMPLES 条才算学成
    check = scraper.get_result_similar(html=html, grouped=True)
    got = 0
    if isinstance(check, dict):
        got = max(len(v or []) for v in check.values()) if check else 0
    elif isinstance(check, list):
        got = len(check)
    if got < MIN_SAMPLES:
        raise ValueError("模板自检未通过，该页结构不支持自动学习，请改用人工栏目配置")
    return scraper, samples


def learn_site_model(db, url: str, html: str | None = None) -> dict:
    """输入 URL 一键学习：抓取 → 选样例 → 学模板 → 保存。返回学习摘要。"""
    validate_external_url(url)
    if html is None:
        html = fetch_list_html(url)
    candidates = _title_link_candidates(url, html)
    if len(candidates) < MIN_SAMPLES:
        raise ValueError("该页结构不支持自动学习（标题型链接不足），请改用人工栏目配置")
    avg_len = round(sum(len(item["title"]) for item in candidates[:MAX_SAMPLES]) / min(len(candidates), MAX_SAMPLES), 1)
    if avg_len < MIN_AVG_TITLE_LEN:
        raise ValueError("该页结构不支持自动学习（标题平均长度过短），请改用人工栏目配置")
    scraper, samples = _build_scraper_model(html, candidates)
    site_key = site_key_of(url)
    model_path = _model_directory() / f"{site_key}.json"
    scraper.save(str(model_path))
    now = get_china_time().strftime("%Y-%m-%d %H:%M:%S")
    db._ensure_connection()
    with db.lock:
        db.connection.execute(
            """
            INSERT INTO site_scraper_models(site_key, model_path, sample_count, title_avg_len,
                                            status, invalid_streak, last_error, learned_at, updated_at)
            VALUES (?, ?, ?, ?, 'active', 0, '', ?, ?)
            ON CONFLICT(site_key) DO UPDATE SET
                model_path=excluded.model_path,
                sample_count=excluded.sample_count,
                title_avg_len=excluded.title_avg_len,
                status='active',
                invalid_streak=0,
                last_error='',
                learned_at=excluded.learned_at,
                updated_at=excluded.updated_at
            """,
            (site_key, str(model_path), len(samples), avg_len, now, now),
        )
        db.connection.commit()
    return {
        "site_key": site_key,
        "sample_count": len(samples),
        "title_avg_len": avg_len,
        "status": "active",
        "samples": samples,
    }


def get_site_model(db, site_key: str):
    """读取某站点处于 active 状态的模型行；无/失效返回 None。"""
    try:
        db._ensure_connection()
        with db.lock:
            row = db.connection.execute(
                "SELECT * FROM site_scraper_models WHERE site_key=? AND status='active'",
                (site_key,),
            ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def _record_miss(db, site_key: str, error: str):
    """提取 0 条：累计失效次数；连续 ≥2 次 → 标记失效（此后回退启发式）。"""
    try:
        db._ensure_connection()
        now = get_china_time().strftime("%Y-%m-%d %H:%M:%S")
        with db.lock:
            db.connection.execute(
                """
                UPDATE site_scraper_models
                SET invalid_streak = invalid_streak + 1,
                    last_error = ?,
                    status = CASE WHEN invalid_streak + 1 >= ? THEN 'invalid' ELSE status END,
                    updated_at = ?
                WHERE site_key = ?
                """,
                (str(error or "")[:300], INVALID_STREAK_LIMIT, now, site_key),
            )
            db.connection.commit()
    except Exception:
        pass


def extract_with_model(db, url: str, html: str, *, limit: int = 0) -> list:
    """爬列表页前先用该站点已学模型提取标题+链接。

    返回 [] 表示未命中/模型失效/提取为空 → 调用方自动回退现有启发式（绝不阻塞主链路）。
    成功条目带 source_method='site_scraper_model'。
    """
    site_key = site_key_of(url)
    row = get_site_model(db, site_key)
    if not row or not html:
        return []
    model_path = str(row.get("model_path") or "")
    if not model_path or not Path(model_path).exists():
        _record_miss(db, site_key, "模型文件缺失")
        return []
    try:
        from autoscraper import AutoScraper
        scraper = AutoScraper()
        scraper.load(model_path)
        # 优先按别名分组（学习时已打 title/url 别名）；旧模型退化为扁平列表半切
        result = scraper.get_result_similar(html=html, grouped=True, group_by_alias=True)
        if isinstance(result, dict) and result.get("title") and result.get("url"):
            # group_by_alias 会把多条同类别规则的提取结果合并（可能重复）→ 保序去重
            def _dedupe(values):
                out, seen = [], set()
                for value in values or []:
                    value = str(value or "").strip()
                    if value and value not in seen:
                        seen.add(value)
                        out.append(value)
                return out
            titles = _dedupe(result.get("title"))
            urls = _dedupe(result.get("url"))
        else:
            flat = scraper.get_result_similar(html=html, grouped=False)
            flat = list(flat or [])
            half = len(flat) // 2
            titles, urls = list(flat[:half]), list(flat[half:])
    except Exception as exc:
        _record_miss(db, site_key, f"模型加载/提取异常: {type(exc).__name__}")
        return []
    items = []
    seen = set()
    for title, target in zip(titles, urls):
        title = " ".join(str(title or "").split())
        target = str(target or "").strip()
        if not title or not target:
            continue
        absolute = urljoin(url, target)
        if not absolute.startswith(("http://", "https://")):
            continue
        identity = absolute.split("#", 1)[0]
        if identity in seen:
            continue
        seen.add(identity)
        items.append({"title": title, "url": absolute, "source_method": "site_scraper_model"})
        if limit and len(items) >= int(limit):
            break
    if not items:
        _record_miss(db, site_key, "模型提取 0 条")
        return []
    # 命中成功 → 清零失效计数
    try:
        db._ensure_connection()
        with db.lock:
            db.connection.execute(
                "UPDATE site_scraper_models SET invalid_streak=0, last_error='', updated_at=? WHERE site_key=?",
                (get_china_time().strftime("%Y-%m-%d %H:%M:%S"), site_key),
            )
            db.connection.commit()
    except Exception:
        pass
    return items


def delete_site_model(db, site_key: str) -> bool:
    """删除站点模型（此后爬取自动回退启发式）。"""
    row = get_site_model(db, site_key)
    if row:
        try:
            Path(str(row.get("model_path") or "")).unlink(missing_ok=True)
        except OSError:
            pass
    try:
        db._ensure_connection()
        with db.lock:
            db.connection.execute("DELETE FROM site_scraper_models WHERE site_key=?", (site_key,))
            db.connection.commit()
        return True
    except Exception:
        return False


def list_site_models(db):
    try:
        db._ensure_connection()
        with db.lock:
            rows = db.connection.execute(
                "SELECT * FROM site_scraper_models ORDER BY updated_at DESC, id DESC LIMIT 200"
            ).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        return []

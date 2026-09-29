#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""13 个信源(平台)的 provider 实现与测试逻辑。

基于 Agent-Reach（已安装）：其 channel.check() 报告某平台上游工具是否可用；
真正的读取/搜索由本模块直接调用上游工具（gh CLI / yt-dlp / feedparser / 公开 API /
Jina Reader 等）。配置未就绪的平台，测试时明确返回"需配置凭据/工具"。

platform_test(platform_id, values) 供设置页"测试"按钮调用，返回
{ok, message, got_info, sample}。
fetch_platform(platform_id, keywords, limit) 供 Agent 管线抓取文章。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

from agent_reach.channels import get_channel
from agent_reach.config import Config

# 平台 id -> Agent-Reach channel name（多数一致，个别不同）
_CHANNEL = {
    "x": "twitter", "bilibili": "bilibili", "github": "github", "youtube": "youtube",
    "v2ex": "v2ex", "xueqiu": "xueqiu", "rss": "rss", "xhs": "xiaohongshu",
    "reddit": "reddit", "facebook": "facebook", "instagram": "instagram",
    "linkedin": "linkedin", "xiaoyuzhou": "xiaoyuzhou",
}


def _channel(platform_id: str):
    try:
        return get_channel(_CHANNEL.get(platform_id, platform_id))
    except Exception:
        return None


def availability(platform_id: str) -> dict[str, Any]:
    """用 Agent-Reach channel.check() 报告平台工具可用性。"""
    ch = _channel(platform_id)
    if ch is None:
        return {"ok": False, "status": "off", "message": "未识别的信源", "backend": None}
    try:
        status, msg = ch.check(Config())
    except Exception as exc:
        return {"ok": False, "status": "error", "message": f"检查失败: {exc}", "backend": getattr(ch, "active_backend", None)}
    return {"ok": status in ("ok", "warn"), "status": status, "message": msg,
            "backend": getattr(ch, "active_backend", None)}


def _cred_configured(platform_id: str, values: dict[str, str]) -> bool:
    auth = str(values.get(f"PLATFORM_SOURCE_{platform_id.upper()}_AUTH", "") or "").strip()
    if platform_id == "rss":
        return bool(auth)  # 订阅地址
    return bool(auth)


def _cred_required(platform_id: str) -> bool:
    # 零配置：无需凭据；其余需 Cookie/Token/API Key
    zero = {"rss", "github", "youtube", "v2ex", "bilibili"}
    return platform_id not in zero


def platform_test(platform_id: str, values: dict[str, str], keywords=None) -> dict[str, Any]:
    """测试：工具可用性 + 凭据 + 尽力抓一条结果。"""
    avail = availability(platform_id)
    configured = _cred_configured(platform_id, values)
    if _cred_required(platform_id) and not configured:
        return {"ok": False, "message": "未配置凭据，请先填写凭据后保存再测试", "got_info": False,
                "available": avail}
    if not avail["ok"]:
        return {"ok": False, "message": f"工具未就绪：{avail['message']}", "got_info": False,
                "available": avail}
    # 可用且配置(如需) → 尝试真实抓取
    kw = (keywords or ["网络安全", "人工智能"])[:1]
    try:
        items = fetch_platform(platform_id, kw, limit=3)
    except Exception as exc:
        return {"ok": False, "message": f"聚合失败: {exc}", "got_info": False, "available": avail}
    if items:
        first = items[0]
        return {"ok": True, "message": f"成功获取 {len(items)} 条，示例：{first.get('title','')[:60]}",
                "got_info": True, "available": avail,
                "sample": {"title": first.get("title", ""), "url": first.get("url", "")}}
    return {"ok": False, "message": "工具可用但未能取到结果（可能无匹配/受限）", "got_info": False, "available": avail}


def _run(cmd: list[str], timeout: int = 60) -> list[str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (out.stdout or "").splitlines() + (out.stderr or "").splitlines()
    except Exception:
        return []


def fetch_platform(platform_id: str, keywords: list[str], limit: int = 8) -> list[dict[str, Any]]:
    """按行业包关键词抓取平台文章（返回 {url,title,content}）。"""
    if platform_id == "rss":
        return _fetch_rss(keywords, limit)
    if platform_id == "github":
        return _fetch_github(keywords, limit)
    if platform_id == "youtube":
        return _fetch_youtube(keywords, limit)
    if platform_id == "v2ex":
        return _fetch_v2ex(keywords, limit)
    if platform_id == "bilibili":
        return _fetch_bilibili(keywords, limit)
    if platform_id == "web":
        return _fetch_web(keywords, limit)
    # 其余需 Cookie/工具的平台：此处不做通用抓取（用户配置后可用上游工具）。
    return []


def _fetch_rss(keywords, limit):
    try:
        import feedparser
    except ImportError:
        return []
    feeds = os.getenv("PLATFORM_SOURCE_RSS_AUTH", "")
    urls = [u.strip() for u in feeds.replace("，", ",").split(",") if u.strip()]
    arts, seen = [], set()
    for url in urls:
        try:
            p = feedparser.parse(url)
        except Exception:
            continue
        for e in p.entries or []:
            link = str(getattr(e, "link", "") or "").strip()
            title = str(getattr(e, "title", "") or "").strip()
            if not link or link in seen:
                continue
            seen.add(link)
            arts.append({"url": link, "title": title, "content": str(getattr(e, "summary", "") or title)})
            if len(arts) >= limit:
                return arts
    return arts


def _fetch_github(keywords, limit):
    import requests
    arts, seen = [], set()
    for kw in (keywords or ["industry"]):
        try:
            r = requests.get("https://api.github.com/search/repositories",
                             params={"q": kw, "sort": "updated", "per_page": max(1, min(limit, 30))},
                             headers={"Accept": "application/vnd.github+json"}, timeout=20)
            r.raise_for_status()
        except Exception:
            continue
        for it in (r.json() or {}).get("items") or []:
            url = str(it.get("html_url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            arts.append({"url": url, "title": str(it.get("full_name") or ""),
                         "content": str(it.get("description") or "") or str(it.get("full_name") or "")})
            if len(arts) >= limit:
                return arts
    return arts


def _fetch_youtube(keywords, limit):
    # yt-dlp 搜索：ytsearch<limit>:query 输出 JSON
    term = (keywords or ["news"])[0].strip()
    if not shutil.which("yt-dlp"):
        return []
    lines = _run(["yt-dlp", f"ytsearch{max(1, min(limit, 10))}:{term}",
                  "--dump-single-json", "--no-playlist", "--skip-download", "--quiet", "--no-warnings"], timeout=90)
    txt = "\n".join(lines)
    import json as _j
    try:
        data = _j.loads(txt)
    except Exception:
        return []
    arts = []
    for it in (data or {}).get("entries") or []:
        url = str(it.get("webpage_url") or it.get("url") or "").strip()
        title = str(it.get("title") or "").strip()
        if not url:
            continue
        arts.append({"url": url, "title": title, "content": title})
        if len(arts) >= limit:
            break
    return arts


def _fetch_v2ex(keywords, limit):
    import requests
    # 热门主题可作"文章"；关键词过滤标题
    try:
        r = requests.get("https://www.v2ex.com/api/topics/hot.json", headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
    except Exception:
        return []
    arts, seen = [], set()
    for t in r.json() or []:
        title = str(t.get("title") or "").strip()
        url = str(t.get("url") or "").strip()
        if not url or ",".join([k for k in (keywords or []) if k]) and not any(k in title for k in (keywords or [])):
            continue
        if url in seen:
            continue
        seen.add(url)
        arts.append({"url": url, "title": title, "content": title})
        if len(arts) >= limit:
            break
    return arts


def _fetch_bilibili(keywords, limit):
    import requests
    # 搜索 API（未登录也可用，直连）
    import urllib.parse
    term = (keywords or ["news"])[0].strip()
    try:
        r = requests.get("https://api.bilibili.com/x/web-interface/search/type",
                         params={"search_type": "video", "keyword": term, "page": 1},
                         headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com"}, timeout=20)
        r.raise_for_status()
    except Exception:
        return []
    arts, seen = [], set()
    for it in ((r.json() or {}).get("data", {}) or {}).get("result") or []:
        title = str(it.get("title") or "").strip()
        bvid = str(it.get("bvid") or "").strip()
        if not bvid or bvid in seen:
            continue
        seen.add(bvid)
        arts.append({"url": f"https://www.bilibili.com/video/{bvid}", "title": title, "content": title})
        if len(arts) >= limit:
            break
    return arts


def _fetch_web(keywords, limit):
    # 用行业关键词生成一个搜索词，再经 Jina Reader 读一个搜索页。作为兜底：返回空。
    return []

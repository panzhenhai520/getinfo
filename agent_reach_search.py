#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent-Reach 关键词聚焦检索：把社媒/社区平台的检索结果变成候选 URL。

定位（重要）：Agent-Reach 本身只是「安装器 + doctor」，它的 `AgentReach` 类只有
doctor()/doctor_report()，没有 search()/read() API。真正的检索由
`agent_reach_providers.fetch_platform()` 调上游工具完成。本模块只做三件事：

  1. 从行业包的搜索词出发，按平台逐个检索（严格按数量/耗时预算，宁可少跑不拖垮扫描）；
  2. 把各平台返回的异构字段统一成候选队列认识的结构
     `{url, title, snippet, published_at, platform}`；
  3. 提供比 SerpAPI 更严的预览闸门：社媒噪声大，必须命中行业锚点才允许它
     绕过候选打分为 0 的情况去占一个全文抓取槽。

这里**只发现 URL**，不做正文抓取、不做分类——后续照旧走候选门禁 → 抓正文 →
分类 → 主题归属。这样"聚焦分析、锁定目标"是靠闸门实现的，而不是靠把噪声灌进来。
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import config
from intel_candidates import quick_score_candidate
from project_keyword_gate import matched_project_keywords

# 平台展示名（只列对行业包有意义的；其余平台不参与扫描）
PLATFORM_LABEL = {
    "v2ex": "V2EX（技术/创投社区）",
    "bilibili": "哔哩哔哩（视频搜索）",
    "github": "GitHub（开源仓库）",
    "rss": "RSS（自备订阅）",
    "xueqiu": "雪球（财经社区）",
    "x": "X/Twitter（微博客）",
    "xhs": "小红书",
    "reddit": "Reddit",
    "youtube": "YouTube",
    "linkedin": "LinkedIn",
    "facebook": "Facebook",
    "instagram": "Instagram",
    "xiaoyuzhou": "小宇宙（播客）",
}

# 零配置平台：服务器上不需要任何 Cookie/Token 就能检索
ZERO_CONFIG_PLATFORMS = ("v2ex", "bilibili", "github", "rss")
# 需要凭据的平台：必须 PLATFORM_SOURCE_<ID>_ENABLED=true 且填了 AUTH 才会用
CREDENTIALED_PLATFORMS = (
    "xueqiu", "x", "xhs", "reddit", "youtube", "linkedin", "facebook",
    "instagram", "xiaoyuzhou",
)

# 追踪参数：不剥掉会让候选去重（按 canonical URL）失效，同一篇文章反复进队列
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm_id_from", "vd_source", "from_source", "share_source", "share_medium",
    "share_plat", "share_tag", "unique_k", "buvid", "seid", "bbid", "ts",
}
# 社区"非文章"页：会话列表、个人页、榜单页——直接丢掉，别浪费抓取槽
_NON_ARTICLE_RE = (
    "/member/", "/u/", "/space/", "/user/", "/login", "/signin", "/settings",
    "/notifications", "/balance", "/topics/recent", "/recent",
)

_AVAILABILITY_CACHE: Dict[str, Dict[str, Any]] = {}


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _platform_enabled(platform_id: str) -> bool:
    """平台开关沿用设置页已有的 PLATFORM_SOURCE_<ID>_ENABLED（显式开启才跑）。"""
    return _env_bool(f"PLATFORM_SOURCE_{platform_id.upper()}_ENABLED", False)


def _platform_has_credential(platform_id: str) -> bool:
    if platform_id in ZERO_CONFIG_PLATFORMS:
        return True
    return bool(str(os.getenv(f"PLATFORM_SOURCE_{platform_id.upper()}_AUTH", "") or "").strip())


def availability(platform_id: str) -> Dict[str, Any]:
    """平台上游工具是否就绪（带进程内缓存，避免每轮重复探测）。"""
    cached = _AVAILABILITY_CACHE.get(platform_id)
    if cached is not None:
        return cached
    try:
        from agent_reach_providers import availability as _avail

        result = _avail(platform_id)
    except Exception as exc:
        result = {"ok": False, "status": "error",
                  "message": f"Agent-Reach 不可用: {type(exc).__name__}: {str(exc)[:120]}",
                  "backend": None}
    _AVAILABILITY_CACHE[platform_id] = result
    return result


def _configured_platforms() -> List[str]:
    raw = str(getattr(config, "AGENT_REACH_PLATFORMS", "") or "")
    if not raw.strip():
        raw = ",".join(ZERO_CONFIG_PLATFORMS)
    wanted = [item.strip().casefold() for item in raw.replace("，", ",").split(",") if item.strip()]
    known = set(PLATFORM_LABEL)
    return [item for item in dict.fromkeys(wanted) if item in known]


def enabled_platforms() -> List[Dict[str, Any]]:
    """本次可以真正检索的平台：配置允许 + 开关打开 + 有凭据 + 上游工具就绪。"""
    usable: List[Dict[str, Any]] = []
    for platform_id in _configured_platforms():
        record = {"platform": platform_id, "label": PLATFORM_LABEL.get(platform_id, platform_id)}
        if not _platform_enabled(platform_id):
            usable.append({**record, "usable": False, "reason": "未在设置页开启（PLATFORM_SOURCE_*_ENABLED）"})
            continue
        if not _platform_has_credential(platform_id):
            usable.append({**record, "usable": False, "reason": "缺少凭据（PLATFORM_SOURCE_*_AUTH）"})
            continue
        state = availability(platform_id)
        usable.append({
            **record,
            "usable": bool(state.get("ok")),
            "reason": "" if state.get("ok") else f"上游工具未就绪：{state.get('message') or state.get('status')}",
            "backend": state.get("backend"),
        })
    return usable


def clean_url(url: str) -> str:
    """剥掉追踪参数与锚点：保证同一篇文章在候选队列里只出现一次。"""
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return ""
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.casefold() not in _TRACKING_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(query, doseq=True), ""))


def _looks_like_non_article(url: str) -> bool:
    lowered = str(url or "").casefold()
    return any(token in lowered for token in _NON_ARTICLE_RE)


def normalize_items(raw_items: Iterable[Dict], platform_id: str) -> List[Dict]:
    """把平台返回的异构结果统一成候选队列认识的字段。"""
    items: List[Dict] = []
    seen = set()
    for raw in raw_items or []:
        if not isinstance(raw, dict):
            continue
        url = clean_url(raw.get("url") or raw.get("link") or "")
        if not url or url in seen or _looks_like_non_article(url):
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        seen.add(url)
        snippet = str(
            raw.get("snippet") or raw.get("summary") or raw.get("description")
            or raw.get("content") or ""
        ).strip()
        items.append({
            "url": url,
            "title": title[:1000],
            "snippet": snippet[:2000],
            "published_at": raw.get("published_at") or raw.get("published") or "",
            "platform": platform_id,
        })
    return items


def search_platform(platform_id: str, query: str, limit: int = 5) -> List[Dict]:
    """单平台单关键词检索。任何失败都返回空列表，绝不影响扫描主流程。"""
    term = str(query or "").strip()
    if not term:
        return []
    try:
        from agent_reach_providers import fetch_platform

        raw = fetch_platform(platform_id, [term], limit=max(1, int(limit)))
    except Exception as exc:
        print(f"[agent-reach] {platform_id} 检索失败（已忽略）: "
              f"{type(exc).__name__}: {str(exc)[:120]}", flush=True)
        return []
    return normalize_items(raw, platform_id)


def search_pack(
    queries: Iterable[str],
    *,
    platforms: Optional[Iterable[str]] = None,
    per_query: Optional[int] = None,
    deadline_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """按行业包搜索词逐个平台聚焦检索。

    预算控制（都是硬边界，宁可少跑也不拖垮扫描）：
      · 平台数上限 AGENT_REACH_MAX_PLATFORMS_PER_RUN
      · 每个平台的关键词数上限 AGENT_REACH_MAX_QUERIES_PER_PLATFORM
      · 单关键词结果数上限 AGENT_REACH_MAX_ITEMS_PER_QUERY
      · 总耗时上限 AGENT_REACH_TIMEOUT_SECONDS（超时即停止，把已拿到的结果交出去）
    """
    started = time.monotonic()
    per_query = int(per_query or getattr(config, "AGENT_REACH_MAX_ITEMS_PER_QUERY", 5) or 5)
    deadline = float(
        deadline_seconds
        if deadline_seconds is not None
        else getattr(config, "AGENT_REACH_TIMEOUT_SECONDS", 90) or 90
    )
    max_platforms = max(1, int(getattr(config, "AGENT_REACH_MAX_PLATFORMS_PER_RUN", 3) or 3))
    max_queries = max(1, int(getattr(config, "AGENT_REACH_MAX_QUERIES_PER_PLATFORM", 2) or 2))

    if platforms is None:
        usable = [item["platform"] for item in enabled_platforms() if item.get("usable")]
    else:
        usable = [str(item) for item in platforms]
    usable = usable[:max_platforms]
    query_list = [str(q).strip() for q in (queries or []) if str(q or "").strip()][:max_queries]

    result: Dict[str, Any] = {
        "platforms": usable,
        "queries": query_list,
        "items": [],
        "errors": [],
        "timeout": False,
        "elapsed_seconds": 0.0,
        "unavailable": [
            item for item in enabled_platforms()
            if not item.get("usable") and item["platform"] in _configured_platforms()
        ],
    }
    if not usable or not query_list:
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        return result

    collected: List[Dict] = []
    for platform_id in usable:
        for query in query_list:
            # deadline<=0 视为"没有时间预算"：一次都不发（Windows 的 monotonic 分辨率
            # 较粗，用 `elapsed > 0` 判断可能因为落在同一时钟刻度而漏判）。
            if deadline <= 0 or time.monotonic() - started > deadline:
                result["timeout"] = True
                result["errors"].append(f"超出总耗时预算 {deadline:.0f}s，停止检索")
                break
            try:
                items = search_platform(platform_id, query, limit=per_query)
            except Exception as exc:
                # 平台失败必须被隔离：一个平台出问题不能让整轮检索中断，
                # 更不能影响扫描器的其它分支（RSS/列表页/正文抓取）。
                result["errors"].append(
                    f"{platform_id}: {type(exc).__name__}: {str(exc)[:120]}"
                )
                continue
            # 带上产生它的检索词：候选门禁要按对应行业包的关键词做聚焦判定
            for item in items:
                item["query"] = query
            collected.extend(items)
        if result["timeout"]:
            break
    result["items"] = collected
    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return result


def preview_gate(item: Dict, pack: Dict, query_text: str = "") -> bool:
    """社媒候选的严格闸门：必须命中行业锚点（机构词/品牌词）才允许绕过打分占槽。

    比 SerpAPI 那道闸门更严：社媒标题短、噪声大，只靠"搜索词命中"不足以证明
    它属于本行业——标题/摘要里必须有行业锚点。
    """
    title = str(item.get("title") or "")
    summary = str(item.get("snippet") or item.get("summary") or "")
    if not summary:
        # 社媒结果常常只有标题：没有摘要时要求标题更长，避免一个短标题就占槽
        if len(title) < 12:
            return False
    try:
        score = quick_score_candidate(title, summary, pack)
    except Exception:
        # 打分依赖行业包结构（如 classification）；结构异常时按"不放行"处理，
        # 绝不让一个门禁把整轮扫描打断。
        return False
    brand_hits = matched_project_keywords(pack.get("brands") or [], title, summary)
    return bool(score.get("anchor_hits") or brand_hits)

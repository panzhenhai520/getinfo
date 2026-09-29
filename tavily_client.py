"""Tavily 搜索客户端。

定位（与 SerpAPIClient 一致）：**只负责"发现 URL"**，返回 {title, url, snippet}；
正文仍走现有链路——候选门禁 → 正文抓取 → 分类（article_intel_classifications）
→ 主题归属（正文匹配 fixed_topics 关键词）。所以搜索结果不会绕过门禁，
也不会把摘要当正文入库。

配额：官方按"credits/月"计费，dev key 额度更小。本客户端只做**每次调用计数**，
"每轮上限 / 每月上限"的判定在调用方（intel_light_scanner / 定时任务）执行，
到顶即跳过而不是硬跑。
"""

from __future__ import annotations

import json
import time
from typing import Dict, List, Optional

import config

try:  # 项目统一使用 requests；缺失时降级为 urllib
    import requests
except Exception:  # pragma: no cover
    requests = None
    import urllib.request


class TavilyClient:
    """Tavily 搜索客户端（接口与 SerpAPIClient 对齐：search(query) -> List[Dict]）。"""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None) -> None:
        self.api_key = (api_key if api_key is not None else getattr(config, "TAVILY_API_KEY", "")) or ""
        self.base_url = (base_url or getattr(config, "TAVILY_BASE_URL", "") or "https://api.tavily.com/search").strip()
        self.max_results = int(getattr(config, "TAVILY_MAX_RESULTS", 5) or 5)
        self.search_depth = str(getattr(config, "TAVILY_SEARCH_DEPTH", "basic") or "basic")
        self.timeout = int(getattr(config, "TAVILY_TIMEOUT_SECONDS", 20) or 20)
        self.max_retries = int(getattr(config, "TAVILY_MAX_RETRIES", 1) or 1)
        self.call_count = 0  # 本次运行内的调用次数（供"每轮上限"判定）

    # ── 可用性 ─────────────────────────────────────────────
    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip())

    @property
    def enabled(self) -> bool:
        return self.configured and bool(getattr(config, "TAVILY_ENABLED", False))

    # ── 搜索 ───────────────────────────────────────────────
    def search(self, query: str, max_results: Optional[int] = None) -> List[Dict[str, str]]:
        """执行一次搜索，返回 [{title, url, snippet}]；失败返回空列表（不抛给调用方）。"""
        text = str(query or "").strip()
        if not text or not self.configured:
            return []
        payload = {
            "api_key": self.api_key,
            "query": text,
            "max_results": int(max_results or self.max_results),
            "search_depth": self.search_depth,
            "include_answer": False,
            "include_raw_content": False,  # 只要 URL/摘要：正文由抓取链路负责
        }
        body = json.dumps(payload).encode("utf-8")
        last_error = ""
        for attempt in range(self.max_retries + 1):
            try:
                self.call_count += 1
                if requests is not None:
                    resp = requests.post(
                        self.base_url, data=body,
                        headers={"Content-Type": "application/json"}, timeout=self.timeout,
                    )
                    if resp.status_code >= 400:
                        last_error = "HTTP %s: %s" % (resp.status_code, (resp.text or "")[:180])
                        continue
                    data = resp.json()
                else:  # pragma: no cover
                    req = urllib.request.Request(self.base_url, data=body,
                                                 headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=self.timeout) as raw:
                        data = json.loads(raw.read().decode("utf-8") or "{}")
                return self._normalize(data)
            except Exception as exc:
                last_error = str(exc)[:180]
                if attempt < self.max_retries:
                    time.sleep(min(2 * (attempt + 1), 5))
        print(f"⚠️ Tavily 搜索失败（{text[:30]}…）: {last_error}")
        return []

    @staticmethod
    def _normalize(data: Dict) -> List[Dict[str, str]]:
        """把 Tavily 响应规范成下游统一结构（与 SerpAPI 结果同形）。"""
        results = []
        for item in (data or {}).get("results") or []:
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            results.append({
                "title": str(item.get("title") or "").strip(),
                "url": url,
                "snippet": str(item.get("content") or item.get("snippet") or "").strip()[:500],
                "source": "tavily",
                # Tavily 基础搜索(basic)可能不返回发布时间；能拿到时映射为 published_at，
                # 供候选时效性准入使用，拿不到则留空（由搜索发现旁路在候选阶段放行）。
                "published_at": str(item.get("published_date") or "").strip(),
            })
        return results


__all__ = ["TavilyClient"]

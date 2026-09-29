#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 管线：对 13 个外部信源，按行业包关键词获取文章，
经与现有聚合相同的远程 LLM 管道做 二次提炼→翻译→男声语音 后发布/落库。

抓取层为可插拔 provider：每个信源对应一个 provider 函数，负责调用该信源已
由上游工具(经 Agent-Reach 安装/配置)抓到原文并归一化为 {url,title,content}。
未实现/未配置的信源返回空，不影响其它信源。

用法:  python tools/agent_pipeline.py --pack <industry_pack_id> [--platform xhs,x] [--limit 8]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
import os

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from sqlite_database import sqlite_db
from remote_pipeline_client import RemotePipelineError, RemotePipelineUnavailable, remote_pipeline_client
from remote_result_ingestor import ensure_remote_pipeline_schema, ingest_remote_result
from industry_packs import industry_pack_loader
from platform_sources import PLATFORM_SOURCES, _env_key


def _pack_keywords(pack_id: str) -> list[str]:
    """从行业包取关键词（抓取口径）。"""
    try:
        pack = industry_pack_loader.load(pack_id) or {}
    except Exception:
        pack = {}
    kw = pack.get('keywords') or pack.get('serpapi_queries') or []
    if isinstance(kw, str):
        kw = [k.strip() for k in kw.split(',') if k.strip()]
    return list(kw)


def _platform_enabled(platform_id: str) -> bool:
    return str(os.getenv(_env_key(platform_id, "ENABLED"), '') or '').strip().lower() in ('1', 'true', 'yes', 'on')


def _platform_auth(platform_id: str) -> str:
    return str(os.getenv(_env_key(platform_id, "AUTH"), '') or '')


def _provider_registry():
    """信源 → 抓取函数(keywords)->list[{url,title,content}]。

    具体实现应调用该信源已安装/配置的上游工具（twitter-cli / yt-dlp / mcporter 等）
    抓取并归一化。默认已实现 RSS（直读订阅源）与 GitHub（公开仓库搜索）。
    """
    return {"rss": _fetch_rss, "github": _fetch_github}


def _fetch_github(keywords, limit=10):
    """GitHub 信源：用行业包关键词逐词做公开仓库搜索（无需 token，限流）。"""
    try:
        import requests
    except ImportError:
        return []
    seen: dict[str, dict] = {}
    for kw in [k for k in (keywords or []) if k.strip()] or ["industry"]:
        try:
            r = requests.get(
                "https://api.github.com/search/repositories",
                params={"q": kw.strip(), "sort": "updated", "per_page": max(1, min(limit, 30))},
                headers={"Accept": "application/vnd.github+json"}, timeout=20,
            )
            r.raise_for_status()
            items = (r.json() or {}).get("items") or []
        except Exception as exc:
            print(f"[github] 搜索失败({kw[:20]}): {exc}", flush=True)
            continue
        for it in items:
            url = str(it.get("html_url") or "").strip()
            if not url or url in seen:
                continue
            name = str(it.get("full_name") or "").strip()
            desc = str(it.get("description") or "").strip()
            content = desc or name
            if not desc:
                try:
                    readme = requests.get(
                        f"https://raw.githubusercontent.com/{name}/HEAD/README.md",
                        headers={"Accept": "application/vnd.github.raw"}, timeout=15,
                    )
                    if readme.status_code == 200:
                        content = readme.text[:3000]
                except Exception:
                    pass
            seen[url] = {"url": url, "title": name, "content": content}
        if len(seen) >= limit:
            break
    return list(seen.values())[:limit]


def _fetch_rss(keywords):
    """RSS 信源：从配置的订阅地址(逗号分隔)抓取并归一化条目。"""
    try:
        import feedparser
    except ImportError:
        print("[rss] 缺少 feedparser", flush=True)
        return []
    feeds = _platform_auth("rss")
    urls = [u.strip() for u in feeds.replace("，", ",").split(",") if u.strip()]
    articles = []
    for feed_url in urls:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as exc:
            print(f"[rss] 解析失败 {feed_url}: {exc}", flush=True)
            continue
        for entry in parsed.entries or []:
            link = str((getattr(entry, "link", None) or '')).strip()
            title = str((getattr(entry, "title", None) or '')).strip()
            content = str((getattr(entry, "summary", None) or getattr(entry, "description", None) or '')).strip()
            if not link or not title:
                continue
            articles.append({"url": link, "title": title, "content": content or title})
    return articles


def _fetch(provider, keywords, limit):
    try:
        items = provider(keywords) or []
        return list(items)[:limit]
    except Exception as exc:
        print(f"[provider-error] {exc}", flush=True)
        return []


def run_agent_pipeline(pack_id: str, platform_ids=None, limit=8) -> dict:
    """对启用+已配置的信源，用行业包关键词抓文章 → 进 LLM 管道 → 落库。"""
    ensure_remote_pipeline_schema(sqlite_db)
    providers = _provider_registry()
    keywords = _pack_keywords(pack_id)
    if not keywords:
        print(f"[warn] industry pack '{pack_id}' has no keywords", flush=True)
    targets = []
    for p in PLATFORM_SOURCES:
        pid = p['id']
        if platform_ids and pid not in platform_ids:
            continue
        if not _platform_enabled(pid):
            continue
        if pid not in providers:
            print(f"[skip] {p['label']}: 未实现 provider（需安装/配置该信源工具）", flush=True)
            continue
        if not _platform_auth(pid):
            print(f"[skip] {p['label']}: 未配置凭据，请在系统设置→信源 中配置", flush=True)
            continue
        targets.append((pid, p['label'], providers[pid]))
    print(f"[run] pack={pack_id} keywords={len(keywords)} platforms={len(targets)}", flush=True)

    ok = 0
    fail = 0
    articles_ingested = 0
    for pid, label, provider in targets:
        fetched = _fetch(provider, keywords, limit)
        print(f"[fetch] {label}({pid}) got={len(fetched)}", flush=True)
        for art in fetched:
            url = str(art.get('url') or '').strip()
            content = str(art.get('content') or '').strip()
            if not url or not content:
                continue
            task_id = f"agent_pipeline_{pid}_{int(time.time()*1000)}"
            try:
                result = remote_pipeline_client.run(
                    url=url, mode='article', keywords=list(keywords), limit=1,
                    task_id=task_id,
                    enrich=config.REMOTE_PIPELINE_ENRICH, tts=config.REMOTE_PIPELINE_TTS,
                    voice=config.REMOTE_PIPELINE_TTS_VOICE,
                )
                if not result.get('success'):
                    fail += 1
                    print(f"[llm-fail] {url}", flush=True)
                    continue
                # 标记来源：agent-search（AI 信源检索）
                for art in (result.get('articles') or []):
                    art['source_method'] = 'agent-search'
                    art['source_task_name'] = f"agent-search:{label}"
                    # 复用前端"来源=domain"展示位：把显示来源设为 Agent-search，普通 robots 来源保持站点名
                    art['domain'] = 'Agent-search'
                saved = ingest_remote_result(
                    sqlite_db, result, configured_url=url, keywords=list(keywords),
                    task_id=task_id, task_name=f"agent-search:{label}",
                )
                n = len(saved.get('articles') or [])
                articles_ingested += n
                ok += 1
                print(f"[ok] {label} {url} articles={n}", flush=True)
            except (RemotePipelineUnavailable, RemotePipelineError) as exc:
                fail += 1
                print(f"[llm-error] {url}: {exc}", flush=True)
    print(f"DONE pack={pack_id} ok={ok} fail={fail} ingested={articles_ingested}", flush=True)
    return {"ok": ok, "fail": fail, "ingested": articles_ingested}


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", default=getattr(config, 'INTEL_DEFAULT_INDUSTRY_PACK', 'family_office'))
    parser.add_argument("--platform", default="", help="逗号分隔的信源 id（默认全部已启用信源）")
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()
    platform_ids = [p.strip() for p in args.platform.split(",") if p.strip()] if args.platform else None
    return 0 if run_agent_pipeline(args.pack, platform_ids, args.limit)['fail'] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

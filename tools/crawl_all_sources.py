#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐源抓取所有 managed_urls：经 VPN 远程管道完成 聚合+提炼总结+翻译+普通话男声语音，并回填 PostgreSQL。

用法（容器内 /app 与宿主当前代码一致）：
  python tools/crawl_all_sources.py --dry-run
  python tools/crawl_all_sources.py --url https://www.aqniu.com --limit 3      # 单源验证
  python tools/crawl_all_sources.py --limit 8 --max-sources 76                # 全量
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# 保证在容器内也能找到项目根目录模块
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from sqlite_database import sqlite_db
from remote_pipeline_client import RemotePipelineError, RemotePipelineUnavailable, remote_pipeline_client
from remote_result_ingestor import ensure_remote_pipeline_schema, ingest_remote_result


def _read_keywords_map():
    """从 scheduled_tasks 取 target_url -> 关键词列表（用于入库闸门命中）。"""
    sqlite_db._ensure_connection()
    with sqlite_db.lock:
        rows = sqlite_db.connection.execute(
            "SELECT target_url, keywords FROM scheduled_tasks "
            "WHERE target_url IS NOT NULL AND keywords IS NOT NULL AND keywords != '' "
            "ORDER BY id"
        ).fetchall()
    mapping: dict[str, list[str]] = {}
    for row in rows:
        url = str(row["target_url"]).strip()
        keywords = [k.strip() for k in str(row["keywords"]).split(",") if k.strip()]
        if url and keywords:
            mapping.setdefault(url, [])
            for k in keywords:
                if k not in mapping[url]:
                    mapping[url].append(k)
    return mapping


def _read_targets(url: str, max_sources: int | None, kw_map: dict[str, list[str]]):
    targets: list[dict] = []
    if url:
        targets.append({"url": url, "name": url, "keywords": kw_map.get(url.split("//", 1)[-1].split("/")[0], [])})
        return targets
    sqlite_db._ensure_connection()
    with sqlite_db.lock:
        rows = sqlite_db.connection.execute(
            "SELECT id, url, name FROM managed_urls WHERE url IS NOT NULL ORDER BY id"
        ).fetchall()
    for row in rows:
        raw_url = str(row["url"])
        targets.append({
            "url": raw_url,
            "name": str(row["name"] or raw_url),
            "keywords": kw_map.get(raw_url, []),
        })
    # 去重
    seen: set[str] = set()
    unique = []
    for t in targets:
        u = t["url"]
        if u in seen:
            continue
        seen.add(u)
        unique.append(t)
    if max_sources:
        unique = unique[:max_sources]
    return unique


def _submit_all(targets, args):
    jobs: list[dict] = []
    for t in targets:
        url = t["url"]
        keywords = list(t.get("keywords") or [])
        task_id = "round_1_" + (url.split("//")[1].split("/")[0] if "//" in url else "src")
        try:
            job_id = remote_pipeline_client.submit(
                url=url,
                mode="list",
                keywords=keywords,
                limit=args.limit,
                task_id=task_id,
                enrich=config.REMOTE_PIPELINE_ENRICH,
                tts=config.REMOTE_PIPELINE_TTS,
                voice=config.REMOTE_PIPELINE_TTS_VOICE,
                industry_pack_id=getattr(args, 'industry_pack_id', ''),
                industry_pack_name=getattr(args, 'industry_pack_name', ''),
            )
            jobs.append({"job_id": job_id, "url": url, "name": t["name"], "task_id": task_id, "keywords": keywords})
            print(f"[submitted] {url} -> {job_id} kw={len(keywords)}", flush=True)
        except (RemotePipelineUnavailable, RemotePipelineError) as exc:
            print(f"[submit-error] {url}: {exc}", flush=True)
    return jobs


def _is_retryable(error: str) -> bool:
    """是否可重试（尚未触犯 robots、非永久失败）。

    - robots_disallowed（网站明确禁止）→ 永久失败，绝不重试（坚守 robots.txt）。
    - 其余（robots_unavailable、连接超时/拒绝等）→ 可重试，稍后重新读 robots 再决定。
    """
    low = str(error or "").lower()
    if "robots_disallowed" in low:
        return False
    return True


def _resubmit(j, args) -> str | None:
    """以新增重试序号的任务ID重新提交，强制管道新建任务（重新读 robots 再爬）。"""
    try:
        task_id = f"{j['task_id']}_r{j.get('retry', 0)}"
        return remote_pipeline_client.submit(
            url=j['url'], mode='list', keywords=list(j.get('keywords') or []),
            limit=args.limit, task_id=task_id,
            enrich=config.REMOTE_PIPELINE_ENRICH, tts=config.REMOTE_PIPELINE_TTS,
            voice=config.REMOTE_PIPELINE_TTS_VOICE,
        )
    except (RemotePipelineUnavailable, RemotePipelineError) as exc:
        print(f"[resubmit-error] {j['url']}: {exc}", flush=True)
        return None


def _process(jobs, args):
    """并发轮询所有已提交任务，完成的即时回填；失败按 robots/连接错分类做可重试。"""
    import requests
    ok = 0
    fail = 0
    ingested_articles = 0
    done_ids: set[str] = set()
    deadline = time.time() + (args.watch_hours or 12) * 3600
    retry_max = max(0, int(getattr(args, 'retry_max', 3) or 0))
    retry_backoff = max(0, int(getattr(args, 'retry_backoff', 600) or 0))
    headers = {'Authorization': 'Bearer ' + config.REMOTE_PIPELINE_TOKEN}
    while time.time() < deadline:
        progress = False
        for j in jobs:
            if j['job_id'] in done_ids:
                continue
            if j.get('next_attempt') and time.time() < j['next_attempt']:
                continue  # 退避中
            try:
                d = requests.get(f"{config.REMOTE_PIPELINE_URL}/v1/pipeline/jobs/{j['job_id']}",
                                 headers=headers, timeout=15).json()
            except Exception:
                continue
            status = d.get('status')
            if status not in ('completed', 'partial_success', 'failed'):
                continue
            progress = True
            if status == 'failed':
                err = str(d.get('error') or '')
                # 可重试且未超次 → 延迟重试；否则永久失败
                if _is_retryable(err) and retry_max > 0 and j.get('retry', 0) < retry_max:
                    j['retry'] = j.get('retry', 0) + 1
                    j['next_attempt'] = time.time() + retry_backoff
                    new_id = _resubmit(j, args)
                    if new_id:
                        j['job_id'] = new_id
                        print(f"[retry] {j['url']} attempt={j['retry']} (was: {err[:70]})", flush=True)
                        continue  # 不移入 done，下次再轮询
                    # 重新提交失败 → 视为永久失败
                    done_ids.add(j['job_id'])
                    fail += 1
                    print(f"[failed] {j['url']} resubmit_failed", flush=True)
                else:
                    done_ids.add(j['job_id'])
                    fail += 1
                    print(f"[failed] {j['url']} job={j['job_id']} err={(err or '')[:90]}"
                          + (" [永久:robots_disallowed]" if "robots_disallowed" in err else ""), flush=True)
                continue
            result = d.get('result') or {}
            if not result.get('success'):
                done_ids.add(j['job_id'])
                fail += 1
                print(f"[failed] {j['url']} status={status} no_success", flush=True)
                continue
            try:
                saved = ingest_remote_result(
                    sqlite_db, result, configured_url=j['url'],
                    keywords=list(j.get('keywords') or []),
                    task_id=j['task_id'], task_name=j['name'],
                )
                n = len(saved.get('articles') or [])
                ingested_articles += n
                ok += 1
                done_ids.add(j['job_id'])
                print(f"[ok] {j['url']} job={j['job_id']} status={status} articles={n}", flush=True)
            except Exception as exc:
                done_ids.add(j['job_id'])
                fail += 1
                print(f"[error-ingest] {j['url']}: {exc}", flush=True)
        if done_ids and len(done_ids) >= len(jobs):
            break
        if not progress:
            time.sleep(10)
    print(f"DONE ok={ok} fail={fail} ingested_articles={ingested_articles} total_jobs={len(jobs)} done={len(done_ids)}", flush=True)
    return 0 if fail == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--url", default="", help="仅处理单个URL")
    parser.add_argument("--limit", type=int, default=8, help="每个源抓取文章上限")
    parser.add_argument("--max-sources", type=int, default=None, help="最多处理多少个源")
    parser.add_argument("--watch-hours", type=int, default=12, help="最多轮询等待多少小时")
    parser.add_argument("--retry-max", type=int, default=3, help="可重试失败(robots_unavailable/连接错)最大重试次数")
    parser.add_argument("--retry-backoff", type=int, default=600, help="每次重试间隔秒数")
    args = parser.parse_args()

    sqlite_db._ensure_connection()
    ensure_remote_pipeline_schema(sqlite_db)

    kw_map = _read_keywords_map()
    targets = _read_targets(args.url, args.max_sources, kw_map)
    print(
        f"targets={len(targets)} limit={args.limit} "
        f"voice={config.REMOTE_PIPELINE_TTS_VOICE} enrich={config.REMOTE_PIPELINE_ENRICH} "
        f"tts={config.REMOTE_PIPELINE_TTS}",
        flush=True,
    )
    if args.dry_run:
        for t in targets:
            print(f"[dry-run] {t['url']}", flush=True)
        return 0

    jobs = _submit_all(targets, args)
    print(f"submitted={len(jobs)}", flush=True)
    if not jobs:
        print("no jobs submitted", flush=True)
        return 1
    return _process(jobs, args)


if __name__ == "__main__":
    raise SystemExit(main())

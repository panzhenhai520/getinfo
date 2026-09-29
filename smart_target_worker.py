#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Smart target URL worker.

Usage examples:
  python3 smart_target_worker.py --mode init
  python3 smart_target_worker.py --mode incremental --task-id 12
  python3 smart_target_worker.py --mode init --dry-run
"""

from __future__ import annotations

import argparse
import json
import signal
import time
from datetime import timedelta

from sqlite_database import SQLiteDatabase
from smart_target_resolver import DEFAULT_SMART_RESOLVER_KEYWORDS, resolve_task_smart_targets
from utils import coerce_int, get_china_time


_STOP_REQUESTED = False


def _request_stop(_signum, _frame) -> None:
    global _STOP_REQUESTED
    _STOP_REQUESTED = True


def _high_confidence_targets(result: dict, include_exploration: bool = False) -> list[dict]:
    targets = []
    seen = set()
    source_items = list(result.get('suggestions') or [])
    if include_exploration:
        source_items.extend(result.get('exploration_urls') or [])

    for item in source_items:
        url = (item.get('url') or '').strip()
        if not url or url in seen:
            continue
        seen.add(url)
        if item.get('auto_checked') or coerce_int(item.get('match_count'), 0) > 0:
            targets.append(item)
    return targets


def _select_tasks(db: SQLiteDatabase, task_id: int | None, limit: int | None) -> list[dict]:
    if task_id:
        task = db.get_scheduled_task(task_id)
        return [task] if task and task.get('is_active') else []

    tasks, _total = db.get_scheduled_tasks(page=1, per_page=1000, is_active=True)
    if limit:
        tasks = tasks[:limit]
    return tasks


def run_worker(
    mode: str,
    task_id: int | None = None,
    ttl_hours: int = 24,
    limit: int | None = None,
    dry_run: bool = False,
    include_exploration: bool = False,
    max_keywords: int | None = 16,
    max_candidates: int = 12,
    max_results: int = 8,
    article_sample_size: int = 3,
    max_articles_per_keyword: int = 3,
) -> dict:
    db = SQLiteDatabase()
    if not db.connect() or not db.create_tables():
        raise RuntimeError('数据库连接或迁移失败')

    expires_at = (get_china_time() + timedelta(hours=ttl_hours)).strftime('%Y-%m-%d %H:%M:%S')
    tasks = _select_tasks(db, task_id, limit)
    summary = {
        'mode': mode,
        'task_count': len(tasks),
        'saved_targets': 0,
        'failed_tasks': 0,
        'skipped_no_targets': 0,
        'dry_run': dry_run,
        'results': [],
    }

    try:
        for task in tasks:
            schedule_id = task.get('id')
            configured_url = task.get('target_url') or ''
            keywords = task.get('keywords') or DEFAULT_SMART_RESOLVER_KEYWORDS
            if keywords:
                try:
                    from keyword_governance import apply_keyword_rules_to_task_keyword_text
                    keywords = apply_keyword_rules_to_task_keyword_text(keywords)
                except Exception as exc:
                    print(f"⚠️ 任务 {schedule_id} 关键词治理失败，跳过: {exc}")
                    summary['failed_tasks'] += 1
                    continue
                if not keywords:
                    print(f"⚠️ 任务 {schedule_id} 关键词治理后为空，跳过")
                    summary['skipped_no_targets'] += 1
                    continue
            keyword_items = [
                item.strip()
                for item in str(keywords or '').replace('，', ',').replace('；', ';').replace(';', ',').split(',')
                if item.strip()
            ]
            if max_keywords:
                keywords = ','.join(keyword_items[:max(1, coerce_int(max_keywords, 16))])
            try:
                result = resolve_task_smart_targets(
                    task,
                    keywords,
                    max_candidates=max_candidates,
                    max_results=max_results,
                    article_sample_size=article_sample_size,
                    max_articles_per_keyword=max_articles_per_keyword,
                )
                targets = _high_confidence_targets(result, include_exploration=include_exploration)
                saved = 0
                if targets and not dry_run:
                    saved = db.save_smart_target_urls(
                        schedule_id=schedule_id,
                        configured_url=configured_url,
                        targets=targets,
                        generated_by=f'smart_target_worker:{mode}',
                        expires_at=expires_at,
                    )
                if not dry_run:
                    db.record_crawl_attempt({
                        'schedule_id': schedule_id,
                        'configured_url': configured_url,
                        'resolved_target_url': targets[0]['url'] if targets else configured_url,
                        'canonical_url': targets[0].get('canonical_url') if targets else '',
                        'crawler_engine': 'smart_resolver',
                        'phase': f'smart_target_{mode}',
                        'status': 'completed',
                        'candidate_urls': len(result.get('suggestions') or []),
                        'keyword_hits': sum(coerce_int(item.get('match_count'), 0) for item in targets),
                        'metadata': {
                            'status': result.get('status'),
                            'saved_targets': saved,
                            'verified_articles': len(result.get('verified_articles') or []),
                        },
                    })
                summary['saved_targets'] += saved
                summary['results'].append({
                    'task_id': schedule_id,
                    'task_name': task.get('task_name'),
                    'status': result.get('status'),
                    'suggestions': len(result.get('suggestions') or []),
                    'high_confidence_targets': len(targets),
                    'saved_targets': saved,
                })
            except Exception as exc:
                summary['failed_tasks'] += 1
                if not dry_run:
                    db.record_crawl_attempt({
                        'schedule_id': schedule_id,
                        'configured_url': configured_url,
                        'resolved_target_url': configured_url,
                        'crawler_engine': 'smart_resolver',
                        'phase': f'smart_target_{mode}',
                        'status': 'failed',
                        'error_message': str(exc)[:500],
                    })
                summary['results'].append({
                    'task_id': schedule_id,
                    'task_name': task.get('task_name'),
                    'status': 'failed',
                    'error': str(exc)[:200],
                })
        return summary
    finally:
        db.disconnect()


def run_daemon(
    mode: str,
    interval_seconds: int,
    ttl_hours: int,
    limit: int | None = None,
    dry_run: bool = False,
    include_exploration: bool = False,
    run_once_first: bool = True,
    max_keywords: int | None = 16,
    max_candidates: int = 12,
    max_results: int = 8,
    article_sample_size: int = 3,
    max_articles_per_keyword: int = 3,
) -> None:
    """Run smart URL analysis continuously for systemd deployment."""
    global _STOP_REQUESTED
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    interval_seconds = max(300, coerce_int(interval_seconds, 3600))
    print(
        json.dumps({
            'event': 'smart_target_worker_started',
            'mode': mode,
            'interval_seconds': interval_seconds,
            'ttl_hours': ttl_hours,
            'dry_run': dry_run,
            'started_at': get_china_time().isoformat(),
        }, ensure_ascii=False),
        flush=True,
    )

    first = True
    while not _STOP_REQUESTED:
        if first and not run_once_first:
            first = False
        else:
            summary = run_worker(
                mode=mode,
                ttl_hours=ttl_hours,
                limit=limit,
                dry_run=dry_run,
                include_exploration=include_exploration,
                max_keywords=max_keywords,
                max_candidates=max_candidates,
                max_results=max_results,
                article_sample_size=article_sample_size,
                max_articles_per_keyword=max_articles_per_keyword,
            )
            summary['event'] = 'smart_target_worker_cycle_completed'
            summary['completed_at'] = get_china_time().isoformat()
            print(json.dumps(summary, ensure_ascii=False), flush=True)
            first = False

        slept = 0
        while slept < interval_seconds and not _STOP_REQUESTED:
            step = min(5, interval_seconds - slept)
            time.sleep(step)
            slept += step

    print(
        json.dumps({
            'event': 'smart_target_worker_stopped',
            'stopped_at': get_china_time().isoformat(),
        }, ensure_ascii=False),
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description='Generate derived smart target URLs for scheduled crawl tasks.')
    parser.add_argument('--mode', choices=('init', 'incremental'), default='incremental')
    parser.add_argument('--task-id', type=int, default=None)
    parser.add_argument('--ttl-hours', type=int, default=24)
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--include-exploration', action='store_true')
    parser.add_argument('--daemon', action='store_true')
    parser.add_argument('--interval-seconds', type=int, default=3600)
    parser.add_argument('--no-run-on-start', action='store_true')
    parser.add_argument('--max-keywords', type=int, default=16)
    parser.add_argument('--max-candidates', type=int, default=12)
    parser.add_argument('--max-results', type=int, default=8)
    parser.add_argument('--article-sample-size', type=int, default=3)
    parser.add_argument('--max-articles-per-keyword', type=int, default=3)
    args = parser.parse_args()

    if args.daemon:
        if args.task_id:
            raise SystemExit('--daemon 模式不支持 --task-id；常驻 worker 会扫描全部启用任务')
        run_daemon(
            mode=args.mode,
            interval_seconds=args.interval_seconds,
            ttl_hours=args.ttl_hours,
            limit=args.limit,
            dry_run=args.dry_run,
            include_exploration=args.include_exploration,
            run_once_first=not args.no_run_on_start,
            max_keywords=args.max_keywords,
            max_candidates=args.max_candidates,
            max_results=args.max_results,
            article_sample_size=args.article_sample_size,
            max_articles_per_keyword=args.max_articles_per_keyword,
        )
        return

    summary = run_worker(
        mode=args.mode,
        task_id=args.task_id,
        ttl_hours=args.ttl_hours,
        limit=args.limit,
        dry_run=args.dry_run,
        include_exploration=args.include_exploration,
        max_keywords=args.max_keywords,
        max_candidates=args.max_candidates,
        max_results=args.max_results,
        article_sample_size=args.article_sample_size,
        max_articles_per_keyword=args.max_articles_per_keyword,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

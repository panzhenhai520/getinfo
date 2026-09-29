#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Initialize every canonical source after an industry-pack activation."""

from __future__ import annotations

from typing import Dict
from datetime import datetime, timedelta, timezone
import json

from industry_pack_runtime import active_industry_composition_service
from intel_database import intel_repository
from intel_sources import intel_source_registry


def finalize_industry_initialization(
    *, activation_id: str, industry_pack_id: str, report: Dict | None = None,
    database=None,
) -> Dict:
    """Atomically promote projected rows after the one-time scan finishes.

    The same projection rows are retained for auditability; only their
    execution state changes. This makes retries idempotent and prevents a
    switch from accidentally enabling another industry's schedules.
    """
    activation_id = str(activation_id or '')
    pack_id = str(industry_pack_id or '')
    if not activation_id or not pack_id:
        return {'finalized': False, 'reason': 'missing_activation'}
    db = database or intel_repository.db
    now = datetime.now(timezone.utc)
    next_run = (now + timedelta(minutes=1)).strftime('%Y-%m-%d %H:%M:%S')
    with db.lock:
        cur = db.connection.cursor()
        try:
            cur.execute('BEGIN IMMEDIATE')
            rows = cur.execute(
                """SELECT id, config FROM scheduled_tasks
                   WHERE ownership_type='source_registry_projection'
                     AND industry_pack_id=? AND activation_id=?""",
                (pack_id, activation_id),
            ).fetchall()
            promoted = 0
            for row in rows:
                cfg = row['config'] if isinstance(row, dict) else row[1]
                if isinstance(cfg, str):
                    try:
                        cfg = json.loads(cfg or '{}')
                    except Exception:
                        cfg = {}
                cfg = dict(cfg or {})
                if cfg.get('initialization_pending') is False:
                    continue
                cfg.update({
                    'initialization_pending': False,
                    'initialization_status': 'completed',
                    'execution_owner': 'menu_scheduler',
                })
                cur.execute(
                    """UPDATE scheduled_tasks
                       SET is_active=TRUE, next_run=?, config=?, updated_at=datetime('now','localtime')
                       WHERE id=? AND industry_pack_id=? AND activation_id=?""",
                    (next_run, json.dumps(cfg, ensure_ascii=False), int(row['id'] if isinstance(row, dict) else row[0]), pack_id, activation_id),
                )
                promoted += cur.rowcount
            db.connection.commit()
            return {
                'finalized': True,
                'activation_id': activation_id,
                'industry_pack_id': pack_id,
                'promoted_schedule_count': promoted,
                'scan_report_status': (report or {}).get('status', 'completed'),
            }
        except Exception:
            db.connection.rollback()
            raise
        finally:
            cur.close()


def enqueue_financial_catchup(*, gap_from: str, gap_to: str, activation_id: str) -> Dict:
    """Queue a live market snapshot/overview when an industry changes.

    These jobs are intentionally independent of article keywords: the market
    card must be refreshed immediately even when the new pack has no news.
    """
    token = f"industry-catchup:{activation_id}:{gap_from}:{gap_to}"
    jobs = []
    for job_type, priority in (("financial_snapshot", 35), ("market_overview", 25)):
        job_id, created = intel_repository.enqueue_job_once(
            job_type,
            f"{token}:{job_type}",
            {
                "trigger": "industry_activation_catchup",
                "activation_id": str(activation_id or ''),
                "gap_from": str(gap_from or ''),
                "gap_to": str(gap_to or ''),
                "schedule_window": f"industry:{activation_id}",
                "requested_at_utc": datetime.now(timezone.utc).isoformat(),
            },
            priority=priority,
            max_attempts=3,
            created_by="industry_collection_runtime",
        )
        jobs.append({'job_id': job_id, 'job_type': job_type, 'created': created})
    return {'created': sum(1 for job in jobs if job['created']), 'jobs': jobs}


def initialize_industry_collection(
    result: Dict,
    *,
    request_id: str,
    created_by: str,
    dedupe_prefix: str,
) -> Dict:
    """Project UI tasks and enqueue one real all-source initialization scan."""

    pack_id = str(result['active_pack_id'])
    activation_id = str(result['activation_id'])
    active = active_industry_composition_service.snapshot()
    if str(active['active_industry_pack_id']) != pack_id:
        raise RuntimeError('初始化采集目标不是当前激活行业包')
    if str(active.get('active_industry_activation_id') or '') != activation_id:
        raise RuntimeError('初始化采集激活批次已经失效')

    collection = dict(result.get('collection_tasks') or {})
    if not collection:
        collection = intel_source_registry.project_effective_sources_to_collection_tasks(
            pack_id,
            effective_pack_ids=active['effective_pack_ids'],
            activation_id=activation_id,
            industry_pack_version_id=result['active_version_id'],
            project_keywords=active.get('project_keywords') or [],
            initialization_from=(result.get('initialization_window') or {}).get('from', ''),
            initialization_to=(result.get('initialization_window') or {}).get('to', ''),
        )
        result['collection_tasks'] = collection
    source_ids = list(collection.get('source_ids') or [])
    job_id, created = intel_repository.enqueue_job(
        'light_scan',
        f"{dedupe_prefix}:{pack_id}:{activation_id}",
        {
            'industry_pack_id': pack_id,
            'industry_pack_version_id': result['active_version_id'],
            'activation_id': activation_id,
            'source_ids': source_ids,
            'scan_sources': bool(source_ids),
            'include_serpapi': True,
            'max_sources': max(1, len(source_ids)),
            'manual': True,
            'switch_full_scan': True,
            'force_rescan_key': str(request_id or activation_id),
            'initialization_from': (result.get('initialization_window') or {}).get('from', ''),
            'initialization_to': (result.get('initialization_window') or {}).get('to', ''),
            'backfill_mode': 'historical_window',
            'crawl_task_ids_by_source': collection.get(
                'crawl_task_ids_by_source'
            ) or {},
        },
        priority=20,
        request_id=str(request_id or ''),
        created_by=str(created_by or ''),
    )
    result.update(
        {
            'initial_scan_job_id': job_id,
            'initial_scan_job_created': bool(created),
            'initial_scan_source_count': len(source_ids),
            'initial_scan_mode': 'all_effective_enabled_sources',
            'financial_catchup': enqueue_financial_catchup(
                gap_from=(result.get('initialization_window') or {}).get('from', ''),
                gap_to=(result.get('initialization_window') or {}).get('to', ''),
                activation_id=activation_id,
            ),
        }
    )
    # 激活后立即重投影主题，闭合「改主题 → 发布 → 激活 → 首页生效」。
    # 首页主题卡取自 intel_topics 投影表，而投影读的是「已激活版本」
    # （industry_packs.published_manifest_for_loader 优先用 active_industry_pack_version_id，
    # 只有该包没有激活版本时才回退到 latest_published）。所以发布了新版本却不激活、
    # 或激活了却不重投影，intel_topics 里都还是上一版的主题，首页会一直显示旧主题，
    # 只能靠人工补跑 cluster。这里按 activation 维度去重入队，重复激活不会重复投影。
    # manual=True：显式激活属于人为动作，不受 INTEL_TOPIC_CLUSTER_ENABLED 开关影响。
    # 整段包在 try 里：此处激活已经落库生效，若投影入队失败不应把激活结果报成失败，
    # 只记日志，等 worker 的周期任务或人工补跑即可。
    try:
        topic_job_id, topic_created = intel_repository.enqueue_job(
            'topic_cluster',
            f"topic-cluster:{pack_id}:{activation_id}:activation",
            {
                'industry_pack_id': pack_id,
                'industry_pack_version_id': result['active_version_id'],
                'activation_id': activation_id,
                'manual': True,
            },
            priority=20,
            request_id=str(request_id or ''),
            created_by=str(created_by or ''),
        )
        result.update(
            {
                'topic_cluster_job_id': topic_job_id,
                'topic_cluster_job_created': bool(topic_created),
            }
        )
    except Exception as exc:  # noqa: BLE001 - 投影入队失败不得影响激活结果
        result.update({'topic_cluster_job_error': str(exc)[:200]})
    return result

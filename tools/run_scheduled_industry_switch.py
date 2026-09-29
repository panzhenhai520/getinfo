#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Perform a backed-up industry switch and persist a post-switch isolation audit."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industry_collection_runtime import initialize_industry_collection
from industry_pack_activation import industry_pack_activation_service
from industry_pack_runtime import active_industry_composition_service
from industry_packs import industry_anchor_keywords, industry_pack_loader, normalize_intel_text
from intel_database import intel_repository
from intel_sources import intel_source_registry
from sqlite_database import sqlite_db


def _post_switch_audit(expected_pack_id: str, activation_result: dict) -> dict:
    active = active_industry_composition_service.snapshot()
    pack_id = str(active['active_industry_pack_id'])
    activation_id = str(active.get('active_industry_activation_id') or '')
    articles, article_total = sqlite_db.get_articles(
        1, 10000,
        industry_pack_id=pack_id,
        activation_id=activation_id,
    )
    statistics = sqlite_db.get_statistics(
        industry_pack_id=pack_id,
        activation_id=activation_id,
    )
    schedules, schedule_total = sqlite_db.get_scheduled_tasks(
        1, 10000, industry_pack_id=pack_id
    )
    crawl_tasks, crawl_total = sqlite_db.get_crawl_tasks(
        1, 10000, industry_pack_id=pack_id
    )
    source_ids = intel_source_registry.effective_enabled_source_ids(pack_id)
    projected_target_source_ids = {
        int(value)
        for value in (
            (activation_result.get('collection_tasks') or {}).get('target_source_ids')
            or source_ids
        )
    }
    managed_urls, managed_total = sqlite_db.get_managed_urls(
        page=1,
        per_page=10000,
        industry_pack_id=pack_id,
        effective_pack_ids=active['effective_pack_ids'],
    )
    map_points = sqlite_db.get_article_spacetime_points(
        min_confidence=0.1,
        limit=5000,
        industry_pack_id=pack_id,
        activation_id=activation_id,
    )
    raw_keyword_map = sqlite_db.get_keyword_map(
        5000,
        industry_pack_id=pack_id,
        activation_id=activation_id,
    )
    pack = industry_pack_loader.load(pack_id)
    allowed_keywords = {
        normalize_intel_text(item)
        for item in (
            industry_anchor_keywords(pack)
            + list(pack.get('core_keywords') or [])
            + list(pack.get('expanded_keywords') or [])
        )
        if normalize_intel_text(item)
    }
    exposed_keyword_map = [
        item for item in raw_keyword_map
        if normalize_intel_text(item.get('keyword')) in allowed_keywords
    ]
    visible_ids = [int(item['id']) for item in articles]
    wrong_projection_rows = 0
    if visible_ids:
        placeholders = ','.join('?' for _ in visible_ids)
        wrong_projection_rows = int(
            sqlite_db.connection.execute(
                f"""
                SELECT COUNT(*) FROM articles a
                WHERE a.id IN ({placeholders})
                  AND NOT EXISTS (
                    SELECT 1 FROM article_intel_classifications c
                    WHERE c.article_id=a.id AND c.industry_pack_id=?
                      AND c.activation_id=?
                      AND COALESCE(
                        json_array_length(json_extract(c.score_details_json, '$.hits.anchor')),
                        0
                      ) > 0
                  )
                """,
                [*visible_ids, pack_id, activation_id],
            ).fetchone()[0]
        )
    checks = {
        'active_pack_matches': pack_id == str(expected_pack_id),
        'active_activation_matches': activation_id == str(activation_result['activation_id']),
        'backup_integrity': str(activation_result.get('backup', {}).get('integrity')) == 'ok',
        'legacy_projection_has_no_cross_pack_rows': wrong_projection_rows == 0,
        'statistics_matches_visible_articles': int(statistics.get('total_articles') or 0) == article_total,
        'schedule_rows_are_current_pack': all(str(item.get('industry_pack_id')) == pack_id for item in schedules),
        'crawl_rows_are_current_pack': all(str(item.get('industry_pack_id')) == pack_id for item in crawl_tasks),
        'all_configured_sources_have_schedules': {
            int((item.get('config') or {}).get('intel_source_id'))
            for item in schedules
            if str(item.get('ownership_type') or '') == 'source_registry_projection'
               and (item.get('config') or {}).get('intel_source_id')
        } == projected_target_source_ids,
        'initial_scan_covers_all_effective_sources': int(
            activation_result.get('initial_scan_source_count') or 0
        ) == len(source_ids),
        'map_points_are_current_pack': all(
            str(item.get('industry_pack_id') or '') == pack_id
            and str(item.get('activation_id') or '') == activation_id
            for item in map_points
        ),
        'keyword_graph_uses_current_pack_allowlist': all(
            normalize_intel_text(item.get('keyword')) in allowed_keywords
            for item in exposed_keyword_map
        ),
    }
    return {
        'audited_at': datetime.now(timezone.utc).isoformat(),
        'expected_pack_id': str(expected_pack_id),
        'active_pack_id': pack_id,
        'activation_id': activation_id,
        'passed': all(checks.values()),
        'checks': checks,
        'counts': {
            'visible_articles': article_total,
            'visible_domains': int(statistics.get('domains') or 0),
            'scheduled_tasks': schedule_total,
            'crawl_tasks': crawl_total,
            'effective_enabled_sources': len(source_ids),
            'configured_target_sources': len(projected_target_source_ids),
            'managed_urls': managed_total,
            'map_points': len(map_points),
            'keyword_nodes': len(exposed_keyword_map),
            'wrong_projection_rows': wrong_projection_rows,
        },
        'root_cause_if_failed': {
            'article_or_statistics': '检查 article_intel_classifications 的行业、激活批次和 anchor 门控',
            'schedule_or_crawl': '检查 canonical source 到 scheduled_tasks/crawl_tasks 的激活投影',
            'map': '检查地图查询是否复用了当前行业分类和激活批次',
            'keyword_graph': '检查图谱是否读取分类关键词且使用当前行业包白名单',
        },
        'backup': activation_result.get('backup') or {},
        'initial_scan_job_id': activation_result.get('initial_scan_job_id'),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--industry-pack-id', required=True)
    parser.add_argument(
        '--audit-dir',
        default=str(PROJECT_ROOT / 'data' / 'industry_switch_audits'),
    )
    parser.add_argument('--actor', default='scheduled-industry-switch')
    parser.add_argument(
        '--audit-only', action='store_true',
        help='Audit the current activation without performing another switch.',
    )
    args = parser.parse_args()

    pack_id = str(args.industry_pack_id).strip()
    if args.audit_only:
        active = active_industry_composition_service.snapshot()
        if str(active['active_industry_pack_id']) != pack_id:
            raise RuntimeError('当前激活行业与审计目标不一致')
        activation_id = str(active.get('active_industry_activation_id') or '')
        activation = sqlite_db.connection.execute(
            "SELECT * FROM industry_pack_activations WHERE id=?",
            (activation_id,),
        ).fetchone()
        if not activation:
            raise RuntimeError('当前激活记录不存在')
        activation = dict(activation)
        schedule_rows, _ = sqlite_db.get_scheduled_tasks(
            1, 10000, industry_pack_id=pack_id
        )
        target_source_ids = [
            int((item.get('config') or {}).get('intel_source_id'))
            for item in schedule_rows
            if str(item.get('ownership_type') or '') == 'source_registry_projection'
               and (item.get('config') or {}).get('intel_source_id')
        ]
        scan_job = sqlite_db.connection.execute(
            """
            SELECT id, payload_json FROM intel_jobs
            WHERE job_type='light_scan'
              AND json_extract(payload_json, '$.activation_id')=?
              AND COALESCE(json_extract(payload_json, '$.switch_full_scan'), 0)=1
            ORDER BY id DESC LIMIT 1
            """,
            (activation_id,),
        ).fetchone()
        scan_payload = json.loads(scan_job['payload_json'] or '{}') if scan_job else {}
        result = {
            'activation_id': activation_id,
            'active_pack_id': pack_id,
            'active_version_id': active.get('active_industry_pack_version_id'),
            'backup': {
                'path': activation.get('backup_path'),
                'sha256': activation.get('backup_sha256'),
                'size': activation.get('backup_size'),
                'schema_version': activation.get('backup_schema_version'),
                'integrity': activation.get('backup_integrity'),
            },
            'collection_tasks': {'target_source_ids': target_source_ids},
            'initial_scan_job_id': int(scan_job['id']) if scan_job else None,
            'initial_scan_source_count': len(scan_payload.get('source_ids') or []),
        }
    else:
        preview = industry_pack_activation_service.preview(pack_id)
        result = industry_pack_activation_service.activate(
            pack_id,
            target_version_id=int(preview['target_version_id']),
            expected_plan_sha256=str(preview['plan_sha256']),
            actor=str(args.actor),
        )
        initialize_industry_collection(
            result,
            request_id=f"scheduled-switch-{uuid.uuid4().hex}",
            created_by=str(args.actor),
            dedupe_prefix='scheduled-industry-switch-full-scan',
        )
    audit = _post_switch_audit(pack_id, result)
    audit_dir = Path(args.audit_dir).expanduser().resolve()
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / (
        datetime.now().strftime('%Y%m%d-%H%M%S') + f'-{pack_id}.json'
    )
    audit_path.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True),
        encoding='utf-8',
    )
    print(json.dumps({**audit, 'audit_path': str(audit_path)}, ensure_ascii=False))
    return 0 if audit['passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())

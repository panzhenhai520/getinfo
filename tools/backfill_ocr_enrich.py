#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""存量修复：给从未成功精炼过的 vpn_ocr 文章重新入队 enrich 任务。

背景：2026-09-03 ~ 09-14 期间 enrich 任务因代码 bug（'IntelWorker' object has no
attribute 'db'）全线失败，导致 OCR 原文（含侧栏/推荐阅读等噪声）直接入库展示。
本脚本把这些文章重新入队 enrich（worker 每轮消化一批），成功后 content 会被
VPN LLM 的 refined_content 精炼替换。

用法:
    python tools/backfill_ocr_enrich.py --dry-run       # 只看范围
    python tools/backfill_ocr_enrich.py --limit 200     # 入队前 200 篇
    python tools/backfill_ocr_enrich.py                 # 全部入队
"""
import argparse
import sys

sys.path.insert(0, ".")

from db_connection import connect_postgres_primary
from intel_database import IntelRepository


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="最多入队多少篇（0=不限）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不实际入队")
    args = parser.parse_args()

    conn = connect_postgres_primary()
    cur = conn.cursor()
    # vpn_ocr 文章，且没有任何成功 enrich 任务（payload 里精确匹配 article_id）
    cur.execute("""
        SELECT a.id, a.title, a.url, a.content
        FROM articles a
        WHERE a.status='active' AND a.extraction_method='vpn_ocr'
          AND NOT EXISTS (
              SELECT 1 FROM intel_jobs j
              WHERE j.job_type='enrich'
                AND j.status='completed'
                AND j.result_json LIKE '%"success": true%'
                AND j.payload_json LIKE '%"article_id": ' || a.id || ',%'
          )
        ORDER BY a.id
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    print(f"待修复 vpn_ocr 文章: {len(rows)}", flush=True)

    if args.dry_run:
        for r in rows[: min(args.limit or len(rows), 30)]:
            print(f"  dry-run: article {r['id']} {str(r['title'])[:48]}", flush=True)
        return

    repo = IntelRepository()
    queued = 0
    targets = rows[: args.limit] if args.limit else rows
    for r in targets:
        aid = int(r["id"])
        payload = {
            "article_id": aid,
            "url": str(r["url"] or ""),
            "title": str(r["title"] or ""),
            "content": str(r["content"] or ""),
            "keywords": [],
            "task_id": f"backfill-ocr-enrich:{aid}",
        }
        try:
            _jid, created = repo.enqueue_job(
                "enrich", f"enrich:{aid}:backfill-ocr-enrich", payload
            )
            if created:
                queued += 1
        except Exception as exc:
            print(f"  enqueue fail article {aid}: {exc}", flush=True)
    print(f"入队完成: {queued}/{len(targets)}", flush=True)


if __name__ == "__main__":
    main()

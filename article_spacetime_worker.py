#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Backfill and refresh derived article spacetime profiles."""

from __future__ import annotations

import argparse
import time

from article_spacetime_analyzer import analyze_article_spacetime
from sqlite_database import sqlite_db


def run_worker(mode: str, limit: int, min_confidence: float, dry_run: bool = False) -> dict:
    started = time.time()
    if not sqlite_db.connect():
        raise RuntimeError('无法连接 SQLite 数据库')
    sqlite_db.create_tables()

    articles = sqlite_db.get_articles_for_spacetime_analysis(
        mode=mode,
        limit=limit,
        min_confidence=min_confidence,
    )
    analyzed = 0
    saved = 0
    failed = 0

    for article in articles:
        article_id = article.get('id')
        try:
            profile = analyze_article_spacetime(article)
            analyzed += 1
            if profile.get('spacetime_status') == 'failed':
                failed += 1
            if not dry_run:
                if sqlite_db.save_article_spacetime_profile(article_id, profile):
                    saved += 1
                else:
                    failed += 1
        except Exception as exc:
            failed += 1
            print(f"⚠️ 分析失败 article_id={article_id}: {exc}")

    elapsed = time.time() - started
    result = {
        'mode': mode,
        'selected': len(articles),
        'analyzed': analyzed,
        'saved': saved,
        'failed': failed,
        'duration_seconds': round(elapsed, 3),
        'avg_seconds': round(elapsed / analyzed, 4) if analyzed else 0,
        'dry_run': dry_run,
    }
    print(
        "时空画像 worker 完成: "
        f"mode={result['mode']} selected={result['selected']} analyzed={result['analyzed']} "
        f"saved={result['saved']} failed={result['failed']} duration={result['duration_seconds']}s "
        f"avg={result['avg_seconds']}s dry_run={result['dry_run']}"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description='Article spacetime profile backfill worker')
    parser.add_argument('--mode', choices=['backfill', 'incremental', 'low-confidence', 'failed'], default='incremental')
    parser.add_argument('--limit', type=int, default=500)
    parser.add_argument('--min-confidence', type=float, default=0.4)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    run_worker(
        mode=args.mode,
        limit=args.limit,
        min_confidence=args.min_confidence,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

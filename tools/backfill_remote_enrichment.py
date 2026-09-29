#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backfill remote LLM summary/translation/audio for existing local articles.

Historical articles were ingested by the local static crawler before the remote
pipeline enrichment path was wired into CandidateCrawlerAdapter.  This command
replays those article URLs through the Ubuntu collectinfo-pipeline with the
same enrich/tts/voice flags as new crawls and attaches the produced derivative
and audio manifest to the existing article_id.

Usage:
    python tools/backfill_remote_enrichment.py --dry-run
    python tools/backfill_remote_enrichment.py --limit 3
    python tools/backfill_remote_enrichment.py --ids 1,2,3
    python tools/backfill_remote_enrichment.py --force
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import json
from typing import Any

import config
from sqlite_database import _parse_matched_keyword_text, sqlite_db
from remote_result_ingestor import (
    attach_remote_result_to_article,
    ensure_remote_pipeline_schema,
)
from remote_pipeline_client import (
    RemotePipelineError,
    RemotePipelineUnavailable,
    remote_pipeline_client,
)


def _parse_keywords(value: Any) -> list[str]:
    parsed = _parse_matched_keyword_text(value)
    return [item for item in parsed if item]


def _iter_articles(
    *,
    limit: int | None,
    ids: list[int] | None,
    only_missing: bool,
    force: bool,
):
    with sqlite_db.lock:
        sqlite_db._ensure_connection()
        connection = sqlite_db.connection
        ensure_remote_pipeline_schema(sqlite_db)
        params: list[Any] = []
        where = ["COALESCE(a.status, 'active') = 'active'"]
        if ids:
            where.append(f"a.id IN ({','.join('?' for _ in ids)})")
            params.extend(ids)
        if only_missing and not force:
            where.append(
                """(
                    NOT EXISTS (SELECT 1 FROM article_derivatives d WHERE d.article_id = a.id)
                    OR NOT EXISTS (SELECT 1 FROM article_audio_manifests m WHERE m.article_id = a.id)
                )"""
            )
        query = (
            "SELECT a.id, a.url, a.title, a.content, a.matched_keywords, "
            "a.source_task_id FROM articles a WHERE "
            + " AND ".join(where)
            + " ORDER BY a.id"
        )
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        return connection.execute(query, params).fetchall()


def _has_derivative(article_id: int) -> bool:
    with sqlite_db.lock:
        sqlite_db._ensure_connection()
        row = sqlite_db.connection.execute(
            "SELECT 1 FROM article_derivatives WHERE article_id=?", (article_id,)
        ).fetchone()
        return bool(row)


def _has_audio(article_id: int) -> bool:
    with sqlite_db.lock:
        sqlite_db._ensure_connection()
        row = sqlite_db.connection.execute(
            "SELECT 1 FROM article_audio_manifests WHERE article_id=?", (article_id,)
        ).fetchone()
        return bool(row)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print targets without submitting jobs")
    parser.add_argument("--limit", type=int, default=None, help="maximum number of articles to process")
    parser.add_argument("--ids", default="", help="comma separated article ids to process")
    parser.add_argument("--only-missing", action="store_true", default=True, help="skip articles that already have both derivative and audio")
    parser.add_argument("--force", action="store_true", help="process every selected article even if already enriched")
    args = parser.parse_args()

    ids = [int(item) for item in args.ids.replace("，", ",").split(",") if item.strip()] if args.ids else None

    if not remote_pipeline_client.configured:
        print("remote pipeline is not configured (missing URL or token)")
        return 2

    rows = _iter_articles(limit=args.limit, ids=ids, only_missing=args.only_missing, force=args.force)
    if not rows:
        print("no matching articles")
        return 0

    print(
        f"targets={len(rows)} enrich={config.REMOTE_PIPELINE_ENRICH} "
        f"tts={config.REMOTE_PIPELINE_TTS} voice={config.REMOTE_PIPELINE_TTS_VOICE}"
    )
    if args.dry_run:
        for row in rows:
            print(
                f"[dry-run] article={row['id']} derivative={_has_derivative(row['id'])} "
                f"audio={_has_audio(row['id'])} url={row['url']}"
            )
        return 0

    ok = 0
    skipped = 0
    failed = 0
    for row in rows:
        article_id = int(row["id"])
        url = str(row["url"] or "").strip()
        if not url:
            print(f"[skip] article={article_id} missing url")
            skipped += 1
            continue
        task_id = f"backfill_article_{article_id}"
        keywords = _parse_keywords(row["matched_keywords"])
        try:
            result = remote_pipeline_client.run(
                url=url,
                mode="article",
                keywords=keywords,
                limit=1,
                task_id=task_id,
                enrich=config.REMOTE_PIPELINE_ENRICH,
                tts=config.REMOTE_PIPELINE_TTS,
                voice=config.REMOTE_PIPELINE_TTS_VOICE,
            )
            attached = attach_remote_result_to_article(sqlite_db, article_id, result)
            if attached:
                ok += 1
                print(f"[ok] article={article_id} job={result.get('job_id')} attached={attached}")
            else:
                failed += 1
                print(f"[warn] article={article_id} no derivative/audio returned")
        except (RemotePipelineUnavailable, RemotePipelineError) as exc:
            failed += 1
            print(f"[error] article={article_id} {exc}")
            print("remote pipeline unavailable; aborting remaining backfill")
            break

    print(f"done ok={ok} skipped={skipped} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

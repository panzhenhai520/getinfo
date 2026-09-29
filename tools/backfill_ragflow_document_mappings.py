#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Backfill exact article -> RAGFlow document mappings by stable document name.

Default mode is dry-run. Use --apply to write exact unique matches. With
--upload-missing, articles not found in RAGFlow are uploaded and parsed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ragflow_client import RagflowClient
from sqlite_database import sqlite_db


def iter_articles(
    limit: int = 500,
    offset: int = 0,
    since: str = '',
    until: str = '',
    matched_only: bool = False,
):
    sqlite_db._ensure_connection()
    cur = sqlite_db.connection.cursor()
    try:
        conditions = [
            "COALESCE(status, 'active') != 'deleted'",
            "content IS NOT NULL",
            "TRIM(content) != ''",
        ]
        params = []
        if since:
            conditions.append("created_at >= ?")
            params.append(since)
        if until:
            conditions.append("created_at < ?")
            params.append(until)
        if matched_only:
            conditions.append("matched_keywords IS NOT NULL")
            conditions.append("TRIM(matched_keywords) != ''")

        sql = f"""
            SELECT id, title, url, content
            FROM articles
            WHERE {' AND '.join(conditions)}
            ORDER BY id ASC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]
    finally:
        cur.close()


def has_mapping(article_id: int, kb_id: str) -> bool:
    return bool(sqlite_db.get_article_ragflow_documents(article_id=article_id, kb_id=kb_id))


def main():
    parser = argparse.ArgumentParser(description='Backfill RAGFlow document mappings by exact document name.')
    parser.add_argument('--kb-id', required=True, help='RAGFlow dataset/knowledge-base id')
    parser.add_argument('--limit', type=int, default=500)
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--since', default='', help='Only articles created at or after this timestamp/date')
    parser.add_argument('--until', default='', help='Only articles created before this timestamp/date')
    parser.add_argument('--matched-only', action='store_true', help='Only articles with matched_keywords')
    parser.add_argument('--apply', action='store_true', help='Write exact unique matches to article_ragflow_documents')
    parser.add_argument('--upload-missing', action='store_true', help='Upload articles not found in RAGFlow and trigger parse')
    parser.add_argument('--include-mapped', action='store_true', help='Also check articles that already have mappings')
    args = parser.parse_args()
    if args.upload_missing and not args.apply:
        parser.error('--upload-missing writes to RAGFlow and must be used with --apply')

    client = RagflowClient()
    stats = {
        'checked': 0,
        'matched': 0,
        'written': 0,
        'skipped_existing_mapping': 0,
        'not_found': 0,
        'uploaded_missing': 0,
        'parse_submitted': 0,
        'parse_failed': 0,
        'ambiguous': 0,
        'errors': 0,
    }

    for article in iter_articles(
        limit=args.limit,
        offset=args.offset,
        since=args.since,
        until=args.until,
        matched_only=args.matched_only,
    ):
        article_id = article.get('id')
        if not args.include_mapped and has_mapping(article_id, args.kb_id):
            stats['skipped_existing_mapping'] += 1
            continue

        stats['checked'] += 1
        file_name = client.build_document_name(article.get('title'), article.get('url'), article_id)
        try:
            docs = client.find_documents_by_name(args.kb_id, file_name)
        except Exception as exc:
            stats['errors'] += 1
            print(f"ERROR article_id={article_id} name={file_name}: {exc}")
            continue

        if not docs:
            stats['not_found'] += 1
            if args.upload_missing:
                try:
                    upload_result = client.upload_document_content(
                        args.kb_id,
                        file_name,
                        article.get('content') or '',
                    )
                    doc_ids = client.extract_document_ids(upload_result)
                    parse_result = upload_result.get('parse_result') or {}
                    parse_ok = bool(parse_result) and parse_result.get('code') == 0
                    sync_status = 'parsed' if parse_ok else 'uploaded'
                    if parse_ok:
                        stats['parse_submitted'] += 1
                    elif upload_result.get('parse_error'):
                        stats['parse_failed'] += 1
                    if doc_ids:
                        stats['uploaded_missing'] += 1
                        print(f"UPLOAD article_id={article_id} kb_id={args.kb_id} doc_id={doc_ids[0]} name={file_name}")
                        if args.apply:
                            client._record_article_document_mapping(article_id, args.kb_id, doc_ids, file_name, sync_status)
                            stats['written'] += len(doc_ids)
                    else:
                        stats['errors'] += 1
                        print(f"ERROR article_id={article_id} name={file_name}: upload returned no document id")
                except Exception as exc:
                    stats['errors'] += 1
                    print(f"ERROR article_id={article_id} name={file_name}: upload failed: {exc}")
            continue
        if len(docs) != 1:
            stats['ambiguous'] += 1
            print(f"AMBIGUOUS article_id={article_id} name={file_name} matches={len(docs)}")
            continue

        doc_id = docs[0].get('id')
        if not doc_id:
            stats['not_found'] += 1
            continue

        stats['matched'] += 1
        print(f"MATCH article_id={article_id} kb_id={args.kb_id} doc_id={doc_id} name={file_name}")
        if args.apply:
            sync_status = 'parsed' if str(docs[0].get('run') or '').upper() == 'DONE' else 'uploaded'
            ok = sqlite_db.upsert_article_ragflow_document(
                article_id=article_id,
                kb_id=args.kb_id,
                document_id=doc_id,
                document_name=file_name,
                sync_status=sync_status,
            )
            if ok:
                stats['written'] += 1
            else:
                stats['errors'] += 1

    mode = 'APPLY' if args.apply else 'DRY_RUN'
    print(f"{mode} stats: {stats}")


if __name__ == '__main__':
    main()

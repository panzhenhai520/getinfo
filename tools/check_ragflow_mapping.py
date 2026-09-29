#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Acceptance checks for local article -> RAGFlow document mappings."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlite_database import SQLiteDatabase


def main():
    fd, db_path = tempfile.mkstemp(prefix='ragflow_mapping_', suffix='.db')
    os.close(fd)
    try:
        db = SQLiteDatabase(db_path)
        db._ensure_connection()
        db.create_tables()

        article_id = db.insert_article({
            'url': 'https://example.com/a',
            'title': 'Test Article',
            'content': 'Family Office article body',
            'domain': 'example.com',
            'matched_keywords': 'Family Office',
        })
        assert article_id

        ok = db.upsert_article_ragflow_document(
            article_id=article_id,
            kb_id='kb-test',
            document_id='doc-test',
            document_name='Test Article_doc.txt',
            sync_status='uploaded',
        )
        assert ok

        rows = db.get_article_ragflow_documents(article_id=article_id, kb_id='kb-test')
        assert len(rows) == 1
        assert rows[0]['document_id'] == 'doc-test'
        assert rows[0]['sync_status'] == 'uploaded'

        ok = db.upsert_article_ragflow_document(
            article_id=article_id,
            kb_id='kb-test',
            document_id='doc-test',
            document_name='Test Article_doc.txt',
            sync_status='parsed',
        )
        assert ok
        rows = db.get_article_ragflow_documents(article_id=article_id, kb_id='kb-test')
        assert len(rows) == 1
        assert rows[0]['sync_status'] == 'parsed'

        assert db.update_article_ragflow_document_status('kb-test', 'doc-test', 'deleted')
        rows = db.get_article_ragflow_documents(article_id=article_id, kb_id='kb-test')
        assert rows[0]['sync_status'] == 'deleted'

        print('ragflow_mapping checks passed')
    finally:
        try:
            os.remove(db_path)
        except OSError:
            pass


if __name__ == '__main__':
    main()

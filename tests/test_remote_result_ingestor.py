from __future__ import annotations

import sqlite3
import threading

from remote_result_ingestor import attach_remote_result_to_article, ingest_remote_result


class FakeDB:
    def __init__(self):
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.execute('CREATE TABLE articles(id INTEGER PRIMARY KEY AUTOINCREMENT,url TEXT UNIQUE)')

    def _ensure_connection(self):
        return None

    def insert_article(self, article):
        self.connection.execute('INSERT OR IGNORE INTO articles(url) VALUES(?)', (article['url'],))
        row = self.connection.execute('SELECT id FROM articles WHERE url=?', (article['url'],)).fetchone()
        return row['id']

    def link_article_to_task(self, article_id, task_id):
        return True


def test_remote_result_is_idempotent_and_keeps_derivative_separate():
    db = FakeDB()
    result = {
        'job_id': 'remote-1', 'status': 'completed',
        'articles': [{
            'url': 'https://example.com/a', 'title': 'AI manufacturing', 'content': 'AI changes manufacturing quality inspection.',
            'derivative': {
                'source_hash': 'hash', 'source_language': 'en', 'target_language': 'zh',
                'summary': 'summary', 'translated_title': '标题', 'translated_content': '译文',
                'model': 'gemma', 'prompt_version': 'v1',
            },
            'audio_manifest': {'summary': [], 'translation': []},
        }],
    }
    first = ingest_remote_result(db, result, configured_url='https://example.com/news', keywords=['AI'])
    second = ingest_remote_result(db, result, configured_url='https://example.com/news', keywords=['AI'])
    assert first['articles_found'] == second['articles_found'] == 1
    assert db.connection.execute('SELECT count(*) FROM articles').fetchone()[0] == 1
    assert db.connection.execute('SELECT count(*) FROM article_derivatives').fetchone()[0] == 1
    row = db.connection.execute('SELECT translated_content FROM article_derivatives').fetchone()
    assert row[0] == '译文'


def test_attach_remote_result_to_existing_article():
    db = FakeDB()
    article_id = db.insert_article({'url': 'https://example.com/a'})
    result = {
        'job_id': 'remote-2',
        'articles': [{
            'url': 'https://example.com/a', 'title': 'T', 'content': 'C',
            'derivative': {
                'source_hash': 'h', 'source_language': 'zh', 'target_language': 'en',
                'summary': 'S', 'translated_title': 'TT', 'translated_content': 'TC',
                'model': 'm', 'prompt_version': 'v1',
            },
            'audio_manifest': {'summary': [{'kind': 'summary', 'text_hash': 'x'}], 'translation': []},
        }],
    }
    assert attach_remote_result_to_article(db, article_id, result) is True
    assert db.connection.execute('SELECT count(*) FROM article_derivatives').fetchone()[0] == 1
    assert db.connection.execute('SELECT count(*) FROM article_audio_manifests').fetchone()[0] == 1


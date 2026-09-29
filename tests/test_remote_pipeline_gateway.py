from __future__ import annotations

import json
import socket

import pytest

import remote_pipeline.app as gateway


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway, 'API_TOKEN', 't' * 32)
    monkeypatch.setattr(gateway, 'DB_PATH', tmp_path / 'jobs.db')
    monkeypatch.setattr(gateway, 'ARTIFACT_DIR', tmp_path / 'artifacts')
    (tmp_path / 'artifacts').mkdir()
    return gateway.app.test_client()


def _public_dns(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 0))]


def test_health_is_public_but_jobs_require_auth(client):
    assert client.get('/v1/health').status_code == 200
    assert client.post('/v1/pipeline/jobs', json={'url': 'https://example.com'}).status_code == 401


def test_private_url_is_rejected(client, monkeypatch):
    monkeypatch.setattr(gateway.socket, 'getaddrinfo', lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 0))])
    response = client.post(
        '/v1/pipeline/jobs',
        headers={'Authorization': 'Bearer ' + 't' * 32},
        json={'url': 'http://internal.example/'},
    )
    assert response.status_code == 400
    assert 'non-public' in response.get_json()['error']


def test_idempotent_submission(client, monkeypatch):
    monkeypatch.setattr(gateway.socket, 'getaddrinfo', _public_dns)
    monkeypatch.setattr(gateway._executor, 'submit', lambda *_a, **_k: None)
    headers = {'Authorization': 'Bearer ' + 't' * 32}
    payload = {'url': 'https://example.com/news', 'mode': 'list', 'idempotency_key': 'same-key'}
    first = client.post('/v1/pipeline/jobs', headers=headers, json=payload)
    second = client.post('/v1/pipeline/jobs', headers=headers, json=payload)
    assert first.status_code == 202
    assert second.status_code == 200
    assert first.get_json()['job_id'] == second.get_json()['job_id']
    assert second.get_json()['deduplicated'] is True


def test_capabilities_explicitly_excludes_zyte(client, monkeypatch):
    monkeypatch.setattr(gateway, '_component_health', lambda _url: {'ok': True, 'status': 200})
    response = client.get('/v1/capabilities', headers={'Authorization': 'Bearer ' + 't' * 32})
    assert response.status_code == 200
    assert response.get_json()['capabilities']['zyte'] is False


def test_robots_disallow_is_respected(monkeypatch):
    class Response:
        status_code = 200
        text = 'User-agent: *\nDisallow: /private\nAllow: /public\n'
        def raise_for_status(self): return None
    monkeypatch.setattr(gateway.requests, 'get', lambda *_a, **_k: Response())
    assert gateway._robots_allows('https://example.com/public/a')[0] is True
    assert gateway._robots_allows('https://example.com/private/a')[0] is False


def test_unfinished_jobs_are_requeued_after_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway, 'DB_PATH', tmp_path / 'jobs.db')
    submitted = []
    monkeypatch.setattr(gateway._executor, 'submit', lambda fn, job_id, payload: submitted.append((job_id, payload)))
    with gateway._db() as connection:
        connection.execute(
            "INSERT INTO jobs(id,idempotency_key,status,phase,request_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ('recover-1', 'recover-key', 'running', 'crawling', json.dumps({'url': 'https://example.com'}), 1, 1),
        )
    monkeypatch.setattr(gateway, '_recovery_done', False)
    gateway._recover_jobs_once()
    with gateway._db() as connection:
        row = connection.execute("SELECT status,phase FROM jobs WHERE id='recover-1'").fetchone()
    assert (row['status'], row['phase']) == ('queued', 'recovered')
    assert submitted == [('recover-1', {'url': 'https://example.com'})]


def test_invalid_voice_is_rejected(client, monkeypatch):
    monkeypatch.setattr(gateway.socket, 'getaddrinfo', _public_dns)
    monkeypatch.setattr(gateway._executor, 'submit', lambda *_a, **_k: None)
    headers = {'Authorization': 'Bearer ' + 't' * 32}
    response = client.post(
        '/v1/pipeline/jobs',
        headers=headers,
        json={'url': 'https://example.com/news', 'mode': 'list', 'voice': 'bad voice!'},
    )
    assert response.status_code == 400
    assert 'voice' in response.get_json()['error']


def test_publish_date_prefers_metadata_and_supports_time_element():
    assert gateway._published_at({'article:published_time': '2026-08-20T12:00:00Z'}, '') == '2026-08-20T12:00:00Z'
    assert gateway._published_at({}, '<time datetime="2026-08-21">today</time>') == '2026-08-21'

def test_summary_audio_manifest_survives_translation_failure(monkeypatch):
    captured = []
    monkeypatch.setattr(gateway, '_set_job', lambda *a, **kw: captured.append((a, kw)))
    monkeypatch.setattr(gateway, '_event', lambda *a, **kw: None)
    monkeypatch.setattr(gateway, '_crawl', lambda url: {
        'success': True, 'url': 'https://example.com/a', 'title': 'T', 'content': 'C',
        'links': [], 'publish_date': '', 'robots': '',
    })

    def fake_enrich(article, on_summary=None):
        if on_summary is not None:
            on_summary({'summary': 'S', 'source_language': 'zh', 'target_language': 'en'})
        raise ValueError('translation failed')

    monkeypatch.setattr(gateway, '_enrich', fake_enrich)
    monkeypatch.setattr(gateway, '_generate_audio', lambda job_id, index, text, language, kind, voice='default': [{'kind': kind, 'text_hash': 'h'}])
    gateway._run_job('job-1', {'url': 'https://example.com/a', 'mode': 'article', 'enrich': True, 'tts': True, 'limit': 10})

    completed = [kw for _a, kw in captured if kw.get('result')]
    assert completed, 'no completed result captured'
    article = completed[-1]['result']['articles'][0]
    assert article.get('derivative_error') == 'translation failed'
    assert article['audio_manifest']['summary'][0]['kind'] == 'summary'

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


def _no_executor(monkeypatch):
    """把抓取队列的 submit 变成空实现：用例只验证接口语义，不需要真跑 job。

    网关按能力拆成 _crawl_pool/_llm_pool/_tts_pool（capabilities.queues 也是这三个），
    提交入口用的是 _crawl_pool.submit(_run_job, ...)。早期用例里写的 _executor 早已不存在。
    """
    monkeypatch.setattr(gateway._crawl_pool, 'submit', lambda *_a, **_k: None)


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
    _no_executor(monkeypatch)
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
    # 能力清单现在只声明网关自有的抓取/提炼/翻译/语音能力（crawl4ai/javascript/...），
    # zyte 是已停用的外部服务（config.ZYTE_ENABLED=False），不再出现在清单里。
    # 断言意图不变：网关不得对外声明 zyte 可用。
    assert 'zyte' not in response.get_json()['capabilities']


def test_robots_disallow_is_respected(monkeypatch):
    class Response:
        status_code = 200
        text = 'User-agent: *\nDisallow: /private\nAllow: /public\n'
        def raise_for_status(self): return None
    monkeypatch.setattr(gateway.requests, 'get', lambda *_a, **_k: Response())
    assert gateway._robots_allows('https://example.com/public/a')[0] is True
    assert gateway._robots_allows('https://example.com/private/a')[0] is False


def test_unfinished_jobs_are_requeued_after_restart(tmp_path, monkeypatch):
    # 产品口径（2026-10-07 确认）：未完成的作业要"重新入队重跑"——排队中（还没执行）的必须去执行，
    # 正在跑的重新跑一遍；已经失败的保持失败，不复活。重做次数有上限（RECOVERY_MAX_ATTEMPTS）。
    monkeypatch.setattr(gateway, 'DB_PATH', tmp_path / 'jobs.db')
    submitted = []
    monkeypatch.setattr(gateway._crawl_pool, 'submit', lambda fn, job_id, payload: submitted.append((job_id, payload)))
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


def test_queued_job_that_never_ran_is_executed_after_restart(tmp_path, monkeypatch):
    """排队中、一次都没跑过的作业：重启后必须真的被执行，而不是被静默丢掉。"""
    monkeypatch.setattr(gateway, 'DB_PATH', tmp_path / 'jobs.db')
    submitted = []
    monkeypatch.setattr(gateway._crawl_pool, 'submit', lambda fn, job_id, payload: submitted.append((job_id, payload)))
    with gateway._db() as connection:
        connection.execute(
            "INSERT INTO jobs(id,idempotency_key,status,phase,request_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ('queued-1', 'queued-key', 'queued', 'queued', json.dumps({'url': 'https://example.com/never-ran'}), 1, 1),
        )
    monkeypatch.setattr(gateway, '_recovery_done', False)
    gateway._recover_jobs_once()
    with gateway._db() as connection:
        row = connection.execute("SELECT status,phase,attempts FROM jobs WHERE id='queued-1'").fetchone()
    assert row['status'] == 'queued'
    assert row['phase'] == 'recovered'
    assert int(row['attempts']) == 1, "重做次数要记账，避免反复重启无限重跑"
    assert submitted == [('queued-1', {'url': 'https://example.com/never-ran'})]


def test_failed_jobs_stay_failed_and_are_not_resubmitted(tmp_path, monkeypatch):
    """已经失败的作业按失败处理：不复活、不重跑。"""
    monkeypatch.setattr(gateway, 'DB_PATH', tmp_path / 'jobs.db')
    submitted = []
    monkeypatch.setattr(gateway._crawl_pool, 'submit', lambda fn, job_id, payload: submitted.append(job_id))
    with gateway._db() as connection:
        connection.execute(
            "INSERT INTO jobs(id,idempotency_key,status,phase,request_json,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ('failed-1', 'failed-key', 'failed', 'failed', json.dumps({'url': 'https://example.com/bad'}),
             'boom', 1, 1),
        )
    monkeypatch.setattr(gateway, '_recovery_done', False)
    gateway._recover_jobs_once()
    with gateway._db() as connection:
        row = connection.execute("SELECT status,phase,error FROM jobs WHERE id='failed-1'").fetchone()
    assert row['status'] == 'failed'
    assert row['phase'] == 'failed'
    assert row['error'] == 'boom'
    assert submitted == [], "失败作业不得被重新提交"


def test_recovery_gives_up_after_attempt_limit(tmp_path, monkeypatch):
    """重做次数用尽：判失败并写明原因，避免"重启一次跑一次"的活锁。"""
    monkeypatch.setattr(gateway, 'DB_PATH', tmp_path / 'jobs.db')
    submitted = []
    monkeypatch.setattr(gateway._crawl_pool, 'submit', lambda fn, job_id, payload: submitted.append(job_id))
    with gateway._db() as connection:
        connection.execute(
            "INSERT INTO jobs(id,idempotency_key,status,phase,request_json,attempts,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ('tired-1', 'tired-key', 'running', 'crawling', json.dumps({'url': 'https://example.com/tired'}),
             gateway.RECOVERY_MAX_ATTEMPTS, 1, 1),
        )
    monkeypatch.setattr(gateway, '_recovery_done', False)
    gateway._recover_jobs_once()
    with gateway._db() as connection:
        row = connection.execute("SELECT status,phase,error FROM jobs WHERE id='tired-1'").fetchone()
    assert row['status'] == 'failed'
    assert row['phase'] == 'recovered'
    assert 'recovery limit reached' in row['error']
    assert submitted == []


def test_invalid_voice_is_rejected(client, monkeypatch):
    monkeypatch.setattr(gateway.socket, 'getaddrinfo', _public_dns)
    _no_executor(monkeypatch)
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

def test_summary_audio_manifest_survives_translation_failure(tmp_path, monkeypatch):
    """翻译失败不能带走音频清单：精炼->翻译->语音是三条独立管线，音频要照常生成。

    早期用例打的是 gateway._enrich(on_summary=...)：那时的网关是「一次调用返回
    summary/translation/audio」的单函数结构，错误字段叫 derivative_error、音频放在
    audio_manifest['summary']。现在的网关是 _process_article 里的
    refine(_refine_article) -> translate(_translate_refined) -> audio(_generate_audio)
    三条管线，音频键是 'refined'、翻译错误字段是 'translation_error'。
    断言意图（翻译失败仍然产出音频清单）保持不变。
    """
    captured = []
    monkeypatch.setattr(gateway, 'DB_PATH', tmp_path / 'jobs.db')
    monkeypatch.setattr(gateway, 'ARTIFACT_DIR', tmp_path / 'artifacts')
    (tmp_path / 'artifacts').mkdir()
    monkeypatch.setattr(gateway, 'TTS_ENABLED', True)
    monkeypatch.setattr(gateway, '_set_job', lambda *a, **kw: captured.append((a, kw)))
    monkeypatch.setattr(gateway, '_event', lambda *a, **kw: None)
    monkeypatch.setattr(gateway, '_crawl', lambda url: {
        'success': True, 'url': 'https://example.com/a', 'title': 'T', 'content': 'C',
        'links': [], 'publish_date': '', 'robots': '',
    })
    monkeypatch.setattr(gateway, '_refine_article', lambda article, **kw: {
        'refined_content': '精炼正文', 'refined_title': 'T', 'source_language': 'zh', 'model': 'm',
    })

    def failing_translate(*_args, **_kwargs):
        raise ValueError('translation failed')

    monkeypatch.setattr(gateway, '_translate_refined', failing_translate)
    # 真实实现会指数退避重试 8 次（10s 起步），测试里只关心失败后的降级行为
    monkeypatch.setattr(gateway, '_retry_until_success', lambda fn, *a, **kw: fn(*a, **kw))
    monkeypatch.setattr(gateway, '_generate_audio', lambda job_id, index, text, language, kind, voice='default', max_parts=0: [{'kind': kind, 'text_hash': 'h'}])
    gateway._run_job('job-1', {'url': 'https://example.com/a', 'mode': 'article', 'enrich': True, 'tts': True, 'limit': 10})

    completed = [kw for _a, kw in captured if kw.get('result')]
    assert completed, 'no completed result captured'
    article = completed[-1]['result']['articles'][0]
    assert article.get('translation_error') == 'translation failed'
    assert article['audio_manifest']['refined'][0]['kind'] == 'refined'

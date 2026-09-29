from __future__ import annotations

import json

import pytest

import config
from remote_pipeline_client import RemotePipelineClient, RemotePipelineUnavailable


class FakeResponse:
    def __init__(self, payload, status=200, *, content=b'', headers=None):
        self.payload = payload
        self.status_code = status
        self.content = content
        self.headers = headers or {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise __import__('requests').HTTPError(f'HTTP {self.status_code}')


class FakeSession:
    def __init__(self, *, capabilities=None, jobs=None):
        self.capability_response = capabilities
        self.jobs = list(jobs or [])
        self.submissions = []

    def get(self, url, **kwargs):
        if url.endswith('/v1/capabilities'):
            if isinstance(self.capability_response, Exception):
                raise self.capability_response
            return self.capability_response
        return FakeResponse(self.jobs.pop(0))

    def post(self, url, **kwargs):
        self.submissions.append(kwargs['json'])
        return FakeResponse({'job_id': 'job-1'}, 202)


def test_remote_route_requires_deep_capabilities(monkeypatch):
    session = FakeSession(capabilities=FakeResponse({
        'ok': True,
        'capabilities': {'crawl4ai': True, 'javascript': True},
    }))
    client = RemotePipelineClient(base_url='http://10.88.0.1:11236', token='x' * 32, session=session)
    assert client.choose_route('auto').channel == 'remote_crawl4ai'


def test_auto_falls_back_but_required_does_not(monkeypatch):
    import requests
    session = FakeSession(capabilities=requests.ConnectionError('down'))
    client = RemotePipelineClient(base_url='http://10.88.0.1:11236', token='x' * 32, session=session)
    assert client.choose_route('auto').channel == 'local_static'
    assert client.choose_route('required').channel == 'remote_required_unavailable'


def test_job_contract_and_wait(monkeypatch):
    monkeypatch.setattr(config, 'REMOTE_PIPELINE_POLL_SECONDS', 0.01)
    session = FakeSession(
        capabilities=FakeResponse({'ok': True, 'capabilities': {'crawl4ai': True, 'javascript': True}}),
        jobs=[{'status': 'running'}, {'status': 'completed', 'result': {'success': True, 'job_id': 'job-1', 'articles': []}}],
    )
    client = RemotePipelineClient(base_url='http://10.88.0.1:11236', token='x' * 32, session=session, sleep=lambda _: None)
    result = client.run(url='https://example.com/news', mode='list', keywords=['AI'], limit=5)
    assert result['job_id'] == 'job-1'
    assert session.submissions[0]['mode'] == 'list'
    assert session.submissions[0]['idempotency_key']
    assert 'zyte' not in json.dumps(session.submissions[0]).lower()


def test_short_token_is_not_configured():
    client = RemotePipelineClient(base_url='http://10.88.0.1:11236', token='short')
    with pytest.raises(RemotePipelineUnavailable):
        client.capabilities()


def test_idempotency_distinguishes_enrichment_and_tts():
    plain = RemotePipelineClient._idempotency_key(
        'https://example.com', 'article', ['AI'], enrich=False, tts=False,
    )
    enriched = RemotePipelineClient._idempotency_key(
        'https://example.com', 'article', ['AI'], enrich=True, tts=True,
    )
    assert plain != enriched


def test_idempotency_distinguishes_voice():
    female = RemotePipelineClient._idempotency_key(
        'https://example.com', 'article', ['AI'], voice='default',
    )
    male = RemotePipelineClient._idempotency_key(
        'https://example.com', 'article', ['AI'], voice='mandarin_male',
    )
    assert female != male


def test_submit_sends_selected_voice(monkeypatch):
    monkeypatch.setattr(config, 'REMOTE_PIPELINE_TTS_VOICE', 'default')
    session = FakeSession(
        capabilities=FakeResponse({'ok': True, 'capabilities': {'crawl4ai': True, 'javascript': True}}),
    )
    client = RemotePipelineClient(base_url='http://10.88.0.1:11236', token='x' * 32, session=session)
    client.submit(url='https://example.com', mode='article', keywords=['AI'], limit=5, voice='mandarin_male')
    assert session.submissions[0]['voice'] == 'mandarin_male'


def test_resolve_remote_tts_voice_is_layered():
    assert config.resolve_remote_tts_voice('zh', 'mandarin', 'female') == 'default'
    assert config.resolve_remote_tts_voice('zh', 'mandarin', 'male') == 'mandarin_male'
    assert config.resolve_remote_tts_voice('zh', 'cantonese', 'female') == 'cantonese_female'
    assert config.resolve_remote_tts_voice('zh', 'cantonese', 'male') == 'cantonese_male'
    assert config.resolve_remote_tts_voice('en', '', 'female') == 'english_female'
    assert config.resolve_remote_tts_voice('en', '', 'male') == 'english_male'


def test_artifact_reference_rejects_path_traversal():
    client = RemotePipelineClient(base_url='http://10.88.0.1:11236', token='x' * 32)
    with pytest.raises(Exception, match='invalid remote artifact'):
        client.fetch_artifact('job-1', '../secret')

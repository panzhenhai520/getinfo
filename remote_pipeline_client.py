"""Authenticated client for the Ubuntu CollectInfo pipeline."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

import config


class RemotePipelineError(RuntimeError):
    pass


class RemotePipelineUnavailable(RemotePipelineError):
    pass


@dataclass(frozen=True)
class PipelineRoute:
    channel: str
    reason: str
    capabilities: dict[str, Any] | None = None


class RemotePipelineClient:
    def __init__(self, *, base_url: str | None = None, token: str | None = None, session=None, monotonic=time.monotonic, sleep=time.sleep):
        self.base_url = str(base_url if base_url is not None else config.REMOTE_PIPELINE_URL).rstrip('/')
        self.token = str(token if token is not None else config.REMOTE_PIPELINE_TOKEN)
        self.session = session or requests.Session()
        self.monotonic = monotonic
        self.sleep = sleep
        self._lock = threading.Lock()
        self._circuit_until = 0.0
        self._last_capabilities: dict[str, Any] | None = None

    @property
    def configured(self) -> bool:
        return bool(self.base_url and len(self.token) >= 32)

    def _headers(self) -> dict[str, str]:
        return {'Authorization': f'Bearer {self.token}', 'Content-Type': 'application/json'}

    def _open_circuit(self) -> None:
        with self._lock:
            self._circuit_until = self.monotonic() + config.REMOTE_PIPELINE_CIRCUIT_SECONDS

    def capabilities(self, *, force: bool = False) -> dict[str, Any]:
        if not self.configured:
            raise RemotePipelineUnavailable('remote pipeline is not configured')
        with self._lock:
            if not force and self.monotonic() < self._circuit_until:
                raise RemotePipelineUnavailable('remote pipeline circuit is open')
        try:
            response = self.session.get(
                f'{self.base_url}/v1/capabilities',
                headers=self._headers(),
                timeout=config.REMOTE_PIPELINE_HEALTH_TIMEOUT_SECONDS,
            )
            if response.status_code == 401:
                raise RemotePipelineUnavailable('remote pipeline authentication failed')
            response.raise_for_status()
            payload = response.json()
            required = payload.get('capabilities') or {}
            if not payload.get('ok') or not required.get('crawl4ai') or not required.get('javascript'):
                raise RemotePipelineUnavailable('remote pipeline is only partially healthy')
            with self._lock:
                self._last_capabilities = payload
                self._circuit_until = 0.0
            return payload
        except (requests.RequestException, ValueError, RemotePipelineUnavailable) as exc:
            self._open_circuit()
            if isinstance(exc, RemotePipelineUnavailable):
                raise
            raise RemotePipelineUnavailable('remote pipeline health check failed') from exc

    def choose_route(self, mode: str | None = None) -> PipelineRoute:
        selected = str(mode or config.PIPELINE_MODE).casefold()
        if selected == 'local':
            return PipelineRoute('local_static', 'pipeline_mode_local')
        try:
            capabilities = self.capabilities()
            return PipelineRoute('remote_crawl4ai', 'remote_deep_health_ok', capabilities)
        except RemotePipelineUnavailable as exc:
            if selected == 'required':
                return PipelineRoute('remote_required_unavailable', str(exc))
            return PipelineRoute('local_static', str(exc))

    @staticmethod
    def _idempotency_key(
        url: str,
        mode: str,
        keywords: list[str],
        task_id: str = '',
        *,
        enrich: bool = True,
        tts: bool = True,
        voice: str = '',
        industry_pack_id: str = '',
    ) -> str:
        value = json.dumps(
            {
                'url': url,
                'mode': mode,
                'keywords': sorted(keywords),
                'task_id': task_id,
                'enrich': bool(enrich),
                'tts': bool(tts),
                'voice': str(voice or ''),
                'industry_pack_id': str(industry_pack_id or ''),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(value.encode('utf-8')).hexdigest()

    def for_pack(self, pack_id: str):
        """返回指向该行业包『运行配置』所配 VPN pipeline 的客户端；未配置回退本客户端。"""
        try:
            from pack_tenant import pack_runtime
            rt = pack_runtime(pack_id)
            url = str(rt.get('vpn_pipeline_url') or '').strip().rstrip('/')
            tok = str(rt.get('vpn_pipeline_token') or '').strip()
            if not url:
                # 未配 URL 时：若配了 server_ip/port 则按 ip:port 合成
                ip = str(rt.get('server_ip') or '').strip()
                if ip:
                    url = 'http://' + ip + (':' + str(rt.get('server_port') or 11236) if rt.get('server_port') else '')
            if url:
                return RemotePipelineClient(base_url=url, token=tok or self.token, session=self.session)
        except Exception:
            pass
        return self

    def submit(self, *, url: str, mode: str, keywords: list[str], limit: int, task_id: str = '', enrich: bool | None = None, tts: bool | None = None, voice: str | None = None, industry_pack_id: str = '', industry_pack_name: str = '') -> str:
        selected_voice = str(config.REMOTE_PIPELINE_TTS_VOICE if voice is None else voice or '')
        # TTS 总闸：SYSTEM_TTS_ENABLED=False 时客户端一律不请求语音合成
        _tts_effective = bool(config.REMOTE_PIPELINE_TTS if tts is None else bool(tts)) and bool(
            getattr(config, 'SYSTEM_TTS_ENABLED', False)
        )
        payload = {
            'url': url,
            'mode': 'list' if mode == 'list' else 'article',
            'keywords': keywords,
            'limit': max(1, int(limit)),
            'task_id': task_id,
            'enrich': config.REMOTE_PIPELINE_ENRICH if enrich is None else bool(enrich),
            'tts': _tts_effective,
            'voice': selected_voice,
            'industry_pack_id': str(industry_pack_id or ''),
            'industry_pack_name': str(industry_pack_name or ''),
        }
        payload['idempotency_key'] = self._idempotency_key(
            url,
            payload['mode'],
            keywords,
            task_id,
            enrich=payload['enrich'],
            tts=payload['tts'],
            voice=selected_voice,
            industry_pack_id=str(industry_pack_id or ''),
        )
        try:
            response = self.session.post(
                f'{self.base_url}/v1/pipeline/jobs', headers=self._headers(), json=payload,
                timeout=max(5, config.REMOTE_PIPELINE_HEALTH_TIMEOUT_SECONDS * 3),
            )
            response.raise_for_status()
            job_id = str(response.json().get('job_id') or '')
            if not job_id:
                raise ValueError('missing job id')
            return job_id
        except (requests.RequestException, ValueError) as exc:
            self._open_circuit()
            raise RemotePipelineUnavailable('remote pipeline job submission failed') from exc

    def wait(self, job_id: str) -> dict[str, Any]:
        deadline = self.monotonic() + config.REMOTE_PIPELINE_JOB_TIMEOUT_SECONDS
        transient_errors = 0
        while self.monotonic() < deadline:
            try:
                response = self.session.get(
                    f'{self.base_url}/v1/pipeline/jobs/{job_id}', headers=self._headers(),
                    timeout=max(5, config.REMOTE_PIPELINE_HEALTH_TIMEOUT_SECONDS * 3),
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                transient_errors += 1
                if transient_errors >= 3:
                    raise RemotePipelineUnavailable('remote pipeline status request failed') from exc
                self.sleep(config.REMOTE_PIPELINE_POLL_SECONDS)
                continue
            transient_errors = 0
            status = payload.get('status')
            if status in {'completed', 'partial_success'}:
                result = payload.get('result') or {}
                if not result.get('success'):
                    raise RemotePipelineError('remote pipeline returned an invalid success result')
                return result
            if status == 'failed':
                raise RemotePipelineError(str(payload.get('error') or 'remote pipeline job failed'))
            self.sleep(config.REMOTE_PIPELINE_POLL_SECONDS)
        raise RemotePipelineError('remote pipeline job timed out')

    def run(self, **kwargs) -> dict[str, Any]:
        return self.wait(self.submit(**kwargs))

    def enrich(self, *, url: str, title: str, content: str,
               enrich: bool | None = None, tts: bool | None = None,
               voice: str | None = None, publish_date: str = '',
               task_id: str = '', timeout: int | None = None,
               industry_pack_id: str = '', industry_pack_name: str = '',
               industry_topics: list | None = None) -> dict[str, Any]:
        """纯加工：把本机已抓好的正文送 VPN 只做 提炼/翻译/语音，VPN 不抓取。

        对应 VPN 端 `POST /v1/pipeline/enrich`。返回
        {url,title,refined_content,translated_content,audio_manifest,source_language,
         content_type,relevance,core_facts,...}。
        industry_pack_* 用于 VPN 端分层提炼（相关性锚定 + A/B/C 分层输出）。
        """
        selected_voice = str(config.REMOTE_PIPELINE_TTS_VOICE if voice is None else voice or '')
        # TTS 总闸：SYSTEM_TTS_ENABLED=False 时客户端一律不请求语音合成
        _tts_effective = bool(config.REMOTE_PIPELINE_TTS if tts is None else bool(tts)) and bool(
            getattr(config, 'SYSTEM_TTS_ENABLED', False)
        )
        payload = {
            'url': url,
            'title': title,
            'content': content,
            'publish_date': str(publish_date or ''),
            'enrich': config.REMOTE_PIPELINE_ENRICH if enrich is None else bool(enrich),
            'tts': _tts_effective,
            'voice': selected_voice,
            'industry_pack_id': str(industry_pack_id or ''),
            'industry_pack_name': str(industry_pack_name or ''),
            'industry_topics': [
                {'key': str(t.get('key') or ''), 'name': str(t.get('name') or '')}
                for t in (industry_topics or [])
                if isinstance(t, dict)
            ][:12],
        }
        if task_id:
            payload['task_id'] = task_id
        try:
            response = self.session.post(
                f'{self.base_url}/v1/pipeline/enrich', headers=self._headers(), json=payload,
                timeout=timeout or max(30, config.REMOTE_PIPELINE_JOB_TIMEOUT_SECONDS),
            )
            response.raise_for_status()
            result = response.json()
        except requests.RequestException as exc:
            self._open_circuit()
            raise RemotePipelineUnavailable('remote pipeline enrich request failed') from exc
        if not result.get('success'):
            raise RemotePipelineError(str(result.get('error') or 'remote pipeline enrich failed'))
        return result

    def ocr(self, *, url: str, timeout: int | None = None) -> dict[str, Any]:
        """VPN 端独立 OCR 阅读：对 URL 截图 → OCR → LLM 总结。

        对应 VPN 端 `POST /v1/pipeline/ocr`。该端点**不检查 robots.txt**（独立的人为阅读
        动作），可用于 robots 拒爬/反爬但页面可见的场景。返回
        {success,url,title,ocr_text,ocr_length,summary,source_language,...}。
        """
        payload = {"url": url}
        try:
            response = self.session.post(
                f'{self.base_url}/v1/pipeline/ocr', headers=self._headers(), json=payload,
                timeout=timeout or max(30, config.REMOTE_PIPELINE_JOB_TIMEOUT_SECONDS),
            )
            response.raise_for_status()
            result = response.json()
        except requests.RequestException as exc:
            self._open_circuit()
            raise RemotePipelineUnavailable('remote pipeline ocr request failed') from exc
        if not result.get('success'):
            raise RemotePipelineError(str(result.get('error') or 'remote pipeline ocr failed'))
        return result

    def links(self, *, url: str, timeout: int | None = None) -> dict[str, Any]:
        """VPN 端真实浏览器渲染页面并返回链接（内部链接 + .pdf 链接），不检查 robots。

        对应 VPN 端 `POST /v1/pipeline/links`。用于报告 URL 探测：对反爬/412/403 等
        抓不到的信源，用真浏览器渲染后从**真实** DOM 里提取 .pdf / 报告页链接。
        """
        payload = {"url": url}
        try:
            response = self.session.post(
                f'{self.base_url}/v1/pipeline/links', headers=self._headers(), json=payload,
                timeout=timeout or max(30, config.REMOTE_PIPELINE_JOB_TIMEOUT_SECONDS),
            )
            response.raise_for_status()
            result = response.json()
        except requests.RequestException as exc:
            self._open_circuit()
            raise RemotePipelineUnavailable('remote pipeline links request failed') from exc
        if not result.get('success'):
            raise RemotePipelineError(str(result.get('error') or 'remote pipeline links failed'))
        return result

    def list_voices(self) -> list[dict[str, Any]]:
        """Return the selectable TTS voices advertised by the VPN gateway."""
        payload = self.capabilities()
        voices = payload.get('tts_voices') or []
        return [item for item in voices if isinstance(item, dict) and item.get('id')]

    def fetch_artifact(self, job_id: str, artifact: str, timeout: float | None = None) -> tuple[bytes, str]:
        """Fetch one server-produced artifact without exposing the bearer token. timeout 可覆盖（朗读用短超时快速回落）。"""
        safe_job_id = str(job_id or '').strip()
        safe_artifact = str(artifact or '').strip()
        if not safe_job_id or not safe_artifact or '/' in safe_artifact or '\\' in safe_artifact:
            raise RemotePipelineError('invalid remote artifact reference')
        try:
            response = self.session.get(
                f'{self.base_url}/v1/pipeline/jobs/{safe_job_id}/artifacts/{safe_artifact}',
                headers=self._headers(),
                timeout=timeout if timeout is not None else config.REMOTE_PIPELINE_JOB_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.content, str(response.headers.get('Content-Type') or 'application/octet-stream')
        except requests.RequestException as exc:
            raise RemotePipelineUnavailable('remote pipeline artifact request failed') from exc


remote_pipeline_client = RemotePipelineClient()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RAGFlow research client, separate from ingestion and classification clients."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from typing import Mapping

import requests


class QaRagflowError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0, retryable: bool = True, request_id: str = ""):
        super().__init__(message)
        self.status_code = int(status_code or 0)
        self.retryable = bool(retryable)
        self.request_id = str(request_id or "")


class QaRagflowProtocolError(QaRagflowError):
    pass


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _records(payload, *keys) -> list:
    value = payload
    if isinstance(payload, Mapping) and "data" in payload:
        value = payload.get("data")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        for key in keys:
            items = value.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, Mapping)]
        if value.get("id"):
            return [dict(value)]
    return []


class QaRagflowResearchClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        app_id: str,
        kb_id: str,
        timeout_seconds: int = 90,
        retries: int = 1,
        session=None,
        proxies=None,
        sleep=time.sleep,
    ):
        self.base_url = str(base_url or "").rstrip("/")
        self.api_key = str(api_key or "")
        self.app_id = str(app_id or "")
        self.kb_id = str(kb_id or "")
        cap = _int_env("QA_RAG_ENHANCEMENT_REQUEST_TIMEOUT_SECONDS", 25, 5, 90)
        self.timeout_seconds = max(5, min(cap, int(timeout_seconds or 90)))
        # 研究调用是"思考型"长生成，不能复用通用上限：实测生产量级的证据提示（3138 字符）
        # 单次调用就要 12.8 秒，而研究调用原先默认 min(timeout_seconds, 15)=15 秒，
        # 证据再多一点就超时 → level2_research 报 INTERNAL_ERROR 并整条答案降级。
        # 这里单独取策略里的 research_timeout_seconds（默认 90 秒）。
        self.research_timeout_seconds = _int_env(
            "QA_RAG_ENHANCEMENT_RESEARCH_TIMEOUT_SECONDS",
            int(timeout_seconds or 90),
            6,
            300,
        )
        self.retries = max(0, min(3, int(retries or 0)))
        self.session = session or requests.Session()
        self.proxies = proxies
        self.sleep = sleep

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.app_id and self.kb_id)

    def _headers(self, request_id: str = "") -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-Request-ID": request_id or uuid.uuid4().hex,
        }

    def _request(self, method: str, path: str, **kwargs):
        if not self.configured:
            raise QaRagflowError("RAG增强检索未完整配置", retryable=False)
        request_id = str(kwargs.pop("request_id", "") or uuid.uuid4().hex)
        max_retries = max(0, min(3, int(kwargs.pop("max_retries", self.retries))))
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                response = self.session.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=self._headers(request_id),
                    timeout=kwargs.pop("timeout", self.timeout_seconds),
                    proxies=self.proxies,
                    **kwargs,
                )
                status = int(response.status_code)
                if status in {408, 429} or status >= 500:
                    if attempt < max_retries:
                        self.sleep(min(2.0, 0.25 * (2**attempt)))
                        continue
                    raise QaRagflowError(
                        "RAG增强检索服务繁忙或暂不可用",
                        status_code=status,
                        retryable=True,
                        request_id=request_id,
                    )
                if status in {401, 403}:
                    raise QaRagflowError(
                        "RAG增强检索服务凭据无效或无权访问研究助手",
                        status_code=status,
                        retryable=False,
                        request_id=request_id,
                    )
                response.raise_for_status()
                return response, request_id
            except QaRagflowError:
                raise
            except (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
            ) as exc:
                last_error = exc
                if attempt < max_retries:
                    self.sleep(min(2.0, 0.25 * (2**attempt)))
                    continue
            except requests.HTTPError as exc:
                status = int(getattr(exc.response, "status_code", 0) or 0)
                raise QaRagflowError(
                    "RAG增强检索请求失败", status_code=status, retryable=status >= 500,
                    request_id=request_id,
                ) from exc
        raise QaRagflowError(
            "无法连接 RAG增强检索服务", retryable=True, request_id=request_id,
        ) from last_error

    @staticmethod
    def _json(response, request_id: str) -> dict:
        try:
            payload = response.json()
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise QaRagflowProtocolError("RAG增强检索返回了无法解析的数据", request_id=request_id) from exc
        if not isinstance(payload, dict):
            raise QaRagflowProtocolError("RAG增强检索返回格式无效", request_id=request_id)
        code = payload.get("code")
        if code not in (None, 0, "0"):
            raise QaRagflowError("RAG增强检索请求未成功", retryable=True, request_id=request_id)
        return payload

    def health_check(self) -> dict:
        if not self.configured:
            return {"ready": False, "configured": False, "app_found": False, "kb_found": False}
        try:
            health_timeout = _int_env("QA_RAG_ENHANCEMENT_HEALTH_TIMEOUT_SECONDS", 6, 3, 20)
            app_response, app_request = self._request(
                "GET", "/api/v1/chats",
                params={"id": self.app_id, "page": 1, "page_size": 100},
                timeout=health_timeout,
            )
            apps = _records(self._json(app_response, app_request), "chats", "items", "records")
            app = next((dict(item) for item in apps if str(item.get("id")) == self.app_id), None)
            kb_response, kb_request = self._request(
                "GET", "/api/v1/datasets",
                params={"id": self.kb_id, "page": 1, "page_size": 100},
                timeout=health_timeout,
            )
            datasets = _records(self._json(kb_response, kb_request), "datasets", "items", "records")
            dataset = next((dict(item) for item in datasets if str(item.get("id")) == self.kb_id), None)
            bound_ids = []
            if app:
                for key in ("dataset_ids", "kb_ids", "knowledge_base_ids"):
                    if isinstance(app.get(key), list):
                        bound_ids.extend(str(item) for item in app[key])
            binding_known = bool(bound_ids)
            binding_ok = self.kb_id in bound_ids if binding_known else None
            return {
                "ready": bool(app and dataset and binding_ok is not False),
                "configured": True,
                "app_found": bool(app),
                "kb_found": bool(dataset),
                "binding_verified": binding_ok,
                "request_ids": [app_request, kb_request],
                "kb_version": hashlib.sha256(json.dumps({
                    "id": (dataset or {}).get("id"),
                    "update_time": (dataset or {}).get("update_time") or (dataset or {}).get("update_date"),
                    "document_count": (dataset or {}).get("document_count") or (dataset or {}).get("doc_num"),
                    "chunk_count": (dataset or {}).get("chunk_count") or (dataset or {}).get("chunk_num"),
                }, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24],
            }
        except QaRagflowError as exc:
            return {
                "ready": False, "configured": True, "app_found": False, "kb_found": False,
                "error_code": "RAGFLOW_UNAVAILABLE", "request_id": exc.request_id,
            }

    def create_session(self, *, name: str = "Unified QA research") -> dict:
        response, request_id = self._request(
            "POST", f"/api/v1/chats/{self.app_id}/sessions", json={"name": str(name)[:200]},
        )
        payload = self._json(response, request_id)
        data = payload.get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        session_id = str((data or {}).get("id") or (data or {}).get("session_id") or "")
        if not session_id:
            raise QaRagflowProtocolError("RAG增强检索未返回研究会话 ID", request_id=request_id)
        return {"session_id": session_id, "request_id": request_id}

    def search_dataset(self, query: str, *, top_n: int = 8, threshold: float = 0.2) -> dict:
        body = {
            "question": str(query)[:1000], "doc_ids": [], "page": 1,
            "size": max(1, min(20, int(top_n))), "top_k": 1024,
            "similarity_threshold": max(0.0, min(1.0, float(threshold))),
            "vector_similarity_weight": 0.7, "keyword": False, "cross_languages": [],
            # This client already performs bounded, traceable multi-query
            # expansion.  Disable RAGFlow's LLM translation expansion so a
            # runaway translation cannot exceed the search backend's clause
            # limit or replace the original legal-policy query.
            "cross_language_expansion": False,
        }
        response, request_id = self._request(
            "POST", f"/api/v1/datasets/{self.kb_id}/search", json=body,
            timeout=_int_env("QA_RAG_ENHANCEMENT_SEARCH_TIMEOUT_SECONDS", 10, 4, 30),
        )
        payload = self._json(response, request_id)
        data = payload.get("data") or {}
        chunks = data if isinstance(data, list) else (
            data.get("chunks") or data.get("items") or data.get("records") or []
            if isinstance(data, Mapping) else []
        )
        doc_aggs = data.get("doc_aggs") or data.get("documents") or [] if isinstance(data, Mapping) else []
        return {
            "chunks": [dict(item) for item in chunks if isinstance(item, Mapping)],
            "doc_aggs": [dict(item) for item in doc_aggs if isinstance(item, Mapping)],
            "total": int((data.get("total") if isinstance(data, Mapping) else len(chunks)) or 0),
            "request_id": request_id,
        }

    def dataset_status(self) -> dict:
        response, request_id = self._request(
            "GET", f"/api/v1/datasets/{self.kb_id}/documents",
            params={"page": 1, "page_size": 50},
            timeout=_int_env("QA_RAG_ENHANCEMENT_HEALTH_TIMEOUT_SECONDS", 6, 3, 20),
        )
        payload = self._json(response, request_id)
        data = payload.get("data") or {}
        documents = _records(data, "docs", "documents", "items", "records")
        total = int((data.get("total") if isinstance(data, Mapping) else len(documents)) or 0)
        parsing = sum(
            1 for item in documents
            if str(item.get("run") or item.get("status") or "").casefold()
            in {"running", "parsing", "processing", "0", "1"}
        )
        return {"total": total, "parsing": parsing, "ready": total > parsing, "request_id": request_id}

    def complete(self, prompt: str, *, session_id: str = "", stream: bool = False) -> dict:
        if stream:
            body = {
                "question": str(prompt), "stream": True,
                "reasoning": False, "max_tokens": 1800,
            }
            if session_id:
                body["session_id"] = str(session_id)
            path = f"/api/v1/chats/{self.app_id}/completions"
        else:
            # Retrieval has already happened through the scoped dataset API.
            # The protected endpoint invokes the same Assistant model directly
            # so the Jinja evidence context is not translated and retrieved a
            # second time by the generic chat path.
            body = {"assistant_id": self.app_id, "prompt": str(prompt), "max_tokens": 1800}
            path = "/v1/unified_qa/research"
        response, request_id = self._request(
            "POST", path, json=body,
            timeout=self.research_timeout_seconds,
            stream=bool(stream), max_retries=0,
        )
        if stream:
            return self._parse_stream(response, request_id)
        payload = self._json(response, request_id)
        data = payload.get("data")
        if isinstance(data, str):
            return {"answer": data, "reference": {}, "request_id": request_id, "session_id": session_id}
        data = data if isinstance(data, Mapping) else payload
        answer = str(data.get("answer") or data.get("content") or "")
        if not answer:
            raise QaRagflowProtocolError("RAG增强检索未返回研究报告", request_id=request_id)
        return {
            "answer": answer,
            "reference": data.get("reference") or payload.get("reference") or {},
            "request_id": request_id,
            "session_id": str(data.get("session_id") or session_id or ""),
        }

    def _parse_stream(self, response, request_id: str) -> dict:
        parts, reference, session_id = [], {}, ""
        for raw in response.iter_lines(decode_unicode=True):
            line = str(raw or "").strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                break
            try:
                event = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            data = event.get("data") if isinstance(event, Mapping) else None
            data = data if isinstance(data, Mapping) else event
            if not isinstance(data, Mapping):
                continue
            text = data.get("answer") or data.get("content") or data.get("delta")
            if text:
                parts.append(str(text))
            if isinstance(data.get("reference"), Mapping):
                reference = dict(data["reference"])
            session_id = str(data.get("session_id") or session_id)
        answer = "".join(parts)
        if not answer:
            raise QaRagflowProtocolError("RAG增强检索流式响应未包含答案", request_id=request_id)
        return {"answer": answer, "reference": reference, "request_id": request_id, "session_id": session_id}


__all__ = ["QaRagflowError", "QaRagflowProtocolError", "QaRagflowResearchClient"]

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Xinference bge-m3 向量服务封装。

为"行业信息演化发现系统"提供文章向量能力：第一阶段产出向量并落库，
第三阶段（语义漂移 / 聚类）消费。纯 requests 实现，独立于
intel_llm_client 的 LLM 信号量，避免与翻译 / 助手抢并发槽。

服务地址（已实测可用）：http://192.168.0.64:9997/v1/embeddings
模型 bge-m3，dim=1024，OpenAI 兼容，无需认证。
"""

from __future__ import annotations

import logging
import time
from typing import List

import numpy as np
import requests

logger = logging.getLogger(__name__)

# 默认指向内网 Xinference（dim=1024）
DEFAULT_BASE_URL = "http://192.168.0.64:9997"
DEFAULT_MODEL = "bge-m3"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_RETRIES = 2
DEFAULT_BATCH_SIZE = 16


class EmbeddingError(Exception):
    """向量服务调用异常。"""


class EmbeddingClient:
    """Xinference bge-m3 封装（OpenAI 兼容 /v1/embeddings）。

    单条 embed() 返回 np.ndarray(dim,)；批量 embed_batch() 内部按
    batch_size 分片请求，返回 list[np.ndarray]。失败按指数退避重试，
    最终抛 EmbeddingError。
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        *,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        batch_size: int = DEFAULT_BATCH_SIZE,
        keep_alive: str = "",
    ):
        self.base_url = str(base_url).rstrip("/")
        self.model = str(model) or DEFAULT_MODEL
        self.timeout = int(timeout)
        self.max_retries = max(0, int(max_retries))
        self.batch_size = max(1, int(batch_size))
        self.keep_alive = str(keep_alive or "")

    # ---------- 对外接口 ----------
    def embed(self, text: str) -> np.ndarray:
        """单条文本 → np.ndarray(dim,)。空文本抛 EmbeddingError。"""
        if not text or not str(text).strip():
            raise EmbeddingError("empty text")
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: List[str]) -> List[np.ndarray]:
        """批量文本 → list[np.ndarray(dim,)]，内部按 batch_size 分片。"""
        cleaned = [str(t) for t in texts]
        results: List[np.ndarray] = []
        for start in range(0, len(cleaned), self.batch_size):
            chunk = cleaned[start : start + self.batch_size]
            results.extend(self._request(chunk))
        return results

    def health_check(self) -> bool:
        """探测服务是否可用（GET /v1/models）。"""
        try:
            resp = requests.get(
                f"{self.base_url}/v1/models",
                timeout=min(self.timeout, 8),
            )
            return resp.status_code == 200
        except Exception:
            return False

    # ---------- 内部 ----------
    def _request(self, texts: List[str]) -> List[np.ndarray]:
        """一次请求一组文本，带指数退避重试。"""
        payload = {"model": self.model, "input": texts}
        if self.keep_alive:
            payload["keep_alive"] = self.keep_alive  # Ollama 专用：模型常驻显存
        last_error: Exception = None  # type: ignore[assignment]
        for attempt in range(self.max_retries + 1):
            try:
                resp = requests.post(
                    f"{self.base_url}/v1/embeddings",
                    json=payload,
                    timeout=self.timeout,
                )
                if resp.status_code != 200:
                    raise EmbeddingError(
                        f"embedding HTTP {resp.status_code}: {resp.text[:200]}"
                    )
                data = resp.json().get("data") or []
                if len(data) != len(texts):
                    raise EmbeddingError(
                        f"embedding count mismatch: want {len(texts)}, got {len(data)}"
                    )
                # 按 index 还原请求顺序
                data.sort(key=lambda d: d.get("index", 0))
                return [
                    np.asarray(item["embedding"], dtype=np.float32) for item in data
                ]
            except Exception as exc:  # 网络错误 + 上面的 EmbeddingError
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(0.5 * (2 ** attempt))  # 0.5s, 1s, 2s ...
                    continue
        raise EmbeddingError(
            f"embedding failed after {self.max_retries + 1} attempts: {last_error}"
        )


def get_embedding_client(pack_id: str = '') -> EmbeddingClient:
    """工厂：优先读行业包『运行配置』的 embedding_base_url/模型，未配置回退全局 .env / 默认。"""
    base_url = DEFAULT_BASE_URL
    model = DEFAULT_MODEL
    timeout = DEFAULT_TIMEOUT_SECONDS
    batch_size = DEFAULT_BATCH_SIZE
    keep_alive = ""
    pack_override = {}
    if pack_id:
        try:
            from pack_tenant import pack_runtime
            rt = pack_runtime(pack_id)
            pack_override['base'] = str(rt.get('embedding_base_url') or '')
            pack_override['model'] = str(rt.get('embedding_model') or '')
        except Exception:
            pass
    try:
        import config  # 延迟导入，避免循环依赖

        base_url = pack_override.get('base') or getattr(config, "INTEL_EMBEDDING_BASE_URL", base_url) or base_url
        model = pack_override.get('model') or getattr(config, "INTEL_EMBEDDING_MODEL", model) or model
        timeout = getattr(config, "INTEL_EMBEDDING_TIMEOUT_SECONDS", timeout)
        batch_size = getattr(config, "INTEL_EMBEDDING_BATCH_SIZE", batch_size)
        keep_alive = getattr(config, "INTEL_EMBEDDING_KEEP_ALIVE", keep_alive) or keep_alive
    except Exception:
        pass
    return EmbeddingClient(
        base_url,
        model,
        timeout=timeout,
        batch_size=batch_size,
        keep_alive=keep_alive,
    )

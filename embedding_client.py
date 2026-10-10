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
import os
import re
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
# 单条输入的最大 token 数（估算口径见 _estimate_tokens）。
# 为什么需要：生产 A 机的 bge-m3 是 llama.cpp 起的（10.88.0.1:8082），原先物理批大小 512，
# 超过就返回 500「input (859 tokens) is too large to process. increase the physical batch size」，
# 而 embed_articles 送的是整篇正文（800 字符≈460 token 还行，1500 字符≈859 token 必失败），
# 于是整批 100% 失败、拆单重试后仍然全失败 —— 纯烧 lane 容量且向量覆盖率永远涨不上去。
# 2026-10-07：服务端 llama-server 已重启为 --batch-size 2048 --ubatch-size 2048，
# 实测 600/1200/1800/3000 字符（约 340/677/1014/1690 token）全部返回 200，
# 因此默认上限从 480 提到 1500（bge-m3 原生支持 8192 token，留出余量又不越过 2048 的物理批）。
# 2026-10-10 标定复核（真实中文样本，实测 token/字 ≈ 0.72 —— 比早先按 0.62 的估算偏高）：
#   2000 字 → 1443 token ✅；2400 字 → 1731 ✅；2600 字 → 1875 ✅；3000 字 → 2163 ❌ HTTP 500。
# 于是把默认上限提到 1800：配合 2400 字的字符上限（≈1731 token）可多嵌入 20% 正文，
# 又对服务端 2048 留出 ~15% 余量（中文密度因文本而异，余量不能吃光）。
DEFAULT_MAX_INPUT_TOKENS = 1800

_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def _estimate_tokens(text: str) -> int:
    """粗估 token 数（偏保守，宁可多算）。

    bge-m3 用 XLM-R sentencepiece：CJK 基本 1 字 ≈ 1 token，拉丁词约 3~4 字符 ≈ 1 token，
    标点/符号各约 1 token。
    """
    value = str(text or "")
    if not value:
        return 0
    cjk = len(_CJK_RE.findall(value))
    latin_words = len(_LATIN_WORD_RE.findall(value))
    others = max(0, len(value) - cjk - sum(len(item) for item in _LATIN_WORD_RE.findall(value)))
    return cjk + int(latin_words * 1.4) + others


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """按 token 估算截断（二分找安全长度，避免逐字循环）。"""
    value = str(text or "")
    if max_tokens <= 0 or _estimate_tokens(value) <= max_tokens:
        return value
    low, high = 0, len(value)
    while low < high:
        mid = (low + high + 1) // 2
        if _estimate_tokens(value[:mid]) <= max_tokens:
            low = mid
        else:
            high = mid - 1
    return value[:low]


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
        max_input_tokens: int = 0,
    ):
        self.base_url = str(base_url).rstrip("/")
        self.model = str(model) or DEFAULT_MODEL
        self.timeout = int(timeout)
        self.max_retries = max(0, int(max_retries))
        self.batch_size = max(1, int(batch_size))
        self.keep_alive = str(keep_alive or "")
        try:
            configured = int(os.environ.get("INTEL_EMBEDDING_MAX_INPUT_TOKENS",
                                            DEFAULT_MAX_INPUT_TOKENS))
        except (TypeError, ValueError):
            configured = DEFAULT_MAX_INPUT_TOKENS
        self.max_input_tokens = max(0, int(max_input_tokens or configured))

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
        """一次请求一组文本，带指数退避重试；超长输入按 token 估算截断。

        遇到"输入过长"这类服务端 500 时，**缩小输入再试**（而不是拿同一份超长文本重试），
        否则每次重试必然同样失败——生产上就是这样把 220 次尝试全烧光的。
        """
        prepared = [_truncate_to_tokens(text, self.max_input_tokens) for text in texts]
        if any(_estimate_tokens(a) > _estimate_tokens(b) for a, b in zip(texts, prepared)):
            logger.info("embedding 输入按 %d token 上限截断（原文最长 %d 估算 token）",
                        self.max_input_tokens,
                        max(_estimate_tokens(text) for text in texts))
        payload = {"model": self.model, "input": prepared}
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
                if len(data) != len(prepared):
                    raise EmbeddingError(
                        f"embedding count mismatch: want {len(prepared)}, got {len(data)}"
                    )
                # 按 index 还原请求顺序
                data.sort(key=lambda d: d.get("index", 0))
                return [
                    np.asarray(item["embedding"], dtype=np.float32) for item in data
                ]
            except Exception as exc:  # 网络错误 + 上面的 EmbeddingError
                last_error = exc
                if self._is_input_too_large(exc):
                    # 服务端说输入过长：把上限继续压小再试（保留至少 64 token）
                    shrunk = max(64, int(self.max_input_tokens * 0.6)) if self.max_input_tokens else 256
                    if shrunk < self.max_input_tokens or self.max_input_tokens == 0:
                        logger.warning("embedding 输入过长，上限 %s → %s 后重试",
                                       self.max_input_tokens or "(未设)", shrunk)
                        self.max_input_tokens = shrunk
                        payload["input"] = [_truncate_to_tokens(text, shrunk) for text in texts]
                if attempt < self.max_retries:
                    time.sleep(0.5 * (2 ** attempt))  # 0.5s, 1s, 2s ...
                    continue
        raise EmbeddingError(
            f"embedding failed after {self.max_retries + 1} attempts: {last_error}"
        )

    @staticmethod
    def _is_input_too_large(exc: Exception) -> bool:
        text = str(exc).casefold()
        return ("too large to process" in text
                or "physical batch size" in text
                or "input is too long" in text
                or "maximum context" in text)


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

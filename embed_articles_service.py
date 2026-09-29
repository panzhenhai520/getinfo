#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""文章向量化服务：扫描缺向量文章 → 调 bge-m3（Ollama/Xinference）→ 向量落库。

为第三阶段（语义漂移 / 聚类 / 聊天语义检索）铺路。第一阶段只产出并存储向量。
按批次调用 embedding 服务（embed_batch）：VPN 侧 bge-m3 单次请求有 ~12s 固定开销，
逐篇调用 2600 篇要 8 小时以上；批量 16 条/请求可压到 40 分钟量级。批次失败整批
记 error 状态，下轮自动重试。
"""

from __future__ import annotations

import logging
from typing import Dict

import numpy as np

import config
from embedding_client import EmbeddingClient, EmbeddingError, get_embedding_client
from intel_database import IntelRepository
from industry_pack_runtime import active_industry_composition_service

logger = logging.getLogger(__name__)


class EmbedArticlesService:
    def __init__(self, repository: IntelRepository = None, embed_client: EmbeddingClient = None):
        self.repository = repository or IntelRepository()
        # 注入的客户端（测试/指定实例）优先，不再被 pack 覆盖
        self._client_injected = embed_client is not None
        self.embed_client = embed_client or get_embedding_client()

    def run(
        self,
        *,
        limit: int = None,
        pack_id: str = "",
        max_chars: int = None,
    ) -> Dict:
        """对缺向量（或内容变化）的文章批量产出向量并落库。"""
        if pack_id and not self._client_injected:
            try:
                self.embed_client = get_embedding_client(pack_id)  # per-pack 向量服务
            except Exception:
                pass
        limit = int(limit or getattr(config, "INTEL_EMBEDDING_MAX_ARTICLES_PER_RUN", 200))
        max_chars = int(max_chars or getattr(config, "INTEL_EMBEDDING_MAX_INPUT_CHARS", 4000))
        model = self.embed_client.model

        pack = str(pack_id or "").strip()
        if not pack:
            try:
                pack = str(
                    active_industry_composition_service.snapshot().get(
                        "active_industry_pack_id", ""
                    )
                )
            except Exception:
                pack = ""

        articles = self.repository.list_articles_missing_embeddings(
            model_id=model, pack_id=pack, limit=limit, max_chars=max_chars
        )
        # 过滤空正文（截断后仍可能为空）
        pending = [a for a in articles if str(a.get("content") or "").strip()]
        if not pending:
            logger.info("embed_articles: pack=%s 无待向量文章", pack or "*")
            return {"model": model, "pack_id": pack, "processed": 0, "succeeded": 0, "failed": 0}

        succeeded = failed = 0
        dim = 0
        batch_size = max(1, int(getattr(self.embed_client, "batch_size", 16) or 16))

        def mark_error(a, exc):
            try:
                self.repository.upsert_article_embedding(
                    article_id=a["article_id"],
                    model_id=model,
                    dim=0,
                    vector_blob=b"",
                    content_hash=str(a.get("content_hash") or ""),
                    status="error",
                    error_message=str(exc)[:500],
                )
            except Exception:
                pass

        def store_one(a, vec):
            nonlocal dim
            vec = np.asarray(vec, dtype=np.float32)
            self.repository.upsert_article_embedding(
                article_id=a["article_id"],
                model_id=model,
                dim=int(vec.shape[0]),
                vector_blob=vec.tobytes(),
                content_hash=str(a.get("content_hash") or ""),
                status="ready",
            )
            dim = int(vec.shape[0])

        for start in range(0, len(pending), batch_size):
            chunk = pending[start : start + batch_size]
            texts = [str(a.get("content") or "").strip() for a in chunk]
            vectors = None
            try:
                vectors = self.embed_client.embed_batch(texts)
            except (EmbeddingError, Exception) as exc:
                logger.warning(
                    "embed_articles: 批次 %d-%d 批量失败（%d 篇），拆单重试: %s",
                    start, start + len(chunk) - 1, len(chunk), exc,
                )
            if not vectors or len(vectors) != len(chunk):
                # 批量失败或返回数量不符：拆成单条逐个处理，保留单篇容错
                for a, text in zip(chunk, texts):
                    try:
                        vec = self.embed_client.embed(text)
                        store_one(a, vec)
                        succeeded += 1
                    except (EmbeddingError, Exception) as single_exc:
                        mark_error(a, single_exc)
                        failed += 1
                continue
            for a, vec in zip(chunk, vectors):
                try:
                    store_one(a, vec)
                    succeeded += 1
                except (EmbeddingError, Exception) as exc:  # 单篇落库失败：记 error，继续
                    logger.warning("embed_articles: 文章 %s 失败: %s", a["article_id"], exc)
                    mark_error(a, exc)
                    failed += 1
        logger.info(
            "embed_articles: pack=%s model=%s 待处理=%d 成功=%d 失败=%d dim=%d",
            pack or "*", model, len(pending), succeeded, failed, dim,
        )
        return {
            "model": model,
            "pack_id": pack,
            "processed": len(pending),
            "succeeded": succeeded,
            "failed": failed,
            "dim": dim,
        }

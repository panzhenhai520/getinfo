#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""事件抽取服务（第二阶段）：扫描未抽取文章 → LLM 抽事件 → 落库。

照 embed_articles_service 模式：逐篇处理（受 intel_llm_client._semaphore 并发=2
自限流）、content_hash 失效判定、单篇失败记占位行不阻塞。慢操作（~10s/篇），
单 job 处理篇数受 INTEL_EVENT_EXTRACT_MAX_ARTICLES_PER_RUN 控制（默认 20，
保证 N×10s < lease=300s）。
"""

from __future__ import annotations

import logging
from typing import Dict

import config
from intel_database import IntelRepository
from intel_llm_client import IntelLLMError, intel_llm_client
from industry_pack_runtime import active_industry_composition_service

logger = logging.getLogger(__name__)


class EventExtractService:
    def __init__(self, repository: IntelRepository = None, llm_client=None, composition=None):
        self.repository = repository or IntelRepository()
        self.llm_client = llm_client or intel_llm_client
        self.composition = composition or active_industry_composition_service

    def run(self, *, pack_id: str = "", limit: int = None) -> Dict:
        """对缺事件的文章批量抽取并落库。返回统计摘要。"""
        limit = int(limit or getattr(config, "INTEL_EVENT_EXTRACT_MAX_ARTICLES_PER_RUN", 20))
        max_chars = getattr(config, "INTEL_LLM_EVENT_MAX_INPUT_CHARS", 8000)

        # 解析激活 pack + pack_dict（喂给 LLM prompt 的锚点）
        pack_id = str(pack_id or "").strip()
        pack_dict: Dict = {}
        try:
            snap = self.composition.snapshot()
            pack_dict = snap.get("primary_pack") or {}
            if not pack_id:
                pack_id = str(snap.get("active_industry_pack_id") or "")
        except Exception:
            pass

        articles = self.repository.list_articles_missing_events(
            pack_id=pack_id, limit=limit, max_chars=max_chars
        )
        if not articles:
            logger.info("event_extract: pack=%s 无待抽取文章", pack_id or "*")
            return {"pack_id": pack_id, "processed": 0, "succeeded": 0, "failed": 0, "events": 0}

        succeeded = failed = total_events = 0
        model_id = getattr(self.llm_client, "model_id", "") or ""
        for a in articles:
            article_pack_id = str(a.get("industry_pack_id") or pack_id)
            try:
                events = self.llm_client.extract_events(
                    {"title": a.get("title") or "", "content": a.get("content") or ""},
                    pack_dict,
                )
                self.repository.replace_article_events(
                    article_id=a["article_id"],
                    content_hash=str(a.get("content_hash") or ""),
                    industry_pack_id=article_pack_id,
                    events=events,
                    llm_model_id=model_id,
                )
                succeeded += 1
                total_events += len(events)
            except (IntelLLMError, Exception) as exc:  # 单篇失败：记占位行，继续
                logger.warning("event_extract: 文章 %s 失败: %s", a["article_id"], exc)
                try:
                    self.repository.mark_article_events_error(
                        article_id=a["article_id"],
                        content_hash=str(a.get("content_hash") or ""),
                        industry_pack_id=article_pack_id,
                        error=str(exc),
                        llm_model_id=model_id,
                    )
                except Exception:
                    pass
                failed += 1

        logger.info(
            "event_extract: pack=%s 待处理=%d 成功=%d 失败=%d 事件=%d",
            pack_id or "*", len(articles), succeeded, failed, total_events,
        )
        return {
            "pack_id": pack_id,
            "processed": len(articles),
            "succeeded": succeeded,
            "failed": failed,
            "events": total_events,
        }

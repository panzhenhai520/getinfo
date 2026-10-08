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
from typing import Dict, List

import config
from intel_database import IntelRepository
from intel_llm_client import intel_llm_client
from industry_pack_runtime import active_industry_composition_service

logger = logging.getLogger(__name__)


class EventExtractService:
    def __init__(self, repository: IntelRepository = None, llm_client=None, composition=None,
                 pack_loader=None):
        self.repository = repository or IntelRepository()
        self.llm_client = llm_client or intel_llm_client
        self.composition = composition or active_industry_composition_service
        self.pack_loader = pack_loader
        self._pack_dict_cache: Dict[str, Dict] = {}

    def pack_dict_for(self, pack_id: str, fallback: Dict = None) -> Dict:
        """取该行业包自己的 manifest（事件 prompt 的行业上下文）。

        为什么必须按文章自己的包取：prompt 里写了"只抽与上述行业直接相关的事件，
        若属其它行业返回空"。回填时若一律用**激活包**的上下文，其它包的文章会被
        模型判成"无关"→ 全部抽成 0 事件（实测 3/3 篇命中这个问题）。
        """
        key = str(pack_id or "").strip()
        if not key:
            return fallback if isinstance(fallback, dict) else {}
        if key in self._pack_dict_cache:
            return self._pack_dict_cache[key]
        pack: Dict = {}
        try:
            loader = self.pack_loader
            if loader is None:
                from industry_packs import industry_pack_loader

                loader = industry_pack_loader
            pack = dict(loader.load(key, enabled_only=False) or {})
        except Exception:
            pack = {}
        self._pack_dict_cache[key] = pack
        return pack or (fallback if isinstance(fallback, dict) else {})

    def run(self, *, pack_id: str = "", limit: int = None, articles: List[Dict] = None) -> Dict:
        """对缺事件的文章批量抽取并落库。返回统计摘要。

        `articles` 可显式传入（回填工具用它做"取一批 → 多线程并发处理"，
        避免多个线程各自 list 出同一批文章重复抽）。
        """
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

        if articles is None:
            articles = self.repository.list_articles_missing_events(
                pack_id=pack_id, limit=limit, max_chars=max_chars
            )
        if not articles:
            logger.info("event_extract: pack=%s 无待抽取文章", pack_id or "*")
            return {"pack_id": pack_id, "processed": 0, "succeeded": 0, "failed": 0, "events": 0}

        succeeded = failed = total_events = 0
        model_id = getattr(self.llm_client, "model_id", "") or ""
        for a in articles:
            status, count = self.extract_one(
                a, pack_id=pack_id, pack_dict=pack_dict, model_id=model_id
            )
            if status == "ok":
                succeeded += 1
                total_events += count
            else:
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

    def extract_one(self, article: Dict, *, pack_id: str = "", pack_dict: Dict = None,
                    model_id: str = "") -> tuple:
        """抽一篇文章（线程安全：pack_dict 只读、写库走 repository 的锁）。

        返回 (status, events_count)：status = "ok" / "failed"。
        单篇失败记占位行，绝不阻塞整批——回填工具靠这个做断点续跑。
        """
        pack_id = str(pack_id or "").strip()
        pack_dict = pack_dict if isinstance(pack_dict, dict) else {}
        model_id = str(model_id or getattr(self.llm_client, "model_id", "") or "")
        article_pack_id = str(article.get("industry_pack_id") or pack_id)
        try:
            extracted = self.llm_client.extract_structured(
                {"title": article.get("title") or "", "content": article.get("content") or ""},
                self.pack_dict_for(article_pack_id, pack_dict),
            )
            events = list((extracted or {}).get("events") or [])
            attributes = list((extracted or {}).get("attributes") or [])
            self.repository.replace_article_events(
                article_id=article["article_id"],
                content_hash=str(article.get("content_hash") or ""),
                industry_pack_id=article_pack_id,
                events=events,
                llm_model_id=model_id,
            )
            # 属性与事件同一次调用产出，一起落库（0 条也走 DELETE，保证幂等）
            try:
                self.repository.replace_article_attributes(
                    article_id=article["article_id"],
                    content_hash=str(article.get("content_hash") or ""),
                    industry_pack_id=article_pack_id,
                    attributes=attributes,
                    llm_model_id=model_id,
                )
            except Exception as attr_exc:  # 属性写库失败绝不能影响事件
                logger.warning("event_extract: 文章 %s 属性写库失败: %s",
                               article.get("article_id"), attr_exc)
            return "ok", len(events)
        except Exception as exc:  # 单篇失败：记占位行，继续
            logger.warning("event_extract: 文章 %s 失败: %s", article.get("article_id"), exc)
            try:
                self.repository.mark_article_events_error(
                    article_id=article["article_id"],
                    content_hash=str(article.get("content_hash") or ""),
                    industry_pack_id=article_pack_id,
                    error=str(exc),
                    llm_model_id=model_id,
                )
            except Exception:
                pass
            return "failed", 0

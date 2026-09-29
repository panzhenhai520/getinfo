#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Dedicated persistent worker for market intelligence jobs."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import threading
import time
import uuid
from datetime import timedelta, timezone
from typing import Callable, Dict, Iterable, Optional

import config
from candidate_dispatcher import IntelCandidateDispatcher
from industry_pack_runtime import (
    ActiveIndustryCompositionService,
)
from intel_candidates import IntelCandidateRepository
from intel_classifier import IntelClassificationService
from intel_contracts import utc_now
from intel_database import IntelRepository, intel_repository
from intel_light_scanner import IntelLightScanner
from intel_sources import IntelSourceRegistry
from intel_topics import IntelTopicService
from intel_evidence import IntelEvidenceService
from intel_reports import IntelReportService
from trend_aggregate_service import TrendAggregateService
from embed_articles_service import EmbedArticlesService
from event_extract_service import EventExtractService
from subject_normalize_service import SubjectNormalizeService
from bertopic_topic_service import BertopicTopicService
from financial_worker_jobs import (
    FinancialJobCancelled,
    FinancialJobContext,
    FinancialJobDispatcher,
)
from financial_market_scheduler import (
    FinancialMarketJobService,
    FinancialMarketScheduler,
)
from financial_paper_trading import FinancialPaperTradingJobService


LOGGER = logging.getLogger(__name__)


class IntelWorker:
    def __init__(
        self,
        repository: IntelRepository = None,
        classification_service: IntelClassificationService = None,
        source_registry: IntelSourceRegistry = None,
        light_scanner: IntelLightScanner = None,
        candidate_dispatcher: IntelCandidateDispatcher = None,
        topic_service: IntelTopicService = None,
        evidence_service: IntelEvidenceService = None,
        worker_id: str = "",
        financial_dispatcher: FinancialJobDispatcher = None,
        financial_runners: Optional[Dict[str, Callable]] = None,
        financial_market_scheduler: FinancialMarketScheduler = None,
        job_lease_seconds: Optional[int] = None,
        heartbeat_seconds: Optional[float] = None,
        active_composition_service: ActiveIndustryCompositionService = None,
    ):
        self.repository = repository or intel_repository
        self.active_composition_service = (
            active_composition_service
            or ActiveIndustryCompositionService(
                self.repository.db,
                pack_loader=(source_registry.pack_loader if source_registry else None),
            )
        )
        self.worker_id = worker_id or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        )
        self.classification_service = classification_service or IntelClassificationService(
            repository=self.repository
        )
        self.source_registry = source_registry or IntelSourceRegistry(database=self.repository.db)
        candidate_repository = IntelCandidateRepository(database=self.repository.db)
        self.light_scanner = light_scanner or IntelLightScanner(
            candidate_repository=candidate_repository,
            source_registry=self.source_registry,
        )
        self.candidate_dispatcher = candidate_dispatcher or IntelCandidateDispatcher(
            repository=candidate_repository,
            worker_id=f"{self.worker_id}:candidate",
        )
        self.topic_service = topic_service or IntelTopicService(database=self.repository.db)
        self.evidence_service = evidence_service or IntelEvidenceService(
            database=self.repository.db
        )
        self.report_service = IntelReportService(database=self.repository.db)
        self.trend_aggregate_service = TrendAggregateService(
            repository=self.repository, composition=self.active_composition_service
        )
        self.embed_articles_service = EmbedArticlesService(repository=self.repository)
        self.event_extract_service = EventExtractService(repository=self.repository)
        self.subject_normalize_service = SubjectNormalizeService(repository=self.repository)
        self.bertopic_topic_service = BertopicTopicService(
            repository=self.repository, composition=self.active_composition_service
        )
        self.job_lease_seconds = max(
            30, int(job_lease_seconds or config.INTEL_JOB_LEASE_SECONDS)
        )
        default_heartbeat = min(30.0, self.job_lease_seconds / 3.0)
        self.heartbeat_seconds = max(
            0.01,
            float(default_heartbeat if heartbeat_seconds is None else heartbeat_seconds),
        )
        self.stop_requested = False
        self._active_job_lock = threading.Lock()
        self._active_cancel_events: Dict[int, threading.Event] = {}
        self._context_handler_types = set()
        # 有界并发：同时最多 4 个 enrich 打 VPN（背压/防满载）
        self._enrich_semaphore = threading.Semaphore(6)
        self.handlers: Dict[str, Callable[[Dict], Dict]] = {
            "classification": self._handle_classification,
            "enrich": self._handle_enrich,
            "enrich_repair": self._handle_enrich_repair,
            "source_sync": self._handle_source_sync,
            "light_scan": self._handle_light_scan,
            "candidate_dispatch": self._handle_candidate_dispatch,
            "candidate_rescore": self._handle_candidate_rescore,
            "topic_cluster": self._handle_topic_cluster,
            "report_ingest": self._handle_report_ingest,
            "report_discover": self._handle_report_discover,
            "report_check": self._handle_report_check,
            "industry_revalidate": self._handle_industry_revalidate,
            "trend_aggregate": self._handle_trend_aggregate,
            "embed_articles": self._handle_embed_articles,
            "event_extract": self._handle_event_extract,
            "subject_normalize": self._handle_subject_normalize,
            "bertopic_cluster": self._handle_bertopic_cluster,
            "dynamic_convert": self._handle_dynamic_convert,
            "pack_report": self._handle_pack_report,
            "task_cleanup": self._handle_task_cleanup,
        }
        _financial_enabled = bool(
            getattr(config, "FINANCIAL_INTELLIGENCE_ENABLED", False)
            or getattr(config, "TRADING_AGENTS_ENABLED", False)
        )
        if not _financial_enabled:
            # 财务未启用：跳过财务模块初始化，避免其在 PostgreSQL 环境下
            # 因 PRAGMA/SAVEPOINT 等 SQLite 特性不兼容而阻塞 intel 调度器启动。
            self.financial_dispatcher = None
            self.financial_market_scheduler = None
        elif financial_dispatcher is None:
            market_runners = FinancialMarketJobService(
                self.repository,
                settings=config,
            ).runners()
            market_runners.update(
                FinancialPaperTradingJobService(
                    self.repository,
                    settings=config,
                ).runners()
            )
            market_runners.update(dict(financial_runners or {}))
            self.financial_dispatcher = FinancialJobDispatcher(
                market_runners,
                settings=config,
            )
        else:
            self.financial_dispatcher = financial_dispatcher
        if self.financial_dispatcher is not None:
            self.financial_dispatcher.register_with(self)
        self._financial_market_scheduler_started = False
        self._financial_market_scheduler_log_state = None

    def register_handler(
        self,
        job_type: str,
        handler: Callable,
        *,
        with_context: bool = False,
    ) -> None:
        normalized = str(job_type)
        self.handlers[normalized] = handler
        if with_context:
            self._context_handler_types.add(normalized)
        else:
            self._context_handler_types.discard(normalized)

    def _heartbeat_job(
        self,
        job_id: int,
        cancel_event: threading.Event,
        stop_event: threading.Event,
    ) -> None:
        while not stop_event.wait(self.heartbeat_seconds):
            try:
                status = self.repository.renew_job_lease(
                    job_id,
                    self.worker_id,
                    lease_seconds=self.job_lease_seconds,
                )
            except Exception:
                cancel_event.set()
                return
            if status != "renewed":
                cancel_event.set()
                return

    def _job_context(self, job: Dict) -> FinancialJobContext:
        cancel_event = threading.Event()
        with self._active_job_lock:
            self._active_cancel_events[int(job["id"])] = cancel_event
        return FinancialJobContext(
            job_id=int(job["id"]),
            job_type=str(job["job_type"]),
            worker_id=self.worker_id,
            repository=self.repository,
            cancel_event=cancel_event,
        )

    def _release_job_context(self, job_id: int) -> None:
        with self._active_job_lock:
            self._active_cancel_events.pop(int(job_id), None)

    def _handle_enrich(self, payload: Dict) -> Dict:
        """异步处理 VPN 精炼/翻译/预生成音频（submit+wait 在独立 worker 槽，不阻塞聚合）。

        聚合只需入队 enrich 任务即返回；本 handler 拿回 refined/translated/audio 落库。
        用有界信号量限制同时打 VPN 的 enrich 数（背压），防止 VPN 满载。
        """
        article_id = int(payload.get("article_id") or 0)
        url = str(payload.get("url") or "")
        title = str(payload.get("title") or "")
        content = str(payload.get("content") or "")
        if not article_id:
            return {"success": False, "error": "article_id 缺失"}
        if not (config.REMOTE_PIPELINE_URL and config.REMOTE_PIPELINE_TOKEN):
            return {"success": False, "error": "VPN 未被配置"}
        # 有界并发：同时最多 _ENRICH_CONCURRENCY 个 enrich 打 VPN（背压/防满载）
        try:
            self._enrich_semaphore.acquire(timeout=config.REMOTE_PIPELINE_JOB_TIMEOUT_SECONDS)
        except Exception:
            return {"success": False, "error": "enrich 并发等待超时"}
        # Redis 每域名并发锁（Phase 2.3）；redis 不可用则跳过，回落到 semaphore
        _lock_key = ""
        try:
            import redis as _redis
            _r = _redis.Redis(host=config.REDIS_HOST, port=config.REDIS_PORT,
                              db=config.REDIS_DB, socket_connect_timeout=2)
            _r.ping()
            _domain = url.split('/')[2] if url.count('/') >= 2 else url
            _lock_key = f"enrich-lock:{_domain}"
            if not _r.set(_lock_key, "1", nx=True, ex=300):
                return {"success": False, "error": "该域名 enrich 并发受限，稍后重试"}
        except Exception:
            _lock_key = ""  # redis 不可用：跳过，靠 semaphore 兜底
        try:
            from remote_pipeline_client import RemotePipelineError, RemotePipelineUnavailable, remote_pipeline_client
            from remote_result_ingestor import _write_article_derivative
            article = self.repository.get_article(article_id) or {}
            if not content:
                content = str(article.get("content") or "")
            # 行业包锚定：payload 里的 declaring_pack_id/primary_industry_pack_id/industry_pack_id
            # 任一存在即加载行业包名与固定主题，供 VPN 端分层提炼做相关性锚定
            pack_id = str(
                payload.get("industry_pack_id")
                or payload.get("declaring_pack_id")
                or payload.get("primary_industry_pack_id")
                or ""
            ).strip()
            pack_name = ""
            pack_topics = []
            if pack_id:
                try:
                    from industry_packs import industry_pack_loader
                    pack = industry_pack_loader.load(pack_id, enabled_only=False) or {}
                    pack_name = str(pack.get("name") or "")
                    pack_topics = []
                    for t in (pack.get("fixed_topics") or []):
                        if isinstance(t, dict):
                            pack_topics.append({
                                "key": str(t.get("key") or "").strip(),
                                "name": str(t.get("name") or t.get("key") or "").strip(),
                            })
                        else:
                            name = str(t).strip()
                            if name:
                                pack_topics.append({"key": name, "name": name})
                except Exception:
                    pack_name = ""
            result = remote_pipeline_client.enrich(
                url=url or str(article.get("url") or ""),
                title=title or str(article.get("title") or ""),
                content=content,
                enrich=config.REMOTE_PIPELINE_ENRICH,
                # TTS 总闸：SYSTEM_TTS_ENABLED=False 时即使 REMOTE_PIPELINE_TTS 误开也不请求语音
                tts=config.REMOTE_PIPELINE_TTS and getattr(config, 'SYSTEM_TTS_ENABLED', False),
                voice=config.REMOTE_PIPELINE_TTS_VOICE,
                industry_pack_id=pack_id,
                industry_pack_name=pack_name,
                industry_topics=pack_topics,
            )
            # 用 VPN LLM 二次创作摘要替换主内容（不保留原文），保留 URL 供对比。
            # 仅保留纯文本：去 HTML 标签、markdown 图片/链接 token（保留文字），保证不入库图片/HTML。
            import re as _re  # noqa: PLC0415
            _refined = _re.sub(r"<[^>]+>", "", str(result.get("refined_content") or ""))
            _refined = _re.sub(r"!\[[^\]]*\]\([^)]*\)", "", _refined)
            _refined = _re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", _refined)
            _refined = _re.sub(r"\n{3,}", "\n\n", _refined).strip()
            # VPN 侧精炼为空（模型异常/内容不可用）不能算成功：必须重试，
            # 否则正文永远停留在原始噪声状态
            if not _refined:
                return {"success": False, "error": "VPN 返回空精炼内容"}
            # 分层提炼元数据（先算好：替换正文与写 derivative 两处共用）
            _refine_meta = {
                "content_type": str(result.get("content_type") or ""),
                "relevance": str(result.get("relevance") or ""),
                "core_facts": list(result.get("core_facts") or [])[:5],
            }
            # B/C 类兜底文本不是正文：relevance=none 或固定噪声标记时保留原文，
            # 避免把「与行业包无相关内容」的占位文本写进 articles.content。
            _noise_markers = ("本文无实质正文内容", "本文为聚合列表，与行业包无实质相关内容")
            if _refined and _refine_meta["relevance"] != "none" and _refined not in _noise_markers:
                import hashlib as _hl  # noqa: PLC0415
                # 阶段1联动：content 被精炼摘要替换后，同步刷新展示用 Markdown
                # （不沿用旧 content_markdown，否则详情页仍显示精炼前的噪声正文）。
                try:
                    from content_handlers import build_article_markdown
                    _refined_markdown = build_article_markdown('', _refined)
                except Exception:
                    _refined_markdown = _refined
                with self.repository.db.lock:
                    self.repository.db.connection.execute(
                        "UPDATE articles SET content=?, content_hash=?, content_length=?, content_markdown=? WHERE id=?",
                        (_refined, _hl.md5(_refined.encode("utf-8")).hexdigest(), len(_refined), _refined_markdown, int(article_id)),
                    )
                    self.repository.db.connection.commit()
            derivative = {
                "url": url or str(article.get("url") or ""),
                "raw_content": "",  # 不保留原文，只留摘要 + URL
                "refined_content": _refined,
                "refined_title": result.get("title") or title,
                "translated_content": result.get("translated_content") or "",
                "audio_manifest": result.get("audio_manifest") or {},
                "source_language": result.get("source_language") or "zh",
                "target_language": result.get("target_language") or ("en" if (result.get("source_language") or "zh") == "zh" else "zh"),
                "refine_error": "", "translation_error": "", "audio_error": "",
                # 分层提炼元数据：content_type(A|B|C)/relevance(high|low|none)/core_facts
                "refine_meta": _refine_meta,
            }
            _write_article_derivative(self.repository.db, int(article_id), derivative, str(result.get("job_id") or ""))
            return {"success": True, "article_id": article_id, "job_id": str(result.get("job_id") or "")}
        except (RemotePipelineUnavailable, RemotePipelineError) as exc:
            return {"success": False, "error": f"VPN 精炼失败: {exc}"}
        except Exception as exc:
            return {"success": False, "error": f"VPN 精炼异常: {exc}"}
        finally:
            if _lock_key:
                try:
                    import redis as _redis
                    _redis.Redis(host=config.REDIS_HOST, port=config.REDIS_PORT,
                                 db=config.REDIS_DB, socket_connect_timeout=2).delete(_lock_key)
                except Exception:
                    pass
            self._enrich_semaphore.release()

    def _extract_product_fields(self, text: str, title: str = "") -> Dict:
        """用本地 LLM 从 正文+OCR 文本 里抽 产品功能/应用场景/核心参数。

        只做结构化抽取，不做总结。LLM 未启用或调用失败时返回空字段（调用方仍会落 OCR 文本）。
        """
        empty = {"product_features": "", "application_scenarios": "", "core_params": ""}
        if not getattr(config, "INTEL_LLM_ENABLED", False):
            return empty
        sample = "\n".join(x for x in (title, str(text or "")) if x).strip()
        if not sample:
            return empty
        window = sample[:11000]
        from intel_llm_client import intel_llm_client, IntelLLMError
        prompt = (
            "从下面网页文本（可能含 OCR 识别出的图片文字）中抽取产品信息。"
            "只输出 JSON，不要任何解释："
            '{"product_features":"该产品的功能/能力，逐条简短罗列",'
            '"application_scenarios":"该产品的应用场景/行业/场合",'
            '"core_params":"核心参数/型号/规格/技术指标"}。'
            "能抽出多少写多少，缺失的字段填空字符串；忽略导航、广告、页脚等噪声。\n"
            f"标题:{title[:300]}\n正文/OCR:\n{window}"
        )
        try:
            payload = {
                "model": intel_llm_client._local_runtime().get("model_id"),
                "messages": [
                    {"role": "system", "content": "你是产品资料抽取服务，只输出符合 Schema 的 JSON，不输出推理过程。"},
                    {"role": "user", "content": prompt},
                ],
                "stream": False, "max_tokens": 900, "temperature": 0.1, "enable_thinking": False,
            }
            response = intel_llm_client.request_openai_compatible(payload, timeout_seconds=60)
            body = response.json()
            choices = body.get("choices") if isinstance(body, dict) else None
            content = (
                choices[0].get("message", {}).get("content")
                if isinstance(choices, list) and choices and isinstance(choices[0], dict)
                else None
            )
            if not content:
                return empty
            import re as _re, json as _json
            m = _re.search(r"\{.*\}", content, _re.S)
            data = _json.loads(m.group(0)) if m else {}
            return {
                "product_features": str(data.get("product_features") or "").strip()[:2000],
                "application_scenarios": str(data.get("application_scenarios") or "").strip()[:2000],
                "core_params": str(data.get("core_params") or "").strip()[:2000],
            }
        except (IntelLLMError, Exception):
            return empty

    def _upsert_article_fields(
        self,
        article_id: int,
        *,
        product_features: str = "",
        application_scenarios: str = "",
        core_params: str = "",
        ocr_text: str = "",
        ocr_summary: str = "",
        ocr_title: str = "",
        source: str = "",
    ) -> Dict:
        try:
            from intel_schema import ensure_intel_article_field_tables
            with self.repository.db.lock:
                cur = self.repository.db.connection.cursor()
                ensure_intel_article_field_tables(cur)
                cur.execute(
                    """INSERT INTO intel_article_fields (
                        article_id, product_features, application_scenarios, core_params,
                        ocr_text, ocr_summary, ocr_title, source, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?, datetime('now'))
                    ON CONFLICT(article_id) DO UPDATE SET
                        product_features=excluded.product_features,
                        application_scenarios=excluded.application_scenarios,
                        core_params=excluded.core_params,
                        ocr_text=excluded.ocr_text,
                        ocr_summary=excluded.ocr_summary,
                        ocr_title=excluded.ocr_title,
                        source=excluded.source,
                        updated_at=excluded.updated_at""",
                    (
                        int(article_id), str(product_features or "")[:2000],
                        str(application_scenarios or "")[:2000], str(core_params or "")[:2000],
                        str(ocr_text or "")[:200000], str(ocr_summary or "")[:5000],
                        str(ocr_title or "")[:500], str(source or "")[:50],
                    ),
                )
                self.repository.db.connection.commit()
                cur.close()
            return {"success": True}
        except Exception as exc:
            return {"success": False, "error": str(exc)[:200]}

    def _handle_enrich_repair(self, payload: Dict) -> Dict:
        """短文/图片型产品页：送 VPN OCR 读图，LLM 抽 产品功能/应用场景/核心参数 回填。

        正文很短而关键信息常在产品图/参数表里（正文抽取读不到）。用现有 ``/v1/pipeline/ocr``
        取回整页截图 OCR 文本（含图内文字），再尽力用本地 LLM 抽结构化字段；LLM 不可用时
        仍保存 OCR 文本，保证“补全”不落空、详情页有内容可展示。
        """
        article_id = int(payload.get("article_id") or 0)
        if not article_id:
            return {"success": False, "error": "article_id 缺失"}
        if not (config.REMOTE_PIPELINE_URL and config.REMOTE_PIPELINE_TOKEN):
            return {"success": False, "error": "VPN 未被配置"}
        article = self.repository.get_article(article_id) or {}
        url = str(payload.get("url") or article.get("url") or "")
        title = str(payload.get("title") or article.get("title") or "")
        content = str(payload.get("content") or article.get("content") or "")
        ocr = {}
        try:
            from remote_pipeline_client import remote_pipeline_client
            ocr = remote_pipeline_client.ocr(url=url)
        except Exception as exc:
            return {"success": False, "error": f"VPN OCR 失败: {exc}"}
        ocr_text = str(ocr.get("ocr_text") or "").strip()
        ocr_summary = str(ocr.get("summary") or "").strip()
        ocr_title = str(ocr.get("title") or title).strip()
        combined = "\n".join(x for x in (content, ocr_text) if x)
        fields = self._extract_product_fields(combined, title)
        written = self._upsert_article_fields(
            article_id,
            product_features=fields.get("product_features", ""),
            application_scenarios=fields.get("application_scenarios", ""),
            core_params=fields.get("core_params", ""),
            ocr_text=ocr_text, ocr_summary=ocr_summary, ocr_title=ocr_title,
            source="vpn_ocr",
        )
        return {
            "success": bool(written.get("success")),
            "article_id": article_id,
            "ocr_length": len(ocr_text),
            "product_features": len(fields.get("product_features") or ""),
            "application_scenarios": len(fields.get("application_scenarios") or ""),
            "core_params": len(fields.get("core_params") or ""),
            "write_error": written.get("error", "") if not written.get("success") else "",
        }

    def _handle_candidate_rescore(self, payload: Dict) -> Dict:
        """关键词/配置变化后，重新打分 discovered 候选并把命中的提升为 queued。"""
        from intel_candidates import IntelCandidateRepository
        repo = IntelCandidateRepository(self.repository.db)
        pack_id = str(payload.get("industry_pack_id")
                      or payload.get("primary_industry_pack_id")
                      or config.INTEL_DEFAULT_INDUSTRY_PACK)
        activation_id = str(payload.get("activation_id") or "")
        return repo.rescore_discovered_candidates(pack_id, activation_id=activation_id)

    def _handle_classification(self, payload: Dict) -> Dict:
        if not config.INTEL_CLASSIFICATION_ENABLED:
            return {"skipped": True, "reason": "classification disabled"}
        article_id = int(payload.get("article_id") or 0)
        industry_pack_id = (
            payload.get("industry_pack_id") or config.INTEL_DEFAULT_INDUSTRY_PACK
        )
        result = self.classification_service.classify_article_id(
            article_id,
            industry_pack_id,
            activation_id=str(payload.get("activation_id") or ""),
        )
        anchor_hits = (
            ((result.get("score_details") or {}).get("hits") or {}).get("anchor")
            or []
        )
        if anchor_hits and bool(payload.get("ragflow_upload")):
            try:
                from industry_ragflow import resolve_industry_ragflow_kb_id
                from ragflow_client import get_ragflow_client

                kb_id = resolve_industry_ragflow_kb_id(industry_pack_id)
                article = self.repository.get_article(article_id) or {}
                if kb_id and str(article.get("content") or "").strip():
                    upload = get_ragflow_client().upload_article(
                        kb_id,
                        str(article.get("title") or "无标题"),
                        str(article.get("content") or ""),
                        str(article.get("url") or ""),
                        {
                            "db_id": article_id,
                            "industry_pack_id": industry_pack_id,
                            "activation_id": str(payload.get("activation_id") or ""),
                        },
                    )
                    result["ragflow_upload"] = {
                        "enabled": True,
                        "knowledge_base_key": "news",
                        "success": bool(upload.get("success")),
                    }
                else:
                    result["ragflow_upload"] = {"enabled": False}
            except Exception as exc:
                LOGGER.warning(
                    "industry RAGFlow upload failed for article %s: %s",
                    article_id,
                    exc,
                )
                result["ragflow_upload"] = {
                    "enabled": True,
                    "success": False,
                    "error": "upload_failed",
                }
        if config.INTEL_TOPIC_CLUSTER_ENABLED and result.get("topic_tags"):
            content_hash = result.get("article_content_hash") or ""
            self.repository.enqueue_job(
                "topic_cluster",
                f"topic-cluster:{industry_pack_id}:{article_id}:{content_hash}",
                {
                    "industry_pack_id": industry_pack_id,
                    "activation_id": str(payload.get("activation_id") or ""),
                    "manual": False,
                },
                priority=-40,
            )
        return result

    def _handle_source_sync(self, payload: Dict) -> Dict:
        if not config.INTEL_SOURCE_SYNC_ENABLED and not payload.get("manual"):
            return {"skipped": True, "reason": "automatic source sync disabled"}
        result = self.source_registry.sync_legacy_sources(
            dry_run=False,
            page_size=payload.get("page_size") or 200,
            # Pre-industry legacy schedules are installation-owned family
            # office sources; switching packs must never reassign them.
            default_industry_pack_id="family_office",
        )
        active = self.active_composition_service.snapshot()
        projection = self.source_registry.project_effective_sources_to_managed_urls(
            str(active["active_industry_pack_id"]),
            effective_pack_ids=active["effective_pack_ids"],
            activation_id=str(active.get("active_industry_activation_id") or ""),
            industry_pack_version_id=active.get("active_industry_pack_version_id"),
            project_keywords=active.get("project_keywords") or [],
        )
        result["managed_url_projection"] = projection
        return result

    def _handle_light_scan(self, payload: Dict) -> Dict:
        crawl_tasks = {
            str(source_id): str(task_id)
            for source_id, task_id in (
                payload.get('crawl_task_ids_by_source') or {}
            ).items()
            if str(source_id) and str(task_id)
        }
        for task_id in crawl_tasks.values():
            self.repository.db.update_crawl_task_status(
                task_id, 'running', progress=10, error_message=''
            )
        try:
            report = self.light_scanner.scan(
                industry_pack_id=payload.get("industry_pack_id")
                or config.INTEL_DEFAULT_INDUSTRY_PACK,
                source_ids=payload.get("source_ids"),
                scan_sources=payload.get("scan_sources", True),
                include_serpapi=payload.get("include_serpapi", True),
                max_sources=payload.get("max_sources"),
                max_items_per_source=payload.get("max_items_per_source"),
                manual=bool(payload.get("manual")),
                activation_id=str(payload.get("activation_id") or ""),
                force_rescan=bool(payload.get("switch_full_scan")),
                force_rescan_key=str(payload.get('force_rescan_key') or ''),
                initialization_from=str(payload.get('initialization_from') or ''),
                initialization_to=str(payload.get('initialization_to') or ''),
            )
        except Exception as exc:
            for task_id in crawl_tasks.values():
                self.repository.db.update_crawl_task_status(
                    task_id, 'failed', progress=100, error_message=str(exc)[:1000]
                )
            raise

        completed_sources = set()
        for run in report.get('runs') or []:
            source_id = run.get('source_id')
            if source_id is None:
                continue
            source_key = str(source_id)
            task_id = crawl_tasks.get(source_key)
            if not task_id:
                continue
            completed_sources.add(source_key)
            status = str(run.get('status') or '')
            failed = status in {'failed', 'partial', 'rate_limited'}
            self.repository.db.update_crawl_task_status(
                task_id,
                'failed' if failed else 'completed',
                progress=100,
                articles_found=int(run.get('discovered_count') or 0),
                articles_processed=int(run.get('queued_count') or 0),
                error_message=(
                    str(run.get('error_message') or run.get('reason') or '')[:1000]
                    if failed else ''
                ),
            )
        for source_key, task_id in crawl_tasks.items():
            if source_key not in completed_sources:
                self.repository.db.update_crawl_task_status(
                    task_id,
                    'failed',
                    progress=100,
                    error_message='初始化扫描未返回该信源的执行结果',
                )
        report['crawl_task_updates'] = len(crawl_tasks)
        try:
            from industry_collection_runtime import finalize_industry_initialization
            report['initialization_finalization'] = finalize_industry_initialization(
                activation_id=str(payload.get('activation_id') or ''),
                industry_pack_id=str(payload.get('industry_pack_id') or ''),
                report=report,
            )
        except Exception as exc:
            # A scan result remains valid even if promotion needs a retry; do
            # not turn a successful source scan into a failed job.
            report['initialization_finalization'] = {
                'finalized': False,
                'reason': str(exc)[:500],
            }
        return report

    def _handle_candidate_dispatch(self, payload: Dict) -> Dict:
        # 定向派发（主题搜索词测试）：只爬这批候选，并跳过全量重打分——
        # 重打分会把所有 discovered 候选一并提升为 queued，与"只测这一个主题"相矛盾。
        raw_candidate_ids = payload.get("candidate_ids")
        if isinstance(raw_candidate_ids, (list, tuple)):
            normalized_ids = []
            for value in raw_candidate_ids:
                try:
                    candidate_id = int(value)
                except (TypeError, ValueError):
                    continue
                if candidate_id > 0:
                    normalized_ids.append(candidate_id)
            if not normalized_ids:
                return {
                    "skipped": True,
                    "reason": "no candidate ids supplied",
                    "claimed": 0,
                    "crawled": 0,
                    "retry_wait": 0,
                    "failed": 0,
                }
            return self.candidate_dispatcher.dispatch_once(
                limit=payload.get("limit"),
                manual=bool(payload.get("manual")),
                candidate_ids=normalized_ids,
                activation_id=str(payload.get("activation_id") or ""),
            )
        # 关键词/配置变化后，先自动重打分 discovered 候选并把命中的提升为 queued，
        # 再在同一轮派发器里接手。这样即使 separate candidate_rescore job 因队列过载
        # 未被及时领取，自动重打分也能随每次派发稳定运行，彻底无需人工介入。
        pack_id = str(
            payload.get("industry_pack_id") or payload.get("primary_industry_pack_id")
            or config.INTEL_DEFAULT_INDUSTRY_PACK
        )
        try:
            from intel_candidates import IntelCandidateRepository
            _rescore = IntelCandidateRepository(self.repository.db).rescore_discovered_candidates(
                pack_id,
                activation_id=str(payload.get("activation_id") or ""),
            )
        except Exception as _rescore_exc:
            _rescore = {"error": str(_rescore_exc)[:120]}
        return self.candidate_dispatcher.dispatch_once(
            limit=payload.get("limit"),
            manual=bool(payload.get("manual")),
            activation_id=str(payload.get("activation_id") or ""),
            # 按行业包限定派发范围：即使 payload 被异包激活上下文污染，
            # claim 也只看本包 should_queue=1 的候选（激活 ID 宽匹配）
            industry_pack_id=pack_id,
        )

    def _handle_topic_cluster(self, payload: Dict) -> Dict:
        pack_id = payload.get("industry_pack_id") or config.INTEL_DEFAULT_INDUSTRY_PACK
        # evidence rebuild 与 topic cluster 相互独立。evidence 层在 Postgres/
        # postgres_compat 下可能因 SQLite 专用 JSON 函数（json_extract/json_array_length/
        # datetime）失败，但主题聚类必须照常跑，否则 customers 等主题卡不会随新入库
        # 文章自动更新。故 evidence 失败只记日志、不阻断 cluster。
        try:
            evidence = self.evidence_service.rebuild(pack_id)
        except Exception as exc:
            LOGGER.warning("evidence rebuild failed (skip evidence, still cluster): %s", exc)
            evidence = {"error": str(exc), "skipped": True}
        topics = self.topic_service.cluster(
            pack_id,
            manual=bool(payload.get("manual")),
        )
        return {**topics, "evidence": evidence}

    def _handle_trend_aggregate(self, payload: Dict) -> Dict:
        """周期/手动触发：聚合行业趋势（关键词/品牌）× 天，算爆发与状态机并落库。"""
        pack_id = payload.get("industry_pack_id") or config.INTEL_DEFAULT_INDUSTRY_PACK
        dimension = payload.get("dimension") or "trend_keyword"
        return self.trend_aggregate_service.run(pack_id=pack_id, dimension=dimension)

    def _handle_bertopic_cluster(self, payload: Dict) -> Dict:
        """周期/手动触发：BERTopic 主题聚类（复用 bge-m3 嵌入）+ LLM 命名 + 动态主题趋势落库。"""
        pack_id = payload.get("industry_pack_id") or config.INTEL_DEFAULT_INDUSTRY_PACK
        return self.bertopic_topic_service.run(pack_id=pack_id)

    def _handle_embed_articles(self, payload: Dict) -> Dict:
        """周期/手动触发：为缺向量文章产出 bge-m3 向量并落库。"""
        pack_id = payload.get("industry_pack_id") or ""
        limit = payload.get("limit")
        return self.embed_articles_service.run(limit=limit, pack_id=pack_id)

    def _handle_event_extract(self, payload: Dict) -> Dict:
        """周期/手动触发：LLM 抽取文章结构化事件并落库（第二阶段）。"""
        pack_id = payload.get("industry_pack_id") or ""
        limit = payload.get("limit")
        return self.event_extract_service.run(limit=limit, pack_id=pack_id)

    def _handle_subject_normalize(self, payload: Dict) -> Dict:
        """手动/周期触发：嵌入聚类归并同主体到话题级规范名（T5）。"""
        pack_id = payload.get("industry_pack_id") or ""
        return self.subject_normalize_service.run(pack_id=pack_id)

    def _handle_report_ingest(self, payload: Dict) -> Dict:
        from intel_reports import ReportSkipError
        candidate_id = payload.get("candidate_id")
        try:
            result = self.report_service.ingest(
                str(payload.get("report_url") or ""),
                payload.get("industry_pack_id") or config.INTEL_DEFAULT_INDUSTRY_PACK,
                source_id=payload.get("source_id"),
                title=str(payload.get("title") or ""),
                ragflow_kb_id=str(payload.get("ragflow_kb_id") or ""),
            )
            if candidate_id:
                self.report_service.mark_report_candidate(int(candidate_id), status="ingested")
            return result
        except ReportSkipError as exc:
            # 报告页无真实 PDF/目录页：预期跳过，任务按完成处理（不计失败、不重试、不告警）
            if candidate_id:
                self.report_service.mark_report_candidate(int(candidate_id), status="skipped", last_error=str(exc))
            return {"skipped": True, "reason": str(exc)}
        except Exception as exc:
            if candidate_id:
                self.report_service.mark_report_candidate(int(candidate_id), status="failed", last_error=str(exc))
            raise

    def _handle_report_discover(self, payload: Dict) -> Dict:
        """报告 URL 探测：对 content_type='report' 信源发现 .pdf 直链并登记候选后自动下载。"""
        from intel_reports import intel_report_service
        pack_ids = [str(payload.get("industry_pack_id") or "")]
        if not pack_ids[0]:
            active = self.active_composition_service.snapshot()
            # 多用户：默认覆盖“全局活跃包 + 所有有绑定用户的包”，
            # 否则切换活跃包后其它行业的报告不再探测/下载。
            from pack_tenant import packs_with_bound_users
            pack_ids = [str(active.get("active_industry_pack_id") or config.INTEL_DEFAULT_INDUSTRY_PACK)]
            for _bound_pack_id in packs_with_bound_users():
                if _bound_pack_id not in pack_ids:
                    pack_ids.append(_bound_pack_id)
        results = {}
        enqueued = 0
        for pack_id in pack_ids:
            if not pack_id:
                continue
            with self.repository.db.lock:
                cur = self.repository.db.connection.cursor()
                try:
                    cur.execute("SELECT id, source_url, source_name, report_section_url FROM intel_sources WHERE content_type='report' AND is_enabled=1")
                    sources = [dict(r) for r in cur.fetchall()]
                finally:
                    cur.close()
            for src in sources:
                try:
                    # 用配置的"报告下载栏目 URL"作为种子定向爬；没配则回退首页+sitemap
                    disc = intel_report_service.discover_report_pdfs(
                        src["id"], pack_id, src["source_url"],
                        seed_url=str(src.get("report_section_url") or "").strip(),
                    )
                    results[str(src["source_url"])] = disc["found"]
                except Exception as exc:
                    results[str(src["source_url"])] = f"err:{str(exc)[:60]}"
            # 自动下载：把新发现的 pending 候选入队 report_ingest
            pending = intel_report_service.pending_report_candidates(pack_id, limit=100)
            for cand in pending:
                intel_report_service.mark_report_candidate(int(cand["id"]), status="queued")
                self.repository.enqueue_job(
                    "report_ingest", f"report-ingest:cand:{int(cand['id'])}",
                    {"report_url": str(cand.get("report_url") or ""),
                     "industry_pack_id": pack_id, "source_id": cand.get("source_id"),
                     "title": str(cand.get("title_hint") or ""), "candidate_id": int(cand["id"])},
                    priority=5, request_id="report-discover",
                )
                enqueued += 1
        return {"packs": pack_ids, "discovered": results, "enqueued_downloads": enqueued}

    def _handle_report_check(self, payload: Dict) -> Dict:
        return self.report_service.check_known_reports(
            payload.get("industry_pack_id") or config.INTEL_DEFAULT_INDUSTRY_PACK
        )

    def _handle_dynamic_convert(self, payload: Dict) -> Dict:
        """阶段3：无正文条目的原文链接 → Markdown（缓存 24h；失败重试 ≤2 次由任务框架负责）。"""
        from dynamic_link_converter import convert_url_to_markdown, record_failure
        url = str(payload.get("url") or "").strip()
        if not url:
            raise ValueError("dynamic_convert 缺少 url")
        try:
            return convert_url_to_markdown(self.repository.db, url)
        except Exception as exc:
            record_failure(self.repository.db, url, str(exc)[:400])
            raise

    def _handle_pack_report(self, payload: Dict) -> Dict:
        """AI 周报：时间窗 + 行业包 → 向量检索 + 时序统计 + LLM Markdown 总结落库。
        （周五定时任务与「测试提示词效果」共用同一入口）"""
        from pack_report import run_pack_report_job
        return run_pack_report_job(payload or {})

    def _handle_task_cleanup(self, payload: Dict) -> Dict:
        """终态任务自动清理：删除保留期之前的 completed/failed/cancelled 任务与失效候选。"""
        from task_retention import cleanup_terminal_records
        return {"success": True, **cleanup_terminal_records(self.repository.db)}

    def _handle_industry_revalidate(self, payload: Dict) -> Dict:
        """Re-run the new anchor gate and hide historic off-topic articles."""
        pack_id = payload.get("industry_pack_id") or config.INTEL_DEFAULT_INDUSTRY_PACK
        limit = max(1, min(int(payload.get("limit") or 5000), 20000))
        self.repository.db._ensure_connection()
        with self.repository.db.lock:
            cursor = self.repository.db.connection.cursor()
            try:
                cursor.execute("SELECT id FROM articles WHERE status='active' ORDER BY id LIMIT ?", (limit,))
                article_ids = [int(row[0]) for row in cursor.fetchall()]
            finally:
                cursor.close()
        from intel_content_quality_gate import assess_article_quality
        # archive=False（用户从页面触发的"重新聚类"）时只做分类，绝不归档任何文章
        allow_archive = bool(payload.get("archive", True))
        # 危险防护：该任务会把"未命中锚点"的文章归档。若该行业包根本没配锚点/机构词，
        # hits 必然为空，就会把该包**全部历史文章**归档（实测风险，会一次性清空资讯流）。
        # 因此只有当该包确实配了门禁词时，未命中才视为不该入库。
        anchor_gate_configured = False
        try:
            from industry_packs import industry_anchor_keywords, industry_pack_loader
            anchor_gate_configured = bool(
                industry_anchor_keywords(industry_pack_loader.load(pack_id, enabled_only=False))
            )
        except Exception:
            anchor_gate_configured = False
        print(f"🧭 重分类门禁检查：包={pack_id} 已配锚点词={anchor_gate_configured}（为假时不会归档任何文章）")
        hidden = 0
        quality_hidden = 0
        for article_id in article_ids:
            article = self.repository.get_article(article_id) or {}
            quality = assess_article_quality(article, {})
            result = self.classification_service.classify_article_id(article_id, pack_id)
            hits = ((result.get('score_details') or {}).get('hits') or {}).get('anchor') or []
            if (anchor_gate_configured and allow_archive and not hits) or (allow_archive and not quality.get('passed')):
                with self.repository.db.lock:
                    cursor = self.repository.db.connection.cursor()
                    try:
                        cursor.execute("UPDATE articles SET status='archived', updated_at=datetime('now') WHERE id=?", (article_id,))
                        cursor.execute("UPDATE article_ragflow_documents SET sync_status='delete_pending' WHERE article_id=? AND sync_status IN ('uploaded','parsed')", (article_id,))
                        self.repository.db.connection.commit()
                        hidden += 1
                        quality_hidden += int(bool(quality.get('issues')))
                    finally:
                        cursor.close()
        # 链式：重分类完成后立刻重跑主题投影。投影只认"有本包分类记录"的文章，
        # 不串联的话用户点一次"重新按此主题聚类"仍会看到新主题 0 篇。
        cluster_chain = {}
        try:
            cluster_id, cluster_created = self.repository.enqueue_job(
                "topic_cluster",
                f"reclassify-cluster:{pack_id}:{utc_now().date().isoformat()}",
                {"industry_pack_id": pack_id, "manual": True},
                priority=15, created_by="industry_revalidate",
            )
            cluster_chain = {"job_id": cluster_id, "created": bool(cluster_created)}
        except Exception as exc:
            cluster_chain = {"error": str(exc)[:200]}
        return {"checked": len(article_ids), "hidden": hidden, "hidden_for_quality": quality_hidden,
                "ragflow_cleanup": "delete_pending", "archive_allowed": allow_archive,
                "topic_cluster": cluster_chain}

    def enqueue_due_periodic_jobs(self) -> None:
        """Schedule daily work once per UTC day; active-job dedupe handles races.

        多行业包并行：全局激活包 + 所有「有绑定用户」的行业包各自独立排程，
        切换激活包不再停掉其它行业（其它用户）的采集与搜索。
        """
        active = self.active_composition_service.snapshot()
        from pack_tenant import packs_with_bound_users
        target_pack_ids = [str(active["active_industry_pack_id"])]
        for _bound in packs_with_bound_users():
            if _bound and _bound not in target_pack_ids:
                target_pack_ids.append(_bound)
        for _pack_id in target_pack_ids:
            try:
                self._enqueue_due_periodic_jobs_for_pack(active, str(_pack_id))
            except Exception as _exc:
                print(f"⚠️ 行业包 {_pack_id} 周期任务入队失败: {_exc}")
        # 终态任务自动清理：每天入队一次（全局一次，不按包）
        try:
            _cleanup_day = utc_now().date().isoformat()
            _cleanup_key = f"task-cleanup:daily:{_cleanup_day}"
            if not self.repository.get_job_by_dedupe_key(_cleanup_key):
                self.repository.enqueue_job(
                    "task_cleanup", _cleanup_key, {}, priority=-20,
                    created_by="task-retention-schedule",
                )
                print(f"🧹 终态任务清理已入队（保留 {1} 天，去重键 {_cleanup_key}）")
        except Exception as _exc:
            print(f"⚠️ 终态任务清理入队失败: {_exc}")

    def _enqueue_due_periodic_jobs_for_pack(self, active: Dict, pack_id: str) -> None:
        """单个行业包的每日周期任务入队（搜索/信源扫描/派发/聚类等）。"""
        if str(pack_id) == str(active["active_industry_pack_id"]):
            pack_context = active
        else:
            from industry_pack_runtime import industry_pack_snapshot
            pack_context = industry_pack_snapshot(pack_id)
        activation_id = str(pack_context.get("active_industry_activation_id") or "")
        job_context = {
            "activation_id": activation_id,
            "primary_industry_pack_id": pack_id,
            "industry_pack_version_id": pack_context.get("active_industry_pack_version_id"),
        }
        # 搜索引擎定时采集：默认每天 1 轮（08:20，中国时区）。
        # 到点才入队 + 按"日期+时间点"去重 → 一天只跑一次，不高频；到点前不会触发。
        if getattr(config, 'SEARCH_ENABLED', False) and int(getattr(config, 'SEARCH_RUNS_PER_DAY', 0) or 0) > 0:
            try:
                from utils import get_china_time
                _now = get_china_time()
                _slots = [s.strip() for s in str(getattr(config, 'SEARCH_RUN_HOURS', '') or '').split(',') if s.strip()]
                _slot = _slots[0] if _slots else '08:20'
                _hh, _, _mm = _slot.partition(':')
                _due_at = '%02d:%02d' % (int(_hh or 0), int(_mm or 0))
                _day = _now.date().isoformat()
                _key = f"search-round:daily:{activation_id or pack_id}:{_day}:{_due_at}"
                if _now.strftime('%H:%M') >= _due_at and not self.repository.get_job_by_dedupe_key(_key):
                    self.repository.enqueue_job(
                        "light_scan",
                        _key,
                        {
                            "industry_pack_id": pack_id,
                            "activation_id": activation_id,
                            "primary_industry_pack_id": pack_id,
                            "industry_pack_version_id": active.get("active_industry_pack_version_id"),
                            "include_serpapi": True,
                            "include_tavily": True,
                            "manual": False,
                            "search_round": True,
                        },
                        priority=20,
                        created_by="search-schedule",
                    )
                    print(f"🔍 搜索引擎定时采集已入队：{_day} {_due_at}（去重键 {_key}）")
            except Exception as exc:
                print(f"⚠️ 搜索引擎定时采集入队失败: {exc}")

        # AI 周报：每周五（中国时区）为每个行业包派发一次 LLM 总结任务，
        # 时间窗 = 最近 7 天（周五、四、三、二、一、上周日、上周六）。
        # 按「包 + ISO 周」去重，一天只入队一次；LLM 生成失败由 job 重试机制负责。
        if getattr(config, 'INTEL_WEEKLY_REPORT_ENABLED', True):
            try:
                from utils import get_china_time as _report_now
                _now_cn = _report_now()
                if _now_cn.weekday() == 4:  # 周五
                    _iso = _now_cn.isocalendar()
                    _key = f"pack-report:weekly:{pack_id}:{_iso[0]}-{_iso[1]}"
                    if not self.repository.get_job_by_dedupe_key(_key):
                        _end = _now_cn.strftime('%Y-%m-%d')
                        _start = (_now_cn - timedelta(days=7)).strftime('%Y-%m-%d')
                        self.repository.enqueue_job(
                            "pack_report",
                            _key,
                            {
                                "industry_pack_id": pack_id,
                                "time_start": _start,
                                "time_end": _end,
                                "source": "scheduled",
                            },
                            priority=5,
                            created_by="weekly-report-schedule",
                        )
                        print(f"📝 AI 周报任务已入队：包={pack_id} 时间窗={_start} ~ {_end}（去重键 {_key}）")
            except Exception as exc:
                print(f"⚠️ AI 周报定时入队失败: {exc}")

        if config.INTEL_SOURCE_SYNC_ENABLED:
            day = utc_now().date().isoformat()
            dedupe_key = f"source-sync:daily:{activation_id or pack_id}:{day}"
            if not self.repository.get_job_by_dedupe_key(dedupe_key):
                self.repository.enqueue_job(
                    "source_sync",
                    dedupe_key,
                    {**job_context, "industry_pack_id": pack_id, "manual": False},
                    priority=-10,
                )
        # Reports are versioned files, not daily article feeds.  One daily
        # conditional re-check is sufficient and unchanged files stay put.
        day = utc_now().date().isoformat()
        for declared_pack_id in active["effective_pack_ids"]:
            dedupe_key = f"report-check:{declared_pack_id}:daily:{day}"
            if not self.repository.get_job_by_dedupe_key(dedupe_key):
                self.repository.enqueue_job(
                    "report_check",
                    dedupe_key,
                    {
                        **job_context,
                        "industry_pack_id": declared_pack_id,
                        "declaring_pack_id": declared_pack_id,
                    },
                    priority=-15,
                )
            # 报告 URL 探测：每天发现一次可下载的 .pdf 报告直链并自动下载
            rdedupe = f"report-discover:{declared_pack_id}:daily:{day}"
            if not self.repository.get_job_by_dedupe_key(rdedupe):
                self.repository.enqueue_job(
                    "report_discover",
                    rdedupe,
                    {**job_context, "industry_pack_id": declared_pack_id},
                    priority=-16,
                )
        if config.INTEL_LIGHT_SCANNER_ENABLED:
            now_hk = utc_now().astimezone(timezone(timedelta(hours=8)))
            # SerpAPI is a discovery channel, not a registered website.  Run
            # it once per Hong Kong calendar day after the configured time.
            # The date-based key also makes a worker restart after 08:30 a
            # safe catch-up: it performs the missed scan once, never once per
            # polling loop.  Website scans remain governed only by each
            # source's own daily/weekly frequency below.
            try:
                daily_hour, daily_minute = [int(part) for part in str(config.INTEL_LIGHT_SCAN_DAILY_TIME).strip()[:5].split(":", 1)]
            except (TypeError, ValueError):
                daily_hour, daily_minute = 8, 30
            if (now_hk.hour, now_hk.minute) >= (daily_hour, daily_minute):
                # Google searches only the active industry pack.  Other
                # installed packs are configuration alternatives and must not
                # consume the daily SerpAPI quota in the background.
                dedupe_key = f"serpapi-daily:{pack_id}:{activation_id}:{now_hk.strftime('%Y%m%d')}"
                if not self.repository.get_job_by_dedupe_key(dedupe_key):
                    self.repository.enqueue_job(
                        "light_scan",
                        dedupe_key,
                        {
                            "industry_pack_id": pack_id,
                            **job_context,
                            "scan_sources": False,
                            "include_serpapi": True,
                            "max_sources": 1,
                            "manual": False,
                        },
                        priority=-18,
                    )
            self.source_registry.migrate_legacy_schedule_preferences()
            for source_id in self.source_registry.due_source_ids(pack_id, now_hk):
                source = self.source_registry.get_source(source_id) or {}
                declared_pack_id = (
                    "financial_markets"
                    if "financial_markets" in (source.get("industry_pack_ids") or [])
                    else pack_id
                )
                failures = max(0, int(source.get("consecutive_scan_failures") or 0))
                retry_suffix = (
                    f":retry-{failures}"
                    if failures and source.get("last_scan_status") not in {"completed", "partial"}
                    else ""
                )
                # The local schedule date is stable across polling loops and
                # worker restarts. Physical source identity is shared by
                # composable packs, so never multiply scans by pack id.
                dedupe_key = (
                    f"light-scan:source:{source_id}:"
                    f"{now_hk.strftime('%Y%m%d')}{retry_suffix}"
                )
                if not self.repository.get_job_by_dedupe_key(dedupe_key):
                    self.repository.enqueue_job(
                        "light_scan",
                        dedupe_key,
                        {
                            **job_context,
                            "industry_pack_id": pack_id,
                            "declaring_pack_id": declared_pack_id,
                            "source_ids": [source_id],
                            "include_serpapi": False,
                            "max_sources": 1,
                            "manual": False,
                        },
                        priority=-20,
                    )
        if config.INTEL_CANDIDATE_DISPATCH_ENABLED:
            # 派发频率总控（⑧ 聚合调度，仅 admin）：
            #   持续模式（默认）→ 每分钟一个去重桶，即"随时在派发"
            #   定点模式 → 只在配置的时间点各派发一次（低频）；已过时刻且当天未跑则补跑一次
            # 事件触发不受此开关影响：搜索完成后 / 信源同步后 / 页面手动 仍会即时派发。
            # 包级 crawl_schedule 优先（admin 在「行业包管理 → 来源管理 → 聚合调度」里设），
            # 缺省回退全局配置。未配置的包按"低频默认：定点聚合、不做持续派发"处理。
            _schedule = {}
            try:
                from industry_packs import industry_pack_loader as _schedule_loader
                _schedule = ((_schedule_loader.load(pack_id, enabled_only=False) or {}).get('crawl_schedule') or {})
            except Exception as exc:
                print(f"⚠️ 读取行业包 crawl_schedule 失败（回退全局）: {exc}")
            if 'allow_continuous_dispatch' in _schedule:
                _continuous = bool(_schedule.get('allow_continuous_dispatch'))
            else:
                _continuous = bool(getattr(config, 'INTEL_CANDIDATE_DISPATCH_CONTINUOUS', True))
            _times_raw = _schedule.get('daily_times') or []
            if isinstance(_times_raw, str):
                _times_raw = [t for t in _times_raw.split(',') if t.strip()]
            _fixed_times = [str(t).strip() for t in _times_raw if str(t).strip()] or [
                t.strip() for t in str(getattr(config, 'INTEL_CANDIDATE_DISPATCH_TIMES', '') or '').split(',') if t.strip()
            ]
            # 落盘调试（绕开 stdout 缓冲，便于在生产/本机直接查看实际取到的调度配置）
            try:
                import os as _trace_os
                _trace_path = _trace_os.path.join(
                    _trace_os.path.dirname(_trace_os.path.abspath(__file__)), '_sched_trace.log')
                with open(_trace_path, 'a', encoding='utf-8') as _trace_fp:
                    _trace_fp.write(
                        "%s pack=%s schedule=%s continuous=%s fixed=%s\n"
                        % (utc_now().isoformat(), pack_id, _schedule, _continuous, _fixed_times))
            except Exception:
                pass
            # 定点聚合：按行业包的 daily_times 各入队一次 light_scan（每天每时刻一次，按去重键去重）
            if _schedule.get('enabled', True) and _fixed_times:
                try:
                    from utils import get_china_time as _get_time
                    _now_pack = _get_time()
                    _hm_pack = _now_pack.strftime('%H:%M')
                    _day_pack = _now_pack.date().isoformat()
                    for _slot in _fixed_times:
                        if _hm_pack < _slot:
                            continue
                        _crawl_key = f"light-scan:fixed:{activation_id or pack_id}:{_day_pack}:{_slot}"
                        if self.repository.get_job_by_dedupe_key(_crawl_key):
                            continue
                        self.repository.enqueue_job(
                            "light_scan",
                            _crawl_key,
                            {
                                **job_context,
                                "industry_pack_id": pack_id,
                                "include_serpapi": True,
                                "include_tavily": True,
                                "manual": False,
                                "scheduled_crawl": True,
                            },
                            priority=-20,
                        )
                        print(f"🕐 定点聚合已入队：{_day_pack} {_slot}（{_crawl_key}）")
                except Exception as exc:
                    print(f"⚠️ 定点聚合入队失败: {exc}")
            _pending_keys = []
            if _continuous:
                bucket = int(utc_now().timestamp() // 60)
                dedupe_key = f"candidate-dispatch:{activation_id or pack_id}:{bucket}"
                if not self.repository.get_job_by_dedupe_key(dedupe_key):
                    _pending_keys.append(dedupe_key)
            else:
                try:
                    from utils import get_china_time
                    _now_cn = get_china_time()
                    _hm = _now_cn.strftime('%H:%M')
                    _day = _now_cn.date().isoformat()
                    for _t in _fixed_times:
                        if _hm < _t:
                            continue
                        _key = f"candidate-dispatch:fixed:{activation_id or pack_id}:{_day}:{_t}"
                        if not self.repository.get_job_by_dedupe_key(_key):
                            _pending_keys.append(_key)
                except Exception as exc:
                    print(f"⚠️ 定点派发时间判断失败: {exc}")
            for dedupe_key in _pending_keys:
                self.repository.enqueue_job(
                    "candidate_dispatch",
                    dedupe_key,
                    {**job_context, "industry_pack_id": pack_id, "manual": False},
                    priority=-30,
                )
            # 自动重打分：关键词/配置变化后，discovered 候选会被周期性重打分并提升为
            # queued，派发器随即接手，彻底无需人工脚本。每 10 分钟一个去重桶，纯关键词匹配。
            rbucket = int(utc_now().timestamp() // 600)
            rdedupe = f"candidate-rescore:{activation_id or pack_id}:{rbucket}"
            if not self.repository.get_job_by_dedupe_key(rdedupe):
                self.repository.enqueue_job(
                    "candidate_rescore",
                    rdedupe,
                    {**job_context, "industry_pack_id": pack_id, "manual": False},
                    priority=5,
                )
        if config.INTEL_TOPIC_CLUSTER_ENABLED:
            bucket = int(
                utc_now().timestamp()
                // (config.INTEL_TOPIC_CLUSTER_INTERVAL_MINUTES * 60)
            )
            dedupe_key = f"topic-cluster:{pack_id}:{activation_id}:periodic:{bucket}"
            if not self.repository.get_job_by_dedupe_key(dedupe_key):
                self.repository.enqueue_job(
                    "topic_cluster",
                    dedupe_key,
                    {**job_context, "industry_pack_id": pack_id, "manual": False},
                    priority=-40,
                )
        if config.INTEL_TREND_AGGREGATE_ENABLED:
            bucket = int(
                utc_now().timestamp()
                // (config.INTEL_TREND_AGGREGATE_INTERVAL_MINUTES * 60)
            )
            dedupe_key = f"trend-aggregate:{pack_id}:{activation_id}:periodic:{bucket}"
            if not self.repository.get_job_by_dedupe_key(dedupe_key):
                self.repository.enqueue_job(
                    "trend_aggregate",
                    dedupe_key,
                    {**job_context, "industry_pack_id": pack_id, "manual": False},
                    priority=-45,
                )
        if config.INTEL_TREND_AGGREGATE_ENABLED:
            bucket = int(
                utc_now().timestamp()
                // (config.INTEL_TREND_AGGREGATE_INTERVAL_MINUTES * 60)
            )
            dedupe_key = f"brand-aggregate:{pack_id}:{activation_id}:periodic:{bucket}"
            if not self.repository.get_job_by_dedupe_key(dedupe_key):
                self.repository.enqueue_job(
                    "trend_aggregate",
                    dedupe_key,
                    {**job_context, "industry_pack_id": pack_id, "dimension": "brand", "manual": False},
                    priority=-45,
                )
        if config.INTEL_EMBEDDING_ENABLED:
            bucket = int(
                utc_now().timestamp()
                // (config.INTEL_EMBEDDING_INTERVAL_MINUTES * 60)
            )
            dedupe_key = f"embed-articles:{pack_id}:periodic:{bucket}"
            if not self.repository.get_job_by_dedupe_key(dedupe_key):
                self.repository.enqueue_job(
                    "embed_articles",
                    dedupe_key,
                    {**job_context, "industry_pack_id": pack_id},
                    # 基础设施任务：聊天语义检索依赖向量，优先级高于其他周期任务，
                    # 避免爬取繁忙时被饿死（此前 -50 曾排队一小时未执行）
                    priority=-30,
                )
        if config.INTEL_EVENT_EXTRACT_ENABLED:
            bucket = int(
                utc_now().timestamp()
                // (config.INTEL_EVENT_EXTRACT_INTERVAL_MINUTES * 60)
            )
            dedupe_key = f"event-extract:{pack_id}:{activation_id}:periodic:{bucket}"
            if not self.repository.get_job_by_dedupe_key(dedupe_key):
                self.repository.enqueue_job(
                    "event_extract",
                    dedupe_key,
                    {**job_context, "industry_pack_id": pack_id},
                    priority=-42,
                )
        if config.INTEL_BERTOPIC_ENABLED:
            bucket = int(
                utc_now().timestamp()
                // (config.INTEL_BERTOPIC_INTERVAL_MINUTES * 60)
            )
            dedupe_key = f"bertopic-cluster:{pack_id}:{activation_id}:periodic:{bucket}"
            if not self.repository.get_job_by_dedupe_key(dedupe_key):
                self.repository.enqueue_job(
                    "bertopic_cluster",
                    dedupe_key,
                    {**job_context, "industry_pack_id": pack_id},
                    priority=-46,
                )
        trigger = (
            "startup" if not self._financial_market_scheduler_started else "periodic"
        )
        # 金融市场调度只在"金融类行业包"激活时才运行；非金融包（如工控安全与算力）
        # 即便引入了 financial_markets 作为子包，也**不**触发金融行情调度，避免拖累。
        _active_pack = active.get("primary_pack") or {}
        _active_caps = _active_pack.get("dashboard_capabilities") or {}
        is_financial_active = bool(
            _active_caps.get("show_financial_news")
            or str(active.get("active_industry_pack_id") or "") == "financial_markets"
            or bool(config.FINANCIAL_INTELLIGENCE_ENABLED and _active_caps.get("financial_intelligence"))
        )
        if self.financial_market_scheduler is not None and is_financial_active:
            market_schedule = self.financial_market_scheduler.enqueue_due_jobs(
                trigger=trigger
            )
            schedule_log_state = (
                str(market_schedule.get("status") or "unknown"),
                str(market_schedule.get("reason") or ""),
            )
            if schedule_log_state != self._financial_market_scheduler_log_state:
                if schedule_log_state[0] == "skipped":
                    LOGGER.warning(
                        "financial market scheduler skipped: reason=%s",
                        schedule_log_state[1] or "unknown",
                    )
                else:
                    LOGGER.info(
                        "financial market scheduler active: trigger=%s created=%s existing=%s",
                        trigger,
                        int(market_schedule.get("created") or 0),
                        int(market_schedule.get("existing") or 0),
                    )
                self._financial_market_scheduler_log_state = schedule_log_state
            self._financial_market_scheduler_started = True

    def enqueue_financial_market_event(
        self,
        event_key: str,
        *,
        now=None,
    ) -> Dict:
        """Queue one exchange-aware refresh for an explicit, stable event id."""

        if self.financial_market_scheduler is None:
            return {"skipped": True, "reason": "financial market disabled"}
        return dict(
            self.financial_market_scheduler.enqueue_due_jobs(
                now=now,
                trigger="event",
                event_key=event_key,
            )
        )

    def run_once(
        self,
        *,
        job_types: Optional[Iterable[str]] = None,
        limit: Optional[int] = None,
        schedule_periodic: bool = True,
    ) -> Dict:
        if schedule_periodic:
            self.enqueue_due_periodic_jobs()
        allowed = list(job_types or self.handlers.keys())
        jobs = self.repository.claim_jobs(
            self.worker_id,
            job_types=allowed,
            limit=limit,
            lease_seconds=self.job_lease_seconds,
        )
        stats = {
            "claimed": len(jobs),
            "completed": 0,
            "retry_wait": 0,
            "failed": 0,
            "cancelled": 0,
            "lease_lost": 0,
        }
        runtimes = {}
        # A batch is claimed atomically but processed sequentially.  Start a
        # lease heartbeat for every claimed row immediately so a long research
        # job cannot let the remaining RSS/financial jobs expire while waiting.
        for job in jobs:
            context = self._job_context(job)
            heartbeat_stop = threading.Event()
            heartbeat = threading.Thread(
                target=self._heartbeat_job,
                args=(int(job["id"]), context.cancel_event, heartbeat_stop),
                name=f"intel-job-heartbeat-{job['id']}",
                daemon=True,
            )
            heartbeat.start()
            runtimes[int(job["id"])] = (context, heartbeat_stop, heartbeat)
        for job in jobs:
            handler = self.handlers.get(job["job_type"])
            context, heartbeat_stop, heartbeat = runtimes[int(job["id"])]
            try:
                if not handler:
                    status = self.repository.fail_job(
                        job["id"],
                        "no handler registered",
                        lease_owner=self.worker_id,
                        retryable=False,
                    )
                    stats[status] = stats.get(status, 0) + 1
                    continue
                if job["job_type"] in self._context_handler_types:
                    result = handler(job.get("payload") or {}, context)
                else:
                    result = handler(job.get("payload") or {})
                # 🔥 失败语义修复：handler 显式返回 success=False 时任务必须走
                # fail_job（重试/终态失败），绝不能无条件 complete——否则 VPN 精炼
                # 失败的文章会被误标 completed 而永远不再重试，正文保持原始噪声。
                if isinstance(result, dict) and result.get("success") is False:
                    status = self.repository.fail_job(
                        job["id"],
                        str(result.get("error") or "handler 返回失败")[:2000],
                        lease_owner=self.worker_id,
                        retryable=bool(result.get("retryable", True)),
                    )
                    stats[status] = stats.get(status, 0) + 1
                    continue
                completed = self.repository.complete_job(
                    job["id"], result, lease_owner=self.worker_id
                )
                if completed:
                    stats["completed"] += 1
                else:
                    current = self.repository.get_job(job["id"]) or {}
                    status = str(current.get("status") or "lease_lost")
                    status = "cancelled" if status == "cancelled" else "lease_lost"
                    stats[status] = stats.get(status, 0) + 1
            except FinancialJobCancelled as exc:
                current = self.repository.get_job(job["id"]) or {}
                if current.get("status") == "cancelled":
                    stats["cancelled"] += 1
                elif exc.error_code == "lease_lost":
                    stats["lease_lost"] += 1
                else:
                    status = self.repository.fail_job(
                        job["id"],
                        exc.error_code,
                        lease_owner=self.worker_id,
                        retryable=True,
                    )
                    stats[status] = stats.get(status, 0) + 1
            except Exception as exc:
                error_code = str(getattr(exc, "error_code", "") or "").strip()
                error_text = f"{error_code}: {exc}" if error_code else str(exc)
                status = self.repository.fail_job(
                    job["id"],
                    error_text,
                    lease_owner=self.worker_id,
                    retryable=bool(getattr(exc, "retryable", True)),
                )
                stats[status] = stats.get(status, 0) + 1
            finally:
                heartbeat_stop.set()
                heartbeat.join(timeout=max(0.1, self.heartbeat_seconds * 2))
                self._release_job_context(job["id"])
        return stats

    def run_forever(
        self,
        poll_seconds: Optional[int] = None,
        *,
        job_types: Optional[Iterable[str]] = None,
        schedule_periodic: bool = True,
    ) -> None:
        interval = max(1, int(poll_seconds or config.INTEL_WORKER_POLL_SECONDS))
        while not self.stop_requested:
            stats = self.run_once(
                job_types=job_types,
                schedule_periodic=schedule_periodic,
            )
            if stats["claimed"] == 0:
                time.sleep(interval)

    def request_stop(self, *_args) -> None:
        self.stop_requested = True
        with self._active_job_lock:
            for cancel_event in self._active_cancel_events.values():
                cancel_event.set()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the market intelligence worker")
    parser.add_argument("--once", action="store_true", help="claim one batch and exit")
    parser.add_argument("--job-type", action="append", default=[])
    parser.add_argument(
        "--no-periodic-scheduler",
        action="store_true",
        help="claim only the selected lane; another worker owns periodic enqueue",
    )
    parser.add_argument("--limit", type=int, default=config.INTEL_WORKER_BATCH_SIZE)
    parser.add_argument("--poll-seconds", type=int, default=config.INTEL_WORKER_POLL_SECONDS)
    args = parser.parse_args()

    worker = IntelWorker()
    signal.signal(signal.SIGTERM, worker.request_stop)
    signal.signal(signal.SIGINT, worker.request_stop)
    if args.once:
        print(worker.run_once(job_types=args.job_type or None, limit=args.limit))
        return 0
    worker.run_forever(
        args.poll_seconds,
        job_types=args.job_type or None,
        schedule_periodic=not args.no_periodic_scheduler,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

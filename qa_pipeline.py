#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Composition root for the shared unified-QA execution stages."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from typing import Mapping

from qa_contracts import QA_CONTRACT_VERSION, QaContractError, validate_level1_result, validate_level2_result
from qa_errors import QaAction, QaPublicError, classify_qa_error
from qa_level1 import QaLevel1Generator, empty_level1_result
from qa_orchestrator import QaStageFailure
from qa_planner import QaQueryPlanner
from qa_policy import QaPolicyResolver
from qa_policy_evidence import filter_and_rank_policy_evidence, normalize_policy_claims
from qa_provider_registry import QaProviderRegistry
from qa_question_templates import render_question_plan_status
from qa_ragflow_client import QaRagflowResearchClient
from qa_relevance import filter_relevant_evidence
from qa_reasoning import build_claim_evidence_graph
from qa_research import QaRagflowResearchService, enrich_ragflow_evidence_from_database, insufficient_level2_result
from qa_synthesis import QaFinalSynthesizer, fallback_final_answer
from qa_retrieval import ArticleRetriever, default_web_search_service
from qa_storage import QaStore
from qa_flags import QaFeatureFlags
from qa_resilience import QaCircuitOpen, QaPersistentResilience

_PREWARM_LOCK = threading.Lock()
_PREWARM_LAST: dict[str, float] = {}


def _make_adjustment_parser(provider_registry):
    def parse(payload: Mapping) -> dict:
        try:
            from qa_level1 import OpenAIJsonModelClient, extract_json_object
        except Exception:
            return {}
        request = payload.get("request") if isinstance(payload.get("request"), Mapping) else {}
        profile = provider_registry.resolve(
            "draft",
            str(request.get("model") or request.get("draft_provider_id") or "local"),
            owner_user_id=str(request.get("owner_user_id") or ""),
            industry_pack_id=str(request.get("industry_pack_id") or ""),
        )
        plan = payload.get("current_plan") if isinstance(payload.get("current_plan"), Mapping) else {}
        subquestions = [
            {
                "id": item.get("id"),
                "text": item.get("text"),
                "category": item.get("category"),
            }
            for item in plan.get("subquestions") or []
            if isinstance(item, Mapping)
        ]
        body = {
            "original_question": str(payload.get("original_question") or "")[:1200],
            "subquestions": subquestions,
            "relationship": plan.get("relationship"),
            "categories": plan.get("categories") or [],
            "current_answer_template": plan.get("answer_template") or [],
            "user_adjustment": str(payload.get("user_adjustment") or "")[:800],
            "allowed_operations": ["filter", "augment", "reorder", "format", "exclude", "replace", "confirm"],
            "required_json_shape": {
                "operation": "filter|augment|reorder|format|exclude|replace|confirm",
                "target_subquestions": ["q1"],
                "answer_template": ["先聚焦用户指定的问题", "再按证据说明影响", "最后给出应对动作"],
                "answer_strategy": "一句话说明如何按调整后的思路回答",
                "format": "structured_text|table|brief",
                "exclude_sections": ["可选"],
            },
        }
        system = (
            "你是问答计划调整解析器。只理解用户对既有回答计划的调整，不回答问题。"
            "只输出一个 JSON 对象，不要 Markdown。"
            "如果用户要求只回答某个子问题，operation=filter，并填写已有 target_subquestions。"
            "如果用户增加例子、格式、顺序、排除内容，分别使用 augment/format/reorder/exclude。"
            "不得创造不存在的 q 编号；不确定时使用 augment，并在 answer_strategy 中保守说明。"
        )
        raw = OpenAIJsonModelClient()(
            profile,
            [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(body, ensure_ascii=False)}],
            timeout=25,
        )
        parsed = extract_json_object(raw)
        return parsed if isinstance(parsed, dict) else {}
    return parse


def _default_semantic_search(question: str, *, allowed_ids: set, limit: int):
    from chat_api import _semantic_top_articles

    return _semantic_top_articles(question, k=limit, allowed_ids=allowed_ids)


def _dedupe_evidence(items: list[dict], limit: int) -> list[dict]:
    result, refs, urls, article_ids, fingerprints = [], set(), set(), set(), set()
    for item in items:
        ref = str(item.get("evidence_ref") or "")
        url = str(item.get("source_url") or "")
        article_id = item.get("article_id")
        fingerprint_text = " ".join(str(item.get(key) or "") for key in ("title", "content_excerpt", "excerpt", "content"))
        fingerprint = hashlib.sha256(" ".join(fingerprint_text.casefold().split())[:600].encode("utf-8")).hexdigest()[:24] if fingerprint_text.strip() else ""
        if (
            ref in refs
            or (article_id and article_id in article_ids)
            or (url and url in urls)
            or (fingerprint and fingerprint in fingerprints)
        ):
            continue
        refs.add(ref)
        if url:
            urls.add(url)
        if article_id:
            article_ids.add(article_id)
        if fingerprint:
            fingerprints.add(fingerprint)
        result.append(item)
        if len(result) >= limit:
            break
    return result


_RAG_RELEVANCE_STOP_TERMS = {
    "问题", "回答", "资料", "材料", "证据", "检索", "分析", "影响", "具体", "内容",
    "相关", "行业", "政策", "公告", "法规", "文件", "如何", "什么", "是否", "哪些",
    "怎么", "为什么", "以及", "关于", "进行", "说明", "解释", "风险", "应对",
}
_RAG_NOISE_TERMS = {
    "instagram", "facebook", "linkedin", "峰会", "论坛", "花絮", "精彩瞬间", "心情",
    "点赞", "转发", "评论", "活动回顾", "纳斯达克大屏", "获奖", "招聘",
}
_GENERIC_BACKGROUND_TERMS = {
    "行业", "产业", "市场", "峰会", "论坛", "活动", "协会", "发布会", "合作伙伴",
    "圆满举行", "精彩回顾", "发展机遇", "专业人士", "战略合作", "高峰论坛",
}
_POLICY_DIRECT_TERMS = {
    "离岸信托", "境外信托", "个人所得税", "个税", "21号", "公告", "税务",
    "征管", "财政部", "税务总局", "信托个人所得税", "财产装入",
}


def _filter_rag_evidence_relevance(
    question: str,
    evidence: list[Mapping],
    *,
    plan: Mapping | None = None,
    limit: int | None = None,
) -> tuple[list[dict], dict]:
    # 通用相关性闸门交给 qa_relevance：标题命中问题实词，或正文前若干字符内多次命中；
    # 付费墙/免责声明与社媒活动类片段直接丢弃。服务器那版是"命中>=3 词，或命中>=2 词且
    # 相似度>=0.2"，实测仍会把无关片段按 0.55 的相似度带进证据包。
    accepted, audit = filter_relevant_evidence(
        question,
        [dict(item) for item in evidence or []],
        plan=plan,
    )
    if not (audit.get("terms") or []):
        # 问题里抽不出实词时不做过滤，与原实现 not_applicable 行为一致
        kept = [dict(item) for item in evidence[: limit or len(evidence)]]
        return kept, {"generic_relevance_filter": "not_applicable", "terms": []}
    accepted.sort(
        key=lambda item: (
            len(item.get("relevance_hits") or []),
            float(item.get("score") or 0),
            int(item.get("authority_level") or 1),
        ),
        reverse=True,
    )
    cap = limit if limit is not None else len(accepted)
    return accepted[:cap], {
        "generic_relevance_filter": "applied",
        "terms": list(audit.get("terms") or [])[:20],
        "term_source": str(audit.get("term_source") or ""),
        "excluded_irrelevant": list(audit.get("excluded") or [])[:50],
        "kept": len(accepted),
        "reason": str(audit.get("reason") or ""),
    }


def _gate_rag_evidence(
    question: str,
    evidence: list[Mapping],
    *,
    plan: Mapping | None = None,
    limit: int | None = None,
) -> tuple[list[dict], dict]:
    policy_filtered, policy_audit = filter_and_rank_policy_evidence(
        question,
        evidence,
        plan=plan,
        limit=limit,
    )
    if policy_audit.get("policy_filter") == "applied":
        return policy_filtered, {
            "policy": policy_audit,
            "generic_relevance": {"generic_relevance_filter": "skipped_policy_filter_applied"},
        }
    generic_filtered, generic_audit = _filter_rag_evidence_relevance(
        question,
        policy_filtered,
        plan=plan,
        limit=limit,
    )
    return generic_filtered, {"policy": policy_audit, "generic_relevance": generic_audit}


def _material_cleaning_config(plan: Mapping | None) -> Mapping:
    plan = plan if isinstance(plan, Mapping) else {}
    question_plan = plan.get("question_plan") if isinstance(plan.get("question_plan"), Mapping) else {}
    config = question_plan.get("material_cleaning") if isinstance(question_plan.get("material_cleaning"), Mapping) else {}
    return config if config.get("enabled") else {}


def _plan_text_terms(plan: Mapping | None) -> set[str]:
    plan = plan if isinstance(plan, Mapping) else {}
    values: list[str] = []
    question_plan = plan.get("question_plan") if isinstance(plan.get("question_plan"), Mapping) else {}
    for item in question_plan.get("subquestions") or []:
        if isinstance(item, Mapping):
            values.append(str(item.get("text") or ""))
    for item in question_plan.get("categories") or []:
        if isinstance(item, Mapping):
            values.append(str(item.get("label") or ""))
    anchors = plan.get("policy_anchors") if isinstance(plan.get("policy_anchors"), Mapping) else {}
    for key in ("strong_terms", "doc_nos", "titles", "issuers"):
        values.extend(str(item or "") for item in anchors.get(key) or [])
    blob = " ".join(values).casefold()
    terms = {term.casefold() for term in _POLICY_DIRECT_TERMS if term.casefold() in blob}
    terms.update(
        term for term in re.findall(r"[\u4e00-\u9fff]{2,12}|[a-z0-9][a-z0-9_\-]{2,}", blob, flags=re.I)
        if term not in _RAG_RELEVANCE_STOP_TERMS and not re.fullmatch(r"\d+", term)
    )
    return {term for term in terms if term}


def _clean_material_evidence(
    question: str,
    evidence: list[Mapping],
    *,
    plan: Mapping | None = None,
    limit: int | None = None,
) -> tuple[list[dict], dict]:
    cleaning = _material_cleaning_config(plan)
    if not cleaning:
        cap = limit if limit is not None else len(evidence)
        return [dict(item) for item in evidence[:cap]], {"material_cleaning": "not_applicable"}

    direct_terms = {term.casefold() for term in _POLICY_DIRECT_TERMS if term.casefold() in str(question or "").casefold()}
    direct_terms.update(_plan_text_terms(plan))
    kept: list[dict] = []
    excluded: list[dict] = []
    for raw in evidence:
        item = dict(raw)
        blob = " ".join(
            str(item.get(key) or "")
            for key in ("title", "source_url", "content_excerpt", "excerpt", "content")
        ).casefold()
        noise_hits = [term for term in _RAG_NOISE_TERMS if term in blob]
        generic_hits = [term for term in _GENERIC_BACKGROUND_TERMS if term.casefold() in blob]
        direct_hits = [term for term in direct_terms if term and term in blob]
        reason = ""
        if cleaning.get("exclude_social_media") and noise_hits:
            reason = "material_cleaning_social_media_or_event_noise"
        elif cleaning.get("exclude_generic_background") and generic_hits and not direct_hits:
            reason = "material_cleaning_generic_pack_background"
        if reason:
            excluded.append({
                "evidence_ref": item.get("evidence_ref"),
                "title": item.get("title"),
                "reason": reason,
                "noise_hits": noise_hits[:8],
                "generic_hits": generic_hits[:8],
            })
            continue
        kept.append(item)

    cap = limit if limit is not None else len(kept)
    if cleaning.get("dedupe_repeated_fragments"):
        kept = _dedupe_evidence(kept, cap)
    else:
        kept = kept[:cap]
    return kept, {
        "material_cleaning": "applied",
        "instruction": str(cleaning.get("instruction") or "")[:240],
        "kept": len(kept),
        "excluded_material_noise": excluded[:50],
    }


def _evidence_anchored_level1_fallback(message: str, evidence: list[dict], queries: list[str]) -> dict:
    """Keep useful first-level findings without inventing model claims."""
    result = empty_level1_result(message, evidence)
    claims = []
    for index, item in enumerate(evidence[:4], 1):
        ref = str(item.get("evidence_ref") or "")
        title = " ".join(str(item.get("title") or "").split())[:140]
        metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        excerpt = " ".join(str(item.get("content_excerpt") or item.get("excerpt") or "").split())
        if title and title in excerpt:
            excerpt = excerpt.split(title, 1)[1].strip(" ：:-") or excerpt
        excerpt = excerpt[:180].strip()
        if not ref or not title:
            continue
        authority = int(item.get("authority_level") or 1)
        doc_type = str(item.get("doc_type") or metadata.get("doc_type") or "")
        source_type = str(item.get("source_type") or "")
        is_official = doc_type in {"official_policy", "official_interpretation"} or source_type in {"official_policy", "official_original"} or authority >= 100
        text = f"已检索到{'官方原文' if is_official else '参考资料'}《{title}》"
        if excerpt:
            text += f"，其内容载明：{excerpt}"
        claims.append({
            "claim_id": f"l1-evidence-{index}", "text": text[:500],
            "claim_type": "current_fact" if is_official else "interpretation",
            "confidence": 0.7 if is_official else 0.5,
            "valid_from": item.get("published_at"), "valid_to": None,
            "scope": [], "evidence_refs": [ref],
            "needs_verification": True, "verification_status": "unverified",
        })
    result["claims"] = claims
    result["citations"] = [claim["evidence_refs"][0] for claim in claims]
    result["gaps"] = ["资料检索已完成，事实主张仍需结合更多可引用依据继续核验。"]
    result["followup_queries"] = list(queries)[:5]
    return validate_level1_result(result)


def _rag_retrieval_fallback(level1: Mapping, reason: str) -> dict:
    evidence = list(level1.get("evidence") or [])
    return {
        "queries": list(level1.get("followup_queries") or [])[:5],
        "query_trace": [],
        "evidence": evidence,
        "excluded": {"rag_enhancement": {"fallback": True, "reason": str(reason or "")[:300]}},
        "stats": {"hops": 0, "queries_run": 0, "adopted": len(evidence), "pg_fallback": True},
        "kb_status": {"ready": False, "fallback": "pg_policy_layered"},
        "request_ids": [],
        "rag_mode": "RAG检索",
        "enhanced": False,
    }


def _pg_layered_research_result(level1: Mapping, retrieval: Mapping, reason: str = "") -> dict:
    evidence = list(retrieval.get("evidence") or []) or list(level1.get("evidence") or [])
    evidence_refs = {str(item.get("evidence_ref") or "") for item in evidence if item.get("evidence_ref")}
    confirmed = []
    corrected = []
    for index, raw in enumerate(level1.get("claims") or [], 1):
        if not isinstance(raw, Mapping):
            continue
        claim = dict(raw)
        refs = [str(ref) for ref in claim.get("evidence_refs") or [] if str(ref) in evidence_refs]
        if not refs:
            claim["evidence_refs"] = []
            try:
                confidence = float(claim.get("confidence") or 0.35)
            except (TypeError, ValueError):
                confidence = 0.35
            claim["confidence"] = min(confidence, 0.35)
            claim["needs_verification"] = True
            claim["verification_status"] = "insufficient_evidence"
            corrected.append(claim)
            continue
        claim["claim_id"] = str(claim.get("claim_id") or f"pg-layered-{index}")
        claim["evidence_refs"] = refs
        try:
            confidence = float(claim.get("confidence") or 0.65)
        except (TypeError, ValueError):
            confidence = 0.65
        claim["confidence"] = max(0.5, min(0.82, confidence))
        claim["needs_verification"] = True
        claim["verification_status"] = "qualified"
        confirmed.append(claim)
    report = {
        "contract_version": QA_CONTRACT_VERSION,
        "confirmed_claims": confirmed[:40],
        "corrected_claims": corrected[:40],
        "new_findings": [],
        "timeline": [],
        "horizontal_comparisons": [],
        "conflicts": [],
        "multi_hop_findings": [{
            "method": "pg_policy_layered",
            "source_order": ["official_policy", "official_interpretation", "professional_commentary"],
            "note": "基于已入库政策元数据和文章证据完成分层检索。",
        }],
        "evidence_gaps": [] if evidence else ["知识库未检索到可用依据"],
        "evidence": evidence,
        "citations": list(dict.fromkeys(ref for claim in confirmed for ref in claim.get("evidence_refs") or [])),
        "research_audit": {
            "mode": "pg_policy_layered",
            "rag_mode": "RAG检索",
            "enhanced": False,
            "fallback_reason": str(reason or "")[:300],
            "evidence_count": len(evidence),
        },
    }
    return validate_level2_result(report)


def build_qa_stage_handlers(
    *,
    database=None,
    planner=None,
    article_retriever=None,
    web_search=None,
    provider_registry=None,
    level1_generator=None,
    policy_resolver=None,
    ragflow_client_factory=None,
    final_synthesizer=None,
    store=None,
    feature_flags=None,
    resilience=None,
) -> dict:
    if database is None:
        from sqlite_database import sqlite_db

        database = sqlite_db
    article_retriever = article_retriever or ArticleRetriever(database, semantic_search=_default_semantic_search)
    web_search = web_search or default_web_search_service()
    provider_registry = provider_registry or QaProviderRegistry()
    planner = planner or QaQueryPlanner(adjustment_parser=_make_adjustment_parser(provider_registry))
    level1_generator = level1_generator or QaLevel1Generator()
    policy_resolver = policy_resolver or QaPolicyResolver()
    store = store or QaStore(database)
    final_synthesizer = final_synthesizer or QaFinalSynthesizer()
    feature_flags = feature_flags or QaFeatureFlags(database)
    resilience = resilience or QaPersistentResilience(database)

    if ragflow_client_factory is None:
        def ragflow_client_factory(policy):
            import config

            return QaRagflowResearchClient(
                base_url=config.RAGFLOW_BASE_URL,
                api_key=config.RAGFLOW_API_KEY,
                app_id=policy.ragflow_app_id,
                kb_id=policy.ragflow_kb_id,
                timeout_seconds=policy.research_timeout_seconds,
                retries=1,
                proxies=config.get_ragflow_proxies() if hasattr(config, "get_ragflow_proxies") else None,
            )

    def _prewarm_synthesis_provider(context, *, reason: str = "plan") -> None:
        run = context.get("run") or {}
        provider_id = str(run.get("synthesis_provider_id") or "local")
        if provider_id != "local":
            return
        try:
            profile = provider_registry.resolve(
                "synthesis", provider_id,
                owner_user_id=str(run.get("owner_user_id") or ""),
                industry_pack_id=str(run.get("industry_pack_id") or ""),
            )
        except Exception:
            return
        base_url = str(getattr(profile, "base_url", "") or "").rstrip("/")
        model_id = str(getattr(profile, "model_id", "") or "")
        if not base_url or not model_id:
            return
        key = f"{provider_id}:{base_url}:{model_id}"
        now = time.time()
        with _PREWARM_LOCK:
            if now - _PREWARM_LAST.get(key, 0) < 180:
                return
            _PREWARM_LAST[key] = now

        def _worker() -> None:
            try:
                import requests

                headers = {"Content-Type": "application/json"}
                api_key = str(getattr(profile, "api_key", "") or "")
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                requests.get(
                    f"{base_url}/models",
                    headers=headers,
                    timeout=2,
                )
            except Exception:
                pass

        threading.Thread(target=_worker, name=f"qa-prewarm-{reason}", daemon=True).start()

    def plan(context):
        result = planner.plan(context["request"])
        question_plan = result.get("question_plan") if isinstance(result.get("question_plan"), Mapping) else {}
        emit_stage_event = context.get("_emit_stage_event")
        if callable(emit_stage_event) and question_plan:
            message = render_question_plan_status(question_plan)
            emit_stage_event("stage_progress", {"message": message, "question_plan": question_plan})
            emit_stage_event("stage_progress", {"message": "正在检查本地 LLM 可用性；若响应过慢，将自动改用证据约束结果继续。", "stage": "prewarm"})
        _prewarm_synthesis_provider(context, reason="plan")
        return result

    def _planned_question(context) -> str:
        plan_output = context["outputs"].get("plan") or {}
        return str(plan_output.get("standalone_question") or context["request"].get("question") or "")

    def _question_for_synthesis(context) -> str:
        request_question = str(context["request"].get("question") or "")
        plan_output = context["outputs"].get("plan") or {}
        question_plan = plan_output.get("question_plan") if isinstance(plan_output.get("question_plan"), Mapping) else {}
        standalone = str(plan_output.get("standalone_question") or request_question)
        if not question_plan:
            return standalone
        summary = {
            "原始问题": request_question,
            "检索用完整问题": standalone,
            "问题数量": question_plan.get("question_count"),
            "问题关系": question_plan.get("relationship"),
            "是否追问": question_plan.get("is_followup"),
            "回答策略": question_plan.get("answer_strategy"),
            "用户调整意见": question_plan.get("user_adjustment"),
            "用户确认": question_plan.get("user_confirmation"),
            "主题聚类": question_plan.get("categories"),
            "材料清洗": question_plan.get("material_cleaning"),
            "检索策略": question_plan.get("retrieval_strategy"),
            "回答大纲": question_plan.get("answer_outline"),
            "动态回答模板": question_plan.get("answer_template"),
            "子问题": question_plan.get("subquestions"),
        }
        return standalone + "\n\n问题拆解与回答计划：" + json.dumps(summary, ensure_ascii=False)

    def _local_version(pack_id: str) -> str:
        try:
            database._ensure_connection()
            with database.lock:
                row = database.connection.execute(
                    """SELECT COUNT(*),COALESCE(MAX(COALESCE(a.first_crawled,a.created_at,'')),'')
                    FROM articles a JOIN article_intel_classifications c ON c.article_id=a.id
                    WHERE c.industry_pack_id=? AND a.status='active'""",
                    (str(pack_id),),
                ).fetchone()
            return hashlib.sha256(f"{row[0]}:{row[1]}".encode("utf-8")).hexdigest()[:24]
        except Exception:
            return "unknown"

    def _scope_hash(context) -> str:
        page = (context["request"].get("page_context") or {})
        if not page:
            return "public-pack"
        return hashlib.sha256(str(context["run"].get("owner_user_id") or "").encode("utf-8")).hexdigest()[:24]

    def level1_retrieval(context):
        request_payload = context["request"]
        retrieval_plan = context["outputs"]["plan"]
        emit_stage_event = context.get("_emit_stage_event")
        if not retrieval_plan.get("needs_local_articles"):
            return {
                "queries": [], "evidence": [], "excluded": {},
                "stats": {"eligible": 0, "adopted": 0}, "search_status": "not_required",
            }
        if callable(emit_stage_event):
            emit_stage_event("stage_progress", {
                "message": "正在按问题类型检索政策库、文章库和可引用来源。",
                "queries": list(retrieval_plan.get("queries") or [])[:4],
                "question_plan": retrieval_plan.get("question_plan") or {},
            })
        pack_id = request_payload["industry_pack_id"]
        kb_version = _local_version(pack_id)
        scope_hash = _scope_hash(context)
        cache_payload = {
            "queries": retrieval_plan.get("queries") or [],
            "mode": request_payload.get("mode"),
            "web": bool(retrieval_plan.get("needs_web")),
            "page_context": request_payload.get("page_context") or {},
        }
        cache_key = resilience.cache_key(
            "level1_retrieval", cache_payload, pack_id=pack_id,
            kb_version=kb_version, policy_version="qa-retrieval-v3-policy-exact",
        )
        cached = resilience.cache_get(
            cache_key, namespace="level1_retrieval", kb_version=kb_version,
            scope_hash=scope_hash,
        )
        if cached is not None:
            cached["cache"] = {"hit": True, "kb_version": kb_version}
            if callable(emit_stage_event):
                emit_stage_event("stage_progress", {
                    "message": f"命中检索缓存，已取得 {len(cached.get('evidence') or [])} 条候选证据。",
                    "evidence": list(cached.get("evidence") or [])[:6],
                    "stats": dict(cached.get("stats") or {}),
                })
            return cached
        local = article_retriever.retrieve(
            {**dict(retrieval_plan), "question": _planned_question(context)},
            industry_pack_id=pack_id,
            page_context=request_payload.get("page_context") or {},
            limit=24 if retrieval_plan.get("high_risk_policy") else (16 if request_payload.get("mode") == "deep" else 12),
        )
        external = web_search.search(
            retrieval_plan.get("queries") or [],
            enabled=bool(retrieval_plan.get("needs_web")),
            limit=8,
        )
        cap = 24 if request_payload.get("mode") == "deep" else 18
        raw_evidence = _dedupe_evidence(
            list(local.get("evidence") or []) + list(external.get("evidence") or []), cap
        )
        evidence, policy_audit = filter_and_rank_policy_evidence(
            _planned_question(context),
            raw_evidence,
            plan=retrieval_plan,
            limit=cap,
        )
        evidence, material_audit = _clean_material_evidence(
            _planned_question(context),
            evidence,
            plan=retrieval_plan,
            limit=cap,
        )
        stats = dict(local.get("stats") or {})
        stats.update({"web_adopted": len(external.get("evidence") or []), "adopted": len(evidence)})
        if policy_audit.get("policy_filter") == "applied":
            stats["policy_source_roles"] = policy_audit.get("source_roles") or {}
            stats["policy_noise_excluded"] = len(policy_audit.get("excluded_policy_noise") or [])
        if material_audit.get("material_cleaning") == "applied":
            stats["material_noise_excluded"] = len(material_audit.get("excluded_material_noise") or [])
        if callable(emit_stage_event):
            emit_stage_event("stage_progress", {
                "message": f"已筛出 {len(evidence)} 条候选证据，正在按权威性和相关性排序。",
                "evidence": evidence[:6],
                "stats": stats,
            })
        # 时间区间必须明确回写给用户：「最近」到底被解释成哪一段，
        # 以及区间内找不到证据时是否已经扩窗 —— 不能让用户猜。
        _time_window = dict(local.get("time_window") or {})
        if callable(emit_stage_event) and _time_window.get("has_time"):
            _tw_message = "时间范围：%s" % (_time_window.get("label") or "")
            if _time_window.get("expanded"):
                _tw_message += "；" + (_time_window.get("note")
                                       or "该范围内没有找到直接证据，已扩大到全部历史资料")
            else:
                _tw_message += "；在此范围内找到 %s 条证据。" % _time_window.get("in_window_adopted", 0)
            emit_stage_event("stage_progress", {
                "message": _tw_message,
                "time_window": _time_window,
                "stats": stats,
            })
        result = {
            "queries": list(retrieval_plan.get("queries") or []),
            "evidence": evidence,
            "excluded": {**dict(local.get("excluded") or {}), "policy": policy_audit, "material_cleaning": material_audit},
            "stats": stats,
            "search_status": external.get("status"),
            "search_providers": external.get("providers") or [],
            "search_errors": external.get("errors") or [],
            "time_window": _time_window,
            "cache": {"hit": False, "kb_version": kb_version},
        }
        resilience.cache_put(
            cache_key, result, namespace="level1_retrieval", pack_id=pack_id,
            kb_version=kb_version, scope_hash=scope_hash, ttl_seconds=180,
        )
        return result

    def level1_draft(context):
        run = context["run"]
        request_payload = context["request"]
        retrieval_plan = context["outputs"]["plan"]
        retrieval = context["outputs"]["level1_retrieval"]
        emit_stage_event = context.get("_emit_stage_event")
        evidence = list(retrieval.get("evidence") or [])
        if callable(emit_stage_event) and evidence:
            emit_stage_event("stage_progress", {
                "message": f"正在整理 {len(evidence)} 条候选证据，抽取来源等级、标题和可引用片段。",
                "evidence": evidence[:6],
            })
        if retrieval_plan.get("intent") == "smalltalk":
            result = empty_level1_result("您好，我可以检索行业文章和知识库，并给出带引用的研究结论。", evidence)
            store.persist_level1_result(run["id"], result)
            return result
        if not evidence:
            result = empty_level1_result("当前检索未找到足以支持事实结论的资料，将继续尝试扩展核验。")
            result["followup_queries"] = list(retrieval_plan.get("queries") or [])[:5]
            result = validate_level1_result(result)
            store.persist_level1_result(run["id"], result)
            return result
        profile = None

        def degrade_to_evidence_draft(public: QaPublicError, *, dependency_id: str = "") -> dict:
            item = {"stage": "level1_draft", **public.to_event_payload(trace_id=run["id"])}
            if public.code not in {"LEVEL1_OUTPUT_INVALID"}:
                store.mark_degraded(run["id"], item)
            if dependency_id:
                resilience.circuit_failure(dependency_id)
            return _evidence_anchored_level1_fallback(
                public.message, evidence, list(retrieval_plan.get("queries") or []),
            )

        try:
            profile = provider_registry.resolve(
                "draft",
                str(run.get("draft_provider_id") or "local"),
                owner_user_id=str(run.get("owner_user_id") or ""),
                industry_pack_id=str(run.get("industry_pack_id") or ""),
            )
            dependency = f"provider:{profile.provider_id}"
            resilience.circuit_before(dependency)
            result = level1_generator.generate(
                question=_question_for_synthesis(context),
                plan=retrieval_plan,
                evidence=evidence,
                profile=profile,
            )
            resilience.circuit_success(dependency)
        except QaContractError as exc:
            # The model has already had one bounded repair attempt.  A bad
            # JSON envelope must not discard the server-owned retrieval
            # evidence or prevent the enhancement layer from performing the authoritative
            # second-level verification.  Preserve an explicit degradation
            # record and continue with a schema-valid evidence-only draft.
            provider_id = str(getattr(profile, "provider_id", None) or run.get("draft_provider_id") or "local")
            public = QaPublicError(
                "LEVEL1_OUTPUT_INVALID",
                "资料检索已完成，系统已按证据锚定方式整理初步结论并继续核验。",
                True,
                provider_id,
                "level1_draft",
                (
                    QaAction("重试证据整理", action="retry_stage"),
                    QaAction("切换模型", action="open_model_picker"),
                ),
            )
            result = degrade_to_evidence_draft(
                public,
                dependency_id=f"provider:{profile.provider_id}" if profile is not None else "",
            )
        except QaStageFailure as exc:
            public = exc.public_error
            public = QaPublicError(
                public.code,
                f"{public.message} 已保留本地检索证据并继续后续核验。",
                public.retryable,
                public.provider_id,
                public.stage,
                tuple(public.actions),
            )
            result = degrade_to_evidence_draft(
                public,
                dependency_id=f"provider:{profile.provider_id}" if profile is not None else "",
            )
        except QaCircuitOpen as exc:
            public = QaPublicError(
                "PROVIDER_CIRCUIT_OPEN",
                "本地模型当前响应过慢，系统已先用检索证据整理初步结论并继续后续核验。",
                True,
                str(getattr(profile, "provider_id", "model")), "level1_draft",
                (QaAction("稍后重试当前阶段", action="retry_stage"), QaAction("切换模型", action="open_model_picker")),
            )
            result = degrade_to_evidence_draft(
                public,
                dependency_id=f"provider:{profile.provider_id}" if profile is not None else "",
            )
        except Exception as exc:
            if profile is not None:
                resilience.circuit_failure(f"provider:{profile.provider_id}")
            use_proxy = bool(getattr(profile, "use_proxy", False))
            public = classify_qa_error(
                exc,
                provider_id=str(run.get("draft_provider_id") or "local"),
                stage="level1_draft",
                use_proxy=use_proxy,
                proxy_configured=use_proxy,
            )
            public = QaPublicError(
                public.code,
                f"{public.message} 已保留本地检索证据并继续后续核验。",
                public.retryable,
                public.provider_id,
                public.stage,
                tuple(public.actions),
            )
            result = degrade_to_evidence_draft(
                public,
                dependency_id=f"provider:{profile.provider_id}" if profile is not None else "",
            )
        store.persist_level1_result(run["id"], result)
        return result

    def _research_service(context):
        run = context["run"]
        policy = policy_resolver.require_research_ready(str(run.get("industry_pack_id") or ""))
        client = ragflow_client_factory(policy)
        return policy, client, QaRagflowResearchService(client)

    def _kb_not_configured(context) -> bool:
        """本包有没有可用知识库：没配就根本不要碰 RAGFlow（连熔断状态都不动）。"""
        try:
            policy = policy_resolver.resolve(str(context["run"].get("industry_pack_id") or ""))
        except Exception:
            return True
        return not (str(policy.ragflow_kb_id or "").strip() and str(policy.ragflow_app_id or "").strip())

    def level2_retrieval(context):
        run = context["run"]
        plan_output = context["outputs"]["plan"]
        level1 = context["outputs"]["level1_draft"]
        emit_stage_event = context.get("_emit_stage_event")
        if not plan_output.get("needs_ragflow"):
            return {
                "queries": [], "query_trace": [], "evidence": [], "excluded": {},
                "stats": {"hops": 0, "queries_run": 0, "adopted": 0},
                "kb_status": {"ready": True, "skipped": True}, "request_ids": [],
            }
        flags = feature_flags.snapshot()
        if not flags.get("level2_enabled"):
            # 开关关掉 = 只走一级（平台文章库）检索，连 RAGFlow 都不调用
            return _rag_retrieval_fallback(level1, "enhancement_disabled")
        if _kb_not_configured(context):
            # 没配知识库同样只走一级；不算故障，也不动 ragflow 熔断计数
            return _rag_retrieval_fallback(level1, "kb_not_configured")
        try:
            resilience.circuit_before("ragflow")
            policy, client, service = _research_service(context)
            if callable(emit_stage_event):
                emit_stage_event("stage_progress", {
                    "message": "正在做RAG增强检索：用官方原文优先规则扩展和重排证据片段。",
                    "claims": list(level1.get("claims") or [])[:6],
                })
            health = client.health_check()
            if not health.get("ready"):
                resilience.circuit_failure("ragflow")
                return _rag_retrieval_fallback(level1, "enhancement_unavailable")
            mode = str(run.get("mode") or "standard")
            kb_version = str(health.get("kb_version") or policy.ragflow_kb_id)
            cache_payload = {
                "question": _planned_question(context),
                "plan": plan_output,
                "claims": level1.get("claims") or [],
                "mode": mode,
            }
            cache_key = resilience.cache_key(
                "level2_retrieval", cache_payload,
                pack_id=str(run.get("industry_pack_id") or ""),
                kb_version=kb_version, policy_version="qa-research-v1",
            )
            cached = resilience.cache_get(
                cache_key, namespace="level2_retrieval", kb_version=kb_version,
                scope_hash="public-pack",
            )
            if cached is not None:
                cached["health"] = health
                cached["cache"] = {"hit": True, "kb_version": kb_version}
                gated_evidence, gate_audit = _gate_rag_evidence(
                    _planned_question(context),
                    list(cached.get("evidence") or []),
                    plan=plan_output,
                    limit=policy.max_evidence,
                )
                gated_evidence, material_audit = _clean_material_evidence(
                    _planned_question(context),
                    gated_evidence,
                    plan=plan_output,
                    limit=policy.max_evidence,
                )
                cached["evidence"] = gated_evidence
                cached["excluded"] = {
                    **dict(cached.get("excluded") or {}),
                    "policy": gate_audit.get("policy") or {},
                    "generic_relevance": gate_audit.get("generic_relevance") or {},
                    "material_cleaning": material_audit,
                    "retrieval_gate": gate_audit,
                }
                cached_stats = {**dict(cached.get("stats") or {}), "adopted": len(gated_evidence)}
                if material_audit.get("material_cleaning") == "applied":
                    cached_stats["material_noise_excluded"] = len(material_audit.get("excluded_material_noise") or [])
                cached["stats"] = cached_stats
                if not gated_evidence:
                    resilience.circuit_success("ragflow")
                    return _rag_retrieval_fallback(level1, "enhancement_no_relevant_evidence")
                cached["rag_mode"] = "RAG增强检索"
                cached["enhanced"] = True
                if callable(emit_stage_event):
                    emit_stage_event("stage_progress", {
                        "message": f"命中RAG增强检索缓存，已通过相关性过滤保留 {len(gated_evidence)} 条增强证据。",
                        "evidence": gated_evidence[:6],
                    })
                resilience.circuit_success("ragflow")
                return cached
            result = service.retrieve(
                question=_planned_question(context),
                level1=level1,
                plan=plan_output,
                mode=mode,
                max_queries=policy.max_queries_per_hop,
                max_evidence=policy.max_evidence,
                max_hops=policy.deep_max_hops if mode == "deep" else policy.standard_max_hops,
            )
            result["evidence"] = enrich_ragflow_evidence_from_database(
                list(result.get("evidence") or []),
                database,
                kb_id=str(policy.ragflow_kb_id or ""),
            )
            filtered_evidence, gate_audit = _gate_rag_evidence(
                _planned_question(context),
                list(result.get("evidence") or []),
                plan=plan_output,
                limit=policy.max_evidence,
            )
            filtered_evidence, material_audit = _clean_material_evidence(
                _planned_question(context),
                filtered_evidence,
                plan=plan_output,
                limit=policy.max_evidence,
            )
            result["evidence"] = filtered_evidence
            result["excluded"] = {
                **dict(result.get("excluded") or {}),
                "policy": gate_audit.get("policy") or {},
                "generic_relevance": gate_audit.get("generic_relevance") or {},
                "material_cleaning": material_audit,
                "retrieval_gate": gate_audit,
            }
            result_stats = {**dict(result.get("stats") or {}), "adopted": len(filtered_evidence)}
            if material_audit.get("material_cleaning") == "applied":
                result_stats["material_noise_excluded"] = len(material_audit.get("excluded_material_noise") or [])
            result["stats"] = result_stats
            result["health"] = health
            result["cache"] = {"hit": False, "kb_version": kb_version}
            result["rag_mode"] = "RAG增强检索" if filtered_evidence else "RAG检索"
            result["enhanced"] = bool(filtered_evidence)
            if callable(emit_stage_event):
                emit_stage_event("stage_progress", {
                    "message": f"RAG增强检索已取得 {len(filtered_evidence)} 条证据，正在进入交叉核验。",
                    "evidence": filtered_evidence[:6],
                    "stats": dict(result.get("stats") or {}),
                })
            kb_status = result.get("kb_status") or {}
            if not result.get("evidence") and (
                int(kb_status.get("total") or 0) == 0 or int(kb_status.get("parsing") or 0) > 0
            ):
                return _rag_retrieval_fallback(level1, "enhancement_kb_empty_or_parsing")
            if not result.get("evidence"):
                return _rag_retrieval_fallback(level1, "enhancement_no_relevant_evidence")
            resilience.cache_put(
                cache_key, result, namespace="level2_retrieval",
                pack_id=str(run.get("industry_pack_id") or ""), kb_version=kb_version,
                scope_hash="public-pack", ttl_seconds=180,
            )
            resilience.circuit_success("ragflow")
            return result
        except QaStageFailure:
            return _rag_retrieval_fallback(level1, "enhancement_stage_unavailable")
        except QaCircuitOpen as exc:
            return _rag_retrieval_fallback(level1, "enhancement_circuit_open")
        except Exception as exc:
            resilience.circuit_failure("ragflow")
            return _rag_retrieval_fallback(level1, "enhancement_error")

    def level2_research(context):
        run = context["run"]
        level1 = context["outputs"]["level1_draft"]
        retrieval = context["outputs"]["level2_retrieval"]
        emit_stage_event = context.get("_emit_stage_event")
        if not context["outputs"]["plan"].get("needs_ragflow"):
            result = insufficient_level2_result(level1, [], "当前问题无需行业深度检索")
            store.persist_level2_result(run["id"], result)
            return result
        if not retrieval.get("enhanced"):
            if callable(emit_stage_event):
                emit_stage_event("stage_progress", {
                    "message": "RAG增强检索不可用，正在改用PG分层证据继续多跳分析。",
                    "evidence": list(retrieval.get("evidence") or [])[:6],
                })
            result = _pg_layered_research_result(level1, retrieval, str((retrieval.get("excluded") or {}).get("rag_enhancement") or ""))
            store.persist_level2_result(run["id"], result)
            return result
        try:
            resilience.circuit_before("ragflow")
            _policy, _client, service = _research_service(context)
            if callable(emit_stage_event):
                emit_stage_event("stage_progress", {
                    "message": "正在交叉核验官方原文、官方解读和专业材料，生成多跳结论。",
                    "evidence": list(retrieval.get("evidence") or [])[:6],
                })
            result = service.research(
                question=_question_for_synthesis(context),
                level1=level1,
                retrieval=retrieval,
            )
            resilience.circuit_success("ragflow")
        except QaStageFailure:
            raise
        except QaCircuitOpen as exc:
            raise QaStageFailure(
                QaPublicError(
                    "RAGFLOW_CIRCUIT_OPEN", "RAG增强检索服务暂时繁忙，已保留现有证据。", True, "rag_enhancement", "level2_research",
                    (QaAction("稍后重试交叉核验", action="retry_stage"),),
                ), degradable=True,
            ) from exc
        except Exception as exc:
            resilience.circuit_failure("ragflow")
            raise QaStageFailure(
                classify_qa_error(exc, provider_id="rag_enhancement", stage="level2_research"),
                degradable=True,
            ) from exc
        store.persist_level2_result(run["id"], result)
        return result

    def conflict_review(context):
        question = _question_for_synthesis(context)
        emit_stage_event = context.get("_emit_stage_event")
        plan_output = context["outputs"].get("plan") or {}
        level1 = context["outputs"]["level1_draft"]
        raw_level2 = context["outputs"].get("level2_research") or {}
        level2 = raw_level2 if isinstance(raw_level2, Mapping) and "confirmed_claims" in raw_level2 else {}
        level1, level2, normalizer_audit = normalize_policy_claims(question, level1, level2, plan=plan_output)
        graph = build_claim_evidence_graph(level1, level2)
        graph["normalization_audit"] = normalizer_audit
        store.persist_reasoning_graph(context["run"]["id"], graph)
        if callable(emit_stage_event):
            emit_stage_event("stage_progress", {
                "message": f"证据图已建立：{len(graph.get('claims') or [])} 条结论、{len(graph.get('evidence') or [])} 条证据，正在过滤真实冲突和缺口。",
                "evidence": list(graph.get("evidence") or [])[:6],
            })
        return graph

    def synthesis(context):
        run = context["run"]
        level1 = context["outputs"]["level1_draft"]
        raw_level2 = context["outputs"].get("level2_research") or {}
        level2 = raw_level2 if isinstance(raw_level2, Mapping) and "confirmed_claims" in raw_level2 else {}
        graph = context["outputs"].get("conflict_review")
        if not isinstance(graph, Mapping):
            graph = build_claim_evidence_graph(level1, level2)
            store.persist_reasoning_graph(run["id"], graph)
        current_run = store.get_run(run["id"]) or run
        degradation = list(current_run.get("degradation") or [])
        retrieval_mode = str((context["outputs"].get("level2_retrieval") or {}).get("rag_mode") or "")
        if not retrieval_mode:
            retrieval_mode = "RAG增强检索" if (context["outputs"].get("level2_retrieval") or {}).get("enhanced") else "RAG检索"
        models = {
            "draft": str(run.get("draft_provider_id") or "local"),
            "research": retrieval_mode,
            "synthesis": str(run.get("synthesis_provider_id") or "local"),
        }
        if not feature_flags.snapshot().get("synthesis_enabled"):
            item = {
                "stage": "synthesis", "code": "SYNTHESIS_OPERATIONS_DISABLED",
                "message": "最终综合模型已由运维临时关闭，现返回已核验证据摘要。",
            }
            store.mark_degraded(run["id"], item)
            return fallback_final_answer(
                graph=graph, level1=level1, level2=level2,
                degradation=degradation + [item], models=models,
                reason=item["message"], question=_question_for_synthesis(context),
            )
        try:
            profile = provider_registry.resolve(
                "synthesis", str(run.get("synthesis_provider_id") or "local"),
                owner_user_id=str(run.get("owner_user_id") or ""),
                industry_pack_id=str(run.get("industry_pack_id") or ""),
            )
            dependency = f"provider:{profile.provider_id}"
            resilience.circuit_before(dependency)
            emit_stage_event = context.get("_emit_stage_event")
            streamed_chars = 0
            if callable(emit_stage_event):
                emit_stage_event("stage_progress", {
                    "message": f"综合模型已连接，正在流式生成回答（{profile.provider_id} / {profile.model_id}）"
                })

            def _token_callback(delta: str) -> None:
                nonlocal streamed_chars
                text = str(delta or "")
                if not text or not callable(emit_stage_event):
                    return
                emit_stage_event("answer_delta", {"delta": text, "offset": streamed_chars})
                streamed_chars += len(text)
                context["_answer_delta_streamed"] = True

            result = final_synthesizer.generate(
                question=_question_for_synthesis(context),
                graph=graph, level1=level1, level2=level2,
                degradation=degradation, profile=profile, models=models,
                token_callback=_token_callback if callable(emit_stage_event) else None,
            )
            resilience.circuit_success(dependency)
            return result
        except Exception as exc:
            if "profile" in locals():
                resilience.circuit_failure(f"provider:{profile.provider_id}")
            provider_id = str(run.get("synthesis_provider_id") or "local")
            if isinstance(exc, QaContractError):
                public = QaPublicError(
                    "SYNTHESIS_STRUCTURED_FALLBACK",
                    "最终综合已按证据图完成结构化整理。",
                    True,
                    provider_id,
                    "synthesis",
                    (QaAction("重试最终综合", action="retry_stage"), QaAction("切换模型", action="open_model_picker")),
                )
            else:
                public = classify_qa_error(exc, provider_id=provider_id, stage="synthesis")
                if public.code == "INTERNAL_ERROR":
                    public = QaPublicError(
                        "SYNTHESIS_FALLBACK_USED",
                        "最终综合模型未稳定返回，已改用证据约束重组。",
                        True,
                        provider_id,
                        "synthesis",
                        (QaAction("重试最终综合", action="retry_stage"), QaAction("切换模型", action="open_model_picker")),
                    )
            item = {"stage": "synthesis", **public.to_event_payload(trace_id=run["id"])}
            store.mark_degraded(run["id"], item)
            return fallback_final_answer(
                graph=graph, level1=level1, level2=level2,
                degradation=degradation + [item], models=models, reason=public.message,
                question=_question_for_synthesis(context),
            )

    def citation_validation(context):
        result = dict(context["outputs"]["synthesis"])
        citations = list(result.get("citations") or [])
        citations.extend(
            str(ref)
            for claim in result.get("claims") or []
            for ref in claim.get("evidence_refs") or []
        )
        citations.extend(
            str(ref)
            for conflict in result.get("conflicts") or []
            for ref in conflict.get("evidence_refs") or []
        )
        result["citations"] = list(dict.fromkeys(citations))
        from qa_contracts import validate_final_answer

        return validate_final_answer(result)

    return {
        "plan": plan,
        "level1_retrieval": level1_retrieval,
        "level1_draft": level1_draft,
        "level2_retrieval": level2_retrieval,
        "level2_research": level2_research,
        "conflict_review": conflict_review,
        "synthesis": synthesis,
        "citation_validation": citation_validation,
    }


__all__ = ["build_qa_stage_handlers"]

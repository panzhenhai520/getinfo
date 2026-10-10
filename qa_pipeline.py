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
from qa_evidence import (
    EVIDENCE_LAYER_VERSION, annotate_evidence, dedupe_by_fingerprint,
    dedupe_evidence_items, evidence_layer_enabled, evidence_object, filter_seen,
    load_seen, record_seen,
)
from qa_graph_contracts import (
    QA_ROUTE_GRAPH, QA_ROUTE_GRAPH_ATTRIBUTE, QA_ROUTE_KEYWORD, QA_ROUTE_PAGE_CONTEXT,
    QA_ROUTE_POLICY_EXACT, QA_ROUTE_SEMANTIC, QA_ROUTE_WEB,
)
from qa_level1 import QaLevel1Generator, empty_level1_result
from qa_orchestrator import QaStageFailure
from qa_planner import QaQueryPlanner
from qa_policy import QaPolicyResolver
from qa_policy_evidence import filter_and_rank_policy_evidence, normalize_policy_claims
from qa_provider_registry import QaProviderRegistry
from qa_question_templates import render_question_plan_status
from qa_ragflow_client import QaRagflowResearchClient
from qa_relevance import filter_relevant_evidence, question_terms
from qa_reasoning import build_claim_evidence_graph
from qa_research import QaRagflowResearchService, enrich_ragflow_evidence_from_database, insufficient_level2_result
from qa_synthesis import QaFinalSynthesizer, fallback_final_answer
from qa_retrieval import ArticleRetriever, default_web_search_service
from qa_storage import QaStore
from qa_flags import QaFeatureFlags
from qa_resilience import QaCircuitOpen, QaPersistentResilience
from qa_evidence_graph import (
    EVIDENCE_GRAPH_VERSION as EVIDENCE_GRAPH_LAYER_VERSION,
    evidence_graph_enabled, layer_from_graph,
)
from qa_gap_analyzer import (
    GAP_ANALYZER_VERSION, GapLoopState, gap_analyzer_enabled, gap_summary,
    plan_next_hops, review_graph as review_gap_graph,
)
from qa_verifier import (
    VERIFIER_VERSION, verification_cache, verification_of, verifier_enabled,
    verify_claim_graph, verify_evidence_batch,
)
import config

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
                # 阶段 5：调整必须能改检索与输出形式，否则"逐段解释原文"这类要求落不了地
                "retrieval_queries": ["要新增的专项检索式，最多 4 条；没有就留空数组"],
                "time_window": {"label": "近 3 个月|2026 年 1 月", "days": 90},
                "must_fetch_fulltext": "true|false，用户要看原文/全文/逐段解释时为 true",
                "output_form": "paragraph_by_paragraph|structured_text|table|timeline|brief",
            },
        }
        system = (
            "你是问答计划调整解析器。只理解用户对既有回答计划的调整，不回答问题。"
            "只输出一个 JSON 对象，不要 Markdown。"
            "如果用户要求只回答某个子问题，operation=filter，并填写已有 target_subquestions。"
            "如果用户增加例子、格式、顺序、排除内容，分别使用 augment/format/reorder/exclude。"
            "如果用户要看原文/全文/逐段解释，把 output_form 设为 paragraph_by_paragraph、"
            "must_fetch_fulltext 设为 true，并在 retrieval_queries 里给出能命中官方原文的专项检索式"
            "（例如「政策全称 发文机关 发文字号 原文」）。"
            "如果用户限定了时间范围，填写 time_window（相对说法给 days，绝对说法给 label）。"
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
    """既有去重口径：evidence_ref / article_id / source_url / 内容指纹，四者任一撞上即重复。

    阶段 02 起实现集中在 `qa_evidence.dedupe_evidence_items`（单一事实源，含指纹算法），
    这里只做委托——行为与改造前逐字一致，调用点一个都没动。
    """
    return dedupe_evidence_items(items, limit)


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


def _env_int(name: str, default: int, low: int, high: int) -> int:
    import os

    try:
        value = int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return int(default)
    return max(int(low), min(int(high), value))


def _evidence_limit(retrieval_plan: Mapping, mode) -> int:
    """阶段 6-3：证据条数**按问题类型收敛**（原先一律 12/16/24）。

    口径：
      · 高风险政策、deep 模式：保持原值（完整性优先，不收敛）；
      · 事实核验类（fact_check / evidence_gap）：收到 3~5 条——这类问题只需"有/没有那个事实"，
        条数堆多了只会拖长草稿与合成；默认 5，`QA_EVIDENCE_LIMIT_FACT_CHECK` 可调；
      · 时序/条件类：8 条（够判先后与条件，但不必铺满）；
      · 其它：原值 12（standard）。
    """
    base = 24 if retrieval_plan.get("high_risk_policy") else (
        16 if str(mode or "standard").casefold() == "deep" else 12)
    if retrieval_plan.get("high_risk_policy") or str(mode or "").casefold() == "deep":
        return base
    category = str((retrieval_plan.get("category") or {}).get("key") or "")
    if category in ("fact_check", "evidence_gap"):
        return min(base, _env_int("QA_EVIDENCE_LIMIT_FACT_CHECK", 5, 3, 12))
    if category in ("temporal_relation", "conditional_constraint"):
        return min(base, _env_int("QA_EVIDENCE_LIMIT_RELATION", 8, 4, 16))
    return base


def _evidence_cap(retrieval_plan: Mapping, mode, limit: int) -> int:
    """证据池上限：与 limit 联动，避免"只要 5 条却收进 18 条"。

    高风险政策走 `max(base, limit)`：它的 limit 本来就是 24（完整性优先），
    池子不该反而缩到 18 把刚取到的证据丢掉（原实现 limit=24 / cap=18 是自相矛盾的）。
    """
    base = 24 if str(mode or "standard").casefold() == "deep" else 18
    if retrieval_plan.get("high_risk_policy"):
        return max(base, int(limit))
    return max(limit + 3, min(base, limit + 3))


def _session_scope(run_meta: Mapping) -> tuple:
    """会话约束的作用域键：(用户, 会话, 行业包)。会话为空时不固化/不读取。"""
    return (str(run_meta.get("owner_user_id") or ""),
            str(run_meta.get("session_id") or ""),
            str(run_meta.get("industry_pack_id") or ""))


def _load_session_constraints(store, run_meta: Mapping) -> dict:
    """读会话约束（阶段 10-3）；任何异常都返回空，绝不影响规划。"""
    owner, session_id, pack_id = _session_scope(run_meta)
    if not session_id:
        return {}
    try:
        return dict(store.session_constraints(
            owner_user_id=owner, session_id=session_id, industry_pack_id=pack_id) or {})
    except Exception:
        return {}


def _evidence_scope(run_meta: Mapping) -> dict:
    """阶段 02（P02-03）证据 seen 集合的作用域：用户 + 会话 + 行业包。

    与 `_session_scope` 同一口径，但**不设"会话为空就不记录"的门槛**：会话为空时记录在
    空会话作用域里，照样互相隔离，不会串到别人的会话上。
    """
    owner, session_id, pack_id = _session_scope(run_meta)
    return {"owner_user_id": owner, "session_id": session_id, "industry_pack_id": pack_id}


def _scope_available(scope: Mapping) -> bool:
    """作用域三元组是否可用：三个键必须至少有一个非空（全空 = 拿不到作用域）。

    全空时**只标注、不登记**：不登记最多是去重记忆少一条，登记错了就是跨用户/跨会话串味。
    """
    return bool(str(scope.get("owner_user_id") or "")
                or str(scope.get("session_id") or "")
                or str(scope.get("industry_pack_id") or ""))


def _evidence_layer_receipt(audit: Mapping) -> dict:
    """证据层回执（单一事实源）：键集与接线前**逐字相同**。

    只有真的跳过了 seen 登记（`skipped_scope`）时才多一条计数——否则不动键集，
    免得破坏既有回执结构（前端与验收脚本都按这套键读）。

    阶段 03 的核验回执**不走这里**：Phase 02 的回归用例把本回执的键集钉死了
    （`tests/test_qa_phase02_wiring.py::test_receipt_keys_are_unchanged_and_hop_audit_is_merged`），
    所以核验结果放在**兄弟键** `stats["verification"]`（见 `_verification_receipt`）——
    既不破坏既有契约，也不用去改跨阶段的冻结键集。
    """
    receipt = {key: audit.get(key) for key in (
        "evidence_layer", "annotated", "seen_dropped", "dedupe_dropped", "recorded", "reason")}
    if int(audit.get("skipped_scope") or 0):
        receipt["skipped_scope"] = int(audit.get("skipped_scope") or 0)
    return receipt


def _verification_receipt(audit: Mapping) -> dict:
    """核验回执（阶段 03）：从证据层审计里摘出核验统计；没跑过核验则返回空字典。

    空字典 = 调用方不要往 `stats` 里塞这个键 → 关掉 `QA_VERIFIER_ENABLED=0` 时
    阶段返回结构与 Phase 02 逐字相同（一键回滚）。
    """
    value = audit.get("verification") if isinstance(audit, Mapping) else None
    if not isinstance(value, Mapping) or not value:
        return {}
    return {key: value.get(key) for key in
            ("verifier", "config_hash", "gate", "checked", "verdicts", "dropped",
             "reasons", "reordered", "degraded", "reason")}


def _merge_verification_audits(target: Mapping | None, extra: Mapping | None) -> dict:
    """合并多跳各跳的核验回执：计数累加、verdicts/reasons 分布合并（键集不变）。"""
    merged = dict(target) if isinstance(target, Mapping) else {}
    extra = extra if isinstance(extra, Mapping) else {}
    if not extra:
        return merged
    for key in ("checked", "dropped", "reordered"):
        merged[key] = int(merged.get(key) or 0) + int(extra.get(key) or 0)
    for field in ("verdicts", "reasons"):
        table = dict(merged.get(field) or {})
        for name, count in (extra.get(field) or {}).items():
            table[str(name)] = int(table.get(str(name)) or 0) + int(count or 0)
        merged[field] = table
    for key in ("verifier", "config_hash", "gate"):
        if extra.get(key):
            merged[key] = extra[key]
    if extra.get("cache"):
        merged["cache"] = extra["cache"]
    degraded = list(merged.get("degraded") or [])
    for item in extra.get("degraded") or []:
        if item not in degraded:
            degraded.append(item)
    if degraded:
        merged["degraded"] = degraded[:5]
    if extra.get("reason"):
        merged["reason"] = str(extra["reason"])[:200]
    return merged


def _merge_evidence_audits(target: dict, extra: Mapping) -> dict:
    """把多跳各跳的证据层回执并入同一条 `stats["evidence_layer"]`（缺口 1，不新增返回键）。

    计数类累加，`evidence_layer` 版本取先有的非空值，`reason` 去重后拼接（截断 200 字）。
    """
    if not isinstance(extra, Mapping) or not extra:
        return target
    for key in ("annotated", "seen_dropped", "dedupe_dropped", "recorded", "skipped_scope"):
        target[key] = int(target.get(key) or 0) + int(extra.get(key) or 0)
    if isinstance(extra.get("verification"), Mapping):
        target["verification"] = _merge_verification_audits(target.get("verification"),
                                                            extra.get("verification"))
    if not str(target.get("evidence_layer") or "").strip():
        target["evidence_layer"] = str(extra.get("evidence_layer") or "")
    reasons = [str(item).strip() for item in (target.get("reason"), extra.get("reason")) if str(item or "").strip()]
    target["reason"] = "；".join(dict.fromkeys(reasons))[:200]
    return target


def _wire_hop_evidence_layer(evidence: list, *, question: str, plan: Mapping | None,
                            run_meta: Mapping | None, store, round_index: int,
                            corpus_version: str, route: str, audit: dict | None) -> list[dict]:
    """多跳**每一跳**的证据层接线（Phase 02 缺口 1）：与 level1 同一套函数、同一套作用域。

    每跳走"标注 → span 级去重 → 按 seen 跨轮去重 → 登记（中性 seen）"，
    回执累加进调用方传进来的 `audit`（最终并入 `stats["evidence_layer"]`）。
    拿不到 store 或作用域时**只记一条 `skipped_scope` 计数，绝不报错**，证据原样返回。
    """
    if store is None or not isinstance(run_meta, Mapping) or not _scope_available(_evidence_scope(run_meta)):
        if audit is not None:
            audit["skipped_scope"] = int(audit.get("skipped_scope") or 0) + 1
        return list(evidence)
    try:
        kept, hop_audit = _apply_evidence_layer(
            evidence, rejected=[], question=question, plan=plan, run_meta=run_meta,
            store=store, round_index=round_index, corpus_version=corpus_version,
            stage="multi_hop", route=route, witness_only=True, empty_fallback=False,
        )
    except Exception as exc:  # noqa: BLE001 —— 证据层绝不拖累多跳
        if audit is not None:
            audit["reason"] = "%s: %s" % (type(exc).__name__, str(exc)[:120])
        return list(evidence)
    if audit is not None:
        _merge_evidence_audits(audit, hop_audit)
    return kept


def _research_route(item: Mapping) -> str:
    """level2（RAGFlow 研究）证据的检索通道：dataset 检索就是向量语义检索，归 `semantic`。

    取值域仍来自 `qa_graph_contracts.QA_ROUTE_*`（单一事实源）；非 RAGFlow 证据
    （分跳里混进来的文章/图谱证据）按既有口径 `_route_of_evidence` 判。
    """
    method = str(item.get("retrieval_method") or "").strip().casefold()
    if method.startswith("ragflow") or str(item.get("source_type") or "") == "ragflow_chunk":
        return QA_ROUTE_SEMANTIC
    return _route_of_evidence(item)


def _has_multi_hop_layer(item: Mapping) -> bool:
    """这条证据是否已被**多跳某一跳**标注过（`provenance.stage == multi_hop`）。

    只有逐跳标注过的才保留：整批标注（level1 / level2）会把 stage 覆盖成自己，
    那样每跳的 provenance 就没了（P02-02 的意义所在）。其它情况一律重标，
    免得把上一轮缓存里的旧 run_id 一直带着（缓存命中的证据必须重算本轮 provenance）。
    """
    layer = evidence_object(item)
    provenance = layer.get("provenance")
    return isinstance(provenance, Mapping) and str(provenance.get("stage") or "") == "multi_hop"


def _apply_evidence_layer(evidence: list, *, rejected, question: str, plan: Mapping | None,
                          run_meta: Mapping, store, round_index: int = 0,
                          corpus_version: str = "", stage: str = "level1_retrieval",
                          route: str = "", witness_only: bool = False,
                          empty_fallback: bool = True) -> tuple[list[dict], dict]:
    """阶段 02（P02-01…P02-04）主接线：证据层标注 + seen 登记 + 跨轮/跨 run 去重。

    只加不改：
      · 标注结果全部落在 `metadata.evidence_layer`（`EVIDENCE_SCHEMA` 放行 metadata），
        证据条目的顶层键集一个字不变，冻结契约指纹不受影响；
      · 去重默认只丢"上一轮被闸门拒掉的来源"（QA_EVIDENCE_SEEN_DEDUPE=rejected），
        不会把上一轮用过的有用来源从本轮证据包里拿掉；
      · 顺序是"先查后记"：先读本轮**之前**的 seen 身份，再做过滤，最后才登记本轮结果，
        否则本轮的登记会把自己过滤掉（mode=all 时尤其致命）；
      · 任何异常都原样返回证据 + 审计里写明原因——证据层绝不能把问答打断。

    阶段 02 缺口 1 增补（多跳每一跳 / level2 复用同一条路，行为仍是"只加不改"）：
      · `stage` / `route` 进 provenance，让每一跳、每一次 RAGFlow 检索都能被回溯；
      · 多跳逐跳标注过的证据（`provenance.stage == multi_hop`）不再被整批标注覆盖，
        否则每跳的 provenance 就没了（`annotated` 仍按"带标注的条数"计）；
      · `witness_only=True`：本轮证据只登记成中性 `seen`。多跳每一跳的候选还没过闸门，
        不能冒充 confirmed——否则之后被闸门拒掉的来源会因为 confirmed 永远躲过跨轮去重；
      · `empty_fallback=False`：去重把整批证据清空时**返回空**而不是退回原证据
        （逐跳用；level1 保持既有"绝不返回空证据包"的兜底口径）；
      · 拿不到作用域三元组（owner/session/pack 全空）时**跳过登记但不报错**，
        审计里记 `skipped_scope` 计数（跨用户/跨会话串味比少记一条危险得多）。

    返回 (证据, 审计)。
    """
    audit = {"evidence_layer": "skipped", "reason": "", "annotated": 0, "seen_dropped": 0,
             "dedupe_dropped": 0, "recorded": 0, "skipped_scope": 0,
             "scope": _evidence_scope(run_meta)}
    if not evidence_layer_enabled():
        audit["reason"] = "QA_EVIDENCE_LAYER_ENABLED=0"
        return list(evidence), audit
    try:
        run_id = str(run_meta.get("id") or "")
        scope = audit["scope"]
        rejected_items = [item for item in (rejected or []) if isinstance(item, Mapping)]
        reviewed = [
            dict(item) if _has_multi_hop_layer(item) else annotate_evidence(
                item, terms=question_terms(question, plan), run_id=run_id, stage=stage,
                route=route, round_index=round_index, corpus_version=corpus_version)
            for item in (evidence or []) if isinstance(item, Mapping)
        ]
        reviewed, fingerprint_audit = dedupe_by_fingerprint(reviewed)
        seen_before = load_seen(store, scope=scope, items=[*reviewed, *rejected_items])
        kept, seen_audit = filter_seen(reviewed, seen_before)
        # 因"重复"被跳过的：登记成中性 seen（它们既没进证据包，也不是这一轮被闸门拒的）
        dropped_keys = {str(entry.get("source_fingerprint") or "")
                        for entry in seen_audit.get("dropped") or []}
        skipped = [item for item in reviewed
                   if str(evidence_object(item).get("source_fingerprint") or "") in dropped_keys]

        # ── 阶段 03（P03-01…P03-04）：证据核验（纯规则/统计，绝不调模型）──
        # 顺序刻意放在"跨轮去重之后"：已经被见过的垃圾不必再花时间核验。
        # 核验结论落在 metadata.evidence_layer.verification（证据顶层键集不变），
        # 闸门默认只丢"有反证"的证据，且**绝不**把证据包清空（清空就退回原证据）。
        def _verify(items):
            return verify_evidence_batch(
                items, claim_text=question, terms=question_terms(question, plan),
                required_entities=_plan_entities(plan), store=store,
                pack_id=str(scope.get("industry_pack_id") or ""),
            )

        verified, verify_audit = _verify(kept)
        verified_refs = {str(item.get("evidence_ref") or "") for item in verified}
        verifier_rejected = [item for item in kept
                             if str(item.get("evidence_ref") or "") not in verified_refs]
        kept = verified
        if not _scope_available(scope):
            # 缺作用域三元组：只标注、不登记（去重记忆宁可少一条，也不能串到别人身上）
            audit["skipped_scope"] = 1
        else:
            if witness_only:
                # 逐跳候选：通过的登记中性 `seen`；被跨轮去重丢掉的按"仍然拒绝"再次登记
                # （刷新 last_seen_at，但不把已有的 rejected 身份降级成 seen——降级等于
                # 下一轮不再丢它，跨轮去重就白做了）；被核验闸门拒掉的走 extra_rejected
                recorded = record_seen(
                    store, scope=scope, rejected=[*rejected_items, *skipped],
                    witnessed=kept, run_id=run_id, round_index=round_index,
                    extra_rejected=verifier_rejected,
                )
            else:
                # 整批路径：`skipped` 是"本轮被跨轮去重丢掉"的来源。它们**上一轮就已经是 rejected**
                # （否则不会被丢），所以这里必须按 rejected 再登记一次，只刷新 last_seen_at。
                # 早先写成 witnessed=skipped 会把它降级成中性 seen → 下一轮不再丢它、靠闸门再拒一次，
                # 跨轮去重就白做了（与逐跳路径的口径也不一致）。
                recorded = record_seen(
                    store, scope=scope, accepted=kept,
                    rejected=[*rejected_items, *skipped],
                    run_id=run_id, round_index=round_index,
                    extra_rejected=verifier_rejected,
                )
            audit["recorded"] = int(recorded.get("recorded") or 0)
            audit["record_error"] = str(recorded.get("error") or "")
        audit.update({
            "evidence_layer": EVIDENCE_LAYER_VERSION,
            "annotated": len(reviewed),
            "seen_dropped": int(seen_audit.get("dropped_count") or 0),
            "dedupe_dropped": int(fingerprint_audit.get("dropped_count") or 0),
            "seen_mode": str(seen_audit.get("mode") or ""),
        })
        if verifier_enabled() and verify_audit.get("verifier"):
            audit["verification"] = {
                key: verify_audit.get(key) for key in
                ("verifier", "config_hash", "gate", "checked", "verdicts", "dropped",
                 "reasons", "reordered", "degraded", "reason")
            }
        if not kept:
            if not empty_fallback:
                # 多跳每一跳：这一跳没新证据就是没新证据，不许把"已知垃圾"当证据塞回去
                # （回执里如实记 empty，缺口交给 logic_validation 说明）
                audit["reason"] = "seen_dedupe_emptied_evidence"
                return [], audit
            # 去重把证据清空了：宁可退回原证据（只加了标注），也不给用户一个空证据包。
            # 退回的这批**照样要过核验**——不然"退回"就成了绕过核验的后门。
            audit["reason"] = "seen_dedupe_emptied_evidence_fallback"
            fallback, fallback_audit = _verify(reviewed)
            if verifier_enabled() and fallback_audit.get("verifier"):
                merged = _merge_verification_audits(audit.get("verification"), fallback_audit)
                audit["verification"] = {
                    key: merged.get(key) for key in
                    ("verifier", "config_hash", "gate", "checked", "verdicts", "dropped",
                     "reasons", "reordered", "degraded", "reason")
                }
            return fallback, audit
        return kept, audit
    except Exception as exc:  # noqa: BLE001 —— 证据层绝不打断问答
        audit["reason"] = "%s: %s" % (type(exc).__name__, str(exc)[:120])
        return list(evidence), audit


def _plan_entities(plan: Mapping | None) -> list:
    """计划里点名的实体（阶段 03 实体核验的输入：结论/问题点名要求的东西必须在证据里出现）。

    不做新 NER：只认计划里已有的 `entities`/`topics`，且长度像实词（2…40 字）。
    """
    if not isinstance(plan, Mapping):
        return []
    values = []
    for key in ("entities", "topics"):
        for value in plan.get(key) or []:
            clean = str(value or "").strip()
            if 2 <= len(clean) <= 40 and clean not in values:
                values.append(clean)
    return values[:10]


def _verify_claim_graph_in_place(graph: dict, *, store, question: str, run_meta: Mapping) -> dict:
    """阶段 03（MASTER_RULES 第 11 条）：claim 级核验并写回 `verification_status`。

    **只加不改**：返回的是核验摘要（stats 里含 unsupported_claim_rate），
    图上的 claims/edges/conflicts 结构一字不动；任何异常都吞掉并记账——
    核验绝不能因为一个坏 claim 把整条 run 打断。
    """
    try:
        return verify_claim_graph(
            graph, store=store, pack_id=str(_session_scope(run_meta)[2] or ""))
    except Exception as exc:  # noqa: BLE001
        return {"verifier_version": VERIFIER_VERSION, "enabled": verifier_enabled(),
                "error": "%s: %s" % (type(exc).__name__, str(exc)[:120])}


def _build_evidence_graph_layer(graph: dict, *, plan: Mapping | None, run_meta: Mapping) -> dict:
    """阶段 06（P06-01…P06-04）：把结论图升级成显式证据图 + 矛盾裁决。

    **只加不改**：结果挂在 `graph["evidence_graph"]` 这个兄弟键上（Phase 02 的
    `stats["evidence_layer"]`、Phase 03 的 `stats["verification"]` 同样做法），
    既有的 claims/edges/conflicts 键集不动；唯一例外是**矛盾裁决**会按冻结
    `CONFLICT_SCHEMA` 允许的三个字段（resolution/rationale/rule_version）回写
    `graph["conflicts"]`——这正是 Phase 06 的交付内容，且不越过冻结契约。
    关掉 `QA_EVIDENCE_GRAPH`（默认关）时本函数一次都不被调用，行为逐字回到接线前。
    任何异常都吞掉并记账：建图绝不能因为一条坏数据把整条 run 打断。
    """
    try:
        return layer_from_graph(graph, plan=plan, run_id=str(run_meta.get("id") or ""))
    except Exception as exc:  # noqa: BLE001
        return {"graph_version": EVIDENCE_GRAPH_LAYER_VERSION,
                "error": "%s: %s" % (type(exc).__name__, str(exc)[:160])}


def _attach_gap_review(graph: Mapping, layer: dict, *, plan: Mapping | None,
                       previous_stop_reason: str = "") -> dict:
    """阶段 07（P07-06）：在证据图上做一次缺口复核，结果挂 `layer["gap_review"]`。

    这是 `UNRESOLVABLE_CONTRADICTION` 的**权威出口**：Phase 06 的规则裁决已经比过 §15 的
    八项比较，落 `unresolved` 就说明"再检索也裁不动"（要裁得换更强的裁决器，本轮硬约束禁止）。
    只加不改：evidence_graph 契约 `additionalProperties=True`，复核是新键；
    复核失败只记账（`error`），绝不影响建图与答案。
    """
    try:
        review = review_gap_graph(layer, graph=graph, plan=plan,
                                  previous_stop_reason=previous_stop_reason)
    except Exception as exc:      # noqa: BLE001 —— 复核绝不能拖累主流程
        review = {"analyzer_version": GAP_ANALYZER_VERSION, "stage": "evidence_graph_review",
                  "stop_reason": "", "error": "%s: %s" % (type(exc).__name__, str(exc)[:160])}
    layer["gap_review"] = review
    return review


def _gap_stop_reason_from_level1(context: Mapping) -> str:
    """多跳缺口循环给出的停止原因（`stats["gap_loop"]["stop_reason"]`；没有就是空串）。"""
    outputs = context.get("outputs") or {}
    level1 = outputs.get("level1_retrieval") if isinstance(outputs, Mapping) else {}
    stats = level1.get("stats") if isinstance(level1, Mapping) else {}
    receipt = stats.get("gap_loop") if isinstance(stats, Mapping) else {}
    return str((receipt or {}).get("stop_reason") or "") if isinstance(receipt, Mapping) else ""


def _persist_session_constraints(store, run_meta: Mapping, plan_result: Mapping) -> int:
    """把本轮已确认的约束固化（时间窗/输出形式/实体/全文要求）。

    只落"用户确实表达过"的东西：来自用户调整（adjustment_receipt）或计划里明确的值。
    失败一律吞掉（留痕/固化绝不能拖累问答）。
    """
    owner, session_id, pack_id = _session_scope(run_meta)
    if not session_id:
        return 0
    question_plan = plan_result.get("question_plan") if isinstance(
        plan_result.get("question_plan"), Mapping) else {}
    try:
        constraints = {
            "time_window": plan_result.get("time_window_adjustment")
            or question_plan.get("time_window") or {},
            "output_form": plan_result.get("output_form") or question_plan.get("output_form") or "",
            "must_fetch_fulltext": bool(plan_result.get("must_fetch_fulltext")),
            "entities": (plan_result.get("entities") or [])[:10],
            "topics": (plan_result.get("topics") or [])[:10],
        }
        return store.save_session_constraints(
            owner_user_id=owner, session_id=session_id, industry_pack_id=pack_id,
            constraints=constraints, run_id=str(run_meta.get("id") or ""),
        )
    except Exception:
        return 0


# ── 阶段 01（F-7）：SearchTrace 的 route / results / accepted / rejected ──────
# route 的取值域是 `qa_graph_contracts.QA_RETRIEVAL_ROUTES`（单一事实源，别在这儿另造字符串）。
_ROUTE_BY_METHOD = {
    "keyword": QA_ROUTE_KEYWORD,
    # hybrid = 关键词召回 + 向量重排命中：按"更强的通道"归到 semantic，
    # 否则 semantic 通道在统计里永远为 0（候选本来就都来自同一条关键词 SQL）。
    "hybrid": QA_ROUTE_SEMANTIC,
    "page_context": QA_ROUTE_PAGE_CONTEXT,
    "policy_metadata_exact": QA_ROUTE_POLICY_EXACT,
    "graph_event": QA_ROUTE_GRAPH,
    "graph_cooccurrence": QA_ROUTE_GRAPH,
    "graph_attribute": QA_ROUTE_GRAPH_ATTRIBUTE,
}


def _as_int(value, default: int = 0) -> int:
    """宽松取整：None/坏值一律退回 default（留痕字段宁可写默认值也不许抛）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _route_of_evidence(item: Mapping) -> str:
    """单条证据走的是哪条检索通道。"""
    if str(item.get("source_type") or "") == "web":
        return QA_ROUTE_WEB
    method = str(item.get("retrieval_method") or "").strip().casefold()
    if method in _ROUTE_BY_METHOD:
        return _ROUTE_BY_METHOD[method]
    if method.startswith("graph"):
        return QA_ROUTE_GRAPH
    if method.startswith("policy"):
        return QA_ROUTE_POLICY_EXACT
    return QA_ROUTE_KEYWORD


def _hop_route(evidence, stats=None) -> str:
    """本跳的主检索通道（SearchTrace 的 `route`）。

    取本跳证据里出现次数最多的通道；同票按首次出现顺序取胜出者（dict 保序），
    保证同样的输入永远给同一个 route。没有可用证据时：有候选就算跑过检索（记基础通道
    keyword），一条候选都没有才留空串（"没搜"与"搜了没中"是两件事）。
    """
    counts: dict[str, int] = {}
    for item in evidence or []:
        if not isinstance(item, Mapping):
            continue
        token = _route_of_evidence(item)
        counts[token] = counts.get(token, 0) + 1
    if not counts:
        metrics = stats if isinstance(stats, Mapping) else {}
        return QA_ROUTE_KEYWORD if _as_int(metrics.get("eligible"), 0) > 0 else ""
    return max(counts.items(), key=lambda item: item[1])[0]


def _hop_counts(stats, evidence) -> tuple[int, int]:
    """本跳的候选/采纳口径（SearchTrace 的 `results` / `accepted`）。

    results = 进入打分前过滤的候选数（检索 stats 的 eligible），accepted = 实际采纳条数
    （stats 的 adopted）。拿不到 stats 的分支（第 1 跳复用、异常/超预算跳）退回"证据条数"，
    并保证 results >= accepted >= 0——rejected 由调用点按 results - accepted 得出。
    """
    metrics = stats if isinstance(stats, Mapping) else {}
    evidence_count = len([item for item in (evidence or []) if isinstance(item, Mapping)])
    accepted = max(0, _as_int(metrics["adopted"], evidence_count)) if "adopted" in metrics else evidence_count
    results = _as_int(metrics["eligible"], evidence_count) if "eligible" in metrics else evidence_count
    return max(results, accepted), accepted


def _hop_carry_terms(evidence, exclude, limit: int = 6):
    """从上一跳证据里挑"可带到下一跳"的词：出现在标题里的短词优先，排除已用过的。"""
    from collections import Counter

    counter = Counter()
    for item in evidence or []:
        title = str(item.get("title") or "")
        try:
            from qa_retrieval import _terms

            for term in _terms([title]):
                text = str(term).strip()
                if 2 <= len(text) <= 12:
                    counter[text] += 1
        except Exception:
            continue
    blocked = {str(item).casefold() for item in (exclude or [])}
    picked = []
    for term, _count in counter.most_common(40):
        if term.casefold() in blocked or term in picked:
            continue
        picked.append(term)
        if len(picked) >= limit:
            break
    return picked


def _gap_plan_claims(retrieval_plan, *, question: str, mode: str = "") -> tuple:
    """阶段 07（P07-01）：缺口分析要的"必要 Claim"（§12 的原话：哪些**必要 Claim** 缺什么证据）。

    复用 Phase 05 `qa_execution_graph.build_research_plan()`（**不重写分解器**）：它把
    `plan["decomposition"]` 的 hops 一对一映射成 sub_question 与 plan claim（role=answer/link）；
    这里只做一件事——把它的陈述前缀"需要证实或证伪："去掉，让缺口生成的查询读起来像人话。

    算不出来就退化成"问题本身就是唯一必要 claim"：宁可少一层分析，也不许因为计划不可用
    就整段跳过缺口分析（那样 NO_GAIN/UNRESOLVABLE_CONTRADICTION 永远产不出来）。
    """
    text = str(question or retrieval_plan.get("question") or "")
    prefix = "需要证实或证伪："
    try:
        from qa_execution_graph import build_research_plan

        plan = build_research_plan(text, plan=retrieval_plan, mode=str(mode or "standard"))
        claims = []
        for row in plan.get("claims") or []:
            if not isinstance(row, Mapping):
                continue
            item = dict(row)
            item["statement"] = str(item.get("statement") or "").replace(prefix, "", 1).strip()
            claims.append(item)
        if claims:
            return plan, claims
    except Exception:          # noqa: BLE001 —— 计划不可用只降级，不影响多跳
        pass
    return ({}, [{"claim_id": "q1", "statement": text, "role": "answer",
                  "plan_node_kind": "claim"}])


def _gap_loop_category(retrieval_plan: Mapping | None) -> str:
    """缺口分析要的问题类别（复用既有 plan["category"]，口径在 qa_gap_analyzer 里）。"""
    from qa_gap_analyzer import category_of

    return category_of(retrieval_plan)


def _run_multi_hop(article_retriever, retrieval_plan, first_local, context, *,
                   pack_id: str, limit: int, emit_stage_event=None,
                   trace_recorder=None, round_index: int = 0, store=None,
                   run_meta: Mapping | None = None, question: str = "",
                   corpus_version: str = "", hop_audit: dict | None = None,
                   budget_seconds: float | None = None,
                   plan_claims: Sequence[Mapping] | None = None, mode: str = "",
                   retrieval_config_version: str = ""):
    """按 DAG 顺序执行多跳检索，返回 (合并后的 local, 每跳回执)。

    预算与跳数是**双重硬约束**：跳数上限 `QA_MAX_HOPS`，墙钟上限
    `QA_MULTI_HOP_BUDGET_SECONDS`；超了就停在做完的跳上，并在回执里写明 `degraded`
    与原因，由 logic_validation 阶段对用户明说。

    阶段 10 增补：
      · `trace_recorder`：每跳完成后回调一次（写 `qa_reasoning_traces`，**失败不影响主流程**）；
      · `round_index`：递归重规划的轮次（0 = 首轮，1 = 依据缺失链接补检那一轮）。

    阶段 02 缺口 1 增补：每一跳取回的证据都过一遍证据层（标注 + seen 登记 + 跨轮去重），
    回执累加进调用方传入的 `hop_audit`，最终并入既有 `stats["evidence_layer"]`（不新增返回键）。
    `store` / `run_meta` 缺省为 None：拿不到作用域时只记 `skipped_scope`，绝不报错。

    阶段 07（P07-01…P07-06）增补（`QA_GAP_ANALYZER` **默认关**时逐字回到上面这套行为）：
      · 每跳之后跑一次缺口分析（`GapLoopState.observe`），逐轮记
        `new_verified_claims` / `resolved_high_priority_gaps` / `new_evidence`；
      · DAG 与补检跑完后，按缺口规划**补充跳**（§13 的 Gap → Best Retrieval Action），
        上限 `QA_GAP_MAX_NEXT_HOPS`、且受同一份墙钟预算与跳数硬上限约束；
      · 收尾时按 §14 给出**停止原因**（`merged["gap_loop"]["stop_reason"]`），
        并把 `gap_id`/`new_claims`/`resolved_gap` 通过 `trace_recorder` 真正落库
        （Phase 01 埋的 `qa_reasoning_traces` 三列，本阶段填上）。
    """
    import time as _time

    hops = list((retrieval_plan.get("decomposition") or {}).get("hops") or [])
    if len(hops) <= 1 and not gap_analyzer_enabled():
        # 既有行为（开关关着时逐字不变）：没有 DAG 就没有多跳可跑。
        # 开关打开时**单跳问题也进缺口循环**——§13 的 "Gap → Next Hop" 对单跳问题最有用
        # （首跳没解掉的缺口只能靠补充跳），只在开关打开时生效。
        return first_local, []

    budget = float(getattr(config, "QA_MULTI_HOP_BUDGET_SECONDS", 25) or 25)
    if budget_seconds is not None and float(budget_seconds) > 0:
        # 阶段 05（P05-05）：预算可由执行图给出（图关着时是 None → 逐字保持既有行为）
        budget = float(budget_seconds)
    started = _time.monotonic()
    merged_evidence = list(first_local.get("evidence") or [])
    seen_refs = {str(item.get("evidence_ref") or "") for item in merged_evidence}
    receipts = []
    carry_terms = []
    question = str(retrieval_plan.get("question") or "")

    # ── 阶段 07：缺口循环状态（开关关着时 gap_state 为 None，下面每一处都是空操作）──
    gap_state = None
    gap_claims: list = []
    if gap_analyzer_enabled():
        gap_claims = [dict(row) for row in (plan_claims or []) if isinstance(row, Mapping)]
        if not gap_claims:
            _, gap_claims = _gap_plan_claims(retrieval_plan, question=question, mode=mode)
        gap_state = GapLoopState(
            rounds_limit=max(2, len(hops) + 1 + max(1, int(getattr(config, "QA_MAX_HOPS", 3) or 3))),
            budget_seconds=budget, started=started,
            category=_gap_loop_category(retrieval_plan), plan=retrieval_plan,
            corpus_version=corpus_version, retrieval_config=retrieval_config_version)

    def _observe_gap_round(round_no: int, *, new_refs=()) -> dict:
        """跑一轮缺口分析（**异常一律吞掉**：缺口分析绝不能把检索阶段打断）。"""
        if gap_state is None:
            return {}
        try:
            return gap_state.observe(round_index=round_no, claims=gap_claims,
                                     evidence=merged_evidence, new_refs=new_refs)
        except Exception as exc:      # noqa: BLE001
            gap_state.record_error("observe", "%s: %s" % (type(exc).__name__, str(exc)[:120]))
            return {}

    def _attribute_gap(receipt: dict, round_receipt: Mapping) -> None:
        """把这一跳的缺口留痕写进回执（P07 的三列：gap_id / resolved_gap / new_claims）。

        口径（可复算）：
          · `resolved_gap` = 本跳解决了几个**高优**缺口（"上轮有、本轮没了"才算，见 observe）；
          · `gap_id` = 解决了就写被解决的那个；没解决就写当前最高优的 open 缺口
            （说明"这一跳之后还欠什么"）；
          · `new_claims` = 本跳新增了多少"拿到已核验支持"的 claim（§14 的 new_verified_claims）。
        """
        if gap_state is None or not isinstance(round_receipt, Mapping):
            return
        try:
            resolved = [str(item) for item in (round_receipt.get("resolved_gap_ids") or [])]
            open_ids = [str(gap.get("gap_id") or "") for gap in gap_state.high_priority_open()]
            receipt["resolved_gap"] = len(resolved)
            receipt["new_claims"] = int(round_receipt.get("new_verified_claims") or 0)
            receipt["gap_id"] = (resolved[0] if resolved
                                 else (open_ids[0] if open_ids else ""))
        except Exception:             # noqa: BLE001 —— 留痕不许影响检索
            return

    def _record(receipt, evidence_list, stats=None):
        """写一条推理留痕（缺省不写；回调内部异常一律吞掉）。

        阶段 01（F-7）：带上检索通道与候选/采纳口径（SearchTrace 的
        route / results / accepted / rejected）。
        阶段 07：带上缺口闭环三列（gap_id / new_claims / resolved_gap）——
        它们来自缺口循环的逐轮回执，**不是编的**；开关关着时是空串/0（与既有默认值一致）。
        """
        if not callable(trace_recorder):
            return
        refs = [str(item.get("evidence_ref") or "") for item in (evidence_list or [])][:40]
        results, accepted = _hop_counts(stats, evidence_list)
        try:
            trace_recorder({
                "hop_index": int(receipt.get("hop_index") or 0),
                "sub_query_id": str(receipt.get("hop_id") or ""),
                "sub_query": str(receipt.get("question") or ""),
                "depends_on": list(receipt.get("depends_on") or []),
                "used_evidence_refs": refs,
                "status": str(receipt.get("status") or ""),
                "latency_ms": int(receipt.get("latency_ms") or 0),
                "round_index": int(receipt.get("round_index", round_index)),
                "route": str(receipt.get("route") or _hop_route(evidence_list, stats)),
                "results": results,
                "accepted": accepted,
                "rejected": max(0, results - accepted),
                "gap_id": str(receipt.get("gap_id") or ""),
                "new_claims": int(receipt.get("new_claims") or 0),
                "resolved_gap": int(receipt.get("resolved_gap") or 0),
            })
        except Exception:
            return

    for index, hop in enumerate(hops):
        hop_id = str(hop.get("id") or ("h%d" % (index + 1)))
        hop_question = str(hop.get("question") or "").strip() or question
        if index == 0:
            # 第 1 跳就是主检索，直接复用，不再打一次
            receipt = {
                "hop_id": hop_id, "hop_index": index, "question": hop_question,
                "depends_on": list(hop.get("depends_on") or []),
                "purpose": str(hop.get("purpose") or ""),
                "evidence": len(merged_evidence), "status": "ok",
                "carry_terms": carry_terms, "latency_ms": 0,
            }
            carry_terms = _hop_carry_terms(merged_evidence, [question])
            _attribute_gap(receipt, _observe_gap_round(0, new_refs=seen_refs))
            receipts.append(receipt)
            _record(receipt, merged_evidence, first_local.get("stats"))
            continue

        elapsed = _time.monotonic() - started
        if elapsed >= budget:
            receipts.append({
                "hop_id": hop_id, "hop_index": index, "question": hop_question,
                "depends_on": list(hop.get("depends_on") or []),
                "purpose": str(hop.get("purpose") or ""),
                "evidence": 0, "status": "skipped_budget",
                "reason": "多跳预算 %.0f 秒已用尽（已用 %.1f 秒）" % (budget, elapsed),
                "carry_terms": list(carry_terms), "latency_ms": 0,
            })
            _record(receipts[-1], [])
            if callable(emit_stage_event):
                emit_stage_event("stage_progress", {
                    "message": "多跳预算用尽，停在第 %d 跳，后续跳已跳过并会明确标注。" % index,
                    "multi_hop": {"budget_seconds": budget, "used_seconds": round(elapsed, 1)},
                })
            break

        hop_plan = dict(retrieval_plan)
        hop_plan["question"] = hop_question
        hop_plan["queries"] = list(dict.fromkeys([hop_question, *carry_terms[:4]]))
        hop_plan["entities"] = list(dict.fromkeys(
            [*(retrieval_plan.get("entities") or []), *carry_terms]))
        # 分跳检索复用同一条检索链路（同一套闸门、时间窗、图证据），不另开旁路
        hop_started = _time.monotonic()
        try:
            hop_local = article_retriever.retrieve(
                hop_plan, industry_pack_id=pack_id,
                page_context={}, limit=limit,
            )
        except Exception as exc:
            receipts.append({
                "hop_id": hop_id, "hop_index": index, "question": hop_question,
                "depends_on": list(hop.get("depends_on") or []),
                "purpose": str(hop.get("purpose") or ""),
                "evidence": 0, "status": "error", "reason": str(exc)[:80],
                "carry_terms": list(carry_terms), "latency_ms": 0,
            })
            _record(receipts[-1], [])
            continue

        hop_evidence = list(hop_local.get("evidence") or [])
        # ── 阶段 02（缺口 1）：本跳候选先过证据层（同一套作用域：标注 + seen + 跨轮去重）──
        # 被跨轮去重丢掉的来源不进合并池；通过的候选只登记成中性 seen（还没过闸门）。
        hop_evidence = _wire_hop_evidence_layer(
            hop_evidence, question=question, plan=retrieval_plan, run_meta=run_meta,
            store=store, round_index=round_index, corpus_version=corpus_version,
            route=_hop_route(hop_evidence, hop_local.get("stats")), audit=hop_audit)
        added = 0
        added_refs = []
        for item in hop_evidence:
            ref = str(item.get("evidence_ref") or "")
            if ref and ref in seen_refs:
                continue
            seen_refs.add(ref)
            merged_evidence.append(item)
            added += 1
            added_refs.append(ref)
        receipt = {
            "hop_id": hop_id, "hop_index": index, "question": hop_question,
            "depends_on": list(hop.get("depends_on") or []),
            "purpose": str(hop.get("purpose") or ""),
            "evidence": len(hop_evidence), "added": added,
            "status": "ok" if hop_evidence else "empty",
            "carry_terms": list(carry_terms),
            "latency_ms": int((_time.monotonic() - hop_started) * 1000),
        }
        carry_terms = _hop_carry_terms(hop_evidence, [question, hop_question])
        _attribute_gap(receipt, _observe_gap_round(index, new_refs=added_refs or [hop_id]))
        receipts.append(receipt)
        _record(receipt, hop_evidence, hop_local.get("stats"))
        if callable(emit_stage_event):
            emit_stage_event("stage_progress", {
                "message": "第 %d 跳「%s」取得 %d 条证据（新增 %d 条）。"
                           % (index + 1, hop_question[:40], len(hop_evidence), added),
                "multi_hop": {"hop_id": hop_id, "evidence": len(hop_evidence), "added": added},
            })

    # ── 阶段 10：依据缺失链接的**递归重规划**（一轮，且必须还有预算）──
    max_rounds = max(0, min(2, int(getattr(config, "QA_RECURSION_MAX_ROUNDS", 1) or 0)))
    if max_rounds > round_index and not any(
            str(item.get("hop_id")) == "r1" for item in receipts):
        missing = [item for item in receipts
                   if item.get("status") in ("empty", "error", "skipped_budget")]
        elapsed = _time.monotonic() - started
        remaining = budget - elapsed
        if missing and remaining > 2.0:
            repair_queries = []
            for item in missing:
                base = str(item.get("question") or "").strip()
                if base and base not in repair_queries:
                    repair_queries.append(base)
                for term in item.get("carry_terms") or []:
                    text = str(term).strip()
                    if text and text not in repair_queries:
                        repair_queries.append(text)
            repair_queries = repair_queries[:3]
            if repair_queries:
                if callable(emit_stage_event):
                    emit_stage_event("stage_progress", {
                        "message": "有 %d 跳未取到证据，按缺失链接补检一轮：%s"
                                   % (len(missing), "；".join(repair_queries[:2])),
                        "recursion": {"round": round_index + 1, "queries": repair_queries},
                    })
                repair_plan = dict(retrieval_plan)
                repair_plan["question"] = " ".join(repair_queries)[:200]
                repair_plan["queries"] = list(repair_queries)
                repair_plan["entities"] = list(dict.fromkeys(
                    [*(retrieval_plan.get("entities") or []), *carry_terms]))
                repair_started = _time.monotonic()
                try:
                    repair_local = article_retriever.retrieve(
                        repair_plan, industry_pack_id=pack_id, page_context={}, limit=limit)
                except Exception:
                    repair_local = {"evidence": []}
                # 阶段 02（缺口 1）：补检这一轮同样过证据层（它是多跳的第 r1 跳，不是旁路）
                repair_evidence = _wire_hop_evidence_layer(
                    list(repair_local.get("evidence") or []), question=question,
                    plan=repair_plan, run_meta=run_meta, store=store,
                    round_index=round_index + 1, corpus_version=corpus_version,
                    route=_hop_route(repair_local.get("evidence") or [], repair_local.get("stats")),
                    audit=hop_audit)
                added = 0
                added_refs = []
                for item in repair_evidence:
                    ref = str(item.get("evidence_ref") or "")
                    if ref and ref in seen_refs:
                        continue
                    seen_refs.add(ref)
                    merged_evidence.append(item)
                    added += 1
                    added_refs.append(ref)
                receipt = {
                    "hop_id": "r1", "hop_index": len(receipts),
                    "question": repair_plan["question"],
                    "depends_on": [item.get("hop_id") for item in missing],
                    "purpose": "依据缺失链接补检（递归第 %d 轮）" % (round_index + 1),
                    "evidence": len(repair_local.get("evidence") or []), "added": added,
                    "status": "ok" if added else "empty",
                    "carry_terms": list(carry_terms),
                    "latency_ms": int((_time.monotonic() - repair_started) * 1000),
                }
                _attribute_gap(receipt, _observe_gap_round(len(receipts),
                                                           new_refs=added_refs or ["r1"]))
                receipts.append(receipt)
                _record(receipt, repair_local.get("evidence") or [], repair_local.get("stats"))
                if callable(emit_stage_event):
                    emit_stage_event("stage_progress", {
                        "message": "补检一轮取得 %d 条新证据。" % added,
                        "recursion": {"round": round_index + 1, "added": added},
                    })
        elif missing and callable(emit_stage_event):
            emit_stage_event("stage_progress", {
                "message": "有 %d 跳未取到证据，但多跳预算已用尽，停止递归并如实标注缺口。" % len(missing),
                "recursion": {"round": round_index + 1, "stopped": "budget_exhausted"},
            })

    # ── 阶段 07（P07-03）：缺口驱动的**补充跳**（§13 Gap → Best Retrieval Action）──
    gap_next_used = 0
    if gap_state is not None:
        from qa_gap_analyzer import max_next_hops as _gap_max_next_hops

        gap_cap = max(0, int(_gap_max_next_hops()))
        from qa_query_decompose import MAX_HOPS_HARD as _MAX_HOPS_HARD

        # 缺口驱动的补充跳**不受 QA_MAX_HOPS 约束**：那个旋钮是**规划期分解上限**
        # （qa_query_decompose 用它决定 DAG 有几跳）。若拿它当运行期总跳数上限，
        # 任何已经用满 3 跳的计划都不可能再有补充跳，§13 的 "Gap → Next Hop" 就落不了地。
        # 运行期的双重硬约束仍然是：墙钟预算（同一份 budget）+ 跳数硬上限 MAX_HOPS_HARD=5。
        hard_cap = max(1, int(_MAX_HOPS_HARD))
        while gap_next_used < gap_cap:
            if gap_state.budget_exhausted():
                break
            if gap_state.no_gain_confirmed():
                # §14：连续若干轮无新增有效证据/无新增 claim → 收敛（不再发补充跳）
                if callable(emit_stage_event):
                    emit_stage_event("stage_progress", {
                        "message": "缺口闭环已收敛（连续 %d 轮无增益），不再发补充跳。"
                                   % int(gap_state.last_round.get("no_gain_streak") or 0),
                        "gap_loop": {"no_gain_streak": int(
                            gap_state.last_round.get("no_gain_streak") or 0)},
                    })
                break
            if len([item for item in receipts if str(item.get("status")) != "skipped_budget"]) >= hard_cap:
                break
            try:
                planned = gap_state.next_hops(plan=retrieval_plan, limit=1)
            except Exception as exc:      # noqa: BLE001 —— 规划器坏掉不许把检索打断
                gap_state.record_error("next_hops", "%s: %s" % (type(exc).__name__,
                                                                str(exc)[:120]))
                if callable(emit_stage_event):
                    emit_stage_event("stage_progress", {
                        "message": "缺口下一跳规划失败（%s），停止补充跳。" % type(exc).__name__,
                        "gap_loop": {"planner_error": "%s: %s" % (type(exc).__name__,
                                                                  str(exc)[:120])},
                    })
                break
            hop_tasks = list(planned.get("hops") or [])
            if planned.get("dedupe", {}).get("dropped_count"):
                if callable(emit_stage_event):
                    emit_stage_event("stage_progress", {
                        "message": "缺口驱动的下一跳被 seen 去重拦下 %d 条（不重复搜同一句话）。"
                                   % int(planned["dedupe"]["dropped_count"]),
                        "gap_dedupe": planned.get("dedupe") or {},
                    })
            if not hop_tasks:
                break
            task = hop_tasks[0]
            overrides = task.get("plan_overrides") if isinstance(
                task.get("plan_overrides"), Mapping) else {}
            hop_question = str(task.get("question") or "")
            hop_plan = dict(retrieval_plan)
            hop_plan["question"] = hop_question
            hop_plan["queries"] = list(dict.fromkeys(
                [str(q) for q in (overrides.get("queries") or [])]
                + [str(q) for q in (task.get("queries") or [])] + carry_terms[:2]))
            hop_plan["entities"] = list(dict.fromkeys(
                [*(retrieval_plan.get("entities") or [])]
                + [str(e) for e in (overrides.get("entities") or [])] + carry_terms))
            if overrides.get("terms"):
                hop_plan["queries"] = list(dict.fromkeys(
                    [*hop_plan["queries"], *[str(t) for t in overrides["terms"]]]))
            hop_started = _time.monotonic()
            try:
                gap_local = article_retriever.retrieve(
                    hop_plan, industry_pack_id=pack_id, page_context={}, limit=limit)
                gap_error = ""
            except Exception as exc:      # noqa: BLE001 —— 补充跳失败只记缺口，不影响主流程
                gap_local, gap_error = {"evidence": []}, str(exc)[:80]
            gap_evidence = _wire_hop_evidence_layer(
                list(gap_local.get("evidence") or []), question=question, plan=hop_plan,
                run_meta=run_meta, store=store, round_index=round_index + 1,
                corpus_version=corpus_version,
                route=str(task.get("route") or "") or _hop_route(
                    gap_local.get("evidence") or [], gap_local.get("stats")),
                audit=hop_audit)
            added = 0
            added_refs = []
            for item in gap_evidence:
                ref = str(item.get("evidence_ref") or "")
                if ref and ref in seen_refs:
                    continue
                seen_refs.add(ref)
                merged_evidence.append(item)
                added += 1
                added_refs.append(ref)
            gap_next_used += 1
            round_no = len(receipts)
            receipts.append({
                "hop_id": str(task.get("hop_id") or "g%d" % gap_next_used),
                "hop_index": round_no, "question": hop_question,
                "depends_on": [hop_id], "purpose": "缺口驱动补充跳（%s）：%s"
                                                   % (str(task.get("gap_id") or ""),
                                                      str(task.get("reason") or "")[:120]),
                "evidence": len(gap_evidence), "added": added,
                "status": ("error" if gap_error else "ok" if gap_evidence else "empty"),
                "reason": gap_error,
                "gap_id": str(task.get("gap_id") or ""),
                "route": str(task.get("route") or ""),
                "priority": float(task.get("priority") or 0),
                "carry_terms": list(carry_terms),
                "latency_ms": int((_time.monotonic() - hop_started) * 1000),
            })
            _record(receipts[-1], gap_local.get("evidence") or [], gap_local.get("stats"))
            if callable(emit_stage_event):
                emit_stage_event("stage_progress", {
                    "message": "按缺口 %s 补一跳（route=%s）取得 %d 条证据（新增 %d 条）。"
                               % (str(task.get("gap_id") or ""), str(task.get("route") or ""),
                                  len(gap_evidence), added),
                    "gap_hop": {"gap_id": str(task.get("gap_id") or ""),
                                "route": str(task.get("route") or ""), "added": added},
                })
            carry_terms = _hop_carry_terms(gap_evidence, [question, hop_question]) or carry_terms
            # 本轮观察 → 缺口是否被解决/是否连续无增益，就看这一下
            gap_state.observe(round_index=round_no, claims=gap_claims,
                              evidence=merged_evidence, new_refs=added_refs)
    merged = dict(first_local)
    merged["evidence"] = merged_evidence
    merged["multi_hop"] = {
        "enabled": True,
        "hops": receipts,
        # 空跳与超预算跳都算降级：某跳没证据却不说，等于把半截结论当完整结论
        "degraded": any(item.get("status") in ("skipped_budget", "error", "empty")
                        for item in receipts),
        "budget_seconds": budget,
        "used_seconds": round(_time.monotonic() - started, 2),
        "pattern": str((retrieval_plan.get("decomposition") or {}).get("pattern") or ""),
        "recursion_rounds": max_rounds,
    }
    if gap_state is not None:
        # ── P07-05/P07-06：收尾算停止原因（缺口驱动的补充跳已经用掉，这里如实判深度）──
        depth_exhausted = bool(gap_state.high_priority_open()) and (
            gap_next_used >= int(_gap_current_max_next_hops()) or gap_state.budget_exhausted())
        decision = gap_state.finalize(budget_exhausted=gap_state.budget_exhausted(),
                                      depth_exhausted=depth_exhausted, actionable_hops=0)
        receipt = gap_state.receipt()
        receipt["stage"] = "multi_hop"
        receipt["next_hops_used"] = gap_next_used
        merged["gap_loop"] = receipt
        merged["multi_hop"]["gap_stop_reason"] = str(decision.get("stop_reason") or "")
        merged["multi_hop"]["gap_loop_version"] = GAP_ANALYZER_VERSION
        if callable(emit_stage_event):
            emit_stage_event("stage_progress", {
                "message": "缺口闭环：停止原因 %s——%s"
                           % (str(decision.get("stop_reason") or ""),
                              str(decision.get("detail") or "")),
                "gap_loop": {key: receipt[key] for key in
                             ("analyzer_version", "stop_reason", "stop_detail", "stats")
                             if key in receipt},
            })
    return merged, receipts


def _gap_current_max_next_hops() -> int:
    """`QA_GAP_MAX_NEXT_HOPS` 的现读（独立函数便于用例打桩，避免 import 期取快照）。"""
    from qa_gap_analyzer import max_next_hops

    return int(max_next_hops())


def _verification_prompt_blocks(context: Mapping) -> dict:
    """阶段 03：把核验结论整理成生成端看得懂的两块（证据级 + 结论级）。

    为什么必须进生成端：MASTER_RULES 第 11 条——LLM 自由生成的内容不能直接当成已验证事实。
    证据包里"取到了但没被确认支持"的条目、以及"结论没有直接证据"的条数，都必须让综合模型
    看见，否则它会照着草稿把所有东西都写成已确证事实（真机实测草稿把所有 claim 都自评成
    qualified）。这里只报告事实，不提要求之外的指令。
    """
    if not isinstance(context, Mapping):
        return {}
    outputs = context.get("outputs") if isinstance(context.get("outputs"), Mapping) else {}
    blocks = {}
    logic = outputs.get("logic_validation") if isinstance(outputs.get("logic_validation"), Mapping) else {}
    verification = logic.get("verification") if isinstance(logic, Mapping) else None
    if isinstance(verification, Mapping) and verification.get("checked"):
        blocks["证据核验"] = {
            "核验版本": verification.get("verifier"),
            "已核验": verification.get("checked"),
            "确认支持": verification.get("supported"),
            "判定分布": verification.get("verdicts"),
            "平均分": verification.get("avg_score"),
            "要求": verification.get("note")
                    or "只有判定为 SUPPORTED 的证据可作为事实结论依据；其余需注明不确定。",
        }
    graph = outputs.get("conflict_review") if isinstance(outputs.get("conflict_review"), Mapping) else {}
    graph_verification = graph.get("verification") if isinstance(graph, Mapping) else None
    stats = graph_verification.get("stats") if isinstance(graph_verification, Mapping) else None
    if isinstance(stats, Mapping) and stats.get("claims"):
        blocks["结论核验"] = {
            "结论数": stats.get("claims"),
            "有直接证据支持": stats.get("confirmed"),
            "仅部分支持": stats.get("qualified"),
            "与证据冲突": stats.get("conflicted"),
            "证据不足": int(stats.get("insufficient_evidence") or 0),
            "无支持证据占比": stats.get("unsupported_claim_rate"),
        }
    return blocks


def _verification_summary(evidence: list) -> dict:
    """证据核验分布（阶段 03）：只统计**已经核验过**的证据条目（不在这里重复跑核验）。

    为什么放在逻辑校验里：`logic_validation` 是"能不能下结论"的关口，用户看到的降级
    提示也来自它。核验分布（几条真支持 / 几条只是未核验）属于同一类信息——
    但它**不改变 status/missing_links**：改状态会让"证据里有未核验项"直接变成降级，
    那是产品决策，不是这一阶段能单方面定的（留到阶段 13 的 Final Verifier 一起定）。
    """
    verdicts: dict = {}
    scores = []
    unverified = 0
    for item in evidence or []:
        value = verification_of(item)
        if not value:
            unverified += 1
            continue
        verdict = str(value.get("verdict") or "")
        if verdict:
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
        try:
            scores.append(float(value.get("score")))
        except (TypeError, ValueError):
            continue
    checked = sum(verdicts.values())
    return {
        "verifier": VERIFIER_VERSION,
        "enabled": verifier_enabled(),
        "checked": checked,
        "not_verified": unverified,
        "verdicts": verdicts,
        "supported": int(verdicts.get("SUPPORTED") or 0),
        "avg_score": round(sum(scores) / len(scores), 4) if scores else None,
        "note": ("证据中有 %d 条未能确认支持关系，作答时不得当作已确证事实。" % unverified)
                if unverified else "",
    }


def _logic_validation(question: str, retrieval: Mapping, plan: Mapping) -> dict:
    """阶段 9 · 逻辑校验：因果链、条件满足、缺失链接（纯规则，不调用 LLM）。

    输出 {status, checks, missing_links, degraded, note}：
      · `passed`     该类型要求的链条都找到了证据
      · `degraded`   多跳有跳没取到证据 / 预算用尽 → 必须在答复里明说
      · `insufficient` 一条都没有，属于证据不足
    """
    evidence = list(retrieval.get("evidence") or [])
    category = str((plan.get("category") or {}).get("key") or "")
    multi_hop = dict(retrieval.get("multi_hop") or {})
    checks = []
    missing = []

    def _haystack():
        return " ".join(
            "%s %s" % (str(item.get("title") or ""), str(item.get("content_excerpt") or "")[:800])
            for item in evidence)

    text = _haystack()

    if category == "causal":
        markers = ("导致", "因为", "由于", "引起", "造成", "原因是", "推动", "带动", "驱动", "因此", "使得")
        hit = [marker for marker in markers if marker in text]
        checks.append({"check": "causal_chain", "passed": bool(hit),
                       "detail": "命中因果标记：%s" % ("、".join(hit[:5]) if hit else "无")})
        if not hit:
            missing.append({"type": "causal_link", "detail": "证据里没有因果连接词，无法确证因果关系"})

    if category == "conditional_constraint":
        condition = str((plan.get("decomposition") or {}).get("hops", [{}])[0].get("question") or "")
        terms = [term for term in (condition or "").replace("，", " ").split(" ") if len(term) >= 2][:4]
        hit = [term for term in terms if term in text]
        passed = bool(hit) if terms else bool(evidence)
        checks.append({"check": "condition_coverage", "passed": passed,
                       "detail": "条件词命中：%s" % ("、".join(hit) if hit else "无")})
        if not passed:
            missing.append({"type": "condition_gap", "detail": "条件本身的规定没有取到证据"})

    if category == "temporal_relation":
        # 容错空格写法（"2025 年 3 月" 与 "2025年3月" 都要认）
        dates = re.findall(r"20\d{2}\s*[-年/]\s*\d{1,2}", text)
        passed = len(set(dates)) >= 2
        checks.append({"check": "temporal_order", "passed": passed,
                       "detail": "证据中出现 %d 个不同时间点" % len(set(dates))})
        if not passed:
            missing.append({"type": "temporal_gap", "detail": "证据不足以判定先后顺序"})

    for hop in multi_hop.get("hops") or []:
        if hop.get("status") in ("empty", "error", "skipped_budget"):
            missing.append({
                "type": "hop_missing",
                "detail": "第 %s 跳「%s」未取到证据（%s）"
                          % (hop.get("hop_id"), str(hop.get("question") or "")[:30],
                             hop.get("status")),
            })

    if multi_hop.get("degraded"):
        reason = "；".join(item["detail"] for item in missing if item["type"] == "hop_missing")[:160]
        note = "多跳检索未走完：%s。已用已完成的跳给出结论。" % (reason or "有跳未取到证据")
        status = "degraded"
    elif not evidence:
        note = "没有取到任何证据，属于证据不足。"
        status = "insufficient"
    elif missing:
        note = "；".join(item["detail"] for item in missing)[:200]
        status = "degraded"
    else:
        note = ""
        status = "passed"

    return {
        "status": status,
        "checks": checks,
        "missing_links": missing,
        "degraded": status in ("degraded", "insufficient"),
        "note": note,
        "multi_hop": multi_hop,
        # 阶段 03：证据核验分布（只报告，不改 status/note —— 见 _verification_summary 的说明）
        "verification": _verification_summary(evidence),
    }


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


def _local_corpus_version(database, pack_id: str) -> str:
    """本地语料指纹：本包 active 文章的条数 + 最新入库时间 → sha256 前 24 位。

    阶段 01（F-5）从 `build_qa_stage_handlers` 里提成模块级函数（SQL 与口径一字未改），
    让 `qa_storage.create_run` 能把**与检索缓存 kb_version 同源**的语料版本写进
    `qa_runs.corpus_version`。取不到（库不可用/无该表）时返回 "unknown"——
    与检索缓存侧的老行为完全一致，不抛异常、不阻塞建 run。
    """
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


def _build_hunter_fleet(database, article_retriever):
    """阶段 04（P04-06）：按需构造默认舰队（五个 Hunter 全部接既有组件，见 qa_hunters）。

    只读、零模型调用：语义通道吃库内既有向量，绝不请求 embedding 端点。
    """
    from qa_hunter_fleet import build_default_fleet

    return build_default_fleet(database=database, retriever=article_retriever)


def _hunter_fleet_enabled() -> bool:
    """`QA_HUNTER_FLEET` 开关（默认关）。每次都重新读环境变量，便于测试与灰度切换。"""
    try:
        from qa_hunter_fleet import fleet_enabled

        return bool(fleet_enabled())
    except Exception:
        return False


def _execution_graph_enabled() -> bool:
    """`QA_EXECUTION_GRAPH` 开关（默认关）：打开才建执行图并记节点预算（阶段 05）。"""
    try:
        from qa_execution_graph import graph_enabled

        return bool(graph_enabled())
    except Exception:
        return False


def _execution_graph_node_runs_enabled() -> bool:
    """`QA_EXECUTION_GRAPH_NODE_RUNS`（默认关）：打开才把节点写进 `qa_stage_runs` 的 node 列。"""
    try:
        from qa_execution_graph import node_runs_enabled

        return bool(node_runs_enabled())
    except Exception:
        return False


# 多跳每跳回执的 status → 执行图节点状态（既有取值 → 契约取值，不新造语义）
_HOP_STATUS_TO_NODE = {
    "ok": "ok", "empty": "empty", "error": "error",
    "skipped_budget": "budget_exhausted", "single_hop": "ok",
}


def _graph_node_id(graph, hop_id: str) -> str:
    """执行图里"第 N 跳"由哪个节点产出（舰队打开时首跳是 merge 节点）。"""
    try:
        from qa_execution_graph import hop_node_id

        return hop_node_id(graph, hop_id)
    except Exception:
        return ""


def _graph_ledger_record_verification(ledger, graph, audit: Mapping) -> None:
    """核验 / 重排两个节点的账本回填（数据来自既有证据层回执，不另算一遍）。"""
    try:
        path = str((graph or {}).get("path") or "")
        annotated = int((audit or {}).get("annotated") or 0)
        ledger.record("%s.verify" % path, status="ok" if annotated else "empty",
                      detail="证据层标注 + 核验判定", evidence=annotated)
        verification = (audit or {}).get("verification") or {}
        reordered = int(verification.get("reordered") or 0)
        ledger.record("%s.rerank" % path, status="ok" if annotated else "empty",
                      detail="按核验分重排（reordered=%d）" % reordered, evidence=reordered)
    except Exception:
        return


def _build_run_graph(retrieval_plan: Mapping, *, pack_id: str, policy_resolver,
                     feature_flags, run_meta: Mapping, question: str, mode,
                     total_seconds=None):
    """阶段 05：建执行图 + 运行账本（任何异常都收敛成"没有图"，绝不影响检索）。

    复用链：`retrieval_plan.decomposition`（既有 DAG）→ `qa_execution_graph.build_execution_graph`
    → 图里带上 §2.2 的 Node 契约、失败策略五值、并行组与预算；返回 `(graph, ledger, receipt)`。
    """
    try:
        from qa_execution_graph import ExecutionLedger, build_execution_graph, graph_receipt

        policy = None
        try:
            policy = policy_resolver.resolve(pack_id) if policy_resolver is not None else None
        except Exception:
            policy = None
        level2_enabled: bool | None = None
        try:
            snapshot = (feature_flags.snapshot() or {}) if feature_flags is not None else {}
            if "level2_enabled" in snapshot:
                level2_enabled = bool(snapshot.get("level2_enabled"))
            elif (run_meta or {}).get("level2_enabled") is not None:
                level2_enabled = bool((run_meta or {}).get("level2_enabled"))
        except Exception:
            level2_enabled = None
        graph = build_execution_graph(
            question, plan=retrieval_plan, mode=str(mode or "standard"), policy=policy,
            level2_enabled=True if level2_enabled is None else bool(level2_enabled),
            run_id=str((run_meta or {}).get("id") or ""), total_seconds=total_seconds)
        ledger = ExecutionLedger(graph)
        return graph, ledger, graph_receipt(graph, ledger=ledger)
    except Exception as exc:
        return None, None, {
            "error": "%s: %s" % (type(exc).__name__, str(exc)[:200]),
            "fallback": "无执行图（既有行为不变）",
        }


def _ledger_record_retrieval(ledger, graph, local: Mapping) -> None:
    """把首跳检索的结果记进账本（节点 id 由执行图给出，舰队打开时是 merge 节点）。"""
    try:
        from qa_execution_graph import hop_node_id

        node_id = hop_node_id(graph, "h1")
        evidence = len(local.get("evidence") or [])
        ledger.record(node_id, status="ok" if evidence else "empty",
                      detail="首跳检索（既有 retriever 或舰队扇入）", evidence=evidence)
    except Exception:
        return


def _ledger_record_hops(ledger, graph, receipts) -> None:
    """把 `_run_multi_hop` 的每跳回执记进账本（第 1 跳已记过，跳过）。"""
    try:
        from qa_execution_graph import hop_node_id
    except Exception:
        return
    for item in receipts or []:
        hop_id = str(item.get("hop_id") or "")
        if hop_id in ("", "h1"):
            continue
        node_id = hop_node_id(graph, hop_id)
        status = _HOP_STATUS_TO_NODE.get(str(item.get("status") or ""), "ok")
        try:
            ledger.record(node_id, status=status, latency_ms=int(item.get("latency_ms") or 0),
                          detail=str(item.get("reason") or item.get("purpose") or "")[:160],
                          evidence=int(item.get("evidence") or 0))
        except Exception:
            continue


def _local_retrieval(article_retriever, fleet, plan: Mapping, *, industry_pack_id: str,
                     page_context, limit: int) -> tuple:
    """level1 首跳检索：默认走既有 `ArticleRetriever.retrieve()`；舰队打开时走并行风扇。

    返回 `(结果, 舰队回执)`。舰队是**并列的新入口**，不是替换：
      · `QA_HUNTER_FLEET` 默认关 → 逐字回到旧路径（零行为变化）；
      · 打开时舰队任何异常都**回落**到既有检索，并把原因写进回执——
        一个通道挂了不许把整条检索拖成失败（§2.7 Failure Isolation）。
    """
    if fleet is None:
        return article_retriever.retrieve(
            plan, industry_pack_id=industry_pack_id, page_context=page_context, limit=limit), {}
    try:
        result = fleet.retrieve(plan, industry_pack_id=industry_pack_id,
                                page_context=page_context, limit=limit)
        return result, dict((result.get("stats") or {}).get("hunter_fleet") or {})
    except Exception as exc:
        receipt = {"error": "%s: %s" % (type(exc).__name__, str(exc)[:200]),
                   "fallback": "ArticleRetriever.retrieve",
                   "note": "舰队失败已回落到既有检索路径（证据包不受影响）"}
        return article_retriever.retrieve(
            plan, industry_pack_id=industry_pack_id, page_context=page_context, limit=limit), receipt


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
    hunter_fleet=None,
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
    # 阶段 04（P04-06）：舰队**每个 handler 集只装配一次**（Hunter 内的分词缓存与候选池
    # 才能跨问题复用）。`ready=False` 表示还没按开关决定要不要建。
    _fleet_state = {"fleet": hunter_fleet, "ready": hunter_fleet is not None}

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
        # 阶段 10-3：会话级约束固化 —— 上一轮已确认的时间/实体/输出形式/排除项，
        # 作为本轮规划的**前置默认**（只补空缺，绝不覆盖用户本轮明确说的话）。
        run_meta = context.get("run") or {}
        request_payload = dict(context["request"])
        saved_constraints = _load_session_constraints(store, run_meta)
        if saved_constraints:
            request_payload["session_constraints"] = saved_constraints
            if callable(context.get("_emit_stage_event")):
                context["_emit_stage_event"]("stage_progress", {
                    "message": "沿用本会话已确认的约束：%s"
                               % "、".join("%s=%s" % (k, v) for k, v in list(saved_constraints.items())[:3]),
                    "session_constraints": saved_constraints,
                })
        result = planner.plan(request_payload)
        _persist_session_constraints(store, run_meta, result)
        question_plan = result.get("question_plan") if isinstance(result.get("question_plan"), Mapping) else {}
        emit_stage_event = context.get("_emit_stage_event")
        if callable(emit_stage_event) and question_plan:
            message = render_question_plan_status(question_plan)
            emit_stage_event("stage_progress", {"message": message, "question_plan": question_plan})
            # 阶段 5 回执：把"你的调整被解析成了什么"直接告诉用户。
            # 实测问题：调整只换了回答模板，用户看不到任何变化 → 以为系统没听懂。
            receipt = question_plan.get("adjustment_receipt") or result.get("adjustment_receipt") or {}
            if isinstance(receipt, Mapping) and receipt.get("summary"):
                emit_stage_event("stage_progress", {
                    "message": str(receipt["summary"]),
                    "adjustment_receipt": dict(receipt),
                })
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
            # 阶段 5：调整落到检索与输出形式后，生成端必须真的照做
            "输出形式": question_plan.get("output_form"),
            "原文全文逐段": bool(question_plan.get("must_fetch_fulltext")),
            "时间范围（用户调整）": question_plan.get("time_window"),
            "调整回执": question_plan.get("adjustment_receipt"),
            "子问题": question_plan.get("subquestions"),
        }
        # 阶段 9：多跳与逻辑校验的结论要进生成端 —— 有缺口必须明说，
        # 不能让答案看起来"完全确证"。
        logic = context["outputs"].get("logic_validation") or {}
        if isinstance(logic, Mapping) and logic:
            summary["逻辑校验"] = {
                "结论": logic.get("status"),
                "缺口": [item.get("detail") for item in (logic.get("missing_links") or [])][:5],
                "说明": logic.get("note"),
                "跳数回执": [
                    {"跳": item.get("hop_id"), "问句": str(item.get("question") or "")[:40],
                     "证据": item.get("evidence"), "状态": item.get("status")}
                    for item in ((logic.get("multi_hop") or {}).get("hops") or [])
                ][:5],
            }
            # 阶段 03（MASTER_RULES 第 11 条）：核验分布必须进生成端——
            # 让综合模型知道"哪些证据只是取到了、并没有被确认支持"，不许当成已确证事实写。
            summary.update(_verification_prompt_blocks(context))
        return standalone + "\n\n问题拆解与回答计划：" + json.dumps(summary, ensure_ascii=False)

    def _local_version(pack_id: str) -> str:
        # 阶段 01（F-5）：实现提到模块级 `_local_corpus_version`（口径一字不改），
        # 这样 qa_storage.create_run 才能复用同一个语料指纹写 qa_runs.corpus_version。
        return _local_corpus_version(database, pack_id)

    def _scope_hash(context) -> str:
        page = (context["request"].get("page_context") or {})
        if not page:
            return "public-pack"
        return hashlib.sha256(str(context["run"].get("owner_user_id") or "").encode("utf-8")).hexdigest()[:24]

    def level1_retrieval(context):
        request_payload = context["request"]
        retrieval_plan = context["outputs"]["plan"]
        run_meta = context.get("run") or {}
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
        # ── 阶段 04（P04-06）：首跳检索 = 既有 retriever 或并行舰队（默认关，见 _local_retrieval）──
        _fleet = _fleet_state["fleet"]
        if not _fleet_state["ready"]:
            _fleet_state["ready"] = True
            if _hunter_fleet_enabled():
                try:
                    _fleet = _fleet_state["fleet"] = _build_hunter_fleet(database, article_retriever)
                except Exception as exc:  # 舰队装配失败也不能影响检索
                    _fleet = _fleet_state["fleet"] = None
                    if callable(emit_stage_event):
                        emit_stage_event("stage_progress", {
                            "message": "并行检索舰队不可用，已使用常规检索：%s"
                                       % str(exc)[:120]})
        # ── 阶段 05（P05-01…P05-05）：执行图 + 运行账本（`QA_EXECUTION_GRAPH` 默认关）──
        # 关掉时这一段是空操作：既不建图、也不改任何返回键（零行为变化）。
        execution_graph = None
        graph_ledger = None
        graph_receipt: dict = {}
        if _execution_graph_enabled():
            execution_graph, graph_ledger, graph_receipt = _build_run_graph(
                retrieval_plan, pack_id=pack_id, policy_resolver=policy_resolver,
                feature_flags=feature_flags, run_meta=run_meta,
                question=_planned_question(context), mode=request_payload.get("mode"),
                total_seconds=_env_int("QA_EXECUTION_GRAPH_BUDGET_SECONDS", 0, 0, 100000) or None)
        if graph_ledger is not None:
            graph_ledger.begin(_graph_node_id(execution_graph, "h1"))
        local, fleet_receipt = _local_retrieval(
            article_retriever, _fleet,
            {**dict(retrieval_plan), "question": _planned_question(context)},
            industry_pack_id=pack_id,
            page_context=request_payload.get("page_context") or {},
            limit=_evidence_limit(retrieval_plan, request_payload.get("mode")),
        )
        if graph_ledger is not None:
            _ledger_record_retrieval(graph_ledger, execution_graph, local)
        # ── 多跳执行（阶段 9）──
        # 只对确有逻辑结构的问题生效（分解器给出的 DAG + 每跳 depends_on）；
        # 上一跳定位到的实体作为下一跳的过滤条件；受预算与跳数双重约束，
        # 超预算就停在做完的跳并明确标注降级（不静默给半截结论）。
        hop_receipts = []
        # 阶段 02（缺口 1）：多跳每一跳的证据层回执累加到这里，最后并入 stats["evidence_layer"]
        hop_evidence_audit: dict = {}
        # 阶段 07（P07-01）：缺口分析要的"必要 Claim"（复用 Phase 05 的研究计划；见 _gap_plan_claims）
        gap_plan: dict = {}
        gap_plan_claims: list = []
        if gap_analyzer_enabled():
            gap_plan, gap_plan_claims = _gap_plan_claims(
                retrieval_plan, question=_planned_question(context),
                mode=str(request_payload.get("mode") or "standard"))
        multi_hop_plan = retrieval_plan.get("decomposition") or {}
        # 阶段 07：缺口循环打开时，**没有多跳 DAG 的问题也进循环**（单跳问题同样可能带缺口，
        # 而 §13 的补充跳正是为它们准备的）；开关关着时条件与原来逐字一致。
        if bool(getattr(config, "QA_MULTI_HOP_ENABLED", True)) and (
                multi_hop_plan.get("is_multi_hop") or gap_analyzer_enabled()):
            def _trace_recorder(entry):
                """阶段 10 + 阶段 01（F-7）：每跳写一条推理留痕（store 内部已吞异常）。

                `entry` 里的 route/results/accepted/rejected 由 `_run_multi_hop._record`
                填；老的 recorder 调用点不传这些键也不影响（`.get` + 默认值兜底）。

                注意：run_id 必须取本阶段作用域里的 `run_meta`——这里原先是 `run["id"]`，
                而 `level1_retrieval` 里根本没有 `run` 这个名字（其它阶段才有），于是每跳都撞
                `NameError`，被 `_record` 的 `except Exception: return` 静默吞掉 →
                `qa_reasoning_traces` 永远是空的（Phase 01 F-7 的 route/计数一个都没落库）。
                """
                results = int(entry.get("results") or 0)
                accepted = int(entry.get("accepted") or 0)
                store.record_reasoning_trace(
                    str(run_meta.get("id") or ""),
                    hop_index=int(entry.get("hop_index") or 0),
                    sub_query_id=str(entry.get("sub_query_id") or ""),
                    sub_query=str(entry.get("sub_query") or ""),
                    depends_on=entry.get("depends_on") or [],
                    partial_answer="已取到 %d 条证据" % len(entry.get("used_evidence_refs") or []),
                    used_evidence_refs=entry.get("used_evidence_refs") or [],
                    missing_links=[] if entry.get("status") == "ok" else [
                        {"type": "hop_missing", "detail": "第 %s 跳状态=%s"
                         % (entry.get("sub_query_id"), entry.get("status"))}],
                    next_queries=[str(entry.get("sub_query") or "")] if entry.get("status") != "ok" else [],
                    status=str(entry.get("status") or ""),
                    round_index=int(entry.get("round_index") or 0),
                    latency_ms=int(entry.get("latency_ms") or 0),
                    route=str(entry.get("route") or ""),
                    results=results,
                    accepted=accepted,
                    rejected=int(entry.get("rejected") or 0) if entry.get("rejected") is not None
                    else max(0, results - accepted),
                    # ── 阶段 07：缺口闭环三列（Phase 01 埋的列，这里真正落库）──
                    gap_id=str(entry.get("gap_id") or ""),
                    new_claims=int(entry.get("new_claims") or 0),
                    resolved_gap=int(entry.get("resolved_gap") or 0),
                )

            local, hop_receipts = _run_multi_hop(
                article_retriever, retrieval_plan, local, context,
                pack_id=pack_id, limit=12, emit_stage_event=emit_stage_event,
                trace_recorder=_trace_recorder, store=store, run_meta=run_meta,
                question=_planned_question(context), corpus_version=kb_version,
                hop_audit=hop_evidence_audit,
                # 阶段 05：图打开时用图给出的多跳预算（缺省 None → 既有 config 值，零变化）
                budget_seconds=(float((execution_graph.get("budget") or {}).get("hop_budget_seconds"))
                                if execution_graph else None),
                # 阶段 07：缺口分析的"必要 Claim"与研究计划（开关关着时是空，整段空操作）
                plan_claims=gap_plan_claims,
                mode=str(request_payload.get("mode") or "standard"),
                retrieval_config_version="qa-retrieval-v3-policy-exact",
            )
        elif multi_hop_plan.get("hops"):
            hop_receipts = [{
                "hop_id": "h1", "question": _planned_question(context),
                "depends_on": [], "evidence": len(local.get("evidence") or []),
                "status": "single_hop", "carry_terms": [],
            }]
        if graph_ledger is not None:
            # 每跳回执 → 账本（超预算跳在 `_run_multi_hop` 里已经是 skipped_budget）
            _ledger_record_hops(graph_ledger, execution_graph, hop_receipts)
        external = web_search.search(
            retrieval_plan.get("queries") or [],
            enabled=bool(retrieval_plan.get("needs_web")),
            limit=8,
        )
        cap = _evidence_cap(retrieval_plan, request_payload.get("mode"),
                            _evidence_limit(retrieval_plan, request_payload.get("mode")))
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
        # ── 阶段 02（P02-01…P02-04）：证据层标注 + seen 登记 + 跨轮/跨 run 去重 ──
        # `raw_evidence` 是被闸门筛掉前的候选全集，交给证据层当"被拒身份"登记；
        # 去重默认只丢上一轮被拒的来源，证据包不会被无谓削薄（详见 _apply_evidence_layer）。
        evidence, evidence_audit = _apply_evidence_layer(
            evidence,
            rejected=raw_evidence,
            question=_planned_question(context),
            plan=retrieval_plan,
            run_meta=run_meta,
            store=store,
            corpus_version=kb_version,
        )
        if graph_ledger is not None:
            # §2.6：核验/重排跑在证据层里 —— 结果回填账本（核验了多少条、重排了多少条）
            _graph_ledger_record_verification(graph_ledger, execution_graph, evidence_audit)
        stats = dict(local.get("stats") or {})
        stats.update({"web_adopted": len(external.get("evidence") or []), "adopted": len(evidence)})
        if fleet_receipt:
            # 阶段 04（P04-06）：舰队回执放**兄弟键**（Phase 02 的 evidence_layer 键集是冻结的）
            stats["hunter_fleet"] = dict(fleet_receipt)
        # 阶段 02（缺口 1）：多跳每一跳的回执并入同一条统计（回执键集不变）
        _merge_evidence_audits(evidence_audit, hop_evidence_audit)
        stats["evidence_layer"] = _evidence_layer_receipt(evidence_audit)
        verification_receipt = _verification_receipt(evidence_audit)
        if verification_receipt:
            # 阶段 03：核验回执放**兄弟键**（evidence_layer 的键集是 Phase 02 冻结的）
            stats["verification"] = verification_receipt
        if policy_audit.get("policy_filter") == "applied":
            stats["policy_source_roles"] = policy_audit.get("source_roles") or {}
            stats["policy_noise_excluded"] = len(policy_audit.get("excluded_policy_noise") or [])
        if material_audit.get("material_cleaning") == "applied":
            stats["material_noise_excluded"] = len(material_audit.get("excluded_material_noise") or [])
        if graph_receipt:
            # 阶段 05：执行图回执放**兄弟键**（evidence_layer 六键与 verification 兄弟键都不动）；
            # 打开 `QA_EXECUTION_GRAPH_NODE_RUNS` 时顺带把节点落进 qa_stage_runs 的 node 列。
            if graph_ledger is not None:
                # 回执必须在**节点跑完之后**重算一次：建图那一刻的账本是空的
                try:
                    from qa_execution_graph import graph_receipt as _receipt

                    graph_receipt = _receipt(execution_graph, ledger=graph_ledger)
                except Exception:
                    pass
            if graph_ledger is not None and _execution_graph_node_runs_enabled() and store is not None:
                try:
                    from qa_execution_graph import record_node_runs

                    graph_receipt = {**graph_receipt, "node_runs": record_node_runs(
                        store, str(run_meta.get("id") or ""), execution_graph, ledger=graph_ledger)}
                except Exception as exc:
                    graph_receipt = {**graph_receipt, "node_runs": {
                        "error": "%s: %s" % (type(exc).__name__, str(exc)[:160])}}
            stats["execution_graph"] = graph_receipt
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
        # 知识图谱证据回执（阶段 8 扩展）：图事实用了多少条、按什么权重、
        # 有没有因为"有效期还没生效"被挡掉——必须让上层与用户看得见，否则无从判断答案依据。
        _graph = dict(local.get("graph") or {})
        if callable(emit_stage_event) and _graph.get("enabled") and _graph.get("used"):
            _graph_message = "图谱事实 %s 条（事件 %s / 属性 %s）" % (
                _graph.get("used"), _graph.get("event", 0), _graph.get("attribute", 0))
            _weights = _graph.get("weights") or {}
            if _weights:
                _graph_message += "；权重 事件 %.2f / 属性 %.2f" % (
                    float(_weights.get("event", 0)), float(_weights.get("attribute", 0)))
            if _graph.get("filtered_by_validity"):
                _graph_message += "；另有 %s 条属性因查询时点未生效被排除" % _graph.get("filtered_by_validity")
            emit_stage_event("stage_progress", {
                "message": _graph_message,
                "graph": _graph,
                "stats": stats,
            })
        # 阶段 07（P07-05/P07-06）：缺口闭环回执放**兄弟键**（Phase 02/03/05/06 同样手法）：
        # 停止原因、逐轮增益、缺口清单与补充跳都在里面，运维/验收据此出报表。
        _gap_receipt = local.get("gap_loop")
        if isinstance(_gap_receipt, Mapping):
            stats["gap_loop"] = dict(_gap_receipt)
            stats["gap_loop_summary"] = gap_summary([_gap_receipt])
            if graph_ledger is not None and execution_graph is not None:
                # 运行账本：P07 的 gap_loop 节点按真实结局记账（不再只是"未实现"）
                try:
                    graph_ledger.record(_graph_node_id(execution_graph, "gap_loop"),
                                        status=("ok" if _gap_receipt.get("stop_reason")
                                                else "skipped"),
                                        detail="停止原因 %s：%s" % (
                                            _gap_receipt.get("stop_reason") or "无",
                                            str(_gap_receipt.get("stop_detail") or "")[:160]),
                                        evidence=int((_gap_receipt.get("stats") or {}).get(
                                            "open_gaps") or 0))
                except Exception:
                    pass
        result = {
            "queries": list(retrieval_plan.get("queries") or []),
            "evidence": evidence,
            "excluded": {**dict(local.get("excluded") or {}), "policy": policy_audit, "material_cleaning": material_audit},
            "stats": stats,
            "graph": _graph,
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

    def logic_validation(context):
        """阶段 9：逻辑校验（因果链 / 条件满足 / 缺失链接），把降级与缺口明确回报。"""
        # 注意：emit_stage_event 由编排器通过 context 注入，必须显式取出
        # （漏了这行会 NameError → 每个 run 到这一阶段直接 INTERNAL_ERROR；
        #  tests/test_qa_stage_contract.py 里有守门用例钉死这一点）
        emit_stage_event = context.get("_emit_stage_event")
        result = _logic_validation(
            _planned_question(context),
            context["outputs"]["level1_retrieval"],
            context["outputs"]["plan"],
        )
        if callable(emit_stage_event):
            message = "逻辑校验：%s" % {"passed": "通过", "degraded": "有缺口（已标注）",
                                        "insufficient": "证据不足"}.get(result["status"], result["status"])
            if result.get("note"):
                message += "——" + str(result["note"])
            emit_stage_event("stage_progress", {"message": message, "logic": result})
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
                # 阶段 6-6：首 token 到达就回报，别让用户干等（草稿阶段没有可见输出）
                first_token_callback=(
                    (lambda seconds: emit_stage_event("stage_progress", {
                        "message": "模型已开始生成初步结论（首字 %.1f 秒），完整草稿仍在生成中。" % seconds,
                        "first_token_seconds": seconds,
                        "stats": {"first_token_seconds": seconds},
                    })) if callable(emit_stage_event) else None
                ),
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

        def _wire_level2_evidence(payload: Mapping, *, corpus_version: str = "") -> dict:
            """阶段 02（缺口 1）：RAGFlow/研究阶段产出的证据同样标注 + 登记 seen。

            与 level1 同一套函数、同一套作用域；route 取 `QA_ROUTE_*` 里本批证据的主导通道
            （RAGFlow dataset 检索 = `semantic`）。异常一律吞掉并如实记 reason：
            证据层绝不打断二级检索，也绝不因为它让整条 run 降级。
            """
            items = [item for item in (payload.get("evidence") or []) if isinstance(item, Mapping)]
            counts: dict[str, int] = {}
            for item in items:
                token = _research_route(item)
                counts[token] = counts.get(token, 0) + 1
            route = max(counts.items(), key=lambda pair: pair[1])[0] if counts else ""
            wired, audit = _apply_evidence_layer(
                items, rejected=[], question=_planned_question(context), plan=plan_output,
                run_meta=run, store=store, corpus_version=corpus_version,
                stage="level2_retrieval", route=route,
            )
            result_payload = dict(payload)
            result_payload["evidence"] = wired
            stats = dict(result_payload.get("stats") or {})
            stats["evidence_layer"] = _evidence_layer_receipt(audit)
            verification_receipt = _verification_receipt(audit)
            if verification_receipt:
                stats["verification"] = verification_receipt
            result_payload["stats"] = stats
            return result_payload

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
                # 阶段 02（缺口 1）：RAGFlow 证据同样过证据层（含缓存分支，别漏一条路）
                cached = _wire_level2_evidence(cached, corpus_version=kb_version)
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
            # 阶段 02（缺口 1）：RAGFlow 取回的证据标注 provenance + 按作用域登记 seen
            result = _wire_level2_evidence(result, corpus_version=kb_version)
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
        # 阶段 03（MASTER_RULES 第 11 条）：模型自评的 claim 状态一律作废，
        # 按"结论 → 引用的证据"逐对核验后重写 verification_status（纯规则，不调模型）
        verification = _verify_claim_graph_in_place(
            graph, store=store, question=question, run_meta=context["run"])
        # 阶段 06（P06-01…P06-04）：显式证据图 + 矛盾裁决（默认关；开启后结果挂兄弟键）
        evidence_graph = {}
        gap_review: dict = {}
        if evidence_graph_enabled():
            evidence_graph = _build_evidence_graph_layer(
                graph, plan=(plan_output if isinstance(plan_output, Mapping) else {}),
                run_meta=context["run"])
            # 阶段 07（P07-06）：证据图上的缺口复核（默认关；UNRESOLVABLE_CONTRADICTION 的出口）
            if gap_analyzer_enabled() and not evidence_graph.get("error"):
                gap_review = _attach_gap_review(
                    graph, evidence_graph,
                    plan=(plan_output if isinstance(plan_output, Mapping) else {}),
                    previous_stop_reason=_gap_stop_reason_from_level1(context))
            graph["evidence_graph"] = evidence_graph
        store.persist_reasoning_graph(context["run"]["id"], graph)
        if callable(emit_stage_event):
            message = (f"证据图已建立：{len(graph.get('claims') or [])} 条结论、"
                       f"{len(graph.get('evidence') or [])} 条证据，正在过滤真实冲突和缺口。")
            stats = verification.get("stats") if isinstance(verification, Mapping) else None
            if isinstance(stats, Mapping) and stats.get("claims"):
                message += "核验：{claims} 条结论中 {confirmed} 条有直接证据支持，{unsupported} 条证据不足。".format(
                    claims=stats.get("claims"), confirmed=stats.get("confirmed"),
                    unsupported=int(stats.get("claims") or 0) - int(stats.get("confirmed") or 0))
            emit_stage_event("stage_progress", {
                "message": message,
                "evidence": list(graph.get("evidence") or [])[:6],
                "verification": verification.get("stats") if isinstance(verification, Mapping) else {},
                # 阶段 06：证据图回执（关系分布/coverage/矛盾裁决）——只在开关打开时出现
                "evidence_graph": (evidence_graph.get("stats") or {}) if evidence_graph else {},
                "claim_coverage": (evidence_graph.get("coverage") or {}) if evidence_graph else {},
                # 阶段 07：缺口复核（停止原因 + 未消解矛盾）——只在两个开关都开时出现
                "gap_review": ({key: gap_review[key] for key in
                                ("stop_reason", "stop_detail", "stop_source", "stats")
                                if key in gap_review} if gap_review else {}),
            })
        return graph

    def synthesis(context):
        run = context["run"]
        level1 = context["outputs"]["level1_draft"]
        raw_level2 = context["outputs"].get("level2_research") or {}
        level2 = raw_level2 if isinstance(raw_level2, Mapping) and "confirmed_claims" in raw_level2 else {}
        graph = context["outputs"].get("conflict_review")
        if not isinstance(graph, Mapping):
            # fast 模式不跑 conflict_review：这里补建图，**同时补跑核验**，
            # 否则快速路径上的 claim 又会退回"模型自评"（MASTER_RULES 第 11 条）
            graph = build_claim_evidence_graph(level1, level2)
            _verify_claim_graph_in_place(graph, store=store, question=_question_for_synthesis(context),
                                         run_meta=run)
            if evidence_graph_enabled():
                # fast 路径同样补 P06 层（口径与 standard/deep 一致，只是没有 level2 计划）
                graph["evidence_graph"] = _build_evidence_graph_layer(
                    graph, plan=(context["outputs"].get("plan") or {}), run_meta=run)
                if gap_analyzer_enabled() and not graph["evidence_graph"].get("error"):
                    # fast 路径同样做 P07 缺口复核（否则两个停止原因只在 standard/deep 可见）
                    _attach_gap_review(
                        graph, graph["evidence_graph"],
                        plan=(context["outputs"].get("plan") or {}),
                        previous_stop_reason=_gap_stop_reason_from_level1(context))
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
        "logic_validation": logic_validation,
        "level1_draft": level1_draft,
        "level2_retrieval": level2_retrieval,
        "level2_research": level2_research,
        "conflict_review": conflict_review,
        "synthesis": synthesis,
        "citation_validation": citation_validation,
    }


__all__ = ["build_qa_stage_handlers"]

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
from qa_relevance import filter_relevant_evidence
from qa_reasoning import build_claim_evidence_graph
from qa_research import QaRagflowResearchService, enrich_ragflow_evidence_from_database, insufficient_level2_result
from qa_synthesis import QaFinalSynthesizer, fallback_final_answer
from qa_retrieval import ArticleRetriever, default_web_search_service
from qa_storage import QaStore
from qa_flags import QaFeatureFlags
from qa_resilience import QaCircuitOpen, QaPersistentResilience
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


def _run_multi_hop(article_retriever, retrieval_plan, first_local, context, *,
                   pack_id: str, limit: int, emit_stage_event=None,
                   trace_recorder=None, round_index: int = 0):
    """按 DAG 顺序执行多跳检索，返回 (合并后的 local, 每跳回执)。

    预算与跳数是**双重硬约束**：跳数上限 `QA_MAX_HOPS`，墙钟上限
    `QA_MULTI_HOP_BUDGET_SECONDS`；超了就停在做完的跳上，并在回执里写明 `degraded`
    与原因，由 logic_validation 阶段对用户明说。

    阶段 10 增补：
      · `trace_recorder`：每跳完成后回调一次（写 `qa_reasoning_traces`，**失败不影响主流程**）；
      · `round_index`：递归重规划的轮次（0 = 首轮，1 = 依据缺失链接补检那一轮）。
    """
    import time as _time

    hops = list((retrieval_plan.get("decomposition") or {}).get("hops") or [])
    if len(hops) <= 1:
        return first_local, []

    budget = float(getattr(config, "QA_MULTI_HOP_BUDGET_SECONDS", 25) or 25)
    started = _time.monotonic()
    merged_evidence = list(first_local.get("evidence") or [])
    seen_refs = {str(item.get("evidence_ref") or "") for item in merged_evidence}
    receipts = []
    carry_terms = []
    question = str(retrieval_plan.get("question") or "")

    def _record(receipt, evidence_list, stats=None):
        """写一条推理留痕（缺省不写；回调内部异常一律吞掉）。

        阶段 01（F-7）：带上检索通道与候选/采纳口径（SearchTrace 的
        route / results / accepted / rejected）。`new_claims` / `resolved_gap` 属
        阶段 07 的缺口闭环，这里不编——由 record_reasoning_trace 的默认值留 0。
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
                "round_index": int(round_index),
                "route": _hop_route(evidence_list, stats),
                "results": results,
                "accepted": accepted,
                "rejected": max(0, results - accepted),
            })
        except Exception:
            return

    for index, hop in enumerate(hops):
        hop_id = str(hop.get("id") or ("h%d" % (index + 1)))
        hop_question = str(hop.get("question") or "").strip() or question
        if index == 0:
            # 第 1 跳就是主检索，直接复用，不再打一次
            receipts.append({
                "hop_id": hop_id, "hop_index": index, "question": hop_question,
                "depends_on": list(hop.get("depends_on") or []),
                "purpose": str(hop.get("purpose") or ""),
                "evidence": len(merged_evidence), "status": "ok",
                "carry_terms": carry_terms, "latency_ms": 0,
            })
            _record(receipts[-1], merged_evidence, first_local.get("stats"))
            carry_terms = _hop_carry_terms(merged_evidence, [question])
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
        added = 0
        for item in hop_evidence:
            ref = str(item.get("evidence_ref") or "")
            if ref and ref in seen_refs:
                continue
            seen_refs.add(ref)
            merged_evidence.append(item)
            added += 1
        receipts.append({
            "hop_id": hop_id, "hop_index": index, "question": hop_question,
            "depends_on": list(hop.get("depends_on") or []),
            "purpose": str(hop.get("purpose") or ""),
            "evidence": len(hop_evidence), "added": added,
            "status": "ok" if hop_evidence else "empty",
            "carry_terms": list(carry_terms),
            "latency_ms": int((_time.monotonic() - hop_started) * 1000),
        })
        _record(receipts[-1], hop_evidence, hop_local.get("stats"))
        if callable(emit_stage_event):
            emit_stage_event("stage_progress", {
                "message": "第 %d 跳「%s」取得 %d 条证据（新增 %d 条）。"
                           % (index + 1, hop_question[:40], len(hop_evidence), added),
                "multi_hop": {"hop_id": hop_id, "evidence": len(hop_evidence), "added": added},
            })
        carry_terms = _hop_carry_terms(hop_evidence, [question, hop_question])

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
                added = 0
                for item in (repair_local.get("evidence") or []):
                    ref = str(item.get("evidence_ref") or "")
                    if ref and ref in seen_refs:
                        continue
                    seen_refs.add(ref)
                    merged_evidence.append(item)
                    added += 1
                receipts.append({
                    "hop_id": "r1", "hop_index": len(receipts),
                    "question": repair_plan["question"],
                    "depends_on": [item.get("hop_id") for item in missing],
                    "purpose": "依据缺失链接补检（递归第 %d 轮）" % (round_index + 1),
                    "evidence": len(repair_local.get("evidence") or []), "added": added,
                    "status": "ok" if added else "empty",
                    "carry_terms": list(carry_terms),
                    "latency_ms": int((_time.monotonic() - repair_started) * 1000),
                })
                _record(receipts[-1], repair_local.get("evidence") or [], repair_local.get("stats"))
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
    return merged, receipts


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
            limit=_evidence_limit(retrieval_plan, request_payload.get("mode")),
        )
        # ── 多跳执行（阶段 9）──
        # 只对确有逻辑结构的问题生效（分解器给出的 DAG + 每跳 depends_on）；
        # 上一跳定位到的实体作为下一跳的过滤条件；受预算与跳数双重约束，
        # 超预算就停在做完的跳并明确标注降级（不静默给半截结论）。
        hop_receipts = []
        multi_hop_plan = retrieval_plan.get("decomposition") or {}
        if bool(getattr(config, "QA_MULTI_HOP_ENABLED", True)) and multi_hop_plan.get("is_multi_hop"):
            def _trace_recorder(entry):
                """阶段 10 + 阶段 01（F-7）：每跳写一条推理留痕（store 内部已吞异常）。

                `entry` 里的 route/results/accepted/rejected 由 `_run_multi_hop._record`
                填；老的 recorder 调用点不传这些键也不影响（`.get` + 默认值兜底）。
                """
                results = int(entry.get("results") or 0)
                accepted = int(entry.get("accepted") or 0)
                store.record_reasoning_trace(
                    run["id"],
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
                )

            local, hop_receipts = _run_multi_hop(
                article_retriever, retrieval_plan, local, context,
                pack_id=pack_id, limit=12, emit_stage_event=emit_stage_event,
                trace_recorder=_trace_recorder,
            )
        elif multi_hop_plan.get("hops"):
            hop_receipts = [{
                "hop_id": "h1", "question": _planned_question(context),
                "depends_on": [], "evidence": len(local.get("evidence") or []),
                "status": "single_hop", "carry_terms": [],
            }]
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
        "logic_validation": logic_validation,
        "level1_draft": level1_draft,
        "level2_retrieval": level2_retrieval,
        "level2_research": level2_research,
        "conflict_review": conflict_review,
        "synthesis": synthesis,
        "citation_validation": citation_validation,
    }


__all__ = ["build_qa_stage_handlers"]

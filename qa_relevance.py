#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""二级（RAGFlow 知识库）检索结果的相关性闸门。

为什么需要：二级检索是从知识库"按相似度捞片段"，知识库里没有这条问题的答案时，
照样会返回一堆"看起来最像"的片段（实测把财新付费墙样板文字、家族办公室、宠物健康
App 的片段当成具身智能的证据引用出来）。这类无关证据一旦进了综合阶段，会把答案带偏，
比不要二级结果更糟。

规则（两道闸，任一满足即保留）：
  ① 词面命中：证据的标题+正文里出现问题的实词/实体（默认命中 1 个即可）；
  ② 相似度：知识库自带的相似度分不低于 QA_LEVEL2_MIN_SCORE（默认 0.35）。
全部不满足 → 判定"二级无相关证据"，整批丢弃，只留一级（本地文章库）证据。
"""
from __future__ import annotations

import os
import re
from typing import Iterable, Mapping

from industry_packs import normalize_intel_text

# 二级检索里没有实义、出现也不算相关的词
_STOP_TERMS = {
    "什么", "哪些", "怎么", "如何", "为何", "为什么", "是否", "有没有", "最近", "最新",
    "进展", "情况", "动态", "消息", "新闻", "介绍", "说明", "分析", "总结", "请问",
    "相关", "方面", "主要", "目前", "现在", "以及", "还有", "这个", "那个", "哪些方面",
}
_SPLIT = re.compile(r"[^\w\u4e00-\u9fff]+")
# 中文没有空格：计划里抽不出实体时（问的是本包词表以外的东西），整句会被当成一个超长
# "词"而被丢掉，闸门等于没开——实测量子计算的问题会把家族办公室的片段按 0.55 的相似度
# 全放进来。所以这种情况下退化成"按字切 n 元组"当实词用。
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
# n 元组里出现这些字，说明它多半是虚词组合（"有哪些""最近的"），不能当实词
_FUNCTION_CHARS = set(
    "的了是有在和与及或吗呢吧啊哪什么怎样为何如此这那我你他她它们就都也还要会能可以"
    "对把被给让从到向于而并且但只更最很太再又已将该等一二三不没无个些种次年月日时点分"
    "多几来去上下里外中前后"
)
# 知识库片段里的噪声标记：命中这些说明片段不是正文（付费墙说明、导航、页脚等）
_NOISE_MARKERS = (
    "请务必在总结开头增加这段话", "本文由第三方AI", "不代表", "推荐点击链接阅读原文",
    "版权所有", "免责声明", "订阅", "登录后查看",
)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def min_term_hits() -> int:
    """至少要命中几个问题实词才算相关（QA_LEVEL2_MIN_TERM_HITS，默认 1）。"""
    return max(1, _env_int("QA_LEVEL2_MIN_TERM_HITS", 1))


def min_term_occurrences() -> int:
    """正文命中次数门槛（QA_LEVEL2_MIN_TERM_OCCURRENCES，默认 2）。

    经验：这类知识库的文档里常带"相关阅读/其它头条"清单，一个词在长正文里出现一次
    并不说明这篇文档在讲它。标题命中即算，正文则要求在前若干字符里出现至少这么多次。
    """
    return max(1, _env_int("QA_LEVEL2_MIN_TERM_OCCURRENCES", 2))


def lead_chars() -> int:
    """判正文相关性时只看开头多少字符（QA_LEVEL2_LEAD_CHARS，默认 1200）。"""
    return max(200, _env_int("QA_LEVEL2_LEAD_CHARS", 1200))


def min_similarity() -> float:
    """知识库相似度下限（QA_LEVEL2_MIN_SCORE，默认 0.35）。

    只在"问题抽不出实词"时作为唯一判据使用（否则词面命中才是主判据）。
    """
    return max(0.0, _env_float("QA_LEVEL2_MIN_SCORE", 0.35))


def strong_similarity() -> float:
    """强相似度（QA_LEVEL2_STRONG_SCORE，默认 0.85）：够高就认，不必词面命中。"""
    return max(0.0, _env_float("QA_LEVEL2_STRONG_SCORE", 0.85))


def _cjk_ngrams(text: str, sizes: tuple = (2, 3)) -> set:
    """按字切 n 元组当实词（只在抽不到实体时兜底用）。"""
    terms = set()
    for run in _CJK_RUN.findall(str(text or "")):
        for size in sizes:
            for index in range(0, len(run) - size + 1):
                gram = run[index:index + size]
                if any(char in _FUNCTION_CHARS for char in gram):
                    continue
                if gram in _STOP_TERMS:
                    continue
                terms.add(gram)
    return terms


def question_terms(question: str, plan: Mapping | None = None, *, allow_ngram_fallback: bool = True) -> set:
    """从问题 + 计划里抽"实词"：计划里的实体优先。

    注意两点经验：
    * 中文问题没有空格，整句会被当成一个"词"，而且检索片段的 match_reason 里往往
      会回显这句话，导致"每条都命中"——所以整句只在很短（<=8 字）时才当词用，
      中文实词主要来自计划里的 entities / queries。
    * 结果里不包含停用词与单字（单字命中太泛，等于没过滤）。
    * 计划里一个实词都抽不到时（问的是本包词表以外的东西），退回按字切 n 元组；
      这种词可信度低，调用方应把命中门槛抬高（见 filter_relevant_evidence）。
    """
    terms = set()

    def add(value) -> None:
        clean = str(value or "").strip()
        if not (2 <= len(clean) <= 16):
            return
        normalized = normalize_intel_text(clean)
        if not normalized or normalized in _STOP_TERMS:
            return
        terms.add(normalized)

    if isinstance(plan, Mapping):
        for entity in plan.get("entities") or []:
            add(entity)
        for anchor in (plan.get("policy_anchors") or {}).get("secondary_terms") or []:
            add(anchor)
        for query in plan.get("queries") or []:
            for token in _SPLIT.split(str(query or "")):
                add(token)
    for token in _SPLIT.split(str(question or "")):
        if len(token) <= 8:
            add(token)
    if not terms and allow_ngram_fallback:
        terms |= _cjk_ngrams(question)
    return terms


def _item_text(item: Mapping) -> str:
    """相关性只看证据本身的标题与正文。

    刻意不含 match_reason：那是检索时写的命中说明，常常回显问题原文，
    把它算进来会让每条证据都"命中"，闸门就失效了。
    """
    parts = [
        str(item.get("title") or ""),
        str(item.get("content_excerpt") or ""),
    ]
    return normalize_intel_text(" ".join(parts))


def evidence_hits(terms: Iterable[str], item: Mapping, *, title_only: bool = False) -> list:
    """该证据命中了哪些问题实词。

    标题命中即算；正文只在开头 lead_chars 内出现 >= min_term_occurrences 次才算，
    避免"相关阅读清单里提了一次"就被当成相关证据。
    `title_only` 用于兜底 n 元组词（可信度低）：只认标题命中。
    """
    title = normalize_intel_text(str(item.get("title") or ""))
    if title_only:
        return [term for term in terms if term and term in title]
    lead = normalize_intel_text(str(item.get("content_excerpt") or "")[:lead_chars()])
    need = min_term_occurrences()
    hits = []
    for term in terms:
        if not term:
            continue
        if term in title or lead.count(term) >= need:
            hits.append(term)
    return hits


def _noise_reason(item: Mapping) -> str:
    text = _item_text(item)
    for marker in _NOISE_MARKERS:
        if normalize_intel_text(marker) in text:
            return "kb_noise_fragment"
    return ""


def filter_relevant_evidence(
    question: str,
    evidence: list,
    *,
    plan: Mapping | None = None,
    min_hits: int | None = None,
    min_score: float | None = None,
) -> tuple:
    """二级证据相关性过滤，返回 (保留的证据, 审计信息)。

    审计信息带 `kept` / `dropped` / `reason` / `terms`，便于排查"为什么这次没走二级"。
    """
    plan_terms = question_terms(question, plan, allow_ngram_fallback=False)
    terms = plan_terms or question_terms(question, plan)
    # 兜底的 n 元组可信度低（"商业""订单"这种两字词在别的行业文章里也会出现），
    # 只认标题命中；计划给的是本包实体，正文前段多次命中也算。
    title_only = not plan_terms
    need_hits = min_term_hits() if min_hits is None else max(1, int(min_hits))
    score_floor = min_similarity() if min_score is None else float(min_score)
    strong_floor = strong_similarity()
    kept: list = []
    dropped: list = []
    for raw in evidence or []:
        item = dict(raw)
        noise = _noise_reason(item)
        hits = evidence_hits(terms, item, title_only=title_only)
        try:
            score = float(item.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        if noise:
            dropped.append({"evidence_ref": item.get("evidence_ref"), "title": item.get("title"),
                            "reason": noise, "term_hits": hits, "score": score})
            continue
        # 主判据：词面命中问题实词；抽不出实词时才退回相似度；相似度极高则豁免词面要求
        relevant = len(hits) >= need_hits
        if not relevant and not terms and score >= score_floor:
            relevant = True
        if not relevant and score >= strong_floor:
            relevant = True
        if relevant:
            item["relevance_hits"] = hits
            kept.append(item)
            continue
        dropped.append({"evidence_ref": item.get("evidence_ref"), "title": item.get("title"),
                        "reason": "no_question_term_overlap", "term_hits": hits, "score": score})
    audit = {
        "terms": sorted(terms)[:20],
        "term_source": "plan" if plan_terms else "ngram_fallback",
        "min_term_hits": need_hits,
        "min_score": score_floor,
        "strong_score": strong_floor,
        "kept": len(kept),
        "dropped": len(dropped),
        "reason": "" if kept else "level2_no_relevant_evidence",
        "excluded": dropped[:50],
        "samples": dropped[:5],
    }
    return kept, audit


__all__ = [
    "question_terms", "evidence_hits", "filter_relevant_evidence",
    "min_term_hits", "min_similarity",
]

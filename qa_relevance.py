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

import math
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
# 社媒/活动类噪声（取自服务器版 qa_pipeline 的 _RAG_NOISE_TERMS）：这类文章即便标题
# 里有本包词，也是活动花絮、点赞转发帖，不是可用证据。只看标题，避免正文里偶然出现
# "评论/转发"把正常文章误判成噪声。
_TITLE_NOISE_MARKERS = (
    "instagram", "facebook", "linkedin", "峰会", "论坛", "花絮", "精彩瞬间", "心情",
    "点赞", "转发", "评论", "活动回顾", "纳斯达克大屏", "获奖", "招聘",
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
    title = normalize_intel_text(str(item.get("title") or "")).casefold()
    for marker in _TITLE_NOISE_MARKERS:
        if marker in title:
            return "kb_social_or_event_noise"
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


# ── 一级（本地文章库 / BM25）检索的"离题闸门" ──────────────────────────────────
#
# 用户拍板（原话）："如果 BM25 查到的最近似的也离题，那就不要召回，不认为是答案。"
# 所以**这一层允许返回空证据包**：全离题 → 0 条 → 生成端如实说"无证据支持"，而不是把
# 离题材料包装成核心结论。（这是对"绝不返回空证据包"口径的**明确例外**，理由见下。）
#
# 为什么不能照抄二级闸门 `filter_relevant_evidence` 的口径：
#   · 二级是召回优先（命中 1 个实词就算相关），因为知识库片段短、词表窄；
#   · 一级是本地文章库全文，`融资/亿元/公司` 这类词到处都是，词面命中极其廉价。
#     实测（B 机两轮真实问答 run 0b047cc4e0db49078f24e75e44beb28d /
#     16d1eeb0425345c5882b0e9cca16e715，plan 逐字为 entities=[]、policy_anchors 全空、
#     queries=["最近融资近3亿元的公司是哪个公司"]）8 条一级证据的核心词覆盖率是
#     1.000 / 0.076 / 0.076 / 1.000 / 0.076 / 1.000 / 0.076 / 1.000：
#     **单靠覆盖率会留下 4 条离题的**（信托业三分类、财富管理降佣降费、美国家族信托、
#     商厦租赁成交）——闸门等于没开。决定性的是问题自带的**金额硬约束**：
#     只有"應天獲買家3億掃兩伙"那条的金额对得上（近3亿元），但它的窗口里没有"融资"，
#     所以按 `qa_retrieval._amount_constraint_hits` 的口径（金额必须与上下文词同窗）也判离题。
#     加金额硬约束以后 8 条全灭（实测 kept=0 / dropped=8）。
#
# 判据（三道，任一不满足即离题）：
#   ① 核心词 idf 覆盖率：coverage = Σ idf(t)·[t ∈ 证据] / Σ idf(t)，t 复用
#      `question_terms`（不另造词表）；idf 用**本证据包内**的 df（BM25 同款公式）。
#      阈值：≥ keep_floor(0.20) 保留；floor(0.12)–keep_floor 保留但标 weak_relevance；
#      < floor 丢弃。**阈值需实测校准**：默认值取自 B 机两轮真实问答的分布
#      （离题的落在 0.076，相关的落在 0.92–1.00），语料/词表变化后必须重新量。
#   ② 硬否决：只命中金额/数字字面（"亿元""3亿"），或整包一个核心词都没命中。
#   ③ 金额硬约束：问题带金额时（"近3亿元"），证据必须命中该金额且与核心词同窗
#      （沿用 `qa_retrieval._amount_constraint_hits` 口径）——数值问题的答案不可能
#      出现在对不上数字的材料里。可用 `QA_LEVEL1_AMOUNT_GATE=0` 单独关掉。
#   次级判据（主判据不可用时=只有 n-gram 兜底词，词本身不可信）：不同核心词命中
#      ≥ QA_LEVEL1_MIN_DISTINCT_HITS(2)，或语义分 ≥ QA_LEVEL1_SEMANTIC_STRONG(0.60)。
#      **语义分是独立量纲**：BM25 分（几十到上百）与余弦相似度（0–1）绝不共用一个阈值，
#      所以这里只对真带语义分的证据生效（`retrieval_method=semantic` 或 match_reason 里
#      的"语义相似度 x"），BM25 证据永远走词面判据。
#
# 豁免（绝不砍）：用户显式引用的页面上下文（page_context）、图谱事实（graph）、
#   联网结果（web）、政策元数据精确命中（policy_metadata_exact）——这些通道的"相关"
#   不靠词面重合，砍掉它们就是回归。
_LEVEL1_EXEMPT_SOURCE_TYPES = frozenset({"page_context", "graph", "web"})
_LEVEL1_EXEMPT_METHODS = frozenset({"page_context", "policy_metadata_exact", "policy_exact"})
_LEVEL1_EXEMPT_METHOD_PREFIXES = ("graph_",)
# 只是金额/数字字面、说明不了"问的是什么"的词
_AMOUNT_LITERALS = ("亿元", "亿", "万元", "万", "千元", "千", "元", "人民币", "美元",
                    "港元", "美金", "日元", "億", "萬")
_DIGITS_RE = re.compile(r"[0-9０-９.,%％]+")
# 长词/整句（计划里的 queries 常常就是整句）要先拆开：整句当词会让覆盖率恒为 0，
# 好证据也会被一起砍掉（实测 plan.queries 就是 15 字的整句问题）。
_LONG_TERM_CHARS = 8
_SENTENCE_CHARS_RE = re.compile(r"[。！？，、；：,.!?;:…\s]")
_SEMANTIC_IN_REASON = re.compile(r"语义相似度\s*([0-9]*\.?[0-9]+)")
_SEMANTIC_METHODS = frozenset({"semantic", "vector", "embedding"})
# 离题原因 → 人话（降级说明与运维报表共用同一份文案，避免两处不一致）
_OFFTOPIC_REASON_TEXT = {
    "no_core_term_hit": "与问题核心词零重合",
    "only_amount_literals": "只命中金额/数字字面",
    "amount_constraint_miss": "金额与问题对不上",
    "coverage_below_floor": "核心词覆盖率低于下限",
    "ngram_insufficient_hits": "命中核心词太少",
}


def offtopic_reason_text(reason_counts: Mapping | None) -> str:
    """把离题原因计数翻成人话（`"金额与问题对不上 3 条；…"`）。"""
    counts = dict(reason_counts or {})
    parts = []
    for code, value in sorted(counts.items(), key=lambda row: -row[1]):
        parts.append("%s %s 条" % (_OFFTOPIC_REASON_TEXT.get(str(code), str(code)), value))
    return "；".join(parts)


def level1_relevance_enabled() -> bool:
    """一级离题闸门总开关（QA_LEVEL1_RELEVANCE_GATE，默认开）。"""
    return _env_int("QA_LEVEL1_RELEVANCE_GATE", 1) != 0


def level1_coverage_floor() -> float:
    """丢弃线（QA_LEVEL1_RELEVANCE_FLOOR，默认 0.12）。**需实测校准**：见本节注释。"""
    return max(0.0, _env_float("QA_LEVEL1_RELEVANCE_FLOOR", 0.12))


def level1_coverage_keep() -> float:
    """保留线（QA_LEVEL1_RELEVANCE_KEEP，默认 0.20）。**需实测校准**：见本节注释。"""
    return max(0.0, _env_float("QA_LEVEL1_RELEVANCE_KEEP", 0.20))


def level1_semantic_strong() -> float:
    """次级判据里的语义强阈值（QA_LEVEL1_SEMANTIC_STRONG，默认 0.60）。

    **只作用于真带语义分的证据**（另一条量纲），BM25 分绝不拿它比。
    """
    return max(0.0, _env_float("QA_LEVEL1_SEMANTIC_STRONG", 0.60))


def level1_min_distinct_hits() -> int:
    """次级判据：n-gram 兜底词表下至少要命中几个不同核心词（默认 2）。"""
    return max(1, _env_int("QA_LEVEL1_MIN_DISTINCT_HITS", 2))


def level1_amount_gate() -> bool:
    """问题带金额时是否启用金额硬约束（QA_LEVEL1_AMOUNT_GATE，默认开）。"""
    return _env_int("QA_LEVEL1_AMOUNT_GATE", 1) != 0


def _term_is_amount_literal(term: str) -> bool:
    """词本身只是金额/数字字面（"亿元""3亿"）→ 不能当"相关"的证据。"""
    clean = normalize_intel_text(term)
    if not clean:
        return True
    stripped = clean
    for literal in _AMOUNT_LITERALS:
        stripped = stripped.replace(normalize_intel_text(literal), "")
    stripped = _DIGITS_RE.sub("", stripped)
    return not stripped.strip()


def _split_long_terms(term: str) -> list:
    """把长词/整句拆成核心词：复用检索器既有的 jieba + 停用词/目标词表。

    刻意**不新增词表**：分词与过滤都走 `qa_retrieval._tokenize_query`，与 BM25 通道
    用同一套口径。分词器不可用时退回既有 `_cjk_ngrams`（二级闸门同款兜底），
    这样闸门不会因为依赖缺失而静默消失。
    """
    tokens: list = []
    try:
        from qa_retrieval import _tokenize_query  # 局部导入：qa_retrieval 很重

        tokens = list(_tokenize_query(str(term or "")))
    except Exception:  # noqa: BLE001 —— 任何异常都走下面的字面兜底
        tokens = []
    if not tokens:
        tokens = sorted(_cjk_ngrams(str(term or ""), sizes=(2,)))
    cleaned = []
    for token in tokens:
        value = normalize_intel_text(token)
        if len(value) >= 2 and value not in cleaned:
            cleaned.append(value)
    return cleaned


def _core_terms(question: str, plan: Mapping | None) -> tuple[set, str, list]:
    """核心词 + 词表来源 + 被拆开的长词。

    词表**只来自** `question_terms`（计划实体/政策锚词/检索式/问题本身），长词用既有
    分词器拆开。返回 `(terms, term_source, split_from)`，term_source ∈
    {"plan", "question", "ngram_fallback"}，与二级闸门的审计口径保持一致。
    """
    strict = question_terms(question, plan, allow_ngram_fallback=False)
    if strict:
        plan_only = question_terms("", plan, allow_ngram_fallback=False) if isinstance(plan, Mapping) else set()
        source = "plan" if plan_only else "question"
        raw = strict
    else:
        # 计划里一个实词都抽不到 → n-gram 兜底词（可信度低，走次级判据）
        raw = question_terms(question, plan)
        source = "ngram_fallback"
    terms: set = set()
    split_from: list = []
    for term in raw:
        if len(term) > _LONG_TERM_CHARS or _SENTENCE_CHARS_RE.search(term):
            split_from.append(term)
            for token in _split_long_terms(term):
                terms.add(token)
            continue
        terms.add(term)
    return terms, source, sorted(split_from)[:5]


def _evidence_text(item: Mapping) -> str:
    """判据只看证据自身的标题 + 正文摘要（与二级闸门口径一致：绝不含 match_reason）。"""
    return normalize_intel_text("%s %s" % (item.get("title") or "", item.get("content_excerpt") or ""))


def _pack_idf(texts: list, terms: Iterable[str]) -> tuple[dict, dict]:
    """证据包内的 idf（BM25 同款公式，k1/b 无关）。

    为什么用包内 df：本层拿不到检索器的全库 df（那是候选池内部的量），而包内 df 在
    同一次检索里**可复算**。df=0 的词（整包一条都没命中）不进权重：它对包内没有任何
    区分度，留在分母里只会把所有覆盖率整体压低——好证据也会被一起砍掉。
    """
    total = max(1, len(texts))
    df = {term: 0 for term in terms}
    for text in texts:
        for term in df:
            if term and term in text:
                df[term] += 1
    weights = {}
    for term, count in df.items():
        if count <= 0:
            continue
        weights[term] = math.log(1.0 + (total - count + 0.5) / (count + 0.5))
    return weights, df


def _semantic_score(item: Mapping) -> float:
    """证据自带的语义分（0–1）；没有就返回 0（**绝不用 BM25 分顶替**）。"""
    method = str(item.get("retrieval_method") or "").casefold()
    try:
        score = float(item.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    if method in _SEMANTIC_METHODS:
        # 语义 Hunter 的证据分就是余弦×100（见 qa_hunters 的 method="semantic"）
        return max(0.0, min(1.0, score / 100.0))
    match = _SEMANTIC_IN_REASON.search(str(item.get("match_reason") or ""))
    if match:
        try:
            return max(0.0, min(1.0, float(match.group(1))))
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _exempt_reason(item: Mapping) -> str:
    """这些通道的"相关"不靠词面重合，本层一律放行。"""
    source_type = str(item.get("source_type") or "")
    method = str(item.get("retrieval_method") or "")
    if source_type in _LEVEL1_EXEMPT_SOURCE_TYPES:
        return source_type
    if method in _LEVEL1_EXEMPT_METHODS:
        return method
    for prefix in _LEVEL1_EXEMPT_METHOD_PREFIXES:
        if method.startswith(prefix):
            return prefix.rstrip("_")
    return ""


def _amount_tools() -> tuple:
    """懒加载 `qa_retrieval` 的金额判据（沿用既有口径，不重写一套）。"""
    try:
        from qa_retrieval import _amount_constraint_hits, _amount_constraints, _amount_context_terms

        return _amount_constraints, _amount_context_terms, _amount_constraint_hits
    except Exception:  # noqa: BLE001 —— 取不到就不启用金额硬约束（不误砍）
        return None, None, None


def filter_offtopic_evidence(
    question: str,
    evidence: list,
    *,
    plan: Mapping | None = None,
    limit: int | None = None,
) -> tuple:
    """一级证据离题闸门：**允许返回空证据包**，返回 (保留的证据, 审计)。

    审计里 `kept` / `dropped` / `floor` / `keep_floor` / `coverage_of_top1` /
    `term_source` 是生效值回执（调用方必须落进阶段 stats，否则无法证伪）。
    证据条目本身**不加顶层键**（冻结的 `EVIDENCE_SCHEMA.additionalProperties=False`）：
    weak 标记与覆盖率写进 `metadata.evidence_relevance`。
    """
    items = [dict(item) for item in (evidence or []) if isinstance(item, Mapping)]
    floor = level1_coverage_floor()
    keep_floor = level1_coverage_keep()
    audit = {
        "gate": "applied", "reason": "", "kept": 0, "dropped": 0, "weak": 0, "exempt": 0,
        "floor": floor, "keep_floor": keep_floor,
        "term_source": "", "terms": [], "terms_split_from": [],
        "coverage_of_top1": 0.0, "top1_ref": "", "coverage_max": 0.0,
        "amount_gate": "off", "amount_constraints": [],
        "excluded": [], "weak_evidence": [], "reason_counts": {},
    }

    def _cap(kept: list) -> list:
        if limit is None:
            return kept
        try:
            return kept[: max(0, int(limit))]
        except (TypeError, ValueError):
            return kept

    if not items:
        audit["reason"] = "no_evidence"
        return [], audit
    if not level1_relevance_enabled():
        # 关掉闸门 = 逐字回到改动前的行为（回滚口径），统计里如实写明
        audit.update({"gate": "disabled", "reason": "QA_LEVEL1_RELEVANCE_GATE=0", "kept": len(items)})
        return _cap(items), audit

    terms, term_source, split_from = _core_terms(question, plan)
    audit["term_source"] = term_source
    audit["terms"] = sorted(terms)[:20]
    audit["terms_split_from"] = split_from
    if not terms:
        # 问句与计划都为空：没有任何判据可用。注意与二级闸门 `not_applicable` 的区别——
        # 这里**不会**因为"抽不出实词"就放行：抽不出实词时会走 n-gram 兜底（上面的
        # term_source=ngram_fallback），那时走的是次级判据，闸门照旧生效。
        audit.update({"gate": "not_applicable", "reason": "no_core_terms", "kept": len(items)})
        return _cap(items), audit

    texts = [_evidence_text(item) for item in items]
    weights, _df = _pack_idf(texts, terms)
    coverage_total = sum(weights.values())
    audit["idf_terms"] = {term: round(value, 4) for term, value in sorted(weights.items())[:20]}

    # 金额硬约束（问题里出现金额时）：口径与检索器完全一致
    _constraints_fn, _context_fn, _hits_fn = _amount_tools()
    queries = [str(item) for item in (plan or {}).get("queries") or [] if str(item).strip()] \
        if isinstance(plan, Mapping) else []
    if not queries:
        queries = [str(question or "")]
    constraints: list = []
    context_terms: list = []
    if _constraints_fn and _context_fn and _hits_fn:
        context_terms = list(_context_fn(queries))
        for query in queries:
            for item in _constraints_fn(query):
                key = (round(float(item.get("value_yi") or 0), 6), bool(item.get("approximate")))
                if not any((round(float(existing.get("value_yi") or 0), 6),
                            bool(existing.get("approximate"))) == key for existing in constraints):
                    constraints.append(item)
        constraints = constraints[:8]
    amount_active = bool(constraints) and level1_amount_gate()
    audit["amount_constraints"] = [dict(item) for item in constraints]
    audit["amount_context_terms"] = context_terms[:12]
    audit["amount_gate"] = "applied" if amount_active else ("disabled" if constraints else "off")

    need_hits = level1_min_distinct_hits()
    strong = level1_semantic_strong()
    kept: list = []
    weak_evidence: list = []
    dropped: list = []
    reason_counts: dict = {}
    # `coverage_of_top1` = **分数最高的那条非豁免证据**（"BM25 查到的最近似的"）的覆盖率；
    # `coverage_max` = 包内最高覆盖率。两个都报，避免"第一"这个词的歧义。
    top1 = (-1.0, 0.0, "")
    coverage_max = 0.0

    def _drop(item: Mapping, reason: str, coverage: float, hits: list) -> None:
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        dropped.append({
            "evidence_ref": item.get("evidence_ref"), "title": str(item.get("title") or "")[:120],
            "reason": reason, "coverage": round(coverage, 6), "term_hits": hits[:10],
        })

    def _mark(entry: dict, record: dict) -> None:
        """标注写进 `metadata.evidence_relevance`：顶层键集冻结（additionalProperties=False），
        且这里刻意换一个新的 metadata 字典，绝不改动调用方传进来的证据对象。"""
        metadata = entry.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
        metadata["evidence_relevance"] = record
        entry["metadata"] = metadata

    for item, text in zip(items, texts):
        exempt = _exempt_reason(item)
        hits = sorted(term for term, weight in weights.items() if weight and term in text)
        coverage = (sum(weights[term] for term in hits) / coverage_total) if coverage_total else 0.0
        content_hits = [term for term in hits if not _term_is_amount_literal(term)]
        coverage_max = max(coverage_max, coverage)
        try:
            _score = float(item.get("score") or 0.0)
        except (TypeError, ValueError):
            _score = 0.0
        if not exempt and _score > top1[0]:
            top1 = (_score, coverage, str(item.get("evidence_ref") or ""))
        amount_hits: list = []
        if amount_active and _hits_fn:
            amount_hits = list(_hits_fn(constraints, text, context_terms=context_terms))
        record = {
            "coverage": round(coverage, 6), "term_hits": hits[:10],
            "amount_hits": amount_hits[:4], "gate": "level1-offtopic-v1",
        }
        reason = ""
        if exempt:
            # 豁免通道：不判、不砍（用户显式引用 / 图谱事实 / 联网 / 政策元数据精确命中）
            record["exempt"] = exempt
            audit["exempt"] += 1
        elif not hits:
            reason = "no_core_term_hit"
        elif not content_hits:
            # 判据②：只命中金额/数字字面（"應天獲買家3億掃兩伙"就是这种）
            reason = "only_amount_literals"
        elif amount_active and not amount_hits:
            # 判据③：数值问题里"数字对不上"就不可能承载答案
            reason = "amount_constraint_miss"
        elif term_source == "ngram_fallback":
            # 次级判据（主判据不可用：词表是 n-gram 兜底，词本身不可信）
            semantic = _semantic_score(item)
            record["semantic"] = round(semantic, 4)
            if not (len(content_hits) >= need_hits or (semantic and semantic >= strong)):
                reason = "ngram_insufficient_hits"
        elif coverage >= keep_floor:
            record["weak"] = False
        elif coverage >= floor:
            # 弱相关档：**保留**并标注（不许当成丢弃），由上层转成 degradation 说明
            record["weak"] = True
        else:
            reason = "coverage_below_floor"
        if reason:
            record["reason"] = reason
            _drop(item, reason, coverage, hits)
        else:
            if record.get("weak"):
                weak_evidence.append({"evidence_ref": item.get("evidence_ref"),
                                      "coverage": round(coverage, 6)})
            kept.append(item)
        _mark(item, record)

    audit.update({
        "kept": len(kept), "dropped": len(dropped), "weak": len(weak_evidence),
        "coverage_of_top1": round(float(top1[1]), 6), "top1_ref": top1[2],
        "coverage_max": round(float(coverage_max), 6),
        "weak_evidence": weak_evidence[:20], "excluded": dropped[:50],
        "reason_counts": dict(sorted(reason_counts.items(), key=lambda row: -row[1])),
        "reason": "level1_all_offtopic" if not kept else "",
    })
    return _cap(kept), audit


__all__ = [
    "question_terms", "evidence_hits", "filter_relevant_evidence",
    "min_term_hits", "min_similarity",
    "filter_offtopic_evidence", "level1_coverage_floor", "level1_coverage_keep",
    "offtopic_reason_text",
]

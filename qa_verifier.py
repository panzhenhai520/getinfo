#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""证据核验层（graph-rag-v2 通用包 Phase 03 · P03-01…P03-04）。

为什么要有这个文件：通用包 `01_V2_ARCHITECTURE` §10 要求"候选证据验证后才能入图"，
§2.6 要求 Verifier 放在**边上**（Retriever → Candidate Evidence → Verifier → ACCEPT/REJECT），
§11 给了 EvidenceScore 的可解释加权口径；MASTER_RULES 第 11 条更是硬要求：
**LLM 自由生成的内容不能直接成为"已验证证据"**。本仓库现状（Phase 02 交付后的对齐结论）：
  · 证据只有"相关性闸门"（`qa_relevance`），没有 entailment / 实体 / 时间 / 否定 / 来源核验；
  · 证据层 `status` 只是 `relationship` 的规范化映射，**没有结论**（Phase 02 明确留给本阶段）；
  · 一级草稿的 claim 上 `verification_status` 由**模型自己填**（真机实测全部是 `qualified`），
    等于让模型给自己打分——这正是第 11 条要禁止的事。

本模块只做四件事，且**只加不改**：
  · P03-01 relevance/reranker：把"问题实词 → 证据"的覆盖度算成 [0,1] 相关性并按分重排；
  · P03-02 entailment/NLI：**纯规则/统计**的方向性蕴含判定（claim 被证据覆盖的比例），
            提供可插拔后端接口；后端缺失/抛错一律保守回落到规则后端并降级，**绝不默认 SUPPORTED**；
  · P03-03 entity/time/negation/source：实体一致性、时间适用性、否定翻转、来源质量
            （二手转述 / 因果夸大 / 只有关键词相似）；
  · P03-04 EvidenceScore/reason/cache：可解释加权分（§11 口径）、机器可读拒绝原因码 + 中文解释、
            核验结果缓存（进程内 TTL+LRU，可选落 `qa_retrieval_cache` 复用既有表，**不升 schema**）。

硬约束（不许违反）：
  1. **绝不调用任何模型/嵌入端点**：本模块只依赖标准库与仓库内的纯文本工具，
     不 import requests/urllib/socket 等任何网络设施（有源码级守门用例钉死）；
     可插拔 NLI 后端只能由**进程内注册**的本地函数提供，`QA_NLI_BACKEND` 只认已注册的名字，
     未注册/抛错一律回落到规则后端。
  2. 不许放宽任何既有 schema：核验结果全部落在 `metadata.evidence_layer.verification`
     （`EVIDENCE_SCHEMA` 放行的 `metadata` 对象内），证据条目顶层键集不变；
     claim 级结论写进**既有字段** `claim.verification_status`（`CLAIM_SCHEMA` 的枚举里
     本来就有 confirmed/qualified/conflicted/unverified/insufficient_evidence，一个字不改）。
  3. 证据不足时**必须**给 UNVERIFIED/QUALIFIED，绝不给 SUPPORTED。判定顺序是先立"否决项"
     （否定翻转 / 数字不符 / 实体不符 / 覆盖度不足），再谈支持。
  4. 任何异常都不打断问答：`verify_evidence_batch` 出错时原样返回证据 + 审计里写原因。

配置化参数（都有默认值；关掉即回到 Phase 02 行为，可随时回滚）：
  · QA_VERIFIER_ENABLED          默认 1：关掉则完全不做核验（回执键集也与 Phase 02 逐字相同）；
  · QA_VERIFIER_GATE             默认 refuted：证据闸门强度 off / refuted / unverified；
  · QA_VERIFIER_RERANK           默认 1：按核验分重排（P03-01 的 reranker）；
  · QA_VERIFIER_SUPPORT_MIN      默认 0.34：判 SUPPORTED 的覆盖度下限；
  · QA_VERIFIER_PARTIAL_MIN      默认 0.18：判 QUALIFIED 的覆盖度下限；
  · QA_VERIFIER_RELEVANCE_FLOOR  默认 0.10：`unverified` 闸门下"明显不相关"的判据；
  · QA_VERIFIER_NLI_CHARS        默认 800：参与蕴含判定的证据文本上限；
  · QA_VERIFIER_FRESHNESS_HALFLIFE_DAYS 默认 180：时效半衰期；
  · QA_VERIFIER_W_*              默认见 `DEFAULT_WEIGHTS`：EvidenceScore 各项权重；
  · QA_VERIFIER_CACHE_ENABLED    默认 1；QA_VERIFIER_CACHE_TTL_SECONDS 默认 900；
    QA_VERIFIER_CACHE_MAX 默认 512；QA_VERIFIER_CACHE_PERSIST 默认 1（store 支持时落库）。
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from datetime import datetime, timezone
from functools import lru_cache
from typing import Callable, Iterable, Mapping

from industry_packs import normalize_intel_text
from qa_evidence import evidence_object, source_identity
from qa_graph_contracts import (
    EVIDENCE_STATUS_CONTEXT,
    EVIDENCE_STATUS_QUALIFIED,
    EVIDENCE_STATUS_REFUTED,
    EVIDENCE_STATUS_SUPPORTED,
    EVIDENCE_STATUS_UNVERIFIED,
    EVIDENCE_STATUSES,
)

# ── 版本 ──────────────────────────────────────────────────────────────────────
VERIFIER_VERSION = "qa-verifier-v1"
"""核验规则版本。改了判定口径/阈值/原因码语义就必须升版本——缓存键含它，
升版本等于让旧缓存自然失效（MASTER_RULES 第 9 条：实验与 trace 不覆盖历史）。"""

CACHE_NAMESPACE = "qa_verification"
"""核验缓存在 `qa_retrieval_cache` 里的 namespace（复用既有表，不新建表）。"""

# ── 证据结论（复用合同一事实源，不新造枚举）──────────────────────────────────
VERDICT_SUPPORTED = EVIDENCE_STATUS_SUPPORTED
VERDICT_QUALIFIED = EVIDENCE_STATUS_QUALIFIED
VERDICT_REFUTED = EVIDENCE_STATUS_REFUTED
VERDICT_CONTEXT = EVIDENCE_STATUS_CONTEXT
VERDICT_UNVERIFIED = EVIDENCE_STATUS_UNVERIFIED
VERDICTS = EVIDENCE_STATUSES
"""核验结论取值域 = `EVIDENCE_STATUSES`（单一事实源，不新增枚举值）。"""

# ── 拒绝/降级原因码（机器可读；中文解释见 REASON_TEXTS）──────────────────────
REASON_NO_CLAIM = "no_claim"
REASON_NO_SPAN = "no_span"
REASON_SUPPORTED = "supported_by_span"
REASON_PARTIAL = "partially_supported"
REASON_LOW_OVERLAP = "insufficient_overlap"
REASON_KEYWORD_ONLY = "keyword_only_similarity"
REASON_NEGATION_FLIP = "negation_flip"
REASON_NEGATION_UNSCOPED = "negation_flip_unscoped"
REASON_NUMBER_MISMATCH = "number_mismatch"
REASON_ENTITY_MISSING = "entity_not_in_evidence"
REASON_TIME_PRECEDES = "evidence_before_claim"
REASON_TIME_AFTER_WINDOW = "evidence_after_window"
REASON_TIME_UNKNOWN = "time_unknown"
REASON_SECOND_HAND = "second_hand_relay"
REASON_CAUSAL_INFLATION = "causal_inflation"
REASON_LOW_AUTHORITY = "low_authority_source"
REASON_AUTHORITY_MISSING = "authority_missing"
REASON_CONTRADICT_RELATION = "relationship_contradicts"
REASON_BACKEND_FALLBACK = "nli_backend_fallback"
REASON_MISSING_EVIDENCE = "claim_evidence_missing"
REASON_CLAIM_NO_EVIDENCE = "claim_without_evidence"

VERIFICATION_REASONS = (
    REASON_NO_CLAIM, REASON_NO_SPAN, REASON_SUPPORTED, REASON_PARTIAL, REASON_LOW_OVERLAP,
    REASON_KEYWORD_ONLY, REASON_NEGATION_FLIP, REASON_NUMBER_MISMATCH, REASON_ENTITY_MISSING,
    REASON_NEGATION_UNSCOPED, REASON_TIME_PRECEDES, REASON_TIME_AFTER_WINDOW, REASON_TIME_UNKNOWN,
    REASON_SECOND_HAND, REASON_CAUSAL_INFLATION, REASON_LOW_AUTHORITY, REASON_AUTHORITY_MISSING,
    REASON_CONTRADICT_RELATION, REASON_BACKEND_FALLBACK, REASON_MISSING_EVIDENCE,
    REASON_CLAIM_NO_EVIDENCE,
)
"""原因码取值域：新增原因必须在这里登记（阶段 16 的看板按它统计）。"""

REASON_TEXTS = {
    REASON_NO_CLAIM: "没有可比对的结论（claim）文本，无法核验。",
    REASON_NO_SPAN: "证据没有可引用的正文片段，无法核验。",
    REASON_SUPPORTED: "结论的关键实词在证据片段里成段出现，判为支持。",
    REASON_PARTIAL: "证据只覆盖了结论的一部分，判为部分支持（降级）。",
    REASON_LOW_OVERLAP: "证据与结论的实词重合度过低，判为未核验（不是支持）。",
    REASON_KEYWORD_ONLY: "只有标题/关键词撞上，正文没有对应内容：属关键词相似，不是支持。",
    REASON_NEGATION_FLIP: "结论与证据的否定极性相反，属反证。",
    REASON_NEGATION_UNSCOPED: "证据里的否定词说的不是结论那件事，不判反证，只降为未核验。",
    REASON_NUMBER_MISMATCH: "结论里的数字/期限在证据里找不到，判为未核验。",
    REASON_ENTITY_MISSING: "结论要求的实体没有出现在证据里，判为未核验。",
    REASON_TIME_PRECEDES: "证据发布时间早于结论生效时间，不能用来支持该结论。",
    REASON_TIME_AFTER_WINDOW: "证据发布时间晚于结论有效期（含问题给的时间窗）。",
    REASON_TIME_UNKNOWN: "证据没有可用时间，时效按中性处理。",
    REASON_SECOND_HAND: "证据是二手转述/汇编（不是原文），最多只能算部分支持。",
    REASON_CAUSAL_INFLATION: "结论写了因果关系，但证据里没有因果表述：不能把相关当因果。",
    REASON_LOW_AUTHORITY: "来源权威性偏低，支撑力打折。",
    REASON_AUTHORITY_MISSING: "来源没有权威性标注，按中性偏低处理。",
    REASON_CONTRADICT_RELATION: "证据自带的 relationship 就是 contradicts。",
    REASON_BACKEND_FALLBACK: "NLI 后端不可用，已保守回落到规则判定。",
    REASON_MISSING_EVIDENCE: "结论引用的证据不存在（引用悬空）。",
    REASON_CLAIM_NO_EVIDENCE: "结论没有引用任何证据（模型生成时未给出处）。",
}


def reason_text(reasons: Iterable) -> str:
    """原因码 → 中文解释（拼接、去重、截断；给用户看的可解释性文字）。"""
    texts = []
    for code in reasons or ():
        text = REASON_TEXTS.get(str(code))
        if text and text not in texts:
            texts.append(text)
    return "；".join(texts)[:400]


# ── 配置 ──────────────────────────────────────────────────────────────────────
DEFAULT_WEIGHTS = {
    "relevance": 0.20,
    "entailment": 0.36,
    "source_quality": 0.18,
    "freshness": 0.12,
    "independence": 0.14,
}
"""EvidenceScore 五项权重（和为 1.0；`contradiction_risk` 是减项，权重见
`contradiction_weight`）。与 01_V2_ARCHITECTURE §11 的
`wr×Relevance + we×Entailment + ws×SourceQuality + wf×Freshness + wi×Independence
 − wc×ContradictionRisk` 一一对应。"""

_GATE_MODES = ("off", "refuted", "unverified")
_CAUSAL_MARKERS = ("导致", "因为", "由于", "引起", "造成", "使得", "推动", "带动", "驱动", "原因是", "因此")
_SECOND_HAND_MARKERS = ("转载", "转述", "汇编", "综合报道", "据媒体报道", "整理自", "摘编")
_SECOND_HAND_ROLES = {"ai_qa_summary", "aggregator", "repost", "digest", "secondary"}
# 否定词口径：只认**多字、语义明确**的否定短语。刻意不收单字"未/无/非"——
# 实测（A 机真实 run 回放）"未来/无锡/无法/非洲"这类词会把整篇证据判成含否定，
# 于是一整批真支持的结论被误判成反证（8 条 claim 里 6 条 conflicted）。
# 在 `qa_reasoning._NEGATION_RE` 的词表基础上补了 不属于/不存在/未能/不予 等写法；
# 既有词表是本表的子集（守门用例钉死），所以只会更精确、不会更宽松。
_NEGATION_RE = re.compile(
    r"不适用|不适于|不包括|不包含|不属于|不存在|不符合|不得超过|不得|不允许|不支持|不具备|不构成|"
    r"没有|并非|无须|无需|禁止|未生效|尚未|暂无|不再|取消|否认|拒绝|"
    r"不会|不能|不是|不予|未能|未获|未达到|未披露|无效果|无效|无改善|无变化|"
    r"not\s|no\s|did not|does not",
    re.I,
)# 句子切分：否定与因果这类"局部语义"必须在**与结论最相关的那一句**上判，
# 不能拿整篇 900 字证据去搜关键词（长文里出现一次 未来/无法 就整篇当成反证）。
# 逗号也算边界：中文长句里"，逾期不予受理"这种程序性子句与主句是两个命题，
# 合在一起判会把"主句支持结论"误判成反证（实测踩到，见 g02 用例）。
_SENT_SPLIT = re.compile(r"[。！？；!?;，,\n\r]+")
# 数字/期限口径：与 `qa_reasoning._NUMBER_RE` 同写法，**只改一处**——前视断言排的是
# 半角字母数字而不是 `\w`。原因：Python 的 `\w` 含中日韩汉字，`(?<![\w.])\d` 会让
# "提高到30%" 里的 30 匹配不上（前面是汉字"到"），于是"结论说 30%、证据说 20%"
# 这种最典型的数字冲突**完全查不出来**（实测踩到）。汉字紧邻数字是中文的常态，
# 不能当词边界。差异由 tests/test_qa_phase03_verifier.py 的守门用例钉死。
_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9_.])\d+(?:\.\d+)?\s*(?:%|亿元|万元|元|天|年|个月|月|日|人|项|次|家)?")
# 日期写法统一：`2026-10-09` / `2026/10/09` 都折成 `2026年10月9日`，否则同一份文件里
# "结论写 2026年10月9日、证据写 2026-10-09" 会被当成数字不符（假阴性：把支持降级）。
_ISO_DATE_RE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")


def _number_tokens(text) -> set:
    """文本里的数字/期限标记（日期写法统一 + 去掉数字与单位之间的空白）。"""
    normalized = _ISO_DATE_RE.sub(
        lambda match: "%s年%s月%s日" % (match.group(1), int(match.group(2)), int(match.group(3))),
        _normalize(text))
    return {re.sub(r"\s+", "", token) for token in _NUMBER_RE.findall(normalized)}
_ASCII_WORD = re.compile(r"[a-z][a-z0-9_.\-]{1,}")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
_FUNCTION_CHARS = set(
    "的了是有在和与及或吗呢吧啊哪什么怎样为何如此这那我你他她它们就都也还要会能可以"
    "对把被给让从到向于而并且但只更最很太再又已将该等一二三不没无个些种次年月日时点分"
    "多几来去上下里外中前后"
)
_STOP_TERMS = {
    "什么", "哪些", "怎么", "如何", "为何", "为什么", "是否", "有没有", "最近", "最新",
    "进展", "情况", "动态", "消息", "新闻", "介绍", "说明", "分析", "总结", "请问",
    "相关", "方面", "主要", "目前", "现在", "以及", "还有", "这个", "那个",
}
# 短证据/短结论不足以判支持：实词太少时判 SUPPORTED 等于没判
_MIN_TERMS_FOR_SUPPORT = 3

_verdict_rank = {
    VERDICT_REFUTED: 0, VERDICT_UNVERIFIED: 1, VERDICT_CONTEXT: 2,
    VERDICT_QUALIFIED: 3, VERDICT_SUPPORTED: 4,
}


def _downgrade(current: str, ceiling: str) -> str:
    """把结论降到不超过 `ceiling`（只降不升）：否决项生效时用。"""
    if _verdict_rank.get(str(current), 1) > _verdict_rank.get(str(ceiling), 1):
        return ceiling
    return current


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().casefold() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(str(os.getenv(name, str(default))).strip())
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(str(os.getenv(name, str(default))).strip())
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def verifier_enabled() -> bool:
    """核验层总开关（QA_VERIFIER_ENABLED，默认开）。关掉 = 完全回到 Phase 02 行为。"""
    return _env_flag("QA_VERIFIER_ENABLED", True)


def gate_mode() -> str:
    """证据闸门强度（QA_VERIFIER_GATE）：

    · `refuted`（默认）：只丢**有反证**的证据（否定翻转）。它们进证据包只会把答案带偏；
    · `unverified`：连"未核验且几乎不相关"的也丢（更省上下文，但更激进）；
    · `off`：只标注不丢（观察期用）。
    """
    raw = str(os.getenv("QA_VERIFIER_GATE", "refuted") or "").strip().casefold()
    return raw if raw in _GATE_MODES else "refuted"


def rerank_enabled() -> bool:
    """是否按核验分重排证据（QA_VERIFIER_RERANK，默认开）——P03-01 的 reranker。"""
    return _env_flag("QA_VERIFIER_RERANK", True)


def support_min() -> float:
    """判 SUPPORTED 的覆盖度下限（QA_VERIFIER_SUPPORT_MIN，默认 0.34）。"""
    return _env_float("QA_VERIFIER_SUPPORT_MIN", 0.34, 0.05, 1.0)


def partial_min() -> float:
    """判 QUALIFIED 的覆盖度下限（QA_VERIFIER_PARTIAL_MIN，默认 0.18）。"""
    return _env_float("QA_VERIFIER_PARTIAL_MIN", 0.18, 0.0, 1.0)


def relevance_floor() -> float:
    """"明显不相关"的相关性判据（QA_VERIFIER_RELEVANCE_FLOOR，默认 0.10）。"""
    return _env_float("QA_VERIFIER_RELEVANCE_FLOOR", 0.10, 0.0, 1.0)


def nli_chars() -> int:
    """参与蕴含判定的证据文本上限（QA_VERIFIER_NLI_CHARS，默认 800，钳制 200…4000）。"""
    return _env_int("QA_VERIFIER_NLI_CHARS", 800, 200, 4000)


def freshness_half_life_days() -> float:
    """时效半衰期（QA_VERIFIER_FRESHNESS_HALFLIFE_DAYS，默认 180 天）。"""
    return _env_float("QA_VERIFIER_FRESHNESS_HALFLIFE_DAYS", 180.0, 1.0, 3650.0)


def contradiction_weight() -> float:
    """矛盾风险减项权重（QA_VERIFIER_W_CONTRADICTION，默认 0.30）。"""
    return _env_float("QA_VERIFIER_W_CONTRADICTION", 0.30, 0.0, 1.0)


def score_weights() -> dict:
    """EvidenceScore 权重（逐项可用 `QA_VERIFIER_W_<键大写>` 覆盖，与既有权重表同风格）。"""
    weights = dict(DEFAULT_WEIGHTS)
    for key in list(weights):
        raw = os.getenv("QA_VERIFIER_W_%s" % key.upper())
        if raw is None or str(raw).strip() == "":
            continue
        try:
            weights[key] = float(str(raw).strip())
        except (TypeError, ValueError):
            continue
    return weights


def cache_enabled() -> bool:
    """核验缓存开关（QA_VERIFIER_CACHE_ENABLED，默认开）。"""
    return _env_flag("QA_VERIFIER_CACHE_ENABLED", True)


def cache_persist_enabled() -> bool:
    """核验缓存是否落库（QA_VERIFIER_CACHE_PERSIST，默认开；store 不支持时自动只走内存）。"""
    return _env_flag("QA_VERIFIER_CACHE_PERSIST", True)


def cache_ttl_seconds() -> int:
    """核验缓存 TTL（QA_VERIFIER_CACHE_TTL_SECONDS，默认 900，钳制 30…86400）。"""
    return _env_int("QA_VERIFIER_CACHE_TTL_SECONDS", 900, 30, 86400)


def cache_max_entries() -> int:
    """进程内核验缓存条数上限（QA_VERIFIER_CACHE_MAX，默认 512，钳制 16…20000）。"""
    return _env_int("QA_VERIFIER_CACHE_MAX", 512, 16, 20000)


def config_hash() -> str:
    """核验配置指纹（权重 + 阈值 + 版本）：进缓存键与日志，便于"同配置稳定、换配置重算"。

    12 位十六进制，与 `qa_runs.config_hash` 12 位口径一致（同风格，不混用同一列）。
    """
    body = "|".join(["%s=%s" % (key, value) for key, value in sorted(score_weights().items())])
    body += "|support_min=%.4f|partial_min=%.4f|relevance_floor=%.4f|contradiction=%.4f|halflife=%.1f" % (
        support_min(), partial_min(), relevance_floor(), contradiction_weight(),
        freshness_half_life_days())
    return hashlib.sha256(("%s|%s" % (VERIFIER_VERSION, body)).encode("utf-8")).hexdigest()[:12]


# ── 文本与度量基元 ────────────────────────────────────────────────────────────

@lru_cache(maxsize=1024)
def _normalize(text: str) -> str:
    """`industry_packs.normalize_intel_text` 的**本模块缓存包装**（纯函数，行为一字不改）。

    为什么要缓存：那里面用 OpenCC 做繁简转换，实测占核验耗时的 80%（738 次调用 0.73s）。
    核验会在一条证据上反复归一化同一段文本（选文本 / 判否定 / 判蕴含 / 判相关性），
    缓存后同一文本只转一次。不改 `industry_packs` 本身（别的模块的调用行为零影响）。
    """
    return normalize_intel_text(text)


@lru_cache(maxsize=1024)
def _term_set_cached(text: str, sizes: tuple) -> frozenset:
    """`term_set` 的实际计算（带缓存）。

    为什么缓存：一条 900 字的证据在核验里要被切 8~10 次实词表（选文本、判否定、
    判蕴含、判相关性），每次都重跑正则与 n 元组生成——实测单条 33ms，12 条证据就是 0.4s。
    纯函数 + 有界 LRU，不改变任何判定结果。返回 frozenset：缓存对象绝不允许被调用方改写。
    """
    normalized = _normalize(text)
    terms: set = set()
    for word in _ASCII_WORD.findall(normalized):
        if len(word) >= 2 and word not in _STOP_TERMS:
            terms.add(word)
    for run in _CJK_RUN.findall(normalized):
        for size in sizes:
            for index in range(0, len(run) - size + 1):
                gram = run[index:index + size]
                if any(char in _FUNCTION_CHARS for char in gram):
                    continue
                if gram in _STOP_TERMS:
                    continue
                terms.add(gram)
    return frozenset(terms)


def term_set(text, *, sizes: tuple = (2, 3)) -> set:
    """文本实词集合：ASCII 词 + 中文 n 元组（滤掉虚词组合与停用词）。

    与 `qa_relevance.question_terms` 同口径（同一份 `_FUNCTION_CHARS`/停用词表）：
    中文没有空格，抽不出词时整句会被当成一个超长词，所以必须按字切 n 元组。
    返回可变副本，缓存里存的是不可变版本（见 `_term_set_cached`）。
    """
    return set(_term_set_cached(str(text or ""), tuple(sizes)))


def overlap_stats(claim_terms: Iterable, span_terms: Iterable) -> dict:
    """方向性覆盖度：claim 的实词有多少落进证据（coverage），以及证据侧精确率。

    `coverage` 是主判据（"证据能不能覆盖结论"），`precision` 只作解释用：
    一条长证据里出现几个结论词并不代表它支持结论，所以不能拿 precision 当支持判据。
    """
    claim, span = set(claim_terms or ()), set(span_terms or ())
    if not claim:
        return {"coverage": 0.0, "precision": 0.0, "overlap": 0, "claim_terms": 0, "span_terms": len(span),
                "missing": []}
    common = claim & span
    missing = sorted(claim - span)
    return {
        "coverage": len(common) / len(claim),
        "precision": len(common) / max(1, len(span)),
        "overlap": len(common),
        "claim_terms": len(claim),
        "span_terms": len(span),
        "missing": missing[:12],
    }


# ── P03-01 relevance / reranker ───────────────────────────────────────────────

def relevance_score(claim_terms: Iterable, item: Mapping) -> dict:
    """证据对"结论/问题"的相关性，[0,1]，纯词面统计（不调模型）。

    口径（可解释、可复算）：
      · 标题覆盖 ×2.5（标题命中信息量大）、正文前段覆盖 ×1.25，两者按 0.55/0.45 加权；
      · 知识库自带相似度若在 [0,1] 内则取两者较大值（>1 的是排序分，不是相似度，忽略）；
      · `title_phrase`：结论实词里有没有整词出现在标题里（有则给 0.15 加成，封顶 1.0）。
    """
    terms = set(claim_terms or ())
    title = _normalize(str(item.get("title") or ""))
    lead = _normalize(str(item.get("content_excerpt") or "")[:600])
    title_hits = sorted(term for term in terms if term and term in title)
    lead_hits = sorted(term for term in terms if term and term in lead)
    if not terms:
        return {"relevance": 0.0, "title_cover": 0.0, "lead_cover": 0.0,
                "title_hits": [], "lead_hits": [], "source": "no_terms"}
    title_cover = len(title_hits) / len(terms)
    lead_cover = len(lead_hits) / len(terms)
    relevance = 0.55 * min(1.0, title_cover * 2.5) + 0.45 * min(1.0, lead_cover * 1.25)
    if title_hits:
        relevance = min(1.0, relevance + 0.15)
    try:
        score = float(item.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    if 0.0 < score <= 1.0:
        relevance = max(relevance, score)
    return {
        "relevance": round(max(0.0, min(1.0, relevance)), 4),
        "title_cover": round(title_cover, 4),
        "lead_cover": round(lead_cover, 4),
        "title_hits": title_hits[:8],
        "lead_hits": lead_hits[:8],
        "source": "title_lead_overlap",
    }


def rerank_evidence(items: Iterable, *, score_of: Callable | None = None) -> tuple:
    """按核验分（或调用方给的分）稳定重排，返回 (排序后, 审计)。

    稳定：同分保持原相对顺序（`sorted` 是稳定排序，键里带上原下标更保险）。
    """
    scored = []
    for index, item in enumerate(items or ()):
        if not isinstance(item, Mapping):
            continue
        if callable(score_of):
            value = score_of(item)
        else:
            value = verification_of(item).get("score")
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = -1.0
        scored.append((number, index, dict(item)))
    ordered = sorted(scored, key=lambda row: (-row[0], row[1]))
    moved = sum(1 for position, row in enumerate(ordered) if position != row[1])
    return [row[2] for row in ordered], {"checked": len(ordered), "reordered": moved,
                                         "top_score": round(ordered[0][0], 4) if ordered else None}


# ── P03-02 entailment / NLI（规则后端 + 可插拔）───────────────────────────────

_BACKENDS: dict = {}
_BACKEND_LOCK = threading.Lock()


def register_entailment_backend(name: str, fn: Callable) -> None:
    """注册一个**进程内**蕴含判定后端。

    只接受本地可调用对象（例如内部训练的 CPU 小模型包装函数）。**没有**"用环境变量直接
    指向一个 HTTP 端点"的口子：想接外部服务必须先写一个本地包装函数并在这里注册，
    这样"是否联网"永远是代码评审看得见的事（MASTER_RULES 第 6/16 条）。
    """
    key = str(name or "").strip()
    if not key or not callable(fn):
        raise ValueError("后端名不能为空，且必须是可调用对象")
    with _BACKEND_LOCK:
        _BACKENDS[key] = fn


def entailment_backends() -> tuple:
    """已注册后端名（`rule` 永远可用）。"""
    with _BACKEND_LOCK:
        return ("rule",) + tuple(sorted(_BACKENDS))


def entailment_backend_name() -> str:
    """当前后端名：QA_NLI_BACKEND > rule；未注册的名字回落到 rule（不报错、不联网）。"""
    raw = str(os.getenv("QA_NLI_BACKEND", "") or "").strip()
    if not raw:
        return "rule"
    return raw if raw in entailment_backends() else "rule"


def _rule_backend(claim_text: str, span_text: str, *, claim_terms=None, span_terms=None) -> dict:
    """规则后端：方向性覆盖度 = 结论实词被证据覆盖的比例（2 元组 + 3 元组加权）。"""
    terms2 = set(claim_terms) if claim_terms is not None else term_set(claim_text, sizes=(2,))
    terms3 = term_set(claim_text, sizes=(3,))
    span2 = set(span_terms) if span_terms is not None else term_set(span_text, sizes=(2,))
    span3 = term_set(span_text, sizes=(3,))
    stats2 = overlap_stats(terms2, span2)
    stats3 = overlap_stats(terms3, span3) if terms3 else stats2
    entailment = 0.65 * stats2["coverage"] + 0.35 * stats3["coverage"]
    label = VERDICT_UNVERIFIED
    if stats2["claim_terms"] == 0:
        label = VERDICT_UNVERIFIED
    elif entailment >= support_min():
        label = VERDICT_SUPPORTED
    elif entailment >= partial_min():
        label = VERDICT_QUALIFIED
    return {
        "label": label,
        "entailment": round(max(0.0, min(1.0, entailment)), 4),
        "backend": "rule",
        "coverage": round(stats2["coverage"], 4),
        "coverage_3gram": round(stats3["coverage"], 4),
        "precision": round(stats2["precision"], 4),
        "claim_terms": stats2["claim_terms"],
        "missing_terms": stats2["missing"][:8],
    }


def nli_entail(claim_text: str, span_text: str, *, claim_terms=None, span_terms=None) -> dict:
    """蕴含判定入口（P03-02）。**任何异常都保守回落**到规则后端并记 `fallback`。

    注意：回落后标签仍按规则算，但审计里带 `fallback_backend`，调用方据此可以把结论
    再降一档（`verify_evidence_item` 就是这么做的：后端不可用时 SUPPORTED 降为 QUALIFIED）。
    `QA_NLI_BACKEND` 配了未注册的名字时也走这条路并**留痕**：静默回落会让"以为接了模型、
    其实一直用规则"变成看不见的事故。
    """
    requested = str(os.getenv("QA_NLI_BACKEND", "") or "").strip()
    name = entailment_backend_name()
    if name == "rule":
        result = _rule_backend(claim_text, span_text, claim_terms=claim_terms, span_terms=span_terms)
        if requested and requested != "rule":
            result["fallback_backend"] = requested
        return result
    with _BACKEND_LOCK:
        fn = _BACKENDS.get(name)
    if not callable(fn):
        result = _rule_backend(claim_text, span_text, claim_terms=claim_terms, span_terms=span_terms)
        result["fallback_backend"] = name
        return result
    try:
        raw = fn(claim_text, span_text)
    except Exception as exc:  # noqa: BLE001 —— 判定后端坏掉绝不能打断问答
        result = _rule_backend(claim_text, span_text, claim_terms=claim_terms, span_terms=span_terms)
        result["fallback_backend"] = name
        result["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:120])
        return result
    result = dict(raw) if isinstance(raw, Mapping) else {}
    label = str(result.get("label") or "")
    if label not in VERDICTS:
        label = VERDICT_UNVERIFIED
    try:
        entailment = float(result.get("entailment"))
    except (TypeError, ValueError):
        entailment = 0.0
    fallback = _rule_backend(claim_text, span_text, claim_terms=claim_terms, span_terms=span_terms)
    return {
        "label": label,
        "entailment": round(max(0.0, min(1.0, entailment)), 4),
        "backend": name,
        "coverage": result.get("coverage", fallback["coverage"]),
        "coverage_3gram": result.get("coverage_3gram", fallback["coverage_3gram"]),
        "precision": result.get("precision", fallback["precision"]),
        "claim_terms": fallback["claim_terms"],
        "missing_terms": fallback["missing_terms"],
    }


# ── P03-03 entity / time / negation / source ─────────────────────────────────

def _focus_sentence(text: str, claim_terms: Iterable, *, cap: int = 300) -> str:
    """挑出与结论实词重合最多的那一分句（否定这类局部语义要在它上面判）。

    拿整篇证据搜否定词会把"未来/无法"这类无关句子算进来（实测：一条真支持的结论被判反证）。
    找不到任何重合就退回整段前 cap 个字符（保守：不放大判定范围）。

    性能：**先整段归一化再切分**（而不是逐分句归一化），重合度用子串判断——
    一段 900 字证据有 20 个分句，逐句归一化会跑 20 次 OpenCC（实测占核验总耗时约 40%）。
    """
    normalized = _normalize(str(text or ""))
    sentences = [part.strip() for part in _SENT_SPLIT.split(normalized) if part.strip()]
    if not sentences:
        return ""
    terms = [str(term) for term in set(claim_terms or ()) if term]
    best, best_score = "", -1
    for sentence in sentences:
        score = sum(1 for term in terms if term in sentence) if terms else 0
        if score > best_score:
            best, best_score = sentence, score
    return (best or normalized)[:cap]


def check_negation(claim_text: str, span_text: str) -> dict:
    """否定一致性：结论与证据的否定极性必须相同，否则**可能是**反证（§10 第 5 条）。

    三层收敛，都是为了不把"碰巧出现的否定词"当成反证（实测：一条完全支持结论的
    新规原文因为末尾"逾期不予受理"被判成反证）：
      ① 极性只在**与结论最相关的那一句证据**上判（`_focus_sentence`）；
      ② 否定词只认多字、语义明确的短语（`_NEGATION_RE`，不收单字 未/无/非）；
      ③ 判定"是否同一件事"（`anchored`）：否定词前后 8 个字里必须出现**对方文本的实词**，
         否则就是各说各的，只降为未核验、不算反证。
    """
    claim_norm = _normalize(claim_text)
    focus = _focus_sentence(span_text, term_set(claim_text))
    span_norm = _normalize(focus or span_text)[:400]
    claim_negated = bool(_NEGATION_RE.search(claim_norm))
    span_negated = bool(_NEGATION_RE.search(span_norm))
    passed = claim_negated == span_negated
    claim_terms = term_set(claim_text, sizes=(2,))
    span_terms = term_set(focus or span_text, sizes=(2,))
    if passed:
        anchored = False
    elif span_negated:
        # 证据含否定、结论不含：这个否定必须落在结论说到的内容上
        anchored = _negation_covers(span_norm, claim_terms)
    else:
        # 结论含否定、证据不含：这个否定必须落在证据提到的内容上
        anchored = _negation_covers(claim_norm, span_terms)
    return {
        "check": "negation", "passed": passed, "anchored": anchored,
        "claim_negated": claim_negated, "evidence_negated": span_negated,
        "focus_sentence": focus[:120],
        "detail": ("否定极性一致" if passed else
                   ("否定极性相反且指向同一话题（按反证处理）" if anchored
                    else "否定极性相反，但否定词说的不是结论那件事（只降为未核验）")),
    }


def _negation_covers(text: str, terms: Iterable, *, window: int = 8) -> bool:
    """否定词前后 `window` 个字里是否出现对方文本的实词（"是不是在说同一件事"）。"""
    match = _NEGATION_RE.search(text or "")
    if not match:
        return False
    start = max(0, match.start() - window)
    end = min(len(text), match.end() + window)
    context = text[start:end]
    return any(term and term in context for term in set(terms or ()))


def check_numbers(claim_text: str, span_text: str) -> dict:
    """数字/期限一致性：结论里的数字必须在证据里找得到（找不到就不许判支持）。"""
    claim_numbers = _number_tokens(claim_text)
    span_numbers = _number_tokens(span_text)
    if not claim_numbers:
        return {"check": "numbers", "passed": True, "applicable": False,
                "claim_numbers": [], "missing": [], "detail": "结论没有数字，跳过数字核验"}
    missing = sorted(claim_numbers - span_numbers)
    return {
        "check": "numbers", "passed": not missing, "applicable": True,
        "claim_numbers": sorted(claim_numbers)[:10], "missing": missing[:10],
        "detail": "数字一致" if not missing else "结论数字 %s 在证据里找不到" % "、".join(missing[:5]),
    }


def check_entities(claim_text: str, item: Mapping, span_text: str, *,
                   required_entities: Iterable | None = None) -> dict:
    """实体一致性（§10 第 3 条）：结论点名要求的实体必须出现在证据里。

    不做新 NER：`required_entities` 由调用方给（计划里的 entities / claim 的 scope），
    证据侧只看它已有的实体表（`metadata.evidence_layer.entities`）与正文/标题文本。
    """
    layer = evidence_object(item)
    evidence_keys = set()
    for entity in layer.get("entities") or []:
        if isinstance(entity, Mapping):
            key = str(entity.get("entity_key") or "")
            if key:
                evidence_keys.add(key)
            label = _normalize(str(entity.get("label") or ""))
            if label:
                evidence_keys.add(label)
    haystack = _normalize("%s %s" % (item.get("title") or "", span_text))
    required, missing = [], []
    for value in required_entities or ():
        clean = _normalize(value)
        if not (2 <= len(clean) <= 40):
            continue
        if clean in required:
            continue
        required.append(clean)
        if clean in haystack or clean in evidence_keys:
            continue
        # 中文实体按 2 元组再给一次机会（"宁德时代" 与 "宁德时代新能源" 这类包含关系）
        grams = {clean[index:index + 2] for index in range(max(0, len(clean) - 1))}
        if grams and all(gram in haystack for gram in grams):
            continue
        missing.append(clean)
    return {
        "check": "entities", "passed": not missing, "applicable": bool(required),
        "required": required[:10], "missing": missing[:10],
        "detail": ("没有点名实体，跳过实体核验" if not required else
                   ("实体一致" if not missing else "证据里没有实体：%s" % "、".join(missing[:5]))),
    }


def _parse_date(value) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for candidate in (raw[:19], raw[:10]):
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    return None


def check_time(published_at, *, valid_from="", valid_to="", now: datetime | None = None) -> dict:
    """时间适用性（§10 第 4 条）+ 时效分。

    规则：证据早于结论生效时间（`valid_from`）→ 不能支持该结论；
    晚于结论有效期（`valid_to`）→ 超出适用范围；拿不到时间 → 时效中性（0.5）并如实记原因。
    """
    moment = now or datetime.now(timezone.utc)
    published = _parse_date(published_at)
    if published is None:
        return {"check": "time", "passed": True, "applicable": False, "freshness": 0.5,
                "reason": REASON_TIME_UNKNOWN, "detail": "证据没有可用发布时间，时效按中性处理"}
    start, end = _parse_date(valid_from), _parse_date(valid_to)
    if start and published < start:
        return {"check": "time", "passed": False, "applicable": True, "freshness": 0.0,
                "reason": REASON_TIME_PRECEDES,
                "detail": "证据发布时间 %s 早于结论生效时间 %s" % (published.date(), start.date())}
    if end and published > end:
        return {"check": "time", "passed": False, "applicable": True, "freshness": 0.0,
                "reason": REASON_TIME_AFTER_WINDOW,
                "detail": "证据发布时间 %s 晚于结论有效期止 %s" % (published.date(), end.date())}
    age_days = max(0.0, (moment - published).total_seconds() / 86400.0)
    freshness = 0.5 ** (age_days / max(1.0, freshness_half_life_days()))
    return {"check": "time", "passed": True, "applicable": True, "freshness": round(freshness, 4),
            "age_days": round(age_days, 2), "reason": "",
            "detail": "证据时间可用（%.0f 天前，时效 %.2f）" % (age_days, freshness)}


def check_source(item: Mapping, claim_text: str, span_text: str) -> dict:
    """来源核验（§10 第 6/7/8 条）：权威性、二手转述、因果夸大、是否只有关键词相似。"""
    layer = evidence_object(item)
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    authority = item.get("authority_level")
    try:
        quality = max(0.1, min(1.0, float(authority) / 100.0))
        has_authority = authority not in (None, "")
    except (TypeError, ValueError):
        quality, has_authority = 0.35, False
    reasons, checks = [], []
    if not has_authority or quality <= 0.35:
        reasons.append(REASON_AUTHORITY_MISSING if not has_authority else REASON_LOW_AUTHORITY)

    doc_type = str(metadata.get("doc_type") or item.get("doc_type") or "").casefold()
    source_role = str(metadata.get("source_role") or item.get("source_role") or "").casefold()
    title = _normalize(str(item.get("title") or ""))
    second_hand = doc_type in _SECOND_HAND_ROLES or source_role in _SECOND_HAND_ROLES \
        or any(marker in title for marker in _SECOND_HAND_MARKERS)
    if second_hand:
        reasons.append(REASON_SECOND_HAND)

    claim_causal = [marker for marker in _CAUSAL_MARKERS if marker in str(claim_text or "")]
    span_causal = [marker for marker in _CAUSAL_MARKERS if marker in str(span_text or "")]
    causal_inflation = bool(claim_causal) and not span_causal
    if causal_inflation:
        reasons.append(REASON_CAUSAL_INFLATION)

    relationship = str(item.get("relationship") or layer.get("relationship") or "").casefold()
    contradicts = relationship == "contradicts"
    if contradicts:
        reasons.append(REASON_CONTRADICT_RELATION)

    checks.append({"check": "source_quality", "passed": bool(has_authority),
                   "detail": "来源权威性 %s" % (authority if has_authority else "缺失")})
    checks.append({"check": "second_hand", "passed": not second_hand,
                   "detail": "非二手转述" if not second_hand else "疑似二手转述/汇编"})
    checks.append({"check": "causality", "passed": not causal_inflation,
                   "detail": "因果表述一致" if not causal_inflation
                             else "结论写了因果（%s），证据里没有因果表述" % "、".join(claim_causal[:3])})
    return {
        "check": "source", "quality": round(quality, 4), "second_hand": second_hand,
        "causal_inflation": causal_inflation, "contradicts": contradicts,
        "reasons": reasons, "checks": checks, "doc_type": doc_type, "source_role": source_role,
    }


# ── P03-04 EvidenceScore / reason / cache ────────────────────────────────────

def evidence_score(dimensions: Mapping, *, weights: Mapping | None = None) -> float:
    """EvidenceScore（01_V2_ARCHITECTURE §11 加权口径），钳制到 [0,1]。

    所有维度都先钳到 [0,1]；缺项按 0 处理（宁可低分，也不给不存在的维度补分）。
    """
    table = dict(weights or score_weights())
    total = 0.0
    for key, weight in table.items():
        try:
            value = float(dimensions.get(key) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        total += float(weight) * max(0.0, min(1.0, value))
    try:
        risk = float(dimensions.get("contradiction_risk") or 0.0)
    except (TypeError, ValueError):
        risk = 0.0
    total -= contradiction_weight() * max(0.0, min(1.0, risk))
    return round(max(0.0, min(1.0, total)), 4)


class VerificationCache:
    """核验结果缓存（P03-04）：进程内 TTL + LRU，可选落 `qa_retrieval_cache`。

    为什么要有：同一条证据在 level1 / 多跳每一跳 / level2 / claim 级核验里会被反复比对，
    规则判定虽然便宜，但 span/术语抽取是可观开销；缓存键含结论文本与证据指纹，
    **不缓存"跨结论"的结论**（换了结论就必须重算）。
    落库复用既有 `qa_retrieval_cache` 表（`QaStore.get/put_verification_cache`），
    因此**不需要动 schema、不需要升版本**；store 不支持时自动只走内存（审计里如实记）。
    """

    def __init__(self, *, store=None, ttl_seconds: int | None = None, max_entries: int | None = None,
                 now: Callable | None = None):
        self.store = store
        self.ttl_seconds = int(ttl_seconds or cache_ttl_seconds())
        self.max_entries = int(max_entries or cache_max_entries())
        self.now = now or time.monotonic
        self._lock = threading.Lock()
        self._data: dict = {}
        self._stats = {"hits": 0, "misses": 0, "stores": 0, "persist_hits": 0, "errors": 0}
        self.persist = bool(store is not None and cache_persist_enabled())

    @staticmethod
    def key(*, claim_text: str, evidence_key: str, pack_id: str = "") -> str:
        """缓存键：结论 + 证据指纹 + 作用域 + 配置指纹。含配置指纹 → 调权重即失效。"""
        body = "%s|%s|%s|%s" % (
            VERIFIER_VERSION, config_hash(), _normalize(claim_text)[:400],
            str(evidence_key or ""))
        if pack_id:
            body += "|pack=%s" % str(pack_id)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()
    def get(self, key: str):
        if not cache_enabled():
            return None
        now = self.now()
        with self._lock:
            row = self._data.get(str(key))
            if row is not None:
                expires, value = row
                if expires > now:
                    self._stats["hits"] += 1
                    return dict(value)
                self._data.pop(str(key), None)
        if self.persist:
            try:
                value = self.store.get_verification_cache(str(key))
            except Exception:  # noqa: BLE001 —— 缓存坏了不算错误，只是慢一点
                self._stats["errors"] += 1
                value = None
            if isinstance(value, Mapping):
                with self._lock:
                    self._stats["persist_hits"] += 1
                return dict(value)
        with self._lock:
            self._stats["misses"] += 1
        return None

    def put(self, key: str, value: Mapping) -> None:
        if not cache_enabled() or not isinstance(value, Mapping):
            return
        payload = dict(value)
        with self._lock:
            self._data[str(key)] = (self.now() + self.ttl_seconds, payload)
            while len(self._data) > self.max_entries:
                self._data.pop(next(iter(self._data)), None)
        if self.persist:
            try:
                self.store.put_verification_cache(str(key), payload, ttl_seconds=self.ttl_seconds)
            except Exception:  # noqa: BLE001
                with self._lock:
                    self._stats["errors"] += 1

    def stats(self) -> dict:
        with self._lock:
            result = dict(self._stats)
            result["size"] = len(self._data)
        result.update({"enabled": cache_enabled(), "persist": self.persist,
                       "ttl_seconds": self.ttl_seconds, "max_entries": self.max_entries})
        return result


_DEFAULT_CACHE = VerificationCache()
_STORE_CACHES: dict = {}
_STORE_CACHE_LOCK = threading.Lock()
_MAX_STORE_CACHES = 8


def verification_cache(store=None) -> VerificationCache:
    """取与该 store 绑定的核验缓存（进程内复用同一个实例）。

    为什么不是一个调用建一个：内存层要跨调用复用才有效（同一条证据在同一次 run 的
    level1/多跳/level2 里来回核验），而落库层要拿到 store。所以按 store 身份缓存实例。
    关掉 `QA_VERIFIER_CACHE_PERSIST` 时只走共享的内存缓存（不写库）。
    """
    if store is None or not cache_persist_enabled():
        return _DEFAULT_CACHE
    with _STORE_CACHE_LOCK:
        cache = _STORE_CACHES.get(id(store))
        if cache is None or cache.store is not store:
            cache = VerificationCache(store=store)
            if len(_STORE_CACHES) >= _MAX_STORE_CACHES:
                _STORE_CACHES.clear()
            _STORE_CACHES[id(store)] = cache
        return cache


def cache_stats() -> dict:
    """默认（无 store）内存缓存的统计；运维自检/验收脚本用。"""
    return _DEFAULT_CACHE.stats()


def verification_of(item: Mapping) -> dict:
    """取出证据上的核验结论（没有就返回空字典）。"""
    value = evidence_object(item).get("verification")
    return dict(value) if isinstance(value, Mapping) else {}


def _evidence_text(item: Mapping, claim_terms: Iterable) -> tuple:
    """挑参与判定的证据文本：最小 span 与正文前段里，覆盖结论实词更多的那个。"""
    layer = evidence_object(item)
    span = layer.get("span") if isinstance(layer.get("span"), Mapping) else {}
    span_quote = str(span.get("quote") or "")
    excerpt = str(item.get("content_excerpt") or "")[:nli_chars()]
    if not span_quote:
        return excerpt, "excerpt"
    terms = set(claim_terms or ())
    span_terms = term_set(span_quote, sizes=(2,))
    excerpt_terms = term_set(excerpt, sizes=(2,))
    if terms:
        span_hit = len(terms & span_terms)
        excerpt_hit = len(terms & excerpt_terms)
        if span_hit >= excerpt_hit:
            return span_quote, "span"
    return excerpt, "excerpt"


def verification_cache_key(*, claim_text: str, item: Mapping, text: str, required_entities=(),
                           valid_from: str = "", valid_to: str = "", independence: float = 1.0,
                           pack_id: str = "", now: datetime | None = None) -> str:
    """核验缓存的完整键：**凡是影响判定的输入都要进键**。

    早先只用"来源指纹 + 正文"当键，结果同来源同正文、但权威性/发布时间不同的两条证据
    互相命中（实测：把 authority_level 从 60 改成 None，仍然拿回上一条的结论）。
    所以键里带上：来源身份、正文、权威性、发布时间、既有 relationship、doc_type/source_role、
    要求实体、有效期、独立性、当天日期（时效分按天稳定，跨天自然重算）。
    """
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    layer = evidence_object(item)
    source = layer.get("source") if isinstance(layer.get("source"), Mapping) else {}
    body = "|".join((
        str(source.get("source_id") or source_identity(item).get("source_id") or ""),
        _normalize(text)[:400],
        str(item.get("authority_level")),
        str(item.get("published_at") or source.get("published_at") or ""),
        str(item.get("relationship") or ""),
        str(metadata.get("doc_type") or item.get("doc_type") or ""),
        str(metadata.get("source_role") or item.get("source_role") or ""),
        ",".join(sorted(_normalize(value) for value in (required_entities or ()))),
        str(valid_from or ""), str(valid_to or ""), "%.3f" % float(independence or 1.0),
        (now or datetime.now(timezone.utc)).date().isoformat(),
    ))
    return VerificationCache.key(claim_text=claim_text,
                                 evidence_key=hashlib.sha256(body.encode("utf-8")).hexdigest()[:32],
                                 pack_id=pack_id)


def verify_evidence_item(item: Mapping, *, claim_text: str, anchor_terms: Iterable | None = None,
                         required_entities: Iterable | None = None, valid_from: str = "",
                         valid_to: str = "", now: datetime | None = None,
                         independence: float = 1.0, cache: VerificationCache | None = None,
                         pack_id: str = "") -> dict:
    """核验一条证据对某条结论（或问题）的支持情况，返回 `verification` 字典。

    判定顺序：**先立否决项**（无结论/无正文 → UNVERIFIED；否定翻转 → REFUTED；
    数字不符/实体缺失 → 封顶 UNVERIFIED；二手转述/因果夸大 → 封顶 QUALIFIED），
    再按覆盖度给 SUPPORTED / QUALIFIED / UNVERIFIED。**证据不足永远不给 SUPPORTED。**

    `anchor_terms` 是调用方额外给的锚点词（计划里的实体等）——只用于相关性打分，
    **不参与覆盖度**：蕴含判定的分子分母必须同一粒度（都是按字切的 n 元组），
    混进 4 字实体词会让覆盖率凭空掉档。
    """
    result = dict(item) if isinstance(item, Mapping) else {}
    anchors = {_normalize(value) for value in (anchor_terms or ())
               if 2 <= len(_normalize(value)) <= 40}
    terms = term_set(claim_text) | anchors
    text, text_source = _evidence_text(result, terms)
    required_list = [str(value) for value in (required_entities or ())]
    store_cache = cache if cache is not None else _DEFAULT_CACHE
    cache_key = verification_cache_key(
        claim_text=claim_text, item=result, text=text, required_entities=required_list,
        valid_from=valid_from, valid_to=valid_to, independence=independence,
        pack_id=pack_id, now=now)
    if cache_enabled():
        cached = store_cache.get(cache_key)
        if isinstance(cached, Mapping) and cached.get("verdict"):
            value = dict(cached)
            value["cache"] = "hit"
            return value

    nli = nli_entail(claim_text, text)
    negation = check_negation(claim_text, text)
    numbers = check_numbers(claim_text, text)
    entities = check_entities(claim_text, result, text, required_entities=required_entities)
    layer = evidence_object(result)
    source = layer.get("source") if isinstance(layer.get("source"), Mapping) else {}
    timing = check_time(result.get("published_at") or source.get("published_at"),
                        valid_from=valid_from, valid_to=valid_to, now=now)
    source_check = check_source(result, claim_text, text)
    relevance = relevance_score(terms, result)

    reasons = []
    verdict = str(nli.get("label") or VERDICT_UNVERIFIED)
    if nli.get("fallback_backend"):
        reasons.append(REASON_BACKEND_FALLBACK)
        # 后端不可用时不许给"满支持"：SUPPORTED 降一档
        if verdict == VERDICT_SUPPORTED:
            verdict = VERDICT_QUALIFIED
    if not str(claim_text or "").strip() or not terms:
        verdict = VERDICT_UNVERIFIED
        reasons.append(REASON_NO_CLAIM)
    elif not text.strip():
        verdict = VERDICT_UNVERIFIED
        reasons.append(REASON_NO_SPAN)
    elif not negation["passed"]:
        # 否定极性相反：**只有指向同一话题时才敢判反证**（§10 第 5 条）。
        # 否则只是碰巧一处有否定词，判 REFUTED 会把无关材料冤枉成反证——那比判 UNVERIFIED 危险得多。
        if negation.get("anchored") and float(nli.get("entailment") or 0.0) >= partial_min():
            verdict = VERDICT_REFUTED
            reasons.append(REASON_NEGATION_FLIP)
        else:
            verdict = VERDICT_UNVERIFIED
            reasons.append(REASON_NEGATION_UNSCOPED if not negation.get("anchored")
                           else REASON_LOW_OVERLAP)
    else:
        if not numbers["passed"]:
            verdict = _downgrade(verdict, VERDICT_UNVERIFIED)
            reasons.append(REASON_NUMBER_MISMATCH)
        if not entities["passed"]:
            verdict = _downgrade(verdict, VERDICT_UNVERIFIED)
            reasons.append(REASON_ENTITY_MISSING)
        if not timing["passed"]:
            verdict = _downgrade(verdict, VERDICT_UNVERIFIED)
            reasons.append(str(timing.get("reason") or REASON_TIME_UNKNOWN))
        if source_check["second_hand"] or source_check["causal_inflation"]:
            verdict = _downgrade(verdict, VERDICT_QUALIFIED)
        if source_check["contradicts"]:
            # 证据自带的标签说它是反证，而规则判定说它支持：两者打架时**不许**悄悄判支持，
            # 降到"部分支持"等人/模型复核（§10：不要只依赖一个判据）。
            verdict = _downgrade(verdict, VERDICT_QUALIFIED)
        if not entities["applicable"] and int(nli.get("claim_terms") or 0) < _MIN_TERMS_FOR_SUPPORT:
            # 实词太少（例如只有两个字的问题）时"支持"没有证据力，降一档
            verdict = _downgrade(verdict, VERDICT_QUALIFIED)
        if verdict == VERDICT_SUPPORTED:
            reasons.append(REASON_SUPPORTED)
        elif verdict == VERDICT_QUALIFIED:
            reasons.append(REASON_PARTIAL)
        elif verdict == VERDICT_UNVERIFIED:
            reasons.append(REASON_LOW_OVERLAP if relevance["relevance"] < relevance_floor()
                           else REASON_KEYWORD_ONLY)
        if verdict == VERDICT_UNVERIFIED and str(result.get("relationship") or "").casefold() == "context":
            # 既有 relationship 就说它是背景材料：不冒充支持，标成 CONTEXT
            verdict = VERDICT_CONTEXT
    reasons.extend(code for code in source_check.get("reasons") or []
                   if code in (REASON_SECOND_HAND, REASON_CAUSAL_INFLATION,
                               REASON_CONTRADICT_RELATION, REASON_LOW_AUTHORITY,
                               REASON_AUTHORITY_MISSING))

    contradiction_risk = 0.0
    if verdict == VERDICT_REFUTED:
        contradiction_risk = max(0.6, min(1.0, float(nli.get("coverage") or 0.0) * 1.5))
    elif REASON_NUMBER_MISMATCH in reasons:
        contradiction_risk = 0.4
    elif source_check["contradicts"]:
        contradiction_risk = 0.5

    dimensions = {
        "relevance": relevance["relevance"],
        "entailment": float(nli.get("entailment") or 0.0),
        "source_quality": source_check["quality"],
        "freshness": timing["freshness"],
        "independence": max(0.0, min(1.0, float(independence))),
        "contradiction_risk": round(contradiction_risk, 4),
    }
    verification = {
        "verifier_version": VERIFIER_VERSION,
        "config_hash": config_hash(),
        "verdict": verdict,
        "verified": verdict == VERDICT_SUPPORTED,
        "score": evidence_score(dimensions),
        "dimensions": dimensions,
        "reasons": list(dict.fromkeys(reasons))[:10],
        "reason_text": reason_text(reasons),
        "nli": {"backend": nli.get("backend"), "entailment": nli.get("entailment"),
                "coverage": nli.get("coverage"), "coverage_3gram": nli.get("coverage_3gram"),
                "label": nli.get("label")},
        "checks": [negation, numbers, entities,
                   {"check": "time", **{key: timing[key] for key in ("passed", "applicable", "detail")}},
                   relevance, source_check],
        "text_source": text_source,
        "cache": "miss",
    }
    if cache_enabled():
        store_cache.put(cache_key, verification)
    return verification


def _domain_of(item: Mapping) -> str:
    """证据的来源域（用于"独立性"折算：同一家媒体的多篇报道不是多份独立证据）。"""
    from urllib.parse import urlsplit

    url = str(item.get("source_url") or "")
    host = (urlsplit(url).hostname or "").casefold()
    if host:
        return host
    source = source_identity(item)
    return str(source.get("source_id") or "").split(":")[0] or "unknown"


def verify_evidence_batch(items: Iterable, *, claim_text: str, terms: Iterable | None = None,
                          required_entities: Iterable | None = None, valid_from: str = "",
                          valid_to: str = "", store=None, pack_id: str = "",
                          now: datetime | None = None, gate: str | None = None,
                          rerank: bool | None = None,
                          cache: VerificationCache | None = None) -> tuple:
    """证据级核验（P03-01…P03-04 主入口）：逐条核验 → 按需闸门 → 按分重排。

    返回 (证据列表, 审计)。**任何异常都原样返回证据**并把原因写进审计——
    核验层和证据层一样，绝不能把问答打断。
    """
    evidence = [item for item in (items or []) if isinstance(item, Mapping)]
    audit = {
        "verifier": VERIFIER_VERSION, "config_hash": config_hash(), "enabled": verifier_enabled(),
        "gate": gate or gate_mode(), "checked": 0, "verdicts": {}, "dropped": 0,
        "reasons": {}, "reordered": 0, "degraded": [],
    }
    if not verifier_enabled():
        audit["reason"] = "QA_VERIFIER_ENABLED=0"
        return list(evidence), audit
    try:
        terms = set(terms) if terms is not None else term_set(claim_text)
        counts: dict = {}
        for item in evidence:
            domain = _domain_of(item)
            counts[domain] = counts.get(domain, 0) + 1
        cache_obj = cache if cache is not None else verification_cache(store)
        reviewed, verdicts, reasons = [], {}, {}
        for item in evidence:
            verification = verify_evidence_item(
                item, claim_text=claim_text, anchor_terms=terms,
                required_entities=required_entities, valid_from=valid_from, valid_to=valid_to,
                now=now, independence=1.0 / max(1, counts.get(_domain_of(item), 1)),
                cache=cache_obj, pack_id=pack_id)
            value = dict(item)
            metadata = dict(value.get("metadata") or {}) if isinstance(value.get("metadata"), Mapping) else {}
            layer = dict(metadata.get("evidence_layer") or {})
            layer["verification"] = verification
            metadata["evidence_layer"] = layer
            value["metadata"] = metadata
            reviewed.append(value)
            verdict = str(verification.get("verdict") or VERDICT_UNVERIFIED)
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
            for code in verification.get("reasons") or []:
                reasons[str(code)] = reasons.get(str(code), 0) + 1
        mode = str(gate or gate_mode())
        kept, dropped = [], 0
        for item in reviewed:
            verification = verification_of(item)
            verdict = str(verification.get("verdict") or VERDICT_UNVERIFIED)
            relevance = float((verification.get("dimensions") or {}).get("relevance") or 0.0)
            if mode == "refuted" and verdict == VERDICT_REFUTED:
                dropped += 1
                continue
            if mode == "unverified" and verdict in (VERDICT_REFUTED, VERDICT_UNVERIFIED) \
                    and relevance < relevance_floor():
                dropped += 1
                continue
            kept.append(item)
        if not kept and dropped:
            # 闸门把整批证据清空：宁可退回原证据（只加了核验标注），也不给用户空证据包
            kept, dropped = reviewed, 0
            audit["degraded"].append("gate_emptied_evidence_fallback")
        if (rerank if rerank is not None else rerank_enabled()) and len(kept) > 1:
            kept, rank_audit = rerank_evidence(kept)
            audit["reordered"] = int(rank_audit.get("reordered") or 0)
        audit.update({"checked": len(reviewed), "verdicts": verdicts, "dropped": dropped,
                      "reasons": dict(sorted(reasons.items(), key=lambda row: -row[1])[:10]),
                      "cache": cache_obj.stats()})
        return kept, audit
    except Exception as exc:  # noqa: BLE001 —— 核验绝不打断问答
        audit["reason"] = "%s: %s" % (type(exc).__name__, str(exc)[:120])
        return list(evidence), audit


# ── claim 级核验（MASTER_RULES 第 11 条落地）─────────────────────────────────

def _claim_status(pairs: list) -> str:
    """证据对综合成 claim 的核验状态（取值来自 CLAIM_SCHEMA 既有枚举，不新增）。

    · 有反证且无反支持的 → `conflicted`；
    · 至少一条真支持 → `confirmed`；
    · 只有部分支持 → `qualified`；
    · 只有背景材料 / 都没过 → `unverified`；
    · 一条证据都没有（或引用悬空）→ `insufficient_evidence`。
    """
    if not pairs:
        return "insufficient_evidence"
    verdicts = [str(pair.get("verdict") or "") for pair in pairs]
    if VERDICT_REFUTED in verdicts and VERDICT_SUPPORTED not in verdicts:
        return "conflicted"
    if VERDICT_SUPPORTED in verdicts:
        return "confirmed"
    if VERDICT_QUALIFIED in verdicts:
        return "qualified"
    return "unverified"


def verify_claim_graph(graph: Mapping, *, store=None, pack_id: str = "",
                       now: datetime | None = None, cache: VerificationCache | None = None) -> dict:
    """Claim 级核验：**覆盖模型自评**，把结论状态改成规则判定的结果（MASTER_RULES 第 11 条）。

    真机实测：一级草稿给出的 claim 里 `verification_status` 全部是模型自己填的 `qualified`
    —— 没有任何东西核验过它。本函数按"结论 → 引用的证据"逐对核验，重写该字段，
    并给出 claim 级的 support/refute 质量（§11 的 support_count / independent_source_count /
    support_mass / refute_mass），以及 `unsupported_claim_rate`（Phase 00 登记的 Quality 缺口）。

    就地修改 `graph`（调用方紧接着就要 `persist_reasoning_graph`），并返回核验摘要。
    """
    evidence_by_ref = {}
    for item in graph.get("evidence") or []:
        if isinstance(item, Mapping):
            evidence_by_ref[str(item.get("evidence_ref") or "")] = item
    if cache is None and store is not None:
        cache = verification_cache(store)
    cache_obj = cache if cache is not None else _DEFAULT_CACHE
    stats = {"claims": 0, "confirmed": 0, "qualified": 0, "conflicted": 0,
             "unverified": 0, "insufficient_evidence": 0, "supported_pairs": 0,
             "refuted_pairs": 0, "pairs": 0}
    details = []
    for node in graph.get("claims") or []:
        if not isinstance(node, Mapping):
            continue
        claim = node.get("claim")
        if not isinstance(claim, Mapping):
            continue
        stats["claims"] += 1
        claim_text = str(claim.get("text") or "")
        # 结论自带的 scope 就是"这条结论点名要求的适用范围/实体"，正是实体核验的输入
        required = list(dict.fromkeys(str(value) for value in (claim.get("scope") or [])
                                      if str(value).strip()))
        pairs = []
        for ref in claim.get("evidence_refs") or []:
            reference = str(ref or "")
            item = evidence_by_ref.get(reference)
            if item is None:
                pairs.append({"evidence_ref": reference, "verdict": VERDICT_UNVERIFIED,
                              "score": 0.0, "reasons": [REASON_MISSING_EVIDENCE],
                              "reason_text": reason_text([REASON_MISSING_EVIDENCE])})
                continue
            # 只认"这条结论 vs 这条证据"的核验：证据包上已有的核验是拿**问题**算的，
            # 直接复用等于用问题的相关性冒充结论的蕴含（MASTER_RULES 第 11 条要拦的正是这个）。
            verification = verify_evidence_item(
                item, claim_text=claim_text, required_entities=required,
                valid_from=str(claim.get("valid_from") or ""),
                valid_to=str(claim.get("valid_to") or ""),
                now=now, cache=cache_obj, pack_id=pack_id)
            pairs.append({
                "evidence_ref": reference,
                "verdict": str(verification.get("verdict") or VERDICT_UNVERIFIED),
                "score": float(verification.get("score") or 0.0),
                "entailment": float((verification.get("dimensions") or {}).get("entailment") or 0.0),
                "reasons": list(verification.get("reasons") or []),
                "reason_text": str(verification.get("reason_text") or ""),
            })
        supported = [pair for pair in pairs if pair["verdict"] == VERDICT_SUPPORTED]
        refuted = [pair for pair in pairs if pair["verdict"] == VERDICT_REFUTED]
        status = _claim_status(pairs)
        support_mass = round(sum(pair["score"] for pair in supported), 4)
        refute_mass = round(sum(pair["score"] for pair in refuted), 4)
        source_ids = set()
        for pair in supported:
            source_ids.add(_domain_of(evidence_by_ref.get(pair["evidence_ref"]) or {}))
        claim_reasons = list(dict.fromkeys(
            [code for pair in pairs for code in pair.get("reasons") or []]))
        if not pairs:
            # 结论一条证据都没引用：这不是"核验没做"，而是模型没给出处，必须显式记下来
            claim_reasons.insert(0, REASON_CLAIM_NO_EVIDENCE)
        node["verification"] = {
            "verifier_version": VERIFIER_VERSION,
            "status": status,
            "support_count": len(supported),
            "independent_source_count": len(source_ids),
            "support_mass": support_mass,
            "refute_mass": refute_mass,
            "score": round(max([pair["score"] for pair in pairs], default=0.0), 4),
            "pairs": pairs[:12],
            "reasons": claim_reasons[:10],
        }
        # 覆盖模型自评：LLM 写的 confirmed/qualified 一律作废，只认规则核验结果
        claim = dict(claim)
        claim["verification_status"] = status
        node["claim"] = claim
        stats[status] = stats.get(status, 0) + 1
        stats["pairs"] += len(pairs)
        stats["supported_pairs"] += len(supported)
        stats["refuted_pairs"] += len(refuted)
        details.append({"claim_id": str(node.get("canonical_id") or claim.get("claim_id") or ""),
                        "status": status, "pairs": len(pairs), "support_count": len(supported)})
    claims = max(1, stats["claims"])
    summary = {
        "verifier_version": VERIFIER_VERSION,
        "config_hash": config_hash(),
        "enabled": verifier_enabled(),
        "stats": {
            **stats,
            "unsupported_claim_rate": round(
                (stats["claims"] - stats["confirmed"]) / claims, 4) if stats["claims"] else None,
            "claim_without_evidence": sum(1 for detail in details if not detail["pairs"]),
        },
        "claims": details[:60],
        "cache": cache_obj.stats(),
    }
    graph["verification"] = summary
    return summary


__all__ = [
    "CACHE_NAMESPACE", "DEFAULT_WEIGHTS", "REASON_TEXTS", "VERDICTS", "VERIFICATION_REASONS",
    "VERIFIER_VERSION", "VerificationCache", "cache_enabled", "cache_persist_enabled",
    "check_entities", "check_negation", "check_numbers", "check_source", "check_time",
    "config_hash", "contradiction_weight", "entailment_backend_name", "entailment_backends",
    "evidence_score", "freshness_half_life_days", "gate_mode", "nli_chars", "nli_entail",
    "overlap_stats", "partial_min", "reason_text", "register_entailment_backend",
    "relevance_floor", "relevance_score", "rerank_enabled", "rerank_evidence", "score_weights",
    "support_min", "term_set", "verification_cache", "verification_of", "verifier_enabled",
    "verify_claim_graph", "verify_evidence_batch", "verify_evidence_item", "cache_stats",
]

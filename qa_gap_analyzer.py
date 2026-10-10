#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 07（P07-01…P07-06）· Gap Analyzer 与 Dynamic Multi-hop（**纯规则，零模型调用**）。

通用包 01_V2_ARCHITECTURE 依据：
  · §12 Gap Analyzer：每轮 fan-in 后问"为了可靠回答原问题，哪些必要 Claim 仍缺少什么类型的
    证据？"——Gap 形状 `{gap_id, claim_id, missing, priority, suggested_queries,
    suggested_routes, reason}` 与**十种** Gap 类型逐字取自这一节；
  · §13 Dynamic Next-hop Planner："下一跳不是 hop+1，而是 Gap → Best Retrieval Action"
    （G1→BM25 Hunter / G4→Two independent Hunters）；
  · §14 收敛与停止："连续两轮 new_verified_claims == 0 AND resolved_high_priority_gaps == 0
    → STOP_NO_GAIN"；五个停止原因沿用 Phase 01 冻结的 `QA_STOP_REASONS`（不新增取值）；
  · §21 Safety-Critical Gap → Priority Override；
  · §23 Search Trace：每跳要能回答"为什么搜 / 为什么走这个 route / 解决了哪个 Gap / 为什么停"；
  · §24 Query Fingerprint = normalized_query + constraints + corpus_version + retrieval_config。

本模块**一行检索/核验逻辑都不重写**，只做"把既有结论读成缺口、把缺口翻成下一跳"：
  复用 ① Phase 03 `qa_verifier` 的核验结论（claim 级 `verification.pairs[].verdict/reasons`、
        证据条目上的 `metadata.evidence_layer.verification.verdict/reasons`）——**缺口类型由
        核验理由码派生**（映射表 `_GAP_TYPES_BY_REASON`），不另写一套质量判断；
  复用 ② Phase 06 `qa_evidence_graph` 的图关系与矛盾裁决（`graph_relation`、`resolution`、
        `reason_code`）——`CONTRADICTION` 缺口与 `UNRESOLVABLE_CONTRADICTION` 停止原因都引它；
  复用 ③ Phase 02 `qa_evidence` 的 seen 机制（`source_fingerprint` / `seen_records` /
        `filter_seen`）——下一跳的去重与"已见被拒来源不再重搜"；
  复用 ④ Phase 05 `qa_execution_graph.build_research_plan` 的计划 Claim（§12 的"必要 Claim"）
        与 `qa_query_normalize.normalize_text`（查询归一化，Query Fingerprint 的第一段）；
  复用 ⑤ Phase 01 冻结的 `QA_STOP_REASONS` / `QA_RETRIEVAL_ROUTES` / `QA_HUNTER_IDS`。

四条硬边界（诚实声明，宁写 PARTIAL 不谎报）
------------------------------------------
  1. **零模型调用**：缺口的分类/优先级/查询生成全部是规则与统计；§13 说"下一跳可以更聪明"，
    本仓库把它落成**可插拔注入点**（`register_next_hop_planner` + `QA_NEXT_HOP_PLANNER`），
    默认实现是规则，本轮不注册任何模型后端，模块内零 HTTP/socket/embedding 依赖（有守门用例）。
     能力边界：规则只能做**词面**的查询构造（claim 实词 + 缺口类型模板 + 计划实体），
     做不了"换个说法重述问题""跨语言改写""发现隐含的中间实体"——那正是 LLM 版的增量。
  2. **route 只影响"怎么搜"、不另开检索旁路**：`suggested_routes` 取既有 7 个通道值，
     它们是**现有检索链路本来就有的通道**（`ArticleRetriever.retrieve()` 内含 keyword /
     semantic / graph / graph_attribute / policy_exact / page_context / web），下一跳把 route
     翻成真实的计划覆盖（`plan_overrides`：queries / entities / terms），逐字复用同一条检索
     链路与同一套闸门；**不新造检索器、不并发开第二套索引**。
  3. **`resolved` 有严格口径**：缺口 id 由 (claim, 缺口类型, 判别细节) 内容哈希而来，同一
     条件下永远同一个 id；"上轮有、本轮没了"才算 resolved（见 `GapLoopState.observe`），
     不靠"看起来解决了"。
  4. **NO_GAIN / UNRESOLVABLE_CONTRADICTION 不许编**：NO_GAIN 必须有连续若干轮
     `new_verified_claims == 0 AND resolved_high_priority_gaps == 0` 的**逐轮回执**；
     UNRESOLVABLE_CONTRADICTION 必须引用 Phase 06 的真实裁决（`resolution == "unresolved"`），
     并在回执里写明是哪条理由码让它无法消解。两者都把证据写进回执，可复算。
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Callable, Iterable, Mapping, Sequence

from qa_graph_contracts import (
    EVIDENCE_STATUS_QUALIFIED,
    EVIDENCE_STATUS_REFUTED,
    EVIDENCE_STATUS_SUPPORTED,
    GAP_ANALYZER_VERSION,
    GAP_PRIORITY_BANDS,
    GAP_PRIORITY_WEIGHTS,
    GAP_STATUSES,
    NEXT_HOP_PLANNER_VERSION,
    QA_GAP_AMBIGUOUS_ENTITY,
    QA_GAP_CONTRADICTION,
    QA_GAP_LOW_AUTHORITY,
    QA_GAP_LOW_RELEVANCE,
    QA_GAP_MISSING_CAUSAL_BRIDGE,
    QA_GAP_MISSING_COUNTEREVIDENCE,
    QA_GAP_MISSING_ENTITY_LINK,
    QA_GAP_MISSING_TIME_LINK,
    QA_GAP_NO_EVIDENCE,
    QA_GAP_SINGLE_SOURCE,
    QA_GAP_TYPES,
    QA_RETRIEVAL_ROUTES,
    QA_ROUTE_GRAPH,
    QA_ROUTE_GRAPH_ATTRIBUTE,
    QA_ROUTE_KEYWORD,
    QA_ROUTE_PAGE_CONTEXT,
    QA_ROUTE_POLICY_EXACT,
    QA_ROUTE_SEMANTIC,
    QA_ROUTE_WEB,
    QA_STOP_ANSWERABLE,
    QA_STOP_BUDGET_EXHAUSTED,
    QA_STOP_MAX_DEPTH,
    QA_STOP_NO_GAIN,
    QA_STOP_UNRESOLVABLE_CONTRADICTION,
    SAFETY_OVERRIDE_PRIORITY,
    validate as validate_contract,
)
from qa_verifier import (
    REASON_AUTHORITY_MISSING,
    REASON_CAUSAL_INFLATION,
    REASON_CLAIM_NO_EVIDENCE,
    REASON_ENTITY_MISSING,
    REASON_LOW_AUTHORITY,
    REASON_LOW_OVERLAP,
    REASON_KEYWORD_ONLY,
    REASON_MISSING_EVIDENCE,
    REASON_NUMBER_MISMATCH,
    REASON_PARTIAL,
    REASON_SECOND_HAND,
    REASON_TIME_AFTER_WINDOW,
    REASON_TIME_PRECEDES,
    REASON_TIME_UNKNOWN,
    relevance_score,
    term_set,
)

# ── 可配置旋钮（全部可回滚；默认值都写在常量里，便于复算）───────────────────
DEFAULT_NO_GAIN_ROUNDS = 2          # §14：连续两轮无增益
DEFAULT_PRIORITY_THRESHOLD = 0.70   # "高优缺口"的门槛（同时是 band 的 high 档下界）
DEFAULT_MAX_NEXT_HOPS = 1           # 缺口驱动的补充跳上限（默认只补一跳，省预算）
DEFAULT_MAX_GAPS = 24               # 缺口清单上限（防止病态输入把回执炸掉）
DEFAULT_MAX_GAPS_PER_CLAIM = 3      # 单条 claim 最多记几个缺口（只留最要紧的）
DEFAULT_AUTHORITY_FLOOR = 50        # "够权威"的来源级别下界（对齐 qa_reasoning.authority_score：
                                    # 50 = professional_commentary，90/100 = 官方解读/官方原文）
DEFAULT_RELEVANCE_FLOOR = 0.34      # 词面相关性下界（与 evidence_graph.plan_match_min 同量级）
DEFAULT_INDEPENDENT_SOURCES = 2     # §12 SINGLE_SOURCE：独立来源数下界
MAX_HOP_ROUNDS = 8                  # 循环轮次硬上限（跳数上限另有 QA_MAX_HOPS / MAX_HOPS_HARD）

GAP_SEVERITY = {
    QA_GAP_NO_EVIDENCE: 1.00,
    QA_GAP_CONTRADICTION: 0.95,
    QA_GAP_LOW_RELEVANCE: 0.80,
    QA_GAP_LOW_AUTHORITY: 0.75,
    QA_GAP_MISSING_CAUSAL_BRIDGE: 0.70,
    QA_GAP_SINGLE_SOURCE: 0.65,
    QA_GAP_MISSING_ENTITY_LINK: 0.60,
    QA_GAP_MISSING_TIME_LINK: 0.55,
    QA_GAP_MISSING_COUNTEREVIDENCE: 0.50,
    QA_GAP_AMBIGUOUS_ENTITY: 0.45,
}
"""缺口类型的基础严重度（0…1，规则给定、可复算）。§12 的十种类型一个不少。"""

GAP_ROUTE_RULES = {
    # Gap → Best Retrieval Action（§13）。取值只能是既有 7 个通道；顺序 = 建议优先级。
    QA_GAP_NO_EVIDENCE: (QA_ROUTE_KEYWORD, QA_ROUTE_SEMANTIC, QA_ROUTE_WEB),
    QA_GAP_LOW_RELEVANCE: (QA_ROUTE_SEMANTIC, QA_ROUTE_KEYWORD, QA_ROUTE_PAGE_CONTEXT),
    QA_GAP_LOW_AUTHORITY: (QA_ROUTE_POLICY_EXACT, QA_ROUTE_GRAPH_ATTRIBUTE, QA_ROUTE_WEB),
    QA_GAP_SINGLE_SOURCE: (QA_ROUTE_KEYWORD, QA_ROUTE_SEMANTIC),
    QA_GAP_MISSING_ENTITY_LINK: (QA_ROUTE_GRAPH, QA_ROUTE_GRAPH_ATTRIBUTE),
    QA_GAP_MISSING_TIME_LINK: (QA_ROUTE_POLICY_EXACT, QA_ROUTE_KEYWORD),
    QA_GAP_CONTRADICTION: (QA_ROUTE_POLICY_EXACT, QA_ROUTE_GRAPH),
    QA_GAP_AMBIGUOUS_ENTITY: (QA_ROUTE_GRAPH_ATTRIBUTE, QA_ROUTE_GRAPH),
    QA_GAP_MISSING_CAUSAL_BRIDGE: (QA_ROUTE_GRAPH, QA_ROUTE_SEMANTIC, QA_ROUTE_KEYWORD),
    QA_GAP_MISSING_COUNTEREVIDENCE: (QA_ROUTE_SEMANTIC, QA_ROUTE_WEB, QA_ROUTE_KEYWORD),
}
"""缺口类型 → 建议检索通道（§13 的 "Gap → Best Retrieval Action" 落成通道选择）。"""

HUNTER_BY_ROUTE = {
    QA_ROUTE_KEYWORD: ("bm25",),
    QA_ROUTE_SEMANTIC: ("semantic",),
    QA_ROUTE_GRAPH: ("graph",),
    QA_ROUTE_GRAPH_ATTRIBUTE: ("graph", "structured"),
    QA_ROUTE_POLICY_EXACT: ("structured",),
    QA_ROUTE_PAGE_CONTEXT: ("bm25",),
    QA_ROUTE_WEB: ("query_expansion",),
}
"""通道 → 执行它的 Hunter 身份（复用 Phase 04 的 `QA_HUNTER_IDS`；§13 的 "G1 → BM25 Hunter"）。

这是**声明式**映射：`hunters` 只进回执/留痕（说明"这一跳该由谁跑"），运行期统一走
`ArticleRetriever`——舰队是否真的按 Hunter 分开跑由 `QA_HUNTER_FLEET` 决定（Phase 04）。"""

# 缺口类型 → 证据要求（§7 的 Evidence Requirement 口径，取值复用 qa_planner 的 source 口径）
EVIDENCE_TYPE_BY_GAP = {
    QA_GAP_NO_EVIDENCE: "any_evidence",
    QA_GAP_LOW_RELEVANCE: "relevant_evidence",
    QA_GAP_LOW_AUTHORITY: "official_source",
    QA_GAP_SINGLE_SOURCE: "independent_source",
    QA_GAP_MISSING_ENTITY_LINK: "entity_link",
    QA_GAP_MISSING_TIME_LINK: "time_link",
    QA_GAP_CONTRADICTION: "authoritative_adjudication",
    QA_GAP_AMBIGUOUS_ENTITY: "entity_disambiguation",
    QA_GAP_MISSING_CAUSAL_BRIDGE: "causal_bridge",
    QA_GAP_MISSING_COUNTEREVIDENCE: "counterevidence",
}
"""缺口类型 → "缺的是哪类证据"。这是 P07-02 的可校验部分（进 `evidence_requirement`）。"""

# 缺口类型 → 查询模板后缀（纯规则；LLM 版的"问题重写"见模块头第 1 条边界）
QUERY_SUFFIX_BY_GAP = {
    QA_GAP_NO_EVIDENCE: "",
    QA_GAP_LOW_RELEVANCE: "依据 原文",
    QA_GAP_LOW_AUTHORITY: "官方 政策 原文",
    QA_GAP_SINGLE_SOURCE: "多方 交叉 验证",
    QA_GAP_MISSING_ENTITY_LINK: "关联 实体",
    QA_GAP_MISSING_TIME_LINK: "时间 生效 时点",
    QA_GAP_CONTRADICTION: "反驳 相反 口径",
    QA_GAP_AMBIGUOUS_ENTITY: "具体 指 哪家",
    QA_GAP_MISSING_CAUSAL_BRIDGE: "原因 导致 传导",
    QA_GAP_MISSING_COUNTEREVIDENCE: "风险 反例 反对",
}

# 核验理由码 → 缺口类型（P07-01 的核心复用：**缺口由 Phase 03 的理由码派生**）
_GAP_TYPES_BY_REASON = {
    REASON_LOW_OVERLAP: QA_GAP_LOW_RELEVANCE,
    REASON_KEYWORD_ONLY: QA_GAP_LOW_RELEVANCE,
    REASON_PARTIAL: QA_GAP_LOW_RELEVANCE,
    REASON_SECOND_HAND: QA_GAP_LOW_AUTHORITY,
    REASON_LOW_AUTHORITY: QA_GAP_LOW_AUTHORITY,
    REASON_AUTHORITY_MISSING: QA_GAP_LOW_AUTHORITY,
    REASON_ENTITY_MISSING: QA_GAP_MISSING_ENTITY_LINK,
    REASON_TIME_PRECEDES: QA_GAP_MISSING_TIME_LINK,
    REASON_TIME_AFTER_WINDOW: QA_GAP_MISSING_TIME_LINK,
    REASON_TIME_UNKNOWN: QA_GAP_MISSING_TIME_LINK,
    REASON_CAUSAL_INFLATION: QA_GAP_MISSING_CAUSAL_BRIDGE,
    REASON_NUMBER_MISMATCH: QA_GAP_CONTRADICTION,
    REASON_MISSING_EVIDENCE: QA_GAP_NO_EVIDENCE,
    REASON_CLAIM_NO_EVIDENCE: QA_GAP_NO_EVIDENCE,
}
"""Phase 03 核验理由码 → 缺口类型。**这是"缺口不是另算的"落地方式**：verifier 说
"证据与结论重叠不足"，这里就记 `LOW_RELEVANCE`，两处口径不可能打架。"""

_TIME_REASONS = {REASON_TIME_PRECEDES, REASON_TIME_AFTER_WINDOW, REASON_TIME_UNKNOWN}
_AUTHORITY_REASONS = {REASON_LOW_AUTHORITY, REASON_AUTHORITY_MISSING, REASON_SECOND_HAND}
_RELEVANCE_REASONS = {REASON_LOW_OVERLAP, REASON_KEYWORD_ONLY, REASON_PARTIAL}

# §21：安全关键缺口 → Priority Override。默认词表只放"高风险事实"这一类词，
# 可用 `QA_GAP_SAFETY_TERMS`（逗号分隔）追加；命中即 priority 抬到安全档。
DEFAULT_SAFETY_TERMS: tuple = (
    "安全", "事故", "召回", "处罚", "违规", "退市", "停牌", "重大风险", "医疗", "用药",
    "剂量", "过敏", "禁忌", "合规", "诉讼", "破产", "违约",
)
"""§21 的 `SafetyCritical` 词面口径（可配置）。注意这是**词面**判定，不是语义判定——
能力边界写在模块头第 1 条；本轮不引入模型做安全分类（硬约束禁止）。"""

_CAUSAL_MARKERS = ("因为", "由于", "导致", "推动", "驱动", "源于", "因此", "使得", "造成")
_COUNTER_MARKERS = ("但是", "然而", "相反", "反之", "不过", "风险", "反例", "下调")
_CAUSAL_QUESTION_CATEGORIES = ("causal", "multi_hop", "mechanism")
_COUNTER_QUESTION_CATEGORIES = ("comparison", "multi_hop", "synthesis")


# ── 开关与旋钮 ─────────────────────────────────────────────────────────────
def _env_flag(name: str, default: bool) -> bool:
    raw = str(os.environ.get(name, "")).strip().casefold()
    if not raw:
        return bool(default)
    return raw not in ("0", "false", "no", "off")


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(float(str(os.environ.get(name, "")).strip()))
    except (TypeError, ValueError):
        return int(default)
    return min(max(value, low), high)


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return float(default)
    return min(max(value, low), high)


def gap_analyzer_enabled() -> bool:
    """缺口循环开关（`QA_GAP_ANALYZER`）。**默认关**：不打开即逐字回到接线前行为。"""
    return _env_flag("QA_GAP_ANALYZER", False)


def no_gain_rounds() -> int:
    return _env_int("QA_GAP_NO_GAIN_ROUNDS", DEFAULT_NO_GAIN_ROUNDS, 1, 5)


def priority_threshold() -> float:
    return _env_float("QA_GAP_PRIORITY_THRESHOLD", DEFAULT_PRIORITY_THRESHOLD, 0.0, 1.0)


def max_next_hops() -> int:
    return _env_int("QA_GAP_MAX_NEXT_HOPS", DEFAULT_MAX_NEXT_HOPS, 0, 4)


def max_gaps() -> int:
    return _env_int("QA_GAP_MAX_GAPS", DEFAULT_MAX_GAPS, 1, 200)


def max_gaps_per_claim() -> int:
    return _env_int("QA_GAP_MAX_GAPS_PER_CLAIM", DEFAULT_MAX_GAPS_PER_CLAIM, 1, 6)


def authority_floor() -> int:
    return _env_int("QA_GAP_AUTHORITY_FLOOR", DEFAULT_AUTHORITY_FLOOR, 0, 1000)


def relevance_floor() -> float:
    return _env_float("QA_GAP_RELEVANCE_FLOOR", DEFAULT_RELEVANCE_FLOOR, 0.0, 1.0)


def independent_sources_floor() -> int:
    return _env_int("QA_GAP_INDEPENDENT_SOURCES", DEFAULT_INDEPENDENT_SOURCES, 1, 20)


def safety_terms() -> tuple:
    raw = str(os.environ.get("QA_GAP_SAFETY_TERMS", "") or "")
    extra = tuple(item.strip() for item in raw.split(",") if item.strip())
    return tuple(dict.fromkeys(DEFAULT_SAFETY_TERMS + extra))


def priority_weights() -> dict:
    return {
        "severity": _env_float("QA_GAP_W_SEVERITY", GAP_PRIORITY_WEIGHTS["severity"], 0.0, 1.0),
        "claim_importance": _env_float("QA_GAP_W_IMPORTANCE",
                                       GAP_PRIORITY_WEIGHTS["claim_importance"], 0.0, 1.0),
        "evidence_deficit": _env_float("QA_GAP_W_DEFICIT",
                                       GAP_PRIORITY_WEIGHTS["evidence_deficit"], 0.0, 1.0),
    }


def category_of(plan: Mapping | None) -> str:
    """从计划里取问题类别（复用既有 `plan["category"]`，不另做分类）。

    接受两种既有形状：`{"category": {"key": "causal"}}`（qa_planner 的产物）
    与 `{"category": "causal"}`（简化形状）。
    """
    plan = plan if isinstance(plan, Mapping) else {}
    category = plan.get("category")
    if isinstance(category, Mapping):
        return str(category.get("key") or "")
    return str(category or "")


def band_of(priority, *, threshold: float | None = None) -> str:
    """优先级分档（同一个数字永远同一档；阈值与"高优"门槛共用，避免两套口径）。"""
    value = _clamp(priority)
    threshold = priority_threshold() if threshold is None else float(threshold)
    if value >= max(0.85, threshold):
        return "critical"
    if value >= threshold:
        return "high"
    if value >= 0.5:
        return "medium"
    return "low"


def is_high_priority(priority, *, threshold: float | None = None) -> bool:
    threshold = priority_threshold() if threshold is None else float(threshold)
    return _clamp(priority) >= threshold


# ── 小工具（确定性、可复算）────────────────────────────────────────────────
def _clamp(value, low: float = 0.0, high: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(low)
    if number != number:      # NaN
        return float(low)
    return min(max(number, low), high)


def _digest(value, length: int = 12) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:length]


def _norm(text) -> str:
    """查询/文本归一化：折叠空白 + casefold。

    **刻意不引入新依赖**：`qa_query_normalize.normalize_text` 做的是简繁转换 + 折叠，
    这里只做成本最低、跨平台稳定的一段（与 `qa_evidence._norm_text` 同口径）；
    需要简繁归一时由 `query_fingerprint` 显式传入 `normalized`。
    """
    return " ".join(str(text or "").split()).casefold()


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _mapping(value) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def item_entailment(item) -> float:
    """证据条目上 Phase 03 核验的 0…1 蕴含分（拿不到返回 0，调用方据此判"未知"）。"""
    metadata = _mapping(_mapping(item).get("metadata"))
    verification = _mapping(_mapping(metadata.get("evidence_layer")).get("verification"))
    dimensions = _mapping(verification.get("dimensions"))
    return _as_float(dimensions.get("entailment"))


def _items(value) -> list:
    return [item for item in (value or []) if isinstance(item, Mapping)]


def _counter(values: Iterable) -> dict:
    counts: dict = {}
    for value in values:
        key = str(value or "")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


# ── P07-01：Gap taxonomy / priority ────────────────────────────────────────
def normalize_claim(row: Mapping, *, index: int = 0) -> dict:
    """把两种 claim 形状归一成缺口分析要的一份事实（**不改原对象**）。

    两种形状都是既有产物，本模块不新造第三种：
      · Phase 06 `qa_evidence_graph.build_layer()` 的 claim row（`text` / `plan_only=false`）；
      · Phase 05 `qa_execution_graph.build_research_plan()` 的 plan claim（`statement` /
        `role` / `plan_only=true`）。
    """
    row = row if isinstance(row, Mapping) else {}
    claim = _mapping(row.get("claim"))
    text = str(row.get("text") or claim.get("text") or row.get("statement") or "").strip()
    claim_id = str(row.get("claim_id") or claim.get("claim_id")
                   or row.get("canonical_id") or ("c%d" % (index + 1)))
    plan_only = bool(row.get("plan_only")) or str(row.get("plan_node_kind") or "") == "claim"
    return {
        "claim_id": claim_id,
        "text": text,
        "claim_type": str(row.get("claim_type") or claim.get("claim_type") or ""),
        "role": str(row.get("role") or ""),
        "scope": [str(item) for item in (row.get("scope") or claim.get("scope") or [])],
        "valid_from": str(row.get("valid_from") or claim.get("valid_from") or ""),
        "valid_to": str(row.get("valid_to") or claim.get("valid_to") or ""),
        "authority_level": _as_int(row.get("authority_level"), 0),
        "verification_status": str(row.get("verification_status")
                                   or claim.get("verification_status") or ""),
        # Phase 03 的 claim 级核验结论（pairs 里有每对"结论 vs 证据"的 verdict/reasons）
        "verification": _verification_of(row),
        "plan_only": plan_only,
        "relations": _mapping(row.get("relations")),
        "verified_support_count": _as_int(row.get("verified_support_count"), 0),
        "verified_refute_count": _as_int(row.get("verified_refute_count"), 0),
        "independent_sources": _as_int(row.get("independent_sources"), 0),
        "support_mass": _as_float(row.get("support_mass")),
        "verified_support_mass": _as_float(row.get("verified_support_mass")),
        "refute_mass": _as_float(row.get("refute_mass")),
    }


def _verification_of(node: Mapping) -> dict:
    value = node.get("verification")
    return dict(value) if isinstance(value, Mapping) else {}


def _row_fact(claim: Mapping, status: str, relation: str) -> dict:
    """claim row 自带的图统计合成的一条"事实"（没有具体 evidence_ref：计数就是它的出处）。"""
    return {
        "evidence_ref": "", "item": None, "status": str(status), "relation": str(relation),
        "verified": True, "strength": 1.0, "relevance": 0.0, "ranking_score": 0.0,
        "authority": _as_int(claim.get("authority_level"), 0), "source_id": "",
        "published_at": "", "reasons": [],
    }


def _evidence_facts(claim: dict, *, edges: Sequence[Mapping], evidence_by_ref: Mapping,
                    evidence_pool: Sequence[Mapping], relevance_floor_value: float) -> dict:
    """一条 claim 的"证据事实"：有几条、几条被核验支持、来源独立性、权威、相关性、理由码。

    三个来源按优先级取（与 Phase 06 的 `edge_status` 同思路：**有核验就用核验**）：
      ① Phase 06 的 claim-evidence 边（带 `metadata.verified` / `graph_relation`）；
      ② Phase 03 的 `verification.pairs`（claim 级核验）；
      ③ 都没有（计划 claim 的常见情形）→ 按词面相关性从证据池现选（规则、可复算）。
    """
    own_edges = [edge for edge in edges if str(edge.get("claim_id") or "") == claim["claim_id"]]
    facts = {
        "evidence": [], "source": "", "verified_supports": [], "qualified_supports": [],
        "refutes": [], "reasons": [], "relevance": 0.0, "relevance_known": False,
        "authority": 0, "independent_sources": 0, "matched": 0, "support_mass": 0.0,
        "verifier_status": "",
    }
    for edge in own_edges:
        reference = str(edge.get("evidence_ref") or "")
        item = evidence_by_ref.get(reference)
        metadata = _mapping(edge.get("metadata"))
        status = str(edge.get("status") or "")
        relation = str(edge.get("graph_relation") or "")
        # 注意单位：边上的 `relevance_score` 是**排序分**（真实数据里 0…200+），
        # 不是 0…1 的相关性。拿它跟相关性下界比是量纲错误，所以另存 `ranking_score`，
        # `relevance` 只在能拿到 0…1 口径（Phase 03 的 entailment）时才认。
        ranking = _as_float(edge.get("relevance_score"))
        entry = {
            "evidence_ref": reference, "item": item, "status": status, "relation": relation,
            "verified": bool(metadata.get("verified")), "strength": _as_float(edge.get("strength")),
            "relevance": _as_float(item_entailment(item)) if item is not None else 0.0,
            "ranking_score": ranking,
            "authority": _as_int(metadata.get("authority_level"),
                                 _as_int((item or {}).get("authority_level"), 0)),
            "source_id": str(metadata.get("source_id") or ""),
            "published_at": str(edge.get("published_at") or (item or {}).get("published_at") or ""),
            "reasons": [str(code) for code in (metadata.get("reasons") or [])],
        }
        facts["evidence"].append(entry)
        facts["reasons"].extend(entry["reasons"])
        if entry["relevance"] > 0:
            facts["relevance"] = max(facts["relevance"], entry["relevance"])
            facts["relevance_known"] = True
        facts["authority"] = max(facts["authority"], entry["authority"])
        if relation == "SUPPORTS" and entry["verified"]:
            facts["verified_supports"].append(entry)
        elif relation == "SUPPORTS" or status == EVIDENCE_STATUS_QUALIFIED:
            facts["qualified_supports"].append(entry)
        elif relation == "REFUTES":
            facts["refutes"].append(entry)
        facts["matched"] += 1
        facts["source"] = facts["source"] or "evidence_graph_edges"
    if own_edges:
        facts["support_mass"] = round(sum(item["strength"] for item in facts["verified_supports"]), 4)
        facts["independent_sources"] = len({item["source_id"] for item in facts["verified_supports"]
                                            if item["source_id"]})
        facts["verifier_status"] = claim["verification_status"]
        return facts

    pairs = _items(_mapping(claim.get("verification")).get("pairs"))
    if pairs:
        for pair in pairs:
            reference = str(pair.get("evidence_ref") or "")
            item = evidence_by_ref.get(reference)
            verdict = str(pair.get("verdict") or "")
            entry = {
                "evidence_ref": reference, "item": item, "status": verdict, "relation": "",
                "verified": verdict == EVIDENCE_STATUS_SUPPORTED,
                "strength": _as_float(pair.get("score")),
                "relevance": _as_float(pair.get("entailment")),
                "authority": _as_int((item or {}).get("authority_level"), 0),
                "source_id": "", "published_at": str((item or {}).get("published_at") or ""),
                "reasons": [str(code) for code in (pair.get("reasons") or [])],
            }
            facts["evidence"].append(entry)
            facts["reasons"].extend(entry["reasons"])
            facts["relevance"] = max(facts["relevance"], entry["relevance"])
            facts["authority"] = max(facts["authority"], entry["authority"])
            if verdict == EVIDENCE_STATUS_SUPPORTED:
                facts["verified_supports"].append(entry)
            elif verdict == EVIDENCE_STATUS_QUALIFIED:
                facts["qualified_supports"].append(entry)
            elif verdict == EVIDENCE_STATUS_REFUTED:
                facts["refutes"].append(entry)
            facts["matched"] += 1
            facts["source"] = facts["source"] or "verifier_pairs"
        facts["support_mass"] = round(sum(item["strength"] for item in facts["verified_supports"]), 4)
        facts["independent_sources"] = len({str((item["item"] or {}).get("source_url")
                                                or (item["item"] or {}).get("source_type") or "")
                                            for item in facts["verified_supports"]} - {""})
        facts["verifier_status"] = claim["verification_status"]
        return facts

    # ③a 计划 claim 的常见情形：没有边也没有核验对，但 claim row 自己带着图统计
    #    （Phase 06 `build_layer()` 的 claim row 就是这种：关系计数/独立来源数都在行上）。
    #    这时**以行上的统计为准**（它才是证据图的产物），不去空证据池里瞎猜。
    row_relations = claim["relations"] if isinstance(claim["relations"], Mapping) else {}
    row_total = sum(_as_int(value) for value in row_relations.values())
    if row_total or claim["verified_support_count"] or claim["verified_refute_count"]:
        for _ in range(claim["verified_support_count"]):
            entry = _row_fact(claim, EVIDENCE_STATUS_SUPPORTED, "SUPPORTS")
            facts["verified_supports"].append(entry)
            facts["evidence"].append(entry)
        for _ in range(claim["verified_refute_count"]):
            entry = _row_fact(claim, EVIDENCE_STATUS_REFUTED, "REFUTES")
            facts["refutes"].append(entry)
            facts["evidence"].append(entry)
        facts["matched"] = max(row_total, claim["verified_support_count"]
                               + claim["verified_refute_count"])
        facts["authority"] = claim["authority_level"]
        facts["independent_sources"] = claim["independent_sources"]
        facts["support_mass"] = (claim["verified_support_mass"] or claim["support_mass"])
        facts["verifier_status"] = claim["verification_status"]
        facts["source"] = "claim_row"
        return facts

    # ③b 计划 claim：按词面相关性从证据池现选（不冒充核验结论，标 matched_by=lexical）
    terms = term_set(claim["text"])
    for item in evidence_pool:
        info = relevance_score(terms, item)
        info = info if isinstance(info, Mapping) else {}
        relevance = _as_float(info.get("relevance"))
        if relevance < relevance_floor_value:
            continue
        layer = _mapping(_mapping(item.get("metadata")).get("evidence_layer"))
        verification = _mapping(layer.get("verification"))
        verdict = str(verification.get("verdict") or "")
        entry = {
            "evidence_ref": str(item.get("evidence_ref") or ""), "item": item,
            "status": verdict, "relation": "", "verified": verdict == EVIDENCE_STATUS_SUPPORTED,
            "strength": _as_float(verification.get("score")),
            "relevance": relevance, "authority": _as_int(item.get("authority_level"), 0),
            "source_id": str(layer.get("source_fingerprint") or ""),
            "published_at": str(item.get("published_at") or ""),
            "reasons": [str(code) for code in (verification.get("reasons") or [])],
        }
        facts["evidence"].append(entry)
        facts["reasons"].extend(entry["reasons"])
        facts["relevance"] = max(facts["relevance"], entry["relevance"])
        facts["authority"] = max(facts["authority"], entry["authority"])
        if verdict == EVIDENCE_STATUS_SUPPORTED:
            facts["verified_supports"].append(entry)
        elif verdict == EVIDENCE_STATUS_REFUTED:
            facts["refutes"].append(entry)
        facts["matched"] += 1
    facts["source"] = "lexical_pool" if facts["matched"] else "none"
    facts["support_mass"] = round(sum(item["strength"] for item in facts["verified_supports"]), 4)
    facts["independent_sources"] = len({item["source_id"] for item in facts["verified_supports"]
                                        if item["source_id"]})
    return facts


def _claim_importance(claim: dict) -> float:
    """缺口对"能不能可靠回答原问题"的权重（§12 的 priority 第一分量之外的那一维）。"""
    role = claim["role"]
    if role == "answer":
        return 1.0
    if role in ("cause", "mechanism"):
        return 0.85
    if role == "link":
        return 0.75
    if role == "counter":
        return 0.65
    return 0.55 if claim["plan_only"] else 0.90


def _expected_causal(claim: dict, *, category: str) -> bool:
    if claim["claim_type"] in ("causal", "mechanism"):
        return True
    if any(marker in claim["text"] for marker in _CAUSAL_MARKERS):
        return True
    return str(category or "") in _CAUSAL_QUESTION_CATEGORIES and not claim["plan_only"]


def _expected_counter(claim: dict, *, category: str) -> bool:
    if any(marker in claim["text"] for marker in _COUNTER_MARKERS):
        return True
    return str(category or "") in _COUNTER_QUESTION_CATEGORIES


def _make_gap(claim: dict, missing: str, *, detail: str, reason: str, facts: dict,
              contradiction: Mapping | None = None) -> dict:
    """造一条 Gap（§12 的形状 + P07-02 的证据要求 + 可复算的优先级分解）。"""
    severity = GAP_SEVERITY.get(missing, 0.5)
    importance = _claim_importance(claim)
    deficit = 1.0 - min(1.0, max(facts.get("support_mass") or 0.0, facts.get("relevance") or 0.0))
    weights = priority_weights()
    priority = _clamp(weights["severity"] * severity
                      + weights["claim_importance"] * importance
                      + weights["evidence_deficit"] * deficit)
    terms = safety_terms()
    haystack = "%s %s" % (claim["text"], " ".join(claim["scope"]))
    safety_critical = any(term and term in haystack for term in terms)
    override = bool(safety_critical and priority < SAFETY_OVERRIDE_PRIORITY)
    if safety_critical:
        priority = max(priority, SAFETY_OVERRIDE_PRIORITY)
    gap_id = "G%s" % _digest("%s|%s|%s" % (claim["claim_id"], missing, detail), 10)
    routes = list(GAP_ROUTE_RULES.get(missing, (QA_ROUTE_KEYWORD,)))
    suggested = suggest_queries(claim, missing, routes=routes)
    gap = {
        "gap_id": gap_id,
        "claim_id": claim["claim_id"],
        "missing": missing,
        "priority": round(priority, 4),
        "band": band_of(priority),
        "status": "open",
        "suggested_queries": suggested,
        "suggested_routes": routes,
        "reason": reason,
        "priority_factors": {
            "severity": round(severity, 4), "claim_importance": round(importance, 4),
            "evidence_deficit": round(deficit, 4),
            "weights": weights,
            "safety_critical": safety_critical, "priority_override": override,
            "formula": ("clamp(severity*w_sev + claim_importance*w_imp + evidence_deficit*w_def)"
                        + (" → max(…, %.2f)（§21 安全关键覆盖）" % SAFETY_OVERRIDE_PRIORITY
                           if safety_critical else "")),
        },
        "safety_critical": safety_critical,
        "priority_override": override,
        "claim_text": claim["text"][:200],
        "plan_only": bool(claim["plan_only"]),
        "origin": str(facts.get("source") or ""),
        "evidence_requirement": evidence_requirement_for(missing, gap_claim=claim, facts=facts),
        "evidence_matched": int(facts.get("matched") or 0),
        "verified_supports": len(facts.get("verified_supports") or []),
        "refutes": len(facts.get("refutes") or []),
        "independent_sources": int(facts.get("independent_sources") or 0),
        "reason_codes": sorted(set(str(code) for code in (facts.get("reasons") or [])))[:10],
        # 这条缺口是**哪个核验理由码**派生的（可追溯：没有理由码就是规则直接判的）
        "derived_from_reasons": sorted(set(str(code) for code in (facts.get("reasons") or [])
                                           if _GAP_TYPES_BY_REASON.get(str(code)) == missing))[:6],
    }
    if isinstance(contradiction, Mapping):
        gap["contradiction"] = {
            "contradiction_id": str(contradiction.get("contradiction_id") or ""),
            "kind": str(contradiction.get("kind") or ""),
            "resolution": str(contradiction.get("resolution") or ""),
            "reason_code": str(contradiction.get("reason_code") or ""),
        }
    return gap


def _rules_for_claim(claim: dict, facts: dict, *, category: str,
                     contradictions: Sequence[Mapping], plan: Mapping | None = None) -> list:
    """一条 claim 上跑一遍缺口规则，返回 [{missing, detail, reason, contradiction}]。

    规则顺序 = 判定优先级（先判"根本没证据"，再判质量/独立性/链接，最后判反证）。
    每条规则都是"事实 → 结论"的纯函数，理由文本里带数字，可复算。
    """
    found: list = []
    total = len(facts["evidence"])
    verified = len(facts["verified_supports"])
    refutes = len(facts["refutes"])
    reasons = set(str(code) for code in facts["reasons"])
    # Phase 03 理由码 → 缺口类型（**缺口不是另算的**：verifier 说"重叠不足"，这里就记 LOW_RELEVANCE）
    derived = {_GAP_TYPES_BY_REASON[code] for code in reasons if code in _GAP_TYPES_BY_REASON}
    facts["derived_types"] = sorted(derived)
    authority_floor_value = authority_floor()
    relevance_floor_value = relevance_floor()
    independent_floor_value = independent_sources_floor()

    if total == 0 or QA_GAP_NO_EVIDENCE in derived:
        found.append({"missing": QA_GAP_NO_EVIDENCE,
                      "detail": "no_evidence",
                      "reason": "这条 Claim 一条证据都没挂上（证据池里没有词面相关的条目）"
                                if total == 0 else
                                "核验理由码 %s：引用的证据缺失/结论没有出处"
                                % "、".join(sorted(reasons & {REASON_MISSING_EVIDENCE,
                                                              REASON_CLAIM_NO_EVIDENCE}))})
        return found

    # 结论与证据"重叠不足/只沾关键词/只沾一部分" → 相关性缺口
    # 触发依据三选一（都能追到出处）：Phase 03 理由码 / 已知的 0…1 相关性低于下界 / 有证据但一条都没被核验
    if (QA_GAP_LOW_RELEVANCE in derived
            or (facts["relevance_known"] and facts["relevance"] < relevance_floor_value)
            or (not verified and facts["matched"] > 0)):
        found.append({
            "missing": QA_GAP_LOW_RELEVANCE, "detail": "relevance_below_floor",
            "reason": "有 %d 条候选但没有一条被核验支持%s%s"
                      % (total,
                         "；已知最高相关性 %.2f（下界 %.2f）" % (facts["relevance"],
                                                               relevance_floor_value)
                         if facts["relevance_known"] else "（相关性未判定）",
                         "；核验理由码 %s" % "、".join(sorted(reasons & _RELEVANCE_REASONS))
                         if reasons & _RELEVANCE_REASONS else "")})
    # 权威不足：只在"有候选、且核验说权威不够"时记
    if QA_GAP_LOW_AUTHORITY in derived or (facts["matched"] and facts["authority"] < authority_floor_value):
        found.append({
            "missing": QA_GAP_LOW_AUTHORITY, "detail": "authority_below_floor",
            "reason": "最高来源级别 %d（下界 %d）%s"
                      % (facts["authority"], authority_floor_value,
                         "；核验理由码含 %s" % "、".join(sorted(reasons & _AUTHORITY_REASONS))
                         if reasons & _AUTHORITY_REASONS else "")})
    # 单一来源：有已核验支持，但独立来源不够（§12 "only one low-quality source"）
    if verified and facts["independent_sources"] < independent_floor_value:
        found.append({
            "missing": QA_GAP_SINGLE_SOURCE, "detail": "independent_sources",
            "reason": "已核验支持 %d 条但只来自 %d 个独立来源（下界 %d）"
                      % (verified, facts["independent_sources"], independent_floor_value)})
    if QA_GAP_MISSING_ENTITY_LINK in derived:
        found.append({
            "missing": QA_GAP_MISSING_ENTITY_LINK, "detail": "entity_not_in_evidence",
            "reason": "核验理由码 %s：结论点名的实体/范围没有在证据里出现"
                      % "、".join(sorted(reasons & {REASON_ENTITY_MISSING}))})
    if reasons & _TIME_REASONS:
        found.append({
            "missing": QA_GAP_MISSING_TIME_LINK, "detail": "time_reason",
            "reason": "核验理由码 %s：证据与结论的时间链条对不上（或时间未知）"
                      % "、".join(sorted(reasons & _TIME_REASONS))})
    unresolved_here = [item for item in contradictions
                       if str(item.get("resolution") or "") == "unresolved"
                       and claim["claim_id"] in [str(cid) for cid in (item.get("claim_ids") or [])]]
    if refutes and verified:
        found.append({
            "missing": QA_GAP_CONTRADICTION, "detail": "support_and_refute",
            "reason": "同一结论既有 %d 条已核验支持又有 %d 条已核验反驳，需要权威口径裁决"
                      % (verified, refutes)})
    elif unresolved_here:
        decision = unresolved_here[0]
        found.append({
            "missing": QA_GAP_CONTRADICTION, "detail": "unresolved_contradiction",
            "reason": "Phase 06 裁决未消解（理由码 %s），需要能给出决定性的第三方口径"
                      % str(decision.get("reason_code") or ""),
            "contradiction": decision})
    if QA_GAP_AMBIGUOUS_ENTITY in derived or _ambiguous_entity(claim, plan):
        found.append({
            "missing": QA_GAP_AMBIGUOUS_ENTITY, "detail": "ambiguous_subject",
            "reason": "结论里点了多个候选主体（%s）却没声明适用范围/实体（scope 为空），"
                      "无法判断它说的是谁" % "、".join(_mentioned_entities(claim, plan)[:4])})
    if (_expected_causal(claim, category=category) or QA_GAP_MISSING_CAUSAL_BRIDGE in derived) \
            and not any(marker in _evidence_text(facts) for marker in _CAUSAL_MARKERS):
        found.append({
            "missing": QA_GAP_MISSING_CAUSAL_BRIDGE, "detail": "no_causal_marker",
            "reason": "问题/结论要的是因果链条，但现有证据里没有出现因果连接词（%s）%s"
                      % ("、".join(_CAUSAL_MARKERS[:5]),
                         "；核验理由码 %s" % REASON_CAUSAL_INFLATION
                         if REASON_CAUSAL_INFLATION in reasons else "")})
    if (_expected_counter(claim, category=category)
            and verified and not refutes
            and not any(marker in _evidence_text(facts) for marker in _COUNTER_MARKERS)):
        found.append({
            "missing": QA_GAP_MISSING_COUNTEREVIDENCE, "detail": "no_counterevidence",
            "reason": "已有 %d 条已核验支持却没有任何反证/风险口径（该题型要求对照）" % verified})
    return found


def _evidence_text(facts: Mapping, limit: int = 6) -> str:
    """把这条 claim 名下证据的标题+正文前段拼起来（判"有没有因果连接词"用，纯词面）。"""
    chunks = []
    for entry in (facts.get("evidence") or [])[:limit]:
        item = entry.get("item") or {}
        chunks.append("%s %s" % (str(item.get("title") or ""),
                                 str(item.get("content_excerpt") or "")[:200]))
    return " ".join(chunks)


def _mentioned_entities(claim: Mapping, plan: Mapping | None) -> list:
    """claim 文本里出现了哪些**计划实体**（不做新 NER：只认计划里已有的实体/主题）。"""
    plan = plan if isinstance(plan, Mapping) else {}
    candidates = [str(item) for item in (plan.get("entities") or []) if str(item).strip()]
    candidates += [str(item) for item in (plan.get("topics") or []) if str(item).strip()]
    text = str(claim.get("text") or "")
    out = []
    for item in candidates:
        if item and item in text and item not in out:
            out.append(item)
    return out


def _ambiguous_entity(claim: Mapping, plan: Mapping | None) -> bool:
    """歧义判定（§12 AMBIGUOUS_ENTITY）：结论点了**多个**计划实体却没声明适用范围。

    能力边界：这是**指代层面的**歧义信号（"这条结论到底说的是哪一家"），不是语义消歧；
    没有任何计划实体时一律判"不歧义"（没有依据就不下结论）。
    """
    mentioned = _mentioned_entities(claim, plan)
    return len(mentioned) >= 2 and not [item for item in (claim.get("scope") or []) if str(item).strip()]


def detect_gaps(claims: Sequence[Mapping], *, edges: Sequence[Mapping] = (),
                evidence: Sequence[Mapping] = (), contradictions: Sequence[Mapping] = (),
                category: str = "", plan: Mapping | None = None,
                limit: int | None = None) -> dict:
    """P07-01：把"当前有哪些必要 Claim、各自缺什么证据"算成 Gap 清单。

    入参都是既有产物（plan claim / Phase 06 claim row、Phase 06 边、证据包、Phase 06 裁决、
    Phase 05 研究计划）；返回 `{"gaps": [...], "stats": {...}}`：gaps 按 (priority 降序,
    gap_id 升序) 排好，单条 claim 最多留 `QA_GAP_MAX_GAPS_PER_CLAIM` 条（只留最要紧的），
    总数受 `QA_GAP_MAX_GAPS` 限制。
    """
    edges = list(edges or [])
    evidence_pool = list(evidence or [])
    evidence_by_ref = {str(item.get("evidence_ref") or ""): item for item in evidence_pool}
    relevance_floor_value = relevance_floor()
    # 类别优先取显式入参，其次从计划里读（两种调用方式给同一个答案）
    category = str(category or category_of(plan) or "")
    normalized = []
    for index, row in enumerate(claims or []):
        normalized.append(normalize_claim(row, index=index))
    per_claim_cap = max_gaps_per_claim()
    gaps: list = []
    for claim in normalized:
        if not claim["text"]:
            continue
        facts = _evidence_facts(claim, edges=edges, evidence_by_ref=evidence_by_ref,
                                evidence_pool=evidence_pool,
                                relevance_floor_value=relevance_floor_value)
        found = _rules_for_claim(claim, facts, category=category,
                                 contradictions=contradictions, plan=plan)
        made = [_make_gap(claim, item["missing"], detail=item["detail"], reason=item["reason"],
                          facts=facts, contradiction=item.get("contradiction"))
                for item in found]
        made.sort(key=lambda gap: (-gap["priority"], gap["gap_id"]))
        gaps.extend(made[:per_claim_cap])
    gaps.sort(key=lambda gap: (-gap["priority"], gap["gap_id"]))
    total = len(gaps)
    cap = int(limit if limit is not None else max_gaps())
    gaps = gaps[:cap]
    by_type = _counter(gap["missing"] for gap in gaps)
    return {
        "gaps": gaps,
        "stats": {
            "analyzer_version": GAP_ANALYZER_VERSION,
            "claims": len(normalized),
            "plan_claims": len([claim for claim in normalized if claim["plan_only"]]),
            "gaps": len(gaps),
            "gaps_total_before_cap": total,
            "truncated": max(0, total - len(gaps)),
            "by_type": by_type,
            "by_band": _counter(gap["band"] for gap in gaps),
            "high_priority": len([gap for gap in gaps if is_high_priority(gap["priority"])]),
            "safety_critical": len([gap for gap in gaps if gap["safety_critical"]]),
            "top_priority": gaps[0]["priority"] if gaps else None,
            "priority_threshold": priority_threshold(),
            "no_gain_rounds": no_gain_rounds(),
            "gaps_by_claim": _counter(gap["claim_id"] for gap in gaps),
        },
    }


# ── P07-02：suggested route / evidence requirement ──────────────────────────
def routes_for(missing: str) -> list:
    """缺口类型 → 建议通道（既有 7 个取值；查不到就给 keyword 这一个兜底）。"""
    routes = list(GAP_ROUTE_RULES.get(str(missing), (QA_ROUTE_KEYWORD,)))
    return [route for route in routes if route in QA_RETRIEVAL_ROUTES] or [QA_ROUTE_KEYWORD]


def hunters_for(route: str) -> list:
    return list(HUNTER_BY_ROUTE.get(str(route), ()))


def evidence_requirement_for(missing: str, *, gap_claim: Mapping | None = None,
                             facts: Mapping | None = None) -> dict:
    """P07-02：这条缺口到底缺哪类证据、缺到什么程度（§7 Evidence Requirement 口径）。

    `satisfied_by` **本轮一律留空**：它是"哪个节点负责满足它"的声明位（Phase 05 定的字段），
    真实满足度由下一轮的缺口重算来证明（`status=resolved`），不在这里自证。
    """
    missing = str(missing)
    claim = gap_claim if isinstance(gap_claim, Mapping) else {}
    facts = facts if isinstance(facts, Mapping) else {}
    need_independent = missing == QA_GAP_SINGLE_SOURCE
    require_counter = missing == QA_GAP_MISSING_COUNTEREVIDENCE
    min_authority = authority_floor() if missing in (QA_GAP_LOW_AUTHORITY, QA_GAP_CONTRADICTION) else 0
    return {
        "requirement_id": "req:%s" % _digest("%s|%s" % (claim.get("claim_id") or "", missing), 10),
        "plan_node_kind": "evidence_requirement",
        "sub_question_id": str(claim.get("sub_question_id") or ""),
        "evidence_type": EVIDENCE_TYPE_BY_GAP.get(missing, "any_evidence"),
        "missing": missing,
        "min_independent_sources": independent_sources_floor() if need_independent else 1,
        "min_authority_level": min_authority,
        "require_verified": True,
        "require_counterevidence": require_counter,
        "expected_evaluation": "next_round_gap_recompute",
        "rationale": _requirement_rationale(missing, facts),
        "satisfied_by": "",
    }


def _requirement_rationale(missing: str, facts: Mapping) -> str:
    verified = len(facts.get("verified_supports") or [])
    found = int(facts.get("matched") or 0)
    return {
        QA_GAP_NO_EVIDENCE: "一条相关证据都没有：至少要拿到 1 条能覆盖结论实词的证据",
        QA_GAP_LOW_RELEVANCE: "现有 %d 条候选都没覆盖结论要点：要能覆盖结论实词的原文段落" % found,
        QA_GAP_LOW_AUTHORITY: "现有证据来源级别不够：要官方原文/官方解读级别的来源",
        QA_GAP_SINGLE_SOURCE: "只有 %d 个独立来源：要 ≥%d 个互不相同的来源交叉"
                              % (int(facts.get("independent_sources") or 0),
                                 independent_sources_floor()),
        QA_GAP_MISSING_ENTITY_LINK: "结论点名的实体没在证据里出现：要有实体级的关联证据",
        QA_GAP_MISSING_TIME_LINK: "时间链条对不上：要带生效时间/发布时间的证据",
        QA_GAP_CONTRADICTION: "支持与反驳并存：要能给出决定性口径的来源（级别/时间/样本）",
        QA_GAP_AMBIGUOUS_ENTITY: "指代不明确：要能把实体定位到具体对象的证据",
        QA_GAP_MISSING_CAUSAL_BRIDGE: "缺因果链条：要有'因为/导致/推动'这类连接证据",
        QA_GAP_MISSING_COUNTEREVIDENCE: "全是支持没有反证：要风险/反例/相反口径的证据",
    }.get(missing, "要补充能覆盖这条缺口的证据（已核验支持 %d 条）" % verified)


def suggest_queries(claim: Mapping, missing: str, *, routes: Sequence[str] = (),
                    limit: int = 3) -> list:
    """P07-02：给这条缺口生成建议查询（**纯规则**：claim 实词 + 缺口类型后缀）。

    §12 说 `suggested_queries` 由 Gap Analyzer 给出；本仓库不许调模型，所以落地为
    "结论实词 + 模板后缀"的确定性拼装（模块头第 1 条写了能力边界）。
    """
    text = str(claim.get("text") or "").strip()
    if not text:
        return []
    # 查询清理（纯规则）：去掉句尾的括注（计划 claim 里常有"（§7 的 H5：……）"这类注解），
    # 它进查询只会污染召回；去掉后为空就保留原文（宁可脏一点，也不能没有查询）。
    text = re.sub(r"[（(][^（）()]{0,80}[）)]\s*$", "", text).strip() or text
    suffix = QUERY_SUFFIX_BY_GAP.get(str(missing), "")
    queries = [text]
    if suffix:
        queries.append("%s %s" % (text, suffix))
    scope = [str(item) for item in (claim.get("scope") or []) if str(item).strip()]
    if scope:
        queries.append("%s %s %s" % (text, scope[0], suffix or "证据"))
    out = []
    for query in queries:
        clean = " ".join(query.split())[:120]
        if clean and clean not in out:
            out.append(clean)
    return out[:limit]


# ── P07-03：Next-hop Planner（规则实现 + 可插拔注入点）──────────────────────
_PLANNERS: dict = {}


def register_next_hop_planner(name: str, fn: Callable) -> None:
    """注册**可插拔**下一跳规划器（§13 允许"更聪明的下一跳"，本仓库留注入点）。

    本轮不注册任何模型后端（GPU 停用、硬约束不许调模型）；接口给后续阶段/私有部署：
    `fn(gaps, **kwargs) -> Sequence[Mapping]`，每项至少要能给出 `gap_id`/`question`/`queries`。
    名字非法、抛错、返回值非法 → 一律保守回落到规则实现并记账（绝不假装规划过）。
    """
    clean = str(name or "").strip()
    if not clean or not callable(fn):
        raise ValueError("规划器名字与可调用对象必填")
    _PLANNERS[clean] = fn


def next_hop_planners() -> tuple:
    return tuple(sorted(_PLANNERS))


def planner_name() -> str:
    return str(os.environ.get("QA_NEXT_HOP_PLANNER", "rule") or "rule").strip() or "rule"


def _route_plan_overrides(route: str, *, question: str, claim: Mapping,
                          plan: Mapping | None) -> dict:
    """把 route 翻成**真实的检索计划覆盖**（同一条检索链路的三个不同着力点）。

    · `policy_exact` / `graph_attribute` → 用计划里的实体/主题（结构化与属性通道按实体查）；
    · `graph` → 用实体 + 关系词（图谱通道按实体对查）；
    · `semantic` → 用 `qa_query_normalize.expand_terms` 的同义/近义扩展（不调嵌入端点，
      只做词表扩展——语义通道本身在没有查询编码器时会降级，见 Phase 04 D-018）；
    · `web` → 把问题原样交给外部检索（既有 web_search 通道）。
    """
    plan = plan if isinstance(plan, Mapping) else {}
    entities = [str(item) for item in (plan.get("entities") or []) if str(item).strip()][:8]
    topics = [str(item) for item in (plan.get("topics") or []) if str(item).strip()][:8]
    claim_scope = [str(item) for item in (claim.get("scope") or []) if str(item).strip()]
    overrides = {"queries": [], "entities": [], "terms": [], "route": str(route)}
    if route in (QA_ROUTE_POLICY_EXACT, QA_ROUTE_GRAPH_ATTRIBUTE):
        overrides["entities"] = list(dict.fromkeys([*claim_scope, *entities]))
        overrides["queries"] = [question]
    elif route == QA_ROUTE_GRAPH:
        overrides["entities"] = list(dict.fromkeys([*claim_scope, *entities, *topics]))
        overrides["queries"] = [question]
    elif route == QA_ROUTE_SEMANTIC:
        try:
            from qa_query_normalize import expand_terms

            overrides["terms"] = [str(item) for item in expand_terms([question]) if str(item).strip()][:8]
        except Exception:          # noqa: BLE001 扩展不可用就用原查询（不阻断）
            overrides["terms"] = []
        overrides["queries"] = [question]
    elif route == QA_ROUTE_PAGE_CONTEXT:
        overrides["queries"] = [question]
    elif route == QA_ROUTE_WEB:
        overrides["queries"] = [question]
    else:
        overrides["queries"] = [question]
        overrides["entities"] = list(dict.fromkeys([*claim_scope, *entities]))
    return overrides


def _rule_plan(gaps: Sequence[Mapping], *, plan: Mapping | None = None,
               round_index: int = 0, limit: int = 1,
               claims_by_id: Mapping | None = None) -> list:
    """规则版下一跳：按优先级取缺口，一条缺口 → 一跳（§13 的 "Gap → Best Retrieval Action"）。"""
    claims_by_id = claims_by_id if isinstance(claims_by_id, Mapping) else {}
    hops = []
    cap = max(0, int(limit))
    if cap <= 0:
        return hops
    for gap in gaps or []:
        if not isinstance(gap, Mapping):
            continue
        if len(hops) >= cap:
            break
        claim = claims_by_id.get(str(gap.get("claim_id") or "")) or {}
        routes = [str(route) for route in (gap.get("suggested_routes") or [])]
        route = routes[0] if routes else QA_ROUTE_KEYWORD
        queries = [str(query) for query in (gap.get("suggested_queries") or []) if str(query).strip()]
        question = queries[0] if queries else str(claim.get("text") or gap.get("claim_text") or "")
        if not question:
            continue
        requirement = gap.get("evidence_requirement") if isinstance(
            gap.get("evidence_requirement"), Mapping) else {}
        hops.append({
            "hop_id": "g%s" % _digest("%s|%s" % (gap.get("gap_id"), route), 8),
            "gap_id": str(gap.get("gap_id") or ""),
            "round_index": int(round_index),
            "question": question[:200],
            "queries": queries[:4] or [question[:200]],
            "route": route,
            "routes": routes,
            "priority": _clamp(gap.get("priority")),
            "reason": "缺口 %s（%s，优先级 %.2f）：%s"
                      % (gap.get("gap_id"), gap.get("missing"), _clamp(gap.get("priority")),
                         str(gap.get("reason") or "")[:160]),
            "evidence_requirement": dict(requirement),
            "plan_overrides": _route_plan_overrides(route, question=question, claim=claim, plan=plan),
            "hunters": hunters_for(route),
        })
    return hops


def plan_next_hops(gaps: Sequence[Mapping], *, plan: Mapping | None = None,
                   claims: Sequence[Mapping] = (), round_index: int = 0,
                   limit: int | None = None, seen_queries: Iterable = (),
                   seen_sources: Iterable = (), planner: Callable | None = None) -> dict:
    """P07-03 + P07-04：缺口 → 下一跳（含 seen 去重），返回带记账的回执。

    规划器优先级：入参 `planner` > `QA_NEXT_HOP_PLANNER` 注册的后端 > 规则实现。
    后端异常/返回非法 → **保守回落**到规则实现，并把原因写进 `fallback`（不假装规划过）。
    """
    cap = max_next_hops() if limit is None else max(0, int(limit))
    claims_by_id = {}
    for index, row in enumerate(claims or []):
        claim = normalize_claim(row, index=index)
        claims_by_id[claim["claim_id"]] = claim
    backend = planner
    backend_label = "rule"
    fallback: dict = {"used": False, "reason": "", "backend": ""}
    if backend is None:
        name = planner_name()
        if name != "rule":
            backend = _PLANNERS.get(name)
            backend_label = "registered:%s" % name
            if backend is None:
                fallback = {"used": True, "backend": name,
                            "reason": "未注册的下一跳规划器：%s（已回落规则实现）" % name}
                backend_label = "rule"
    hops: list = []
    if callable(backend):
        try:
            raw = backend(list(gaps or []), plan=plan, round_index=round_index, limit=cap,
                          claims=list(claims or []))
            validated = _validate_planned_hops(raw)
            if validated is None:
                fallback = {"used": True, "backend": backend_label,
                            "reason": "规划器返回非法载荷（需要可迭代、每项含 gap_id/question/queries）"}
            else:
                hops = validated[:cap]
        except Exception as exc:  # noqa: BLE001  规划器坏掉不能让循环停摆
            fallback = {"used": True, "backend": backend_label,
                        "reason": "%s: %s" % (type(exc).__name__, str(exc)[:120])}
            hops = []
    if not hops and (not callable(backend) or fallback.get("used")):
        hops = _rule_plan(gaps, plan=plan, round_index=round_index, limit=cap,
                          claims_by_id=claims_by_id)
        if fallback.get("used"):
            backend_label = "rule"
    kept, dedupe = dedupe_next_hops(hops, seen_queries=seen_queries, seen_sources=seen_sources,
                                    plan=plan)
    return {
        "planner_version": NEXT_HOP_PLANNER_VERSION,
        "planner": backend_label,
        "fallback": fallback,
        "hops": kept,
        "dropped": dedupe["dropped"],
        "dedupe": dedupe,
    }


def _validate_planned_hops(raw):
    """校验规划器返回值：可迭代 + 每项含 gap_id/question/queries/route。

    返回 None 表示非法（调用方回落规则实现）；返回 list 表示合法（缺 route 时补 keyword）。
    """
    if raw is None or isinstance(raw, (str, bytes, Mapping)):
        return None
    try:
        items = list(raw)
    except TypeError:
        return None
    hops = []
    for item in items:
        if not isinstance(item, Mapping):
            return None
        gap_id = str(item.get("gap_id") or "")
        question = str(item.get("question") or "")
        queries = item.get("queries")
        if not gap_id or not question:
            return None
        if isinstance(queries, (str, bytes)) or queries is None:
            return None
        queries = [str(query) for query in queries if str(query).strip()]
        if not queries:
            return None
        route = str(item.get("route") or "")
        if route not in QA_RETRIEVAL_ROUTES:
            route = QA_ROUTE_KEYWORD
        hop = dict(item)
        hop.update({"hop_id": str(item.get("hop_id") or ("g%s" % _digest(
                        "%s|%s" % (gap_id, route), 8))),
                    "gap_id": gap_id, "question": question[:200], "queries": queries[:4],
                    "route": route})
        hops.append(hop)
    return hops


# ── P07-04：seen dedupe ────────────────────────────────────────────────────
def query_fingerprint(question, *, route: str = "", constraints: Iterable = (),
                      corpus_version: str = "", retrieval_config: str = "",
                      normalized: str = "") -> str:
    """§24 的 Query Fingerprint：normalized_query + constraints + corpus_version + retrieval_config。

    `constraints` 取"会影响召回结果"的那几个（route/时间窗/实体），排序后拼接——
    同样的问题在不同 route/不同时间窗下**不是**同一个指纹（否则去重会把有效重检也砍掉）。
    """
    body = "|".join([
        _norm(normalized or question),
        str(route or ""),
        ",".join(sorted(_norm(item) for item in (constraints or ()) if str(item).strip())),
        str(corpus_version or ""),
        str(retrieval_config or ""),
    ])
    return _digest(body, 16)


def dedupe_next_hops(hops: Sequence[Mapping], *, seen_queries: Iterable = (),
                     seen_sources: Iterable = (), plan: Mapping | None = None,
                     corpus_version: str = "", retrieval_config: str = "") -> tuple:
    """P07-04：下一跳去重（**复用 Phase 02 的 seen 机制，不另造一套**）。

    三件事，全部留痕：
      ① 同一批规划里重复的查询（同指纹）→ 只留第一条；
      ② 与本轮/历史已经搜过的查询指纹相同 → 丢掉（不重复搜同一句话）；
      ③ 缺口点名的 claim 其"已见且被拒"的来源（Phase 02 `qa_evidence_seen.status=rejected`）
         已经覆盖这条查询 → 丢掉（MASTER_RULES 第 14 条：被拒证据仍属于 seen）。
    """
    constraints = [str(item) for item in ((plan or {}).get("entities") or [])][:8]
    seen_fp = {str(item) for item in (seen_queries or ()) if str(item)}
    rejected = {str(item) for item in (seen_sources or ()) if str(item)}
    kept, dropped = [], []
    for hop in hops or []:
        if not isinstance(hop, Mapping):
            continue
        fingerprint = query_fingerprint(hop.get("question"), route=str(hop.get("route") or ""),
                                        constraints=constraints, corpus_version=corpus_version,
                                        retrieval_config=retrieval_config)
        entry = dict(hop)
        entry["query_fingerprint"] = fingerprint
        if fingerprint in seen_fp:
            dropped.append({"hop_id": str(hop.get("hop_id") or ""),
                            "gap_id": str(hop.get("gap_id") or ""),
                            "query_fingerprint": fingerprint,
                            "reason": "seen_query（同 route 同约束的查询已经搜过）"})
            continue
        gap_sources = {str(item) for item in (hop.get("evidence_refs") or [])}
        if gap_sources and gap_sources <= rejected:
            dropped.append({"hop_id": str(hop.get("hop_id") or ""),
                            "gap_id": str(hop.get("gap_id") or ""),
                            "query_fingerprint": fingerprint,
                            "reason": "seen_rejected_source（该缺口的来源已见且被拒，不再重搜）"})
            continue
        seen_fp.add(fingerprint)
        kept.append(entry)
    return kept, {
        "checked": len(list(hops or [])), "kept": len(kept), "dropped": dropped,
        "dropped_count": len(dropped), "seen_queries": len(seen_fp),
        "rejected_sources": len(rejected),
        "mode": "gap_next_hop_dedupe（复用 Phase 02 qa_evidence_seen）",
    }


# ── P07-05 / P07-06：no-gain 收敛与停止原因 ────────────────────────────────
def decide_stop_reason(*, no_gain_streak: int, no_gain_threshold: int, high_priority_open: int,
                       unresolved_contradictions: int, actionable_hops: int,
                       depth_exhausted: bool, budget_exhausted: bool,
                       evidence_insufficient: bool = False) -> dict:
    """P07-06：五值停止原因的唯一决策点（§14；取值域 = Phase 01 冻结的 `QA_STOP_REASONS`）。

    顺序（按"什么真正让循环停下来"排；越具体越靠前）：
      1. `UNRESOLVABLE_CONTRADICTION`：有未消解矛盾且已无可用下一跳（再搜也裁不了）；
      2. `ANSWERABLE`：目标已达成——没有高优缺口了（无论预算/深度如何，这就是结论）；
      3. `BUDGET_EXHAUSTED`：目标未达成且预算用尽（硬约束，如实说）；
      4. `NO_GAIN`：目标未达成且连续若干轮无新增有效证据、无高优缺口被解决（§14 原话）；
      5. `MAX_DEPTH`：目标未达成且跳数/深度到顶；
      6. 兜底 `NO_GAIN`：还有高优缺口却拿不出任何可用下一跳。
    返回 `{stop_reason, detail, factors}`；`stop_reason=""` 表示"还能继续"。
    """
    factors = {
        "no_gain_streak": int(no_gain_streak), "no_gain_threshold": int(no_gain_threshold),
        "high_priority_open": int(high_priority_open),
        "unresolved_contradictions": int(unresolved_contradictions),
        "actionable_hops": int(actionable_hops), "depth_exhausted": bool(depth_exhausted),
        "budget_exhausted": bool(budget_exhausted),
        "evidence_insufficient": bool(evidence_insufficient),
    }
    if actionable_hops > 0 and not depth_exhausted and not budget_exhausted:
        return {"stop_reason": "", "detail": "仍有可用下一跳，循环继续", "factors": factors}
    if unresolved_contradictions > 0 and actionable_hops == 0:
        return {"stop_reason": QA_STOP_UNRESOLVABLE_CONTRADICTION,
                "detail": ("%d 条矛盾无法消解（Phase 06 裁决 unresolved）且已无可用下一跳："
                           "规则裁决能比的八项都比过了，再检索改变不了结论——按 §15 保留不确定性"
                           % int(unresolved_contradictions)),
                "factors": factors}
    if int(high_priority_open) == 0:
        return {"stop_reason": QA_STOP_ANSWERABLE,
                "detail": "没有高优缺口了：证据足以回答（不代表答案一定确定）",
                "factors": factors}
    if budget_exhausted:
        return {"stop_reason": QA_STOP_BUDGET_EXHAUSTED,
                "detail": "预算用尽（墙钟/轮次），停在做完的跳上并如实标注缺口",
                "factors": factors}
    if int(no_gain_streak) >= max(1, int(no_gain_threshold)):
        return {"stop_reason": QA_STOP_NO_GAIN,
                "detail": ("连续 %d 轮 new_verified_claims=0 且 resolved_high_priority_gaps=0"
                           "（§14 的 STOP_NO_GAIN 条件）") % int(no_gain_streak),
                "factors": factors}
    if depth_exhausted:
        return {"stop_reason": QA_STOP_MAX_DEPTH,
                "detail": ("还有 %d 个高优缺口，但跳数/深度到顶（QA_GAP_MAX_NEXT_HOPS 已用完，"
                           "且运行期跳数硬上限 MAX_HOPS_HARD=5）") % int(high_priority_open),
                "factors": factors}
    return {"stop_reason": QA_STOP_NO_GAIN,
            "detail": "还有 %d 个高优缺口，却拿不出任何可用下一跳（规划器无输出或全被去重）"
                      % int(high_priority_open),
            "factors": factors}


class GapLoopState:
    """缺口循环状态机（P07-05/P07-06 的载体）：**纯规则、无副作用、可单测**。

    用法（`qa_pipeline._run_multi_hop` 就是这么用的）：

        state = GapLoopState(rounds_limit=3, budget_seconds=25, started=time.monotonic())
        state.observe(round_index=0, claims=plan_claims, evidence=merged, new_refs=[...])
        ...
        receipt = state.finalize(budget_exhausted=..., depth_exhausted=...)

    每一轮 `observe` 都会重算缺口并与上一轮做差，产出 `new_verified_claims` /
    `resolved_high_priority_gaps` / `dedupe_hits` 等逐轮事实（这是 NO_GAIN 的唯一依据）。
    """

    def __init__(self, *, rounds_limit: int = MAX_HOP_ROUNDS, budget_seconds: float = 0.0,
                 started: float | None = None, clock: Callable | None = None,
                 threshold: float | None = None, no_gain_threshold: int | None = None,
                 category: str = "", plan: Mapping | None = None,
                 corpus_version: str = "", retrieval_config: str = ""):
        self.rounds_limit = max(1, int(rounds_limit or MAX_HOP_ROUNDS))
        self.budget_seconds = float(budget_seconds or 0.0)
        self.clock = clock or _monotonic
        self.started = float(self.clock() if started is None else started)
        self.threshold = priority_threshold() if threshold is None else float(threshold)
        self.no_gain_threshold = no_gain_rounds() if no_gain_threshold is None else int(no_gain_threshold)
        self.category = str(category or "")
        self.plan = dict(plan) if isinstance(plan, Mapping) else {}
        self.corpus_version = str(corpus_version or "")
        self.retrieval_config = str(retrieval_config or "")
        self.rounds: list = []
        self.gaps: list = []
        self.previous_gaps: dict = {}
        self.seen_queries: set = set()
        # 「本来就已经搜过」的查询指纹（外部基线：真机留痕 / 计划 hop 问题）；
        # 与 planned 指纹分开记，才能把"去重命中已搜查询"与"同一缺口的下一跳重复规划"分开报。
        self.searched_baseline: set = set()
        self.seen_rejected_sources: set = set()
        self.planned_hops: list = []
        self.dropped_audit: list = []
        self.dedupe_hits = 0
        self.stop: dict = {"stop_reason": "", "detail": "", "factors": {}}
        self.claims: list = []
        self.evidence_refs: set = set()
        self.verified_claims: set = set()
        self.contradictions: list = []
        self.last_round: dict = {}
        self.errors: list = []          # 分析/规划失败的留痕（失败绝不能静默）

    def record_error(self, where: str, error) -> None:
        """记一条失败（**不抛**）：缺口分析坏了要在回执里看得见，而不是悄悄少一段。"""
        self.errors.append({"where": str(where), "error": str(error)[:200],
                            "round_index": len(self.rounds)})

    # —— 观察一轮 ——
    def observe(self, *, round_index: int, claims: Sequence[Mapping], evidence: Sequence[Mapping],
                edges: Sequence[Mapping] = (), contradictions: Sequence[Mapping] = (),
                new_refs: Iterable = (), dropped_hops: int = 0) -> dict:
        """算这一轮的缺口事实并与上一轮做差（返回本轮回执）。"""
        analysis = detect_gaps(claims, edges=edges, evidence=evidence,
                               contradictions=contradictions,
                               category=self.category, plan=self.plan, limit=None)
        gaps = analysis["gaps"]
        current = {gap["gap_id"]: gap for gap in gaps}
        resolved = [gap for gap_id, gap in self.previous_gaps.items() if gap_id not in current]
        resolved_high = [gap for gap in resolved if is_high_priority(gap["priority"],
                                                                    threshold=self.threshold)]
        refs = {str(item.get("evidence_ref") or "") for item in (evidence or [])
                if isinstance(item, Mapping)}
        new_refs = {str(item) for item in (new_refs or ()) if str(item)}
        previous_refs = self.evidence_refs
        fresh_refs = refs - previous_refs
        self.evidence_refs = refs
        self.claims = [normalize_claim(row, index=index) for index, row in enumerate(claims or [])]
        verified_now = self._verified_claim_ids(self.claims, evidence)
        newly_verified = verified_now - self.verified_claims
        self.verified_claims = verified_now
        self.previous_gaps = current
        self.gaps = gaps
        if contradictions is not None:
            self.contradictions = [dict(item) for item in contradictions
                                   if isinstance(item, Mapping)]
        unresolved = self.unresolved_contradictions()
        high_open = [gap for gap in gaps if is_high_priority(gap["priority"],
                                                             threshold=self.threshold)]
        # 第 0 轮是**基线**（还没有"上一轮"可比）：不计入连续无增益，
        # §14 的"连续两轮"从第 1 轮起算 —— 否则"初始检索没拿到东西"会被算成一次无增益。
        baseline = len(self.rounds) == 0
        no_gain = bool(not baseline and len(newly_verified) == 0 and len(resolved_high) == 0)
        previous_streak = int(self.last_round.get("no_gain_streak") or 0)
        streak = (previous_streak + 1) if no_gain else 0
        receipt = {
            "round_index": int(round_index),
            "open_gaps": len(gaps),
            "high_priority_gaps": len(high_open),
            # 本轮真正"没见过"的证据条数（跨轮自己算，不依赖调用方报数）
            "new_evidence": len(fresh_refs),
            "new_refs_reported": len(new_refs),
            "new_verified_claims": len(newly_verified),
            "newly_verified_claim_ids": sorted(newly_verified)[:10],
            "resolved_gaps": len(resolved),
            "resolved_high_priority_gaps": len(resolved_high),
            "resolved_gap_ids": [gap["gap_id"] for gap in resolved_high][:10],
            "unresolved_contradictions": unresolved,
            "planned_hops": 0,
            "skipped_hops": int(dropped_hops or 0),
            "dedupe_hits": int(self.dedupe_hits),
            "no_gain": bool(no_gain),
            "no_gain_streak": int(streak),
            "baseline": bool(baseline),
            "by_type": analysis["stats"]["by_type"],
            "stop_reason": "",
            "detail": "",
            "gap_ids": [gap["gap_id"] for gap in gaps][:20],
        }
        self.rounds.append(receipt)
        self.last_round = receipt
        return receipt

    @staticmethod
    def _verified_claim_ids(claims: Sequence[Mapping], evidence: Sequence[Mapping]) -> set:
        """哪些 claim 至少拿到一条"已核验支持"（Phase 03 verdict 是唯一真源）。"""
        pool = [item for item in (evidence or []) if isinstance(item, Mapping)]
        supported_refs = set()
        for item in pool:
            layer = _mapping(_mapping(item.get("metadata")).get("evidence_layer"))
            verdict = str(_mapping(layer.get("verification")).get("verdict") or "")
            if verdict == EVIDENCE_STATUS_SUPPORTED:
                supported_refs.add(str(item.get("evidence_ref") or ""))
        verified = set()
        for index, row in enumerate(claims or []):
            claim = normalize_claim(row, index=index)
            if not claim["text"]:
                continue
            verification = _verification_of(row if isinstance(row, Mapping) else {})
            pairs = _items(verification.get("pairs"))
            if any(str(pair.get("verdict") or "") == EVIDENCE_STATUS_SUPPORTED for pair in pairs):
                verified.add(claim["claim_id"])
                continue
            if claim["verified_support_count"] > 0:
                verified.add(claim["claim_id"])
                continue
            terms = term_set(claim["text"])
            for item in pool:
                if str(item.get("evidence_ref") or "") not in supported_refs:
                    continue
                if _as_float(relevance_score(terms, item).get("relevance")) >= relevance_floor():
                    verified.add(claim["claim_id"])
                    break
        return verified

    def seed_searched(self, queries: Iterable) -> int:
        """登记"本来就已经搜过"的查询指纹基线（真机留痕 / 计划 hop 问题）。

        与 `seen_queries` 的关系：`seen_queries` 是**去重用的全量集合**（基线 + 本轮已规划），
        `searched_baseline` 只装外部基线 —— 两者分开才能把"去重命中了已经搜过的查询"与
        "同一缺口的下一跳在下一轮被重复规划"分开报账（前者才是 seen 去重的真实收益）。
        """
        added = 0
        for item in queries or ():
            clean = str(item or "")
            if not clean:
                continue
            self.searched_baseline.add(clean)
            self.seen_queries.add(clean)
            added += 1
        return added

    # —— 缺口 → 下一跳 ——
    def next_hops(self, *, plan: Mapping | None = None, limit: int | None = None,
                  planner: Callable | None = None) -> dict:
        """对当前 open 缺口规划下一跳（含 seen 去重），并把记账并回状态。"""
        live = self.open_gaps()
        plan_result = plan_next_hops(live, plan=plan, claims=self.claims,
                                     round_index=len(self.rounds), limit=limit,
                                     seen_queries=self.seen_queries,
                                     seen_sources=self.seen_rejected_sources, planner=planner)
        # 给每条被丢掉的下一跳标上"为什么被丢"：已搜过的查询 vs 同一缺口的重复规划
        for item in plan_result["dedupe"].get("dropped") or []:
            item["basis"] = ("already_searched"
                             if str(item.get("query_fingerprint") or "") in self.searched_baseline
                             else "already_planned")
            self.dropped_audit.append(dict(item))
        for hop in plan_result["hops"]:
            self.seen_queries.add(str(hop.get("query_fingerprint") or ""))
        self.dedupe_hits += int(plan_result["dedupe"].get("dropped_count") or 0)
        self.planned_hops.extend(plan_result["hops"])
        if self.rounds:
            self.rounds[-1]["planned_hops"] = len(plan_result["hops"])
            self.rounds[-1]["skipped_hops"] = int(plan_result["dedupe"].get("dropped_count") or 0)
            self.rounds[-1]["dedupe_hits"] = int(self.dedupe_hits)
        return plan_result

    def open_gaps(self) -> list:
        return [gap for gap in self.gaps if gap.get("status") == "open"]

    def _dropped_by_basis(self) -> dict:
        """被 seen 去重丢掉的下一跳按原因分账（已搜过 / 同一缺口重复规划）。"""
        counts: dict = {}
        for item in self.dropped_audit:
            key = str(item.get("basis") or "unknown")
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda entry: (-entry[1], entry[0])))

    def high_priority_open(self) -> list:
        return [gap for gap in self.open_gaps()
                if is_high_priority(gap["priority"], threshold=self.threshold)]

    def unresolved_contradictions(self) -> int:
        """未消解矛盾数（直接引 Phase 06 的裁决结论，不自己再判一次）。"""
        count = 0
        seen_ids = set()
        for item in self.contradictions:
            if str(item.get("resolution") or "") != "unresolved":
                continue
            cid = str(item.get("contradiction_id") or "")
            if cid and cid in seen_ids:
                continue
            seen_ids.add(cid)
            count += 1
        for gap in self.gaps:
            if gap.get("missing") != QA_GAP_CONTRADICTION:
                continue
            decision = _mapping(gap.get("contradiction"))
            if str(decision.get("resolution") or "") != "unresolved":
                continue
            cid = str(decision.get("contradiction_id") or gap["gap_id"])
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            count += 1
        return count

    def budget_exhausted(self) -> bool:
        if self.budget_seconds <= 0:
            return False
        return (self.clock() - self.started) >= self.budget_seconds

    def remaining_seconds(self) -> float:
        if self.budget_seconds <= 0:
            return float("inf")
        return max(0.0, self.budget_seconds - (self.clock() - self.started))

    def depth_exhausted(self, *, extra_used: int = 0, hop_cap: int | None = None) -> bool:
        cap = max_next_hops() if hop_cap is None else int(hop_cap)
        return int(extra_used) >= max(0, cap) and bool(self.high_priority_open())

    def should_stop(self) -> bool:
        return bool(self.stop.get("stop_reason"))

    def no_gain_confirmed(self) -> bool:
        """§14 的收敛条件是否已经满足（连续 `no_gain_threshold` 轮无增益）。

        它是**循环的刹车**：确认无增益后不再发补充跳（把预算留给别的阶段），
        也正是 NO_GAIN 这个停止原因的唯一依据。
        """
        return int(self.last_round.get("no_gain_streak") or 0) >= max(1, self.no_gain_threshold)

    def finalize(self, *, budget_exhausted: bool | None = None, depth_exhausted: bool = False,
                 actionable_hops: int = 0) -> dict:
        """算出停止原因并固化成回执（P07-06 的唯一出口）。"""
        budget = self.budget_exhausted() if budget_exhausted is None else bool(budget_exhausted)
        high_open = self.high_priority_open()
        decision = decide_stop_reason(
            no_gain_streak=int(self.last_round.get("no_gain_streak") or 0),
            no_gain_threshold=self.no_gain_threshold,
            high_priority_open=len(high_open),
            unresolved_contradictions=self.unresolved_contradictions(),
            actionable_hops=int(actionable_hops),
            depth_exhausted=bool(depth_exhausted), budget_exhausted=budget,
            evidence_insufficient=bool(high_open))
        self.stop = decision
        if self.rounds:
            self.rounds[-1]["stop_reason"] = decision["stop_reason"]
            self.rounds[-1]["detail"] = decision["detail"]
        return decision

    def receipt(self) -> dict:
        """整体回执（过 `gap_loop` 契约）。"""
        unresolved = self.unresolved_contradictions()
        return {
            "analyzer_version": GAP_ANALYZER_VERSION,
            "planner_version": NEXT_HOP_PLANNER_VERSION,
            "enabled": True,
            "stop_reason": str(self.stop.get("stop_reason") or ""),
            "stop_detail": str(self.stop.get("detail") or ""),
            "stop_factors": dict(self.stop.get("factors") or {}),
            "no_gain_rounds": int(self.no_gain_threshold),
            "rounds": [dict(item) for item in self.rounds],
            "gaps": [dict(gap) for gap in self.open_gaps()],
            "hops": [dict(hop) for hop in self.planned_hops],
            "errors": [dict(item) for item in self.errors],
            "seen_dedupe": {
                "seen_queries": len(self.seen_queries),
                "searched_baseline": len(self.searched_baseline),
                "planned_fingerprints": len([hop for hop in self.planned_hops
                                             if hop.get("query_fingerprint")]),
                "rejected_sources": len(self.seen_rejected_sources),
                "dedupe_hits": int(self.dedupe_hits),
                "dropped_by_basis": self._dropped_by_basis(),
            },
            "stats": {
                "rounds": len(self.rounds),
                "open_gaps": len(self.open_gaps()),
                "high_priority_open": len(self.high_priority_open()),
                "unresolved_contradictions": unresolved,
                "by_type": _counter(gap["missing"] for gap in self.open_gaps()),
                "by_band": _counter(gap["band"] for gap in self.open_gaps()),
                "resolved_high_priority_total": sum(
                    int(item.get("resolved_high_priority_gaps") or 0) for item in self.rounds),
                "new_verified_claims_total": sum(
                    int(item.get("new_verified_claims") or 0) for item in self.rounds),
                "planned_hops": len(self.planned_hops),
                "priority_threshold": self.threshold,
            },
        }


def _monotonic() -> float:
    import time

    return time.monotonic()


# ── 证据图阶段的缺口复核（UNRESOLVABLE_CONTRADICTION 的权威出口）────────────
def review_graph(layer: Mapping, *, graph: Mapping | None = None, plan: Mapping | None = None,
                 state: GapLoopState | None = None,
                 previous_stop_reason: str = "") -> dict:
    """在 Phase 06 证据图上做一次缺口复核（**不再发起检索**：检索阶段已经结束）。

    它回答两个问题：
      ① 证据图上还有哪些缺口（含 coverage 缺口与矛盾缺口）——走同一套 `detect_gaps`；
      ② 停止原因是什么。**只有 UNRESOLVABLE_CONTRADICTION 在这里被"权威地"给出**：
        Phase 06 的规则裁决已经比过 §15 的八项，落 `unresolved` 就说明"现有规则再也裁不动"，
        此时再检索改变不了结论（要裁就得换更强的裁决器，而本轮硬约束禁止调模型）。
        其余情形**沿用多跳循环已经给出的停止原因**（不在这里另判一次，避免"两处口径打架"）；
        复核只做一次升级：证据图显示已经没有高优缺口了 → `ANSWERABLE`。
        （离线验收工具不传 `previous_stop_reason`，此时按"复核即终局"的口径现算，见 `_gates`。）
    `state` 传入时复用它的轮次记账（预算/增益连续轮次从循环状态里取）。
    """
    layer = layer if isinstance(layer, Mapping) else {}
    claims = [row for row in (layer.get("claims") or []) if isinstance(row, Mapping)]
    edges = [edge for edge in (layer.get("edges") or []) if isinstance(edge, Mapping)]
    evidence = [item for item in ((graph or {}).get("evidence") or []) if isinstance(item, Mapping)]
    contradictions = [item for item in (layer.get("contradictions") or [])
                      if isinstance(item, Mapping)]
    analysis = detect_gaps(claims, edges=edges, evidence=evidence,
                           contradictions=contradictions, plan=plan,
                           category=category_of(plan))
    gaps = analysis["gaps"]
    unresolved = [item for item in contradictions
                  if str(item.get("resolution") or "") == "unresolved"]
    coverage = _mapping(layer.get("coverage"))
    high_open = [gap for gap in gaps if is_high_priority(gap["priority"])]
    # 证据图阶段**没有**下一跳可发（检索已结束）→ actionable_hops=0 是事实，不是借口
    decided = decide_stop_reason(
        no_gain_streak=int((state.last_round.get("no_gain_streak") if state else 0) or 0),
        no_gain_threshold=no_gain_rounds(),
        high_priority_open=len(high_open),
        unresolved_contradictions=len(unresolved),
        actionable_hops=0,
        depth_exhausted=True,
        budget_exhausted=bool(state.budget_exhausted()) if state else False)
    carried = str(previous_stop_reason or (state.stop.get("stop_reason") if state else "") or "")
    if decided["stop_reason"] == QA_STOP_UNRESOLVABLE_CONTRADICTION:
        decision = decided
        source = "evidence_graph_review（Phase 06 裁决 unresolved 是权威依据）"
    elif not high_open:
        decision = {"stop_reason": QA_STOP_ANSWERABLE,
                    "detail": "证据图上没有高优缺口：证据足以回答（不代表答案一定确定）",
                    "factors": decided["factors"]}
        source = "evidence_graph_review（无高优缺口）"
    elif carried:
        decision = {"stop_reason": carried,
                    "detail": "沿用多跳循环的停止原因：%s" % carried,
                    "factors": decided["factors"]}
        source = "carried_from_gap_loop"
    else:
        decision = decided
        source = "evidence_graph_review（离线复核，无循环状态）"
    receipt = {
        "analyzer_version": GAP_ANALYZER_VERSION,
        "stage": "evidence_graph_review",
        "stop_reason": decision["stop_reason"],
        "stop_detail": decision["detail"],
        "stop_source": source,
        "stop_factors": decision["factors"],
        "gaps": gaps,
        "unresolved_contradictions": [
            {"contradiction_id": str(item.get("contradiction_id") or ""),
             "kind": str(item.get("kind") or ""),
             "reason_code": str(item.get("reason_code") or ""),
             "claim_ids": [str(cid) for cid in (item.get("claim_ids") or [])],
             "rationale": str(item.get("rationale") or "")[:200]}
            for item in unresolved],
        "stats": {
            **analysis["stats"],
            "evidence_graph_claims": len([row for row in claims if not row.get("plan_only")]),
            "evidence_graph_edges": len(edges),
            "contradictions": len(contradictions),
            "unresolved_contradictions": len(unresolved),
            "high_priority_open": len(high_open),
            "claim_coverage": coverage.get("claim_coverage"),
            "evidence_coverage": coverage.get("evidence_coverage"),
        },
    }
    return receipt


def gap_summary(receipts: Sequence[Mapping]) -> dict:
    """把多份回执（多跳阶段 + 证据图复核）汇成一份分布报表（验收/观测用）。

    停止原因分布、Gap 类型分布、跳数与耗时口径都在这里统一，避免各处自己数。
    """
    receipts = [item for item in (receipts or []) if isinstance(item, Mapping)]
    stop_reasons = _counter(item.get("stop_reason") for item in receipts if item.get("stop_reason"))
    gap_types: dict = {}
    bands: dict = {}
    for item in receipts:
        for gap in item.get("gaps") or []:
            if not isinstance(gap, Mapping):
                continue
            key = str(gap.get("missing") or "")
            gap_types[key] = gap_types.get(key, 0) + 1
            band = str(gap.get("band") or "")
            bands[band] = bands.get(band, 0) + 1
    rounds = [rnd for item in receipts for rnd in (item.get("rounds") or [])
              if isinstance(rnd, Mapping)]
    return {
        "analyzer_version": GAP_ANALYZER_VERSION,
        "planner_version": NEXT_HOP_PLANNER_VERSION,
        "receipts": len(receipts),
        "stop_reason_distribution": stop_reasons,
        "stop_reasons_produced": sorted(stop_reasons),
        "gap_type_distribution": dict(sorted(gap_types.items(), key=lambda item: (-item[1],
                                                                                 item[0]))),
        "gap_band_distribution": dict(sorted(bands.items(), key=lambda item: (-item[1], item[0]))),
        "gaps_total": sum(gap_types.values()),
        "rounds_total": len(rounds),
        "hop_rounds": len([item for item in receipts if isinstance(item.get("hops"), list)
                           and item.get("hops")]),
        "planned_hops_total": sum(len(item.get("hops") or []) for item in receipts),
        "dedupe_hits_total": sum(int((item.get("seen_dedupe") or {}).get("dedupe_hits") or 0)
                                 for item in receipts),
        "new_verified_claims_total": sum(int(rnd.get("new_verified_claims") or 0) for rnd in rounds),
        "resolved_high_priority_total": sum(int(rnd.get("resolved_high_priority_gaps") or 0)
                                            for rnd in rounds),
        "no_gain_rounds": sum(1 for rnd in rounds if rnd.get("no_gain")),
        "unresolved_contradictions_total": sum(int(rnd.get("unresolved_contradictions") or 0)
                                               for rnd in rounds),
    }


def contract_ok(payload: Mapping, schema: str) -> tuple:
    """给调用方/测试用的一行校验（薄封装，口径统一）。"""
    return validate_contract(schema, dict(payload or {}))


__all__ = [
    "DEFAULT_NO_GAIN_ROUNDS", "DEFAULT_PRIORITY_THRESHOLD", "GAP_ROUTE_RULES", "GAP_SEVERITY",
    "GapLoopState", "band_of", "detect_gaps", "evidence_requirement_for", "gap_analyzer_enabled",
    "gap_summary", "hunters_for", "is_high_priority", "max_next_hops", "next_hop_planners",
    "no_gain_rounds", "normalize_claim", "plan_next_hops", "planner_name", "priority_threshold",
    "query_fingerprint", "register_next_hop_planner", "review_graph", "routes_for",
    "suggest_queries", "dedupe_next_hops", "decide_stop_reason",
]

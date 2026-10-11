#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 12（P12-01…P12-06）· Episodic / Failure / Strategy / Source / Skill performance
experience memory（**纯规则与统计，零模型调用、零嵌入端点**）。

通用包 01_V2_ARCHITECTURE 依据：
  · §3.2 Episodic Research Memory：问题类型 / 成功拆解 / 关键 Gap / 有效检索路径 / 停止原因
    （`EPISODE_SECTIONS` 五要素）—— 归纳器**只能总结 trace，不可制造事实**；
  · §3.3 Failure Memory：无效 query / 失败 route / false-positive source / 语义漂移扩展，
    且**必须绑定 corpus_version / retrieval_config_version / 时间**（MASTER_RULES 15）；
  · §3.4 Strategy Memory：某类问题哪些检索组合更有效（策略经验，不是事实）；
  · §3.5 Source Memory：authority / domain / update_frequency / reliability / known limitations；
  · §3.6 Skill Performance Memory：skill_id / task_type / success_rate / latency / cost /
    evidence_yield，"供 Skill Router 学习"；
  · §10 Planning Recall：在 Research Planner 之前优先召回 Strategy/Failure/QueryPattern；
  · §11 两个公式、§12 库表（`skill_performance_memory` / `source_reliability_memory` 是
    Phase 09 刻意留给本阶段的两张表）、§14 作用域与"某轮搜索失败为 SESSION 或带
    corpus/version 的 EPISODIC"。

硬边界（诚实声明，宁写 PARTIAL 不谎报）
------------------------------------------------
  1. **零模型/零嵌入端点**（GPU 机已停用）：归纳只有规则 + 计数 + 时间衰减。
     §3.2 的"成功拆解"= 真实计划里的子问题/跳，不是模型写的摘要；规格书若提到
     LLM 归纳，那只是一个**可插拔注入点**（`register_episode_summarizer`），默认不调用。
  2. **写门是唯一入口**：本模块**不自己写库**，一律走 `qa_memory.apply_write_gate`
     （类型归属/效用/去重/TTL/隐私全部由那一个决策点管，见 D-044）。
  3. **经验永远不是证据**（MASTER_RULES 11/12）：本模块产出的记忆召回后仍是
     MEMORY_HINT；指导（`build_guidance`）恒 `requires_revalidation=True`、
     `verified_evidence=False`，永不进 citation_map。
  4. **失败绑定不许编**：语料版本取 `run.corpus_version`，为空则退到该轮检索回执里
     **真实观测到的** `cache.kb_version`；两者都没有就是 `VERSION_UNBOUND`
     —— 没有版本绑定**一律允许重试**，绝不凭空压制（MASTER_RULES 15）。
  5. **只吃冻结的七个通道值**：证据里的 `hybrid` / `tavily` 这类本部署自有取值
     **不做硬映射**（与 Phase 07 D-028、Phase 11 P11-06 的处置一致，如实记进
     `unmapped_routes` 而不是硬塞给某个通道）。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping, Sequence

from qa_evidence import evidence_object, now_utc
from qa_graph_contracts import (
    EPISODE_MEMORY_VERSION,
    EPISODE_SECTIONS,
    EXPERIENCE_GUIDANCE_VERSION,
    EXPERIENCE_MEMORY_VERSION,
    EXPERIENCE_RECALL_TYPES,
    EXPERIENCE_TRACE_SECTIONS,
    FAILURE_MEMORY_VERSION,
    QA_RETRIEVAL_ROUTES,
    QA_STOP_REASONS,
    RETRY_POLICY_VERSION,
    SKILL_IDS,
    SOURCE_MEMORY_VERSION,
    SOURCE_RELIABILITY_FACTORS,
    STRATEGY_MEMORY_VERSION,
    SKILL_PERFORMANCE_MEMORY_VERSION,
    validate as validate_contract,
)
from qa_memory import (
    MEMORY_SCOPES,
    canonicalize,
    evidence_link_row,
)
from qa_verifier import term_set, verification_of

# ── 版本与口径常量 ──────────────────────────────────────────────────────────

EXPERIENCE_TRACE_VERSION = "qa-experience-trace-v1"
"""trace 形状版本（八个来源段的口径；段缺席要如实记 `missing_sections`）。"""

EXPERIENCE_WRITE_VERSION = "qa-experience-write-v1"
"""经验归纳 → 写门 → 投影的落库口径版本。"""

DEFAULT_FAILURE_MAX_AGE_DAYS = 30.0
"""失败记忆的**压制时效**（天）：超过它就 `FAILURE_AGED_OUT` → 允许重试。

标定口径：失败记忆的价值是"别在同一个环境里重复踩坑"，不是"永久拉黑一条通道"。
30 天 ≈ 一次语料刷新周期；`QA_EXPERIENCE_FAILURE_MAX_AGE_DAYS` 可调（1..3650）。"""

DEFAULT_EXPERIENCE_MIN_SAMPLES = 2
"""经验成记忆的最小样本数（策略/来源/查询形态/技能表现都吃它）。

与 Phase 11 的 `skill_min_samples` **同一口径**（不许拿一次观测当规律）；
技能表现类直接复用 Phase 11 的门槛值，不另立一个。"""

DEFAULT_ROUTE_SUCCESS_FLOOR = 0.60
"""通道"在这个任务类型上算有效"的成功率下限（`QA_EXPERIENCE_ROUTE_SUCCESS_FLOOR`）。

标定口径：**不要求 100%**——真实语料上同一条通道在不同轮次的表现本来就有起伏，
要求"次次成功"会让策略记忆在真机上一条都产不出来（等于死数据）。
0.60 = "多数情况下取到了已核验证据"；观测少于门槛的通道直接不参与。"""

DEFAULT_RECALL_MIN_SCORE = 0.02
"""规划期召回的分数下限（`QA_EXPERIENCE_RECALL_MIN_SCORE`，默认 0.02）。

标定口径（**实测**，不是拍的）：Phase 09 对事实类记忆的默认门槛是 0.05，而经验类记忆的
正文以结构性词为主（通道名/任务类型/统计数字），词面覆盖天然比"事实正文"低 ——
在管线里实测一条**同一环境、同一任务类型、直指 keyword 通道**的失败记忆召回分只有
0.037（semantic_relevance 0.222 × applicability 1.0 × confidence 0.51 × freshness 0.408
× historical 0.8），0.05 门槛会把它挡在门外 → 经验就真的成了死数据。
因此规划期单独用一个更低的门槛；风险可控：命中恒为 MEMORY_HINT，
且**只有环境绑定一致**的失败才会真的改变通道选择（见 `retry_decision`）。"""

EXPERIENCE_CLAIM_TYPES = {
    "EPISODIC_RESEARCH": "episode", "FAILURE": "failure", "STRATEGY": "strategy",
    "QUERY_PATTERN": "query_pattern", "SOURCE": "source", "SKILL_PERFORMANCE": "skill_performance",
}
"""经验类记忆的 `claim_type` 名（写门用它算 ReuseProbability，与 P09 的
`_REUSABLE_CLAIM_TYPES` 走**同一处公式**：经验本身就是"可复用的经验"，+0.05 加成）。"""

FRESHNESS_BY_TYPE = {
    "EPISODIC_RESEARCH": "MEDIUM", "FAILURE": "MEDIUM", "STRATEGY": "MEDIUM",
    "QUERY_PATTERN": "MEDIUM", "SOURCE": "MEDIUM", "SKILL_PERFORMANCE": "MEDIUM",
}
"""六类经验记忆的时效档（**标定后统一 MEDIUM**，逐项理由写在验收报告里）：

  · 经验类记忆不是"某条新闻"，它的价值随语料/配置变化而衰减，而**版本绑定 +
    重试策略**才是防陈旧的主手段（失败记忆 30 天压制窗口）；MEDIUM 的半衰期 365 天
    与"一年内还可参考"的直觉一致；
  · 不给 `SHORT`：`STALENESS_BASE['SHORT']=0.08` 会把刚学到的经验在写门就压到效用线下
    （实测 0.03 < 0.05 门槛），等于"学到的经验永远存不下来"；
  · 不给 `LONG`：经验会随环境漂移，LONG 的过期风险基线是 0.0，会高估它的长期有效性。"""

# 通道归一与"某轮到底试过哪些通道"的**真实信号表**（全部来自真实检索回执）
_ROUTE_ALIASES = {
    "keyword": "keyword", "bm25": "keyword",
    "semantic": "semantic", "vector": "semantic",
    "page_context": "page_context", "policy_exact": "policy_exact",
    "graph": "graph", "graph_attribute": "graph_attribute",
    "web": "web",
}
"""`retrieval_method` / 回执里的通道名 → **Phase 01 冻结的七个通道值**。

只认能一一对应的那些；`hybrid`（混合）与 `tavily`（本部署自有的外部检索实现）
**故意不映射**（D-028/P11-06 同一口径：不硬塞），它们进 `unmapped_routes` 计数。"""

_ATTEMPT_SIGNALS = (
    # (通道, "这一轮真的试过"的 stats 键, "采纳数"来自哪个键（空 = 只从证据行数）, 附加条件)
    ("keyword", ("keyword_candidates",), "", ""),
    ("web", ("web_adopted",), "web_adopted", "search_status"),
    ("graph", ("graph_adopted",), "graph_adopted", ""),
    ("semantic", ("vector_variants",), "", "vector_enabled"),
)
"""通道"这一轮真的试过没有"的判据表（**只读真实回执**，不猜）：

  · `keyword`：有 `keyword_candidates` 就说明关键词通道跑过；它的**采纳数只从证据行数**
    （候选数 ≠ 采纳数，把候选当采纳是第一阶段就踩过的坑）；
  · `web` / `graph`：`web_adopted` / `graph_adopted` 本身就是**采纳数**（真机回执自带）；
    web 还要 `search_status != 'disabled'`（通道没开不算失败）；
  · `semantic`：`vector_prefilter.enabled is True`（**enabled=false 就是没跑**，
    不能把"预筛被关掉"记成"语义通道零产出"，那是两件事）。"""

_POLICY_EXACT_SIGNAL = ("policy_exact", "policy_exact.adopted", "policy_exact_gate")
"""结构化/政策精确通道的判据（`policy_exact_gate == 'not_applicable'` = 没试过）。"""

_MATCH_FAMILIES = ("标题短语命中", "标题命中", "分类词命中", "正文短语命中", "核心词命中")
"""`match_reason` 里的命中族（真实检索回执自带）；"语义漂移"只认**没有标题族命中**的证据。"""

_SUMMARIZERS: dict = {"rule:trace-v1": None}
"""剧集归纳后端注册表（`None` = 内置规则实现）。**默认只有规则实现**，
LLM 归纳只能通过 `register_episode_summarizer` 注入，且本阶段一次都不调用。"""


# ── 可配置旋钮 ──────────────────────────────────────────────────────────────

def _env_flag(name: str, default: bool) -> bool:
    raw = str(os.environ.get(name, "") or "").strip().casefold()
    if not raw:
        return default
    return raw not in ("0", "false", "off", "no", "disabled", "")


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(str(os.environ.get(name, "") or "").strip())
    except (TypeError, ValueError):
        return default
    if math.isnan(value):
        return default
    return max(low, min(high, value))


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(float(str(os.environ.get(name, "") or "").strip()))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def experience_memory_enabled() -> bool:
    """管线开关（`QA_EXPERIENCE_MEMORY`，**默认关**）：关掉时行为逐字回到 Phase 11。"""
    return _env_flag("QA_EXPERIENCE_MEMORY", False)


def failure_max_age_days() -> float:
    return _env_float("QA_EXPERIENCE_FAILURE_MAX_AGE_DAYS", DEFAULT_FAILURE_MAX_AGE_DAYS, 1.0, 3650.0)


def experience_min_samples() -> int:
    return _env_int("QA_EXPERIENCE_MIN_SAMPLES", DEFAULT_EXPERIENCE_MIN_SAMPLES, 1, 100)


def route_success_floor() -> float:
    return _env_float("QA_EXPERIENCE_ROUTE_SUCCESS_FLOOR", DEFAULT_ROUTE_SUCCESS_FLOOR, 0.05, 1.0)


def experience_scope() -> str:
    """经验记忆的作用域（默认 `ORGANIZATION`）。

    §14 说"某轮搜索失败为 SESSION **或**带 corpus/version 的 EPISODIC"——本模块取后者：
    经验是**本部署的检索环境**知识（语料 + 检索配置），跨会话复用才有价值；
    作用域键 = `org:<owner>`（同部署同用户可见），而"什么时候不许再用"由
    **版本绑定 + 重试策略**管，不由会话边界管（会话边界会把经验变成死数据）。
    """
    value = str(os.environ.get("QA_EXPERIENCE_SCOPE", "") or "").strip().upper()
    return value if value in MEMORY_SCOPES else "ORGANIZATION"


def summarizer_name() -> str:
    return str(os.environ.get("QA_EXPERIENCE_SUMMARIZER", "") or "").strip() or "rule:trace-v1"


def experience_recall_min_score() -> float:
    return _env_float("QA_EXPERIENCE_RECALL_MIN_SCORE", DEFAULT_RECALL_MIN_SCORE, 0.0, 1.0)


def register_episode_summarizer(name: str, backend: Callable | None) -> None:
    """注册剧集归纳后端（**注入点**）：`name` 空/重复覆盖要显式。

    默认实现是 `rule:trace-v1`（纯规则）。LLM 归纳只能从此处注入，
    本阶段（硬约束：不准调模型）**不注册、不调用**任何模型后端。
    """
    clean = str(name or "").strip()
    if not clean:
        raise ValueError("归纳后端名不能为空")
    _SUMMARIZERS[clean] = backend if callable(backend) else backend


def registered_summarizers() -> tuple:
    return tuple(sorted(_SUMMARIZERS))


# ── 通用小工具 ──────────────────────────────────────────────────────────────

def _digest(value, length: int = 20) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:length]


def _clamp(value, low: float = 0.0, high: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low
    if math.isnan(number) or math.isinf(number):
        return low
    return max(low, min(high, number))


def _mapping(value) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _items(value) -> list:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, (list, tuple)) else []


def _round(value, digits: int = 6) -> float:
    return round(float(value or 0.0), digits)


def _as_float(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(number) or math.isinf(number) else number


def _parse_time(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                moment = datetime.strptime(text[:len(fmt) + 4].strip(), fmt)
                break
            except ValueError:
                moment = None
        if moment is None:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _now(value=None) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = _parse_time(value)
    if parsed is not None:
        return parsed
    return datetime.fromisoformat(now_utc().replace("Z", "+00:00"))


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _counter(values) -> dict:
    out: dict = {}
    for value in values or []:
        key = str(value or "")
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda item: (-item[1], item[0])))


def normalize_route(value) -> str:
    """通道名 → 冻结七值之一（认不出返回空串，**不硬映射**）。"""
    return str(_ROUTE_ALIASES.get(str(value or "").strip().casefold(), "") or "")


def route_of_evidence(item: Mapping) -> str:
    """一条证据走的通道：`metadata.evidence_layer.route` 优先，退回 `retrieval_method`。"""
    layer = evidence_object(item if isinstance(item, Mapping) else {})
    provenance = layer.get("provenance") if isinstance(layer.get("provenance"), Mapping) else {}
    raw = str(provenance.get("route") or (item.get("retrieval_method")
                                          if isinstance(item, Mapping) else "") or "")
    return normalize_route(raw), raw


def domain_of(url) -> str:
    """来源域名（纯字符串处理，剥离 www. 前缀与端口；认不出返回空串）。"""
    text = str(url or "").strip()
    if not text:
        return ""
    match = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/?#]+)", text)
    host = match.group(1) if match else text.split("/")[0]
    host = host.split("@")[-1].split(":")[0].strip().casefold()
    if host.startswith("www."):
        host = host[4:]
    return host


# ── P12-01：trace 装配（八个来源段，缺哪段记哪段）──────────────────────────

def task_type_of(*, trace_task_type: str = "", plan: Mapping | None = None,
                 question: str = "", mode: str = "") -> str:
    """问题/任务类型：**复用 Phase 05 的规则解释器**，不另造分类。

    优先级：trace 显式给的 → 计划的 `category`（`qa_gap_analyzer.category_of`）→
    `qa_query_interpreter` 的 `intent`（纯规则）→ `mode` → 空串。
    """
    clean = str(trace_task_type or "").strip()
    if clean:
        return clean
    try:
        from qa_gap_analyzer import category_of
    except Exception:            # noqa: BLE001
        category_of = None
    if callable(category_of) and isinstance(plan, Mapping):
        try:
            value = str(category_of(plan) or "").strip()
        except Exception:        # noqa: BLE001
            value = ""
        if value:
            return value
    text = str(question or "").strip()
    if text:
        try:
            from qa_query_interpreter import interpret_query
        except Exception:        # noqa: BLE001
            interpret_query = None
        if callable(interpret_query):
            try:
                payload = interpret_query(text, plan=plan if isinstance(plan, Mapping) else None)
                value = str((payload or {}).get("intent") or "").strip()
            except Exception:    # noqa: BLE001
                value = ""
            if value:
                return value
    return str(mode or "").strip()


def _retrieval_section(*, graph: Mapping, level1: Mapping, stage_rows: Sequence = ()) -> dict:
    """装配 trace 的 `retrieval` 段：逐通道采纳/已核验/证据引用 + 回执统计。"""
    stats: dict = {}
    queries: list = []
    excluded: dict = {}
    cache: dict = {}
    status = ""
    errors: list = []
    for row in stage_rows or ():
        if not isinstance(row, Mapping):
            continue
        if str(row.get("stage") or "") not in ("", "level1_retrieval"):
            continue
        details = row.get("details") if isinstance(row.get("details"), Mapping) else {}
        result = details.get("result") if isinstance(details.get("result"), Mapping) else {}
        stats = {**stats, **_mapping(result.get("stats"))}
        excluded = {**excluded, **_mapping(result.get("excluded"))}
        cache = {**cache, **_mapping(result.get("cache"))}
        status = str(result.get("search_status") or status or "")
        queries = [str(item) for item in (result.get("queries") or []) if str(item).strip()] or queries
        errors = [str(item) for item in (result.get("search_errors") or []) if str(item)] or errors
    for key in ("stats", "queries", "excluded", "search_status", "cache", "search_errors"):
        value = level1.get(key) if isinstance(level1, Mapping) else None
        if value in (None, "", [], {}):
            continue
        if key == "stats":
            stats = {**stats, **_mapping(value)}
        elif key == "queries":
            queries = [str(item) for item in value if str(item).strip()] or queries
        elif key == "excluded":
            excluded = {**excluded, **_mapping(value)}
        elif key == "cache":
            cache = {**cache, **_mapping(value)}
        elif key == "search_errors":
            errors = [str(item) for item in value if str(item)] or errors
        else:
            status = str(value or status)

    evidence = [item for item in (graph.get("evidence") or []) if isinstance(item, Mapping)]
    yields: dict = {}
    unmapped: dict = {}
    for item in evidence:
        route, raw = route_of_evidence(item)
        if not route:
            unmapped[raw or "<empty>"] = unmapped.get(raw or "<empty>", 0) + 1
            continue
        bucket = yields.setdefault(route, {"adopted": 0, "verified": 0, "refuted": 0,
                                           "evidence_refs": [], "scores": []})
        bucket["adopted"] += 1
        bucket["evidence_refs"].append(str(item.get("evidence_ref") or ""))
        verification = verification_of(item)
        verdict = str(verification.get("verdict") or "").upper()
        if verdict == "SUPPORTED":
            bucket["verified"] += 1
        elif verdict == "REFUTED":
            bucket["refuted"] += 1
        bucket["scores"].append(_as_float(verification.get("score")))
    attempts = _attempted_routes(stats=stats, status=status, excluded=excluded)
    for route, row in attempts.items():
        bucket = yields.setdefault(route, {"adopted": 0, "verified": 0, "refuted": 0,
                                           "evidence_refs": [], "scores": []})
        bucket["attempted"] = True
        bucket["adopted_signal"] = int(row.get("adopted") or 0)
        bucket["attempt_source"] = str(row.get("source") or "")
        # 回执自带采纳数的通道（web / graph）：采纳数就取它（真机回执本来就按通道分账）；
        # 关键词通道的 `keyword_candidates` 是**候选**不是采纳，绝不能当采纳用。
        if row.get("adopted_key") and not bucket.get("evidence_refs"):
            bucket["adopted"] = max(int(bucket.get("adopted") or 0), int(row.get("adopted") or 0))
    for route, bucket in yields.items():
        bucket.setdefault("attempted", False)
        bucket["scores"] = [_round(value) for value in bucket.get("scores") or []]
        bucket["evidence_refs"] = [ref for ref in bucket["evidence_refs"] if ref][:20]
    return {
        "routes": dict(sorted(yields.items())),
        "unmapped_routes": dict(sorted(unmapped.items())),
        "queries": queries[:8],
        "stats": stats,
        "excluded": excluded,
        "cache": cache,
        "search_status": status,
        "search_errors": errors[:5],
        "evidence_count": len(evidence),
    }


def _attempted_routes(*, stats: Mapping, status: str, excluded: Mapping) -> dict:
    """按 `_ATTEMPT_SIGNALS` + 政策精确通道判据，算出"这一轮真的试过哪些通道"。"""
    stats = stats if isinstance(stats, Mapping) else {}
    out: dict = {}
    for route, keys, adopted_key, condition in _ATTEMPT_SIGNALS:
        if not any(key in stats for key in keys):
            continue
        if condition == "search_status" and str(status or "").casefold() in ("disabled", "off"):
            continue
        if condition == "vector_enabled":
            prefilter = stats.get("vector_prefilter")
            if not (isinstance(prefilter, Mapping) and bool(prefilter.get("enabled"))):
                continue
        value = stats.get(adopted_key) if adopted_key else 0
        out[route] = {"adopted": value if isinstance(value, (int, float)) else 0,
                      "adopted_key": adopted_key,
                      "source": "stats.%s" % (adopted_key or keys[0])}
    exact = stats.get(_POLICY_EXACT_SIGNAL[0])
    if isinstance(exact, Mapping) and str(exact.get("policy_exact_gate") or "") not in ("", "not_applicable"):
        out["policy_exact"] = {"adopted": int(exact.get("adopted") or 0),
                               "adopted_key": "%s.adopted" % _POLICY_EXACT_SIGNAL[0],
                               "source": "stats.%s.adopted" % _POLICY_EXACT_SIGNAL[0]}
    return out


def build_trace(*, run_meta: Mapping, plan: Mapping | None = None, graph: Mapping | None = None,
                level1: Mapping | None = None, gap_receipt: Mapping | None = None,
                stage_rows: Sequence = (), reasoning_traces: Sequence = (),
                skill_records: Sequence = (), stop_reason: str = "",
                task_type: str = "", now=None, captured_at: str = "",
                retrieval_config_version: str = "") -> dict:
    """装配一个 run 的 trace（**只读既有回执**，不新增事实）。

    八个来源段（`EXPERIENCE_TRACE_SECTIONS`）里缺哪段就写进 `missing_sections` ——
    "没有这一段"与"这一段是空的"是两件事，归纳时必须分得清。

    `retrieval_config_version` 是**运行期真实的检索配置版本**（管线手里就有，
    例如 `qa-retrieval-v3-policy-exact`）；给了就用它，没给才退到规则派生的令牌
    （`retrieval_config_token`）—— 版本标识的口径**优先用真的，不用算的**。
    """
    run_meta = run_meta if isinstance(run_meta, Mapping) else {}
    graph = graph if isinstance(graph, Mapping) else {}
    level1 = level1 if isinstance(level1, Mapping) else {}
    gap_receipt = gap_receipt if isinstance(gap_receipt, Mapping) else {}
    question = str(run_meta.get("question_text") or run_meta.get("question") or "")
    retrieval = _retrieval_section(graph=graph, level1=level1, stage_rows=stage_rows)

    verification: dict = {"verdicts": {}, "evidence": 0, "supported": 0, "refuted": 0}
    for item in (graph.get("evidence") or []):
        if not isinstance(item, Mapping):
            continue
        verdict = str(verification_of(item).get("verdict") or "UNVERIFIED").upper()
        verification["verdicts"][verdict] = verification["verdicts"].get(verdict, 0) + 1
        verification["evidence"] += 1
        if verdict == "SUPPORTED":
            verification["supported"] += 1
        elif verdict == "REFUTED":
            verification["refuted"] += 1

    gaps = [item for item in (gap_receipt.get("gaps") or graph.get("gaps") or [])
            if isinstance(item, Mapping)]
    open_gaps = [gap for gap in gaps if str(gap.get("status") or "open") == "open"]
    resolved = [gap for gap in gaps if str(gap.get("status") or "") in ("resolved", "closed")]
    hops = [hop for hop in (gap_receipt.get("planned_hops") or gap_receipt.get("hops") or [])
            if isinstance(hop, Mapping)]
    gap_section = {
        "gaps": len(gaps), "open": len(open_gaps), "resolved": len(resolved),
        "types": _counter(gap.get("missing") for gap in gaps),
        "open_detail": [{"gap_id": str(gap.get("gap_id") or ""),
                         "missing": str(gap.get("missing") or ""),
                         "priority": _round(gap.get("priority")),
                         "suggested_routes": [str(item) for item in (gap.get("suggested_routes") or [])],
                         "claim_id": str(gap.get("claim_id") or "")} for gap in gaps][:20],
        "next_hops": [{"hop_id": str(hop.get("hop_id") or ""), "route": str(hop.get("route") or ""),
                       "gap_id": str(hop.get("gap_id") or ""),
                       "routes": [str(item) for item in (hop.get("routes") or [])]} for hop in hops][:10],
    }
    stop = {
        "stop_reason": str(stop_reason or gap_receipt.get("stop_reason") or ""),
        "rounds": len(_items(gap_receipt.get("rounds"))),
        "no_gain_streak": int((_mapping(gap_receipt.get("last_round")) or {}).get("no_gain_streak") or 0),
    }
    stop["stop_reason"] = stop["stop_reason"] if stop["stop_reason"] in QA_STOP_REASONS else ""

    sources: list = []
    claim_ids_by_ref: dict = {}
    for edge in (graph.get("edges") or []):
        if not isinstance(edge, Mapping):
            continue
        ref = str(edge.get("evidence_ref") or "")
        claim_id = str(edge.get("claim_id") or edge.get("claim_key") or "")
        if ref and claim_id:
            claim_ids_by_ref.setdefault(ref, []).append(claim_id)
    evidence_index: dict = {}
    for item in (graph.get("evidence") or []):
        if not isinstance(item, Mapping):
            continue
        ref = str(item.get("evidence_ref") or "")
        if ref:
            evidence_index[ref] = dict(item)
        layer = evidence_object(item)
        source = layer.get("source") if isinstance(layer.get("source"), Mapping) else {}
        url = str(item.get("source_url") or source.get("source_url") or "")
        verification_row = verification_of(item)
        route, _raw = route_of_evidence(item)
        sources.append({
            "domain": domain_of(url),
            "url": url,
            "evidence_ref": ref,
            "authority_level": item.get("authority_level"),
            "published_at": str(item.get("published_at") or source.get("published_at") or ""),
            "verdict": str(verification_row.get("verdict") or "UNVERIFIED").upper(),
            "route": route,
            "claim_ids": sorted(set(claim_ids_by_ref.get(ref) or [])),
            "match_families": _match_families(item),
        })

    planning = {
        "mode": str(run_meta.get("mode") or ""),
        "sub_questions": [str(item.get("question") or item.get("sub_question") or "")
                          for item in _items((plan or {}).get("sub_questions") or (plan or {}).get("hops"))][:10],
        "entities": [str(item) for item in ((plan or {}).get("entities") or [])][:12],
        "topics": [str(item) for item in ((plan or {}).get("topics") or [])][:12],
        "decomposition_source": str((plan or {}).get("decomposition_source")
                                    or (plan or {}).get("source") or ""),
    }
    trace = {
        "trace_version": EXPERIENCE_TRACE_VERSION,
        "captured_at": str(captured_at or _stamp(_now(now))),
        "run": {
            "id": str(run_meta.get("id") or run_meta.get("run_id") or ""),
            "question": question,
            "mode": str(run_meta.get("mode") or ""),
            "owner_user_id": str(run_meta.get("owner_user_id") or ""),
            "session_id": str(run_meta.get("session_id") or ""),
            "industry_pack_id": str(run_meta.get("industry_pack_id") or ""),
            "corpus_version": str(run_meta.get("corpus_version") or ""),
            "config_hash": str(run_meta.get("config_hash") or ""),
            "retrieval_config_version": str(retrieval_config_version or ""),
            "status": str(run_meta.get("status") or ""),
        },
        "task_type": task_type_of(trace_task_type=task_type, plan=plan, question=question,
                                  mode=str(run_meta.get("mode") or "")),
        "planning": planning,
        "retrieval": retrieval,
        "verification": verification,
        "gaps": gap_section,
        "stop": stop,
        "sources": sources,
        "skills": [dict(item) for item in (skill_records or []) if isinstance(item, Mapping)],
        "reasoning_traces": [dict(item) for item in (reasoning_traces or [])
                             if isinstance(item, Mapping)][:20],
    }
    missing = []
    for name in EXPERIENCE_TRACE_SECTIONS:
        if name == "run":
            value = trace["run"].get("id")
        elif name == "retrieval":
            value = retrieval.get("evidence_count") or retrieval.get("queries")
        elif name == "verification":
            value = verification.get("evidence")
        elif name == "gaps":
            value = gap_section.get("gaps")
        elif name == "stop":
            value = stop.get("stop_reason")
        elif name == "sources":
            value = len(sources)
        elif name == "skills":
            value = len(trace["skills"])
        else:
            value = planning.get("sub_questions") or planning.get("entities")
        if not value:
            missing.append(name)
    trace["missing_sections"] = missing
    # 私有索引：给 `memory_evidence_link` 提供**证据对象本体**（不参与 trace 指纹，
    # 也不进任何回执 —— 记忆的 provenance 必须落在真实证据上，不能只留一个 ref 字符串）。
    trace["evidence_index"] = evidence_index
    trace["trace_fingerprint"] = trace_fingerprint(trace)
    return trace


def _match_families(item: Mapping) -> list:
    """证据的 `match_reason` 里命中了哪些族（真实检索回执自带；没有就空列表）。"""
    reason = str(item.get("match_reason") or "")
    if not reason:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        reason = str(metadata.get("match_reason") or "")
    return [family for family in _MATCH_FAMILIES if family in reason]


def trace_fingerprint(trace: Mapping) -> str:
    """trace 的内容指纹：**只吃事实段**（不含指纹自身与抓取时刻），同输入同输出。"""
    trace = trace if isinstance(trace, Mapping) else {}
    payload = {key: trace.get(key) for key in
               ("run", "task_type", "planning", "retrieval", "verification", "gaps", "stop",
                "sources", "skills")}
    return "XTF" + _digest(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                      default=str))


def retrieval_config_token(trace: Mapping) -> dict:
    """检索配置令牌（**规则派生**，不编版本号）。

    口径：`run.config_hash` 非空 → 直接用它（平台的真实配置指纹，`source` 如实标注）；
    否则用本轮**真实观测到的检索开关**做确定性指纹（web/vector/graph 是否开、
    试过哪些通道、发出几个查询）；一个信号都没有 → 空串 = `VERSION_UNBOUND`。
    `source` 字段永远写明这个令牌是怎么来的（可审计）。
    """
    trace = trace if isinstance(trace, Mapping) else {}
    run = _mapping(trace.get("run"))
    explicit = str(run.get("config_hash") or "").strip()
    if explicit:
        return {"token": explicit, "source": "qa_runs.config_hash"}
    runtime = str(run.get("retrieval_config_version") or "").strip()
    if runtime:
        # 运行期手里的**真实**检索配置版本优先（管线传进来的那个字符串）
        return {"token": runtime, "source": "qa_pipeline.retrieval_config_version"}
    retrieval = _mapping(trace.get("retrieval"))
    stats = _mapping(retrieval.get("stats"))
    routes = sorted(route for route, row in _mapping(retrieval.get("routes")).items()
                    if isinstance(row, Mapping) and row.get("attempted"))
    signals = {
        "routes_attempted": routes,
        "queries": len([item for item in (retrieval.get("queries") or []) if str(item or "")]),
        "search_status": str(retrieval.get("search_status") or ""),
        "web": "web_adopted" in stats,
        "vector": bool(_mapping(stats.get("vector_prefilter")).get("enabled")),
        "graph": "graph_adopted" in stats,
        "keyword": "keyword_candidates" in stats,
    }
    if not any((signals["web"], signals["vector"], signals["graph"], signals["keyword"],
                signals["routes_attempted"])):
        return {"token": "", "source": ""}
    return {"token": "rcfg:" + _digest(json.dumps(signals, sort_keys=True))[:16],
            "source": "rule:retrieval-signals-v1"}


def corpus_binding(trace: Mapping) -> dict:
    """语料版本绑定：`run.corpus_version` 优先，空则退到真实观测到的 `cache.kb_version`。"""
    trace = trace if isinstance(trace, Mapping) else {}
    run = _mapping(trace.get("run"))
    explicit = str(run.get("corpus_version") or "").strip()
    if explicit:
        return {"token": explicit, "source": "qa_runs.corpus_version"}
    cached = str(_mapping(_mapping(trace.get("retrieval")).get("cache")).get("kb_version") or "").strip()
    if cached:
        return {"token": cached, "source": "qa_runs/level1_retrieval.cache.kb_version"}
    return {"token": "", "source": ""}


# ── P12-01：剧集归纳（只能总结 trace）──────────────────────────────────────

def _effective_routes(trace: Mapping) -> list:
    """有效检索路径 = **真的取到已核验证据**的通道（按 (verified desc, route) 排序）。"""
    routes = _mapping(_mapping(trace.get("retrieval")).get("routes"))
    rows = [(route, int(_mapping(row).get("verified") or 0)) for route, row in routes.items()]
    return [route for route, verified in sorted(rows, key=lambda item: (-item[1], item[0]))
            if verified > 0]


def _key_gaps(trace: Mapping) -> list:
    """关键 Gap = 高优先级（≥0.7）或未解决的缺口，按 (priority desc, gap_id) 排序取前 5。"""
    gaps = _mapping(trace.get("gaps"))
    detail = [gap for gap in (gaps.get("open_detail") or []) if isinstance(gap, Mapping)]
    ranked = sorted(detail, key=lambda gap: (-_as_float(gap.get("priority")), str(gap.get("gap_id") or "")))
    return [{"gap_id": str(gap.get("gap_id") or ""), "missing": str(gap.get("missing") or ""),
             "priority": _round(gap.get("priority"))} for gap in ranked[:5]]


def episode_summary_rule(trace: Mapping) -> dict:
    """**规则版**剧集归纳（P12-01 的默认后端）：只把 trace 里的事实拼成五要素。

    五要素与它们的唯一来源（**没有一个字是模型写的**）：
      · `task_type`：Phase 05 的 intent / 计划的 category；
      · `decomposition`：真实计划的子问题/跳数（没有计划就如实空）；
      · `key_gaps`：Phase 07 的缺口（高优先级优先）；
      · `effective_paths`：**真的取到已核验证据**的通道；
      · `stop_reason`：Phase 07 的五值停止原因（没有就空）。
    """
    trace = trace if isinstance(trace, Mapping) else {}
    retrieval = _mapping(trace.get("retrieval"))
    verification = _mapping(trace.get("verification"))
    planning = _mapping(trace.get("planning"))
    gaps = _mapping(trace.get("gaps"))
    stop = _mapping(trace.get("stop"))
    routes = _mapping(retrieval.get("routes"))
    decomposition = [str(item) for item in (planning.get("sub_questions") or []) if str(item).strip()]
    effective = _effective_routes(trace)
    # 关注点：计划里的实体/主题（**trace 里的真值**）。它同时让这条经验在规划期
    # 能被同一话题的问题召回 —— 一条召不回来的记忆等于没记（§10 的 planning recall）。
    focus = [str(item) for item in (planning.get("entities") or []) if str(item).strip()][:3]
    sections = {
        "task_type": str(trace.get("task_type") or ""),
        "decomposition": {"sub_questions": decomposition[:5], "count": len(decomposition),
                          "source": str(planning.get("decomposition_source") or "plan")},
        "key_gaps": _key_gaps(trace),
        "effective_paths": effective,
        "stop_reason": str(stop.get("stop_reason") or ""),
    }
    summary = ("经验·剧集：问题类型 %s；关注点 %s；证据 %d 条（已核验支持 %d、反驳 %d）；"
               "关键缺口 %d 个（未解决 %d）；有效通道 %s；停止原因 %s。"
               % (sections["task_type"] or "未分类",
                  "、".join(focus) or "（计划里没有实体）",
                  int(retrieval.get("evidence_count") or 0),
                  int(verification.get("supported") or 0),
                  int(verification.get("refuted") or 0),
                  int(gaps.get("gaps") or 0), int(gaps.get("open") or 0),
                  "、".join(effective) or "无（本轮没有任何通道取到已核验证据）",
                  sections["stop_reason"] or "未记录"))
    return {"sections": sections, "summary": summary,
            "effective_routes": effective, "key_gaps": sections["key_gaps"],
            "task_type": sections["task_type"], "stop_reason": sections["stop_reason"],
            "route_yields": {route: {"adopted": int(_mapping(row).get("adopted") or 0),
                                     "verified": int(_mapping(row).get("verified") or 0)}
                             for route, row in routes.items()},
            "counts": {"evidence": int(retrieval.get("evidence_count") or 0),
                       "verified": int(verification.get("supported") or 0),
                       "gaps": int(gaps.get("gaps") or 0),
                       "open_gaps": int(gaps.get("open") or 0)}}


def episode_from_trace(trace: Mapping, *, summarizer: str = "", now=None) -> dict:
    """P12-01：从 trace 归纳一集（返回 `EPISODE_MEMORY_SCHEMA` 形态，过契约校验）。

    归纳后端可插拔：入参 > `QA_EXPERIENCE_SUMMARIZER` > 内置 `rule:trace-v1`。
    未注册名 / 后端抛错 / 返回非法载荷 **一律保守回落规则实现**并记账
    （与 Phase 05 的解释器、Phase 07 的规划器同一手法）—— 归纳失败绝不能挡住记忆写入。
    """
    trace = trace if isinstance(trace, Mapping) else {}
    name = str(summarizer or summarizer_name() or "rule:trace-v1")
    backend_label = name
    fallback = {"used": False, "reason": "", "backend": name}
    payload: dict = {}
    backend = _SUMMARIZERS.get(name)
    if name != "rule:trace-v1" and not callable(backend):
        fallback = {"used": True, "backend": name,
                    "reason": "未注册的剧集归纳后端：%s（已回落规则实现）" % name}
        backend_label = "rule:trace-v1"
    elif callable(backend):
        try:
            candidate = backend(dict(trace))
        except Exception as exc:      # noqa: BLE001 —— 后端坏掉不许拖垮写门
            candidate = None
            fallback = {"used": True, "backend": name,
                        "reason": "归纳后端抛错：%s: %s" % (type(exc).__name__, str(exc)[:120])}
        else:
            candidate = candidate if isinstance(candidate, Mapping) else None
            if candidate is None:
                fallback = {"used": True, "backend": name, "reason": "归纳后端返回了非法载荷"}
        if isinstance(candidate, Mapping):
            payload = dict(candidate)
        else:
            backend_label = "rule:trace-v1"
    if not payload:
        payload = episode_summary_rule(trace)
    run = _mapping(trace.get("run"))
    episode = {
        "episode_version": EPISODE_MEMORY_VERSION,
        "run_id": str(run.get("id") or ""),
        "sections": payload.get("sections") if isinstance(payload.get("sections"), Mapping) else {},
        "summary": canonicalize(str(payload.get("summary") or "")),
        "task_type": str(payload.get("task_type") or trace.get("task_type") or ""),
        "effective_routes": [str(item) for item in (payload.get("effective_routes") or [])],
        "key_gaps": [dict(item) for item in (payload.get("key_gaps") or []) if isinstance(item, Mapping)],
        "stop_reason": str(payload.get("stop_reason") or ""),
        "trace_fingerprint": str(trace.get("trace_fingerprint") or trace_fingerprint(trace)),
        "summarizer": str(payload.get("summarizer") or backend_label),
        "missing_sections": [str(item) for item in (trace.get("missing_sections") or [])],
        "captured_at": str(trace.get("captured_at") or ""),
        "fallback": fallback,
        "counts": dict(payload.get("counts") or {}),
        "route_yields": dict(payload.get("route_yields") or {}),
    }
    episode["sections"] = {name: episode["sections"].get(name) for name in EPISODE_SECTIONS}
    ok, note = validate_contract("episode_memory", dict(episode))
    episode["contract_ok"] = bool(ok)
    if not ok:
        episode["contract_error"] = note
    return episode


def episode_confidence(episode: Mapping) -> float:
    """剧集置信：证据量与已核验产出越高越可信（**纯统计**，逐项可复算）。"""
    counts = _mapping((episode or {}).get("counts"))
    verified = int(counts.get("verified") or 0)
    evidence = int(counts.get("evidence") or 0)
    return _round(_clamp(0.30 + 0.12 * min(verified, 4) + 0.05 * min(evidence, 2)), 8)


# ── P12-02：失败记忆（四类 + 版本绑定）────────────────────────────────────

def _binding_of(trace: Mapping) -> dict:
    corpus = corpus_binding(trace)
    config = retrieval_config_token(trace)
    return {"corpus_version": str(corpus.get("token") or ""),
            "corpus_source": str(corpus.get("source") or ""),
            "retrieval_config_version": str(config.get("token") or ""),
            "config_source": str(config.get("source") or "")}


def query_fingerprint(query) -> str:
    """查询指纹（归一化后内容寻址）：同一句话（大小写/空白不同）恒等。"""
    return "QF" + _digest(canonicalize(query).casefold())


def detect_failures(trace: Mapping, *, now=None) -> list:
    """P12-02：从 trace 判出四类失败（每条都带五个绑定字段，**版本号不编**）。

    规则（先判先返回，一条 trace 里每条失败最多一条，可复算）：

      · `FAILED_ROUTE`：**这一轮真的试过**该通道（`_ATTEMPT_SIGNALS`/政策精确判据），
        但采纳为 0；或采纳 > 0 而走该通道的证据**没有一条**拿到 SUPPORTED。
      · `INVALID_QUERY`：本轮发出了查询、候选池非空（`keyword_candidates > 0`）却零采纳
        —— 逐查询记一条（最多 3 条），正文如实写"该查询所在的一轮零采纳"
        （**不宣称"这句话本身无效"**，那是没有逐查询归因时的越界断言）。
      · `FALSE_POSITIVE_SOURCE`：某个域的证据里**有被 Phase 03 判 `REFUTED`** 的
        （"看着相关、其实反证"），逐域一条。
      · `SEMANTIC_DRIFT`：某条证据**没有任何标题族命中**（只有分类词/核心词/正文短语）
        且没拿到 SUPPORTED —— 扩展把检索带偏了，按 (通道, 查询指纹) 归并一条。
    """
    trace = trace if isinstance(trace, Mapping) else {}
    run = _mapping(trace.get("run"))
    retrieval = _mapping(trace.get("retrieval"))
    binding = _binding_of(trace)
    observed_at = str(trace.get("captured_at") or _stamp(_now(now)))
    task_type = str(trace.get("task_type") or "")
    gap_types = list(_mapping(trace.get("gaps")).get("types") or {})
    gap_type = gap_types[0] if gap_types else ""
    queries = [str(item) for item in (retrieval.get("queries") or []) if str(item).strip()]
    stats = _mapping(retrieval.get("stats"))
    routes = _mapping(retrieval.get("routes"))
    # 关注点：计划里的实体（**trace 真值**）。写进正文是为了让这条经验在**同一话题**的
    # 下一轮能被规划期召回 —— 召不回来的经验等于没记（§10 planning recall）。
    focus = [str(item) for item in (_mapping(trace.get("planning")).get("entities") or [])
             if str(item).strip()][:3]
    failures: list = []

    def base(**kwargs) -> dict:
        row = {
            "failure_version": FAILURE_MEMORY_VERSION,
            "kind": "", "route": "", "query": "", "query_fingerprint": "",
            "corpus_version": binding["corpus_version"],
            "retrieval_config_version": binding["retrieval_config_version"],
            "binding_source": ("%s|%s" % (binding["corpus_source"], binding["config_source"])).strip("|"),
            "observed_at": observed_at, "task_type": task_type, "gap_type": gap_type,
            "domain": "", "adopted": 0, "rejected": 0, "verified": 0,
            "evidence_refs": [], "detail": "", "focus": list(focus),
        }
        row.update(kwargs)
        return row

    rejected_total = sum(int(value) for key, value in _mapping(retrieval.get("excluded")).items()
                         if key in ("quality_gate", "inactive", "stale_activation", "policy_registry"))
    for route, row in sorted(routes.items()):
        row = _mapping(row)
        if not row.get("attempted"):
            continue
        adopted = int(row.get("adopted") or 0)
        verified = int(row.get("verified") or 0)
        if adopted <= 0:
            failures.append(base(kind="FAILED_ROUTE", route=route, adopted=0,
                                 rejected=rejected_total, verified=0,
                                 detail=("通道 %s 这一轮真的跑过但没有采纳任何证据"
                                         "（本轮候选被闸门拒掉 %d 条）" % (route, rejected_total))))
        elif verified <= 0 and row.get("evidence_refs"):
            failures.append(base(kind="FAILED_ROUTE", route=route, adopted=adopted,
                                 rejected=rejected_total, verified=0,
                                 evidence_refs=list(row.get("evidence_refs") or [])[:10],
                                 detail=("通道 %s 采纳了 %d 条证据但没有一条通过核验（零已验证产出）"
                                         % (route, adopted))))
    candidates = int(stats.get("keyword_candidates") or 0)
    adopted_total = len([item for item in (trace.get("sources") or []) if isinstance(item, Mapping)])
    if queries and candidates > 0 and adopted_total == 0:
        for query in queries[:3]:
            failures.append(base(kind="INVALID_QUERY", route="keyword", query=query,
                                 query_fingerprint=query_fingerprint(query),
                                 rejected=rejected_total,
                                 detail=("该查询所在的一轮检索在 %d 条候选里零采纳"
                                         "（查询指纹 %s；本轮真实发出的查询）"
                                         % (candidates, query_fingerprint(query)))))
    by_domain: dict = {}
    drift: dict = {}
    for item in (trace.get("sources") or []):
        if not isinstance(item, Mapping):
            continue
        domain = str(item.get("domain") or "")
        row = by_domain.setdefault(domain, {"refuted": 0, "verified": 0, "total": 0, "refs": []})
        row["total"] += 1
        row["refs"].append(str(item.get("evidence_ref") or ""))
        if str(item.get("verdict") or "") == "SUPPORTED":
            row["verified"] += 1
        if str(item.get("verdict") or "") == "REFUTED":
            row["refuted"] += 1
            failures.append(base(kind="FALSE_POSITIVE_SOURCE", domain=domain, route=str(item.get("route") or ""),
                                 verified=0, adopted=row["total"], rejected=row["refuted"],
                                 evidence_refs=[str(item.get("evidence_ref") or "")],
                                 detail=("域 %s 的一条证据被 Phase 03 判为反驳（false positive）："
                                         "看着相关但结论相反" % (domain or "未知域"))))
        families = [str(name) for name in (item.get("match_families") or [])]
        weak = bool(families) and not any(name.startswith("标题") for name in families)
        if weak and str(item.get("verdict") or "") != "SUPPORTED":
            key = (str(item.get("route") or ""), domain)
            bucket = drift.setdefault(key, {"refs": [], "families": set(), "domain": domain})
            bucket["refs"].append(str(item.get("evidence_ref") or ""))
            bucket["families"].update(families)
    for (route, domain), bucket in sorted(drift.items()):
        query = queries[0] if queries else ""
        failures.append(base(kind="SEMANTIC_DRIFT", route=route, domain=domain, query=query,
                             query_fingerprint=query_fingerprint(query) if query else "",
                             adopted=len(bucket["refs"]), verified=0,
                             rejected=rejected_total,
                             evidence_refs=list(bucket["refs"])[:10],
                             detail=("通道 %s 上有 %d 条证据只命中非标题族（%s）且未通过核验："
                                     "疑似扩展词把检索带偏"
                                     % (route or "未知通道", len(bucket["refs"]),
                                        "、".join(sorted(bucket["families"]))))))
    for row in failures:
        row["detail"] = canonicalize(row["detail"])
        row["failure_id"] = failure_id(row)
        ok, note = validate_contract("failure_memory", dict(row))
        row["contract_ok"] = bool(ok)
        if not ok:
            row["contract_error"] = note
    return failures


def failure_content(failure: Mapping) -> str:
    """失败记忆的正文（**去掉可变部分**后内容寻址：时间不进正文，进 metadata）。"""
    failure = failure if isinstance(failure, Mapping) else {}
    kind = str(failure.get("kind") or "")
    label = {"FAILED_ROUTE": "失败通道", "INVALID_QUERY": "无效查询",
             "FALSE_POSITIVE_SOURCE": "误导来源", "SEMANTIC_DRIFT": "语义漂移"}.get(kind, kind)
    scope = []
    if failure.get("route"):
        scope.append("通道 %s" % failure["route"])
    if failure.get("query"):
        scope.append("查询「%s」" % str(failure["query"])[:60])
    if failure.get("domain"):
        scope.append("来源域 %s" % failure["domain"])
    binding = []
    if failure.get("corpus_version"):
        binding.append("语料 %s" % failure["corpus_version"])
    if failure.get("retrieval_config_version"):
        binding.append("检索配置 %s" % failure["retrieval_config_version"])
    focus = [str(item) for item in (failure.get("focus") or []) if str(item).strip()][:3]
    context = []
    if failure.get("task_type"):
        context.append("任务类型 %s" % failure["task_type"])
    if focus:
        context.append("关注点 %s" % "、".join(focus))
    return canonicalize("经验·%s：%s。%s；%s。%s环境变化后允许重试。"
                        % (label, "；".join(scope) or "（未归因到具体对象）",
                           str(failure.get("detail") or ""),
                           "，".join(binding) if binding else "无版本绑定（一律允许重试）",
                           ("情境：%s。" % "、".join(context)) if context else ""))


def failure_confidence(failure: Mapping) -> float:
    """失败置信：观测越硬（有采纳/拒绝计数、有证据引用）越可信。"""
    failure = failure if isinstance(failure, Mapping) else {}
    return _round(_clamp(0.45 + 0.06 * min(int(failure.get("adopted") or 0), 4)
                         + 0.02 * min(int(failure.get("rejected") or 0), 5)), 8)


def failure_candidates(trace: Mapping, *, scope: str = "", now=None) -> list:
    """把四类失败转成写门候选（经验类：带 `trace_ref` 绑定 + `FAILED_ON` 关系）。"""
    trace = trace if isinstance(trace, Mapping) else {}
    run = _mapping(trace.get("run"))
    scope = str(scope or experience_scope())
    run_refs = [str(item.get("evidence_ref") or "") for item in (trace.get("sources") or [])
                if isinstance(item, Mapping)]
    candidates: list = []
    for failure in detect_failures(trace, now=now):
        content = failure_content(failure)
        refs = [str(ref) for ref in (failure.get("evidence_refs") or []) if str(ref)]
        # provenance 口径：失败记忆必须能回到证据。**零采纳类失败本来就没有产出证据**
        # （它记的正是"没有产出"），这类退到"该轮 trace 里的证据"作为派生依据，
        # 并在 metadata 里写明依据类型（`evidence_link_basis`）——不冒充成"这条失败
        # 的证据"。
        basis = "failure_yield" if refs else "run_trace"
        links = _evidence_links(trace, refs=refs) if refs else _evidence_links(
            trace, refs=_episode_evidence_refs(trace))
        relations = []
        if failure.get("route"):
            relations.append({"relation": "FAILED_ON", "target_kind": "route",
                              "target_ref": str(failure["route"]), "weight": 0.8,
                              "rationale": str(failure.get("kind") or "")})
        if failure.get("domain"):
            relations.append({"relation": "FAILED_ON", "target_kind": "domain",
                              "target_ref": str(failure["domain"]), "weight": 0.6,
                              "rationale": str(failure.get("kind") or "")})
        candidates.append({
            "memory_type": "FAILURE",
            "canonical_content": content,
            "claim_type": EXPERIENCE_CLAIM_TYPES["FAILURE"],
            "confidence": failure_confidence(failure),
            "freshness_class": FRESHNESS_BY_TYPE["FAILURE"],
            "valid_from": str(failure.get("observed_at") or ""),
            "valid_until": "",
            "entity_ids": [str(failure.get("route") or "") or str(failure.get("domain") or "")],
            "evidence": links,
            "supporting": 0,
            "scope": scope,
            "owner_user_id": str(run.get("owner_user_id") or ""),
            "session_id": str(run.get("session_id") or ""),
            "industry_pack_id": str(run.get("industry_pack_id") or ""),
            "run_id": str(run.get("id") or ""),
            "trace_ref": str(trace.get("trace_fingerprint") or ""),
            "samples": max(1, int(failure.get("adopted") or 0) + int(failure.get("rejected") or 0)),
            "sample_floor": 0,
            "relations": relations,
            "metadata": {
                "kind": str(failure.get("kind") or ""),
                "route": str(failure.get("route") or ""),
                "domain": str(failure.get("domain") or ""),
                "query_fingerprint": str(failure.get("query_fingerprint") or ""),
                "corpus_version": str(failure.get("corpus_version") or ""),
                "retrieval_config_version": str(failure.get("retrieval_config_version") or ""),
                "binding_source": str(failure.get("binding_source") or ""),
                "observed_at": str(failure.get("observed_at") or ""),
                "task_type": str(failure.get("task_type") or ""),
                "gap_type": str(failure.get("gap_type") or ""),
                "adopted": int(failure.get("adopted") or 0),
                "rejected": int(failure.get("rejected") or 0),
                "verified": int(failure.get("verified") or 0),
                "evidence_refs": list(failure.get("evidence_refs") or []),
                "evidence_link_basis": basis,
                "trace_fingerprint": str(trace.get("trace_fingerprint") or ""),
                "experience_version": EXPERIENCE_MEMORY_VERSION,
                "hint_only": True,
            },
        })
    return candidates


# ── P12-06：版本感知重试（MASTER_RULES 15）────────────────────────────────

def retry_decision(failure: Mapping, *, corpus_version: str = "",
                    retrieval_config_version: str = "", now=None,
                    max_age_days: float | None = None) -> dict:
    """P12-06：**唯一**能压制一条失败的条件是"环境没变"（先判先返回，可复算）。

    判定顺序：
      1) 没有失败记录 → `ALLOW_RETRY/NO_FAILURE_RECORD`；
      2) 语料版本两侧都非空且不同 → `ALLOW_RETRY/CORPUS_VERSION_CHANGED`；
      3) 检索配置两侧都非空且不同 → `ALLOW_RETRY/RETRIEVAL_CONFIG_CHANGED`；
      4) **没有任何一个轴能证明"没变"**（缺绑定/单侧为空）→ `ALLOW_RETRY/VERSION_UNBOUND`
         —— 压制必须建立在"证明环境相同"上，证明不了就不压制（MASTER_RULES 15）；
      5) 超过压制时效 → `ALLOW_RETRY/FAILURE_AGED_OUT`；
      6) 否则 → `SUPPRESS/SAME_ENVIRONMENT`。
    """
    failure = failure if isinstance(failure, Mapping) else {}
    limit = failure_max_age_days() if max_age_days is None else float(max_age_days)
    recorded_corpus = str(failure.get("corpus_version") or "").strip()
    recorded_config = str(failure.get("retrieval_config_version") or "").strip()
    current_corpus = str(corpus_version or "").strip()
    current_config = str(retrieval_config_version or "").strip()
    observed = _parse_time(failure.get("observed_at"))
    moment = _now(now)
    age_days = _round((moment - observed).total_seconds() / 86400.0, 4) if observed else 0.0

    def row(decision: str, reason: str) -> dict:
        detail = {
            "retry_version": RETRY_POLICY_VERSION,
            "failure_id": str(failure.get("failure_id") or ""),
            "kind": str(failure.get("kind") or ""),
            "route": str(failure.get("route") or ""),
            "decision": decision, "reason": reason,
            "corpus_version_recorded": recorded_corpus, "corpus_version_current": current_corpus,
            "config_version_recorded": recorded_config, "config_version_current": current_config,
            "age_days": age_days, "max_age_days": _round(limit, 4),
        }
        ok, note = validate_contract("retry_decision", dict(detail))
        detail["contract_ok"] = bool(ok)
        if not ok:
            detail["contract_error"] = note
        return detail

    if not str(failure.get("kind") or ""):
        return row("ALLOW_RETRY", "NO_FAILURE_RECORD")
    if recorded_corpus and current_corpus and recorded_corpus != current_corpus:
        return row("ALLOW_RETRY", "CORPUS_VERSION_CHANGED")
    if recorded_config and current_config and recorded_config != current_config:
        return row("ALLOW_RETRY", "RETRIEVAL_CONFIG_CHANGED")
    # 压制必须**证明环境没变**：记录侧记过的每一条轴，当前环境都要能给出一致值。
    # 记了但当前未知（or 反向缺失）→ 证明不了 → 一律允许重试（MASTER_RULES 15 的安全方向）。
    axes = [(recorded_corpus, current_corpus), (recorded_config, current_config)]
    bound = [pair for pair in axes if pair[0]]
    if not bound:
        return row("ALLOW_RETRY", "VERSION_UNBOUND")
    for recorded, current in bound:
        if not current:
            return row("ALLOW_RETRY", "VERSION_UNBOUND")
    if observed and age_days > limit:
        return row("ALLOW_RETRY", "FAILURE_AGED_OUT")
    return row("SUPPRESS", "SAME_ENVIRONMENT")


def failure_id(failure: Mapping) -> str:
    """失败记忆的稳定 id（= 它的内容寻址 id 的短形式，便于在指导回执里引用）。"""
    failure = failure if isinstance(failure, Mapping) else {}
    return "FAIL" + _digest("%s|%s|%s|%s" % (failure.get("kind"), failure.get("route"),
                                             failure.get("domain"),
                                             failure_content(failure)))[:16]


# ── P12-03：策略记忆 + 查询形态（检索组合的有效率）────────────────────────

def query_pattern_of(question) -> str:
    """查询形态（**可复算的特征元组**，不是模型写的分类）。

    特征：是否有实体（≥2 字实词）、是否含时间词、是否含疑问词、长度档、是否含对比词。
    """
    text = canonicalize(question)
    if not text:
        return ""
    terms = term_set(text, sizes=(2, 3))
    has_time = bool(re.search(r"(\d{4}\s*年|本周|上周|本月|上月|最近|今日|昨日|季度|年度)", text))
    has_question = bool(re.search(r"(什么|哪些|哪个|如何|怎么|为什么|是否|多少|几)", text))
    has_compare = bool(re.search(r"(对比|相比|比较|更|区别|差异|分别)", text))
    length = "SHORT" if len(text) <= 12 else ("MEDIUM" if len(text) <= 30 else "LONG")
    return ("entity=%d|time=%d|question=%d|compare=%d|len=%s|terms=%d"
            % (1 if len(terms) >= 2 else 0, 1 if has_time else 0, 1 if has_question else 0,
               1 if has_compare else 0, length, min(len(terms), 20)))


def _route_table(traces: Sequence[Mapping]) -> dict:
    """跨 trace 的通道有效率表（只统计**真的试过**的通道）。"""
    table: dict = {}
    for trace in traces or ():
        if not isinstance(trace, Mapping):
            continue
        task_type = str(trace.get("task_type") or "")
        route_rows = _mapping(_mapping(trace.get("retrieval")).get("routes"))
        attempted = [route for route, row in route_rows.items()
                     if isinstance(row, Mapping) and row.get("attempted")]
        for route in sorted(attempted):
            bucket = table.setdefault((task_type, route), {"observations": 0, "successes": 0,
                                                           "verified_yield": 0, "run_ids": []})
            bucket["observations"] += 1
            verified = int(_mapping(route_rows.get(route)).get("verified") or 0)
            bucket["verified_yield"] += verified
            if verified > 0:
                bucket["successes"] += 1
            run_id = str(_mapping(trace.get("run")).get("id") or "")
            if run_id:
                bucket["run_ids"].append(run_id)
    return table


def strategy_rows(traces: Sequence[Mapping], *, min_samples: int | None = None,
                  success_floor: float | None = None, now=None) -> list:
    """P12-03：按 (任务类型, 通道) 统计有效率，再拼成"在这个任务类型上有效的通道组合"。

    成功率口径（写死，可复算）：`successes / observations`，
    成功 ⟺ 那一次该通道**至少产出 1 条通过 Phase 03 核验的证据**；
    `observations < min_samples` 一律不参与（不许拿一次观测当规律）；
    有效率 ≥ `route_success_floor`（默认 0.60）的通道才算"有效"，组合 = 这些通道（按名排序）。
    组合自身的样本 = **该任务类型下所有有效通道都被试过**的那些轮，
    成功 = 这些轮里**每条有效通道都产出了已核验证据**（保守口径，宁可低报）。
    """
    gate = experience_min_samples() if min_samples is None else max(1, int(min_samples))
    floor = route_success_floor() if success_floor is None else _clamp(success_floor, 0.05, 1.0)
    traces = [trace for trace in (traces or []) if isinstance(trace, Mapping)]
    table = _route_table(traces)
    task_types = sorted({task_type for task_type, _route in table})
    rows: list = []
    for task_type in task_types:
        winners = []
        for (row_task, route), bucket in sorted(table.items()):
            if row_task != task_type or bucket["observations"] < gate:
                continue
            rate = bucket["successes"] / bucket["observations"]
            if rate >= floor:
                winners.append((route, bucket, rate))
        if not winners:
            continue
        combination = [route for route, _bucket, _rate in winners]
        # 组合样本：只数"这些通道全都被试过"的轮（可比较的样本）
        comparable: list = []
        for trace in traces:
            if str(trace.get("task_type") or "") != task_type:
                continue
            route_rows = _mapping(_mapping(trace.get("retrieval")).get("routes"))
            tried = {route for route, row in route_rows.items()
                     if isinstance(row, Mapping) and row.get("attempted")}
            if not set(combination) <= tried:
                continue
            ok = all(int(_mapping(route_rows.get(route)).get("verified") or 0) > 0
                     for route in combination)
            comparable.append((str(_mapping(trace.get("run")).get("id") or ""), ok))
        observations = len(comparable) or min(bucket["observations"] for _r, bucket, _rate in winners)
        successes = len([row for row in comparable if row[1]])
        run_ids = sorted({run_id for run_id, _ok in comparable if run_id}) or \
            sorted({run_id for _route, bucket, _rate in winners for run_id in bucket["run_ids"]})
        rows.append({
            "strategy_version": STRATEGY_MEMORY_VERSION,
            "task_type": task_type,
            "combination": combination,
            "observations": int(observations), "successes": int(successes),
            "success_rate": _round(successes / observations if observations else 0.0),
            "verified_yield": int(sum(bucket["verified_yield"] for _route, bucket, _rate in winners)),
            "min_samples": int(gate), "success_floor": _round(floor, 6),
            "run_ids": run_ids[:10],
            "success_definition": ("该通道那一次至少产出 1 条 Phase 03 判 SUPPORTED 的证据；"
                                   "组合成功率 = 组合里每条通道都做到的那些轮 / 都可比较的轮"),
            "routes_detail": {route: {"observations": bucket["observations"],
                                      "successes": bucket["successes"],
                                      "success_rate": _round(rate)}
                              for route, bucket, rate in winners},
        })
    return rows


def strategy_content(row: Mapping) -> str:
    row = row if isinstance(row, Mapping) else {}
    detail = _mapping(row.get("routes_detail"))
    parts = ["%s %d/%d" % (route, int(_mapping(detail.get(route)).get("successes") or 0),
                           int(_mapping(detail.get(route)).get("observations") or 0))
             for route in (row.get("combination") or [])]
    return canonicalize("经验·策略：任务类型 %s 下通道组合 [%s] 的有效率 %d%%（%d/%d 次可比较观测；"
                        "逐通道 %s；成功 = 该通道那次至少产出 1 条已核验证据）。"
                        % (str(row.get("task_type") or "未分类"),
                           "、".join(str(item) for item in (row.get("combination") or [])),
                           int(round(100 * _as_float(row.get("success_rate")))),
                           int(row.get("successes") or 0), int(row.get("observations") or 0),
                           "；".join(parts)))


def strategy_candidates(traces: Sequence[Mapping], *, scope: str = "", min_samples: int | None = None,
                        now=None) -> list:
    """策略记忆候选（经验类：带 trace 绑定 + 真实证据链接 + `HELPED_RESOLVE` 关系指向真实 run）。"""
    gate = experience_min_samples() if min_samples is None else max(1, int(min_samples))
    traces = [trace for trace in (traces or []) if isinstance(trace, Mapping)]
    run_meta = _run_meta_of(traces)
    candidates: list = []
    for row in strategy_rows(traces, min_samples=gate, now=now):
        rate = _as_float(row.get("success_rate"))
        combination = list(row.get("combination") or [])
        by_route = _refs_by_routes(traces, combination)
        refs = [ref for route in sorted(by_route) for ref in by_route[route]]
        candidates.append({
            "memory_type": "STRATEGY",
            "canonical_content": strategy_content(row),
            "claim_type": EXPERIENCE_CLAIM_TYPES["STRATEGY"],
            "confidence": _round(_clamp(0.25 + 0.50 * rate), 8),
            "freshness_class": FRESHNESS_BY_TYPE["STRATEGY"],
            "valid_from": str(run_meta.get("observed_at") or ""),
            "entity_ids": [],
            "evidence": _links_for_traces(traces, refs),
            "scope": str(scope or experience_scope()),
            "owner_user_id": str(run_meta.get("owner_user_id") or ""),
            "session_id": str(run_meta.get("session_id") or ""),
            "industry_pack_id": str(run_meta.get("industry_pack_id") or ""),
            "run_id": str(run_meta.get("run_id") or ""),
            "trace_ref": str(run_meta.get("trace_fingerprint") or ""),
            "samples": int(row.get("observations") or 0),
            "sample_floor": int(gate),
            "relations": [{"relation": "HELPED_RESOLVE", "target_kind": "run",
                           "target_ref": str(run_id), "weight": rate}
                          for run_id in (row.get("run_ids") or [])[:5]],
            "metadata": {
                "combination": list(row.get("combination") or []),
                "task_type": str(row.get("task_type") or ""),
                "observations": int(row.get("observations") or 0),
                "successes": int(row.get("successes") or 0),
                "success_rate": rate,
                "min_samples": int(gate),
                "success_floor": _as_float(row.get("success_floor")),
                "success_definition": str(row.get("success_definition") or ""),
                "routes_detail": dict(row.get("routes_detail") or {}),
                "run_ids": list(row.get("run_ids") or []),
                "strategy_version": STRATEGY_MEMORY_VERSION,
                "experience_version": EXPERIENCE_MEMORY_VERSION,
                "hint_only": True,
            },
        })
    return candidates


def query_pattern_candidates(traces: Sequence[Mapping], *, scope: str = "", min_samples: int | None = None,
                             now=None) -> list:
    """查询形态记忆（P12 拥有 `QUERY_PATTERN` 类）：**哪种问法在本语料上真的取到过证据**。

    形态 = 可复算的特征元组（`query_pattern_of`）；成功 ⟺ 该轮有已核验支持证据。
    与 `INVALID_QUERY` 失败记忆互为对偶（一个记有效的，一个记无效的）。
    """
    gate = experience_min_samples() if min_samples is None else max(1, int(min_samples))
    buckets: dict = {}
    for trace in traces or ():
        if not isinstance(trace, Mapping):
            continue
        question = str(_mapping(trace.get("run")).get("question") or "")
        pattern = query_pattern_of(question)
        if not pattern:
            continue
        task_type = str(trace.get("task_type") or "")
        key = (task_type, pattern)
        bucket = buckets.setdefault(key, {"observations": 0, "successes": 0, "queries": [],
                                          "run_ids": [], "verified": 0})
        bucket["observations"] += 1
        verified = int(_mapping(trace.get("verification")).get("supported") or 0)
        bucket["verified"] += verified
        if verified > 0:
            bucket["successes"] += 1
        bucket["queries"].append(question[:60])
        run_id = str(_mapping(trace.get("run")).get("id") or "")
        if run_id:
            bucket["run_ids"].append(run_id)
    run_meta = _run_meta_of(traces)
    candidates: list = []
    for (task_type, pattern), bucket in sorted(buckets.items()):
        if bucket["observations"] < gate:
            continue
        rate = bucket["successes"] / bucket["observations"]
        examples = [str(item) for item in (bucket["queries"] or []) if str(item).strip()][:2]
        content = canonicalize("经验·查询形态：任务类型 %s 下形态 %s 的成功率 %d%%（%d/%d 次观测；"
                               "成功 = 该形态的提问取到过已核验证据）；实例提问：%s。"
                               % (task_type or "未分类", pattern, int(round(100 * rate)),
                                  bucket["successes"], bucket["observations"],
                                  "；".join("「%s」" % item for item in examples) or "无"))
        # 证据绑定：该形态的每一轮里真实取到的证据（已核验的排在前面）
        matched = [trace for trace in traces
                   if str(trace.get("task_type") or "") == task_type
                   and query_pattern_of(str(_mapping(trace.get("run")).get("question") or "")) == pattern]
        by_route = _refs_by_routes(matched, ())
        refs = [ref for route in sorted(by_route) for ref in by_route[route]]
        candidates.append({
            "memory_type": "QUERY_PATTERN",
            "canonical_content": content,
            "claim_type": EXPERIENCE_CLAIM_TYPES["QUERY_PATTERN"],
            "confidence": _round(_clamp(0.25 + 0.50 * rate), 8),
            "freshness_class": FRESHNESS_BY_TYPE["QUERY_PATTERN"],
            "valid_from": str(run_meta.get("observed_at") or ""),
            "entity_ids": [],
            "evidence": _links_for_traces(traces, refs),
            "scope": str(scope or experience_scope()),
            "owner_user_id": str(run_meta.get("owner_user_id") or ""),
            "session_id": str(run_meta.get("session_id") or ""),
            "industry_pack_id": str(run_meta.get("industry_pack_id") or ""),
            "run_id": str(run_meta.get("run_id") or ""),
            "trace_ref": str(run_meta.get("trace_fingerprint") or ""),
            "samples": int(bucket["observations"]),
            "sample_floor": int(gate),
            "metadata": {"pattern": pattern, "task_type": task_type,
                         "observations": int(bucket["observations"]),
                         "successes": int(bucket["successes"]), "success_rate": _round(rate),
                         "queries": bucket["queries"][:5], "run_ids": bucket["run_ids"][:10],
                         "experience_version": EXPERIENCE_MEMORY_VERSION, "hint_only": True},
        })
    return candidates


def _run_meta_of(traces: Sequence[Mapping]) -> dict:
    """从一组 trace 里取一个代表 run 元信息（作用域/绑定用；取最后一轮，确定性）。"""
    rows = [trace for trace in (traces or []) if isinstance(trace, Mapping)]
    if not rows:
        return {}
    last = rows[-1]
    run = _mapping(last.get("run"))
    return {
        "run_id": str(run.get("id") or ""), "owner_user_id": str(run.get("owner_user_id") or ""),
        "session_id": str(run.get("session_id") or ""),
        "industry_pack_id": str(run.get("industry_pack_id") or ""),
        "observed_at": str(last.get("captured_at") or ""),
        "trace_fingerprint": str(last.get("trace_fingerprint") or ""),
        "run_ids": [str(_mapping(trace.get("run")).get("id") or "") for trace in rows],
    }


# ── P12-04：来源可靠性 ─────────────────────────────────────────────────────

_AUTHORITY_SCORE = ((1, 1.0), (10, 0.85), (30, 0.70), (60, 0.55))
"""权威等级 → [0,1] 分数（等级越小越权威；沿用 `qa_verifier` 的"数值越小越权威"口径，
不另造一套打分）。"""


def authority_score(level) -> float:
    if level in (None, ""):
        return 0.50
    value = _as_float(level, -1.0)
    if value < 0:
        return 0.50
    for threshold, score in _AUTHORITY_SCORE:
        if value <= threshold:
            return score
    return 0.40


def source_reliability_rows(trace: Mapping, *, now=None, half_life_days: float = 365.0) -> list:
    """P12-04：**单 trace** 内按域聚合的证据统计（六因子的原料，全部来自真实证据行）。

    时效因子用"最近一次发布时间的年龄"的确定性半衰期衰减（缺发布时间记 `recency=0.5`
    并在 `known_limitations` 里写明"发布时间缺失"，**不猜**）。
    """
    trace = trace if isinstance(trace, Mapping) else {}
    moment = _now(now or trace.get("captured_at"))
    by_domain: dict = {}
    corroboration_index = _corroboration_by_domain(trace)
    for item in (trace.get("sources") or []):
        if not isinstance(item, Mapping):
            continue
        domain = str(item.get("domain") or "")
        if not domain:
            continue
        bucket = by_domain.setdefault(domain, {"total": 0, "verified": 0, "refuted": 0,
                                               "refs": [], "levels": [], "published": []})
        bucket["total"] += 1
        verdict = str(item.get("verdict") or "")
        if verdict == "SUPPORTED":
            bucket["verified"] += 1
        elif verdict == "REFUTED":
            bucket["refuted"] += 1
        ref = str(item.get("evidence_ref") or "")
        if ref:
            bucket["refs"].append(ref)
        if item.get("authority_level") not in (None, ""):
            bucket["levels"].append(item.get("authority_level"))
        if item.get("published_at"):
            bucket["published"].append(str(item.get("published_at")))
    rows: list = []
    for domain, bucket in sorted(by_domain.items()):
        total = max(1, bucket["total"])
        verified_rate = bucket["verified"] / total
        rejection_rate = bucket["refuted"] / total
        authority = authority_score(min(bucket["levels"]) if bucket["levels"] else None)
        recency = 0.50
        if bucket["published"]:
            ages = [(moment - published).total_seconds() / 86400.0
                    for published in (_parse_time(value) for value in bucket["published"])
                    if published is not None]
            if ages:
                newest = max(0.0, min(ages))
                recency = _round(_clamp(0.5 ** (newest / half_life_days)), 8)
        corroboration = _round(_clamp(corroboration_index.get(domain, 0.0)))
        volume = _clamp(bucket["total"] / 3.0)
        factors = {
            "authority": _round(authority), "verified_yield": _round(verified_rate),
            "rejection_rate": _round(rejection_rate), "recency": _round(recency),
            "corroboration": _round(corroboration), "volume": _round(volume),
        }
        reliability = _round(_clamp(0.25 * factors["authority"] + 0.30 * factors["verified_yield"]
                                    + 0.15 * (1.0 - factors["rejection_rate"])
                                    + 0.15 * factors["recency"] + 0.10 * factors["corroboration"]
                                    + 0.05 * factors["volume"]), 8)
        rows.append({
            "source_version": SOURCE_MEMORY_VERSION,
            "domain": domain,
            "authority_level": min(bucket["levels"]) if bucket["levels"] else None,
            "reliability": reliability, "factors": factors,
            "observations": bucket["total"], "verified": bucket["verified"],
            "rejected": bucket["refuted"], "evidence_refs": bucket["refs"][:20],
            "update_frequency": "UNKNOWN",
            "run_ids": [str(_mapping(trace.get("run")).get("id") or "")],
            "task_type": str(trace.get("task_type") or ""),
        })
    return rows


def _corroboration_by_domain(trace: Mapping) -> dict:
    """多来源印证：同一结论上有几个不同域在支持它（取该域所有结论里的最大值，归一到 3 域）。"""
    trace = trace if isinstance(trace, Mapping) else {}
    claim_domains: dict = {}
    domain_claims: dict = {}
    for item in (trace.get("sources") or []):
        if not isinstance(item, Mapping):
            continue
        domain = str(item.get("domain") or "")
        if not domain:
            continue
        for claim_id in (item.get("claim_ids") or []):
            claim = str(claim_id or "")
            if not claim:
                continue
            claim_domains.setdefault(claim, set()).add(domain)
            domain_claims.setdefault(domain, set()).add(claim)
    out: dict = {}
    for domain, claims in domain_claims.items():
        best = 0
        for claim_id in claims:
            best = max(best, len(claim_domains.get(claim_id) or ()))
        out[domain] = min(1.0, best / 3.0)
    return out


def source_band(row: Mapping, *, min_samples: int | None = None) -> str:
    """分档：样本不足一律 `UNKNOWN`（不猜）；否则按可靠性四档。"""
    gate = experience_min_samples() if min_samples is None else max(1, int(min_samples))
    if int((row or {}).get("observations") or 0) < gate:
        return "UNKNOWN"
    value = _as_float((row or {}).get("reliability"))
    if value >= 0.75:
        return "HIGH"
    if value >= 0.55:
        return "MEDIUM"
    return "LOW"


def source_limitations(row: Mapping, *, min_samples: int | None = None) -> list:
    """已知局限（确定性文案，只写**能从数据看出来**的）。"""
    gate = experience_min_samples() if min_samples is None else max(1, int(min_samples))
    row = row if isinstance(row, Mapping) else {}
    out: list = []
    if int(row.get("observations") or 0) < gate:
        out.append("样本量 %d 少于门槛 %d：不给可靠档位" % (int(row.get("observations") or 0), gate))
    if int(row.get("rejected") or 0) > 0:
        out.append("有 %d 条证据被核验判为反驳（误导风险）" % int(row.get("rejected") or 0))
    if row.get("authority_level") in (None, ""):
        out.append("权威等级缺失")
    if _as_float(_mapping(row.get("factors")).get("recency")) == 0.5:
        out.append("发布时间缺失或过旧：时效因子取中性值 0.5")
    if str(row.get("update_frequency") or "UNKNOWN") == "UNKNOWN":
        out.append("更新频率未知（本阶段不从站点抓取节奏推断）")
    return out


def aggregate_source_reliability(traces: Sequence[Mapping], *, min_samples: int | None = None,
                                 now=None) -> list:
    """跨 trace 聚合来源可靠性（同一域的证据合起来看；权重与单轮相同，只是样本更多）。"""
    merged: dict = {}
    for trace in traces or ():
        if not isinstance(trace, Mapping):
            continue
        for row in source_reliability_rows(trace, now=now):
            bucket = merged.setdefault(row["domain"], {"observations": 0, "verified": 0,
                                                       "rejected": 0, "refs": [], "levels": [],
                                                       "reliability": [], "factors": [],
                                                       "run_ids": [], "task_types": []})
            bucket["observations"] += int(row.get("observations") or 0)
            bucket["verified"] += int(row.get("verified") or 0)
            bucket["rejected"] += int(row.get("rejected") or 0)
            bucket["refs"].extend(row.get("evidence_refs") or [])
            if row.get("authority_level") not in (None, ""):
                bucket["levels"].append(row.get("authority_level"))
            bucket["reliability"].append(_as_float(row.get("reliability")))
            bucket["factors"].append(_mapping(row.get("factors")))
            bucket["run_ids"].extend(row.get("run_ids") or [])
            bucket["task_types"].append(str(row.get("task_type") or ""))
    gate = experience_min_samples() if min_samples is None else max(1, int(min_samples))
    rows: list = []
    for domain, bucket in sorted(merged.items()):
        total = max(1, bucket["observations"])
        factors = {
            key: _round(_clamp(sum(_as_float(item.get(key)) for item in bucket["factors"])
                               / max(1, len(bucket["factors"]))))
            for key in SOURCE_RELIABILITY_FACTORS
        }
        # 跨轮聚合时"量"与"实际核验率"用**总计数**重算（比多轮均值更硬）
        factors["verified_yield"] = _round(bucket["verified"] / total)
        factors["rejection_rate"] = _round(bucket["rejected"] / total)
        factors["volume"] = _round(_clamp(bucket["observations"] / 3.0))
        reliability = _round(_clamp(0.25 * factors["authority"] + 0.30 * factors["verified_yield"]
                                    + 0.15 * (1.0 - factors["rejection_rate"])
                                    + 0.15 * factors["recency"] + 0.10 * factors["corroboration"]
                                    + 0.05 * factors["volume"]), 8)
        row = {
            "source_version": SOURCE_MEMORY_VERSION, "domain": domain,
            "authority_level": min(bucket["levels"]) if bucket["levels"] else None,
            "reliability": reliability, "factors": factors,
            "observations": bucket["observations"], "verified": bucket["verified"],
            "rejected": bucket["rejected"], "evidence_refs": sorted(set(bucket["refs"]))[:20],
            "update_frequency": "UNKNOWN",
            "run_ids": sorted(set(bucket["run_ids"])), "min_samples": gate,
            "task_types": sorted(set(bucket["task_types"])),
        }
        row["band"] = source_band(row, min_samples=gate)
        row["known_limitations"] = source_limitations(row, min_samples=gate)
        rows.append(row)
    return rows


def source_content(row: Mapping) -> str:
    row = row if isinstance(row, Mapping) else {}
    return canonicalize("经验·来源：域 %s（权威等级 %s）可靠性 %.2f（档位 %s），观测 %d 条、"
                        "已核验 %d 条、被反驳 %d 条；已知局限：%s。"
                        % (str(row.get("domain") or ""),
                           row.get("authority_level") if row.get("authority_level") not in (None, "")
                           else "未知", _as_float(row.get("reliability")),
                           str(row.get("band") or "UNKNOWN"),
                           int(row.get("observations") or 0), int(row.get("verified") or 0),
                           int(row.get("rejected") or 0),
                           "、".join(row.get("known_limitations") or []) or "无"))


def source_candidates(traces: Sequence[Mapping], *, scope: str = "", min_samples: int | None = None,
                      now=None) -> list:
    """来源记忆候选（§3.5 六个字段齐全；每域一条，并绑该域真实取到的证据）。"""
    gate = experience_min_samples() if min_samples is None else max(1, int(min_samples))
    traces = [trace for trace in (traces or []) if isinstance(trace, Mapping)]
    run_meta = _run_meta_of(traces)
    refs_by_domain: dict = {}
    for trace in traces:
        for item in (trace.get("sources") or []):
            if isinstance(item, Mapping) and str(item.get("domain") or ""):
                bucket = refs_by_domain.setdefault(str(item["domain"]), [])
                ref = str(item.get("evidence_ref") or "")
                if ref and ref not in bucket:
                    bucket.append(ref)
    candidates: list = []
    for row in aggregate_source_reliability(traces, min_samples=gate, now=now):
        candidates.append({
            "memory_type": "SOURCE",
            "canonical_content": source_content(row),
            "claim_type": EXPERIENCE_CLAIM_TYPES["SOURCE"],
            "confidence": _round(_as_float(row.get("reliability"))),
            "freshness_class": FRESHNESS_BY_TYPE["SOURCE"],
            "valid_from": str(run_meta.get("observed_at") or ""),
            "entity_ids": [str(row.get("domain") or "")],
            "evidence": _links_for_traces(traces, refs_by_domain.get(str(row.get("domain") or ""), [])),
            "scope": str(scope or experience_scope()),
            "owner_user_id": str(run_meta.get("owner_user_id") or ""),
            "session_id": str(run_meta.get("session_id") or ""),
            "industry_pack_id": str(run_meta.get("industry_pack_id") or ""),
            "run_id": str(run_meta.get("run_id") or ""),
            "trace_ref": str(run_meta.get("trace_fingerprint") or ""),
            "samples": int(row.get("observations") or 0),
            "sample_floor": int(gate),
            "metadata": {**{key: row.get(key) for key in
                            ("domain", "authority_level", "reliability", "band", "factors",
                             "observations", "verified", "rejected", "update_frequency",
                             "known_limitations", "evidence_refs", "run_ids", "source_version")},
                         "experience_version": EXPERIENCE_MEMORY_VERSION, "hint_only": True},
        })
    return candidates


# ── P12-05：技能表现记忆（成功率口径**直接引 Phase 11**）──────────────────

def skill_performance_rows(records: Sequence[Mapping], *, task_type: str = "",
                           min_samples: int | None = None) -> list:
    """P12-05：把 Phase 11 的遥测记录聚合成 §3.6 的行（**口径零重定义**）。

    `qa_skills.performance_table` 是成功率与分位数的唯一实现（D-042/D-043）；
    本函数只做"把它摆成记忆行的形状"，连 `success_definition` 都逐字抄过来。
    """
    from qa_skills import performance_table
    from qa_graph_contracts import SKILL_TELEMETRY_VERSION

    table = performance_table([row for row in (records or []) if isinstance(row, Mapping)],
                              min_samples=min_samples, task_type=task_type)
    rows: list = []
    for skill_id in SKILL_IDS:
        row = table.get(skill_id)
        if not isinstance(row, Mapping):
            continue
        rows.append({
            "skill_memory_version": SKILL_PERFORMANCE_MEMORY_VERSION,
            "telemetry_version": str(row.get("telemetry_version") or SKILL_TELEMETRY_VERSION),
            "skill_id": skill_id, "task_type": str(task_type or row.get("task_type") or ""),
            "attempts": int(row.get("attempts") or 0), "successes": int(row.get("successes") or 0),
            "success_rate": row.get("success_rate"),
            "latency_ms": dict(row.get("latency_ms") or {}),
            "cost_units": _round(row.get("cost_units")),
            "evidence_yield": int(row.get("evidence_yield") or 0),
            "verified_yield": int(row.get("verified_yield") or 0),
            "min_samples": int(row.get("min_samples") or 0),
            "success_definition": str(row.get("success_definition") or ""),
            "reason": str(row.get("reason") or ""),
            "source": "qa_skills.performance_table(qa_stage_runs:skill:*)",
        })
    return rows


def skill_candidates(traces: Sequence[Mapping], *, scope: str = "", now=None,
                     records: Sequence[Mapping] | None = None, min_samples: int | None = None) -> list:
    """技能表现记忆候选：记录来自 `qa_stage_runs`（或快照离线重建的同形记录）。

    样本不足（`attempts < 门槛`）的行走写门的 `BELOW_SAMPLE_FLOOR`（如实拒收并记账），
    绝不把"1 次成功"写成 100%。
    """
    rows: list = list(records or [])
    if not rows:
        for trace in traces or ():
            if isinstance(trace, Mapping):
                rows.extend([row for row in (trace.get("skills") or []) if isinstance(row, Mapping)])
    gate = experience_min_samples() if min_samples is None else max(1, int(min_samples))
    traces = [trace for trace in (traces or []) if isinstance(trace, Mapping)]
    run_meta = _run_meta_of(traces)
    refs_by_route = _refs_by_routes(traces, ())
    candidates: list = []
    for row in skill_performance_rows(rows, min_samples=None):
        rate = row.get("success_rate")
        attempts = int(row.get("attempts") or 0)
        try:
            from qa_skills import skill_route_by_id

            route = str(skill_route_by_id(str(row.get("skill_id") or "")) or "")
        except Exception:        # noqa: BLE001 —— 通道映射不可用就不绑证据（如实为空）
            route = ""
        content = canonicalize("经验·技能表现：技能 %s 尝试 %d 次、成功 %d 次（成功率 %s；口径：%s），"
                               "延迟 p50 %s ms、成本 %s、产出证据 %d 条（已核验 %d 条）。"
                               % (row.get("skill_id"), attempts, int(row.get("successes") or 0),
                                  ("%.2f" % _as_float(rate)) if rate is not None else "样本不足",
                                  row.get("success_definition"),
                                  _mapping(row.get("latency_ms")).get("p50"),
                                  row.get("cost_units"), int(row.get("evidence_yield") or 0),
                                  int(row.get("verified_yield") or 0)))
        candidates.append({
            "memory_type": "SKILL_PERFORMANCE",
            "canonical_content": content,
            "claim_type": EXPERIENCE_CLAIM_TYPES["SKILL_PERFORMANCE"],
            "confidence": _round(_as_float(rate) if rate is not None else 0.0),
            "freshness_class": FRESHNESS_BY_TYPE["SKILL_PERFORMANCE"],
            "valid_from": str(run_meta.get("observed_at") or ""),
            "entity_ids": [str(row.get("skill_id") or "")],
            "evidence": _links_for_traces(traces, refs_by_route.get(route, [])),
            "scope": str(scope or experience_scope()),
            "owner_user_id": str(run_meta.get("owner_user_id") or ""),
            "session_id": str(run_meta.get("session_id") or ""),
            "industry_pack_id": str(run_meta.get("industry_pack_id") or ""),
            "run_id": str(run_meta.get("run_id") or ""),
            "trace_ref": str(run_meta.get("trace_fingerprint") or ""),
            "samples": attempts,
            "sample_floor": int(min_samples if min_samples is not None
                                else _mapping(row).get("min_samples") or gate),
            "run_ids": list(run_meta.get("run_ids") or [])[:10],
            "metadata": {**{key: row.get(key) for key in
                            ("skill_id", "task_type", "attempts", "successes", "success_rate",
                             "latency_ms", "cost_units", "evidence_yield", "verified_yield",
                             "min_samples", "success_definition", "telemetry_version",
                             "skill_memory_version")},
                         "source": str(row.get("source") or ""),
                         "experience_version": EXPERIENCE_MEMORY_VERSION, "hint_only": True},
        })
    return candidates


# ── 证据链接（provenance：每条经验记忆都要能回到真实证据）─────────────────

def _evidence_links(trace: Mapping, *, refs: Sequence[str]) -> list:
    """把 trace 里被引用到的证据对象转成 `memory_evidence_link` 行（供写门落库）。

    证据对象取自 trace 的私有索引 `evidence_index`；**索引里没有的 ref 一律丢掉**
    （宁可少一条链接、也不拿一个只有字符串的"证据"冒充 provenance）。
    """
    trace = trace if isinstance(trace, Mapping) else {}
    evidence_index = _mapping(trace.get("evidence_index"))
    by_ref: dict = {}
    for item in (trace.get("sources") or []):
        if isinstance(item, Mapping):
            by_ref[str(item.get("evidence_ref") or "")] = item
    links: list = []
    binding = _binding_of(trace)
    for ref in refs:
        if not ref:
            continue
        item = evidence_index.get(ref) or {}
        if not item:
            continue
        row = evidence_link_row(item, {"verdict": str(by_ref.get(ref, {}).get("verdict") or "")},
                                run_id=str(_mapping(trace.get("run")).get("id") or ""),
                                stage="experience_memory",
                                route=str(by_ref.get(ref, {}).get("route") or ""),
                                corpus_version=binding["corpus_version"])
        links.append(row)
    return links


def _episode_evidence_refs(trace: Mapping, *, limit: int = 6) -> list:
    """剧集记忆绑定的证据：优先已核验支持的，其次被反驳的，最后任意采纳的。"""
    rows = [item for item in (trace.get("sources") or []) if isinstance(item, Mapping)]
    order = {"SUPPORTED": 0, "REFUTED": 1}
    ranked = sorted(rows, key=lambda item: (order.get(str(item.get("verdict") or ""), 2),
                                            str(item.get("evidence_ref") or "")))
    return [str(item.get("evidence_ref") or "") for item in ranked][:limit]


def _refs_by_routes(traces: Sequence[Mapping], routes: Iterable, *, limit: int = 6,
                    verdict_preferred: bool = True) -> dict:
    """把若干 trace 的证据按**通道**归拢成 `{route: [evidence_ref, ...]}`。

    用于给策略/查询形态/技能表现三类记忆绑证据（它们是**跨轮聚合**的，
    绑的证据必须是参与聚合的那些轮里真实取到的证据）。已核验支持的排在前面。
    """
    wanted = {str(item) for item in (routes or ()) if str(item)}
    out: dict = {}
    for trace in traces or ():
        if not isinstance(trace, Mapping):
            continue
        rows = [item for item in (trace.get("sources") or []) if isinstance(item, Mapping)]
        if verdict_preferred:
            order = {"SUPPORTED": 0, "REFUTED": 1}
            rows = sorted(rows, key=lambda item: (order.get(str(item.get("verdict") or ""), 2),
                                                  str(item.get("evidence_ref") or "")))
        for item in rows:
            route = str(item.get("route") or "")
            ref = str(item.get("evidence_ref") or "")
            if not ref or (wanted and route not in wanted):
                continue
            bucket = out.setdefault(route, [])
            if ref not in bucket and len(bucket) < limit:
                bucket.append(ref)
    return out


def _links_for_traces(traces: Sequence[Mapping], refs: Iterable, *, limit: int = 6) -> list:
    """跨 trace 收集证据链接行（同一 ref 只取一次，按 ref 排序 → 确定性）。"""
    picked = [str(ref) for ref in (refs or []) if str(ref)]
    if not picked:
        return []
    seen: list = []
    for ref in sorted(dict.fromkeys(picked)):
        if len(seen) >= limit:
            break
        seen.append(ref)
    links: list = []
    for trace in traces or ():
        if not isinstance(trace, Mapping):
            continue
        have = {str(item.get("evidence_ref") or "")
                for item in (trace.get("sources") or []) if isinstance(item, Mapping)}
        links.extend(_evidence_links(trace, refs=[ref for ref in seen if ref in have]))
    unique: dict = {}
    for row in links:
        unique.setdefault((str(row.get("evidence_ref") or ""),
                           str(row.get("source_fingerprint") or "")), row)
    return [unique[key] for key in sorted(unique)][:limit]


# ── 行为出口：经验指导（P12-02/P12-03/P12-06 真的改变下一轮）───────────────

def build_guidance(*, failures: Sequence[Mapping] = (), strategies: Sequence[Mapping] = (),
                   task_type: str = "", corpus_version: str = "",
                   retrieval_config_version: str = "", now=None,
                   max_failures: int = 20) -> dict:
    """把失败/策略记忆转成**下一轮可执行的通道偏好 + 提前提示**（纯规则）。

    · `SUPPRESS` 的失败 → `avoid_routes`（并按种类生成 warnings）；
    · 成功率 100% 的策略 → `prefer_routes`；
    · 严格顺序：先按失败剔掉，再按策略把可用的偏好顶到前面（`apply_route_guidance`）。
    每条 decision/warning 都带 `memory_id`/`failure_id` 与理由码，完全可追溯。
    """
    failures = [row for row in (failures or []) if isinstance(row, Mapping)]
    strategies = [row for row in (strategies or []) if isinstance(row, Mapping)]
    decisions: list = []
    avoid: list = []
    warnings: list = []
    skipped_by_task_type = 0
    for failure in failures[:max_failures]:
        row_task = str(failure.get("task_type") or "")
        # 经验是**在某种语境里学到的**：任务类型对不上就不拿它压制（与策略的过滤对称）——
        # 否则"另一类问题的失败"会替这一类问题做决定（跨语境误伤）。
        if task_type and row_task and row_task != task_type:
            skipped_by_task_type += 1
            continue
        decision = retry_decision(failure, corpus_version=corpus_version,
                                  retrieval_config_version=retrieval_config_version, now=now)
        decision["failure_id"] = str(failure.get("failure_id") or decision.get("failure_id") or "")
        decisions.append(decision)
        route = str(failure.get("route") or "").strip()
        kind = str(failure.get("kind") or "")
        if decision["decision"] == "SUPPRESS" and route and route in QA_RETRIEVAL_ROUTES:
            if route not in avoid:
                avoid.append(route)
            warnings.append({
                "code": "FAILURE_MEMORY_AVOID", "kind": kind, "route": route,
                "failure_id": decision["failure_id"], "reason": decision["reason"],
                "text": ("经验提示：通道 %s 在同一语料版本/检索配置下曾%s，本轮已避开它。"
                         % (route, {"FAILED_ROUTE": "零已验证产出",
                                    "INVALID_QUERY": "零采纳",
                                    "SEMANTIC_DRIFT": "被扩展词带偏",
                                    "FALSE_POSITIVE_SOURCE": "取回误导来源"}.get(kind, "失败"))),
            })
        elif decision["decision"] == "ALLOW_RETRY" and route:
            warnings.append({
                "code": "FAILURE_MEMORY_RETRY_ALLOWED", "kind": kind, "route": route,
                "failure_id": decision["failure_id"], "reason": decision["reason"],
                "text": ("经验提示：通道 %s 曾失败，但%s，因此**允许重试**（不做压制）。"
                         % (route, {"CORPUS_VERSION_CHANGED": "语料版本已变",
                                    "RETRIEVAL_CONFIG_CHANGED": "检索配置已变",
                                    "VERSION_UNBOUND": "失败记录没有版本绑定",
                                    "FAILURE_AGED_OUT": "失败已过压制时效"}.get(decision["reason"],
                                                                              "不满足压制条件"))),
            })
    prefer: list = []
    for row in strategies:
        meta = _mapping(row.get("metadata")) if row.get("metadata") is not None else dict(row)
        combination = [str(item) for item in (meta.get("combination") or [])]
        samples = int(meta.get("observations") or 0)
        floor = int(meta.get("min_samples") or experience_min_samples())
        row_task = str(meta.get("task_type") or "")
        if task_type and row_task and row_task != task_type:
            continue
        if not combination or samples < floor:
            continue
        for route in combination:
            if route in QA_RETRIEVAL_ROUTES and route not in prefer:
                prefer.append(route)
        detail = _mapping(meta.get("routes_detail"))
        warnings.append({
            "code": "STRATEGY_MEMORY_PREFER", "routes": combination,
            "memory_id": str(row.get("memory_id") or ""),
            "text": ("经验提示：任务类型 %s 下通道组合 [%s] 的有效率 %d%%（组合 %d/%d；逐通道 %s），"
                     "本轮在缺口已建议的通道之间优先走它。"
                     % (row_task or task_type or "未分类", "、".join(combination),
                        int(round(100 * _as_float(meta.get("success_rate")))),
                        int(meta.get("successes") or 0), samples,
                        "；".join("%s %d/%d" % (route,
                                                int(_mapping(detail.get(route)).get("successes") or 0),
                                                int(_mapping(detail.get(route)).get("observations") or 0))
                                  for route in combination))),
        })
    guidance = {
        "guidance_version": EXPERIENCE_GUIDANCE_VERSION,
        "hint_version": EXPERIENCE_MEMORY_VERSION,
        "task_type": str(task_type or ""),
        "corpus_version": str(corpus_version or ""),
        "retrieval_config_version": str(retrieval_config_version or ""),
        "prefer_routes": prefer, "avoid_routes": avoid,
        "retry_decisions": decisions, "warnings": warnings, "route_changes": [],
        "stats": {
            "failures_considered": len(failures),
            "failures_skipped_by_task_type": skipped_by_task_type,
            "suppressed": len([row for row in decisions if row["decision"] == "SUPPRESS"]),
            "allowed_retry": len([row for row in decisions if row["decision"] == "ALLOW_RETRY"]),
            "strategies_considered": len(strategies),
            "prefer_routes": len(prefer), "avoid_routes": len(avoid),
            "retry_reasons": _counter(row.get("reason") for row in decisions),
        },
        "hint": True, "requires_revalidation": True, "verified_evidence": False,
    }
    ok, note = validate_contract("experience_guidance", dict(guidance))
    guidance["contract_ok"] = bool(ok)
    if not ok:
        guidance["contract_error"] = note
    return guidance


def apply_route_guidance(gaps: Sequence[Mapping], guidance: Mapping,
                         *, max_changes: int = 200) -> tuple:
    """把通道偏好**真的作用到缺口的建议通道次序**上（返回新列表 + 回执）。

    只做两件事，且**绝不发明通道**：
      1) 先剔掉 `avoid_routes`：从缺口自己的 `suggested_routes` 里删掉被避免的通道
         （全被剔光时保留原次序并记 `no_alternative`）；
      2) 再把 `prefer_routes` 里**本来就在建议列表里**的通道顶到最前（按 pref 次序）。
    返回的缺口是**新字典**（不改调用方的数据），逐条记 `route_changes`。
    """
    guidance = guidance if isinstance(guidance, Mapping) else {}
    avoid = [str(item) for item in (guidance.get("avoid_routes") or [])]
    prefer = [str(item) for item in (guidance.get("prefer_routes") or [])]
    changes: list = []
    out: list = []
    for gap in gaps or ():
        if not isinstance(gap, Mapping):
            continue
        routes = [str(item) for item in (gap.get("suggested_routes") or [])]
        if not routes:
            out.append(dict(gap))
            continue
        original = list(routes)
        kept = [route for route in routes if route not in avoid]
        no_alternative = not kept
        if no_alternative:
            kept = list(routes)
        ordered = [route for route in prefer if route in kept] + \
                  [route for route in kept if route not in prefer]
        changed = ordered[0] != original[0]
        item = dict(gap)
        item["suggested_routes"] = ordered
        item["route_guidance"] = {
            "original": original, "applied": ordered, "avoided": [route for route in avoid if route in original],
            "preferred": [route for route in prefer if route in original],
            "changed": changed, "no_alternative": no_alternative,
        }
        out.append(item)
        if changed and len(changes) < max_changes:
            changes.append({
                "gap_id": str(gap.get("gap_id") or ""), "gap_type": str(gap.get("missing") or ""),
                "from": original[0], "to": ordered[0],
                "reason": ("FAILURE_AVOIDED" if original[0] in avoid else "STRATEGY_PREFERRED"),
                "original": original, "applied": ordered,
            })
    receipt = {
        "guidance_version": str(guidance.get("guidance_version") or EXPERIENCE_GUIDANCE_VERSION),
        "gaps": len(out), "changed": len(changes), "route_changes": changes,
        "avoid_routes": avoid, "prefer_routes": prefer,
        "changed_by_reason": _counter(row.get("reason") for row in changes),
        "hint": True, "requires_revalidation": True, "verified_evidence": False,
    }
    return out, receipt


def guidance_receipt(guidance: Mapping) -> dict:
    """给 stats/SSE 的指导回执（不含记忆正文，只有计数与理由码）。"""
    guidance = guidance if isinstance(guidance, Mapping) else {}
    stats = _mapping(guidance.get("stats"))
    return {
        "guidance_version": str(guidance.get("guidance_version") or EXPERIENCE_GUIDANCE_VERSION),
        "hint_version": str(guidance.get("hint_version") or EXPERIENCE_MEMORY_VERSION),
        "task_type": str(guidance.get("task_type") or ""),
        "corpus_version": str(guidance.get("corpus_version") or ""),
        "retrieval_config_version": str(guidance.get("retrieval_config_version") or ""),
        "prefer_routes": list(guidance.get("prefer_routes") or []),
        "avoid_routes": list(guidance.get("avoid_routes") or []),
        "warnings": len(guidance.get("warnings") or []),
        "warning_codes": _counter(_mapping(row).get("code")
                                  for row in (guidance.get("warnings") or [])),
        "retry_reasons": dict(stats.get("retry_reasons") or {}),
        "suppressed": int(stats.get("suppressed") or 0),
        "allowed_retry": int(stats.get("allowed_retry") or 0),
        "hint": True, "requires_revalidation": True, "verified_evidence": False,
    }


# ── 写入口（唯一写门 = qa_memory.apply_write_gate）────────────────────────

def collect_candidates(traces: Sequence[Mapping], *, scope: str = "", min_samples: int | None = None,
                       skill_records: Sequence[Mapping] | None = None,
                       summarizer: str = "", now=None,
                       include: Iterable = ("EPISODIC_RESEARCH", "FAILURE", "STRATEGY",
                                            "QUERY_PATTERN", "SOURCE", "SKILL_PERFORMANCE")) -> dict:
    """P12-01…P12-05 一次归纳出六类候选（**只产候选，不写库**）。"""
    traces = [trace for trace in (traces or []) if isinstance(trace, Mapping)]
    scope = str(scope or experience_scope())
    wanted = {str(name) for name in (include or [])}
    gate = experience_min_samples() if min_samples is None else max(1, int(min_samples))
    produced: dict = {}
    candidates: list = []
    episodes: list = []
    if "EPISODIC_RESEARCH" in wanted:
        for trace in traces:
            episode = episode_from_trace(trace, summarizer=summarizer, now=now)
            episodes.append(episode)
            run = _mapping(trace.get("run"))
            refs = _episode_evidence_refs(trace)
            candidates.append({
                "memory_type": "EPISODIC_RESEARCH",
                "canonical_content": episode["summary"],
                "claim_type": EXPERIENCE_CLAIM_TYPES["EPISODIC_RESEARCH"],
                "confidence": episode_confidence(episode),
                "freshness_class": FRESHNESS_BY_TYPE["EPISODIC_RESEARCH"],
                "valid_from": str(episode.get("captured_at") or ""),
                "entity_ids": [],
                "evidence": _evidence_links(trace, refs=refs),
                "scope": scope,
                "owner_user_id": str(run.get("owner_user_id") or ""),
                "session_id": str(run.get("session_id") or ""),
                "industry_pack_id": str(run.get("industry_pack_id") or ""),
                "run_id": str(run.get("id") or ""),
                "trace_ref": str(trace.get("trace_fingerprint") or ""),
                "samples": int(_mapping(episode.get("counts")).get("evidence") or 0),
                "sample_floor": 0,
                "relations": [{"relation": "HELPED_RESOLVE", "target_kind": "route",
                               "target_ref": str(route), "weight": 0.9}
                              for route in (episode.get("effective_routes") or [])[:5]],
                "metadata": {
                    "task_type": str(episode.get("task_type") or ""),
                    "sections": dict(episode.get("sections") or {}),
                    "effective_routes": list(episode.get("effective_routes") or []),
                    "key_gaps": list(episode.get("key_gaps") or []),
                    "stop_reason": str(episode.get("stop_reason") or ""),
                    "summarizer": str(episode.get("summarizer") or ""),
                    "trace_fingerprint": str(episode.get("trace_fingerprint") or ""),
                    "missing_sections": list(episode.get("missing_sections") or []),
                    "counts": dict(episode.get("counts") or {}),
                    "route_yields": dict(episode.get("route_yields") or {}),
                    "episode_version": EPISODE_MEMORY_VERSION,
                    "experience_version": EXPERIENCE_MEMORY_VERSION,
                    "hint_only": True,
                },
            })
        produced["EPISODIC_RESEARCH"] = len(episodes)
    if "FAILURE" in wanted:
        rows = [candidate for trace in traces for candidate in failure_candidates(trace, scope=scope, now=now)]
        produced["FAILURE"] = len(rows)
        candidates.extend(rows)
    if "STRATEGY" in wanted:
        rows = strategy_candidates(traces, scope=scope, min_samples=gate, now=now)
        produced["STRATEGY"] = len(rows)
        candidates.extend(rows)
    if "QUERY_PATTERN" in wanted:
        rows = query_pattern_candidates(traces, scope=scope, min_samples=gate, now=now)
        produced["QUERY_PATTERN"] = len(rows)
        candidates.extend(rows)
    if "SOURCE" in wanted:
        rows = source_candidates(traces, scope=scope, min_samples=gate, now=now)
        produced["SOURCE"] = len(rows)
        candidates.extend(rows)
    if "SKILL_PERFORMANCE" in wanted:
        rows = skill_candidates(traces, scope=scope, now=now, records=skill_records)
        produced["SKILL_PERFORMANCE"] = len(rows)
        candidates.extend(rows)
    return {"candidates": candidates, "produced": produced, "episodes": episodes,
            "scope": scope, "min_samples": int(gate),
            "experience_version": EXPERIENCE_MEMORY_VERSION,
            "summarizer": str(summarizer or summarizer_name())}


def write_experience_memories(store, *, traces: Sequence[Mapping], run_meta: Mapping | None = None,
                              scope: str = "", min_samples: int | None = None,
                              skill_records: Sequence[Mapping] | None = None,
                              summarizer: str = "", now=None, audit=None,
                              include: Iterable = ("EPISODIC_RESEARCH", "FAILURE", "STRATEGY",
                                                   "QUERY_PATTERN", "SOURCE", "SKILL_PERFORMANCE")) -> dict:
    """P12 总入口：归纳 → **唯一写门** → 两张投影表（`skill_performance_memory` /
    `source_reliability_memory`）。写库失败只记账，绝不冒泡（§9 的主链路在它之前已经完成）。
    """
    traces = [trace for trace in (traces or []) if isinstance(trace, Mapping)]
    run_meta = run_meta if isinstance(run_meta, Mapping) else _run_meta_of(traces)
    collected = collect_candidates(traces, scope=scope, min_samples=min_samples,
                                   skill_records=skill_records, summarizer=summarizer,
                                   now=now, include=include)
    candidates = collected["candidates"]
    receipt: dict = {
        "experience_version": EXPERIENCE_MEMORY_VERSION,
        "write_version": EXPERIENCE_WRITE_VERSION,
        "run_id": str(run_meta.get("run_id") or run_meta.get("id") or ""),
        "traces": len(traces), "produced": dict(collected["produced"]),
        "candidates": len(candidates),
        "missing_sections": _counter(section for trace in traces
                                     for section in (trace.get("missing_sections") or [])),
        "summarizer": str(collected["summarizer"]),
        "min_samples": int(collected["min_samples"]),
        "scope": str(collected["scope"]),
        "gate": {}, "persisted_by_type": {}, "projections": {"source": 0, "skill": 0},
        "errors": [],
    }
    if not candidates:
        ok, note = validate_contract("experience_memory", {
            "experience_version": EXPERIENCE_MEMORY_VERSION, "run_id": receipt["run_id"],
            "produced": receipt["produced"], "decisions": []})
        receipt["contract_ok"] = bool(ok)
        if not ok:
            receipt["contract_error"] = note
        return receipt
    from qa_memory import apply_write_gate

    gate_receipt = apply_write_gate(
        store, candidates, run_id=receipt["run_id"],
        trace_id=str(run_meta.get("id") or run_meta.get("run_id") or ""), now=now, audit=audit)
    receipt["gate"] = {key: gate_receipt.get(key) for key in
                       ("candidates", "persisted", "merged", "session_only", "dropped",
                        "decision_counts", "reason_counts", "distribution", "min_utility")}
    by_type: dict = {}
    memory_ids: dict = {}
    for row in gate_receipt.get("decisions") or []:
        if store is None:
            break            # 没有记忆库时决策照样算，但**不算落库**（不许虚报 persisted）
        key = str(_mapping(row).get("memory_type") or "")
        if str(_mapping(row).get("decision") or "") in ("PERSIST", "PERSIST_WITH_TTL"):
            by_type[key] = by_type.get(key, 0) + 1
            if _mapping(row).get("memory_id"):
                memory_ids[key] = str(row["memory_id"])
    receipt["persisted_by_type"] = dict(sorted(by_type.items()))
    receipt["memory_ids"] = memory_ids
    receipt["decisions"] = list(gate_receipt.get("decisions") or [])
    if store is not None and candidates:
        receipt["projections"] = _write_projections(store, traces=traces, candidates=candidates,
                                                    memory_ids=memory_ids, scope=collected["scope"],
                                                    min_samples=collected["min_samples"],
                                                    skill_records=skill_records, now=now)
    ok, note = validate_contract("experience_memory", {
        "experience_version": EXPERIENCE_MEMORY_VERSION, "run_id": receipt["run_id"],
        "produced": receipt["produced"], "decisions": receipt["decisions"]})
    receipt["contract_ok"] = bool(ok)
    if not ok:
        receipt["contract_error"] = note
    return receipt


def _write_projections(store, *, traces: Sequence[Mapping], candidates: Sequence[Mapping],
                       memory_ids: Mapping, scope: str, min_samples: int,
                       skill_records: Sequence[Mapping] | None = None, now=None) -> dict:
    """把来源/技能两类**同时**写进各自的投影表（失败只记账，不影响记忆本体）。

    `memory_id` 用 `qa_memory.memory_id_for` 按（作用域 + 类型 + 正文）**重算**：
    与写门落库的 id 同一口径（内容寻址），所以投影行与记忆行永远对得上，
    不依赖决策回执的顺序（顺序一变就对错的写法在这里被排除）。
    """
    from qa_memory import memory_id_for

    out = {"source": 0, "skill": 0, "errors": []}
    persisted = {str(key) for key in (memory_ids or {})}
    meta = _run_meta_of(traces)
    key = _scope_key(scope, traces)

    def memory_id(content: str, memory_type: str) -> str:
        return memory_id_for(scope=scope, memory_type=memory_type,
                             canonical_content=canonicalize(content),
                             owner_user_id=str(meta.get("owner_user_id") or ""),
                             session_id=str(meta.get("session_id") or ""),
                             industry_pack_id=str(meta.get("industry_pack_id") or ""))

    try:
        source_rows = []
        if "SOURCE" in persisted:
            for row in aggregate_source_reliability(traces, min_samples=min_samples, now=now):
                source_rows.append({
                    **row, "scope_key": key,
                    "industry_pack_id": str(_mapping(_mapping(traces[-1]).get("run"))
                                            .get("industry_pack_id") or ""),
                    "memory_id": memory_id(source_content(row), "SOURCE"),
                    "run_id": str(_mapping(_mapping(traces[-1]).get("run")).get("id") or ""),
                })
            out["source"] = store.save_source_reliability(source_rows)
        skill_rows = []
        if "SKILL_PERFORMANCE" in persisted:
            rows = list(skill_records or [])
            if not rows:
                for trace in traces:
                    rows.extend([row for row in (trace.get("skills") or [])
                                 if isinstance(row, Mapping)])
            run_ids = [str(_mapping(trace.get("run")).get("id") or "") for trace in traces]
            for row in skill_candidates(traces, scope=scope, now=now, records=rows):
                content = str(row.get("canonical_content") or "")
                skill_rows.append({
                    **{k: _mapping(row.get("metadata")).get(k) for k in
                       ("skill_id", "task_type", "attempts", "successes", "success_rate",
                        "latency_ms", "cost_units", "evidence_yield", "verified_yield",
                        "min_samples", "success_definition", "telemetry_version")},
                    "skill_memory_version": SKILL_PERFORMANCE_MEMORY_VERSION,
                    "source": str(_mapping(row.get("metadata")).get("source") or ""),
                    "scope_key": key, "run_ids": [value for value in run_ids if value][:20],
                    "memory_id": memory_id(content, "SKILL_PERFORMANCE"),
                })
            out["skill"] = store.save_skill_performance(skill_rows)
    except Exception as exc:      # noqa: BLE001 —— 投影失败只记账
        out["errors"].append("%s: %s" % (type(exc).__name__, str(exc)[:160]))
    return out


def _scope_key(scope: str, traces: Sequence[Mapping]) -> str:
    """投影表的作用域键：与 `qa_memory.scope_key` **同一口径**（不另造一套键）。"""
    from qa_memory import scope_key

    meta = _run_meta_of(traces)
    return scope_key(scope, owner_user_id=str(meta.get("owner_user_id") or ""),
                     session_id=str(meta.get("session_id") or ""),
                     industry_pack_id=str(meta.get("industry_pack_id") or ""))


def experience_receipt(receipt: Mapping) -> dict:
    """给 stats/SSE 的经验写入回执（不含记忆正文）。"""
    receipt = receipt if isinstance(receipt, Mapping) else {}
    gate = _mapping(receipt.get("gate"))
    return {
        "experience_version": str(receipt.get("experience_version") or EXPERIENCE_MEMORY_VERSION),
        "traces": int(receipt.get("traces") or 0),
        "produced": dict(receipt.get("produced") or {}),
        "candidates": int(receipt.get("candidates") or 0),
        "persisted": int(gate.get("persisted") or 0),
        "dropped": int(gate.get("dropped") or 0),
        "session_only": int(gate.get("session_only") or 0),
        "merged": int(gate.get("merged") or 0),
        "persisted_by_type": dict(receipt.get("persisted_by_type") or {}),
        "reason_counts": dict(gate.get("reason_counts") or {}),
        "projections": dict(receipt.get("projections") or {}),
        "summarizer": str(receipt.get("summarizer") or ""),
        "missing_sections": dict(receipt.get("missing_sections") or {}),
        "error": "; ".join(receipt.get("errors") or []) if receipt.get("errors") else "",
    }


# ── 规划期召回（§10：Research Planner 之前的 planning recall）─────────────

def planning_hint(store, *, run_meta: Mapping, task_type: str = "", question: str = "",
                  corpus_version: str = "", retrieval_config_version: str = "",
                  now=None, limit: int | None = None, terms: Iterable | None = None,
                  extra_terms: Iterable = (), min_score: float | None = None) -> dict:
    """在规划之前召回经验类记忆并给出指导（**只读**；每次命中恒为 MEMORY_HINT）。

    走的还是 Phase 09 的 `qa_memory.recall(mode="planning")`（四通道 + 提示语义），
    本函数只多做三件事：
      · 查询实词 = 问题实词 ∪ `extra_terms`（规划期该带上任务类型/计划实体 ——
        "同类缺口/同类问题"的经验才会被看见，§10 的 planning recall 才有意义）；
      · 按 memory_id 回读元数据（召回命中本身只带计数，不含正文/元数据）；
      · 把失败/策略转成可执行的指导（`build_guidance`）。
    """
    from qa_memory import recall

    run_meta = run_meta if isinstance(run_meta, Mapping) else {}
    query_terms = set(terms or ()) or term_set(str(question or ""), sizes=(2, 3))
    for value in (extra_terms or ()):
        query_terms |= term_set(str(value or ""), sizes=(2, 3))
    receipt = recall(store, mode="planning",
                     query=str(question or ""), terms=query_terms,
                     owner_user_id=str(run_meta.get("owner_user_id") or ""),
                     session_id=str(run_meta.get("session_id") or ""),
                     industry_pack_id=str(run_meta.get("industry_pack_id") or ""),
                     run_id=str(run_meta.get("id") or ""),
                     trace_id=str(run_meta.get("id") or ""), limit=limit,
                     min_score=experience_recall_min_score() if min_score is None else float(min_score),
                     now=now, memory_types=EXPERIENCE_RECALL_TYPES)
    failures: list = []
    strategies: list = []
    # 召回命中本身**只带计数与分数**（不含记忆正文/元数据，见 MEMORY_RECALL_HIT_SCHEMA），
    # 所以这里按 memory_id 回读一次行拿 metadata —— 仍然是只读，且只读命中的那几条。
    meta_by_id: dict = {}
    hit_ids = [str(_mapping(hit).get("memory_id") or "") for hit in (receipt.get("hits") or [])]
    hit_ids = [value for value in hit_ids if value]
    if store is not None and hit_ids:
        try:
            for row in store.load_memory_items(memory_ids=hit_ids, include_all_scopes=True,
                                              limit=max(1, len(hit_ids))) or []:
                meta_by_id[str(_mapping(row).get("memory_id") or "")] = _mapping(row)
        except Exception:        # noqa: BLE001 —— 读不到元数据就退化成"只有分数"
            meta_by_id = {}
    for hit in receipt.get("hits") or []:
        if not isinstance(hit, Mapping):
            continue
        memory_id = str(hit.get("memory_id") or "")
        item = meta_by_id.get(memory_id) or {}
        meta = _mapping(item.get("metadata"))
        row = {
            "failure_id": memory_id,
            "memory_id": memory_id,
            "kind": str(meta.get("kind") or ""),
            "route": str(meta.get("route") or ""), "domain": str(meta.get("domain") or ""),
            "corpus_version": str(meta.get("corpus_version") or ""),
            "retrieval_config_version": str(meta.get("retrieval_config_version") or ""),
            "observed_at": str(meta.get("observed_at") or ""),
            "task_type": str(meta.get("task_type") or ""),
            "score": _round(hit.get("score")),
            "hint": True, "requires_revalidation": True, "verified_evidence": False,
        }
        if str(hit.get("memory_type") or "") == "FAILURE":
            failures.append(row)
        elif str(hit.get("memory_type") or "") in ("STRATEGY", "QUERY_PATTERN"):
            strategies.append({"memory_id": memory_id, "metadata": meta, "score": row["score"]})
    guidance = build_guidance(failures=failures, strategies=strategies, task_type=task_type,
                              corpus_version=corpus_version,
                              retrieval_config_version=retrieval_config_version, now=now)
    return {"recall": receipt, "guidance": guidance, "failures": failures,
            "strategies": strategies, "hits": len(receipt.get("hits") or []),
            "min_score": float(experience_recall_min_score() if min_score is None
                               else min_score),
            "terms": sorted(query_terms)[:40],
            "hint": True, "requires_revalidation": True, "verified_evidence": False}


__all__ = [
    "DEFAULT_EXPERIENCE_MIN_SAMPLES", "DEFAULT_FAILURE_MAX_AGE_DAYS",
    "EXPERIENCE_CLAIM_TYPES", "EXPERIENCE_TRACE_VERSION",
    "EXPERIENCE_WRITE_VERSION", "FRESHNESS_BY_TYPE",
    "aggregate_source_reliability", "apply_route_guidance", "authority_score", "build_guidance",
    "build_trace", "collect_candidates", "corpus_binding", "detect_failures", "domain_of",
    "episode_confidence", "episode_from_trace", "episode_summary_rule", "experience_memory_enabled",
    "experience_min_samples", "experience_receipt", "experience_scope", "failure_candidates",
    "failure_confidence", "failure_content", "failure_id", "failure_max_age_days",
    "guidance_receipt", "normalize_route", "planning_hint", "query_fingerprint",
    "query_pattern_candidates", "query_pattern_of", "register_episode_summarizer",
    "registered_summarizers", "retrieval_config_token", "retry_decision", "route_of_evidence",
    "skill_candidates", "skill_performance_rows", "source_band", "source_candidates",
    "source_content", "source_limitations", "source_reliability_rows", "strategy_candidates",
    "strategy_content", "strategy_rows", "summarizer_name", "task_type_of", "trace_fingerprint",
    "write_experience_memories",
]

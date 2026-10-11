#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 14 · P14-07 · 重复提问识别与思路调整确认（**纯规则，零模型调用**）。

需求（用户 2026-10-11 明确提出，见 DECISION_LOG D-040 与 PHASE_14 规格）：
  「多轮对话中，用户对同一问题或相似问题问了两次以上，说明对上次答案不满意，
    系统应当有交互，询问是否更改/调整回答问题的思路。」

本模块把这句话落成**可复算的判定 + 可执行的配置**，硬边界如下：
  1. **触发判据纯规则**：同一会话（`scope_key` 含 session）内，与最近 `window` 轮问题相比，
     "归一化词面重叠 ≥ 阈值" **或** "同一主体 + 同一命题意图" 即判重复。
     · 词面口径复用 Phase 04/07 的"什么算实词"（`qa_gap_analyzer.term_set`）；
     · 主体 Jaccard 门槛**直接引 Phase 10 的实体一致性口径**（`contradiction_jaccard()`）；
     · 命题意图**直接引 Phase 05 的 `qa_query_interpreter.interpret_query()`**（§6 的九值意图）。
  2. **"不满意"必须可解释**：四类六条依据（上次无新增已核验证据 / 上次被标注降档 /
     上次停止原因属 NO_GAIN·BUDGET_EXHAUSTED·MAX_DEPTH / 上次缺口未解决率超阈值），
     逐条落进回执 `evidence`；**一条都拿不到就不许交互**（记 `NO_DISSATISFACTION_EVIDENCE`）。
  3. **一次、给选项、可拒绝**：同一会话同一议题最多交互 `max_interactions`（默认 1）次；
     选项表**必须**包含"换通道 / 扩窗 / 收窗 / 提高权威要求 / 放宽门槛 / 收紧门槛 /
     换分析口径 / 保持原思路"，每项都写清**预期影响与代价**。
  4. **选择落成会话级可执行配置**：`route_preference / time_window / authority_floor /
     relevance_floor / analysis_lens` 五个键（`REALIGNMENT_CONFIG_KEYS`）由
     `qa_acquisition.apply_session_config()` 作用到下一轮的检索计划上 ——
     真的会改变下一跳通道、时间硬过滤窗口与缺口判据（有用例给前后对比数字）。
  5. **失败不阻断**：判定/交互构造抛错只记账（回执带 `realignment_error`），照常出答案；
     单轮调用（无会话上下文）自动跳过并如实标注 `NO_SESSION_CONTEXT`。
  6. **零模型**：文案是规则模板；规格提到的"LLM 生成澄清问题"做成可插拔注入点
     （`register_clarifier()`），**默认不注册、不调用**。
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Mapping, Sequence

from qa_gap_analyzer import term_set
from qa_graph_contracts import (
    ANALYSIS_LENSES,
    QA_RETRIEVAL_ROUTES,
    REALIGNMENT_CONFIG_KEYS,
    REALIGNMENT_DEFAULT_OVERLAP_THRESHOLD,
    REALIGNMENT_DEFAULT_TTL_HOURS,
    REALIGNMENT_DEFAULT_UNRESOLVED_RATE,
    REALIGNMENT_DEFAULT_WINDOW_TURNS,
    REALIGNMENT_DETECTOR_VERSION,
    REALIGNMENT_DISSATISFACTION_REASONS,
    REALIGNMENT_MAX_INTERACTIONS,
    REALIGNMENT_OPTION_IDS,
    REALIGNMENT_OPTION_VERSION,
    REALIGNMENT_SKIP_REASONS,
    REALIGNMENT_TRIGGERS,
    REALIGNMENT_VERSION,
    REPEAT_QUESTION_SETTINGS_ENV,
    SKILL_INTERACTION_SLOT,
    SKILL_INTERACTION_SLOT_OWNER,
    validate as validate_contract,
)
from qa_memory import scope_key as memory_scope_key
from qa_memory_revalidation import contradiction_jaccard
from qa_query_interpreter import interpret_query

# ── 可配置旋钮（全部可回滚；默认值写在契约里便于复算）───────────────────────
DEFAULT_QUESTION_HISTORY_LIMIT = 12
"""回看历史时的取数上限（`window` 之外还要多取一点才能按时间正确切窗）。"""

CLARIFIERS: dict = {}
"""可插拔"澄清问题生成器"注册表（**默认空 = 只用模板，绝不调用模型**）。

规格允许将来用 LLM 把澄清问题写得更自然；本机硬约束禁止调用任何模型端点，
所以这里只提供注入点，且"未注册 / 抛错 / 非法载荷"三条失败路径一律**回落模板**并记账。
"""

BASE_QUESTION = "同一个问题你问了不止一次：上一次的回答没能让你满意。要不要换个思路？"
"""交互主问题（规则模板）：措辞只说事实（"问了不止一次"），**不替用户断言情绪**。"""

_REASON_TEXT = {
    "NO_NEW_VERIFIED_EVIDENCE": "上一次回答没有新增任何**已核验**证据（Phase 07 的 new_verified_claims=0）",
    "ANSWER_DEGRADED": "上一次回答被标注为降档或证据不足（Phase 08/13 的 degradation_reasons）",
    "STOP_REASON_NO_GAIN": "上一次检索的停止原因是 NO_GAIN（连续轮次没有增益）",
    "STOP_REASON_BUDGET_EXHAUSTED": "上一次检索的停止原因是 BUDGET_EXHAUSTED（预算用尽，没搜完）",
    "STOP_REASON_MAX_DEPTH": "上一次检索的停止原因是 MAX_DEPTH（跳数到顶，仍有高优缺口）",
    "UNRESOLVED_GAP_RATE_HIGH": "上一次的缺口未解决率高于阈值",
}

_STOP_REASON_TO_EVIDENCE = {
    "NO_GAIN": "STOP_REASON_NO_GAIN",
    "BUDGET_EXHAUSTED": "STOP_REASON_BUDGET_EXHAUSTED",
    "MAX_DEPTH": "STOP_REASON_MAX_DEPTH",
}
"""Phase 07 的停止原因 → 不满意依据码（只认这三个：ANSWERABLE / UNRESOLVABLE_CONTRADICTION 不算）。"""

LENS_CATEGORY = {"event": "causal", "trend": "comparison", "risk": "multi_hop"}
"""分析口径 → **既有**的问题类别键（`qa_gap_analyzer.category_of` 认的那几个，不新造类别）。

语义：`event` = 事件/因果链（按因果桥缺口组织）；`trend` = 趋势/对比（按对比与反证组织）；
`risk` = 风险（走多跳，强制找反证与矛盾）。映射改变的是**缺口判据**（哪类缺口会被期待），
这就是"换分析口径"在本仓库的真实含义。"""

ROUTE_ORDER_FOR_CHANGE = (
    "semantic", "graph", "graph_attribute", "policy_exact", "web", "page_context", "keyword",
)
"""选"换检索通道"时的通道次序：把语义/图谱顶到前面、关键词兜到最后（**真的换一条路**）。"""

DEFAULT_TIME_WINDOW_DAYS = 90
WIDENED_TIME_WINDOW_DAYS = 365 * 3
"""收窗/扩窗的天数（扩窗会引入更旧证据 —— 代价写进选项表）。"""

DEFAULT_AUTHORITY_RAISE = 90
"""提高权威要求时的权威下界（对齐 `qa_reasoning.authority_score` 的"官方解读"档）。"""

LOOSENED_RELEVANCE_FLOOR = 0.20
TIGHTENED_RELEVANCE_FLOOR = 0.55
"""放宽/收紧证据门槛时的相关性下界（默认门槛见 `qa_gap_analyzer.DEFAULT_RELEVANCE_FLOOR=0.34`）。"""

OPTION_CATALOG: dict = {
    "change_route": {
        "label": "换检索通道（先语义/图谱，关键词兜最后）",
        "config_keys": ("route_preference",),
        "config": {"route_preference": list(ROUTE_ORDER_FOR_CHANGE)},
        "expected_effect": "上次主要靠关键词命中的证据会被语义/图谱通道重排，可能找回词面不同但相关的材料",
        "cost": "语义通道依赖库内已有向量，命中率可能下降；图谱只覆盖已建边的关系",
        "risk": "换通道后引用可能整批变化，需要重新核对引用",
    },
    "widen_time_window": {
        "label": "扩大时间窗（近三年）",
        "config_keys": ("time_window",),
        "config": {"time_window": {"days": WIDENED_TIME_WINDOW_DAYS, "label": "近三年"}},
        "expected_effect": "把更早的材料纳入硬过滤窗口，历史口径与旧版本文件会重新出现",
        "cost": "会引入更旧的证据，时效性下降；旧口径可能已失效",
        "risk": "窗口过大时噪声上升，答案里的时间口径要重新标注",
    },
    "narrow_time_window": {
        "label": "收窄时间窗（近三个月）",
        "config_keys": ("time_window",),
        "config": {"time_window": {"days": DEFAULT_TIME_WINDOW_DAYS, "label": "近三个月"}},
        "expected_effect": "只保留近期材料，结果更贴近当前状态",
        "cost": "可能丢掉仍在生效的旧文件；窗口内证据不足时会触发扩窗梯子",
        "risk": "样本变少，结论可能从'充分'降为'部分'",
    },
    "raise_authority": {
        "label": "提高来源权威要求（只认官方原文/官方解读）",
        "config_keys": ("authority_floor",),
        "config": {"authority_floor": DEFAULT_AUTHORITY_RAISE},
        "expected_effect": "低权威来源会被判成缺口（LOW_AUTHORITY），推动去找官方口径",
        "cost": "可选证据变少，回答可能变慢、变短",
        "risk": "只有二手转述的议题会直接变成'证据不足'",
    },
    "loosen_evidence_threshold": {
        "label": "放宽证据门槛（相关性下界 0.20）",
        "config_keys": ("relevance_floor",),
        "config": {"relevance_floor": LOOSENED_RELEVANCE_FLOOR},
        "expected_effect": "边角相关的材料也算进证据池，减少'无证据'判定",
        "cost": "噪声上升，答案里可能夹带弱相关材料",
        "risk": "弱证据被当成依据的风险上升，引用需逐条核对",
    },
    "tighten_evidence_threshold": {
        "label": "收紧证据门槛（相关性下界 0.55）",
        "config_keys": ("relevance_floor",),
        "config": {"relevance_floor": TIGHTENED_RELEVANCE_FLOOR},
        "expected_effect": "只保留强相关材料，结论更干净",
        "cost": "证据条数明显减少，可能直接降档为证据不足",
        "risk": "本该引用的边角证据被过滤掉",
    },
    "change_analysis_lens": {
        "label": "换分析口径（按事件 → 按趋势）",
        "config_keys": ("analysis_lens",),
        "config": {"analysis_lens": "trend"},
        "expected_effect": "缺口判据从'因果桥'改为'趋势与反证'，检索会去找对比与佐证材料",
        "cost": "与上一版答案的结构不可直接比较，需要重新读一遍",
        "risk": "趋势口径对样本量敏感，材料少时结论不稳",
    },
    "keep_strategy": {
        "label": "保持原思路（不用改）",
        "config_keys": (),
        "config": {},
        "expected_effect": "沿用原思路继续作答，本轮不做任何调整",
        "cost": "若上次确实没拿到新证据，结果可能仍然相同",
        "risk": "无（这是明确决定的默认方向）",
    },
}
"""P14-07 的选项表（**逐项写清预期影响与代价**；`keep_strategy` 是"可拒绝"的落地）。

`config` 是"选了它就落成的会话级可执行配置"（键必须是 `REALIGNMENT_CONFIG_KEYS` 的子集）。"""


# ── 旋钮 ───────────────────────────────────────────────────────────────────
def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().casefold() not in {"0", "false", "no", "off", ""}


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(float(os.getenv(name, "") or default))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def realignment_enabled() -> bool:
    """P14-07 总开关（`QA_REPEAT_QUESTION_ENABLED`，**默认关**）。

    关掉时 `plan` 阶段一个键都不新增（逐字回到 Phase 13 的行为）—— 与 Phase 06/07/08/12/13
    同一处置口径。
    """
    return _env_flag("QA_REPEAT_QUESTION_ENABLED", False)


def overlap_threshold() -> float:
    return _env_float("QA_REPEAT_QUESTION_OVERLAP", REALIGNMENT_DEFAULT_OVERLAP_THRESHOLD, 0.0, 1.0)


def window_turns() -> int:
    return _env_int("QA_REPEAT_QUESTION_WINDOW", REALIGNMENT_DEFAULT_WINDOW_TURNS, 1, 50)


def unresolved_rate_threshold() -> float:
    return _env_float("QA_REPEAT_QUESTION_UNRESOLVED_RATE",
                      REALIGNMENT_DEFAULT_UNRESOLVED_RATE, 0.0, 1.0)


def ttl_hours() -> float:
    return _env_float("QA_REPEAT_QUESTION_TTL_HOURS", float(REALIGNMENT_DEFAULT_TTL_HOURS), 0.0, 24 * 365)


def max_interactions() -> int:
    return _env_int("QA_REPEAT_QUESTION_MAX_INTERACTIONS", REALIGNMENT_MAX_INTERACTIONS, 1, 10)


def clarifier_name() -> str:
    """注册的澄清生成器名（`QA_REPEAT_QUESTION_LLM_INTERPRETER`，默认空 = 用模板）。"""
    return str(os.getenv("QA_REPEAT_QUESTION_LLM_INTERPRETER") or "").strip()


def realignment_settings() -> dict:
    """当前**实际生效**的全部旋钮（写进回执 → 判定可复算）。"""
    return {
        "realignment_version": REALIGNMENT_VERSION,
        "detector_version": REALIGNMENT_DETECTOR_VERSION,
        "option_version": REALIGNMENT_OPTION_VERSION,
        "overlap_threshold": overlap_threshold(),
        "window_turns": window_turns(),
        "unresolved_rate_threshold": unresolved_rate_threshold(),
        "ttl_hours": ttl_hours(),
        "max_interactions": max_interactions(),
        "clarifier": clarifier_name(),
        "subject_jaccard_floor": contradiction_jaccard(),
        "env": list(REPEAT_QUESTION_SETTINGS_ENV),
    }


def register_clarifier(name: str, fn: Callable) -> None:
    """注册"澄清问题生成器"（**可插拔注入点，默认不注册**；未注册/抛错/非法载荷回落模板）。"""
    if not str(name or "").strip():
        raise ValueError("澄清生成器必须有名字")
    CLARIFIERS[str(name)] = fn


def clarifiers() -> tuple:
    return tuple(sorted(CLARIFIERS))


# ── 归一化与相似度（复用既有口径）──────────────────────────────────────────
def _digest(value, length: int = 20) -> str:
    body = value if isinstance(value, str) else repr(value)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:length]


def _mapping(value) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def normalize_question(text) -> str:
    """归一化（**复用 Phase 04 的 `normalize_text`**；拿不到就退回本地小写去噪）。"""
    raw = str(text or "")
    if not raw.strip():
        return ""
    try:
        from qa_query_normalize import normalize_text

        cleaned = str(normalize_text(raw) or "")
        if cleaned.strip():
            return cleaned.strip()
    except Exception:  # noqa: BLE001 —— 归一化坏掉退回本地规则，绝不让判定抛错
        pass
    return re.sub(r"[\s，。！？；：、,.!?:;]+", "", raw.casefold())


def question_terms(text) -> set:
    """问题的实词集合（**复用 Phase 04/07 的 `term_set` 口径**，不另造分词）。"""
    return {str(item) for item in term_set(str(text or "")) if str(item).strip()}


def lexical_overlap(left, right) -> float:
    """归一化实词的 Jaccard（0…1，可手算复算）。"""
    first, second = question_terms(left), question_terms(right)
    if not first or not second:
        return 0.0
    return round(len(first & second) / float(len(first | second)), 6)


def subject_intent(question, *, plan: Mapping | None = None) -> dict:
    """一条问题的"主体 + 命题意图"（**复用 Phase 05/07/10 的三处既有口径**）。

      · 意图：`qa_query_interpreter.interpret_query()`（§6 的九值意图，Phase 05 单一真源）；
      · 主体：计划里的实体的实词集合（没有计划就用问题自身的实词集合，Phase 04/07 口径）；
      · 主体一致性门槛：Phase 10 的实体 Jaccard（`contradiction_jaccard()`，默认 0.5）。
    """
    try:
        interpretation = interpret_query(str(question or ""))
    except Exception:  # noqa: BLE001 —— 解释器坏掉退回"无意图"（宁可判不出重复，也不误报）
        interpretation = {}
    intent = str(interpretation.get("intent") or "")
    subjects = {str(item) for item in (interpretation.get("entities") or []) if str(item).strip()}
    if plan:
        subjects |= {str(item) for item in (plan.get("entities") or []) if str(item).strip()}
    if not subjects:
        subjects = question_terms(question)
    return {
        "intent": intent,
        "subjects": sorted(subjects),
        "category": str(_mapping(interpretation.get("category")).get("key")
                        or interpretation.get("category") or ""),
        "interpreter_version": str(interpretation.get("interpreter_version") or ""),
    }


def subject_jaccard(left_subjects: Iterable, right_subjects: Iterable) -> float:
    """两组主体的 Jaccard（口径与 Phase 10 的实体一致性判定一致，门槛由它给）。"""
    first = {str(item).casefold() for item in (left_subjects or ()) if str(item).strip()}
    second = {str(item).casefold() for item in (right_subjects or ()) if str(item).strip()}
    if not first or not second:
        return 0.0
    return round(len(first & second) / float(len(first | second)), 6)


def topic_key(question, *, subject: Mapping | None = None) -> str:
    """议题键（内容寻址）：同主体 + 同意图 → 同一个议题（**幂等与 TTL 都按它匹配**）。

    口径写死：只用**问题自身**的意图与主体（**不含计划里的实体**）—— 因为议题键要在
    "问的时候"与"下一轮读配置的时候"算出同一个值，把随计划漂移的实体算进去会让
    `(scope_key, topic_key)` 匹配不上（配置就白落了）。
    """
    info = dict(subject or subject_intent(question))
    body = "|".join([str(info.get("intent") or ""),
                     ",".join(sorted(str(item) for item in info.get("subjects") or []))[:200]])
    return "TQ" + _digest(body, 20)


def session_scope(run_meta: Mapping) -> dict:
    """会话作用域（**复用 Phase 09 的记忆作用域口径**：`SESSION` 作用域的 `scope_key`）。"""
    run_meta = run_meta if isinstance(run_meta, Mapping) else {}
    owner = str(run_meta.get("owner_user_id") or "")
    session_id = str(run_meta.get("session_id") or "")
    pack_id = str(run_meta.get("industry_pack_id") or "")
    return {
        "scope_key": memory_scope_key("SESSION", owner_user_id=owner, session_id=session_id,
                                      industry_pack_id=pack_id),
        "owner_user_id": owner, "session_id": session_id, "industry_pack_id": pack_id,
    }


# ── 历史与"上一次运行"的信号（**只读既有库表**，零新检索）────────────────────
def question_history(store, run_meta: Mapping, *, limit: int = DEFAULT_QUESTION_HISTORY_LIMIT,
                     exclude_run_id: str = "") -> list:
    """本会话的历史问题（按时间倒序；**跨会话不算重复**，所以只取同 session_id）。"""
    scope = session_scope(run_meta)
    if not scope["session_id"] or store is None:
        return []
    try:
        runs = store.list_runs(owner_user_id=scope["owner_user_id"],
                               session_id=scope["session_id"],
                               industry_pack_id=scope["industry_pack_id"],
                               limit=max(1, int(limit)))
    except Exception:  # noqa: BLE001
        return []
    history: list = []
    for run in runs or ():
        run_id = str(run.get("id") or "")
        if not run_id or run_id == str(exclude_run_id or ""):
            continue
        question = str(run.get("question_text") or "")
        if not question.strip():
            continue
        history.append({
            "run_id": run_id, "question": question,
            "created_at": str(run.get("created_at") or ""),
            "status": str(run.get("status") or ""),
            "final_answer": _mapping(run.get("final_answer")), "run": dict(run),
        })
    return history


def run_signals(store, run_id: str) -> dict:
    """一次历史运行的**既有信号**：答案降档 / 停止原因 / 增益 / 缺口未解决率。

    全部只读既有产物（`qa_runs.final_answer_json`、`qa_stage_runs.details_json` 里的
    `level1_retrieval.result.stats.gap_loop`、`qa_reasoning_traces` 的 `new_claims`），
    **不重跑任何检索、不调用任何模型**。
    """
    signals = {
        "run_id": str(run_id or ""), "stop_reason": "", "gap_loop": {}, "final_answer": {},
        "degraded": False, "degradation_reasons": [], "answer_status": "",
        "new_verified_claims": None, "unresolved_rate": None,
        "high_priority_open": 0, "resolved_high_priority_total": 0, "traces_new_claims": None,
    }
    if store is None or not run_id:
        return signals
    try:
        stages = store.stage_runs(str(run_id)) or []
    except Exception:  # noqa: BLE001
        stages = []
    for stage in stages:
        if str(stage.get("stage") or "") != "level1_retrieval":
            continue
        details = _mapping(stage.get("details"))
        result = _mapping(details.get("result"))
        stats = _mapping(result.get("stats"))
        gap_loop = _mapping(stats.get("gap_loop"))
        if gap_loop:
            signals["gap_loop"] = gap_loop
    loop = _mapping(signals["gap_loop"])
    loop_stats = _mapping(loop.get("stats"))
    signals["stop_reason"] = str(loop.get("stop_reason") or "")
    if loop_stats:
        signals["new_verified_claims"] = int(loop_stats.get("new_verified_claims_total") or 0)
        signals["high_priority_open"] = int(loop_stats.get("high_priority_open") or 0)
        signals["resolved_high_priority_total"] = int(
            loop_stats.get("resolved_high_priority_total") or 0)
        signals["unresolved_rate"] = unresolved_rate(loop_stats)
    if signals["new_verified_claims"] is None:
        try:
            traces = store.reasoning_traces(str(run_id)) or []
        except Exception:  # noqa: BLE001
            traces = []
        if traces:
            # 没有缺口环回执时，用 Phase 01 的 SearchTrace 列兜底（同样是"真的有没有新增"）
            signals["traces_new_claims"] = sum(int(row.get("new_claims") or 0) for row in traces)
    try:
        run = store.get_run(str(run_id)) or {}
    except Exception:  # noqa: BLE001
        run = {}
    answer = _mapping(run.get("final_answer"))
    signals["final_answer"] = answer
    signals["degraded"] = bool(run.get("degraded")) or bool(answer.get("degraded"))
    signals["degradation_reasons"] = [str(item) for item in
                                      (answer.get("degradation_reasons") or run.get("degradation_reasons") or [])]
    signals["answer_status"] = str(answer.get("status") or "")
    return signals


def unresolved_rate(gap_stats: Mapping) -> float:
    """缺口未解决率 = 未解决高优缺口 /（未解决高优缺口 + 已解决高优缺口）（分母 0 → 0.0）。

    口径写死在这里，回执里同时给出分子分母 —— 于是"未解决率高"这个判据可复算。
    """
    stats = _mapping(gap_stats)
    open_high = int(stats.get("high_priority_open") or 0)
    resolved = int(stats.get("resolved_high_priority_total") or 0)
    total = open_high + resolved
    if total <= 0:
        return 0.0
    return round(open_high / float(total), 6)


# ── 触发判据（可复算，纯规则）──────────────────────────────────────────────
def detect_repeat(*, question: str, history: Sequence[Mapping],
                  plan: Mapping | None = None) -> dict:
    """在同一会话的历史问题里找"同题/相似题"（返回命中详情，**判不出就如实说没命中**）。

    判据（两条，命中任一即算重复；两条都写进回执）：
      1. `LEXICAL_OVERLAP`：归一化实词 Jaccard ≥ `overlap_threshold()`；
      2. `SAME_SUBJECT_SAME_INTENT`：意图相同（Phase 05 口径）**且** 主体 Jaccard ≥
         Phase 10 的实体门槛（`contradiction_jaccard()`）。
    """
    current = subject_intent(question)
    threshold = overlap_threshold()
    jaccard_floor = contradiction_jaccard()
    best: dict = {
        "repeat": False, "triggers": [], "previous_run_id": "", "previous_question": "",
        "similarity": {"overlap": 0.0, "subject_jaccard": 0.0, "intent": current["intent"],
                       "previous_intent": "", "overlap_threshold": threshold,
                       "subject_jaccard_floor": jaccard_floor},
        "matches": [],
    }
    for item in list(history or ())[:window_turns()]:
        if not isinstance(item, Mapping):
            continue
        prior_question = str(item.get("question") or "")
        # 两侧都用**问题自身**的主体/意图（不掺计划实体）：判据对称才可复算
        prior = subject_intent(prior_question)
        overlap = lexical_overlap(question, prior_question)
        jaccard = subject_jaccard(current["subjects"], prior["subjects"])
        triggers: list = []
        if overlap >= threshold:
            triggers.append("LEXICAL_OVERLAP")
        if current["intent"] and current["intent"] == prior["intent"] and jaccard >= jaccard_floor:
            triggers.append("SAME_SUBJECT_SAME_INTENT")
        if not triggers:
            continue
        match = {
            "run_id": str(item.get("run_id") or ""), "question": prior_question,
            "triggers": triggers, "overlap": overlap, "subject_jaccard": jaccard,
            "intent": current["intent"], "previous_intent": prior["intent"],
            "created_at": str(item.get("created_at") or ""),
        }
        best["matches"].append(match)
        # 取**最近**的一次（history 已按时间倒序）作为"上一次"
        if not best["previous_run_id"]:
            best.update({
                "repeat": True, "triggers": triggers,
                "previous_run_id": match["run_id"], "previous_question": prior_question,
                "previous_status": str(item.get("status") or ""),
                "previous_final_answer": _mapping(item.get("final_answer")),
            })
            best["similarity"].update({
                "overlap": overlap, "subject_jaccard": jaccard,
                "previous_intent": prior["intent"], "intent": current["intent"],
                "matches_in_window": 1,
            })
    best["similarity"]["matches_in_window"] = len(best["matches"])
    best["subject"] = current
    if best["repeat"]:
        best["topic_key"] = topic_key(question)
    return best


# ── 不满意依据（**必须逐条落到回执**，不许空口宣称）────────────────────────
def assess_dissatisfaction(signals: Mapping, *, threshold: float | None = None) -> dict:
    """从"上一次运行"的既有信号里**逐条找可解释依据**（返回 `{evidence, detail}`）。

    四类六条（`REALIGNMENT_DISSATISFACTION_REASONS`）：
      ① 上次无新增已核验证据；② 上次被标注降档/无证据；③ 上次停止原因属 NO_GAIN /
      BUDGET_EXHAUSTED / MAX_DEPTH；④ 上次缺口未解决率高于阈值。
    **一条都拿不到 → `evidence` 为空 → 不许交互。**
    """
    signals = _mapping(signals)
    limit = unresolved_rate_threshold() if threshold is None else float(threshold)
    evidence: list = []
    detail: dict = {}
    new_claims = signals.get("new_verified_claims")
    if new_claims is None:
        new_claims = signals.get("traces_new_claims")
    if new_claims is not None and int(new_claims) == 0:
        evidence.append("NO_NEW_VERIFIED_EVIDENCE")
        detail["NO_NEW_VERIFIED_EVIDENCE"] = {"new_verified_claims": int(new_claims),
                                              "basis": ("gap_loop.new_verified_claims_total"
                                                        if signals.get("new_verified_claims") is not None
                                                        else "reasoning_traces.new_claims")}
    reasons = [str(item) for item in (signals.get("degradation_reasons") or [])]
    if bool(signals.get("degraded")) or reasons or str(signals.get("answer_status") or "") \
            == "insufficient_evidence":
        evidence.append("ANSWER_DEGRADED")
        detail["ANSWER_DEGRADED"] = {"degraded": bool(signals.get("degraded")),
                                     "reasons": reasons[:6],
                                     "answer_status": str(signals.get("answer_status") or "")}
    stop_reason = str(signals.get("stop_reason") or "")
    mapped = _STOP_REASON_TO_EVIDENCE.get(stop_reason)
    if mapped:
        evidence.append(mapped)
        detail[mapped] = {"stop_reason": stop_reason}
    rate = signals.get("unresolved_rate")
    if rate is not None and float(rate) > limit:
        evidence.append("UNRESOLVED_GAP_RATE_HIGH")
        detail["UNRESOLVED_GAP_RATE_HIGH"] = {
            "unresolved_rate": float(rate), "threshold": limit,
            "high_priority_open": int(signals.get("high_priority_open") or 0),
            "resolved_high_priority_total": int(signals.get("resolved_high_priority_total") or 0)}
    ordered = [code for code in REALIGNMENT_DISSATISFACTION_REASONS if code in evidence]
    return {
        "evidence": ordered,
        "detail": detail,
        "threshold": limit,
        "has_evidence": bool(ordered),
        "explanation": "；".join(_REASON_TEXT.get(code, code) for code in ordered),
    }


# ── 交互（一次、给选项、可拒绝）────────────────────────────────────────────
def options_payload() -> list:
    """选项表（契约形状）：每一项都带**预期影响与代价**（P14-07 硬要求）。"""
    rows: list = []
    for option_id in REALIGNMENT_OPTION_IDS:
        spec = OPTION_CATALOG.get(option_id) or {}
        rows.append({
            "option_version": REALIGNMENT_OPTION_VERSION,
            "option_id": option_id,
            "label": str(spec.get("label") or option_id),
            "config_keys": [str(key) for key in (spec.get("config_keys") or ())],
            "expected_effect": str(spec.get("expected_effect") or ""),
            "cost": str(spec.get("cost") or ""),
            "risk": str(spec.get("risk") or ""),
        })
    return rows


def _template_clarification(*, evidence: Mapping, previous_question: str = "",
                            triggers: Sequence[str] = ()) -> str:
    """规则模板交互文案（**零模型**）：把依据逐条说清，再给选择权。"""
    lines = [BASE_QUESTION]
    why = str(_mapping(evidence).get("explanation") or "").strip()
    if why:
        lines.append("判定依据：%s。" % why)
    if previous_question:
        lines.append("上次的问题是：「%s」。" % previous_question[:60])
    if "SAME_SUBJECT_SAME_INTENT" in (triggers or ()):
        lines.append("判定口径：同一主体 + 同一命题意图（复用 Phase 05 的意图与 Phase 10 的实体一致性）。")
    elif "LEXICAL_OVERLAP" in (triggers or ()):
        lines.append("判定口径：与上一轮问题的归一化词面重叠达到阈值。")
    lines.append("下面每一项都写了预期影响与代价；也可以直接选「保持原思路」。")
    return "".join(lines)


def clarification_text(*, evidence: Mapping, previous_question: str = "",
                       triggers: Sequence[str] = (), clarifier: str = "") -> dict:
    """交互文案（模板；可选注入生成器，**未注册/抛错/非法载荷回落模板并记账**）。"""
    fallback = ""
    used = "template"
    text = ""
    name = str(clarifier or clarifier_name())
    if name:
        fn = CLARIFIERS.get(name)
        if fn is None:
            fallback = "UNKNOWN_CLARIFIER"
        else:
            try:
                payload = fn({"evidence": dict(evidence or {}), "previous_question": previous_question,
                              "triggers": list(triggers or ())})
                candidate = str((payload or {}).get("question") or "") if isinstance(
                    payload, Mapping) else ""
                if candidate.strip():
                    text, used = candidate.strip(), name
                else:
                    fallback = "INVALID_CLARIFIER_PAYLOAD"
            except Exception as exc:  # noqa: BLE001 —— 生成器坏掉绝不许阻断交互
                fallback = "%s: %s" % (type(exc).__name__, str(exc)[:80])
    if not text:
        text = _template_clarification(evidence=evidence, previous_question=previous_question,
                                       triggers=triggers)
    return {"question": text, "writer": used, "fallback": fallback}


def build_interaction(*, topic: str, scope: Mapping, detection: Mapping, signals: Mapping,
                      dissatisfaction: Mapping, previous_run_id: str = "",
                      interaction_index: int = 1, clarifier: str = "") -> dict:
    """构造一次交互（`REALIGNMENT_INTERACTION_SCHEMA` 形状）。"""
    text = clarification_text(evidence=dissatisfaction, triggers=detection.get("triggers") or (),
                              previous_question=str(detection.get("previous_question") or ""),
                              clarifier=clarifier)
    payload = {
        "realignment_version": REALIGNMENT_VERSION,
        "detector_version": REALIGNMENT_DETECTOR_VERSION,
        "topic_key": str(topic or ""),
        "scope_key": str(_mapping(scope).get("scope_key") or ""),
        "previous_run_id": str(previous_run_id or ""),
        "previous_stop_reason": str(_mapping(signals).get("stop_reason") or ""),
        "interaction_index": max(1, int(interaction_index)),
        "max_interactions": max_interactions(),
        "triggers": list(detection.get("triggers") or []),
        "evidence": list(_mapping(dissatisfaction).get("evidence") or []),
        "evidence_detail": dict(_mapping(dissatisfaction).get("detail") or {}),
        "similarity": dict(_mapping(detection.get("similarity")) or {}),
        "question": text["question"],
        "options": options_payload(),
        "settings": realignment_settings(),
        "idempotent": True,
    }
    if text.get("fallback"):
        payload["clarifier_fallback"] = text["fallback"]
    ok, note = validate_contract("realignment_interaction", dict(payload))
    payload["contract_ok"] = bool(ok)
    if not ok:
        payload["contract_error"] = note
    return payload


# ── 用户选择 → 会话级可执行配置 ────────────────────────────────────────────
def choice_to_config(option_id: str) -> dict:
    """选项 → 可执行配置（**只取契约允许的键**；`keep_strategy` → 空配置）。"""
    spec = OPTION_CATALOG.get(str(option_id)) or {}
    config = {str(key): value for key, value in _mapping(spec.get("config")).items()
              if str(key) in REALIGNMENT_CONFIG_KEYS}
    return normalize_config(config)


def normalize_config(config: Mapping) -> dict:
    """配置归一化（键闭集合 + 取值校验）：**非法键一律丢弃**（绝不把脏配置灌进检索）。"""
    out: dict = {}
    config = _mapping(config)
    if "route_preference" in config:
        routes = [str(item) for item in (config.get("route_preference") or ())
                  if str(item) in QA_RETRIEVAL_ROUTES]
        if routes:
            out["route_preference"] = list(dict.fromkeys(routes))
    if "time_window" in config:
        window = _mapping(config.get("time_window"))
        days = window.get("days")
        clean = {}
        try:
            if days is not None and int(days) > 0:
                clean["days"] = int(days)
        except (TypeError, ValueError):
            clean = {}
        for key in ("start", "end", "label"):
            if str(window.get(key) or "").strip():
                clean[key] = str(window[key])
        if clean:
            out["time_window"] = clean
    for key, low, high in (("authority_floor", 0.0, 100.0),
                           ("relevance_floor", 0.0, 1.0)):
        if key in config:
            try:
                out[key] = max(low, min(high, float(config.get(key))))
            except (TypeError, ValueError):
                continue
    if "analysis_lens" in config:
        lens = str(config.get("analysis_lens") or "")
        if lens in ANALYSIS_LENSES:
            out["analysis_lens"] = lens
    return out


def config_keys() -> tuple:
    return tuple(REALIGNMENT_CONFIG_KEYS)


def decision_payload(*, option_id: str, topic: str, scope: Mapping, decided_by: str = "user",
                     ttl_until: str = "") -> dict:
    """用户决定 → `REALIGNMENT_CONFIG_SCHEMA` 形状（含 decision 与落成的配置）。"""
    option_id = str(option_id or "")
    if option_id not in REALIGNMENT_OPTION_IDS:
        raise ValueError("未知的调整选项：%s" % option_id)
    decision = "KEEP_STRATEGY" if option_id == "keep_strategy" else "CHANGE_STRATEGY"
    config = choice_to_config(option_id)
    payload = {
        "config_version": REALIGNMENT_VERSION,
        "decision": decision,
        "option_id": option_id,
        "topic_key": str(topic or ""),
        "scope_key": str(_mapping(scope).get("scope_key") or ""),
        "session_id": str(_mapping(scope).get("session_id") or ""),
        "applied_keys": sorted(config),
        "config": config,
        "decided_by": str(decided_by or "user"),
        "decided_at": _stamp(_now()),
        "ttl_until": str(ttl_until or ""),
    }
    return payload


def record_choice(store, *, run_meta: Mapping, topic: str, option_id: str,
                  interaction: Mapping | None = None, decided_by: str = "user") -> dict:
    """把用户的选择落库（**会话级可执行配置**），返回写后的行。

    幂等：同 `(scope_key, topic_key)` 覆盖同一行；`asked_count` 不因"记录决定"而增加
    （只有真的发起过一次交互才算）。
    """
    scope = session_scope(run_meta)
    if not scope["session_id"]:
        return {}
    ttl = _stamp(_now() + timedelta(hours=ttl_hours()))
    payload = decision_payload(option_id=option_id, topic=topic, scope=scope,
                               decided_by=decided_by, ttl_until=ttl)
    if store is None:
        return payload
    interaction = _mapping(interaction)
    return store.save_realignment_decision(
        scope_key=scope["scope_key"], topic_key=str(topic or ""),
        owner_user_id=scope["owner_user_id"], session_id=scope["session_id"],
        industry_pack_id=scope["industry_pack_id"],
        payload={
            "decision": payload["decision"], "option_id": payload["option_id"],
            "config": payload["config"], "applied_keys": payload["applied_keys"],
            "ttl_until": payload["ttl_until"],
            "triggers": list(interaction.get("triggers") or []),
            "evidence": list(interaction.get("evidence") or []),
            "similarity": dict(interaction.get("similarity") or {}),
            "previous_run_id": str(interaction.get("previous_run_id") or ""),
            "run_id": str(_mapping(run_meta).get("id") or ""),
            "record": {"decided_by": payload["decided_by"], "decided_at": payload["decided_at"]},
        }, asked=False)


def session_config_for(store, *, run_meta: Mapping, question: str) -> dict:
    """本会话对该议题**当前生效**的可执行配置（没有/已过期/选择"保持原思路" → 空）。

    只认 `CHANGE_STRATEGY`（"保持原思路"是"别改"，不是一份配置）；TTL 过期后自动失效并
    在回执里标明 `expired`（不静默继续用旧决定）。
    """
    scope = session_scope(run_meta)
    if not scope["session_id"] or store is None:
        return {}
    topic = topic_key(question)
    try:
        row = store.realignment_decision(scope_key=scope["scope_key"], topic_key=topic) or {}
    except Exception:  # noqa: BLE001
        return {}
    if not row:
        return {}
    decision = str(row.get("decision") or "")
    ttl_until = _parse_time(row.get("ttl_until"))
    expired = bool(ttl_until and _now() > ttl_until)
    config = normalize_config(row.get("config") or {})
    payload = {
        "topic_key": topic, "decision": decision, "option_id": str(row.get("option_id") or ""),
        "config": config, "applied_keys": list(row.get("applied_keys") or []),
        "decided_at": str(row.get("updated_at") or ""), "ttl_until": str(row.get("ttl_until") or ""),
        "expired": expired, "usable": bool(decision == "CHANGE_STRATEGY" and config and not expired),
    }
    return payload


# ── 主入口（管线只调这一个函数）────────────────────────────────────────────
def realignment_receipt(*, interaction: Mapping | None = None, config: Mapping | None = None,
                        applied: Mapping | None = None, skipped_reason: str = "",
                        skip_detail: str = "", error: str = "",
                        detection: Mapping | None = None) -> dict:
    """总回执（`REALIGNMENT_RECEIPT_SCHEMA` 形状；放 `plan` 阶段的兄弟键）。"""
    payload = {
        "realignment_version": REALIGNMENT_VERSION,
        "interaction": dict(interaction or {}),
        "config": dict(config or {}),
        "applied": dict(applied or {}),
        "skipped_reason": str(skipped_reason or ""),
        "skip_detail": str(skip_detail or ""),
        "realignment_error": str(error or ""),
        "settings": realignment_settings(),
        "detection": {
            "triggers": list(_mapping(detection).get("triggers") or []),
            "similarity": dict(_mapping(detection).get("similarity") or {}),
            "previous_run_id": str(_mapping(detection).get("previous_run_id") or ""),
            "matches": len(_mapping(detection).get("matches") or []),
        },
        "stats": {
            "asked": 1 if interaction else 0,
            "has_evidence": bool(_mapping(interaction).get("evidence")),
            "config_keys": sorted(_mapping(config).get("config") or {}),
            "applied_keys": list(_mapping(applied).get("changed") or []),
        },
    }
    ok, note = validate_contract("realignment_receipt", dict(payload))
    payload["contract_ok"] = bool(ok)
    if not ok:
        payload["contract_error"] = note
    return payload


def interaction_slot_receipt() -> dict:
    """P11 预留的**交互技能位**在 P14-07 的挂载回执（**不改 Phase 11 的声明**）。

    Phase 11 的 `SKILL_INTERACTION_SLOT` 是"当时只声明不实现"的历史记录（`mounted=False`
    有守例钉着，不许改）；这里是同一个技能位在 P14-07 的**运行时视图**：
    `patient_inquiry` + owner `P14-07` + 两个预留能力现在**已实现**。
    """
    return {
        "skill_id": str(SKILL_INTERACTION_SLOT.get("skill_id") or "patient_inquiry"),
        "owner_phase": SKILL_INTERACTION_SLOT_OWNER,
        "declared_by_phase11": bool(SKILL_INTERACTION_SLOT.get("mounted") is False),
        "mounted_in_phase14": True,
        "capabilities_reserved": list(SKILL_INTERACTION_SLOT.get("capabilities_reserved") or ()),
        "capabilities_implemented": list(
            SKILL_INTERACTION_SLOT.get("capabilities_not_implemented_here") or ()),
        "implementation": {"detector": REALIGNMENT_DETECTOR_VERSION,
                           "options": REALIGNMENT_OPTION_VERSION,
                           "realignment": REALIGNMENT_VERSION},
        "note": ("P14-07 挂载：重复提问识别 → 给出可选思路 → 落会话级配置 → 写进 SearchTrace。"
                 "Phase 11 的声明（mounted=False）保持不变，它是「当时未实现」的历史记录。"),
    }


def realignment_step(*, store, run_meta: Mapping, question: str, plan: Mapping | None = None,
                     interact: bool = True, clarifier: str = "") -> dict:
    """P14-07 的唯一入口：**判定 → 依据 → 交互（一次）→ 生效配置**（返回总回执）。

    任何异常都在这里被收敛成带 `realignment_error` 的回执（**绝不阻断回答**）。
    单轮调用（无 session_id）→ `NO_SESSION_CONTEXT`。
    """
    try:
        scope = session_scope(run_meta)
        if not scope["session_id"]:
            return realignment_receipt(skipped_reason="NO_SESSION_CONTEXT",
                                       skip_detail="请求没有会话上下文（单轮调用），重复提问识别自动跳过")
        history = question_history(store, run_meta, exclude_run_id=str(_mapping(run_meta).get("id") or ""))
        detection = detect_repeat(question=question, history=history, plan=plan)
        active = session_config_for(store, run_meta=run_meta, question=question)
        if not detection.get("repeat"):
            return realignment_receipt(skipped_reason="NOT_A_REPEAT", config=active,
                                       skip_detail="本会话内没有同题/相似题（多个历史问题但都不构成重复）",
                                       detection=detection)
        topic = str(detection.get("topic_key") or "")
        row = {}
        try:
            row = store.realignment_decision(scope_key=scope["scope_key"], topic_key=topic) or {}
        except Exception:  # noqa: BLE001
            row = {}
        asked_count = int(row.get("asked_count") or 0)
        run_id = str(_mapping(run_meta).get("id") or "")
        same_run = bool(row.get("run_id") and str(row.get("run_id")) == run_id
                        and asked_count > 0)
        ttl_until = _parse_time(row.get("ttl_until"))
        within_ttl = bool(ttl_until and _now() <= ttl_until)
        if str(row.get("decision") or "") == "KEEP_STRATEGY" and within_ttl:
            return realignment_receipt(skipped_reason="KEEP_STRATEGY_DECLARED",
                                       config=active,
                                       skip_detail="用户已明确「保持原思路」，TTL 内不再追问同一议题",
                                       detection=detection)
        if asked_count >= max_interactions() and (within_ttl or same_run):
            return realignment_receipt(skipped_reason="ALREADY_ASKED_IN_TTL",
                                       config=active,
                                       skip_detail="同一会话同一议题已经交互过 %d 次（上限 %d）"
                                                   % (asked_count, max_interactions()),
                                       detection=detection)
        signals = run_signals(store, str(detection.get("previous_run_id") or ""))
        dissatisfaction = assess_dissatisfaction(signals)
        if not dissatisfaction.get("has_evidence"):
            return realignment_receipt(skipped_reason="NO_DISSATISFACTION_EVIDENCE",
                                       config=active,
                                       skip_detail="拿不到任何可解释的不满意依据（不空口宣称用户不满意）",
                                       detection=detection)
        if not interact:
            return realignment_receipt(skipped_reason="NO_SESSION_CONTEXT", config=active,
                                       skip_detail="interact=False：只判定不交互",
                                       detection=detection)
        interaction = build_interaction(topic=topic, scope=scope, detection=detection,
                                        signals=signals, dissatisfaction=dissatisfaction,
                                        previous_run_id=str(detection.get("previous_run_id") or ""),
                                        interaction_index=asked_count + 1, clarifier=clarifier)
        interaction["dissatisfaction_explanation"] = dissatisfaction["explanation"]
        if store is not None and not same_run:
            store.save_realignment_decision(
                scope_key=scope["scope_key"], topic_key=topic,
                owner_user_id=scope["owner_user_id"], session_id=scope["session_id"],
                industry_pack_id=scope["industry_pack_id"],
                payload={"triggers": interaction["triggers"], "evidence": interaction["evidence"],
                         "similarity": interaction["similarity"],
                         "previous_run_id": interaction["previous_run_id"], "run_id": run_id,
                         "question_hash": _digest(normalize_question(question)),
                         "last_asked_at": _stamp(_now()),
                         "record": {"explanation": dissatisfaction["explanation"]}},
                asked=True)
        return realignment_receipt(interaction=interaction, config=active,
                                   detection=detection)
    except Exception as exc:  # noqa: BLE001 —— 判定/交互构造抛错**绝不阻断回答**
        return realignment_receipt(error="%s: %s" % (type(exc).__name__, str(exc)[:160]))


__all__ = [
    "BASE_QUESTION",
    "CLARIFIERS",
    "LENS_CATEGORY",
    "OPTION_CATALOG",
    "ROUTE_ORDER_FOR_CHANGE",
    "assess_dissatisfaction",
    "build_interaction",
    "choice_to_config",
    "clarification_text",
    "clarifiers",
    "config_keys",
    "decision_payload",
    "detect_repeat",
    "interaction_slot_receipt",
    "lexical_overlap",
    "max_interactions",
    "normalize_config",
    "normalize_question",
    "options_payload",
    "overlap_threshold",
    "question_history",
    "question_terms",
    "realignment_enabled",
    "realignment_receipt",
    "realignment_settings",
    "realignment_step",
    "record_choice",
    "register_clarifier",
    "run_signals",
    "session_config_for",
    "session_scope",
    "subject_intent",
    "subject_jaccard",
    "topic_key",
    "ttl_hours",
    "unresolved_rate",
    "window_turns",
]

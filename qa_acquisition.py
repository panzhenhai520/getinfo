#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 14（P14-01…P14-06）· SAGE-RAG Unified Acquisition（**纯规则/统计，零模型调用、零嵌入端点**）。

通用包 01_V2_ARCHITECTURE 依据：
  · §33 第 10 条工程原则："把『检索』和『问患者』统一成 Evidence Acquisition" → 六个动作
    `retrieve_document/retrieve_emr/graph_traverse/query_database/web_search/ask_patient`
    统一成 **Action Candidate**（P14-01，取值逐字进契约）；
  · §13/§21 的效用公式 `Utility = ExpectedGapReduction × Importance × EvidenceReliability /
    (Latency + Cost + UserBurden)`（P14-02：EGR 与效用逐项可手算复现，权重全部可配）；
  · §33 的四路统一获取（Knowledge RAG / Historical EMR / Structured DB / Web）→ P14-03
    的路由表，**只吃 Phase 01 冻结的七个通道**，与 Phase 04 的 `HUNTER_BY_ROUTE`、
    Phase 11 的 `SKILL_ROUTE_BY_ID` 同源；
  · §6/§13 的 Ask Patient → P14-04：规则模板拼装问诊候选（零模型），恒 `is_evidence=False`；
  · §13 的关键收益原话："若信息已在 Evidence Graph 中但只是 Context Gap，系统应**重组上下文
    而不是再次询问患者**，从而减少无意义追问" → P14-05 的抑制闸门；
  · §21/§22 的 `clinical_risk / safety_weight / diagnostic_discrimination` 与
    `SafetyCritical=true → Priority Override` → P14-06（安全规则覆盖普通成本效用）。

边界与取舍（诚实声明，宁写 PARTIAL 不谎报）
--------------------------------------------
  1. **零模型**：本模块全部判定都是规则与算术（可手算复现）；问诊文案是模板拼装。
     若将来要接 LLM 生成澄清问题，入口已经在 `register_inquiry_writer()` 预留（**默认不调用**）。
  2. **不新造真源**：缺口类型/严重度/建议通道来自 Phase 07（`GAP_SEVERITY` / `routes_for`），
     Context Gap 与动作来自 Phase 08（`CONTEXT_GAP_ACTIONS`），安全关键与优先级覆盖来自
     Phase 07（`safety_terms` 与 `SAFETY_PRIORITY_FLOOR` 同档），权限位来自 Phase 11
     （`SKILL_PERMISSIONS` 的 `patient_contact`，默认被拒）。
  3. **动作是"计划"不是"执行"**：本模块产出获取计划与通道偏好；真的去检索仍然只有
     Phase 04/07 那一条链路（`ArticleRetriever` + 缺口补充跳），`ask_patient` 只产出
     "要问谁什么"的候选（本仓库无患者通道，如实标记 `SOURCE_UNAVAILABLE` 或把候选交回交互层）。
  4. **MASTER_RULES 第 11 条**：问诊候选与交互文案**不是**已验证证据
     （`is_evidence=False` / `in_citation_map=False` / `requires_revalidation=True`）。
"""
from __future__ import annotations

import hashlib
import os
from typing import Callable, Iterable, Mapping, Sequence

from qa_gap_analyzer import (
    GAP_SEVERITY,
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
    relevance_floor,
    relevance_score,
    routes_for,
    term_set,
)
from qa_graph_contracts import (
    ACQUISITION_ACTION_HUNTERS,
    ACQUISITION_ACTION_ROUTES,
    ACQUISITION_ACTION_SCHEMA_VERSION,
    ACQUISITION_ACTION_SOURCE,
    ACQUISITION_ACTIONS,
    ACQUISITION_CLINICAL_FACTORS,
    ACQUISITION_INTERACTION_ACTIONS,
    ACQUISITION_PLANNER_VERSION,
    ACQUISITION_ROUTES_VERSION,
    ACQUISITION_SOURCES,
    ACQUISITION_UTILITY_FACTORS,
    ACQUISITION_UTILITY_VERSION,
    CONTEXT_GAP_ACTIONS,
    CONTEXT_GAP_NO_REPEAT_REASONS,
    CONTEXT_GAP_NO_REPEAT_VERSION,
    PATIENT_INQUIRY_VERSION,
    QA_GAP_TYPES,
    QA_RETRIEVAL_ROUTES,
    SAFETY_PRIORITY_FLOOR,
    SKILL_INTERACTION_SLOT,
    SKILL_INTERACTION_SLOT_OWNER,
    validate as validate_contract,
)

# ── 可配置旋钮（全部可回滚；默认值写在常量里便于复算）───────────────────────
DEFAULT_UTILITY_FLOOR = 0.0
"""动作效用下限（默认 0 = 不设门槛，只按相对效用选）：低于它就记 `UTILITY_FLOOR`。"""

DEFAULT_MAX_USER_BURDEN = "medium"
"""用户负担上限档（§13 的 UserBurden；超过它的交互动作记 `OVER_USER_BURDEN`）。"""

DEFAULT_ROUTE_FIT_STEP = 0.15
"""Phase 07 建议通道列表里，第 2/3/… 个通道的 EGR 折减步长（第 1 个 = 1.0）。"""

DEFAULT_ROUTE_FIT_FLOOR = 0.40
"""折减下限（再靠后的通道也不会低于它 —— 通道列表本来就都是"能试的路子"）。"""

DEFAULT_UTILITY_EPSILON = 0.05
"""分母下限（避免 0 除）：`Latency + Cost + UserBurden` 至少按 0.05 算。"""

DEFAULT_SAFETY_RATIO = 0.60
"""安全动作换掉普通赢家所需的最低效用比（§22 的安全覆盖**有条件**，见 `safety_ratio()`）。"""

BURDEN_ORDER = ("none", "low", "medium", "high")
"""用户负担四档（§13 的 UserBurden 落成可比较的档位）。"""

BURDEN_COST = {"none": 0.0, "low": 0.15, "medium": 0.35, "high": 0.60}
"""负担档 → 效用分母里的数值（**可复算**：手算用例直接用它）。"""

SOURCE_RELIABILITY = {
    # §21 的 EvidenceReliability：结构化登记表 > 内部业务库 > 文档检索 > 图谱 > 网页 > 患者主诉。
    # 患者主诉最低是**故意的**：它是"用户陈述"，必须过 Phase 03 核验才可能变成证据。
    "db": 0.85, "emr": 0.80, "kb": 0.75, "graph": 0.70, "web": 0.50, "patient": 0.35,
}
"""来源 → 证据可靠性（0…1，规则给定、可复算）。"""

ACTION_LATENCY = {
    "query_database": 0.10, "graph_traverse": 0.20, "retrieve_emr": 0.30,
    "retrieve_document": 0.40, "web_search": 0.70, "ask_patient": 0.05,
}
"""动作 → 延迟成本（0…1 归一化；`ask_patient` 的**等待**成本最低，但负担另算）。"""

ACTION_COST = {
    "query_database": 0.10, "graph_traverse": 0.20, "retrieve_emr": 0.30,
    "retrieve_document": 0.40, "web_search": 0.60, "ask_patient": 0.00,
}
"""动作 → 预算成本（0…1 归一化；问患者不花检索预算 —— 花的是人的耐心）。"""

EMR_GAP_FIT = {
    # 本部署没有 EMR 通道（D-002 中性映射为"内部证据库"）：这里给的是**能力声明**，
    # 只有当部署方真的注入了 structured_providers 时这些动作才会被选中。
    QA_GAP_NO_EVIDENCE: 0.70,
    QA_GAP_MISSING_ENTITY_LINK: 0.80,
    QA_GAP_MISSING_TIME_LINK: 0.90,
    QA_GAP_LOW_AUTHORITY: 0.60,
    QA_GAP_CONTRADICTION: 0.70,
}
"""`retrieve_emr` 对各缺口类型的基础契合度（没有检索通道可反查，所以单独给表）。"""

ASKABLE_GAP_FIT = {
    # §6/§13："能不能从当前 EMR 找到？找不到 → Patient Inquiry Candidate"。
    # 只有"关于当事者本人的事实"才值得问人；知识型缺口问人也拿不到（给 0）。
    QA_GAP_NO_EVIDENCE: 0.45,
    QA_GAP_MISSING_TIME_LINK: 0.90,
    QA_GAP_MISSING_ENTITY_LINK: 0.85,
    QA_GAP_AMBIGUOUS_ENTITY: 0.95,
    QA_GAP_LOW_RELEVANCE: 0.20,
    QA_GAP_LOW_AUTHORITY: 0.00,
    QA_GAP_SINGLE_SOURCE: 0.00,
    QA_GAP_CONTRADICTION: 0.30,
    QA_GAP_MISSING_CAUSAL_BRIDGE: 0.00,
    QA_GAP_MISSING_COUNTEREVIDENCE: 0.55,
}
"""`ask_patient` 对各缺口类型的基础契合度（0 = **不该问人**，问也拿不到）。

这张表是 P14-04 的"该问什么"口径，也是 §13 的"减少无意义追问"在计划层的落点。"""

ASK_PATIENT_BURDEN_BY_GAP = {
    QA_GAP_AMBIGUOUS_ENTITY: "low",
    QA_GAP_MISSING_TIME_LINK: "low",
    QA_GAP_MISSING_ENTITY_LINK: "low",
    QA_GAP_NO_EVIDENCE: "medium",
    QA_GAP_MISSING_COUNTEREVIDENCE: "medium",
    QA_GAP_CONTRADICTION: "medium",
}
"""问诊一次的负担档（缺口类型 → 档位；没列的按 `medium`）。"""

PATIENT_QUESTION_TEMPLATES = {
    QA_GAP_MISSING_TIME_LINK: "请确认时间口径：这件事发生的具体时点（或大致区间）是什么？",
    QA_GAP_MISSING_ENTITY_LINK: "请确认对象：你指的是哪一个具体主体（名称/口径）？",
    QA_GAP_AMBIGUOUS_ENTITY: "你说的这个主体，具体是指哪一家/哪一个？请给一个能唯一确定的说法。",
    QA_GAP_NO_EVIDENCE: "公开资料里没有找到直接依据：你手上是否有一手信息（文件编号、时间、来源）可以补充？",
    QA_GAP_MISSING_COUNTEREVIDENCE: "你这边是否知道与当前结论相反的情况或风险点？",
    QA_GAP_CONTRADICTION: "现有资料存在互相矛盾的说法：以你掌握的情况，哪一版更接近事实？",
}
"""问诊文案模板（**规则模板、零模型**；语言中立口径，本项目中文环境用中文）。

模板只说"缺什么、请给什么" —— 不诱导答案、不许诺结论（合规底线）。"""

_INQUIRY_WRITERS: dict = {}
"""可插拔"问诊文案生成器"注册表（**默认空 = 只用模板，绝不调用**）。

留这个口子的原因：规格允许将来用 LLM 生成更自然的澄清问题，但本机硬约束禁止调用任何模型端点，
所以本阶段只提供注入点与"未注册/抛错/非法载荷一律回落模板并记账"的三条失败路径。
"""

_ASSERTION_ACTIONS = ("NO_EVIDENCE", "MISSING_TIME_LINK", "MISSING_ENTITY_LINK", "AMBIGUOUS_ENTITY")


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().casefold() not in {"0", "false", "no", "off", ""}


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def acquisition_enabled() -> bool:
    """P14 总开关（`QA_ACQUISITION`，**默认关**：与 Phase 06/07/08/12/13 同样手法，
    关掉时检索阶段一个键都不新增、行为逐字回到 Phase 13）。"""
    return _env_flag("QA_ACQUISITION", False)


def ask_patient_enabled() -> bool:
    """问诊路由开关（`QA_ASK_PATIENT`，默认关）：关掉时 `ask_patient` 恒记
    `SOURCE_UNAVAILABLE`（本仓库无患者通道，**不假装能问**）。"""
    return _env_flag("QA_ASK_PATIENT", False)


def utility_floor() -> float:
    return _env_float("QA_ACQ_UTILITY_FLOOR", DEFAULT_UTILITY_FLOOR, 0.0, 100.0)


def max_user_burden() -> str:
    raw = str(os.getenv("QA_ACQ_MAX_USER_BURDEN") or DEFAULT_MAX_USER_BURDEN).strip().casefold()
    return raw if raw in BURDEN_ORDER else DEFAULT_MAX_USER_BURDEN


def route_fit_step() -> float:
    return _env_float("QA_ACQ_ROUTE_FIT_STEP", DEFAULT_ROUTE_FIT_STEP, 0.0, 1.0)


def route_fit_floor() -> float:
    return _env_float("QA_ACQ_ROUTE_FIT_FLOOR", DEFAULT_ROUTE_FIT_FLOOR, 0.0, 1.0)


def utility_epsilon() -> float:
    return _env_float("QA_ACQ_UTILITY_EPSILON", DEFAULT_UTILITY_EPSILON, 0.0001, 1.0)


def safety_ratio() -> float:
    """安全动作的效用下限比（`QA_ACQ_SAFETY_RATIO`，默认 0.6）。

    §22 的安全覆盖**不是无条件**的：安全动作至少要拿到普通赢家 60% 的效用才换得动它
    （否则等于"只要沾安全词就无视一切效用"，那会把普通问题也拖成追问）。
    """
    return _env_float("QA_ACQ_SAFETY_RATIO", DEFAULT_SAFETY_RATIO, 0.0, 1.0)


def utility_weights() -> dict:
    """§21 的 `Importance` 加分权重（临床/安全因子），全部可配、逐项写进回执。"""
    return {
        "priority": _env_float("QA_ACQ_W_PRIORITY", 1.0, 0.0, 10.0),
        "safety": _env_float("QA_ACQ_W_SAFETY", 0.20, 0.0, 1.0),
        "clinical_risk": _env_float("QA_ACQ_W_CLINICAL_RISK", 0.10, 0.0, 1.0),
        "diagnostic": _env_float("QA_ACQ_W_DIAGNOSTIC", 0.05, 0.0, 1.0),
    }


def acquisition_settings() -> dict:
    """本模块当前**实际生效**的全部旋钮（回执里原样带上 → 判定可复算）。"""
    return {
        "planner_version": ACQUISITION_PLANNER_VERSION,
        "utility_version": ACQUISITION_UTILITY_VERSION,
        "routes_version": ACQUISITION_ROUTES_VERSION,
        "action_schema_version": ACQUISITION_ACTION_SCHEMA_VERSION,
        "inquiry_version": PATIENT_INQUIRY_VERSION,
        "no_repeat_version": CONTEXT_GAP_NO_REPEAT_VERSION,
        "utility_floor": utility_floor(),
        "max_user_burden": max_user_burden(),
        "route_fit_step": route_fit_step(),
        "route_fit_floor": route_fit_floor(),
        "epsilon": utility_epsilon(),
        "weights": utility_weights(),
        "ask_patient_enabled": ask_patient_enabled(),
        "safety_priority_floor": SAFETY_PRIORITY_FLOOR,
    }


# ── 小工具（确定性）────────────────────────────────────────────────────────
def _clamp(value, low: float = 0.0, high: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low
    if number != number:  # NaN
        return low
    return max(low, min(high, number))


def _digest(value, length: int = 16) -> str:
    body = value if isinstance(value, str) else repr(value)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:length]


def _mapping(value) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _counter(values: Iterable) -> dict:
    out: dict = {}
    for value in values or ():
        key = str(value or "")
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda item: (-item[1], item[0])))


def _gap_type(gap: Mapping) -> str:
    return str((gap or {}).get("missing") or "")


def _gap_claim(gap: Mapping) -> dict:
    claim = (gap or {}).get("claim")
    return dict(claim) if isinstance(claim, Mapping) else {}


def action_kind(action: str) -> str:
    """动作二值（`interaction` 只有 `ask_patient`；与契约的推导视图机器对齐）。"""
    return "interaction" if str(action) in ACQUISITION_INTERACTION_ACTIONS else "retrieval"


def action_route(action: str) -> str:
    """P14-03：动作 → 检索通道（空串 = 本部署没有这条通道；**绝不发明通道值**）。"""
    value = str(ACQUISITION_ACTION_ROUTES.get(str(action)) or "")
    return value if value in QA_RETRIEVAL_ROUTES else ""


def action_source(action: str) -> str:
    """P14-03：动作 → 来源（KB/EMR/DB/图/Web/患者）。"""
    value = str(ACQUISITION_ACTION_SOURCE.get(str(action)) or "")
    return value if value in ACQUISITION_SOURCES else ""


def action_hunters(action: str) -> list:
    return list(ACQUISITION_ACTION_HUNTERS.get(str(action)) or ())


def deployment_sources(*, needs_web: bool = False, has_graph: bool = False,
                       has_emr: bool = False, has_db: bool = True, has_kb: bool = True,
                       ask_patient: bool | None = None) -> dict:
    """P14-03：本部署**实际可用的来源**（KB/EMR/DB/Web/图/患者）。

    默认值按本仓库的真实能力：KB 与 DB（政策登记表）在；EMR 需要部署方注入
    `structured_providers`（默认没有）；图谱与 Web 由本轮计划决定；患者通道默认关。
    """
    patient = ask_patient_enabled() if ask_patient is None else bool(ask_patient)
    return {
        "kb": bool(has_kb), "emr": bool(has_emr), "db": bool(has_db),
        "graph": bool(has_graph), "web": bool(needs_web), "patient": bool(patient),
    }


# ── P14-02：ExpectedGapReduction 与动作效用 ────────────────────────────────
def route_fit(action: str, missing: str) -> dict:
    """动作对该缺口类型的**基础契合度**（0…1）+ 可以手算的推导过程。

    口径（**复用 Phase 07 的单一真源，不另造 gap→route 表**）：
      · `retrieve_document/graph_traverse/query_database/web_search` 的落点就是 Phase 01 的通道，
        于是"契合度"直接由 `qa_gap_analyzer.routes_for(missing)` 给出：命中第 i 个建议通道
        （0 起）→ `1 - step*i`，下限 `route_fit_floor()`；不在建议列表里 → 0（`NOT_APPLICABLE_TO_GAP`）。
      · `retrieve_emr` / `ask_patient` 没有检索通道可反查 → 用各自的能力表（`EMR_GAP_FIT` /
        `ASKABLE_GAP_FIT`）。
    """
    missing = str(missing or "")
    route = action_route(action)
    if missing not in QA_GAP_TYPES:
        # 未知缺口类型**不给兜底通道**：Phase 07 的 `routes_for` 会给 keyword 兜底（它要保证
        # 下一跳总有路可走），但"该拿什么"这件事上，一个不认识的缺口不该被当成关键词检索。
        return {"value": 0.0, "basis": "UNKNOWN_GAP_TYPE", "route": route,
                "route_index": -1, "routes": []}
    if route:
        routes = [str(item) for item in routes_for(missing)]
        if route in routes:
            index = routes.index(route)
            base = max(route_fit_floor(), 1.0 - route_fit_step() * index)
            return {"value": round(_clamp(base), 4), "basis": "GAP_ROUTE_RULES",
                    "route": route, "route_index": index, "routes": routes}
        return {"value": 0.0, "basis": "GAP_ROUTE_RULES", "route": route,
                "route_index": -1, "routes": routes}
    if str(action) == "retrieve_emr":
        value = float(EMR_GAP_FIT.get(missing, 0.0))
        return {"value": round(_clamp(value), 4), "basis": "EMR_GAP_FIT", "route": ""}
    if str(action) == "ask_patient":
        value = float(ASKABLE_GAP_FIT.get(missing, 0.0))
        return {"value": round(_clamp(value), 4), "basis": "ASKABLE_GAP_FIT", "route": ""}
    return {"value": 0.0, "basis": "UNKNOWN_ACTION", "route": route}


def severity_weight(missing: str) -> float:
    """缺口严重度 → 折减系数（`0.5 + 0.5 × §12 的基础严重度`，恒在 0.5…1.0）。"""
    return round(0.5 + 0.5 * _clamp(GAP_SEVERITY.get(str(missing), 0.5)), 4)


def expected_gap_reduction(action: str, gap: Mapping) -> dict:
    """P14-02：`ExpectedGapReduction(action, gap)` —— **纯函数、逐项可复算**。

    `EGR = route_fit(action, gap_type) × severity_weight(gap_type)`
    （缺口越严重，拿到它就越值钱；动作离 Phase 07 的首选通道越远，收益折减越多）。
    """
    missing = _gap_type(gap)
    fit = route_fit(action, missing)
    weight = severity_weight(missing)
    value = round(_clamp(float(fit["value"]) * weight), 4)
    return {
        "value": value,
        "factors": {
            "fit": float(fit["value"]), "fit_basis": str(fit["basis"]),
            "route": str(fit.get("route") or ""),
            "route_index": int(fit.get("route_index", -1)),
            "severity": round(_clamp(GAP_SEVERITY.get(missing, 0.0)), 4),
            "severity_weight": weight,
            "formula": "fit × (0.5 + 0.5 × severity)",
        },
    }


def clinical_weights(gap: Mapping) -> dict:
    """P14-06：§21/§22 的三个医疗侧权重（**全部由缺口自身的既有字段派生**，不新造判定）。

      · `clinical_risk`：缺口 claims 的 `safety_critical` 与优先级越高越接近 1；
      · `safety_weight`：缺口命中 Phase 07 的安全词表（`safety_critical=True`）时为 1，否则 0；
      · `diagnostic_discrimination`：**区分度** —— 该缺口能排除/区分多少种可能：
        反证型缺口（缺反证/矛盾）给 1.0（能证伪的最有区分度），实体/时间歧义给 0.6，
        纯"没证据"给 0.3（拿到也未必分得清）。
    """
    missing = _gap_type(gap)
    safety_critical = bool((gap or {}).get("safety_critical"))
    priority = _clamp((gap or {}).get("priority"))
    discrimination = {
        QA_GAP_MISSING_COUNTEREVIDENCE: 1.0, QA_GAP_CONTRADICTION: 1.0,
        QA_GAP_AMBIGUOUS_ENTITY: 0.6, QA_GAP_MISSING_ENTITY_LINK: 0.6,
        QA_GAP_MISSING_TIME_LINK: 0.6,
        QA_GAP_LOW_AUTHORITY: 0.5, QA_GAP_SINGLE_SOURCE: 0.5,
        QA_GAP_LOW_RELEVANCE: 0.4, QA_GAP_MISSING_CAUSAL_BRIDGE: 0.4,
        QA_GAP_NO_EVIDENCE: 0.3,
    }.get(missing, 0.3)
    return {
        "clinical_risk": round(_clamp(priority * (1.0 if safety_critical else 0.6)), 4),
        "safety_weight": 1.0 if safety_critical else 0.0,
        "diagnostic_discrimination": round(_clamp(discrimination), 4),
        "basis": {"safety_critical": safety_critical, "priority": priority, "gap_type": missing},
    }


def importance_of(gap: Mapping) -> dict:
    """§21 的 `Importance`：缺口优先级 + 医疗侧加分（权重可配、结果钳在 0…1）。

    `SafetyCritical=true` 时抬到 `SAFETY_PRIORITY_FLOOR`（与 Phase 07 的安全覆盖**同档**）。
    """
    weights = utility_weights()
    clinical = clinical_weights(gap)
    priority = _clamp((gap or {}).get("priority"))
    value = (weights["priority"] * priority
             + weights["safety"] * clinical["safety_weight"]
             + weights["clinical_risk"] * clinical["clinical_risk"]
             + weights["diagnostic"] * clinical["diagnostic_discrimination"])
    floored = False
    if clinical["safety_weight"] > 0 and value < SAFETY_PRIORITY_FLOOR:
        value = SAFETY_PRIORITY_FLOOR
        floored = True
    return {
        "value": round(_clamp(value), 4),
        "clinical": clinical,
        "safety_override": floored,
        "factors": {"priority": priority, "weights": weights},
    }


def action_user_burden(action: str, gap: Mapping) -> str:
    """动作的用户负担档：检索动作恒 `none`；问诊按缺口类型取档（§13 的 UserBurden）。"""
    if action_kind(action) != "interaction":
        return "none"
    return str(ASK_PATIENT_BURDEN_BY_GAP.get(_gap_type(gap)) or "medium")


def action_utility(action: str, gap: Mapping, *, burden: str = "",
                   safety_override: bool = False) -> dict:
    """P14-02/P14-06：`Utility = EGR × Importance × Reliability / (Latency + Cost + UserBurden)`。

    逐项写进 `factors`，**手算用例直接对着它复算**。
    §22 的 `safety_override=True`（安全关键缺口）时：问诊的 `user_burden` 项被压到该档的下界
    —— "不能因为 UserBurden 高就降低追问优先级"（架构 §22 原话）。
    """
    egr = expected_gap_reduction(action, gap)
    importance = importance_of(gap)
    source = action_source(action)
    reliability = float(SOURCE_RELIABILITY.get(source, 0.0))
    latency = float(ACTION_LATENCY.get(str(action), 0.5))
    cost = float(ACTION_COST.get(str(action), 0.5))
    label = str(burden or action_user_burden(action, gap))
    user_burden = float(BURDEN_COST.get(label, BURDEN_COST["medium"]))
    burden_floor_applied = False
    if safety_override and action_kind(action) == "interaction" and user_burden > BURDEN_COST["low"]:
        user_burden = BURDEN_COST["low"]
        burden_floor_applied = True
    denominator = max(utility_epsilon(), latency + cost + user_burden)
    numerator = float(egr["value"]) * float(importance["value"]) * reliability
    return {
        "utility": round(numerator / denominator, 6),
        "numerator": round(numerator, 6),
        "denominator": round(denominator, 6),
        "factors": {
            "expected_gap_reduction": float(egr["value"]),
            "egr_factors": egr["factors"],
            "importance": float(importance["value"]),
            "importance_factors": importance["factors"],
            "evidence_reliability": reliability,
            "source": source,
            "latency": latency, "cost": cost,
            "user_burden": user_burden, "user_burden_label": label,
            "formula": "EGR × Importance × Reliability / (Latency + Cost + UserBurden)",
            "safety_override": bool(safety_override),
            "burden_floor_applied": burden_floor_applied,
        },
        "clinical": importance["clinical"],
    }


# ── P14-04：ask-patient route（规则模板，零模型）─────────────────────────────
def register_inquiry_writer(name: str, fn: Callable) -> None:
    """注册"问诊文案生成器"（**可插拔注入点**）。

    本机硬约束禁止调用模型端点，所以：注册了也只有调用方显式指定才会用，
    未注册名/抛错/非法载荷一律**回落模板**并记账（三条失败路径都有用例）。
    """
    if not str(name or "").strip():
        raise ValueError("问诊生成器必须有名字")
    _INQUIRY_WRITERS[str(name)] = fn


def inquiry_writers() -> tuple:
    return tuple(sorted(_INQUIRY_WRITERS))


def _template_question(gap: Mapping) -> str:
    missing = _gap_type(gap)
    base = str(PATIENT_QUESTION_TEMPLATES.get(missing) or "").strip()
    if base:
        return base
    claim_text = str(_gap_claim(gap).get("text") or "").strip()
    if claim_text:
        return "还请补充与「%s」有关的一手事实（时间/对象/来源）。" % claim_text[:40]
    return "还请补充这条结论缺的一手事实（时间/对象/来源）。"


def patient_question_for(gap: Mapping, *, writer: str = "") -> dict:
    """P14-04：给一个缺口拼一条**具体**问诊问题（规则模板；可选注入生成器）。

    返回 `{question_text, fields, writer, fallback}`；`writer` 缺省空 = 只用模板。
    """
    fallback = ""
    text = ""
    used = "template"
    if writer:
        fn = _INQUIRY_WRITERS.get(str(writer))
        if fn is None:
            fallback = "UNKNOWN_INQUIRY_WRITER"
        else:
            try:
                payload = fn(dict(gap))
                candidate = str((payload or {}).get("question_text") or "") if isinstance(
                    payload, Mapping) else ""
                if candidate.strip():
                    text, used = candidate.strip(), str(writer)
                else:
                    fallback = "INVALID_INQUIRY_PAYLOAD"
            except Exception as exc:  # noqa: BLE001 —— 生成器坏掉绝不许影响问诊候选
                fallback = "%s: %s" % (type(exc).__name__, str(exc)[:80])
    if not text:
        text = _template_question(gap)
    return {
        "question_text": text,
        "fields": [str(item) for item in _gap_claim(gap).get("scope") or []][:6],
        "writer": used,
        "fallback": fallback,
    }


def patient_inquiry_candidate(gap: Mapping, *, permission_granted: bool = True,
                              asked_ids: Sequence[str] = (), suppressed_by: str = "",
                              writer: str = "") -> dict:
    """P14-04：一条问诊候选（`PATIENT_INQUIRY_SCHEMA` 形状）。

    **恒 `is_evidence=False` / `in_citation_map=False` / `requires_revalidation=True`**
    （MASTER_RULES 11）—— 患者说的是"用户陈述"，要变成证据仍必须过 Phase 03 的核验。
    权限位固定是 Phase 11 的 `patient_contact`（默认被拒）。
    """
    question = patient_question_for(gap, writer=writer)
    missing = _gap_type(gap)
    burden = str(ASK_PATIENT_BURDEN_BY_GAP.get(missing) or "medium")
    question_id = "PI" + _digest("%s|%s" % (str((gap or {}).get("gap_id") or ""),
                                            question["question_text"]), 18)
    asked_before = question_id in {str(item) for item in (asked_ids or ())}
    payload = {
        "inquiry_version": PATIENT_INQUIRY_VERSION,
        "inquiry_id": question_id,
        "slot_skill_id": str(SKILL_INTERACTION_SLOT.get("skill_id") or "patient_inquiry"),
        "skill_owner_phase": SKILL_INTERACTION_SLOT_OWNER,
        "gap_id": str((gap or {}).get("gap_id") or ""),
        "gap_type": missing if missing in QA_GAP_TYPES else "",
        "question_text": question["question_text"],
        "question_fields": question["fields"],
        "why": "缺口 %s（%s）：公开资料拿不到，只有当事方能提供（§6 的 Patient Inquiry Candidate）"
               % (str((gap or {}).get("gap_id") or ""), missing),
        "user_burden": burden if burden in BURDEN_ORDER else "medium",
        "requires_permission": "patient_contact",
        "permission_granted": bool(permission_granted),
        "is_evidence": False,
        "requires_revalidation": True,
        "in_citation_map": False,
        "asked_before": bool(asked_before),
        "suppressed_by": str(suppressed_by or ""),
        "writer": question["writer"],
    }
    if question.get("fallback"):
        payload["writer_fallback"] = question["fallback"]
    return payload


# ── P14-05：Context Gap no-repeat（§13 的关键收益）──────────────────────────
_CONTEXT_GAP_REASON_BY_ACTION = {
    "REPACK_CONTEXT": "EVIDENCE_ALREADY_IN_GRAPH",
    "EXPAND_EVIDENCE_SPAN": "SPAN_TRUNCATED_ONLY",
    "LOAD_COUNTEREVIDENCE": "COUNTEREVIDENCE_ALREADY_IN_GRAPH",
}
"""Phase 08 的 Context Gap 动作 → 本阶段的抑制理由码（**同一件事的两种说法**，不新造概念）。"""

_CONTEXT_GAP_TYPE_REASON = {
    "OMITTED_EVIDENCE": "EVIDENCE_ALREADY_IN_GRAPH",
    "TRUNCATED_SPAN": "SPAN_TRUNCATED_ONLY",
    "MISSING_COUNTEREVIDENCE": "COUNTEREVIDENCE_ALREADY_IN_GRAPH",
    "UNGROUNDED_CLAIM": "EVIDENCE_ALREADY_IN_PACK",
    "DUPLICATE_SECTION": "EVIDENCE_ALREADY_IN_PACK",
}
"""Phase 08 的 `context_gap_type` → 抑制理由码（按类型兜底，动作缺失时也能判）。"""

_ALREADY_HELD_GAP_TYPES = (
    QA_GAP_LOW_RELEVANCE, QA_GAP_SINGLE_SOURCE, QA_GAP_LOW_AUTHORITY,
    QA_GAP_MISSING_ENTITY_LINK, QA_GAP_MISSING_TIME_LINK, QA_GAP_MISSING_COUNTEREVIDENCE,
)
"""这些缺口类型**可能**是"证据已在图上、只是没被用上"（→ Context Gap，而非真缺事实）。"""


def context_gap_signal(gap: Mapping, *, context_gaps: Sequence[Mapping] = (),
                       acquired_evidence: Sequence[Mapping] = (),
                       resolved_claims: Sequence[str] = ()) -> dict:
    """P14-05：这个缺口到底是"真缺事实"还是"证据已在手上只是没进上下文"？

    两个判据（都用既有产物；**同时给出全部命中理由**，不只返回第一条）：
      ① **Phase 08 的回执**（最权威）：该 claim 上有 Context Gap → 给出对应理由码
         （`REPACK_CONTEXT`/`EXPAND_EVIDENCE_SPAN`/`LOAD_COUNTEREVIDENCE`；
         `LOAD_SKILL` 属 Phase 11 的能力缺口，**不**算"信息已在手上"，故不抑制）；
      ② **本轮的已获取证据池**（缺口环在上下文包成形之前就要做决定）：该缺口的 claim 文本
         在池子里有 ≥ `relevance_floor()` 的词面覆盖，且缺口类型属"可能已持有"那几类
         → 记 `EVIDENCE_ALREADY_IN_GRAPH`。这条是**保守**判据：只覆盖不判定语义。

    返回 `{reason, reasons, basis, context_action, context_actions, ...}`：
    `reason` 是主理由（回执按它逐条计数），`reasons` 是**全部**命中理由（可查全貌）。
    """
    missing = _gap_type(gap)
    claim_id = str((gap or {}).get("claim_id") or "")
    reasons: list = []
    actions: list = []
    basis = ""
    extra: dict = {}
    for item in context_gaps or ():
        if not isinstance(item, Mapping):
            continue
        if claim_id and str(item.get("claim_id") or "") != claim_id:
            continue
        action = str(item.get("action") or "")
        if action == "LOAD_SKILL":
            continue
        # **类型优先、动作兜底**：UNGROUNDED_CLAIM / DUPLICATE_SECTION 的动作也是 REPACK_CONTEXT，
        # 但它们的语义是"进了包却没绑定/重复占位"（EVIDENCE_ALREADY_IN_PACK），
        # 不是"被预算裁掉"（EVIDENCE_ALREADY_IN_GRAPH）—— 按动作判会把这两种混成一种。
        reason = (_CONTEXT_GAP_TYPE_REASON.get(str(item.get("context_gap_type") or ""))
                  or _CONTEXT_GAP_REASON_BY_ACTION.get(action) or "")
        if reason not in CONTEXT_GAP_NO_REPEAT_REASONS:
            continue
        if reason not in reasons:
            reasons.append(reason)
        context_action = action if action in CONTEXT_GAP_ACTIONS else "REPACK_CONTEXT"
        if context_action not in actions:
            actions.append(context_action)
        basis = basis or "PHASE08_CONTEXT_GAP"
    if not reasons and claim_id and missing == QA_GAP_CONTRADICTION \
            and claim_id in {str(item) for item in (resolved_claims or ())}:
        reasons.append("CONFLICT_IS_ADJUDICATED")
        actions.append("REPACK_CONTEXT")
        basis = "PHASE06_ADJUDICATED"
    if not reasons and missing in _ALREADY_HELD_GAP_TYPES:
        text = str(_gap_claim(gap).get("text") or "")
        if text and acquired_evidence:
            floor = relevance_floor()
            terms = term_set(text)
            best = 0.0
            for item in acquired_evidence:
                if not isinstance(item, Mapping):
                    continue
                try:
                    score = relevance_score(terms, item).get("relevance")
                except Exception:  # noqa: BLE001 —— 判据坏掉绝不许误抑制
                    continue
                best = max(best, float(score or 0.0))
            if best >= floor:
                reasons.append("EVIDENCE_ALREADY_IN_GRAPH")
                actions.append("REPACK_CONTEXT")
                basis = "ACQUIRED_POOL"
                extra = {"relevance": round(best, 4), "relevance_floor": floor}
    return {
        "reason": reasons[0] if reasons else "",
        "reasons": reasons,
        "basis": basis,
        "context_action": actions[0] if actions else "",
        "context_actions": actions,
        "gap_id": "",
        **extra,
    }


def context_gap_no_repeat(candidates: Sequence[Mapping], *, context_gaps: Sequence[Mapping] = (),
                          acquired_evidence: Sequence[Mapping] = (),
                          resolved_claims: Sequence[str] = (),
                          asked_ids: Sequence[str] = ()) -> tuple:
    """P14-05：把"证据已在手上"的缺口上的动作**拦下来**（返回 `(新候选, 回执)`）。

    拦谁（§6 + MASTER_RULES 13）：
      · **所有检索型动作**（再检索一遍拿不到新东西，只会烧预算）；
      · **`ask_patient`**（§13 原话：该重组上下文，而不是再次询问患者 —— 这是本阶段的
        "no-repeat" 核心：**不重复追问用户**）。
    保留谁：`REPACK_CONTEXT` / `EXPAND_EVIDENCE_SPAN` / `LOAD_COUNTEREVIDENCE`
    （Phase 08 冻结的 Context Gap 动作，记在回执的 `context_actions` 里，供上下文层执行）。
    """
    adjusted: list = []
    suppressed_by_reason: dict = {}
    reason_hits: dict = {}
    suppressed_actions: dict = {}
    kept_actions: dict = {}
    context_actions: list = []
    in_graph = in_pack = 0
    for candidate in candidates or ():
        if not isinstance(candidate, Mapping):
            continue
        item = dict(candidate)
        signal = context_gap_signal(item.get("gap") or {}, context_gaps=context_gaps,
                                    acquired_evidence=acquired_evidence,
                                    resolved_claims=resolved_claims)
        reason = str(signal.get("reason") or "")
        if reason:
            item["skip_reason"] = "CONTEXT_GAP_NO_REPEAT"
            item["decision"] = "skipped"
            item["context_gap"] = signal
            suppressed_by_reason[reason] = suppressed_by_reason.get(reason, 0) + 1
            for hit in signal.get("reasons") or [reason]:
                reason_hits[str(hit)] = reason_hits.get(str(hit), 0) + 1
            suppressed_actions[str(item.get("action") or "")] = \
                suppressed_actions.get(str(item.get("action") or ""), 0) + 1
            if reason == "EVIDENCE_ALREADY_IN_GRAPH":
                in_graph += 1
            elif reason == "EVIDENCE_ALREADY_IN_PACK":
                in_pack += 1
            for action in signal.get("context_actions") or [signal.get("context_action")]:
                action = str(action or "")
                if action and action not in context_actions:
                    context_actions.append(action)
        else:
            kept_actions[str(item.get("action") or "")] = \
                kept_actions.get(str(item.get("action") or ""), 0) + 1
        adjusted.append(item)
    # 问诊问题**同一轮内不重复问**（内容寻址的问诊 id；与 Phase 02 的 seen 同一思路）
    asked = {str(value) for value in (asked_ids or ())}
    for item in adjusted:
        if str(item.get("action") or "") != "ask_patient" or item.get("decision") == "skipped":
            continue
        inquiry = _mapping(item.get("inquiry"))
        if not inquiry:
            continue
        if str(inquiry.get("inquiry_id") or "") in asked:
            item["skip_reason"] = "PATIENT_QUESTION_ALREADY_ASKED"
            item["decision"] = "skipped"
            inquiry["asked_before"] = True
            item["inquiry"] = inquiry
            suppressed_by_reason["PATIENT_QUESTION_ALREADY_ASKED"] = \
                suppressed_by_reason.get("PATIENT_QUESTION_ALREADY_ASKED", 0) + 1
            suppressed_actions["ask_patient"] = suppressed_actions.get("ask_patient", 0) + 1
            kept_actions["ask_patient"] = max(0, kept_actions.get("ask_patient", 0) - 1)
    receipt = {
        "no_repeat_version": CONTEXT_GAP_NO_REPEAT_VERSION,
        "gaps": len({str(item.get("gap", {}).get("gap_id") or "")
                     for item in (candidates or ()) if isinstance(item, Mapping)}),
        "in_graph": in_graph, "in_pack": in_pack,
        "suppressed": sum(suppressed_by_reason.values()),
        "suppressed_by_reason": suppressed_by_reason,
        "reason_hits": reason_hits,
        "suppressed_actions": suppressed_actions,
        "kept_actions": kept_actions,
        "context_actions": context_actions,
    }
    return adjusted, receipt


# ── P14-01/P14-02/P14-03：候选与获取计划 ────────────────────────────────────
def _candidate(action: str, gap: Mapping, *, sources: Mapping, permissions: Mapping,
               asked_ids: Sequence[str], resolved_claims: Sequence[str],
               writer: str = "", hard_safety: bool = False) -> dict:
    """构造一条动作候选（**先算效用，再逐道闸门**；每个决策都带理由码）。

    `hard_safety=True`（P15-04 的**硬触发**：危险信号成组出现）时，**安全动作**
    （直接问当事者 / 查内部登记）不再吃"追问负担上限"与"效用下限"两道闸门 ——
    这正是 §22 那句"不能因为 UserBurden 高就降低追问优先级"的机器形态。
    权限、来源可用性、适用性三道闸门**照旧生效**（安全不能凭空变出权限或数据源）。
    """
    egr = expected_gap_reduction(action, gap)
    clinical = clinical_weights(gap)
    safety_critical = bool(clinical["safety_weight"] > 0)
    utility = action_utility(action, gap, safety_override=safety_critical or hard_safety)
    source = action_source(action)
    safety_action = bool(action_kind(action) == "interaction" or source in ("db", "emr"))
    bypass_cost_gates = bool(hard_safety and safety_action)
    item = {
        "action_version": ACQUISITION_ACTION_SCHEMA_VERSION,
        "action_id": "AC" + _digest("%s|%s" % (str((gap or {}).get("gap_id") or ""), action), 18),
        "action": action,
        "kind": action_kind(action),
        "source": source,
        "route": action_route(action),
        "hunters": action_hunters(action),
        "gap_id": str((gap or {}).get("gap_id") or ""),
        "gap_type": _gap_type(gap) if _gap_type(gap) in QA_GAP_TYPES else "",
        "decision": "selected",
        "skip_reason": "",
        "utility": float(utility["utility"]),
        "factors": utility["factors"],
        "clinical": clinical,
        "safety_critical": safety_critical,
        "hard_safety_trigger": bool(hard_safety),
        "cost_gates_bypassed": bypass_cost_gates,
        "expected_gap_reduction": float(egr["value"]),
        "gap": dict(gap) if isinstance(gap, Mapping) else {},
    }
    if action == "ask_patient":
        item["inquiry"] = patient_inquiry_candidate(
            gap, permission_granted=bool(permissions.get("patient_contact")),
            asked_ids=asked_ids, writer=writer)
    # 闸门（先判先返回；顺序固定 → 回执可复算）
    if not bool(sources.get(source)):
        item.update({"decision": "skipped", "skip_reason": "SOURCE_UNAVAILABLE"})
        return item
    if action_kind(action) == "interaction" and not bool(permissions.get("patient_contact")):
        item.update({"decision": "skipped", "skip_reason": "PERMISSION_DENIED"})
        return item
    if float(egr["value"]) <= 0.0:
        item.update({"decision": "skipped", "skip_reason": "NOT_APPLICABLE_TO_GAP"})
        return item
    if action_kind(action) == "interaction" and not bypass_cost_gates and \
            BURDEN_ORDER.index(str(item["factors"]["user_burden_label"])) > \
            BURDEN_ORDER.index(max_user_burden()):
        item.update({"decision": "skipped", "skip_reason": "OVER_USER_BURDEN"})
        return item
    if float(utility["utility"]) < utility_floor() and not bypass_cost_gates:
        item.update({"decision": "skipped", "skip_reason": "UTILITY_FLOOR"})
        return item
    return item


def plan_acquisition(gaps: Sequence[Mapping], *, sources: Mapping | None = None,
                     permissions: Mapping | None = None, context_gaps: Sequence[Mapping] = (),
                     acquired_evidence: Sequence[Mapping] = (), asked_ids: Sequence[str] = (),
                     resolved_claims: Sequence[str] = (), actions: Sequence[str] | None = None,
                     preferred_routes: Sequence[str] = (), writer: str = "",
                     max_gaps: int = 24) -> dict:
    """P14-01/P14-02/P14-03 主入口：**缺口 → 动作候选 → 效用 → 选择**（返回获取计划）。

    每一条候选都带：效用与逐项因子（可手算复算）、临床/安全权重、淘汰理由码；
    每个缺口**恰好选一条**动作（并列时按契约里的动作次序，保证同输入同输出）。
    P14-05 的抑制闸门在"选择"之前生效（`CONTEXT_GAP_NO_REPEAT` / 问诊不重复问）。

    `preferred_routes` 是**会话级通道偏好**（P14-07 的用户选择）：它只在**效用并列**时
    改变排序（`ROUTE_PREFERRED`），绝不把"效用更低的动作"硬抬成赢家 ——
    真正的通道次序改变发生在 `qa_pipeline._apply_preferred_routes`（缺口下一跳）。
    """
    sources_map = dict(sources or deployment_sources())
    permissions_map = {"patient_contact": False, "corpus_read": True, "graph_read": True,
                       "db_read": True, "emr_read": False, "web_access": True}
    permissions_map.update(dict(permissions or {}))
    action_list = [str(item) for item in (actions or ACQUISITION_ACTIONS)
                   if str(item) in ACQUISITION_ACTIONS]
    prefer = [str(item) for item in (preferred_routes or ()) if str(item) in QA_RETRIEVAL_ROUTES]

    def _rank_key(item: Mapping) -> tuple:
        route = str(item.get("route") or "")
        preferred = 1 if (route and route in prefer) else 0
        try:
            order = action_list.index(str(item.get("action")))
        except ValueError:
            order = len(action_list)
        return (-float(item.get("utility") or 0.0), -preferred, order)

    candidates: list = []
    hard_receipts: list = []
    # P15-04：安全硬触发（危险信号**成组**出现）。延迟导入：记忆安全模块依赖 qa_memory，
    # 模块级导入会把整条链拉进 qa_acquisition 的 import 图（本函数只在运行期用一次）。
    try:
        from qa_memory_safety import (hard_trigger_active, safety_enforcement_receipt,
                                      plan_safety_receipt)

        _hard_available = True
    except Exception:      # noqa: BLE001 —— 安全模块不可用时逐字回到 Phase 14 行为
        _hard_available = False
    no_repeat_total: dict = {"gaps": 0, "in_graph": 0, "in_pack": 0, "suppressed": 0,
                             "suppressed_by_reason": {}, "reason_hits": {},
                             "suppressed_actions": {}, "kept_actions": {},
                             "context_actions": []}

    def _merge_no_repeat(receipt: Mapping) -> None:
        receipt = receipt if isinstance(receipt, Mapping) else {}
        for key in ("gaps", "in_graph", "in_pack", "suppressed"):
            no_repeat_total[key] += int(receipt.get(key) or 0)
        for key in ("suppressed_by_reason", "reason_hits", "suppressed_actions", "kept_actions"):
            merged = dict(no_repeat_total[key])
            for name, count in _mapping(receipt.get(key)).items():
                merged[str(name)] = merged.get(str(name), 0) + int(count or 0)
            no_repeat_total[key] = merged
        for action in receipt.get("context_actions") or ():
            if str(action) and str(action) not in no_repeat_total["context_actions"]:
                no_repeat_total["context_actions"].append(str(action))
    for gap in list(gaps or ())[:max(1, int(max_gaps))]:
        if not isinstance(gap, Mapping):
            continue
        hard_safety = bool(_hard_available and hard_trigger_active(gap))
        made: list = []
        seen: set = set()
        for action in action_list:
            if action in seen:
                # 同一缺口被重复提出同一个动作 → 记 DUPLICATE_ACTION（幂等，不重复记候选）
                candidates.append({
                    "action_version": ACQUISITION_ACTION_SCHEMA_VERSION,
                    "action_id": "AC" + _digest("%s|%s|dup" % (
                        str(gap.get("gap_id") or ""), action), 18),
                    "action": action, "kind": action_kind(action), "source": action_source(action),
                    "route": action_route(action), "hunters": action_hunters(action),
                    "gap_id": str(gap.get("gap_id") or ""), "gap_type": _gap_type(gap),
                    "decision": "skipped", "skip_reason": "DUPLICATE_ACTION",
                    "utility": 0.0, "factors": {}, "clinical": {}, "safety_critical": False,
                    "gap": dict(gap),
                })
                continue
            seen.add(action)
            made.append(_candidate(action, gap, sources=sources_map, permissions=permissions_map,
                                   asked_ids=asked_ids, resolved_claims=resolved_claims,
                                   writer=writer, hard_safety=hard_safety))
        made, _no_repeat = context_gap_no_repeat(
            made, context_gaps=context_gaps, acquired_evidence=acquired_evidence,
            resolved_claims=resolved_claims, asked_ids=asked_ids)
        _merge_no_repeat(_no_repeat)
        # ── 选择：先按效用取赢家；P14-06 的安全规则可以在最后一步**真的把它换掉** ──
        # §22 原话："不能因为 UserBurden 高，就降低追问优先级" —— 落成机器可校验的规则：
        # 安全关键缺口上，如果"普通效用赢家"是一个**更慢/可靠性更低**的检索动作，而池子里
        # 有"直接问当事者 / 查内部登记"这类安全动作（且其效用不低于赢家的 `safety_ratio`），
        # 则按安全规则换成后者，并给前者记 `SAFETY_OVERRIDE`。
        viable = [item for item in made if item.get("decision") == "selected"]
        winner = None
        if viable:
            ranked = sorted(viable, key=_rank_key)
            winner = ranked[0]
            if bool(winner.get("safety_critical")):
                pool = [item for item in viable
                        if str(item.get("kind")) == "interaction"
                        or str(item.get("source")) in ("db", "emr")]
                if pool:
                    safety = sorted(pool, key=_rank_key)[0]
                    if safety is not winner and float(safety.get("utility") or 0.0) >= \
                            float(winner.get("utility") or 0.0) * safety_ratio():
                        winner["decision"] = "skipped"
                        winner["skip_reason"] = "SAFETY_OVERRIDE"
                        winner["factors"] = {**dict(winner.get("factors") or {}),
                                             "safety_override": True,
                                             "replaced_by": str(safety.get("action") or "")}
                        winner = safety
            # ── P15-04：**硬触发**（§22 的 Hard Safety Trigger）────────────────
            # §22 原话的结构是"硬触发 → 高风险缺口 → 优先获取关键安全证据"，并且
            # "不能因为 UserBurden 高就降低追问优先级"。与上面 P14-06 的软覆盖分开：
            #   · 软覆盖（一个安全词）：带 0.6 的效用比门槛（D-052），口径一个字不动；
            #   · 硬触发（危险信号**成组**）：不谈效用比、直接忽略延迟/成本/追问负担，
            #     并把忽略的三项逐条写进回执（`ignored_cost_terms`）。
            if hard_safety and _hard_available:
                pool = [item for item in viable
                        if str(item.get("kind")) == "interaction"
                        or str(item.get("source")) in ("db", "emr")]
                safety_pick = sorted(pool, key=_rank_key)[0] if pool else None
                overrode = False
                if safety_pick is None:
                    hard_receipts.append(safety_enforcement_receipt(
                        gap, winner_before=str(winner.get("action") or ""),
                        reason="NO_SAFETY_ACTION",
                        basis={"note": "危险信号成组，但本轮没有可用的安全动作（来源/权限/适用性拦下）"}))
                elif safety_pick is winner:
                    hard_receipts.append(safety_enforcement_receipt(
                        gap, winner_before=str(winner.get("action") or ""),
                        winner_after=str(safety_pick.get("action") or ""),
                        reason="ALREADY_SAFETY_ACTION",
                        basis={"note": "普通效用赢家本身就是安全动作，无需覆盖"}))
                else:
                    overrode = True
                    winner_before = str(winner.get("action") or "")
                    winner["decision"] = "skipped"
                    winner["skip_reason"] = "SAFETY_OVERRIDE"
                    winner["factors"] = {**dict(winner.get("factors") or {}),
                                         "safety_override": True,
                                         "safety_override_kind": "HARD_SAFETY_TRIGGER",
                                         "replaced_by": str(safety_pick.get("action") or "")}
                    winner = safety_pick
                    hard_receipts.append(safety_enforcement_receipt(
                        gap, winner_before=winner_before,
                        winner_after=str(winner.get("action") or ""),
                        reason="DANGER_SIGN_COMBINATION", overrode_utility=True,
                        basis={"note": "§22：安全规则压过普通效用；忽略延迟/成本/追问负担",
                               "winner_utility": float(safety_pick.get("utility") or 0.0)}))
            for loser in ranked:
                if loser is winner or loser.get("decision") != "selected":
                    continue
                loser["decision"] = "skipped"
                loser["skip_reason"] = "LOWER_UTILITY"
        if winner is None and made:
            # 全被闸门拦下：如实记"没有可用动作"（不许硬凑一个）
            made[0]["no_action_reason"] = "ALL_CANDIDATES_SKIPPED"
        candidates.extend(made)
    selected: list = []
    by_source: dict = {}
    selected_gaps: dict = {}
    for item in candidates:
        if item.get("decision") != "selected":
            continue
        action = str(item.get("action") or "")
        if action not in selected:
            selected.append(action)
        source = str(item.get("source") or "")
        by_source[source] = by_source.get(source, 0) + 1
        selected_gaps[str(item.get("gap_id") or "")] = action
    skipped = _counter(item.get("skip_reason") for item in candidates
                       if item.get("decision") == "skipped")
    plan = {
        "planner_version": ACQUISITION_PLANNER_VERSION,
        "utility_version": ACQUISITION_UTILITY_VERSION,
        "routes_version": ACQUISITION_ROUTES_VERSION,
        "candidates": candidates,
        "selected": selected,
        "selected_gaps": selected_gaps,
        "by_source": by_source,
        "skipped": skipped,
        "no_repeat": {**no_repeat_total, "no_repeat_version": CONTEXT_GAP_NO_REPEAT_VERSION},
        "sources": {str(key): bool(value) for key, value in sources_map.items()},
        "permissions": {str(key): bool(value) for key, value in permissions_map.items()},
        "clinical_factors": list(ACQUISITION_CLINICAL_FACTORS),
        "utility_factors": list(ACQUISITION_UTILITY_FACTORS),
        "preferred_routes": list(prefer),
        "settings": acquisition_settings(),
        # P15-04：硬触发回执（没有硬触发时为空字典 → 一个键都不新增）
        "safety_enforcement": plan_safety_receipt(hard_receipts) if _hard_available else {},
        "stats": {
            "gaps": len({str(item.get("gap_id") or "") for item in candidates}),
            "candidates": len(candidates),
            "selected": len([item for item in candidates if item.get("decision") == "selected"]),
            "skipped": len([item for item in candidates if item.get("decision") == "skipped"]),
            "actions": len(ACQUISITION_ACTIONS),
            "safety_critical": len([item for item in candidates if item.get("safety_critical")]),
            "safety_overrides": int(skipped.get("SAFETY_OVERRIDE") or 0),
            "no_repeat_suppressed": int(skipped.get("CONTEXT_GAP_NO_REPEAT") or 0),
            "inquiries": len([item for item in candidates
                              if str(item.get("action")) == "ask_patient"
                              and item.get("decision") == "selected"]),
            # P15-04：安全硬触发的两个计数（为 0 时也如实给 0，便于验收对比）
            "hard_safety_triggers": len([row for row in hard_receipts
                                         if row.get("trigger") == "HARD_SAFETY_TRIGGER"]),
            "hard_safety_overrides": len([row for row in hard_receipts
                                          if row.get("overrode_utility")]),
        },
    }
    ok, note = validate_contract("acquisition_plan", dict(plan))
    plan["contract_ok"] = bool(ok)
    if not ok:
        plan["contract_error"] = note
    return plan


def route_guidance_from(plan: Mapping) -> dict:
    """从获取计划里取**通道偏好**（P14-03 的落地形态之一，喂给缺口下一跳规划）。

    只取"被选中的检索型动作"的通道（`ask_patient`/EMR 没有通道值，不进这份偏好）。
    返回的形状与 Phase 12 的经验指导**同一套**（`prefer_routes`/`avoid_routes`），
    于是复用同一条 `qa_memory_experience.apply_route_guidance` —— 通道次序真的会变。
    """
    plan = plan if isinstance(plan, Mapping) else {}
    prefer: list = []
    for item in plan.get("candidates") or ():
        if not isinstance(item, Mapping) or item.get("decision") != "selected":
            continue
        route = str(item.get("route") or "")
        if route and route in QA_RETRIEVAL_ROUTES and route not in prefer:
            prefer.append(route)
    return {"prefer_routes": prefer, "avoid_routes": [], "source": ACQUISITION_PLANNER_VERSION}


def acquisition_receipt(plan: Mapping, *, no_repeat: Mapping | None = None,
                        guidance: Mapping | None = None, config: Mapping | None = None,
                        error: str = "") -> dict:
    """给 `stats` 的**兄弟键**用的精简回执（不含缺口正文，只有计数/理由码/生效旋钮）。"""
    plan = plan if isinstance(plan, Mapping) else {}
    stats = _mapping(plan.get("stats"))
    inquiries: list = []
    for item in plan.get("candidates") or ():
        if not isinstance(item, Mapping) or item.get("decision") != "selected":
            continue
        if str(item.get("action")) != "ask_patient":
            continue
        inquiry = _mapping(item.get("inquiry"))
        if not inquiry:
            continue
        inquiries.append({
            "inquiry_id": str(inquiry.get("inquiry_id") or ""),
            "gap_id": str(inquiry.get("gap_id") or ""),
            "gap_type": str(inquiry.get("gap_type") or ""),
            "question_text": str(inquiry.get("question_text") or ""),
            "user_burden": str(inquiry.get("user_burden") or ""),
            "is_evidence": False,
            "in_citation_map": False,
        })
    return {
        "planner_version": str(plan.get("planner_version") or ACQUISITION_PLANNER_VERSION),
        "selected": list(plan.get("selected") or []),
        "by_source": dict(plan.get("by_source") or {}),
        "skipped": dict(plan.get("skipped") or {}),
        "sources": dict(plan.get("sources") or {}),
        "inquiries": inquiries,
        "stats": stats,
        "no_repeat": dict(no_repeat or plan.get("no_repeat") or {}),
        "route_guidance": dict(guidance or {}),
        # P15-04：安全硬触发回执（空字典 = 本轮没有硬触发，老回执键一字不差）
        "safety_enforcement": dict(plan.get("safety_enforcement") or {}),
        "route_guidance": dict(guidance or {}),
        "session_config": dict(config or {}),
        "settings": dict(plan.get("settings") or acquisition_settings()),
        "contract_ok": bool(plan.get("contract_ok")),
        "error": str(error or ""),
    }


# ── P14-07 的落地：会话级配置 → **真的改变下一次运行** ─────────────────────
def apply_session_config(plan: Mapping, config: Mapping, *,
                         plan_like: bool = True) -> tuple:
    """把 P14-07 的会话级可执行配置作用到本轮的检索计划上（返回 `(新计划, 回执)`）。

    配置键（`REALIGNMENT_CONFIG_KEYS`）逐个落到**既有旋钮**上：
      · `route_preference`：写进 `plan["route_preference"]`（P14-03 的通道偏好 → 缺口下一跳）；
      · `time_window`：写进 `plan["time_window_adjustment"]`（**既有**的窗口调整真源，
        `qa_retrieval._window_from_adjustment` 会把它变成硬过滤的区间）；
      · `authority_floor` / `relevance_floor`：写进 `plan["gap_floors"]`（缺口判据的覆盖值，
        `qa_gap_analyzer.detect_gaps(floors=...)` 读它）；
      · `analysis_lens`：写进 `plan["category"]`（Phase 05/07 的问题类别真源，缺口语义：
        事件→causal、趋势→comparison、风险→multi_hop）。
    只写非空键；每一项都记进回执 `applied`（**可复算"这次为什么换了思路"**）。
    **时间窗是"只补空缺"**：计划里已经有本轮明确的时间范围（`time_window_adjustment`）时，
    会话配置的时间窗被跳过并记 `skipped_keys=["time_window"]` —— 绝不覆盖用户本轮说的话
    （Phase 10-3 的既有口径）。
    """
    from qa_realignment import LENS_CATEGORY, config_keys, normalize_config

    normalized = normalize_config(config)
    out = dict(plan) if isinstance(plan, Mapping) else {}
    applied: dict = {}
    skipped: list = []
    if "route_preference" in normalized:
        out["route_preference"] = list(normalized["route_preference"] or [])
        applied["route_preference"] = list(out["route_preference"])
    if "time_window" in normalized:
        existing = out.get("time_window_adjustment")
        if isinstance(existing, Mapping) and existing:
            skipped.append("time_window")
        else:
            out["time_window_adjustment"] = dict(normalized["time_window"] or {})
            applied["time_window"] = dict(out["time_window_adjustment"])
    floors: dict = dict(out.get("gap_floors") or {})
    if "authority_floor" in normalized:
        floors["authority"] = float(normalized["authority_floor"])
        applied["authority_floor"] = floors["authority"]
    if "relevance_floor" in normalized:
        floors["relevance"] = float(normalized["relevance_floor"])
        applied["relevance_floor"] = floors["relevance"]
    if floors:
        out["gap_floors"] = floors
    if "analysis_lens" in normalized:
        lens = str(normalized["analysis_lens"])
        category = LENS_CATEGORY.get(lens, "")
        if category:
            # `plan["category"]` 既有两种形状（str 与 {"key": ...}），保持调用方原来的形状
            current = out.get("category")
            out["category"] = ({"key": category, "source": "realignment_lens",
                                "previous": str(current.get("key") if isinstance(current, Mapping)
                                                else current or "")}
                               if isinstance(current, Mapping) else category)
            out["analysis_lens"] = lens
            applied["analysis_lens"] = lens
    receipt = {
        "config_keys": list(config_keys()),
        "requested": dict(normalized),
        "applied": applied,
        "changed": sorted(applied),
        "changed_count": len(applied),
        "skipped_keys": skipped,
        "plan_like": bool(plan_like),
    }
    return out, receipt


__all__ = [
    "ACTION_COST",
    "ACTION_LATENCY",
    "ASKABLE_GAP_FIT",
    "ASK_PATIENT_BURDEN_BY_GAP",
    "BURDEN_COST",
    "BURDEN_ORDER",
    "DEFAULT_MAX_USER_BURDEN",
    "EMR_GAP_FIT",
    "PATIENT_QUESTION_TEMPLATES",
    "SOURCE_RELIABILITY",
    "acquisition_enabled",
    "acquisition_receipt",
    "acquisition_settings",
    "action_hunters",
    "action_kind",
    "action_route",
    "action_source",
    "action_user_burden",
    "action_utility",
    "apply_session_config",
    "ask_patient_enabled",
    "clinical_weights",
    "context_gap_no_repeat",
    "context_gap_signal",
    "deployment_sources",
    "expected_gap_reduction",
    "importance_of",
    "inquiry_writers",
    "max_user_burden",
    "patient_inquiry_candidate",
    "patient_question_for",
    "plan_acquisition",
    "register_inquiry_writer",
    "route_fit",
    "route_guidance_from",
    "severity_weight",
    "utility_floor",
    "utility_weights",
]

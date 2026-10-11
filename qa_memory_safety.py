#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 15 · 记忆安全与作用域隔离（P15-01…P15-06）。

把规格书 §14（记忆边界）、§21/§22（安全优先级）、§15/§17（可回放）与
MASTER_RULES 第 11/12/17/18 条落成**纯规则、可复算、零模型**的判定与回执。
命名一律按 D-002 的中性映射（医疗词不出现在变量/理由码/正文里）。

六个子任务各自的**唯一真源**（不另造第二套口径）：

  · P15-01 作用域强制：读 `qa_graph_contracts` 的 `SCOPE_BREADTH` ×
    `CONTENT_KIND_MAX_BREADTH`（派生视图 `SCOPE_ALLOWED_CONTENT_KINDS`）。判定
    **只允许不变或变严**：越界先尝试收窄，收窄不了才拒收（丢数据是最后手段）。
  · P15-02 轮次隔离：给 `qa_memory.scope_key` 的 `ENCOUNTER` 维加显式轮次
    （可选参数，默认空 = 逐字回到 Phase 09 的键），并给出**逐条理由码**的隔离回执。
  · P15-03 长期时效：**直接调** `qa_memory_revalidation.freshness_gate`（P10 的唯一闸门），
    本模块只补两条时间规则（长期窗口 / 尚未生效）。
  · P15-04 安全硬触发：复用 Phase 07 的安全词表与 `SAFETY_OVERRIDE_PRIORITY`、
    Phase 14 的动作候选与安全动作池；只补"危险信号**组合**"这一档，
    命中即**忽略成本/延迟/追问负担**（§22 原话）。
  · P15-05 日志最小化：复用 `qa_security.redact_sensitive/sanitize_log_text` 与
    `qa_memory.sensitive_hits`；补值级标识符最小化与"正文只留指纹"的日志政策，
    并给出 `leaks == []` 的可断言回执。
  · P15-06 审计与回放：事件落**既有** `qa_audit_events`（复用 `QaAuditLogger`，零迁移），
    回放链 = 执行 → 证据 → 上下文 → 记忆四层，逐层校验引用闭环并**重放比对**。

硬约束（与 Phase 03~14 一致）：零模型调用、零嵌入端点、零新依赖、零库表迁移；
所有判定同输入同输出；任何异常都收敛成回执，绝不把出答案的链路打断。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Mapping, Sequence

from qa_graph_contracts import (
    ACQUISITION_ACTIONS,
    AUDIT_REPLAY_LAYERS,
    CONTENT_KIND_MAX_BREADTH,
    CROSS_ENCOUNTER_POLICIES,
    ENCOUNTER_ISOLATION_VERSION,
    LONGITUDINAL_FRESHNESS_VERSION,
    MEMORY_AUDIT_EVENT_KINDS,
    MEMORY_AUDIT_REPLAY_VERSION,
    MEMORY_FRESHNESS_DECISIONS,
    MEMORY_FRESHNESS_REASONS,
    MEMORY_SAFETY_VERSION,
    MEMORY_SCOPES,
    MEMORY_TYPES,
    PRIVACY_MINIMAL_LOG_VERSION,
    PRIVACY_MINIMAL_RULES,
    SAFETY_CRITICAL_OVERRIDE_VERSION,
    SAFETY_OVERRIDE_PRIORITY,
    SAFETY_TRIGGER_KINDS,
    SAFETY_TRIGGER_REASONS,
    SCOPE_BREADTH,
    SCOPE_CONTENT_KINDS,
    SCOPE_ENFORCEMENT_REASONS,
    SCOPE_ENFORCEMENT_VERSION,
    validate as validate_contract,
)
from qa_memory import (
    canonicalize,
    content_fingerprint,
    scope_key,
    sensitive_hits,
    visible_scope_keys,
)
from qa_security import SENSITIVE_KEYS, sanitize_log_text


def _now_dt(value=None):
    """统一时钟（与 Phase 09/10 同口径）：传字符串/日期就用它，否则取当前 UTC。"""
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return datetime.now(timezone.utc)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _counter(values, *, top: int = 0) -> dict:
    counts: dict = {}
    for value in values or ():
        key = str(value or "")
        counts[key] = counts.get(key, 0) + 1
    ordered = dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
    return dict(list(ordered.items())[:top]) if top else ordered


# ── 旋钮（全部可回滚；默认值写在常量里便于复算）──────────────────────────────

DEFAULT_LONGITUDINAL_WINDOW_DAYS = 365.0
"""长期（跨会话主体）记忆的时效窗口（天）。

标定口径：`qa_memory.HALF_LIFE_DAYS["MEDIUM"] = 365`，即"产品/参数说明"级别的半衰期。
§14 只说"长期病史可 PATIENT_LONGITUDINAL 但使用时检查时间"，没给天数；取**一档半衰期**
作为窗口是这套代码里已有的时间尺度，不另造一套数字。窗口内的长期记忆照旧走 Phase 10
闸门；超出窗口即使闸门判 ALLOW 也要复验（P15-03 的唯一新增规则）。"""

DEFAULT_DANGER_SIGN_MIN_TERMS = 2
"""硬触发所需的**不同**危险信号个数（§22 的例子是"胸痛 + 呼吸困难 + 冷汗"三个凑一起）。

取 2 的理由：单个安全词是 Phase 14 的软覆盖（有 0.6 的效用比门槛，防"沾一个安全词就
无视全部效用"，见 D-052）；**组合**才是 §22 的"Hard Safety Trigger"。把两者分开，
既不动 D-052 的口径，又让"危险信号成组出现"这一档真的压过成本与追问负担。"""

DEFAULT_REPLAY_MAX_ROWS = 500
DEFAULT_LOG_TEXT_LIMIT = 240

# §14 的"当前状态"词面（**规则判定，不是语义判定**；能力边界写在 `classify_content_kind`）
DEFAULT_CURRENT_STATE_TERMS: tuple = (
    "当前状态", "症状", "生命体征", "体温", "血压", "心率", "呼吸频率", "主诉",
    "正在服用", "当前用药", "用药状态", "实时状态", "此刻", "本次发作",
)
"""刻意**不**收"用药"/"剂量"/"过敏"这类单字级医疗词：它们在规范/说明文本里同样常见，
收进来会把普通知识误判成当前状态（实测口径见验收报告的能力边界一节）。"""

CURRENT_STATE_CLAIM_TYPES: tuple = ("current_fact", "metric", "price", "status")
KNOWLEDGE_CLAIM_TYPES: tuple = ("policy", "regulation", "background", "definition")
ENVIRONMENT_MEMORY_TYPES: tuple = (
    "EPISODIC_RESEARCH", "STRATEGY", "FAILURE", "QUERY_PATTERN", "SOURCE", "SKILL_PERFORMANCE",
)
"""Phase 12 的六类经验记忆：它们是**本部署检索环境**的知识（语料 + 检索配置），
升格成 GLOBAL_KNOWLEDGE 就是污染（§14 把"医院规则"限在 ORGANIZATION 是同一个道理）。"""

RULE_MEMORY_TYPES: tuple = ("USER_APPROVED_DOMAIN_RULE",)


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


def _env_terms(name: str, default: tuple) -> tuple:
    raw = str(os.environ.get(name, "") or "")
    extra = tuple(item.strip() for item in raw.split(",") if item.strip())
    return tuple(dict.fromkeys(tuple(default) + extra))


def scope_enforcement_enabled() -> bool:
    """P15-01 开关（`QA_MEMORY_SCOPE_ENFORCEMENT`，**默认开**）。

    默认开的方向是安全的：判定只可能"不变或变严"，所以打开不会让任何内容获得
    比原来更大的可见范围；关掉它即逐字回到 Phase 09 的写门/召回行为。
    """
    return _env_flag("QA_MEMORY_SCOPE_ENFORCEMENT", True)


def encounter_isolation_enabled() -> bool:
    """P15-02 开关（`QA_ENCOUNTER_ISOLATION`，默认开）：跨轮次/跨会话的记忆不进命中。"""
    return _env_flag("QA_ENCOUNTER_ISOLATION", True)


def longitudinal_freshness_enabled() -> bool:
    """P15-03 开关（`QA_LONGITUDINAL_FRESHNESS`，默认开）。"""
    return _env_flag("QA_LONGITUDINAL_FRESHNESS", True)


def safety_hard_trigger_enabled() -> bool:
    """P15-04 开关（`QA_SAFETY_HARD_TRIGGER`，默认开）：危险信号组合 → 忽略成本/负担。"""
    return _env_flag("QA_SAFETY_HARD_TRIGGER", True)


def privacy_minimal_enabled() -> bool:
    """P15-05 开关（`QA_PRIVACY_MINIMAL_LOG`，默认开）。"""
    return _env_flag("QA_PRIVACY_MINIMAL_LOG", True)


def audit_enabled() -> bool:
    """P15-06 开关（`QA_MEMORY_AUDIT`，默认开）：记忆安全事件落既有审计表。"""
    return _env_flag("QA_MEMORY_AUDIT", True)


def longitudinal_window_days() -> float:
    return _env_float("QA_LONGITUDINAL_WINDOW_DAYS", DEFAULT_LONGITUDINAL_WINDOW_DAYS, 1.0, 36500.0)


def danger_sign_min_terms() -> int:
    return _env_int("QA_SAFETY_MIN_DANGER_SIGNS", DEFAULT_DANGER_SIGN_MIN_TERMS, 1, 8)


def replay_max_rows() -> int:
    return _env_int("QA_AUDIT_REPLAY_MAX_ROWS", DEFAULT_REPLAY_MAX_ROWS, 1, 5000)


def log_text_limit() -> int:
    return _env_int("QA_PRIVACY_LOG_TEXT_LIMIT", DEFAULT_LOG_TEXT_LIMIT, 16, 4000)


def current_state_terms() -> tuple:
    return _env_terms("QA_MEMORY_STATE_TERMS", DEFAULT_CURRENT_STATE_TERMS)


def memory_safety_settings() -> dict:
    """一次运行的全部旋钮（回执里带一份，保证"同一份设置同一个结论"可复算）。"""
    return {
        "memory_safety_version": MEMORY_SAFETY_VERSION,
        "scope_enforcement": scope_enforcement_enabled(),
        "encounter_isolation": encounter_isolation_enabled(),
        "longitudinal_freshness": longitudinal_freshness_enabled(),
        "safety_hard_trigger": safety_hard_trigger_enabled(),
        "privacy_minimal_log": privacy_minimal_enabled(),
        "audit": audit_enabled(),
        "longitudinal_window_days": longitudinal_window_days(),
        "danger_sign_min_terms": danger_sign_min_terms(),
        "safety_override_priority": SAFETY_OVERRIDE_PRIORITY,
        "replay_max_rows": replay_max_rows(),
        "log_text_limit": log_text_limit(),
    }


# ── P15-01：作用域强制（scope enforcement）──────────────────────────────────

def classify_content_kind(candidate: Mapping) -> dict:
    """判定一条候选记忆的**内容种类**（P15-01 的横轴）。纯规则、先判先返回。

    顺序（写死 → 同输入同输出）：显式声明 → 经验类类型 → 规则类类型 → 时效档
    （`SESSION`/`ENCOUNTER_BOUND` 就是本仓库对"当前状态"的既有定义，§10）→
    claim 类型 → 正文词面（`DEFAULT_CURRENT_STATE_TERMS`）→ 结论/实体类默认"主体事实"
    → 其余"通用知识"。

    **能力边界（如实标注）**：正文词面是**词面判定**，不是语义判定 —— 本轮不许调模型，
    所以"这句话是不是在描述当前状态"只能靠词表。词表刻意只收"当前状态"才用的词
    （症状/生命体征/正在服用…），不收"用药""剂量""过敏"这类在规范文本里同样常见的词，
    以免把普通知识误判成当前状态。每一条判定都把依据写进 `basis`，可逐条复算。
    """
    candidate = candidate if isinstance(candidate, Mapping) else {}
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), Mapping) else {}
    explicit = str(candidate.get("content_kind") or metadata.get("content_kind")
                   or "").strip().upper()
    if explicit in SCOPE_CONTENT_KINDS:
        return {"content_kind": explicit, "basis": {"source": "explicit"}}
    basis = {"memory_type": str(candidate.get("memory_type") or "").strip().upper()}
    memory_type = basis["memory_type"]
    if memory_type in ENVIRONMENT_MEMORY_TYPES:
        return {"content_kind": "ENVIRONMENT", "basis": {**basis, "source": "memory_type"}}
    if memory_type in RULE_MEMORY_TYPES:
        return {"content_kind": "RULE", "basis": {**basis, "source": "memory_type"}}
    freshness = str(candidate.get("freshness_class") or "").strip().upper()
    basis["freshness_class"] = freshness
    if freshness in ("SESSION", "ENCOUNTER_BOUND"):
        return {"content_kind": "CURRENT_STATE",
                "basis": {**basis, "source": "freshness_class"}}
    claim_type = str(candidate.get("claim_type") or metadata.get("claim_type")
                     or "").strip().casefold()
    basis["claim_type"] = claim_type
    if claim_type in CURRENT_STATE_CLAIM_TYPES:
        return {"content_kind": "CURRENT_STATE", "basis": {**basis, "source": "claim_type"}}
    if claim_type in KNOWLEDGE_CLAIM_TYPES:
        return {"content_kind": "KNOWLEDGE", "basis": {**basis, "source": "claim_type"}}
    text = canonicalize(candidate.get("canonical_content"))
    hits = [term for term in current_state_terms() if term and term in text]
    basis["state_terms"] = hits
    if hits:
        return {"content_kind": "CURRENT_STATE", "basis": {**basis, "source": "state_terms"}}
    # 知识类回退：**直接读 Phase 09 的时效判档**（LONG / VERSION_SENSITIVE 就是这套代码里
    # "长效知识"的既有定义：定义/定理/历史沿革 与 指南/规范/条例/政策/标准）。
    # 复用同一张规则表，而不是在这里再写一张"什么算知识"的词表（单一真源）。
    try:
        from qa_memory import classify_freshness

        graded = classify_freshness(claim_type=claim_type, text=text)
    except Exception:  # noqa: BLE001
        graded = ""
    basis["freshness_grading"] = graded
    if graded in ("LONG", "VERSION_SENSITIVE"):
        return {"content_kind": "KNOWLEDGE", "basis": {**basis, "source": "freshness_rules"}}
    if memory_type in ("VERIFIED_CLAIM", "ENTITY", "MEMORY_ITEM"):
        return {"content_kind": "SUBJECT_FACT",
                "basis": {**basis, "source": "memory_type_default"}}
    return {"content_kind": "KNOWLEDGE", "basis": {**basis, "source": "default"}}


def scope_binding(*, owner_user_id="", session_id="", industry_pack_id="",
                  encounter_id="", run_id="") -> dict:
    """作用域的**绑定轴**（§14 的"作用域主体"）：作用域键能不能唯一确定一个边界，只看这几项。"""
    return {
        "owner_user_id": str(owner_user_id or ""),
        "session_id": str(session_id or ""),
        "industry_pack_id": str(industry_pack_id or ""),
        "encounter_id": str(encounter_id or ""),
        "run_id": str(run_id or ""),
    }


def _scope_binding_ok(scope: str, binding: Mapping) -> tuple:
    """该作用域在给定绑定下**能不能唯一确定边界**；不能就给出对应的拒收理由码。

    口径（§14）：会话内/轮次内的内容如果绑不到会话或轮次，键就会退化成"大家共用一格"，
    那正是"当前状态被当成跨会话事实"的入口 → 拒收；主体长期记忆绑不到主体同理。
    组织规则绑不到组织只退化成"本部署"（同一个组织内部，不是跨主体泄漏）→ 放行但记账。
    """
    binding = binding if isinstance(binding, Mapping) else {}
    session = str(binding.get("session_id") or "")
    encounter = str(binding.get("encounter_id") or "")
    pack = str(binding.get("industry_pack_id") or "")
    if scope == "SESSION":
        return (bool(session), "SESSION_SCOPE_NEEDS_SESSION")
    if scope == "ENCOUNTER":
        return (bool(session or encounter), "ENCOUNTER_SCOPE_NEEDS_ENCOUNTER")
    if scope == "PATIENT_LONGITUDINAL":
        return (bool(pack), "SUBJECT_SCOPE_NEEDS_SUBJECT")
    return (True, "")


def _narrow_target(content_kind: str, current: str, binding: Mapping) -> str:
    """在**更严**的作用域里挑一个"该类内容允许且绑定齐备"的目标（从宽到窄取第一个）。

    只挑 `< 当前广度` 的：判定要么不变、要么变严，绝不放宽（§14 的安全方向）。
    """
    cap = CONTENT_KIND_MAX_BREADTH.get(content_kind, 4)
    current_breadth = SCOPE_BREADTH.get(current, 0)
    choices = [name for name in MEMORY_SCOPES
               if SCOPE_BREADTH.get(name, 0) <= cap
               and SCOPE_BREADTH.get(name, 0) < current_breadth]
    for name in sorted(choices, key=lambda item: -SCOPE_BREADTH.get(item, 0)):
        if _scope_binding_ok(name, binding)[0]:
            return name
    return ""


def enforce_scope(candidate: Mapping, *, scope: str = "", owner_user_id=None, session_id=None,
                  industry_pack_id=None, encounter_id=None) -> dict:
    """P15-01 的唯一判定点：一条记忆能不能落在它声明的作用域上。

    返回回执（过 `scope_enforcement` 契约）：`decision` ∈
    `ALLOW`（原样）/ `NARROW`（收窄到更严的作用域）/ `REJECT`（拒收）。
    判定顺序写死（先判先返回）：
      ① 作用域缺失 / 不在五值内 → 拒收；
      ② 声明了 `scope_key` 但与"作用域 + 绑定轴"重算的结果不一致 → 拒收
         （键是作用域隔离的物理载体，手写的键一律不可信）；
      ③ 内容种类与作用域：广度不越界 → 放行；越界 → 找更严的目标；
      ④ 目标作用域的绑定轴齐备 → 收窄；不齐备 → 拒收（丢数据是最后手段）。
    """
    candidate = candidate if isinstance(candidate, Mapping) else {}
    declared = str(scope or candidate.get("scope") or "").strip()
    binding = scope_binding(
        owner_user_id=candidate.get("owner_user_id") if owner_user_id is None else owner_user_id,
        session_id=candidate.get("session_id") if session_id is None else session_id,
        industry_pack_id=(candidate.get("industry_pack_id") if industry_pack_id is None
                          else industry_pack_id),
        encounter_id=(candidate.get("encounter_id") if encounter_id is None else encounter_id),
        run_id=candidate.get("run_id"),
    )
    kind = classify_content_kind(candidate)
    receipt = {
        "enforcement_version": SCOPE_ENFORCEMENT_VERSION,
        "memory_id": str(candidate.get("memory_id") or ""),
        "memory_type": str(candidate.get("memory_type") or "") if str(
            candidate.get("memory_type") or "") in MEMORY_TYPES else "",
        "scope": declared if declared in MEMORY_SCOPES else "",
        "scope_key": str(candidate.get("scope_key") or ""),
        "expected_scope_key": "",
        "content_kind": kind["content_kind"],
        "decision": "REJECT",
        "reason": "SCOPE_MISSING",
        "narrowed_from": "",
        "narrowed_to": "",
        "binding": dict(binding),
        "allowed_kinds": [
            name for name, cap in CONTENT_KIND_MAX_BREADTH.items()
            if declared in SCOPE_BREADTH and cap >= SCOPE_BREADTH.get(declared, -1)],
        "basis": {"content_kind": kind["basis"], "rule": ""},
    }

    def _done(decision: str, reason: str, rule: str) -> dict:
        receipt["decision"] = decision
        receipt["reason"] = reason
        receipt["basis"]["rule"] = rule
        receipt["basis"]["breadth"] = SCOPE_BREADTH.get(declared, None)
        receipt["basis"]["content_cap"] = CONTENT_KIND_MAX_BREADTH.get(kind["content_kind"], None)
        receipt["basis"].setdefault("legacy_key", False)
        ok, note = validate_contract("scope_enforcement", dict(receipt))
        receipt["contract_ok"] = bool(ok)
        if not ok:
            receipt["contract_error"] = note
        return receipt

    if not declared:
        return _done("REJECT", "SCOPE_MISSING", "作用域缺失：没有作用域就没有隔离边界")
    if declared not in MEMORY_SCOPES:
        return _done("REJECT", "SCOPE_UNKNOWN", "作用域取值不在 §14 的五值内")
    expected = scope_key(declared, owner_user_id=binding["owner_user_id"],
                         session_id=binding["session_id"],
                         industry_pack_id=binding["industry_pack_id"],
                         encounter_id=binding["encounter_id"])
    # 兼容适配器（MASTER_RULES 8）：P15 之前写入的 `ENCOUNTER` 记忆用的是"会话级轮次桶"键
    # （没有轮次维）。那是**同一会话**内的合法历史形态，接受它并如实标注 `legacy_key`；
    # 手写别的键仍然一律拒收（键是隔离的物理载体，不接受任何第三种形状）。
    legacy_key = ""
    legacy_matched = False
    if declared == "ENCOUNTER":
        legacy_key = scope_key(declared, owner_user_id=binding["owner_user_id"],
                              session_id=binding["session_id"],
                              industry_pack_id=binding["industry_pack_id"])
        legacy_matched = bool(legacy_key) and legacy_key != expected
    receipt["expected_scope_key"] = expected
    if receipt["scope_key"] and receipt["scope_key"] != expected:
        if legacy_matched and receipt["scope_key"] == legacy_key:
            receipt["basis"]["legacy_key"] = True
        else:
            return _done("REJECT", "SCOPE_KEY_MISMATCH",
                         "声明的作用域键与'作用域+绑定轴'重算的结果不一致（键是隔离的物理载体）")
    breadth = SCOPE_BREADTH.get(declared, 0)
    cap = CONTENT_KIND_MAX_BREADTH.get(kind["content_kind"], 0)
    if breadth <= cap:
        ok_binding, _ = _scope_binding_ok(declared, binding)
        if not ok_binding:
            reason = _scope_binding_ok(declared, binding)[1]
            return _done("REJECT", reason, "绑定轴缺失：作用域键退化，隔离边界不成立")
        if declared == "ORGANIZATION" and not binding["owner_user_id"]:
            return _done("ALLOW", "ORGANIZATION_BINDING_DEGRADED",
                         "组织绑定缺失：只退化到本部署可见（同组织内部，不是跨主体泄漏）")
        return _done("ALLOW", "SCOPE_AND_CONTENT_ALIGNED", "内容种类与作用域广度相容")
    target = _narrow_target(kind["content_kind"], declared, binding)
    if not target:
        return _done("REJECT", "SCOPE_CONTENT_MISMATCH",
                     "越界且找不到绑定齐备的更严作用域（收窄不了就不许落库）")
    receipt["narrowed_from"] = declared
    receipt["narrowed_to"] = target
    if kind["content_kind"] == "CURRENT_STATE" and target == "ENCOUNTER":
        return _done("NARROW", "CURRENT_STATE_NARROWED_TO_ENCOUNTER",
                     "§14：当前状态不得无条件跨轮次/跨会话当事实")
    return _done("NARROW", "CONTENT_KIND_NARROWED_TO_ALLOWED_SCOPE",
                 "该内容种类不允许这么宽的作用域 → 收到它允许的最宽作用域")


def apply_scope_enforcement(candidate: Mapping, **axes) -> tuple:
    """给写门用的包装：返回 `(调整后的候选, 回执)`。

    · 开关关 → `(原候选副本, {})`（一个字段都不动，逐字回到 Phase 09 行为）；
    · `ALLOW` → 候选不动（但回执仍返回，写门要留痕）；
    · `NARROW` → **只改 `scope` 与随之重算的 `scope_key`**，其余字段一个不动；
    · `REJECT` → 候选不动，由写门把它转成一条带理由码的 DROP 决策（可审计）。
    """
    candidate = dict(candidate) if isinstance(candidate, Mapping) else {}
    if not scope_enforcement_enabled():
        return candidate, {}
    receipt = enforce_scope(candidate, **axes)
    if receipt.get("decision") == "NARROW":
        target = str(receipt.get("narrowed_to") or "")
        candidate["scope"] = target
        if candidate.get("scope_key"):
            # 只有本来就有键的候选才重算键（没有键的候选由写门按新作用域生成）
            candidate["scope_key"] = scope_key(
                target, owner_user_id=str(candidate.get("owner_user_id") or ""),
                session_id=str(candidate.get("session_id") or ""),
                industry_pack_id=str(candidate.get("industry_pack_id") or ""),
                encounter_id=str(candidate.get("encounter_id") or ""))
        metadata = dict(candidate.get("metadata") or {})
        metadata["scope_enforcement"] = {
            "decision": receipt.get("decision"), "reason": receipt.get("reason"),
            "narrowed_from": receipt.get("narrowed_from"), "narrowed_to": target,
            "content_kind": receipt.get("content_kind"),
        }
        candidate["metadata"] = metadata
    return candidate, receipt


def audit_items_scope(items: Sequence[Mapping], **axes) -> dict:
    """召回侧的**防御性复核**：既有记忆行（可能由本阶段之前写入或由别的工具写入）
    逐条重算作用域判定，越界的行**隔离出命中**并记账（不是删库、不是改库）。

    这条复核的意义：键是历史数据，规则是现在的规则 —— 只要两者不一致，就说明
    数据是在旧规则（或被人为构造）下写进去的，不能当正常命中用。
    """
    rows = [item for item in (items or []) if isinstance(item, Mapping)]
    violations: list = []
    quarantined: list = []
    receipts: list = []
    for item in rows:
        receipt = enforce_scope(item, **axes)
        receipts.append(receipt)
        if receipt.get("decision") == "REJECT":
            violations.append({
                "memory_id": str(item.get("memory_id") or ""),
                "content_fingerprint": str(item.get("content_fingerprint") or ""),
                "scope": str(item.get("scope") or ""),
                "reason": receipt.get("reason"),
                "content_kind": receipt.get("content_kind"),
                "declared_scope_key": str(item.get("scope_key") or ""),
                "expected_scope_key": receipt.get("expected_scope_key"),
            })
            quarantined.append(str(item.get("memory_id") or ""))
    return {
        "enforcement_version": SCOPE_ENFORCEMENT_VERSION,
        "checked": len(rows),
        "violations": violations,
        "quarantined": quarantined,
        "receipts": receipts,
        "by_reason": _counter(row["reason"] for row in violations),
        "decisions": _counter(row.get("decision") for row in receipts),
        "narrowed": len([row for row in receipts if row.get("decision") == "NARROW"]),
        "settings": {"scope_enforcement": scope_enforcement_enabled()},
    }


# ── P15-02：轮次隔离（encounter isolation）──────────────────────────────────

_SCOPE_BY_KEY_PREFIX = (
    ("global", "GLOBAL_KNOWLEDGE"), ("org:", "ORGANIZATION"), ("subject:", "PATIENT_LONGITUDINAL"),
    ("encounter:", "ENCOUNTER"), ("session:", "SESSION"),
)


def scope_of_item(item: Mapping) -> str:
    """取一条记忆的作用域：优先 `scope` 字段，缺失/脏值时**从作用域键前缀反推**。

    反推是必要的：隔离判定的输入是"库里的行"，而行的作用域键才是真正决定可见性的东西；
    两者不一致时以键为准（键是物理隔离载体），并把不一致如实暴露给调用方。
    """
    item = item if isinstance(item, Mapping) else {}
    scope = str(item.get("scope") or "").strip()
    if scope in MEMORY_SCOPES:
        return scope
    key = str(item.get("scope_key") or "")
    for prefix, name in _SCOPE_BY_KEY_PREFIX:
        if key.startswith(prefix):
            return name
    return ""


def encounter_context(*, owner_user_id="", session_id="", industry_pack_id="",
                      encounter_id="", run_id="") -> dict:
    """本轮次的可见性上下文：五个作用域键 + 绑定轴（P15-02 的唯一输入形态）。

    **兼容适配器（MASTER_RULES 8）**：本阶段之前写入的 `ENCOUNTER` 作用域记忆用的键是
    "会话级轮次桶"（`encounter:<主体>:<会话>`，没有轮次维），所以可见集合里**同时**给出
    新键与老键 —— 老数据照旧可见（不回退它们的可见性），新写入才享受逐轮次隔离。
    两个键都记在回执里，运维一眼看得出"这条为什么可见"。
    """
    binding = scope_binding(owner_user_id=owner_user_id, session_id=session_id,
                            industry_pack_id=industry_pack_id, encounter_id=encounter_id,
                            run_id=run_id)
    keys = visible_scope_keys(owner_user_id=binding["owner_user_id"],
                              session_id=binding["session_id"],
                              industry_pack_id=binding["industry_pack_id"],
                              encounter_id=binding["encounter_id"])
    legacy = scope_key("ENCOUNTER", owner_user_id=binding["owner_user_id"],
                       session_id=binding["session_id"],
                       industry_pack_id=binding["industry_pack_id"])
    visible = list(keys)
    if legacy and legacy not in visible:
        visible.append(legacy)
    return {
        **binding,
        "visible_scope_keys": visible,
        "global_key": keys[0], "organization_key": keys[1], "subject_key": keys[2],
        "encounter_key": keys[3], "session_key": keys[4],
        "legacy_encounter_key": legacy,
    }


def isolation_verdict(item: Mapping, context: Mapping) -> dict:
    """一条记忆在给定轮次上下文里**可不可见**，以及为什么（逐条理由码）。

    判据（§14 + `CROSS_ENCOUNTER_POLICIES`）：
      · 通用知识：处处可见；
      · 组织规则：同一组织可见，换组织隔离；
      · 主体长期：同一主体**跨轮次可见**，但要 `REVALIDATE_ON_USE`（P15-03 复用）；
      · 轮次内：键不同就隔离（这是 §14 硬规则的落点 —— 上一次轮次的当前状态不是这一次的）；
      · 会话内：换会话一律隔离；
      · 未知作用域：一律隔离（不猜）。
    """
    item = item if isinstance(item, Mapping) else {}
    context = context if isinstance(context, Mapping) else {}
    scope = scope_of_item(item)
    key = str(item.get("scope_key") or "")
    same_encounter = bool(context.get("encounter_key")) and key == str(context.get("encounter_key") or "")
    legacy_bucket = bool(
        context.get("legacy_encounter_key")) and key == str(context.get("legacy_encounter_key") or "")
    same_session = bool(context.get("session_key")) and key == str(context.get("session_key") or "")
    same_subject = str(item.get("industry_pack_id") or "") == str(
        context.get("industry_pack_id") or "")
    same_org = str(item.get("owner_user_id") or "") == str(context.get("owner_user_id") or "")
    # "跨轮次"只针对主体长期记忆：它在本轮之前就写下了（别的会话/别的 run）
    cross_encounter = bool(
        scope == "PATIENT_LONGITUDINAL"
        and (str(item.get("session_id") or "") != str(context.get("session_id") or "")
             or str(item.get("created_from_run_id") or "") not in ("", str(context.get("run_id") or ""))))
    row = {
        "memory_id": str(item.get("memory_id") or ""),
        "content_fingerprint": str(item.get("content_fingerprint") or ""),
        "scope": scope,
        "scope_key": key,
        "memory_type": str(item.get("memory_type") or ""),
        "cross_encounter": cross_encounter,
    }
    if scope == "GLOBAL_KNOWLEDGE":
        return {**row, "visible": True, "reason": "GLOBAL", "policy": "ALLOW"}
    if scope == "ORGANIZATION":
        return {**row, "visible": same_org, "policy": "ALLOW" if same_org else "BLOCK",
                "reason": "SAME_ORGANIZATION" if same_org else "OTHER_ORGANIZATION"}
    if scope == "PATIENT_LONGITUDINAL":
        return {**row, "visible": same_subject, "policy": "REVALIDATE_ON_USE" if same_subject
                else "BLOCK", "reason": "SAME_SUBJECT" if same_subject else "OTHER_SUBJECT"}
    if scope == "ENCOUNTER":
        if same_encounter:
            return {**row, "visible": True, "policy": "ALLOW", "reason": "SAME_ENCOUNTER",
                    "legacy_bucket": False}
        if legacy_bucket:
            # 兼容适配器（MASTER_RULES 8）：本阶段之前写入的"会话级轮次桶"记忆仍然可见，
            # 但**不许当作当前事实直接用** —— 标 `REVALIDATE_ON_USE`（它到底属于哪一轮次
            # 键上没写），与"主体长期记忆跨轮次"同一处置：可见、但用前必须复验。
            return {**row, "visible": True, "policy": "REVALIDATE_ON_USE",
                    "reason": "SAME_ENCOUNTER", "legacy_bucket": True}
        return {**row, "visible": False, "policy": "BLOCK", "reason": "OTHER_ENCOUNTER",
                "legacy_bucket": False}
    if scope == "SESSION":
        return {**row, "visible": same_session, "policy": "ALLOW" if same_session else "BLOCK",
                "reason": "SAME_SESSION" if same_session else "OTHER_SESSION"}
    return {**row, "visible": False, "policy": "BLOCK", "reason": "OTHER_SESSION"}


def isolate(items: Sequence[Mapping], context: Mapping, *, enabled=None) -> dict:
    """P15-02 主入口：把一批记忆按轮次上下文分成"可见/被隔离"，并给出可复算回执。

    `isolated_refs` 只留**引用与指纹**（不留正文）：隔离回执本身也不能变成敏感信息的搬运工
    （MASTER_RULES 18，与 P15-05 同一口径）。
    `leaks` 是**防御性检查**：键在可见集合里、但按作用域轴不该可见的行 —— 那就是键与轴不一致
    （被人为构造或旧规则写入），必须显式报出来而不是静默放进命中。
    """
    if enabled is None:
        enabled = encounter_isolation_enabled()
    context = context if isinstance(context, Mapping) else encounter_context()
    rows = [item for item in (items or []) if isinstance(item, Mapping)]
    visible_keys = [str(key) for key in (context.get("visible_scope_keys") or [])]
    verdicts = [isolation_verdict(item, context) for item in rows]
    isolated = [row for row in verdicts if not row["visible"]]
    # 泄漏 = **键说可见、轴说不可见**：作用域键在可见集合里，但这行的绑定列
    # （主体/组织/会话）与上下文对不上 —— 键是被构造出来的，或行被改过。必须显式报出来。
    leaks = [row["memory_id"] for row, item in zip(verdicts, rows)
             if str((item or {}).get("scope_key") or "") in visible_keys and not row["visible"]]
    if not enabled:
        # 开关关：**不做隔离**，但如实记账"本来会被隔离多少条"（回滚口径可审计）
        isolated = []
    receipt = {
        "isolation_version": ENCOUNTER_ISOLATION_VERSION,
        "context": {key: context.get(key) for key in (
            "owner_user_id", "session_id", "industry_pack_id", "encounter_id", "run_id")},
        "visible_scope_keys": visible_keys,
        "checked": len(verdicts),
        "visible": len(verdicts) - len(isolated),
        "isolated": len(isolated),
        "isolated_by_reason": _counter(row["reason"] for row in isolated),
        "isolated_refs": [{key: row[key] for key in (
            "memory_id", "content_fingerprint", "scope", "reason", "policy")} for row in isolated],
        "cross_encounter": [{key: row[key] for key in (
            "memory_id", "content_fingerprint", "scope", "reason", "policy")}
            for row in verdicts if row["cross_encounter"] and row["visible"]],
        "leaks": leaks,
        "settings": {"encounter_isolation": bool(enabled),
                     "cross_encounter_policies": list(CROSS_ENCOUNTER_POLICIES)},
    }
    ok, note = validate_contract("encounter_isolation", dict(receipt))
    receipt["contract_ok"] = bool(ok)
    if not ok:
        receipt["contract_error"] = note
    return receipt


def isolated_memory_ids(receipt: Mapping) -> list:
    """从隔离回执里取被隔离的记忆 id（调用方过滤命中用；顺序与输入一致）。"""
    receipt = receipt if isinstance(receipt, Mapping) else {}
    return [str(row.get("memory_id") or "") for row in (receipt.get("isolated_refs") or [])
            if isinstance(row, Mapping)]


# ── P15-03：长期时效（longitudinal freshness）───────────────────────────────

LONGITUDINAL_SCOPES: tuple = ("PATIENT_LONGITUDINAL",)
"""需要"使用时检查时间"的作用域（§14："长期病史可 PATIENT_LONGITUDINAL 但使用时检查时间"）。"""


def _freshness_gate(item: Mapping, *, now=None, source_version_changed=None, high_stakes=None) -> dict:
    """调 Phase 10 的时效闸门（**唯一真源**；模块不可用时如实返回空闸门，不自己判一套）。"""
    try:
        from qa_memory_revalidation import freshness_gate

        return freshness_gate(item, now=now, source_version_changed=source_version_changed,
                              high_stakes=high_stakes)
    except Exception as exc:  # noqa: BLE001
        return {"gate_version": "", "decision": "REVALIDATE", "reason": "",
                "error": "%s: %s" % (type(exc).__name__, str(exc)[:120]), "age_days": 0.0}


def longitudinal_check(item: Mapping, *, now=None, source_version_changed=None,
                       high_stakes=None) -> dict:
    """P15-03 的单条结论：**透传** Phase 10 闸门，只在"长期"这一档补两条时间规则。

    新增的（其余理由码一律原样来自闸门，绝不另判一套）：
      · `VALID_FROM_IN_FUTURE`：生效时间还没到 → `BLOCK`（还没发生的事不能当当前事实用）；
      · `AGE_BEYOND_LONGITUDINAL_WINDOW`：闸门判 ALLOW 但年龄超长期窗口 → 抬到 `REVALIDATE`；
      · `GATE_UNAVAILABLE`：Phase 10 闸门不可用（模块缺席/抛错）→ 保守判 `REVALIDATE`
        （**绝不**在拿不到闸门结论时判 ALLOW）。
    非长期作用域：`longitudinal=False`、`reason="NOT_LONGITUDINAL"`，`decision` 仍是闸门结论
    （调用方可以一个函数走完所有作用域）。
    """
    item = item if isinstance(item, Mapping) else {}
    moment = _now_dt(now)
    gate = _freshness_gate(item, now=moment, source_version_changed=source_version_changed,
                           high_stakes=high_stakes)
    scope = scope_of_item(item)
    longitudinal = scope in LONGITUDINAL_SCOPES
    window = longitudinal_window_days()
    age = float(gate.get("age_days") or 0.0)
    gate_reason = str(gate.get("reason") or "")
    gate_ok = not gate.get("error") and gate_reason in MEMORY_FRESHNESS_REASONS
    decision = str(gate.get("decision") or "REVALIDATE") if gate_ok else "REVALIDATE"
    reason = gate_reason if gate_ok else "GATE_UNAVAILABLE"
    valid_from = _parse_time(item.get("valid_from"))
    extra_rule = ""
    if not longitudinal:
        reason = "NOT_LONGITUDINAL"
    elif valid_from is not None and valid_from > moment:
        decision, reason, extra_rule = "BLOCK", "VALID_FROM_IN_FUTURE", "valid_from_in_future"
    elif not gate_ok:
        extra_rule = "gate_unavailable"
    elif decision == "ALLOW" and age > window:
        decision, reason, extra_rule = ("REVALIDATE", "AGE_BEYOND_LONGITUDINAL_WINDOW",
                                        "age_beyond_longitudinal_window")
    receipt = {
        "longitudinal_version": LONGITUDINAL_FRESHNESS_VERSION,
        "memory_id": str(item.get("memory_id") or ""),
        "scope": scope,
        "longitudinal": bool(longitudinal),
        "decision": decision if decision in MEMORY_FRESHNESS_DECISIONS else "REVALIDATE",
        "gate_reason": gate_reason if gate_reason in MEMORY_FRESHNESS_REASONS else "",
        "reason": reason,
        "age_days": age,
        "window_days": window,
        "valid_from": str(item.get("valid_from") or ""),
        "gate": gate,
        "settings": {"longitudinal_window_days": window,
                     "longitudinal_freshness": longitudinal_freshness_enabled(),
                     "extra_rule": extra_rule},
    }
    if not longitudinal_freshness_enabled():
        # 开关关：仍然是闸门结论（Phase 10 的行为），但不加长期窗口这条
        if extra_rule:
            receipt["decision"] = str(gate.get("decision") or "REVALIDATE") if gate_ok \
                else "REVALIDATE"
            receipt["reason"] = gate_reason if gate_ok else "GATE_UNAVAILABLE"
            receipt["settings"]["extra_rule"] = ""
            receipt["settings"]["disabled_note"] = "长期窗口规则已关闭（QA_LONGITUDINAL_FRESHNESS=0）"
    ok, note = validate_contract("longitudinal_freshness", dict(receipt))
    receipt["contract_ok"] = bool(ok)
    if not ok:
        receipt["contract_error"] = note
    return receipt


def longitudinal_report(items: Sequence[Mapping], *, now=None, source_version_changed=None) -> dict:
    """一批记忆的长期时效分布（验收/运维用）：只统计，不改任何状态。"""
    rows = [item for item in (items or []) if isinstance(item, Mapping)]
    checks = [longitudinal_check(item, now=now, source_version_changed=source_version_changed)
              for item in rows]
    long_checks = [row for row in checks if row["longitudinal"]]
    return {
        "longitudinal_version": LONGITUDINAL_FRESHNESS_VERSION,
        "checked": len(checks),
        "longitudinal": len(long_checks),
        "decisions": _counter(row["decision"] for row in checks),
        "reasons": _counter(row["reason"] for row in checks),
        "longitudinal_decisions": _counter(row["decision"] for row in long_checks),
        "needs_revalidation": len([row for row in long_checks if row["decision"] == "REVALIDATE"]),
        "blocked": len([row for row in long_checks if row["decision"] == "BLOCK"]),
        "beyond_window": len([row for row in long_checks
                              if row["reason"] == "AGE_BEYOND_LONGITUDINAL_WINDOW"]),
        "not_yet_effective": len([row for row in long_checks
                                  if row["reason"] == "VALID_FROM_IN_FUTURE"]),
        "stale_rate": round(len([row for row in long_checks if row["decision"] != "ALLOW"])
                            / float(len(long_checks)), 6) if long_checks else None,
        "checks": checks,
        "settings": {"window_days": longitudinal_window_days()},
    }


# ── P15-04：安全硬触发（SafetyCritical override）─────────────────────────────

IGNORED_COST_TERMS: tuple = ("latency", "cost", "user_burden")
"""§22 硬触发时**被忽略的三项代价**：延迟、成本、追问负担。

列出来而不是假装它们不存在：回执里写清"这三项被安全规则压过了"，运维才知道
"为什么一个又慢又贵的动作被选中"（§22 原话：不能因为 UserBurden 高就降低追问优先级）。"""

SAFETY_ACTION_KINDS: tuple = ("interaction",)
SAFETY_ACTION_SOURCES: tuple = ("db", "emr")
"""安全动作池（与 Phase 14 的 `SAFETY_OVERRIDE` 用**同一个池子**：直接问当事者 / 查内部登记）。

刻意不在这里 import `qa_acquisition`（会成环）：池子的判据用"动作种类 + 来源"两组常量表达，
Phase 14 侧接线时按它自己的 `action_kind/action_source` 传进来，两边取值同源。"""


def safety_terms() -> tuple:
    """Phase 07 的安全词表（**唯一真源**；模块不可用时退化为空表，绝不自己编一套词）。"""
    try:
        from qa_gap_analyzer import safety_terms as phase07_terms

        return tuple(phase07_terms())
    except Exception:  # noqa: BLE001
        return ()


def _gap_haystack(gap: Mapping) -> str:
    """缺口的安全判定文本：与 Phase 07 同口径（claim 正文 + 其作用域词）。"""
    gap = gap if isinstance(gap, Mapping) else {}
    claim_text = str(gap.get("claim_text") or "")
    if not claim_text:
        claim = gap.get("claim") if isinstance(gap.get("claim"), Mapping) else {}
        claim_text = str(claim.get("text") or "")
    scope_words = " ".join(str(item) for item in (gap.get("scope") or []))
    if not scope_words:
        claim = gap.get("claim") if isinstance(gap.get("claim"), Mapping) else {}
        scope_words = " ".join(str(item) for item in (claim.get("scope") or []))
    return "%s %s" % (claim_text, scope_words)


def danger_signs(gap: Mapping) -> list:
    """命中的安全信号（**去重、按词表次序**，同输入同输出）。"""
    haystack = _gap_haystack(gap)
    return [term for term in safety_terms() if term and term in haystack]


def safety_trigger(gap: Mapping) -> dict:
    """§22 的安全触发判定（三档，纯规则）：

      · `HARD_SAFETY_TRIGGER`：**不同**安全信号个数 ≥ `danger_sign_min_terms()`（默认 2）——
        这正是 §22 例子里"胸痛 + 呼吸困难 + 冷汗"的形态：危险信号**成组**出现；
      · `SOFT_SAFETY_TRIGGER`：只命中一个（走 Phase 14 既有的、带 0.6 效用比门槛的覆盖，
        口径一个字不改，见 D-052）；
      · `NO_TRIGGER`：一个都没命中。
    """
    signs = danger_signs(gap)
    minimum = danger_sign_min_terms()
    if len(signs) >= minimum:
        return {"trigger": "HARD_SAFETY_TRIGGER", "reason": "DANGER_SIGN_COMBINATION",
                "danger_signs": signs, "sign_count": len(signs), "min_signs": minimum}
    if signs:
        return {"trigger": "SOFT_SAFETY_TRIGGER", "reason": "SINGLE_SAFETY_TERM",
                "danger_signs": signs, "sign_count": len(signs), "min_signs": minimum}
    return {"trigger": "NO_TRIGGER", "reason": "NO_SAFETY_TERM",
            "danger_signs": [], "sign_count": 0, "min_signs": minimum}


def hard_trigger_active(gap: Mapping, *, enabled=None) -> bool:
    """这个缺口是不是**硬触发**（写门/获取计划用它决定"要不要忽略代价三项"）。"""
    if enabled is None:
        enabled = safety_hard_trigger_enabled()
    return bool(enabled) and safety_trigger(gap)["trigger"] == "HARD_SAFETY_TRIGGER"


def safety_enforcement_receipt(gap: Mapping, *, winner_before: str = "", winner_after: str = "",
                               reason: str = "", overrode_utility: bool = False,
                               basis: Mapping | None = None) -> dict:
    """造一条硬触发回执（过 `safety_enforcement` 契约；`reason` 由调用方按实际分支给）。"""
    trigger = safety_trigger(gap)
    receipt = {
        "override_version": SAFETY_CRITICAL_OVERRIDE_VERSION,
        "gap_id": str((gap or {}).get("gap_id") or "") if isinstance(gap, Mapping) else "",
        "trigger": trigger["trigger"] if trigger["trigger"] in SAFETY_TRIGGER_KINDS
        else "NO_TRIGGER",
        "reason": reason if reason in SAFETY_TRIGGER_REASONS else trigger["reason"],
        "danger_signs": list(trigger["danger_signs"]),
        "sign_count": int(trigger["sign_count"]),
        "min_signs": int(trigger["min_signs"]),
        "ignored_cost_terms": list(IGNORED_COST_TERMS) if trigger["trigger"] == "HARD_SAFETY_TRIGGER"
        else [],
        "winner_before": winner_before if winner_before in ACQUISITION_ACTIONS else "",
        "winner_after": winner_after if winner_after in ACQUISITION_ACTIONS else "",
        "overrode_utility": bool(overrode_utility),
        "basis": dict(basis or {}),
    }
    ok, note = validate_contract("safety_enforcement", dict(receipt))
    receipt["contract_ok"] = bool(ok)
    if not ok:
        receipt["contract_error"] = note
    return receipt


def plan_safety_receipt(receipts: Sequence[Mapping]) -> dict:
    """把逐缺口的硬触发回执汇总成获取计划的**兄弟键**（`plan["safety_enforcement"]`）。

    只有真的发生硬触发（或尝试过）才有内容；一个都没触发就返回空字典，
    调用方据此**一个键都不新增**（回滚口径）。
    """
    rows = [row for row in (receipts or []) if isinstance(row, Mapping)
            and row.get("trigger") == "HARD_SAFETY_TRIGGER"]
    if not rows:
        return {}
    return {
        "override_version": SAFETY_CRITICAL_OVERRIDE_VERSION,
        "hard_triggers": len(rows),
        "overrides": len([row for row in rows if row.get("overrode_utility")]),
        "by_reason": _counter(row.get("reason") for row in rows),
        "danger_signs": _counter(sign for row in rows for sign in (row.get("danger_signs") or [])),
        "receipts": rows,
        "settings": {"safety_hard_trigger": safety_hard_trigger_enabled(),
                     "min_signs": danger_sign_min_terms(),
                     "ignored_cost_terms": list(IGNORED_COST_TERMS)},
    }


# ── P15-05：日志最小化（privacy-minimal logs）───────────────────────────────

CONTENT_LOG_KEYS = frozenset({
    "canonical_content", "content", "text", "body", "quote", "span_quote", "excerpt",
    "claim_text", "question_text", "answer", "final_answer", "draft_answer", "memory_content",
    "summary", "canonical", "passage", "note_text",
})
"""**正文类**字段名：进日志时只留"指纹 + 长度"，不留原文（MASTER_RULES 18）。

只按**字段名**判定（不猜内容）：调用方知道哪个字段是正文，系统不靠启发式猜
—— 猜错一次就是把敏感正文写进日志。"""

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"1[3-9]\d{9}")
_ID_RE = re.compile(r"\d{17}[\dXx]")
_LONG_DIGITS_RE = re.compile(r"\d{16,19}")
_SSN_RE = re.compile(r"\d{3}-\d{2}-\d{4}")
# 与 `qa_security` 的 `_BEARER_RE` / `_KEY_RE` **同一形状**（那边是行为真源，这边只用来
# 判定"是哪条规则命中的"，好让回执能逐条给出理由码）。
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_KEY_RE = re.compile(r"(?i)\b(api[_-]?key|token|password|secret|cookie)\s*[:=]\s*([^\s,;]{4,})")


def _minimize_text(text: str) -> tuple:
    """值级最小化：返回值与命中的规则集合（每条规则可单独验证）。"""
    raw = str(text or "")
    hits: set = set()
    if _BEARER_RE.search(raw):
        hits.add("BEARER_TOKEN")
    if _KEY_RE.search(raw):
        hits.add("KEY_VALUE_SECRET")
    body = sanitize_log_text(raw, limit=log_text_limit())
    # 顺序 = **长模式优先**：证件号/长数字串先于手机号。否则 17 位证件号里的 10 位数字
    # 会被手机号规则先吃掉，剩下的碎片既不像手机号也不像证件号（实测踩过这个坑）。
    for pattern, marker, rule in (
            (_EMAIL_RE, "[EMAIL]", "EMAIL"),
            (_ID_RE, "[IDENTIFIER]", "IDENTIFIER_RUN"),
            (_LONG_DIGITS_RE, "[IDENTIFIER]", "IDENTIFIER_RUN"),
            (_SSN_RE, "[IDENTIFIER]", "IDENTIFIER_RUN"),
            (_PHONE_RE, "[PHONE]", "PHONE")):
        body, count = pattern.subn(marker, body)
        if count:
            hits.add(rule)
    return body, hits


def minimize_value(value, *, key: str = "", depth: int = 0, limit: int | None = None) -> tuple:
    """递归最小化任意 JSON 形状的值，返回 `(最小化后的值, 规则命中计数)`。

    · 键名在 `qa_security.SENSITIVE_KEYS` → 整个值 `[REDACTED]`（复用既有清单）；
    · 键名是正文类 → 换成 `{fingerprint, length}`；
    · 字符串 → 值级规则（邮箱/手机号/证件号/长数字串/令牌）；
    · 超长 → 截断；深度/条数有上限（日志不该被一个畸形载荷撑爆）。
    """
    limit = log_text_limit() if limit is None else int(limit)
    counts: dict = {}

    def _bump(rule: str, amount: int = 1) -> None:
        counts[rule] = counts.get(rule, 0) + amount

    def _walk(node, name: str, level: int):
        if level > 6:
            _bump("TRUNCATED")
            return "[TRUNCATED_DEPTH]"
        if isinstance(node, Mapping):
            out = {}
            for index, (child_key, child_value) in enumerate(node.items()):
                if index >= 200:
                    _bump("TRUNCATED")
                    out["[TRUNCATED_ITEMS]"] = True
                    break
                if str(child_key).casefold() in SENSITIVE_KEYS:
                    # 密钥类键**整值脱敏**（复用 qa_security 的既有清单，嵌套层同样生效）
                    _bump("SECRET_KEY")
                    out[str(child_key)] = "[REDACTED]"
                    continue
                out[str(child_key)] = _walk(child_value, str(child_key), level + 1)
            return out
        if isinstance(node, (list, tuple)):
            return [_walk(item, name, level + 1) for item in list(node)[:200]]
        if isinstance(node, str):
            folded = name.casefold()
            if folded in CONTENT_LOG_KEYS:
                _bump("CONTENT_FINGERPRINTED")
                return {"fingerprint": content_fingerprint(node), "length": len(node)}
            body, hits = _minimize_text(node)
            for rule in hits:
                _bump(rule)
            if len(body) >= limit:
                body = body[:limit] + "…"
                _bump("TRUNCATED")
            return body
        return node

    if key and key.casefold() in SENSITIVE_KEYS:
        _bump("SECRET_KEY")
        return "[REDACTED]", counts
    return _walk(value, key, depth), counts


def minimize_log_payload(payload: Mapping, *, purpose: str = "") -> dict:
    """P15-05 主入口：把要进日志/审计的载荷压到"最小必要"，并给出**可断言的**回执。

    返回 `{"payload": …, "receipt": …}`；`receipt["leaks"]` 是**独立复算**的结果：
    对最小化后的载荷再扫一遍原始敏感串，任何一个还在就算泄漏（必须为空数组）。
    """
    original = payload if isinstance(payload, Mapping) else {}
    if not privacy_minimal_enabled():
        result = {"payload": dict(original), "receipt": {
            "policy_version": PRIVACY_MINIMAL_LOG_VERSION,
            "rule_hits": {}, "redacted_fields": 0, "fingerprinted_fields": 0,
            "leaks": [], "content_policy": "disabled", "purpose": str(purpose or ""),
            "settings": {"privacy_minimal_log": False}}}
        return result
    minimized, counts = minimize_value(original)
    leaks = verify_no_leak(original, minimized)
    receipt = {
        "policy_version": PRIVACY_MINIMAL_LOG_VERSION,
        "rule_hits": dict(sorted(counts.items())),
        "redacted_fields": sum(counts.get(rule, 0) for rule in ("SECRET_KEY", "BEARER_TOKEN",
                                                                "KEY_VALUE_SECRET")),
        "fingerprinted_fields": int(counts.get("CONTENT_FINGERPRINTED") or 0),
        "leaks": leaks,
        "content_policy": "字段名判定：正文类只留指纹与长度；密钥类整值脱敏；标识符值级替换",
        "purpose": str(purpose or ""),
        "settings": {"privacy_minimal_log": True, "log_text_limit": log_text_limit(),
                     "content_keys": sorted(CONTENT_LOG_KEYS)},
    }
    ok, note = validate_contract("privacy_minimal_log", dict(receipt))
    receipt["contract_ok"] = bool(ok)
    if not ok:
        receipt["contract_error"] = note
    return {"payload": minimized, "receipt": receipt}


def verify_no_leak(original, minimized) -> list:
    """独立复算：最小化后的载荷里**还能不能找到**原来的敏感串（返回找到的那些，应为空）。

    三条独立判据（任一条命中都算泄漏，宁可保守）：
      ① 原载荷里被 `qa_security` 认定为敏感键的值（长度 ≥4 的字符串）不该原样出现；
      ② 原载荷里任何正文类字段的**长片段**（≥12 字）不该原样出现；
      ③ 最小化结果里不该有 `qa_memory.sensitive_hits` 能命中的标识符串。
    """
    leaks: list = []
    rendered = json.dumps(minimized, ensure_ascii=False, sort_keys=True, default=str)
    secrets = _collect_sensitive_values(original, keys=SENSITIVE_KEYS)
    for value in secrets:
        text = str(value or "")
        if len(text) >= 4 and text in rendered:
            leaks.append("SECRET_VALUE:%s" % text[:4])
    for value in _collect_sensitive_values(original, keys=CONTENT_LOG_KEYS):
        text = str(value or "")
        if len(text) >= 12 and text[:12] in rendered:
            leaks.append("CONTENT_VALUE:%s…" % text[:6])
    if sensitive_hits(rendered):
        leaks.append("IDENTIFIER_RUN")
    return sorted(set(leaks))


def _collect_sensitive_values(node, *, keys, out=None):
    """收集"键名属于给定集合"的字符串值（递归；只用于泄漏复核，不搬进日志）。"""
    out = [] if out is None else out
    if isinstance(node, Mapping):
        for key, value in node.items():
            if str(key).casefold() in keys and isinstance(value, str):
                out.append(value)
            else:
                _collect_sensitive_values(value, keys=keys, out=out)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _collect_sensitive_values(item, keys=keys, out=out)
    return out


def audit_payload(payload: Mapping, *, purpose: str = "memory_safety") -> tuple:
    """审计留痕前的统一入口：**先最小化再落库**（返回 `(payload, receipt)`）。"""
    minimized = minimize_log_payload(payload, purpose=purpose)
    return minimized["payload"], minimized["receipt"]


# ── P15-06：审计与回放（audit/replay）───────────────────────────────────────

def _audit_logger(store):
    """拿审计写入器：**复用** Phase 06 的 `QaAuditLogger`（→ 既有 `qa_audit_events`，零迁移）。"""
    database = getattr(store, "database", None)
    if database is None:
        return None
    try:
        from qa_observability import QaAuditLogger

        return QaAuditLogger(database)
    except Exception:  # noqa: BLE001
        return None


def record_memory_audit(store, kind: str, payload: Mapping, *, run_id: str = "", trace_id: str = "",
                        actor_id: str = "", origin: str = "", industry_pack_id: str = "",
                        purpose: str = "memory_safety") -> dict:
    """落一条记忆安全审计事件（**载荷先过 P15-05 最小化**）。

    事件种类只用契约里的六值；未知种类一律记 `LOG_MINIMIZED` 而不是写一个自造字符串
    （审计表的 `event_type` 也就不会长出规格外的取值）。写入失败只记 `error`，绝不冒泡：
    审计不能把回答链路打断。
    """
    event = str(kind or "").upper()
    if event not in MEMORY_AUDIT_EVENT_KINDS:
        event = "LOG_MINIMIZED"
    minimized, receipt = audit_payload(payload, purpose=purpose)
    result = {"event_type": event, "payload": minimized, "privacy": receipt,
              "recorded": False, "error": ""}
    if not audit_enabled():
        result["error"] = "audit_disabled"
        return result
    logger = _audit_logger(store)
    if logger is None:
        result["error"] = "no_audit_sink"
        return result
    try:
        logger.record(event, trace_id=str(trace_id or run_id or ""), run_id=str(run_id or ""),
                      actor_id=str(actor_id or ""), origin=str(origin or "")[:30],
                      industry_pack_id=str(industry_pack_id or ""),
                      payload={**minimized, "privacy_policy": PRIVACY_MINIMAL_LOG_VERSION})
        result["recorded"] = True
    except Exception as exc:  # noqa: BLE001 —— 审计写入失败不许冒泡
        result["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:120])
    return result


def _stage_details(rows: Sequence[Mapping]) -> list:
    out = []
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        raw = row.get("details_json") or row.get("details") or {}
        details = raw if isinstance(raw, Mapping) else {}
        if isinstance(raw, str):
            try:
                details = json.loads(raw or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                details = {}
        out.append({"stage": str(row.get("stage") or ""), "node_id": str(row.get("node_id") or ""),
                    "status": str(row.get("status") or ""), "attempt": row.get("attempt"),
                    "round_index": row.get("round_index"), "details": details})
    return out


def replay_bundle(store, run_id: str, *, limit: int | None = None) -> dict:
    """P15-06 的四层回放链（§17："可 replay 任意回答：执行 → 证据 → 上下文 → 记忆"）。

    四层**全部读既有持久化产物**（零新表、零重跑检索、零模型调用）：
      · 执行层：`qa_stage_runs`（阶段/节点/状态/尝试/轮次）；
      · 证据层：`qa_storage.load_reasoning_graph()`（结论 / 结论-证据边 / 证据 / 冲突）；
      · 上下文层：`conflict_review` 阶段详情里的 `result.context_pack`（Phase 08 产物，
        随证据图一起免费持久化）；
      · 记忆层：`memory_write_decision`（本轮写了什么/为什么没写）+ 这些决策落下的
        memory_item 行 + 记忆↔证据绑定 + 召回日志。
    """
    store = store
    limit = replay_max_rows() if limit is None else int(limit)
    run_id = str(run_id or "")
    stages = _stage_details(store.stage_runs(run_id) if store is not None else [])
    graph = {}
    if store is not None:
        try:
            graph = store.load_reasoning_graph(run_id) or {}
        except Exception:  # noqa: BLE001
            graph = {}
    context_pack = {}
    for row in stages:
        details = row.get("details") if isinstance(row.get("details"), Mapping) else {}
        result = details.get("result") if isinstance(details.get("result"), Mapping) else {}
        candidate = result.get("context_pack")
        if isinstance(candidate, Mapping) and candidate:
            context_pack = candidate
    decisions: list = []
    if store is not None:
        try:
            decisions = store.memory_write_decisions(run_id=run_id, limit=limit) or []
        except Exception:  # noqa: BLE001
            decisions = []
    memory_ids = sorted({str(row.get("memory_id") or "") for row in decisions
                         if str(row.get("memory_id") or "")})
    items: list = []
    links: list = []
    if store is not None and memory_ids:
        try:
            items = store.load_memory_items(memory_ids=memory_ids, include_all_scopes=True,
                                            limit=limit) or []
        except Exception:  # noqa: BLE001
            items = []
        try:
            links = store.memory_evidence(memory_ids=memory_ids) or []
        except Exception:  # noqa: BLE001
            links = []
    evidence_refs = sorted({
        str(row.get("evidence_ref") or "")
        for row in (graph.get("evidence") or []) if isinstance(row, Mapping)}
        | {str(row.get("evidence_ref") or "")
           for row in (graph.get("edges") or []) if isinstance(row, Mapping)})
    citations = context_pack.get("citation_map") if isinstance(context_pack, Mapping) else {}
    return {
        "run_id": run_id,
        "layers": list(AUDIT_REPLAY_LAYERS),
        "execution": {"stages": stages, "count": len(stages),
                      "stage_names": sorted({row["stage"] for row in stages if row["stage"]})},
        "evidence": {"claims": list(graph.get("claims") or []),
                     "edges": list(graph.get("edges") or []),
                     "evidence": list(graph.get("evidence") or []),
                     "conflicts": list(graph.get("conflicts") or []),
                     "evidence_refs": evidence_refs,
                     "counts": {"claims": len(graph.get("claims") or []),
                                "edges": len(graph.get("edges") or []),
                                "evidence": len(graph.get("evidence") or []),
                                "conflicts": len(graph.get("conflicts") or [])}},
        "context": {"present": bool(context_pack),
                    "pack_id": str((context_pack or {}).get("pack_id") or ""),
                    "citations": (list(citations.values()) if isinstance(citations, Mapping)
                                  else list(citations or [])),
                    "citation_count": len(citations or []),
                    "sections": sorted((context_pack.get("sections") or {}).keys())
                    if isinstance(context_pack, Mapping) else []},
        "memory": {"decisions": decisions, "items": items, "links": links,
                   "memory_ids": memory_ids,
                   "counts": {"decisions": len(decisions), "items": len(items),
                              "links": len(links)}},
    }


def _evidence_refs_of_citation(citation) -> str:
    if isinstance(citation, Mapping):
        for key in ("evidence_ref", "ref", "evidence_id"):
            value = str(citation.get(key) or "")
            if value:
                return value
        return ""
    return str(citation or "")


def verify_replay(store, run_id: str, *, limit: int | None = None, bundle=None) -> dict:
    """回放校验：层间引用是否闭环 + 规则判定**重放是否复现**（P15-06 的可复算部分）。

    两个不同的东西，都必须真做：
      ① `hops`：四层之间的引用完整性（执行→证据→上下文→记忆→证据）；
      ② `replay_matches`：拿审计事件里存下的**输入**重跑同一批纯规则，比对当时记下的
         `decision`/`reason`。不一致就如实列进 `mismatches`（数据被改过、规则换过版本、
         或者审计载荷被截断 —— 三种都可能，报告只陈述事实，不猜原因）。
    """
    bundle = bundle if isinstance(bundle, Mapping) else replay_bundle(store, run_id, limit=limit)
    limit = replay_max_rows() if limit is None else int(limit)
    execution = bundle.get("execution") or {}
    evidence = bundle.get("evidence") or {}
    context = bundle.get("context") or {}
    memory = bundle.get("memory") or {}
    executed = set(execution.get("stage_names") or [])
    evidence_refs = set(evidence.get("evidence_refs") or [])
    hops: list = []

    # ① 执行 → 证据：证据/结论行必须来自本 run 真实执行过的阶段
    claim_stages = sorted({str(row.get("stage") or "") for row in (evidence.get("claims") or [])
                           if isinstance(row, Mapping)})
    missing_stages = [stage for stage in claim_stages if stage and stage not in executed]
    hops.append({"hop": "execution->evidence", "ok": not missing_stages,
                 "checked": len(claim_stages), "missing": missing_stages})

    # ② 证据 → 上下文：每条引用的证据必须在证据层里真的存在
    context_missing = []
    for citation in (context.get("citations") or []):
        ref = _evidence_refs_of_citation(citation)
        if ref and ref not in evidence_refs:
            context_missing.append(ref)
    hops.append({"hop": "evidence->context", "ok": not context_missing,
                 "checked": len(context.get("citations") or []), "missing": context_missing})

    # ③ 上下文 → 记忆：写下来的记忆，其证据出处不能凭空出现
    memory_missing = []
    for row in (memory.get("decisions") or []):
        if not isinstance(row, Mapping):
            continue
        for ref in (row.get("evidence_refs") or []):
            if ref and ref not in evidence_refs:
                memory_missing.append(str(ref))
    memory_missing = sorted(set(memory_missing))
    hops.append({"hop": "context->memory", "ok": not memory_missing,
                 "checked": len(memory.get("decisions") or []), "missing": memory_missing})

    # ④ 记忆 → 证据：记忆↔证据绑定行指向的证据也必须存在
    link_missing = sorted({str(row.get("evidence_ref") or "")
                           for row in (memory.get("links") or []) if isinstance(row, Mapping)
                           and str(row.get("evidence_ref") or "")
                           and str(row.get("evidence_ref") or "") not in evidence_refs})
    hops.append({"hop": "memory->evidence", "ok": not link_missing,
                 "checked": len(memory.get("links") or []), "missing": link_missing})

    audit_rows = []
    database = getattr(store, "database", None)
    if store is not None and database is not None:
        try:
            audit_rows = store.list_audit_events(trace_id=str(run_id or ""), limit=limit) or []
        except Exception:  # noqa: BLE001
            audit_rows = []
    mismatches: list = []
    checked_decisions = 0
    for row in audit_rows:
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        inputs = payload.get("inputs") if isinstance(payload.get("inputs"), Mapping) else {}
        expected = {"decision": str(payload.get("decision") or ""),
                    "reason": str(payload.get("reason") or "")}
        if not expected["decision"]:
            continue
        recomputed = _replay_one(row.get("event_type"), inputs)
        if recomputed is None:
            continue
        checked_decisions += 1
        if (recomputed.get("decision") != expected["decision"]
                or recomputed.get("reason") != expected["reason"]):
            mismatches.append({"event_type": str(row.get("event_type") or ""),
                               "memory_id": str(inputs.get("memory_id") or ""),
                               "expected": expected, "recomputed": recomputed})
    missing = [hop["hop"] for hop in hops if not hop["ok"]]
    receipt = {
        "replay_version": MEMORY_AUDIT_REPLAY_VERSION,
        "run_id": str(run_id or ""),
        "layers": list(AUDIT_REPLAY_LAYERS),
        "counts": {"stages": int(execution.get("count") or 0),
                   "claims": int((evidence.get("counts") or {}).get("claims") or 0),
                   "evidence": int((evidence.get("counts") or {}).get("evidence") or 0),
                   "citations": int(context.get("citation_count") or 0),
                   "memory_decisions": int((memory.get("counts") or {}).get("decisions") or 0),
                   "memory_items": int((memory.get("counts") or {}).get("items") or 0),
                   "audited_decisions": int(checked_decisions)},
        "hops": hops,
        "missing": missing,
        "replay_matches": not mismatches,
        "mismatches": mismatches,
        "redacted": True,
        "idempotent": True,
        "settings": memory_safety_settings(),
        "note": ("回放只读既有产物（执行/证据/上下文/记忆四层），不重跑检索、不调模型；"
                 "`replay_matches` 是拿审计事件里的输入重跑同一批纯规则的结果比对。"),
    }
    ok, note = validate_contract("audit_replay", dict(receipt))
    receipt["contract_ok"] = bool(ok)
    if not ok:
        receipt["contract_error"] = note
    return receipt


def _replay_one(event_type, inputs: Mapping):
    """按事件种类重跑对应的纯规则（返回 `{decision, reason}`；无法重放返回 None）。"""
    inputs = inputs if isinstance(inputs, Mapping) else {}
    kind = str(event_type or "")
    if kind == "SCOPE_ENFORCED":
        row = enforce_scope(inputs)
        return {"decision": row.get("decision"), "reason": row.get("reason")}
    if kind == "LONGITUDINAL_CHECKED":
        row = longitudinal_check(inputs, now=inputs.get("checked_at") or None,
                                 source_version_changed=inputs.get("source_version_changed"),
                                 high_stakes=inputs.get("high_stakes"))
        return {"decision": row.get("decision"), "reason": row.get("reason")}
    if kind == "ENCOUNTER_ISOLATED":
        context = inputs.get("context") if isinstance(inputs.get("context"), Mapping) else {}
        row = isolation_verdict(inputs.get("item") or {}, context)
        return {"decision": "ALLOW" if row.get("visible") else "BLOCK", "reason": row.get("reason")}
    return None


def _scope_inputs(row: Mapping, kind: Mapping | None = None, axes: Mapping | None = None) -> dict:
    """审计事件里存下的**可重放输入**（作用域判定所需的最小字段集）。

    刻意只留结构化字段与**命中的状态词**（短词，不是正文）：正文一律不进审计
    （P15-05 的字段名政策）。`content_kind` 显式存下来 —— 重放校验的是
    "给定内容种类与作用域，判定是否可复现"，而不是重新跑一遍分类（正文不在场，
    分类本来也无法重跑，这一点如实写在 `verify_replay` 的说明里）。

    `axes` 是**当时实际用于判定的绑定轴**（召回上下文），必须原样记下来：同一条记忆
    在不同的轮次上下文里会得到不同结论，只记行自身的字段是重放不出来的。
    """
    row = row if isinstance(row, Mapping) else {}
    axes = axes if isinstance(axes, Mapping) else {}
    kind = kind if isinstance(kind, Mapping) else classify_content_kind(row)
    scope = str(row.get("scope") or "")
    key = str(row.get("scope_key") or "")
    # 轮次维在库里没有独立列（零迁移），它**编码在作用域键里**；只有在没有显式轴时才从键上还原。
    encounter_from_key = ""
    if scope == "ENCOUNTER" and key.startswith("encounter:"):
        parts = key.split(":")
        if len(parts) >= 4:
            encounter_from_key = parts[3]
    return {
        "memory_id": str(row.get("memory_id") or ""),
        "memory_type": str(row.get("memory_type") or ""),
        "scope": scope,
        "scope_key": key,
        "content_kind": str(kind.get("content_kind") or ""),
        "content_kind_source": str((kind.get("basis") or {}).get("source") or ""),
        "state_terms": list((kind.get("basis") or {}).get("state_terms") or []),
        "freshness_class": str(row.get("freshness_class") or ""),
        "claim_type": str(row.get("claim_type") or (row.get("metadata") or {}).get("claim_type")
                          or ""),
        "owner_user_id": str(axes.get("owner_user_id", row.get("owner_user_id") or "")),
        "session_id": str(axes.get("session_id", row.get("session_id") or "")),
        "industry_pack_id": str(axes.get("industry_pack_id", row.get("industry_pack_id") or "")),
        "encounter_id": str(axes.get("encounter_id", row.get("encounter_id")
                                      or encounter_from_key)),
    }


def _longitudinal_inputs(row: Mapping, *, checked_at: str, gate: Mapping | None = None) -> dict:
    """审计事件里存下的**可重放输入**（长期时效判定所需的最小字段集 + 冻结的时钟）。

    闸门真正读的东西只有这些：状态/时效档/有效期/最后核验时间/证据绑定/衰减/置信 +
    两个"外部结论"入口（高危、来源版本变）。正文不在场，重算不了 `high_stakes` 与
    `source_version_changed` 本身，所以把它们**当时的值**一并记下来（回执里写明白）。
    """
    row = row if isinstance(row, Mapping) else {}
    gate = gate if isinstance(gate, Mapping) else {}
    metadata = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
    return {
        "memory_id": str(row.get("memory_id") or ""),
        "memory_type": str(row.get("memory_type") or ""),
        "scope": str(row.get("scope") or ""),
        "scope_key": str(row.get("scope_key") or ""),
        "freshness_class": str(row.get("freshness_class") or ""),
        "status": str(row.get("status") or ""),
        "valid_from": str(row.get("valid_from") or ""),
        "valid_until": str(row.get("valid_until") or ""),
        "last_verified_at": str(row.get("last_verified_at") or ""),
        "source_evidence_ids": list(row.get("source_evidence_ids") or []),
        "decay_score": row.get("decay_score"),
        "confidence": row.get("confidence"),
        "claim_type": str(row.get("claim_type") or metadata.get("claim_type") or ""),
        "metadata": {"claim_type": str(metadata.get("claim_type") or "")},
        "high_stakes": bool(gate.get("high_stakes")),
        "source_version_changed": bool(gate.get("source_version_changed")),
        "checked_at": str(checked_at or ""),
    }


def apply_recall_safety(store, receipt: Mapping, *, run_meta: Mapping | None = None,
                        trace_id: str = "", record_audit: bool = True) -> dict:
    """P15-01/02/03 作用到**一次召回回执**上：命中过滤 + 逐条判定 + 子回执 + 审计事件。

    三条过滤全部**只减不增**（安全方向单调），每条都带理由码：
      ① 作用域完整性复核判 REJECT 的行 → 隔离（`SCOPE_QUARANTINE`）；
      ② 轮次隔离判不可见的行 → 隔离（`ENCOUNTER_ISOLATION`）；
      ③ 长期时效判 BLOCK 的行 → 隔离（`LONGITUDINAL_BLOCK`，尚未生效或已到终态）。
    保留下来的命中原样带上 `longitudinal`（判定 + 理由 + 年龄），供 Phase 10 复验与 P16 视图。

    回执写成 `receipt["memory_safety"]`（**嵌套子键**）：`QA_MEMORY_GRAPH=0` 时一次召回都不发生，
    所以不会新增任何顶层键，回滚口径与 Phase 09 逐字一致。任何异常都降级成"不过滤 + 记账"。
    """
    receipt = dict(receipt) if isinstance(receipt, Mapping) else {}
    run_meta = run_meta if isinstance(run_meta, Mapping) else {}
    hits = [hit for hit in (receipt.get("hits") or []) if isinstance(hit, Mapping)]
    context = encounter_context(
        owner_user_id=str(run_meta.get("owner_user_id") or ""),
        session_id=str(run_meta.get("session_id") or ""),
        industry_pack_id=str(run_meta.get("industry_pack_id") or ""),
        encounter_id=str(run_meta.get("id") or run_meta.get("run_id") or ""),
        run_id=str(run_meta.get("id") or run_meta.get("run_id") or ""))
    ids = [str(hit.get("memory_id") or "") for hit in hits]
    rows: dict = {}
    if store is not None and ids:
        try:
            rows = {str(row.get("memory_id") or ""): row
                    for row in (store.load_memory_items(memory_ids=ids, include_all_scopes=True,
                                                        limit=max(1, len(ids))) or [])
                    if isinstance(row, Mapping)}
        except Exception:  # noqa: BLE001 —— 读不回来就按"只有命中字段"处理（更保守）
            rows = {}
    enriched: list = []
    missing_rows: list = []
    for hit in hits:
        memory_id = str(hit.get("memory_id") or "")
        row = rows.get(memory_id)
        if row is None:
            missing_rows.append(memory_id)
            row = {**dict(hit), "scope_key": "", "source_evidence_ids": list(hit.get("evidence_refs") or [])}
        enriched.append(dict(row))

    scope_audit = audit_items_scope(enriched, owner_user_id=context["owner_user_id"],
                                    session_id=context["session_id"],
                                    industry_pack_id=context["industry_pack_id"],
                                    encounter_id=context["encounter_id"])
    isolation = isolate(enriched, context)
    # 一次召回用一个**冻结的时刻**（批内一致；审计事件把它记下来，回放才能逐字复现）
    moment = _now_dt(None)
    checks = [longitudinal_check(row, now=moment) for row in enriched]
    blocked = [check["memory_id"] for check in checks
               if check["decision"] == "BLOCK" and longitudinal_freshness_enabled()]
    quarantined = list(scope_audit["quarantined"])
    isolated = isolated_memory_ids(isolation)
    dropped = set(quarantined) | set(isolated) | set(blocked)
    kept: list = []
    by_id = {row["memory_id"]: row for row in enriched}
    check_by_id = {check["memory_id"]: check for check in checks}
    for hit in hits:
        memory_id = str(hit.get("memory_id") or "")
        if memory_id in dropped:
            continue
        annotation = check_by_id.get(memory_id) or {}
        kept.append({**dict(hit),
                     "longitudinal": {key: annotation.get(key) for key in (
                         "decision", "reason", "age_days", "window_days", "longitudinal")},
                     "scope_enforced": True})
    receipt["hits"] = kept
    receipt["memory_safety"] = {
        "version": MEMORY_SAFETY_VERSION,
        "context": {key: context.get(key) for key in (
            "owner_user_id", "session_id", "industry_pack_id", "encounter_id", "run_id")},
        "hits_before": len(hits),
        "hits_after": len(kept),
        "excluded": {"total": len(dropped), "scope_quarantine": len(quarantined),
                     "encounter_isolation": len(isolated), "longitudinal_block": len(blocked),
                     "reasons": _counter(
                         [row.get("reason") for row in scope_audit["violations"]]
                         + [row.get("reason") for row in isolation["isolated_refs"]]
                         + [check.get("reason") for check in checks
                            if check["memory_id"] in blocked and check["decision"] == "BLOCK"])},
        "scope": {key: scope_audit[key] for key in
                  ("checked", "violations", "quarantined", "by_reason")},
        "isolation": {key: isolation[key] for key in
                      ("checked", "visible", "isolated", "isolated_by_reason", "leaks",
                       "cross_encounter")},
        "longitudinal": {key: value for key, value in
                         longitudinal_report(enriched).items() if key != "checks"},
        "missing_rows": missing_rows,
        "settings": memory_safety_settings(),
    }
    if record_audit and audit_enabled() and store is not None:
        sample = int(_env_int("QA_MEMORY_AUDIT_SAMPLE", 5, 1, 50))
        events = []
        # 每个种类都取**前 sample 条被检查过的行**（不只是被拦下的）：审计要能回答
        # "这一轮到底判过什么、结论是什么"，而不只是"拦了谁"。
        for enforcement in (scope_audit.get("receipts") or [])[:sample]:
            events.append(("SCOPE_ENFORCED", enforcement))
        for check in checks[:sample]:
            events.append(("LONGITUDINAL_CHECKED", check))
        verdicts_by_id = {}
        for item in enriched:
            verdicts_by_id[str(item.get("memory_id") or "")] = isolation_verdict(item, context)
        for item in enriched[:sample]:
            verdicts_by_id[str(item.get("memory_id") or "")]["scope_key"] = str(
                item.get("scope_key") or "")
            events.append(("ENCOUNTER_ISOLATED", verdicts_by_id[str(item.get("memory_id") or "")]))
        results = []
        for kind, row in events:
            memory_id = str(row.get("memory_id") or "")
            source = by_id.get(memory_id) or {}
            if kind == "SCOPE_ENFORCED":
                payload = {"inputs": _scope_inputs(
                    {**source, "scope_key": str(row.get("scope_key") or "")}, axes=context),
                    "decision": str(row.get("decision") or ""),
                    "reason": str(row.get("reason") or "")}
            elif kind == "ENCOUNTER_ISOLATED":
                payload = {"inputs": {"item": _scope_inputs(source, axes=context),
                                      "context": context},
                           "decision": "ALLOW" if row.get("visible") else "BLOCK",
                           "reason": str(row.get("reason") or "")}
            else:
                payload = {"inputs": _longitudinal_inputs(source, checked_at=_stamp(moment),
                                                          gate=row.get("gate") or {}),
                           "decision": str(row.get("decision") or ""),
                           "reason": str(row.get("reason") or "")}
            results.append(record_memory_audit(
                store, kind, payload, run_id=context["run_id"],
                trace_id=str(trace_id or context["run_id"]), origin="memory_recall",
                industry_pack_id=context["industry_pack_id"]))
        receipt["memory_safety"]["audit"] = {
            "events": len(events),
            "recorded": len([row for row in results if row.get("recorded")]),
            "errors": [row.get("error") for row in results if row.get("error")][:3],
        }
    return receipt


def scope_census(store, *, limit: int | None = None) -> dict:
    """真机/本地库的作用域普查（验收用统计；只读，不改任何状态）。

    回答验收要的那几个数字：**记忆按作用域/类型的分布**、**隔离会拦掉多少条**、
    **长期记忆里有多少条要复验**、**有多少条的作用域键与规则不一致**。
    """
    limit = replay_max_rows() * 4 if limit is None else int(limit)
    items: list = []
    if store is not None:
        try:
            items = store.load_memory_items(include_all_scopes=True, limit=limit) or []
        except Exception:  # noqa: BLE001
            items = []
    items = [row for row in items if isinstance(row, Mapping)]
    contexts: dict = {}
    isolation_rows: list = []
    for item in items:
        key = (str(item.get("owner_user_id") or ""), str(item.get("session_id") or ""),
               str(item.get("industry_pack_id") or ""), str(item.get("created_from_run_id") or ""))
        if key not in contexts:
            contexts[key] = encounter_context(owner_user_id=key[0], session_id=key[1],
                                              industry_pack_id=key[2], encounter_id=key[3])
        isolation_rows.append(isolation_verdict(item, contexts[key]))
    longitudinal = longitudinal_report(items)
    audit_violations = audit_items_scope(items)
    return {
        "memory_safety_version": MEMORY_SAFETY_VERSION,
        "items": len(items),
        "by_scope": _counter(item.get("scope") for item in items),
        "by_type": _counter(item.get("memory_type") for item in items),
        "by_status": _counter(item.get("status") for item in items),
        "by_freshness": _counter(item.get("freshness_class") for item in items),
        "scope_keys": _counter(item.get("scope_key") for item in items),
        "isolation": {"checked": len(isolation_rows),
                      "visible_in_own_context": len([row for row in isolation_rows
                                                     if row["visible"]]),
                      "cross_encounter_live": len([row for row in isolation_rows
                                                   if row["cross_encounter"] and row["visible"]]),
                      "by_reason": _counter(row["reason"] for row in isolation_rows)},
        "longitudinal": {key: value for key, value in longitudinal.items() if key != "checks"},
        "scope_violations": {"count": len(audit_violations["violations"]),
                             "by_reason": audit_violations["by_reason"],
                             "refs": audit_violations["violations"][:20]},
        "settings": memory_safety_settings(),
    }


__all__ = [
    "CONTENT_LOG_KEYS", "CURRENT_STATE_CLAIM_TYPES", "DEFAULT_CURRENT_STATE_TERMS",
    "DEFAULT_DANGER_SIGN_MIN_TERMS", "DEFAULT_LONGITUDINAL_WINDOW_DAYS",
    "ENVIRONMENT_MEMORY_TYPES", "IGNORED_COST_TERMS", "KNOWLEDGE_CLAIM_TYPES",
    "LONGITUDINAL_SCOPES", "RULE_MEMORY_TYPES", "SAFETY_ACTION_KINDS",
    "SAFETY_ACTION_SOURCES",
    "apply_recall_safety", "apply_scope_enforcement", "audit_enabled", "audit_items_scope",
    "audit_payload",
    "classify_content_kind", "current_state_terms", "danger_sign_min_terms", "danger_signs",
    "encounter_context", "encounter_isolation_enabled", "enforce_scope", "hard_trigger_active",
    "isolate", "isolated_memory_ids", "isolation_verdict", "log_text_limit",
    "longitudinal_check", "longitudinal_freshness_enabled", "longitudinal_report",
    "longitudinal_window_days", "memory_safety_settings", "minimize_log_payload",
    "minimize_value", "plan_safety_receipt", "privacy_minimal_enabled", "record_memory_audit",
    "replay_bundle", "replay_max_rows", "safety_enforcement_receipt", "safety_hard_trigger_enabled",
    "safety_terms", "safety_trigger", "scope_binding", "scope_census", "scope_enforcement_enabled",
    "scope_of_item", "verify_no_leak", "verify_replay",
]

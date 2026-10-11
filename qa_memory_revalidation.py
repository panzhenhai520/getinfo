#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 10（P10-01…P10-06）· Revalidation & Memory Conflict：时效闸门 / 来源版本检测 /
MEMORY_HINT 复验 / 记忆矛盾 / 取代 / 污染撤销（**纯规则与统计，零模型调用，零嵌入端点**）。

通用包 01_V2_ARCHITECTURE 依据：
  · §2.1 `Memory Hint → freshness/source-version/applicability gate → 必要时重新检索 →
    Verifier → 当前 Evidence Graph → Answer`：本模块补上 P09 留下的**闸门与复验**；
  · §10 硬规则四条逐条落地（先判先返回）：`status!=ACTIVE 不作证据`、
    `source_version_changed 必须 revalidate`、`freshness_required 必须 revalidate`、
    `high_stakes 默认 revalidate`；
  · §11 矛盾口径：**M1 支持 C、M2 反驳 C 时建立 MemoryContradiction，禁止"最新自动覆盖"**；
    新证据确认 M2 后 `M1.status=SUPERSEDED` 并建立 `M1--SUPERSEDED_BY→M2`；
  · §11 防污染："未验证自由生成内容不得成为事实 Memory；必须保存 provenance；高风险事实
    重新验证；写入/失效/覆盖全部 audit；支持按 source/entity/session 撤销污染 Memory"；
  · §16 指标：Stale Memory Rate / Revalidation Pass Rate / Supersession Accuracy 的分母与
    分子由本模块的分布回执给出（真跑分布见 `baseline/qa-memory-revalidation-acceptance.json`）。

复用（一行判定逻辑都没有重写）：
  ① Phase 03 `qa_verifier`：`verify_evidence_item()`（**复验的唯一判定器**）、
     `evidence_score()`、`check_negation()`/`check_numbers()`（配对的单一真源）、`term_set()`；
  ② Phase 06 `qa_evidence_graph.resolve()`：**矛盾裁决的唯一决策点**（§15 八项比较 + 九条理由码），
     本模块只把"记忆"与"本轮证据"折算成同一套可比指标（mass/verified_mass/authority/
     independence/latest/claim_latest），绝不另算一套质量判断；
  ③ Phase 09 `qa_memory`：衰减（`decay_score`）、半衰期与 TTL 表（`HALF_LIFE_DAYS`/`TTL_DAYS`）、
     证据绑定行（`evidence_link_row`）、作用域与内容寻址；
  ④ 冻结契约 `qa_contracts`（一个字段都没碰）与图谱契约 `qa_graph_contracts` 的 P10 段。

边界与取舍（诚实声明，宁写 PARTIAL 不谎报）
------------------------------------------------
  1. **零模型调用**：闸门、版本检测、复验、矛盾、取代、撤销全部是**规则 + 统计**。
     §29 的"strong model 裁决器"与"LLM 复验判定"做成**可插拔注入点**
     （`register_revalidation_judge()` / `qa_evidence_graph.register_contradiction_resolver()`），
     默认 `rule`，**不注册任何模型后端、不发任何网络请求**。
  2. **复验不发明检索**：候选证据只来自"本轮证据图里已有的证据"（同一 run 的已核验证据、
     或记忆自身已绑定的证据引用）。没有候选就如实报 `NO_CANDIDATE_EVIDENCE` ——
     重新检索是 Phase 04/07 的账，这里**绝不假装查过**。
  3. **记忆正文永远不是证据**（MASTER_RULES 11）：复验成功只意味着"它重新绑上了本轮
     Phase 03 判为 SUPPORTED 的证据引用"，回执里用 `verified_scope="evidence_refs"` 钉死语义；
     `verified_evidence=True` 指的是**那些证据引用**，不是记忆正文。
  4. **只有时间裁决能取代**（§11 禁止"最新自动覆盖"）：`NEWER_VERSION_PRECEDES` 之外的理由码
     一律走 `CONTRADICTED`；且 SUPERSEDE 必须能指向一个**后继记忆 id**，
     否则退化为 `CONTRADICTED`（保证 `superseded_by` 永不悬空）。
  5. **REVOKED 是终态**：撤销不可复活（`MEMORY_REVALIDATION_TRANSITIONS` 里没有任何
     `REVOKED → *`）。
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping, Sequence

from qa_evidence import evidence_object
from qa_evidence_graph import resolve as resolve_contradiction
from qa_graph_contracts import (
    CONTRADICTION_RESOLVED_CODES,
    EVIDENCE_STATUS_SUPPORTED,
    MEMORY_CONTRADICTION_KINDS,
    MEMORY_CONTRADICTION_OUTCOMES,
    MEMORY_CONTRADICTION_STATUS_ACTIONS,
    MEMORY_CONTRADICTION_VERSION,
    MEMORY_FRESHNESS_CLASSES,
    MEMORY_FRESHNESS_DECISIONS,
    MEMORY_FRESHNESS_GATE_VERSION,
    MEMORY_FRESHNESS_REASONS,
    MEMORY_REVALIDATION_OUTCOMES,
    MEMORY_REVALIDATION_REASONS,
    MEMORY_REVALIDATION_TRANSITIONS,
    MEMORY_REVALIDATION_VERSION,
    MEMORY_REVOKE_REASONS,
    MEMORY_REVOKE_VERSION,
    MEMORY_SOURCE_VERSION_REASONS,
    MEMORY_SOURCE_VERSION_VERSION,
    MEMORY_STATUSES,
    MEMORY_SUPERSESSION_REASONS,
    MEMORY_TYPES,
    MEMORY_SUPERSESSION_VERSION,
    RESOLUTION_NEWER_VERSION,
    RESOLUTION_NO_DECISIVE_RULE,
    RESOLUTION_SCOPE_DIFFERENCE,
    validate as validate_contract,
)
from qa_memory import HALF_LIFE_DAYS, TTL_DAYS, decay_score, evidence_link_row
from qa_verifier import (
    VERDICT_QUALIFIED,
    VERDICT_REFUTED,
    VERDICT_SUPPORTED,
    VERDICT_UNVERIFIED,
    check_negation,
    check_numbers,
    term_set,
    verify_evidence_item,
)

# ── 可配置旋钮（全部可回滚；默认值写在常量里便于复算）───────────────────────

DEFAULT_REVALIDATE_AGE_RATIO = 0.25
"""年龄阈值：时效档为 LONG/MEDIUM 的记忆，年龄超过"半衰期 × 本比例"就要复验。

0.25 的标定：LONG 半衰期 3650 天 → 2.5 年后复验一次；MEDIUM 365 天 → 约 91 天。
不设 0：那会把每条记忆都判成"必须复验"，闸门就失去分辨力。"""

REVALIDATE_FRESHNESS_CLASSES = ("VERSION_SENSITIVE", "SHORT", "VERY_SHORT", "SESSION",
                                "ENCOUNTER_BOUND")
"""§10 里"时效敏感"的档：这些档的记忆**一律**要走复验（口径与 P09 的 `TTL_DAYS` 同源）。

LONG / MEDIUM 不在此列（它们走年龄阈值），这是"数学定义不需要每轮重验"的机器形态。"""

HIGH_STAKES_FRESHNESS_CLASSES = ("VERSION_SENSITIVE", "VERY_SHORT", "SESSION", "ENCOUNTER_BOUND")
"""天然高危的时效档（规范/指引、实时数据、当前状态）——§10 "high_stakes 默认 revalidate"。"""

DEFAULT_HIGH_STAKES_CLAIM_TYPES = ("policy", "regulation", "finance", "medical", "legal",
                                   "price", "tax")
"""高危结论类型（可被 `QA_MEMORY_HIGH_STAKES_TYPES` 覆盖，逗号分隔）。"""

HIGH_STAKES_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?\s*(?:%|％|元|港元|美元|万|亿|天|个月|年))")
"""高危内容形态：带单位/比率/金额的数字（政策门槛、税率、期限）。纯规则、可复算。"""

SOURCE_VERSION_TOKEN_RES = (
    re.compile(r"\bv?(\d+(?:\.\d+)+)\b"),                      # v2.1 / 3.0.1
    re.compile(r"第\s*(\d+)\s*版"),                             # 第 3 版
    re.compile(r"(\d{4})\s*年版"),                              # 2026 年版
    re.compile(r"版本\s*(\d+(?:\.\d+)*)"),                       # 版本 2
    re.compile(r"(\d{4})\s*年\s*修订"),                          # 2026 年修订
)
"""来源版本的**抽取规则**（P10-02）：只认显式的版本标记，**不把普通日期当版本**
（否则每篇带日期的证据都会被判成"版本变了"，闸门就废了）。"""

CANDIDATE_TERM_OVERLAP = 0.2
"""候选证据的词面相关下限：记忆正文实词与证据实词的交集 / 记忆实词数。

0.2 是"至少说到同一件事的一小半"的确定性门槛；低于它的证据不会被拿来当复验候选
（拿无关证据去"复验"等于制造假结论）。"""

CONTRADICTION_ELIGIBLE_TYPES = ("VERIFIED_CLAIM",)
"""**能参与矛盾判定的记忆类型**（可被 `QA_MEMORY_CONTRADICTION_TYPES` 覆盖）。

只有"可以对错"的断言型记忆才谈得上互相矛盾：Phase 09 的 `ENTITY` 记忆只是**索引**
（"实体记忆只是索引，不是事实断言"），拿一条实体名去和一条含该词的长句比否定极性，
会把"实体 X"与"提到 X 且带否定词的一段话"判成矛盾 —— 真机数据上这条正是 8 条假矛盾的
来源（实体记忆用 `last_verified_at` 当生效时间，还顺带赢了时间裁决）。所以类型白名单是
**判定前置条件**，而不是事后过滤。"""

CONTRADICTION_TERM_OVERLAP = 0.5
"""两条记忆被判为"说同一件事"的词面重叠门槛（配对矛盾的**必要**条件之一）。"""

CONTRADICTION_ENTITY_JACCARD = 0.5
"""主体一致性的门槛：两条记忆的实体键集合 Jaccard 相似度（另一条**必要**条件）。

两个门槛**同时**满足才配对（"同一主体"且"同一命题"）。实测教训（真机数据）：
只要"共享任意一个实体键"就配对，会把两条**不相干的长段落**（各自带着私募基金/股权投资等
公共词）配成矛盾 —— 长文里出现一次"不予/无需"就能被 `check_negation` 锚定成反证。
宁可漏（真机断言型记忆之间当前 0 条真矛盾）也不许自动把不相干的记忆标成 CONTRADICTED：
§11 禁止"最新自动覆盖"，而这个判定会直接改状态。
可用 `QA_MEMORY_CONTRADICTION_OVERLAP` / `QA_MEMORY_CONTRADICTION_JACCARD` 调。"""

UNKNOWN_SUBJECT_OVERLAP = 0.75
"""两侧**都没有实体信息**时的词面重叠门槛（主体一致性判不了，只认近乎逐字相同的命题）。"""

MAX_CONTRADICTION_PAIRS = 200
"""一轮最多配对多少对矛盾（确定性上限，防止 O(n²) 在真机大库上失控）。"""

VALIDATION_WRITE_OUTCOMES = ("REVALIDATED", "REFRESHED_NO_CHANGE", "REFUTED", "UNVERIFIED",
                             "NO_CANDIDATE_EVIDENCE", "BLOCKED_BY_GATE", "SKIPPED_NOT_ACTIVE")
"""允许写进 `memory_validation` 的出口（与契约枚举等值；守卫用例钉死两者一致）。"""

_REVOKE_SELECTORS = ("source_fingerprint", "entity_key", "session_id", "memory_ids")
"""按来源/实体/会话/显式 id 撤销（§11 "支持按 source/entity/session 撤销污染 Memory"）。"""


# ── 旋钮读取（环境变量全部可回滚；默认值在常量里）──────────────────────────

def _env_flag(name: str, default: bool) -> bool:
    raw = str(os.environ.get(name, "") or "").strip().casefold()
    if not raw:
        return default
    return raw not in ("0", "false", "off", "no", "disabled", "")


def _env_float(name: str, default: float, low: float = 0.0, high: float = 1.0) -> float:
    try:
        value = float(str(os.environ.get(name, "") or "").strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def revalidation_enabled() -> bool:
    """管线开关（默认**关**）：关掉时 Phase 09 之后的行为逐字不变（回滚口径）。"""
    return _env_flag("QA_MEMORY_REVALIDATION", False)


def revalidate_age_ratio() -> float:
    return _env_float("QA_MEMORY_REVALIDATE_AGE_RATIO", DEFAULT_REVALIDATE_AGE_RATIO, 0.0, 10.0)


def high_stakes_types() -> tuple:
    raw = str(os.environ.get("QA_MEMORY_HIGH_STAKES_TYPES", "") or "").strip()
    if not raw:
        return DEFAULT_HIGH_STAKES_CLAIM_TYPES
    return tuple(sorted({item.strip().casefold() for item in raw.split(",") if item.strip()}))


def unresolved_conflict_status() -> str:
    """未消解矛盾落到什么状态（默认 `CONTRADICTED`；可配 `STALE` 作为更软的档）。"""
    value = str(os.environ.get("QA_MEMORY_UNRESOLVED_STATUS", "CONTRADICTED") or "").strip().upper()
    return value if value in ("CONTRADICTED", "STALE") else "CONTRADICTED"


def contradiction_types() -> tuple:
    """能参与矛盾判定的记忆类型（默认只有 `VERIFIED_CLAIM`；环境变量可覆盖）。"""
    raw = str(os.environ.get("QA_MEMORY_CONTRADICTION_TYPES", "") or "").strip()
    if not raw:
        return CONTRADICTION_ELIGIBLE_TYPES
    picked = tuple(sorted({item.strip().upper() for item in raw.split(",") if item.strip()}))
    return picked or CONTRADICTION_ELIGIBLE_TYPES


def contradiction_overlap() -> float:
    """配对矛盾的词面重叠门槛（`QA_MEMORY_CONTRADICTION_OVERLAP`，默认 0.5）。"""
    return _env_float("QA_MEMORY_CONTRADICTION_OVERLAP", CONTRADICTION_TERM_OVERLAP, 0.0, 1.0)


def contradiction_jaccard() -> float:
    """配对矛盾的主体（实体键 Jaccard）门槛（`QA_MEMORY_CONTRADICTION_JACCARD`，默认 0.5）。"""
    return _env_float("QA_MEMORY_CONTRADICTION_JACCARD", CONTRADICTION_ENTITY_JACCARD, 0.0, 1.0)


# ── 小工具（纯函数）─────────────────────────────────────────────────────────

def _digest(value, length: int = 24) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:length]


def _clamp(value, low: float = 0.0, high: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = low
    return max(low, min(high, number))


def _now_dt(value=None) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = _parse_time(value)
    if parsed is not None:
        return parsed
    return datetime.now(timezone.utc)


def _parse_time(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    candidate = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        moment = datetime.fromisoformat(candidate)
    except ValueError:
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                moment = datetime.strptime(text[:19], pattern)
                break
            except ValueError:
                continue
        else:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _count(values) -> dict:
    counts: dict = {}
    for value in values or []:
        key = str(value or "")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda row: (-row[1], row[0])))


def _as_list(value) -> list:
    if isinstance(value, (list, tuple, set)):
        return [item for item in value]
    if value in (None, ""):
        return []
    return [value]


# ── P10-01：freshness / TTL 闸门 ────────────────────────────────────────────

def freshness_required(item: Mapping, *, now=None) -> dict:
    """这一档时效是否需要复验（§10 的 `freshness_required`）——纯规则、带数字。"""
    item = item if isinstance(item, Mapping) else {}
    freshness = str(item.get("freshness_class") or "MEDIUM")
    decay = decay_score(item, now=_now_dt(now))
    ratio = revalidate_age_ratio()
    half_life = float(HALF_LIFE_DAYS.get(freshness, 365.0))
    threshold_days = round(half_life * ratio, 4)
    by_class = freshness in REVALIDATE_FRESHNESS_CLASSES
    return {
        "required": bool(by_class or float(decay.get("age_days") or 0) > threshold_days),
        "by_class": bool(by_class),
        "by_age": bool(float(decay.get("age_days") or 0) > threshold_days),
        "age_days": float(decay.get("age_days") or 0),
        "threshold_days": threshold_days,
        "age_ratio": ratio,
        "freshness_class": freshness,
    }


def high_stakes_of(item: Mapping, *, claim_type: str = "", stakes=None) -> dict:
    """高危判定（§10 "high_stakes 默认 revalidate"）：纯规则，理由逐条列出。

    命中任一即高危：① 结论类型在 `HIGH_STAKES_CLAIM_TYPES`；② 时效档是
    `HIGH_STAKES_FRESHNESS_CLASSES`；③ 正文含带单位/比率/金额的数字；④ 调用方显式指定。
    """
    item = item if isinstance(item, Mapping) else {}
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    kind = str(claim_type or metadata.get("claim_type") or item.get("claim_type") or "").casefold()
    freshness = str(item.get("freshness_class") or "")
    text = str(item.get("canonical_content") or "")
    reasons = []
    if stakes is True:
        reasons.append("EXPLICIT_STAKES")
    if kind and kind in high_stakes_types():
        reasons.append("CLAIM_TYPE:%s" % kind)
    if freshness in HIGH_STAKES_FRESHNESS_CLASSES:
        reasons.append("FRESHNESS:%s" % freshness)
    numbers = sorted({match.group(0).strip() for match in HIGH_STAKES_NUMBER_RE.finditer(text)})
    if numbers:
        reasons.append("NUMBERED:%s" % ",".join(numbers[:3]))
    if stakes is False:
        # 显式指定"不是高危"优先于规则命中（调用方负责，例如纯背景类研究任务）
        return {"high_stakes": False, "reasons": ["EXPLICIT_NOT_HIGH_STAKES"], "claim_type": kind}
    return {"high_stakes": bool(reasons), "reasons": reasons, "claim_type": kind,
            "numbers": numbers[:5], "freshness_class": freshness}


def freshness_gate(item: Mapping, *, now=None, source_version_changed: bool | None = None,
                   high_stakes=None, decay: Mapping | None = None) -> dict:
    """P10-01 时效闸门：**先判先返回**的十一条规则（顺序 = §10 硬规则 + 状态/年龄阈值）。

    输入：记忆条目 + 时钟 + P10-02 的来源版本结论（None = 未检测，按"未变"处理）。
    输出：`{gate_version, memory_id, decision, reason, ...}`，全部字段可复算。
    `decision` 三值：`ALLOW`（提示可原样用，**仍是提示不是证据**）/ `REVALIDATE` / `BLOCK`。
    """
    item = item if isinstance(item, Mapping) else {}
    moment = _now_dt(now)
    computed = dict(decay) if isinstance(decay, Mapping) else decay_score(item, now=moment)
    # 脏数据（库里的取值不在契约枚举内）一律**归一化到合法值**并在 detail 里留下原值：
    # 回执必须恒可校验（契约绿），但也不能假装脏值没出现过。
    status_raw = str(item.get("status") or "ACTIVE")
    status = status_raw if status_raw in MEMORY_STATUSES else "ACTIVE"
    freshness_raw = str(item.get("freshness_class") or "MEDIUM")
    freshness = freshness_raw if freshness_raw in MEMORY_FRESHNESS_CLASSES else "MEDIUM"
    stakes = high_stakes_of(item) if high_stakes is None else (
        high_stakes if isinstance(high_stakes, Mapping) else high_stakes_of(item, stakes=high_stakes))
    required = freshness_required(item, now=moment)
    valid_until = _parse_time(item.get("valid_until"))
    decision, reason = "ALLOW", "FRESH_AND_VERIFIED"

    if status in ("SUPERSEDED", "CONTRADICTED", "REVOKED"):
        decision, reason = "BLOCK", "NOT_ACTIVE"
    elif valid_until is not None and valid_until < moment:
        decision, reason = "REVALIDATE", "VALID_UNTIL_PASSED"
    elif status == "EXPIRED":
        decision, reason = "REVALIDATE", "STATUS_EXPIRED"
    elif float(computed.get("score") or 0) < 0.02:      # EXPIRE_FLOOR（与 qa_memory 同值）
        decision, reason = "BLOCK", "DECAY_BELOW_EXPIRE_FLOOR"
    elif source_version_changed:
        decision, reason = "REVALIDATE", "SOURCE_VERSION_CHANGED"
    elif stakes.get("high_stakes"):
        decision, reason = "REVALIDATE", "HIGH_STAKES_DEFAULT"
    elif required.get("by_class"):
        decision, reason = "REVALIDATE", "FRESHNESS_REQUIRED"
    elif status == "STALE":
        decision, reason = "REVALIDATE", "STATUS_STALE"
    elif required.get("by_age"):
        decision, reason = "REVALIDATE", "AGE_OVER_REVALIDATE_THRESHOLD"
    elif not (item.get("source_evidence_ids") or item.get("evidence_refs")
              or item.get("source_evidence_ids_json")):
        decision, reason = "REVALIDATE", "NO_EVIDENCE_BINDING"

    receipt = {
        "gate_version": MEMORY_FRESHNESS_GATE_VERSION,
        "memory_id": str(item.get("memory_id") or ""),
        "decision": decision, "reason": reason, "freshness_class": freshness, "status": status,
        "age_days": float(computed.get("age_days") or 0),
        "half_life_days": float(computed.get("half_life_days") or 0),
        "decay_score": float(computed.get("score") or 0),
        "valid_until": str(item.get("valid_until") or ""),
        "high_stakes": bool(stakes.get("high_stakes")),
        "source_version_changed": bool(source_version_changed),
        "thresholds": {"age_ratio": required.get("age_ratio"),
                       "age_threshold_days": required.get("threshold_days"),
                       "expire_floor": 0.02,
                       "revalidate_classes": list(REVALIDATE_FRESHNESS_CLASSES)},
        "detail": {
            "high_stakes_reasons": list(stakes.get("reasons") or []),
            "freshness_required": required,
            "freshness_class_raw": freshness_raw,
            "status_raw": status_raw,
            "rule_order": ["NOT_ACTIVE", "VALID_UNTIL_PASSED", "STATUS_EXPIRED",
                           "DECAY_BELOW_EXPIRE_FLOOR", "SOURCE_VERSION_CHANGED",
                           "HIGH_STAKES_DEFAULT", "FRESHNESS_REQUIRED", "STATUS_STALE",
                           "AGE_OVER_REVALIDATE_THRESHOLD", "NO_EVIDENCE_BINDING",
                           "FRESH_AND_VERIFIED"],
        },
    }
    ok, note = validate_contract("memory_freshness_decision", dict(receipt))
    receipt["contract_ok"] = bool(ok)
    if not ok:
        receipt["contract_error"] = note
    return receipt


# ── P10-02：source-version detection ───────────────────────────────────────

def version_tokens(text) -> list:
    """抽取显式版本标记（P10-02）：`v2.1` / `第3版` / `2026年版` / `版本2` / `2026年修订`。"""
    body = str(text or "")
    tokens = set()
    for pattern in SOURCE_VERSION_TOKEN_RES:
        for match in pattern.finditer(body):
            value = match.group(1)
            if value:
                tokens.add(str(value))
    return sorted(tokens, key=_version_sort_key)


def _version_sort_key(token: str):
    """版本号比较键：数字段按数值比，`2026-04-01` 这类按元组比，其余按字符串比。"""
    parts = re.findall(r"\d+", str(token or ""))
    if parts:
        return (1, tuple(int(item) for item in parts[:4]), str(token))
    return (0, (), str(token))


def latest_version_token(tokens: Iterable) -> str:
    values = [str(item) for item in (tokens or []) if str(item or "")]
    return max(values, key=_version_sort_key) if values else ""


def source_version_state(item: Mapping, links: Sequence[Mapping] = (), *,
                         current_evidence: Sequence[Mapping] = (),
                         corpus_version: str = "") -> dict:
    """P10-02 来源版本检测：**语料版本** + **文档版本号**两条确定性口径。

    ① 语料版本：记忆绑定的证据行里记着写入时的 `corpus_version`；当前 run 的语料版本
       与它不同（两边都非空）→ `CORPUS_VERSION_CHANGED`（§10 硬规则：必须 revalidate）；
    ② 文档版本号：记忆正文里的版本标记 vs **同一份来源/同一证据引用**在本轮重新出现时的
       版本标记；当前更"新"（数值段更大）→ `DOCUMENT_VERSION_CHANGED`；
    ③ 都没有可比信息 → `NO_CORPUS_VERSION` / `NO_VERSION_TOKEN`，**不算变更**（不猜）。
    """
    item = item if isinstance(item, Mapping) else {}
    links = [row for row in (links or []) if isinstance(row, Mapping)]
    reasons: list = []
    link_versions = sorted({str(row.get("corpus_version") or "") for row in links
                            if str(row.get("corpus_version") or "")})
    current = str(corpus_version or "")
    changed = False
    if current and link_versions and any(value != current for value in link_versions):
        changed = True
        reasons.append("CORPUS_VERSION_CHANGED")
    elif not current or not link_versions:
        reasons.append("NO_CORPUS_VERSION")

    memory_tokens = version_tokens(item.get("canonical_content"))
    refs = {str(row.get("evidence_ref") or "") for row in links if str(row.get("evidence_ref") or "")}
    sources = {str(row.get("source_fingerprint") or "") for row in links
               if str(row.get("source_fingerprint") or "")}
    current_tokens: list = []
    for evidence in current_evidence or []:
        if not isinstance(evidence, Mapping):
            continue
        layer = evidence_object(evidence)
        source = layer.get("source") if isinstance(layer.get("source"), Mapping) else {}
        same_ref = str(evidence.get("evidence_ref") or "") in refs
        same_source = str(layer.get("source_fingerprint") or source.get("source_id") or "") in sources
        if not (same_ref or same_source):
            continue
        body = "%s %s %s" % (evidence.get("title") or "", evidence.get("content_excerpt") or "",
                             (layer.get("span") or {}).get("quote")
                             if isinstance(layer.get("span"), Mapping) else "")
        current_tokens.extend(version_tokens(body))
    current_tokens = sorted(set(current_tokens), key=_version_sort_key)
    newest_memory, newest_current = latest_version_token(memory_tokens), \
        latest_version_token(current_tokens)
    if newest_memory and newest_current and _version_sort_key(newest_current) > \
            _version_sort_key(newest_memory):
        changed = True
        reasons.append("DOCUMENT_VERSION_CHANGED")
    elif not memory_tokens or not current_tokens:
        reasons.append("NO_VERSION_TOKEN")
    if not changed:
        reasons.append("SOURCE_VERSION_STABLE")
    receipt = {
        "source_version_check": MEMORY_SOURCE_VERSION_VERSION,
        "memory_id": str(item.get("memory_id") or ""),
        "changed": bool(changed),
        "reasons": sorted(set(reasons), key=MEMORY_SOURCE_VERSION_REASONS.index
                          if set(reasons) <= set(MEMORY_SOURCE_VERSION_REASONS) else str),
        "corpus_version_current": current,
        "link_corpus_versions": link_versions,
        "version_tokens_memory": memory_tokens,
        "version_tokens_current": current_tokens,
        "newest_token_memory": newest_memory,
        "newest_token_current": newest_current,
        "detail": ("语料版本 %s → %s；文档版本 %s → %s"
                   % (",".join(link_versions) or "无", current or "无",
                      newest_memory or "无", newest_current or "无")),
    }
    ok, note = validate_contract("memory_source_version", dict(receipt))
    receipt["contract_ok"] = bool(ok)
    if not ok:
        receipt["contract_error"] = note
    return receipt


# ── P10-03：MEMORY_HINT revalidation ───────────────────────────────────────

_REVALIDATION_JUDGES: dict = {}


def register_revalidation_judge(name: str, fn) -> None:
    """注册**可插拔**复验判定器（规格书 §29 的 "strong model, conditional"）。

    本轮不注册任何模型后端（GPU 停用、硬约束不许调模型）；接口留给后续阶段/私有部署：
    `fn(payload: Mapping) -> Mapping`，需返回含 `outcome` 的字典。
    **安全约束（MASTER_RULES 11）**：注入的判定器**不能**把一条记忆提升成
    "已绑上已验证证据" —— 它只能否决提升（返回非 `REVALIDATED` 的出口）；
    只有 Phase 03 的规则核验判出 SUPPORTED 才允许 `REVALIDATED`。
    """
    clean = str(name or "").strip()
    if not clean or not callable(fn):
        raise ValueError("判定器名字与可调用对象必填")
    _REVALIDATION_JUDGES[clean] = fn


def revalidation_judges() -> tuple:
    return tuple(sorted(_REVALIDATION_JUDGES))


def judge_name() -> str:
    return str(os.environ.get("QA_MEMORY_REVALIDATION_JUDGE", "rule") or "rule").strip() or "rule"


def _candidate_evidence(memory: Mapping, current_evidence: Sequence[Mapping],
                        links: Sequence[Mapping] = (), *, limit: int = 8) -> list:
    """挑复验候选证据（**只用本轮已有证据**，确定性排序）：

    优先级 ① 与记忆绑定的证据引用同 `evidence_ref`（同一份文件的新版本）；
    ② 与绑定证据同 `source_fingerprint`（同来源的新材料）；
    ③ 词面相关度 ≥ `CANDIDATE_TERM_OVERLAP` 且来源指纹未在记忆里出现过的证据。
    排序键 = (优先级, evidence_ref)，同输入同输出。
    """
    claim_terms = term_set(str(memory.get("canonical_content") or ""), sizes=(2, 3))
    refs = {str(row.get("evidence_ref") or "") for row in (links or [])}
    sources = {str(row.get("source_fingerprint") or "") for row in (links or [])}
    scored = []
    for evidence in current_evidence or []:
        if not isinstance(evidence, Mapping):
            continue
        ref = str(evidence.get("evidence_ref") or "")
        if not ref:
            continue
        layer = evidence_object(evidence)
        source = layer.get("source") if isinstance(layer.get("source"), Mapping) else {}
        fingerprint = str(layer.get("source_fingerprint") or source.get("source_id") or "")
        body = "%s %s" % (evidence.get("title") or "", evidence.get("content_excerpt") or "")
        terms = term_set(body, sizes=(2, 3))
        overlap = (len(claim_terms & terms) / float(len(claim_terms))) if claim_terms else 0.0
        if ref in refs:
            priority = 0
        elif fingerprint and fingerprint in sources:
            priority = 1
        elif overlap >= CANDIDATE_TERM_OVERLAP:
            priority = 2
        else:
            continue
        scored.append((priority, ref, round(overlap, 6), evidence))
    scored.sort(key=lambda row: (row[0], row[1]))
    return [{"priority": row[0], "evidence_ref": row[1], "overlap": row[2],
             "evidence": row[3]} for row in scored[:max(1, int(limit))]]


def _evidence_refs_of(item: Mapping) -> list:
    item = item if isinstance(item, Mapping) else {}
    refs = item.get("evidence_refs") or item.get("source_evidence_ids") or []
    return sorted({str(value) for value in refs if str(value or "")})


def _link_row_for(evidence: Mapping, verification: Mapping, *, run_id: str, stage: str,
                  corpus_version: str) -> dict:
    row = {"verdict": str(verification.get("verdict") or ""),
           "evidence_score": float(verification.get("score") or 0),
           "relationship": str(evidence.get("relationship") or "")}
    return evidence_link_row(evidence, row, run_id=run_id, stage=stage,
                             corpus_version=corpus_version)


def _validation_id(*, memory_id: str, run_id: str, outcome: str, evidence_refs: Sequence[str],
                   gate: str, status_before: str) -> str:
    return "MVAL" + _digest("|".join((memory_id, run_id, outcome, gate, status_before,
                                      ",".join(sorted(evidence_refs)))))


def _apply_status(store, memory_id: str, target: str, *, reason: str, run_id: str,
                  now=None) -> dict:
    """按 P10 的迁移表做一次状态迁移（不在表里的一律拒绝并记账）。"""
    current = store.load_memory_items(memory_ids=[memory_id], include_all_scopes=True, limit=1)
    item = current[0] if current else {}
    before = str(item.get("status") or "")
    if before == target and target not in ("REVALIDATED",):
        return {"memory_id": memory_id, "from": before, "to": target, "changed": False,
                "reason": "ALREADY_" + target}
    key = (before, target)
    if key not in MEMORY_REVALIDATION_TRANSITIONS:
        return {"memory_id": memory_id, "from": before, "to": target, "changed": False,
                "error": "TRANSITION_NOT_ALLOWED", "transition": list(key)}
    result = store.update_memory_status(memory_id, status=target, reason=reason,
                                        change="STATUS", now=_stamp(_now_dt(now)))
    result["transition_reason"] = MEMORY_REVALIDATION_TRANSITIONS[key]
    return result


def revalidate_item(store, item: Mapping, *, current_evidence: Sequence[Mapping] = (),
                    links: Sequence[Mapping] = (), corpus_version: str = "", run_id: str = "",
                    trace_id: str = "", now=None, high_stakes=None, write: bool = True,
                    judge=None) -> dict:
    """复验**一条**记忆（P10-03 的唯一入口）：闸门 → 候选 → Phase 03 核验 → 出口 → 写库。

    出口与后续动作（全部可复算）：
      · `REVALIDATED`：本轮证据判 SUPPORTED → 重新绑定证据行、刷新 `last_verified_at`/置信/
        TTL（`STALE`/`EXPIRED` 回 `ACTIVE`）、`verified_evidence=True`（语义 = 证据引用）；
      · `REFRESHED_NO_CHANGE`：闸门 ALLOW → **不刷新时间戳**（否则衰减永远不走）；
      · `REFUTED` / `UNVERIFIED` / `NO_CANDIDATE_EVIDENCE`：不写记忆本体，只留痕，
        交给 P10-04 的矛盾裁决与 P10-06 的高危钩子；
      · `BLOCKED_BY_GATE` / `SKIPPED_NOT_ACTIVE`：闸门不允许，一条证据都不看。
    `high_stakes` 且未复验成功的记忆会被高危钩子降级（`ACTIVE → STALE`，理由
    `HIGH_RISK_UNREVALIDATED`）—— 记忆是加速器不是真理（MASTER_RULES 12）。
    """
    item = item if isinstance(item, Mapping) else {}
    memory_id = str(item.get("memory_id") or "")
    moment = _now_dt(now)
    run_id = str(run_id or "")
    links = [row for row in (links or []) if isinstance(row, Mapping)]
    # 脏数据（库里取值不在契约枚举内）归一化后入回执，原值留在 detail（回执必须恒可校验）
    status_before_raw = str(item.get("status") or "")
    status_before = status_before_raw if status_before_raw in MEMORY_STATUSES else ""
    memory_type_raw = str(item.get("memory_type") or "")
    memory_type = memory_type_raw if memory_type_raw in MEMORY_TYPES else ""
    version_state = source_version_state(item, links, current_evidence=current_evidence,
                                         corpus_version=corpus_version)
    gate = freshness_gate(item, now=moment, source_version_changed=version_state["changed"],
                          high_stakes=high_stakes)
    stakes = high_stakes_of(item, stakes=high_stakes)
    outcome, reason = "", ""
    evidence_refs: list = []
    verdicts: dict = {}
    candidates: list = []
    written = False
    promoted = False
    refuting_hit: dict = {}
    confidence_after = float(item.get("confidence") or 0)
    valid_until_after = str(item.get("valid_until") or "")
    status_after = status_before

    if gate["decision"] == "BLOCK":
        outcome = "SKIPPED_NOT_ACTIVE" if gate["reason"] == "NOT_ACTIVE" else "BLOCKED_BY_GATE"
        reason = "MEMORY_NOT_ACTIVE" if outcome == "SKIPPED_NOT_ACTIVE" else "GATE_BLOCKED"
    elif gate["decision"] == "ALLOW":
        outcome, reason = "REFRESHED_NO_CHANGE", "FRESHNESS_NOT_DUE"
    else:
        candidates = _candidate_evidence(item, current_evidence, links)
        if not candidates:
            outcome, reason = "NO_CANDIDATE_EVIDENCE", "NO_EVIDENCE_AVAILABLE"
        else:
            supporting, refuting = [], []
            for candidate in candidates:
                verification = verify_evidence_item(
                    candidate["evidence"], claim_text=str(item.get("canonical_content") or ""),
                    now=moment)
                verdict = str(verification.get("verdict") or VERDICT_UNVERIFIED)
                verdicts[verdict] = verdicts.get(verdict, 0) + 1
                if verdict == VERDICT_SUPPORTED:
                    supporting.append((candidate, verification))
                elif verdict == VERDICT_REFUTED:
                    refuting.append((candidate, verification))
            if supporting:
                outcome, reason = "REVALIDATED", "EVIDENCE_STILL_SUPPORTS"
                evidence_refs = sorted({row[0]["evidence_ref"] for row in supporting})
                best = max(float(row[1].get("score") or 0) for row in supporting)
                confidence_after = round(min(1.0, max(confidence_after, best)), 6)
                ttl_days = int(TTL_DAYS.get(str(item.get("freshness_class") or "MEDIUM"), 0) or 0)
                if ttl_days > 0:
                    valid_until_after = _stamp(moment + timedelta(days=ttl_days))
                status_after = "ACTIVE" if status_before in ("STALE", "EXPIRED") else status_before
                promoted = status_before in ("STALE", "EXPIRED")
            elif refuting:
                outcome, reason = "REFUTED", "EVIDENCE_REFUTES"
                # 反证的那条证据要带进回执：P10-04 的 `memory_evidence` 矛盾靠它构成右侧
                candidate, verification = refuting[0]
                refuting_hit = {"evidence_ref": candidate["evidence_ref"],
                                "verification": verification, "evidence": candidate["evidence"]}
            else:
                outcome, reason = "UNVERIFIED", "EVIDENCE_INSUFFICIENT"

    # 可插拔判定器：**只能否决提升**（安全约束见 register_revalidation_judge）
    judge_used, judge_fallback = "rule", ""
    chooser = judge
    if chooser is None and judge_name() != "rule":
        chooser = _REVALIDATION_JUDGES.get(judge_name())
        if chooser is None:
            judge_fallback = "judge_not_registered:%s" % judge_name()
    if chooser is not None and outcome == "REVALIDATED":
        try:
            produced = chooser({"memory": dict(item), "outcome": outcome, "reason": reason,
                                "evidence_refs": list(evidence_refs), "verdicts": dict(verdicts)})
            if isinstance(produced, Mapping) and str(produced.get("outcome") or "") in \
                    MEMORY_REVALIDATION_OUTCOMES:
                judge_used = "injected:%s" % (judge_name() or "inline")
                if str(produced["outcome"]) != "REVALIDATED":
                    outcome = str(produced["outcome"])
                    reason = str(produced.get("reason") or reason)
                    if reason not in MEMORY_REVALIDATION_REASONS:
                        reason = "EVIDENCE_INSUFFICIENT"
                    evidence_refs, promoted = [], False
            else:
                judge_fallback = judge_fallback or "judge_returned_invalid_payload"
        except Exception as exc:      # noqa: BLE001 —— 注入点坏掉绝不能打断复验
            judge_fallback = judge_fallback or "judge_error:%s" % type(exc).__name__

    if outcome == "REVALIDATED" and (not verdicts or verdicts.get(VERDICT_SUPPORTED, 0) < 1):
        # 硬保险：没有 Phase 03 的 SUPPORTED，任何路径都不许产出"已绑上已验证证据"
        outcome, reason, evidence_refs, promoted = "UNVERIFIED", "EVIDENCE_INSUFFICIENT", [], False

    # 高危钩子：高危且未复验成功 → 降级为 STALE（不可作为证据，除非后续复验成功）
    hook = {"applied": False, "action": "NONE", "reasons": list(stakes.get("reasons") or [])}
    if stakes.get("high_stakes") and outcome in ("UNVERIFIED", "NO_CANDIDATE_EVIDENCE"):
        if status_before == "ACTIVE":
            hook.update({"applied": True, "action": "DOWNGRADE_TO_STALE",
                         "reason": "HIGH_RISK_UNREVALIDATED"})
        else:
            hook.update({"action": "ALREADY_NOT_ACTIVE"})
    elif stakes.get("high_stakes") and outcome == "REVALIDATED":
        hook.update({"action": "REVALIDATED_HIGH_STAKES"})

    if write and store is not None:
        if outcome == "REVALIDATED":
            link_rows = [_link_row_for(candidate["evidence"], verification, run_id=run_id,
                                       stage="memory_revalidation",
                                       corpus_version=corpus_version)
                         for candidate, verification in supporting]
            if link_rows:
                store.link_memory_evidence(memory_id, link_rows)
            applied = store.apply_memory_revalidation(
                memory_id, status=status_after, confidence=confidence_after,
                last_verified_at=_stamp(moment), valid_until=valid_until_after,
                reason=reason, run_id=run_id,
                payload={"evidence_refs": evidence_refs, "verdicts": verdicts,
                         "revalidation_version": MEMORY_REVALIDATION_VERSION})
            written = bool(applied.get("changed"))
        elif hook.get("applied"):
            applied = _apply_status(store, memory_id, "STALE", reason="HIGH_RISK_UNREVALIDATED",
                                    run_id=run_id, now=moment)
            written = bool(applied.get("changed")) and not applied.get("error")
        status_after = status_before if outcome != "REVALIDATED" else status_after
        if hook.get("applied"):
            status_after = "STALE"

    validation_id = _validation_id(memory_id=memory_id, run_id=run_id, outcome=outcome,
                                   evidence_refs=evidence_refs, gate=gate["decision"],
                                   status_before=status_before)
    receipt = {
        "validation_id": validation_id,
        "revalidation_version": MEMORY_REVALIDATION_VERSION,
        "memory_id": memory_id,
        "memory_type": memory_type,
        "run_id": run_id,
        "trace_id": str(trace_id or ""),
        "outcome": outcome,
        "reason": reason,
        "gate_decision": gate["decision"],
        "gate_reason": gate["reason"],
        "status_before": status_before,
        "status_after": status_after,
        "freshness": gate,
        "source_version": version_state,
        "evidence_refs": evidence_refs,
        "verdicts": verdicts,
        "verified_evidence": bool(outcome == "REVALIDATED" and evidence_refs),
        "verified_scope": "evidence_refs",
        "promoted": bool(promoted),
        "high_stakes": bool(stakes.get("high_stakes")),
        "judge": judge_used,
        "candidates": len(candidates),
        "refuting_evidence_ref": str(refuting_hit.get("evidence_ref") or ""),
        "refuting_verification": dict(refuting_hit.get("verification") or {}),
        "refuting_evidence": dict(refuting_hit.get("evidence") or {}),
        "written": bool(written),
        "high_risk_hook": hook,
        "judge_fallback": judge_fallback,
        "raw": {"status": status_before_raw, "memory_type": memory_type_raw},
        "detail": ("复验出口 %s（%s）；闸门 %s/%s；候选 %d；verdict %s；证据引用 %s"
                   % (outcome, reason, gate["decision"], gate["reason"], len(candidates),
                      verdicts or "{}", ",".join(evidence_refs) or "无")),
    }
    ok, note = validate_contract("memory_revalidation", dict(receipt))
    receipt["contract_ok"] = bool(ok)
    if not ok:
        receipt["contract_error"] = note
    if write and store is not None:
        try:
            store.record_memory_validation([{
                "validation_id": validation_id, "memory_id": memory_id, "run_id": run_id,
                "trace_id": str(trace_id or ""), "outcome": outcome, "reason": reason,
                "gate_decision": gate["decision"], "gate_reason": gate["reason"],
                "status_before": status_before, "status_after": status_after,
                "verdicts": verdicts, "evidence_refs": evidence_refs,
                "verified_evidence": receipt["verified_evidence"], "promoted": promoted,
                "high_stakes": bool(stakes.get("high_stakes")), "judge": judge_used,
                "revalidation_version": MEMORY_REVALIDATION_VERSION,
                "gate_version": MEMORY_FRESHNESS_GATE_VERSION,
                "metadata": {"source_version": version_state, "high_risk_hook": hook,
                             "candidates": len(candidates)},
            }])
        except Exception:      # noqa: BLE001 —— 留痕失败不影响复验结论
            pass
    return receipt


def revalidated_hint(hit: Mapping, validation: Mapping) -> dict:
    """把召回命中按复验结论改写（§2.1 的"提示 → 经闸门与 Verifier 后才可进证据图"）。

    只有 `REVALIDATED` 才把 `requires_revalidation` 置 False、`verified_evidence` 置 True，
    并且**必须**带上 `verified_scope="evidence_refs"`（记忆正文永远不是证据）。
    """
    hit = dict(hit) if isinstance(hit, Mapping) else {}
    validation = validation if isinstance(validation, Mapping) else {}
    outcome = str(validation.get("outcome") or "")
    revalidated = outcome == "REVALIDATED"
    updated = dict(hit)
    updated.update({
        "requires_revalidation": not revalidated,
        "verified_evidence": bool(revalidated and validation.get("evidence_refs")),
        "verified_scope": "evidence_refs" if revalidated else "",
        "revalidation": {"outcome": outcome, "reason": str(validation.get("reason") or ""),
                         "validation_id": str(validation.get("validation_id") or ""),
                         "gate_decision": str(validation.get("gate_decision") or ""),
                         "gate_reason": str(validation.get("gate_reason") or ""),
                         "status_after": str(validation.get("status_after") or ""),
                         "verified_evidence_refs": list(validation.get("evidence_refs") or [])},
        "evidence_refs": sorted({*(str(value) for value in (hit.get("evidence_refs") or [])),
                                 *(str(value) for value in (validation.get("evidence_refs") or []))}
                                - {""}),
    })
    return updated


def revalidate(store, *, memory_ids: Iterable = (), hints: Sequence[Mapping] = (),
               current_evidence: Sequence[Mapping] = (), evidence_links: Sequence[Mapping] = (),
               corpus_version: str = "", run_id: str = "", trace_id: str = "", now=None,
               write: bool = True, limit: int = 500) -> dict:
    """批量复验（P10-03 的批量入口）：吃记忆 id 或召回命中，返回**分布回执**。

    口径：id 集合 = 显式 id ∪ 命中里的 memory_id（去重排序 → 同输入同输出）；
    记忆与证据绑定行从库里读（`memory_evidence_link`），候选证据只用调用方给的
    `current_evidence`（本轮证据图里的证据）。
    """
    moment = _now_dt(now)
    ids = {str(item) for item in (memory_ids or []) if str(item or "")}
    hits_by_id = {}
    for hit in hints or []:
        if not isinstance(hit, Mapping):
            continue
        memory_id = str(hit.get("memory_id") or "")
        if memory_id:
            ids.add(memory_id)
            hits_by_id.setdefault(memory_id, hit)
    ordered = sorted(ids)[:max(1, int(limit))]
    items = []
    if store is not None and ordered:
        items = store.load_memory_items(memory_ids=ordered, include_all_scopes=True, limit=len(ordered))
    items_by_id = {str(item.get("memory_id") or ""): item for item in items or []}
    links_by_id: dict = {}
    if store is not None and ordered:
        try:
            for row in store.memory_evidence(memory_ids=ordered) or []:
                links_by_id.setdefault(str(row.get("memory_id") or ""), []).append(row)
        except Exception:      # noqa: BLE001
            links_by_id = {}
    validations, updated_hints = [], []
    for memory_id in ordered:
        item = items_by_id.get(memory_id)
        if item is None:
            validations.append({"validation_id": _validation_id(
                memory_id=memory_id, run_id=run_id, outcome="NO_CANDIDATE_EVIDENCE",
                evidence_refs=(), gate="MISSING", status_before=""),
                "revalidation_version": MEMORY_REVALIDATION_VERSION, "memory_id": memory_id,
                "run_id": str(run_id or ""), "outcome": "NO_CANDIDATE_EVIDENCE",
                "reason": "NO_EVIDENCE_AVAILABLE", "gate_decision": "BLOCK",
                "gate_reason": "NOT_ACTIVE", "status_before": "", "status_after": "",
                "freshness": {}, "source_version": {}, "evidence_refs": [], "verdicts": {},
                "verified_evidence": False, "verified_scope": "evidence_refs", "promoted": False,
                "high_stakes": False, "judge": "rule", "candidates": 0, "written": False,
                "high_risk_hook": {"applied": False}, "error": "MEMORY_NOT_FOUND"})
            continue
        receipt = revalidate_item(store, item, current_evidence=current_evidence,
                                  links=links_by_id.get(memory_id) or links_for(evidence_links,
                                                                               memory_id),
                                  corpus_version=corpus_version, run_id=run_id,
                                  trace_id=trace_id, now=moment, write=write)
        validations.append(receipt)
        if memory_id in hits_by_id:
            updated_hints.append(revalidated_hint(hits_by_id[memory_id], receipt))
    report = {
        "revalidation_version": MEMORY_REVALIDATION_VERSION,
        "gate_version": MEMORY_FRESHNESS_GATE_VERSION,
        "source_version_check": MEMORY_SOURCE_VERSION_VERSION,
        "contradiction_version": MEMORY_CONTRADICTION_VERSION,
        "checked": len(validations),
        "gate_decisions": _count(row.get("gate_decision") for row in validations),
        "gate_reasons": _count(row.get("gate_reason") for row in validations),
        "outcomes": _count(row.get("outcome") for row in validations),
        "reasons": _count(row.get("reason") for row in validations),
        "source_version": {"changed": sum(1 for row in validations
                                          if (row.get("source_version") or {}).get("changed")),
                           "reasons": _count(reason for row in validations
                                             for reason in (row.get("source_version") or {})
                                             .get("reasons") or [])},
        "revalidated": sum(1 for row in validations if row.get("outcome") == "REVALIDATED"),
        "promoted": sum(1 for row in validations if row.get("promoted")),
        "high_risk": {"high_stakes": sum(1 for row in validations if row.get("high_stakes")),
                      "hooked": sum(1 for row in validations
                                    if (row.get("high_risk_hook") or {}).get("applied")),
                      "actions": _count((row.get("high_risk_hook") or {}).get("action")
                                        for row in validations)},
        "written": sum(1 for row in validations if row.get("written")),
        "validations": validations,
        "hints": updated_hints,
        "note": ("复验只用本轮已有证据（不发明检索）；只有 Phase 03 判 SUPPORTED 才提升；"
                 "记忆正文永远不是证据（verified_scope=evidence_refs）"),
    }
    ok, note = validate_contract("memory_revalidation_report", dict(report))
    report["contract_ok"] = bool(ok)
    if not ok:
        report["contract_error"] = note
    return report


def links_for(links: Sequence[Mapping], memory_id: str) -> list:
    """从一整批绑定行里挑出某条记忆的（调用方已自带全部行时用）。"""
    return [row for row in (links or [])
            if isinstance(row, Mapping) and str(row.get("memory_id") or "") == str(memory_id or "")]


# ── P10-04：MemoryContradiction ────────────────────────────────────────────

def side_metrics(*, links: Sequence[Mapping] = (), item: Mapping | None = None,
                 verification: Mapping | None = None, evidence: Mapping | None = None) -> dict:
    """把"一侧"（一条记忆 / 一份证据）折算成 **Phase 06 同名的可比指标**。

    字段与 `qa_evidence_graph._side_metrics` 一一对应（mass / verified_mass / effective_mass /
    authority / independence / latest / claim_latest / claim_type …），这样裁决器拿到的输入
    与证据图阶段完全同构 —— **不另算一套质量判断**。折算口径：
      · `mass` = 该侧证据行的 `evidence_score` 之和（没有分数时按 0.5 计，明确记录）；
      · `verified_mass` = 其中 Phase 03 判 SUPPORTED 的那些的分数之和；
      · `authority` = 证据行里记录的最大 `authority_level`（memory_evidence_link.payload_json）；
      · `independence` = 不同来源指纹数；
      · `latest` = 证据发布日期最大值；`claim_latest` = 该侧结论的生效时间（记忆 `valid_from`）。
    """
    item = item if isinstance(item, Mapping) else {}
    rows = [row for row in (links or []) if isinstance(row, Mapping)]
    mass, verified_mass, authority, latest = 0.0, 0.0, 0, None
    sources = set()
    for row in rows:
        score = float(row.get("evidence_score") or 0) or 0.5
        mass += score
        if str(row.get("verdict") or "") == EVIDENCE_STATUS_SUPPORTED:
            verified_mass += score
        metadata = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
        try:
            authority = max(authority, int(metadata.get("authority_level") or 0))
        except (TypeError, ValueError):
            authority = max(authority, 0)
        published = _parse_time(metadata.get("published_at"))
        if published and (latest is None or published > latest):
            latest = published
        fingerprint = str(row.get("source_fingerprint") or "")
        if fingerprint:
            sources.add(fingerprint)
    if evidence is not None:
        layer = evidence_object(evidence)
        source = layer.get("source") if isinstance(layer.get("source"), Mapping) else {}
        score = float((verification or {}).get("score") or evidence.get("score") or 0) or 0.5
        mass += score
        if str((verification or {}).get("verdict") or "") == VERDICT_SUPPORTED:
            verified_mass += score
        try:
            authority = max(authority, int(evidence.get("authority_level") or 0))
        except (TypeError, ValueError):
            authority = authority
        published = _parse_time(evidence.get("published_at") or source.get("published_at"))
        if published and (latest is None or published > latest):
            latest = published
        fingerprint = str(layer.get("source_fingerprint") or "")
        if fingerprint:
            sources.add(fingerprint)
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    # 生效时间只取"这条说法什么时候成立/我们什么时候知道它"：
    # `valid_from` → `metadata.valid_from` → `created_at`。**刻意不用 `last_verified_at`** ——
    # 那是"上次复验时间"，拿它当生效时间会让"刚复验过的记忆"自动变成最新版本，
    # 与 §11「禁止最新自动覆盖」的意图正好相反（真机数据上这条造成过 8 条假取代）。
    claim_latest = _parse_time(item.get("valid_from") or metadata.get("valid_from")
                               or item.get("created_at"))
    return {
        "edge_count": len(rows) + (1 if evidence is not None else 0),
        "mass": round(mass, 4),
        "verified_mass": round(verified_mass, 4),
        "effective_mass": round(verified_mass if verified_mass > 0 else mass, 4),
        "authority": int(authority),
        "independence": len(sources),
        "latest": latest.isoformat(timespec="seconds") if latest else "",
        "claim_latest": claim_latest.isoformat(timespec="seconds") if claim_latest else "",
        "claim_type": str(metadata.get("claim_type") or item.get("claim_type") or ""),
    }


def _conflict_type(left_item: Mapping, right_item: Mapping, *, kind: str,
                   verification: Mapping | None = None) -> str:
    """矛盾类型（复用 Phase 03 的检查器口径，不新造判定）：
      · `memory_evidence` 且核验的检查里有一条**数字缺失/不符** → `method_difference`（不许取平均）；
      · 两侧时效作用域不同（`scope` 不同）→ `scope_difference`（两份都成立）；
      · 其余 → `claim_conflict`（结论级冲突）。
    """
    if verification is not None:
        checks = verification.get("checks") or []
        for check in checks:
            if isinstance(check, Mapping) and check.get("check") == "numbers" \
                    and check.get("applicable") and not check.get("passed"):
                return "method_difference"
        return "claim_conflict"
    left_scope = str((left_item or {}).get("scope") or "")
    right_scope = str((right_item or {}).get("scope") or "")
    if left_scope and right_scope and left_scope != right_scope:
        return "scope_difference"
    return "claim_conflict"


def _status_action(resolution: str, reason_code: str, winner: str, *, left_id: str = "",
                   right_id: str = "") -> tuple:
    """裁决结果 → 状态动作（§11：**只有时间裁决能取代**，且必须有后继记忆）。

    返回 `(action, target_status, loser_memory_id, winner_memory_id)`：
      · `SCOPE_DIFFERENCE` → `KEEP_BOTH`（两份都成立，不动状态）；
      · 未消解 → `CONTRADICT` 双方（`loser_memory_id` 留空，由调用方把两侧都标上）；
      · 时间裁决（`NEWER_VERSION_PRECEDES`）→ 败方 `SUPERSEDE` 到胜方；
      · 其余可裁理由码 → 败方 `CONTRADICTED`。
    胜方可能是左也可能是右（pair 的顺序只按 memory_id 排，与时间无关），所以两侧对称处理。
    """
    if reason_code == RESOLUTION_SCOPE_DIFFERENCE:
        return "KEEP_BOTH", "", "", ""
    if resolution != "resolved":
        return "CONTRADICT", unresolved_conflict_status(), "", ""
    if winner == "left":
        loser_id, winner_id = right_id, left_id
    elif winner == "right":
        loser_id, winner_id = left_id, right_id
    else:
        return "NONE", "", "", ""
    if not loser_id or loser_id == winner_id:
        return "NONE", "", "", ""
    if reason_code == RESOLUTION_NEWER_VERSION:
        return "SUPERSEDE", "SUPERSEDED", loser_id, winner_id
    return "CONTRADICT", "CONTRADICTED", loser_id, winner_id


def contradiction_id(*, kind: str, left: str, right: str, refs: Sequence[str] = ()) -> str:
    return "MCT" + _digest("|".join((kind, left, right, ",".join(sorted(str(x) for x in refs)))))


def adjudicate_memory_contradiction(kind: str, *, left_item: Mapping, left_links: Sequence[Mapping],
                                    right_item: Mapping | None = None, right_links: Sequence[Mapping] = (),
                                    right_evidence: Mapping | None = None,
                                    right_verification: Mapping | None = None,
                                    memory_id: str = "", evidence_ref: str = "",
                                    conflict_type: str = "", run_id: str = "") -> dict:
    """裁决一条记忆矛盾：**决策完全交给 Phase 06 的 `resolve()`**，本函数只做折算与落状态。

    返回矛盾回执（契约 `memory_contradiction`）；`resolution`/`reason_code`/`decider` 原样来自
    Phase 06（`rule:qa-contradiction-resolver-v1`），任何注入的裁决器都由 Phase 06 的
    注册表与回落逻辑处理 —— 本模块**不重复实现** §15 的八项比较。
    """
    left_item = left_item if isinstance(left_item, Mapping) else {}
    right_item = right_item if isinstance(right_item, Mapping) else {}
    left_metrics = side_metrics(links=left_links, item=left_item)
    right_metrics = side_metrics(links=right_links, item=right_item, verification=right_verification,
                                 evidence=right_evidence)
    conflict_type = str(conflict_type or "") or _conflict_type(
        left_item, right_item, kind=kind, verification=right_verification)
    contradiction = {
        "contradiction_id": contradiction_id(kind=kind,
                                             left=str(left_item.get("memory_id") or ""),
                                             right=str(right_item.get("memory_id") or "")
                                             or str(evidence_ref or ""),
                                             refs=[evidence_ref] if evidence_ref else []),
        "kind": kind, "conflict_type": conflict_type,
        "left": left_metrics, "right": right_metrics,
        "inputs": {"left": left_metrics, "right": right_metrics, "conflict_type": conflict_type,
                   "left_memory_id": str(left_item.get("memory_id") or ""),
                   "right_memory_id": str(right_item.get("memory_id") or ""),
                   "right_evidence_ref": str(evidence_ref or "")},
    }
    decision = resolve_contradiction(contradiction)
    resolution = str(decision.get("resolution") or "unresolved")
    reason_code = str(decision.get("reason_code") or RESOLUTION_NO_DECISIVE_RULE)
    winner = str((decision.get("winner") or {}).get("side") or "")
    left_id = str(left_item.get("memory_id") or "")
    right_id = str(right_item.get("memory_id") or "")
    action, target, loser_id, winner_id = _status_action(
        resolution, reason_code, winner, left_id=left_id, right_id=right_id)
    supersede_to = winner_id if action == "SUPERSEDE" else ""
    degraded = False
    if action == "SUPERSEDE" and not supersede_to:
        # §11 的 SUPERSEDED_BY **必须指向后继记忆**；后继不是记忆时退化为 CONTRADICTED
        action, target = "CONTRADICT", "CONTRADICTED"
        loser_id = loser_id or supersede_to
        degraded = True
    rationale = str(decision.get("rationale") or "")[:2000]
    if degraded:
        rationale += ("（本侧的「较新一方」是**证据**而不是记忆，没有可指向的后继记忆，"
                      "按 CONTRADICTED 处理：SUPERSEDED_BY 不允许悬空）")
    return {
        "contradiction_id": contradiction["contradiction_id"],
        "contradiction_version": MEMORY_CONTRADICTION_VERSION,
        "kind": kind,
        "conflict_type": conflict_type,
        "left_memory_id": left_id,
        "right_memory_id": right_id,
        "right_evidence_ref": str(evidence_ref or ""),
        "resolution": resolution if resolution in MEMORY_CONTRADICTION_OUTCOMES else "unresolved",
        "reason_code": reason_code,
        "decider": str(decision.get("decider") or ""),
        "rationale": rationale,
        "status_action": action if action in MEMORY_CONTRADICTION_STATUS_ACTIONS else "NONE",
        "status_target": target,
        "supersede_to": supersede_to,
        "loser_memory_id": loser_id,
        "winner_memory_id": winner_id,
        "winner_metrics": (left_metrics if winner == "left" else
                           (right_metrics if winner == "right" else {})),
        "supersede_degraded": degraded,
        "winner": winner,
        "left": left_metrics,
        "right": right_metrics,
        "run_id": str(run_id or ""),
        "status_actions": [],
    }


def _pair_candidates(items: Sequence[Mapping], entities: Mapping, links_by_id: Mapping) -> list:
    """挑**可能互相冲突**的记忆对（确定性、有上限）：

    前置条件（四个都要）：
      ① 两条都是**断言型**记忆（`contradiction_types()`，默认只有 `VERIFIED_CLAIM`）——
         实体记忆只是索引，不参与对错判定；
      ② 主体一致：实体键集合 Jaccard ≥ `contradiction_jaccard()`（默认 0.5）；
      ③ 命题一致：词面重叠 ≥ `contradiction_overlap()`（默认 0.5）；
      ④ Phase 03 的 `check_negation()` 判出"否定极性相反且指向同一话题"（`anchored`）——
         这才算"一条支持、一条反驳"（§11 的 M1/M2）。
    """
    pairs = []
    eligible = set(contradiction_types())
    candidates = [item for item in items or []
                  if str(item.get("memory_type") or "") in eligible]
    ids = sorted(str(item.get("memory_id") or "") for item in candidates)
    by_id = {str(item.get("memory_id") or ""): item for item in candidates}
    overlap_floor = contradiction_overlap()
    jaccard_floor = contradiction_jaccard()
    for index, left_id in enumerate(ids):
        left = by_id.get(left_id) or {}
        left_entities = set(entities.get(left_id) or [])
        left_terms = term_set(str(left.get("canonical_content") or ""), sizes=(2, 3))
        for right_id in ids[index + 1:]:
            right = by_id.get(right_id) or {}
            right_entities = set(entities.get(right_id) or [])
            union = left_entities | right_entities
            shared = left_entities & right_entities
            jaccard = (len(shared) / float(len(union))) if union else 0.0
            right_terms = term_set(str(right.get("canonical_content") or ""), sizes=(2, 3))
            overlap = (len(left_terms & right_terms) / float(len(left_terms))) if left_terms else 0.0
            if union:
                subject_ok = jaccard >= jaccard_floor
            else:
                # 两侧都没有实体信息 → 主体一致性**无法判定**：只认"近乎逐字相同"的命题
                # （没有实体还敢配对，等于把长文里的一次否定词当成反证，这正是真机的假矛盾）
                subject_ok = overlap >= max(overlap_floor, UNKNOWN_SUBJECT_OVERLAP)
            if not subject_ok or overlap < overlap_floor:
                continue
            negation = check_negation(str(left.get("canonical_content") or ""),
                                      str(right.get("canonical_content") or ""))
            if negation.get("passed") or not negation.get("anchored"):
                continue
            numbers_left = check_numbers(str(left.get("canonical_content") or ""),
                                         str(right.get("canonical_content") or ""))
            conflict_type = _conflict_type(left, right, kind="memory_memory")
            if conflict_type == "claim_conflict" and numbers_left.get("missing"):
                conflict_type = "method_difference"
            pairs.append({"left_id": left_id, "right_id": right_id,
                          "shared_entities": sorted(shared),
                          "entity_jaccard": round(jaccard, 6),
                          "overlap": round(overlap, 6),
                          "conflict_type": conflict_type,
                          "negation": {"claim_negated": negation.get("claim_negated"),
                                       "evidence_negated": negation.get("evidence_negated"),
                                       "detail": negation.get("detail")}})
            if len(pairs) >= MAX_CONTRADICTION_PAIRS:
                return pairs
    return pairs


def _prefer_successor(candidate: Mapping, current: Mapping) -> bool:
    """两个候选后继谁更"规范"：生效时间更晚的优先；并列时 memory_id 更小的优先。

    纯函数、可复算 —— 这是"一条记忆只能有一个后继"的确定性挑选规则（§11 禁止最新自动覆盖，
    但"谁取代谁"必须唯一，否则 `superseded_by` 会随配对顺序漂移）。
    """
    left_clock = str(candidate.get("clock") or "")
    right_clock = str(current.get("clock") or "")
    if left_clock != right_clock:
        return left_clock > right_clock
    return str(candidate.get("successor") or "") < str(current.get("successor") or "")


def detect_memory_contradictions(store, *, items: Sequence[Mapping] = (), validations: Sequence[Mapping] = (),
                                 run_id: str = "", now=None, write: bool = True,
                                 max_items: int = 500) -> dict:
    """P10-04：检出并裁决记忆矛盾（两族：`memory_evidence` 与 `memory_memory`）。

    输入：`items`（记忆条目，缺省从库读）+ `validations`（P10-03 的复验回执：其中
    `REFUTED` 的那些**就是**"本轮证据反驳了这条记忆"，直接构成 `memory_evidence` 矛盾）。
    输出：`{contradictions: [...], counts, supersessions, ...}`，全部落 `memory_contradiction`。
    """
    moment = _now_dt(now)
    rows = [item for item in (items or []) if isinstance(item, Mapping)]
    if not rows and store is not None:
        rows = store.load_memory_items(include_all_scopes=True, limit=max_items) or []
    by_id = {str(item.get("memory_id") or ""): item for item in rows}
    ids = sorted(by_id)
    links_by_id: dict = {}
    entities_by_id: dict = {}
    if store is not None and ids:
        try:
            for row in store.memory_evidence(memory_ids=ids) or []:
                links_by_id.setdefault(str(row.get("memory_id") or ""), []).append(row)
            for row in store.memory_entities(memory_ids=ids) or []:
                entities_by_id.setdefault(str(row.get("memory_id") or ""), []).append(
                    str(row.get("entity_key") or ""))
        except Exception:      # noqa: BLE001
            links_by_id, entities_by_id = {}, {}
    contradictions, supersessions = [], []
    skipped_by_type = 0
    eligible_types = set(contradiction_types())
    # ① 记忆 ↔ 本轮证据（P10-03 判 REFUTED 的那些）
    for validation in validations or []:
        if not isinstance(validation, Mapping) or str(validation.get("outcome")) != "REFUTED":
            continue
        memory_id = str(validation.get("memory_id") or "")
        item = by_id.get(memory_id)
        if item is None:
            continue
        if str(item.get("memory_type") or "") not in eligible_types:
            skipped_by_type += 1        # 索引类记忆不参与对错判定（见 CONTRADICTION_ELIGIBLE_TYPES）
            continue
        ref = str(validation.get("refuting_evidence_ref")
                  or (validation.get("evidence_refs") or [""])[0] or "")
        receipt = adjudicate_memory_contradiction(
            "memory_evidence", left_item=item, left_links=links_by_id.get(memory_id) or [],
            right_evidence=(validation.get("refuting_evidence") or None),
            right_verification=(validation.get("refuting_verification") or None),
            evidence_ref=ref, run_id=run_id)
        contradictions.append(receipt)
    # ② 记忆 ↔ 记忆（实体共享 + 否定极性相反且指向同一话题）
    for pair in _pair_candidates(rows, entities_by_id, links_by_id):
        left, right = by_id.get(pair["left_id"]) or {}, by_id.get(pair["right_id"]) or {}
        receipt = adjudicate_memory_contradiction(
            "memory_memory", left_item=left, left_links=links_by_id.get(pair["left_id"]) or [],
            right_item=right, right_links=links_by_id.get(pair["right_id"]) or [],
            conflict_type=pair["conflict_type"], run_id=run_id)
        receipt["pair"] = pair
        contradictions.append(receipt)
    # 状态动作：**先规划再落地**。一条记忆同一轮可能出现在多对矛盾里，而
    # `superseded_by` 只有一个字段 —— 所以每个败方只挑**一个规范后继**：
    # 生效时间最晚的那个（并列时取 memory_id 最小的那个），然后按 memory_id 排序逐条落地。
    # 这样同一时钟重放算出的后继与写入结果逐字相同（幂等），也不会让一个后覆盖另一个。
    plans: dict = {}
    for receipt in sorted(contradictions, key=lambda row: str(row.get("contradiction_id") or "")):
        action = str(receipt.get("status_action") or "NONE")
        if action == "SUPERSEDE" and receipt.get("loser_memory_id") and receipt.get("supersede_to"):
            loser = str(receipt["loser_memory_id"])
            metrics = receipt.get("winner_metrics") if isinstance(receipt.get("winner_metrics"),
                                                                 Mapping) else {}
            clock = str(metrics.get("claim_latest") or metrics.get("latest") or "")
            candidate = {"action": "SUPERSEDE", "target": "SUPERSEDED",
                         "successor": str(receipt["supersede_to"]), "clock": clock,
                         "reason": str(receipt.get("reason_code") or ""),
                         "rationale": str(receipt.get("rationale") or ""),
                         "contradiction_id": str(receipt.get("contradiction_id") or "")}
            current = plans.get(loser)
            if current is None or current.get("action") != "SUPERSEDE" or \
                    _prefer_successor(candidate, current):
                plans[loser] = candidate
            continue
        if action == "CONTRADICT":
            targets = [str(receipt.get("loser_memory_id") or "")] if receipt.get("loser_memory_id") \
                else [str(receipt.get("left_memory_id") or ""), str(receipt.get("right_memory_id") or "")]
            for memory_id in sorted({item for item in targets if item}):
                current = plans.get(memory_id)
                if current is not None and current.get("action") == "SUPERSEDE":
                    continue      # 取代优先于"标为矛盾"（更强的结论）
                plans.setdefault(memory_id, {
                    "action": "CONTRADICT", "target": str(receipt.get("status_target") or ""),
                    "successor": "", "clock": "", "reason": str(receipt.get("reason_code") or ""),
                    "rationale": str(receipt.get("rationale") or ""),
                    "contradiction_id": str(receipt.get("contradiction_id") or "")})
    by_contradiction: dict = {}
    for memory_id in sorted(plans):
        plan = plans[memory_id]
        if plan["action"] == "SUPERSEDE":
            outcome = supersede_memory(store, memory_id, plan["successor"],
                                       reason=plan["reason"] or RESOLUTION_NEWER_VERSION,
                                       rationale=plan["rationale"], run_id=run_id, now=moment,
                                       write=write)
            if outcome.get("changed"):
                supersessions.append(outcome)
        elif plan["action"] == "CONTRADICT" and plan["target"]:
            outcome = _apply_status(store, memory_id, plan["target"],
                                    reason=plan["reason"] or "CONTRADICTED_BY_CONFLICT",
                                    run_id=run_id, now=moment) if write else {
                "memory_id": memory_id, "changed": False, "dry_run": True}
            outcome["plan"] = {key: plan[key] for key in ("action", "target", "reason")}
        else:
            outcome = {"memory_id": memory_id, "changed": False, "error": "PLAN_NOT_APPLICABLE"}
        by_contradiction.setdefault(plan["contradiction_id"], []).append(outcome)
    for receipt in contradictions:
        for outcome in by_contradiction.get(str(receipt.get("contradiction_id") or ""), []):
            if outcome.get("memory_id") in (receipt.get("loser_memory_id"),
                                            receipt.get("left_memory_id"),
                                            receipt.get("right_memory_id")):
                receipt["status_actions"].append(outcome)
        receipt["planned_action"] = (plans.get(str(receipt.get("loser_memory_id") or ""))
                                     or {}).get("action", receipt["status_action"])
    persisted = 0
    if write and store is not None and contradictions:
        try:
            persisted = store.record_memory_contradiction(contradictions)
        except Exception:      # noqa: BLE001
            persisted = 0
    report = {
        "contradiction_version": MEMORY_CONTRADICTION_VERSION,
        "checked": len(rows),
        "eligible_types": sorted(eligible_types),
        "skipped_by_type": skipped_by_type,
        "candidates": len(contradictions),
        "persisted": persisted,
        "by_kind": _count(row.get("kind") for row in contradictions),
        "by_resolution": _count(row.get("resolution") for row in contradictions),
        "by_reason_code": _count(row.get("reason_code") for row in contradictions),
        "by_status_action": _count(row.get("status_action") for row in contradictions),
        "supersessions": len(supersessions),
        "superseded": [row.get("memory_id") for row in supersessions],
        "contradictions": contradictions,
        "note": ("裁决由 Phase 06 的规则裁决器给出（单一真源）；只有 NEWER_VERSION_PRECEDES "
                 "才 SUPERSEDE，且必须能指向后继记忆"),
    }
    return report


# ── P10-05：SUPERSEDED_BY ──────────────────────────────────────────────────

def supersede_memory(store, memory_id: str, superseded_by: str, *, reason: str = "",
                     rationale: str = "", run_id: str = "", now=None,
                     write: bool = True) -> dict:
    """P10-05：建立取代关系（§11 的 `M1--SUPERSEDED_BY→M2`）。

    本仓库的落地形态（契约里写死）：
      · `memory_item.superseded_by = M2`（字段级指向）；
      · `memory_item.status = 'SUPERSEDED'`（§2.3 六状态之一）；
      · `memory_relation` 一条 **M2 → M1** 的 `SUPERSEDES` 边（关系枚举冻结的九个取值里选，
        **不新增** `SUPERSEDED_BY` 这个边名）；
      · 追加 `memory_version`（不物理覆盖历史）。
    幂等：已经是"被同一个后继取代"时一行都不写；`REVOKED` 的记忆拒绝被取代。
    """
    reason = str(reason or RESOLUTION_NEWER_VERSION)
    if reason not in MEMORY_SUPERSESSION_REASONS:
        reason = "MANUAL_SUPERSEDE" if reason not in MEMORY_SUPERSESSION_REASONS else reason
    applied = store.mark_memory_superseded(memory_id, superseded_by=superseded_by, reason=reason,
                                           run_id=run_id, change="SUPERSEDE") if write \
        else {"memory_id": memory_id, "changed": False, "dry_run": True}
    relation_written = 0
    if write and applied.get("changed"):
        relation_written = store.add_memory_relation([{
            "memory_id": str(superseded_by), "relation": "SUPERSEDES",
            "target_memory_id": str(memory_id), "target_kind": "memory",
            "target_ref": str(memory_id), "weight": 1.0, "rationale": str(rationale or reason),
            "run_id": str(run_id or ""), "version": 1,
            "metadata": {"supersession_version": MEMORY_SUPERSESSION_VERSION,
                         "reason": reason}}])
    receipt = {
        "supersession_id": "MSUP" + _digest("|".join((str(memory_id), str(superseded_by), reason))),
        "supersession_version": MEMORY_SUPERSESSION_VERSION,
        "memory_id": str(memory_id or ""),
        # 生效的后继以**库里的实际值**为准（被拒绝时不许把请求值当成结果报出去）
        "superseded_by": str(applied.get("superseded_by") or superseded_by or ""),
        "relation": "SUPERSEDES",
        "reason": reason,
        "from_status": str(applied.get("from") or ""),
        "to_status": str(applied.get("to") or ""),
        "changed": bool(applied.get("changed")),
        "relation_rows": int(relation_written),
        "version": int(applied.get("version") or 0),
        "run_id": str(run_id or ""),
        "rationale": str(rationale or ""),
        "error": str(applied.get("error") or ""),
    }
    ok, note = validate_contract("memory_supersession", dict(receipt))
    receipt["contract_ok"] = bool(ok)
    if not ok:
        receipt["contract_error"] = note
    return receipt


def supersession_chains(store, *, memory_ids: Iterable = (), max_depth: int = 8) -> list:
    """复算取代链（验收口径）：`M1 -> M2 -> ...`，环与悬空指针都要能被查出来。

    判定用两个独立来源交叉验证（缺一不算成立）：
      ① `memory_item.superseded_by` 字段；② `memory_relation` 里的 `SUPERSEDES` 边（新→旧）。
    """
    ids = sorted({str(item) for item in (memory_ids or []) if str(item or "")})
    items = store.load_memory_items(memory_ids=ids, include_all_scopes=True,
                                    limit=max(1, len(ids) or 1)) if (store and ids) else []
    if not ids and store is not None:
        items = store.load_memory_items(include_all_scopes=True, limit=5000) or []
    by_id = {str(item.get("memory_id") or ""): item for item in items or []}
    relations = store.memory_relations(sorted(by_id), relations=["SUPERSEDES"]) if \
        (store and by_id) else []
    edges = {}
    for row in relations or []:
        edges.setdefault(str(row.get("target_memory_id") or ""), []).append(
            str(row.get("memory_id") or ""))
    chains, problems = [], []
    for memory_id in sorted(by_id):
        item = by_id[memory_id]
        target = str(item.get("superseded_by") or "")
        if not target and str(item.get("status") or "") != "SUPERSEDED":
            continue
        if not target:
            problems.append({"memory_id": memory_id, "issue": "SUPERSEDED_WITHOUT_TARGET"})
            continue
        if target not in edges.get(memory_id, []):
            problems.append({"memory_id": memory_id, "issue": "SUPERSEDED_BY_FIELD_WITHOUT_EDGE",
                             "superseded_by": target})
        chain, seen, cursor = [memory_id], {memory_id}, target
        while cursor and len(chain) <= max_depth:
            if cursor in seen:
                problems.append({"memory_id": memory_id, "issue": "SUPERSESSION_CYCLE",
                                 "at": cursor})
                break
            seen.add(cursor)
            chain.append(cursor)
            if cursor not in by_id:
                problems.append({"memory_id": memory_id, "issue": "SUPERSESSION_DANGLING",
                                 "at": cursor})
                break
            cursor = str(by_id[cursor].get("superseded_by") or "")
        chains.append({"memory_id": memory_id, "chain": chain,
                       "terminal": "active" if not cursor else "dangling"})
    return {"chains": chains, "problems": problems,
            "superseded": sum(1 for item in (items or [])
                              if str(item.get("status") or "") == "SUPERSEDED")}


# ── P10-06：revoke / high-risk hook ────────────────────────────────────────

def run_high_risk_hook(item: Mapping, *, stakes=None, outcome: str = "",
                       context: Mapping | None = None) -> dict:
    """高危钩子（P10-06 的机器形态）：高危记忆在"未复验成功"时的**降级动作**由它给出。

    纯规则、可复算：`high_stakes` 判定 → 出口映射 → 动作（`DOWNGRADE_TO_STALE` /
    `KEEP` / `REVALIDATED_HIGH_STAKES`）。**不调用任何模型**；
    将来要接模型判定时走 `register_revalidation_judge()` 注入点（默认仍不接）。
    """
    stakes = stakes if isinstance(stakes, Mapping) else high_stakes_of(item, stakes=stakes)
    context = context if isinstance(context, Mapping) else {}
    action, reason = "KEEP", ""
    if stakes.get("high_stakes"):
        if outcome == "REVALIDATED":
            action, reason = "KEEP", "REVALIDATED_HIGH_STAKES"
        elif outcome in ("UNVERIFIED", "NO_CANDIDATE_EVIDENCE"):
            action, reason = "DOWNGRADE_TO_STALE", "HIGH_RISK_UNREVALIDATED"
        elif outcome == "REFUTED":
            action, reason = "CONTRADICTION_HANDLES", "HIGH_RISK_REFUTED"
    return {"high_stakes": bool(stakes.get("high_stakes")),
            "reasons": list(stakes.get("reasons") or []),
            "action": action, "reason": reason, "outcome": str(outcome or ""),
            "judge": judge_name(), "context": dict(context)}


def revoke_memories(store, *, reason: str = "MANUAL_REVOKE", source_fingerprint: str = "",
                    entity_key: str = "", session_id: str = "", memory_ids: Iterable = (),
                    run_id: str = "", now=None, write: bool = True,
                    high_risk: bool | None = None) -> dict:
    """P10-06：按 source / entity / session / 显式 id 撤销污染记忆（§11 防污染）。

    选择器先取并集再撤销（次序确定：先显式 id，再来源、实体、会话命中的 id，全部排序去重）；
    每条被撤销的记忆都会：置 `REVOKED` + 追加版本行 + 一条 `EXPIRED_BY` 关系边
    （`target_kind` = source/entity/session，`target_ref` = 选择器取值）—— 撤销必须可追溯。
    已 REVOKED 的记忆跳过（幂等）；`REVOKED` 是终态（不能被复验复活）。
    """
    moment = _now_dt(now)
    reason = str(reason or "MANUAL_REVOKE")
    if reason not in MEMORY_REVOKE_REASONS:
        reason = "MANUAL_REVOKE"
    selected = {str(item) for item in (memory_ids or []) if str(item or "")}
    selector = {"memory_ids": sorted(selected), "source_fingerprint": str(source_fingerprint or ""),
                "entity_key": str(entity_key or ""), "session_id": str(session_id or "")}
    if store is not None:
        if source_fingerprint:
            for row in store.memory_evidence(source_fingerprints=[source_fingerprint]) or []:
                selected.add(str(row.get("memory_id") or ""))
        if entity_key:
            for row in store.memory_entities(entity_keys=[entity_key]) or []:
                selected.add(str(row.get("memory_id") or ""))
        if session_id:
            for item in store.load_memory_items(include_all_scopes=True, limit=5000) or []:
                if str(item.get("session_id") or "") == str(session_id):
                    selected.add(str(item.get("memory_id") or ""))
    ordered = sorted(item for item in selected if item)
    hook = run_high_risk_hook({}, stakes=high_risk, outcome="REVOKE",
                              context={"selector": selector}) if high_risk is not None else {}
    receipt = {"revoke_version": MEMORY_REVOKE_VERSION, "reason": reason, "selector": selector,
               "checked": len(ordered), "revoked": [], "skipped": [], "status_counts": {},
               "already_revoked": 0, "run_id": str(run_id or ""), "high_risk": hook,
               "checked_at": _stamp(moment)}
    if write and store is not None and ordered:
        applied = store.revoke_memory_items(ordered, reason=reason, run_id=run_id,
                                            payload={"selector": selector, "hook": hook})
        receipt["revoked"] = list(applied.get("revoked") or [])
        receipt["skipped"] = list(applied.get("skipped") or [])
        receipt["already_revoked"] = int(applied.get("already_revoked") or 0)
        receipt["error"] = str(applied.get("error") or "")
        target_kind = ("source" if source_fingerprint else
                       ("entity" if entity_key else ("session" if session_id else "manual")))
        target_ref = source_fingerprint or entity_key or session_id or ""
        if receipt["revoked"] and (target_ref or reason):
            store.add_memory_relation([{
                "memory_id": str(row.get("memory_id") or ""), "relation": "EXPIRED_BY",
                "target_kind": target_kind, "target_ref": target_ref or reason,
                "target_memory_id": "", "weight": 1.0,
                "rationale": "污染撤销：%s" % reason, "run_id": str(run_id or ""),
                "metadata": {"revoke_version": MEMORY_REVOKE_VERSION, "reason": reason}}
                for row in receipt["revoked"] if str(row.get("memory_id") or "")])
    items = store.load_memory_items(include_all_scopes=True, limit=5000) if store is not None else []
    receipt["status_counts"] = _count(item.get("status") for item in items or [])
    receipt["note"] = ("REVOKED 是终态（不可复活）；已撤销的记忆跳过不重复处理（幂等）；"
                       "撤销动作全部落 memory_version + EXPIRED_BY 关系边，可追溯")
    ok, note = validate_contract("memory_revoke_receipt", dict(receipt))
    receipt["contract_ok"] = bool(ok)
    if not ok:
        receipt["contract_error"] = note
    return receipt


# ── 总入口（管线与验收共用）────────────────────────────────────────────────

def run_revalidation(store, *, hints: Sequence[Mapping] = (), memory_ids: Iterable = (),
                     graph: Mapping | None = None, run_meta: Mapping | None = None,
                     current_evidence: Sequence[Mapping] = (), corpus_version: str = "",
                     run_id: str = "", trace_id: str = "", now=None, write: bool = True,
                     contradictions: bool = True, contradiction_scope: str = "touched") -> dict:
    """Phase 10 总入口：闸门 → 复验 → 矛盾/取代（+分布回执）。

    `current_evidence` 缺省时从 `graph["evidence"]` 取（本轮证据图里的证据，零新增检索）；
    `corpus_version` 缺省取 `run_meta["corpus_version"]`。
    `contradiction_scope`：`touched`（默认）只在本轮碰过的记忆之间找矛盾（管线里开销有界）；
    `all` 扫描全库（维护任务/验收统计用）。
    """
    run_meta = run_meta if isinstance(run_meta, Mapping) else {}
    graph = graph if isinstance(graph, Mapping) else {}
    run_id = str(run_id or run_meta.get("id") or "")
    corpus_version = str(corpus_version or run_meta.get("corpus_version") or "")
    evidence = list(current_evidence or [])
    if not evidence:
        evidence = [item for item in (graph.get("evidence") or []) if isinstance(item, Mapping)]
    report = revalidate(store, memory_ids=memory_ids, hints=hints, current_evidence=evidence,
                        corpus_version=corpus_version, run_id=run_id, trace_id=trace_id,
                        now=now, write=write)
    if contradictions:
        scope_items: list = []
        if str(contradiction_scope) != "all":
            touched = sorted({str(row.get("memory_id") or "") for row in report["validations"]
                              if str(row.get("memory_id") or "")})
            if touched and store is not None:
                scope_items = store.load_memory_items(memory_ids=touched,
                                                      include_all_scopes=True, limit=len(touched))
        conflict = detect_memory_contradictions(store, items=scope_items,
                                                validations=report["validations"],
                                                run_id=run_id, now=now, write=write)
        report["contradictions"] = {
            "candidates": conflict["candidates"], "by_kind": conflict["by_kind"],
            "by_resolution": conflict["by_resolution"],
            "by_reason_code": conflict["by_reason_code"],
            "by_status_action": conflict["by_status_action"],
            "supersessions": conflict["supersessions"],
            "persisted": conflict["persisted"],
            "details": conflict["contradictions"],
        }
        report["supersessions"] = conflict["supersessions"]
    else:
        report["contradictions"] = {"candidates": 0, "by_kind": {}, "by_resolution": {},
                                   "by_reason_code": {}, "by_status_action": {},
                                   "supersessions": 0, "persisted": 0, "details": []}
        report["supersessions"] = 0
    report["revocations"] = 0
    report["version"] = MEMORY_REVALIDATION_VERSION
    return report


def revalidation_receipt(report: Mapping) -> dict:
    """给 stats/SSE 的紧凑回执（不含记忆正文：只给计数、理由码与口径）。"""
    report = report if isinstance(report, Mapping) else {}
    conflict = report.get("contradictions") if isinstance(report.get("contradictions"), Mapping) \
        else {}
    return {
        "revalidation_version": report.get("revalidation_version"),
        "gate_version": report.get("gate_version"),
        "checked": int(report.get("checked") or 0),
        "gate_decisions": dict(report.get("gate_decisions") or {}),
        "gate_reasons": dict(report.get("gate_reasons") or {}),
        "outcomes": dict(report.get("outcomes") or {}),
        "reasons": dict(report.get("reasons") or {}),
        "revalidated": int(report.get("revalidated") or 0),
        "promoted": int(report.get("promoted") or 0),
        "source_version": dict(report.get("source_version") or {}),
        "high_risk": dict(report.get("high_risk") or {}),
        "contradictions": {
            "candidates": int(conflict.get("candidates") or 0),
            "by_kind": dict(conflict.get("by_kind") or {}),
            "by_reason_code": dict(conflict.get("by_reason_code") or {}),
            "by_status_action": dict(conflict.get("by_status_action") or {}),
            "supersessions": int(conflict.get("supersessions") or 0)},
        "note": report.get("note") or "",
    }


__all__ = [
    "CANDIDATE_TERM_OVERLAP", "CONTRADICTION_TERM_OVERLAP", "DEFAULT_HIGH_STAKES_CLAIM_TYPES",
    "DEFAULT_REVALIDATE_AGE_RATIO", "HIGH_STAKES_FRESHNESS_CLASSES", "MAX_CONTRADICTION_PAIRS",
    "REVALIDATE_FRESHNESS_CLASSES", "adjudicate_memory_contradiction", "contradiction_id",
    "detect_memory_contradictions", "freshness_gate", "freshness_required", "high_stakes_of",
    "judge_name", "latest_version_token", "register_revalidation_judge",
    "revalidate", "revalidate_age_ratio", "revalidate_item", "revalidated_hint",
    "revalidation_enabled", "revalidation_judges", "revalidation_receipt", "revoke_memories",
    "run_high_risk_hook", "run_revalidation", "side_metrics", "source_version_state",
    "supersede_memory", "supersession_chains", "unresolved_conflict_status", "version_tokens",
]

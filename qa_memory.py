#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 09（P09-01…P09-06）· Memory Graph Core：跨会话长期记忆 + Write Gate + Recall +
lifecycle + provenance/三通道召回（**纯规则/统计，零模型调用，零嵌入端点**）。

通用包 01_V2_ARCHITECTURE 依据：
  · §1.4 Memory Graph：节点十类、关系九个（逐字入 `qa_graph_contracts.MEMORY_*`）；
  · §2.1 **Memory 不能直接成为当前事实证据**：`Memory Hint → freshness/source-version/
    applicability gate → 必要时重新检索 → Verifier → 当前 Evidence Graph → Answer`
    —— 所以本模块召回出来的每一条**都是 MEMORY_HINT**（`hint=True`、
    `requires_revalidation=True`、`verified_evidence=False`），闸门与重验是 Phase 10 的账；
  · §2.2 Write Gate 四出口 `DROP / SESSION_ONLY / PERSIST / PERSIST_WITH_TTL`；
    **"未经 Evidence Verifier 的 LLM 自由总结禁止写成 VerifiedClaimMemory"**（MASTER_RULES 11）；
  · §2.3 生命周期字段与六状态；**旧 Memory 不物理覆盖，以版本关系保存演化**；
  · §3 六类记忆（本阶段只产 `VERIFIED_CLAIM` / `ENTITY` 两类，其余归属写在
    `MEMORY_TYPE_OWNER_PHASE`，写入门显式拒绝并记账 —— 不提前实现后续 Phase）；
  · §10 召回：`planning`（策略/失败/查询模式，避免旧事实污染规划）与 `evidence`
    （结论/来源，先标 MEMORY_HINT）；时效档 seven 值逐字入契约；
  · §11 两个公式逐字落地：`MemoryWriteUtility`（七项）与 `MemoryRecallScore`（六项）；
    **三层集合** seen（Phase 02 的 `qa_evidence_seen`）/ confirmed（Phase 03 核验）/
    remembered（本模块）；"不是所有 confirmed 都值得 remembered"；
  · §12 库表：本阶段建八张表（`memory_validation` / `memory_contradiction` 属 Phase 10，
    `skill_performance_memory` / `source_reliability_memory` 属 Phase 12，见 D-033）；
  · §14 作用域：五值逐字 + "当前状态类记忆不得无条件跨会话"（`scope_key` 把会话/轮次编码进去）；
  · MASTER_RULES 11/12/15/16：自由生成内容不得成事实记忆；Memory 是加速器不是真理；
    时效事实必须可重新验证；外部网页指令不得升级成系统规则。

复用（一行都没重写）：
  ① Phase 02 `qa_evidence`：`evidence_object()`（最小 span / 来源身份 / 指纹）、
     `source_fingerprint()`、`evidence_fingerprint()`、`node_key()`、`now_utc()`；
  ② Phase 03 `qa_verifier`：`verification_of()`（核验结论与分数）、`evidence_score()`、
     `term_set()`（"什么算实词"的唯一口径）；
  ③ Phase 04 `qa_hunters.load_article_vectors()`：**库内已有向量**（零端点调用）；
  ④ Phase 08 `qa_context_pack.make_context_item()`（P09 的 memory_context 段靠它成型）；
  ⑤ 冻结契约 `qa_contracts`（一个字段都没碰）与图谱契约 `qa_graph_contracts`。

边界与取舍（诚实声明，宁写 PARTIAL 不谎报）
------------------------------------------------
  1. **零模型调用**：§11 的十三个分量全部是**词面统计 + 时间衰减 + 计数**，
     没有 embedding、没有 LLM 总结。因此：
       · 记忆内容只能是**已核验 claim 的原文**（不是模型写的摘要）；
       · `semantic_relevance` 是词面/向量余弦（库内已有向量），召回不了"与记忆毫无
         词面与向量交集"的内容 —— 这是能力边界，不是"效果差"。
  2. **向量通道只用库内已有向量**：查询向量 = 词面种子记忆所链文章的向量质心
     （Phase 04 的同一手法与同一份数据），**绝不调嵌入端点**（GPU 机已停用）；
     没有种子/没有向量时降级为词面通道并记账（`no_lexical_seed` / `no_vectors`）。
  3. **本阶段只产两类记忆**：`VERIFIED_CLAIM`（必须绑定 Phase 03 判为 SUPPORTED 的证据）
     与 `ENTITY`（必须出现在证据实体表里）。策略/失败/剧集/来源/技能表现类是 Phase 12 的账，
     写入门对它们返回 `TYPE_DEFERRED_TO_PHASE_12`（**记账而不是静默丢**）。
  4. **不做 revalidation**：`valid_until` 过期与衰减只改**状态**（ACTIVE/STALE/EXPIRED），
     不重新检索、不重新核验（Phase 10 才有 freshness gate 与 revalidate 动作）。
     `SUPERSEDED`/`CONTRADICTED`/`REVOKED` 由 Phase 10/15 产出，本模块只尊重、不自动改。
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping, Sequence

from qa_evidence import (
    evidence_object,
    evidence_fingerprint,
    node_key,
    now_utc,
    source_fingerprint,
)
from qa_graph_contracts import (
    MEMORY_FRESHNESS_CLASSES,
    MEMORY_GRAPH_VERSION,
    MEMORY_LIFECYCLE_REPORT_SCHEMA,
    MEMORY_LIFECYCLE_TRANSITIONS,
    MEMORY_LIFECYCLE_VERSION,
    MEMORY_PRODUCIBLE_TYPES,
    MEMORY_PROVENANCE_VERSION,
    MEMORY_RECALL_CHANNELS,
    MEMORY_RECALL_FACTORS,
    MEMORY_RECALL_HINT_VERSION,
    MEMORY_RECALL_MODES,
    MEMORY_RECALL_VERSION,
    MEMORY_RELATIONS,
    MEMORY_SCOPES,
    MEMORY_STATUSES,
    MEMORY_TYPE_OWNER_PHASE,
    MEMORY_TYPES,
    MEMORY_WRITE_DECISIONS,
    MEMORY_WRITE_FACTORS,
    MEMORY_WRITE_GATE_VERSION,
    MEMORY_WRITE_REASONS,
    EVIDENCE_STATUS_QUALIFIED,
    EVIDENCE_STATUS_REFUTED,
    EVIDENCE_STATUS_SUPPORTED,
    validate as validate_contract,
)
from qa_verifier import evidence_score, term_set, verification_of

# ── 可配置旋钮（全部可回滚；默认值写在常量里便于复算）───────────────────────

DEFAULT_WRITE_MIN_UTILITY = 0.05
"""PERSIST 的效用下限（§11 的 MemoryWriteUtility 低于它一律 DROP，默认 0.05）。

标定口径（都是可复算的确定值，见验收报告的标定表）：
  · LONG 且已核验 ≈ 0.49 → PERSIST；
  · VERSION_SENSITIVE / MEDIUM / SHORT / VERY_SHORT 且已核验 ≈ 0.32 / 0.21 / 0.11 / 0.08
    → `PERSIST_WITH_TTL`（门按档补 TTL，这正是短时效记忆的正确处置）；
  · 低置信或没有实质内容的候选 ≈ 0 或负 → DROP。
不设 0：效用为 0 的记忆既不省检索也提不了质量，只会占库与污染召回排序。"""

DEFAULT_RECALL_LIMIT = 8
DEFAULT_RECALL_MIN_SCORE = 0.05
"""召回条数上限与分数下限。

门槛标定（真机 15 个 run 复算，见 `baseline/qa-memory-acceptance.json` 的 `recall.scores`）：
§11 的召回分是**五个 [0,1] 因子相乘**的量级，真机 top 分落在 0.06–0.11（中位 0.068）。
0.10 会把 12/15 的 run 拦掉（它们其实有同包同主题的记忆），0.05 保留 12/15 且仍能挡掉
"零词面交集"的噪声。命中恒为 MEMORY_HINT（还要过 ContextUtility 与 Phase 10 的重验），
所以这一层偏召回是安全的；`QA_MEMORY_RECALL_MIN_SCORE` 可随时调回 0.10。"""

STALE_FLOOR = 0.25
EXPIRE_FLOOR = 0.02
"""衰减分两个门槛（P09-05）：< STALE_FLOOR 判 STALE；< EXPIRE_FLOOR 判 EXPIRED。"""

GRAPH_EXPANSION_WEIGHT = 0.9
"""图通道一跳扩展的分数折扣（父命中分 × 边权 × 0.9）：跳得越远越不该压过直接命中。

方向口径：只沿 `memory_relation.memory_id → target_memory_id` **正向**一跳（关系是有向的，
反向可见性要由写入方建对应边）——一条规则、无歧义、可复算。"""

HALF_LIFE_DAYS = {
    "LONG": 3650.0,              # 数学定义 / 历史事实（§10）
    "VERSION_SENSITIVE": 540.0,  # 规范/指引/指南（版本一变就可能失效）
    "MEDIUM": 365.0,             # 产品/参数说明
    "SHORT": 90.0,               # 软件 / API / 政策年度
    "VERY_SHORT": 14.0,          # 新闻 / 价格 / 库存 / 排名
    "SESSION": 3.0,              # 当前状态（会话内）
    "ENCOUNTER_BOUND": 1.0,      # 本次研究轮次内
}
"""衰减半衰期（天），按 §10 的时效档。**纯规则**、同输入同输出（P09-05）。"""

TTL_DAYS = {
    "LONG": 0,                   # 不设 TTL（0 = 不落 valid_until）
    "VERSION_SENSITIVE": 540,
    "MEDIUM": 365,
    "SHORT": 90,
    "VERY_SHORT": 14,
    "SESSION": 3,
    "ENCOUNTER_BOUND": 1,
}
"""`PERSIST_WITH_TTL` 的 TTL（天）；0 = 该档不设过期时间（走 PERSIST）。"""

REUSE_BASE = {
    "LONG": 0.90, "VERSION_SENSITIVE": 0.80, "MEDIUM": 0.65,
    "SHORT": 0.55, "VERY_SHORT": 0.60, "SESSION": 0.10, "ENCOUNTER_BOUND": 0.05,
}
"""ReuseProbability 的时效档基线：越"长效"的知识越值得记住（§11 的复用概率）。

VERY_SHORT（新闻/价格）反而略高于 SHORT：它**当轮就可能被反复问**（同一批问题的实时数据），
所以复用概率高、但靠 `PERSIST_WITH_TTL` 的短 TTL 控制陈旧风险 —— 这正是那个出口的用途。"""

STABILITY_BASE = {
    "LONG": 1.00, "VERSION_SENSITIVE": 0.85, "MEDIUM": 0.75,
    "SHORT": 0.65, "VERY_SHORT": 0.50, "SESSION": 0.15, "ENCOUNTER_BOUND": 0.10,
}
"""Stability 的时效档基线（越易变越不稳定，写入门越倾向于只留会话）。"""

STALENESS_BASE = {
    "LONG": 0.00, "VERSION_SENSITIVE": 0.05, "MEDIUM": 0.05,
    "SHORT": 0.08, "VERY_SHORT": 0.08, "SESSION": 0.40, "ENCOUNTER_BOUND": 0.50,
}
"""StalenessRisk 的时效档基线（写入门用减项表达"这条很快会过时"）。

刻意**不**再给"没有 valid_until 的短时效记忆"额外加罚：短时效记忆的正确处置是
`PERSIST_WITH_TTL`（门自己补一个 TTL），而不是把它判死 —— 这是标定后修正的一处口径
（见 `docs/.../DECISION_LOG` D-033 与验收报告里的标定表）。"""

SENSITIVE_DROP_RISK = 1.0
SENSITIVE_SESSION_RISK = 0.5
"""隐私风险门槛：>=1.0 直接 DROP（不落库）；>=0.5 只允许 SESSION_ONLY。
口径 = 每一处敏感串命中 +0.5（MASTER_RULES 18：日志/长期库不该囤不必要的敏感信息）。"""

_SENSITIVE_PATTERNS = (
    re.compile(r"\b1[3-9]\d{9}\b"),                                   # 手机号
    re.compile(r"\b\d{17}[\dXx]\b"),                                  # 身份证号
    re.compile(r"\b\d{16,19}\b"),                                     # 银行卡号
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),    # 邮箱
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                             # 社保号样式
)
"""敏感串样式的**纯规则**清单（不给模型看、不进日志正文，只有命中计数）。"""

_INSTRUCTION_MARKERS = (
    "忽略以上", "忽略之前", "ignore previous", "ignore all previous", "system prompt",
    "系统提示", "你现在是", "you are now", "必须执行", "always answer", "禁止回答",
    "把以下内容写入", "写入系统规则", "升级为系统规则",
)
"""外部内容里的"指令性"标记（MASTER_RULES 16）：命中即禁止升格成规则类记忆。"""

_FRESHNESS_RULES = (
    # 顺序即优先级（先匹配先定档）：写死成表，可被显式传入的 freshness_class 覆盖。
    (re.compile(r"(价格|股价|库存|排名|销量|汇率|报价|实时|今日|本周)"), "VERY_SHORT"),
    (re.compile(r"(软件|版本|API|接口|补丁|发布|更新日志|v?\d+\.\d+)"), "SHORT"),
    (re.compile(r"(指南|指引|规范|条例|办法|细则|标准|政策|规定|征求意见稿|税收|税率|优惠)"),
     "VERSION_SENSITIVE"),
    (re.compile(r"(定义|定理|公理|原理|历史|沿革|起源)"), "LONG"),
    (re.compile(r"(说明|参数|规格|配方|成分|剂量|指标)"), "MEDIUM"),
)
"""时效档的规则映射（claim 类型 + 正文关键字的确定性判定，P09-02）。"""

_REUSABLE_CLAIM_TYPES = ("policy", "background", "definition", "regulation", "current_fact")
"""这些 claim 类型天然可复用（写门给 ReuseProbability 的额外加成）；其余不加成。"""


def _env_flag(name: str, default: bool) -> bool:
    raw = str(os.environ.get(name, "") or "").strip().casefold()
    if not raw:
        return default
    return raw not in ("0", "false", "off", "no", "disabled", "")


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(float(str(os.environ.get(name, "") or "").strip()))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _env_float(name: str, default: float, low: float = 0.0, high: float = 1.0) -> float:
    try:
        value = float(str(os.environ.get(name, "") or "").strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def memory_graph_enabled() -> bool:
    """管线开关（默认**关**）：关掉时 Phase 08 之后的行为逐字不变（回滚口径）。"""
    return _env_flag("QA_MEMORY_GRAPH", False)


def write_min_utility() -> float:
    return _env_float("QA_MEMORY_WRITE_MIN_UTILITY", DEFAULT_WRITE_MIN_UTILITY)


def recall_limit() -> int:
    return _env_int("QA_MEMORY_RECALL_LIMIT", DEFAULT_RECALL_LIMIT, 1, 50)


def recall_min_score() -> float:
    return _env_float("QA_MEMORY_RECALL_MIN_SCORE", DEFAULT_RECALL_MIN_SCORE)


def recall_counts_reuse() -> bool:
    """召回是否直接计 `reuse_count`（默认**关**：召回只是"给过提示"，用过才算复用）。"""
    return _env_flag("QA_MEMORY_RECALL_COUNTS_REUSE", False)


def _clamp(value, low: float = 0.0, high: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low
    if math.isnan(number) or math.isinf(number):
        return low
    return max(low, min(high, number))


def _digest(value, length: int = 24) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:length]


def _norm_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _now_dt(value=None) -> datetime:
    """统一时钟（可注入）：字符串/None/datetime 都能吃，统一成 UTC aware。"""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if text:
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"):
            try:
                moment = datetime.strptime(text[:10], fmt)
                break
            except ValueError:
                moment = None
        if moment is None:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# ── P09-01：schema/version/relations（标识、作用域键、版本行口径）────────────

def content_fingerprint(text) -> str:
    """记忆内容的稳定指纹（与 Phase 02 证据指纹同一手法：归一化后 sha256 截断）。"""
    return "MCF" + _digest(_norm_text(text).casefold())


def scope_key(scope: str, *, owner_user_id: str = "", session_id: str = "",
              industry_pack_id: str = "") -> str:
    """把 §14 的作用域 + 作用域主体编码成**一个可精确匹配的键**（P09-01）。

    §14 最后一段是硬规则："当前状态类记忆（`SESSION` / `ENCOUNTER_BOUND`）不得无条件
    跨会话当成当前事实"。所以把会话/轮次**编进键里**：作用域键不同 = 互不可见，
    这条规则就变成数据库层面的精确匹配，而不是靠调用方自觉。
    """
    scope = str(scope or "SESSION")
    owner = str(owner_user_id or "")
    session = str(session_id or "")
    pack = str(industry_pack_id or "")
    if scope == "GLOBAL_KNOWLEDGE":
        return "global"
    if scope == "ORGANIZATION":
        return "org:%s" % owner
    if scope == "PATIENT_LONGITUDINAL":
        # D-002 中性映射：跨会话长期主体记忆 = 绑行业包 + 主体实体（不含会话）
        return "subject:%s" % pack
    if scope == "ENCOUNTER":
        return "encounter:%s:%s" % (pack, session)
    return "session:%s:%s:%s" % (owner, session, pack)


def visible_scope_keys(*, owner_user_id: str = "", session_id: str = "",
                       industry_pack_id: str = "") -> list:
    """一次召回能看见的作用域键（顺序固定，便于复算）：
    全局知识 + 组织规则 + 主体长期 + 本次轮次 + 本会话。"""
    return [
        scope_key("GLOBAL_KNOWLEDGE", owner_user_id=owner_user_id, session_id=session_id,
                  industry_pack_id=industry_pack_id),
        scope_key("ORGANIZATION", owner_user_id=owner_user_id, session_id=session_id,
                  industry_pack_id=industry_pack_id),
        scope_key("PATIENT_LONGITUDINAL", owner_user_id=owner_user_id, session_id=session_id,
                  industry_pack_id=industry_pack_id),
        scope_key("ENCOUNTER", owner_user_id=owner_user_id, session_id=session_id,
                  industry_pack_id=industry_pack_id),
        scope_key("SESSION", owner_user_id=owner_user_id, session_id=session_id,
                  industry_pack_id=industry_pack_id),
    ]


def memory_id_for(*, scope: str, memory_type: str, canonical_content: str,
                  owner_user_id: str = "", session_id: str = "",
                  industry_pack_id: str = "") -> str:
    """内容寻址的记忆 id：同作用域 + 同类型 + 同内容 → 恒等 id（幂等写的前提）。"""
    key = scope_key(scope, owner_user_id=owner_user_id, session_id=session_id,
                    industry_pack_id=industry_pack_id)
    return "MEM" + _digest("%s|%s|%s" % (key, memory_type, content_fingerprint(canonical_content)))


def memory_version_row(item: Mapping) -> dict:
    """把一条记忆投影成"可读的版本视图"（验收/UI 用；不参与业务判定）。"""
    item = item if isinstance(item, Mapping) else {}
    return {
        "memory_id": str(item.get("memory_id") or ""),
        "memory_type": str(item.get("memory_type") or ""),
        "version": int(item.get("version") or 1),
        "status": str(item.get("status") or ""),
        "confidence": float(item.get("confidence") or 0),
        "decay_score": float(item.get("decay_score") or 0),
        "valid_until": str(item.get("valid_until") or ""),
    }


# ── P09-02：memory types（分档、正文归一、类型归属与形状校验）───────────────

def classify_freshness(*, claim_type: str = "", text: str = "") -> str:
    """时效档判定（P09-02，**纯规则**）：claim 类型优先，其次正文关键字，最后 MEDIUM。

    同一个输入永远得到同一档；调用方显式给了 `freshness_class` 时以显式值为准
    （判档只是"给没标注的记忆补一个可复算的默认档"）。
    """
    body = _norm_text(text)
    kind = str(claim_type or "").strip().casefold()
    for pattern, value in _FRESHNESS_RULES:
        if pattern.search(body):
            return value
    if kind in ("policy", "regulation"):
        return "VERSION_SENSITIVE"
    if kind in ("background", "definition"):
        return "LONG"
    if kind in ("current_fact", "metric", "price"):
        return "SHORT"
    return "MEDIUM"


def type_policy(memory_type: str) -> dict:
    """类型策略：归属 Phase、本阶段能否产出、为什么（可复算的拒绝理由）。"""
    kind = str(memory_type or "").strip().upper()
    owner = MEMORY_TYPE_OWNER_PHASE.get(kind, "")
    producible = kind in MEMORY_PRODUCIBLE_TYPES
    reason = ""
    if not owner:
        reason = "TYPE_NOT_SUPPORTED"
    elif not producible:
        reason = "TYPE_DEFERRED_TO_PHASE_%s" % owner[1:] if owner.startswith("P") else "TYPE_NOT_SUPPORTED"
    if reason and reason not in MEMORY_WRITE_REASONS:      # 兜底：理由码必须是契约里的取值
        reason = "TYPE_NOT_SUPPORTED"
    return {"memory_type": kind, "owner_phase": owner, "producible": producible, "reason": reason}


def sensitive_hits(text) -> int:
    """敏感串命中数（只看计数，不搬运正文；MASTER_RULES 18）。"""
    body = str(text or "")
    return sum(len(pattern.findall(body)) for pattern in _SENSITIVE_PATTERNS)


def external_instruction(text) -> bool:
    """外部内容的"指令性"判定（MASTER_RULES 16）：命中即禁止升格成系统/领域规则。"""
    body = _norm_text(text).casefold()
    return any(marker.casefold() in body for marker in _INSTRUCTION_MARKERS)


def canonicalize(text) -> str:
    """记忆正文的规范形态：去多余空白 + 去首尾标点噪声（**不改写内容**）。

    刻意不做摘要、不做改写：记忆正文只能是已核验内容的原文（§2.1/MASTER_RULES 11）。
    """
    body = _norm_text(text)
    return body.strip("　 \t\r\n-—·•*#>「」\"'`")


def information_value(text) -> float:
    """InformationValue（§11）：实词密度 + 长度因子，值域 [0,1]，纯统计。"""
    body = canonicalize(text)
    if not body:
        return 0.0
    terms = term_set(body, sizes=(2, 3))
    density = _clamp(len(terms) / 12.0)
    length = _clamp(len(body) / 60.0)
    return round(_clamp(0.35 + 0.45 * density + 0.20 * length), 8)


def validate_memory_item(item: Mapping) -> tuple:
    """记忆形状校验（契约 + 类型归属）：返回 (是否通过, 说明)。"""
    item = item if isinstance(item, Mapping) else {}
    ok, note = validate_contract("memory_item", dict(item))
    if not ok:
        return ok, note
    policy = type_policy(item.get("memory_type"))
    if not policy["owner_phase"]:
        return False, "未登记归属 Phase 的类型：%s" % item.get("memory_type")
    if str(item.get("scope")) not in MEMORY_SCOPES:
        return False, "作用域不在 §14 五值内：%s" % item.get("scope")
    return True, "ok"


def claim_evidence_rows(graph: Mapping) -> dict:
    """把证据图上的 claim↔evidence 关系归一成 `{claim_id: [row, ...]}`（只读既有结构）。

    `row` = {evidence_ref, graph_relation, verdict, evidence_item}：
      · `graph_relation` 优先取 Phase 06 的图级大写关系，退回 `relationship`（Phase 06 之前）；
      · `verdict` 取 Phase 03 的核验结论（`verification_of`），**没有核验就当作未核验**。
    """
    graph = graph if isinstance(graph, Mapping) else {}
    evidence_by_ref = {}
    for item in graph.get("evidence") or []:
        if not isinstance(item, Mapping):
            continue
        ref = str(item.get("evidence_ref") or "")
        if ref:
            evidence_by_ref[ref] = dict(item)
    rows: dict = {}
    for edge in graph.get("edges") or []:
        if not isinstance(edge, Mapping):
            continue
        claim_id = str(edge.get("claim_id") or "")
        ref = str(edge.get("evidence_ref") or "")
        if not claim_id or not ref:
            continue
        relation = str(edge.get("graph_relation") or "").upper()
        if not relation:
            relation = {
                "supports": "SUPPORTS", "refutes": "REFUTES", "context": "MENTIONS",
                "contradicts": "CONTRADICTS",
            }.get(str(edge.get("relationship") or "").casefold(), "MENTIONS")
        item = evidence_by_ref.get(ref) or {}
        verification = verification_of(item) if item else {}
        rows.setdefault(claim_id, []).append({
            "evidence_ref": ref, "graph_relation": relation,
            "relationship": str(edge.get("relationship") or ""),
            "verdict": str(verification.get("verdict") or verification.get("status") or ""),
            "verified": bool(verification.get("verified")),
            "evidence_score": float(verification.get("score") or 0),
            "evidence_item": item,
        })
    return rows


def evidence_link_row(item: Mapping, row: Mapping, *, run_id: str = "", stage: str = "",
                      route: str = "", corpus_version: str = "") -> dict:
    """把一条证据（+核验结论）转成 `memory_evidence_link` 的一行（P09-06 provenance）。"""
    item = item if isinstance(item, Mapping) else {}
    layer = evidence_object(item)
    provenance = layer.get("provenance") if isinstance(layer.get("provenance"), Mapping) else {}
    source = layer.get("source") if isinstance(layer.get("source"), Mapping) else {}
    return {
        "evidence_ref": str(item.get("evidence_ref") or ""),
        "source_fingerprint": str(layer.get("source_fingerprint") or source_fingerprint(item)),
        "span_fingerprint": str(layer.get("fingerprint") or evidence_fingerprint(item)),
        "run_id": str(run_id or provenance.get("run_id") or ""),
        "stage": str(stage or provenance.get("stage") or ""),
        "route": str(route or provenance.get("route") or item.get("retrieval_method") or ""),
        "corpus_version": str(corpus_version or provenance.get("corpus_version") or ""),
        "verdict": str(row.get("verdict") or ""),
        "evidence_score": float(row.get("evidence_score") or 0),
        "relationship": str(row.get("relationship") or ""),
        "metadata": {
            "article_id": item.get("article_id"),
            "source_url": str(item.get("source_url") or source.get("source_url") or ""),
            "title": str(item.get("title") or ""),
            "published_at": str(item.get("published_at") or source.get("published_at") or ""),
            "authority_level": item.get("authority_level"),
            "span_quote": str((layer.get("span") or {}).get("quote") or "")
            if isinstance(layer.get("span"), Mapping) else "",
        },
    }


def memory_candidates_from_graph(graph: Mapping, *, scope: str = "PATIENT_LONGITUDINAL",
                                 owner_user_id: str = "", session_id: str = "",
                                 industry_pack_id: str = "", run_id: str = "",
                                 stage: str = "", route: str = "", corpus_version: str = "",
                                 max_entities: int = 12, now=None) -> list:
    """从**已核验的证据图**构造记忆候选（P09-02/P09-03 的输入）。

    只做两件事，且都要求可回溯证据：
      · `VERIFIED_CLAIM`：每个非计划 claim 一条，证据 = 图上判为 **SUPPORTED** 的支持边
        （MASTER_RULES 11：没有通过核验的支撑证据 → 不产候选，交给写门拒收并记账）；
      · `ENTITY`：出现在已核验证据实体表里的实体各一条（实体记忆只是"索引"，不是事实断言）。
    计划 claim（`plan_only`）与只有 MENTIONS/REFUTES 边的 claim 一律不进候选。
    """
    graph = graph if isinstance(graph, Mapping) else {}
    rows_by_claim = claim_evidence_rows(graph)
    candidates: list = []
    entity_index: dict = {}
    for node in graph.get("claims") or []:
        if not isinstance(node, Mapping):
            continue
        claim = node.get("claim") if isinstance(node.get("claim"), Mapping) else {}
        claim_id = str(node.get("canonical_id") or claim.get("claim_id") or "")
        if not claim_id or bool(node.get("plan_only")):
            continue
        text = canonicalize(claim.get("text") or node.get("text") or "")
        if not text:
            continue
        supports = [row for row in (rows_by_claim.get(claim_id) or [])
                    if row["graph_relation"] == "SUPPORTS"]
        verified = [row for row in supports
                    if row["verdict"] == EVIDENCE_STATUS_SUPPORTED and row["evidence_item"]]
        links = [evidence_link_row(row["evidence_item"], row, run_id=run_id, stage=stage,
                                   route=route, corpus_version=corpus_version)
                 for row in verified]
        entities = []
        for row in verified:
            source = row["evidence_item"]
            layer = evidence_object(source)
            for entity in (layer.get("entities") or []):
                if not isinstance(entity, Mapping):
                    continue
                # Phase 02 的实体对象键名是 entity_key/label（`evidence_entities()` 的产物），
                # 这里同时兼容 text/key，但**不自己造 NER**。
                label = str(entity.get("label") or entity.get("text") or "")
                key = str(entity.get("entity_key") or entity.get("key") or node_key(label))
                if not key:
                    continue
                entities.append({"entity_key": key, "text": label or key,
                                 "role": "subject"})
                bucket = entity_index.setdefault(key, {"text": label or key, "links": []})
                bucket["links"].append(next((item for item in links
                                             if item["evidence_ref"] == row["evidence_ref"]), {}))
        freshness = classify_freshness(claim_type=str(claim.get("claim_type") or ""), text=text)
        candidates.append({
            "memory_type": "VERIFIED_CLAIM",
            "canonical_content": text,
            "claim_id": claim_id,
            "claim_type": str(claim.get("claim_type") or ""),
            "claim_confidence": float(claim.get("confidence") or 0),
            "freshness_class": freshness,
            "valid_from": str(claim.get("valid_from") or ""),
            "valid_until": str(claim.get("valid_to") or ""),
            "entity_ids": sorted({item["entity_key"] for item in entities}),
            "evidence": [row for row in links if row.get("evidence_ref")],
            "supporting": len([row for row in links if row.get("evidence_ref")]),
            "scope": scope, "owner_user_id": owner_user_id, "session_id": session_id,
            "industry_pack_id": industry_pack_id,
            "metadata": {"claim_id": claim_id, "claim_type": str(claim.get("claim_type") or ""),
                         "verified_evidence": len(links)},
        })
    for key, bucket in sorted(entity_index.items()):
        links = [row for row in bucket["links"] if row and row.get("evidence_ref")]
        if not links:
            continue
        candidates.append({
            "memory_type": "ENTITY",
            "canonical_content": canonicalize(bucket["text"]),
            "claim_id": "",
            "claim_type": "entity",
            "claim_confidence": 0.0,
            "freshness_class": "MEDIUM",
            "valid_from": "", "valid_until": "",
            "entity_ids": [key],
            "evidence": links[:max_entities],
            "supporting": len(links),
            "scope": scope, "owner_user_id": owner_user_id, "session_id": session_id,
            "industry_pack_id": industry_pack_id,
            "metadata": {"entity_key": key, "mentions": len(links)},
        })
    return candidates


# ── P09-03：Write Gate（§2.2 四出口 + §11 MemoryWriteUtility）────────────────

def existing_memory_index(store, *, scope_keys=(), include_all_scopes: bool = True) -> dict:
    """既有记忆的内容索引：`content_fingerprint` → 行（重复惩罚与合并用）。"""
    if store is None:
        return {}
    try:
        items = store.load_memory_items(scope_keys=scope_keys,
                                        include_all_scopes=include_all_scopes, limit=5000)
    except Exception:            # noqa: BLE001 —— 读不到既有记忆时按"全新建"处理
        return {}
    index: dict = {}
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        key = str(item.get("content_fingerprint") or "")
        if key:
            index.setdefault(key, []).append(dict(item))
    return index


def write_utility(candidate: Mapping, *, existing: Sequence[Mapping] = (),
                  batch_seen: Mapping | None = None, claim_type: str = "") -> dict:
    """§11 `MemoryWriteUtility` 的**逐项可复算**实现（P09-03）。

    `ReuseProbability × Confidence × Stability × InformationValue − PrivacyRisk −
     StalenessRisk − DuplicationPenalty`

    每一项怎么来的都写在 factors 里（同输入同输出，零随机、零模型）：
      · reuse_probability：时效档基线 ×(0.6 + 0.4×min(1, 支撑证据数/2))，可复用 claim 类型 +0.05；
      · confidence：0.5×最高核验分 + 0.5×claim 置信度（实体类用"出现次数"统计口径）；
      · stability：时效档基线 ×(有有效期信息 1.0 否则 0.9)；
      · information_value：实词密度 + 长度因子（`information_value()`）；
      · privacy_risk：敏感串命中数 ×0.5（封顶 1.0）；
      · staleness_risk：时效档基线（SHORT/VERY_SHORT 且无 valid_until 再 +0.1）；
      · duplication_penalty：同内容同来源 1.0 / 同内容不同来源 0.5 / 同批重复 0.15 / 否则 0。
    """
    candidate = candidate if isinstance(candidate, Mapping) else {}
    body = canonicalize(candidate.get("canonical_content"))
    freshness = str(candidate.get("freshness_class") or "MEDIUM")
    freshness = freshness if freshness in MEMORY_FRESHNESS_CLASSES else "MEDIUM"
    supporting = len([row for row in (candidate.get("evidence") or [])
                      if isinstance(row, Mapping) and row.get("evidence_ref")])
    positive = [float(row.get("evidence_score") or 0) for row in (candidate.get("evidence") or [])
                if isinstance(row, Mapping)]
    claim_confidence = float(candidate.get("claim_confidence") or 0)
    kind = str(claim_type or candidate.get("claim_type") or "")
    if candidate.get("memory_type") == "ENTITY":
        # 实体记忆没有"核验分"：用出现次数当置信（统计口径，逐项可复算）
        confidence = _clamp(0.30 + 0.20 * min(supporting, 3))
    else:
        confidence = _clamp(0.5 * (max(positive) if positive else 0.0) + 0.5 * claim_confidence)
    reuse = _clamp(REUSE_BASE.get(freshness, 0.5) * (0.6 + 0.4 * min(1.0, supporting / 2.0))
                   + (0.05 if kind in _REUSABLE_CLAIM_TYPES else 0.0))
    stability = _clamp(STABILITY_BASE.get(freshness, 0.5)
                       * (1.0 if (candidate.get("valid_from") or candidate.get("valid_until")) else 0.9))
    info = information_value(body)
    privacy = _clamp(0.5 * sensitive_hits(body))
    staleness = _clamp(STALENESS_BASE.get(freshness, 0.1))
    duplication = 0.0
    same_source = False
    if existing:
        fingerprints = {str(row.get("content_fingerprint") or "") for row in existing}
        if str(candidate.get("content_fingerprint") or content_fingerprint(body)) in fingerprints:
            duplication = 0.5
            incoming_sources = {str(row.get("source_fingerprint") or "")
                                for row in (candidate.get("evidence") or [])
                                if isinstance(row, Mapping)}
            for row in existing:
                links = row.get("metadata") or {}
                known = set()
                if isinstance(links, Mapping):
                    known = {str(item) for item in (links.get("source_fingerprints") or [])}
                if known and incoming_sources and known & incoming_sources:
                    same_source = True
                    duplication = 1.0
                    break
            if duplication == 0.5 and not same_source and existing:
                duplication = 0.5
    if batch_seen:
        key = str(candidate.get("content_fingerprint") or content_fingerprint(body))
        if batch_seen.get(key):
            duplication = max(duplication, 0.15)
    total = _clamp(reuse * confidence * stability * info - privacy - staleness - duplication,
                   -1.0, 1.0)
    return {
        "reuse_probability": round(reuse, 8), "confidence": round(confidence, 8),
        "stability": round(stability, 8), "information_value": round(info, 8),
        "privacy_risk": round(privacy, 8), "staleness_risk": round(staleness, 8),
        "duplication_penalty": round(duplication, 8),
        "utility": round(total, 8), "freshness_class": freshness,
        "supporting_evidence": supporting, "claim_type": kind,
    }


def decide_write(candidate: Mapping, *, factors: Mapping, has_existing: bool = False) -> tuple:
    """§2.2 四出口的**唯一决策点**（P09-03）：返回 (decision, reason)。

    判定顺序写死（先硬规则、再归属、再效用、最后 TTL），因此同样输入必然同样输出：
      1) 空正文 → DROP/EMPTY_CONTENT；
      2) 外部指令性内容 → DROP/EXTERNAL_INSTRUCTION_NOT_A_RULE（MASTER_RULES 16）；
      3) 类型不在本阶段可产清单 → DROP/TYPE_DEFERRED_TO_PHASE_xx（不提前实现后续 Phase）；
      4) 没有通过核验的支撑证据 → DROP/NO_VERIFIED_EVIDENCE（MASTER_RULES 11）；
      5) 隐私风险 ≥1.0 → DROP/SENSITIVE_CONTENT；≥0.5 → SESSION_ONLY/PRIVACY_RISK；
      6) 完全重复（同内容同来源）→ PERSIST/DUPLICATE_MERGED（幂等：只刷新，不新增版本语义）；
      7) 作用域是 SESSION/ENCOUNTER_BOUND → SESSION_ONLY/SESSION_SCOPE_ONLY；
      8) 效用低于下限 → DROP/UTILITY_BELOW_FLOOR；
      9) 时效档要求 TTL（TTL_DAYS>0 且没有显式 valid_until）→ PERSIST_WITH_TTL；
     10) 否则 → PERSIST。
    """
    candidate = candidate if isinstance(candidate, Mapping) else {}
    factors = factors if isinstance(factors, Mapping) else {}
    body = canonicalize(candidate.get("canonical_content"))
    policy = type_policy(candidate.get("memory_type"))
    if not body:
        return "DROP", "EMPTY_CONTENT"
    if external_instruction(body):
        return "DROP", "EXTERNAL_INSTRUCTION_NOT_A_RULE"
    if not policy["producible"]:
        return "DROP", policy["reason"]
    verified = [row for row in (candidate.get("evidence") or [])
                if isinstance(row, Mapping) and row.get("evidence_ref")
                and str(row.get("verdict") or "") == EVIDENCE_STATUS_SUPPORTED]
    if not verified and candidate.get("memory_type") == "VERIFIED_CLAIM":
        return "DROP", "NO_VERIFIED_EVIDENCE"
    if not verified and candidate.get("memory_type") == "ENTITY":
        return "DROP", "NO_VERIFIED_EVIDENCE"
    privacy = float(factors.get("privacy_risk") or 0)
    if privacy >= SENSITIVE_DROP_RISK:
        return "DROP", "SENSITIVE_CONTENT"
    if privacy >= SENSITIVE_SESSION_RISK:
        return "SESSION_ONLY", "PRIVACY_RISK"
    if float(factors.get("duplication_penalty") or 0) >= 1.0 and has_existing:
        return "PERSIST", "DUPLICATE_MERGED"
    if str(candidate.get("scope") or "") in ("SESSION", "ENCOUNTER_BOUND"):
        return "SESSION_ONLY", "SESSION_SCOPE_ONLY"
    if float(factors.get("utility") or 0) < write_min_utility():
        return "DROP", "UTILITY_BELOW_FLOOR"
    freshness = str(factors.get("freshness_class") or "MEDIUM")
    if TTL_DAYS.get(freshness, 0) and not str(candidate.get("valid_until") or ""):
        return "PERSIST_WITH_TTL", "TTL_REQUIRED_BY_FRESHNESS"
    return "PERSIST", "UTILITY_ABOVE_FLOOR"


def build_memory_item(candidate: Mapping, *, decision: str, factors: Mapping,
                      now=None) -> dict:
    """按决策成一条 MemoryItem（§2.3 字段一个不少；旧版本由 store 追加版本行）。"""
    candidate = candidate if isinstance(candidate, Mapping) else {}
    factors = factors if isinstance(factors, Mapping) else {}
    body = canonicalize(candidate.get("canonical_content"))
    scope = str(candidate.get("scope") or "SESSION")
    scope_value = scope if scope in MEMORY_SCOPES else "SESSION"
    freshness = str(factors.get("freshness_class") or "MEDIUM")
    moment = _now_dt(now)
    valid_until = str(candidate.get("valid_until") or "")
    ttl_days = TTL_DAYS.get(freshness, 0)
    if decision == "PERSIST_WITH_TTL" and not valid_until and ttl_days:
        valid_until = _stamp(moment + timedelta(days=ttl_days))
    evidence = [row for row in (candidate.get("evidence") or []) if isinstance(row, Mapping)]
    verified_at = max([str(row.get("verified_at") or "") for row in evidence] or [""])
    return {
        "memory_id": memory_id_for(
            scope=scope_value, memory_type=str(candidate.get("memory_type") or "VERIFIED_CLAIM"),
            canonical_content=body, owner_user_id=str(candidate.get("owner_user_id") or ""),
            session_id=str(candidate.get("session_id") or ""),
            industry_pack_id=str(candidate.get("industry_pack_id") or "")),
        "memory_type": str(candidate.get("memory_type") or "VERIFIED_CLAIM"),
        "canonical_content": body,
        "content_fingerprint": content_fingerprint(body),
        "confidence": round(float(factors.get("confidence") or 0), 8),
        "freshness_class": freshness,
        "valid_from": str(candidate.get("valid_from") or ""),
        "valid_until": valid_until,
        "last_verified_at": verified_at or _stamp(moment),
        "status": "ACTIVE",
        "scope": scope_value,
        "scope_key": scope_key(scope_value,
                               owner_user_id=str(candidate.get("owner_user_id") or ""),
                               session_id=str(candidate.get("session_id") or ""),
                               industry_pack_id=str(candidate.get("industry_pack_id") or "")),
        "owner_user_id": str(candidate.get("owner_user_id") or ""),
        "session_id": str(candidate.get("session_id") or ""),
        "industry_pack_id": str(candidate.get("industry_pack_id") or ""),
        "entity_ids": list(candidate.get("entity_ids") or []),
        "source_evidence_ids": [str(row.get("evidence_ref") or "") for row in evidence
                                if row.get("evidence_ref")],
        "superseded_by": "",
        "reuse_count": 0,
        "created_from_session_id": str(candidate.get("session_id") or ""),
        "created_from_run_id": str(candidate.get("run_id") or ""),
        "version": 1,
        "decay_score": round(float(factors.get("utility") or 0), 8),
        "change": "CREATE" if decision == "PERSIST" else "CREATE_WITH_TTL",
        "change_reason": "",
        "metadata": {
            "gate_version": MEMORY_WRITE_GATE_VERSION,
            "graph_version": MEMORY_GRAPH_VERSION,
            "utility": float(factors.get("utility") or 0),
            "factors": {key: factors.get(key) for key in MEMORY_WRITE_FACTORS},
            "claim_id": str(candidate.get("claim_id") or ""),
            "claim_type": str(candidate.get("claim_type") or ""),
            "supporting_evidence": int(factors.get("supporting_evidence") or 0),
            "source_fingerprints": sorted({str(row.get("source_fingerprint") or "")
                                           for row in evidence if row.get("source_fingerprint")}),
            "decision": decision,
        },
    }


def apply_write_gate(store, candidates: Sequence[Mapping], *, run_id: str = "",
                     trace_id: str = "", now=None, audit=None) -> dict:
    """P09-03：对候选记忆跑一次写门，落决策留痕并按决策写库。

    返回回执（`decisions` 明细 + 分布 + 落库条数）。**每一条候选**都会有一行
    `memory_write_decision`（含被 DROP 的），这是"为什么没记住"的唯一权威来源。
    写入侧顺序固定：memory_item → memory_evidence_link → memory_entity_link →
    memory_relation（ABOUT 实体 / DERIVED_FROM 证据 / APPLIES_TO 作用域）。
    """
    candidates = [item for item in (candidates or []) if isinstance(item, Mapping)]
    moment = _now_dt(now)
    index = existing_memory_index(store)
    batch: dict = {}
    decisions: list = []
    persisted: list = []
    for candidate in candidates:
        body = canonicalize(candidate.get("canonical_content"))
        fingerprint = content_fingerprint(body)
        existing = index.get(fingerprint) or []
        factors = write_utility(candidate, existing=existing, batch_seen=batch,
                                claim_type=str(candidate.get("claim_type") or ""))
        decision, reason = decide_write(candidate, factors=factors, has_existing=bool(existing))
        item = build_memory_item(candidate, decision=decision, factors=factors, now=moment) \
            if decision in ("PERSIST", "PERSIST_WITH_TTL") else {}
        decision_row = {
            "decision_id": "MWD" + _digest("%s|%s|%s|%s" % (
                run_id, candidate.get("memory_type"), fingerprint, reason))[:24],
            "run_id": str(run_id or ""),
            "memory_type": str(candidate.get("memory_type") or ""),
            "decision": decision, "reason": reason,
            "utility": float(factors.get("utility") or 0),
            "factors": {key: factors.get(key) for key in MEMORY_WRITE_FACTORS},
            "memory_id": str(item.get("memory_id") or ""),
            "content_fingerprint": fingerprint,
            "evidence_refs": [str(row.get("evidence_ref") or "")
                              for row in (candidate.get("evidence") or [])
                              if isinstance(row, Mapping)],
            "scope": str(item.get("scope") or candidate.get("scope") or ""),
            "scope_key": str(item.get("scope_key") or ""),
            "gate_version": MEMORY_WRITE_GATE_VERSION,
            "metadata": {"freshness_class": factors.get("freshness_class"),
                         "supporting_evidence": factors.get("supporting_evidence"),
                         "claim_type": factors.get("claim_type")},
        }
        decisions.append(decision_row)
        batch[fingerprint] = int(batch.get(fingerprint) or 0) + 1
        if decision not in ("PERSIST", "PERSIST_WITH_TTL") or store is None:
            continue
        try:
            written = store.save_memory_item(item)
        except Exception as exc:      # noqa: BLE001 —— 单条写失败不许拖垮整批
            decision_row["metadata"]["write_error"] = "%s: %s" % (type(exc).__name__,
                                                                  str(exc)[:120])
            continue
        if written.get("error"):
            decision_row["metadata"]["write_error"] = written["error"]
            continue
        decision_row["memory_id"] = str(written.get("memory_id") or item["memory_id"])
        links = []
        for row in (candidate.get("evidence") or []):
            if not isinstance(row, Mapping) or not row.get("evidence_ref"):
                continue
            links.append({**{key: row.get(key) for key in (
                "evidence_ref", "source_fingerprint", "span_fingerprint", "run_id", "stage",
                "route", "corpus_version", "verdict", "evidence_score", "relationship")},
                "metadata": row.get("metadata") or {}})
        store.link_memory_evidence(item["memory_id"], links)
        relations = []
        for entity_key in (candidate.get("entity_ids") or []):
            relations.append({"memory_id": item["memory_id"], "relation": "APPLIES_TO",
                              "target_kind": "entity", "target_ref": str(entity_key),
                              "weight": 0.5, "run_id": run_id})
        for link in links:
            relations.append({"memory_id": item["memory_id"], "relation": "DERIVED_FROM",
                              "target_kind": "evidence",
                              "target_ref": str(link.get("evidence_ref") or ""),
                              "weight": 1.0, "run_id": run_id})
        relations.append({"memory_id": item["memory_id"], "relation": "ABOUT",
                          "target_kind": "scope", "target_ref": str(item.get("scope_key") or ""),
                          "weight": 1.0, "run_id": run_id})
        store.add_memory_relation(relations)
        persisted.append(item["memory_id"])
        index.setdefault(fingerprint, []).append({
            **item, "metadata": {**(item.get("metadata") or {}),
                                 "source_fingerprints": (item.get("metadata") or {}).get(
                                     "source_fingerprints") or []}})
    if store is not None:
        try:
            store.record_memory_write_decision(decisions)
        except Exception:      # noqa: BLE001 —— 留痕失败不冒泡（回执里仍有 decisions）
            pass
    distribution: dict = {}
    for row in decisions:
        key = "%s/%s" % (row["decision"], row["reason"])
        distribution[key] = distribution.get(key, 0) + 1
    receipt = {
        "gate_version": MEMORY_WRITE_GATE_VERSION,
        "graph_version": MEMORY_GRAPH_VERSION,
        "run_id": str(run_id or ""),
        "candidates": len(candidates),
        "decisions": decisions,
        "decision_counts": _count(row["decision"] for row in decisions),
        "reason_counts": _count(row["reason"] for row in decisions),
        "distribution": dict(sorted(distribution.items(), key=lambda item: (-item[1], item[0]))),
        "persisted": len(persisted),
        "merged": len([row for row in decisions if row["reason"] == "DUPLICATE_MERGED"]),
        "session_only": len([row for row in decisions if row["decision"] == "SESSION_ONLY"]),
        "dropped": len([row for row in decisions if row["decision"] == "DROP"]),
        "min_utility": write_min_utility(),
        "memory_ids": persisted,
    }
    if audit is not None:
        try:
            audit.record("memory_write_gate", trace_id=str(trace_id or run_id or ""),
                         run_id=str(run_id or ""), payload={
                             "candidates": receipt["candidates"], "persisted": receipt["persisted"],
                             "dropped": receipt["dropped"], "session_only": receipt["session_only"],
                             "gate_version": MEMORY_WRITE_GATE_VERSION})
        except Exception:      # noqa: BLE001 —— 审计失败不影响写库结果
            pass
    return receipt


def _count(values) -> dict:
    counts: dict = {}
    for value in values or []:
        key = str(value or "")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def write_gate_receipt(receipt: Mapping) -> dict:
    """给 stats/SSE 的写门回执（不含正文，只有计数与口径）。"""
    receipt = receipt if isinstance(receipt, Mapping) else {}
    return {key: receipt.get(key) for key in (
        "gate_version", "graph_version", "candidates", "persisted", "merged", "session_only",
        "dropped", "decision_counts", "reason_counts", "distribution", "min_utility")}


# ── P09-04：Recall API（§11 MemoryRecallScore + §2.1 MEMORY_HINT 语义）───────

def _memory_terms(item: Mapping) -> set:
    """记忆的实词集合：正文 + 实体文本（同 `qa_verifier.term_set` 口径）。"""
    item = item if isinstance(item, Mapping) else {}
    parts = [str(item.get("canonical_content") or "")]
    for entity in (item.get("entity_ids") or []):
        parts.append(str(entity or ""))
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    for entity in (metadata.get("entity_texts") or []):
        parts.append(str(entity or ""))
    return term_set(" ".join(parts), sizes=(2, 3))


def semantic_relevance(terms: Iterable, item: Mapping, *, channel: str = "lexical",
                       cosine: float | None = None) -> float:
    """SemanticRelevance：词面覆盖（lexical）或向量余弦（vector）——**都是确定性统计**。

    词面口径与 Phase 03 的 `overlap_stats.coverage` 同源（"记忆能不能覆盖查询"）：
    查询实词里有多少**出现在记忆正文/实体文本里**。用"子串出现"而不是只比 n 元组集合，
    是因为查询词可能是 4~6 字的长词（如"家族办公室"），而 `term_set` 只切 2/3 元组——
    只看集合交集会把长词判成没命中（实测踩到）。向量口径直接用库内已有向量的余弦。
    """
    if channel == "vector" and cosine is not None:
        return _clamp(cosine)
    query = set(terms or ())
    if not query:
        return 0.0
    item = item if isinstance(item, Mapping) else {}
    parts = [str(item.get("canonical_content") or "")]
    parts.extend(str(entity or "") for entity in (item.get("entity_ids") or []))
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    parts.extend(str(entity or "") for entity in (metadata.get("entity_texts") or []))
    body_text = _norm_text(" ".join(parts))
    body_terms = term_set(body_text, sizes=(2, 3))
    hits = len([term for term in query if term in body_text or term in body_terms])
    return _clamp(hits / float(len(query)))


def task_applicability(mode: str, item: Mapping, *, terms: Iterable = (),
                       industry_pack_id: str = "") -> dict:
    """TaskApplicability（§11）：模式↔类型、作用域↔包、实体↔查询 的匹配度（可复算）。

    `planning` 模式只认策略/失败/查询模式类（§10：避免旧事实污染规划）；
    `evidence` 模式只认结论/实体/来源类。类型不匹配就是 0 —— 这一项是**乘法因子**，
    所以不匹配的记忆绝不会因为词面巧合被召回。
    """
    mode = str(mode or "evidence")
    kind = str((item or {}).get("memory_type") or "")
    planning_types = ("STRATEGY", "FAILURE", "QUERY_PATTERN")
    evidence_types = ("VERIFIED_CLAIM", "ENTITY", "SOURCE")
    if mode == "planning":
        type_match = 1.0 if kind in planning_types else 0.0
    else:
        type_match = 1.0 if kind in evidence_types else 0.6   # 其它类型降权但不判死
    pack = str((item or {}).get("industry_pack_id") or "")
    scope = str((item or {}).get("scope") or "")
    scope_match = 1.0
    if scope in ("PATIENT_LONGITUDINAL", "ENCOUNTER", "SESSION") and industry_pack_id:
        scope_match = 1.0 if pack == str(industry_pack_id) else 0.3
    elif scope in ("GLOBAL_KNOWLEDGE", "ORGANIZATION"):
        scope_match = 1.0
    if kind == "ENTITY":
        entity_match = 1.0 if set(str(key) for key in ((item or {}).get("entity_ids") or [])) \
            & set(node_key(term) for term in (terms or ())) else 0.25
    else:
        entity_match = 1.0
    return {"value": round(_clamp(type_match * scope_match * entity_match), 8),
            "type_match": type_match, "scope_match": scope_match, "entity_match": entity_match}


def freshness_factor_of(item: Mapping, *, now=None) -> dict:
    """Freshness（§11 召回公式里的那一项）：由 P09-05 的衰减分给出，同源同口径。"""
    decay = decay_score(item, now=now)
    return {"value": round(float(decay.get("score") or 0.0), 8),
            "age_days": decay.get("age_days"), "half_life_days": decay.get("half_life_days"),
            "status": decay.get("status")}


def historical_utility(item: Mapping, *, usage: Mapping | None = None) -> dict:
    """HistoricalUtility：召回/用过/帮上忙的**计数**统计。

    标定口径（可复算、有真跑依据）：
      · **新记忆中性 0.8**（不是 1.0，也不是 0.5）：它是"没历史"而不是"历史表现差"；
        早先用 0.5 会把每一条新记忆的召回分直接砍半，实测真机 15 个 run 里只有 3 个能过
        0.10 门槛，而实际相关性最高的那些分数落在 0.06–0.11（见验收报告 `recall.scores`）；
      · 被真的用过（`used`）/帮上忙（`helped`）→ 上调，封顶 1.0；
      · **被反复召回却从没被用过**（recalled ≥ 3 且 used == 0）→ 降到 0.5：这是"给了提示
        但没人采用"的可观测证据，不是惩罚。
    """
    item = item if isinstance(item, Mapping) else {}
    usage = usage if isinstance(usage, Mapping) else {}
    recalled = int(usage.get("recalled") or item.get("recall_count") or 0)
    used = int(usage.get("used") or item.get("reuse_count") or 0)
    helped = int(usage.get("helped") or 0)
    base = 0.5 if (recalled >= 3 and used == 0) else 0.8
    value = _clamp(base + 0.1 * min(used, 2) + 0.05 * min(helped, 2))
    return {"value": round(value, 8), "recalled": recalled, "used": used, "helped": helped,
            "base": base}


def contradiction_risk(item: Mapping, *, relations: Sequence[Mapping] = ()) -> dict:
    """ContradictionRisk（减项）：CONTRADICTS 关系 + 非 ACTIVE 状态。

    Phase 10 才会写 CONTRADICTS 边，所以本阶段真机上通常是 0 —— 但口径先接上：
    有 `CONTRADICTS` 边（无论指向谁）就按边权计风险，非 ACTIVE 状态额外 +0.3
    （**非 ACTIVE 本身已不进命中清单**，这一项只用于说明"它为什么被排除"）。
    """
    item = item if isinstance(item, Mapping) else {}
    risk = 0.0
    contradictions = [row for row in (relations or [])
                      if str(row.get("relation") or "").upper() == "CONTRADICTS"]
    for row in contradictions:
        risk += _clamp(row.get("weight") or 0.5)
    if str(item.get("status") or "ACTIVE") != "ACTIVE":
        risk += 0.3
    return {"value": round(_clamp(risk), 8), "contradictions": len(contradictions),
            "status": str(item.get("status") or "")}


def recall_score(factors: Mapping) -> float:
    """§11 `MemoryRecallScore`：五个乘子相乘后减去 ContradictionRisk（钳到 [-1,1]）。"""
    factors = factors if isinstance(factors, Mapping) else {}
    product = 1.0
    for key in ("semantic_relevance", "task_applicability", "confidence", "freshness",
                "historical_utility"):
        product *= float(factors.get(key) or 0.0)
    return round(_clamp(product - float(factors.get("contradiction_risk") or 0.0), -1.0, 1.0), 8)


def _link_article_ids(store, memory_ids: Sequence[str]) -> dict:
    """记忆 → 所链证据的 article_id 清单（向量通道用；库内已有向量，零端点调用）。"""
    mapping: dict = {}
    if store is None or not memory_ids:
        return mapping
    try:
        rows = store.memory_evidence(memory_ids=list(memory_ids))
    except Exception:        # noqa: BLE001
        return mapping
    for row in rows or []:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
        article_id = metadata.get("article_id")
        try:
            article_id = int(article_id)
        except (TypeError, ValueError):
            continue
        mapping.setdefault(str(row.get("memory_id") or ""), set()).add(article_id)
    return mapping


def _vectors_for(vectors) -> tuple:
    """接受 `(ids, matrix)` 或可调用加载器；返回 `(ids, matrix)`（拿不到就是空）。"""
    if vectors is None:
        return [], None
    if callable(vectors):
        try:
            vectors = vectors()
        except Exception:    # noqa: BLE001
            return [], None
    if isinstance(vectors, (tuple, list)) and len(vectors) == 2:
        ids, matrix = vectors
        return list(ids or []), matrix
    return [], None


def vector_channel(items: Sequence[Mapping], *, terms: Iterable, article_ids: Mapping,
                   vectors, threshold: float = 0.35, seed_ids: Iterable = ()) -> dict:
    """**向量通道**（P09-06）：用库内已有向量做余弦，查询向量 = 词面种子记忆的质心。

    口径（与 Phase 04 的语义通道同源，D-018 的能力边界一并继承）：
      · 种子 = `seed_ids` 指定的记忆（调用方给"词面命中"的那批），没给就用全部候选；
      · 查询向量 = 种子文章向量的**加权质心**，L2 归一化；
      · 打分对象是 `items` **全体**（不只是种子）—— 否则"语义相近但用词不同"的记忆
        永远进不来，向量通道就退化成了词面通道的复制品（实测踩到）；
      · 每条记忆的向量 = 它链的文章向量的质心；余弦 ≥ threshold 才算命中；
      · **绝不调嵌入端点**：没有向量/没有种子一律返回空 + 原因码。
    """
    ids, matrix = _vectors_for(vectors)
    if matrix is None or not len(ids):
        return {"scores": {}, "stats": {"channel": "vector", "reason": "no_vectors",
                                        "vectors": 0,
                                        "note": "库内没有 status='ready' 的文章向量："
                                                "向量通道降级（不调用嵌入端点）"}}
    try:
        import numpy as np
    except Exception:        # noqa: BLE001
        return {"scores": {}, "stats": {"channel": "vector", "reason": "numpy_missing"}}
    position = {int(article_id): index for index, article_id in enumerate(ids)}
    seeds = {str(value) for value in (seed_ids or []) if str(value or "")}
    seed_vectors, total = [], 0.0
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        memory_id = str(item.get("memory_id") or "")
        if seeds and memory_id not in seeds:
            continue
        for article_id in (article_ids.get(memory_id) or ()):
            if int(article_id) in position:
                seed_vectors.append(matrix[position[int(article_id)]])
                total += 1.0
    if not seed_vectors or total <= 0:
        return {"scores": {}, "stats": {"channel": "vector", "reason": "no_lexical_seed",
                                        "vectors": len(ids),
                                        "note": "词面种子记忆没有可用的文章向量："
                                                "无法构造离线查询向量"}}
    query = np.sum(np.vstack(seed_vectors), axis=0)
    norm = float(np.linalg.norm(query))
    if norm <= 0:
        return {"scores": {}, "stats": {"channel": "vector", "reason": "degenerate_query_vector"}}
    query = query / norm
    scores: dict = {}
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        memory_id = str(item.get("memory_id") or "")
        vectors_of = [matrix[position[int(article_id)]]
                      for article_id in (article_ids.get(memory_id) or ())
                      if int(article_id) in position]
        if not vectors_of:
            continue
        centroid = np.sum(np.vstack(vectors_of), axis=0)
        norm = float(np.linalg.norm(centroid))
        if norm <= 0:
            continue
        cosine = float(centroid / norm @ query)
        if cosine >= float(threshold):
            # 量化到 6 位小数：float32 的 BLAS 求和顺序会让末位差 ~1e-7，
            # 那点噪声不该变成"同一输入不同输出"（跨进程复算/报告比对都要稳）。
            scores[memory_id] = round(cosine, 6)
    return {"scores": scores, "stats": {"channel": "vector", "vectors": len(ids),
                                        "seeds": len(seed_vectors),
                                        "threshold": float(threshold), "hits": len(scores),
                                        "reason": "" if scores else "no_vector_hit",
                                        "note": "查询向量为离线质心（库内已有向量），"
                                                "无嵌入端点调用"}}


def memory_hint(item: Mapping, *, factors: Mapping, score: float, channels: Iterable,
                evidence_refs: Iterable = (), explain: str = "") -> dict:
    """把一条记忆包成 MEMORY_HINT（§2.1 / MASTER_RULES 11/12 的机器可校验形态）。

    `requires_revalidation` 恒 True、`verified_evidence` 恒 False：本阶段**没有**实现
    revalidation（Phase 10 的账），所以一条记忆永远只能说"这是提示，用前必须重验"。
    """
    item = item if isinstance(item, Mapping) else {}
    return {
        "memory_id": str(item.get("memory_id") or ""),
        "memory_type": str(item.get("memory_type") or ""),
        "canonical_content": str(item.get("canonical_content") or ""),
        "status": str(item.get("status") or ""),
        "scope": str(item.get("scope") or ""),
        "freshness_class": str(item.get("freshness_class") or ""),
        "score": round(float(score or 0), 8),
        "factors": {key: float((factors or {}).get(key) or 0) for key in MEMORY_RECALL_FACTORS},
        "channels": sorted({str(value) for value in (channels or []) if str(value or "")}),
        "evidence_refs": sorted({str(value) for value in (evidence_refs or []) if str(value or "")}),
        "hint": True,
        "hint_version": MEMORY_RECALL_HINT_VERSION,
        "requires_revalidation": True,
        "verified_evidence": False,
        "explain": str(explain or ""),
    }


def recall(store, *, mode: str = "evidence", query: str = "", terms: Iterable | None = None,
           owner_user_id: str = "", session_id: str = "", industry_pack_id: str = "",
           run_id: str = "", trace_id: str = "", limit: int | None = None,
           min_score: float | None = None, memory_types: Iterable = (),
           include_all_scopes: bool = False, vectors=None, now=None,
           usage: Mapping | None = None, store_log: bool = True,
           graph_expansion: bool = True) -> dict:
    """P09-04：Recall API（§11 的 MemoryRecallScore + §2.1 的 hint 语义）。

    四通道（P09-06）：`relational`（作用域+状态过滤）→ `lexical`（词面覆盖）→
    `vector`（库内已有向量余弦，离线质心）→ `graph`（记忆图关系一跳扩展）。

    硬规则：
      · **非 ACTIVE 一律不进命中**（§10 "status!=ACTIVE 不作证据"），只进计数；
      · 每条命中都是 `MEMORY_HINT`（`requires_revalidation=True`）；
      · 作用域不可见就看不到（`scope_key` 精确匹配，§14）；
      · 同输入同输出：命中排序按 (score desc, memory_id)，图扩展只一跳、权重固定。
    """
    mode = str(mode or "evidence")
    mode = mode if mode in MEMORY_RECALL_MODES else "evidence"
    moment = _now_dt(now)
    limit = recall_limit() if limit is None else max(1, min(int(limit), 50))
    min_score = recall_min_score() if min_score is None else float(min_score)
    keys = visible_scope_keys(owner_user_id=owner_user_id, session_id=session_id,
                              industry_pack_id=industry_pack_id)
    types = [str(item).upper() for item in (memory_types or []) if str(item or "")]
    planning_filter = False
    if mode == "planning" and not types:
        types = ["STRATEGY", "FAILURE", "QUERY_PATTERN"]
        # 规划召回**在 Python 侧过滤**（而不是只让 SQL 筛）：这样"为什么没召回"能落到
        # `counts.excluded_by_type` 上，运维一眼看得出是"类型不匹配"而不是"没有记忆"。
        planning_filter = True
    items = []
    if store is not None:
        try:
            items = store.load_memory_items(
                scope_keys=keys, memory_types=[] if planning_filter else types,
                owner_user_id="", include_all_scopes=bool(include_all_scopes), limit=1000)
        except Exception:        # noqa: BLE001 —— 召回失败退化成"没有可召回的记忆"
            items = []
    query_terms = set(terms or ()) or term_set(query, sizes=(2, 3))
    counts = {"considered": len(items), "excluded_by_status": 0, "excluded_by_type": 0,
              "lexical": 0, "vector": 0, "graph": 0, "below_threshold": 0}
    active = []
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("status") or "") != "ACTIVE":
            counts["excluded_by_status"] += 1
            continue
        if types and str(item.get("memory_type") or "") not in types:
            counts["excluded_by_type"] += 1
            continue
        active.append(dict(item))
    article_ids = _link_article_ids(store, [str(item.get("memory_id") or "") for item in active])
    relations = {}
    if store is not None and active:
        try:
            for row in store.memory_relations([str(item.get("memory_id") or "")
                                               for item in active]) or []:
                relations.setdefault(str(row.get("memory_id") or ""), []).append(row)
        except Exception:        # noqa: BLE001
            relations = {}
    lexical: dict = {}
    for item in active:
        score = semantic_relevance(query_terms, item, channel="lexical")
        if score > 0:
            lexical[str(item.get("memory_id") or "")] = score
    counts["lexical"] = len(lexical)
    seed_items = [item for item in active
                  if str(item.get("memory_id") or "") in lexical]
    # 种子只从词面命中里取（构造离线查询向量），但**打分对象是全体可见记忆**：
    # 否则"语义相近但用词不同"的记忆永远进不来（向量通道就成了词面通道的复制品）。
    vector_result = vector_channel(active, terms=query_terms, article_ids=article_ids,
                                   vectors=vectors, seed_ids=lexical) if seed_items else {
        "scores": {}, "stats": {"channel": "vector", "reason": "no_lexical_seed",
                                "note": "没有词面种子：不构造离线查询向量"}}
    vector_scores = dict(vector_result.get("scores") or {})
    counts["vector"] = len(vector_scores)
    by_id = {str(item.get("memory_id") or ""): item for item in active}
    scored: dict = {}
    for item in active:
        memory_id = str(item.get("memory_id") or "")
        lexical_score = float(lexical.get(memory_id) or 0.0)
        cosine = vector_scores.get(memory_id)
        semantic = max(lexical_score, float(cosine or 0.0))
        if semantic <= 0:
            continue
        applicability = task_applicability(mode, item, terms=query_terms,
                                          industry_pack_id=industry_pack_id)
        decay = freshness_factor_of(item, now=moment)
        historical = historical_utility(item, usage=(usage or {}).get(memory_id))
        risk = contradiction_risk(item, relations=relations.get(memory_id) or [])
        factors = {
            "semantic_relevance": round(semantic, 8),
            "task_applicability": applicability["value"],
            "confidence": round(_clamp(item.get("confidence") or 0), 8),
            "freshness": decay["value"],
            "historical_utility": historical["value"],
            "contradiction_risk": risk["value"],
        }
        score = recall_score(factors)
        channels = []
        if lexical_score > 0:
            channels.append("lexical")
        if cosine is not None:
            channels.append("vector")
        scored[memory_id] = {
            "item": item, "score": score, "factors": factors, "channels": channels,
            "detail": {"applicability": applicability, "freshness": decay,
                       "historical": historical, "contradiction": risk,
                       "lexical": lexical_score, "cosine": cosine},
        }
    # 图通道：一跳扩展（父命中分 × 边权 × GRAPH_EXPANSION_WEIGHT）
    if graph_expansion and store is not None and scored:
        parents = sorted(scored.items(), key=lambda item: (-item[1]["score"], item[0]))
        for memory_id, row in list(parents):
            for relation in relations.get(memory_id) or []:
                target = str(relation.get("target_memory_id") or "")
                if not target or target in scored or target not in by_id:
                    continue
                target_item = by_id[target]
                if str(target_item.get("status") or "") != "ACTIVE":
                    continue
                weight = _clamp(relation.get("weight") or 0.5)
                score = round(float(row["score"]) * weight * GRAPH_EXPANSION_WEIGHT, 8)
                if score < min_score:
                    continue
                applicability = task_applicability(mode, target_item, terms=query_terms,
                                                   industry_pack_id=industry_pack_id)
                decay = freshness_factor_of(target_item, now=moment)
                risk = contradiction_risk(target_item, relations=relations.get(target) or [])
                historical = historical_utility(target_item, usage=(usage or {}).get(target))
                factors = {
                    "semantic_relevance": round(float(row["factors"]["semantic_relevance"]), 8),
                    "task_applicability": applicability["value"],
                    "confidence": round(_clamp(target_item.get("confidence") or 0), 8),
                    "freshness": decay["value"],
                    "historical_utility": historical["value"],
                    "contradiction_risk": risk["value"],
                }
                scored[target] = {
                    "item": target_item, "score": score, "factors": factors,
                    "channels": ["graph"],
                    "detail": {"applicability": applicability, "freshness": decay,
                               "historical": historical, "contradiction": risk,
                               "graph_parent": memory_id, "graph_weight": weight},
                }
                counts["graph"] += 1
    ordered = sorted(scored.items(), key=lambda item: (-item[1]["score"], item[0]))
    hits: list = []
    for memory_id, row in ordered:
        if row["score"] < min_score:
            counts["below_threshold"] += 1
            continue
        if len(hits) >= limit:
            break
        item = row["item"]
        evidence_refs = []
        if store is not None:
            try:
                evidence_refs = [str(link.get("evidence_ref") or "")
                                 for link in store.memory_evidence(memory_ids=[memory_id])]
            except Exception:    # noqa: BLE001
                evidence_refs = list(item.get("source_evidence_ids") or [])
        explain = ("记忆提示：%s（%s，时效档 %s）；分数 = 词面/向量 %.3f × 任务适配 %.3f × "
                   "置信 %.3f × 时效 %.3f × 历史效用 %.3f − 矛盾风险 %.3f；通道 %s。"
                   "**这不是证据**：用前必须重新核验（Phase 10）。" % (
                       item.get("memory_type"), item.get("status"), item.get("freshness_class"),
                       row["factors"]["semantic_relevance"], row["factors"]["task_applicability"],
                       row["factors"]["confidence"], row["factors"]["freshness"],
                       row["factors"]["historical_utility"],
                       row["factors"]["contradiction_risk"], ",".join(row["channels"])))
        hits.append(memory_hint(item, factors=row["factors"], score=row["score"],
                                channels=row["channels"], evidence_refs=evidence_refs,
                                explain=explain))
    attempted = ["relational", "lexical"]
    if seed_items:
        attempted.append("vector")
    if graph_expansion:
        attempted.append("graph")
    receipt = {
        "recall_version": MEMORY_RECALL_VERSION,
        "hint_version": MEMORY_RECALL_HINT_VERSION,
        "mode": mode,
        "channels": [name for name in MEMORY_RECALL_CHANNELS if name in attempted],
        "hits": hits,
        "counts": dict(counts),
        "stats": {
            "limit": limit, "min_score": min_score, "query_terms": len(query_terms),
            "scope_keys": keys, "types": types,
            "vector": vector_result.get("stats") or {},
            "hits_by_type": _count(hit["memory_type"] for hit in hits),
            "hits_by_channel": _count(channel for hit in hits for channel in hit["channels"]),
            "note": ("命中恒为 MEMORY_HINT（requires_revalidation=True）：本阶段不实现"
                     "revalidation（Phase 10），记忆不能直接进当前证据图"),
        },
        "scope": {"owner_user_id": str(owner_user_id or ""), "session_id": str(session_id or ""),
                  "industry_pack_id": str(industry_pack_id or "")},
        "trace_id": str(trace_id or run_id or ""),
        "query_fingerprint": "MQF" + _digest("%s|%s" % (mode, _norm_text(query).casefold()))[:20],
    }
    ok, note = validate_contract("memory_recall_receipt", dict(receipt))
    receipt["contract_ok"] = bool(ok)
    if not ok:
        receipt["contract_error"] = note
    if store is not None and store_log:
        try:
            store.record_memory_recall({
                "recall_id": "MRL" + _digest("%s|%s|%s|%s" % (
                    receipt["trace_id"], mode, receipt["query_fingerprint"],
                    ",".join(hit["memory_id"] for hit in hits)))[:24],
                "trace_id": receipt["trace_id"], "run_id": str(run_id or ""), "mode": mode,
                "scope_key": scope_key("SESSION", owner_user_id=owner_user_id,
                                       session_id=session_id, industry_pack_id=industry_pack_id),
                "owner_user_id": str(owner_user_id or ""), "session_id": str(session_id or ""),
                "industry_pack_id": str(industry_pack_id or ""),
                "query_fingerprint": receipt["query_fingerprint"], "channels": receipt["channels"],
                "hits": len(hits), "top_score": float(hits[0]["score"]) if hits else 0.0,
                "counts": receipt["counts"], "metadata": receipt["stats"],
            })
            if hits:
                store.bump_memory_usage([hit["memory_id"] for hit in hits], recalled=1,
                                        reuse=recall_counts_reuse())
        except Exception:        # noqa: BLE001 —— 召回留痕失败不影响召回结果
            pass
    return receipt


def mark_memory_used(store, memory_ids: Iterable, *, helped: bool = False) -> int:
    """标记"记忆被真的用上了"（§2.3 reuse_count）：只有这里会递增复用次数。

    与召回**分开**：召回只是"给过提示"，用了才算复用 —— 否则反复召回就能把
    HistoricalUtility 刷高，召回分与实际价值脱钩。
    """
    keys = [str(item) for item in (memory_ids or []) if str(item or "")]
    if not keys or store is None:
        return 0
    return store.bump_memory_usage(keys, used=1, helped=1 if helped else 0, reuse=True)


def memory_context_items(receipt: Mapping, *, max_items: int = 6) -> list:
    """P09-04 → Phase 08 §4 的 `memory_context` 段：把召回命中转成 ContextItem。

    口径：
      · `kind="memory"`、`section="memory_context"`、`source_stage="memory_graph"`；
      · 正文带 `MEMORY_HINT_MARK` 前缀（生成端看得见"这不是证据"）；
      · `grounding.grounded` 只在**能落到证据引用**时为 True，并带 `hint=True` 与
        `requires_revalidation=True`；回溯不上的记忆标 grounded=False 并给理由；
      · 命中里的 `evidence_refs` 一并带进 metadata，便于 Context Inspector 追。
    """
    receipt = receipt if isinstance(receipt, Mapping) else {}
    hits = [hit for hit in (receipt.get("hits") or []) if isinstance(hit, Mapping)]
    if not hits:
        return []
    try:
        from qa_context_pack import make_context_item
    except Exception:        # noqa: BLE001 —— 上下文模块不可用时本函数返回空（不编条目）
        return []
    items = []
    for hit in hits[:max(1, int(max_items))]:
        refs = list(hit.get("evidence_refs") or [])
        grounded = bool(refs)
        text = "%s%s（%s，时效档 %s，召回分 %.3f；用前必须重新核验）" % (
            MEMORY_HINT_MARK, hit.get("canonical_content"), hit.get("memory_type"),
            hit.get("freshness_class"), float(hit.get("score") or 0))
        items.append(make_context_item(
            kind="memory", section="memory_context", text=text, source_stage="memory_graph",
            evidence_ref=str(refs[0]) if refs else "",
            grounding={
                "grounded": grounded, "hint": True,
                "requires_revalidation": True, "verified_evidence": False,
                "memory_id": str(hit.get("memory_id") or ""),
                "reason": ("记忆提示：可回溯到证据引用，但**未经本轮核验**（Phase 10 才做 revalidation）"
                           if grounded else "记忆提示：该记忆链不到证据引用（不可当证据）"),
            },
            metadata={"role": "memory_hint", "memory_id": str(hit.get("memory_id") or ""),
                      "memory_type": str(hit.get("memory_type") or ""),
                      "status": str(hit.get("status") or ""),
                      "score": float(hit.get("score") or 0),
                      "hint_version": str(hit.get("hint_version") or ""),
                      "channels": list(hit.get("channels") or []),
                      "evidence_refs": refs,
                      "requires_revalidation": True},
        ))
    return items


MEMORY_HINT_MARK = "【记忆提示·未重新核验】"
"""上下文与提示里统一使用的记忆标记（生成端据此知道"这条不是证据"）。"""


# ── P09-05：lifecycle（§2.3 六状态 + 确定性衰减/失效）────────────────────────

def decay_score(item: Mapping, *, now=None) -> dict:
    """衰减分（P09-05，**同输入同输出**）：

    `decay = 0.5 ** (age_days / half_life(freshness_class)) × confidence × verified × reuse`

    逐项可复算：
      · `age_days`：从 `last_verified_at`（没有就退到 `created_at`）到现在；
      · `half_life`：§10 时效档对应的半衰期（表在 `HALF_LIFE_DAYS`）；
      · `confidence`：记忆自身的置信度（写门算出来的那个，不是新编的）；
      · `verified`：有通过核验的证据绑定 → 1.0，否则 0.8（没有绑定就不该被当成可信记忆用）；
      · `reuse`：被真的复用过的记忆衰减更慢（`1 + 0.05×min(reuse_count, 4)`，封顶 1.2）；
      · `valid_until` 已过 → 分数直接按 0 报（过期就是过期，不做平滑）。
    """
    item = item if isinstance(item, Mapping) else {}
    moment = _now_dt(now)
    freshness = str(item.get("freshness_class") or "MEDIUM")
    half_life = float(HALF_LIFE_DAYS.get(freshness, 365.0))
    anchor = _parse_time(item.get("last_verified_at")) or _parse_time(item.get("created_at")) or moment
    age_days = max(0.0, (moment - anchor).total_seconds() / 86400.0)
    decay = 0.5 ** (age_days / max(1e-6, half_life))
    confidence = _clamp(item.get("confidence") or 0)
    verified = 1.0 if (item.get("source_evidence_ids") or item.get("evidence_refs")) else 0.8
    reuse = min(1.2, 1.0 + 0.05 * min(int(item.get("reuse_count") or 0), 4))
    score = decay * confidence * verified * min(1.0, reuse)
    valid_until = _parse_time(item.get("valid_until"))
    expired = bool(valid_until and valid_until < moment)
    if expired:
        score = 0.0
    return {
        "score": round(_clamp(score), 8), "age_days": round(age_days, 4),
        "half_life_days": half_life, "freshness_class": freshness,
        "decay_component": round(_clamp(decay), 8), "confidence": round(confidence, 8),
        "verified": verified, "reuse_multiplier": round(reuse, 8),
        "expired": expired, "status": str(item.get("status") or "ACTIVE"),
    }


def plan_transition(item: Mapping, *, now=None) -> dict:
    """给定记忆与时钟，算出**应当**处于的状态（纯函数，不写库）。返回 {from,to,reason,decay}。"""
    item = item if isinstance(item, Mapping) else {}
    decay = decay_score(item, now=now)
    before = str(item.get("status") or "ACTIVE")
    if before in ("SUPERSEDED", "CONTRADICTED", "REVOKED", "EXPIRED"):
        return {"from": before, "to": before, "reason": "NO_CHANGE", "decay": decay}
    if decay["expired"]:
        return {"from": before, "to": "EXPIRED", "reason": "VALID_UNTIL_PASSED", "decay": decay}
    if decay["score"] < EXPIRE_FLOOR:
        return {"from": before, "to": "EXPIRED", "reason": "DECAY_BELOW_EXPIRE_FLOOR", "decay": decay}
    if decay["score"] < STALE_FLOOR:
        return {"from": before, "to": "STALE", "reason": "DECAY_BELOW_STALE_FLOOR", "decay": decay}
    if before == "STALE":
        return {"from": before, "to": "ACTIVE", "reason": "DECAY_ABOVE_STALE_FLOOR", "decay": decay}
    return {"from": before, "to": before, "reason": "NO_CHANGE", "decay": decay}


def apply_lifecycle(store, *, now=None, scope_keys=(), include_all_scopes: bool = True,
                    dry_run: bool = False, audit=None) -> dict:
    """P09-05：跑一次生命周期维护（确定性、幂等）。

    幂等性怎么证明：迁移只在 `状态真的会变` 时写库（`plan_transition` 是纯函数），
    所以**同一时钟连跑两次，第二次 transitions = 0**（有守例钉死）。
    只自动产出 ACTIVE/STALE/EXPIRED；`SUPERSEDED`/`CONTRADICTED`/`REVOKED` 原样尊重。
    """
    moment = _now_dt(now)
    items = []
    if store is not None:
        try:
            items = store.load_memory_items(scope_keys=scope_keys,
                                            include_all_scopes=bool(include_all_scopes),
                                            limit=5000)
        except Exception:        # noqa: BLE001
            items = []
    transitions: list = []
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        # 终态（SUPERSEDED/CONTRADICTED/REVOKED/EXPIRED）**一行都不碰**：
        # 它们由 Phase 10/15 产出，本阶段只尊重；衰减分对它们也没有意义。
        if str(item.get("status") or "") in ("SUPERSEDED", "CONTRADICTED", "REVOKED", "EXPIRED"):
            continue
        plan = plan_transition(item, now=moment)
        if plan["to"] == plan["from"] and plan["reason"] == "NO_CHANGE":
            # 状态不变但衰减分要刷新（分本身会随时间变），但只在真的变了的时候写；
            # 纯衰减刷新**不追加版本行**（衰减分是可复算派生量，不是演化事件）。
            current = float(item.get("decay_score") or 0)
            if abs(current - float(plan["decay"]["score"])) < 1e-9:
                continue
            if dry_run or store is None:
                transitions.append({"memory_id": item.get("memory_id"), "from": plan["from"],
                                    "to": plan["to"], "reason": "DECAY_REFRESH",
                                    "decay": plan["decay"]["score"]})
                continue
            store.update_memory_status(str(item.get("memory_id") or ""), status=plan["to"],
                                       decay_score=plan["decay"]["score"],
                                       reason="DECAY_REFRESH", change="DECAY", now=_stamp(moment),
                                       append_version=False)
            transitions.append({"memory_id": item.get("memory_id"), "from": plan["from"],
                                "to": plan["to"], "reason": "DECAY_REFRESH",
                                "decay": plan["decay"]["score"]})
            continue
        if plan["to"] == plan["from"]:
            continue
        transitions.append({"memory_id": item.get("memory_id"), "from": plan["from"],
                            "to": plan["to"], "reason": plan["reason"],
                            "decay": plan["decay"]["score"]})
        if dry_run or store is None:
            continue
        store.update_memory_status(str(item.get("memory_id") or ""), status=plan["to"],
                                   decay_score=plan["decay"]["score"], reason=plan["reason"],
                                   change="EXPIRE" if plan["to"] == "EXPIRED" else "STATUS",
                                   now=_stamp(moment))
    report = {
        "lifecycle_version": MEMORY_LIFECYCLE_VERSION,
        "checked": len([item for item in items if isinstance(item, Mapping)]),
        "transitions": transitions,
        "transition_counts": _count("%s->%s/%s" % (row["from"], row["to"], row["reason"])
                                    for row in transitions),
        "status_counts": _count(str(item.get("status") or "") for item in items
                                if isinstance(item, Mapping)),
        "decay_bands": _decay_bands(items, now=moment),
        "dry_run": bool(dry_run),
        "note": ("只自动产出 ACTIVE/STALE/EXPIRED；SUPERSEDED/CONTRADICTED/REVOKED 由 "
                 "Phase 10/15 产出，本阶段只尊重（不复活、不覆盖）"),
    }
    ok, note = validate_contract("memory_lifecycle_report", dict(report))
    report["contract_ok"] = bool(ok)
    if not ok:
        report["contract_error"] = note
    if audit is not None and not dry_run:
        try:
            audit.record("memory_lifecycle", payload={
                "checked": report["checked"], "transitions": len(transitions),
                "lifecycle_version": MEMORY_LIFECYCLE_VERSION})
        except Exception:        # noqa: BLE001
            pass
    return report


def _decay_bands(items: Sequence[Mapping], *, now=None) -> dict:
    """衰减分分桶（真跑分布的证据口径）：[0,0.05) [0.05,0.25) [0.25,0.5) [0.5,0.8) [0.8,1]。"""
    bands = {"0-0.05": 0, "0.05-0.25": 0, "0.25-0.5": 0, "0.5-0.8": 0, "0.8-1.0": 0}
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        score = float(decay_score(item, now=now).get("score") or 0)
        if score < 0.05:
            bands["0-0.05"] += 1
        elif score < 0.25:
            bands["0.05-0.25"] += 1
        elif score < 0.5:
            bands["0.25-0.5"] += 1
        elif score < 0.8:
            bands["0.5-0.8"] += 1
        else:
            bands["0.8-1.0"] += 1
    return bands


def lifecycle_allowed_transition(before: str, after: str) -> bool:
    """状态迁移是否被本阶段允许（守例直接调它，口径单一真源）。"""
    return (str(before), str(after)) in MEMORY_LIFECYCLE_TRANSITIONS


# ── P09-06：provenance 审计 + 三通道召回的可复算统计 ────────────────────────

def provenance_report(store, *, scope_keys=(), include_all_scopes: bool = True,
                      run_id: str = "") -> dict:
    """P09-06：逐条审计"每条记忆能否回到证据/来源"。

    回溯成立的条件（三个都要）：
      ① 至少一条 `memory_evidence_link`；
      ② 该链接有非空 `source_fingerprint`（Phase 02 的来源身份）；
      ③ 该链接有非空 `span_fingerprint` 或 `evidence_ref`（Phase 02 的最小 span / 证据引用）。
    回溯不上的记忆**逐条列进 `untraceable`**（不四舍五入成"都能回溯"）。
    """
    items = []
    if store is not None:
        try:
            items = store.load_memory_items(scope_keys=scope_keys,
                                            include_all_scopes=bool(include_all_scopes),
                                            limit=5000)
        except Exception:        # noqa: BLE001
            items = []
    ids = [str(item.get("memory_id") or "") for item in items if isinstance(item, Mapping)]
    links_by_memory: dict = {}
    if store is not None and ids:
        try:
            for row in store.memory_evidence(memory_ids=ids) or []:
                links_by_memory.setdefault(str(row.get("memory_id") or ""), []).append(row)
        except Exception:        # noqa: BLE001
            links_by_memory = {}
    traceable, untraceable = 0, []
    link_counts = {"evidence": 0, "with_source_fingerprint": 0, "with_span": 0, "with_run_id": 0,
                   "with_corpus_version": 0, "verified_verdict": 0}
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        memory_id = str(item.get("memory_id") or "")
        links = links_by_memory.get(memory_id) or []
        link_counts["evidence"] += len(links)
        ok = False
        for row in links:
            has_source = bool(str(row.get("source_fingerprint") or ""))
            has_span = bool(str(row.get("span_fingerprint") or row.get("evidence_ref") or ""))
            link_counts["with_source_fingerprint"] += 1 if has_source else 0
            link_counts["with_span"] += 1 if has_span else 0
            link_counts["with_run_id"] += 1 if str(row.get("run_id") or "") else 0
            link_counts["with_corpus_version"] += 1 if str(row.get("corpus_version") or "") else 0
            link_counts["verified_verdict"] += 1 if str(row.get("verdict") or "") == \
                EVIDENCE_STATUS_SUPPORTED else 0
            if has_source and has_span:
                ok = True
        if ok:
            traceable += 1
        else:
            untraceable.append({"memory_id": memory_id, "memory_type": item.get("memory_type"),
                                "scope": item.get("scope"), "links": len(links),
                                "reason": "NO_EVIDENCE_LINK" if not links else "LINK_INCOMPLETE"})
    report = {
        "provenance_version": MEMORY_PROVENANCE_VERSION,
        "graph_version": MEMORY_GRAPH_VERSION,
        "run_id": str(run_id or ""),
        "checked": len([item for item in items if isinstance(item, Mapping)]),
        "traceable": traceable,
        "untraceable": untraceable,
        "links": link_counts,
        "channels": {"relational": "作用域+状态精确匹配（scope_key）",
                     "graph": "memory_relation 一跳扩展",
                     "vector": "库内已有向量余弦（离线质心，零端点调用）",
                     "lexical": "词面覆盖（qa_verifier.term_set 口径）"},
        "note": ("每条记忆都必须能回到 Phase 02 的来源指纹 + 最小 span；"
                 "回溯不上的逐条列出，不做四舍五入"),
    }
    ok, note = validate_contract("memory_provenance_report", dict(report))
    report["contract_ok"] = bool(ok)
    if not ok:
        report["contract_error"] = note
    return report


def memory_receipt(receipt: Mapping) -> dict:
    """给 stats/SSE 的召回回执（不含记忆正文：只给计数、通道与口径）。"""
    receipt = receipt if isinstance(receipt, Mapping) else {}
    stats = receipt.get("stats") if isinstance(receipt.get("stats"), Mapping) else {}
    return {
        "recall_version": receipt.get("recall_version"),
        "hint_version": receipt.get("hint_version") or MEMORY_RECALL_HINT_VERSION,
        "mode": receipt.get("mode"),
        "hits": len(receipt.get("hits") or []),
        "counts": dict(receipt.get("counts") or {}),
        "hits_by_type": dict(stats.get("hits_by_type") or {}),
        "hits_by_channel": dict(stats.get("hits_by_channel") or {}),
        "vector": dict(stats.get("vector") or {}),
        "scope_keys": list(stats.get("scope_keys") or []),
        "requires_revalidation": len(receipt.get("hits") or []),
        "verified_evidence": 0,
        "top_score": float((receipt.get("hits") or [{}])[0].get("score") or 0)
        if receipt.get("hits") else 0.0,
        "note": stats.get("note") or "",
    }


def recall_from_run(store, *, run_meta: Mapping, graph: Mapping | None = None,
                    question: str = "", mode: str = "evidence", vectors=None,
                    now=None, store_log: bool = True, trace_id: str = "") -> dict:
    """管线入口（P09-04 + P09-06）：按 run 的作用域召回并落日志。

    作用域三键一律取 run 自己的（`owner_user_id` / `session_id` / `industry_pack_id`），
    调用方不许改写 —— 跨会话串味是这一层最危险的错（§14）。
    """
    run_meta = run_meta if isinstance(run_meta, Mapping) else {}
    graph = graph if isinstance(graph, Mapping) else {}
    # 查询实词 = **问题实词 ∪ 本轮结论实词**：§10 的 evidence recall 发生在缺口/结论已经
    # 出现之后，"用哪句话去查记忆"当然要包含这轮到底在讨论什么（只用原始问句会漏掉
    # 结论里出现、问句里没写的说法——真机复算里这是命中率的主要来源）。
    terms = term_set(question, sizes=(2, 3)) if question else set()
    for node in graph.get("claims") or []:
        if not isinstance(node, Mapping):
            continue
        claim = node.get("claim") if isinstance(node.get("claim"), Mapping) else {}
        text = str(claim.get("text") or node.get("text") or "")
        if text:
            terms |= term_set(text, sizes=(2, 3))
    return recall(store, mode=mode, query=str(question or ""), terms=terms,
                  owner_user_id=str(run_meta.get("owner_user_id") or ""),
                  session_id=str(run_meta.get("session_id") or ""),
                  industry_pack_id=str(run_meta.get("industry_pack_id") or ""),
                  run_id=str(run_meta.get("id") or ""), trace_id=str(trace_id or run_meta.get("id") or ""),
                  vectors=vectors, now=now, store_log=store_log)


def load_vectors(database=None):
    """库内已有向量的加载器（复用 Phase 04 的实现；零端点调用）。"""
    try:
        from qa_hunters import load_article_vectors

        return load_article_vectors(database)
    except Exception:        # noqa: BLE001
        return [], None


def write_memories_from_graph(store, *, graph: Mapping, run_meta: Mapping, trace_id: str = "",
                              scope: str = "PATIENT_LONGITUDINAL", now=None, audit=None) -> dict:
    """管线入口（P09-02 + P09-03）：把本轮**已核验**的结论/实体写进记忆图。

    §9 主执行流里 Write Gate 在 Final Answer 之后；本函数只吃证据图（已核验结论 +
    被核验证据），**不吃最终答案文本** —— 生成端自由生成的内容永远不进货（MASTER_RULES 11）。
    """
    run_meta = run_meta if isinstance(run_meta, Mapping) else {}
    candidates = memory_candidates_from_graph(
        graph, scope=scope, owner_user_id=str(run_meta.get("owner_user_id") or ""),
        session_id=str(run_meta.get("session_id") or ""),
        industry_pack_id=str(run_meta.get("industry_pack_id") or ""),
        run_id=str(run_meta.get("id") or ""), stage="memory_write_gate", now=now)
    return apply_write_gate(store, candidates, run_id=str(run_meta.get("id") or ""),
                            trace_id=str(trace_id or run_meta.get("id") or ""), now=now,
                            audit=audit)


__all__ = [
    "DEFAULT_RECALL_LIMIT", "DEFAULT_RECALL_MIN_SCORE", "DEFAULT_WRITE_MIN_UTILITY",
    "EXPIRE_FLOOR", "GRAPH_EXPANSION_WEIGHT", "HALF_LIFE_DAYS", "MEMORY_HINT_MARK",
    "REUSE_BASE", "STABILITY_BASE", "STALE_FLOOR", "STALENESS_BASE", "TTL_DAYS",
    "apply_lifecycle", "apply_write_gate", "build_memory_item", "canonicalize",
    "claim_evidence_rows", "classify_freshness", "content_fingerprint",
    "contradiction_risk", "decay_score", "decide_write", "evidence_link_row",
    "existing_memory_index", "external_instruction", "freshness_factor_of",
    "historical_utility", "information_value", "lifecycle_allowed_transition",
    "load_vectors", "mark_memory_used", "memory_candidates_from_graph",
    "memory_context_items", "memory_graph_enabled", "memory_hint", "memory_id_for",
    "memory_receipt", "memory_version_row", "plan_transition", "provenance_report",
    "recall", "recall_from_run", "recall_limit", "recall_min_score", "recall_score",
    "scope_key", "semantic_relevance", "sensitive_hits", "task_applicability",
    "type_policy", "validate_memory_item", "vector_channel", "visible_scope_keys",
    "write_gate_receipt", "write_memories_from_graph", "write_min_utility", "write_utility",
]

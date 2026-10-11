#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图谱与执行链的**机器可校验契约**（graph-rag-v2 通用包 Phase 01 · F-6/F-9）。

为什么要这个文件：通用包 MASTER_RULES 第 10 条要求
"Node / Edge / Skill / Context / Memory 必须使用**机器可校验的 contract**"。
在此之前，本仓库的图/边/通道/失败策略都是**散落各处的字符串字面量**：
  · 关系类型在 `kg_builder.py:51-53`；
  · 节点类型以字面量出现在 `kg_builder.py:228/243/245/250/312`；
  · Claim–Evidence 边字段在 `qa_reasoning.py:220-225`；
  · 检索通道名散落在 `qa_retrieval.py`；
  · 审计事件名散落在 `qa_orchestrator.py`。

本模块**只做"把既有取值提成单一事实源"**，不引入任何依赖、不改任何行为、不动既有取值
（等价替换：值一个字都不改）。后续阶段（05 执行图、11 Skill、15 审计）从这里取常量，
避免各处字符串漂移。

Phase 02（证据层）在这里追加 `Evidence Object / Source / Span / Entity / Relation` 五个
schema 与证据状态枚举；**既有取值与既有 schema 一个字不改**，`qa_contracts.EVIDENCE_SCHEMA`
（`additionalProperties: False` 的历史冻结契约）更是完全不碰——证据层新字段一律走它放行的
`metadata` 对象。

Phase 03（核验层）在这里追加 `EVIDENCE_VERIFICATION_SCHEMA`（核验结论），并把它作为
`EVIDENCE_OBJECT_SCHEMA` 的**可选**字段 `verification` 挂上去：不核验的调用方照旧能过校验，
`status`/`relationship` 的语义与 Phase 02 逐字相同（回归用例钉着）。

边界：本文件是纯常量 + schema（外加一个不参与链路的自校验函数），**不含业务逻辑**，
不被任何写路径依赖；证据层的构造/指纹/去重逻辑在 `qa_evidence.py`，
核验逻辑在 `qa_verifier.py`。

Phase 05（研究规划与执行图）在这里追加 `QUERY_INTENTS`/`QA_PATHS`/`EXECUTION_NODE_KINDS`/
`MODEL_TIERS` 等取值，并把 §2.2 的 Node 契约全字段（purpose/input_schema/output_schema/
timeout/retry/model_tier/allowed_tools/validation/failure_policy）作为**可选字段**挂到
`EXECUTION_NODE_SCHEMA` 上，另加 `EXECUTION_GRAPH_SCHEMA`/`SUB_QUESTION_SCHEMA`/
`PLAN_CLAIM_SCHEMA`。既有取值与既有 schema 的 required/枚举一个字不改（Phase 01 的
`validate("execution_node", {"node_id": "plan"})` 继续成立）；规划/建图逻辑在
`qa_query_interpreter.py` 与 `qa_execution_graph.py`。
"""

from __future__ import annotations

from typing import Mapping, Tuple

# ── 版本 ──────────────────────────────────────────────────────────────────────
GRAPH_CONTRACT_VERSION = "graph-contract-v1"
"""图谱契约版本；与 `qa_reasoning.ADJUDICATION_VERSION`、`qa_schema.QA_SCHEMA_VERSION`
是三件不同的事（分别是"图结构契约""裁决规则版本""库表结构版本"），不要混用。"""

# ── 知识图谱：节点类型（等价取自 kg_builder.py 的字面量）────────────────────────
KG_NODE_ENTITY = "entity"
KG_NODE_TOPIC = "topic"
KG_NODE_VALUE = "value"
KG_NODE_TYPES: Tuple[str, ...] = (KG_NODE_ENTITY, KG_NODE_TOPIC, KG_NODE_VALUE)
"""节点类型：主体实体 / 主题 / 属性取值（`kg_builder.py:228/243/245/250/312`）。"""

# ── 知识图谱：边关系类型（等价取自 kg_builder.py:51-53）────────────────────────
KG_RELATION_EVENT = "event"
KG_RELATION_ATTRIBUTE = "attribute"
KG_RELATION_COOCCURRENCE = "cooccurrence"
KG_RELATION_KINDS: Tuple[str, ...] = (
    KG_RELATION_EVENT, KG_RELATION_ATTRIBUTE, KG_RELATION_COOCCURRENCE,
)
"""边关系：事件三元组 / 属性（带 attr_key 与有效期）/ 共现。"""

# ── Claim–Evidence 边：关系取值（等价取自 qa_contracts.EVIDENCE_SCHEMA 与 qa_reasoning）──
CLAIM_EVIDENCE_RELATIONSHIPS: Tuple[str, ...] = (
    "supports", "contradicts", "qualifies", "context",
)
"""证据对 claim 的关系；`qa_reasoning.py:220` 缺省为 `supports`。
与契约里的 `relationship` 枚举一致（旧契约里还有一个 `null` 表示"未判定"，用空串表达）。"""

# ── 检索通道（route）：等价取自 qa_retrieval 的实际取值 ────────────────────────
QA_ROUTE_KEYWORD = "keyword"
QA_ROUTE_SEMANTIC = "semantic"
QA_ROUTE_GRAPH = "graph"
QA_ROUTE_PAGE_CONTEXT = "page_context"
QA_ROUTE_POLICY_EXACT = "policy_exact"
QA_ROUTE_WEB = "web"
QA_ROUTE_GRAPH_ATTRIBUTE = "graph_attribute"
QA_RETRIEVAL_ROUTES: Tuple[str, ...] = (
    QA_ROUTE_KEYWORD, QA_ROUTE_SEMANTIC, QA_ROUTE_GRAPH, QA_ROUTE_GRAPH_ATTRIBUTE,
    QA_ROUTE_PAGE_CONTEXT, QA_ROUTE_POLICY_EXACT, QA_ROUTE_WEB,
)
"""检索通道名；用于 SearchTrace 的 `route` 字段与统计口径（阶段 04/16 会按通道出报表）。"""

# ── 节点失败策略（通用包 01_V2_ARCHITECTURE 的 5 值枚举）──────────────────────
QA_FAILURE_FAIL_FAST = "FAIL_FAST"
QA_FAILURE_RETRY = "RETRY"
QA_FAILURE_SKIP = "SKIP"
QA_FAILURE_FALLBACK = "FALLBACK"
QA_FAILURE_DEGRADE = "DEGRADE"
QA_FAILURE_POLICIES: Tuple[str, ...] = (
    QA_FAILURE_FAIL_FAST, QA_FAILURE_RETRY, QA_FAILURE_SKIP,
    QA_FAILURE_FALLBACK, QA_FAILURE_DEGRADE,
)
"""执行节点失败策略五值。本仓库现状：`qa_orchestrator.DEGRADABLE_STAGES` 实现的是
`DEGRADE`，其余四值暂未落到具体阶段——本常量先作为契约占位，阶段 05 建执行图时引用。"""

# ── 循环停止原因（通用包 01_V2_ARCHITECTURE 的 5 值枚举）─────────────────────
QA_STOP_ANSWERABLE = "ANSWERABLE"
QA_STOP_BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
QA_STOP_MAX_DEPTH = "MAX_DEPTH"
QA_STOP_NO_GAIN = "NO_GAIN"
QA_STOP_UNRESOLVABLE_CONTRADICTION = "UNRESOLVABLE_CONTRADICTION"
QA_STOP_REASONS: Tuple[str, ...] = (
    QA_STOP_ANSWERABLE, QA_STOP_BUDGET_EXHAUSTED, QA_STOP_MAX_DEPTH,
    QA_STOP_NO_GAIN, QA_STOP_UNRESOLVABLE_CONTRADICTION,
)
"""多跳循环的停止原因。本仓库现状：预算耗尽会写 `status='skipped_budget'`，
"无缺失链接"会自然收束——对应 `BUDGET_EXHAUSTED` 与 `ANSWERABLE`；`MAX_DEPTH` 由
执行图的跳数上限给出；**Phase 07 补齐 `NO_GAIN` 与 `UNRESOLVABLE_CONTRADICTION`**
（`qa_gap_analyzer.GapLoopState`：连续若干轮无新增有效证据/无新增 claim → NO_GAIN；
Phase 06 矛盾裁决 unresolved 且已无可用下一跳 → UNRESOLVABLE_CONTRADICTION）。"""

# ── 研究规划与执行图（Phase 05 · P05-01…P05-05）────────────────────────────
# 通用包 01_V2_ARCHITECTURE §6（Query Interpreter）/ §7（Research Planner）/
# §18（Fast/Standard/Deep）/ §2.2（Node Contract）/ §28（Failure Policy）。
# 本段只**新增**取值与 schema：既有取值（通道/失败策略/停止原因/Hunter）一个字不动。
QUERY_INTERPRETER_VERSION = "qa-query-interpreter-v1"
"""Query Interpreter 版本；换规则=换版本（与 GRAPH/EVIDENCE/HUNTER 三个版本是三件事）。"""

# §6 的问题结构分类（9 个取值，逐字取自规格书）
QUERY_SIMPLE_FACT = "SIMPLE_FACT"
QUERY_MULTI_ENTITY = "MULTI_ENTITY"
QUERY_COMPARISON = "COMPARISON"
QUERY_TEMPORAL = "TEMPORAL"
QUERY_CAUSAL = "CAUSAL"
QUERY_MECHANISM = "MECHANISM"
QUERY_DIAGNOSTIC = "DIAGNOSTIC"
QUERY_MULTI_HOP = "MULTI_HOP"
QUERY_SYNTHESIS = "SYNTHESIS"
QUERY_INTENTS: Tuple[str, ...] = (
    QUERY_SIMPLE_FACT, QUERY_MULTI_ENTITY, QUERY_COMPARISON, QUERY_TEMPORAL,
    QUERY_CAUSAL, QUERY_MECHANISM, QUERY_DIAGNOSTIC, QUERY_MULTI_HOP, QUERY_SYNTHESIS,
)
"""问题结构分类（§6）。本仓库落地口径：**复用** `qa_planner._CATEGORY_RULES` 的既有类别，
再按"是不是多实体/比较/冲突"派生到这里（映射规则见 `qa_query_interpreter.intent_of`），
不另写一套分类器。"""

QUERY_COMPLEXITY_SIMPLE = "simple"
QUERY_COMPLEXITY_STANDARD = "standard"
QUERY_COMPLEXITY_DEEP = "deep"
QUERY_COMPLEXITIES: Tuple[str, ...] = (
    QUERY_COMPLEXITY_SIMPLE, QUERY_COMPLEXITY_STANDARD, QUERY_COMPLEXITY_DEEP,
)
"""复杂度三档（§6：`complexity = simple` 直接进 Fast Path）。"""

QA_ANSWER_TYPES: Tuple[str, ...] = (
    "no_answer", "fact", "comparison", "timeline", "causal_explanation",
    "diagnostic", "evidence_synthesis",
)
"""期望答案形态。`no_answer` = 不需要检索（闲聊/自指类问题），直接快路径回话。"""

QA_PATH_FAST = "fast"
QA_PATH_STANDARD = "standard"
QA_PATH_DEEP = "deep"
QA_PATHS: Tuple[str, ...] = (QA_PATH_FAST, QA_PATH_STANDARD, QA_PATH_DEEP)
"""三条执行路径（§18）。取值与既有 `mode` 一致（fast/standard/deep），不新造命名。"""

EXECUTION_GRAPH_VERSION = "qa-execution-graph-v1"
"""执行图契约版本（§3.1 Execution Graph：谁执行/能否并行/何时汇合/失败怎么办/何时停）。"""

EXECUTION_NODE_KINDS: Tuple[str, ...] = (
    "plan", "retrieve", "rerank", "merge", "verify", "evidence_graph",
    "gap_loop", "contradiction", "answer", "final_verify",
)
"""节点种类：与 §18 的三条路径逐段对应，且能映射到既有编排阶段（见 qa_execution_graph）。"""

NODE_STATUSES: Tuple[str, ...] = (
    "pending", "ok", "empty", "skipped", "degraded", "error", "timeout",
    "budget_exhausted", "deferred",
)
"""节点结局。`deferred` = 属于后续 Phase、本阶段**明确不执行**（不许假装跑过）；
`budget_exhausted` = 总预算不够、被计划裁掉；`skipped` = 前置条件不满足。"""

MODEL_TIERS: Tuple[str, ...] = ("none", "rule", "small", "medium", "strong")
"""模型分层（§29）。本轮硬约束**不许调模型**：所有实际执行的节点都是 `none`/`rule`；
`small`/`medium`/`strong` 只用于**声明**（后续阶段真正接入时按声明取模型）。"""

PLAN_NODE_KINDS: Tuple[str, ...] = ("sub_question", "claim", "evidence_requirement")
"""P05-02 的规划产物类型：子问题 / Claim / 证据要求（§7 的四件事里 dependency 走边表达）。"""

PLAN_CLAIM_ROLES: Tuple[str, ...] = ("answer", "cause", "mechanism", "link", "counter")
"""Claim 角色：待回答命题 / 原因 / 机制 / 传导连接 / 反证与替代解释（§7 的 H1–H5 抽象）。"""

# ── 证据层（Phase 02 · Evidence Object / Source / Span / Entity / Relation）────
# 通用包 01_V2_ARCHITECTURE §9 的 Evidence Object 契约：
#   "Chunk 可以很长，但真正进入 Evidence Graph 的应该是支持某个 Claim 的最小证据 Span"。
# 本仓库现状（Phase 01 对齐盘点结论）：证据条目里**没有** quote_span、没有 entities/
# relations，且判定字段叫 `relationship`（supports/contradicts/qualifies/context）。
# 这里的取值口径是：
#   · `relationship` **一个字不改**（既有契约 EVIDENCE_SCHEMA 与 qa_reasoning 都在用）；
#   · `status` 是它的规范化派生值（通用包用 SUPPORTED 这一套大写枚举），两者并存，
#     证据层与四图都读 `status`，旧调用方继续读 `relationship`。
EVIDENCE_LAYER_VERSION = "qa-evidence-v1"
"""证据层版本；与 `GRAPH_CONTRACT_VERSION`、`qa_schema.QA_SCHEMA_VERSION` 是三件事
（分别是"证据对象契约""图结构契约""库表结构版本"），不要混用。"""

EVIDENCE_STATUS_SUPPORTED = "SUPPORTED"
EVIDENCE_STATUS_REFUTED = "REFUTED"
EVIDENCE_STATUS_QUALIFIED = "QUALIFIED"
EVIDENCE_STATUS_CONTEXT = "CONTEXT"
EVIDENCE_STATUS_UNVERIFIED = "UNVERIFIED"
EVIDENCE_STATUSES: Tuple[str, ...] = (
    EVIDENCE_STATUS_SUPPORTED, EVIDENCE_STATUS_REFUTED, EVIDENCE_STATUS_QUALIFIED,
    EVIDENCE_STATUS_CONTEXT, EVIDENCE_STATUS_UNVERIFIED,
)
"""证据对 Claim 的判定状态。`UNVERIFIED` = 尚未判定（旧数据 relationship 为空的缺省）。
Phase 03 起核验结论记在 `metadata.evidence_layer.verification.verdict`（同取值域），
`status` 仍保持 Phase 02 的"relationship 规范化映射"语义——两者并存、不互相顶替
（既有调用方与 Phase 02 回归用例都按 `status` 读）。"""

EVIDENCE_STATUS_BY_RELATIONSHIP = {
    "supports": EVIDENCE_STATUS_SUPPORTED,
    "contradicts": EVIDENCE_STATUS_REFUTED,
    "qualifies": EVIDENCE_STATUS_QUALIFIED,
    "context": EVIDENCE_STATUS_CONTEXT,
}
"""既有 `relationship` 取值 → 规范化 `status`；查不到（含空串/None）落 UNVERIFIED。"""

EVIDENCE_SPAN_SOURCES: Tuple[str, ...] = ("query_terms", "matched_keywords", "anchor", "lead", "whole")
"""最小 span 的定位方式（可解释性用）：命中问题实词 / 命中关键词 / 政策锚点 /
都没有就取开头一段 / 整条短于上限本身就是最小 span。"""

EVIDENCE_SEEN_STATUSES: Tuple[str, ...] = ("seen", "confirmed", "rejected")
"""证据 `seen` 集合的三种身份（MASTER_RULES 第 14 条：被拒证据仍属于 seen）。

· `confirmed` = 通过检索层闸门、进入本轮证据包（**不是** verifier 级确认，那属 Phase 03）；
· `rejected` = 被闸门拒掉（相关性/政策/材料噪声），必须留身份，避免下一轮重复捞同一批垃圾；
· `seen` = 见过但尚未分类（保留值，给"只登记去重、不表态"的调用方用）。"""

# ── 审计事件类型（等价取自 qa_orchestrator 的字面量）─────────────────────────
QA_AUDIT_EVENT_TYPES: Tuple[str, ...] = (
    "qa_run_created",
    "qa_stage_completed",
    "qa_stage_degraded",
    "qa_run_completed",
    "qa_run_failed",
    "qa_action_requested",
)
"""审计事件类型占位元组：把散落的字面量集中到一处，便于阶段 15 做审计回放。
（取值以现有调用点为准，新增事件时**必须**在这里登记。）"""

# ── 机器可校验 schema（strict object；additionalProperties=False）────────────
# 只声明"必须有、且必须是什么类型"的最小字段集；多余字段由各自的表结构承载，
# 因此这里对 node/edge 采用"必需字段严格 + 不禁止额外字段"的折中，避免与既有实现打架。

KG_NODE_SCHEMA = {
    "type": "object",
    "properties": {
        "pack_id": {"type": "string"},          # 作用域（现实现 = industry_pack_id）
        "node_key": {"type": "string"},
        "node_type": {"type": "string", "enum": list(KG_NODE_TYPES)},
        "label": {"type": "string"},
    },
    "required": ["node_key", "node_type"],
    "additionalProperties": True,
}

KG_EDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "pack_id": {"type": "string"},
        "src_key": {"type": "string"},
        "dst_key": {"type": "string"},
        "relation_kind": {"type": "string", "enum": list(KG_RELATION_KINDS)},
        "attr_key": {"type": "string"},          # 仅 attribute 边
        "attr_value": {"type": "string"},        # 仅 attribute 边
        "valid_from": {"type": "string"},
        "valid_to": {"type": "string"},
        "evidence_quote": {"type": "string"},
    },
    "required": ["src_key", "dst_key", "relation_kind"],
    "additionalProperties": True,
}

CLAIM_EVIDENCE_EDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "claim_id": {"type": "string"},
        "evidence_ref": {"type": "string"},
        "relationship": {"type": "string", "enum": list(CLAIM_EVIDENCE_RELATIONSHIPS)},
        "relevance_score": {"type": "number", "minimum": 0},
        "published_at": {"type": "string"},
        "scope": {"type": "string"},
    },
    "required": ["claim_id", "evidence_ref"],
    "additionalProperties": True,
}

SEARCH_TRACE_SCHEMA = {
    "type": "object",
    "properties": {
        "hop_index": {"type": "integer", "minimum": 0},
        "round_index": {"type": "integer", "minimum": 0},
        "sub_query": {"type": "string"},
        "route": {"type": "string", "enum": list(QA_RETRIEVAL_ROUTES) + [""]},
        "results": {"type": "integer", "minimum": 0},
        "accepted": {"type": "integer", "minimum": 0},
        "rejected": {"type": "integer", "minimum": 0},
        "new_claims": {"type": "integer", "minimum": 0},
        "resolved_gap": {"type": "integer", "minimum": 0},
        "gap_id": {"type": "string"},
        "latency_ms": {"type": "integer", "minimum": 0},
    },
    "required": ["hop_index"],
    "additionalProperties": True,
}

# ── 信号（Node 的输入/输出契约引用）───────────────────────────────────────
SIGNAL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},                       # 契约名（如 qa.research_plan / qa.search_trace）
        "fields": {"type": "array", "items": {"type": "string"}},   # 该契约承载的字段名（可读性用）
    },
    "required": ["name"],
    "additionalProperties": True,
}
"""Edge 也是数据契约（§2.3）：节点声明"我输出什么结构、下游需要什么结构"，
这里用 `{name, fields}` 表达对契约的引用，不复制契约实体（避免两处定义漂移）。"""

EXECUTION_NODE_SCHEMA = {
    "type": "object",
    "properties": {
        "node_id": {"type": "string"},
        "node_kind": {"type": "string"},
        "parent_node_id": {"type": "string"},
        "failure_policy": {"type": "string", "enum": list(QA_FAILURE_POLICIES)},
        "stop_reason": {"type": "string", "enum": list(QA_STOP_REASONS) + [""]},
        # ── Phase 05（P05-05）：§2.2 的 Node 契约全字段（全部可选，既有调用方零改动）──
        "purpose": {"type": "string"},
        "input_schema": SIGNAL_SCHEMA,
        "output_schema": SIGNAL_SCHEMA,
        "timeout": {"type": "number", "minimum": 0},       # 秒（单次尝试）
        "retry": {"type": "integer", "minimum": 0},        # 重试次数（§28 `RETRY 1`）
        "model_tier": {"type": "string", "enum": list(MODEL_TIERS)},
        "allowed_tools": {"type": "array", "items": {"type": "string"}},
        "validation": {"type": "array", "items": {"type": "string"}},
        "status": {"type": "string", "enum": list(NODE_STATUSES)},
        "path": {"type": "string", "enum": list(QA_PATHS) + [""]},
        "parallel_group": {"type": "string"},
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "barrier": {"type": "boolean"},
        "implemented": {"type": "boolean"},
        "deferred_to": {"type": "string"},
    },
    "required": ["node_id"],
    "additionalProperties": True,
}
"""执行图节点（Phase 01 建骨架、Phase 05 补全 §2.2 的 Node 契约字段）。
`required` 仍只有 `node_id`：Phase 01 的调用方与用例一字不改；
Phase 05 建图时产出的节点会带上全部契约字段，并由 `validate("execution_node")` 逐字段校验。"""

# ── Phase 05：执行图 / 规划产物（P05-02…P05-05）────────────────────────────
SUB_QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "sub_question_id": {"type": "string"},
        "plan_node_kind": {"type": "string", "enum": ["sub_question"]},
        "question": {"type": "string"},
        "purpose": {"type": "string"},
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "carry": {"type": "array", "items": {"type": "string"}},
        "hop_index": {"type": "integer", "minimum": 0},
        "parallel_group": {"type": "string"},
        "required_evidence_types": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["sub_question_id", "question"],
    "additionalProperties": True,
}
"""子问题（§7）：由 `qa_query_decompose.decompose()` 的 hop 一对一映射而来
（`sub_question_id = "sq:" + hop.id`），**不新建分解器**。"""

PLAN_CLAIM_SCHEMA = {
    "type": "object",
    "properties": {
        "claim_id": {"type": "string"},
        "plan_node_kind": {"type": "string", "enum": ["claim"]},
        "statement": {"type": "string"},
        "role": {"type": "string", "enum": list(PLAN_CLAIM_ROLES)},
        "sub_question_id": {"type": "string"},
        "required_evidence_types": {"type": "array", "items": {"type": "string"}},
        "parallel_group": {"type": "string"},
        "plan_only": {"type": "boolean"},     # True = 本轮不检索，交给 Phase 06/07/13
    },
    "required": ["claim_id", "statement", "role"],
    "additionalProperties": True,
}
"""规划期 Claim（§7 的"问题拆成 SubQuestion/Claim/Evidence Requirement"）。
**注意**：这里只是"要证实/证伪什么"的声明，不是结论，也不是证据；
判定仍必须由 Phase 03 的 `qa_verifier` 基于真实证据给出（MASTER_RULES 第 11 条）。"""

EVIDENCE_REQUIREMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "requirement_id": {"type": "string"},
        "plan_node_kind": {"type": "string", "enum": ["evidence_requirement"]},
        "sub_question_id": {"type": "string"},
        # 取值复用 qa_planner._retrieval_strategy 的 source 口径（官方原文/官方解读/
        # 专业材料/跳链/裁决），不另造证据类型学。
        "evidence_type": {"type": "string"},
        "rationale": {"type": "string"},
        "satisfied_by": {"type": "string"},   # 哪个节点负责满足它（Phase 05 只声明）
    },
    "required": ["requirement_id", "evidence_type"],
    "additionalProperties": True,
}
"""证据要求（§7 "Evidence Requirement"）。本轮只声明"需要哪类证据、由哪个节点满足"，
真实满足度判定属 Phase 06/07。"""

QUERY_INTERPRETATION_SCHEMA = {
    "type": "object",
    "properties": {
        "interpreter_version": {"type": "string"},
        "backend": {"type": "string"},
        "backend_source": {"type": "string"},
        "fallback": {"type": "object"},
        "question": {"type": "string"},
        "intent": {"type": "string", "enum": list(QUERY_INTENTS)},
        "entities": {"type": "array", "items": {"type": "string"}},
        "time_scope": {"type": "object"},
        "constraints": {"type": "array", "items": {"type": "object"}},
        "required_claims": {"type": "array", "items": PLAN_CLAIM_SCHEMA},
        "freshness_required": {"type": "boolean"},
        "answer_type": {"type": "string", "enum": list(QA_ANSWER_TYPES)},
        "complexity": {"type": "string", "enum": list(QUERY_COMPLEXITIES)},
        "category": {"type": "object"},
        "relationship": {"type": "string"},
        "question_count": {"type": "integer", "minimum": 0},
        "needs_retrieval": {"type": "boolean"},
        "high_risk_policy": {"type": "boolean"},
        "axes": {"type": "array", "items": {"type": "string"}},
        "reuse": {"type": "object"},
    },
    "required": ["interpreter_version", "question", "intent", "complexity", "answer_type",
                 "required_claims"],
    "additionalProperties": True,
}
"""Query Interpreter 输出（§6 的字段 + 可追溯的复用说明）。
`backend_source` = `rules` 或 `registered:<name>`；`fallback.used=True` 表示注册的后端
不可用/返回非法，已**保守回落**到规则后端（绝不会因为后端坏掉就不给规划）。"""

EXECUTION_GRAPH_SCHEMA = {    "type": "object",
    "properties": {
        "contract_version": {"type": "string"},
        "graph_version": {"type": "string"},
        "run_id": {"type": "string"},
        "question": {"type": "string"},
        "path": {"type": "string", "enum": list(QA_PATHS)},
        "path_source": {"type": "string"},
        "intent": {"type": "string", "enum": list(QUERY_INTENTS)},
        "complexity": {"type": "string", "enum": list(QUERY_COMPLEXITIES)},
        "nodes": {"type": "array", "items": EXECUTION_NODE_SCHEMA},
        "edges": {"type": "array", "items": {"type": "object"}},
        "parallel_groups": {"type": "array", "items": {"type": "object"}},
        "budget": {"type": "object"},
        "stop_reason": {"type": "string", "enum": list(QA_STOP_REASONS) + [""]},
        "stop_reasons": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["contract_version", "path", "nodes", "edges", "parallel_groups", "budget"],
    "additionalProperties": True,
}
"""执行图（§3.1）。`stop_reason` 取既有五值枚举（Phase 01 冻结）。
**规划期**（建图那一刻）只可能产出 {ANSWERABLE, BUDGET_EXHAUSTED, MAX_DEPTH}——NO_GAIN 与
UNRESOLVABLE_CONTRADICTION 是**运行期**结论（要等检索/核验/裁决跑完才知道），
由 Phase 07 的缺口循环（`qa_gap_analyzer`）在 `_run_multi_hop` 与 conflict_review 里给出，
落在 `qa_pipeline._run_multi_hop` 的 `gap_loop` 回执与执行图 `gap_loop` 节点的账本记录里。"""


# ── 证据层 schema（Phase 02 · P02-01）────────────────────────────────────────
# 口径与上面的图 schema 一致："必需字段严格 + 不禁止额外字段"。证据对象本身是
# `metadata.evidence_layer` 的载荷（见 qa_evidence.annotate_evidence），不进
# `qa_contracts.EVIDENCE_SCHEMA`——那个契约是 `additionalProperties: False` 的历史冻结
# 指纹（P00-02），放宽或收紧都造成过生产回归，**本阶段一个字都不动它**。

EVIDENCE_SPAN_SCHEMA = {
    "type": "object",
    "properties": {
        "start": {"type": "integer", "minimum": 0},
        "end": {"type": "integer", "minimum": 0},
        "quote": {"type": "string"},
        "source": {"type": "string", "enum": list(EVIDENCE_SPAN_SOURCES)},
        "chars": {"type": "integer", "minimum": 0},
    },
    "required": ["start", "end", "quote"],
    "additionalProperties": True,
}
"""最小证据 span：`content_excerpt[start:end] == quote`（偏移相对证据自身的 content_excerpt）。"""

EVIDENCE_SOURCE_SCHEMA = {
    "type": "object",
    "properties": {
        "source_id": {"type": "string"},          # 稳定来源标识（article:<id> / doc:.. / edge:.. / web:..）
        "source_type": {"type": "string"},
        "source_url": {"type": "string"},
        "article_id": {"type": ["integer", "null"]},
        "document_id": {"type": "string"},
        "chunk_id": {"type": "string"},
        "authority_level": {"type": ["integer", "null"]},
        "published_at": {"type": "string"},
    },
    "required": ["source_id", "source_type"],
    "additionalProperties": True,
}
"""Source：证据可回溯到的来源（Evidence → Source / Chunk / Span 链条的第一环）。"""

EVIDENCE_ENTITY_SCHEMA = {
    "type": "object",
    "properties": {
        "entity_key": {"type": "string"},         # 与 kg_builder._node_key 同口径
        "label": {"type": "string"},
        "entity_type": {"type": "string", "enum": list(KG_NODE_TYPES) + ["keyword"]},
        # 值的出处（不做新 NER，只把既有 metadata / 图谱边里已有的实体显式化）
        "origin": {"type": "string"},
    },
    "required": ["entity_key"],
    "additionalProperties": True,
}

EVIDENCE_RELATION_SCHEMA = {
    "type": "object",
    "properties": {
        "src_key": {"type": "string"},
        "dst_key": {"type": "string"},
        "relation_kind": {"type": "string", "enum": list(KG_RELATION_KINDS)},
        "attr_key": {"type": "string"},
        "attr_value": {"type": "string"},
        "valid_from": {"type": "string"},
        "valid_to": {"type": "string"},
        "evidence_ref": {"type": "string"},
    },
    "required": ["src_key", "dst_key", "relation_kind"],
    "additionalProperties": True,
}
"""证据里表达的关系。目前只有图谱边证据能给出（事件边/属性边），文章证据一律为空——
宁可空着，也不把"共现"编成"因果"（01_V2_ARCHITECTURE §10 Verifier 第 8 条）。"""

# ── 核验层 schema（Phase 03 · P03-01…P03-04）────────────────────────────────
# 核验结论落在 `metadata.evidence_layer.verification`（可选字段），**不改**证据对象既有的
# `status`/`relationship` 语义（Phase 02 的回归用例钉着它们），也不动 `qa_contracts` 的
# 七个冻结 schema。结论取值域直接复用 `EVIDENCE_STATUSES`，不新造枚举。
EVIDENCE_VERIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "verifier_version": {"type": "string"},     # 规则版本（换规则=换版本，缓存自然失效）
        "config_hash": {"type": "string"},          # 权重/阈值指纹（同配置稳定）
        "verdict": {"type": "string", "enum": list(EVIDENCE_STATUSES)},
        "verified": {"type": "boolean"},            # = (verdict == SUPPORTED)，唯一可当"已验证"的取值
        "score": {"type": "number", "minimum": 0, "maximum": 1},
        "dimensions": {"type": "object"},           # relevance/entailment/source_quality/freshness/...
        "reasons": {"type": "array", "items": {"type": "string"}},   # 机器可读原因码
        "reason_text": {"type": "string"},          # 中文解释（可解释性）
        "nli": {"type": "object"},                  # 蕴含判定后端与覆盖度
        "checks": {"type": "array", "items": {"type": "object"}},
        "text_source": {"type": "string", "enum": ["span", "excerpt"]},
        "cache": {"type": "string", "enum": ["hit", "miss", "off"]},
    },
    "required": ["verifier_version", "verdict", "score"],
    "additionalProperties": True,
}
"""证据核验结论（Phase 03）。`verdict` 与 `status` 并存：前者是 verifier 的判定，
后者是 Phase 02 交付的"relationship 规范化映射"，两者语义不同、不许互相顶替。"""

EVIDENCE_OBJECT_SCHEMA = {
    "type": "object",
    "properties": {
        "layer_version": {"type": "string"},
        "evidence_ref": {"type": "string"},
        "status": {"type": "string", "enum": list(EVIDENCE_STATUSES)},
        "relationship": {"type": "string"},               # 既有字段的镜像（不改既有语义）
        "span": EVIDENCE_SPAN_SCHEMA,
        "source": EVIDENCE_SOURCE_SCHEMA,
        "entities": {"type": "array", "items": EVIDENCE_ENTITY_SCHEMA},
        "relations": {"type": "array", "items": EVIDENCE_RELATION_SCHEMA},
        "fingerprint": {"type": "string"},                # span 级身份（P02-04）
        "source_fingerprint": {"type": "string"},         # 来源级身份（同一篇文章/同一 chunk）
        "provenance": {"type": "object"},                 # P02-02
        # Phase 03（P03-01…P03-04）：核验结论。**可选**——不做核验的调用方（或
        # QA_VERIFIER_ENABLED=0 回滚态）产出的证据对象照样能过校验。
        "verification": EVIDENCE_VERIFICATION_SCHEMA,
    },
    "required": ["evidence_ref", "status", "span", "fingerprint", "source"],
    "additionalProperties": True,
}
"""Evidence Object（Phase 02）：`metadata.evidence_layer` 的载荷契约。"""

# ── 检索舰队（Phase 04 · P04-01…P04-06）─────────────────────────────────────
# 通用包 01_V2_ARCHITECTURE §8 把 Retrieval Fleet 拆成若干 Hunter；本仓库既有
# `ArticleRetriever` 已内含 keyword / semantic / graph / policy_exact / page_context / web
# 多通道，Phase 04 只把**通道**提成统一 Hunter 接口 + 并行风扇，不另写一套检索。
#
# `QA_HUNTER_IDS` 是**新命名空间**（Hunter 身份），与 `QA_RETRIEVAL_ROUTES`（通道）分离：
#   · 每个 Hunter 结果同时带 `hunter_id`（谁跑的）与 `route`（既有通道枚举里的值），
#     SearchTrace 的 route 字段继续只吃既有 7 个取值 → **不动 P01-01 的通道枚举**；
#   · `structured` 在本仓库的落地形态是"结构化业务表查询"（政策登记表 + 文章元数据列），
#     其 route 记 `policy_exact`（该值的既有语义就是"按结构化元数据精确命中"）。
HUNTER_CONTRACT_VERSION = "qa-hunter-v1"
"""Hunter 结果契约版本；与 `GRAPH_CONTRACT_VERSION`/`EVIDENCE_LAYER_VERSION` 是三件事。"""

HUNTER_BM25 = "bm25"
HUNTER_SEMANTIC = "semantic"
HUNTER_GRAPH = "graph"
HUNTER_STRUCTURED = "structured"
HUNTER_QUERY_EXPANSION = "query_expansion"
QA_HUNTER_IDS: Tuple[str, ...] = (
    HUNTER_BM25, HUNTER_SEMANTIC, HUNTER_GRAPH, HUNTER_STRUCTURED, HUNTER_QUERY_EXPANSION,
)
"""Hunter 身份枚举（§8.1 BM25 / §8.2 Semantic / §8.3 Graph / §8.5 Query Expansion /
§8.6 Structured；§8.4 Metadata 的口径并入 `structured`——见 qa_hunters 的模块说明）。"""

QA_HUNTER_ROUTE_BY_ID = {
    HUNTER_BM25: QA_ROUTE_KEYWORD,
    HUNTER_SEMANTIC: QA_ROUTE_SEMANTIC,
    HUNTER_GRAPH: QA_ROUTE_GRAPH,
    HUNTER_STRUCTURED: QA_ROUTE_POLICY_EXACT,
    HUNTER_QUERY_EXPANSION: "",
}
"""Hunter → SearchTrace 通道（沿用既有 7 个取值；Query Expansion 不产证据、不占通道）。"""

HUNTER_STATUSES: Tuple[str, ...] = (
    "ok", "empty", "degraded", "timeout", "error", "skipped",
)
"""单次 Hunter 调用的结局：成功有证据 / 成功但空 / 降级（含无向量、无种子这类可解释回退）/
超时 / 抛错 / 未执行（预算或依赖不允许）。"""

HUNTER_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "hunter_id": {"type": "string", "enum": list(QA_HUNTER_IDS)},
        "contract_version": {"type": "string"},
        "route": {"type": "string", "enum": list(QA_RETRIEVAL_ROUTES) + [""]},
        "status": {"type": "string", "enum": list(HUNTER_STATUSES)},
        "ok": {"type": "boolean"},
        "degraded": {"type": "boolean"},
        "timed_out": {"type": "boolean"},
        "reason_code": {"type": "string"},
        "failure_policy": {"type": "string", "enum": list(QA_FAILURE_POLICIES)},
        "attempts": {"type": "integer", "minimum": 0},
        "latency_ms": {"type": "integer", "minimum": 0},
        "evidence": {"type": "array", "items": {"type": "object"}},
        "queries": {"type": "array", "items": {"type": "string"}},
        "terms": {"type": "array", "items": {"type": "object"}},
        "stats": {"type": "object"},
        "error": {"type": "string"},
        "fallback_from": {"type": "string"},
    },
    "required": ["hunter_id", "status", "attempts", "latency_ms", "evidence"],
    "additionalProperties": True,
}
"""单 Hunter 结果契约。`status` 归一到 `HUNTER_STATUSES`；`evidence` 一律存在（可为空数组）；
Query Expansion 的产出走 `queries`/`terms`（**不允许**产证据——§8.5 明确它只负责生成词）。"""

HUNTER_FLEET_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "contract_version": {"type": "string"},
        "hunters": {"type": "array", "items": HUNTER_RESULT_SCHEMA},
        "evidence": {"type": "array", "items": {"type": "object"}},
        "partial": {"type": "boolean"},
        "stop_reason": {"type": "string", "enum": list(QA_STOP_REASONS) + [""]},
        "stats": {"type": "object"},
    },
    "required": ["contract_version", "hunters", "evidence", "stats"],
    "additionalProperties": True,
}
"""舰队扇入结果：`partial=true` 表示"总预算内只等到部分 Hunter"（部分结果回退，不阻塞）。"""


# ── 证据图与矛盾裁决（Phase 06 · P06-01…P06-04）────────────────────────────
# 通用包 01_V2_ARCHITECTURE §3.2（Evidence Graph 节点/推荐边）、§15（Contradiction Agent：
# "不要简单多数投票；比较来源级别/发布时间/版本/样本人群/实体一致性/定义一致性/证据独立性/
# 是否存在更新版本；无法消解则保留 UNRESOLVED_CONTRADICTION"）、§9（只有验证过的证据进图）。
#
# **为什么不扩 `CLAIM_EVIDENCE_RELATIONSHIPS`**：那 4 个取值（supports/contradicts/
# qualifies/context）是**证据条目**上 `relationship` 字段的镜像，而 `relationship` 的枚举写在
# P00-02 冻结的 `qa_contracts.EVIDENCE_SCHEMA` 里（additionalProperties=False、指纹
# 370301331c02c738，不许改）。若把 `depends`/`refutes` 塞进 CLAIM_EVIDENCE_RELATIONSHIPS：
#   · `CLAIM_EVIDENCE_EDGE_SCHEMA.relationship` 会开始接受冻结证据契约**永远产不出**的取值
#     ——两个契约互相打架（契约漂移）；
#   · 既有 `qa_reasoning` 边与库表 `qa_claim_evidence.relationship` 也会被误读。
# 所以 Phase 06 用**自己的**图级枚举 `EVIDENCE_GRAPH_RELATIONSHIPS`（架构 §3.2 推荐边口径，
# 大写），并给出与冻结证据层取值的**全量映射** `EVIDENCE_GRAPH_RELATION_BY_STATUS`
# （P06-02 的口径统一规则：核验 verdict 是唯一真源，图里的关系由它派生，二者不许打架）。
EVIDENCE_GRAPH_VERSION = "qa-evidence-graph-v1"
"""证据图版本；与 GRAPH/EVIDENCE/HUNTER/EXECUTION 四个版本是不同的事（知识状态图 vs 运行图）。"""

EVIDENCE_GRAPH_NODE_TYPES: Tuple[str, ...] = ("claim", "evidence", "source", "contradiction")
"""Phase 06 真正**物化**的节点类型。§3.2 还推荐 Question/SubQuestion/Entity/Gap——
它们分别归 Phase 05（计划）、Phase 02（实体）、Phase 07（缺口），本阶段不抢后续阶段的活。"""

EVIDENCE_GRAPH_NODE_PREFIX = {
    "claim": "claim:", "evidence": "evidence:", "source": "source:",
    "contradiction": "contradiction:",
}
"""节点 id 命名空间（`node_id = 前缀 + 原始 id`），避免 claim/evidence 同名撞车。"""

EG_RELATION_SUPPORTS = "SUPPORTS"
EG_RELATION_REFUTES = "REFUTES"
EG_RELATION_DEPENDS = "DEPENDS"
EG_RELATION_CONTRADICTS = "CONTRADICTS"
EG_RELATION_MENTIONS = "MENTIONS"
EVIDENCE_GRAPH_RELATIONSHIPS: Tuple[str, ...] = (
    EG_RELATION_SUPPORTS, EG_RELATION_REFUTES, EG_RELATION_DEPENDS,
    EG_RELATION_CONTRADICTS, EG_RELATION_MENTIONS,
)
"""证据图边关系（P06-02 要的四个 + `MENTIONS`）。

`MENTIONS` 是**必须**存在的第五个值，不是顺手加的：冻结的证据契约里有 `context`（背景材料）
与"未判定"（`relationship` 为空）两种证据，它们既不是支持也不是反驳；没有 `MENTIONS`
就只能把它们错记成 `SUPPORTS`（claim coverage 会虚高）或悄悄丢边（图与证据包对不上）。
它同时是架构 §3.2 的推荐边之一。"""

EVIDENCE_GRAPH_EDGE_KINDS: Tuple[str, ...] = (
    "claim-evidence", "claim-claim", "claim-contradiction", "evidence-contradiction",
)
"""边的端点类型组合（P06-01 的 typed contract）：claim-evidence = 证据对结论；
claim-claim = 结论之间的推导/冲突；后两种 = 矛盾节点的挂载边。"""

EVIDENCE_GRAPH_RELATIONS_BY_KIND = {
    "claim-evidence": (EG_RELATION_SUPPORTS, EG_RELATION_REFUTES, EG_RELATION_MENTIONS),
    "claim-claim": (EG_RELATION_DEPENDS, EG_RELATION_CONTRADICTS),
    "claim-contradiction": (EG_RELATION_CONTRADICTS,),
    "evidence-contradiction": (EG_RELATION_CONTRADICTS,),
}
"""关系 → 合法端点的**机器可校验**约束（防止 SUPPORTS 挂到 claim-claim 上这类漂移）。"""

EVIDENCE_GRAPH_RELATION_BY_STATUS = {
    EVIDENCE_STATUS_SUPPORTED: EG_RELATION_SUPPORTS,
    EVIDENCE_STATUS_REFUTED: EG_RELATION_REFUTES,
    # QUALIFIED = "带保留地支持"：关系仍是 SUPPORTS，但边标 qualified=true 且强度降档。
    # 判 REFUTES 会直接篡改核验结论；另造 QUALIFIES 值则架构 §3.2 里没有这条推荐边。
    EVIDENCE_STATUS_QUALIFIED: EG_RELATION_SUPPORTS,
    # CONTEXT / UNVERIFIED 都不构成支持或反驳 → MENTIONS（由 edge.status 区分两者）。
    EVIDENCE_STATUS_CONTEXT: EG_RELATION_MENTIONS,
    EVIDENCE_STATUS_UNVERIFIED: EG_RELATION_MENTIONS,
}
"""核验 `verdict` / 证据层 `status` → 图级关系（P06-02 的唯一映射表，全量覆盖五个状态）。

**这就是"图里的关系不许与核验层 verdict 打架"的落地方式**：关系不是另算的，而是从 Phase 03
的 `verdict`（claim 级核验 `verification.pairs[].verdict`，或证据包上的
`metadata.evidence_layer.verification.verdict`）派生；只有两者都缺失时才回落到 Phase 02 的
`relationship → status` 映射（`EVIDENCE_STATUS_BY_RELATIONSHIP`）。"""

EVIDENCE_GRAPH_QUALIFIED_STATUSES: Tuple[str, ...] = (EVIDENCE_STATUS_QUALIFIED,)
"""映射到 SUPPORTS 但需打 `qualified=true`、强度乘折扣的状态（唯一一个非一一映射）。"""

EVIDENCE_GRAPH_CONTRADICTION_KINDS: Tuple[str, ...] = ("evidence_conflict", "claim_conflict")
"""矛盾的两族：同一结论同时有支持与反驳证据（§15 的 `E1 SUPPORTS C / E2 REFUTES C`）；
以及两条结论互相冲突（`qa_reasoning` 已经检出、并写进 `graph["conflicts"]` 的那族）。"""

CONTRADICTION_RESOLVER_VERSION = "qa-contradiction-resolver-v1"
"""裁决规则版本：换规则=换版本；每条裁决结果都带这个版本号（可复算）。"""

RESOLUTION_SCOPE_DIFFERENCE = "SCOPE_DIFFERENCE"
RESOLUTION_NEWER_VERSION = "NEWER_VERSION_PRECEDES"
RESOLUTION_AUTHORITY = "AUTHORITY_ADVANTAGE"
RESOLUTION_QUALITY = "EVIDENCE_QUALITY_ADVANTAGE"
RESOLUTION_INDEPENDENCE = "INDEPENDENCE_ADVANTAGE"
RESOLUTION_STRENGTH = "RELATION_STRENGTH_ADVANTAGE"
RESOLUTION_METHOD_UNDECIDED = "METHOD_DIFFERENCE_UNDECIDED"
RESOLUTION_OPINION_UNDECIDED = "OPINION_ONLY_UNDECIDED"
RESOLUTION_NO_DECISIVE_RULE = "NO_DECISIVE_RULE"
CONTRADICTION_RESOLUTION_CODES: Tuple[str, ...] = (
    RESOLUTION_SCOPE_DIFFERENCE, RESOLUTION_NEWER_VERSION, RESOLUTION_AUTHORITY,
    RESOLUTION_QUALITY, RESOLUTION_INDEPENDENCE, RESOLUTION_STRENGTH,
    RESOLUTION_METHOD_UNDECIDED, RESOLUTION_OPINION_UNDECIDED, RESOLUTION_NO_DECISIVE_RULE,
)
"""裁决理由码（§15 的八项比较逐条落成规则）：

· `SCOPE_DIFFERENCE`：适用范围不同 → 两份说法都成立（resolved，各自限定范围）；
· `NEWER_VERSION_PRECEDES`：生效时间/发布时间不同 → 新版优先，旧版留在时间线（resolved）；
· `AUTHORITY_ADVANTAGE`：来源权威度（`qa_reasoning.authority_score` 口径）领先 ≥ 阈值；
· `EVIDENCE_QUALITY_ADVANTAGE`：核验证据分（§11 EvidenceScore）质量领先 ≥ 比值阈值；
· `INDEPENDENCE_ADVANTAGE`：独立来源数领先 ≥ 阈值（§15 的"证据独立性"）；
· `RELATION_STRENGTH_ADVANTAGE`：支持/反驳强度差 ≥ 比值阈值（兜底的强弱比较）；
· `METHOD_DIFFERENCE_UNDECIDED`：数字/口径/期限不一致 → 不许取平均（unresolved）；
· `OPINION_ONLY_UNDECIDED`：只有解读角度差异 → unresolved；
· `NO_DECISIVE_RULE`：条条都不满足 → unresolved，回答里必须呈现不确定性。
前六条 = 能给出"以谁为准"的理由码，后三条 = 保留不确定性的理由码。"""

CONTRADICTION_RESOLVED_CODES: Tuple[str, ...] = (
    RESOLUTION_SCOPE_DIFFERENCE, RESOLUTION_NEWER_VERSION, RESOLUTION_AUTHORITY,
    RESOLUTION_QUALITY, RESOLUTION_INDEPENDENCE, RESOLUTION_STRENGTH,
)
"""resolution=resolved 的合法理由码。"""

CONTRADICTION_UNRESOLVED_CODES: Tuple[str, ...] = (
    RESOLUTION_METHOD_UNDECIDED, RESOLUTION_OPINION_UNDECIDED, RESOLUTION_NO_DECISIVE_RULE,
)
"""resolution=unresolved 的合法理由码（§15：无法消解则保留，回答明确呈现不确定性）。"""

EVIDENCE_GRAPH_EDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "edge_id": {"type": "string"},
        "kind": {"type": "string", "enum": list(EVIDENCE_GRAPH_EDGE_KINDS)},
        "src": {"type": "string"},
        "dst": {"type": "string"},
        "graph_relation": {"type": "string", "enum": list(EVIDENCE_GRAPH_RELATIONSHIPS)},
        "status": {"type": "string", "enum": list(EVIDENCE_STATUSES) + [""]},
        "strength": {"type": "number", "minimum": 0, "maximum": 1},
        "claim_id": {"type": "string"},
        "evidence_ref": {"type": "string"},
        "metadata": {"type": "object"},
    },
    "required": ["edge_id", "kind", "src", "dst", "graph_relation"],
    "additionalProperties": True,
}
"""证据图边契约（P06-01/P06-02）：`graph_relation` 是图级关系，`status` 是它来自的核验状态。"""

EVIDENCE_GRAPH_NODE_SCHEMA = {
    "type": "object",
    "properties": {
        "node_id": {"type": "string"},
        "node_type": {"type": "string", "enum": list(EVIDENCE_GRAPH_NODE_TYPES)},
        "label": {"type": "string"},
        "ref": {"type": "string"},
        "metadata": {"type": "object"},
    },
    "required": ["node_id", "node_type"],
    "additionalProperties": True,
}
"""证据图节点契约（P06-01）。"""

CLAIM_COVERAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "coverage_version": {"type": "string"},
        "coverage_definition": {"type": "string"},
        "total_claims": {"type": "integer", "minimum": 0},
        "supported_claims": {"type": "integer", "minimum": 0},
        "qualified_claims": {"type": "integer", "minimum": 0},
        "refuted_claims": {"type": "integer", "minimum": 0},
        "claims_with_evidence": {"type": "integer", "minimum": 0},
        "claims_without_evidence": {"type": "integer", "minimum": 0},
        "claim_coverage": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "weighted_claim_coverage": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "evidence_coverage": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "refuted_claim_rate": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "support_count_histogram": {"type": "object"},
        "coverage_buckets": {"type": "object"},
        "by_claim": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["coverage_version", "total_claims", "supported_claims"],
    "additionalProperties": True,
}
"""claim coverage 契约（P06-03）：口径逐字写在 `coverage_definition` 里，可复算。

`claim_coverage` 等比率字段**不在 required 里**：空图（0 条 claim）时分母为 0，
口径上"不适用"——用 `null` 表达，绝不用 `0.0` 冒充"覆盖率为零"。"""

CONTRADICTION_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "contradiction_id": {"type": "string"},
        "kind": {"type": "string", "enum": list(EVIDENCE_GRAPH_CONTRADICTION_KINDS)},
        "conflict_type": {"type": "string"},
        "claim_ids": {"type": "array", "items": {"type": "string"}},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "resolution": {"type": "string", "enum": ["resolved", "unresolved"]},
        "reason_code": {"type": "string", "enum": list(CONTRADICTION_RESOLUTION_CODES)},
        "decider": {"type": "string"},
        "rule_version": {"type": "string"},
        "winner": {"type": "object"},
        "inputs": {"type": "object"},
        "rationale": {"type": "string"},
    },
    "required": ["contradiction_id", "kind", "resolution", "reason_code", "rule_version"],
    "additionalProperties": True,
}
"""矛盾裁决结果契约（P06-04）。`resolution` 取值域与冻结 CONFLICT_SCHEMA 完全一致
（resolved/unresolved）——裁决细节（winner/inputs/reason_code）走这个**新契约**，
不往冻结的 CONFLICT_SCHEMA 里塞字段。"""

EVIDENCE_GRAPH_SCHEMA = {
    "type": "object",
    "properties": {
        "graph_version": {"type": "string"},
        "run_id": {"type": "string"},
        "nodes": {"type": "array", "items": EVIDENCE_GRAPH_NODE_SCHEMA},
        "edges": {"type": "array", "items": EVIDENCE_GRAPH_EDGE_SCHEMA},
        "claims": {"type": "array", "items": {"type": "object"}},
        "coverage": CLAIM_COVERAGE_SCHEMA,
        "contradictions": {"type": "array", "items": CONTRADICTION_DECISION_SCHEMA},
        "stats": {"type": "object"},
    },
    "required": ["graph_version", "nodes", "edges", "claims", "coverage", "contradictions"],
    "additionalProperties": True,
}
"""证据图整体契约（P06-01 的仓储/API 返回体）。"""


# ── 缺口分析与动态多跳（Phase 07 · P07-01…P07-06）──────────────────────────
# 通用包 01_V2_ARCHITECTURE 依据：
#   · §12 Gap Analyzer："每一轮 fan-in 后不问'还要不要再检索一次'，而问'为了可靠回答原问题，
#     哪些必要 Claim 仍缺少什么类型的证据？'"——gap 形状（gap_id/claim_id/missing/priority/
#     suggested_queries/suggested_routes/reason）与**十种** Gap 类型逐字取自这一节；
#   · §13 Dynamic Next-hop Planner："下一跳不是 hop+1，而是 Gap → Best Retrieval Action"
#     （G1→BM25 Hunter / G4→Two independent Hunters）；完整循环 PLAN→FAN OUT→VERIFY→MERGE→
#     GAP ANALYSIS→（gaps→GENERATE NEXT HOPS→FAN OUT | sufficient→ANSWER）；
#   · §14 收敛与停止：连续两轮 `new_verified_claims == 0 AND resolved_high_priority_gaps == 0`
#     → STOP_NO_GAIN；五个停止原因（Phase 01 已冻结在 `QA_STOP_REASONS`，本阶段不再新增取值）；
#   · §21 Safety-Critical Gap → Priority Override；
#   · §24 Query Fingerprint = normalized_query + constraints + corpus_version + retrieval_config；
#   · §23 Search Trace：每跳必须能回答"为什么搜/为什么走这个 route/解决了哪个 Gap/为什么停"。
#
# 三条口径（写死在这里，避免后续阶段各算一套）：
#   1. `missing` 取 `QA_GAP_TYPES` 的十个值（**逐字**取自 §12，不新造、不改名）；
#   2. `suggested_routes` 只吃 `QA_RETRIEVAL_ROUTES` 的既有 7 个通道值 —— §12 示例里的
#      `bm25`/`graph` 在本仓库的对应物是 Hunter 身份（`QA_HUNTER_IDS`）与检索通道
#      （`QA_RETRIEVAL_ROUTES`）两层，Phase 04（D-017）已经把它们分开；缺口建议只写通道，
#      否则 SearchTrace 的 route 会开始接受冻结枚举外的取值（跨阶段契约漂移）；
#   3. 停止原因**不新增取值**：NO_GAIN / UNRESOLVABLE_CONTRADICTION 就是 Phase 01 冻结五值里
#      那两个"待阶段 07 补齐"的值（本阶段补齐，见 qa_gap_analyzer）。
GAP_ANALYZER_VERSION = "qa-gap-analyzer-v1"
"""缺口分析版本（换规则=换版本）：gap 分类/优先级/证据要求都可复算到这个版本号。"""

NEXT_HOP_PLANNER_VERSION = "qa-next-hop-planner-v1"
"""下一跳规划器版本（§13）。默认实现是规则；注册后端走 `qa_gap_analyzer.register_next_hop_planner`。"""

QA_GAP_NO_EVIDENCE = "NO_EVIDENCE"
QA_GAP_LOW_RELEVANCE = "LOW_RELEVANCE"
QA_GAP_LOW_AUTHORITY = "LOW_AUTHORITY"
QA_GAP_SINGLE_SOURCE = "SINGLE_SOURCE"
QA_GAP_MISSING_ENTITY_LINK = "MISSING_ENTITY_LINK"
QA_GAP_MISSING_TIME_LINK = "MISSING_TIME_LINK"
QA_GAP_CONTRADICTION = "CONTRADICTION"
QA_GAP_AMBIGUOUS_ENTITY = "AMBIGUOUS_ENTITY"
QA_GAP_MISSING_CAUSAL_BRIDGE = "MISSING_CAUSAL_BRIDGE"
QA_GAP_MISSING_COUNTEREVIDENCE = "MISSING_COUNTEREVIDENCE"
QA_GAP_TYPES: Tuple[str, ...] = (
    QA_GAP_NO_EVIDENCE, QA_GAP_LOW_RELEVANCE, QA_GAP_LOW_AUTHORITY, QA_GAP_SINGLE_SOURCE,
    QA_GAP_MISSING_ENTITY_LINK, QA_GAP_MISSING_TIME_LINK, QA_GAP_CONTRADICTION,
    QA_GAP_AMBIGUOUS_ENTITY, QA_GAP_MISSING_CAUSAL_BRIDGE, QA_GAP_MISSING_COUNTEREVIDENCE,
)
"""Gap 类型十值（§12 逐字）。含义与判定规则见 `qa_gap_analyzer.detect_gaps` 的文档字符串。"""

GAP_PRIORITY_BANDS: Tuple[str, ...] = ("critical", "high", "medium", "low")
"""优先级分档：由 `priority` 单一数字派生（阈值可配置），保证"同一个数字永远同一档"。"""

GAP_STATUSES: Tuple[str, ...] = ("open", "resolved", "unactionable")
"""缺口生命周期：`open` = 还没被证据满足；`resolved` = 后续跳已满足其证据要求；
`unactionable` = 规则判定"再检索也拿不到"（例如已见且被拒的来源），必须显式记账而不是消失。"""

GAP_PRIORITY_WEIGHTS = {"severity": 0.55, "claim_importance": 0.25, "evidence_deficit": 0.20}
"""优先级三个分量的权重（和为 1）。§21 的安全关键覆盖是**覆盖**（override）而非加权项。"""

SAFETY_OVERRIDE_PRIORITY = 0.95
"""§21 Priority Override：`safety_critical=true` 的缺口优先级**抬高到至少**这个值。"""

NEXT_HOP_PLANNERS: Tuple[str, ...] = ("rule",)
"""内置下一跳规划器身份。注册后端用 `register_next_hop_planner(name, fn)`（名字不在这里）。"""

GAP_SCHEMA = {
    "type": "object",
    "properties": {
        "gap_id": {"type": "string"},
        "claim_id": {"type": "string"},
        "missing": {"type": "string", "enum": list(QA_GAP_TYPES)},
        "priority": {"type": "number", "minimum": 0, "maximum": 1},
        "band": {"type": "string", "enum": list(GAP_PRIORITY_BANDS)},
        "status": {"type": "string", "enum": list(GAP_STATUSES)},
        "suggested_queries": {"type": "array", "items": {"type": "string"}},
        "suggested_routes": {"type": "array", "items": {"type": "string",
                                                        "enum": list(QA_RETRIEVAL_ROUTES)}},
        "reason": {"type": "string"},
        # P07-02：这条缺口到底缺哪类证据、缺到什么程度（§7 的 Evidence Requirement 口径）
        "evidence_requirement": EVIDENCE_REQUIREMENT_SCHEMA,
        # 可复算的优先级分解（每个分量都写出来，便于"为什么它排第一"）
        "priority_factors": {"type": "object"},
        "safety_critical": {"type": "boolean"},
        "priority_override": {"type": "boolean"},
        "origin": {"type": "string"},
    },
    "required": ["gap_id", "missing", "priority", "suggested_routes", "reason"],
    "additionalProperties": True,
}
"""Evidence Gap 契约（§12 的字段 + P07-02 的证据要求 + 可复算的优先级分解）。

`claim_id` 不在 required 里：`MISSING_ENTITY_LINK` 这类缺口可能挂在计划 claim 上
（`plan_only=true`，是"要证实什么"而不是已下结论），也可能是全局缺口（无 claim）。"""

NEXT_HOP_SCHEMA = {
    "type": "object",
    "properties": {
        "hop_id": {"type": "string"},
        "gap_id": {"type": "string"},
        "round_index": {"type": "integer", "minimum": 0},
        "question": {"type": "string"},
        "queries": {"type": "array", "items": {"type": "string"}},
        "route": {"type": "string", "enum": list(QA_RETRIEVAL_ROUTES) + [""]},
        "routes": {"type": "array", "items": {"type": "string",
                                              "enum": list(QA_RETRIEVAL_ROUTES)}},
        "priority": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
        "query_fingerprint": {"type": "string"},
        "evidence_requirement": EVIDENCE_REQUIREMENT_SCHEMA,
        # route → 真实的检索计划覆盖（三通道各自的落点：queries / entities / terms）
        "plan_overrides": {"type": "object"},
        "hunters": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["hop_id", "gap_id", "question", "queries", "route", "reason"],
    "additionalProperties": True,
}
"""下一跳契约（§13）：一跳 = 一个缺口 + 一条最优检索动作（route）+ 它的可复算指纹。"""

GAP_LOOP_ROUND_SCHEMA = {
    "type": "object",
    "properties": {
        "round_index": {"type": "integer", "minimum": 0},
        "open_gaps": {"type": "integer", "minimum": 0},
        "high_priority_gaps": {"type": "integer", "minimum": 0},
        "new_evidence": {"type": "integer", "minimum": 0},
        "new_verified_claims": {"type": "integer", "minimum": 0},
        "resolved_high_priority_gaps": {"type": "integer", "minimum": 0},
        "planned_hops": {"type": "integer", "minimum": 0},
        "skipped_hops": {"type": "integer", "minimum": 0},
        "dedupe_hits": {"type": "integer", "minimum": 0},
        "no_gain": {"type": "boolean"},
        "no_gain_streak": {"type": "integer", "minimum": 0},
        "stop_reason": {"type": "string", "enum": list(QA_STOP_REASONS) + [""]},
        "detail": {"type": "string"},
    },
    "required": ["round_index", "no_gain", "no_gain_streak"],
    "additionalProperties": True,
}
"""缺口循环单轮回执：每一轮都记"新增多少有效证据/解决了多少高优缺口/丢了几个重复跳"。"""

GAP_LOOP_SCHEMA = {
    "type": "object",
    "properties": {
        "analyzer_version": {"type": "string"},
        "planner_version": {"type": "string"},
        "enabled": {"type": "boolean"},
        "stop_reason": {"type": "string", "enum": list(QA_STOP_REASONS) + [""]},
        "stop_detail": {"type": "string"},
        "no_gain_rounds": {"type": "integer", "minimum": 0},
        "rounds": {"type": "array", "items": GAP_LOOP_ROUND_SCHEMA},
        "gaps": {"type": "array", "items": GAP_SCHEMA},
        "hops": {"type": "array", "items": NEXT_HOP_SCHEMA},
        "seen_dedupe": {"type": "object"},
        "stats": {"type": "object"},
    },
    "required": ["analyzer_version", "enabled", "stop_reason", "rounds"],
    "additionalProperties": True,
}
"""缺口循环整体回执（P07-05/P07-06）：停止原因 + 每轮回执 + 缺口清单 + 下一跳清单。"""


# ── Phase 08（P08-01…P08-06）：Context Graph / Context Planner ────────────────
# 边界：本阶段只做"按任务组装最小有效 Context"，**不新增任何检索/核验/生成能力**。
# 契约设计口径（与 Phase 04~07 一致）：
#   1. §4 的九段上下文**逐字**入 `CONTEXT_SECTIONS`，缺一段就有守门用例挂；
#   2. §5 的 ContextUtility 五个乘子**逐字**入 `CONTEXT_UTILITY_FACTORS`，权重和为 1；
#   3. §6 的 Context Gap 四个动作**逐字**入 `CONTEXT_GAP_ACTIONS`；Context Gap
#      **默认不得触发昂贵新检索**（MASTER_RULES 第 13 条）——契约里用
#      `requires_retrieval` 恒 False 把这条硬规则写成机器可校验形态；
#   4. 引用可回溯：`CONTEXT_ITEM_SCHEMA.grounding` 必须能指到 Phase 02 的
#      `evidence_ref` + 最小 span；指不到的条目必须显式 `grounded=false`
#      （不许伪装成有据，MASTER_RULES 第 11 条）。
CONTEXT_PACK_VERSION = "qa-context-pack-v1"
"""Context Pack 组装版本（换组装口径=换版本）：整包与回执都可复算到这个版本号。"""

CONTEXT_UTILITY_VERSION = "qa-context-utility-v1"
"""ContextUtility 口径版本（§5）：分量定义/权重/词面统计方式变更都要换版本号。"""

CONTEXT_GAP_VERSION = "qa-context-gap-v1"
"""Context Gap 检测版本（§6）：检测规则与动作映射变更都要换版本号。"""

CONTEXT_SELECTION_VERSION = "qa-context-selection-v1"
"""selection trace 版本（P08-06）：选择决策的记法变更要换版本号。"""

GROUNDING_VERSION = "qa-grounding-v1"
"""生成端 grounding 校验版本：校验规则（无证据断言/引用越界/反证漏引）变更要换版本号。"""

CONTEXT_SECTIONS: Tuple[str, ...] = (
    "system_context", "task_context", "evidence_context", "counter_evidence",
    "memory_context", "skill_context", "working_memory", "constraints", "budget",
)
"""§4 统一 Context Pack 的九段（**逐字**）。本阶段只填 7 段：
`memory_context` / `skill_context` 是 Phase 09 / Phase 11 的产物，本阶段留
**空段 + `deferred_to` 标注**（宁缺勿造，不提前实现后续 Phase）。"""

CONTEXT_ITEM_KINDS: Tuple[str, ...] = (
    "claim", "evidence", "counter_evidence", "gap", "constraint",
    "task", "system", "memory", "skill", "working_memory",
)
"""ContextItem 种类：7 种本阶段可产出 + 3 种（memory/skill/working_memory）本阶段只占位。"""

CONTEXT_ITEM_SOURCES: Tuple[str, ...] = (
    "evidence_graph", "evidence", "plan", "verification", "gap_analyzer", "request", "config",
    "memory_graph",
)
"""ContextItem 的**来源**（provenance 的可读形态）：每一条都必须是上游阶段真实产出物，
禁止"凭空造一条上下文"。`memory_graph` 是 Phase 09 的 Memory Graph 召回产物
（只在 `memory_context` 段出现，且恒为 MEMORY_HINT）。"""

CONTEXT_DECISIONS: Tuple[str, ...] = ("included", "excluded")
"""selection trace 的两种决策。"""

CONTEXT_SELECTION_REASONS: Tuple[str, ...] = (
    "MANDATORY_SECTION", "COUNTER_EVIDENCE_RESERVED", "TOP_UTILITY", "DIVERSITY_BONUS",
    "OVER_TOKEN_BUDGET", "DUPLICATE_IDENTITY", "LOW_UTILITY", "NOT_TASK_RELEVANT",
    "NO_GROUNDING_SPAN", "SECTION_DEFERRED", "BUDGET_EXHAUSTED",
)
"""选择/淘汰原因码（每条决策必须带一个，可复算"为什么这条没进包"）。"""

CONTEXT_GAP_ACTIONS: Tuple[str, ...] = (
    "REPACK_CONTEXT", "EXPAND_EVIDENCE_SPAN", "LOAD_COUNTEREVIDENCE", "LOAD_SKILL",
)
"""§6 Context Gap 的四个动作（**逐字**）：证据已存在但 Agent 没拿到 → 先重组上下文。"""

CONTEXT_GAP_TYPES: Tuple[str, ...] = (
    "OMITTED_EVIDENCE", "TRUNCATED_SPAN", "MISSING_COUNTEREVIDENCE",
    "UNGROUNDED_CLAIM", "DUPLICATE_SECTION", "SKILL_NOT_AVAILABLE",
)
"""Context Gap 类型六值（本仓库口径，与 §12 的十种 Evidence Gap 严格区分）：
`OMITTED_EVIDENCE` 证据在图上但没进包；`TRUNCATED_SPAN` 进了包但 span 被截断/摘要不足；
`MISSING_COUNTEREVIDENCE` 该条结论的反证没进包；`UNGROUNDED_CLAIM` 进包的结论没有可回溯 span；
`DUPLICATE_SECTION` 同一事实在多个段落重复占用预算；`SKILL_NOT_AVAILABLE` §8 需要的能力
（contradiction_resolution / citation_verification）属 Phase 11 的 skill_context，本阶段为空段
——只**标注**这个动作，绝不假装已加载。"""

CONTEXT_UTILITY_FACTORS: Tuple[str, ...] = (
    "relevance", "evidence_strength", "task_necessity", "freshness", "diversity",
)
"""§5 ContextUtility 的五个乘子（**逐字**）：
`ContextUtility = Relevance × EvidenceStrength × TaskNecessity × Freshness × Diversity / TokenCost`。
全部 ∈ [0,1]；`TokenCost` 是分母（token 估算，见 `qa_context_pack.estimate_tokens`）。"""

CONTEXT_UTILITY_WEIGHTS = {
    "relevance": 0.34, "evidence_strength": 0.26, "task_necessity": 0.22,
    "freshness": 0.10, "diversity": 0.08,
}
"""五个乘子取"加权几何平均"时的权重（和为 1）。乘以权重是为了让分数可解释：
直接把五个 [0,1] 相乘会让任何一个小分量把整条压到 0（实测：一条权威但没有时间戳的证据
会被 freshness 直接清零），几何平均保留"任一维度为 0 则该维度确实没贡献"的语义，
但不至于因为一个未知维度把整条证据判死。"""

CONTEXT_GROUNDING_VIOLATIONS: Tuple[str, ...] = (
    "CLAIM_WITHOUT_EVIDENCE", "CLAIM_WITH_UNVERIFIED_EVIDENCE", "CITATION_NOT_IN_MAP",
    "CITATION_WITHOUT_SPAN", "NUMBER_WITHOUT_SPAN", "COUNTER_EVIDENCE_OMITTED",
    "UNMARKED_UNGROUNDED_TEXT",
)
"""生成端 grounding 违规码七值（校验用，**不动冻结 schema**）：
前五条是"无证据断言/引用不上"，后两条是"漏反证/无标注"。
`blocking` 的判定见 `qa_context_pack.GROUNDING_BLOCKING_VIOLATIONS`。"""

CONTEXT_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "item_id": {"type": "string"},
        "kind": {"type": "string", "enum": list(CONTEXT_ITEM_KINDS)},
        "section": {"type": "string", "enum": list(CONTEXT_SECTIONS)},
        "text": {"type": "string"},
        "tokens": {"type": "integer", "minimum": 0},
        "source_stage": {"type": "string", "enum": list(CONTEXT_ITEM_SOURCES) + [""]},
        "grounding": {
            "type": "object",
            "properties": {
                "grounded": {"type": "boolean"},
                "evidence_ref": {"type": "string"},
                "evidence_id": {"type": "string"},
                "span": {"type": "object"},
                "source_url": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["grounded"],
            "additionalProperties": True,
        },
        "utility": {"type": "number", "minimum": 0},
        "utility_factors": {"type": "object"},
        "claim_id": {"type": "string"},
        "evidence_ref": {"type": "string"},
        "metadata": {"type": "object"},
    },
    "required": ["item_id", "kind", "section", "text", "tokens", "grounding"],
    "additionalProperties": True,
}
"""ContextItem（P08-01）：每一条上下文都必须能回答"它从哪来、能不能回溯到最小 span"。"""

CONTEXT_EDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "edge_id": {"type": "string"},
        "src": {"type": "string"},
        "dst": {"type": "string"},
        "relation": {"type": "string",
                     "enum": ["SUPPORTS", "REFUTES", "DERIVED_FROM", "REQUIRES", "CONSTRAINS"]},
        "section": {"type": "string"},
    },
    "required": ["edge_id", "src", "dst", "relation"],
    "additionalProperties": True,
}
"""Context 图中的边（P08-01）：只表达"这条上下文与哪条 claim/证据/子问题有关系"，
关系值域取 §3.2 推荐边名的子集（SUPPORTS/REFUTES/DERIVED_FROM/REQUIRES/CONSTRAINS）。"""

CONTEXT_SELECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "trace_version": {"type": "string"},
        "item_id": {"type": "string"},
        "kind": {"type": "string", "enum": list(CONTEXT_ITEM_KINDS)},
        "section": {"type": "string", "enum": list(CONTEXT_SECTIONS)},
        "decision": {"type": "string", "enum": list(CONTEXT_DECISIONS)},
        "reason": {"type": "string", "enum": list(CONTEXT_SELECTION_REASONS)},
        "utility": {"type": "number", "minimum": 0},
        "utility_factors": {"type": "object"},
        "tokens": {"type": "integer", "minimum": 0},
        "reserved": {"type": "boolean"},
        "budget_after": {"type": "integer", "minimum": 0},
        "rank": {"type": "integer", "minimum": 0},
        "detail": {"type": "string"},
    },
    "required": ["trace_version", "item_id", "decision", "reason", "tokens"],
    "additionalProperties": True,
}
"""selection trace 的单条记录（P08-06）：同输入同输出，`reason` 必须来自冻结枚举。"""

CONTEXT_GAP_SCHEMA = {
    "type": "object",
    "properties": {
        "gap_id": {"type": "string"},
        "context_gap_type": {"type": "string", "enum": list(CONTEXT_GAP_TYPES)},
        "claim_id": {"type": "string"},
        "evidence_ref": {"type": "string"},
        "section": {"type": "string", "enum": list(CONTEXT_SECTIONS) + [""]},
        "action": {"type": "string", "enum": list(CONTEXT_GAP_ACTIONS)},
        "requires_retrieval": {"type": "boolean"},
        "detail": {"type": "string"},
        "tokens_recoverable": {"type": "integer", "minimum": 0},
    },
    "required": ["gap_id", "context_gap_type", "action", "requires_retrieval"],
    "additionalProperties": True,
}
"""Context Gap（P08-05）：`requires_retrieval` 恒为 False（MASTER_RULES 第 13 条）。"""

CONTEXT_PACK_SCHEMA = {
    "type": "object",
    "properties": {
        "pack_version": {"type": "string"},
        "pack_id": {"type": "string"},
        "built_at": {"type": "string"},
        "task": {"type": "object"},
        "sections": {"type": "object"},
        "items": {"type": "array", "items": CONTEXT_ITEM_SCHEMA},
        "citation_map": {"type": "object"},
        "citation_index": {"type": "object"},
        "grounding": {"type": "object"},
        "budget": {"type": "object"},
        "context_gaps": {"type": "array", "items": CONTEXT_GAP_SCHEMA},
        "selection_trace": {"type": "array", "items": CONTEXT_SELECTION_SCHEMA},
        "stats": {"type": "object"},
    },
    "required": ["pack_version", "sections", "items", "citation_map", "budget", "stats"],
    "additionalProperties": True,
}
"""Context Pack（P08-03）：九段 + 引用索引 + 预算回执 + Context Gap + selection trace。"""

GROUNDING_REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "grounding_version": {"type": "string"},
        "checked_claims": {"type": "integer", "minimum": 0},
        "grounded_claims": {"type": "integer", "minimum": 0},
        "ungrounded_claims": {"type": "integer", "minimum": 0},
        "violations": {"type": "array", "items": {"type": "object"}},
        "violation_counts": {"type": "object"},
        "blocking": {"type": "boolean"},
        "blocking_codes": {"type": "array",
                           "items": {"type": "string", "enum": list(CONTEXT_GROUNDING_VIOLATIONS)}},
        "note": {"type": "string"},
    },
    "required": ["grounding_version", "checked_claims", "violations", "blocking"],
    "additionalProperties": True,
}
"""生成端 grounding 校验回执（不改冻结 FINAL_ANSWER_SCHEMA：只**校验**，不新增字段）。"""


# ── Phase 09（P09-01…P09-06）：Memory Graph Core ───────────────────────────────
# 边界：跨会话长期记忆及其 **Write Gate / Recall / lifecycle / provenance**。
# 契约设计口径（与 Phase 04~08 一致，全部逐字对齐 01_V2_ARCHITECTURE）：
#   1. §1.4 的十个节点类型入 `MEMORY_TYPES`（缺一个就有守例挂）；§2.3 的六个状态入
#      `MEMORY_STATUSES`；§1.4 的九个关系入 `MEMORY_RELATIONS`；§14 的五个作用域入
#      `MEMORY_SCOPES`（医疗术语按 D-002 中性映射，**取值逐字保留**，映射写在注释里）；
#      §2.2 的四个写决策入 `MEMORY_WRITE_DECISIONS`；§10 的时效档入 `MEMORY_FRESHNESS_CLASSES`。
#   2. §11 的两个公式**逐字**拆进 `MEMORY_WRITE_FACTORS`（写：ReuseProbability × Confidence ×
#      Stability × InformationValue − PrivacyRisk − StalenessRisk − DuplicationPenalty）与
#      `MEMORY_RECALL_FACTORS`（召回：SemanticRelevance × TaskApplicability × Confidence ×
#      Freshness × HistoricalUtility − ContradictionRisk）；每个分量的口径在 `qa_memory` 里
#      写死并可复算（无模型、无嵌入端点）。
#   3. **谁能产出谁负责**：`MEMORY_TYPE_OWNER_PHASE` 把"本阶段可产出"与"后续 Phase 的账"
#      写成机器可校验的形态——Phase 09 只产 VERIFIED_CLAIM / ENTITY（都要求可回溯证据），
#      其余类型一律由 Write Gate **显式拒绝并记账**（`TYPE_DEFERRED_TO_PHASE_*`），
#      绝不静默丢弃、也绝不提前实现后续 Phase。
#   4. MASTER_RULES 第 11/12/16 条写成机器可校验形态：`MEMORY_HINT` 恒不冒充已核验证据
#      （`MEMORY_RECALL_HIT_SCHEMA.requires_revalidation` 恒 True、`verified_evidence` 恒 False），
#      外部网页指令不得升级为系统规则（`MEMORY_WRITE_REASONS` 里的
#      `EXTERNAL_INSTRUCTION_NOT_A_RULE`）。
MEMORY_GRAPH_VERSION = "qa-memory-graph-v1"
"""Memory Graph 版本（schema/类型/写入门/召回/生命周期口径的总版本，落每条记忆的版本行）。"""

MEMORY_WRITE_GATE_VERSION = "qa-memory-write-gate-v1"
"""Write Gate 口径版本（§2.2/§11 的分量与阈值变更都要换这个号）。"""

MEMORY_RECALL_VERSION = "qa-memory-recall-v1"
"""Recall API 口径版本（§11 召回公式与通道口径变更要换这个号）。"""

MEMORY_LIFECYCLE_VERSION = "qa-memory-lifecycle-v1"
"""lifecycle 口径版本（§2.3 状态机与衰减规则变更要换这个号）。"""

MEMORY_PROVENANCE_VERSION = "qa-memory-provenance-v1"
"""provenance 口径版本（记忆→证据/来源/指纹的绑定方式变更要换这个号）。"""

MEMORY_TYPES: Tuple[str, ...] = (
    "MEMORY_ITEM", "VERIFIED_CLAIM", "ENTITY", "EPISODIC_RESEARCH", "STRATEGY", "FAILURE",
    "QUERY_PATTERN", "SOURCE", "SKILL_PERFORMANCE", "USER_APPROVED_DOMAIN_RULE",
)
"""§1.4 Memory Graph 的节点类型**逐字**（MemoryItem / VerifiedClaimMemory / EntityMemory /
EpisodicResearchMemory / StrategyMemory / FailureMemory / QueryPatternMemory / SourceMemory /
SkillPerformanceMemory / UserApprovedDomainRule），命名统一成大写下划线（契约形态）。"""

MEMORY_TYPE_OWNER_PHASE = {
    "MEMORY_ITEM": "P09",
    "VERIFIED_CLAIM": "P09",
    "ENTITY": "P09",
    "EPISODIC_RESEARCH": "P12",
    "STRATEGY": "P12",
    "FAILURE": "P12",
    "QUERY_PATTERN": "P12",
    "SOURCE": "P12",
    "SKILL_PERFORMANCE": "P12",
    "USER_APPROVED_DOMAIN_RULE": "P15",
}
"""每类记忆的**产出归属 Phase**（`P09` 是本阶段；`P12` = Episodic/Failure/Strategy/Source/
Skill performance；`P15` = 作用域与规则类记忆的安全边界）。
Write Gate 对非本阶段类型一律拒收并记账 —— 这是"不提前实现后续 Phase"的机器可校验形态。"""

MEMORY_PRODUCIBLE_TYPES: Tuple[str, ...] = ("VERIFIED_CLAIM", "ENTITY")
"""本阶段（P09）**真的能产出**的两类记忆：都要求可回溯到 Phase 02 证据 + Phase 03 核验结论。"""

MEMORY_SCOPES: Tuple[str, ...] = (
    "GLOBAL_KNOWLEDGE", "ORGANIZATION", "PATIENT_LONGITUDINAL", "ENCOUNTER", "SESSION",
)
"""§14 的五个作用域**逐字**（按 D-002 中性映射到本仓库，取值一个字不改）：
`GLOBAL_KNOWLEDGE`=跨行业包通用知识；`ORGANIZATION`=本部署的组织规则（写入属 P15）；
`PATIENT_LONGITUDINAL`=跨会话长期主体记忆（本项目里绑 industry_pack_id + 主体实体）；
`ENCOUNTER`=本次研究任务的轮次内（含 run 级）；`SESSION`=单会话。
**"当前状态"类记忆不得无条件跨会话当事实**（§14 最后一段）——`ENCOUNTER`/`SESSION` 作用域
的记忆在召回时只在本作用域内可见（有守例）。"""

MEMORY_STATUSES: Tuple[str, ...] = (
    "ACTIVE", "STALE", "SUPERSEDED", "CONTRADICTED", "EXPIRED", "REVOKED",
)
"""§2.3 的六个生命周期状态**逐字**。
本阶段（P09-05）只**自动产出** `ACTIVE`/`STALE`/`EXPIRED`（确定性衰减）；
`SUPERSEDED`/`CONTRADICTED`/`REVOKED` 由 Phase 10（supersession/矛盾/污染撤销）产出，
本阶段只**尊重**它们（非 ACTIVE 一律不作证据、且不被衰减规则"复活"）。"""

MEMORY_FRESHNESS_CLASSES: Tuple[str, ...] = (
    "LONG", "VERSION_SENSITIVE", "MEDIUM", "SHORT", "VERY_SHORT", "SESSION", "ENCOUNTER_BOUND",
)
"""§10 的时效档**逐字**（数学定义/历史事实 LONG；临床指南→本项目"版本敏感的规范/指引"
VERSION_SENSITIVE；药品说明→"产品/参数说明" MEDIUM；软件/API SHORT；新闻/价格/库存
VERY_SHORT；患者当前状态→"当前状态类" SESSION / ENCOUNTER_BOUND）。
**Freshness Gate 的判定属 Phase 10**；本阶段只用它决定"写入门给不给 TTL、衰减多快"。"""

MEMORY_RELATIONS: Tuple[str, ...] = (
    "ABOUT", "DERIVED_FROM", "VALIDATED_BY", "SUPERSEDES", "CONTRADICTS", "EXPIRED_BY",
    "HELPED_RESOLVE", "FAILED_ON", "APPLIES_TO",
)
"""§1.4 的九个关系**逐字**。P09 只**写** `ABOUT`/`DERIVED_FROM`/`VALIDATED_BY`/`APPLIES_TO`，
`SUPERSEDES`/`CONTRADICTS`/`EXPIRED_BY` 的写入属 Phase 10、`HELPED_RESOLVE`/`FAILED_ON`
属 Phase 12；契约先把取值域定死，免得到时候各写各的字符串。"""

MEMORY_WRITE_DECISIONS: Tuple[str, ...] = ("DROP", "SESSION_ONLY", "PERSIST", "PERSIST_WITH_TTL")
"""§2.2 Memory Write Gate 的四个出口**逐字**。"""

MEMORY_WRITE_REASONS: Tuple[str, ...] = (
    # 硬规则拒绝（MASTER_RULES 11/12/16）
    "NO_VERIFIED_EVIDENCE", "EXTERNAL_INSTRUCTION_NOT_A_RULE", "SENSITIVE_CONTENT",
    # 归属与形状
    "TYPE_DEFERRED_TO_PHASE_12", "TYPE_DEFERRED_TO_PHASE_15", "TYPE_NOT_SUPPORTED",
    "EMPTY_CONTENT", "SCOPE_MISSING",
    # 效用与去重
    "UTILITY_BELOW_FLOOR", "DUPLICATE_MERGED", "SESSION_SCOPE_ONLY", "STALENESS_RISK",
    "PRIVACY_RISK",
    # 通过
    "UTILITY_ABOVE_FLOOR", "TTL_REQUIRED_BY_FRESHNESS",
)
"""写决策的理由码（每条决策必须带一个，可复算"为什么这条没被记住"）。"""

MEMORY_WRITE_FACTORS: Tuple[str, ...] = (
    "reuse_probability", "confidence", "stability", "information_value",
    "privacy_risk", "staleness_risk", "duplication_penalty",
)
"""§11 `MemoryWriteUtility` 的七项**逐字**：
`ReuseProbability × Confidence × Stability × InformationValue − PrivacyRisk −
StalenessRisk − DuplicationPenalty`。前三项为乘子（∈[0,1]），后三项为减项（∈[0,1]）。"""

MEMORY_RECALL_FACTORS: Tuple[str, ...] = (
    "semantic_relevance", "task_applicability", "confidence", "freshness",
    "historical_utility", "contradiction_risk",
)
"""§11 `MemoryRecallScore` 的六项**逐字**：
`SemanticRelevance × TaskApplicability × Confidence × Freshness × HistoricalUtility −
ContradictionRisk`（前五项乘子，最后一项减项）。"""

MEMORY_RECALL_MODES: Tuple[str, ...] = ("planning", "evidence")
"""§10 的两种召回模式：`planning`（Research Planner 之前，只召回策略/失败/查询模式类，
避免旧事实污染规划）、`evidence`（缺口出现后召回已验证结论/来源类，但必须先标 MEMORY_HINT）。"""

MEMORY_RECALL_CHANNELS: Tuple[str, ...] = ("relational", "graph", "vector", "lexical")
"""P09-06 的四种召回通道：关系库过滤 / 图关系扩展 / **库内已有向量**的余弦（零端点调用）/
词面匹配。四通道的命中都要能被 `explain` 复算。"""

MEMORY_LIFECYCLE_TRANSITIONS = {
    ("ACTIVE", "STALE"): "DECAY_BELOW_STALE_FLOOR",
    ("ACTIVE", "EXPIRED"): "VALID_UNTIL_PASSED",
    ("STALE", "EXPIRED"): "DECAY_BELOW_EXPIRE_FLOOR",
    ("STALE", "ACTIVE"): "DECAY_ABOVE_STALE_FLOOR",
    ("EXPIRED", "EXPIRED"): "NO_CHANGE",
}
"""P09-05 **允许自动发生的状态迁移**（其余迁移一律拒绝，包括"复活 REVOKED"）：
值 = 理由码。`ACTIVE→STALE→EXPIRED` 是单向衰减，`STALE→ACTIVE` 只在衰减分回升时发生
（例如后来又被核验/复用），`EXPIRED` 不再自动回退（回退要 Phase 10 的 revalidation）。"""

MEMORY_RECALL_HINT_VERSION = "qa-memory-hint-v1"
"""MEMORY_HINT 的记法版本（§2.1：memory hint 经时效/版本/适用性闸门与 Verifier 后才可进证据图）。"""

MEMORY_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "memory_id": {"type": "string"},
        "memory_type": {"type": "string", "enum": list(MEMORY_TYPES)},
        "canonical_content": {"type": "string"},
        "content_fingerprint": {"type": "string"},
        "confidence": {"type": "number"},
        "freshness_class": {"type": "string", "enum": list(MEMORY_FRESHNESS_CLASSES)},
        "valid_from": {"type": "string"},
        "valid_until": {"type": "string"},
        "last_verified_at": {"type": "string"},
        "status": {"type": "string", "enum": list(MEMORY_STATUSES)},
        "scope": {"type": "string", "enum": list(MEMORY_SCOPES)},
        "scope_key": {"type": "string"},
        "entity_ids": {"type": "array", "items": {"type": "string"}},
        "source_evidence_ids": {"type": "array", "items": {"type": "string"}},
        "superseded_by": {"type": "string"},
        "reuse_count": {"type": "integer", "minimum": 0},
        "created_from_session_id": {"type": "string"},
        "created_at": {"type": "string"},
        "updated_at": {"type": "string"},
        "version": {"type": "integer", "minimum": 1},
        "decay_score": {"type": "number"},
        "metadata": {"type": "object"},
    },
    "required": ["memory_id", "memory_type", "canonical_content", "confidence",
                 "freshness_class", "status", "scope"],
    "additionalProperties": True,
}
"""MemoryItem（P09-01/P09-02）：§2.3 要求的字段一个不少（`id/memory_type/canonical_content/
confidence/freshness_class/valid_from/valid_until/last_verified_at/status/scope/
created_from_session_id/created_at` + `entity_ids/source_evidence_ids/superseded_by/reuse_count`）。"""

MEMORY_WRITE_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "decision_id": {"type": "string"},
        "run_id": {"type": "string"},
        "memory_type": {"type": "string", "enum": list(MEMORY_TYPES) + [""]},
        "decision": {"type": "string", "enum": list(MEMORY_WRITE_DECISIONS)},
        "reason": {"type": "string", "enum": list(MEMORY_WRITE_REASONS)},
        "utility": {"type": "number"},
        "factors": {"type": "object"},
        "memory_id": {"type": "string"},
        "content_fingerprint": {"type": "string"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "scope": {"type": "string", "enum": list(MEMORY_SCOPES) + [""]},
        "gate_version": {"type": "string"},
    },
    "required": ["decision_id", "decision", "reason", "gate_version"],
    "additionalProperties": True,
}
"""写决策留痕（P09-03）：**每一条候选记忆**都要留下一行（含被 DROP 的），
`factors` 逐项可复算 —— 这是"为什么没记住"的唯一权威来源。"""

MEMORY_RECALL_HIT_SCHEMA = {
    "type": "object",
    "properties": {
        "memory_id": {"type": "string"},
        "memory_type": {"type": "string", "enum": list(MEMORY_TYPES)},
        "canonical_content": {"type": "string"},
        "status": {"type": "string", "enum": list(MEMORY_STATUSES)},
        "scope": {"type": "string", "enum": list(MEMORY_SCOPES)},
        "freshness_class": {"type": "string", "enum": list(MEMORY_FRESHNESS_CLASSES)},
        "score": {"type": "number"},
        "factors": {"type": "object"},
        "channels": {"type": "array",
                     "items": {"type": "string", "enum": list(MEMORY_RECALL_CHANNELS)}},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "hint": {"type": "boolean"},
        "hint_version": {"type": "string"},
        "requires_revalidation": {"type": "boolean"},
        "verified_evidence": {"type": "boolean"},
        "explain": {"type": "string"},
    },
    "required": ["memory_id", "memory_type", "status", "score", "channels", "hint",
                 "requires_revalidation", "verified_evidence"],
    "additionalProperties": True,
}
"""召回命中（P09-04）：**恒是 MEMORY_HINT**，不是证据。
`requires_revalidation` 恒 True、`verified_evidence` 恒 False（§2.1 + MASTER_RULES 11/12），
本阶段**不实现** revalidation（属 Phase 10）——所以只能说"这是提示，用前必须重验"。"""

MEMORY_RECALL_RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "recall_version": {"type": "string"},
        "mode": {"type": "string", "enum": list(MEMORY_RECALL_MODES)},
        "channels": {"type": "array",
                     "items": {"type": "string", "enum": list(MEMORY_RECALL_CHANNELS)}},
        "hits": {"type": "array", "items": MEMORY_RECALL_HIT_SCHEMA},
        "counts": {"type": "object"},
        "stats": {"type": "object"},
        "scope": {"type": "object"},
        "trace_id": {"type": "string"},
    },
    "required": ["recall_version", "mode", "channels", "hits", "counts"],
    "additionalProperties": True,
}
"""召回回执（P09-04）：命中清单 + 各通道计数 + 边界原因（无向量/无种子/非 ACTIVE 被排除）。"""

MEMORY_LIFECYCLE_REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "lifecycle_version": {"type": "string"},
        "checked": {"type": "integer", "minimum": 0},
        "transitions": {"type": "array", "items": {"type": "object"}},
        "transition_counts": {"type": "object"},
        "status_counts": {"type": "object"},
        "decay_bands": {"type": "object"},
        "factors": {"type": "object"},
        "note": {"type": "string"},
    },
    "required": ["lifecycle_version", "checked", "transitions"],
    "additionalProperties": True,
}
"""lifecycle 维护回执（P09-05）：迁移明细 + 状态/衰减分布（同样输入同样输出、可重复运行）。"""

MEMORY_PROVENANCE_REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "provenance_version": {"type": "string"},
        "checked": {"type": "integer", "minimum": 0},
        "traceable": {"type": "integer", "minimum": 0},
        "untraceable": {"type": "array", "items": {"type": "object"}},
        "links": {"type": "object"},
        "channels": {"type": "object"},
        "note": {"type": "string"},
    },
    "required": ["provenance_version", "checked", "traceable", "links"],
    "additionalProperties": True,
}
"""provenance 审计回执（P09-06）：**每条记忆都必须能回到证据条目/来源指纹**，
回溯不上的必须列进 `untraceable`（不许四舍五入成"都能回溯"）。"""


# ── Phase 10（P10-01…P10-06）：Revalidation & Memory Conflict ──────────────────
# 边界：时效闸门、来源版本检测、MEMORY_HINT 复验、记忆矛盾、取代与污染撤销。
# 契约设计口径（全部逐字对齐 01_V2_ARCHITECTURE）：
#   1. §10 的硬规则逐条落成**判定顺序表**："status!=ACTIVE 不作证据"、
#      "source_version_changed 必须 revalidate"、"freshness_required 必须 revalidate"、
#      "high_stakes 默认 revalidate" —— 四条都是先判先返回、带理由码；
#   2. §11 的矛盾口径**完全复用 Phase 06 的规则裁决**（`qa_evidence_graph.resolve()` 与
#      `CONTRADICTION_RESOLUTION_CODES`）：Phase 10 不另算一套质量判断，只把"记忆"与
#      "本轮证据"折算成同类可比指标（mass/verified_mass/authority/independence/时间）；
#   3. §11 "禁止最新自动覆盖"：只有 `NEWER_VERSION_PRECEDES` 才允许 SUPERSEDE，
#      其余 resolved 只把败方标 `CONTRADICTED`，unresolved 两侧都标 `CONTRADICTED`
#      （status!=ACTIVE 不作证据 → 冲突未消解的记忆不得再当依据）；
#   4. §11 防污染："支持按 source/entity/session 撤销污染 Memory" → `MEMORY_REVOKE_REASONS`；
#   5. MASTER_RULES 11：复验把记忆重新绑上"**本轮** Phase 03 判为 SUPPORTED 的证据"时，
#      才算"有已验证证据支撑"；记忆**正文本身永远不是证据**（只有证据引用是），
#      所以复验回执里 `verified_evidence` 的语义被写成 `verified_scope="evidence_refs"`。
MEMORY_FRESHNESS_GATE_VERSION = "qa-memory-freshness-gate-v1"
"""P10-01 时效闸门口径版本（判定顺序、阈值、需求档位变更都要换这个号）。"""

MEMORY_SOURCE_VERSION_VERSION = "qa-memory-source-version-v1"
"""P10-02 来源版本检测口径版本（版本号抽取规则与比较口径变更要换这个号）。"""

MEMORY_REVALIDATION_VERSION = "qa-memory-revalidation-v1"
"""P10-03 MEMORY_HINT 复验口径版本（复验输入、出口与提升规则变更要换这个号）。"""

MEMORY_CONTRADICTION_VERSION = "qa-memory-contradiction-v1"
"""P10-04 记忆矛盾口径版本（配对规则与状态动作映射变更要换这个号）。"""

MEMORY_SUPERSESSION_VERSION = "qa-memory-supersession-v1"
"""P10-05 取代口径版本（`SUPERSEDED_BY` 的落地形态变更要换这个号）。"""

MEMORY_REVOKE_VERSION = "qa-memory-revoke-v1"
"""P10-06 撤销口径版本（污染选择器与高危钩子变更要换这个号）。"""

MEMORY_FRESHNESS_DECISIONS: Tuple[str, ...] = ("ALLOW", "REVALIDATE", "BLOCK")
"""§10 Freshness Gate 的三个出口：
· `ALLOW`：够新、有已核验证据绑定 → 提示可原样用（**仍然是提示，不是证据**）；
· `REVALIDATE`：必须重新核验（四条硬规则命中其一）；
· `BLOCK`：连提示都不该用（非 ACTIVE / 衰减低于过期线）。"""

MEMORY_FRESHNESS_REASONS: Tuple[str, ...] = (
    # BLOCK（两个：终态不许复验 / 衰减已到过期线）
    "NOT_ACTIVE", "DECAY_BELOW_EXPIRE_FLOOR",
    # REVALIDATE（§10 的四条硬规则 + 状态与年龄阈值）
    "VALID_UNTIL_PASSED", "STATUS_EXPIRED", "SOURCE_VERSION_CHANGED", "HIGH_STAKES_DEFAULT",
    "FRESHNESS_REQUIRED", "STATUS_STALE", "AGE_OVER_REVALIDATE_THRESHOLD",
    "NO_EVIDENCE_BINDING",
    # ALLOW
    "FRESH_AND_VERIFIED",
)
"""时效闸门的理由码（每条判定都要带一个，可复算"为什么这条要重新核验"）：

判定顺序**先判先返回**（P10-01 的唯一真源，守卫用例逐条钉死）：
① `SUPERSEDED`/`CONTRADICTED`/`REVOKED` → BLOCK/`NOT_ACTIVE`（终态，不许复验）；
② `valid_until` 已过 → REVALIDATE/`VALID_UNTIL_PASSED`；
③ 状态 `EXPIRED` → REVALIDATE/`STATUS_EXPIRED`（§2.3 写明回退要 Phase 10 的 revalidation）；
④ 衰减分 < 过期线 → BLOCK/`DECAY_BELOW_EXPIRE_FLOOR`；
⑤ 来源版本变了 → REVALIDATE/`SOURCE_VERSION_CHANGED`（§10 硬规则）；
⑥ 高危 → REVALIDATE/`HIGH_STAKES_DEFAULT`（§10 硬规则）；
⑦ 时效档属于必须复验的档 → REVALIDATE/`FRESHNESS_REQUIRED`（§10 硬规则）；
⑧ 状态 `STALE` → REVALIDATE/`STATUS_STALE`；
⑨ 年龄超过"半衰期 × 比例"阈值 → REVALIDATE/`AGE_OVER_REVALIDATE_THRESHOLD`；
⑩ 没有任何证据绑定 → REVALIDATE/`NO_EVIDENCE_BINDING`；
⑪ 其余 → ALLOW/`FRESH_AND_VERIFIED`。"""

MEMORY_SOURCE_VERSION_REASONS: Tuple[str, ...] = (
    "CORPUS_VERSION_CHANGED", "DOCUMENT_VERSION_CHANGED", "SOURCE_VERSION_STABLE",
    "NO_CORPUS_VERSION", "NO_VERSION_TOKEN",
)
"""来源版本检测的理由码：语料版本变了 / 文档版本号变新 / 稳定 / 没有可比信息。"""

MEMORY_REVALIDATION_OUTCOMES: Tuple[str, ...] = (
    "REVALIDATED", "REFRESHED_NO_CHANGE", "REFUTED", "UNVERIFIED",
    "NO_CANDIDATE_EVIDENCE", "BLOCKED_BY_GATE", "SKIPPED_NOT_ACTIVE",
)
"""P10-03 的七个出口：
· `REVALIDATED`：本轮证据**规则核验判 SUPPORTED** → 记忆重新绑上已验证证据；
· `REFRESHED_NO_CHANGE`：闸门判 ALLOW（够新且有绑定）→ 不重跑核验，只刷新时间戳；
· `REFUTED`：本轮证据判 REFUTED（交给 P10-04 的矛盾裁决）；
· `UNVERIFIED`：有候选证据但都判不到 SUPPORTED（不许假装验过）；
· `NO_CANDIDATE_EVIDENCE`：本轮没有可比对的证据 —— **不编检索**（检索是 Phase 04/07 的账）；
· `BLOCKED_BY_GATE` / `SKIPPED_NOT_ACTIVE`：时效闸门或状态不允许复验。"""

MEMORY_REVALIDATION_REASONS: Tuple[str, ...] = (
    "EVIDENCE_STILL_SUPPORTS", "FRESHNESS_NOT_DUE", "EVIDENCE_REFUTES",
    "EVIDENCE_INSUFFICIENT", "NO_EVIDENCE_AVAILABLE", "MEMORY_NOT_ACTIVE",
    "GATE_BLOCKED", "HIGH_RISK_UNREVALIDATED",
)
"""复验理由码（与出口一一对应；`HIGH_RISK_UNREVALIDATED` 是高危钩子的降级理由）。"""

MEMORY_CONTRADICTION_KINDS: Tuple[str, ...] = ("memory_memory", "memory_evidence")
"""两类记忆矛盾：两条记忆互相冲突；一条记忆被**本轮证据**反驳（§11 的 M1 支持 C / M2 反驳 C）。"""

MEMORY_CONTRADICTION_OUTCOMES: Tuple[str, ...] = ("resolved", "unresolved")
"""矛盾裁决的两个结果（**逐字沿用 Phase 06** 的 `resolution` 取值域）。"""

MEMORY_CONTRADICTION_STATUS_ACTIONS: Tuple[str, ...] = (
    "SUPERSEDE", "CONTRADICT", "KEEP_BOTH", "NONE",
)
"""矛盾裁决落到状态上的四个动作（§11 禁止"最新自动覆盖"，所以只有时间裁决才 SUPERSEDE）：
· `SUPERSEDE`：`NEWER_VERSION_PRECEDES` → 旧方 SUPERSEDED + 建取代链；
· `CONTRADICT`：权威/质量/独立性/强度裁决或未消解 → 败方（未消解则双方）CONTRADICTED；
· `KEEP_BOTH`：`SCOPE_DIFFERENCE` → 两份都成立，不改状态；
· `NONE`：不适用（例如对侧不是记忆）。"""

MEMORY_SUPERSESSION_REASONS: Tuple[str, ...] = (
    "NEWER_VERSION_PRECEDES", "SOURCE_VERSION_CHANGED", "MANUAL_SUPERSEDE",
)
"""取代理由码：时间裁决胜出 / 来源版本变新 / 人工指定。"""

MEMORY_REVOKE_REASONS: Tuple[str, ...] = (
    "SOURCE_CONTAMINATED", "ENTITY_CONTAMINATED", "SESSION_CONTAMINATED",
    "EXTERNAL_INSTRUCTION_CONTAMINATION", "SENSITIVE_CONTENT", "MANUAL_REVOKE",
)
"""§11 防污染的撤销理由码（按 source/entity/session 撤销 + 指令注入/敏感内容 + 人工）。"""

MEMORY_CONTRADICTION_RESOLUTION_CODES: Tuple[str, ...] = CONTRADICTION_RESOLUTION_CODES
"""Phase 10 的矛盾理由码 = **Phase 06 的同一张表**（单一真源，不另立一套质量判断）。"""

MEMORY_REVALIDATION_TRANSITIONS = {
    # 复验成功：把被衰减压下去的记忆放回可用（§2.3；P09 契约里写明"回退要 Phase 10 的 revalidation"）
    ("STALE", "ACTIVE"): "REVALIDATED_WITH_FRESH_EVIDENCE",
    ("EXPIRED", "ACTIVE"): "REVALIDATED_WITH_FRESH_EVIDENCE",
    # 复验不成立但仍是提示：状态不动
    ("ACTIVE", "ACTIVE"): "NO_CHANGE",
    ("STALE", "STALE"): "NO_CHANGE",
    ("EXPIRED", "EXPIRED"): "NO_CHANGE",
    ("SUPERSEDED", "SUPERSEDED"): "NO_CHANGE",
    ("CONTRADICTED", "CONTRADICTED"): "NO_CHANGE",
    ("REVOKED", "REVOKED"): "NO_CHANGE",
    # 高危且未复验成功：降级为 STALE（仍是提示、但明确"不该直接采信"）
    ("ACTIVE", "STALE"): "HIGH_RISK_UNREVALIDATED",
    # 取代（§11）：只有时间裁决能走到这里
    ("ACTIVE", "SUPERSEDED"): "SUPERSEDED_BY_NEWER",
    ("STALE", "SUPERSEDED"): "SUPERSEDED_BY_NEWER",
    # 矛盾：败方 / 未消解双方
    ("ACTIVE", "CONTRADICTED"): "CONTRADICTED_BY_CONFLICT",
    ("STALE", "CONTRADICTED"): "CONTRADICTED_BY_CONFLICT",
    # 污染撤销（终态，不可复活）
    ("ACTIVE", "REVOKED"): "REVOKED_BY_POLLUTION",
    ("STALE", "REVOKED"): "REVOKED_BY_POLLUTION",
    ("SUPERSEDED", "REVOKED"): "REVOKED_BY_POLLUTION",
    ("CONTRADICTED", "REVOKED"): "REVOKED_BY_POLLUTION",
    ("EXPIRED", "REVOKED"): "REVOKED_BY_POLLUTION",
}
"""Phase 10 **允许自动发生的状态迁移**（值 = 理由码）。

与 P09 的 `MEMORY_LIFECYCLE_TRANSITIONS` 分开维护：那张表是"衰减能自动做什么"，
这张表是"复验/矛盾/取代/撤销能自动做什么"。**REVOKED 是终态**：本表里没有任何
`REVOKED → *` 的迁移（撤销不可复活，MASTER_RULES 11/12 的防污染口径）。"""

MEMORY_FRESHNESS_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "gate_version": {"type": "string"},
        "memory_id": {"type": "string"},
        "decision": {"type": "string", "enum": list(MEMORY_FRESHNESS_DECISIONS)},
        "reason": {"type": "string", "enum": list(MEMORY_FRESHNESS_REASONS)},
        "freshness_class": {"type": "string", "enum": list(MEMORY_FRESHNESS_CLASSES) + [""]},
        "status": {"type": "string", "enum": list(MEMORY_STATUSES) + [""]},
        "age_days": {"type": "number"},
        "half_life_days": {"type": "number"},
        "decay_score": {"type": "number"},
        "valid_until": {"type": "string"},
        "high_stakes": {"type": "boolean"},
        "source_version_changed": {"type": "boolean"},
        "thresholds": {"type": "object"},
        "detail": {"type": "string"},
    },
    "required": ["gate_version", "memory_id", "decision", "reason"],
    "additionalProperties": True,
}
"""时效闸门判定（P10-01）：每条记忆一条，理由码 + 参与判定的数字全部落下来。"""

MEMORY_SOURCE_VERSION_SCHEMA = {
    "type": "object",
    "properties": {
        "source_version_check": {"type": "string"},
        "memory_id": {"type": "string"},
        "changed": {"type": "boolean"},
        "reasons": {"type": "array",
                    "items": {"type": "string", "enum": list(MEMORY_SOURCE_VERSION_REASONS)}},
        "corpus_version_current": {"type": "string"},
        "link_corpus_versions": {"type": "array", "items": {"type": "string"}},
        "version_tokens_memory": {"type": "array", "items": {"type": "string"}},
        "version_tokens_current": {"type": "array", "items": {"type": "string"}},
        "detail": {"type": "string"},
    },
    "required": ["source_version_check", "memory_id", "changed", "reasons"],
    "additionalProperties": True,
}
"""来源版本检测（P10-02）：语料版本 + 文档版本号两条确定性口径，理由码可复算。"""

MEMORY_REVALIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "validation_id": {"type": "string"},
        "revalidation_version": {"type": "string"},
        "memory_id": {"type": "string"},
        "memory_type": {"type": "string", "enum": list(MEMORY_TYPES) + [""]},
        "run_id": {"type": "string"},
        "trace_id": {"type": "string"},
        "outcome": {"type": "string", "enum": list(MEMORY_REVALIDATION_OUTCOMES)},
        "reason": {"type": "string", "enum": list(MEMORY_REVALIDATION_REASONS)},
        "status_before": {"type": "string", "enum": list(MEMORY_STATUSES) + [""]},
        "status_after": {"type": "string", "enum": list(MEMORY_STATUSES) + [""]},
        "freshness": {"type": "object"},
        "source_version": {"type": "object"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "verdicts": {"type": "object"},
        "verified_evidence": {"type": "boolean"},
        "verified_scope": {"type": "string"},
        "promoted": {"type": "boolean"},
        "high_stakes": {"type": "boolean"},
        "judge": {"type": "string"},
        "candidates": {"type": "integer", "minimum": 0},
        "detail": {"type": "string"},
    },
    "required": ["validation_id", "revalidation_version", "memory_id", "outcome", "reason",
                 "verified_evidence", "verified_scope"],
    "additionalProperties": True,
}
"""复验回执（P10-03）：出口/理由码/证据引用/核验 verdict 分布全部落库，可逐条复算。

`verified_evidence=True` 的语义被 `verified_scope` 钉死为 `evidence_refs`：
**可作证据的是被重新核验的那些证据引用，记忆正文本身不是证据**（MASTER_RULES 11）。"""

MEMORY_CONTRADICTION_SCHEMA = {
    "type": "object",
    "properties": {
        "contradiction_id": {"type": "string"},
        "contradiction_version": {"type": "string"},
        "kind": {"type": "string", "enum": list(MEMORY_CONTRADICTION_KINDS)},
        "conflict_type": {"type": "string"},
        "left_memory_id": {"type": "string"},
        "right_memory_id": {"type": "string"},
        "right_evidence_ref": {"type": "string"},
        "resolution": {"type": "string", "enum": list(MEMORY_CONTRADICTION_OUTCOMES)},
        "reason_code": {"type": "string", "enum": list(CONTRADICTION_RESOLUTION_CODES)},
        "decider": {"type": "string"},
        "rationale": {"type": "string"},
        "status_action": {"type": "string", "enum": list(MEMORY_CONTRADICTION_STATUS_ACTIONS)},
        "status_actions": {"type": "array", "items": {"type": "object"}},
        "left": {"type": "object"},
        "right": {"type": "object"},
        "run_id": {"type": "string"},
    },
    "required": ["contradiction_id", "contradiction_version", "kind", "left_memory_id",
                 "resolution", "reason_code", "status_action"],
    "additionalProperties": True,
}
"""记忆矛盾（P10-04）：`resolution`/`reason_code` 直接来自 Phase 06 的裁决器（单一真源）。"""

MEMORY_SUPERSESSION_SCHEMA = {
    "type": "object",
    "properties": {
        "supersession_id": {"type": "string"},
        "supersession_version": {"type": "string"},
        "memory_id": {"type": "string"},
        "superseded_by": {"type": "string"},
        "relation": {"type": "string", "enum": list(MEMORY_RELATIONS)},
        "reason": {"type": "string", "enum": list(MEMORY_SUPERSESSION_REASONS)},
        "from_status": {"type": "string", "enum": list(MEMORY_STATUSES) + [""]},
        "run_id": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["supersession_id", "supersession_version", "memory_id", "superseded_by",
                 "relation", "reason"],
    "additionalProperties": True,
}
"""取代（P10-05）：§11 的 `M1--SUPERSEDED_BY→M2` 在本仓库的落地形态 ——
`memory_item.superseded_by` 字段 + 一条方向相反的 `SUPERSEDES` 关系边（M2 → M1）；
关系枚举是冻结的九个取值，**不新增** `SUPERSEDED_BY` 这个边名。"""

MEMORY_REVOKE_RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "revoke_version": {"type": "string"},
        "reason": {"type": "string", "enum": list(MEMORY_REVOKE_REASONS)},
        "selector": {"type": "object"},
        "checked": {"type": "integer", "minimum": 0},
        "revoked": {"type": "array", "items": {"type": "object"}},
        "skipped": {"type": "array", "items": {"type": "object"}},
        "status_counts": {"type": "object"},
        "already_revoked": {"type": "integer", "minimum": 0},
        "run_id": {"type": "string"},
        "high_risk": {"type": "object"},
        "note": {"type": "string"},
    },
    "required": ["revoke_version", "reason", "checked", "revoked"],
    "additionalProperties": True,
}
"""污染撤销回执（P10-06）：逐条列出被撤销/被跳过的记忆（不许四舍五入成"撤销过了"）。"""

MEMORY_REVALIDATION_REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "revalidation_version": {"type": "string"},
        "gate_version": {"type": "string"},
        "source_version_check": {"type": "string"},
        "contradiction_version": {"type": "string"},
        "checked": {"type": "integer", "minimum": 0},
        "gate_decisions": {"type": "object"},
        "gate_reasons": {"type": "object"},
        "outcomes": {"type": "object"},
        "reasons": {"type": "object"},
        "source_version": {"type": "object"},
        "revalidated": {"type": "integer", "minimum": 0},
        "promoted": {"type": "integer", "minimum": 0},
        "contradictions": {"type": "object"},
        "supersessions": {"type": "integer", "minimum": 0},
        "revocations": {"type": "integer", "minimum": 0},
        "high_risk": {"type": "object"},
        "idempotent": {"type": "boolean"},
        "note": {"type": "string"},
    },
    "required": ["revalidation_version", "checked", "gate_decisions", "outcomes"],
    "additionalProperties": True,
}
"""Phase 10 总回执（真跑分布的证据口径）：闸门判定、复验出口、矛盾裁决、取代与撤销计数。"""


def describe() -> str:
    """给验收脚本/日志用的一行摘要（不参与业务逻辑）。"""
    return ("图谱契约 %s / 证据层 %s / 检索舰队 %s / 执行图 %s / 证据图 %s / 缺口分析 %s："
            "节点类型 %d / 边关系 %d / "
            "检索通道 %d / 失败策略 %d / 停止原因 %d / 证据状态 %d / Hunter %d / "
            "问题意图 %d / 执行节点类型 %d / 证据图节点类型 %d / 证据图关系 %d / 裁决理由码 %d / "
            "缺口类型 %d / 优先级分档 %d / Context 段 %d / ContextItem 种类 %d / Context Gap 动作 %d / "
            "记忆类型 %d / 记忆状态 %d / 记忆关系 %d / "
            "时效闸门出口 %d / 复验出口 %d / 记忆矛盾种类 %d / 撤销理由 %d"
            % (GRAPH_CONTRACT_VERSION, EVIDENCE_LAYER_VERSION, HUNTER_CONTRACT_VERSION,
               EXECUTION_GRAPH_VERSION, EVIDENCE_GRAPH_VERSION, GAP_ANALYZER_VERSION,
               len(KG_NODE_TYPES), len(KG_RELATION_KINDS),
               len(QA_RETRIEVAL_ROUTES), len(QA_FAILURE_POLICIES), len(QA_STOP_REASONS),
               len(EVIDENCE_STATUSES), len(QA_HUNTER_IDS),
               len(QUERY_INTENTS), len(EXECUTION_NODE_KINDS),
               len(EVIDENCE_GRAPH_NODE_TYPES), len(EVIDENCE_GRAPH_RELATIONSHIPS),
               len(CONTRADICTION_RESOLUTION_CODES),
               len(QA_GAP_TYPES), len(GAP_PRIORITY_BANDS),
               len(CONTEXT_SECTIONS), len(CONTEXT_ITEM_KINDS), len(CONTEXT_GAP_ACTIONS),
               len(MEMORY_TYPES), len(MEMORY_STATUSES), len(MEMORY_RELATIONS),
               len(MEMORY_FRESHNESS_DECISIONS), len(MEMORY_REVALIDATION_OUTCOMES),
               len(MEMORY_CONTRADICTION_KINDS), len(MEMORY_REVOKE_REASONS)))


def _check_node(schema: dict, payload: Mapping, path: str) -> str:
    """递归查一个对象：required / enum / 嵌套 object / 数组元素。返回空串表示通过。"""
    for key in schema.get("required") or []:
        if key not in payload or payload.get(key) in (None, ""):
            return "缺少必需字段：%s%s" % (path, key)
    for key, spec in (schema.get("properties") or {}).items():
        if key not in payload or not isinstance(spec, Mapping):
            continue
        value = payload.get(key)
        enum = spec.get("enum")
        if enum and value not in enum:
            return "字段 %s%s 取值 %r 不在枚举内" % (path, key, value)
        if spec.get("type") == "object" and isinstance(value, Mapping):
            failure = _check_node(spec, value, "%s%s." % (path, key))
            if failure:
                return failure
        if spec.get("type") == "array" and isinstance(value, (list, tuple)):
            item_schema = spec.get("items")
            if isinstance(item_schema, Mapping):
                for index, item in enumerate(value):
                    if isinstance(item, Mapping):
                        failure = _check_node(item_schema, item, "%s%s[%d]." % (path, key, index))
                        if failure:
                            return failure
                        continue
                    # 标量元素同样要守枚举：字符串数组的取值域也是契约的一部分
                    # （Phase 07 的 `suggested_routes` / `routes` 就靠这一条拦住冻结枚举外的通道值）
                    item_enum = item_schema.get("enum")
                    if item_enum and item not in item_enum:
                        return "字段 %s%s[%d] 取值 %r 不在枚举内" % (path, key, index, item)
    return ""


def validate(schema_name: str, payload: dict) -> Tuple[bool, str]:
    """极简自校验（不引入 jsonschema 依赖）：查 required 与枚举，并递归到嵌套对象/数组。

    返回 (是否通过, 说明)。用于守门测试与运维自检；**不参与检索/生成链路**。
    Phase 02 起支持嵌套（span/source/entities/relations），对既有五个 schema 行为不变
    （它们没有嵌套对象字段）。
    """
    schemas = {
        "kg_node": KG_NODE_SCHEMA, "kg_edge": KG_EDGE_SCHEMA,
        "claim_evidence_edge": CLAIM_EVIDENCE_EDGE_SCHEMA,
        "search_trace": SEARCH_TRACE_SCHEMA, "execution_node": EXECUTION_NODE_SCHEMA,
        "evidence_span": EVIDENCE_SPAN_SCHEMA, "evidence_source": EVIDENCE_SOURCE_SCHEMA,
        "evidence_entity": EVIDENCE_ENTITY_SCHEMA, "evidence_relation": EVIDENCE_RELATION_SCHEMA,
        "evidence_object": EVIDENCE_OBJECT_SCHEMA,
        "evidence_verification": EVIDENCE_VERIFICATION_SCHEMA,
        # Phase 04（P04-01…P04-06）
        "hunter_result": HUNTER_RESULT_SCHEMA,
        "hunter_fleet_result": HUNTER_FLEET_RESULT_SCHEMA,
        # Phase 05（P05-01…P05-05）
        "execution_graph": EXECUTION_GRAPH_SCHEMA,
        "sub_question": SUB_QUESTION_SCHEMA,
        "plan_claim": PLAN_CLAIM_SCHEMA,
        "evidence_requirement": EVIDENCE_REQUIREMENT_SCHEMA,
        "query_interpretation": QUERY_INTERPRETATION_SCHEMA,
        "signal": SIGNAL_SCHEMA,
        # Phase 06（P06-01…P06-04）
        "evidence_graph": EVIDENCE_GRAPH_SCHEMA,
        "evidence_graph_node": EVIDENCE_GRAPH_NODE_SCHEMA,
        "evidence_graph_edge": EVIDENCE_GRAPH_EDGE_SCHEMA,
        "claim_coverage": CLAIM_COVERAGE_SCHEMA,
        "contradiction_decision": CONTRADICTION_DECISION_SCHEMA,
        # Phase 07（P07-01…P07-06）
        "gap": GAP_SCHEMA,
        "next_hop": NEXT_HOP_SCHEMA,
        "gap_loop_round": GAP_LOOP_ROUND_SCHEMA,
        "gap_loop": GAP_LOOP_SCHEMA,
        # Phase 08（P08-01…P08-06）
        "context_item": CONTEXT_ITEM_SCHEMA,
        "context_edge": CONTEXT_EDGE_SCHEMA,
        "context_selection": CONTEXT_SELECTION_SCHEMA,
        "context_gap": CONTEXT_GAP_SCHEMA,
        "context_pack": CONTEXT_PACK_SCHEMA,
        "grounding_report": GROUNDING_REPORT_SCHEMA,
        # Phase 09（P09-01…P09-06）
        "memory_item": MEMORY_ITEM_SCHEMA,
        "memory_write_decision": MEMORY_WRITE_DECISION_SCHEMA,
        "memory_recall_hit": MEMORY_RECALL_HIT_SCHEMA,
        "memory_recall_receipt": MEMORY_RECALL_RECEIPT_SCHEMA,
        "memory_lifecycle_report": MEMORY_LIFECYCLE_REPORT_SCHEMA,
        "memory_provenance_report": MEMORY_PROVENANCE_REPORT_SCHEMA,
        # Phase 10（P10-01…P10-06）
        "memory_freshness_decision": MEMORY_FRESHNESS_DECISION_SCHEMA,
        "memory_source_version": MEMORY_SOURCE_VERSION_SCHEMA,
        "memory_revalidation": MEMORY_REVALIDATION_SCHEMA,
        "memory_contradiction": MEMORY_CONTRADICTION_SCHEMA,
        "memory_supersession": MEMORY_SUPERSESSION_SCHEMA,
        "memory_revoke_receipt": MEMORY_REVOKE_RECEIPT_SCHEMA,
        "memory_revalidation_report": MEMORY_REVALIDATION_REPORT_SCHEMA,
    }
    schema = schemas.get(str(schema_name))
    if not schema:
        return False, "未知 schema：%s" % schema_name
    payload = payload if isinstance(payload, dict) else {}
    failure = _check_node(schema, payload, "")
    return (False, failure) if failure else (True, "ok")

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
    "memory_graph", "skill_registry",
)
"""ContextItem 的**来源**（provenance 的可读形态）：每一条都必须是上游阶段真实产出物，
禁止"凭空造一条上下文"。`memory_graph` 是 Phase 09 的 Memory Graph 召回产物
（只在 `memory_context` 段出现，且恒为 MEMORY_HINT）；`skill_registry` 是 Phase 11 的
Skill Router 产物（只在 `skill_context` 段出现，且恒为 SKILL_HINT，**不是证据**）。

来源列表**只做追加**（不改既有取值）：Phase 09 追加 `memory_graph`、Phase 11 追加
`skill_registry`；`tests/test_qa_phase09_contracts.py` 的位置断言已同步强化为
"Phase 08 的前 7 个取值逐字不变 + Phase 09 的追加在第 8 位"，比原来的"最后一个是谁"更强。"""

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

**这是类型归属的唯一真源**（Phase 12 起）：Write Gate 用"产出方 Phase 是否已激活"
决定拒收还是放行 —— `P12` 落地后那六类由 `qa_memory_experience` 产出；
`P15` 未落地，仍然拒收并记 `TYPE_DEFERRED_TO_PHASE_15`（不提前实现后续 Phase）。
派生视图见 `MEMORY_PRODUCIBLE_TYPES_BY_PHASE`（守门用例断言两者逐键一致）。"""

MEMORY_PRODUCIBLE_TYPES: Tuple[str, ...] = ("VERIFIED_CLAIM", "ENTITY")
"""Phase 09 落地时**真的能产出**的两类记忆：都要求可回溯到 Phase 02 证据 + Phase 03 核验结论。

Phase 12 起，写门的"能不能产"不再只读这一个元组，而是读
`MEMORY_PRODUCIBLE_TYPES_BY_PHASE` × 激活的产出方（`EXPERIENCE_PRODUCER_PHASES`）；
本元组保留为 **P09 产出方的声明**（历史口径，仍被 P09 用例引用）。"""

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
`SUPERSEDES`/`CONTRADICTS`/`EXPIRED_BY` 的写入属 Phase 10；
`HELPED_RESOLVE`（剧集/策略 → 它真的解决过的缺口或结论）与 `FAILED_ON`（失败记忆 → 它
栽过的那条通道/来源）由 **Phase 12** 写入（契约取值域早就定死，免得到时候各写各的字符串）。"""

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
    # Phase 12（P12-02/P12-03/P12-04/P12-05）：经验类记忆的两条硬规则
    # （**只追加、不改既有取值**，见 DECISION_LOG D-044）：
    #   · `NO_TRACE_BINDING`：经验类记忆是"对 trace 的归纳"，没有 trace 绑定就只能是编造；
    #   · `BELOW_SAMPLE_FLOOR`：样本少于下限不许成记忆（与 Phase 11 的 min_samples 同源，
    #     不许拿一次观测当规律）。
    "NO_TRACE_BINDING", "BELOW_SAMPLE_FLOOR",
    #   · `NO_EVIDENCE_LINK`（P12-04 起）：经验记忆也必须能回到**真实证据**（P09 的
    #     provenance 硬规则）。零证据轮（那一轮一条证据都没取到）连经验都记不下来 ——
    #     代价是"这条轮次的信息丢了"，但换来的是"落库的每一条经验都查得到出处"，
    #     并且**拒收本身留在写决策里**（可审计）。
    "NO_EVIDENCE_LINK",
    # 通过
    "UTILITY_ABOVE_FLOOR", "TTL_REQUIRED_BY_FRESHNESS",
    # Phase 15（P15-01）：作用域强制**拒收**时的出口理由码（**只追加、不改既有取值**）。
    # 判定层给出"为什么"（`SCOPE_ENFORCEMENT_REASONS`），写门给出"出口"（这一条），
    # 具体的判定理由码落在决策行的 `metadata.scope_enforcement` 里（可审计、可复算）。
    "SCOPE_ENFORCEMENT_REJECTED",
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
        # Phase 15（P15-01/P15-02/P15-03）：记忆安全的**子回执**（作用域判定 + 轮次隔离 +
        # 长期时效）。做成**嵌套子键**而不是新的顶层兄弟键：`QA_MEMORY_GRAPH=0` 时一次召回
        # 都不发生 → 一个键都不新增，回滚口径与 Phase 09 逐字一致（有守门用例）。
        "memory_safety": {"type": "object"},
    },
    "required": ["recall_version", "mode", "channels", "hits", "counts"],
    "additionalProperties": True,
}
"""召回回执（P09-04）：命中清单 + 各通道计数 + 边界原因（无向量/无种子/非 ACTIVE 被排除）。

P15 起多一个**可选**子键 `memory_safety`（作用域强制 + 轮次隔离 + 长期时效的回执）。"""

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


# ── Phase 11（P11-01…P11-06）：Skill Registry & Router ─────────────────────────
# 边界：**按任务加载最小必要能力**（§8）。本阶段只做"技能目录 + 选择 + 指令按需加载 +
# 效果遥测"，**不新增任何检索/核验/生成能力**，也**不调用任何模型或嵌入端点**。
# 契约设计口径（与 Phase 04~10 一致，逐字对齐 01_V2_ARCHITECTURE）：
#   1. §8 的目录树（`skills/` 下 11 个技能目录）**逐字**入 `SKILL_IDS`，缺一个就有守例挂；
#      §8 的九个声明字段（skill_id/description/input_schema/output_schema/preconditions/
#      cost_class/latency_class/permissions/version）**全部进 `SKILL_SCHEMA.required`** ——
#      技能是"可被机器校验的能力声明"，少一个字段就不该被注册进注册表。
#   2. **与既有阶段口径统一，不另造一套**：
#      · 技能的检索落点只吃 Phase 01 冻结的 `QA_RETRIEVAL_ROUTES` 七个通道值
#        （`SKILL_ROUTE_BY_ID`，与 `qa_gap_analyzer.HUNTER_BY_ROUTE` 同源）；
#      · Phase 07 的 `suggested_routes` 就是"缺口需要哪些能力"的单一真源：Router 按通道
#        反查技能（`SKILL_FOR_ROUTE`），所以"Gap → 能力"这条链子 Phase 07 已经定过，
#        Phase 11 只是把它翻译成技能，不重新发明一套 gap→skill 表；
#      · Phase 08 的 Context Gap `SKILL_NOT_AVAILABLE` 与动作 `LOAD_SKILL` 是**同一个东西
#        的上游标注**，Phase 11 负责把它真的加载掉（加载后该缺口不再成立）。
#   3. **MASTER_RULES 第 11 条写成机器可校验形态**：技能条目是**提示**不是已验证证据 ——
#      `SKILL_INSTRUCTION_SCHEMA` 的 `is_evidence` 恒 False、`requires_revalidation` 恒 True、
#      `in_citation_map` 恒 False；`skill_context` 段只承载"怎么做"，不承载"事实是什么"。
#   4. **谁能产出谁负责**：`SKILL_ROUTE_BY_ID` 里空串 = 本部署没有这条通道（EMR / 问诊 /
#      推理型技能）。这类技能**可以被选中当指令**（它们教 Agent 怎么处理），但绝不被
#      记成"检索通道"（否则 SearchTrace 的 route 会开始接受冻结枚举外的取值）。
SKILL_SCHEMA_VERSION = "qa-skill-schema-v1"
"""Skill 声明 schema 版本（§8 的九个字段）：字段增删/语义变更都要换版本号。"""

SKILL_REGISTRY_VERSION = "qa-skill-registry-v1"
"""注册表版本（换目录=换版本）：整表指纹与注册回执都可复算到这个版本号。"""

SKILL_ROUTER_VERSION = "qa-skill-router-v1"
"""Skill Router 版本（§8 的选择口径）：选择规则/理由码/排序键变更都要换版本号。"""

SKILL_BUDGET_VERSION = "qa-skill-budget-v1"
"""permission/cost/latency 闸门版本（P11-04）：判据与理由码变更都要换版本号。"""

SKILL_INSTRUCTION_VERSION = "qa-skill-instruction-v1"
"""按需指令版本（P11-05）：指令文本模板/标记/接地口径变更都要换版本号。"""

SKILL_TELEMETRY_VERSION = "qa-skill-telemetry-v1"
"""效果遥测版本（P11-06）：成功率/延迟/成本/证据产出的口径变更都要换版本号。"""

SKILL_IDS: Tuple[str, ...] = (
    "bm25_search", "semantic_search", "graph_traversal", "sql_query", "emr_search",
    "web_search", "causal_reasoning", "clinical_evidence", "contradiction_resolution",
    "citation_verification", "patient_inquiry",
)
"""§8 的 11 个技能标识（**逐字**取自目录树）：前 6 个是检索型，后 5 个是推理/交互型。

医疗语义技能（`emr_search` / `clinical_evidence` / `patient_inquiry`）按 D-002 的中性映射
在本仓库落地为"内部证据库检索 / 领域知识核验 / 用户澄清"；**取值逐字保留**，映射只写在
`qa_skills.DOMAIN_MAPPING_NOTE` 里（与 Phase 09 的 `MEMORY_SCOPES` 同一处置口径）。"""

SKILL_KINDS: Tuple[str, ...] = ("retrieval", "reasoning")
"""两类技能：`retrieval` 会产出证据（但**只有 Phase 03 核验过的才算已验证证据**）；
`reasoning` 只产出"怎么做"的指令，恒不产证据。"""

SKILL_COST_CLASSES: Tuple[str, ...] = ("free", "cheap", "moderate", "expensive")
"""成本档四值（§8 的 `cost_class`）：`free` = 纯本地计算/组装，不花检索或模型预算。"""

SKILL_LATENCY_CLASSES: Tuple[str, ...] = ("instant", "fast", "normal", "slow")
"""延迟档四值（§8 的 `latency_class`）：`instant` < 100ms、`fast` < 1s、`normal` < 8s、`slow` ≥ 8s。"""

SKILL_PERMISSIONS: Tuple[str, ...] = (
    "corpus_read", "graph_read", "db_read", "emr_read", "web_access", "patient_contact",
)
"""权限位六值（§8 的 `permissions`）：本仓库的落地映射见 `qa_skills.PERMISSION_NOTE`。

Router 的权限闸门是**默认拒绝**：`permissions` 里出现调用方没被授予的位 → 该技能
`PERMISSION_DENIED`（§6 的 `ASK_PATIENT` 与 §21 的安全关键缺口都靠这条兜住）。"""

SKILL_STATUSES: Tuple[str, ...] = ("active", "deprecated", "disabled")
"""技能生命周期：`deprecated` 仍可被显式指名加载（但要记账），`disabled` 一律不加载。"""

SKILL_SOURCES: Tuple[str, ...] = ("builtin", "injected")
"""注册来源：`builtin` = 本仓库内置的 §8 目录；`injected` = 部署方注册的实现（可插拔）。"""

SKILL_ROUTE_BY_ID: Mapping[str, str] = {
    "bm25_search": QA_ROUTE_KEYWORD,
    "semantic_search": QA_ROUTE_SEMANTIC,
    "graph_traversal": QA_ROUTE_GRAPH,
    "sql_query": QA_ROUTE_POLICY_EXACT,
    "emr_search": "",                 # 本部署没有 EMR 通道（D-002 中性映射）
    "web_search": QA_ROUTE_WEB,
    "causal_reasoning": "",           # 推理型：不检索
    "clinical_evidence": "",          # 推理型：不检索
    "contradiction_resolution": "",   # 推理型：不检索
    "citation_verification": "",      # 推理型：不检索
    "patient_inquiry": "",            # §6 的 ASK_PATIENT 不是检索通道（本仓库无该 route 值）
}
"""技能 → 检索通道（**空串 = 本部署没有这条通道**；取值只允许 `QA_RETRIEVAL_ROUTES` 或空）。

空串**不是**"未实现"的搪塞：它意味着"这个技能不通过检索通道干活"，所以它既不会被
记进 SearchTrace 的 route，也不会被当成"检索能力"统计。"""

SKILL_FOR_ROUTE: Mapping[str, str] = {
    QA_ROUTE_KEYWORD: "bm25_search",
    QA_ROUTE_PAGE_CONTEXT: "bm25_search",          # 与 `qa_gap_analyzer.HUNTER_BY_ROUTE` 同源
    QA_ROUTE_SEMANTIC: "semantic_search",
    QA_ROUTE_GRAPH: "graph_traversal",
    QA_ROUTE_GRAPH_ATTRIBUTE: "graph_traversal",   # 属性边也是图遍历
    QA_ROUTE_POLICY_EXACT: "sql_query",            # 政策登记表 = 结构化查询
    QA_ROUTE_WEB: "web_search",
}
"""检索通道 → 执行它的技能（Phase 07 的 `suggested_routes` 反查技能，**七个通道全覆盖**）。

为什么是反查而不是 `gap_type → skill` 新表：§13 的 "Gap → Best Retrieval Action" 在 Phase 07
已经落成 `GAP_ROUTE_RULES` + `routes_for()`。再写一张 gap→skill 表就会出现两个真源，
迟早打架（Phase 06/07 的教训）。"""

SKILL_GAP_TYPE_RULES: Mapping[str, Tuple[str, ...]] = {
    QA_GAP_CONTRADICTION: ("contradiction_resolution",),
    QA_GAP_AMBIGUOUS_ENTITY: ("patient_inquiry", "graph_traversal"),
    QA_GAP_MISSING_CAUSAL_BRIDGE: ("causal_reasoning",),
}
"""缺口类型 → **推理型**技能的补充规则（检索型一律走 `SKILL_FOR_ROUTE` 反查）。

为什么只有三条：其余七种缺口（缺证据/低相关/低权威/单源/缺实体链/缺时间链/缺反证）
都是"再去拿证据"的活，Phase 07 的 `GAP_ROUTE_RULES` 已经把通道定死；只有这三条
（矛盾消解、实体歧义、因果桥）是"换个方式想"而不是"再搜一次"。"""

SKILL_TASK_TYPE_RULES: Mapping[str, Tuple[str, ...]] = {
    "CAUSAL": ("causal_reasoning",),
    "MECHANISM": ("causal_reasoning",),
    "DIAGNOSTIC": ("clinical_evidence",),
    "COMPARISON": ("citation_verification",),
    "SYNTHESIS": ("citation_verification",),
    "MULTI_HOP": ("citation_verification",),
}
"""`task_type`（Phase 05 的 `QUERY_INTENTS` 值）→ 推理型技能（§8 的 "task_type + gap_type"）。"""

SKILL_SELECTION_REASONS: Tuple[str, ...] = (
    "GAP_ROUTE_MATCH", "GAP_TYPE_MATCH", "TASK_TYPE_MATCH", "CONTEXT_GAP_LOAD_SKILL",
    "MANDATORY_SKILL", "PERFORMANCE_BOOST", "MINIMAL_SET_DEDUPE",
    "PERMISSION_DENIED", "OVER_COST_BUDGET", "OVER_LATENCY_BUDGET", "OVER_SKILL_COUNT",
    "DISABLED", "DEPRECATED_SKIPPED", "LOW_SUCCESS_RATE", "NOT_TASK_RELEVANT",
    "NO_ROUTE_IN_DEPLOYMENT", "UNKNOWN_SKILL", "MINIMAL_SET_SATISFIED",
)
"""选择/淘汰理由码：**每一条决策必须带一个**，否则"为什么没加载这个技能"无法回答。

`MINIMAL_SET_DEDUPE` 是"最小必要集合"的核心证据：同一能力被多条缺口/多个任务同时需要时
只加载一次；`MINIMAL_SET_SATISFIED` 是"预算还有但没必要再加载"的正面理由。"""

SKILL_ROUTER_DECISIONS: Tuple[str, ...] = ("selected", "skipped")
"""Router 对每个候选技能的两个决策（与 Phase 08 的 `CONTEXT_DECISIONS` 同构）。"""

SKILL_LOAD_STAGES: Tuple[str, ...] = ("routed", "instruction_built", "included_in_pack", "executed")
"""一次技能加载的生命周期四步：路由选中 → 指令成形 → 真的进包 → 真的被执行。

为什么要把 `included_in_pack` 单列：被 Router 选中**不等于**生成端真的看得见
（它还要过 ContextUtility 与 token 预算）。遥测里这两个数分开记，才不会有"加载成功率"
这种把两件事混在一起的指标。"""

SKILL_LOAD_OUTCOMES: Tuple[str, ...] = ("ok", "degraded", "error", "skipped")
"""单次技能加载的结果四值（遥测的成功率分子分母口径见 `qa_skills.performance_summary`）。

`degraded` = 跑到了但降级（例如该通道无候选）；`error` = 抛错；两者都**不算成功**。"""

SKILL_TELEMETRY_REASONS: Tuple[str, ...] = (
    "INSUFFICIENT_SAMPLES", "NO_TASK_TYPE", "UNKNOWN_SKILL", "EMPTY_RECORDS",
)
"""遥测侧的诚实标记：样本不足时**不给成功率**（`INSUFFICIENT_SAMPLES`），而不是拿 1 次当 100%。"""

SKILL_HINT_POLICY = (
    "SKILL_HINT：技能指令**不是证据**（§8 + MASTER_RULES 11）——它只说明「该怎么做」，"
    "事实仍然只能来自 Phase 03 核验过的证据；指令条目恒 `is_evidence=False`、"
    "`requires_revalidation=True`、`in_citation_map=False`。"
)
"""技能指令的统一政策说明（**唯一定义在这里**，避免上下文包与技能模块各写一份而漂移）。

`qa_context_pack`（`skill_context` 段）与 `qa_skills`（指令条目 metadata / 回执）都从这里取。"""

SKILL_INTERACTION_SLOT_OWNER = "P14-07"
"""交互类技能的**归属声明**：`patient_inquiry` 是 §8 里唯一的"与用户交互"技能位。

它的取值**逐字保留**（§8 目录树），但"同一问题被问两次以上 → 判定为对上次答案不满意 →
主动给出可选思路（换通道/调时间窗/改权威要求/调证据门槛/换分析口径/保持原思路）并落成会话级
配置"这一整块能力是 **P14-07（重复提问识别与思路调整确认）** 的账。
Phase 11 **只声明这个技能位、不实现它的交互逻辑、也不填任何内容**（越界会被判 FAIL）。"""

SKILL_INTERACTION_SLOT = {
    "skill_id": "patient_inquiry",
    "owner_phase": SKILL_INTERACTION_SLOT_OWNER,
    "mounted": False,
    "capabilities_reserved": ("clarification",),
    "capabilities_not_implemented_here": ("repeat_question_detection",
                                          "strategy_change_confirmation"),
    "note": ("本阶段只把 `patient_inquiry` 当成一个「需要 patient_contact 权限、默认被拒」的"
             "技能位登记进注册表；交互流程（重复提问识别 → 给出可选思路 → 落会话级配置 →"
             "写进 SearchTrace）归 P14-07，Phase 11 不实现。"),
}
"""可挂载位（**预留，不填充**）：Phase 14 的 P14-07 直接挂到这里，不必改注册表结构。"""

DEFAULT_SKILL_MIN_SAMPLES = 3
"""给出成功率所需的最小样本数（`success_rate` 的分母下限）。

为什么是 3：成功率是给 Router 学习用的（§3.6 Skill Performance Memory），一次成功写 1.0
会让 Router 永久偏袒某个技能。低于门槛时遥测**如实返回 None + `INSUFFICIENT_SAMPLES`**，
Router 侧也就不会拿它当 `PERFORMANCE_BOOST` 的依据。"""

SKILL_SCHEMA = {
    "type": "object",
    "properties": {
        "skill_id": {"type": "string", "enum": list(SKILL_IDS)},
        "description": {"type": "string"},
        "kind": {"type": "string", "enum": list(SKILL_KINDS)},
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "preconditions": {"type": "array", "items": {"type": "string"}},
        "cost_class": {"type": "string", "enum": list(SKILL_COST_CLASSES)},
        "latency_class": {"type": "string", "enum": list(SKILL_LATENCY_CLASSES)},
        "permissions": {"type": "array",
                        "items": {"type": "string", "enum": list(SKILL_PERMISSIONS)}},
        "version": {"type": "string"},
        # 本仓库扩展（§8 之外的登记字段，全部可选）：通道落点/状态/来源/能力标签
        "route": {"type": "string", "enum": list(QA_RETRIEVAL_ROUTES) + [""]},
        "capabilities": {"type": "array", "items": {"type": "string"}},
        "status": {"type": "string", "enum": list(SKILL_STATUSES)},
        "source": {"type": "string", "enum": list(SKILL_SOURCES)},
        "produces_evidence": {"type": "boolean"},
        "instruction_template": {"type": "string"},
    },
    "required": ["skill_id", "description", "input_schema", "output_schema",
                 "preconditions", "cost_class", "latency_class", "permissions", "version"],
    "additionalProperties": True,
}
"""§8 的技能声明（九个字段**全部 required**）：少任何一个字段的技能都注册不进去。"""

SKILL_SELECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "router_version": {"type": "string"},
        "skill_id": {"type": "string", "enum": list(SKILL_IDS)},
        "decision": {"type": "string", "enum": list(SKILL_ROUTER_DECISIONS)},
        "reason": {"type": "string", "enum": list(SKILL_SELECTION_REASONS)},
        "need": {"type": "string"},
        "source": {"type": "string"},
        "cost_class": {"type": "string", "enum": list(SKILL_COST_CLASSES) + [""]},
        "latency_class": {"type": "string", "enum": list(SKILL_LATENCY_CLASSES) + [""]},
        "rank": {"type": "integer", "minimum": 0},
        "detail": {"type": "string"},
    },
    "required": ["router_version", "skill_id", "decision", "reason"],
    "additionalProperties": True,
}
"""选择 trace 单条（P11-03）：`reason` 必须来自 `SKILL_SELECTION_REASONS`，同输入同输出。"""

SKILL_ROUTING_SCHEMA = {
    "type": "object",
    "properties": {
        "router_version": {"type": "string"},
        "registry_version": {"type": "string"},
        "budget_version": {"type": "string"},
        "task_type": {"type": "string"},
        "needs": {"type": "array", "items": {"type": "object"}},
        "selected": {"type": "array", "items": {"type": "string", "enum": list(SKILL_IDS)}},
        "selected_detail": {"type": "array", "items": {"type": "object"}},
        "trace": {"type": "array", "items": SKILL_SELECTION_SCHEMA},
        "skipped": {"type": "object"},
        "budget": {"type": "object"},
        "permissions": {"type": "object"},
        "requires_retrieval": {"type": "boolean"},
        "stats": {"type": "object"},
    },
    "required": ["router_version", "selected", "trace", "budget"],
    "additionalProperties": True,
}
"""Skill Router 回执（P11-03 + P11-04）：最小必要集合 + 逐条理由 + 预算/权限闸门账面。"""

SKILL_INSTRUCTION_SCHEMA = {
    "type": "object",
    "properties": {
        "instruction_version": {"type": "string"},
        "skill_id": {"type": "string", "enum": list(SKILL_IDS)},
        "section": {"type": "string", "enum": ["skill_context"]},
        "text": {"type": "string"},
        "tokens": {"type": "integer", "minimum": 0},
        "is_evidence": {"type": "boolean"},
        "requires_revalidation": {"type": "boolean"},
        "in_citation_map": {"type": "boolean"},
        "preconditions_met": {"type": "boolean"},
        "route": {"type": "string", "enum": list(QA_RETRIEVAL_ROUTES) + [""]},
        "metadata": {"type": "object"},
    },
    "required": ["instruction_version", "skill_id", "text", "is_evidence",
                 "requires_revalidation", "in_citation_map"],
    "additionalProperties": True,
}
"""按需指令（P11-05）：`is_evidence` 恒 False、`requires_revalidation` 恒 True、
`in_citation_map` 恒 False —— **技能指令永远是提示，不是已验证证据**（MASTER_RULES 11）。"""

SKILL_BUDGET_SCHEMA = {
    "type": "object",
    "properties": {
        "budget_version": {"type": "string"},
        "max_skills": {"type": "integer", "minimum": 0},
        "max_total_cost_units": {"type": "number", "minimum": 0},
        "max_total_latency_ms": {"type": "number", "minimum": 0},
        "granted_permissions": {"type": "array",
                                "items": {"type": "string", "enum": list(SKILL_PERMISSIONS)}},
        "denied_permissions": {"type": "array",
                               "items": {"type": "string", "enum": list(SKILL_PERMISSIONS)}},
        "used_skills": {"type": "integer", "minimum": 0},
        "used_cost_units": {"type": "number", "minimum": 0},
        "used_latency_ms": {"type": "number", "minimum": 0},
        "exhausted": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["budget_version", "max_skills", "granted_permissions"],
    "additionalProperties": True,
}
"""技能预算与权限账面（P11-04）：三个上限（条数/成本/延迟）+ 权限授予/拒绝集合。"""

SKILL_LOAD_RECORD_SCHEMA = {
    "type": "object",
    "properties": {
        "telemetry_version": {"type": "string"},
        "record_id": {"type": "string"},
        "run_id": {"type": "string"},
        "skill_id": {"type": "string", "enum": list(SKILL_IDS)},
        "task_type": {"type": "string"},
        "stage_reached": {"type": "string", "enum": list(SKILL_LOAD_STAGES)},
        "outcome": {"type": "string", "enum": list(SKILL_LOAD_OUTCOMES)},
        "reason": {"type": "string"},
        "latency_ms": {"type": "number", "minimum": 0},
        "cost_units": {"type": "number", "minimum": 0},
        "evidence_yield": {"type": "integer", "minimum": 0},
        # Phase 03 对齐：取回的候选里有几条真的被判 SUPPORTED（"产出证据"与"产出**已验证**
        # 证据"是两件事，分开记才不会把"取回了 5 条"说成"验过了 5 条"）。
        "verified_yield": {"type": "integer", "minimum": 0},
        "attempt": {"type": "integer", "minimum": 1},
        "recorded_at": {"type": "string"},
    },
    "required": ["telemetry_version", "record_id", "skill_id", "stage_reached", "outcome"],
    "additionalProperties": True,
}
"""一次技能加载的遥测记录（P11-06）：落 `qa_stage_runs`（`node_kind='skill'`，**零迁移**）。"""

SKILL_PERFORMANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "telemetry_version": {"type": "string"},
        # `skill_id` **可空**：跨技能聚合行（`telemetry_receipt.overall`）不属于任何单个技能，
        # 如实给空串而不是硬塞一个；单技能行仍然必须是 §8 的合法取值。
        "skill_id": {"type": "string", "enum": list(SKILL_IDS) + [""]},
        "task_type": {"type": "string"},
        "attempts": {"type": "integer", "minimum": 0},
        "successes": {"type": "integer", "minimum": 0},
        "degraded": {"type": "integer", "minimum": 0},
        "errors": {"type": "integer", "minimum": 0},
        "success_rate": {"type": "number", "minimum": 0, "maximum": 1},
        "latency_ms": {"type": "object"},
        "cost_units": {"type": "number", "minimum": 0},
        "evidence_yield": {"type": "integer", "minimum": 0},
        "verified_yield": {"type": "integer", "minimum": 0},
        "reason": {"type": "string", "enum": list(SKILL_TELEMETRY_REASONS) + [""]},
        "min_samples": {"type": "integer", "minimum": 0},
    },
    "required": ["telemetry_version", "attempts", "successes", "success_rate"],
    "additionalProperties": True,
}
"""聚合后的技能表现（§3.6 的 `skill_id/task_type/success_rate/latency/cost/evidence_yield`）。

样本不足时 `qa_skills.performance_summary` 的 `success_rate` 返回 `None` 并带
`reason=INSUFFICIENT_SAMPLES` —— 这一条由**用例**钉住（不许把 1 次成功当 100%）。"""


# ── Phase 12（P12-01…P12-06）：Episodic / Failure / Strategy / Source / Skill memory ──
# 边界：**经验类记忆**（学习成功与失败的检索经验），不含事实类记忆（那是 P09/P10 的账）。
# 契约设计口径（全部逐字对齐 01_V2_ARCHITECTURE §1.4/§3/§10/§11/§12/§14 + MASTER_RULES 11/12/15）：
#   1. §3 的六类记忆逐字对号：`EPISODIC_RESEARCH`（§3.2 问题类型/成功拆解/关键 Gap/有效
#      检索路径/停止原因 → `EPISODE_SECTIONS` 五要素）、`FAILURE`（§3.3 无效 query/失败
#      route/false-positive source/语义漂移扩展 → `FAILURE_KINDS` 四值 + `FAILURE_BINDING_FIELDS`
#      的 corpus/config/version 绑定）、`STRATEGY`（§3.4 某类问题哪些检索组合更有效）、
#      `SOURCE`（§3.5 authority/domain/update_frequency/reliability/known limitations）、
#      `SKILL_PERFORMANCE`（§3.6 skill_id/task_type/success_rate/latency/cost/evidence_yield）。
#      `QUERY_PATTERN` 没有独立小节，但它由 `MEMORY_TYPE_OWNER_PHASE` 归属本阶段（D-034），
#      因此同样在本阶段产出（有效 query 形态 = 无效 query 的对偶）。
#   2. **谁能产出谁负责（单一真源）**：`MEMORY_PRODUCIBLE_TYPES_BY_PHASE` 是
#      `MEMORY_TYPE_OWNER_PHASE` 的**派生视图**（守门用例断言两者逐键一致），
#      `qa_memory.type_policy` 只读它 —— 写门不允许出现第二套类型判定。
#   3. **MASTER_RULES 11/12**：经验类记忆全部由**规则与统计**从 trace 归纳（零模型、零嵌入
#      端点），且恒为提示：`EXPERIENCE_GUIDANCE_SCHEMA.requires_revalidation` 恒 True、
#      `verified_evidence` 恒 False —— 经验永远进不了 citation_map。
#   4. **MASTER_RULES 15**：失败记忆必须绑定 corpus/检索配置/时间，**版本变化后允许重试**：
#      `RETRY_DECISIONS` + `RETRY_REASONS` 把"为什么这次允许重试"写成闭集合。
EXPERIENCE_MEMORY_VERSION = "qa-experience-memory-v1"
"""Phase 12 经验类记忆的总口径版本（类型/归纳规则/门/指导/重试的总版本）。"""

EPISODE_MEMORY_VERSION = "qa-episode-summary-v1"
"""剧集归纳口径版本（§3.2 的五要素与"只总结 trace、不制造事实"的实现变更要换这个号）。"""

FAILURE_MEMORY_VERSION = "qa-failure-memory-v1"
"""失败记忆口径版本（四类失败的判定规则变更要换这个号）。"""

STRATEGY_MEMORY_VERSION = "qa-strategy-memory-v1"
"""策略记忆口径版本（检索组合聚合与成功的判据变更要换这个号）。"""

SOURCE_MEMORY_VERSION = "qa-source-reliability-v1"
"""来源可靠性口径版本（§3.5 六因子的权重变更要换这个号）。"""

SKILL_PERFORMANCE_MEMORY_VERSION = "qa-skill-performance-memory-v1"
"""技能表现记忆口径版本（**成功率口径不在本阶段重定义**：直接引 Phase 11 的
`SKILL_TELEMETRY_VERSION` 与 `qa_skills.performance_summary`，见 D-042）。"""

RETRY_POLICY_VERSION = "qa-version-aware-retry-v1"
"""版本感知重试口径版本（MASTER_RULES 15 的落地变更要换这个号）。"""

EXPERIENCE_GUIDANCE_VERSION = "qa-experience-guidance-v1"
"""经验指导（下一轮通道路由偏好 + 提前提示）口径版本：它**改变后续行为**，必须可复算。"""

MEMORY_PRODUCIBLE_TYPES_BY_PHASE = {
    "P09": ("VERIFIED_CLAIM", "ENTITY"),
    "P12": ("EPISODIC_RESEARCH", "STRATEGY", "FAILURE", "QUERY_PATTERN", "SOURCE",
            "SKILL_PERFORMANCE"),
    "P15": ("USER_APPROVED_DOMAIN_RULE",),
}
"""**每类记忆由哪个 Phase 产出** —— `MEMORY_TYPE_OWNER_PHASE` 的派生视图（单一真源的镜像）。

Phase 12 落地后，`MEMORY_TYPE_OWNER_PHASE` 里标 `P12` 的六类**不再被写门拒收**，
而是由 `qa_memory_experience` 依据 trace 产出（`TYPE_DEFERRED_TO_PHASE_12` 只在
产出方 Phase 未激活时出现，见 `MEMORY_PRODUCER_PHASES_ENV`）。

**唯一被排除的是 `MEMORY_ITEM`**：它是"记忆条目"这个节点本身（§1.4 的第一个节点类型），
不是可写的内容类型 —— 它仍在 `MEMORY_TYPE_OWNER_PHASE` 里（owner=P09），
但**永远不可产出**（写门对它给 `TYPE_NOT_SUPPORTED`，与 Phase 09 行为逐字相同）。
守门用例断言：`set(本表所有取值) ∪ {"MEMORY_ITEM"} == set(MEMORY_TYPE_OWNER_PHASE)`
且本表里的每个取值与 `MEMORY_TYPE_OWNER_PHASE` **同值**（多一个键、少一个键都挂）。"""

MEMORY_PRODUCER_PHASES_ENV = "QA_MEMORY_PRODUCER_PHASES"
"""激活的产出方 Phase 列表（逗号分隔；缺省 = `EXPERIENCE_PRODUCER_PHASES`）。

口径：写门只让"已经落地的产出方"产它的类型。默认值随 Phase 推进而增长，
但**类型 → 产出方**的映射始终只有 `MEMORY_TYPE_OWNER_PHASE` 一份。"""

EXPERIENCE_PRODUCER_PHASES: Tuple[str, ...] = ("P09", "P12")
"""缺省激活的产出方：P09（事实类两类，Phase 09 已落地）+ P12（经验类六类，本阶段落地）。
`P15` 未落地，所以 `USER_APPROVED_DOMAIN_RULE` 仍然被拒收并记 `TYPE_DEFERRED_TO_PHASE_15`。"""

EPISODE_SECTIONS: Tuple[str, ...] = (
    "task_type", "decomposition", "key_gaps", "effective_paths", "stop_reason",
)
"""§3.2 Episodic Research Memory 的五个要素**逐字**：问题类型 / 成功拆解 / 关键 Gap /
有效检索路径 / 停止原因。缺一个就有守例挂。"""

FAILURE_KINDS: Tuple[str, ...] = (
    "INVALID_QUERY", "FAILED_ROUTE", "FALSE_POSITIVE_SOURCE", "SEMANTIC_DRIFT",
)
"""§3.3 Failure Memory 的四类**逐字**：无效 query / 失败 route / false-positive source /
语义漂移扩展（本项目按 D-002 中性映射为"扩展词把检索带偏"）。"""

FAILURE_BINDING_FIELDS: Tuple[str, ...] = (
    "corpus_version", "retrieval_config_version", "route", "query_fingerprint", "observed_at",
)
"""§3.3 + MASTER_RULES 15 要求的**失败绑定**：corpus 版本 / 检索配置版本 / 通道 /
查询指纹 / 观测时间。**五件全空 = VERSION_UNBOUND**（重试策略一律放行，绝不凭空压制）。"""

RETRY_DECISIONS: Tuple[str, ...] = ("ALLOW_RETRY", "SUPPRESS")
"""版本感知重试的两个出口（MASTER_RULES 15）：版本没变才允许压制，变了必须允许重试。"""

RETRY_REASONS: Tuple[str, ...] = (
    "NO_FAILURE_RECORD", "SAME_ENVIRONMENT", "CORPUS_VERSION_CHANGED",
    "RETRIEVAL_CONFIG_CHANGED", "VERSION_UNBOUND", "FAILURE_AGED_OUT",
)
"""重试判定的理由码（闭集合，可复算"为什么这次允许/压制"）：
`NO_FAILURE_RECORD` 无失败记录；`SAME_ENVIRONMENT` 语料与配置都没变（唯一能压制的理由）；
`CORPUS_VERSION_CHANGED` / `RETRIEVAL_CONFIG_CHANGED` 环境变了（MASTER_RULES 15 要求放行）；
`VERSION_UNBOUND` 失败记录没有版本绑定（不许拿它压制）；`FAILURE_AGED_OUT` 超出时效。"""

SOURCE_RELIABILITY_BANDS: Tuple[str, ...] = ("HIGH", "MEDIUM", "LOW", "UNKNOWN")
"""§3.5 来源可靠性的四档（`UNKNOWN` 给"样本不足"的来源，不猜）。"""

SOURCE_RELIABILITY_FACTORS: Tuple[str, ...] = (
    "authority", "verified_yield", "rejection_rate", "recency", "corroboration", "volume",
)
"""§3.5 的六个可复算因子：权威性 / 被核验产出率 / 被拒率 / 时效 / 多来源印证 / 样本量。
全部来自真实证据行（零模型、零端点）。"""

EXPERIENCE_RECALL_TYPES: Tuple[str, ...] = (
    "STRATEGY", "FAILURE", "QUERY_PATTERN", "EPISODIC_RESEARCH",
)
"""规划期召回的类型（§10 "Planning Recall 在 Research Planner 前优先召回
Strategy/Failure/QueryPattern Memory"）：本阶段把 `EPISODIC_RESEARCH` 一并纳入 ——
剧集是策略与失败的原始素材，且同样不含事实断言（不会被当成事实污染规划）。"""

EXPERIENCE_TRACE_SECTIONS: Tuple[str, ...] = (
    "run", "retrieval", "verification", "gaps", "stop", "sources", "skills", "planning",
)
"""trace 的八个来源段（**每一段都可以缺席**，缺席的段名进 `missing_sections`，绝不编造）。
`run` = run 元信息；`retrieval` = 检索回执/证据产出；`verification` = Phase 03 核验分布；
`gaps` = Phase 07 缺口与下一跳；`stop` = 停止原因；`sources` = 证据来源；`skills` = Phase 11
技能遥测；`planning` = 计划与问题类型。"""

EXPERIENCE_MEMORY_SCHEMA = {
    "type": "object",
    "properties": {
        "experience_version": {"type": "string"},
        "trace_version": {"type": "string"},
        "run_id": {"type": "string"},
        "task_type": {"type": "string"},
        "corpus_version": {"type": "string"},
        "retrieval_config_version": {"type": "string"},
        "missing_sections": {"type": "array",
                             "items": {"type": "string", "enum": list(EXPERIENCE_TRACE_SECTIONS)}},
        "produced": {"type": "object"},
        "decisions": {"type": "array", "items": {"type": "object"}},
        "counts": {"type": "object"},
        "note": {"type": "string"},
    },
    "required": ["experience_version", "run_id", "produced", "decisions"],
    "additionalProperties": True,
}
"""经验归纳回执（P12-01…P12-05 的公共出口）：`produced` = 各类候选条数，
`decisions` = 写门逐条决策（含被拒的），`missing_sections` = trace 缺了哪几段。"""

EPISODE_MEMORY_SCHEMA = {
    "type": "object",
    "properties": {
        "episode_version": {"type": "string"},
        "run_id": {"type": "string"},
        "sections": {"type": "object"},
        "summary": {"type": "string"},
        "task_type": {"type": "string"},
        "effective_routes": {"type": "array", "items": {"type": "string"}},
        "key_gaps": {"type": "array", "items": {"type": "object"}},
        "stop_reason": {"type": "string", "enum": list(QA_STOP_REASONS) + [""]},
        "trace_fingerprint": {"type": "string"},
        "summarizer": {"type": "string"},
        "missing_sections": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["episode_version", "run_id", "sections", "summary", "summarizer"],
    "additionalProperties": True,
}
"""§3.2 剧集记忆：五要素齐全（`sections` 的键集必须等于 `EPISODE_SECTIONS`），
`stop_reason` 只吃 Phase 01 冻结的五值（**空串 = 这一轮没有停止原因**，如实空而不是编一个），
`summarizer` 记"谁写的"（默认 `rule:trace-v1`；LLM 归纳只留注入点且**默认不调用**）。"""

FAILURE_MEMORY_SCHEMA = {
    "type": "object",
    "properties": {
        "failure_version": {"type": "string"},
        "kind": {"type": "string", "enum": list(FAILURE_KINDS)},
        "route": {"type": "string"},
        "query": {"type": "string"},
        "query_fingerprint": {"type": "string"},
        "corpus_version": {"type": "string"},
        "retrieval_config_version": {"type": "string"},
        "binding_source": {"type": "string"},
        "observed_at": {"type": "string"},
        "task_type": {"type": "string"},
        "gap_type": {"type": "string"},
        "domain": {"type": "string"},
        "adopted": {"type": "integer", "minimum": 0},
        "rejected": {"type": "integer", "minimum": 0},
        "verified": {"type": "integer", "minimum": 0},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "detail": {"type": "string"},
    },
    "required": ["failure_version", "kind", "route", "corpus_version",
                 "retrieval_config_version", "observed_at"],
    "additionalProperties": True,
}
"""§3.3 失败记忆：四类 + 五个绑定字段（corpus/config/route/query 指纹/时间）。
`binding_source` 记"版本号是从哪儿读到的"（**不编版本**）。"""

STRATEGY_MEMORY_SCHEMA = {
    "type": "object",
    "properties": {
        "strategy_version": {"type": "string"},
        "task_type": {"type": "string"},
        "combination": {"type": "array", "items": {"type": "string"}},
        "observations": {"type": "integer", "minimum": 0},
        "successes": {"type": "integer", "minimum": 0},
        "success_rate": {"type": "number", "minimum": 0, "maximum": 1},
        "verified_yield": {"type": "integer", "minimum": 0},
        "min_samples": {"type": "integer", "minimum": 0},
        "run_ids": {"type": "array", "items": {"type": "string"}},
        "success_definition": {"type": "string"},
    },
    "required": ["strategy_version", "task_type", "combination", "observations", "successes",
                 "min_samples", "success_definition"],
    "additionalProperties": True,
}
"""§3.4 策略记忆：**某类问题的检索组合**（通道集合）在真实 run 上的成功率。
组合只吃 Phase 01 冻结的七个通道值（与 Phase 07 的 `suggested_routes` 同源）。"""

SOURCE_MEMORY_SCHEMA = {
    "type": "object",
    "properties": {
        "source_version": {"type": "string"},
        "domain": {"type": "string"},
        "authority_level": {"type": "integer"},
        "reliability": {"type": "number", "minimum": 0, "maximum": 1},
        "band": {"type": "string", "enum": list(SOURCE_RELIABILITY_BANDS)},
        "factors": {"type": "object"},
        "known_limitations": {"type": "array", "items": {"type": "string"}},
        "observations": {"type": "integer", "minimum": 0},
        "verified": {"type": "integer", "minimum": 0},
        "rejected": {"type": "integer", "minimum": 0},
        "update_frequency": {"type": "string"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["source_version", "domain", "reliability", "band", "factors", "observations"],
    "additionalProperties": True,
}
"""§3.5 来源记忆：authority/domain/update_frequency/reliability/known limitations，
六因子逐项可复算，样本不足一律 `UNKNOWN`。"""

SKILL_PERFORMANCE_MEMORY_SCHEMA = {
    "type": "object",
    "properties": {
        "skill_memory_version": {"type": "string"},
        "telemetry_version": {"type": "string"},
        "skill_id": {"type": "string", "enum": list(SKILL_IDS)},
        "task_type": {"type": "string"},
        "attempts": {"type": "integer", "minimum": 0},
        "successes": {"type": "integer", "minimum": 0},
        "success_rate": {"type": "number", "minimum": 0, "maximum": 1},
        "latency_ms": {"type": "object"},
        "cost_units": {"type": "number", "minimum": 0},
        "evidence_yield": {"type": "integer", "minimum": 0},
        "verified_yield": {"type": "integer", "minimum": 0},
        "min_samples": {"type": "integer", "minimum": 0},
        "success_definition": {"type": "string"},
        "source": {"type": "string"},
        "run_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["skill_memory_version", "skill_id", "attempts", "successes", "min_samples",
                 "success_definition", "source"],
    "additionalProperties": True,
}
"""§3.6 技能表现记忆：字段与 Phase 11 的 `SKILL_PERFORMANCE_SCHEMA` **同源**，
成功率口径**不重定义**（`success_definition` 直接抄 Phase 11 的那一句），
`source` 记数据从哪来（`qa_stage_runs:skill:<id>` 或离线重建）。"""

RETRY_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "retry_version": {"type": "string"},
        "failure_id": {"type": "string"},
        "kind": {"type": "string", "enum": list(FAILURE_KINDS) + [""]},
        "route": {"type": "string"},
        "decision": {"type": "string", "enum": list(RETRY_DECISIONS)},
        "reason": {"type": "string", "enum": list(RETRY_REASONS)},
        "corpus_version_recorded": {"type": "string"},
        "corpus_version_current": {"type": "string"},
        "config_version_recorded": {"type": "string"},
        "config_version_current": {"type": "string"},
        "age_days": {"type": "number", "minimum": 0},
        "max_age_days": {"type": "number", "minimum": 0},
    },
    "required": ["retry_version", "decision", "reason"],
    "additionalProperties": True,
}
"""版本感知重试判定（P12-06）：唯一能压制的是 `SAME_ENVIRONMENT`；
版本变了 / 没绑定 / 过期一律 `ALLOW_RETRY`（MASTER_RULES 15）。"""

EXPERIENCE_GUIDANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "guidance_version": {"type": "string"},
        "hint_version": {"type": "string"},
        "task_type": {"type": "string"},
        "corpus_version": {"type": "string"},
        "retrieval_config_version": {"type": "string"},
        "prefer_routes": {"type": "array", "items": {"type": "string"}},
        "avoid_routes": {"type": "array", "items": {"type": "string"}},
        "retry_decisions": {"type": "array", "items": RETRY_DECISION_SCHEMA},
        "warnings": {"type": "array", "items": {"type": "object"}},
        "route_changes": {"type": "array", "items": {"type": "object"}},
        "stats": {"type": "object"},
        "hint": {"type": "boolean"},
        "requires_revalidation": {"type": "boolean"},
        "verified_evidence": {"type": "boolean"},
    },
    "required": ["guidance_version", "prefer_routes", "avoid_routes", "warnings",
                 "requires_revalidation", "verified_evidence"],
    "additionalProperties": True,
}
"""经验指导（P12-02/P12-03/P12-06 的**行为出口**）：给下一轮的通道路由偏好 + 提前提示。

硬规则（MASTER_RULES 11/12）：`requires_revalidation` 恒 True、`verified_evidence` 恒 False ——
它是**提示**，不是证据，永远不进 citation_map。`avoid_routes` 里的通道一定是按 P12-06
判定为 `SUPPRESS` 的（版本一变就自动从里面消失）。"""


# ── Phase 13（P13-01…P13-06）：Answer Composer & Final Verifier ────────────────
# 边界（§16/§17）：**只用当前已验证证据回答**。
#   · §16 Answer Composer 的输入清单（Question / Verified Claims / Evidence Graph /
#     Unresolved Gaps / Contradictions / Citation Map）与输出形态
#     （`claim_id → evidence_id[] → source/chunk/span`）→ 落成 `ANSWER_COMPOSITION_SCHEMA`；
#   · §17 Final Verifier 的**六问**逐字入 `FINAL_VERIFIER_QUESTIONS`，六个检查码入
#     `FINAL_VERIFIER_CHECKS`；
#   · §17 "失败时只重跑 Composer 或只重跑相关 Verifier，不重跑整个 Research Graph"
#     → `FINAL_RETRY_SCOPES` 两值（**没有第三个作用域**，因为规格只给了这两个出口）；
#   · 口径单一真源：终验**不另造质量判断**——六问全部由 Phase 03 的核验结论
#     （`qa_verifier` 的 verdict/evidence_score）与 Phase 08 的生成端 grounding 回执
#     （`qa_context_pack.check_grounding` 的违规码）派生，派生关系写在每个 check 的
#     `derived_from` 里（机器可校验）；守门用例钉住"改 Phase 08 的回执 → 终验结论跟着变"。
#   · 冻结契约一个字不动：`FINAL_ANSWER_SCHEMA` 仍 `additionalProperties=False`，
#     本阶段只写它**已有**的字段（status/degraded/degradation_reasons/citations/citation_map）。
CLAIM_VERIFICATION_STATUSES: Tuple[str, ...] = (
    "unverified", "confirmed", "corrected", "qualified", "conflicted", "insufficient_evidence",
)
"""结论的核验状态六值 —— **冻结契约 `qa_contracts.CLAIM_SCHEMA.verification_status` 的镜像**。

它与证据层的 `EVIDENCE_STATUSES`（SUPPORTED/QUALIFIED/REFUTED/CONTEXT/UNVERIFIED）是**两套
词汇**：前者是"这条结论站不站得住"，后者是"这条证据支不支持"。Phase 03 的
`verify_claim_graph()` 产前者、`verify_evidence_item()` 产后者。本阶段为了不混用两套词，
在契约层显式给出这个镜像（同样由 `tests/test_qa_phase13_contracts.py` 逐值断言，不放宽）。"""

ANSWER_STATUSES: Tuple[str, ...] = ("ready", "partial", "insufficient_evidence")
"""答案形态三值——**冻结契约 `qa_contracts.FINAL_ANSWER_SCHEMA.status` 的镜像**（不改它）。
两处必须机器对齐，由 `tests/test_qa_phase13_contracts.py` 逐值断言（Phase 06 对
`CLAIM_EVIDENCE_RELATIONSHIPS` 用的是同一手法：镜像 + 守门，而不是让契约层互相 import
（`qa_contracts` 已经在模块顶部 import 本模块，反向 import 会成环）。"""

ANSWER_COMPOSER_VERSION = "qa-answer-composer-v1"
"""Answer Composer 组装口径版本（绑定规则/裁剪规则/引用编号口径变更都要换这个号）。"""

FINAL_VERIFIER_VERSION = "qa-final-verifier-v1"
"""Final Verifier 口径版本（六问的判定规则与派生来源变更要换这个号）。"""

FINAL_VERIFY_RETRY_VERSION = "qa-final-retry-v1"
"""局部重试口径版本（作用域判定表/上限/幂等判据变更要换这个号）。"""

INSUFFICIENT_EVIDENCE_VERSION = "qa-insufficient-evidence-v1"
"""证据不足输出口径版本（四条判据与输出形态变更要换这个号）。"""

FINAL_VERIFIER_CHECKS: Tuple[str, ...] = (
    "FACT_WITHOUT_EVIDENCE",       # §17-1
    "EVIDENCE_NOT_SUPPORTING",     # §17-2
    "OVER_INFERENCE",              # §17-3
    "COUNTER_EVIDENCE_MISSING",    # §17-4
    "CITATION_MISBOUND",           # §17-5
    "SPECULATION_AS_FACT",         # §17-6
)
"""§17 Final Verifier 的**六个检查码**（一个码对应一问，顺序与规格书逐字一致）。
`passed=False` 表示这一问没通过；每个码的**派生来源**写在该 check 的 `derived_from` 里，
绝不新造一套质量判断。"""

FINAL_VERIFIER_QUESTIONS = {
    "FACT_WITHOUT_EVIDENCE": "每个事实是否有证据？",
    "EVIDENCE_NOT_SUPPORTING": "证据是否真正支持？",
    "OVER_INFERENCE": "有没有过度推理？",
    "SPECULATION_AS_FACT": "有没有把推测写成事实？",
    "COUNTER_EVIDENCE_MISSING": "是否漏掉重要反证？",
    "CITATION_MISBOUND": "citation 是否正确？",
}
"""§17 六问原文（`FINAL_VERIFIER_CHECKS` 的顺序按"身份类→支持类→推理类→标注类→反证→引用"
给出，问号原文一字不改；两者的键集合恒相等，有守例钉着）。"""

FINAL_VERIFIER_OUTCOMES: Tuple[str, ...] = ("pass", "fail")
"""终验的两个结论值（`pass` 才允许原样交付；`fail` 必须走局部重试或证据不足输出）。"""

FINAL_RETRY_SCOPES: Tuple[str, ...] = ("composer", "verifier")
"""§17 的两个局部重试出口（**只有这两个**）：只重跑 Composer、或只重跑相关 Verifier。
两条都不重跑检索/证据图/整个 Research Graph（有用例断言本轮零检索、核验只覆盖受影响 claim）。"""

FINAL_RETRY_REASONS: Tuple[str, ...] = (
    "FIXED", "NO_CHANGE", "RETRY_LIMIT_REACHED", "NO_SCOPE", "SCOPE_UNAVAILABLE",
)
"""局部重试的五个去向码：`FIXED` 复验通过；`NO_CHANGE` 重跑后结论一字没变（幂等，不再重试）；
`RETRY_LIMIT_REACHED` 到上限；`NO_SCOPE` 没有失败码需要重试；`SCOPE_UNAVAILABLE` 作用域要用的
上游产物（核验结论/证据）本轮不存在，如实记账、绝不假装重试过。"""

FINAL_BINDING_REASONS: Tuple[str, ...] = (
    "BOUND_SUPPORTED", "BOUND_QUALIFIED", "PLAN_ONLY", "NO_EVIDENCE_REF",
    "NO_TRACEABLE_SPAN", "NOT_IN_ANSWER",
)
"""一条 claim 的绑定判定：前两条是绑上了（按 Phase 03 的 verdict 分档），
后四条是**没绑上**的具体原因（缺 ref / ref 不可回溯 / 计划态 claim / 不在答案里）。"""

INSUFFICIENT_EVIDENCE_REASONS: Tuple[str, ...] = (
    "NO_EVIDENCE", "NO_VERIFIED_CLAIM", "RESEARCH_NOT_ANSWERABLE", "GROUNDING_BLOCKED",
)
"""证据不足输出的四条判据（任一成立即降为 `status="insufficient_evidence"`）：
① 本轮一条证据都没有；② 没有任何**被 Phase 03 判 SUPPORTED 且绑得上可回溯 span** 的 claim；
③ Phase 07 的停止原因不是 `ANSWERABLE`（研究循环自己说了不可回答，单一真源）；
④ 终验仍有阻断级违规（局部重试也修不掉）。"""

INSUFFICIENT_EVIDENCE_TRIGGERS: Tuple[str, ...] = (
    "NO_EVIDENCE", "NO_VERIFIED_CLAIM", "GROUNDING_BLOCKED",
)
"""四条判据里**单独成立就足以**降档的三条（硬判据）。

`RESEARCH_NOT_ANSWERABLE` 刻意不在里面：它只说明"再检索也没增益/超预算/到深度上限"，
不代表手上**已有**的已验证证据不够（§14 的语义正是"带着不确定性交付"）。
它单独出现时按**告警**披露（写进 `degradation_reasons`），与硬判据同时出现时才一起进
`reason_codes`。这条分歧写死在这里，避免"多一个信号就悄悄把答案降档"。"""

ANSWER_BINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "composer_version": {"type": "string"},
        "claim_id": {"type": "string"},
        "text": {"type": "string"},
        "claim_status": {"type": "string", "enum": list(CLAIM_VERIFICATION_STATUSES) + [""]},
        "plan_only": {"type": "boolean"},
        "in_answer": {"type": "boolean"},
        "bound": {"type": "boolean"},
        "binding_reason": {"type": "string", "enum": list(FINAL_BINDING_REASONS)},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "evidence_ref": {"type": "string"},
                    "evidence_id": {"type": "string"},
                    "verdict": {"type": "string", "enum": list(EVIDENCE_STATUSES) + [""]},
                    "evidence_score": {"type": "number"},
                    "span": {"type": "object"},
                    "source_url": {"type": "string"},
                    "chunk_id": {"type": "string"},
                    "document_id": {"type": "string"},
                    "source_identity": {"type": "string"},
                    "grounded": {"type": "boolean"},
                    "label": {"type": "string"},
                },
                "required": ["evidence_ref", "verdict", "grounded"],
                "additionalProperties": True,
            },
        },
    },
    "required": ["claim_id", "bound", "binding_reason", "evidence"],
    "additionalProperties": True,
}
"""§16 的**单条事实声明绑定**：`claim_id → evidence_id[] → source/chunk/span`。
`verdict`/`evidence_score` 直接来自 Phase 03；`span` 直接来自 Phase 02 的最小 span；
`label` 是引用编号（与 Phase 08 的 `citation_index` 同口径）。"""

ANSWER_COMPOSITION_SCHEMA = {
    "type": "object",
    "properties": {
        "composer_version": {"type": "string"},
        "question": {"type": "string"},
        "status": {"type": "string", "enum": list(ANSWER_STATUSES)},
        "bindings": {"type": "array", "items": ANSWER_BINDING_SCHEMA},
        "verified_claims": {"type": "array", "items": {"type": "string"}},
        "unbound_claims": {"type": "array", "items": {"type": "string"}},
        "counter_evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim_id": {"type": "string"},
                    "evidence_ref": {"type": "string"},
                    "relation": {"type": "string", "enum": list(EVIDENCE_GRAPH_RELATIONSHIPS) + [""]},
                    "label": {"type": "string"},
                    "quote": {"type": "string"},
                    "cited_in_answer": {"type": "boolean"},
                },
                "required": ["claim_id", "evidence_ref", "cited_in_answer"],
                "additionalProperties": True,
            },
        },
        "unresolved_gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "gap_id": {"type": "string"},
                    "gap_type": {"type": "string", "enum": list(QA_GAP_TYPES) + [""]},
                    "claim_id": {"type": "string"},
                    "priority": {"type": "number"},
                    "band": {"type": "string", "enum": list(GAP_PRIORITY_BANDS) + [""]},
                    "severity": {"type": "string"},
                    "suggested_routes": {"type": "array",
                                         "items": {"type": "string",
                                                   "enum": list(QA_RETRIEVAL_ROUTES)}},
                    "mentioned_in_answer": {"type": "boolean"},
                },
                # `gap_type` 刻意**不进 required**：上游给了闭集合外的取值时，组装层把它记空
                # 并在 `inputs.unrecognized` 里计数（既不静默放行，也不因为一个陌生值把整份
                # 组装打爆）；字段本身仍只能是闭集合内的值加空串。
                "required": ["gap_id"],
                "additionalProperties": True,
            },
        },
        "contradictions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "conflict_id": {"type": "string"},
                    "claim_id": {"type": "string"},
                    "resolution": {"type": "string"},
                    "reason_code": {"type": "string"},
                    "winner": {"type": "string"},
                    "mentioned_in_answer": {"type": "boolean"},
                },
                "required": ["conflict_id"],
                "additionalProperties": True,
            },
        },
        "citation_map": {"type": "object"},
        "citation_index": {"type": "object"},
        "inputs": {"type": "object"},
        "reason_codes": {"type": "array", "items": {"type": "string"}},
        "note": {"type": "string"},
    },
    "required": ["composer_version", "bindings", "citation_map", "inputs"],
    "additionalProperties": True,
}
"""§16 Answer Composer 的产物：**事实声明绑定表** + 反证 + 未解决缺口 + 矛盾 + 引用索引。
它**不写正文**——正文由既有 `qa_synthesis` 生成端负责（本机硬约束禁止调任何模型），
Composer 只做"把已验证证据组装成可校验的绑定结构"以及"约束与校验"。"""

FINAL_VERIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "verifier_version": {"type": "string"},
        "composer_version": {"type": "string"},
        "outcome": {"type": "string", "enum": list(FINAL_VERIFIER_OUTCOMES)},
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "check_id": {"type": "string", "enum": list(FINAL_VERIFIER_CHECKS)},
                    "question": {"type": "string"},
                    "passed": {"type": "boolean"},
                    "severity": {"type": "string", "enum": ["blocking", "warning", "none"]},
                    "derived_from": {"type": "array", "items": {"type": "string"}},
                    "inputs": {"type": "object"},
                    "failures": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["check_id", "passed", "severity", "derived_from", "inputs"],
                "additionalProperties": True,
            },
        },
        "failed_checks": {"type": "array", "items": {"type": "string",
                                                     "enum": list(FINAL_VERIFIER_CHECKS)}},
        "blocking_codes": {"type": "array", "items": {"type": "string"}},
        "warning_codes": {"type": "array", "items": {"type": "string"}},
        "grounding": {"type": "object"},
        "stats": {"type": "object"},
        "note": {"type": "string"},
    },
    "required": ["verifier_version", "outcome", "checks", "failed_checks", "blocking_codes"],
    "additionalProperties": True,
}
"""§17 Final Verifier 的回执：六问逐项可复算（`inputs` 是判据的输入数字，
`derived_from` 是**上游单一真源**的标识：Phase 03 verdict / Phase 08 违规码）。"""

FINAL_RETRY_SCHEMA = {
    "type": "object",
    "properties": {
        "retry_version": {"type": "string"},
        "attempt": {"type": "integer", "minimum": 0},
        "max_attempts": {"type": "integer", "minimum": 0},
        "scope": {"type": "array", "items": {"type": "string", "enum": list(FINAL_RETRY_SCOPES)}},
        "scope_by_code": {"type": "object"},
        "reason": {"type": "string", "enum": list(FINAL_RETRY_REASONS) + [""]},
        "composer": {"type": "object"},
        "verifier": {"type": "object"},
        "changes": {"type": "array", "items": {"type": "object"}},
        "retrieval_calls": {"type": "integer", "minimum": 0},
        "stats": {"type": "object"},
    },
    "required": ["retry_version", "attempt", "max_attempts", "scope", "reason"],
    "additionalProperties": True,
}
"""§17 的局部重试回执：`scope` 只可能是 `composer`/`verifier` 的子集；
`retrieval_calls` 恒 0（不重跑 Research Graph 的机器可校验形态）。"""

INSUFFICIENT_EVIDENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "insufficient_version": {"type": "string"},
        "insufficient": {"type": "boolean"},
        "status": {"type": "string", "enum": list(ANSWER_STATUSES)},
        "reason_codes": {"type": "array",
                         "items": {"type": "string",
                                   "enum": list(INSUFFICIENT_EVIDENCE_REASONS)}},
        "inputs": {"type": "object"},
        "missing": {"type": "array", "items": {"type": "object"}},
        "searched": {"type": "object"},
        "text_block": {"type": "string"},
    },
    "required": ["insufficient_version", "insufficient", "status", "reason_codes", "inputs"],
    "additionalProperties": True,
}
"""P13-06 证据不足输出：四条判据（`reason_codes`）+ 判据输入（`inputs`，可复算）
+ 缺什么（`missing`，来自 Phase 07 缺口）+ 已经搜过什么（`searched`，来自停止原因与检索回执）。
答案形态只用冻结 schema 的**已有字段**（status/degraded/degradation_reasons）。"""


# ── Phase 14（P14-01…P14-07）：SAGE-RAG Unified Acquisition ───────────────────
# 边界：把"检索"与"问患者"统一成 **Evidence Acquisition**（§33 第 10 条工程原则 + §6/§13/§21/§22）。
# 契约设计口径（与前序阶段一致，逐字对齐 01_V2_ARCHITECTURE）：
#   1. §13/§21 的动作清单**逐字**入 `ACQUISITION_ACTIONS`（六值），缺一个就有守例挂；
#      §21/§22 的医疗侧三个权重因子入 `ACQUISITION_CLINICAL_FACTORS`（clinical_risk /
#      safety_weight / diagnostic_discrimination）。
#   2. **与既有阶段口径统一，不另造一套**：
#      · 动作的检索落点只吃 Phase 01 冻结的 `QA_RETRIEVAL_ROUTES`（`ACQUISITION_ACTION_ROUTES`），
#        与 Phase 04 的 `HUNTER_BY_ROUTE`、Phase 11 的 `SKILL_ROUTE_BY_ID` **同源**；
#      · 缺口类型/严重度/优先级只用 Phase 07 的 `GAP_SEVERITY` / `is_high_priority`（不重算）；
#      · "证据已在图上、只是没进上下文"用 Phase 08 的 Context Gap 与四个 Context Gap 动作；
#      · 安全关键缺口用 Phase 07/§21 的 `Priority Override` **同档**（`SAFETY_PRIORITY_FLOOR`）。
#   3. **MASTER_RULES 第 11 条**：问诊候选与交互文案都是**交互/提示**，恒不进 citation_map；
#      `PATIENT_INQUIRY_SCHEMA.is_evidence` 恒 False —— 患者回答是"用户陈述"，
#      要成为证据仍必须过 Phase 03 的核验（本阶段不改变这一点）。
#   4. **MASTER_RULES 第 13/17 条**：Context Gap 默认不触发新检索（P14-05 把这条写成闭集合）；
#      SafetyCritical=true 时安全规则覆盖 UserBurden（P14-06 的 `SAFETY_OVERRIDE`）。
ACQUISITION_ACTION_SCHEMA_VERSION = "qa-acquisition-action-v1"
"""动作候选 schema 版本（P14-01）：动作取值/因子/理由码增删都要换这个号。"""

ACQUISITION_UTILITY_VERSION = "qa-acquisition-utility-v1"
"""ExpectedGapReduction 与动作效用口径版本（P14-02）：公式或权重变更都要换这个号。"""

ACQUISITION_PLANNER_VERSION = "qa-acquisition-planner-v1"
"""动作选择（Planner）口径版本（P14-02/P14-03）：判据顺序与闸门变更都要换这个号。"""

ACQUISITION_ROUTES_VERSION = "qa-acquisition-routes-v1"
"""KB/EMR/DB/Web 路由落点口径版本（P14-03）：动作→通道/来源映射变更都要换这个号。"""

PATIENT_INQUIRY_VERSION = "qa-patient-inquiry-v1"
"""问诊候选与文案模板版本（P14-04）：模板/负担口径变更都要换这个号。"""

CONTEXT_GAP_NO_REPEAT_VERSION = "qa-context-gap-no-repeat-v1"
"""Context Gap 不重复追问口径版本（P14-05）：抑制规则变更都要换这个号。"""

CLINICAL_SAFETY_WEIGHTS_VERSION = "qa-clinical-safety-weights-v1"
"""临床/安全权重与 Safety Override 口径版本（P14-06）：权重或覆盖规则变更都要换这个号。"""

REALIGNMENT_VERSION = "qa-repeat-question-realignment-v1"
"""P14-07 总口径版本（触发判据 + 不满意依据 + 选项表 + 会话级配置）。"""

REALIGNMENT_DETECTOR_VERSION = "qa-repeat-question-detector-v1"
"""重复提问检测器版本（P14-07）：相似判据/阈值/窗口变更都要换这个号。"""

REALIGNMENT_OPTION_VERSION = "qa-realignment-options-v1"
"""思路调整选项表版本（P14-07）：选项集合、预期影响与代价文案变更都要换这个号。"""

ACQUISITION_ACTIONS: Tuple[str, ...] = (
    "retrieve_document", "retrieve_emr", "graph_traverse", "query_database",
    "web_search", "ask_patient",
)
"""§13/§21 的 Evidence Acquisition Action **逐字**六值（§16 的 Action Candidate）。
`ask_patient` 是唯一**交互型**动作（§6 的 ASK_PATIENT），其余五个都是检索型。"""

ACQUISITION_ACTION_KINDS: Tuple[str, ...] = ("retrieval", "interaction")
"""两类动作：`retrieval` 会去拿证据；`interaction` 只产出"该问谁什么"（本阶段 = ask_patient）。"""

ACQUISITION_INTERACTION_ACTIONS: Tuple[str, ...] = ("ask_patient",)
"""交互型动作集合（推导视图，守门用例断言与 `ACQUISITION_ACTION_KINDS` 机器对齐）。"""

ACQUISITION_SOURCES: Tuple[str, ...] = ("kb", "emr", "db", "graph", "web", "patient")
"""动作的**来源**六值（§33 的四路统一获取 + 图谱 + 患者）：KB/EMR/DB/Web 是 §8.6 的四条腿，
`graph` 是既有图谱通道，`patient` 是本阶段新增的交互来源。"""

ACQUISITION_ACTION_SOURCE: Mapping[str, str] = {
    "retrieve_document": "kb",
    "retrieve_emr": "emr",
    "graph_traverse": "graph",
    "query_database": "db",
    "web_search": "web",
    "ask_patient": "patient",
}
"""动作 → 来源（**单一真源**：P14-03 的路由表就是它的派生视图）。"""

ACQUISITION_ACTION_ROUTES: Mapping[str, str] = {
    # 取值只允许 Phase 01 冻结的七个通道或空串（空串 = 本部署没有这条检索通道）。
    "retrieve_document": QA_ROUTE_KEYWORD,
    "graph_traverse": QA_ROUTE_GRAPH,
    "query_database": QA_ROUTE_POLICY_EXACT,
    "web_search": QA_ROUTE_WEB,
    # EMR 在本部署按 D-002 的中性映射落成"内部证据库"（Phase 04 的 `structured_providers`
    # 注入点），它不是 §2 冻结的七个检索通道之一，所以**如实给空串**而不是硬塞一个通道值。
    "retrieve_emr": "",
    # §6 的 ASK_PATIENT 不是检索通道（Phase 11 的 `SKILL_ROUTE_BY_ID` 同一处置口径）。
    "ask_patient": "",
}
"""动作 → 检索通道（**空串 = 本部署没有这条通道**；与 Phase 04/11 的两张表同源）。"""

ACQUISITION_ACTION_HUNTERS: Mapping[str, Tuple[str, ...]] = {
    # 与 Phase 07 的 `HUNTER_BY_ROUTE` 逐键一致（复用 Phase 04 的 Hunter 身份）。
    "retrieve_document": ("bm25",),
    "graph_traverse": ("graph",),
    "query_database": ("structured",),
    "web_search": ("query_expansion",),
    "retrieve_emr": ("structured",),
    "ask_patient": (),
}
"""动作 → 执行它的 Hunter 身份（声明式；`ask_patient` 没有 Hunter —— 它不是检索）。"""

ACQUISITION_UTILITY_FACTORS: Tuple[str, ...] = (
    "expected_gap_reduction", "importance", "evidence_reliability", "latency", "cost", "user_burden",
)
"""§13/§21 的效用分子分母六项**逐字**：`Utility = EGR × Importance × Reliability / (Latency + Cost + UserBurden)`。"""

ACQUISITION_CLINICAL_FACTORS: Tuple[str, ...] = (
    "clinical_risk", "safety_weight", "diagnostic_discrimination",
)
"""§21/§22 的医疗侧三个权重因子**逐字**（`Importance` 的加分项）。"""

ACQUISITION_DECISIONS: Tuple[str, ...] = ("selected", "skipped")
"""动作选择的两个决策（与 Phase 08 的 `CONTEXT_DECISIONS`、Phase 11 的 Router 同构）。"""

ACQUISITION_SKIP_REASONS: Tuple[str, ...] = (
    "SOURCE_UNAVAILABLE", "NOT_APPLICABLE_TO_GAP", "PERMISSION_DENIED", "CONTEXT_GAP_NO_REPEAT",
    "PATIENT_QUESTION_ALREADY_ASKED", "LOWER_UTILITY", "DUPLICATE_ACTION", "OVER_USER_BURDEN",
    "UTILITY_FLOOR", "SAFETY_OVERRIDE",
)
"""动作淘汰理由码（闭集合）：**每一条淘汰必须带一个**，否则"为什么没走这条路"无法回答。

`CONTEXT_GAP_NO_REPEAT`（P14-05）与 `PATIENT_QUESTION_ALREADY_ASKED`（不重复追问）都指向
MASTER_RULES 第 13 条；`SAFETY_OVERRIDE`（P14-06）表示"按普通效用该降级，但安全规则把它抬回来"。
"""

CONTEXT_GAP_NO_REPEAT_REASONS: Tuple[str, ...] = (
    "EVIDENCE_ALREADY_IN_GRAPH", "EVIDENCE_ALREADY_IN_PACK", "SPAN_TRUNCATED_ONLY",
    "COUNTEREVIDENCE_ALREADY_IN_GRAPH", "CONFLICT_IS_ADJUDICATED",
)
"""P14-05 的抑制理由码（§6 + MASTER_RULES 13）：证据**已经在手上**，只是没进当前上下文。

每条都对应一个 Phase 08 的 Context Gap 动作（`REPACK_CONTEXT` / `EXPAND_EVIDENCE_SPAN` /
`LOAD_COUNTEREVIDENCE`）或一个已经裁决过的矛盾 —— 也就是说：**该重组上下文，不该再检索、
更不该再问患者**。
"""

SAFETY_PRIORITY_FLOOR = SAFETY_OVERRIDE_PRIORITY
"""§21/§22 的 `Priority Override` 档位（**直接引 Phase 07 的 `SAFETY_OVERRIDE_PRIORITY`**，
不另设阈值 —— 两个阶段对"安全关键"必须是同一个数字）。"""

SAFETY_WEIGHT_CEILING = 1.0
"""`safety_weight` 的上界（§22：SafetyCritical 时安全规则覆盖普通成本效用）。"""

ANALYSIS_LENSES: Tuple[str, ...] = ("event", "trend", "risk")
"""分析口径（P14-07 的"换分析口径（如事件 vs 趋势）"）：`event` = 事件/因果链，
`trend` = 趋势/对比，`risk` = 风险/反证。取值闭集合，映射到 Phase 07 的问题类别时见
`qa_realignment.LENS_CATEGORY`（**不新造类别**，只用 `qa_gap_analyzer` 认得的那几个键）。"""

REALIGNMENT_TRIGGERS: Tuple[str, ...] = ("LEXICAL_OVERLAP", "SAME_SUBJECT_SAME_INTENT")
"""P14-07 的两个（可同时命中的）触发判据：归一化词面重叠达阈值 **或** 同主体+同命题意图。"""

REALIGNMENT_DISSATISFACTION_REASONS: Tuple[str, ...] = (
    "NO_NEW_VERIFIED_EVIDENCE",          # ① 上次回答没有新增已核验证据（Phase 07 的 new_verified_claims）
    "ANSWER_DEGRADED",                   # ② 上次回答被标注降档/无证据（Phase 08/13 的 degradation_reasons）
    "STOP_REASON_NO_GAIN",               # ③ 上次停止原因 NO_GAIN
    "STOP_REASON_BUDGET_EXHAUSTED",      # ③ 上次停止原因 BUDGET_EXHAUSTED
    "STOP_REASON_MAX_DEPTH",             # ③ 上次停止原因 MAX_DEPTH
    "UNRESOLVED_GAP_RATE_HIGH",          # ④ 上次缺口未解决率高于阈值
)
"""P14-07 的"不满意信号"四类六值（**必须逐条落到回执里**，不许空口宣称用户不满意）。"""

REALIGNMENT_OPTION_IDS: Tuple[str, ...] = (
    "change_route", "widen_time_window", "narrow_time_window", "raise_authority",
    "loosen_evidence_threshold", "tighten_evidence_threshold", "change_analysis_lens",
    "keep_strategy",
)
"""P14-07 的选项闭集合：换通道 / 扩窗 / 收窗 / 提高权威要求 / 放宽门槛 / 收紧门槛 /
换分析口径 / **保持原思路**（最后一项是"可拒绝"的落地，缺它就不合规）。"""

REALIGNMENT_CONFIG_KEYS: Tuple[str, ...] = (
    "route_preference", "time_window", "authority_floor", "relevance_floor", "analysis_lens",
)
"""会话级**可执行配置**的键闭集合（P14-07）：用户的选择必须落成这五个键里的子集，
下一次同题运行据此**真的改变检索/缺口判据**（见 `qa_realignment.apply_session_config`）。"""

REALIGNMENT_DECISIONS: Tuple[str, ...] = ("CHANGE_STRATEGY", "KEEP_STRATEGY")
"""用户决定的二值。`KEEP_STRATEGY` 是**明确的用户决定**（不是沉默默认），落入 TTL 后不再追问。"""

REALIGNMENT_SKIP_REASONS: Tuple[str, ...] = (
    "NO_SESSION_CONTEXT", "NOT_A_REPEAT", "NO_DISSATISFACTION_EVIDENCE",
    "ALREADY_ASKED_IN_TTL", "KEEP_STRATEGY_DECLARED", "INTERACTION_LIMIT_REACHED", "ERROR",
)
"""不交互的理由码（闭集合）：**跳过必须可解释**（单轮调用、不同题、没依据、问过了、
已声明保持原思路、超次数上限、判定抛错）。"""

REALIGNMENT_DEFAULT_OVERLAP_THRESHOLD = 0.60
"""词面重叠阈值默认值（`QA_REPEAT_QUESTION_OVERLAP`）：归一化实词集合的 Jaccard。"""

REALIGNMENT_DEFAULT_WINDOW_TURNS = 5
"""回看窗口默认值（`QA_REPEAT_QUESTION_WINDOW`）：同一会话内最多回看最近几轮问题。"""

REALIGNMENT_DEFAULT_UNRESOLVED_RATE = 0.50
"""缺口未解决率阈值默认值（`QA_REPEAT_QUESTION_UNRESOLVED_RATE`）。"""

REALIGNMENT_DEFAULT_TTL_HOURS = 24
""""保持原思路"后的静默 TTL（小时，`QA_REPEAT_QUESTION_TTL_HOURS`）。"""

REALIGNMENT_MAX_INTERACTIONS = 1
"""同一会话同一议题的交互次数上限（默认 1：**幂等，重复触发只记一次**）。"""

REPEAT_QUESTION_SETTINGS_ENV: Tuple[str, ...] = (
    "QA_REPEAT_QUESTION_ENABLED", "QA_REPEAT_QUESTION_OVERLAP", "QA_REPEAT_QUESTION_WINDOW",
    "QA_REPEAT_QUESTION_UNRESOLVED_RATE", "QA_REPEAT_QUESTION_TTL_HOURS",
    "QA_REPEAT_QUESTION_MAX_INTERACTIONS", "QA_REPEAT_QUESTION_LLM_INTERPRETER",
)
"""P14-07 的全部配置旋钮（**可复算**：回执里逐项回写实际生效值）。"""

ACQUISITION_ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "action_version": {"type": "string", "enum": [ACQUISITION_ACTION_SCHEMA_VERSION]},
        "action_id": {"type": "string"},
        "action": {"type": "string", "enum": list(ACQUISITION_ACTIONS)},
        "kind": {"type": "string", "enum": list(ACQUISITION_ACTION_KINDS)},
        "source": {"type": "string", "enum": list(ACQUISITION_SOURCES)},
        "route": {"type": "string", "enum": list(QA_RETRIEVAL_ROUTES) + [""]},
        "hunters": {"type": "array", "items": {"type": "string", "enum": list(QA_HUNTER_IDS)}},
        "gap_id": {"type": "string"},
        "gap_type": {"type": "string", "enum": list(QA_GAP_TYPES) + [""]},
        "decision": {"type": "string", "enum": list(ACQUISITION_DECISIONS)},
        "skip_reason": {"type": "string", "enum": list(ACQUISITION_SKIP_REASONS) + [""]},
        "utility": {"type": "number", "minimum": 0},
        "factors": {"type": "object"},
        "clinical": {"type": "object"},
        "safety_critical": {"type": "boolean"},
        "rank": {"type": "integer", "minimum": 0},
        "detail": {"type": "string"},
    },
    "required": ["action_version", "action_id", "action", "kind", "source",
                 "decision", "gap_id", "utility", "factors"],
    "additionalProperties": True,
}
"""P14-01 的动作候选：**一条动作一个决策一个理由**，效用与因子全部可复算。

`route` **刻意不进 required**：EMR 与问诊动作在本部署**没有**检索通道（空串是如实取值，
不是缺字段）—— 与 `SKILL_ROUTE_BY_ID` 对空串的处置口径一致。"""

ACQUISITION_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "planner_version": {"type": "string", "enum": [ACQUISITION_PLANNER_VERSION]},
        "utility_version": {"type": "string", "enum": [ACQUISITION_UTILITY_VERSION]},
        "routes_version": {"type": "string", "enum": [ACQUISITION_ROUTES_VERSION]},
        "candidates": {"type": "array", "items": ACQUISITION_ACTION_SCHEMA},
        "selected": {"type": "array", "items": {"type": "string", "enum": list(ACQUISITION_ACTIONS)}},
        "by_source": {"type": "object"},
        "skipped": {"type": "object"},
        "sources": {"type": "object"},
        "safe_current_evidence": {"type": "object"},
        "settings": {"type": "object"},
        "stats": {"type": "object"},
    },
    "required": ["planner_version", "candidates", "selected", "sources"],
    "additionalProperties": True,
}
"""P14-02/P14-03 的获取计划回执（放 `stats` 的**兄弟键**，既有回执键集不动）。"""

PATIENT_INQUIRY_SCHEMA = {
    "type": "object",
    "properties": {
        "inquiry_version": {"type": "string", "enum": [PATIENT_INQUIRY_VERSION]},
        "inquiry_id": {"type": "string"},
        "slot_skill_id": {"type": "string", "enum": ["patient_inquiry"]},
        "skill_owner_phase": {"type": "string", "enum": [SKILL_INTERACTION_SLOT_OWNER]},
        "gap_id": {"type": "string"},
        "gap_type": {"type": "string", "enum": list(QA_GAP_TYPES) + [""]},
        "question_text": {"type": "string"},
        "question_fields": {"type": "array", "items": {"type": "string"}},
        "why": {"type": "string"},
        "user_burden": {"type": "string", "enum": ["none", "low", "medium", "high"]},
        "requires_permission": {"type": "string", "enum": list(SKILL_PERMISSIONS)},
        "permission_granted": {"type": "boolean"},
        # MASTER_RULES 11：问诊拿到的是"用户陈述"，**不是**已验证证据。
        "is_evidence": {"type": "boolean"},
        "requires_revalidation": {"type": "boolean"},
        "in_citation_map": {"type": "boolean"},
        "asked_before": {"type": "boolean"},
        "suppressed_by": {"type": "string", "enum": list(CONTEXT_GAP_NO_REPEAT_REASONS) + [""]},
    },
    "required": ["inquiry_version", "inquiry_id", "slot_skill_id", "question_text", "user_burden",
                 "is_evidence", "in_citation_map"],
    "additionalProperties": True,
}
"""P14-04 的问诊候选：规则模板拼装、**零模型调用**；恒 `is_evidence=False`、`in_citation_map=False`。"""

CONTEXT_GAP_NO_REPEAT_SCHEMA = {
    "type": "object",
    "properties": {
        "no_repeat_version": {"type": "string", "enum": [CONTEXT_GAP_NO_REPEAT_VERSION]},
        "gaps": {"type": "integer", "minimum": 0},
        "in_graph": {"type": "integer", "minimum": 0},
        "in_pack": {"type": "integer", "minimum": 0},
        "suppressed": {"type": "integer", "minimum": 0},
        "suppressed_by_reason": {"type": "object"},
        "suppressed_actions": {"type": "object"},
        "kept_actions": {"type": "object"},
        "context_actions": {"type": "array", "items": {"type": "string",
                                                       "enum": list(CONTEXT_GAP_ACTIONS)}},
    },
    "required": ["no_repeat_version", "gaps", "suppressed", "suppressed_by_reason"],
    "additionalProperties": True,
}
"""P14-05 的抑制回执：**几条动作因为"证据已在手上"被拦下**，逐条带理由码与对应的上下文动作。"""

REALIGNMENT_OPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "option_version": {"type": "string", "enum": [REALIGNMENT_OPTION_VERSION]},
        "option_id": {"type": "string", "enum": list(REALIGNMENT_OPTION_IDS)},
        "label": {"type": "string"},
        "config_keys": {"type": "array",
                        "items": {"type": "string", "enum": list(REALIGNMENT_CONFIG_KEYS)}},
        "expected_effect": {"type": "string"},
        "cost": {"type": "string"},
        "risk": {"type": "string"},
    },
    "required": ["option_version", "option_id", "label", "config_keys", "expected_effect", "cost"],
    "additionalProperties": True,
}
"""P14-07 的单个选项：**必须写清预期影响与代价**（否则用户无法判断该不该换思路）。"""

REALIGNMENT_INTERACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "realignment_version": {"type": "string", "enum": [REALIGNMENT_VERSION]},
        "detector_version": {"type": "string", "enum": [REALIGNMENT_DETECTOR_VERSION]},
        "topic_key": {"type": "string"},
        "scope_key": {"type": "string"},
        "previous_run_id": {"type": "string"},
        "interaction_index": {"type": "integer", "minimum": 1},
        "max_interactions": {"type": "integer", "minimum": 1},
        "triggers": {"type": "array", "items": {"type": "string",
                                                "enum": list(REALIGNMENT_TRIGGERS)}},
        "evidence": {"type": "array", "items": {"type": "string",
                                                "enum": list(REALIGNMENT_DISSATISFACTION_REASONS)}},
        "similarity": {"type": "object"},
        "question": {"type": "string"},
        "options": {"type": "array", "items": REALIGNMENT_OPTION_SCHEMA},
        "settings": {"type": "object"},
        "idempotent": {"type": "boolean"},
    },
    "required": ["realignment_version", "topic_key", "scope_key", "triggers", "evidence",
                 "options", "question"],
    "additionalProperties": True,
}
"""P14-07 的一次交互：触发判据 + 逐条不满意依据 + 具体选项表（**一次、给选项、可拒绝**）。"""

REALIGNMENT_CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "config_version": {"type": "string", "enum": [REALIGNMENT_VERSION]},
        "decision": {"type": "string", "enum": list(REALIGNMENT_DECISIONS)},
        "option_id": {"type": "string", "enum": list(REALIGNMENT_OPTION_IDS)},
        "topic_key": {"type": "string"},
        "scope_key": {"type": "string"},
        "session_id": {"type": "string"},
        "applied_keys": {"type": "array",
                         "items": {"type": "string", "enum": list(REALIGNMENT_CONFIG_KEYS)}},
        "config": {"type": "object"},
        "decided_by": {"type": "string"},
        "decided_at": {"type": "string"},
        "ttl_until": {"type": "string"},
    },
    "required": ["config_version", "decision", "option_id", "topic_key", "config"],
    "additionalProperties": True,
}
"""P14-07 的会话级可执行配置（**用户决定的落地形态**）：下一次同题运行据此真的换思路。"""

REALIGNMENT_RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "realignment_version": {"type": "string", "enum": [REALIGNMENT_VERSION]},
        "interaction": {"type": "object"},
        "config": {"type": "object"},
        "applied": {"type": "object"},
        "skipped_reason": {"type": "string", "enum": list(REALIGNMENT_SKIP_REASONS) + [""]},
        "skip_detail": {"type": "string"},
        "realignment_error": {"type": "string"},
        "settings": {"type": "object"},
        "stats": {"type": "object"},
    },
    "required": ["realignment_version", "settings"],
    "additionalProperties": True,
}
"""P14-07 的总回执（兄弟键）：交互、会话配置、**本次运行真的应用了什么**，失败时带
`realignment_error` 而**不阻断回答**。

`skipped_reason` **刻意不进 required**：真的发起交互时它就是空串（合法状态），
放进去会让"成功交互"被判成缺字段 —— 这类"空串是合法取值"的字段一律不进 required
（与 `ACQUISITION_ACTION_SCHEMA` 的 `route` 同一处置口径）。"""


# ── Phase 15（P15-01…P15-06）：记忆安全与作用域隔离 ─────────────────────────
# 本段的边界（一律按 D-002 的中性映射，医疗词只留在注释里）：
#   1. §14 的**记忆边界**：五个作用域 + "当前状态不得无条件跨轮次/跨会话当事实"。
#      P15-01 把"内容种类 × 作用域"变成可判定的规则（越界**收窄**，破坏作用域键完整性**拒收**）；
#      P15-02 把"轮次"变成作用域键的**显式一维**（`encounter_id`），并给出隔离回执；
#      P15-03 把"长期记忆使用时检查时间"落到 Phase 10 的**同一个**时效闸门上（不另造第二套）。
#   2. §21/§22 的**安全覆盖**：Phase 07 已把 `SafetyCritical` 抬到优先级下界、Phase 14 已把
#      安全动作按"效用比"换掉普通赢家。P15-04 只补**硬触发**那一档（危险信号**组合**）：
#      组合命中时忽略成本/延迟/追问负担，并把覆盖逐条留痕。
#   3. MASTER_RULES 18（日志不得泄露密钥与不必要的敏感信息）：P15-05 复用 `qa_security`
#      既有的密钥/令牌脱敏，补上**值级**的标识符最小化与"记忆正文只留指纹"的日志政策。
#   4. §15/§17 的**可 replay**：P15-06 给出"执行 → 证据 → 上下文 → 记忆"四层回放链，
#      审计事件落**既有** `qa_audit_events` 表（零迁移），回放比对同一份输入与同一份代码。
MEMORY_SAFETY_VERSION = "qa-memory-safety-v1"
"""记忆安全与作用域隔离的总口径版本（P15-01…P15-06 的公共锚点）。"""

SCOPE_ENFORCEMENT_VERSION = "qa-scope-enforcement-v1"
"""P15-01 作用域强制（规则表 + 收窄/拒收判定）的口径版本。"""

ENCOUNTER_ISOLATION_VERSION = "qa-encounter-isolation-v1"
"""P15-02 轮次隔离（作用域键的 encounter 维 + 隔离回执）的口径版本。"""

LONGITUDINAL_FRESHNESS_VERSION = "qa-longitudinal-freshness-v1"
"""P15-03 长期记忆时效（复用 P10 闸门 + 长期窗口）的口径版本。"""

SAFETY_CRITICAL_OVERRIDE_VERSION = "qa-safety-critical-override-v1"
"""P15-04 安全硬触发的口径版本。"""

PRIVACY_MINIMAL_LOG_VERSION = "qa-privacy-minimal-log-v1"
"""P15-05 日志最小化的口径版本。"""

MEMORY_AUDIT_REPLAY_VERSION = "qa-memory-audit-replay-v1"
"""P15-06 记忆审计与回放的口径版本。"""

SCOPE_DECISIONS: Tuple[str, ...] = ("ALLOW", "NARROW", "REJECT")
"""作用域判定的三个出口：**放行 / 收窄到更严作用域 / 拒收**。

刻意没有"放宽"这一档：作用域判定**只能不变或变严**（§14 的安全方向）。
"""

SCOPE_CONTENT_KINDS: Tuple[str, ...] = (
    "KNOWLEDGE", "RULE", "ENVIRONMENT", "SUBJECT_FACT", "CURRENT_STATE",
)
"""记忆**内容种类**（P15-01 判定的横轴；与作用域正交）：

`KNOWLEDGE`=跨主体通用知识（§14 "指南知识可为 GLOBAL_KNOWLEDGE"）；
`RULE`=本部署的组织规则（§14 "医院规则为 ORGANIZATION"）；
`ENVIRONMENT`=检索环境经验（策略/失败/查询形态/来源可靠性/技能表现，P12 六类）；
`SUBJECT_FACT`=关于某个主体的**非当前**事实（长期病史 → PATIENT_LONGITUDINAL）；
`CURRENT_STATE`=主体的**当前状态**（症状/生命体征/用药状态/实时指标 —— §14 硬规则的落点）。"""

SCOPE_BREADTH = {
    "GLOBAL_KNOWLEDGE": 4, "ORGANIZATION": 3, "PATIENT_LONGITUDINAL": 2,
    "ENCOUNTER": 1, "SESSION": 0,
}
"""五个作用域的**广度序**（越大越"通用"、越小越"贴身"）。

§14 的安全方向是**单调的**：把内容放进更窄的作用域永远安全（只是少复用），
放进更宽的作用域才可能污染（拿别的主体的当前状态当事实）。所以判定只需要
"内容种类允许的最大广度"这一个数，不需要逐对枚举。"""

CONTENT_KIND_MAX_BREADTH = {
    "KNOWLEDGE": 4,        # 通用知识可以进 GLOBAL_KNOWLEDGE（§14 明说）
    "RULE": 3,             # 组织规则最多到 ORGANIZATION（写进更窄的作用域只是少复用）
    "ENVIRONMENT": 3,      # 检索环境经验是**本部署**的知识，不得升格成通用知识
    "SUBJECT_FACT": 2,     # 关于某个主体的事实最多到主体长期（不得进 GLOBAL/ORGANIZATION）
    "CURRENT_STATE": 1,    # 当前状态最多到轮次（§14 硬规则：不得无条件跨轮次/跨会话）
}
"""每种内容种类**允许的最大作用域广度**（P15-01 的唯一判据来源）。"""

SCOPE_ALLOWED_CONTENT_KINDS = {
    scope: tuple(kind for kind, cap in CONTENT_KIND_MAX_BREADTH.items() if cap >= breadth)
    for scope, breadth in SCOPE_BREADTH.items()
}
"""由上面两张表**派生**的"作用域 → 允许的内容种类"视图（守门用例断言派生结果与之一致）。

派生而不是手写：两张表写死、视图算出来，就不存在"改了一张忘改另一张"的漂移
（与 Phase 12 的 `MEMORY_PRODUCIBLE_TYPES_BY_PHASE` 同一手法，见 D-044）。"""

SCOPE_ENFORCEMENT_REASONS: Tuple[str, ...] = (
    # 放行
    "SCOPE_AND_CONTENT_ALIGNED",
    # 放行但记账（组织绑定缺失：收窄到"本部署"而不是某个组织；不是跨主体泄漏）
    "ORGANIZATION_BINDING_DEGRADED",
    # 收窄（§14：当前状态不得无条件跨轮次/跨会话）
    "CURRENT_STATE_NARROWED_TO_ENCOUNTER",
    # 收窄（非当前状态类越界：维度允许内**收到该类允许的最宽作用域**，而不是丢数据）
    "CONTENT_KIND_NARROWED_TO_ALLOWED_SCOPE",
    # 拒收（作用域键完整性 / 内容与作用域不匹配）
    "SCOPE_MISSING", "SCOPE_UNKNOWN", "SCOPE_KEY_MISMATCH", "SCOPE_CONTENT_MISMATCH",
    "SUBJECT_SCOPE_NEEDS_SUBJECT", "SESSION_SCOPE_NEEDS_SESSION", "ENCOUNTER_SCOPE_NEEDS_ENCOUNTER",
)
"""作用域判定的理由码（闭集合，**每条都必须可达**，有守门用例逐条覆盖）。

`SCOPE_MISSING` 与 Phase 09 写门的同名理由码**同一个意思**（这里只是把它提前到判定层，
写门仍然保留自己的分支，两处不冲突：判定层给出"为什么"，写门给出"出口"）。"""

SCOPE_ENFORCEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "enforcement_version": {"type": "string", "enum": [SCOPE_ENFORCEMENT_VERSION]},
        "memory_id": {"type": "string"},
        "memory_type": {"type": "string", "enum": list(MEMORY_TYPES) + [""]},
        "scope": {"type": "string", "enum": list(MEMORY_SCOPES) + [""]},
        "scope_key": {"type": "string"},
        "expected_scope_key": {"type": "string"},
        "content_kind": {"type": "string", "enum": list(SCOPE_CONTENT_KINDS)},
        "decision": {"type": "string", "enum": list(SCOPE_DECISIONS)},
        "reason": {"type": "string", "enum": list(SCOPE_ENFORCEMENT_REASONS)},
        "narrowed_from": {"type": "string", "enum": list(MEMORY_SCOPES) + [""]},
        "narrowed_to": {"type": "string", "enum": list(MEMORY_SCOPES) + [""]},
        "binding": {"type": "object"},
        "allowed_kinds": {"type": "array", "items": {"type": "string",
                                                     "enum": list(SCOPE_CONTENT_KINDS)}},
        "basis": {"type": "object"},
    },
    "required": ["enforcement_version", "content_kind", "decision", "reason", "binding"],
    "additionalProperties": True,
}
"""P15-01 的单条判定：决策 + 理由码 + 内容种类 + 绑定证据（**逐项可复算**）。"""

ENCOUNTER_ISOLATION_REASONS: Tuple[str, ...] = (
    "SAME_ENCOUNTER", "SAME_SESSION", "SAME_SUBJECT", "SAME_ORGANIZATION", "GLOBAL",
    "OTHER_ENCOUNTER", "OTHER_SESSION", "OTHER_SUBJECT", "OTHER_ORGANIZATION",
)
"""一条记忆在本轮次上下文里"为什么可见/为什么被隔离"（闭集合）。

前五个是**可见**理由，后四个是**隔离**理由：轮次/会话不同一律隔离（§14 硬规则）；
主体长期记忆跨轮次**允许**（但要在使用时过 P15-03 的时效窗口）；组织规则跨轮次允许；
通用知识处处可见。"""

CROSS_ENCOUNTER_POLICIES: Tuple[str, ...] = ("BLOCK", "REVALIDATE_ON_USE", "ALLOW")
"""跨轮次使用一条记忆的三种处置：隔离 / 允许但要复验 / 直接允许。"""

ENCOUNTER_ISOLATION_SCHEMA = {
    "type": "object",
    "properties": {
        "isolation_version": {"type": "string", "enum": [ENCOUNTER_ISOLATION_VERSION]},
        "context": {"type": "object"},
        "visible_scope_keys": {"type": "array", "items": {"type": "string"}},
        "checked": {"type": "integer", "minimum": 0},
        "visible": {"type": "integer", "minimum": 0},
        "isolated": {"type": "integer", "minimum": 0},
        "isolated_by_reason": {"type": "object"},
        "isolated_refs": {"type": "array", "items": {"type": "object"}},
        "cross_encounter": {"type": "array", "items": {"type": "object"}},
        "leaks": {"type": "array", "items": {"type": "string"}},
        "settings": {"type": "object"},
    },
    "required": ["isolation_version", "checked", "visible", "isolated", "isolated_by_reason"],
    "additionalProperties": True,
}
"""P15-02 的隔离回执：**被隔离的是哪些**（只留引用与指纹，不留正文）+ 跨轮次放行的那些。"""

LONGITUDINAL_FRESHNESS_REASONS: Tuple[str, ...] = (
    "NOT_LONGITUDINAL",             # 非长期作用域：原样透传 P10 闸门结论
    "AGE_BEYOND_LONGITUDINAL_WINDOW",   # 长期记忆超窗口 → 复验
    "VALID_FROM_IN_FUTURE",         # 尚未生效（时间点还没到）→ 不可当当前事实用
    "GATE_UNAVAILABLE",             # P10 闸门不可用（模块缺席/抛错）→ 保守判复验并记账
)
"""P15-03 在 Phase 10 闸门之外**唯一**新增的这几条理由码（其余理由码一律原样来自闸门）。

这样"为什么这条长期记忆要复验"永远只有一个权威出口（P10 的 `reason`），
本阶段只补"长期"这一档的窗口、"尚未生效"这一条时间方向，以及"闸门不可用"的保守出口
（安全方向：拿不到闸门结论时判复验，绝不判 ALLOW）。"""

LONGITUDINAL_FRESHNESS_SCHEMA = {
    "type": "object",
    "properties": {
        "longitudinal_version": {"type": "string", "enum": [LONGITUDINAL_FRESHNESS_VERSION]},
        "memory_id": {"type": "string"},
        "scope": {"type": "string", "enum": list(MEMORY_SCOPES) + [""]},
        "longitudinal": {"type": "boolean"},
        "decision": {"type": "string", "enum": list(MEMORY_FRESHNESS_DECISIONS)},
        "gate_reason": {"type": "string", "enum": list(MEMORY_FRESHNESS_REASONS) + [""]},
        "reason": {"type": "string", "enum": list(LONGITUDINAL_FRESHNESS_REASONS)
                   + list(MEMORY_FRESHNESS_REASONS)},
        "age_days": {"type": "number"},
        "window_days": {"type": "number"},
        "valid_from": {"type": "string"},
        "gate": {"type": "object"},
        "settings": {"type": "object"},
    },
    "required": ["longitudinal_version", "decision", "reason", "longitudinal"],
    "additionalProperties": True,
}
"""P15-03 的单条结论：`decision` 三值**逐字复用** `MEMORY_FRESHNESS_DECISIONS`（不另造枚举），
`reason` 的取值域 = Phase 10 的 `MEMORY_FRESHNESS_REASONS` **并上**本阶段新增的三条。"""

SAFETY_TRIGGER_KINDS: Tuple[str, ...] = ("HARD_SAFETY_TRIGGER", "SOFT_SAFETY_TRIGGER", "NO_TRIGGER")
"""§22 的安全触发三档：**危险信号组合**（硬）/ 单个安全词（软，走 P14 的效用比覆盖）/ 无。"""

SAFETY_TRIGGER_REASONS: Tuple[str, ...] = (
    "DANGER_SIGN_COMBINATION", "SINGLE_SAFETY_TERM", "NO_SAFETY_TERM", "NO_SAFETY_ACTION",
    "BELOW_SAFETY_RATIO", "ALREADY_SAFETY_ACTION",
)
"""安全触发与覆盖的理由码（闭集合）：**硬触发必须能解释"是哪几个信号凑成的"**。"""

SAFETY_ENFORCEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "override_version": {"type": "string", "enum": [SAFETY_CRITICAL_OVERRIDE_VERSION]},
        "gap_id": {"type": "string"},
        "trigger": {"type": "string", "enum": list(SAFETY_TRIGGER_KINDS)},
        "reason": {"type": "string", "enum": list(SAFETY_TRIGGER_REASONS)},
        "danger_signs": {"type": "array", "items": {"type": "string"}},
        "sign_count": {"type": "integer", "minimum": 0},
        "min_signs": {"type": "integer", "minimum": 1},
        "ignored_cost_terms": {"type": "array", "items": {"type": "string"}},
        "winner_before": {"type": "string", "enum": list(ACQUISITION_ACTIONS) + [""]},
        "winner_after": {"type": "string", "enum": list(ACQUISITION_ACTIONS) + [""]},
        "overrode_utility": {"type": "boolean"},
        "basis": {"type": "object"},
    },
    "required": ["override_version", "trigger", "reason", "danger_signs", "sign_count", "min_signs"],
    "additionalProperties": True,
}
"""P15-04 的硬触发回执：**触发信号逐条列出**、覆盖前的赢家与覆盖后的赢家都可追溯。

`ignored_cost_terms` 就是 §22 那句"不能因为 UserBurden 高就降低追问优先级"的机器形态：
硬触发时把成本/延迟/负担三项显式列出来，说明**它们被忽略了**（而不是假装它们不存在）。"""

PRIVACY_MINIMAL_RULES: Tuple[str, ...] = (
    "SECRET_KEY",            # 键名本身敏感（复用 qa_security.SENSITIVE_KEYS）
    "BEARER_TOKEN",          # 值里出现 bearer/令牌形态
    "KEY_VALUE_SECRET",      # 值里出现 key=value 形态的密钥
    "EMAIL", "PHONE", "IDENTIFIER_RUN",   # 值级标识符（复用 qa_memory 的敏感串口径）
    "CONTENT_FINGERPRINTED", # 记忆/证据正文只留指纹与长度
    "TRUNCATED",             # 超长字段截断
)
"""P15-05 的最小化规则（闭集合）：每条都必须可单独验证（有守例逐条覆盖）。"""

PRIVACY_MINIMAL_LOG_SCHEMA = {
    "type": "object",
    "properties": {
        "policy_version": {"type": "string", "enum": [PRIVACY_MINIMAL_LOG_VERSION]},
        "rule_hits": {"type": "object"},
        "redacted_fields": {"type": "integer", "minimum": 0},
        "fingerprinted_fields": {"type": "integer", "minimum": 0},
        "leaks": {"type": "array", "items": {"type": "string"}},
        "content_policy": {"type": "string"},
        "purpose": {"type": "string"},
    },
    "required": ["policy_version", "rule_hits", "leaks", "content_policy"],
    "additionalProperties": True,
}
"""P15-05 的最小化回执：`leaks` **必须为空数组**才算通过（可复算、可断言）。"""

MEMORY_AUDIT_EVENT_KINDS: Tuple[str, ...] = (
    "SCOPE_ENFORCED", "ENCOUNTER_ISOLATED", "LONGITUDINAL_CHECKED",
    "SAFETY_CRITICAL_OVERRIDDEN", "LOG_MINIMIZED", "REPLAY_VERIFIED",
)
"""P15-06 落进**既有** `qa_audit_events` 表的六种事件（`event_type` 取值，闭集合）。"""

AUDIT_REPLAY_LAYERS: Tuple[str, ...] = ("execution", "evidence", "context", "memory")
"""可回放的四层链（§17 追加验收项："可 replay 任意回答：执行 → 证据 → 上下文 → 记忆"）。"""

AUDIT_REPLAY_SCHEMA = {
    "type": "object",
    "properties": {
        "replay_version": {"type": "string", "enum": [MEMORY_AUDIT_REPLAY_VERSION]},
        "run_id": {"type": "string"},
        "layers": {"type": "array", "items": {"type": "string", "enum": list(AUDIT_REPLAY_LAYERS)}},
        "counts": {"type": "object"},
        "hops": {"type": "array", "items": {"type": "object"}},
        "missing": {"type": "array", "items": {"type": "string"}},
        "replay_matches": {"type": "boolean"},
        "mismatches": {"type": "array", "items": {"type": "object"}},
        "redacted": {"type": "boolean"},
        "idempotent": {"type": "boolean"},
        "settings": {"type": "object"},
    },
    "required": ["replay_version", "run_id", "layers", "counts", "replay_matches", "redacted"],
    "additionalProperties": True,
}
"""P15-06 的回放链：四层各自的条数 + 层间引用是否闭环 + **重放是否逐字复现**。"""

# ── Phase 16（P16-01…P16-06）：可观测性 UI ─────────────────────────────────────
# 本段的边界（§15 可观测性 UI + §23 Search Trace + §32 Phase 10 的 UI 清单）：
#   1. **本阶段不产生任何新业务语义**：六个视图（执行图/证据图/上下文/记忆/检索轨迹/
#      成本延迟看板）都是**只读投影** —— 数据一律来自前序阶段已经落盘的产物
#      （`qa_stage_runs.details_json` 的阶段输出、`qa_reasoning_traces`、`memory_*`、
#      `qa_retrieval_cache`），UI 层**不另算一套**口径。
#   2. **每个数字都必须带出处**：契约里的 `numbers` 把"字段 → 值 + 来源 + 口径定义"
#      绑在一起，`source` 指向**拥有该口径的那个阶段**（不是"UI 算的"）。
#   3. **空态/降级态是合法结果**：开关关着、老 run 没有该阶段产物、库里一条 run 都没有
#      时，视图必须如实给 `empty`/`degraded`/`unavailable` 状态与理由，**绝不用 0 冒充**。
#   4. 只读硬约束：视图只暴露读接口，UI 不得改证据/结论/记忆（见 `OBSERVABILITY_READ_ONLY`）。
OBSERVABILITY_UI_VERSION = "qa-observability-ui-v1"
"""可观测性 UI 的总口径版本（P16-01…P16-06 的公共锚点；本阶段零迁移、零新表）。"""

OBSERVABILITY_VIEWS: Tuple[str, ...] = (
    "execution", "evidence", "context", "memory", "search_trace", "dashboard",
)
"""§15 要求的四图（Execution / Evidence / Context / Memory）+ §23 Search Trace +
§32 的 cost/latency/token/cache/skill dashboard，共六个视图。"""

OBSERVABILITY_STATES: Tuple[str, ...] = ("ok", "empty", "degraded", "unavailable")
"""视图的四种状态（闭集合）：

`ok`=有真实产物且口径完整；`empty`=口径可用但**确实没有对象**（例如开关关着、老 run 没有
该阶段产物）；`degraded`=有产物但带着降级/错误标记（例如阶段 `degraded`、视图内某一路
读失败）；`unavailable`=该视图在当前部署上**根本不可得**（例如抓不到库/表不存在）。
后三种都是**合法且必须如实呈现**的结果 —— 不允许用 0 或空表冒充 `ok`。"""

OBSERVABILITY_NUMBER_FIELDS: Tuple[str, ...] = ("field", "value", "source", "definition")
"""`numbers` 里每个数字的四个必填位：字段名 / 值 / **来源** / **口径定义**。

`source` 的形态固定为 `"<阶段>·<载体路径>"`（例如
`"P05·qa_stage_runs.details_json#result.stats.execution_graph"`），
`definition` 是中文口径说明 —— 这两项就是"界面展示的每个数字都能追到某个阶段的产物"
的机器可校验形态。"""

OBSERVABILITY_STATE_REASONS: Tuple[str, ...] = (
    "PRODUCT_PRESENT",              # ok：确实读到了该阶段的产物
    "SWITCH_OFF",                   # empty：该阶段的开关默认关（产物键根本不存在）
    "NO_STAGE_ARTIFACT",            # empty：老 run 没跑过该阶段 / 该阶段没留产物
    "NO_RUN_IN_SCOPE",              # empty：作用域内一条 run 都没有
    "NO_MEMORY_IN_SCOPE",           # empty：库里一条记忆都没有
    "NO_SKILL_TELEMETRY",           # empty：还没有技能遥测行
    "NO_CACHE_ENTRY",               # empty：缓存表为空
    "PHASE_DEGRADED",               # degraded：产物带着错误/降级标记
    "PARTIAL_READ",                 # degraded：某一路读失败，其余照常呈现
    "STORE_UNAVAILABLE",            # unavailable：拿不到库/表
)
"""视图状态理由码（闭集合）：**不允许"没有理由的空态"** —— 每个非 `ok` 状态都必须点名原因。"""

OBSERVABILITY_NUMBER_SCHEMA = {
    "type": "object",
    "properties": {
        "field": {"type": "string"},
        # 值可以是标量，也可以是分布映射（例如 by_status / trim_reasons 这类"计数表"）——
        # 契约故意不把 value 收成标量，否则 `_check_node` 会对它递归，把合法载荷误判失败。
        "value": {"type": ["integer", "number", "string", "boolean", "null",
                           "object", "array"]},
        "source": {"type": "string"},
        "definition": {"type": "string"},
    },
    "required": list(OBSERVABILITY_NUMBER_FIELDS),
    "additionalProperties": True,
}
"""单条口径数字（`numbers` 的 value 形状）。`source` / `definition` 都**不许为空串**。"""

OBSERVABILITY_VIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "view_version": {"type": "string", "enum": [OBSERVABILITY_UI_VERSION]},
        "view": {"type": "string", "enum": list(OBSERVABILITY_VIEWS)},
        "run_id": {"type": "string"},
        "state": {"type": "string", "enum": list(OBSERVABILITY_STATES)},
        "state_reason": {"type": "string", "enum": list(OBSERVABILITY_STATE_REASONS)},
        "state_text": {"type": "string"},
        "source": {"type": "string"},
        "notes": {"type": "array", "items": {"type": "string"}},
        "numbers": {"type": "object"},
        "payload": {"type": "object"},
        "generated_at": {"type": "string"},
        "read_only": {"type": "boolean"},
    },
    "required": ["view_version", "view", "run_id", "state", "state_reason", "source",
                 "numbers", "payload", "read_only"],
    "additionalProperties": True,
}
"""一个视图的完整载荷（六个视图共用同一张契约）：**状态 + 理由 + 来源 + 口径数字 + 正文**。

`read_only` 恒为 True（P16 的硬约束：UI 只读）。`state_reason` 取闭集合，`state_text` 是
给人看的中文说明 —— 即便是 `ok` 也要写清楚"数是从哪份产物上读的"。"""

OBSERVABILITY_READ_ONLY = True
"""P16 只读硬约束：可观测性视图**只暴露读路径**，不得通过 UI 改动证据/结论/记忆。

代码侧守门（`tests/test_qa_phase16_observability_ui.py`）：
  · 投影模块与 API 模块的 AST 里**没有** INSERT/UPDATE/DELETE 语句与写方法调用；
  · API 蓝图只注册 GET 方法；
  · 模板里只有 GET fetch，没有任何非 GET 的请求动词。"""

OBSERVABILITY_SOURCES: Tuple[str, ...] = (
    "P01·qa_reasoning_traces",
    "P02·qa_stage_runs.details_json#result.stats.evidence_layer",
    "P05·qa_stage_runs.details_json#result.stats.execution_graph",
    "P06·qa_stage_runs.details_json#result.evidence_graph",
    "P07·qa_stage_runs.details_json#result.stats.gap_loop",
    "P08·qa_stage_runs.details_json#result.context_pack",
    "P08·qa_events.payload.context_pack",
    "P09·memory_item/memory_recall_log",
    "P10·memory_validation/memory_contradiction",
    "P11·qa_stage_runs(stage=skill:<id>)/result.skill_telemetry",
    "P12·skill_performance_memory/source_reliability_memory",
    "P15·memory_item(scope/scope_key/decay_score)",
    "P00·qa_stage_runs(latency_ms/token_usage_json)",
    "P04·qa_retrieval_cache(namespace)",
    "P14·qa_stage_runs.details_json#result.stats.acquisition",
)
"""六个视图允许引用的**来源白名单**（每个来源点名"拥有该口径的阶段 + 载体"）。

守门用例断言：投影模块产出的每一条 `numbers[*].source` 都以这里某一项为前缀 ——
这样"UI 层另算一套"就不可能悄悄发生（新来源必须显式加进白名单）。"""


def describe() -> str:
    """给验收脚本/日志用的一行摘要（不参与业务逻辑）。"""
    return ("图谱契约 %s / 证据层 %s / 检索舰队 %s / 执行图 %s / 证据图 %s / 缺口分析 %s / "
            "技能注册表 %s / 技能路由 %s / 经验记忆 %s / 答案组装 %s / 终验 %s / 局部重试 %s / "
            "证据不足输出 %s / 获取动作 %s / 获取效用 %s / 问诊 %s / 上下文不重复追问 %s / "
            "临床安全权重 %s / 重复提问重对齐 %s / "
            "记忆安全 %s / 作用域强制 %s / 轮次隔离 %s / 长期时效 %s / "
            "安全硬触发 %s / 日志最小化 %s / 审计回放 %s / 可观测性 UI %s："
            "节点类型 %d / 边关系 %d / "
            "检索通道 %d / 失败策略 %d / 停止原因 %d / 证据状态 %d / Hunter %d / "
            "问题意图 %d / 执行节点类型 %d / 证据图节点类型 %d / 证据图关系 %d / 裁决理由码 %d / "
            "缺口类型 %d / 优先级分档 %d / Context 段 %d / ContextItem 种类 %d / Context Gap 动作 %d / "
            "记忆类型 %d / 记忆状态 %d / 记忆关系 %d / "
            "时效闸门出口 %d / 复验出口 %d / 记忆矛盾种类 %d / 撤销理由 %d / "
            "技能 %d / 技能种类 %d / 成本档 %d / 延迟档 %d / 权限位 %d / 选择理由码 %d / "
            "剧集要素 %d / 失败种类 %d / 重试出口 %d / 来源档 %d / "
            "终验检查 %d / 局部重试作用域 %d / 证据不足判据 %d / "
            "获取动作 %d / 获取来源 %d / 淘汰理由码 %d / 上下文抑制理由码 %d / "
            "重复提问触发判据 %d / 不满意依据 %d / 思路选项 %d / 会话配置键 %d / "
            "作用域决策 %d / 作用域理由码 %d / 内容种类 %d / 轮次隔离理由 %d / "
            "跨轮次策略 %d / 长期时效理由 %d / 安全触发档 %d / 安全触发理由 %d / "
            "日志最小化规则 %d / 审计事件种类 %d / 回放层 %d / "
            "可观测性视图 %d / 视图状态 %d / 视图状态理由 %d / 口径来源 %d"
            % (GRAPH_CONTRACT_VERSION, EVIDENCE_LAYER_VERSION, HUNTER_CONTRACT_VERSION,
               EXECUTION_GRAPH_VERSION, EVIDENCE_GRAPH_VERSION, GAP_ANALYZER_VERSION,
               SKILL_REGISTRY_VERSION, SKILL_ROUTER_VERSION, EXPERIENCE_MEMORY_VERSION,
               ANSWER_COMPOSER_VERSION, FINAL_VERIFIER_VERSION, FINAL_VERIFY_RETRY_VERSION,
               INSUFFICIENT_EVIDENCE_VERSION, ACQUISITION_ACTION_SCHEMA_VERSION,
               ACQUISITION_UTILITY_VERSION, PATIENT_INQUIRY_VERSION,
               CONTEXT_GAP_NO_REPEAT_VERSION, CLINICAL_SAFETY_WEIGHTS_VERSION,
               REALIGNMENT_VERSION,
               MEMORY_SAFETY_VERSION, SCOPE_ENFORCEMENT_VERSION, ENCOUNTER_ISOLATION_VERSION,
               LONGITUDINAL_FRESHNESS_VERSION, SAFETY_CRITICAL_OVERRIDE_VERSION,
               PRIVACY_MINIMAL_LOG_VERSION, MEMORY_AUDIT_REPLAY_VERSION,
               OBSERVABILITY_UI_VERSION,
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
               len(MEMORY_CONTRADICTION_KINDS), len(MEMORY_REVOKE_REASONS),
               len(SKILL_IDS), len(SKILL_KINDS), len(SKILL_COST_CLASSES),
               len(SKILL_LATENCY_CLASSES), len(SKILL_PERMISSIONS),
               len(SKILL_SELECTION_REASONS),
               len(EPISODE_SECTIONS), len(FAILURE_KINDS), len(RETRY_DECISIONS),
               len(SOURCE_RELIABILITY_BANDS),
               len(FINAL_VERIFIER_CHECKS), len(FINAL_RETRY_SCOPES),
               len(INSUFFICIENT_EVIDENCE_REASONS),
               len(ACQUISITION_ACTIONS), len(ACQUISITION_SOURCES),
               len(ACQUISITION_SKIP_REASONS), len(CONTEXT_GAP_NO_REPEAT_REASONS),
               len(REALIGNMENT_TRIGGERS), len(REALIGNMENT_DISSATISFACTION_REASONS),
                len(REALIGNMENT_OPTION_IDS), len(REALIGNMENT_CONFIG_KEYS),
                len(SCOPE_DECISIONS), len(SCOPE_ENFORCEMENT_REASONS),
                len(SCOPE_CONTENT_KINDS), len(ENCOUNTER_ISOLATION_REASONS),
                len(CROSS_ENCOUNTER_POLICIES), len(LONGITUDINAL_FRESHNESS_REASONS),
                len(SAFETY_TRIGGER_KINDS), len(SAFETY_TRIGGER_REASONS),
                len(PRIVACY_MINIMAL_RULES), len(MEMORY_AUDIT_EVENT_KINDS),
                len(AUDIT_REPLAY_LAYERS),
                len(OBSERVABILITY_VIEWS), len(OBSERVABILITY_STATES),
                len(OBSERVABILITY_STATE_REASONS), len(OBSERVABILITY_SOURCES)))


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
        # Phase 11（P11-01…P11-06）
        "skill": SKILL_SCHEMA,
        "skill_selection": SKILL_SELECTION_SCHEMA,
        "skill_routing": SKILL_ROUTING_SCHEMA,
        "skill_instruction": SKILL_INSTRUCTION_SCHEMA,
        "skill_budget": SKILL_BUDGET_SCHEMA,
        "skill_load_record": SKILL_LOAD_RECORD_SCHEMA,
        "skill_performance": SKILL_PERFORMANCE_SCHEMA,
        # Phase 12（P12-01…P12-06）
        "experience_memory": EXPERIENCE_MEMORY_SCHEMA,
        "episode_memory": EPISODE_MEMORY_SCHEMA,
        "failure_memory": FAILURE_MEMORY_SCHEMA,
        "strategy_memory": STRATEGY_MEMORY_SCHEMA,
        "source_memory": SOURCE_MEMORY_SCHEMA,
        "skill_performance_memory": SKILL_PERFORMANCE_MEMORY_SCHEMA,
        "retry_decision": RETRY_DECISION_SCHEMA,
        "experience_guidance": EXPERIENCE_GUIDANCE_SCHEMA,
        # Phase 13（P13-01…P13-06）
        "answer_binding": ANSWER_BINDING_SCHEMA,
        "answer_composition": ANSWER_COMPOSITION_SCHEMA,
        "final_verification": FINAL_VERIFICATION_SCHEMA,
        "final_retry": FINAL_RETRY_SCHEMA,
        "insufficient_evidence": INSUFFICIENT_EVIDENCE_SCHEMA,
        # Phase 14（P14-01…P14-07）
        "acquisition_action": ACQUISITION_ACTION_SCHEMA,
        "acquisition_plan": ACQUISITION_PLAN_SCHEMA,
        "patient_inquiry": PATIENT_INQUIRY_SCHEMA,
        "context_gap_no_repeat": CONTEXT_GAP_NO_REPEAT_SCHEMA,
        "realignment_option": REALIGNMENT_OPTION_SCHEMA,
        "realignment_interaction": REALIGNMENT_INTERACTION_SCHEMA,
        "realignment_config": REALIGNMENT_CONFIG_SCHEMA,
        "realignment_receipt": REALIGNMENT_RECEIPT_SCHEMA,
        # Phase 15（P15-01…P15-06）
        "scope_enforcement": SCOPE_ENFORCEMENT_SCHEMA,
        "encounter_isolation": ENCOUNTER_ISOLATION_SCHEMA,
        "longitudinal_freshness": LONGITUDINAL_FRESHNESS_SCHEMA,
        "safety_enforcement": SAFETY_ENFORCEMENT_SCHEMA,
        "privacy_minimal_log": PRIVACY_MINIMAL_LOG_SCHEMA,
        "audit_replay": AUDIT_REPLAY_SCHEMA,
        # Phase 16（P16-01…P16-06）
        "observability_view": OBSERVABILITY_VIEW_SCHEMA,
        "observability_number": OBSERVABILITY_NUMBER_SCHEMA,
    }
    schema = schemas.get(str(schema_name))
    if not schema:
        return False, "未知 schema：%s" % schema_name
    payload = payload if isinstance(payload, dict) else {}
    failure = _check_node(schema, payload, "")
    return (False, failure) if failure else (True, "ok")

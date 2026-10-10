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
"无缺失链接"会自然收束——对应 `BUDGET_EXHAUSTED` 与 `ANSWERABLE`；
`MAX_DEPTH`/`NO_GAIN`/`UNRESOLVABLE_CONTRADICTION` 待阶段 07 补齐。"""

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
"""执行图（§3.1）。`stop_reason` 取既有五值枚举（Phase 01 冻结），
本阶段只可能产出 {ANSWERABLE, BUDGET_EXHAUSTED, MAX_DEPTH}——另两值属 Phase 07 的缺口闭环。"""


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


def describe() -> str:
    """给验收脚本/日志用的一行摘要（不参与业务逻辑）。"""
    return ("图谱契约 %s / 证据层 %s / 检索舰队 %s / 执行图 %s / 证据图 %s：节点类型 %d / 边关系 %d / "
            "检索通道 %d / 失败策略 %d / 停止原因 %d / 证据状态 %d / Hunter %d / "
            "问题意图 %d / 执行节点类型 %d / 证据图节点类型 %d / 证据图关系 %d / 裁决理由码 %d"
            % (GRAPH_CONTRACT_VERSION, EVIDENCE_LAYER_VERSION, HUNTER_CONTRACT_VERSION,
               EXECUTION_GRAPH_VERSION, EVIDENCE_GRAPH_VERSION,
               len(KG_NODE_TYPES), len(KG_RELATION_KINDS),
               len(QA_RETRIEVAL_ROUTES), len(QA_FAILURE_POLICIES), len(QA_STOP_REASONS),
               len(EVIDENCE_STATUSES), len(QA_HUNTER_IDS),
               len(QUERY_INTENTS), len(EXECUTION_NODE_KINDS),
               len(EVIDENCE_GRAPH_NODE_TYPES), len(EVIDENCE_GRAPH_RELATIONSHIPS),
               len(CONTRADICTION_RESOLUTION_CODES)))


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
    }
    schema = schemas.get(str(schema_name))
    if not schema:
        return False, "未知 schema：%s" % schema_name
    payload = payload if isinstance(payload, dict) else {}
    failure = _check_node(schema, payload, "")
    return (False, failure) if failure else (True, "ok")

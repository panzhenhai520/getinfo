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

边界：本文件是纯常量 + schema，**不含业务逻辑**，不被任何写路径依赖。
"""

from __future__ import annotations

from typing import Tuple

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

EXECUTION_NODE_SCHEMA = {
    "type": "object",
    "properties": {
        "node_id": {"type": "string"},
        "node_kind": {"type": "string"},
        "parent_node_id": {"type": "string"},
        "failure_policy": {"type": "string", "enum": list(QA_FAILURE_POLICIES)},
        "stop_reason": {"type": "string", "enum": list(QA_STOP_REASONS) + [""]},
    },
    "required": ["node_id"],
    "additionalProperties": True,
}


def describe() -> str:
    """给验收脚本/日志用的一行摘要（不参与业务逻辑）。"""
    return ("图谱契约 %s：节点类型 %d / 边关系 %d / 检索通道 %d / 失败策略 %d / 停止原因 %d"
            % (GRAPH_CONTRACT_VERSION, len(KG_NODE_TYPES), len(KG_RELATION_KINDS),
               len(QA_RETRIEVAL_ROUTES), len(QA_FAILURE_POLICIES), len(QA_STOP_REASONS)))


def validate(schema_name: str, payload: dict) -> Tuple[bool, str]:
    """极简自校验（不引入 jsonschema 依赖）：只查 required 与几个 enum 字段。

    返回 (是否通过, 说明)。用于守门测试与运维自检；**不参与检索/生成链路**。
    """
    schemas = {
        "kg_node": KG_NODE_SCHEMA, "kg_edge": KG_EDGE_SCHEMA,
        "claim_evidence_edge": CLAIM_EVIDENCE_EDGE_SCHEMA,
        "search_trace": SEARCH_TRACE_SCHEMA, "execution_node": EXECUTION_NODE_SCHEMA,
    }
    schema = schemas.get(str(schema_name))
    if not schema:
        return False, "未知 schema：%s" % schema_name
    payload = payload if isinstance(payload, dict) else {}
    for key in schema.get("required") or []:
        if key not in payload or payload.get(key) in (None, ""):
            return False, "缺少必需字段：%s" % key
    for key, spec in (schema.get("properties") or {}).items():
        enum = spec.get("enum")
        if enum and key in payload and payload.get(key) not in enum:
            return False, "字段 %s 取值 %r 不在枚举内" % (key, payload.get(key))
    return True, "ok"

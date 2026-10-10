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


def describe() -> str:
    """给验收脚本/日志用的一行摘要（不参与业务逻辑）。"""
    return ("图谱契约 %s / 证据层 %s / 检索舰队 %s：节点类型 %d / 边关系 %d / 检索通道 %d / "
            "失败策略 %d / 停止原因 %d / 证据状态 %d / Hunter %d"
            % (GRAPH_CONTRACT_VERSION, EVIDENCE_LAYER_VERSION, HUNTER_CONTRACT_VERSION,
               len(KG_NODE_TYPES), len(KG_RELATION_KINDS), len(QA_RETRIEVAL_ROUTES),
               len(QA_FAILURE_POLICIES), len(QA_STOP_REASONS), len(EVIDENCE_STATUSES),
               len(QA_HUNTER_IDS)))


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
    }
    schema = schemas.get(str(schema_name))
    if not schema:
        return False, "未知 schema：%s" % schema_name
    payload = payload if isinstance(payload, dict) else {}
    failure = _check_node(schema, payload, "")
    return (False, failure) if failure else (True, "ok")

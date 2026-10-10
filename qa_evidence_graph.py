#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 06（P06-01…P06-04）· Evidence Graph 与 Contradiction（**纯规则，零模型调用**）。

通用包 01_V2_ARCHITECTURE 依据：
  · §3.2 Evidence Graph："表示我们现在知道什么：哪些 Claim 已经得到支持 / 哪些被反驳 /
    哪些证据相互冲突"；节点 Question/SubQuestion/Claim/Entity/Evidence/Source/Gap/Contradiction；
    推荐边 REQUIRES/SUPPORTS/REFUTES/MENTIONS/DERIVED_FROM/DEPENDS_ON/CONTRADICTS/RESOLVES；
  · §9："Chunk 可以很长，但真正进入 Evidence Graph 的应该是支持某个 Claim 的最小证据 Span"；
    §2.6："只有验证后的 Evidence 才进入 Evidence Graph"；
  · §15 Contradiction Agent："不要简单多数投票"，比较来源级别/发布时间/版本/样本人群/实体与
    定义一致性/证据独立性/是否存在更新版本；无法消解则保留 UNRESOLVED_CONTRADICTION；
  · MASTER_RULES 第 11 条：LLM 自由生成内容不能直接成为 Verified Evidence。

**本模块一行检索/核验逻辑都不重写**，只做"把既有结论升级成显式证据图"：
  复用 ① `qa_reasoning.build_claim_evidence_graph()` 产出的 `{claims, evidence, edges, conflicts}`
        （canonical claim、边结构、conflict_type 五值、裁决函数都在那里，本模块不改它）；
  复用 ② Phase 03 `qa_verifier` 的核验结论（claim 级 `verification.pairs[].verdict`、
        证据条目上的 `metadata.evidence_layer.verification.verdict`）——**图里的关系由 verdict 派生**，
        两处口径不可能打架（映射表见 `qa_graph_contracts.EVIDENCE_GRAPH_RELATION_BY_STATUS`）；
  复用 ③ Phase 02 `qa_evidence.source_identity()`（来源身份）与 `qa_verifier.term_set()`
        （分词口径），不另造一套；
  复用 ④ Phase 05 `qa_execution_graph.build_research_plan()` 的 dependency（DEPENDS 边的唯一来源）；
  复用 ⑤ `qa_storage.persist_reasoning_graph()` / `load_reasoning_graph()`（写入/读回，零迁移）；
  复用 ⑥ `qa_graph_contracts.CONFLICT_SCHEMA` 的 `resolution` 取值域（resolved/unresolved）——
        裁决细节走 Phase 06 自己的 `contradiction_decision` 契约，**不往冻结 schema 里塞字段**。

四条硬边界（诚实声明，宁写 PARTIAL 不谎报）
------------------------------------------
  1. **零模型调用**：矛盾裁决是本模块的规则实现（理由码见
     `qa_graph_contracts.CONTRADICTION_RESOLUTION_CODES`）。规格书 §29 说"Contradiction Resolver
     用 strong model（有条件）"——本仓库把它落成**可插拔注入点**
     （`register_contradiction_resolver` + `QA_CONTRADICTION_RESOLVER`），默认实现是规则；
     本轮不注册任何模型后端，模块内零 HTTP/socket/embedding 依赖（有 AST 级守门用例）。
     能力边界：规则只能裁"可比"的冲突（时间/权威/质量/独立性/强度），语义级（同一事实的
     不同表述、隐含矛盾）裁不了 → 落 `NO_DECISIVE_RULE`（unresolved），这正是 §15 要的保守行为。
  2. **未被核验的支持不算支持**：没有任何核验结论时，证据条目上的 `relationship=supports`
     只作为"声称的支持"记录（`edge.metadata.verification_basis="relationship"`、`verified=false`），
     claim coverage 的主口径只数**已核验**的支持（MASTER_RULES 第 11 条）。
  3. **DEPENDS 边只来自真实依赖**：Phase 05 计划里的 dependency 是唯一来源；图上没有
     依赖就不连线（§2.1 "B 不读 A 的结果就不许写成 A→B"）。计划 claim 以
     `metadata.plan_only=true` 进图（它们是"要证实什么"，不是结论），**不计入 coverage 分母**。
  4. **真相不覆盖历史**：本模块只读 `graph`，把结果挂在 `graph["evidence_graph"]` 这个**兄弟键**
     上（Phase 02 的 `stats["evidence_layer"]`、Phase 03 的 `stats["verification"]` 同样做法），
     既有键集与既有边结构一个字不改；矛盾裁决只回写冻结 `CONFLICT_SCHEMA` 允许的三个字段
     （resolution / rationale / rule_version）。
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping, Sequence

from qa_evidence import source_identity
from qa_graph_contracts import (
    CONTRADICTION_RESOLVED_CODES,
    CONTRADICTION_RESOLUTION_CODES,
    CONTRADICTION_RESOLVER_VERSION,
    CONTRADICTION_UNRESOLVED_CODES,
    EG_RELATION_CONTRADICTS,
    EG_RELATION_DEPENDS,
    EG_RELATION_MENTIONS,
    EG_RELATION_REFUTES,
    EG_RELATION_SUPPORTS,
    EVIDENCE_GRAPH_NODE_PREFIX,
    EVIDENCE_GRAPH_QUALIFIED_STATUSES,
    EVIDENCE_GRAPH_RELATION_BY_STATUS,
    EVIDENCE_GRAPH_RELATIONS_BY_KIND,
    EVIDENCE_GRAPH_VERSION,
    EVIDENCE_STATUS_BY_RELATIONSHIP,
    EVIDENCE_STATUS_QUALIFIED,
    EVIDENCE_STATUS_REFUTED,
    EVIDENCE_STATUS_SUPPORTED,
    EVIDENCE_STATUS_UNVERIFIED,
    EVIDENCE_STATUSES,
    RESOLUTION_AUTHORITY,
    RESOLUTION_INDEPENDENCE,
    RESOLUTION_METHOD_UNDECIDED,
    RESOLUTION_NEWER_VERSION,
    RESOLUTION_NO_DECISIVE_RULE,
    RESOLUTION_OPINION_UNDECIDED,
    RESOLUTION_QUALITY,
    RESOLUTION_SCOPE_DIFFERENCE,
    RESOLUTION_STRENGTH,
    validate,
)
from qa_verifier import term_set

COVERAGE_VERSION = "qa-claim-coverage-v1"
"""claim coverage 口径版本；换口径=换版本（coverage 数字必须能追到口径）。"""

COVERAGE_DEFINITION = (
    "主口径 claim_coverage = 有 ≥1 条**已核验 SUPPORTS 边**（verdict=SUPPORTED 且 "
    "verification_basis ∈ {verifier_pairs, evidence_layer_verification}）的非 planned claim 数 / "
    "非 planned claim 总数；"
    "weighted_claim_coverage = Σ_claim min(1, Σ 已核验 SUPPORTS 边强度) / 非 planned claim 总数"
    "（带证据权重的口径：单条 claim 由多条证据支持时不重复计数，强度取 §11 EvidenceScore 归一分）；"
    "claimed_claim_coverage = 把未被核验的 supports 关系（声称的支持）也算上，只作对照、不作结论；"
    "evidence_coverage = 有 ≥1 条 claim-evidence 边的 claim 数 / 总数（区分没有证据与证据不足）；"
    "refuted_claim_rate = 有 ≥1 条已核验 REFUTES 边且无已核验 SUPPORTS 边的 claim 数 / 总数。"
)
"""**口径逐字写死在这里**（P06-03 要求"明确口径"）：哪些算支持、分母是谁、权重怎么算。"""

QUALIFIED_FACTOR_DEFAULT = 0.6
"""QUALIFIED（带保留地支持）的关系是 SUPPORTS，但强度打这个折扣（避免与完全支持同权）。"""

DEFAULT_THRESHOLDS = {
    # 权威差阈值沿用 `qa_reasoning._adjudicate` 的既有口径（abs(auth)>=2）：
    # 目的是"裁决结果不比接线前更模糊"（接线前 resolved 的冲突，接线后仍然 resolved）。
    "authority_gap": 2,
    "quality_ratio": 2.0,          # 证据质量分（§11 EvidenceScore）领先倍数
    "independence_gap": 2,         # 独立来源数领先条数（§15 的"证据独立性"）
    "strength_ratio": 2.0,         # 支持/反驳强度比（兜底的强弱比较）
    "plan_match_min": 0.34,        # 计划 claim ↔ 结论 claim 的文本匹配阈值（只用于标注映射）
    "max_edges": 2000,             # 边数上限（防止病态输入把图炸掉）
}


# ── 开关与旋钮（全部可配置、可回滚；默认不改变既有行为）─────────────────────
def _env_flag(name: str, default: bool) -> bool:
    raw = str(os.environ.get(name, "")).strip().casefold()
    if not raw:
        return bool(default)
    return raw not in ("0", "false", "no", "off")


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return float(default)
    return min(max(value, low), high)


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(float(str(os.environ.get(name, "")).strip()))
    except (TypeError, ValueError):
        return int(default)
    return min(max(value, low), high)


def evidence_graph_enabled() -> bool:
    """证据图层开关。**默认关**：不打开即逐字回到既有行为（不产生新键、不额外读库）。"""
    return _env_flag("QA_EVIDENCE_GRAPH", False)


def coverage_version() -> str:
    return COVERAGE_VERSION


def qualify_factor() -> float:
    return _env_float("QA_EVIDENCE_GRAPH_QUALIFY_FACTOR", QUALIFIED_FACTOR_DEFAULT, 0.0, 1.0)


def thresholds() -> dict:
    return {
        "authority_gap": _env_int("QA_EVIDENCE_GRAPH_AUTHORITY_GAP",
                                  DEFAULT_THRESHOLDS["authority_gap"], 1, 100),
        "quality_ratio": _env_float("QA_EVIDENCE_GRAPH_QUALITY_RATIO",
                                    DEFAULT_THRESHOLDS["quality_ratio"], 1.0, 100.0),
        "independence_gap": _env_int("QA_EVIDENCE_GRAPH_INDEPENDENCE_GAP",
                                     DEFAULT_THRESHOLDS["independence_gap"], 1, 100),
        "strength_ratio": _env_float("QA_EVIDENCE_GRAPH_STRENGTH_RATIO",
                                     DEFAULT_THRESHOLDS["strength_ratio"], 1.0, 100.0),
        "plan_match_min": _env_float("QA_EVIDENCE_GRAPH_PLAN_MATCH_MIN",
                                     DEFAULT_THRESHOLDS["plan_match_min"], 0.0, 1.0),
        "max_edges": _env_int("QA_EVIDENCE_GRAPH_MAX_EDGES",
                              DEFAULT_THRESHOLDS["max_edges"], 1, 20000),
    }


# ── 小工具（确定性、可复算：所有 id 都是内容的哈希）─────────────────────────
def _digest(value, length: int = 16) -> str:
    body = str(value)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:length]


def _clamp(value, low: float = 0.0, high: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(low)
    if number != number:  # NaN
        return float(low)
    return min(max(number, low), high)


def _metadata(item: Mapping) -> dict:
    value = item.get("metadata") if isinstance(item, Mapping) else None
    return dict(value) if isinstance(value, Mapping) else {}


def _evidence_layer(item: Mapping) -> dict:
    layer = _metadata(item).get("evidence_layer")
    return dict(layer) if isinstance(layer, Mapping) else {}


def _pair_index(claim_node: Mapping) -> dict:
    """claim 级核验的逐对结论（Phase 03 `verify_claim_graph` 写进 `node["verification"]`）。"""
    verification = claim_node.get("verification") if isinstance(claim_node, Mapping) else None
    if not isinstance(verification, Mapping):
        return {}
    index = {}
    for pair in verification.get("pairs") or []:
        if isinstance(pair, Mapping) and str(pair.get("evidence_ref") or ""):
            index[str(pair["evidence_ref"])] = dict(pair)
    return index


def _date(value) -> datetime | None:
    raw = str(value or "").strip()[:10]
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _claim_id(claim_node: Mapping) -> str:
    claim = claim_node.get("claim") if isinstance(claim_node, Mapping) else None
    claim = claim if isinstance(claim, Mapping) else {}
    return str(claim_node.get("canonical_id") or claim.get("claim_id") or "")


def node_id(node_type: str, ref: str) -> str:
    """`claim:x` / `evidence:x` / `source:x` / `contradiction:x`（命名空间隔离）。"""
    prefix = EVIDENCE_GRAPH_NODE_PREFIX.get(str(node_type), "%s:" % node_type)
    return "%s%s" % (prefix, str(ref or ""))


def edge_id(kind: str, src: str, dst: str, relation: str) -> str:
    return "eg:%s" % _digest("|".join((str(kind), str(src), str(dst), str(relation))))


# ── P06-02：关系推导（核验 verdict 是唯一真源）───────────────────────────────
def relation_for_status(status: str) -> str:
    """证据/核验状态 → 图级关系；未知状态一律 `MENTIONS`（绝不当成支持）。"""
    return EVIDENCE_GRAPH_RELATION_BY_STATUS.get(str(status or "").upper(), EG_RELATION_MENTIONS)


def edge_status(claim_node: Mapping, evidence_ref: str, item: Mapping | None) -> dict:
    """一条 claim-evidence 边的状态与**它是怎么来的**（可复算、可审计）。

    **关系只认已核验结论**（§2.6"只有验证后的 Evidence 才进入 Evidence Graph"、
    MASTER_RULES 第 11 条）。优先级：
      1. claim 级核验 `verification.pairs[].verdict`（Phase 03，权威）；
      2. 证据条目 `metadata.evidence_layer.verification.verdict`（Phase 03，证据包上的核验）；
      3. 两者都没有 → `status=UNVERIFIED`（关系 `MENTIONS`），把"检索层声称的关系"
         （Phase 02 的 `metadata.evidence_layer.status` 或证据条目 `relationship`）记进
         `claimed_status`/`claimed_relationship`——**声称不等于已核验**，绝不当成支持，
         否则 claim coverage 会被未核验数据灌水。
    """
    pair = _pair_index(claim_node).get(str(evidence_ref))
    if pair and str(pair.get("verdict") or "").upper() in EVIDENCE_STATUSES:
        return {
            "status": str(pair["verdict"]).upper(),
            "verification_basis": "verifier_pairs",
            "verified": True,
            "score": _clamp(pair.get("score")),
            "reasons": list(pair.get("reasons") or [])[:10],
            "reason_text": str(pair.get("reason_text") or ""),
        }
    layer = _evidence_layer(item or {})
    verdict = str(((layer.get("verification") or {}) if isinstance(layer.get("verification"), Mapping)
                   else {}).get("verdict") or "").upper()
    if verdict in EVIDENCE_STATUSES:
        score = _clamp((layer.get("verification") or {}).get("score"))
        return {"status": verdict, "verification_basis": "evidence_layer_verification",
                "verified": True, "score": score, "reasons": [], "reason_text": ""}
    relationship = str((item or {}).get("relationship") or "").strip().casefold()
    claimed = str(layer.get("status") or "").upper()
    basis = "evidence_layer_status" if claimed in EVIDENCE_STATUSES else (
        "relationship" if relationship else "none")
    if claimed not in EVIDENCE_STATUSES:
        claimed = EVIDENCE_STATUS_BY_RELATIONSHIP.get(relationship, EVIDENCE_STATUS_UNVERIFIED)
    return {
        "status": EVIDENCE_STATUS_UNVERIFIED,
        "verification_basis": basis,
        "verified": False,
        "score": 0.0,
        "reasons": [],
        "reason_text": "",
        "claimed_status": claimed,
        "claimed_relationship": relationship,
    }


def edge_strength(status_info: Mapping) -> float:
    """边的强度（0..1）：取核验分（§11 EvidenceScore 归一），QUALIFIED 打折扣。

    没有任何核验分时给 0（不许拿检索原始分 `score`（可能上千）冒充概率）。
    """
    strength = _clamp(status_info.get("score"))
    if str(status_info.get("status") or "").upper() in EVIDENCE_GRAPH_QUALIFIED_STATUSES:
        strength *= qualify_factor()
    return round(strength, 4)


def claim_status_from_edges(edges: Sequence[Mapping]) -> str:
    """边集合 → claim 核验状态（与 `qa_verifier._claim_status` **逐条等价**，有守门用例）。

    这是"图里的关系不与核验层 verdict 打架"的第二道保险：同一批 verdict 既能算出
    `verification.status`，也能算出这里的状态，两者必须相等（`stats.status_consistency`）。
    """
    if not edges:
        return "insufficient_evidence"
    statuses = [str(item.get("status") or "").upper() for item in edges]
    if EVIDENCE_STATUS_REFUTED in statuses and EVIDENCE_STATUS_SUPPORTED not in statuses:
        return "conflicted"
    if EVIDENCE_STATUS_SUPPORTED in statuses:
        return "confirmed"
    if EVIDENCE_STATUS_QUALIFIED in statuses:
        return "qualified"
    return "unverified"


def _source_key(item: Mapping | None) -> str:
    if not isinstance(item, Mapping):
        return ""
    try:
        return str(source_identity(item).get("source_id") or "")
    except Exception:  # noqa: BLE001  来源身份失败绝不能把建图打断
        return str(item.get("source_url") or item.get("document_id") or item.get("evidence_ref") or "")


def _text_similarity(left: str, right: str) -> float:
    a, b = term_set(left), term_set(right)
    return len(a & b) / max(1, len(a | b))


CONTRADICTION_RESOLVER_VERSION_ALIAS = CONTRADICTION_RESOLVER_VERSION  # noqa: N816  兼容别名

_NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")
"""矛盾分型用的数字口径（与 `qa_reasoning._NUMBER_RE` 同源：数字不同 ≠ 谁对谁错）。"""


# ── P06-01 / P06-02：建图主入口 ─────────────────────────────────────────────
def build_layer(graph: Mapping, *, plan: Mapping | None = None, run_id: str = "",
                resolver: Callable | None = None) -> dict:
    """把 Phase 01/02/03 的结论图升级成**显式的证据图**（P06-01…P06-04 一次算完）。

    只读入参：`graph` 是 `build_claim_evidence_graph()`（可选经 `verify_claim_graph` 核验过）
    的产物；`plan` 是 Phase 05 的研究计划（DEPENDS 边的唯一来源）。
    返回值就是 `evidence_graph` 契约的载荷；同时**回写** `graph["conflicts"]` 里被本模块
    重新裁决的三字段（resolution / rationale / rule_version——冻结 CONFLICT_SCHEMA 允许的）。
    """
    graph_claims = [item for item in (graph.get("claims") or []) if isinstance(item, Mapping)]
    evidence_items = [item for item in (graph.get("evidence") or []) if isinstance(item, Mapping)]
    evidence_by_ref = {str(item.get("evidence_ref") or ""): item for item in evidence_items}

    nodes: list = []
    edges: list = []
    claim_rows: list = []
    edges_by_claim: dict = {}
    seen_node_ids: set = set()

    def add_node(node_type: str, ref: str, label: str = "", **extra) -> str:
        identifier = node_id(node_type, ref)
        if identifier not in seen_node_ids:
            seen_node_ids.add(identifier)
            nodes.append({"node_id": identifier, "node_type": str(node_type),
                          "label": str(label or "")[:300], "ref": str(ref or ""),
                          "metadata": dict(extra)})
        return identifier

    # 证据节点 + 来源节点（§3.2 的 Evidence/Source）
    for ref, item in evidence_by_ref.items():
        add_node("evidence", ref, str(item.get("title") or ref),
                 source_id=_source_key(item), published_at=str(item.get("published_at") or ""))
    source_seen: set = set()
    for ref, item in evidence_by_ref.items():
        key = _source_key(item)
        if key and key not in source_seen:
            source_seen.add(key)
            add_node("source", key, str(item.get("source_url") or key)[:300],
                     authority_level=item.get("authority_level"))

    limit = int(thresholds()["max_edges"])
    truncated = 0
    for claim_node in graph_claims:
        claim = claim_node.get("claim") if isinstance(claim_node.get("claim"), Mapping) else {}
        cid = _claim_id(claim_node)
        if not cid:
            continue
        add_node("claim", cid, str(claim.get("text") or "")[:300],
                 claim_type=str(claim.get("claim_type") or ""),
                 verification_status=str(claim.get("verification_status") or ""),
                 authority_level=claim_node.get("authority_level"))
        claim_edges = []
        for ref in claim.get("evidence_refs") or []:
            reference = str(ref or "")
            item = evidence_by_ref.get(reference)
            info = edge_status(claim_node, reference, item)
            relation = relation_for_status(info.get("status"))
            strength = edge_strength(info)
            src = node_id("claim", cid)
            dst = add_node("evidence", reference, reference)
            if len(edges) >= limit:
                truncated += 1
                continue
            edge = {
                "edge_id": edge_id("claim-evidence", src, dst, relation),
                "kind": "claim-evidence",
                "src": src, "dst": dst,
                "graph_relation": relation,
                "status": str(info.get("status") or ""),
                "strength": strength,
                "claim_id": cid, "evidence_ref": reference,
                # 兼容既有边字段（qa_reasoning 的边结构），值口径不变
                "relationship": str((item or {}).get("relationship") or ""),
                "relevance_score": float((item or {}).get("score") or 0),
                "published_at": (item or {}).get("published_at"),
                "scope": list(claim.get("scope") or []),
                "metadata": {
                    "verification_basis": str(info.get("verification_basis") or ""),
                    "verified": bool(info.get("verified")),
                    "qualified": str(info.get("status")) in EVIDENCE_GRAPH_QUALIFIED_STATUSES,
                    "source_id": _source_key(item),
                    "authority_level": (item or {}).get("authority_level"),
                    "reasons": list(info.get("reasons") or []),
                    "reason_text": str(info.get("reason_text") or ""),
                    "dangling_ref": item is None,
                    "claimed_status": str(info.get("claimed_status")
                                          or info.get("status") or ""),
                    **({"claimed_relationship": info["claimed_relationship"]}
                       if info.get("claimed_relationship") else {}),
                },
            }
            edges.append(edge)
            claim_edges.append(edge)
        edges_by_claim[cid] = claim_edges
        support = [e for e in claim_edges if e["graph_relation"] == EG_RELATION_SUPPORTS]
        refute = [e for e in claim_edges if e["graph_relation"] == EG_RELATION_REFUTES]
        mention = [e for e in claim_edges if e["graph_relation"] == EG_RELATION_MENTIONS]
        claim_rows.append({
            "claim_id": cid,
            "node_id": node_id("claim", cid),
            "text": str(claim.get("text") or ""),
            "claim_type": str(claim.get("claim_type") or ""),
            "scope": list(claim.get("scope") or []),
            "valid_from": claim.get("valid_from"),
            "authority_level": int(claim_node.get("authority_level") or 0),
            "verification_status": str(claim.get("verification_status") or ""),
            "plan_only": False,
            "relations": {
                "SUPPORTS": len(support), "REFUTES": len(refute), "MENTIONS": len(mention),
            },
            "verified_support_count": len([e for e in support if e["metadata"]["verified"]]),
            "verified_refute_count": len([e for e in refute if e["metadata"]["verified"]]),
            "support_mass": round(sum(e["strength"] for e in support), 4),
            "verified_support_mass": round(
                sum(e["strength"] for e in support if e["metadata"]["verified"]), 4),
            "refute_mass": round(sum(e["strength"] for e in refute), 4),
            "independent_sources": len({e["metadata"]["source_id"] for e in support
                                        if e["metadata"]["source_id"]}),
        })

    claim_rows.extend(_plan_claim_rows(plan, claim_rows, add_node))
    _merge_plan_dependencies(plan, claim_rows)

    # 矛盾（P06-04）：检测 → 裁决 → 挂 CONTRADICTS 边 + contradiction 节点
    conflicts = [item for item in (graph.get("conflicts") or []) if isinstance(item, Mapping)]
    contradictions = detect_contradictions(claim_rows, edges_by_claim, conflicts,
                                           evidence_by_ref=evidence_by_ref)
    for decision in contradictions:
        add_node("contradiction", decision["contradiction_id"], decision.get("rationale", "")[:300],
                 kind=decision["kind"], resolution=decision["resolution"],
                 reason_code=decision["reason_code"])
    edges.extend(_contradiction_edges(contradictions))
    edges.extend(_depends_edges(claim_rows))

    coverage = claim_coverage(claim_rows, edges)
    consistency = _status_consistency(graph_claims, edges_by_claim)
    stats = {
        "graph_version": EVIDENCE_GRAPH_VERSION,
        "nodes": len(nodes), "edges": len(edges), "truncated_edges": truncated,
        "claims": len([row for row in claim_rows if not row["plan_only"]]),
        "evidence_nodes": len(evidence_by_ref),
        "source_nodes": len(source_seen),
        "relation_distribution": relation_distribution(edges, kind="claim-evidence"),
        "edge_kind_distribution": _counter(edge.get("kind") for edge in edges),
        "verification_basis": _counter(edge.get("metadata", {}).get("verification_basis")
                                       for edge in edges
                                       if edge.get("kind") == "claim-evidence"),
        "contradictions": len(contradictions),
        "contradiction_kinds": _counter(item["kind"] for item in contradictions),
        "resolution_distribution": _counter(item["resolution"] for item in contradictions),
        "reason_codes": _counter(item["reason_code"] for item in contradictions),
        "coverage": coverage,
        "status_consistency": consistency,
        "resolver": resolver_report(resolver),
        "depends_edges": len([e for e in edges if e["graph_relation"] == EG_RELATION_DEPENDS]),
        "plan_claims": len([row for row in claim_rows if row["plan_only"]]),
        "plan_claims_matched": len([row for row in claim_rows
                                    if row["plan_only"] and row.get("matched_claim_id")]),
        "plan_dependencies": len((plan or {}).get("dependencies") or [])
        if isinstance(plan, Mapping) else 0,
        "valid_edges": _valid_edges(edges),
    }
    return {
        "graph_version": EVIDENCE_GRAPH_VERSION,
        "run_id": str(run_id or ""),
        "nodes": nodes,
        "edges": edges,
        "claims": claim_rows,
        "coverage": coverage,
        "contradictions": contradictions,
        "stats": stats,
    }


def _counter(values: Iterable) -> dict:
    counts: dict = {}
    for value in values:
        key = str(value or "")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _valid_edges(edges: Sequence[Mapping]) -> dict:
    """边的机器可校验自检：端点种类与关系的合法组合（`EVIDENCE_GRAPH_RELATIONS_BY_KIND`）。"""
    checked = {}
    for edge in edges:
        ok, note = validate("evidence_graph_edge", dict(edge))
        kind = str(edge.get("kind") or "")
        allowed = EVIDENCE_GRAPH_RELATIONS_BY_KIND.get(kind, ())
        relation_ok = str(edge.get("graph_relation") or "") in allowed
        checked[edge["edge_id"]] = {"schema": bool(ok), "relation_allowed": bool(relation_ok),
                                    "note": note}
    return {"checked": len(checked),
            "schema_failures": len([v for v in checked.values() if not v["schema"]]),
            "relation_failures": len([v for v in checked.values() if not v["relation_allowed"]])}


def relation_distribution(edges: Sequence[Mapping], *, kind: str = "") -> dict:
    return _counter(edge.get("graph_relation") for edge in edges
                    if not kind or str(edge.get("kind")) == str(kind))


def _plan_claim_rows(plan: Mapping | None, claim_rows: list, add_node) -> list:
    """计划 claim 进图（`plan_only=True`）：它们是 §7 的"要证实什么"，不是结论。

    只做两件事：① 为 DEPENDS 边提供真实的依赖端点；② 用文本相似度标注它与哪条结论文本
    可能对应（`matched_claim_id`，仅标注、不参与 coverage）。**永不**计入 coverage 分母。
    """
    rows = []
    plan = plan if isinstance(plan, Mapping) else {}
    for claim in plan.get("claims") or []:
        if not isinstance(claim, Mapping):
            continue
        claim_id = str(claim.get("claim_id") or "")
        if not claim_id:
            continue
        statement = str(claim.get("statement") or "")
        best, best_score = "", 0.0
        for row in claim_rows:
            if row["plan_only"]:
                continue
            score = _text_similarity(statement, row["text"])
            if score > best_score:
                best, best_score = row["claim_id"], score
        threshold = float(thresholds()["plan_match_min"])
        matched = best if best_score >= threshold else ""
        add_node("claim", claim_id, statement[:300], plan_only=True,
                 sub_question_id=str(claim.get("sub_question_id") or ""),
                 role=str(claim.get("role") or ""),
                 matched_claim_id=matched, match_similarity=round(best_score, 4))
        rows.append({
            "claim_id": claim_id, "node_id": node_id("claim", claim_id),
            "text": statement, "claim_type": "plan", "scope": [],
            "valid_from": None, "authority_level": 0, "verification_status": "",
            "plan_only": True, "role": str(claim.get("role") or ""),
            "sub_question_id": str(claim.get("sub_question_id") or ""),
            "matched_claim_id": matched, "match_similarity": round(best_score, 4),
            "relations": {"SUPPORTS": 0, "REFUTES": 0, "MENTIONS": 0},
            "verified_support_count": 0, "verified_refute_count": 0,
            "support_mass": 0.0, "verified_support_mass": 0.0, "refute_mass": 0.0,
            "independent_sources": 0,
        })
    return rows


def _depends_edges(claim_rows: Sequence[Mapping]) -> list:
    """DEPENDS 边：计划里声明的依赖（`from` 是前置、`to` 是后继）在 claim 层的投影。"""
    plan_rows = {row["claim_id"]: row for row in claim_rows if row.get("plan_only")}
    by_sub = {}
    for row in plan_rows.values():
        by_sub.setdefault(str(row.get("sub_question_id") or ""), []).append(row)
    edges = []
    for row in claim_rows:
        if not row.get("plan_only"):
            continue
        for dep in row.get("depends_on_subs") or []:
            for source in by_sub.get(str(dep), []):
                if source["claim_id"] == row["claim_id"]:
                    continue
                src = row["node_id"]            # 后继（依赖方）
                dst = source["node_id"]         # 前置（被依赖方）
                edges.append({
                    "edge_id": edge_id("claim-claim", src, dst, EG_RELATION_DEPENDS),
                    "kind": "claim-claim", "src": src, "dst": dst,
                    "graph_relation": EG_RELATION_DEPENDS,
                    "status": "", "strength": 1.0,
                    "claim_id": row["claim_id"], "evidence_ref": "",
                    "metadata": {"dependency_source": "qa_execution_graph.build_research_plan",
                                 "sub_question": row.get("sub_question_id", ""),
                                 "depends_on_sub_question": str(dep),
                                 "carries": list(row.get("carries") or [])},
                })
    return edges


def _contradiction_edges(contradictions: Sequence[Mapping]) -> list:
    """矛盾节点的挂载边 + 冲突双方之间的直接 CONTRADICTS 边。

    两种表示并存，各有用途：
      · claim→contradiction / evidence→contradiction（P06-01 的 Contradiction 节点，§3.2）；
      · claim↔claim 的直接 CONTRADICTS 边（结论级冲突时才有）：查"这两条结论是否打架"
        不必先跳节点，且让 `claim-claim` 这一端点上真的出现 CONTRADICTS 关系。
    """
    edges = []
    for item in contradictions:
        target = node_id("contradiction", item["contradiction_id"])
        claim_ids = [str(cid) for cid in (item.get("claim_ids") or []) if str(cid)]
        for cid in claim_ids:
            src = node_id("claim", cid)
            edges.append({
                "edge_id": edge_id("claim-contradiction", src, target, EG_RELATION_CONTRADICTS),
                "kind": "claim-contradiction", "src": src, "dst": target,
                "graph_relation": EG_RELATION_CONTRADICTS, "status": "", "strength": 1.0,
                "claim_id": str(cid), "evidence_ref": "",
                "metadata": {"reason_code": item["reason_code"], "resolution": item["resolution"]},
            })
        if str(item.get("kind")) == "claim_conflict" and len(claim_ids) >= 2:
            # 方向固定为声明顺序（确定性哈希），不做双向重复边
            src, dst = node_id("claim", claim_ids[0]), node_id("claim", claim_ids[1])
            edges.append({
                "edge_id": edge_id("claim-claim", src, dst, EG_RELATION_CONTRADICTS),
                "kind": "claim-claim", "src": src, "dst": dst,
                "graph_relation": EG_RELATION_CONTRADICTS, "status": "", "strength": 1.0,
                "claim_id": claim_ids[0], "evidence_ref": "",
                "metadata": {"contradiction_id": item["contradiction_id"],
                             "conflict_type": item.get("conflict_type", ""),
                             "reason_code": item["reason_code"],
                             "resolution": item["resolution"],
                             "counterpart_claim_id": claim_ids[1]},
            })
        for ref in item.get("evidence_refs") or []:
            src = node_id("evidence", ref)
            edges.append({
                "edge_id": edge_id("evidence-contradiction", src, target, EG_RELATION_CONTRADICTS),
                "kind": "evidence-contradiction", "src": src, "dst": target,
                "graph_relation": EG_RELATION_CONTRADICTS, "status": "", "strength": 1.0,
                "claim_id": "", "evidence_ref": str(ref),
                "metadata": {"reason_code": item["reason_code"], "resolution": item["resolution"]},
            })
    return edges


# ── P06-03：claim coverage ─────────────────────────────────────────────────
def claim_coverage(claim_rows: Sequence[Mapping], edges: Sequence[Mapping]) -> dict:
    """coverage 口径见模块常量 `COVERAGE_DEFINITION`（主口径只数**已核验**支持）。"""
    answered = [row for row in claim_rows if not row.get("plan_only")]
    by_claim: dict = {}
    for edge in edges:
        if str(edge.get("kind")) != "claim-evidence":
            continue
        by_claim.setdefault(str(edge.get("claim_id") or ""), []).append(edge)
    rows, histogram, buckets = [], {}, {"0": 0, "(0,0.5]": 0, "(0.5,1)": 0, "1": 0, "refuted": 0}
    supported = qualified_only = refuted = with_evidence = 0
    weight_sum = verified_mass_total = 0.0
    claimed_edges = 0
    for row in answered:
        own = by_claim.get(row["claim_id"], [])
        verified_support = [e for e in own if e["graph_relation"] == EG_RELATION_SUPPORTS
                            and e["metadata"]["verified"]]
        claimed_support = [e for e in own
                           if str(e["metadata"].get("claimed_status") or "") == EVIDENCE_STATUS_SUPPORTED
                           and not e["metadata"]["verified"]]
        verified_refute = [e for e in own if e["graph_relation"] == EG_RELATION_REFUTES
                           and e["metadata"]["verified"]]
        claimed_edges += len(claimed_support)
        mass = round(sum(e["strength"] for e in verified_support), 4)
        weight = min(1.0, mass)
        if verified_support:
            supported += 1
        elif verified_refute:
            refuted += 1
        elif claimed_support:
            qualified_only += 1        # 有"声称的支持"但一条都没被核验
        if own:
            with_evidence += 1
        weight_sum += weight
        verified_mass_total += mass
        histogram[str(len(verified_support))] = histogram.get(str(len(verified_support)), 0) + 1
        if verified_refute and not verified_support:
            buckets["refuted"] += 1
        elif weight <= 0:
            buckets["0"] += 1
        elif weight <= 0.5:
            buckets["(0,0.5]"] += 1
        elif weight < 1:
            buckets["(0.5,1)"] += 1
        else:
            buckets["1"] += 1
        rows.append({"claim_id": row["claim_id"], "status": row.get("verification_status", ""),
                     "verified_supports": len(verified_support),
                     "claimed_supports": len(claimed_support),
                     "verified_refutes": len(verified_refute),
                     "verified_support_mass": mass, "weight": round(weight, 4),
                     "independent_sources": row.get("independent_sources", 0)})
    total = len(answered)
    divisor = max(1, total)
    return {
        "coverage_version": COVERAGE_VERSION,
        "coverage_definition": COVERAGE_DEFINITION,
        "total_claims": total,
        "supported_claims": supported,
        "qualified_claims": qualified_only,
        "refuted_claims": refuted,
        "claims_with_evidence": with_evidence,
        "claims_without_evidence": total - with_evidence,
        "claim_coverage": round(supported / divisor, 4) if total else None,
        "weighted_claim_coverage": round(weight_sum / divisor, 4) if total else None,
        "claimed_claim_coverage": round(
            (supported + qualified_only) / divisor, 4) if total else None,
        "claimed_support_edges": claimed_edges,
        "evidence_coverage": round(with_evidence / divisor, 4) if total else None,
        "refuted_claim_rate": round(refuted / divisor, 4) if total else None,
        "verified_support_mass": round(verified_mass_total, 4),
        "support_count_histogram": dict(sorted(histogram.items(), key=lambda item: int(item[0]))),
        "coverage_buckets": buckets,
        "by_claim": rows[:200],
    }


def _status_consistency(graph_claims: Sequence[Mapping], edges_by_claim: Mapping) -> dict:
    """图里的关系能否复算出核验层写的 `verification.status`（不一致就报出来，不掩盖）。"""
    checked = mismatches = 0
    details = []
    for claim_node in graph_claims:
        cid = _claim_id(claim_node)
        verification = claim_node.get("verification")
        if cid not in edges_by_claim or not isinstance(verification, Mapping):
            continue
        expected = str(verification.get("status") or "")
        if not expected:
            continue
        checked += 1
        computed = claim_status_from_edges(edges_by_claim[cid])
        if computed != expected:
            mismatches += 1
            details.append({"claim_id": cid, "verification_status": expected,
                            "from_edges": computed})
    return {"checked": checked, "mismatches": mismatches, "details": details[:10]}


# ── P06-04：矛盾检测与规则裁决（零模型调用）────────────────────────────────
_RESOLVERS: dict = {}
_RESOLVER_LOCK_SENTINEL = object()


def register_contradiction_resolver(name: str, fn: Callable) -> None:
    """注册**可插拔**裁决器（规格书 §29 "Contradiction Resolver：strong model, conditional"）。

    本轮不注册任何模型后端（GPU 停用、硬约束不许调模型）；接口留给后续阶段/私有部署：
    `fn(contradiction: Mapping) -> Mapping`，需返回含 `resolution`/`reason_code` 的字典。
    名字非法、抛错、返回值非法 → 一律保守回落到规则实现并记账（绝不假装裁过）。
    """
    clean = str(name or "").strip()
    if not clean or not callable(fn):
        raise ValueError("裁决器名字与可调用对象必填")
    _RESOLVERS[clean] = fn


def contradiction_resolvers() -> tuple:
    return tuple(sorted(_RESOLVERS))


def resolver_name() -> str:
    return str(os.environ.get("QA_CONTRADICTION_RESOLVER", "rule") or "rule").strip() or "rule"


def _rule_resolver(contradiction: Mapping) -> dict:
    """默认裁决器：§15 的八项比较 → 规则（**确定性**，同输入同输出）。"""
    return _rule_decide(dict(contradiction))


def resolve(contradiction: Mapping, *, resolver: Callable | None = None) -> dict:
    """裁决一条矛盾；返回 `{resolution, reason_code, decider, ...}`（失败一律回落规则）。"""
    name = resolver_name()
    fallback = ""
    chooser = resolver
    if chooser is None and name and name != "rule":
        chooser = _RESOLVERS.get(name)
        if chooser is None:
            fallback = "resolver_not_registered:%s" % name
    if chooser is not None:
        try:
            produced = chooser(dict(contradiction))
            if isinstance(produced, Mapping) and str(produced.get("resolution") or "") in (
                    "resolved", "unresolved"):
                return {"resolution": str(produced["resolution"]),
                        "reason_code": str(produced.get("reason_code") or RESOLUTION_NO_DECISIVE_RULE),
                        "decider": "injected:%s" % (name or "inline"),
                        "rationale": str(produced.get("rationale") or "")[:2000],
                        "fallback": fallback}
            fallback = fallback or "resolver_returned_invalid_payload"
        except Exception as exc:  # noqa: BLE001  注入点坏掉绝不能把建图打断
            fallback = "resolver_error:%s" % type(exc).__name__
    decided = _rule_resolver(contradiction)
    decided["fallback"] = fallback
    return decided


def resolver_report(resolver: Callable | None = None) -> dict:
    return {"name": resolver_name(), "registered": list(contradiction_resolvers()),
            "inline": bool(resolver is not None),
            "version": CONTRADICTION_RESOLVER_VERSION, "thresholds": thresholds()}


def _side_metrics(edges: Sequence[Mapping], claims: Sequence[Mapping] = ()) -> dict:
    """一侧（支持侧 / 反驳侧）的可比指标：全部来自已记录的边与 claim，可逐项复算。

    `claim_latest` = 该侧结论自己的生效时间（claim.valid_from）——两类矛盾的"时间比较"含义不同：
      · `claim_conflict`：比的是**两条结论的生效时间**（与 `qa_reasoning._adjudicate` 的
        time_change 口径一致）；
      · `evidence_conflict`：同一条结论没有两个生效时间，比的是**证据发布时间**（`latest`）。
    """
    mass = sum(float(edge.get("strength") or 0) for edge in edges)
    verified_mass = sum(float(edge.get("strength") or 0) for edge in edges
                        if (edge.get("metadata") or {}).get("verified"))
    authority = max([int(edge.get("metadata", {}).get("authority_level") or 0) for edge in edges]
                    + [int(claim.get("authority_level") or 0) for claim in claims] + [0])
    edge_dates = [_date(edge.get("published_at")) for edge in edges]
    claim_dates = [_date(claim.get("valid_from")) for claim in claims
                   if isinstance(claim, Mapping)]
    known_edges = [item for item in edge_dates if item]
    known_claims = [item for item in claim_dates if item]
    sources = {str((edge.get("metadata") or {}).get("source_id") or "") for edge in edges}
    sources.discard("")
    latest = max(known_edges + known_claims, default=None)
    claim_latest = max(known_claims, default=None)
    # 一律 ISO 字符串：决策载荷会被 `persist_reasoning_graph` / `record_stage` 直接
    # json.dumps 落库（实测：datetime 会让整个阶段回执写不进去）——所以这里不放 datetime 对象，
    # 比较时再用 `_date()` 解析回来。
    return {
        "edge_count": len(edges),
        "mass": round(mass, 4),
        "verified_mass": round(verified_mass, 4),
        "effective_mass": round(verified_mass if verified_mass > 0 else mass, 4),
        "authority": int(authority),
        "independence": len(sources),
        "latest": latest.isoformat(timespec="seconds") if latest else "",
        "claim_latest": claim_latest.isoformat(timespec="seconds") if claim_latest else "",
        "claim_type": str(claims[0].get("claim_type") or "") if claims else "",
    }


def _rule_decide(item: Mapping) -> dict:
    """规则裁决：按固定优先级逐条判，命中即返回，**每条都记录用到的数字**。"""
    limits = item.get("inputs", {}).get("thresholds") or thresholds()
    left, right = dict(item.get("left") or {}), dict(item.get("right") or {})
    conflict_type = str(item.get("conflict_type") or "")
    basis = {"left": left, "right": right, "thresholds": limits,
             "conflict_type": conflict_type, "kind": item.get("kind")}

    def decide(resolution: str, code: str, rationale: str, winner=None) -> dict:
        return {"resolution": resolution, "reason_code": code,
                "decider": "rule:%s" % CONTRADICTION_RESOLVER_VERSION,
                "rationale": rationale, "winner": winner or {},
                "inputs": {**basis, "winner": winner or {}}}

    # R0 适用范围不同：两份说法都成立（各限定范围），不是"谁对谁错"
    if conflict_type == "scope_difference":
        return decide("resolved", RESOLUTION_SCOPE_DIFFERENCE,
                      "两项说法的适用范围不同（scope 无交集），分别成立；回答时分别标注范围。")
    # R1 生效时间/发布时间的版本比较（§15"是否存在更新版本"）
    # 两类矛盾比的是不同的时间：结论级比"结论生效时间"，证据级比"证据发布时间"。
    def _version_date(side_metrics: Mapping):
        return _date(side_metrics.get("claim_latest") or side_metrics.get("latest"))

    ld, rd = _version_date(left), _version_date(right)
    if ld and rd and ld != rd:
        newer = "left" if ld > rd else "right"
        older = "right" if newer == "left" else "left"
        return decide("resolved", RESOLUTION_NEWER_VERSION,
                      "按生效时间/发布时间区分：较新的一方用于当前结论，较旧的一方保留在时间线上。",
                      {"side": newer, "effective_at": (ld if newer == "left" else rd).isoformat(
                          timespec="seconds"), "superseded_at": (
                              rd if newer == "left" else ld).isoformat(timespec="seconds"),
                       "superseded_side": older})
    # R2 来源权威度（§15"来源级别"）
    gap = int(left.get("authority") or 0) - int(right.get("authority") or 0)
    if abs(gap) >= int(limits.get("authority_gap") or 2):
        side = "left" if gap > 0 else "right"
        return decide("resolved", RESOLUTION_AUTHORITY,
                      "一边来自权威度更高的来源（权威分差 %d ≥ 阈值 %d），以它为准；另一边只作参考。"
                      % (abs(gap), int(limits.get("authority_gap") or 2)),
                      {"side": side, "authority_gap": abs(gap)})
    # R3 证据质量分（§11 EvidenceScore 归一后的**已核验**质量差）
    lq = float(left.get("verified_mass") or 0)
    rq = float(right.get("verified_mass") or 0)
    ratio = float(limits.get("quality_ratio") or 2.0)
    if min(lq, rq) > 0 and max(lq, rq) >= min(lq, rq) * ratio:
        side = "left" if lq > rq else "right"
        return decide("resolved", RESOLUTION_QUALITY,
                      "已核验证据质量分（§11 得分）相差 ≥ %.1f 倍，以证据更强的一方为准。" % ratio,
                      {"side": side, "quality_ratio": round(max(lq, rq) / min(lq, rq), 3),
                       "left_verified_mass": round(lq, 4), "right_verified_mass": round(rq, 4)})
    # R4 证据独立性（§15"证据独立性"：独立来源数）
    igap = int(left.get("independence") or 0) - int(right.get("independence") or 0)
    need = int(limits.get("independence_gap") or 2)
    if abs(igap) >= need:
        side = "left" if igap > 0 else "right"
        return decide("resolved", RESOLUTION_INDEPENDENCE,
                      "一边有更多相互独立的来源（差 %d ≥ 阈值 %d），以它为准。" % (abs(igap), need),
                      {"side": side, "independence_gap": abs(igap)})
    # R5 支持/反驳强度比（兜底的强弱比较：含未被核验的"声称"证据强度）
    sratio = float(limits.get("strength_ratio") or 2.0)
    lmass, rmass = float(left.get("mass") or 0), float(right.get("mass") or 0)
    if min(lmass, rmass) > 0 and max(lmass, rmass) >= min(lmass, rmass) * sratio:
        side = "left" if lmass > rmass else "right"
        return decide("resolved", RESOLUTION_STRENGTH,
                      "支持与反驳的证据强度相差 ≥ %.1f 倍，以更强的一方为准。" % sratio,
                      {"side": side, "strength_ratio": round(max(lmass, rmass) / min(lmass, rmass), 3)})
    # R6 口径/数字不一致：不许取平均，必须回到官方原文
    if conflict_type == "method_difference":
        return decide("unresolved", RESOLUTION_METHOD_UNDECIDED,
                      "不同资料里的数字/期限/计算口径不一致，不能取平均；需以官方原文或最新官方口径为准。")
    # R7 只有解读角度差异（**必须**两侧都明确是解读性说法才算，空值不算）
    claim_types = [str(side.get("claim_type") or "") for side in (left, right)]
    if conflict_type == "opinion_difference" or (
            all(claim_types) and all(item == "interpretation" for item in claim_types)):
        return decide("unresolved", RESOLUTION_OPINION_UNDECIDED,
                      "双方都是解读性说法，无法判定谁对谁错；需回到官方原文或后续官方口径核验。")
    # R8 没有任何一条规则可裁 → 保留不确定性（§15 UNRESOLVED_CONTRADICTION）
    return decide("unresolved", RESOLUTION_NO_DECISIVE_RULE,
                  "现有规则（时间/权威/质量/独立性/强度）都不足以判定，保留为未解决冲突，"
                  "回答必须明确呈现不确定性。")


def detect_contradictions(claim_rows: Sequence[Mapping], edges_by_claim: Mapping,
                          conflicts: Sequence[Mapping],
                          evidence_by_ref: Mapping | None = None) -> list:
    """两族矛盾（P06-04 的检测侧）：

      · `evidence_conflict`：**同一条结论**既有已核验支持、又有已核验反驳（§15 的
        `E1 SUPPORTS C / E2 REFUTES C`）——这是接线前完全没有的检测能力；
      · `claim_conflict`：两条结论互相冲突（`qa_reasoning` 已检出的 `graph["conflicts"]`），
        这里做**更细的裁决**（时间/权威/质量/独立性/强度 + 理由码）。

    两侧指标全部来自 `claim_rows`/边，逐项可复算；裁决由 `resolve()` 完成。
    """
    rows_by_id = {row["claim_id"]: row for row in claim_rows if not row.get("plan_only")}
    decisions = []

    # ① 证据级矛盾（同一结论的支持侧 vs 反驳侧）
    for cid, own in edges_by_claim.items():
        row = rows_by_id.get(cid)
        if row is None:
            continue
        support = [e for e in own if e["graph_relation"] == EG_RELATION_SUPPORTS]
        refute = [e for e in own if e["graph_relation"] == EG_RELATION_REFUTES]
        if not support or not refute:
            continue
        left = _side_metrics(support, [row])
        right = _side_metrics(refute, [row])
        refs = [str(e.get("evidence_ref") or "") for e in support + refute]
        conflict_type = _evidence_conflict_type(support, refute, evidence_by_ref)
        contradiction = {
            "contradiction_id": "ctr:%s" % _digest("|".join(("evidence", cid, *sorted(refs)))),
            "kind": "evidence_conflict",
            "conflict_type": conflict_type,
            "claim_ids": [cid],
            "evidence_refs": refs,
            "left": left, "right": right,
            "inputs": {"thresholds": thresholds(), "conflict_type": conflict_type,
                       "left_refs": [e["evidence_ref"] for e in support],
                       "right_refs": [e["evidence_ref"] for e in refute]},
            "rule_version": CONTRADICTION_RESOLVER_VERSION,
        }
        decisions.append(_finalize_decision(contradiction))

    # ② 结论级矛盾（复用 qa_reasoning 的检测结果，重新做细粒度裁决）
    for conflict in conflicts:
        claim_ids = [str(item) for item in (conflict.get("claim_ids") or []) if str(item)]
        if len(claim_ids) < 2:
            continue
        sides = [rows_by_id.get(item) for item in claim_ids[:2]]
        if any(side is None for side in sides):
            continue
        left_edges = list(edges_by_claim.get(claim_ids[0], []))
        right_edges = list(edges_by_claim.get(claim_ids[1], []))
        contradiction = {
            "contradiction_id": "ctr:%s" % _digest("|".join(("claim",
                                                            str(conflict.get("conflict_id") or ""),
                                                            *sorted(claim_ids[:2])))),
            "kind": "claim_conflict",
            "conflict_type": str(conflict.get("conflict_type") or "real_conflict"),
            "claim_ids": claim_ids[:2],
            "evidence_refs": list(dict.fromkeys(
                [str(ref) for ref in (conflict.get("evidence_refs") or [])]
                + [str(e.get("evidence_ref") or "") for e in left_edges + right_edges])),
            "left": _side_metrics(left_edges, [sides[0]]),
            "right": _side_metrics(right_edges, [sides[1]]),
            "inputs": {"thresholds": thresholds(),
                       "conflict_type": str(conflict.get("conflict_type") or ""),
                       "conflict_id": str(conflict.get("conflict_id") or ""),
                       "legacy_resolution": str(conflict.get("resolution") or ""),
                       "left_claim_id": claim_ids[0], "right_claim_id": claim_ids[1]},
            "rule_version": CONTRADICTION_RESOLVER_VERSION,
        }
        decisions.append(_finalize_decision(contradiction))
    return decisions


def _evidence_conflict_type(support: Sequence[Mapping], refute: Sequence[Mapping],
                            evidence_by_ref: Mapping | None = None) -> str:
    """证据级冲突的类型：两侧证据里的数字/期限口径不同 → method_difference，否则 real_conflict。

    口径与 `qa_reasoning._conflict_type` 的 method_difference 分支一致（"数字不一样 ≠ 谁对谁错"），
    只是这里比较的是**同一条结论的支持/反驳证据**而不是两条结论的文本。
    """
    def numbers_of(edges) -> set:
        found: set = set()
        for edge in edges:
            ref = str(edge.get("evidence_ref") or "")
            item = (evidence_by_ref or {}).get(ref) or {}
            found |= {match.group(0) for match in _NUMBER_RE.finditer(
                str(item.get("content_excerpt") or ""))}
        return found

    left, right = numbers_of(support), numbers_of(refute)
    if left and right and left != right:
        return "method_difference"
    return "real_conflict"


def _finalize_decision(contradiction: Mapping) -> dict:
    """跑裁决器 → 组装 `contradiction_decision` 契约载荷（含理由码合法性自检）。"""
    decided = resolve(contradiction)
    resolution = str(decided.get("resolution") or "unresolved")
    code = str(decided.get("reason_code") or RESOLUTION_NO_DECISIVE_RULE)
    if code not in CONTRADICTION_RESOLUTION_CODES:
        resolution, code = "unresolved", RESOLUTION_NO_DECISIVE_RULE
        decided["fallback"] = (str(decided.get("fallback") or "") + ";unknown_reason_code").strip(";")
    if code in CONTRADICTION_RESOLVED_CODES and resolution != "resolved":
        resolution = "resolved"
    if code in CONTRADICTION_UNRESOLVED_CODES and resolution != "unresolved":
        resolution = "unresolved"
    winner = dict(decided.get("winner") or {})
    if winner.get("side") == "left":
        winner["claim_id"] = contradiction.get("claim_ids", [""])[0]
    elif winner.get("side") == "right":
        winner["claim_id"] = (contradiction.get("claim_ids") or ["", ""])[-1]
    return {
        "contradiction_id": str(contradiction["contradiction_id"]),
        "kind": str(contradiction["kind"]),
        "conflict_type": str(contradiction.get("conflict_type") or ""),
        "claim_ids": list(contradiction.get("claim_ids") or []),
        "evidence_refs": list(contradiction.get("evidence_refs") or []),
        "resolution": resolution,
        "reason_code": code,
        "decider": str(decided.get("decider") or "rule"),
        "rule_version": CONTRADICTION_RESOLVER_VERSION,
        "winner": winner,
        "inputs": dict(contradiction.get("inputs") or {}),
        "left": dict(contradiction.get("left") or {}),
        "right": dict(contradiction.get("right") or {}),
        "rationale": str(decided.get("rationale") or ""),
        "resolver_fallback": str(decided.get("fallback") or ""),
    }


def apply_conflict_decisions(graph: dict, decisions: Sequence[Mapping]) -> int:
    """把裁决结论回写到 `graph["conflicts"]`——**只动冻结 CONFLICT_SCHEMA 允许的三个字段**。

    接线前的 `qa_reasoning._adjudicate` 只有三条分支（scope/时间/权威），这里换成更细的规则；
    由于权威阈值沿用同一口径，接线前 resolved 的冲突在接线后**仍然 resolved**（单调性有守例）。
    """
    by_conflict = {}
    for item in decisions:
        if str(item.get("kind")) != "claim_conflict":
            continue
        legacy = str((item.get("inputs") or {}).get("conflict_id") or "")
        if legacy:
            by_conflict[legacy] = item
    changed = 0
    for conflict in graph.get("conflicts") or []:
        if not isinstance(conflict, dict):
            continue
        decision = by_conflict.get(str(conflict.get("conflict_id") or ""))
        if decision is None:
            continue
        before = (str(conflict.get("resolution") or ""), str(conflict.get("rationale") or ""))
        conflict["resolution"] = str(decision["resolution"])
        conflict["rationale"] = str(decision["rationale"])
        conflict["rule_version"] = CONTRADICTION_RESOLVER_VERSION
        if (conflict["resolution"], conflict["rationale"]) != before:
            changed += 1
    return changed


# ── P06-01：仓储 + 公开只读 API ─────────────────────────────────────────────
def graph_from_rows(*, claims: Sequence[Mapping] = (), edges: Sequence[Mapping] = (),
                    conflicts: Sequence[Mapping] = (), evidence: Sequence[Mapping] = (),
                    run_id: str = "") -> dict:
    """从**库行**重建 `qa_reasoning` 形态的结论图（仓储读回 / 离线复算共用一条路径）。

    `claims` 每行是 `qa_claims` 的字典（`payload` 为完整性优先：优先取 payload 里的节点结构）；
    `edges` 是 `qa_claim_evidence` 行；`evidence` 是 `qa_evidence` 行（payload 里带证据层）。
    """
    nodes: list = []
    by_claim: dict = {}
    for row in claims or []:
        if not isinstance(row, Mapping):
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else None
        node = dict(payload) if payload else {}
        cid = str(node.get("canonical_id") or row.get("claim_key") or "")
        if not cid:
            continue
        claim = node.get("claim") if isinstance(node.get("claim"), Mapping) else {}
        claim = dict(claim)
        claim.setdefault("claim_id", cid)
        claim.setdefault("text", str(row.get("claim_text") or ""))
        claim["verification_status"] = str(
            row.get("verification_status") or claim.get("verification_status") or "")
        node["canonical_id"] = cid
        node["claim"] = claim
        if cid not in by_claim:
            by_claim[cid] = node
    for cid, node in by_claim.items():
        node.setdefault("variants", [])
        node.setdefault("stages", [])
        if not isinstance(node.get("variants"), list):
            node["variants"] = []
        if not isinstance(node.get("stages"), list):
            node["stages"] = []
        nodes.append(node)

    # 边：DB 只存 (claim_key,evidence_ref,relationship,relevance_score)；claim 引用的
    # 证据优先用 DB 的引用集（claim payload 里的 evidence_refs 可能被后续阶段扩过）。
    refs_by_claim: dict = {}
    for row in edges or []:
        if not isinstance(row, Mapping):
            continue
        refs_by_claim.setdefault(str(row.get("claim_key") or ""), []).append(str(row.get("evidence_ref") or ""))
    for cid, node in by_claim.items():
        refs = list(dict.fromkeys(refs_by_claim.get(cid) or []))
        if refs:
            node["claim"]["evidence_refs"] = refs
        elif not node["claim"].get("evidence_refs"):
            node["claim"]["evidence_refs"] = []
    rebuilt_edges = []
    for row in edges or []:
        if not isinstance(row, Mapping):
            continue
        rebuilt_edges.append({
            "claim_id": str(row.get("claim_key") or ""),
            "evidence_ref": str(row.get("evidence_ref") or ""),
            "relationship": str(row.get("relationship") or "supports"),
            "relevance_score": float(row.get("relevance_score") or 0),
        })
    items = []
    for row in evidence or []:
        if not isinstance(row, Mapping):
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        item = dict(payload) if payload else {}
        item.setdefault("evidence_ref", str(row.get("evidence_ref") or ""))
        item.setdefault("source_type", str(row.get("source_type") or ""))
        item.setdefault("source_url", str(row.get("source_url") or ""))
        item.setdefault("title", str(row.get("source_title") or ""))
        item.setdefault("published_at", row.get("published_at"))
        item.setdefault("content_excerpt", "")
        if row.get("authority_level") is not None:
            item.setdefault("authority_level", row.get("authority_level"))
        items.append(item)
    return {"run_id": str(run_id or ""), "claims": nodes, "evidence": items,
            "edges": rebuilt_edges, "conflicts": [dict(item) for item in (conflicts or [])
                                                  if isinstance(item, Mapping)]}


class EvidenceGraphRepository:
    """P06-01：证据图仓储（读写都复用既有存储，**零新表、零迁移**）。

    · `load(run_id)`：从 `qa_claims` / `qa_claim_evidence` / `qa_conflicts` / `qa_evidence` 读回；
    · `build(run_id, plan=...)`：读回 + 建层（只读，不写库）；
    · `save(run_id, graph)`：复用 `qa_storage.persist_reasoning_graph`；
    · `snapshot(run_id)`：面向 API/UI 的公开视图（有上限、JSON 安全、无敏感字段）。
    """

    def __init__(self, store=None):
        self.store = store

    def _store(self, store=None):
        target = store or self.store
        if target is None:
            raise ValueError("需要 QaStore（或用 rows 直接构造仓储）")
        return target

    def load(self, run_id: str, *, store=None) -> dict:
        rows = self._store(store).load_reasoning_graph(str(run_id))
        return graph_from_rows(claims=rows.get("claims") or [], edges=rows.get("edges") or [],
                              conflicts=rows.get("conflicts") or [],
                              evidence=rows.get("evidence") or [], run_id=str(run_id))

    def build(self, run_id: str, *, plan: Mapping | None = None, store=None,
              resolver: Callable | None = None) -> dict:
        graph = self.load(run_id, store=store)
        layer = build_layer(graph, plan=plan, run_id=str(run_id), resolver=resolver)
        apply_conflict_decisions(graph, layer.get("contradictions") or [])
        layer["conflicts"] = [dict(item) for item in graph.get("conflicts") or []]
        return layer

    def save(self, run_id: str, graph: Mapping, *, store=None) -> int:
        """写库：直接复用既有 `persist_reasoning_graph`（本模块不新增任何写 SQL）。"""
        self._store(store).persist_reasoning_graph(str(run_id), graph)
        return len(graph.get("claims") or [])

    def relation_distribution(self, run_id: str, *, store=None) -> dict:
        layer = self.build(run_id, store=store)
        return dict(layer["stats"]["relation_distribution"])

    def coverage(self, run_id: str, *, store=None) -> dict:
        return dict(self.build(run_id, store=store)["coverage"])

    def load_persisted(self, run_id: str, *, store=None, stage: str = "conflict_review") -> dict:
        """读回**当时真的跑过**的那份证据图（`qa_stage_runs.details_json` 里的阶段输出）。

        `qa_orchestrator` 把每个阶段的输出整体写进 `details["result"]`，所以 Phase 06 层的
        回执是**免费持久化**的（零新表、零迁移）。读不到就返回空字典，调用方自行回落
        `build()`（从原始表重建，结果同口径）。
        """
        for row in self._store(store).stage_runs(str(run_id)):
            if str(row.get("stage") or "") != str(stage):
                continue
            details = row.get("details") if isinstance(row.get("details"), Mapping) else {}
            result = details.get("result") if isinstance(details.get("result"), Mapping) else {}
            layer = result.get("evidence_graph") if isinstance(result, Mapping) else None
            if isinstance(layer, Mapping) and layer.get("graph_version"):
                return dict(layer)
        return {}

    def snapshot(self, run_id: str, *, store=None, limit: int = 20) -> dict:
        """公开只读视图（P06-01 的 API 载荷）：节点/边按重要性截断，方向明确。

        优先返回**当时跑过的那份**（`load_persisted`），没有再按表重建（`build`）——
        两者同口径，前者是回放、后者是可复算。
        """
        layer = self.load_persisted(run_id, store=store) or self.build(run_id, store=store)
        cap = max(1, int(limit))
        claims = sorted(layer["claims"], key=lambda row: (-row.get("verified_support_count", 0),
                                                          row["claim_id"]))[:cap]
        return {
            "graph_version": layer["graph_version"],
            "run_id": layer["run_id"],
            "coverage": layer["coverage"],
            "stats": layer["stats"],
            "claims": claims,
            "contradictions": layer["contradictions"][:cap],
            "edges": layer["edges"][: cap * 4],
            "nodes": layer["nodes"][: cap * 4],
            "truncated": {"claims": max(0, len(layer["claims"]) - cap),
                          "edges": max(0, len(layer["edges"]) - cap * 4)},
        }


def _merge_plan_dependencies(plan: Mapping | None, rows: list) -> None:
    """把 Phase 05 计划里的 dependency 挂到 plan claim 行上（DEPENDS 边的输入）。

    `qa_execution_graph.build_research_plan()` 的依赖是 `{"from": 前置, "to": 后继}`
    （`from` 的输出是 `to` 的输入）——照抄这个方向，不重新解释依赖。
    """
    plan = plan if isinstance(plan, Mapping) else {}
    deps_by_to: dict = {}
    for edge in plan.get("dependencies") or []:
        if not isinstance(edge, Mapping):
            continue
        deps_by_to.setdefault(str(edge.get("to") or ""), []).append({
            "from": str(edge.get("from") or ""),
            "carries": [str(item) for item in (edge.get("carries") or [])],
        })
    for row in rows:
        if not row.get("plan_only"):
            continue
        entries = deps_by_to.get(str(row.get("sub_question_id") or ""), [])
        row["depends_on_subs"] = [entry["from"] for entry in entries]
        carries: list = []
        for entry in entries:
            carries.extend(entry["carries"])
        row["carries"] = list(dict.fromkeys(item for item in carries if item))[:20]


def layer_from_graph(graph: Mapping, *, plan: Mapping | None = None, run_id: str = "",
                     resolver: Callable | None = None) -> dict:
    """建层 + 回写冲突裁决的**一步到位**入口（管线只调这一个函数）。

    `build_layer()` 是纯函数（只读入参）；本函数额外把裁决结果回写进
    `graph["conflicts"]`（冻结 CONFLICT_SCHEMA 允许的 resolution/rationale/rule_version）。
    """
    layer = build_layer(graph, plan=plan, run_id=run_id, resolver=resolver)
    if isinstance(graph, dict):
        layer["applied_conflicts"] = apply_conflict_decisions(graph, layer["contradictions"])
    return layer


__all__ = [
    "COVERAGE_DEFINITION", "COVERAGE_VERSION", "DEFAULT_THRESHOLDS",
    "EvidenceGraphRepository", "apply_conflict_decisions", "build_layer", "claim_coverage",
    "claim_status_from_edges", "contradiction_resolvers", "coverage_version",
    "detect_contradictions", "edge_id", "edge_status", "edge_strength",
    "evidence_graph_enabled", "graph_from_rows", "layer_from_graph", "node_id",
    "qualify_factor", "register_contradiction_resolver", "relation_distribution",
    "relation_for_status", "resolve", "resolver_name", "resolver_report", "thresholds",
]

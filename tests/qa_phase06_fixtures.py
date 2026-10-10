#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 06 用例的公共构造器（**不是测试文件**，不参与收集）。

只做一件事：手搓 `qa_reasoning.build_claim_evidence_graph()` 形态的输入，让 Phase 06 的
关系推导/coverage/裁决可以脱离检索链路单独测（零模型、零网络、零数据库）。
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_verifier as verifier  # noqa: E402


def evidence(ref, *, relation="supports", authority=100, published="2026-10-09", url=None,
             excerpt="10月9日A股大涨3.84%", verification=None, source="article"):
    """一条证据条目（含可选 `metadata.evidence_layer.verification`，即 Phase 03 的核验结论）。"""
    metadata = {}
    layer = {}
    if verification:
        layer["verification"] = verification
    if layer:
        metadata["evidence_layer"] = layer
    return {
        "evidence_ref": ref, "source_type": source, "title": "标题 %s" % ref,
        "source_url": url or ("https://example.com/%s" % ref.replace(":", "-")),
        "content_excerpt": excerpt, "published_at": published, "relationship": relation,
        "authority_level": authority, "score": 12.5, "metadata": metadata,
    }


def claim_node(cid, *, text="A股10月9日大涨", refs=(), pairs=None, status="unverified",
               authority=None, valid_from="2026-10-09", scope=None, claim_type="current_fact",
               verification=True):
    """一个 canonical claim 节点（`canonical_id`/`claim`/`verification` 三段结构）。"""
    claim = {"claim_id": cid, "text": text, "claim_type": claim_type, "confidence": 0.8,
             "valid_from": valid_from, "valid_to": None, "scope": list(scope or []),
             "evidence_refs": list(refs), "needs_verification": True,
             "verification_status": status}
    node = {"canonical_id": cid, "claim": claim, "variants": [], "stages": ["level1"]}
    if authority is not None:
        node["authority_level"] = authority
    if verification:
        node["verification"] = {"verifier_version": verifier.VERIFIER_VERSION, "status": status,
                                "pairs": list(pairs or [])}
    return node


def graph_of(claims, evidence_items, conflicts=None, edges=None):
    """`qa_reasoning.build_claim_evidence_graph()` 形态的结论图。

    `edges` 不传时按 claim 的 evidence_refs 现造（与 `qa_reasoning` 的边结构逐字一致：
    claim_id/evidence_ref/relationship/relevance_score/published_at/scope）——
    这样 `persist_reasoning_graph()` 也能直接吃这份图（仓储用例需要）。
    """
    items = list(evidence_items)
    if edges is None:
        by_ref = {str(item.get("evidence_ref") or ""): item for item in items}
        edges = []
        for node in claims:
            claim = node.get("claim") or {}
            for ref in claim.get("evidence_refs") or []:
                item = by_ref.get(str(ref)) or {}
                edges.append({
                    "claim_id": str(node.get("canonical_id") or claim.get("claim_id") or ""),
                    "evidence_ref": str(ref),
                    "relationship": str(item.get("relationship") or "supports"),
                    "relevance_score": float(item.get("score") or claim.get("confidence") or 0),
                    "published_at": item.get("published_at"),
                    "scope": list(claim.get("scope") or []),
                })
    return {"version": "qa-adjudication-v1", "claims": list(claims),
            "evidence": items, "edges": list(edges), "conflicts": list(conflicts or [])}

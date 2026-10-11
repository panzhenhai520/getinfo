#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 07 用例的公共构造器（**不是测试文件**，不参与收集）。

只做一件事：手搓缺口分析的三种输入形状，让 P07-01…P07-06 可以脱离检索链路单独测
（零模型、零网络、零外部端点）：
  · `plan_claim()`   —— Phase 05 `qa_execution_graph.build_research_plan()` 的计划 claim；
  · `graph_claim()`  —— Phase 06 `qa_evidence_graph.build_layer()` 的 claim row；
  · `graph_edge()`   —— Phase 06 的 claim-evidence 边（带 metadata.verified / reasons）；
  · `evidence_item()`—— 证据条目（可带 Phase 03 的 `metadata.evidence_layer.verification`）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def evidence_item(ref, *, text="2026年医保新规要求民营医院按病种付费", url=None, authority=50,
                  published="2026-01-10", verification=None, source_type="article",
                  title=None):
    """一条证据（`metadata.evidence_layer.verification` 即 Phase 03 的核验结论）。"""
    metadata = {}
    if verification:
        metadata["evidence_layer"] = {"verification": dict(verification)}
    return {
        "evidence_ref": ref, "source_type": source_type,
        "title": title or text[:40], "content_excerpt": text,
        "source_url": url or ("https://example.com/%s" % str(ref).replace(":", "-")),
        "authority_level": authority, "published_at": published, "score": 12.5,
        "retrieval_method": "keyword", "metadata": metadata,
    }


def verification(verdict="SUPPORTED", *, score=0.7, reasons=(), entailment=0.8):
    return {"verifier_version": "qa-verifier-v1", "verdict": verdict, "verified":
            verdict == "SUPPORTED", "score": score, "reasons": list(reasons),
            "dimensions": {"entailment": entailment}, "text_source": "span",
            "cache": "off"}


def graph_claim(cid, *, text="2026年医保新规要求民营医院按病种付费", status="unverified",
                refs=(), scope=("民营医院",), authority=None, plan_only=False,
                claim_type="current_fact", verified_support_count=0, verified_refute_count=0,
                independent_sources=0, verified_support_mass=0.0, support_mass=0.0,
                refute_mass=0.0, pairs=None):
    """Phase 06 形状的 claim row（`qa_evidence_graph.build_layer()` 的 claims 元素）。"""
    row = {
        "claim_id": cid, "node_id": "claim:%s" % cid, "text": text,
        "claim_type": claim_type, "scope": list(scope), "valid_from": "2026-01-01",
        "authority_level": authority if authority is not None else 50,
        "verification_status": status, "plan_only": bool(plan_only),
        "relations": {"SUPPORTS": verified_support_count, "REFUTES": verified_refute_count,
                      "MENTIONS": 0},
        "verified_support_count": verified_support_count,
        "verified_refute_count": verified_refute_count,
        "independent_sources": independent_sources,
        "support_mass": support_mass, "verified_support_mass": verified_support_mass,
        "refute_mass": refute_mass,
    }
    if pairs is not None:
        row["verification"] = {"verifier_version": "qa-verifier-v1", "status": status,
                               "pairs": list(pairs)}
    return row


def plan_claim(cid, statement, *, role="answer", sub_question_id="", plan_only=None):
    """Phase 05 形状的计划 claim（`build_research_plan()["claims"]` 的元素）。"""
    row = {"claim_id": cid, "plan_node_kind": "claim", "statement": statement, "role": role,
           "sub_question_id": sub_question_id or ("sq:%s" % cid),
           "required_evidence_types": ["official_policy"], "parallel_group": ""}
    if plan_only is not None:
        row["plan_only"] = bool(plan_only)
    return row


def graph_edge(claim_id, ref, *, relation="SUPPORTS", verified=True, status="SUPPORTED",
               strength=0.7, authority=50, source_id=None, reasons=(), relevance=45.0,
               published="2026-01-10"):
    """Phase 06 形状的 claim-evidence 边（`graph_relation` 用图级大写关系）。"""
    return {
        "edge_id": "edge:%s:%s" % (claim_id, ref), "kind": "claim-evidence",
        "src": "claim:%s" % claim_id, "dst": "evidence:%s" % ref,
        "graph_relation": relation, "status": status, "strength": strength,
        "claim_id": claim_id, "evidence_ref": ref, "relationship": relation.casefold(),
        "relevance_score": relevance, "published_at": published,
        "scope": [], "metadata": {
            "verification_basis": "verifier_pairs" if verified else "relationship",
            "verified": bool(verified), "source_id": source_id or ("source:%s" % ref),
            "authority_level": authority, "reasons": list(reasons),
            "claimed_status": status,
        },
    }


def contradiction(cid, claim_ids, *, resolution="unresolved", reason_code="NO_DECISIVE_RULE",
                  kind="claim_conflict"):
    """Phase 06 的矛盾裁决结论（`layer["contradictions"]` 的元素）。"""
    return {"contradiction_id": cid, "kind": kind, "resolution": resolution,
            "reason_code": reason_code, "decider": "rule",
            "rule_version": "qa-contradiction-resolver-v1", "claim_ids": list(claim_ids),
            "evidence_refs": [], "rationale": "条条都不满足：无法消解，保留不确定性"}


def plan(question="2026年医保新规对民营医院有什么影响？", *, entities=("民营医院", "医保"),
         topics=(), category="multi_hop"):
    return {"question": question, "standalone_question": question,
            "entities": list(entities), "topics": list(topics), "category": {"key": category},
            "decomposition": {"is_multi_hop": True, "pattern": "impact_chain",
                              "hops": [{"id": "h1", "question": question, "depends_on": []}]}}

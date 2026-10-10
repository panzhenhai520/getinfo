#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 08 用例的公共构造器（**不是测试文件**，不参与收集）。

只做一件事：手搓 Context Pack 的四种输入形状，让 P08-01…P08-06 与生成端 grounding
校验可以脱离检索链路单独测（零模型、零网络、零外部端点）：
  · `evidence_item()` —— 证据条目，**真的过一遍 Phase 02 的 `annotate_evidence()`**
    （所以 `metadata.evidence_layer` 里的最小 span / 指纹 / 来源身份都是真实产物，
    不是手写的假数据）；
  · `graph_claim()` / `graph_edge()` / `graph()` —— Phase 06 证据图的形状；
  · `plan()` —— Phase 05 计划（问题 + 子问题）；
  · `final_answer()` —— `FINAL_ANSWER_SCHEMA` 形状的生成端草稿（grounding 校验的输入）。

长文本用来验证"最小 span 真的切出来了"：`LONG_ARTICLE` 里埋了一段与问题实词重合的句子。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qa_evidence import annotate_evidence  # noqa: E402

QUESTION = "香港家族办公室税收优惠政策对内地高净值客户有什么影响？"

LONG_ARTICLE = (
    "本报记者近日就家族办公室税收安排采访了多位业内人士。"
    + "背景资料显示，离岸架构的合规成本在近三年持续上升，从业者的关注点也从税率转向申报义务。" * 6
    + "香港家族办公室税收优惠政策明确对符合条件的管理人给予利得税宽免，"
      "并要求实质经营与本地雇佣，这一条对内地高净值客户的跨境配置影响最大。"
    + "另有观点认为，客户还需要同时评估内地个人所得税与外汇合规要求。" * 5
)


def evidence_item(ref, *, text=None, url=None, authority=50, published="2026-01-10",
                  verification=None, source_type="article", title=None, annotate=True,
                  relationship=None, doc_type=None):
    """一条证据（`metadata.evidence_layer` 由 Phase 02 真实标注产生）。"""
    item = {
        "evidence_ref": ref, "source_type": source_type,
        "title": title or (text or LONG_ARTICLE)[:40],
        "content_excerpt": text if text is not None else LONG_ARTICLE,
        "source_url": url or ("https://example.com/%s" % str(ref).replace(":", "-")),
        "authority_level": authority, "published_at": published, "score": 12.5,
        "retrieval_method": "keyword", "metadata": {},
    }
    if doc_type:
        # 必须放 metadata：冻结 EVIDENCE_SCHEMA 的 additionalProperties=False，
        # 顶层多一个 doc_type 会让整条最终答案校验失败（真实读取点也是
        # `item.get("doc_type") or metadata.get("doc_type")`）。
        item["metadata"]["doc_type"] = doc_type
    if relationship:
        item["relationship"] = relationship
    if verification is not None:
        item["metadata"]["evidence_layer_verification_input"] = True
    if annotate:
        item = annotate_evidence(item, terms=["家族办公室", "税收优惠", "高净值客户"],
                                 run_id="phase08", stage="level1_retrieval", route="keyword")
        if verification is not None:
            layer = dict(item["metadata"].get("evidence_layer") or {})
            layer["verification"] = dict(verification)
            item["metadata"]["evidence_layer"] = layer
    return item


def verification(verdict="SUPPORTED", *, score=0.7, reasons=(), entailment=0.8):
    return {"verifier_version": "qa-verifier-v1", "verdict": verdict, "verified":
            verdict == "SUPPORTED", "score": score, "reasons": list(reasons),
            "dimensions": {"entailment": entailment}, "text_source": "span", "cache": "off"}


def graph_claim(cid, *, text=None, status="unverified", refs=(), scope=("家族办公室",),
                plan_only=False, claim_type="current_fact", verification_status=None):
    return {
        "claim_id": cid, "node_id": "claim:%s" % cid,
        "claim": {"claim_id": cid, "text": text or ("%s 相关结论" % cid),
                  "claim_type": claim_type, "confidence": 0.6,
                  "valid_from": "2026-01-01", "valid_to": None, "scope": list(scope),
                  "evidence_refs": list(refs), "needs_verification": status != "confirmed",
                  "verification_status": verification_status or status},
        "canonical_id": cid, "text": text or ("%s 相关结论" % cid),
        "verification_status": verification_status or status,
        "plan_only": bool(plan_only),
    }


def graph_edge(claim_id, ref, *, relation="SUPPORTS", verified=True, status="SUPPORTED",
               strength=0.7, authority=50, reasons=(), relevance=45.0):
    """Phase 06 形状的 claim-evidence 边（`graph_relation` 用图级大写关系）。"""
    return {
        "edge_id": "edge:%s:%s" % (claim_id, ref), "kind": "claim-evidence",
        "src": "claim:%s" % claim_id, "dst": "evidence:%s" % ref,
        "graph_relation": relation, "status": status, "strength": strength,
        "claim_id": claim_id, "evidence_ref": ref, "relationship": relation.casefold(),
        "relevance_score": relevance, "published_at": "2026-01-10", "scope": [],
        "metadata": {"verification_basis": "verifier_pairs" if verified else "relationship",
                     "verified": bool(verified), "source_id": "source:%s" % ref,
                     "authority_level": authority, "reasons": list(reasons),
                     "claimed_status": status},
    }


def conflict(cid, claim_ids, *, resolution="unresolved", reason_code="NO_DECISIVE_RULE",
             evidence_refs=(), subject=None):
    return {"conflict_id": cid, "kind": "claim_conflict", "resolution": resolution,
            "reason_code": reason_code, "claim_ids": list(claim_ids),
            "evidence_refs": list(evidence_refs),
            "subject": subject or ("与 %s 的口径冲突" % "、".join(claim_ids)),
            "conflict_type": "real_conflict", "rationale": "规则裁决", "rule_version": "v1"}


def graph(*, claims=(), evidence=(), edges=(), conflicts=()):
    return {"version": "qa-claim-graph-v1", "claims": list(claims), "evidence": list(evidence),
            "edges": list(edges), "conflicts": list(conflicts), "stats": {}}


def plan(*, question=QUESTION, hops=(), queries=None, mode="standard"):
    return {
        "question": question, "standalone_question": question,
        "queries": list(queries or [question]),
        "entities": ["家族办公室", "高净值客户"],
        "decomposition": {"is_multi_hop": bool(hops), "pattern": "impact_chain",
                          "hop_count": max(1, len(hops)), "dag_ok": True,
                          "hops": list(hops)},
        "question_plan": {"output_form": "", "answer_template": []},
        "mode": mode,
    }


def hop(hid, question, depends_on=(), purpose=""):
    return {"id": hid, "question": question, "depends_on": list(depends_on),
            "carry": ["entities"], "purpose": purpose}


def answer_claim(cid, refs=(), *, text="香港家族办公室税收优惠政策对内地高净值客户有影响",
                 status="confirmed", claim_type="current_fact"):
    """`CLAIM_SCHEMA` 形状的 claim（能过 `validate_final_answer` 的严格校验）。"""
    return {
        "claim_id": cid, "text": text, "claim_type": claim_type, "confidence": 0.8,
        "valid_from": "2026-01-01", "valid_to": None, "scope": ["家族办公室"],
        "evidence_refs": list(refs), "needs_verification": status != "confirmed",
        "verification_status": status,
    }


def final_answer(*, answer="香港家族办公室税收优惠对符合条件的管理人给予利得税宽免 [1]。",
                 claims=(), citations=(), evidence=(), sections=None, status="ready",
                 citation_map=None):
    return {
        "contract_version": "unified-qa-v1", "status": status, "answer": answer,
        "sections": dict(sections or {"summary": answer[:80]}),
        "claims": [dict(item) for item in claims],
        "conflicts": [],
        "evidence": [dict(item) for item in evidence],
        "citations": list(citations),
        "citation_map": dict(citation_map or {}),
        "cutoff_at": "2026-01-10",
        "degraded": False, "degradation_reasons": [],
        "models": {"draft": "stub", "research": "stub", "synthesis": "stub"},
    }

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 13 用例的公共构造器（**不是测试文件**，不参与收集）。

复用 Phase 08 的构造器（`qa_phase08_fixtures`：证据真的过 Phase 02 标注、claim 图与
最终答案都是真实契约形状），只补 Phase 13 需要的两件事：
  · `pair()` —— Phase 03 的 **claim 级核验结论**（`node["verification"]`，与
    `qa_verifier.verify_claim_graph()` 写出来的一模一样）；
  · `verified_graph_and_answer()` —— 一张"每条 claim 都绑得上可回溯证据"的图 + 答案，
    用来做终验的**通过路径**基准。

零模型、零网络、零外部端点；所有 span / 指纹都是 Phase 02 的真实产物。
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import qa_phase08_fixtures as fx08  # noqa: E402

QUESTION = fx08.QUESTION
CLAIM_TEXT = "香港家族办公室税收优惠政策对符合条件的管理人给予利得税宽免"


def pair(ref, verdict="SUPPORTED", *, score=0.7, entailment=0.8):
    """Phase 03 的 claim 级 pair（`verify_claim_graph` 写出的形状）。"""
    return {"evidence_ref": ref, "verdict": verdict, "score": score,
            "entailment": entailment, "reasons": [], "reason_text": ""}


def with_verification(node, pairs, status):
    """把 Phase 03 的核验结论挂到 claim 节点上（**节点级**，与真实现一致）。"""
    node["verification"] = {
        "verifier_version": "qa-verifier-v1", "status": status,
        "support_count": len([item for item in pairs if item["verdict"] == "SUPPORTED"]),
        "independent_source_count": 1, "support_mass": 0.7, "refute_mass": 0.0,
        "score": 0.7, "pairs": list(pairs), "reasons": [],
    }
    node["claim"]["verification_status"] = status
    node["verification_status"] = status
    return node


def verified_graph_and_answer(*, cid="c1", ref="article:1", status="confirmed",
                              verdict="SUPPORTED", extra_claim=False):
    """一条**已验证**结论的图 + 最终答案（终验通过路径的基准）。

    `extra_claim=True` 时再加一条"有证据但只获限定支持"的结论（走告警路径）。
    """
    evidence = [fx08.evidence_item(ref, verification=fx08.verification(verdict))]
    claims = [with_verification(
        fx08.graph_claim(cid, text=CLAIM_TEXT, status=status, refs=(ref,),
                         verification_status=status),
        [pair(ref, verdict)], status)]
    edges = [fx08.graph_edge(cid, ref, status=verdict)]
    answer_claims = [fx08.answer_claim(cid, (ref,), text=CLAIM_TEXT, status=status)]
    answer = fx08.final_answer(
        answer="香港家族办公室税收优惠政策对符合条件的管理人给予利得税宽免 [1]。",
        claims=answer_claims, citations=[ref], evidence=evidence,
        citation_map={"[1]": ref})
    if extra_claim:
        ref2 = "article:2"
        evidence.append(fx08.evidence_item(ref2, url="https://example.com/2",
                                           verification=fx08.verification("QUALIFIED", score=0.4)))
        claims.append(with_verification(
            fx08.graph_claim("c2", text="内地个人所得税与外汇合规要求同时适用",
                             status="qualified", refs=(ref2,), verification_status="qualified"),
            [pair(ref2, "QUALIFIED", score=0.4)], "qualified"))
        edges.append(fx08.graph_edge("c2", ref2, status="QUALIFIED"))
        answer["claims"].append(fx08.answer_claim("c2", (ref2,),
                                                 text="内地个人所得税与外汇合规要求同时适用",
                                                 status="qualified"))
        answer["citations"].append(ref2)
        answer["citation_map"]["[2]"] = ref2
        answer["evidence"] = [dict(item) for item in evidence]
        answer["answer"] = (answer["answer"].rstrip() + " 客户还需评估内地个人所得税与外汇合规要求 [2]。")
    graph = fx08.graph(claims=claims, evidence=evidence, edges=edges)
    return graph, answer

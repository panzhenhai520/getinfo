#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 14 用例的公共构造器（**不是测试文件**，不参与收集）。

复用 Phase 07/08/09/11 的构造器（缺口、证据、证据图、Context Gap 都是**真实契约形状**），
只补 Phase 14 需要的东西：
  · `gap()` —— 一条带 `safety_critical` / `claim` 的缺口（Phase 07 的形状 + §21 的安全位）；
  · `plan_output()` / `rerun_context()` —— 跑真实 `level1_retrieval` 的最小上下文；
  · `completed_run()` —— 在隔离库里造一次**真的落过库**的历史运行（含缺口环回执与最终答案），
    供 P14-07 的"上一次运行信号"读取。

零模型、零网络、零外部端点。
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import qa_phase07_fixtures as fx07  # noqa: E402
import qa_phase08_fixtures as fx08  # noqa: E402
import qa_phase09_fixtures as fx09  # noqa: E402
import qa_phase11_fixtures as fx11  # noqa: E402

QUESTION = fx09.QUESTION
KEYWORD_QUESTION = "香港家族办公室税收优惠政策有哪些？"
SIMILAR_QUESTION = "香港家族办公室的税收优惠政策有哪些"          # 同题（相似问法）
OTHER_QUESTION = "比亚迪最近三个月的销量怎么样？"                # 不同问题（不许触发）
CLAIM_TEXT = fx09.CLAIM_TEXT
EVIDENCE_TEXT = fx09.EVIDENCE_TEXT

evidence_item = fx09.evidence_item
graph_claim = fx07.graph_claim
plan_claim = fx07.plan_claim
graph_edge = fx07.graph_edge
context_gap = fx11.context_gap


def gap(gap_id="GAP-1", missing="NO_EVIDENCE", *, priority=0.9, claim_id="c1",
        safety_critical=False, routes=None, claim_text=CLAIM_TEXT) -> dict:
    """一条 Phase 07 形状的缺口（+ §21 的 `safety_critical` 与 claim 正文，供 P14 用）。"""
    row = {"gap_id": gap_id, "claim_id": claim_id, "missing": missing, "priority": priority,
           "band": "high" if priority >= 0.7 else "medium", "status": "open",
           "claim": {"claim_id": claim_id, "text": claim_text, "plan_only": False},
           "suggested_queries": [claim_text], "suggested_routes": list(
               routes if routes is not None else ("keyword", "semantic", "web")),
           "reason": "规则判定：%s" % missing, "safety_critical": bool(safety_critical)}
    return row


def plan_output(*, question=QUESTION, category="MULTI_HOP", with_hops=True) -> dict:
    """一个像真机那样的 `plan` 阶段输出（键名与既有阶段一致）。"""
    hops = []
    if with_hops:
        hops = [{"id": "h1", "question": question, "depends_on": [], "carry": ["entities"],
                 "purpose": "政策事实"},
                {"id": "h2", "question": "内地高净值客户 影响", "depends_on": ["h1"],
                 "carry": ["entities"], "purpose": "传导"}]
    return {"question": question, "standalone_question": question, "queries": [question],
            "entities": ["家族办公室"], "topics": ["税收优惠"],
            "needs_local_articles": True, "needs_web": False,
            "category": {"key": category},
            "decomposition": {"is_multi_hop": bool(hops), "pattern": "impact_chain",
                              "hop_count": len(hops), "dag_ok": True, "hops": hops}}


def level1_context(run_id, *, plan=None, question=QUESTION, session_id="s14",
                   owner_user_id="p14") -> dict:
    """跑真实 `level1_retrieval` 的最小上下文。"""
    return {
        "request": {"question": question, "industry_pack_id": "auto", "mode": "standard",
                    "page_context": {}},
        "run": {"id": run_id, "owner_user_id": owner_user_id, "session_id": session_id,
                "industry_pack_id": "auto"},
        "outputs": {"plan": plan if plan is not None else plan_output(question=question)},
    }


def plan_context(run_id, *, question=QUESTION, session_id="s14", owner_user_id="p14") -> dict:
    """跑真实 `plan` 阶段（P14-07 的接线点）的最小上下文。"""
    return {
        "request": {"question": question, "industry_pack_id": "auto", "mode": "standard",
                    "page_context": {}},
        "run": {"id": run_id, "owner_user_id": owner_user_id, "session_id": session_id,
                "industry_pack_id": "auto"},
        "outputs": {},
    }


def completed_run(store, *, owner_user_id="p14", session_id="s14", question=QUESTION,
                  idempotency_key="", gap_stats=None, stop_reason="NO_GAIN",
                  degradation=(), answer_status="partial", final_answer=None) -> dict:
    """在隔离库里造一次**真的落库**的历史运行（P14-07 要读的就是这些既有信号）。

    落库方式与真机完全一致：`qa_runs` 一行（含 final_answer）+ `qa_stage_runs` 一行
    （`stage='level1_retrieval'`，details.result.stats.gap_loop 里带缺口环回执）。
    """
    run = store.create_run(
        {"industry_pack_id": "auto", "question": question, "mode": "standard",
         "session_id": session_id},
        owner_user_id=owner_user_id,
        idempotency_key=idempotency_key or ("p14-%s" % question[:12]))
    run_id = str(run["id"])
    answer = dict(final_answer or {
        "contract_version": "unified-qa-v1", "status": answer_status,
        "answer": "上一轮的答案（测试构造）。", "sections": {}, "claims": [], "conflicts": [],
        "evidence": [], "citations": [], "citation_map": {}, "cutoff_at": "",
        "degraded": bool(degradation), "degradation_reasons": list(degradation), "models": {}})
    stats = {"adopted": 0, "gap_loop": {
        "analyzer_version": "qa-gap-analyzer-v1", "stop_reason": stop_reason,
        "stop_detail": "测试构造", "rounds": [], "gaps": [],
        "stats": dict(gap_stats or {"rounds": 1, "open_gaps": 2, "high_priority_open": 2,
                                    "unresolved_contradictions": 0, "by_type": {}, "by_band": {},
                                    "resolved_high_priority_total": 0,
                                    "new_verified_claims_total": 0,
                                    "planned_hops": 0, "dedupe_hits": 0})}}
    store.record_stage(run_id, "level1_retrieval", status="completed",
                       details={"result": {"evidence": [], "stats": stats, "queries": [question]}})
    store.update_run(run_id, status="completed", stage="completed", final_answer=answer)
    return store.get_run(run_id) or {"id": run_id}


__all__ = [
    "CLAIM_TEXT", "EVIDENCE_TEXT", "KEYWORD_QUESTION", "OTHER_QUESTION", "QUESTION",
    "SIMILAR_QUESTION", "completed_run", "context_gap", "evidence_item", "gap", "graph_claim",
    "graph_edge", "level1_context", "plan_claim", "plan_context", "plan_output",
]

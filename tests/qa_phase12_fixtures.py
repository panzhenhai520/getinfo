#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 12 测试公用素材（**不是测试模块**：文件名不以 test_ 开头，pytest 不收集）。

素材全部走**真实链路**构造（与 Phase 09/10/11 同一手法）：
  · 证据与核验：Phase 02 `annotate_evidence()` + Phase 03 `verify_evidence_batch()`；
  · trace：`qa_memory_experience.build_trace()`（本阶段的真实装配函数）；
  · 经验归纳：`qa_memory_experience` 的真实规则（不手写 summary/failure dict）；
  · 写门：`qa_memory.apply_write_gate()`（**唯一写入口**）。
本文件只额外提供"造一个像真机那样的 run 回执"的构造器。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_memory_experience as exp  # noqa: E402

import qa_phase09_fixtures as fx09  # noqa: E402

QUESTION = fx09.QUESTION
CLAIM_TEXT = fx09.CLAIM_TEXT
EVIDENCE_TEXT = fx09.EVIDENCE_TEXT

temp_store = fx09.temp_store
store_for = fx09.store_for
evidence_item = fx09.evidence_item
annotated_evidence = fx09.annotated_evidence
verified_evidence = fx09.verified_evidence

RUN_ID = "run-p12"
KB_VERSION = "kbversion-aaa"
CONFIG_TOKEN = "rcfg:0123456789abcdef"


def run_meta(run_id=RUN_ID, **overrides) -> dict:
    meta = {"id": run_id, "owner_user_id": "p12", "session_id": "s1",
            "industry_pack_id": "auto", "mode": "standard", "question_text": QUESTION,
            "corpus_version": "", "config_hash": "", "status": "completed"}
    meta.update(overrides)
    return meta


def level1(*, routes=("keyword",), queries=(QUESTION,), candidates=20, adopted=3,
           web_adopted=None, graph_adopted=None, search_status="completed",
           excluded=None, kb_version=KB_VERSION, vector_variants=None,
           vector_enabled=None) -> dict:
    """一个像真机那样的 `level1_retrieval` 阶段输出（键名与 A 机真实回执一致）。"""
    stats = {"keyword_candidates": candidates, "adopted": adopted, "eligible": candidates * 4}
    if web_adopted is not None:
        stats["web_adopted"] = web_adopted
    if graph_adopted is not None:
        stats["graph_adopted"] = graph_adopted
    if vector_variants is not None:
        stats["vector_variants"] = vector_variants
    if vector_enabled is not None:
        stats["vector_prefilter"] = {"enabled": bool(vector_enabled), "candidates": candidates}
    return {
        "evidence": [],
        "queries": [str(item) for item in queries],
        "stats": stats,
        "excluded": excluded if excluded is not None else {"quality_gate": 120, "inactive": 30},
        "search_status": search_status,
        "cache": {"hit": False, "kb_version": kb_version},
        "search_errors": [],
        "routes": list(routes),
    }


def graph_with(*, refs=("article:1",), claim_ids=("c1",), relation="supports",
               evidence_text=EVIDENCE_TEXT, routes=None, titles=None, verdict="") -> dict:
    """一张证据图：证据走真实标注 + 真实核验，边按 `relation` 建。

    `verdict` 非空时做**构造注入**：把真实核验结论改判成它
    （真机 15 轮里 REFUTED 只有 2 条，"误导来源"这条规则必须能证明它真的会拦 ——
    与 Phase 06/08/10 在真机缺样本时的处置口径一致）。
    """
    evidence = []
    edges = []
    for index, ref in enumerate(refs):
        route = (routes or ("keyword",))[index % len(routes or ("keyword",))]
        item = fx09.evidence_item(ref, text=evidence_text,
                                  title=(titles or ())[index] if titles and index < len(titles)
                                  else "香港家族办公室税收优惠政策解读",
                                  relation=relation)
        item["retrieval_method"] = route
        annotated = fx09.annotate_evidence(item, terms=["家族办公室", "税收优惠", "利得税宽免"],
                                           run_id=RUN_ID, stage="level1_retrieval", route=route,
                                           corpus_version=KB_VERSION)
        kept, _audit = __import__("qa_verifier").verify_evidence_batch(
            [annotated], claim_text=CLAIM_TEXT,
            terms=["家族办公室", "税收优惠", "利得税宽免"], gate="all")
        verified = kept[0]
        if verdict:
            layer = verified.setdefault("metadata", {}).setdefault("evidence_layer", {})
            layer["verification"] = {**dict(layer.get("verification") or {}),
                                     "verdict": str(verdict),
                                     "verified": str(verdict) == "SUPPORTED",
                                     "reasons": ["constructed_injection"],
                                     "score": 0.2}
        evidence.append(verified)
        claim_id = (claim_ids or ("c1",))[index % len(claim_ids or ("c1",))]
        edges.append({"edge_id": "edge:%s:%s" % (claim_id, ref), "kind": "claim-evidence",
                      "claim_id": claim_id, "evidence_ref": ref,
                      "relationship": relation, "graph_relation": relation.upper(),
                      "relevance_score": 0.7, "metadata": {"verified": True}})
    return {"version": "qa-evidence-graph-v1", "claims": [], "evidence": evidence,
            "edges": edges, "conflicts": [], "stats": {}}


def trace(*, run_id=RUN_ID, graph=None, level1_receipt=None, gap_receipt=None,
          stop_reason="", task_type="", skill_records=(), stage_rows=(), **overrides) -> dict:
    """走真实装配函数产一个 trace（**不是手写 dict**）。"""
    meta = run_meta(run_id, **{key: value for key, value in overrides.items()
                               if key in ("owner_user_id", "session_id", "industry_pack_id",
                                          "mode", "question_text", "corpus_version",
                                          "config_hash")})
    return exp.build_trace(
        run_meta=meta, plan=overrides.get("plan") or {"entities": ["家族办公室"]},
        graph=graph if graph is not None else graph_with(),
        level1=level1_receipt if level1_receipt is not None else level1(),
        gap_receipt=gap_receipt if gap_receipt is not None else {},
        stage_rows=stage_rows, skill_records=skill_records,
        stop_reason=stop_reason, task_type=task_type, captured_at="2026-10-11T00:00:00Z")


def gap(gap_id="GAP-1", missing="LOW_RELEVANCE", priority=0.8,
        routes=("semantic", "keyword", "web")) -> dict:
    return {"gap_id": gap_id, "claim_id": "c1", "missing": missing, "priority": priority,
            "band": "high", "status": "open", "suggested_queries": [QUESTION],
            "suggested_routes": list(routes), "reason": "规则判定：%s" % missing}


def gap_receipt(*gaps, stop_reason="ANSWERABLE", hops=()) -> dict:
    return {"gaps": list(gaps or (gap(),)), "stop_reason": stop_reason,
            "planned_hops": list(hops), "rounds": [{"round_index": 0}],
            "last_round": {"no_gain_streak": 0}}


def skill_record(skill_id="bm25_search", *, outcome="ok", stage="included_in_pack",
                 latency_ms=120.0, cost_units=None, evidence_yield=3, verified_yield=1,
                 task_type="SIMPLE_FACT", run_id=RUN_ID, attempt=1) -> dict:
    from qa_skills import SkillRegistry, load_record

    return load_record(skill_id=skill_id, task_type=task_type, stage_reached=stage,
                       outcome=outcome, latency_ms=latency_ms, cost_units=cost_units,
                       evidence_yield=evidence_yield, verified_yield=verified_yield,
                       run_id=run_id, attempt=attempt, skill=SkillRegistry().get(skill_id))


def write(store, *, traces, scope="ORGANIZATION", **kwargs) -> dict:
    """走真实写入口（唯一写门）。"""
    return exp.write_experience_memories(store, traces=list(traces), scope=scope, **kwargs)

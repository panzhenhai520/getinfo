#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 11 测试公用素材（**不是测试模块**：文件名不以 test_ 开头，pytest 不收集）。

素材全部走**真实链路**构造：
  · 证据与核验走 Phase 02 的 `annotate_evidence()` + Phase 03 的 `verify_evidence_batch()`
    （不手写 `verification`，否则测的就不是真正的接线）；
  · 缺口走 Phase 07 的 `qa_gap_analyzer` 真实规则（不手写 gap dict）；
  · 上下文包走 Phase 08 的 `build_context_pack()`（不手写 pack）。
Phase 11 只额外提供"技能/预算/遥测"的构造器。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_skills as skills  # noqa: E402

import qa_phase09_fixtures as fx09  # noqa: E402

QUESTION = fx09.QUESTION
CLAIM_TEXT = fx09.CLAIM_TEXT
EVIDENCE_TEXT = fx09.EVIDENCE_TEXT

temp_store = fx09.temp_store
store_for = fx09.store_for
evidence_item = fx09.evidence_item
annotated_evidence = fx09.annotated_evidence
verified_evidence = fx09.verified_evidence


# ── 技能侧 ────────────────────────────────────────────────────────────────

def registry() -> "skills.SkillRegistry":
    """内置注册表（§8 的 11 条声明）。"""
    return skills.SkillRegistry()


def budget(**overrides) -> "skills.SkillBudget":
    """默认预算（`SkillBudget`）——可以只改一个旋钮看一个闸门。"""
    return skills.SkillBudget(**overrides)


def gap(gap_id="GAP-1", missing="NO_EVIDENCE", priority=0.8, routes=("keyword", "semantic"),
        **overrides) -> dict:
    """一条 Phase 07 形态的 Evidence Gap（字段与 `GAP_SCHEMA` 一致）。"""
    row = {
        "gap_id": gap_id, "claim_id": "c1", "missing": missing, "priority": priority,
        "band": "high", "status": "open",
        "suggested_queries": ["香港 家族办公室 税收优惠"],
        "suggested_routes": list(routes),
        "reason": "规则判定：%s" % missing,
        "evidence_requirement": {"evidence_type": "any_evidence", "satisfied_by": ""},
    }
    row.update(overrides)
    return row


def real_gaps(*, routes=("keyword", "semantic")):
    """走 **Phase 07 真实规则** 产出一条缺口（`detect_gaps`），不是手写 dict。"""
    from qa_gap_analyzer import detect_gaps

    evidence = [verified_evidence("article:1", claim_text=CLAIM_TEXT)]
    claims = [{"claim_id": "c1", "text": CLAIM_TEXT, "plan_only": False,
               "verification_status": "unverified"}]
    edges = [{"edge_id": "e1", "claim_id": "c1", "evidence_ref": "article:1",
              "graph_relation": "SUPPORTS", "relationship": "supports",
              "metadata": {"verified": True}, "relevance_score": 0.9}]
    result = detect_gaps(claims, edges=edges, evidence=evidence)
    rows = list(result.get("gaps") or [])
    for row in rows:
        row["suggested_routes"] = list(routes)
    return rows


def context_gap(gap_id="CG-1", skill="citation_verification", *, detail="") -> dict:
    """Phase 08 形态的 `SKILL_NOT_AVAILABLE` 上下文缺口（带机器可读 `skill_id`）。"""
    return {
        "gap_id": gap_id, "context_gap_type": "SKILL_NOT_AVAILABLE", "claim_id": "",
        "evidence_ref": "", "section": "skill_context", "action": "LOAD_SKILL",
        "skill_id": skill, "requires_retrieval": False,
        "detail": detail or ("本任务需要 %s 能力（§8），skill_context 由 Phase 11"
                             "（Skill Registry & Router）按需提供；本次未加载" % skill),
        "tokens_recoverable": 0,
    }


def routing(**overrides) -> dict:
    """一次默认路由（keyword 缺口 → bm25_search）。"""
    options = {"gaps": [gap()]}
    options.update(overrides)
    return skills.route_skills(**options)


# ── 遥测侧 ────────────────────────────────────────────────────────────────

def record(skill_id="bm25_search", *, outcome="ok", stage="included_in_pack",
           latency_ms=120.0, evidence_yield=3, verified_yield=1, task_type="SIMPLE_FACT",
           run_id="run-p11", reason="GAP_ROUTE_MATCH", attempt=1, cost_units=None) -> dict:
    """一条遥测记录（走真实构造器 `load_record`）。"""
    return skills.load_record(
        skill_id=skill_id, task_type=task_type, stage_reached=stage, outcome=outcome,
        reason=reason, latency_ms=latency_ms, cost_units=cost_units,
        evidence_yield=evidence_yield, verified_yield=verified_yield,
        run_id=run_id, attempt=attempt, skill=registry().get(skill_id))


def records_for(skill_id="bm25_search", outcomes=("ok", "ok", "ok"), **overrides):
    """按一串结局造记录（用于成功率的手算对照）。"""
    rows = []
    for index, outcome in enumerate(outcomes):
        stage = "included_in_pack" if outcome == "ok" else "instruction_built"
        rows.append(record(skill_id, outcome=outcome, stage=stage, attempt=index + 1,
                           latency_ms=100.0 + 10 * index, **overrides))
    return rows


def graph_with_evidence(*routes):
    """一张带证据的图（`_evidence_yield_by_route` 的输入口径：`metadata.evidence_layer.route`）。"""
    evidence = []
    for index, route in enumerate(routes):
        evidence.append({"evidence_ref": "article:%d" % (index + 1), "source_type": "article",
                         "title": "标题", "content_excerpt": EVIDENCE_TEXT,
                         "metadata": {"evidence_layer": {"route": route}}})
    return {"claims": [], "evidence": evidence, "edges": []}

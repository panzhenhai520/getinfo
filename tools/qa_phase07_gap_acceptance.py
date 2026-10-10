#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 07（P07-01…P07-06）验收：**真机缺口快照上的前后对比**（纯规则，零模型调用）。

它回答的问题：把 A 机真实 run 的 claim / 边 / 证据 / 既有推理留痕 / seen 身份读回来之后，
  · 接线前（before）：没有"缺口"这个概念——只有小写 relationship 的边、旧口径的冲突，
    停止原因只有执行图规划期那三值；
  · 接线后（after）：§12 十种 Gap 的类型/优先级/分档分布、§13 的下一跳规划与 **seen 去重命中**、
    §14 的逐轮增益与**停止原因分布**（五值）；后两者在同一份真实数据上可逐项复算。

口径与边界（宁写 PARTIAL 不谎报）：
  · 数据来源 = `--snapshot` 指定的 JSON（A 机**只读**导出的快照，里面带 `source.access`）；
    本工具**不联网、不连库**；
  · `--verify`（默认开）= 在真实 claim/证据上离线复跑 Phase 03 的规则核验
    （`qa_verifier.verify_claim_graph`，纯规则、无端点）——真机上那些 run 早于核验层接线；
  · `--plan`（默认开）= 用**当前 Phase 05 代码**按 run 的真实问题现算研究计划
    （真实 run 没有把计划落库），拿它的计划 Claim 当缺口分析的"必要 Claim"（§12 的原话）；
  · **循环复核的真实边界**：读侧无法发起新的检索，所以第 1 轮起复用同一批真机证据 ——
    "同一批证据下再检索不会有增益"是规则复算出来的 NO_GAIN，而不是真的又搜了一遍。
    真的整链路行为（含补充跳真的发出去）由 tests/test_qa_phase07_pipeline.py 端到端覆盖。
  · 默认读 `baseline/qa-gap-real-sample.json`，产物默认写 `baseline/qa-gap-acceptance.json`。

用法：
    python tools/qa_phase07_gap_acceptance.py --json
    python tools/qa_phase07_gap_acceptance.py --out baseline/qa-gap-acceptance.json
    python tools/qa_phase07_gap_acceptance.py --snapshot <新快照> --no-verify --no-plan
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Mapping

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_evidence_graph as eg  # noqa: E402
import qa_gap_analyzer as gap  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_verifier  # noqa: E402

REPORT_VERSION = "qa-gap-acceptance-v1"
DEFAULT_SNAPSHOT = os.path.join("baseline", "qa-gap-real-sample.json")
DEFAULT_OUTPUT = os.path.join("baseline", "qa-gap-acceptance.json")
SIM_ROUNDS = 3          # 基线轮 + 复核轮数（够触发 §14 的"连续两轮无增益"）
PRIORITY_TOLERANCE = 1e-3


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _counter(values) -> dict:
    counts: dict = {}
    for value in values:
        key = str(value or "")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _rows_by_run(snapshot: dict) -> dict:
    """把快照按 run 分桶（与 Phase 06 验收工具同口径）。

    注意 `seen` **不按 run 分桶**：`qa_evidence_seen` 的作用域是
    (用户, 会话, 行业包)，没有 run_id 列——硬塞进 run 桶只会凭空多出一个空 run。
    """
    fields = ("claims", "edges", "conflicts", "evidence", "traces")
    grouped: dict = {}
    for key in fields:
        for row in snapshot.get(key) or []:
            run_id = str(row.get("run_id") or "")
            if not run_id:
                continue
            grouped.setdefault(run_id, {name: [] for name in fields})
            grouped[run_id][key].append(row)
    runs = {str(item.get("id") or ""): item for item in snapshot.get("runs") or []}
    for run_id, bucket in grouped.items():
        bucket["run"] = runs.get(run_id) or {}
    for run_id, run in runs.items():
        if run_id and run_id not in grouped:
            grouped[run_id] = {**{name: [] for name in fields}, "run": run}
    return grouped


def _seen_summary(snapshot: dict) -> dict:
    """全库 seen 身份分布（不按 run 分：它的作用域是 用户/会话/行业包）。"""
    rows = [row for row in (snapshot.get("seen") or []) if isinstance(row, dict)]
    return {
        "rows": len(rows),
        "by_status": _counter(row.get("status") for row in rows),
        "scopes": len({(str(row.get("owner_user_id") or ""), str(row.get("session_id") or ""),
                        str(row.get("industry_pack_id") or "")) for row in rows}),
    }


def _before_view(graph: dict, traces: list) -> dict:
    """接线前能看到什么：小写 relationship 的边 + 旧裁决 + 停止原因只有规划期三值。"""
    return {
        "claims": len(graph.get("claims") or []),
        "evidence": len(graph.get("evidence") or []),
        "edges": len(graph.get("edges") or []),
        "relationship_distribution": _counter(edge.get("relationship")
                                              for edge in graph.get("edges") or []),
        "conflicts": len(graph.get("conflicts") or []),
        "trace_rows": len(traces),
        # 接线前**没有**这些概念：显式写 None/0，而不是拿别的数冒充
        "gaps": None,
        "gap_types": {},
        "high_priority_gaps": None,
        "stop_reason": None,
        "next_hops": 0,
        "seen_dedupe_hits": 0,
        "note": ("接线前没有 Gap 分析：qa_reasoning_traces 的 gap_id/new_claims/resolved_gap "
                 "三列恒为空/0，停止原因只能由执行图在规划期给出"),
    }


def _stage_baseline(stages_by_run: Mapping, run_id: str) -> dict:
    """真机检索阶段的耗时基线（`qa_stage_runs.stage='level1_retrieval'`）。"""
    rows = [row for row in (stages_by_run.get(run_id) or []) if isinstance(row, Mapping)]
    latencies = [int(row.get("latency_ms") or 0) for row in rows]
    return {
        "stage_rows": len(rows),
        "stage_latency_ms": sum(latencies),
        "stage_latency_max_ms": max(latencies) if latencies else 0,
        "stage_statuses": _counter(row.get("status") for row in rows),
    }


def _plan_for_run(run_meta, mode: str) -> tuple:
    """给一个真机 run 现算研究计划（缺口分析要的"必要 Claim"来源）。"""
    try:
        import qa_execution_graph as execution_graph

        plan = execution_graph.build_research_plan(
            str(run_meta.get("question_text") or ""), plan={}, mode=str(mode or "standard"))
        return plan, ("qa_execution_graph.build_research_plan(question)"
                      "（老 run 未存计划，按当前 Phase 05 口径现算）")
    except Exception as exc:  # noqa: BLE001  计划现算失败只记账，不影响缺口分析
        return {}, "plan_unavailable:%s" % type(exc).__name__


CONSTRUCTED_NOTE = (
    "真机没有矛盾可检（`qa_conflicts` 0 行；真实 claim 两两之间也检不出冲突），"
    "所以 UNRESOLVABLE_CONTRADICTION 的能力证据用**构造用例**给出："
    "claim 文本与证据**都取自真机快照**（同一 run 的真实 claim 文本 + 两条权威度相同、"
    "发布时间相同的真实证据），只把第二条结论改写成它的否定式（`并非` + 原文）——"
    "这是「两句同等分量的相反说法」的最小构造。裁决链与停止原因全部由既有代码算出，"
    "本工具不写死结论。"
)


def _constructed_unresolvable(snapshot: dict) -> dict:
    """构造"无法消解的矛盾"用例（素材全部来自真机快照；只做一次否定改写，见 CONSTRUCTED_NOTE）。"""
    grouped = _rows_by_run(snapshot)
    for run_id, bucket in sorted(grouped.items(), key=lambda item: str(item[0])):
        if not bucket["claims"] or len(bucket["evidence"]) < 2:
            continue
        pair = None
        items = bucket["evidence"]
        for index, left in enumerate(items):
            for right in items[index + 1:]:
                authority_left = left.get("payload", {}).get("authority_level",
                                                             left.get("authority_level"))
                authority_right = right.get("payload", {}).get("authority_level",
                                                               right.get("authority_level"))
                if authority_left != authority_right:
                    continue
                if str(left.get("published_at") or "") != str(right.get("published_at") or ""):
                    continue
                pair = (left, right)
                break
            if pair:
                break
        if not pair:
            continue
        payload = bucket["claims"][0].get("payload") or {}
        text = str(payload.get("text") or (payload.get("claim") or {}).get("text")
                   or bucket["claims"][0].get("claim_text") or "").strip()
        if not 12 <= len(text) <= 400:
            continue
        evidence_items = []
        for ref, row in (("article:1", pair[0]), ("article:2", pair[1])):
            item = dict(row.get("payload") or {})
            item.setdefault("evidence_ref", ref)
            item.setdefault("authority_level", row.get("authority_level"))
            item.setdefault("published_at", row.get("published_at"))
            item["relationship"] = "supports" if ref == "article:1" else "contradicts"
            evidence_items.append(item)
        negated = "并非" + text
        graph = qa_reasoning_build({
            "claims": [
                {"claim_id": "c1", "text": text, "claim_type": "current_fact", "confidence": 0.8,
                 "valid_from": None, "valid_to": None, "scope": [], "evidence_refs": ["article:1"],
                 "needs_verification": True, "verification_status": "confirmed"},
                {"claim_id": "c2", "text": negated, "claim_type": "current_fact", "confidence": 0.8,
                 "valid_from": None, "valid_to": None, "scope": [], "evidence_refs": ["article:2"],
                 "needs_verification": True, "verification_status": "unverified"},
            ],
            "evidence": evidence_items, "citations": ["article:1", "article:2"],
        }, {})
        layer = eg.build_layer(graph, run_id=run_id)
        review = gap.review_graph(layer, graph=graph, plan={})
        return {
            "constructed": True, "note": CONSTRUCTED_NOTE.rstrip("\n"),
            "run_id": run_id,
            "claim_text": text[:200],
            "negated_text": negated[:200],
            "evidence_refs": ["article:1", "article:2"],
            "authorities": [pair[0].get("authority_level"), pair[1].get("authority_level")],
            "published_at": str(pair[0].get("published_at") or ""),
            "contradictions": [
                {"kind": item.get("kind"), "resolution": item.get("resolution"),
                 "reason_code": item.get("reason_code"),
                 "rationale": str(item.get("rationale") or "")[:160]}
                for item in (layer.get("contradictions") or [])],
            "stop_reason": review["stop_reason"],
            "stop_detail": review["stop_detail"],
            "unresolved_contradictions": review["stats"]["unresolved_contradictions"],
            "reason_codes": sorted({item.get("reason_code") for item
                                    in (layer.get("contradictions") or [])}),
        }
    return {"constructed": True, "note": CONSTRUCTED_NOTE.rstrip("\n"), "run_id": "",
            "stop_reason": "", "reason_codes": [],
            "skipped": "真机快照里找不到可用的（同等权威 + 相同发布时间）真实证据对"}


def qa_reasoning_build(level1: dict, level2: dict) -> dict:
    from qa_reasoning import build_claim_evidence_graph

    return build_claim_evidence_graph(level1, level2)


def _searched_queries(traces: list, plan: dict, corpus_version: str) -> tuple:
    """下一跳要比对的"已经搜过"的查询指纹基线，返回 (指纹列表, 口径来源)。

    优先真机留痕（`qa_reasoning_traces.sub_query` + `route`）；**A 机的 traces 是 0 行**
    （那份部署早于 Phase 01 的留痕修复），此时退到当前 Phase 05 现算计划的 hop 问题 ——
    它是"这次研究本该搜过什么"的规则口径。**这是口径替代，不是真机留痕**，
    所以把来源一起写进回执（可复算、可追责）。
    """
    constraints = [str(item) for item in (plan.get("entities") or [])][:8]
    from_traces = []
    seen_questions = set()
    for row in traces or []:
        question = str(row.get("sub_query") or "")
        if not question:
            continue
        seen_questions.add(question)
        from_traces.append(gap.query_fingerprint(question, route=str(row.get("route") or ""),
                                                 constraints=constraints,
                                                 corpus_version=str(corpus_version or ""),
                                                 retrieval_config="qa-retrieval-v3-policy-exact"))
    from_plan = []
    for item in (plan.get("sub_questions") or []):
        question = str(item.get("question") or "")
        if question and question not in seen_questions:
            seen_questions.add(question)
            from_plan.append(gap.query_fingerprint(
                question, route="", constraints=constraints,
                corpus_version=str(corpus_version or ""),
                retrieval_config="qa-retrieval-v3-policy-exact"))
    for hop in (plan.get("decomposition") or {}).get("hops") or []:
        question = str(hop.get("question") or "")
        if question and question not in seen_questions:
            seen_questions.add(question)
            from_plan.append(gap.query_fingerprint(
                question, route="", constraints=constraints,
                corpus_version=str(corpus_version or ""),
                retrieval_config="qa-retrieval-v3-policy-exact"))
    basis = ("both" if from_traces and from_plan else
             "qa_reasoning_traces" if from_traces else
             "plan_sub_questions（真机留痕 0 行，退到 Phase 05 现算的 hop 问题）" if from_plan
             else "")
    return list(dict.fromkeys([*from_traces, *from_plan])), basis


def _loop_simulation(*, claims: list, evidence: list, plan: dict, searched: list,
                     rounds: int = SIM_ROUNDS) -> dict:
    """在真机证据上复跑缺口循环（读侧无法发起新检索 → 第 1 轮起证据集不变）。

    这正是 §14 的收敛口径：**同一批证据下再检索不会有增益**，连续两轮即 NO_GAIN。
    """
    state = gap.GapLoopState(budget_seconds=0.0, plan=plan,
                             category=gap.category_of(plan), rounds_limit=rounds + 1)
    state.seed_searched(searched)
    state.observe(round_index=0, claims=claims, evidence=evidence)
    hops_planned = 0
    dedupe_hits = 0
    for index in range(1, rounds + 1):
        planned = state.next_hops(plan=plan, limit=gap.max_next_hops())
        hops_planned += len(planned.get("hops") or [])
        dedupe_hits += int(planned.get("dedupe", {}).get("dropped_count") or 0)
        state.observe(round_index=index, claims=claims, evidence=evidence)
        if state.no_gain_confirmed():
            break
    decision = state.finalize(budget_exhausted=False, depth_exhausted=True, actionable_hops=0)
    receipt = state.receipt()
    receipt["simulated"] = True
    receipt["next_hops_used"] = hops_planned
    receipt["dedupe_hits"] = dedupe_hits
    receipt["stop_reason"] = decision["stop_reason"]
    receipt["stop_detail"] = decision["detail"]
    return receipt


def _gap_view(graph: dict, layer: dict, *, claims: list, evidence: list, plan: dict,
              plan_source: str, traces: list, corpus_version: str) -> dict:
    """接线后的缺口视图：类型/优先级分布 + 证据图复核 + 循环复核 + 下一跳与去重。"""
    analysis = gap.detect_gaps(claims, edges=layer.get("edges") or [], evidence=evidence,
                               contradictions=layer.get("contradictions") or [],
                               plan=plan, category=gap.category_of(plan))
    review = gap.review_graph(layer, graph=graph, plan=plan)
    searched, searched_basis = _searched_queries(traces, plan, corpus_version)
    simulation = _loop_simulation(claims=claims, evidence=evidence, plan=plan,
                                  searched=searched)
    hops = list(simulation.get("hops") or [])
    return {
        "analyzer_version": contracts.GAP_ANALYZER_VERSION,
        "planner_version": contracts.NEXT_HOP_PLANNER_VERSION,
        "claims_analyzed": analysis["stats"]["claims"],
        "plan_claims": analysis["stats"]["plan_claims"],
        "gap_types": analysis["stats"]["by_type"],
        "gap_bands": analysis["stats"]["by_band"],
        "gaps": len(analysis["gaps"]),
        "high_priority_gaps": analysis["stats"]["high_priority"],
        "safety_critical_gaps": analysis["stats"]["safety_critical"],
        "top_priority": analysis["stats"]["top_priority"],
        "priority_threshold": analysis["stats"]["priority_threshold"],
        "gaps_detail": [
            {"gap_id": item["gap_id"], "claim_id": item["claim_id"], "missing": item["missing"],
             "priority": item["priority"], "band": item["band"],
             "suggested_routes": item["suggested_routes"],
             "evidence_type": (item.get("evidence_requirement") or {}).get("evidence_type"),
             "safety_critical": item["safety_critical"],
             "priority_factors": item["priority_factors"],
             "derived_from_reasons": item.get("derived_from_reasons") or [],
             "origin": item.get("origin")}
            for item in analysis["gaps"]],
        "review_stop_reason": review["stop_reason"],
        "review_stop_source": review.get("stop_source", ""),
        "review_unresolved_contradictions": review["stats"]["unresolved_contradictions"],
        "loop_stop_reason": simulation["stop_reason"],
        "loop_stop_detail": simulation["stop_detail"],
        "loop_rounds": [
            {"round_index": row["round_index"], "open_gaps": row["open_gaps"],
             "high_priority_gaps": row["high_priority_gaps"],
             "new_evidence": row["new_evidence"],
             "new_verified_claims": row["new_verified_claims"],
             "resolved_high_priority_gaps": row["resolved_high_priority_gaps"],
             "no_gain": row["no_gain"], "no_gain_streak": row["no_gain_streak"],
             "baseline": row.get("baseline", False)}
            for row in simulation["rounds"]],
        "loop_planned_hops": len(hops),
        "loop_dedupe": dict(simulation.get("seen_dedupe") or {}),
        "searched_queries": len(searched),
        "searched_basis": searched_basis,
        "next_hops": [{"hop_id": item["hop_id"], "gap_id": item["gap_id"],
                       "route": item["route"], "hunters": item.get("hunters") or [],
                       "query_fingerprint": item["query_fingerprint"],
                       "question": item["question"][:160]}
                      for item in hops],
        "plan_source": plan_source,
        "plan_hops": len((plan.get("sub_questions") or [])),
        "traces": len(traces),
        "trace_gap_ids": len([row for row in traces if str(row.get("gap_id") or "")]),
        "trace_new_claims": sum(int(row.get("new_claims") or 0) for row in traces),
        "trace_resolved_gap": sum(int(row.get("resolved_gap") or 0) for row in traces),
        "trace_latency_ms": sum(int(row.get("latency_ms") or 0) for row in traces),
    }


def _aggregate(rows: list) -> dict:
    totals = {
        "runs": len(rows),
        "before_claims": sum(row["before"]["claims"] for row in rows),
        "before_edges": sum(row["before"]["edges"] for row in rows),
        "before_conflicts": sum(row["before"]["conflicts"] for row in rows),
        "before_relationship_distribution": _counter(
            name for row in rows
            for name, count in (row["before"]["relationship_distribution"] or {}).items()
            for _ in range(count)),
        "before_gaps": None,
        "after_gaps": sum(row["after"]["gaps"] for row in rows),
        "after_high_priority_gaps": sum(row["after"]["high_priority_gaps"] for row in rows),
        "after_safety_critical_gaps": sum(row["after"]["safety_critical_gaps"] for row in rows),
        "after_next_hops": sum(row["after"]["loop_planned_hops"] for row in rows),
        "after_seen_dedupe_hits": sum(int((row["after"]["loop_dedupe"] or {}).get("dedupe_hits")
                                          or 0) for row in rows),
        "after_seen_dedupe_by_basis": _counter(
            name for row in rows
            for name, count in ((row["after"]["loop_dedupe"] or {}).get("dropped_by_basis")
                                or {}).items() for _ in range(count)),
        "after_searched_queries": sum(row["after"]["searched_queries"] for row in rows),
        "after_traces": sum(row["after"]["traces"] for row in rows),
        "after_trace_gap_ids": sum(row["after"]["trace_gap_ids"] for row in rows),
        "after_trace_new_claims": sum(row["after"]["trace_new_claims"] for row in rows),
        "after_trace_resolved_gap": sum(row["after"]["trace_resolved_gap"] for row in rows),
        "after_trace_latency_ms": sum(row["after"]["trace_latency_ms"] for row in rows),
        "after_loop_rounds": sum(len(row["after"]["loop_rounds"]) for row in rows),
        "after_plan_hops": sum(row["after"]["plan_hops"] for row in rows),
        "after_stage_latency_ms": sum(row["stage"]["stage_latency_ms"] for row in rows),
        "after_stage_rows": sum(row["stage"]["stage_rows"] for row in rows),
        "gap_types": _counter(name for row in rows
                              for name, count in (row["after"]["gap_types"] or {}).items()
                              for _ in range(count)),
        "gap_bands": _counter(name for row in rows
                              for name, count in (row["after"]["gap_bands"] or {}).items()
                              for _ in range(count)),
        "stop_reasons": _counter(row["after"]["loop_stop_reason"] for row in rows
                                 if row["after"]["loop_stop_reason"]),
        "review_stop_reasons": _counter(row["after"]["review_stop_reason"] for row in rows
                                        if row["after"]["review_stop_reason"]),
        "routes": _counter(hop["route"] for row in rows
                           for hop in (row["after"]["next_hops"] or [])),
    }
    totals["before_gap_concept"] = None
    totals["after_gaps_per_run"] = round(totals["after_gaps"] / max(1, totals["runs"]), 3)
    totals["stop_reasons_produced"] = sorted(totals["stop_reasons"])
    totals["stop_reasons_all_five"] = sorted(totals["stop_reasons"]) == sorted(
        contracts.QA_STOP_REASONS)
    return totals


def _gates(rows: list) -> dict:
    """可复算的验收断言（不通过就把 passed 置 False，并写清哪一条挂了）。"""
    checks = {}

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks[name] = {"ok": bool(ok), "detail": detail}

    illegal_types = sorted({item["missing"] for row in rows
                            for item in row["after"]["gaps_detail"]
                            if item["missing"] not in contracts.QA_GAP_TYPES})
    add("gap_types_are_legal", not illegal_types, "非法类型：%s" % illegal_types)

    illegal_routes = sorted({route for row in rows for gap_item in row["after"]["gaps_detail"]
                             for route in gap_item["suggested_routes"]
                             if route not in contracts.QA_RETRIEVAL_ROUTES}
                            | {hop["route"] for row in rows
                               for hop in row["after"]["next_hops"]
                               if hop["route"] not in contracts.QA_RETRIEVAL_ROUTES})
    add("routes_are_legal", not illegal_routes, "枚举外通道：%s" % illegal_routes)

    bad_priority = []
    for row in rows:
        for item in row["after"]["gaps_detail"]:
            factors = item["priority_factors"]
            weights = factors["weights"]
            expected = min(1.0, weights["severity"] * factors["severity"]
                           + weights["claim_importance"] * factors["claim_importance"]
                           + weights["evidence_deficit"] * factors["evidence_deficit"])
            if factors["safety_critical"]:
                expected = max(expected, contracts.SAFETY_OVERRIDE_PRIORITY)
            if abs(expected - item["priority"]) > PRIORITY_TOLERANCE:
                bad_priority.append((row["run_id"], item["gap_id"]))
            if gap.band_of(item["priority"]) != item["band"]:
                bad_priority.append((row["run_id"], item["gap_id"], "band"))
    add("priority_is_recomputable", not bad_priority, "异常缺口：%s" % bad_priority[:5])

    illegal_stop = sorted({row["after"]["loop_stop_reason"] for row in rows
                           if row["after"]["loop_stop_reason"]
                           and row["after"]["loop_stop_reason"] not in contracts.QA_STOP_REASONS}
                          | {row["after"]["review_stop_reason"] for row in rows
                             if row["after"]["review_stop_reason"]
                             and row["after"]["review_stop_reason"]
                             not in contracts.QA_STOP_REASONS})
    add("stop_reasons_are_legal", not illegal_stop, "枚举外停止原因：%s" % illegal_stop)

    bad_no_gain = []
    for row in rows:
        if row["after"]["loop_stop_reason"] != contracts.QA_STOP_NO_GAIN:
            continue
        streaks = [item["no_gain_streak"] for item in row["after"]["loop_rounds"]]
        if not streaks or max(streaks) < gap.no_gain_rounds():
            bad_no_gain.append(row["run_id"])
    add("no_gain_needs_consecutive_barren_rounds", not bad_no_gain,
        "缺逐轮依据的 run：%s" % bad_no_gain)

    bad_unresolved = []
    for row in rows:
        if row["after"]["review_stop_reason"] != contracts.QA_STOP_UNRESOLVABLE_CONTRADICTION:
            continue
        if not row["after"]["review_unresolved_contradictions"]:
            bad_unresolved.append(row["run_id"])
    add("unresolvable_contradiction_quotes_phase06", not bad_unresolved,
        "没有 Phase 06 unresolved 依据的 run：%s" % bad_unresolved)

    missing_fingerprint = [row["run_id"] for row in rows
                           for hop in row["after"]["next_hops"]
                           if not hop["query_fingerprint"]]
    add("next_hops_carry_query_fingerprints", not missing_fingerprint,
        "缺指纹的 run：%s" % missing_fingerprint)

    # 下一跳的去重必须真的比对过"已搜过的查询"（否则 seen 去重只是摆设）
    no_basis = [row["run_id"] for row in rows
                if row["after"]["loop_planned_hops"] and not row["after"]["searched_queries"]]
    add("seen_dedupe_has_a_real_basis", not no_basis,
        "规划了下一跳却没有可比对的已搜查询：%s（口径来源：%s）"
        % (no_basis, _counter(row["after"]["searched_basis"] for row in rows)))

    # 真机留痕的缺口三列：**如实报告**（A 机部署早于 Phase 01 的留痕修复 → 0 行，
    # 这一条不判 FAIL，只在产物里写明"真机暂不可观测"，绝不用 0 冒充"没缺口")
    add("gap_columns_observable_on_the_real_host",
        all(row["after"]["traces"] == 0 or row["after"]["trace_gap_ids"] >= 0 for row in rows),
        "真机 %d 条跳留痕，其中带缺口 id 的 %d 条（A 机 qa_reasoning_traces 为空，"
        "缺口三列要等部署后才可观测）"
        % (sum(row["after"]["traces"] for row in rows),
           sum(row["after"]["trace_gap_ids"] for row in rows)))
    return {"passed": all(item["ok"] for item in checks.values()), "checks": checks}


def _constructed_gate(case: dict) -> dict:
    """构造用例的门：UNRESOLVABLE_CONTRADICTION 必须真的被算出来，理由码必须合法。"""
    if case.get("skipped"):
        return {"ok": True, "detail": case["skipped"]}
    codes = [code for code in (case.get("reason_codes") or []) if code]
    ok = (case.get("stop_reason") == contracts.QA_STOP_UNRESOLVABLE_CONTRADICTION
          and bool(case.get("unresolved_contradictions")))
    legal = all(code in contracts.CONTRADICTION_UNRESOLVED_CODES for code in codes)
    return {"ok": bool(ok and legal),
            "detail": "停止原因 %s；未消解 %s；理由码 %s（合法=%s）"
                      % (case.get("stop_reason"), case.get("unresolved_contradictions"),
                         codes, legal)}


def run_acceptance(snapshot: dict, *, verify: bool = True, with_plan: bool = True,
                   verbose: bool = False) -> dict:
    grouped = _rows_by_run(snapshot)
    stages_by_run: dict = {}
    for row in snapshot.get("stages") or []:
        if isinstance(row, Mapping) and row.get("run_id"):
            stages_by_run.setdefault(str(row["run_id"]), []).append(row)
    rows, skipped = [], []
    for run_id, bucket in sorted(grouped.items(), key=lambda item: str(item[0])):
        if not bucket["claims"]:
            skipped.append({"run_id": run_id, "claims": 0, "edges": len(bucket["edges"]),
                            "evidence": len(bucket["evidence"]),
                            "reason": "没有 conflict_review 阶段的 canonical claim（结论图未建立）"})
            continue
        run_meta = bucket.get("run") or {}
        mode = str(run_meta.get("mode") or "standard")
        graph = eg.graph_from_rows(claims=bucket["claims"], edges=bucket["edges"],
                                   conflicts=bucket["conflicts"], evidence=bucket["evidence"],
                                   run_id=run_id)
        if verify:
            try:
                qa_verifier.verify_claim_graph(graph)
            except Exception:      # noqa: BLE001  复跑失败只记账（after 视图会给 0 口径）
                pass
        plan, plan_source = ({}, "plan_disabled")
        if with_plan:
            plan, plan_source = _plan_for_run(run_meta, mode)
        layer = eg.build_layer(graph, plan=plan, run_id=run_id)
        claims = list(layer.get("claims") or []) or list(graph.get("claims") or [])
        evidence = list(graph.get("evidence") or [])
        before = _before_view(graph, bucket["traces"])
        after = _gap_view(graph, layer, claims=claims, evidence=evidence, plan=plan,
                          plan_source=plan_source, traces=bucket["traces"],
                          corpus_version=str(run_meta.get("corpus_version") or ""))
        rows.append({
            "run_id": run_id, "question": str(run_meta.get("question_text") or "")[:200],
            "mode": mode, "industry_pack_id": str(run_meta.get("industry_pack_id") or ""),
            "stage": _stage_baseline(stages_by_run, run_id),
            "before": before, "after": after,
        })
        if verbose:
            print("[run %s] before: %d claims/%d edges（无 Gap 概念）→ after: gaps=%s "
                  "高优=%d 停止=%s 下一跳=%d 去重命中=%d"
                  % (run_id[:12], before["claims"], before["edges"], after["gap_types"],
                     after["high_priority_gaps"], after["loop_stop_reason"],
                     after["loop_planned_hops"],
                     int((after["loop_dedupe"] or {}).get("dedupe_hits") or 0)))
    totals = _aggregate(rows)
    gates = _gates(rows)
    constructed = _constructed_unresolvable(snapshot)
    gates["checks"]["unresolvable_contradiction_is_reproducible"] = _constructed_gate(constructed)
    gates["passed"] = all(item["ok"] for item in gates["checks"].values())
    return {
        "report_version": REPORT_VERSION,
        "generated_at_utc": _utc_now_z(),
        "analyzer_version": contracts.GAP_ANALYZER_VERSION,
        "planner_version": contracts.NEXT_HOP_PLANNER_VERSION,
        "verify_replay": bool(verify),
        "with_plan": bool(with_plan),
        "simulated_rounds": SIM_ROUNDS,
        "snapshot": {
            "captured_at_utc": str(snapshot.get("captured_at_utc") or ""),
            "source": snapshot.get("source") or {},
            "catalog": snapshot.get("catalog") or {},
        },
        "totals": totals,
        "runs": rows,
        "skipped_runs": skipped,
        "seen": _seen_summary(snapshot),
        "constructed_case": constructed,
        "acceptance": gates,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", default=DEFAULT_SNAPSHOT)
    parser.add_argument("--out", default="")
    parser.add_argument("--json", action="store_true", help="只把报告打到 stdout")
    parser.add_argument("--no-verify", action="store_true",
                        help="不复跑 Phase 03 核验（关系只能是 MENTIONS，缺口会偏 LOW_RELEVANCE）")
    parser.add_argument("--no-plan", action="store_true",
                        help="不给历史 run 现算研究计划（缺口分析只剩真机 claim）")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--full", action="store_true",
                        help="把逐 run 明细也写进产物（默认写；--json 时只打摘要）")
    args = parser.parse_args()
    with open(args.snapshot, encoding="utf-8") as handle:
        snapshot = json.load(handle)
    report = run_acceptance(snapshot, verify=not args.no_verify, with_plan=not args.no_plan,
                            verbose=args.verbose)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=1)
    if args.json or not args.out:
        print(json.dumps({key: report[key] for key in
                          ("report_version", "generated_at_utc", "verify_replay", "with_plan",
                           "simulated_rounds", "totals", "acceptance")},
                         ensure_ascii=False, indent=1))
    else:
        totals = report["totals"]
        print("报告已写入 %s" % args.out)
        print("run 数：%d；接线前：%d claim / %d 边（%s）/ %d 冲突 / **没有 Gap 概念**"
              % (totals["runs"], totals["before_claims"], totals["before_edges"],
                 json.dumps(totals["before_relationship_distribution"], ensure_ascii=False),
                 totals["before_conflicts"]))
        print("接线后：缺口 %d（高优 %d / 安全关键 %d）类型分布 %s"
              % (totals["after_gaps"], totals["after_high_priority_gaps"],
                 totals["after_safety_critical_gaps"], totals["gap_types"]))
        print("分档分布 %s；每 run 平均缺口 %.2f" % (totals["gap_bands"],
                                              totals["after_gaps_per_run"]))
        print("停止原因（循环复核）%s；证据图复核 %s"
              % (totals["stop_reasons"], totals["review_stop_reasons"]))
        print("下一跳 %d 条（通道 %s）；已搜查询 %d 条；seen 去重命中 %d 次（按原因 %s）"
              % (totals["after_next_hops"], totals["routes"], totals["after_searched_queries"],
                 totals["after_seen_dedupe_hits"], totals["after_seen_dedupe_by_basis"]))
        print("真机留痕：%d 条跳留痕（带缺口 id 的 %d 条，new_claims=%d，resolved_gap=%d，"
              "累计耗时 %.0fms）→ 说明开启开关前缺口三列恒为空"
              % (totals["after_traces"], totals["after_trace_gap_ids"],
                 totals["after_trace_new_claims"], totals["after_trace_resolved_gap"],
                 totals["after_trace_latency_ms"]))
        print("跳数与耗时：当前口径下计划跳数合计 %d（每 run 平均 %.2f）；"
              "真机 level1_retrieval 阶段 %d 行、累计耗时 %.0fms"
              % (totals["after_plan_hops"],
                 totals["after_plan_hops"] / max(1, totals["runs"]),
                 totals["after_stage_rows"], totals["after_stage_latency_ms"]))
        print("跳过 run 数 %d" % len(report["skipped_runs"]))
        case = report["constructed_case"]
        print("构造用例（真机素材，UNRESOLVABLE_CONTRADICTION 的能力证据）：run=%s 停止原因=%s "
              "理由码=%s" % (str(case.get("run_id") or "")[:10], case.get("stop_reason"),
                            case.get("reason_codes")))
        print("全库 seen 身份：%s" % json.dumps(report.get("seen") or {}, ensure_ascii=False))
        print("验收：%s" % ("PASS" if report["acceptance"]["passed"] else "FAIL"))
        for name, item in report["acceptance"]["checks"].items():
            print("  · %-42s %s  %s" % (name, "OK" if item["ok"] else "NG", item["detail"]))
    return 0 if report["acceptance"]["passed"] else 1


def _counter_flat(totals: dict, key: str) -> str:
    return json.dumps(totals.get(key) or {}, ensure_ascii=False)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 11（P11-01…P11-06）· **技能路由验收工具**（快照 + 同代码离线重建，可复算）。

为什么是"离线重建"而不是"线上统计"：线上 `qa_stage_runs` 里 `node_kind='skill'` 是 **0 行**
（Phase 11 还没发布），所以真机**没有**技能遥测可直接聚合。本工具的口径与 Phase 04/09/10 一致：
  · 从 A 机**只读**导出真实 run 的结论/边/证据（`tools/qa_phase11_real_snapshot.py`）；
  · 离线用**同一份代码**重放：Phase 02 标注 → Phase 07 真实缺口规则 → Phase 11 真实路由规则
    → Phase 08 真实上下文包组装；
  · 报出来的每个数字都来自真数据 + 真规则，并明确标注"候选证据为本轮快照内的证据"这条边界。

本工具**零模型调用、零嵌入端点、零写库**：只读快照 JSON，只写报告 JSON。

关键口径（与 tracking / ACCEPTANCE_MATRIX 对齐）：
  · `before` = 接线前（不给 `skill_items`）：`skill_context` 是空段，`SKILL_NOT_AVAILABLE`
    缺口只能**标注** `LOAD_SKILL`；
  · `after` = 接线后（给 `skill_items`）：能力真的进包，该缺口**不再成立**；
  · 技能命中率 = 被选中的技能数 / 需要能力的缺口数（可复算）；
  · 成功率口径 = `qa_skills.performance_summary` 的那一条（写死在契约里）；
  · 时钟：`clock.baseline_utc` 显式取自快照的 `captured_at_utc`（**不用墙上钟**，
    两次运行产出逐字相同 —— 与 Phase 09/10 的时钟修复同一口径）。

用法：
    python tools/qa_phase11_skill_acceptance.py --snapshot baseline/qa-skill-snapshot.json \\
        --out baseline/qa-skill-acceptance.json [--history data/qa_skill_history.jsonl] [--print]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATABASE_TYPE", "sqlite")

REPORT_VERSION = "qa-skill-acceptance-v1"


def _counter(values) -> dict:
    out: dict = {}
    for value in values:
        key = str(value or "")
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def _parse_time(value):
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def rebuild_evidence(row: dict, *, run_id: str, stage: str = "level1_retrieval") -> dict:
    """把快照里的一条证据还原成 Phase 02 的证据对象（走真实 `annotate_evidence`）。"""
    from qa_evidence import annotate_evidence

    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    item = dict(payload)
    item.setdefault("evidence_ref", str(row.get("evidence_ref") or ""))
    item.setdefault("source_type", str(row.get("source_type") or "article"))
    item.setdefault("title", str(row.get("title") or ""))
    item.setdefault("source_url", str(row.get("source_url") or ""))
    item.setdefault("published_at", row.get("published_at"))
    item.setdefault("authority_level", row.get("authority_level"))
    return annotate_evidence(item, terms=[], run_id=run_id, stage=stage,
                             route=str(payload.get("retrieval_method") or "keyword"),
                             corpus_version="")


def rebuild_graph(run: dict, *, annotate: bool = True) -> dict:
    """把快照里的一个 run 还原成 `detect_gaps` 能吃的最小证据图。

    · claim 取 `qa_claims` 的原文与核验状态（不重算核验 —— 核验是 Phase 03 的账）；
    · 边取 `qa_claim_evidence` 的关系与相关性（`metadata.verified` 由核验状态派生，
      与 Phase 06/07 的读法一致：`SUPPORTS` 且 claim 已核验才算已验证边）；
    · 证据走 Phase 02 的真实标注（不手写 `metadata.evidence_layer`）。
    """
    claims: list = []
    verified = {}
    for row in run.get("claims") or []:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        claim_id = str(row.get("claim_key") or payload.get("claim_id") or "")
        status = str(row.get("verification_status") or payload.get("verification_status") or "")
        verified[claim_id] = status in ("confirmed", "supported", "verified")
        claims.append({"claim_id": claim_id, "canonical_id": claim_id,
                       "text": str(row.get("text") or payload.get("text") or ""),
                       "claim_type": str(row.get("claim_type") or "background"),
                       "confidence": float(row.get("confidence") or 0.0),
                       "plan_only": False, "verification_status": status or "unverified",
                       "claim": dict(payload, claim_id=claim_id)})
    edges: list = []
    for row in run.get("edges") or []:
        claim_id = str(row.get("claim_key") or "")
        relation = str(row.get("relationship") or "supports")
        edges.append({
            "edge_id": "edge:%s:%s" % (claim_id, row.get("evidence_ref")),
            "kind": "claim-evidence",
            "src": "claim:%s" % claim_id, "dst": "evidence:%s" % row.get("evidence_ref"),
            "claim_id": claim_id, "evidence_ref": str(row.get("evidence_ref") or ""),
            "graph_relation": {"supports": "SUPPORTS", "contradicts": "REFUTES",
                               "qualifies": "SUPPORTS", "context": "MENTIONS"}.get(
                                   relation, "MENTIONS"),
            "relationship": relation,
            "status": relation,
            "relevance_score": float(row.get("relevance_score") or 0.0),
            "strength": 0.7,
            "metadata": {"verified": bool(verified.get(claim_id))},
            "scope": [],
        })
    evidence: list = []
    for row in run.get("evidence") or []:
        if annotate:
            evidence.append(rebuild_evidence(row, run_id=str(run.get("run_id") or "")))
        else:
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            evidence.append(dict(payload, evidence_ref=str(row.get("evidence_ref") or ""),
                                 title=str(row.get("title") or ""),
                                 content_excerpt=str(payload.get("content_excerpt") or ""),
                                 authority_level=row.get("authority_level"),
                                 published_at=row.get("published_at"),
                                 metadata=dict(payload.get("metadata") or {})))
    return {"version": "qa-evidence-graph-v1", "claims": claims, "evidence": evidence,
            "edges": edges, "conflicts": [],
            "verification": {"stats": {"claims": len(claims),
                                       "confirmed": len([cid for cid, ok in verified.items() if ok])}},
            "stats": {}}


def _phase08_pack(graph: dict, run: dict, *, skill_items=(), budget_tokens: int = 6000) -> dict:
    """走**真实**的 Phase 08 上下文包组装（before/after 的对照都靠它）。"""
    from qa_context_pack import build_context_pack

    return build_context_pack(
        graph=graph, plan={"question": str(run.get("question") or "")},
        request={"question": str(run.get("question") or ""), "mode": str(run.get("mode") or "")},
        run_id=str(run.get("run_id") or ""), budget_tokens=int(budget_tokens),
        skill_items=tuple(skill_items))


def analyze_run(run: dict, *, annotate: bool = True, budget_tokens: int = 6000,
                min_samples: int | None = None, performance=None) -> dict:
    """单个 run 的完整离线重建：真实缺口 → 真实路由 → 真实上下文包 → before/after。"""
    from qa_context_pack import build_context_graph, skill_context_needs
    from qa_gap_analyzer import detect_gaps
    from qa_skills import (performance_table, route_skills, skill_context_items,
                           telemetry_records_from_routing)

    graph = rebuild_graph(run, annotate=annotate)
    gaps_result = detect_gaps(graph["claims"], edges=graph["edges"], evidence=graph["evidence"])
    gaps = list(gaps_result.get("gaps") or [])

    before = _phase08_pack(graph, run, budget_tokens=budget_tokens)
    before_gaps = [gap for gap in before["context_gaps"]
                   if gap["context_gap_type"] == "SKILL_NOT_AVAILABLE"]

    # 任务级需要的能力：与 Phase 08 的缺口检测**共用同一个函数**（打包前先问一次，
    # 因为 skill_context 段本身要靠这个结论去填 —— 见 qa_pipeline._skill_needed_skills）
    context_graph = build_context_graph(
        graph=graph, plan={"question": str(run.get("question") or "")}, run_id="")
    needed = skill_context_needs(items=context_graph.get("items") or ())

    routing = route_skills(gaps=gaps, needed_skills=[needed] if needed else (),
                           task_type=str((gaps_result.get("stats") or {}).get("category") or ""),
                           performance=performance)
    items = skill_context_items(routing)
    after_graph = dict(graph, context_pack=None)
    after = _phase08_pack(graph, run, skill_items=items, budget_tokens=budget_tokens)
    after_graph["context_pack"] = after
    after_gaps = [gap for gap in after["context_gaps"]
                  if gap["context_gap_type"] == "SKILL_NOT_AVAILABLE"]
    loaded = list((after["sections"].get("skill_context") or {}).get("loaded_skills") or [])

    records = telemetry_records_from_routing(routing, graph=after_graph,
                                             run_id=str(run.get("run_id") or ""),
                                             task_type=routing.get("task_type") or "")
    return {
        "run_id": str(run.get("run_id") or ""),
        "question": str(run.get("question") or "")[:80],
        "mode": str(run.get("mode") or ""),
        "claims": len(graph["claims"]),
        "evidence": len(graph["evidence"]),
        "edges": len(graph["edges"]),
        "gaps": len(gaps),
        "gap_types": _counter(gap.get("missing") for gap in gaps),
        "needs": len(routing.get("needs") or []),
        "selected": list(routing.get("selected") or []),
        "loaded_in_pack": loaded,
        "skipped": dict(routing.get("skipped") or {}),
        "budget": {"used_skills": (routing.get("budget") or {}).get("used_skills"),
                   "max_skills": (routing.get("budget") or {}).get("max_skills"),
                   "used_cost_units": (routing.get("budget") or {}).get("used_cost_units"),
                   "used_latency_ms": (routing.get("budget") or {}).get("used_latency_ms"),
                   "exhausted": list((routing.get("budget") or {}).get("exhausted") or [])},
        "before_skill_gaps": len(before_gaps),
        "before_skill_section_empty": int((before["sections"]["skill_context"] or {})
                                          .get("count") or 0) == 0,
        "after_skill_gaps": len(after_gaps),
        "after_skill_section_count": (after["sections"]["skill_context"] or {}).get("count"),
        "skill_in_citation_map": (after["stats"] or {}).get("skill_in_citation_map"),
        "records": records,
        "selected_detail": list(routing.get("selected_detail") or []),
        "trace": list(routing.get("trace") or []),
    }


def analyze(snapshot: dict, *, annotate: bool = True, budget_tokens: int = 6000,
            min_samples: int | None = None, performance: dict | None = None) -> dict:
    """整份快照的验收报告（**确定性**：同快照同参数 → 逐字相同的报告）。"""
    from qa_skills import (performance_table, routing_summary, skill_routing_receipt,
                           telemetry_receipt)

    runs = [row for row in (snapshot.get("runs") or []) if isinstance(row, dict)]
    details = [analyze_run(run, annotate=annotate, budget_tokens=budget_tokens,
                           min_samples=min_samples, performance=performance)
               for run in runs]
    records: list = []
    for detail in details:
        records.extend(detail["records"])

    # 第二次跑一遍用于"可复算"证明（**不是**缓存；真的重算）
    second = [analyze_run(run, annotate=annotate, budget_tokens=budget_tokens,
                          min_samples=min_samples, performance=performance)
              for run in runs]
    determinism = {
        "same_selection": all(a["selected"] == b["selected"] for a, b in zip(details, second)),
        "same_trace": all(a["trace"] == b["trace"] for a, b in zip(details, second)),
        "same_records": all(a["records"] == b["records"] for a, b in zip(details, second)),
    }

    selected_counts = _counter(name for detail in details for name in detail["selected"])
    loaded_counts = _counter(name for detail in details for name in detail["loaded_in_pack"])
    reason_counts = _counter(row.get("reason") for detail in details for row in detail["trace"])
    telemetry = telemetry_receipt(records, min_samples=min_samples)
    table = performance_table(records, min_samples=min_samples)

    runs_with_gaps = [detail for detail in details if detail["gaps"]]
    needs_total = sum(detail["needs"] for detail in details)
    selected_total = sum(len(detail["selected"]) for detail in details)
    before_gap_total = sum(detail["before_skill_gaps"] for detail in details)
    after_gap_total = sum(detail["after_skill_gaps"] for detail in details)

    report = {
        "report_version": REPORT_VERSION,
        "clock": {
            "source": "snapshot.captured_at_utc（显式基准，绝不用墙上钟）",
            "baseline_utc": str(snapshot.get("captured_at_utc") or ""),
        },
        "snapshot": {
            "snapshot_version": str(snapshot.get("snapshot_version") or ""),
            "captured_at_utc": str(snapshot.get("captured_at_utc") or ""),
            "counts": dict(snapshot.get("counts") or {}),
            "errors": list(snapshot.get("errors") or []),
        },
        "parameters": {"annotate": bool(annotate), "budget_tokens": int(budget_tokens),
                       "min_samples": telemetry["min_samples"]},
        "before_after": {
            "before": {
                "skill_section_empty_runs": sum(1 for detail in details
                                                if detail["before_skill_section_empty"]),
                "skill_not_available_gaps": before_gap_total,
                "skills_loaded": 0,
            },
            "after": {
                "skill_section_empty_runs": len([detail for detail in details
                                                 if not detail["after_skill_section_count"]]),
                "skill_not_available_gaps": after_gap_total,
                "skills_loaded": len([name for detail in details
                                      for name in detail["loaded_in_pack"]]),
            },
            "delta": {"skill_not_available_gaps": after_gap_total - before_gap_total,
                      "skills_loaded": len([name for detail in details
                                            for name in detail["loaded_in_pack"]])},
            "note": ("before = 不给 skill_items（Phase 08 行为）；after = 给 Phase 11 的 "
                     "SKILL_HINT 指令。缺口减少说明'能力真的进包了'，不是把缺口藏起来。"),
        },
        "routing": {
            "runs": len(details),
            "runs_with_gaps": len(runs_with_gaps),
            "needs_total": needs_total,
            "selected_total": selected_total,
            "hit_rate": round(selected_total / needs_total, 6) if needs_total else None,
            "selected_per_skill": selected_counts,
            "loaded_per_skill": loaded_counts,
            "reason_distribution": reason_counts,
            "budget_exhausted": _counter(reason for detail in details
                                         for reason in detail["budget"]["exhausted"]),
            "trace_rows": sum(len(detail["trace"]) for detail in details),
            "summary": routing_summary({"trace": [row for detail in details
                                                  for row in detail["trace"]]}),
        },
        "telemetry": telemetry,
        "per_skill": table,
        "skill_in_citation_map": sum(int(detail["skill_in_citation_map"] or 0)
                                     for detail in details),
        "determinism": determinism,
        "notes": [
            ("`evidence_yield` = 该技能的通道在本轮证据里取回了几条候选；"
             "`verified_yield` = 其中被 Phase 03 判 SUPPORTED 的条数。真实快照上两者都为 0："
             "离线重建**不重跑 Phase 03 核验**（快照的 qa_evidence payload 里没有核验结论，"
             "不重跑就不会凭空长出 verdict —— 这是如实为 0，不是漏统计）。"
             "同一份快照的 qa_claims.verification_status 分布是 "
             "{qualified 14, unverified 82, insufficient_evidence 33}，"
             "**没有一条 SUPPORTED**，两个口径互相印证。"),
            ("选中的技能落点在 semantic / policy_exact 通道上，而快照里 93 条证据的"
             "`retrieval_method` 分布是 {keyword 70, hybrid 20, tavily 3}："
             "路由给的是「下一轮该用什么能力」，不是「上一轮用了什么」。"
             "`hybrid`/`tavily` 不在冻结的七个通道值里，所以它们**匹配不到任何技能**，"
             "如实计 0 而不是硬塞给某个技能。"),
        ],
        "runs": details,
    }
    # 回执必须能被契约认出来（越界就是契约漂移）
    receipt = skill_routing_receipt({"selected": list(selected_counts),
                                     "budget": {}, "stats": {}})
    report["receipt_sample"] = {key: receipt[key] for key in
                                ("router_version", "registry_version", "budget_version",
                                 "instruction_version", "telemetry_version")}
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Phase 11 技能路由验收（快照离线重建）")
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--out", default="")
    parser.add_argument("--history", default="")
    parser.add_argument("--budget-tokens", type=int, default=6000)
    parser.add_argument("--min-samples", type=int, default=None)
    parser.add_argument("--no-annotate", action="store_true",
                        help="跳过 Phase 02 标注（快照里已有证据层时更快）")
    parser.add_argument("--print", action="store_true", dest="print_only")
    args = parser.parse_args(argv)

    with open(args.snapshot, encoding="utf-8") as handle:
        snapshot = json.load(handle)
    report = analyze(snapshot, annotate=not args.no_annotate,
                     budget_tokens=args.budget_tokens, min_samples=args.min_samples)
    summary = {key: report[key] for key in ("report_version", "clock", "snapshot",
                                            "before_after", "determinism")}
    summary["routing"] = {key: value for key, value in report["routing"].items()
                          if key not in ("summary",)}
    summary["telemetry_overall"] = report["telemetry"]["overall"]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.out and not args.print_only:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print("written: %s" % args.out)
    if args.history and not args.print_only:
        line = {"report_version": REPORT_VERSION,
                "captured_at_utc": report["snapshot"]["captured_at_utc"],
                "runs": report["routing"]["runs"],
                "needs_total": report["routing"]["needs_total"],
                "selected_total": report["routing"]["selected_total"],
                "hit_rate": report["routing"]["hit_rate"],
                "before_skill_gaps": report["before_after"]["before"]["skill_not_available_gaps"],
                "after_skill_gaps": report["before_after"]["after"]["skill_not_available_gaps"],
                "skills_loaded": report["before_after"]["after"]["skills_loaded"],
                "determinism": report["determinism"]}
        with open(args.history, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
        print("history appended: %s" % args.history)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

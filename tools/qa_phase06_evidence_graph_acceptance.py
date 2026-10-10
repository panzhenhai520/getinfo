#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 06（P06-01…P06-04）验收：**真机证据图快照上的前后对比**（纯规则，零模型调用）。

它回答的问题：把 A 机真实 run 的 claim / 边 / 冲突 / 证据读回来之后，
  · 接线前（before）：只有 `qa_reasoning` 的三样东西 —— 小写 relationship 的边、
    按 `_conflict_type` 检出的冲突、**没有任何 coverage 概念**；
  · 接线后（after）：四类关系 + `MENTIONS` 的分布、claim coverage（主口径/带权/对照口径）、
    两族矛盾与九种理由码的裁决、口径一致性自检。
两者都在同一份真实数据上算，数字可逐项复算。

口径与边界：
  · 数据来源 = `--snapshot` 指定的 JSON（A 机只读导出的 `baseline/qa-evidence-graph-real-sample.json`，
    里面带 `source.query` 与只读声明），工具**本身不联网、不连库**；
  · `--verify`（默认开）= 在真实 claim/证据上**离线复跑 Phase 03 的规则核验**
    （`qa_verifier.verify_claim_graph`，纯规则、无端点），因为快照里那些 run 早于核验层接线；
    关掉它就只看"库里当时存了什么"（此时关系只能是 MENTIONS，coverage 主口径为 0 —— 这本身是
    一条重要结论：**没有核验结论就没有已验证证据**）；
  · 默认读 `baseline/qa-evidence-graph-real-sample.json`，产物默认写
    `baseline/qa-evidence-graph-acceptance.json`。

用法：
    python tools/qa_phase06_evidence_graph_acceptance.py --json
    python tools/qa_phase06_evidence_graph_acceptance.py --out baseline/qa-evidence-graph-acceptance.json
    python tools/qa_phase06_evidence_graph_acceptance.py --snapshot <新快照> --no-verify
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_evidence_graph as eg  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_verifier  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402

REPORT_VERSION = "qa-evidence-graph-acceptance-v1"
DEFAULT_SNAPSHOT = os.path.join("baseline", "qa-evidence-graph-real-sample.json")
DEFAULT_OUTPUT = os.path.join("baseline", "qa-evidence-graph-acceptance.json")


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _counter(values) -> dict:
    counts: dict = {}
    for value in values:
        key = str(value or "")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _rows_by_run(snapshot: dict) -> dict:
    grouped: dict = {}
    for row in snapshot.get("claims") or []:
        grouped.setdefault(str(row.get("run_id") or ""), {"claims": [], "edges": [],
                                                          "conflicts": [], "evidence": []})
        grouped[str(row.get("run_id") or "")]["claims"].append(row)
    for key, field in (("edges", "edges"), ("conflicts", "conflicts"), ("evidence", "evidence")):
        for row in snapshot.get(key) or []:
            run_id = str(row.get("run_id") or "")
            grouped.setdefault(run_id, {"claims": [], "edges": [], "conflicts": [],
                                        "evidence": []})
            grouped[run_id][field].append(row)
    runs = {str(item.get("id") or ""): item for item in snapshot.get("runs") or []}
    for run_id, bucket in grouped.items():
        bucket["run"] = runs.get(run_id) or {}
    return grouped


def _before_view(graph: dict) -> dict:
    """接线前能看到什么：小写 relationship 的边 + 旧口径的冲突 + 没有 coverage。"""
    relationships = _counter(edge.get("relationship") for edge in graph.get("edges") or [])
    types = _counter(item.get("conflict_type") for item in graph.get("conflicts") or [])
    resolutions = _counter(item.get("resolution") for item in graph.get("conflicts") or [])
    claims_with_evidence = len({str(edge.get("claim_id") or "") for edge in graph.get("edges") or []})
    return {
        "claims": len(graph.get("claims") or []),
        "evidence": len(graph.get("evidence") or []),
        "edges": len(graph.get("edges") or []),
        "relationship_distribution": relationships,
        "conflict_types": types,
        "conflict_resolutions": resolutions,
        "conflicts": len(graph.get("conflicts") or []),
        "claims_with_evidence": claims_with_evidence,
        # 接线前**没有**这些口径：显式写 None，而不是拿别的数冒充
        "claim_coverage": None,
        "weighted_claim_coverage": None,
        "relation_typed_edges": 0,
        "contradictions": 0,
        "reason_codes": {},
        "coverage_note": "接线前只有小写 relationship 与旧裁决，没有 coverage / 关系类型 / 理由码",
    }


def _plan_for_run(run_meta: Mapping, mode: str) -> tuple:
    """给一个 run 现算研究计划（DEPENDS 边的来源）。

    **口径声明**：真机上的历史 run 没有把计划落库（Phase 05 那时还没接线），所以这里用
    **当前 Phase 05 代码**（`qa_execution_graph.build_research_plan`，纯规则、零模型）
    按 run 的真实问题文本现算一份计划；计划 claim 进图时恒标 `plan_only=true`，
    **不计入 coverage 分母**，只用来给出真实的依赖边与"计划↔结论"的文本匹配幅度。
    """
    try:
        import qa_execution_graph as execution_graph

        plan = execution_graph.build_research_plan(
            str(run_meta.get("question_text") or ""), plan={},
            mode=str(mode or "standard"))
        return plan, "qa_execution_graph.build_research_plan(question)（老 run 未存计划，按当前 Phase 05 口径现算）"
    except Exception as exc:  # noqa: BLE001  计划现算失败只记账，不影响证据图
        return {}, "plan_unavailable:%s" % type(exc).__name__


def _after_view(graph: dict, *, run_id: str, verify: bool, plan: Mapping | None = None,
                plan_source: str = "") -> dict:
    audit = {"verify_replay": False, "verify_stats": {}, "verify_error": "",
             "plan_source": str(plan_source or "")}
    if verify:
        try:
            summary = qa_verifier.verify_claim_graph(graph)
            audit["verify_replay"] = True
            audit["verify_stats"] = dict(summary.get("stats") or {})
        except Exception as exc:  # noqa: BLE001  复跑失败要留痕，不许静默
            audit["verify_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
    layer = eg.build_layer(graph, plan=plan, run_id=run_id)
    contradictions = layer["contradictions"]
    return {
        "graph_version": layer["graph_version"],
        "nodes": len(layer["nodes"]),
        "edges": len(layer["edges"]),
        "relation_distribution": layer["stats"]["relation_distribution"],
        "edge_kind_distribution": layer["stats"]["edge_kind_distribution"],
        "verification_basis": layer["stats"]["verification_basis"],
        "coverage": layer["coverage"],
        "contradictions": len(contradictions),
        "contradiction_kinds": layer["stats"]["contradiction_kinds"],
        "resolution_distribution": layer["stats"]["resolution_distribution"],
        "reason_codes": layer["stats"]["reason_codes"],
        "status_consistency": layer["stats"]["status_consistency"],
        "depends_edges": layer["stats"]["depends_edges"],
        "plan_claims": layer["stats"]["plan_claims"],
        "plan_claims_matched": layer["stats"]["plan_claims_matched"],
        "plan_dependencies": layer["stats"]["plan_dependencies"],
        "valid_edges": layer["stats"]["valid_edges"],
        "resolver": layer["stats"]["resolver"],
        **audit,
    }


def _aggregate(rows: list) -> dict:
    totals = {
        "runs": len(rows),
        "before_claims": sum(row["before"]["claims"] for row in rows),
        "before_edges": sum(row["before"]["edges"] for row in rows),
        "before_conflicts": sum(row["before"]["conflicts"] for row in rows),
        "after_claims": sum(row["after"]["coverage"]["total_claims"] for row in rows),
        "after_edges": sum(row["after"]["edges"] for row in rows),
        "after_contradictions": sum(row["after"]["contradictions"] for row in rows),
        "after_depends_edges": sum(row["after"].get("depends_edges") or 0 for row in rows),
        "after_plan_claims": sum(row["after"].get("plan_claims") or 0 for row in rows),
        "after_plan_claims_matched": sum(row["after"].get("plan_claims_matched") or 0 for row in rows),
        "after_supported_claims": sum(row["after"]["coverage"]["supported_claims"] for row in rows),
        "after_claims_with_evidence": sum(row["after"]["coverage"]["claims_with_evidence"]
                                          for row in rows),
        "after_weight_mass": round(sum(row["after"]["coverage"]["weighted_claim_coverage"] or 0
                                       for row in rows), 4),
        "relation_distribution": _counter(
            relation for row in rows for relation, count in
            (row["after"]["relation_distribution"] or {}).items() for _ in range(count)),
        "before_relationship_distribution": _counter(
            name for row in rows for name, count in
            (row["before"]["relationship_distribution"] or {}).items() for _ in range(count)),
        "reason_codes": _counter(
            code for row in rows for code, count in (row["after"]["reason_codes"] or {}).items()
            for _ in range(count)),
        "contradiction_kinds": _counter(
            kind for row in rows for kind, count in
            (row["after"]["contradiction_kinds"] or {}).items() for _ in range(count)),
        "verification_basis": _counter(
            basis for row in rows for basis, count in
            (row["after"]["verification_basis"] or {}).items() for _ in range(count)),
        "edge_kind_distribution": _counter(
            kind for row in rows for kind, count in
            (row["after"]["edge_kind_distribution"] or {}).items() for _ in range(count)),
        "support_count_histogram": _counter(
            str(bucket) for row in rows
            for bucket, count in (row["after"]["coverage"]["support_count_histogram"] or {}).items()
            for _ in range(count)),
        "coverage_buckets": _counter(
            name for row in rows
            for name, count in (row["after"]["coverage"]["coverage_buckets"] or {}).items()
            for _ in range(count)),
    }
    divisor = max(1, totals["after_claims"])
    if not totals["after_claims"]:
        # 一条 claim 都没有 → coverage 口径不适用，写 None（不许用 0 冒充）
        totals["after_claim_coverage"] = None
        totals["after_weighted_claim_coverage"] = None
        totals["after_evidence_coverage"] = None
        totals["before_claim_coverage"] = None
        return totals
    totals["after_claim_coverage"] = round(totals["after_supported_claims"] / divisor, 4)
    totals["after_weighted_claim_coverage"] = round(totals["after_weight_mass"] / divisor, 4)
    totals["after_evidence_coverage"] = round(totals["after_claims_with_evidence"] / divisor, 4)
    totals["before_claim_coverage"] = None
    return totals


def _gates(rows: list, *, verify: bool = True) -> dict:
    """可复算的验收断言（不通过就把 passed 置 False，并写清哪一条挂了）。"""
    checks = {}

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks[name] = {"ok": bool(ok), "detail": detail}

    schema_failures = sum(row["after"]["valid_edges"]["schema_failures"] for row in rows)
    relation_failures = sum(row["after"]["valid_edges"]["relation_failures"] for row in rows)
    add("edges_pass_contract", schema_failures == 0 and relation_failures == 0,
        "schema_failures=%d relation_failures=%d" % (schema_failures, relation_failures))

    mismatches = sum(row["after"]["status_consistency"]["mismatches"] for row in rows)
    checked = sum(row["after"]["status_consistency"]["checked"] for row in rows)
    add("relations_agree_with_verifier", mismatches == 0,
        "checked=%d mismatches=%d" % (checked, mismatches))

    recompute_bad = []
    for row in rows:
        coverage = row["after"]["coverage"]
        total = coverage["total_claims"]
        if not total:
            continue
        expected = round(coverage["supported_claims"] / total, 4)
        if abs((coverage["claim_coverage"] or 0) - expected) > 1e-6:
            recompute_bad.append(row["run_id"])
    add("coverage_is_recomputable", not recompute_bad, "异常 run：%s" % recompute_bad)

    illegal = [row["run_id"] for row in rows
               for code in (row["after"]["reason_codes"] or {})
               if code not in contracts.CONTRADICTION_RESOLUTION_CODES]
    add("reason_codes_are_legal", not illegal, "非法理由码 run：%s" % illegal)

    bad_resolution = []
    for row in rows:
        for code, count in (row["after"]["reason_codes"] or {}).items():
            if not count:
                continue
            resolved = (row["after"]["resolution_distribution"] or {}).get("resolved", 0)
            unresolved = (row["after"]["resolution_distribution"] or {}).get("unresolved", 0)
            if resolved + unresolved != row["after"]["contradictions"]:
                bad_resolution.append(row["run_id"])
    add("resolution_distribution_sums_up", not bad_resolution, "异常 run：%s" % bad_resolution)

    degraded = [row["run_id"] for row in rows if row["after"].get("verify_error")]
    add("verify_replay_had_no_errors", not degraded, "复跑核验失败：%s" % degraded)

    executed = sum(1 for row in rows if row["after"].get("verify_replay"))
    if verify:
        add("verify_replay_executed", executed == len(rows) or not rows,
            "%d/%d 个 run 跑了 Phase 03 核验复放" % (executed, len(rows)))
    return {"passed": all(item["ok"] for item in checks.values()), "checks": checks}


def run_acceptance(snapshot: dict, *, verify: bool = True, verbose: bool = False,
                   with_plan: bool = True) -> dict:
    grouped = _rows_by_run(snapshot)
    rows = []
    skipped = []
    for run_id, bucket in sorted(grouped.items(), key=lambda item: str(item[0])):
        if not bucket["claims"]:
            # 只登记了 level1_draft claim（没有 conflict_review 的 canonical claim）的 run：
            # 结论图根本没有建立过，不能拿它的边冒充证据图 —— 如实记账并跳过。
            skipped.append({"run_id": run_id, "claims": 0, "edges": len(bucket["edges"]),
                            "evidence": len(bucket["evidence"]),
                            "reason": "没有 conflict_review 阶段的 canonical claim（结论图未建立）"})
            continue
        run_meta = bucket.get("run") or {}
        graph = eg.graph_from_rows(claims=bucket["claims"], edges=bucket["edges"],
                                  conflicts=bucket["conflicts"], evidence=bucket["evidence"],
                                  run_id=run_id)
        before = _before_view(graph)
        plan, plan_source = ({}, "plan_disabled")
        if with_plan:
            plan, plan_source = _plan_for_run(run_meta, str(run_meta.get("mode") or "standard"))
        after = _after_view(graph, run_id=run_id, verify=verify, plan=plan,
                            plan_source=plan_source)
        rows.append({
            "run_id": run_id,
            "question": str(run_meta.get("question_text") or "")[:200],
            "mode": str(run_meta.get("mode") or ""),
            "industry_pack_id": str(run_meta.get("industry_pack_id") or ""),
            "before": before,
            "after": after,
        })
        if verbose:
            print("[run %s] before: %d claims/%d edges/%d conflicts（无 coverage）→ after: "
                  "relations=%s coverage=%s contradictions=%d"
                  % (run_id[:12], before["claims"], before["edges"], before["conflicts"],
                     after["relation_distribution"], after["coverage"]["claim_coverage"],
                     after["contradictions"]))
    totals = _aggregate(rows)
    gates = _gates(rows, verify=verify)
    return {
        "report_version": REPORT_VERSION,
        "generated_at_utc": _utc_now_z(),
        "graph_version": contracts.EVIDENCE_GRAPH_VERSION,
        "coverage_version": eg.COVERAGE_VERSION,
        "resolver_version": contracts.CONTRADICTION_RESOLVER_VERSION,
        "verify_replay": bool(verify),
        "with_plan": bool(with_plan),
        "snapshot": {
            "captured_at_utc": str(snapshot.get("captured_at_utc") or ""),
            "source": snapshot.get("source") or {},
            "catalog": snapshot.get("catalog") or {},
        },
        "totals": totals,
        "runs": rows,
        "skipped_runs": skipped,
        "acceptance": gates,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", default=DEFAULT_SNAPSHOT)
    parser.add_argument("--out", default="")
    parser.add_argument("--json", action="store_true", help="只把报告打到 stdout")
    parser.add_argument("--no-verify", action="store_true",
                        help="不复跑 Phase 03 核验（只看库里当时存了什么）")
    parser.add_argument("--no-plan", action="store_true",
                        help="不给历史 run 现算研究计划（DEPENDS 边会全是 0）")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    with open(args.snapshot, encoding="utf-8") as handle:
        snapshot = json.load(handle)
    report = run_acceptance(snapshot, verify=not args.no_verify, verbose=args.verbose,
                            with_plan=not args.no_plan)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=1)
    if args.json or not args.out:
        print(json.dumps({key: report[key] for key in
                          ("report_version", "generated_at_utc", "verify_replay", "totals",
                           "acceptance")}, ensure_ascii=False, indent=1))
    else:
        totals = report["totals"]
        print("报告已写入 %s" % args.out)
        print("run 数：%d；接线前：%d claim / %d 边（%s）/ %d 冲突 / coverage 无口径"
              % (totals["runs"], totals["before_claims"], totals["before_edges"],
                 totals["before_relationship_distribution"], totals["before_conflicts"]))
        print("接线后：关系分布 %s；claim_coverage=%.4f（带权 %.4f）；证据覆盖 %.4f；矛盾 %d（%s）"
              % (totals["relation_distribution"], totals["after_claim_coverage"],
                 totals["after_weighted_claim_coverage"], totals["after_evidence_coverage"],
                 totals["after_contradictions"], totals["contradiction_kinds"]))
        print("支持数分布 %s；覆盖分桶 %s；核验来源 %s；跳过 run 数 %d"
              % (totals["support_count_histogram"], totals["coverage_buckets"],
                 totals["verification_basis"], len(report["skipped_runs"])))
        print("依赖边 %d（计划 claim %d，其中文本匹配上结论的 %d 条）"
              % (totals["after_depends_edges"], totals["after_plan_claims"],
                 totals["after_plan_claims_matched"]))
        print("验收：%s" % ("PASS" if report["acceptance"]["passed"] else "FAIL"))
        for name, item in report["acceptance"]["checks"].items():
            print("  · %-32s %s  %s" % (name, "OK" if item["ok"] else "NG", item["detail"]))
    return 0 if report["acceptance"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

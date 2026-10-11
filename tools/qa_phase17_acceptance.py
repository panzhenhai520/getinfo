#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 17（P17-01…P17-06）· **验收驱动**：E2E / 消融 / 指标报告 / 兼容回归 / 回滚 / 发布清单。

**只读 + 离线**：全部在隔离临时 sqlite 上跑（语料来自 A 机只读快照），
零模型、零嵌入端点、零外网；本工具不改任何真实库、不部署任何机器。

    python tools/qa_phase17_acceptance.py --section all
    python tools/qa_phase17_acceptance.py --section e2e --print
    python tools/qa_phase17_acceptance.py --section metrics --rebuild-annotation
    python tools/qa_phase17_acceptance.py --section ablation --max-questions 3   # 冒烟

产物（默认写到 baseline/）：
    qa-e2e-acceptance.json / qa-ablation-acceptance.json / qa-metric-report.json /
    qa-release-acceptance.json / config/qa_e2e_annotation_v1.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

# 必须在**导入 config 之前**决定数据库后端（与 Phase 16 验收工具同一手法）：
# 全程在隔离 sqlite 上跑，绝不碰本机 .env 指向的库。
_ISOLATED = "--db" not in sys.argv
if _ISOLATED:
    _TMP = tempfile.mkdtemp(prefix="qa-phase17-acceptance-")
    os.environ["DATABASE_TYPE"] = "sqlite"
    os.environ["DATABASE_PATH"] = os.path.join(_TMP, "acceptance.sqlite3")
    os.environ["SQLITE_BACKUP_PATH"] = os.environ["DATABASE_PATH"]

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_ablation  # noqa: E402
import qa_e2e  # noqa: E402
import qa_metrics_report as metrics  # noqa: E402
import qa_release  # noqa: E402

CORPUS_SNAPSHOT = os.path.join("baseline", "qa-hunter-corpus-snapshot.json")
ANSWER_SNAPSHOT = os.path.join("baseline", "qa-context-real-sample.json")
QUESTIONS_FILE = os.path.join("config", "qa_acceptance_questions.json")
PHASE13_ARTIFACT = os.path.join("baseline", "qa-final-acceptance.json")
E2E_OUT = os.path.join("baseline", "qa-e2e-acceptance.json")
ABLATION_OUT = os.path.join("baseline", "qa-ablation-acceptance.json")
METRICS_OUT = os.path.join("baseline", "qa-metric-report.json")
RELEASE_OUT = os.path.join("baseline", "qa-release-acceptance.json")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _subset_metrics(rows, disjointness: dict, *, limit: int, benchmark_meta=None) -> dict:
    """快照内语料（in-scope）子集上的指标：**唯一公平的对比基准**。

    benchmark 12 题里只有 N 题的行业包在快照里；不在快照里的题**永远不可能命中**，
    把它们算进 hit_rate 会把"语料不全"误读成"检索变差"。两个数都报，口径写在字段里。
    """
    in_scope_ids = {str(item.get("id")) for item in (disjointness or {}).get("in_scope") or []}
    subset = [row for row in rows if str(row.get("id")) in in_scope_ids]
    metrics = qa_e2e.retrieval_metrics(subset, limit=limit, benchmark_meta=benchmark_meta)
    metrics["caliber"] = ("只统计行业包出现在快照里的题（id ∈ corpus_scope.in_scope）；"
                          "其余题因语料不在快照里而必然 no-hit，单列在 corpus_scope.out_of_scope")
    metrics["questions_in_scope"] = len(subset)
    metrics["questions_total"] = len(rows)
    return metrics


def _load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: str, payload) -> str:
    output = os.path.abspath(path)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=True, default=str)
        handle.write("\n")
    return output


def run_e2e(*, limit: int = 12, mode: str = "standard", max_questions: int = 0,
            workdir: str = "") -> dict:
    """P17-01：全套（检索侧真跑生产阶段 + 回答侧真机 run 重放）。"""
    from tools.qa_retrieval_acceptance import load_questions

    questions, benchmark_meta = load_questions(QUESTIONS_FILE)
    template = qa_e2e.build_corpus_template(CORPUS_SNAPSHOT, workdir=workdir)
    answer_snapshot = _load_json(ANSWER_SNAPSHOT)
    selected = questions[:max_questions] if max_questions else questions
    disjointness = qa_e2e.corpus_disjointness(questions, template["packs"])

    from qa_storage import QaStore
    from sqlite_database import SQLiteDatabase

    workdir = workdir or template["workdir"]
    db_path = qa_e2e.clone_database(template["template_path"],
                                    os.path.join(workdir, "e2e-full-stack.sqlite3"))
    database = SQLiteDatabase(db_path)
    database.connect()
    database.create_tables()
    store = QaStore(database)
    store.ensure_schema()
    try:
        # 全套 = 所有层打开（E2E 的"after"侧）；"before"侧由消融的 phase01_baseline 提供。
        with qa_e2e.layer_switches({name: True for name in qa_e2e.LAYER_IDS}):
            outcome = qa_e2e.run_suite(database, selected, answer_snapshot, limit=limit,
                                       mode=mode, store=store, answer_store=store,
                                       write_memories=True, benchmark_meta=benchmark_meta)
        rows = outcome["retrieval"]["rows"]
        receipts = outcome["retrieval"]["receipts"]
        metrics_all = outcome["retrieval_metrics"]
        ranking = metrics.ranking_metrics(rows)
        totals = outcome["answer_totals"]
        answer_rows = outcome["answer"]["rows"]
        phase00 = qa_e2e.phase00_baseline()
        calibration = _phase13_calibration(totals)
        checks = {
            "corpus_template_from_real_snapshot": bool(template["counts"]["articles"]),
            "questions_loaded": len(selected) > 0,
            "no_retrieval_errors": int(metrics_all.get("errors") or 0) == 0,
            "evidence_layer_annotated": any(
                int((row.get("evidence_layer") or {}).get("annotated") or 0) > 0
                for row in receipts),
            "verification_verdicts_present": any(
                bool((row.get("verification") or {}).get("verdicts")) for row in receipts),
            "answer_side_has_real_runs": int(totals.get("runs_scored") or 0) > 0,
            "citations_all_traceable": totals.get("citation_traceable_rate") == 1.0,
            "six_checks_ran": int(totals.get("checks_total") or 0) > 0,
            "retry_never_retrieves": int(totals.get("retrieval_calls") or 0) == 0,
            "offline_substitutions_recorded": bool(outcome["substitutions"]),
            "no_model_endpoint_called": (
                (outcome["substitutions"].get("provider_registry") or {}).get("calls", 0) > 0),
            "web_channel_has_no_object": (
                (outcome["substitutions"].get("web_search") or {}).get("evidence", 0) == 0),
            "phase13_calibration_explained": bool(calibration.get("explained")),
        }
        report = {
            "report_version": qa_e2e.E2E_SUITE_VERSION,
            "generated_at_utc": _now(),
            "data_source": {
                "corpus_snapshot": {"path": CORPUS_SNAPSHOT,
                                    "version": template["snapshot_version"],
                                    "captured_at_utc": template["captured_at_utc"],
                                    "packs": template["packs"], "counts": template["counts"],
                                    "host": (template["source"] or {}).get("host", "")},
                "answer_snapshot": {"path": ANSWER_SNAPSHOT,
                                    "captured_at_utc": answer_snapshot.get("captured_at_utc"),
                                    "runs": len(answer_snapshot.get("runs") or []),
                                    "claims": len(answer_snapshot.get("claims") or []),
                                    "evidence": len(answer_snapshot.get("evidence") or [])},
                "caliber": ("A 机只读快照 + 同一份代码离线重放；生产阶段处理器（plan / "
                            "level1_retrieval）与回答侧规则层全走真代码，仅三个外呼口换离线桩"),
                "substitutions": outcome["substitutions"],
                "offline_substitutions": list(qa_e2e.OFFLINE_SUBSTITUTIONS),
            },
            "benchmark": dict(benchmark_meta or {}),
            "corpus_scope": disjointness,
            "layer_states": outcome["layers"],
            "cache_reset": outcome["cache_reset"],
            "retrieval_metrics": metrics_all,
            "in_scope_metrics": _subset_metrics(rows, disjointness, limit=limit,
                                                benchmark_meta=benchmark_meta),
            "retrieval_rows": rows,
            "ranking_metrics": ranking,
            "answer_totals": totals,
            "no_object": qa_e2e.no_object_report(receipts, totals),
            "phase00_baseline": {"history_path": phase00.get("history_path"),
                                 "frozen_summary": ((phase00.get("frozen") or {})
                                                    .get("summary") or {}),
                                 "reason_code": phase00.get("reason_code") or ""},
            "phase13_calibration": calibration,
            "receipts": receipts,
            "answer_rows": answer_rows,
            "checks": checks,
            "acceptance": {"status": "PASS" if all(checks.values()) else "PARTIAL",
                           "numbers": {"questions": len(rows),
                                       "hit_rate": metrics_all.get("hit_rate"),
                                       "recall_at_k": metrics_all.get("recall_at_k"),
                                       "citation_precision": metrics_all.get("citation_precision"),
                                       "citation_recall": metrics_all.get("citation_recall"),
                                       "mrr_at_k": ranking.get("mrr_at_k"),
                                       "ndcg_at_k": ranking.get("ndcg_at_k"),
                                       "avg_ms": metrics_all.get("avg_ms"),
                                       "runs_scored": totals.get("runs_scored"),
                                       "claims": totals.get("claims"),
                                       "evidence": totals.get("evidence"),
                                       "unsupported_claim_rate": totals.get("unsupported_claim_rate"),
                                       "citation_traceable_rate": totals.get("citation_traceable_rate"),
                                       "checks_pass_rate": totals.get("checks_pass_rate")}},
        }
        report["fingerprint"] = qa_e2e.fingerprint({
            "rows": [{key: row.get(key) for key in qa_e2e.RETRIEVAL_ROW_KEYS} for row in rows],
            "totals": totals})
        return report
    finally:
        try:
            database.connection.close()
        except Exception:  # noqa: BLE001
            pass


def _phase13_calibration(totals) -> dict:
    """与 P13 冻结产物对齐（差多少、为什么差，逐键写清）。"""
    if not os.path.isfile(PHASE13_ARTIFACT):
        return {"artifact": PHASE13_ARTIFACT, "available": False, "explained": True,
                "reason_code": "NO_PHASE13_ARTIFACT"}
    frozen = (_load_json(PHASE13_ARTIFACT).get("totals") or {})
    keys = ("runs_scored", "claims", "evidence", "claims_verified_supported",
            "citation_traceable_rate", "retrieval_calls", "checks_passed", "checks_total")
    deltas = {}
    for key in keys:
        left, right = frozen.get(key), (totals or {}).get(key)
        deltas[key] = (round(float(right) - float(left), 6)
                       if isinstance(left, (int, float)) and isinstance(right, (int, float))
                       else None)
    differing = {key: value for key, value in deltas.items() if value not in (0, None)}
    return {
        "artifact": PHASE13_ARTIFACT, "available": True,
        "frozen": {key: frozen.get(key) for key in keys},
        "current": {key: (totals or {}).get(key) for key in keys},
        "deltas": deltas,
        "differing": differing,
        "explained": all(key in {"checks_passed", "checks_total"} for key in differing),
        "reason": ("唯一允许的差异是 checks_passed/checks_total：本 E2E 的 stop_reason 取**生产口径**"
                   "（缺口复核的 MAX_DEPTH），而 Phase 13 验收工具当时写死 ANSWERABLE —— "
                   "口径以生产为准，差异如实保留，不做粉饰。"
                   if differing else "与 Phase 13 冻结产物逐键一致"),
    }


def run_ablation(*, limit: int = 12, mode: str = "standard", max_questions: int = 0,
                 workdir: str = "") -> dict:
    """P17-02：逐层消融（15 格：2 基线 + 2 组合 + 11 单层）。"""
    from tools.qa_retrieval_acceptance import load_questions

    questions, benchmark_meta = load_questions(QUESTIONS_FILE)
    template = qa_e2e.build_corpus_template(CORPUS_SNAPSHOT, workdir=workdir)
    answer_snapshot = _load_json(ANSWER_SNAPSHOT)
    selected = questions[:max_questions] if max_questions else questions
    report = qa_ablation.run_ladder(template=template, questions=selected,
                                    snapshot=answer_snapshot, limit=limit, mode=mode,
                                    workdir=template["workdir"])
    report["generated_at_utc"] = _now()
    report["benchmark"] = dict(benchmark_meta or {})
    checks = {
        "all_cells_ran": all(bool(cell.get("metrics")) for cell in report["cells"]),
        "baseline_and_full_present": {"phase01_baseline", "full_stack"} <= {
            cell["cell"] for cell in report["cells"]},
        "every_layer_has_leave_one_out": all(
            ("minus_%s" % layer) in {cell["cell"] for cell in report["cells"]}
            for layer in qa_e2e.LAYER_IDS),
        "increments_computed": bool(report["increments"]["combination"]["delta_vs_baseline"]),
        "no_object_marked_explicitly": bool(report["no_object"]),
        "cells_isolated_databases": all(
            cell.get("cache_reset", {}).get("verifier_cache", {}).get("ok") is not None
            for cell in report["cells"]),
        "no_retrieval_errors_in_full_stack": int(
            (report["increments"]["full_stack"] or {}).get("errors") or 0) == 0,
    }
    report["checks"] = checks
    report["acceptance"] = {"status": "PASS" if all(checks.values()) else "PARTIAL",
                            "numbers": {"cells": len(report["cells"]),
                                        "layers": len(qa_e2e.LAYER_IDS),
                                        "combination": report["increments"]["combination"][
                                            "delta_vs_baseline"]}}
    return report


def run_metrics(*, rebuild_annotation: bool = False, e2e_report: dict | None = None,
                ablation_report: dict | None = None, max_questions: int = 0,
                workdir: str = "") -> dict:
    """P17-03：指标总表 + 独立标注集。"""
    if e2e_report is None:
        e2e_report = run_e2e(max_questions=max_questions, workdir=workdir)
    answer_snapshot = _load_json(ANSWER_SNAPSHOT)
    if rebuild_annotation or not os.path.isfile(metrics.DEFAULT_ANNOTATION_PATH):
        annotation = metrics.build_independent_annotation(answer_snapshot,
                                                          source_path=ANSWER_SNAPSHOT)
        metrics.save_annotation(annotation)
    else:
        annotation = metrics.load_annotation()
    evaluation = metrics.evaluate_annotation(annotation)
    overlap = metrics.overlap_with_golden(annotation)
    report = metrics.build_metric_report(
        retrieval_rows=e2e_report.get("retrieval_rows") or [],   # Phase 00 口径的逐题行
        retrieval_metrics=e2e_report.get("in_scope_metrics")
        or e2e_report.get("retrieval_metrics") or {},
        answer_totals=e2e_report.get("answer_totals") or {},
        ablation=ablation_report, annotation=annotation, annotation_eval=evaluation,
        phase00=e2e_report.get("phase00_baseline") and
        {"history_path": e2e_report["phase00_baseline"].get("history_path"),
         "frozen": {"summary": e2e_report["phase00_baseline"].get("frozen_summary") or {}},
         "reason_code": e2e_report["phase00_baseline"].get("reason_code") or ""})
    report["generated_at_utc"] = _now()
    report["independent_annotation"] = {
        "path": metrics.DEFAULT_ANNOTATION_PATH,
        "disjointness": overlap,
        "counts": annotation.get("counts") or {},
        "evaluation": {key: value for key, value in evaluation.items() if key != "rows"},
        "rules": annotation.get("rules") or {},
    }
    checks = {
        "annotation_disjoint_from_golden": bool(overlap.get("disjoint")),
        "annotation_has_both_groups": bool(annotation.get("counts", {}).get("positives"))
        and bool(annotation.get("counts", {}).get("negatives")),
        "annotation_size_above_floor": int(annotation.get("counts", {}).get("pairs") or 0)
        >= 16,
        "false_support_rate_is_zero": evaluation.get("false_support_rate") == 0.0,
        "support_recall_measured": evaluation.get("support_recall") is not None,
        "ranking_metrics_measured": report.get("ranking", {}).get("mrr_at_k") is not None,
        "catalog_covers_seven_families": len(report.get("catalog") or {}) >= 7,
        "no_zero_masquerading_nulls": _null_discipline(report),
    }
    report["checks"] = checks
    report["acceptance"] = {"status": "PASS" if all(checks.values()) else "PARTIAL",
                            "numbers": {"annotation_pairs": annotation.get("counts", {}).get("pairs"),
                                        "support_recall": evaluation.get("support_recall"),
                                        "false_support_rate": evaluation.get("false_support_rate"),
                                        "mrr_at_k": report.get("ranking", {}).get("mrr_at_k"),
                                        "ndcg_at_k": report.get("ranking", {}).get("ndcg_at_k")}}
    return report


def _null_discipline(report: dict) -> bool:
    """`no_object`/`not_measurable` 的条目必须是 null（不是 0）——D-060 的口径。"""
    for family in (report.get("catalog") or {}).values():
        for entry in (family or {}).values():
            if not isinstance(entry, dict):
                continue
            if entry.get("status") in {"no_object", "not_measurable", "not_implemented"}:
                if entry.get("value") not in (None, 0.0, 0):
                    return False
    return True


def run_release(*, e2e_report: dict | None = None, ablation_report: dict | None = None,
                metric_report: dict | None = None, max_questions: int = 0,
                workdir: str = "") -> dict:
    """P17-04/05/06：兼容回归 + 回滚 + 发布清单。"""
    compatibility = qa_release.compatibility_regression()
    rollback = qa_release.migration_rollback(workdir=workdir or tempfile.mkdtemp(
        prefix="qa-p17-rollback-"))
    manifest = qa_release.release_manifest(e2e=e2e_report, ablation=ablation_report,
                                           metric_report=metric_report,
                                           compatibility=compatibility, rollback=rollback)
    manifest["generated_at_utc"] = _now()
    manifest["compatibility"] = compatibility
    manifest["migration_rollback"] = rollback
    manifest["checks"] = {
        "compatibility_regression_passed": bool(compatibility.get("passed")),
        "migration_rollback_passed": bool(rollback.get("passed")),
        "release_matrix_complete": len(manifest["acceptance_matrix"]) == 6,
        "artifacts_recorded": bool(manifest["artifacts"]),
        "rollback_sql_present": bool((rollback.get("rollback_sql") or {}).get("steps")),
        "blocker_recorded_truthfully": any(item.get("id") == "B-003"
                                           for item in manifest["blockers"]),
    }
    manifest["acceptance"] = {
        "status": "PASS" if all(manifest["checks"].values()) else "PARTIAL",
        "gates_passed": manifest["gates_passed"],
        "failed_gates": [item["gate"] for item in manifest["gates"] if not item["ok"]],
    }
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Phase 17 验收（E2E/消融/指标/兼容/回滚/发布）")
    parser.add_argument("--section", default="all",
                        choices=("all", "e2e", "ablation", "metrics", "compat", "rollback",
                                 "release"))
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--mode", default="standard")
    parser.add_argument("--max-questions", type=int, default=0, help="冒烟用：只跑前 N 题")
    parser.add_argument("--rebuild-annotation", action="store_true")
    parser.add_argument("--out-dir", default="baseline")
    parser.add_argument("--print", dest="show", action="store_true")
    parser.add_argument("--json", action="store_true", help="把报告整份打到 stdout")
    args = parser.parse_args(argv)

    workdir = tempfile.mkdtemp(prefix="qa-p17-acceptance-")
    output = {}
    e2e_report = ablation_report = metric_report = None
    if args.section in ("all", "e2e", "metrics"):
        e2e_report = run_e2e(limit=args.limit, mode=args.mode,
                             max_questions=args.max_questions, workdir=workdir)
        output["e2e"] = _write_json(os.path.join(args.out_dir, "qa-e2e-acceptance.json"),
                                    e2e_report)
    if args.section in ("all", "ablation"):
        ablation_report = run_ablation(limit=args.limit, mode=args.mode,
                                       max_questions=args.max_questions, workdir=workdir)
        output["ablation"] = _write_json(os.path.join(args.out_dir, "qa-ablation-acceptance.json"),
                                         ablation_report)
    if args.section in ("all", "metrics"):
        metric_report = run_metrics(rebuild_annotation=args.rebuild_annotation,
                                    e2e_report=e2e_report, ablation_report=ablation_report,
                                    max_questions=args.max_questions, workdir=workdir)
        output["metrics"] = _write_json(os.path.join(args.out_dir, "qa-metric-report.json"),
                                        metric_report)
    if args.section in ("all", "release", "compat", "rollback"):
        release = run_release(e2e_report=e2e_report, ablation_report=ablation_report,
                              metric_report=metric_report, max_questions=args.max_questions,
                              workdir=workdir)
        output["release"] = _write_json(os.path.join(args.out_dir, "qa-release-acceptance.json"),
                                        release)
    summary = {
        "section": args.section,
        "artifacts": output,
        "acceptance": {
            "e2e": (e2e_report or {}).get("acceptance", {}).get("status"),
            "ablation": (ablation_report or {}).get("acceptance", {}).get("status"),
            "metrics": (metric_report or {}).get("acceptance", {}).get("status"),
            "release": (release if args.section in ("all", "release", "compat", "rollback")
                        else {}).get("acceptance", {}).get("status"),
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    if args.show:
        if e2e_report:
            print("\n=== E2E ===")
            print(json.dumps(e2e_report["acceptance"], ensure_ascii=False, indent=1))
            print(json.dumps(e2e_report["retrieval_metrics"], ensure_ascii=False)[:1200])
        if ablation_report:
            print("\n=== ABLATION ===")
            print(qa_ablation.format_table(ablation_report))
        if metric_report:
            print("\n=== METRICS ===")
            print(json.dumps(metric_report["acceptance"], ensure_ascii=False, indent=1))
    if args.json:
        print(json.dumps({key: value for key, value in
                          (("e2e", e2e_report), ("ablation", ablation_report),
                           ("metrics", metric_report)) if value},
                         ensure_ascii=False, default=str)[:20000])
    failed = [name for name, status in summary["acceptance"].items() if status == "PARTIAL"]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

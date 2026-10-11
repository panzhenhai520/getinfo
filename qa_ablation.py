#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 17（P17-02）· **ablation ladder**：逐层开关的真跑消融（增量 + 组合效应）。

## 口径（三条，缺一条这个消融就没有意义）

1. **真跑**：每一格都是"同一份代码 + 同一份真机快照 + 同一批题/同一批真实 run"跑出来的，
   不是从别的报告里抄数字。每格一份**独立临时库**（`qa_e2e.clone_database`），
   格子之间不共享缓存/seen/记忆行，避免上一格的状态污染下一格。
2. **只翻开关**：消融腿只改 `LAYER_SPECS` 里那批**生产环境变量**（其余一个字节不动），
   关掉某一层就是生产里关掉那一层的真实行为（接线点写在 `LAYER_SPECS[*].wired_at`）。
3. **增量与组合分开报**：
   · `leave_one_out[layer]` = full_stack − (full_stack 去掉该层)：这一层的**边际贡献**；
   · `combination` = full_stack − phase01_baseline（全部可选层关掉，即 Phase 01 行为），
     并给出 `additivity_residual = combination − Σ(leave_one_out)`：
     残差≈0 说明各层近似可加；残差明显不为 0 说明层间有交互（互补或互相遮蔽），
     **必须一起读**，不能只挑好看的单个增量。

## 无对象的层

某些层在真机数据上根本没有可判对象（真机 `qa_conflicts` 0 行、线上 `memory_item` 0 行、
离线无网/无 RAGFlow）。这类层照跑不误，但结论写成 `NO_OBJECT` + 原因码，
**绝不把"没有对象"混成"增量为 0"**（`null` 与 `0` 必须分开）。
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Mapping, Sequence

import qa_e2e

ABLATION_VERSION = "qa-ablation-ladder-v1"

RETRIEVAL_METRIC_KEYS = qa_e2e.E2E_METRIC_KEYS
IN_SCOPE_METRIC_KEYS = ("in_scope_questions", "in_scope_hit_rate", "in_scope_recall_at_k",
                        "in_scope_citation_precision", "in_scope_citation_recall",
                        "in_scope_grounded_rate", "in_scope_avg_evidence")
ANSWER_METRIC_KEYS = (
    "claims_verified_supported", "unsupported_claim_rate", "claim_coverage", "contradictions",
    "counter_evidence", "unresolved_gaps", "gaps", "stop_reason",
    "citation_checked", "citation_traceable_rate", "checks_passed", "checks_total",
    "checks_pass_rate", "blocking_before", "blocking_after", "retrieval_calls",
    "insufficient_runs", "memory_recalled", "memory_written", "skills_selected",
    "context_items", "context_tokens", "context_trimmed",
)
METRIC_KEYS = tuple(RETRIEVAL_METRIC_KEYS) + tuple(IN_SCOPE_METRIC_KEYS) + tuple(ANSWER_METRIC_KEYS)


def cell_states(layer: str = "", *, all_on: bool = True) -> dict:
    """一格的开关组合：`all_on=True` 给全部层开（或去掉一层），否则全关。"""
    states = {name: bool(all_on) for name in qa_e2e.LAYER_IDS}
    if layer and all_on:
        states[layer] = False
    return states


def ladder_cells() -> list:
    """消融阶梯的格子清单（确定性顺序，可复算）。"""
    cells = [
        {"cell": "phase01_baseline", "kind": "baseline", "layer": "",
         "states": cell_states(all_on=False),
         "note": "全部可选层关闭 = Phase 01 行为（证据层/核验层/舰队/执行图/缺口/证据图/上下文包/记忆/技能/终验/获取全关）"},
        {"cell": "full_stack", "kind": "full", "layer": "",
         "states": cell_states(all_on=True), "note": "本包全部可选层打开"},
        {"cell": "retrieval_only", "kind": "combo", "layer": "",
         "states": {name: (spec["sides"] == ["retrieval"] or "retrieval" in spec["sides"])
                    for name, spec in qa_e2e.LAYER_BY_ID.items()},
         "note": "只开作用在检索侧的层（证据层/核验层/舰队/执行图/缺口）"},
        {"cell": "answer_only", "kind": "combo", "layer": "",
         "states": {name: ("answer" in spec["sides"])
                    for name, spec in qa_e2e.LAYER_BY_ID.items()},
         "note": "只开作用在回答侧的层（证据图/上下文包/记忆/技能/终验/获取）"},
    ]
    for layer in qa_e2e.LAYER_IDS:
        cells.append({"cell": "minus_%s" % layer, "kind": "leave_one_out", "layer": layer,
                      "states": cell_states(layer, all_on=True),
                      "note": "full_stack 去掉 %s（%s）" % (layer,
                                                            qa_e2e.LAYER_BY_ID[layer]["phase"])})
    return cells


def _metrics_of(outcome: Mapping, *, in_scope_ids=(), limit: int = 12) -> dict:
    metrics = dict(outcome.get("retrieval_metrics") or {})
    totals = dict(outcome.get("answer_totals") or {})
    merged = {}
    for key in RETRIEVAL_METRIC_KEYS:
        merged[key] = metrics.get(key)
    for key in ANSWER_METRIC_KEYS:
        merged[key] = totals.get(key)
    # 快照内语料子集（唯一公平的取回对比基准）：全 12 题里有 5 题的行业包不在快照里，
    # 它们必然 no-hit，会把"语料不全"读成"检索变差"。
    rows = ((outcome.get("retrieval") or {}).get("rows")
            or outcome.get("retrieval_rows") or [])
    if in_scope_ids:
        subset = [row for row in rows if str(row.get("id")) in in_scope_ids]
        scoped = qa_e2e.retrieval_metrics(subset, limit=limit)
        for key in ("hit_rate", "recall_at_k", "citation_precision", "citation_recall",
                    "grounded_rate", "avg_evidence"):
            merged["in_scope_%s" % key] = scoped.get(key)
        merged["in_scope_questions"] = scoped.get("questions")
    return merged


def run_cell(cell: Mapping, *, template: Mapping, questions: Sequence[Mapping],
             snapshot: Mapping | None, limit: int = 12, mode: str = "standard",
             max_questions: int = 0, max_answer_runs: int = 0,
             workdir: str = "") -> dict:
    """跑一格：独立临时库 + 只翻开关 + 真跑两端（检索侧 / 回答侧）。"""
    from qa_storage import QaStore
    from sqlite_database import SQLiteDatabase

    workdir = workdir or tempfile.mkdtemp(prefix="qa-p17-cell-")
    os.makedirs(workdir, exist_ok=True)
    db_path = qa_e2e.clone_database(template["template_path"],
                                    os.path.join(workdir, "%s.sqlite3" % cell["cell"]))
    database = SQLiteDatabase(db_path)
    database.connect()
    database.create_tables()
    if database.backend != "sqlite":
        raise AssertionError("消融必须跑在隔离临时 sqlite 上")
    store = QaStore(database)
    store.ensure_schema()
    selected = list(questions[:max_questions] if max_questions else questions)
    try:
        with qa_e2e.layer_switches(cell["states"]):
            outcome = qa_e2e.run_suite(
                database, selected, snapshot, limit=limit, mode=mode, store=store,
                answer_store=store, write_memories=True)
        answer_rows = outcome.get("answer", {}).get("rows") or []
        packs = {str(item) for item in (template.get("packs") or [])}
        in_scope_ids = {str(item.get("id")) for item in selected
                        if str(item.get("industry_pack_id") or "") in packs}
        metrics = _metrics_of(outcome, in_scope_ids=in_scope_ids, limit=limit)
        layers = outcome.get("layers") or {}
        return {
            "cell": cell["cell"], "kind": cell["kind"], "layer": cell.get("layer") or "",
            "note": cell.get("note") or "",
            "switches": {name: bool(value) for name, value in dict(cell["states"]).items()},
            "states": {name: {"runtime_default": item.get("runtime_default"),
                              "enabled_in_cell": bool(dict(cell["states"]).get(name)),
                              "switch": item.get("switch")}
                       for name, item in layers.items()},
            "metrics": metrics,
            "retrieval_metrics": dict(outcome.get("retrieval_metrics") or {}),
            "answer_totals": dict(outcome.get("answer_totals") or {}),
            "no_object": qa_e2e.no_object_report(
                (outcome.get("retrieval") or {}).get("receipts") or [],
                outcome.get("answer_totals") or {}),
            "cache_reset": dict(outcome.get("cache_reset") or {}),
            "substitutions": dict(outcome.get("substitutions") or {}),
            "questions": len(selected),
            "retrieval_rows": (outcome.get("retrieval") or {}).get("rows") or [],
            "retrieval_receipts": (outcome.get("retrieval") or {}).get("receipts") or [],
            "answer_rows": answer_rows,
        }
    finally:
        try:
            database.connection.close()
        except Exception:  # noqa: BLE001
            pass


def _safe_delta(left, right) -> object:
    """`right - left`；任一侧是 null/非数就返回 None（**不用 0 充数**）。"""
    if isinstance(left, (int, float)) and isinstance(right, (int, float)) and not isinstance(left, bool):
        value = float(right) - float(left)
        return round(value, 6) if abs(value) < 1e6 else value
    return None


def increments(cells: Sequence[Mapping]) -> dict:
    """逐层增量（leave_one_out）与组合效应（full_stack − phase01_baseline）。"""
    by_cell = {str(cell.get("cell")): cell for cell in cells}
    full = by_cell.get("full_stack") or {}
    base = by_cell.get("phase01_baseline") or {}
    full_metrics = dict(full.get("metrics") or {})
    base_metrics = dict(base.get("metrics") or {})
    leave_one_out = {}
    for cell in cells:
        if cell.get("kind") != "leave_one_out":
            continue
        layer = str(cell.get("layer") or "")
        without = dict(cell.get("metrics") or {})
        leave_one_out[layer] = {
            "phase": qa_e2e.LAYER_BY_ID.get(layer, {}).get("phase", ""),
            "cell": cell.get("cell"),
            "enabled_value": {key: value for key, value in (full_metrics or {}).items()},
            "without_layer": without,
            "contribution": {key: _safe_delta(without.get(key), full_metrics.get(key))
                              for key in METRIC_KEYS},
            "no_object": dict(cell.get("no_object") or {}),
        }
    combination = {key: _safe_delta(base_metrics.get(key), full_metrics.get(key))
                   for key in METRIC_KEYS}
    additive = {key: sum(value for value in
                         (item["contribution"].get(key) for item in leave_one_out.values())
                         if isinstance(value, (int, float)))
                for key in METRIC_KEYS}
    residual = {}
    for key in METRIC_KEYS:
        combo = combination.get(key)
        total = additive.get(key)
        residual[key] = (None if combo is None or total is None or total == 0
                         else round(float(combo) - float(total), 6))
    return {
        "version": ABLATION_VERSION,
        "full_stack": full_metrics,
        "phase01_baseline": base_metrics,
        "leave_one_out": leave_one_out,
        "combination": {"delta_vs_baseline": combination,
                        "sum_of_leave_one_out": additive,
                        "additivity_residual": residual,
                        "note": ("组合效应 = full_stack − phase01_baseline；"
                                 "sum_of_leave_one_out 是 11 个单层增量的算术和；"
                                 "additivity_residual = 组合效应 − 单层增量之和（非可加性）。"
                                 "残差为 null 表示该指标至少一侧无对象/无值。")},
    }


def no_object_summary(cells: Sequence[Mapping]) -> dict:
    """跨格汇总"哪些层/哪些通道在真机数据上无对象"（如实标注，不硬凑）。"""
    summary = {}
    for cell in cells:
        for side, items in (cell.get("no_object") or {}).items():
            for name, payload in (items or {}).items():
                if not isinstance(payload, Mapping):
                    continue
                key = "%s.%s" % (side, name)
                entry = summary.setdefault(key, {"objects": 0, "reason_codes": {}, "cells": []})
                objects = int(payload.get("objects") or 0)
                entry["objects"] = max(entry["objects"], objects)
                reason = str(payload.get("reason_code") or "")
                if reason:
                    entry["reason_codes"][reason] = entry["reason_codes"].get(reason, 0) + 1
                entry["cells"].append(cell.get("cell"))
    for name, spec in qa_e2e.LAYER_BY_ID.items():
        key = "layer.%s" % name
        summary[key] = {
            "objects": None if spec["default"] else 0,
            "reason_codes": {"SWITCH_OFF_BY_DEFAULT": 1} if not spec["default"] else {},
            "cells": [],
            "note": ("该层生产默认关闭（%s=0）：真机生产链路上本阶段之前从未跑过它，"
                     "增量只能来自本次消融" % spec["switch"]) if not spec["default"] else
                    "该层生产默认打开",
        }
    return summary


def run_ladder(*, template: Mapping, questions: Sequence[Mapping], snapshot: Mapping | None,
               limit: int = 12, mode: str = "standard", workdir: str = "",
               cells: Sequence[Mapping] | None = None, max_questions: int = 0) -> dict:
    """跑完整条阶梯（默认 15 格：2 基线 + 2 组合 + 11 单层）。"""
    workdir = workdir or tempfile.mkdtemp(prefix="qa-p17-ladder-")
    ladder = list(cells or ladder_cells())
    results = []
    for cell in ladder:
        results.append(run_cell(cell, template=template, questions=questions, snapshot=snapshot,
                                limit=limit, mode=mode, workdir=workdir,
                                max_questions=max_questions))
    report = {
        "version": ABLATION_VERSION,
        "report_version": ABLATION_VERSION,
        "suite_version": qa_e2e.E2E_SUITE_VERSION,
        "corpus": {key: template.get(key) for key in
                   ("snapshot_path", "snapshot_version", "captured_at_utc", "packs", "counts")},
        "limit": int(limit), "mode": mode,
        "layers": qa_e2e.layer_states(),
        "cells": results,
        "increments": increments(results),
        "no_object": no_object_summary(results),
    }
    report["fingerprint"] = qa_e2e.fingerprint({
        "cells": [(cell["cell"], cell["switches"], cell["metrics"]) for cell in results]})
    return report


def format_table(report: Mapping) -> str:
    """控制台表格（人读用；JSON 才是机器口径）。"""
    lines = []
    full = (report.get("increments") or {}).get("full_stack") or {}
    base = (report.get("increments") or {}).get("phase01_baseline") or {}
    lines.append("指标                         phase01     full_stack  组合效应")
    for key in ("hit_rate", "recall_at_k", "citation_precision", "citation_recall",
                "avg_evidence", "avg_ms", "unsupported_claim_rate", "claim_coverage",
                "citation_traceable_rate", "checks_pass_rate", "context_tokens",
                "memory_recalled", "skills_selected"):
        delta = ((report.get("increments") or {}).get("combination") or {}).get(
            "delta_vs_baseline", {}).get(key)
        lines.append("%-26s %-11s %-11s %s" % (key, base.get(key), full.get(key), delta))
    lines.append("")
    lines.append("逐层增量（full_stack 去掉该层后的变化；正数=该层有正贡献）")
    for layer, item in ((report.get("increments") or {}).get("leave_one_out") or {}).items():
        delta = item.get("contribution") or {}
        lines.append("  %-16s %s" % (layer, json.dumps(
            {key: delta.get(key) for key in ("recall_at_k", "citation_precision", "avg_evidence",
                                             "avg_ms", "claim_coverage", "checks_pass_rate",
                                             "context_tokens", "skills_selected")},
            ensure_ascii=False)))
    return "\n".join(lines)


__all__ = ["ABLATION_VERSION", "METRIC_KEYS", "ladder_cells", "cell_states", "run_cell",
           "run_ladder", "increments", "no_object_summary", "format_table"]

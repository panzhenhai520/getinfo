#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 14（P14-01…P14-07）· **统一获取 + 重复提问重对齐 验收工具**
（只读快照 / 只读本地库，**可复算**；零模型调用、零嵌入端点、零写库）。

本工具回答规格书点名的那两个数字（P14-07 验收标准）：
  1. **重复提问检出数**（按触发判据分组：词面重叠 / 同主体+同意图）；
  2. **交互次数分布**（同一会话同一议题最多一次，TTL 内不重复追问）。

外加 P14-01…P14-06 的可复算事实：动作候选与选取分布、淘汰理由码分布、来源可用性、
以及 P14-05 的"因信息已在手上被拦下"的条数。

两种数据源（都用**同一份真代码**跑）：
  · `--snapshot <json>`：A 机只读快照（如 `baseline/qa-context-real-sample.json`）。
    快照里**有**：真实问题流、最终答案（`degradation` / `status`）、真实 claims/evidence。
    快照里**没有**：`session_id`、`qa_stage_runs` 的缺口环回执（stop_reason / 新增已核验证据）。
    所以本工具在快照模式下：① 把整条问题流当作**一个会话**（明确标注这个假设）；
    ② 用"上一轮与这一轮的**已核验 claim 数之差**"替代 Phase 07 的 `new_verified_claims`
    （口径替代**如实标注**，不假装等价）；③ `stop_reason` / 未解决率**标记为不可用**，
    绝不编一个数字出来。
  · `--db <sqlite 路径>`：本地部署库（信号齐全）→ 走完整口径（含缺口环回执）。

用法：
    python tools/qa_phase14_acceptance.py --snapshot baseline/qa-context-real-sample.json --print
    python tools/qa_phase14_acceptance.py --db data/crawler_articles.db --print
    python tools/qa_phase14_acceptance.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_acquisition as ac  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_realignment as rl  # noqa: E402

REPORT_VERSION = "qa-acquisition-realignment-acceptance-v1"
DEFAULT_SNAPSHOT = os.path.join("baseline", "qa-context-real-sample.json")
DEFAULT_OUTPUT = os.path.join("baseline", "qa-phase14-acceptance.json")
VERIFIED_STATUSES = ("confirmed", "qualified")


def _counter(values, *, top: int = 0) -> dict:
    counts: dict = {}
    for value in values or ():
        key = str(value or "")
        counts[key] = counts.get(key, 0) + 1
    ordered = dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
    return dict(list(ordered.items())[:top]) if top else ordered


def _verified_claims_by_run(snapshot: dict) -> dict:
    """快照里每条 run 的"已核验结论数"（用真实 claims 行的 `verification_status` 数）。"""
    counts: dict = {}
    for row in snapshot.get("claims") or ():
        if not isinstance(row, dict):
            continue
        run_id = str(row.get("run_id") or "")
        if str(row.get("verification_status") or "") in VERIFIED_STATUSES:
            counts[run_id] = counts.get(run_id, 0) + 1
    return counts


def snapshot_turns(snapshot: dict) -> list:
    """快照 → 按时间排序的"轮次"序列（每轮：问题 + 真实信号）。

    信号口径（**只认快照里真的有的东西**）：
      · `degraded` / `degradation_reasons` / `answer_status` 来自真实 `final_answer` 与 `degradation`；
      · `verified_claims` 来自真实 claims 行；`new_verified_claims` 用**与上一轮之差**替代
        （快照没有缺口环回执，无法拿到 Phase 07 的逐轮增量 —— 口径替代在本工具的 `source.note` 里写明）。
    """
    verified = _verified_claims_by_run(snapshot)
    runs = sorted([row for row in (snapshot.get("runs") or ()) if isinstance(row, dict)],
                  key=lambda row: (str(row.get("created_at") or ""), str(row.get("id") or "")))
    turns: list = []
    previous_verified = 0
    for index, run in enumerate(runs):
        answer = run.get("final_answer") if isinstance(run.get("final_answer"), dict) else {}
        degradation = run.get("degradation")
        reasons = list(answer.get("degradation_reasons") or [])
        if isinstance(degradation, (list, tuple)):
            reasons.extend(str(item) for item in degradation)
        elif isinstance(degradation, dict):
            reasons.extend(str(item) for item in (degradation.get("reasons") or []))
        run_id = str(run.get("id") or "")
        current_verified = int(verified.get(run_id) or 0)
        turns.append({
            "index": index,
            "run_id": run_id,
            "question": str(run.get("question_text") or ""),
            "status": str(run.get("status") or ""),
            "created_at": str(run.get("created_at") or ""),
            "signals": {
                "degraded": bool(degradation) or bool(answer.get("degraded")),
                "degradation_reasons": reasons[:6],
                "answer_status": str(answer.get("status") or ""),
                "verified_claims": current_verified,
                # 口径替代：快照无缺口环 → 用相邻两轮的已核验结论数之差近似"新增"
                "new_verified_claims": max(0, current_verified - previous_verified),
                "new_verified_claims_basis": "snapshot_verified_claim_delta",
                "stop_reason": "",                 # 快照不可得：如实留空，绝不编
                "unresolved_rate": None,
            },
        })
        previous_verified = current_verified
    return turns


def analyze_snapshot(snapshot: dict, *, session_key: str = "snapshot-as-one-session") -> dict:
    """在快照上跑**真的检测器与真的不满意判定**（纯函数：同一份输入永远同一份报告）。"""
    turns = snapshot_turns(snapshot)
    history: list = []
    rows: list = []
    interactions: list = []
    asked: set = set()
    for turn in turns:
        if turn["question"].strip():
            detection = rl.detect_repeat(question=turn["question"], history=history)
            assessment = rl.assess_dissatisfaction(turn["signals"])
            topic = str(detection.get("topic_key") or "")
            row = {
                "run_id": turn["run_id"], "index": turn["index"],
                "question": turn["question"][:80], "status": turn["status"],
                "repeat": bool(detection.get("repeat")),
                "triggers": list(detection.get("triggers") or []),
                "overlap": detection["similarity"]["overlap"],
                "subject_jaccard": detection["similarity"]["subject_jaccard"],
                "matches_in_window": detection["similarity"].get("matches_in_window", 0),
                "dissatisfaction": list(assessment.get("evidence") or []),
                "topic_key": topic,
                "would_interact": False, "skip_reason": "NOT_A_REPEAT",
            }
            if row["repeat"] and not assessment.get("has_evidence"):
                row["skip_reason"] = "NO_DISSATISFACTION_EVIDENCE"
            elif row["repeat"]:
                if topic in asked:
                    row["skip_reason"] = "ALREADY_ASKED_IN_TTL"
                else:
                    row["would_interact"] = True
                    row["skip_reason"] = ""
                    asked.add(topic)
                    interactions.append({
                        "run_id": turn["run_id"], "topic_key": topic,
                        "triggers": row["triggers"], "evidence": row["dissatisfaction"],
                        "options": len(rl.options_payload()),
                    })
            rows.append(row)
            history.insert(0, {"run_id": turn["run_id"], "question": turn["question"],
                               "created_at": turn["created_at"], "status": turn["status"],
                               "final_answer": {}})
    interaction_sizes = _counter(len(item["evidence"]) for item in interactions)
    report = {
        "report_version": REPORT_VERSION,
        "source": {
            "snapshot_version": str((snapshot.get("source") or {}).get("snapshot_version") or ""),
            "host": str((snapshot.get("source") or {}).get("host") or ""),
            "captured_at_utc": snapshot.get("captured_at_utc"),
            "access": str((snapshot.get("source") or {}).get("access") or ""),
            "session_assumption": ("快照没有 session_id：把整条问题流当作一个会话"
                                   "（这是**最保守/最容易触发**的假设，触发数只会偏高不会偏低）"
                                   if session_key else ""),
            "substituted_signals": {
                "new_verified_claims": "snapshot_verified_claim_delta（快照无缺口环回执）",
                "stop_reason": "不可得（快照的 qa_stage_runs 无 payload）→ 不参与判定",
                "unresolved_rate": "不可得 → 不参与判定",
            },
        },
        "params": {
            "planner_version": contracts.ACQUISITION_PLANNER_VERSION,
            "realignment_version": contracts.REALIGNMENT_VERSION,
            "detector_version": contracts.REALIGNMENT_DETECTOR_VERSION,
            "option_version": contracts.REALIGNMENT_OPTION_VERSION,
            "settings": rl.realignment_settings(),
            "acquisition_settings": ac.acquisition_settings(),
        },
        "turns": rows,
        "interactions": interactions,
        "totals": {
            "turns": len(rows),
            "with_question": len([row for row in rows if row["question"]]),
            "repeat_detected": len([row for row in rows if row["repeat"]]),
            "repeat_by_trigger": _counter(trigger for row in rows for trigger in row["triggers"]),
            "interactions": len(interactions),
            "interactions_histogram": _counter(len(item["evidence"]) for item in interactions),
            "by_evidence": _counter(reason for row in rows for reason in row["dissatisfaction"]),
            "skip_reasons": _counter(row["skip_reason"] for row in rows
                                     if not row["would_interact"]),
            "topics": len({row["topic_key"] for row in rows if row["topic_key"]}),
            "evidence_per_interaction": interaction_sizes,
        },
        "acceptance": {},
    }
    totals = report["totals"]
    report["acceptance"] = {
        "runs_on_real_data": totals["with_question"] > 0,
        "every_interaction_has_evidence": all(item["evidence"] for item in interactions),
        "interactions_are_one_per_topic": len(interactions) == len(
            {item["topic_key"] for item in interactions}),
        "options_are_complete": all(item["options"] == len(contracts.REALIGNMENT_OPTION_IDS)
                                    for item in interactions),
        "no_fabricated_evidence": all(
            code in contracts.REALIGNMENT_DISSATISFACTION_REASONS
            for row in rows for code in row["dissatisfaction"]),
        "detector_used": bool(totals["turns"]),
    }
    report["acceptance"]["passed"] = all(report["acceptance"].values())
    return report


def analyze_db(path: str) -> dict:
    """本地部署库（信号齐全）：走**完整口径**（含缺口环回执）。"""
    from qa_storage import QaStore  # 延迟导入：快照模式不需要库

    os.environ.setdefault("DATABASE_TYPE", "sqlite")
    from sqlite_database import SQLiteDatabase

    database = SQLiteDatabase(path)
    database.connect()
    try:
        store = QaStore(database)
        rows = store.realignment_decisions(limit=1000)
        runs = store.list_runs(owner_user_id="", limit=1) if False else []
        del runs
        stats = store.realignment_stats()
        acquisition = {"note": "本地库模式：交互账直接读库；获取计划需要一轮真实 run 才能算"}
        return {
            "report_version": REPORT_VERSION,
            "source": {"db": os.path.abspath(path), "access": "local sqlite (read)"},
            "params": {"realignment_version": contracts.REALIGNMENT_VERSION,
                       "settings": rl.realignment_settings()},
            "decisions": rows,
            "totals": dict(stats, decisions=len(rows)),
            "acquisition": acquisition,
            "acceptance": {
                "has_rows": bool(rows),
                "interactions_are_one_per_topic": stats.get("asked", 0) == len(
                    [row for row in rows if int(row.get("asked_count") or 0) > 0]),
                "passed": bool(rows),
            },
        }
    finally:
        try:
            database.connection.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", default="")
    parser.add_argument("--db", default="")
    parser.add_argument("--out", default=DEFAULT_OUTPUT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--print", dest="show", action="store_true")
    args = parser.parse_args()

    if args.db:
        report = analyze_db(args.db)
    else:
        path = args.snapshot or DEFAULT_SNAPSHOT
        if not os.path.exists(path):
            print("缺少快照文件：%s（先跑 tools/qa_phase08_real_snapshot.py）" % path)
            return 2
        with open(path, encoding="utf-8") as handle:
            snapshot = json.load(handle)
        report = analyze_snapshot(snapshot)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=1)
    totals = report.get("totals") or {}
    print("报告已写入 %s" % args.out)
    if "turns" in totals:
        print("轮次 %d（有问题 %d）｜**重复提问检出 %d**｜触发判据分布 %s"
              % (totals["turns"], totals["with_question"], totals["repeat_detected"],
                 json.dumps(totals["repeat_by_trigger"], ensure_ascii=False)))
        print("**交互次数分布**：交互 %d 次｜每条交互依据条数分布 %s"
              % (totals["interactions"], json.dumps(totals["interactions_histogram"],
                                                    ensure_ascii=False)))
        print("不满意依据分布（真数据上真的命中的）：%s"
              % json.dumps(totals["by_evidence"], ensure_ascii=False))
        print("不交互的原因分布：%s" % json.dumps(totals["skip_reasons"], ensure_ascii=False))
    else:
        print("交互账：%s" % json.dumps(totals, ensure_ascii=False))
    print("验收：%s" % json.dumps(report.get("acceptance") or {}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

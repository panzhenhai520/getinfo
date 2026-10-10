#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 05（P05-01…P05-05）规划/执行图验收：**三档路径的规划输出 + 预算 + 停止原因**（真跑）。

它回答的问题：把"问题理解 → 子问题/Claim/依赖 → 执行图 → 预算与停止原因"接起来之后，
同一批问题在 fast / standard / deep 三档下分别会跑几个节点、几跳、预算多少、为什么停。

口径：
  · 题目默认读 Phase 00 冻结的 benchmark `config/qa_acceptance_questions.json`
    （同一份题，不另造题）；`--questions` 可换成真机导出的真实问题清单；
  · **纯规则、零模型调用、零网络**：只调 `qa_query_interpreter` / `qa_query_decompose` /
    `qa_execution_graph` 与既有预算常量；
  · before/after 口径：
      - before = "接线前"能拿到的信息：既有分解器给几跳 + 既有阶段预算表；
      - after  = 执行图给出的节点数/并行组/每节点预算/关键路径估算/停止原因。

用法：
    python tools/qa_phase05_planner_acceptance.py --json
    python tools/qa_phase05_planner_acceptance.py --out baseline/qa-planner-acceptance.json \
        --history data/qa_planner_history.jsonl
    # 真机真实问题（只读导出的 JSON：[{"id","question","industry_pack_id"}]）
    python tools/qa_phase05_planner_acceptance.py --questions /tmp/real_questions.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_execution_graph as graph_module  # noqa: E402
import qa_query_interpreter as interpreter  # noqa: E402
from qa_graph_contracts import QA_PATHS, validate  # noqa: E402
from qa_resilience import STAGE_BUDGET_SECONDS  # noqa: E402

REPORT_VERSION = "qa-planner-acceptance-v1"
DEFAULT_QUESTIONS = os.path.join("config", "qa_acceptance_questions.json")
PRODUCTION_GRAPH_FLAGS = ("QA_EXECUTION_GRAPH", "QA_EXECUTION_GRAPH_NODE_RUNS")


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _fingerprint(value) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def load_questions(path: str) -> tuple:
    """读题目：兼容 Phase 00 的信封形态与纯数组形态（与既有验收工具同口径）。"""
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict):
        questions = list(payload.get("questions") or [])
        meta = {key: value for key, value in payload.items() if key != "questions"}
    else:
        questions = list(payload or [])
        meta = {"benchmark_version": "", "source": "bare-list"}
    items = []
    for index, item in enumerate(questions):
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        if not question:
            continue
        items.append({"id": str(item.get("id") or ("q%d" % (index + 1))),
                      "question": question,
                      "kind": str(item.get("kind") or ""),
                      "industry_pack_id": str(item.get("industry_pack_id") or "")})
    return items, meta


def _baseline_view(question: str, *, mode: str, policy=None) -> dict:
    """接线前视角：既有分解器给几跳 + 既有阶段预算之和（**没有**节点/并行组/停止原因概念）。"""
    import config

    from qa_query_decompose import decompose

    max_hops = max(1, min(int(getattr(config, "QA_MAX_HOPS", 3) or 3), 5))
    decomposition = decompose(question, max_hops=max_hops)
    stages = [stage for stage, _fallback in graph_module.PATH_STAGES.get(mode, ())]
    return {
        "hops": len(decomposition.get("hops") or []),
        "is_multi_hop": bool(decomposition.get("is_multi_hop")),
        "pattern": str(decomposition.get("pattern") or ""),
        "stages": stages,
        # 用同一个 path_budget 口径（含 qa_policy.research_timeout_seconds），
        # 这样 before/after 的预算数字可比——Phase 05 没有改预算常量，只是把它显式化。
        "stage_budget_seconds": graph_module.path_budget(mode, policy=policy)["total_seconds"],
        "max_hops": max_hops,
        "nodes": 0,               # 接线前：没有"节点"这个概念
        "parallel_groups": 0,     # 接线前：没有并行组/barrier
        "per_node_budget": 0,     # 接线前：只有阶段预算，没有每节点预算
        "stop_reason": "",        # 接线前：没有统一的停止原因枚举回执
    }


def plan_one(question: str, *, mode: str, policy=None, hunters=None) -> dict:
    """一题一档：解释 → 计划 → 执行图（不跑检索、不调模型）。"""
    interpretation = interpreter.interpret_query(question)
    graph = graph_module.build_execution_graph(
        question, plan={}, mode=mode, policy=policy, interpretation=interpretation,
        hunters=hunters, level2_enabled=True)
    nodes = graph["nodes"]
    ok, note = validate("execution_graph", graph)
    contract_bad = [node["node_id"] for node in nodes
                    if not validate("execution_node", node)[0]]
    return {
        "path": graph["path"],
        "path_source": graph["path_source"],
        "intent": graph["intent"],
        "complexity": graph["complexity"],
        "answer_type": graph["answer_type"],
        "hops": int(((graph.get("research_plan") or {}).get("decomposition") or {}).get("hop_count") or 0),
        "sub_questions": len((graph.get("research_plan") or {}).get("sub_questions") or []),
        "claims": len((graph.get("research_plan") or {}).get("claims") or []),
        "claims_plan_only": len([item for item in (graph.get("research_plan") or {}).get("claims") or []
                                 if item.get("plan_only")]),
        "evidence_requirements": len((graph.get("research_plan") or {}).get("evidence_requirements") or []),
        "dependencies": len((graph.get("research_plan") or {}).get("dependencies") or []),
        "node_counts": dict(graph.get("node_counts") or {}),
        "parallel_groups": len(graph.get("parallel_groups") or []),
        "parallel_groups_multi": len([group for group in (graph.get("parallel_groups") or [])
                                      if group.get("parallel")]),
        "barriers": int(graph["node_counts"]["barriers"]),
        "budget_total_seconds": graph["budget"]["total_seconds"],
        "budget_estimated_seconds": graph["budget"]["estimated_wall_clock_seconds"],
        "budget_allocated_seconds": graph["budget"]["allocated_seconds"],
        "budget_enforced_nodes": int(graph["node_counts"]["enforced"]),
        "cut_nodes": list(graph["budget"]["cut_nodes"]),
        "infeasible": bool(graph["budget"]["infeasible"]),
        "stop_reason": graph["stop_reason"],
        "stop_reasons": [item["reason"] for item in graph["stop_reasons"]],
        "deferred_nodes": [node["node_id"] for node in nodes if node["status"] == "deferred"],
        "failure_policies": sorted({node["failure_policy"] for node in nodes}),
        "contract_ok": bool(ok) and not contract_bad,
        "contract_note": note if not ok else ("节点契约不过：%s" % contract_bad if contract_bad else ""),
        "graph_fingerprint": _fingerprint(graph),
    }


def run(questions, *, modes=QA_PATHS, policy=None, hunters=None, verbose=True) -> dict:
    rows = []
    for item in questions:
        row = {"id": item["id"], "question": item["question"], "kind": item["kind"],
               "industry_pack_id": item["industry_pack_id"], "paths": {}}
        for mode in modes:
            row["paths"][mode] = plan_one(item["question"], mode=mode, policy=policy, hunters=hunters)
            if verbose:
                value = row["paths"][mode]
                print("[%s] %-8s 意图 %-12s 节点 %2d（可跑 %2d/占位 %d）跳 %d 预算 %.0fs/估算 %.0fs 停 %s"
                      % (item["id"], mode, value["intent"], value["node_counts"]["total"],
                         value["node_counts"]["runnable"], value["node_counts"]["deferred"],
                         value["hops"], value["budget_total_seconds"],
                         value["budget_estimated_seconds"], value["stop_reason"]))
        rows.append(row)
    return rows


def _mode_summary(rows, mode: str) -> dict:
    values = [row["paths"][mode] for row in rows]
    if not values:
        return {}
    total_nodes = sum(item["node_counts"]["total"] for item in values)
    return {
        "questions": len(values),
        "avg_nodes": round(total_nodes / len(values), 3),
        "nodes_total": total_nodes,
        "runnable_total": sum(item["node_counts"]["runnable"] for item in values),
        "deferred_total": sum(item["node_counts"]["deferred"] for item in values),
        "hops_total": sum(item["hops"] for item in values),
        "hops_avg": round(sum(item["hops"] for item in values) / len(values), 3),
        "claims_total": sum(item["claims"] for item in values),
        "dependencies_total": sum(item["dependencies"] for item in values),
        "parallel_groups_total": sum(item["parallel_groups"] for item in values),
        "parallel_groups_multi_total": sum(item["parallel_groups_multi"] for item in values),
        "barriers_total": sum(item["barriers"] for item in values),
        "budget_total_seconds_avg": round(sum(item["budget_total_seconds"] for item in values)
                                          / len(values), 3),
        "budget_estimated_seconds_avg": round(sum(item["budget_estimated_seconds"] for item in values)
                                              / len(values), 3),
        "enforced_nodes_total": sum(item["budget_enforced_nodes"] for item in values),
        "cut_nodes_total": sum(len(item["cut_nodes"]) for item in values),
        "stop_reason_distribution": _counts([item["stop_reason"] for item in values]),
        "intent_distribution": _counts([item["intent"] for item in values]),
        "complexity_distribution": _counts([item["complexity"] for item in values]),
        "contract_ok": all(item["contract_ok"] for item in values),
    }


def _counts(values) -> dict:
    result: dict = {}
    for value in values:
        key = str(value)
        result[key] = result.get(key, 0) + 1
    return result


def compare(rows, *, policy=None) -> dict:
    """before（接线前可见信息）vs after（执行图）逐题 + 三档汇总。"""
    baseline = {}
    for mode in QA_PATHS:
        baseline[mode] = {
            "views": [_baseline_view(row["question"], mode=mode, policy=policy) for row in rows],
        }
        baseline[mode]["hops_total"] = sum(item["hops"] for item in baseline[mode]["views"])
        baseline[mode]["stage_budget_seconds_avg"] = round(
            sum(item["stage_budget_seconds"] for item in baseline[mode]["views"])
            / max(1, len(baseline[mode]["views"])), 3)
        baseline[mode]["nodes_total"] = 0
        baseline[mode]["parallel_groups_total"] = 0
        baseline[mode]["per_node_budget_total"] = 0
        baseline[mode]["views"] = [{key: value for key, value in item.items() if key != "stages"}
                                   for item in baseline[mode]["views"]]
    after = {mode: _mode_summary(rows, mode) for mode in QA_PATHS}
    delta = {}
    for mode in QA_PATHS:
        delta[mode] = {
            "hops_total": after[mode]["hops_total"] - baseline[mode]["hops_total"],
            "budget_total_seconds_avg": round(after[mode]["budget_total_seconds_avg"]
                                              - baseline[mode]["stage_budget_seconds_avg"], 3),
            "nodes_total": after[mode]["nodes_total"] - baseline[mode]["nodes_total"],
            "parallel_groups_multi_total": (after[mode]["parallel_groups_multi_total"]
                                            - baseline[mode]["parallel_groups_total"]),
            "per_node_budget_total": (sum(len(item["paths"][mode]["cut_nodes"]) for item in rows)
                                      + after[mode]["questions"] * 0),
            "enforced_nodes_total": after[mode]["enforced_nodes_total"],
            "cut_nodes_total": after[mode]["cut_nodes_total"],
        }
    return {"baseline": baseline, "graph": after, "delta": delta}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Phase 05 规划/执行图验收（真跑、零模型调用）")
    parser.add_argument("--questions", default=DEFAULT_QUESTIONS)
    parser.add_argument("--out", default="")
    parser.add_argument("--history", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--fleet", action="store_true",
                        help="按当前 QA_HUNTER_FLEET 展开并行 Hunter 节点")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not os.path.exists(args.questions):
        print("题目文件不存在：%s" % args.questions)
        return 2
    questions, benchmark = load_questions(args.questions)
    if args.limit and args.limit > 0:
        questions = questions[:args.limit]
    if not questions:
        print("没有可用题目")
        return 2

    import qa_hunter_fleet as fleet_module
    from qa_policy import QaPolicyResolver

    hunters = list(fleet_module.DEFAULT_HUNTER_ORDER) if args.fleet else None
    policy = None
    try:
        policy = QaPolicyResolver().resolve(str(questions[0].get("industry_pack_id")
                                               or "family_office"))
    except Exception as exc:
        print("行业包 policy 取不到（用默认预算继续）：%s" % str(exc)[:120])

    print("题目：%d 题（benchmark=%s）｜路径：%s｜舰队节点：%s"
          % (len(questions), benchmark.get("benchmark_version") or "无版本",
             "/".join(QA_PATHS), "展开" if hunters else "不展开（首跳单节点）"))
    print("policy：%s" % ("已加载（research_timeout=%ss）" % getattr(policy, "research_timeout_seconds", "?")
                          if policy is not None else "无（用阶段预算默认值）"))
    print("-" * 100)
    rows = run(questions, policy=policy, hunters=hunters, verbose=not args.json)

    report = {
        "report_version": REPORT_VERSION,
        "generated_at_utc": _utc_now_z(),
        "questions_file": args.questions,
        "questions_fingerprint": _fingerprint([item["question"] for item in questions]),
        "benchmark": dict(benchmark or {}),
        "interpreter": interpreter.describe(),
        "planner": graph_module.describe(),
        "flags": {name: os.environ.get(name, "") for name in PRODUCTION_GRAPH_FLAGS},
        "policy": ({"research_timeout_seconds": getattr(policy, "research_timeout_seconds", None),
                    "standard_max_hops": getattr(policy, "standard_max_hops", None),
                    "deep_max_hops": getattr(policy, "deep_max_hops", None),
                    "max_queries_per_hop": getattr(policy, "max_queries_per_hop", None)}
                   if policy is not None else None),
        "hunters": hunters or [],
        "questions": len(questions),
        "comparison": compare(rows, policy=policy),
        "rows": rows,
    }
    text = json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
    if args.history:
        with open(args.history, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "generated_at_utc": report["generated_at_utc"],
                "questions": report["questions"],
                "questions_fingerprint": report["questions_fingerprint"],
                "summary": {mode: report["comparison"]["graph"][mode] for mode in QA_PATHS},
            }, ensure_ascii=False, sort_keys=True) + "\n")
    if args.json:
        print(text)
    else:
        print("-" * 100)
        for mode in QA_PATHS:
            summary = report["comparison"]["graph"][mode]
            print("  %-8s 节点均值 %5.2f（可跑 %d / 占位 %d）｜跳合计 %2d｜Claim %2d｜依赖 %2d｜"
                  "并行组 %2d（多节点 %d）｜barrier %2d｜预算均值 %.0fs（估算 %.0fs）｜"
                  "执行中预算节点 %d｜裁剪 %d｜契约 %s"
                  % (mode, summary["avg_nodes"], summary["runnable_total"],
                     summary["deferred_total"], summary["hops_total"], summary["claims_total"],
                     summary["dependencies_total"], summary["parallel_groups_total"],
                     summary["parallel_groups_multi_total"], summary["barriers_total"],
                     summary["budget_total_seconds_avg"], summary["budget_estimated_seconds_avg"],
                     summary["enforced_nodes_total"], summary["cut_nodes_total"],
                     "OK" if summary["contract_ok"] else "FAIL"))
            print("           停止原因分布 %s；意图分布 %s"
                  % (summary["stop_reason_distribution"], summary["intent_distribution"]))
        if args.out:
            print("报告已写入 %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

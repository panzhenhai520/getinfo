#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 8 遗留验收：邻域查询 **P95 < 300ms** 压测（只读）。

做法：取图里度数最高的若干节点（最坏情况），按指定跳数/时间过滤反复查询，
统计 P50/P95/P99/max；同时给出一条边的查询开销分解（节点查询 + 边查询）。
不写库、不调用模型，可与回填并行跑。

用法
    python tools/bench_kg_neighborhood.py                    # 默认 200 次、1 跳
    python tools/bench_kg_neighborhood.py --calls 300 --depth 2
    python tools/bench_kg_neighborhood.py --pack automotive_industry --json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ACCEPT_P95_MS = 300.0


def _hot_nodes(repository, pack_id: str, limit: int) -> list:
    """取度数最高的节点（最能代表最坏情况）。"""
    builder = None
    from kg_builder import KnowledgeGraphBuilder

    builder = KnowledgeGraphBuilder(repository=repository)
    where = "WHERE industry_pack_id=?" if pack_id else ""
    params = [pack_id] if pack_id else []
    rows = builder._fetch(
        "SELECT src_key AS key, COUNT(*) AS degree FROM kg_edges "
        + (where or "WHERE 1=1")
        + " GROUP BY src_key ORDER BY degree DESC LIMIT ?",
        params + [max(1, int(limit))],
    )
    return [str(row["key"]) for row in rows if str(row.get("key") or "")]


def benchmark(*, pack_id: str = "", depth: int = 1, calls: int = 200,
              since: str = "", until: str = "", as_of: str = "",
              relation_kind: str = "") -> dict:
    from intel_database import intel_repository
    from kg_builder import KnowledgeGraphBuilder

    intel_repository._ensure()
    builder = KnowledgeGraphBuilder(repository=intel_repository)
    nodes = _hot_nodes(intel_repository, pack_id, 20)
    if not nodes:
        return {"error": "图里没有节点（先跑 build_knowledge_graph.py --apply）", "calls": 0}

    latencies = []
    edge_counts = []
    for index in range(max(1, int(calls))):
        node = nodes[index % len(nodes)]
        started = time.perf_counter()
        result = builder.neighborhood(
            node, pack_id=pack_id, depth=depth, since=since, until=until,
            limit=50, relation_kind=relation_kind, as_of=as_of,
        )
        latencies.append((time.perf_counter() - started) * 1000.0)
        edge_counts.append(len(result.get("edges") or []))

    latencies.sort()

    def percentile(value: float) -> float:
        if not latencies:
            return 0.0
        position = min(len(latencies) - 1, int(round((value / 100.0) * (len(latencies) - 1))))
        return latencies[position]

    report = {
        "pack_id": pack_id or "*",
        "depth": depth,
        "calls": len(latencies),
        "nodes_sampled": len(nodes),
        "avg_edges": round(statistics.fmean(edge_counts), 2) if edge_counts else 0,
        "p50_ms": round(percentile(50), 2),
        "p95_ms": round(percentile(95), 2),
        "p99_ms": round(percentile(99), 2),
        "max_ms": round(latencies[-1], 2) if latencies else 0,
        "min_ms": round(latencies[0], 2) if latencies else 0,
        "accept_p95_ms": ACCEPT_P95_MS,
        "passed": bool(latencies) and percentile(95) < ACCEPT_P95_MS,
    }
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="邻域查询 P95 压测（阶段 8 验收遗留项）")
    parser.add_argument("--pack", default="", help="只看某个行业包")
    parser.add_argument("--depth", type=int, default=1, choices=[1, 2])
    parser.add_argument("--calls", type=int, default=200)
    parser.add_argument("--since", default="")
    parser.add_argument("--until", default="")
    parser.add_argument("--as-of", default="", help="属性边有效期过滤的查询时点")
    parser.add_argument("--relation-kind", default="", choices=["", "event", "attribute"])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = benchmark(pack_id=args.pack, depth=args.depth, calls=args.calls,
                       since=args.since, until=args.until, as_of=args.as_of,
                       relation_kind=args.relation_kind)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0 if report.get("passed") else 1

    if report.get("error"):
        print("[中止] %s" % report["error"])
        return 1
    print("=" * 88)
    print("邻域查询压测（阶段 8 遗留验收：P95 < %.0f ms）" % ACCEPT_P95_MS)
    print("=" * 88)
    print("  范围=%s | 跳数=%d | 采样节点=%d | 查询次数=%d | 平均边数=%.2f"
          % (report["pack_id"], report["depth"], report["nodes_sampled"],
             report["calls"], report["avg_edges"]))
    print("  P50 = %.2f ms | P95 = %.2f ms | P99 = %.2f ms | min = %.2f | max = %.2f"
          % (report["p50_ms"], report["p95_ms"], report["p99_ms"],
             report["min_ms"], report["max_ms"]))
    print("  结论：%s" % ("✅ 通过（P95 < %.0f ms）" % ACCEPT_P95_MS if report["passed"]
                         else "❌ 未达标（P95 ≥ %.0f ms）" % ACCEPT_P95_MS))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

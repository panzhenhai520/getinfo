#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知识图谱归并任务（阶段 8）：从事件/实体/主题表生成 kg_nodes / kg_edges。

图是**派生视图**（源表为准），所以这个工具可以随时重跑：
  · 归并是幂等 UPSERT（节点键 = 包+类型+node_key；边键 = 主体+动作+客体+article_id）；
  · `--rebuild` 会先清空图再重建（换了归并口径时用）；
  · `--neighborhood 实体名` 直接查邻域，验证"建出来的图真能遍历"。

用法：
    python tools/build_knowledge_graph.py                                  # 只读预演
    python tools/build_knowledge_graph.py --apply
    python tools/build_knowledge_graph.py --apply --pack invest_mgmt
    python tools/build_knowledge_graph.py --apply --rebuild
    python tools/build_knowledge_graph.py --neighborhood 工信部 --depth 2
    python tools/build_knowledge_graph.py --apply --json
退出码：0 = 正常；1 = 事件覆盖不足（先补阶段 7 的抽取，建图会是空图）。
"""
from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, __file__.rsplit("tools", 1)[0])

# 阶段 8 的前置判据：事件覆盖不足就别建图（建出来是空图）
MIN_EVENT_COVERAGE = 0.2


def _event_coverage(db) -> float:
    db._ensure_connection()
    with db.lock:
        cur = db.connection.cursor()
        try:
            cur.execute("SELECT COUNT(*) AS n FROM articles WHERE status='active'")
            active = int(cur.fetchone()["n"] or 0)
            cur.execute(
                "SELECT COUNT(DISTINCT e.article_id) AS n FROM intel_article_events e"
                " JOIN articles a ON a.id=e.article_id"
                " WHERE a.status='active' AND e.subject NOT IN ('__no_event__','__error__')"
            )
            covered = int(cur.fetchone()["n"] or 0)
            return covered / float(active) if active else 0.0
        finally:
            cur.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="知识图谱归并（kg_nodes / kg_edges）")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只读预演）")
    parser.add_argument("--pack", default="", help="只归并某个行业包（默认全部）")
    parser.add_argument("--rebuild", action="store_true", help="先清空图再重建")
    parser.add_argument("--force", action="store_true", help="事件覆盖不足也照建（不建议）")
    parser.add_argument("--neighborhood", default="", help="查该实体/主题的邻域（只读）")
    parser.add_argument("--depth", type=int, default=1, help="邻域跳数（1~2）")
    parser.add_argument("--since", default="", help="邻域时间过滤起点 YYYY-MM-DD")
    parser.add_argument("--until", default="", help="邻域时间过滤终点 YYYY-MM-DD")
    parser.add_argument("--relation-kind", default="", choices=["", "event", "attribute"],
                        help="只看某一类边（event / attribute）")
    parser.add_argument("--as-of", default="",
                        help="查询时点：属性边按有效期过滤（YYYY-MM-DD）")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from intel_database import intel_repository
    from kg_builder import KnowledgeGraphBuilder
    from sqlite_database import sqlite_db

    intel_repository._ensure()
    coverage = _event_coverage(sqlite_db)
    builder = KnowledgeGraphBuilder(repository=intel_repository)

    if args.neighborhood:
        result = builder.neighborhood(
            args.neighborhood, pack_id=args.pack, depth=args.depth,
            since=args.since, until=args.until, limit=args.limit,
            relation_kind=str(args.relation_kind or ""), as_of=str(args.as_of or ""),
        )
        node = result.get("node") or {}
        print("=" * 88)
        print("邻域查询：%s（%s，%d 跳%s%s）"
              % (args.neighborhood, args.pack or "全部包", result["stats"]["depth"],
                 "，只看" + args.relation_kind + "边" if args.relation_kind else "",
                 "，时点 " + args.as_of if args.as_of else ""))
        print("=" * 88)
        print("中心节点：%s [%s] 文章 %s 篇 / 事件 %s 条 / 时间 %s ~ %s"
              % (node.get("label") or "（不在图中）", node.get("node_type") or "-",
                 node.get("article_count"), node.get("event_count"),
                 node.get("first_seen") or "-", node.get("last_seen") or "-"))
        print("\n边（%d 条）：" % len(result["edges"]))
        for edge in result["edges"][:30]:
            arrow = "→" if edge["src_key"] == node.get("node_key") else "←"
            other = edge["dst_key"] if arrow == "→" else edge["src_key"]
            kind = str(edge.get("relation_kind") or "event")
            if kind == "attribute":
                window = "%s~%s" % (edge.get("valid_from") or "-", edge.get("valid_to") or "-")
                print("   [属性] %s --%s--> %s | 有效期 %s | conf=%.2f | %s"
                      % (edge["src_key"][:16], str(edge.get("attr_key") or edge["action"])[:14],
                         str(edge.get("attr_value") or other)[:24], window,
                         edge["confidence"], edge["evidence_ref"]))
            else:
                print("   [事件] %s --%s--> %s | %s | conf=%.2f | %s"
                      % (edge["src_key"][:16], edge["action"][:14], other[:24],
                         edge["event_time"] or "无时间", edge["confidence"], edge["evidence_ref"]))
        print("\n邻居节点 %d 个" % len(result["neighbors"]))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
        return 0

    print("=" * 88)
    print("知识图谱归并（阶段 8）· 范围：%s" % (args.pack or "全部行业包"))
    print("=" * 88)
    print("当前活跃文章事件覆盖率：%.1f%%（阶段 7 验收线 60%%，建图最低线 %.0f%%）"
          % (coverage * 100, MIN_EVENT_COVERAGE * 100))
    if coverage < MIN_EVENT_COVERAGE and not args.force:
        print("\n[中止] 事件覆盖过低，此时归并只会得到空图。先跑：")
        print("    python tools/backfill_article_events.py --apply --until-coverage 0.6")
        print("确实要建空图骨架可加 --force。")
        return 1

    if not args.apply:
        summary = builder.build(pack_id=args.pack, apply=False)
        print("\n[dry-run] 预演结果（未写库）：")
    else:
        if args.rebuild:
            with sqlite_db.lock:
                cur = sqlite_db.connection.cursor()
                try:
                    if args.pack:
                        cur.execute("DELETE FROM kg_edges WHERE industry_pack_id=?", (args.pack,))
                        cur.execute("DELETE FROM kg_nodes WHERE industry_pack_id=?", (args.pack,))
                    else:
                        cur.execute("DELETE FROM kg_edges")
                        cur.execute("DELETE FROM kg_nodes")
                    sqlite_db.connection.commit()
                finally:
                    cur.close()
            print("已按 --rebuild 清空图（范围：%s）" % (args.pack or "全部"))
        summary = builder.build(pack_id=args.pack, apply=True)
        print("\n归并结果：")

    print("  源事件行: %d" % summary["events"])
    print("  源属性行: %d" % summary.get("attributes", 0))
    print("  节点: %d（含主题 %d）" % (summary["nodes"], summary["topics"]))
    print("  边: %d（事件 %d / 属性 %d）"
          % (summary["edges"], summary.get("event_edges", summary["edges"]),
             summary.get("attribute_edges", 0)))
    print("  边覆盖率（事件边 / 事件行）: %s"
          % (("%.1f%%" % (summary["edge_coverage"] * 100)) if summary["edge_coverage"] is not None else "-"))
    print("  未映射到 canonical 的主体: %d 种，客体无法成键: %d 条"
          % (summary["unmapped_subjects"], summary["unmapped_objects"]))
    if summary["unmapped_subject_samples"]:
        print("  未映射样例（先跑实体归并可消除）：%s"
              % "、".join("%s×%d" % (name, count)
                          for name, count in summary["unmapped_subject_samples"][:6]))
    if args.apply:
        print("  写入节点 %s / 写入边 %s" % (summary.get("written_nodes"), summary.get("written_edges")))

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

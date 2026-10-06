#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""结构化产物填充率体检（只读）。

用途：在决定"建知识图谱"路线之前，先量清楚已有结构化产物到底填了多少。
填充率高 → 可以直接由现有表归并出 kg_nodes / kg_edges；
填充率低 → 先补抽取覆盖率再建图，否则建出来是空图。

统计口径：以活跃文章（articles.status='active'）为分母，量每个产物覆盖了多少篇文章；
QA 侧产物（claims/conflicts）以"有最终答案的 run"为分母。

用法：
    python tools/structured_artifact_coverage_audit.py            # 本机
    python tools/structured_artifact_coverage_audit.py --json     # 输出 JSON（便于对比历史）
"""
from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, __file__.rsplit("tools", 1)[0])

# (产物名, 表名, 关联文章的列名候选, 说明)
ARTICLE_ARTIFACTS = (
    ("事件三元组", "intel_article_events", ("article_id",), "主体/动作/客体 + 实体清单 + 事件时间"),
    ("实体归一", "intel_subject_canonical", (), "subject_text → canonical_name/subject_key"),
    ("文章向量", "intel_article_embeddings", ("article_id",), "bge-m3 语义检索"),
    ("主题关联", "intel_topic_articles", ("article_id",), "主题聚类文章关联"),
    ("证据分组-成员", "intel_evidence_group_articles", ("article_id",), "独立来源数/权威等级/冲突状态"),
    ("包归属", "article_intel_classifications", ("article_id",), "行业包分类（兜底归属已保证 100%）"),
    ("内容包关联", "content_industry_packs", ("content_id",), "content_type='article' 的包关联"),
)

QA_ARTIFACTS = (
    ("主张", "qa_claims", "run_id"),
    ("主张-证据边", "qa_claim_evidence", "run_id"),
    ("冲突裁决", "qa_conflicts", "run_id"),
    ("证据解析单元", "qa_attribution_units", "run_id"),
    ("证据解析链路", "qa_attribution_links", "run_id"),
    ("词元影响力", "qa_token_influence", "run_id"),
)


def _fetchone(db, sql, params=()):
    with db.lock:
        row = db.connection.execute(sql, params).fetchone()
    if row is None:
        return None
    return dict(row) if hasattr(row, "keys") else row[0]


def _table_exists(db, table: str) -> bool:
    try:
        _fetchone(db, "SELECT count(*) AS n FROM %s" % table)
        return True
    except Exception:
        return False


def _count(db, table: str) -> int:
    try:
        value = _fetchone(db, "SELECT count(*) AS n FROM %s" % table)
        return int((value or {}).get("n") or 0)
    except Exception:
        return -1


def _distinct_articles(db, table: str, columns) -> int:
    for column in columns:
        try:
            value = _fetchone(
                db, "SELECT count(DISTINCT %s) AS n FROM %s WHERE %s IS NOT NULL"
                % (column, table, column))
            return int((value or {}).get("n") or 0)
        except Exception:
            continue
    return -1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(argv)

    from sqlite_database import sqlite_db

    sqlite_db._ensure_connection()
    active = int((_fetchone(sqlite_db, "SELECT count(*) AS n FROM articles WHERE status='active'") or {}).get("n") or 0)
    all_articles = int((_fetchone(sqlite_db, "SELECT count(*) AS n FROM articles") or {}).get("n") or 0)
    answered_runs = _count(sqlite_db, "qa_runs") if _table_exists(sqlite_db, "qa_runs") else 0
    try:
        answered_runs = int((_fetchone(
            sqlite_db,
            "SELECT count(*) AS n FROM qa_runs WHERE status='completed' "
            "AND final_answer_json NOT IN ('', '{}')") or {}).get("n") or 0)
    except Exception:
        answered_runs = 0

    report = {"active_articles": active, "all_articles": all_articles,
              "completed_qa_runs": answered_runs, "artifacts": [], "qa_artifacts": []}
    print("全部文章: %d 篇（其中活跃 %d 篇；覆盖率以全部文章为分母）" % (all_articles, active))
    print("有最终答案的问答 run: %d 个" % answered_runs)
    print()
    print("=== 文章侧结构化产物填充率 ===")
    print("%-18s %10s %12s %10s  %s" % ("产物", "总行数", "覆盖文章数", "覆盖率", "说明"))
    for label, table, columns, note in ARTICLE_ARTIFACTS:
        if not _table_exists(sqlite_db, table):
            print("%-18s %10s %12s %10s  %s" % (label, "-", "-", "缺表", note))
            report["artifacts"].append({"label": label, "table": table, "rows": None,
                                        "articles": None, "coverage": None, "note": note})
            continue
        rows = _count(sqlite_db, table)
        covered = _distinct_articles(sqlite_db, table, columns) if columns else -1
        coverage = (covered / float(all_articles)) if (all_articles and covered >= 0) else None
        print("%-18s %10d %12s %10s  %s" % (
            label, rows, covered if covered >= 0 else "（无文章外键）",
            ("%.1f%%" % (coverage * 100)) if coverage is not None else "-", note))
        report["artifacts"].append({
            "label": label, "table": table, "rows": rows,
            "articles": covered if covered >= 0 else None,
            "coverage": round(coverage, 4) if coverage is not None else None, "note": note,
        })

    print()
    print("=== QA 侧结构化产物填充率（分母=有最终答案的 run）===")
    for label, table, run_column in QA_ARTIFACTS:
        if not _table_exists(sqlite_db, table):
            print("%-18s %10s %12s %10s" % (label, "-", "-", "缺表"))
            report["qa_artifacts"].append({"label": label, "table": table, "rows": None,
                                           "runs": None, "coverage": None})
            continue
        rows = _count(sqlite_db, table)
        try:
            value = _fetchone(sqlite_db, "SELECT count(DISTINCT %s) AS n FROM %s" % (run_column, table))
            runs = int((value or {}).get("n") or 0)
        except Exception:
            runs = -1
        coverage = (runs / float(answered_runs)) if (answered_runs and runs >= 0) else None
        print("%-18s %10d %12s %10s" % (
            label, rows, runs if runs >= 0 else "-",
            ("%.1f%%" % (coverage * 100)) if coverage is not None else "-"))
        report["qa_artifacts"].append({
            "label": label, "table": table, "rows": rows,
            "runs": runs if runs >= 0 else None,
            "coverage": round(coverage, 4) if coverage is not None else None,
        })

    # 结论：按"能否直接用现有表归并出图"给判据
    events = next((item for item in report["artifacts"] if item["table"] == "intel_article_events"), {})
    subjects = next((item for item in report["artifacts"] if item["table"] == "intel_subject_canonical"), {})
    events_cov = events.get("coverage") or 0.0
    print()
    print("=== 建图路线判据 ===")
    if events_cov >= 0.6:
        route = "直接归并：事件三元组覆盖 %.1f%%，可由 intel_article_events + intel_subject_canonical 直接归并出 kg_nodes/kg_edges。" % (events_cov * 100)
    elif events_cov >= 0.2:
        route = "先补抽取再建图：事件覆盖仅 %.1f%%，建议先提高事件抽取覆盖率（重点补最近 90 天文章）再归并。" % (events_cov * 100)
    else:
        route = "先补抽取：事件覆盖仅 %.1f%%，此时建图会得到空图，应先做事件抽取回填。" % (events_cov * 100)
    print(route)
    report["route_recommendation"] = route
    report["subject_canonical_rows"] = subjects.get("rows")
    if args.json:
        print()
        print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

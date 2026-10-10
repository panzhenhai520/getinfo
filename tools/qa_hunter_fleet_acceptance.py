#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 04（P04-01…P04-06）舰队验收：**单通道 vs 舰队**的同题对比（真跑、不调 LLM/嵌入端点）。

它回答的问题：把检索从"既有 ArticleRetriever 顺序多通道"换成"并行 Hunter 舰队"之后，
命中/可回溯/接地/Recall@K/引用 P·R/耗时/降级次数分别变成什么样。

口径与 Phase 00 冻结的 benchmark 完全一致：题目读 `config/qa_acceptance_questions.json`，
指标复用 `tools/qa_retrieval_acceptance` 的 `summarize` / `_grounded` / `_citation_counts`
（同一套函数、同一套分母规则，绝不另立一套口径）。**只用 CPU 规则与库内既有向量**：
   · 基线侧：`ArticleRetriever.retrieve`（不传 semantic_search，避免任何端点调用）；
   · 舰队侧：`build_default_fleet`（语义 Hunter 只读 intel_article_embeddings）。

语料两种来源：
   · `--snapshot baseline/qa-hunter-corpus-snapshot.json`：A 机**只读**导出的生产快照，
     本地建临时 sqlite 回放（与 Phase 03 的 real-sample 回放同思路）；
   · 不带 `--snapshot`：直接用当前配置的库（测试环境请先设 DATABASE_TYPE=sqlite +
     临时 DATABASE_PATH）。

用法：
    python tools/qa_hunter_fleet_acceptance.py --json --out baseline/qa-hunter-fleet-acceptance.json
    python tools/qa_hunter_fleet_acceptance.py --snapshot baseline/qa-hunter-corpus-snapshot.json --json
    python tools/qa_hunter_fleet_acceptance.py --history data/qa_hunter_history.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.qa_retrieval_acceptance as acceptance  # noqa: E402

REPORT_VERSION = "qa-hunter-fleet-acceptance-v1"
DEFAULT_SNAPSHOT = os.path.join("baseline", "qa-hunter-corpus-snapshot.json")

# 与 tools/qa_retrieval_acceptance.evaluate 完全相同的逐题明细键（让 summarize 能直接吃）
ROW_KEYS = ("id", "question", "kind", "industry_pack_id", "evidence", "hit", "traceable",
            "traceable_ratio", "grounded", "terms", "graph", "graph_attribute", "graph_event",
            "graph_receipt", "time_window", "ms", "expect_terms_count", "expect_terms_hit",
            "cited_total", "cited_matched", "citation_hit")


# ---------------------------------------------------------------------------
# 快照 → 临时 sqlite（回放用；只读快照，不改任何生产数据）
# ---------------------------------------------------------------------------
def build_db_from_snapshot(snapshot: dict, path: str):
    """把 A 机只读快照灌进一个临时 sqlite（表结构与生产一致）。

    这里**强制 backend=sqlite**：回放必须落在临时文件库里，绝不许把快照写进当前配置的
    主库（本机 .env 可能是 postgres，`SQLiteDatabase` 会跟着走 PG）。做法是临时改
    `config.DATABASE_TYPE` 再构造实例；实例一旦建好，`backend` 就是它自己的属性。
    """
    import config
    from intel_database import IntelRepository
    from sqlite_database import SQLiteDatabase

    saved_type = getattr(config, "DATABASE_TYPE", None)
    config.DATABASE_TYPE = "sqlite"
    try:
        db = SQLiteDatabase(path)
        db.connect()
        db.create_tables()
        db.analyze_article_spacetime_profile = lambda _article_id: None
        IntelRepository(db)._ensure()
    finally:
        if saved_type is not None:
            config.DATABASE_TYPE = saved_type
    assert db.backend == "sqlite", "快照回放必须落在临时 sqlite 上"
    assert os.path.abspath(path) == os.path.abspath(db.db_path), "回放库路径不对（拒绝写主库）"

    def _exec(sql, params=()):
        with db.lock:
            cursor = db.connection.cursor()
            try:
                cursor.execute(sql, tuple(params))
                db.connection.commit()
            finally:
                cursor.close()

    for row in snapshot.get("articles") or []:
        _exec("INSERT OR REPLACE INTO articles(id,url,title,content,domain,publish_date,"
              "first_crawled,status,quality_score,content_length,published_at_utc,"
              "published_timezone,published_precision) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (row.get("id"), row.get("url"), row.get("title"), row.get("content") or "",
               row.get("domain"), row.get("publish_date"), row.get("first_crawled"),
               row.get("status") or "active", row.get("quality_score") or 0,
               row.get("content_length") or len(str(row.get("content") or "")),
               row.get("published_at_utc"), row.get("published_timezone"),
               row.get("published_precision")))
    for row in snapshot.get("classifications") or []:
        _exec("INSERT OR REPLACE INTO article_intel_classifications(article_id, industry_pack_id,"
              " activation_id, industry_pack_version, classifier_version, article_content_hash,"
              " rule_category, rule_confidence, score_details_json, matched_keywords_json,"
              " topic_tags_json, final_category, result_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (row.get("article_id"), row.get("industry_pack_id"), row.get("activation_id") or "",
               row.get("industry_pack_version") or "v1", row.get("classifier_version") or "snapshot",
               row.get("article_content_hash") or "snapshot", row.get("rule_category") or "other",
               row.get("rule_confidence") or 0, row.get("score_details_json") or "{}",
               row.get("matched_keywords_json") or "[]", row.get("topic_tags_json") or "[]",
               row.get("final_category") or "other", row.get("result_source") or "rule"))
    for row in snapshot.get("ragflow_documents") or []:
        _exec("INSERT INTO article_ragflow_documents(article_id, kb_id, document_id,"
              " document_name, sync_status, doc_type, issuer, doc_no, article_no, policy_title,"
              " publish_date, effective_date, source_url, authority_level)"
              " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (row.get("article_id"), row.get("kb_id") or "", row.get("document_id") or "",
               row.get("document_name") or "", row.get("sync_status") or "",
               row.get("doc_type") or "", row.get("issuer") or "", row.get("doc_no") or "",
               row.get("article_no") or "", row.get("policy_title") or "",
               row.get("publish_date"), row.get("effective_date"), row.get("source_url"),
               row.get("authority_level") or 1))
    for row in snapshot.get("events") or []:
        _exec("INSERT OR REPLACE INTO intel_article_events(article_id, event_index,"
              " industry_pack_id, subject, action, object, event_time, event_type, subject_type,"
              " state_before, state_after, event_hash, content_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (row.get("article_id"), row.get("event_index") or 0, row.get("industry_pack_id") or "",
               row.get("subject") or "", row.get("action") or "", row.get("object") or "",
               row.get("event_time") or "", row.get("event_type") or "other",
               row.get("subject_type") or "entity", row.get("state_before") or "",
               row.get("state_after") or "", row.get("event_hash") or "", row.get("content_hash") or ""))
    for row in snapshot.get("attributes") or []:
        _exec("INSERT OR REPLACE INTO intel_article_attributes(article_id, attr_index,"
              " industry_pack_id, subject, attribute, value, value_type, valid_from, valid_to,"
              " as_of, evidence_quote, content_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (row.get("article_id"), row.get("attr_index") or 0, row.get("industry_pack_id") or "",
               row.get("subject") or "", row.get("attribute") or "", row.get("value") or "",
               row.get("value_type") or "text", row.get("valid_from") or "",
               row.get("valid_to") or "", row.get("as_of") or "",
               row.get("evidence_quote") or "", row.get("content_hash") or ""))
    for row in snapshot.get("embeddings") or []:
        import base64

        import numpy as np

        blob = base64.b64decode(str(row.get("embedding_b64") or ""))
        dim = int(row.get("embedding_dim") or 0)
        if not dim or len(blob) != dim * 4:
            continue
        array = np.frombuffer(blob, dtype=np.float32)
        _exec("INSERT OR REPLACE INTO intel_article_embeddings(article_id, model_id, embedding_dim,"
              " embedding, content_hash, status) VALUES(?,?,?,?,?,?)",
              (row.get("article_id"), row.get("model_id") or "bge-m3", int(array.size),
               array.tobytes(), "snapshot", row.get("status") or "ready"))
    # 图是**派生视图**：生产库里 kg_edges 是物化好的，回放时用导出的事件/属性按同一个
    # `KnowledgeGraphBuilder` 重建（不另写建图逻辑），否则图通道在回放里恒为空。
    built = {}
    if snapshot.get("events") or snapshot.get("attributes"):
        from intel_database import IntelRepository
        from kg_builder import KnowledgeGraphBuilder

        builder = KnowledgeGraphBuilder(repository=IntelRepository(db))
        for pack in (snapshot.get("packs") or {}):
            try:
                built[str(pack)] = builder.build(pack_id=str(pack), apply=True)
            except Exception as exc:
                built[str(pack)] = {"error": str(exc)[:120]}
    db.snapshot_graph_build = built
    return db


def snapshot_packs(snapshot: dict) -> list:
    return sorted(str(key) for key in (snapshot.get("packs") or {}))


# ---------------------------------------------------------------------------
# 两侧跑同一批题
# ---------------------------------------------------------------------------
def run_side(questions, *, side: str, retriever, fleet, database, limit: int,
             verbose: bool = False) -> tuple:
    """side='baseline' 走既有 retriever；side='fleet' 走舰队。返回 (逐题明细, 汇总统计)。"""
    rows, receipts = [], []
    for index, item in enumerate(questions, 1):
        question = str(item.get("question") or "").strip()
        if not question:
            continue
        pack_id = str(item.get("industry_pack_id") or "")
        plan = {
            "question": question,
            "queries": [question],
            "entities": [str(term) for term in (item.get("expect_terms") or [])],
            "needs_local_articles": True,
        }
        labeled_terms = acceptance._weak_label_terms(item)
        started = time.monotonic()
        error = ""
        try:
            if side == "fleet":
                outcome = fleet.retrieve(plan, industry_pack_id=pack_id, limit=limit)
                receipts.append(dict((outcome.get("stats") or {}).get("hunter_fleet") or {}))
            else:
                outcome = retriever.retrieve(plan, industry_pack_id=pack_id, limit=limit)
        except Exception as exc:
            error = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            outcome = {"evidence": [], "stats": {}, "graph": {}, "time_window": {}}
        elapsed_ms = int((time.monotonic() - started) * 1000)
        evidence = list(outcome.get("evidence") or [])
        cursor = database.connection.cursor()
        try:
            checks = [acceptance._resolve_evidence(cursor, one) for one in evidence]
        finally:
            cursor.close()
        terms = acceptance._terms_of(question, item.get("expect_terms"))
        cited_total, cited_matched, matched_terms = acceptance._citation_counts(evidence, labeled_terms)
        graph_items = [one for one in evidence if str(one.get("source_type")) == "graph"]
        row = {
            "id": item.get("id") or index,
            "question": question,
            "kind": item.get("kind") or "",
            "industry_pack_id": pack_id,
            "evidence": len(evidence),
            "hit": bool(evidence),
            "traceable": bool(checks) and all(one["ok"] for one in checks),
            "traceable_ratio": (sum(1 for one in checks if one["ok"]) / len(checks)) if checks else 0.0,
            "grounded": acceptance._grounded(question, evidence, terms),
            "terms": terms,
            "graph": len(graph_items),
            "graph_attribute": sum(1 for one in graph_items
                                   if (one.get("metadata") or {}).get("relation_kind") == "attribute"),
            "graph_event": sum(1 for one in graph_items
                               if (one.get("metadata") or {}).get("relation_kind") == "event"),
            "graph_receipt": outcome.get("graph") or {},
            "time_window": {key: value for key, value in (outcome.get("time_window") or {}).items()
                            if key in ("has_time", "label", "expanded", "in_window_adopted")},
            "ms": elapsed_ms,
            "expect_terms_count": len(labeled_terms),
            "expect_terms_hit": len(matched_terms),
            "cited_total": cited_total,
            "cited_matched": cited_matched,
            "citation_hit": bool(cited_matched),
        }
        if error:
            row["error"] = error
        rows.append(row)
        if verbose:
            print("  [%-4s] %-4s 证据 %2d 条（图 %d）| 可回溯 %.0f%% | 接地 %s | %5d ms | %s"
                  % (side, "命中" if row["hit"] else "空", row["evidence"], row["graph"],
                     100.0 * row["traceable_ratio"], "是" if row["grounded"] else "否",
                     row["ms"], question[:38]))
    return rows, receipts


def _fleet_metrics(receipts) -> dict:
    """舰队侧的降级/并行统计（这一层是 Phase 04 新增的可观测面）。"""
    if not receipts:
        return {"runs": 0}
    degraded = sum(int(item.get("degraded") or 0) for item in receipts)
    statuses: dict = {}
    semantic_only = 0
    reason_codes: dict = {}
    for item in receipts:
        for hunter_id, info in (item.get("by_hunter") or {}).items():
            status = str(info.get("status") or "")
            statuses[status] = statuses.get(status, 0) + 1
            if info.get("reason_code"):
                key = "%s:%s" % (hunter_id, info["reason_code"])
                reason_codes[key] = reason_codes.get(key, 0) + 1
    return {
        "runs": len(receipts),
        "degraded_total": degraded,
        "degraded_per_run": round(degraded / max(1, len(receipts)), 3),
        "hunter_statuses": statuses,
        "reason_codes": reason_codes,
        "wall_ms_total": sum(int(item.get("wall_ms") or 0) for item in receipts),
        "hunter_ms_sum_total": sum(int(item.get("hunter_ms_sum") or 0) for item in receipts),
        "parallelism_gain_ms_total": sum(int(item.get("parallelism_gain_ms") or 0)
                                         for item in receipts),
        "pool_loads": sum(int((item.get("pool") or {}).get("pool_loads") or 0) for item in receipts),
        "partial_runs": sum(1 for item in receipts if item.get("budget_exhausted")),
    }


def compare(baseline_rows, fleet_rows, baseline_receipts, fleet_receipts, *, limit, meta,
            cost=None) -> dict:
    """两侧汇总对比（指标口径 = `tools/qa_retrieval_acceptance.summarize`，一套不重写）。"""
    if cost is None:
        cost = acceptance._cost_summary()
    base_summary = acceptance.summarize(baseline_rows, limit=limit, benchmark_meta=meta, cost=cost)
    fleet_summary = acceptance.summarize(fleet_rows, limit=limit, benchmark_meta=meta, cost=cost)
    delta = {}
    for key in ("hit_rate", "traceable_rate", "grounded_rate", "recall_at_k",
                "citation_precision", "citation_recall", "avg_ms", "errors"):
        left = base_summary.get(key)
        right = fleet_summary.get(key)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            delta[key] = round(float(right) - float(left), 6)
        else:
            delta[key] = None
    base_evidence = sum(row["evidence"] for row in baseline_rows)
    fleet_evidence = sum(row["evidence"] for row in fleet_rows)
    return {
        "baseline": base_summary,
        "fleet": fleet_summary,
        "delta": delta,
        "evidence_total": {"baseline": base_evidence, "fleet": fleet_evidence,
                           "delta": fleet_evidence - base_evidence},
        "fleet_metrics": _fleet_metrics(fleet_receipts),
        "baseline_metrics": _fleet_metrics(baseline_receipts),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Phase 04 舰队验收（单通道 vs 舰队，真跑不调 LLM）")
    parser.add_argument("--questions", default=acceptance.DEFAULT_QUESTIONS_FILE)
    parser.add_argument("--snapshot", default="", help="A 机只读语料快照（默认看 baseline/ 有没有）")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--out", default="")
    parser.add_argument("--history", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    questions, benchmark_meta = acceptance.load_questions(args.questions)
    temp_dir = None
    snapshot_path = args.snapshot
    if not snapshot_path and os.path.exists(DEFAULT_SNAPSHOT):
        snapshot_path = DEFAULT_SNAPSHOT
    snapshot = None
    if snapshot_path:
        if not os.path.exists(snapshot_path):
            print("快照不存在：%s" % snapshot_path)
            return 2
        with open(snapshot_path, encoding="utf-8") as handle:
            snapshot = json.load(handle)

    if snapshot is not None:
        temp_dir = tempfile.TemporaryDirectory()
        database = build_db_from_snapshot(snapshot, os.path.join(temp_dir.name, "snapshot.sqlite3"))
        packs = snapshot_packs(snapshot)
        questions = [item for item in questions
                     if str(item.get("industry_pack_id") or "") in packs]
        print("语料来源：快照 %s（packs=%s，文章 %d 篇，向量 %d 条）"
              % (snapshot_path, ",".join(packs), len(snapshot.get("articles") or []),
                 len(snapshot.get("embeddings") or [])))
    else:
        from sqlite_database import sqlite_db
        from intel_database import IntelRepository

        database = sqlite_db
        database._ensure_connection()
        IntelRepository(database)._ensure()
        print("语料来源：当前配置的库（backend=%s）" % getattr(database, "backend", "?"))

    import intel_database
    from qa_hunter_fleet import build_default_fleet
    from qa_retrieval import ArticleRetriever

    saved = intel_database.sqlite_db
    intel_database.sqlite_db = database
    try:
        retriever = ArticleRetriever(database)          # 基线：顺序多通道，不接端点语义
        fleet = build_default_fleet(database=database, retriever=retriever)
        print("题目数：%d（benchmark=%s）" % (len(questions), benchmark_meta.get("benchmark_version")))
        print("-" * 78)
        print("[基线] 既有 ArticleRetriever.retrieve（顺序多通道）")
        base_rows, base_receipts = run_side(questions, side="baseline", retriever=retriever,
                                            fleet=fleet, database=database, limit=args.limit,
                                            verbose=bool(args.json is False))
        print("[舰队] HunterFleet（并行风扇 + 超时/重试/回退）")
        fleet_rows, fleet_receipts = run_side(questions, side="fleet", retriever=retriever,
                                              fleet=fleet, database=database, limit=args.limit,
                                              verbose=bool(args.json is False))
    finally:
        intel_database.sqlite_db = saved
        if temp_dir is not None:
            try:
                database.connection.close()
            except Exception:
                pass
            temp_dir.cleanup()

    report = {
        "report_version": REPORT_VERSION,
        "generated_at_utc": acceptance._utc_now_z(),
        "snapshot": snapshot_path or "",
        "benchmark": dict(benchmark_meta or {}),
        "limit": int(args.limit),
        "questions": len(questions),
        "comparison": compare(base_rows, fleet_rows, base_receipts, fleet_receipts,
                              limit=int(args.limit), meta=benchmark_meta,
                              cost=acceptance._cost_summary()),
        "rows": {"baseline": base_rows, "fleet": fleet_rows},
    }
    text = json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
    if args.history:
        with open(args.history, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "generated_at_utc": report["generated_at_utc"],
                "snapshot": report["snapshot"],
                "questions": report["questions"],
                "delta": report["comparison"]["delta"],
                "evidence_total": report["comparison"]["evidence_total"],
                "fleet_metrics": report["comparison"]["fleet_metrics"],
            }, ensure_ascii=False, sort_keys=True) + "\n")
    if args.json:
        print(text)
    else:
        summary = report["comparison"]
        for key in ("hit_rate", "traceable_rate", "grounded_rate", "recall_at_k",
                    "citation_precision", "citation_recall", "avg_ms", "errors"):
            print("  %-20s 基线 %-10s 舰队 %-10s Δ %s"
                  % (key, acceptance._fmt_ratio(summary["baseline"].get(key))
                     if key != "avg_ms" else summary["baseline"].get(key),
                     acceptance._fmt_ratio(summary["fleet"].get(key))
                     if key != "avg_ms" else summary["fleet"].get(key),
                     summary["delta"].get(key)))
        print("  证据条数合计 %s → %s（Δ %s）"
              % (summary["evidence_total"]["baseline"], summary["evidence_total"]["fleet"],
                 summary["evidence_total"]["delta"]))
        print("  舰队降级合计 %s；并行增益 %s ms；候选池加载 %s 次"
              % (summary["fleet_metrics"]["degraded_total"],
                 summary["fleet_metrics"]["parallelism_gain_ms_total"],
                 summary["fleet_metrics"]["pool_loads"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

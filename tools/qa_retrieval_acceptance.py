#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 7/8 使用侧验收：**检索命中率 / 引用可回溯率 / 词面接地率**（不调用 LLM）。

为什么要有这个工具：事件覆盖率是"过程指标"，抽得多不等于答得好。
用户真正关心的是"问一句，能不能给出有出处的答案"。所以验收改成看：

  · 命中率        问题能不能取到证据（evidence 非空）
  · 可回溯率      每条证据能不能点回原文（article:<id> / edge:<key> 都能在库里解析）
  · 词面接地率    取到的证据里是否真的出现了问题里的关键词（防止"答非所问"）
  · 图证据利用率  有多少问题用上了图谱事实（事件边/属性边）与有效期过滤
  · 平均耗时      单题检索耗时（不含 LLM 生成）

纯读库、纯检索，不占用模型槽位，可以和抽取回填并行跑。

用法
    # 1) 先按语料生成一套真实问题（离线，不用 LLM）
    python tools/qa_retrieval_acceptance.py --generate 24 --out config/qa_acceptance_questions.json

    # 2) 跑验收（可指定行业包 / 上限 / 输出 JSON）
    python tools/qa_retrieval_acceptance.py --questions config/qa_acceptance_questions.json
    python tools/qa_retrieval_acceptance.py --questions config/qa_acceptance_questions.json --json

问题文件格式（可手改，也可自己写）：
    [{"id": "q1", "question": "某公司最近有什么动态？", "kind": "event",
      "industry_pack_id": "ai_news", "expect_terms": ["某公司"]}]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlite_database import sqlite_db  # noqa: E402


def _terms_of(question: str, expect_terms=None) -> list:
    """问题里的检索关键词（用于"词面接地"判定）：显式 expect_terms 优先。"""
    terms = [str(item).strip() for item in (expect_terms or []) if str(item).strip()]
    if terms:
        return terms
    try:
        from qa_retrieval import _semantic_anchor_terms, _terms

        terms = list(_semantic_anchor_terms([question])) + list(_terms([question]))
    except Exception:
        terms = []
    cleaned = []
    for item in terms:
        text = str(item).strip()
        if len(text) >= 2 and text not in cleaned:
            cleaned.append(text)
    return cleaned[:6]


def _fetch_ref(cursor, ref: str) -> dict:
    """解析一条证据引用：article:<id> 或 edge:<edge_key>。"""
    text = str(ref or "")
    if text.startswith("article:"):
        try:
            article_id = int(text.split(":", 1)[1])
        except ValueError:
            return {}
        cursor.execute("SELECT id, url, title FROM articles WHERE id=?", (article_id,))
        row = cursor.fetchone()
        return dict(row) if row else {}
    if text.startswith("edge:"):
        edge_key = text.split(":", 1)[1]
        cursor.execute(
            "SELECT edge_key, article_id, relation_kind, src_key, dst_key, valid_from, valid_to"
            " FROM kg_edges WHERE edge_key=?", (edge_key,))
        row = cursor.fetchone()
        if not row:
            return {}
        edge = dict(row)
        if edge.get("article_id"):
            cursor.execute("SELECT id, url, title FROM articles WHERE id=?",
                           (int(edge["article_id"]),))
            article = cursor.fetchone()
            edge["article"] = dict(article) if article else {}
        return edge
    return {}


def _resolve_evidence(cursor, item: dict) -> dict:
    """判定一条证据可不可回溯（要点：必须有可打开的原文链接）。"""
    ref = str(item.get("evidence_ref") or "")
    resolved = _fetch_ref(cursor, ref)
    if resolved:
        url = str(resolved.get("url") or "")
        if not url and isinstance(resolved.get("article"), dict):
            url = str(resolved["article"].get("url") or "")
        return {"ref": ref, "ok": bool(url), "url": url, "kind": ref.split(":", 1)[0]}
    # 没有可解析的库内引用时，退一步看是否自带 URL（联网/官方来源）
    url = str(item.get("source_url") or "")
    return {"ref": ref, "ok": bool(url), "url": url, "kind": "external"}


def _grounded(question: str, evidence: list, terms: list) -> bool:
    """词面接地：证据文本里是否出现问题的关键词（至少命中一个）。"""
    if not terms:
        return bool(evidence)
    for item in evidence:
        haystack = " ".join([
            str(item.get("title") or ""),
            str(item.get("content_excerpt") or ""),
            str(item.get("source_url") or ""),
        ])
        if any(term in haystack for term in terms):
            return True
    return False


def evaluate(questions: list, *, limit: int = 12, verbose: bool = True) -> dict:
    """逐题跑真实检索，统计命中 / 可回溯 / 接地 / 图证据 / 耗时。"""
    from qa_retrieval import ArticleRetriever

    retriever = ArticleRetriever(sqlite_db)
    sqlite_db._ensure_connection()
    results = []
    for index, item in enumerate(questions, 1):
        question = str(item.get("question") or "").strip()
        if not question:
            continue
        pack_id = str(item.get("industry_pack_id") or "")
        plan = {
            "question": question,
            "queries": [question],
            "entities": [str(t) for t in (item.get("expect_terms") or [])],
            "needs_local_articles": True,
        }
        started = time.monotonic()
        try:
            outcome = retriever.retrieve(plan, industry_pack_id=pack_id, limit=limit)
        except Exception as exc:
            results.append({"id": item.get("id") or index, "question": question,
                            "kind": item.get("kind") or "", "error": str(exc)[:160],
                            "evidence": 0, "hit": False, "traceable": False,
                            "grounded": False, "graph": 0, "ms": 0})
            continue
        elapsed_ms = int((time.monotonic() - started) * 1000)
        evidence = list(outcome.get("evidence") or [])
        cursor = sqlite_db.connection.cursor()
        try:
            checks = [_resolve_evidence(cursor, one) for one in evidence]
        finally:
            cursor.close()
        traceable = bool(checks) and all(one["ok"] for one in checks)
        terms = _terms_of(question, item.get("expect_terms"))
        graph_items = [one for one in evidence if str(one.get("source_type")) == "graph"]
        row = {
            "id": item.get("id") or index,
            "question": question,
            "kind": item.get("kind") or "",
            "industry_pack_id": pack_id,
            "evidence": len(evidence),
            "hit": bool(evidence),
            "traceable": traceable,
            "traceable_ratio": (sum(1 for one in checks if one["ok"]) / len(checks)) if checks else 0.0,
            "grounded": _grounded(question, evidence, terms),
            "terms": terms,
            "graph": len(graph_items),
            "graph_attribute": sum(1 for one in graph_items
                                   if (one.get("metadata") or {}).get("relation_kind") == "attribute"),
            "graph_event": sum(1 for one in graph_items
                               if (one.get("metadata") or {}).get("relation_kind") == "event"),
            "graph_receipt": outcome.get("graph") or {},
            "time_window": {k: v for k, v in (outcome.get("time_window") or {}).items()
                            if k in ("has_time", "label", "expanded", "in_window_adopted")},
            "ms": elapsed_ms,
        }
        results.append(row)
        if verbose:
            flag = "命中" if row["hit"] else "空"
            print("  [%-4s] %-4s 证据 %2d 条（图 %d）| 可回溯 %.0f%% | 接地 %s | %5d ms | %s"
                  % (row["id"], flag, row["evidence"], row["graph"],
                     100.0 * row["traceable_ratio"], "是" if row["grounded"] else "否",
                     row["ms"], question[:40]))

    total = len(results)
    summary = {
        "questions": total,
        "hit_rate": round(sum(1 for r in results if r["hit"]) / total, 4) if total else 0.0,
        "traceable_rate": round(sum(1 for r in results if r["traceable"]) / total, 4) if total else 0.0,
        "grounded_rate": round(sum(1 for r in results if r["grounded"]) / total, 4) if total else 0.0,
        "avg_evidence": round(sum(r["evidence"] for r in results) / total, 2) if total else 0.0,
        "graph_rate": round(sum(1 for r in results if r["graph"]) / total, 4) if total else 0.0,
        "errors": sum(1 for r in results if r.get("error")),
        "avg_ms": int(sum(r["ms"] for r in results) / total) if total else 0,
        "by_kind": {},
    }
    kinds = {}
    for row in results:
        bucket = kinds.setdefault(row["kind"] or "other",
                                  {"questions": 0, "hit": 0, "traceable": 0, "grounded": 0, "graph": 0})
        bucket["questions"] += 1
        bucket["hit"] += 1 if row["hit"] else 0
        bucket["traceable"] += 1 if row["traceable"] else 0
        bucket["grounded"] += 1 if row["grounded"] else 0
        bucket["graph"] += 1 if row["graph"] else 0
    summary["by_kind"] = kinds
    return {"summary": summary, "results": results}


def generate_questions(count: int = 24, *, limit_packs: int = 6) -> list:
    """按语料现况生成问题（不调用 LLM）：事件问法 / 属性问法 / 时间窗问法三类。"""
    sqlite_db._ensure_connection()
    questions = []
    with sqlite_db.lock:
        cursor = sqlite_db.connection.cursor()
        try:
            # 1) 有事件的主体 → "最近有什么动态"（考事件证据）
            cursor.execute(
                """
                SELECT subject, industry_pack_id, COUNT(DISTINCT article_id) AS articles
                FROM intel_article_events
                WHERE subject NOT IN ('__no_event__', '__error__') AND subject != ''
                GROUP BY subject, industry_pack_id
                HAVING COUNT(DISTINCT article_id) >= 2
                ORDER BY articles DESC
                LIMIT 12
                """)
            subjects = [dict(row) for row in cursor.fetchall()]
            # 2) 有属性的主体 → "某属性是什么"（考属性边与有效期）
            cursor.execute(
                """
                SELECT subject, attribute, industry_pack_id, COUNT(*) AS rows_count
                FROM intel_article_attributes
                WHERE subject != '' AND attribute != ''
                GROUP BY subject, attribute, industry_pack_id
                ORDER BY rows_count DESC, subject
                LIMIT 12
                """)
            attributes = [dict(row) for row in cursor.fetchall()]
            # 3) 近期热点词 → 时间窗问法（考时间硬过滤与扩窗）
            cursor.execute(
                """
                SELECT matched_keywords_json, industry_pack_id FROM article_intel_classifications
                WHERE COALESCE(matched_keywords_json, '[]') NOT IN ('', '[]')
                ORDER BY id DESC LIMIT 60
                """)
            keyword_rows = [dict(row) for row in cursor.fetchall()]
        finally:
            cursor.close()

    pack_seen = {}
    for row in subjects:
        pack = str(row.get("industry_pack_id") or "")
        if pack_seen.get(pack, 0) >= 2 or len(pack_seen) > limit_packs:
            continue
        pack_seen[pack] = pack_seen.get(pack, 0) + 1
        questions.append({
            "id": "e%d" % (len(questions) + 1),
            "question": "%s最近有什么动态？" % str(row["subject"])[:24],
            "kind": "event",
            "industry_pack_id": pack,
            "expect_terms": [str(row["subject"])[:24]],
        })
    for row in attributes:
        pack = str(row.get("industry_pack_id") or "")
        questions.append({
            "id": "a%d" % (len(questions) + 1),
            "question": "%s的%s是什么？" % (str(row["subject"])[:20], str(row["attribute"])[:12]),
            "kind": "attribute",
            "industry_pack_id": pack,
            "expect_terms": [str(row["subject"])[:20]],
        })
    hot_words = []
    for row in keyword_rows:
        try:
            words = json.loads(row.get("matched_keywords_json") or "[]")
        except Exception:
            continue
        for word in words:
            text = str(word).strip()
            if len(text) >= 2 and text not in hot_words:
                hot_words.append((text, str(row.get("industry_pack_id") or "")))
    for word, pack in hot_words[:6]:
        questions.append({
            "id": "t%d" % (len(questions) + 1),
            "question": "%s最近 90 天有什么新动态？" % word[:16],
            "kind": "time_window",
            "industry_pack_id": pack,
            "expect_terms": [word[:16]],
        })
    return questions[: max(1, int(count))]


def _corpus_snapshot() -> dict:
    """留档用的语料/图谱快照：覆盖率双口径 + 图规模（每次验收都记一份，便于看趋势）。"""
    snapshot = {}
    try:
        from intel_database import intel_repository

        intel_repository._ensure()
        snapshot["coverage"] = intel_repository.coverage_report()
        with sqlite_db.lock:
            cursor = sqlite_db.connection.cursor()
            try:
                for label, sql in (
                    ("nodes", "SELECT COUNT(*) AS n FROM kg_nodes"),
                    ("edges", "SELECT COUNT(*) AS n FROM kg_edges"),
                    ("event_edges",
                     "SELECT COUNT(*) AS n FROM kg_edges WHERE relation_kind='event'"),
                    ("attribute_edges",
                     "SELECT COUNT(*) AS n FROM kg_edges WHERE relation_kind='attribute'"),
                    ("attributes",
                     "SELECT COUNT(*) AS n FROM intel_article_attributes"),
                    ("attributes_with_validity",
                     "SELECT COUNT(*) AS n FROM intel_article_attributes"
                     " WHERE COALESCE(valid_from,'')<>'' OR COALESCE(valid_to,'')<>''"),
                    ("event_rows",
                     "SELECT COUNT(*) AS n FROM intel_article_events"
                     " WHERE subject NOT IN ('__no_event__','__error__')"),
                    ("placeholders",
                     "SELECT COUNT(*) AS n FROM intel_article_events WHERE subject='__no_event__'"),
                ):
                    try:
                        cursor.execute(sql)
                        row = cursor.fetchone()
                        snapshot[label] = int((row["n"] if hasattr(row, "keys") else row[0]) or 0)
                    except Exception:
                        snapshot[label] = None
            finally:
                cursor.close()
    except Exception as exc:
        snapshot["error"] = str(exc)[:160]
    return snapshot


def _append_history(path: str, outcome: dict) -> None:
    """把一次验收结果追加到 JSONL（一行一次，便于按天对比与出趋势报告）。"""
    import datetime as _dt

    record = {
        "recorded_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": outcome.get("summary") or {},
        "corpus": _corpus_snapshot(),
    }
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="阶段 7/8 使用侧检索验收（不调用 LLM）")
    parser.add_argument("--questions", default="", help="问题 JSON 文件（默认用内置生成）")
    parser.add_argument("--generate", type=int, default=0,
                        help="按语料生成 N 个问题并写文件（配合 --out）")
    parser.add_argument("--out", default="", help="生成的问题写到哪（默认打印）")
    parser.add_argument("--limit", type=int, default=12, help="每题最多取多少条证据")
    parser.add_argument("--pack", default="", help="只看某个行业包的问题")
    parser.add_argument("--save-history", default="",
                        help="把本次结果追加到 JSONL（含覆盖率与图规模快照）")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = parser.parse_args(argv)

    if args.generate:
        questions = generate_questions(args.generate)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                json.dump(questions, handle, ensure_ascii=False, indent=1)
            print("已生成 %d 个问题 → %s" % (len(questions), args.out))
        else:
            print(json.dumps(questions, ensure_ascii=False, indent=1))
        return 0

    if args.questions:
        with open(args.questions, "r", encoding="utf-8") as handle:
            questions = json.load(handle)
    else:
        questions = generate_questions(24)
    if args.pack:
        questions = [q for q in questions if str(q.get("industry_pack_id") or "") == args.pack]

    print("=" * 96)
    print("检索验收（阶段 7/8 使用侧）：%d 个问题" % len(questions))
    print("=" * 96)
    outcome = evaluate(questions, limit=args.limit)
    summary = outcome["summary"]
    print("\n" + "=" * 96)
    print("汇总")
    print("=" * 96)
    print("  命中率（取到证据）      %.1f%%" % (100.0 * summary["hit_rate"]))
    print("  可回溯率（能点回原文）  %.1f%%" % (100.0 * summary["traceable_rate"]))
    print("  词面接地率（答对题）    %.1f%%" % (100.0 * summary["grounded_rate"]))
    print("  图证据利用率            %.1f%%" % (100.0 * summary["graph_rate"]))
    print("  平均证据条数            %.2f" % summary["avg_evidence"])
    print("  平均检索耗时            %d ms" % summary["avg_ms"])
    print("  报错题数                %d" % summary["errors"])
    print("\n  分类：")
    for kind, bucket in sorted(summary["by_kind"].items()):
        print("    %-12s 题 %2d | 命中 %2d | 可回溯 %2d | 接地 %2d | 用图 %2d"
              % (kind, bucket["questions"], bucket["hit"], bucket["traceable"],
                 bucket["grounded"], bucket["graph"]))
    if args.save_history:
        _append_history(args.save_history, outcome)
        print("\n  已留档 → %s" % args.save_history)
    if args.json:
        print("\n" + json.dumps(outcome, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

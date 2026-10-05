#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补全缺失的行业包归属（article_intel_classifications）。

背景：生产上 6166 篇活跃文章里 2754 篇没有分类记录、1952 篇完全没有包归属。
但页面上仍然看得到它们 —— 因为「最近关注」走的是
intel_repository.list_dashboard_recent_articles()，它**只按项目关键词快照筛、不看包归属**；
而问答检索 ArticleRetriever._rows() 要求 article_intel_classifications 有本包记录。
两条链路口径不一致 → 同一篇文章"页面看得到、AI 搜不到"。

本工具把缺失的归属补齐，让两条链路一致。来源优先级：

  1. 文章由候选路径入库（source_task_id = intel_candidate_<id>）：
     用该候选已准入的行业包（intel_candidate_industries）→ 逐包入队 classification。
  2. 候选没有准入包（正是它当初没被分类的原因）：退化为按行业包关键词表打分，
     命中最高且不低于门槛的包作为归属，同样入队 classification 落库。
  3. 两条都拿不到：记为 unattributed 并打印，留待人工决定（不硬塞）。

用法：
    python tools/backfill_article_pack_attribution.py --dry-run     # 只统计不写库（默认）
    python tools/backfill_article_pack_attribution.py --apply       # 入队补全
"""
from __future__ import annotations

import argparse
import collections
import re
import sys

sys.path.insert(0, __file__.rsplit("tools", 1)[0])

_CANDIDATE_RE = re.compile(r"intel_candidate_(\d+)")


def _unattributed_articles(db):
    """既无 article_intel_classifications 也无 content_industry_packs 的活跃文章。"""
    db._ensure_connection()
    with db.lock:
        rows = db.connection.execute(
            """
            SELECT a.id, a.title, a.matched_keywords, a.source_task_id, a.domain, a.content
              FROM articles a
             WHERE a.status='active'
               AND NOT EXISTS (SELECT 1 FROM article_intel_classifications c WHERE c.article_id=a.id)
               AND NOT EXISTS (SELECT 1 FROM content_industry_packs p
                                WHERE p.content_type='article' AND p.content_id=CAST(a.id AS TEXT))
             ORDER BY a.id DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def _candidate_packs(db, source_task_id: str) -> list:
    """候选路径入库的文章：取它当初准入的行业包。"""
    m = _CANDIDATE_RE.search(str(source_task_id or ""))
    if not m:
        return []
    try:
        from intel_candidates import IntelCandidateRepository

        repo = IntelCandidateRepository(db)
        return list(repo.get_candidate_industry_pack_ids(int(m.group(1))) or [])
    except Exception:
        return []


def _keyword_packs(article: dict) -> list:
    """候选没有准入包时，按各行业包的关键词表打分挑（返回 [(pack_id, 命中数)])。"""
    try:
        from industry_packs import industry_pack_loader
    except Exception:
        return []
    blob = " ".join(str(article.get(k) or "") for k in ("title", "matched_keywords"))
    blob = blob + " " + str(article.get("content") or "")[:2000]
    blob = blob.casefold()
    if not blob.strip():
        return []
    hits = []
    for pack in industry_pack_loader.list():
        pack_id = str(pack.get("id") or "")
        words = [str(w) for w in (pack.get("core_keywords") or [])]
        words += [str(w) for w in (pack.get("expanded_keywords") or [])]
        count = sum(1 for w in words if w and w.casefold() in blob)
        if count:
            hits.append((pack_id, count))
    hits.sort(key=lambda item: item[1], reverse=True)
    return hits


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真正入队补全（默认只统计）")
    parser.add_argument("--min-keyword-hits", type=int, default=2,
                        help="关键词兜底路径的最低命中数（默认 2）")
    parser.add_argument("--limit", type=int, default=0, help="只处理最新的 N 篇（0=全部）")
    args = parser.parse_args(argv)

    from sqlite_database import sqlite_db

    rows = _unattributed_articles(sqlite_db)
    if args.limit:
        rows = rows[: args.limit]
    print("无任何包归属的活跃文章: %d 篇" % len(rows))
    if not rows:
        return 0

    via_candidate, via_keyword, unresolved = [], [], []
    for art in rows:
        packs = _candidate_packs(sqlite_db, art.get("source_task_id"))
        if packs:
            via_candidate.append((art, packs))
            continue
        kw = _keyword_packs(art)
        strong = [p for p, n in kw if n >= args.min_keyword_hits]
        if strong:
            via_keyword.append((art, strong))
        else:
            unresolved.append((art, kw[:3]))

    print("  ① 候选已准入包 → 可补: %d 篇" % len(via_candidate))
    print("  ② 关键词兜底命中 → 可补: %d 篇" % len(via_keyword))
    print("  ③ 两条都拿不到   → 待人工: %d 篇" % len(unresolved))

    dist = collections.Counter()
    for _art, packs in via_candidate + via_keyword:
        for p in packs:
            dist[p] += 1
    if dist:
        print("\n  归属分布（前 12）:")
        for pack_id, n in dist.most_common(12):
            print("     %-28s %d 篇" % (pack_id, n))

    if unresolved:
        print("\n  待人工样例（前 5）:")
        for art, kw in unresolved[:5]:
            print("     id=%s %s | 弱命中=%s" % (art["id"], str(art.get("title"))[:40], kw))

    if not args.apply:
        print("\n[dry-run] 未写库。确认分布后加 --apply 执行。")
        return 0

    from intel_database import IntelRepository

    repo = IntelRepository(sqlite_db)
    queued, failed = 0, 0
    for art, packs in via_candidate + via_keyword:
        for pack_id in packs:
            try:
                repo.enqueue_classification(int(art["id"]), str(pack_id), ragflow_upload=False)
                queued += 1
            except Exception as exc:
                failed += 1
                print("    入队失败 id=%s pack=%s: %s" % (art["id"], pack_id, str(exc)[:80]))
    print("\n已入队 classification 任务: %d 个（失败 %d）" % (queued, failed))
    print("分类由既有 worker 执行；跑完后再次运行本工具应看到剩余数下降。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

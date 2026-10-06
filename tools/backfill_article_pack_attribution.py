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


def _purge_generic(db, *, apply: bool, min_hits: int) -> int:
    """删除（软删）无任何关键词命中的通用条目。

    判据：活跃 + 无任何包归属 + 对所有行业包的核心/扩展关键词命中数都为 0。
    这类文章的标题长这样：「普通公司新闻」「行业观察」「银行理财产品收益更新」
    —— 是占位/测试性质的条目，既不属任何行业，也不该出现在检索池里。

    安全措施：先把待删记录导出成 JSON 备份（id/标题/url/域名/长度/命中词），
    再做软删除（status='deleted'），随时可回滚；不直接 DELETE。
    """
    import json
    import os
    import time

    rows = _unattributed_articles(db)
    victims = []
    for art in rows:
        if _keyword_packs(art):
            continue
        victims.append(art)
    print("无归属且零关键词命中的通用条目: %d 篇" % len(victims))
    for art in victims[:10]:
        print("   id=%s [%s] %s" % (art["id"], art.get("domain"), str(art.get("title"))[:40]))
    if not victims:
        return 0
    if not apply:
        print("\n[dry-run] 未删除。确认清单后加 --apply 执行（会先导出备份）。")
        return len(victims)

    backup = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                          "industry_pack_backups",
                          "purged_generic_articles_%s.json" % time.strftime("%Y%m%d%H%M%S"))
    os.makedirs(os.path.dirname(backup), exist_ok=True)
    with open(backup, "w", encoding="utf-8") as fh:
        json.dump([{k: str(v)[:500] for k, v in art.items()} for art in victims],
                  fh, ensure_ascii=False, indent=1)
    print("已导出备份: %s" % os.path.abspath(backup))

    ids = [int(art["id"]) for art in victims]
    with db.lock:
        cur = db.connection.cursor()
        for chunk_start in range(0, len(ids), 200):
            chunk = ids[chunk_start:chunk_start + 200]
            marks = ",".join("?" for _ in chunk)
            cur.execute("UPDATE articles SET status='deleted' WHERE id IN (%s)" % marks, tuple(chunk))
        db.connection.commit()
        cur.close()
    print("已软删除 %d 篇（status='deleted'，可用备份文件回滚）" % len(ids))
    return len(ids)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真正补全（默认只统计）")
    parser.add_argument("--also-enqueue", action="store_true",
                        help="除同步写兜底归属外，再入队 classification 让真实分类覆盖它")
    parser.add_argument("--purge-generic", action="store_true",
                        help="删除无归属且零关键词命中的通用条目（先导出备份再软删）")
    parser.add_argument("--min-keyword-hits", type=int, default=2,
                        help="关键词兜底路径的最低命中数（默认 2）")
    parser.add_argument("--limit", type=int, default=0, help="只处理最新的 N 篇（0=全部）")
    args = parser.parse_args(argv)

    from sqlite_database import sqlite_db

    if args.purge_generic:
        _purge_generic(sqlite_db, apply=args.apply, min_hits=args.min_keyword_hits)
        return 0

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

    # 同步写兜底归属（pipeline 收口里同一套逻辑 intel_attribution.ensure_pack_attribution）：
    # 不依赖分类队列——队列可能积压几千条，而"页面看得到、AI 搜不到"是当下就要修的。
    # 兜底行落在该包「其他」分类，并带真实行业打分证据（能通过检索质量门）；
    # 之后 worker 的真实分类会把这一行升级成 trend/event。
    from intel_attribution import ensure_pack_attribution

    written, skipped = 0, 0
    for art, packs in via_candidate + via_keyword:
        picked = [p for p, _n in packs] if packs and isinstance(packs[0], tuple) else list(packs)
        outcome = ensure_pack_attribution(
            sqlite_db, int(art["id"]), art, pack_ids=picked
        )
        if outcome.get("attributed"):
            written += 1
        else:
            skipped += 1
    # 弱命中/无命中的也要有归属（要求：至少归到该行业包的「其他」分类），
    # 交给 ensure_pack_attribution 自己的兜底顺序：关键词命中 → 当前激活包。
    for art, _kw in unresolved:
        outcome = ensure_pack_attribution(sqlite_db, int(art["id"]), art)
        if outcome.get("attributed"):
            written += 1
        else:
            skipped += 1
    print("\n已同步写入兜底归属: %d 篇（未写成 %d 篇）" % (written, skipped))

    if args.also_enqueue:
        from intel_database import IntelRepository

        repo = IntelRepository(sqlite_db)
        queued, failed = 0, 0
        for art, packs in via_candidate + via_keyword:
            picked = [p for p, _n in packs] if packs and isinstance(packs[0], tuple) else list(packs)
            for pack_id in picked:
                try:
                    repo.enqueue_classification(int(art["id"]), str(pack_id), ragflow_upload=False)
                    queued += 1
                except Exception as exc:
                    failed += 1
                    print("    入队失败 id=%s pack=%s: %s" % (art["id"], pack_id, str(exc)[:80]))
        print("已额外入队 classification 任务: %d 个（失败 %d）" % (queued, failed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

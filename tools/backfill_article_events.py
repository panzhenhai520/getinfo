#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""事件抽取回填（阶段 7：知识图谱的必经前置）。

为什么需要单独的回填工具（实测依据）：
  · A 机 7022 篇里 `intel_article_events` 只有 **3 行（0.0%）**、实体归一 **0 行**
    —— 此时建图 = 建空图；
  · 现有自动链路是 `intel_worker` 的周期作业 `event_extract`：**每 720 分钟最多 20 篇**
    （`INTEL_EVENT_EXTRACT_ENABLED` 默认还是关的）→ 一天 40 篇，队列永远排不空，
    等价于这项能力没生效。
所以这里做"取一批 → 并发抽 → 立即落库"的批量回填：断点续跑靠
`list_articles_missing_events` 的 NOT EXISTS 语义（已抽取且 content_hash 未失效的不再返回），
中断后重跑不会重复消耗 LLM。

用法：
    python tools/backfill_article_events.py                       # 只读审计（默认）
    python tools/backfill_article_events.py --apply --max-articles 200
    python tools/backfill_article_events.py --apply --until-coverage 0.6 --workers 4
    python tools/backfill_article_events.py --apply --pack invest_mgmt --batches 50
    python tools/backfill_article_events.py --normalize-subjects  # 抽完顺带做实体归一
退出码：0 = 正常结束；1 = 前置不满足（LLM 未启用 / 连续失败）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, __file__.rsplit("tools", 1)[0])


def _coverage(db) -> dict:
    db._ensure_connection()
    with db.lock:
        cur = db.connection.cursor()
        try:
            cur.execute(
                "SELECT COUNT(*) AS n,"
                " SUM(CASE WHEN COALESCE(published_at_utc,'')<>'' THEN 1 ELSE 0 END) AS dated"
                " FROM articles WHERE status='active'"
            )
            active = int(cur.fetchone()["n"] or 0)
            cur.execute(
                "SELECT COUNT(DISTINCT e.article_id) AS n FROM intel_article_events e"
                " JOIN articles a ON a.id=e.article_id WHERE a.status='active'"
            )
            covered = int(cur.fetchone()["n"] or 0)
            cur.execute(
                "SELECT COUNT(*) AS n FROM intel_article_events WHERE subject='__no_event__'"
            )
            placeholders = int(cur.fetchone()["n"] or 0)
            cur.execute("SELECT COUNT(*) AS n FROM intel_subject_canonical")
            canonicals = int(cur.fetchone()["n"] or 0)
            cur.execute(
                "SELECT COUNT(*) AS n FROM intel_article_events"
                " WHERE COALESCE(state_before,'')<>'' OR COALESCE(state_after,'')<>''"
            )
            with_state = int(cur.fetchone()["n"] or 0)
            return {"active": active, "covered": covered, "placeholders": placeholders,
                    "canonicals": canonicals, "rows_with_state": with_state}
        finally:
            cur.close()


def _eligible(db, pack_id: str) -> int:
    """还有多少篇"够格"的活跃文章没抽（与 list_articles_missing_events 同一口径）。"""
    db._ensure_connection()
    params = []
    pack_clause = ""
    if pack_id:
        pack_clause = "AND c.industry_pack_id=?"
        params.append(pack_id)
    sql = (
        "SELECT COUNT(*) AS n FROM articles a"
        " JOIN article_intel_classifications c ON c.article_id=a.id"
        " WHERE a.status='active' AND COALESCE(a.content,'')!=''"
        "   AND c.final_category IN ('trend','event')"
        f"   {pack_clause}"
        "   AND json_array_length(COALESCE(json_extract(c.score_details_json,"
        " '$.hits.anchor'), '[]')) > 0"
        "   AND NOT EXISTS (SELECT 1 FROM intel_article_events e"
        "                   WHERE e.article_id=a.id"
        "                     AND e.content_hash = COALESCE(a.content_hash,''))"
    )
    with db.lock:
        cur = db.connection.cursor()
        try:
            cur.execute(sql, tuple(params))
            return int(cur.fetchone()["n"] or 0)
        finally:
            cur.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="事件抽取回填（断点续跑）")
    parser.add_argument("--apply", action="store_true", help="真正抽取（默认只读审计）")
    parser.add_argument("--pack", default="", help="只抽某个行业包（默认全部）")
    parser.add_argument("--batch-size", type=int, default=16, help="每批取多少篇（默认 16）")
    parser.add_argument("--batches", type=int, default=0, help="最多跑多少批（0=不限）")
    parser.add_argument("--max-articles", type=int, default=0, help="本次最多抽多少篇（0=不限）")
    parser.add_argument("--until-coverage", type=float, default=0.0,
                        help="达到该活跃文章覆盖率就停（0=不设目标）")
    parser.add_argument("--workers", type=int, default=2,
                        help="并发线程数（受 INTEL_LLM_MAX_CONCURRENCY 上限约束，默认 2）")
    parser.add_argument("--max-consecutive-failures", type=int, default=3,
                        help="连续失败这么多批就停（默认 3）")
    parser.add_argument("--normalize-subjects", action="store_true",
                        help="抽完后跑实体归一（写 intel_subject_canonical）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args(argv)

    import config
    from event_extract_service import EventExtractService
    from intel_database import intel_repository
    from sqlite_database import sqlite_db

    db = sqlite_db
    # 先跑一次 intel 侧的建表/补列迁移（state_before/state_after 等新列）
    try:
        intel_repository._ensure()
    except Exception as exc:
        print("（intel 侧迁移提示：%s）" % str(exc)[:120])
    state = _coverage(db)
    eligible = _eligible(db, str(args.pack or ""))
    summary = {
        "pack": args.pack or "*",
        "active_articles": state["active"],
        "covered_before": state["covered"],
        "coverage_before": round(state["covered"] / max(1, state["active"]), 4),
        "eligible_missing": eligible,
        "applied": bool(args.apply),
    }

    print("=" * 88)
    print("事件抽取回填（阶段 7）")
    print("=" * 88)
    print("活跃文章 %d 篇；已覆盖 %d 篇（%.1f%%）；占位行 %d；实体归一 %d 行；带状态字段的事件 %d 行"
          % (state["active"], state["covered"],
             100.0 * state["covered"] / max(1, state["active"]),
             state["placeholders"], state["canonicals"], state["rows_with_state"]))
    print("待抽取（活跃 / 有包归属 / trend|event / 命中锚点 / 未抽过）: %d 篇" % eligible)

    if not args.apply:
        print("\n[dry-run] 未调用 LLM。确认后加 --apply（断点续跑，可随时中断重跑）。")
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=1))
        return 0

    if not getattr(config, "INTEL_LLM_ENABLED", False):
        print("\n[中止] INTEL_LLM_ENABLED 未开启——事件抽取依赖本地 LLM，先开启再回填。")
        return 1
    if not getattr(config, "INTEL_EVENT_EXTRACT_ENABLED", False):
        print("\n[提示] INTEL_EVENT_EXTRACT_ENABLED 未开启（周期作业不会自己跑），"
              "本次回填不受影响，但抽完后新文章不会自动补事件。")
    if not eligible:
        print("\n没有待抽取文章，结束。")
        return 0

    service = EventExtractService(repository=intel_repository)
    workers = max(1, min(int(args.workers), int(getattr(config, "INTEL_LLM_MAX_CONCURRENCY", 2))))
    batch_size = max(1, int(args.batch_size))
    target = int(args.until_coverage * state["active"]) if args.until_coverage else 0

    processed = succeeded = failed = events_total = 0
    batches = 0
    consecutive_failed_batches = 0
    started = time.monotonic()
    failures = []

    while True:
        if args.batches and batches >= args.batches:
            print("\n达到 --batches 上限，停止。")
            break
        if args.max_articles and processed >= args.max_articles:
            print("\n达到 --max-articles 上限，停止。")
            break
        remaining_budget = 0
        if args.max_articles:
            remaining_budget = max(1, args.max_articles - processed)
        fetch = min(batch_size, remaining_budget) if remaining_budget else batch_size
        batch = intel_repository.list_articles_missing_events(
            pack_id=str(args.pack or ""), limit=fetch
        )
        if not batch:
            print("\n没有更多待抽取文章，停止。")
            break
        batches += 1
        batch_ok = batch_failed = 0
        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(
                    lambda item: service.extract_one(item, pack_id=str(args.pack or "")),
                    batch,
                ))
        else:
            results = [service.extract_one(item, pack_id=str(args.pack or "")) for item in batch]
        for item, (status, count) in zip(batch, results):
            processed += 1
            if status == "ok":
                batch_ok += 1
                succeeded += 1
                events_total += count
            else:
                batch_failed += 1
                failed += 1
                failures.append({"article_id": item.get("article_id"),
                                 "title": str(item.get("title") or "")[:80]})
        elapsed = time.monotonic() - started
        rate = processed / elapsed if elapsed > 0 else 0
        print("第 %d 批：成功 %d / 失败 %d（累计 %d 篇，%.1f 篇/分钟，事件 %d 条）"
              % (batches, batch_ok, batch_failed, processed, rate * 60, events_total))
        consecutive_failed_batches = consecutive_failed_batches + 1 if batch_ok == 0 else 0
        if consecutive_failed_batches >= max(1, int(args.max_consecutive_failures)):
            print("\n[中止] 连续 %d 批全部失败——多为 LLM 端点不可用，先修端点再重跑。"
                  % consecutive_failed_batches)
            break
        if target:
            now_state = _coverage(db)
            if now_state["covered"] >= target:
                print("\n已达到目标覆盖率 %.1f%%，停止。"
                      % (100.0 * target / max(1, state["active"])))
                break

    after = _coverage(db)
    summary.update({
        "processed": processed, "succeeded": succeeded, "failed": failed,
        "events": events_total, "batches": batches,
        "covered_after": after["covered"],
        "coverage_after": round(after["covered"] / max(1, after["active"]), 4),
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "failure_sample": failures[:20],
    })
    print("\n本次：处理 %d 篇（成功 %d / 失败 %d），新增事件 %d 条，用时 %.1f 秒"
          % (processed, succeeded, failed, events_total, summary["elapsed_seconds"]))
    print("活跃文章事件覆盖率：%d/%d = %.1f%%（回填前 %.1f%%）"
          % (after["covered"], after["active"],
             100.0 * after["covered"] / max(1, after["active"]),
             100.0 * state["covered"] / max(1, state["active"])))
    if failures:
        print("失败样例（最多 20 条，重跑会自动重试）：")
        for item in failures[:20]:
            print("   id=%-7s %s" % (item["article_id"], item["title"]))

    if args.normalize_subjects:
        from subject_normalize_service import SubjectNormalizeService

        print("\n=== 实体归一（subject → canonical）===")
        result = SubjectNormalizeService(repository=intel_repository).run(
            pack_id=str(args.pack or ""), all_packs=not str(args.pack or "").strip()
        )
        summary["subject_normalize"] = result
        print("  subjects=%s 规则覆盖=%s LLM长尾=%s → canonical=%s（写入 %s 行）"
              % (result.get("subjects"), result.get("rule_covered"), result.get("llm_tail"),
                 result.get("canonicals"), result.get("written")))

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

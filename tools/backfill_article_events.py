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
                "   AND e.subject NOT IN ('__no_event__', '__error__')"
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
            # 结构化覆盖率：**有事件 或 有属性**就算已结构化。
            # 主系表/数值类文章（例如"某模型是高精度的"）0 事件但已抽到属性，
            # 只按"有事件"统计会把它们误判成没抽到东西。
            try:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM ("
                    "  SELECT e.article_id AS aid FROM intel_article_events e"
                    "   JOIN articles a ON a.id=e.article_id AND a.status='active'"
                    "   WHERE e.subject NOT IN ('__no_event__', '__error__')"
                    "  UNION"
                    "  SELECT at.article_id FROM intel_article_attributes at"
                    "   JOIN articles a ON a.id=at.article_id AND a.status='active'"
                    ") t"
                )
                structured = int(cur.fetchone()["n"] or 0)
                cur.execute("SELECT COUNT(*) AS n FROM intel_article_attributes")
                attributes = int(cur.fetchone()["n"] or 0)
                cur.execute(
                    "SELECT COUNT(*) AS n FROM intel_article_attributes"
                    " WHERE COALESCE(valid_from,'')<>'' OR COALESCE(valid_to,'')<>''"
                )
                attributes_with_validity = int(cur.fetchone()["n"] or 0)
            except Exception:
                structured, attributes, attributes_with_validity = covered, 0, 0
            return {"active": active, "covered": covered, "placeholders": placeholders,
                    "canonicals": canonicals, "rows_with_state": with_state,
                    "structured": structured, "attributes": attributes,
                    "attributes_with_validity": attributes_with_validity}
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
        # COUNT(DISTINCT a.id)：一篇文章可能被多个包归类，普通 COUNT 会把分母放大
        # （实测 A 机被算成 6650，去重后是 4608）
        "SELECT COUNT(DISTINCT a.id) AS n FROM articles a"
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
    parser.add_argument("--max-minutes", type=float, default=0.0,
                        help="最长运行多少分钟（0=不限）。生产上跑长任务要有边界："
                             "到点就停并打印进度，下次重跑会自动接着跑（断点续跑）")
    parser.add_argument("--normalize-subjects", action="store_true",
                        help="抽完后跑实体归一（写 intel_subject_canonical）")
    parser.add_argument("--refresh-attributes", action="store_true",
                        help="补属性模式：对「抽过事件但没有属性行」的存量文章重抽"
                             "（同一次调用同时刷新事件与属性，幂等）")
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
    print("活跃文章 %d 篇；**有事件**的 %d 篇（%.1f%%）；抽过但无事件（占位行）%d 篇；"
          "实体归一 %d 行；带状态字段的事件 %d 行"
          % (state["active"], state["covered"],
             100.0 * state["covered"] / max(1, state["active"]),
             state["placeholders"], state["canonicals"], state["rows_with_state"]))
    if state.get("structured") is not None:
        print("结构化覆盖（有事件**或**有属性）: %d 篇（%.1f%%）；属性 %d 行（带有效期 %d 行）"
              % (state["structured"],
                 100.0 * state["structured"] / max(1, state["active"]),
                 state.get("attributes", 0), state.get("attributes_with_validity", 0)))

    # 覆盖率双口径（阶段 7 验收修正）：旧口径把"本来就没有事件的文章"也算进分母，
    # 天花板只有 46%，60% 永远够不到；新口径的分母是"本来该有事件的那类文章"。
    try:
        cover = intel_repository.coverage_report(pack_id=str(args.pack or ""))
        print("\n覆盖率双口径（阶段 7 验收用**新口径**）：")
        print("  旧口径（分母=全部活跃文章）      %d/%d = %.1f%%"
              % (cover["events_all"], cover["denominator_all"],
                 100.0 * cover["rate_events_all"]))
        print("  新口径（分母=趋势/事件类文章）    %d/%d = %.1f%%   ← 验收看这一行"
              % (cover["events_scope"], cover["denominator_scope"],
                 100.0 * cover["rate_events_scope"]))
        print("  新口径·结构化（含属性）          %d/%d = %.1f%%"
              % (cover["structured_scope"], cover["denominator_scope"],
                 100.0 * cover["rate_structured_scope"]))
        summary["coverage"] = cover
    except Exception as exc:
        print("\n[提示] 覆盖率双口径统计失败（不影响回填）：%s" % str(exc)[:120])
    print("待抽取（活跃 / 有包归属 / trend|event / 命中锚点 / 未抽过）: %d 篇" % eligible)

    # 覆盖率天花板账：让"要不要放宽准入"变成可决策的数字（实测产出率约 59%）
    # 分子分母**都用新口径**：分母=趋势/事件类文章，分子=已有事件 + 0.59×新准入。
    try:
        admission = intel_repository.admission_report(pack_id=str(args.pack or ""))
        cover = summary.get("coverage") or intel_repository.coverage_report(
            pack_id=str(args.pack or ""))
        YIELD = 0.59  # 实测：送抽文章里约 59% 能抽出至少一个事件

        def ceiling(extra_count: int, extra_denominator: int, label: str) -> str:
            numerator = cover["events_scope"] + YIELD * extra_count
            denominator = cover["denominator_scope"] + extra_denominator
            return "  %-14s 准入 %5d 篇（其中新加入分母 %4d）→ 天花板约 %.0f%%" % (
                label, extra_count, extra_denominator,
                100.0 * min(1.0, numerator / max(1.0, denominator)))

        print("\n覆盖率天花板（新口径：趋势/事件类为分母；按实测产出率 %.0f%% 估算）：" % (YIELD * 100))
        print(ceiling(admission["base"], 0, "当前口径"))
        print(ceiling(admission["no_anchor"], 0, "放宽锚点"))
        print(ceiling(admission["with_other"], admission["extra_if_with_other"], "纳入 other 类"))
        print(ceiling(admission["widened"], admission["extra_if_widened"], "两者都放宽"))
        print("  验收线 60%：能否达成看上面四行（当前口径那行）")
        print("  提示：新口径下分母只算"本来该有事件的文章"，因此 60% 是可达的；")
        print("        旧口径（全部文章为分母）天花板只有 46%，不建议再作为验收线。")
        summary["admission"] = admission
    except Exception as exc:
        print("\n[提示] 准入阶梯统计失败（不影响回填）：%s" % str(exc)[:120])

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
    refresh_attributes = bool(args.refresh_attributes)
    if refresh_attributes:
        print("模式：补属性（对象 = 抽过事件但还没有属性行的存量文章）")

    processed = succeeded = failed = events_total = 0
    batches = 0
    consecutive_failed_batches = 0
    started = time.monotonic()
    failures = []

    while True:
        if args.max_minutes and (time.monotonic() - started) >= float(args.max_minutes) * 60:
            print("\n达到 --max-minutes 上限（%.0f 分钟），停止；重跑会自动接着抽。"
                  % float(args.max_minutes))
            break
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
        if refresh_attributes:
            # 补属性模式：对象是"抽过事件、但还没有属性行"的文章（存量文章在旧 prompt 下
            # 只抽了事件）。同一次调用会同时刷新事件与属性，幂等。
            batch = intel_repository.list_articles_missing_attributes(
                pack_id=str(args.pack or ""), limit=fetch
            )
        else:
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
    print("活跃文章**事件覆盖率**：%d/%d = %.1f%%（回填前 %.1f%%；口径不含占位行）"
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

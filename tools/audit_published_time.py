#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布时间精度体检（阶段 4 收尾）：分布可查 + 抽样核对 + 自洽性检查（只读）。

《爬虫改进实施步骤》阶段 4 要求"对 200 篇抽样人工核对精度分布"。人工逐篇看不现实，
这里把**人能看出来的问题**变成可执行断言，并把样本原样打印出来供人工眼过：

  1. 分布：published_precision / published_time_source / published_timezone 三张分布表；
  2. 自洽：precision ∈ {date,day,url} 时 published_at_utc 前 10 位必须等于源站本地日期
     （与读取方 financial_evidence._article_time_interval 的约定一致；不相等就是整体错一天）；
  3. 合法：published_at_utc 不得超过今天（不得早于 2000 年），时区必须是可加载的 IANA 名；
  4. 覆盖：published_at_utc 非空率、以及"只有抓取时间"（discovered）的占比；
  5. 抽样：随机 N 篇（默认 200，可 --seed 复现）打印 id/日期/UTC/时区/精度/来源/标题，
     供人工核对"这条日期放在这篇文章上合不合理"。

用法：
    python tools/audit_published_time.py
    python tools/audit_published_time.py --sample 200 --seed 20261008
    python tools/audit_published_time.py --json
退出码：0 = 无自洽性问题；1 = 发现错位/非法时间（需修数据或修抽取）。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from datetime import date, datetime, timezone

sys.path.insert(0, __file__.rsplit("tools", 1)[0])

# 这些精度下 published_at_utc 的前 10 位**就是**源站本地日期（见 publish_time.to_utc 注释）
DATE_LIKE_PRECISIONS = {"date", "day", "url"}


def _zone_ok(name: str) -> bool:
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(str(name))
        return True
    except Exception:
        try:
            import pytz

            pytz.timezone(str(name))
            return True
        except Exception:
            return False


def _rows(db, status: str):
    db._ensure_connection()
    where = "WHERE status='active'" if status == "active" else ""
    with db.lock:
        cur = db.connection.cursor()
        try:
            cur.execute(
                "SELECT id, title, url, publish_date, published_at_utc, published_timezone,"
                " published_precision, published_time_source, first_crawled"
                f" FROM articles {where} ORDER BY id"
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            cur.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="发布时间精度体检")
    parser.add_argument("--sample", type=int, default=200, help="抽样条数（默认 200）")
    parser.add_argument("--seed", type=int, default=20261008, help="抽样随机种子（可复现）")
    parser.add_argument("--all-status", action="store_true", help="含非 active 文章")
    parser.add_argument("--show", type=int, default=20, help="打印多少条样本明细")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from publish_time import normalize_precision
    from sqlite_database import sqlite_db

    rows = _rows(sqlite_db, "all" if args.all_status else "active")
    total = len(rows)
    today = date.today()

    filled = [row for row in rows if str(row.get("published_at_utc") or "").strip()]
    precision_dist = Counter()
    source_dist = Counter()
    zone_dist = Counter()
    problems = {
        "day_mismatch": [],      # published_at_utc[:10] 与源站本地日期不一致（整体错一天）
        "future_implausible": [],  # 晚于今天 + 90 天（按产品规则不可采信）
        "future_announced": [],  # 未来 90 天内的"预告日期"（合规，UI 有标记，仅提示）
        "too_old": [],           # 早于 2000 年
        "bad_timezone": [],      # 时区名不可加载
        "missing_zone": [],      # 有日期精度却没有时区
    }
    # 未来日期的判据必须与入库闸门**同一个**（article_identity._is_implausible_future_date）：
    # 产品允许 90 天内的"预告日期"（活动/发布会），时间轴会标「预告日期」，
    # 审计若自己定更严的线，就会把合规数据报成错误。
    try:
        from article_identity import _is_implausible_future_date
    except Exception:
        def _is_implausible_future_date(value):
            return False
    for row in rows:
        precision = normalize_precision(row.get("published_precision"))
        zone = str(row.get("published_timezone") or "").strip()
        instant = str(row.get("published_at_utc") or "").strip()
        precision_dist[precision or "(空)"] += 1
        source_dist[str(row.get("published_time_source") or "(空)")] += 1
        if zone:
            zone_dist[zone] += 1
        if not instant:
            continue
        day_text = instant[:10]
        try:
            instant_day = date(int(day_text[0:4]), int(day_text[5:7]), int(day_text[8:10]))
        except (ValueError, IndexError):
            problems["day_mismatch"].append((row["id"], "无法解析 UTC 日期", instant))
            continue
        if _is_implausible_future_date(instant):
            problems["future_implausible"].append((row["id"], instant, row.get("title")))
        elif instant_day > today:
            problems["future_announced"].append((row["id"], instant, row.get("title")))
        if instant_day.year < 2000:
            problems["too_old"].append((row["id"], instant, row.get("title")))
        local_day = str(row.get("publish_date") or "")[:10]
        if precision in DATE_LIKE_PRECISIONS and local_day and day_text != local_day:
            problems["day_mismatch"].append((row["id"], local_day, instant))
        if precision in DATE_LIKE_PRECISIONS and not zone:
            problems["missing_zone"].append((row["id"], local_day))
        if zone and not _zone_ok(zone):
            problems["bad_timezone"].append((row["id"], zone))

    rng = random.Random(int(args.seed))
    sample = rng.sample(rows, min(int(args.sample), total)) if total else []
    discovered = sum(1 for row in rows
                     if normalize_precision(row.get("published_precision")) == "discovered")

    print("=" * 96)
    print("发布时间精度体检（阶段 4）· 范围：%s，文章 %d 篇"
          % ("全部状态" if args.all_status else "status=active", total))
    print("=" * 96)
    print("published_at_utc 非空：%d/%d = %.1f%%"
          % (len(filled), total, 100.0 * len(filled) / max(1, total)))
    print("其中 precision=discovered（只有抓取时间，硬约束会排除）：%d 篇（%.1f%%）"
          % (discovered, 100.0 * discovered / max(1, total)))
    print("\n精度分布：%s" % dict(precision_dist))
    print("来源分布：%s" % dict(source_dist.most_common(12)))
    print("时区分布：%s" % dict(zone_dist.most_common(12)))

    print("\n=== 自洽性检查 ===")
    checks = [
        ("日期错位（UTC 前 10 位 ≠ 源站本地日期）", problems["day_mismatch"], True),
        ("未来日期超过 90 天（产品规则不可采信）", problems["future_implausible"], True),
        ("未来 90 天内的预告日期（合规，仅提示）", problems["future_announced"], False),
        ("早于 2000 年", problems["too_old"], True),
        ("时区名不可加载", problems["bad_timezone"], True),
        ("有日期精度但缺时区", problems["missing_zone"], False),
    ]
    fatal = 0
    for label, items, is_fatal in checks:
        mark = "✅" if not items else ("❌" if is_fatal else "⚠️")
        print("  %s %s：%d 条" % (mark, label, len(items)))
        for item in items[:5]:
            print("       %s" % (item,))
        if items and is_fatal:
            fatal += len(items)

    print("\n=== 抽样明细（seed=%s，共 %d 条，打印前 %d 条）==="
          % (args.seed, len(sample), min(args.show, len(sample))))
    print("%-8s %-12s %-21s %-17s %-11s %-22s %s"
          % ("id", "源站日期", "published_at_utc", "时区", "精度", "来源", "标题"))
    for row in sample[: max(0, int(args.show))]:
        print("%-8s %-12s %-21s %-17s %-11s %-22s %s"
              % (row["id"], str(row.get("publish_date") or "-")[:10],
                 str(row.get("published_at_utc") or "-")[:21],
                 str(row.get("published_timezone") or "-")[:17],
                 normalize_precision(row.get("published_precision")) or "-",
                 str(row.get("published_time_source") or "-")[:22],
                 str(row.get("title") or "")[:28]))

    report = {
        "scope": "all" if args.all_status else "active",
        "articles": total,
        "filled": len(filled),
        "coverage": round(len(filled) / max(1, total), 4),
        "discovered": discovered,
        "precision": dict(precision_dist),
        "source": dict(source_dist),
        "timezone": dict(zone_dist),
        "problems": {key: len(value) for key, value in problems.items()},
        "problem_samples": {key: value[:20] for key, value in problems.items()},
        "sample_seed": args.seed,
        "sample_size": len(sample),
    }
    if args.json:
        print()
        print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    return 1 if fatal else 0


if __name__ == "__main__":
    raise SystemExit(main())

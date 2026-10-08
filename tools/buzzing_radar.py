#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行：打印某个行业包的海外财经标题雷达。

用法：
    python tools/buzzing_radar.py --industry family_office
    python tools/buzzing_radar.py --industry financial_markets --top 20 --days 3 --json

只读：拉 feed + 查我方文章覆盖情况，不写库、不产生候选、不占爬取槽。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from buzzing_radar import radar_report  # noqa: E402


def _print_text(report: dict) -> None:
    print("=" * 78)
    print(f"海外财经标题雷达 · 行业包={report['industry_pack_id']} · "
          f"{report['entry_count']} 条标题 · 词表 {report['vocabulary_size']} 词")
    print("=" * 78)
    for feed in report["feeds"]:
        state = feed["error"] or ("缓存" if feed["cached"] else "实时")
        print(f"  feed {feed['feed']} → {feed['entry_count']} 条（{state}）")

    print("\n出处排行（谁在密集发声）:")
    for row in report["publishers"][:10]:
        print(f"  {row['publisher']:<26} {row['count']}")

    print("\n热词榜:")
    print(f"  {'热词':<16}{'条数':>4}  {'我方标题命中':>10}  {'近N天趋势':>9}  状态")
    for row in report["hot_terms"]:
        trend = "-" if row["our_trend_articles"] is None else row["our_trend_articles"]
        if row["covered"] is True:
            state = "已覆盖"
        elif row["covered"] is False:
            state = "**缺口**" if row["in_vocabulary"] else "词表外"
        else:
            state = "无法判定"
        print(f"  {row['term']:<16}{row['count']:>4}  "
              f"{str(row['our_title_hits']):>10}  {str(trend):>9}  {state}")

    missing = report["missing_terms"]
    if missing:
        print("\n覆盖缺口（海外在炒、我方 0 篇）:")
        for row in missing:
            print(f"  · {row['term']}（{row['count']} 条海外标题）例："
                  f"{(row['titles'] or [''])[0][:56]}")

    new_terms = report["new_terms"]
    if new_terms:
        print("\n词表里没有的词（考虑补进行业包词表）:")
        print("  " + "、".join(row["term"] for row in new_terms))
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="海外财经标题雷达（buzzing.cc）")
    parser.add_argument("--industry", default=config.INTEL_DEFAULT_INDUSTRY_PACK)
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--days", type=int, default=2)
    parser.add_argument("--json", action="store_true", help="输出原始 JSON")
    args = parser.parse_args()

    if not getattr(config, "BUZZING_RADAR_ENABLED", True):
        print("BUZZING_RADAR_ENABLED=false，已关闭", file=sys.stderr)
        return 2
    report = radar_report(args.industry, days=args.days, top=args.top)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

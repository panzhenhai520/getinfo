#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 9 · 业务规则同步工具（行业包配置 → `intel_business_rules`）。

平时由"行业包激活"自动触发（`industry_pack_activation._apply_preview` 成功后同步）；
这个工具用于**首次铺底**与排查：把现有包的规则一次性灌进表里，或只看差异不写库。

用法
    python tools/sync_business_rules.py                 # 预演：列出每个包会生成多少条规则
    python tools/sync_business_rules.py --apply         # 真写
    python tools/sync_business_rules.py --apply --pack family_office
    python tools/sync_business_rules.py --list          # 看表里现状（按包分组）
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intel_database import intel_repository  # noqa: E402
from sqlite_database import sqlite_db  # noqa: E402


def _packs():
    from industry_packs import industry_pack_loader

    items = []
    try:
        for pack in industry_pack_loader.list() or []:
            pack_id = str((pack or {}).get("id") or "")
            if pack_id:
                items.append(pack_id)
    except Exception:
        pass
    return items


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="业务规则同步（行业包配置 → intel_business_rules）")
    parser.add_argument("--apply", action="store_true", help="真写库（默认只预演）")
    parser.add_argument("--pack", default="", help="只同步某个行业包")
    parser.add_argument("--list", action="store_true", help="列出表里现状")
    args = parser.parse_args(argv)

    intel_repository._ensure()

    if args.list:
        rows = intel_repository.list_business_rules(pack_id=args.pack, enabled_only=False)
        grouped = {}
        for row in rows:
            grouped.setdefault(str(row["industry_pack_id"]), []).append(row)
        print("规则表现状：%d 条 / %d 个包" % (len(rows), len(grouped)))
        for pack_id, items in sorted(grouped.items()):
            kinds = {}
            for item in items:
                kinds[str(item["rule_type"])] = kinds.get(str(item["rule_type"]), 0) + 1
            print("   %-26s %3d 条（%s）"
                  % (pack_id, len(items),
                     "、".join("%s×%d" % (key, value) for key, value in sorted(kinds.items()))))
        return 0

    from business_rules import business_rule_engine
    from industry_packs import industry_pack_loader

    pack_ids = [str(args.pack)] if args.pack else _packs()
    if not pack_ids:
        print("没有可同步的行业包。")
        return 1

    total_rules = total_written = 0
    print("%s（%d 个包）" % ("真写库" if args.apply else "预演（未写库）", len(pack_ids)))
    for pack_id in pack_ids:
        try:
            pack = industry_pack_loader.load(pack_id) or {}
        except Exception as exc:
            print("   %-26s 跳过：%s" % (pack_id, str(exc)[:60]))
            continue
        if not args.apply:
            keywords = len(pack.get("core_keywords") or [])
            topics = len(pack.get("fixed_topics") or [])
            print("   %-26s 预计 keyword %d + topic %d 条" % (pack_id, keywords, topics))
            continue
        outcome = business_rule_engine.sync_from_pack(pack_id, pack)
        total_rules += int(outcome.get("rules") or 0)
        total_written += int(outcome.get("written") or 0)
        print("   %-26s 规则 %3d 条（写入 %d）"
              % (pack_id, outcome.get("rules") or 0, outcome.get("written") or 0))
    if args.apply:
        print("合计：规则 %d 条（写入 %d）" % (total_rules, total_written))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理指定行业包下的包用户 / 执行包用户唯一性迁移（默认 dry-run）。

在项目根目录执行：
    python tools/cleanup_pack_users.py --list                     # 按行业包汇总用户数
    python tools/cleanup_pack_users.py --pack <industry_pack_id>  # 列出该包用户（预览）
    python tools/cleanup_pack_users.py --pack <industry_pack_id> --apply   # 真正删除
    python tools/cleanup_pack_users.py --dedupe                   # 去重 + 补唯一索引
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pack_tenant  # noqa: E402
from pack_tenant import sqlite_db  # noqa: E402


def _summary():
    pack_tenant._ensure()
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        cur.execute(
            "SELECT industry_pack_id, COUNT(*) AS n FROM pack_users"
            " GROUP BY industry_pack_id ORDER BY industry_pack_id"
        )
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
    return rows


def _rows(pack: str):
    pack_tenant._ensure()
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        cur.execute(
            "SELECT id, industry_pack_id, username, email, activated, status, must_change_password"
            " FROM pack_users WHERE industry_pack_id=? ORDER BY id",
            (str(pack),),
        )
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="清理行业包下的包用户")
    parser.add_argument("--pack", default="", help="industry_pack_id")
    parser.add_argument("--list", action="store_true", help="按行业包汇总用户数")
    parser.add_argument("--apply", action="store_true", help="真正执行删除（默认只预览）")
    parser.add_argument("--dedupe", action="store_true", help="执行唯一性迁移（去重+补唯一索引）")
    args = parser.parse_args()

    if args.dedupe:
        print("唯一性迁移结果:", pack_tenant.ensure_pack_user_uniqueness())
        return 0

    if not args.pack:
        print("按行业包汇总：")
        for r in _summary():
            print("   {0}  {1}".format(r["industry_pack_id"], r["n"]))
        print("\n用 --pack <industry_pack_id> 查看/删除某个包的用户。")
        return 0

    rows = _rows(args.pack)
    print("包 {0} 下共 {1} 个用户：".format(args.pack, len(rows)))
    for r in rows:
        print("   ", r)
    if not rows:
        return 0
    if not args.apply:
        print("\n（预览模式）确认无误后加 --apply 执行删除。")
        return 0
    n = pack_tenant.delete_pack_users(args.pack)
    print("\n已删除 {0} 个用户。".format(n))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

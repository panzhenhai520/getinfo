#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回填 articles.published_at_utc（阶段 4：时间维度补前置）。

背景（实测）：`published_at_utc` 在整表里是**空的**——列表/正文抽取写的是
`publish_date`（日期）与 `published_precision`/`published_time_source`，
而排序、时效判定、时间窗硬约束读的却是 `published_at_utc`。
读取方（`financial_evidence._article_time_interval`、`financial_news_query._published_fields`）
已有明确约定：precision ∈ {date, day} 时，`published_at_utc` 的**前 10 位必须是源站本地日期**，
配合 `published_timezone` 才能拼出"那一天的本地时间区间"。本工具按同一约定回填。

取值顺序（与《爬虫改进实施步骤》阶段 4 一致）：
  ① 已有 publish_date（采集当刻抽到的，含时分的按真实瞬间换算；只有日期的按本地日期字面量）
  ② URL 里的日期（/2026/10/07/ 等）
  ③ 正文首尾的日期文本
  ④ 抓取水位线 first_crawled —— **默认不用**：它不是发布时间，只是"我们发现它的时间"。
     需要时显式加 `--include-discovered`，会被标成 precision=discovered，供硬约束排除。

用法：
    python tools/backfill_published_at_utc.py                     # 只读审计（默认）
    python tools/backfill_published_at_utc.py --apply             # 回填（先自动导出备份）
    python tools/backfill_published_at_utc.py --apply --include-discovered
    python tools/backfill_published_at_utc.py --all-status --limit 500
    python tools/backfill_published_at_utc.py --rollback data/backups/xxx.json
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time

sys.path.insert(0, __file__.rsplit("tools", 1)[0])

BACKUP_DIR = os.path.join(__file__.rsplit("tools", 1)[0], "data", "backups")


def _rows_needing_backfill(db, *, statuses, limit: int = 0):
    """published_at_utc 为空、但还有别的线索可用的文章。"""
    db._ensure_connection()
    where = ["COALESCE(a.published_at_utc, '') = ''"]
    params = []
    if statuses:
        where.append("a.status IN (%s)" % ",".join("?" for _ in statuses))
        params.extend(statuses)
    sql = (
        "SELECT a.id, a.url, a.title, a.publish_date, a.published_precision,"
        " a.published_time_source, a.first_crawled, a.status,"
        " SUBSTR(COALESCE(a.content_markdown, a.content, ''), 1, 1200) AS head_text,"
        " SUBSTR(COALESCE(a.content_markdown, a.content, ''), -800) AS tail_text"
        " FROM articles a WHERE " + " AND ".join(where) +
        " ORDER BY a.id DESC"
    )
    if limit:
        sql += " LIMIT %d" % int(limit)
    with db.lock:
        cur = db.connection.cursor()
        try:
            cur.execute(sql, tuple(params))
            return [dict(row) for row in cur.fetchall()]
        finally:
            cur.close()


def _derive(row, *, include_discovered: bool):
    """给一篇文章定出 (published_at, precision, source)；拿不到返回 None。

    与入库闸门共用同一条"未来日期不可采信"规则（article_identity._is_implausible_future_date，
    容忍 90 天内的预告日期）：A 机实测有 `published_at_utc=2027-12-01` 这种从正文里误抽出来的
    未来日期——回填时若原样搬运，等于把脏日期灌进排序与时间过滤。遇到就**跳过该来源**，
    继续尝试 URL/正文（而不是直接放弃）。
    """
    from publish_time import (
        PRECISION_DISCOVERED, parse_text_date, parse_url_date,
    )

    try:
        from article_identity import _is_implausible_future_date
    except Exception:
        def _is_implausible_future_date(value):
            return False

    existing = str(row.get("publish_date") or "").strip()
    if existing and not _is_implausible_future_date(existing):
        precision = str(row.get("published_precision") or "").strip()
        source = str(row.get("published_time_source") or "").strip() or "publish_date"
        if source.startswith("crawl_watermark"):
            # 水位线只能证明"在采集窗口内"，不是发布时间本身
            precision = precision or PRECISION_DISCOVERED
        return existing, precision, source, "publish_date"
    if existing:
        print("   ⏭️ 跳过不可采信的未来发布日期 %s（id=%s）" % (existing[:10], row.get("id")))

    parsed = parse_url_date(str(row.get("url") or ""))
    if parsed[0]:
        return parsed[0], parsed[1], "url", "url"
    for key in ("head_text", "tail_text"):
        parsed = parse_text_date(str(row.get(key) or ""))
        if parsed[0] and not _is_implausible_future_date(parsed[0]):
            return parsed[0], parsed[1], "content_" + key.split("_")[0], "content"

    if include_discovered:
        first_seen = str(row.get("first_crawled") or "").strip()
        if first_seen:
            return first_seen, PRECISION_DISCOVERED, "first_seen", "discovered"
    return None


def _coverage(db, statuses):
    db._ensure_connection()
    where, params = "", []
    if statuses:
        where = " WHERE status IN (%s)" % ",".join("?" for _ in statuses)
        params = list(statuses)
    with db.lock:
        cur = db.connection.cursor()
        try:
            cur.execute(
                "SELECT COUNT(*) AS n,"
                " SUM(CASE WHEN COALESCE(published_at_utc,'') <> '' THEN 1 ELSE 0 END) AS filled,"
                " SUM(CASE WHEN COALESCE(publish_date,'') <> '' THEN 1 ELSE 0 END) AS dated"
                " FROM articles" + where,
                tuple(params),
            )
            row = cur.fetchone()
            total = int(row["n"] or 0)
            filled = int(row["filled"] or 0)
            dated = int(row["dated"] or 0)
            return total, filled, dated
        finally:
            cur.close()


def _write_backup(rows) -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(
        BACKUP_DIR, "published_at_utc_%s.json" % time.strftime("%Y%m%d%H%M%S")
    )
    payload = [
        {
            "id": int(row["id"]),
            "published_at_utc": "",
            "published_timezone": "",
            "published_precision": row.get("published_precision") or "",
            "published_time_source": row.get("published_time_source") or "",
        }
        for row in rows
    ]
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
    return path


def rollback(db, path: str) -> int:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    restored = 0
    with db.lock:
        cur = db.connection.cursor()
        try:
            for item in payload:
                cur.execute(
                    "UPDATE articles SET published_at_utc=?, published_timezone=?,"
                    " published_precision=?, published_time_source=? WHERE id=?",
                    (
                        item.get("published_at_utc") or "",
                        item.get("published_timezone") or "",
                        item.get("published_precision") or "",
                        item.get("published_time_source") or "",
                        int(item["id"]),
                    ),
                )
                restored += 1
            db.connection.commit()
        finally:
            cur.close()
    return restored


def clear_implausible(db, *, statuses, apply: bool) -> int:
    """清掉"不可采信的未来日期"（超过今天 + 90 天）。

    这类值多半是早期从正文里误抽出来的（实测 A 机有 2027-12-01 落在 published_at_utc，
    源列 publish_date 里也还留着同一个假日期）。**两个列一起清**——口径与入库闸门一致
    （sqlite_database 里对未来的 publish_date 就是"直接抹掉，宁可显示未知"）；
    清空后重跑回填会按 URL/正文重新取值。
    """
    try:
        from article_identity import _is_implausible_future_date
    except Exception:
        return 0
    db._ensure_connection()
    where = ["(COALESCE(published_at_utc,'') <> '' OR COALESCE(publish_date,'') <> '')"]
    params = []
    if statuses:
        where.append("status IN (%s)" % ",".join("?" for _ in statuses))
        params.extend(statuses)
    with db.lock:
        cur = db.connection.cursor()
        try:
            cur.execute(
                "SELECT id, publish_date, published_at_utc, title FROM articles WHERE "
                + " AND ".join(where),
                tuple(params),
            )
            rows = [dict(row) for row in cur.fetchall()]
        finally:
            cur.close()
    victims = [
        row for row in rows
        if _is_implausible_future_date(row.get("publish_date"))
        or _is_implausible_future_date(row.get("published_at_utc"))
    ]
    print("\n不可采信的未来日期（> 今天 + 90 天）：%d 条" % len(victims))
    for row in victims[:10]:
        print("   id=%s publish_date=%s published_at_utc=%s  %s"
              % (row["id"], str(row.get("publish_date") or "-")[:10],
                 str(row.get("published_at_utc") or "-")[:10],
                 str(row.get("title") or "")[:34]))
    if not victims or not apply:
        if victims:
            print("   [dry-run] 未清理；加 --apply 一起清掉（随后重跑回填会自动重新取值）")
        return len(victims)
    ids = [int(row["id"]) for row in victims]
    with db.lock:
        cur = db.connection.cursor()
        try:
            for chunk_start in range(0, len(ids), 200):
                chunk = ids[chunk_start:chunk_start + 200]
                marks = ",".join("?" for _ in chunk)
                cur.execute(
                    "UPDATE articles SET publish_date='', published_at_utc='',"
                    " published_timezone='', published_precision='', published_time_source=''"
                    " WHERE id IN (%s)" % marks,
                    tuple(chunk),
                )
            db.connection.commit()
        finally:
            cur.close()
    print("   已清空 %d 行（publish_date 与四个时间列；重跑回填会按 URL/正文重新取值）" % len(ids))
    return len(ids)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="回填 articles.published_at_utc")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只读审计）")
    parser.add_argument("--all-status", action="store_true",
                        help="包含非 active 文章（默认只处理 status='active'）")
    parser.add_argument("--include-discovered", action="store_true",
                        help="允许用 first_crawled 兜底（标 precision=discovered）")
    parser.add_argument("--limit", type=int, default=0, help="只处理最新的 N 篇（0=全部）")
    parser.add_argument("--fix-implausible", action="store_true",
                        help="先清掉不可采信的未来时间（配合 --apply）")
    parser.add_argument("--rollback", default="", help="用备份文件回滚")
    args = parser.parse_args(argv)

    from publish_time import article_time_fields
    from sqlite_database import sqlite_db

    if args.rollback:
        restored = rollback(sqlite_db, args.rollback)
        print("已回滚 %d 行（来自 %s）" % (restored, args.rollback))
        return 0

    statuses = [] if args.all_status else ["active"]
    if args.fix_implausible:
        clear_implausible(sqlite_db, statuses=statuses, apply=bool(args.apply))
    total, filled, dated = _coverage(sqlite_db, statuses)
    label = "全部状态" if args.all_status else "status=active"
    print("=" * 84)
    print("published_at_utc 回填（范围：%s，兜底水位线：%s）"
          % (label, "开" if args.include_discovered else "关"))
    print("=" * 84)
    print("现有文章 %d 篇：已有 published_at_utc %d 篇（%.1f%%）、有 publish_date %d 篇（%.1f%%）"
          % (total, filled, 100.0 * filled / max(1, total), dated, 100.0 * dated / max(1, total)))

    rows = _rows_needing_backfill(sqlite_db, statuses=statuses, limit=args.limit)
    print("待处理（published_at_utc 为空）: %d 篇" % len(rows))

    planned, skipped = [], []
    for row in rows:
        derived = _derive(row, include_discovered=args.include_discovered)
        if not derived:
            skipped.append(row)
            continue
        published_at, precision, source, origin = derived
        fields = article_time_fields(
            published_at=published_at, precision=precision, source=source,
            domain=str(row.get("url") or ""),
        )
        if not fields.get("published_at_utc"):
            fields = article_time_fields(
                published_at=published_at, precision=precision, source=source
            )
        if not fields.get("published_at_utc"):
            skipped.append(row)
            continue
        planned.append((row, fields, origin))

    by_origin = collections.Counter(item[2] for item in planned)
    by_precision = collections.Counter(item[1]["published_precision"] for item in planned)
    print("\n可回填 %d 篇；拿不到时间 %d 篇" % (len(planned), len(skipped)))
    print("  取值来源: %s" % dict(by_origin))
    print("  精度分布: %s" % dict(by_precision))
    after = filled + len(planned)
    print("  回填后 published_at_utc 覆盖率: %.1f%%（%d/%d）"
          % (100.0 * after / max(1, total), after, total))

    print("\n样例（前 5 条）:")
    for row, fields, origin in planned[:5]:
        print("  id=%-7s [%s] %s -> %s (%s) %s"
              % (row["id"], origin, str(row.get("publish_date") or "-")[:10],
                 fields["published_at_utc"], fields["published_precision"],
                 str(row.get("title") or "")[:36]))

    if not args.apply:
        print("\n[dry-run] 未写库。确认后加 --apply 执行（会先导出备份）。")
        return 0

    backup_path = _write_backup(rows)
    print("\n已导出备份: %s" % backup_path)

    written = 0
    with sqlite_db.lock:
        cur = sqlite_db.connection.cursor()
        try:
            for row, fields, _origin in planned:
                cur.execute(
                    "UPDATE articles SET published_at_utc=?, published_timezone=?,"
                    " published_precision=?, published_time_source=? WHERE id=?",
                    (
                        fields["published_at_utc"], fields["published_timezone"],
                        fields["published_precision"], fields["published_time_source"],
                        int(row["id"]),
                    ),
                )
                written += 1
            sqlite_db.connection.commit()
        finally:
            cur.close()
    print("已回填 %d 行。回滚：python tools/backfill_published_at_utc.py --rollback %s"
          % (written, backup_path))
    _total, _filled, _dated = _coverage(sqlite_db, statuses)
    print("复核：published_at_utc 覆盖 %d/%d（%.1f%%）"
          % (_filled, _total, 100.0 * _filled / max(1, _total)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

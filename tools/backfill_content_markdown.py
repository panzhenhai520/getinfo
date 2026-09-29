#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""存量回填：为已有文章生成统一 Markdown 正文（content_markdown）与原始正文快照（raw_content）。

阶段1（正文统一 Markdown）配套工具：
- 确保 articles 表存在 raw_content / content_markdown 两列（兼容 SQLite / PostgreSQL 双模式）；
- 对 active 且 content_markdown 为空的文章，按 build_article_markdown(raw, content) 生成展示用 Markdown；
- raw_content 为空时用 content 补齐，保证详情页"原文比对"有内容可看；
- 不改动 content 字段（翻译/语音/向量/哈希等旧链路零影响）。

用法:
    python tools/backfill_content_markdown.py --dry-run       # 只看范围
    python tools/backfill_content_markdown.py --limit 200    # 只回填前 200 篇
    python tools/backfill_content_markdown.py                # 全部回填
"""
import argparse
import sys

sys.path.insert(0, ".")

from db_connection import connect_database, is_postgres_connection


def ensure_columns(conn, cur):
    """确保 articles 表存在 raw_content / content_markdown 两列（幂等）。"""
    wanted = {"raw_content": "TEXT", "content_markdown": "TEXT"}
    existing = set()
    if is_postgres_connection(conn):
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='articles' AND table_schema=current_schema()"
        )
        existing = {str(r[0]) for r in cur.fetchall()}
    else:
        cur.execute("PRAGMA table_info(articles)")
        existing = {str(r[1]) for r in cur.fetchall()}
    for name, definition in wanted.items():
        if name not in existing:
            cur.execute(f"ALTER TABLE articles ADD COLUMN {name} {definition}")
            print(f"  + 已添加列 articles.{name} {definition}", flush=True)
            if not is_postgres_connection(conn):
                conn.commit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="最多回填多少篇（0=不限）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不实际写入")
    parser.add_argument("--refresh", action="store_true", help="重新生成全部文章（含已有 content_markdown 的）")
    args = parser.parse_args()

    from content_handlers import build_article_markdown

    conn = connect_database()
    cur = conn.cursor()
    ensure_columns(conn, cur)

    where = "" if args.refresh else "AND (content_markdown IS NULL OR content_markdown='') "
    cur.execute(
        "SELECT id, title, content, raw_content, extraction_method FROM articles "
        f"WHERE status='active' {where}ORDER BY id"
    )
    rows = cur.fetchall()
    print(f"待回填文章: {len(rows)}", flush=True)

    if args.dry_run:
        for r in rows[: min(args.limit or len(rows), 30)]:
            print(f"  dry-run: article {r['id']} {str(r['title'])[:48]}", flush=True)
        cur.close()
        conn.close()
        return

    targets = rows[: args.limit] if args.limit else rows
    done = 0
    failed = 0
    for idx, r in enumerate(targets, 1):
        aid = int(r["id"])
        content = str(r["content"] or "")
        raw = str(r["raw_content"] or "").strip() or content
        # vpn_ocr：OCR 原文含导航噪声，展示 Markdown 优先用 content（摘要/精炼文），
        # 原文仍保留在 raw_content 备查
        if str(r["extraction_method"] or "") == "vpn_ocr":
            raw_for_markdown = ""
        else:
            raw_for_markdown = raw
        try:
            md = build_article_markdown(raw_for_markdown, content)
            cur.execute(
                "UPDATE articles SET raw_content=?, content_markdown=? WHERE id=?",
                (raw, md, aid),
            )
            if not is_postgres_connection(conn) and idx % 50 == 0:
                conn.commit()
            done += 1
        except Exception as exc:
            failed += 1
            print(f"  fail article {aid}: {exc}", flush=True)
        if idx % 100 == 0:
            print(f"  进度 {idx}/{len(targets)}（成功 {done}，失败 {failed}）", flush=True)
    if not is_postgres_connection(conn):
        conn.commit()
    cur.close()
    conn.close()
    print(f"回填完成: 成功 {done}，失败 {failed}，共 {len(targets)}", flush=True)


if __name__ == "__main__":
    main()

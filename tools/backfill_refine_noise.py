#!/usr/bin/env python3
"""存量文章噪音清洗 + 精炼补齐（生产机一次性脚本）。

用法（在生产容器内运行）：
    python tools/backfill_refine_noise.py --noise-strip --limit 2000   # 可反复执行
    python tools/backfill_refine_noise.py --enqueue --limit 500       # 可反复执行
    python tools/backfill_refine_noise.py --requeue-failed            # 重置误标 completed 的 enrich 任务

说明：
- --noise-strip（别名 --image-strip）：对存量 content 应用 clean_article_markdown
  （图片行/行内图片 token、连续短行导航块）与标题行去重；对 content_markdown
  剔除图片 token、短行块、标题行去重。content 变化时同步刷新 content_hash 与
  content_length。幂等，可分批反复执行。
- --enqueue：为「无 completed 精炼记录、也从未入过 enrich 队列」的 active 文章
  补入 enrich 任务（dedupe_key=enrich:{id}，与新入库漏斗同 key，天然幂等）。
  按 id 倒序（新文优先），每次最多 --limit 条。
- --requeue-failed：把「status=completed 但 result_json 里 success=false」的 enrich
  任务重置为 queued 重试（修复 worker 误标 completed 的历史数据）。
"""
import argparse
import hashlib
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlite_database import (
    clean_article_markdown,
    sqlite_db,
    _dedup_title_lines,
)
from content_handlers import _drop_short_line_blocks
from intel_database import IntelRepository

_IMG_TOKEN = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def _strip_markdown_noise(text: str, title: str) -> str:
    """展示 Markdown 清洗：图片 token、短行块、标题行去重。"""
    if not text:
        return ""
    out = _IMG_TOKEN.sub("", text)
    out = _drop_short_line_blocks(out)
    out = _dedup_title_lines(out, title)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument(
        "--noise-strip", "--image-strip", action="store_true", dest="noise_strip"
    )
    parser.add_argument("--enqueue", action="store_true")
    parser.add_argument("--requeue-failed", action="store_true")
    args = parser.parse_args()
    if not (args.noise_strip or args.enqueue or args.requeue_failed):
        parser.error("至少指定 --noise-strip / --enqueue / --requeue-failed 之一")

    db = sqlite_db
    db._ensure_connection()
    repo = IntelRepository(db)
    cur = db.connection.cursor()
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if args.requeue_failed:
        cur.execute(
            """
            UPDATE intel_jobs
            SET status='queued', lease_owner=NULL, lease_expires_at=NULL,
                next_retry_at=NULL, completed_at=NULL, updated_at=?
            WHERE job_type='enrich' AND status='completed'
              AND result_json LIKE '%"success": false%'
            """,
            (now_utc,),
        )
        db.connection.commit()
        print(f"[requeue-failed] 已重置 {cur.rowcount} 条误标 completed 的 enrich 任务")

    if args.noise_strip:
        rows = cur.execute(
            """
            SELECT id, title, content, content_markdown FROM articles
            WHERE status='active'
              AND (content LIKE '%![%' OR content_markdown LIKE '%![%'
                   OR content LIKE '%新浪首页%' OR content_markdown LIKE '%新浪首页%')
            ORDER BY id DESC LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        if not rows:
            # 兜底：没有明显标记时也轮询最近的未精炼文章
            rows = cur.execute(
                """
                SELECT a.id, a.title, a.content, a.content_markdown FROM articles a
                WHERE a.status='active'
                  AND NOT EXISTS (
                      SELECT 1 FROM article_derivatives d
                      WHERE d.article_id=a.id AND d.summary IS NOT NULL
                        AND d.summary!='' AND d.status='completed')
                ORDER BY a.id DESC LIMIT ?
                """,
                (args.limit,),
            ).fetchall()
        print(f"[noise-strip] 本轮扫描 {len(rows)} 条")
        changed = 0
        for row in rows:
            aid, title = row["id"], row["title"] or ""
            content = row["content"] or ""
            md = row["content_markdown"] or ""
            new_content = clean_article_markdown(content)
            new_content = _dedup_title_lines(new_content, title)
            new_md = _strip_markdown_noise(md, title)
            if new_content == content and new_md == md:
                continue
            db.connection.execute(
                "UPDATE articles SET content=?, content_hash=?, content_length=?, content_markdown=? WHERE id=?",
                (
                    new_content,
                    hashlib.md5(new_content.encode("utf-8")).hexdigest(),
                    len(new_content),
                    new_md,
                    aid,
                ),
            )
            changed += 1
        db.connection.commit()
        print(f"[noise-strip] 已更新 {changed} 条")

    if args.enqueue:
        rows = cur.execute(
            """
            SELECT a.id FROM articles a
            WHERE a.status='active'
              AND length(coalesce(a.content,'')) >= 60
              AND NOT EXISTS (
                  SELECT 1 FROM article_derivatives d
                  WHERE d.article_id = a.id
                    AND d.summary IS NOT NULL AND d.summary != ''
                    AND d.status = 'completed'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM intel_jobs j
                  WHERE j.dedupe_key = 'enrich:' || a.id
              )
            ORDER BY a.id DESC LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        print(f"[enqueue] 待补精炼 {len(rows)} 条（新文优先）")
        queued = 0
        for row in rows:
            aid = row["id"]
            art = db.get_article_by_id(aid) or {}
            try:
                _, created = repo.enqueue_job(
                    "enrich",
                    f"enrich:{aid}",
                    {
                        "article_id": int(aid),
                        "url": str(art.get("url") or ""),
                        "title": str(art.get("title") or ""),
                        "content": str(art.get("content") or ""),
                        "keywords": [],
                        "task_id": str(art.get("source_task_id") or ""),
                    },
                    priority=-10,
                )
                queued += 1 if created else 0
            except Exception as exc:
                print(f"  ⚠️ 文章 {aid} 入队失败: {exc}")
        print(f"[enqueue] 新入队 {queued} 条")

    cur.close()


if __name__ == "__main__":
    main()

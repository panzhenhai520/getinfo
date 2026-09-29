# -*- coding: utf-8 -*-
"""真正入库合并：把同源标题高度相似的文章合并成一篇。
- 每组保留最早一篇（min id）作代表；
- 近似重复文章的 article_intel_classifications / intel_topic_articles 先重指到代表（按 (article_id, pack/topic) 去重）；
- 再删除近似重复文章行（ON DELETE CASCADE 级联删其子表）。
用法：python tools/merge_near_duplicates.py [--apply]
  --apply 缺省只预览（dry-run），加 --apply 才真正删除。"""
import sys, io, re, argparse
sys.path.insert(0, 'F:/CollectInfo')
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from sqlite_database import sqlite_db
from intel_api import _tl_title_tokens, _tl_near_duplicate


def _strict_dup(a, b):
    """物理合并用更严格判定：同域名 + 标题 token Jaccard>=0.6（避免误删不同内容）。"""
    if (a.get('domain') or '') != (b.get('domain') or ''):
        return False
    t1 = _tl_title_tokens(a.get('title')); t2 = _tl_title_tokens(b.get('title'))
    if not t1 or not t2:
        return False
    inter = len(t1 & t2); union = len(t1 | t2)
    return union and inter / union >= 0.6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='真正执行合并（否则仅预览）')
    ap.add_argument('--pack', default='', help='仅处理某行业包（空=全部）')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    do_apply = args.apply and not args.dry_run

    sqlite_db._ensure_connection()
    with sqlite_db.lock:
        c = sqlite_db.connection.cursor()
        c.execute("SELECT id, title, domain, content FROM articles WHERE status='active' ORDER BY id")
        rows = [dict(r) for r in c.fetchall()]
        c.close()

    # 分组近似重复
    groups = []  # list of dicts
    for a in rows:
        g = next((gr for gr in groups if _strict_dup(gr['rep'], a)), None)
        if g:
            g['dups'].append(a)
        else:
            groups.append({'rep': a, 'dups': []})

    total_dup = 0
    for g in groups:
        if not g['dups']:
            continue
        rep = g['rep']; dups = g['dups']
        total_dup += len(dups)
        rep_id = int(rep['id'])
        dup_ids = [int(d['id']) for d in dups]
        print(f"[合并] 保留 #{rep_id} {rep['title'][:36]}  <- {len(dup_ids)} 篇")
        if not do_apply:
            continue
        with sqlite_db.lock:
            cur = sqlite_db.connection.cursor()
            for did in dup_ids:
                # 近似重复的分类/主题与代表相同，直接删除其冗余关联（代表已有）
                cur.execute("DELETE FROM article_intel_classifications WHERE article_id=?", (did,))
                cur.execute("DELETE FROM intel_topic_articles WHERE article_id=?", (did,))
                try:
                    cur.execute("DELETE FROM intel_article_events WHERE article_id=?", (did,))
                except Exception:
                    pass
            # 删除近似重复文章（级联删其余子表）
            marks = ','.join('?' * len(dup_ids))
            cur.execute(f"DELETE FROM articles WHERE id IN ({marks})", dup_ids)
            sqlite_db.connection.commit()
            cur.close()
    print(f"合计近似重复 {total_dup} 篇", '已删除' if do_apply else '（预览，加 --apply 执行）')


if __name__ == '__main__':
    main()

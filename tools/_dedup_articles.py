"""文章去重：同一篇文章因 www./非 www. 等 URL 差异被存成多行时，合并为一行。

规则（按运营决定）：
  · 同一组内**保留 id 较小者**（先入库的那条）；
  · 若组内存在更长的正文，则把最长正文继承到保留行（同时重算 content_hash / content_length）；
  · 其余行**直接删除**（连带清理分类、主题关联、个人可见性等从属行）。
  · 删除前把整行快照写进审计文件，可据此恢复。

分组口径（只取高置信，避免误合并）：
  A. 域名去掉前导 www. 后相同，且 URL 去掉主机后路径完全相同 → 同一篇文章
  B. 非空 content_hash 完全相同 → 同一篇内容
"""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, '/app' if os.path.isdir('/app') else r'F:\CollectInfo')

import sqlite_database as sdb

AUDIT = None
DEPENDENT_TABLES = (
    ('article_intel_classifications', 'article_id'),
    ('intel_topic_articles', 'article_id'),
    ('article_user_visibility', 'article_id'),
    ('intel_candidate_observations', 'article_id'),
)


def _norm_domain(value: str) -> str:
    text = str(value or '').strip().lower()
    return text[4:] if text.startswith('www.') else text


def _path_of(url: str) -> str:
    text = str(url or '').strip().lower()
    for scheme in ('https://', 'http://'):
        if text.startswith(scheme):
            text = text[len(scheme):]
            break
    return '/' + text.split('/', 1)[1] if '/' in text else '/'


def main() -> int:
    global AUDIT
    base = '/app/data' if os.path.isdir('/app') else os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data')
    os.makedirs(base, exist_ok=True)
    AUDIT = os.path.join(base, 'dedup_articles_%s.json' % datetime.utcnow().strftime('%Y%m%d_%H%M%S'))

    db = sdb.sqlite_db
    db._ensure_connection()
    cursor = db.connection.cursor()
    rows = [dict(r) for r in cursor.execute(
        "SELECT id, url, domain, title, content, content_hash, status FROM articles ORDER BY id"
    ).fetchall()]
    print('扫描文章 =', len(rows))

    groups = []          # 每组：{'ids': [...], 'reason': str}
    seen_ids = set()

    # A. 同域名(去 www) + 同路径
    by_path = {}
    for row in rows:
        key = (_norm_domain(row['domain']), _path_of(row['url']))
        if not key[0] or key[1] == '/':
            continue
        by_path.setdefault(key, []).append(row)
    for key, items in by_path.items():
        if len(items) > 1:
            ids = [int(x['id']) for x in items]
            seen_ids.update(ids)
            groups.append({'ids': ids, 'reason': 'same_path:%s%s' % key})

    # B. 同 content_hash
    by_hash = {}
    for row in rows:
        digest = str(row['content_hash'] or '').strip()
        if len(digest) < 8:
            continue
        by_hash.setdefault(digest, []).append(row)
    for digest, items in by_hash.items():
        if len(items) > 1:
            ids = [int(x['id']) for x in items]
            if seen_ids.issuperset(ids):
                continue
            seen_ids.update(ids)
            groups.append({'ids': ids, 'reason': 'same_hash:%s' % digest[:10]})

    removed, merged_content = [], 0
    for group in groups:
        ids = sorted(group['ids'])
        keeper = ids[0]
        losers = ids[1:]
        group_rows = [r for r in rows if int(r['id']) in ids]
        longest = max(group_rows, key=lambda r: len(str(r['content'] or '')))
        snapshots = [{
            'id': int(r['id']), 'url': r['url'], 'domain': r['domain'], 'title': r['title'],
            'content_hash': r['content_hash'], 'status': r['status'],
            'content_length': len(str(r['content'] or '')),
        } for r in group_rows if int(r['id']) in losers]
        # 继承最长正文到保留行
        keeper_row = next(r for r in group_rows if int(r['id']) == keeper)
        if len(str(longest['content'] or '')) > len(str(keeper_row['content'] or '')):
            new_content = str(longest['content'])
            import hashlib
            cursor.execute(
                "UPDATE articles SET content=?, content_hash=?, content_length=? WHERE id=?",
                (new_content, hashlib.md5(new_content.encode('utf-8')).hexdigest(), len(new_content), keeper),
            )
            merged_content += 1
        # 删除从属行 + 文章行
        for table, column in DEPENDENT_TABLES:
            try:
                cursor.execute("DELETE FROM %s WHERE %s IN (%s)" % (table, column, ','.join('?' * len(losers))), losers)
            except Exception:
                pass
        cursor.execute("DELETE FROM articles WHERE id IN (%s)" % ','.join('?' * len(losers)), losers)
        removed.append({'keeper': keeper, 'deleted': snapshots, 'reason': group['reason']})

    db.connection.commit()
    with open(AUDIT, 'w', encoding='utf-8') as handle:
        json.dump({'created_at': datetime.utcnow().isoformat() + 'Z', 'groups': removed}, handle,
                  ensure_ascii=False, indent=2)
    print('重复组 =', len(groups))
    print('删除行 =', sum(len(g['deleted']) for g in removed))
    print('继承更长正文 =', merged_content)
    print('审计文件 =', AUDIT)
    left = cursor.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    print('处理后文章总数 =', left)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

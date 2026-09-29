"""一次性回填：把已入库的“空心页/列表页”文章标为 removed（软删除，可依审计文件恢复）。

判定（复用生产质量门禁 + 第二层空心判定，避免误伤含链接的真文章）：
  - intel_content_quality_gate.assess_article_quality 的硬问题：
    content_too_short / content_is_link_directory / empty_content / no_real_paragraphs
  - 第二层：content<200 且 raw_content<100（维护通知、页面不存在、机构概况等空心页）

审计文件写到 data/hollow_articles_backfill_<UTC日期>.json，含 id + 原因 + 标题，便于回滚。
"""
import sys, os, json, datetime
sys.path.insert(0, '/app')
from intel_content_quality_gate import assess_article_quality as assess
import sqlite_database as sdb

HARD = {'content_too_short', 'content_is_link_directory', 'empty_content', 'no_real_paragraphs'}
AUDIT = '/app/data/hollow_articles_backfill_%s.json' % datetime.datetime.utcnow().strftime('%Y%m%d')

db = sdb.sqlite_db
db._ensure_connection()
cur = db.connection.cursor()
rows = cur.execute("SELECT id,title,content,raw_content FROM articles WHERE status='active'").fetchall()

hit = []
for r in rows:
    d = dict(r)
    c = str(d.get('content') or ''); raw = str(d.get('raw_content') or ''); t = str(d.get('title') or '')
    hard = [i for i in assess({'content': c, 'title': t})['issues'] if i in HARD]
    why = ','.join(hard) if hard else ('layer2-hollow' if (len(c.strip()) < 200 and len(raw.strip()) < 100) else None)
    if why:
        hit.append({'id': int(d['id']), 'reason': why, 'title': t[:120],
                    'content_len': len(c), 'raw_len': len(raw)})

with open(AUDIT, 'w', encoding='utf-8') as fh:
    json.dump({'created_at': datetime.datetime.utcnow().isoformat() + 'Z',
               'rule': 'quality_gate_hard_issues + layer2_hollow', 'articles': hit},
              fh, ensure_ascii=False, indent=2)

ids = [h['id'] for h in hit]
if ids:
    with db.lock:
        cur.execute("UPDATE articles SET status='removed', updated_at=datetime('now') WHERE id IN (%s)"
                    % ','.join('?' * len(ids)), ids)
        db.connection.commit()

after = dict((r[0], r[1]) for r in cur.execute('SELECT status,COUNT(*) FROM articles GROUP BY status').fetchall())
print('待处理命中=%d 已置为 removed；审计文件=%s' % (len(ids), AUDIT))
print('回填后 status 分布:', after)

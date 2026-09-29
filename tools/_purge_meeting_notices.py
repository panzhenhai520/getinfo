"""一次性清理：把已入库的"会议/活动通知页"标为 removed（软删除，可依审计文件恢复）。

判定复用生产质量门禁 intel_content_quality_gate.looks_like_meeting_notice：
命中 ≥3 个会议专用字段（会议时间/主办单位/议程/报名回执…），
或命中 ≥2 个且全文很短、几乎没有句子（典型通知体的紧凑字段块）。

这类页面的唯一日期往往是**未来的会议时间**，会被日期抽取误当成新文章入库。
审计文件写到 data/meeting_notice_purge_<UTC日期>.json，含 id + 标题，便于回滚。
"""
import sys, json, datetime
sys.path.insert(0, '/app')
from intel_content_quality_gate import looks_like_meeting_notice
import sqlite_database as sdb

AUDIT = '/app/data/meeting_notice_purge_%s.json' % datetime.datetime.utcnow().strftime('%Y%m%d')

db = sdb.sqlite_db
db._ensure_connection()
cur = db.connection.cursor()
rows = cur.execute("SELECT id,title,content FROM articles WHERE status='active'").fetchall()

hit = []
for r in rows:
    d = dict(r)
    title = str(d.get('title') or '')
    content = str(d.get('content') or '')
    if looks_like_meeting_notice(content, title):
        hit.append({'id': int(d['id']), 'title': title[:120], 'content_len': len(content)})

with open(AUDIT, 'w', encoding='utf-8') as fh:
    json.dump({'created_at': datetime.datetime.utcnow().isoformat() + 'Z',
               'rule': 'looks_like_meeting_notice', 'articles': hit},
              fh, ensure_ascii=False, indent=2)

ids = [h['id'] for h in hit]
if ids:
    with db.lock:
        cur.execute("UPDATE articles SET status='removed', updated_at=datetime('now') WHERE id IN (%s)"
                    % ','.join('?' * len(ids)), ids)
        db.connection.commit()

after = dict((r[0], r[1]) for r in cur.execute('SELECT status,COUNT(*) FROM articles GROUP BY status').fetchall())
print('命中会议通知页=%d 已置为 removed；审计文件=%s' % (len(ids), AUDIT))
print('清理后 status 分布:', after)

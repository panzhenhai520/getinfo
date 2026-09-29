"""一次性升级：重新抓取“真报告”行；站点提供 PDF 下载的则入库为真 PDF。

只处理未被标记为 page_kind=listing 的行；upload:// 本地文件跳过。
进度写入 /tmp/report_upgrade.log。
"""
import sys, os, json, time
sys.path.insert(0, '/app')
import sqlite_database as sdb
from intel_reports import intel_report_service as svc, LISTING_PAGE_META

db = sdb.sqlite_db
db._ensure_connection()
cur = db.connection.cursor()
rows = cur.execute(
    "SELECT id,title,report_url,industry_pack_id FROM intel_reports "
    "WHERE COALESCE(metadata_json,'') NOT LIKE ? ORDER BY id",
    ('%' + LISTING_PAGE_META + '%',)
).fetchall()

log = open('/tmp/report_upgrade.log', 'w', buffering=1)
stat = {'total': len(rows), 'pdf': 0, 'html': 0, 'unchanged': 0, 'failed': 0, 'skipped': 0}
log.write(f'start rows={len(rows)}\n')

for i, (rid, title, url, pack) in enumerate(rows, 1):
    if str(url or '').startswith('upload://'):
        stat['skipped'] += 1
        continue
    before = json.loads((cur.execute('SELECT metadata_json FROM intel_reports WHERE id=?', (rid,)).fetchone()[0]) or '{}')
    try:
        out = svc.ingest(url, pack, source_id=None, title=title or '')
        after = json.loads((cur.execute('SELECT metadata_json FROM intel_reports WHERE id=?', (rid,)).fetchone()[0]) or '{}')
        fmt = after.get('original_format')
        if fmt == 'pdf':
            stat['pdf'] += 1
        elif out.get('status') == 'unchanged':
            stat['unchanged'] += 1
        else:
            stat['html'] += 1
        log.write(f"[{i}/{len(rows)}] id={rid} {out.get('status')} fmt={fmt} {str(title)[:34]}\n")
    except Exception as exc:
        stat['failed'] += 1
        log.write(f"[{i}/{len(rows)}] id={rid} FAIL {type(exc).__name__}: {str(exc)[:90]}\n")
    time.sleep(0.3)

log.write('DONE ' + json.dumps(stat, ensure_ascii=False) + '\n')
log.close()
print(json.dumps(stat, ensure_ascii=False))

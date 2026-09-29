"""一次性回填：把已入库的“栏目/目录/列表页”标记为 page_kind=listing，使其不再出现在报告列表。

判据与 IntelReportService._looks_like_report_listing 一致（标题栏目式 / 链接占比≥0.5）。
标记写入 metadata_json，可随时清除以恢复。
"""
import sys, os, json, re
sys.path.insert(0, '/app')
import sqlite_database as sdb
from intel_reports import IntelReportService, LISTING_PAGE_META

db = sdb.sqlite_db
db._ensure_connection()
cur = db.connection.cursor()
rows = cur.execute('SELECT id,title,resolved_url,report_url,local_path,metadata_json FROM intel_reports ORDER BY id').fetchall()

marked, kept, skipped = [], 0, 0
for rid, title, res, rep, local, meta_raw in rows:
    try:
        meta = json.loads(meta_raw or '{}')
    except (TypeError, ValueError):
        meta = {}
    if meta.get('page_kind') == 'listing':
        skipped += 1
        continue
    if local and os.path.exists(local) and local.endswith('.html'):
        raw = open(local, 'rb').read()
    else:
        raw = b''          # 原生PDF：不会是目录页
    if raw and IntelReportService._looks_like_report_listing(raw, title or ''):
        meta['page_kind'] = 'listing'
        cur.execute('UPDATE intel_reports SET metadata_json=?, updated_at=datetime(\'now\') WHERE id=?',
                    (json.dumps(meta, ensure_ascii=False), rid))
        marked.append(rid)
    else:
        kept += 1

db.connection.commit()
print(f'总行数={len(rows)} 新标记目录页={len(marked)} 保留真报告={kept} 已标记跳过={skipped}')
print('标记ID(前40):', marked[:40])

total = cur.execute('SELECT COUNT(*) FROM intel_reports').fetchone()[0]
visible = cur.execute(
    "SELECT COUNT(*) FROM intel_reports WHERE COALESCE(metadata_json,'') NOT LIKE ?",
    ('%' + LISTING_PAGE_META + '%',)).fetchone()[0]
print(f'库内总数={total} 列表可见(排除目录页)={visible}')

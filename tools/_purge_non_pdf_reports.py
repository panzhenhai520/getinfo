"""一次性清理：删除“不合格”的报告行，只保留符合列表白名单的报告。

合格 = metadata.original_format == 'pdf'  且  local_path 文件存在并带 %PDF 魔数
       且  报告标题/开头命中当前行业包的主题锚点词（与报告列表白名单完全一致）

删除文件时只动报告存储目录内的 .html/.htm/.md/.pdf，且**同一份文件若仍被其它报告行引用则不删**。
"""
import sys, os, json

APP_DIR = os.getenv('APP_DIR') or '/app'
sys.path.insert(0, APP_DIR)
import sqlite_database as sdb  # noqa: E402
from industry_packs import industry_pack_loader  # noqa: E402
from intel_reports import REPORT_TOPIC_HEADING_CHARS, report_matches_industry_topic  # noqa: E402

REPORT_DIR = os.path.abspath(os.getenv('INTEL_REPORT_STORAGE_DIR', 'data/intel_reports'))

db = sdb.sqlite_db
db._ensure_connection()
cur = db.connection.cursor()
rows = cur.execute('SELECT id,title,industry_pack_id,metadata_json,local_path FROM intel_reports').fetchall()

pack_cache = {}


def pack_of(pack_id):
    if pack_id not in pack_cache:
        try:
            pack_cache[pack_id] = industry_pack_loader.load(pack_id)
        except Exception:
            pack_cache[pack_id] = {}
    return pack_cache[pack_id]


def is_qualified(row, meta):
    if str(meta.get('original_format') or '').casefold() != 'pdf':
        return False
    try:
        with open(str(row.get('local_path') or ''), 'rb') as handle:
            if handle.read(4) != b'%PDF':
                return False
    except OSError:
        return False
    heading = ''
    markdown_path = str(meta.get('markdown_path') or '')
    if markdown_path and os.path.exists(markdown_path):
        try:
            with open(markdown_path, 'r', encoding='utf-8', errors='replace') as handle:
                heading = handle.read(REPORT_TOPIC_HEADING_CHARS)
        except OSError:
            pass
    return report_matches_industry_topic(pack_of(str(row.get('industry_pack_id') or '')), row.get('title') or '', heading)


keep, drop = [], []
for item in rows:
    record = dict(item)
    try:
        meta = json.loads(record.get('metadata_json') or '{}')
    except (TypeError, ValueError):
        meta = {}
    (keep if is_qualified(record, meta) else drop).append((record, meta))

# 删除行
ids = [record['id'] for record, _ in drop]
for start in range(0, len(ids), 200):
    chunk = ids[start:start + 200]
    with db.lock:
        cur.execute('DELETE FROM intel_reports WHERE id IN (%s)' % ','.join('?' * len(chunk)), chunk)
        db.connection.commit()

# 删除文件：只删报告目录内的文件，且不再被任何存活行引用
survivors = set()
for row in cur.execute('SELECT local_path, metadata_json FROM intel_reports').fetchall():
    record = dict(row)
    if record.get('local_path'):
        survivors.add(os.path.abspath(str(record['local_path'])))
    try:
        survivors.add(os.path.abspath(str(json.loads(record.get('metadata_json') or '{}').get('markdown_path') or '')))
    except (TypeError, ValueError):
        pass

removed_files, freed = 0, 0
for record, meta in drop:
    for raw_path in (record.get('local_path'), meta.get('markdown_path')):
        path = str(raw_path or '')
        if not path:
            continue
        absolute = os.path.abspath(path)
        if not absolute.startswith(REPORT_DIR) or absolute in survivors:
            continue
        if not absolute.lower().endswith(('.html', '.htm', '.md', '.pdf')):
            continue
        try:
            freed += os.path.getsize(absolute)
            os.remove(absolute)
            removed_files += 1
        except OSError:
            pass

after = cur.execute('SELECT COUNT(*) FROM intel_reports').fetchone()[0]
print('保留(正式+符合行业包)=%d  删除不合格=%d  清理文件=%d  释放=%.1f MB  剩余总数=%d'
      % (len(keep), len(ids), removed_files, freed / 1048576, after))

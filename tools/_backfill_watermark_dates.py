"""一次性回填：把已删除的"无日期"文章按信源水位线规则恢复入库。

规则与 sqlite_database.insert_article 的补救通道保持一致：
同一域名下的 URL 经去重后仍是新链接，说明发布时间落在
(上次获取时间, 本次获取时间] 区间内，因此用本次聚合日期近似发布时间。
first_crawled 由 datetime('now') 写入（UTC），输出为中国时区的日期。

分档：
  gap ≤ 3 天   → crawl_watermark:high
  4 ~ 30 天    → crawl_watermark:medium
  > 30 天      → 不推断（保持删除，区间太宽）
  无基线       → 不推断（该信源首次抓取，没有可比较的基线文章）
  first_crawled 距今 > 365 天 → 不回填（不绕过 1 年时效闸门）

审计文件写到 data/watermark_backfill_<UTC日期>.json，含 id/标题/域名/近似日期/间隔/置信度，
回滚方式：把这些 id 重新置为 status='removed' 并清空 publish_date/published_time_source/published_precision。
"""
import sys, json, datetime
sys.path.insert(0, '/app')
import sqlite_database as sdb

AUDIT = '/app/data/watermark_backfill_%s.json' % datetime.datetime.utcnow().strftime('%Y%m%d')
CN = datetime.timezone(datetime.timedelta(hours=8))


def _parse(value):
    """解析 first_crawled；无时区的按 UTC 处理（与写入口的 _source_crawl_gap_days 一致）。"""
    text = str(value or '').strip().replace('Z', '+00:00').replace(' ', 'T')
    parsed = None
    for fmt in (None, '%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d'):
        try:
            parsed = datetime.datetime.fromisoformat(text) if fmt is None else datetime.datetime.strptime(text[:10], fmt)
            break
        except (TypeError, ValueError):
            parsed = None
    if parsed is None:
        return None
    return parsed.replace(tzinfo=datetime.timezone.utc) if parsed.tzinfo is None else parsed


db = sdb.sqlite_db
db._ensure_connection()
cur = db.connection.cursor()
now = datetime.datetime.now(datetime.timezone.utc)

# 每个域名的抓取时间序列 → 该文章上一次抓取该域名的时间（水位线基线）
by_domain = {}
for row in cur.execute('SELECT id, domain, first_crawled FROM articles').fetchall():
    stamp = _parse(row['first_crawled'])
    if stamp:
        by_domain.setdefault(str(row['domain'] or '').lower(), []).append((stamp, int(row['id'])))
previous = {}
for domain, items in by_domain.items():
    items.sort()
    for index, (stamp, article_id) in enumerate(items):
        if index > 0:
            previous[article_id] = items[index - 1][0]

targets = cur.execute(
    "SELECT id, domain, first_crawled, title FROM articles "
    "WHERE status='removed' AND COALESCE(publish_date,'')=''"
).fetchall()

restored, skipped = [], {'无基线': 0, '超过30天': 0, '超365天': 0}
for row in targets:
    stamp = _parse(row['first_crawled'])
    if stamp is None:
        skipped['无基线'] += 1
        continue
    base = previous.get(int(row['id']))
    if base is None:
        skipped['无基线'] += 1
        continue
    gap = (stamp - base).days
    if gap > 30:
        skipped['超过30天'] += 1
        continue
    if (now - stamp).days > 365:
        skipped['超365天'] += 1
        continue
    restored.append({
        'id': int(row['id']),
        'domain': str(row['domain'] or ''),
        'title': str(row['title'] or '')[:120],
        'publish_date': stamp.astimezone(CN).strftime('%Y-%m-%d'),
        'gap_days': gap,
        'source': 'crawl_watermark:%s' % ('high' if gap <= 3 else 'medium'),
    })

with open(AUDIT, 'w', encoding='utf-8') as fh:
    json.dump({'created_at': datetime.datetime.utcnow().isoformat() + 'Z',
               'rule': 'source_crawl_watermark (insert_article fallback)',
               'restored': restored, 'skipped': skipped}, fh, ensure_ascii=False, indent=2)

if restored:
    with db.lock:
        for item in restored:
            cur.execute(
                "UPDATE articles SET status='active', publish_date=?, published_precision='day', "
                "published_time_source=?, updated_at=datetime('now') WHERE id=?",
                (item['publish_date'], item['source'], item['id'])
            )
        db.connection.commit()

after = dict((r[0], r[1]) for r in cur.execute('SELECT status,COUNT(*) FROM articles GROUP BY status').fetchall())
print('回填恢复 %d 篇；跳过 %s；审计文件=%s' % (len(restored), skipped, AUDIT))
print('回填后 status 分布:', after)

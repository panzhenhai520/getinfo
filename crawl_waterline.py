# -*- coding: utf-8 -*-

"""
智能增量爬取一期：水位线（high-water mark）模块。

解决的问题：
1. 爬过的不再爬：crawl_item_seen 记录每个条目的内容指纹（URL+标题），
   列表页发现阶段直接拦截，连详情页请求都不发；
2. 没有新的就没有必要爬：crawl_waterline 记录每个目标列表 URL 的上次爬取时间、
   已见最新文章发布时间、列表头部条目指纹；
3. 新→旧遍历 + 连续命中停止：列表条目按发现顺序（新→旧）遍历，
   连续命中 K 个「已爬过/已保存」条目即停止，不再往后翻页/发详情请求；
   乱序插入容错：命中后出现新条目会重置连续计数（重叠窗口思路，
   参考 feder-cr/invisible_playwright 的 high-water mark 方案）；
4. 时间短路径：条目自带发布时间且早于水位线时间 → 直接判为旧文丢弃，
   杜绝「新时间爬出老文章」。

说明（一期边界）：sitemap lastmod / RSS pubDate / ETag 304 等「零请求跳过」
信号属二期/三期；一期保证列表页阶段即拦截已见条目并提前停止。
"""

import hashlib
import json
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlite_database import SQLiteDatabase

# 连续命中多少条「已保存」条目即停止（乱序插入容错窗口）
STOP_AFTER_KNOWN_RUN = 3
# 水位线头部指纹数量（覆盖列表首页）
TOP_FINGERPRINT_COUNT = 20

_TRACKING_PARAMS = {'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
                    'spm', 'from', 'ref', 'refer', 'share_token'}


def normalize_list_url(url: str) -> str:
    """列表 URL 归一化：去 fragment、排序查询参数、去跟踪参数、去尾部斜杠。"""
    raw = str(url or '').strip()
    if not raw:
        return ''
    parsed = urlparse(raw)
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if k.lower() not in _TRACKING_PARAMS]
    query.sort()
    path = parsed.path.rstrip('/') or '/'
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, '', urlencode(query), ''))


def normalize_item_url(url: str) -> str:
    """文章 URL 归一化：去 fragment 与跟踪参数（用于指纹，避免同一文章换参重爬）。"""
    raw = str(url or '').strip()
    if not raw:
        return ''
    parsed = urlparse(raw)
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if k.lower() not in _TRACKING_PARAMS]
    query.sort()
    path = parsed.path.rstrip('/') or '/'
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, '', urlencode(query), ''))


def item_fingerprint(url: str, title: str = '') -> str:
    """条目内容指纹：归一化 URL + 归一化标题 的 sha256。

    标题参与指纹的意义：同 URL 标题变化视为内容被编辑（重抓）；URL 参与的意义：
    不同 URL 必然不同条目。
    """
    normalized_title = re.sub(r'\s+', '', str(title or ''))[:200]
    key = f"{normalize_item_url(url)}|{normalized_title}"
    return hashlib.sha256(key.encode('utf-8')).hexdigest()


def _now_text() -> str:
    from utils import get_china_time
    return get_china_time().strftime('%Y-%m-%d %H:%M:%S')


# ----------------------------------------------------------------------
# 建表
# ----------------------------------------------------------------------
def _ensure_tables(cursor) -> None:
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS crawl_waterline ("
        "  list_url TEXT PRIMARY KEY,"
        "  domain TEXT NOT NULL DEFAULT '',"
        "  last_crawl_at TEXT NOT NULL DEFAULT '',"
        "  last_new_count INTEGER NOT NULL DEFAULT 0,"
        "  last_max_publish_time TEXT NOT NULL DEFAULT '',"
        "  top_item_fingerprints TEXT NOT NULL DEFAULT '[]',"
        "  etag TEXT NOT NULL DEFAULT '',"
        "  last_modified TEXT NOT NULL DEFAULT '',"
        "  updated_at TEXT NOT NULL DEFAULT ''"
        ")"
    )
    # 老库补列：etag/last_modified（T3.2 条件请求校验头）
    for col, definition in (("etag", "TEXT NOT NULL DEFAULT ''"),
                            ("last_modified", "TEXT NOT NULL DEFAULT ''")):
        try:
            cursor.execute(f"ALTER TABLE crawl_waterline ADD COLUMN IF NOT EXISTS {col} {definition}")
        except Exception:
            try:
                cursor.execute(f"ALTER TABLE crawl_waterline ADD COLUMN {col} {definition}")
            except Exception:
                pass
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS crawl_item_seen ("
        "  item_fingerprint TEXT PRIMARY KEY,"
        "  list_url TEXT NOT NULL DEFAULT '',"
        "  url TEXT NOT NULL DEFAULT '',"
        "  title TEXT NOT NULL DEFAULT '',"
        "  publish_date TEXT NOT NULL DEFAULT '',"
        "  saved INTEGER NOT NULL DEFAULT 0,"
        "  first_seen_at TEXT NOT NULL DEFAULT '',"
        "  last_seen_at TEXT NOT NULL DEFAULT ''"
        ")"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_crawl_item_seen_list ON crawl_item_seen(list_url)"
    )


# ----------------------------------------------------------------------
# 水位线读取
# ----------------------------------------------------------------------
def get_waterline(list_url: str, db: Optional[SQLiteDatabase] = None) -> Optional[Dict]:
    """读取目标列表 URL 的水位线；从未爬过返回 None。"""
    db = db or _default_db()
    key = normalize_list_url(list_url)
    if not key:
        return None
    db._ensure_connection()
    with db.lock:
        cursor = db.connection.cursor()
        try:
            _ensure_tables(cursor)
            cursor.execute("SELECT * FROM crawl_waterline WHERE list_url=?", (key,))
            row = cursor.fetchone()
        finally:
            cursor.close()
    if not row:
        return None
    data = dict(row)
    try:
        data['top_item_fingerprints'] = json.loads(str(data.get('top_item_fingerprints') or '[]'))
    except (TypeError, ValueError):
        data['top_item_fingerprints'] = []
    return data


def _saved_fingerprints(list_url: str, db: SQLiteDatabase) -> Dict[str, Dict]:
    """全库已保存条目指纹 → 行（「爬过不再爬」全局生效，跨列表页/跨入口）。"""
    db._ensure_connection()
    with db.lock:
        cursor = db.connection.cursor()
        try:
            _ensure_tables(cursor)
            cursor.execute("SELECT * FROM crawl_item_seen WHERE saved=1")
            rows = [dict(r) for r in cursor.fetchall()]
        finally:
            cursor.close()
    return {str(r.get('item_fingerprint') or ''): r for r in rows}


# ----------------------------------------------------------------------
# 新条目判定（列表阶段：新→旧遍历 + 连续命中停止 + 日期短路径）
# ----------------------------------------------------------------------
def classify_new_items(
    list_url: str,
    links: List[Dict],
    *,
    db: Optional[SQLiteDatabase] = None,
    stop_after: int = STOP_AFTER_KNOWN_RUN,
) -> Dict:
    """对列表页发现的条目做水位线过滤。

    返回：
    {
      'keep': [link, ...],            # 需要继续爬详情的新条目（保持原顺序）
      'seen_skipped': int,            # 因「爬过已保存」被跳过的条目数
      'stale_dropped': int,           # 因发布时间早于水位线被丢弃的旧条目数
      'stop_index': int,              # 触发停止的位置（未触发为 -1）
      'known_run': int,               # 停止时连续命中数
    }
    规则：按 links 顺序（新→旧）遍历；命中「已保存」→ 计数并跳过；
    连续命中 stop_after 条 → 停止丢弃剩余；条目发布时间早于水位线时间 → 直接丢弃。
    """
    db = db or _default_db()
    key = normalize_list_url(list_url)
    if not key:
        return {'keep': list(links), 'seen_skipped': 0, 'stale_dropped': 0,
                'stop_index': -1, 'known_run': 0}
    waterline = get_waterline(list_url, db=db)
    saved_map = _saved_fingerprints(list_url, db) if waterline is not None else {}
    last_max_time = str((waterline or {}).get('last_max_publish_time') or '')[:10]

    keep: List[Dict] = []
    seen_skipped = 0
    stale_dropped = 0
    known_run = 0
    stop_index = -1
    for index, link in enumerate(links):
        fp = item_fingerprint(str(link.get('url') or ''), str(link.get('title') or link.get('text') or ''))
        if fp in saved_map:
            seen_skipped += 1
            known_run += 1
            if known_run >= int(stop_after):
                stop_index = index
                break
            continue
        # 日期短路径：条目自带发布时间且早于水位线 → 旧文，丢弃（不重爬、不入实时流）
        publish_date = str(link.get('publish_date') or link.get('candidate_publish_date') or '')[:10]
        if last_max_time and publish_date and publish_date < last_max_time:
            stale_dropped += 1
            known_run += 1
            if known_run >= int(stop_after):
                stop_index = index
                break
            continue
        known_run = 0
        keep.append(link)
    return {
        'keep': keep,
        'seen_skipped': seen_skipped,
        'stale_dropped': stale_dropped,
        'stop_index': stop_index,
        'known_run': known_run,
    }


# ----------------------------------------------------------------------
# 水位线推进（只有成功入库才推进 max 时间与头部指纹）
# ----------------------------------------------------------------------
def mark_items_seen(list_url: str, links: List[Dict], db: Optional[SQLiteDatabase] = None) -> int:
    """记录本轮在列表页见过的条目（saved 状态保留，不推进水位线时间）。"""
    db = db or _default_db()
    key = normalize_list_url(list_url)
    if not key or not links:
        return 0
    now = _now_text()
    db._ensure_connection()
    inserted = 0
    with db.lock:
        cursor = db.connection.cursor()
        try:
            _ensure_tables(cursor)
            for link in links:
                url = str(link.get('url') or '')
                if not url:
                    continue
                fp = item_fingerprint(url, str(link.get('title') or link.get('text') or ''))
                publish_date = str(link.get('publish_date') or link.get('candidate_publish_date') or '')[:10]
                cursor.execute(
                    "INSERT INTO crawl_item_seen(item_fingerprint, list_url, url, title, publish_date,"
                    " saved, first_seen_at, last_seen_at) VALUES(?,?,?,?,?,0,?,?) "
                    "ON CONFLICT(item_fingerprint) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                    (fp, key, url, str(link.get('title') or link.get('text') or '')[:300],
                     publish_date, now, now),
                )
                inserted += 1
            db.connection.commit()
        finally:
            cursor.close()
    return inserted


def mark_items_saved(
    list_url: str,
    articles: List[Dict],
    *,
    ordered_links: Optional[List[Dict]] = None,
    db: Optional[SQLiteDatabase] = None,
) -> Dict:
    """成功入库后推进水位线：标记 saved=1、更新 last_max_publish_time 与头部指纹。

    只有本次真正保存成功的文章才参与推进；失败/跳过的条目只停留在 seen（下次可重试）。
    """
    db = db or _default_db()
    key = normalize_list_url(list_url)
    if not key:
        return {'saved': 0}
    now = _now_text()
    saved_urls = set()
    max_time = ''
    for article in articles or []:
        url = str(article.get('url') or article.get('link') or '')
        if not url:
            continue
        saved_urls.add(normalize_item_url(url))
        publish = str(article.get('publish_date') or article.get('effective_time') or '')[:10]
        if publish > max_time:
            max_time = publish

    db._ensure_connection()
    with db.lock:
        cursor = db.connection.cursor()
        try:
            _ensure_tables(cursor)
            if saved_urls:
                cursor.execute(
                    "SELECT item_fingerprint, url FROM crawl_item_seen WHERE list_url=?", (key,)
                )
                rows = cursor.fetchall()
                for row in rows:
                    if normalize_item_url(str(row['url'] or '')) in saved_urls:
                        cursor.execute(
                            "UPDATE crawl_item_seen SET saved=1, last_seen_at=? WHERE item_fingerprint=?",
                            (now, str(row['item_fingerprint'])),
                        )
            # 头部指纹：按列表发现顺序取前 N 条
            ordered = ordered_links if ordered_links is not None else []
            top_fps = [
                item_fingerprint(str(link.get('url') or ''), str(link.get('title') or link.get('text') or ''))
                for link in ordered[:TOP_FINGERPRINT_COUNT]
            ]
            previous = get_waterline(list_url, db=db)
            prev_max = str((previous or {}).get('last_max_publish_time') or '')[:10]
            if max_time and (not prev_max or max_time > prev_max):
                new_max = max_time
            else:
                new_max = prev_max
            domain = urlparse(key).netloc
            cursor.execute(
                "INSERT INTO crawl_waterline(list_url, domain, last_crawl_at, last_new_count,"
                " last_max_publish_time, top_item_fingerprints, updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(list_url) DO UPDATE SET last_crawl_at=excluded.last_crawl_at,"
                " last_new_count=excluded.last_new_count,"
                " last_max_publish_time=excluded.last_max_publish_time,"
                " top_item_fingerprints=excluded.top_item_fingerprints, updated_at=excluded.updated_at",
                (key, domain, now, len(saved_urls), new_max,
                 json.dumps(top_fps, ensure_ascii=False), now),
            )
            db.connection.commit()
        finally:
            cursor.close()
    return {'saved': len(saved_urls), 'max_publish_time': new_max if saved_urls else prev_max if previous else ''}


def record_waterline_visit(list_url: str, new_count: int = 0, db: Optional[SQLiteDatabase] = None) -> None:
    """本轮没有新文章（或未抓到任何条目）时，仅更新访问时间与新增数。"""
    db = db or _default_db()
    key = normalize_list_url(list_url)
    if not key:
        return
    now = _now_text()
    db._ensure_connection()
    with db.lock:
        cursor = db.connection.cursor()
        try:
            _ensure_tables(cursor)
            domain = urlparse(key).netloc
            cursor.execute(
                "INSERT INTO crawl_waterline(list_url, domain, last_crawl_at, last_new_count,"
                " last_max_publish_time, top_item_fingerprints, updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(list_url) DO UPDATE SET last_crawl_at=excluded.last_crawl_at,"
                " last_new_count=excluded.last_new_count, updated_at=excluded.updated_at",
                (key, domain, now, int(new_count or 0), '', '[]', now),
            )
            db.connection.commit()
        finally:
            cursor.close()


def filter_stale_signals(links: List[Dict], list_url: str,
                         db: Optional[SQLiteDatabase] = None) -> Dict:
    """T3.1 外部信号（sitemap lastmod / RSS pubDate）增量过滤。

    sitemap/RSS 是站点自报值，只当候选提示不当证据：
    - 来源为 sitemap/feed 且自带日期早于该列表页水位线时间 → 丢弃（旧文）；
    - 无日期的条目保留（交给详情页日期窗口/水位线兜底）。
    """
    db = db or _default_db()
    waterline = get_waterline(list_url, db=db)
    max_time = str((waterline or {}).get('last_max_publish_time') or '')[:10]
    if not max_time:
        return {'keep': list(links), 'stale_dropped': 0}
    keep: List[Dict] = []
    dropped = 0
    for link in links:
        source = str(link.get('source_method') or '').casefold()
        date = str(link.get('publish_date') or link.get('candidate_publish_date') or '')[:10]
        if source in ('sitemap', 'feed') and date and date < max_time:
            dropped += 1
            continue
        keep.append(link)
    return {'keep': keep, 'stale_dropped': dropped}


def update_waterline_validators(list_url: str, etag: str = '', last_modified: str = '',
                                db: Optional[SQLiteDatabase] = None) -> None:
    """保存列表页的 ETag / Last-Modified 校验头（T3.2 条件请求用）。"""
    db = db or _default_db()
    key = normalize_list_url(list_url)
    if not key:
        return
    now = _now_text()
    db._ensure_connection()
    with db.lock:
        cursor = db.connection.cursor()
        try:
            _ensure_tables(cursor)
            domain = urlparse(key).netloc
            cursor.execute(
                "INSERT INTO crawl_waterline(list_url, domain, last_crawl_at, last_new_count,"
                " last_max_publish_time, top_item_fingerprints, etag, last_modified, updated_at)"
                " VALUES(?,?,?,0,'','[]',?,?,?) "
                "ON CONFLICT(list_url) DO UPDATE SET etag=excluded.etag,"
                " last_modified=excluded.last_modified, updated_at=excluded.updated_at",
                (key, domain, now, str(etag or ""), str(last_modified or ""), now),
            )
            db.connection.commit()
        finally:
            cursor.close()


def apply_conditional_get(list_url: str, fetch_fn, db: Optional[SQLiteDatabase] = None) -> Dict:
    """T3.2 条件请求：带 If-None-Match/If-Modified-Since 拉列表页。

    fetch_fn(headers: dict) -> (status, text, resp_headers: dict)，由调用方注入（测试用假实现）。
    返回 {changed: bool, status, text, etag, last_modified}：
      - 304 → changed=False（页面未变，本轮零详情请求）；
      - 200 → changed=True，并把响应校验头写回水位线。
    """
    db = db or _default_db()
    waterline = get_waterline(list_url, db=db) or {}
    headers = {}
    if waterline.get("etag"):
        headers["If-None-Match"] = str(waterline["etag"])
    if waterline.get("last_modified"):
        headers["If-Modified-Since"] = str(waterline["last_modified"])
    status, text, resp_headers = fetch_fn(headers)
    status = int(status or 0)
    if status == 304:
        return {"changed": False, "status": 304, "text": "", "etag": "", "last_modified": ""}
    etag = str((resp_headers or {}).get("etag") or "")
    last_modified = str((resp_headers or {}).get("last-modified") or "")
    if etag or last_modified:
        update_waterline_validators(list_url, etag=etag, last_modified=last_modified, db=db)
    return {"changed": True, "status": status, "text": str(text or ""),
            "etag": etag, "last_modified": last_modified}


# ----------------------------------------------------------------------
# T3.3 调度层零请求跳过（sitemap/RSS 信号不晚于水位线 → 整源跳过）
# ----------------------------------------------------------------------
def should_skip_scan(list_url: str, latest_signal_time: str,
                     db: Optional[SQLiteDatabase] = None) -> bool:
    """信号时间（sitemap lastmod / RSS 最新 pubDate 的最大值）不晚于水位线访问时间
    → 站点没有新内容，可整源跳过（连列表页都不请求）。"""
    signal = str(latest_signal_time or "").strip()
    if not signal:
        return False
    waterline = get_waterline(list_url, db=db)
    if not waterline or not str(waterline.get("last_crawl_at") or "").strip():
        return False
    return signal[:19] <= str(waterline["last_crawl_at"])[:19]


def latest_external_signal(feed_url: str, sitemap_url: str, fetch_fn) -> Optional[str]:
    """拉 RSS + sitemap，返回最新 pubDate/lastmod 时间（取最大）；全失败返回 None。

    fetch_fn(url) -> str（响应文本），由调用方注入（默认 requests 实现见 check_source_no_change）。
    """
    latest = ""
    for url in (feed_url, sitemap_url):
        if not url:
            continue
        try:
            text = str(fetch_fn(url) or "")
        except Exception:
            continue
        dates = re.findall(
            r"<(?:pubDate|lastmod|published|updated|dc:date)>\s*([^<]+?)\s*</",
            text, re.IGNORECASE,
        )
        for raw in dates:
            normalized, _ = _norm_datetime_text(str(raw))
            if normalized and normalized > latest:
                latest = normalized
    return latest or None


def _norm_datetime_text(value: str) -> Tuple[str, str]:
    """把 RSS/sitemap 日期串归一为 YYYY-MM-DD HH:MM:SS（或 YYYY-MM-DD）。"""
    text = str(value or "").strip()
    if not text:
        return "", ""
    parsed = None
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        match = re.match(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
        if match:
            return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}", "date"
        return "", ""
    return (parsed.strftime("%Y-%m-%d %H:%M:%S") if any(c in text for c in (":", "T", " ")) else
            parsed.strftime("%Y-%m-%d")), ("datetime" if any(c in text for c in (":", "T", " ")) else "date")


def check_source_no_change(source: Dict, db: Optional[SQLiteDatabase] = None, fetch_fn=None) -> Dict:
    """信源级零请求跳过判定：拉 RSS/sitemap 信号，不晚于水位线 → skip=True。"""
    db = db or _default_db()
    list_url = str((source or {}).get("source_url") or "").strip()
    if not list_url:
        return {"skip": False, "reason": "no_source_url"}
    metadata = (source or {}).get("metadata") or {}
    feed_url = str(metadata.get("rss_url") or "").strip()
    sitemap_url = ""
    if list_url.startswith("http"):
        try:
            from urllib.parse import urlparse
            sitemap_url = f"{urlparse(list_url).scheme}://{urlparse(list_url).netloc}/sitemap.xml"
        except Exception:
            sitemap_url = ""

    def _default_fetch(url: str) -> str:
        import requests as _requests
        resp = _requests.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; CollectInfo/1.0)"},
                             timeout=10)
        return resp.text or ""

    latest = latest_external_signal(feed_url, sitemap_url, fetch_fn or _default_fetch)
    if should_skip_scan(list_url, latest or "", db=db):
        return {"skip": True, "reason": "skipped_no_change", "latest_signal": latest}
    return {"skip": False, "reason": "", "latest_signal": latest}


# ----------------------------------------------------------------------
# T4.2 水位线可视（信源管理展示 + 手动检查）
# ----------------------------------------------------------------------
def waterline_summary(domain: str = "", db: Optional[SQLiteDatabase] = None) -> List[Dict]:
    """汇总每个列表页的水位线：访问时间 / 上次新增 / 已见最新发布时间 / 已保存条目数。"""
    db = db or _default_db()
    db._ensure_connection()
    with db.lock:
        cursor = db.connection.cursor()
        try:
            _ensure_tables(cursor)
            if domain:
                cursor.execute(
                    "SELECT * FROM crawl_waterline WHERE domain=? ORDER BY last_crawl_at DESC",
                    (str(domain or "").strip().lower(),),
                )
            else:
                cursor.execute("SELECT * FROM crawl_waterline ORDER BY domain, last_crawl_at DESC")
            rows = [dict(r) for r in cursor.fetchall()]
            for row in rows:
                row["saved_item_count"] = int(cursor.execute(
                    "SELECT COUNT(*) FROM crawl_item_seen WHERE list_url=? AND saved=1",
                    (str(row.get("list_url") or ""),),
                ).fetchone()[0])
        finally:
            cursor.close()
    return rows


def _default_db() -> SQLiteDatabase:
    from sqlite_database import sqlite_db
    return sqlite_db

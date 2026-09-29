# -*- coding: utf-8 -*-
"""
文章身份判定与信源水位线工具（从 sqlite_database.py 文件末尾整体平移）。

sqlite_database.py 通过 `from article_identity import ...` 重新导出这些名字，
保持 sqlite_database.xxx 旧引用可访问（零行为变化）。
"""


def _is_implausible_future_date(value) -> bool:
    """日期是否落在未来（容一天时区差）。

    抽取出来的发布日期若晚于今天，说明解析到了错误的时间源——例如把
    "2026年度标准化工作会议"里的会议日期当成了发布日期。这类日期不可采信。
    支持 "YYYY-MM-DD" 以及带时间的 ISO 串。
    """
    from datetime import datetime, timedelta, timezone
    text = str(value or '').strip().replace('Z', '+00:00').replace(' ', 'T')
    if not text:
        return False
    parsed = None
    for fmt in (None, '%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d'):
        try:
            parsed = datetime.fromisoformat(text) if fmt is None else datetime.strptime(text[:10], fmt)
            break
        except (TypeError, ValueError):
            continue
    if parsed is None:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed > datetime.now(timezone.utc) + timedelta(days=90)

_ARTICLE_RETENTION_DAYS = 365


def _normalize_article_url(value) -> str:
    """文章**判重身份**：小写、去前导 www.、去 # 片段。

    只用于判重，不改动存进库的原始 URL（展示链接保持原样）。
    同一个站常同时以 www.zhidx.com 与 zhidx.com 出现，过去按原样比较会被当成两篇，
    于是在文章列表里出现"看起来是同一篇文章的两条"，来源也显示成两个。
    """
    text = str(value or '').strip()
    if not text:
        return ''
    text = text.split('#', 1)[0].lower()
    for scheme in ('https://', 'http://'):
        prefix = scheme + 'www.'
        if text.startswith(prefix):
            text = scheme + text[len(prefix):]
            break
    return text


def _is_stale_publish_date(value, retention_days: int = _ARTICLE_RETENTION_DAYS) -> bool:
    """发布日期是否已超出保留窗口（运营口径：只保留 1 年内的内容）。"""
    from datetime import datetime, timedelta, timezone
    text = str(value or '').strip().replace('Z', '+00:00').replace(' ', 'T')
    if not text:
        return False
    parsed = None
    for fmt in (None, '%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d'):
        try:
            parsed = datetime.fromisoformat(text) if fmt is None else datetime.strptime(text[:10], fmt)
            break
        except (TypeError, ValueError):
            continue
    if parsed is None:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed < datetime.now(timezone.utc) - timedelta(days=retention_days)


# 信源水位线置信度分档：区间越窄，用聚合日期近似发布时间越可信
_WATERMARK_HIGH_GAP_DAYS = 3
_WATERMARK_MAX_GAP_DAYS = 30


def _source_crawl_gap_days(cursor, domain):
    """信源水位线：该域名最近一次获取到文章距今多少天；没有基线可比时返回 None。

    articles 表本身就是水位线记录——同一域名下的 URL 经过 is_article_crawled 去重后
    仍然是新链接，说明它的发布时间落在 (上次获取时间, 本次获取时间] 区间内。
    因此只有该域名此前已抓到过文章，才允许用本次聚合日期近似发布日期：
    区间越窄越可信，区间宽或无基线时不给日期，避免把旧文伪装成新文。
    """
    from datetime import datetime, timezone
    domain = str(domain or '').strip().lower()
    if not domain:
        return None
    try:
        row = cursor.execute(
            "SELECT MAX(first_crawled) FROM articles WHERE LOWER(domain) = ?", (domain,)
        ).fetchone()
    except Exception:
        return None
    raw = str((row[0] if row else '') or '').strip()
    if not raw:
        return None
    text = raw.replace('Z', '+00:00').replace(' ', 'T')
    parsed = None
    for fmt in (None, '%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d'):
        try:
            parsed = datetime.fromisoformat(text) if fmt is None else datetime.strptime(text[:10], fmt)
            break
        except (TypeError, ValueError):
            continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)  # first_crawled 由 datetime('now') 写入，为 UTC
    return max(0, (datetime.now(timezone.utc) - parsed).days)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
工具函数模块
包含时区处理、日期时间等工具函数
"""

from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except ImportError:
    try:
        from backports.zoneinfo import ZoneInfo
    except ImportError:
        import pytz
        ZoneInfo = None


def get_china_time():
    """获取中国时区的当前时间（UTC+8 / Asia/Shanghai）
    
    这个函数确保无论服务器在哪个时区，都返回中国时间。
    用于定时任务的时间比对，避免时区差异导致任务不触发。
    """
    if ZoneInfo is not None:
        # 使用 zoneinfo（Python 3.9+）
        try:
            china_tz = ZoneInfo('Asia/Shanghai')
            return datetime.now(china_tz).replace(tzinfo=None)
        except Exception:
            pass

    # 使用 pytz 作为后备方案
    import pytz
    china_tz = pytz.timezone('Asia/Shanghai')
    return datetime.now(china_tz).replace(tzinfo=None)


def get_china_time_str():
    """获取中国时区的当前时间字符串（用于数据库存储）
    
    返回格式：'YYYY-MM-DD HH:MM:SS'
    """
    return get_china_time().strftime('%Y-%m-%d %H:%M:%S')


def get_china_time_iso():
    """获取中国时区的ISO格式时间字符串
    
    返回格式：'YYYY-MM-DDTHH:MM:SS'
    """
    return get_china_time().isoformat()


def coerce_int(value, default=0, min_value=None, max_value=None):
    """Convert request/config values to a bounded int without raising."""
    if value in (None, ''):
        result = default
    else:
        try:
            result = int(float(value))
        except (TypeError, ValueError):
            result = default

    if result is None:
        return None
    if min_value is not None:
        result = max(min_value, result)
    if max_value is not None:
        result = min(max_value, result)
    return result


# 只影响“页面界面语言/展示”、不代表不同内容的查询参数。
# 带这些参数的 URL 会把同一篇文章变成另一个地址，而且抓回来的是界面语言版本：
# 实测 x.com/...?lang=hi 抓到印地语页，标题被写成「X पर AI Will: …」（天城文），
# ?lang=bg 抓到保加利亚语页，标题被写成「GitHubDaily в X: …」（西里尔字母），
# 在中文列表里看着就像乱码。入库前统一剥掉。
URL_PRESENTATION_PARAMS = frozenset({
    'lang', 'hl', 'locale', 'ui_locales', 'lang_code', 'language',
})


def strip_url_presentation_params(url):
    """剥掉 URL 里只影响界面语言的查询参数（lang / hl / locale 等）。

    只删这些参数，其余查询串与原始编码逐字保留（不做 parse_qsl+urlencode 往返，
    避免把 %20 变成 + 、把重复参数顺序打乱）；没有命中时原样返回。
    """
    raw = str(url or '').strip()
    if not raw or '?' not in raw:
        return raw
    from urllib.parse import urlsplit, urlunsplit

    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw
    segments = [seg for seg in parsed.query.split('&') if seg]
    if not segments:
        return raw
    kept = [seg for seg in segments
            if seg.split('=', 1)[0].strip().lower() not in URL_PRESENTATION_PARAMS]
    if len(kept) == len(segments):
        return raw
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '&'.join(kept), parsed.fragment))

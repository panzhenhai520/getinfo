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

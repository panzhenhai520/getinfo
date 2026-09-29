"""Hong Kong time-window resource guard for collection scheduling."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

HKT = ZoneInfo('Asia/Hong_Kong')
PEAK_WINDOWS = ((time(8, 0), time(12, 0)), (time(13, 0), time(18, 0)))


def is_peak_time(value: datetime) -> bool:
    local = value.astimezone(HKT) if value.tzinfo else value.replace(tzinfo=HKT)
    clock = local.time().replace(tzinfo=None)
    return any(start <= clock < end for start, end in PEAK_WINDOWS)


def next_offpeak_time(value: datetime) -> datetime:
    local = value.astimezone(HKT) if value.tzinfo else value.replace(tzinfo=HKT)
    day = local.date()
    clock = local.time().replace(tzinfo=None)
    if time(8, 0) <= clock < time(12, 0):
        target = datetime.combine(day, time(12, 0), tzinfo=HKT)
    elif time(13, 0) <= clock < time(18, 0):
        target = datetime.combine(day, time(18, 0), tzinfo=HKT)
    else:
        target = local + timedelta(minutes=1)
    return target


def should_defer_task(task: dict, current_time: datetime) -> bool:
    """Only heavy crawl tasks are deferred; financial snapshots stay live."""
    cfg = task.get('config') or {}
    if isinstance(cfg, str):
        import json
        try:
            cfg = json.loads(cfg or '{}')
        except Exception:
            cfg = {}
    resource_class = str(cfg.get('resource_class') or 'crawl').casefold()
    if resource_class in {'financial_realtime', 'market_snapshot', 'light_scan'}:
        return False
    return is_peak_time(current_time) and not bool(cfg.get('allow_peak_execution'))

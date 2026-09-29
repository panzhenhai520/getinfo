# -*- coding: utf-8 -*-

"""终态任务自动清理（保留期可配，默认 1 天）。

清理对象（只删「不会再变化」的终态审计记录，业务数据不受影响）：
  - intel_jobs：status IN ('completed','failed','cancelled') 且 updated_at 早于保留期；
  - intel_candidates：status IN ('failed','discarded') 且 updated_at 早于保留期
    （失败的候选与已判弃的候选；queued/crawled/retry_wait 等活跃数据不动）。
保留期由环境变量 SYSTEM_TERMINAL_JOB_RETENTION_DAYS 控制（默认 1 天）。
"""

import os
from typing import Dict

from sqlite_database import SQLiteDatabase


def retention_days() -> int:
    try:
        return max(1, int(os.getenv('SYSTEM_TERMINAL_JOB_RETENTION_DAYS', '') or 1))
    except (TypeError, ValueError):
        return 1


def cleanup_terminal_records(db: SQLiteDatabase = None, days: int = None) -> Dict:
    """删除保留期之前的终态任务与失效候选，返回各表删除行数。"""
    if db is None:
        from sqlite_database import sqlite_db
        db = sqlite_db
    keep = max(1, int(days or retention_days()))
    db._ensure_connection()
    stats: Dict[str, int] = {}
    with db.lock:
        cursor = db.connection.cursor()
        try:
            cursor.execute(
                "DELETE FROM intel_jobs WHERE status IN ('completed','failed','cancelled')"
                " AND updated_at < datetime('now', ?)",
                (f'-{keep} day',),
            )
            stats['jobs_deleted'] = int(cursor.rowcount or 0)
            cursor.execute(
                "DELETE FROM intel_candidates WHERE status IN ('failed','discarded')"
                " AND updated_at < datetime('now', ?)",
                (f'-{keep} day',),
            )
            stats['candidates_deleted'] = int(cursor.rowcount or 0)
            db.connection.commit()
        finally:
            cursor.close()
    return stats

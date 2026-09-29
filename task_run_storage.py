# -*- coding: utf-8 -*-
"""
定时任务运行锁与运行统计存储子模块（从 sqlite_database.SQLiteDatabase 纯平移拆分）。

所有公开函数以 db 实例为第一参数；sqlite_database 中的 __getattr__ 代理会以
sqlite_db.<name>(...) 形式继续调用它们（零行为变化）。
"""

import json
from datetime import datetime

from utils import get_china_time


def _is_run_marker_stale(db, started_at, stale_seconds: int) -> bool:
    if not started_at:
        return False
    try:
        if isinstance(started_at, datetime):
            parsed = started_at
        else:
            text = str(started_at).strip().replace('Z', '+00:00')
            if ' ' in text and 'T' not in text:
                text = text.replace(' ', 'T')
            parsed = datetime.fromisoformat(text)
        if parsed.tzinfo:
            parsed = parsed.replace(tzinfo=None)
        return (get_china_time() - parsed).total_seconds() > stale_seconds
    except Exception:
        return False


def _datetime_to_db_text(value) -> str:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def claim_scheduled_task_run(
    db,
    task_id: int,
    lock_id: str,
    started_at=None,
    stale_seconds: int = 3600,
    scheduled_for=None,
    next_run=None,
    run_key: str = None,
):
    """Atomically reserve one scheduled run slot before submitting it to workers."""
    if not task_id or not lock_id:
        return False

    db._ensure_connection()
    with db.lock:
        cursor = db.connection.cursor()
        try:
            run_started_at = started_at or get_china_time()
            run_started_text = run_started_at.isoformat() if isinstance(run_started_at, datetime) else str(run_started_at)

            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                "SELECT is_active, running_lock_id, running_started_at FROM scheduled_tasks WHERE id = ?",
                (task_id,),
            )
            task_row = cursor.fetchone()
            if not task_row:
                db.connection.rollback()
                return False

            is_active = task_row['is_active']
            try:
                is_active = bool(int(is_active))
            except Exception:
                is_active = bool(is_active)
            if not is_active:
                db.connection.rollback()
                return False

            scheduled_for_text = _datetime_to_db_text(scheduled_for or run_started_at)
            next_run_text = _datetime_to_db_text(next_run)
            run_key = run_key or f"schedule:{task_id}:{scheduled_for_text}"

            cursor.execute(
                """
                SELECT id, status, started_at
                FROM task_execution_history
                WHERE run_key = ?
                LIMIT 1
                """,
                (run_key,),
            )
            existing_slot = cursor.fetchone()
            if existing_slot:
                if existing_slot['status'] in ('pending', 'running') and _is_run_marker_stale(db, existing_slot['started_at'], stale_seconds):
                    cursor.execute(
                        """
                        UPDATE task_execution_history
                        SET status = 'timeout',
                            completed_at = ?,
                            error_message = ?,
                            result_summary = ?
                        WHERE id = ?
                        """,
                        (
                            run_started_text,
                            'Previous run slot was stale and has been timed out',
                            json.dumps({'message': 'Previous run slot was stale and has been timed out'}),
                            existing_slot['id'],
                        ),
                    )
                else:
                    db.connection.rollback()
                    return False

            if existing_slot and existing_slot['status'] not in ('pending', 'running'):
                db.connection.rollback()
                return False

            cursor.execute(
                """
                SELECT id, run_key, scheduled_for, started_at
                FROM task_execution_history
                WHERE schedule_id = ?
                  AND status IN ('pending', 'running')
                ORDER BY COALESCE(scheduled_for, started_at, created_at) DESC
                LIMIT 1
                """,
                (task_id,),
            )
            incomplete_execution = cursor.fetchone()
            if incomplete_execution:
                if _is_run_marker_stale(db, incomplete_execution['started_at'], stale_seconds):
                    cursor.execute(
                        """
                        UPDATE task_execution_history
                        SET status = 'timeout',
                            completed_at = ?,
                            error_message = ?,
                            result_summary = ?
                        WHERE id = ?
                        """,
                        (
                            run_started_text,
                            'Previous recurring task instance was stale and has been timed out',
                            json.dumps({
                                'message': 'Previous recurring task instance was stale and has been timed out',
                                'previous_run_key': incomplete_execution['run_key'],
                                'previous_scheduled_for': incomplete_execution['scheduled_for'],
                            }),
                            incomplete_execution['id'],
                        ),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT OR IGNORE INTO task_execution_history (
                            schedule_id, task_id, status, run_key, scheduled_for,
                            started_at, completed_at, duration_seconds, articles_found,
                            error_message, result_summary, created_at
                        ) VALUES (?, NULL, 'skipped', ?, ?, ?, ?, 0, 0, ?, ?, ?)
                        """,
                        (
                            task_id,
                            run_key,
                            scheduled_for_text,
                            run_started_text,
                            run_started_text,
                            'Skipped because previous recurring task instance is still incomplete',
                            json.dumps({
                                'message': 'Skipped because previous recurring task instance is still incomplete',
                                'previous_execution_id': incomplete_execution['id'],
                                'previous_run_key': incomplete_execution['run_key'],
                                'previous_scheduled_for': incomplete_execution['scheduled_for'],
                            }),
                            run_started_text,
                        ),
                    )
                    if next_run_text:
                        cursor.execute(
                            """
                            UPDATE scheduled_tasks
                            SET next_run = ?,
                                updated_at = datetime('now', 'localtime')
                            WHERE id = ?
                            """,
                            (next_run_text, task_id),
                        )
                    db.connection.commit()
                    return False

            existing_lock = task_row['running_lock_id']
            if existing_lock:
                if _is_run_marker_stale(db, task_row['running_started_at'], stale_seconds):
                    cursor.execute(
                        """
                        UPDATE scheduled_tasks
                        SET running_lock_id = NULL,
                            running_started_at = NULL,
                            updated_at = datetime('now', 'localtime')
                        WHERE id = ?
                        """,
                        (task_id,),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT OR IGNORE INTO task_execution_history (
                            schedule_id, task_id, status, run_key, scheduled_for,
                            started_at, completed_at, duration_seconds, articles_found,
                            error_message, result_summary, created_at
                        ) VALUES (?, NULL, 'skipped', ?, ?, ?, ?, 0, 0, ?, ?, ?)
                        """,
                        (
                            task_id,
                            run_key,
                            scheduled_for_text,
                            run_started_text,
                            run_started_text,
                            'Skipped because scheduled task already has a running lock',
                            json.dumps({
                                'message': 'Skipped because scheduled task already has a running lock',
                                'running_lock_id': existing_lock,
                            }),
                            run_started_text,
                        ),
                    )
                    if next_run_text:
                        cursor.execute(
                            """
                            UPDATE scheduled_tasks
                            SET next_run = ?,
                                updated_at = datetime('now', 'localtime')
                            WHERE id = ?
                            """,
                            (next_run_text, task_id),
                        )
                    db.connection.commit()
                    return False

            cursor.execute(
                """
                SELECT id, started_at
                FROM task_execution_history
                WHERE schedule_id = ? AND status = 'running'
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (task_id,),
            )
            running_execution = cursor.fetchone()
            if running_execution:
                if _is_run_marker_stale(db, running_execution['started_at'], stale_seconds):
                    cursor.execute(
                        """
                        UPDATE task_execution_history
                        SET status = 'timeout',
                            completed_at = ?,
                            error_message = ?,
                            result_summary = ?
                        WHERE id = ?
                        """,
                        (
                            run_started_text,
                            'Previous running execution was stale and has been timed out',
                            json.dumps({'message': 'Previous running execution was stale and has been timed out'}),
                            running_execution['id'],
                        ),
                    )
                else:
                    db.connection.rollback()
                    return False

            cursor.execute(
                """
                SELECT id, created_at
                FROM crawl_tasks
                WHERE task_id LIKE ? AND status IN ('pending', 'running')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (f'schedule_{task_id}_%',),
            )
            running_crawl = cursor.fetchone()
            if running_crawl:
                if _is_run_marker_stale(db, running_crawl['created_at'], stale_seconds):
                    cursor.execute(
                        """
                        UPDATE crawl_tasks
                        SET status = 'failed',
                            completed_at = ?,
                            error_message = ?,
                            updated_at = datetime('now', 'localtime')
                        WHERE id = ?
                        """,
                        (
                            run_started_text,
                            'Stale crawl task was released by scheduler claim',
                            running_crawl['id'],
                        ),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT OR IGNORE INTO task_execution_history (
                            schedule_id, task_id, status, run_key, scheduled_for,
                            started_at, completed_at, duration_seconds, articles_found,
                            error_message, result_summary, created_at
                        ) VALUES (?, NULL, 'skipped', ?, ?, ?, ?, 0, 0, ?, ?, ?)
                        """,
                        (
                            task_id,
                            run_key,
                            scheduled_for_text,
                            run_started_text,
                            run_started_text,
                            'Skipped because a crawl task instance is still pending or running',
                            json.dumps({
                                'message': 'Skipped because a crawl task instance is still pending or running',
                            }),
                            run_started_text,
                        ),
                    )
                    if next_run_text:
                        cursor.execute(
                            """
                            UPDATE scheduled_tasks
                            SET next_run = ?,
                                updated_at = datetime('now', 'localtime')
                            WHERE id = ?
                            """,
                            (next_run_text, task_id),
                        )
                    db.connection.commit()
                    return False

            cursor.execute(
                """
                INSERT INTO task_execution_history (
                    schedule_id, task_id, status, run_key, scheduled_for,
                    started_at, articles_found, result_summary, created_at
                ) VALUES (?, NULL, 'pending', ?, ?, ?, 0, ?, ?)
                """,
                (
                    task_id,
                    run_key,
                    scheduled_for_text,
                    run_started_text,
                    json.dumps({'message': 'Scheduled run reserved'}),
                    run_started_text,
                ),
            )
            execution_id = cursor.lastrowid

            cursor.execute(
                """
                UPDATE scheduled_tasks
                SET running_lock_id = ?,
                    running_started_at = ?,
                    next_run = COALESCE(?, next_run),
                    updated_at = datetime('now', 'localtime')
                WHERE id = ?
                """,
                (lock_id, run_started_text, next_run_text, task_id),
            )
            db.connection.commit()
            if cursor.rowcount <= 0:
                return False
            return {
                'claimed': True,
                'execution_id': execution_id,
                'run_key': run_key,
                'scheduled_for': scheduled_for_text,
                'next_run': next_run_text,
            }
        except Exception as e:
            try:
                db.connection.rollback()
            except Exception:
                pass
            print(f"❌ 领取定时任务运行锁失败: {e}")
            return False
        finally:
            cursor.close()


def release_scheduled_task_run(db, task_id: int, lock_id: str = None) -> bool:
    """Release a scheduled task run lock after the worker exits."""
    if not task_id:
        return False

    try:
        db._ensure_connection()
        with db.lock:
            cursor = db.connection.cursor()
            try:
                if lock_id:
                    cursor.execute(
                        """
                        UPDATE scheduled_tasks
                        SET running_lock_id = NULL,
                            running_started_at = NULL,
                            updated_at = datetime('now', 'localtime')
                        WHERE id = ? AND running_lock_id = ?
                        """,
                        (task_id, lock_id),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE scheduled_tasks
                        SET running_lock_id = NULL,
                            running_started_at = NULL,
                            updated_at = datetime('now', 'localtime')
                        WHERE id = ?
                        """,
                        (task_id,),
                    )
                db.connection.commit()
                return cursor.rowcount > 0
            finally:
                cursor.close()
    except Exception as e:
        print(f"❌ 释放定时任务运行锁失败: {e}")
        return False


def update_scheduled_task_run_stats(db, task_id: int, success: bool, 
                                   last_run: datetime = None, next_run: datetime = None):
    """更新定时任务运行统计"""
    try:
        db._ensure_connection()
        with db.lock:
            if last_run is None:
                last_run = get_china_time()
            
            update_sql = """
            UPDATE scheduled_tasks SET
                total_runs = total_runs + 1,
                success_runs = success_runs + ?,
                failed_runs = failed_runs + ?,
                last_run = ?,
                next_run = CASE
                    WHEN ? IS NULL THEN next_run
                    WHEN next_run IS NULL THEN ?
                    WHEN datetime(?) > datetime(next_run) THEN ?
                    ELSE next_run
                END,
                updated_at = datetime('now', 'localtime')
            WHERE id = ?
            """
            next_run_text = next_run.isoformat() if next_run and isinstance(next_run, datetime) else next_run
            
            values = (
                1 if success else 0,
                0 if success else 1,
                last_run.isoformat() if isinstance(last_run, datetime) else last_run,
                next_run_text,
                next_run_text,
                next_run_text,
                next_run_text,
                task_id
            )
            
            cursor = db.connection.cursor()
            try:
                cursor.execute(update_sql, values)
                db.connection.commit()
                return True
            finally:
                cursor.close()
            
    except Exception as e:
        print(f"❌ 更新定时任务运行统计失败: {e}")
        return False


PROXY_METHODS = {
    "claim_scheduled_task_run": claim_scheduled_task_run,
    "release_scheduled_task_run": release_scheduled_task_run,
    "update_scheduled_task_run_stats": update_scheduled_task_run_stats,
}

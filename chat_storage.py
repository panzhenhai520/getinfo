# -*- coding: utf-8 -*-
"""
在线助手历史对话存储子模块（从 sqlite_database.SQLiteDatabase 纯平移拆分）。

所有公开函数以 db 实例为第一参数；sqlite_database 中的 __getattr__ 代理会以
sqlite_db.<name>(...) 形式继续调用它们（零行为变化）。
"""

import json
import sqlite3
from typing import Optional

from utils import get_china_time


def ensure_chat_tables(cursor):
    """幂等创建在线助手历史对话相关表、索引，并执行 v2 迁移与补列。"""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            model_id TEXT NOT NULL DEFAULT '',
            topic TEXT DEFAULT '',
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            created_at TEXT NOT NULL,
            industry_pack_id TEXT NOT NULL DEFAULT ''
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_history_session ON chat_history(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_history_created ON chat_history(created_at)")
    cursor.execute("""CREATE TABLE IF NOT EXISTS chat_operations (
        operation_id TEXT PRIMARY KEY, operation_type TEXT NOT NULL CHECK(operation_type IN ('gcd','synthesize')),
        source_session_ids_json TEXT NOT NULL, status TEXT NOT NULL,
        stage TEXT NOT NULL DEFAULT '', progress INTEGER NOT NULL DEFAULT 0,
        result_json TEXT NOT NULL DEFAULT '{}', error_message TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_operations_created ON chat_operations(created_at DESC)")
    cursor.execute("""CREATE TABLE IF NOT EXISTS chat_conflict_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL, conflict_key TEXT NOT NULL,
        decision TEXT NOT NULL CHECK(decision IN (
            'keep_a','keep_b','keep_both_pending','keep_newer_verified',
            'keep_historical','both_opinions','reject_all'
        )),
        rationale TEXT NOT NULL DEFAULT '', decision_version INTEGER NOT NULL DEFAULT 1,
        conflict_payload_sha256 TEXT NOT NULL DEFAULT '', report_versions_json TEXT NOT NULL DEFAULT '[]',
        decided_by TEXT NOT NULL DEFAULT '', applied_at TEXT NOT NULL,
        UNIQUE(operation_id, conflict_key, decision_version),
        FOREIGN KEY(operation_id) REFERENCES chat_operations(operation_id) ON DELETE CASCADE
    )""")
    _ensure_chat_conflict_decisions_v2(cursor)
    _ensure_chat_history_user_col(cursor)
    _ensure_chat_operations_owner_col(cursor)


def _ensure_chat_conflict_decisions_v2(cursor):
    """Migrate the pre-financial upsert table to immutable decision versions."""
    cursor.execute("PRAGMA table_info(chat_conflict_decisions)")
    columns = {str(row['name'] if isinstance(row, sqlite3.Row) else row[1]) for row in cursor.fetchall()}
    cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='chat_conflict_decisions'"
    )
    row = cursor.fetchone()
    ddl = str((row['sql'] if isinstance(row, sqlite3.Row) else row[0]) if row else '')
    if {
        'decision_version', 'conflict_payload_sha256', 'report_versions_json', 'decided_by'
    }.issubset(columns) and 'keep_newer_verified' in ddl:
        return
    cursor.execute("DROP TABLE IF EXISTS chat_conflict_decisions_v2")
    cursor.execute(
        """
        CREATE TABLE chat_conflict_decisions_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id TEXT NOT NULL,
            conflict_key TEXT NOT NULL,
            decision TEXT NOT NULL CHECK(decision IN (
                'keep_a','keep_b','keep_both_pending','keep_newer_verified',
                'keep_historical','both_opinions','reject_all'
            )),
            rationale TEXT NOT NULL DEFAULT '',
            decision_version INTEGER NOT NULL DEFAULT 1,
            conflict_payload_sha256 TEXT NOT NULL DEFAULT '',
            report_versions_json TEXT NOT NULL DEFAULT '[]',
            decided_by TEXT NOT NULL DEFAULT '',
            applied_at TEXT NOT NULL,
            UNIQUE(operation_id, conflict_key, decision_version),
            FOREIGN KEY(operation_id) REFERENCES chat_operations(operation_id) ON DELETE CASCADE
        )
        """
    )
    if columns:
        cursor.execute(
            """
            INSERT INTO chat_conflict_decisions_v2(
                id, operation_id, conflict_key, decision, rationale,
                decision_version, applied_at
            )
            SELECT id, operation_id, conflict_key, decision, rationale, 1, applied_at
            FROM chat_conflict_decisions
            """
        )
        cursor.execute("DROP TABLE chat_conflict_decisions")
    cursor.execute("ALTER TABLE chat_conflict_decisions_v2 RENAME TO chat_conflict_decisions")


# ─── 在线助手历史对话 ───────────────────────────────────────
def save_chat_qa(
    db,
    session_id: str,
    model_id: str,
    topic: str,
    question: str,
    answer: str,
    financial_route_key: str = '',
    industry_pack_id: str = '',
) -> Optional[int]:
    """保存问答，并在同一事务内关联可追溯的金融证据引用。"""
    from utils import get_china_time
    from financial_chat_history import attach_financial_audit
    now_str = get_china_time().strftime('%Y-%m-%d %H:%M:%S')
    pack = str(industry_pack_id or '').strip()
    try:
        db._ensure_connection()
        with db.lock:
            cursor = db.connection.cursor()
            try:
                user_id = _current_user_id()
                cursor.execute("BEGIN IMMEDIATE")
                # 老库/独立建表可能缺 user_id 列（多租户隔离），幂等补齐
                _ensure_chat_history_user_col(cursor)
                cursor.execute(
                    "INSERT INTO chat_history (session_id, model_id, topic, question, answer, created_at, industry_pack_id, user_id) VALUES (?,?,?,?,?,?,?,?)",
                    (session_id, model_id, topic or '', question, answer, now_str, pack, user_id)
                )
                row_id = int(cursor.lastrowid)
                attach_financial_audit(
                    db.connection,
                    chat_history_id=row_id,
                    session_id=session_id,
                    question=question,
                    route_key=str(financial_route_key or ''),
                )
                db.connection.commit()
                return row_id
            except Exception:
                db.connection.rollback()
                raise
            finally:
                cursor.close()
    except Exception as e:
        print(f"❌ 保存对话历史失败: {e}")
        return None


def _current_user_id():
    """当前包用户 id；无则 None（回退按行业包隔离，兼容管理员）。"""
    try:
        from pack_tenant import current_pack_user_id
        return current_pack_user_id()
    except Exception:
        return None


def _chat_scope(db):
    """聊天历史严格隔离条件（行业包内再按用户隔离，绝不串）：
    包用户 → 只能看到 user_id=自己的会话；
    管理员 → 只能看到 user_id IS NULL（管理员自己的）会话。"""
    try:
        from pack_tenant import current_pack_user_id
        uid = current_pack_user_id()
    except Exception:
        uid = None
    if uid:
        return "(user_id=?)", [int(uid)]
    return "(user_id IS NULL)", []


def _ensure_chat_history_user_col(cursor):
    """为 chat_history 增加 user_id 列（多租户按用户隔离；已存在则跳过）。"""
    try:
        cursor.execute("ALTER TABLE chat_history ADD COLUMN IF NOT EXISTS user_id BIGINT")
    except Exception:
        try:
            cursor.execute("ALTER TABLE chat_history ADD COLUMN user_id BIGINT")
        except Exception:
            pass


def get_chat_sessions(db, limit: int = 30, industry_pack_id: str = '') -> list:
    """获取历史会话列表（每个session取第一条问题作预览）。
    隔离口径：行业包 + 严格用户隔离（包用户只看自己的，管理员只看自己的）。"""
    pack = str(industry_pack_id or '').strip()
    try:
        db._ensure_connection()
        with db.lock:
            cursor = db.connection.cursor()
            try:
                _ensure_chat_history_user_col(cursor)
                conds = []
                params = []
                if pack:
                    conds.append("industry_pack_id=?")
                    params.append(pack)
                _scope_clause, _scope_params = _chat_scope(db)
                conds.append(_scope_clause)
                params.extend(_scope_params)
                where_clause = ("WHERE " + " AND ".join(conds)) if conds else ""
                params.append(limit)
                cursor.execute(f"""
                    SELECT session_id,
                           MIN(created_at) AS started_at,
                           MAX(created_at) AS last_at,
                           MAX(created_at) AS last_time,
                           COUNT(*) AS qa_count,
                           MIN(question) AS first_question,
                           MIN(model_id) AS model_id,
                           MIN(topic) AS topic
                    FROM chat_history
                    {where_clause}
                    GROUP BY session_id
                    ORDER BY last_at DESC
                    LIMIT ?
                """, params)
                return [dict(r) for r in cursor.fetchall()]
            finally:
                cursor.close()
    except Exception as e:
        print(f"❌ 获取会话列表失败: {e}")
        return []


def get_chat_session_messages(db, session_id: str, industry_pack_id: str = '') -> list:
    """获取某个会话的全部问答对。
    隔离口径：行业包 + 严格用户隔离（包用户只看自己的，管理员只看自己的），防跨包/跨用户读取。"""
    pack = str(industry_pack_id or '').strip()
    try:
        db._ensure_connection()
        with db.lock:
            cursor = db.connection.cursor()
            try:
                _ensure_chat_history_user_col(cursor)
                conds = ["session_id=?"]
                params = [session_id]
                if pack:
                    conds.append("industry_pack_id=?")
                    params.append(pack)
                _scope_clause, _scope_params = _chat_scope(db)
                conds.append(_scope_clause)
                params.extend(_scope_params)
                cursor.execute(
                    "SELECT id, question, answer, model_id, topic, created_at FROM chat_history WHERE "
                    + " AND ".join(conds) + " ORDER BY id ASC",
                    params,
                )
                messages = [dict(r) for r in cursor.fetchall()]
                from financial_chat_history import load_financial_audits
                audits = load_financial_audits(
                    db.connection,
                    [int(item['id']) for item in messages],
                )
                for item in messages:
                    audit = audits.get(int(item['id']))
                    if audit:
                        item['financial_audit'] = audit
                return messages
            finally:
                cursor.close()
    except Exception as e:
        print(f"❌ 获取会话消息失败: {e}")
        return []


def delete_chat_session(db, session_id: str, industry_pack_id: str = '') -> bool:
    """删除某个会话的全部记录。
    隔离口径：行业包 + 严格用户隔离，防跨包/跨用户删除。"""
    from financial_chat_history import delete_session_financial_audit
    pack = str(industry_pack_id or '').strip()
    try:
        db._ensure_connection()
        with db.lock:
            cursor = db.connection.cursor()
            try:
                _ensure_chat_history_user_col(cursor)
                conds = ["session_id=?"]
                params = [session_id]
                if pack:
                    conds.append("industry_pack_id=?")
                    params.append(pack)
                _scope_clause, _scope_params = _chat_scope(db)
                conds.append(_scope_clause)
                params.extend(_scope_params)
                cursor.execute("BEGIN IMMEDIATE")
                delete_session_financial_audit(db.connection, session_id)
                cursor.execute("DELETE FROM chat_history WHERE " + " AND ".join(conds), params)
                db.connection.commit()
                return True
            except Exception:
                db.connection.rollback()
                raise
            finally:
                cursor.close()
    except Exception as e:
        print(f"❌ 删除会话失败: {e}")
        return False


def _chat_owner_key(db):
    """聊天操作记录的用户归属键：包用户='pu:<id>'，管理员='admin'。"""
    try:
        from pack_tenant import current_pack_user_id
        uid = current_pack_user_id()
    except Exception:
        uid = None
    return f"pu:{int(uid)}" if uid else "admin"


def _ensure_chat_operations_owner_col(cursor):
    try:
        cursor.execute("ALTER TABLE chat_operations ADD COLUMN IF NOT EXISTS owner_key TEXT NOT NULL DEFAULT ''")
    except Exception:
        try:
            cursor.execute("ALTER TABLE chat_operations ADD COLUMN owner_key TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass


def create_chat_operation(db, operation_id, operation_type, session_ids):
    import json
    now = get_china_time().strftime('%Y-%m-%d %H:%M:%S')
    db._ensure_connection()
    with db.lock:
        cursor = db.connection.cursor()
        try:
            _ensure_chat_operations_owner_col(cursor)
            owner = _chat_owner_key(db)
            cursor.execute(
                "INSERT INTO chat_operations(operation_id,operation_type,source_session_ids_json,status,stage,progress,created_at,updated_at,owner_key) VALUES (?,?,?,'running','collecting',5,?,?,?)",
                (operation_id, operation_type, json.dumps(session_ids, ensure_ascii=False), now, now, owner))
            db.connection.commit()
        finally:
            cursor.close()


def update_chat_operation(db, operation_id, *, status=None, stage=None, progress=None, result=None, error=None):
    import json
    db._ensure_connection(); fields=[]; values=[]
    for name, value in [('status',status),('stage',stage),('progress',progress)]:
        if value is not None: fields.append(f'{name}=?'); values.append(value)
    if result is not None: fields.append('result_json=?'); values.append(json.dumps(result,ensure_ascii=False))
    if error is not None: fields.append('error_message=?'); values.append(str(error)[:1000])
    fields.append('updated_at=?'); values.append(get_china_time().strftime('%Y-%m-%d %H:%M:%S')); values.append(operation_id)
    # 仅允许操作创建者（同一用户）更新自己的操作记录
    with db.lock:
        cursor = db.connection.cursor()
        try:
            _ensure_chat_operations_owner_col(cursor)
            values.append(_chat_owner_key(db))
            cursor.execute(f"UPDATE chat_operations SET {', '.join(fields)} WHERE operation_id=? AND owner_key=?", values)
            db.connection.commit()
        finally:
            cursor.close()


def get_chat_operation(db, operation_id):
    import json
    db._ensure_connection()
    with db.lock:
        cursor = db.connection.cursor()
        try:
            _ensure_chat_operations_owner_col(cursor)
            row = cursor.execute(
                "SELECT * FROM chat_operations WHERE operation_id=? AND owner_key=?",
                (operation_id, _chat_owner_key(db)),
            ).fetchone()
        finally:
            cursor.close()
    if not row: return None
    data=dict(row)
    for key in ('source_session_ids_json','result_json'):
        try: data[key[:-5] if key.endswith('_json') else key]=json.loads(data[key] or '{}')
        except ValueError: data[key[:-5] if key.endswith('_json') else key]=[] if key.startswith('source') else {}
    return data


def save_chat_conflict_decision(
    db, operation_id, conflict_key, decision, rationale='', *,
    conflict_payload_sha256='', report_versions=None, decided_by='',
):
    allowed = {
        'keep_a', 'keep_b', 'keep_both_pending', 'keep_newer_verified',
        'keep_historical', 'both_opinions', 'reject_all',
    }
    if decision not in allowed:
        raise ValueError('无效裁决')
    import json
    db._ensure_connection(); now=get_china_time().strftime('%Y-%m-%d %H:%M:%S')
    versions_json = json.dumps(report_versions or [], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    rationale = str(rationale or '')[:1000]
    with db.lock:
        current = db.connection.execute(
            """
            SELECT id, decision, rationale, decision_version,
                   conflict_payload_sha256, report_versions_json, decided_by, applied_at
            FROM chat_conflict_decisions
            WHERE operation_id=? AND conflict_key=?
            ORDER BY decision_version DESC, id DESC LIMIT 1
            """,
            (operation_id, conflict_key),
        ).fetchone()
        if current and (
            str(current[1]) == decision
            and str(current[2] or '') == rationale
            and str(current[4] or '') == str(conflict_payload_sha256 or '')
            and str(current[5] or '[]') == versions_json
            and str(current[6] or '') == str(decided_by or '')
        ):
            return {
                'id': int(current[0]), 'decision': str(current[1]),
                'decision_version': int(current[3]), 'applied_at': str(current[7]),
                'idempotent': True,
            }
        version = int(current[3]) + 1 if current else 1
        cursor = db.connection.execute(
            """
            INSERT INTO chat_conflict_decisions(
                operation_id, conflict_key, decision, rationale, decision_version,
                conflict_payload_sha256, report_versions_json, decided_by, applied_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                operation_id, conflict_key, decision, rationale, version,
                str(conflict_payload_sha256 or ''), versions_json,
                str(decided_by or '')[:200], now,
            ),
        )
        db.connection.commit()
        return {
            'id': int(cursor.lastrowid), 'decision': decision,
            'decision_version': version, 'applied_at': now, 'idempotent': False,
        }


def get_chat_conflict_decisions(db, operation_id):
    db._ensure_connection()
    with db.lock:
        rows=db.connection.execute(
            """
            SELECT conflict_key, decision, rationale, decision_version,
                   conflict_payload_sha256, report_versions_json, decided_by, applied_at
            FROM (
                SELECT *, ROW_NUMBER() OVER(
                    PARTITION BY conflict_key ORDER BY decision_version DESC, id DESC
                ) AS rank_no
                FROM chat_conflict_decisions WHERE operation_id=?
            ) AS ranked WHERE rank_no=1 ORDER BY conflict_key
            """,
            (operation_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_chat_conflict_decision_history(db, operation_id):
    db._ensure_connection()
    with db.lock:
        rows = db.connection.execute(
            """
            SELECT conflict_key, decision, rationale, decision_version,
                   conflict_payload_sha256, report_versions_json, decided_by, applied_at
            FROM chat_conflict_decisions
            WHERE operation_id=? ORDER BY conflict_key, decision_version, id
            """,
            (operation_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_chat_operation_for_review_session(db, session_id):
    import json
    db._ensure_connection()
    with db.lock:
        rows = db.connection.execute(
            """
            SELECT * FROM chat_operations
            WHERE operation_type='synthesize' AND result_json<>'{}'
            ORDER BY updated_at DESC, created_at DESC
            """
        ).fetchall()
    for row in rows:
        item = dict(row)
        try:
            result = json.loads(item.get('result_json') or '{}')
        except (TypeError, ValueError):
            continue
        if str(result.get('session_id') or '') != str(session_id or ''):
            continue
        item['result'] = result
        try:
            item['source_session_ids'] = json.loads(item.get('source_session_ids_json') or '[]')
        except (TypeError, ValueError):
            item['source_session_ids'] = []
        return item
    return None


def get_chat_operation_for_review_pairs(db, pairs):
    """Detect copied review Q&A even when a client omits source_session_id."""
    db._ensure_connection()
    sessions = set()
    with db.lock:
        for pair in pairs or []:
            if not isinstance(pair, dict):
                continue
            rows = db.connection.execute(
                """
                SELECT DISTINCT session_id FROM chat_history
                WHERE question=? AND answer=?
                """,
                (str(pair.get('q') or ''), str(pair.get('a') or '')),
            ).fetchall()
            sessions.update(str(row[0]) for row in rows if str(row[0] or ''))
    for session_id in sorted(sessions):
        operation = get_chat_operation_for_review_session(db, session_id)
        result = (operation or {}).get('result') or {}
        if operation and result.get('financial_review') and result.get('conflicts'):
            return operation
    return None


PROXY_METHODS = {
    "save_chat_qa": save_chat_qa,
    "get_chat_sessions": get_chat_sessions,
    "get_chat_session_messages": get_chat_session_messages,
    "delete_chat_session": delete_chat_session,
    "create_chat_operation": create_chat_operation,
    "update_chat_operation": update_chat_operation,
    "get_chat_operation": get_chat_operation,
    "save_chat_conflict_decision": save_chat_conflict_decision,
    "get_chat_conflict_decisions": get_chat_conflict_decisions,
    "get_chat_conflict_decision_history": get_chat_conflict_decision_history,
    "get_chat_operation_for_review_session": get_chat_operation_for_review_session,
    "get_chat_operation_for_review_pairs": get_chat_operation_for_review_pairs,
    # 兼容外部按实例方法调用 v2 迁移（签名只有 cursor，代理时丢弃首参 db）
    "_ensure_chat_conflict_decisions_v2": lambda db, cursor: _ensure_chat_conflict_decisions_v2(cursor),
}

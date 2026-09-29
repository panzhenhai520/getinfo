# -*- coding: utf-8 -*-
"""Phase C：按行业包路由到不同数据库（真分布式落库）。每个行业包可配置 db_url/db_name。

当前先提供连接管理与路由入口；后续把 intel_repository 的读写方法改为按 pack_id 走对应连接。
无 db_url 的行业包回退到默认主库（sqlite_db）。
"""
from __future__ import annotations

import threading
from typing import Optional

from sqlite_database import sqlite_db

_conn_pool = {}
_lock = threading.Lock()


def _new_conn(db_url: str, db_name: str):
    """按 db_url/db_name 建立新连接：支持 postgres 或 sqlite。"""
    db_url = (db_url or '').strip().rstrip('/')
    db_name = (db_name or '').strip()
    if not db_url:
        return None  # 无配置 → 默认主库
    from sqlalchemy import create_engine  # noqa
    url = db_url
    if db_name:
        # postgres: postgresql://host:port/dbname
        if url.endswith('/'):
            url += db_name
        elif '?' in url:
            url = url.rsplit('?', 1)[0] + '/' + db_name + '?' + url.rsplit('?', 1)[1]
        else:
            url += '/' + db_name
    engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)
    return engine


def get_pack_engine(pack_id: Optional[str]):
    """返回该行业包配置的数据库 engine；无配置/失败回退默认主库（sqlite_db）。"""
    if not pack_id:
        return None
    key = str(pack_id)
    with _lock:
        if key in _conn_pool:
            return _conn_pool[key]
    try:
        from pack_tenant import get_pack_remote_config
        cfg = get_pack_remote_config(key)
        db_url = str(cfg.get('db_url') or '').strip()
        if not db_url:
            return None
        engine = _new_conn(db_url, str(cfg.get('db_name') or ''))
        with _lock:
            _conn_pool[key] = engine
        print(f'[pack_db] {key} -> engine at {db_url}', flush=True)
        return engine
    except Exception as exc:
        print(f'[pack_db] {key} 连接失败: {exc}', flush=True)
        return None


def pack_conn(pack_id: Optional[str]):
    """获取该行业包的 DB 连接；无包配置则用默认 sqlite_db.connection。"""
    engine = get_pack_engine(pack_id)
    if engine is not None:
        return engine.connect()
    return sqlite_db.connection

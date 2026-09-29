#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Database connection factory.

Routes the application between PostgreSQL (primary) and SQLite (backup)
according to ``config.DATABASE_TYPE``.  PostgreSQL connections get the
SQLite-compatibility shims installed so the existing SQL can keep working.
"""

from __future__ import annotations

import os
import sqlite3

import config


def database_type() -> str:
    value = str(getattr(config, "DATABASE_TYPE", "sqlite") or "sqlite").strip().lower()
    return "postgres" if value in {"postgres", "postgresql", "pg"} else "sqlite"


def is_postgres_connection(connection) -> bool:
    from postgres_compat import PostgresConnection
    return isinstance(connection, PostgresConnection)


def sqlite_path() -> str:
    return str(
        getattr(config, "SQLITE_BACKUP_PATH", "")
        or getattr(config, "DATABASE_PATH", "")
        or "crawler_articles.db"
    )


def connect_postgres_primary():
    from postgres_compat import connect_postgres
    from postgres_shims import ensure_postgres_shims
    connection = connect_postgres(config)
    ensure_postgres_shims(connection)
    return connection


def connect_database(read_only: bool = False, path: str | None = None):
    """Open the configured primary database.

    In PostgreSQL mode this returns a ``PostgresConnection``.  In SQLite mode
    it returns a regular ``sqlite3.Connection`` with ``Row`` configured.
    ``read_only`` is best effort for SQLite and intentionally ignored for
    PostgreSQL (PostgreSQL permissions are enforced server-side).
    """
    if database_type() == "postgres":
        return connect_postgres_primary()

    path = sqlite_path()
    if read_only and path != ":memory:":
        absolute = os.path.abspath(path)
        return sqlite3.connect(f"file:{absolute}?mode=ro", uri=True, timeout=30)

    connection = sqlite3.connect(
        path,
        check_same_thread=False,
        timeout=float(getattr(config, "SQLITE_BUSY_TIMEOUT_MS", 2000)) / 1000.0,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    return connection

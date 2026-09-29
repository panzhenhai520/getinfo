#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Full SQLite -> PostgreSQL replica migration.

Copies every table, row, primary key, index and (optionally) foreign key from
the CollectInfo SQLite database into a standalone PostgreSQL database.  The
script intentionally does not modify the SQLite file or the existing Flask app.

Usage:
    python tools/migrate_sqlite_to_postgres.py --dry-run
    python tools/migrate_sqlite_to_postgres.py --drop
    python tools/migrate_sqlite_to_postgres.py --verify
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
from decimal import Decimal
from typing import Any

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError as exc:  # pragma: no cover
    raise SystemExit("缺少 psycopg2，请先运行: pip install psycopg2-binary") from exc

try:
    from postgres_shims import ensure_postgres_shims
except ImportError:  # pragma: no cover
    ensure_postgres_shims = None


DEFAULT_SQLITE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "data", "crawler_articles.db")
)


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _pg_type(declared: str) -> str:
    value = str(declared or "").strip().upper()
    if not value:
        return "TEXT"
    if "BOOL" in value:
        return "BOOLEAN"
    if "INT" in value:
        return "BIGINT"
    if "REAL" in value or "FLOA" in value or "DOUB" in value:
        return "DOUBLE PRECISION"
    if "BLOB" in value:
        return "BYTEA"
    if "NUMERIC" in value or "DECIMAL" in value:
        return "NUMERIC"
    return "TEXT"


def _pg_default(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    upper = value.upper()
    if upper == "CURRENT_TIMESTAMP":
        return "CURRENT_TIMESTAMP"
    if upper == "CURRENT_DATE":
        return "CURRENT_DATE"
    if upper == "CURRENT_TIME":
        return "CURRENT_TIME"
    if upper == "NULL":
        return "NULL"
    if value in {"0", "1", "-1"}:
        return value
    if value.startswith("'") and value.endswith("'") and "'" not in value[1:-1]:
        return value
    compact = "".join(value.split()).lower()
    if compact.startswith("datetime('now'"):
        if "'localtime'" in compact:
            return "public.datetime('now','localtime')"
        return "public.datetime('now')"
    if compact.startswith("strftime('"):
        body = value[value.find("(") + 1:]
        if body.endswith(")"):
            body = body[:-1]
        return f"public.strftime({body})"
    if upper in {"TRUE", "FALSE"}:
        return upper
    return None


def _sqlite_tables(con: sqlite3.Connection) -> list[str]:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows]


def _table_columns(con: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [
        {
            "cid": row[0],
            "name": str(row[1]),
            "type": str(row[2] or ""),
            "notnull": bool(row[3]),
            "default": row[4],
            "pk": int(row[5] or 0),
        }
        for row in con.execute(f'PRAGMA table_info("{table}")').fetchall()
    ]


def _sequence_tables(con: sqlite3.Connection) -> set[str]:
    tables: set[str] = set()
    try:
        tables = {str(row[0]) for row in con.execute("SELECT name FROM sqlite_sequence").fetchall()}
    except sqlite3.DatabaseError:
        tables = set()

    # SQLite INTEGER PRIMARY KEY without AUTOINCREMENT still auto-assigns
    # rowid; PostgreSQL must get a BIGSERIAL default for those tables too.
    for table in _sqlite_tables(con):
        columns = _table_columns(con, table)
        pk_columns = sorted([c for c in columns if c["pk"] > 0], key=lambda c: c["pk"])
        if (
            len(pk_columns) == 1
            and str(pk_columns[0]["type"] or "").upper().startswith("INT")
        ):
            tables.add(table)
    return tables


def _indexes(con: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    indexes = []
    for row in con.execute(f'PRAGMA index_list("{table}")').fetchall():
        name = str(row[1])
        origin = str(row[3] or "") if len(row) > 3 else ""
        # Primary keys are recreated by the table DDL; unique constraints from
        # sqlite_autoindex still need an equivalent unique index in PostgreSQL.
        if name.startswith("sqlite_autoindex_") and origin != "u":
            continue
        columns = [
            str(item[2])
            for item in sorted(
                con.execute(f'PRAGMA index_info("{name}")').fetchall(),
                key=lambda item: int(item[0]),
            )
        ]
        if not columns:
            continue
        indexes.append({"name": name, "unique": bool(row[2]), "columns": columns})
    return indexes


def _foreign_keys(con: sqlite3.Connection, table: str) -> list[list[dict[str, Any]]]:
    groups: dict[int, list[dict[str, Any]]] = {}
    for row in con.execute(f'PRAGMA foreign_key_list("{table}")').fetchall():
        item = {
            "id": int(row[0]),
            "seq": int(row[1]),
            "table": str(row[2]),
            "from": str(row[3]),
            "to": str(row[4] or row[3]),
            "on_update": str(row[5] or "NO ACTION"),
            "on_delete": str(row[6] or "NO ACTION"),
        }
        groups.setdefault(item["id"], []).append(item)
    return [groups[key] for key in sorted(groups)]


def _column_ddl(col: dict[str, Any], is_serial: bool) -> str:
    name = _quote_ident(col["name"])
    if is_serial:
        return f"{name} BIGSERIAL PRIMARY KEY"
    part = f"{name} {_pg_type(col['type'])}"
    if col["notnull"]:
        part += " NOT NULL"
    default = _pg_default(col["default"])
    if default:
        part += f" DEFAULT {default}"
    return part


def _table_ddl(
    con: sqlite3.Connection,
    table: str,
    columns: list[dict[str, Any]],
    sequence_tables: set[str],
    with_foreign_keys: bool,
) -> str:
    pk_columns = sorted([col for col in columns if col["pk"] > 0], key=lambda col: col["pk"])
    single_pk = pk_columns[0] if len(pk_columns) == 1 else None
    is_serial = bool(
        single_pk
        and table in sequence_tables
        and single_pk["type"].upper().startswith("INT")
    )

    parts = []
    for col in columns:
        if col is single_pk and is_serial:
            parts.append(_column_ddl(col, True))
        else:
            parts.append(_column_ddl(col, False))

    if len(pk_columns) == 1 and not is_serial:
        pk = pk_columns[0]
        for index, part in enumerate(parts):
            if part.startswith(_quote_ident(pk["name"]) + " "):
                parts[index] = part + " PRIMARY KEY"
                break
    elif len(pk_columns) > 1:
        names = ", ".join(_quote_ident(col["name"]) for col in pk_columns)
        parts.append(f"PRIMARY KEY ({names})")

    if with_foreign_keys:
        valid_tables = set(_sqlite_tables(con))
        for group in _foreign_keys(con, table):
            if not group:
                continue
            parent = group[0]["table"]
            if parent not in valid_tables:
                continue
            from_cols = ", ".join(_quote_ident(item["from"]) for item in group)
            to_cols = ", ".join(_quote_ident(item["to"]) for item in group)
            on_delete = str(group[0]["on_delete"] or "NO ACTION").upper()
            on_update = str(group[0]["on_update"] or "NO ACTION").upper()
            if on_delete not in {"NO ACTION", "CASCADE", "SET NULL", "SET DEFAULT", "RESTRICT"}:
                on_delete = "NO ACTION"
            if on_update not in {"NO ACTION", "CASCADE", "SET NULL", "SET DEFAULT", "RESTRICT"}:
                on_update = "NO ACTION"
            parts.append(
                f"FOREIGN KEY ({from_cols}) REFERENCES {_quote_ident(parent)} ({to_cols}) "
                f"ON DELETE {on_delete} ON UPDATE {on_update}"
            )

    return f"CREATE TABLE {_quote_ident(table)} (\n  " + ",\n  ".join(parts) + "\n)"


def _index_ddl(table: str, index: dict[str, Any]) -> str:
    digest = hashlib.sha1(f"{table}:{index['name']}".encode("utf-8")).hexdigest()[:16]
    name = f"idx_{digest}"
    cols = ", ".join(_quote_ident(col) for col in index["columns"])
    unique = "UNIQUE " if index["unique"] else ""
    return f"CREATE {unique}INDEX IF NOT EXISTS {_quote_ident(name)} ON {_quote_ident(table)} ({cols})"


def _connect_postgres(args: argparse.Namespace):
    connection = psycopg2.connect(
        host=args.pg_host,
        port=args.pg_port,
        dbname=args.pg_db,
        user=args.pg_user,
        password=args.pg_password,
        connect_timeout=10,
    )
    try:
        connection.set_client_encoding("UTF8")
    except Exception:
        pass
    return connection


def _row_values(con: sqlite3.Connection, table: str):
    cursor = con.cursor()
    cursor.execute(f"SELECT * FROM {_quote_ident(table)}")
    while True:
        rows = cursor.fetchmany(1000)
        if not rows:
            break
        yield rows


def _adapt_value(value: Any, pg_kind: str = "") -> Any:
    if value is None:
        return None
    if isinstance(value, memoryview):
        return bytes(value)
    if pg_kind == "BOOLEAN" and isinstance(value, int) and value in {0, 1}:
        return bool(value)
    return value


def migrate(args: argparse.Namespace) -> int:
    sqlite_path = os.path.abspath(args.sqlite)
    if not os.path.exists(sqlite_path):
        print(f"SQLite 不存在: {sqlite_path}")
        return 2

    sqlite_con = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    tables = _sqlite_tables(sqlite_con)
    sequence_tables = _sequence_tables(sqlite_con)

    print(f"SQLite: {sqlite_path}")
    print(f"Tables: {len(tables)}")
    print(f"Postgres: {args.pg_host}:{args.pg_port}/{args.pg_db} as {args.pg_user}")

    if args.dry_run:
        for table in tables:
            columns = _table_columns(sqlite_con, table)
            print("\n" + _table_ddl(sqlite_con, table, columns, sequence_tables, args.with_foreign_keys) + ";")
            for index in _indexes(sqlite_con, table):
                print(_index_ddl(table, index) + ";")
        sqlite_con.close()
        return 0

    pg = _connect_postgres(args)
    pg.autocommit = False
    try:
        if ensure_postgres_shims is not None:
            ensure_postgres_shims(pg)
        with pg.cursor() as cursor:
            if args.drop:
                print("Dropping all tables in target database...")
                cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                for (name,) in cursor.fetchall():
                    cursor.execute(f"DROP TABLE IF EXISTS {_quote_ident(name)} CASCADE")
            print("Creating tables...")
            for table in tables:
                columns = _table_columns(sqlite_con, table)
                cursor.execute(_table_ddl(sqlite_con, table, columns, sequence_tables, args.with_foreign_keys))
                for index in _indexes(sqlite_con, table):
                    cursor.execute(_index_ddl(table, index))
            pg.commit()
    except Exception as exc:
        pg.rollback()
        print(f"迁移建表失败: {exc}")
        sqlite_con.close()
        pg.close()
        return 1

    try:
        with pg.cursor() as cursor:
            for table in tables:
                columns = _table_columns(sqlite_con, table)
                col_names = [col["name"] for col in columns]
                quoted_cols = ", ".join(_quote_ident(col) for col in col_names)
                quoted_table = _quote_ident(table)
                insert_sql = f"INSERT INTO {quoted_table} ({quoted_cols}) VALUES %s"
                copied = 0
                for batch in _row_values(sqlite_con, table):
                    pg_kinds = [_pg_type(col["type"]) for col in columns]
                    values = [
                        tuple(
                            _adapt_value(item[idx], pg_kinds[idx])
                            for idx in range(len(col_names))
                        )
                        for item in batch
                    ]
                    if not values:
                        continue
                    execute_values(cursor, insert_sql, values, page_size=1000)
                    copied += len(values)
                for col in columns:
                    if table in sequence_tables and col["pk"] == 1 and col["type"].upper().startswith("INT"):
                        sequence_name = f"{table}_{col['name']}_seq"
                        cursor.execute(
                            f"SELECT setval(%s, COALESCE((SELECT MAX({_quote_ident(col['name'])}) FROM {_quote_ident(table)}), 1), TRUE)",
                            (sequence_name,),
                        )
                        break
                print(f"{table}: {copied}")
            pg.commit()
    except Exception as exc:
        pg.rollback()
        print(f"迁移数据失败: {exc}")
        sqlite_con.close()
        pg.close()
        return 1

    sqlite_con.close()
    pg.close()
    print("迁移完成。")
    return 0


def _canonical(value: Any) -> str:
    if value is None:
        return "N"
    if isinstance(value, memoryview):
        value = bytes(value)
    if isinstance(value, bytes):
        return "b:" + hashlib.sha1(value).hexdigest()
    if isinstance(value, (bool, int)):
        return "n:" + str(int(value))
    if isinstance(value, float):
        return "f:" + repr(value)
    if isinstance(value, Decimal):
        return "n:" + str(value)
    return "s:" + str(value)


def _table_hash(sqlite_con: sqlite3.Connection, pg_cursor, table: str) -> tuple[str, str]:
    sqlite_rows = [
        tuple(_canonical(v) for v in row)
        for row in sqlite_con.execute(f"SELECT * FROM {_quote_ident(table)}").fetchall()
    ]
    pg_cursor.execute(f"SELECT * FROM {_quote_ident(table)}")
    pg_rows = [tuple(_canonical(v) for v in row) for row in pg_cursor.fetchall()]
    def digest(rows):
        rows = sorted(rows)
        joined = "\n".join("\x1f".join(row) for row in rows)
        return hashlib.sha1(joined.encode("utf-8", "replace")).hexdigest()
    return digest(sqlite_rows), digest(pg_rows)


def verify(args: argparse.Namespace) -> int:
    sqlite_con = sqlite3.connect(f"file:{os.path.abspath(args.sqlite)}?mode=ro", uri=True)
    pg = _connect_postgres(args)
    ok = True
    try:
        with pg.cursor() as cursor:
            for table in _sqlite_tables(sqlite_con):
                sqlite_count = sqlite_con.execute(f"SELECT COUNT(*) FROM {_quote_ident(table)}").fetchone()[0]
                try:
                    cursor.execute(f"SELECT COUNT(*) FROM {_quote_ident(table)}")
                    pg_count = cursor.fetchone()[0]
                except Exception as exc:
                    print(f"{table}: 缺失或读取失败 ({exc})")
                    ok = False
                    continue
                sqlite_hash, pg_hash = _table_hash(sqlite_con, cursor, table)
                status = "OK" if sqlite_count == pg_count and sqlite_hash == pg_hash else "MISMATCH"
                if status != "OK":
                    ok = False
                print(f"{table}: sqlite={sqlite_count} postgres={pg_count} {status} hash={sqlite_hash[:8]}..{pg_hash[:8]}..")
    finally:
        sqlite_con.close()
        pg.close()
    print("验证通过" if ok else "验证失败")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Full SQLite -> PostgreSQL replica migration")
    parser.add_argument("--sqlite", default=DEFAULT_SQLITE, help="SQLite database path")
    parser.add_argument("--pg-host", default=os.getenv("PGHOST", "127.0.0.1"))
    parser.add_argument("--pg-port", default=int(os.getenv("PGPORT", "5433")))
    parser.add_argument("--pg-db", default=os.getenv("PGDATABASE", "collectinfo"))
    parser.add_argument("--pg-user", default=os.getenv("PGUSER", "collectinfo"))
    parser.add_argument("--pg-password", default=os.getenv("PGPASSWORD", "collectinfo_pg_2026"))
    parser.add_argument("--drop", action="store_true", help="drop existing tables before migration")
    parser.add_argument("--with-foreign-keys", action="store_true", help="recreate foreign keys")
    parser.add_argument("--dry-run", action="store_true", help="print DDL only, no PostgreSQL connection")
    parser.add_argument("--verify", action="store_true", help="compare table row counts")
    args = parser.parse_args()

    if args.verify:
        return verify(args)
    return migrate(args)


if __name__ == "__main__":
    raise SystemExit(main())

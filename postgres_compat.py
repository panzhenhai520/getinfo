#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PostgreSQL compatibility layer for the SQLite-oriented CollectInfo code.

This module is intentionally small: it exposes a connection/cursor surface
similar enough to sqlite3 for the existing code to use while translating the
most common SQLite idioms.  The app should still use `DATABASE_TYPE=postgres`
only after the database has been initialized with the migration tool.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

try:
    import psycopg2
except ImportError as exc:  # pragma: no cover
    raise SystemExit("缺少 psycopg2，请先运行: pip install psycopg2-binary") from exc


def _translate_placeholders(sql: str) -> str:
    """Replace SQLite ``?`` placeholders with PostgreSQL ``%s``.

    The scanner is quote-aware so question marks inside string literals or
    comments are left untouched.  Backtick-quoted identifiers are converted to
    double quotes for PostgreSQL.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    state = "normal"
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if state == "normal":
            if ch == "'":
                state = "single"
                out.append(ch)
            elif ch == '"':
                state = "double"
                out.append(ch)
            elif ch == "`":
                state = "backtick"
                out.append('"')
            elif ch == "-" and nxt == "-":
                state = "line_comment"
                out.append(ch)
            elif ch == "/" and nxt == "*":
                state = "block_comment"
                out.append(ch)
            elif ch == "?":
                out.append("\x00COLLECTINFO_PARAM\x00")
            else:
                out.append(ch)
        elif state == "single":
            out.append(ch)
            if ch == "'" and nxt == "'":
                out.append(nxt)
                i += 1
            elif ch == "'":
                state = "normal"
        elif state == "double":
            out.append(ch)
            if ch == '"' and nxt == '"':
                out.append(nxt)
                i += 1
            elif ch == '"':
                state = "normal"
        elif state == "backtick":
            if ch == "`":
                out.append('"')
                state = "normal"
            else:
                out.append(ch)
        elif state == "line_comment":
            out.append(ch)
            if ch == "\n":
                state = "normal"
        elif state == "block_comment":
            out.append(ch)
            if ch == "*" and nxt == "/":
                out.append(nxt)
                i += 1
                state = "normal"
        i += 1
    rendered = "".join(out)
    # Escape literal percent signs for psycopg2, then restore the
    # positional placeholders generated from SQLite ``?`` markers.
    rendered = rendered.replace("%", "%%")
    rendered = rendered.replace("\x00COLLECTINFO_PARAM\x00", "%s")
    return rendered


def _translate_insert_or_ignore(sql: str) -> str:
    """Convert simple SQLite INSERT OR IGNORE into PostgreSQL ON CONFLICT DO NOTHING."""
    pattern = re.compile(
        r"(?is)^(\s*INSERT\s+OR\s+IGNORE\s+INTO\s+)(.*)$"
    )
    match = pattern.match(sql)
    if not match:
        return sql
    tail = match.group(2)
    upper_tail = tail.upper()
    if "ON CONFLICT" in upper_tail or "RETURNING" in upper_tail:
        return "INSERT INTO " + tail
    return "INSERT INTO " + tail.rstrip() + " ON CONFLICT DO NOTHING"


def _translate_insert_or_replace(sql: str) -> str:
    """SQLite INSERT OR REPLACE has no generic PG equivalent.

    Return a clearly failing statement so the caller sees the unsupported idiom
    instead of silently corrupting data.  The handful of such statements should
    be migrated manually to PostgreSQL upsert syntax.
    """
    if re.match(r"(?is)^\s*INSERT\s+OR\s+REPLACE\s+", sql):
        return "SELECT collectinfo_unsupported_insert_or_replace()"
    return sql


def _split_top_level_commas(text: str) -> list:
    """按顶层逗号切分参数列表（忽略括号内与单引号字符串内的逗号）。"""
    parts: list = []
    depth = 0
    in_single = False
    current: list = []
    for ch in text:
        if ch == "'":
            in_single = not in_single
        elif ch == "(" and not in_single:
            depth += 1
        elif ch == ")" and not in_single:
            depth -= 1
        if ch == "," and depth == 0 and not in_single:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    parts.append("".join(current))
    return parts


def _find_matching_paren(sql: str, open_idx: int) -> int:
    """返回与 open_idx 处 '(' 配对的 ')' 下标；找不到返回 -1。跳过字符串字面量。"""
    depth = 0
    i = open_idx
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            i += 1
            while i < n:
                if sql[i] == "'" and (i + 1 >= n or sql[i + 1] != "'"):
                    break
                i += 1
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _translate_scalar_max_min(sql: str) -> str:
    """SQLite 的 MAX(a,b)/MIN(a,b) 是标量取大/小值，PostgreSQL 无此函数，需 GREATEST/LEAST。

    仅当参数列表含两个及以上顶层参数（即标量取大/小）时转换；单个参数的聚合
    MAX(col)/MIN(col) 保持不变。GREATEST/LEAST 在 double precision / numeric /
    boolean / integer 混合参数下均可隐式统一类型，无需显式 cast（实测通过）。
    """

    pattern = re.compile(r"(?is)\b(MAX|MIN)\s*\(")
    out: list = []
    pos = 0
    n = len(sql)
    while True:
        m = pattern.search(sql, pos)
        if not m:
            out.append(sql[pos:])
            break
        out.append(sql[pos : m.start()])
        func = m.group(1).upper()
        open_idx = m.end() - 1  # '(' 的位置
        end_idx = _find_matching_paren(sql, open_idx)
        if end_idx < 0:
            out.append(sql[m.start() :])
            break
        inner = sql[open_idx + 1 : end_idx]
        parts = _split_top_level_commas(inner)
        if len(parts) >= 2:
            target = "GREATEST" if func == "MAX" else "LEAST"
            joined = ", ".join(part.strip() for part in parts)
            out.append(f"{target}({joined})")
        else:
            out.append(sql[m.start() : end_idx + 1])
        pos = end_idx + 1
    return "".join(out)


def _translate_sqlite_functions(sql: str) -> str:
    """把 SQLite 的 ``datetime()`` 转成 PostgreSQL 兼容写法（Postgres 主库）。

    注：``json_extract``/``json_array_length``/``json_valid``/``json_each``/``strftime``/
    ``julianday`` 等已由 postgres_shims 提供 postgres 兼容函数，此处不再处理，避免改写
    ``CAST(json_extract(...) AS INTEGER)`` 等既有模式。

    - datetime(col, '-N unit') ->  to_char((col)::timestamp + interval '-N unit', 'YYYY-MM-DD HH24:MI:SS')
                                   （返回 text，与 SQLite datetime() 返回文本一致，避免与 CASE 里
                                   原始 text 分支类型不匹配）
    - datetime(alias)           ->  alias（ORDER BY 引用 SELECT 列别名，去掉包装）
    """

    def _datetime_repl(m: re.Match) -> str:
        col = m.group(1)
        offset = f"{m.group(2)} {m.group(3)}"  # 如 '-8 hours'
        return f"to_char(({col})::timestamp + interval '{offset}', 'YYYY-MM-DD HH24:MI:SS')"

    sql = re.sub(
        r"datetime\(\s*([A-Za-z0-9_.]+)\s*,\s*'(-?\d+)\s*(hour|hours|day|days|minute|minutes)'\s*\)",
        _datetime_repl,
        sql,
        flags=re.I,
    )
    sql = re.sub(r"datetime\(\s*([A-Za-z0-9_.]+)\s*\)", r"\1", sql, flags=re.I)
    return sql


def translate_sql(sql: str) -> str:
    sql = _translate_placeholders(sql)
    sql = re.sub(r"(?is)^\s*BEGIN\s+IMMEDIATE\b", "BEGIN", sql)
    sql = re.sub(r"(?is)^\s*BEGIN\s+EXCLUSIVE\b", "BEGIN", sql)
    # SQLite 的 MAX(a,b)/MIN(a,b) 是标量取大/小值，PostgreSQL 需 GREATEST/LEAST。
    sql = _translate_scalar_max_min(sql)
    sql = _translate_sqlite_functions(sql)
    sql = _translate_insert_or_replace(sql)
    sql = _translate_insert_or_ignore(sql)
    # SQLite 的 INTEGER PRIMARY KEY AUTOINCREMENT 在 PG 必须换 BIGSERIAL。
    # ensure_* 建表函数大量走 cursor.execute，必须在这里兜底翻译，否则 PG 主库
    # 上这些 DDL 会报 AUTOINCREMENT 语法错误（曾导致新表整批建不出来）。
    sql = _translate_ddl(sql)
    return sql


def _translate_ddl(sql: str) -> str:
    """Translate SQLite DDL idioms that PostgreSQL's parser rejects outright.

    ``CREATE TABLE IF NOT EXISTS`` still parses its whole statement, so a
    SQLite ``INTEGER PRIMARY KEY AUTOINCREMENT`` would raise a syntax error
    even when the table already exists.  Convert it to a PostgreSQL
    ``BIGSERIAL PRIMARY KEY`` (the same shape the migration tool emits).
    """
    sql = re.sub(r"(?is)\bINT(?:EGER)?\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "BIGSERIAL PRIMARY KEY", sql)
    return sql


def _split_script(script: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    state = "normal"
    i = 0
    n = len(script)
    while i < n:
        ch = script[i]
        nxt = script[i + 1] if i + 1 < n else ""
        if state == "normal":
            if ch == "'":
                state = "single"
            elif ch == '"':
                state = "double"
            elif ch == "`":
                state = "backtick"
            elif ch == "-" and nxt == "-":
                state = "line_comment"
            elif ch == "/" and nxt == "*":
                state = "block_comment"
            elif ch == ";":
                text = "".join(current).strip()
                if text:
                    statements.append(text)
                current = []
                # 消费分号并跳过 current.append；否则 continue 跳过底部 i += 1，会在分号处死循环。
                i += 1
                continue
            current.append(ch)
        elif state == "single":
            current.append(ch)
            if ch == "'" and nxt == "'":
                current.append(nxt)
                i += 1
            elif ch == "'":
                state = "normal"
        elif state == "double":
            current.append(ch)
            if ch == '"' and nxt == '"':
                current.append(nxt)
                i += 1
            elif ch == '"':
                state = "normal"
        elif state == "backtick":
            if ch == "`":
                current.append('"')
                state = "normal"
            else:
                current.append(ch)
        elif state == "line_comment":
            current.append(ch)
            if ch == "\n":
                state = "normal"
        elif state == "block_comment":
            current.append(ch)
            if ch == "*" and nxt == "/":
                current.append(nxt)
                i += 1
                state = "normal"
        i += 1
    text = "".join(current).strip()
    if text:
        statements.append(text)
    return statements


class CompatRow:
    """Minimal sqlite3.Row-compatible result object."""

    def __init__(self, values: tuple, columns: list[str]):
        self._values = tuple(values)
        self._columns = list(columns)
        self._index = {name: idx for idx, name in enumerate(columns)}

    def keys(self) -> list[str]:
        return list(self._columns)

    def values(self) -> list:
        return list(self._values)

    def items(self):
        return [(name, self._values[idx]) for idx, name in enumerate(self._columns)]

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._values[self._index[key]]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __repr__(self):
        return f"CompatRow({dict(self.items())!r})"


class PostgresCursor:
    def __init__(self, cursor, owner=None):
        self._cursor = cursor
        self._owner = owner
        self.lastrowid = 0

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description

    def execute(self, sql: str, params: Any = None):
        translated = translate_sql(sql)
        if re.match(r"(?is)^\s*PRAGMA\b", translated):
            return self

        # 布尔字面量改写：按表结构把 0/1 换成 FALSE/TRUE（SQLite→PG 差异的统一收口）
        if self._owner is not None:
            try:
                translated = self._owner.rewrite_boolean_literals(translated)
            except Exception:
                pass

        translated = self._add_returning_id(translated)
        if isinstance(params, dict):
            self._cursor.execute(translated, params)
        else:
            self._cursor.execute(translated, tuple(params) if params is not None else None)
        self.lastrowid = getattr(self._cursor, "lastrowid", 0)
        if re.search(r"\bRETURNING\b", translated, re.I):
            row = self._cursor.fetchone()
            if row and row[0] is not None:
                try:
                    self.lastrowid = int(row[0])
                except (TypeError, ValueError):
                    # 主键是 TEXT 时（如 industry_pack_activations 的 RETURNING activation_id），
                    # lastrowid 没有意义；绝不能因此让整条 INSERT 抛错失败。
                    pass
        return self

    def _add_returning_id(self, sql: str) -> str:
        if not self._owner or "RETURNING" in sql.upper():
            return sql
        match = re.match(r"(?is)^\s*INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(?:\"?)([\w.]+)", sql)
        if not match:
            return sql
        table = match.group(1)
        if not self._owner.table_has_id(table):
            return sql
        statement = sql.rstrip().rstrip(";")
        return statement + " RETURNING id"

    def executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]):
        translated = translate_sql(sql)
        converted = [tuple(item) for item in seq_of_params]
        self._cursor.executemany(translated, converted)
        return self

    def _to_row(self, raw):
        if raw is None:
            return None
        columns = [item.name for item in self._cursor.description] if self._cursor.description else []
        return CompatRow(tuple(raw), columns)

    def fetchone(self):
        return self._to_row(self._cursor.fetchone())

    def fetchall(self):
        return [self._to_row(raw) for raw in self._cursor.fetchall()]

    def fetchmany(self, size=None):
        return [self._to_row(raw) for raw in self._cursor.fetchmany(size)]

    def close(self):
        self._cursor.close()

    def __iter__(self):
        for raw in self._cursor:
            yield self._to_row(raw)


class PostgresConnection:
    def __init__(self, *, host: str, port: int, dbname: str, user: str, password: str):
        self._connection = psycopg2.connect(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password,
            connect_timeout=10,
        )
        try:
            self._connection.set_client_encoding("UTF8")
        except Exception:
            pass
        self._connection.autocommit = True
        self.row_factory = None
        self._table_has_id_cache: dict[str, bool] = {}

    def cursor(self) -> PostgresCursor:
        return PostgresCursor(self._connection.cursor(), owner=self)

    def table_has_id(self, table: str) -> bool:
        table = (table or "").split(".")[-1]
        if table in self._table_has_id_cache:
            return self._table_has_id_cache[table]
        try:
            raw = self._connection.cursor()
            try:
                raw.execute(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = %s "
                    "AND column_name = 'id')",
                    (table,),
                )
                value = bool(raw.fetchone()[0])
            finally:
                raw.close()
        except Exception:
            value = False
        self._table_has_id_cache[table] = value
        return value

    def boolean_columns(self, table: str) -> set:
        """该表的布尔列名集合（缓存）。"""
        table = (table or "").split(".")[-1]
        cache = getattr(self, "_bool_columns_cache", None)
        if cache is None:
            cache = self._bool_columns_cache = {}
        if table in cache:
            return cache[table]
        columns = set()
        try:
            raw = self._connection.cursor()
            try:
                raw.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = %s "
                    "AND data_type = 'boolean'",
                    (table,),
                )
                columns = {str(row[0]).lower() for row in raw.fetchall()}
            finally:
                raw.close()
        except Exception:
            columns = set()
        cache[table] = columns
        return columns

    @staticmethod
    def _split_top_level(text: str, sep: str = ",") -> list:
        """按顶层分隔符切分（忽略括号内与引号内的分隔符）。"""
        parts, depth, quote, current = [], 0, "", []
        for ch in text:
            if quote:
                current.append(ch)
                if ch == quote:
                    quote = ""
                continue
            if ch in ("'", '"'):
                quote = ch
                current.append(ch)
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == sep and depth == 0:
                parts.append("".join(current))
                current = []
                continue
            current.append(ch)
        parts.append("".join(current))
        return parts

    def rewrite_boolean_literals(self, sql: str) -> str:
        """把布尔列上的 SQLite 风格 0/1 字面量改写成 PG 的 FALSE/TRUE。

        全仓历史上按 SQLite 写 SQL（布尔列写 `is_active=1`、INSERT 里塞 `0`），
        在 PostgreSQL 上会报 `boolean = integer` / `DatatypeMismatch`。这里按表结构
        统一改写，一次覆盖所有表的布尔列，避免逐个调用点去打补丁。
        """
        if not self._connection:
            return sql
        match = re.match(r"(?is)^\s*(INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE)\s+\"?([\w.]+)\"?", sql)
        if not match:
            return sql
        columns = self.boolean_columns(match.group(2))
        if not columns:
            return sql
        # 1) 比较 / 赋值形式： col = 1 / col=0
        for name in columns:
            sql = re.sub(
                r"(?i)\b%s\s*=\s*([01])(?![\d.])" % re.escape(name),
                lambda m, _n=name: "%s=%s" % (_n, "TRUE" if m.group(1) == "1" else "FALSE"),
                sql,
            )
        # 2) INSERT 的字面量：按列位置把 VALUES 里对应的 0/1 换掉
        if match.group(1).upper().startswith("INSERT"):
            sql = self._rewrite_insert_booleans(sql, columns)
        return sql

    def _rewrite_insert_booleans(self, sql: str, columns: set) -> str:
        open_paren = sql.find("(")
        close_paren = sql.find(")", open_paren)
        values_match = re.search(r"(?is)\bVALUES\s*\(", sql)
        if open_paren < 0 or close_paren < 0 or not values_match:
            return sql
        names = [
            name.strip().strip('"').strip("`").lower()
            for name in self._split_top_level(sql[open_paren + 1:close_paren])
        ]
        start = values_match.end()
        depth, idx = 1, start
        while idx < len(sql) and depth > 0:
            if sql[idx] == "(":
                depth += 1
            elif sql[idx] == ")":
                depth -= 1
            idx += 1
        inner_start, inner_end = start, idx - 1
        if inner_end <= inner_start:
            return sql
        items = self._split_top_level(sql[inner_start:inner_end])
        changed = False
        for position, name in enumerate(names):
            if position >= len(items) or name not in columns:
                continue
            token = items[position].strip()
            if token in ("0", "1"):
                items[position] = " TRUE " if token == "1" else " FALSE "
                changed = True
        if not changed:
            return sql
        return sql[:inner_start] + ",".join(items) + sql[inner_end:]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def execute(self, sql: str, params: Any = None) -> PostgresCursor:
        cursor = self.cursor()
        cursor.execute(sql, params)
        return cursor

    def executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]) -> PostgresCursor:
        cursor = self.cursor()
        cursor.executemany(sql, seq_of_params)
        return cursor

    def executescript(self, script: str) -> None:
        for statement in _split_script(script):
            upper = statement.upper()
            if upper.startswith(("PRAGMA ", "VACUUM", "ANALYZE", "REINDEX")):
                continue
            self.execute(_translate_ddl(statement))

    def commit(self):
        if self._connection.autocommit:
            # Under autocommit psycopg2.commit() is a no-op, so an explicit
            # ``BEGIN IMMEDIATE ... COMMIT`` block (the SQLite idiom the whole
            # app uses) would silently roll back on connection close.  Detect
            # an open server transaction and end it with a real COMMIT.
            try:
                if self._connection.get_transaction_status() != 0:
                    raw = self._connection.cursor()
                    try:
                        raw.execute("COMMIT")
                    finally:
                        raw.close()
            except Exception:
                self._connection.commit()
        else:
            self._connection.commit()

    def rollback(self):
        if self._connection.autocommit:
            try:
                if self._connection.get_transaction_status() != 0:
                    raw = self._connection.cursor()
                    try:
                        raw.execute("ROLLBACK")
                    finally:
                        raw.close()
            except Exception:
                self._connection.rollback()
        else:
            self._connection.rollback()

    def close(self):
        self._connection.close()


def connect_postgres(config) -> PostgresConnection:
    return PostgresConnection(
        host=getattr(config, "POSTGRES_HOST", "127.0.0.1"),
        port=int(getattr(config, "POSTGRES_PORT", 5432)),
        dbname=getattr(config, "POSTGRES_DB", "collectinfo"),
        user=getattr(config, "POSTGRES_USER", "postgres"),
        password=getattr(config, "POSTGRES_PASSWORD", ""),
    )

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PostgreSQL-side compatibility shims for SQLite-oriented application SQL.

The CollectInfo codebase was written for SQLite.  Instead of rewriting every
query, these shims recreate the small SQLite surface the app actually uses so
the same SQL keeps working after ``DATABASE_TYPE=postgres``.
"""

from __future__ import annotations

import re

_SKIP_RE = re.compile(r"(?is)^\s*(PRAGMA|VACUUM|ANALYZE|REINDEX|ATTACH|DETACH)\b")


def _raw_cursor(connection):
    underlying = getattr(connection, "_connection", connection)
    return underlying.cursor()


def _execute(connection, sql: str, name: str) -> None:
    raw = _raw_cursor(connection)
    try:
        raw.execute(sql)
    except Exception as exc:  # pragma: no cover - best effort only
        print(f"⚠️ PostgreSQL 兼容层 {name} 安装失败: {exc}")
    finally:
        raw.close()


def ensure_postgres_shims(connection) -> None:
    """Install SQLite compatibility objects into the public schema."""

    _execute(
        connection,
        """
        CREATE OR REPLACE VIEW public.sqlite_master AS
        SELECT 'table'::text AS type,
               c.relname::text AS name,
               c.relname::text AS tbl_name,
               0::integer AS rootpage,
               ''::text AS sql
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind IN ('r', 'p', 'v', 'm')
        """,
        "sqlite_master",
    )

    _execute(
        connection,
        r"""
        CREATE OR REPLACE FUNCTION public.json_extract(payload text, path text)
        RETURNS text
        LANGUAGE plpgsql IMMUTABLE STRICT
        AS $$
        DECLARE result jsonb;
        BEGIN
            BEGIN
                SELECT jsonb_path_query_first(
                    payload::jsonb,
                    path::jsonpath,
                    '{}'::jsonb,
                    true
                ) INTO result;
            EXCEPTION WHEN others THEN
                RETURN NULL;
            END;
            IF result IS NULL OR jsonb_typeof(result) = 'null' THEN
                RETURN NULL;
            END IF;
            RETURN result #>> '{}';
        END;
        $$;
        """,
        "json_extract(text,text)",
    )

    _execute(
        connection,
        r"""
        CREATE OR REPLACE FUNCTION public.json_extract(payload jsonb, path text)
        RETURNS text
        LANGUAGE plpgsql IMMUTABLE STRICT
        AS $$
        DECLARE result jsonb;
        BEGIN
            BEGIN
                SELECT jsonb_path_query_first(
                    payload,
                    path::jsonpath,
                    '{}'::jsonb,
                    true
                ) INTO result;
            EXCEPTION WHEN others THEN
                RETURN NULL;
            END;
            IF result IS NULL OR jsonb_typeof(result) = 'null' THEN
                RETURN NULL;
            END IF;
            RETURN result #>> '{}';
        END;
        $$;
        """,
        "json_extract(jsonb,text)",
    )

    _execute(
        connection,
        r"""
        CREATE OR REPLACE FUNCTION public.json_array_length(payload text)
        RETURNS integer
        LANGUAGE plpgsql IMMUTABLE STRICT
        AS $$
        DECLARE decoded jsonb;
        BEGIN
            BEGIN
                decoded := payload::jsonb;
            EXCEPTION WHEN others THEN
                RETURN 0;
            END;
            IF jsonb_typeof(decoded) = 'array' THEN
                RETURN jsonb_array_length(decoded);
            END IF;
            RETURN 0;
        END;
        $$;
        """,
        "json_array_length(text)",
    )

    _execute(
        connection,
        r"""
        CREATE OR REPLACE FUNCTION public.json_array_length(payload jsonb)
        RETURNS integer
        LANGUAGE plpgsql IMMUTABLE STRICT
        AS $$
        BEGIN
            IF jsonb_typeof(payload) = 'array' THEN
                RETURN jsonb_array_length(payload);
            END IF;
            RETURN 0;
        END;
        $$;
        """,
        "json_array_length(jsonb)",
    )

    _execute(
        connection,
        r"""
        CREATE OR REPLACE FUNCTION public.json_valid(payload text)
        RETURNS boolean
        LANGUAGE plpgsql IMMUTABLE STRICT
        AS $$
        BEGIN
            BEGIN
                PERFORM payload::jsonb;
            EXCEPTION WHEN others THEN
                RETURN false;
            END;
            RETURN true;
        END;
        $$;
        """,
        "json_valid(text)",
    )

    _execute(
        connection,
        r"""
        CREATE OR REPLACE FUNCTION public.json_valid(payload jsonb)
        RETURNS boolean
        LANGUAGE sql IMMUTABLE STRICT
        AS $$ SELECT true $$;
        """,
        "json_valid(jsonb)",
    )

    _execute(
        connection,
        r"""
        CREATE OR REPLACE FUNCTION public.json_each(payload text)
        RETURNS TABLE(
            key text,
            value text,
            type text,
            atom text,
            id bigint,
            parent text,
            fullkey text,
            path text
        )
        LANGUAGE plpgsql IMMUTABLE STRICT
        AS $$
        DECLARE decoded jsonb;
        BEGIN
            BEGIN
                decoded := payload::jsonb;
            EXCEPTION WHEN others THEN
                decoded := '[]'::jsonb;
            END;
            IF jsonb_typeof(decoded) = 'array' THEN
                RETURN QUERY
                SELECT (ord - 1)::text,
                       CASE WHEN jsonb_typeof(v) = 'null' THEN NULL ELSE v #>> '{}' END,
                       jsonb_typeof(v),
                       CASE WHEN jsonb_typeof(v) = 'null' THEN NULL ELSE v #>> '{}' END,
                       (ord - 1)::bigint,
                       NULL::text,
                       '$[' || (ord - 1)::text || ']',
                       '$[' || (ord - 1)::text || ']'
                FROM jsonb_array_elements(decoded) WITH ORDINALITY AS e(v, ord);
            ELSE
                RETURN QUERY
                SELECT j.key,
                       CASE WHEN jsonb_typeof(j.value) = 'null' THEN NULL ELSE j.value #>> '{}' END,
                       jsonb_typeof(j.value),
                       CASE WHEN jsonb_typeof(j.value) = 'null' THEN NULL ELSE j.value #>> '{}' END,
                       row_number() OVER ()::bigint - 1,
                       NULL::text,
                       '$.' || j.key,
                       '$.' || j.key
                FROM jsonb_each(decoded) AS j;
            END IF;
        END;
        $$;
        """,
        "json_each(text)",
    )

    _execute(
        connection,
        r"""
        CREATE OR REPLACE FUNCTION public.json_each(payload jsonb)
        RETURNS TABLE(
            key text,
            value text,
            type text,
            atom text,
            id bigint,
            parent text,
            fullkey text,
            path text
        )
        LANGUAGE plpgsql IMMUTABLE STRICT
        AS $$
        BEGIN
            IF jsonb_typeof(payload) = 'array' THEN
                RETURN QUERY
                SELECT (ord - 1)::text,
                       CASE WHEN jsonb_typeof(v) = 'null' THEN NULL ELSE v #>> '{}' END,
                       jsonb_typeof(v),
                       CASE WHEN jsonb_typeof(v) = 'null' THEN NULL ELSE v #>> '{}' END,
                       (ord - 1)::bigint,
                       NULL::text,
                       '$[' || (ord - 1)::text || ']',
                       '$[' || (ord - 1)::text || ']'
                FROM jsonb_array_elements(payload) WITH ORDINALITY AS e(v, ord);
            ELSE
                RETURN QUERY
                SELECT j.key,
                       CASE WHEN jsonb_typeof(j.value) = 'null' THEN NULL ELSE j.value #>> '{}' END,
                       jsonb_typeof(j.value),
                       CASE WHEN jsonb_typeof(j.value) = 'null' THEN NULL ELSE j.value #>> '{}' END,
                       row_number() OVER ()::bigint - 1,
                       NULL::text,
                       '$.' || j.key,
                       '$.' || j.key
                FROM jsonb_each(payload) AS j;
            END IF;
        END;
        $$;
        """,
        "json_each(jsonb)",
    )

    _execute(
        connection,
        r"""
        CREATE OR REPLACE FUNCTION public.datetime(
            p_value text,
            p_mod1 text DEFAULT NULL,
            p_mod2 text DEFAULT NULL,
            p_mod3 text DEFAULT NULL
        )
        RETURNS text
        LANGUAGE plpgsql IMMUTABLE
        AS $$
        DECLARE
            v_ts timestamp without time zone;
            modifier text;
            mods text[] := ARRAY[p_mod1, p_mod2, p_mod3];
        BEGIN
            IF lower(coalesce(p_value, '')) = 'now' THEN
                v_ts := (now() AT TIME ZONE 'Asia/Shanghai')::timestamp without time zone;
            ELSE
                BEGIN
                    v_ts := p_value::timestamp without time zone;
                EXCEPTION WHEN others THEN
                    BEGIN
                        v_ts := to_timestamp(p_value, 'YYYY-MM-DD');
                    EXCEPTION WHEN others THEN
                        RETURN NULL;
                    END;
                END;
            END IF;

            FOREACH modifier IN ARRAY mods LOOP
                IF modifier IS NULL THEN
                    CONTINUE;
                END IF;
                modifier := lower(btrim(modifier));
                IF modifier IN ('localtime', 'utc', 'auto', 'subsec') THEN
                    NULL;
                ELSIF modifier = 'start of day' THEN
                    v_ts := date_trunc('day', v_ts)::timestamp without time zone;
                ELSIF modifier = 'start of month' THEN
                    v_ts := date_trunc('month', v_ts)::timestamp without time zone;
                ELSIF modifier = 'start of year' THEN
                    v_ts := date_trunc('year', v_ts)::timestamp without time zone;
                ELSIF modifier ~ '^[+-]?\s*[0-9.]+\s+(hour|hours|day|days|minute|minutes|second|seconds|month|months|year|years)$' THEN
                    BEGIN
                        v_ts := v_ts + modifier::interval;
                    EXCEPTION WHEN others THEN
                        NULL;
                    END;
                END IF;
            END LOOP;

            RETURN to_char(v_ts, 'YYYY-MM-DD HH24:MI:SS');
        END;
        $$;
        """,
        "datetime",
    )

    _execute(
        connection,
        r"""
        CREATE OR REPLACE FUNCTION public.datetime(
            p_value text,
            p_mod1 text,
            p_mod2 text,
            p_mod3 text,
            p_mod4 text
        )
        RETURNS text
        LANGUAGE plpgsql IMMUTABLE
        AS $$
        DECLARE
            v_ts timestamp without time zone;
            modifier text;
            mods text[] := ARRAY[p_mod1, p_mod2, p_mod3, p_mod4];
        BEGIN
            IF lower(coalesce(p_value, '')) = 'now' THEN
                v_ts := (now() AT TIME ZONE 'Asia/Shanghai')::timestamp without time zone;
            ELSE
                BEGIN
                    v_ts := p_value::timestamp without time zone;
                EXCEPTION WHEN others THEN
                    BEGIN
                        v_ts := to_timestamp(p_value, 'YYYY-MM-DD');
                    EXCEPTION WHEN others THEN
                        RETURN NULL;
                    END;
                END;
            END IF;

            FOREACH modifier IN ARRAY mods LOOP
                IF modifier IS NULL THEN
                    CONTINUE;
                END IF;
                modifier := lower(btrim(modifier));
                IF modifier IN ('localtime', 'utc', 'auto', 'subsec') THEN
                    NULL;
                ELSIF modifier = 'start of day' THEN
                    v_ts := date_trunc('day', v_ts)::timestamp without time zone;
                ELSIF modifier = 'start of month' THEN
                    v_ts := date_trunc('month', v_ts)::timestamp without time zone;
                ELSIF modifier = 'start of year' THEN
                    v_ts := date_trunc('year', v_ts)::timestamp without time zone;
                ELSIF modifier ~ '^[+-]?\s*[0-9.]+\s+(hour|hours|day|days|minute|minutes|second|seconds|month|months|year|years)$' THEN
                    BEGIN
                        v_ts := v_ts + modifier::interval;
                    EXCEPTION WHEN others THEN
                        NULL;
                    END;
                END IF;
            END LOOP;

            RETURN to_char(v_ts, 'YYYY-MM-DD HH24:MI:SS');
        END;
        $$;
        """,
        "datetime-5args",
    )

    _execute(
        connection,
        r"""
        CREATE OR REPLACE FUNCTION public.date(
            p_value text,
            p_mod1 text DEFAULT NULL,
            p_mod2 text DEFAULT NULL,
            p_mod3 text DEFAULT NULL,
            p_mod4 text DEFAULT NULL
        )
        RETURNS text
        LANGUAGE plpgsql IMMUTABLE
        AS $$
        DECLARE
            v_ts timestamp without time zone;
            modifier text;
            mods text[] := ARRAY[p_mod1, p_mod2, p_mod3, p_mod4];
        BEGIN
            IF lower(coalesce(p_value, '')) = 'now' THEN
                v_ts := (now() AT TIME ZONE 'Asia/Shanghai')::timestamp without time zone;
            ELSE
                BEGIN
                    v_ts := p_value::timestamp without time zone;
                EXCEPTION WHEN others THEN
                    BEGIN
                        v_ts := to_timestamp(p_value, 'YYYY-MM-DD');
                    EXCEPTION WHEN others THEN
                        RETURN NULL;
                    END;
                END;
            END IF;

            FOREACH modifier IN ARRAY mods LOOP
                IF modifier IS NULL THEN
                    CONTINUE;
                END IF;
                modifier := lower(btrim(modifier));
                IF modifier = 'start of day' THEN
                    v_ts := date_trunc('day', v_ts)::timestamp without time zone;
                ELSIF modifier = 'start of month' THEN
                    v_ts := date_trunc('month', v_ts)::timestamp without time zone;
                ELSIF modifier = 'start of year' THEN
                    v_ts := date_trunc('year', v_ts)::timestamp without time zone;
                ELSIF modifier ~ '^[+-]?\s*[0-9.]+\s+(hour|hours|day|days|minute|minutes|second|seconds|month|months|year|years)$' THEN
                    BEGIN
                        v_ts := v_ts + modifier::interval;
                    EXCEPTION WHEN others THEN
                        NULL;
                    END;
                END IF;
            END LOOP;

            RETURN to_char(v_ts, 'YYYY-MM-DD');
        END;
        $$;
        """,
        "date",
    )

    _execute(
        connection,
        r"""
        CREATE OR REPLACE FUNCTION public.strftime(p_format text, p_value text)
        RETURNS text
        LANGUAGE plpgsql IMMUTABLE
        AS $$
        DECLARE
            v_ts timestamp without time zone;
            pg_format text := '';
            token text;
            mapped text;
            i integer := 1;
            c text;
        BEGIN
            IF lower(coalesce(p_value, '')) = 'now' THEN
                v_ts := (now() AT TIME ZONE 'Asia/Shanghai')::timestamp without time zone;
            ELSE
                BEGIN
                    v_ts := p_value::timestamp without time zone;
                EXCEPTION WHEN others THEN
                    RETURN NULL;
                END;
            END IF;

            WHILE i <= length(p_format) LOOP
                c := substr(p_format, i, 1);
                IF c = '%' AND i < length(p_format) THEN
                    token := substr(p_format, i, 2);
                    mapped := CASE token
                        WHEN '%Y' THEN 'YYYY'
                        WHEN '%m' THEN 'MM'
                        WHEN '%d' THEN 'DD'
                        WHEN '%H' THEN 'HH24'
                        WHEN '%M' THEN 'MI'
                        WHEN '%S' THEN 'SS'
                        WHEN '%f' THEN 'US'
                        WHEN '%z' THEN 'OF'
                        WHEN '%%' THEN '%'
                        ELSE NULL
                    END;
                    IF mapped IS NOT NULL THEN
                        pg_format := pg_format || mapped;
                        i := i + 2;
                        CONTINUE;
                    END IF;
                END IF;
                pg_format := pg_format || '"' || replace(c, '"', '""') || '"';
                i := i + 1;
            END LOOP;

            RETURN to_char(v_ts, pg_format);
        END;
        $$;
        """,
        "strftime",
    )

    _execute(
        connection,
        r"""
        CREATE OR REPLACE FUNCTION public.ifnull(a anyelement, b anyelement)
        RETURNS anyelement
        LANGUAGE sql IMMUTABLE
        AS $$ SELECT coalesce(a, b) $$;
        """,
        "ifnull",
    )

    _execute(
        connection,
        r"""
        CREATE OR REPLACE FUNCTION public._collectinfo_group_concat_step(
            state text,
            value text
        )
        RETURNS text
        LANGUAGE sql IMMUTABLE
        AS $$
            SELECT CASE
                WHEN state IS NULL OR state = '' THEN value
                ELSE state || ',' || value
            END
        $$;
        """,
        "_collectinfo_group_concat_step",
    )

    _execute(
        connection,
        r"""
        DROP AGGREGATE IF EXISTS public.group_concat(text);
        CREATE AGGREGATE public.group_concat(text) (
            SFUNC = public._collectinfo_group_concat_step,
            STYPE = text,
            INITCOND = ''
        );
        """,
        "group_concat",
    )

    # SQLite instr(X, Y) -> 1-based index of Y in X, 0 when absent.
    # PostgreSQL strpos has identical semantics (case-sensitive).
    _execute(
        connection,
        r"""
        CREATE OR REPLACE FUNCTION public.instr(p_source text, p_search text)
        RETURNS integer
        LANGUAGE sql IMMUTABLE STRICT
        AS $$ SELECT strpos(p_source, p_search) $$;
        """,
        "instr(text,text)",
    )

    # SQLite julianday(text) -> Julian day number (float).  We only rely on
    # differences (julianday(b) - julianday(a)) to get elapsed days, so the
    # epoch offset cancels out.  Use seconds-since-epoch / 86400.0.
    _execute(
        connection,
        r"""
        CREATE OR REPLACE FUNCTION public.julianday(p_value text)
        RETURNS double precision
        LANGUAGE plpgsql IMMUTABLE STRICT
        AS $$
        BEGIN
            RETURN EXTRACT(EPOCH FROM p_value::timestamp) / 86400.0;
        EXCEPTION WHEN others THEN
            RETURN NULL;
        END;
        $$;
        """,
        "julianday(text)",
    )

    # 扫描类型 CHECK 约束演进：老表约束不含 'tavily' → 重建（NOT VALID 只约束新行，
    # 不校验历史行；幂等，失败静默）。SQLite 新库由建表文本覆盖，此处仅 PG 生效。
    _execute(
        connection,
        "ALTER TABLE IF EXISTS intel_scan_runs "
        "DROP CONSTRAINT IF EXISTS intel_scan_runs_scanner_type_check",
        "intel_scan_runs_scanner_type_check.drop",
    )
    _execute(
        connection,
        "ALTER TABLE IF EXISTS intel_scan_runs "
        "ADD CONSTRAINT intel_scan_runs_scanner_type_check "
        "CHECK (scanner_type IN ('rss', 'list_page', 'website', 'serpapi', 'tavily')) NOT VALID",
        "intel_scan_runs_scanner_type_check.add",
    )


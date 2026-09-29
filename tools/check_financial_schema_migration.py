#!/usr/bin/env python3
"""Verify the additive financial migration against the frozen SQLite baseline."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from financial_schema import (  # noqa: E402
    FINANCIAL_REQUIRED_TABLES,
    FINANCIAL_SCHEMA_VERSION,
    ensure_financial_tables,
    financial_schema_checksum,
    get_financial_schema_version,
)


def _user_tables(connection: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _schema_for_tables(connection: sqlite3.Connection, tables: list[str]) -> dict[str, str]:
    if not tables:
        return {}
    placeholders = ",".join("?" for _ in tables)
    rows = connection.execute(
        f"SELECT name, sql FROM sqlite_master WHERE type='table' AND name IN ({placeholders})",
        tables,
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def _indexes_for_tables(connection: sqlite3.Connection, tables: list[str]) -> dict[str, tuple]:
    if not tables:
        return {}
    placeholders = ",".join("?" for _ in tables)
    rows = connection.execute(
        f"SELECT name, tbl_name, sql FROM sqlite_master "
        f"WHERE type='index' AND tbl_name IN ({placeholders}) ORDER BY name",
        tables,
    ).fetchall()
    return {row[0]: (row[1], row[2]) for row in rows}


def _row_counts(connection: sqlite3.Connection, tables: list[str]) -> dict[str, int]:
    return {
        table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in tables
    }


def _copy_with_sqlite_backup(source: Path, destination: Path) -> None:
    source_uri = f"file:{source.resolve()}?mode=ro"
    source_connection = sqlite3.connect(source_uri, uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def verify(baseline_path: Path, required_readonly_tables: list[str]) -> dict:
    with tempfile.TemporaryDirectory(prefix="financial-schema-acceptance-") as temp_dir:
        restored_path = Path(temp_dir) / "baseline-with-financial-schema.sqlite3"
        _copy_with_sqlite_backup(baseline_path, restored_path)

        connection = sqlite3.connect(restored_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        baseline_tables = _user_tables(connection)
        schema_before = _schema_for_tables(connection, baseline_tables)
        indexes_before = _indexes_for_tables(connection, baseline_tables)
        rows_before = _row_counts(connection, baseline_tables)
        foreign_keys_before = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]

        ensure_financial_tables(connection.cursor())
        first_schema = list(
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )
        ensure_financial_tables(connection.cursor())
        second_schema = list(
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )

        tables_after = set(_user_tables(connection))
        schema_after = _schema_for_tables(connection, baseline_tables)
        indexes_after = _indexes_for_tables(connection, baseline_tables)
        rows_after = _row_counts(connection, baseline_tables)
        integrity_rows = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys_after = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
        schema_version = get_financial_schema_version(connection.cursor())
        migration_rows = connection.execute(
            "SELECT version, migration_name, schema_checksum, status "
            "FROM financial_schema_migrations ORDER BY version"
        ).fetchall()
        connection.close()

        readonly_results = {}
        readonly_uri = f"file:{restored_path.resolve()}?mode=ro"
        readonly_connection = sqlite3.connect(readonly_uri, uri=True)
        try:
            for table in required_readonly_tables:
                try:
                    readonly_connection.execute(f'SELECT * FROM "{table}" LIMIT 1').fetchall()
                    readonly_results[table] = "passed"
                except sqlite3.Error as exc:
                    readonly_results[table] = f"failed: {exc}"
        finally:
            readonly_connection.close()

        acceptance = {
            "all_financial_tables_present": FINANCIAL_REQUIRED_TABLES <= tables_after,
            "baseline_table_schema_unchanged": schema_before == schema_after,
            "baseline_indexes_unchanged": indexes_before == indexes_after,
            "baseline_row_counts_unchanged": rows_before == rows_after,
            "history_and_dashboard_readers_passed": all(
                value == "passed" for value in readonly_results.values()
            ),
            "idempotent_schema": first_schema == second_schema,
            "integrity_check_passed": integrity_rows == ["ok"],
            # The frozen database contains known historical orphan rows.  This
            # migration must preserve that exact set and introduce none of its own.
            "foreign_key_violations_unchanged": foreign_keys_before == foreign_keys_after,
            "no_financial_foreign_key_violations": not any(
                row[0] in FINANCIAL_REQUIRED_TABLES for row in foreign_keys_after
            ),
            "schema_version_current": schema_version == FINANCIAL_SCHEMA_VERSION,
            "single_applied_migration": len(migration_rows) == 1
            and migration_rows[0][0] == FINANCIAL_SCHEMA_VERSION
            and migration_rows[0][2] == financial_schema_checksum()
            and migration_rows[0][3] == "applied",
        }
        acceptance["passed"] = all(acceptance.values())
        return {
            "report_version": "financial-schema-acceptance-v1",
            "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "baseline_database": str(baseline_path),
            "baseline_table_count": len(baseline_tables),
            "baseline_row_count_total": sum(rows_before.values()),
            "financial_schema_version": schema_version,
            "financial_table_count": len(FINANCIAL_REQUIRED_TABLES),
            "readonly_queries": readonly_results,
            "integrity_check": integrity_rows,
            "preexisting_foreign_key_violation_count": len(foreign_keys_before),
            "foreign_key_violation_count_after": len(foreign_keys_after),
            "acceptance": acceptance,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "baseline" / "database-backup-manifest.json",
    )
    parser.add_argument("--baseline", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "architecture" / "financial-schema-acceptance.json",
    )
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    baseline_path = args.baseline or Path(manifest["backup_database"])
    if not baseline_path.is_file():
        raise SystemExit(f"Frozen baseline database not found: {baseline_path}")
    required_readonly_tables = list(manifest.get("required_readonly_tables") or [])
    report = verify(baseline_path, required_readonly_tables)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

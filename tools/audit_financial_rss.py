#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Read-only lifecycle audit for registered RSS intelligence sources.

The audit deliberately distinguishes source registration from successful
discovery, article ingestion, and industry classification.  It opens SQLite in
read-only mode and can therefore be run safely against the live project file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


REQUIRED_TABLES = {
    "intel_sources",
    "intel_source_industries",
    "intel_scan_runs",
    "intel_candidate_observations",
    "intel_candidates",
    "article_intel_classifications",
}
SENSITIVE_QUERY_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?token|auth|authorization|password|secret|signature)",
    re.IGNORECASE,
)
SENSITIVE_TEXT = re.compile(
    r"(?i)((?:api[_-]?key|access[_-]?token|authorization|password|secret)\s*[=:]\s*)[^\s&,;]+"
)


class RSSAuditError(RuntimeError):
    """Raised when a database cannot be audited safely."""


def _utc_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sanitize_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        query = urlencode(
            [
                (key, "REDACTED" if SENSITIVE_QUERY_KEY.search(key) else item)
                for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            ]
        )
        return urlunsplit((parsed.scheme, host, parsed.path, query, ""))
    except (TypeError, ValueError):
        return SENSITIVE_TEXT.sub(r"\1REDACTED", raw)


def _sanitize_text(value: str) -> str:
    return SENSITIVE_TEXT.sub(r"\1REDACTED", str(value or ""))[:1000]


def _open_read_only(database_path: str | os.PathLike[str]) -> sqlite3.Connection:
    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        raise RSSAuditError(f"database does not exist: {path}")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _lifecycle(source: Dict) -> str:
    if not source["enabled"] and not source["scan_run_count"]:
        return "registered_disabled"
    if not source["scan_run_count"]:
        return "registered_never_scanned"
    if source["last_scan_status"] in {"failed", "rate_limited"} and not source[
        "successful_scan_count"
    ]:
        return "scan_failed"
    if not source["candidate_count"]:
        return "scanned_no_candidates"
    if not source["article_count"]:
        return "candidates_not_ingested"
    if not source["classification_count"]:
        return "ingested_unclassified"
    return "classified"


def audit_rss_database(database_path: str | os.PathLike[str]) -> Dict:
    """Return a machine-readable RSS lifecycle report without modifying SQLite."""
    path = Path(database_path).expanduser().resolve()
    connection = _open_read_only(path)
    try:
        missing = sorted(REQUIRED_TABLES - _table_names(connection))
        if missing:
            raise RSSAuditError(f"database is missing required tables: {', '.join(missing)}")

        rows = connection.execute(
            """
            SELECT
                s.id,
                s.source_name,
                s.source_url,
                s.market,
                s.authority_level,
                s.is_enabled,
                s.enabled_is_manual,
                s.polling_interval_minutes,
                s.last_scan_at,
                s.last_successful_scan_at,
                s.last_scan_status,
                s.last_scan_error,
                s.consecutive_scan_failures,
                COALESCE((
                    SELECT GROUP_CONCAT(x.industry_pack_id, ',')
                    FROM (
                        SELECT DISTINCT industry_pack_id
                        FROM intel_source_industries
                        WHERE source_id=s.id
                        ORDER BY industry_pack_id
                    ) x
                ), '') AS industry_pack_ids,
                (SELECT COUNT(*) FROM intel_scan_runs r WHERE r.source_id=s.id) AS scan_run_count,
                (SELECT COUNT(*) FROM intel_scan_runs r WHERE r.source_id=s.id
                    AND r.status IN ('completed','partial')) AS successful_scan_count,
                (SELECT COUNT(*) FROM intel_candidate_observations o
                    WHERE o.source_id=s.id) AS observation_count,
                (SELECT COUNT(DISTINCT o.candidate_id)
                    FROM intel_candidate_observations o
                    WHERE o.source_id=s.id) AS candidate_count,
                (SELECT COUNT(DISTINCT c.article_id)
                    FROM intel_candidate_observations o
                    JOIN intel_candidates c ON c.id=o.candidate_id
                    WHERE o.source_id=s.id AND c.article_id IS NOT NULL) AS article_count,
                (SELECT COUNT(DISTINCT ic.id)
                    FROM intel_candidate_observations o
                    JOIN intel_candidates c ON c.id=o.candidate_id
                    JOIN article_intel_classifications ic ON ic.article_id=c.article_id
                    WHERE o.source_id=s.id AND c.article_id IS NOT NULL) AS classification_count,
                (SELECT r.id FROM intel_scan_runs r WHERE r.source_id=s.id
                    ORDER BY COALESCE(r.completed_at,r.started_at,r.created_at) DESC, r.id DESC
                    LIMIT 1) AS latest_scan_run_id
            FROM intel_sources s
            WHERE LOWER(s.source_type)='rss'
            ORDER BY s.id
            """
        ).fetchall()

        sources: List[Dict] = []
        for row in rows:
            item = {
                "id": int(row["id"]),
                "name": str(row["source_name"] or ""),
                "url": _sanitize_url(row["source_url"]),
                "market": str(row["market"] or ""),
                "authority_level": int(row["authority_level"] or 0),
                "enabled": bool(row["is_enabled"]),
                "enabled_is_manual": bool(row["enabled_is_manual"]),
                "polling_interval_minutes": int(row["polling_interval_minutes"] or 0),
                "industry_pack_ids": [
                    value for value in str(row["industry_pack_ids"] or "").split(",") if value
                ],
                "last_scan_at": row["last_scan_at"],
                "last_successful_scan_at": row["last_successful_scan_at"],
                "last_scan_status": str(row["last_scan_status"] or ""),
                "last_scan_error": _sanitize_text(row["last_scan_error"]),
                "consecutive_scan_failures": int(row["consecutive_scan_failures"] or 0),
                "latest_scan_run_id": row["latest_scan_run_id"],
                "scan_run_count": int(row["scan_run_count"] or 0),
                "successful_scan_count": int(row["successful_scan_count"] or 0),
                "observation_count": int(row["observation_count"] or 0),
                "candidate_count": int(row["candidate_count"] or 0),
                "article_count": int(row["article_count"] or 0),
                "classification_count": int(row["classification_count"] or 0),
            }
            item["lifecycle_state"] = _lifecycle(item)
            sources.append(item)

        lifecycle_counts = Counter(item["lifecycle_state"] for item in sources)
        return {
            "audit_version": "financial-rss-audit-v1",
            "generated_at": _utc_text(),
            "database": str(path),
            "read_only": True,
            "summary": {
                "rss_source_count": len(sources),
                "enabled_count": sum(int(item["enabled"]) for item in sources),
                "disabled_count": sum(not item["enabled"] for item in sources),
                "never_scanned_count": sum(not item["scan_run_count"] for item in sources),
                "classified_source_count": lifecycle_counts.get("classified", 0),
                "lifecycle_counts": dict(sorted(lifecycle_counts.items())),
            },
            "sources": sources,
        }
    finally:
        connection.close()


def format_text_report(report: Dict) -> str:
    summary = report["summary"]
    lines = [
        "RSS lifecycle audit",
        f"database: {report['database']}",
        f"generated_at: {report['generated_at']}",
        (
            "sources: {rss_source_count}; enabled: {enabled_count}; disabled: "
            "{disabled_count}; never scanned: {never_scanned_count}; classified: "
            "{classified_source_count}"
        ).format(**summary),
    ]
    for source in report["sources"]:
        lines.append(
            "- #{id} {name}: {state}; enabled={enabled}; runs={runs}; "
            "candidates={candidates}; articles={articles}; classifications={classifications}".format(
                id=source["id"],
                name=source["name"],
                state=source["lifecycle_state"],
                enabled=str(source["enabled"]).lower(),
                runs=source["scan_run_count"],
                candidates=source["candidate_count"],
                articles=source["article_count"],
                classifications=source["classification_count"],
            )
        )
    return "\n".join(lines)


def _default_database() -> str:
    configured = Path(os.environ.get("DATABASE_PATH") or "crawler_articles.db")
    if configured.is_file():
        return str(configured)
    project_data = Path(__file__).resolve().parents[1] / "data" / "crawler_articles.db"
    return str(project_data if project_data.is_file() else configured)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit RSS source lifecycle in read-only mode")
    parser.add_argument("--database", default=_default_database())
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    parser.add_argument("--json-output", help="also write the JSON report to this path")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        report = audit_rss_database(args.database)
    except RSSAuditError as exc:
        print(f"RSS audit failed: {exc}")
        return 2
    if args.json_output:
        output = Path(args.json_output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, output)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_text_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

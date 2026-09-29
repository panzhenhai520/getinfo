#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Build the stage-0 live acceptance artifact without exposing credentials."""

from __future__ import annotations

import argparse
import json
import socket
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.check_financial_dashboard import check_dashboard
from tools.check_financial_dashboard_xss import check_xss
from tools.trace_financial_rss_pipeline import trace_pipeline


def _json_object(value) -> dict:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _worker_deployment(container: str) -> dict:
    if not container:
        return {"checked": False, "healthy": False, "reason": "container not supplied"}
    try:
        status = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", container],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        probe_process = subprocess.run(
            [
                "docker",
                "exec",
                container,
                "python",
                "tools/check_intel_worker_health.py",
                "--database",
                "/app/data/crawler_articles.db",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        probe = _json_object(probe_process.stdout.strip().splitlines()[-1])
        return {
            "checked": True,
            "container": container,
            "docker_health_status": status,
            "probe": probe,
            "healthy": status == "healthy" and bool(probe.get("healthy")),
        }
    except (OSError, subprocess.SubprocessError, IndexError) as exc:
        return {
            "checked": True,
            "container": container,
            "healthy": False,
            "reason": f"{type(exc).__name__}: {exc}",
        }


def _feed_acceptance(database_path: str, trace: dict) -> list[dict]:
    path = Path(database_path).expanduser().resolve()
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    traces = {int(item["source_id"]): item for item in trace.get("sources") or []}
    try:
        sources = connection.execute(
            """
            SELECT s.id,s.source_name,s.source_url,s.is_enabled,
                   s.last_scan_status,s.last_scan_error,s.last_scan_at,
                   s.last_successful_scan_at,s.consecutive_scan_failures,
                   s.polling_interval_minutes,s.metadata_json
            FROM intel_sources s
            JOIN intel_source_industries si ON si.source_id=s.id
            WHERE si.industry_pack_id='financial_markets' AND s.source_type='rss'
            ORDER BY s.id
            """
        ).fetchall()
        result = []
        for source in sources:
            latest = connection.execute(
                """
                SELECT id,status,discovered_count,queued_count,duplicate_count,
                       below_threshold_count,request_count,error_type,error_message,
                       metadata_json,started_at,completed_at
                FROM intel_scan_runs
                WHERE source_id=? AND industry_pack_id='financial_markets'
                ORDER BY id DESC LIMIT 1
                """,
                (int(source["id"]),),
            ).fetchone()
            metadata = _json_object(source["metadata_json"])
            latest_item = dict(latest) if latest else {}
            latest_metadata = _json_object(latest_item.pop("metadata_json", "{}"))
            latest_item["request_id"] = latest_metadata.get("request_id")
            latest_item["duration_ms"] = latest_metadata.get("duration_ms")
            source_trace = traces.get(int(source["id"])) or {}
            dedupe_passed = bool(
                latest_item
                and int(latest_item.get("discovered_count") or 0) > 0
                and int(latest_item.get("duplicate_count") or 0)
                == int(latest_item.get("discovered_count") or 0)
            )
            health_passed = bool(
                source["is_enabled"]
                and source["last_scan_status"] in {"completed", "partial"}
                and int(source["consecutive_scan_failures"] or 0) == 0
            )
            item = {
                "source_id": int(source["id"]),
                "source_name": source["source_name"],
                "source_url": source["source_url"],
                "access": {
                    "cost": metadata.get("access_cost") or "unknown",
                    "api_key_required": bool(metadata.get("api_key_required")),
                    "note": "无需 API Key 不等于免除来源条款、署名和再分发限制",
                },
                "schedule": {
                    "polling_interval_minutes": int(source["polling_interval_minutes"] or 0),
                    "preferred_scan_time": metadata.get("preferred_scan_time") or "08:30",
                    "timezone": "Asia/Hong_Kong",
                },
                "health": {
                    "last_scan_status": source["last_scan_status"],
                    "last_scan_error": source["last_scan_error"],
                    "last_scan_at": source["last_scan_at"],
                    "last_successful_scan_at": source["last_successful_scan_at"],
                    "consecutive_scan_failures": int(source["consecutive_scan_failures"] or 0),
                    "passed": health_passed,
                },
                "latest_financial_scan": latest_item,
                "rescan_deduplication_passed": dedupe_passed,
                "pipeline": {
                    "complete": bool(source_trace.get("complete")),
                    "run_count": int(source_trace.get("run_count") or 0),
                    "observation_count": int(source_trace.get("observation_count") or 0),
                    "candidate_count": int(source_trace.get("candidate_count") or 0),
                    "article_count": int(source_trace.get("article_count") or 0),
                    "classification_count": int(source_trace.get("classification_count") or 0),
                    "sample_chain": source_trace.get("sample_chain"),
                },
            }
            item["passed"] = bool(
                item["access"]["cost"] == "free_public_rss"
                and not item["access"]["api_key_required"]
                and health_passed
                and dedupe_passed
                and item["pipeline"]["complete"]
            )
            result.append(item)
        return result
    finally:
        connection.close()


def build_acceptance(
    database_path: str,
    *,
    worker_container: str = "firecrawl-intel-worker",
    run_browser_xss: bool = True,
) -> dict:
    now_utc = datetime.now(timezone.utc)
    trace = trace_pipeline(database_path)
    dashboard = check_dashboard(database_path, time_range="730d")
    xss = check_xss() if run_browser_xss else {"passed": False, "skipped": True}
    worker = _worker_deployment(worker_container)
    feeds = _feed_acceptance(database_path, trace)
    passed = bool(
        len(feeds) == 5
        and all(item["passed"] for item in feeds)
        and trace.get("all_sources_complete")
        and dashboard.get("passed")
        and xss.get("passed")
        and worker.get("healthy")
    )
    return {
        "acceptance_version": "financial-rss-stage0-v1",
        "passed": passed,
        "generated_at_utc": now_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "generated_at_hong_kong": now_utc.astimezone(
            timezone(timedelta(hours=8))
        ).isoformat(timespec="seconds"),
        "clock_source": "computer_server_system_clock",
        "environment": {
            "host": socket.gethostname(),
            "python": sys.version.split()[0],
            "database_file": Path(database_path).name,
        },
        "summary": {
            "required_feed_count": 5,
            "accepted_feed_count": sum(int(item["passed"]) for item in feeds),
            "pipeline_complete_count": int(trace.get("complete_source_count") or 0),
            "financial_dashboard_total": int(
                ((dashboard.get("financial") or {}).get("total")) or 0
            ),
            "family_office_dashboard_total": int(
                ((dashboard.get("family_office") or {}).get("total")) or 0
            ),
        },
        "feeds": feeds,
        "pipeline": {
            "all_sources_complete": bool(trace.get("all_sources_complete")),
            "trace_version": trace.get("trace_version"),
        },
        "dashboard": {
            "passed": bool(dashboard.get("passed")),
            "authentication": dashboard.get("authentication"),
            "financial": dashboard.get("financial"),
            "family_office": dashboard.get("family_office"),
            "frontend": dashboard.get("frontend"),
        },
        "browser_xss": xss,
        "worker_deployment": worker,
        "licensing_boundary": (
            "本阶段仅确认五条公开 RSS 无需 API Key；继续使用必须遵守各来源条款、"
            "合理轮询、署名及再分发限制，不承诺 SLA。"
        ),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build financial RSS stage-0 acceptance JSON")
    parser.add_argument("--database", default="data/crawler_articles.db")
    parser.add_argument("--output", default="rss-live-acceptance.json")
    parser.add_argument("--worker-container", default="firecrawl-intel-worker")
    parser.add_argument("--skip-browser-xss", action="store_true")
    args = parser.parse_args(argv)
    report = build_acceptance(
        args.database,
        worker_container=args.worker_container,
        run_browser_xss=not args.skip_browser_xss,
    )
    output = Path(args.output).expanduser().resolve()
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "passed": report["passed"],
                "summary": report["summary"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

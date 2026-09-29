#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Container health probe for the non-HTTP market-intelligence worker."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path


def find_worker_processes(proc_root: str | Path = "/proc", *, current_pid: int | None = None) -> list[dict]:
    root = Path(proc_root)
    current_pid = int(current_pid or os.getpid())
    workers = []
    for process_dir in root.iterdir():
        if not process_dir.name.isdigit() or int(process_dir.name) == current_pid:
            continue
        try:
            arguments = [
                item.decode("utf-8", "replace")
                for item in (process_dir / "cmdline").read_bytes().split(b"\0")
                if item
            ]
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        executable = Path(arguments[0]).name.casefold() if arguments else ""
        if executable.startswith("python") and any(
            Path(argument).name == "intel_worker.py" for argument in arguments[1:]
        ):
            workers.append({"pid": int(process_dir.name), "argv": arguments[:4]})
    return workers


def check_database(database_path: str) -> dict:
    path = Path(database_path).expanduser().resolve()
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
    connection.execute("PRAGMA query_only=ON")
    try:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='intel_jobs'"
        ).fetchone()
        if not row:
            raise RuntimeError("intel_jobs 表不存在")
        connection.execute("SELECT COUNT(*) FROM intel_jobs WHERE status='running'").fetchone()
        return {"path": str(path), "readable": True, "intel_jobs_present": True}
    finally:
        connection.close()


def health_report(
    database_path: str,
    *,
    proc_root: str | Path = "/proc",
    require_process: bool = True,
) -> dict:
    errors = []
    try:
        database = check_database(database_path)
    except Exception as exc:
        database = {"path": str(Path(database_path).expanduser()), "readable": False}
        errors.append(f"database:{type(exc).__name__}:{exc}")
    try:
        workers = find_worker_processes(proc_root)
    except Exception as exc:
        workers = []
        errors.append(f"process:{type(exc).__name__}:{exc}")
    if require_process and not workers:
        errors.append("process:intel_worker.py not running")
    return {
        "check_version": "intel-worker-health-v1",
        "healthy": not errors,
        "database": database,
        "worker_process_count": len(workers),
        "worker_processes": workers,
        "errors": errors,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check the intel worker process and SQLite read path")
    parser.add_argument("--database", default=os.getenv("DATABASE_PATH", "data/crawler_articles.db"))
    parser.add_argument("--proc-root", default="/proc")
    parser.add_argument("--skip-process", action="store_true")
    args = parser.parse_args(argv)
    report = health_report(
        args.database,
        proc_root=args.proc_root,
        require_process=not args.skip_process,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["healthy"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

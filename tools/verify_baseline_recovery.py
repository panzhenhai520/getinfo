#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Restore the frozen code/database in isolation and run non-destructive smoke tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.backup_sqlite_baseline import inspect_database


SEMANTIC_SNAPSHOT_FIELDS = (
    "integrity_ok",
    "user_version",
    "schema_sha256",
    "tables",
    "indexes",
    "row_counts",
    "readonly_queries",
    "readonly_queries_ok",
)
REQUIRED_CODE_FILES = (
    "firecrawl_app.py",
    "config.py",
    "sqlite_database.py",
    "docker-compose.crawler.yml",
    "Dockerfile.baseline-smoke",
    "baseline/sanitized-config-manifest.json",
    "baseline/runtime-pip-freeze.txt",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_differences(expected: dict, actual: dict) -> dict:
    return {
        field: {"expected": expected.get(field), "actual": actual.get(field)}
        for field in SEMANTIC_SNAPSHOT_FIELDS
        if expected.get(field) != actual.get(field)
    }


def recovery_acceptance(
    checks: dict,
    *,
    container_smoke_requested: bool,
    current_schema_compatibility_requested: bool = False,
) -> dict:
    required = (
        "revision_resolved",
        "archive_matches_revision",
        "required_code_files_present",
        "runtime_files_excluded",
        "backup_checksum_matches",
        "database_restore_matches_manifest",
        "database_restore_rto_passed",
        "database_integrity_passed",
        "readonly_queries_passed",
        "config_summary_matches_revision",
        "config_summary_is_sanitized",
        "host_application_smoke_passed",
    )
    passed = all(checks.get(name) is True for name in required)
    if container_smoke_requested:
        passed = passed and checks.get("container_build_passed") is True
        passed = passed and checks.get("container_startup_smoke_passed") is True
        passed = passed and checks.get("container_network_disabled") is True
    if current_schema_compatibility_requested:
        passed = passed and checks.get("current_schema_compatibility_passed") is True
    return {
        **{name: bool(checks.get(name)) for name in required},
        "container_smoke_requested": container_smoke_requested,
        "container_build_passed": bool(checks.get("container_build_passed")),
        "container_startup_smoke_passed": bool(
            checks.get("container_startup_smoke_passed")
        ),
        "container_network_disabled": bool(checks.get("container_network_disabled")),
        "current_schema_compatibility_requested": current_schema_compatibility_requested,
        "current_schema_compatibility_passed": bool(
            checks.get("current_schema_compatibility_passed")
        ),
        "passed": passed,
    }


def _run(
    args: list[str],
    *,
    cwd: Path,
    timeout: int = 120,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(cwd),
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _git_value(repository: Path, revision: str) -> str:
    return _run(
        ["git", "rev-parse", "--verify", revision],
        cwd=repository,
    ).stdout.strip()


def _safe_extract_archive(archive_path: Path, destination: Path) -> int:
    with tarfile.open(archive_path, mode="r") as archive:
        members = archive.getmembers()
        for member in members:
            relative = PurePosixPath(member.name)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or member.issym()
                or member.islnk()
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError(f"unsafe archive member: {member.name}")
            target = (destination / Path(*relative.parts)).resolve()
            if destination.resolve() not in (target, *target.parents):
                raise ValueError(f"archive path escapes destination: {member.name}")
        archive.extractall(destination)
    return sum(member.isfile() for member in members)


def _sqlite_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30
    )
    connection.execute("PRAGMA query_only=ON")
    return connection


def _sqlite_restore(source_path: Path, target_path: Path) -> None:
    source = _sqlite_readonly(source_path)
    destination = sqlite3.connect(str(target_path), timeout=30)
    try:
        source.backup(destination, pages=256, sleep=0.01)
    finally:
        destination.close()
        source.close()


def _minimal_smoke_environment(database_path: Path) -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "TZ")
        if os.environ.get(key)
    }
    environment.update(
        {
            # The recovery smoke test runs inside the Linux deployment even
            # when its environment is assembled by a Windows control host.
            "DATABASE_PATH": database_path.as_posix(),
            "DEFAULT_ADMIN_PASSWORD": "recovery-smoke-not-for-production",
            "PYTHONDONTWRITEBYTECODE": "1",
            "ENABLE_SCHEDULER": "false",
            "INTEL_SOURCE_SYNC_ENABLED": "false",
            "INTEL_LIGHT_SCANNER_ENABLED": "false",
            "INTEL_CANDIDATE_DISPATCH_ENABLED": "false",
            "INTEL_TOPIC_CLUSTER_ENABLED": "false",
            "INTEL_LLM_ENABLED": "false",
            "RAGFLOW_LLM_ENABLED": "false",
            "RAGFLOW_UPLOAD_ENABLED": "false",
            "RAGFLOW_TTS_ENABLED": "false",
            "SERPAPI_ENABLED": "false",
        }
    )
    return environment


_APPLICATION_SMOKE = r"""
from firecrawl_app import app, user_db
user = user_db.connection.execute(
    "SELECT id FROM users WHERE is_active=1 ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, id LIMIT 1"
).fetchone()
assert user is not None
token = user_db.create_session(int(user['id']), user_agent='isolated-recovery-smoke', expire_hours=1)
assert token
client = app.test_client()
client.set_cookie('session_token', token)
health = client.get('/api/system/health')
assert health.status_code == 200 and health.get_json().get('success') is True
dashboard = client.get('/api/intel/dashboard?industry_pack_id=financial_markets&time_range=7d')
assert dashboard.status_code == 200 and dashboard.get_json().get('success') is True
sessions = client.get('/api/chat/history/sessions')
assert sessions.status_code == 200 and sessions.get_json().get('success') is True
items = sessions.get_json().get('sessions') or []
if items:
    session_id = str(items[0].get('session_id') or '')
    history = client.get('/api/chat/history/session/' + session_id)
    assert history.status_code == 200 and history.get_json().get('success') is True
"""


def _host_application_smoke(code_root: Path, database_path: Path) -> dict:
    try:
        _run(
            ["python3", "-c", _APPLICATION_SMOKE],
            cwd=code_root,
            env=_minimal_smoke_environment(database_path),
            timeout=120,
        )
        return {
            "passed": True,
            "health_endpoint": "passed",
            "dashboard_read": "passed",
            "chat_history_read": "passed",
            "subprocess_output_retained": False,
        }
    except (subprocess.SubprocessError, OSError) as exc:
        return {
            "passed": False,
            "error_type": type(exc).__name__,
            "subprocess_output_retained": False,
        }


_CURRENT_SCHEMA_PREPARE = r"""
from sqlite_database import SQLiteDatabase
database = SQLiteDatabase(__import__('os').environ['DATABASE_PATH'])
assert database.connect()
assert database.create_tables()
database.disconnect()
"""


def _protected_row_counts(database_path: Path) -> tuple[bool, dict[str, int]]:
    connection = _sqlite_readonly(database_path)
    try:
        integrity_ok = connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        counts = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in (
                "articles",
                "chat_history",
                "users",
                "financial_research_runs",
            )
        }
        return integrity_ok, counts
    finally:
        connection.close()


def _current_schema_compatibility_smoke(
    baseline_code_root: Path,
    restored_database: Path,
    isolation_root: Path,
) -> dict:
    """Run baseline code against a latest-schema copy, preserving a new row."""

    compatibility_database = isolation_root / "current-schema-compatibility.sqlite3"
    _sqlite_restore(restored_database, compatibility_database)
    started = time.monotonic()
    try:
        _run(
            ["python3", "-c", _CURRENT_SCHEMA_PREPARE],
            cwd=PROJECT_ROOT,
            env=_minimal_smoke_environment(compatibility_database),
            timeout=120,
        )
        connection = sqlite3.connect(str(compatibility_database), timeout=30)
        try:
            connection.execute(
                """
                INSERT INTO financial_research_runs(
                    id, trigger_type, scope_type, status, requested_at
                ) VALUES(
                    'stage-6.6-legal-post-baseline', 'recovery_smoke', 'market',
                    'completed', '2026-08-03T00:00:00.000Z'
                )
                """
            )
            connection.commit()
        finally:
            connection.close()
        integrity_before, counts_before = _protected_row_counts(compatibility_database)
        smoke = _host_application_smoke(baseline_code_root, compatibility_database)
        integrity_after, counts_after = _protected_row_counts(compatibility_database)
        legal_row_preserved = counts_after == counts_before
        passed = all(
            (
                integrity_before,
                integrity_after,
                legal_row_preserved,
                smoke["passed"],
                counts_after.get("financial_research_runs") == 1,
            )
        )
        return {
            "passed": passed,
            "baseline_application_smoke": smoke,
            "latest_financial_schema_present": True,
            "synthetic_post_baseline_row_preserved": legal_row_preserved,
            "protected_row_counts_before": counts_before,
            "protected_row_counts_after": counts_after,
            "integrity_before": integrity_before,
            "integrity_after": integrity_after,
            "live_database_modified": False,
            "subprocess_output_retained": False,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "rto_target_seconds": 180,
        }
    except (subprocess.SubprocessError, OSError, sqlite3.Error, KeyError) as exc:
        return {
            "passed": False,
            "error_type": type(exc).__name__,
            "live_database_modified": False,
            "subprocess_output_retained": False,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "rto_target_seconds": 180,
        }


def _container_smoke(
    code_root: Path,
    database_path: Path,
    container_data: Path,
    image_tag: str,
) -> dict:
    build_passed = False
    startup_passed = False
    error_type = ""
    image_id = ""
    container_data.mkdir(parents=True, exist_ok=True)
    container_database = container_data / "runtime.sqlite3"
    _sqlite_restore(database_path, container_database)
    try:
        _run(
            [
                "docker",
                "build",
                "--network=none",
                "--file",
                "Dockerfile.baseline-smoke",
                "--tag",
                image_tag,
                ".",
            ],
            cwd=code_root,
            timeout=600,
        )
        build_passed = True
        image_id = _run(
            ["docker", "image", "inspect", image_tag, "--format", "{{.Id}}"],
            cwd=code_root,
        ).stdout.strip()
        _run(
            [
                "docker",
                "run",
                "--rm",
                "--network=none",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=128m",
                "--mount",
                f"type=bind,source={container_data.resolve()},target=/recovery",
                "--env",
                "DATABASE_PATH=/recovery/runtime.sqlite3",
                "--env",
                "DEFAULT_ADMIN_PASSWORD=recovery-smoke-not-for-production",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--env",
                "ENABLE_SCHEDULER=false",
                "--env",
                "INTEL_LLM_ENABLED=false",
                "--env",
                "RAGFLOW_LLM_ENABLED=false",
                "--env",
                "RAGFLOW_UPLOAD_ENABLED=false",
                "--env",
                "SERPAPI_ENABLED=false",
                image_tag,
                "python",
                "-c",
                _APPLICATION_SMOKE,
            ],
            cwd=code_root,
            timeout=180,
        )
        startup_passed = True
    except (subprocess.SubprocessError, OSError) as exc:
        error_type = type(exc).__name__
    return {
        "build_passed": build_passed,
        "startup_smoke_passed": startup_passed,
        "network_mode": "none",
        "read_only_root": True,
        "temporary_database_mount": True,
        "subprocess_output_retained": False,
        "image_tag": image_tag,
        "image_id": image_id,
        "error_type": error_type,
    }


def verify_baseline_recovery(
    repository: str | Path,
    *,
    revision: str,
    database_manifest: str | Path,
    backup_path: str | Path | None = None,
    run_container_smoke: bool = False,
    run_current_schema_compatibility: bool = False,
    image_tag: str = "firecrawlapp-crawler:financial-baseline-recovery",
) -> dict:
    root = Path(repository).expanduser().resolve()
    manifest_path = Path(database_manifest).expanduser()
    if not manifest_path.is_absolute():
        manifest_path = root / manifest_path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    saved_backup_path = Path(manifest["backup_database"])
    backup = (
        Path(backup_path).expanduser().resolve()
        if backup_path
        else saved_backup_path.expanduser().resolve()
    )
    resolved_commit = _git_value(root, f"{revision}^{{commit}}")
    resolved_tree = _git_value(root, f"{revision}^{{tree}}")
    tracked_count = int(
        _run(
            ["git", "ls-tree", "-r", "--name-only", resolved_commit], cwd=root
        ).stdout.count("\n")
    )

    checks: dict[str, bool] = {"revision_resolved": len(resolved_commit) == 40}
    with tempfile.TemporaryDirectory(prefix="financial-baseline-recovery-") as temp:
        isolation_root = Path(temp)
        archive_path = isolation_root / "baseline.tar"
        code_root = isolation_root / "code"
        code_root.mkdir()
        _run(
            [
                "git",
                "archive",
                "--format=tar",
                f"--output={archive_path}",
                resolved_commit,
            ],
            cwd=root,
        )
        extracted_file_count = _safe_extract_archive(archive_path, code_root)
        checks["archive_matches_revision"] = extracted_file_count == tracked_count
        missing_code_files = [
            relative for relative in REQUIRED_CODE_FILES if not (code_root / relative).is_file()
        ]
        checks["required_code_files_present"] = not missing_code_files
        forbidden_runtime_paths = [
            relative
            for relative in (".git", ".env", "data", "backups", "auth_storage")
            if (code_root / relative).exists()
        ]
        checks["runtime_files_excluded"] = not forbidden_runtime_paths

        config_path = code_root / "baseline/sanitized-config-manifest.json"
        expected_config = _run(
            [
                "git",
                "show",
                f"{resolved_commit}:baseline/sanitized-config-manifest.json",
            ],
            cwd=root,
        ).stdout.encode("utf-8")
        config_payload = json.loads(config_path.read_text(encoding="utf-8"))
        config_sha256 = sha256_file(config_path)
        checks["config_summary_matches_revision"] = hashlib.sha256(
            expected_config
        ).hexdigest() == config_sha256
        config_acceptance = config_payload.get("acceptance") or {}
        checks["config_summary_is_sanitized"] = all(
            (
                config_acceptance.get("passed") is True,
                config_acceptance.get("raw_secret_values_absent") is True,
                config_acceptance.get("dotenv_values_committed") is False,
            )
        )

        checks["backup_checksum_matches"] = (
            backup.is_file()
            and sha256_file(backup) == str(manifest.get("backup_sha256") or "")
        )
        if not checks["backup_checksum_matches"]:
            raise ValueError("backup is missing or does not match the frozen checksum")
        restored_database = isolation_root / "restored.sqlite3"
        restore_started = time.monotonic()
        _sqlite_restore(backup, restored_database)
        restore_elapsed_seconds = time.monotonic() - restore_started
        checks["database_restore_rto_passed"] = restore_elapsed_seconds < 300
        connection = _sqlite_readonly(restored_database)
        try:
            restored_snapshot = inspect_database(
                connection,
                tuple(manifest.get("required_readonly_tables") or ()),
            )
        finally:
            connection.close()
        database_differences = snapshot_differences(
            manifest["backup_snapshot"], restored_snapshot
        )
        checks["database_restore_matches_manifest"] = not database_differences
        checks["database_integrity_passed"] = restored_snapshot["integrity_ok"]
        checks["readonly_queries_passed"] = restored_snapshot["readonly_queries_ok"]

        runtime_database = isolation_root / "host-runtime.sqlite3"
        _sqlite_restore(restored_database, runtime_database)
        host_smoke = _host_application_smoke(code_root, runtime_database)
        checks["host_application_smoke_passed"] = host_smoke["passed"]

        current_schema_compatibility = {
            "requested": run_current_schema_compatibility,
            "passed": False,
            "live_database_modified": False,
            "subprocess_output_retained": False,
        }
        if run_current_schema_compatibility:
            current_schema_compatibility = {
                "requested": True,
                **_current_schema_compatibility_smoke(
                    code_root, restored_database, isolation_root
                ),
            }
        checks["current_schema_compatibility_passed"] = (
            current_schema_compatibility["passed"]
        )

        container_smoke = {
            "requested": run_container_smoke,
            "build_passed": False,
            "startup_smoke_passed": False,
            "network_mode": "not_run",
            "subprocess_output_retained": False,
        }
        if run_container_smoke:
            container_smoke = {
                "requested": True,
                **_container_smoke(
                    code_root,
                    restored_database,
                    isolation_root / "container-data",
                    image_tag,
                ),
            }
        checks["container_build_passed"] = container_smoke["build_passed"]
        checks["container_startup_smoke_passed"] = container_smoke[
            "startup_smoke_passed"
        ]
        checks["container_network_disabled"] = (
            container_smoke.get("network_mode") == "none"
        )
        acceptance = recovery_acceptance(
            checks,
            container_smoke_requested=run_container_smoke,
            current_schema_compatibility_requested=run_current_schema_compatibility,
        )

    return {
        "manifest_version": "financial-baseline-recovery-v1",
        "verified_at_utc": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "revision": {
            "requested": revision,
            "commit": resolved_commit,
            "tree": resolved_tree,
            "tracked_file_count": tracked_count,
            "archive_file_count": extracted_file_count,
            "missing_required_files": missing_code_files,
            "forbidden_runtime_paths_found": forbidden_runtime_paths,
        },
        "database": {
            "backup_filename": backup.name,
            "backup_sha256": str(manifest["backup_sha256"]),
            "schema_sha256": restored_snapshot["schema_sha256"],
            "table_count": len(restored_snapshot["tables"]),
            "index_count": len(restored_snapshot["indexes"]),
            "row_counts_match": not bool(
                database_differences.get("row_counts")
            ),
            "differences": database_differences,
            "integrity_check": restored_snapshot["integrity_check"],
            "readonly_queries": restored_snapshot["readonly_queries"],
            "restore_method": "sqlite3.Connection.backup",
            "restore_elapsed_seconds": round(restore_elapsed_seconds, 3),
            "restore_rto_target_seconds": 300,
            "restore_rto_passed": checks["database_restore_rto_passed"],
            "live_database_modified": False,
        },
        "configuration": {
            "summary_sha256": config_sha256,
            "configuration_key_count": len(
                config_payload.get("configuration_keys") or []
            ),
            "boolean_setting_count": len(config_payload.get("boolean_settings") or []),
            "endpoint_host_count": len(config_payload.get("endpoint_hosts") or []),
            "raw_secret_values_recorded": False,
        },
        "host_application_smoke": host_smoke,
        "current_schema_compatibility": current_schema_compatibility,
        "container_smoke": container_smoke,
        "isolation": {
            "temporary_directory_removed_after_run": True,
            "git_worktree_modified": False,
            "live_database_modified": False,
            "live_containers_restarted": False,
            "subprocess_output_retained": False,
        },
        "acceptance": acceptance,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Verify the frozen baseline recovery")
    parser.add_argument("--repository", default=".")
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument(
        "--database-manifest", default="baseline/database-backup-manifest.json"
    )
    parser.add_argument("--backup")
    parser.add_argument("--container-smoke", action="store_true")
    parser.add_argument("--current-schema-compatibility", action="store_true")
    parser.add_argument(
        "--image-tag", default="firecrawlapp-crawler:financial-baseline-recovery"
    )
    parser.add_argument("--output", default="baseline/recovery-acceptance.json")
    args = parser.parse_args(argv)
    report = verify_baseline_recovery(
        args.repository,
        revision=args.revision,
        database_manifest=args.database_manifest,
        backup_path=args.backup,
        run_container_smoke=args.container_smoke,
        run_current_schema_compatibility=args.current_schema_compatibility,
        image_tag=args.image_tag,
    )
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "revision": report["revision"],
                "database": {
                    key: report["database"][key]
                    for key in (
                        "schema_sha256",
                        "table_count",
                        "index_count",
                        "row_counts_match",
                    )
                },
                "host_application_smoke": report["host_application_smoke"],
                "current_schema_compatibility": report[
                    "current_schema_compatibility"
                ],
                "container_smoke": report["container_smoke"],
                "acceptance": report["acceptance"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["acceptance"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

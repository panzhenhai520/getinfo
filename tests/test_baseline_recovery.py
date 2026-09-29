#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.verify_baseline_recovery import (
    _minimal_smoke_environment,
    _safe_extract_archive,
    recovery_acceptance,
    snapshot_differences,
)


class BaselineRecoveryTests(unittest.TestCase):
    def test_snapshot_comparison_ignores_sqlite_internal_schema_counter(self):
        expected = {
            "integrity_ok": True,
            "schema_version": 1,
            "user_version": 0,
            "schema_sha256": "schema",
            "tables": ["articles"],
            "indexes": [],
            "row_counts": {"articles": 3},
            "readonly_queries": {"articles": "passed"},
            "readonly_queries_ok": True,
        }
        actual = {**expected, "schema_version": 999}
        self.assertEqual(snapshot_differences(expected, actual), {})
        actual["row_counts"] = {"articles": 2}
        self.assertIn("row_counts", snapshot_differences(expected, actual))

    def test_safe_archive_rejects_parent_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_path = root / "unsafe.tar"
            with tarfile.open(archive_path, "w") as archive:
                payload = b"escape"
                member = tarfile.TarInfo("../escape.txt")
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
            destination = root / "destination"
            destination.mkdir()
            with self.assertRaises(ValueError):
                _safe_extract_archive(archive_path, destination)
            self.assertFalse((root / "escape.txt").exists())

    def test_smoke_environment_does_not_inherit_secrets(self):
        with patch.dict(
            os.environ,
            {
                "PATH": "/usr/bin",
                "SECRET_TOKEN": "must-not-propagate",
                "RAGFLOW_API_KEY": "must-not-propagate",
            },
            clear=True,
        ):
            environment = _minimal_smoke_environment(Path("/tmp/recovery.sqlite3"))
        self.assertNotIn("SECRET_TOKEN", environment)
        self.assertNotIn("RAGFLOW_API_KEY", environment)
        self.assertEqual(environment["RAGFLOW_LLM_ENABLED"], "false")
        self.assertEqual(environment["DATABASE_PATH"], "/tmp/recovery.sqlite3")

    def test_container_smoke_is_required_when_requested(self):
        required_checks = {
            "revision_resolved": True,
            "archive_matches_revision": True,
            "required_code_files_present": True,
            "runtime_files_excluded": True,
            "backup_checksum_matches": True,
            "database_restore_matches_manifest": True,
            "database_restore_rto_passed": True,
            "database_integrity_passed": True,
            "readonly_queries_passed": True,
            "config_summary_matches_revision": True,
            "config_summary_is_sanitized": True,
            "host_application_smoke_passed": True,
            "container_build_passed": True,
            "container_startup_smoke_passed": False,
            "container_network_disabled": True,
        }
        self.assertFalse(
            recovery_acceptance(
                required_checks, container_smoke_requested=True
            )["passed"]
        )
        self.assertTrue(
            recovery_acceptance(
                required_checks, container_smoke_requested=False
            )["passed"]
        )


if __name__ == "__main__":
    unittest.main()

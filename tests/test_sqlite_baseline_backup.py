#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.backup_sqlite_baseline import (
    create_baseline_backup,
    verify_backup_manifest,
)


class SQLiteBaselineBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "source.sqlite3"
        self.connection = sqlite3.connect(str(self.source))
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE articles (id INTEGER PRIMARY KEY, title TEXT NOT NULL);
            CREATE TABLE chat_history (id INTEGER PRIMARY KEY, message TEXT);
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE);
            CREATE INDEX idx_articles_title ON articles(title);
            INSERT INTO articles(title) VALUES ('market report');
            INSERT INTO chat_history(message) VALUES ('historical question');
            INSERT INTO users(username) VALUES ('analyst');
            """
        )
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        self.temp_dir.cleanup()

    def test_online_backup_restores_wal_data_and_matches_schema_and_counts(self):
        backup = self.root / "backup.sqlite3"
        report = create_baseline_backup(self.source, backup)
        self.assertTrue(report["acceptance"]["passed"], report["acceptance"])
        self.assertEqual(report["backup_method"], "sqlite3.Connection.backup")
        self.assertEqual(report["backup_snapshot"]["row_counts"]["articles"], 1)
        self.assertEqual(
            report["source_snapshot"]["schema_sha256"],
            report["restore_snapshot"]["schema_sha256"],
        )
        self.assertTrue(
            report["acceptance"]["readonly_dashboard_history_queries_passed"]
        )

    def test_manifest_verification_detects_backup_mutation(self):
        backup = self.root / "backup.sqlite3"
        manifest = self.root / "manifest.json"
        report = create_baseline_backup(self.source, backup)
        manifest.write_text(json.dumps(report), encoding="utf-8")
        self.assertTrue(verify_backup_manifest(manifest)["valid"])
        with backup.open("ab") as stream:
            stream.write(b"changed")
        verification = verify_backup_manifest(manifest)
        self.assertFalse(verification["valid"])
        self.assertIn("backup_sha256", verification["differences"])

    def test_refuses_missing_empty_and_existing_targets(self):
        with self.assertRaises(ValueError):
            create_baseline_backup(self.root / "missing.sqlite3", self.root / "x.db")
        target = self.root / "existing.sqlite3"
        target.write_bytes(b"do not overwrite")
        with self.assertRaises(FileExistsError):
            create_baseline_backup(self.source, target)
        self.assertEqual(target.read_bytes(), b"do not overwrite")


if __name__ == "__main__":
    unittest.main()

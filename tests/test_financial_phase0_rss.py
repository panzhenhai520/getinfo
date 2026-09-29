#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout

from tools.audit_financial_rss import (
    RSSAuditError,
    audit_rss_database,
    format_text_report,
    main,
)


class FinancialRSSAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = os.path.join(self.temp_dir.name, "rss-audit.sqlite3")
        connection = sqlite3.connect(self.database_path)
        connection.executescript(
            """
            CREATE TABLE intel_sources (
                id INTEGER PRIMARY KEY, source_name TEXT, source_url TEXT,
                source_type TEXT, market TEXT, authority_level INTEGER,
                is_enabled INTEGER, enabled_is_manual INTEGER,
                polling_interval_minutes INTEGER, last_scan_at TEXT,
                last_successful_scan_at TEXT, last_scan_status TEXT,
                last_scan_error TEXT, consecutive_scan_failures INTEGER
            );
            CREATE TABLE intel_source_industries (
                source_id INTEGER, industry_pack_id TEXT
            );
            CREATE TABLE intel_scan_runs (
                id INTEGER PRIMARY KEY, source_id INTEGER, status TEXT,
                started_at TEXT, completed_at TEXT, created_at TEXT
            );
            CREATE TABLE intel_candidate_observations (
                id INTEGER PRIMARY KEY, candidate_id INTEGER, source_id INTEGER
            );
            CREATE TABLE intel_candidates (
                id INTEGER PRIMARY KEY, article_id INTEGER
            );
            CREATE TABLE article_intel_classifications (
                id INTEGER PRIMARY KEY, article_id INTEGER, industry_pack_id TEXT
            );
            """
        )
        sources = [
            (1, "registered", "https://example.com/one.xml", 0, "", ""),
            (2, "failed", "https://example.com/two.xml?api_key=private-value", 1, "failed", "api_key=private-value timeout"),
            (3, "candidate", "https://example.com/three.xml", 1, "completed", ""),
            (4, "article", "https://example.com/four.xml", 1, "completed", ""),
            (5, "classified", "https://example.com/five.xml", 1, "completed", ""),
        ]
        for source_id, name, url, enabled, status, error in sources:
            connection.execute(
                """INSERT INTO intel_sources VALUES (
                    ?,?,?, 'rss','HK',5,?,0,1440,NULL,NULL,?,?,0
                )""",
                (source_id, name, url, enabled, status, error),
            )
            connection.execute(
                "INSERT INTO intel_source_industries VALUES (?, 'financial_markets')",
                (source_id,),
            )
        for source_id, status in ((2, "failed"), (3, "completed"), (4, "completed"), (5, "completed")):
            connection.execute(
                "INSERT INTO intel_scan_runs VALUES (?,?,?,?,?,?)",
                (source_id, source_id, status, "2026-07-31T00:00:00Z", "2026-07-31T00:01:00Z", "2026-07-31T00:00:00Z"),
            )
        connection.executemany(
            "INSERT INTO intel_candidates VALUES (?,?)",
            ((30, None), (40, 400), (50, 500)),
        )
        connection.executemany(
            "INSERT INTO intel_candidate_observations VALUES (?,?,?)",
            ((30, 30, 3), (40, 40, 4), (50, 50, 5)),
        )
        connection.execute(
            "INSERT INTO article_intel_classifications VALUES (1,500,'financial_markets')"
        )
        connection.commit()
        connection.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_distinguishes_each_rss_lifecycle_without_writing(self):
        before = os.stat(self.database_path)
        report = audit_rss_database(self.database_path)
        after = os.stat(self.database_path)
        states = {source["id"]: source["lifecycle_state"] for source in report["sources"]}
        self.assertEqual(states[1], "registered_disabled")
        self.assertEqual(states[2], "scan_failed")
        self.assertEqual(states[3], "candidates_not_ingested")
        self.assertEqual(states[4], "ingested_unclassified")
        self.assertEqual(states[5], "classified")
        self.assertTrue(report["read_only"])
        self.assertEqual(before.st_size, after.st_size)
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)

    def test_redacts_sensitive_url_and_error_values(self):
        report = audit_rss_database(self.database_path)
        failed = next(item for item in report["sources"] if item["id"] == 2)
        self.assertIn("api_key=REDACTED", failed["url"])
        self.assertNotIn("private-value", json.dumps(report))

    def test_text_and_json_cli_reports_are_stable(self):
        report = audit_rss_database(self.database_path)
        text = format_text_report(report)
        self.assertIn("#1 registered: registered_disabled", text)
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["--database", self.database_path, "--json"])
        self.assertEqual(exit_code, 0)
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["summary"]["rss_source_count"], 5)

    def test_missing_schema_fails_explicitly(self):
        empty_path = os.path.join(self.temp_dir.name, "empty.sqlite3")
        sqlite3.connect(empty_path).close()
        with self.assertRaises(RSSAuditError):
            audit_rss_database(empty_path)


if __name__ == "__main__":
    unittest.main()

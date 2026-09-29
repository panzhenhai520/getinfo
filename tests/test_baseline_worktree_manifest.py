#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.capture_baseline_worktree import capture_manifest, verify_manifest


class BaselineWorktreeManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self._git("init", "-q")
        self._git("config", "user.name", "Baseline Test")
        self._git("config", "user.email", "baseline@example.invalid")
        (self.root / ".gitignore").write_text(".env\ndata/\n", encoding="utf-8")
        (self.root / "alpha.txt").write_text("alpha\n", encoding="utf-8")
        (self.root / "beta.txt").write_text("beta\n", encoding="utf-8")
        self._git("add", ".gitignore", "alpha.txt", "beta.txt")
        self._git("commit", "-q", "-m", "fixture")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_clean_capture_hashes_and_self_artifact_verification(self):
        output = self.root / "baseline" / "worktree-manifest.json"
        report = capture_manifest(self.root, output_path=output, sample_size=2)
        self.assertTrue(report["worktree_clean"])
        self.assertEqual(report["status_counts"]["total"], 0)
        self.assertEqual(report["tracked_file_count"], 3)
        self.assertEqual(len(report["sample_hashes"]), 2)
        output.parent.mkdir()
        output.write_text(json.dumps(report), encoding="utf-8")
        self.assertTrue(verify_manifest(output)["valid"])

    def test_dirty_file_is_counted_and_requires_review(self):
        (self.root / "alpha.txt").write_text("changed\n", encoding="utf-8")
        report = capture_manifest(self.root, sample_size=2)
        self.assertFalse(report["worktree_clean"])
        self.assertEqual(report["status_counts"]["modified"], 1)
        self.assertEqual(
            report["status_entries"][0]["disposition"],
            "requires_review_before_baseline",
        )


if __name__ == "__main__":
    unittest.main()

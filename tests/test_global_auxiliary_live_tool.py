#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class GlobalAuxiliaryLiveToolTest(unittest.TestCase):
    def test_default_run_is_offline_sanitized_and_does_not_probe_network(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "acceptance.json"
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/check_global_auxiliary_providers.py",
                    "--output",
                    str(output),
                ],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(report["passed"])
            self.assertFalse(report["live_acceptance"]["requested"])
            self.assertTrue(report["live_acceptance"]["skipped"])
            self.assertFalse(report["secrets_included"])
            encoded = json.dumps(report, ensure_ascii=False).casefold()
            self.assertNotIn("api_key=", encoded)
            self.assertNotIn("token=", encoded)


if __name__ == "__main__":
    unittest.main()

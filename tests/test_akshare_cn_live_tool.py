import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AKShareCNLiveToolTest(unittest.TestCase):
    def test_default_invocation_never_runs_live_network(self):
        result = subprocess.run(
            [sys.executable, "tools/check_akshare_cn_live.py"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(payload["skipped"])
        self.assertFalse(payload["passed"])
        self.assertEqual(
            payload["reason"], "network_disabled_without_explicit_live_flag"
        )


if __name__ == "__main__":
    unittest.main()

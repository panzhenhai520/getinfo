import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "check_tushare_cn_live.py"


class TushareCNLiveToolTest(unittest.TestCase):
    def _run(self, *arguments):
        environment = dict(os.environ)
        environment.pop("TUSHARE_TOKEN", None)
        return subprocess.run(
            [sys.executable, str(TOOL), *arguments],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_default_and_live_without_token_never_access_network(self):
        offline = self._run()
        self.assertEqual(offline.returncode, 0, offline.stderr)
        self.assertEqual(
            json.loads(offline.stdout)["reason"],
            "network_disabled_without_explicit_live_flag",
        )
        no_token = self._run("--live")
        self.assertEqual(no_token.returncode, 0, no_token.stderr)
        report = json.loads(no_token.stdout)
        self.assertEqual(report["reason"], "tushare_token_not_configured")
        self.assertFalse(report["token_configured"])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Run isolated core and long-financial lanes from the same application image."""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Sequence

from financial_resource_isolation import (
    CORE_WORKER_JOB_TYPES,
    LONG_FINANCIAL_JOB_TYPES,
)


ROOT = Path(__file__).resolve().parent


def worker_lane_commands(
    *, python_executable: str = sys.executable, worker_script: Path | str | None = None
) -> Dict[str, List[str]]:
    script = str(Path(worker_script or ROOT / "intel_worker.py").resolve())
    core = [str(python_executable), script]
    for job_type in CORE_WORKER_JOB_TYPES:
        core.extend(["--job-type", job_type])
    long_financial = [str(python_executable), script, "--no-periodic-scheduler"]
    for job_type in LONG_FINANCIAL_JOB_TYPES:
        long_financial.extend(["--job-type", job_type])
    return {"core": core, "long_financial": long_financial}


class WorkerLaneSupervisor:
    def __init__(self, commands: Dict[str, Sequence[str]] | None = None):
        self.commands = {
            key: list(value)
            for key, value in (commands or worker_lane_commands()).items()
        }
        self.children: Dict[str, subprocess.Popen] = {}
        self.stopping = False

    def request_stop(self, *_args) -> None:
        self.stopping = True
        for child in tuple(self.children.values()):
            if child.poll() is None:
                child.terminate()

    def run(self) -> int:
        lane_commands = list(self.commands.items())
        for index, (lane, command) in enumerate(lane_commands):
            self.children[lane] = subprocess.Popen(command, cwd=str(ROOT))
            # Both lanes seed the shared financial universe during startup.
            # Starting them in the same millisecond can exceed SQLite's busy
            # timeout and put the supervisor into a restart loop.
            if index + 1 < len(lane_commands):
                time.sleep(3)
        try:
            while not self.stopping:
                for lane, child in self.children.items():
                    return_code = child.poll()
                    if return_code is not None:
                        self.request_stop()
                        return int(return_code or 1)
                time.sleep(0.5)
            return 0
        finally:
            self.request_stop()
            deadline = time.monotonic() + 10
            for child in self.children.values():
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    child.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    child.kill()
            for child in self.children.values():
                child.wait()


def main() -> int:
    supervisor = WorkerLaneSupervisor()
    signal.signal(signal.SIGTERM, supervisor.request_stop)
    signal.signal(signal.SIGINT, supervisor.request_stop)
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())

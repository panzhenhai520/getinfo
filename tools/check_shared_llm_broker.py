#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Sanitized offline identity check and opt-in live SharedLLMBroker probe."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared_llm_broker import SharedLLMBroker, SharedLLMBrokerError  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402


def run(*, live: bool, timeout_seconds: int) -> dict:
    if not live:
        identity = SharedLLMBroker().runtime_identity()
        return {
            "mode": "offline_identity",
            "network_attempted": False,
            "passed": bool(identity.get("base_url") and identity.get("model_id")),
            "identity": identity,
        }

    with tempfile.TemporaryDirectory(prefix="shared-llm-broker-") as directory:
        database = SQLiteDatabase(str(Path(directory) / "probe.sqlite3"))
        if not database.connect() or not database.create_tables():
            return {
                "mode": "live_probe",
                "network_attempted": False,
                "passed": False,
                "error_code": "probe_database_unavailable",
            }
        try:
            probe = SharedLLMBroker(database.connection).probe(
                timeout_seconds=timeout_seconds
            )
            return {
                "mode": "live_probe",
                "network_attempted": True,
                "passed": bool(probe.pop("ready", False)),
                "probe": probe,
            }
        except SharedLLMBrokerError as exc:
            return {
                "mode": "live_probe",
                "network_attempted": True,
                "passed": False,
                "error_code": exc.error_code,
            }
        finally:
            database.disconnect()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Send one bounded completion to the currently configured local model.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=15)
    args = parser.parse_args(argv)
    report = run(live=args.live, timeout_seconds=max(1, min(args.timeout_seconds, 30)))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Read-only verification for an industry-pack SQLite recovery backup."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from industry_pack_activation import verify_sqlite_backup


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("backup")
    parser.add_argument("--sha256", default="")
    parser.add_argument("--size", type=int)
    parser.add_argument("--schema-version", type=int)
    args = parser.parse_args()
    result = verify_sqlite_backup(
        args.backup,
        expected_sha256=args.sha256,
        expected_size=args.size,
        expected_schema_version=args.schema_version,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

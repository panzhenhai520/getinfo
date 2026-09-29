#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Synchronize legacy URL/task records into the market-intelligence registry."""

from __future__ import annotations

import argparse
import json
import os
import sys


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config
from intel_sources import intel_source_registry


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync legacy URLs to intel_sources")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report changes and roll them back")
    mode.add_argument("--apply", action="store_true", help="commit the reported changes")
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument(
        "--industry-pack-id",
        default=config.INTEL_DEFAULT_INDUSTRY_PACK,
    )
    args = parser.parse_args()

    report = intel_source_registry.sync_legacy_sources(
        dry_run=not args.apply,
        page_size=args.batch_size,
        default_industry_pack_id=args.industry_pack_id,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["errors"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

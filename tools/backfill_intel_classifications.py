#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Queue historical articles for market-intelligence classification."""

from __future__ import annotations

import argparse
import json
import os
import sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import config
from industry_packs import industry_pack_loader
from intel_database import intel_repository
from sqlite_database import sqlite_db


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--industry", default=config.INTEL_DEFAULT_INDUSTRY_PACK)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--after-id", type=int, default=0)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    industry_pack_loader.load(args.industry)
    sqlite_db.connect()
    if args.apply:
        sqlite_db.create_tables()
    rows = intel_repository.list_unclassified_articles(
        args.industry,
        after_id=max(0, args.after_id),
        limit=max(1, min(args.limit, args.batch_size, 1000)),
    )
    result = {
        "industry_pack_id": args.industry,
        "mode": "apply" if args.apply else "dry-run",
        "selected": len(rows),
        "queued": 0,
        "deduplicated": 0,
        "last_article_id": rows[-1]["id"] if rows else args.after_id,
    }
    if args.apply:
        for row in rows:
            _job_id, created = intel_repository.enqueue_classification(
                row["id"],
                args.industry,
            )
            if created:
                result["queued"] += 1
            else:
                result["deduplicated"] += 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

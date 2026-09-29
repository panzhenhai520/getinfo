#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Idempotently register and explicitly enable financial RSS sources."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industry_packs import IndustryPackLoader
import config
from financial_source_license import require_rss_authorization
from intel_sources import IntelSourceRegistry, canonicalize_source_url
from sqlite_database import SQLiteDatabase


def configure_sources(database_path: str, *, pack_id: str, apply: bool) -> dict:
    db = SQLiteDatabase(database_path)
    if not db.connect():
        raise RuntimeError(f"无法连接数据库：{database_path}")
    try:
        registry = IntelSourceRegistry(db)
        pack = IndustryPackLoader().load(pack_id)
        specs = [item for item in pack.get("default_sources") or [] if item.get("source_type") == "rss"]
        license_decisions = {
            canonicalize_source_url(spec["url"]): require_rss_authorization(
                spec["url"], str(spec.get("license_profile") or ""), config
            )
            for spec in specs
        }
        before = {}
        for spec in specs:
            canonical = canonicalize_source_url(spec["url"])
            row = db.connection.execute(
                "SELECT id FROM intel_sources WHERE canonical_source_url=?", (canonical,)
            ).fetchone()
            before[canonical] = registry.get_source(int(row["id"])) if row else None

        ensured = {"sources_added": 0, "sources_attached": 0}
        activated = []
        skipped_manual_disabled = []
        if apply:
            ensured = registry.ensure_pack_default_sources(pack_id)
            for spec in specs:
                canonical = canonicalize_source_url(spec["url"])
                row = db.connection.execute(
                    "SELECT id FROM intel_sources WHERE canonical_source_url=?", (canonical,)
                ).fetchone()
                if not row:
                    raise RuntimeError(f"默认信源注册失败：{canonical}")
                source = registry.get_source(int(row["id"]))
                if source["enabled_is_manual"] and not source["is_enabled"]:
                    skipped_manual_disabled.append(source["id"])
                    continue
                pack_ids = list(source.get("industry_pack_ids") or [])
                if pack_id not in pack_ids:
                    pack_ids.append(pack_id)
                source = registry.update_source(
                    source["id"],
                    is_enabled=True,
                    industry_pack_ids=pack_ids,
                    authority_level=spec.get("authority_level", 5),
                    polling_interval_minutes=spec.get("polling_interval_minutes", 1440),
                )
                activated.append(source["id"])

        sources = []
        for spec in specs:
            canonical = canonicalize_source_url(spec["url"])
            row = db.connection.execute(
                "SELECT id FROM intel_sources WHERE canonical_source_url=?", (canonical,)
            ).fetchone()
            source = registry.get_source(int(row["id"])) if row else None
            sources.append(
                {
                    "name": spec["name"],
                    "canonical_url": canonical,
                    "license_profile": str(spec.get("license_profile") or ""),
                    "license_decision": license_decisions[canonical],
                    "before": before[canonical],
                    "after": source,
                }
            )
        return {
            "applied": apply,
            "industry_pack_id": pack_id,
            **ensured,
            "activated_source_ids": activated,
            "skipped_manual_disabled_source_ids": skipped_manual_disabled,
            "sources": sources,
        }
    finally:
        db.disconnect()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Configure official financial RSS sources")
    parser.add_argument("--database", default=str(PROJECT_ROOT / "data" / "crawler_articles.db"))
    parser.add_argument("--industry", default="financial_markets")
    parser.add_argument("--apply", action="store_true", help="persist registration and explicit enables")
    args = parser.parse_args(argv)
    report = configure_sources(args.database, pack_id=args.industry, apply=args.apply)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

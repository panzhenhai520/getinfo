#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Validate and transactionally bootstrap immutable seed-pack versions."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from industry_pack_activation import SQLiteActivationBackupService
from industry_pack_admin import (
    IndustryPackAdminService,
    IndustryPackVersionStore,
    _manifest_sha256,
    _manifest_text,
)
from industry_packs import IndustryPackLoader
from intel_http import validate_external_url
from sqlite_database import SQLiteDatabase


def bootstrap(
    database: SQLiteDatabase,
    *,
    pack_ids: list[str],
    apply: bool,
    config_dir: str,
    url_validator=validate_external_url,
    backup_dir: str = "",
) -> dict:
    loader = IndustryPackLoader(config_dir, use_published_store=False)
    store = IndustryPackVersionStore(database)
    admin = IndustryPackAdminService(store, loader, url_validator=url_validator)
    plan = []
    manifests = {}
    for pack_id in pack_ids:
        manifest = admin.validate_manifest(
            pack_id,
            loader.load(pack_id, enabled_only=False, use_published=False),
        )
        manifests[pack_id] = manifest
        digest = _manifest_sha256(manifest)
        latest = store.latest_published(pack_id)
        if latest is None:
            action = "create_seed_version"
        elif str(latest["content_sha256"]) == digest:
            action = "already_current"
        else:
            action = "preserve_existing_custom_version"
        plan.append(
            {
                "industry_pack_id": pack_id,
                "pack_version": str(manifest.get("pack_version") or ""),
                "schema_version": int(manifest.get("schema_version") or 0),
                "source_count": len(manifest.get("default_sources") or []),
                "content_sha256": digest,
                "action": action,
                "existing_version_id": int(latest["id"]) if latest else None,
            }
        )
    creates = [item for item in plan if item["action"] == "create_seed_version"]
    if not apply:
        return {
            "check_version": "industry-pack-seed-bootstrap-v1",
            "dry_run": True,
            "writes_performed": False,
            "plan": plan,
            "create_count": len(creates),
            "passed": True,
        }

    backup = None
    if creates:
        backup = SQLiteActivationBackupService(
            database,
            backup_dir=backup_dir or None,
        ).create(f"seed-bootstrap-{uuid.uuid4().hex}")
        with database.lock:
            cursor = database.connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                for item in creates:
                    pack_id = item["industry_pack_id"]
                    manifest = manifests[pack_id]
                    current = cursor.execute(
                        """
                        SELECT id FROM industry_pack_versions
                        WHERE industry_pack_id=? ORDER BY version_number DESC LIMIT 1
                        """,
                        (pack_id,),
                    ).fetchone()
                    if current:
                        raise RuntimeError(
                            f"{pack_id} 在 dry-run 后出现新发布版本，请重新执行预览"
                        )
                    cursor.execute(
                        """
                        INSERT INTO industry_pack_versions(
                            industry_pack_id, version_number, pack_version,
                            schema_version, parent_version_id, manifest_json,
                            content_sha256, created_by
                        ) VALUES(?, 1, ?, ?, NULL, ?, ?, 'seed-bootstrap')
                        """,
                        (
                            pack_id,
                            str(manifest.get("pack_version") or ""),
                            int(manifest.get("schema_version") or 0),
                            _manifest_text(manifest),
                            item["content_sha256"],
                        ),
                    )
                    item["published_version_id"] = int(cursor.lastrowid)
                    item["action"] = "created_seed_version"
                database.connection.commit()
            except Exception:
                database.connection.rollback()
                raise
            finally:
                cursor.close()
        for item in creates:
            loader.clear_cache(item["industry_pack_id"])
    return {
        "check_version": "industry-pack-seed-bootstrap-v1",
        "dry_run": False,
        "writes_performed": bool(creates),
        "plan": plan,
        "created_count": len(creates),
        "backup": backup,
        "passed": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=str(config.DATABASE_PATH))
    parser.add_argument("--config-dir", default=str(ROOT / "config" / "industry_packs"))
    parser.add_argument("--pack", action="append", dest="packs")
    parser.add_argument("--backup-dir", default="")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    database = SQLiteDatabase(str(Path(args.database).expanduser().resolve()))
    if not database.connect():
        raise RuntimeError("无法连接 SQLite 数据库")
    try:
        loader = IndustryPackLoader(args.config_dir, use_published_store=False)
        pack_ids = args.packs or [item["id"] for item in loader.metadata()]
        result = bootstrap(
            database,
            pack_ids=pack_ids,
            apply=bool(args.apply),
            config_dir=args.config_dir,
            backup_dir=args.backup_dir,
        )
    finally:
        database.disconnect()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

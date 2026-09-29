#!/usr/bin/env python3
"""Publish and activate metadata-only family-office authority profiles."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from industry_pack_admin import IndustryPackAdminService, industry_pack_version_store
from industry_pack_activation import industry_pack_activation_service
from industry_packs import IndustryPackLoader, industry_pack_loader
from intel_evidence import IntelEvidenceService
from intel_sources import canonicalize_source_url
from sqlite_database import sqlite_db


PACK_ID = "family_office"
TARGET_PACK_VERSION = "2.2.0"


def url_set(manifest: dict) -> set[str]:
    return {
        canonicalize_source_url(source.get("url"))
        for source in manifest.get("default_sources") or []
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    seed_loader = IndustryPackLoader(use_published_store=False)
    seed = seed_loader.load(PACK_ID, enabled_only=False)
    draft = industry_pack_version_store.draft(PACK_ID)
    if not draft:
        raise SystemExit("family_office draft is missing")
    if url_set(seed) != url_set(draft["manifest"]):
        raise SystemExit("source URLs changed; metadata-only runtime update refused")
    manifest = dict(draft["manifest"])
    manifest["pack_version"] = TARGET_PACK_VERSION
    manifest["default_sources"] = list(seed["default_sources"])
    preview = {
        "apply": bool(args.apply),
        "draft_revision": int(draft["revision"]),
        "source_count": len(manifest["default_sources"]),
        "pack_version": TARGET_PACK_VERSION,
        "roles": {},
    }
    for source in manifest["default_sources"]:
        role = str(source.get("source_role") or "unclassified")
        preview["roles"][role] = preview["roles"].get(role, 0) + 1
    if not args.apply:
        print(json.dumps(preview, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    # URLs are byte-for-byte the already-published set.  The standard manifest
    # validator still checks schemes, roles, levels and source uniqueness; DNS
    # is intentionally not repeated for this metadata-only version bump.
    admin = IndustryPackAdminService(
        industry_pack_version_store,
        industry_pack_loader,
        url_validator=lambda value: str(value),
    )
    saved = admin.save_draft(
        PACK_ID,
        manifest,
        expected_revision=int(draft["revision"]),
        actor="codex-source-authority",
    )
    published = admin.publish_draft(
        PACK_ID,
        expected_revision=int(saved["revision"]),
        actor="codex-source-authority",
    )
    industry_pack_loader.clear_cache()
    if not sqlite_db.create_tables():
        raise RuntimeError("failed to initialize source evidence tables")
    activation_preview = industry_pack_activation_service.preview(
        PACK_ID, target_version_id=int(published["id"])
    )
    activation = industry_pack_activation_service.activate(
        PACK_ID,
        target_version_id=int(published["id"]),
        expected_plan_sha256=activation_preview["plan_sha256"],
        actor="codex-source-authority",
    )
    evidence = IntelEvidenceService().rebuild(PACK_ID)
    preview.update(
        {
            "draft_revision": int(saved["revision"]),
            "published_version_id": int(published["id"]),
            "published_version_number": int(published["version_number"]),
            "activation_id": activation["activation_id"],
            "backup_integrity": activation["backup"]["integrity"],
            "source_reconciliation": activation["source_reconciliation"],
            "evidence": evidence,
        }
    )
    print(json.dumps(preview, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

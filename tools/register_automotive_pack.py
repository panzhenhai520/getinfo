# -*- coding: utf-8 -*-
"""Register and publish the automotive_industry seed pack as a switchable DB pack.

Mirrors tools/import_automotive_rss_sources.py main() but for the
config/industry_packs/automotive_industry.json seed manifest.
"""
import argparse
import copy
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, str(ROOT))

from industry_pack_admin import IndustryPackAdminService
from intel_api import industry_pack_admin_service

PACK_ID = "automotive_industry"
PACK_PATH = os.path.join(ROOT, "config", "industry_packs", "automotive_industry.json")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    if args.publish and not args.apply:
        parser.error("--publish requires --apply")

    with open(PACK_PATH, encoding="utf-8") as fh:
        seed = json.load(fh)

    draft = industry_pack_admin_service.get_or_create_draft(PACK_ID, actor="codex-auto-register")
    previous_revision = int(draft["revision"])
    print("draft revision before:", previous_revision)

    # Validate only (no DNS side effect beyond normalize): reuse the app validator.
    admin = IndustryPackAdminService(
        industry_pack_admin_service.store,
        industry_pack_admin_service.loader,
        url_validator=lambda value: str(value),
    )
    normalized = admin.validate_manifest(PACK_ID, copy.deepcopy(seed))
    print("validation: PASS — id=%s schema=%s kind=%s sources=%d trend_keywords=%d"
          % (normalized["id"], normalized["schema_version"], normalized.get("pack_kind"),
             len(normalized.get("default_sources") or []), len(normalized.get("trend_keywords") or [])))

    result = {
        "pack_id": PACK_ID,
        "applied": bool(args.apply),
        "published": False,
        "previous_revision": previous_revision,
    }
    if args.apply:
        saved = admin.save_draft(
            PACK_ID,
            normalized,
            expected_revision=previous_revision,
            actor="codex-auto-register",
        )
        result["draft_revision"] = int(saved["revision"])
        print("draft saved, revision:", result["draft_revision"])
        if args.publish:
            published = admin.publish_draft(
                PACK_ID,
                expected_revision=int(saved["revision"]),
                actor="codex-auto-register",
            )
            result["published"] = True
            result["published_version_id"] = int(published["id"])
            result["published_version_number"] = int(published["version_number"])
            print("published version id:", result["published_version_id"],
                  "number:", result["published_version_number"])
    print("RESULT:", json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Publish and activate the precision-search update for the automotive pack."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from industry_collection_runtime import initialize_industry_collection
from industry_pack_activation import industry_pack_activation_service
from industry_pack_admin import IndustryPackAdminService
from industry_packs import unique_normalized_keywords
from intel_api import industry_pack_admin_service
from tools.import_automotive_rss_sources import (
    AUTOMOTIVE_EXACT_TOPIC_KEYWORDS,
    AUTOMOTIVE_SERPAPI_QUERIES,
    AUTOMOTIVE_SERPAPI_QUERY_GATES,
)


PACK_ID = "automotive"
TARGET_PACK_VERSION = "1.1.2"
ACTOR = "codex-automotive-search-precision"


def build_manifest(current: dict) -> dict:
    manifest = copy.deepcopy(current)
    manifest["pack_version"] = TARGET_PACK_VERSION
    manifest["serpapi_queries"] = list(AUTOMOTIVE_SERPAPI_QUERIES)
    manifest["serpapi_query_gates"] = copy.deepcopy(
        AUTOMOTIVE_SERPAPI_QUERY_GATES
    )
    topics = {
        str(topic.get("key") or ""): topic
        for topic in manifest.get("fixed_topics") or []
    }
    missing_topics = sorted(set(AUTOMOTIVE_EXACT_TOPIC_KEYWORDS) - set(topics))
    if missing_topics:
        raise ValueError("汽车行业包缺少固定主题：" + ", ".join(missing_topics))
    exact_keywords = []
    for topic_key, keywords in AUTOMOTIVE_EXACT_TOPIC_KEYWORDS.items():
        topic = topics[topic_key]
        topic["keywords"] = unique_normalized_keywords(
            list(topic.get("keywords") or []) + list(keywords)
        )
        exact_keywords.extend(keywords)
    manifest["expanded_keywords"] = unique_normalized_keywords(
        list(manifest.get("expanded_keywords") or []) + exact_keywords
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--activate", action="store_true")
    args = parser.parse_args()
    if args.publish and not args.apply:
        parser.error("--publish requires --apply")
    if args.activate and not args.publish:
        parser.error("--activate requires --publish")

    draft = industry_pack_admin_service.get_or_create_draft(PACK_ID, actor=ACTOR)
    manifest = build_manifest(draft["manifest"])
    # The sources were already validated when imported. Keep this update
    # limited to keywords and queries instead of performing a second DNS pass.
    admin = IndustryPackAdminService(
        industry_pack_admin_service.store,
        industry_pack_admin_service.loader,
        url_validator=lambda value: str(value),
    )
    normalized = admin.validate_manifest(PACK_ID, manifest)
    result = {
        "pack_id": PACK_ID,
        "target_pack_version": TARGET_PACK_VERSION,
        "query_count": len(normalized["serpapi_queries"]),
        "queries": normalized["serpapi_queries"],
        "draft_revision_before": int(draft["revision"]),
        "diff": admin.diff(PACK_ID, normalized),
        "applied": False,
        "published": False,
        "activated": False,
    }
    if args.apply:
        saved = admin.save_draft(
            PACK_ID,
            normalized,
            expected_revision=int(draft["revision"]),
            actor=ACTOR,
        )
        result["applied"] = True
        result["draft_revision_after"] = int(saved["revision"])
        if args.publish:
            published = admin.publish_draft(
                PACK_ID,
                expected_revision=int(saved["revision"]),
                actor=ACTOR,
            )
            result.update(
                {
                    "published": True,
                    "published_version_id": int(published["id"]),
                    "published_version_number": int(published["version_number"]),
                }
            )
            if args.activate:
                preview = industry_pack_activation_service.preview(
                    PACK_ID,
                    target_version_id=int(published["id"]),
                )
                activation = industry_pack_activation_service.activate(
                    PACK_ID,
                    target_version_id=int(published["id"]),
                    expected_plan_sha256=str(preview["plan_sha256"]),
                    actor=ACTOR,
                )
                initialize_industry_collection(
                    activation,
                    request_id=f"automotive-search-{published['id']}",
                    created_by=ACTOR,
                    dedupe_prefix="automotive-search-precision",
                )
                result.update(
                    {
                        "activated": True,
                        "activation_id": activation["activation_id"],
                        "initial_scan_job_id": activation.get("initial_scan_job_id"),
                        "initial_scan_job_created": activation.get(
                            "initial_scan_job_created"
                        ),
                    }
                )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

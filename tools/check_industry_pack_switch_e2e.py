#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Isolated family-office → education → family-office acceptance check."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from industry_pack_activation import (
    IndustryPackActivationService,
    SQLiteActivationBackupService,
    verify_sqlite_backup,
)
from industry_pack_admin import IndustryPackVersionStore
from industry_pack_runtime import ActiveIndustryCompositionService
from industry_packs import IndustryPackLoader
from intel_database import IntelRepository
from intel_sources import IntelSourceRegistry
from project_keyword_gate import configured_project_keyword_snapshot
from sqlite_database import SQLiteDatabase


def _publish_seed(store, loader, pack_id: str) -> dict:
    seed = loader.load(pack_id, enabled_only=False, use_published=False)
    draft = store.get_or_create_draft(pack_id, seed, actor="e2e")
    published = store.publish_draft(
        pack_id,
        expected_revision=int(draft["revision"]),
        actor="e2e",
    )
    loader.clear_cache(pack_id)
    return published


def _activate(service, version: dict) -> dict:
    pack_id = str(version["industry_pack_id"])
    preview = service.preview(pack_id, target_version_id=int(version["id"]))
    return service.activate(
        pack_id,
        target_version_id=int(version["id"]),
        expected_plan_sha256=str(preview["plan_sha256"]),
        actor="e2e",
    )


def run() -> dict:
    with tempfile.TemporaryDirectory() as temporary_root:
        root = Path(temporary_root)
        database_path = root / "switch-e2e.sqlite3"
        database = SQLiteDatabase(str(database_path))
        if not database.connect() or not database.create_tables():
            raise RuntimeError("无法初始化行业包端到端测试数据库")
        try:
            original_inode = int(database_path.stat().st_ino)
            store = IndustryPackVersionStore(database)
            loader = IndustryPackLoader(
                str(ROOT / "config" / "industry_packs"),
                use_published_store=True,
                published_manifest_provider=store.published_manifest_for_loader,
            )
            registry = IntelSourceRegistry(database, pack_loader=loader)
            repository = IntelRepository(database)
            backup_service = SQLiteActivationBackupService(
                database,
                backup_dir=str(root / "recovery-backups"),
            )
            service = IndustryPackActivationService(
                database,
                version_store=store,
                source_registry=registry,
                repository=repository,
                backup_service=backup_service,
            )
            runtime = ActiveIndustryCompositionService(database, pack_loader=loader)

            family_version = _publish_seed(store, loader, "family_office")
            education_version = _publish_seed(store, loader, "education_news")
            family_activation = _activate(service, family_version)

            old_job_id, _ = repository.enqueue_job(
                "light_scan",
                "e2e-family-job-before-switch",
                {"industry_pack_id": "family_office"},
            )
            article_id = int(
                database.connection.execute(
                    """
                    INSERT INTO articles(
                        url, title, content, domain, publish_date, status,
                        matched_keywords, content_hash, content_length
                    ) VALUES(
                        'https://family.example.test/e2e',
                        '家族办公室切换验收样本',
                        '家族办公室政策与财富传承样本，仅用于隔离测试。',
                        'family.example.test', date('now'), 'active',
                        '家族办公室,财富传承,政策', 'e2e-family-article', 31
                    )
                    """
                ).lastrowid
            )
            database.connection.commit()
            repository.upsert_classification(
                {
                    "article_id": article_id,
                    "industry_pack_id": "family_office",
                    "activation_id": family_activation["activation_id"],
                    "industry_pack_version": family_version["pack_version"],
                    "classifier_version": "e2e",
                    "article_content_hash": "e2e-family-article",
                    "rule_category": "event",
                    "rule_confidence": 1,
                    "rule_reason": "e2e",
                    "score_details": {
                        "hits": {
                            "anchor": ["家族办公室", "财富传承"],
                            "trend": ["政策"],
                        }
                    },
                    "matched_keywords": ["家族办公室", "财富传承", "政策"],
                    "topic_tags": ["政策与税务"],
                    "final_category": "event",
                    "final_confidence": 1,
                    "final_reason": "e2e",
                }
            )
            repository.record_financial_addon_match(
                article_id,
                activation_id=family_activation["activation_id"],
                primary_industry_pack_id="family_office",
                matched_keywords=["家族办公室", "财富传承"],
            )

            education_activation = _activate(service, education_version)
            education_runtime = runtime.snapshot()
            education_keywords = configured_project_keyword_snapshot(
                database.connection,
                pack_loader=loader,
            )
            education_manifest = loader.load("education_news")
            education_visible = repository.list_classified_articles(
                industry_pack_id="education_news",
                time_range="7d",
            )[1]
            raw_family_rows = int(
                database.connection.execute(
                    """
                    SELECT COUNT(*) FROM article_intel_classifications
                    WHERE article_id=? AND industry_pack_id='family_office'
                    """,
                    (article_id,),
                ).fetchone()[0]
            )
            shared_financial_sources = int(
                database.connection.execute(
                    """
                    SELECT COUNT(DISTINCT source_id)
                    FROM intel_source_industries
                    WHERE industry_pack_id='financial_markets'
                      AND ownership_type='shared_financial' AND is_active=1
                    """
                ).fetchone()[0]
            )

            rollback_preview = service.preview_rollback(
                education_activation["activation_id"]
            )
            rollback = service.rollback(
                education_activation["activation_id"],
                target_version_id=int(rollback_preview["target_version_id"]),
                expected_plan_sha256=str(rollback_preview["plan_sha256"]),
                actor="e2e",
            )
            family_runtime = runtime.snapshot()
            family_keywords = configured_project_keyword_snapshot(
                database.connection,
                pack_loader=loader,
            )
            family_visible_after_new_activation = repository.list_classified_articles(
                industry_pack_id="family_office",
                time_range="7d",
            )[1]
            family_policy_visible_after_new_activation = (
                repository.list_classified_articles(
                    industry_pack_id="family_office",
                    time_range="7d",
                    policy_only=True,
                )[1]
            )
            restored_classification = database.connection.execute(
                """
                SELECT matched_keywords_json,topic_tags_json
                FROM article_intel_classifications
                WHERE article_id=? AND industry_pack_id='family_office'
                """,
                (article_id,),
            ).fetchone()
            restored_financial_match = database.connection.execute(
                """
                SELECT is_visible,matched_keywords_json
                FROM financial_addon_article_matches
                WHERE article_id=? AND activation_id=?
                """,
                (article_id, rollback["activation_id"]),
            ).fetchone()
            article_status = str(
                database.connection.execute(
                    "SELECT status FROM articles WHERE id=?", (article_id,)
                ).fetchone()[0]
            )
            activations = service.list_activations(10)
            backup_checks = [
                verify_sqlite_backup(
                    item["backup_path"],
                    expected_sha256=item["backup_sha256"],
                    expected_size=int(item["backup_size"]),
                    expected_schema_version=int(item["backup_schema_version"]),
                )
                for item in activations
            ]
            final_inode = int(database_path.stat().st_ino)
            current_records = [item for item in activations if bool(item["is_current"])]
            education_dashboard = dict(
                education_manifest.get("dashboard_capabilities") or {}
            )
            expected_education_keywords = list(
                dict.fromkeys(
                    list(education_manifest.get("core_keywords") or [])
                    + list(education_manifest.get("expanded_keywords") or [])
                )
            )
            checks = {
                "education_activated_exact_version": (
                    education_runtime["active_industry_pack_id"] == "education_news"
                    and education_runtime["active_industry_pack_version_id"]
                    == int(education_version["id"])
                ),
                "education_effective_composition": education_runtime[
                    "effective_pack_ids"
                ]
                == ["education_news", "financial_markets"],
                "education_keyword_isolation": (
                    education_keywords["keywords"] == expected_education_keywords
                    and "家族办公室" not in education_keywords["keywords"]
                ),
                "non_family_market_cards_hidden": (
                    not education_dashboard.get("show_market_index_cards")
                    and not education_dashboard.get("show_watched_stock_cards")
                ),
                "shared_financial_addon_retained": shared_financial_sources == 7,
                "old_queued_job_cancelled": repository.get_job(old_job_id)["status"]
                == "cancelled",
                "old_article_preserved_but_not_in_new_industry": (
                    raw_family_rows == 1 and education_visible == 0
                ),
                "rollback_restored_exact_family_version": (
                    rollback["active_pack_id"] == "family_office"
                    and family_runtime["active_industry_pack_version_id"]
                    == int(family_version["id"])
                ),
                "rollback_restored_family_keywords": (
                    "家族办公室" in family_keywords["keywords"]
                    and "教育" not in family_keywords["keywords"]
                ),
                "article_rows_never_archived": article_status == "active",
                "family_aggregation_restored_after_return": (
                    raw_family_rows == 1 and family_visible_after_new_activation == 1
                ),
                "family_policy_category_restored_after_return": (
                    family_policy_visible_after_new_activation == 1
                    and restored_classification is not None
                    and "政策" in json.loads(restored_classification[0])
                    and "政策与税务" in json.loads(restored_classification[1])
                ),
                "family_financial_match_restored_after_return": (
                    restored_financial_match is not None
                    and int(restored_financial_match[0]) == 1
                ),
                "normal_rollback_did_not_replace_database_file": original_inode
                == final_inode,
                "all_activation_backups_verified": (
                    len(backup_checks) == 3
                    and all(item["passed"] and item["read_only"] for item in backup_checks)
                ),
                "exactly_one_current_activation": (
                    len(current_records) == 1
                    and current_records[0]["target_pack_id"] == "family_office"
                ),
            }
            return {
                "check_version": "industry-pack-switch-e2e-v1",
                "passed": all(checks.values()),
                "checks": checks,
                "versions": {
                    "family_office": int(family_version["id"]),
                    "education_news": int(education_version["id"]),
                },
                "activations": {
                    "family": family_activation["activation_id"],
                    "education": education_activation["activation_id"],
                    "rollback": rollback["activation_id"],
                    "count": len(activations),
                },
                "source_counts": {
                    "shared_financial": shared_financial_sources,
                },
                "backup_checks": [
                    {
                        "sha256": item["sha256"],
                        "size": item["size"],
                        "schema_version": item["schema_version"],
                        "integrity": item["integrity"],
                        "passed": item["passed"],
                    }
                    for item in backup_checks
                ],
            }
        finally:
            database.disconnect()


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

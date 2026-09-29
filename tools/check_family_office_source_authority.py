#!/usr/bin/env python3
"""Read-only acceptance check for family-office source authority evidence."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from industry_packs import IndustryPackLoader
from source_authority import authority_registry


PACK_ID = "family_office"
TARGET_PACK_VERSION = "2.2.0"


def _database_path() -> Path:
    value = Path(str(config.DATABASE_PATH))
    return value if value.is_absolute() else ROOT / value


def run() -> dict:
    manifest = IndustryPackLoader(use_published_store=False).load(
        PACK_ID, enabled_only=False
    )
    seed_sources = list(manifest.get("default_sources") or [])
    supported_roles = set(authority_registry()["roles"])
    seed_role_counts = Counter(str(item.get("source_role") or "") for item in seed_sources)

    database_path = _database_path().resolve()
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        activation = connection.execute(
            """
            SELECT a.*, v.pack_version
            FROM industry_pack_activations a
            JOIN industry_pack_versions v ON v.id=a.target_version_id
            WHERE a.is_current=1 AND a.status='active'
            ORDER BY a.completed_at DESC LIMIT 1
            """
        ).fetchone()
        if not activation:
            raise RuntimeError("没有当前生效的行业包激活记录")
        activation = dict(activation)
        runtime_sources = connection.execute(
            """
            SELECT s.source_name, s.source_url, s.authority_level, s.metadata_json,
                   s.is_enabled
            FROM intel_source_industries si
            JOIN intel_sources s ON s.id=si.source_id
            WHERE si.industry_pack_id=? AND si.ownership_type='pack_owned'
              AND si.is_active=1
            ORDER BY s.id
            """,
            (PACK_ID,),
        ).fetchall()
        runtime_roles = Counter()
        runtime_missing = []
        for row in runtime_sources:
            metadata = json.loads(row["metadata_json"] or "{}")
            role = str(metadata.get("source_role") or "")
            runtime_roles[role] += 1
            if (
                role not in supported_roles
                or role == "unclassified"
                or not metadata.get("publisher_key")
                or not isinstance(metadata.get("authority_scope"), list)
                or not 1 <= int(row["authority_level"] or 0) <= 5
            ):
                runtime_missing.append(str(row["source_name"]))

        evidence_rows = connection.execute(
            """
            SELECT evidence_grade, conflict_status, COUNT(*) AS group_count,
                   SUM(CASE WHEN independent_source_count > 1 THEN 1 ELSE 0 END)
                     AS multi_source_groups
            FROM intel_evidence_groups
            WHERE industry_pack_id=? AND activation_id=?
            GROUP BY evidence_grade, conflict_status
            """,
            (PACK_ID, activation["id"]),
        ).fetchall()
        evidence_distribution = [dict(row) for row in evidence_rows]
        evidence_group_count = sum(int(row["group_count"]) for row in evidence_rows)
        evidence_member_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM intel_evidence_group_articles ega
                JOIN intel_evidence_groups eg ON eg.id=ega.evidence_group_id
                WHERE eg.industry_pack_id=? AND eg.activation_id=?
                """,
                (PACK_ID, activation["id"]),
            ).fetchone()[0]
        )
        folded_duplicate_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM intel_evidence_group_articles ega
                JOIN intel_evidence_groups eg ON eg.id=ega.evidence_group_id
                WHERE eg.industry_pack_id=? AND eg.activation_id=?
                  AND ega.relationship<>'representative'
                """,
                (PACK_ID, activation["id"]),
            ).fetchone()[0]
        )
    finally:
        connection.close()

    checks = {
        "active_pack_is_family_office": activation["target_pack_id"] == PACK_ID,
        "active_pack_version_is_target": activation["pack_version"]
        == TARGET_PACK_VERSION,
        "backup_integrity_recorded": activation["backup_integrity"] == "ok",
        "seed_version_is_target": manifest["pack_version"] == TARGET_PACK_VERSION,
        "all_seed_sources_have_supported_roles": bool(seed_sources)
        and set(seed_role_counts).issubset(supported_roles)
        and not seed_role_counts.get("")
        and not seed_role_counts.get("unclassified"),
        "all_seed_sources_have_evidence_metadata": all(
            item.get("publisher_key")
            and isinstance(item.get("authority_scope"), list)
            and 1 <= int(item.get("authority_level") or 0) <= 5
            for item in seed_sources
        ),
        "runtime_source_count_matches_seed": len(runtime_sources) == len(seed_sources),
        "runtime_sources_have_evidence_metadata": not runtime_missing,
        "evidence_groups_exist_for_current_activation": evidence_group_count > 0,
        "evidence_members_cover_groups": evidence_member_count >= evidence_group_count,
    }
    result = {
        "success": all(checks.values()),
        "checks": checks,
        "database_path": str(database_path),
        "activation_id": activation["id"],
        "pack_version": activation["pack_version"],
        "seed_source_count": len(seed_sources),
        "seed_role_counts": dict(sorted(seed_role_counts.items())),
        "runtime_source_count": len(runtime_sources),
        "runtime_enabled_source_count": sum(
            1 for row in runtime_sources if bool(row["is_enabled"])
        ),
        "runtime_role_counts": dict(sorted(runtime_roles.items())),
        "runtime_missing_metadata": runtime_missing,
        "evidence_group_count": evidence_group_count,
        "evidence_member_count": evidence_member_count,
        "folded_duplicate_count": folded_duplicate_count,
        "evidence_distribution": evidence_distribution,
    }
    return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

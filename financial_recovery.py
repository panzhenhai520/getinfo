#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Fail-closed recovery policy and data-preservation evidence for stage 6.6.

This module only classifies recovery actions and reads database metadata.  It
does not stop services, change rollout configuration, restore files or delete
data.  Destructive recovery remains an explicitly approved operator action.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable


FINANCIAL_RECOVERY_VERSION = "financial-recovery-v1"

RECOVERY_RTO_TARGETS_SECONDS = {
    "feature_flag_rollback": 5,
    "provider_or_llm_degrade": 5,
    "report_fallback": 5,
    "worker_restart": 60,
    "schema_resume": 120,
    "baseline_code_smoke": 180,
    "isolated_database_restore": 300,
}

RECOVERY_FAULT_POLICIES = {
    "provider_failure": {
        "action": "rollback_feature_flag_and_use_verified_cache",
        "database_action": "preserve_current_database",
        "rto_target": "provider_or_llm_degrade",
        "rpo": "zero_database_rows_lost",
    },
    "llm_failure": {
        "action": "rollback_feature_flag_and_keep_verified_facts",
        "database_action": "preserve_current_database",
        "rto_target": "provider_or_llm_degrade",
        "rpo": "zero_database_rows_lost",
    },
    "schema_interruption": {
        "action": "capture_incident_copy_then_resume_additive_migration",
        "database_action": "preserve_current_database",
        "rto_target": "schema_resume",
        "rpo": "zero_database_rows_lost",
    },
    "report_corruption": {
        "action": "quarantine_damaged_artifact_and_load_previous_valid_version",
        "database_action": "preserve_current_database",
        "rto_target": "report_fallback",
        "rpo": "zero_database_rows_lost",
    },
    "worker_failure": {
        "action": "stop_claiming_then_restart_and_recover_persistent_job",
        "database_action": "preserve_current_database",
        "rto_target": "worker_restart",
        "rpo": "zero_completed_jobs_lost",
    },
    "database_corruption": {
        "action": "restore_approved_backup_to_isolated_path",
        "database_action": "approved_restore_only",
        "rto_target": "isolated_database_restore",
        "rpo": "approved_backup_capture_time",
    },
}

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class FinancialRecoveryError(RuntimeError):
    """Stable recovery-policy failure."""

    def __init__(self, message: str, *, error_code: str):
        super().__init__(message)
        self.error_code = str(error_code)


def recovery_decision(
    fault_type: str,
    *,
    database_integrity_ok: bool,
    database_restore_approved: bool = False,
) -> dict:
    """Return the only permitted recovery class for a known failure.

    A failed integrity check never authorizes a restore by itself.  It changes
    the decision to an approval-required hold unless explicit approval is
    supplied.  Conversely, a healthy database can never be overwritten even
    when an approval flag is accidentally passed.
    """

    normalized = str(fault_type or "").strip().casefold()
    if normalized not in RECOVERY_FAULT_POLICIES:
        raise FinancialRecoveryError(
            "未知金融恢复故障类型", error_code="unknown_recovery_fault"
        )
    policy = dict(RECOVERY_FAULT_POLICIES[normalized])
    restore_requested = normalized == "database_corruption"

    if database_integrity_ok and restore_requested:
        return {
            "version": FINANCIAL_RECOVERY_VERSION,
            "fault_type": normalized,
            **policy,
            "allowed": False,
            "database_restore_allowed": False,
            "reason": "healthy_database_restore_forbidden",
            "requires_explicit_approval": False,
            "preserve_new_legal_data": True,
        }

    if not database_integrity_ok:
        if not database_restore_approved:
            return {
                "version": FINANCIAL_RECOVERY_VERSION,
                "fault_type": normalized,
                **RECOVERY_FAULT_POLICIES["database_corruption"],
                "allowed": False,
                "database_restore_allowed": False,
                "reason": "database_restore_requires_explicit_approval",
                "requires_explicit_approval": True,
                "preserve_new_legal_data": True,
            }
        return {
            "version": FINANCIAL_RECOVERY_VERSION,
            "fault_type": normalized,
            **RECOVERY_FAULT_POLICIES["database_corruption"],
            "allowed": True,
            "database_restore_allowed": True,
            "reason": "database_corruption_restore_approved",
            "requires_explicit_approval": True,
            "preserve_new_legal_data": False,
        }

    return {
        "version": FINANCIAL_RECOVERY_VERSION,
        "fault_type": normalized,
        **policy,
        "allowed": True,
        "database_restore_allowed": False,
        "reason": "non_destructive_recovery",
        "requires_explicit_approval": False,
        "preserve_new_legal_data": True,
    }


def assert_database_restore_allowed(
    *, database_integrity_ok: bool, database_restore_approved: bool
) -> None:
    """Fail closed before any operator/tool enters a database restore path."""

    decision = recovery_decision(
        "database_corruption",
        database_integrity_ok=database_integrity_ok,
        database_restore_approved=database_restore_approved,
    )
    if decision["database_restore_allowed"]:
        return
    raise FinancialRecoveryError(
        "当前数据库不得被恢复备份覆盖", error_code=str(decision["reason"])
    )


def database_fingerprint(connection, *, protected_tables: Iterable[str]) -> dict:
    """Capture schema/integrity/count evidence without reading user values."""

    tables = tuple(dict.fromkeys(str(item) for item in protected_tables))
    if not tables or any(not _SAFE_IDENTIFIER.fullmatch(item) for item in tables):
        raise FinancialRecoveryError(
            "受保护表名无效", error_code="invalid_protected_table"
        )
    integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
    integrity = [str(row[0]) for row in integrity_rows]
    schema_rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    schema_payload = json.dumps(
        [tuple(str(value or "") for value in row) for row in schema_rows],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    present = {str(row[1]) for row in schema_rows if str(row[0]) == "table"}
    missing = [table for table in tables if table not in present]
    if missing:
        raise FinancialRecoveryError(
            "受保护表不存在", error_code="protected_table_missing"
        )
    row_counts = {
        table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in tables
    }
    return {
        "version": FINANCIAL_RECOVERY_VERSION,
        "integrity": integrity,
        "integrity_ok": integrity == ["ok"],
        "schema_sha256": hashlib.sha256(schema_payload).hexdigest(),
        "protected_row_counts": row_counts,
    }


def verify_legal_data_preserved(before: dict, after: dict) -> dict:
    """Require healthy storage and no protected-row loss after recovery."""

    before_counts = dict(before.get("protected_row_counts") or {})
    after_counts = dict(after.get("protected_row_counts") or {})
    missing = sorted(set(before_counts) - set(after_counts))
    decreased = {
        table: {"before": int(count), "after": int(after_counts.get(table, -1))}
        for table, count in before_counts.items()
        if int(after_counts.get(table, -1)) < int(count)
    }
    checks = {
        "integrity_ok": bool(after.get("integrity_ok")),
        "protected_tables_present": not missing,
        "protected_row_counts_not_decreased": not decreased,
    }
    if not all(checks.values()):
        raise FinancialRecoveryError(
            "恢复过程丢失或损坏合法数据", error_code="legal_data_preservation_failed"
        )
    return {
        "passed": True,
        "checks": checks,
        "protected_row_counts": after_counts,
    }

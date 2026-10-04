#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Portable schema for durable unified-QA runs and evidence."""

from __future__ import annotations

import hashlib


QA_SCHEMA_VERSION = "unified-qa-schema-v4"

QA_TABLE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS qa_runs (
        id TEXT PRIMARY KEY,
        contract_version TEXT NOT NULL,
        session_id TEXT NOT NULL DEFAULT '',
        owner_user_id TEXT NOT NULL DEFAULT '',
        industry_pack_id TEXT NOT NULL,
        origin TEXT NOT NULL,
        mode TEXT NOT NULL,
        question_hash TEXT NOT NULL,
        question_text TEXT NOT NULL DEFAULT '',
        request_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL,
        current_stage TEXT NOT NULL DEFAULT 'plan',
        draft_provider_id TEXT NOT NULL DEFAULT '',
        research_app_id TEXT NOT NULL DEFAULT '',
        synthesis_provider_id TEXT NOT NULL DEFAULT '',
        idempotency_key TEXT NOT NULL DEFAULT '',
        job_id INTEGER,
        degraded INTEGER NOT NULL DEFAULT 0,
        degradation_json TEXT NOT NULL DEFAULT '[]',
        final_answer_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT,
        UNIQUE(owner_user_id, idempotency_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS qa_stage_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        stage TEXT NOT NULL,
        attempt INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        latency_ms INTEGER,
        input_hash TEXT NOT NULL DEFAULT '',
        output_hash TEXT NOT NULL DEFAULT '',
        token_usage_json TEXT NOT NULL DEFAULT '{}',
        error_code TEXT NOT NULL DEFAULT '',
        details_json TEXT NOT NULL DEFAULT '{}',
        UNIQUE(run_id, stage, attempt)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS qa_claims (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        claim_key TEXT NOT NULL,
        stage TEXT NOT NULL,
        claim_text TEXT NOT NULL,
        claim_type TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 0,
        valid_from TEXT,
        valid_to TEXT,
        scope_json TEXT NOT NULL DEFAULT '[]',
        verification_status TEXT NOT NULL DEFAULT 'unverified',
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        UNIQUE(run_id, claim_key, stage)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS qa_evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        evidence_ref TEXT NOT NULL,
        source_type TEXT NOT NULL,
        article_id INTEGER,
        ragflow_kb_id TEXT,
        document_id TEXT,
        chunk_id TEXT,
        source_url TEXT NOT NULL DEFAULT '',
        source_title TEXT NOT NULL DEFAULT '',
        published_at TEXT,
        fetched_at TEXT,
        authority_level INTEGER,
        content_hash TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        UNIQUE(run_id, evidence_ref)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS qa_claim_evidence (
        run_id TEXT NOT NULL,
        claim_key TEXT NOT NULL,
        evidence_ref TEXT NOT NULL,
        relationship TEXT NOT NULL,
        relevance_score REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        PRIMARY KEY(run_id, claim_key, evidence_ref, relationship)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS qa_conflicts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        conflict_key TEXT NOT NULL,
        conflict_type TEXT NOT NULL,
        resolution TEXT NOT NULL,
        rationale TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(run_id, conflict_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS qa_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        event_id INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        stage TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(run_id, event_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS qa_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        owner_user_id TEXT NOT NULL DEFAULT '',
        rating INTEGER,
        feedback_text TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE(run_id, owner_user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS qa_provider_profiles (
        profile_id TEXT PRIMARY KEY,
        provider_type TEXT NOT NULL,
        base_url TEXT NOT NULL DEFAULT '',
        model_id TEXT NOT NULL DEFAULT '',
        credential_ref TEXT NOT NULL DEFAULT '',
        proxy_policy TEXT NOT NULL DEFAULT 'provider_default',
        timeout_seconds INTEGER NOT NULL DEFAULT 120,
        enabled INTEGER NOT NULL DEFAULT 1,
        settings_json TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS qa_bridge_assertion_nonces (
        nonce_hash TEXT PRIMARY KEY,
        subject_id TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS qa_bridge_sessions (
        session_hash TEXT PRIMARY KEY,
        subject_id TEXT NOT NULL,
        owner_user_id TEXT NOT NULL,
        industry_pack_id TEXT NOT NULL,
        allowed_kb_ids_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        revoked_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS qa_audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trace_id TEXT NOT NULL DEFAULT '',
        run_id TEXT NOT NULL DEFAULT '',
        event_type TEXT NOT NULL,
        actor_id TEXT NOT NULL DEFAULT '',
        origin TEXT NOT NULL DEFAULT '',
        industry_pack_id TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS qa_health_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        component TEXT NOT NULL,
        status TEXT NOT NULL,
        summary_json TEXT NOT NULL DEFAULT '{}',
        checked_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS qa_retrieval_cache (
        cache_key TEXT PRIMARY KEY,
        namespace TEXT NOT NULL,
        industry_pack_id TEXT NOT NULL DEFAULT '',
        kb_version TEXT NOT NULL DEFAULT '',
        scope_hash TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS qa_circuit_states (
        dependency TEXT PRIMARY KEY,
        state TEXT NOT NULL,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        opened_at TEXT,
        probe_after TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS qa_rate_limit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bucket_key TEXT NOT NULL,
        event_at TEXT NOT NULL,
        weight INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS qa_feature_flags (
        flag_key TEXT PRIMARY KEY,
        value_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        updated_by TEXT NOT NULL DEFAULT ''
    )
    """,
)

QA_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_qa_runs_owner_created ON qa_runs(owner_user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_qa_runs_pack_status ON qa_runs(industry_pack_id, status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_qa_runs_job ON qa_runs(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_qa_stage_run ON qa_stage_runs(run_id, stage, attempt)",
    "CREATE INDEX IF NOT EXISTS idx_qa_claim_run ON qa_claims(run_id, verification_status)",
    "CREATE INDEX IF NOT EXISTS idx_qa_evidence_run ON qa_evidence(run_id, source_type)",
    "CREATE INDEX IF NOT EXISTS idx_qa_events_resume ON qa_events(run_id, event_id)",
    "CREATE INDEX IF NOT EXISTS idx_qa_bridge_sessions_owner ON qa_bridge_sessions(owner_user_id, expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_qa_audit_trace ON qa_audit_events(trace_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_qa_audit_pack ON qa_audit_events(industry_pack_id, event_type, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_qa_health_component ON qa_health_snapshots(component, checked_at)",
    "CREATE INDEX IF NOT EXISTS idx_qa_cache_expiry ON qa_retrieval_cache(namespace, expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_qa_rate_bucket ON qa_rate_limit_events(bucket_key, event_at)",
)

QA_REQUIRED_TABLES = frozenset(
    {
        "qa_runs",
        "qa_stage_runs",
        "qa_claims",
        "qa_evidence",
        "qa_claim_evidence",
        "qa_conflicts",
        "qa_events",
        "qa_feedback",
        "qa_provider_profiles",
        "qa_bridge_assertion_nonces",
        "qa_bridge_sessions",
        "qa_audit_events",
        "qa_health_snapshots",
        "qa_retrieval_cache",
        "qa_circuit_states",
        "qa_rate_limit_events",
        "qa_feature_flags",
    }
)


def qa_schema_checksum() -> str:
    payload = "\n".join(sql.strip() for sql in QA_TABLE_DDL + QA_INDEX_DDL)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ensure_qa_tables(cursor) -> None:
    for statement in QA_TABLE_DDL:
        cursor.execute(statement)
    for statement in QA_INDEX_DDL:
        cursor.execute(statement)


__all__ = [
    "QA_INDEX_DDL",
    "QA_REQUIRED_TABLES",
    "QA_SCHEMA_VERSION",
    "QA_TABLE_DDL",
    "ensure_qa_tables",
    "qa_schema_checksum",
]

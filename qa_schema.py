#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Portable schema for durable unified-QA runs and evidence."""

from __future__ import annotations

import hashlib


# v5 → v6（graph-rag-v2 通用包 Phase 01 · F-5/F-7/F-8）：
#   · qa_runs 加版本四元组（corpus_version / model_version / prompt_version / config_hash）；
#   · qa_reasoning_traces 补 SearchTrace 字段（gap_id / route / results /
#     accepted / rejected / new_claims / resolved_gap）；
#   · qa_stage_runs 补 node-run 载体（node_id / node_kind / parent_node_id）与 round_index。
# 老库升级一律 ADD COLUMN + DEFAULT（见 QA_ADDED_COLUMNS_V6），既有列语义一个字不改。
#
# v6 → v7（graph-rag-v2 通用包 Phase 02 · P02-03）：
#   · 新增 **一张表** qa_evidence_seen（证据 seen/confirmed/rejected 身份，按
#     owner_user_id / session_id / industry_pack_id 作用域隔离，跨轮 + 跨 run 去重）；
#   · **没有新增列**：新表走 `CREATE TABLE IF NOT EXISTS`，老库执行 ensure_qa_tables 时
#     自动补建，不需要 ADD COLUMN，也就不存在"老库有列新库没有"的漂移风险。
#   · 回滚：`DROP TABLE qa_evidence_seen`（外加把本常量改回 v6）即可，既有表一字不动；
#     代码侧还有 QA_EVIDENCE_LAYER_ENABLED=0 一键回到 Phase 01 行为。
#
# v7 → v8（graph-rag-v2 通用包 Phase 09 · P09-01）：
#   · 新增 **八张表**（跨会话记忆图，§12）：memory_item / memory_version /
#     memory_entity_link / memory_evidence_link / memory_relation / memory_recall_log /
#     memory_write_decision / memory_usage_stat；
#   · **没有新增列、没有改既有列语义**：老库执行 ensure_qa_tables 时按
#     `CREATE TABLE IF NOT EXISTS` 自动补建八张新表（`QA_ADDED_COLUMNS_V8` 为空元组，
#     与 Phase 02 同一手法），既有 QA 表一个字不动；
#   · §12 的 `memory_validation` / `memory_contradiction` **刻意不在本阶段建**：
#     它们是 Phase 10（revalidation / 冲突 / supersession）的账，提前建空表等于把
#     后续阶段的形态写死（见 DECISION_LOG D-033）。同理 `skill_performance_memory` /
#     `source_reliability_memory` 属 Phase 12。
#   · 回滚：`DROP TABLE memory_usage_stat, memory_write_decision, memory_recall_log,
#     memory_relation, memory_evidence_link, memory_entity_link, memory_version,
#     memory_item;` + 本常量改回 v7；代码侧 `QA_MEMORY_GRAPH=0`（默认）即回到 Phase 08 行为。
QA_SCHEMA_VERSION = "unified-qa-schema-v8"

QA_TABLE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS qa_reasoning_traces (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        hop_index INTEGER NOT NULL DEFAULT 0,
        sub_query_id TEXT NOT NULL DEFAULT '',
        sub_query TEXT NOT NULL DEFAULT '',
        depends_on_json TEXT NOT NULL DEFAULT '[]',
        partial_answer TEXT NOT NULL DEFAULT '',
        used_evidence_refs_json TEXT NOT NULL DEFAULT '[]',
        missing_links_json TEXT NOT NULL DEFAULT '[]',
        next_queries_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT '',
        round_index INTEGER NOT NULL DEFAULT 0,
        latency_ms INTEGER NOT NULL DEFAULT 0,
        gap_id TEXT DEFAULT '',
        route TEXT DEFAULT '',
        results INTEGER DEFAULT 0,
        accepted INTEGER DEFAULT 0,
        rejected INTEGER DEFAULT 0,
        new_claims INTEGER DEFAULT 0,
        resolved_gap INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(run_id, round_index, hop_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS qa_session_constraints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_user_id TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '',
        industry_pack_id TEXT NOT NULL DEFAULT '',
        constraint_key TEXT NOT NULL,
        constraint_value_json TEXT NOT NULL DEFAULT '{}',
        source TEXT NOT NULL DEFAULT 'plan',
        confirmed INTEGER NOT NULL DEFAULT 1,
        run_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(owner_user_id, session_id, industry_pack_id, constraint_key)
    )
    """,
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
        corpus_version TEXT DEFAULT '',
        model_version TEXT DEFAULT '',
        prompt_version TEXT DEFAULT '',
        config_hash TEXT DEFAULT '',
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
        node_id TEXT DEFAULT '',
        node_kind TEXT DEFAULT '',
        parent_node_id TEXT DEFAULT '',
        round_index INTEGER DEFAULT 0,
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
    """
    CREATE TABLE IF NOT EXISTS qa_attribution_units (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        unit_id TEXT NOT NULL,
        order_index INTEGER NOT NULL DEFAULT 0,
        text TEXT NOT NULL,
        claim_type TEXT NOT NULL DEFAULT '',
        needs_evidence INTEGER NOT NULL DEFAULT 1,
        method TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        UNIQUE(run_id, unit_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS qa_attribution_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        unit_id TEXT NOT NULL,
        evidence_ref TEXT NOT NULL,
        article_id INTEGER,
        sentence_index INTEGER NOT NULL DEFAULT 0,
        sentence_text TEXT NOT NULL DEFAULT '',
        relation TEXT NOT NULL DEFAULT 'mention',
        confidence REAL NOT NULL DEFAULT 0,
        recall_score REAL NOT NULL DEFAULT 0,
        method TEXT NOT NULL DEFAULT '',
        influence_score REAL,
        influence_method TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(run_id, unit_id, evidence_ref, sentence_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS qa_token_influence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        unit_id TEXT NOT NULL,
        evidence_ref TEXT NOT NULL,
        sentence_index INTEGER NOT NULL DEFAULT 0,
        token TEXT NOT NULL,
        influence REAL NOT NULL DEFAULT 0,
        method TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE(run_id, unit_id, evidence_ref, sentence_index, token)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS qa_attribution_runs (
        run_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        method TEXT NOT NULL DEFAULT '',
        units INTEGER NOT NULL DEFAULT 0,
        links INTEGER NOT NULL DEFAULT 0,
        llm_calls INTEGER NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL DEFAULT '{}',
        computed_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS qa_evidence_seen (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_user_id TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '',
        industry_pack_id TEXT NOT NULL DEFAULT '',
        source_fingerprint TEXT NOT NULL,
        span_fingerprint TEXT NOT NULL DEFAULT '',
        evidence_ref TEXT NOT NULL DEFAULT '',
        source_type TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'seen',
        seen_count INTEGER NOT NULL DEFAULT 0,
        rejected_count INTEGER NOT NULL DEFAULT 0,
        first_run_id TEXT NOT NULL DEFAULT '',
        last_run_id TEXT NOT NULL DEFAULT '',
        round_index INTEGER NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL DEFAULT '{}',
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        UNIQUE(owner_user_id, session_id, industry_pack_id, source_fingerprint)
    )
    """,
    # ── Phase 09（P09-01）：Memory Graph Core 的八张表（§12 的子集，见文件头 v8 说明）──
    """
    CREATE TABLE IF NOT EXISTS memory_item (
        memory_id TEXT PRIMARY KEY,
        memory_type TEXT NOT NULL,
        canonical_content TEXT NOT NULL,
        content_fingerprint TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 0,
        freshness_class TEXT NOT NULL DEFAULT 'MEDIUM',
        valid_from TEXT DEFAULT '',
        valid_until TEXT DEFAULT '',
        last_verified_at TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'ACTIVE',
        scope TEXT NOT NULL DEFAULT 'SESSION',
        scope_key TEXT NOT NULL DEFAULT '',
        owner_user_id TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '',
        industry_pack_id TEXT NOT NULL DEFAULT '',
        entity_ids_json TEXT NOT NULL DEFAULT '[]',
        source_evidence_ids_json TEXT NOT NULL DEFAULT '[]',
        superseded_by TEXT DEFAULT '',
        reuse_count INTEGER NOT NULL DEFAULT 0,
        recall_count INTEGER NOT NULL DEFAULT 0,
        created_from_session_id TEXT NOT NULL DEFAULT '',
        created_from_run_id TEXT NOT NULL DEFAULT '',
        version INTEGER NOT NULL DEFAULT 1,
        decay_score REAL NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(scope_key, memory_type, content_fingerprint)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_version (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        memory_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        change TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT '',
        confidence REAL NOT NULL DEFAULT 0,
        canonical_content TEXT NOT NULL DEFAULT '',
        valid_until TEXT DEFAULT '',
        last_verified_at TEXT DEFAULT '',
        decay_score REAL NOT NULL DEFAULT 0,
        reason TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        UNIQUE(memory_id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_entity_link (
        memory_id TEXT NOT NULL,
        entity_key TEXT NOT NULL,
        entity_text TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL DEFAULT 'subject',
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        PRIMARY KEY(memory_id, entity_key, role)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_evidence_link (
        memory_id TEXT NOT NULL,
        evidence_ref TEXT NOT NULL,
        source_fingerprint TEXT NOT NULL DEFAULT '',
        span_fingerprint TEXT NOT NULL DEFAULT '',
        run_id TEXT NOT NULL DEFAULT '',
        stage TEXT NOT NULL DEFAULT '',
        route TEXT NOT NULL DEFAULT '',
        corpus_version TEXT NOT NULL DEFAULT '',
        verdict TEXT NOT NULL DEFAULT '',
        evidence_score REAL NOT NULL DEFAULT 0,
        relationship TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        PRIMARY KEY(memory_id, evidence_ref, source_fingerprint)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_relation (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        memory_id TEXT NOT NULL,
        relation TEXT NOT NULL,
        target_memory_id TEXT NOT NULL DEFAULT '',
        target_kind TEXT NOT NULL DEFAULT 'memory',
        target_ref TEXT NOT NULL DEFAULT '',
        weight REAL NOT NULL DEFAULT 0,
        rationale TEXT NOT NULL DEFAULT '',
        created_from_run_id TEXT NOT NULL DEFAULT '',
        version INTEGER NOT NULL DEFAULT 1,
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        UNIQUE(memory_id, relation, target_memory_id, target_ref)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_recall_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recall_id TEXT NOT NULL,
        trace_id TEXT NOT NULL DEFAULT '',
        run_id TEXT NOT NULL DEFAULT '',
        mode TEXT NOT NULL DEFAULT '',
        scope_key TEXT NOT NULL DEFAULT '',
        owner_user_id TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '',
        industry_pack_id TEXT NOT NULL DEFAULT '',
        query_fingerprint TEXT NOT NULL DEFAULT '',
        channels_json TEXT NOT NULL DEFAULT '[]',
        hits INTEGER NOT NULL DEFAULT 0,
        top_score REAL NOT NULL DEFAULT 0,
        counts_json TEXT NOT NULL DEFAULT '{}',
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        UNIQUE(recall_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_write_decision (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id TEXT NOT NULL,
        run_id TEXT NOT NULL DEFAULT '',
        memory_type TEXT NOT NULL DEFAULT '',
        decision TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        utility REAL NOT NULL DEFAULT 0,
        factors_json TEXT NOT NULL DEFAULT '{}',
        memory_id TEXT NOT NULL DEFAULT '',
        content_fingerprint TEXT NOT NULL DEFAULT '',
        evidence_refs_json TEXT NOT NULL DEFAULT '[]',
        scope TEXT NOT NULL DEFAULT '',
        scope_key TEXT NOT NULL DEFAULT '',
        gate_version TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        UNIQUE(decision_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_usage_stat (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        memory_id TEXT NOT NULL,
        day TEXT NOT NULL,
        recalled INTEGER NOT NULL DEFAULT 0,
        used INTEGER NOT NULL DEFAULT 0,
        helped INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(memory_id, day)
    )
    """,
)

QA_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_qa_runs_owner_created ON qa_runs(owner_user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_qa_runs_pack_status ON qa_runs(industry_pack_id, status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_qa_runs_job ON qa_runs(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_qa_runs_session ON qa_runs(session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_qa_reasoning_trace_run ON qa_reasoning_traces(run_id, hop_index)",
    "CREATE INDEX IF NOT EXISTS idx_qa_reasoning_trace_gap ON qa_reasoning_traces(run_id, gap_id)",
    "CREATE INDEX IF NOT EXISTS idx_qa_session_constraints_scope"
    " ON qa_session_constraints(owner_user_id, session_id)",
    "CREATE INDEX IF NOT EXISTS idx_qa_stage_run ON qa_stage_runs(run_id, stage, attempt)",
    "CREATE INDEX IF NOT EXISTS idx_qa_stage_runs_node ON qa_stage_runs(run_id, node_id)",
    "CREATE INDEX IF NOT EXISTS idx_qa_claim_run ON qa_claims(run_id, verification_status)",
    "CREATE INDEX IF NOT EXISTS idx_qa_evidence_run ON qa_evidence(run_id, source_type)",
    "CREATE INDEX IF NOT EXISTS idx_qa_events_resume ON qa_events(run_id, event_id)",
    "CREATE INDEX IF NOT EXISTS idx_qa_bridge_sessions_owner ON qa_bridge_sessions(owner_user_id, expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_qa_audit_trace ON qa_audit_events(trace_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_qa_audit_pack ON qa_audit_events(industry_pack_id, event_type, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_qa_health_component ON qa_health_snapshots(component, checked_at)",
    "CREATE INDEX IF NOT EXISTS idx_qa_cache_expiry ON qa_retrieval_cache(namespace, expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_qa_rate_bucket ON qa_rate_limit_events(bucket_key, event_at)",
    "CREATE INDEX IF NOT EXISTS idx_qa_attr_unit ON qa_attribution_units(run_id, order_index)",
    "CREATE INDEX IF NOT EXISTS idx_qa_attr_link ON qa_attribution_links(run_id, unit_id)",
    "CREATE INDEX IF NOT EXISTS idx_qa_token_influence ON qa_token_influence(run_id, unit_id)",
    # Phase 02（P02-03）：证据 seen 集合按 (用户, 会话, 行业包) 作用域查，别让它全表扫。
    "CREATE INDEX IF NOT EXISTS idx_qa_evidence_seen_scope"
    " ON qa_evidence_seen(owner_user_id, session_id, industry_pack_id, status)",
    # Phase 09（P09-01）：记忆图的四类热路径查询（作用域+状态、类型、证据反查、按天统计）。
    "CREATE INDEX IF NOT EXISTS idx_memory_item_scope ON memory_item(scope_key, status)",
    "CREATE INDEX IF NOT EXISTS idx_memory_item_type ON memory_item(memory_type, status)",
    "CREATE INDEX IF NOT EXISTS idx_memory_item_freshness"
    " ON memory_item(freshness_class, valid_until)",
    "CREATE INDEX IF NOT EXISTS idx_memory_evidence_ref ON memory_evidence_link(evidence_ref)",
    "CREATE INDEX IF NOT EXISTS idx_memory_evidence_memory ON memory_evidence_link(memory_id)",
    "CREATE INDEX IF NOT EXISTS idx_memory_entity_key ON memory_entity_link(entity_key)",
    "CREATE INDEX IF NOT EXISTS idx_memory_relation_memory ON memory_relation(memory_id, relation)",
    "CREATE INDEX IF NOT EXISTS idx_memory_recall_run ON memory_recall_log(run_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_memory_write_run ON memory_write_decision(run_id, decision)",
    "CREATE INDEX IF NOT EXISTS idx_memory_usage_day ON memory_usage_stat(memory_id, day)",
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
        "qa_attribution_units",
        "qa_attribution_links",
        "qa_token_influence",
        "qa_attribution_runs",
        "qa_reasoning_traces",
        "qa_session_constraints",
        "qa_evidence_seen",
        # Phase 09（v8）：跨会话记忆图八张表
        "memory_item",
        "memory_version",
        "memory_entity_link",
        "memory_evidence_link",
        "memory_relation",
        "memory_recall_log",
        "memory_write_decision",
        "memory_usage_stat",
    }
)


QA_ADDED_COLUMNS_V6 = (
    # 只允许 ADD COLUMN，且一律带 DEFAULT —— 老库（v5）升级不重写表、不改既有列语义，
    # 回滚时把这些列留着不读即可（写入侧全部有默认值兜底）。
    ("qa_runs", "corpus_version", "TEXT DEFAULT ''"),
    ("qa_runs", "model_version", "TEXT DEFAULT ''"),
    ("qa_runs", "prompt_version", "TEXT DEFAULT ''"),
    ("qa_runs", "config_hash", "TEXT DEFAULT ''"),
    ("qa_reasoning_traces", "gap_id", "TEXT DEFAULT ''"),
    ("qa_reasoning_traces", "route", "TEXT DEFAULT ''"),
    ("qa_reasoning_traces", "results", "INTEGER DEFAULT 0"),
    ("qa_reasoning_traces", "accepted", "INTEGER DEFAULT 0"),
    ("qa_reasoning_traces", "rejected", "INTEGER DEFAULT 0"),
    ("qa_reasoning_traces", "new_claims", "INTEGER DEFAULT 0"),
    ("qa_reasoning_traces", "resolved_gap", "INTEGER DEFAULT 0"),
    ("qa_stage_runs", "node_id", "TEXT DEFAULT ''"),
    ("qa_stage_runs", "node_kind", "TEXT DEFAULT ''"),
    ("qa_stage_runs", "parent_node_id", "TEXT DEFAULT ''"),
    # round 在 qa_stage_runs 里没有既有列（attempt 是重试次数，语义不同），
    # 按 qa_reasoning_traces.round_index 的口径补一列，不重复加同义列。
    ("qa_stage_runs", "round_index", "INTEGER DEFAULT 0"),
)
"""Phase 01（v5 → v6）新增列清单：(表名, 列名, 列定义)。

与上面 `QA_TABLE_DDL` 的建表文本**必须一致**：清库建表走 DDL，老库升级走这里；
`tests/test_qa_phase01_schema.py` 会同时校验两条路径产出的列完全相同。
"""

QA_ADDED_COLUMNS_V8 = ()
"""Phase 09（v7 → v8）新增列清单：**空**。

v8 只新增八张表（走 `CREATE TABLE IF NOT EXISTS`，老库执行 `ensure_qa_tables` 自动补建），
一行 ADD COLUMN 都不需要 —— 与 Phase 02 的 v7 同一手法，回滚只需 DROP 八张新表。
`tests/test_qa_phase09_schema.py` 断言本元组为空，防止后续有人偷偷往老表加列。"""


def _existing_columns(cursor, table_name: str) -> set:
    """列出表已有列名：SQLite 用 PRAGMA、PostgreSQL 用 information_schema，两边都试。

    与 `intel_schema._existing_columns` 同一套做法（这里不想跨模块 import 一个私有函数）：
    PG 上 `PRAGMA table_info(x)` 不报错但返回空集，若据此认为"列都不存在"就会对已存在的列
    ADD COLUMN → DuplicateColumn 打断整段建表；因此只在**确实拿到非空列集**时才决定加列。
    """
    names = set()
    try:
        cursor.execute(f"PRAGMA table_info({table_name})")
        for row in cursor.fetchall():
            try:
                names.add(str(row["name"]))
            except Exception:
                try:
                    names.add(str(row[1]))
                except Exception:
                    continue
    except Exception:
        pass
    if not names:
        try:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name=?",
                (table_name,),
            )
            for row in cursor.fetchall():
                try:
                    names.add(str(row["column_name"]))
                except Exception:
                    try:
                        names.add(str(row[0]))
                    except Exception:
                        continue
        except Exception:
            pass
    return names


def _ensure_qa_columns(cursor) -> None:
    """老库升级：把 `QA_ADDED_COLUMNS_V6` 里缺的列 ADD 上去（已存在则跳过）。"""
    seen: dict[str, set] = {}
    for table_name, column_name, definition in QA_ADDED_COLUMNS_V6:
        if table_name not in seen:
            # 一张表只查一次列清单（ensure_schema 被每个 store 方法调用，别把它拖慢）
            seen[table_name] = _existing_columns(cursor, table_name)
        columns = seen[table_name]
        if not columns:
            # 拿不到列清单（表还没建/后端异常）时不盲加，避免 DuplicateColumn 打断建表
            continue
        if column_name not in columns:
            cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def qa_schema_checksum() -> str:
    payload = "\n".join(sql.strip() for sql in QA_TABLE_DDL + QA_INDEX_DDL)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ensure_qa_tables(cursor) -> None:
    for statement in QA_TABLE_DDL:
        cursor.execute(statement)
    # 建表后、建索引前补齐老库缺列：新加的索引可能引用这些新列，顺序不能颠倒。
    _ensure_qa_columns(cursor)
    for statement in QA_INDEX_DDL:
        cursor.execute(statement)


__all__ = [
    "QA_ADDED_COLUMNS_V6",
    "QA_ADDED_COLUMNS_V8",
    "QA_INDEX_DDL",
    "QA_REQUIRED_TABLES",
    "QA_SCHEMA_VERSION",
    "QA_TABLE_DDL",
    "ensure_qa_tables",
    "qa_schema_checksum",
]

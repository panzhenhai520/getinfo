#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Additive, versioned SQLite schema for financial intelligence capabilities.

The schema deliberately lives in the existing application database.  Callers must
run :func:`ensure_financial_tables` from the explicit startup migration path, not
from request transactions.
"""

from __future__ import annotations

import hashlib


UTC_NOW_SQL = "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
FINANCIAL_SCHEMA_VERSION = 9


FINANCIAL_TABLE_DDL = (
    f"""
    CREATE TABLE IF NOT EXISTS industry_pack_dependencies (
        parent_pack_id TEXT NOT NULL,
        dependency_pack_id TEXT NOT NULL,
        minimum_version TEXT NOT NULL DEFAULT '',
        is_required INTEGER NOT NULL DEFAULT 1 CHECK (is_required IN (0, 1)),
        priority INTEGER NOT NULL DEFAULT 0,
        capability_config_json TEXT NOT NULL DEFAULT '{{}}',
        created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        PRIMARY KEY (parent_pack_id, dependency_pack_id),
        CHECK (parent_pack_id <> dependency_pack_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS content_industry_packs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content_type TEXT NOT NULL,
        content_id TEXT NOT NULL,
        industry_pack_id TEXT NOT NULL,
        association_type TEXT NOT NULL DEFAULT 'primary',
        origin_pack_id TEXT NOT NULL DEFAULT '',
        is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
        metadata_json TEXT NOT NULL DEFAULT '{{}}',
        created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        UNIQUE (content_type, content_id, industry_pack_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS financial_instruments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_symbol TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        asset_type TEXT NOT NULL,
        market TEXT NOT NULL,
        exchange TEXT NOT NULL DEFAULT '',
        currency TEXT NOT NULL DEFAULT '',
        country_code TEXT NOT NULL DEFAULT '',
        listing_status TEXT NOT NULL DEFAULT 'active',
        listed_at TEXT,
        delisted_at TEXT,
        provider_mappings_json TEXT NOT NULL DEFAULT '{{}}',
        metadata_json TEXT NOT NULL DEFAULT '{{}}',
        created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL}
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS financial_instrument_aliases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instrument_id INTEGER NOT NULL,
        alias TEXT NOT NULL,
        alias_normalized TEXT NOT NULL,
        market TEXT NOT NULL DEFAULT '',
        provider_key TEXT NOT NULL DEFAULT '',
        alias_type TEXT NOT NULL DEFAULT 'other',
        source_key TEXT NOT NULL DEFAULT '',
        source_url TEXT NOT NULL DEFAULT '',
        is_official INTEGER NOT NULL DEFAULT 0 CHECK (is_official IN (0, 1)),
        valid_from TEXT NOT NULL DEFAULT '',
        valid_to TEXT,
        is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
        created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        FOREIGN KEY (instrument_id) REFERENCES financial_instruments(id) ON DELETE CASCADE,
        UNIQUE (instrument_id, alias_normalized, market, provider_key, valid_from)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS financial_instrument_candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_key TEXT NOT NULL UNIQUE,
        query_normalized TEXT NOT NULL,
        canonical_symbol TEXT NOT NULL,
        display_name TEXT NOT NULL,
        asset_type TEXT NOT NULL,
        market TEXT NOT NULL,
        exchange TEXT NOT NULL,
        currency TEXT NOT NULL,
        country_code TEXT NOT NULL DEFAULT '',
        listing_status TEXT NOT NULL DEFAULT 'active',
        listed_at TEXT,
        provider_mappings_json TEXT NOT NULL DEFAULT '{{}}',
        aliases_json TEXT NOT NULL DEFAULT '[]',
        authoritative_sources_json TEXT NOT NULL DEFAULT '[]',
        corroborating_sources_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'discovered'
            CHECK (status IN ('discovered', 'verified', 'promoted', 'rejected', 'expired')),
        confidence REAL NOT NULL DEFAULT 0 CHECK (confidence >= 0 AND confidence <= 1),
        reason_codes_json TEXT NOT NULL DEFAULT '[]',
        payload_sha256 TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        expires_at TEXT,
        promoted_instrument_id INTEGER,
        first_request_id TEXT NOT NULL DEFAULT '',
        last_request_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        FOREIGN KEY (promoted_instrument_id) REFERENCES financial_instruments(id) ON DELETE SET NULL
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS financial_universes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        universe_key TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        universe_type TEXT NOT NULL,
        market TEXT NOT NULL DEFAULT '',
        definition_json TEXT NOT NULL DEFAULT '{{}}',
        source_provider_key TEXT NOT NULL DEFAULT '',
        constituent_as_of TEXT,
        is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
        created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL}
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS financial_universe_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        universe_id INTEGER NOT NULL,
        instrument_id INTEGER NOT NULL,
        weight REAL,
        effective_from TEXT NOT NULL DEFAULT '',
        effective_to TEXT,
        source_observed_at TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{{}}',
        created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        FOREIGN KEY (universe_id) REFERENCES financial_universes(id) ON DELETE CASCADE,
        FOREIGN KEY (instrument_id) REFERENCES financial_instruments(id) ON DELETE CASCADE,
        UNIQUE (universe_id, instrument_id, effective_from)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS financial_provider_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_key TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        provider_type TEXT NOT NULL,
        access_tier TEXT NOT NULL DEFAULT 'free',
        capabilities_json TEXT NOT NULL DEFAULT '[]',
        priority INTEGER NOT NULL DEFAULT 100,
        is_enabled INTEGER NOT NULL DEFAULT 0 CHECK (is_enabled IN (0, 1)),
        health_status TEXT NOT NULL DEFAULT 'unknown',
        terms_url TEXT NOT NULL DEFAULT '',
        attribution_text TEXT NOT NULL DEFAULT '',
        metadata_json TEXT NOT NULL DEFAULT '{{}}',
        last_health_check_at TEXT,
        created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL}
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS financial_data_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_key TEXT NOT NULL UNIQUE,
        instrument_id INTEGER,
        universe_id INTEGER,
        provider_profile_id INTEGER NOT NULL,
        data_type TEXT NOT NULL,
        interval_code TEXT NOT NULL DEFAULT '',
        observed_at TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        market_status TEXT NOT NULL DEFAULT 'unknown',
        currency TEXT NOT NULL DEFAULT '',
        timezone TEXT NOT NULL DEFAULT '',
        stale_after TEXT,
        quality_status TEXT NOT NULL DEFAULT 'unverified',
        payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        source_url TEXT NOT NULL DEFAULT '',
        request_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        FOREIGN KEY (instrument_id) REFERENCES financial_instruments(id) ON DELETE SET NULL,
        FOREIGN KEY (universe_id) REFERENCES financial_universes(id) ON DELETE SET NULL,
        FOREIGN KEY (provider_profile_id) REFERENCES financial_provider_profiles(id) ON DELETE RESTRICT,
        CHECK (instrument_id IS NOT NULL OR universe_id IS NOT NULL)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS financial_research_runs (
        id TEXT PRIMARY KEY,
        trigger_type TEXT NOT NULL,
        scope_type TEXT NOT NULL,
        instrument_id INTEGER,
        universe_id INTEGER,
        chat_session_id TEXT NOT NULL DEFAULT '',
        user_question TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'queued',
        current_stage TEXT NOT NULL DEFAULT '',
        time_context_json TEXT NOT NULL DEFAULT '{{}}',
        config_json TEXT NOT NULL DEFAULT '{{}}',
        llm_call_budget INTEGER NOT NULL DEFAULT 0,
        token_budget INTEGER NOT NULL DEFAULT 0,
        debate_round_budget INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        requested_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        started_at TEXT,
        completed_at TEXT,
        updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        FOREIGN KEY (instrument_id) REFERENCES financial_instruments(id) ON DELETE SET NULL,
        FOREIGN KEY (universe_id) REFERENCES financial_universes(id) ON DELETE SET NULL,
        CHECK (instrument_id IS NOT NULL OR universe_id IS NOT NULL OR scope_type = 'market')
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS financial_research_evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        research_run_id TEXT NOT NULL,
        evidence_key TEXT NOT NULL,
        evidence_kind TEXT NOT NULL
            CHECK (evidence_kind IN ('structured_snapshot', 'source_document')),
        snapshot_id INTEGER,
        article_id INTEGER,
        instrument_id INTEGER,
        universe_id INTEGER,
        evidence_role TEXT NOT NULL,
        match_method TEXT NOT NULL,
        match_score REAL NOT NULL DEFAULT 0,
        observed_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{{}}',
        created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        FOREIGN KEY (research_run_id) REFERENCES financial_research_runs(id) ON DELETE CASCADE,
        FOREIGN KEY (snapshot_id) REFERENCES financial_data_snapshots(id) ON DELETE CASCADE,
        FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE,
        FOREIGN KEY (instrument_id) REFERENCES financial_instruments(id) ON DELETE SET NULL,
        FOREIGN KEY (universe_id) REFERENCES financial_universes(id) ON DELETE SET NULL,
        UNIQUE (research_run_id, evidence_key),
        CHECK (
            (evidence_kind = 'structured_snapshot' AND snapshot_id IS NOT NULL AND article_id IS NULL)
            OR
            (evidence_kind = 'source_document' AND article_id IS NOT NULL AND snapshot_id IS NULL)
        ),
        CHECK (instrument_id IS NOT NULL OR universe_id IS NOT NULL),
        CHECK (match_score >= 0 AND match_score <= 1)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS financial_report_sections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        research_run_id TEXT NOT NULL,
        role_key TEXT NOT NULL,
        section_type TEXT NOT NULL,
        sequence_no INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'completed',
        content_markdown TEXT NOT NULL DEFAULT '',
        content_json TEXT NOT NULL DEFAULT '{{}}',
        citations_json TEXT NOT NULL DEFAULT '[]',
        model_id TEXT NOT NULL DEFAULT '',
        prompt_version TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        FOREIGN KEY (research_run_id) REFERENCES financial_research_runs(id) ON DELETE CASCADE,
        UNIQUE (research_run_id, role_key, section_type, sequence_no)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS financial_final_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        research_run_id TEXT NOT NULL,
        report_version INTEGER NOT NULL DEFAULT 1,
        report_status TEXT NOT NULL DEFAULT 'draft',
        recommendation TEXT NOT NULL DEFAULT 'insufficient_evidence',
        confidence REAL,
        title TEXT NOT NULL DEFAULT '',
        executive_summary TEXT NOT NULL DEFAULT '',
        report_markdown TEXT NOT NULL DEFAULT '',
        report_json TEXT NOT NULL DEFAULT '{{}}',
        risk_summary_json TEXT NOT NULL DEFAULT '{{}}',
        suitability_notice TEXT NOT NULL DEFAULT '',
        disclaimer TEXT NOT NULL DEFAULT '',
        observed_at TEXT,
        fetched_at TEXT,
        verified_at TEXT,
        created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        FOREIGN KEY (research_run_id) REFERENCES financial_research_runs(id) ON DELETE CASCADE,
        UNIQUE (research_run_id, report_version),
        CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS financial_artifacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        research_run_id TEXT NOT NULL,
        final_report_id INTEGER,
        artifact_kind TEXT NOT NULL,
        artifact_version INTEGER NOT NULL DEFAULT 1,
        content_format TEXT NOT NULL,
        storage_uri TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
        status TEXT NOT NULL DEFAULT 'ready'
            CHECK (status IN ('ready', 'missing', 'corrupt')),
        ragflow_kb_id TEXT NOT NULL DEFAULT '',
        ragflow_document_id TEXT NOT NULL DEFAULT '',
        memory_status TEXT NOT NULL DEFAULT 'not_requested',
        metadata_json TEXT NOT NULL DEFAULT '{{}}',
        created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        FOREIGN KEY (research_run_id) REFERENCES financial_research_runs(id) ON DELETE CASCADE,
        FOREIGN KEY (final_report_id) REFERENCES financial_final_reports(id) ON DELETE CASCADE,
        UNIQUE (research_run_id, artifact_kind, artifact_version, content_format)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS financial_claims (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        research_run_id TEXT NOT NULL,
        final_report_id INTEGER,
        claim_key TEXT NOT NULL,
        claim_type TEXT NOT NULL,
        subject TEXT NOT NULL DEFAULT '',
        statement TEXT NOT NULL,
        normalized_value_json TEXT NOT NULL DEFAULT '{{}}',
        unit TEXT NOT NULL DEFAULT '',
        currency TEXT NOT NULL DEFAULT '',
        effective_at TEXT,
        observed_at TEXT,
        verification_status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        FOREIGN KEY (research_run_id) REFERENCES financial_research_runs(id) ON DELETE CASCADE,
        FOREIGN KEY (final_report_id) REFERENCES financial_final_reports(id) ON DELETE CASCADE,
        UNIQUE (research_run_id, claim_key)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS financial_claim_evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        claim_id INTEGER NOT NULL,
        snapshot_id INTEGER,
        article_id INTEGER,
        provider_profile_id INTEGER,
        evidence_type TEXT NOT NULL,
        relationship TEXT NOT NULL DEFAULT 'supports',
        source_url TEXT NOT NULL DEFAULT '',
        source_title TEXT NOT NULL DEFAULT '',
        evidence_json TEXT NOT NULL DEFAULT '{{}}',
        authority_score REAL,
        freshness_score REAL,
        observed_at TEXT,
        fetched_at TEXT,
        created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        FOREIGN KEY (claim_id) REFERENCES financial_claims(id) ON DELETE CASCADE,
        FOREIGN KEY (snapshot_id) REFERENCES financial_data_snapshots(id) ON DELETE SET NULL,
        FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE SET NULL,
        FOREIGN KEY (provider_profile_id) REFERENCES financial_provider_profiles(id) ON DELETE SET NULL,
        CHECK (snapshot_id IS NOT NULL OR article_id IS NOT NULL OR source_url <> '')
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS financial_verdicts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        claim_id INTEGER NOT NULL,
        adjudication_version INTEGER NOT NULL DEFAULT 1,
        verdict TEXT NOT NULL,
        rationale TEXT NOT NULL DEFAULT '',
        selected_evidence_ids_json TEXT NOT NULL DEFAULT '[]',
        conflicting_evidence_ids_json TEXT NOT NULL DEFAULT '[]',
        adjudicator TEXT NOT NULL DEFAULT '',
        model_id TEXT NOT NULL DEFAULT '',
        decided_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        FOREIGN KEY (claim_id) REFERENCES financial_claims(id) ON DELETE CASCADE,
        UNIQUE (claim_id, adjudication_version)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS chat_financial_routes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        route_key TEXT NOT NULL UNIQUE,
        session_id TEXT NOT NULL,
        message_id TEXT NOT NULL DEFAULT '',
        question_sha256 TEXT NOT NULL,
        raw_question TEXT NOT NULL,
        intent TEXT NOT NULL DEFAULT 'unknown',
        financial_attributes_json TEXT NOT NULL DEFAULT '{{}}',
        resolved_targets_json TEXT NOT NULL DEFAULT '[]',
        clarification_json TEXT NOT NULL DEFAULT '{{}}',
        route_status TEXT NOT NULL DEFAULT 'classified',
        route_destination TEXT NOT NULL DEFAULT 'normal_chat',
        server_now TEXT NOT NULL,
        server_timezone TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL}
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS chat_financial_artifacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_history_id INTEGER,
        chat_route_id INTEGER NOT NULL,
        artifact_type TEXT NOT NULL,
        artifact_ref TEXT NOT NULL,
        research_run_id TEXT,
        final_report_id INTEGER,
        snapshot_id INTEGER,
        payload_json TEXT NOT NULL DEFAULT '{{}}',
        created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        FOREIGN KEY (chat_history_id) REFERENCES chat_history(id) ON DELETE CASCADE,
        FOREIGN KEY (chat_route_id) REFERENCES chat_financial_routes(id) ON DELETE CASCADE,
        FOREIGN KEY (research_run_id) REFERENCES financial_research_runs(id) ON DELETE SET NULL,
        FOREIGN KEY (final_report_id) REFERENCES financial_final_reports(id) ON DELETE SET NULL,
        FOREIGN KEY (snapshot_id) REFERENCES financial_data_snapshots(id) ON DELETE SET NULL,
        UNIQUE (chat_route_id, artifact_type, artifact_ref)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS paper_accounts (
        id TEXT PRIMARY KEY,
        account_name TEXT NOT NULL,
        base_currency TEXT NOT NULL,
        initial_cash REAL NOT NULL,
        cash_balance REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        execution_mode TEXT NOT NULL DEFAULT 'paper' CHECK (execution_mode = 'paper'),
        config_json TEXT NOT NULL DEFAULT '{{}}',
        created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL}
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS paper_orders (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        instrument_id INTEGER NOT NULL,
        research_run_id TEXT,
        final_report_id INTEGER,
        side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
        order_type TEXT NOT NULL,
        quantity REAL NOT NULL CHECK (quantity > 0),
        limit_price REAL,
        stop_price REAL,
        status TEXT NOT NULL DEFAULT 'pending',
        submitted_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        completed_at TEXT,
        cancelled_at TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{{}}',
        FOREIGN KEY (account_id) REFERENCES paper_accounts(id) ON DELETE CASCADE,
        FOREIGN KEY (instrument_id) REFERENCES financial_instruments(id) ON DELETE RESTRICT,
        FOREIGN KEY (research_run_id) REFERENCES financial_research_runs(id) ON DELETE SET NULL,
        FOREIGN KEY (final_report_id) REFERENCES financial_final_reports(id) ON DELETE SET NULL
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS paper_fills (
        id TEXT PRIMARY KEY,
        order_id TEXT NOT NULL,
        quantity REAL NOT NULL CHECK (quantity > 0),
        price REAL NOT NULL CHECK (price >= 0),
        fee REAL NOT NULL DEFAULT 0 CHECK (fee >= 0),
        currency TEXT NOT NULL DEFAULT '',
        snapshot_id INTEGER,
        filled_at TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        FOREIGN KEY (order_id) REFERENCES paper_orders(id) ON DELETE CASCADE,
        FOREIGN KEY (snapshot_id) REFERENCES financial_data_snapshots(id) ON DELETE SET NULL
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS paper_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT NOT NULL,
        instrument_id INTEGER NOT NULL,
        quantity REAL NOT NULL DEFAULT 0,
        average_cost REAL NOT NULL DEFAULT 0,
        realized_pnl REAL NOT NULL DEFAULT 0,
        last_price REAL,
        market_value REAL,
        as_of TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        FOREIGN KEY (account_id) REFERENCES paper_accounts(id) ON DELETE CASCADE,
        FOREIGN KEY (instrument_id) REFERENCES financial_instruments(id) ON DELETE RESTRICT,
        UNIQUE (account_id, instrument_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS backtest_runs (
        id TEXT PRIMARY KEY,
        strategy_key TEXT NOT NULL,
        model_version TEXT NOT NULL DEFAULT '',
        scope_type TEXT NOT NULL,
        instrument_id INTEGER,
        universe_id INTEGER,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        initial_capital REAL NOT NULL,
        config_json TEXT NOT NULL DEFAULT '{{}}',
        status TEXT NOT NULL DEFAULT 'queued',
        data_cutoff_at TEXT,
        last_error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        started_at TEXT,
        completed_at TEXT,
        FOREIGN KEY (instrument_id) REFERENCES financial_instruments(id) ON DELETE SET NULL,
        FOREIGN KEY (universe_id) REFERENCES financial_universes(id) ON DELETE SET NULL,
        CHECK (instrument_id IS NOT NULL OR universe_id IS NOT NULL)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS backtest_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        backtest_run_id TEXT NOT NULL,
        metric_key TEXT NOT NULL,
        metric_value REAL,
        metric_text TEXT NOT NULL DEFAULT '',
        unit TEXT NOT NULL DEFAULT '',
        metadata_json TEXT NOT NULL DEFAULT '{{}}',
        created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        FOREIGN KEY (backtest_run_id) REFERENCES backtest_runs(id) ON DELETE CASCADE,
        UNIQUE (backtest_run_id, metric_key)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS backtest_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        backtest_run_id TEXT NOT NULL,
        instrument_id INTEGER NOT NULL,
        side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
        quantity REAL NOT NULL CHECK (quantity > 0),
        price REAL NOT NULL CHECK (price >= 0),
        fee REAL NOT NULL DEFAULT 0 CHECK (fee >= 0),
        signal_at TEXT,
        executed_at TEXT NOT NULL,
        reason_json TEXT NOT NULL DEFAULT '{{}}',
        created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        FOREIGN KEY (backtest_run_id) REFERENCES backtest_runs(id) ON DELETE CASCADE,
        FOREIGN KEY (instrument_id) REFERENCES financial_instruments(id) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS financial_research_checkpoints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        research_run_id TEXT NOT NULL,
        stage_key TEXT NOT NULL,
        checkpoint_version INTEGER NOT NULL DEFAULT 1,
        schema_version TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'valid',
        payload_json TEXT NOT NULL DEFAULT '{{}}',
        storage_uri TEXT NOT NULL DEFAULT '',
        content_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        FOREIGN KEY (research_run_id) REFERENCES financial_research_runs(id) ON DELETE CASCADE,
        UNIQUE (research_run_id, stage_key, checkpoint_version)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS llm_call_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        call_id TEXT NOT NULL UNIQUE,
        research_run_id TEXT,
        job_id INTEGER,
        role_key TEXT NOT NULL DEFAULT '',
        profile_key TEXT NOT NULL,
        model_id TEXT NOT NULL,
        prompt_sha256 TEXT NOT NULL,
        response_sha256 TEXT NOT NULL DEFAULT '',
        input_tokens INTEGER,
        output_tokens INTEGER,
        latency_ms INTEGER,
        status TEXT NOT NULL,
        error_code TEXT NOT NULL DEFAULT '',
        request_id TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL,
        completed_at TEXT,
        created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        FOREIGN KEY (research_run_id) REFERENCES financial_research_runs(id) ON DELETE SET NULL,
        FOREIGN KEY (job_id) REFERENCES intel_jobs(id) ON DELETE SET NULL
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS financial_official_news_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instrument_id INTEGER NOT NULL,
        source_id INTEGER,
        source_key TEXT NOT NULL,
        source_url TEXT NOT NULL,
        document_url TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        stock_code TEXT NOT NULL DEFAULT '',
        stock_name TEXT NOT NULL DEFAULT '',
        published_at_utc TEXT NOT NULL,
        published_timezone TEXT NOT NULL DEFAULT 'Asia/Hong_Kong',
        payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        quality_status TEXT NOT NULL DEFAULT 'verified_official_metadata'
            CHECK (quality_status IN ('verified_official_metadata')),
        fetched_at TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        FOREIGN KEY (instrument_id) REFERENCES financial_instruments(id) ON DELETE CASCADE,
        UNIQUE (instrument_id, document_url)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS financial_dashboard_hidden_instruments (
        owner_user_id TEXT NOT NULL,
        instrument_id INTEGER NOT NULL,
        canonical_symbol TEXT NOT NULL,
        hidden_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
        PRIMARY KEY (owner_user_id, instrument_id),
        FOREIGN KEY (instrument_id) REFERENCES financial_instruments(id) ON DELETE CASCADE
    )
    """,
)


FINANCIAL_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_pack_dependencies_child ON industry_pack_dependencies(dependency_pack_id)",
    "CREATE INDEX IF NOT EXISTS idx_content_packs_lookup ON content_industry_packs(industry_pack_id, content_type, is_active)",
    "CREATE INDEX IF NOT EXISTS idx_financial_instruments_market_type ON financial_instruments(market, asset_type, listing_status)",
    "CREATE INDEX IF NOT EXISTS idx_financial_alias_lookup ON financial_instrument_aliases(alias_normalized, market)",
    "CREATE INDEX IF NOT EXISTS idx_financial_candidates_query ON financial_instrument_candidates(query_normalized, status, observed_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_financial_candidates_symbol ON financial_instrument_candidates(canonical_symbol, status)",
    "CREATE INDEX IF NOT EXISTS idx_financial_universe_members_active ON financial_universe_members(universe_id, effective_to)",
    "CREATE INDEX IF NOT EXISTS idx_financial_provider_health ON financial_provider_profiles(is_enabled, health_status, priority)",
    "CREATE INDEX IF NOT EXISTS idx_financial_snapshots_instrument ON financial_data_snapshots(instrument_id, data_type, observed_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_financial_snapshots_universe ON financial_data_snapshots(universe_id, data_type, observed_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_financial_snapshots_freshness ON financial_data_snapshots(data_type, fetched_at DESC, stale_after)",
    "CREATE INDEX IF NOT EXISTS idx_financial_runs_status ON financial_research_runs(status, requested_at)",
    "CREATE INDEX IF NOT EXISTS idx_financial_runs_instrument ON financial_research_runs(instrument_id, requested_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_financial_runs_universe ON financial_research_runs(universe_id, requested_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_financial_run_evidence_kind ON financial_research_evidence(research_run_id, evidence_kind, observed_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_financial_run_evidence_instrument ON financial_research_evidence(instrument_id, observed_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_financial_sections_run_sequence ON financial_report_sections(research_run_id, sequence_no)",
    "CREATE INDEX IF NOT EXISTS idx_financial_reports_status ON financial_final_reports(report_status, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_financial_artifacts_run ON financial_artifacts(research_run_id, artifact_kind, artifact_version)",
    "CREATE INDEX IF NOT EXISTS idx_financial_artifacts_report ON financial_artifacts(final_report_id)",
    "CREATE INDEX IF NOT EXISTS idx_financial_artifacts_memory ON financial_artifacts(memory_status, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_financial_claims_verification ON financial_claims(verification_status, research_run_id)",
    "CREATE INDEX IF NOT EXISTS idx_financial_evidence_claim ON financial_claim_evidence(claim_id, relationship)",
    "CREATE INDEX IF NOT EXISTS idx_financial_verdicts_verdict ON financial_verdicts(verdict, decided_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_chat_financial_routes_session ON chat_financial_routes(session_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_chat_financial_routes_destination ON chat_financial_routes(route_destination, route_status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_chat_financial_artifacts_run ON chat_financial_artifacts(research_run_id)",
    "CREATE INDEX IF NOT EXISTS idx_chat_financial_artifacts_history ON chat_financial_artifacts(chat_history_id)",
    "CREATE INDEX IF NOT EXISTS idx_paper_orders_account_status ON paper_orders(account_id, status, submitted_at)",
    "CREATE INDEX IF NOT EXISTS idx_paper_fills_order_time ON paper_fills(order_id, filled_at)",
    "CREATE INDEX IF NOT EXISTS idx_paper_positions_account ON paper_positions(account_id, as_of)",
    "CREATE INDEX IF NOT EXISTS idx_backtest_runs_status ON backtest_runs(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_backtest_trades_run_time ON backtest_trades(backtest_run_id, executed_at)",
    "CREATE INDEX IF NOT EXISTS idx_financial_checkpoints_latest ON financial_research_checkpoints(research_run_id, stage_key, checkpoint_version DESC)",
    "CREATE INDEX IF NOT EXISTS idx_llm_call_audit_run ON llm_call_audit(research_run_id, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_llm_call_audit_status ON llm_call_audit(status, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_financial_official_news_latest ON financial_official_news_items(instrument_id, published_at_utc DESC)",
    "CREATE INDEX IF NOT EXISTS idx_financial_hidden_instrument_lookup ON financial_dashboard_hidden_instruments(instrument_id, owner_user_id)",
)


FINANCIAL_REQUIRED_TABLES = frozenset(
    {
        "financial_schema_migrations",
        "industry_pack_dependencies",
        "content_industry_packs",
        "financial_instruments",
        "financial_instrument_aliases",
        "financial_instrument_candidates",
        "financial_universes",
        "financial_universe_members",
        "financial_provider_profiles",
        "financial_data_snapshots",
        "financial_research_runs",
        "financial_research_evidence",
        "financial_report_sections",
        "financial_final_reports",
        "financial_artifacts",
        "financial_claims",
        "financial_claim_evidence",
        "financial_verdicts",
        "chat_financial_routes",
        "chat_financial_artifacts",
        "paper_accounts",
        "paper_orders",
        "paper_fills",
        "paper_positions",
        "backtest_runs",
        "backtest_metrics",
        "backtest_trades",
        "financial_research_checkpoints",
        "llm_call_audit",
        "financial_official_news_items",
        "financial_dashboard_hidden_instruments",
    }
)


def financial_schema_checksum() -> str:
    payload = "\n".join(sql.strip() for sql in FINANCIAL_TABLE_DDL + FINANCIAL_INDEX_DDL)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _migrate_instrument_alias_identity(cursor) -> None:
    """Allow one alias to truthfully resolve to multiple instruments.

    Schema v1 treated an alias/market/provider/date tuple as globally unique.
    That cannot represent legitimate ambiguity such as an ETF and index sharing
    a familiar name.  The v2 identity includes ``instrument_id`` and keeps every
    existing row and primary key while rebuilding only this financial table.
    """

    cursor.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='table' AND name='financial_instrument_aliases'"
    )
    row = cursor.fetchone()
    if row is None:
        return
    normalized_sql = " ".join(str(row[0] or "").lower().split())
    current_identity = (
        "unique (instrument_id, alias_normalized, market, provider_key, valid_from)"
    )
    if current_identity in normalized_sql:
        return
    old_identity = "unique (alias_normalized, market, provider_key, valid_from)"
    if old_identity not in normalized_sql:
        raise RuntimeError("unrecognized financial_instrument_aliases identity")

    cursor.execute(
        "ALTER TABLE financial_instrument_aliases "
        "RENAME TO financial_instrument_aliases_v1"
    )
    cursor.execute(
        f"""
        CREATE TABLE financial_instrument_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instrument_id INTEGER NOT NULL,
            alias TEXT NOT NULL,
            alias_normalized TEXT NOT NULL,
            market TEXT NOT NULL DEFAULT '',
            provider_key TEXT NOT NULL DEFAULT '',
            valid_from TEXT NOT NULL DEFAULT '',
            valid_to TEXT,
            is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            FOREIGN KEY (instrument_id) REFERENCES financial_instruments(id) ON DELETE CASCADE,
            UNIQUE (instrument_id, alias_normalized, market, provider_key, valid_from)
        )
        """
    )
    cursor.execute(
        """
        INSERT INTO financial_instrument_aliases(
            id, instrument_id, alias, alias_normalized, market, provider_key,
            valid_from, valid_to, is_primary, created_at
        )
        SELECT id, instrument_id, alias, alias_normalized, market, provider_key,
               valid_from, valid_to, is_primary, created_at
        FROM financial_instrument_aliases_v1
        """
    )
    cursor.execute("DROP TABLE financial_instrument_aliases_v1")


def _migrate_chat_financial_history_link(cursor) -> None:
    """Add the answer-level FK without dropping existing route artifacts."""

    cursor.execute("PRAGMA table_info(chat_financial_artifacts)")
    columns = {str(row[1]) for row in cursor.fetchall()}
    if not columns or "chat_history_id" in columns:
        return
    cursor.execute(
        "ALTER TABLE chat_financial_artifacts RENAME TO chat_financial_artifacts_v4"
    )
    cursor.execute(
        f"""
        CREATE TABLE chat_financial_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_history_id INTEGER,
            chat_route_id INTEGER NOT NULL,
            artifact_type TEXT NOT NULL,
            artifact_ref TEXT NOT NULL,
            research_run_id TEXT,
            final_report_id INTEGER,
            snapshot_id INTEGER,
            payload_json TEXT NOT NULL DEFAULT '{{}}',
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            FOREIGN KEY (chat_history_id) REFERENCES chat_history(id) ON DELETE CASCADE,
            FOREIGN KEY (chat_route_id) REFERENCES chat_financial_routes(id) ON DELETE CASCADE,
            FOREIGN KEY (research_run_id) REFERENCES financial_research_runs(id) ON DELETE SET NULL,
            FOREIGN KEY (final_report_id) REFERENCES financial_final_reports(id) ON DELETE SET NULL,
            FOREIGN KEY (snapshot_id) REFERENCES financial_data_snapshots(id) ON DELETE SET NULL,
            UNIQUE (chat_route_id, artifact_type, artifact_ref)
        )
        """
    )
    cursor.execute(
        """
        INSERT INTO chat_financial_artifacts(
            id, chat_route_id, artifact_type, artifact_ref, research_run_id,
            final_report_id, snapshot_id, payload_json, created_at
        )
        SELECT id, chat_route_id, artifact_type, artifact_ref, research_run_id,
               final_report_id, snapshot_id, payload_json, created_at
        FROM chat_financial_artifacts_v4
        """
    )
    cursor.execute("DROP TABLE chat_financial_artifacts_v4")


def _migrate_instrument_alias_provenance(cursor) -> None:
    """Add source/type audit fields without rebuilding valid temporal aliases."""

    cursor.execute("PRAGMA table_info(financial_instrument_aliases)")
    columns = {str(row[1]) for row in cursor.fetchall()}
    additions = (
        ("alias_type", "TEXT NOT NULL DEFAULT 'other'"),
        ("source_key", "TEXT NOT NULL DEFAULT ''"),
        ("source_url", "TEXT NOT NULL DEFAULT ''"),
        ("is_official", "INTEGER NOT NULL DEFAULT 0 CHECK (is_official IN (0, 1))"),
    )
    for name, declaration in additions:
        if name not in columns:
            cursor.execute(
                f"ALTER TABLE financial_instrument_aliases ADD COLUMN {name} {declaration}"
            )


def get_financial_schema_version(cursor) -> int:
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='financial_schema_migrations'"
    )
    if cursor.fetchone() is None:
        return 0
    cursor.execute(
        "SELECT COALESCE(MAX(version), 0) FROM financial_schema_migrations WHERE status='applied'"
    )
    row = cursor.fetchone()
    return int(row[0] if row else 0)


def ensure_financial_tables(cursor) -> None:
    """Apply all financial migrations atomically and safely on repeated runs."""

    cursor.execute("SAVEPOINT financial_schema_migration")
    try:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS financial_schema_migrations (
                version INTEGER PRIMARY KEY,
                migration_name TEXT NOT NULL,
                schema_checksum TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('applied')),
                applied_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL}
            )
            """
        )
        for table_sql in FINANCIAL_TABLE_DDL:
            cursor.execute(table_sql)
        _migrate_instrument_alias_identity(cursor)
        _migrate_chat_financial_history_link(cursor)
        _migrate_instrument_alias_provenance(cursor)
        for index_sql in FINANCIAL_INDEX_DDL:
            cursor.execute(index_sql)
        cursor.execute(
            """
            INSERT INTO financial_schema_migrations(
                version, migration_name, schema_checksum, status
            ) VALUES (?, ?, ?, 'applied')
            ON CONFLICT(version) DO UPDATE SET
                migration_name=excluded.migration_name,
                schema_checksum=excluded.schema_checksum,
                status='applied'
            """,
            (
                FINANCIAL_SCHEMA_VERSION,
                "dashboard_hidden_instruments_v9",
                financial_schema_checksum(),
            ),
        )
        cursor.execute("RELEASE SAVEPOINT financial_schema_migration")
    except Exception:
        cursor.execute("ROLLBACK TO SAVEPOINT financial_schema_migration")
        cursor.execute("RELEASE SAVEPOINT financial_schema_migration")
        raise

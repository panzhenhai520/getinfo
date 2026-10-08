#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Idempotent SQLite schema for the market intelligence radar."""

from __future__ import annotations


UTC_NOW_SQL = "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"


def _existing_columns(cursor, table_name: str) -> set:
    """列出表已有列名：SQLite 用 PRAGMA、PostgreSQL 用 information_schema，两边都试。

    为什么两边都试：在 PostgreSQL 上 `PRAGMA table_info(x)` 不报错但**返回空集**，
    老实现因此认为"所有列都不存在"，于是对已存在的列执行 ALTER TABLE ADD COLUMN
    → `DuplicateColumn`（实测：intel_source_industries.is_active 已存在却仍被 ADD）。
    这里改成取两个来源的并集，并且只在**确实拿到非空列集**时才决定要不要加列。
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


def _ensure_column(cursor, table_name: str, column_name: str, definition: str) -> None:
    columns = _existing_columns(cursor, table_name)
    if not columns:
        # 拿不到列清单（表还没建/后端异常）时不要盲加列，避免 DuplicateColumn 打断整段建表流程
        return
    if column_name not in columns:
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def ensure_intel_embedding_tables(cursor) -> None:
    """文章向量表（Xinference bge-m3，dim=1024，BLOB 存 float32）。

    第一阶段产出向量并落库，第三阶段语义漂移/聚类消费。
    """
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS intel_article_embeddings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER NOT NULL,
            model_id TEXT NOT NULL DEFAULT 'bge-m3',
            embedding_dim INTEGER NOT NULL DEFAULT 1024 CHECK (embedding_dim > 0),
            embedding BLOB NOT NULL,
            content_hash TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'ready'
                CHECK (status IN ('ready', 'stale', 'error')),
            error_message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE,
            UNIQUE(article_id, model_id)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_embeddings_article "
        "ON intel_article_embeddings(article_id, model_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_embeddings_status "
        "ON intel_article_embeddings(status, updated_at)"
    )


def ensure_intel_trend_tables(cursor) -> None:
    """关键词/主题 × 按天桶 的趋势聚合表（爆发检测 + 五态状态机产出）。

    每行 = (industry_pack_id, dimension, keyword, day)；state/burst 由
    trend_detect 聚合后回填最近 window 天，历史行保留旧 state 以便回溯。
    """
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS intel_topic_trends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            industry_pack_id TEXT NOT NULL,
            activation_id TEXT NOT NULL DEFAULT '',
            dimension TEXT NOT NULL DEFAULT 'trend_keyword',
            keyword TEXT NOT NULL,
            bucket_date TEXT NOT NULL,
            article_count INTEGER NOT NULL DEFAULT 0,
            distinct_source_count INTEGER NOT NULL DEFAULT 0,
            is_burst INTEGER NOT NULL DEFAULT 0 CHECK (is_burst IN (0, 1)),
            burst_score REAL NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'EMERGING'
                CHECK (state IN ('EMERGING', 'RISING', 'BURSTING', 'MATURE', 'DECLINING')),
            computed_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            UNIQUE(industry_pack_id, dimension, keyword, bucket_date)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_trends_pack_date "
        "ON intel_topic_trends(industry_pack_id, dimension, bucket_date)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_trends_keyword "
        "ON intel_topic_trends(industry_pack_id, dimension, keyword, bucket_date)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_trends_state "
        "ON intel_topic_trends(industry_pack_id, state, burst_score DESC)"
    )
    # 去 dimension CHECK：放开 bertopic 等新维度（SQLite 不能改 CHECK，需重建表）
    _trends_ddl = cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='intel_topic_trends'"
    ).fetchone()
    if _trends_ddl and "CHECK (dimension IN" in str(_trends_ddl[0] or ""):
        for _idx in ("idx_intel_trends_pack_date", "idx_intel_trends_keyword", "idx_intel_trends_state"):
            cursor.execute(f"DROP INDEX IF EXISTS {_idx}")
        cursor.execute("ALTER TABLE intel_topic_trends RENAME TO _intel_topic_trends_old")
        cursor.execute(
            """
            CREATE TABLE intel_topic_trends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                industry_pack_id TEXT NOT NULL,
                activation_id TEXT NOT NULL DEFAULT '',
                dimension TEXT NOT NULL DEFAULT 'trend_keyword',
                keyword TEXT NOT NULL,
                bucket_date TEXT NOT NULL,
                article_count INTEGER NOT NULL DEFAULT 0,
                distinct_source_count INTEGER NOT NULL DEFAULT 0,
                is_burst INTEGER NOT NULL DEFAULT 0 CHECK (is_burst IN (0, 1)),
                burst_score REAL NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'EMERGING'
                    CHECK (state IN ('EMERGING', 'RISING', 'BURSTING', 'MATURE', 'DECLINING')),
                computed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                UNIQUE(industry_pack_id, dimension, keyword, bucket_date)
            )
            """
        )
        cursor.execute(
            "INSERT INTO intel_topic_trends "
            "(id, industry_pack_id, activation_id, dimension, keyword, bucket_date, "
            "article_count, distinct_source_count, is_burst, burst_score, state, "
            "computed_at, created_at, updated_at) "
            "SELECT id, industry_pack_id, activation_id, dimension, keyword, bucket_date, "
            "article_count, distinct_source_count, is_burst, burst_score, state, "
            "computed_at, created_at, updated_at FROM _intel_topic_trends_old"
        )
        cursor.execute("DROP TABLE _intel_topic_trends_old")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_intel_trends_pack_date ON intel_topic_trends(industry_pack_id, dimension, bucket_date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_intel_trends_keyword ON intel_topic_trends(industry_pack_id, dimension, keyword, bucket_date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_intel_trends_state ON intel_topic_trends(industry_pack_id, state, burst_score DESC)")
    # TaskShift statistical evidence is additive so existing databases migrate
    # in place without rebuilding the user's historical trend rows.
    _ensure_column(cursor, "intel_topic_trends", "trend_test_method", "TEXT NOT NULL DEFAULT 'count_poisson'")
    _ensure_column(cursor, "intel_topic_trends", "pettitt_p", "REAL NOT NULL DEFAULT 1")
    _ensure_column(cursor, "intel_topic_trends", "pettitt_q", "REAL NOT NULL DEFAULT 1")
    _ensure_column(cursor, "intel_topic_trends", "delta_bic", "REAL NOT NULL DEFAULT -999")
    _ensure_column(cursor, "intel_topic_trends", "change_point_index", "INTEGER")
    _ensure_column(cursor, "intel_topic_trends", "taskshift_emerging", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(cursor, "intel_topic_trends", "taskshift_high_confidence", "INTEGER NOT NULL DEFAULT 0")


def ensure_intel_event_tables(cursor) -> None:
    """事件抽取表（第二阶段）：每篇文章可抽多个结构化事件。

    event_hash 是事件指纹（subject+action+object+type 归一化的 sha256[:16]），
    用于跨文章把"同一事件的不同报道"聚成簇。与 intel_evidence_groups（标题
    去重/证据）物理隔离，互不影响。
    """
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS intel_article_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER NOT NULL,
            event_index INTEGER NOT NULL DEFAULT 0,
            industry_pack_id TEXT NOT NULL DEFAULT '',
            subject TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL DEFAULT '',
            object TEXT NOT NULL DEFAULT '',
            entities_json TEXT NOT NULL DEFAULT '[]',
            event_time TEXT NOT NULL DEFAULT '',
            -- 状态维度（阶段 7）：事件发生前后主体所处的状态。
            -- "发布/处罚/获批"这类动作只说清发生了什么，说清"从什么变成什么"才能支持
            -- 建图后的因果与趋势推理；抽不到就留空（绝不编造）。
            state_before TEXT NOT NULL DEFAULT '',
            state_after TEXT NOT NULL DEFAULT '',
            event_type TEXT NOT NULL DEFAULT 'other'
                CHECK (event_type IN ('regulation','enforcement','release','market','transaction','other')),
            subject_type TEXT NOT NULL DEFAULT 'entity'
                CHECK (subject_type IN ('entity', 'topic')),
            event_hash TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL DEFAULT '',
            llm_model_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE,
            UNIQUE(article_id, event_index)
        )
        """
    )
    _ensure_column(
        cursor, "intel_article_events", "subject_type", "TEXT NOT NULL DEFAULT 'entity'"
    )
    _ensure_column(
        cursor, "intel_article_events", "state_before", "TEXT NOT NULL DEFAULT ''"
    )
    _ensure_column(
        cursor, "intel_article_events", "state_after", "TEXT NOT NULL DEFAULT ''"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_events_article "
        "ON intel_article_events(article_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_events_hash "
        "ON intel_article_events(event_hash)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_events_pack_time "
        "ON intel_article_events(industry_pack_id, event_time)"
    )


def ensure_intel_subject_tables(cursor) -> None:
    """主体规范表（T5）：LLM 抽取的 subject → 话题级规范名 canonical_name（嵌入聚类归并）。

    aggregate_subject_clusters 按 canonical_name 分组，使"国家税务总局/税务部门"
    "J.P. Morgan私人银行/Private Bank" 这类同主体不同写法合并。
    """
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS intel_subject_canonical (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            industry_pack_id TEXT NOT NULL,
            subject_text TEXT NOT NULL,
            canonical_name TEXT NOT NULL,
            subject_key TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            UNIQUE(industry_pack_id, subject_text)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_subject_canonical_pack "
        "ON intel_subject_canonical(industry_pack_id, subject_text)"
    )


def ensure_intel_kg_tables(cursor) -> None:
    """知识图谱派生表（阶段 8）：kg_nodes / kg_edges。

    图是**派生视图**，源表（intel_article_events / intel_subject_canonical / intel_topics）
    才是准；所以这里只存"归并结果 + 幂等键 + 来源指纹"，随时可以删表重建。
    不引在线图数据库：PostgreSQL/SQLite 表 + 索引足够支撑邻域查询。

      · 节点：实体（subject_key）与主题（topic_key）。
      · 边：事件三元组（主体 → 客体），带 article_id / event_time / confidence / evidence_ref；
        边身份 = (src, action, dst, article_id)，保证**同一篇文章重跑不产生重复边**。
    """
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS kg_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_key TEXT NOT NULL,
            node_type TEXT NOT NULL DEFAULT 'entity'
                CHECK (node_type IN ('entity', 'topic')),
            label TEXT NOT NULL DEFAULT '',
            industry_pack_id TEXT NOT NULL DEFAULT '',
            article_count INTEGER NOT NULL DEFAULT 0,
            event_count INTEGER NOT NULL DEFAULT 0,
            first_seen TEXT NOT NULL DEFAULT '',
            last_seen TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            UNIQUE(industry_pack_id, node_type, node_key)
        )
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS kg_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            edge_key TEXT NOT NULL,
            src_key TEXT NOT NULL DEFAULT '',
            dst_key TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL DEFAULT '',
            event_type TEXT NOT NULL DEFAULT 'other',
            industry_pack_id TEXT NOT NULL DEFAULT '',
            article_id INTEGER,
            event_time TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0.5,
            evidence_ref TEXT NOT NULL DEFAULT '',
            state_before TEXT NOT NULL DEFAULT '',
            state_after TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            UNIQUE(edge_key)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_edges_src ON kg_edges(industry_pack_id, src_key)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_edges_dst ON kg_edges(industry_pack_id, dst_key)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_edges_time ON kg_edges(industry_pack_id, event_time)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_edges_article ON kg_edges(article_id)"
    )


def ensure_intel_core_tables(cursor) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS article_intel_classifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER NOT NULL,
            industry_pack_id TEXT NOT NULL,
            activation_id TEXT NOT NULL DEFAULT '',
            industry_pack_version TEXT NOT NULL,
            classifier_version TEXT NOT NULL,
            article_content_hash TEXT NOT NULL,
            rule_category TEXT NOT NULL CHECK (rule_category IN ('trend', 'event', 'other')),
            rule_confidence REAL NOT NULL DEFAULT 0 CHECK (rule_confidence >= 0 AND rule_confidence <= 1),
            rule_reason TEXT NOT NULL DEFAULT '',
            score_details_json TEXT NOT NULL DEFAULT '{{}}',
            matched_keywords_json TEXT NOT NULL DEFAULT '[]',
            llm_category TEXT CHECK (llm_category IS NULL OR llm_category IN ('trend', 'event', 'other')),
            llm_confidence REAL CHECK (llm_confidence IS NULL OR (llm_confidence >= 0 AND llm_confidence <= 1)),
            llm_reason TEXT NOT NULL DEFAULT '',
            why_important TEXT NOT NULL DEFAULT '',
            trend_summary TEXT NOT NULL DEFAULT '',
            topic_tags_json TEXT NOT NULL DEFAULT '[]',
            final_category TEXT NOT NULL CHECK (final_category IN ('trend', 'event', 'other')),
            final_confidence REAL NOT NULL DEFAULT 0 CHECK (final_confidence >= 0 AND final_confidence <= 1),
            final_reason TEXT NOT NULL DEFAULT '',
            result_source TEXT NOT NULL DEFAULT 'rule',
            fusion_version TEXT NOT NULL DEFAULT '',
            llm_model_id TEXT NOT NULL DEFAULT '',
            llm_prompt_version TEXT NOT NULL DEFAULT '',
            llm_error TEXT NOT NULL DEFAULT '',
            classified_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE,
            UNIQUE(article_id, industry_pack_id)
        )
        """
    )
    _ensure_column(
        cursor,
        "article_intel_classifications",
        "activation_id",
        "TEXT NOT NULL DEFAULT ''",
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS intel_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_type TEXT NOT NULL,
            dedupe_key TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{{}}',
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'retry_wait', 'completed', 'failed', 'cancelled')),
            priority INTEGER NOT NULL DEFAULT 0,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            next_retry_at TEXT,
            lease_owner TEXT,
            lease_expires_at TEXT,
            result_json TEXT NOT NULL DEFAULT '{{}}',
            last_error TEXT NOT NULL DEFAULT '',
            request_id TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            completed_at TEXT,
            started_at TEXT
        )
        """
    )
    _ensure_column(cursor, "intel_jobs", "created_by", "TEXT NOT NULL DEFAULT ''")
    # started_at：本次领取的开始时间（updated_at 被心跳不断刷新，无法用来算运行时长）。
    # 有了它才能做"运行时长超阈值就回收"的巡检，也才能在 worker 心跳里上报当前作业跑了多久。
    _ensure_column(cursor, "intel_jobs", "started_at", "TEXT")

    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS intel_worker_heartbeats (
            worker_id TEXT PRIMARY KEY,
            lane TEXT NOT NULL DEFAULT '',
            pid INTEGER,
            host TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            inflight_count INTEGER NOT NULL DEFAULT 0,
            timeout_streak INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            detail_json TEXT NOT NULL DEFAULT '{{}}',
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL}
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_worker_heartbeats_seen "
        "ON intel_worker_heartbeats(last_seen)"
    )

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_classifications_industry_category "
        "ON article_intel_classifications(industry_pack_id, final_category)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_classifications_activation "
        "ON article_intel_classifications(activation_id, industry_pack_id, final_category)"
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS financial_addon_article_matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER NOT NULL,
            activation_id TEXT NOT NULL,
            primary_industry_pack_id TEXT NOT NULL,
            matched_keywords_json TEXT NOT NULL DEFAULT '[]',
            is_visible INTEGER NOT NULL DEFAULT 0 CHECK (is_visible IN (0, 1)),
            evaluated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE,
            UNIQUE(article_id, activation_id)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_financial_addon_matches_activation "
        "ON financial_addon_article_matches(activation_id, is_visible, article_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_classifications_classified_at "
        "ON article_intel_classifications(classified_at)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_classifications_article_hash "
        "ON article_intel_classifications(article_id, article_content_hash)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_jobs_claim "
        "ON intel_jobs(status, next_retry_at, priority DESC, created_at)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_jobs_lease "
        "ON intel_jobs(status, lease_expires_at)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_jobs_type_created "
        "ON intel_jobs(job_type, created_at)"
    )
    # dedupe_key 全唯一索引（与生产 PG 库现状一致）：
    # - enqueue_job 的 ON CONFLICT(dedupe_key) DO NOTHING 需要**非部分**唯一索引，
    #   SQLite 的 upsert 不支持部分索引作为冲突目标（PG 的列列表推断同样不支持）；
    # - 部分索引（WHERE status IN (...)）曾导致 SQLite 下入队直接报
    #   "ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint"。
    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_intel_jobs_active_dedupe
        ON intel_jobs(dedupe_key)
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS intel_article_translation_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER NOT NULL,
            target_language TEXT NOT NULL,
            translation_scope TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            model_id TEXT NOT NULL DEFAULT '',
            translated_text TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE,
            UNIQUE(article_id, target_language, translation_scope, source_hash, model_id)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_translation_cache_lookup "
        "ON intel_article_translation_cache(article_id, target_language, translation_scope, source_hash)"
    )
    ensure_industry_pack_version_tables(cursor)
    ensure_industry_pack_activation_tables(cursor)
    ensure_intel_source_tables(cursor)
    ensure_intel_report_tables(cursor)
    ensure_intel_embedding_tables(cursor)
    ensure_intel_trend_tables(cursor)
    ensure_intel_event_tables(cursor)
    ensure_intel_subject_tables(cursor)
    ensure_intel_kg_tables(cursor)
    ensure_intel_article_field_tables(cursor)
    ensure_intel_report_candidate_tables(cursor)
    cursor.execute("CREATE TABLE IF NOT EXISTS intel_runtime_settings (setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT " + UTC_NOW_SQL + ")")
    cursor.execute(
        """CREATE TABLE IF NOT EXISTS intel_pack_backups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            industry_pack_id TEXT NOT NULL,
            article_ids_json TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT """ + UTC_NOW_SQL + ")"
    )


def ensure_industry_pack_version_tables(cursor) -> None:
    """Versioned published manifests and one mutable draft per industry pack."""

    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS industry_pack_registry (
            industry_pack_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            origin TEXT NOT NULL DEFAULT 'custom'
                CHECK (origin IN ('custom')),
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'deleted')),
            created_by TEXT NOT NULL DEFAULT '',
            updated_by TEXT NOT NULL DEFAULT '',
            deleted_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            deleted_at TEXT
        )
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS industry_pack_lifecycle_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            industry_pack_id TEXT NOT NULL,
            pack_name TEXT NOT NULL DEFAULT '',
            event_type TEXT NOT NULL
                CHECK (event_type IN ('created', 'deleted')),
            actor TEXT NOT NULL DEFAULT '',
            details_json TEXT NOT NULL DEFAULT '{{}}',
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL}
        )
        """
    )

    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS industry_pack_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            industry_pack_id TEXT NOT NULL,
            version_number INTEGER NOT NULL CHECK (version_number > 0),
            pack_version TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            parent_version_id INTEGER,
            manifest_json TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT '',
            published_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            FOREIGN KEY (parent_version_id) REFERENCES industry_pack_versions(id),
            UNIQUE(industry_pack_id, version_number),
            UNIQUE(industry_pack_id, content_sha256)
        )
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS industry_pack_drafts (
            industry_pack_id TEXT PRIMARY KEY,
            base_version_id INTEGER,
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
            manifest_json TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT '',
            updated_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            FOREIGN KEY (base_version_id) REFERENCES industry_pack_versions(id)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_industry_pack_versions_latest "
        "ON industry_pack_versions(industry_pack_id, version_number DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_industry_pack_registry_status "
        "ON industry_pack_registry(status, industry_pack_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_industry_pack_lifecycle_events_time "
        "ON industry_pack_lifecycle_events(created_at DESC, id DESC)"
    )


def ensure_industry_pack_activation_tables(cursor) -> None:
    """Auditable state for prepared and completed industry-pack activations."""

    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS industry_pack_activations (
            id TEXT PRIMARY KEY,
            previous_pack_id TEXT NOT NULL DEFAULT '',
            previous_version_id INTEGER,
            target_pack_id TEXT NOT NULL,
            target_version_id INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('prepared','draining','applying','verifying','active','rolled_back','failed')
            ),
            is_current INTEGER NOT NULL DEFAULT 0 CHECK (is_current IN (0, 1)),
            plan_sha256 TEXT NOT NULL,
            source_plan_json TEXT NOT NULL,
            backup_path TEXT NOT NULL DEFAULT '',
            backup_sha256 TEXT NOT NULL DEFAULT '',
            backup_size INTEGER NOT NULL DEFAULT 0,
            backup_schema_version INTEGER NOT NULL DEFAULT 0,
            backup_integrity TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            started_at TEXT,
            completed_at TEXT,
            FOREIGN KEY (previous_version_id) REFERENCES industry_pack_versions(id),
            FOREIGN KEY (target_version_id) REFERENCES industry_pack_versions(id)
        )
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS industry_pack_activation_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            activation_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{{}}',
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            FOREIGN KEY (activation_id) REFERENCES industry_pack_activations(id) ON DELETE CASCADE
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_industry_pack_activation_current "
        "ON industry_pack_activations(is_current, completed_at DESC)"
    )


def ensure_intel_report_tables(cursor) -> None:
    """Persistent state for PDF/report ingestion and version checks."""
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS intel_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER,
            industry_pack_id TEXT NOT NULL,
            report_url TEXT NOT NULL UNIQUE,
            resolved_url TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            article_id INTEGER,
            content_sha256 TEXT NOT NULL DEFAULT '',
            local_path TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'discovered'
                CHECK (status IN ('discovered', 'downloaded', 'ingested', 'unchanged', 'failed')),
            extraction_method TEXT NOT NULL DEFAULT '',
            extracted_characters INTEGER NOT NULL DEFAULT 0,
            last_checked_at TEXT,
            last_changed_at TEXT,
            last_error TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{{}}',
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            FOREIGN KEY (source_id) REFERENCES intel_sources(id) ON DELETE SET NULL,
            FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE SET NULL
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_intel_reports_pack_status ON intel_reports(industry_pack_id, status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_intel_reports_source ON intel_reports(source_id, last_checked_at)")


def ensure_intel_source_tables(cursor) -> None:
    """Create the additive source registry without changing legacy URL tables."""
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS intel_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_source_url TEXT NOT NULL UNIQUE,
            source_url TEXT NOT NULL,
            source_name TEXT NOT NULL DEFAULT '',
            source_description TEXT NOT NULL DEFAULT '',
            source_type TEXT NOT NULL DEFAULT 'website'
                CHECK (source_type IN ('rss', 'list_page', 'website')),
            content_type TEXT NOT NULL DEFAULT 'other'
                CHECK (content_type IN ('official', 'media', 'report', 'event', 'other')),
            market TEXT NOT NULL DEFAULT '',
            authority_level INTEGER NOT NULL DEFAULT 2
                CHECK (authority_level BETWEEN 1 AND 5),
            polling_interval_minutes INTEGER NOT NULL DEFAULT 1440
                CHECK (polling_interval_minutes BETWEEN 5 AND 10080),
            is_enabled INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0, 1)),
            authority_is_manual INTEGER NOT NULL DEFAULT 0
                CHECK (authority_is_manual IN (0, 1)),
            industries_are_manual INTEGER NOT NULL DEFAULT 0
                CHECK (industries_are_manual IN (0, 1)),
            enabled_is_manual INTEGER NOT NULL DEFAULT 0
                CHECK (enabled_is_manual IN (0, 1)),
            metadata_json TEXT NOT NULL DEFAULT '{{}}',
            last_synced_at TEXT,
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL}
        )
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS intel_source_industries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            industry_pack_id TEXT NOT NULL,
            is_manual INTEGER NOT NULL DEFAULT 0 CHECK (is_manual IN (0, 1)),
            ownership_type TEXT NOT NULL DEFAULT 'legacy',
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            declared_version_id INTEGER,
            manifest_source_sha256 TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            FOREIGN KEY (source_id) REFERENCES intel_sources(id) ON DELETE CASCADE,
            UNIQUE(source_id, industry_pack_id)
        )
        """
    )
    _ensure_column(
        cursor,
        "intel_source_industries",
        "ownership_type",
        "TEXT NOT NULL DEFAULT 'legacy'",
    )
    _ensure_column(
        cursor,
        "intel_source_industries",
        "is_active",
        "INTEGER NOT NULL DEFAULT 1",
    )
    _ensure_column(
        cursor,
        "intel_source_industries",
        "declared_version_id",
        "INTEGER",
    )
    _ensure_column(
        cursor,
        "intel_source_industries",
        "manifest_source_sha256",
        "TEXT NOT NULL DEFAULT ''",
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS intel_source_origins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            origin_type TEXT NOT NULL
                CHECK (origin_type IN ('managed_url', 'scheduled_task', 'manual', 'industry_pack')),
            origin_key TEXT NOT NULL,
            managed_url_id INTEGER,
            scheduled_task_id INTEGER,
            origin_url TEXT NOT NULL DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            last_seen_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            FOREIGN KEY (source_id) REFERENCES intel_sources(id) ON DELETE CASCADE,
            FOREIGN KEY (managed_url_id) REFERENCES managed_urls(id) ON DELETE CASCADE,
            FOREIGN KEY (scheduled_task_id) REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
            UNIQUE(origin_type, origin_key)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_sources_type_enabled "
        "ON intel_sources(source_type, is_enabled)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_sources_content_authority "
        "ON intel_sources(content_type, authority_level DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_source_industries_active_pack "
        "ON intel_source_industries(industry_pack_id, is_active, source_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_source_origins_source "
        "ON intel_source_origins(source_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_source_origins_managed_url "
        "ON intel_source_origins(managed_url_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_source_origins_scheduled_task "
        "ON intel_source_origins(scheduled_task_id)"
    )
    _ensure_column(cursor, "intel_sources", "last_scan_at", "TEXT")
    _ensure_column(cursor, "intel_sources", "last_successful_scan_at", "TEXT")
    _ensure_column(cursor, "intel_sources", "last_scan_status", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(cursor, "intel_sources", "last_scan_error", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(
        cursor,
        "intel_sources",
        "consecutive_scan_failures",
        "INTEGER NOT NULL DEFAULT 0",
    )
    ensure_intel_candidate_tables(cursor)


def ensure_intel_article_field_tables(cursor) -> None:
    """产品型文章的结构化字段（产品功能/应用场景/核心参数）。

    这类字段通常只存在于产品页的图片里，正文抽取读不到；由 ``enrich_repair``
    worker 任务用 VPN OCR + LLM 抽取回填到本表，详情页据此展示。
    """
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS intel_article_fields (
            article_id INTEGER PRIMARY KEY,
            product_features TEXT NOT NULL DEFAULT '',
            application_scenarios TEXT NOT NULL DEFAULT '',
            core_params TEXT NOT NULL DEFAULT '',
            ocr_text TEXT NOT NULL DEFAULT '',
            ocr_summary TEXT NOT NULL DEFAULT '',
            ocr_title TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_article_fields_article "
        "ON intel_article_fields(article_id)"
    )


def ensure_intel_report_candidate_tables(cursor) -> None:
    """报告 URL 探测候选：`report_discover` 从信源里找到的、可下载的 PDF/报告页直链。

    命中后由 worker enqueue `report_ingest` 自动下载入库；content_sha256 用于版本去重。
    探测层是包无关的，仅针对 `intel_sources.content_type='report'` 的信源。
    """
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS intel_report_candidates (
            id BIGSERIAL PRIMARY KEY,
            canonical_url TEXT NOT NULL UNIQUE,
            report_url TEXT NOT NULL,
            source_id INTEGER,
            industry_pack_id TEXT NOT NULL DEFAULT '',
            url_type TEXT NOT NULL DEFAULT 'pdf'
                CHECK (url_type IN ('pdf', 'html_report', 'unknown')),
            title_hint TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'queued', 'ingested', 'failed', 'discarded')),
            content_sha256 TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            last_seen_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            FOREIGN KEY (source_id) REFERENCES intel_sources(id) ON DELETE SET NULL
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_report_candidates_status "
        "ON intel_report_candidates(status, industry_pack_id)"
    )


def ensure_intel_candidate_tables(cursor) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS intel_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_url TEXT NOT NULL UNIQUE,
            original_url TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            normalized_title TEXT NOT NULL DEFAULT '',
            title_hash TEXT NOT NULL DEFAULT '',
            domain TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            published_at TEXT,
            published_precision TEXT NOT NULL DEFAULT 'unknown'
                CHECK (published_precision IN ('exact', 'date', 'unknown')),
            first_seen_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            last_seen_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            quick_score REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'discovered'
                CHECK (status IN (
                    'discovered', 'queued', 'dispatching', 'crawled',
                    'retry_wait', 'discarded', 'failed'
                )),
            possible_duplicate_of INTEGER,
            duplicate_reason TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            next_retry_at TEXT,
            lease_owner TEXT,
            lease_expires_at TEXT,
            last_error TEXT NOT NULL DEFAULT '',
            crawler_task_id TEXT NOT NULL DEFAULT '',
            article_id INTEGER,
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            FOREIGN KEY (possible_duplicate_of) REFERENCES intel_candidates(id) ON DELETE SET NULL,
            FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE SET NULL
        )
        """
    )
    # Admission and quality are deliberately independent of the legacy
    # candidate status column, whose CHECK constraint exists in deployed DBs.
    # Keeping these as explicit columns makes the migration backwards-safe.
    for name, definition in (
        ("quality_status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("admission_status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("admission_reason", "TEXT NOT NULL DEFAULT ''"),
        ("admission_confidence", "REAL"),
        ("metadata_status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("metadata_issues_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("extraction_attempt_count", "INTEGER NOT NULL DEFAULT 0"),
    ):
        _ensure_column(cursor, "intel_candidates", name, definition)
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS intel_extraction_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            strategy TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('started','passed','failed','retryable')),
            content_length INTEGER NOT NULL DEFAULT 0,
            quality_score REAL,
            integrity_issues_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{{}}',
            error_message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            FOREIGN KEY (candidate_id) REFERENCES intel_candidates(id) ON DELETE CASCADE
        )
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS intel_url_expansion_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            industry_pack_id TEXT NOT NULL,
            parent_candidate_id INTEGER,
            parent_url TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            original_url TEXT NOT NULL,
            anchor_text TEXT NOT NULL DEFAULT '',
            context_text TEXT NOT NULL DEFAULT '',
            discovery_method TEXT NOT NULL DEFAULT 'admission_page',
            score REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','dispatched')),
            rejection_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            FOREIGN KEY (parent_candidate_id) REFERENCES intel_candidates(id) ON DELETE SET NULL,
            UNIQUE(industry_pack_id, parent_url, canonical_url)
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_intel_extraction_attempts_candidate ON intel_extraction_attempts(candidate_id, id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_intel_url_expansion_pending ON intel_url_expansion_candidates(status, score DESC)")
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS intel_candidate_industries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            industry_pack_id TEXT NOT NULL,
            activation_id TEXT NOT NULL DEFAULT '',
            quick_score REAL NOT NULL DEFAULT 0,
            threshold REAL NOT NULL DEFAULT 0,
            matched_keywords_json TEXT NOT NULL DEFAULT '[]',
            should_queue INTEGER NOT NULL DEFAULT 0 CHECK (should_queue IN (0, 1)),
            first_seen_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            last_seen_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            FOREIGN KEY (candidate_id) REFERENCES intel_candidates(id) ON DELETE CASCADE,
            UNIQUE(candidate_id, industry_pack_id)
        )
        """
    )
    _ensure_column(
        cursor,
        "intel_candidate_industries",
        "activation_id",
        "TEXT NOT NULL DEFAULT ''",
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS intel_scan_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER,
            industry_pack_id TEXT NOT NULL,
            activation_id TEXT NOT NULL DEFAULT '',
            scan_window_key TEXT NOT NULL DEFAULT '',
            requested_pack_ids_json TEXT NOT NULL DEFAULT '[]',
            scanner_type TEXT NOT NULL
                CHECK (scanner_type IN ('rss', 'list_page', 'website', 'serpapi', 'tavily', 'ggzy_api', 'agent_reach')),
            status TEXT NOT NULL DEFAULT 'running'
                CHECK (status IN (
                    'running', 'completed', 'partial', 'failed',
                    'rate_limited', 'skipped'
                )),
            discovered_count INTEGER NOT NULL DEFAULT 0,
            queued_count INTEGER NOT NULL DEFAULT 0,
            duplicate_count INTEGER NOT NULL DEFAULT 0,
            below_threshold_count INTEGER NOT NULL DEFAULT 0,
            request_count INTEGER NOT NULL DEFAULT 0,
            error_type TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{{}}',
            started_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            completed_at TEXT,
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            FOREIGN KEY (source_id) REFERENCES intel_sources(id) ON DELETE SET NULL
        )
        """
    )
    _ensure_column(cursor, "intel_scan_runs", "scan_window_key", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(
        cursor, "intel_scan_runs", "activation_id", "TEXT NOT NULL DEFAULT ''"
    )
    _ensure_column(
        cursor,
        "intel_scan_runs",
        "requested_pack_ids_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    # 扫描类型 CHECK 约束演进（PG 老表不含 'tavily'/'ggzy_api'/'agent_reach'，需显式重建；
    # SQLite 老库由新库建表文本覆盖）：
    # DROP/ADD 均幂等，失败静默跳过（SQLite 不支持 ALTER 约束，新库建表文本已含这些值）。
    # 注意：postgres_shims 里还有一处同名的 DROP/ADD，两处必须保持一致，
    # 否则后执行的一处会把这里的取值覆盖回去（ggzy_api 就踩过这个坑）。
    try:
        cursor.execute(
            "ALTER TABLE intel_scan_runs DROP CONSTRAINT IF EXISTS intel_scan_runs_scanner_type_check"
        )
    except Exception:
        pass
    try:
        cursor.execute(
            "ALTER TABLE intel_scan_runs ADD CONSTRAINT intel_scan_runs_scanner_type_check "
            "CHECK (scanner_type IN ('rss', 'list_page', 'website', 'serpapi', 'tavily', "
            "'ggzy_api', 'agent_reach')) NOT VALID"
        )
    except Exception:
        pass
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS intel_candidate_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            source_id INTEGER,
            scan_run_id INTEGER,
            activation_id TEXT NOT NULL DEFAULT '',
            observation_type TEXT NOT NULL
                CHECK (observation_type IN ('rss', 'list_page', 'website', 'serpapi', 'tavily', 'ggzy_api', 'agent_reach')),
            observation_key TEXT NOT NULL,
            query_text TEXT NOT NULL DEFAULT '',
            raw_url TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            published_at TEXT,
            observed_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            last_observed_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            seen_count INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            FOREIGN KEY (candidate_id) REFERENCES intel_candidates(id) ON DELETE CASCADE,
            FOREIGN KEY (source_id) REFERENCES intel_sources(id) ON DELETE SET NULL,
            FOREIGN KEY (scan_run_id) REFERENCES intel_scan_runs(id) ON DELETE SET NULL,
            UNIQUE(candidate_id, observation_key)
        )
        """
    )
    _ensure_column(
        cursor,
        "intel_candidate_observations",
        "activation_id",
        "TEXT NOT NULL DEFAULT ''",
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS intel_api_usage (
            usage_date TEXT NOT NULL,
            service TEXT NOT NULL,
            usage_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            PRIMARY KEY (usage_date, service)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_candidates_status_retry "
        "ON intel_candidates(status, next_retry_at, first_seen_at)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_candidates_lease "
        "ON intel_candidates(status, lease_expires_at)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_candidates_title_audit "
        "ON intel_candidates(domain, title_hash)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_candidates_article "
        "ON intel_candidates(article_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_candidate_industries_pack "
        "ON intel_candidate_industries(industry_pack_id, should_queue, quick_score DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_candidate_industries_activation "
        "ON intel_candidate_industries(activation_id, industry_pack_id, should_queue)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_candidate_observations_source "
        "ON intel_candidate_observations(source_id, last_observed_at)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_scan_runs_source_started "
        "ON intel_scan_runs(source_id, started_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_scan_runs_industry_started "
        "ON intel_scan_runs(industry_pack_id, started_at DESC)"
    )
    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_intel_scan_runs_successful_window
        ON intel_scan_runs(scan_window_key)
        WHERE scan_window_key != '' AND status IN ('running', 'completed', 'partial')
        """
    )
    # 逐个独立容错：任何一组表初始化失败都不能连带中断后续组的建表
    for _fn in (
        ensure_intel_topic_search_test_tables,
        ensure_intel_topic_tables,
        ensure_pack_attention_tables,
        ensure_intel_evidence_tables,
        ensure_user_gate_tables,
    ):
        try:
            _fn(cursor)
        except Exception as _exc:
            print(f"⚠️ intel schema 初始化 {getattr(_fn, '__name__', _fn)} 失败: {_exc}")


def ensure_user_gate_tables(cursor) -> None:
    """用户个人门禁覆盖 + 文章可见性物化（按用户层，叠加语义）。

    pack_user_gate_overrides：用户对所属行业包的**个人**覆盖（锚点/机构/品牌/趋势主题/
    信源启停/报告栏目/趋势参数）。未设置的用户不写行 = 看全包，保持既有行为、写入量最小。
    锚点词由接口强制校验"必须是官方门禁的子集"（只能收紧不能放宽）。

    article_user_visibility：入库时物化的可见性。只对**有个人设置**的用户写行，并记录
    命中了该用户的哪些竞争对手（brands），供"我的竞争对手动态"板块使用；读取面按
    "有设置则 JOIN、无设置则不过滤"统一处理。
    """
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS pack_user_gate_overrides (
            pack_user_id INTEGER PRIMARY KEY,
            industry_pack_id TEXT NOT NULL,
            anchor_keywords_json TEXT NOT NULL DEFAULT '[]',
            entity_keywords_json TEXT NOT NULL DEFAULT '[]',
            brand_keywords_json TEXT NOT NULL DEFAULT '[]',
            trend_topics_json TEXT NOT NULL DEFAULT '[]',
            source_overrides_json TEXT NOT NULL DEFAULT '{{}}',
            report_seeds_json TEXT NOT NULL DEFAULT '{{}}',
            trend_settings_json TEXT NOT NULL DEFAULT '{{}}',
            updated_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL}
        )
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS article_user_visibility (
            article_id INTEGER NOT NULL,
            pack_user_id INTEGER NOT NULL,
            matched_brands_json TEXT NOT NULL DEFAULT '[]',
            matched_anchors_json TEXT NOT NULL DEFAULT '[]',
            topic_keys_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE,
            PRIMARY KEY (article_id, pack_user_id)
        )
        """
    )
    # 已有库补列（主题归属按用户存：同一篇文章对不同用户可有不同主题，互不影响）
    # Postgres 用 ADD COLUMN IF NOT EXISTS；SQLite 不支持该语法，退化为直接 ADD（重复列会报错并被吞掉）
    for _sql in (
        "ALTER TABLE article_user_visibility ADD COLUMN IF NOT EXISTS topic_keys_json TEXT DEFAULT '[]'",
        "ALTER TABLE article_user_visibility ADD COLUMN topic_keys_json TEXT DEFAULT '[]'",
    ):
        try:
            cursor.execute(_sql)
            break
        except Exception:
            continue
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_article_user_visibility_user "
        "ON article_user_visibility(pack_user_id, article_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_pack_user_gate_overrides_pack "
        "ON pack_user_gate_overrides(industry_pack_id)"
    )


def ensure_intel_topic_search_test_tables(cursor) -> None:
    """主题搜索词测试记录：按「行业包 + 主题」限制测试次数（每主题 5 次）。

    有意不使用 AUTOINCREMENT 自增主键，也不用 SQLite 的 DEFAULT 时间函数：
    PostgreSQL 兼容层只在 executescript 路径翻译这些 SQLite 方言，走 cursor.execute
    会直接语法报错。本表只做计数与审计，created_at 由写入方显式提供。
    """
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS intel_topic_search_tests (
            industry_pack_id TEXT NOT NULL,
            topic_key TEXT NOT NULL,
            query_text TEXT NOT NULL DEFAULT '',
            google_count INTEGER NOT NULL DEFAULT 0,
            tavily_count INTEGER NOT NULL DEFAULT 0,
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_topic_search_tests_pack_topic "
        "ON intel_topic_search_tests(industry_pack_id, topic_key)"
    )


def ensure_intel_topic_tables(cursor) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS intel_topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            industry_pack_id TEXT NOT NULL,
            topic_key TEXT NOT NULL,
            topic_name TEXT NOT NULL,
            topic_source TEXT NOT NULL DEFAULT 'fixed'
                CHECK (topic_source IN ('fixed', 'automatic', 'watch')),
            keywords_json TEXT NOT NULL DEFAULT '[]',
            summary TEXT NOT NULL DEFAULT '',
            summary_source TEXT NOT NULL DEFAULT 'rule',
            summary_version TEXT NOT NULL DEFAULT '',
            content_signature TEXT NOT NULL DEFAULT '',
            article_count_cache INTEGER NOT NULL DEFAULT 0,
            representative_article_id INTEGER,
            last_clustered_at TEXT,
            last_summary_at TEXT,
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            FOREIGN KEY (representative_article_id) REFERENCES articles(id) ON DELETE SET NULL,
            UNIQUE(industry_pack_id, topic_key)
        )
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS intel_topic_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id INTEGER NOT NULL,
            article_id INTEGER NOT NULL,
            association_score REAL NOT NULL DEFAULT 0,
            assignment_method TEXT NOT NULL DEFAULT 'rule_keyword',
            evidence_json TEXT NOT NULL DEFAULT '{{}}',
            assigned_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            pinned INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (topic_id) REFERENCES intel_topics(id) ON DELETE CASCADE,
            FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE,
            UNIQUE(topic_id, article_id)
        )
        """
    )
    # 置顶列：手工发文在所在主题排第 1 位（ORDER BY pinned DESC）
    _ensure_column(cursor, "intel_topic_articles", "pinned", "INTEGER NOT NULL DEFAULT 0")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_topics_industry_updated "
        "ON intel_topics(industry_pack_id, updated_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_topic_articles_article "
        "ON intel_topic_articles(article_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_topic_articles_topic_score "
        "ON intel_topic_articles(topic_id, association_score DESC)"
    )
    # 去 assignment_method CHECK：允许 bertopic 等新方法（SQLite 不能改 CHECK，重建表）
    _ta_ddl = cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='intel_topic_articles'"
    ).fetchone()
    if _ta_ddl and "CHECK (assignment_method IN" in str(_ta_ddl[0] or ""):
        cursor.execute("ALTER TABLE intel_topic_articles RENAME TO _intel_topic_articles_old")
        cursor.execute(
            f"""
            CREATE TABLE intel_topic_articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id INTEGER NOT NULL,
                article_id INTEGER NOT NULL,
                association_score REAL NOT NULL DEFAULT 0,
                assignment_method TEXT NOT NULL DEFAULT 'rule_keyword',
                evidence_json TEXT NOT NULL DEFAULT '{{}}',
                assigned_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
                updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
                FOREIGN KEY (topic_id) REFERENCES intel_topics(id) ON DELETE CASCADE,
                FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE,
                UNIQUE(topic_id, article_id)
            )
            """
        )
        cursor.execute(
            "INSERT INTO intel_topic_articles(id, topic_id, article_id, association_score, "
            "assignment_method, evidence_json, assigned_at, updated_at) "
            "SELECT id, topic_id, article_id, association_score, assignment_method, "
            "evidence_json, assigned_at, updated_at FROM _intel_topic_articles_old"
        )
        cursor.execute("DROP TABLE _intel_topic_articles_old")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_intel_topic_articles_article ON intel_topic_articles(article_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_intel_topic_articles_topic_score ON intel_topic_articles(topic_id, association_score DESC)")


def ensure_pack_attention_tables(cursor) -> None:
    """注意力方向：把周报「下周关注」的线索固化成可跟踪、可回收的盯防项。

    背景：周报每周自动生成，其中「下周关注」列出 3~5 条值得继续跟踪的线索。
    这些线索是**动态**的（每周都变），不能塞进 fixed_topics（那是人工维护的固定主题，
    改一次要草稿→发布→激活），所以单独一张表按周保存：

    * 每条线索一行（direction=线索标题，keywords_json=从线索里抽出的盯防词）；
    * 盯防词同时用于：① 建一张动态主题卡「本周盯防 · 第N周」把命中文章挂上去；
      ② 进入搜索采集的查询词，主动搜这些线索；
    * 下一份周报生成时把上一周的行标为 closed 并结算 hit_count，便于回看
      「上周那几条线索最后命中了几篇」。
    """
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS pack_attention_directions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            industry_pack_id TEXT NOT NULL,
            week_key TEXT NOT NULL,
            week_label TEXT NOT NULL DEFAULT '',
            report_id INTEGER,
            report_title TEXT NOT NULL DEFAULT '',
            direction TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            keywords_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'active',
            source TEXT NOT NULL DEFAULT 'report',
            hit_count INTEGER NOT NULL DEFAULT 0,
            closed_at TEXT,
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            UNIQUE(industry_pack_id, week_key, direction)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_pack_attention_pack_status "
        "ON pack_attention_directions(industry_pack_id, status, week_key DESC)"
    )


def ensure_intel_evidence_tables(cursor) -> None:
    """Persist conservative, cross-source evidence groups for every pack."""

    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS intel_evidence_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            industry_pack_id TEXT NOT NULL,
            activation_id TEXT NOT NULL DEFAULT '',
            event_key TEXT NOT NULL,
            representative_article_id INTEGER NOT NULL,
            evidence_grade TEXT NOT NULL DEFAULT 'D'
                CHECK (evidence_grade IN ('A', 'B', 'C', 'D', 'CONFLICT')),
            base_evidence_grade TEXT NOT NULL DEFAULT 'D'
                CHECK (base_evidence_grade IN ('A', 'B', 'C', 'D')),
            independent_source_count INTEGER NOT NULL DEFAULT 0,
            max_authority_level INTEGER NOT NULL DEFAULT 1
                CHECK (max_authority_level BETWEEN 1 AND 5),
            conflict_status TEXT NOT NULL DEFAULT 'none'
                CHECK (conflict_status IN ('none', 'unresolved', 'authoritative_preferred')),
            conflict_details_json TEXT NOT NULL DEFAULT '[]',
            citations_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            FOREIGN KEY (representative_article_id) REFERENCES articles(id) ON DELETE CASCADE,
            UNIQUE(industry_pack_id, activation_id, event_key)
        )
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS intel_evidence_group_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evidence_group_id INTEGER NOT NULL,
            article_id INTEGER NOT NULL,
            source_id INTEGER,
            publisher_key TEXT NOT NULL DEFAULT '',
            source_role TEXT NOT NULL DEFAULT 'unclassified',
            authority_level INTEGER NOT NULL DEFAULT 1
                CHECK (authority_level BETWEEN 1 AND 5),
            title_similarity REAL NOT NULL DEFAULT 1,
            relationship TEXT NOT NULL DEFAULT 'representative'
                CHECK (relationship IN ('representative', 'corroborating', 'duplicate', 'conflicting')),
            created_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            updated_at TEXT NOT NULL DEFAULT {UTC_NOW_SQL},
            FOREIGN KEY (evidence_group_id) REFERENCES intel_evidence_groups(id) ON DELETE CASCADE,
            FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE,
            FOREIGN KEY (source_id) REFERENCES intel_sources(id) ON DELETE SET NULL,
            UNIQUE(evidence_group_id, article_id)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_evidence_groups_pack "
        "ON intel_evidence_groups(industry_pack_id, activation_id, updated_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_evidence_articles_article "
        "ON intel_evidence_group_articles(article_id, evidence_group_id)"
    )

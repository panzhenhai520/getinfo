#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Persistence operations for the market intelligence radar."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

import config
from intel_contracts import (
    DEFAULT_INDUSTRY_PACK_ID,
    normalize_internal_category,
    parse_time_range,
    public_category,
    utc_now,
    utc_text,
)
from sqlite_database import sqlite_db
from utils import coerce_int


ARTICLE_TIME_SQL = """
CASE
    WHEN a.publish_date IS NOT NULL AND LOWER(TRIM(a.publish_date)) NOT IN ('', 'none', 'null') AND LENGTH(TRIM(a.publish_date)) = 10
        THEN datetime(a.publish_date, '-8 hours')
    WHEN a.publish_date IS NOT NULL AND LOWER(TRIM(a.publish_date)) NOT IN ('', 'none', 'null')
        THEN a.publish_date
    WHEN a.first_crawled IS NOT NULL
        THEN a.first_crawled
    ELSE datetime(a.created_at, '-8 hours')
END
""".strip()

# 分类作业的优先级（数值越大越先被领取：claim 是 ORDER BY priority + 等待时长积分 DESC）。
# 维护类作业的优先级在 -45 ~ +5 之间，这里取 enqueue_job 允许的上限 100
# （coerce_int(priority, 0, -100, 100)），让分类稳定排在它们前面。
_CLASSIFICATION_JOB_PRIORITY = 100

# intel 侧建表/补列的一次性守卫（见 IntelRepository._ensure_intel_schema_once）
_SCHEMA_ENSURE_LOCK = threading.Lock()

# A classification may legitimately finish as ``other`` when it has no industry
# relevance at all.  That is useful for audit and model evaluation, but it must
# never enter a user's industry dashboard.  The dashboard gate therefore uses
# the configured pack's explicit industry hits, not generic trend/event words
# such as "报告" or "投资".  Since industry providers (能耗/算力/电力/风电场等)
# 主要靠 expanded/core 关键词命中而非实体锚点，这里放开到 anchor/expanded/core，
# 与派发准入 & 聚类放开后的 _classification_admitted 保持一致，避免"当日已分类"
# 统计把这类文章漏成 0。
INDUSTRY_KEYWORD_GATE_SQL = """
(
    COALESCE(json_array_length(json_extract(c.score_details_json, '$.hits.anchor')), 0) > 0
    OR COALESCE(json_array_length(json_extract(c.score_details_json, '$.hits.expanded')), 0) > 0
    OR COALESCE(json_array_length(json_extract(c.score_details_json, '$.hits.core')), 0) > 0
)
""".strip()

# Policy membership must come from classification evidence, not only from the
# denormalized display-keyword list. Industry-pack activation revalidates the
# anchor projection independently from trend/topic classification, and legacy
# rows may contain either topic keys or topic names.
POLICY_ARTICLE_SQL = """
(
    LOWER(COALESCE(a.domain, '')) IN ('www.nfra.gov.cn', 'www.xtxh.net')
    OR EXISTS (
        SELECT 1
        FROM json_each(
            CASE
                WHEN json_valid(COALESCE(c.topic_tags_json, '[]'))
                    THEN COALESCE(c.topic_tags_json, '[]')
                ELSE '[]'
            END
        ) AS policy_topic
        WHERE LOWER(CAST(policy_topic.value AS TEXT))
              IN ('policy_tax', '政策与税务', '政策法规', 'policy_regulation')
    )
    OR EXISTS (
        SELECT 1
        FROM json_each(
            CASE
                WHEN json_valid(COALESCE(c.score_details_json, '{}'))
                    THEN COALESCE(
                        json_extract(c.score_details_json, '$.topic_assignments'),
                        '[]'
                    )
                ELSE '[]'
            END
        ) AS policy_assignment
        WHERE LOWER(COALESCE(
            json_extract(policy_assignment.value, '$.key'),
            ''
        )) = 'policy_tax'
    )
    OR EXISTS (
        SELECT 1
        FROM json_each(
            CASE
                WHEN json_valid(COALESCE(c.score_details_json, '{}'))
                    THEN COALESCE(
                        json_extract(c.score_details_json, '$.hits.trend'),
                        '[]'
                    )
                ELSE '[]'
            END
        ) AS policy_signal
        WHERE CAST(policy_signal.value AS TEXT)
              IN ('政策', '监管', '税务', '税改', '税务宽免', '新政')
    )
    OR c.matched_keywords_json LIKE '%政策%'
    OR c.matched_keywords_json LIKE '%监管%'
    OR c.matched_keywords_json LIKE '%税务%'
    OR c.matched_keywords_json LIKE '%税改%'
    OR c.matched_keywords_json LIKE '%新政%'
)
""".strip()


def _json_text(value, default):
    if value is None:
        value = default
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_value(value, default):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


class IntelRepository:
    def __init__(self, database=None):
        self.db = database or sqlite_db

    def _ensure(self):
        self.db._ensure_connection()
        self._ensure_intel_schema_once()

    def _ensure_intel_schema_once(self) -> None:
        """每个进程只跑一次 intel 侧建表/补列。

        为什么必须有这一步：PostgreSQL 主库模式下 `create_tables()` 直接跳过所有 DDL
        （日志："跳过 SQLite DDL，使用已迁移 schema"），所以**代码里新增的表/列不会自动落到生产库**
        （本轮实测：`intel_jobs.started_at` 与 `intel_worker_heartbeats` 在 A/B 上都不存在，
        心跳与巡检因此报 UndefinedTable）。这里在首次使用 intel 仓储时补一次，
        `CREATE TABLE IF NOT EXISTS` + `_ensure_column` 都是幂等的。
        """
        if getattr(self, "_intel_schema_ready", False):
            return
        with _SCHEMA_ENSURE_LOCK:
            if getattr(self, "_intel_schema_ready", False):
                return
            try:
                from intel_schema import ensure_intel_core_tables

                with self.db.lock:
                    cursor = self.db.connection.cursor()
                    try:
                        ensure_intel_core_tables(cursor)
                        self.db.connection.commit()
                    finally:
                        cursor.close()
                self._intel_schema_ready = True
            except Exception as exc:
                # 建表失败不能阻断主流程（只读查询仍应可用），但要留下明确日志
                print("⚠️ intel schema 迁移失败（将重试）: %s" % str(exc)[:200])

    def active_industry_pack_id(self) -> str:
        self._ensure()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                try:
                    cursor.execute(
                        "SELECT setting_value FROM intel_runtime_settings "
                        "WHERE setting_key='active_industry_pack_id'"
                    )
                except sqlite3.OperationalError as exc:
                    if "no such table" not in str(exc).casefold():
                        raise
                    return config.INTEL_DEFAULT_INDUSTRY_PACK
                row = cursor.fetchone()
                return str(row[0]) if row and row[0] else config.INTEL_DEFAULT_INDUSTRY_PACK
            finally:
                cursor.close()

    def active_runtime_context(self) -> Dict:
        """Read the current activation identity used to stamp and gate jobs."""

        self._ensure()
        keys = (
            "active_industry_pack_id",
            "active_industry_pack_version_id",
            "active_industry_activation_id",
        )
        with self.db.lock:
            rows = self.db.connection.execute(
                """
                SELECT setting_key, setting_value FROM intel_runtime_settings
                WHERE setting_key IN (?, ?, ?)
                """,
                keys,
            ).fetchall()
        values = {str(row[0]): str(row[1]) for row in rows}
        version = values.get("active_industry_pack_version_id", "")
        return {
            "primary_industry_pack_id": values.get(
                "active_industry_pack_id", config.INTEL_DEFAULT_INDUSTRY_PACK
            ),
            "industry_pack_version_id": int(version) if version.isdigit() else None,
            "activation_id": values.get("active_industry_activation_id", ""),
        }

    def _stamp_active_job_context(self, payload: Optional[Dict]) -> Dict:
        result = dict(payload or {})
        context = self.active_runtime_context()
        if not context["activation_id"]:
            return result
        for key, value in context.items():
            result.setdefault(key, value)
        result.setdefault(
            "declaring_pack_id",
            result.get("industry_pack_id") or context["primary_industry_pack_id"],
        )
        return result

    def cancel_stale_activation_jobs(
        self,
        active_activation_id: str,
        *,
        transaction_cursor=None,
    ) -> int:
        """Cancel unclaimed work from every older activation atomically."""

        self._ensure()
        with self.db.lock:
            owns_transaction = transaction_cursor is None
            cursor = transaction_cursor or self.db.connection.cursor()
            try:
                if owns_transaction:
                    cursor.execute("BEGIN IMMEDIATE")
                cursor.execute(
                    """
                    UPDATE intel_jobs
                    SET status='cancelled',
                        last_error='superseded by industry-pack activation',
                        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                        completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                    WHERE status IN ('queued', 'retry_wait')
                      AND COALESCE(json_extract(payload_json, '$.activation_id'), '') != ?
                    """,
                    (str(active_activation_id),),
                )
                changed = int(cursor.rowcount)
                if owns_transaction:
                    self.db.connection.commit()
                return changed
            except Exception:
                if owns_transaction:
                    self.db.connection.rollback()
                raise
            finally:
                if owns_transaction:
                    cursor.close()

    def restore_pack_projection_for_activation(
        self,
        *,
        industry_pack_id: str,
        activation_id: str,
        target_pack_version: str,
        project_keywords: Iterable[str],
        transaction_cursor=None,
    ) -> Dict:
        """Safely restore previously aggregated cards into a new activation.

        A return to an industry creates a new activation identity. Historical
        classifications therefore remain hidden until their article evidence
        is checked against the target pack's *current* project keywords. Only
        still-matching rows are rebound; unrelated legacy rows stay auditable
        under their previous/empty activation identity. Rebinding updates only
        the activation projection and its current anchor evidence. It must not
        overwrite category/topic evidence or claim that an old classification
        was produced by the target pack version; a version mismatch deliberately
        leaves the row eligible for normal reclassification.
        """

        from project_keyword_gate import matched_project_keywords

        self._ensure()
        normalized_keywords = list(
            dict.fromkeys(
                str(value or "").strip()
                for value in project_keywords
                if str(value or "").strip()
            )
        )
        with self.db.lock:
            owns_transaction = transaction_cursor is None
            cursor = transaction_cursor or self.db.connection.cursor()
            try:
                if owns_transaction:
                    cursor.execute("BEGIN IMMEDIATE")
                rows = cursor.execute(
                    """
                    SELECT c.id, c.activation_id, c.industry_pack_version,
                           c.score_details_json,
                           a.title, a.content, a.matched_keywords
                    FROM article_intel_classifications c
                    JOIN articles a ON a.id=c.article_id
                    WHERE c.industry_pack_id=? AND a.status='active'
                    ORDER BY c.id
                    """,
                    (str(industry_pack_id),),
                ).fetchall()
                restored = 0
                left_hidden = 0
                reclassification_required = 0
                for row in rows:
                    matched = matched_project_keywords(
                        normalized_keywords,
                        row["title"],
                        row["content"],
                        row["matched_keywords"],
                    )
                    if not matched:
                        left_hidden += 1
                        continue
                    score_details = _json_value(row["score_details_json"], {})
                    if not isinstance(score_details, dict):
                        score_details = {}
                    hits = score_details.get("hits")
                    if not isinstance(hits, dict):
                        hits = {}
                    hits["anchor"] = list(matched)
                    score_details["hits"] = hits
                    cursor.execute(
                        """
                        UPDATE article_intel_classifications
                        SET activation_id=?, score_details_json=?,
                            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                        WHERE id=?
                        """,
                        (
                            str(activation_id),
                            _json_text(score_details, {}),
                            int(row["id"]),
                        ),
                    )
                    restored += 1
                    reclassification_required += int(
                        str(row["industry_pack_version"] or "")
                        != str(target_pack_version)
                    )

                financial_rows = cursor.execute(
                    """
                    SELECT m.article_id, a.title, a.content, a.matched_keywords
                    FROM financial_addon_article_matches m
                    JOIN articles a ON a.id=m.article_id
                    WHERE m.primary_industry_pack_id=? AND a.status='active'
                    -- 这些列都由 a.id（主键）唯一决定，补进 GROUP BY 后分组结果不变；
                    -- SQLite 允许 SELECT 非聚合列，PostgreSQL 会报 GroupingError。
                    GROUP BY m.article_id, a.title, a.content, a.matched_keywords
                    ORDER BY m.article_id
                    """,
                    (str(industry_pack_id),),
                ).fetchall()
                financial_visible = 0
                for row in financial_rows:
                    matched = matched_project_keywords(
                        normalized_keywords,
                        row["title"],
                        row["content"],
                        row["matched_keywords"],
                    )
                    cursor.execute(
                        """
                        INSERT INTO financial_addon_article_matches(
                            article_id, activation_id, primary_industry_pack_id,
                            matched_keywords_json, is_visible
                        ) VALUES(?, ?, ?, ?, ?)
                        ON CONFLICT(article_id, activation_id) DO UPDATE SET
                            primary_industry_pack_id=excluded.primary_industry_pack_id,
                            matched_keywords_json=excluded.matched_keywords_json,
                            is_visible=excluded.is_visible,
                            evaluated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                        """,
                        (
                            int(row["article_id"]),
                            str(activation_id),
                            str(industry_pack_id),
                            _json_text(matched, []),
                            int(bool(matched)),
                        ),
                    )
                    financial_visible += int(bool(matched))
                if owns_transaction:
                    self.db.connection.commit()
                return {
                    "industry_pack_id": str(industry_pack_id),
                    "activation_id": str(activation_id),
                    "target_pack_version": str(target_pack_version),
                    "classification_rows_evaluated": len(rows),
                    "classification_rows_restored": restored,
                    "classification_rows_left_hidden": left_hidden,
                    "classification_rows_reclassification_required": (
                        reclassification_required
                    ),
                    "financial_rows_evaluated": len(financial_rows),
                    "financial_rows_visible": financial_visible,
                }
            except Exception:
                if owns_transaction:
                    self.db.connection.rollback()
                raise
            finally:
                if owns_transaction:
                    cursor.close()

    def dashboard_window_days(self) -> int:
        self._ensure()
        with self.db.lock:
            row = self.db.connection.execute("SELECT setting_value FROM intel_runtime_settings WHERE setting_key='dashboard_window_days'").fetchone()
        try: return max(1, min(730, int(row[0]))) if row and row[0] else 90
        except (TypeError, ValueError): return 90

    def get_translation_cache(
        self, article_id: int, target_language: str, translation_scope: str,
        source_text: str, model_id: str = '',
    ) -> str:
        """Return a translation only when its exact source text and model match."""
        self._ensure()
        source_hash = hashlib.sha256(str(source_text or '').encode('utf-8')).hexdigest()
        with self.db.lock:
            row = self.db.connection.execute(
                """SELECT translated_text FROM intel_article_translation_cache
                   WHERE article_id=? AND target_language=? AND translation_scope=?
                     AND source_hash=? AND model_id=?
                   ORDER BY id DESC LIMIT 1""",
                (int(article_id), str(target_language), str(translation_scope), source_hash, str(model_id or '')),
            ).fetchone()
        return str(row['translated_text'] or '') if row else ''

    def set_translation_cache(
        self, article_id: int, target_language: str, translation_scope: str,
        source_text: str, translated_text: str, model_id: str = '',
    ) -> None:
        self._ensure()
        source_hash = hashlib.sha256(str(source_text or '').encode('utf-8')).hexdigest()
        with self.db.lock:
            self.db.connection.execute(
                """INSERT INTO intel_article_translation_cache(
                       article_id,target_language,translation_scope,source_hash,model_id,translated_text,updated_at
                   ) VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(article_id,target_language,translation_scope,source_hash,model_id)
                   DO UPDATE SET translated_text=excluded.translated_text, updated_at=excluded.updated_at""",
                (int(article_id), str(target_language), str(translation_scope), source_hash,
                 str(model_id or ''), str(translated_text or ''), utc_text()),
            )
            self.db.connection.commit()

    def dashboard_activity_statistics(self, ragflow_kb_id: str = '', industry_pack_id: str = '') -> Dict:
        """今日采集与 News-KB 解析计数（香港自然日）。

        传入 industry_pack_id 时，今日统计按该行业过滤：today_crawled /
        today_news_parsed 走 article_intel_classifications，today_google_search /
        today_google_ingested 走 intel_source_industries。切换行业后今日统计只
        反映当前行业，不再把别的行业今天的采集也算进来。
        """
        self._ensure()
        today_hk = utc_now().astimezone(timezone(timedelta(hours=8))).date().isoformat()
        pack = str(industry_pack_id or '').strip()
        art_join = "JOIN article_intel_classifications _c ON _c.article_id=a.id AND _c.industry_pack_id=?" if pack else ""
        # 搜索引擎统计按「候选的行业归属」（intel_candidate_industries）过滤。
        # 不能按信源绑定（intel_source_industries）过滤：serpapi/tavily/topic-test
        # 等搜索发现的候选 source_id 往往为 NULL，按信源 join 会全部漏掉、统计恒为 0。
        cand_join_obs = "JOIN intel_candidate_industries _ci ON _ci.candidate_id=o.candidate_id AND _ci.industry_pack_id=?" if pack else ""
        cand_join_c = "JOIN intel_candidate_industries _ci2 ON _ci2.candidate_id=c.id AND _ci2.industry_pack_id=?" if pack else ""
        pack_params: List = [pack] if pack else []
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    f"""SELECT COUNT(*) AS total FROM articles a {art_join}
                       WHERE a.status='active'
                         AND date(COALESCE(NULLIF(a.first_crawled,''), a.created_at), '+8 hours')=?""",
                    list(pack_params) + [today_hk],
                )
                today_crawled = int(cursor.fetchone()['total'] or 0)
                params: List = list(pack_params) + [today_hk]
                kb_filter = ''
                if str(ragflow_kb_id or '').strip():
                    kb_filter = ' AND d.kb_id=?'
                    params.append(str(ragflow_kb_id).strip())
                cursor.execute(
                    f"""SELECT COUNT(DISTINCT d.article_id) AS total
                       FROM article_ragflow_documents d
                       JOIN articles a ON a.id=d.article_id {art_join}
                       WHERE a.status='active' AND d.sync_status='parsed'
                         AND date(COALESCE(NULLIF(d.updated_at,''), d.created_at), '+8 hours')=?""" + kb_filter,
                    params,
                )
                today_news_parsed = int(cursor.fetchone()['total'] or 0)
                cursor.execute(
                    f"""SELECT COUNT(DISTINCT o.candidate_id) AS total
                       FROM intel_candidate_observations o {cand_join_obs}
                       WHERE o.observation_type IN ('serpapi','tavily')
                         AND date(o.observed_at, '+8 hours')=?""",
                    list(pack_params) + [today_hk],
                )
                today_google_search = int(cursor.fetchone()['total'] or 0)
                # 搜索引擎入库（N1）：口径与「今日入库 M」完全对齐——文章 first_crawled 今天
                # 且已分类到当前行业包，且来自搜索引擎候选（含反爬转 VPN OCR 兜底进入的，
                # 只要最终 article_id 落库都算）。
                _n1_art_join = "JOIN article_intel_classifications _c3 ON _c3.article_id=a.id AND _c3.industry_pack_id=?" if pack else ""
                _n1_params: List = ([pack] if pack else []) + ([pack] if pack else []) + [today_hk]
                cursor.execute(
                    f"""SELECT COUNT(DISTINCT a.id) AS total
                       FROM articles a
                       {_n1_art_join}
                       JOIN intel_candidates c ON c.article_id=a.id
                       JOIN intel_candidate_observations o ON o.candidate_id=c.id AND o.observation_type IN ('serpapi','tavily')
                       {cand_join_c}
                       WHERE a.status='active'
                         AND date(COALESCE(NULLIF(a.first_crawled,''), a.created_at), '+8 hours')=?""",
                    _n1_params,
                )
                today_google_ingested = int(cursor.fetchone()['total'] or 0)
                # 固定信源扫描入库（N2）= 今日入库总数 M - 搜索引擎入库 N1
                today_source_ingested = max(0, today_crawled - today_google_ingested)
                cursor.execute(
                    """SELECT datetime(MAX(started_at), '-8 hours') AS value
                       FROM crawl_attempts WHERE status IN ('completed', 'success')"""
                )
                last_actual_crawl_time = str(cursor.fetchone()['value'] or '')
                if not last_actual_crawl_time:
                    cursor.execute(
                        """SELECT MAX(completed_at) AS value FROM intel_scan_runs
                           WHERE status IN ('completed', 'partial')"""
                    )
                    last_actual_crawl_time = str(cursor.fetchone()['value'] or '')
                return {
                    'today_date': today_hk,
                    'today_crawled_articles': today_crawled,
                    'today_news_parsed_articles': today_news_parsed,
                    'today_google_search_articles': today_google_search,
                    'today_google_ingested_articles': today_google_ingested,
                    'today_source_ingested_articles': today_source_ingested,
                    'last_crawl_time': last_actual_crawl_time,
                    'news_kb_id': str(ragflow_kb_id or '').strip(),
                }
            finally:
                cursor.close()


    # ---------------- 行业演化趋势（第一阶段：baseline 趋势）----------------

    def aggregate_trend_keyword_daily(
        self, *, industry_pack_id: str, days_back: int = 90,
        hits_field: str = "$.hits.trend",
    ) -> List[Dict]:
        """按 (day, keyword) 聚合文章命中（score_details_json.$.hits.trend 展开）。

        用 SQLite json_each 在 SQL 层把每篇文章命中的趋势词数组展开成多行，
        再 GROUP BY (day, keyword)，一次性产出全部关键词×天的计数与独立来源数。
        返回 [{day, keyword, article_count, distinct_source_count}]。
        """
        self._ensure()
        pack = str(industry_pack_id or "").strip()
        since = (
            utc_now().astimezone(timezone(timedelta(hours=8))).date()
            - timedelta(days=int(days_back))
        ).isoformat()
        if pack:
            pack_join = (
                "JOIN article_intel_classifications _c "
                "ON _c.article_id=a.id AND _c.industry_pack_id=?"
            )
            params: List = [pack, since]
        else:
            pack_join = "JOIN article_intel_classifications _c ON _c.article_id=a.id"
            params = [since]
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT date({ARTICLE_TIME_SQL}) AS day, je.value AS keyword,
                           COUNT(*) AS article_count,
                           COUNT(DISTINCT a.domain) AS distinct_source_count
                    FROM articles a
                    {pack_join}
                    , json_each(COALESCE(
                          json_extract(_c.score_details_json, '{hits_field}'), '[]')) je
                    WHERE a.status='active'
                      AND _c.final_category IN ('trend', 'event')
                      AND date({ARTICLE_TIME_SQL}) >= ?
                    GROUP BY day, keyword
                    """,
                    params,
                )
                return [dict(row) for row in cursor.fetchall()]
            finally:
                cursor.close()

    def upsert_topic_trend_rows(
        self, *, industry_pack_id: str, dimension: str,
        activation_id: str, rows: List[Dict],
    ) -> int:
        """批量 UPSERT 趋势聚合行（count/sources/state/burst 全字段覆盖）。"""
        self._ensure()
        if not rows:
            return 0
        pack = str(industry_pack_id or "").strip()
        dim = str(dimension or "trend_keyword")
        act = str(activation_id or "")
        now = utc_text()
        payload = []
        for r in rows:
            payload.append((
                pack, act, dim, str(r["keyword"]), str(r["bucket_date"]),
                int(r.get("article_count") or 0),
                int(r.get("distinct_source_count") or 0),
                1 if r.get("is_burst") else 0,
                float(r.get("burst_score") or 0.0),
                str(r.get("state") or "EMERGING"),
                str(r.get("trend_test_method") or "count_poisson"),
                float(r.get("pettitt_p", 1.0)),
                float(r.get("pettitt_q", 1.0)),
                float(r.get("delta_BIC", -999.0)),
                r.get("change_point_index"),
                1 if r.get("taskshift_emerging") else 0,
                1 if r.get("taskshift_high_confidence") else 0,
                now, now, now,
            ))
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.executemany(
                    """
                    INSERT INTO intel_topic_trends (
                        industry_pack_id, activation_id, dimension, keyword, bucket_date,
                        article_count, distinct_source_count, is_burst, burst_score, state,
                        trend_test_method, pettitt_p, pettitt_q, delta_bic,
                        change_point_index, taskshift_emerging, taskshift_high_confidence,
                        computed_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(industry_pack_id, dimension, keyword, bucket_date) DO UPDATE SET
                        activation_id=excluded.activation_id,
                        article_count=excluded.article_count,
                        distinct_source_count=excluded.distinct_source_count,
                        is_burst=excluded.is_burst,
                        burst_score=excluded.burst_score,
                        state=excluded.state,
                        trend_test_method=excluded.trend_test_method,
                        pettitt_p=excluded.pettitt_p,
                        pettitt_q=excluded.pettitt_q,
                        delta_bic=excluded.delta_bic,
                        change_point_index=excluded.change_point_index,
                        taskshift_emerging=excluded.taskshift_emerging,
                        taskshift_high_confidence=excluded.taskshift_high_confidence,
                        computed_at=excluded.computed_at,
                        updated_at=excluded.updated_at
                    """,
                    payload,
                )
                self.db.connection.commit()
                return len(payload)
            finally:
                cursor.close()

    def list_topic_trends(
        self, *, industry_pack_id: str, dimension: str = "trend_keyword",
        days: int = 30, keywords=None, top: int = 12, window: int = 7,
        min_total: int = 2,
    ) -> List[Dict]:
        """读取趋势序列：每关键词的 points + 当前 state/burst（取最新天行）。"""
        self._ensure()
        pack = str(industry_pack_id or "").strip()
        since = (
            utc_now().astimezone(timezone(timedelta(hours=8))).date()
            - timedelta(days=int(days))
        ).isoformat()
        kw_filter = set(k.strip() for k in (keywords or []) if str(k).strip())
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT keyword, bucket_date, article_count, distinct_source_count,
                           is_burst, burst_score, state, trend_test_method,
                           pettitt_p, pettitt_q, delta_bic, change_point_index,
                           taskshift_emerging, taskshift_high_confidence
                    FROM intel_topic_trends
                    WHERE industry_pack_id=? AND dimension=? AND bucket_date>=?
                    ORDER BY keyword, bucket_date
                    """,
                    [pack, str(dimension), since],
                )
                rows = [dict(r) for r in cursor.fetchall()]
            finally:
                cursor.close()
        # 组装：keyword -> 升序 points
        by_kw: Dict[str, List[Dict]] = {}
        for r in rows:
            by_kw.setdefault(r["keyword"], []).append(r)
        series: List[Dict] = []
        win = int(window)
        for kw, pts in by_kw.items():
            if kw_filter and kw not in kw_filter:
                continue
            pts.sort(key=lambda x: x["bucket_date"])
            latest = pts[-1]
            total_count = sum(p["article_count"] for p in pts)
            recent_window = pts[-win:] if len(pts) >= win else pts
            recent_avg = round(
                sum(p["article_count"] for p in recent_window) / max(len(recent_window), 1), 4
            )
            base_pts = pts[:-win] if len(pts) > win else []
            baseline_avg = round(
                sum(p["article_count"] for p in base_pts) / max(len(base_pts), 1), 4
            ) if base_pts else 0.0
            peak = max(pts, key=lambda x: x["article_count"]) if pts else None
            peak_date = str(peak["bucket_date"]) if peak else ""
            peak_count = int(peak["article_count"]) if peak else 0
            state = str(latest["state"])
            # 生成一句话解读，降低用户读图门槛
            if state == "BURSTING":
                ratio = recent_avg / max(baseline_avg, 0.1)
                interp = f"近{win}天突然爆发，日均{recent_avg:.1f}篇（约为前期的{ratio:.1f}倍）"
            elif state == "RISING":
                interp = f"近{win}天日均{recent_avg:.1f}篇，较前期{baseline_avg:.1f}篇上升，热度在涨"
            elif state == "DECLINING":
                peak_info = (
                    f"{peak_date}前后达高峰（{peak_count}篇/天），"
                    if peak_count > recent_avg * 1.5 else ""
                )
                interp = f"{peak_info}近{win}天回落至日均{recent_avg:.1f}篇，话题正在降温"
            elif state == "MATURE":
                interp = f"近{len(pts)}天持续活跃（共{total_count}篇），热度平稳高位"
            else:  # EMERGING
                interp = (
                    f"零星出现（共{total_count}篇），数据不足以判断明确趋势"
                    if total_count > 2 else f"仅零星出现（共{total_count}篇），数据不足"
                )
            series.append({
                "keyword": kw,
                "state": state,
                # 数据不足与新生是两种语义：数据点数 < 窗口 → 数据不足；
                # 点数充足但暂不构成明确趋势 → 新生（EMERGING 兜底）。
                "insufficient_data": bool(len(pts) < win),
                "burst_score": latest["burst_score"],
                "is_burst": bool(latest["is_burst"]),
                "trend_test_method": latest["trend_test_method"],
                "pettitt_p": latest["pettitt_p"],
                "pettitt_q": latest["pettitt_q"],
                "delta_BIC": latest["delta_bic"],
                "change_point_index": latest["change_point_index"],
                "taskshift_emerging": bool(latest["taskshift_emerging"]),
                "taskshift_high_confidence": bool(latest["taskshift_high_confidence"]),
                "total": total_count,
                "recent_avg": recent_avg,
                "baseline_avg": baseline_avg,
                "peak_date": peak_date,
                "peak_count": peak_count,
                "interpretation": interp,
                "points": [
                    {
                        "date": p["bucket_date"],
                        "count": p["article_count"],
                        "sources": p["distinct_source_count"],
                    }
                    for p in pts
                ],
            })
        # 过滤低命中噪声词（过滤后为空则回退全部，避免空白）
        filtered = [s for s in series if s["total"] >= int(min_total)] or series
        # 排序：总量优先（展示政策/监管/增长等核心话题）→ 爆发分次之
        filtered.sort(key=lambda s: (s["total"], s["burst_score"]), reverse=True)
        return filtered[: int(top)] if int(top) > 0 else filtered

    def list_articles_by_trend_keyword(
        self, *, industry_pack_id: str, keyword: str,
        days: int = 30, page: int = 1, per_page: int = 10,
        dimension: str = "trend_keyword",
    ) -> Tuple[List[Dict], int]:
        """趋势关键词下钻：返回近 days 天命中该关键词的文章（分页）。

        命中判定：article_intel_classifications.score_details_json 的 $.hits.trend
        数组里含该 keyword（json_each 展开）。返回 (articles, total)。
        """
        self._ensure()
        pack = str(industry_pack_id or "").strip()
        kw = str(keyword or "").strip()
        if not kw:
            return [], 0
        since = (
            utc_now().astimezone(timezone(timedelta(hours=8))).date()
            - timedelta(days=int(days))
        ).isoformat()
        page = max(1, int(page))
        per_page = max(1, min(int(per_page), 100))
        offset = (page - 1) * per_page
        if dimension in ("bertopic", "fixed_topic"):
            return self._list_articles_by_topic(pack, kw, since, page, per_page, offset,
                                                source=dimension)
        hits_field = "$.hits.brand" if dimension == "brand" else "$.hits.trend"
        base_where = (
            "FROM article_intel_classifications _c "
            "JOIN articles a ON a.id=_c.article_id AND a.status='active' "
            "WHERE _c.industry_pack_id=? "
            "  AND _c.final_category IN ('trend', 'event') "
            f"  AND date({ARTICLE_TIME_SQL}) >= ? "
            "  AND EXISTS ("
            "      SELECT 1 FROM json_each("
            f"          COALESCE(json_extract(_c.score_details_json, '{hits_field}'), '[]')"
            "      ) WHERE value=?"
            "  )"
        )
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(f"SELECT COUNT(*) AS c {base_where}", [pack, since, kw])
                total = int(cursor.fetchone()["c"])
                cursor.execute(
                    f"""
                    SELECT a.id AS article_id, a.title, a.url, a.domain,
                           a.publish_date, _c.final_category, _c.trend_summary,
                           {ARTICLE_TIME_SQL} AS effective_time
                    {base_where}
                    ORDER BY date({ARTICLE_TIME_SQL}) DESC, a.id DESC
                    LIMIT ? OFFSET ?
                    """,
                    [pack, since, kw, per_page, offset],
                )
                return [dict(row) for row in cursor.fetchall()], total
            finally:
                cursor.close()

    def _list_articles_by_topic(self, pack, topic_name, since, page, per_page, offset,
                                source="bertopic"):
        """主题下钻：主题名 → intel_topics → intel_topic_articles → 文章。

        bertopic 只取自动主题(assignment_method='bertopic', topic_source='automatic')；
        fixed_topic 取配置的固定主题（rule_keyword 关联），两者都从 intel_topic_articles 关联下钻。
        """
        if source == "bertopic":
            topic_join = "AND ta.assignment_method='bertopic'"
            topic_filter = "AND t.topic_source='automatic'"
        else:
            topic_join = ""
            topic_filter = ""
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                base_where = (
                    "FROM intel_topics t "
                    f"JOIN intel_topic_articles ta ON ta.topic_id=t.id {topic_join} "
                    "JOIN articles a ON a.id=ta.article_id AND a.status='active' "
                    f"WHERE t.industry_pack_id=? {topic_filter} AND t.topic_name=? "
                    f"  AND date({ARTICLE_TIME_SQL}) >= ? "
                )
                cursor.execute(f"SELECT COUNT(*) AS c {base_where}", [pack, topic_name, since])
                total = int(cursor.fetchone()["c"])
                cursor.execute(
                    f"""
                    SELECT a.id AS article_id, a.title, a.url, a.domain,
                           a.publish_date, 'trend' AS final_category, '' AS trend_summary,
                           {ARTICLE_TIME_SQL} AS effective_time
                    {base_where}
                    ORDER BY date({ARTICLE_TIME_SQL}) DESC, a.id DESC
                    LIMIT ? OFFSET ?
                    """,
                    [pack, topic_name, since, per_page, offset],
                )
                return [dict(row) for row in cursor.fetchall()], total
            finally:
                cursor.close()

    # ---------------- 行业演化趋势（第一阶段：文章向量 Embedding）----------------

    def list_articles_missing_embeddings(
        self, *, model_id: str = "bge-m3", pack_id: str = "",
        limit: int = 200, max_chars: int = 4000,
    ) -> List[Dict]:
        """返回缺向量或 content_hash 失效的文章 [{article_id, content_hash, content}]。

        content 截断到 max_chars（embedding 输入有上限，且节省传输）。pack_id
        非空时只取该 pack 的分类文章。
        """
        self._ensure()
        pack = str(pack_id or "").strip()
        if pack:
            pack_join = (
                "JOIN article_intel_classifications _c "
                "ON _c.article_id=a.id AND _c.industry_pack_id=?"
            )
            params: List = [int(max_chars), pack, str(model_id), int(limit)]
        else:
            pack_join = ""
            params = [int(max_chars), str(model_id), int(limit)]
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT a.id AS article_id,
                           COALESCE(a.content_hash, '') AS content_hash,
                           substr(COALESCE(a.content, ''), 1, ?) AS content
                    FROM articles a
                    {pack_join}
                    LEFT JOIN intel_article_embeddings e
                      ON e.article_id=a.id AND e.model_id=?
                    WHERE a.status='active'
                      AND COALESCE(a.content, '') != ''
                      AND (
                          e.id IS NULL
                          OR COALESCE(e.content_hash, '') != COALESCE(a.content_hash, '')
                          OR e.status = 'error'
                      )
                    ORDER BY a.id DESC
                    LIMIT ?
                    """,
                    params,
                )
                return [dict(r) for r in cursor.fetchall()]
            finally:
                cursor.close()

    def load_pack_embeddings(
        self, *, pack_id: str, model_id: str = "bge-m3",
        days_back: int = 90, limit: int = 2000, max_chars: int = 4000,
    ) -> List[Dict]:
        """读取某 pack 已向量化的文章：正文 + 嵌入(np.float32 还原) + 按天，供 BERTopic 聚类与动态趋势。
        返回 [{article_id, content, embedding(np.ndarray), day}]，content/embedding 行序一一对应。
        """
        import numpy as np
        self._ensure()
        pack = str(pack_id or "").strip()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT a.id AS article_id,
                           substr(COALESCE(a.content, ''), 1, ?) AS content,
                           e.embedding AS embedding,
                           date({ARTICLE_TIME_SQL}) AS day
                    FROM articles a
                    JOIN article_intel_classifications _c
                      ON _c.article_id=a.id AND _c.industry_pack_id=?
                    JOIN intel_article_embeddings e
                      ON e.article_id=a.id AND e.model_id=? AND e.status='ready'
                    WHERE a.status='active'
                      AND COALESCE(a.content, '') != ''
                      AND date({ARTICLE_TIME_SQL}) >= date('now', ?)
                    ORDER BY a.id DESC
                    LIMIT ?
                    """,
                    [int(max_chars), pack, str(model_id), f"-{int(days_back)} days", int(limit)],
                )
                rows = []
                for r in cursor.fetchall():
                    d = dict(r)
                    blob = d.pop("embedding", None)
                    if not blob:
                        continue
                    d["embedding"] = np.frombuffer(blob, dtype=np.float32)
                    rows.append(d)
                return rows
            finally:
                cursor.close()

    def upsert_article_embedding(
        self, *, article_id: int, model_id: str, dim: int,
        vector_blob: bytes, content_hash: str,
        status: str = "ready", error_message: str = "",
    ) -> int:
        """写入/更新一篇文章的向量（BLOB 存 np.float32 .tobytes()）。"""
        self._ensure()
        now = utc_text()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO intel_article_embeddings (
                        article_id, model_id, embedding_dim, embedding, content_hash,
                        status, error_message, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(article_id, model_id) DO UPDATE SET
                        embedding_dim=excluded.embedding_dim,
                        embedding=excluded.embedding,
                        content_hash=excluded.content_hash,
                        status=excluded.status,
                        error_message=excluded.error_message,
                        updated_at=excluded.updated_at
                    """,
                    [
                        int(article_id), str(model_id), int(dim), vector_blob,
                        str(content_hash), str(status), str(error_message)[:500],
                        now, now,
                    ],
                )
                self.db.connection.commit()
                return int(article_id)
            finally:
                cursor.close()

    # ---------------- 事件抽取（第二阶段）----------------

    def list_articles_missing_events(
        self, *, pack_id: str = "", limit: int = 20, max_chars: int = 8000,
    ) -> List[Dict]:
        """返回待抽取事件的文章 [{article_id, content_hash, content, industry_pack_id}]。

        只取 final_category IN ('trend','event') 的有价值文章。已抽取且 content_hash
        未失效的跳过（NOT EXISTS：无"content_hash 匹配"的事件行 → 需抽取）。
        """
        self._ensure()
        pack = str(pack_id or "").strip()
        if pack:
            pack_clause = "AND _c.industry_pack_id=?"
            params: List = [int(max_chars), pack, int(limit)]
        else:
            pack_clause = ""
            params = [int(max_chars), int(limit)]
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT a.id AS article_id,
                           a.title,
                           COALESCE(a.content_hash, '') AS content_hash,
                           _c.industry_pack_id AS industry_pack_id,
                           substr(COALESCE(a.content, ''), 1, ?) AS content
                    FROM articles a
                    JOIN article_intel_classifications _c ON _c.article_id=a.id
                    WHERE a.status='active'
                      AND COALESCE(a.content, '') != ''
                      AND _c.final_category IN ('trend', 'event')
                      {pack_clause}
                      AND json_array_length(COALESCE(json_extract(_c.score_details_json, '$.hits.anchor'), '[]')) > 0
                      AND NOT EXISTS (
                          SELECT 1 FROM intel_article_events e
                          WHERE e.article_id=a.id
                            AND e.content_hash = COALESCE(a.content_hash, '')
                      )
                    ORDER BY a.id DESC
                    LIMIT ?
                    """,
                    params,
                )
                return [dict(r) for r in cursor.fetchall()]
            finally:
                cursor.close()

    def replace_article_events(
        self, *, article_id: int, content_hash: str, industry_pack_id: str,
        events: List[Dict], llm_model_id: str = "",
    ) -> int:
        """幂等写入一篇文章的事件（DELETE+INSERT）。0 事件写占位行防反复重抽。"""
        self._ensure()
        now = utc_text()
        pack = str(industry_pack_id or "")
        chash = str(content_hash or "")
        model = str(llm_model_id or "")
        # 同文章内按 (subject, action) 归一去重：防 LLM 对同一主体同一动作重复抽取
        # （如"摩根荣获"抽 5 次，object 略异致 hash 不同）；event_hash 兜底防完全重复
        seen_keys = set()
        dedup_events = []
        for e in events:
            subj = " ".join(str(e.get("subject") or "").lower().split())
            act = " ".join(str(e.get("action") or "").lower().split())
            key = subj + "|" + act
            h = e.get("event_hash") or ""
            dedup_key = key if key != "|" else (h or id(e))
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)
            dedup_events.append(e)
        rows = []
        for idx, e in enumerate(dedup_events):
            rows.append((
                int(article_id), idx, pack,
                str(e.get("subject") or "")[:120],
                str(e.get("subject_type") or "entity"),
                str(e.get("action") or "")[:160],
                str(e.get("object") or "")[:160],
                json.dumps(e.get("entities") or [], ensure_ascii=False),
                str(e.get("event_time") or "")[:20],
                str(e.get("event_type") or "other"),
                str(e.get("event_hash") or ""),
                chash, model, now, now,
            ))
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    "DELETE FROM intel_article_events WHERE article_id=?",
                    (int(article_id),),
                )
                if rows:
                    cursor.executemany(
                        """
                        INSERT INTO intel_article_events (
                            article_id, event_index, industry_pack_id, subject, subject_type, action, object,
                            entities_json, event_time, event_type, event_hash,
                            content_hash, llm_model_id, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
                else:
                    # 0 事件也标记已抽取（占位行），下次 NOT EXISTS 跳过
                    cursor.execute(
                        """
                        INSERT INTO intel_article_events (
                            article_id, event_index, industry_pack_id, subject, subject_type, action, object,
                            entities_json, event_time, event_type, event_hash,
                            content_hash, llm_model_id, created_at, updated_at
                        ) VALUES (?, 0, ?, '__no_event__', 'entity', '', '', '[]', '', 'other', '', ?, ?, ?, ?)
                        """,
                        (int(article_id), pack, chash, model, now, now),
                    )
                self.db.connection.commit()
                return len(rows)
            finally:
                cursor.close()

    def mark_article_events_error(
        self, *, article_id: int, content_hash: str, industry_pack_id: str,
        error: str, llm_model_id: str = "",
    ) -> None:
        """记录抽取失败（占位行 subject='__error__'，content_hash 标记避免立即重试）。"""
        self._ensure()
        now = utc_text()
        msg = str(error or "")[:160]
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    "DELETE FROM intel_article_events WHERE article_id=?",
                    (int(article_id),),
                )
                cursor.execute(
                    """
                    INSERT INTO intel_article_events (
                        article_id, event_index, industry_pack_id, subject, subject_type, action, object,
                        entities_json, event_time, event_type, event_hash,
                        content_hash, llm_model_id, created_at, updated_at
                    ) VALUES (?, 0, ?, '__error__', 'entity', ?, ?, '[]', '', 'other', '', ?, ?, ?, ?)
                    """,
                    (int(article_id), str(industry_pack_id or ""), msg, msg,
                     str(content_hash or ""), str(llm_model_id or ""), now, now),
                )
                self.db.connection.commit()
            finally:
                cursor.close()

    def aggregate_event_clusters(
        self, *, pack_id: str = "", days: int = 30,
        min_articles: int = 1, window: int = 7,
    ) -> List[Dict]:
        """事件聚类：GROUP BY event_hash × day → 簇（含 day 序列 + state + 代表文章）。

        返回 [{event_hash, event_type, subject, action, object, description,
              article_count, distinct_source_count, first/last_time,
              state, burst_score, is_burst, representative_article_id, points}]。
        """
        self._ensure()
        pack = str(pack_id or "").strip()
        since = (
            utc_now().astimezone(timezone(timedelta(hours=8))).date()
            - timedelta(days=int(days))
        ).isoformat()
        pack_where = "AND e.industry_pack_id=?" if pack else ""
        params: List = ([pack] if pack else []) + [since]
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT e.event_hash, e.event_type, e.subject, e.action, e.object,
                           e.article_id, a.domain, e.event_time, a.publish_date, a.first_crawled, a.created_at,
                           date({ARTICLE_TIME_SQL}) AS day
                    FROM intel_article_events e
                    JOIN articles a ON a.id=e.article_id AND a.status='active'
                    WHERE e.event_hash != ''
                      AND e.subject NOT IN ('__no_event__', '__error__')
                      {pack_where}
                      AND date({ARTICLE_TIME_SQL}) >= ?
                    ORDER BY e.event_hash, day
                    """,
                    params,
                )
                rows = [dict(r) for r in cursor.fetchall()]
            finally:
                cursor.close()
        if not rows:
            return []
        # Python 组装簇
        from trend_detect import analyze, apply_bh_correction
        from utils import get_china_time

        today = get_china_time().date()
        days_sorted = sorted(
            (today - timedelta(days=i)).isoformat() for i in range(int(days))
        )
        clusters: Dict[str, Dict] = {}
        day_article_ids: Dict[str, set] = {}
        order: List[str] = []
        for r in rows:
            h = r["event_hash"]
            if h not in clusters:
                clusters[h] = {
                    "event_hash": h, "event_type": r["event_type"],
                    "subject": r["subject"], "action": r["action"], "object": r["object"],
                    "article_ids": [], "domains": set(), "days": {}, "first_time": "", "last_time": "",
                }
                order.append(h)
            c = clusters[h]
            aid = r["article_id"]
            if aid in c["article_ids"]:
                continue  # 同簇同文章只计一次（去历史重复抽取行）
            c["article_ids"].append(aid)
            if r.get("domain"):
                c["domains"].add(r["domain"])
            day = r.get("day") or ""
            if day:
                day_article_ids.setdefault(day, set()).add(aid)
                c["days"][day] = c["days"].get(day, 0) + 1
            et = str(
                r.get("event_time")
                or r.get("publish_date")
                or (str(r.get("first_crawled") or "")[:10])
                or (str(r.get("created_at") or "")[:10])
                or ""
            )
            if et:
                if not c["first_time"] or et < c["first_time"]:
                    c["first_time"] = et
                if not c["last_time"] or et > c["last_time"]:
                    c["last_time"] = et
        result: List[Dict] = []
        for h in order:
            c = clusters[h]
            if len(c["article_ids"]) < int(min_articles):
                continue
            series = [c["days"].get(d, 0) for d in days_sorted]
            exposures = [len(day_article_ids.get(d, set())) for d in days_sorted]
            analysis = analyze(series, int(window), exposures=exposures)
            subj, act, obj = c["subject"], c["action"], c["object"]
            desc = " ".join(x for x in (subj, act, obj) if x)
            result.append({
                "event_hash": h,
                "event_type": c["event_type"],
                "subject": subj, "action": act, "object": obj,
                "description": desc[:200],
                "article_count": len(c["article_ids"]),
                "distinct_source_count": len(c["domains"]),
                "first_time": c["first_time"], "last_time": c["last_time"],
                "state": analysis["state"],
                "burst_score": analysis["burst_score"],
                "is_burst": analysis["is_burst"],
                "trend_test_method": analysis["trend_test_method"],
                "pettitt_p": analysis["pettitt_p"],
                "pettitt_q": analysis["pettitt_q"],
                "delta_BIC": analysis["delta_BIC"],
                "change_point_index": analysis["change_point_index"],
                "pre_mean": analysis["pre_mean"],
                "post_mean": analysis["post_mean"],
                "taskshift_emerging": analysis["taskshift_emerging"],
                "taskshift_high_confidence": analysis["taskshift_high_confidence"],
                "representative_article_id": max(c["article_ids"]),
                "points": [{"date": d, "count": c["days"].get(d, 0)} for d in days_sorted],
            })
        apply_bh_correction(result)
        result.sort(key=lambda x: (x["article_count"], x["burst_score"]), reverse=True)
        return result

    def list_event_subjects(self, *, pack_id: str = "") -> List[Dict]:
        """返回该 pack 的所有 subject + 频次（供主体归并）。"""
        self._ensure()
        pack = str(pack_id or "").strip()
        pack_where = "AND e.industry_pack_id=?" if pack else ""
        params: List = [pack] if pack else []
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT e.subject AS subject, COUNT(*) AS freq
                    FROM intel_article_events e
                    JOIN articles a ON a.id=e.article_id AND a.status='active'
                    WHERE e.subject NOT IN ('__no_event__', '__error__') AND e.subject != ''
                    {pack_where}
                    GROUP BY e.subject
                    """,
                    params,
                )
                return [dict(r) for r in cursor.fetchall()]
            finally:
                cursor.close()

    def save_subject_canonical(self, *, industry_pack_id: str, mappings) -> int:
        """UPSERT 主体规范映射。mappings=[(subject_text, canonical_name, subject_key), ...]"""
        self._ensure()
        if not mappings:
            return 0
        pack = str(industry_pack_id or "")
        now = utc_text()
        payload = [(pack, str(s), str(c), str(k), now, now) for s, c, k in mappings]
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.executemany(
                    """
                    INSERT INTO intel_subject_canonical (
                        industry_pack_id, subject_text, canonical_name, subject_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(industry_pack_id, subject_text) DO UPDATE SET
                        canonical_name=excluded.canonical_name,
                        subject_key=excluded.subject_key,
                        updated_at=excluded.updated_at
                    """,
                    payload,
                )
                self.db.connection.commit()
                return len(payload)
            finally:
                cursor.close()

    def aggregate_subject_clusters(
        self, *, pack_id: str = "", days: int = 30,
        min_articles: int = 1, window: int = 7,
    ) -> List[Dict]:
        """主体级聚类（T4）：按归一化 subject 把同主体事件聚合，算主体趋势(state) + 该主体的事件列表。

        返回 [{subject, article_count, event_count, distinct_source_count,
              first/last_time, state, burst_score, is_burst, events[], points[]}]。
        subject 归一化（去空格/括号/标点/lower）作 key，显示用原始 subject。
        """
        import re
        self._ensure()
        pack = str(pack_id or "").strip()
        since = (
            utc_now().astimezone(timezone(timedelta(hours=8))).date()
            - timedelta(days=int(days))
        ).isoformat()
        pack_where = "AND e.industry_pack_id=?" if pack else ""
        params: List = ([pack] if pack else []) + [since]
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT e.subject, e.subject_type, e.event_hash, e.action, e.object, e.event_type,
                           e.article_id, a.domain, e.event_time, a.publish_date, a.first_crawled, a.created_at,
                           e.entities_json,
                           date({ARTICLE_TIME_SQL}) AS day
                    FROM intel_article_events e
                    JOIN articles a ON a.id=e.article_id AND a.status='active'
                    WHERE e.subject NOT IN ('__no_event__', '__error__')
                      AND e.subject != ''
                      {pack_where}
                      AND date({ARTICLE_TIME_SQL}) >= ?
                    ORDER BY e.subject, day
                    """,
                    params,
                )
                rows = [dict(r) for r in cursor.fetchall()]
            finally:
                cursor.close()
        if not rows:
            return []

        def norm_subj(s):
            s = str(s or "").lower()
            s = re.sub(r"\s+", "", s)
            s = re.sub(r"[（(].*?[)）]", "", s)
            s = re.sub(r"[^一-鿿a-z0-9]", "", s)
            return s

        from trend_detect import analyze, apply_bh_correction
        from utils import get_china_time

        today = get_china_time().date()
        days_sorted = sorted(
            (today - timedelta(days=i)).isoformat() for i in range(int(days))
        )
        day_article_ids: Dict[str, set] = {}
        for row in rows:
            day = row.get("day") or ""
            if day:
                day_article_ids.setdefault(day, set()).add(row["article_id"])
        # 主体规范映射（T5）：subject → canonical_name（entity 用）
        canon_map: Dict[str, str] = {}
        with self.db.lock:
            cur2 = self.db.connection.cursor()
            try:
                cur2.execute(
                    "SELECT subject_text, canonical_name FROM intel_subject_canonical WHERE industry_pack_id=?",
                    (pack,),
                )
                canon_map = {str(row["subject_text"]): str(row["canonical_name"]) for row in cur2.fetchall()}
            finally:
                cur2.close()
        # fixed_topics（topic 二次聚类维度，复用行业包配置，非硬编码）
        fixed_topics: List[Dict] = []
        try:
            from industry_pack_runtime import active_industry_composition_service
            fixed_topics = (active_industry_composition_service.snapshot()
                            .get("primary_pack", {}).get("fixed_topics") or [])
        except Exception:
            fixed_topics = []

        def match_fixed_topic(r):
            """topic 事件按 fixed_topics keywords 匹配 → (key, name)；未命中 → other/其它。"""
            try:
                ents = json.loads(r.get("entities_json") or "[]")
            except Exception:
                ents = []
            text = " ".join([str(r.get("subject") or ""), str(r.get("action") or ""),
                             str(r.get("object") or "")] + [str(x) for x in ents])
            best, best_score = None, 0
            for ft in fixed_topics:
                score = sum(1 for kw in (ft.get("keywords") or []) if kw and str(kw) in text)
                if score > best_score:
                    best, best_score = ft, score
            if best:
                return str(best.get("key") or "other"), str(best.get("name") or "其它")
            return "other", "其它"

        subjects: Dict[str, Dict] = {}
        order: List[str] = []
        for r in rows:
            stype = str(r.get("subject_type") or "entity")
            if stype == "topic":
                # topic 类型：按 fixed_topics 二次聚类（只在无明确主体时触发）
                topic_key, topic_name = match_fixed_topic(r)
                nk = "topic:" + topic_key
                disp = topic_name
            else:
                # entity 类型：保持当前 canonical 分组（不二次聚类）
                canon = canon_map.get(r["subject"])
                disp = canon or str(r["subject"])[:120]
                nk = "entity:" + (norm_subj(canon) if canon else norm_subj(r["subject"]))
                topic_key, topic_name = "", ""
            if not nk or nk in ("entity:", "topic:"):
                continue
            if nk not in subjects:
                subjects[nk] = {
                    "display": disp, "subject_type": stype,
                    "topic_key": topic_key, "topic_name": topic_name,
                    "article_ids": [], "domains": set(), "days": {},
                    "events": {}, "article_event_seen": set(), "day_article_seen": set(),
                    "first": "", "last": "",
                }
                order.append(nk)
            c = subjects[nk]
            aid = r["article_id"]
            if aid not in c["article_ids"]:
                c["article_ids"].append(aid)
            if r.get("domain"):
                c["domains"].add(r["domain"])
            day = r.get("day") or ""
            day_marker = (day, aid)
            if day and day_marker not in c["day_article_seen"]:
                c["day_article_seen"].add(day_marker)
                c["days"][day] = c["days"].get(day, 0) + 1
            # 按文章去重：同一篇文章只取首个事件作为节点，避免一文被拆成多个事件节点重复展示
            eh = r["event_hash"]
            if aid not in c["article_event_seen"]:
                c["article_event_seen"].add(aid)
                if eh and eh not in c["events"]:
                    desc = " ".join(x for x in (r["subject"], r["action"], r["object"]) if x)[:200]
                    c["events"][eh] = {
                        "event_hash": eh, "description": desc,
                        "event_type": r["event_type"], "article_count": 0, "last_time": "",
                    }
                if eh and eh in c["events"]:
                    c["events"][eh]["article_count"] += 1
            et = str(
                r.get("event_time")
                or r.get("publish_date")
                or (str(r.get("first_crawled") or "")[:10])
                or (str(r.get("created_at") or "")[:10])
                or ""
            )
            if et:
                if eh and eh in c["events"] and et > c["events"][eh].get("last_time", ""):
                    c["events"][eh]["last_time"] = et
                if not c["first"] or et < c["first"]:
                    c["first"] = et
                if not c["last"] or et > c["last"]:
                    c["last"] = et
        result: List[Dict] = []
        for nk in order:
            c = subjects[nk]
            if len(c["article_ids"]) < int(min_articles):
                continue
            series = [c["days"].get(d, 0) for d in days_sorted]
            exposures = [len(day_article_ids.get(d, set())) for d in days_sorted]
            analysis = analyze(series, int(window), exposures=exposures)
            result.append({
                "subject": c["display"], "subject_key": nk,
                "subject_type": c["subject_type"],
                "topic_key": c["topic_key"], "topic_name": c["topic_name"],
                "article_count": len(c["article_ids"]),
                "event_count": len(c["events"]),
                "distinct_source_count": len(c["domains"]),
                "first_time": c["first"], "last_time": c["last"],
                "state": analysis["state"],
                "burst_score": analysis["burst_score"],
                "is_burst": analysis["is_burst"],
                "trend_test_method": analysis["trend_test_method"],
                "pettitt_p": analysis["pettitt_p"],
                "pettitt_q": analysis["pettitt_q"],
                "delta_BIC": analysis["delta_BIC"],
                "change_point_index": analysis["change_point_index"],
                "pre_mean": analysis["pre_mean"],
                "post_mean": analysis["post_mean"],
                "taskshift_emerging": analysis["taskshift_emerging"],
                "taskshift_high_confidence": analysis["taskshift_high_confidence"],
                "events": sorted(c["events"].values(), key=lambda e: e["article_count"], reverse=True)[:10],
                "points": [{"date": d, "count": c["days"].get(d, 0)} for d in days_sorted],
            })
        apply_bh_correction(result)
        result.sort(key=lambda x: (x["article_count"], x["burst_score"]), reverse=True)
        return result

    def list_articles_by_event_hash(
        self, *, industry_pack_id: str, event_hash: str,
        days: int = 30, page: int = 1, per_page: int = 10,
    ) -> Tuple[List[Dict], int]:
        """事件簇下钻：返回该 event_hash 的文章（分页）。"""
        self._ensure()
        pack = str(industry_pack_id or "").strip()
        eh = str(event_hash or "").strip()
        if not eh:
            return [], 0
        since = (
            utc_now().astimezone(timezone(timedelta(hours=8))).date()
            - timedelta(days=int(days))
        ).isoformat()
        page = max(1, int(page))
        per_page = max(1, min(int(per_page), 100))
        offset = (page - 1) * per_page
        base_where = (
            "FROM intel_article_events e "
            "JOIN articles a ON a.id=e.article_id AND a.status='active' "
            "WHERE e.event_hash=? "
            f"  AND date({ARTICLE_TIME_SQL}) >= ? "
            "  AND e.subject NOT IN ('__no_event__', '__error__')"
        )
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(f"SELECT COUNT(DISTINCT a.id) AS c {base_where}", [eh, since])
                total = int(cursor.fetchone()["c"])
                cursor.execute(
                    f"""
                    SELECT DISTINCT a.id AS article_id, a.title, a.url, a.domain,
                           a.publish_date, substr(COALESCE(a.content, ''), 1, 360) AS content_preview,
                           {ARTICLE_TIME_SQL} AS effective_time
                    {base_where}
                    ORDER BY date({ARTICLE_TIME_SQL}) DESC, a.id DESC
                    LIMIT ? OFFSET ?
                    """,
                    [eh, since, per_page, offset],
                )
                articles = [dict(row) for row in cursor.fetchall()]
                return articles, total
            finally:
                cursor.close()

    def articles_by_ids(self, article_ids) -> Dict[int, Dict]:
        """批量取文章基本字段 {id: {id,title,url,domain,publish_date}}。"""
        self._ensure()
        ids = [int(i) for i in (article_ids or []) if i]
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    f"SELECT id, title, url, domain, publish_date FROM articles WHERE id IN ({placeholders})",
                    ids,
                )
                return {int(r["id"]): dict(r) for r in cursor.fetchall()}
            finally:
                cursor.close()

    def list_articles_by_stat_dimension(
        self,
        dimension: str,
        *,
        page: int = 1,
        per_page: int = 20,
        industry_pack_id: str = DEFAULT_INDUSTRY_PACK_ID,
        news_kb_id: str = "",
    ) -> Tuple[List[Dict], int]:
        """按首页统计卡片的维度返回文章列表，口径与各统计数字一致（含行业过滤）。

        前 3 个维度（ingested/valid_730d/classified）复用 list_classified_articles；
        后 4 个「今日」维度为时间维度，SQL 与 dashboard_activity_statistics 完全同口径
        （按行业过滤），保证统计数字 = 卡片列表 total。
        """
        self._ensure()
        page = coerce_int(page, 1, 1)
        per_page = coerce_int(per_page, 20, 1, 100)
        dimension = str(dimension or "").strip().lower()
        pack = str(industry_pack_id or '').strip()

        # 当日口径（从0点至今），与首页统计一致；统计卡片点击看当日文章
        classified_variants = {
            "ingested": {"time_range": "today"},
            "valid_730d": {"time_range": "today"},
            "classified": {"time_range": "today"},
        }
        if dimension in classified_variants:
            articles, total, _ = self.list_classified_articles(
                industry_pack_id=industry_pack_id, page=page, per_page=per_page,
                **classified_variants[dimension],
            )
            for item in articles:
                item["has_detail"] = True
            return articles, total

        today_hk = utc_now().astimezone(timezone(timedelta(hours=8))).date().isoformat()
        offset = (page - 1) * per_page
        art_join = "JOIN article_intel_classifications _c ON _c.article_id=a.id AND _c.industry_pack_id=?" if pack else ""
        obs_join = "JOIN intel_source_industries _si ON _si.source_id=o.source_id AND _si.industry_pack_id=?" if pack else ""
        pack_params: List = [pack] if pack else []
        list_sql = count_sql = ""
        params: List = []
        candidate_mode = False

        if dimension == "today_crawled":
            base = f"FROM articles a {art_join} WHERE a.status='active' AND substr(COALESCE(NULLIF(a.first_crawled,''), a.created_at),1,10)=?"
            count_sql = f"SELECT COUNT(*) AS total {base}"
            list_sql = (f"SELECT a.id AS article_id, a.url, a.title, a.domain, a.publish_date, a.first_crawled, a.content_length, "
                        f"substr(COALESCE(a.content,''),1,360) AS content_preview, "
                        f"COALESCE(NULLIF(a.matched_keywords,''),'') AS matched_keywords {base} "
                        f"ORDER BY datetime(COALESCE(NULLIF(a.first_crawled,''), a.created_at)) DESC, a.id DESC LIMIT ? OFFSET ?")
            params = list(pack_params) + [today_hk]
        elif dimension == "today_news_parsed":
            kb_filter = " AND d.kb_id=?" if str(news_kb_id or "").strip() else ""
            base = (f"FROM article_ragflow_documents d JOIN articles a ON a.id=d.article_id {art_join} "
                    f"WHERE a.status='active' AND d.sync_status='parsed' "
                    f"AND substr(COALESCE(NULLIF(d.updated_at,''), d.created_at),1,10)=?" + kb_filter)
            count_sql = f"SELECT COUNT(DISTINCT d.article_id) AS total {base}"
            list_sql = (f"SELECT a.id AS article_id, a.url, a.title, a.domain, a.publish_date, a.first_crawled, a.content_length, "
                        f"substr(COALESCE(a.content,''),1,360) AS content_preview, "
                        f"COALESCE(NULLIF(a.matched_keywords,''),'') AS matched_keywords {base} "
                        f"GROUP BY a.id ORDER BY datetime(MAX(COALESCE(NULLIF(d.updated_at,''), d.created_at))) DESC, a.id DESC LIMIT ? OFFSET ?")
            params = list(pack_params) + [today_hk] + ([str(news_kb_id).strip()] if kb_filter else [])
        elif dimension == "today_google_ingested":
            base = (f"FROM intel_candidates c JOIN articles a ON a.id=c.article_id AND a.status='active' "
                    f"JOIN intel_candidate_observations o ON o.candidate_id=c.id AND o.observation_type='serpapi' {obs_join} "
                    f"WHERE c.article_id IS NOT NULL AND date(c.first_seen_at, '+8 hours')=?")
            count_sql = f"SELECT COUNT(DISTINCT c.article_id) AS total {base}"
            list_sql = (f"SELECT a.id AS article_id, a.url, a.title, a.domain, a.publish_date, a.first_crawled, a.content_length, "
                        f"substr(COALESCE(a.content,''),1,360) AS content_preview, "
                        f"COALESCE(NULLIF(a.matched_keywords,''),'') AS matched_keywords {base} "
                        f"GROUP BY a.id ORDER BY datetime(c.first_seen_at) DESC, a.id DESC LIMIT ? OFFSET ?")
            params = list(pack_params) + [today_hk]
        elif dimension == "today_google_search":
            candidate_mode = True
            count_sql = (f"SELECT COUNT(DISTINCT o.candidate_id) AS total FROM intel_candidate_observations o {obs_join} "
                         f"WHERE o.observation_type='serpapi' AND date(o.observed_at, '+8 hours')=?")
            list_sql = (f"SELECT c.id AS candidate_id, c.original_url AS url, c.title, c.article_id, c.status AS candidate_status, "
                        f"a.domain, a.publish_date, a.first_crawled, a.content_length, substr(COALESCE(a.content,''),1,360) AS content_preview "
                        f"FROM intel_candidate_observations o {obs_join} "
                        f"JOIN intel_candidates c ON c.id=o.candidate_id "
                        f"LEFT JOIN articles a ON a.id=c.article_id AND a.status='active' "
                        f"WHERE o.observation_type='serpapi' AND date(o.observed_at, '+8 hours')=? "
                        f"GROUP BY c.id ORDER BY datetime(o.observed_at) DESC, c.id DESC LIMIT ? OFFSET ?")
            params = list(pack_params) + [today_hk]
        else:
            raise ValueError(f"未知的统计维度：{dimension}")

        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(count_sql, params)
                total = int(cursor.fetchone()["total"] or 0)
                cursor.execute(list_sql, params + [per_page, offset])
                rows = [dict(row) for row in cursor.fetchall()]
            finally:
                cursor.close()

        items: List[Dict] = []
        for row in rows:
            if candidate_mode:
                article_id = row.get("article_id")
                items.append({
                    "article_id": article_id, "has_detail": bool(article_id),
                    "url": row.get("url") or "", "title": row.get("title") or row.get("url") or "（待聚合的搜索候选）",
                    "domain": row.get("domain") or "", "publish_date": row.get("first_crawled") or row.get("publish_date") or "",
                    "content_length": int(row.get("content_length") or 0),
                    "content_preview": (row.get("content_preview") or "").strip() or ("待聚合：该搜索候选尚未入库为文章" if not article_id else ""),
                    "candidate_status": row.get("candidate_status") or "",
                })
            else:
                item = dict(row); item["has_detail"] = True
                items.append(item)
        return items, total

    def set_dashboard_window_days(self, days: int) -> int:
        value = coerce_int(days, None, 1, 730)
        if value is None: raise ValueError('首页资讯时间窗口必须为 1 至 730 天')
        self._ensure()
        with self.db.lock:
            self.db.connection.execute("INSERT INTO intel_runtime_settings(setting_key,setting_value,updated_at) VALUES ('dashboard_window_days',?,datetime('now')) ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value,updated_at=excluded.updated_at", (str(value),))
            self.db.connection.commit()
        return value

    def switch_industry_pack(self, pack_id: str, description: str = '') -> Dict:
        """Switch the runtime default without mutating shared article state.

        The active IDs are retained in the legacy backup table for audit and
        rollback compatibility.  Industry visibility is determined by each
        pack's classification/association rows, never by globally hiding data.
        """
        self._ensure()
        previous = self.active_industry_pack_id()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute("SELECT id FROM articles WHERE status='active' ORDER BY id")
                ids = [int(row[0]) for row in cursor.fetchall()]
                cursor.execute("INSERT INTO intel_pack_backups(industry_pack_id, article_ids_json, description) VALUES (?, ?, ?)", (previous, json.dumps(ids), str(description or '切换行业包前自动备份')))
                backup_id = int(cursor.lastrowid)
                cursor.execute("INSERT INTO intel_runtime_settings(setting_key, setting_value, updated_at) VALUES ('active_industry_pack_id', ?, datetime('now')) ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value, updated_at=excluded.updated_at", (pack_id,))
                self.db.connection.commit()
                return {
                    'previous_pack_id': previous,
                    'active_pack_id': pack_id,
                    'backup_id': backup_id,
                    'archived_articles': 0,
                    'preserved_articles': len(ids),
                    'article_status_unchanged': True,
                }
            finally:
                cursor.close()

    def list_pack_backups(self) -> List[Dict]:
        self._ensure()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute("SELECT id, industry_pack_id, description, created_at, json_array_length(article_ids_json) AS article_count FROM intel_pack_backups ORDER BY id DESC")
                return [dict(row) for row in cursor.fetchall()]
            finally:
                cursor.close()

    def restore_pack_backup(self, backup_id: int) -> Dict:
        self._ensure()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute("SELECT industry_pack_id, article_ids_json FROM intel_pack_backups WHERE id=?", (coerce_int(backup_id, 0, 1),))
                row = cursor.fetchone()
                if not row:
                    raise ValueError('行业包备份不存在')
                ids = [int(value) for value in json.loads(row['article_ids_json'] or '[]')]
                cursor.execute("INSERT INTO intel_runtime_settings(setting_key, setting_value, updated_at) VALUES ('active_industry_pack_id', ?, datetime('now')) ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value, updated_at=excluded.updated_at", (row['industry_pack_id'],))
                self.db.connection.commit()
                return {
                    'active_pack_id': row['industry_pack_id'],
                    'restored_articles': 0,
                    'preserved_articles': len(ids),
                    'article_status_unchanged': True,
                }
            finally:
                cursor.close()

    def enqueue_job(
        self,
        job_type: str,
        dedupe_key: str,
        payload: Optional[Dict] = None,
        *,
        priority: int = 0,
        max_attempts: Optional[int] = None,
        request_id: str = "",
        created_by: str = "",
    ) -> Tuple[int, bool]:
        self._ensure()
        payload = self._stamp_active_job_context(payload)
        max_attempts = coerce_int(
            max_attempts,
            config.INTEL_JOB_MAX_RETRIES + 1,
            1,
            20,
        )
        now_text = utc_text()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO intel_jobs (
                        job_type, dedupe_key, payload_json, status, priority,
                        max_attempts, request_id, created_by, created_at, updated_at
                    ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(dedupe_key) DO NOTHING
                    """,
                    (
                        str(job_type or "").strip(),
                        str(dedupe_key or "").strip(),
                        _json_text(payload or {}, {}),
                        coerce_int(priority, 0, -100, 100),
                        max_attempts,
                        str(request_id or ""),
                        str(created_by or ""),
                        now_text,
                        now_text,
                    ),
                )
                inserted = bool(cursor.rowcount)
                cursor.execute(
                    """
                    SELECT id FROM intel_jobs
                    WHERE dedupe_key = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (str(dedupe_key or "").strip(),),
                )
                row = cursor.fetchone()
                job_id = int(row["id"]) if row else int(cursor.lastrowid or 0)
                self.db.connection.commit()
                return job_id, inserted
            finally:
                cursor.close()

    def enqueue_job_once(
        self,
        job_type: str,
        dedupe_key: str,
        payload: Optional[Dict] = None,
        *,
        priority: int = 0,
        max_attempts: Optional[int] = None,
        request_id: str = "",
        created_by: str = "",
    ) -> Tuple[int, bool]:
        """Atomically create a schedule window once, including terminal rows."""

        self._ensure()
        payload = self._stamp_active_job_context(payload)
        normalized_key = str(dedupe_key or "").strip()
        if not normalized_key:
            raise ValueError("dedupe_key is required")
        max_attempts = coerce_int(
            max_attempts,
            config.INTEL_JOB_MAX_RETRIES + 1,
            1,
            20,
        )
        now_text = utc_text()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                cursor.execute(
                    "SELECT id FROM intel_jobs WHERE dedupe_key=? ORDER BY id DESC LIMIT 1",
                    (normalized_key,),
                )
                row = cursor.fetchone()
                if row:
                    self.db.connection.commit()
                    return int(row["id"]), False
                cursor.execute(
                    """
                    INSERT INTO intel_jobs(
                        job_type, dedupe_key, payload_json, status, priority,
                        max_attempts, request_id, created_by, created_at, updated_at
                    ) VALUES(?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(dedupe_key) DO NOTHING
                    """,
                    (
                        str(job_type or "").strip(),
                        normalized_key,
                        _json_text(payload or {}, {}),
                        coerce_int(priority, 0, -100, 100),
                        max_attempts,
                        str(request_id or ""),
                        str(created_by or ""),
                        now_text,
                        now_text,
                    ),
                )
                inserted = bool(cursor.rowcount)
                cursor.execute(
                    "SELECT id FROM intel_jobs WHERE dedupe_key=? ORDER BY id DESC LIMIT 1",
                    (normalized_key,),
                )
                row = cursor.fetchone()
                job_id = int(row["id"]) if row else int(cursor.lastrowid or 0)
                self.db.connection.commit()
                return job_id, inserted
            except Exception:
                self.db.connection.rollback()
                raise
            finally:
                cursor.close()

    def claim_jobs(
        self,
        worker_id: str,
        *,
        job_types: Optional[Iterable[str]] = None,
        limit: Optional[int] = None,
        lease_seconds: Optional[int] = None,
        starved_before: Optional[str] = None,
    ) -> List[Dict]:
        """领取待执行作业。

        starved_before：传入时间戳（UTC 文本）时只领取 created_at <= 该时刻的「饥饿作业」，
        并按 FIFO（最早入队优先）排序。这是防饿死的保留名额通道——单靠「优先级+等待积分」
        无法避免低优先级类型被持续到货的高优先级类型永久压住（实测最久 32 小时未被领取）。
        """
        self._ensure()
        limit = coerce_int(limit, config.INTEL_WORKER_BATCH_SIZE, 1, 500)
        lease_seconds = coerce_int(
            lease_seconds,
            config.INTEL_JOB_LEASE_SECONDS,
            30,
            3600,
        )
        now = utc_now()
        now_text = utc_text(now)
        lease_text = utc_text(now + timedelta(seconds=lease_seconds))
        types = [str(item).strip() for item in (job_types or []) if str(item).strip()]
        type_sql = ""
        params: List = [now_text, now_text]
        if types:
            placeholders = ",".join("?" for _ in types)
            type_sql = f" AND job_type IN ({placeholders})"
            params.extend(types)
        # 饥饿保留通道：只取最早入队的那批，按 FIFO 领取，保证低优先级类型能推进
        starve_sql = ""
        starved_before = str(starved_before).strip() if starved_before else ""
        if starved_before:
            starve_sql = " AND created_at <= ?"
        params.append(limit)

        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                cursor.execute(
                    f"""
                    UPDATE intel_jobs
                    SET status = 'retry_wait',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        next_retry_at = ?,
                        last_error = CASE
                            WHEN last_error = '' THEN 'worker lease expired'
                            ELSE last_error
                        END,
                        updated_at = ?
                    WHERE status = 'running'
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at <= ?
                    """,
                    (now_text, now_text, now_text),
                )
                # 饥饿通道用 FIFO（先来先服务），普通通道用「优先级 + 等待积分」
                if starved_before:
                    select_params = [now_text]
                    if types:
                        select_params.extend(types)
                    select_params.extend([starved_before, limit])
                    order_sql = "created_at ASC, id ASC"
                else:
                    select_params = [now_text]
                    if types:
                        select_params.extend(types)
                    select_params.extend(
                        [
                            now_text,                               # next_retry_at 比较
                            int(config.INTEL_JOB_PRIORITY_AGING_SECONDS),
                            float(config.INTEL_JOB_PRIORITY_AGING_CAP),
                            float(config.INTEL_JOB_PRIORITY_AGING_CAP),
                            now_text,
                            int(config.INTEL_JOB_PRIORITY_AGING_SECONDS),
                            limit,
                        ]
                    )
                    order_sql = """
                        (
                            priority + CAST(
                                CASE
                                    WHEN MAX(
                                        0.0,
                                        (julianday(?) - julianday(created_at))
                                        * 86400.0 / ?
                                    ) > ?
                                    THEN ?
                                    ELSE MAX(
                                        0.0,
                                        (julianday(?) - julianday(created_at))
                                        * 86400.0 / ?
                                    )
                                END AS INTEGER
                            )
                        ) DESC, created_at ASC, id ASC"""
                cursor.execute(
                    f"""
                    SELECT *
                    FROM intel_jobs
                    WHERE status IN ('queued', 'retry_wait')
                      AND (next_retry_at IS NULL OR next_retry_at <= ?)
                      {type_sql}
                      {starve_sql}
                    ORDER BY {order_sql}
                    LIMIT ?
                    """,
                    select_params,
                )
                rows = [dict(row) for row in cursor.fetchall()]
                claimed = []
                for row in rows:
                    cursor.execute(
                        """
                        UPDATE intel_jobs
                        SET status = 'running',
                            attempt_count = attempt_count + 1,
                            lease_owner = ?,
                            lease_expires_at = ?,
                            started_at = ?,
                            updated_at = ?
                        WHERE id = ?
                          AND status IN ('queued', 'retry_wait')
                        """,
                        (worker_id, lease_text, now_text, now_text, row["id"]),
                    )
                    if cursor.rowcount:
                        row["status"] = "running"
                        row["attempt_count"] = int(row.get("attempt_count") or 0) + 1
                        row["lease_owner"] = worker_id
                        row["lease_expires_at"] = lease_text
                        row["started_at"] = now_text
                        row["payload"] = _json_value(row.pop("payload_json", "{}"), {})
                        claimed.append(row)
                self.db.connection.commit()
                return claimed
            except Exception:
                self.db.connection.rollback()
                raise
            finally:
                cursor.close()

    def renew_job_lease(
        self,
        job_id: int,
        lease_owner: str,
        *,
        lease_seconds: Optional[int] = None,
    ) -> str:
        """Renew one running job without allowing a stale worker to steal it."""

        self._ensure()
        lease_seconds = coerce_int(
            lease_seconds,
            config.INTEL_JOB_LEASE_SECONDS,
            30,
            3600,
        )
        now = utc_now()
        now_text = utc_text(now)
        lease_text = utc_text(now + timedelta(seconds=lease_seconds))
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    """
                    UPDATE intel_jobs
                    SET lease_expires_at=?, updated_at=?
                    WHERE id=? AND status='running' AND lease_owner=?
                    """,
                    (
                        lease_text,
                        now_text,
                        coerce_int(job_id, 0),
                        str(lease_owner or ""),
                    ),
                )
                if cursor.rowcount:
                    self.db.connection.commit()
                    return "renewed"
                cursor.execute(
                    "SELECT status, lease_owner FROM intel_jobs WHERE id=?",
                    (coerce_int(job_id, 0),),
                )
                row = cursor.fetchone()
                self.db.connection.commit()
                if not row:
                    return "missing"
                if row["status"] == "cancelled":
                    return "cancelled"
                return "lease_lost"
            finally:
                cursor.close()

    def cancel_job(self, job_id: int, *, reason: str = "cancelled") -> bool:
        """Cancel queued/retrying/running work; handlers observe it via heartbeat."""

        self._ensure()
        now_text = utc_text()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    """
                    UPDATE intel_jobs
                    SET status='cancelled', next_retry_at=NULL,
                        lease_owner=NULL, lease_expires_at=NULL,
                        last_error=?, completed_at=?, updated_at=?
                    WHERE id=? AND status IN ('queued', 'running', 'retry_wait')
                    """,
                    (
                        str(reason or "cancelled")[:2000],
                        now_text,
                        now_text,
                        coerce_int(job_id, 0),
                    ),
                )
                self.db.connection.commit()
                return cursor.rowcount > 0
            finally:
                cursor.close()

    def complete_job(
        self,
        job_id: int,
        result: Optional[Dict] = None,
        *,
        lease_owner: str = "",
    ) -> bool:
        self._ensure()
        now_text = utc_text()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                owner_sql = " AND lease_owner = ?" if lease_owner else ""
                params = [
                    _json_text(result or {}, {}),
                    now_text,
                    now_text,
                    coerce_int(job_id, 0),
                ]
                if lease_owner:
                    params.append(str(lease_owner))
                cursor.execute(
                    f"""
                    UPDATE intel_jobs
                    SET status = 'completed',
                        result_json = ?,
                        last_error = '',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        completed_at = ?,
                        updated_at = ?
                    WHERE id = ? AND status = 'running'{owner_sql}
                    """,
                    params,
                )
                self.db.connection.commit()
                return cursor.rowcount > 0
            finally:
                cursor.close()

    def fail_job(
        self,
        job_id: int,
        error: str,
        *,
        retry_delay_seconds: Optional[int] = None,
        lease_owner: str = "",
        retryable: bool = True,
    ) -> str:
        self._ensure()
        now = utc_now()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    "SELECT attempt_count, max_attempts, status, lease_owner "
                    "FROM intel_jobs WHERE id = ?",
                    (coerce_int(job_id, 0),),
                )
                row = cursor.fetchone()
                if not row:
                    return "missing"
                if row["status"] == "cancelled":
                    return "cancelled"
                if row["status"] != "running" or (
                    lease_owner and row["lease_owner"] != str(lease_owner)
                ):
                    return "lease_lost"
                retry = bool(retryable) and int(row["attempt_count"] or 0) < int(
                    row["max_attempts"] or 1
                )
                if retry:
                    delay = retry_delay_seconds
                    if delay is None:
                        delay = min(3600, 30 * (2 ** max(0, int(row["attempt_count"] or 1) - 1)))
                    status = "retry_wait"
                    next_retry = utc_text(now + timedelta(seconds=max(1, delay)))
                    completed_at = None
                else:
                    status = "failed"
                    next_retry = None
                    completed_at = utc_text(now)
                owner_sql = " AND lease_owner=?" if lease_owner else ""
                params = [
                    status,
                    next_retry,
                    str(error or "")[:2000],
                    completed_at,
                    utc_text(now),
                    coerce_int(job_id, 0),
                ]
                if lease_owner:
                    params.append(str(lease_owner))
                cursor.execute(
                    f"""
                    UPDATE intel_jobs
                    SET status = ?,
                        next_retry_at = ?,
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        last_error = ?,
                        completed_at = ?,
                        updated_at = ?
                    WHERE id = ? AND status='running'{owner_sql}
                    """,
                    params,
                )
                self.db.connection.commit()
                return status if cursor.rowcount else "lease_lost"
            finally:
                cursor.close()

    def get_job(self, job_id: int) -> Optional[Dict]:
        self._ensure()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute("SELECT * FROM intel_jobs WHERE id = ?", (coerce_int(job_id, 0),))
                row = cursor.fetchone()
                if not row:
                    return None
                result = dict(row)
                result["payload"] = _json_value(result.pop("payload_json", "{}"), {})
                result["result"] = _json_value(result.pop("result_json", "{}"), {})
                return result
            finally:
                cursor.close()

    def reap_stuck_jobs(self, *, max_runtime_seconds: int, limit: int = 50) -> List[Dict]:
        """回收"运行时长超阈值但租约还有效"的作业（调度层主动巡检，不依赖作业自己）。

        为什么要它：心跳会持续续租，所以租约回收救不了卡死的作业（实测有作业卡了 19.3 小时）。
        这里按 started_at（领取时间）判断真实运行时长，超过阈值就按可重试失败写回；
        fail_job 内部已有 attempt_count < max_attempts 的重试上限与指数退避，
        因此不会无限重试。返回被回收的作业列表，供调用方记录/告警。
        """
        threshold = max(60, int(max_runtime_seconds or 0))
        self._ensure()
        # 时间比较在 Python 侧算好再按文本比：存储格式是 UTC ISO（…Z），
        # 而 datetime('now', '-N seconds') 在 PostgreSQL 上不可用（实测巡检查不到任何行）。
        cutoff = utc_text(utc_now() - timedelta(seconds=threshold))
        with self.db.lock:
            rows = self.db.connection.execute(
                """
                SELECT id, job_type, lease_owner, started_at, created_at
                  FROM intel_jobs
                 WHERE status = 'running'
                   AND COALESCE(started_at, created_at) <= ?
                 ORDER BY COALESCE(started_at, created_at) ASC
                 LIMIT ?
                """,
                (cutoff, max(1, int(limit))),
            ).fetchall()
        reclaimed = []
        for row in rows:
            item = dict(row)
            error = ("运行时长超过 %s 秒阈值，由调度巡检回收（owner=%s）"
                     % (threshold, str(item.get("lease_owner") or "")[:40]))
            status = self.fail_job(int(item["id"]), error, retryable=True)
            item["reclaimed_status"] = status
            if status in {"retry_wait", "failed"}:
                reclaimed.append(item)
        return reclaimed

    def record_worker_heartbeat(
        self,
        worker_id: str,
        *,
        lane: str = "",
        pid: int = 0,
        host: str = "",
        status: str = "running",
        inflight_count: int = 0,
        timeout_streak: int = 0,
        note: str = "",
        detail: Optional[Dict] = None,
    ) -> None:
        """worker 心跳：上报进程、当前在跑的作业与连续超时次数，供调度层巡检。"""
        self._ensure()
        now_text = utc_text(utc_now())
        payload = _json_text(detail or {}, {})
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO intel_worker_heartbeats(
                        worker_id, lane, pid, host, started_at, last_seen, status,
                        inflight_count, timeout_streak, note, detail_json, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(worker_id) DO UPDATE SET
                        lane=excluded.lane, pid=excluded.pid, host=excluded.host,
                        last_seen=excluded.last_seen, status=excluded.status,
                        inflight_count=excluded.inflight_count,
                        timeout_streak=excluded.timeout_streak,
                        note=excluded.note, detail_json=excluded.detail_json,
                        updated_at=excluded.updated_at
                    """,
                    (str(worker_id), str(lane), int(pid or 0), str(host)[:120], now_text,
                     now_text, str(status), int(inflight_count or 0), int(timeout_streak or 0),
                     str(note)[:500], payload, now_text),
                )
                self.db.connection.commit()
            except Exception:
                self.db.connection.rollback()
                raise
            finally:
                cursor.close()

    def list_worker_heartbeats(self, *, limit: int = 50) -> List[Dict]:
        self._ensure()
        with self.db.lock:
            rows = self.db.connection.execute(
                """
                SELECT worker_id, lane, pid, host, started_at, last_seen, status,
                       inflight_count, timeout_streak, note, detail_json,
                       round(EXTRACT(EPOCH FROM (now() - last_seen::timestamptz))) AS since_seen_s
                  FROM intel_worker_heartbeats ORDER BY last_seen DESC LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_job_by_dedupe_key(self, dedupe_key: str) -> Optional[Dict]:
        self._ensure()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    "SELECT id FROM intel_jobs WHERE dedupe_key=? ORDER BY id DESC LIMIT 1",
                    (str(dedupe_key or ""),),
                )
                row = cursor.fetchone()
                return self.get_job(int(row["id"])) if row else None
            finally:
                cursor.close()

    def get_article(self, article_id: int) -> Optional[Dict]:
        self._ensure()
        from remote_result_ingestor import ensure_remote_pipeline_schema
        ensure_remote_pipeline_schema(self.db)
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT a.id, a.url, a.canonical_url, a.title, a.content, a.domain, a.publish_date,
                           a.content_hash, a.first_crawled, a.created_at, a.updated_at,
                           a.matched_keywords,
                           d.summary AS generated_summary,
                           d.translated_title AS generated_translated_title,
                           d.translated_content AS generated_translated_content,
                           d.source_language AS generated_source_language,
                           d.target_language AS generated_target_language,
                           d.model_id AS generated_model_id,
                           d.prompt_version AS generated_prompt_version,
                           d.status AS generated_status,
                           d.remote_job_id AS generated_remote_job_id,
                           m.manifest_json AS generated_audio_manifest,
                           m.status AS generated_audio_status,
                           m.remote_job_id AS generated_audio_remote_job_id
                    FROM articles a
                    LEFT JOIN article_derivatives d ON d.id=(
                        SELECT id FROM article_derivatives WHERE article_id=a.id ORDER BY updated_at DESC,id DESC LIMIT 1
                    )
                    LEFT JOIN article_audio_manifests m ON m.id=(
                        SELECT id FROM article_audio_manifests WHERE article_id=a.id ORDER BY updated_at DESC,id DESC LIMIT 1
                    )
                    WHERE a.id = ? AND a.status = 'active'
                    """,
                    (coerce_int(article_id, 0),),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                result = dict(row)
                try:
                    result['generated_audio_manifest'] = json.loads(result.get('generated_audio_manifest') or '{}')
                except (TypeError, ValueError):
                    result['generated_audio_manifest'] = {}
                return result
            finally:
                cursor.close()

    def list_ai_recent_articles(
        self,
        *,
        page: int = 1,
        per_page: int = 20,
        project_keywords: Optional[Iterable[str]] = None,
    ) -> Tuple[List[Dict], int]:
        """List saved AI Q&A, optionally gated by the active industry terms."""
        self._ensure()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                where = "a.status='active' AND a.url LIKE 'ai://chat%' AND EXISTS (SELECT 1 FROM article_ragflow_documents d WHERE d.article_id=a.id AND d.sync_status IN ('uploaded','parsed'))"
                if project_keywords is not None:
                    from project_keyword_gate import matched_project_keywords

                    keywords = list(project_keywords)
                    if not keywords:
                        return [], 0
                    cursor.execute(
                        f"""SELECT a.id AS article_id,a.url,a.title,a.domain,a.publish_date,a.first_crawled,a.created_at,a.content_length,substr(a.content,1,360) AS content_preview,'AI助手问答' AS source_display_name,'recent' AS final_category,1.0 AS final_confidence,'已保存至知识库的 AI 助手问答' AS final_reason,COALESCE(a.first_crawled,a.created_at) AS effective_time,a.content,a.matched_keywords FROM articles a WHERE {where} ORDER BY COALESCE(a.first_crawled,a.created_at) DESC LIMIT 1000"""
                    )
                    filtered = []
                    for row in cursor.fetchall():
                        record = dict(row)
                        hits = matched_project_keywords(
                            keywords,
                            record.get("title"),
                            record.pop("content", ""),
                            record.get("matched_keywords"),
                        )
                        record.pop("matched_keywords", None)
                        if hits:
                            record["matched_keywords"] = hits
                            record["keyword_gate_source"] = "active_published_industry_pack"
                            filtered.append(record)
                    page = coerce_int(page, 1, 1)
                    per_page = coerce_int(per_page, 20, 1, 100)
                    offset = (page - 1) * per_page
                    return filtered[offset : offset + per_page], len(filtered)
                cursor.execute(f"SELECT COUNT(*) FROM articles a WHERE {where}")
                total = int(cursor.fetchone()[0])
                cursor.execute(f"""SELECT a.id AS article_id,a.url,a.title,a.domain,a.publish_date,a.first_crawled,a.created_at,a.content_length,substr(a.content,1,360) AS content_preview,'AI助手问答' AS source_display_name,'recent' AS final_category,1.0 AS final_confidence,'已保存至知识库的 AI 助手问答' AS final_reason,COALESCE(a.first_crawled,a.created_at) AS effective_time FROM articles a WHERE {where} ORDER BY COALESCE(a.first_crawled,a.created_at) DESC LIMIT ? OFFSET ?""", (per_page, (page-1)*per_page))
                return [dict(row) for row in cursor.fetchall()], total
            finally:
                cursor.close()

    def list_dashboard_recent_articles(
        self,
        *,
        page: int = 1,
        per_page: int = 20,
        project_keywords: Optional[Iterable[str]] = None,
        search: str = "",
    ) -> Tuple[List[Dict], int]:
        """Paginate the same followed + saved-AI stream shown on the dashboard."""

        page = coerce_int(page, 1, 1)
        per_page = coerce_int(per_page, 20, 1, 100)
        keywords = list(project_keywords) if project_keywords is not None else None
        followed = self.dashboard_followed_articles(
            limit=300,
            project_keywords=keywords,
        )
        ai_articles: List[Dict] = []
        ai_page = 1
        ai_total = 0
        while True:
            batch, ai_total = self.list_ai_recent_articles(
                page=ai_page,
                per_page=100,
                project_keywords=keywords,
            )
            ai_articles.extend(batch)
            if not batch or len(ai_articles) >= ai_total or ai_page >= 10:
                break
            ai_page += 1

        seen = set()
        combined: List[Dict] = []
        for item in followed + ai_articles:
            article_id = int(item.get("article_id") or 0)
            if article_id <= 0 or article_id in seen:
                continue
            seen.add(article_id)
            combined.append(item)

        query = str(search or "").strip().casefold()
        if query:
            combined = [
                item
                for item in combined
                if query
                in " ".join(
                    str(item.get(field) or "")
                    for field in ("title", "content_preview", "final_reason")
                ).casefold()
            ]
        total = len(combined)
        offset = (page - 1) * per_page
        return combined[offset : offset + per_page], total

    def toggle_dashboard_follow(self, article_id: int) -> bool:
        """Toggle a dashboard article in the shared current-focus stream."""
        self._ensure(); article_id = coerce_int(article_id, 0)
        if not article_id or not self.get_article(article_id): raise ValueError('文章不存在或已删除')
        with self.db.lock:
            row = self.db.connection.execute("SELECT setting_value FROM intel_runtime_settings WHERE setting_key='dashboard_followed_articles'").fetchone()
            try: values = json.loads(row[0]) if row else []
            except (TypeError, ValueError): values = []
            ids = [int(item) for item in values if str(item).isdigit()]
            following = article_id not in ids
            ids = ([article_id] + [item for item in ids if item != article_id]) if following else [item for item in ids if item != article_id]
            self.db.connection.execute("INSERT INTO intel_runtime_settings(setting_key,setting_value,updated_at) VALUES ('dashboard_followed_articles',?,datetime('now')) ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value,updated_at=excluded.updated_at", (json.dumps(ids[:300]),))
            self.db.connection.commit()
            return following

    def dashboard_followed_articles(
        self,
        *,
        limit: int = 100,
        project_keywords: Optional[Iterable[str]] = None,
    ) -> List[Dict]:
        self._ensure()
        with self.db.lock:
            row = self.db.connection.execute("SELECT setting_value FROM intel_runtime_settings WHERE setting_key='dashboard_followed_articles'").fetchone()
            try: ids = [int(item) for item in json.loads(row[0])] if row else []
            except (TypeError, ValueError): ids = []
            # When a project gate is active, inspect the complete bounded
            # follow list first so unrelated leading items cannot crowd out a
            # later on-industry item.
            ids = ids[:300 if project_keywords is not None else max(1, limit)]
            if not ids: return []
            marks = ','.join('?' for _ in ids)
            rows = self.db.connection.execute(f"SELECT id AS article_id,url,title,domain,publish_date,first_crawled,created_at,content_length,substr(content,1,360) AS content_preview,'当前关注' AS source_display_name,'recent' AS final_category,1.0 AS final_confidence,'手动关注的资讯' AS final_reason,COALESCE(first_crawled,created_at) AS effective_time,matched_keywords,content FROM articles WHERE status='active' AND id IN ({marks})", ids).fetchall()
            by_id = {}
            for item in rows:
                record = dict(item)
                content = record.pop('content', '')
                try: record['matched_keywords'] = json.loads(record.get('matched_keywords') or '[]')
                except (TypeError, ValueError): record['matched_keywords'] = []
                if project_keywords is not None:
                    from project_keyword_gate import matched_project_keywords

                    hits = matched_project_keywords(
                        list(project_keywords),
                        record.get('title'),
                        content,
                        record.get('matched_keywords'),
                    )
                    if not hits:
                        continue
                    record['matched_keywords'] = hits
                    record['keyword_gate_source'] = 'active_published_industry_pack'
                by_id[int(record['article_id'])] = record
            return [by_id[item] for item in ids if item in by_id][:max(1, limit)]

    def article_content_hash(self, article: Dict) -> str:
        existing = str((article or {}).get("content_hash") or "").strip()
        if existing:
            return existing
        content = str((article or {}).get("content") or "")
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def delete_article_classification(self, article_id: int, industry_pack_id: str) -> bool:
        """删除某文章在指定行业包下的分类行（禁止兜底归包：未命中锚点词时清除历史行）。"""
        self._ensure()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    "DELETE FROM article_intel_classifications "
                    "WHERE article_id=? AND industry_pack_id=?",
                    (int(article_id), str(industry_pack_id)),
                )
                self.db.connection.commit()
                return cursor.rowcount > 0
            finally:
                cursor.close()

    def enqueue_classification(
        self,
        article_id: int,
        industry_pack_id: str = DEFAULT_INDUSTRY_PACK_ID,
        *,
        request_id: str = "",
        force: bool = False,
        created_by: str = "",
        activation_id: Optional[str] = None,
        ragflow_upload: bool = False,
    ) -> Tuple[int, bool]:
        article = self.get_article(article_id)
        if not article:
            raise ValueError(f"article not found: {article_id}")
        content_hash = self.article_content_hash(article)
        # A content hash alone is not enough to determine whether a
        # classification is reusable.  Industry keyword gates evolve, and a
        # previously classified article must be revisited when its pack
        # version changes.
        from industry_packs import industry_pack_loader
        pack_version = str(
            industry_pack_loader.load(industry_pack_id)["pack_version"]
        )
        runtime_context = self.active_runtime_context()
        activation_id = str(
            runtime_context.get("activation_id") or ""
            if activation_id is None
            else activation_id or ""
        )
        if not force:
            self._ensure()
            with self.db.lock:
                cursor = self.db.connection.cursor()
                try:
                    cursor.execute(
                        """
                        SELECT article_content_hash, industry_pack_version, activation_id
                        FROM article_intel_classifications
                        WHERE article_id = ? AND industry_pack_id = ?
                        """,
                        (coerce_int(article_id, 0), industry_pack_id),
                    )
                    row = cursor.fetchone()
                    if (
                        row
                        and str(row["article_content_hash"] or "") == content_hash
                        and str(row["industry_pack_version"] or "") == pack_version
                        and str(row["activation_id"] or "") == activation_id
                    ):
                        if not ragflow_upload:
                            return 0, False
                finally:
                    cursor.close()
        dedupe_key = f"classify:{article_id}:{industry_pack_id}:{pack_version}:{activation_id}:{content_hash}"
        if force:
            dedupe_key = f"{dedupe_key}:force:{utc_text()}"
        payload = {
            "article_id": int(article_id),
            "industry_pack_id": industry_pack_id,
            "activation_id": activation_id,
            "article_content_hash": content_hash,
            "force": bool(force),
            "ragflow_upload": bool(ragflow_upload),
        }
        job_id, created = self.enqueue_job(
            "classification",
            dedupe_key,
            payload,
            request_id=request_id,
            created_by=created_by,
            # 分类是"新内容上线"的关键路径：既决定行业包归属/看板分类，也是问答检索的准入依据。
            # 实测教训：之前用 priority=10，而 INTEL_JOB_PRIORITY_AGING_SECONDS=30（每等 30 秒 +1），
            # 于是任何等待超过约 1 分钟的维护类作业都会盖过它 —— 优先级形同虚设、退化成纯 FIFO，
            # 分类被 31 小时未处理的 trend_aggregate/topic_cluster 长期插队（14 小时只完成 11 个）。
            # 现在给一个明显高于维护类作业的优先级，让它稳定排到前面；aging 仍会保证
            # 极老的作业最终能插回来，不会把其它类型饿死。
            priority=_CLASSIFICATION_JOB_PRIORITY,
        )
        if ragflow_upload and not created:
            with self.db.lock:
                row = self.db.connection.execute(
                    "SELECT status,payload_json FROM intel_jobs WHERE id=?",
                    (int(job_id),),
                ).fetchone()
                if row and str(row["status"]) in {"queued", "retry_wait"}:
                    merged = _json_value(row["payload_json"], {})
                    merged["ragflow_upload"] = True
                    self.db.connection.execute(
                        "UPDATE intel_jobs SET payload_json=?,updated_at=? WHERE id=?",
                        (_json_text(merged, {}), utc_text(), int(job_id)),
                    )
                    self.db.connection.commit()
                elif row and str(row["status"]) == "running":
                    return self.enqueue_job(
                        "classification",
                        f"{dedupe_key}:ragflow-upload:{utc_text()}",
                        payload,
                        request_id=request_id,
                        created_by=created_by,
                    )
        return job_id, created

    def upsert_classification(self, result: Dict) -> int:
        self._ensure()
        now_text = utc_text()
        incoming_activation_id = str(result.get("activation_id") or "")
        current_activation_id = str(
            self.active_runtime_context().get("activation_id") or ""
        )
        category = normalize_internal_category(result.get("final_category") or result.get("rule_category"))
        rule_category = normalize_internal_category(result.get("rule_category") or category)
        values = (
            coerce_int(result.get("article_id"), 0),
            str(result.get("industry_pack_id") or DEFAULT_INDUSTRY_PACK_ID),
            str(result.get("activation_id") or ""),
            str(result.get("industry_pack_version") or ""),
            str(result.get("classifier_version") or ""),
            str(result.get("article_content_hash") or ""),
            rule_category,
            float(result.get("rule_confidence") or 0),
            str(result.get("rule_reason") or ""),
            _json_text(result.get("score_details") or {}, {}),
            _json_text(result.get("matched_keywords") or [], []),
            result.get("llm_category"),
            result.get("llm_confidence"),
            str(result.get("llm_reason") or ""),
            str(result.get("why_important") or ""),
            str(result.get("trend_summary") or ""),
            _json_text(result.get("topic_tags") or [], []),
            category,
            float(result.get("final_confidence") or result.get("rule_confidence") or 0),
            str(result.get("final_reason") or result.get("rule_reason") or ""),
            str(result.get("result_source") or "rule"),
            str(result.get("fusion_version") or ""),
            str(result.get("llm_model_id") or ""),
            str(result.get("llm_prompt_version") or ""),
            str(result.get("llm_error") or "")[:2000],
            now_text,
            now_text,
        )
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                # A handler claimed before a switch may finish afterwards.
                # Preserve its job result for audit, but never let that stale
                # write replace a classification already produced by the new
                # current activation for the same article/pack identity.
                if (
                    incoming_activation_id
                    and current_activation_id
                    and incoming_activation_id != current_activation_id
                ):
                    existing = cursor.execute(
                        """
                        SELECT id, activation_id
                        FROM article_intel_classifications
                        WHERE article_id=? AND industry_pack_id=?
                        """,
                        (values[0], values[1]),
                    ).fetchone()
                    if existing and str(existing["activation_id"] or "") == current_activation_id:
                        return int(existing["id"])
                cursor.execute(
                    """
                    INSERT INTO article_intel_classifications (
                        article_id, industry_pack_id, activation_id, industry_pack_version,
                        classifier_version, article_content_hash,
                        rule_category, rule_confidence, rule_reason,
                        score_details_json, matched_keywords_json,
                        llm_category, llm_confidence, llm_reason,
                        why_important, trend_summary, topic_tags_json,
                        final_category, final_confidence, final_reason,
                        result_source, fusion_version, llm_model_id,
                        llm_prompt_version, llm_error, classified_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    ON CONFLICT(article_id, industry_pack_id) DO UPDATE SET
                        activation_id = excluded.activation_id,
                        industry_pack_version = excluded.industry_pack_version,
                        classifier_version = excluded.classifier_version,
                        article_content_hash = excluded.article_content_hash,
                        rule_category = excluded.rule_category,
                        rule_confidence = excluded.rule_confidence,
                        rule_reason = excluded.rule_reason,
                        score_details_json = excluded.score_details_json,
                        matched_keywords_json = excluded.matched_keywords_json,
                        llm_category = excluded.llm_category,
                        llm_confidence = excluded.llm_confidence,
                        llm_reason = excluded.llm_reason,
                        why_important = excluded.why_important,
                        trend_summary = excluded.trend_summary,
                        topic_tags_json = excluded.topic_tags_json,
                        final_category = excluded.final_category,
                        final_confidence = excluded.final_confidence,
                        final_reason = excluded.final_reason,
                        result_source = excluded.result_source,
                        fusion_version = excluded.fusion_version,
                        llm_model_id = excluded.llm_model_id,
                        llm_prompt_version = excluded.llm_prompt_version,
                        llm_error = excluded.llm_error,
                        classified_at = excluded.classified_at,
                        updated_at = excluded.updated_at
                    """,
                    values,
                )
                cursor.execute(
                    """
                    SELECT id FROM article_intel_classifications
                    WHERE article_id = ? AND industry_pack_id = ?
                    """,
                    (values[0], values[1]),
                )
                row = cursor.fetchone()
                # 按用户物化可见性（叠加语义）：只对有个人门禁设置的用户写行。
                # 用同一个 cursor、在同一事务内完成；失败只忽略，绝不影响分类结果。
                try:
                    from pack_user_gate import materialize_visibility
                    materialize_visibility(cursor, int(values[0]), str(values[1]))
                except Exception as materialize_exc:
                    print(f"⚠️ 个人可见性物化失败（不影响分类）: {materialize_exc}")
                self.db.connection.commit()
                return int(row["id"])
            finally:
                cursor.close()

    def record_financial_addon_match(
        self,
        article_id: int,
        *,
        activation_id: str,
        primary_industry_pack_id: str,
        matched_keywords: Iterable[str],
    ) -> Dict:
        """Persist the current primary-pack gate decision for a finance article."""

        self._ensure()
        keywords = list(
            dict.fromkeys(
                str(value or "").strip()
                for value in matched_keywords
                if str(value or "").strip()
            )
        )
        with self.db.lock:
            self.db.connection.execute(
                """
                INSERT INTO financial_addon_article_matches(
                    article_id, activation_id, primary_industry_pack_id,
                    matched_keywords_json, is_visible
                ) VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(article_id, activation_id) DO UPDATE SET
                    primary_industry_pack_id=excluded.primary_industry_pack_id,
                    matched_keywords_json=excluded.matched_keywords_json,
                    is_visible=excluded.is_visible,
                    evaluated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                """,
                (
                    int(article_id),
                    str(activation_id),
                    str(primary_industry_pack_id),
                    _json_text(keywords, []),
                    int(bool(keywords)),
                ),
            )
            self.db.connection.commit()
        return {
            "article_id": int(article_id),
            "activation_id": str(activation_id),
            "primary_industry_pack_id": str(primary_industry_pack_id),
            "matched_keywords": keywords,
            "is_visible": bool(keywords),
        }

    def list_unclassified_articles(
        self,
        industry_pack_id: str,
        *,
        after_id: int = 0,
        limit: int = 100,
    ) -> List[Dict]:
        self._ensure()
        from industry_packs import industry_pack_loader
        pack_version = industry_pack_loader.load(industry_pack_id)["pack_version"]
        runtime = self.active_runtime_context()
        activation_id = (
            str(runtime["activation_id"])
            if str(runtime["primary_industry_pack_id"]) == str(industry_pack_id)
            else ""
        )
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT a.id, a.content_hash, a.content
                    FROM articles a
                    LEFT JOIN article_intel_classifications c
                      ON c.article_id = a.id AND c.industry_pack_id = ?
                    WHERE a.status = 'active'
                      AND a.id > ?
                      AND (
                        c.id IS NULL
                        OR c.article_content_hash != COALESCE(NULLIF(a.content_hash, ''), c.article_content_hash)
                        OR c.industry_pack_version != ?
                        OR (? != '' AND c.activation_id != ?)
                      )
                    ORDER BY a.id ASC
                    LIMIT ?
                    """,
                    (
                        industry_pack_id,
                        coerce_int(after_id, 0, 0),
                        pack_version,
                        activation_id,
                        activation_id,
                        coerce_int(limit, 100, 1, 1000),
                    ),
                )
                return [dict(row) for row in cursor.fetchall()]
            finally:
                cursor.close()

    def list_dashboard_other_articles(
        self,
        *,
        industry_pack_id: str = DEFAULT_INDUSTRY_PACK_ID,
        time_range: str = "7d",
        domain: str = "",
        min_confidence: float = 0,
        page: int = 1,
        per_page: int = 20,
        ragflow_kb_id: str = "",
        ai_only: bool = False,
        policy_only: bool = False,
        exclude_article_ids: Optional[Iterable[int]] = None,
        search: str = "",
    ) -> Tuple[List[Dict], int, Dict]:
        """Return relevant residual articles for the dashboard "other" section.

        Explicit ``other`` classifications still need pack-anchor evidence.
        Unclassified rows are admitted only when their title or stored matched
        keywords contain a core keyword from the requested pack.  Body-only
        matches are intentionally excluded because archive/list pages commonly
        mention unrelated industries in navigation or recommended links.
        """

        self._ensure()
        start, end = parse_time_range(time_range)
        page = coerce_int(page, 1, 1)
        per_page = coerce_int(per_page, 20, 1, 100)
        from industry_packs import industry_pack_loader

        pack = industry_pack_loader.load(industry_pack_id)
        pack_version = pack["pack_version"]
        core_keywords = list(
            dict.fromkeys(
                str(value or "").strip()
                for value in pack.get("core_keywords") or []
                if str(value or "").strip()
            )
        )
        runtime = self.active_runtime_context()
        activation_id = (
            str(runtime.get("activation_id") or "")
            if str(runtime.get("primary_industry_pack_id")) == str(industry_pack_id)
            else ""
        )
        source_display_sql = """
                        COALESCE(
                            (SELECT s.source_name
                             FROM intel_candidates ic
                             JOIN intel_candidate_observations o ON o.candidate_id=ic.id
                             JOIN intel_sources s ON s.id=o.source_id
                             WHERE ic.article_id=a.id AND TRIM(COALESCE(s.source_name, ''))!=''
                             ORDER BY o.last_observed_at DESC, o.id DESC LIMIT 1),
                            (SELECT mu.name FROM managed_urls mu
                             WHERE mu.id=a.source_url_id AND TRIM(COALESCE(mu.name, ''))!=''),
                            (SELECT mu.name FROM managed_urls mu
                             WHERE LOWER(COALESCE(mu.domain, ''))=LOWER(COALESCE(a.domain, ''))
                               AND TRIM(COALESCE(mu.name, ''))!=''
                             ORDER BY mu.is_active DESC, mu.id ASC LIMIT 1),
                            a.domain
                        )
        """
        classified_filters = [
            "a.status = 'active'",
            "c.industry_pack_id = ?",
            "c.final_confidence >= ?",
            "c.final_category = 'other'",
            INDUSTRY_KEYWORD_GATE_SQL,
            f"datetime({ARTICLE_TIME_SQL}) >= datetime(?)",
            f"datetime({ARTICLE_TIME_SQL}) <= datetime(?)",
        ]
        classified_params: List = [
            industry_pack_id,
            max(0.0, min(1.0, float(min_confidence or 0))),
            utc_text(start),
            utc_text(end),
        ]
        if activation_id:
            classified_filters.append("c.activation_id = ?")
            classified_params.append(activation_id)
        if ai_only:
            classified_filters.append("a.url LIKE 'ai://chat%'")
        else:
            classified_filters.append("a.url NOT LIKE 'ai://chat%'")
        if policy_only:
            classified_filters.append(POLICY_ARTICLE_SQL)
        if domain:
            classified_filters.append("a.domain = ?")
            classified_params.append(domain)
        if str(search or "").strip():
            classified_filters.append("(a.title LIKE ? OR a.content LIKE ?)")
            like = f"%{str(search).strip()}%"
            classified_params.extend([like, like])
        exclude_ids = []
        for raw_id in exclude_article_ids or []:
            try:
                article_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if article_id > 0 and article_id not in exclude_ids:
                exclude_ids.append(article_id)
        if exclude_ids:
            exclude_sql = f"a.id NOT IN ({','.join('?' for _ in exclude_ids)})"
            classified_filters.append(exclude_sql)
            classified_params.extend(exclude_ids)
        unclassified_filters = [
            "a.status = 'active'",
            "("
            "c.id IS NULL OR "
            "c.article_content_hash != COALESCE(NULLIF(a.content_hash, ''), c.article_content_hash) OR "
            "c.industry_pack_version != ? OR "
            "(? != '' AND c.activation_id != ?)"
            ")",
            "NOT (c.id IS NOT NULL AND c.final_category = 'other')",
            f"datetime({ARTICLE_TIME_SQL}) >= datetime(?)",
            f"datetime({ARTICLE_TIME_SQL}) <= datetime(?)",
        ]
        unclassified_params: List = [
            pack_version,
            activation_id,
            activation_id,
            utc_text(start),
            utc_text(end),
        ]
        keyword_predicates = []
        for keyword in core_keywords:
            escaped = (
                keyword.casefold()
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            keyword_predicates.append(
                "(LOWER(COALESCE(a.title, '')) LIKE ? ESCAPE '\\' "
                "OR LOWER(COALESCE(a.matched_keywords, '')) LIKE ? ESCAPE '\\')"
            )
            pattern = f"%{escaped}%"
            unclassified_params.extend([pattern, pattern])
        unclassified_filters.append(
            f"({' OR '.join(keyword_predicates)})" if keyword_predicates else "0 = 1"
        )
        if ai_only:
            unclassified_filters.append("a.url LIKE 'ai://chat%'")
        else:
            unclassified_filters.append("a.url NOT LIKE 'ai://chat%'")
        if policy_only:
            unclassified_filters.append(POLICY_ARTICLE_SQL)
        if domain:
            unclassified_filters.append("a.domain = ?")
            unclassified_params.append(domain)
        if str(search or "").strip():
            unclassified_filters.append("(a.title LIKE ? OR a.content LIKE ?)")
            like = f"%{str(search).strip()}%"
            unclassified_params.extend([like, like])
        if exclude_ids:
            exclude_sql = f"a.id NOT IN ({','.join('?' for _ in exclude_ids)})"
            unclassified_filters.append(exclude_sql)
            unclassified_params.extend(exclude_ids)

        combined_sql = f"""
            WITH combined AS (
                SELECT
                    a.id AS article_id, a.url, a.title, a.domain, a.publish_date,
                    a.first_crawled, a.created_at, a.matched_keywords,
                    a.content_length, substr(COALESCE(a.content, ''), 1, 360) AS content_preview,
                    {source_display_sql} AS source_display_name,
                    c.industry_pack_id, c.activation_id, c.industry_pack_version, c.classifier_version,
                    c.rule_category, c.rule_confidence, c.rule_reason,
                    c.score_details_json, c.matched_keywords_json,
                    c.final_category, c.final_confidence, c.final_reason,
                    c.result_source, c.why_important, c.trend_summary,
                    c.topic_tags_json, c.classified_at,
                    {ARTICLE_TIME_SQL} AS effective_time,
                    CASE
                        WHEN a.publish_date IS NOT NULL AND TRIM(a.publish_date) != '' THEN 'articles.publish_date'
                        WHEN a.first_crawled IS NOT NULL THEN 'articles.first_crawled'
                        ELSE 'articles.created_at'
                    END AS time_source,
                    CASE
                        WHEN a.publish_date IS NOT NULL AND LENGTH(TRIM(a.publish_date)) = 10 THEN 'date'
                        ELSE 'timestamp'
                    END AS time_precision
                FROM article_intel_classifications c
                JOIN articles a ON a.id = c.article_id
                WHERE {" AND ".join(classified_filters)}

                UNION ALL

                SELECT
                    a.id AS article_id, a.url, a.title, a.domain, a.publish_date,
                    a.first_crawled, a.created_at, a.matched_keywords,
                    a.content_length, substr(COALESCE(a.content, ''), 1, 360) AS content_preview,
                    {source_display_sql} AS source_display_name,
                    ? AS industry_pack_id,
                    COALESCE(c.activation_id, '') AS activation_id,
                    '' AS industry_pack_version,
                    'rule-v1' AS classifier_version,
                    'other' AS rule_category,
                    0.0 AS rule_confidence,
                    '未分类文章，归入其他' AS rule_reason,
                    '{{}}' AS score_details_json,
                    '[]' AS matched_keywords_json,
                    'other' AS final_category,
                    0.0 AS final_confidence,
                    '未分类文章，归入其他' AS final_reason,
                    'unclassified_fallback' AS result_source,
                    '' AS why_important,
                    '' AS trend_summary,
                    '[]' AS topic_tags_json,
                    '' AS classified_at,
                    {ARTICLE_TIME_SQL} AS effective_time,
                    CASE
                        WHEN a.publish_date IS NOT NULL AND TRIM(a.publish_date) != '' THEN 'articles.publish_date'
                        WHEN a.first_crawled IS NOT NULL THEN 'articles.first_crawled'
                        ELSE 'articles.created_at'
                    END AS time_source,
                    CASE
                        WHEN a.publish_date IS NOT NULL AND LENGTH(TRIM(a.publish_date)) = 10 THEN 'date'
                        ELSE 'timestamp'
                    END AS time_precision
                FROM articles a
                LEFT JOIN article_intel_classifications c
                  ON c.article_id = a.id AND c.industry_pack_id = ?
                WHERE {" AND ".join(unclassified_filters)}
            )
            SELECT *
            FROM combined
            ORDER BY effective_time DESC, article_id DESC
            LIMIT ? OFFSET ?
        """
        count_sql = """
            WITH combined AS (
                SELECT 1 AS marker
                FROM article_intel_classifications c
                JOIN articles a ON a.id = c.article_id
                WHERE {classified_where}
                UNION ALL
                SELECT 1 AS marker
                FROM articles a
                LEFT JOIN article_intel_classifications c
                  ON c.article_id = a.id AND c.industry_pack_id = ?
                WHERE {unclassified_where}
            )
            SELECT COUNT(*) AS total FROM combined
        """.format(
            classified_where=" AND ".join(classified_filters),
            unclassified_where=" AND ".join(unclassified_filters),
        )
        count_params = list(classified_params) + [industry_pack_id] + list(unclassified_params)
        row_params = list(classified_params) + [industry_pack_id, industry_pack_id] + list(unclassified_params)
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(count_sql, count_params)
                total = int(cursor.fetchone()["total"])
                cursor.execute(
                    combined_sql,
                    row_params + [per_page, (page - 1) * per_page],
                )
                articles = [dict(row) for row in cursor.fetchall()]
                article_ids = [item["article_id"] for item in articles]
                ragflow_by_article: Dict[int, List[Dict]] = {}
                if article_ids:
                    placeholders = ",".join("?" for _ in article_ids)
                    ragflow_sql = (
                        f"SELECT article_id, kb_id, document_id, document_name, sync_status "
                        f"FROM article_ragflow_documents WHERE article_id IN ({placeholders})"
                    )
                    ragflow_params: List = list(article_ids)
                    if ragflow_kb_id:
                        ragflow_sql += " AND kb_id = ?"
                        ragflow_params.append(ragflow_kb_id)
                    cursor.execute(ragflow_sql, ragflow_params)
                    for row in cursor.fetchall():
                        ragflow_by_article.setdefault(int(row["article_id"]), []).append(dict(row))
            finally:
                cursor.close()

        for item in articles:
            item["score_details"] = _json_value(item.pop("score_details_json", "{}"), {})
            item["classification_keywords"] = _json_value(
                item.pop("matched_keywords_json", "[]"), []
            )
            if item.get("result_source") == "unclassified_fallback":
                from project_keyword_gate import matched_project_keywords

                item["classification_keywords"] = matched_project_keywords(
                    core_keywords,
                    item.get("title"),
                    item.get("matched_keywords"),
                )
            item["topic_tags"] = _json_value(item.pop("topic_tags_json", "[]"), [])
            item["category"] = public_category(item["final_category"])
            item["ragflow_documents"] = ragflow_by_article.get(item["article_id"], [])
            item["ragflow_status"] = (
                item["ragflow_documents"][0]["sync_status"]
                if len(item["ragflow_documents"]) == 1
                else ("multiple" if item["ragflow_documents"] else "not_uploaded")
            )
        if articles:
            from intel_evidence import IntelEvidenceService

            evidence_by_article = IntelEvidenceService(
                self.db
            ).evidence_for_articles(
                [item["article_id"] for item in articles],
                industry_pack_id=industry_pack_id,
            )
            for item in articles:
                item["source_evidence"] = evidence_by_article.get(
                    int(item["article_id"]),
                    {
                        "evidence_grade": "D",
                        "base_evidence_grade": "D",
                        "independent_source_count": 1,
                        "max_authority_level": 1,
                        "conflict_status": "none",
                        "citations": [],
                        "is_representative": True,
                    },
                )
        return articles, total, {
            "time_range": time_range,
            "from": utc_text(start),
            "to": utc_text(end),
            "timezone": "Asia/Hong_Kong",
        }

    def list_classified_articles(
        self,
        *,
        industry_pack_id: str = DEFAULT_INDUSTRY_PACK_ID,
        category: str = "",
        time_range: str = "7d",
        domain: str = "",
        min_confidence: float = 0,
        page: int = 1,
        per_page: int = 20,
        ragflow_kb_id: str = "",
        ai_only: bool = False,
        policy_only: bool = False,
        search: str = "",
    ) -> Tuple[List[Dict], int, Dict]:
        self._ensure()
        category_value = normalize_internal_category(category, allow_empty=True)
        start, end = parse_time_range(time_range)
        page = coerce_int(page, 1, 1)
        per_page = coerce_int(per_page, 20, 1, 100)
        conditions = [
            "a.status = 'active'",
            "c.industry_pack_id = ?",
            "c.final_confidence >= ?",
            INDUSTRY_KEYWORD_GATE_SQL,
            "NOT EXISTS ("
            "SELECT 1 FROM intel_evidence_group_articles ega "
            "JOIN intel_evidence_groups eg ON eg.id=ega.evidence_group_id "
            "WHERE ega.article_id=a.id "
            "AND eg.industry_pack_id=c.industry_pack_id "
            "AND eg.representative_article_id!=a.id)",
            f"datetime({ARTICLE_TIME_SQL}) >= datetime(?)",
            f"datetime({ARTICLE_TIME_SQL}) <= datetime(?)",
        ]
        # AI/RAGFlow Q&A is knowledge material, not an external market-news
        # signal.  Keep it in its dedicated dashboard section.
        if ai_only:
            conditions.append("a.url LIKE 'ai://chat%'")
        else:
            conditions.append("a.url NOT LIKE 'ai://chat%'")
        if policy_only:
            conditions.append(POLICY_ARTICLE_SQL)
        params: List = [
            industry_pack_id,
            max(0.0, min(1.0, float(min_confidence or 0))),
            utc_text(start),
            utc_text(end),
        ]
        runtime = self.active_runtime_context()
        if (
            str(runtime.get("activation_id") or "")
            and str(runtime.get("primary_industry_pack_id")) == str(industry_pack_id)
        ):
            conditions.append("c.activation_id = ?")
            params.append(str(runtime["activation_id"]))
        if category_value:
            conditions.append("c.final_category = ?")
            params.append(category_value)
        if domain:
            conditions.append("a.domain = ?")
            params.append(domain)
        if str(search or '').strip():
            conditions.append("(a.title LIKE ? OR a.content LIKE ?)")
            like = f"%{str(search).strip()}%"; params.extend([like, like])
        where_sql = " AND ".join(conditions)
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT COUNT(*) AS total
                    FROM article_intel_classifications c
                    JOIN articles a ON a.id = c.article_id
                    WHERE {where_sql}
                    """,
                    params,
                )
                total = int(cursor.fetchone()["total"])
                query_params = list(params) + [per_page, (page - 1) * per_page]
                cursor.execute(
                    f"""
                    SELECT
                        a.id AS article_id, a.url, a.title, a.domain, a.publish_date,
                        a.first_crawled, a.created_at, a.matched_keywords,
                        a.content_length, substr(COALESCE(a.content, ''), 1, 360) AS content_preview,
                        COALESCE(
                            (SELECT s.source_name
                             FROM intel_candidates ic
                             JOIN intel_candidate_observations o ON o.candidate_id=ic.id
                             JOIN intel_sources s ON s.id=o.source_id
                             WHERE ic.article_id=a.id AND TRIM(COALESCE(s.source_name, ''))!=''
                             ORDER BY o.last_observed_at DESC, o.id DESC LIMIT 1),
                            (SELECT mu.name FROM managed_urls mu
                             WHERE mu.id=a.source_url_id AND TRIM(COALESCE(mu.name, ''))!=''),
                            (SELECT mu.name FROM managed_urls mu
                             WHERE LOWER(COALESCE(mu.domain, ''))=LOWER(COALESCE(a.domain, ''))
                               AND TRIM(COALESCE(mu.name, ''))!=''
                             ORDER BY mu.is_active DESC, mu.id ASC LIMIT 1),
                            a.domain
                        ) AS source_display_name,
                        c.industry_pack_id, c.activation_id, c.industry_pack_version, c.classifier_version,
                        c.rule_category, c.rule_confidence, c.rule_reason,
                        c.score_details_json, c.matched_keywords_json,
                        c.final_category, c.final_confidence, c.final_reason,
                        c.result_source, c.why_important, c.trend_summary,
                        c.topic_tags_json, c.classified_at,
                        {ARTICLE_TIME_SQL} AS effective_time,
                        CASE
                            WHEN a.publish_date IS NOT NULL AND TRIM(a.publish_date) != '' THEN 'articles.publish_date'
                            WHEN a.first_crawled IS NOT NULL THEN 'articles.first_crawled'
                            ELSE 'articles.created_at'
                        END AS time_source,
                        CASE
                            WHEN a.publish_date IS NOT NULL AND LENGTH(TRIM(a.publish_date)) = 10 THEN 'date'
                            ELSE 'timestamp'
                        END AS time_precision
                    FROM article_intel_classifications c
                    JOIN articles a ON a.id = c.article_id
                    WHERE {where_sql}
                    ORDER BY effective_time DESC, a.id DESC
                    LIMIT ? OFFSET ?
                    """,
                    query_params,
                )
                articles = [dict(row) for row in cursor.fetchall()]
                article_ids = [item["article_id"] for item in articles]
                ragflow_by_article: Dict[int, List[Dict]] = {}
                if article_ids:
                    placeholders = ",".join("?" for _ in article_ids)
                    ragflow_sql = (
                        f"SELECT article_id, kb_id, document_id, document_name, sync_status "
                        f"FROM article_ragflow_documents WHERE article_id IN ({placeholders})"
                    )
                    ragflow_params: List = list(article_ids)
                    if ragflow_kb_id:
                        ragflow_sql += " AND kb_id = ?"
                        ragflow_params.append(ragflow_kb_id)
                    cursor.execute(ragflow_sql, ragflow_params)
                    for row in cursor.fetchall():
                        ragflow_by_article.setdefault(int(row["article_id"]), []).append(dict(row))
            finally:
                cursor.close()

        for item in articles:
            item["score_details"] = _json_value(item.pop("score_details_json", "{}"), {})
            item["classification_keywords"] = _json_value(
                item.pop("matched_keywords_json", "[]"), []
            )
            item["topic_tags"] = _json_value(item.pop("topic_tags_json", "[]"), [])
            item["category"] = public_category(item["final_category"])
            item["ragflow_documents"] = ragflow_by_article.get(item["article_id"], [])
            item["ragflow_status"] = (
                item["ragflow_documents"][0]["sync_status"]
                if len(item["ragflow_documents"]) == 1
                else ("multiple" if item["ragflow_documents"] else "not_uploaded")
            )
        if articles:
            from intel_evidence import IntelEvidenceService

            evidence_by_article = IntelEvidenceService(
                self.db
            ).evidence_for_articles(
                [item["article_id"] for item in articles],
                industry_pack_id=industry_pack_id,
            )
            for item in articles:
                item["source_evidence"] = evidence_by_article.get(
                    int(item["article_id"]),
                    {
                        "evidence_grade": "D",
                        "base_evidence_grade": "D",
                        "independent_source_count": 1,
                        "max_authority_level": 1,
                        "conflict_status": "none",
                        "citations": [],
                        "is_representative": True,
                    },
                )
        return articles, total, {
            "time_range": time_range,
            "from": utc_text(start),
            "to": utc_text(end),
            "timezone": "Asia/Hong_Kong",
        }

    def classification_summary(
        self,
        *,
        industry_pack_id: str = DEFAULT_INDUSTRY_PACK_ID,
        time_range: str = "7d",
    ) -> Dict:
        self._ensure()
        start, end = parse_time_range(time_range)
        runtime = self.active_runtime_context()
        activation_id = (
            str(runtime.get("activation_id") or "")
            if str(runtime.get("primary_industry_pack_id")) == str(industry_pack_id)
            else ""
        )
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                # 用抓取/入库日（first_crawled/created_at，香港自然日）作为"当日/近N天"口径，
                # 与 dashboard 的"今日抓取/行业动态"一致；否则按 publish_date（往往早于抓取日）
                # 会把"今天入库但发布时间不同"的文章漏成 0。
                from datetime import timezone as _tz, timedelta as _td
                _hk = _tz(_td(hours=8))
                start_d = start.astimezone(_hk).date().isoformat()
                end_d = end.astimezone(_hk).date().isoformat()
                cursor.execute(
                    f"""
                    SELECT c.final_category, COUNT(*) AS total
                    FROM article_intel_classifications c
                    JOIN articles a ON a.id = c.article_id
                    WHERE c.industry_pack_id = ?
                      AND (? = '' OR c.activation_id = '' OR c.activation_id = ?)
                      AND a.status = 'active'
                      AND {INDUSTRY_KEYWORD_GATE_SQL}
                      AND date(COALESCE(NULLIF(a.first_crawled,''), a.created_at), '+8 hours') >= ?
                      AND date(COALESCE(NULLIF(a.first_crawled,''), a.created_at), '+8 hours') <= ?
                    GROUP BY c.final_category
                    """,
                    (
                        industry_pack_id,
                        activation_id,
                        activation_id,
                        start_d,
                        end_d,
                    ),
                )
                counts = {row["final_category"]: int(row["total"]) for row in cursor.fetchall()}
            finally:
                cursor.close()
        return {
            "industry_pack_id": industry_pack_id,
            "time_range": time_range,
            "from": utc_text(start),
            "to": utc_text(end),
            "timezone": "Asia/Hong_Kong",
            "counts": {
                "trend": counts.get("trend", 0),
                "today": counts.get("event", 0),
                "other": counts.get("other", 0),
            },
            "total": sum(counts.values()),
        }


intel_repository = IntelRepository()


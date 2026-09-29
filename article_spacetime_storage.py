# -*- coding: utf-8 -*-
"""
文章时空画像存储子模块（从 sqlite_database.SQLiteDatabase 纯平移拆分）。

所有公开函数以 db 实例为第一参数；sqlite_database 中的 __getattr__ 代理会以
sqlite_db.<name>(...) 形式继续调用它们（零行为变化）。
"""

import json
from typing import Dict, List, Optional

from utils import coerce_int


def _ensure_article_spacetime_profiles_table(cursor):
    """Create the derived article spacetime profile table."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS article_spacetime_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER NOT NULL UNIQUE,
            time_value TEXT,
            time_type TEXT,
            time_confidence REAL DEFAULT 0,
            time_evidence TEXT,
            location_name TEXT,
            location_lat REAL,
            location_lng REAL,
            location_type TEXT,
            location_confidence REAL DEFAULT 0,
            location_evidence TEXT,
            source_location_name TEXT,
            source_location_lat REAL,
            source_location_lng REAL,
            event_location_name TEXT,
            event_location_lat REAL,
            event_location_lng REAL,
            jurisdiction_name TEXT,
            spacetime_status TEXT DEFAULT 'ready',
            analysis_version TEXT,
            analyzed_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
            metadata TEXT,
            created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
            updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE
        )
    """)


# ─── 文章时空画像 ───────────────────────────────────────────
def analyze_article_spacetime_profile(db, article_id: int) -> Optional[int]:
    """Best-effort analysis and persistence for one article."""
    try:
        article = db.get_article_by_id(article_id)
        if not article:
            return None
        from article_spacetime_analyzer import analyze_article_spacetime
        profile = analyze_article_spacetime(article)
        return save_article_spacetime_profile(db, article_id, profile)
    except Exception as e:
        print(f"⚠️ 文章时空画像生成失败 article_id={article_id}: {e}")
        return None


def save_article_spacetime_profile(db, article_id: int, profile: Dict) -> Optional[int]:
    """Insert or update one derived spacetime profile."""
    if not article_id or not profile:
        return None

    try:
        db._ensure_connection()
        with db.lock:
            cursor = db.connection.cursor()
            try:
                metadata = profile.get('metadata') or {}
                if not isinstance(metadata, str):
                    metadata = json.dumps(metadata, ensure_ascii=False)

                values = (
                    article_id,
                    profile.get('time_value'),
                    profile.get('time_type'),
                    profile.get('time_confidence') or 0,
                    profile.get('time_evidence') or '',
                    profile.get('location_name') or '',
                    profile.get('location_lat'),
                    profile.get('location_lng'),
                    profile.get('location_type') or '',
                    profile.get('location_confidence') or 0,
                    profile.get('location_evidence') or '',
                    profile.get('source_location_name') or '',
                    profile.get('source_location_lat'),
                    profile.get('source_location_lng'),
                    profile.get('event_location_name') or '',
                    profile.get('event_location_lat'),
                    profile.get('event_location_lng'),
                    profile.get('jurisdiction_name') or '',
                    profile.get('spacetime_status') or 'ready',
                    profile.get('analysis_version') or '',
                    metadata,
                )
                cursor.execute(
                    """
                    INSERT INTO article_spacetime_profiles (
                        article_id, time_value, time_type, time_confidence, time_evidence,
                        location_name, location_lat, location_lng, location_type,
                        location_confidence, location_evidence, source_location_name,
                        source_location_lat, source_location_lng, event_location_name,
                        event_location_lat, event_location_lng, jurisdiction_name,
                        spacetime_status, analysis_version, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(article_id) DO UPDATE SET
                        time_value = excluded.time_value,
                        time_type = excluded.time_type,
                        time_confidence = excluded.time_confidence,
                        time_evidence = excluded.time_evidence,
                        location_name = excluded.location_name,
                        location_lat = excluded.location_lat,
                        location_lng = excluded.location_lng,
                        location_type = excluded.location_type,
                        location_confidence = excluded.location_confidence,
                        location_evidence = excluded.location_evidence,
                        source_location_name = excluded.source_location_name,
                        source_location_lat = excluded.source_location_lat,
                        source_location_lng = excluded.source_location_lng,
                        event_location_name = excluded.event_location_name,
                        event_location_lat = excluded.event_location_lat,
                        event_location_lng = excluded.event_location_lng,
                        jurisdiction_name = excluded.jurisdiction_name,
                        spacetime_status = excluded.spacetime_status,
                        analysis_version = excluded.analysis_version,
                        analyzed_at = datetime('now', 'localtime'),
                        metadata = excluded.metadata,
                        updated_at = datetime('now', 'localtime')
                    """,
                    values
                )
                db.connection.commit()
                row_id = cursor.lastrowid
                if not row_id:
                    cursor.execute("SELECT id FROM article_spacetime_profiles WHERE article_id = ?", (article_id,))
                    row = cursor.fetchone()
                    row_id = row['id'] if row else None
                return row_id
            finally:
                cursor.close()
    except Exception as e:
        print(f"❌ 保存文章时空画像失败: {e}")
        return None


def get_article_spacetime_profile(db, article_id: int) -> Optional[Dict]:
    """Get one derived spacetime profile by article id."""
    try:
        db._ensure_connection()
        with db.lock:
            cursor = db.connection.cursor()
            try:
                cursor.execute("SELECT * FROM article_spacetime_profiles WHERE article_id = ?", (article_id,))
                row = cursor.fetchone()
                return dict(row) if row else None
            finally:
                cursor.close()
    except Exception as e:
        print(f"❌ 获取文章时空画像失败: {e}")
        return None


def get_article_spacetime_points(
    db,
    from_date: str = None,
    to_date: str = None,
    keyword: str = None,
    min_confidence: float = 0.3,
    limit: int = 1000,
    industry_pack_id: str = None,
    activation_id: str = None,
) -> List[Dict]:
    """Return articles joined with their spacetime profiles for map rendering."""
    try:
        db._ensure_connection()
        with db.lock:
            cursor = db.connection.cursor()
            try:
                where = [
                    "a.status = 'active'",
                    "a.url NOT LIKE 'ai://chat%'",
                    "p.time_value IS NOT NULL",
                    "p.location_lat IS NOT NULL",
                    "p.location_lng IS NOT NULL",
                    "COALESCE(p.location_confidence, 0) >= ?",
                ]
                params = [float(min_confidence or 0)]
                classification_join = ""
                classification_columns = """
                    NULL AS industry_pack_id,
                    NULL AS activation_id,
                    NULL AS industry_matched_keywords,
                    NULL AS final_category,
                    NULL AS final_confidence,
                    NULL AS final_reason,
                    NULL AS why_important,
                    NULL AS trend_summary,
                """
                if industry_pack_id:
                    classification_join = (
                        "JOIN article_intel_classifications c ON c.article_id=a.id"
                    )
                    classification_columns = """
                        c.industry_pack_id,
                        c.activation_id,
                        c.matched_keywords_json AS industry_matched_keywords,
                        c.final_category,
                        c.final_confidence,
                        c.final_reason,
                        c.why_important,
                        c.trend_summary,
                    """
                    where.extend([
                        "c.industry_pack_id = ?",
                        "COALESCE(json_array_length(json_extract(c.score_details_json, '$.hits.anchor')), 0) > 0",
                        "NOT EXISTS ("
                        "SELECT 1 FROM intel_evidence_group_articles ega "
                        "JOIN intel_evidence_groups eg ON eg.id=ega.evidence_group_id "
                        "WHERE ega.article_id=a.id "
                        "AND eg.industry_pack_id=c.industry_pack_id "
                        "AND eg.representative_article_id!=a.id)",
                    ])
                    params.append(str(industry_pack_id))
                    if activation_id:
                        where.append("c.activation_id = ?")
                        params.append(str(activation_id))
                if from_date:
                    where.append(
                        "datetime(CASE "
                        "WHEN a.publish_date IS NOT NULL AND LENGTH(TRIM(a.publish_date))=10 "
                        "THEN datetime(a.publish_date, '-8 hours') "
                        "WHEN a.publish_date IS NOT NULL AND TRIM(a.publish_date)!='' "
                        "THEN a.publish_date "
                        "WHEN a.first_crawled IS NOT NULL THEN a.first_crawled "
                        "ELSE datetime(a.created_at, '-8 hours') END) >= datetime(?)"
                    )
                    params.append(from_date)
                if to_date:
                    where.append(
                        "datetime(CASE "
                        "WHEN a.publish_date IS NOT NULL AND LENGTH(TRIM(a.publish_date))=10 "
                        "THEN datetime(a.publish_date, '-8 hours') "
                        "WHEN a.publish_date IS NOT NULL AND TRIM(a.publish_date)!='' "
                        "THEN a.publish_date "
                        "WHEN a.first_crawled IS NOT NULL THEN a.first_crawled "
                        "ELSE datetime(a.created_at, '-8 hours') END) <= datetime(?)"
                    )
                    params.append(to_date)
                if keyword:
                    like = f"%{keyword}%"
                    where.append("(a.title LIKE ? OR a.content LIKE ? OR a.matched_keywords LIKE ?)")
                    params.extend([like, like, like])

                params.append(coerce_int(limit, 1000, 1, 5000))
                cursor.execute(
                    f"""
                    SELECT
                        a.id, a.url, a.title, a.content, a.content_length,
                        a.domain, a.publish_date,
                        a.matched_keywords, a.quality_score, a.crawler_engine_used,
                        a.source_method, a.extraction_method,
                        {classification_columns}
                        COALESCE(
                            (SELECT s.source_name
                             FROM intel_candidates ic
                             JOIN intel_candidate_observations o ON o.candidate_id=ic.id
                             JOIN intel_sources s ON s.id=o.source_id
                             WHERE ic.article_id=a.id
                               AND TRIM(COALESCE(s.source_name, ''))!=''
                             ORDER BY o.last_observed_at DESC, o.id DESC LIMIT 1),
                            (SELECT mu.name FROM managed_urls mu
                             WHERE mu.id=a.source_url_id
                               AND TRIM(COALESCE(mu.name, ''))!=''),
                            a.domain
                        ) AS source_display_name,
                        p.id AS profile_id, p.time_value, p.time_type,
                        p.time_confidence, p.time_evidence, p.location_name,
                        p.location_lat, p.location_lng, p.location_type,
                        p.location_confidence, p.location_evidence,
                        p.source_location_name, p.source_location_lat,
                        p.source_location_lng, p.event_location_name,
                        p.event_location_lat, p.event_location_lng,
                        p.jurisdiction_name, p.spacetime_status,
                        p.analysis_version, p.analyzed_at, p.metadata
                    FROM articles a
                    JOIN article_spacetime_profiles p ON p.article_id = a.id
                    {classification_join}
                    WHERE {' AND '.join(where)}
                    ORDER BY p.time_value DESC, COALESCE(a.quality_score, 0) DESC
                    LIMIT ?
                    """,
                    params
                )
                return [dict(row) for row in cursor.fetchall()]
            finally:
                cursor.close()
    except Exception as e:
        print(f"❌ 获取文章时空点失败: {e}")
        return []


def get_articles_for_spacetime_analysis(
    db,
    mode: str = 'incremental',
    limit: int = 500,
    min_confidence: float = 0.4,
    industry_pack_id: str = None,
    activation_id: str = None,
) -> List[Dict]:
    """Return articles needing spacetime profile backfill or refresh."""
    try:
        db._ensure_connection()
        with db.lock:
            cursor = db.connection.cursor()
            try:
                mode = mode or 'incremental'
                where = [
                    "a.status = 'active'",
                    "a.url NOT LIKE 'ai://chat%'",
                ]
                params = []
                classification_join = ""
                if industry_pack_id:
                    classification_join = (
                        "JOIN article_intel_classifications c ON c.article_id=a.id"
                    )
                    where.extend([
                        "c.industry_pack_id = ?",
                        "COALESCE(json_array_length(json_extract(c.score_details_json, '$.hits.anchor')), 0) > 0",
                        "NOT EXISTS ("
                        "SELECT 1 FROM intel_evidence_group_articles ega "
                        "JOIN intel_evidence_groups eg ON eg.id=ega.evidence_group_id "
                        "WHERE ega.article_id=a.id "
                        "AND eg.industry_pack_id=c.industry_pack_id "
                        "AND eg.representative_article_id!=a.id)",
                    ])
                    params.append(str(industry_pack_id))
                    if activation_id:
                        where.append("c.activation_id = ?")
                        params.append(str(activation_id))
                if mode == 'incremental':
                    where.append("p.article_id IS NULL")
                elif mode == 'low-confidence':
                    where.append("p.article_id IS NOT NULL")
                    where.append("(COALESCE(p.time_confidence, 0) < ? OR COALESCE(p.location_confidence, 0) < ? OR p.spacetime_status != 'ready')")
                    params.extend([float(min_confidence or 0.4), float(min_confidence or 0.4)])
                elif mode == 'failed':
                    where.append("p.spacetime_status = 'failed'")
                elif mode == 'backfill':
                    pass
                else:
                    where.append("p.article_id IS NULL")

                params.append(coerce_int(limit, 500, 1, 10000))
                cursor.execute(
                    f"""
                    SELECT a.*
                    FROM articles a
                    LEFT JOIN article_spacetime_profiles p ON p.article_id = a.id
                    {classification_join}
                    WHERE {' AND '.join(where)}
                    ORDER BY a.updated_at DESC, a.id DESC
                    LIMIT ?
                    """,
                    params
                )
                rows = [dict(row) for row in cursor.fetchall()]
                for article in rows:
                    db._hydrate_matched_keywords_for_display(cursor, article)
                return rows
            finally:
                cursor.close()
    except Exception as e:
        print(f"❌ 获取待分析文章失败: {e}")
        return []


PROXY_METHODS = {
    "analyze_article_spacetime_profile": analyze_article_spacetime_profile,
    "save_article_spacetime_profile": save_article_spacetime_profile,
    "get_article_spacetime_profile": get_article_spacetime_profile,
    "get_article_spacetime_points": get_article_spacetime_points,
    "get_articles_for_spacetime_analysis": get_articles_for_spacetime_analysis,
}

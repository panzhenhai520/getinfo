"""Single-writer ingestion of remote pipeline results into local SQLite."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from smart_target_resolver import matched_keywords_in_text

# 第二层拦截阈值（可从环境变量调整）：精炼文与原始正文同时低于阈值，才判定为“无正文页”。
# 之所以要求“同时偏低”，是因为远端精炼文本身就是 200~500 字的摘要，
# 单看精炼文会把“精炼未跑完但原稿很长”的真文章（如深度分析/漏洞分析）误杀。
ARTICLE_MIN_REFINED_CHARS = int(os.getenv('INTEL_ARTICLE_MIN_REFINED_CHARS', '200') or 200)
ARTICLE_MIN_RAW_CHARS = int(os.getenv('INTEL_ARTICLE_MIN_RAW_CHARS', '100') or 100)


def ensure_remote_pipeline_schema(db) -> None:
    db._ensure_connection()
    with db.lock:
        db.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS article_derivatives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER NOT NULL,
                source_hash TEXT NOT NULL,
                source_language TEXT NOT NULL,
                target_language TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                translated_title TEXT NOT NULL DEFAULT '',
                translated_content TEXT NOT NULL DEFAULT '',
                model_id TEXT NOT NULL DEFAULT '',
                prompt_version TEXT NOT NULL DEFAULT '',
                remote_job_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'completed',
                error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(article_id, source_hash, target_language, model_id),
                FOREIGN KEY(article_id) REFERENCES articles(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS article_audio_manifests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER NOT NULL,
                remote_job_id TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'completed',
                error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(article_id, remote_job_id),
                FOREIGN KEY(article_id) REFERENCES articles(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS remote_pipeline_jobs (
                remote_job_id TEXT PRIMARY KEY,
                source_url TEXT NOT NULL,
                channel TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS article_raw_staging (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                canonical_url TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                raw_content TEXT NOT NULL DEFAULT '',
                source_method TEXT NOT NULL DEFAULT '',
                source_task_id TEXT NOT NULL DEFAULT '',
                source_task_name TEXT NOT NULL DEFAULT '',
                industry_pack_id TEXT NOT NULL DEFAULT '',
                matched_keywords TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'staging' CHECK (status IN ('staging','enriching','done','failed')),
                error TEXT NOT NULL DEFAULT '',
                article_id INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_raw_staging_status ON article_raw_staging(status);
            CREATE INDEX IF NOT EXISTS idx_raw_staging_created ON article_raw_staging(created_at);
            """
        )
        # 分层提炼元数据列（content_type/relevance/core_facts），老库 ALTER 追加
        try:
            db.connection.execute(
                "ALTER TABLE article_derivatives ADD COLUMN refine_meta_json TEXT NOT NULL DEFAULT ''"
            )
        except Exception:
            pass  # 列已存在或方言不支持，忽略


def purge_expired_staging(db, *, days: int = 3) -> int:
    """删除超过 days 天仍未完成的临时库原文（3天自动清理，避免累积）。"""
    db._ensure_connection()
    with db.lock:
        cur = db.connection.cursor()
        try:
            cur.execute(
                "DELETE FROM article_raw_staging "
                "WHERE status <> 'done' AND created_at < datetime('now', ?) ",
                (f'-{int(days)} days',),
            )
            db.connection.commit()
            return cur.rowcount
        finally:
            cur.close()


def _write_article_derivative(db, article_id: int, article: dict[str, Any], job_id: str) -> None:
    """Persist refined/translation/audio for one already-inserted article.

    新流程：展示内容为二次加工后(精炼)文章，翻译与音频都针对精炼文。
    """
    if not article_id:
        return
    ensure_remote_pipeline_schema(db)
    refined = str(article.get('refined_content') or '').strip()
    summary = refined or str(article.get('summary') or '').strip()
    derivative = article.get('derivative') or {}
    manifest = article.get('audio_manifest') or {}
    translated_content = str(article.get('translated_content') or derivative.get('translated_content') or '').strip()
    translated_title = str(article.get('refined_title') or article.get('translated_title') or derivative.get('translated_title') or '').strip()
    source_language = str(article.get('source_language') or derivative.get('source_language') or 'zh').strip()
    target_language = str(article.get('target_language') or derivative.get('target_language') or ('en' if source_language == 'zh' else 'zh')).strip()
    model_id = str(article.get('refine_model') or derivative.get('model') or '')
    prompt_version = str(article.get('refine_prompt_version') or derivative.get('prompt_version') or 'collectinfo-refine-v2')
    source_hash = str(article.get('source_hash') or derivative.get('source_hash') or hashlib.sha256(refined.encode('utf-8')).hexdigest())
    # 分层提炼元数据：content_type(A|B|C)、relevance(high|low|none)、core_facts
    refine_meta = article.get('refine_meta') or {}
    if not isinstance(refine_meta, dict):
        refine_meta = {}
    refine_meta_json = json.dumps(refine_meta, ensure_ascii=False) if refine_meta else ''
    flattened_manifest = []
    if isinstance(manifest, dict):
        # 新版 audio_manifest = {"refined": [...]}; 旧版为 list
        for key in ('refined', 'summary', 'translation'):
            items = manifest.get(key)
            if isinstance(items, list):
                flattened_manifest.extend(items)
        if not flattened_manifest:
            for v in manifest.values():
                if isinstance(v, list):
                    flattened_manifest.extend(v)
    elif isinstance(manifest, list):
        flattened_manifest = manifest
    with db.lock:
        db.connection.execute(
            """INSERT INTO article_derivatives(
            article_id,source_hash,source_language,target_language,summary,
            translated_title,translated_content,model_id,prompt_version,
            remote_job_id,status,error,refine_meta_json,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
            ON CONFLICT(article_id,source_hash,target_language,model_id) DO UPDATE SET
            summary=excluded.summary,translated_title=excluded.translated_title,
            translated_content=excluded.translated_content,prompt_version=excluded.prompt_version,
            remote_job_id=excluded.remote_job_id,status=excluded.status,error=excluded.error,
            refine_meta_json=excluded.refine_meta_json,
            updated_at=datetime('now')""",
            (
                article_id, source_hash, source_language, target_language,
                summary, translated_title, translated_content, model_id, prompt_version, job_id,
                'failed' if article.get('refine_error') or article.get('translation_error') else 'completed',
                str(article.get('refine_error') or article.get('translation_error') or ''),
                refine_meta_json,
            ),
        )
        # TTS 总闸关闭时 audio_manifest 为空：不再写空音频清单行（不留任何 TTS 痕迹）
        if flattened_manifest:
            db.connection.execute(
                """INSERT INTO article_audio_manifests(article_id,remote_job_id,manifest_json,status,error,updated_at)
                VALUES(?,?,?,?,?,datetime('now'))
                ON CONFLICT(article_id,remote_job_id) DO UPDATE SET manifest_json=excluded.manifest_json,
                status=excluded.status,error=excluded.error,updated_at=datetime('now')""",
                (article_id, job_id, json.dumps(flattened_manifest, ensure_ascii=False),
                 'failed' if article.get('audio_error') else 'completed', str(article.get('audio_error') or '')),
            )


def attach_remote_result_to_article(db, article_id: int, result: dict[str, Any]) -> bool:
    """Attach a remote job's derivative/audio to an existing local article."""
    if not article_id or not isinstance(result, dict):
        return False
    job_id = str(result.get('job_id') or '')
    articles = result.get('articles') or []
    if not articles:
        return False
    target = articles[0]
    if target.get('derivative') or target.get('audio_manifest') or target.get('derivative_error') or target.get('audio_error'):
        _write_article_derivative(db, article_id, target, job_id)
        return True
    return False


def ingest_remote_result(db, result: dict[str, Any], *, configured_url: str, keywords: list[str], task_id: str = '', task_name: str = '') -> dict[str, Any]:
    ensure_remote_pipeline_schema(db)
    job_id = str(result.get('job_id') or '')
    saved = []
    for article in result.get('articles') or []:
        title = str(article.get('refined_title') or article.get('title') or '').strip()
        raw = str(article.get('raw_content') or article.get('content') or '').strip()
        refined = str(article.get('refined_content') or '').strip()
        # 展示内容 = 二次加工后(精炼)文章；无精炼时退回原稿
        content = refined or raw
        # 新增：栏目/列表页或空洞内容 → 跳过不入库，避免把栏目/聚合页误当文章
        try:
            from content_handlers import _looks_like_listing_or_contentless
            if _looks_like_listing_or_contentless(content, title):
                continue
        except Exception:
            pass
        # 第二层：LLM 精炼文为空判定 → 精炼文没产出可展示正文，且原稿本身也极短，
        # 说明该页根本没有正文（网站维护通知 / 页面不存在 / 机构概况等空心页），跳过不入库。
        # 精炼文为空但原稿很长时不在此拦（多为精炼未跑完），仍以 processing 入库等待重跑，避免误删真文章。
        if len(refined) < ARTICLE_MIN_REFINED_CHARS and len(raw) < ARTICLE_MIN_RAW_CHARS:
            continue
        matched = matched_keywords_in_text(f'{title} {raw} {refined}', keywords)
        display_status = article.get('display_status') or ('ready' if refined else 'processing')
        article_data = {
            **article,
            'title': title or article.get('url') or 'Untitled',
            'content': content,
            'matched_keywords': matched,
            'configured_url': configured_url,
            'source_task_id': task_id,
            'source_task_name': task_name,
            'remote_job_id': job_id,
        }
        article_id = db.insert_article(article_data)
        if not article_id:
            continue
        # 记录原稿(未展示用)与展示状态：精炼后 active(可见)，否则 processing(不展示)
        with db.lock:
            db.connection.execute(
                "UPDATE articles SET raw_content=?, status=? WHERE id=?",
                (raw or None, 'active' if display_status == 'ready' else 'processing', int(article_id)),
            )
            db.connection.commit()
        if task_id:
            db.link_article_to_task(article_id, task_id)
        _write_article_derivative(db, article_id, article, job_id)
        saved.append(article_id)
    # 批内去重机制：本批同源近似文章（高相似度）合并保留一篇，避免同源重复入库
    try:
        from intel_api import dedupe_article_batch
        dedupe_article_batch(saved)
    except Exception:
        pass
    with db.lock:
        db.connection.execute(
            """INSERT INTO remote_pipeline_jobs(remote_job_id,source_url,channel,status,result_json,updated_at)
            VALUES(?,?,?,?,?,datetime('now'))
            ON CONFLICT(remote_job_id) DO UPDATE SET status=excluded.status,result_json=excluded.result_json,updated_at=datetime('now')""",
            (job_id, configured_url, 'remote_crawl4ai', str(result.get('status') or 'completed'), json.dumps(result, ensure_ascii=False)),
        )
    return {'success': bool(saved), 'articles': saved, 'articles_found': len(saved), 'remote_job_id': job_id, 'channel': 'remote_crawl4ai'}


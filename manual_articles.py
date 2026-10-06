#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手动发文：admin 在行业管理里直接录入文章（富文本/图文混排），发布后直入 articles。

发布语义：
- 正文按编辑者录入的 HTML 原样保存（content/content_markdown/raw_content 均为 HTML），
  **不送 LLM 精炼清洗**、不跑自动分类——编辑者已定好行业、主题/领域、标签与关键词。
- 写入 article_intel_classifications 关联行业包与主题标签，卡片/详情/趋势复用现有管线。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from sqlite_database import sqlite_db

_UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "uploads", "editor")
_ALLOWED_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")


def ensure_manual_articles_table(db=None) -> None:
    conn = db or sqlite_db
    conn._ensure_connection()
    with conn.lock:
        cur = conn.connection.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS manual_articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                industry_pack_id TEXT NOT NULL DEFAULT '',
                topic_key TEXT NOT NULL DEFAULT '',
                topic_name TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                tags_json TEXT NOT NULL DEFAULT '[]',
                keywords_json TEXT NOT NULL DEFAULT '[]',
                trend_words_json TEXT NOT NULL DEFAULT '[]',
                content_html TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                article_id INTEGER,
                pinned INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.connection.commit()
        cur.close()


def _ensure_manual_articles_columns(db) -> None:
    """补列（幂等）：pinned（置顶标记）。SQLite/PG 通用。"""
    with db.lock:
        cur = db.connection.cursor()
        try:
            cur.execute("PRAGMA table_info(manual_articles)")
            columns = {row["name"] if hasattr(row, "keys") else row[1] for row in cur.fetchall()}
        except Exception:
            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name=?",
                ("manual_articles",),
            )
            columns = {row["column_name"] for row in cur.fetchall()}
        if "pinned" not in columns:
            cur.execute("ALTER TABLE manual_articles ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
            try:
                db.connection.commit()
            except Exception:
                pass
        cur.close()


def _now_str() -> str:
    from datetime import datetime
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _list_field(value) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in re.split(r'[,，;；\n]', value) if item.strip()]
    return []


def save_manual_article(db, data: Dict, *, draft_id: Optional[int] = None, created_by: str = "") -> Dict:
    """保存草稿（新建或更新），返回 {id, ...}。"""
    ensure_manual_articles_table(db)
    _ensure_manual_articles_columns(db)
    now = _now_str()
    fields = {
        "industry_pack_id": str(data.get("industry_pack_id") or "").strip()[:80],
        "topic_key": str(data.get("topic_key") or "").strip()[:80],
        "topic_name": str(data.get("topic_name") or "").strip()[:120],
        "url": str(data.get("url") or "").strip()[:2000],
        "title": str(data.get("title") or "").strip()[:500],
        "tags_json": json.dumps(_list_field(data.get("tags")), ensure_ascii=False),
        "keywords_json": json.dumps(_list_field(data.get("keywords")), ensure_ascii=False),
        "trend_words_json": json.dumps(_list_field(data.get("trend_words")), ensure_ascii=False),
        "content_html": str(data.get("content_html") or ""),
        "pinned": 1 if bool(data.get("pinned")) else 0,
    }
    if not fields["title"]:
        raise ValueError("标题不能为空")
    if not fields["industry_pack_id"]:
        raise ValueError("请选择行业")
    with db.lock:
        cur = db.connection.cursor()
        if draft_id:
            cur.execute(
                """UPDATE manual_articles SET industry_pack_id=?, topic_key=?, topic_name=?, url=?, title=?,
                   tags_json=?, keywords_json=?, trend_words_json=?, content_html=?, pinned=?, updated_at=?
                   WHERE id=? AND status='draft'""",
                (*fields.values(), now, int(draft_id)),
            )
            if not cur.rowcount:
                cur.close()
                raise ValueError("草稿不存在或已发布")
            new_id = int(draft_id)
        else:
            cur.execute(
                """INSERT INTO manual_articles(industry_pack_id, topic_key, topic_name, url, title,
                   tags_json, keywords_json, trend_words_json, content_html, pinned, status, created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?, 'draft', ?, ?, ?)""",
                (*fields.values(), str(created_by or "")[:80], now, now),
            )
            new_id = int(cur.lastrowid)
        db.connection.commit()
        cur.close()
    return get_manual_article(db, new_id)


def get_manual_article(db, draft_id: int) -> Optional[Dict]:
    ensure_manual_articles_table(db)
    with db.lock:
        cur = db.connection.cursor()
        cur.execute("SELECT * FROM manual_articles WHERE id=?", (int(draft_id),))
        row = cur.fetchone()
        cur.close()
    if not row:
        return None
    d = dict(row)
    d["tags"] = json.loads(d.get("tags_json") or "[]")
    d["keywords"] = json.loads(d.get("keywords_json") or "[]")
    d["trend_words"] = json.loads(d.get("trend_words_json") or "[]")
    return d


def list_manual_articles(db, *, industry_pack_id: str = "", status: str = "") -> List[Dict]:
    ensure_manual_articles_table(db)
    where, params = [], []
    if industry_pack_id:
        where.append("industry_pack_id=?")
        params.append(str(industry_pack_id))
    if status in ("draft", "published"):
        where.append("status=?")
        params.append(status)
    sql = "SELECT * FROM manual_articles" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT 200"
    with db.lock:
        cur = db.connection.cursor()
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
    for d in rows:
        d["tags"] = json.loads(d.get("tags_json") or "[]")
        d["keywords"] = json.loads(d.get("keywords_json") or "[]")
        d["trend_words"] = json.loads(d.get("trend_words_json") or "[]")
    return rows


def delete_manual_article(db, draft_id: int) -> bool:
    ensure_manual_articles_table(db)
    with db.lock:
        cur = db.connection.cursor()
        cur.execute("DELETE FROM manual_articles WHERE id=? AND status='draft'", (int(draft_id),))
        deleted = bool(cur.rowcount)
        db.connection.commit()
        cur.close()
    return deleted


def publish_manual_article(db, draft_id: int, *, created_by: str = "") -> Dict:
    """发布：草稿原文写入 articles + 行业/主题分类，不送 LLM 管线。"""
    draft = get_manual_article(db, draft_id)
    if not draft:
        raise ValueError("草稿不存在")
    if draft.get("status") == "published":
        raise ValueError("该草稿已发布")
    content = str(draft.get("content_html") or "").strip()
    if not content:
        raise ValueError("正文不能为空")

    title = str(draft.get("title") or "").strip()
    url = str(draft.get("url") or "").strip() or f"manual://{draft_id}"
    # 编辑者常从浏览器直接复制链接，可能带上界面语言参数（如 x.com/…?lang=zh），
    # 与爬虫入库闸门保持一致：这里也剥掉，避免同一文章因语言参数重复。
    from utils import strip_url_presentation_params
    url = strip_url_presentation_params(url)
    domain = (urlparse(url).hostname or "manual").casefold()
    now = _now_str()
    today = now[:10]
    content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()
    keywords = draft.get("keywords") or []
    tags = draft.get("tags") or []
    trend_words = draft.get("trend_words") or []

    pack = load_manual_pack(draft.get("industry_pack_id"))
    pack_version = str(pack.get("pack_version") or "")

    # 卡片/动态的行业关键词门禁（INDUSTRY_KEYWORD_GATE_SQL）要求
    # score_details_json 带 hits.core/expanded/anchor 命中数组：
    # 按编辑者录入的标题+正文对行业包关键词做真实匹配，手动文章也必须写齐，
    # 否则发布成功但首页卡片/实时动态查不到。
    from intel_candidates import quick_score_candidate
    _manual_score = quick_score_candidate(title, content, pack)
    _manual_hits = {
        "core": [
            m["keyword"] for m in _manual_score.get("matched_keywords") or []
            if m.get("group") == "core_keywords"
        ],
        "expanded": [
            m["keyword"] for m in _manual_score.get("matched_keywords") or []
            if m.get("group") == "expanded_keywords"
        ],
        "anchor": list(_manual_score.get("anchor_hits") or []),
    }
    _manual_score_details = {
        "components": {},
        "manual": True,
        "score": _manual_score.get("score"),
        "hits": _manual_hits,
    }

    # 统一入库收口：所有入库路径都必须走 ingest_article → insert_article，禁止再直接
    # INSERT INTO articles。这样「关键词必中 / 框架页判废 / 未来日期校验 / 归属兜底」
    # 这几道最终闸门对手动发文同样生效；skip_pipeline=True 表示不送 LLM 精炼、
    # 不跑自动分类（编辑者已定好标签/主题），但入库闸门与归属兜底仍然执行。
    # 把编辑者选的包显式传下去，归属兜底就落在同一个包上，下面的分类 upsert 可直接覆盖它。
    from article_ingest import ingest_article

    article_id = ingest_article({
        "url": url,
        "title": title,
        "content": content,
        "domain": domain,
        "publish_date": today,
        "content_hash": content_hash,
        "matched_keywords": ",".join(keywords),
        "matched_keywords_raw": ",".join(keywords),
        "keyword_match_detail": json.dumps(keywords, ensure_ascii=False),
        "extraction_method": "manual",
        "quality_score": 100,
        "crawler_engine_used": "manual",
        "crawler_engines": "manual",
        "crawler_attempts": 1,
        "fallback_trigger_reason": "",
        "source_method": "manual",
        "source_task_id": "",
        "source_task_name": "manual-article",
        "configured_url": "",
        "resolved_target_url": "",
        "canonical_url": url,
        "published_time_source": "manual",
        "published_precision": "day",
        "raw_content": content,
        "content_markdown": content,
        "industry_pack_id": str(draft.get("industry_pack_id") or ""),
    }, source_kind="manual_article", db=db, skip_pipeline=True)
    if not article_id:
        raise ValueError("文章未通过入库闸门（无关键词命中 / 页面框架 / 日期不可信），已拒绝入库")
    with db.lock:
        cur = db.connection.cursor()
        # 行业包 + 主题标签关联（不跑自动分类）
        topic_tags = list(dict.fromkeys([str(draft.get("topic_name") or "").strip()] + tags))
        topic_tags = [t for t in topic_tags if t][:8]
        # 用 upsert：insert_article 的归属兜底已经为该包写过一条（final_category='other'），
        # 这里用编辑者指定的真实分类/主题覆盖它，而不是插入第二条（唯一索引会冲突）。
        cur.execute(
            """
            INSERT INTO article_intel_classifications (
                article_id, industry_pack_id, activation_id, industry_pack_version,
                classifier_version, article_content_hash, rule_category, rule_confidence,
                rule_reason, score_details_json, matched_keywords_json,
                llm_reason, why_important, trend_summary, topic_tags_json,
                final_category, final_confidence, final_reason, result_source,
                fusion_version, llm_model_id, llm_prompt_version, llm_error,
                classified_at, created_at, updated_at
            ) VALUES (?, ?, '', ?, 'manual-v1', ?, 'event', 1.0, ?, ?, ?, '',
                      '', ?, ?, 'event', 1.0, '手动发文（编辑者指定主题）', 'manual',
                      'manual-v1', '', '', '', datetime('now'), datetime('now'), datetime('now'))
            ON CONFLICT (article_id, industry_pack_id) DO UPDATE SET
                industry_pack_version = excluded.industry_pack_version,
                classifier_version = excluded.classifier_version,
                article_content_hash = excluded.article_content_hash,
                rule_category = excluded.rule_category,
                rule_confidence = excluded.rule_confidence,
                rule_reason = excluded.rule_reason,
                score_details_json = excluded.score_details_json,
                matched_keywords_json = excluded.matched_keywords_json,
                trend_summary = excluded.trend_summary,
                topic_tags_json = excluded.topic_tags_json,
                final_category = excluded.final_category,
                final_confidence = excluded.final_confidence,
                final_reason = excluded.final_reason,
                result_source = excluded.result_source,
                classified_at = excluded.classified_at,
                updated_at = excluded.updated_at
            """,
            (
                article_id, draft.get("industry_pack_id"), pack_version, content_hash,
                "手动发文", json.dumps(_manual_score_details, ensure_ascii=False),
                json.dumps(keywords, ensure_ascii=False),
                ",".join(trend_words), json.dumps(topic_tags, ensure_ascii=False),
            ),
        )
        # 实时动态（事件簇）来自 intel_article_events；手动文章不跑 LLM 事件抽取，
        # 这里以编辑者指定的主题直接落一条事件记录，保证实时动态立即可见。
        _event_subject = (str(draft.get("topic_name") or "").strip() or title)[:120]
        cur.execute(
            """
            INSERT INTO intel_article_events (
                article_id, event_index, industry_pack_id, subject, action, object,
                entities_json, event_time, event_type, subject_type, event_hash,
                content_hash, llm_model_id, created_at, updated_at
            ) VALUES (?, 0, ?, ?, ?, ?, '[]', ?, 'other', 'topic', ?, ?, 'manual',
                      datetime('now'), datetime('now'))
            """,
            (
                article_id, draft.get("industry_pack_id"),
                _event_subject, "发布", title[:120],
                today, f"manual-{article_id}", content_hash,
            ),
        )
        # 主题卡关联（intel_topic_articles）：编辑者选定主题 → 以 manual 方式关联，
        # topic_cluster 重跑不会清除 manual 关联；pinned=1 时在该主题排第 1 位。
        _pinned = 1 if bool(draft.get("pinned")) else 0
        _topic_key = str(draft.get("topic_key") or "").strip()[:80]
        if _topic_key:
            cur.execute(
                "SELECT id FROM intel_topics WHERE industry_pack_id=? AND topic_key=?",
                (draft.get("industry_pack_id"), _topic_key),
            )
            _trow = cur.fetchone()
            if _trow:
                cur.execute(
                    """
                    INSERT INTO intel_topic_articles (
                        topic_id, article_id, association_score, assignment_method,
                        evidence_json, assigned_at, updated_at, pinned
                    ) VALUES (?, ?, 1.0, 'manual', ?, datetime('now'), datetime('now'), ?)
                    ON CONFLICT(topic_id, article_id) DO UPDATE SET
                        association_score=1.0, assignment_method='manual',
                        pinned=excluded.pinned, updated_at=datetime('now')
                    """,
                    (int(_trow["id"]), article_id,
                     json.dumps({"manual": True, "topic_key": _topic_key}, ensure_ascii=False),
                     _pinned),
                )
        cur.execute(
            "UPDATE manual_articles SET status='published', article_id=?, published_at=?, updated_at=? WHERE id=?",
            (article_id, now, now, int(draft_id)),
        )
        db.connection.commit()
        cur.close()

    db.analyze_article_spacetime_profile(article_id)
    return {"draft_id": int(draft_id), "article_id": article_id, "title": title}


def edit_source_for_article(db, article_id: int) -> Optional[Dict]:
    """编辑已发布文章用的预填数据：文章字段 + 分类记录（主题/标签/关键词）。"""
    ensure_manual_articles_table(db)
    with db.lock:
        cur = db.connection.cursor()
        cur.execute("SELECT * FROM articles WHERE id=?", (int(article_id),))
        row = cur.fetchone()
        if not row:
            cur.close()
            return None
        art = dict(row)
        cur.execute(
            "SELECT industry_pack_id, topic_tags_json, matched_keywords_json "
            "FROM article_intel_classifications WHERE article_id=? "
            "ORDER BY classified_at DESC, id DESC LIMIT 1",
            (int(article_id),),
        )
        cls = cur.fetchone()
        # 主题卡关联（manual）：取 topic_key 与置顶标记供编辑窗口预填
        cur.execute(
            """SELECT t.topic_key, ta.pinned
               FROM intel_topic_articles ta
               JOIN intel_topics t ON t.id=ta.topic_id
               WHERE ta.article_id=? AND ta.assignment_method='manual'
               ORDER BY ta.id LIMIT 1""",
            (int(article_id),),
        )
        _link = cur.fetchone()
        cur.close()
    cls = dict(cls) if cls else {}
    _link = dict(_link) if _link else {}
    tags = []
    try:
        tags = [str(t).strip() for t in (json.loads(cls.get("topic_tags_json") or "[]") or []) if str(t).strip()]
    except Exception:
        tags = []
    keywords = _list_field(cls.get("matched_keywords_json") or "")
    pack_id = str(cls.get("industry_pack_id") or "").strip()
    # 主题标签的第一项即主题名（发布时 topic_name 置于 tags 首位）
    topic_name = tags[0] if tags else ""
    return {
        "article_id": int(article_id),
        "industry_pack_id": pack_id,
        "topic_key": str(_link.get("topic_key") or ""),
        "topic_name": topic_name,
        "url": str(art.get("url") or "").strip(),
        "title": str(art.get("title") or "").strip(),
        "tags": tags[1:] if topic_name else tags,
        "keywords": keywords,
        "trend_words": [],
        "content_html": str(art.get("content") or ""),
        "pinned": int(_link.get("pinned") or 0),
    }


def update_published_article(db, article_id: int, data: Dict) -> Dict:
    """编辑并保存已发布文章（admin）：更新 articles 正文/标题，同步分类主题与关键词。

    不送 LLM 管线；正文按编辑者 HTML 原样保存（与手动发布语义一致）。
    """
    ensure_manual_articles_table(db)
    fields = {
        "title": str(data.get("title") or "").strip()[:500],
        "url": str(data.get("url") or "").strip()[:2000],
        "content_html": str(data.get("content_html") or ""),
        "industry_pack_id": str(data.get("industry_pack_id") or "").strip()[:80],
        "topic_key": str(data.get("topic_key") or "").strip()[:80],
        "topic_name": str(data.get("topic_name") or "").strip()[:120],
        "tags": _list_field(data.get("tags")),
        "keywords": _list_field(data.get("keywords")),
        "trend_words": _list_field(data.get("trend_words")),
        "pinned": 1 if bool(data.get("pinned")) else 0,
    }
    if not fields["title"]:
        raise ValueError("标题不能为空")
    if not fields["content_html"].strip():
        raise ValueError("正文不能为空")
    content = fields["content_html"]
    content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()
    with db.lock:
        cur = db.connection.cursor()
        cur.execute(
            "SELECT id, url FROM articles WHERE id=? AND status='active'",
            (int(article_id),),
        )
        _art_row = cur.fetchone()
        if not _art_row:
            cur.close()
            raise ValueError("文章不存在或已下线")
        # URL 为空时保留原值：articles.url 有唯一约束，清空会撞上其它空 URL 记录
        if not fields["url"]:
            fields["url"] = str(_art_row["url"] or "")[:2000]
        cur.execute(
            """UPDATE articles SET title=?, url=?, content=?, content_markdown=?, raw_content=?,
               content_length=?, content_hash=?, updated_at=datetime('now', 'localtime')
               WHERE id=?""",
            (fields["title"], fields["url"], content, content, content,
             len(content), content_hash, int(article_id)),
        )
        # 同步分类记录：主题标签（topic_name 置首）+ 关键词（不重跑自动分类）
        topic_tags = list(dict.fromkeys(
            [t for t in [fields["topic_name"]] + fields["tags"] if t]
        ))[:8]
        cur.execute(
            "SELECT id, industry_pack_id, topic_tags_json FROM article_intel_classifications WHERE article_id=? "
            "ORDER BY classified_at DESC, id DESC LIMIT 1",
            (int(article_id),),
        )
        cls_row = cur.fetchone()
        if cls_row:
            # 编辑表单未选行业/主题时保留原值，避免把分类记录清空
            final_pack = fields["industry_pack_id"] or str(cls_row["industry_pack_id"] or "").strip()
            if not topic_tags and str(cls_row["topic_tags_json"] or "").strip() not in ("", "[]"):
                try:
                    topic_tags = [str(t).strip() for t in (json.loads(cls_row["topic_tags_json"]) or []) if str(t).strip()][:8]
                except Exception:
                    topic_tags = []
            cur.execute(
                """UPDATE article_intel_classifications
                   SET industry_pack_id=?, topic_tags_json=?, matched_keywords_json=?,
                       article_content_hash=?, updated_at=datetime('now', 'localtime')
                   WHERE id=?""",
                (
                    final_pack,
                    json.dumps(topic_tags, ensure_ascii=False),
                    json.dumps(fields["keywords"], ensure_ascii=False),
                    content_hash,
                    int(cls_row["id"]),
                ),
            )
        # 主题卡关联（intel_topic_articles）：编辑时同步主题归属与置顶标记。
        # 未选主题时保留现有 manual 关联（不动）；选定主题则 upsert。
        if fields["topic_key"]:
            cur.execute(
                "SELECT id FROM intel_topics WHERE industry_pack_id=? AND topic_key=?",
                (final_pack or fields["industry_pack_id"], fields["topic_key"]),
            )
            _trow = cur.fetchone()
            if _trow:
                cur.execute(
                    """
                    INSERT INTO intel_topic_articles (
                        topic_id, article_id, association_score, assignment_method,
                        evidence_json, assigned_at, updated_at, pinned
                    ) VALUES (?, ?, 1.0, 'manual', ?, datetime('now'), datetime('now'), ?)
                    ON CONFLICT(topic_id, article_id) DO UPDATE SET
                        association_score=1.0, assignment_method='manual',
                        pinned=excluded.pinned, updated_at=datetime('now')
                    """,
                    (int(_trow["id"]), int(article_id),
                     json.dumps({"manual": True, "topic_key": fields["topic_key"]}, ensure_ascii=False),
                     fields["pinned"]),
                )
        else:
            # 未改主题：无论置顶还是取消置顶，都同步该文章已有 manual 关联的 pinned
            cur.execute(
                "UPDATE intel_topic_articles SET pinned=?, updated_at=datetime('now','localtime') "
                "WHERE article_id=? AND assignment_method='manual'",
                (fields["pinned"], int(article_id)),
            )
        db.connection.commit()
        cur.close()
    return {"article_id": int(article_id), "title": fields["title"]}


def save_editor_image(db, file_storage) -> str:
    """编辑器图片上传：保存到 static/uploads/editor/，返回相对 URL。"""
    ext = ""
    if file_storage and file_storage.filename:
        ext = os.path.splitext(str(file_storage.filename))[1].lower()
    ext = ext if ext in _ALLOWED_EXT else ".png"
    os.makedirs(_UPLOAD_DIR, exist_ok=True)
    import uuid
    filename = f"editor_{uuid.uuid4().hex[:12]}{ext}"
    file_storage.save(os.path.join(_UPLOAD_DIR, filename))
    return f"/static/uploads/editor/{filename}"


def load_manual_pack(industry_pack_id: str) -> Dict:
    """取该行业包的运行时定义（已发布版本优先，回退安装种子文件）。

    为什么不能只用 use_published=False：embodied_ai / energy_news / climate_news 这类包
    只存在于已发布版本表里、仓库没有种子 JSON，写死读种子文件会让手动发文直接报
    "industry pack not found"（实测本地即如此）。
    """
    from industry_packs import industry_pack_loader

    pack_id = str(industry_pack_id or "").strip()
    try:
        return industry_pack_loader.load(pack_id) or {}
    except Exception:
        return industry_pack_loader.load(pack_id, use_published=False) or {}


def manual_topics_for_pack(industry_pack_id: str) -> List[Dict]:
    """该行业包可选的主题/领域（fixed_topics）+ 仪表盘分类。

    fixed_topics 兼容两种形态：字符串列表（如医疗包 ['智慧医院', ...]）与字典列表
    （{key,name}），避免字符串被当 dict 取 .get() 报错。
    """
    pack = load_manual_pack(industry_pack_id)
    topics = []
    for t in (pack.get("fixed_topics") or []):
        if isinstance(t, dict):
            key = str(t.get("key") or "").strip()
            name = str(t.get("name") or "").strip() or key
            if key:
                topics.append({"key": key, "name": name, "kind": "fixed_topic"})
        else:
            name = str(t).strip()
            if name:
                topics.append({"key": name, "name": name, "kind": "fixed_topic"})
    for cat in pack.get("dashboard_categories") or []:
        if isinstance(cat, dict) and cat.get("key"):
            topics.append({"key": cat.get("key"), "name": cat.get("name") or cat.get("key"), "kind": "dashboard_category"})
    return topics

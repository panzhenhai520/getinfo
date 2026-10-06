#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行业包归属兜底：保证每篇入库文章至少有一条行业包归属。

为什么需要：展示链路「最近关注」走 intel_repository.list_dashboard_recent_articles()，
它只按项目关键词筛、**不接收 industry_pack_id**，所以没有包归属的文章照样出现在
包页面上；而问答检索 ArticleRetriever._rows() 要求 article_intel_classifications
有本包记录 → 同一篇文章"页面看得到、AI 搜不到"（实测 id=6910 就是这样被漏掉的）。

分类任务（_enqueue_intel_classification）是异步的，且行业包关键词门禁不达标时
**不会写分类行**，所以入库时必须先落一条兜底归属，异步分类之后可以把它升级成
真实分类。兜底归属一律归到该包的「其他」分类（final_category='other'）。

归属目标包的确定顺序：
  1. article_data 里显式给的 industry_pack_ids / industry_pack_id
  2. 按各行业包关键词表打分，命中最高且达到门槛的包
  3. 兜底用当前激活包（active_industry_pack_id）
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

_FALLBACK_SOURCE = "fallback_attribution"
_FALLBACK_CLASSIFIER_VERSION = "fallback-attribution-v1"
_MIN_KEYWORD_HITS = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _explicit_packs(article_data: dict) -> list:
    out = []
    raw = article_data.get("industry_pack_ids")
    if isinstance(raw, (list, tuple)):
        out.extend(str(item).strip() for item in raw if str(item or "").strip())
    single = str(article_data.get("industry_pack_id") or "").strip()
    if single:
        out.append(single)
    return [p for p in dict.fromkeys(out) if p]


def _active_pack(db) -> str:
    try:
        with db.lock:
            row = db.connection.execute(
                "SELECT setting_value FROM intel_runtime_settings "
                "WHERE setting_key='active_industry_pack_id'"
            ).fetchone()
        return str((row[0] if row else "") or "").strip()
    except Exception:
        return ""


def _keyword_packs(article_data: dict) -> list:
    """按各行业包的核心/扩展关键词打分，返回 [(pack_id, 命中数, [命中词])] 降序。"""
    try:
        from industry_packs import industry_pack_loader
    except Exception:
        return []
    blob = " ".join(str(article_data.get(k) or "") for k in ("title", "matched_keywords"))
    blob = (blob + " " + str(article_data.get("content") or "")[:2000]).casefold()
    if not blob.strip():
        return []
    scored = []
    for pack in industry_pack_loader.list():
        pack_id = str(pack.get("id") or "")
        if not pack_id:
            continue
        words = [str(w) for w in (pack.get("core_keywords") or [])]
        words += [str(w) for w in (pack.get("expanded_keywords") or [])]
        hits = [w for w in words if w and w.casefold() in blob]
        if hits:
            scored.append((pack_id, len(hits), hits))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored


def _pack_version(pack_id: str) -> str:
    """取该包当前版本号：article_intel_classifications.industry_pack_version 是 NOT NULL。"""
    try:
        from industry_packs import industry_pack_loader

        pack = industry_pack_loader.load(pack_id)
        return str(pack.get("pack_version") or "")
    except Exception:
        return ""


def _content_hash(article_data: dict) -> str:
    """article_intel_classifications.article_content_hash 是 NOT NULL，口径与
    IntelRepository.article_content_hash 保持一致（优先复用已算好的 hash）。"""
    existing = str(article_data.get("content_hash") or "").strip()
    if existing:
        return existing
    content = str(article_data.get("content") or "")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _real_score_details(pack_id: str, article_data: dict) -> dict:
    """用真实分类器（同一套行业锚点/关键词打分）算出该包的 score_details。

    为什么不能写空 score_details：问答检索 ArticleRetriever._rows() 有一道质量门
    （intel_topics._classification_admitted），要求 score_details 里有 anchor / core /
    expanded 命中。兜底归属若写空值，文章虽然"有归属行"，AI 依然检索不到，
    "页面看得到、AI 搜不到"的问题只是从"没有行"变成"有空行"。
    这里复用 intel_classifier.classify_article，写进去的是真实命中证据，不是伪造分。
    """
    try:
        from industry_packs import industry_pack_loader
        from intel_classifier import classify_article

        pack = industry_pack_loader.load(pack_id)
        scored = classify_article(
            {
                "title": article_data.get("title") or "",
                "content": article_data.get("content") or "",
                "matched_keywords": article_data.get("matched_keywords") or "",
            },
            pack,
        )
        return scored or {}
    except Exception:
        return {}


def _topic_tags(pack_id: str, article_data: dict) -> list:
    """把文章归到该包设定主题/领域里最匹配的一个（命中即打标）。"""
    try:
        from industry_packs import industry_pack_loader

        pack = industry_pack_loader.load(pack_id)
    except Exception:
        return []
    blob = " ".join(str(article_data.get(k) or "") for k in ("title", "matched_keywords")
                    ).casefold()
    best, best_hits = "", 0
    for topic in (pack.get("fixed_topics") or []):
        words = [str(w) for w in (topic.get("keywords") or [])]
        hits = sum(1 for w in words if w and w.casefold() in blob)
        if hits > best_hits:
            best, best_hits = str(topic.get("name") or topic.get("key") or ""), hits
    return [best] if best else []


def ensure_pack_attribution(db, article_id: int, article_data: dict) -> dict:
    """保证文章至少有本包归属（异步分类之后可升级为真实分类）。

    返回 {attributed: [...], source: 'existing'|'explicit'|'keyword'|'active'|'none'}。
    """
    result = {"attributed": [], "source": "none"}
    try:
        db._ensure_connection()
        with db.lock:
            rows = db.connection.execute(
                "SELECT industry_pack_id FROM article_intel_classifications WHERE article_id=?",
                (int(article_id),),
            ).fetchall()
        existing = [str(r[0]) for r in rows if r and r[0]]
        if existing:
            result["attributed"] = existing
            result["source"] = "existing"
            return result

        packs, source = [], ""
        explicit = _explicit_packs(article_data)
        if explicit:
            packs, source = explicit[:3], "explicit"
        else:
            scored = _keyword_packs(article_data)
            strong = [p for p, n, _hits in scored if n >= _MIN_KEYWORD_HITS]
            if strong:
                packs, source = strong[:2], "keyword"
            else:
                active = _active_pack(db)
                if active:
                    packs, source = [active], "active"
        if not packs:
            return result

        now = _utc_now()
        content_hash = _content_hash(article_data)
        _raw_keywords = article_data.get("matched_keywords") or []
        keywords_json = (
            json.dumps(list(_raw_keywords), ensure_ascii=False)
            if isinstance(_raw_keywords, (list, tuple))
            else str(_raw_keywords)
        )
        # 真实打分放到锁外先算好：classify_article 是纯函数但耗 CPU，
        # 不要在持有写锁时跑全量关键词匹配。
        scored_by_pack = {pack_id: _real_score_details(pack_id, article_data) for pack_id in packs}
        with db.lock:
            cursor = db.connection.cursor()
            for pack_id in packs:
                scored = scored_by_pack.get(pack_id) or {}
                # 兜底行必须是真实的行业打分证据（检索质量门要用），分类一律先落「其他」，
                # 主题/命中词尽量用真实打分结果填齐。
                score_details = scored.get("score_details") or {}
                tags = list(scored.get("topic_tags") or []) or _topic_tags(pack_id, article_data)
                matched = scored.get("matched_keywords") or []
                if matched:
                    keywords_json = json.dumps(list(matched), ensure_ascii=False)
                confidence = float(scored.get("rule_confidence") or 0.0) or 0.3
                reason = {
                    "keyword": "关键词命中兜底归属（分类任务未产出归属，先归入「其他」分类）",
                    "explicit": "入库方显式指定行业包，先归入该包「其他」分类",
                    "active": "无关键词命中，归入当前激活包的「其他」分类",
                }.get(source, "兜底归属")
                try:
                    cursor.execute(
                        """
                        INSERT INTO article_intel_classifications
                            (article_id, industry_pack_id, activation_id,
                             industry_pack_version, classifier_version,
                             article_content_hash,
                             rule_category, rule_confidence, rule_reason,
                             score_details_json, matched_keywords_json,
                             topic_tags_json,
                             final_category, final_confidence, final_reason,
                             result_source, classified_at, created_at, updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT (article_id, industry_pack_id) DO NOTHING
                        """,
                        (
                            int(article_id), str(pack_id), "",
                            _pack_version(pack_id), _FALLBACK_CLASSIFIER_VERSION,
                            content_hash,
                            "other", confidence, reason,
                            json.dumps(score_details, ensure_ascii=False), keywords_json,
                            json.dumps(tags, ensure_ascii=False),
                            "other", confidence, reason,
                            _FALLBACK_SOURCE, now, now, now,
                        ),
                    )
                    result["attributed"].append(pack_id)
                except Exception as exc:
                    print("⚠️ 兜底归属写入失败 article=%s pack=%s: %s"
                          % (article_id, pack_id, str(exc)[:160]))
            db.connection.commit()
            cursor.close()
        result["source"] = source
    except Exception as exc:
        print("⚠️ 归属兜底异常 article=%s: %s" % (article_id, str(exc)[:120]))
    return result


__all__ = ["ensure_pack_attribution"]

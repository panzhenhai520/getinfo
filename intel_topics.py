#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Deterministic per-industry topic clustering and topic query service."""

from __future__ import annotations

import hashlib
import json
from typing import Dict, List, Optional, Tuple

import config
from industry_packs import IndustryPackLoader, industry_pack_loader, normalize_intel_text
from intel_classifier import (
    TOPIC_TAGGING_VERSION,
    _BID_TENDER_RE,
    _TITLE_ONLY_EXCLUDE,
    match_fixed_topics,
)
from intel_contracts import parse_time_range, utc_text
from intel_database import ARTICLE_TIME_SQL
from pack_attention import WATCH_ASSIGNMENT_METHOD
from sqlite_database import sqlite_db
from utils import coerce_int


TOPIC_CLUSTER_VERSION = "fixed-topic-cluster-v2"
TOPIC_SUMMARY_VERSION = "topic-rule-summary-v1"


def _json_value(value, default):
    try:
        return json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _classification_admitted(score_details: Dict) -> bool:
    # LLM 语义准入（产品变更 2026-10-07）：文章没命中行业锚点词，但 LLM 明确判定
    # "属于本行业包"并给出了达阈值的分类结论时，按真实分类看待、允许进 AI 证据池。
    # 标记由 intel_classifier.classify_article_id 写入（admission_source='llm_semantic'），
    # 只有"LLM 真的被调用且给了肯定判定"才带上，兜底归属永远不会带。
    if str(score_details.get("admission_source") or "") == "llm_semantic":
        return True
    hits = (score_details.get("hits") or {})
    anchor = hits.get("anchor") or []
    min_score = float(score_details.get("minimum_relevance_score") or 0)
    relevance = float(score_details.get("relevance_score") or 0)
    # 实体锚点命中且相关性达标 → 准入（原文行为）。
    if anchor and relevance >= min_score:
        return True
    # 与派发准入（quick_score_candidate 认扩展/核心词）对齐：命中扩展/核心关键词且
    # 其分数 ≥ 阈值一半，也视为行业相关，允许聚类（否则风电/新能源/变电站等靠扩展词
    # 命中的行业标会被聚类跳过、进不了 topic）。
    if hits.get("expanded") or hits.get("core"):
        comp = float((score_details.get("components") or {}).get("expanded") or 0) + float(
            (score_details.get("components") or {}).get("core") or 0
        )
        return comp >= max(0.5, min_score * 0.5)
    return False


class IntelTopicService:
    def __init__(
        self,
        *,
        database=None,
        pack_loader: IndustryPackLoader = None,
    ):
        self.db = database or sqlite_db
        self.pack_loader = pack_loader or industry_pack_loader

    def _ensure(self):
        self.db._ensure_connection()

    def cluster(self, industry_pack_id: str, *, manual: bool = False) -> Dict:
        if not config.INTEL_TOPIC_CLUSTER_ENABLED and not manual:
            return {"skipped": True, "reason": "automatic topic clustering disabled"}
        self._ensure()
        pack = self.pack_loader.load(industry_pack_id)
        now = utc_text()
        stats = {
            "industry_pack_id": industry_pack_id,
            "topics": 0,
            "articles": 0,
            "trend_articles": 0,
            "category_counts": {"trend": 0, "event": 0, "other": 0},
            "associations": 0,
            "summaries_updated": 0,
            "cluster_version": TOPIC_CLUSTER_VERSION,
        }
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                topic_rows = {}
                for fixed in pack.get("fixed_topics") or []:
                    cursor.execute(
                        """
                        INSERT INTO intel_topics (
                            industry_pack_id, topic_key, topic_name, topic_source,
                            keywords_json, last_clustered_at, created_at, updated_at
                        ) VALUES (?, ?, ?, 'fixed', ?, ?, ?, ?)
                        ON CONFLICT(industry_pack_id, topic_key) DO UPDATE SET
                            topic_name=excluded.topic_name,
                            keywords_json=excluded.keywords_json,
                            last_clustered_at=excluded.last_clustered_at,
                            updated_at=excluded.updated_at
                        """,
                        (
                            industry_pack_id,
                            fixed["key"],
                            fixed["name"],
                            json.dumps(fixed.get("keywords") or [], ensure_ascii=False),
                            now,
                            now,
                            now,
                        ),
                    )
                    cursor.execute(
                        """
                        SELECT id FROM intel_topics
                        WHERE industry_pack_id=? AND topic_key=?
                        """,
                        (industry_pack_id, fixed["key"]),
                    )
                    topic_rows[fixed["key"]] = {
                        **fixed,
                        "id": int(cursor.fetchone()["id"]),
                    }
                configured_keys = list(topic_rows)
                if configured_keys:
                    placeholders = ",".join("?" for _ in configured_keys)
                    cursor.execute(
                        f"""
                        DELETE FROM intel_topics
                        WHERE industry_pack_id=? AND topic_source='fixed'
                          AND topic_key NOT IN ({placeholders})
                        """,
                        [industry_pack_id, *configured_keys],
                    )
                else:
                    cursor.execute(
                        """
                        DELETE FROM intel_topics
                        WHERE industry_pack_id=? AND topic_source='fixed'
                        """,
                        (industry_pack_id,),
                    )
                stats["topics"] = len(topic_rows)
                if topic_rows:
                    placeholders = ",".join("?" for _ in topic_rows)
                    cursor.execute(
                        f"""
                        DELETE FROM intel_topic_articles
                        WHERE assignment_method!='manual'
                          AND topic_id IN (
                              SELECT id FROM intel_topics
                              WHERE industry_pack_id=? AND topic_key IN ({placeholders})
                          )
                        """,
                        [industry_pack_id, *topic_rows.keys()],
                    )

                cursor.execute(
                    """
                    SELECT a.id AS article_id, a.title, a.content,
                           c.topic_tags_json, c.matched_keywords_json,
                           c.score_details_json, c.final_category
                    FROM article_intel_classifications c
                    JOIN articles a ON a.id=c.article_id
                    WHERE c.industry_pack_id=? AND a.status='active'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM intel_evidence_group_articles ega
                          JOIN intel_evidence_groups eg ON eg.id=ega.evidence_group_id
                          WHERE ega.article_id=a.id
                            AND eg.industry_pack_id=c.industry_pack_id
                            AND eg.representative_article_id!=a.id
                      )
                    ORDER BY c.classified_at DESC, a.id DESC
                    """,
                    (industry_pack_id,),
                )
                articles = []
                for row in cursor.fetchall():
                    article = dict(row)
                    score_details = _json_value(
                        article.get("score_details_json"), {}
                    )
                    if not _classification_admitted(score_details):
                        continue
                    article["score_details"] = score_details
                    articles.append(article)
                stats["articles"] = len(articles)
                for article in articles:
                    category = str(article.get("final_category") or "other")
                    if category in stats["category_counts"]:
                        stats["category_counts"][category] += 1
                stats["trend_articles"] = stats["category_counts"]["trend"]
                # 各主题的 exclude_keywords（按 topic_key 索引），用于在 rule 与 llm_tag 两条路径都跳过厂商产品/导航页
                exclude_by_key = {
                    str(topic.get("key") or ""): list(topic.get("exclude_keywords") or [])
                    for topic in (pack.get("fixed_topics") or [])
                }
                for article in articles:
                    article_for_matching = {
                        "title": article.get("title") or "",
                        "content": article.get("content") or "",
                        "matched_keywords": _json_value(
                            article.get("matched_keywords_json"), []
                        ),
                    }
                    rule_assignments = {
                        item["key"]: item
                        for item in match_fixed_topics(article_for_matching, pack)
                    }
                    tags = [
                        normalize_intel_text(value)
                        for value in _json_value(article.get("topic_tags_json"), [])
                    ]
                    persisted_assignments = []
                    candidates = []
                    title_norm = normalize_intel_text(str(article.get("title") or ""))
                    content_norm = normalize_intel_text(str(article.get("content") or "")[:200000])
                    for topic in topic_rows.values():
                        # 「客户与中标」只收招标/中标/采购类文章：rule_keyword 与 llm_tag 都需强标讯信号
                        if (
                            (topic.get("key") == "customers" or topic.get("name") == "客户与中标")
                            and not _BID_TENDER_RE.search(
                                str(article.get("title") or "") + " " + str(article.get("content") or "")
                            )
                        ):
                            continue
                        # 排除词门：厂商产品页/导航页无论经 rule 还是 llm_tag 分配都跳过该主题
                        excl = exclude_by_key.get(str(topic.get("key") or "")) or []
                        if excl:
                            excluded = False
                            for ek in excl:
                                nk = normalize_intel_text(str(ek))
                                if not nk:
                                    continue
                                if nk in title_norm:
                                    excluded = True
                                    break
                                if nk not in _TITLE_ONLY_EXCLUDE and nk in content_norm:
                                    excluded = True
                                    break
                            if excluded:
                                continue
                        rule_assignment = rule_assignments.get(topic["key"])
                        keyword_hits = list(
                            (rule_assignment or {}).get("matched_keywords") or []
                        )
                        topic_terms = {
                            normalize_intel_text(topic["key"]),
                            normalize_intel_text(topic["name"]),
                            *(
                                normalize_intel_text(value)
                                for value in topic.get("keywords") or []
                            ),
                        }
                        tag_hits = [tag for tag in tags if tag in topic_terms]
                        if not keyword_hits and not tag_hits:
                            continue
                        method = "rule_keyword" if keyword_hits else "llm_tag"
                        score = (
                            float(rule_assignment.get("score") or 0)
                            if rule_assignment
                            else min(1.0, 0.5 * len(tag_hits))
                        )
                        candidates.append({
                            "topic": topic, "score": score, "method": method,
                            "keyword_hits": keyword_hits, "tag_hits": tag_hits,
                            "rule_assignment": rule_assignment,
                        })
                    # 单主题近亲：一篇文章只归入"权重最高"的一个主题，避免交叉出现在多个领域
                    if candidates:
                        candidates.sort(key=lambda c: c["score"], reverse=True)
                        best = candidates[0]
                        topic = best["topic"]
                        score = best["score"]
                        method = best["method"]
                        keyword_hits = best["keyword_hits"]
                        tag_hits = best["tag_hits"]
                        rule_assignment = best["rule_assignment"]
                        # 先清除该文章在其它主题的关联，确保只在最优主题出现。
                        # 手工发文/编辑写入的 manual 关联保留：topic_cluster 不得清除
                        # 编辑者显式指定的主题归属（否则手动文章每次重建后从主题卡消失）。
                        # watch_keyword 关联同样保留：周报「下周关注」生成的「本周盯防」主题
                        # 是叠加层（一篇文章可以既属于领域主题、又被本周盯防盯上），
                        # 聚类不得把它从盯防卡里抹掉。
                        cursor.execute(
                            "DELETE FROM intel_topic_articles WHERE article_id=? "
                            "AND assignment_method NOT IN ('manual', ?)",
                            (int(article["article_id"]), WATCH_ASSIGNMENT_METHOD),
                        )
                        evidence = {
                            "keywords": keyword_hits,
                            "llm_tags": tag_hits if method == "llm_tag" else [],
                            "rule_assignment": rule_assignment or {},
                            "content_type": article.get("final_category") or "other",
                            "topic_tagging_version": TOPIC_TAGGING_VERSION,
                            "cluster_version": TOPIC_CLUSTER_VERSION,
                        }
                        cursor.execute(
                            """
                            INSERT INTO intel_topic_articles (
                                topic_id, article_id, association_score,
                                assignment_method, evidence_json, assigned_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(topic_id, article_id) DO UPDATE SET
                                association_score=excluded.association_score,
                                assignment_method=excluded.assignment_method,
                                evidence_json=excluded.evidence_json,
                                updated_at=excluded.updated_at
                            WHERE intel_topic_articles.assignment_method!='manual'
                            """,
                            (
                                topic["id"],
                                int(article["article_id"]),
                                score,
                                method,
                                json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                                now,
                                now,
                            ),
                        )
                        persisted_assignments.append(
                            rule_assignment
                            or {
                                "key": topic["key"],
                                "name": topic["name"],
                                "matched_keywords": [],
                                "evidence": [{"llm_tags": tag_hits}],
                                "score": score,
                                "assignment_method": "llm_tag",
                            }
                        )
                        stats["associations"] += 1
                    score_details = article["score_details"]
                    score_details["topic_tagging_version"] = TOPIC_TAGGING_VERSION
                    score_details["topic_assignments"] = persisted_assignments
                    cursor.execute(
                        """
                        UPDATE article_intel_classifications
                        SET topic_tags_json=?, score_details_json=?, updated_at=?
                        WHERE article_id=? AND industry_pack_id=?
                        """,
                        (
                            json.dumps(
                                [item["name"] for item in persisted_assignments],
                                ensure_ascii=False,
                            ),
                            json.dumps(score_details, ensure_ascii=False, sort_keys=True),
                            now,
                            int(article["article_id"]),
                            industry_pack_id,
                        ),
                    )

                for topic in topic_rows.values():
                    cursor.execute(
                        """
                        SELECT ta.article_id, ta.association_score, a.title,
                               c.classified_at, eg.evidence_grade,
                               eg.max_authority_level, eg.conflict_status
                        FROM intel_topic_articles ta
                        JOIN articles a ON a.id=ta.article_id
                        JOIN article_intel_classifications c
                          ON c.article_id=a.id AND c.industry_pack_id=?
                        LEFT JOIN intel_evidence_group_articles ega
                          ON ega.article_id=a.id
                        LEFT JOIN intel_evidence_groups eg
                          ON eg.id=ega.evidence_group_id
                         AND eg.industry_pack_id=c.industry_pack_id
                        WHERE ta.topic_id=? AND a.status='active'
                        ORDER BY
                          CASE COALESCE(eg.evidence_grade,'D')
                            WHEN 'A' THEN 0 WHEN 'B' THEN 1 WHEN 'C' THEN 2
                            WHEN 'D' THEN 3 ELSE 4 END,
                          COALESCE(eg.max_authority_level,1) DESC,
                          ta.association_score DESC, c.classified_at DESC, a.id DESC
                        """,
                        (industry_pack_id, topic["id"]),
                    )
                    members = [dict(row) for row in cursor.fetchall()]
                    signature = hashlib.sha256(
                        "|".join(
                            f"{row['article_id']}:{row['classified_at']}"
                            for row in members
                        ).encode("utf-8")
                    ).hexdigest()
                    cursor.execute(
                        "SELECT content_signature FROM intel_topics WHERE id=?",
                        (topic["id"],),
                    )
                    old_signature = cursor.fetchone()["content_signature"]
                    summary = ""
                    summary_time = None
                    if (
                        len(members) >= config.INTEL_TOPIC_SUMMARY_MIN_ARTICLES
                        and signature != old_signature
                    ):
                        titles = [
                            " ".join(str(row.get("title") or "").split())[:80]
                            for row in members[:3]
                        ]
                        summary = f"{topic['name']}近期关注：" + "；".join(titles)
                        summary_time = now
                        stats["summaries_updated"] += 1
                    cursor.execute(
                        """
                        UPDATE intel_topics
                        SET article_count_cache=?, representative_article_id=?,
                            content_signature=?, last_clustered_at=?,
                            summary=CASE WHEN ?!='' THEN ? ELSE summary END,
                            summary_source=CASE WHEN ?!='' THEN 'rule' ELSE summary_source END,
                            summary_version=CASE WHEN ?!='' THEN ? ELSE summary_version END,
                            last_summary_at=COALESCE(?, last_summary_at),
                            updated_at=?
                        WHERE id=?
                        """,
                        (
                            len(members),
                            int(members[0]["article_id"]) if members else None,
                            signature,
                            now,
                            summary,
                            summary,
                            summary,
                            summary,
                            TOPIC_SUMMARY_VERSION,
                            summary_time,
                            now,
                            topic["id"],
                        ),
                    )
                self.db.connection.commit()
                return stats
            except Exception:
                self.db.connection.rollback()
                raise
            finally:
                cursor.close()

    def list_topics(
        self,
        *,
        industry_pack_id: str,
        time_range: str = "7d",
        page: int = 1,
        per_page: int = 20,
    ) -> Tuple[List[Dict], int, Dict]:
        self._ensure()
        start, end = parse_time_range(time_range)
        page = coerce_int(page, 1, 1)
        per_page = coerce_int(per_page, 20, 1, 100)
        time_filter = (
            f"datetime({ARTICLE_TIME_SQL})>=datetime(?) "
            f"AND datetime({ARTICLE_TIME_SQL})<=datetime(?)"
        )
        params = (industry_pack_id, utc_text(start), utc_text(end))
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT COUNT(*) AS total FROM intel_topics t
                    WHERE t.industry_pack_id=?
                      AND EXISTS (
                          SELECT 1 FROM intel_topic_articles ta
                          JOIN articles a ON a.id=ta.article_id
                        WHERE ta.topic_id=t.id AND a.status='active'
                            AND NOT EXISTS (
                                SELECT 1 FROM intel_evidence_group_articles ega
                                JOIN intel_evidence_groups eg ON eg.id=ega.evidence_group_id
                                WHERE ega.article_id=a.id
                                  AND eg.industry_pack_id=t.industry_pack_id
                                  AND eg.representative_article_id!=a.id
                            )
                            AND {time_filter}
                      )
                    """,
                    params,
                )
                total = int(cursor.fetchone()["total"])
                cursor.execute(
                    f"""
                    SELECT t.*,
                           (
                               SELECT COUNT(*) FROM intel_topic_articles ta
                               JOIN articles a ON a.id=ta.article_id
                               WHERE ta.topic_id=t.id AND a.status='active'
                                 AND NOT EXISTS (
                                     SELECT 1 FROM intel_evidence_group_articles ega
                                     JOIN intel_evidence_groups eg ON eg.id=ega.evidence_group_id
                                     WHERE ega.article_id=a.id
                                       AND eg.industry_pack_id=t.industry_pack_id
                                       AND eg.representative_article_id!=a.id
                                 )
                                 AND {time_filter}
                           ) AS article_count
                    FROM intel_topics t
                    WHERE t.industry_pack_id=?
                      AND EXISTS (
                          SELECT 1 FROM intel_topic_articles ta2
                          JOIN articles a ON a.id=ta2.article_id
                          WHERE ta2.topic_id=t.id AND a.status='active'
                            AND NOT EXISTS (
                                SELECT 1 FROM intel_evidence_group_articles ega
                                JOIN intel_evidence_groups eg ON eg.id=ega.evidence_group_id
                                WHERE ega.article_id=a.id
                                  AND eg.industry_pack_id=t.industry_pack_id
                                  AND eg.representative_article_id!=a.id
                            )
                            AND {time_filter}
                      )
                    ORDER BY article_count DESC, t.updated_at DESC, t.id
                    LIMIT ? OFFSET ?
                    """,
                    (
                        utc_text(start),
                        utc_text(end),
                        industry_pack_id,
                        utc_text(start),
                        utc_text(end),
                        per_page,
                        (page - 1) * per_page,
                    ),
                )
                topics = []
                for topic_row in cursor.fetchall():
                    topic = dict(topic_row)
                    topic["keywords"] = _json_value(topic.pop("keywords_json", "[]"), [])
                    cursor.execute(
                        f"""
                        SELECT a.id AS article_id, a.title, a.domain,
                               ta.association_score, ta.assignment_method,
                               ta.evidence_json, eg.evidence_grade,
                               eg.base_evidence_grade,
                               eg.independent_source_count,
                               eg.max_authority_level, eg.conflict_status,
                               eg.citations_json
                        FROM intel_topic_articles ta
                        JOIN articles a ON a.id=ta.article_id
                        LEFT JOIN intel_evidence_group_articles ega
                          ON ega.article_id=a.id
                        LEFT JOIN intel_evidence_groups eg
                          ON eg.id=ega.evidence_group_id
                         AND eg.industry_pack_id=?
                        WHERE ta.topic_id=? AND a.status='active'
                          AND NOT EXISTS (
                              SELECT 1 FROM intel_evidence_group_articles ega
                              JOIN intel_evidence_groups eg ON eg.id=ega.evidence_group_id
                              WHERE ega.article_id=a.id
                                AND eg.industry_pack_id=?
                                AND eg.representative_article_id!=a.id
                          )
                          AND {time_filter}
                        ORDER BY
                                 CASE COALESCE(eg.evidence_grade,'D')
                                   WHEN 'A' THEN 0 WHEN 'B' THEN 1
                                   WHEN 'C' THEN 2 WHEN 'D' THEN 3 ELSE 4 END,
                                 COALESCE(eg.max_authority_level,1) DESC,
                                 ta.association_score DESC,
                                 datetime({ARTICLE_TIME_SQL}) DESC, a.id DESC
                        LIMIT 5
                        """,
                        (
                            industry_pack_id,
                            topic["id"],
                            industry_pack_id,
                            utc_text(start),
                            utc_text(end),
                        ),
                    )
                    topic["articles"] = []
                    for article_row in cursor.fetchall():
                        article = dict(article_row)
                        article["evidence"] = _json_value(
                            article.pop("evidence_json", "{}"),
                            {},
                        )
                        article["source_evidence"] = {
                            "evidence_grade": article.pop("evidence_grade", None) or "D",
                            "base_evidence_grade": article.pop(
                                "base_evidence_grade", None
                            )
                            or "D",
                            "independent_source_count": int(
                                article.pop("independent_source_count", None) or 1
                            ),
                            "max_authority_level": int(
                                article.pop("max_authority_level", None) or 1
                            ),
                            "conflict_status": article.pop("conflict_status", None)
                            or "none",
                            "citations": _json_value(
                                article.pop("citations_json", "[]"), []
                            ),
                        }
                        topic["articles"].append(article)
                    topics.append(topic)
                return topics, total, {
                    "time_range": time_range,
                    "from": utc_text(start),
                    "to": utc_text(end),
                    "timezone": "Asia/Hong_Kong",
                }
            finally:
                cursor.close()


intel_topic_service = IntelTopicService()

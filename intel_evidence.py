#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Conservative cross-source evidence grouping for generic industry news."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlsplit

from industry_packs import normalize_intel_text
from intel_contracts import utc_text
from intel_database import ARTICLE_TIME_SQL, INDUSTRY_KEYWORD_GATE_SQL
from source_authority import resolve_source_authority
from sqlite_database import sqlite_db


EVIDENCE_VERSION = "generic-source-evidence-v1"
EVENT_WINDOW_SECONDS = 7 * 24 * 60 * 60
EVENT_SIMILARITY_THRESHOLD = 0.72
DUPLICATE_SIMILARITY_THRESHOLD = 0.90

_NUMBER_RE = re.compile(
    r"(?<!\d)(\d+(?:\.\d+)?)\s*(%|％|亿元|亿港元|亿元人民币|万元|万|亿|"
    r"million|billion|trillion|percent|家|个|宗|项|人)(?!\w)",
    re.IGNORECASE,
)
_DIRECTION_GROUPS = (
    ("approved", ("获批准", "批准", "通过", "获批", "核准")),
    ("rejected", ("拒绝", "否决", "未通过", "不予批准")),
    ("up", ("增长", "上升", "增加", "扩大", "提高")),
    ("down", ("下降", "减少", "缩减", "下跌", "降低")),
    ("started", ("启动", "签约", "达成", "成立", "推出")),
    ("stopped", ("终止", "取消", "暂停", "撤回", "关闭")),
)
_DIRECTION_OPPOSITES = {
    frozenset(("approved", "rejected")),
    frozenset(("up", "down")),
    frozenset(("started", "stopped")),
}


def _json_value(value, default):
    try:
        return json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _timestamp(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return 0.0


def _normalized_title(value: object) -> str:
    text = normalize_intel_text(value)
    text = re.sub(r"\d+(?:\.\d+)?", "数字", text)
    direction_terms = sorted(
        {term for _direction, terms in _DIRECTION_GROUPS for term in terms},
        key=len,
        reverse=True,
    )
    if direction_terms:
        text = re.sub("|".join(map(re.escape, direction_terms)), "方向", text)
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", text)


def _shingles(value: object, size: int = 3) -> set[str]:
    text = _normalized_title(value)
    if not text:
        return set()
    if len(text) <= size:
        return {text}
    return {text[index : index + size] for index in range(len(text) - size + 1)}


def title_similarity(left: object, right: object) -> float:
    a = _shingles(left)
    b = _shingles(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def extract_comparable_claims(title: object) -> List[Dict]:
    """Extract only conservative title-level claims suitable for comparison."""

    text = normalize_intel_text(title)
    claims = []
    numeric_matches = list(_NUMBER_RE.finditer(text))
    if numeric_matches:
        template = _NUMBER_RE.sub(
            lambda match: f"<number:{match.group(2).casefold()}>", text
        )
        unit_slots = defaultdict(int)
        for match in numeric_matches:
            unit = match.group(2).casefold()
            unit_slots[unit] += 1
            claims.append(
                {
                    "kind": "numeric",
                    # Keep separate metrics in one title (for example a price
                    # change percentage and turnover in 亿元) from being
                    # compared as if they were competing values for one fact.
                    "key": f"numeric:{template}:unit={unit}:slot={unit_slots[unit]}",
                    "value": f"{match.group(1)}{unit}",
                }
            )
    direction_hits = []
    term_directions = {
        normalize_intel_text(term): direction
        for direction, terms in _DIRECTION_GROUPS
        for term in terms
    }
    ordered_terms = sorted(term_directions, key=len, reverse=True)
    direction_template = text
    if ordered_terms:
        pattern = re.compile("|".join(map(re.escape, ordered_terms)))

        def replace_direction(match):
            direction = term_directions[match.group(0)]
            if direction not in direction_hits:
                direction_hits.append(direction)
            return "<direction>"

        direction_template = pattern.sub(replace_direction, text)
        direction_template = direction_template.replace(
            "获<direction>", "<direction>"
        ).replace("未<direction>", "<direction>")
    for direction in direction_hits:
        claims.append(
            {
                "kind": "direction",
                "key": f"direction:{direction_template}",
                "value": direction,
            }
        )
    return claims


def _claim_scope(rows: Iterable[Dict]) -> str:
    text = normalize_intel_text(" ".join(str(row.get("title") or "") for row in rows))
    if any(term in text for term in ("调查", "事故", "安全结论")):
        return "investigation_finding"
    if any(term in text for term in ("统计", "数量", "规模", "宗", "家族办公室数")):
        return "official_statistics"
    if any(term in text for term in ("监管", "法规", "政策", "处罚", "批准", "许可")):
        return "regulatory_decision"
    return "reported_claim"


def _base_grade(independent_count: int, max_authority: int) -> str:
    if max_authority >= 5 or (independent_count >= 2 and max_authority >= 4):
        return "A"
    if max_authority >= 4 or (independent_count >= 2 and max_authority >= 3):
        return "B"
    if max_authority >= 2:
        return "C"
    return "D"


class IntelEvidenceService:
    def __init__(self, database=None):
        self.db = database or sqlite_db

    def _ensure(self) -> None:
        self.db._ensure_connection()

    def _source_profiles(self, article_rows: List[Dict]) -> Dict[int, Dict]:
        article_ids = [int(row["article_id"]) for row in article_rows]
        if not article_ids:
            return {}
        placeholders = ",".join("?" for _ in article_ids)
        with self.db.lock:
            observed = self.db.connection.execute(
                f"""
                SELECT ic.article_id, s.*
                FROM intel_candidates ic
                JOIN intel_candidate_observations o ON o.candidate_id=ic.id
                JOIN intel_sources s ON s.id=o.source_id
                WHERE ic.article_id IN ({placeholders})
                ORDER BY s.authority_level DESC, o.last_observed_at DESC, s.id
                """,
                article_ids,
            ).fetchall()
            all_sources = self.db.connection.execute(
                "SELECT * FROM intel_sources ORDER BY authority_level DESC, id"
            ).fetchall()
        observed_by_article = defaultdict(list)
        for raw in observed:
            observed_by_article[int(raw["article_id"])].append(dict(raw))
        source_by_host = {}
        for raw in all_sources:
            source = dict(raw)
            host = (
                urlsplit(str(source.get("source_url") or "")).hostname or ""
            ).casefold()
            if host.startswith("www."):
                host = host[4:]
            if host and host not in source_by_host:
                source_by_host[host] = source

        profiles = {}
        for article in article_rows:
            article_id = int(article["article_id"])
            candidates = observed_by_article.get(article_id) or []
            if not candidates:
                host = (urlsplit(str(article.get("url") or "")).hostname or "").casefold()
                if host.startswith("www."):
                    host = host[4:]
                matched = source_by_host.get(host)
                if matched:
                    candidates = [matched]
            source = candidates[0] if candidates else {}
            metadata = _json_value(source.get("metadata_json"), {})
            resolved = resolve_source_authority(
                {
                    "url": source.get("source_url") or article.get("url"),
                    "source_role": metadata.get("source_role"),
                    "authority_level": source.get("authority_level") or 1,
                    "authority_scope": metadata.get("authority_scope"),
                    "publisher_key": metadata.get("publisher_key"),
                }
            )
            profiles[article_id] = {
                **resolved,
                "source_id": int(source["id"]) if source.get("id") else None,
                "source_name": str(
                    source.get("source_name")
                    or article.get("domain")
                    or resolved["publisher_key"]
                    or "未登记来源"
                ),
                "source_url": str(source.get("source_url") or article.get("url") or ""),
            }
        return profiles

    def _article_rows(self, industry_pack_id: str) -> List[Dict]:
        with self.db.lock:
            rows = self.db.connection.execute(
                f"""
                SELECT a.id AS article_id, a.url, a.title, a.domain,
                       a.content_length, substr(COALESCE(a.content,''),1,1200) AS content_preview,
                       c.activation_id, c.final_category, c.topic_tags_json,
                       c.score_details_json, {ARTICLE_TIME_SQL} AS effective_time
                FROM article_intel_classifications c
                JOIN articles a ON a.id=c.article_id
                WHERE c.industry_pack_id=? AND a.status='active'
                  AND {INDUSTRY_KEYWORD_GATE_SQL}
                ORDER BY datetime(effective_time) DESC, a.id DESC
                """,
                (industry_pack_id,),
            ).fetchall()
        result = []
        for raw in rows:
            row = dict(raw)
            row["topic_tags"] = set(_json_value(row.pop("topic_tags_json", "[]"), []))
            result.append(row)
        return result

    @staticmethod
    def _group_rows(rows: List[Dict]) -> List[List[Dict]]:
        groups: List[List[Dict]] = []
        for row in rows:
            best_group = None
            best_similarity = 0.0
            for group in groups:
                head = group[0]
                if str(head.get("activation_id") or "") != str(
                    row.get("activation_id") or ""
                ):
                    continue
                if str(head.get("final_category") or "") != str(
                    row.get("final_category") or ""
                ):
                    continue
                if abs(
                    _timestamp(head.get("effective_time"))
                    - _timestamp(row.get("effective_time"))
                ) > EVENT_WINDOW_SECONDS:
                    continue
                similarity = max(
                    title_similarity(row.get("title"), member.get("title"))
                    for member in group
                )
                shared_topics = bool(
                    set(row.get("topic_tags") or set())
                    & set(head.get("topic_tags") or set())
                )
                threshold = (
                    EVENT_SIMILARITY_THRESHOLD
                    if shared_topics
                    else DUPLICATE_SIMILARITY_THRESHOLD
                )
                if similarity >= threshold and similarity > best_similarity:
                    best_group = group
                    best_similarity = similarity
            row["group_similarity"] = round(best_similarity or 1.0, 4)
            if best_group is None:
                groups.append([row])
            else:
                best_group.append(row)
        return groups

    @staticmethod
    def _conflicts(group: List[Dict], profiles: Dict[int, Dict]) -> Dict:
        claims_by_key = defaultdict(list)
        for row in group:
            for claim in extract_comparable_claims(row.get("title")):
                claims_by_key[claim["key"]].append(
                    {
                        **claim,
                        "article_id": int(row["article_id"]),
                        "title": str(row.get("title") or ""),
                        "source_name": profiles[int(row["article_id"])]["source_name"],
                        "source_role": profiles[int(row["article_id"])]["source_role"],
                        "authority_level": profiles[int(row["article_id"])]["authority_level"],
                    }
                )
        details = []
        for key, claims in claims_by_key.items():
            values = {claim["value"] for claim in claims}
            is_conflict = len(values) > 1
            if claims and claims[0]["kind"] == "direction":
                is_conflict = any(
                    frozenset(pair) in _DIRECTION_OPPOSITES
                    for pair in (
                        (left, right)
                        for left in values
                        for right in values
                        if left != right
                    )
                )
            if is_conflict:
                details.append({"claim_key": key, "claims": claims})
        if not details:
            return {"status": "none", "details": [], "preferred_article_id": None}

        scope = _claim_scope(group)
        eligible = []
        conflicting_ids = {
            int(claim["article_id"])
            for detail in details
            for claim in detail["claims"]
        }
        for article_id in conflicting_ids:
            profile = profiles[article_id]
            if profile["can_resolve_conflicts"] and scope in set(
                profile.get("authority_scope") or []
            ):
                eligible.append((profile["authority_level"], article_id))
        eligible.sort(reverse=True)
        preferred = None
        status = "unresolved"
        if eligible and (len(eligible) == 1 or eligible[0][0] > eligible[1][0]):
            preferred = int(eligible[0][1])
            status = "authoritative_preferred"
        return {
            "status": status,
            "details": [{**item, "scope": scope} for item in details],
            "preferred_article_id": preferred,
        }

    def rebuild(self, industry_pack_id: str) -> Dict:
        """Rebuild one pack atomically; source articles are never deleted."""

        self._ensure()
        rows = self._article_rows(str(industry_pack_id))
        profiles = self._source_profiles(rows)
        groups = self._group_rows(rows)
        now = utc_text()
        stats = {
            "industry_pack_id": str(industry_pack_id),
            "article_count": len(rows),
            "group_count": len(groups),
            "collapsed_article_count": max(0, len(rows) - len(groups)),
            "conflict_count": 0,
            "version": EVIDENCE_VERSION,
        }
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                cursor.execute(
                    "DELETE FROM intel_evidence_groups WHERE industry_pack_id=?",
                    (str(industry_pack_id),),
                )
                for group in groups:
                    conflict = self._conflicts(group, profiles)
                    if conflict["status"] != "none":
                        stats["conflict_count"] += 1
                    preferred_id = conflict.get("preferred_article_id")
                    representative = max(
                        group,
                        key=lambda row: (
                            int(int(row["article_id"]) == int(preferred_id or -1)),
                            profiles[int(row["article_id"])]["authority_level"],
                            int(profiles[int(row["article_id"])]["can_resolve_conflicts"]),
                            int(row.get("content_length") or 0),
                            _timestamp(row.get("effective_time")),
                            -int(row["article_id"]),
                        ),
                    )
                    representative_id = int(representative["article_id"])
                    unique_publishers = {}
                    for row in group:
                        profile = profiles[int(row["article_id"])]
                        publisher = profile["publisher_key"] or f"article:{row['article_id']}"
                        current = unique_publishers.get(publisher)
                        if not current or profile["authority_level"] > current["authority_level"]:
                            unique_publishers[publisher] = {
                                **profile,
                                "article_id": int(row["article_id"]),
                                "article_url": str(row.get("url") or ""),
                                "title": str(row.get("title") or ""),
                                "published_at": str(row.get("effective_time") or ""),
                            }
                    independent_count = len(unique_publishers)
                    max_authority = max(
                        (item["authority_level"] for item in unique_publishers.values()),
                        default=1,
                    )
                    base_grade = _base_grade(independent_count, max_authority)
                    grade = (
                        "CONFLICT"
                        if conflict["status"] == "unresolved"
                        else base_grade
                    )
                    conflict_ids = {
                        int(claim["article_id"])
                        for detail in conflict["details"]
                        for claim in detail["claims"]
                    }
                    citations = []
                    for publisher, citation in unique_publishers.items():
                        article_id = int(citation["article_id"])
                        citations.append(
                            {
                                "article_id": article_id,
                                "article_url": citation["article_url"],
                                "title": citation["title"],
                                "source_id": citation["source_id"],
                                "source_name": citation["source_name"],
                                "source_url": citation["source_url"],
                                "source_role": citation["source_role"],
                                "source_role_label": citation["source_role_label"],
                                "authority_level": citation["authority_level"],
                                "publisher_key": publisher,
                                "published_at": citation["published_at"],
                                "relationship": (
                                    "conflicting"
                                    if article_id in conflict_ids
                                    else (
                                        "representative"
                                        if article_id == representative_id
                                        else "corroborating"
                                    )
                                ),
                            }
                        )
                    citations.sort(
                        key=lambda item: (
                            -int(item["authority_level"]),
                            -_timestamp(item["published_at"]),
                            item["source_name"],
                        )
                    )
                    event_key = hashlib.sha256(
                        f"{industry_pack_id}\x1f{group[0].get('activation_id') or ''}\x1f"
                        f"{min(int(row['article_id']) for row in group)}".encode("utf-8")
                    ).hexdigest()
                    cursor.execute(
                        """
                        INSERT INTO intel_evidence_groups(
                            industry_pack_id,activation_id,event_key,
                            representative_article_id,evidence_grade,
                            base_evidence_grade,independent_source_count,
                            max_authority_level,conflict_status,
                            conflict_details_json,citations_json,created_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            str(industry_pack_id),
                            str(group[0].get("activation_id") or ""),
                            event_key,
                            representative_id,
                            grade,
                            base_grade,
                            independent_count,
                            max_authority,
                            conflict["status"],
                            json.dumps(conflict["details"], ensure_ascii=False, sort_keys=True),
                            json.dumps(citations, ensure_ascii=False, sort_keys=True),
                            now,
                            now,
                        ),
                    )
                    group_id = int(cursor.lastrowid)
                    representative_title = representative.get("title")
                    for row in group:
                        article_id = int(row["article_id"])
                        profile = profiles[article_id]
                        similarity = title_similarity(
                            row.get("title"), representative_title
                        )
                        relationship = "representative"
                        if article_id != representative_id:
                            if article_id in conflict_ids:
                                relationship = "conflicting"
                            elif similarity >= DUPLICATE_SIMILARITY_THRESHOLD:
                                relationship = "duplicate"
                            else:
                                relationship = "corroborating"
                        cursor.execute(
                            """
                            INSERT INTO intel_evidence_group_articles(
                                evidence_group_id,article_id,source_id,publisher_key,
                                source_role,authority_level,title_similarity,
                                relationship,created_at,updated_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                group_id,
                                article_id,
                                profile["source_id"],
                                profile["publisher_key"],
                                profile["source_role"],
                                profile["authority_level"],
                                round(similarity, 4),
                                relationship,
                                now,
                                now,
                            ),
                        )
                self.db.connection.commit()
            except Exception:
                self.db.connection.rollback()
                raise
            finally:
                cursor.close()
        return stats

    def evidence_for_articles(
        self, article_ids: Iterable[int], *, industry_pack_id: str
    ) -> Dict[int, Dict]:
        ids = sorted({int(value) for value in article_ids if int(value) > 0})
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self.db.lock:
            rows = self.db.connection.execute(
                f"""
                SELECT ga.article_id, ga.relationship, ga.publisher_key,
                       ga.source_role, ga.authority_level,
                       g.id AS evidence_group_id, g.event_key,
                       g.representative_article_id, g.evidence_grade,
                       g.base_evidence_grade, g.independent_source_count,
                       g.max_authority_level, g.conflict_status,
                       g.conflict_details_json, g.citations_json
                FROM intel_evidence_group_articles ga
                JOIN intel_evidence_groups g ON g.id=ga.evidence_group_id
                WHERE g.industry_pack_id=? AND ga.article_id IN ({placeholders})
                """,
                [str(industry_pack_id), *ids],
            ).fetchall()
        result = {}
        for raw in rows:
            item = dict(raw)
            item["is_representative"] = int(item["article_id"]) == int(
                item["representative_article_id"]
            )
            item["conflict_details"] = _json_value(
                item.pop("conflict_details_json", "[]"), []
            )
            item["citations"] = _json_value(item.pop("citations_json", "[]"), [])
            item["evidence_version"] = EVIDENCE_VERSION
            result[int(item["article_id"])] = item
        return result


intel_evidence_service = IntelEvidenceService()

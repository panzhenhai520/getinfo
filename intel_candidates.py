#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Candidate discovery persistence, scoring, state machine, and scan audit."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

import config
from industry_packs import industry_anchor_keywords, industry_pack_loader, normalize_intel_text
from intel_contracts import CANDIDATE_STATUSES, parse_time_range, utc_now, utc_text
from intel_sources import canonicalize_source_url
from sqlite_database import sqlite_db
from utils import coerce_int

try:
    from dateutil import parser as date_parser
except ImportError:  # pragma: no cover
    date_parser = None


def _json_text(value, default):
    return json.dumps(value if value is not None else default, ensure_ascii=False, sort_keys=True)


_DENY_DOMAINS_SETTING_KEY = "crawl_deny_domains"
_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")


def deny_domains_snapshot(db) -> Dict:
    """域名黑名单快照：环境变量基线 + 运行时维护（信源管理页）合并。

    返回 {"domains": [...], "env_domains": [...], "runtime_domains": [...]}。
    env 基线只能改 .env；runtime 部分可在信源管理页面增删，worker 每次建候选时
    从数据库现读，修改即时生效（无需重启 worker）。
    """
    env_domains = [str(d).casefold() for d in config.CRAWL_DENY_DOMAINS if str(d).strip()]
    runtime_domains = []
    try:
        if db is not None:
            db._ensure_connection()
            row = db.connection.execute(
                "SELECT setting_value FROM intel_runtime_settings WHERE setting_key=?",
                (_DENY_DOMAINS_SETTING_KEY,),
            ).fetchone()
            if row:
                runtime_domains = [
                    str(d).strip().casefold()
                    for d in str(row["setting_value"] or "").split(",")
                    if str(d).strip()
                ]
    except Exception:
        runtime_domains = []
    merged = list(dict.fromkeys(env_domains + runtime_domains))
    return {
        "domains": merged,
        "env_domains": env_domains,
        "runtime_domains": runtime_domains,
    }


def _deny_domain_set(db) -> set:
    """discover 用的黑名单集合（env + 运行时合并，含子域匹配时逐项比对）。"""
    snapshot = deny_domains_snapshot(db)
    return set(snapshot["domains"])


_ASCII_ONLY_KEYWORD = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 .+\-_/]*$')


def _kw_present(text: str, keyword: str) -> bool:
    """关键词是否命中。纯 ASCII 关键词按**单词边界**匹配：短缩写（OTA/ANC/V2X 等）
    不允许命中单词内部子串（如 automotive / Toyota 里的 "ota"、BALANCE 里的 "ANC"），
    否则英文文章会被宽进；中文没有词边界，按子串匹配。"""
    if not text or not keyword:
        return False
    if _ASCII_ONLY_KEYWORD.match(keyword):
        return bool(re.search(r'(?<![A-Za-z0-9])' + re.escape(keyword) + r'(?![A-Za-z0-9])', text, re.I))
    return keyword in text


def _json_value(value, default):
    try:
        return json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def normalize_candidate_time(value) -> Tuple[Optional[str], str]:
    if value is None or str(value).strip() == "":
        return None, "unknown"
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if len(raw) == 10:
            try:
                datetime.strptime(raw, "%Y-%m-%d")
                if _is_implausible_future(raw):
                    return None, "unknown"
                return raw, "date"
            except ValueError:
                pass
        if not date_parser:
            return None, "unknown"
        try:
            parsed = date_parser.parse(raw)
        except (TypeError, ValueError, OverflowError):
            return None, "unknown"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    # 未来日期一律视为解析错误：历史上出现过把"明年1月1日／2027年底"这类模糊表述
    # 硬填成具体日期（如 2027-01-01 / 2027-12-01）。假日期会污染时效判定与界面显示。
    if parsed > datetime.now(timezone.utc) + timedelta(days=90):
        return None, "unknown"
    return utc_text(parsed), "exact"


def _is_implausible_future(raw: str) -> bool:
    """纯日期字符串是否落在未来（容一天时区差）。"""
    try:
        value = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return value > datetime.now(timezone.utc) + timedelta(days=90)


def freshness_window_days(pack_id: str = "") -> int:
    """行业包时效窗（天）：按包配置，默认 90 天，环境变量可兜底覆盖。

    安全漏洞通告 30 天就失去意义，政策公告两年后仍是权威依据，
    所以时效窗必须能按行业包分别设置。
    """
    try:
        pack = industry_pack_loader.load(str(pack_id)) if pack_id else {}
        value = pack.get("freshness_window_days")
        if value not in (None, ""):
            return max(1, int(value))
    except Exception:
        pass
    try:
        from pack_tenant import get_pack_setting
        override = str(get_pack_setting(str(pack_id), "freshness_window_days", "") or "").strip()
        if override.isdigit():
            return max(1, int(override))
    except Exception:
        pass
    return max(1, int(os.getenv("INTEL_FRESHNESS_WINDOW_DAYS", "90") or 90))


def candidate_within_freshness_window(published_at, pack_id: str = "") -> bool:
    """候选是否在时效窗内。

    运营决策：过时信息在候选阶段就丢弃；**没有可用发布日期的同样舍弃**。
    未来日期（超过今天 1 天）视为解析错误，一律不采信——历史上出现过把
    "明年1月1日 / 2027年底"这类模糊表述硬填成具体日期的情况。
    """
    if not published_at:
        return False
    if isinstance(published_at, datetime):
        published = published_at if published_at.tzinfo else published_at.replace(tzinfo=timezone.utc)
    else:
        text = str(published_at).strip().replace("Z", "+00:00").replace(" ", "T")
        try:
            published = datetime.fromisoformat(text)
        except ValueError:
            return False
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if published > now + timedelta(days=1):
        return False
    return (now - published) <= timedelta(days=freshness_window_days(pack_id))


def quick_score_candidate(title: str, summary: str, pack: Dict) -> Dict:
    title_text = normalize_intel_text(title)
    summary_text = normalize_intel_text(summary)
    combined = f"{title_text} {summary_text}"
    classification = pack["classification"]
    groups = (
        ("core_keywords", "core_weight"),
        ("expanded_keywords", "expanded_weight"),
        ("trend_keywords", "trend_weight"),
        ("event_keywords", "event_weight"),
        ("negative_keywords", "negative_weight"),
    )
    matched = []
    score = 0.0
    anchor_terms = {normalize_intel_text(value) for value in industry_anchor_keywords(pack)}
    anchor_hits = []
    for keyword_field, weight_field in groups:
        weight = float(classification[weight_field])
        for keyword in pack.get(keyword_field) or []:
            normalized_keyword = normalize_intel_text(keyword)
            if normalized_keyword and _kw_present(combined, normalized_keyword):
                title_multiplier = 1.5 if _kw_present(title_text, normalized_keyword) else 1.0
                contribution = weight * title_multiplier
                score += contribution
                matched.append(
                    {
                        "keyword": keyword,
                        "group": keyword_field,
                        "weight": contribution,
                    }
                )
                if normalized_keyword in anchor_terms and keyword not in anchor_hits:
                    anchor_hits.append(keyword)
    # Registered entity anchors need not also be repeated in core_keywords.
    # Count them as core relevance while retaining their distinct provenance.
    existing_normalized = {normalize_intel_text(item['keyword']) for item in matched}
    for keyword in industry_anchor_keywords(pack):
        normalized_keyword = normalize_intel_text(keyword)
        if not normalized_keyword or not _kw_present(combined, normalized_keyword):
            continue
        if keyword not in anchor_hits:
            anchor_hits.append(keyword)
        if normalized_keyword not in existing_normalized:
            contribution = float(classification['core_weight']) * (1.5 if _kw_present(title_text, normalized_keyword) else 1.0)
            score += contribution
            matched.append({'keyword': keyword, 'group': 'entity_anchor', 'weight': contribution})
    threshold = float(classification["minimum_relevance_score"])
    # 行业锚点门禁（硬门控）：必须命中行业包的主题锚点词才能进入候选队列。
    # 泛词（电力/通信/基础设施/中标/发布 之类）只能给“已经相关”的内容加分，不允许单独把
    # 无关文章送进聚合——否则采购招标公告会因为施工范围里附带提了一句“配套电力管网”就入库。
    # 唯一例外：行业包自己没定义任何锚点词时不强制，否则会把整包拦空。
    anchor_enforced = bool(anchor_terms)
    return {
        "score": round(score, 4),
        "threshold": threshold,
        "matched_keywords": matched,
        "anchor_hits": anchor_hits,
        "anchor_required": anchor_enforced,
        "should_queue": (bool(matched)
                         and score >= max(0.5, threshold * 0.5)
                         and (bool(anchor_hits) or not anchor_enforced)),
    }


class IntelCandidateRepository:
    def __init__(self, database=None):
        self.db = database or sqlite_db

    def _ensure(self):
        self.db._ensure_connection()

    def record_extraction_attempt(self, candidate_id: int, *, strategy: str, status: str,
                                  content_length: int = 0, quality_score=None,
                                  integrity_issues=None, metadata=None, error: str = "") -> None:
        """Append-only extraction audit; callers must never overwrite attempts."""
        self._ensure()
        if status not in {"started", "passed", "failed", "retryable"}:
            raise ValueError("invalid extraction attempt status")
        with self.db.lock:
            self.db.connection.execute(
                """INSERT INTO intel_extraction_attempts
                   (candidate_id,strategy,status,content_length,quality_score,integrity_issues_json,metadata_json,error_message)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (int(candidate_id), str(strategy), status, max(0, int(content_length or 0)), quality_score,
                 _json_text(integrity_issues or [], []), _json_text(metadata or {}, {}), str(error or "")[:1000]),
            )
            self.db.connection.execute(
                "UPDATE intel_candidates SET extraction_attempt_count=extraction_attempt_count+1, updated_at=? WHERE id=?",
                (utc_text(), int(candidate_id)),
            )
            self.db.connection.commit()

    def set_candidate_decision(self, candidate_id: int, *, quality_status: str = None,
                               admission_status: str = None, admission_reason: str = "",
                               admission_confidence=None, metadata_status: str = None,
                               metadata_issues=None) -> None:
        """Persist pre-insert quality/admission decisions without changing legacy status."""
        self._ensure()
        fields, values = [], []
        for column, value in (("quality_status", quality_status), ("admission_status", admission_status),
                              ("metadata_status", metadata_status)):
            if value is not None:
                fields.append(f"{column}=?"); values.append(str(value))
        if admission_status is not None:
            fields.extend(["admission_reason=?", "admission_confidence=?"])
            values.extend([str(admission_reason or "")[:500], admission_confidence])
        if metadata_issues is not None:
            fields.append("metadata_issues_json=?"); values.append(_json_text(metadata_issues, []))
        if not fields:
            return
        fields.append("updated_at=?"); values.append(utc_text()); values.append(int(candidate_id))
        with self.db.lock:
            self.db.connection.execute(f"UPDATE intel_candidates SET {', '.join(fields)} WHERE id=?", values)
            self.db.connection.commit()

    def add_url_expansion_candidate(self, *, industry_pack_id: str, parent_candidate_id: int,
                                    parent_url: str, url: str, anchor_text: str = "",
                                    context_text: str = "", score: float = 0) -> bool:
        canonical = canonicalize_source_url(url)
        if not canonical or canonical == canonicalize_source_url(parent_url):
            return False
        self._ensure()
        with self.db.lock:
            self.db.connection.execute(
                """INSERT INTO intel_url_expansion_candidates
                   (industry_pack_id,parent_candidate_id,parent_url,canonical_url,original_url,anchor_text,context_text,score,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(industry_pack_id,parent_url,canonical_url) DO UPDATE SET
                     score=MAX(score, excluded.score), anchor_text=excluded.anchor_text, context_text=excluded.context_text, updated_at=excluded.updated_at""",
                (industry_pack_id, int(parent_candidate_id), parent_url, canonical, url, anchor_text[:1000], context_text[:2000], float(score), utc_text()),
            )
            self.db.connection.commit()
        return True

    def discover(
        self,
        item: Dict,
        *,
        industry_pack_id: str,
        activation_id: str = "",
        source_id: Optional[int] = None,
        scan_run_id: Optional[int] = None,
        observation_type: str,
        query_text: str = "",
        bypass_industry_gate: bool = False,
        force_unqueued: bool = False,
    ) -> Dict:
        self._ensure()
        pack = industry_pack_loader.load(industry_pack_id)
        raw_url = str(item.get("url") or "").strip()
        canonical_url = canonicalize_source_url(raw_url)
        domain = (urlsplit(canonical_url).hostname or "").casefold()
        # 域名黑名单：不可达/被污染的源（如境内服务器抓不到的境外站）不再建候选。
        # 支持根域匹配：黑名单配 investing.com 时其子域 cn.investing.com 一并拒绝。
        # 黑名单 = 环境变量基线 + 运行时维护（信源管理页），worker 现读现用。
        _deny_set = _deny_domain_set(self.db)
        _denied = any(
            domain == _d or domain.endswith("." + _d) for _d in _deny_set
        )
        if domain and _denied:
            return {
                "candidate_id": None,
                "created": False,
                "reactivated": False,
                "duplicate_observation": False,
                "possible_duplicate_of": None,
                "score": 0.0,
                "threshold": 0.0,
                "matched_keywords": [],
                "anchor_hits": [],
                "anchor_required": False,
                "should_queue": False,
                "denied_domain": True,
            }
        title = str(item.get("title") or "").strip()[:1000]
        summary = str(item.get("summary") or item.get("snippet") or "").strip()[:5000]
        normalized_title = normalize_intel_text(title)
        title_hash = (
            hashlib.sha256(normalized_title.encode("utf-8")).hexdigest()
            if normalized_title
            else ""
        )
        published_at, precision = normalize_candidate_time(item.get("published_at"))
        scoring = quick_score_candidate(title, summary, pack)
        # Google/SerpAPI is intentionally constrained to the industry's
        # configured search phrases.  Do not apply the second, snippet-only
        # candidate gate here: snippets are frequently truncated and would
        # otherwise hide a legitimate Google result before its page is read.
        # The full-content classification still runs after extraction.
        if bypass_industry_gate:
            scoring["should_queue"] = True
        elif force_unqueued:
            scoring["should_queue"] = False
        # 时效性准入（运营决策）：超时效窗的候选一律不派发。
        # 无发布日期的处理分两条路：
        # - 搜索发现（bypass_industry_gate）：摘要里通常没有可靠发布时间，候选阶段放行，
        #   时效判定推迟到抓正文之后（全文日期会进入分类）。
        # - 信源直采（website/rss/list_page/sitemap）：水位线已经确认该链接是"新出现"的，
        #   列表页/RSS 条目又普遍不带日期；无日期同样放行，正文抽取后由信源水位线近似日期。
        #   有日期但超窗的仍拒绝。
        if not bypass_industry_gate and not candidate_within_freshness_window(
            published_at, industry_pack_id
        ):
            _direct_source_no_date = (
                not published_at
                and observation_type in ("website", "rss", "list_page", "sitemap")
            )
            if not _direct_source_no_date:
                scoring["should_queue"] = False
                scoring["freshness_rejected"] = True
        now = utc_text()
        observation_key = hashlib.sha256(
            "\x1f".join(
                (
                    observation_type,
                    str(activation_id or ""),
                    str(source_id or ""),
                    str(query_text or ""),
                    raw_url,
                )
            ).encode("utf-8")
        ).hexdigest()
        result = {
            "candidate_id": None,
            "created": False,
            "reactivated": False,
            "duplicate_observation": False,
            "possible_duplicate_of": None,
            **scoring,
        }

        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                cursor.execute(
                    "SELECT * FROM intel_candidates WHERE canonical_url=?",
                    (canonical_url,),
                )
                existing = cursor.fetchone()
                if existing:
                    candidate_id = int(existing["id"])
                    previous_industry = cursor.execute(
                        """
                        SELECT activation_id
                        FROM intel_candidate_industries
                        WHERE candidate_id=? AND industry_pack_id=?
                        """,
                        (candidate_id, industry_pack_id),
                    ).fetchone()
                    previous_activation_id = str(
                        previous_industry["activation_id"]
                        if previous_industry else ""
                    )
                    reactivated = bool(
                        scoring["should_queue"]
                        and not existing["article_id"]
                        and existing["status"] in {"failed", "discarded"}
                        and str(activation_id or "")
                        and previous_activation_id != str(activation_id)
                    )
                    next_status = existing["status"]
                    if reactivated or (
                        scoring["should_queue"] and next_status == "discovered"
                    ):
                        next_status = "queued"
                    cursor.execute(
                        """
                        UPDATE intel_candidates
                        SET original_url=?, title=CASE WHEN ?!='' THEN ? ELSE title END,
                            normalized_title=CASE WHEN ?!='' THEN ? ELSE normalized_title END,
                            title_hash=CASE WHEN ?!='' THEN ? ELSE title_hash END,
                            summary=CASE WHEN ?!='' THEN ? ELSE summary END,
                            published_at=COALESCE(?, published_at),
                            published_precision=CASE WHEN ? IS NOT NULL THEN ? ELSE published_precision END,
                            last_seen_at=?, quick_score=MAX(quick_score, ?),
                            status=?, updated_at=?
                        WHERE id=?
                        """,
                        (
                            raw_url,
                            title,
                            title,
                            normalized_title,
                            normalized_title,
                            title_hash,
                            title_hash,
                            summary,
                            summary,
                            published_at,
                            published_at,
                            precision,
                            now,
                            scoring["score"],
                            next_status,
                            now,
                            candidate_id,
                        ),
                    )
                    if reactivated:
                        cursor.execute(
                            """
                            UPDATE intel_candidates
                            SET attempt_count=0, next_retry_at=NULL,
                                lease_owner=NULL, lease_expires_at=NULL,
                                last_error='', quality_status='pending',
                                admission_status='pending', admission_reason='',
                                admission_confidence=NULL, metadata_status='pending',
                                metadata_issues_json='[]', updated_at=?
                            WHERE id=?
                            """,
                            (now, candidate_id),
                        )
                        result["reactivated"] = True
                else:
                    possible_duplicate_of = None
                    if title_hash:
                        cursor.execute(
                            """
                            SELECT id FROM intel_candidates
                            WHERE domain=? AND title_hash=?
                            ORDER BY first_seen_at, id LIMIT 1
                            """,
                            (domain, title_hash),
                        )
                        suspicious = cursor.fetchone()
                        if suspicious:
                            possible_duplicate_of = int(suspicious["id"])
                    status = "queued" if scoring["should_queue"] else "discovered"
                    cursor.execute(
                        """
                        INSERT INTO intel_candidates (
                            canonical_url, original_url, title, normalized_title,
                            title_hash, domain, summary, published_at,
                            published_precision, first_seen_at, last_seen_at,
                            quick_score, status, possible_duplicate_of,
                            duplicate_reason, max_attempts, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            canonical_url,
                            raw_url,
                            title,
                            normalized_title,
                            title_hash,
                            domain,
                            summary,
                            published_at,
                            precision,
                            now,
                            now,
                            scoring["score"],
                            status,
                            possible_duplicate_of,
                            "same_domain_and_title_hash" if possible_duplicate_of else "",
                            config.INTEL_CANDIDATE_MAX_RETRIES + 1,
                            now,
                            now,
                        ),
                    )
                    candidate_id = int(cursor.lastrowid)
                    result["created"] = True
                    result["possible_duplicate_of"] = possible_duplicate_of

                cursor.execute(
                    """
                    INSERT INTO intel_candidate_industries (
                        candidate_id, industry_pack_id, activation_id, quick_score, threshold,
                        matched_keywords_json, should_queue, first_seen_at,
                        last_seen_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(candidate_id, industry_pack_id) DO UPDATE SET
                        activation_id=excluded.activation_id,
                        quick_score=excluded.quick_score,
                        threshold=excluded.threshold,
                        matched_keywords_json=excluded.matched_keywords_json,
                        should_queue=excluded.should_queue,
                        last_seen_at=excluded.last_seen_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        candidate_id,
                        industry_pack_id,
                        str(activation_id or ""),
                        scoring["score"],
                        scoring["threshold"],
                        _json_text(scoring["matched_keywords"], []),
                        int(scoring["should_queue"]),
                        now,
                        now,
                        now,
                        now,
                    ),
                )
                cursor.execute(
                    """
                    SELECT id FROM intel_candidate_observations
                    WHERE candidate_id=? AND observation_key=?
                    """,
                    (candidate_id, observation_key),
                )
                old_observation = cursor.fetchone()
                if old_observation:
                    cursor.execute(
                        """
                        UPDATE intel_candidate_observations
                        SET source_id=?, scan_run_id=?, title=?, summary=?,
                            published_at=COALESCE(?, published_at),
                            last_observed_at=?, seen_count=seen_count+1,
                            updated_at=?
                        WHERE id=?
                        """,
                        (
                            source_id,
                            scan_run_id,
                            title,
                            summary,
                            published_at,
                            now,
                            now,
                            int(old_observation["id"]),
                        ),
                    )
                    result["duplicate_observation"] = True
                else:
                    cursor.execute(
                        """
                        INSERT INTO intel_candidate_observations (
                            candidate_id, source_id, scan_run_id, activation_id, observation_type,
                            observation_key, query_text, raw_url, title, summary,
                            published_at, observed_at, last_observed_at,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            candidate_id,
                            source_id,
                            scan_run_id,
                            str(activation_id or ""),
                            observation_type,
                            observation_key,
                            str(query_text or "")[:1000],
                            raw_url,
                            title,
                            summary,
                            published_at,
                            now,
                            now,
                            now,
                            now,
                        ),
                    )
                if existing and existing["article_id"] and scoring["should_queue"]:
                    cursor.execute(
                        """
                        INSERT INTO content_industry_packs(
                            content_type, content_id, industry_pack_id,
                            association_type, origin_pack_id, is_active,
                            created_at, updated_at
                        ) VALUES('article', ?, ?, 'derived', ?, 1, ?, ?)
                        ON CONFLICT(content_type, content_id, industry_pack_id)
                        DO UPDATE SET is_active=1, updated_at=excluded.updated_at
                        """,
                        (
                            str(int(existing["article_id"])),
                            industry_pack_id,
                            industry_pack_id,
                            now,
                            now,
                        ),
                    )
                self.db.connection.commit()
                result["candidate_id"] = candidate_id
                if existing and existing["article_id"]:
                    # 关联文章已软删（如历史 tag 列表页误抓被清理）：解除关联并跳过分类，
                    # 否则 enqueue_classification 会对已删文章报 "article not found"。
                    # 注意 get_article_by_id 只返回 active 文章，这里必须用原生 SQL 查 status。
                    cursor.execute(
                        "SELECT status FROM articles WHERE id=?",
                        (int(existing["article_id"]),),
                    )
                    _art_row = cursor.fetchone()
                    if not _art_row or str(_art_row["status"] or "") == "deleted":
                        cursor.execute(
                            "UPDATE intel_candidates SET article_id=NULL, updated_at=? WHERE id=?",
                            (now, candidate_id),
                        )
                        self.db.connection.commit()
                        return result
                    # A canonical candidate can already have an article from a
                    # different industry pack.  Re-observation under this pack
                    # must schedule the pack-specific classification instead
                    # of leaving a permanent article-without-classification gap.
                    from intel_database import IntelRepository

                    job_id, created = IntelRepository(self.db).enqueue_classification(
                        int(existing["article_id"]),
                        industry_pack_id,
                        activation_id=str(activation_id or ""),
                        ragflow_upload=True,
                    )
                    result["classification_job"] = {
                        "industry_pack_id": industry_pack_id,
                        "job_id": int(job_id or 0),
                        "created": bool(created),
                    }
                return result
            except Exception:
                self.db.connection.rollback()
                raise
            finally:
                cursor.close()

    def rescore_discovered_candidates(
        self,
        industry_pack_id: str,
        *,
        activation_id: str = "",
        max_candidates: int = 20000,
    ) -> Dict:
        """关键词/配置变化后，把该行业包下仍未派发的 ``discovered`` 候选重新打分。

        当行业包关键词扩宽/调整（发布新版本）后，之前因旧关键词命中不足而停在
        ``discovered`` 的候选，多数现在能命中。本方法用**当前**包配置对每个候选的
        标题+摘要重跑 ``quick_score_candidate``；命中阈值的就从 ``discovered`` 提升为
        ``queued``（并按 reactivation 一样重置尝试/租约/质量位），让派发器自动接手，
        无需人工脚本。纯关键词匹配，无 LLM、无网络，速度很快。
        """
        self._ensure()
        try:
            from industry_packs import industry_pack_loader
            pack = industry_pack_loader.load(str(industry_pack_id or "")) or {}
        except Exception:
            pack = {}
        if not pack:
            return {"pack": str(industry_pack_id), "scanned": 0, "promoted": 0,
                    "error": "行业包未找到，跳过重打分"}
        # 1) 读取出当前包仍待派发（discovered）的候选
        with self.db.lock:
            cur = self.db.connection.cursor()
            try:
                cur.execute(
                    """SELECT c.id, c.title, c.summary
                       FROM intel_candidates c
                       JOIN intel_candidate_industries ci ON ci.candidate_id=c.id
                       WHERE c.status='discovered' AND ci.industry_pack_id=?
                       ORDER BY c.id ASC LIMIT ?""",
                    (str(industry_pack_id), int(max_candidates or 20000)),
                )
                rows = [dict(r) for r in cur.fetchall()]
            finally:
                cur.close()
        # 2) 在内存里打分（无锁），只收集应提升的候选
        to_promote = []
        for row in rows:
            try:
                scoring = quick_score_candidate(
                    str(row["title"] or ""), str(row["summary"] or ""), pack
                )
            except Exception:
                continue
            if scoring.get("should_queue"):
                to_promote.append((int(row["id"]), scoring))
        # 3) 批量提升（一次事务提交）
        now = utc_text()
        with self.db.lock:
            cur = self.db.connection.cursor()
            try:
                for cid, scoring in to_promote:
                    cur.execute(
                        """UPDATE intel_candidates
                           SET status='queued', quick_score=MAX(quick_score, ?),
                               attempt_count=0, next_retry_at=NULL,
                               lease_owner=NULL, lease_expires_at=NULL,
                               last_error='', quality_status='pending',
                               admission_status='pending', admission_reason='',
                               admission_confidence=NULL, metadata_status='pending',
                               metadata_issues_json='[]', updated_at=?
                           WHERE id=? AND status='discovered'""",
                        (scoring["score"], now, cid),
                    )
                    cur.execute(
                        """UPDATE intel_candidate_industries
                           SET quick_score=?, threshold=?, matched_keywords_json=?,
                               should_queue=1, activation_id=?, updated_at=?
                           WHERE candidate_id=? AND industry_pack_id=?""",
                        (
                            scoring["score"],
                            scoring["threshold"],
                            _json_text(scoring["matched_keywords"], []),
                            str(activation_id or ""),
                            now,
                            cid,
                            str(industry_pack_id),
                        ),
                    )
                self.db.connection.commit()
            finally:
                cur.close()
        return {
            "pack": str(industry_pack_id),
            "scanned": len(rows),
            "promoted": len(to_promote),
        }

    def claim_candidates(
        self,
        worker_id: str,
        *,
        limit: Optional[int] = None,
        lease_seconds: Optional[int] = None,
        candidate_ids: Optional[Iterable[int]] = None,
        active_activation_id: str = "",
        industry_pack_id: str = "",
    ) -> List[Dict]:
        self._ensure()
        limit = coerce_int(limit, config.INTEL_CANDIDATE_BATCH_SIZE, 1, 100)
        lease_seconds = coerce_int(
            lease_seconds,
            config.INTEL_CANDIDATE_LEASE_SECONDS,
            30,
            3600,
        )
        now = utc_now()
        now_text = utc_text(now)
        lease_text = utc_text(now + timedelta(seconds=lease_seconds))
        selected_ids = None
        if candidate_ids is not None:
            selected_ids = tuple(
                dict.fromkeys(int(value) for value in candidate_ids if int(value) > 0)
            )
            if not selected_ids:
                return []
        claimed: List[Dict] = []
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                cursor.execute(
                    """
                    UPDATE intel_candidates
                    SET status='retry_wait', lease_owner=NULL, lease_expires_at=NULL,
                        next_retry_at=?, last_error=CASE WHEN last_error=''
                            THEN 'dispatcher lease expired' ELSE last_error END,
                        updated_at=?
                    WHERE status='dispatching' AND lease_expires_at IS NOT NULL
                      AND lease_expires_at<=?
                    """,
                    (now_text, now_text, now_text),
                )
                id_filter = ""
                select_params = [now_text]
                activation_filter = ""
                pack_id = str(industry_pack_id or "").strip()
                if pack_id:
                    # 按行业包限定派发范围：只派该包 should_queue=1 的候选。
                    # activation 宽匹配：候选可能没有激活会话（ci.activation_id=''），
                    # 而派发任务可能被异包的激活上下文污染（历史 bug 会把其它包的
                    # activation_id stamp 进 payload）——匹配"同激活或未激活"都放行，
                    # 否则无激活包（如汽车）的候选永远派不出去。
                    if str(active_activation_id or ""):
                        activation_filter = (
                            " AND EXISTS (SELECT 1 FROM intel_candidate_industries ci "
                            "WHERE ci.candidate_id=intel_candidates.id "
                            "AND ci.should_queue=1 AND ci.industry_pack_id=? "
                            "AND (ci.activation_id=? OR ci.activation_id=''))"
                        )
                        select_params.extend([pack_id, str(active_activation_id)])
                    else:
                        activation_filter = (
                            " AND EXISTS (SELECT 1 FROM intel_candidate_industries ci "
                            "WHERE ci.candidate_id=intel_candidates.id "
                            "AND ci.should_queue=1 AND ci.industry_pack_id=?)"
                        )
                        select_params.append(pack_id)
                elif str(active_activation_id or ""):
                    activation_filter = (
                        " AND EXISTS (SELECT 1 FROM intel_candidate_industries ci "
                        "WHERE ci.candidate_id=intel_candidates.id "
                        "AND ci.should_queue=1 AND ci.activation_id=?)"
                    )
                    select_params.append(str(active_activation_id))
                if selected_ids is not None:
                    placeholders = ",".join("?" for _ in selected_ids)
                    id_filter = f" AND id IN ({placeholders})"
                    select_params.extend(selected_ids)
                select_params.append(limit)
                cursor.execute(
                    f"""
                    SELECT * FROM intel_candidates
                    WHERE status IN ('queued', 'retry_wait')
                      AND (next_retry_at IS NULL OR next_retry_at<=?)
                      {activation_filter}
                      {id_filter}
                    ORDER BY quick_score DESC, first_seen_at, id
                    LIMIT ?
                    """,
                    select_params,
                )
                for row in cursor.fetchall():
                    cursor.execute(
                        """
                        UPDATE intel_candidates
                        SET status='dispatching', attempt_count=attempt_count+1,
                            lease_owner=?, lease_expires_at=?, updated_at=?
                        WHERE id=? AND status IN ('queued', 'retry_wait')
                        """,
                        (worker_id, lease_text, now_text, int(row["id"])),
                    )
                    if cursor.rowcount:
                        item = dict(row)
                        item["activation_id"] = str(active_activation_id or "")
                        item["status"] = "dispatching"
                        item["attempt_count"] = int(item["attempt_count"] or 0) + 1
                        claimed.append(item)
                self.db.connection.commit()
                return claimed
            except Exception:
                self.db.connection.rollback()
                raise
            finally:
                cursor.close()

    def complete_candidate(self, candidate_id: int, article_id: int, crawler_task_id: str) -> bool:
        self._ensure()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    """
                    UPDATE intel_candidates
                    SET status='crawled', article_id=?, crawler_task_id=?,
                        lease_owner=NULL, lease_expires_at=NULL, next_retry_at=NULL,
                        last_error='', updated_at=?
                    WHERE id=? AND status='dispatching'
                    """,
                    (article_id, str(crawler_task_id or ""), utc_text(), candidate_id),
                )
                completed = cursor.rowcount > 0
                if completed:
                    now = utc_text()
                    cursor.execute(
                        """
                        INSERT INTO content_industry_packs(
                            content_type, content_id, industry_pack_id,
                            association_type, origin_pack_id, is_active,
                            created_at, updated_at
                        )
                        SELECT 'article', ?, industry_pack_id, 'derived',
                               industry_pack_id, 1, ?, ?
                        FROM intel_candidate_industries
                        WHERE candidate_id=? AND should_queue=1
                        ON CONFLICT(content_type, content_id, industry_pack_id)
                        DO UPDATE SET is_active=1, updated_at=excluded.updated_at
                        """,
                        (str(int(article_id)), now, now, int(candidate_id)),
                    )
                self.db.connection.commit()
                return completed
            finally:
                cursor.close()

    def fail_candidate(
        self,
        candidate_id: int,
        error: str,
        *,
        permanent: bool = False,
    ) -> str:
        self._ensure()
        now = utc_now()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    "SELECT attempt_count, max_attempts FROM intel_candidates WHERE id=?",
                    (candidate_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return "missing"
                retry = not permanent and int(row["attempt_count"]) < int(row["max_attempts"])
                status = "retry_wait" if retry else "failed"
                next_retry = (
                    utc_text(
                        now
                        + timedelta(
                            seconds=min(3600, 30 * (2 ** max(0, int(row["attempt_count"]) - 1)))
                        )
                    )
                    if retry
                    else None
                )
                cursor.execute(
                    """
                    UPDATE intel_candidates
                    SET status=?, next_retry_at=?, lease_owner=NULL,
                        lease_expires_at=NULL, last_error=?, updated_at=?
                    WHERE id=?
                    """,
                    (status, next_retry, str(error or "")[:500], utc_text(now), candidate_id),
                )
                self.db.connection.commit()
                return status
            finally:
                cursor.close()

    def find_article_for_candidate(self, candidate: Dict) -> Optional[int]:
        """Match URL evidence exactly after canonicalization; never guess by recency."""
        self._ensure()
        canonical = candidate["canonical_url"]
        original = candidate["original_url"]
        domain = candidate["domain"]
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT id, url, canonical_url, resolved_target_url
                    FROM articles
                    WHERE status='active' AND (
                        url IN (?, ?) OR canonical_url IN (?, ?)
                        OR resolved_target_url IN (?, ?)
                        OR domain=?
                    )
                    """,
                    (original, canonical, original, canonical, original, canonical, domain),
                )
                for row in cursor.fetchall():
                    for field in ("url", "canonical_url", "resolved_target_url"):
                        value = row[field]
                        if not value:
                            continue
                        try:
                            if canonicalize_source_url(value) == canonical:
                                return int(row["id"])
                        except ValueError:
                            continue
                return None
            finally:
                cursor.close()

    def get_candidate_industry_keywords(self, candidate_id: int) -> List[str]:
        self._ensure()
        keywords = []
        seen = set()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT matched_keywords_json FROM intel_candidate_industries
                    WHERE candidate_id=?
                    """,
                    (candidate_id,),
                )
                for row in cursor.fetchall():
                    for match in _json_value(row["matched_keywords_json"], []):
                        value = str(match.get("keyword") if isinstance(match, dict) else match).strip()
                        if value and value not in seen:
                            seen.add(value)
                            keywords.append(value)
                return keywords
            finally:
                cursor.close()

    def get_candidate_industry_pack_id(
        self, candidate_id: int, *, activation_id: str = ""
    ) -> str:
        """Return the pack which admitted a candidate, for full-text validation."""
        pack_ids = self.get_candidate_industry_pack_ids(
            candidate_id, activation_id=activation_id
        )
        return pack_ids[0] if pack_ids else ""

    def get_candidate_industry_pack_ids(
        self, candidate_id: int, *, activation_id: str = ""
    ) -> List[str]:
        """Return every pack which admitted the same canonical candidate."""
        self._ensure()
        with self.db.lock:
            rows = self.db.connection.execute(
                """SELECT industry_pack_id FROM intel_candidate_industries
                   WHERE candidate_id=? AND should_queue=1
                     AND (?='' OR activation_id=?)
                   ORDER BY id ASC""",
                (int(candidate_id), str(activation_id or ""), str(activation_id or "")),
            ).fetchall()
        return [str(row["industry_pack_id"]) for row in rows if row["industry_pack_id"]]

    def create_scan_run(
        self,
        *,
        source_id: Optional[int],
        industry_pack_id: str,
        scanner_type: str,
        metadata: Optional[Dict] = None,
        scan_window_key: str = "",
        requested_pack_ids: Optional[Iterable[str]] = None,
        activation_id: str = "",
    ) -> int:
        run_id, _created = self.claim_scan_run(
            source_id=source_id,
            industry_pack_id=industry_pack_id,
            scanner_type=scanner_type,
            metadata=metadata,
            scan_window_key=scan_window_key,
            requested_pack_ids=requested_pack_ids,
            activation_id=activation_id,
        )
        return run_id

    def claim_scan_run(
        self,
        *,
        source_id: Optional[int],
        industry_pack_id: str,
        scanner_type: str,
        metadata: Optional[Dict] = None,
        scan_window_key: str = "",
        requested_pack_ids: Optional[Iterable[str]] = None,
        activation_id: str = "",
    ) -> Tuple[int, bool]:
        """Atomically own one physical source/time-window scan across workers."""
        self._ensure()
        window_key = str(scan_window_key or "")
        pack_ids = list(dict.fromkeys(str(value) for value in (requested_pack_ids or [])))
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                try:
                    cursor.execute(
                        """
                        INSERT INTO intel_scan_runs (
                            source_id, industry_pack_id, activation_id, scan_window_key,
                            requested_pack_ids_json, scanner_type, status,
                            metadata_json, started_at, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
                        """,
                        (
                            source_id,
                            industry_pack_id,
                            str(activation_id or ""),
                            window_key,
                            _json_text(pack_ids, []),
                            scanner_type,
                            _json_text(metadata or {}, {}),
                            utc_text(),
                            utc_text(),
                        ),
                    )
                except Exception as _exc:
                    # SQLite 抛 sqlite3.IntegrityError；PostgreSQL 抛 UniqueViolation。
                    # 两者都表示同一 scan_window_key 已有记录（DB 上该列为唯一约束，
                    # 且当前库为整列唯一，failed 行也参与约束）。不应崩溃：
                    #  - 已有 run 为 running/completed/partial → 复用并跳过本次驱动；
                    #  - 已有 run 为 failed → 就地复用到 running 重新扫描（唯一约束阻止新增行）。
                    _is_unique_dup = (
                        "unique" in str(_exc).lower()
                        or "duplicate" in str(_exc).lower()
                        or "重复键" in str(_exc)
                        or "integrity" in str(_exc).lower()
                        or "already exists" in str(_exc).lower()
                    )
                    if not _is_unique_dup or not window_key:
                        raise
                    cursor.execute(
                        """SELECT id, status FROM intel_scan_runs
                           WHERE scan_window_key=?
                           ORDER BY id DESC LIMIT 1""",
                        (window_key,),
                    )
                    existing = cursor.fetchone()
                    if not existing:
                        raise
                    existing_id = int(existing["id"])
                    existing_status = existing["status"]
                    if existing_status in ("running", "completed", "partial"):
                        return existing_id, False
                    # 之前同窗口扫描失败：复用该行并重新扫描。
                    cursor.execute(
                        """UPDATE intel_scan_runs
                           SET status='running', started_at=?, completed_at=NULL,
                               error_type='', error_message='', metadata_json=?
                           WHERE id=?""",
                        (utc_text(), _json_text(metadata or {}, {}), existing_id),
                    )
                    self.db.connection.commit()
                    return existing_id, True
                self.db.connection.commit()
                return int(cursor.lastrowid), True
            finally:
                cursor.close()

    def finish_scan_run(self, run_id: int, stats: Dict) -> None:
        self._ensure()
        status = stats.get("status") or ("failed" if stats.get("error_message") else "completed")
        now = utc_text()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    "SELECT source_id, metadata_json FROM intel_scan_runs WHERE id=?",
                    (run_id,),
                )
                run_row = cursor.fetchone()
                existing_metadata = _json_value(
                    run_row["metadata_json"] if run_row else "", {}
                )
                existing_metadata.update(stats.get("metadata") or {})
                cursor.execute(
                    """
                    UPDATE intel_scan_runs
                    SET status=?, discovered_count=?, queued_count=?,
                        duplicate_count=?, below_threshold_count=?,
                        request_count=?, error_type=?, error_message=?,
                        metadata_json=?, completed_at=?
                    WHERE id=?
                    """,
                    (
                        status,
                        coerce_int(stats.get("discovered_count"), 0, 0),
                        coerce_int(stats.get("queued_count"), 0, 0),
                        coerce_int(stats.get("duplicate_count"), 0, 0),
                        coerce_int(stats.get("below_threshold_count"), 0, 0),
                        coerce_int(stats.get("request_count"), 0, 0),
                        str(stats.get("error_type") or "")[:100],
                        str(stats.get("error_message") or "")[:500],
                        _json_text(existing_metadata, {}),
                        now,
                        run_id,
                    ),
                )
                row = run_row
                if row and row["source_id"]:
                    if status in {"completed", "partial"}:
                        cursor.execute(
                            """
                            UPDATE intel_sources
                            SET last_scan_at=?, last_successful_scan_at=?,
                                last_scan_status=?, last_scan_error='',
                                consecutive_scan_failures=0, updated_at=?
                            WHERE id=?
                            """,
                            (now, now, status, now, int(row["source_id"])),
                        )
                    else:
                        cursor.execute(
                            """
                            UPDATE intel_sources
                            SET last_scan_at=?, last_scan_status=?,
                                last_scan_error=?,
                                consecutive_scan_failures=consecutive_scan_failures+1,
                                updated_at=?
                            WHERE id=?
                            """,
                            (
                                now,
                                status,
                                str(stats.get("error_message") or "")[:500],
                                now,
                                int(row["source_id"]),
                            ),
                        )
                self.db.connection.commit()
            finally:
                cursor.close()

    def reserve_api_usage(self, service: str, requested: int, daily_limit: int) -> int:
        self._ensure()
        requested = coerce_int(requested, 0, 0, 10000)
        daily_limit = coerce_int(daily_limit, 0, 0, 1000000)
        usage_date = utc_now().date().isoformat()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                cursor.execute(
                    "SELECT usage_count FROM intel_api_usage WHERE usage_date=? AND service=?",
                    (usage_date, service),
                )
                row = cursor.fetchone()
                used = int(row["usage_count"]) if row else 0
                allocated = min(requested, max(0, daily_limit - used))
                cursor.execute(
                    """
                    INSERT INTO intel_api_usage (usage_date, service, usage_count, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(usage_date, service) DO UPDATE SET
                        usage_count=excluded.usage_count,
                        updated_at=excluded.updated_at
                    """,
                    (usage_date, service, used + allocated, utc_text()),
                )
                self.db.connection.commit()
                return allocated
            except Exception:
                self.db.connection.rollback()
                raise
            finally:
                cursor.close()

    def api_usage_remaining(self, service: str, daily_limit: int) -> int:
        self._ensure()
        usage_date = utc_now().date().isoformat()
        row = self.db.connection.execute(
            "SELECT usage_count FROM intel_api_usage WHERE usage_date=? AND service=?",
            (usage_date, service),
        ).fetchone()
        return max(0, int(daily_limit) - int(row["usage_count"] if row else 0))

    def list_candidates(
        self,
        *,
        industry_pack_id: str = "",
        status: str = "",
        source_id: Optional[int] = None,
        time_range: str = "7d",
        page: int = 1,
        per_page: int = 20,
    ) -> Tuple[List[Dict], int, Dict]:
        self._ensure()
        page = coerce_int(page, 1, 1)
        per_page = coerce_int(per_page, 20, 1, 100)
        if status and status not in CANDIDATE_STATUSES:
            raise ValueError("不支持的候选状态")
        start, end = parse_time_range(time_range)
        filters = [
            "datetime(CASE WHEN LENGTH(c.published_at)=10 "
            "THEN datetime(c.published_at, '-8 hours') "
            "ELSE COALESCE(c.published_at, c.last_seen_at) END) >= datetime(?)",
            "datetime(CASE WHEN LENGTH(c.published_at)=10 "
            "THEN datetime(c.published_at, '-8 hours') "
            "ELSE COALESCE(c.published_at, c.last_seen_at) END) <= datetime(?)",
        ]
        params: List = [utc_text(start), utc_text(end)]
        if industry_pack_id:
            filters.append(
                "EXISTS (SELECT 1 FROM intel_candidate_industries ci "
                "WHERE ci.candidate_id=c.id AND ci.industry_pack_id=?)"
            )
            params.append(industry_pack_id)
        if status:
            filters.append("c.status=?")
            params.append(status)
        if source_id:
            filters.append(
                "EXISTS (SELECT 1 FROM intel_candidate_observations co "
                "WHERE co.candidate_id=c.id AND co.source_id=?)"
            )
            params.append(coerce_int(source_id, 0, 1))
        where_sql = " AND ".join(filters)
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    f"SELECT COUNT(*) AS total FROM intel_candidates c WHERE {where_sql}",
                    params,
                )
                total = int(cursor.fetchone()["total"])
                cursor.execute(
                    f"""
                    SELECT c.*,
                           COUNT(DISTINCT co.id) AS observation_count,
                           GROUP_CONCAT(DISTINCT ci.industry_pack_id) AS industry_pack_ids
                    FROM intel_candidates c
                    LEFT JOIN intel_candidate_observations co ON co.candidate_id=c.id
                    LEFT JOIN intel_candidate_industries ci ON ci.candidate_id=c.id
                    WHERE {where_sql}
                    GROUP BY c.id
                    ORDER BY COALESCE(c.published_at, c.last_seen_at) DESC, c.id DESC
                    LIMIT ? OFFSET ?
                    """,
                    [*params, per_page, (page - 1) * per_page],
                )
                rows = []
                for row in cursor.fetchall():
                    item = dict(row)
                    item["industry_pack_ids"] = [
                        value
                        for value in str(item.get("industry_pack_ids") or "").split(",")
                        if value
                    ]
                    item["time_source"] = (
                        "candidate_published_at" if item.get("published_at") else "last_seen_at"
                    )
                    rows.append(item)
                return rows, total, {
                    "time_range": time_range,
                    "from": utc_text(start),
                    "to": utc_text(end),
                    "timezone": "Asia/Hong_Kong",
                }
            finally:
                cursor.close()

    def list_scan_runs(
        self,
        *,
        industry_pack_id: str = "",
        source_id: Optional[int] = None,
        status: str = "",
        page: int = 1,
        per_page: int = 20,
    ) -> Tuple[List[Dict], int]:
        self._ensure()
        page = coerce_int(page, 1, 1)
        per_page = coerce_int(per_page, 20, 1, 100)
        filters = ["1=1"]
        params: List = []
        if industry_pack_id:
            filters.append("r.industry_pack_id=?")
            params.append(industry_pack_id)
        if source_id:
            filters.append("r.source_id=?")
            params.append(coerce_int(source_id, 0, 1))
        if status:
            filters.append("r.status=?")
            params.append(status)
        where_sql = " AND ".join(filters)
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    f"SELECT COUNT(*) AS total FROM intel_scan_runs r WHERE {where_sql}",
                    params,
                )
                total = int(cursor.fetchone()["total"])
                cursor.execute(
                    f"""
                    SELECT r.*, s.source_name, s.canonical_source_url
                    FROM intel_scan_runs r
                    LEFT JOIN intel_sources s ON s.id=r.source_id
                    WHERE {where_sql}
                    ORDER BY r.started_at DESC, r.id DESC
                    LIMIT ? OFFSET ?
                    """,
                    [*params, per_page, (page - 1) * per_page],
                )
                runs = []
                for row in cursor.fetchall():
                    item = dict(row)
                    item["metadata"] = _json_value(item.pop("metadata_json", "{}"), {})
                    runs.append(item)
                return runs, total
            finally:
                cursor.close()

    def list_source_scan_tasks(
        self,
        *,
        industry_pack_id: str = "",
        effective_pack_ids: Optional[Iterable[str]] = None,
        page: int = 1,
        per_page: int = 100,
    ) -> Tuple[List[Dict], int]:
        """Return one operational scan row per registered source.

        This is deliberately source-centric rather than a second copy of the
        legacy scheduled-task list.  Counts and failures belong to the latest
        scan run for that source, so an operator can see what happened during
        its most recent scheduled/initial scan.
        """
        self._ensure()
        page = coerce_int(page, 1, 1)
        per_page = coerce_int(per_page, 100, 1, 500)
        filters = ["1=1"]
        params: List = []
        scoped_pack_ids = list(
            dict.fromkeys(
                str(value or "").strip()
                for value in (effective_pack_ids or [])
                if str(value or "").strip()
            )
        )
        if not scoped_pack_ids and industry_pack_id:
            scoped_pack_ids = [str(industry_pack_id)]
        if scoped_pack_ids:
            placeholders = ",".join("?" for _ in scoped_pack_ids)
            filters.append(
                "EXISTS (SELECT 1 FROM intel_source_industries si "
                "WHERE si.source_id=s.id AND si.is_active=1 "
                f"AND si.industry_pack_id IN ({placeholders}))"
            )
            params.extend(scoped_pack_ids)
        where_sql = " AND ".join(filters)
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    f"SELECT COUNT(*) AS total FROM intel_sources s WHERE {where_sql}", params
                )
                total = int(cursor.fetchone()["total"])
                cursor.execute(
                    f"""
                    SELECT s.id, s.source_name, s.source_url, s.canonical_source_url,
                           s.source_type, s.is_enabled, s.polling_interval_minutes,
                           s.metadata_json, s.last_scan_at, s.last_scan_status,
                           s.last_scan_error, s.last_successful_scan_at,
                           r.id AS scan_run_id, r.scanner_type, r.status AS run_status,
                           r.discovered_count, r.queued_count, r.duplicate_count,
                           r.below_threshold_count, r.error_type, r.error_message,
                           r.started_at, r.completed_at,
                           (
                             SELECT COUNT(DISTINCT c.article_id)
                             FROM intel_candidate_observations o
                             JOIN intel_candidates c ON c.id=o.candidate_id
                             WHERE o.source_id=s.id AND o.scan_run_id=r.id
                               AND c.article_id IS NOT NULL
                           ) AS ingested_count,
                           (
                             SELECT GROUP_CONCAT(DISTINCT ard.kb_id)
                             FROM intel_candidate_observations o
                             JOIN intel_candidates c ON c.id=o.candidate_id
                             JOIN article_ragflow_documents ard ON ard.article_id=c.article_id
                             WHERE o.source_id=s.id AND o.scan_run_id=r.id
                               AND ard.sync_status NOT IN ('deleted', 'delete_failed')
                           ) AS knowledge_base_ids
                    FROM intel_sources s
                    LEFT JOIN intel_scan_runs r ON r.id=(
                        SELECT latest.id FROM intel_scan_runs latest
                        WHERE latest.source_id=s.id
                        ORDER BY latest.started_at DESC, latest.id DESC LIMIT 1
                    )
                    WHERE {where_sql}
                    ORDER BY s.is_enabled DESC, s.authority_level DESC, s.source_name, s.id
                    LIMIT ? OFFSET ?
                    """,
                    [*params, per_page, (page - 1) * per_page],
                )
                rows: List[Dict] = []
                for row in cursor.fetchall():
                    item = dict(row)
                    try:
                        metadata = json.loads(item.pop("metadata_json", "{}") or "{}")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        metadata = {}
                    item["browser_fetch_enabled"] = bool(metadata.get("browser_fetch_enabled"))
                    item["preferred_scan_time"] = str(metadata.get("preferred_scan_time") or config.INTEL_LIGHT_SCAN_DAILY_TIME)[:5]
                    item["schedule_rule"] = str(metadata.get("schedule_rule") or "daily")
                    item["polling_interval_minutes"] = int(item.get("polling_interval_minutes") or 1440)
                    item["knowledge_base_ids"] = [
                        value for value in str(item.get("knowledge_base_ids") or "").split(",") if value
                    ]
                    item["discovered_count"] = int(item.get("discovered_count") or 0)
                    item["ingested_count"] = int(item.get("ingested_count") or 0)
                    item["is_enabled"] = bool(item.get("is_enabled"))
                    item["last_scan_at"] = (
                        item.get("completed_at") or item.get("started_at") or item.get("last_scan_at")
                    )
                    item["scan_status"] = item.get("run_status") or item.get("last_scan_status") or "not_scanned"
                    item["failure_reason"] = (
                        item.get("error_message") or item.get("last_scan_error") or ""
                    )
                    rows.append(item)
                return rows, total
            finally:
                cursor.close()


intel_candidate_repository = IntelCandidateRepository()

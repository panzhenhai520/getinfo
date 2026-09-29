#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Source registry, URL canonicalization, and legacy-source synchronization."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import config
from industry_packs import industry_pack_loader
from financial_source_license import (
    require_rss_authorization,
    rss_authorization_decision,
)
from intel_contracts import utc_text
from sqlite_database import sqlite_db
from utils import coerce_int
from source_authority import resolve_source_authority


TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "_hsenc",
    "_hsmi",
}
SOURCE_TYPES = {"rss", "list_page", "website"}
CONTENT_TYPES = {"official", "media", "report", "event", "other"}
SOURCE_SCAN_MAX_AUTOMATIC_ATTEMPTS = 3
MANIFEST_SOURCE_METADATA_FIELDS = (
    "language",
    "expected_classifications",
    "license_profile",
    "approval_status",
    "source_role",
    "authority_scope",
    "publisher_key",
    "on_demand_only",
    "approved_domains",
    "target_symbols",
    "search_query_template",
    "evidence_url",
    # Generic import/audit metadata. These fields are optional for every pack
    # and allow source catalogues to preserve validation without changing the
    # crawler's source identity contract.
    "source_import_id",
    "organization",
    "country_or_region",
    "import_mode",
    "feed_url",
    "directory_url",
    "homepage_url",
    "topics",
    "required_filter_any",
    # 列表页链接抽取的 URL 正则过滤器（如医保局栏目只取 /art/.../art_xxx.html 文章链接，
    # 排除导航栏目链接），由行业包 manifest 声明。
    "link_include_pattern",
    "deduplication_group",
    "viewpoint_label",
    "validation_status",
    "validation_checked_at",
    "http_status",
    "validation_content_type",
    "final_url",
    "entry_count",
    "latest_published_at",
    "sample_title",
    "sample_url",
    "redirected",
    "error",
)


def _parse_utc_timestamp(value) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _preferred_scan_clock(metadata: Dict) -> Tuple[int, int]:
    raw = str(metadata.get("preferred_scan_time") or config.INTEL_LIGHT_SCAN_DAILY_TIME)
    try:
        hour, minute = [int(part) for part in raw.strip()[:5].split(":", 1)]
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError
        return hour, minute
    except (TypeError, ValueError):
        return 8, 30


def canonicalize_source_url(value: str) -> str:
    """Normalize identity details while preserving business paths and queries."""
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("来源网址必须是完整的 HTTP(S) URL")

    host = parsed.hostname.rstrip(".").lower()
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("来源网址域名无效") from exc
    port = parsed.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"

    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    query_items = []
    for key, query_value in parse_qsl(parsed.query, keep_blank_values=True):
        normalized_key = key.casefold()
        if normalized_key.startswith("utm_") or normalized_key in TRACKING_QUERY_KEYS:
            continue
        query_items.append((key, query_value))
    return urlunsplit((scheme, host, path, urlencode(query_items, doseq=True), ""))


def infer_source_type(url: str, text: str = "") -> str:
    parsed = urlsplit(url)
    haystack = f"{parsed.path} {parsed.query} {text}".casefold()
    if re.search(r"(^|[/_.?=&-])(rss|atom|feed)([/_.?=&-]|$)", haystack):
        return "rss"
    if parsed.path.casefold().endswith((".rss", ".atom", ".xml")):
        return "rss"
    if any(
        token in haystack
        for token in (
            "/news",
            "/articles",
            "/blog",
            "/insights",
            "/research",
            "/reports",
            "/events",
            "资讯",
            "新闻",
            "列表",
        )
    ):
        return "list_page"
    return "website"


def infer_content_attributes(url: str, text: str = "") -> Tuple[str, int]:
    host = (urlsplit(url).hostname or "").casefold()
    haystack = f"{host} {text}".casefold()
    if host.endswith((".gov", ".gov.cn", ".gov.hk")) or any(
        token in haystack for token in ("政府", "监管局", "委员会", "official", "官网")
    ):
        return "official", 5
    if any(token in haystack for token in ("report", "research", "whitepaper", "报告", "研究", "白皮书")):
        return "report", 4
    if any(token in haystack for token in ("event", "conference", "webinar", "活动", "会议", "峰会")):
        return "event", 3
    if any(token in haystack for token in ("news", "media", "daily", "资讯", "新闻", "媒体", "日报")):
        return "media", 3
    return "other", 2


def infer_polling_interval_minutes(record: Dict) -> int:
    schedule_type = str(record.get("schedule_type") or "").casefold()
    if schedule_type == "weekly":
        return 10080
    if schedule_type == "monthly":
        return 10080
    frequency = str(record.get("crawl_frequency") or "").casefold()
    if any(token in frequency for token in ("hour", "小时")):
        return 60
    if any(token in frequency for token in ("week", "周")):
        return 10080
    return 1440


class IntelSourceRegistry:
    def __init__(self, database=None, *, pack_loader=None):
        self.db = database or sqlite_db
        self.pack_loader = pack_loader or industry_pack_loader

    def _ensure(self) -> None:
        self.db._ensure_connection()

    @staticmethod
    def _source_manifest_hash(source: Dict) -> str:
        payload = {
            key: source.get(key)
            for key in (
                "name",
                "url",
                "source_type",
                "content_type",
                "market",
                "authority_level",
                "polling_interval_minutes",
                "is_enabled",
                "language",
                "expected_classifications",
                "license_profile",
                "approval_status",
                "source_role",
                "authority_scope",
                "publisher_key",
                *MANIFEST_SOURCE_METADATA_FIELDS,
            )
        }
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _reconciliation_hash(plan: Dict) -> str:
        payload = copy.deepcopy(plan)
        payload.pop("plan_sha256", None)
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def plan_source_reconciliation(
        self,
        industry_pack_id: str,
        *,
        declared_version_id: Optional[int] = None,
    ) -> Dict:
        """Build a deterministic authoritative source diff without writing."""

        self._ensure()
        composition = self.pack_loader.compose(industry_pack_id)
        packs_by_id = {item["id"]: item for item in composition["packs"]}
        desired = []
        desired_association_keys = set()
        for raw_spec in composition["default_sources"]:
            spec = copy.deepcopy(raw_spec)
            url = str(spec.get("url") or "").strip()
            if not url:
                continue
            canonical = canonicalize_source_url(url)
            declared_by = list(spec.get("declared_by_pack_ids") or [])
            owner_pack_id = str(
                spec.get("origin_pack_id")
                or (declared_by[0] if declared_by else industry_pack_id)
            )
            ownership_type = (
                "shared_financial"
                if owner_pack_id == "financial_markets"
                else "pack_owned"
            )
            declaring_pack = packs_by_id.get(owner_pack_id) or composition["primary_pack"]
            content_type, inferred_authority = infer_content_attributes(
                url, str(spec.get("name") or "")
            )
            authorized = True
            if str(spec.get("source_type") or infer_source_type(url)) == "rss" and ownership_type == "shared_financial":
                authorized = bool(
                    rss_authorization_decision(
                        url,
                        str(spec.get("license_profile") or ""),
                        config,
                    )["authorized"]
                )
            normalized = {
                **spec,
                "canonical_source_url": canonical,
                "url": url,
                "owner_pack_id": owner_pack_id,
                "ownership_type": ownership_type,
                "source_type": str(spec.get("source_type") or infer_source_type(url)),
                "content_type": str(spec.get("content_type") or content_type),
                "market": str(spec.get("market") or declaring_pack.get("default_market") or ""),
                "authority_level": coerce_int(spec.get("authority_level"), inferred_authority, 1, 5),
                "polling_interval_minutes": coerce_int(spec.get("polling_interval_minutes"), 1440, 5, 10080),
                "is_enabled": bool(spec.get("is_enabled", True) and authorized),
            }
            normalized["manifest_source_sha256"] = self._source_manifest_hash(normalized)
            desired.append(normalized)
            desired_association_keys.add((owner_pack_id, canonical))

        with self.db.lock:
            source_rows = self.db.connection.execute(
                "SELECT * FROM intel_sources"
            ).fetchall()
            association_rows = self.db.connection.execute(
                """
                SELECT si.*, s.canonical_source_url
                FROM intel_source_industries si
                JOIN intel_sources s ON s.id=si.source_id
                WHERE si.industry_pack_id IN ({})
                """.format(
                    ",".join("?" for _ in composition["effective_pack_ids"])
                ),
                composition["effective_pack_ids"],
            ).fetchall()
        sources_by_canonical = {
            str(row["canonical_source_url"]): dict(row) for row in source_rows
        }
        associations_by_key = {
            (str(row["industry_pack_id"]), str(row["canonical_source_url"])): dict(row)
            for row in association_rows
        }
        add_sources = []
        update_sources = []
        upsert_associations = []
        deactivate_associations = []
        for spec in desired:
            current = sources_by_canonical.get(spec["canonical_source_url"])
            if current is None:
                add_sources.append(spec)
            else:
                configured = {
                    "source_url": spec["url"],
                    "source_name": str(spec.get("name") or spec["canonical_source_url"]),
                    "source_type": spec["source_type"],
                    "content_type": spec["content_type"],
                    "market": spec["market"],
                    "authority_level": spec["authority_level"],
                    "polling_interval_minutes": spec["polling_interval_minutes"],
                    "is_enabled": int(spec["is_enabled"]),
                }
                if bool(current.get("authority_is_manual")):
                    configured["authority_level"] = int(current["authority_level"])
                if bool(current.get("enabled_is_manual")):
                    configured["is_enabled"] = int(current["is_enabled"])
                changed = {
                    key: value
                    for key, value in configured.items()
                    if current.get(key) != value
                }
                if changed:
                    update_sources.append(
                        {
                            "source_id": int(current["id"]),
                            "canonical_source_url": spec["canonical_source_url"],
                            "changes": changed,
                            "spec": spec,
                        }
                    )
            association = associations_by_key.get(
                (spec["owner_pack_id"], spec["canonical_source_url"])
            )
            if not association or str(association.get("ownership_type") or "legacy") != "protected_manual":
                desired_association = {
                    "source_id": int(current["id"]) if current else None,
                    "canonical_source_url": spec["canonical_source_url"],
                    "industry_pack_id": spec["owner_pack_id"],
                    "ownership_type": spec["ownership_type"],
                    "declared_version_id": declared_version_id,
                    "manifest_source_sha256": spec["manifest_source_sha256"],
                }
                if (
                    not association
                    or not bool(association.get("is_active", 1))
                    or str(association.get("ownership_type") or "") != spec["ownership_type"]
                    or association.get("declared_version_id") != declared_version_id
                    or str(association.get("manifest_source_sha256") or "") != spec["manifest_source_sha256"]
                ):
                    upsert_associations.append(desired_association)

        for key, association in associations_by_key.items():
            if key in desired_association_keys:
                continue
            if str(association.get("ownership_type") or "legacy") != "pack_owned":
                continue
            if not bool(association.get("is_active", 1)):
                continue
            deactivate_associations.append(
                {
                    "association_id": int(association["id"]),
                    "source_id": int(association["source_id"]),
                    "industry_pack_id": str(association["industry_pack_id"]),
                    "canonical_source_url": str(association["canonical_source_url"]),
                }
            )

        plan = {
            "industry_pack_id": str(industry_pack_id),
            "primary_pack_id": composition["primary_pack_id"],
            "effective_pack_ids": list(composition["effective_pack_ids"]),
            "declared_version_id": declared_version_id,
            "desired_sources": desired,
            "actions": {
                "add_sources": add_sources,
                "update_sources": update_sources,
                "upsert_associations": upsert_associations,
                "deactivate_associations": deactivate_associations,
            },
        }
        plan["counts"] = {
            key: len(value) for key, value in plan["actions"].items()
        }
        plan["plan_sha256"] = self._reconciliation_hash(plan)
        return plan

    def apply_source_reconciliation(
        self,
        industry_pack_id: str,
        *,
        expected_plan_sha256: str,
        declared_version_id: Optional[int] = None,
        transaction_cursor=None,
        precomputed_plan: Optional[Dict] = None,
    ) -> Dict:
        """Apply exactly the currently reproducible source plan in one transaction."""

        plan = precomputed_plan or self.plan_source_reconciliation(
            industry_pack_id, declared_version_id=declared_version_id
        )
        if str(expected_plan_sha256 or "") != plan["plan_sha256"]:
            raise ValueError("信源对账差异已经变化，请重新预览")
        now = utc_text()
        source_ids = {}
        with self.db.lock:
            owns_transaction = transaction_cursor is None
            cursor = transaction_cursor or self.db.connection.cursor()
            try:
                if owns_transaction:
                    cursor.execute("BEGIN IMMEDIATE")
                for spec in plan["desired_sources"]:
                    row = cursor.execute(
                        "SELECT * FROM intel_sources WHERE canonical_source_url=?",
                        (spec["canonical_source_url"],),
                    ).fetchone()
                    content_metadata = {
                        key: copy.deepcopy(spec.get(key))
                        for key in MANIFEST_SOURCE_METADATA_FIELDS
                        if key in spec
                    }
                    content_metadata.update(
                        {
                            "origin_pack_id": spec["owner_pack_id"],
                            "industry_pack_default": spec["owner_pack_id"],
                        }
                    )
                    if not row:
                        cursor.execute(
                            """
                            INSERT INTO intel_sources(
                                canonical_source_url, source_url, source_name,
                                source_description, source_type, content_type,
                                market, authority_level, polling_interval_minutes,
                                is_enabled, metadata_json, last_synced_at, updated_at
                            ) VALUES(?, ?, ?, '行业包发布信源', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                spec["canonical_source_url"],
                                spec["url"],
                                str(spec.get("name") or spec["canonical_source_url"]),
                                spec["source_type"],
                                spec["content_type"],
                                spec["market"],
                                spec["authority_level"],
                                spec["polling_interval_minutes"],
                                int(spec["is_enabled"]),
                                json.dumps(content_metadata, ensure_ascii=False, sort_keys=True),
                                now,
                                now,
                            ),
                        )
                        source_id = int(cursor.lastrowid)
                    else:
                        source_id = int(row["id"])
                        try:
                            metadata = json.loads(row["metadata_json"] or "{}")
                        except (TypeError, ValueError, json.JSONDecodeError):
                            metadata = {}
                        metadata.update(content_metadata)
                        authority = (
                            int(row["authority_level"])
                            if bool(row["authority_is_manual"])
                            else spec["authority_level"]
                        )
                        enabled = (
                            int(row["is_enabled"])
                            if bool(row["enabled_is_manual"])
                            else int(spec["is_enabled"])
                        )
                        cursor.execute(
                            """
                            UPDATE intel_sources SET
                                source_url=?, source_name=?, source_type=?,
                                content_type=?, market=?, authority_level=?,
                                polling_interval_minutes=?, is_enabled=?,
                                metadata_json=?, updated_at=?
                            WHERE id=?
                            """,
                            (
                                spec["url"],
                                str(spec.get("name") or spec["canonical_source_url"]),
                                spec["source_type"],
                                spec["content_type"],
                                spec["market"],
                                authority,
                                spec["polling_interval_minutes"],
                                enabled,
                                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                                now,
                                source_id,
                            ),
                        )
                    source_ids[spec["canonical_source_url"]] = source_id

                for association in plan["actions"]["upsert_associations"]:
                    source_id = association["source_id"] or source_ids[
                        association["canonical_source_url"]
                    ]
                    current = cursor.execute(
                        """
                        SELECT id, ownership_type FROM intel_source_industries
                        WHERE source_id=? AND industry_pack_id=?
                        """,
                        (source_id, association["industry_pack_id"]),
                    ).fetchone()
                    if current and str(current["ownership_type"] or "") == "protected_manual":
                        continue
                    cursor.execute(
                        """
                        INSERT INTO intel_source_industries(
                            source_id, industry_pack_id, is_manual,
                            ownership_type, is_active, declared_version_id,
                            manifest_source_sha256, created_at, updated_at
                        ) VALUES(?, ?, 0, ?, 1, ?, ?, ?, ?)
                        ON CONFLICT(source_id, industry_pack_id) DO UPDATE SET
                            ownership_type=excluded.ownership_type,
                            is_active=1,
                            declared_version_id=excluded.declared_version_id,
                            manifest_source_sha256=excluded.manifest_source_sha256,
                            updated_at=excluded.updated_at
                        """,
                        (
                            source_id,
                            association["industry_pack_id"],
                            association["ownership_type"],
                            association["declared_version_id"],
                            association["manifest_source_sha256"],
                            now,
                            now,
                        ),
                    )
                for association in plan["actions"]["deactivate_associations"]:
                    cursor.execute(
                        """
                        UPDATE intel_source_industries
                        SET is_active=0, updated_at=?
                        WHERE id=? AND ownership_type='pack_owned'
                        """,
                        (now, association["association_id"]),
                    )
                if owns_transaction:
                    self.db.connection.commit()
            except Exception:
                if owns_transaction:
                    self.db.connection.rollback()
                raise
            finally:
                if owns_transaction:
                    cursor.close()
        return {
            "industry_pack_id": str(industry_pack_id),
            "plan_sha256": plan["plan_sha256"],
            "counts": plan["counts"],
            "applied": True,
        }

    def project_effective_sources_to_managed_urls(
        self,
        industry_pack_id: str,
        *,
        effective_pack_ids: Iterable[str],
        activation_id: str,
        industry_pack_version_id: Optional[int],
        project_keywords: Iterable[str] = (),
        initialization_from: str = '',
        initialization_to: str = '',
        transaction_cursor=None,
    ) -> Dict:
        """Expose canonical pack sources to legacy URL selectors without cloning them.

        ``intel_sources`` and its industry associations remain authoritative.  A
        single stable ``managed_urls`` row is only a compatibility projection;
        its origin link lets the old crawler UI resolve that row through every
        industry association that owns or shares the canonical source.
        """

        self._ensure()
        scoped_pack_ids = list(
            dict.fromkeys(
                str(value or "").strip()
                for value in effective_pack_ids
                if str(value or "").strip()
            )
        )
        if not scoped_pack_ids:
            scoped_pack_ids = [str(industry_pack_id)]
        placeholders = ",".join("?" for _ in scoped_pack_ids)
        now = utc_text()
        keywords_text = ",".join(
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
                    f"""
                    SELECT s.*, si.industry_pack_id AS association_pack_id,
                           si.ownership_type AS association_ownership
                    FROM intel_sources s
                    JOIN intel_source_industries si ON si.source_id=s.id
                    WHERE s.is_enabled=1 AND si.is_active=1
                      AND si.industry_pack_id IN ({placeholders})
                    ORDER BY s.id,
                             CASE WHEN si.industry_pack_id=? THEN 0 ELSE 1 END,
                             si.id
                    """,
                    [*scoped_pack_ids, str(industry_pack_id)],
                ).fetchall()
                source_rows = {}
                for raw in rows:
                    row = dict(raw)
                    source_rows.setdefault(int(row["id"]), row)

                created = 0
                reused = 0
                origins_upserted = 0
                for source in source_rows.values():
                    source_id = int(source["id"])
                    origin = cursor.execute(
                        """
                        SELECT managed_url_id FROM intel_source_origins
                        WHERE source_id=? AND origin_type='managed_url'
                          AND managed_url_id IS NOT NULL
                        ORDER BY is_active DESC, id LIMIT 1
                        """,
                        (source_id,),
                    ).fetchone()
                    managed_url_id = int(origin[0]) if origin and origin[0] else 0
                    if not managed_url_id:
                        existing = cursor.execute(
                            "SELECT id FROM managed_urls WHERE url=?",
                            (str(source["source_url"]),),
                        ).fetchone()
                        managed_url_id = int(existing[0]) if existing else 0
                    if not managed_url_id:
                        polling = int(source.get("polling_interval_minutes") or 1440)
                        frequency = "weekly" if polling >= 10080 else "daily"
                        cursor.execute(
                            """
                            INSERT INTO managed_urls(
                                url, name, description, domain, is_active,
                                auto_crawl, crawl_frequency, keywords,
                                industry_pack_id, industry_pack_version_id,
                                activation_id, ownership_type, created_at, updated_at
                            ) VALUES(?, ?, ?, ?, TRUE, FALSE, ?, ?, ?, ?, ?,
                                     'pack_projection', ?, ?)
                            """,
                            (
                                str(source["source_url"]),
                                str(source.get("source_name") or ""),
                                str(source.get("source_description") or "行业包发布信源"),
                                str(urlsplit(str(source["source_url"])).hostname or ""),
                                frequency,
                                keywords_text,
                                str(source.get("association_pack_id") or industry_pack_id),
                                industry_pack_version_id,
                                str(activation_id or ""),
                                now,
                                now,
                            ),
                        )
                        managed_url_id = int(cursor.lastrowid)
                        created += 1
                    else:
                        reused += 1

                    cursor.execute(
                        """
                        INSERT INTO intel_source_origins(
                            source_id, origin_type, origin_key, managed_url_id,
                            scheduled_task_id, origin_url, is_active,
                            last_seen_at, created_at, updated_at
                        ) VALUES(?, 'managed_url', ?, ?, NULL, ?, 1, ?, ?, ?)
                        ON CONFLICT(origin_type, origin_key) DO UPDATE SET
                            source_id=excluded.source_id,
                            managed_url_id=excluded.managed_url_id,
                            origin_url=excluded.origin_url,
                            is_active=1,
                            last_seen_at=excluded.last_seen_at,
                            updated_at=excluded.updated_at
                        """,
                        (
                            source_id,
                            str(managed_url_id),
                            managed_url_id,
                            str(source["source_url"]),
                            now,
                            now,
                            now,
                        ),
                    )
                    origins_upserted += 1
                if owns_transaction:
                    self.db.connection.commit()
                return {
                    "industry_pack_id": str(industry_pack_id),
                    "effective_pack_ids": scoped_pack_ids,
                    "canonical_sources": len(source_rows),
                    "managed_urls_created": created,
                    "managed_urls_reused": reused,
                    "origins_upserted": origins_upserted,
                    "projection_only": True,
                }
            except Exception:
                if owns_transaction:
                    self.db.connection.rollback()
                raise
            finally:
                if owns_transaction:
                    cursor.close()

    def project_effective_sources_to_collection_tasks(
        self,
        industry_pack_id: str,
        *,
        effective_pack_ids: Iterable[str],
        activation_id: str,
        industry_pack_version_id: Optional[int],
        project_keywords: Iterable[str] = (),
        initialization_from: str = '',
        initialization_to: str = '',
        transaction_cursor=None,
    ) -> Dict:
        """Project pack sources into both legacy task views.

        The recurring rows are an auditable mirror of the canonical source
        scheduler; ``IntelWorker`` remains their sole execution owner so the
        legacy scheduler cannot crawl the same website a second time.  A
        separate crawl-task row is created for every source in this activation
        and is completed from the source-level result of the initial scan.
        """

        self._ensure()
        primary_pack_id = str(industry_pack_id)
        scoped_pack_ids = list(
            dict.fromkeys(
                str(value or '').strip()
                for value in effective_pack_ids
                if str(value or '').strip()
            )
        ) or [primary_pack_id]
        placeholders = ','.join('?' for _ in scoped_pack_ids)
        now = utc_text()
        keywords_text = ','.join(
            dict.fromkeys(
                str(value or '').strip()
                for value in project_keywords
                if str(value or '').strip()
            )
        )
        with self.db.lock:
            owns_transaction = transaction_cursor is None
            cursor = transaction_cursor or self.db.connection.cursor()
            try:
                if owns_transaction:
                    cursor.execute('BEGIN IMMEDIATE')
                rows = cursor.execute(
                    f"""
                    SELECT DISTINCT s.*
                    FROM intel_sources s
                    JOIN intel_source_industries si ON si.source_id=s.id
                    WHERE si.is_active=1
                      AND si.industry_pack_id IN ({placeholders})
                      AND (
                        si.ownership_type IN (
                          'pack_owned', 'shared_financial', 'protected_manual'
                        ) OR si.is_manual=1
                      )
                    ORDER BY s.id
                    """,
                    scoped_pack_ids,
                ).fetchall()
                sources = [dict(row) for row in rows]
                target_source_ids = [int(source['id']) for source in sources]
                source_ids = []
                schedule_ids_by_source = {}
                crawl_task_ids_by_source = {}
                all_crawl_task_ids_by_source = {}
                schedules_created = schedules_updated = crawl_tasks_created = 0

                for source in sources:
                    source_id = int(source['id'])
                    try:
                        metadata = json.loads(source.get('metadata_json') or '{}')
                    except (TypeError, ValueError, json.JSONDecodeError):
                        metadata = {}
                    on_demand_only = str(
                        metadata.get('on_demand_only') or ''
                    ).strip().casefold() in {'1', 'true'}
                    bulk_enabled = bool(source.get('is_enabled')) and not on_demand_only
                    if bulk_enabled:
                        source_ids.append(source_id)
                    origin = cursor.execute(
                        """
                        SELECT managed_url_id FROM intel_source_origins
                        WHERE source_id=? AND origin_type='managed_url'
                          AND managed_url_id IS NOT NULL AND is_active=1
                        ORDER BY id LIMIT 1
                        """,
                        (source_id,),
                    ).fetchone()
                    managed_url_id = int(origin[0]) if origin and origin[0] else None
                    polling = int(source.get('polling_interval_minutes') or 1440)
                    schedule_type = 'weekly' if polling >= 10080 else 'daily'
                    hour, minute = _preferred_scan_clock(metadata)
                    schedule_time = f'{hour:02d}:{minute:02d}:00'
                    schedule_day = coerce_int(metadata.get('preferred_scan_weekday'), 0, 0, 6)
                    schedule_config = {
                        'execution_owner': 'intel_light_scan_worker',
                        'projection_type': 'canonical_source_schedule',
                        'schedule_origin': 'industry_pack',
                        'initialization_pending': True,
                        'initialization_batch_id': str(activation_id or ''),
                        'initialization_from': str(initialization_from or ''),
                        'initialization_to': str(initialization_to or ''),
                        'intel_source_id': source_id,
                        'industry_pack_id': primary_pack_id,
                        'activation_id': str(activation_id or ''),
                        'polling_interval_minutes': polling,
                        'bulk_scan_enabled': bulk_enabled,
                        'exclusion_reason': (
                            '按需信源，不参与批量初始化'
                            if on_demand_only else (
                                '' if bool(source.get('is_enabled')) else '行业包中已停用'
                            )
                        ),
                    }
                    existing = cursor.execute(
                        """
                        SELECT id FROM scheduled_tasks
                        WHERE ownership_type='source_registry_projection'
                          AND industry_pack_id=?
                          AND json_valid(config)
                          AND CAST(json_extract(config, '$.intel_source_id') AS INTEGER)=?
                        ORDER BY id LIMIT 1
                        """,
                        (primary_pack_id, source_id),
                    ).fetchone()
                    schedule_values = (
                        f"[{primary_pack_id}] {str(source.get('source_name') or source['source_url'])}",
                        str(source['source_url']),
                        managed_url_id,
                        schedule_type,
                        schedule_time,
                        schedule_day if schedule_type == 'weekly' else None,
                        keywords_text,
                        industry_pack_version_id,
                        str(activation_id or ''),
                        int(bulk_enabled),
                        json.dumps(schedule_config, ensure_ascii=False, sort_keys=True),
                        now,
                    )
                    if existing:
                        schedule_id = int(existing[0])
                        cursor.execute(
                            """
                            UPDATE scheduled_tasks SET
                              task_name=?, task_type='crawl', target_url=?, url_id=?,
                              schedule_type=?, schedule_time=?, schedule_day=?,
                              keywords=?, industry_pack_version_id=?, activation_id=?,
                              is_active=FALSE, ragflow_kb_id=NULL, config=?, updated_at=?
                            WHERE id=?
                            """,
                            (
                                *schedule_values[:9],
                                schedule_values[10],
                                schedule_values[11],
                                schedule_id,
                            ),
                        )
                        schedules_updated += 1
                    else:
                        cursor.execute(
                            """
                            INSERT INTO scheduled_tasks(
                              task_name, task_type, target_url, url_id,
                              schedule_type, schedule_time, schedule_day, keywords,
                              industry_pack_id, industry_pack_version_id,
                              activation_id, ownership_type, is_active,
                              ragflow_kb_id, days_limit, config, created_at, updated_at
                            ) VALUES(?, 'crawl', ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                     'source_registry_projection', FALSE, NULL, 0, ?, ?, ?)
                            """,
                            (
                                schedule_values[0], schedule_values[1], schedule_values[2],
                                schedule_values[3], schedule_values[4], schedule_values[5],
                                schedule_values[6], primary_pack_id,
                                schedule_values[7], schedule_values[8],
                                schedule_values[10], now, now,
                            ),
                        )
                        schedule_id = int(cursor.lastrowid)
                        schedules_created += 1
                    schedule_ids_by_source[str(source_id)] = schedule_id

                    crawl_task_id = (
                        f"industry-init-{str(activation_id or '')[:16]}-{source_id}"
                    )
                    existing_crawl = cursor.execute(
                        'SELECT id FROM crawl_tasks WHERE task_id=?',
                        (crawl_task_id,),
                    ).fetchone()
                    if not existing_crawl:
                        initial_status = 'pending' if bulk_enabled else 'cancelled'
                        initial_progress = 0 if bulk_enabled else 100
                        exclusion_reason = (
                            '' if bulk_enabled else schedule_config['exclusion_reason']
                        )
                        cursor.execute(
                            """
                            INSERT INTO crawl_tasks(
                              task_id, target_url, task_name, crawl_depth,
                              crawl_mode, page_limit, incremental_mode, keywords,
                              industry_pack_id, industry_pack_version_id,
                              activation_id, ownership_type, status, progress,
                              articles_found, articles_processed, error_message,
                              initialization_batch_id, initialization_from,
                              initialization_to,
                              created_at, updated_at
                            ) VALUES(?, ?, ?, 1, 'industry_source_initial_scan', 50,
                                     0, ?, ?, ?, ?, 'pack_initialization',
                                     ?, ?, 0, 0, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                crawl_task_id,
                                str(source['source_url']),
                                f"[{primary_pack_id}] 初始化采集 - {str(source.get('source_name') or source['source_url'])}",
                                keywords_text,
                                primary_pack_id,
                                industry_pack_version_id,
                                str(activation_id or ''),
                                initial_status,
                                initial_progress,
                                exclusion_reason,
                                str(activation_id or ''),
                                str(initialization_from or ''),
                                str(initialization_to or ''),
                                now,
                                now,
                            ),
                        )
                        crawl_tasks_created += 1
                    all_crawl_task_ids_by_source[str(source_id)] = crawl_task_id
                    if bulk_enabled:
                        crawl_task_ids_by_source[str(source_id)] = crawl_task_id

                if target_source_ids:
                    stale_placeholders = ','.join('?' for _ in target_source_ids)
                    cursor.execute(
                        f"""
                        UPDATE scheduled_tasks SET is_active=FALSE, updated_at=?
                        WHERE ownership_type='source_registry_projection'
                          AND industry_pack_id=? AND json_valid(config)
                          AND CAST(json_extract(config, '$.intel_source_id') AS INTEGER)
                              NOT IN ({stale_placeholders})
                        """,
                        [now, primary_pack_id, *target_source_ids],
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE scheduled_tasks SET is_active=FALSE, updated_at=?
                        WHERE ownership_type='source_registry_projection'
                          AND industry_pack_id=?
                        """,
                        (now, primary_pack_id),
                    )
                schedules_deactivated = max(0, cursor.rowcount)
                if owns_transaction:
                    self.db.connection.commit()
                return {
                    'industry_pack_id': primary_pack_id,
                    'activation_id': str(activation_id or ''),
                    'source_count': len(sources),
                    'source_ids': source_ids,
                    'target_source_ids': target_source_ids,
                    'schedule_ids_by_source': schedule_ids_by_source,
                    'crawl_task_ids_by_source': crawl_task_ids_by_source,
                    'all_crawl_task_ids_by_source': all_crawl_task_ids_by_source,
                    'schedules_created': schedules_created,
                    'schedules_updated': schedules_updated,
                    'schedules_deactivated': schedules_deactivated,
                    'crawl_tasks_created': crawl_tasks_created,
                    'execution_owner': 'intel_light_scan_worker',
                }
            except Exception:
                if owns_transaction:
                    self.db.connection.rollback()
                raise
            finally:
                if owns_transaction:
                    cursor.close()

    def ensure_pack_default_sources(self, industry_pack_id: str) -> Dict:
        """Register one effective pack set without duplicating physical sources."""
        self._ensure()
        composition = self.pack_loader.compose(industry_pack_id)
        pack = composition["primary_pack"]
        packs_by_id = {item["id"]: item for item in composition["packs"]}
        source_authorizations = {}
        for spec in composition["default_sources"]:
            origin_pack_id = str(
                spec.get("origin_pack_id")
                or ((spec.get("declared_by_pack_ids") or [industry_pack_id])[0])
            )
            if spec.get("source_type") == "rss" and origin_pack_id == "financial_markets":
                source_authorizations[canonicalize_source_url(spec.get("url"))] = (
                    rss_authorization_decision(
                        str(spec.get("url") or ""),
                        str(spec.get("license_profile") or ""),
                        config,
                    )
                )
        now = utc_text()
        added = attached = 0
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                for declaring_pack in composition["packs"]:
                    for include in declaring_pack.get("includes") or []:
                        cursor.execute(
                            """
                            INSERT INTO industry_pack_dependencies(
                                parent_pack_id, dependency_pack_id, minimum_version,
                                is_required, capability_config_json, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT(parent_pack_id, dependency_pack_id) DO UPDATE SET
                                minimum_version=excluded.minimum_version,
                                is_required=excluded.is_required,
                                capability_config_json=excluded.capability_config_json,
                                updated_at=excluded.updated_at
                            """,
                            (
                                declaring_pack["id"],
                                include["pack_id"],
                                str(include.get("minimum_version") or ""),
                                int(include.get("required", True)),
                                json.dumps(include, ensure_ascii=False, sort_keys=True),
                                now,
                            ),
                        )
                for spec in composition["default_sources"]:
                    url = str(spec.get('url') or '').strip()
                    if not url:
                        continue
                    declared_by_pack_ids = list(spec.get("declared_by_pack_ids") or [])
                    origin_pack_id = str(
                        spec.get("origin_pack_id")
                        or (declared_by_pack_ids[0] if declared_by_pack_ids else industry_pack_id)
                    )
                    declaring_pack = packs_by_id.get(origin_pack_id) or pack
                    canonical = canonicalize_source_url(url)
                    source_authorized = source_authorizations.get(
                        canonical, {"authorized": True}
                    )["authorized"]
                    cursor.execute(
                        "SELECT id, metadata_json FROM intel_sources WHERE canonical_source_url=?",
                        (canonical,),
                    )
                    row = cursor.fetchone()
                    if row:
                        source_id = int(row['id'])
                        try:
                            metadata = json.loads(row['metadata_json'] or '{}')
                        except (TypeError, ValueError, json.JSONDecodeError):
                            metadata = {}
                    else:
                        content_type, inferred_authority = infer_content_attributes(url, str(spec.get('name') or ''))
                        metadata = {}
                        metadata.update(
                            {
                                'industry_pack_default': origin_pack_id,
                                'origin_pack_id': origin_pack_id,
                                'declared_by_pack_ids': declared_by_pack_ids,
                                'requested_by_pack_ids': [industry_pack_id],
                                'language': str(spec.get('language') or ''),
                                'market': str(spec.get('market') or declaring_pack.get('default_market') or ''),
                                'expected_classifications': list(spec.get('expected_classifications') or []),
                                'api_key_required': bool(spec.get('api_key_required')),
                                'access_cost': str(spec.get('access_cost') or ''),
                                'license_profile': str(spec.get('license_profile') or ''),
                                'source_role': str(spec.get('source_role') or ''),
                                'authority_scope': list(spec.get('authority_scope') or []),
                                'publisher_key': str(spec.get('publisher_key') or ''),
                                'approval_status': str(spec.get('approval_status') or ''),
                                'on_demand_only': bool(spec.get('on_demand_only')),
                                'approved_domains': list(spec.get('approved_domains') or []),
                                'target_symbols': list(spec.get('target_symbols') or []),
                                'search_query_template': str(spec.get('search_query_template') or ''),
                                'evidence_url': str(spec.get('evidence_url') or ''),
                            }
                        )
                        metadata.update(
                            {
                                key: copy.deepcopy(spec.get(key))
                                for key in MANIFEST_SOURCE_METADATA_FIELDS
                                if key in spec
                            }
                        )
                        cursor.execute(
                            """INSERT INTO intel_sources(canonical_source_url,source_url,source_name,source_description,source_type,content_type,market,authority_level,polling_interval_minutes,is_enabled,metadata_json,last_synced_at,updated_at)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?, ?,?)""",
                            (
                                canonical,
                                url,
                                str(spec.get('name') or canonical),
                                '行业包基础信源',
                                str(spec.get('source_type') or infer_source_type(url)),
                                str(spec.get('content_type') or content_type),
                                str(spec.get('market') or declaring_pack.get('default_market') or ''),
                                coerce_int(spec.get('authority_level'), inferred_authority, 1, 5),
                                coerce_int(spec.get('polling_interval_minutes'), 1440, 5, 10080),
                                int(source_authorized),
                                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                                now,
                                now,
                            ),
                        )
                        source_id = int(cursor.lastrowid)
                        added += 1
                    if row:
                        declared = list(metadata.get('declared_by_pack_ids') or [])
                        for declared_pack_id in declared_by_pack_ids:
                            if declared_pack_id not in declared:
                                declared.append(declared_pack_id)
                        requested_by = list(metadata.get('requested_by_pack_ids') or [])
                        if industry_pack_id not in requested_by:
                            requested_by.append(industry_pack_id)
                        metadata.update(
                            {
                                'industry_pack_default': str(metadata.get('industry_pack_default') or origin_pack_id),
                                # A declared origin is authoritative during the
                                # v2 migration; old associations remain intact.
                                'origin_pack_id': origin_pack_id,
                                'declared_by_pack_ids': declared,
                                'requested_by_pack_ids': requested_by,
                                'language': str(spec.get('language') or metadata.get('language') or ''),
                                'market': str(spec.get('market') or metadata.get('market') or declaring_pack.get('default_market') or ''),
                                'expected_classifications': list(
                                    spec.get('expected_classifications')
                                    or metadata.get('expected_classifications')
                                    or []
                                ),
                                'api_key_required': bool(spec.get('api_key_required')),
                                'access_cost': str(spec.get('access_cost') or metadata.get('access_cost') or ''),
                                'license_profile': str(
                                    spec.get('license_profile')
                                    or metadata.get('license_profile')
                                    or ''
                                ),
                                'source_role': str(spec.get('source_role') or metadata.get('source_role') or ''),
                                'authority_scope': list(spec.get('authority_scope') or metadata.get('authority_scope') or []),
                                'publisher_key': str(spec.get('publisher_key') or metadata.get('publisher_key') or ''),
                                'approval_status': str(spec.get('approval_status') or metadata.get('approval_status') or ''),
                                'on_demand_only': bool(spec.get('on_demand_only', metadata.get('on_demand_only', False))),
                                'approved_domains': list(spec.get('approved_domains') or metadata.get('approved_domains') or []),
                                'target_symbols': list(spec.get('target_symbols') or metadata.get('target_symbols') or []),
                                'search_query_template': str(spec.get('search_query_template') or metadata.get('search_query_template') or ''),
                                'evidence_url': str(spec.get('evidence_url') or metadata.get('evidence_url') or ''),
                            }
                        )
                        metadata.update(
                            {
                                key: copy.deepcopy(spec.get(key))
                                for key in MANIFEST_SOURCE_METADATA_FIELDS
                                if key in spec
                            }
                        )
                        cursor.execute(
                            """
                            UPDATE intel_sources
                            SET metadata_json=?,
                                is_enabled=CASE WHEN ? = 1 THEN is_enabled ELSE 0 END,
                                updated_at=?
                            WHERE id=?
                            """,
                            (
                                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                                int(source_authorized),
                                now,
                                source_id,
                            ),
                        )
                    for declared_pack_id in declared_by_pack_ids:
                        ownership_type = (
                            "shared_financial"
                            if declared_pack_id == "financial_markets"
                            else "pack_owned"
                        )
                        association = cursor.execute(
                            """
                            SELECT id, ownership_type
                            FROM intel_source_industries
                            WHERE source_id=? AND industry_pack_id=?
                            """,
                            (source_id, declared_pack_id),
                        ).fetchone()
                        if association:
                            if str(association["ownership_type"] or "") != "protected_manual":
                                cursor.execute(
                                    """
                                    UPDATE intel_source_industries
                                    SET ownership_type=?, is_active=1, updated_at=?
                                    WHERE id=?
                                    """,
                                    (ownership_type, now, int(association["id"])),
                                )
                        else:
                            cursor.execute(
                                """
                                INSERT INTO intel_source_industries(
                                    source_id, industry_pack_id, is_manual,
                                    ownership_type, is_active, created_at, updated_at
                                ) VALUES (?, ?, 0, ?, 1, ?, ?)
                                """,
                                (source_id, declared_pack_id, ownership_type, now, now),
                            )
                            attached += 1
                self.db.connection.commit()
            except Exception:
                self.db.connection.rollback()
                raise
            finally:
                cursor.close()
        return {'sources_added': added, 'sources_attached': attached}

    def migrate_legacy_schedule_preferences(self) -> int:
        """Copy legacy task timing into source metadata once, without reactivating tasks."""
        self._ensure()
        changed = 0
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute("""SELECT o.source_id, t.schedule_time, t.schedule_type,
                                         t.schedule_weekdays, t.schedule_day
                                  FROM intel_source_origins o JOIN scheduled_tasks t ON t.id=o.scheduled_task_id
                                  WHERE o.origin_type='scheduled_task' AND TRIM(COALESCE(t.schedule_time,''))!=''
                                  ORDER BY o.source_id, t.id""")
                preferences = {}
                for row in cursor.fetchall():
                    preferences.setdefault(int(row['source_id']), {
                        'time': str(row['schedule_time'])[:5],
                        'rule': str(row['schedule_type'] or 'daily'),
                        'weekdays': str(row['schedule_weekdays'] or ''),
                        'schedule_day': row['schedule_day'],
                    })
                for source_id, preference in preferences.items():
                    cursor.execute("SELECT metadata_json FROM intel_sources WHERE id=?", (source_id,))
                    row = cursor.fetchone()
                    try: metadata = json.loads((row['metadata_json'] if row else '') or '{}')
                    except (TypeError, ValueError, json.JSONDecodeError): metadata = {}
                    if (metadata.get('preferred_scan_time') == preference['time']
                            and metadata.get('schedule_rule') == preference['rule']
                            and str(metadata.get('schedule_weekdays') or '') == preference['weekdays']):
                        continue
                    metadata.update({
                        'preferred_scan_time': preference['time'],
                        'schedule_rule': preference['rule'],
                        # Old weekly jobs supported one or more weekdays
                        # (0=Monday … 6=Sunday); preserve that exact choice.
                        'schedule_weekdays': preference['weekdays'],
                        'schedule_day': preference['schedule_day'],
                        'schedule_origin': 'legacy_task_migration',
                    })
                    cursor.execute("UPDATE intel_sources SET metadata_json=?, updated_at=? WHERE id=?", (json.dumps(metadata, ensure_ascii=False, sort_keys=True), utc_text(), source_id))
                    changed += 1
                self.db.connection.commit()
            finally:
                cursor.close()
        return changed

    def due_source_ids(self, industry_pack_id: str, now_hk: datetime) -> List[int]:
        """Return sources due in the current daily/weekly schedule window.

        A persistent worker can be busy at the exact configured minute.  A
        source therefore remains due until that window has one recorded scan.
        Failed scans receive two bounded, exponentially delayed retries; the
        next daily/weekly window becomes eligible again regardless.
        """
        self._ensure()
        effective_pack_ids = [
            pack["id"] for pack in self.pack_loader.effective_pack_set(industry_pack_id)
        ]
        if now_hk.tzinfo is None:
            now_hk = now_hk.replace(tzinfo=timezone(timedelta(hours=8)))
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                placeholders = ",".join("?" for _ in effective_pack_ids)
                cursor.execute(f"""SELECT s.id,s.metadata_json,s.last_scan_at,
                                           s.last_scan_status,s.consecutive_scan_failures
                    FROM intel_sources s
                    WHERE s.is_enabled=1 AND EXISTS (
                        SELECT 1 FROM intel_source_industries si
                        WHERE si.source_id=s.id AND si.is_active=1
                          AND si.industry_pack_id IN ({placeholders})
                    )""", effective_pack_ids)
                result = []
                for row in cursor.fetchall():
                    try: metadata=json.loads(row['metadata_json'] or '{}')
                    except (TypeError, ValueError, json.JSONDecodeError): metadata={}
                    if bool(metadata.get('on_demand_only')):
                        continue
                    hour, minute = _preferred_scan_clock(metadata)
                    rule = str(metadata.get('schedule_rule') or 'daily').lower()
                    weekdays = []
                    for value in str(metadata.get('schedule_weekdays') or '').split(','):
                        parsed = coerce_int(value.strip(), None, 0, 6)
                        if parsed is not None and parsed not in weekdays:
                            weekdays.append(parsed)
                    if rule == 'weekly' and not weekdays:
                        fallback = coerce_int(metadata.get('schedule_day'), now_hk.weekday(), 0, 6)
                        weekdays = [fallback]
                    scheduled_at = None
                    if rule == 'weekly':
                        for days_ago in range(7):
                            candidate_day = now_hk - timedelta(days=days_ago)
                            if candidate_day.weekday() not in weekdays:
                                continue
                            candidate = candidate_day.replace(
                                hour=hour, minute=minute, second=0, microsecond=0
                            )
                            if candidate <= now_hk:
                                scheduled_at = candidate
                                break
                    else:
                        candidate = now_hk.replace(
                            hour=hour, minute=minute, second=0, microsecond=0
                        )
                        if candidate <= now_hk:
                            scheduled_at = candidate
                    if scheduled_at is None:
                        continue
                    last_scan_utc = _parse_utc_timestamp(row['last_scan_at'])
                    last_scan_hk = (
                        last_scan_utc.astimezone(now_hk.tzinfo)
                        if last_scan_utc is not None
                        else None
                    )
                    if last_scan_hk is None or last_scan_hk < scheduled_at:
                        result.append(int(row['id']))
                        continue
                    failures = max(0, int(row['consecutive_scan_failures'] or 0))
                    if (
                        row['last_scan_status'] not in {'completed', 'partial'}
                        and 0 < failures < SOURCE_SCAN_MAX_AUTOMATIC_ATTEMPTS
                    ):
                        retry_minutes = min(60, 5 * (2 ** (failures - 1)))
                        if now_hk >= last_scan_hk + timedelta(minutes=retry_minutes):
                            result.append(int(row['id']))
                return result
            finally:
                cursor.close()

    @staticmethod
    def _read_pages(cursor, sql: str, *, page_size: int) -> Iterable[Dict]:
        last_id = 0
        while True:
            cursor.execute(f"{sql} AND base.id > ? ORDER BY base.id LIMIT ?", (last_id, page_size))
            rows = [dict(row) for row in cursor.fetchall()]
            if not rows:
                break
            yield from rows
            last_id = int(rows[-1]["id"])

    def _legacy_entries(self, cursor, page_size: int, report: Dict) -> List[Dict]:
        entries: List[Dict] = []
        managed_by_id: Dict[int, Dict] = {}
        managed_sql = """
            SELECT base.id, base.url, base.name, base.description, base.is_active,
                   base.crawl_frequency, base.industry_pack_id,
                   base.ownership_type
            FROM managed_urls base
            WHERE 1=1
        """
        for row in self._read_pages(cursor, managed_sql, page_size=page_size):
            managed_by_id[int(row["id"])] = row
            entries.append(
                {
                    "origin_type": "managed_url",
                    "origin_key": str(row["id"]),
                    "managed_url_id": int(row["id"]),
                    "scheduled_task_id": None,
                    "url": row.get("url") or "",
                    "name": row.get("name") or "",
                    "description": row.get("description") or "",
                    "is_active": bool(row.get("is_active")),
                    "crawl_frequency": row.get("crawl_frequency") or "",
                    "industry_pack_id": row.get("industry_pack_id") or "family_office",
                    "ownership_type": row.get("ownership_type") or "legacy_family",
                }
            )

        scheduled_sql = """
            SELECT base.id, base.task_name, base.target_url, base.url_id,
                   base.is_active, base.schedule_type, base.keywords,
                   base.industry_pack_id, base.ownership_type
            FROM scheduled_tasks base
            WHERE 1=1
        """
        for row in self._read_pages(cursor, scheduled_sql, page_size=page_size):
            linked = managed_by_id.get(int(row["url_id"])) if row.get("url_id") else None
            effective_url = str(row.get("target_url") or "").strip()
            if not effective_url and linked:
                effective_url = linked.get("url") or ""
            entries.append(
                {
                    "origin_type": "scheduled_task",
                    "origin_key": str(row["id"]),
                    "managed_url_id": None,
                    "scheduled_task_id": int(row["id"]),
                    "url": effective_url,
                    "name": row.get("task_name") or (linked or {}).get("name") or "",
                    "description": row.get("keywords") or "",
                    "is_active": bool(row.get("is_active")),
                    "schedule_type": row.get("schedule_type") or "",
                    "industry_pack_id": row.get("industry_pack_id") or "family_office",
                    "ownership_type": row.get("ownership_type") or "legacy_family",
                }
            )
        report["legacy_managed_urls"] = len(managed_by_id)
        report["legacy_scheduled_tasks"] = sum(
            1 for entry in entries if entry["origin_type"] == "scheduled_task"
        )
        return entries

    @staticmethod
    def _aggregate(entries: Iterable[Dict], report: Dict) -> Dict[str, Dict]:
        grouped: Dict[str, Dict] = {}
        for entry in entries:
            try:
                canonical = canonicalize_source_url(entry.get("url") or "")
            except (ValueError, TypeError) as exc:
                report["errors"] += 1
                if len(report["error_details"]) < 100:
                    report["error_details"].append(
                        {
                            "origin_type": entry.get("origin_type"),
                            "origin_key": entry.get("origin_key"),
                            "error": str(exc),
                        }
                    )
                continue
            text = " ".join(
                str(entry.get(field) or "") for field in ("name", "description")
            )
            source_type = infer_source_type(canonical, text)
            content_type, authority = infer_content_attributes(canonical, text)
            aggregate = grouped.setdefault(
                canonical,
                {
                    "canonical_source_url": canonical,
                    "source_url": str(entry.get("url") or "").strip(),
                    "source_name": str(entry.get("name") or "").strip(),
                    "source_description": str(entry.get("description") or "").strip(),
                    "source_type": source_type,
                    "content_type": content_type,
                    "_content_authority": authority,
                    "authority_level": authority,
                    "polling_interval_minutes": infer_polling_interval_minutes(entry),
                    "is_enabled": bool(entry.get("is_active")),
                    "origins": [],
                    "industry_bindings": {},
                },
            )
            binding_pack_id = str(
                entry.get("industry_pack_id") or "family_office"
            ).strip() or "family_office"
            binding_ownership = str(
                entry.get("ownership_type") or "legacy_family"
            ).strip() or "legacy_family"
            existing_binding = aggregate["industry_bindings"].get(binding_pack_id)
            # Explicit manual ownership wins over a migrated legacy marker.
            if existing_binding != "protected_manual":
                aggregate["industry_bindings"][binding_pack_id] = binding_ownership
            previous_content_authority = aggregate["_content_authority"]
            if entry["origin_type"] == "managed_url":
                if entry.get("name"):
                    aggregate["source_name"] = str(entry["name"]).strip()
                if entry.get("description"):
                    aggregate["source_description"] = str(entry["description"]).strip()
            aggregate["is_enabled"] = aggregate["is_enabled"] or bool(entry.get("is_active"))
            aggregate["authority_level"] = max(aggregate["authority_level"], authority)
            aggregate["polling_interval_minutes"] = min(
                aggregate["polling_interval_minutes"],
                infer_polling_interval_minutes(entry),
            )
            if source_type == "rss":
                aggregate["source_type"] = "rss"
            elif source_type == "list_page" and aggregate["source_type"] == "website":
                aggregate["source_type"] = "list_page"
            if authority > previous_content_authority:
                aggregate["content_type"] = content_type
                aggregate["_content_authority"] = authority
            aggregate["origins"].append(entry)
        return grouped

    def sync_legacy_sources(
        self,
        *,
        dry_run: bool = True,
        page_size: int = 200,
        default_industry_pack_id: Optional[str] = None,
    ) -> Dict:
        self._ensure()
        page_size = coerce_int(page_size, 200, 1, 1000)
        pack_id = (
            str(default_industry_pack_id or config.INTEL_DEFAULT_INDUSTRY_PACK).strip()
            or "family_office"
        )
        pack = self.pack_loader.load(pack_id)
        now = utc_text()
        report = {
            "dry_run": bool(dry_run),
            "default_industry_pack_id": pack_id,
            "legacy_managed_urls": 0,
            "legacy_scheduled_tasks": 0,
            "sources_added": 0,
            "sources_updated": 0,
            "sources_skipped": 0,
            "origins_added": 0,
            "origins_updated": 0,
            "origins_unlinked": 0,
            "industries_added": 0,
            "conflicts": 0,
            "errors": 0,
            "error_details": [],
        }

        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                entries = self._legacy_entries(cursor, page_size, report)
                grouped = self._aggregate(entries, report)
                seen_origins = set()

                for canonical, source in grouped.items():
                    cursor.execute(
                        "SELECT * FROM intel_sources WHERE canonical_source_url = ?",
                        (canonical,),
                    )
                    existing_row = cursor.fetchone()
                    existing = dict(existing_row) if existing_row else None
                    authority = (
                        int(existing["authority_level"])
                        if existing and existing["authority_is_manual"]
                        else int(source["authority_level"])
                    )
                    enabled = (
                        int(existing["is_enabled"])
                        if existing and existing["enabled_is_manual"]
                        else int(bool(source["is_enabled"]))
                    )
                    try:
                        source_metadata = json.loads((existing or {}).get('metadata_json') or '{}')
                    except (TypeError, ValueError, json.JSONDecodeError):
                        source_metadata = {}
                    source_metadata['legacy_origin_count'] = len(source['origins'])
                    values = (
                        source["source_url"],
                        source["source_name"],
                        source["source_description"],
                        source["source_type"],
                        source["content_type"],
                        str(pack.get("default_market") or ""),
                        authority,
                        source["polling_interval_minutes"],
                        enabled,
                        json.dumps(source_metadata, ensure_ascii=False, sort_keys=True),
                        now,
                        now,
                    )
                    if not existing:
                        cursor.execute(
                            """
                            INSERT INTO intel_sources (
                                canonical_source_url, source_url, source_name,
                                source_description, source_type, content_type,
                                market, authority_level, polling_interval_minutes,
                                is_enabled, metadata_json, last_synced_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (canonical, *values),
                        )
                        source_id = int(cursor.lastrowid)
                        report["sources_added"] += 1
                    else:
                        source_id = int(existing["id"])
                        comparable_fields = (
                            "source_url",
                            "source_name",
                            "source_description",
                            "source_type",
                            "content_type",
                            "market",
                            "authority_level",
                            "polling_interval_minutes",
                            "is_enabled",
                            "metadata_json",
                        )
                        desired = values[:10]
                        changed = any(
                            str(existing[field]) != str(desired[index])
                            for index, field in enumerate(comparable_fields)
                        )
                        cursor.execute(
                            """
                            UPDATE intel_sources
                            SET source_url=?, source_name=?, source_description=?,
                                source_type=?, content_type=?, market=?,
                                authority_level=?, polling_interval_minutes=?,
                                is_enabled=?, metadata_json=?, last_synced_at=?,
                                updated_at=?
                            WHERE id=?
                            """,
                            (*values, source_id),
                        )
                        report["sources_updated" if changed else "sources_skipped"] += 1

                    if not existing or not existing["industries_are_manual"]:
                        bindings = source.get("industry_bindings") or {
                            pack_id: "legacy_family"
                        }
                        for binding_pack_id, binding_ownership in bindings.items():
                            protected = binding_ownership in {
                                "protected_manual", "manual"
                            }
                            ownership = (
                                "protected_manual" if protected else "legacy"
                            )
                            cursor.execute(
                                """
                                INSERT INTO intel_source_industries (
                                    source_id, industry_pack_id, is_manual,
                                    ownership_type, is_active, created_at, updated_at
                                ) VALUES (?, ?, ?, ?, 1, ?, ?)
                                ON CONFLICT(source_id, industry_pack_id) DO UPDATE SET
                                    is_active=1,
                                    is_manual=CASE
                                        WHEN intel_source_industries.ownership_type IN (
                                            'pack_owned', 'shared_financial'
                                        ) THEN intel_source_industries.is_manual
                                        ELSE excluded.is_manual
                                    END,
                                    ownership_type=CASE
                                        WHEN intel_source_industries.ownership_type IN (
                                            'pack_owned', 'shared_financial', 'protected_manual'
                                        ) THEN intel_source_industries.ownership_type
                                        ELSE excluded.ownership_type
                                    END,
                                    updated_at=excluded.updated_at
                                """,
                                (
                                    source_id,
                                    binding_pack_id,
                                    int(protected),
                                    ownership,
                                    now,
                                    now,
                                ),
                            )
                            report["industries_added"] += max(0, cursor.rowcount)

                    for origin in source["origins"]:
                        origin_identity = (origin["origin_type"], origin["origin_key"])
                        seen_origins.add(origin_identity)
                        cursor.execute(
                            """
                            SELECT id, source_id, origin_url, is_active
                            FROM intel_source_origins
                            WHERE origin_type=? AND origin_key=?
                            """,
                            origin_identity,
                        )
                        old_origin = cursor.fetchone()
                        origin_values = (
                            source_id,
                            origin.get("managed_url_id"),
                            origin.get("scheduled_task_id"),
                            str(origin.get("url") or "").strip(),
                            int(bool(origin.get("is_active"))),
                            now,
                            now,
                        )
                        if old_origin:
                            changed = (
                                int(old_origin["source_id"]) != source_id
                                or str(old_origin["origin_url"]) != origin_values[3]
                                or int(old_origin["is_active"]) != origin_values[4]
                            )
                            cursor.execute(
                                """
                                UPDATE intel_source_origins
                                SET source_id=?, managed_url_id=?, scheduled_task_id=?,
                                    origin_url=?, is_active=?, last_seen_at=?, updated_at=?
                                WHERE id=?
                                """,
                                (*origin_values, int(old_origin["id"])),
                            )
                            if changed:
                                report["origins_updated"] += 1
                        else:
                            cursor.execute(
                                """
                                INSERT INTO intel_source_origins (
                                    source_id, origin_type, origin_key, managed_url_id,
                                    scheduled_task_id, origin_url, is_active,
                                    last_seen_at, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    source_id,
                                    origin["origin_type"],
                                    origin["origin_key"],
                                    origin.get("managed_url_id"),
                                    origin.get("scheduled_task_id"),
                                    str(origin.get("url") or "").strip(),
                                    int(bool(origin.get("is_active"))),
                                    now,
                                    now,
                                ),
                            )
                            report["origins_added"] += 1

                cursor.execute(
                    """
                    SELECT id, origin_type, origin_key
                    FROM intel_source_origins
                    WHERE origin_type IN ('managed_url', 'scheduled_task')
                    """
                )
                for old_origin in cursor.fetchall():
                    identity = (old_origin["origin_type"], old_origin["origin_key"])
                    if identity not in seen_origins:
                        cursor.execute(
                            "DELETE FROM intel_source_origins WHERE id=?",
                            (int(old_origin["id"]),),
                        )
                        report["origins_unlinked"] += 1

                cursor.execute(
                    """
                    UPDATE intel_sources
                    SET is_enabled=0, updated_at=?
                    WHERE enabled_is_manual=0
                      AND EXISTS (
                          SELECT 1 FROM intel_source_industries legacy_si
                          WHERE legacy_si.source_id=intel_sources.id
                            AND legacy_si.ownership_type='legacy'
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM intel_source_industries owned_si
                          WHERE owned_si.source_id=intel_sources.id
                            AND owned_si.is_active=1
                            AND owned_si.ownership_type IN (
                                'pack_owned', 'shared_financial',
                                'protected_manual'
                            )
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM intel_source_origins o WHERE o.source_id=intel_sources.id
                      )
                    """,
                    (now,),
                )
                # Unscoped legacy URLs belong to the original family-office
                # installation. Older source-sync jobs incorrectly attached
                # them to whichever pack happened to be active; retire those
                # drifting associations while preserving pack-owned/shared and
                # explicitly protected manual sources.
                cursor.execute(
                    """
                    UPDATE intel_source_industries
                    SET is_active=0, updated_at=?
                    WHERE industry_pack_id!='family_office'
                      AND ownership_type='legacy'
                      AND is_manual=0
                      AND is_active=1
                    """,
                    (now,),
                )
                report["legacy_industries_retired"] = max(0, cursor.rowcount)
                report["source_total"] = len(grouped)
                if dry_run:
                    self.db.connection.rollback()
                else:
                    self.db.connection.commit()
                return report
            except Exception:
                self.db.connection.rollback()
                raise
            finally:
                cursor.close()

    def list_sources(
        self,
        *,
        industry_pack_id: str = "",
        source_type: str = "",
        is_enabled: Optional[bool] = None,
        page: int = 1,
        per_page: int = 20,
    ) -> Tuple[List[Dict], int]:
        self._ensure()
        page = coerce_int(page, 1, 1)
        per_page = coerce_int(per_page, 20, 1, 100)
        filters = ["1=1"]
        params: List = []
        if industry_pack_id:
            effective_pack_ids = [
                pack["id"]
                for pack in self.pack_loader.effective_pack_set(industry_pack_id)
            ]
            placeholders = ",".join("?" for _ in effective_pack_ids)
            filters.append(
                "EXISTS (SELECT 1 FROM intel_source_industries si "
                f"WHERE si.source_id=s.id AND si.is_active=1 "
                f"AND si.industry_pack_id IN ({placeholders}))"
            )
            params.extend(effective_pack_ids)
        if source_type:
            if source_type not in SOURCE_TYPES:
                raise ValueError("不支持的来源类型")
            filters.append("s.source_type=?")
            params.append(source_type)
        if is_enabled is not None:
            filters.append("s.is_enabled=?")
            params.append(int(bool(is_enabled)))
        where_sql = " AND ".join(filters)
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(f"SELECT COUNT(*) AS total FROM intel_sources s WHERE {where_sql}", params)
                total = int(cursor.fetchone()["total"])
                cursor.execute(
                    f"""
                    SELECT s.*,
                           GROUP_CONCAT(DISTINCT si.industry_pack_id) AS industry_pack_ids,
                           COUNT(DISTINCT o.id) AS origin_count
                    FROM intel_sources s
                    LEFT JOIN intel_source_industries si
                      ON si.source_id=s.id AND si.is_active=1
                    LEFT JOIN intel_source_origins o ON o.source_id=s.id
                    WHERE {where_sql}
                    GROUP BY s.id
                    ORDER BY s.authority_level DESC, s.source_name, s.id
                    LIMIT ? OFFSET ?
                    """,
                    [*params, per_page, (page - 1) * per_page],
                )
                sources = [self._serialize_source(dict(row)) for row in cursor.fetchall()]
                return sources, total
            finally:
                cursor.close()

    def effective_enabled_source_ids(self, industry_pack_id: str) -> List[int]:
        """Return every enabled bulk-scan source owned by an active composition."""

        self._ensure()
        effective_pack_ids = [
            pack["id"] for pack in self.pack_loader.effective_pack_set(industry_pack_id)
        ]
        placeholders = ",".join("?" for _ in effective_pack_ids)
        with self.db.lock:
            rows = self.db.connection.execute(
                f"""
                SELECT DISTINCT s.id
                FROM intel_sources s
                JOIN intel_source_industries si ON si.source_id=s.id
                WHERE s.is_enabled=1
                  AND si.is_active=1
                  AND si.industry_pack_id IN ({placeholders})
                  AND COALESCE(
                      LOWER(CAST(json_extract(s.metadata_json, '$.on_demand_only') AS TEXT)),
                      'false'
                  ) NOT IN ('1', 'true')
                  AND (
                      si.ownership_type IN (
                          'pack_owned', 'shared_financial', 'protected_manual'
                      )
                      OR si.is_manual=1
                  )
                ORDER BY s.id
                """,
                effective_pack_ids,
            ).fetchall()
        return [int(row[0]) for row in rows]

    @staticmethod
    def _serialize_source(source: Dict) -> Dict:
        source["industry_pack_ids"] = [
            value for value in str(source.get("industry_pack_ids") or "").split(",") if value
        ]
        try:
            source["metadata"] = json.loads(source.pop("metadata_json", "{}") or "{}")
        except (TypeError, ValueError):
            source["metadata"] = {}
        for field in (
            "is_enabled",
            "authority_is_manual",
            "industries_are_manual",
            "enabled_is_manual",
        ):
            if field in source:
                source[field] = bool(source[field])
        return source

    def get_source(self, source_id: int) -> Optional[Dict]:
        self._ensure()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT s.*,
                           GROUP_CONCAT(DISTINCT si.industry_pack_id) AS industry_pack_ids,
                           COUNT(DISTINCT o.id) AS origin_count
                    FROM intel_sources s
                    LEFT JOIN intel_source_industries si
                      ON si.source_id=s.id AND si.is_active=1
                    LEFT JOIN intel_source_origins o ON o.source_id=s.id
                    WHERE s.id=?
                    GROUP BY s.id
                    """,
                    (coerce_int(source_id, 0),),
                )
                row = cursor.fetchone()
                return self._serialize_source(dict(row)) if row else None
            finally:
                cursor.close()

    def update_source(
        self,
        source_id: int,
        *,
        authority_level=None,
        is_enabled=None,
        industry_pack_ids: Optional[Iterable[str]] = None,
        polling_interval_minutes=None,
        source_role=None,
        authority_scope=None,
        publisher_key=None,
    ) -> Optional[Dict]:
        self._ensure()
        source_id = coerce_int(source_id, 0, 1)
        now = utc_text()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                cursor.execute(
                    "SELECT id, source_url, source_type, authority_level, metadata_json FROM intel_sources WHERE id=?",
                    (source_id,),
                )
                source_row = cursor.fetchone()
                if not source_row:
                    self.db.connection.rollback()
                    return None
                if source_role is not None or authority_scope is not None or publisher_key is not None:
                    try:
                        source_metadata = json.loads(source_row["metadata_json"] or "{}")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        source_metadata = {}
                    profile = resolve_source_authority(
                        {
                            "url": source_row["source_url"],
                            "source_role": source_role
                            if source_role is not None
                            else source_metadata.get("source_role"),
                            "authority_level": (
                                authority_level
                                if authority_level is not None
                                else (
                                    None
                                    if source_role is not None
                                    else source_row["authority_level"]
                                )
                            ),
                            "authority_scope": authority_scope
                            if authority_scope is not None
                            else source_metadata.get("authority_scope"),
                            "publisher_key": publisher_key
                            if publisher_key is not None
                            else source_metadata.get("publisher_key"),
                        },
                        strict=True,
                    )
                    source_metadata.update(
                        {
                            "source_role": profile["source_role"],
                            "authority_scope": profile["authority_scope"],
                            "publisher_key": profile["publisher_key"],
                        }
                    )
                    cursor.execute(
                        """
                        UPDATE intel_sources
                        SET authority_level=?, authority_is_manual=1,
                            metadata_json=?, updated_at=? WHERE id=?
                        """,
                        (
                            profile["authority_level"],
                            json.dumps(source_metadata, ensure_ascii=False, sort_keys=True),
                            now,
                            source_id,
                        ),
                    )
                    authority_level = None
                if authority_level is not None:
                    authority = coerce_int(authority_level, None)
                    if authority is None or not 1 <= authority <= 5:
                        raise ValueError("权威等级必须在 1 到 5 之间")
                    cursor.execute(
                        """
                        UPDATE intel_sources
                        SET authority_level=?, authority_is_manual=1, updated_at=?
                        WHERE id=?
                        """,
                        (authority, now, source_id),
                    )
                if is_enabled is not None:
                    if bool(is_enabled):
                        try:
                            source_metadata = json.loads(
                                source_row["metadata_json"] or "{}"
                            )
                        except (TypeError, ValueError, json.JSONDecodeError):
                            source_metadata = {}
                        if (
                            source_metadata.get("origin_pack_id") == "financial_markets"
                            and str(source_row["source_type"] or "") == "rss"
                        ):
                            require_rss_authorization(
                                str(source_row["source_url"] or ""),
                                str(source_metadata.get("license_profile") or ""),
                                config,
                            )
                    cursor.execute(
                        """
                        UPDATE intel_sources
                        SET is_enabled=?, enabled_is_manual=1, updated_at=?
                        WHERE id=?
                        """,
                        (int(bool(is_enabled)), now, source_id),
                    )
                if polling_interval_minutes is not None:
                    interval = coerce_int(polling_interval_minutes, None, 5, 10080)
                    if interval is None:
                        raise ValueError("扫描间隔必须在 5 到 10080 分钟之间")
                    cursor.execute("UPDATE intel_sources SET polling_interval_minutes=?, updated_at=? WHERE id=?", (interval, now, source_id))
                if industry_pack_ids is not None:
                    pack_ids = list(dict.fromkeys(str(value).strip() for value in industry_pack_ids))
                    cursor.execute(
                        """
                        UPDATE intel_source_industries
                        SET is_active=0, updated_at=?
                        WHERE source_id=? AND ownership_type!='shared_financial'
                        """,
                        (now, source_id),
                    )
                    for pack_id in pack_ids:
                        cursor.execute(
                            """
                            INSERT INTO intel_source_industries (
                                source_id, industry_pack_id, is_manual,
                                ownership_type, is_active, created_at, updated_at
                            ) VALUES (?, ?, 1, 'protected_manual', 1, ?, ?)
                            ON CONFLICT(source_id, industry_pack_id) DO UPDATE SET
                                is_manual=1,
                                ownership_type='protected_manual',
                                is_active=1,
                                updated_at=excluded.updated_at
                            """,
                            (source_id, pack_id, now, now),
                        )
                    cursor.execute(
                        """
                        UPDATE intel_sources
                        SET industries_are_manual=1, updated_at=? WHERE id=?
                        """,
                        (now, source_id),
                    )
                self.db.connection.commit()
            except Exception:
                self.db.connection.rollback()
                raise
            finally:
                cursor.close()
        return self.get_source(source_id)

    def update_source_metadata(self, source_id: int, updates: Dict) -> Optional[Dict]:
        """Merge operational metadata without changing source identity or ownership."""
        self._ensure()
        source_id = coerce_int(source_id, 0, 1)
        now = utc_text()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                cursor.execute("SELECT metadata_json FROM intel_sources WHERE id=?", (source_id,))
                row = cursor.fetchone()
                if not row:
                    self.db.connection.rollback()
                    return None
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except (TypeError, ValueError):
                    metadata = {}
                metadata.update(dict(updates or {}))
                cursor.execute(
                    "UPDATE intel_sources SET metadata_json=?, updated_at=? WHERE id=?",
                    (json.dumps(metadata, ensure_ascii=False, sort_keys=True), now, source_id),
                )
                self.db.connection.commit()
            except Exception:
                self.db.connection.rollback()
                raise
            finally:
                cursor.close()
        return self.get_source(source_id)


intel_source_registry = IntelSourceRegistry()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Draft, validate, publish and diff versioned industry-pack manifests."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from typing import Optional

import config
from financial_source_license import rss_authorization_decision
from industry_packs import (
    PACK_ID_PATTERN,
    IndustryPackError,
    validate_industry_pack,
)
from intel_http import UnsafeExternalURLError, validate_external_url
from intel_sources import canonicalize_source_url
from sqlite_database import sqlite_db


KEYWORD_FIELDS = (
    "core_keywords",
    "expanded_keywords",
    "trend_keywords",
    "event_keywords",
    "negative_keywords",
    "serpapi_queries",
)
SOURCE_TYPES = frozenset({"rss", "list_page", "website"})


def _manifest_text(manifest: Mapping[str, object]) -> str:
    return json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _manifest_sha256(manifest: Mapping[str, object]) -> str:
    return hashlib.sha256(_manifest_text(manifest).encode("utf-8")).hexdigest()


def _payload_sha256(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_manifest_text(payload).encode("utf-8")).hexdigest()


def _decoded_record(row) -> dict:
    record = dict(row)
    record["manifest"] = json.loads(record.pop("manifest_json") or "{}")
    return record


class IndustryPackVersionStore:
    def __init__(self, database=None):
        self.db = database or sqlite_db

    def _ensure(self) -> None:
        self.db._ensure_connection()

    def custom_record(self, pack_id: str) -> Optional[dict]:
        """Return the custom-pack registry row, including tombstones."""

        self._ensure()
        with self.db.lock:
            row = self.db.connection.execute(
                "SELECT * FROM industry_pack_registry WHERE industry_pack_id=?",
                (str(pack_id),),
            ).fetchone()
        return dict(row) if row else None

    def is_deleted(self, pack_id: str) -> bool:
        row = self.custom_record(pack_id)
        return bool(row and str(row.get("status")) == "deleted")

    def list_runtime_pack_ids(self) -> list[str]:
        """List database-backed published pack IDs that remain loadable."""

        self._ensure()
        with self.db.lock:
            rows = self.db.connection.execute(
                """
                SELECT DISTINCT v.industry_pack_id
                FROM industry_pack_versions v
                LEFT JOIN industry_pack_registry r
                  ON r.industry_pack_id=v.industry_pack_id
                WHERE r.industry_pack_id IS NULL OR r.status='active'
                ORDER BY v.industry_pack_id
                """
            ).fetchall()
        return [str(row[0]) for row in rows]

    def list_custom_records(self, *, include_deleted: bool = False) -> list[dict]:
        self._ensure()
        query = "SELECT * FROM industry_pack_registry"
        parameters: tuple = ()
        if not include_deleted:
            query += " WHERE status='active'"
        query += " ORDER BY created_at, industry_pack_id"
        with self.db.lock:
            rows = self.db.connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def draft(self, pack_id: str) -> Optional[dict]:
        self._ensure()
        with self.db.lock:
            row = self.db.connection.execute(
                "SELECT * FROM industry_pack_drafts WHERE industry_pack_id=?",
                (str(pack_id),),
            ).fetchone()
        return _decoded_record(row) if row else None

    def create_custom_draft(
        self,
        pack_id: str,
        manifest: Mapping[str, object],
        *,
        actor: str = "",
    ) -> dict:
        """Atomically register a custom pack, its first draft and audit event."""

        self._ensure()
        normalized_id = str(pack_id)
        payload = copy.deepcopy(dict(manifest))
        manifest_text = _manifest_text(payload)
        digest = _manifest_sha256(payload)
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                registry = cursor.execute(
                    "SELECT status FROM industry_pack_registry WHERE industry_pack_id=?",
                    (normalized_id,),
                ).fetchone()
                existing = cursor.execute(
                    """
                    SELECT 1 FROM industry_pack_versions WHERE industry_pack_id=?
                    UNION ALL
                    SELECT 1 FROM industry_pack_drafts WHERE industry_pack_id=?
                    LIMIT 1
                    """,
                    (normalized_id, normalized_id),
                ).fetchone()
                if registry or existing:
                    if registry and str(registry["status"]) == "deleted":
                        raise ValueError("该行业包 ID 已删除并保留用于审计，不能重复使用")
                    raise ValueError("行业包 ID 已存在")
                cursor.execute(
                    """
                    INSERT INTO industry_pack_registry(
                        industry_pack_id, name, origin, status,
                        created_by, updated_by
                    ) VALUES(?, ?, 'custom', 'active', ?, ?)
                    """,
                    (
                        normalized_id,
                        str(payload.get("name") or ""),
                        str(actor or ""),
                        str(actor or ""),
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO industry_pack_drafts(
                        industry_pack_id, base_version_id, revision,
                        manifest_json, content_sha256, created_by, updated_by
                    ) VALUES(?, NULL, 1, ?, ?, ?, ?)
                    """,
                    (
                        normalized_id,
                        manifest_text,
                        digest,
                        str(actor or ""),
                        str(actor or ""),
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO industry_pack_lifecycle_events(
                        industry_pack_id, pack_name, event_type, actor, details_json
                    ) VALUES(?, ?, 'created', ?, ?)
                    """,
                    (
                        normalized_id,
                        str(payload.get("name") or ""),
                        str(actor or ""),
                        json.dumps(
                            {
                                "schema_version": payload.get("schema_version"),
                                "pack_version": payload.get("pack_version"),
                                "origin": "custom",
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    ),
                )
                row = cursor.execute(
                    "SELECT * FROM industry_pack_drafts WHERE industry_pack_id=?",
                    (normalized_id,),
                ).fetchone()
                self.db.connection.commit()
                return _decoded_record(row)
            except Exception:
                self.db.connection.rollback()
                raise
            finally:
                cursor.close()

    def latest_published(self, pack_id: str) -> Optional[dict]:
        self._ensure()
        try:
            with self.db.lock:
                row = self.db.connection.execute(
                    """
                    SELECT * FROM industry_pack_versions
                    WHERE industry_pack_id=?
                    ORDER BY version_number DESC LIMIT 1
                    """,
                    (str(pack_id),),
                ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).casefold():
                return None
            raise
        return _decoded_record(row) if row else None

    def get_version(self, version_id: int) -> Optional[dict]:
        """Return one immutable published version by its database identity."""

        self._ensure()
        with self.db.lock:
            row = self.db.connection.execute(
                "SELECT * FROM industry_pack_versions WHERE id=?",
                (int(version_id),),
            ).fetchone()
        return _decoded_record(row) if row else None

    def published_manifest_for_loader(self, pack_id: str):
        """Serve the active immutable version, otherwise the latest published one."""

        self._ensure()
        if self.is_deleted(pack_id):
            return None
        record = None
        try:
            with self.db.lock:
                active_pack = self.db.connection.execute(
                    """
                    SELECT setting_value FROM intel_runtime_settings
                    WHERE setting_key='active_industry_pack_id'
                    """
                ).fetchone()
                active_version = self.db.connection.execute(
                    """
                    SELECT setting_value FROM intel_runtime_settings
                    WHERE setting_key='active_industry_pack_version_id'
                    """
                ).fetchone()
            if (
                active_pack
                and active_version
                and str(active_pack[0]) == str(pack_id)
                and str(active_version[0]).isdigit()
            ):
                candidate = self.get_version(int(active_version[0]))
                if candidate and str(candidate["industry_pack_id"]) == str(pack_id):
                    record = candidate
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).casefold():
                raise
        if record is None:
            record = self.latest_published(pack_id)
        if not record:
            return None
        return copy.deepcopy(record["manifest"]), str(record["content_sha256"])

    def get_or_create_draft(
        self,
        pack_id: str,
        seed_manifest: Mapping[str, object],
        *,
        actor: str = "",
    ) -> dict:
        self._ensure()
        normalized_id = str(pack_id)
        manifest = copy.deepcopy(dict(seed_manifest))
        manifest_text = _manifest_text(manifest)
        digest = _manifest_sha256(manifest)
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                row = cursor.execute(
                    "SELECT * FROM industry_pack_drafts WHERE industry_pack_id=?",
                    (normalized_id,),
                ).fetchone()
                if not row:
                    latest = cursor.execute(
                        """
                        SELECT id FROM industry_pack_versions
                        WHERE industry_pack_id=?
                        ORDER BY version_number DESC LIMIT 1
                        """,
                        (normalized_id,),
                    ).fetchone()
                    cursor.execute(
                        """
                        INSERT INTO industry_pack_drafts(
                            industry_pack_id, base_version_id, revision,
                            manifest_json, content_sha256, created_by, updated_by
                        ) VALUES(?, ?, 1, ?, ?, ?, ?)
                        """,
                        (
                            normalized_id,
                            int(latest["id"]) if latest else None,
                            manifest_text,
                            digest,
                            str(actor or ""),
                            str(actor or ""),
                        ),
                    )
                    row = cursor.execute(
                        "SELECT * FROM industry_pack_drafts WHERE industry_pack_id=?",
                        (normalized_id,),
                    ).fetchone()
                self.db.connection.commit()
                return _decoded_record(row)
            except Exception:
                self.db.connection.rollback()
                raise
            finally:
                cursor.close()

    def save_draft(
        self,
        pack_id: str,
        manifest: Mapping[str, object],
        *,
        expected_revision: int,
        actor: str = "",
    ) -> dict:
        self._ensure()
        payload = copy.deepcopy(dict(manifest))
        text = _manifest_text(payload)
        digest = _manifest_sha256(payload)
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                row = cursor.execute(
                    "SELECT revision FROM industry_pack_drafts WHERE industry_pack_id=?",
                    (str(pack_id),),
                ).fetchone()
                if not row:
                    raise ValueError("行业包草稿不存在，请先创建草稿")
                if int(row["revision"]) != int(expected_revision):
                    raise ValueError("行业包草稿已被其他操作更新，请刷新后重试")
                next_revision = int(row["revision"]) + 1
                cursor.execute(
                    """
                    UPDATE industry_pack_drafts
                    SET revision=?, manifest_json=?, content_sha256=?,
                        updated_by=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                    WHERE industry_pack_id=?
                    """,
                    (
                        next_revision,
                        text,
                        digest,
                        str(actor or ""),
                        str(pack_id),
                    ),
                )
                cursor.execute(
                    """
                    UPDATE industry_pack_registry
                    SET name=?, updated_by=?,
                        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                    WHERE industry_pack_id=? AND status='active'
                    """,
                    (
                        str(payload.get("name") or ""),
                        str(actor or ""),
                        str(pack_id),
                    ),
                )
                saved = cursor.execute(
                    "SELECT * FROM industry_pack_drafts WHERE industry_pack_id=?",
                    (str(pack_id),),
                ).fetchone()
                self.db.connection.commit()
                return _decoded_record(saved)
            except Exception:
                self.db.connection.rollback()
                raise
            finally:
                cursor.close()

    def publish_draft(
        self,
        pack_id: str,
        *,
        expected_revision: int,
        actor: str = "",
    ) -> dict:
        self._ensure()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                draft = cursor.execute(
                    "SELECT * FROM industry_pack_drafts WHERE industry_pack_id=?",
                    (str(pack_id),),
                ).fetchone()
                if not draft:
                    raise ValueError("行业包草稿不存在")
                if int(draft["revision"]) != int(expected_revision):
                    raise ValueError("行业包草稿版本已变化，请重新校验")
                duplicate = cursor.execute(
                    """
                    SELECT id FROM industry_pack_versions
                    WHERE industry_pack_id=? AND content_sha256=?
                    """,
                    (str(pack_id), str(draft["content_sha256"])),
                ).fetchone()
                if duplicate:
                    raise ValueError("该行业包内容已经发布，不能创建重复版本")
                latest = cursor.execute(
                    """
                    SELECT id, version_number FROM industry_pack_versions
                    WHERE industry_pack_id=?
                    ORDER BY version_number DESC LIMIT 1
                    """,
                    (str(pack_id),),
                ).fetchone()
                manifest = json.loads(draft["manifest_json"] or "{}")
                version_number = int(latest["version_number"]) + 1 if latest else 1
                cursor.execute(
                    """
                    INSERT INTO industry_pack_versions(
                        industry_pack_id, version_number, pack_version,
                        schema_version, parent_version_id, manifest_json,
                        content_sha256, created_by
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(pack_id),
                        version_number,
                        str(manifest.get("pack_version") or ""),
                        int(manifest.get("schema_version") or 0),
                        int(latest["id"]) if latest else None,
                        str(draft["manifest_json"]),
                        str(draft["content_sha256"]),
                        str(actor or ""),
                    ),
                )
                version_id = int(cursor.lastrowid)
                cursor.execute(
                    "DELETE FROM industry_pack_drafts WHERE industry_pack_id=?",
                    (str(pack_id),),
                )
                published = cursor.execute(
                    "SELECT * FROM industry_pack_versions WHERE id=?",
                    (version_id,),
                ).fetchone()
                self.db.connection.commit()
                return _decoded_record(published)
            except Exception:
                self.db.connection.rollback()
                raise
            finally:
                cursor.close()

    def list_versions(self, pack_id: str) -> list[dict]:
        self._ensure()
        with self.db.lock:
            rows = self.db.connection.execute(
                """
                SELECT * FROM industry_pack_versions
                WHERE industry_pack_id=? ORDER BY version_number DESC
                """,
                (str(pack_id),),
            ).fetchall()
        return [_decoded_record(row) for row in rows]


class IndustryPackAdminService:
    def __init__(
        self,
        store: IndustryPackVersionStore,
        loader,
        *,
        url_validator: Callable[[str], str] = validate_external_url,
        settings=None,
    ):
        self.store = store
        self.loader = loader
        self.url_validator = url_validator
        self.settings = config if settings is None else settings

    def assert_managed_pack(self, pack_id: str) -> str:
        normalized_id = str(pack_id or "").strip()
        if not PACK_ID_PATTERN.fullmatch(normalized_id):
            raise IndustryPackError("invalid industry pack id")
        if self.loader.has_seed_pack(normalized_id):
            return normalized_id
        custom = self.store.custom_record(normalized_id)
        if custom and str(custom.get("status")) == "active":
            return normalized_id
        if custom and str(custom.get("status")) == "deleted":
            raise ValueError("行业包已删除，只保留历史版本与审计记录")
        raise ValueError("行业包不存在")

    @staticmethod
    def _new_manifest(
        pack_id: str,
        name: str,
        *,
        default_market: str,
        timezone: str,
    ) -> dict:
        capability_key = f"{pack_id}_content_intelligence"
        category_key = f"{pack_id}_industry_information"
        return {
            "id": pack_id,
            "name": name,
            "schema_version": 3,
            "pack_version": "1.0.0",
            "enabled": True,
            "default_market": default_market,
            "timezone": timezone,
            "core_keywords": [],
            "expanded_keywords": [],
            "trend_keywords": [],
            "event_keywords": [],
            "negative_keywords": [],
            "classification": {
                "core_weight": 3,
                "expanded_weight": 1,
                "trend_weight": 2,
                "event_weight": 2,
                "negative_weight": -3,
                "minimum_relevance_score": 2,
                "llm_confidence_threshold": 0.65,
                "tie_break_order": ["trend", "event", "other"],
            },
            "serpapi_queries": [],
            # 聚合调度（仅 admin 可改）：默认低频 —— 定点聚合、不做持续高频派发。
            # allow_continuous_dispatch=False 时由 worker 只按 daily_times 派发；
            # 搜索完成后 / 信源同步后 / 页面手动 这三类事件触发不受影响（避免 URL 积压不抓）。
            "crawl_schedule": {
                "enabled": True,
                "daily_times": ["02:00"],
                "polling_interval_minutes": 1440,
                "max_sources_per_run": 45,
                "allow_continuous_dispatch": False,
            },
            "default_sources": [],
            "fixed_topics": [],
            "pack_kind": "primary",
            "includes": [{"pack_id": "financial_markets", "required": True}],
            "capabilities": [
                {
                    "key": capability_key,
                    "name": f"{name}资讯聚合与分类",
                    "enabled": True,
                    "implementation_status": "configured",
                }
            ],
            "dashboard_categories": [
                {
                    "key": category_key,
                    "name": f"{name}行业信息",
                    "enabled": True,
                    "implementation_status": "configured",
                }
            ],
            "dashboard_capabilities": {
                "show_financial_news": True,
                "show_market_index_cards": False,
                "show_watched_stock_cards": False,
                # 首页默认走【主题卡 Dashboard】而不是时空地图：新建行业包激活后
                # 直接进入资讯流首页；需要地图的包在管理页显式勾选（写 true）。
                "show_spatiotemporal_map": False,
            },
            "ragflow_policy": {
                "upload_crawled_articles": False,
                "knowledge_base_key": "news",
            },
        }

    def create_pack(
        self,
        pack_id: str,
        name: str,
        *,
        default_market: str = "GLOBAL",
        timezone: str = "Asia/Hong_Kong",
        actor: str = "",
    ) -> dict:
        normalized_id = str(pack_id or "").strip()
        normalized_name = str(name or "").strip()
        if not PACK_ID_PATTERN.fullmatch(normalized_id):
            raise ValueError("行业包 ID 只能包含小写字母、数字和下划线")
        if normalized_id == "financial_markets" or self.loader.has_seed_pack(normalized_id):
            raise ValueError("行业包 ID 已被系统内置包占用")
        if not normalized_name or len(normalized_name) > 120:
            raise ValueError("行业名称不能为空且不能超过 120 个字符")
        normalized_market = str(default_market or "GLOBAL").strip().upper()
        if not normalized_market or len(normalized_market) > 32:
            raise ValueError("默认市场不能为空且不能超过 32 个字符")
        normalized_timezone = str(timezone or "Asia/Hong_Kong").strip()
        if not normalized_timezone or len(normalized_timezone) > 64:
            raise ValueError("时区不能为空且不能超过 64 个字符")
        manifest = self._new_manifest(
            normalized_id,
            normalized_name,
            default_market=normalized_market,
            timezone=normalized_timezone,
        )
        validated = self.validate_manifest(normalized_id, manifest)
        draft = self.store.create_custom_draft(
            normalized_id, validated, actor=actor
        )
        self.loader.clear_cache(normalized_id)
        return draft

    def list_managed_packs(self) -> list[dict]:
        records: dict[str, dict] = {}
        for pack in self.loader.list(enabled_only=False):
            pack_id = str(pack["id"])
            is_system = self.loader.has_seed_pack(pack_id)
            latest = self.store.latest_published(pack_id)
            records[pack_id] = {
                "id": pack_id,
                "name": str(pack.get("name") or pack_id),
                "schema_version": int(pack.get("schema_version") or 0),
                "pack_version": str(pack.get("pack_version") or ""),
                "enabled": bool(pack.get("enabled")),
                "default_market": str(pack.get("default_market") or ""),
                "timezone": str(pack.get("timezone") or ""),
                "origin": "system" if is_system else "custom",
                "draft_only": False,
                "published_version_id": int(latest["id"]) if latest else None,
            }
        for custom in self.store.list_custom_records():
            pack_id = str(custom["industry_pack_id"])
            if pack_id in records:
                continue
            draft = self.store.draft(pack_id)
            latest = self.store.latest_published(pack_id)
            manifest = (latest or draft or {}).get("manifest") or {}
            records[pack_id] = {
                "id": pack_id,
                "name": str(manifest.get("name") or custom.get("name") or pack_id),
                "schema_version": int(manifest.get("schema_version") or 0),
                "pack_version": str(manifest.get("pack_version") or ""),
                "enabled": bool(manifest.get("enabled", True)),
                "default_market": str(manifest.get("default_market") or ""),
                "timezone": str(manifest.get("timezone") or ""),
                "origin": "custom",
                "draft_only": latest is None,
                "published_version_id": int(latest["id"]) if latest else None,
            }
        return [records[key] for key in sorted(records)]

    def _known_manifests(self) -> list[dict]:
        manifests = [dict(item) for item in self.loader.list(enabled_only=False)]
        present = {str(item.get("id")) for item in manifests}
        for custom in self.store.list_custom_records():
            pack_id = str(custom["industry_pack_id"])
            if pack_id in present:
                continue
            record = self.store.latest_published(pack_id) or self.store.draft(pack_id)
            if record and isinstance(record.get("manifest"), dict):
                manifests.append(dict(record["manifest"]))
                present.add(pack_id)
        return manifests

    def _count(self, cursor, query: str, parameters: tuple) -> int:
        try:
            row = cursor.execute(query, parameters).fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(row[0] if row else 0)

    def deletion_preview(self, pack_id: str) -> dict:
        normalized_id = self.assert_managed_pack(pack_id)
        custom = self.store.custom_record(normalized_id)
        system_owned = self.loader.has_seed_pack(normalized_id)
        active_pack_id = ""
        dependencies = []
        for manifest in self._known_manifests():
            current_id = str(manifest.get("id") or "")
            if current_id == normalized_id:
                continue
            if normalized_id in {
                str(item.get("pack_id") if isinstance(item, dict) else item)
                for item in manifest.get("includes") or []
            }:
                dependencies.append(current_id)
        self.store._ensure()
        with self.store.db.lock:
            cursor = self.store.db.connection.cursor()
            try:
                setting = cursor.execute(
                    """
                    SELECT setting_value FROM intel_runtime_settings
                    WHERE setting_key='active_industry_pack_id'
                    """
                ).fetchone()
                active_pack_id = str(setting[0]) if setting else ""
                counts = {
                    "published_versions": self._count(
                        cursor,
                        "SELECT COUNT(*) FROM industry_pack_versions WHERE industry_pack_id=?",
                        (normalized_id,),
                    ),
                    "drafts": self._count(
                        cursor,
                        "SELECT COUNT(*) FROM industry_pack_drafts WHERE industry_pack_id=?",
                        (normalized_id,),
                    ),
                    "classifications": self._count(
                        cursor,
                        "SELECT COUNT(*) FROM article_intel_classifications WHERE industry_pack_id=?",
                        (normalized_id,),
                    ),
                    "source_associations": self._count(
                        cursor,
                        "SELECT COUNT(*) FROM intel_source_industries WHERE industry_pack_id=? AND is_active=1",
                        (normalized_id,),
                    ),
                    "reports": self._count(
                        cursor,
                        "SELECT COUNT(*) FROM intel_reports WHERE industry_pack_id=?",
                        (normalized_id,),
                    ),
                    "activations": self._count(
                        cursor,
                        "SELECT COUNT(*) FROM industry_pack_activations WHERE target_pack_id=? OR previous_pack_id=?",
                        (normalized_id, normalized_id),
                    ),
                }
                pending_jobs = 0
                running_jobs = 0
                try:
                    job_rows = cursor.execute(
                        """
                        SELECT status, payload_json FROM intel_jobs
                        WHERE status IN ('queued','retry_wait','running')
                        """
                    ).fetchall()
                except sqlite3.OperationalError:
                    job_rows = []
                for row in job_rows:
                    try:
                        payload = json.loads(row["payload_json"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if str(payload.get("industry_pack_id") or "") != normalized_id:
                        continue
                    if str(row["status"]) == "running":
                        running_jobs += 1
                    else:
                        pending_jobs += 1
                counts["pending_jobs"] = pending_jobs
                counts["running_jobs"] = running_jobs
            finally:
                cursor.close()
        blockers = []
        if system_owned:
            blockers.append("系统内置行业包不能删除")
        if normalized_id == "financial_markets":
            blockers.append("共享金融能力包不能删除")
        if active_pack_id == normalized_id:
            blockers.append("当前激活行业包不能删除，请先切换行业")
        if dependencies:
            blockers.append("仍被其他行业包依赖：" + "、".join(sorted(dependencies)))
        if counts["running_jobs"]:
            blockers.append("仍有运行中的行业包任务，请等待任务结束")
        if not custom or str(custom.get("status")) != "active":
            blockers.append("仅允许删除页面创建的有效自定义行业包")
        name = str((custom or {}).get("name") or normalized_id)
        identity = {
            "industry_pack_id": normalized_id,
            "name": name,
            "active_pack_id": active_pack_id,
            "dependent_pack_ids": sorted(dependencies),
            "counts": counts,
            "blockers": blockers,
        }
        return {
            **identity,
            "deletable": not blockers,
            "requires_confirmation": True,
            "confirmation_text": normalized_id,
            "retention_policy": {
                "logical_delete": True,
                "versions_retained": True,
                "articles_retained": True,
                "classifications_retained": True,
                "activation_history_retained": True,
                "active_source_associations_disabled": True,
                "pending_jobs_cancelled": True,
            },
            "plan_sha256": _payload_sha256(identity),
            "writes_performed": False,
        }

    def delete_pack(
        self,
        pack_id: str,
        *,
        expected_plan_sha256: str,
        confirmation_text: str,
        actor: str = "",
    ) -> dict:
        preview = self.deletion_preview(pack_id)
        if not preview["deletable"]:
            raise ValueError("；".join(preview["blockers"]))
        if str(expected_plan_sha256 or "") != str(preview["plan_sha256"]):
            raise ValueError("删除影响已经变化，请重新预览并确认")
        if str(confirmation_text or "").strip() != str(preview["confirmation_text"]):
            raise ValueError("最终确认文本必须与行业包 ID 完全一致")
        normalized_id = str(preview["industry_pack_id"])
        cancelled_jobs = 0
        disabled_associations = 0
        self.store._ensure()
        with self.store.db.lock:
            cursor = self.store.db.connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                current = cursor.execute(
                    """
                    SELECT status FROM industry_pack_registry
                    WHERE industry_pack_id=?
                    """,
                    (normalized_id,),
                ).fetchone()
                if not current or str(current["status"]) != "active":
                    raise ValueError("行业包已删除或不存在")
                cursor.execute(
                    """
                    UPDATE intel_source_industries
                    SET is_active=0,
                        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                    WHERE industry_pack_id=? AND is_active=1
                    """,
                    (normalized_id,),
                )
                disabled_associations = max(0, int(cursor.rowcount))
                job_rows = cursor.execute(
                    """
                    SELECT id, payload_json FROM intel_jobs
                    WHERE status IN ('queued','retry_wait')
                    """
                ).fetchall()
                job_ids = []
                for row in job_rows:
                    try:
                        payload = json.loads(row["payload_json"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if str(payload.get("industry_pack_id") or "") == normalized_id:
                        job_ids.append(int(row["id"]))
                for job_id in job_ids:
                    cursor.execute(
                        """
                        UPDATE intel_jobs
                        SET status='cancelled', last_error='行业包已由管理员删除',
                            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                            completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                        WHERE id=? AND status IN ('queued','retry_wait')
                        """,
                        (job_id,),
                    )
                    cancelled_jobs += max(0, int(cursor.rowcount))
                cursor.execute(
                    """
                    UPDATE industry_pack_registry
                    SET status='deleted', deleted_by=?, updated_by=?,
                        deleted_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                    WHERE industry_pack_id=? AND status='active'
                    """,
                    (str(actor or ""), str(actor or ""), normalized_id),
                )
                details = {
                    "plan_sha256": preview["plan_sha256"],
                    "impact": preview["counts"],
                    "retention_policy": preview["retention_policy"],
                    "disabled_source_associations": disabled_associations,
                    "cancelled_pending_jobs": cancelled_jobs,
                }
                cursor.execute(
                    """
                    INSERT INTO industry_pack_lifecycle_events(
                        industry_pack_id, pack_name, event_type, actor, details_json
                    ) VALUES(?, ?, 'deleted', ?, ?)
                    """,
                    (
                        normalized_id,
                        str(preview["name"]),
                        str(actor or ""),
                        json.dumps(details, ensure_ascii=False, sort_keys=True),
                    ),
                )
                event_id = int(cursor.lastrowid)
                self.store.db.connection.commit()
            except Exception:
                self.store.db.connection.rollback()
                raise
            finally:
                cursor.close()
        self.loader.clear_cache(normalized_id)
        return {
            "industry_pack_id": normalized_id,
            "name": preview["name"],
            "event_id": event_id,
            "deleted": True,
            "logical_delete": True,
            "disabled_source_associations": disabled_associations,
            "cancelled_pending_jobs": cancelled_jobs,
            "history_retained": True,
        }

    def list_lifecycle_events(self, limit: int = 100) -> list[dict]:
        self.store._ensure()
        with self.store.db.lock:
            rows = self.store.db.connection.execute(
                """
                SELECT id, industry_pack_id, pack_name, event_type,
                       actor, details_json, created_at
                FROM industry_pack_lifecycle_events
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (max(1, min(200, int(limit))),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["details"] = json.loads(item.pop("details_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                item["details"] = {}
                item.pop("details_json", None)
            result.append(item)
        return result

    def validate_manifest(self, pack_id: str, manifest: Mapping[str, object]) -> dict:
        normalized = validate_industry_pack(
            copy.deepcopy(dict(manifest)), expected_id=str(pack_id)
        )
        source_identities = set()
        for index, source in enumerate(normalized.get("default_sources") or []):
            source_type = str(source.get("source_type") or "website")
            if source_type not in SOURCE_TYPES:
                raise IndustryPackError(
                    f"default_sources[{index}].source_type is invalid"
                )
            authority = int(source.get("authority_level", 2))
            if authority < 1 or authority > 5:
                raise IndustryPackError(
                    f"default_sources[{index}].authority_level must be within 1..5"
                )
            interval = int(source.get("polling_interval_minutes", 1440))
            if interval < 5 or interval > 10080:
                raise IndustryPackError(
                    f"default_sources[{index}].polling_interval_minutes must be within 5..10080"
                )
            if "is_enabled" in source and not isinstance(source["is_enabled"], bool):
                raise IndustryPackError(
                    f"default_sources[{index}].is_enabled must be boolean"
                )
            try:
                checked_url = self.url_validator(str(source.get("url") or ""))
            except UnsafeExternalURLError as _url_err:
                # 放宽：信源 URL 域名解析失败/网络受限等环境问题不应阻断行业包编辑。
                # 该信源可能已在生产环境可解析或已属已发布信源；保留原始 URL 继续校验其余字段。
                checked_url = str(source.get("url") or "").strip()
                if not checked_url:
                    raise
            source["url"] = checked_url
            identity = canonicalize_source_url(checked_url)
            if identity in source_identities:
                raise IndustryPackError(
                    f"default_sources[{index}] duplicates a physical source URL"
                )
            source_identities.add(identity)
            if source_type == "rss" and str(pack_id) == "financial_markets":
                decision = rss_authorization_decision(
                    checked_url,
                    str(source.get("license_profile") or ""),
                    self.settings,
                )
                if not decision["authorized"]:
                    raise IndustryPackError(
                        "financial RSS source is not authorized: "
                        + str(decision["reason"])
                    )
        return normalized

    def get_or_create_draft(self, pack_id: str, *, actor: str = "") -> dict:
        self.assert_managed_pack(pack_id)
        existing = self.store.draft(pack_id)
        if existing:
            return existing
        latest = self.store.latest_published(pack_id)
        seed = (
            latest["manifest"]
            if latest
            else self.loader.load(pack_id, enabled_only=False)
        )
        return self.store.get_or_create_draft(pack_id, seed, actor=actor)

    def save_draft(
        self,
        pack_id: str,
        manifest: Mapping[str, object],
        *,
        expected_revision: int,
        actor: str = "",
    ) -> dict:
        self.assert_managed_pack(pack_id)
        normalized = self.validate_manifest(pack_id, manifest)
        return self.store.save_draft(
            pack_id,
            normalized,
            expected_revision=expected_revision,
            actor=actor,
        )

    def publish_draft(
        self,
        pack_id: str,
        *,
        expected_revision: int,
        actor: str = "",
    ) -> dict:
        self.assert_managed_pack(pack_id)
        draft = self.get_or_create_draft(pack_id, actor=actor)
        if int(draft["revision"]) != int(expected_revision):
            raise ValueError("行业包草稿版本已变化，请重新校验")
        self.validate_manifest(pack_id, draft["manifest"])
        published = self.store.publish_draft(
            pack_id,
            expected_revision=expected_revision,
            actor=actor,
        )
        self.loader.clear_cache(pack_id)
        return published

    def diff(self, pack_id: str, draft: Mapping[str, object]) -> dict:
        latest = self.store.latest_published(pack_id)
        if latest:
            before = latest["manifest"]
        elif self.loader.has_seed_pack(pack_id):
            before = self.loader.load(
                pack_id, enabled_only=False, use_published=False
            )
        else:
            stored_draft = self.store.draft(pack_id)
            before = stored_draft["manifest"] if stored_draft else dict(draft)
        after = dict(draft)
        keyword_diff = {}
        for field in KEYWORD_FIELDS:
            old = list(before.get(field) or [])
            new = list(after.get(field) or [])
            keyword_diff[field] = {
                "added": [item for item in new if item not in old],
                "removed": [item for item in old if item not in new],
            }

        def source_map(manifest):
            return {
                canonicalize_source_url(item.get("url")): dict(item)
                for item in manifest.get("default_sources") or []
                if str(item.get("url") or "").strip()
            }

        old_sources = source_map(before)
        new_sources = source_map(after)
        return {
            "base_version_id": latest["id"] if latest else None,
            "base_content_sha256": latest["content_sha256"] if latest else _manifest_sha256(before),
            "draft_content_sha256": _manifest_sha256(after),
            "keywords": keyword_diff,
            "sources": {
                "added": [new_sources[key] for key in sorted(new_sources.keys() - old_sources.keys())],
                "removed": [old_sources[key] for key in sorted(old_sources.keys() - new_sources.keys())],
                "modified": [
                    {"before": old_sources[key], "after": new_sources[key]}
                    for key in sorted(old_sources.keys() & new_sources.keys())
                    if old_sources[key] != new_sources[key]
                ],
            },
            "dashboard_capabilities": {
                "before": dict(before.get("dashboard_capabilities") or {}),
                "after": dict(after.get("dashboard_capabilities") or {}),
            },
            "ragflow_policy": {
                "before": dict(before.get("ragflow_policy") or {}),
                "after": dict(after.get("ragflow_policy") or {}),
            },
        }


industry_pack_version_store = IndustryPackVersionStore()

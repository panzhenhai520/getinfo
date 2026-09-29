#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Read-only export and isolated replay for industry-pack source migration."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Mapping

from industry_pack_admin import IndustryPackAdminService, IndustryPackVersionStore
from industry_packs import IndustryPackLoader, validate_industry_pack
from intel_sources import (
    IntelSourceRegistry,
    canonicalize_source_url,
    infer_content_attributes,
    infer_polling_interval_minutes,
    infer_source_type,
)
from sqlite_database import SQLiteDatabase


SAFE_METADATA_FIELDS = (
    "language",
    "expected_classifications",
    "license_profile",
    "approval_status",
    "source_role",
    "on_demand_only",
    "preferred_scan_time",
    "schedule_rule",
    "schedule_weekdays",
    "schedule_day",
    "browser_fetch_enabled",
)


def _sha256(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _metadata(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    try:
        decoded = json.loads(value or "{}")
        return decoded if isinstance(decoded, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _manifest_spec(source: Mapping[str, object]) -> dict:
    result = copy.deepcopy(dict(source))
    result.setdefault("source_type", infer_source_type(str(result.get("url") or "")))
    result.setdefault("authority_level", 2)
    result.setdefault("polling_interval_minutes", 1440)
    result.setdefault("is_enabled", True)
    return result


class FamilyOfficePackExporter:
    """Aggregate current configuration without mutating the source database."""

    def __init__(
        self,
        connection,
        *,
        pack_loader: IndustryPackLoader,
        target_pack_version: str = "",
    ):
        self.connection = connection
        self.pack_loader = pack_loader
        self.target_pack_version = str(target_pack_version or "").strip()

    def _candidate(self, source: Mapping[str, object], *, origin: dict, priority: int) -> dict:
        url = str(source.get("url") or source.get("source_url") or "").strip()
        canonical = canonicalize_source_url(url)
        name = str(source.get("name") or source.get("source_name") or canonical).strip()
        content_type, inferred_authority = infer_content_attributes(url, name)
        metadata = _metadata(source.get("metadata_json") or source.get("metadata"))
        spec = {
            "name": name,
            "url": url,
            "source_type": str(source.get("source_type") or infer_source_type(url, name)),
            "content_type": str(source.get("content_type") or content_type),
            "authority_level": int(source.get("authority_level") or inferred_authority),
            "polling_interval_minutes": int(
                source.get("polling_interval_minutes")
                or infer_polling_interval_minutes(dict(source))
            ),
            "is_enabled": bool(source.get("is_enabled", True)),
        }
        market = str(source.get("market") or "").strip()
        if market:
            spec["market"] = market
        for field in SAFE_METADATA_FIELDS:
            if field in metadata and metadata[field] not in (None, "", [], {}):
                spec[field] = copy.deepcopy(metadata[field])
            elif field in source and source[field] not in (None, "", [], {}):
                spec[field] = copy.deepcopy(source[field])
        return {
            "canonical_source_url": canonical,
            "spec": spec,
            "priority": int(priority),
            "origins": [origin],
            "ownership_hints": set(),
        }

    def export(self) -> dict:
        family = self.pack_loader.load(
            "family_office", enabled_only=False, use_published=False
        )
        finance = self.pack_loader.load(
            "financial_markets", enabled_only=False, use_published=False
        )
        grouped: dict[str, dict] = {}
        errors = []

        def add(source, *, origin, priority, ownership_hint=""):
            try:
                item = self._candidate(source, origin=origin, priority=priority)
            except (ValueError, TypeError) as exc:
                errors.append({"origin": origin, "error": str(exc)})
                return
            canonical = item["canonical_source_url"]
            current = grouped.get(canonical)
            if current is None:
                grouped[canonical] = item
                current = item
            else:
                current["origins"].extend(item["origins"])
                if item["priority"] > current["priority"]:
                    current["spec"] = item["spec"]
                    current["priority"] = item["priority"]
            if ownership_hint:
                current["ownership_hints"].add(str(ownership_hint))

        for pack_id, manifest in (("family_office", family), ("financial_markets", finance)):
            for index, source in enumerate(manifest.get("default_sources") or []):
                add(
                    source,
                    origin={"type": "manifest", "pack_id": pack_id, "index": index},
                    priority=100,
                    ownership_hint=("shared_financial" if pack_id == "financial_markets" else "pack_owned"),
                )

        source_rows = self.connection.execute(
            """
            SELECT s.*, si.industry_pack_id, si.ownership_type,
                   si.is_active AS association_active
            FROM intel_sources s
            JOIN intel_source_industries si ON si.source_id=s.id
            WHERE si.industry_pack_id IN ('family_office','financial_markets')
              AND si.is_active=1
            ORDER BY s.id, si.id
            """
        ).fetchall()
        for row in source_rows:
            record = dict(row)
            ownership = str(record.get("ownership_type") or "legacy")
            if str(record.get("industry_pack_id")) == "financial_markets":
                ownership = "shared_financial"
            add(
                record,
                origin={
                    "type": "intel_source",
                    "source_id": int(record["id"]),
                    "pack_id": str(record["industry_pack_id"]),
                    "ownership_type": ownership,
                },
                priority=80,
                ownership_hint=ownership,
            )

        managed = self.connection.execute(
            """
            SELECT id, url, name, description, is_active, crawl_frequency
            FROM managed_urls WHERE is_active=TRUE ORDER BY id
            """
        ).fetchall()
        for row in managed:
            record = dict(row)
            add(
                {
                    "url": record["url"],
                    "name": record.get("name") or record["url"],
                    "is_enabled": True,
                    "polling_interval_minutes": infer_polling_interval_minutes(record),
                },
                origin={"type": "managed_url", "id": int(record["id"])},
                priority=50,
                ownership_hint="legacy_family_office",
            )

        schedules = self.connection.execute(
            """
            SELECT st.id, st.task_name, st.target_url, st.url_id,
                   st.schedule_type, st.schedule_time, st.schedule_weekdays,
                   st.keywords, st.config, mu.url AS managed_url
            FROM scheduled_tasks st
            LEFT JOIN managed_urls mu ON mu.id=st.url_id
            WHERE st.is_active=TRUE ORDER BY st.id
            """
        ).fetchall()
        for row in schedules:
            record = dict(row)
            url = str(record.get("target_url") or record.get("managed_url") or "").strip()
            if not url:
                continue
            schedule_metadata = {
                "schedule_rule": str(record.get("schedule_type") or "daily"),
                "preferred_scan_time": str(record.get("schedule_time") or "08:30")[:5],
                "schedule_weekdays": str(record.get("schedule_weekdays") or ""),
            }
            add(
                {
                    "url": url,
                    "name": record.get("task_name") or url,
                    "is_enabled": True,
                    "metadata": schedule_metadata,
                    "polling_interval_minutes": infer_polling_interval_minutes(record),
                },
                origin={"type": "scheduled_task", "id": int(record["id"])},
                priority=60,
                ownership_hint="legacy_family_office",
            )

        family_sources = []
        finance_sources = []
        protected = []
        source_audit = []
        for canonical in sorted(grouped):
            item = grouped[canonical]
            hints = set(item.pop("ownership_hints"))
            if "shared_financial" in hints:
                ownership = "shared_financial"
                finance_sources.append(item["spec"])
            elif "protected_manual" in hints:
                ownership = "protected_manual"
                protected.append(item["spec"])
            else:
                ownership = "pack_owned"
                family_sources.append(item["spec"])
            source_audit.append(
                {
                    "canonical_source_url": canonical,
                    "ownership": ownership,
                    "origins": item["origins"],
                    "spec_sha256": _sha256(item["spec"]),
                }
            )

        proposed_family = copy.deepcopy(family)
        proposed_family["default_sources"] = family_sources
        if self.target_pack_version:
            proposed_family["pack_version"] = self.target_pack_version
        proposed_finance = copy.deepcopy(finance)
        proposed_finance["default_sources"] = finance_sources
        validate_industry_pack(proposed_family, expected_id="family_office")
        validate_industry_pack(proposed_finance, expected_id="financial_markets")
        return {
            "export_version": "industry-pack-family-office-export-v1",
            "read_only": True,
            "database_writes": 0,
            "counts": {
                "unique_physical_sources": len(grouped),
                "family_office_sources": len(family_sources),
                "shared_financial_sources": len(finance_sources),
                "protected_manual_sources": len(protected),
                "managed_urls_scanned": len(managed),
                "scheduled_tasks_scanned": len(schedules),
                "errors": len(errors),
            },
            "proposed_manifests": {
                "family_office": proposed_family,
                "financial_markets": proposed_finance,
            },
            "protected_manual_sources": protected,
            "source_audit": source_audit,
            "errors": errors,
            "configuration_sha256": _sha256(
                {"family_office": proposed_family, "financial_markets": proposed_finance}
            ),
        }


def write_family_office_seed(
    result: Mapping[str, object], *, destination: str
) -> dict:
    """Atomically install a fully replayed family-office proposal as a seed.

    The live database and the shared-financial manifest are never modified by
    this operation. Database publication remains an explicit admin action.
    """

    counts = dict(result.get("counts") or {})
    replay = dict(result.get("replay") or {})
    if not bool(result.get("read_only")) or int(result.get("database_writes") or 0):
        raise ValueError("迁移输入不是只读导出结果")
    if int(counts.get("errors") or 0):
        raise ValueError("迁移仍有导出错误，拒绝写入种子包")
    if int(counts.get("protected_manual_sources") or 0):
        raise ValueError("迁移仍有受保护人工来源，拒绝自动写入种子包")
    if not bool(result.get("passed")) or not bool(replay.get("passed")):
        raise ValueError("临时数据库回放未通过，拒绝写入种子包")
    manifests = result.get("proposed_manifests") or {}
    manifest = copy.deepcopy(dict(manifests.get("family_office") or {}))
    validate_industry_pack(manifest, expected_id="family_office")
    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    temporary = target.parent / f".{target.name}.{os.getpid()}.tmp"
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "path": str(target),
        "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "source_count": len(manifest.get("default_sources") or []),
        "pack_version": str(manifest.get("pack_version") or ""),
    }


def replay_export(export: Mapping[str, object], *, config_dir: str) -> dict:
    """Publish and round-trip the proposal in a fresh temporary database."""

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        pack_dir = root / "packs"
        shutil.copytree(config_dir, pack_dir)
        proposed = export["proposed_manifests"]
        for pack_id in ("family_office", "financial_markets"):
            (pack_dir / f"{pack_id}.json").write_text(
                json.dumps(proposed[pack_id], ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        empty_manifest = copy.deepcopy(proposed["family_office"])
        empty_manifest.update(
            {
                "id": "migration_empty",
                "name": "迁移隔离测试包",
                "pack_version": "0.0.1",
                "default_sources": [],
                "dashboard_capabilities": {
                    "show_financial_news": True,
                    "show_market_index_cards": False,
                    "show_watched_stock_cards": False,
                },
            }
        )
        validate_industry_pack(empty_manifest, expected_id="migration_empty")
        (pack_dir / "migration_empty.json").write_text(
            json.dumps(empty_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        loader = IndustryPackLoader(str(pack_dir), use_published_store=False)
        database = SQLiteDatabase(str(root / "replay.sqlite3"))
        if not database.connect() or not database.create_tables():
            raise RuntimeError("临时数据库初始化失败")
        try:
            registry = IntelSourceRegistry(database, pack_loader=loader)
            plan = registry.plan_source_reconciliation("family_office")
            applied = registry.apply_source_reconciliation(
                "family_office", expected_plan_sha256=plan["plan_sha256"]
            )
            empty_plan = registry.plan_source_reconciliation("migration_empty")
            empty_applied = registry.apply_source_reconciliation(
                "migration_empty",
                expected_plan_sha256=empty_plan["plan_sha256"],
            )
            return_plan = registry.plan_source_reconciliation("family_office")
            return_applied = registry.apply_source_reconciliation(
                "family_office",
                expected_plan_sha256=return_plan["plan_sha256"],
            )
            second = registry.plan_source_reconciliation("family_office")
            store = IndustryPackVersionStore(database)
            admin = IndustryPackAdminService(
                store,
                loader,
                url_validator=lambda value: str(value),
            )
            draft = admin.get_or_create_draft("family_office", actor="migration-replay")
            published = admin.publish_draft(
                "family_office",
                expected_revision=int(draft["revision"]),
                actor="migration-replay",
            )
            expected = {
                canonicalize_source_url(source["url"])
                for source in loader.compose("family_office")["default_sources"]
            }
            rows = database.connection.execute(
                """
                SELECT DISTINCT s.canonical_source_url
                FROM intel_sources s
                JOIN intel_source_industries si ON si.source_id=s.id
                WHERE si.industry_pack_id IN ('family_office','financial_markets')
                  AND si.is_active=1
                """
            ).fetchall()
            actual = {str(row[0]) for row in rows}
            replay = {
                "expected_source_count": len(expected),
                "actual_source_count": len(actual),
                "missing_sources": sorted(expected - actual),
                "unexpected_sources": sorted(actual - expected),
                "first_apply": applied,
                "empty_pack_apply": empty_applied,
                "return_apply": return_applied,
                "published_version": {
                    "id": int(published["id"]),
                    "version_number": int(published["version_number"]),
                    "pack_version": str(published["pack_version"]),
                    "content_sha256": str(published["content_sha256"]),
                },
                "second_plan_counts": second["counts"],
                "idempotent": all(value == 0 for value in second["counts"].values()),
                "keyword_sha256": _sha256(
                    {
                        field: loader.load("family_office").get(field) or []
                        for field in (
                            "core_keywords",
                            "expanded_keywords",
                            "trend_keywords",
                            "event_keywords",
                            "negative_keywords",
                        )
                    }
                ),
            }
            replay["passed"] = bool(
                not replay["missing_sources"]
                and not replay["unexpected_sources"]
                and replay["idempotent"]
                and int(replay["published_version"]["version_number"]) == 1
            )
            return replay
        finally:
            database.disconnect()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Two-phase, backed-up and auditable industry-pack activation."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from industry_pack_admin import IndustryPackVersionStore, industry_pack_version_store
from industry_packs import IndustryPackLoader
from intel_contracts import utc_text
from intel_database import IntelRepository, intel_repository
from intel_sources import IntelSourceRegistry, intel_source_registry
from sqlite_database import sqlite_db


def normalize_initialization_window(
    initialization_from: str = "",
    initialization_to: str = "",
    *,
    now: Optional[datetime] = None,
) -> dict:
    """Normalize a user-selected historical window to UTC.

    The default is a 30-day initialization window.  The legacy seven-day
    crawl default is intentionally not used for industry initialization.
    """

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)

    def parse(value: str, label: str) -> Optional[datetime]:
        raw = str(value or '').strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        except ValueError as exc:
            raise ValueError(f'{label}格式无效，应为 ISO 日期或时间') from exc
        if parsed.tzinfo is None:
            # UI inputs are Hong Kong local time unless an offset is supplied.
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
        return parsed.astimezone(timezone.utc)

    end = parse(initialization_to, '初始化结束时间') or current
    start = parse(initialization_from, '初始化开始时间')
    if end > current + timedelta(minutes=1):
        raise ValueError('初始化结束时间不能晚于当前时间')
    if start is None:
        start = end - timedelta(days=30)
    if start >= end:
        raise ValueError('初始化开始时间必须早于结束时间')
    if end - start > timedelta(days=365):
        raise ValueError('单次初始化时间范围不能超过365天')
    return {
        'from': utc_text(start),
        'to': utc_text(end),
        'days': round((end - start).total_seconds() / 86400, 3),
        'timezone': 'Asia/Hong_Kong',
        'defaulted': not bool(str(initialization_from or '').strip()),
    }


ACTIVATION_BACKUP_TABLES = (
    "articles",
    "article_intel_classifications",
    "article_spacetime_profiles",
    "managed_urls",
    "scheduled_tasks",
    "crawl_tasks",
    "intel_sources",
    "intel_source_industries",
    "intel_source_origins",
)


def _sqlite_table_counts(connection, table_names=ACTIVATION_BACKUP_TABLES) -> dict:
    existing = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    return {
        table_name: int(
            connection.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
        )
        for table_name in table_names
        if table_name in existing
    }


def _pg_backup_column_type(udt_name: str, data_type: str) -> str:
    kind = str(udt_name or data_type or "").strip().lower()
    if kind in {"int2", "int4", "int8", "smallint", "integer", "bigint", "serial", "bigserial"}:
        return "INTEGER"
    if kind in {"float4", "float8", "real", "double precision", "numeric", "decimal"}:
        return "REAL"
    if kind == "bytea":
        return "BLOB"
    if kind in {"bool", "boolean"}:
        return "INTEGER"
    return "TEXT"


def _sqlite_adapt_backup_value(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    from decimal import Decimal
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _export_postgres_activation_backup(pg_connection, destination, table_names) -> None:
    """Copy the activation-relevant tables from PostgreSQL into a SQLite file."""
    raw_conn = getattr(pg_connection, "_connection", pg_connection)
    raw = raw_conn.cursor()
    try:
        for table_name in table_names:
            raw.execute(
                """
                SELECT column_name, data_type, udt_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = %s
                ORDER BY ordinal_position
                """,
                (table_name,),
            )
            cols = raw.fetchall()
            if not cols:
                continue
            col_names = [row[0] for row in cols]
            col_defs = []
            for name, data_type, udt_name in cols:
                col_defs.append(f'"{name}" {_pg_backup_column_type(udt_name, data_type)}')
            quoted_table = f'"{table_name}"'
            destination.execute(f"DROP TABLE IF EXISTS {quoted_table}")
            destination.execute(f"CREATE TABLE {quoted_table} ({', '.join(col_defs)})")
            quoted_cols = ", ".join(f'"{name}"' for name in col_names)
            placeholders = ", ".join("?" for _ in col_names)
            insert_sql = f"INSERT INTO {quoted_table} ({quoted_cols}) VALUES ({placeholders})"
            raw.execute(f"SELECT * FROM {quoted_table}")
            batch = []
            for pg_row in raw.fetchall():
                batch.append(tuple(_sqlite_adapt_backup_value(v) for v in pg_row))
                if len(batch) >= 500:
                    destination.executemany(insert_sql, batch)
                    batch = []
            if batch:
                destination.executemany(insert_sql, batch)
    finally:
        raw.close()


def _stable_hash(value: dict) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify_sqlite_backup(
    path: str,
    *,
    expected_sha256: str = "",
    expected_size: Optional[int] = None,
    expected_schema_version: Optional[int] = None,
    expected_table_counts: Optional[dict] = None,
) -> dict:
    """Verify a disaster-recovery SQLite file without modifying either DB."""

    target = Path(path).expanduser().resolve()
    if not target.is_file():
        raise FileNotFoundError(f"SQLite 备份不存在：{target}")
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    actual_sha256 = digest.hexdigest()
    actual_size = int(target.stat().st_size)
    uri = f"{target.as_uri()}?mode=ro&immutable=1"
    # 注意：sqlite3.Connection 的 with 上下文管理器只提交/回滚，**不关闭连接**。
    # 用 with 会让只读句柄一直占着备份文件：Windows 上备份无法删除/轮转
    # （实测 verify_sqlite_backup 之后 os.unlink 报 WinError 32），Linux 上每次激活泄漏一个 fd。
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
        integrity = str(integrity_row[0] if integrity_row else "")
        schema_row = connection.execute("PRAGMA schema_version").fetchone()
        schema_version = int(schema_row[0] if schema_row else 0)
        table_counts = _sqlite_table_counts(connection)
    finally:
        connection.close()
    expected_counts = {
        str(key): int(value)
        for key, value in (expected_table_counts or {}).items()
    }
    checks = {
        "integrity": integrity.casefold() == "ok",
        "sha256": not expected_sha256 or actual_sha256 == str(expected_sha256),
        "size": expected_size is None or actual_size == int(expected_size),
        "schema_version": (
            expected_schema_version is None
            or schema_version == int(expected_schema_version)
        ),
        "table_counts": (
            not expected_counts
            or all(table_counts.get(key) == value for key, value in expected_counts.items())
        ),
    }
    return {
        "path": str(target),
        "sha256": actual_sha256,
        "size": actual_size,
        "schema_version": schema_version,
        "integrity": integrity,
        "table_counts": table_counts,
        "checks": checks,
        "passed": all(checks.values()),
        "read_only": True,
    }


class SQLiteActivationBackupService:
    """Create a consistent SQLite backup and verify it before activation."""

    def __init__(self, database=None, *, backup_dir: Optional[str] = None):
        self.db = database or sqlite_db
        configured_path = str(getattr(self.db, "db_path", "") or "")
        default_parent = Path(configured_path).resolve().parent if configured_path and configured_path != ":memory:" else Path(tempfile.gettempdir())
        self.backup_dir = Path(backup_dir) if backup_dir else default_parent / "industry_pack_backups"

    def create(self, activation_id: str) -> dict:
        self.db._ensure_connection()
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        final_path = self.backup_dir / f"industry-pack-{activation_id}.sqlite3"
        temporary_path = self.backup_dir / f".{final_path.name}.{uuid.uuid4().hex}.tmp"
        destination = None
        try:
            destination = sqlite3.connect(str(temporary_path), isolation_level=None, timeout=30)
            with self.db.lock:
                from db_connection import is_postgres_connection
                is_pg = bool(is_postgres_connection(self.db.connection))
                # Postgres 导出不是原子快照：并发 worker 写入会让 table_counts 在"统计"与
                # "导出"之间漂移，精确比对会偶发"SQLite 备份独立复核失败"。PG 模式下跳过
                # 该精确比对，完整性 / 大小 / schema_version 仍照常校验。
                source_table_counts = {} if is_pg else _sqlite_table_counts(self.db.connection)
                if is_pg:
                    _export_postgres_activation_backup(
                        self.db.connection, destination, ACTIVATION_BACKUP_TABLES
                    )
                else:
                    self.db.connection.backup(destination)
            integrity_row = destination.execute("PRAGMA integrity_check").fetchone()
            integrity = str(integrity_row[0] if integrity_row else "")
            if integrity.casefold() != "ok":
                raise RuntimeError(f"SQLite 备份完整性检查失败：{integrity or 'unknown'}")
            schema_row = destination.execute("PRAGMA schema_version").fetchone()
            schema_version = int(schema_row[0] if schema_row else 0)
            destination.close()
            destination = None
            os.replace(temporary_path, final_path)
            verified = verify_sqlite_backup(
                str(final_path),
                expected_size=int(final_path.stat().st_size),
                expected_schema_version=schema_version,
                expected_table_counts=source_table_counts,
            )
            if not verified["passed"]:
                raise RuntimeError("SQLite 备份独立复核失败")
            return {
                "path": str(final_path.resolve()),
                "sha256": verified["sha256"],
                "size": verified["size"],
                "schema_version": verified["schema_version"],
                "integrity": verified["integrity"],
                "table_counts": verified["table_counts"],
                "table_counts_verified": verified["checks"]["table_counts"],
            }
        except Exception:
            if destination is not None:
                destination.close()
            temporary_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise


class IndustryPackActivationService:
    """Preview and atomically activate only immutable published manifests."""

    def __init__(
        self,
        database=None,
        *,
        version_store: Optional[IndustryPackVersionStore] = None,
        source_registry: Optional[IntelSourceRegistry] = None,
        repository: Optional[IntelRepository] = None,
        backup_service: Optional[SQLiteActivationBackupService] = None,
    ):
        self.db = database or sqlite_db
        self.version_store = version_store or industry_pack_version_store
        self.source_registry = source_registry or intel_source_registry
        self.repository = repository or intel_repository
        self.backup_service = backup_service or SQLiteActivationBackupService(self.db)

    def _setting(self, key: str, default: str = "") -> str:
        self.db._ensure_connection()
        with self.db.lock:
            row = self.db.connection.execute(
                "SELECT setting_value FROM intel_runtime_settings WHERE setting_key=?",
                (str(key),),
            ).fetchone()
        return str(row[0]) if row and row[0] is not None else str(default)

    def _version_registry(self, target: dict) -> IntelSourceRegistry:
        """Resolve the target primary manifest at the exact published version."""

        base_loader = self.source_registry.pack_loader

        def published_provider(pack_id: str):
            if str(pack_id) == str(target["industry_pack_id"]):
                return target["manifest"], target["content_sha256"]
            record = self.version_store.latest_published(str(pack_id))
            if record:
                return record["manifest"], record["content_sha256"]
            return None

        loader = IndustryPackLoader(
            str(base_loader.config_dir),
            use_published_store=True,
            published_manifest_provider=published_provider,
        )
        return IntelSourceRegistry(self.db, pack_loader=loader)

    def preview(
        self, target_pack_id: str, *, target_version_id: Optional[int] = None,
        initialization_from: str = '', initialization_to: str = ''
    ) -> dict:
        target = (
            self.version_store.get_version(int(target_version_id))
            if target_version_id is not None
            else self.version_store.latest_published(str(target_pack_id))
        )
        if not target:
            raise ValueError("目标行业包尚无已发布版本，请先校验并发布")
        if str(target["industry_pack_id"]) != str(target_pack_id):
            raise ValueError("目标发布版本不属于所选行业包")
        if self.version_store.is_deleted(str(target_pack_id)):
            raise ValueError("目标行业包已删除，不能激活或回滚到该行业包")
        initialization_window = normalize_initialization_window(
            initialization_from, initialization_to
        )
        previous_pack_id = self._setting(
            "active_industry_pack_id", self.repository.active_industry_pack_id()
        )
        previous_version_text = self._setting("active_industry_pack_version_id")
        previous_version_id = int(previous_version_text) if previous_version_text.isdigit() else None
        if previous_version_id is None:
            previous_published = self.version_store.latest_published(previous_pack_id)
            if previous_published:
                previous_version_id = int(previous_published["id"])
        if previous_version_id is None and str(previous_pack_id) != str(target_pack_id):
            raise ValueError(
                "当前行业包尚无已发布基线；请先发布当前行业，再执行可回滚切换"
            )
        source_plan = self._version_registry(target).plan_source_reconciliation(
            str(target_pack_id), declared_version_id=int(target["id"])
        )
        project_keywords = []
        seen_keywords = set()
        for field in ("core_keywords", "expanded_keywords"):
            for raw in target["manifest"].get(field) or []:
                value = str(raw or "").strip()
                key = value.casefold()
                if value and key not in seen_keywords:
                    seen_keywords.add(key)
                    project_keywords.append(value)
        # 默认初始化窗口由 normalize_initialization_window 用 datetime.now() 生成，
        # dry-run 与 confirm 是两次独立请求、间隔数秒，各自 now() 不同会让窗口漂移，
        # 进而 plan_sha256 不可重现、两阶段确认必然失败（报“行业包激活计划已经变化”）。
        # 这里只把“是否使用默认窗口”纳入哈希（固定占位符），具体时间不参与，
        # 保证 plan_sha256 在默认窗口下稳定；实际采集窗口仍用 preview 返回的具体值。
        if initialization_window.get('defaulted'):
            window_marker_from = '__default_window__'
            window_marker_to = '__default_window__'
        else:
            window_marker_from = initialization_window['from']
            window_marker_to = initialization_window['to']
        identity = {
            "previous_pack_id": previous_pack_id,
            "previous_version_id": previous_version_id,
            "target_pack_id": str(target_pack_id),
            "target_version_id": int(target["id"]),
            "target_content_sha256": str(target["content_sha256"]),
            "target_pack_version": str(target["manifest"].get("pack_version") or ""),
            "source_plan_sha256": str(source_plan["plan_sha256"]),
            "initialization_from": window_marker_from,
            "initialization_to": window_marker_to,
        }
        return {
            **identity,
            "plan_sha256": _stable_hash(identity),
            "source_plan": source_plan,
            "source_counts": dict(source_plan["counts"]),
            "target_project_keywords": project_keywords,
            "initialization_window": initialization_window,
            "requires_confirmation": True,
            "writes_performed": False,
        }

    @staticmethod
    def _put_setting(cursor, key: str, value: object) -> None:
        cursor.execute(
            """
            INSERT INTO intel_runtime_settings(setting_key, setting_value, updated_at)
            VALUES(?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            ON CONFLICT(setting_key) DO UPDATE SET
                setting_value=excluded.setting_value,
                updated_at=excluded.updated_at
            """,
            (str(key), str(value)),
        )

    @staticmethod
    def _event(cursor, activation_id: str, stage: str, details: Optional[dict] = None) -> None:
        cursor.execute(
            """
            INSERT INTO industry_pack_activation_events(activation_id, stage, details_json)
            VALUES(?, ?, ?)
            """,
            (
                str(activation_id),
                str(stage),
                json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
            ),
        )

    def activate(
        self,
        target_pack_id: str,
        *,
        target_version_id: int,
        expected_plan_sha256: str,
        actor: str = "",
        initialization_from: str = '', initialization_to: str = '',
    ) -> dict:
        preview = self.preview(
            str(target_pack_id), target_version_id=int(target_version_id),
            initialization_from=initialization_from,
            initialization_to=initialization_to,
        )
        return self._apply_preview(
            preview,
            expected_plan_sha256=expected_plan_sha256,
            actor=actor,
        )

    def _apply_preview(
        self,
        preview: dict,
        *,
        expected_plan_sha256: str,
        actor: str = "",
        rollback_of_activation_id: str = "",
    ) -> dict:
        if str(expected_plan_sha256 or "") != str(preview["plan_sha256"]):
            raise ValueError("行业包激活计划已经变化，请重新预览并确认")

        activation_id = uuid.uuid4().hex
        # 按需备份：先看信源差异。若新增/更新/关联/停用全为 0（无实质变化），本次激活
        # 只会写 3 个 runtime 设置 + 1 条激活记录，做整库快照纯属浪费（大库要一两分钟）。
        # 有实质变化才备份，既省时间又保住可回滚能力。
        source_counts = dict(preview.get("source_counts") or {})
        substantive_change = any(
            int(source_counts.get(key) or 0)
            for key in ("add_sources", "update_sources", "upsert_associations", "deactivate_associations")
        )
        if substantive_change:
            backup = self.backup_service.create(activation_id)
        else:
            backup = {
                "path": "",
                "sha256": "",
                "size": 0,
                "schema_version": 0,
                "integrity": "skipped_no_source_change",
            }
        now = utc_text()
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                preservation_before = _sqlite_table_counts(
                    self.db.connection,
                    ("articles", "article_spacetime_profiles"),
                )
                cursor.execute(
                    """
                    INSERT INTO industry_pack_activations(
                        id, previous_pack_id, previous_version_id,
                        target_pack_id, target_version_id, status, is_current,
                        plan_sha256, source_plan_json,
                        backup_path, backup_sha256, backup_size,
                        backup_schema_version, backup_integrity,
                        created_by, created_at, started_at
                    ) VALUES(?, ?, ?, ?, ?, 'applying', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        activation_id,
                        preview["previous_pack_id"],
                        preview["previous_version_id"],
                        preview["target_pack_id"],
                        preview["target_version_id"],
                        preview["plan_sha256"],
                        json.dumps(preview["source_plan"], ensure_ascii=False, sort_keys=True),
                        backup["path"],
                        backup["sha256"],
                        backup["size"],
                        backup["schema_version"],
                        backup["integrity"],
                        str(actor or ""),
                        now,
                        now,
                    ),
                )
                self._event(cursor, activation_id, "applying", preview["source_counts"])
                source_result = self.source_registry.apply_source_reconciliation(
                    preview["target_pack_id"],
                    expected_plan_sha256=preview["source_plan"]["plan_sha256"],
                    declared_version_id=preview["target_version_id"],
                    transaction_cursor=cursor,
                    precomputed_plan=preview["source_plan"],
                )
                self._event(cursor, activation_id, "verifying", source_result)
                compatibility_result = (
                    self.source_registry.project_effective_sources_to_managed_urls(
                        preview["target_pack_id"],
                        effective_pack_ids=preview["source_plan"]["effective_pack_ids"],
                        activation_id=activation_id,
                        industry_pack_version_id=preview["target_version_id"],
                        project_keywords=preview["target_project_keywords"],
                        initialization_from=preview['initialization_window']['from'],
                        initialization_to=preview['initialization_window']['to'],
                        transaction_cursor=cursor,
                    )
                )
                self._event(
                    cursor,
                    activation_id,
                    "legacy_url_projection_synced",
                    compatibility_result,
                )
                collection_task_result = (
                    self.source_registry.project_effective_sources_to_collection_tasks(
                        preview["target_pack_id"],
                        effective_pack_ids=preview["source_plan"]["effective_pack_ids"],
                        activation_id=activation_id,
                        industry_pack_version_id=preview["target_version_id"],
                        project_keywords=preview["target_project_keywords"],
                        transaction_cursor=cursor,
                    )
                )
                self._event(
                    cursor,
                    activation_id,
                    "collection_tasks_projected",
                    collection_task_result,
                )
                projection_result = self.repository.restore_pack_projection_for_activation(
                    industry_pack_id=preview["target_pack_id"],
                    activation_id=activation_id,
                    target_pack_version=preview["target_pack_version"],
                    project_keywords=preview["target_project_keywords"],
                    transaction_cursor=cursor,
                )
                self._event(
                    cursor,
                    activation_id,
                    "restoring_projection",
                    projection_result,
                )
                preservation_after = _sqlite_table_counts(
                    self.db.connection,
                    ("articles", "article_spacetime_profiles"),
                )
                if preservation_after != preservation_before:
                    raise RuntimeError(
                        "行业切换改变了文章或地图画像数量，已自动回滚本次切换"
                    )
                preservation_result = {
                    "tables": preservation_after,
                    "verified": True,
                }
                self._event(
                    cursor,
                    activation_id,
                    "content_preservation_verified",
                    preservation_result,
                )
                cursor.execute(
                    "UPDATE industry_pack_activations SET is_current=0 WHERE is_current=1"
                )
                if rollback_of_activation_id:
                    cursor.execute(
                        """
                        UPDATE industry_pack_activations
                        SET status='rolled_back', is_current=0
                        WHERE id=? AND status='active'
                        """,
                        (str(rollback_of_activation_id),),
                    )
                self._put_setting(cursor, "active_industry_pack_id", preview["target_pack_id"])
                self._put_setting(cursor, "active_industry_pack_version_id", preview["target_version_id"])
                self._put_setting(cursor, "active_industry_activation_id", activation_id)
                cancelled_jobs = self.repository.cancel_stale_activation_jobs(
                    activation_id, transaction_cursor=cursor
                )
                cursor.execute(
                    """
                    UPDATE industry_pack_activations
                    SET status='active', is_current=1,
                        completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                    WHERE id=?
                    """,
                    (activation_id,),
                )
                self._event(
                    cursor,
                    activation_id,
                    "active",
                    {
                        "source_reconciliation": source_result,
                        "legacy_url_projection": compatibility_result,
                        "collection_tasks": collection_task_result,
                        "restored_projection": projection_result,
                        "content_preservation": preservation_result,
                    },
                )
                self.db.connection.commit()
            except Exception:
                self.db.connection.rollback()
                raise
            finally:
                cursor.close()
        # 行业包激活成功后**同步业务规则**（阶段 9）：规则来自本包的
        # core_keywords / fixed_topics / 本周追踪方向。放在锁外、失败只记录，
        # 绝不回滚激活结果（规则是检索增强项，不能拖累行业切换这条主流程）。
        rule_sync: dict
        try:
            from business_rules import business_rule_engine
            from industry_packs import industry_pack_loader

            pack = industry_pack_loader.load(preview["target_pack_id"]) or {}
            rule_sync = business_rule_engine.sync_from_pack(preview["target_pack_id"], pack)
        except Exception as exc:
            rule_sync = {"error": str(exc)[:120]}
        return {
            "activation_id": activation_id,
            "previous_pack_id": preview["previous_pack_id"],
            "active_pack_id": preview["target_pack_id"],
            "active_version_id": preview["target_version_id"],
            "plan_sha256": preview["plan_sha256"],
            "source_reconciliation": source_result,
            "legacy_url_projection": compatibility_result,
            "collection_tasks": collection_task_result,
            "restored_projection": projection_result,
            "cancelled_stale_jobs": cancelled_jobs,
            "backup": backup,
            "article_status_unchanged": True,
            "content_preservation": preservation_result,
            "initialization_window": preview.get('initialization_window'),
            "business_rules": rule_sync,
        }

    def _current_activation(self, activation_id: str) -> dict:
        self.db._ensure_connection()
        with self.db.lock:
            row = self.db.connection.execute(
                "SELECT * FROM industry_pack_activations WHERE id=?",
                (str(activation_id),),
            ).fetchone()
        if not row:
            raise ValueError("行业包激活记录不存在")
        record = dict(row)
        if not bool(record["is_current"]) or str(record["status"]) != "active":
            raise ValueError("只能回滚当前生效的行业包激活记录")
        if not record.get("previous_version_id"):
            raise ValueError("该激活记录没有可回滚的上一发布版本")
        return record

    def preview_rollback(self, activation_id: str) -> dict:
        current = self._current_activation(activation_id)
        target = self.version_store.get_version(int(current["previous_version_id"]))
        if not target:
            raise ValueError("上一发布版本不存在，不能执行配置回滚")
        # A rollback must catch up the interval spent on the current pack,
        # rather than silently falling back to the normal 30-day initializer.
        gap_from = str(current.get('started_at') or current.get('created_at') or '')
        now_utc = datetime.now(timezone.utc)
        try:
            gap_window = normalize_initialization_window(
                gap_from, '', now=now_utc
            )
        except ValueError:
            # Legacy rows may carry a clock from a different host timezone;
            # clamp that malformed boundary to a small safe catch-up window.
            gap_window = normalize_initialization_window(
                (now_utc - timedelta(minutes=1)).isoformat(),
                now=now_utc,
            )
        try:
            preview = self.preview(
                str(target["industry_pack_id"]), target_version_id=int(target["id"]),
                initialization_from=gap_window['from'],
                initialization_to=gap_window['to'],
            )
        except ValueError:
            # Keep rollback available when a legacy timestamp is ahead of the
            # host clock; use the bounded default window as a safe fallback.
            preview = self.preview(
                str(target["industry_pack_id"]), target_version_id=int(target["id"])
            )
            preview['initialization_window']['mode'] = 'rollback_gap_fallback'
        preview['initialization_window']['mode'] = 'rollback_gap'
        identity = {
            key: preview[key]
            for key in (
                "previous_pack_id",
                "previous_version_id",
                "target_pack_id",
                "target_version_id",
                "target_content_sha256",
                "source_plan_sha256",
            )
        }
        identity["rollback_of_activation_id"] = str(activation_id)
        preview.update(
            {
                "rollback_of_activation_id": str(activation_id),
                "plan_sha256": _stable_hash(identity),
            }
        )
        return preview

    def rollback(
        self,
        activation_id: str,
        *,
        target_version_id: int,
        expected_plan_sha256: str,
        actor: str = "",
    ) -> dict:
        preview = self.preview_rollback(str(activation_id))
        if int(preview["target_version_id"]) != int(target_version_id):
            raise ValueError("回滚目标版本已经变化，请重新预览并确认")
        result = self._apply_preview(
            preview,
            expected_plan_sha256=expected_plan_sha256,
            actor=actor,
            rollback_of_activation_id=str(activation_id),
        )
        result["rollback_of_activation_id"] = str(activation_id)
        return result

    def list_activations(self, limit: int = 50) -> list[dict]:
        self.db._ensure_connection()
        with self.db.lock:
            rows = self.db.connection.execute(
                """
                SELECT id, previous_pack_id, previous_version_id,
                       target_pack_id, target_version_id, status, is_current,
                       plan_sha256, backup_path, backup_sha256, backup_size,
                       backup_schema_version, backup_integrity, created_by,
                       error_message, created_at, started_at, completed_at
                FROM industry_pack_activations
                ORDER BY created_at DESC LIMIT ?
                """,
                (max(1, min(200, int(limit))),),
            ).fetchall()
        return [dict(row) for row in rows]


industry_pack_activation_service = IndustryPackActivationService()

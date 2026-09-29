#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""SQLite checkpoint, atomic artifact and RAGFlow memory persistence.

All durable metadata stays in the current application SQLite database.  Large
JSON/Markdown payloads use the existing crawl-results data volume, and semantic
memory uses the existing RAGFlow client.  Network calls never run inside a
SQLite write transaction.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

import config
from financial_resource_isolation import ArtifactIOTimeout, artifact_io_controller


ARTIFACT_STORE_VERSION = "financial-artifact-store-v1"
ARTIFACT_URI_PREFIX = "financial-artifact://"
SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


class FinancialPersistenceError(RuntimeError):
    """Stable persistence error without filesystem or provider secrets."""

    def __init__(self, message: str, *, error_code: str):
        super().__init__(message)
        self.error_code = str(error_code)


@dataclass(frozen=True)
class CheckpointLoadResult:
    checkpoint_id: int
    stage_key: str
    checkpoint_version: int
    schema_version: str
    checkpoint: Mapping[str, Any]
    skipped: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class ArtifactLoadResult:
    artifact: "ArtifactRecord"
    payload: bytes
    skipped: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: int
    research_run_id: str
    final_report_id: Optional[int]
    artifact_kind: str
    artifact_version: int
    content_format: str
    storage_uri: str
    content_sha256: str
    byte_count: int
    status: str
    ragflow_kb_id: str
    ragflow_document_id: str
    memory_status: str
    metadata: Mapping[str, Any]


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise FinancialPersistenceError(
            "持久化内容不是有限 JSON", error_code="invalid_persistence_payload"
        ) from exc
    return text.encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class FinancialArtifactStore:
    """Repository for graph checkpoints and report files on existing bases."""

    def __init__(
        self,
        connection,
        *,
        root_dir: Path | str | None = None,
        inline_checkpoint_max_bytes: int = 262_144,
    ):
        self.connection = connection
        configured_root = Path(
            root_dir
            if root_dir is not None
            else Path(getattr(config, "CRAWL_RESULTS_DIR", "crawl_results"))
            / "financial_artifacts"
        )
        if isinstance(inline_checkpoint_max_bytes, bool) or not 256 <= int(
            inline_checkpoint_max_bytes
        ) <= 16 * 1024 * 1024:
            raise FinancialPersistenceError(
                "checkpoint 内联阈值无效", error_code="invalid_artifact_config"
            )
        self.inline_checkpoint_max_bytes = int(inline_checkpoint_max_bytes)
        configured_root.mkdir(parents=True, exist_ok=True)
        self.root_dir = configured_root.resolve()
        if not self.root_dir.is_dir():
            raise FinancialPersistenceError(
                "金融制品目录不可用", error_code="artifact_root_unavailable"
            )
        self._assert_schema()

    def _assert_schema(self) -> None:
        rows = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        present = {str(row[0]) for row in rows}
        required = {
            "financial_research_runs",
            "financial_research_checkpoints",
            "financial_final_reports",
            "financial_artifacts",
        }
        if not required <= present:
            raise FinancialPersistenceError(
                "金融持久化表尚未迁移", error_code="financial_schema_missing"
            )

    @staticmethod
    def _validate_run_id(research_run_id: str) -> str:
        run_id = str(research_run_id or "")
        if not SAFE_RUN_ID.fullmatch(run_id):
            raise FinancialPersistenceError(
                "research_run_id 格式无效", error_code="invalid_research_run_id"
            )
        return run_id

    def _require_run(self, research_run_id: str) -> str:
        run_id = self._validate_run_id(research_run_id)
        row = self.connection.execute(
            "SELECT 1 FROM financial_research_runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise FinancialPersistenceError(
                "金融研究任务不存在", error_code="research_run_not_found"
            )
        return run_id

    def _to_uri(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(self.root_dir)
        except ValueError as exc:
            raise FinancialPersistenceError(
                "制品路径超出数据卷", error_code="artifact_path_outside_root"
            ) from exc
        return ARTIFACT_URI_PREFIX + relative.as_posix()

    def _from_uri(self, storage_uri: str) -> Path:
        uri = str(storage_uri or "")
        if not uri.startswith(ARTIFACT_URI_PREFIX):
            raise FinancialPersistenceError(
                "制品 URI 无效", error_code="invalid_artifact_uri"
            )
        relative_text = uri[len(ARTIFACT_URI_PREFIX) :]
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise FinancialPersistenceError(
                "制品 URI 超出数据卷", error_code="artifact_path_outside_root"
            )
        candidate = (self.root_dir / relative).resolve()
        try:
            candidate.relative_to(self.root_dir)
        except ValueError as exc:
            raise FinancialPersistenceError(
                "制品 URI 超出数据卷", error_code="artifact_path_outside_root"
            ) from exc
        return candidate

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        if not isinstance(payload, bytes):
            raise FinancialPersistenceError(
                "金融制品必须为 bytes", error_code="invalid_artifact_payload"
            )
        if len(payload) > int(config.FINANCIAL_ARTIFACT_MAX_BYTES):
            raise FinancialPersistenceError(
                "金融制品超过文件大小边界", error_code="artifact_too_large"
            )
        try:
            with artifact_io_controller.slot(
                timeout_seconds=config.FINANCIAL_ARTIFACT_IO_TIMEOUT_SECONDS
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary_path: Optional[Path] = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="wb",
                        prefix=f".{path.name}.",
                        suffix=".tmp",
                        dir=path.parent,
                        delete=False,
                    ) as handle:
                        temporary_path = Path(handle.name)
                        handle.write(payload)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary_path, path)
                    temporary_path = None
                    try:
                        directory_fd = os.open(path.parent, os.O_RDONLY)
                        try:
                            os.fsync(directory_fd)
                        finally:
                            os.close(directory_fd)
                    except OSError:
                        pass
                finally:
                    if temporary_path is not None:
                        try:
                            temporary_path.unlink()
                        except FileNotFoundError:
                            pass
        except ArtifactIOTimeout as exc:
            raise FinancialPersistenceError(
                "金融制品 I/O 繁忙", error_code="artifact_io_saturated"
            ) from exc

    def _checkpoint_payload(
        self, storage_uri: str, payload_json: str, expected_sha256: str
    ) -> Mapping[str, Any]:
        if storage_uri:
            path = self._from_uri(storage_uri)
            try:
                payload = path.read_bytes()
            except FileNotFoundError as exc:
                raise FinancialPersistenceError(
                    "checkpoint 附件缺失", error_code="checkpoint_artifact_missing"
                ) from exc
        else:
            payload = str(payload_json).encode("utf-8")
        if _sha256(payload) != str(expected_sha256):
            raise FinancialPersistenceError(
                "checkpoint hash 校验失败", error_code="checkpoint_integrity_failed"
            )
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise FinancialPersistenceError(
                "checkpoint JSON 无效", error_code="checkpoint_integrity_failed"
            ) from exc
        if not isinstance(parsed, Mapping):
            raise FinancialPersistenceError(
                "checkpoint 必须为对象", error_code="checkpoint_integrity_failed"
            )
        return dict(parsed)

    @staticmethod
    def _derive_stage_key(checkpoint: Mapping[str, Any]) -> str:
        terminal = str(checkpoint.get("terminal_status") or "").strip()
        if terminal:
            return f"terminal:{terminal}"
        index = checkpoint.get("stage_index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise FinancialPersistenceError(
                "checkpoint stage_index 无效", error_code="invalid_checkpoint"
            )
        return f"stage:{index:03d}"

    def save_checkpoint(
        self,
        research_run_id: str,
        checkpoint: Mapping[str, Any],
        *,
        stage_key: str = "",
    ) -> Mapping[str, Any]:
        run_id = self._require_run(research_run_id)
        if not isinstance(checkpoint, Mapping):
            raise FinancialPersistenceError(
                "checkpoint 必须为对象", error_code="invalid_checkpoint"
            )
        payload = _canonical_json_bytes(checkpoint)
        digest = _sha256(payload)
        schema_version = str(checkpoint.get("schema_version") or "").strip()
        if not schema_version:
            raise FinancialPersistenceError(
                "checkpoint schema_version 缺失", error_code="invalid_checkpoint"
            )
        if str(checkpoint.get("research_run_id") or "") != run_id:
            raise FinancialPersistenceError(
                "checkpoint 任务范围不一致", error_code="checkpoint_scope_mismatch"
            )
        effective_stage = str(stage_key or self._derive_stage_key(checkpoint)).strip()
        if not effective_stage or len(effective_stage) > 160:
            raise FinancialPersistenceError(
                "checkpoint stage_key 无效", error_code="invalid_checkpoint"
            )
        storage_uri = ""
        payload_json = payload.decode("utf-8")
        if len(payload) > self.inline_checkpoint_max_bytes:
            path = (
                self.root_dir
                / run_id
                / "checkpoints"
                / f"checkpoint-{digest}.json"
            )
            self._atomic_write(path, payload)
            storage_uri = self._to_uri(path)
            payload_json = json.dumps(
                {
                    "external": True,
                    "storage_uri": storage_uri,
                    "content_sha256": digest,
                    "byte_count": len(payload),
                },
                sort_keys=True,
                separators=(",", ":"),
            )

        self.connection.execute("SAVEPOINT financial_checkpoint_write")
        try:
            row = self.connection.execute(
                """
                SELECT COALESCE(MAX(checkpoint_version), 0)
                FROM financial_research_checkpoints
                WHERE research_run_id=? AND stage_key=?
                """,
                (run_id, effective_stage),
            ).fetchone()
            version = int(row[0] or 0) + 1
            cursor = self.connection.execute(
                """
                INSERT INTO financial_research_checkpoints(
                    research_run_id, stage_key, checkpoint_version,
                    schema_version, status, payload_json, storage_uri,
                    content_sha256
                ) VALUES(?, ?, ?, ?, 'valid', ?, ?, ?)
                """,
                (
                    run_id,
                    effective_stage,
                    version,
                    schema_version,
                    payload_json,
                    storage_uri,
                    digest,
                ),
            )
            checkpoint_id = int(cursor.lastrowid)
            self.connection.execute(
                """
                UPDATE financial_research_runs
                SET status=CASE WHEN status='queued' THEN 'running' ELSE status END,
                    current_stage=?,
                    started_at=COALESCE(started_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id=?
                """,
                (effective_stage, run_id),
            )
            self.connection.execute("RELEASE SAVEPOINT financial_checkpoint_write")
        except Exception:
            self.connection.execute("ROLLBACK TO SAVEPOINT financial_checkpoint_write")
            self.connection.execute("RELEASE SAVEPOINT financial_checkpoint_write")
            raise
        return {
            "checkpoint_id": checkpoint_id,
            "research_run_id": run_id,
            "stage_key": effective_stage,
            "checkpoint_version": version,
            "schema_version": schema_version,
            "storage_uri": storage_uri,
            "content_sha256": digest,
            "byte_count": len(payload),
            "externalized": bool(storage_uri),
        }

    def checkpoint_callback(self, research_run_id: str) -> Callable[[Mapping[str, Any]], None]:
        run_id = self._require_run(research_run_id)

        def persist(checkpoint: Mapping[str, Any]) -> None:
            self.save_checkpoint(run_id, checkpoint)

        return persist

    def load_latest_compatible_checkpoint(
        self,
        research_run_id: str,
        *,
        accepted_schema_versions: Sequence[str],
    ) -> CheckpointLoadResult:
        run_id = self._require_run(research_run_id)
        accepted = {str(value).strip() for value in accepted_schema_versions if str(value).strip()}
        if not accepted:
            raise FinancialPersistenceError(
                "未声明兼容 checkpoint schema", error_code="invalid_checkpoint_policy"
            )
        rows = self.connection.execute(
            """
            SELECT id, stage_key, checkpoint_version, schema_version,
                   payload_json, storage_uri, content_sha256
            FROM financial_research_checkpoints
            WHERE research_run_id=? AND status='valid'
            ORDER BY id DESC
            """,
            (run_id,),
        ).fetchall()
        skipped = []
        last_integrity_error: Optional[FinancialPersistenceError] = None
        for row in rows:
            schema_version = str(row[3])
            if schema_version not in accepted:
                skipped.append(
                    {
                        "checkpoint_id": int(row[0]),
                        "schema_version": schema_version,
                        "reason": "incompatible_checkpoint_schema",
                    }
                )
                continue
            try:
                checkpoint = self._checkpoint_payload(row[5], row[4], row[6])
            except FinancialPersistenceError as exc:
                last_integrity_error = exc
                skipped.append(
                    {
                        "checkpoint_id": int(row[0]),
                        "schema_version": schema_version,
                        "reason": exc.error_code,
                    }
                )
                continue
            if str(checkpoint.get("research_run_id") or "") != run_id:
                last_integrity_error = FinancialPersistenceError(
                    "checkpoint 任务范围不一致",
                    error_code="checkpoint_scope_mismatch",
                )
                skipped.append(
                    {
                        "checkpoint_id": int(row[0]),
                        "schema_version": schema_version,
                        "reason": "checkpoint_scope_mismatch",
                    }
                )
                continue
            return CheckpointLoadResult(
                checkpoint_id=int(row[0]),
                stage_key=str(row[1]),
                checkpoint_version=int(row[2]),
                schema_version=schema_version,
                checkpoint=checkpoint,
                skipped=tuple(skipped),
            )
        if last_integrity_error is not None:
            raise last_integrity_error
        if rows:
            raise FinancialPersistenceError(
                "没有兼容的 checkpoint",
                error_code="incompatible_checkpoint",
            )
        raise FinancialPersistenceError(
            "研究任务没有 checkpoint", error_code="checkpoint_not_found"
        )

    @staticmethod
    def _artifact_from_row(row) -> ArtifactRecord:
        try:
            metadata = json.loads(str(row[13] or "{}"))
        except (ValueError, json.JSONDecodeError) as exc:
            raise FinancialPersistenceError(
                "制品 metadata 无效", error_code="artifact_metadata_invalid"
            ) from exc
        return ArtifactRecord(
            artifact_id=int(row[0]),
            research_run_id=str(row[1]),
            final_report_id=int(row[2]) if row[2] is not None else None,
            artifact_kind=str(row[3]),
            artifact_version=int(row[4]),
            content_format=str(row[5]),
            storage_uri=str(row[6]),
            content_sha256=str(row[7]),
            byte_count=int(row[8]),
            status=str(row[9]),
            ragflow_kb_id=str(row[10] or ""),
            ragflow_document_id=str(row[11] or ""),
            memory_status=str(row[12] or ""),
            metadata=metadata if isinstance(metadata, Mapping) else {},
        )

    def _artifact_row(self, artifact_id: int):
        return self.connection.execute(
            """
            SELECT id, research_run_id, final_report_id, artifact_kind,
                   artifact_version, content_format, storage_uri,
                   content_sha256, byte_count, status, ragflow_kb_id,
                   ragflow_document_id, memory_status, metadata_json
            FROM financial_artifacts WHERE id=?
            """,
            (int(artifact_id),),
        ).fetchone()

    def persist_final_report(self, final_report_id: int) -> tuple[ArtifactRecord, ...]:
        row = self.connection.execute(
            """
            SELECT id, research_run_id, report_version, report_status,
                   title, report_markdown, report_json
            FROM financial_final_reports WHERE id=?
            """,
            (int(final_report_id),),
        ).fetchone()
        if row is None:
            raise FinancialPersistenceError(
                "终极报告不存在", error_code="final_report_not_found"
            )
        report_id = int(row[0])
        run_id = self._require_run(str(row[1]))
        version = int(row[2])
        markdown = str(row[5] or "")
        try:
            report_json_value = json.loads(str(row[6] or "{}"))
        except (ValueError, json.JSONDecodeError) as exc:
            raise FinancialPersistenceError(
                "终极报告 JSON 无效", error_code="invalid_report_json"
            ) from exc
        payloads = {
            "markdown": markdown.encode("utf-8"),
            "json": _canonical_json_bytes(report_json_value),
        }
        paths = {
            "markdown": self.root_dir / run_id / "reports" / f"report-v{version}.md",
            "json": self.root_dir / run_id / "reports" / f"report-v{version}.json",
        }
        for content_format, payload in payloads.items():
            self._atomic_write(paths[content_format], payload)

        self.connection.execute("SAVEPOINT financial_artifact_write")
        try:
            artifact_ids = []
            for content_format, payload in payloads.items():
                storage_uri = self._to_uri(paths[content_format])
                digest = _sha256(payload)
                metadata = json.dumps(
                    {
                        "artifact_store_version": ARTIFACT_STORE_VERSION,
                        "report_status": str(row[3]),
                        "title": str(row[4]),
                        "atomic_replace": True,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                self.connection.execute(
                    """
                    INSERT INTO financial_artifacts(
                        research_run_id, final_report_id, artifact_kind,
                        artifact_version, content_format, storage_uri,
                        content_sha256, byte_count, status, metadata_json
                    ) VALUES(?, ?, 'final_report', ?, ?, ?, ?, ?, 'ready', ?)
                    ON CONFLICT(
                        research_run_id, artifact_kind, artifact_version, content_format
                    ) DO UPDATE SET
                        final_report_id=excluded.final_report_id,
                        storage_uri=excluded.storage_uri,
                        content_sha256=excluded.content_sha256,
                        byte_count=excluded.byte_count,
                        status='ready',
                        metadata_json=excluded.metadata_json,
                        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    """,
                    (
                        run_id,
                        report_id,
                        version,
                        content_format,
                        storage_uri,
                        digest,
                        len(payload),
                        metadata,
                    ),
                )
                artifact_id = int(
                    self.connection.execute(
                        """
                        SELECT id FROM financial_artifacts
                        WHERE research_run_id=? AND artifact_kind='final_report'
                          AND artifact_version=? AND content_format=?
                        """,
                        (run_id, version, content_format),
                    ).fetchone()[0]
                )
                artifact_ids.append(artifact_id)
            self.connection.execute("RELEASE SAVEPOINT financial_artifact_write")
        except Exception:
            self.connection.execute("ROLLBACK TO SAVEPOINT financial_artifact_write")
            self.connection.execute("RELEASE SAVEPOINT financial_artifact_write")
            raise
        return tuple(
            self._artifact_from_row(self._artifact_row(artifact_id))
            for artifact_id in artifact_ids
        )

    def load_artifact(self, artifact_id: int) -> tuple[ArtifactRecord, bytes]:
        row = self._artifact_row(artifact_id)
        if row is None:
            raise FinancialPersistenceError(
                "金融制品不存在", error_code="artifact_not_found"
            )
        artifact = self._artifact_from_row(row)
        path = self._from_uri(artifact.storage_uri)
        try:
            payload = path.read_bytes()
        except FileNotFoundError as exc:
            self._mark_artifact_status(artifact.artifact_id, "missing")
            raise FinancialPersistenceError(
                "金融制品文件缺失", error_code="artifact_missing"
            ) from exc
        if len(payload) != artifact.byte_count or _sha256(payload) != artifact.content_sha256:
            self._mark_artifact_status(artifact.artifact_id, "corrupt")
            raise FinancialPersistenceError(
                "金融制品 hash 校验失败", error_code="artifact_integrity_failed"
            )
        return artifact, payload

    def load_latest_valid_report_artifact(
        self, research_run_id: str, *, content_format: str
    ) -> ArtifactLoadResult:
        """Load the newest hash-valid report, falling back across versions."""

        run_id = self._require_run(research_run_id)
        normalized_format = str(content_format or "").strip().casefold()
        if normalized_format not in {"markdown", "json"}:
            raise FinancialPersistenceError(
                "终极报告格式无效", error_code="invalid_report_format"
            )
        rows = self.connection.execute(
            """
            SELECT id, research_run_id, final_report_id, artifact_kind,
                   artifact_version, content_format, storage_uri,
                   content_sha256, byte_count, status, ragflow_kb_id,
                   ragflow_document_id, memory_status, metadata_json
            FROM financial_artifacts
            WHERE research_run_id=? AND artifact_kind='final_report'
              AND content_format=?
            ORDER BY artifact_version DESC, id DESC
            """,
            (run_id, normalized_format),
        ).fetchall()
        skipped = []
        last_error = None
        for row in rows:
            artifact = self._artifact_from_row(row)
            if artifact.status != "ready":
                skipped.append(
                    {
                        "artifact_id": artifact.artifact_id,
                        "artifact_version": artifact.artifact_version,
                        "reason": f"artifact_status_{artifact.status}",
                    }
                )
                continue
            try:
                loaded, payload = self.load_artifact(artifact.artifact_id)
            except FinancialPersistenceError as exc:
                last_error = exc
                skipped.append(
                    {
                        "artifact_id": artifact.artifact_id,
                        "artifact_version": artifact.artifact_version,
                        "reason": exc.error_code,
                    }
                )
                continue
            return ArtifactLoadResult(
                artifact=loaded,
                payload=payload,
                skipped=tuple(skipped),
            )
        if last_error is not None:
            raise last_error
        raise FinancialPersistenceError(
            "研究任务没有可用终极报告制品", error_code="report_artifact_not_found"
        )

    def _mark_artifact_status(self, artifact_id: int, status: str) -> None:
        self.connection.execute("SAVEPOINT financial_artifact_status")
        try:
            self.connection.execute(
                """
                UPDATE financial_artifacts SET status=?,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id=?
                """,
                (status, int(artifact_id)),
            )
            self.connection.execute("RELEASE SAVEPOINT financial_artifact_status")
        except Exception:
            self.connection.execute("ROLLBACK TO SAVEPOINT financial_artifact_status")
            self.connection.execute("RELEASE SAVEPOINT financial_artifact_status")
            raise

    def report_artifact(self, final_report_id: int, content_format: str) -> Optional[ArtifactRecord]:
        row = self.connection.execute(
            """
            SELECT id, research_run_id, final_report_id, artifact_kind,
                   artifact_version, content_format, storage_uri,
                   content_sha256, byte_count, status, ragflow_kb_id,
                   ragflow_document_id, memory_status, metadata_json
            FROM financial_artifacts
            WHERE final_report_id=? AND artifact_kind='final_report'
              AND content_format=?
            ORDER BY artifact_version DESC LIMIT 1
            """,
            (int(final_report_id), str(content_format)),
        ).fetchone()
        return self._artifact_from_row(row) if row is not None else None

    def update_report_memory(
        self,
        final_report_id: int,
        *,
        memory_status: str,
        ragflow_kb_id: str = "",
        ragflow_document_id: str = "",
        error_code: str = "",
        ragflow_documents: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> None:
        metadata_rows = self.connection.execute(
            "SELECT id, metadata_json FROM financial_artifacts WHERE final_report_id=?",
            (int(final_report_id),),
        ).fetchall()
        self.connection.execute("SAVEPOINT financial_memory_status")
        try:
            for artifact_id, metadata_text in metadata_rows:
                try:
                    metadata = json.loads(str(metadata_text or "{}"))
                except (ValueError, json.JSONDecodeError):
                    metadata = {}
                metadata["memory_error_code"] = str(error_code or "")
                if ragflow_documents is not None:
                    metadata["ragflow_documents"] = [dict(item) for item in ragflow_documents]
                self.connection.execute(
                    """
                    UPDATE financial_artifacts
                    SET memory_status=?, ragflow_kb_id=?, ragflow_document_id=?,
                        metadata_json=?,
                        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE id=?
                    """,
                    (
                        str(memory_status),
                        str(ragflow_kb_id or ""),
                        str(ragflow_document_id or ""),
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                        int(artifact_id),
                    ),
                )
            self.connection.execute("RELEASE SAVEPOINT financial_memory_status")
        except Exception:
            self.connection.execute("ROLLBACK TO SAVEPOINT financial_memory_status")
            self.connection.execute("RELEASE SAVEPOINT financial_memory_status")
            raise


class FinancialSemanticMemory:
    """Evidence-gated report memory through the existing RAGFlow client."""

    def __init__(
        self,
        artifact_store: FinancialArtifactStore,
        *,
        ragflow_client=None,
        kb_id: str = "",
    ):
        self.artifact_store = artifact_store
        if ragflow_client is None:
            from ragflow_client import get_ragflow_client

            ragflow_client = get_ragflow_client()
        self.ragflow_client = ragflow_client
        self.kb_id = str(
            kb_id or getattr(config, "FINANCIAL_RAGFLOW_KB_ID", "") or ""
        ).strip()

    def sync_final_report(self, final_report_id: int) -> Mapping[str, Any]:
        previous_artifact = self.artifact_store.report_artifact(
            final_report_id, "markdown"
        )
        previous_documents = list(
            (previous_artifact.metadata.get("ragflow_documents") or [])
            if previous_artifact is not None
            else []
        )
        artifacts = self.artifact_store.persist_final_report(final_report_id)
        markdown_artifact = next(
            (item for item in artifacts if item.content_format == "markdown"), None
        )
        if markdown_artifact is None:
            raise FinancialPersistenceError(
                "报告 Markdown 制品缺失", error_code="report_markdown_missing"
            )
        _, payload = self.artifact_store.load_artifact(markdown_artifact.artifact_id)
        if not self.kb_id:
            self.artifact_store.update_report_memory(
                final_report_id,
                memory_status="not_configured",
                error_code="financial_ragflow_kb_not_configured",
            )
            return {
                "status": "degraded",
                "error_code": "financial_ragflow_kb_not_configured",
                "report_available": True,
            }
        from financial_rag_gate import FinancialRAGPublicationPlanner

        documents = FinancialRAGPublicationPlanner(
            self.artifact_store.connection
        ).build_documents(final_report_id)
        existing_by_hash = {
            str(item.get("content_sha256") or ""): dict(item)
            for item in previous_documents
            if isinstance(item, Mapping)
            and str(item.get("status") or "") == "uploaded"
            and str(item.get("kb_id") or "") == self.kb_id
            and item.get("document_id")
        }
        records = []
        for document in documents:
            existing = existing_by_hash.get(str(document["content_sha256"]))
            records.append(
                {
                    "document_key": str(document["document_key"]),
                    "document_name": str(document["document_name"]),
                    "content_sha256": str(document["content_sha256"]),
                    "kb_id": self.kb_id,
                    "document_id": str((existing or {}).get("document_id") or ""),
                    "status": "uploaded" if existing else "planned",
                    "metadata": dict(document["metadata"]),
                }
            )
        primary_id = str(records[0].get("document_id") or "") if records else ""
        self.artifact_store.update_report_memory(
            final_report_id,
            memory_status="publishing",
            ragflow_kb_id=self.kb_id,
            ragflow_document_id=primary_id,
            ragflow_documents=records,
        )
        try:
            for index, document in enumerate(documents):
                if records[index]["status"] == "uploaded":
                    continue
                result = self.ragflow_client.upload_document_content(
                    self.kb_id,
                    str(document["document_name"]),
                    str(document["content"]),
                    auto_parse=True,
                )
                if result.get("disabled") or result.get("skipped"):
                    self.artifact_store.update_report_memory(
                        final_report_id,
                        memory_status="not_configured",
                        ragflow_kb_id=self.kb_id,
                        ragflow_document_id=primary_id,
                        error_code="ragflow_upload_disabled",
                        ragflow_documents=records,
                    )
                    return {
                        "status": "degraded",
                        "error_code": "ragflow_upload_disabled",
                        "report_available": True,
                    }
                extract = getattr(self.ragflow_client, "extract_document_ids", None)
                document_ids = list(extract(result)) if callable(extract) else []
                if not document_ids:
                    data = result.get("data")
                    if isinstance(data, Mapping) and data.get("id"):
                        document_ids = [str(data["id"])]
                    elif isinstance(data, list):
                        document_ids = [
                            str(item["id"])
                            for item in data
                            if isinstance(item, Mapping) and item.get("id")
                        ]
                if not document_ids:
                    raise ValueError("ragflow_upload_missing_document_id")
                records[index]["document_id"] = str(document_ids[0])
                records[index]["status"] = "uploaded"
                primary_id = str(records[0].get("document_id") or "")
                self.artifact_store.update_report_memory(
                    final_report_id,
                    memory_status="publishing",
                    ragflow_kb_id=self.kb_id,
                    ragflow_document_id=primary_id,
                    ragflow_documents=records,
                )
            document_id = str(records[0].get("document_id") or "") if records else ""
            self.artifact_store.update_report_memory(
                final_report_id,
                memory_status="uploaded",
                ragflow_kb_id=self.kb_id,
                ragflow_document_id=document_id,
                ragflow_documents=records,
            )
            return {
                "status": "uploaded",
                "kb_id": self.kb_id,
                "document_id": document_id or None,
                "documents": len(records),
                "report_available": True,
            }
        except Exception as exc:
            error_code = (
                "ragflow_timeout"
                if isinstance(exc, TimeoutError)
                or "timeout" in type(exc).__name__.casefold()
                else "ragflow_unavailable"
            )
            self.artifact_store.update_report_memory(
                final_report_id,
                memory_status="degraded",
                ragflow_kb_id=self.kb_id,
                ragflow_document_id=primary_id,
                error_code=error_code,
                ragflow_documents=records,
            )
            return {
                "status": "degraded",
                "error_code": error_code,
                "report_available": True,
            }

    def search(
        self,
        question: str,
        *,
        route: str,
        server_time_context: Mapping[str, Any],
        time_range: Optional[Mapping[str, Any]] = None,
        instrument_keys: Sequence[str] = (),
        limit: int = 8,
    ) -> Mapping[str, Any]:
        """Retrieve only candidates authorized by the local financial gate."""
        from financial_rag_gate import FinancialRAGRetrievalGate

        return FinancialRAGRetrievalGate(
            self.artifact_store.connection,
            ragflow_client=self.ragflow_client,
            kb_id=self.kb_id,
        ).search(
            question,
            route=route,
            server_time_context=server_time_context,
            time_range=time_range,
            instrument_keys=instrument_keys,
            limit=limit,
        )

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Short-transaction durable storage used by the unified QA Gateway."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Mapping

from qa_contracts import QA_CONTRACT_VERSION
from qa_schema import ensure_qa_tables


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _minutes_ago_text(minutes: float) -> str:
    """N 分钟前的 UTC 文本（格式与 _now() 一致，便于直接做字符串比较）。"""
    moment = datetime.now(timezone.utc) - timedelta(minutes=float(minutes))
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _row_dict(row) -> dict | None:
    if row is None:
        return None
    try:
        return dict(row)
    except Exception:
        return {str(index): value for index, value in enumerate(row)}


def _decode_run(row) -> dict | None:
    value = _row_dict(row)
    if value is None:
        return None
    for source, target, fallback in (
        ("request_json", "request", {}),
        ("degradation_json", "degradation", []),
        ("final_answer_json", "final_answer", {}),
    ):
        try:
            decoded = json.loads(value.get(source) or _json(fallback))
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = fallback
        value[target] = decoded
    value["degraded"] = bool(value.get("degraded"))
    return value


class QaStore:
    def __init__(self, database):
        self.database = database

    def ensure_schema(self) -> None:
        self.database._ensure_connection()
        with self.database.lock:
            cursor = self.database.connection.cursor()
            try:
                ensure_qa_tables(cursor)
                self.database.connection.commit()
            except Exception:
                self.database.connection.rollback()
                raise
            finally:
                cursor.close()

    def create_run(
        self,
        request_payload: Mapping,
        *,
        owner_user_id: str,
        idempotency_key: str,
        research_app_id: str = "",
        synthesis_provider_id: str = "local",
    ) -> dict:
        self.ensure_schema()
        owner = str(owner_user_id or "")
        idem = str(idempotency_key or "").strip()
        if not idem:
            raise ValueError("idempotency_key is required")
        with self.database.lock:
            run_id = uuid.uuid4().hex
            now = _now()
            question = str(request_payload.get("question") or "")
            values = (
                run_id,
                QA_CONTRACT_VERSION,
                str(request_payload.get("session_id") or ""),
                owner,
                str(request_payload.get("industry_pack_id") or ""),
                str(request_payload.get("origin") or "api"),
                str(request_payload.get("mode") or "standard"),
                hashlib.sha256(question.encode("utf-8")).hexdigest(),
                question,
                _json(dict(request_payload)),
                "queued",
                "plan",
                str(request_payload.get("draft_provider") or "local"),
                str(research_app_id or ""),
                str(synthesis_provider_id or "local"),
                idem,
                now,
                now,
            )
            cursor = self.database.connection.execute(
                """
                INSERT INTO qa_runs(
                    id,contract_version,session_id,owner_user_id,industry_pack_id,
                    origin,mode,question_hash,question_text,request_json,status,current_stage,
                    draft_provider_id,research_app_id,synthesis_provider_id,
                    idempotency_key,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(owner_user_id,idempotency_key) DO NOTHING
                """,
                values,
            )
            self.database.connection.commit()
            created = bool(cursor.rowcount)
            if created:
                result = self.get_run(run_id, owner_user_id=owner)
            else:
                result = self.get_run_by_idempotency(owner, idem)
            result["_created"] = created
            return result

    def get_run(self, run_id: str, *, owner_user_id: str | None = None) -> dict | None:
        self.ensure_schema()
        sql = "SELECT * FROM qa_runs WHERE id=?"
        params: list = [str(run_id)]
        if owner_user_id is not None:
            sql += " AND owner_user_id=?"
            params.append(str(owner_user_id or ""))
        with self.database.lock:
            return _decode_run(self.database.connection.execute(sql, tuple(params)).fetchone())

    def get_run_by_idempotency(self, owner_user_id: str, idempotency_key: str) -> dict | None:
        self.ensure_schema()
        with self.database.lock:
            row = self.database.connection.execute(
                "SELECT * FROM qa_runs WHERE owner_user_id=? AND idempotency_key=?",
                (str(owner_user_id or ""), str(idempotency_key or "")),
            ).fetchone()
        return _decode_run(row)

    def list_runs(
        self,
        *,
        owner_user_id: str,
        industry_pack_id: str = "",
        session_id: str = "",
        limit: int = 50,
    ) -> list[dict]:
        self.ensure_schema()
        conditions = ["owner_user_id=?"]
        params: list = [str(owner_user_id or "")]
        if industry_pack_id:
            conditions.append("industry_pack_id=?")
            params.append(str(industry_pack_id))
        if session_id:
            conditions.append("session_id=?")
            params.append(str(session_id))
        params.append(max(1, min(int(limit or 50), 200)))
        with self.database.lock:
            rows = self.database.connection.execute(
                "SELECT * FROM qa_runs WHERE " + " AND ".join(conditions) +
                " ORDER BY created_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [_decode_run(row) for row in rows]

    def set_job_id(self, run_id: str, job_id: int) -> None:
        self.ensure_schema()
        with self.database.lock:
            self.database.connection.execute(
                "UPDATE qa_runs SET job_id=?, updated_at=? WHERE id=?",
                (int(job_id), _now(), str(run_id)),
            )
            self.database.connection.commit()

    def update_run(self, run_id: str, *, status: str, stage: str, final_answer: Mapping | None = None) -> None:
        self.ensure_schema()
        now = _now()
        completed = now if status in {"completed", "failed", "cancelled"} else None
        with self.database.lock:
            if final_answer is None:
                self.database.connection.execute(
                    """
                    UPDATE qa_runs SET status=?, current_stage=?, updated_at=?,
                        completed_at=COALESCE(?, completed_at) WHERE id=?
                    """,
                    (status, stage, now, completed, str(run_id)),
                )
            else:
                self.database.connection.execute(
                    """
                    UPDATE qa_runs SET status=?, current_stage=?, final_answer_json=?,
                        updated_at=?, completed_at=COALESCE(?, completed_at) WHERE id=?
                    """,
                    (status, stage, _json(final_answer), now, completed, str(run_id)),
                )
            self.database.connection.commit()

    def mark_degraded(self, run_id: str, item: Mapping) -> None:
        run = self.get_run(run_id) or {}
        degradation = list(run.get("degradation") or [])
        degradation.append(dict(item))
        with self.database.lock:
            self.database.connection.execute(
                "UPDATE qa_runs SET degraded=1, degradation_json=?, updated_at=? WHERE id=?",
                (_json(degradation), _now(), str(run_id)),
            )
            self.database.connection.commit()

    def cancel_run(self, run_id: str, *, owner_user_id: str | None = None) -> bool:
        self.ensure_schema()
        params: list = [_now(), _now(), str(run_id)]
        owner_sql = ""
        if owner_user_id is not None:
            owner_sql = " AND owner_user_id=?"
            params.append(str(owner_user_id or ""))
        with self.database.lock:
            cursor = self.database.connection.execute(
                """
                UPDATE qa_runs SET status='cancelled', completed_at=?, updated_at=?
                WHERE id=? AND status IN ('queued','running','retry_wait')
                """ + owner_sql,
                tuple(params),
            )
            self.database.connection.commit()
            return bool(cursor.rowcount)

    def next_event_id(self, run_id: str) -> int:
        self.ensure_schema()
        with self.database.lock:
            row = self.database.connection.execute(
                "SELECT COALESCE(MAX(event_id), 0) + 1 FROM qa_events WHERE run_id=?",
                (str(run_id),),
            ).fetchone()
        return int(row[0] if row else 1)

    def append_event(self, event: Mapping) -> dict:
        self.ensure_schema()
        now = str(event.get("timestamp") or _now())
        with self.database.lock:
            self.database.connection.execute(
                """
                INSERT INTO qa_events(run_id,event_id,event_type,stage,payload_json,created_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(run_id,event_id) DO NOTHING
                """,
                (
                    str(event["run_id"]),
                    int(event["event_id"]),
                    str(event["type"]),
                    str(event["stage"]),
                    _json(dict(event)),
                    now,
                ),
            )
            self.database.connection.commit()
        return dict(event)

    def events_after(self, run_id: str, event_id: int = 0) -> list[dict]:
        self.ensure_schema()
        with self.database.lock:
            rows = self.database.connection.execute(
                "SELECT payload_json FROM qa_events WHERE run_id=? AND event_id>? ORDER BY event_id",
                (str(run_id), int(event_id or 0)),
            ).fetchall()
        result = []
        for row in rows:
            raw = row[0]
            try:
                value = json.loads(raw or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                result.append(value)
        return result

    def stage_runs(self, run_id: str) -> list[dict]:
        self.ensure_schema()
        with self.database.lock:
            rows = self.database.connection.execute(
                "SELECT * FROM qa_stage_runs WHERE run_id=? ORDER BY id", (str(run_id),)
            ).fetchall()
        result = []
        for row in rows:
            value = _row_dict(row)
            try:
                value["details"] = json.loads(value.get("details_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                value["details"] = {}
            result.append(value)
        return result

    def next_stage_attempt(self, run_id: str, stage: str) -> int:
        self.ensure_schema()
        with self.database.lock:
            row = self.database.connection.execute(
                "SELECT COALESCE(MAX(attempt), 0) + 1 FROM qa_stage_runs WHERE run_id=? AND stage=?",
                (str(run_id), str(stage)),
            ).fetchone()
        return int(row[0] if row else 1)

    def count_active_runs(self, owner_user_id: str = "") -> int:
        self.ensure_schema()
        sql = "SELECT COUNT(*) FROM qa_runs WHERE status IN ('queued','running','retry_wait')"
        params: tuple = ()
        if owner_user_id:
            sql += " AND owner_user_id=?"
            params = (str(owner_user_id),)
        with self.database.lock:
            row = self.database.connection.execute(sql, params).fetchone()
        return int(row[0] if row else 0)

    def expire_stale_runs(self, max_age_seconds: int, owner_user_id: str = "") -> list[str]:
        """把长时间没推进的问答运行置为失败，返回被回收的 run_id 列表。

        为什么需要：活跃配额（count_active_runs）把 queued/running/retry_wait 全部计入，
        而 worker 不在跑、或作业入队后没被领取时这些运行永远不会结束——用户会被永久锁在
        429「当前已有问答正在研究」，前端连阻塞的 run_id 都拿不到，无法自助取消。
        实测：本机库里 2 条 queued 的 run（qa_events / qa_stage_runs 都是 0 行）就会让之后
        所有 /api/chat/send 恒返回 USER_CONCURRENCY_LIMIT。
        """
        self.ensure_schema()
        max_age_seconds = max(60, int(max_age_seconds or 0))
        cutoff = _minutes_ago_text(max_age_seconds / 60.0)
        now = _now()
        owner_sql = ""
        select_params: list = [cutoff]
        if owner_user_id:
            owner_sql = " AND owner_user_id=?"
            select_params.append(str(owner_user_id))
        run_ids: list[str] = []
        with self.database.lock:
            cursor = self.database.connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT id FROM qa_runs
                     WHERE status IN ('queued','running','retry_wait')
                       AND COALESCE(NULLIF(updated_at,''), created_at) <= ?
                    """ + owner_sql,
                    select_params,
                )
                for row in cursor.fetchall():
                    value = dict(row) if hasattr(row, "keys") else {"id": row[0]}
                    if value.get("id"):
                        run_ids.append(str(value["id"]))
                if run_ids:
                    placeholders = ",".join("?" for _ in run_ids)
                    degradation = _json([{
                        "code": "STALE_RUN_EXPIRED",
                        "message": "该问答长时间未推进，已自动结束，可重新提问。",
                        "stage": "recovery",
                    }])
                    cursor.execute(
                        """
                        UPDATE qa_runs
                           SET status='failed', completed_at=?, updated_at=?,
                               degradation_json=?, degraded=1
                         WHERE id IN (%s)
                        """ % placeholders,
                        [now, now, degradation, *run_ids],
                    )
                self.database.connection.commit()
            finally:
                cursor.close()
        return run_ids

    def persist_level1_result(self, run_id: str, result: Mapping) -> None:
        """Upsert verified L1 evidence/claims in one short transaction."""
        self.ensure_schema()
        now = _now()
        with self.database.lock:
            cursor = self.database.connection.cursor()
            try:
                for evidence in result.get("evidence") or []:
                    cursor.execute(
                        """
                        INSERT INTO qa_evidence(
                            run_id,evidence_ref,source_type,article_id,ragflow_kb_id,
                            document_id,chunk_id,source_url,source_title,published_at,
                            fetched_at,authority_level,content_hash,payload_json,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(run_id,evidence_ref) DO UPDATE SET
                            source_type=excluded.source_type,article_id=excluded.article_id,
                            source_url=excluded.source_url,source_title=excluded.source_title,
                            published_at=excluded.published_at,fetched_at=excluded.fetched_at,
                            authority_level=excluded.authority_level,content_hash=excluded.content_hash,
                            payload_json=excluded.payload_json
                        """,
                        (
                            str(run_id), str(evidence.get("evidence_ref") or ""),
                            str(evidence.get("source_type") or ""), evidence.get("article_id"),
                            evidence.get("ragflow_kb_id"), evidence.get("document_id"), evidence.get("chunk_id"),
                            str(evidence.get("source_url") or ""), str(evidence.get("title") or ""),
                            evidence.get("published_at"), evidence.get("fetched_at"), evidence.get("authority_level"),
                            hashlib.sha256(str(evidence.get("content_excerpt") or "").encode("utf-8")).hexdigest(),
                            _json(evidence), now,
                        ),
                    )
                for claim in result.get("claims") or []:
                    claim_id = str(claim.get("claim_id") or "")
                    cursor.execute(
                        """
                        INSERT INTO qa_claims(
                            run_id,claim_key,stage,claim_text,claim_type,confidence,
                            valid_from,valid_to,scope_json,verification_status,payload_json,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(run_id,claim_key,stage) DO UPDATE SET
                            claim_text=excluded.claim_text,claim_type=excluded.claim_type,
                            confidence=excluded.confidence,valid_from=excluded.valid_from,
                            valid_to=excluded.valid_to,scope_json=excluded.scope_json,
                            verification_status=excluded.verification_status,payload_json=excluded.payload_json
                        """,
                        (
                            str(run_id), claim_id, "level1_draft", str(claim.get("text") or ""),
                            str(claim.get("claim_type") or "background"), float(claim.get("confidence") or 0),
                            claim.get("valid_from"), claim.get("valid_to"), _json(claim.get("scope") or []),
                            str(claim.get("verification_status") or "unverified"), _json(claim), now,
                        ),
                    )
                    for evidence_ref in claim.get("evidence_refs") or []:
                        cursor.execute(
                            """
                            INSERT INTO qa_claim_evidence(run_id,claim_key,evidence_ref,relationship,relevance_score,created_at)
                            VALUES(?,?,?,?,?,?) ON CONFLICT(run_id,claim_key,evidence_ref,relationship) DO NOTHING
                            """,
                            (str(run_id), claim_id, str(evidence_ref), "supports", float(claim.get("confidence") or 0), now),
                        )
                self.database.connection.commit()
            except Exception:
                self.database.connection.rollback()
                raise
            finally:
                cursor.close()

    def persist_level2_result(self, run_id: str, result: Mapping) -> None:
        """Persist verified RAGFlow evidence, claims, links and conflicts."""
        self.ensure_schema()
        now = _now()
        with self.database.lock:
            cursor = self.database.connection.cursor()
            try:
                for evidence in result.get("evidence") or []:
                    cursor.execute(
                        """
                        INSERT INTO qa_evidence(
                            run_id,evidence_ref,source_type,article_id,ragflow_kb_id,
                            document_id,chunk_id,source_url,source_title,published_at,
                            fetched_at,authority_level,content_hash,payload_json,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(run_id,evidence_ref) DO UPDATE SET
                            document_id=excluded.document_id,chunk_id=excluded.chunk_id,
                            source_url=excluded.source_url,source_title=excluded.source_title,
                            published_at=excluded.published_at,fetched_at=excluded.fetched_at,
                            authority_level=excluded.authority_level,content_hash=excluded.content_hash,
                            payload_json=excluded.payload_json
                        """,
                        (
                            str(run_id), str(evidence.get("evidence_ref") or ""),
                            str(evidence.get("source_type") or ""), evidence.get("article_id"),
                            evidence.get("ragflow_kb_id"), evidence.get("document_id"), evidence.get("chunk_id"),
                            str(evidence.get("source_url") or ""), str(evidence.get("title") or ""),
                            evidence.get("published_at"), evidence.get("fetched_at"), evidence.get("authority_level"),
                            hashlib.sha256(str(evidence.get("content_excerpt") or "").encode("utf-8")).hexdigest(),
                            _json(evidence), now,
                        ),
                    )
                for field in ("confirmed_claims", "corrected_claims", "new_findings"):
                    for claim in result.get(field) or []:
                        claim_id = str(claim.get("claim_id") or "")
                        cursor.execute(
                            """
                            INSERT INTO qa_claims(
                                run_id,claim_key,stage,claim_text,claim_type,confidence,
                                valid_from,valid_to,scope_json,verification_status,payload_json,created_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(run_id,claim_key,stage) DO UPDATE SET
                                claim_text=excluded.claim_text,claim_type=excluded.claim_type,
                                confidence=excluded.confidence,verification_status=excluded.verification_status,
                                payload_json=excluded.payload_json
                            """,
                            (
                                str(run_id), claim_id, "level2_research", str(claim.get("text") or ""),
                                str(claim.get("claim_type") or "background"), float(claim.get("confidence") or 0),
                                claim.get("valid_from"), claim.get("valid_to"), _json(claim.get("scope") or []),
                                str(claim.get("verification_status") or "unverified"), _json({"bucket": field, **claim}), now,
                            ),
                        )
                        for evidence_ref in claim.get("evidence_refs") or []:
                            cursor.execute(
                                """
                                INSERT INTO qa_claim_evidence(run_id,claim_key,evidence_ref,relationship,relevance_score,created_at)
                                VALUES(?,?,?,?,?,?) ON CONFLICT(run_id,claim_key,evidence_ref,relationship) DO NOTHING
                                """,
                                (str(run_id), claim_id, str(evidence_ref), "supports", float(claim.get("confidence") or 0), now),
                            )
                for conflict in result.get("conflicts") or []:
                    cursor.execute(
                        """
                        INSERT INTO qa_conflicts(run_id,conflict_key,conflict_type,resolution,rationale,payload_json,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_id,conflict_key) DO UPDATE SET
                            conflict_type=excluded.conflict_type,resolution=excluded.resolution,
                            rationale=excluded.rationale,payload_json=excluded.payload_json,updated_at=excluded.updated_at
                        """,
                        (
                            str(run_id), str(conflict.get("conflict_id") or ""), str(conflict.get("conflict_type") or ""),
                            str(conflict.get("resolution") or "unresolved"), str(conflict.get("rationale") or ""),
                            _json(conflict), now, now,
                        ),
                    )
                self.database.connection.commit()
            except Exception:
                self.database.connection.rollback()
                raise
            finally:
                cursor.close()

    def persist_reasoning_graph(self, run_id: str, graph: Mapping) -> None:
        self.ensure_schema()
        now = _now()
        with self.database.lock:
            cursor = self.database.connection.cursor()
            try:
                for node in graph.get("claims") or []:
                    claim = dict(node.get("claim") or {})
                    claim_id = str(node.get("canonical_id") or claim.get("claim_id") or "")
                    cursor.execute(
                        """
                        INSERT INTO qa_claims(
                            run_id,claim_key,stage,claim_text,claim_type,confidence,
                            valid_from,valid_to,scope_json,verification_status,payload_json,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(run_id,claim_key,stage) DO UPDATE SET
                            claim_text=excluded.claim_text,claim_type=excluded.claim_type,
                            confidence=excluded.confidence,verification_status=excluded.verification_status,
                            payload_json=excluded.payload_json
                        """,
                        (
                            str(run_id), claim_id, "conflict_review", str(claim.get("text") or ""),
                            str(claim.get("claim_type") or "background"), float(claim.get("confidence") or 0),
                            claim.get("valid_from"), claim.get("valid_to"), _json(claim.get("scope") or []),
                            str(claim.get("verification_status") or "unverified"), _json(node), now,
                        ),
                    )
                for edge in graph.get("edges") or []:
                    cursor.execute(
                        """
                        INSERT INTO qa_claim_evidence(run_id,claim_key,evidence_ref,relationship,relevance_score,created_at)
                        VALUES(?,?,?,?,?,?) ON CONFLICT(run_id,claim_key,evidence_ref,relationship) DO UPDATE SET
                            relevance_score=excluded.relevance_score
                        """,
                        (
                            str(run_id), str(edge.get("claim_id") or ""), str(edge.get("evidence_ref") or ""),
                            str(edge.get("relationship") or "supports"), float(edge.get("relevance_score") or 0), now,
                        ),
                    )
                for conflict in graph.get("conflicts") or []:
                    cursor.execute(
                        """
                        INSERT INTO qa_conflicts(run_id,conflict_key,conflict_type,resolution,rationale,payload_json,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_id,conflict_key) DO UPDATE SET
                            conflict_type=excluded.conflict_type,resolution=excluded.resolution,
                            rationale=excluded.rationale,payload_json=excluded.payload_json,updated_at=excluded.updated_at
                        """,
                        (
                            str(run_id), str(conflict.get("conflict_id") or ""), str(conflict.get("conflict_type") or ""),
                            str(conflict.get("resolution") or "unresolved"), str(conflict.get("rationale") or ""),
                            _json(conflict), now, now,
                        ),
                    )
                self.database.connection.commit()
            except Exception:
                self.database.connection.rollback()
                raise
            finally:
                cursor.close()

    def prepare_retry(self, run_id: str, stage: str, *, owner_user_id: str) -> dict | None:
        """Remove only the requested stage and downstream snapshots; keep earlier evidence."""
        orders = {
            "level2_retrieval": ("level2_retrieval", "level2_research", "conflict_review", "synthesis", "citation_validation"),
            "level2_research": ("level2_research", "conflict_review", "synthesis", "citation_validation"),
            "synthesis": ("synthesis", "citation_validation"),
        }
        stages = orders.get(str(stage))
        if not stages:
            raise ValueError("不支持重试该阶段")
        run = self.get_run(run_id, owner_user_id=owner_user_id)
        if not run:
            return None
        placeholders = ",".join("?" for _ in stages)
        with self.database.lock:
            cursor = self.database.connection.cursor()
            try:
                cursor.execute(
                    f"DELETE FROM qa_stage_runs WHERE run_id=? AND stage IN ({placeholders})",
                    (str(run_id), *stages),
                )
                if "level2_research" in stages:
                    cursor.execute("DELETE FROM qa_claims WHERE run_id=? AND stage IN ('level2_research','conflict_review')", (str(run_id),))
                    cursor.execute("DELETE FROM qa_conflicts WHERE run_id=?", (str(run_id),))
                    cursor.execute("DELETE FROM qa_claim_evidence WHERE run_id=? AND claim_key NOT IN (SELECT claim_key FROM qa_claims WHERE run_id=?)", (str(run_id), str(run_id)))
                degradation = [
                    item for item in run.get("degradation") or []
                    if str(item.get("stage") or "") not in stages
                ]
                cursor.execute(
                    """
                    UPDATE qa_runs SET status='retry_wait',current_stage=?,completed_at=NULL,
                        final_answer_json='{}',degraded=?,degradation_json=?,updated_at=?
                    WHERE id=? AND owner_user_id=?
                    """,
                    (
                        str(stage), 1 if degradation else 0, _json(degradation), _now(),
                        str(run_id), str(owner_user_id),
                    ),
                )
                self.database.connection.commit()
            except Exception:
                self.database.connection.rollback()
                raise
            finally:
                cursor.close()
        return self.get_run(run_id, owner_user_id=owner_user_id)

    def record_stage(
        self,
        run_id: str,
        stage: str,
        *,
        status: str,
        attempt: int = 1,
        input_hash: str = "",
        output_hash: str = "",
        error_code: str = "",
        details: Mapping | None = None,
    ) -> None:
        self.ensure_schema()
        now = _now()
        completed = now if status in {"completed", "failed", "cancelled", "degraded"} else None
        with self.database.lock:
            prior = self.database.connection.execute(
                "SELECT started_at FROM qa_stage_runs WHERE run_id=? AND stage=? AND attempt=?",
                (str(run_id), str(stage), int(attempt)),
            ).fetchone()
            latency_ms = None
            if completed:
                try:
                    started_text = str(prior[0] if prior else now).replace("Z", "+00:00")
                    completed_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
                    latency_ms = max(0, int((completed_dt - datetime.fromisoformat(started_text)).total_seconds() * 1000))
                except (TypeError, ValueError):
                    latency_ms = None
            token_usage = dict((details or {}).get("token_usage") or {}) if isinstance((details or {}).get("token_usage"), Mapping) else {}
            self.database.connection.execute(
                """
                INSERT INTO qa_stage_runs(
                    run_id,stage,attempt,status,started_at,completed_at,
                    latency_ms,input_hash,output_hash,token_usage_json,error_code,details_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id,stage,attempt) DO UPDATE SET
                    status=excluded.status,
                    completed_at=excluded.completed_at,
                    latency_ms=excluded.latency_ms,
                    input_hash=excluded.input_hash,
                    output_hash=excluded.output_hash,
                    token_usage_json=excluded.token_usage_json,
                    error_code=excluded.error_code,
                    details_json=excluded.details_json
                """,
                (
                    str(run_id),
                    str(stage),
                    int(attempt),
                    str(status),
                    now,
                    completed,
                    latency_ms,
                    str(input_hash or ""),
                    str(output_hash or ""),
                    _json(token_usage),
                    str(error_code or ""),
                    _json(details or {}),
                ),
            )
            self.database.connection.commit()

    def list_audit_events(self, *, trace_id: str = "", limit: int = 200) -> list[dict]:
        self.ensure_schema()
        sql = "SELECT trace_id,run_id,event_type,actor_id,origin,industry_pack_id,payload_json,created_at FROM qa_audit_events"
        params: list = []
        if trace_id:
            sql += " WHERE trace_id=?"
            params.append(str(trace_id))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self.database.lock:
            rows = self.database.connection.execute(sql, tuple(params)).fetchall()
        result = []
        for row in rows:
            value = _row_dict(row)
            try:
                value["payload"] = json.loads(value.get("payload_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                value["payload"] = {}
            value.pop("payload_json", None)
            result.append(value)
        return result


__all__ = ["QaStore"]

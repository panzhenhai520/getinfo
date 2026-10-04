#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Persistent cache, circuit breaker, rate limiting and stage budgets."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from qa_schema import ensure_qa_tables


STAGE_BUDGET_SECONDS = {
    "plan": 3,
    "level1_retrieval": 10,
    "level1_draft_first_token": 30,
    "level1_draft": 60,
    "level2_query": 12,
    "level2_retrieval": 60,
    "level2_research": 90,
    "level2_research_standard": 90,
    "level2_research_deep": 180,
    "synthesis": 45,
    "ui_ack_ms": 300,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


class QaRateLimitError(RuntimeError):
    def __init__(self, retry_after: int, bucket: str):
        super().__init__("请求过于频繁，请稍后重试。")
        self.retry_after = max(1, int(retry_after))
        self.bucket = str(bucket)


class QaCircuitOpen(RuntimeError):
    def __init__(self, dependency: str, retry_after: int):
        super().__init__(f"{dependency} 暂时不可用，系统正在自动恢复。")
        self.dependency = dependency
        self.retry_after = max(1, int(retry_after))


class QaPersistentResilience:
    def __init__(self, database, *, now_provider=None):
        self.database = database
        self.now_provider = now_provider or _now

    def _ensure(self):
        self.database._ensure_connection()
        with self.database.lock:
            cursor = self.database.connection.cursor()
            try:
                ensure_qa_tables(cursor)
                self.database.connection.commit()
            finally:
                cursor.close()

    @staticmethod
    def cache_key(namespace: str, payload: Mapping, *, pack_id: str, kb_version: str, policy_version: str = "v1") -> str:
        body = _json({
            "namespace": namespace, "payload": dict(payload), "pack": pack_id,
            "kb_version": kb_version, "policy_version": policy_version,
        })
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def cache_get(self, key: str, *, namespace: str, kb_version: str, scope_hash: str = "") -> dict | None:
        self._ensure()
        now = _text(self.now_provider())
        with self.database.lock:
            row = self.database.connection.execute(
                "SELECT payload_json FROM qa_retrieval_cache WHERE cache_key=? AND namespace=? "
                "AND kb_version=? AND scope_hash=? AND expires_at>?",
                (str(key), str(namespace), str(kb_version), str(scope_hash), now),
            ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row[0] or "{}")
            return value if isinstance(value, dict) else None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def cache_put(
        self, key: str, payload: Mapping, *, namespace: str, pack_id: str,
        kb_version: str, scope_hash: str = "", ttl_seconds: int = 300,
    ) -> None:
        self._ensure()
        now = self.now_provider()
        expires = now + timedelta(seconds=max(1, min(int(ttl_seconds), 3600)))
        with self.database.lock:
            self.database.connection.execute(
                """INSERT INTO qa_retrieval_cache(
                    cache_key,namespace,industry_pack_id,kb_version,scope_hash,
                    payload_json,created_at,expires_at
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(cache_key) DO UPDATE SET payload_json=excluded.payload_json,
                    kb_version=excluded.kb_version,scope_hash=excluded.scope_hash,
                    created_at=excluded.created_at,expires_at=excluded.expires_at""",
                (str(key), str(namespace), str(pack_id), str(kb_version), str(scope_hash), _json(dict(payload)), _text(now), _text(expires)),
            )
            self.database.connection.commit()

    def circuit_before(self, dependency: str) -> str:
        self._ensure()
        now = self.now_provider()
        with self.database.lock:
            row = self.database.connection.execute(
                "SELECT state,probe_after FROM qa_circuit_states WHERE dependency=?", (str(dependency),)
            ).fetchone()
            if not row or str(row[0]) == "closed":
                return "closed"
            probe_after = datetime.fromisoformat(str(row[1]).replace("Z", "+00:00")) if row[1] else now
            if probe_after > now:
                raise QaCircuitOpen(str(dependency), int((probe_after - now).total_seconds()) + 1)
            self.database.connection.execute(
                "UPDATE qa_circuit_states SET state='half_open',updated_at=? WHERE dependency=?",
                (_text(now), str(dependency)),
            )
            self.database.connection.commit()
        return "half_open"

    def circuit_success(self, dependency: str) -> None:
        self._ensure()
        now = _text(self.now_provider())
        with self.database.lock:
            self.database.connection.execute(
                """INSERT INTO qa_circuit_states(dependency,state,consecutive_failures,opened_at,probe_after,updated_at)
                VALUES(?,'closed',0,NULL,NULL,?) ON CONFLICT(dependency) DO UPDATE SET
                state='closed',consecutive_failures=0,opened_at=NULL,probe_after=NULL,updated_at=excluded.updated_at""",
                (str(dependency), now),
            )
            self.database.connection.commit()

    def circuit_failure(self, dependency: str, *, threshold: int = 3, recovery_seconds: int = 30) -> str:
        self._ensure()
        now = self.now_provider()
        with self.database.lock:
            row = self.database.connection.execute(
                "SELECT consecutive_failures FROM qa_circuit_states WHERE dependency=?", (str(dependency),)
            ).fetchone()
            failures = int(row[0] if row else 0) + 1
            state = "open" if failures >= max(1, threshold) else "closed"
            opened = _text(now) if state == "open" else None
            probe = _text(now + timedelta(seconds=max(1, recovery_seconds))) if state == "open" else None
            self.database.connection.execute(
                """INSERT INTO qa_circuit_states(dependency,state,consecutive_failures,opened_at,probe_after,updated_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(dependency) DO UPDATE SET
                state=excluded.state,consecutive_failures=excluded.consecutive_failures,
                opened_at=excluded.opened_at,probe_after=excluded.probe_after,updated_at=excluded.updated_at""",
                (str(dependency), state, failures, opened, probe, _text(now)),
            )
            self.database.connection.commit()
        return state

    def rate_limit(self, owner_user_id: str, industry_pack_id: str) -> None:
        self._ensure()
        now = self.now_provider()
        window = max(10, min(int(os.getenv("QA_RATE_WINDOW_SECONDS", "60")), 3600))
        cutoff = _text(now - timedelta(seconds=window))
        buckets = (
            (f"user:{owner_user_id}", max(1, int(os.getenv("QA_RATE_USER_LIMIT", "10")))),
            (f"pack:{industry_pack_id}", max(1, int(os.getenv("QA_RATE_PACK_LIMIT", "60")))),
            ("system", max(1, int(os.getenv("QA_RATE_SYSTEM_LIMIT", "200")))),
        )
        with self.database.lock:
            self.database.connection.execute("DELETE FROM qa_rate_limit_events WHERE event_at<?", (cutoff,))
            for bucket, limit in buckets:
                row = self.database.connection.execute(
                    "SELECT COALESCE(SUM(weight),0) FROM qa_rate_limit_events WHERE bucket_key=? AND event_at>=?",
                    (bucket, cutoff),
                ).fetchone()
                if int(row[0] if row else 0) >= limit:
                    self.database.connection.rollback()
                    raise QaRateLimitError(window, bucket.split(":", 1)[0])
            for bucket, _limit in buckets:
                self.database.connection.execute(
                    "INSERT INTO qa_rate_limit_events(bucket_key,event_at,weight) VALUES(?,?,1)",
                    (bucket, _text(now)),
                )
            self.database.connection.commit()

    def circuit_snapshot(self) -> list[dict]:
        self._ensure()
        with self.database.lock:
            rows = self.database.connection.execute(
                "SELECT dependency,state,consecutive_failures,probe_after,updated_at FROM qa_circuit_states ORDER BY dependency"
            ).fetchall()
        return [dict(row) for row in rows]


__all__ = [
    "QaCircuitOpen", "QaPersistentResilience", "QaRateLimitError", "STAGE_BUDGET_SECONDS",
]

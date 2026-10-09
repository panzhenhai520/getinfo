#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Health, audit and bounded-cardinality metrics for unified QA."""

from __future__ import annotations

import json
import math
import os
import socket
import time
from datetime import datetime, timezone
from typing import Mapping
from urllib.parse import urlsplit

import requests

from qa_policy import QaPolicyResolver
from qa_provider_registry import QaProviderRegistry
from qa_ragflow_client import QaRagflowResearchClient
from qa_schema import ensure_qa_tables
from qa_security import redact_sensitive, sanitize_log_text, validate_outbound_url, validate_proxy_url


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _percentile(values: list[float], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return int(ordered[index])


class QaAuditLogger:
    def __init__(self, database):
        self.database = database

    def record(
        self, event_type: str, *, trace_id: str = "", run_id: str = "",
        actor_id: str = "", origin: str = "", industry_pack_id: str = "",
        payload: Mapping | None = None,
    ) -> None:
        self.database._ensure_connection()
        with self.database.lock:
            cursor = self.database.connection.cursor()
            ensure_qa_tables(cursor)
            cursor.execute(
                """INSERT INTO qa_audit_events(
                    trace_id,run_id,event_type,actor_id,origin,industry_pack_id,payload_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    str(trace_id or ""), str(run_id or ""), str(event_type or "unknown")[:80],
                    str(actor_id or "")[:200], str(origin or "")[:30], str(industry_pack_id or "")[:80],
                    json.dumps(redact_sensitive(dict(payload or {})), ensure_ascii=False, sort_keys=True), _now(),
                ),
            )
            self.database.connection.commit()
            cursor.close()


class QaMetricsService:
    def __init__(self, database):
        self.database = database

    def snapshot(self, *, industry_pack_id: str = "", limit: int = 2000) -> dict:
        self.database._ensure_connection()
        with self.database.lock:
            cursor = self.database.connection.cursor()
            ensure_qa_tables(cursor)
            conditions, params = [], []
            if industry_pack_id:
                conditions.append("industry_pack_id=?")
                params.append(str(industry_pack_id))
            sql = "SELECT id,origin,status,degraded,created_at,completed_at FROM qa_runs"
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(max(1, min(int(limit), 10000)))
            runs = [dict(row) for row in cursor.execute(sql, tuple(params)).fetchall()]
            run_ids = [row["id"] for row in runs]
            stages = []
            if run_ids:
                placeholders = ",".join("?" for _ in run_ids)
                stages = [dict(row) for row in cursor.execute(
                    f"SELECT run_id,stage,status,latency_ms,error_code,details_json FROM qa_stage_runs WHERE run_id IN ({placeholders})",
                    tuple(run_ids),
                ).fetchall()]
            cursor.close()
        by_origin, by_status, stage_stats, error_codes = {}, {}, {}, {}
        for run in runs:
            origin = str(run.get("origin") or "unknown")
            status = str(run.get("status") or "unknown")
            by_origin[origin] = by_origin.get(origin, 0) + 1
            by_status[status] = by_status.get(status, 0) + 1
        for stage in stages:
            name = str(stage.get("stage") or "unknown")
            bucket = stage_stats.setdefault(name, {"count": 0, "latencies": [], "failures": 0})
            bucket["count"] += 1
            if stage.get("latency_ms") is not None:
                bucket["latencies"].append(float(stage["latency_ms"]))
            if stage.get("status") == "failed":
                bucket["failures"] += 1
            code = str(stage.get("error_code") or "")
            if code:
                error_codes[code] = error_codes.get(code, 0) + 1
        normalized = {
            name: {
                "count": item["count"], "failures": item["failures"],
                "p50_ms": _percentile(item["latencies"], 0.50),
                "p95_ms": _percentile(item["latencies"], 0.95),
            }
            for name, item in stage_stats.items()
        }
        evidence_count = sum(
            int((json.loads(row.get("details_json") or "{}").get("result") or {}).get("stats", {}).get("adopted") or 0)
            for row in stages if row.get("stage") in {"level1_retrieval", "level2_retrieval"}
        )
        return {
            "runs": len(runs), "by_origin": by_origin, "by_status": by_status,
            "degraded": sum(1 for row in runs if bool(row.get("degraded"))),
            "stages": normalized, "error_codes": error_codes,
            "retrieval_evidence_adopted": evidence_count,
        }


_ALLOWED_HOSTS_CACHE = None  # (环境指纹, monotonic 时间, 主机集合) —— 带 TTL 的进程内缓存
_ALLOWED_HOSTS_TTL_SECONDS = 60.0


def _allowlist_env_fingerprint() -> tuple:
    """白名单相关的环境指纹：指纹一变就让缓存失效。

    只读环境变量、不触发任何导入，所以既便宜又**不会读到过期白名单**
    （单靠 TTL 会让"管理员刚改完 .env"的 60 秒内继续用旧值；测试也抓到了这一点）。
    """
    prefixed = tuple(sorted(
        (key, os.environ[key]) for key in os.environ
        if key.startswith("QA_LLM_BASE_URL_") or key.startswith("QA_EMBEDDING_BASE_URL_")
    ))
    return (
        os.getenv("INTEL_LLM_BASE_URL", ""),
        os.getenv("INTEL_EMBEDDING_BASE_URL", ""),
        os.getenv("RAGFLOW_BASE_URL", ""),
        os.getenv("QA_RAGFLOW_BASE_URL", ""),
        os.getenv("QA_LLM_BASE_URL", ""),
        os.getenv("QA_ALLOWED_PROVIDER_HOSTS", ""),
        prefixed,
    )


def provider_allowed_hosts(force: bool = False) -> set[str]:
    """出站白名单（**带缓存 + 环境指纹失效**，阶段 6 提速）。

    为什么必须缓存：这个函数在**每次** level1_draft / synthesis 的模型调用前都会被调用，
    而它内部 `from chat_api import MODEL_META` 会拉起整个 Flask 应用配置——实测冷调用
    三秒以上，热调用也要重新构建一遍集合。白名单只随配置变化，缓存 60 秒 + 指纹失效足够安全。

    返回集合的**副本**，避免调用方就地修改污染缓存。
    """
    global _ALLOWED_HOSTS_CACHE

    now = time.monotonic()
    fingerprint = _allowlist_env_fingerprint()
    cached = _ALLOWED_HOSTS_CACHE
    if (not force and cached and cached[0] == fingerprint
            and (now - cached[1]) < _ALLOWED_HOSTS_TTL_SECONDS):
        return set(cached[2])
    computed = _compute_provider_allowed_hosts()
    _ALLOWED_HOSTS_CACHE = (fingerprint, now, set(computed))
    return set(computed)


def _compute_provider_allowed_hosts() -> set[str]:
    """重建白名单（缓存未命中时才会走到这里）。"""
    result = set()
    try:
        from chat_api import MODEL_META

        for item in MODEL_META.values():
            host = urlsplit(str(item.get("base_url") or "")).hostname
            if host:
                result.add(host.casefold())
    except Exception:
        pass

    def _add(raw: str) -> None:
        try:
            host = urlsplit(str(raw or "").strip()).hostname
        except Exception:
            host = ""
        if host:
            result.add(host.casefold())

    # 管理员在 .env 里配置的模型/知识库端点：单值项 + 按前缀的多值项（QA_LLM_BASE_URL_*）
    for key in ("INTEL_LLM_BASE_URL", "INTEL_EMBEDDING_BASE_URL", "RAGFLOW_BASE_URL",
                "QA_RAGFLOW_BASE_URL", "QA_LLM_BASE_URL"):
        _add(os.getenv(key, ""))
    for key, value in os.environ.items():
        if key.startswith("QA_LLM_BASE_URL_") or key.startswith("QA_EMBEDDING_BASE_URL_"):
            _add(value)

    result.update(item.strip().casefold() for item in os.getenv("QA_ALLOWED_PROVIDER_HOSTS", "").split(",") if item.strip())
    return result


def probe_provider(profile, *, session=None, timeout: float = 8.0) -> dict:
    session = session or requests.Session()
    started = time.monotonic()
    try:
        base = validate_outbound_url(
            profile.base_url,
            allowed_hosts=provider_allowed_hosts(),
            allow_private_for_allowlist=True,
        )
        headers = {"Authorization": f"Bearer {profile.api_key}"} if profile.api_key else {}
        response = session.get(base.rstrip("/") + "/models", headers=headers, timeout=timeout)
        response.raise_for_status()
        body = response.json()
        models = body.get("data") if isinstance(body, dict) else []
        return {
            "ready": True, "provider_id": profile.provider_id,
            "model_found": not models or any(str(item.get("id") or "") == profile.model_id for item in models if isinstance(item, Mapping)),
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    except Exception as exc:
        return {
            "ready": False, "provider_id": profile.provider_id,
            "error": sanitize_log_text(exc, secrets=(getattr(profile, "api_key", ""),)),
            "latency_ms": int((time.monotonic() - started) * 1000),
        }


def probe_proxy(proxy_url: str, *, connector=socket.create_connection, timeout: float = 3.0) -> dict:
    if not proxy_url:
        return {"configured": False, "ready": True, "mode": "direct"}
    try:
        safe = validate_proxy_url(proxy_url)
        parts = urlsplit(safe)
        sock = connector((parts.hostname, parts.port), timeout=timeout)
        try:
            sock.close()
        except Exception:
            pass
        return {"configured": True, "ready": True, "scheme": parts.scheme, "host": parts.hostname, "port": parts.port}
    except Exception as exc:
        return {"configured": True, "ready": False, "error": sanitize_log_text(exc)}


class QaHealthService:
    def __init__(self, database, *, provider_registry=None, policy_resolver=None, ragflow_factory=None):
        self.database = database
        self.provider_registry = provider_registry or QaProviderRegistry()
        self.policy_resolver = policy_resolver or QaPolicyResolver()
        self.ragflow_factory = ragflow_factory

    def _ragflow(self, policy):
        if self.ragflow_factory:
            return self.ragflow_factory(policy)
        import config

        return QaRagflowResearchClient(
            base_url=config.RAGFLOW_BASE_URL, api_key=config.RAGFLOW_API_KEY,
            app_id=policy.ragflow_app_id, kb_id=policy.ragflow_kb_id,
            timeout_seconds=min(15, policy.research_timeout_seconds), retries=0,
            proxies=config.get_ragflow_proxies() if hasattr(config, "get_ragflow_proxies") else None,
        )

    def check(self, *, owner_user_id: str, industry_pack_id: str, live: bool = False) -> dict:
        policy = self.policy_resolver.resolve(industry_pack_id)
        providers = []
        for provider_id in ("local", "deepseek", "gemini", "chatgpt", "claude", "openrouter"):
            try:
                profile = self.provider_registry.resolve(
                    "draft", provider_id, owner_user_id=owner_user_id, industry_pack_id=industry_pack_id,
                )
                item = {
                    "provider_id": profile.provider_id, "name": profile.name,
                    "configured": bool(profile.base_url and profile.model_id and (profile.api_key or profile.provider_id == "local")),
                    "has_key": bool(profile.api_key), "use_proxy": bool(profile.use_proxy),
                }
                if live and item["configured"]:
                    item.update(probe_provider(profile))
                else:
                    item["ready"] = item["configured"]
                providers.append(item)
            except Exception as exc:
                providers.append({"provider_id": provider_id, "configured": False, "ready": False, "error": sanitize_log_text(exc)})
        ragflow = {"configured": bool(policy.ragflow_app_id and policy.ragflow_kb_id), "ready": False}
        if live and ragflow["configured"]:
            client = self._ragflow(policy)
            ragflow.update(client.health_check())
            if ragflow.get("ready"):
                ragflow["dataset"] = client.dataset_status()
                ragflow["ready"] = bool(ragflow["dataset"].get("ready"))
        else:
            ragflow["ready"] = ragflow["configured"]
        ragflow["assistant_id"] = policy.ragflow_app_id
        ragflow["kb_id"] = policy.ragflow_kb_id
        drift = []
        if os.getenv("QA_EXPECTED_RAGFLOW_APP_ID") and policy.ragflow_app_id != os.getenv("QA_EXPECTED_RAGFLOW_APP_ID"):
            drift.append("ragflow_app_id")
        if os.getenv("QA_EXPECTED_RAGFLOW_KB_ID") and policy.ragflow_kb_id != os.getenv("QA_EXPECTED_RAGFLOW_KB_ID"):
            drift.append("ragflow_kb_id")
        result = {
            "ready": any(item.get("ready") for item in providers) and bool(ragflow.get("ready")),
            "checked_at": _now(), "live": bool(live), "providers": providers,
            "ragflow": ragflow, "config_drift": drift,
        }
        self._save_snapshot("unified_qa", "ready" if result["ready"] else "degraded", result)
        return redact_sensitive(result)

    def _save_snapshot(self, component: str, status: str, summary: Mapping) -> None:
        self.database._ensure_connection()
        with self.database.lock:
            cursor = self.database.connection.cursor()
            ensure_qa_tables(cursor)
            cursor.execute(
                "INSERT INTO qa_health_snapshots(component,status,summary_json,checked_at) VALUES(?,?,?,?)",
                (component, status, json.dumps(redact_sensitive(dict(summary)), ensure_ascii=False), _now()),
            )
            self.database.connection.commit()
            cursor.close()


__all__ = [
    "QaAuditLogger", "QaHealthService", "QaMetricsService", "probe_provider",
    "probe_proxy", "provider_allowed_hosts",
]

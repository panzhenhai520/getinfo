#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Authenticated HTTP gateway and testable service for unified QA v1."""

from __future__ import annotations

import os
import time
import uuid
from typing import Mapping

from flask import Blueprint, Response, jsonify, request, stream_with_context

from qa_bridge import (
    BRIDGE_SESSION_TTL_SECONDS,
    QaBridgeError,
    bridge_token_from_request,
    get_qa_bridge_manager,
    qa_access_required,
)
from qa_contracts import QA_CONTRACT_VERSION, QA_MODES, QaContractError, normalize_qa_request
from qa_flags import QaFeatureFlags
from qa_observability import QaAuditLogger, QaHealthService, QaMetricsService
from qa_policy import QaPolicyError, QaPolicyResolver
from qa_provider_registry import QaProviderRegistry
from qa_resilience import QaPersistentResilience, QaRateLimitError, STAGE_BUDGET_SECONDS
from qa_settings import QaSettingsError, QaSettingsService
from qa_sse import encode_qa_sse


qa_bp = Blueprint("unified_qa", __name__)
_service_override = None


class QaGatewayError(ValueError):
    def __init__(self, message: str, *, code: str = "INVALID_REQUEST", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def _integer_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


class QaGatewayService:
    def __init__(
        self, store, repository, *, policy_resolver=None, provider_registry=None,
        resilience=None, feature_flags=None, audit_logger=None,
    ):
        self.store = store
        self.repository = repository
        self.policy_resolver = policy_resolver or QaPolicyResolver()
        self.provider_registry = provider_registry or QaProviderRegistry()
        self.resilience = resilience
        self.feature_flags = feature_flags
        self.audit_logger = audit_logger

    def create_run(
        self,
        payload: Mapping,
        *,
        owner_user_id: str,
        authorized_pack_id: str = "",
        idempotency_key: str,
        trusted_origin: str = "getinfo_ui",
    ) -> tuple[dict, bool]:
        requested_pack = str(payload.get("industry_pack_id") or "").strip()
        authorized_pack = str(authorized_pack_id or "").strip()
        if authorized_pack and requested_pack and requested_pack != authorized_pack:
            raise QaGatewayError("无权访问该行业包", code="PACK_ACCESS_DENIED", status=403)
        effective = dict(payload)
        if authorized_pack:
            effective["industry_pack_id"] = authorized_pack
        normalized = normalize_qa_request(effective, trusted_origin=trusted_origin)
        if self.feature_flags is not None:
            decision = self.feature_flags.evaluate(
                owner_user_id=owner_user_id,
                industry_pack_id=normalized["industry_pack_id"],
                origin=trusted_origin,
            )
            if not decision.get("allowed"):
                raise QaGatewayError(
                    "统一问答当前未向此用户或入口开放，请联系管理员。",
                    code="UNIFIED_QA_DISABLED",
                    status=503,
                )
        if normalized["mode"] == "fast":
            from qa_planner import is_high_risk_policy_question

            if is_high_risk_policy_question(normalized["question"]):
                raise QaGatewayError(
                    "政策法规和税务问题必须执行标准或深度研究，不能使用快速模式。",
                    code="FAST_MODE_NOT_ALLOWED_FOR_POLICY",
                    status=400,
                )
        idem = str(idempotency_key or "").strip()
        if not idem or len(idem) > 200:
            raise QaGatewayError("缺少或无效的 Idempotency-Key", code="IDEMPOTENCY_KEY_REQUIRED")

        owner_limit = _integer_env("QA_MAX_ACTIVE_RUNS_PER_USER", 2, 1, 20)
        system_limit = _integer_env("QA_MAX_ACTIVE_RUNS_SYSTEM", 20, 1, 500)
        # Existing idempotent requests bypass capacity checks.
        existing = self.store.get_run_by_idempotency(owner_user_id, idem)
        if existing is None:
            if self.resilience is not None:
                try:
                    self.resilience.rate_limit(owner_user_id, normalized["industry_pack_id"])
                except QaRateLimitError as exc:
                    error = QaGatewayError(str(exc), code="RATE_LIMITED", status=429)
                    error.retry_after = exc.retry_after
                    raise error from exc
            if self.store.count_active_runs(owner_user_id) >= owner_limit:
                raise QaGatewayError("当前已有问答正在研究，请等待完成后再试。", code="USER_CONCURRENCY_LIMIT", status=429)
            if self.store.count_active_runs() >= system_limit:
                raise QaGatewayError("问答服务当前繁忙，请稍后重试。", code="SYSTEM_CONCURRENCY_LIMIT", status=503)

        # 二级检索（RAGFlow 知识库）的取用策略：
        #   ① 知识库没配（RAGFLOW_KB_ID / 包的 settings_json.ragflow_kb_id / RAGFLOW_LLM_APP_ID 都为空）
        #      → **不再拒绝这次问答**，照常建 run，level2_retrieval 会因 health_check 不 ready
        #      自动降级为"只用一级（平台文章库）证据"；
        #   ② 知识库配了但检索不到相关片段 → 由 qa_relevance 闸门整批丢弃二级证据，
        #      同样只用一级证据（见 qa_pipeline.level2_retrieval）；
        #   ③ 想彻底不挂知识库 → 设 UNIFIED_QA_LEVEL2_ENABLED=false（或 admin 热开关
        #      qa_feature_flags.level2_enabled），连检索都不会发起。
        if normalized["mode"] == "fast":
            policy = self.policy_resolver.resolve(normalized["industry_pack_id"])
        else:
            try:
                policy = self.policy_resolver.require_research_ready(normalized["industry_pack_id"])
            except QaPolicyError as exc:
                policy = self.policy_resolver.resolve(normalized["industry_pack_id"])
                print("ℹ️ 二级研究配置缺失（%s），本次问答降级为只用一级证据: %s"
                      % (exc, normalized["industry_pack_id"]))
        try:
            providers = self.provider_registry.resolve_run_roles(
                normalized["draft_provider"], policy, owner_user_id=owner_user_id
            )
        except TypeError:
            providers = self.provider_registry.resolve_run_roles(normalized["draft_provider"], policy)
        run = self.store.create_run(
            normalized,
            owner_user_id=owner_user_id,
            idempotency_key=idem,
            research_app_id=policy.ragflow_app_id,
            synthesis_provider_id=providers["synthesis"].provider_id,
        )
        created = bool(run.pop("_created", False))
        if created or not run.get("job_id"):
            job_id, _inserted = self.repository.enqueue_job(
                "qa.run",
                f"qa-run:{run['id']}",
                {"run_id": run["id"], "industry_pack_id": normalized["industry_pack_id"]},
                priority=10,
                max_attempts=3,
                request_id=run["id"],
                created_by=owner_user_id,
            )
            self.store.set_job_id(run["id"], job_id)
            run = self.store.get_run(run["id"], owner_user_id=owner_user_id)
        if self.audit_logger is not None:
            self.audit_logger.record(
                "run_created" if created else "run_idempotent_replay",
                trace_id=run["id"], run_id=run["id"], actor_id=owner_user_id,
                origin=trusted_origin, industry_pack_id=normalized["industry_pack_id"],
                payload={"mode": normalized["mode"], "draft_provider": normalized["draft_provider"]},
            )
        return run, created

    def get_run(self, run_id: str, *, owner_user_id: str) -> dict:
        run = self.store.get_run(run_id, owner_user_id=owner_user_id)
        if not run:
            raise QaGatewayError("问答运行不存在", code="RUN_NOT_FOUND", status=404)
        return run

    def list_runs(self, *, owner_user_id: str, authorized_pack_id: str = "", session_id: str = "", limit: int = 50) -> list[dict]:
        return self.store.list_runs(
            owner_user_id=owner_user_id,
            industry_pack_id=str(authorized_pack_id or ""),
            session_id=str(session_id or ""),
            limit=limit,
        )

    def events(self, run_id: str, *, owner_user_id: str, after: int = 0) -> list[dict]:
        self.get_run(run_id, owner_user_id=owner_user_id)
        return self.store.events_after(run_id, after)

    def cancel(self, run_id: str, *, owner_user_id: str) -> dict:
        run = self.get_run(run_id, owner_user_id=owner_user_id)
        changed = self.store.cancel_run(run_id, owner_user_id=owner_user_id)
        job_id = int(run.get("job_id") or 0)
        if job_id:
            self.repository.cancel_job(job_id, reason="user cancelled QA run")
        if self.audit_logger is not None:
            self.audit_logger.record(
                "run_cancelled", trace_id=run_id, run_id=run_id, actor_id=owner_user_id,
                origin=str(run.get("origin") or ""), industry_pack_id=str(run.get("industry_pack_id") or ""),
            )
        return self.store.get_run(run_id, owner_user_id=owner_user_id) | {"cancelled_now": changed}

    def retry(self, run_id: str, *, owner_user_id: str, stage: str, idempotency_key: str) -> dict:
        self.get_run(run_id, owner_user_id=owner_user_id)
        idem = str(idempotency_key or "").strip()
        if not idem or len(idem) > 200:
            raise QaGatewayError("重试请求缺少 Idempotency-Key", code="IDEMPOTENCY_KEY_REQUIRED")
        dedupe_key = f"qa-retry:{run_id}:{stage}:{idem}"
        existing_job = None
        if hasattr(self.repository, "get_job_by_dedupe_key"):
            existing_job = self.repository.get_job_by_dedupe_key(dedupe_key)
        if existing_job:
            return self.store.get_run(run_id, owner_user_id=owner_user_id)
        run = self.store.prepare_retry(run_id, stage, owner_user_id=owner_user_id)
        if not run:
            raise QaGatewayError("问答运行不存在", code="RUN_NOT_FOUND", status=404)
        job_id, _created = self.repository.enqueue_job(
            "qa.run", dedupe_key,
            {"run_id": run_id, "industry_pack_id": run.get("industry_pack_id"), "retry_stage": stage},
            priority=12, max_attempts=3, request_id=run_id, created_by=owner_user_id,
        )
        self.store.set_job_id(run_id, job_id)
        if self.audit_logger is not None:
            self.audit_logger.record(
                "stage_retry_requested", trace_id=run_id, run_id=run_id, actor_id=owner_user_id,
                origin=str(run.get("origin") or ""), industry_pack_id=str(run.get("industry_pack_id") or ""),
                payload={"stage": stage},
            )
        return self.store.get_run(run_id, owner_user_id=owner_user_id)


def set_qa_gateway_service(service) -> None:
    global _service_override
    _service_override = service


def get_qa_gateway_service() -> QaGatewayService:
    if _service_override is not None:
        return _service_override
    from intel_database import intel_repository
    from qa_storage import QaStore
    from sqlite_database import sqlite_db

    store = QaStore(sqlite_db)
    return QaGatewayService(
        store, intel_repository,
        resilience=QaPersistentResilience(sqlite_db),
        feature_flags=QaFeatureFlags(sqlite_db),
        audit_logger=QaAuditLogger(sqlite_db),
    )


def _identity() -> tuple[str, str]:
    current = getattr(request, "current_user", {}) or {}
    if str(current.get("role") or "") == "qa_bridge":
        return str(current.get("owner_user_id") or ""), str(current.get("pack_id") or "")
    if str(current.get("role") or "") == "pack_user":
        return f"pack:{int(current.get('pack_user_id') or 0)}", str(current.get("pack_id") or "")
    user_id = current.get("user_id") or current.get("id") or 0
    return f"user:{int(user_id or 0)}", ""


def _trusted_origin() -> str:
    current = getattr(request, "current_user", {}) or {}
    if str(current.get("role") or "") == "qa_bridge":
        return "ragflow_ui"
    requested = str(request.headers.get("X-QA-Origin") or "getinfo_ui").casefold()
    # Browser headers never grant the RAGFlow origin; only a verified bridge
    # session above may do so.
    return "api" if requested == "api" else "getinfo_ui"


def _public_run(run: Mapping) -> dict:
    allowed = {
        "id", "contract_version", "session_id", "industry_pack_id", "origin", "mode",
        "status", "current_stage", "draft_provider_id", "research_app_id",
        "synthesis_provider_id", "job_id", "degraded", "degradation", "final_answer",
        "created_at", "updated_at", "completed_at",
    }
    value = {key: run.get(key) for key in allowed}
    value["question"] = str((run.get("request") or {}).get("question") or run.get("question_text") or "")
    return value


def _error_response(exc):
    if isinstance(exc, QaBridgeError):
        return jsonify({"success": False, "error": {"code": exc.code, "message": str(exc)}}), exc.status
    if isinstance(exc, QaGatewayError):
        response = jsonify({"success": False, "error": {
            "code": exc.code, "message": str(exc),
            "retryable": exc.status in {429, 503},
            "retry_after": int(getattr(exc, "retry_after", 0) or 0),
        }})
        if getattr(exc, "retry_after", 0):
            response.headers["Retry-After"] = str(int(exc.retry_after))
        return response, exc.status
    if isinstance(exc, (QaContractError, QaPolicyError, QaSettingsError, ValueError)):
        return jsonify({"success": False, "error": {"code": "INVALID_REQUEST", "message": str(exc)}}), 400
    return jsonify({"success": False, "error": {"code": "INTERNAL_ERROR", "message": "系统暂时无法创建问答，请稍后重试。"}}), 500


@qa_bp.route("/api/qa/v1/bridge/exchange", methods=["POST"])
def exchange_ragflow_bridge():
    try:
        payload = request.get_json(silent=True) or {}
        token, claims = get_qa_bridge_manager().exchange(str(payload.get("assertion") or ""))
        response = jsonify({
            "success": True,
            "origin": "ragflow_ui",
            "industry_pack_id": claims["industry_pack_id"],
            "expires_at": claims["exp"],
        })
        forwarded_proto = str(request.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0].strip().casefold()
        response.set_cookie(
            "qa_bridge_session", token, max_age=BRIDGE_SESSION_TTL_SECONDS,
            httponly=True, secure=bool(request.is_secure or forwarded_proto == "https"),
            samesite="Lax", path="/",
        )
        return response
    except Exception as exc:
        return _error_response(exc)


@qa_bp.route("/api/qa/v1/bridge/status", methods=["GET"])
@qa_access_required
def ragflow_bridge_status():
    owner, pack = _identity()
    current = getattr(request, "current_user", {}) or {}
    return jsonify({
        "success": True,
        "origin": _trusted_origin(),
        "owner_user_id": owner,
        "industry_pack_id": pack,
        "allowed_kb_ids": list(current.get("allowed_kb_ids") or []),
    })


@qa_bp.route("/api/qa/v1/bridge/logout", methods=["POST"])
@qa_access_required
def logout_ragflow_bridge():
    token = bridge_token_from_request()
    if token:
        get_qa_bridge_manager().revoke(token)
    response = jsonify({"success": True})
    response.delete_cookie("qa_bridge_session", path="/")
    return response


@qa_bp.route("/api/qa/v1/runs", methods=["POST"])
@qa_access_required
def create_qa_run():
    try:
        owner, pack = _identity()
        payload = request.get_json(silent=True) or {}
        if not pack and not payload.get("industry_pack_id"):
            from industry_pack_runtime import active_industry_identity

            payload["industry_pack_id"] = str(active_industry_identity().get("id") or "")
        current = getattr(request, "current_user", {}) or {}
        if str(current.get("role") or "") == "qa_bridge":
            policy = get_qa_gateway_service().policy_resolver.resolve(pack or payload.get("industry_pack_id"))
            allowed_kb_ids = {str(item) for item in current.get("allowed_kb_ids") or []}
            if not policy.ragflow_kb_id or policy.ragflow_kb_id not in allowed_kb_ids:
                raise QaGatewayError(
                    "RAGFlow 登录用户无权访问该问答知识库。",
                    code="BRIDGE_KB_ACCESS_DENIED",
                    status=403,
                )
        run, created = get_qa_gateway_service().create_run(
            payload,
            owner_user_id=owner,
            authorized_pack_id=pack,
            idempotency_key=request.headers.get("Idempotency-Key") or "",
            trusted_origin=_trusted_origin(),
        )
        body = _public_run(run)
        body.update({
            "success": True,
            "created": created,
            "run_id": run["id"],
            "status_url": f"/api/qa/v1/runs/{run['id']}",
            "events_url": f"/api/qa/v1/runs/{run['id']}/events",
        })
        return jsonify(body), 201 if created else 200
    except Exception as exc:
        return _error_response(exc)


@qa_bp.route("/api/qa/v1/runs", methods=["GET"])
@qa_access_required
def list_qa_runs():
    try:
        owner, pack = _identity()
        requested_pack = str(request.args.get("industry_pack_id") or "").strip()
        if pack and requested_pack and requested_pack != pack:
            raise QaGatewayError("无权访问该行业包", code="PACK_ACCESS_DENIED", status=403)
        if not pack and not requested_pack:
            from industry_pack_runtime import active_industry_identity

            requested_pack = str(active_industry_identity().get("id") or "")
        runs = get_qa_gateway_service().list_runs(
            owner_user_id=owner,
            authorized_pack_id=pack or requested_pack,
            session_id=str(request.args.get("session_id") or ""),
            limit=int(request.args.get("limit") or 50),
        )
        return jsonify({"success": True, "runs": [_public_run(item) for item in runs]})
    except Exception as exc:
        return _error_response(exc)


@qa_bp.route("/api/qa/v1/runs/<run_id>", methods=["GET"])
@qa_access_required
def get_qa_run(run_id: str):
    try:
        owner, _pack = _identity()
        run = get_qa_gateway_service().get_run(run_id, owner_user_id=owner)
        return jsonify({"success": True, **_public_run(run)})
    except Exception as exc:
        return _error_response(exc)


@qa_bp.route("/api/qa/v1/runs/<run_id>/events", methods=["GET"])
@qa_access_required
def qa_run_events(run_id: str):
    try:
        owner, _pack = _identity()
        service = get_qa_gateway_service()
        service.get_run(run_id, owner_user_id=owner)
        raw_after = request.headers.get("Last-Event-ID") or request.args.get("after") or "0"
        after = max(0, int(raw_after))
        follow = str(request.args.get("follow", "1")).casefold() not in {"0", "false", "no"}
    except Exception as exc:
        return _error_response(exc)

    @stream_with_context
    def generate():
        cursor = after
        deadline = time.monotonic() + 20.0
        while True:
            emitted = False
            for event in service.events(run_id, owner_user_id=owner, after=cursor):
                cursor = max(cursor, int(event.get("event_id") or 0))
                emitted = True
                yield encode_qa_sse(event)
            run = service.get_run(run_id, owner_user_id=owner)
            if not follow or run.get("status") in {"completed", "failed", "cancelled"}:
                break
            if time.monotonic() >= deadline:
                yield ": keep-alive\n\n"
                break
            if not emitted:
                time.sleep(0.25)

    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@qa_bp.route("/api/qa/v1/runs/<run_id>/cancel", methods=["POST"])
@qa_access_required
def cancel_qa_run(run_id: str):
    try:
        owner, _pack = _identity()
        run = get_qa_gateway_service().cancel(run_id, owner_user_id=owner)
        return jsonify({"success": True, **_public_run(run)})
    except Exception as exc:
        return _error_response(exc)


@qa_bp.route("/api/qa/v1/runs/<run_id>/retry", methods=["POST"])
@qa_access_required
def retry_qa_run(run_id: str):
    try:
        owner, _pack = _identity()
        payload = request.get_json(silent=True) or {}
        run = get_qa_gateway_service().retry(
            run_id,
            owner_user_id=owner,
            stage=str(payload.get("stage") or ""),
            idempotency_key=request.headers.get("Idempotency-Key") or "",
        )
        return jsonify({"success": True, **_public_run(run)}), 202
    except Exception as exc:
        return _error_response(exc)


@qa_bp.route("/api/qa/v1/config", methods=["GET"])
@qa_access_required
def qa_public_config():
    try:
        owner, pack = _identity()
        if not pack:
            from industry_pack_runtime import active_industry_identity

            pack = str(active_industry_identity().get("id") or "")
        service = get_qa_gateway_service()
        policy = service.policy_resolver.resolve(pack)
        providers = []
        for provider_id in ("local", "openai", "deepseek", "gemini"):
            try:
                providers.append(service.provider_registry.resolve(
                    "draft", provider_id, owner_user_id=owner, industry_pack_id=pack,
                ).public_dict())
            except Exception:
                continue
        return jsonify({
            "success": True,
            "contract_version": QA_CONTRACT_VERSION,
            "origin": _trusted_origin(),
            "modes": list(QA_MODES),
            "default_mode": "standard",
            "industry_pack_id": pack,
            "research_configured": bool(policy.ragflow_kb_id and policy.ragflow_app_id),
            "providers": providers,
            "stage_budgets": STAGE_BUDGET_SECONDS,
            "settings_url": "/my-ai-settings",
        })
    except Exception as exc:
        return _error_response(exc)


def _is_admin_identity() -> bool:
    current = getattr(request, "current_user", {}) or {}
    return str(current.get("role") or "").casefold() == "admin"


def _settings_service() -> QaSettingsService:
    service = get_qa_gateway_service()
    return QaSettingsService(
        service.store.database,
        provider_registry=service.provider_registry,
        policy_resolver=service.policy_resolver,
    )


@qa_bp.route("/api/qa/v1/settings", methods=["GET"])
@qa_access_required
def qa_settings_get():
    try:
        owner, pack = _identity()
        if not pack:
            from industry_pack_runtime import active_industry_identity

            pack = str(active_industry_identity().get("id") or "")
        return jsonify(_settings_service().get(
            owner_user_id=owner, industry_pack_id=pack, is_admin=_is_admin_identity(),
        ))
    except Exception as exc:
        return _error_response(exc)


@qa_bp.route("/api/qa/v1/settings", methods=["PUT"])
@qa_access_required
def qa_settings_put():
    try:
        owner, pack = _identity()
        if not pack:
            from industry_pack_runtime import active_industry_identity

            pack = str(active_industry_identity().get("id") or "")
        result = _settings_service().save(
            request.get_json(silent=True) or {}, owner_user_id=owner, industry_pack_id=pack,
        )
        return jsonify(result)
    except Exception as exc:
        return _error_response(exc)


@qa_bp.route("/api/qa/v1/settings/providers/<provider_id>/probe", methods=["POST"])
@qa_access_required
def qa_provider_probe(provider_id: str):
    try:
        owner, pack = _identity()
        if not pack:
            from industry_pack_runtime import active_industry_identity

            pack = str(active_industry_identity().get("id") or "")
        result = _settings_service().probe_provider(
            provider_id, owner_user_id=owner, industry_pack_id=pack,
        )
        return jsonify({"success": True, **result})
    except Exception as exc:
        return _error_response(exc)


@qa_bp.route("/api/qa/v1/settings/network/probe", methods=["POST"])
@qa_access_required
def qa_network_probe():
    try:
        owner, _pack = _identity()
        return jsonify({"success": True, **_settings_service().probe_network(owner_user_id=owner)})
    except Exception as exc:
        return _error_response(exc)


@qa_bp.route("/api/qa/v1/health", methods=["GET"])
@qa_access_required
def qa_health():
    try:
        owner, pack = _identity()
        if not pack:
            from industry_pack_runtime import active_industry_identity

            pack = str(active_industry_identity().get("id") or "")
        live = _is_admin_identity() and str(request.args.get("live") or "").casefold() in {"1", "true", "yes"}
        service = get_qa_gateway_service()
        health = QaHealthService(
            service.store.database,
            provider_registry=service.provider_registry,
            policy_resolver=service.policy_resolver,
        ).check(owner_user_id=owner, industry_pack_id=pack, live=live)
        return jsonify({"success": True, **health})
    except Exception as exc:
        return _error_response(exc)


@qa_bp.route("/api/qa/v1/metrics", methods=["GET"])
@qa_access_required
def qa_metrics():
    if not _is_admin_identity():
        return _error_response(QaGatewayError("需要管理员权限。", code="ADMIN_REQUIRED", status=403))
    try:
        pack = str(request.args.get("industry_pack_id") or "")
        result = QaMetricsService(get_qa_gateway_service().store.database).snapshot(industry_pack_id=pack)
        return jsonify({"success": True, **result})
    except Exception as exc:
        return _error_response(exc)


@qa_bp.route("/api/qa/v1/audit", methods=["GET"])
@qa_access_required
def qa_audit():
    if not _is_admin_identity():
        return _error_response(QaGatewayError("需要管理员权限。", code="ADMIN_REQUIRED", status=403))
    try:
        events = get_qa_gateway_service().store.list_audit_events(
            trace_id=str(request.args.get("trace_id") or ""),
            limit=int(request.args.get("limit") or 200),
        )
        return jsonify({"success": True, "events": events})
    except Exception as exc:
        return _error_response(exc)


@qa_bp.route("/api/qa/v1/feature-flags", methods=["GET", "PUT"])
@qa_access_required
def qa_feature_flags():
    if not _is_admin_identity():
        return _error_response(QaGatewayError("需要管理员权限。", code="ADMIN_REQUIRED", status=403))
    try:
        owner, _pack = _identity()
        flags = QaFeatureFlags(get_qa_gateway_service().store.database)
        result = flags.update(request.get_json(silent=True) or {}, actor_id=owner) if request.method == "PUT" else flags.snapshot()
        return jsonify({"success": True, "flags": result})
    except Exception as exc:
        return _error_response(exc)


__all__ = ["QaGatewayError", "QaGatewayService", "get_qa_gateway_service", "qa_bp", "set_qa_gateway_service"]

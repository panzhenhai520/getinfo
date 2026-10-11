#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 Phase 16（P16-01…P16-06）· **可观测性 UI 的只读 HTTP API**。

三个端点，**全部 GET**、全部走既有严格鉴权装饰器 `qa_access_required`
（未登录 → `login_required` 对 `/api/` 路径返回 401；带 bridge cookie → 校验签名与知识库范围）：

    GET /api/qa/v1/runs/<run_id>/observability              五个 run 级视图一次给全
    GET /api/qa/v1/runs/<run_id>/observability/<view>        单个 run 级视图
    GET /api/qa/v1/observability/dashboard                   全局看板（跨 run）

硬约束（P16）：**只读**。蓝图里不注册任何 POST/PUT/DELETE/PATCH；投影层不写库；
归属校验复用 Phase 01 起就有的 `QaGatewayService.get_run(run_id, owner_user_id=...)`
（跨用户读别人的 run 一律 404，与其余 run 级接口同一口径）。
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from qa_bridge import qa_access_required
from qa_graph_contracts import OBSERVABILITY_UI_VERSION, OBSERVABILITY_VIEWS

observability_bp = Blueprint("qa_observability", __name__)

# 视图名 → 该视图接受的查询参数（只读、可选；缺省值由投影层给）
_VIEW_QUERY = {
    "execution": (),
    "evidence": ("limit",),
    "context": ("trace_limit",),
    "memory": ("limit",),
    "search_trace": (),
    "dashboard": ("industry_pack_id", "limit"),
}


class QaObservabilityError(ValueError):
    """API 层自己的参数错误（未知视图名 / 非法 limit）。"""

    def __init__(self, message: str, *, code: str = "INVALID_REQUEST", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def _identity() -> tuple:
    """身份口径**只有一处**：直接复用网关的 `_identity()`，不在这里重写一份。"""
    from qa_gateway import _identity as gateway_identity

    return gateway_identity()


def _error_response(exc):
    """错误响应同样只有一处：复用网关的 `_error_response()`（含 401/403/404 语义）。"""
    from qa_gateway import _error_response as gateway_error

    if isinstance(exc, QaObservabilityError):
        return jsonify({"success": False, "error": {"code": exc.code,
                                                    "message": str(exc)}}), exc.status
    return gateway_error(exc)


def _int_arg(name: str, default: int, low: int, high: int) -> int:
    raw = request.args.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise QaObservabilityError("参数 %s 必须是整数" % name) from exc
    if value < low or value > high:
        raise QaObservabilityError("参数 %s 必须在 %d..%d 之间" % (name, low, high))
    return value


def _service():
    from qa_gateway import get_qa_gateway_service

    return get_qa_gateway_service()


def _owned_run(run_id: str) -> dict:
    """归属校验：读别人的 run 与读不存在的 run 一律 404（网关既有口径）。"""
    owner, _pack = _identity()
    return _service().get_run(run_id, owner_user_id=owner)


def _envelope(view: str, payload: dict) -> dict:
    return {"success": True, "view_version": OBSERVABILITY_UI_VERSION,
            "view": view, "read_only": True, "observability": payload}


@observability_bp.route("/api/qa/v1/runs/<run_id>/observability", methods=["GET"])
@qa_access_required
def qa_run_observability(run_id: str):
    """五个 run 级视图一次给全（页面一次请求即可渲染）。只读、零模型调用。"""
    try:
        _owned_run(run_id)
        import qa_observability_ui as ui

        payload = ui.run_views(
            _service().store, run_id,
            evidence_limit=_int_arg("limit", 20, 1, 200),
            memory_limit=_int_arg("memory_limit", 200, 1, 2000))
        return jsonify(_envelope("all", payload))
    except Exception as exc:  # noqa: BLE001 —— 统一交给网关的错误翻译
        return _error_response(exc)


@observability_bp.route("/api/qa/v1/runs/<run_id>/observability/<view>", methods=["GET"])
@qa_access_required
def qa_run_observability_view(run_id: str, view: str):
    """单个 run 级视图。`dashboard` 不在这里（它是跨 run 的全局看板）。"""
    try:
        name = str(view or "")
        if name not in OBSERVABILITY_VIEWS:
            raise QaObservabilityError(
                "未知视图：%s（可用：%s）" % (name, "、".join(OBSERVABILITY_VIEWS)),
                code="UNKNOWN_VIEW")
        if name == "dashboard":
            raise QaObservabilityError(
                "全局看板请用 /api/qa/v1/observability/dashboard（它不带 run_id）",
                code="VIEW_NOT_RUN_SCOPED")
        _owned_run(run_id)
        import qa_observability_ui as ui

        kwargs: dict = {}
        if name == "evidence":
            kwargs["limit"] = _int_arg("limit", 20, 1, 200)
        elif name == "memory":
            kwargs["limit"] = _int_arg("limit", 200, 1, 2000)
        elif name == "context":
            kwargs["trace_limit"] = _int_arg("trace_limit", 600, 1, 5000)
        payload = ui.build_view(_service().store, run_id, name, **kwargs)
        return jsonify(_envelope(name, payload))
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc)


@observability_bp.route("/api/qa/v1/observability/dashboard", methods=["GET"])
@qa_access_required
def qa_observability_dashboard():
    """全局看板：cost / latency / token / cache / skill（全部来自既有落点与既有指标服务）。"""
    try:
        import qa_observability_ui as ui

        pack = str(request.args.get("industry_pack_id") or "").strip()
        payload = ui.dashboard(_service().store, industry_pack_id=pack,
                               limit=_int_arg("limit", 2000, 1, 10000))
        return jsonify(_envelope("dashboard", payload))
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc)


@observability_bp.route("/api/qa/v1/observability/views", methods=["GET"])
@qa_access_required
def qa_observability_catalog():
    """视图目录（自描述）：页面与验收脚本据此渲染，不在前端硬编码视图清单。"""
    try:
        return jsonify({
            "success": True,
            "view_version": OBSERVABILITY_UI_VERSION,
            "read_only": True,
            "views": [{"view": name, "run_scoped": name != "dashboard",
                       "query": list(_VIEW_QUERY.get(name, ()))}
                      for name in OBSERVABILITY_VIEWS],
        })
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc)


__all__ = ["QaObservabilityError", "observability_bp"]

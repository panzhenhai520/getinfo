#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 16 · 只读 HTTP API 用例（P16-01…P16-06 的接口面）。

覆盖：
  1. **未登录**：`/api/qa/v1/...` 一律 401 JSON（既有约定，不许放行）；
  2. **登录后**：五个 run 级视图 + 全局看板 + 视图目录都给 200，且载荷过契约；
  3. **失败路径**：未知视图名 / 拿 run 级路径要全局看板 / 非法 limit / 越权读别人的 run；
  4. **只读**：所有端点跑一遍，库里一行都不变；路由表里只有 GET。
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask  # noqa: E402

import qa_gateway  # noqa: E402
import qa_observability_ui as ui  # noqa: E402
import qa_phase16_fixtures as fx  # noqa: E402
from qa_graph_contracts import OBSERVABILITY_VIEWS, validate  # noqa: E402
from qa_observability_api import observability_bp  # noqa: E402

ADMIN = {"user_id": 1, "role": "admin", "username": "tester"}


class _ApiBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db, self.store = fx.store_for(self.temp_dir.name)
        self.assertEqual(self.db.backend, "sqlite", "测试必须跑在隔离 sqlite 上")
        self.run, _receipt = fx.seed_standard_run(self.db, self.store, owner="user:1")
        self.run_id = str(self.run["id"])
        self._saved_service = qa_gateway._service_override
        qa_gateway.set_qa_gateway_service(qa_gateway.QaGatewayService(self.store, None))
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True)
        self.app.register_blueprint(observability_bp)
        self.client = self.app.test_client()

    def tearDown(self):
        qa_gateway.set_qa_gateway_service(self._saved_service)
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def _login(self):
        patcher = patch("decorators.user_db.verify_session", return_value=dict(ADMIN))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client.set_cookie("localhost", "session_token", "test-session-token")

    def _counts(self):
        def _row(sql):
            return int(self.store.database.connection.execute(sql).fetchone()[0] or 0)

        return {name: _row("SELECT count(*) FROM %s" % name)
                for name in ("qa_stage_runs", "memory_item", "memory_recall_log",
                             "qa_reasoning_traces", "qa_events", "qa_retrieval_cache")}


class UnauthenticatedTests(_ApiBase):
    def test_run_views_require_login(self):
        for path in ("/api/qa/v1/runs/%s/observability" % self.run_id,
                     "/api/qa/v1/runs/%s/observability/execution" % self.run_id,
                     "/api/qa/v1/observability/dashboard",
                     "/api/qa/v1/observability/views"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 401, path)
            body = response.get_json()
            self.assertFalse(body["success"], path)

    def test_bridge_cookie_alone_grants_nothing(self):
        """只带一个没签名的 bridge cookie：必须有严格校验把它挡在外面（不是 200）。"""
        self.client.set_cookie("localhost", "qa_bridge_session", "forged")
        response = self.client.get("/api/qa/v1/runs/%s/observability" % self.run_id)
        self.assertIn(response.status_code, {401, 403})


class AuthenticatedTests(_ApiBase):
    def setUp(self):
        super().setUp()
        self._login()

    def test_aggregate_endpoint_returns_all_five_views(self):
        response = self.client.get("/api/qa/v1/runs/%s/observability" % self.run_id)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["success"])
        self.assertIs(body["read_only"], True)
        views = body["observability"]["views"]
        self.assertEqual(set(views), set(ui.RUN_VIEWS))
        for name, view in views.items():
            ok, note = validate("observability_view", view)
            self.assertTrue(ok, "%s: %s" % (name, note))

    def test_single_view_endpoints(self):
        for name in ui.RUN_VIEWS:
            response = self.client.get("/api/qa/v1/runs/%s/observability/%s"
                                       % (self.run_id, name))
            self.assertEqual(response.status_code, 200, name)
            payload = response.get_json()["observability"]
            self.assertEqual(payload["view"], name)
            ok, note = validate("observability_view", payload)
            self.assertTrue(ok, "%s: %s" % (name, note))

    def test_single_view_query_parameters(self):
        response = self.client.get("/api/qa/v1/runs/%s/observability/evidence?limit=3"
                                   % self.run_id)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()["observability"]
        self.assertLessEqual(len(payload["payload"]["claims"]), 3)
        response = self.client.get("/api/qa/v1/runs/%s/observability/context?trace_limit=2"
                                   % self.run_id)
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(response.get_json()["observability"]["payload"]["selection_trace"]), 2)

    def test_dashboard_endpoint(self):
        response = self.client.get("/api/qa/v1/observability/dashboard")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()["observability"]
        self.assertEqual(payload["view"], "dashboard")
        self.assertEqual(payload["run_id"], "*")
        ok, note = validate("observability_view", payload)
        self.assertTrue(ok, note)
        self.assertEqual(response.get_json()["view"], "dashboard")

    def test_catalog_endpoint_is_self_describing(self):
        response = self.client.get("/api/qa/v1/observability/views")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual([item["view"] for item in body["views"]], list(OBSERVABILITY_VIEWS))
        run_scoped = {item["view"]: item["run_scoped"] for item in body["views"]}
        self.assertFalse(run_scoped["dashboard"])
        self.assertTrue(run_scoped["execution"])

    def test_unknown_view_is_400(self):
        response = self.client.get("/api/qa/v1/runs/%s/observability/nope" % self.run_id)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"]["code"], "UNKNOWN_VIEW")

    def test_dashboard_view_is_not_run_scoped(self):
        response = self.client.get("/api/qa/v1/runs/%s/observability/dashboard" % self.run_id)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"]["code"], "VIEW_NOT_RUN_SCOPED")

    def test_invalid_limit_is_400(self):
        for query in ("limit=abc", "limit=0", "limit=99999"):
            response = self.client.get("/api/qa/v1/runs/%s/observability/evidence?%s"
                                       % (self.run_id, query))
            self.assertEqual(response.status_code, 400, query)
            self.assertEqual(response.get_json()["error"]["code"], "INVALID_REQUEST")

    def test_other_users_run_is_not_readable(self):
        other = self.store.create_run({"industry_pack_id": "auto", "question": "别人的问题",
                                       "mode": "standard"},
                                      owner_user_id="user:9", idempotency_key="phase16-other")
        response = self.client.get("/api/qa/v1/runs/%s/observability" % other["id"])
        self.assertEqual(response.status_code, 404, "越权读别人的 run 必须 404（不是 200/403）")
        self.assertEqual(response.get_json()["error"]["code"], "RUN_NOT_FOUND")

    def test_missing_run_is_404(self):
        response = self.client.get("/api/qa/v1/runs/does-not-exist/observability")
        self.assertEqual(response.status_code, 404)

    def test_api_is_read_only(self):
        before = self._counts()
        for path in ("/api/qa/v1/runs/%s/observability" % self.run_id,
                     "/api/qa/v1/observability/dashboard",
                     "/api/qa/v1/observability/views"):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        for name in ui.RUN_VIEWS:
            self.client.get("/api/qa/v1/runs/%s/observability/%s" % (self.run_id, name))
        self.assertEqual(before, self._counts(), "只读接口改动了数据库")

    def test_every_registered_route_is_get_only(self):
        methods = set()
        for rule in self.app.url_map.iter_rules():
            if rule.endpoint == "static":
                continue
            methods |= {method for method in rule.methods if method not in {"HEAD", "OPTIONS"}}
        self.assertEqual(methods, {"GET"}, "可观测性蓝图里出现了非 GET 方法：%s" % sorted(methods))

    def test_owner_identity_comes_from_the_gateway(self):
        """身份口径只有一处：接口识别出的 owner 必须与网关 `_identity()` 一致。"""
        from qa_gateway import _identity

        with self.app.test_request_context("/"):
            from flask import request

            request.current_user = dict(ADMIN)
            self.assertEqual(_identity(), ("user:1", ""))


if __name__ == "__main__":
    unittest.main()

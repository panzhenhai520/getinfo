#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 16 · 页面与模板/DOM 契约用例（P16-01…P16-06 的前端面）。

覆盖：
  1. **前端契约静态检查**（`tools/check_qa_observability_view.py`）：六个视图的容器齐备、
     只读（零写请求）、所有动态文本都过转义、静态资源全部是仓库既有文件；
  2. **模板真的能渲染**：六个面板 + 六种状态容器都出现在渲染结果里，没有未解析的 Jinja；
  3. **路由可达性**：未登录 302 跳登录页、登录后 200（与全站既有约定一致）；
  4. **注册面**：页面路由与只读蓝图都真的注册进应用，且蓝图里只有 GET。
"""
import os
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

from check_qa_observability_view import inspect_frontend_contract, static_acceptance  # noqa: E402

VIEWS = ("execution", "evidence", "context", "memory", "search_trace", "dashboard")
TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "templates", "qa_observability.html")


class FrontendContractTests(unittest.TestCase):
    def test_frontend_contract_is_safe_and_read_only(self):
        frontend = inspect_frontend_contract()
        self.assertEqual(frontend["missing_markers"], [], "结构契约标记缺失")
        self.assertEqual(frontend["write_markers"], [], "模板里出现了写请求/写路径")
        self.assertEqual(frontend["unescaped_sink_functions"], [],
                         "有函数把未转义的动态文本塞进了 HTML sink")
        self.assertEqual(frontend["static_assets_missing"], [])
        self.assertTrue(frontend["safe"], frontend)
        self.assertTrue(frontend["read_only"], frontend)

    def test_static_acceptance_passes(self):
        report = static_acceptance()
        self.assertEqual(report["view_version"], "qa-observability-ui-v1")
        self.assertTrue(all(report["checks"].values()), report["checks"])
        self.assertEqual(report["new_tables"], [])
        self.assertEqual(report["new_columns"], [])
        self.assertEqual(report["schema_version"], "unified-qa-schema-v11")
        self.assertEqual(report["model_calls"], 0)
        self.assertEqual(report["gpu_calls"], 0)

    def test_template_only_uses_existing_static_assets(self):
        frontend = inspect_frontend_contract()
        for asset in frontend["static_assets"]:
            self.assertTrue(asset.startswith("/static/"), asset)
        # 六个视图的 tab 与面板一一对应（`data-view` 在 tab 与 panel 上各出现一次）
        source = open(TEMPLATE_PATH, encoding="utf-8").read()
        for view in VIEWS:
            self.assertEqual(source.count('data-view="%s"' % view), 2,
                             "%s 的 tab + 面板必须各一个" % view)
            self.assertEqual(source.count('id="qoPanel-%s"' % view), 1,
                             "%s 的面板必须且只有一个" % view)
            self.assertEqual(source.count('data-view="%s" role="tab"' % view), 1,
                             "%s 的 tab 按钮必须且只有一个" % view)

    def test_read_only_markers_are_present(self):
        source = open(TEMPLATE_PATH, encoding="utf-8").read()
        self.assertIn('data-read-only="1"', source)
        self.assertIn("只读视图", source)
        self.assertIn("只读投影", source)


class TemplateRenderTests(unittest.TestCase):
    def test_template_renders_all_six_panels(self):
        import firecrawl_app

        with firecrawl_app.app.test_request_context("/qa-observability"):
            html = firecrawl_app.render_template("qa_observability.html")
        self.assertNotIn("{{", html, "渲染后仍有未解析的 Jinja 变量")
        self.assertNotIn("{%", html, "渲染后仍有未解析的 Jinja 语句")
        for view in VIEWS:
            self.assertIn('id="qoPanel-%s"' % view, html)
            self.assertIn('id="qoState-%s"' % view, html)
            self.assertIn('id="qoSource-%s"' % view, html)
            self.assertIn('id="qoNumbers-%s"' % view, html)
            self.assertIn('id="qoBody-%s"' % view, html)
            self.assertIn('id="qoNotes-%s"' % view, html)
        # 空态与错误态的分支必须在渲染结果里（JS 静态文本也算 —— 页面必须能显示它们）
        self.assertIn("暂无数据", html)
        self.assertIn("需要登录", html)
        self.assertIn("找不到该问答运行", html)
        # 六个只读接口路径
        for endpoint in ("/api/qa/v1/runs/", "/api/qa/v1/observability/dashboard",
                         "/api/qa/v1/runs?limit=50"):
            self.assertIn(endpoint, html)


class RouteReachabilityTests(unittest.TestCase):
    def setUp(self):
        import firecrawl_app

        self.app = firecrawl_app.app
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()

    def test_page_is_registered(self):
        rules = {str(rule.rule) for rule in self.app.url_map.iter_rules()}
        self.assertIn("/qa-observability", rules)
        for rule in ("/api/qa/v1/runs/<run_id>/observability",
                     "/api/qa/v1/runs/<run_id>/observability/<view>",
                     "/api/qa/v1/observability/dashboard",
                     "/api/qa/v1/observability/views"):
            self.assertIn(rule, rules, "%s 没注册进应用" % rule)

    def test_observability_blueprint_is_get_only_in_the_real_app(self):
        methods = set()
        for rule in self.app.url_map.iter_rules():
            if not str(rule.rule).startswith("/api/qa/v1/") and \
                    str(rule.rule) != "/qa-observability":
                continue
            if "observability" not in str(rule.rule) and str(rule.rule) != "/qa-observability":
                continue
            methods |= {item for item in rule.methods if item not in {"HEAD", "OPTIONS"}}
        self.assertEqual(methods, {"GET"}, "可观测性相关的路由出现了非 GET 方法：%s" % sorted(methods))

    def test_unauthenticated_page_redirects_to_login(self):
        with patch("decorators.user_db.verify_session", return_value=None):
            response = self.client.get("/qa-observability")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))

    def test_unauthenticated_api_returns_401(self):
        with patch("decorators.user_db.verify_session", return_value=None):
            response = self.client.get("/api/qa/v1/observability/views")
        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.get_json()["success"])

    def test_authenticated_page_renders(self):
        # 前一道闸门（`firecrawl_app.require_login_for_app`）读的是**应用自己那份** user_db，
        # 与 `decorators.user_db` 不是同一个绑定，两处都要打桩。
        with patch("firecrawl_app.user_db.verify_session",
                   return_value={"user_id": 1, "role": "admin", "username": "tester"}), \
                patch("decorators.user_db.verify_session",
                      return_value={"user_id": 1, "role": "admin", "username": "tester"}):
            self.client.set_cookie("localhost", "session_token", "test-session-token")
            response = self.client.get("/qa-observability")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for view in VIEWS:
            self.assertIn('id="qoPanel-%s"' % view, html)


class NavEntryTests(unittest.TestCase):
    def test_nav_has_the_observability_entry(self):
        nav = os.path.join(os.path.dirname(TEMPLATE_PATH), "_dashboard_nav.html")
        source = open(nav, encoding="utf-8").read()
        self.assertIn('href="/qa-observability"', source)
        self.assertIn("可观测性", source)


if __name__ == "__main__":
    unittest.main()

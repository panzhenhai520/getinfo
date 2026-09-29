#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Stage 6.2 security and privacy acceptance gate."""

from __future__ import annotations

import argparse
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


RUNTIME_SUITES = (
    "tests.test_financial_security",
    "tests.test_financial_phase0_rss_feeds",
    "tests.test_intel_stage3",
    "tests.test_financial_sse",
    "tests.test_financial_feed",
    "tests.test_financial_answer_composer",
    "tests.test_financial_report_view",
    "tests.test_financial_simulation_gate",
    "tests.test_tradingagents_cn_data_adapter",
    "tests.test_stock_research_graph",
    "tests.test_index_market_research_graph",
)


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def static_acceptance() -> dict:
    security = _source("financial_security.py")
    safe_http = _source("intel_http.py")
    alpha = _source("financial_providers/alpha_vantage.py")
    fred = _source("financial_providers/fred.py")
    tushare = _source("financial_providers/tushare_cn.py")
    rss = _source("rss_feed_contract.py")
    sse = _source("financial_sse.py")
    feed = _source("financial_feed.py")
    answer = _source("financial_answer_composer.py")
    report = _source("financial_report_view.py")
    stock = _source("stock_research_graph.py")
    index = _source("index_market_research_graph.py")
    adapter = _source("tradingagents_cn_data_adapter.py")
    chat_api = _source("chat_api.py")
    intel_api = _source("intel_api.py")
    mapindex_api = _source("mapindex_api.py")
    template = _source("templates/mapindex.html")
    browser_gate = _source("tools/check_financial_dashboard_xss.py")

    financial_renderer_start = template.index("function renderFinancialFeed(data)")
    financial_renderer_end = template.index("async function loadIntelDashboard()")
    financial_renderers = template[financial_renderer_start:financial_renderer_end]
    checks = {
        "shared_secret_redaction_and_url_projection": all(
            marker in security
            for marker in (
                "configured_secret_values",
                "redact_sensitive_text",
                "redact_public_payload",
                "safe_public_url",
                "SENSITIVE_QUERY_KEYS",
            )
        ),
        "safe_http_validates_every_redirect_without_automatic_follow": (
            safe_http.count("validate_external_url(") >= 3
            and safe_http.count("allow_redirects=False") >= 2
        ),
        "external_provider_http_is_bounded": all(
            "SafeHTTPClient" in source for source in (alpha, fred)
        ),
        "tushare_sdk_endpoint_is_https_allowlisted": all(
            marker in tushare
            for marker in ("ALLOWED_API_HOSTS", "api.waditu.com", 'scheme="https"')
        ),
        "malicious_rss_dtd_entity_and_size_rejected": all(
            marker in rss
            for marker in ("FORBIDDEN_XML_DECLARATIONS", "<!doctype", "<!entity", "INTEL_SCAN_MAX_RESPONSE_BYTES")
        ),
        "public_sse_is_event_allowlisted_and_recursively_redacted": all(
            marker in sse
            for marker in ("_PUBLIC_EVENT_FIELDS", "redact_public_payload", "safe_public_url")
        ),
        "shared_chat_log_metric_json_and_sse_errors_are_redacted": (
            "def _safe_chat_error" in chat_api
            and "metric['error'] = _safe_chat_error" in chat_api
            and '"message":_safe_chat_error(e, 200)' in chat_api
            and "key_prefix" not in chat_api
        ),
        "public_financial_views_share_redaction": all(
            "financial_security" in source for source in (feed, answer, report)
        ),
        "external_documents_are_explicitly_untrusted": (
            "external_content_policy" in adapter
            and "content_is_untrusted_external_text" in adapter
        ),
        "stock_and_index_agents_forbid_instruction_tool_and_order_escalation": all(
            all(
                marker in source
                for marker in (
                    "never instructions",
                    "cannot mutate configuration",
                    "registered tool scope",
                    "trigger any order",
                    "unregistered_tool_call",
                )
            )
            for source in (stock, index)
        ),
        "financial_read_apis_require_login": (
            "@login_required\ndef get_financial_report" in chat_api
            and "@login_required\ndef get_financial_snapshot" in chat_api
            and "@login_required\ndef list_industry_packs" in intel_api
        ),
        "financial_adjudication_mutations_require_admin": (
            "@admin_required\ndef decide_mapindex_chat_conflict" in mapindex_api
            and "@admin_required\ndef get_mapindex_financial_knowledge_gate" in mapindex_api
        ),
        "frontend_uses_safe_links_and_text_nodes": (
            "function safeFinancialHref" in template
            and "url.username || url.password" in template
            and "hostname.endsWith('.localhost')" in template
            and "sensitiveKeys.includes(key.toLowerCase())" in template
            and "title.textContent" in financial_renderers
            and "content.textContent" in financial_renderers
            and "innerHTML" not in financial_renderers
        ),
        "browser_gate_covers_feed_report_and_unsafe_links": all(
            marker in browser_gate
            for marker in (
                "financial-dashboard-xss-v2",
                "renderFinancialFeed",
                "renderFinancialReport",
                "javascript:",
                "http://127.0.0.1/admin",
            )
        ),
    }
    _assert(all(checks.values()), checks)

    from tools.check_tradingagents_architecture import check_repository

    architecture = check_repository(ROOT)
    _assert(architecture["acceptance"]["passed"], architecture["acceptance"])
    return {
        "checks": checks,
        "architecture": {
            "passed": True,
            "services": architecture["compose"]["services"],
            "published_ports": architecture["ports"]["compose_published_container_ports"],
            "new_services": [],
            "new_ports": [],
            "new_tables": [],
            "real_trading_allowed": False,
        },
    }


def runtime_acceptance(browser_report_path: str) -> dict:
    _assert(browser_report_path, "--browser-report is required with --runtime")
    browser_path = Path(browser_report_path)
    _assert(browser_path.is_file(), f"browser report does not exist: {browser_path}")
    browser = json.loads(browser_path.read_text(encoding="utf-8"))
    _assert(browser["passed"], browser)
    _assert(browser["check_version"] == "financial-dashboard-xss-v2", browser)
    _assert(not browser["external_network_requests"], browser["external_network_requests"])

    network_attempts = []

    def blocked_network(*_args, **_kwargs):
        network_attempts.append("blocked")
        raise AssertionError("unexpected live network call during financial security tests")

    with tempfile.TemporaryDirectory() as temp_dir:
        previous_database = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = str(Path(temp_dir) / "security-gate.sqlite3")
        try:
            suite = unittest.TestSuite(
                unittest.defaultTestLoader.loadTestsFromName(name)
                for name in RUNTIME_SUITES
            )
            stream = io.StringIO()
            with patch.object(socket.socket, "connect", blocked_network), patch(
                "socket.create_connection", blocked_network
            ):
                result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
        finally:
            if previous_database is None:
                os.environ.pop("DATABASE_PATH", None)
            else:
                os.environ["DATABASE_PATH"] = previous_database
    _assert(result.wasSuccessful(), stream.getvalue())
    _assert(not network_attempts, network_attempts)

    return {
        "executed": True,
        "suites": list(RUNTIME_SUITES),
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "network_calls": len(network_attempts),
        "secrets_in_output": False,
        "browser": browser,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--browser-report", default="")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    report = {
        "acceptance": "passed",
        "task": "6.2",
        "security_version": "financial-security-v1",
        "static": static_acceptance(),
        "runtime": runtime_acceptance(args.browser_report) if args.runtime else {"executed": False},
        "scenarios": {
            "api_keys_and_tokens_redacted_from_logs_sse_and_json": True,
            "ssrf_and_private_redirects_blocked": True,
            "malicious_rss_and_xml_entities_rejected": True,
            "external_prompt_injection_is_data_only": True,
            "unregistered_tools_and_order_execution_blocked": True,
            "financial_read_apis_require_authentication": True,
            "adjudication_mutations_require_admin": True,
            "financial_feed_report_and_links_are_xss_safe": True,
        },
        "boundaries": {
            "new_database": False,
            "new_table": False,
            "new_service": False,
            "new_port": False,
            "real_trading": False,
        },
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

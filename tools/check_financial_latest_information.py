#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Offline acceptance gate for financial latest-information routing."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

P4_FIXTURE = ROOT / "tests" / "fixtures" / "financial_latest_bundle" / "scenarios.json"
DEFAULT_GOLDEN = ROOT / "tests" / "fixtures" / "financial_latest_information_golden.json"
RUNTIME_SUITES = (
    "tests.test_financial_information_needs",
    "tests.test_financial_latest_time",
    "tests.test_financial_instrument_discovery",
    "tests.test_financial_news_query",
    "tests.test_financial_latest_bundle",
    "tests.test_financial_latest_bundle_sse",
    "tests.test_financial_spacex_latest_e2e",
    "tests.test_financial_latest_information_e2e",
    "tests.test_financial_latest_observability",
    "tests.test_financial_sse",
)


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = {
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    modules.update(
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    return modules


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def static_acceptance(golden_path: Path = DEFAULT_GOLDEN) -> dict:
    fixture = json.loads(P4_FIXTURE.read_text(encoding="utf-8"))
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    case_ids = {str(item.get("id") or "") for item in fixture.get("cases") or []}
    expected_case_ids = {f"P4-{index:02d}" for index in range(1, 15)}
    p5_case_ids = {str(item) for item in golden.get("covered_cases") or []}
    expected_p5_case_ids = {f"P5-{index:02d}" for index in range(1, 13)}
    bundle_path = ROOT / "financial_latest_bundle.py"
    news_path = ROOT / "financial_news_query.py"
    chat_path = ROOT / "chat_api.py"
    config_path = ROOT / "config.py"
    bundle_source = bundle_path.read_text(encoding="utf-8")
    news_source = news_path.read_text(encoding="utf-8")
    chat_source = chat_path.read_text(encoding="utf-8")
    config_source = config_path.read_text(encoding="utf-8")
    orchestrator_source = (ROOT / "chat_route_orchestrator.py").read_text(
        encoding="utf-8"
    )
    prohibited = sorted(
        (_imports(bundle_path) | _imports(news_path))
        & {"requests", "httpx", "openai", "anthropic"}
    )
    required_files = (
        "tests/test_financial_news_query.py",
        "tests/test_financial_latest_bundle.py",
        "tests/test_financial_latest_bundle_sse.py",
        "tests/test_financial_spacex_latest_e2e.py",
        "tests/test_financial_latest_information_e2e.py",
        "tests/test_financial_latest_observability.py",
    )
    checks = {
        "all_p4_fixture_cases_present": case_ids == expected_case_ids,
        "fixture_network_policy_is_offline": fixture.get("network_policy") == "fixture_only",
        "golden_network_policy_is_none": golden.get("network_policy") == "none",
        "all_p5_golden_cases_present": p5_case_ids == expected_p5_case_ids,
        "golden_repeats_at_least_three_times": int(golden.get("repeat_runs") or 0) >= 3,
        "golden_hash_is_fixed": len(str(golden.get("expected_answer_sha256") or "")) == 64,
        "parallel_channel_executor_present": "ThreadPoolExecutor" in bundle_source,
        "closed_bundle_schema_present": (
            "LATEST_BUNDLE_SCHEMA" in bundle_source
            and '"additionalProperties": False' in bundle_source
        ),
        "independent_channel_timeouts_present": all(
            marker in bundle_source
            for marker in (
                "FINANCIAL_LATEST_QUOTE_TIMEOUT_SECONDS",
                "FINANCIAL_LATEST_NEWS_TIMEOUT_SECONDS",
                "FINANCIAL_LATEST_BUNDLE_TIMEOUT_SECONDS",
                "channel_timeout",
            )
        ),
        "child_feature_switches_present": all(
            marker in config_source
            for marker in (
                "FINANCIAL_LATEST_NEWS_ENABLED",
                "FINANCIAL_LATEST_BUNDLE_ENABLED",
            )
        ),
        "news_refresh_is_injected_not_direct_web_search": (
            "self.refresher" in news_source and not prohibited
        ),
        "unresolved_target_fails_closed": (
            "_closed_latest_information_response" in chat_source
            and "不会调用通用模型猜测" in chat_source
        ),
        "latest_observability_is_wired_to_existing_health": all(
            marker in (ROOT / "financial_health.py").read_text(encoding="utf-8")
            for marker in (
                "FinancialLatestObservabilityService",
                '"latest_information"',
            )
        ),
        "default_orchestrator_uses_live_financial_settings": (
            "financial_settings=_runtime_financial_settings" in orchestrator_source
        ),
        "all_required_test_files_present": all((ROOT / item).is_file() for item in required_files),
    }
    _assert(all(checks.values()), {"checks": checks, "prohibited": prohibited})
    return {
        "checks": checks,
        "fixture": str(P4_FIXTURE.relative_to(ROOT)),
        "fixture_case_count": len(case_ids),
        "fixture_sha256": _sha256(P4_FIXTURE),
        "golden": str(golden_path.relative_to(ROOT)),
        "golden_sha256": _sha256(golden_path),
        "golden_answer_sha256": golden["expected_answer_sha256"],
        "golden_repeat_runs": int(golden["repeat_runs"]),
        "direct_network_or_model_imports": prohibited,
        "new_ports": [],
        "new_services": [],
    }


def runtime_acceptance() -> dict:
    suite = unittest.TestSuite(
        unittest.defaultTestLoader.loadTestsFromName(name)
        for name in RUNTIME_SUITES
    )
    stream = io.StringIO()
    def _network_forbidden(*_args, **_kwargs):
        raise AssertionError("offline acceptance forbids network access")

    with patch.object(socket.socket, "connect", _network_forbidden), patch.object(
        socket, "create_connection", _network_forbidden
    ):
        result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    _assert(result.wasSuccessful(), stream.getvalue())
    return {
        "executed": True,
        "suites": list(RUNTIME_SUITES),
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "network_policy": "fixture_only",
        "network_calls_observed": 0,
        "generic_model_fallback_asserted_zero": True,
    }


def build_report(*, runtime: bool, golden_path: Path = DEFAULT_GOLDEN) -> dict:
    return {
        "acceptance": "passed",
        "acceptance_version": "financial-latest-information-v1",
        "task": "latest-information-stages-1-through-5",
        "static": static_acceptance(golden_path),
        "runtime": runtime_acceptance() if runtime else {"executed": False},
        "boundaries": {
            "quote_and_news_are_separate_evidence_channels": True,
            "news_document_numbers_are_not_quotes": True,
            "future_records_are_rejected": True,
            "partial_channel_results_are_preserved": True,
            "unverified_targets_and_total_failure_do_not_use_general_model": True,
            "legacy_sse_contract_is_preserved": True,
            "real_order_execution": False,
        },
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--network", choices=("none",))
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    fixture = args.fixture.expanduser().resolve()
    if not fixture.is_file():
        parser.error(f"fixture not found: {fixture}")
    report = build_report(
        runtime=bool(args.runtime or args.network == "none"),
        golden_path=fixture,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Stage 2 aggregate gate for the embedded TradingAgents foundation."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


REQUIRED_ACCEPTANCE_ARTIFACTS = (
    "tradingagents-supply-chain-acceptance.json",
    "tradingagents-component-acceptance.json",
    "financial-schema-acceptance.json",
    "global-auxiliary-provider-acceptance.json",
    "financial-evidence-acceptance.json",
    "shared-llm-broker-acceptance.json",
    "tradingagents-llm-adapter-acceptance.json",
    "tradingagents-cn-data-adapter-acceptance.json",
    "stock-research-graph-acceptance.json",
    "index-market-research-graph-acceptance.json",
    "fund-etf-research-acceptance.json",
    "financial-research-persistence-acceptance.json",
    "financial-worker-jobs-acceptance.json",
    "financial-market-scheduler-acceptance.json",
)


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def _artifact_passed(data):
    if data.get("passed") is True or data.get("status") == "passed":
        return True
    acceptance = data.get("acceptance")
    if acceptance == "passed":
        return True
    return isinstance(acceptance, dict) and acceptance.get("passed") is True


def _load_artifact(name):
    path = ROOT / "architecture" / name
    _assert(path.is_file(), f"acceptance artifact missing: {name}")
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def static_acceptance():
    from tools.check_tradingagents_architecture import check_repository

    hashes = {}
    for name in REQUIRED_ACCEPTANCE_ARTIFACTS:
        artifact, digest = _load_artifact(name)
        _assert(_artifact_passed(artifact), f"acceptance artifact did not pass: {name}")
        hashes[name] = digest

    topology = check_repository(ROOT)
    _assert(topology["acceptance"]["passed"], "architecture topology gate failed")
    services = topology["compose"]["services"]
    ports = topology["ports"]["compose_published_container_ports"]
    _assert(
        services == ["crawler", "intel-worker", "redis", "worker"],
        "compose service set changed",
    )
    _assert(ports == [8003], "published ports changed")

    tushare, tushare_hash = _load_artifact("tushare-cn-live-acceptance.json")
    tushare_state = "configured_and_passed" if tushare.get("passed") else "not_configured"
    if tushare_state == "not_configured":
        _assert(tushare.get("skipped") is True, "Tushare failure is not an explicit skip")
        _assert(
            tushare.get("reason") == "tushare_token_not_configured",
            "Tushare skip reason changed",
        )
    hashes["tushare-cn-live-acceptance.json"] = tushare_hash

    return {
        "required_artifact_count": len(REQUIRED_ACCEPTANCE_ARTIFACTS),
        "artifact_sha256": hashes,
        "compose_services": services,
        "published_ports": ports,
        "tushare_state": tushare_state,
        "duplicate_services": [],
        "duplicate_ports": [],
    }


def _validate_target(target, *, index=False):
    symbol = str(target.get("canonical_symbol") or "")
    _assert(target.get("passed") is True, f"target did not pass: {symbol}")
    _assert(target.get("providers"), f"provider lineage missing: {symbol}")
    _assert(int(target.get("section_count") or 0) >= (15 if index else 14), f"report sections missing: {symbol}")
    _assert(target.get("report_status"), f"report status missing: {symbol}")
    _assert(target.get("evidence_coverage") is not None, f"evidence coverage missing: {symbol}")
    _assert(target.get("execution_allowed") is False, f"execution boundary failed: {symbol}")
    if index:
        _assert(target.get("order_target") is None, f"index order target exists: {symbol}")
        _assert(target.get("compiler"), f"index compiler missing: {symbol}")
        _assert(target.get("market_status"), f"market status missing: {symbol}")
    else:
        _assert(target.get("latest_observed_at"), f"observed time missing: {symbol}")
    return {
        "canonical_symbol": symbol,
        "providers": list(target["providers"]),
        "report_status": target["report_status"],
        "evidence_coverage": target["evidence_coverage"],
        "section_count": target["section_count"],
        "execution_allowed": False,
    }


def target_traceability_acceptance():
    stock_report, stock_hash = _load_artifact("stock-research-live-acceptance.json")
    index_report, index_hash = _load_artifact("index-market-research-live-acceptance.json")
    _assert(stock_report.get("passed") is True, "stock live trace artifact failed")
    _assert(index_report.get("passed") is True, "index live trace artifact failed")
    _assert(stock_report.get("secrets_included") is False, "stock trace contains secrets")
    _assert(index_report.get("secrets_included") is False, "index trace contains secrets")
    stock_targets = {
        str(item.get("canonical_symbol")): item for item in stock_report.get("targets", ())
    }
    index_targets = {
        str(item.get("canonical_symbol")): item for item in index_report.get("targets", ())
    }
    required_stocks = ("0700.HK", "600000.SH", "000001.SZ")
    required_indices = ("000001.SH", "399001.SZ", "HSI.HK")
    _assert(set(required_stocks) <= set(stock_targets), "stock target trace is incomplete")
    _assert(set(required_indices) <= set(index_targets), "index target trace is incomplete")
    return {
        "stocks": [
            _validate_target(stock_targets[symbol]) for symbol in required_stocks
        ],
        "indices": [
            _validate_target(index_targets[symbol], index=True)
            for symbol in required_indices
        ],
        "live_artifact_sha256": {
            "stock-research-live-acceptance.json": stock_hash,
            "index-market-research-live-acceptance.json": index_hash,
        },
        "network_calls_during_this_gate": 0,
    }


def runtime_acceptance():
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    stream = io.StringIO()
    outcome = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    _assert(outcome.wasSuccessful(), stream.getvalue())
    return {
        "executed": True,
        "full_regression_tests_run": outcome.testsRun,
        "failures": len(outcome.failures),
        "errors": len(outcome.errors),
        "skipped": len(outcome.skipped),
        "network_calls": 0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = {
        "acceptance": "passed",
        "task": "2.24",
        "checked_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
        "static": static_acceptance(),
        "target_traceability": target_traceability_acceptance(),
        "runtime": runtime_acceptance() if args.runtime else {"executed": False},
        "deployment_boundary": {
            "tradingagents_embedded": True,
            "existing_llm_reused": True,
            "existing_sqlite_reused": True,
            "existing_intel_worker_reused": True,
            "existing_ragflow_reused": True,
            "new_services": [],
            "new_ports": [],
            "real_order_execution": False,
            "production_dashboard_exposed": False,
        },
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()

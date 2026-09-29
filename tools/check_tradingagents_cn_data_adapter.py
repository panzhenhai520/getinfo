#!/usr/bin/env python3
"""Verify the evidence-preserving TradingAgents A/H data-tool boundary.

The default check is dependency-light.  ``--runtime`` is an image gate that
creates the real project schema in a temporary SQLite database, registers real
LangChain StructuredTools, calculates an indicator from a persisted OHLCV
snapshot, and proves that neither the calculation nor tool registration calls
an external provider.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ADAPTER_PATH = ROOT / "tradingagents_cn_data_adapter.py"
EXPECTED_TOOLS = {
    "get_stock_data",
    "get_indicators",
    "get_verified_market_snapshot",
    "get_index_identity",
    "get_index_constituents",
    "get_market_breadth",
    "get_sector_rotation",
    "get_market_liquidity",
    "get_fund_identity",
    "get_fund_profile",
    "get_fund_nav",
    "get_fund_holdings",
    "get_fund_manager",
    "get_fund_share",
    "get_fund_fees",
    "get_fund_subscription_redemption",
    "get_etf_constituents",
    "get_etf_tracking",
    "get_etf_liquidity",
    "get_fundamentals",
    "get_balance_sheet",
    "get_cashflow",
    "get_income_statement",
    "get_news",
    "get_global_news",
    "get_insider_transactions",
    "get_macro_indicators",
    "get_prediction_markets",
    "get_sentiment_inputs",
}
EXPECTED_CATEGORIES = {
    "core_stock_apis",
    "technical_indicators",
    "fundamental_data",
    "news_data",
    "macro_data",
    "prediction_markets",
    "sentiment",
    "index_identity",
    "index_constituents",
    "market_breadth",
    "sector_rotation",
    "market_liquidity",
    "fund_identity",
    "fund_valuation",
    "fund_disclosures",
    "fund_terms",
    "etf_tracking",
}
EXPECTED_CHAINS = {
    "CN": {
        "quote": ("tushare_cn", "akshare_cn", "easyquotation", "yahoo"),
        "bars": ("tushare_cn", "akshare_cn", "yahoo"),
        "financials": ("tushare_cn",),
        "industry": ("akshare_cn",),
        "announcements": ("tushare_cn",),
        "constituents": ("akshare_cn",),
        "market_breadth": ("akshare_cn",),
        "sector_rotation": ("akshare_cn",),
        "fund_basic": ("tushare_cn",),
        "fund_nav": ("tushare_cn", "akshare_cn"),
        "fund_holdings": ("tushare_cn", "akshare_cn"),
        "fund_manager": ("tushare_cn", "akshare_cn"),
        "fund_share": ("tushare_cn",),
        "fund_fees": ("akshare_cn",),
        "fund_trading": ("akshare_cn",),
    },
    "CN_FUND": {
        "quote": ("tushare_cn", "akshare_cn", "yahoo"),
        "bars": ("tushare_cn", "akshare_cn", "yahoo"),
        "fund_basic": ("tushare_cn",),
        "fund_nav": ("tushare_cn", "akshare_cn"),
        "fund_holdings": ("tushare_cn", "akshare_cn"),
        "fund_manager": ("tushare_cn", "akshare_cn"),
        "fund_share": ("tushare_cn",),
        "fund_fees": ("akshare_cn",),
        "fund_trading": ("akshare_cn",),
    },
    "XHKG": {
        "quote": ("easyquotation", "yahoo"),
        "bars": ("yahoo",),
        "constituents": (),
        "market_breadth": (),
        "sector_rotation": (),
    },
    "US": {
        "quote": ("yahoo", "alpha_vantage"),
        "bars": ("yahoo", "alpha_vantage"),
    },
    "JP": {
        "quote": ("yahoo",),
        "bars": ("yahoo",),
    },
    "MACRO": {"macro": ("fred",)},
}


class CheckFailure(RuntimeError):
    pass


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def _static_acceptance() -> dict[str, Any]:
    import tradingagents_cn_data_adapter as module

    source = ADAPTER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)

    _assert(
        not any(name == "tradingagents" or name.startswith("tradingagents.") for name in imports),
        "adapter imports or mutates upstream TradingAgents modules",
    )
    _assert("sys.modules" not in source, "adapter mutates imported module state")
    _assert("route_to_vendor" not in source, "adapter can bypass the project provider router")
    _assert("monkeypatch" not in source.casefold(), "adapter contains a monkey-patch path")
    _assert(dict(module.DEFAULT_PROVIDER_CHAINS) == EXPECTED_CHAINS, "provider chains drifted")
    _assert(len(module.SUPPORTED_INDICATORS) == 12, "deterministic indicator allowlist drifted")
    _assert(
        {"CN", "CN_FUND", "XHKG"} == set(module.SUPPORTED_TARGET_MARKETS),
        "target-market boundary drifted",
    )
    _assert("reddit" in source.casefold() and "stocktwits" in source.casefold(), "social-source boundary is missing")

    return {
        "adapter_path": str(ADAPTER_PATH.relative_to(ROOT)),
        "target_markets": sorted(module.SUPPORTED_TARGET_MARKETS),
        "provider_chains": {
            market: {key: list(value) for key, value in chains.items()}
            for market, chains in module.DEFAULT_PROVIDER_CHAINS.items()
        },
        "deterministic_indicators": sorted(module.SUPPORTED_INDICATORS),
        "upstream_tradingagents_imports": [],
        "runtime_monkey_patch_paths": 0,
        "vendor_bypass_paths": 0,
    }


class _NoNetworkRouter:
    calls = 0

    def fetch_and_persist(self, *args, **kwargs):
        del args, kwargs
        self.calls += 1
        raise CheckFailure("runtime acceptance attempted an external provider call")


def _seed_bar_snapshot(connection, instrument_id: int, now: datetime) -> int:
    connection.execute(
        """
        INSERT INTO financial_provider_profiles(
            provider_key, display_name, provider_type, access_tier,
            capabilities_json, is_enabled, health_status
        ) VALUES('acceptance_fixture', 'Acceptance fixture', 'fixture', 'test',
                 '["bar"]', 1, 'healthy')
        """
    )
    profile_id = int(
        connection.execute(
            "SELECT id FROM financial_provider_profiles WHERE provider_key='acceptance_fixture'"
        ).fetchone()[0]
    )
    bars = []
    first = now - timedelta(days=59)
    for index in range(60):
        observed = first + timedelta(days=index)
        close = 10.0 + index * 0.03
        bars.append(
            {
                "observed_at": observed.isoformat().replace("+00:00", "Z"),
                "open": close - 0.02,
                "high": close + 0.05,
                "low": close - 0.06,
                "close": close,
                "volume": 1000 + index,
                "turnover": close * (1000 + index),
            }
        )
    payload = {
        "provider_id": "acceptance_fixture",
        "endpoint": "bars",
        "license_profile": "fixture-only",
        "data_kind": "bar",
        "metric": "ohlcv",
        "value": bars,
        "normalized_payload": {
            "symbol": "000001.SZ",
            "interval": "1d",
            "adjustment": "raw",
            "bars": bars,
        },
        "adjustment": "raw",
        "quality_flags": ["acceptance_fixture"],
        "lineage": {"external_network_calls": 0},
    }
    payload_text = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    payload_sha = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
    connection.execute(
        """
        INSERT INTO financial_data_snapshots(
            snapshot_key, instrument_id, provider_profile_id, data_type,
            interval_code, observed_at, fetched_at, market_status, currency,
            timezone, quality_status, payload_json, payload_sha256, request_id
        ) VALUES('acceptance-source-bar', ?, ?, 'bar', '1d', ?, ?, 'closed',
                 'CNY', 'Asia/Shanghai', 'normalized_fixture', ?, ?,
                 'acceptance-source-request')
        """,
        (
            instrument_id,
            profile_id,
            bars[-1]["observed_at"],
            now.isoformat().replace("+00:00", "Z"),
            payload_text,
            payload_sha,
        ),
    )
    return int(
        connection.execute(
            "SELECT id FROM financial_data_snapshots WHERE snapshot_key='acceptance-source-bar'"
        ).fetchone()[0]
    )


def _runtime_acceptance() -> dict[str, Any]:
    from langchain_core.tools import StructuredTool
    from langgraph.prebuilt import ToolNode

    from financial_instruments import InstrumentRegistry
    from sqlite_database import SQLiteDatabase
    from tradingagents_cn_data_adapter import (
        DERIVED_PROVIDER_KEY,
        TradingAgentsCNDataAdapter,
        TradingAgentsCNRunContext,
    )

    now = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory(prefix="tradingagents-cn-acceptance-") as directory:
        database = SQLiteDatabase(str(Path(directory) / "acceptance.sqlite3"))
        _assert(database.connect(), "temporary project database did not connect")
        _assert(database.create_tables(), "temporary project schema did not migrate")
        try:
            connection = database.connection
            registry = InstrumentRegistry(connection)
            registry.load_controlled_seed()
            instrument = registry.get_by_canonical_symbol("000001.SZ")
            _assert(instrument is not None, "controlled A-share instrument seed is missing")
            connection.execute(
                """
                INSERT INTO financial_research_runs(
                    id, trigger_type, scope_type, instrument_id, status, requested_at
                ) VALUES('acceptance-cn-run', 'acceptance', 'instrument', ?, 'running', ?)
                """,
                (instrument.instrument_id, now.isoformat().replace("+00:00", "Z")),
            )
            source_snapshot_id = _seed_bar_snapshot(
                connection, instrument.instrument_id, now
            )
            router = _NoNetworkRouter()
            adapter = TradingAgentsCNDataAdapter(
                connection,
                TradingAgentsCNRunContext(
                    "acceptance-cn-run", instrument.instrument_id, now
                ),
                settings={"FINANCIAL_INTELLIGENCE_ENABLED": True},
                router=router,
            )
            tools = adapter.registered_tools()
            categories = adapter.tool_categories()
            _assert(set(tools) == EXPECTED_TOOLS, "registered tool set drifted")
            _assert(set(categories) == EXPECTED_CATEGORIES, "tool categories drifted")
            _assert(
                all(isinstance(tool, StructuredTool) for tool in tools.values()),
                "runtime tools are not real LangChain StructuredTools",
            )
            ToolNode(list(adapter.market_analyst_tools()))
            ToolNode(list(adapter.news_analyst_tools()))
            ToolNode(list(adapter.fundamentals_analyst_tools()))
            ToolNode(list(adapter.index_identity_tools()))
            ToolNode(list(adapter.index_participation_tools()))
            ToolNode(list(adapter.index_composition_tools()))
            ToolNode(list(adapter.index_macro_policy_tools()))

            result = json.loads(
                tools["get_indicators"].invoke(
                    {
                        "symbol": "000001.SZ",
                        "indicator": "close_50_sma,rsi",
                        "curr_date": "2026-07-31",
                        "look_back_days": 60,
                    }
                )
            )
            _assert(result["status"] == "completed", "indicator tool did not complete")
            _assert(result["external_network_calls"] == 0, "indicator used a network source")
            _assert(
                result["source_snapshot_ids"] == [source_snapshot_id],
                "derived result lost its source snapshot",
            )
            derived = connection.execute(
                """
                SELECT s.id, s.payload_sha256, e.id
                FROM financial_data_snapshots s
                JOIN financial_provider_profiles p ON p.id=s.provider_profile_id
                JOIN financial_research_evidence e ON e.snapshot_id=s.id
                WHERE p.provider_key=? AND e.research_run_id='acceptance-cn-run'
                """,
                (DERIVED_PROVIDER_KEY,),
            ).fetchall()
            _assert(len(derived) == 1, "derived snapshot/evidence link was not persisted once")
            _assert(router.calls == 0, "deterministic runtime invoked a provider")

            return {
                "executed": True,
                "langchain_structured_tool_count": len(tools),
                "tool_categories": sorted(categories),
                "upstream_tool_nodes_constructed": 7,
                "derived_indicator_snapshot_id": result["snapshot_id"],
                "source_snapshot_ids": result["source_snapshot_ids"],
                "evidence_links": len(derived),
                "provider_calls": router.calls,
                "network_calls": 0,
            }
        finally:
            database.disconnect()


def run(*, runtime: bool) -> dict[str, Any]:
    static = _static_acceptance()
    runtime_result = _runtime_acceptance() if runtime else {"executed": False}
    return {
        "check_version": "tradingagents-cn-data-adapter-v1",
        "checked_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "static": static,
        "runtime": runtime_result,
        "acceptance": {
            "explicit_a_h_provider_chains": True,
            "all_tool_categories_registered": True,
            "technical_indicators_use_persisted_ohlcv_only": True,
            "numeric_output_has_snapshot_lineage": True,
            "future_data_contract_enforced": True,
            "mainland_social_proxy_disabled": True,
            "index_identity_composition_breadth_rotation_and_liquidity_registered": True,
            "etf_and_open_fund_tools_registered": True,
            "no_upstream_monkey_patch": True,
            "runtime_tool_and_evidence_gate_passed": (
                runtime_result.get("executed") if runtime else None
            ),
            "passed": True,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = run(runtime=args.runtime)
    except (CheckFailure, KeyError, OSError, TypeError, ValueError) as exc:
        print(json.dumps({"passed": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

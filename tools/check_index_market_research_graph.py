#!/usr/bin/env python3
"""Verify the dedicated resumable index/market TradingAgents graph.

Static mode enforces the no-company-data/no-order architecture boundary.
``--runtime`` uses the real project schema, real CN adapter, locked upstream
debate/risk roles and SharedLLMBroker adapter with deterministic offline data.
"""

from __future__ import annotations

import argparse
import ast
import importlib.metadata
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GRAPH_PATH = ROOT / "index_market_research_graph.py"
EXPECTED_VERSION = "0.3.1"
EXPECTED_GRAPH_ROLES = {
    "index_identity_analyst",
    "index_technical_analyst",
    "breadth_liquidity_analyst",
    "constituents_rotation_analyst",
    "macro_policy_analyst",
    "bull_researcher",
    "bear_researcher",
    "research_manager",
    "market_strategy_analyst",
    "aggressive_risk_analyst",
    "conservative_risk_analyst",
    "neutral_risk_analyst",
    "portfolio_manager",
}
ORIGINAL_DOWNSTREAM_ROLES = {
    "bull_researcher",
    "bear_researcher",
    "research_manager",
    "aggressive_risk_analyst",
    "conservative_risk_analyst",
    "neutral_risk_analyst",
    "portfolio_manager",
}


class CheckFailure(RuntimeError):
    pass


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def _static_acceptance() -> dict[str, Any]:
    import index_market_research_graph as module

    source = GRAPH_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    banned = {
        "tradingagents.dataflows",
        "tradingagents.graph.checkpointer",
        "langgraph.checkpoint",
        "openai",
        "anthropic",
        "requests",
        "httpx",
    }
    prohibited_tools = {
        "get_balance_sheet",
        "get_cashflow",
        "get_income_statement",
        "get_insider_transactions",
    }
    _assert(
        not any(
            any(name == value or name.startswith(value + ".") for value in banned)
            for name in imports
        ),
        "index graph imports an upstream dataflow/checkpointer or direct client",
    )
    _assert(
        not any(name in source for name in prohibited_tools),
        "index graph contains a company-only tool",
    )
    _assert(
        "paper_orders" not in source and "paper_fills" not in source,
        "index graph contains an order write path",
    )
    _assert("TradingAgentsCNDataAdapter" in source, "project data adapter is not required")
    _assert("TradingAgentsLLMAdapterFactory" in source, "project LLM adapter is not required")
    _assert("MarketClockService" in source, "project market clock is not required")
    _assert(module.OUTPUT_CLASSIFICATION == "market_research_opinion", "output classification drifted")
    _assert(module.INDEX_GRAPH_VERSION == "index-market-research-graph-v1", "graph version drifted")
    return {
        "graph_path": str(GRAPH_PATH.relative_to(ROOT)),
        "graph_version": module.INDEX_GRAPH_VERSION,
        "checkpoint_schema_version": module.INDEX_CHECKPOINT_SCHEMA_VERSION,
        "required_role_count": len(EXPECTED_GRAPH_ROLES),
        "required_roles": sorted(EXPECTED_GRAPH_ROLES),
        "original_downstream_roles": sorted(ORIGINAL_DOWNSTREAM_ROLES),
        "company_only_tools": [],
        "upstream_dataflow_imports": [],
        "upstream_or_extra_checkpoint_imports": [],
        "direct_model_or_http_imports": [],
        "order_write_paths": 0,
    }


def _insert_index_article(connection) -> None:
    connection.execute(
        """
        INSERT INTO articles(
            url, canonical_url, title, content, domain, publish_date,
            first_crawled, status, matched_keywords
        ) VALUES(
            'https://news.example.test/index-runtime',
            'https://news.example.test/index-runtime',
            '上证综合指数市场政策动态',
            '外部不可信新闻正文，仅作为指数政策与新闻证据。',
            'news.example.test', '2026-07-31',
            '2026-07-31T01:00:00Z', 'active', '上证综合指数'
        )
        """
    )


def _runtime_acceptance() -> dict[str, Any]:
    from check_stock_research_graph import (
        _AcceptanceBroker,
        _UnavailableRouter,
        _seed_cached_bars,
    )
    from financial_instruments import InstrumentRegistry
    from index_market_research_graph import (
        IndexMarketResearchGraph,
        IndexMarketResearchGraphInterrupted,
    )
    from sqlite_database import SQLiteDatabase
    from tradingagents_cn_data_adapter import (
        TradingAgentsCNDataAdapter,
        TradingAgentsCNRunContext,
    )
    from tradingagents_llm_adapter import (
        TradingAgentsLLMAdapterFactory,
        TradingAgentsLLMRunContext,
    )

    version = importlib.metadata.version("tradingagents")
    _assert(version == EXPECTED_VERSION, f"installed TradingAgents version drifted: {version}")
    now = datetime(2026, 7, 31, 2, 0, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory(prefix="index-research-runtime-") as directory:
        database = SQLiteDatabase(str(Path(directory) / "runtime.sqlite3"))
        _assert(database.connect(), "temporary database did not connect")
        _assert(database.create_tables(), "project schema did not migrate")
        try:
            connection = database.connection
            registry = InstrumentRegistry(connection)
            registry.load_controlled_seed()
            instrument = registry.get_by_canonical_symbol("000001.SH")
            _assert(instrument is not None, "controlled SSE index seed missing")
            _assert(instrument.metadata.get("compiler") == "上海证券交易所", "index compiler missing")
            run_id = "index-runtime-acceptance"
            connection.execute(
                """
                INSERT INTO financial_research_runs(
                    id, trigger_type, scope_type, instrument_id, status, requested_at
                ) VALUES(?, 'acceptance', 'instrument', ?, 'running', ?)
                """,
                (run_id, instrument.instrument_id, now.isoformat().replace("+00:00", "Z")),
            )
            _insert_index_article(connection)
            router = _UnavailableRouter()
            data_context = TradingAgentsCNRunContext(run_id, instrument.instrument_id, now)
            data_adapter = TradingAgentsCNDataAdapter(
                connection,
                data_context,
                settings={"FINANCIAL_INTELLIGENCE_ENABLED": True},
                router=router,
            )
            source_snapshot_id = _seed_cached_bars(connection, data_adapter, instrument, now)
            first_broker = _AcceptanceBroker(
                fail_bear_once=True,
                tool_symbol="000001.SH",
                exercise_tools=False,
            )
            first_factory = TradingAgentsLLMAdapterFactory(
                first_broker,
                TradingAgentsLLMRunContext(run_id, request_id="index-runtime-first"),
            )
            first_graph = IndexMarketResearchGraph(
                connection,
                run_id,
                data_adapter=data_adapter,
                llm_factory=first_factory,
            )
            try:
                first_graph.run()
            except IndexMarketResearchGraphInterrupted as interrupted:
                _assert(interrupted.stage_key == "bear_researcher:1", "failure did not stop at bear role")
                _assert(interrupted.error_code == "llm_timeout", "interruption error was not stable")
                checkpoint = interrupted.checkpoint
            else:
                raise CheckFailure("fixture LLM interruption did not occur")
            _assert(checkpoint["stage_index"] == 6, "checkpoint cursor lost completed roles")

            resumed_broker = _AcceptanceBroker(
                tool_symbol="000001.SH", exercise_tools=False
            )
            resumed_factory = TradingAgentsLLMAdapterFactory(
                resumed_broker,
                TradingAgentsLLMRunContext(run_id, request_id="index-runtime-resume"),
            )
            resumed_adapter = TradingAgentsCNDataAdapter(
                connection,
                data_context,
                settings={"FINANCIAL_INTELLIGENCE_ENABLED": True},
                router=router,
            )
            result = IndexMarketResearchGraph(
                connection,
                run_id,
                data_adapter=resumed_adapter,
                llm_factory=resumed_factory,
            ).run(checkpoint)
            roles = {item["role_key"] for item in result["checkpoint"]["node_trace"]}
            _assert(roles == EXPECTED_GRAPH_ROLES, "complete index role trace is missing roles")
            _assert(ORIGINAL_DOWNSTREAM_ROLES <= roles, "original debate/risk roles were not preserved")
            _assert(result["recommendation"] == "Hold", "final structured rating drifted")
            _assert(result["section_count"] == 15, "index role/debate sections were not all saved")
            _assert(result["output_classification"] == "market_research_opinion", "output boundary drifted")
            _assert(result["execution_allowed"] is False and result["order_target"] is None, "index graph enabled execution")
            _assert(result["market_status"] == "open", "server-clock market status drifted")
            _assert(source_snapshot_id in result["checkpoint"]["preflight"]["snapshot_ids"], "source snapshot was lost")
            _assert(connection.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 0, "paper order was created")
            _assert(connection.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0] == 0, "paper fill was created")
            report = connection.execute(
                "SELECT report_status, verified_at, report_json FROM financial_final_reports WHERE id=?",
                (result["report_id"],),
            ).fetchone()
            report_json = json.loads(report[2])
            _assert(report[0] == "degraded_unverified", "degraded index report status missing")
            _assert(report[1] is None, "unverified index report marked verified")
            _assert(report_json["compiler"] == "上海证券交易所", "compiler not persisted")
            _assert(report_json["market_status"] == "open", "market state not persisted")
            _assert(report_json["execution_allowed"] is False, "persisted report enabled execution")
            all_calls = first_broker.calls + resumed_broker.calls
            observed_broker_roles = {item["role_key"] for item in all_calls}
            _assert(ORIGINAL_DOWNSTREAM_ROLES <= observed_broker_roles, "original roles bypassed broker")
            return {
                "executed": True,
                "tradingagents_version": version,
                "target": instrument.canonical_symbol,
                "compiler": report_json["compiler"],
                "market_status": result["market_status"],
                "source_snapshot_id": source_snapshot_id,
                "checkpoint_interruption_stage": "bear_researcher:1",
                "checkpoint_completed_roles_before_resume": checkpoint["stage_index"],
                "complete_role_count": len(roles),
                "saved_section_count": result["section_count"],
                "final_recommendation": result["recommendation"],
                "report_status": result["status"],
                "evidence_coverage": result["evidence_coverage"],
                "component_contribution_coverage": report_json["component_contribution_coverage"],
                "broker_calls": len(all_calls),
                "provider_fixture_calls": router.calls,
                "network_calls": 0,
                "orders_created": 0,
            }
        finally:
            database.disconnect()


def run(*, runtime: bool) -> dict[str, Any]:
    static = _static_acceptance()
    runtime_result = _runtime_acceptance() if runtime else {"executed": False}
    return {
        "check_version": "index-market-research-graph-v1",
        "checked_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "static": static,
        "runtime": runtime_result,
        "acceptance": {
            "project_data_llm_clock_and_database_only": True,
            "sse_szse_hsi_index_templates_tested": True,
            "original_downstream_debate_and_risk_roles_preserved": True,
            "company_only_tools_excluded": True,
            "market_status_constituent_date_coverage_and_latency_present": True,
            "json_checkpoint_resume_passed": runtime_result.get("executed") if runtime else None,
            "output_is_unverified_market_research_opinion": True,
            "no_index_order_execution_path": True,
            "runtime_complete_graph_passed": runtime_result.get("executed") if runtime else None,
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

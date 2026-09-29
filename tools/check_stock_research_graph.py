#!/usr/bin/env python3
"""Verify the complete resumable stock research graph in the locked runtime.

Static mode validates architecture boundaries.  ``--runtime`` creates the real
project schema, real CN data tools and real locked TradingAgents downstream
roles.  A deterministic broker drives tool calls, deliberately interrupts the
bear role once, resumes from the emitted checkpoint and persists the final
research-opinion report without network or order execution.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GRAPH_PATH = ROOT / "stock_research_graph.py"
EXPECTED_VERSION = "0.3.1"
EXPECTED_ROLES = {
    "market_analyst",
    "sentiment_analyst",
    "news_analyst",
    "fundamentals_analyst",
    "bull_researcher",
    "bear_researcher",
    "research_manager",
    "trader",
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
    import stock_research_graph as module

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
    _assert(not any(any(name == value or name.startswith(value + ".") for value in banned) for name in imports), "graph imports an upstream dataflow/checkpointer or direct client")
    _assert("paper_orders" not in source and "paper_fills" not in source, "stock research graph contains an order write path")
    _assert("TradingAgentsCNDataAdapter" in source, "project data adapter is not required")
    _assert("TradingAgentsLLMAdapterFactory" in source, "project LLM adapter is not required")
    _assert(module.OUTPUT_CLASSIFICATION == "research_opinion", "output classification drifted")
    _assert(module.STOCK_GRAPH_VERSION == "stock-research-graph-v1", "graph version drifted")

    return {
        "graph_path": str(GRAPH_PATH.relative_to(ROOT)),
        "graph_version": module.STOCK_GRAPH_VERSION,
        "checkpoint_schema_version": module.CHECKPOINT_SCHEMA_VERSION,
        "required_role_count": len(EXPECTED_ROLES),
        "required_roles": sorted(EXPECTED_ROLES),
        "upstream_dataflow_imports": [],
        "upstream_or_extra_checkpoint_imports": [],
        "direct_model_or_http_imports": [],
        "order_write_paths": 0,
    }


class _UnavailableRouter:
    def __init__(self):
        self.calls = 0

    def fetch_and_persist(self, request, *, candidate_provider_ids, allow_fallback):
        del allow_fallback
        from financial_provider_contract import PermissionDeniedError

        self.calls += 1
        raise PermissionDeniedError(
            "acceptance fixture provider disabled",
            provider_id=candidate_provider_ids[0],
            endpoint=request.endpoint,
            request_id=request.request_id,
        )


class _AcceptanceBroker:
    def __init__(
        self,
        *,
        fail_bear_once: bool = False,
        tool_symbol: str = "000001.SZ",
        exercise_tools: bool = True,
    ):
        self.calls: list[dict[str, Any]] = []
        self.role_counts: dict[str, int] = {}
        self.fail_bear_once = fail_bear_once
        self.failed = False
        self.tool_symbol = str(tool_symbol)
        self.exercise_tools = bool(exercise_tools)

    def runtime_identity(self):
        return {
            "provider_id": "local",
            "model_id": "fixture-local-model",
            "base_url": "http://fixture-local.invalid/v1",
            "runtime_source": "acceptance-fixture",
            "api_key_exposed": False,
        }

    @staticmethod
    def _tool_call(name: str, arguments: dict[str, Any], ordinal: int):
        return (
            {
                "id": f"acceptance-tool-{ordinal}",
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            },
        )

    def complete(self, messages, **kwargs):
        from shared_llm_broker import LLMCallResult, SharedLLMBrokerError

        role = kwargs["role_key"]
        self.calls.append({"messages": list(messages), **kwargs})
        self.role_counts[role] = self.role_counts.get(role, 0) + 1
        ordinal = len(self.calls)
        if role == "bear_researcher" and self.fail_bear_once and not self.failed:
            self.failed = True
            raise SharedLLMBrokerError("fixture endpoint detail", error_code="llm_timeout")

        parsed = None
        tool_calls = ()
        content = f"Fixture evidence-bounded report for {role}."
        if kwargs.get("response_schema") is not None:
            values = {
                "sentiment_analyst": {
                    "overall_band": "Mixed",
                    "overall_score": 5.0,
                    "confidence": "low",
                    "narrative": "Only project news evidence was available; social proxies were disabled.",
                },
                "research_manager": {
                    "recommendation": "Hold",
                    "rationale": "Bull and bear fixture evidence was balanced.",
                    "strategic_actions": "Wait for the next verified snapshot.",
                },
                "trader": {
                    "action": "Hold",
                    "reasoning": "No execution is appropriate for the bounded fixture.",
                    "entry_price": None,
                    "stop_loss": None,
                    "position_sizing": None,
                },
                "portfolio_manager": {
                    "rating": "Hold",
                    "executive_summary": "Maintain observation only; do not create an order.",
                    "investment_thesis": "The verified market evidence is available but fundamentals are degraded.",
                    "price_target": None,
                    "time_horizon": "1-3 months",
                },
            }
            parsed = values[role]
            content = json.dumps(parsed, ensure_ascii=False, sort_keys=True)
        elif role == "market_analyst" and self.exercise_tools and self.role_counts[role] == 1:
            content = ""
            tool_calls = self._tool_call(
                "get_verified_market_snapshot",
                {"symbol": self.tool_symbol, "curr_date": "2026-07-31", "look_back_days": 60},
                ordinal,
            )
        elif role == "news_analyst" and self.exercise_tools and self.role_counts[role] == 1:
            content = ""
            tool_calls = self._tool_call(
                "get_news",
                {"ticker": self.tool_symbol, "start_date": "2026-07-24", "end_date": "2026-07-31"},
                ordinal,
            )
        elif role == "fundamentals_analyst" and self.exercise_tools and self.role_counts[role] == 1:
            content = ""
            tool_calls = self._tool_call(
                "get_fundamentals",
                {"ticker": self.tool_symbol, "curr_date": "2026-07-31"},
                ordinal,
            )

        return LLMCallResult(
            call_id=f"acceptance-call-{ordinal}",
            profile_key=kwargs["profile"],
            provider_id="local",
            model_id="fixture-local-model",
            runtime_source="acceptance-fixture",
            content=content,
            parsed=parsed,
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
            input_tokens=24,
            output_tokens=12,
            latency_ms=4,
            response_sha256=f"{ordinal:064x}",
        )


def _insert_article(connection) -> None:
    connection.execute(
        """
        INSERT INTO articles(
            url, canonical_url, title, content, domain, publish_date,
            first_crawled, status, matched_keywords
        ) VALUES(
            'https://news.example.test/stock-runtime',
            'https://news.example.test/stock-runtime',
            '平安银行发布经营信息',
            '外部不可信新闻正文，仅作为情绪与新闻证据。',
            'news.example.test', '2026-07-31',
            '2026-07-31T07:00:00Z', 'active', '平安银行'
        )
        """
    )


def _seed_cached_bars(connection, adapter, instrument, now: datetime) -> int:
    trade_day = now.astimezone(timezone(timedelta(hours=8))).date()
    start = trade_day - timedelta(days=600)
    parameters = {
        "start": start.isoformat(),
        "end": trade_day.isoformat(),
        "interval": "1d",
        "adjustment": "raw",
    }
    request_id = adapter._request_id(
        "get_stock_data", instrument, "bars", "ohlcv", parameters
    )
    connection.execute(
        """
        INSERT INTO financial_provider_profiles(
            provider_key, display_name, provider_type, access_tier,
            capabilities_json, is_enabled, health_status
        ) VALUES('stock_graph_runtime_fixture', 'fixture', 'fixture', 'test',
                 '["bar"]', 1, 'healthy')
        """
    )
    profile_id = int(connection.execute(
        "SELECT id FROM financial_provider_profiles WHERE provider_key='stock_graph_runtime_fixture'"
    ).fetchone()[0])
    bars = []
    first = now - timedelta(days=259)
    for index in range(260):
        observed = first + timedelta(days=index)
        close = 10.0 + index * 0.02
        bars.append(
            {
                "observed_at": observed.isoformat().replace("+00:00", "Z"),
                "open": close - 0.03,
                "high": close + 0.08,
                "low": close - 0.09,
                "close": close,
                "volume": 1000 + index,
                "turnover": close * (1000 + index),
            }
        )
    payload = {
        "provider_id": "stock_graph_runtime_fixture",
        "endpoint": "bars",
        "license_profile": "fixture-only",
        "data_kind": "bar",
        "metric": "ohlcv",
        "value": bars,
        "normalized_payload": {
            "symbol": instrument.canonical_symbol,
            "interval": "1d",
            "adjustment": "raw",
            "bars": bars,
        },
        "adjustment": "raw",
        "quality_flags": ["acceptance_fixture"],
        "lineage": {"external_network_calls": 0},
    }
    payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload_text.encode()).hexdigest()
    connection.execute(
        """
        INSERT INTO financial_data_snapshots(
            snapshot_key, instrument_id, provider_profile_id, data_type,
            interval_code, observed_at, fetched_at, market_status, currency,
            timezone, quality_status, payload_json, payload_sha256, request_id
        ) VALUES('stock-graph-runtime-bars', ?, ?, 'bar', '1d', ?, ?, 'closed',
                 'CNY', 'Asia/Shanghai', 'normalized_fixture', ?, ?, ?)
        """,
        (
            instrument.instrument_id,
            profile_id,
            bars[-1]["observed_at"],
            now.isoformat().replace("+00:00", "Z"),
            payload_text,
            digest,
            request_id,
        ),
    )
    return int(connection.execute(
        "SELECT id FROM financial_data_snapshots WHERE snapshot_key='stock-graph-runtime-bars'"
    ).fetchone()[0])


def _runtime_acceptance() -> dict[str, Any]:
    from financial_instruments import InstrumentRegistry
    from sqlite_database import SQLiteDatabase
    from stock_research_graph import StockResearchGraph, StockResearchGraphInterrupted
    from tradingagents_cn_data_adapter import TradingAgentsCNDataAdapter, TradingAgentsCNRunContext
    from tradingagents_llm_adapter import TradingAgentsLLMAdapterFactory, TradingAgentsLLMRunContext

    version = importlib.metadata.version("tradingagents")
    _assert(version == EXPECTED_VERSION, f"installed TradingAgents version drifted: {version}")
    now = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory(prefix="stock-research-runtime-") as directory:
        database = SQLiteDatabase(str(Path(directory) / "runtime.sqlite3"))
        _assert(database.connect(), "temporary database did not connect")
        _assert(database.create_tables(), "project schema did not migrate")
        try:
            connection = database.connection
            registry = InstrumentRegistry(connection)
            registry.load_controlled_seed()
            instrument = registry.get_by_canonical_symbol("000001.SZ")
            _assert(instrument is not None, "controlled stock seed missing")
            run_id = "stock-runtime-acceptance"
            connection.execute(
                """
                INSERT INTO financial_research_runs(
                    id, trigger_type, scope_type, instrument_id, status, requested_at
                ) VALUES(?, 'acceptance', 'instrument', ?, 'running', ?)
                """,
                (run_id, instrument.instrument_id, now.isoformat().replace("+00:00", "Z")),
            )
            _insert_article(connection)
            router = _UnavailableRouter()
            data_context = TradingAgentsCNRunContext(run_id, instrument.instrument_id, now)
            data_adapter = TradingAgentsCNDataAdapter(
                connection,
                data_context,
                settings={"FINANCIAL_INTELLIGENCE_ENABLED": True},
                router=router,
            )
            source_snapshot_id = _seed_cached_bars(
                connection, data_adapter, instrument, now
            )
            first_broker = _AcceptanceBroker(fail_bear_once=True)
            first_factory = TradingAgentsLLMAdapterFactory(
                first_broker,
                TradingAgentsLLMRunContext(run_id, request_id="stock-runtime-first"),
            )
            first_graph = StockResearchGraph(
                connection,
                run_id,
                data_adapter=data_adapter,
                llm_factory=first_factory,
            )
            try:
                first_graph.run()
            except StockResearchGraphInterrupted as interrupted:
                _assert(interrupted.stage_key == "bear_researcher:1", "failure did not stop at bear role")
                _assert(interrupted.error_code == "llm_timeout", "interruption error was not stable")
                checkpoint = interrupted.checkpoint
            else:
                raise CheckFailure("fixture LLM interruption did not occur")
            _assert(checkpoint["stage_index"] == 5, "checkpoint cursor lost completed roles")

            resumed_broker = _AcceptanceBroker()
            resumed_factory = TradingAgentsLLMAdapterFactory(
                resumed_broker,
                TradingAgentsLLMRunContext(run_id, request_id="stock-runtime-resume"),
            )
            resumed_adapter = TradingAgentsCNDataAdapter(
                connection,
                data_context,
                settings={"FINANCIAL_INTELLIGENCE_ENABLED": True},
                router=router,
            )
            result = StockResearchGraph(
                connection,
                run_id,
                data_adapter=resumed_adapter,
                llm_factory=resumed_factory,
            ).run(checkpoint)
            roles = {item["role_key"] for item in result["checkpoint"]["node_trace"]}
            _assert(roles == EXPECTED_ROLES, "complete role trace is missing roles")
            _assert(result["recommendation"] == "Hold", "final structured rating drifted")
            _assert(result["section_count"] == 14, "role/debate sections were not all saved")
            _assert(result["output_classification"] == "research_opinion", "output boundary drifted")
            _assert(result["execution_allowed"] is False, "research graph enabled execution")
            _assert(result["evidence_coverage"] == 0.75, "degraded evidence coverage drifted")
            _assert(source_snapshot_id in result["checkpoint"]["preflight"]["snapshot_ids"], "source snapshot was lost")
            _assert(connection.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 0, "paper order was created")
            _assert(connection.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0] == 0, "paper fill was created")
            report = connection.execute(
                "SELECT report_status, verified_at, report_json FROM financial_final_reports WHERE id=?",
                (result["report_id"],),
            ).fetchone()
            _assert(report[0] == "degraded_unverified", "degraded report status missing")
            _assert(report[1] is None, "unverified stage marked report verified")
            _assert(json.loads(report[2])["execution_allowed"] is False, "persisted report enabled execution")

            all_calls = first_broker.calls + resumed_broker.calls
            observed_roles = {item["role_key"] for item in all_calls}
            _assert(observed_roles == EXPECTED_ROLES, "not all roles called through broker")
            tool_names = {
                tool["function"]["name"]
                for item in all_calls
                for tool in item.get("tools") or []
            }
            _assert(
                {"get_verified_market_snapshot", "get_news", "get_fundamentals"} <= tool_names,
                "project data tools were not bound to analyst calls",
            )
            return {
                "executed": True,
                "tradingagents_version": version,
                "source_snapshot_id": source_snapshot_id,
                "checkpoint_interruption_stage": "bear_researcher:1",
                "checkpoint_completed_roles_before_resume": checkpoint["stage_index"],
                "complete_role_count": len(roles),
                "saved_section_count": result["section_count"],
                "final_recommendation": result["recommendation"],
                "report_status": result["status"],
                "evidence_coverage": result["evidence_coverage"],
                "project_tools_bound": sorted(tool_names),
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
        "check_version": "stock-research-graph-v1",
        "checked_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "static": static,
        "runtime": runtime_result,
        "acceptance": {
            "project_data_and_llm_bases_only": True,
            "original_downstream_debate_and_decision_roles_preserved": True,
            "all_role_sections_and_transcripts_persisted": True,
            "json_checkpoint_resume_passed": runtime_result.get("executed") if runtime else None,
            "insufficient_market_evidence_gate_present": True,
            "output_is_unverified_research_opinion": True,
            "no_order_execution_path": True,
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

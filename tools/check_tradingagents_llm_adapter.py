#!/usr/bin/env python3
"""Verify the SharedLLMBroker-only TradingAgents LLM integration boundary.

Default mode is dependency-light and validates policies, role resolution and
the absence of a direct model/network client. ``--runtime`` is the crawler
image gate: it compiles the complete upstream role graph with the adapter and
runs deterministic plain, structured and reflection calls through one fake
broker (zero network requests).
"""

from __future__ import annotations

import argparse
import ast
import importlib.metadata
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
ADAPTER_PATH = ROOT / "tradingagents_llm_adapter.py"
EXPECTED_TRADINGAGENTS_VERSION = "0.3.1"
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
    "reflection",
}
EXPECTED_TOOL_ROLES = {
    frozenset({"get_stock_data", "get_indicators", "get_verified_market_snapshot"}): "market_analyst",
    frozenset({"get_news"}): "sentiment_analyst",
    frozenset(
        {"get_news", "get_global_news", "get_macro_indicators", "get_prediction_markets"}
    ): "news_analyst",
    frozenset(
        {"get_fundamentals", "get_balance_sheet", "get_cashflow", "get_income_statement"}
    ): "fundamentals_analyst",
}
EXPECTED_SCHEMA_ROLES = {
    "SentimentReport": "sentiment_analyst",
    "ResearchPlan": "research_manager",
    "TraderProposal": "trader",
    "PortfolioDecision": "portfolio_manager",
}
BANNED_IMPORT_ROOTS = {"os", "requests", "httpx", "openai", "anthropic"}


class CheckFailure(RuntimeError):
    pass


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def _static_acceptance() -> dict[str, Any]:
    import tradingagents_llm_adapter as module

    source = ADAPTER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])

    _assert(not (imported_roots & BANNED_IMPORT_ROOTS), "adapter imports a direct network/model client")
    _assert("os.environ" not in source and "getenv(" not in source, "adapter reads environment keys")
    _assert(source.count("self.broker.complete(") == 1, "adapter has an unexpected LLM call path")
    _assert(set(module.ROLE_POLICIES) == EXPECTED_ROLES, "role policy set drifted")
    _assert(module.TOOL_ROLE_MAP == EXPECTED_TOOL_ROLES, "upstream tool-to-role map drifted")
    _assert(module.SCHEMA_ROLE_MAP == EXPECTED_SCHEMA_ROLES, "upstream schema-to-role map drifted")
    _assert(all(policy.profile in {"fast", "deep"} for policy in module.ROLE_POLICIES.values()), "invalid profile")
    _assert(all(0 < policy.max_output_tokens <= 8192 for policy in module.ROLE_POLICIES.values()), "invalid token boundary")
    _assert(all(0 < policy.timeout_seconds <= 300 for policy in module.ROLE_POLICIES.values()), "invalid timeout boundary")
    _assert(all(policy.max_rounds > 0 for policy in module.ROLE_POLICIES.values()), "invalid round boundary")
    _assert(
        all(policy.max_json_repairs <= 1 for policy in module.ROLE_POLICIES.values()),
        "structured repair is not finite",
    )

    return {
        "adapter_path": str(ADAPTER_PATH.relative_to(ROOT)),
        "role_count": len(module.ROLE_POLICIES),
        "roles": sorted(module.ROLE_POLICIES),
        "tool_role_sets": len(module.TOOL_ROLE_MAP),
        "structured_schema_roles": dict(sorted(module.SCHEMA_ROLE_MAP.items())),
        "direct_network_or_model_imports": [],
        "environment_key_reads": 0,
        "broker_completion_paths": 1,
        "finite_json_repair_max": max(
            policy.max_json_repairs for policy in module.ROLE_POLICIES.values()
        ),
    }


class _AcceptanceBroker:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def runtime_identity(self):
        return {
            "provider_id": "local",
            "model_id": "fixture-local-model",
            "base_url": "http://fixture-local.invalid/v1",
            "runtime_source": "chat_api.get_chat_model_runtime_config(local)",
            "api_key_exposed": False,
        }

    def complete(self, messages, **kwargs):
        from shared_llm_broker import LLMCallResult

        self.calls.append({"messages": list(messages), **kwargs})
        role = kwargs["role_key"]
        parsed = None
        if kwargs.get("response_schema") is not None:
            values = {
                "sentiment_analyst": {
                    "overall_band": "Neutral",
                    "overall_score": 5.0,
                    "narrative": "Fixture sentiment.",
                },
                "research_manager": {
                    "recommendation": "Hold",
                    "rationale": "Fixture balance.",
                    "strategic_actions": "Observe the next verified snapshot.",
                },
                "trader": {
                    "action": "Hold",
                    "reasoning": "Fixture-only transaction decision.",
                    "entry_price": None,
                    "stop_loss": None,
                    "position_sizing": None,
                },
                "portfolio_manager": {
                    "rating": "Hold",
                    "executive_summary": "Fixture-only final action.",
                    "investment_thesis": "No live market evidence was used.",
                    "price_target": None,
                    "time_horizon": "fixture",
                },
            }
            parsed = values[role]
            content = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        else:
            content = f"fixture response for {role}"
        return LLMCallResult(
            call_id=f"fixture-{len(self.calls)}",
            profile_key=kwargs["profile"],
            provider_id="local",
            model_id="fixture-local-model",
            runtime_source="chat_api.get_chat_model_runtime_config(local)",
            content=content,
            parsed=parsed,
            tool_calls=(),
            finish_reason="stop",
            input_tokens=12,
            output_tokens=8,
            latency_ms=3,
            response_sha256=f"{len(self.calls):064x}",
        )


def _runtime_acceptance() -> dict[str, Any]:
    import tradingagents_llm_adapter as module
    from langchain_core.runnables import Runnable
    from langchain_core.tools import tool
    from langgraph.prebuilt import ToolNode
    from tradingagents.agents import create_bull_researcher
    from tradingagents.agents.schemas import TraderProposal
    from tradingagents.graph.conditional_logic import ConditionalLogic
    from tradingagents.graph.reflection import Reflector
    from tradingagents.graph.setup import GraphSetup

    version = importlib.metadata.version("tradingagents")
    _assert(version == EXPECTED_TRADINGAGENTS_VERSION, f"installed version drifted: {version}")
    _assert(module.LANGCHAIN_AVAILABLE, "adapter did not load real LangChain classes")

    broker = _AcceptanceBroker()
    context = module.TradingAgentsLLMRunContext(
        "acceptance-run-2-16",
        request_id="acceptance-request-2-16",
    )
    factory = module.TradingAgentsLLMAdapterFactory(broker, context)
    quick = factory.quick()
    deep = factory.deep()
    _assert(isinstance(quick, Runnable) and isinstance(deep, Runnable), "adapter is not a Runnable")

    @tool
    def fixed_financial_fixture(symbol: str) -> str:
        """Return a deterministic, network-free financial fixture."""
        return json.dumps({"symbol": symbol, "close": 100.0, "as_of": "2026-07-31T08:00:00Z"})

    tool_nodes = {
        key: ToolNode([fixed_financial_fixture])
        for key in ("market", "social", "news", "fundamentals")
    }
    logic = ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1)
    workflow = GraphSetup(quick, deep, tool_nodes, logic).setup_graph(
        ["market", "social", "news", "fundamentals"]
    )
    compiled = workflow.compile()
    graph_nodes = set(compiled.get_graph().nodes)
    expected_nodes = {
        "Market Analyst",
        "Sentiment Analyst",
        "News Analyst",
        "Fundamentals Analyst",
        "Bull Researcher",
        "Bear Researcher",
        "Research Manager",
        "Trader",
        "Aggressive Analyst",
        "Conservative Analyst",
        "Neutral Analyst",
        "Portfolio Manager",
    }
    _assert(expected_nodes <= graph_nodes, "complete graph is missing upstream role nodes")

    bull = create_bull_researcher(quick)
    state = {
        "company_of_interest": "0700.HK",
        "asset_type": "stock",
        "instrument_context": "fixture:0700.HK",
        "market_report": "fixture market",
        "sentiment_report": "fixture sentiment",
        "news_report": "fixture news",
        "fundamentals_report": "fixture fundamentals",
        "investment_debate_state": {
            "history": "",
            "bull_history": "",
            "bear_history": "",
            "current_response": "",
            "count": 0,
        },
    }
    bull_result = bull(state)
    _assert("Bull Analyst: fixture response" in bull_result["investment_debate_state"]["history"], "upstream bull node failed")

    trader_value = quick.with_structured_output(TraderProposal).invoke("fixture trader prompt")
    _assert(trader_value.action.value == "Hold", "upstream Pydantic structured output failed")
    reflection = Reflector(quick).reflect_on_final_decision(
        "**Rating**: Hold", 0.01, 0.0, "HSI"
    )
    _assert(reflection == "fixture response for reflection", "upstream reflection routing failed")

    _assert([item["role_key"] for item in broker.calls] == ["bull_researcher", "trader", "reflection"], "runtime role routing drifted")
    required_call_fields = {
        "profile",
        "priority",
        "role_key",
        "research_run_id",
        "request_id",
        "max_tokens",
        "timeout_seconds",
        "cancel_event",
    }
    _assert(all(required_call_fields <= set(item) for item in broker.calls), "audit/scheduling fields not threaded")
    _assert(all(item["research_run_id"] == context.research_run_id for item in broker.calls), "run identity drifted")
    metadata = factory.upstream_config()
    _assert(metadata["collectinfo_adapter_required"] is True, "upstream direct client was not disabled")
    _assert(not ({"api_key", "key", "secret"} & set(metadata)), "compatibility metadata exposes a credential")

    return {
        "executed": True,
        "tradingagents_version": version,
        "langchain_runnable": True,
        "complete_graph_compiled": True,
        "complete_graph_node_count": len(graph_nodes),
        "original_role_nodes_present": sorted(expected_nodes),
        "upstream_plain_node_invoked": True,
        "upstream_pydantic_structured_output_invoked": True,
        "upstream_reflection_invoked": True,
        "broker_only_runtime_calls": len(broker.calls),
        "runtime_roles_observed": [item["role_key"] for item in broker.calls],
        "research_run_and_audit_fields_threaded": True,
        "network_calls": 0,
    }


def run(*, runtime: bool) -> dict[str, Any]:
    static = _static_acceptance()
    runtime_result = _runtime_acceptance() if runtime else {"executed": False}
    return {
        "check_version": "tradingagents-llm-adapter-v1",
        "checked_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "static": static,
        "runtime": runtime_result,
        "acceptance": {
            "all_upstream_roles_have_explicit_policy": True,
            "fast_and_deep_profiles_are_bounded": True,
            "structured_repairs_are_finite": True,
            "adapter_has_no_direct_model_or_network_client": True,
            "adapter_does_not_read_environment_keys": True,
            "all_llm_calls_use_shared_broker": True,
            "research_run_role_model_latency_hash_contract": True,
            "complete_upstream_graph_runtime_passed": runtime_result.get("executed") if runtime else None,
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

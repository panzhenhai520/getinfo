import ast
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import tradingagents_llm_adapter as adapter_module
from intel_llm_client import IntelLLMClient
from shared_llm_broker import LLMCallResult, SharedLLMBroker, SharedLLMBrokerError
from sqlite_database import SQLiteDatabase
from tradingagents_llm_adapter import (
    ROLE_POLICIES,
    BrokerBackedTradingAgentsLLM,
    TradingAgentsLLMAdapterError,
    TradingAgentsLLMAdapterFactory,
    TradingAgentsLLMRunContext,
)


RUNTIME = {
    "provider_id": "local",
    "name": "项目本地模型",
    "type": "openai",
    "base_url": "http://local-llm.example/v1",
    "api_key": "adapter-test-secret-key",
    "model_id": "project-local-model",
    "use_proxy": False,
}


class _Response:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


def _body(content="OK", *, tool_calls=None):
    message = {"content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3},
    }


def _result(*, content="OK", parsed=None, tool_calls=()):
    return LLMCallResult(
        call_id="call-1",
        profile_key="fast",
        provider_id="local",
        model_id="project-local-model",
        runtime_source="chat_api.get_chat_model_runtime_config(local)",
        content=content,
        parsed=parsed,
        tool_calls=tuple(tool_calls),
        finish_reason="stop",
        input_tokens=11,
        output_tokens=3,
        latency_ms=17,
        response_sha256="a" * 64,
    )


class _FakeBroker:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append({"messages": list(messages), **kwargs})
        if self.responses:
            value = self.responses.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value
        parsed = {} if kwargs.get("response_schema") is not None else None
        return _result(content="{}" if parsed is not None else "OK", parsed=parsed)

    def runtime_identity(self):
        return {
            "provider_id": "local",
            "model_id": RUNTIME["model_id"],
            "base_url": RUNTIME["base_url"],
            "runtime_source": "chat_api.get_chat_model_runtime_config(local)",
            "api_key_exposed": False,
        }


def _tools(*names):
    return [
        {
            "name": name,
            "description": f"Tool {name}",
            "parameters": {"type": "object", "properties": {}},
        }
        for name in names
    ]


def _schema(name):
    return {
        "title": name,
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    }


class TradingAgentsLLMAdapterTest(unittest.TestCase):
    def _factory(self, broker=None, **context_kwargs):
        actual = broker or _FakeBroker()
        context = TradingAgentsLLMRunContext(
            research_run_id="research-run-1",
            request_id="request-1",
            **context_kwargs,
        )
        return TradingAgentsLLMAdapterFactory(actual, context), actual, context

    def test_all_upstream_roles_have_bounded_profiles_and_route_only_to_broker(self):
        factory, broker, context = self._factory()

        for role, policy in ROLE_POLICIES.items():
            llm = factory.quick() if policy.profile == "fast" else factory.deep()
            message = llm.for_role(role).invoke("bounded role request")
            call = broker.calls[-1]
            self.assertEqual(message.response_metadata["role_key"], role)
            self.assertEqual(call["role_key"], role)
            self.assertEqual(call["research_run_id"], "research-run-1")
            self.assertEqual(call["profile"], policy.profile)
            self.assertEqual(call["max_tokens"], policy.max_output_tokens)
            self.assertEqual(call["timeout_seconds"], policy.timeout_seconds)
            self.assertEqual(call["priority"], "interactive_research")
            self.assertIs(call["cancel_event"], context.cancel_event)

        self.assertEqual(set(context.role_counts()), set(ROLE_POLICIES))
        self.assertEqual(len(broker.calls), len(ROLE_POLICIES))

    def test_upstream_tool_sets_resolve_each_analyst_deterministically(self):
        factory, broker, _ = self._factory()
        cases = {
            "market_analyst": (
                "get_stock_data",
                "get_indicators",
                "get_verified_market_snapshot",
            ),
            "news_analyst": (
                "get_news",
                "get_global_news",
                "get_macro_indicators",
                "get_prediction_markets",
            ),
            "fundamentals_analyst": (
                "get_fundamentals",
                "get_balance_sheet",
                "get_cashflow",
                "get_income_statement",
            ),
        }
        for role, names in cases.items():
            factory.quick().bind_tools(_tools(*names)).invoke("upstream prompt")
            self.assertEqual(broker.calls[-1]["role_key"], role)
            self.assertEqual(
                {tool["function"]["name"] for tool in broker.calls[-1]["tools"]},
                set(names),
            )

    def test_upstream_structured_schemas_resolve_quick_and_deep_roles(self):
        factory, broker, _ = self._factory()
        cases = (
            (factory.quick(), "SentimentReport", "sentiment_analyst"),
            (factory.deep(), "ResearchPlan", "research_manager"),
            (factory.quick(), "TraderProposal", "trader"),
            (factory.deep(), "PortfolioDecision", "portfolio_manager"),
        )
        for llm, schema_name, role in cases:
            self.assertEqual(llm.with_structured_output(_schema(schema_name)).invoke("prompt"), {})
            self.assertEqual(broker.calls[-1]["role_key"], role)
            self.assertEqual(broker.calls[-1]["response_schema"]["title"], schema_name)

    def test_plain_fallback_prompts_resolve_every_non_tool_role(self):
        factory, broker, _ = self._factory()
        cases = (
            (factory.quick(), "You are a Bull Analyst advocating", "bull_researcher"),
            (factory.quick(), "You are a Bear Analyst making the case", "bear_researcher"),
            (factory.deep(), "As the Research Manager and facilitator", "research_manager"),
            (factory.quick(), "You are a trading agent analyzing market data", "trader"),
            (factory.quick(), "As the Aggressive Risk Analyst", "aggressive_risk_analyst"),
            (factory.quick(), "As the Conservative Risk Analyst", "conservative_risk_analyst"),
            (factory.quick(), "As the Neutral Risk Analyst", "neutral_risk_analyst"),
            (factory.deep(), "As the Portfolio Manager, synthesize", "portfolio_manager"),
            (factory.quick(), "reviewing your own past decision", "reflection"),
        )
        for llm, prompt, expected in cases:
            llm.invoke(prompt)
            self.assertEqual(broker.calls[-1]["role_key"], expected)

    def test_unknown_conflicting_and_wrong_profile_roles_fail_closed(self):
        factory, broker, _ = self._factory()
        with self.assertRaises(TradingAgentsLLMAdapterError) as unknown:
            factory.quick().invoke("generic unclassified prompt")
        self.assertEqual(unknown.exception.error_code, "role_unresolved")

        with self.assertRaises(TradingAgentsLLMAdapterError) as conflict:
            factory.quick().invoke("You are a Bull Analyst and You are a Bear Analyst")
        self.assertEqual(conflict.exception.error_code, "role_resolution_conflict")

        with self.assertRaises(TradingAgentsLLMAdapterError) as tools_conflict:
            factory.quick().for_role("news_analyst").bind_tools(
                _tools("get_stock_data", "get_indicators", "get_verified_market_snapshot")
            )
        self.assertEqual(tools_conflict.exception.error_code, "role_resolution_conflict")

        with self.assertRaises(TradingAgentsLLMAdapterError) as profile:
            factory.quick().for_role("portfolio_manager")
        self.assertEqual(profile.exception.error_code, "role_profile_mismatch")
        self.assertFalse(broker.calls)

    def test_json_schema_failure_repairs_once_then_succeeds(self):
        invalid = SharedLLMBrokerError("bad json", error_code="structured_output_invalid")
        broker = _FakeBroker([invalid, _result(content='{"rating":"Hold"}', parsed={"rating": "Hold"})])
        factory, _, _ = self._factory(broker)

        value = factory.quick().with_structured_output(_schema("TraderProposal")).invoke("prompt")

        self.assertEqual(value, {"rating": "Hold"})
        self.assertEqual(len(broker.calls), 2)
        self.assertEqual(broker.calls[0]["role_key"], "trader")
        self.assertIn("previous response failed", broker.calls[1]["messages"][-1]["content"].lower())

    def test_json_schema_failure_stops_at_declared_repair_limit(self):
        failures = [
            SharedLLMBrokerError("bad", error_code="structured_output_invalid"),
            SharedLLMBrokerError("still bad", error_code="structured_output_invalid"),
            _result(parsed={}),
        ]
        broker = _FakeBroker(failures)
        factory, _, _ = self._factory(broker)

        with self.assertRaises(TradingAgentsLLMAdapterError) as caught:
            factory.deep().with_structured_output(_schema("ResearchPlan")).invoke("prompt")

        self.assertEqual(caught.exception.error_code, "structured_output_invalid")
        self.assertEqual(len(broker.calls), 2)

    def test_pydantic_style_validation_failure_uses_the_same_finite_repair(self):
        class ValidatedProposal:
            @classmethod
            def model_json_schema(cls):
                return {"type": "object", "properties": {"rating": {"type": "string"}}}

            @classmethod
            def model_validate(cls, value):
                if value.get("rating") != "Hold":
                    raise ValueError("business validator rejected rating")
                return value

        ValidatedProposal.__name__ = "TraderProposal"
        broker = _FakeBroker(
            [
                _result(content='{"rating":"bad"}', parsed={"rating": "bad"}),
                _result(content='{"rating":"Hold"}', parsed={"rating": "Hold"}),
            ]
        )
        factory, _, _ = self._factory(broker)

        value = factory.quick().with_structured_output(ValidatedProposal).invoke("prompt")

        self.assertEqual(value, {"rating": "Hold"})
        self.assertEqual(len(broker.calls), 2)

    def test_timeout_cancellation_and_round_limit_are_stable_adapter_errors(self):
        timeout_broker = _FakeBroker(
            [SharedLLMBrokerError("endpoint detail", error_code="llm_timeout")]
        )
        factory, _, _ = self._factory(timeout_broker)
        with self.assertRaises(TradingAgentsLLMAdapterError) as timeout:
            factory.quick().for_role("bull_researcher").invoke("prompt")
        self.assertEqual(timeout.exception.error_code, "llm_timeout")
        self.assertNotIn("endpoint detail", str(timeout.exception))

        cancelled_broker = _FakeBroker(
            [SharedLLMBrokerError("cancel detail", error_code="llm_cancelled")]
        )
        factory, _, _ = self._factory(cancelled_broker)
        with self.assertRaises(TradingAgentsLLMAdapterError) as cancelled:
            factory.quick().for_role("bear_researcher").invoke("prompt")
        self.assertEqual(cancelled.exception.error_code, "llm_cancelled")

        factory, broker, _ = self._factory()
        reflection = factory.quick().for_role("reflection")
        reflection.invoke("first")
        with self.assertRaises(TradingAgentsLLMAdapterError) as rounds:
            reflection.invoke("second")
        self.assertEqual(rounds.exception.error_code, "role_round_limit")
        self.assertEqual(len(broker.calls), 1)

    def test_context_priority_and_identifiers_are_validated_before_broker_use(self):
        factory, broker, _ = self._factory(priority="scheduled_research")
        factory.quick().for_role("bull_researcher").invoke("scheduled")
        self.assertEqual(broker.calls[0]["priority"], "scheduled_research")
        self.assertLessEqual(len(broker.calls[0]["request_id"]), 160)

        with self.assertRaises(TradingAgentsLLMAdapterError):
            TradingAgentsLLMRunContext("run", request_id="x" * 101)
        with self.assertRaises(TradingAgentsLLMAdapterError):
            TradingAgentsLLMRunContext("run", priority="unknown")
        with self.assertRaises(TradingAgentsLLMAdapterError):
            TradingAgentsLLMRunContext("unsafe run id")

    def test_tool_calls_are_translated_without_adapter_execution(self):
        tool_call = {
            "id": "call-market",
            "type": "function",
            "function": {"name": "get_stock_data", "arguments": {"symbol": "600000.SS"}},
        }
        broker = _FakeBroker([_result(content="", tool_calls=(tool_call,))])
        factory, _, _ = self._factory(broker)
        llm = factory.quick().bind_tools(
            _tools("get_stock_data", "get_indicators", "get_verified_market_snapshot")
        )

        message = llm.invoke("market")

        self.assertEqual(message.tool_calls[0]["name"], "get_stock_data")
        self.assertEqual(message.tool_calls[0]["args"], {"symbol": "600000.SS"})
        self.assertEqual(len(broker.calls), 1)

    def test_upstream_compatibility_metadata_contains_no_key_or_secret(self):
        factory, _, _ = self._factory()
        value = factory.upstream_config()
        lowered = json.dumps(value, ensure_ascii=False).lower()

        self.assertEqual(value["llm_provider"], "openai_compatible")
        self.assertEqual(value["quick_think_llm"], RUNTIME["model_id"])
        self.assertEqual(value["deep_think_llm"], RUNTIME["model_id"])
        self.assertTrue(value["collectinfo_adapter_required"])
        self.assertNotIn("api_key", lowered)
        self.assertNotIn("secret", lowered)

    def test_adapter_source_has_no_direct_network_or_environment_client(self):
        source = Path(adapter_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])

        self.assertFalse(imported_roots & {"os", "requests", "httpx", "openai", "anthropic"})
        self.assertEqual(source.count("self.broker.complete("), 1)
        self.assertNotIn("os.environ", source)


class TradingAgentsLLMAdapterAuditTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "adapter.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.connection = self.database.connection
        self.connection.executemany(
            """
            INSERT INTO financial_research_runs(id, trigger_type, scope_type, status)
            VALUES(?, 'test', 'market', 'running')
            """,
            (("research-audit-1",), ("research-cancel-1",)),
        )

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _broker(self, body="audited"):
        session = Mock()
        session.request.return_value = _Response(_body(body))
        transport = IntelLLMClient(
            provider="local",
            runtime_config_loader=lambda: dict(RUNTIME),
            session=session,
            sleep=lambda _seconds: None,
        )
        broker = SharedLLMBroker(
            self.connection,
            runtime_config_loader=lambda: dict(RUNTIME),
            transport=transport,
            clock=lambda: datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc),
        )
        return broker, session

    def test_real_broker_writes_complete_role_run_model_latency_and_hash_audit(self):
        broker, session = self._broker()
        context = TradingAgentsLLMRunContext(
            "research-audit-1",
            priority="interactive_research",
            request_id="chat-audit-1",
        )
        llm = BrokerBackedTradingAgentsLLM(
            broker,
            context,
            profile_hint="fast",
        ).for_role("bull_researcher")

        message = llm.invoke("audited role call")

        audit = self.connection.execute(
            """
            SELECT research_run_id, role_key, profile_key, model_id, latency_ms,
                   prompt_sha256, response_sha256, request_id, status
              FROM llm_call_audit
             ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        self.assertEqual(audit["research_run_id"], "research-audit-1")
        self.assertEqual(audit["role_key"], "bull_researcher")
        self.assertEqual(audit["profile_key"], "fast")
        self.assertEqual(audit["model_id"], RUNTIME["model_id"])
        self.assertGreaterEqual(audit["latency_ms"], 0)
        self.assertEqual(len(audit["prompt_sha256"]), 64)
        self.assertEqual(len(audit["response_sha256"]), 64)
        self.assertEqual(audit["status"], "completed")
        self.assertTrue(audit["request_id"].startswith("chat-audit-1:bull_researcher:"))
        self.assertEqual(message.response_metadata["model_id"], RUNTIME["model_id"])
        call = session.request.call_args
        self.assertEqual(call.args[1], f"{RUNTIME['base_url']}/chat/completions")
        self.assertEqual(call.kwargs["json"]["model"], RUNTIME["model_id"])

    def test_pre_cancelled_adapter_call_never_reaches_transport_and_is_audited(self):
        broker, session = self._broker()
        cancelled = threading.Event()
        cancelled.set()
        context = TradingAgentsLLMRunContext(
            "research-cancel-1",
            request_id="cancel-1",
            cancel_event=cancelled,
        )
        llm = BrokerBackedTradingAgentsLLM(
            broker,
            context,
            profile_hint="fast",
        ).for_role("bear_researcher")

        with self.assertRaises(TradingAgentsLLMAdapterError) as caught:
            llm.invoke("cancelled role call")

        self.assertEqual(caught.exception.error_code, "llm_cancelled")
        self.assertEqual(session.request.call_count, 0)
        audit = self.connection.execute(
            "SELECT status, error_code, role_key, research_run_id FROM llm_call_audit"
        ).fetchone()
        self.assertEqual(audit["status"], "cancelled")
        self.assertEqual(audit["error_code"], "llm_cancelled")
        self.assertEqual(audit["role_key"], "bear_researcher")
        self.assertEqual(audit["research_run_id"], "research-cancel-1")


if __name__ == "__main__":
    unittest.main()

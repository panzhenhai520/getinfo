#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""LangChain/TradingAgents facade backed exclusively by SharedLLMBroker.

The adapter deliberately owns no endpoint, credential or HTTP client.  It is a
small in-process Runnable that translates LangChain messages/tools/schemas into
the existing broker contract and translates the audited result back to an
AIMessage.  It can also be imported without LangChain on maintenance hosts;
the crawler image supplies the real Runnable and AIMessage classes.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from shared_llm_broker import (
    LLMCallResult,
    PRIORITY_ORDER,
    SharedLLMBroker,
    SharedLLMBrokerError,
)

try:  # The production crawler image installs this through the audited lock.
    from langchain_core.messages import AIMessage as _LangChainAIMessage
    from langchain_core.runnables import Runnable as _LangChainRunnable
    from langchain_core.utils.function_calling import convert_to_openai_tool

    LANGCHAIN_AVAILABLE = True
except ImportError:  # Static checks and host unit tests do not need LangChain.
    _LangChainAIMessage = None
    convert_to_openai_tool = None
    LANGCHAIN_AVAILABLE = False

    class _LangChainRunnable:  # type: ignore[no-redef]
        pass


_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
# A role and ordinal are appended before the id reaches SharedLLMBroker, whose
# complete request id is capped at 160 characters.  Keeping the run-level
# prefix at 100 makes that invariant independent of future role-name growth.
_SAFE_REQUEST_PREFIX = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")


class TradingAgentsLLMAdapterError(RuntimeError):
    """Stable, redacted adapter failure."""

    def __init__(self, message: str, *, error_code: str, role_key: str = ""):
        super().__init__(message)
        self.error_code = str(error_code)
        self.role_key = str(role_key)


@dataclass(frozen=True)
class TradingAgentsRolePolicy:
    key: str
    profile: str
    max_output_tokens: int
    timeout_seconds: int
    max_rounds: int
    max_json_repairs: int = 0


# The limits are per research run and fail closed.  Analyst limits include tool
# round-trips; decision roles include one structured repair and one upstream
# free-text fallback.  Debate limits match the configured system maximum (10).
ROLE_POLICIES: dict[str, TradingAgentsRolePolicy] = {
    "market_analyst": TradingAgentsRolePolicy("market_analyst", "fast", 2048, 90, 12),
    "sentiment_analyst": TradingAgentsRolePolicy(
        "sentiment_analyst", "fast", 2048, 90, 3, 1
    ),
    "news_analyst": TradingAgentsRolePolicy("news_analyst", "fast", 2048, 90, 10),
    "fundamentals_analyst": TradingAgentsRolePolicy(
        "fundamentals_analyst", "fast", 2048, 90, 12
    ),
    "bull_researcher": TradingAgentsRolePolicy("bull_researcher", "fast", 1536, 60, 10),
    "bear_researcher": TradingAgentsRolePolicy("bear_researcher", "fast", 1536, 60, 10),
    "research_manager": TradingAgentsRolePolicy(
        "research_manager", "deep", 4096, 180, 3, 1
    ),
    "trader": TradingAgentsRolePolicy("trader", "fast", 2048, 90, 3, 1),
    "aggressive_risk_analyst": TradingAgentsRolePolicy(
        "aggressive_risk_analyst", "fast", 1536, 60, 10
    ),
    "conservative_risk_analyst": TradingAgentsRolePolicy(
        "conservative_risk_analyst", "fast", 1536, 60, 10
    ),
    "neutral_risk_analyst": TradingAgentsRolePolicy(
        "neutral_risk_analyst", "fast", 1536, 60, 10
    ),
    "portfolio_manager": TradingAgentsRolePolicy(
        "portfolio_manager", "deep", 4096, 180, 3, 1
    ),
    "reflection": TradingAgentsRolePolicy("reflection", "fast", 1024, 60, 1),
}


SCHEMA_ROLE_MAP = {
    "SentimentReport": "sentiment_analyst",
    "ResearchPlan": "research_manager",
    "TraderProposal": "trader",
    "PortfolioDecision": "portfolio_manager",
}

TOOL_ROLE_MAP = {
    frozenset({"get_stock_data", "get_indicators", "get_verified_market_snapshot"}): "market_analyst",
    frozenset({"get_news"}): "sentiment_analyst",
    frozenset(
        {
            "get_news",
            "get_global_news",
            "get_macro_indicators",
            "get_prediction_markets",
        }
    ): "news_analyst",
    frozenset(
        {"get_fundamentals", "get_balance_sheet", "get_cashflow", "get_income_statement"}
    ): "fundamentals_analyst",
}

PROMPT_ROLE_MARKERS = (
    ("you are a bull analyst", "bull_researcher"),
    ("you are a bear analyst", "bear_researcher"),
    ("as the research manager", "research_manager"),
    ("you are a trading agent analyzing market data", "trader"),
    ("as the aggressive risk analyst", "aggressive_risk_analyst"),
    ("as the conservative risk analyst", "conservative_risk_analyst"),
    ("as the neutral risk analyst", "neutral_risk_analyst"),
    ("as the portfolio manager", "portfolio_manager"),
    ("reviewing your own past decision", "reflection"),
    ("select the most relevant indicators", "market_analyst"),
    ("analyzing fundamental information", "fundamentals_analyst"),
)


@dataclass
class TradingAgentsLLMRunContext:
    """Auditable identity, scheduling class, cancellation and round budgets."""

    research_run_id: str
    priority: str = "interactive_research"
    job_id: Optional[int] = None
    request_id: str = ""
    cancel_event: Any = field(default_factory=threading.Event)
    _role_counts: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(str(self.research_run_id or "")):
            raise TradingAgentsLLMAdapterError(
                "research_run_id 格式无效",
                error_code="invalid_llm_request",
            )
        if self.priority not in PRIORITY_ORDER:
            raise TradingAgentsLLMAdapterError(
                "TradingAgents LLM priority 无效",
                error_code="invalid_llm_request",
            )
        if self.job_id is not None and (
            isinstance(self.job_id, bool) or not isinstance(self.job_id, int) or self.job_id < 1
        ):
            raise TradingAgentsLLMAdapterError(
                "TradingAgents job_id 无效",
                error_code="invalid_llm_request",
            )
        if self.request_id and not _SAFE_REQUEST_PREFIX.fullmatch(str(self.request_id)):
            raise TradingAgentsLLMAdapterError(
                "TradingAgents request_id 无效",
                error_code="invalid_llm_request",
            )
        if not self.request_id:
            self.request_id = f"ta-{uuid.uuid4().hex[:20]}"

    def consume_round(self, policy: TradingAgentsRolePolicy) -> int:
        with self._lock:
            count = self._role_counts.get(policy.key, 0) + 1
            if count > policy.max_rounds:
                raise TradingAgentsLLMAdapterError(
                    "TradingAgents 角色调用轮数超过上限",
                    error_code="role_round_limit",
                    role_key=policy.key,
                )
            self._role_counts[policy.key] = count
            return count

    def role_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._role_counts)

    def cancel(self) -> None:
        setter = getattr(self.cancel_event, "set", None)
        if setter is None:
            raise TradingAgentsLLMAdapterError(
                "cancel_event 不支持取消",
                error_code="invalid_llm_request",
            )
        setter()


@dataclass
class BrokerAIMessage:
    """Minimal host-test fallback matching the attributes upstream reads."""

    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    response_metadata: dict[str, Any] = field(default_factory=dict)


def _schema_name(schema: Any) -> str:
    if isinstance(schema, Mapping):
        return str(schema.get("title") or schema.get("name") or "")
    return str(getattr(schema, "__name__", ""))


def _schema_json(schema: Any) -> dict[str, Any]:
    if isinstance(schema, Mapping):
        value = dict(schema)
    elif callable(getattr(schema, "model_json_schema", None)):
        value = dict(schema.model_json_schema())
    elif callable(getattr(schema, "schema", None)):
        value = dict(schema.schema())
    else:
        raise TradingAgentsLLMAdapterError(
            "TradingAgents structured schema 无效",
            error_code="invalid_llm_request",
        )
    if value.get("type") != "object" and "properties" not in value:
        raise TradingAgentsLLMAdapterError(
            "TradingAgents structured schema 必须是对象",
            error_code="invalid_llm_request",
        )
    return value


def _tool_definition(tool: Any) -> dict[str, Any]:
    if convert_to_openai_tool is not None:
        try:
            return dict(convert_to_openai_tool(tool))
        except (TypeError, ValueError):
            pass
    if isinstance(tool, Mapping):
        if tool.get("type") == "function" and isinstance(tool.get("function"), Mapping):
            return dict(tool)
        name = str(tool.get("name") or "")
        description = str(tool.get("description") or "")
        parameters = tool.get("parameters") or tool.get("args_schema") or {
            "type": "object",
            "properties": {},
        }
    else:
        name = str(getattr(tool, "name", "") or getattr(tool, "__name__", ""))
        description = str(getattr(tool, "description", "") or getattr(tool, "__doc__", "") or "")
        args_schema = getattr(tool, "args_schema", None)
        if callable(getattr(args_schema, "model_json_schema", None)):
            parameters = args_schema.model_json_schema()
        elif isinstance(args_schema, Mapping):
            parameters = dict(args_schema)
        else:
            parameters = {"type": "object", "properties": {}}
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name):
        raise TradingAgentsLLMAdapterError(
            "TradingAgents tool name 无效",
            error_code="invalid_llm_request",
        )
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description[:1000],
            "parameters": parameters,
        },
    }


def _tool_names(tools: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    return frozenset(str(item["function"]["name"]) for item in tools)


def _message_role(message: Any) -> str:
    raw = str(getattr(message, "type", "") or getattr(message, "role", "")).lower()
    return {
        "human": "user",
        "user": "user",
        "ai": "assistant",
        "assistant": "assistant",
        "system": "system",
        "tool": "tool",
        "function": "tool",
    }.get(raw, raw)


def _one_message(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        message = dict(value)
        role = str(message.get("role") or "")
        if role == "human":
            role = "user"
        elif role == "ai":
            role = "assistant"
        message["role"] = role
        return message
    if isinstance(value, tuple) and len(value) == 2:
        return {"role": "user" if value[0] == "human" else str(value[0]), "content": value[1]}
    role = _message_role(value)
    content = getattr(value, "content", None)
    if role not in {"system", "user", "assistant", "tool"} or content is None:
        raise TradingAgentsLLMAdapterError(
            "TradingAgents message 类型无效",
            error_code="invalid_llm_request",
        )
    message: dict[str, Any] = {"role": role, "content": content}
    if role == "assistant":
        calls = getattr(value, "tool_calls", None) or []
        if calls:
            message["tool_calls"] = [
                {
                    "id": str(item.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(item.get("name") or ""),
                        "arguments": json.dumps(
                            item.get("args") or {},
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                }
                for item in calls
            ]
    if role == "tool" and getattr(value, "tool_call_id", None):
        message["tool_call_id"] = str(value.tool_call_id)
    if getattr(value, "name", None):
        message["name"] = str(value.name)
    return message


def normalize_messages(value: Any) -> list[dict[str, Any]]:
    to_messages = getattr(value, "to_messages", None)
    if callable(to_messages):
        value = to_messages()
    if isinstance(value, str):
        return [{"role": "user", "content": value}]
    if isinstance(value, Mapping):
        return [_one_message(value)]
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
        return [_one_message(value)]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        messages = [_one_message(item) for item in value]
        if messages:
            return messages
    raise TradingAgentsLLMAdapterError(
        "TradingAgents messages 必须非空",
        error_code="invalid_llm_request",
    )


def _prompt_text(messages: Sequence[Mapping[str, Any]]) -> str:
    pieces = []
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, str):
            pieces.append(content)
        else:
            pieces.append(json.dumps(content, ensure_ascii=False, sort_keys=True))
    return "\n".join(pieces).lower()


def _result_message(result: LLMCallResult, role_key: str, run_id: str) -> Any:
    calls = [
        {
            "name": str(item["function"]["name"]),
            "args": dict(item["function"]["arguments"]),
            "id": str(item.get("id") or ""),
            "type": "tool_call",
        }
        for item in result.tool_calls
    ]
    metadata = {
        "call_id": result.call_id,
        "research_run_id": run_id,
        "role_key": role_key,
        "profile_key": result.profile_key,
        "provider_id": result.provider_id,
        "model_id": result.model_id,
        "runtime_source": result.runtime_source,
        "latency_ms": result.latency_ms,
        "response_sha256": result.response_sha256,
        "finish_reason": result.finish_reason,
    }
    if _LangChainAIMessage is not None:
        return _LangChainAIMessage(
            content=result.content,
            tool_calls=calls,
            response_metadata=metadata,
        )
    return BrokerAIMessage(result.content, calls, metadata)


class BrokerBackedTradingAgentsLLM(_LangChainRunnable):
    """Runnable facade used as TradingAgents' quick/deep LLM object."""

    def __init__(
        self,
        broker: SharedLLMBroker,
        run_context: TradingAgentsLLMRunContext,
        *,
        profile_hint: str,
        role_key: str = "",
        tools: Optional[Sequence[Mapping[str, Any]]] = None,
        tool_choice: Any = None,
    ):
        if profile_hint not in {"fast", "deep"}:
            raise TradingAgentsLLMAdapterError(
                "TradingAgents profile hint 无效",
                error_code="invalid_llm_request",
            )
        self.broker = broker
        self.run_context = run_context
        self.profile_hint = profile_hint
        self.role_key = role_key
        self.tools = tuple(dict(item) for item in (tools or ()))
        self.tool_choice = tool_choice

    def _copy(self, **overrides: Any) -> "BrokerBackedTradingAgentsLLM":
        values = {
            "profile_hint": self.profile_hint,
            "role_key": self.role_key,
            "tools": self.tools,
            "tool_choice": self.tool_choice,
        }
        values.update(overrides)
        return BrokerBackedTradingAgentsLLM(self.broker, self.run_context, **values)

    def for_role(self, role_key: str) -> "BrokerBackedTradingAgentsLLM":
        if role_key not in ROLE_POLICIES:
            raise TradingAgentsLLMAdapterError(
                "未知 TradingAgents 角色",
                error_code="invalid_llm_request",
            )
        policy = ROLE_POLICIES[role_key]
        if policy.profile != self.profile_hint:
            raise TradingAgentsLLMAdapterError(
                "TradingAgents 角色与 fast/deep profile 不匹配",
                error_code="role_profile_mismatch",
                role_key=role_key,
            )
        return self._copy(role_key=role_key)

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: Any = "auto",
        **_kwargs: Any,
    ) -> "BrokerBackedTradingAgentsLLM":
        definitions = tuple(_tool_definition(tool) for tool in tools)
        inferred = TOOL_ROLE_MAP.get(_tool_names(definitions), "")
        role = self.role_key or inferred
        if self.role_key and inferred and self.role_key != inferred:
            raise TradingAgentsLLMAdapterError(
                "TradingAgents tool 类别与角色冲突",
                error_code="role_resolution_conflict",
                role_key=self.role_key,
            )
        return self._copy(role_key=role, tools=definitions, tool_choice=tool_choice)

    def with_structured_output(
        self,
        schema: Any,
        *,
        include_raw: bool = False,
        **_kwargs: Any,
    ) -> "BrokerStructuredTradingAgentsRunnable":
        name = _schema_name(schema)
        inferred = SCHEMA_ROLE_MAP.get(name, "")
        role = self.role_key or inferred
        if not role:
            raise TradingAgentsLLMAdapterError(
                "无法从 structured schema 确定 TradingAgents 角色",
                error_code="role_unresolved",
            )
        if self.role_key and inferred and self.role_key != inferred:
            raise TradingAgentsLLMAdapterError(
                "TradingAgents schema 与角色冲突",
                error_code="role_resolution_conflict",
                role_key=self.role_key,
            )
        bound = self.for_role(role) if not self.role_key else self
        return BrokerStructuredTradingAgentsRunnable(bound, schema, include_raw=include_raw)

    def bind(self, **kwargs: Any) -> "BrokerBackedTradingAgentsLLM":
        allowed = {"tool_choice"}
        unknown = set(kwargs) - allowed
        if unknown:
            raise TradingAgentsLLMAdapterError(
                "TradingAgents bind 参数不受支持",
                error_code="invalid_llm_request",
            )
        return self._copy(**kwargs)

    def with_config(self, _config: Any = None, **_kwargs: Any) -> "BrokerBackedTradingAgentsLLM":
        return self._copy()

    def _resolve_role(self, messages: Sequence[Mapping[str, Any]]) -> str:
        if self.role_key:
            return self.role_key
        prompt = _prompt_text(messages)
        matches = {role for marker, role in PROMPT_ROLE_MARKERS if marker in prompt}
        if len(matches) != 1:
            raise TradingAgentsLLMAdapterError(
                "无法唯一确定 TradingAgents 调用角色",
                error_code="role_unresolved" if not matches else "role_resolution_conflict",
            )
        return matches.pop()

    def _invoke_once(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        response_schema: Optional[Mapping[str, Any]] = None,
    ) -> tuple[LLMCallResult, Any, TradingAgentsRolePolicy]:
        role = self._resolve_role(messages)
        policy = ROLE_POLICIES[role]
        if policy.profile != self.profile_hint:
            raise TradingAgentsLLMAdapterError(
                "TradingAgents 角色与 fast/deep profile 不匹配",
                error_code="role_profile_mismatch",
                role_key=role,
            )
        ordinal = self.run_context.consume_round(policy)
        request_id = f"{self.run_context.request_id}:{role}:{ordinal}"
        try:
            result = self.broker.complete(
                messages,
                profile=policy.profile,
                priority=self.run_context.priority,
                role_key=role,
                research_run_id=self.run_context.research_run_id,
                job_id=self.run_context.job_id,
                request_id=request_id,
                response_schema=response_schema,
                tools=self.tools or None,
                tool_choice=self.tool_choice if self.tools else None,
                max_tokens=policy.max_output_tokens,
                timeout_seconds=policy.timeout_seconds,
                cancel_event=self.run_context.cancel_event,
            )
        except SharedLLMBrokerError as exc:
            raise TradingAgentsLLMAdapterError(
                "TradingAgents 本地模型调用失败",
                error_code=exc.error_code,
                role_key=role,
            ) from exc
        message = _result_message(result, role, self.run_context.research_run_id)
        return result, message, policy

    def invoke(self, input: Any, config: Any = None, **_kwargs: Any) -> Any:
        del config
        messages = normalize_messages(input)
        _result, message, _policy = self._invoke_once(messages)
        return message

    def stream(self, input: Any, config: Any = None, **kwargs: Any):
        yield self.invoke(input, config=config, **kwargs)


class BrokerStructuredTradingAgentsRunnable(_LangChainRunnable):
    """Schema-bound Runnable with a finite broker-level JSON repair budget."""

    def __init__(
        self,
        adapter: BrokerBackedTradingAgentsLLM,
        schema: Any,
        *,
        include_raw: bool,
    ):
        self.adapter = adapter
        self.schema = schema
        self.response_schema = _schema_json(schema)
        self.include_raw = bool(include_raw)

    def _parse(self, value: Any) -> Any:
        if isinstance(self.schema, Mapping):
            return value
        validator = getattr(self.schema, "model_validate", None)
        if callable(validator):
            return validator(value)
        parser = getattr(self.schema, "parse_obj", None)
        if callable(parser):
            return parser(value)
        return value

    def _validated_parse(self, value: Any, role: str) -> Any:
        try:
            return self._parse(value)
        except Exception as exc:
            raise TradingAgentsLLMAdapterError(
                "TradingAgents 结构化输出未通过类型校验",
                error_code="structured_output_invalid",
                role_key=role,
            ) from exc

    def invoke(self, input: Any, config: Any = None, **_kwargs: Any) -> Any:
        del config
        original = normalize_messages(input)
        role = self.adapter._resolve_role(original)
        policy = ROLE_POLICIES[role]
        messages = list(original)
        last_error: Optional[TradingAgentsLLMAdapterError] = None
        raw = None
        for attempt in range(policy.max_json_repairs + 1):
            try:
                result, raw, _ = self.adapter._invoke_once(
                    messages,
                    response_schema=self.response_schema,
                )
                parsed = self._validated_parse(result.parsed, role)
                if self.include_raw:
                    return {"raw": raw, "parsed": parsed, "parsing_error": None}
                return parsed
            except TradingAgentsLLMAdapterError as exc:
                last_error = exc
                if exc.error_code != "structured_output_invalid" or attempt >= policy.max_json_repairs:
                    if self.include_raw:
                        return {"raw": raw, "parsed": None, "parsing_error": exc}
                    raise
                messages = list(original) + [
                    {
                        "role": "system",
                        "content": (
                            "The previous response failed the required JSON Schema. "
                            "Return exactly one valid JSON object matching the schema; no markdown."
                        ),
                    }
                ]
        raise last_error or TradingAgentsLLMAdapterError(
            "TradingAgents structured output failed",
            error_code="structured_output_invalid",
            role_key=role,
        )


class TradingAgentsBrokerLLMClient:
    """Small upstream-style client wrapper exposing ``get_llm``."""

    def __init__(self, llm: BrokerBackedTradingAgentsLLM):
        self._llm = llm

    def get_llm(self) -> BrokerBackedTradingAgentsLLM:
        return self._llm

    def validate_model(self) -> bool:
        return True


class TradingAgentsLLMAdapterFactory:
    """Create the quick/deep objects injected into upstream GraphSetup."""

    def __init__(self, broker: SharedLLMBroker, run_context: TradingAgentsLLMRunContext):
        self.broker = broker
        self.run_context = run_context

    def quick(self) -> BrokerBackedTradingAgentsLLM:
        return BrokerBackedTradingAgentsLLM(
            self.broker,
            self.run_context,
            profile_hint="fast",
        )

    def deep(self) -> BrokerBackedTradingAgentsLLM:
        return BrokerBackedTradingAgentsLLM(
            self.broker,
            self.run_context,
            profile_hint="deep",
        )

    def upstream_config(self) -> dict[str, Any]:
        """Return compatibility metadata; never pass it to the upstream client factory."""
        identity = self.broker.runtime_identity()
        return {
            "llm_provider": "openai_compatible",
            "quick_think_llm": identity["model_id"],
            "deep_think_llm": identity["model_id"],
            "backend_url": identity["base_url"],
            "collectinfo_adapter_required": True,
            "runtime_source": identity["runtime_source"],
            "profiles": {"quick": "fast", "deep": "deep"},
        }


__all__ = [
    "BrokerAIMessage",
    "BrokerBackedTradingAgentsLLM",
    "BrokerStructuredTradingAgentsRunnable",
    "LANGCHAIN_AVAILABLE",
    "ROLE_POLICIES",
    "TradingAgentsBrokerLLMClient",
    "TradingAgentsLLMAdapterError",
    "TradingAgentsLLMAdapterFactory",
    "TradingAgentsLLMRunContext",
    "TradingAgentsRolePolicy",
    "normalize_messages",
]

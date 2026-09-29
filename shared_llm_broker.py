#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""In-process broker for the project's existing local LLM runtime.

This module owns no model server, key, queue service, or listener.  It reloads
the same trusted configuration used by the AI assistant and delegates HTTP to
``IntelLLMClient``.  Its additions are bounded profiles, fair priority slots,
cooperative cancellation, strict structured-output validation, and hash-only
SQLite audit records.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, SchemaError, ValidationError

import config
from chat_api import get_chat_model_runtime_config
from intel_llm_client import IntelLLMClient, IntelLLMError


UTC = timezone.utc
PRIORITY_ORDER = {
    "chat_clarification": 0,
    "chat_fact": 1,
    "interactive_research": 2,
    "scheduled_research": 3,
    "batch_reflection": 4,
}
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{0,160}$")


class SharedLLMBrokerError(RuntimeError):
    def __init__(self, message: str, *, error_code: str):
        super().__init__(message)
        self.error_code = str(error_code)


class SharedLLMCancelled(SharedLLMBrokerError):
    def __init__(self):
        super().__init__("LLM 调用已取消", error_code="llm_cancelled")


class SharedLLMTimeout(SharedLLMBrokerError):
    def __init__(self, message: str = "LLM 调用超时"):
        super().__init__(message, error_code="llm_timeout")


@dataclass(frozen=True)
class LLMProfile:
    key: str
    max_input_chars: int
    max_output_tokens: int
    timeout_seconds: int
    max_retries: int
    temperature: float
    enable_thinking: bool


@dataclass(frozen=True)
class LLMCallResult:
    call_id: str
    profile_key: str
    provider_id: str
    model_id: str
    runtime_source: str
    content: str
    parsed: object
    tool_calls: Tuple[Mapping[str, object], ...]
    finish_reason: str
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    latency_ms: int
    response_sha256: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "call_id": self.call_id,
            "profile_key": self.profile_key,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "runtime_source": self.runtime_source,
            "content": self.content,
            "parsed": self.parsed,
            "tool_calls": [dict(item) for item in self.tool_calls],
            "finish_reason": self.finish_reason,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "response_sha256": self.response_sha256,
        }


@dataclass
class _Ticket:
    sequence: int
    base_priority: int
    enqueued_at: float
    cancel_event: object
    foreground: bool


class _FairPrioritySlots:
    """FIFO within a class, priority with aging across classes."""

    def __init__(
        self,
        capacity: int,
        *,
        monotonic=time.monotonic,
        aging_seconds: float = 30.0,
        interactive_reserve: int = 0,
    ):
        if int(capacity) < 1:
            raise ValueError("LLM broker concurrency must be positive")
        self.capacity = int(capacity)
        self.interactive_reserve = max(
            0, min(int(interactive_reserve), self.capacity - 1)
        )
        self.background_capacity = self.capacity - self.interactive_reserve
        self.monotonic = monotonic
        self.aging_seconds = max(0.01, float(aging_seconds))
        self.condition = threading.Condition()
        self.waiting: list[_Ticket] = []
        self.running = 0
        self.background_running = 0
        self.sequence = 0

    @staticmethod
    def _cancelled(event) -> bool:
        return bool(event is not None and event.is_set())

    def _rank(self, ticket: _Ticket, now: float) -> Tuple[int, int]:
        aged = int(max(0.0, now - ticket.enqueued_at) / self.aging_seconds)
        return max(0, ticket.base_priority - aged), ticket.sequence

    def _eligible(self, ticket: _Ticket) -> bool:
        if self.running >= self.capacity:
            return False
        return ticket.foreground or self.background_running < self.background_capacity

    @contextmanager
    def slot(
        self,
        priority: int,
        *,
        deadline: float,
        cancel_event=None,
        foreground: bool = False,
    ):
        with self.condition:
            self.sequence += 1
            ticket = _Ticket(
                self.sequence,
                int(priority),
                self.monotonic(),
                cancel_event,
                bool(foreground),
            )
            self.waiting.append(ticket)
            acquired = False
            try:
                while not acquired:
                    now = self.monotonic()
                    if self._cancelled(cancel_event):
                        raise SharedLLMCancelled()
                    if now >= deadline:
                        raise SharedLLMTimeout("等待本地 LLM 调度槽超时")
                    self.waiting[:] = [
                        item for item in self.waiting if not self._cancelled(item.cancel_event)
                    ]
                    eligible = [item for item in self.waiting if self._eligible(item)]
                    best = (
                        min(eligible, key=lambda item: self._rank(item, now))
                        if eligible
                        else None
                    )
                    if best is ticket:
                        self.waiting.remove(ticket)
                        self.running += 1
                        if not ticket.foreground:
                            self.background_running += 1
                        acquired = True
                        break
                    self.condition.wait(timeout=min(0.05, max(0.0, deadline - now)))
            except Exception:
                if ticket in self.waiting:
                    self.waiting.remove(ticket)
                self.condition.notify_all()
                raise
        try:
            yield
        finally:
            with self.condition:
                if acquired:
                    self.running -= 1
                    if not ticket.foreground:
                        self.background_running -= 1
                self.condition.notify_all()

    def snapshot(self) -> Dict[str, int]:
        with self.condition:
            return {
                "capacity": self.capacity,
                "interactive_reserve": self.interactive_reserve,
                "background_capacity": self.background_capacity,
                "running": self.running,
                "background_running": self.background_running,
                "waiting": len(self.waiting),
            }


def _utc_text(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _stable_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise SharedLLMBrokerError(
            "LLM 请求必须是可序列化的有限 JSON",
            error_code="invalid_llm_request",
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _safe_identifier(value: object, label: str, *, allow_empty: bool = True) -> str:
    text = str(value or "").strip()
    if not text and allow_empty:
        return ""
    if not _SAFE_ID.fullmatch(text):
        raise SharedLLMBrokerError(
            f"{label} 格式无效",
            error_code="invalid_llm_request",
        )
    return text


def _token_value(usage: Mapping[str, object], key: str) -> Optional[int]:
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, int(value))


class SharedLLMBroker:
    """One auditable entry point for chat and financial multi-agent calls."""

    def __init__(
        self,
        connection=None,
        *,
        runtime_config_loader=None,
        transport: Optional[IntelLLMClient] = None,
        max_concurrency: Optional[int] = None,
        clock=None,
        monotonic=time.monotonic,
        aging_seconds: float = 30.0,
        interactive_reserve: Optional[int] = None,
    ):
        self.connection = connection
        self.runtime_config_loader = runtime_config_loader or (
            lambda: get_chat_model_runtime_config("local")
        )
        self.transport = transport or IntelLLMClient(
            provider="local",
            runtime_config_loader=self.runtime_config_loader,
        )
        self.clock = clock or (lambda: datetime.now(UTC))
        self.monotonic = monotonic
        concurrency = int(max_concurrency or config.INTEL_LLM_MAX_CONCURRENCY)
        self.slots = _FairPrioritySlots(
            concurrency,
            monotonic=monotonic,
            aging_seconds=aging_seconds,
            interactive_reserve=(
                int(config.INTEL_LLM_INTERACTIVE_RESERVED)
                if interactive_reserve is None
                else int(interactive_reserve)
            ),
        )
        self.audit_lock = threading.RLock()
        self.profiles = {
            "fast": LLMProfile(
                "fast",
                max_input_chars=max(32000, int(config.INTEL_LLM_MAX_INPUT_CHARS)),
                max_output_tokens=2048,
                timeout_seconds=min(90, int(config.INTEL_LLM_TIMEOUT_SECONDS)),
                max_retries=min(1, int(config.INTEL_LLM_MAX_RETRIES)),
                temperature=0.1,
                enable_thinking=False,
            ),
            "deep": LLMProfile(
                "deep",
                max_input_chars=max(200000, int(config.INTEL_LLM_MAX_INPUT_CHARS)),
                max_output_tokens=8192,
                timeout_seconds=min(300, int(config.FINANCIAL_RESEARCH_TIMEOUT_SECONDS)),
                max_retries=min(1, int(config.INTEL_LLM_MAX_RETRIES)),
                temperature=0.2,
                # 全站统一：无条件关闭思维链输出（无论模型是否支持、无论哪个 provider）
                enable_thinking=False,
            ),
        }

    def _runtime(self) -> Dict[str, object]:
        runtime = self.runtime_config_loader()
        if not isinstance(runtime, Mapping):
            raise SharedLLMBrokerError(
                "AI 助手本地模型配置无效",
                error_code="llm_not_configured",
            )
        runtime = dict(runtime)
        if runtime.get("provider_id") != "local" or runtime.get("type") != "openai":
            raise SharedLLMBrokerError(
                "SharedLLMBroker 只允许 AI 助手的本地 OpenAI-compatible 配置",
                error_code="unauthorized_llm_provider",
            )
        missing = [key for key in ("base_url", "api_key", "model_id") if not runtime.get(key)]
        if missing:
            raise SharedLLMBrokerError(
                "AI 助手本地模型尚未完整配置",
                error_code="llm_not_configured",
            )
        base_url = str(runtime["base_url"]).rstrip("/")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise SharedLLMBrokerError(
                "AI 助手本地模型 base_url 无效",
                error_code="llm_not_configured",
            )
        runtime["base_url"] = base_url
        return runtime

    def runtime_identity(self) -> Dict[str, object]:
        runtime = self._runtime()
        return {
            "provider_id": "local",
            "model_id": str(runtime["model_id"]),
            "base_url": str(runtime["base_url"]),
            "runtime_source": "chat_api.get_chat_model_runtime_config(local)",
            "api_key_configured": True,
            "api_key_exposed": False,
            "profiles": sorted(self.profiles),
            "service_type": "embedded_library",
            "resource_isolation": self.slots.snapshot(),
        }

    @staticmethod
    def _validate_messages(messages: Sequence[Mapping[str, object]]) -> Tuple[list, int]:
        if not isinstance(messages, (list, tuple)) or not messages:
            raise SharedLLMBrokerError(
                "messages 必须是非空列表",
                error_code="invalid_llm_request",
            )
        validated = []
        total_chars = 0
        for item in messages:
            if not isinstance(item, Mapping):
                raise SharedLLMBrokerError(
                    "message 必须是对象",
                    error_code="invalid_llm_request",
                )
            role = str(item.get("role") or "")
            if role not in {"system", "user", "assistant", "tool"}:
                raise SharedLLMBrokerError(
                    "message role 无效",
                    error_code="invalid_llm_request",
                )
            content = item.get("content", "")
            if not isinstance(content, (str, list)):
                raise SharedLLMBrokerError(
                    "message content 类型无效",
                    error_code="invalid_llm_request",
                )
            normalized = dict(item)
            serialized = _stable_json(normalized)
            total_chars += len(serialized)
            validated.append(normalized)
        return validated, total_chars

    @staticmethod
    def _validate_tools(tools: Optional[Sequence[Mapping[str, object]]]) -> Optional[list]:
        if tools is None:
            return None
        if not isinstance(tools, (list, tuple)) or len(tools) > 32:
            raise SharedLLMBrokerError(
                "tools 必须是最多 32 项的列表",
                error_code="invalid_llm_request",
            )
        validated = []
        for tool in tools:
            if not isinstance(tool, Mapping) or tool.get("type") != "function":
                raise SharedLLMBrokerError("只支持 function tools", error_code="invalid_llm_request")
            function = tool.get("function")
            if not isinstance(function, Mapping) or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]{0,63}", str(function.get("name") or "")
            ):
                raise SharedLLMBrokerError("tool function 无效", error_code="invalid_llm_request")
            validated.append(dict(tool))
        _stable_json(validated)
        return validated

    @staticmethod
    def _parse_tool_calls(message: Mapping[str, object]) -> Tuple[Mapping[str, object], ...]:
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise SharedLLMBrokerError("LLM tool_calls 无效", error_code="invalid_llm_response")
        result = []
        for raw in raw_calls:
            if not isinstance(raw, Mapping) or raw.get("type") != "function":
                raise SharedLLMBrokerError("LLM tool_call 类型无效", error_code="invalid_llm_response")
            function = raw.get("function")
            if not isinstance(function, Mapping):
                raise SharedLLMBrokerError("LLM tool_call function 无效", error_code="invalid_llm_response")
            name = str(function.get("name") or "")
            arguments_text = str(function.get("arguments") or "")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name):
                raise SharedLLMBrokerError("LLM tool_call name 无效", error_code="invalid_llm_response")
            try:
                arguments = json.loads(arguments_text)
            except json.JSONDecodeError as exc:
                raise SharedLLMBrokerError(
                    "LLM tool_call arguments 不是合法 JSON",
                    error_code="invalid_llm_response",
                ) from exc
            if not isinstance(arguments, Mapping):
                raise SharedLLMBrokerError(
                    "LLM tool_call arguments 必须是对象",
                    error_code="invalid_llm_response",
                )
            result.append(
                {
                    "id": str(raw.get("id") or ""),
                    "type": "function",
                    "function": {"name": name, "arguments": dict(arguments)},
                }
            )
        return tuple(result)

    def _audit_start(
        self,
        *,
        call_id: str,
        research_run_id: str,
        job_id: Optional[int],
        role_key: str,
        profile_key: str,
        model_id: str,
        prompt_sha256: str,
        request_id: str,
        started_at: str,
    ) -> bool:
        if self.connection is None:
            return False
        with self.audit_lock:
            self.connection.execute(
                """
                INSERT INTO llm_call_audit(
                    call_id, research_run_id, job_id, role_key, profile_key,
                    model_id, prompt_sha256, status, request_id, started_at
                ) VALUES(?, NULLIF(?, ''), ?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    call_id,
                    research_run_id,
                    job_id,
                    role_key,
                    profile_key,
                    model_id,
                    prompt_sha256,
                    request_id,
                    started_at,
                ),
            )
        return True

    def _audit_finish(
        self,
        call_id: str,
        *,
        status: str,
        response_sha256: str = "",
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        latency_ms: int,
        error_code: str = "",
    ) -> None:
        if self.connection is None:
            return
        with self.audit_lock:
            self.connection.execute(
                """
                UPDATE llm_call_audit
                SET status=?, response_sha256=?, input_tokens=?, output_tokens=?,
                    latency_ms=?, error_code=?,
                    completed_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE call_id=?
                """,
                (
                    status,
                    response_sha256,
                    input_tokens,
                    output_tokens,
                    max(0, int(latency_ms)),
                    error_code,
                    call_id,
                ),
            )

    def complete(
        self,
        messages: Sequence[Mapping[str, object]],
        *,
        profile: str = "fast",
        priority: str = "interactive_research",
        role_key: str = "",
        research_run_id: str = "",
        job_id: Optional[int] = None,
        request_id: str = "",
        response_schema: Optional[Mapping[str, object]] = None,
        tools: Optional[Sequence[Mapping[str, object]]] = None,
        tool_choice: object = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        timeout_seconds: Optional[int] = None,
        cancel_event=None,
    ) -> LLMCallResult:
        if profile not in self.profiles:
            raise SharedLLMBrokerError("未知 LLM profile", error_code="invalid_llm_request")
        if priority not in PRIORITY_ORDER:
            raise SharedLLMBrokerError("未知 LLM priority", error_code="invalid_llm_request")
        selected = self.profiles[profile]
        validated_messages, input_chars = self._validate_messages(messages)
        if input_chars > selected.max_input_chars:
            raise SharedLLMBrokerError(
                "LLM 上下文超过 profile 边界，未静默截断",
                error_code="llm_context_too_large",
            )
        validated_tools = self._validate_tools(tools)
        role = _safe_identifier(role_key, "role_key")
        run_id = _safe_identifier(research_run_id, "research_run_id")
        req_id = _safe_identifier(request_id, "request_id")
        if self.connection is None:
            raise SharedLLMBrokerError(
                "LLM 调用缺少审计数据库",
                error_code="llm_audit_unavailable",
            )
        if job_id is not None and (isinstance(job_id, bool) or int(job_id) < 1):
            raise SharedLLMBrokerError("job_id 无效", error_code="invalid_llm_request")
        output_tokens = selected.max_output_tokens if max_tokens is None else int(max_tokens)
        if output_tokens < 1 or output_tokens > selected.max_output_tokens:
            raise SharedLLMBrokerError("max_tokens 超出 profile 边界", error_code="invalid_llm_request")
        resolved_temperature = selected.temperature if temperature is None else float(temperature)
        if not 0 <= resolved_temperature <= 2:
            raise SharedLLMBrokerError("temperature 超出 0..2", error_code="invalid_llm_request")
        call_timeout = selected.timeout_seconds if timeout_seconds is None else int(timeout_seconds)
        if call_timeout < 1 or call_timeout > selected.timeout_seconds:
            raise SharedLLMBrokerError("timeout_seconds 超出 profile 边界", error_code="invalid_llm_request")
        validator = None
        if response_schema is not None:
            try:
                validator = Draft202012Validator(dict(response_schema))
                validator.check_schema(dict(response_schema))
            except (SchemaError, TypeError, ValueError) as exc:
                raise SharedLLMBrokerError(
                    "response_schema 无效",
                    error_code="invalid_llm_request",
                ) from exc

        runtime = self._runtime()
        payload: Dict[str, object] = {
            "model": str(runtime["model_id"]),
            "messages": validated_messages,
            "stream": False,
            "max_tokens": output_tokens,
            "temperature": resolved_temperature,
            "enable_thinking": selected.enable_thinking,
            # Ollama 原生/OpenAI 兼容端点统一兜底：无条件关闭思维链输出
            "think": False,
        }
        if validator is not None:
            payload["response_format"] = {"type": "json_object"}
        if validated_tools is not None:
            payload["tools"] = validated_tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
        request_fingerprint = {
            "profile": profile,
            "priority": priority,
            "role_key": role,
            "payload": payload,
            "response_schema": dict(response_schema) if response_schema is not None else None,
        }
        prompt_sha256 = _digest(request_fingerprint)
        call_id = str(uuid.uuid4())
        started_wall = self.clock()
        started_at = _utc_text(started_wall)
        started_tick = self.monotonic()
        deadline = started_tick + call_timeout
        try:
            try:
                self._audit_start(
                    call_id=call_id,
                    research_run_id=run_id,
                    job_id=int(job_id) if job_id is not None else None,
                    role_key=role,
                    profile_key=profile,
                    model_id=str(runtime["model_id"]),
                    prompt_sha256=prompt_sha256,
                    request_id=req_id,
                    started_at=started_at,
                )
            except Exception as exc:
                raise SharedLLMBrokerError(
                    "LLM 调用审计不可用",
                    error_code="llm_audit_unavailable",
                ) from exc
            if cancel_event is not None and cancel_event.is_set():
                raise SharedLLMCancelled()
            with self.slots.slot(
                PRIORITY_ORDER[priority],
                deadline=deadline,
                cancel_event=cancel_event,
                foreground=priority in {"chat_clarification", "chat_fact"},
            ):
                remaining = max(1, int(deadline - self.monotonic()))
                response = self.transport.request_openai_compatible(
                    payload,
                    runtime=runtime,
                    timeout_seconds=remaining,
                    max_retries=selected.max_retries,
                )
                if cancel_event is not None and cancel_event.is_set():
                    raise SharedLLMCancelled()
            try:
                body = response.json()
            except Exception as exc:
                raise SharedLLMBrokerError(
                    "本地 LLM 响应不是合法 JSON",
                    error_code="invalid_llm_response",
                ) from exc
            choices = body.get("choices") if isinstance(body, Mapping) else None
            choice = choices[0] if isinstance(choices, list) and choices else None
            message = choice.get("message") if isinstance(choice, Mapping) else None
            if not isinstance(message, Mapping):
                raise SharedLLMBrokerError(
                    "本地 LLM 响应缺少 message",
                    error_code="invalid_llm_response",
                )
            content = message.get("content")
            content = "" if content is None else str(content)
            tool_calls = self._parse_tool_calls(message)
            if not content.strip() and not tool_calls:
                raise SharedLLMBrokerError(
                    "本地 LLM 响应没有 content 或 tool_calls",
                    error_code="invalid_llm_response",
                )
            parsed = None
            if validator is not None:
                try:
                    parsed = json.loads(content)
                    validator.validate(parsed)
                except (json.JSONDecodeError, ValidationError) as exc:
                    raise SharedLLMBrokerError(
                        "本地 LLM 结构化输出不符合 Schema",
                        error_code="structured_output_invalid",
                    ) from exc
            usage = body.get("usage") if isinstance(body.get("usage"), Mapping) else {}
            input_tokens = _token_value(usage, "prompt_tokens")
            actual_output_tokens = _token_value(usage, "completion_tokens")
            response_fingerprint = {
                "content": content,
                "tool_calls": tool_calls,
                "finish_reason": str(choice.get("finish_reason") or ""),
            }
            response_sha256 = _digest(response_fingerprint)
            latency_ms = max(0, int((self.monotonic() - started_tick) * 1000))
            self._audit_finish(
                call_id,
                status="completed",
                response_sha256=response_sha256,
                input_tokens=input_tokens,
                output_tokens=actual_output_tokens,
                latency_ms=latency_ms,
            )
            result = LLMCallResult(
                call_id=call_id,
                profile_key=profile,
                provider_id="local",
                model_id=str(runtime["model_id"]),
                runtime_source="chat_api.get_chat_model_runtime_config(local)",
                content=content,
                parsed=parsed,
                tool_calls=tool_calls,
                finish_reason=str(choice.get("finish_reason") or ""),
                input_tokens=input_tokens,
                output_tokens=actual_output_tokens,
                latency_ms=latency_ms,
                response_sha256=response_sha256,
            )
            return result
        except Exception as exc:
            latency_ms = max(0, int((self.monotonic() - started_tick) * 1000))
            if isinstance(exc, SharedLLMBrokerError):
                broker_error = exc
            elif isinstance(exc, IntelLLMError):
                broker_error = SharedLLMBrokerError(
                    "本地 LLM 调用失败",
                    error_code=getattr(exc, "error_code", "llm_request_failed"),
                )
            else:
                broker_error = SharedLLMBrokerError(
                    "本地 LLM 调用失败",
                    error_code="llm_request_failed",
                )
            try:
                self._audit_finish(
                    call_id,
                    status="cancelled" if broker_error.error_code == "llm_cancelled" else "failed",
                    latency_ms=latency_ms,
                    error_code=broker_error.error_code,
                )
            except Exception:
                pass
            raise broker_error from exc

    def probe(self, *, timeout_seconds: int = 15) -> Dict[str, object]:
        result = self.complete(
            [{"role": "user", "content": "Reply with exactly OK."}],
            profile="fast",
            priority="interactive_research",
            role_key="broker_probe",
            request_id=f"probe-{uuid.uuid4().hex[:12]}",
            max_tokens=8,
            temperature=0,
            timeout_seconds=min(timeout_seconds, self.profiles["fast"].timeout_seconds),
        )
        identity = self.runtime_identity()
        return {
            **identity,
            "ready": bool(result.content.strip() or result.tool_calls),
            "call_id": result.call_id,
            "latency_ms": result.latency_ms,
        }


__all__ = [
    "LLMCallResult",
    "LLMProfile",
    "PRIORITY_ORDER",
    "SharedLLMBroker",
    "SharedLLMBrokerError",
    "SharedLLMCancelled",
    "SharedLLMTimeout",
]

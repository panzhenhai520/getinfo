import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import requests

from intel_llm_client import IntelLLMClient
from shared_llm_broker import SharedLLMBroker, SharedLLMBrokerError
from sqlite_database import SQLiteDatabase


RUNTIME = {
    "provider_id": "local",
    "name": "项目本地模型",
    "type": "openai",
    "base_url": "http://local-llm.example/v1",
    "api_key": "broker-test-secret-key",
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


def _body(content="OK", *, tool_calls=None, usage=None):
    message = {"content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message, "finish_reason": "stop"}],
        "usage": usage or {"prompt_tokens": 11, "completion_tokens": 3},
    }


class _BlockingTransport:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.order = []
        self.lock = threading.Lock()

    def request_openai_compatible(self, payload, **_kwargs):
        label = payload["messages"][-1]["content"]
        with self.lock:
            self.order.append(label)
        if label == "active":
            self.started.set()
            self.release.wait(3)
        return _Response(_body(label))


class SharedLLMBrokerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "broker.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.connection = self.database.connection

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _broker(self, *, body=None, session=None, connection=True, max_concurrency=2):
        actual_session = session or Mock()
        if session is None:
            actual_session.request.return_value = _Response(body or _body())
        transport = IntelLLMClient(
            provider="local",
            runtime_config_loader=lambda: dict(RUNTIME),
            session=actual_session,
            sleep=lambda _seconds: None,
        )
        broker = SharedLLMBroker(
            self.connection if connection else None,
            runtime_config_loader=lambda: dict(RUNTIME),
            transport=transport,
            max_concurrency=max_concurrency,
            clock=lambda: datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc),
        )
        return broker, actual_session

    def test_probe_and_runtime_identity_reuse_chat_local_configuration(self):
        broker, session = self._broker(body=_body("OK"))

        identity = broker.runtime_identity()
        probe = broker.probe(timeout_seconds=5)

        self.assertEqual(identity["base_url"], RUNTIME["base_url"])
        self.assertEqual(identity["model_id"], RUNTIME["model_id"])
        self.assertEqual(identity["runtime_source"], "chat_api.get_chat_model_runtime_config(local)")
        self.assertFalse(identity["api_key_exposed"])
        self.assertTrue(probe["ready"])
        call = session.request.call_args
        self.assertEqual(call.args[1], f"{RUNTIME['base_url']}/chat/completions")
        self.assertEqual(call.kwargs["json"]["model"], RUNTIME["model_id"])
        self.assertFalse(call.kwargs["json"]["stream"])
        audit = self.connection.execute(
            "SELECT model_id, prompt_sha256, response_sha256, status FROM llm_call_audit"
        ).fetchone()
        self.assertEqual(audit["model_id"], RUNTIME["model_id"])
        self.assertEqual(len(audit["prompt_sha256"]), 64)
        self.assertEqual(len(audit["response_sha256"]), 64)
        self.assertEqual(audit["status"], "completed")

    def test_structured_output_is_validated_and_invalid_result_is_audited(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["decision", "confidence"],
            "properties": {
                "decision": {"enum": ["hold", "review"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
        }
        broker, _ = self._broker(body=_body('{"decision":"hold","confidence":0.8}'))
        result = broker.complete(
            [{"role": "user", "content": "structured"}],
            response_schema=schema,
            role_key="risk_manager",
        )
        self.assertEqual(result.parsed, {"decision": "hold", "confidence": 0.8})

        invalid, _ = self._broker(body=_body('{"decision":"buy","confidence":2}'))
        with self.assertRaises(SharedLLMBrokerError) as caught:
            invalid.complete(
                [{"role": "user", "content": "invalid structured"}],
                response_schema=schema,
            )
        self.assertEqual(caught.exception.error_code, "structured_output_invalid")
        failed = self.connection.execute(
            "SELECT error_code FROM llm_call_audit ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(failed["error_code"], "structured_output_invalid")

    def test_tool_calls_are_returned_but_never_executed_by_broker(self):
        calls = [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "get_quote",
                    "arguments": '{"instrument_id":7}',
                },
            }
        ]
        broker, session = self._broker(body=_body("", tool_calls=calls))
        tool_definition = [
            {
                "type": "function",
                "function": {
                    "name": "get_quote",
                    "description": "Read a quote",
                    "parameters": {
                        "type": "object",
                        "properties": {"instrument_id": {"type": "integer"}},
                    },
                },
            }
        ]

        result = broker.complete(
            [{"role": "user", "content": "quote"}],
            tools=tool_definition,
            tool_choice="auto",
        )

        self.assertEqual(result.tool_calls[0]["function"]["name"], "get_quote")
        self.assertEqual(result.tool_calls[0]["function"]["arguments"], {"instrument_id": 7})
        self.assertEqual(session.request.call_count, 1)

    def test_deep_profile_accepts_long_context_without_silent_truncation(self):
        broker, session = self._broker(body=_body("long-context-ok"))
        long_text = "金融证据" * 12000

        result = broker.complete(
            [{"role": "user", "content": long_text}],
            profile="deep",
            max_tokens=100,
        )

        self.assertEqual(result.content, "long-context-ok")
        sent = session.request.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertEqual(sent, long_text)
        with self.assertRaises(SharedLLMBrokerError) as caught:
            broker.complete([{"role": "user", "content": long_text}], profile="fast")
        self.assertEqual(caught.exception.error_code, "llm_context_too_large")
        self.assertEqual(session.request.call_count, 1)

    def test_pre_cancelled_call_never_reaches_transport_and_is_audited(self):
        broker, session = self._broker()
        cancelled = threading.Event()
        cancelled.set()

        with self.assertRaises(SharedLLMBrokerError) as caught:
            broker.complete(
                [{"role": "user", "content": "cancel me"}],
                cancel_event=cancelled,
            )

        self.assertEqual(caught.exception.error_code, "llm_cancelled")
        self.assertEqual(session.request.call_count, 0)
        audit = self.connection.execute(
            "SELECT status, error_code FROM llm_call_audit ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(tuple(audit), ("cancelled", "llm_cancelled"))

    def test_priority_scheduler_serves_chat_fact_before_queued_batch(self):
        transport = _BlockingTransport()
        broker = SharedLLMBroker(
            self.connection,
            runtime_config_loader=lambda: dict(RUNTIME),
            transport=transport,
            max_concurrency=1,
            aging_seconds=60,
        )
        errors = []

        def invoke(label, priority):
            try:
                broker.complete(
                    [{"role": "user", "content": label}],
                    priority=priority,
                    timeout_seconds=10,
                )
            except Exception as exc:
                errors.append(exc)

        active = threading.Thread(target=invoke, args=("active", "interactive_research"))
        low = threading.Thread(target=invoke, args=("batch", "batch_reflection"))
        high = threading.Thread(target=invoke, args=("chat", "chat_fact"))
        active.start()
        self.assertTrue(transport.started.wait(2))
        low.start()
        for _ in range(100):
            if len(broker.slots.waiting) >= 1:
                break
            time.sleep(0.005)
        high.start()
        for _ in range(100):
            if len(broker.slots.waiting) >= 2:
                break
            time.sleep(0.005)
        transport.release.set()
        for thread in (active, low, high):
            thread.join(3)

        self.assertEqual(errors, [])
        self.assertEqual(transport.order, ["active", "chat", "batch"])

    def test_interactive_reserve_stays_available_while_slow_batch_is_running(self):
        class ReservedTransport:
            def __init__(self):
                self.batch_started = threading.Event()
                self.chat_started = threading.Event()
                self.release = threading.Event()
                self.order = []
                self.lock = threading.Lock()

            def request_openai_compatible(self, payload, **_kwargs):
                label = payload["messages"][-1]["content"]
                with self.lock:
                    self.order.append(label)
                if label.startswith("batch"):
                    self.batch_started.set()
                    self.release.wait(3)
                else:
                    self.chat_started.set()
                return _Response(_body(label))

        transport = ReservedTransport()
        broker = SharedLLMBroker(
            self.connection,
            runtime_config_loader=lambda: dict(RUNTIME),
            transport=transport,
            max_concurrency=2,
            interactive_reserve=1,
        )
        errors = []

        def invoke(label, priority):
            try:
                broker.complete(
                    [{"role": "user", "content": label}],
                    priority=priority,
                    timeout_seconds=10,
                )
            except Exception as exc:
                errors.append(exc)

        first = threading.Thread(target=invoke, args=("batch-1", "batch_reflection"))
        second = threading.Thread(target=invoke, args=("batch-2", "scheduled_research"))
        chat = threading.Thread(target=invoke, args=("chat", "chat_fact"))
        first.start()
        self.assertTrue(transport.batch_started.wait(2))
        second.start()
        deadline = time.monotonic() + 2
        while len(broker.slots.waiting) < 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(broker.slots.snapshot()["background_running"], 1)

        chat_started_at = time.monotonic()
        chat.start()
        self.assertTrue(transport.chat_started.wait(0.5))
        self.assertLess(time.monotonic() - chat_started_at, 0.5)
        self.assertFalse(transport.release.is_set())

        transport.release.set()
        for thread in (first, second, chat):
            thread.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(broker.slots.snapshot()["running"], 0)

    def test_timeout_has_stable_code_and_secret_never_enters_audit_or_error(self):
        session = Mock()
        session.request.side_effect = requests.Timeout(
            f"timeout Authorization Bearer {RUNTIME['api_key']}"
        )
        broker, _ = self._broker(session=session)

        with self.assertRaises(SharedLLMBrokerError) as caught:
            broker.complete(
                [{"role": "user", "content": "timeout"}],
                timeout_seconds=2,
            )

        self.assertEqual(caught.exception.error_code, "llm_timeout")
        self.assertNotIn(RUNTIME["api_key"], str(caught.exception))
        self.assertGreaterEqual(session.request.call_count, 1)
        self.assertLessEqual(session.request.call_count, 2)
        self.assertTrue(
            all(call.kwargs["timeout"] <= 2 for call in session.request.call_args_list)
        )
        audit = dict(
            self.connection.execute(
                "SELECT * FROM llm_call_audit ORDER BY id DESC LIMIT 1"
            ).fetchone()
        )
        self.assertEqual(audit["error_code"], "llm_timeout")
        self.assertNotIn(RUNTIME["api_key"], json.dumps(audit, ensure_ascii=False))

    def test_every_call_requires_existing_audit_store_and_no_cloud_fallback(self):
        no_audit, session = self._broker(connection=False)
        with self.assertRaises(SharedLLMBrokerError) as caught:
            no_audit.complete(
                [{"role": "user", "content": "research"}],
            )
        self.assertEqual(caught.exception.error_code, "llm_audit_unavailable")
        self.assertEqual(session.request.call_count, 0)

        cloud_runtime = {**RUNTIME, "provider_id": "openai"}
        transport = Mock()
        broker = SharedLLMBroker(
            self.connection,
            runtime_config_loader=lambda: dict(cloud_runtime),
            transport=transport,
        )
        with self.assertRaises(SharedLLMBrokerError) as cloud_error:
            broker.complete([{"role": "user", "content": "do not fallback"}])
        self.assertEqual(cloud_error.exception.error_code, "unauthorized_llm_provider")
        transport.request_openai_compatible.assert_not_called()


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import chat_api
from chat_route_orchestrator import (
    ChatFinancialRouteStore,
    ChatRouteOrchestrator,
)
from sqlite_database import SQLiteDatabase


UTC = timezone.utc


def _at(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _payload(question, *, session_id="session-time-1", timezone_name="Asia/Hong_Kong"):
    return {
        "session_id": session_id,
        "model": "local",
        "messages": [{"role": "user", "content": question}],
        "web_search": False,
        "user_timezone": timezone_name,
    }


class ChatServerTimeContextTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "chat-time.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.store = ChatFinancialRouteStore(self.database)

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def test_today_is_resolved_from_one_server_clock_read(self):
        calls = []

        def clock():
            calls.append(True)
            return _at("2026-07-31T15:30:00Z")

        plan = ChatRouteOrchestrator(clock=clock).plan(_payload("今天市场如何"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(plan.server_time_context["clock_source"], "application_server")
        self.assertEqual(plan.server_time_context["server_now_utc"], "2026-07-31T15:30:00Z")
        resolved = plan.time_resolution["primary_range"]
        self.assertEqual(resolved["start_utc"], "2026-07-30T16:00:00Z")
        self.assertEqual(resolved["end_utc"], "2026-07-31T15:30:00Z")

    def test_yesterday_this_week_and_now_are_absolute_ranges(self):
        orchestrator = ChatRouteOrchestrator(clock=lambda: _at("2026-07-31T15:30:00Z"))
        yesterday = orchestrator.plan(_payload("昨天发生什么"))
        self.assertEqual(
            yesterday.time_resolution["primary_range"]["start_utc"],
            "2026-07-29T16:00:00Z",
        )
        self.assertEqual(
            yesterday.time_resolution["primary_range"]["end_utc"],
            "2026-07-30T16:00:00Z",
        )
        week = orchestrator.plan(_payload("本周走势"))
        self.assertEqual(
            week.time_resolution["primary_range"]["start_utc"],
            "2026-07-26T16:00:00Z",
        )
        now = orchestrator.plan(_payload("此时价格"))
        self.assertEqual(
            now.time_resolution["primary_range"]["start_utc"],
            "2026-07-31T15:30:00Z",
        )
        self.assertEqual(
            now.time_resolution["primary_range"]["start_utc"],
            now.time_resolution["primary_range"]["end_utc"],
        )

    def test_user_timezone_is_hint_and_client_clock_cannot_override_server(self):
        payload = _payload("today market", timezone_name="America/New_York")
        payload["client_now"] = "2099-01-01T00:00:00Z"
        plan = ChatRouteOrchestrator(
            clock=lambda: _at("2026-07-31T15:30:00Z")
        ).plan(payload)
        self.assertEqual(plan.server_time_context["user_timezone_source"], "client_hint")
        self.assertEqual(
            plan.time_resolution["primary_range"]["start_utc"],
            "2026-07-31T04:00:00Z",
        )
        self.assertNotIn("2099", json.dumps(plan.to_internal_dict()))

        invalid = ChatRouteOrchestrator(
            clock=lambda: _at("2026-07-31T15:30:00Z")
        ).plan(_payload("今天", timezone_name="Mars/Olympus"))
        self.assertEqual(
            invalid.server_time_context["user_timezone_source"],
            "invalid_hint_fallback_server",
        )
        self.assertEqual(invalid.server_time_context["user_timezone"], "Asia/Hong_Kong")

    def test_naive_server_clock_is_rejected_before_persistence(self):
        orchestrator = ChatRouteOrchestrator(
            clock=lambda: datetime(2026, 7, 31, 12, 0), store=self.store
        )
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            orchestrator.plan(_payload("今天"))
        count = self.database.connection.execute(
            "SELECT COUNT(*) FROM chat_financial_routes"
        ).fetchone()[0]
        self.assertEqual(int(count), 0)

    def test_context_and_resolved_range_are_persisted_on_existing_table(self):
        payload = _payload("今天上证怎么样", session_id="persisted-session")
        orchestrator = ChatRouteOrchestrator(
            clock=lambda: _at("2026-07-31T02:00:00Z"), store=self.store
        )
        plan = orchestrator.plan(payload)
        persisted = orchestrator.persist(plan, payload)
        self.assertEqual(persisted["status"], "persisted")
        row = self.database.connection.execute(
            """
            SELECT session_id, raw_question, server_now, server_timezone,
                   route_status, route_destination, financial_attributes_json
            FROM chat_financial_routes WHERE id=?
            """,
            (persisted["route_id"],),
        ).fetchone()
        attributes = json.loads(row[6])
        self.assertEqual(str(row[0]), "persisted-session")
        self.assertEqual(str(row[1]), "今天上证怎么样")
        self.assertEqual(str(row[2]), "2026-07-31T02:00:00Z")
        self.assertEqual(str(row[3]), "Asia/Hong_Kong")
        self.assertEqual(str(row[4]), "time_context_captured")
        self.assertEqual(str(row[5]), "normal_chat")
        self.assertEqual(
            attributes["time_resolution"]["primary_range"]["resolved_at_utc"],
            "2026-07-31T02:00:00Z",
        )

    def test_follow_up_inherits_saved_absolute_range_across_midnight(self):
        times = iter((_at("2026-07-31T15:59:00Z"), _at("2026-07-31T16:01:00Z")))
        orchestrator = ChatRouteOrchestrator(clock=lambda: next(times), store=self.store)
        first_payload = _payload("今天腾讯怎样", session_id="cross-midnight")
        first = orchestrator.plan(first_payload)
        orchestrator.persist(first, first_payload)
        second_payload = {
            **_payload("继续详细分析", session_id="cross-midnight"),
            "messages": [
                {"role": "user", "content": "今天腾讯怎样"},
                {"role": "assistant", "content": "上一轮回答"},
                {"role": "user", "content": "继续详细分析"},
            ],
        }
        second = orchestrator.plan(second_payload)
        self.assertEqual(second.server_time_context["server_now_utc"], "2026-07-31T16:01:00Z")
        self.assertTrue(second.time_resolution["inherited"])
        self.assertEqual(
            second.time_resolution["primary_range"],
            first.time_resolution["primary_range"],
        )
        self.assertEqual(
            second.time_resolution["inherited_from_route_key"],
            first.audit_route_key,
        )

    def test_chat_endpoint_persists_context_without_changing_sse(self):
        orchestrator = ChatRouteOrchestrator(
            clock=lambda: _at("2026-07-31T02:00:00Z"), store=self.store
        )
        app = Flask(__name__)
        app.config.update(TESTING=True)
        app.register_blueprint(chat_api.chat_bp)
        client = app.test_client()
        # /api/chat/send 带 @login_required（问答 run 归属需要登录身份），本用例断言的是
        # 旧链路 SSE 契约，所以这里显式给一个管理员会话。
        auth_patcher = patch(
            "decorators.user_db.verify_session",
            return_value={"user_id": 1, "username": "tester", "role": "admin"},
        )
        auth_patcher.start()
        self.addCleanup(auth_patcher.stop)
        client.set_cookie("localhost", "session_token", "test-session-token")
        # 统一 QA 网关可用时会接管 /api/chat/send（不再走本文件校验的旧链路 SSE），
        # 本用例测的是该网关不可用时的回落链路，所以固定为不可用。
        unified_patcher = patch.object(chat_api, "_unified_qa_available", return_value=False)
        unified_patcher.start()
        self.addCleanup(unified_patcher.stop)
        config = {
            "models": {
                "local": {
                    "api_key": "fixture",
                    "model_id": "fixture-model",
                    "base_url": "http://local.example/v1",
                    "use_proxy": False,
                }
            }
        }
        with patch.object(chat_api, "chat_route_orchestrator", orchestrator), patch.object(
            chat_api, "_load_config", return_value=config
        ), patch.object(chat_api, "_save_chat_metric", return_value=None), patch.object(
            chat_api, "_stream_openai", return_value=iter(("回答",))
        ):
            response = client.post(
                "/api/chat/send",
                json=_payload("今天市场", session_id="api-time-session"),
            )
            body = response.get_data(as_text=True)
        self.assertIn('"type": "chunk"', body)
        self.assertIn('"type": "done"', body)
        row = self.database.connection.execute(
            "SELECT server_now, session_id FROM chat_financial_routes"
        ).fetchone()
        self.assertEqual(tuple(row), ("2026-07-31T02:00:00Z", "api-time-session"))


if __name__ == "__main__":
    unittest.main()

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

import chat_api
import config
from chat_route_orchestrator import ChatFinancialRouteStore, ChatRouteOrchestrator
from financial_chat_market_scope import (
    FinancialMarketScopeRouter,
    format_market_scope_answer,
    validate_market_scope,
)
from financial_instruments import InstrumentRegistry
from financial_intent_classifier import FinancialIntentClassifier
from financial_market_scheduler import FinancialMarketJobService, FinancialMarketScheduler
from financial_target_resolver import FinancialTargetResolver
from financial_worker_jobs import FINANCIAL_JOB_TYPES, FinancialJobDispatcher
from intel_database import IntelRepository
from intel_worker import IntelWorker
from sqlite_database import SQLiteDatabase


UTC = timezone.utc
BASE_FINANCIAL_SETTINGS = {
    "FINANCIAL_INTELLIGENCE_ENABLED": True,
    "TRADING_AGENTS_ENABLED": False,
    "FINANCIAL_AUTO_RESEARCH_ENABLED": False,
    "TRADING_SIMULATION_ENABLED": False,
    "FINANCIAL_QUOTE_FRESHNESS_SECONDS": 300,
}


def _at(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _payload(question, session_id="market-scope-session"):
    return {
        "session_id": session_id,
        "model": "local",
        "messages": [{"role": "user", "content": question}],
        "web_search": True,
        "user_timezone": "Asia/Hong_Kong",
    }


def _events(response):
    return [
        json.loads(block[5:].strip())
        for block in response.get_data(as_text=True).split("\n\n")
        if block.startswith("data:")
    ]


class _PersistingFixtureRouter:
    def __init__(self, connection):
        self.connection = connection
        self.calls = []

    def fetch_and_persist(
        self, request, *, candidate_provider_ids, allow_fallback
    ):
        self.calls.append(request)
        provider_key = "chat_market_scope_fixture"
        self.connection.execute(
            """
            INSERT INTO financial_provider_profiles(
                provider_key, display_name, provider_type, access_tier,
                capabilities_json, is_enabled
            ) VALUES(?, 'Chat scope fixture', 'fixture', 'test', '[]', 1)
            ON CONFLICT(provider_key) DO UPDATE SET is_enabled=1
            """,
            (provider_key,),
        )
        provider_id = int(
            self.connection.execute(
                "SELECT id FROM financial_provider_profiles WHERE provider_key=?",
                (provider_key,),
            ).fetchone()[0]
        )
        normalized = {
            "fixture": True,
            "instrument_id": int(request.instrument_id),
            "exchange": str(request.parameters.get("exchange") or ""),
        }
        stored = {
            "provider_id": provider_key,
            "endpoint": request.endpoint,
            "data_kind": request.data_kind.value,
            "metric": request.metric,
            "value": 100.0 + int(request.instrument_id),
            "normalized_payload": normalized,
        }
        payload_text = json.dumps(
            stored,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload_hash = hashlib.sha256(payload_text.encode()).hexdigest()
        snapshot_key = hashlib.sha256(
            f"{provider_key}|{request.request_id}|{request.data_kind.value}".encode()
        ).hexdigest()
        observed = request.requested_as_of.astimezone(UTC).isoformat().replace(
            "+00:00", "Z"
        )
        self.connection.execute(
            """
            INSERT INTO financial_data_snapshots(
                snapshot_key, instrument_id, provider_profile_id, data_type,
                interval_code, observed_at, fetched_at, market_status,
                currency, timezone, quality_status, payload_json,
                payload_sha256, source_url, request_id
            ) VALUES(?, ?, ?, ?, '', ?, ?, 'open', '', 'UTC',
                     'normalized_fixture', ?, ?, 'fixture://chat-scope', ?)
            """,
            (
                snapshot_key,
                int(request.instrument_id),
                provider_id,
                request.data_kind.value,
                observed,
                observed,
                payload_text,
                payload_hash,
                request.request_id,
            ),
        )
        snapshot_id = int(
            self.connection.execute(
                "SELECT id FROM financial_data_snapshots WHERE snapshot_key=?",
                (snapshot_key,),
            ).fetchone()[0]
        )
        return (
            SimpleNamespace(
                provider_id=provider_key,
                degradation=SimpleNamespace(degraded=False, reason=""),
            ),
            (snapshot_id,),
        )


class FinancialChatMarketScopeTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(
            str(Path(self.temp_dir.name) / "financial-chat-market-scope.sqlite3")
        )
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.repository = IntelRepository(self.database)
        self.scheduler = FinancialMarketScheduler(
            self.repository, settings=BASE_FINANCIAL_SETTINGS
        )
        self.market_router = FinancialMarketScopeRouter(
            self.repository,
            self.scheduler,
            settings=BASE_FINANCIAL_SETTINGS,
        )
        registry = InstrumentRegistry(self.database.connection)
        registry.load_controlled_seed()
        self.orchestrator = ChatRouteOrchestrator(
            clock=lambda: _at("2026-07-31T02:00:00Z"),
            store=ChatFinancialRouteStore(self.database),
            intent_classifier=FinancialIntentClassifier(registry),
            target_resolver=FinancialTargetResolver(registry),
            market_scope_router=self.market_router,
            financial_settings=BASE_FINANCIAL_SETTINGS,
        )

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _run_existing_worker(self):
        fixture_router = _PersistingFixtureRouter(self.database.connection)
        service = FinancialMarketJobService(
            self.repository,
            settings=BASE_FINANCIAL_SETTINGS,
            router=fixture_router,
        )
        dispatcher = FinancialJobDispatcher(
            service.runners(), settings=BASE_FINANCIAL_SETTINGS
        )
        # IntelWorker.__init__ 用全局 config 的 FINANCIAL_INTELLIGENCE_ENABLED /
        # TRADING_AGENTS_ENABLED 判断要不要挂上「注入的」financial_dispatcher：
        # 本机 .env 两项都是 false，不显式打开的话注入的 dispatcher 会被丢掉，
        # worker 不注册任何金融 handler，run_once 领不到作业（completed=0）。
        # 这里只影响构造期，和本用例要验证的「一次刷新只排一个 universe」无关。
        with patch.object(config, "FINANCIAL_INTELLIGENCE_ENABLED", True):
            worker = IntelWorker(
                repository=self.repository,
                worker_id="chat-market-scope-fixture-worker",
                financial_dispatcher=dispatcher,
                financial_market_scheduler=self.scheduler,
                # Fixture jobs finish synchronously; a long heartbeat interval
                # avoids test-only concurrent SQLite probes while still exercising
                # the production worker dispatcher and lease completion path.
                heartbeat_seconds=10.0,
                job_lease_seconds=30,
            )
        worker.enqueue_due_periodic_jobs = lambda: None
        stats = worker.run_once(job_types=FINANCIAL_JOB_TYPES, limit=100)
        return fixture_router, stats

    def test_simplified_traditional_oral_and_omitted_scope_mapping(self):
        cases = {
            "上证怎么样": "CN_XSHG_MARKET",
            "上證怎麼樣": "CN_XSHG_MARKET",
            "上海股市今天如何": "CN_XSHG_MARKET",
            "深证行情": "CN_XSHE_MARKET",
            "深證現在怎麼樣": "CN_XSHE_MARKET",
            "深圳股市": "CN_XSHE_MARKET",
            "A股现在怎么样": "CN_A_MARKET",
            "中國股市今日如何": "CN_A_MARKET",
            "港市今日如何": "HK_MARKET",
            "香港市場怎麼樣": "HK_MARKET",
            "今天大盤如何": "DEFAULT_MARKET_PULSE",
            "現在股市怎麼樣": "DEFAULT_MARKET_PULSE",
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                plan = self.orchestrator.plan(_payload(question, question))
                self.assertEqual(plan.financial_intent["intent"], "market_overview")
                self.assertEqual(plan.market_scope["status"], "planned")
                self.assertEqual(
                    plan.market_scope["universe"]["universe_key"], expected
                )

        exact = self.orchestrator.plan(_payload("上证指数怎么样", "exact-index"))
        self.assertEqual(exact.target_resolution["status"], "resolved")
        self.assertEqual(
            exact.target_resolution["targets"][0]["canonical_symbol"], "000001.SH"
        )
        self.assertEqual(exact.market_scope["status"], "skipped")

    def test_interactive_scope_queues_only_one_universe_and_reuses_worker(self):
        plan = self.orchestrator.plan(_payload("今天A股怎么样"))
        queued = self.orchestrator.activate_market_scope(plan)
        scope = validate_market_scope(queued.market_scope)
        self.assertEqual(scope["status"], "refresh_queued")
        self.assertFalse(scope["answer_allowed"])
        self.assertEqual(scope["refresh"]["full_research_jobs_created"], 0)
        jobs = [
            self.repository.get_job(item["job_id"])
            for item in scope["refresh"]["jobs"]
        ]
        self.assertEqual(sum(job["job_type"] == "financial_snapshot" for job in jobs), 6)
        self.assertEqual(sum(job["job_type"] == "market_overview" for job in jobs), 1)
        self.assertEqual(
            {job["created_by"] for job in jobs}, {"financial_chat_market_scope"}
        )
        self.assertEqual(
            int(
                self.database.connection.execute(
                    "SELECT COUNT(*) FROM intel_jobs WHERE job_type='financial_research'"
                ).fetchone()[0]
            ),
            0,
        )

        fixture, stats = self._run_existing_worker()
        self.assertEqual(stats["completed"], 7)
        self.assertEqual(len(fixture.calls), 6)
        ready = self.orchestrator.activate_market_scope(plan).market_scope
        self.assertEqual(ready["status"], "ready")
        self.assertTrue(ready["answer_allowed"])
        self.assertEqual(ready["report"]["coverage"], 1.0)
        self.assertEqual(len(ready["report"]["snapshot_ids"]), 6)
        self.assertIn("上证综指", ready["universe"]["constituent_basis"])
        self.assertEqual(ready["refresh"]["created"], 0)
        saved = self.orchestrator.persist(
            self.orchestrator.activate_market_scope(plan), _payload("今天A股怎么样")
        )
        route_status = self.database.connection.execute(
            "SELECT route_status FROM chat_financial_routes WHERE id=?",
            (saved["route_id"],),
        ).fetchone()[0]
        self.assertEqual(route_status, "market_scope_ready")

    def test_partial_data_is_answerable_only_after_snapshots_and_discloses_gaps(self):
        plan = self.orchestrator.plan(_payload("香港股市怎么样", "hk-partial"))
        pending = self.orchestrator.activate_market_scope(plan).market_scope
        self.assertFalse(pending["answer_allowed"])
        self._run_existing_worker()
        ready = self.orchestrator.activate_market_scope(plan).market_scope
        self.assertTrue(ready["answer_allowed"])
        self.assertAlmostEqual(ready["report"]["coverage"], 2 / 3)
        self.assertEqual(ready["report"]["missing_market_metrics"], ["breadth:XHKG"])
        answer = format_market_scope_answer(ready)
        self.assertIn("证据覆盖 2/3", answer)
        self.assertIn("breadth:XHKG", answer)
        self.assertIn("不是完整多代理研究", answer)

    def test_current_report_cannot_launder_stale_snapshot_evidence(self):
        plan = self.orchestrator.plan(_payload("今天A股怎么样", "stale-evidence"))
        self.orchestrator.activate_market_scope(plan)
        self._run_existing_worker()
        self.database.connection.execute(
            "UPDATE financial_data_snapshots SET fetched_at='2026-07-30T00:00:00Z'"
        )
        rejected = self.orchestrator.activate_market_scope(plan).market_scope
        self.assertEqual(rejected["status"], "refresh_queued")
        self.assertFalse(rejected["answer_allowed"])
        self.assertEqual(rejected["report"], {})

    def test_close_settlement_holiday_and_cross_market_sessions_never_block(self):
        settling_scheduler = FinancialMarketScheduler(
            self.repository, settings=BASE_FINANCIAL_SETTINGS
        )
        settling = settling_scheduler.enqueue_scope_refresh(
            "CN_A_MARKET", now=_at("2026-07-31T07:03:00Z")
        )
        self.assertEqual(settling["status"], "scheduled")
        self.assertIn("post_close_settling", {
            self.repository.get_job(item["job_id"])["payload"].get("phase")
            for item in settling["jobs"]
        })

        holiday = settling_scheduler.enqueue_scope_refresh(
            "DEFAULT_MARKET_PULSE", now=_at("2026-02-20T02:00:00Z")
        )
        self.assertEqual(
            holiday["market_sessions"]["XSHG"]["reason"], "exchange_holiday"
        )
        self.assertEqual(
            holiday["market_sessions"]["XHKG"]["market_session_state"], "open"
        )
        self.assertGreater(len(holiday["jobs"]), 0)

    def test_chat_sse_never_calls_model_or_web_search_before_persisted_report(self):
        app = Flask(__name__)
        app.config.update(TESTING=True)
        app.register_blueprint(chat_api.chat_bp)
        client = app.test_client()
        # /api/chat/send 带 @login_required（统一 QA 网关按登录身份归属问答 run）：
        # 未登录时接口返回 401 JSON，SSE 一个事件都没有。这里给一个管理员会话
        # （与 tests/test_chat_sse_compatibility.py 同一套夹具做法）。
        auth = patch(
            "decorators.user_db.verify_session",
            return_value={"user_id": 1, "username": "tester", "role": "admin"},
        )
        auth.start()
        self.addCleanup(auth.stop)
        client.set_cookie("localhost", "session_token", "test-session-token")
        payload = _payload("今天A股怎么样", "chat-pending")
        # 本用例验证的是「旧版金融路由」这条链路（chat_route_orchestrator + market scope 文案：
        # 不输出行情数值 / 证据覆盖 N/N / 报告编号）。统一 QA 网关接管后，同一入口会先走
        # QA 链路，而且它的运行态（并发 run）存在共享库里，会随别的运行残留变成 429，
        # 用例结果就随环境漂移。这里把被测链路钉死为旧版金融路由。
        with patch.object(chat_api, "_unified_qa_available", return_value=False), patch.object(
            chat_api, "chat_route_orchestrator", self.orchestrator
        ), patch.object(
            chat_api, "_stream_openai"
        ) as model, patch.object(chat_api, "_web_search") as web, patch.object(
            chat_api, "_load_config"
        ) as config_loader:
            pending_events = _events(client.post("/api/chat/send", json=payload))
        self.assertEqual(
            [item["type"] for item in pending_events], ["status", "chunk", "done"]
        )
        self.assertIn("不输出行情数值", pending_events[1]["content"])
        model.assert_not_called()
        web.assert_not_called()
        config_loader.assert_not_called()

        self._run_existing_worker()
        with patch.object(chat_api, "_unified_qa_available", return_value=False), patch.object(
            chat_api, "chat_route_orchestrator", self.orchestrator
        ), patch.object(
            chat_api, "_stream_openai"
        ) as model, patch.object(chat_api, "_web_search") as web:
            ready_events = _events(client.post("/api/chat/send", json=payload))
        self.assertEqual(
            [item["type"] for item in ready_events], ["status", "chunk", "done"]
        )
        self.assertIn("证据覆盖 6/6", ready_events[1]["content"])
        self.assertIn("报告编号", ready_events[1]["content"])
        model.assert_not_called()
        web.assert_not_called()


if __name__ == "__main__":
    unittest.main()

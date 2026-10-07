# -*- coding: utf-8 -*-
"""IntelWorker 依赖注入契约回归测试：显式注入优先，且行情调度器属性必须始终存在。

生产现象（2026-10-06 探针复现）：
  1. `IntelWorker.__init__` 的 `financial_market_scheduler` 形参**从未被赋值**——
     财务开关打开时 `self.financial_market_scheduler` 属性根本不存在，
     周期行情调度（enqueue_due_periodic_jobs 里）会 AttributeError，`enqueue_financial_market_event`
     也直接抛异常；三个用例传进来的 scheduler 是空操作。
  2. 财务总开关全关时，显式注入的 `financial_dispatcher` 被静默丢弃（先判开关、后判注入），
     调用方以为注册了 handler，实际一个都没注册。
本用例把"注入优先"和"属性恒存在"两条契约钉死。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from financial_market_scheduler import FinancialMarketScheduler
from financial_worker_jobs import FinancialJobDispatcher
from intel_database import IntelRepository
from intel_worker import IntelWorker
from sqlite_database import SQLiteDatabase


class _StubRunner:
    """最小 runner：只记录被调用。"""


class _StubDispatcher:
    def __init__(self):
        self.registered_with = []
        self.runners = {}

    def register_with(self, worker):
        self.registered_with.append(worker)


class IntelWorkerInjectionContractTest(unittest.TestCase):
    def setUp(self):
        self._patches = [
            patch.object(config, "DATABASE_TYPE", "sqlite"),
            patch("db_connection.database_type", lambda: "sqlite"),
        ]
        for item in self._patches:
            item.start()
            self.addCleanup(item.stop)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "worker-injection.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.repository = IntelRepository(self.database)

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def test_injected_dispatcher_wins_even_when_financial_switches_are_off(self):
        """显式注入必须优先于环境开关（开关只决定是否自动装配）。"""
        dispatcher = _StubDispatcher()
        with patch.object(config, "FINANCIAL_INTELLIGENCE_ENABLED", False), \
                patch.object(config, "TRADING_AGENTS_ENABLED", False):
            worker = IntelWorker(
                repository=self.repository,
                worker_id="injection-off",
                financial_dispatcher=dispatcher,
            )
        self.assertIs(dispatcher, worker.financial_dispatcher,
                      "财务开关关闭时不得丢弃显式注入的 dispatcher")
        self.assertEqual([worker], dispatcher.registered_with,
                         "注入的 dispatcher 必须完成 register_with（否则 handler 注册不上）")

    def test_market_scheduler_attribute_always_exists(self):
        """无论开关如何，financial_market_scheduler 属性都必须存在（否则属性访问即崩）。"""
        for enabled in (True, False):
            with patch.object(config, "FINANCIAL_INTELLIGENCE_ENABLED", enabled), \
                    patch.object(config, "TRADING_AGENTS_ENABLED", enabled):
                worker = IntelWorker(
                    repository=self.repository,
                    worker_id="scheduler-attr-%s" % enabled,
                )
            self.assertTrue(hasattr(worker, "financial_market_scheduler"),
                            "财务开关=%s 时属性缺失 → 周期行情调度与事件入队都会 AttributeError" % enabled)

    def test_injected_market_scheduler_is_used(self):
        """注入的行情调度器必须被采用，而不是被忽略或覆盖。"""
        injected = FinancialMarketScheduler(self.repository, settings=config)
        with patch.object(config, "FINANCIAL_INTELLIGENCE_ENABLED", True), \
                patch.object(config, "TRADING_AGENTS_ENABLED", True):
            worker = IntelWorker(
                repository=self.repository,
                worker_id="scheduler-injected",
                financial_market_scheduler=injected,
            )
        self.assertIs(injected, worker.financial_market_scheduler)

    def test_market_event_enqueue_does_not_raise_when_disabled(self):
        """财务关闭时事件入队返回 skipped，而不是抛 AttributeError。"""
        with patch.object(config, "FINANCIAL_INTELLIGENCE_ENABLED", False), \
                patch.object(config, "TRADING_AGENTS_ENABLED", False):
            worker = IntelWorker(repository=self.repository, worker_id="event-disabled")
        result = worker.enqueue_financial_market_event("probe-event")
        self.assertTrue(result.get("skipped"))
        self.assertEqual("financial market disabled", result.get("reason"))


if __name__ == "__main__":
    unittest.main()

import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from jsonschema import ValidationError

import config
from chat_route_orchestrator import chat_route_orchestrator
from financial_instruments import InstrumentRegistry
from financial_latest_bundle import (
    FinancialLatestBundleService,
    format_latest_bundle_answer,
    plan_latest_bundle,
    validate_latest_bundle,
)
from financial_news_query import FinancialNewsQueryService
from financial_realtime_query import skipped_realtime_query, validate_realtime_query
from sqlite_database import SQLiteDatabase


UTC = timezone.utc
NOW = datetime(2026, 8, 3, 4, 0, tzinfo=UTC)
SERVER_CONTEXT = {
    "server_now_utc": "2026-08-03T04:00:00Z",
    "server_timezone": "Asia/Hong_Kong",
    "user_timezone": "Asia/Hong_Kong",
}


class _QuoteService:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def execute(self, query):
        self.calls += 1
        return self.result


class _TimedService(_QuoteService):
    def __init__(self, result, *, delay=0.0, barrier=None):
        super().__init__(result)
        self.delay = delay
        self.barrier = barrier

    def execute(self, query):
        self.calls += 1
        if self.barrier is not None:
            self.barrier.wait(timeout=0.5)
        time.sleep(self.delay)
        return self.result


class FinancialLatestBundleTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(
            str(Path(self.temp_dir.name) / "latest-bundle.sqlite3")
        )
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.connection = self.database.connection
        self.instrument = InstrumentRegistry(self.connection).upsert_instrument(
            {
                "canonical_symbol": "SPCX.US",
                "display_name": "Space Exploration Technologies Corp.",
                "asset_type": "equity",
                "market": "US",
                "exchange": "XNAS",
                "currency": "USD",
                "country_code": "US",
                "listed_at": "2026-06-12",
                "provider_mappings": {"yahoo": "SPCX"},
                "aliases": ["SPCX", "SpaceX", "Space Exploration Technologies"],
            }
        )
        self.target = self.instrument.to_dict()
        self.target_resolution = {
            "status": "resolved",
            "targets": [self.target],
        }
        self.news = FinancialNewsQueryService(
            self.database,
            settings={
                "FINANCIAL_INTELLIGENCE_ENABLED": True,
                "FINANCIAL_LATEST_NEWS_ENABLED": True,
                "FINANCIAL_NEWS_LOOKBACK_DAYS": 7,
            },
            clock=lambda: NOW,
        )

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _article(
        self,
        url,
        title,
        *,
        published_at,
        precision="instant",
        timezone_name="UTC",
        content="SpaceX 发布公司更新。",
        first_crawled="2026-08-03T03:10:00Z",
    ):
        self.connection.execute(
            """
            INSERT INTO articles(
                url, canonical_url, title, content, domain, publish_date,
                published_at_utc, published_timezone, published_precision,
                published_time_source, first_crawled, status
            ) VALUES(?, ?, ?, ?, 'news.example.test', ?, ?, ?, ?,
                     'publisher_metadata', ?, 'active')
            """,
            (
                url,
                url.split("?", 1)[0],
                title,
                content,
                published_at[:10],
                published_at,
                timezone_name,
                precision,
                first_crawled,
            ),
        )

    def _news_plan(self):
        return self.news.plan(
            self.target_resolution,
            SERVER_CONTEXT,
            {"channels": ["quote", "news"]},
        )

    def _quote_result(self, *, status="ready", evidence=True):
        query = skipped_realtime_query("fixture")
        query.update(
            {
                "status": status,
                "target": self.target,
                "requested_at_utc": "2026-08-03T04:00:00.000Z",
                "completed_at_utc": "2026-08-03T04:00:00.000Z",
                "market_session": {
                    "market_calendar_id": "XNAS",
                    "market_timezone": "America/New_York",
                    "market_session_state": "closed",
                    "calendar_version": "official-2026-v2",
                    "calendar_source": "official",
                },
                "evidence": (
                    [
                        {
                            "snapshot_id": 91,
                            "provider_id": "yahoo",
                            "provider_display_name": "Yahoo Finance",
                            "source_url": "https://finance.yahoo.com/quote/SPCX/",
                            "observed_at": "2026-08-01T20:00:00.000Z",
                            "fetched_at": "2026-08-01T20:00:02.000Z",
                            "market_status": "closed",
                            "currency": "USD",
                            "price": 42.5,
                            "change": 0.5,
                            "change_percent": 1.19,
                        },
                        {
                            "snapshot_id": 92,
                            "provider_id": "alpha_vantage",
                            "provider_display_name": "Alpha Vantage",
                            "source_url": "https://example.test/alpha/SPCX",
                            "observed_at": "2026-08-01T20:00:00.000Z",
                            "fetched_at": "2026-08-01T20:00:03.000Z",
                            "market_status": "closed",
                            "currency": "USD",
                            "price": 42.5,
                            "change": 0.5,
                            "change_percent": 1.19,
                        },
                    ]
                    if evidence
                    else []
                ),
                "answer_allowed": evidence,
                "numeric_claims_allowed": evidence and status in {"ready", "stale"},
                "route_destination": "financial_realtime_snapshot",
                "reason_codes": ["fixture_quote"],
            }
        )
        return validate_realtime_query(query)

    def test_news_query_reuses_entity_rules_rejects_future_and_keeps_date_precision(self):
        self._article(
            "https://news.example.test/exact",
            "SpaceX 公布最新业务进展",
            published_at="2026-08-03T03:00:00Z",
        )
        self._article(
            "https://news.example.test/date-only",
            "SPCX 发布投资者资料",
            published_at="2026-08-02",
            precision="date",
            timezone_name="America/New_York",
            first_crawled="2026-08-03T02:00:00Z",
        )
        self._article(
            "https://news.example.test/future",
            "SpaceX 未来新闻",
            published_at="2026-08-03T05:00:00Z",
        )
        self._article(
            "https://news.example.test/unrelated",
            "其他公司新闻",
            published_at="2026-08-03T03:30:00Z",
            content="与目标证券无关。",
        )

        result = self.news.execute(self._news_plan())

        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(result["evidence"]), 2)
        by_title = {item["title"]: item for item in result["evidence"]}
        self.assertEqual(by_title["SPCX 发布投资者资料"]["published_precision"], "date")
        self.assertEqual(
            by_title["SPCX 发布投资者资料"]["published_timezone"],
            "America/New_York",
        )
        self.assertIn("future_articles_rejected", result["reason_codes"])
        self.assertTrue(
            all(item["numeric_claim_boundary"] == "document_is_not_a_quote" for item in result["evidence"])
        )

    def test_bundle_keeps_quote_and_news_times_separate_and_formats_sources(self):
        self._article(
            "https://news.example.test/latest",
            "SpaceX 发布最新公告",
            published_at="2026-08-03T03:00:00Z",
        )
        quote_result = self._quote_result(status="stale")
        quote_service = _QuoteService(quote_result)
        quote_plan = {**quote_result, "status": "planned", "evidence": [], "answer_allowed": False,
                      "numeric_claims_allowed": False, "completed_at_utc": ""}
        news_plan = self._news_plan()
        bundle = plan_latest_bundle(
            {"channels": ["quote", "news"]}, quote_plan, news_plan
        )

        completed = FinancialLatestBundleService(
            realtime_service=quote_service,
            news_service=self.news,
        ).execute(bundle, SERVER_CONTEXT)
        answer = format_latest_bundle_answer(completed, SERVER_CONTEXT)

        self.assertEqual(completed["status"], "ready")
        self.assertEqual(quote_service.calls, 1)
        self.assertEqual(
            completed["latest_available"]["quote"]["selected_at_utc"],
            "2026-08-01T20:00:00.000Z",
        )
        self.assertEqual(
            completed["latest_available"]["news"]["selected_at_utc"],
            "2026-08-03T03:00:00.000Z",
        )
        self.assertIn("42.5 USD", answer)
        self.assertIn("SpaceX 发布最新公告", answer)
        self.assertIn("America/New_York", answer)
        self.assertIn("新闻文档不是结构化行情", answer)
        self.assertNotIn("TradingAgents", answer)
        self.assertNotIn("## 风险与反证", answer)
        self.assertNotIn("## 已核验当前事实", answer)
        self.assertEqual(answer.count("免责声明："), 1)
        self.assertIn("用户时区=Asia/Hong_Kong", answer)
        self.assertIn("本地时间=2026-08-03T12:00:00+08:00", answer)

    def test_bundle_labels_last_known_news_outside_recent_window(self):
        self._article(
            "https://news.example.test/last-known",
            "SpaceX 最后一条历史公告",
            published_at="2026-06-01T03:00:00Z",
        )
        quote_result = self._quote_result(status="unavailable", evidence=False)
        quote_service = _QuoteService(quote_result)
        quote_plan = {**quote_result, "status": "planned", "completed_at_utc": ""}
        bundle = plan_latest_bundle(
            {"channels": ["quote", "news"]}, quote_plan, self._news_plan()
        )

        completed = FinancialLatestBundleService(
            realtime_service=quote_service,
            news_service=self.news,
        ).execute(bundle, SERVER_CONTEXT)
        answer = format_latest_bundle_answer(completed, SERVER_CONTEXT)

        self.assertEqual(completed["news"]["status"], "ready")
        self.assertEqual(
            completed["news"]["evidence"][0]["recency_status"],
            "latest_available",
        )
        self.assertIn("SpaceX 最后一条历史公告", answer)
        self.assertIn("最近可得", answer)
        self.assertIn("已超出7天近期窗口", answer)

    def test_one_channel_failure_returns_partial_without_fabrication(self):
        self._article(
            "https://news.example.test/only-news",
            "SpaceX 新闻仍可用",
            published_at="2026-08-03T03:00:00Z",
        )
        quote_result = self._quote_result(status="unavailable", evidence=False)
        quote_service = _QuoteService(quote_result)
        quote_plan = {**quote_result, "status": "planned", "completed_at_utc": ""}
        bundle = plan_latest_bundle(
            {"channels": ["quote", "news"]}, quote_plan, self._news_plan()
        )

        completed = FinancialLatestBundleService(
            realtime_service=quote_service,
            news_service=self.news,
        ).execute(bundle, SERVER_CONTEXT)
        answer = format_latest_bundle_answer(completed, SERVER_CONTEXT)

        self.assertEqual(completed["status"], "partial")
        self.assertIn("暂时没有可核验的价格快照", answer)
        self.assertIn("SpaceX 新闻仍可用", answer)

    def test_single_source_lists_observations_without_publishing_current_price(self):
        quote_result = self._quote_result(status="ready")
        current = dict(quote_result["evidence"][0])
        delayed = {
            **dict(quote_result["evidence"][1]),
            "price": 42.25,
            "observed_at": "2026-08-01T19:45:00.000Z",
            "freshness": "stale",
        }
        quote_result.update(
            {
                "evidence": [current],
                "refresh": {
                    "status": "completed",
                    "source_observations": [current, delayed],
                },
                "answer_allowed": True,
                "numeric_claims_allowed": True,
            }
        )
        quote_plan = {
            **quote_result,
            "status": "planned",
            "completed_at_utc": "",
            "evidence": [],
            "answer_allowed": False,
            "numeric_claims_allowed": False,
        }
        news_plan = self._news_plan()
        bundle = plan_latest_bundle(
            {"channels": ["quote", "news"]}, quote_plan, news_plan
        )

        completed = FinancialLatestBundleService(
            realtime_service=_QuoteService(quote_result),
            news_service=self.news,
        ).execute(bundle, SERVER_CONTEXT)
        answer = format_latest_bundle_answer(completed, SERVER_CONTEXT)

        self.assertIn("来源观察（不代表当前价共识）", answer)
        self.assertIn("最新来源报价（单源观察，未交叉核验）：42.5 USD", answer)
        self.assertIn("Yahoo Finance：42.5 USD", answer)
        self.assertIn("Alpha Vantage：42.25 USD", answer)
        self.assertIn("observed_at=2026-08-01T19:45:00.000Z", answer)
        self.assertIn("未形成可发布的单一当前价格", answer)
        self.assertNotIn("核验价格 42.5", answer)

    def test_p4_quote_and_news_execute_in_parallel_with_bounded_telemetry(self):
        self._article(
            "https://news.example.test/parallel",
            "SpaceX 并行通道新闻",
            published_at="2026-08-03T03:00:00Z",
        )
        quote_result = self._quote_result()
        news_result = self.news.execute(self._news_plan())
        quote_plan = {
            **quote_result,
            "status": "planned",
            "completed_at_utc": "",
            "evidence": [],
            "answer_allowed": False,
            "numeric_claims_allowed": False,
        }
        barrier = threading.Barrier(2)
        quote = _TimedService(quote_result, delay=0.08, barrier=barrier)
        news = _TimedService(news_result, delay=0.08, barrier=barrier)
        bundle = plan_latest_bundle(
            {"channels": ["quote", "news"]}, quote_plan, self._news_plan()
        )

        started = time.monotonic()
        completed = FinancialLatestBundleService(
            realtime_service=quote,
            news_service=news,
            settings={
                "FINANCIAL_INTELLIGENCE_ENABLED": True,
                "FINANCIAL_LATEST_BUNDLE_ENABLED": True,
                "FINANCIAL_LATEST_QUOTE_TIMEOUT_SECONDS": 1,
                "FINANCIAL_LATEST_NEWS_TIMEOUT_SECONDS": 1,
                "FINANCIAL_LATEST_BUNDLE_TIMEOUT_SECONDS": 1,
            },
        ).execute(bundle, SERVER_CONTEXT)
        elapsed = time.monotonic() - started

        self.assertEqual(completed["status"], "ready")
        self.assertEqual(completed["execution"]["strategy"], "parallel")
        self.assertLess(elapsed, 0.16)
        self.assertEqual(quote.calls, 1)
        self.assertEqual(news.calls, 1)

    def test_p4_slow_news_times_out_without_discarding_quote(self):
        quote_result = self._quote_result()
        quote_plan = {
            **quote_result,
            "status": "planned",
            "completed_at_utc": "",
            "evidence": [],
            "answer_allowed": False,
            "numeric_claims_allowed": False,
        }
        news_plan = self._news_plan()
        news_result = {
            **news_plan,
            "status": "ready",
            "completed_at_utc": "2026-08-03T04:00:00.000Z",
            "evidence": [{"article_id": 7, "published_at": "2026-08-03T03:00:00Z"}],
            "answer_allowed": True,
        }
        bundle = plan_latest_bundle(
            {"channels": ["quote", "news"]}, quote_plan, news_plan
        )

        completed = FinancialLatestBundleService(
            realtime_service=_TimedService(quote_result),
            news_service=_TimedService(news_result, delay=0.15),
            settings={
                "FINANCIAL_INTELLIGENCE_ENABLED": True,
                "FINANCIAL_LATEST_BUNDLE_ENABLED": True,
                "FINANCIAL_LATEST_QUOTE_TIMEOUT_SECONDS": 0.2,
                "FINANCIAL_LATEST_NEWS_TIMEOUT_SECONDS": 0.03,
                "FINANCIAL_LATEST_BUNDLE_TIMEOUT_SECONDS": 0.2,
            },
        ).execute(bundle, SERVER_CONTEXT)

        self.assertEqual(completed["status"], "partial")
        self.assertTrue(completed["quote"]["answer_allowed"])
        self.assertFalse(completed["news"]["answer_allowed"])
        self.assertEqual(
            completed["execution"]["channels"]["news"]["status"], "timed_out"
        )
        self.assertEqual(completed["news"]["refresh"]["status"], "timed_out")
        self.assertIn(
            "news_channel_timeout", completed["news"]["refresh"]["reason_codes"]
        )
        self.assertIn("news_channel_timeout", completed["reason_codes"])

    def test_p4_slow_quote_or_conflict_keeps_verified_news(self):
        self._article(
            "https://news.example.test/quote-fallback",
            "SpaceX 新闻通道保持可用",
            published_at="2026-08-03T03:00:00Z",
        )
        news_plan = self._news_plan()
        news_result = self.news.execute(news_plan)
        ready_quote = self._quote_result()
        quote_plan = {
            **ready_quote,
            "status": "planned",
            "completed_at_utc": "",
            "evidence": [],
            "answer_allowed": False,
            "numeric_claims_allowed": False,
        }
        bundle = plan_latest_bundle(
            {"channels": ["quote", "news"]}, quote_plan, news_plan
        )
        timed_out = FinancialLatestBundleService(
            realtime_service=_TimedService(ready_quote, delay=0.15),
            news_service=_TimedService(news_result),
            settings={
                "FINANCIAL_INTELLIGENCE_ENABLED": True,
                "FINANCIAL_LATEST_BUNDLE_ENABLED": True,
                "FINANCIAL_LATEST_QUOTE_TIMEOUT_SECONDS": 0.03,
                "FINANCIAL_LATEST_NEWS_TIMEOUT_SECONDS": 0.2,
                "FINANCIAL_LATEST_BUNDLE_TIMEOUT_SECONDS": 0.2,
            },
        ).execute(bundle, SERVER_CONTEXT)

        conflict_quote = self._quote_result(status="conflict")
        conflicted = FinancialLatestBundleService(
            realtime_service=_QuoteService(conflict_quote),
            news_service=_QuoteService(news_result),
        ).execute(bundle, SERVER_CONTEXT)
        conflict_answer = format_latest_bundle_answer(conflicted, SERVER_CONTEXT)

        self.assertEqual(timed_out["status"], "partial")
        self.assertTrue(timed_out["news"]["answer_allowed"])
        self.assertEqual(conflicted["status"], "partial")
        self.assertFalse(conflicted["quote"]["numeric_claims_allowed"])
        self.assertIn("SpaceX 新闻通道保持可用", conflict_answer)
        self.assertIn("本轮不选择单一实时价格", conflict_answer)

    def test_p4_news_only_never_calls_quote_and_bundle_switch_rolls_back(self):
        self._article(
            "https://news.example.test/news-only",
            "SPCX 新闻通道",
            published_at="2026-08-03T03:00:00Z",
        )
        news_plan = self._news_plan()
        quote_plan = skipped_realtime_query("quote_channel_not_requested")
        disabled = plan_latest_bundle(
            {"channels": ["news"]},
            quote_plan,
            news_plan,
            settings={
                "FINANCIAL_INTELLIGENCE_ENABLED": True,
                "FINANCIAL_LATEST_BUNDLE_ENABLED": False,
            },
        )
        self.assertEqual(disabled["status"], "skipped")
        self.assertIn("latest_bundle_disabled", disabled["reason_codes"])

        quote = _QuoteService(self._quote_result())
        bundle = plan_latest_bundle(
            {"channels": ["news"]}, quote_plan, news_plan
        )
        completed = FinancialLatestBundleService(
            realtime_service=quote,
            news_service=self.news,
        ).execute(bundle, SERVER_CONTEXT)

        self.assertEqual(completed["status"], "ready")
        self.assertEqual(quote.calls, 0)
        self.assertTrue(completed["news"]["answer_allowed"])

    def test_p5_default_orchestrator_uses_live_runtime_bundle_switch(self):
        self.assertIs(chat_route_orchestrator.financial_settings, config)
        with patch.object(config, "FINANCIAL_INTELLIGENCE_ENABLED", True), patch.object(
            config, "FINANCIAL_LATEST_BUNDLE_ENABLED", False
        ):
            disabled = plan_latest_bundle(
                {"channels": ["news"]},
                skipped_realtime_query("quote_channel_not_requested"),
                self._news_plan(),
                settings=chat_route_orchestrator.financial_settings,
            )
        self.assertEqual(disabled["status"], "skipped")
        self.assertIn("latest_bundle_disabled", disabled["reason_codes"])

    def test_p4_all_channels_fail_closed(self):
        quote_result = self._quote_result(status="unavailable", evidence=False)
        quote_plan = {**quote_result, "status": "planned", "completed_at_utc": ""}
        news_plan = self._news_plan()
        news_result = self.news.execute(news_plan)
        bundle = plan_latest_bundle(
            {"channels": ["quote", "news"]}, quote_plan, news_plan
        )

        completed = FinancialLatestBundleService(
            realtime_service=_QuoteService(quote_result),
            news_service=_QuoteService(news_result),
        ).execute(bundle, SERVER_CONTEXT)

        self.assertEqual(completed["status"], "unavailable")
        self.assertFalse(completed["answer_allowed"])
        self.assertFalse(completed["quote"]["numeric_claims_allowed"])

    def test_p5_bundle_schema_rejects_unreviewed_top_level_fields(self):
        bundle = plan_latest_bundle(
            {"channels": ["news"]},
            skipped_realtime_query("quote_channel_not_requested"),
            self._news_plan(),
        )
        with self.assertRaises(ValidationError):
            validate_latest_bundle({**bundle, "unreviewed_field": True})


if __name__ == "__main__":
    unittest.main()

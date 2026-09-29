import hashlib
import unittest
from unittest.mock import patch

import chat_api
from financial_sse import FINANCIAL_SSE_PROTOCOL_VERSION
from tests import test_financial_spacex_latest_e2e as spacex_fixture


class FinancialLatestInformationE2ETest(unittest.TestCase):
    """补齐跨会话、时区、开关和离线可重复性的公共 SSE 验收。"""

    def setUp(self):
        self.fixture = spacex_fixture.FinancialSpacexLatestE2ETest(
            "test_empty_registry_discovers_spacex_then_returns_quote_and_news_sse"
        )
        self.fixture.setUp()
        self.client = self.fixture.client
        self.orchestrator = self.fixture.orchestrator
        self.question = self.fixture.golden["question"]

    def tearDown(self):
        self.fixture.tearDown()

    def _payload(self, session_id, question=None, timezone_name="Asia/Hong_Kong"):
        return {
            "session_id": session_id,
            "model": "local",
            "user_timezone": timezone_name,
            "messages": [{"role": "user", "content": question or self.question}],
            "sse_features": [FINANCIAL_SSE_PROTOCOL_VERSION],
        }

    def _financial_post(self, payload):
        with patch.object(
            chat_api, "chat_route_orchestrator", self.orchestrator
        ), patch.object(
            chat_api,
            "_stream_openai",
            side_effect=AssertionError("financial latest route must not call general model"),
        ):
            return self.client.post("/api/chat/send", json=payload, buffered=True)

    @staticmethod
    def _answer(events):
        return next(item["content"] for item in events if item["type"] == "chunk")

    def _protected_counts(self):
        connection = self.fixture.database.connection
        return {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in (
                "financial_instrument_candidates",
                "financial_instruments",
                "financial_data_snapshots",
                "articles",
            )
        }

    def test_p5_hong_kong_and_new_york_views_share_evidence_identity(self):
        hk_events = spacex_fixture._events(
            self._financial_post(self._payload("timezone-hk"))
        )
        ny_events = spacex_fixture._events(
            self._financial_post(
                self._payload("timezone-ny", timezone_name="America/New_York")
            )
        )

        hk_route = next(item for item in hk_events if item["type"] == "route")
        ny_route = next(item for item in ny_events if item["type"] == "route")
        hk_sources = next(item["sources"] for item in hk_events if item["type"] == "sources")
        ny_sources = next(item["sources"] for item in ny_events if item["type"] == "sources")
        hk_ids = {(item["source_kind"], item["reference_id"]) for item in hk_sources}
        ny_ids = {(item["source_kind"], item["reference_id"]) for item in ny_sources}

        self.assertEqual(hk_route["server_time"]["user_timezone"], "Asia/Hong_Kong")
        self.assertEqual(ny_route["server_time"]["user_timezone"], "America/New_York")
        self.assertEqual(hk_ids, ny_ids)

    def test_p5_new_session_pronoun_does_not_inherit_spacex(self):
        self._financial_post(self._payload("confirmed-session"))
        other = self._payload("unrelated-session", "它现在怎么样")
        model_config = {
            "models": {
                "local": {
                    "api_key": "fixture",
                    "model_id": "fixture",
                    "use_proxy": False,
                    "base_url": "http://fixture.invalid",
                }
            }
        }
        with patch.object(
            chat_api, "chat_route_orchestrator", self.orchestrator
        ), patch.object(chat_api, "_load_config", return_value=model_config), patch.object(
            chat_api, "_stream_openai", return_value=iter(["普通非金融回答"])
        ):
            response = self.client.post(
                "/api/chat/send", json=other, buffered=True
            )
        events = spacex_fixture._events(response)
        body = "".join(
            str(item.get("content") or "") for item in events if item["type"] == "chunk"
        )

        self.assertIn("普通非金融回答", body)
        self.assertNotIn("SPCX.US", body)
        latest = self.orchestrator.store.latest_target_state("unrelated-session")
        self.assertTrue(latest is None or not latest.get("resolved_targets"))

    def test_p5_child_switches_roll_back_without_model_fallback(self):
        self._financial_post(self._payload("switch-seed"))
        protected_after_seed = self._protected_counts()
        self.orchestrator.financial_settings = {
            **spacex_fixture.SETTINGS,
            "FINANCIAL_LATEST_BUNDLE_ENABLED": False,
        }

        quote_events = spacex_fixture._events(
            self._financial_post(self._payload("switch-quote-bundle-off"))
        )
        quote_route = next(item for item in quote_events if item["type"] == "route")
        quote_answer = self._answer(quote_events)
        self.assertEqual(quote_route["route_destination"], "financial_realtime_snapshot")
        self.assertNotIn("SpaceX 完成首次公开募股后的首项业务更新", quote_answer)
        self.assertEqual(self._protected_counts(), protected_after_seed)

        news_events = spacex_fixture._events(
            self._financial_post(
                self._payload("switch-news-bundle-off", "SpaceX 最新新闻")
            )
        )
        news_answer = self._answer(news_events)
        self.assertIn("SpaceX 完成首次公开募股后的首项业务更新", news_answer)
        self.assertNotIn("88.25 USD", news_answer)
        self.assertEqual(self._protected_counts(), protected_after_seed)

        self.orchestrator.financial_settings = {
            **spacex_fixture.SETTINGS,
            "FINANCIAL_LATEST_NEWS_ENABLED": False,
            "FINANCIAL_LATEST_BUNDLE_ENABLED": True,
        }
        self.orchestrator.news_query_service.settings = self.orchestrator.financial_settings
        partial_events = spacex_fixture._events(
            self._financial_post(self._payload("switch-news-off"))
        )
        partial_answer = self._answer(partial_events)
        self.assertIn("88.25 USD", partial_answer)
        self.assertIn("没有匹配且可核验的新闻", partial_answer)
        self.assertEqual(self._protected_counts(), protected_after_seed)

    def test_p5_networkless_fixture_is_repeatable_with_stable_answer_hash(self):
        runs = [
            spacex_fixture._events(
                self._financial_post(self._payload(f"offline-{index}"))
            )
            for index in range(int(self.fixture.golden["repeat_runs"]))
        ]
        hashes = {
            hashlib.sha256(self._answer(events).encode("utf-8")).hexdigest()
            for events in runs
        }

        self.assertEqual(
            hashes,
            {self.fixture.golden["expected_answer_sha256"]},
        )
        self.assertTrue(all(events[-1]["type"] == "done" for events in runs))
        self.assertTrue(
            all("error" not in {item["type"] for item in events} for events in runs)
        )


if __name__ == "__main__":
    unittest.main()

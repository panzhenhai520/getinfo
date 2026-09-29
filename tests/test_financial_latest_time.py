import unittest
from datetime import datetime, timezone

from financial_latest_time import LatestAvailableTimeResolver
from financial_market_clock import MarketClockService, RequestTimeContext
from financial_provider_contract import MarketStatus


UTC = timezone.utc


def _at(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _context(value, user_timezone="Asia/Hong_Kong"):
    return RequestTimeContext(
        server_now_utc=_at(value),
        server_timezone="Asia/Hong_Kong",
        user_timezone=user_timezone,
    )


class FinancialLatestTimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clock = MarketClockService()
        cls.resolver = LatestAvailableTimeResolver()

    def test_xnas_regular_weekend_and_early_close_states(self):
        regular = self.clock.market_state(
            "XNAS", _context("2026-08-03T15:00:00Z")
        )
        self.assertEqual(regular.market_timezone, "America/New_York")
        self.assertEqual(regular.market_session_state, MarketStatus.OPEN)

        weekend = self.clock.market_state(
            "XNAS", _context("2026-08-02T14:00:00Z")
        )
        self.assertEqual(weekend.market_session_state, MarketStatus.CLOSED)
        self.assertEqual(weekend.reason, "weekend")

        early_close = self.clock.market_state(
            "XNAS", _context("2026-11-27T18:00:00Z")
        )
        self.assertTrue(early_close.is_half_day)
        self.assertEqual(early_close.market_session_state, MarketStatus.CLOSED)
        self.assertEqual(early_close.session_close_utc, _at("2026-11-27T18:00:00Z"))

    def test_xnas_dst_changes_utc_session_boundaries(self):
        before_dst = self.clock.market_state(
            "XNAS", _context("2026-03-06T20:30:00Z")
        )
        after_dst = self.clock.market_state(
            "XNAS", _context("2026-03-09T19:30:00Z")
        )
        self.assertEqual(before_dst.session_close_utc, _at("2026-03-06T21:00:00Z"))
        self.assertEqual(after_dst.session_close_utc, _at("2026-03-09T20:00:00Z"))

    def test_latest_available_rejects_future_and_keeps_channel_times_separate(self):
        context = _context("2026-08-03T04:00:00Z")
        market = self.clock.market_state("XNAS", context)
        result = self.resolver.resolve(
            context,
            market=market,
            quote_records=[
                {
                    "snapshot_id": 1,
                    "observed_at": "2026-08-01T20:00:00Z",
                    "fetched_at": "2026-08-01T20:00:02Z",
                },
                {
                    "snapshot_id": 2,
                    "observed_at": "2026-08-03T04:00:01Z",
                    "fetched_at": "2026-08-03T04:00:01Z",
                },
            ],
            news_records=[
                {
                    "article_id": 7,
                    "published_at": "2026-08-03T03:30:00Z",
                    "fetched_at": "2026-08-03T03:35:00Z",
                    "published_precision": "instant",
                },
                {
                    "article_id": 8,
                    "published_at": "2026-08-03T04:30:00Z",
                    "fetched_at": "2026-08-03T04:31:00Z",
                    "published_precision": "instant",
                },
            ],
        )
        self.assertEqual(result["quote"]["record"]["snapshot_id"], 1)
        self.assertEqual(result["news"]["record"]["article_id"], 7)
        self.assertEqual(result["future_records_rejected"], 2)
        self.assertEqual(result["cutoff_at_utc"], "2026-08-03T04:00:00.000Z")
        self.assertNotEqual(
            result["quote"]["selected_at_utc"],
            result["news"]["selected_at_utc"],
        )

    def test_date_only_news_keeps_precision_without_inventing_time(self):
        context = _context("2026-08-03T04:00:00Z")
        result = self.resolver.resolve(
            context,
            news_records=[
                {
                    "article_id": 9,
                    "published_at": "2026-08-02",
                    "published_timezone": "America/New_York",
                    "published_precision": "date",
                    "fetched_at": "2026-08-03T01:00:00Z",
                }
            ],
        )
        self.assertEqual(result["news"]["record"]["article_id"], 9)
        self.assertEqual(result["news"]["precision"], "date")
        self.assertIsNone(result["news"]["selected_at_utc"])
        self.assertEqual(result["news"]["selected_date"], "2026-08-02")


if __name__ == "__main__":
    unittest.main()

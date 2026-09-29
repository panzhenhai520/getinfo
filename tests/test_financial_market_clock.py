import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from financial_market_clock import MarketClockService, RequestTimeContext
from financial_provider_contract import FreshnessState, MarketStatus


def _utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _context(value, user_timezone="Asia/Hong_Kong"):
    return RequestTimeContext(
        server_now_utc=_utc(value),
        server_timezone="Asia/Hong_Kong",
        user_timezone=user_timezone,
    )


class MarketClockServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = MarketClockService()

    def test_request_clock_is_captured_once_and_requires_timezone(self):
        calls = []

        def clock():
            calls.append(True)
            return _utc("2026-07-31T07:30:00Z")

        context = MarketClockService(clock=clock).capture_request(
            user_timezone="America/New_York"
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(context.server_now_utc, _utc("2026-07-31T07:30:00Z"))
        self.assertEqual(context.server_timezone, "Asia/Hong_Kong")
        self.assertEqual(context.user_timezone, "America/New_York")

        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            MarketClockService(clock=lambda: datetime(2026, 7, 31)).capture_request()

    def test_same_utc_has_different_a_share_and_hong_kong_state(self):
        context = _context("2026-07-31T07:30:00Z")  # 15:30 in both markets
        shanghai = self.service.market_state("XSHG", context)
        shenzhen = self.service.market_state("XSHE", context)
        hong_kong = self.service.market_state("XHKG", context)

        self.assertEqual(shanghai.market_session_state, MarketStatus.CLOSED)
        self.assertEqual(shenzhen.market_session_state, MarketStatus.CLOSED)
        self.assertEqual(hong_kong.market_session_state, MarketStatus.OPEN)
        self.assertEqual(shanghai.market_timezone, "Asia/Shanghai")
        self.assertEqual(hong_kong.market_timezone, "Asia/Hong_Kong")
        self.assertEqual(shanghai.calendar_source, "official_calendar")

    def test_xshg_open_lunch_and_close_boundaries(self):
        cases = {
            "2026-07-31T01:14:59Z": (MarketStatus.CLOSED, "before_pre_open"),
            "2026-07-31T01:15:00Z": (MarketStatus.PRE_OPEN, "pre_open"),
            "2026-07-31T01:29:59Z": (MarketStatus.PRE_OPEN, "pre_open"),
            "2026-07-31T01:30:00Z": (MarketStatus.OPEN, "regular_open"),
            "2026-07-31T03:30:00Z": (MarketStatus.LUNCH_BREAK, "lunch_break"),
            "2026-07-31T05:00:00Z": (MarketStatus.OPEN, "regular_open"),
            "2026-07-31T07:00:00Z": (MarketStatus.CLOSED, "after_close"),
        }
        for instant, expected in cases.items():
            with self.subTest(instant=instant):
                result = self.service.market_state("XSHG", _context(instant))
                self.assertEqual(
                    (result.market_session_state, result.reason), expected
                )

    def test_xhkg_open_lunch_and_close_boundaries(self):
        cases = {
            "2026-07-31T01:00:00Z": MarketStatus.PRE_OPEN,
            "2026-07-31T01:30:00Z": MarketStatus.OPEN,
            "2026-07-31T04:00:00Z": MarketStatus.LUNCH_BREAK,
            "2026-07-31T05:00:00Z": MarketStatus.OPEN,
            "2026-07-31T08:00:00Z": MarketStatus.CLOSED,
        }
        for instant, expected in cases.items():
            with self.subTest(instant=instant):
                result = self.service.market_state("XHKG", _context(instant))
                self.assertEqual(result.market_session_state, expected)

    def test_xtks_official_hours_and_2026_holiday(self):
        cases = {
            "2026-08-05T00:00:00Z": MarketStatus.OPEN,
            "2026-08-05T02:30:00Z": MarketStatus.LUNCH_BREAK,
            "2026-08-05T03:30:00Z": MarketStatus.OPEN,
            "2026-08-05T06:30:00Z": MarketStatus.CLOSED,
        }
        for instant, expected in cases.items():
            with self.subTest(instant=instant):
                result = self.service.market_state("XTKS", _context(instant))
                self.assertEqual(result.market_session_state, expected)
                self.assertEqual(result.market_timezone, "Asia/Tokyo")
                self.assertEqual(result.calendar_source, "official_calendar")
        holiday = self.service.market_state(
            "XTKS", _context("2026-09-22T01:00:00Z")
        )
        self.assertEqual(holiday.reason, "exchange_holiday")
        self.assertFalse(holiday.is_trading_day)

    def test_weekend_and_exchange_holidays_use_official_calendar(self):
        weekend = self.service.market_state(
            "XSHG", _context("2026-08-01T02:00:00Z")
        )
        self.assertFalse(weekend.is_trading_day)
        self.assertEqual(weekend.reason, "weekend")

        # Spring Festival closes mainland markets while HKEX trades on Feb 20.
        mainland_holiday = self.service.market_state(
            "XSHG", _context("2026-02-20T02:00:00Z")
        )
        hong_kong_open = self.service.market_state(
            "XHKG", _context("2026-02-20T02:00:00Z")
        )
        self.assertEqual(mainland_holiday.reason, "exchange_holiday")
        self.assertEqual(hong_kong_open.market_session_state, MarketStatus.OPEN)

        # The day following Easter Monday closes HKEX but not mainland exchanges.
        hong_kong_holiday = self.service.market_state(
            "XHKG", _context("2026-04-07T02:00:00Z")
        )
        mainland_open = self.service.market_state(
            "XSHE", _context("2026-04-07T02:00:00Z")
        )
        self.assertEqual(hong_kong_holiday.reason, "exchange_holiday")
        self.assertEqual(mainland_open.market_session_state, MarketStatus.OPEN)

    def test_hong_kong_half_day_has_authoritative_early_close(self):
        before_close = self.service.market_state(
            "XHKG", _context("2026-02-16T03:59:59Z")
        )
        at_close = self.service.market_state(
            "XHKG", _context("2026-02-16T04:00:00Z")
        )
        self.assertTrue(before_close.is_half_day)
        self.assertEqual(before_close.market_session_state, MarketStatus.OPEN)
        self.assertEqual(before_close.reason, "half_day_open")
        self.assertEqual(at_close.market_session_state, MarketStatus.CLOSED)
        self.assertEqual(at_close.session_close_utc, _utc("2026-02-16T04:00:00Z"))

    def test_outside_authoritative_coverage_is_visible_template_fallback(self):
        result = self.service.market_state(
            "XSHG", _context("2027-01-04T02:00:00Z")
        )
        self.assertEqual(result.market_session_state, MarketStatus.OPEN)
        self.assertEqual(result.calendar_source, "template_fallback")
        self.assertTrue(result.calendar_version.endswith(":template"))

    def test_today_uses_captured_server_instant_and_user_timezone(self):
        instant = "2026-07-31T23:30:00Z"
        hong_kong_context = _context(instant, "Asia/Hong_Kong")
        new_york_context = _context(instant, "America/New_York")

        hk_today = self.service.resolve_time_range("今天", hong_kong_context)
        ny_today = self.service.resolve_time_range("today", new_york_context)
        repeated = self.service.resolve_time_range("今天", hong_kong_context)

        self.assertEqual(hk_today.start_utc, _utc("2026-07-31T16:00:00Z"))
        self.assertEqual(ny_today.start_utc, _utc("2026-07-31T04:00:00Z"))
        self.assertEqual(hk_today.end_utc, _utc(instant))
        self.assertEqual(hk_today, repeated)

        market = self.service.market_state("XHKG", hong_kong_context)
        contract = self.service.time_contract(
            hong_kong_context, market=market, resolved_range=hk_today
        )
        self.assertEqual(contract["server_now_utc"], instant)
        self.assertEqual(contract["market_calendar_id"], "XHKG")
        self.assertEqual(
            contract["resolved_time_range"]["start_utc"],
            "2026-07-31T16:00:00Z",
        )

    def test_now_and_rolling_24_hours_are_reproducible(self):
        context = _context("2026-07-31T07:30:00Z")
        now = self.service.resolve_time_range("此时", context)
        rolling = self.service.resolve_time_range("24h", context)
        self.assertEqual(now.start_utc, context.server_now_utc)
        self.assertEqual(now.end_utc, context.server_now_utc)
        self.assertEqual(
            rolling.start_utc, context.server_now_utc - timedelta(hours=24)
        )
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.service.resolve_time_range("最近一阵", context)

    def test_freshness_states_and_input_boundaries(self):
        context = _context("2026-07-31T07:30:00Z")
        fetched = _utc("2026-07-31T07:29:59Z")
        cases = {
            30: FreshnessState.CURRENT,
            90: FreshnessState.DELAYED,
            121: FreshnessState.STALE,
        }
        for age, expected in cases.items():
            with self.subTest(age=age):
                result = self.service.assess_freshness(
                    context.server_now_utc - timedelta(seconds=age),
                    fetched,
                    current_threshold_seconds=60,
                    context=context,
                )
                self.assertEqual(result.state, expected)

        historical = self.service.assess_freshness(
            _utc("2026-07-30T07:30:00Z"),
            _utc("2026-07-30T07:31:00Z"),
            current_threshold_seconds=60,
            requested_as_of=_utc("2026-07-30T07:30:00Z"),
            context=context,
        )
        self.assertEqual(historical.state, FreshnessState.HISTORICAL)

        with self.assertRaisesRegex(ValueError, "later than"):
            self.service.assess_freshness(
                fetched + timedelta(seconds=1),
                fetched,
                current_threshold_seconds=60,
                context=context,
            )
        with self.assertRaisesRegex(ValueError, "positive"):
            self.service.assess_freshness(
                fetched,
                fetched,
                current_threshold_seconds=0,
                context=context,
            )
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.service.assess_freshness(
                datetime(2026, 7, 31),
                fetched,
                current_threshold_seconds=60,
                context=context,
            )

    def test_unknown_market_and_invalid_calendar_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported market calendar"):
            self.service.market_state("NOT-A-MARKET", _context("2026-07-31T02:00:00Z"))

        malformed = {
            "schema_version": 1,
            "calendar_version": "bad-v1",
            "coverage_start": "2026-01-01",
            "coverage_end": "2026-12-31",
            "calendars": {
                "BAD": {
                    "timezone": "Asia/Hong_Kong",
                    "regular_sessions": [["13:00", "16:00"], ["09:30", "12:00"]],
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps(malformed), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "ordered and non-overlapping"):
                MarketClockService(calendar_dir=directory)


if __name__ == "__main__":
    unittest.main()

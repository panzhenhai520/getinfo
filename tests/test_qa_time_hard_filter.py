#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 4 时间窗硬约束单测：日期归属、硬过滤、分级扩窗、开关回退。

背景（实测）：`published_at_utc` 全表为空，检索只按 `publish_date` 给"区间内 +8 分"，
问"2026 年 1 月工信部说了什么"照样能选进 2026-10-04 的新文。
现在改成「硬过滤 + 分级扩窗」，并且必须满足三条底线：
  1. 时间窗内证据足够时，窗口外文章**不得**进证据；
  2. 窗口内证据不足时逐级放宽（3 个月 → 6 个月 → 1 年），并回报放宽到哪一级；
  3. 时间未知的文章不得被静默丢掉（计数上报即可）。
"""
import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_TYPE", "sqlite")

import qa_retrieval as qr  # noqa: E402


def _item(article_id, day, score=100):
    """构造 ranked 元组：(score, 排序日期, id, row, reason, 本地日期)。"""
    row = {"id": article_id, "title": "T%d" % article_id}
    return (score, day.isoformat() if day else "", article_id, row, "正文相关", day)


class ArticleDayTests(unittest.TestCase):
    def test_date_precision_keeps_source_local_day(self):
        row = {"published_at_utc": "2026-10-07T00:00:00Z", "published_timezone": "Asia/Shanghai",
               "published_precision": "date"}
        self.assertEqual(str(qr._article_day(row)), "2026-10-07")

    def test_legacy_plain_date_column_is_understood(self):
        self.assertEqual(str(qr._article_day({"published_at_utc": "2026-10-07"})), "2026-10-07")

    def test_exact_instant_is_converted_to_source_timezone(self):
        # 2026-10-07T06:30Z = 上海 14:30，仍是 10-07
        row = {"published_at_utc": "2026-10-07T06:30:00Z", "published_timezone": "Asia/Shanghai",
               "published_precision": "exact"}
        self.assertEqual(str(qr._article_day(row)), "2026-10-07")

    def test_exact_instant_near_midnight_shifts_day(self):
        # 2026-10-06T17:30Z = 上海 10-07 01:30
        row = {"published_at_utc": "2026-10-06T17:30:00Z", "published_timezone": "Asia/Shanghai",
               "published_precision": "exact"}
        self.assertEqual(str(qr._article_day(row)), "2026-10-07")

    def test_discovered_is_treated_as_unknown(self):
        """只有抓取时间的（discovered）不得当成发布时间参与硬过滤。"""
        row = {"published_at_utc": "2026-10-07T00:00:00Z", "published_precision": "discovered"}
        self.assertIsNone(qr._article_day(row))

    def test_falls_back_to_publish_date(self):
        self.assertEqual(str(qr._article_day({"publish_date": "2026-09-01"})), "2026-09-01")
        self.assertIsNone(qr._article_day({}))


class TimeGateTests(unittest.TestCase):
    def setUp(self):
        self.window = {
            "has_time": True, "label": "2026 年 1 月",
            "start": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "end": datetime(2026, 2, 1, tzinfo=timezone.utc),
        }

    def test_in_window_evidence_survives_and_out_of_window_is_dropped(self):
        ranked = [
            _item(1, datetime(2026, 1, 20).date()),
            _item(2, datetime(2025, 12, 20).date(), score=99),
            _item(3, datetime(2026, 10, 4).date(), score=98),
        ]
        gated, receipt = qr.apply_time_gate(ranked, self.window, min_in_window=1, ladder=[90, 180, 365])
        self.assertEqual([item[2] for item in gated], [1])
        self.assertTrue(receipt["hard_filter"])
        self.assertEqual(receipt["ladder_step"], 0)
        self.assertEqual(receipt["dropped_out_of_window"], 2)

    def test_ladder_widens_when_window_is_empty(self):
        """1 月里一条都没有 → 先放宽 90 天（含 2025-12-20），命中即停。"""
        ranked = [
            _item(2, datetime(2025, 12, 20).date()),
            _item(3, datetime(2026, 10, 4).date(), score=98),
        ]
        gated, receipt = qr.apply_time_gate(ranked, self.window, min_in_window=1, ladder=[90, 180, 365])
        self.assertEqual([item[2] for item in gated], [2])
        self.assertEqual(receipt["ladder_step"], 1)
        self.assertEqual(receipt["ladder_days"], 90)
        self.assertIn("放宽", receipt["note"])

    def test_ladder_keeps_climbing_until_minimum_satisfied(self):
        ranked = [
            _item(2, datetime(2025, 12, 1).date()),
            _item(3, datetime(2025, 9, 1).date(), score=98),
        ]
        gated, receipt = qr.apply_time_gate(ranked, self.window, min_in_window=2, ladder=[90, 180, 365])
        self.assertEqual(sorted(item[2] for item in gated), [2, 3])
        self.assertEqual(receipt["ladder_step"], 2)   # 放宽到 180 天

    def test_unknown_time_is_kept_but_ranked_last(self):
        ranked = [
            _item(1, None, score=100),
            _item(2, datetime(2026, 1, 20).date(), score=50),
        ]
        gated, receipt = qr.apply_time_gate(ranked, self.window, min_in_window=1, ladder=[90])
        self.assertEqual([item[2] for item in gated], [2, 1])
        self.assertEqual(receipt["unknown_time_kept"], 1)

    def test_switch_off_returns_old_behaviour(self):
        ranked = [
            _item(1, datetime(2026, 1, 20).date()),
            _item(3, datetime(2026, 10, 4).date(), score=98),
        ]
        gated, receipt = qr.apply_time_gate(ranked, self.window, hard_filter=False)
        self.assertEqual([item[2] for item in gated], [1, 3])
        self.assertFalse(receipt["hard_filter"])

    def test_question_without_time_is_untouched(self):
        ranked = [_item(1, datetime(2026, 1, 20).date()), _item(2, None)]
        gated, receipt = qr.apply_time_gate(
            ranked, {"has_time": False, "start": None, "end": None}, min_in_window=1)
        self.assertEqual([item[2] for item in gated], [1, 2])
        self.assertFalse(receipt["hard_filter"])


class PrecisionDisplayTests(unittest.TestCase):
    """精度贯通"展示"：证据与时间轴要能看出"仅到日""时间未知"的可信度差别。"""

    def test_precision_labels_are_human_readable(self):
        self.assertIn("精确", qr.describe_published_precision("exact"))
        self.assertIn("仅到日", qr.describe_published_precision("date"))
        self.assertIn("链接", qr.describe_published_precision("url"))
        self.assertIn("未知", qr.describe_published_precision("discovered"))

    def test_legacy_precision_vocabulary_is_accepted(self):
        self.assertIn("仅到日", qr.describe_published_precision("day"))
        self.assertIn("精确", qr.describe_published_precision("datetime"))
        self.assertEqual(qr.normalize_precision("day"), "date")
        self.assertEqual(qr.normalize_precision("datetime"), "exact")

    def test_timezone_is_included_only_for_real_timestamps(self):
        self.assertIn("Asia/Shanghai", qr.describe_published_precision("date", "Asia/Shanghai"))
        # discovered 不是发布时间，带上时区反而像在暗示它有可信时间
        self.assertNotIn("Asia/Shanghai", qr.describe_published_precision("discovered", "Asia/Shanghai"))

    def test_unknown_precision_describes_nothing(self):
        self.assertEqual(qr.describe_published_precision(""), "")
        self.assertEqual(qr.describe_published_precision("weird"), "")

    def test_evidence_carries_precision_fields(self):
        evidence = qr._article_evidence(
            {"id": 7, "title": "T", "content": "正文", "publish_date": "2026-10-07",
             "published_at_utc": "2026-10-07T00:00:00Z", "published_timezone": "Asia/Shanghai",
             "published_precision": "day"},
            score=1.0, method="keyword", reason="命中")
        self.assertEqual(evidence["published_precision"], "date")
        self.assertEqual(evidence["published_timezone"], "Asia/Shanghai")
        self.assertEqual(evidence["published_at_utc"], "2026-10-07T00:00:00Z")
        self.assertIn("仅到日", evidence["published_time_note"])

    def test_evidence_without_precision_stays_quiet(self):
        evidence = qr._article_evidence(
            {"id": 8, "title": "T", "content": "正文"}, score=1.0, method="keyword", reason="命中")
        self.assertEqual(evidence["published_precision"], "")
        self.assertEqual(evidence["published_time_note"], "")


class LadderConfigTests(unittest.TestCase):
    def test_default_ladder_is_three_six_twelve_months(self):
        self.assertEqual(qr._time_ladder(), [90, 180, 365])

    def test_ladder_is_configurable(self):
        previous = os.environ.get("QA_TIME_EXPAND_LADDER")
        os.environ["QA_TIME_EXPAND_LADDER"] = "7d,2w,1y"
        try:
            self.assertEqual(qr._time_ladder(), [7, 14, 365])
        finally:
            if previous is None:
                os.environ.pop("QA_TIME_EXPAND_LADDER", None)
            else:
                os.environ["QA_TIME_EXPAND_LADDER"] = previous

    def test_broken_ladder_falls_back_to_default(self):
        previous = os.environ.get("QA_TIME_EXPAND_LADDER")
        os.environ["QA_TIME_EXPAND_LADDER"] = "abc,,0"
        try:
            self.assertEqual(qr._time_ladder(), [90, 180, 365])
        finally:
            if previous is None:
                os.environ.pop("QA_TIME_EXPAND_LADDER", None)
            else:
                os.environ["QA_TIME_EXPAND_LADDER"] = previous


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""trend_detect 单测：覆盖五态判定、爆发检测、退化场景。"""

import unittest

from trend_detect import (
    BURSTING,
    DECLINING,
    EMERGING,
    MATURE,
    RISING,
    analyze,
    compute_state,
    detect_burst,
)

WINDOW = 7


class TrendDetectTest(unittest.TestCase):
    def test_bursting(self):
        # baseline 平稳 2/天，近窗突增至 ~9/天
        series = [2] * 7 + [2, 2, 2, 15, 16, 14, 15]
        self.assertEqual(compute_state(series, WINDOW), BURSTING)
        self.assertTrue(detect_burst(series, WINDOW)["is_burst"])

    def test_rising(self):
        # 稳步上升，但未到爆发
        series = [3] * 7 + [4, 4, 5, 5, 5, 6, 6]
        self.assertEqual(compute_state(series, WINDOW), RISING)
        self.assertFalse(detect_burst(series, WINDOW)["is_burst"])

    def test_declining(self):
        # 高位 baseline，近窗大幅滑落
        series = [10] * 7 + [3, 2, 2, 1, 1, 1, 2]
        self.assertEqual(compute_state(series, WINDOW), DECLINING)

    def test_mature(self):
        # 高位平稳波动
        series = [8] * 7 + [7, 8, 8, 9, 8, 7, 8]
        self.assertEqual(compute_state(series, WINDOW), MATURE)

    def test_emerging_short_series(self):
        # 数据点不足 window → 强制 EMERGING
        self.assertEqual(compute_state([1, 2, 3], WINDOW), EMERGING)

    def test_empty_and_single_not_crash(self):
        self.assertEqual(compute_state([], WINDOW), EMERGING)
        self.assertEqual(compute_state([5], WINDOW), EMERGING)
        # detect_burst 不能崩
        r = detect_burst([], WINDOW)
        self.assertFalse(r["is_burst"])
        self.assertEqual(r["burst_score"], 0.0)

    def test_analyze_fields(self):
        r = analyze([2] * 7 + [2, 2, 2, 15, 16, 14, 15], WINDOW)
        for key in ("state", "is_burst", "burst_score", "recent_avg", "baseline_avg", "slope", "n"):
            self.assertIn(key, r)
        self.assertEqual(r["n"], 14)
        self.assertGreater(r["recent_avg"], r["baseline_avg"])

    def test_burst_score_positive_on_rise(self):
        r = analyze([3] * 7 + [4, 4, 5, 5, 5, 6, 6], WINDOW)
        self.assertGreater(r["burst_score"], 0.0)

    def test_no_false_burst_on_constant_zero(self):
        # 全 0 序列不应误报爆发
        self.assertFalse(detect_burst([0] * 14, WINDOW)["is_burst"])
        self.assertEqual(compute_state([0] * 14, WINDOW), EMERGING)

    def test_full_history_window_split(self):
        # n >= 2w 时 baseline/recent 各取 w 个，状态仍可判定
        series = [1] * 10 + [1, 1, 2, 2, 12, 13, 11]
        self.assertEqual(compute_state(series, WINDOW), BURSTING)


if __name__ == "__main__":
    unittest.main()

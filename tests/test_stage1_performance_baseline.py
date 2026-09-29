#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import unittest

from tools.benchmark_stage1 import (
    parse_sse_event_line,
    percentile,
    summarize_samples,
)


class StageOnePerformanceBaselineTests(unittest.TestCase):
    def test_percentile_and_summary_are_stable(self):
        samples = [
            {"elapsed_ms": 30, "success": True},
            {"elapsed_ms": 10, "success": True},
            {"elapsed_ms": 20, "success": False, "error_type": "Timeout"},
        ]
        self.assertEqual(percentile([10, 20, 30], 0.95), 29.0)
        summary = summarize_samples(samples, scope="local_deterministic")
        self.assertEqual(summary["median_ms"], 20.0)
        self.assertEqual(summary["p95_ms"], 29.0)
        self.assertEqual(summary["success_count"], 2)
        self.assertEqual(summary["error_types"], ["Timeout"])

    def test_sse_parser_accepts_data_only_without_retaining_secret_headers(self):
        self.assertEqual(
            parse_sse_event_line('data: {"type":"chunk","content":"ok"}'),
            {"type": "chunk", "content": "ok"},
        )
        self.assertIsNone(parse_sse_event_line("event: chunk"))
        self.assertIsNone(parse_sse_event_line("data: not-json"))


if __name__ == "__main__":
    unittest.main()

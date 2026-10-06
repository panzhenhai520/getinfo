#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""证据解析（第 6 项）与词元影响力（第 7 项一期）的纯函数级回归测试。

不连库、不调模型：只钉住"句子切分 / 词元重叠 / 续写对齐"这些判定基础，
避免以后改动把归因的判据悄悄改坏。
"""
import unittest

from qa_attribution import (
    _LogprobScorer,
    _OverlapScorer,
    _overlap,
    _terms,
    split_sentences,
)


class SentenceSplitTests(unittest.TestCase):
    def test_split_chinese_and_english_sentences(self):
        text = "香港已实施税务宽免。政策自2023年生效！是否覆盖单一家族办公室？Yes it does."
        parts = split_sentences(text)
        self.assertEqual(4, len(parts))
        self.assertTrue(parts[0].startswith("香港已实施"))

    def test_strip_inline_citations_and_bullets(self):
        parts = split_sentences("- 结论一：税率 4.25% [1]\n2. 结论二：门槛 2.4 亿 [2]")
        self.assertEqual(["结论一：税率 4.25%", "结论二：门槛 2.4 亿"], parts)

    def test_short_fragments_are_dropped(self):
        self.assertEqual([], split_sentences("好。"))

    def test_limit_is_respected(self):
        self.assertEqual(3, len(split_sentences("一二三四五六。\n" * 10, limit=3)))


class TermOverlapTests(unittest.TestCase):
    def test_terms_include_chinese_bigrams(self):
        terms = _terms("税务宽免")
        self.assertIn("税务", terms)
        self.assertIn("宽免", terms)

    def test_overlap_is_bounded_and_symmetric_in_range(self):
        high = _overlap("香港家族办公室税务宽免", "香港家族办公室税务宽免安排已生效")
        low = _overlap("香港家族办公室税务宽免", "北极熊的狩猎习性")
        self.assertGreater(high, low)
        self.assertLessEqual(high, 1.0)
        self.assertEqual(0.0, low)

    def test_overlap_handles_empty(self):
        self.assertEqual(0.0, _overlap("", "任意"))
        self.assertEqual(0.0, _overlap("任意", ""))


class ContinuationAlignmentTests(unittest.TestCase):
    def test_match_prefix_accepts_matching_continuation(self):
        matched, used = _LogprobScorer._match_prefix("发生变化。", "发生变化，随后生效。", [-1.0, -2.0, -3.0])
        self.assertGreaterEqual(matched, 2)
        self.assertTrue(used)

    def test_match_prefix_strips_leading_noise(self):
        matched, _used = _LogprobScorer._match_prefix("发生变化。", "\n- 发生变化，随后生效。", [-1.0])
        self.assertGreaterEqual(matched, 2)

    def test_match_prefix_rejects_divergent_continuation(self):
        matched, used = _LogprobScorer._match_prefix("发生变化。", "完全不同的续写", [-1.0])
        self.assertEqual(0, matched)
        self.assertEqual([], used)

    def test_match_prefix_returns_zero_without_logprobs(self):
        matched, used = _LogprobScorer._match_prefix("发生变化。", "发生变化", [])
        self.assertEqual(0, matched)


class OverlapScorerTests(unittest.TestCase):
    def test_proxy_scores_more_evidence_higher(self):
        scorer = _OverlapScorer()
        target = "香港家族办公室税务宽免已生效"
        with_evidence = scorer(target, ["香港家族办公室税务宽免安排已生效。", "无关句子。"])
        without = scorer(target, ["无关句子。"])
        self.assertGreater(with_evidence, without)
        self.assertEqual("occlusion_overlap_proxy", scorer.method)

    def test_proxy_token_influence_only_reports_present_tokens(self):
        scorer = _OverlapScorer()
        target = "香港税务宽免"
        tokens = scorer.token_influence(target, ["香港税务宽免安排"], 0)
        self.assertTrue(all(value >= 0 for _token, value in tokens))
        self.assertTrue(any("香港" in token for token, _value in tokens))

    def test_proxy_empty_context_scores_zero(self):
        self.assertEqual(0.0, _OverlapScorer()("任意结论", []))


if __name__ == "__main__":
    unittest.main()

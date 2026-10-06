#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""embedding 输入上限与 enrich 失败语义的回归测试。

两件事都是生产实测暴露的"纯浪费容量"：
  1. bge-m3（llama.cpp，-ub 512）对 >512 token 的输入一律 500；
     embed_articles 送整篇正文 → 整批 100% 失败、拆单重试仍全失败。
     现在客户端按 token 估算截断，并在服务端回"输入过长"时**缩小输入再试**。
  2. enrich 的「VPN 返回空精炼内容」以前一律可重试，55 个作业烧了 220 次尝试；
     现在区分"内容本身没东西可提炼"（永久失败）与"模型抖动"（可重试）。
"""
import unittest
from unittest import mock

from embedding_client import EmbeddingClient, _estimate_tokens, _truncate_to_tokens
from intel_worker import _enrich_failure_is_permanent

LONG_TEXT = "香港家族办公室税务宽免政策。" * 200


class TokenEstimateTests(unittest.TestCase):
    def test_cjk_counts_one_per_char(self):
        text = "香港家族办公室税务宽免"
        self.assertEqual(len(text), _estimate_tokens(text))

    def test_empty_is_zero(self):
        self.assertEqual(0, _estimate_tokens(""))
        self.assertEqual(0, _estimate_tokens(None))

    def test_latin_words_are_grouped(self):
        # 逐字符计数会得到 len(text)；拉丁词应显著低于它（否则 512 限制下什么都塞不下）
        text = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
        estimate = _estimate_tokens(text)
        self.assertLess(estimate, len(text) * 0.6)
        self.assertGreaterEqual(estimate, 10)


class TruncateTests(unittest.TestCase):
    def test_short_text_untouched(self):
        self.assertEqual("香港税务", _truncate_to_tokens("香港税务", 480))

    def test_long_text_truncated_within_budget(self):
        result = _truncate_to_tokens(LONG_TEXT, 480)
        self.assertLessEqual(_estimate_tokens(result), 480)
        self.assertGreater(len(result), 100)

    def test_zero_budget_means_no_limit(self):
        self.assertEqual(LONG_TEXT, _truncate_to_tokens(LONG_TEXT, 0))


class _StubResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class ClientTruncationTests(unittest.TestCase):
    def test_payload_is_truncated_before_send(self):
        client = EmbeddingClient("http://127.0.0.1:1", "bge-m3", max_input_tokens=480)
        seen = {}

        def fake_post(url, json=None, timeout=None):
            seen["payload"] = json
            count = len(json["input"])
            return _StubResponse(200, {"data": [
                {"index": index, "embedding": [0.0, 1.0]} for index in range(count)]})

        with mock.patch("embedding_client.requests.post", side_effect=fake_post):
            vectors = client.embed_batch([LONG_TEXT, "短文"])
        self.assertEqual(2, len(vectors))
        for text in seen["payload"]["input"]:
            self.assertLessEqual(_estimate_tokens(text), 480)

    def test_shrinks_input_when_server_says_too_large(self):
        client = EmbeddingClient("http://127.0.0.1:1", "bge-m3",
                                 max_input_tokens=480, max_retries=2)
        attempts = []

        def fake_post(url, json=None, timeout=None):
            attempts.append(max(_estimate_tokens(t) for t in json["input"]))
            if len(attempts) == 1:
                return _StubResponse(
                    500, text='{"error":{"message":"input (859 tokens) is too large to process. '
                              'increase the physical batch size (current batch size: 512)"}}')
            count = len(json["input"])
            return _StubResponse(200, {"data": [
                {"index": index, "embedding": [0.0, 1.0]} for index in range(count)]})

        with mock.patch("embedding_client.requests.post", side_effect=fake_post), \
                mock.patch("embedding_client.time.sleep"):
            vectors = client.embed_batch([LONG_TEXT])
        self.assertEqual(1, len(vectors))
        self.assertEqual(2, len(attempts), "第一次失败后应缩小输入重试，而不是原样重试")
        self.assertLess(attempts[1], attempts[0], "重试时的输入必须更短")

    def test_unrelated_500_does_not_loop_shrinking(self):
        client = EmbeddingClient("http://127.0.0.1:1", "bge-m3",
                                 max_input_tokens=480, max_retries=1)

        def fake_post(url, json=None, timeout=None):
            return _StubResponse(500, text="internal server error")

        with mock.patch("embedding_client.requests.post", side_effect=fake_post), \
                mock.patch("embedding_client.time.sleep"):
            with self.assertRaises(Exception):
                client.embed_batch(["短文"])


class EnrichFailureSemanticsTests(unittest.TestCase):
    def test_irrelevant_content_is_permanent(self):
        self.assertTrue(_enrich_failure_is_permanent({"relevance": "none"}, "长" * 500))

    def test_listing_page_is_permanent(self):
        self.assertTrue(_enrich_failure_is_permanent({"content_type": "list"}, "长" * 500))

    def test_short_source_is_permanent(self):
        self.assertTrue(_enrich_failure_is_permanent({}, "太短了"))

    def test_refinable_content_stays_retryable(self):
        self.assertFalse(_enrich_failure_is_permanent(
            {"relevance": "high", "content_type": "article"}, "正" * 800))

    def test_missing_metadata_with_long_content_is_retryable(self):
        self.assertFalse(_enrich_failure_is_permanent({}, "正" * 800))

    def test_bad_shapes_do_not_crash(self):
        self.assertTrue(_enrich_failure_is_permanent(None, ""))
        self.assertFalse(_enrich_failure_is_permanent(None, "正" * 800))


if __name__ == "__main__":
    unittest.main()

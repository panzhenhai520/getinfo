#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""embedding_client 单测：用 mock 覆盖分片/排序/重试/错误路径，不依赖网络。"""

import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from embedding_client import EmbeddingClient, EmbeddingError


def _resp(data, status=200):
    """构造一个 fake requests.Response。"""
    r = MagicMock()
    r.status_code = status
    r.json.return_value = {"data": data}
    r.text = ""
    return r


def _vec(index, dim=4):
    """构造一条 embedding 结果，向量值取 index 便于断言顺序。"""
    return {"index": index, "embedding": [float(index)] * dim}


class EmbeddingClientTest(unittest.TestCase):
    def test_embed_batch_basic(self):
        client = EmbeddingClient(batch_size=8)
        with patch("embedding_client.requests.post", return_value=_resp([_vec(0), _vec(1)])) as mp:
            out = client.embed_batch(["a", "b"])
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].shape, (4,))
        self.assertEqual(out[0].dtype, np.float32)
        mp.assert_called_once()

    def test_embed_single(self):
        client = EmbeddingClient()
        with patch("embedding_client.requests.post", return_value=_resp([_vec(0, 1024)])):
            v = client.embed("hello")
        self.assertEqual(v.shape, (1024,))

    def test_embed_empty_raises(self):
        client = EmbeddingClient()
        with self.assertRaises(EmbeddingError):
            client.embed("   ")

    def test_batch_slicing(self):
        # batch_size=2，4 条文本应触发 2 次请求
        client = EmbeddingClient(batch_size=2)
        with patch(
            "embedding_client.requests.post",
            side_effect=[_resp([_vec(0), _vec(1)]), _resp([_vec(0), _vec(1)])],
        ) as mp:
            out = client.embed_batch(["a", "b", "c", "d"])
        self.assertEqual(len(out), 4)
        self.assertEqual(mp.call_count, 2)

    def test_index_ordering(self):
        # 服务返回乱序 index，应按 index 还原请求顺序
        client = EmbeddingClient()
        with patch("embedding_client.requests.post", return_value=_resp([_vec(1), _vec(0)])):
            out = client.embed_batch(["a", "b"])
        self.assertAlmostEqual(float(out[0][0]), 0.0)
        self.assertAlmostEqual(float(out[1][0]), 1.0)

    def test_retry_then_success(self):
        # 首次 500，重试后 200 → 成功
        client = EmbeddingClient(max_retries=2)
        with patch(
            "embedding_client.requests.post",
            side_effect=[_resp(None, status=500), _resp([_vec(0)])],
        ), patch("embedding_client.time.sleep"):
            out = client.embed_batch(["a"])
        self.assertEqual(len(out), 1)

    def test_retry_exhausted(self):
        # 重试耗尽仍失败 → 抛 EmbeddingError
        client = EmbeddingClient(max_retries=1)
        with patch("embedding_client.requests.post", return_value=_resp(None, status=500)), patch(
            "embedding_client.time.sleep"
        ):
            with self.assertRaises(EmbeddingError):
                client.embed_batch(["a"])

    def test_count_mismatch(self):
        client = EmbeddingClient()
        with patch("embedding_client.requests.post", return_value=_resp([_vec(0)])):
            with self.assertRaises(EmbeddingError):
                client.embed_batch(["a", "b"])

    def test_health_check_ok(self):
        client = EmbeddingClient()
        with patch("embedding_client.requests.get", return_value=_resp(None, status=200)):
            self.assertTrue(client.health_check())

    def test_health_check_fail(self):
        client = EmbeddingClient()
        with patch("embedding_client.requests.get", side_effect=Exception("boom")):
            self.assertFalse(client.health_check())


if __name__ == "__main__":
    unittest.main()

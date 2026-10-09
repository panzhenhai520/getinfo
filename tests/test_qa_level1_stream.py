#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 6-6 · 流式首字节测试（真 HTTP + SSE，不占用 GPU）。

为什么要用桩而不是直接打真模型：这个改动只动"传输层 + 首字回调 + 失败回退"，
用真 SSE 服务能精确验证这三件事（含分块边界、非 200 回退）；真模型的生成逻辑没变。
桩服务按 llama.cpp/OpenAI 的 SSE 格式逐块下发，行为与真端点一致。

要钉住：
  1. 首个 chunk 到达**立刻**回调（不等整段生成完），回调收到的是耗时秒数；
  2. 拼接出的原文与各 chunk 串起来完全一致（不能丢字/串字）；
  3. 流式失败（HTTP 500 / 端点不可用）时**回退非流式**，不会把整条草稿搞挂。
"""
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qa_level1 import QaLevel1Generator  # noqa: E402

CHUNKS = ['{"claims":[', '{"claim_id":"l1-c1",', '"text":"测试结论",', '"claim_type":"current_fact"}',
          '],"status":"unverified"}']


class _StubProfile:
    provider_id = "local"
    api_key = ""
    use_proxy = False

    def __init__(self, base_url):
        self.base_url = base_url
        self.model_id = "stub-model"


def _make_handler(chunks, *, status=200, delay=0.05, drop_after=None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # 静音
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            if status != 200:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            payloads = chunks if drop_after is None else chunks[:drop_after]
            for index, piece in enumerate(payloads):
                if index:
                    time.sleep(delay)
                body = json.dumps({"choices": [{"delta": {"content": piece}}]}, ensure_ascii=False)
                data = ("data: %s\n\n" % body).encode("utf-8")
                self.wfile.write(("%x\r\n" % len(data)).encode("ascii") + data + b"\r\n")
                self.wfile.flush()
            tail = b"data: [DONE]\n\n"
            self.wfile.write(("%x\r\n" % len(tail)).encode("ascii") + tail + b"\r\n")
            # chunked 结束标记：不写它客户端会一直等（实测把测试挂到超时）
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    return Handler


class StreamingFirstByteTests(unittest.TestCase):
    def setUp(self):
        # 预热出站白名单缓存：它的冷启动要数秒（会拉起 chat_api 配置），
        # 那是进程级一次性开销，不属于"首字延迟"的度量范围。
        try:
            from qa_observability import provider_allowed_hosts

            provider_allowed_hosts(force=True)
        except Exception:
            pass

    def _serve(self, chunks, **kwargs):
        server = HTTPServer(("127.0.0.1", 0), _make_handler(chunks, **kwargs))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return "http://127.0.0.1:%d/v1" % server.server_port

    def _evidence(self):
        return [{"evidence_ref": "article:1", "source_type": "article", "title": "测试来源",
                 "content_excerpt": "测试正文", "source_url": "https://example.com/1",
                 "authority_level": 50, "published_at": "2026-10-01", "metadata": {}}]

    def test_first_chunk_reports_immediately(self):
        base = self._serve(CHUNKS, delay=0.25)
        generator = QaLevel1Generator()
        calls = []
        result = generator.generate(
            question="测试问题", plan={"question": "测试问题"}, evidence=self._evidence(),
            profile=_StubProfile(base),
            first_token_callback=lambda seconds: calls.append(seconds),
        )
        self.assertEqual(len(calls), 1, "首字回调只能触发一次")
        self.assertLess(calls[0], 0.2,
                        "首字回调必须在第一个 chunk 到达时就触发，而不是等整段生成完（实测 %.2fs）" % calls[0])
        self.assertEqual(result["claims"][0]["text"], "测试结论")

    def test_streamed_text_matches_all_chunks(self):
        base = self._serve(CHUNKS, delay=0.0)
        generator = QaLevel1Generator()
        raw = generator._stream_raw(_StubProfile(base), [{"role": "user", "content": "x"}],
                                   30, lambda seconds: None)
        self.assertEqual(raw, "".join(CHUNKS), "拼接结果必须与各 chunk 完全一致")

    def test_stream_failure_falls_back_to_non_streaming(self):
        base = self._serve(CHUNKS, status=500)
        generator = QaLevel1Generator()
        used = {"non_stream": 0}
        original = generator.model_client

        def _fake_client(profile, messages, *, timeout=90):
            used["non_stream"] += 1
            return "".join(CHUNKS)

        generator.model_client = _fake_client
        result = generator.generate(
            question="测试问题", plan={"question": "测试问题"}, evidence=self._evidence(),
            profile=_StubProfile(base), first_token_callback=lambda seconds: None,
        )
        self.assertEqual(used["non_stream"], 1, "流式失败必须回退到非流式调用")
        self.assertEqual(result["claims"][0]["claim_id"], "l1-c1")
        generator.model_client = original

    def test_no_callback_keeps_non_streaming_path(self):
        base = self._serve(CHUNKS)
        generator = QaLevel1Generator()
        used = {"non_stream": 0}

        def _fake_client(profile, messages, *, timeout=90):
            used["non_stream"] += 1
            return "".join(CHUNKS)

        generator.model_client = _fake_client
        generator.generate(question="测试问题", plan={"question": "测试问题"},
                           evidence=self._evidence(), profile=_StubProfile(base))
        self.assertEqual(used["non_stream"], 1, "没传首字回调时不该走流式（保持旧行为）")


if __name__ == "__main__":
    unittest.main()

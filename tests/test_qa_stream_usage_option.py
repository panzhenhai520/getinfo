#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""流式 token 用量 · `stream_options.include_usage` 开关与 400 回退守门测试。

背景（A 机实测真端点 http://192.168.0.18:8081/v1 · llama.cpp）：
  · 不带 `stream_options`：SSE 3 个 chunk、usage 块 0 个 → 流式路径永远采不到用量，
    Cost 基线恒为 0（生成侧已经能把末块 usage 归一/累加，只是请求时从没要过它）；
  · 带 `{"stream_options": {"include_usage": true}}`：SSE 4 个 chunk、usage 块 1 个，
    HTTP 200（该端点接受这个参数，不报 400）。

这里钉住五件事（全部走本地 SSE 桩，不连真端点）：
  1. 默认开启：两条流式路径（qa_level1 草稿 / qa_synthesis 终稿）的请求体都带
     `stream_options.include_usage == True`；
  2. `QA_STREAM_INCLUDE_USAGE=0` 时请求体里**没有** `stream_options` 键（一键回滚旧行为）；
  3. 400 且报错点名 stream_options/include_usage → 摘掉参数**原样重发一次**（仍是流式），
     内容照旧解析、用量照旧采到，且**不**退化成非流式；
  4. 400 但报错没点名 → **不重试**（只请求一次），照旧走既有失败路径（退回非流式）；
  5. 末块 usage（真端点形状，多带 `prompt_tokens_details.cached_tokens`）能复用既有归一
     函数变成 {"tokens_in","tokens_out","tokens_total"}。
"""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qa_level1 import (  # noqa: E402
    QaLevel1Generator,
    _merge_token_usage,
    _normalize_token_usage,
)
from qa_synthesis import _stream_openai_json_content  # noqa: E402

# 合法的一级 JSON 原文，切块下发（与 tests/test_qa_token_usage_capture.py 同款）
CHUNKS = ['{"claims":[', '{"claim_id":"l1-c1","text":"测试结论",',
          '"claim_type":"current_fact","evidence_refs":["article:1"],',
          '"verification_status":"unverified"}],"entities":[],"timeline_hints":[],',
          '"gaps":[],"followup_queries":[],"citations":["article:1"]}']

# 真端点实测的用量形状（多一个 prompt_tokens_details.cached_tokens，归一函数必须忽略它）
REAL_USAGE = {"completion_tokens": 1, "prompt_tokens": 18, "total_tokens": 19,
              "prompt_tokens_details": {"cached_tokens": 14}}

SWITCH = "QA_STREAM_INCLUDE_USAGE"


def _delta(text):
    """一个内容块（llama.cpp / OpenAI 兼容 SSE 形态）。"""
    return json.dumps({"choices": [{"delta": {"content": text}}]}, ensure_ascii=False)


def _usage_event(usage):
    """末块：没有 delta，只带 usage —— include_usage 打开时端点才这么回。"""
    return json.dumps({"choices": [{"delta": {}}], "usage": usage}, ensure_ascii=False)


class _StubServer(ThreadingHTTPServer):
    """每个连接一个线程：400 重试会开新连接，单线程服务会被上一个 keep-alive 连接堵死。"""

    daemon_threads = True
    allow_reuse_address = True


class _SseStub:
    """按脚本逐次应答的本地 SSE 桩，并把每次请求体记下来。

    script 每项 = (status, payload)：status==200 时 payload 是 SSE 事件体列表，
    其它状态时 payload 是错误文本。脚本用完后重复最后一项（方便钉住"只请求一次"）。
    """

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.server = None

    def start(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # 静音
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                try:
                    stub.requests.append(json.loads(raw.decode("utf-8")))
                except Exception:
                    stub.requests.append({})
                status, payload = stub.script[min(len(stub.requests) - 1, len(stub.script) - 1)]
                if status != 200:
                    body = str(payload).encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for event in [*payload, "[DONE]"]:
                    chunk = "data: [DONE]\n\n" if event == "[DONE]" else "data: %s\n\n" % event
                    data = chunk.encode("utf-8")
                    self.wfile.write(("%x\r\n" % len(data)).encode("ascii") + data + b"\r\n")
                    self.wfile.flush()
                # chunked 结束标记：不写它客户端会一直等（实测把测试挂到超时）
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

        self.server = _StubServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return "http://127.0.0.1:%d/v1" % self.server.server_port

    def stop(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass


class _StubProfile:
    """桩 profile：只带生成侧会读到的字段，绝不联网。"""

    def __init__(self, base_url, provider_id="local"):
        self.base_url = base_url
        self.model_id = "stub-model"
        self.provider_id = provider_id
        self.api_key = ""
        self.use_proxy = False


class _FallbackClient:
    """非流式回退桩：记录被调用次数（用来证明"没退化成非流式"）。"""

    def __init__(self, reply=None):
        self.reply = "".join(CHUNKS) if reply is None else reply
        self.calls = 0

    def __call__(self, profile, messages, *, timeout=90):
        self.calls += 1
        return self.reply


def _evidence():
    return [{"evidence_ref": "article:1", "source_type": "article", "title": "测试来源",
             "source_url": "https://example.com/1", "content_excerpt": "测试正文内容",
             "published_at": "2026-10-01", "authority_level": 50, "metadata": {}}]


class _StubCase(unittest.TestCase):
    """公共装置：开关复位 + 桩服务 + 预热出站白名单（冷启动要数秒，不属于被测行为）。"""

    def setUp(self):
        try:
            from qa_observability import provider_allowed_hosts

            provider_allowed_hosts(force=True)
        except Exception:
            pass
        # 显式给出超时，避免测试依赖端点能力探测（探测在桩服务上必然失败，只是拖慢时钟）
        os.environ["QA_LEVEL1_LOCAL_TIMEOUT_SECONDS"] = "20"
        os.environ["QA_LEVEL1_LOCAL_REPAIR_TIMEOUT_SECONDS"] = "10"
        os.environ["QA_SYNTHESIS_LOCAL_FIRST_TOKEN_TIMEOUT_SECONDS"] = "20"
        for key in ("QA_LEVEL1_LOCAL_TIMEOUT_SECONDS", "QA_LEVEL1_LOCAL_REPAIR_TIMEOUT_SECONDS",
                    "QA_SYNTHESIS_LOCAL_FIRST_TOKEN_TIMEOUT_SECONDS"):
            self.addCleanup(os.environ.pop, key, None)
        self._switch(None)

    def _switch(self, value):
        """设置 QA_STREAM_INCLUDE_USAGE；value=None 表示"未配置"（默认开启态）。"""
        original = os.environ.get(SWITCH)

        def _restore():
            if original is None:
                os.environ.pop(SWITCH, None)
            else:
                os.environ[SWITCH] = original

        self.addCleanup(_restore)
        if value is None:
            os.environ.pop(SWITCH, None)
        else:
            os.environ[SWITCH] = value

    def serve(self, script):
        stub = _SseStub(script)
        base_url = stub.start()
        self.addCleanup(stub.stop)
        return base_url, stub

    @staticmethod
    def pieces():
        """把一级 JSON 原文切成内容块下发（每块 = 一个 SSE delta）。"""
        return [_delta(piece) for piece in CHUNKS]

    def profile(self, base_url):
        return _StubProfile(base_url)

    def level1(self, base_url, *, model_client=None):
        """跑一次一级草稿（本地 provider + 首字回调 → 走流式路径）。"""
        return QaLevel1Generator(model_client=model_client or _FallbackClient()).generate(
            question="测试问题", plan={"question": "测试问题"}, evidence=_evidence(),
            profile=self.profile(base_url), first_token_callback=lambda seconds: None,
        )

    def synthesis_stream(self, base_url, *, usage_sink=None):
        """直接消费 qa_synthesis 侧的流式生成器（终稿路径用的是同一个函数）。"""
        return "".join(_stream_openai_json_content(
            self.profile(base_url), [{"role": "user", "content": "测试"}],
            timeout=20, usage_sink=usage_sink,
        ))


class StreamOptionsRequestTests(_StubCase):
    """要求 1/2/6：两条流式路径的请求体都受 QA_STREAM_INCLUDE_USAGE 控制。"""

    def test_level1_streaming_requests_usage_by_default(self):
        base_url, stub = self.serve([(200, self.pieces())])

        result = self.level1(base_url)

        self.assertEqual(1, len(stub.requests))
        self.assertEqual({"include_usage": True}, stub.requests[0].get("stream_options"),
                         "默认必须带上 stream_options.include_usage，否则流式采不到用量")
        self.assertEqual("l1-c1", result["claims"][0]["claim_id"])

    def test_level1_streaming_omits_option_when_disabled(self):
        self._switch("0")
        base_url, stub = self.serve([(200, self.pieces())])

        self.level1(base_url)

        self.assertEqual(1, len(stub.requests))
        self.assertNotIn("stream_options", stub.requests[0], "关掉开关后请求体必须回到旧形状")
        self.assertIs(True, stub.requests[0]["stream"], "关掉开关只是不带该参数，仍是流式")

    def test_synthesis_streaming_requests_usage_by_default(self):
        base_url, stub = self.serve([(200, [_delta('{"answer":"x"}')])])

        text = self.synthesis_stream(base_url)

        self.assertEqual('{"answer":"x"}', text)
        self.assertEqual({"include_usage": True}, stub.requests[0].get("stream_options"),
                         "终稿流式路径同样必须索取 usage")

    def test_synthesis_streaming_omits_option_when_disabled(self):
        self._switch("0")
        base_url, stub = self.serve([(200, [_delta('{"answer":"x"}')])])

        self.synthesis_stream(base_url)

        self.assertEqual(1, len(stub.requests))
        self.assertNotIn("stream_options", stub.requests[0])


class BadRequestFallbackTests(_StubCase):
    """要求 3/4：只有"点名了该参数"的 400 才摘参数重发一次，其它错误一律照旧失败。"""

    def test_400_naming_stream_options_retries_once_without_it(self):
        base_url, stub = self.serve([
            (400, '{"error":{"message":"unknown field `stream_options`","type":"invalid_request_error"}}'),
            (200, [*self.pieces(), _usage_event(REAL_USAGE)]),
        ])
        fallback = _FallbackClient()

        result = self.level1(base_url, model_client=fallback)

        self.assertEqual(2, len(stub.requests), "点名 stream_options 的 400 必须摘参数重发一次")
        self.assertEqual({"include_usage": True}, stub.requests[0]["stream_options"])
        self.assertNotIn("stream_options", stub.requests[1], "重试请求不能再带该参数")
        self.assertEqual(0, fallback.calls, "重试成功后不得退化成非流式")
        self.assertEqual("l1-c1", result["claims"][0]["claim_id"],
                         "重试拿到的流式内容必须照旧走既有解析逻辑")
        self.assertEqual({"tokens_in": 18, "tokens_out": 1, "tokens_total": 19},
                         result["token_usage"], "重试成功后的用量同样要采到")

    def test_400_mentioning_include_usage_also_retries(self):
        base_url, stub = self.serve([
            (400, '{"error":"Unsupported parameter: include_usage"}'),
            (200, self.pieces()),
        ])

        result = self.level1(base_url)

        self.assertEqual(2, len(stub.requests), "命中 include_usage 关键词同样要重发")
        self.assertNotIn("stream_options", stub.requests[1])
        self.assertEqual("l1-c1", result["claims"][0]["claim_id"])

    def test_400_without_keyword_does_not_retry(self):
        base_url, stub = self.serve([(400, '{"error":{"message":"invalid value for temperature"}}')])
        fallback = _FallbackClient()

        result = self.level1(base_url, model_client=fallback)

        self.assertEqual(1, len(stub.requests), "没点名的 400 不许重试（不许扩大重试范围）")
        self.assertEqual(1, fallback.calls, "照旧走既有失败路径：流式失败 → 退回非流式")
        self.assertEqual("l1-c1", result["claims"][0]["claim_id"])

    def test_500_is_never_retried(self):
        base_url, stub = self.serve([(500, "boom")])
        fallback = _FallbackClient()

        self.level1(base_url, model_client=fallback)

        self.assertEqual(1, len(stub.requests), "只有 400 才考虑重试，500 照旧失败")
        self.assertEqual(1, fallback.calls)


class StreamedUsageNormalizationTests(_StubCase):
    """要求 5：末块 usage 复用既有归一函数 → 三字段。"""

    def test_usage_tail_becomes_three_fields(self):
        base_url, stub = self.serve([(200, [*self.pieces(), _usage_event(dict(REAL_USAGE))])])

        result = self.level1(base_url)

        self.assertEqual({"tokens_in": 18, "tokens_out": 1, "tokens_total": 19},
                         result["token_usage"], "流式末块的 usage 必须被采到")
        # 真端点形状（多带 prompt_tokens_details.cached_tokens）直接过既有归一函数
        self.assertEqual({"tokens_in": 18, "tokens_out": 1, "tokens_total": 19},
                         _normalize_token_usage({"usage": dict(REAL_USAGE)}))
        self.assertEqual({"tokens_in": 18, "tokens_out": 1, "tokens_total": 19},
                         _merge_token_usage(result["token_usage"]))

    def test_synthesis_sink_captures_usage_tail(self):
        base_url, stub = self.serve([(200, [_delta('{"answer":"x"}'), _usage_event(dict(REAL_USAGE))])])
        sink = {}

        text = self.synthesis_stream(base_url, usage_sink=sink)

        self.assertEqual('{"answer":"x"}', text)
        self.assertEqual({"tokens_in": 18, "tokens_out": 1, "tokens_total": 19}, sink)


if __name__ == "__main__":
    unittest.main()

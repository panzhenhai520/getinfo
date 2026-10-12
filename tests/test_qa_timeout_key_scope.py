#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""超时键口径（乙案）守门测试：**传入值 = 生效值 = 判定值**。

历史坑（`qa_synthesis._stream_openai_json_content` 的 local 分支自己去读 env）：
  ① 一个"合成"命名的键 `QA_SYNTHESIS_LOCAL_FIRST_TOKEN_TIMEOUT_SECONDS` **越界控制**了
     level1 的流式读超时（`qa_level1._stream_raw` 也走这条函数）；
  ② 不设它 = 本地流式读超时**恒 8 秒**，调用方按端点能力推导出来的值被整条丢弃；
  ③ 夹取区间 [4,300] 与调用侧 [0,600] 不一致（写 600 实际只生效 300）；
  ④ 于是调 `QA_LEVEL1_LOCAL_TIMEOUT_SECONDS` 对流式路径**完全无效**。

本文件钉住（全程零网络：`requests.post` 被换成探针，只记录 `kwargs["timeout"]`）：
  1. socket 层源码里**不出现任何 env 键名**（乙案第 1 条：谁都不许在这里读环境变量）；
  2. 兼容性三场景的实测 timeout 三元组：
     · 本机 .env（LEVEL1=120 / SYNTH_FIRST=120）→ level1 (5,120)、synthesis (5,120)；
     · B 机（LEVEL1=240 / SYNTH_FIRST=240 / SYNTH_TIMEOUT=300）→ 两条流式都 (5,240)；
     · 未设该键的机器 → 不再恒 8 秒，改用**端点能力推导值**（level1 取 request_seconds、
       synthesis 取 first_token_seconds）；
  3. 越界消除：设 `SYNTH_FIRST=99` 后 synthesis 得 (5,99)，**level1 仍是 (5,180)**；
  4. 正面回答：`QA_LEVEL1_LOCAL_TIMEOUT_SECONDS` 对流式路径**是**生效的，且它是该路径
     唯一的决定键（改它 → level1 变；改 synthesis 的键 → level1 不变）；
  5. 非流式路径同样各自解析自己的键（`QA_SYNTHESIS_LOCAL_TIMEOUT_SECONDS` /
     `..._REPAIR_...`），不是"一个键管全部"。
"""
import ast
import inspect
import json
import os
import sys
import textwrap
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_endpoint_profile  # noqa: E402
import qa_level1  # noqa: E402
import qa_synthesis  # noqa: E402
from qa_contracts import QaContractError  # noqa: E402

# 本文件用到的全部超时键（每个用例自己决定设/不设，互不串味）
TIMEOUT_KEYS = (
    "QA_LEVEL1_LOCAL_TIMEOUT_SECONDS",
    "QA_LEVEL1_LOCAL_REPAIR_TIMEOUT_SECONDS",
    "QA_LEVEL1_LOCAL_MAX_TOKENS",
    "QA_SYNTHESIS_LOCAL_FIRST_TOKEN_TIMEOUT_SECONDS",
    "QA_SYNTHESIS_LOCAL_TIMEOUT_SECONDS",
    "QA_SYNTHESIS_LOCAL_REPAIR_TIMEOUT_SECONDS",
    "QA_ENDPOINT_REQUEST_TIMEOUT_SECONDS",
    "QA_ENDPOINT_FIRST_TOKEN_TIMEOUT_SECONDS",
    "QA_LEVEL1_STREAM_FIRST_BYTE",
)

_STREAM_BODY = json.dumps({
    "status": "ready",
    "answer": "测试答案 [1]",
    "sections": {"summary": "小结"},
    "claims": ["c1"],
    "conflicts": [],
    "citations": ["article:1"],
}, ensure_ascii=False)


class _FakeStreamResponse:
    """极简流式响应桩：只有流式路径真正会用到的那几个成员。"""

    status_code = 200
    text = ""

    def raise_for_status(self):
        return None

    def close(self):
        return None

    def iter_lines(self):
        yield ('data: {"choices":[{"delta":{"content":%s}}]}'
               % json.dumps(_STREAM_BODY, ensure_ascii=False)).encode("utf-8")
        yield b"data: [DONE]"


class _StubProfile:
    """桩 profile：只带生成侧会读到的字段，绝不联网。"""

    def __init__(self, provider_id="local", base_url="http://127.0.0.1:9/v1"):
        self.base_url = base_url
        self.model_id = "stub-model"
        self.provider_id = provider_id
        self.api_key = ""
        self.use_proxy = False


class _StubClient:
    """非流式回退桩：记录每次调用拿到的 timeout（用来钉住非流式路径的键）。"""

    def __init__(self):
        self.calls = []

    def __call__(self, profile, messages, timeout=90):
        self.calls.append(timeout)
        return ""

    @property
    def last_usage(self):
        return {}


def _evidence():
    return [{"evidence_ref": "article:1", "source_type": "article", "title": "测试来源",
             "source_url": "https://example.com/1", "content_excerpt": "测试正文内容",
             "published_at": "2026-10-01", "authority_level": 50, "metadata": {}}]


def _claim():
    return {"claim_id": "c1", "text": "测试结论", "claim_type": "current_fact", "confidence": 0.8,
            "valid_from": "2026-10-01", "valid_to": None, "scope": [],
            "evidence_refs": ["article:1"], "needs_verification": True,
            "verification_status": "unverified"}


class TimeoutScopeCase(unittest.TestCase):
    """公共装置：环境键逐个复位 + 出站白名单预热 + 探针。"""

    def setUp(self):
        try:
            from qa_observability import provider_allowed_hosts

            provider_allowed_hosts(force=True)
        except Exception:
            pass
        for key in TIMEOUT_KEYS:
            original = os.environ.get(key)
            self.addCleanup(self._restore, key, original)
            os.environ.pop(key, None)
        # 探针：拦截流式路径的 requests.post，只记录 timeout（不发真请求）
        self.posted = []
        probe = mock.Mock(side_effect=self._capture)
        patcher = mock.patch.object(qa_synthesis.requests, "post", probe)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _restore(key, value):
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def _capture(self, url, **kwargs):
        self.posted.append({"url": url, "timeout": kwargs.get("timeout")})
        return _FakeStreamResponse()

    def set_env(self, **values):
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)

    def stream_timeouts(self):
        """探针抓到的 read 超时序列（`(connect, read)` 的第二个数）。"""
        return [item["timeout"][1] if isinstance(item["timeout"], tuple) else item["timeout"]
                for item in self.posted]

    # ── 两条被测路径 ────────────────────────────────────────────────────────
    def run_level1(self):
        """走真调用方：QaLevel1Generator.generate（本地 + 首字回调 → 流式路径）。"""
        return qa_level1.QaLevel1Generator(model_client=_StubClient()).generate(
            question="测试问题", plan={"question": "测试问题"}, evidence=_evidence(),
            profile=_StubProfile(), first_token_callback=lambda seconds: None,
        )

    def run_synthesis(self, *, stream=True, client=None):
        """走真调用方：QaFinalSynthesizer.generate（token_callback → 流式路径）。"""
        synthesizer = qa_synthesis.QaFinalSynthesizer(model_client=client or _StubClient())
        return synthesizer.generate(
            question="测试问题",
            graph={"evidence": _evidence(), "claims": [_claim()], "conflicts": []},
            level1={"gaps": [], "evidence": _evidence()},
            level2={"evidence_gaps": []},
            degradation=[],
            profile=_StubProfile(),
            models={"draft": "local", "research": "RAG检索", "synthesis": "local"},
            token_callback=(lambda delta: None) if stream else None,
        )

    def stub_endpoint_profile(self, **values):
        payload = {"kind": "local", "first_token_seconds": 45, "request_seconds": 180,
                   "repair_seconds": 90, "source": "probe", "evidence": {}}
        payload.update(values)
        return mock.patch.object(qa_endpoint_profile, "endpoint_profile",
                                 side_effect=lambda *args, **kwargs: dict(payload))


class SocketLayerReadsNoEnvTests(TimeoutScopeCase):
    """乙案第 1 条：socket 层**不读任何 env** —— 源码级钉死（改回去必红）。"""

    def test_stream_function_source_has_no_env_keys(self):
        source = inspect.getsource(qa_synthesis._stream_openai_json_content)
        # 只看**可执行代码**（AST 反解，注释与 docstring 自然被去掉）：docstring 里可以
        # 写明"调用方各自解析哪个键"，但代码里绝不许真去读它。
        function = ast.parse(textwrap.dedent(source)).body[0]
        nodes = function.body[1:] if (function.body and isinstance(function.body[0], ast.Expr)
                                      and isinstance(function.body[0].value, ast.Constant)
                                      and isinstance(function.body[0].value.value, str)
                                      ) else function.body
        code = "\n".join(ast.unparse(node) for node in nodes)
        self.assertNotIn("QA_", code,
                         "socket 层不许出现任何配置键名：超时只能由调用方解析后传进来")
        self.assertNotIn("_int_env", code, "socket 层不许读环境变量")
        self.assertNotIn("environ", code, "socket 层不许读环境变量")

    def test_level1_repair_key_is_consumed_by_the_caller(self):
        """补充口径：level1 的键在 `QaLevel1Generator.generate` 里解析（不在 socket 层）。"""
        source = inspect.getsource(qa_level1.QaLevel1Generator.generate)
        self.assertIn("QA_LEVEL1_LOCAL_TIMEOUT_SECONDS", source)
        synth_source = inspect.getsource(qa_synthesis.QaFinalSynthesizer.generate)
        self.assertIn("QA_SYNTHESIS_LOCAL_FIRST_TOKEN_TIMEOUT_SECONDS", synth_source)


class CompatibilityScenariosTests(TimeoutScopeCase):
    """兼容性三场景：两台机器现有行为一个字都不许变，没配键的机器才修。"""

    def test_local_env_level1_120_synthesis_120(self):
        """本机 .env：LEVEL1=120 / SYNTH_FIRST=120 → 两条流式都读 120。"""
        self.set_env(QA_LEVEL1_LOCAL_TIMEOUT_SECONDS=120,
                     QA_SYNTHESIS_LOCAL_FIRST_TOKEN_TIMEOUT_SECONDS=120)

        self.run_level1()
        self.assertEqual([(5, 120)], [item["timeout"] for item in self.posted],
                         "level1 流式读超时必须 = QA_LEVEL1_LOCAL_TIMEOUT_SECONDS")

        self.posted.clear()
        self.run_synthesis()
        self.assertEqual([(5, 120)], [item["timeout"] for item in self.posted],
                         "synthesis 流式读超时必须 = QA_SYNTHESIS_LOCAL_FIRST_TOKEN_TIMEOUT_SECONDS")

    def test_machine_b_level1_240_synthesis_240(self):
        """B 机：LEVEL1=240 / SYNTH_FIRST=240 / SYNTH_TIMEOUT=300 → 两条流式都读 240。"""
        self.set_env(QA_LEVEL1_LOCAL_TIMEOUT_SECONDS=240,
                     QA_SYNTHESIS_LOCAL_FIRST_TOKEN_TIMEOUT_SECONDS=240,
                     QA_SYNTHESIS_LOCAL_TIMEOUT_SECONDS=300)

        self.run_level1()
        self.assertEqual([240], self.stream_timeouts(),
                         "B 机 level1 今天就是 240（只是碰巧）；改后必须**仍然是** 240，且由 LEVEL1 键决定")

        self.posted.clear()
        self.run_synthesis()
        self.assertEqual([240], self.stream_timeouts(),
                         "B 机 synthesis 今天 240 → 改后不变（非流式的 300 不许污染流式路径）")

    def test_unset_keys_fall_back_to_endpoint_derivation(self):
        """未设该键的机器：不再恒 8 秒，改用端点推导值（这正是要修的那个坑）。"""
        with self.stub_endpoint_profile(request_seconds=300, first_token_seconds=150):
            self.run_level1()
            self.assertEqual([300], self.stream_timeouts(),
                             "level1 流式未设键时必须用端点推导的 request_seconds，不许恒 8 秒")

            self.posted.clear()
            self.run_synthesis()
            self.assertEqual([150], self.stream_timeouts(),
                             "synthesis 流式未设键时必须用端点推导的 first_token_seconds")

    def test_unset_keys_without_endpoint_evidence_use_conservative_defaults(self):
        """端点推导也拿不到时的保守兜底：level1 180 / synthesis 120（仍不是 8）。"""
        with mock.patch.object(qa_endpoint_profile, "endpoint_profile",
                               side_effect=RuntimeError("probe unavailable")):
            self.run_level1()
            self.assertEqual([180], self.stream_timeouts())

            self.posted.clear()
            self.run_synthesis()
            self.assertEqual([120], self.stream_timeouts())


class KeyScopeTests(TimeoutScopeCase):
    """越界消除 + 正面回答：谁决定 level1 的流式读超时。"""

    def test_synthesis_key_no_longer_controls_level1(self):
        self.set_env(QA_LEVEL1_LOCAL_TIMEOUT_SECONDS=180)

        self.run_level1()
        self.assertEqual([(5, 180)], [item["timeout"] for item in self.posted])

        # 再设 synthesis 的键：synthesis 立刻听它，而 level1 仍是 180（越界消除）
        self.set_env(QA_SYNTHESIS_LOCAL_FIRST_TOKEN_TIMEOUT_SECONDS=99)
        self.posted.clear()
        self.run_level1()
        self.assertEqual([(5, 180)], [item["timeout"] for item in self.posted],
                         "synthesis 的键**不许**再控制 level1 的流式读超时")
        self.posted.clear()
        self.run_synthesis()
        self.assertEqual([(5, 99)], [item["timeout"] for item in self.posted],
                         "synthesis 的键管自己：改它就只改 synthesis")

    def test_level1_key_is_the_only_decision_for_that_path(self):
        """正面回答：调 `QA_LEVEL1_LOCAL_TIMEOUT_SECONDS` 对流式路径**真的生效**，
        且它是该路径**唯一的决定键**（改它 → 变；改别人的键 → 不变）。"""
        self.set_env(QA_LEVEL1_LOCAL_TIMEOUT_SECONDS=180)
        self.run_level1()
        self.assertEqual([180], self.stream_timeouts(), "① 设 180 → 生效 180")

        self.set_env(QA_LEVEL1_LOCAL_TIMEOUT_SECONDS=240)
        self.posted.clear()
        self.run_level1()
        self.assertEqual([240], self.stream_timeouts(), "② 改成 240 → 立刻跟着变（确实生效）")

        # 把 synthesis 的所有键都改一遍：level1 必须一个字都不变
        self.set_env(QA_SYNTHESIS_LOCAL_FIRST_TOKEN_TIMEOUT_SECONDS=77,
                     QA_SYNTHESIS_LOCAL_TIMEOUT_SECONDS=66,
                     QA_SYNTHESIS_LOCAL_REPAIR_TIMEOUT_SECONDS=55)
        self.posted.clear()
        self.run_level1()
        self.assertEqual([240], self.stream_timeouts(),
                         "③ synthesis 三个键全改 → level1 仍 240（唯一决定键是 LEVEL1 那个）")

    def test_endpoint_derived_value_is_not_clamped_to_300(self):
        """夹取区间口径修正：写 600 必须真的是 600（旧 socket 层夹到 300）。"""
        self.set_env(QA_LEVEL1_LOCAL_TIMEOUT_SECONDS=600,
                     QA_SYNTHESIS_LOCAL_FIRST_TOKEN_TIMEOUT_SECONDS=600)
        self.run_level1()
        self.assertEqual([600], self.stream_timeouts(), "调用侧口径 [0,600] → 600 就是 600")
        self.posted.clear()
        self.run_synthesis()
        self.assertEqual([600], self.stream_timeouts())


class NonStreamKeyScopeTests(TimeoutScopeCase):
    """非流式路径各自解析自己的键（不是"一个键管全部"）。"""

    def test_synthesis_non_stream_uses_its_own_key(self):
        self.set_env(QA_SYNTHESIS_LOCAL_FIRST_TOKEN_TIMEOUT_SECONDS=99,
                     QA_SYNTHESIS_LOCAL_TIMEOUT_SECONDS=300,
                     QA_SYNTHESIS_LOCAL_REPAIR_TIMEOUT_SECONDS=44)
        client = _StubClient()

        self.run_synthesis(stream=False, client=client)

        self.assertEqual([], self.stream_timeouts(), "非流式路径不碰流式端点")
        self.assertEqual([300, 44], client.calls,
                         "非流式首答用 QA_SYNTHESIS_LOCAL_TIMEOUT_SECONDS，修复重试用 ..._REPAIR_...")

    def test_level1_repair_uses_its_own_key(self):
        self.set_env(QA_LEVEL1_LOCAL_TIMEOUT_SECONDS=180,
                     QA_LEVEL1_LOCAL_REPAIR_TIMEOUT_SECONDS=60,
                     QA_SYNTHESIS_LOCAL_FIRST_TOKEN_TIMEOUT_SECONDS=7)
        client = _StubClient()

        # 桩客户端永远返回空串 → 两次契约校验都不过，异常是预期内的
        # （本用例只关心"每次调用拿到的 timeout 是哪个键给的"）
        with self.assertRaises(QaContractError):
            qa_level1.QaLevel1Generator(model_client=client).generate(
                question="测试问题", plan={"question": "测试问题"}, evidence=_evidence(),
                profile=_StubProfile(), first_token_callback=None,
            )

        self.assertEqual([180, 60], client.calls,
                         "非流式首答 180（LEVEL1 键）/ 修复重试 60（REPAIR 键），不被 synthesis 的 7 干扰")


if __name__ == "__main__":
    unittest.main()

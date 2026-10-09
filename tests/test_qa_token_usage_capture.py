#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 00 · P00-04（Cost 基线前置）：模型 token 用量的采集与落库守门测试。

为什么要这份测试：`qa_storage.record_stage()` 一直支持 `details["token_usage"]`
（→ `qa_stage_runs.token_usage_json`），但**没有任何调用方传过**——生成侧从没解析
provider 返回体里的用量字段，于是 A 机实测 114 个 run / 897 行 qa_stage_runs 全是
`token_usage_json='{}'`，验收工具算出的 cost 恒为 0，Cost 基线立不起来。

这里钉住四件事：
  1. 常见返回形状（OpenAI / input-output / llama.cpp / 顶层平铺）都归一成
     {"tokens_in","tokens_out","tokens_total"}；
  2. 拿不到用量就**不写** `token_usage` 键（绝不用 0 冒充）；
  3. 同一阶段的多次模型调用（草稿修复重试、合成引用修复）用量**累加**；
  4. 编排器把阶段输出里的用量搬进 `details["token_usage"]`，真正写进
     `qa_stage_runs.token_usage_json`（隔离临时 sqlite，不连真库），
     且不破坏 `details` 里既有的 result / performance。

用例全部用桩模型客户端，不发任何网络请求。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# 与 tests/test_qa_stale_run_expiry.py 同款隔离：先把库路径指到临时目录，
# 再 import 任何会读 config / sqlite_db 的模块，确保碰不到生产库。
# 用 mkdtemp 而不是 TemporaryDirectory（Windows 上解释器退出时的 rmtree 偶发
# NotADirectoryError，会把 pytest 收尾炸掉，连"共几个通过"都打不出来）。
_TEMP_PATH = tempfile.mkdtemp(prefix="qa-token-usage-")
os.environ["DATABASE_TYPE"] = "sqlite"
os.environ["SQLITE_BACKUP_PATH"] = os.path.join(_TEMP_PATH, "qa-token-usage.sqlite3")
os.environ["DATABASE_PATH"] = os.environ["SQLITE_BACKUP_PATH"]


def tearDownModule():
    shutil.rmtree(_TEMP_PATH, ignore_errors=True)


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qa_level1 import (  # noqa: E402
    QaLevel1Generator,
    _merge_token_usage,
    _normalize_token_usage,
)
from qa_orchestrator import QaOrchestrator  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from qa_synthesis import QaFinalSynthesizer  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402


class _StubProfile:
    """桩 profile：只带生成侧会读到的字段，绝不联网。"""

    api_key = "stub-key"
    use_proxy = False

    def __init__(self, base_url="http://127.0.0.1:1/v1", provider_id="stub"):
        self.base_url = base_url
        self.model_id = "stub-model"
        self.provider_id = provider_id


class _StubModelClient:
    """按调用顺序返回预设原文，并像真客户端一样把用量记在 last_usage 上。

    第 n 次调用取 replies[n]（越界就一直用最后一条），同时把 usages[n] 放进 last_usage；
    没给对应用量就置空 —— 与真客户端"本次没有用量"的行为一致。
    """

    def __init__(self, replies, usages=None):
        self.replies = list(replies)
        self.usages = list(usages or [])
        self.calls = 0
        self.last_usage = {}

    def __call__(self, profile, messages, *, timeout=90):
        index = min(self.calls, len(self.replies) - 1)
        self.calls += 1
        self.last_usage = dict(self.usages[index]) if index < len(self.usages) else {}
        return self.replies[index]


def _evidence():
    return [{"evidence_ref": "article:1", "source_type": "article", "title": "测试来源",
             "source_url": "https://example.com/1", "content_excerpt": "测试正文内容",
             "published_at": "2026-10-01", "authority_level": 50, "metadata": {}}]


def _level1_json():
    return json.dumps({
        "contract_version": "unified-qa-v1",
        "draft_answer": "初步结论",
        "claims": [{"claim_id": "l1-c1", "text": "测试结论", "claim_type": "current_fact",
                    "confidence": 0.6, "evidence_refs": ["article:1"],
                    "verification_status": "unverified"}],
        "entities": [], "timeline_hints": [], "gaps": [], "followup_queries": [],
        "citations": ["article:1"],
    }, ensure_ascii=False)


def _graph():
    return {
        "claims": [{"claim": {"claim_id": "c1", "text": "测试结论一", "claim_type": "current_fact",
                              "confidence": 0.8, "evidence_refs": ["article:1"],
                              "verification_status": "confirmed", "needs_verification": False}}],
        "evidence": _evidence(),
        "conflicts": [],
    }


def _final_json():
    # 答案里不放任何数字：`_unsupported_numbers` 只允许出现证据里已有的数字，
    # 这里干脆不带，避免测试被"证据外数字"的修复分支带走。
    return json.dumps({
        "contract_version": "unified-qa-v1",
        "status": "ready",
        "answer": "这是一段足够长的最终综合回答，只用已核验的证据表述结论，不引入新数字。",
        "sections": {},
        "claims": ["c1"],
        "citations": ["article:1"],
    }, ensure_ascii=False)


class NormalizeTokenUsageTests(unittest.TestCase):
    """归一函数：形状兼容 + 拿不到就不给（不用 0 冒充）。"""

    def test_openai_style_usage(self):
        usage = _normalize_token_usage({"usage": {"prompt_tokens": 120, "completion_tokens": 45,
                                                 "total_tokens": 165}})
        self.assertEqual({"tokens_in": 120, "tokens_out": 45, "tokens_total": 165}, usage)

    def test_input_output_and_llamacpp_styles(self):
        self.assertEqual({"tokens_in": 7, "tokens_out": 2, "tokens_total": 9},
                         _normalize_token_usage({"usage": {"input_tokens": 7, "output_tokens": 2}}))
        self.assertEqual({"tokens_in": 300, "tokens_out": 88, "tokens_total": 388},
                         _normalize_token_usage({"usage": {"prompt_eval_count": 300, "eval_count": 88}}))

    def test_flat_top_level_counts(self):
        self.assertEqual({"tokens_in": 9, "tokens_out": 4, "tokens_total": 13},
                         _normalize_token_usage({"prompt_tokens": 9, "completion_tokens": 4}))
        # 只给一段也要认；缺的那段按 0 计，但**不能**凭空给出 usage（见下一个用例）
        self.assertEqual({"tokens_in": 0, "tokens_out": 5, "tokens_total": 5},
                         _normalize_token_usage({"usage": {"completion_tokens": 5}}))

    def test_merge_sums_each_field(self):
        merged = _merge_token_usage(
            {"tokens_in": 10, "tokens_out": 4, "tokens_total": 14},
            {"usage": {"prompt_tokens": 20, "completion_tokens": 6}},
            {},
            None,
        )
        self.assertEqual({"tokens_in": 30, "tokens_out": 10, "tokens_total": 40}, merged)
        self.assertEqual({}, _merge_token_usage({}, None), "全空时不能造出 0 用量")


class Level1TokenUsageTests(unittest.TestCase):
    """草稿路径：拿不到就不写键；多次调用累加；异常形状不抛异常也不误写。"""

    def test_missing_usage_leaves_no_key(self):
        client = _StubModelClient([_level1_json()])
        result = QaLevel1Generator(model_client=client).generate(
            question="测试问题", plan={"question": "测试问题"}, evidence=_evidence(),
            profile=_StubProfile(),
        )
        self.assertNotIn("token_usage", result, "拿不到用量时必须不写这个键，而不是写 0")
        self.assertEqual("l1-c1", result["claims"][0]["claim_id"])

    def test_repair_retry_accumulates_usage(self):
        client = _StubModelClient(
            replies=["不是 JSON", _level1_json()],
            usages=[{"usage": {"prompt_tokens": 200, "completion_tokens": 30, "total_tokens": 230}},
                    {"usage": {"prompt_eval_count": 260, "eval_count": 40}}],
        )
        result = QaLevel1Generator(model_client=client).generate(
            question="测试问题", plan={"question": "测试问题"}, evidence=_evidence(),
            profile=_StubProfile(),
        )
        self.assertEqual(2, client.calls, "首答不合法时必须走一次修复重试")
        self.assertEqual({"tokens_in": 460, "tokens_out": 70, "tokens_total": 530},
                         result["token_usage"], "同一阶段两次调用的用量必须累加")

    def test_weird_shapes_never_raise_and_never_write(self):
        for weird in ({"usage": "oops"}, {"usage": [1, 2]}, {"usage": None},
                      {"usage": {"prompt_tokens": -3, "completion_tokens": -1}},
                      {"usage": {"prompt_tokens": "abc", "completion_tokens": True}}):
            with self.subTest(weird=weird):
                self.assertEqual({}, _normalize_token_usage(weird))
                client = _StubModelClient([_level1_json()], usages=[weird])
                result = QaLevel1Generator(model_client=client).generate(
                    question="测试问题", plan={"question": "测试问题"}, evidence=_evidence(),
                    profile=_StubProfile(),
                )
                self.assertNotIn("token_usage", result, "异常形状一律当没拿到，不许误写")
                self.assertEqual("l1-c1", result["claims"][0]["claim_id"])


class SynthesisTokenUsageTests(unittest.TestCase):
    """终稿路径：引用修复重试同样累加。"""

    def test_repair_retry_accumulates_usage(self):
        client = _StubModelClient(
            replies=["不是 JSON", _final_json()],
            usages=[{"usage": {"prompt_tokens": 400, "completion_tokens": 60, "total_tokens": 460}},
                    {"usage": {"input_tokens": 500, "output_tokens": 100}}],
        )
        result = QaFinalSynthesizer(model_client=client).generate(
            question="测试问题", graph=_graph(), level1={"gaps": []}, level2={},
            degradation=[], profile=_StubProfile(), models={"draft": "stub"},
        )
        self.assertEqual(2, client.calls, "首答不合法时必须走一次修复重试")
        self.assertEqual({"tokens_in": 900, "tokens_out": 160, "tokens_total": 1060},
                         result["token_usage"])

    def test_missing_usage_leaves_no_key(self):
        client = _StubModelClient([_final_json()])
        result = QaFinalSynthesizer(model_client=client).generate(
            question="测试问题", graph=_graph(), level1={"gaps": []}, level2={},
            degradation=[], profile=_StubProfile(), models={"draft": "stub"},
        )
        self.assertNotIn("token_usage", result)


class Level1StreamingTokenUsageTests(unittest.TestCase):
    """流式路径（本地模型默认走这条）：最后一块带的 usage 也要采到。

    用真 HTTP + SSE 桩：流式是 llama.cpp/OpenAI 兼容端点的默认用法，
    `usage` 一般只在**最后一块**出现（且是整段累计值），必须取最后一块而不是累加。
    """

    CHUNKS = ['{"claims":[', '{"claim_id":"l1-c1","text":"测试结论",',
              '"claim_type":"current_fact","evidence_refs":["article:1"],',
              '"verification_status":"unverified"}],"entities":[],"timeline_hints":[],',
              '"gaps":[],"followup_queries":[],"citations":["article:1"]}']

    def setUp(self):
        try:
            from qa_observability import provider_allowed_hosts

            provider_allowed_hosts(force=True)
        except Exception:
            pass
        # 显式给出超时，避免测试依赖端点探测（探测在桩服务上必然失败，只是拖慢时钟）
        os.environ["QA_LEVEL1_LOCAL_TIMEOUT_SECONDS"] = "20"
        os.environ["QA_LEVEL1_LOCAL_REPAIR_TIMEOUT_SECONDS"] = "10"
        self.addCleanup(os.environ.pop, "QA_LEVEL1_LOCAL_TIMEOUT_SECONDS", None)
        self.addCleanup(os.environ.pop, "QA_LEVEL1_LOCAL_REPAIR_TIMEOUT_SECONDS", None)

    def _serve(self, chunks, usage):
        import json as _json
        import threading
        import time as _time
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for piece in chunks:
                    body = _json.dumps({"choices": [{"delta": {"content": piece}}]}, ensure_ascii=False)
                    data = ("data: %s\n\n" % body).encode("utf-8")
                    self.wfile.write(("%x\r\n" % len(data)).encode("ascii") + data + b"\r\n")
                    self.wfile.flush()
                # 末块：没有 delta，只带 usage（llama.cpp / include_usage 的形态）
                tail_body = _json.dumps({"choices": [{"delta": {}}], "usage": usage}, ensure_ascii=False)
                tail = ("data: %s\n\n" % tail_body).encode("utf-8")
                self.wfile.write(("%x\r\n" % len(tail)).encode("ascii") + tail + b"\r\n")
                done = b"data: [DONE]\n\n"
                self.wfile.write(("%x\r\n" % len(done)).encode("ascii") + done + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        _time.sleep(0.01)
        return "http://127.0.0.1:%d/v1" % server.server_port

    def test_streamed_usage_is_captured(self):
        base = self._serve(self.CHUNKS, {"prompt_tokens": 640, "completion_tokens": 96,
                                         "total_tokens": 736})
        result = QaLevel1Generator().generate(
            question="测试问题", plan={"question": "测试问题"}, evidence=_evidence(),
            profile=_StubProfile(base_url=base, provider_id="local"),
            first_token_callback=lambda seconds: None,
        )
        self.assertEqual("l1-c1", result["claims"][0]["claim_id"])
        self.assertEqual({"tokens_in": 640, "tokens_out": 96, "tokens_total": 736},
                         result["token_usage"], "流式末块的 usage 必须被采到")


class OrchestratorTokenUsageStorageTests(unittest.TestCase):
    """落库路径：隔离临时 sqlite + 桩阶段处理器，验证真的写进了 token_usage_json。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "qa.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self.captured = {}

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def _run_orchestrator(self, handlers, *, mode="fast"):
        run = self.store.create_run(
            {"question": "测试问题", "industry_pack_id": "family_office",
             "origin": "api", "mode": mode},
            owner_user_id="user:1", idempotency_key="idem-" + str(len(self.captured)),
        )
        orchestrator = QaOrchestrator(self.store, handlers)
        outcome = orchestrator.execute(run["id"])
        self.assertTrue(outcome["success"], outcome)
        return run["id"]

    def _stage_rows(self, run_id):
        with self.db.lock:
            rows = self.db.connection.execute(
                "SELECT stage, token_usage_json, details_json FROM qa_stage_runs"
                " WHERE run_id=? ORDER BY id", (run_id,),
            ).fetchall()
        return [{"stage": str(row[0]), "token_usage_json": str(row[1]),
                 "details": json.loads(row[2] or "{}")} for row in rows]

    def test_token_usage_is_written_to_stage_row(self):
        def _citation_validation(context):
            # synthesis 的输出会原样进入 citation_validation 的严格 schema 校验
            # （validate_final_answer，additionalProperties=False）——用量键必须已经被摘走
            self.captured["synthesis_output"] = dict(context["outputs"]["synthesis"])
            return {"status": "ready"}

        handlers = {
            "plan": lambda context: {"question_plan": {}},
            "level1_retrieval": lambda context: {"evidence": []},
            "logic_validation": lambda context: {"status": "passed"},
            "level1_draft": lambda context: {
                "draft_answer": "草稿", "token_usage": {"tokens_in": 120, "tokens_out": 40, "tokens_total": 160}},
            "synthesis": lambda context: {
                "answer": "终稿", "token_usage": {"tokens_in": 300, "tokens_out": 200, "tokens_total": 500}},
            "citation_validation": _citation_validation,
        }
        run_id = self._run_orchestrator(handlers)
        rows = {row["stage"]: row for row in self._stage_rows(run_id)}

        self.assertEqual({"tokens_in": 120, "tokens_out": 40, "tokens_total": 160},
                         json.loads(rows["level1_draft"]["token_usage_json"]),
                         "草稿阶段用量必须落到 qa_stage_runs.token_usage_json")
        self.assertEqual({"tokens_in": 300, "tokens_out": 200, "tokens_total": 500},
                         json.loads(rows["synthesis"]["token_usage_json"]))
        self.assertEqual("{}", rows["plan"]["token_usage_json"],
                         "没有用量的阶段保持空对象，不许编数")
        self.assertNotIn("token_usage", self.captured["synthesis_output"],
                         "用量键不能留在交给下游严格 schema 校验的阶段输出里")

    def test_details_keep_result_and_performance(self):
        handlers = {
            "plan": lambda context: {"question_plan": {}},
            "level1_retrieval": lambda context: {"evidence": []},
            "logic_validation": lambda context: {"status": "passed"},
            "level1_draft": lambda context: {"draft_answer": "草稿"},
            "synthesis": lambda context: {"answer": "终稿"},
            "citation_validation": lambda context: {"status": "ready"},
        }
        run_id = self._run_orchestrator(handlers)
        rows = {row["stage"]: row for row in self._stage_rows(run_id)}

        synthesis = rows["synthesis"]
        self.assertEqual({"answer": "终稿"}, synthesis["details"].get("result"),
                         "没有用量时 result 原样保留")
        performance = synthesis["details"].get("performance") or {}
        self.assertIn("elapsed_ms", performance)
        self.assertIn("budget_ms", performance)
        self.assertIn("over_budget", performance)
        self.assertNotIn("token_usage", synthesis["details"],
                         "没拿到用量就不加 token_usage 键（拿不到≠0）")
        self.assertEqual("{}", synthesis["token_usage_json"])

    def test_empty_or_non_mapping_usage_is_not_written(self):
        handlers = {
            "plan": lambda context: {"question_plan": {}},
            "level1_retrieval": lambda context: {"evidence": []},
            "logic_validation": lambda context: {"status": "passed"},
            "level1_draft": lambda context: {"draft_answer": "草稿", "token_usage": {}},
            "synthesis": lambda context: {"answer": "终稿", "token_usage": "不是字典"},
            "citation_validation": lambda context: {"status": "ready"},
        }
        run_id = self._run_orchestrator(handlers)
        rows = {row["stage"]: row for row in self._stage_rows(run_id)}

        self.assertEqual("{}", rows["level1_draft"]["token_usage_json"], "空用量不写")
        self.assertEqual("{}", rows["synthesis"]["token_usage_json"], "非字典用量不写")
        self.assertEqual({"answer": "终稿"}, rows["synthesis"]["details"].get("result"),
                         "认不出的用量键一律摘走（不能流向严格 schema 校验），只是不当成计数落库")
        self.assertNotIn("token_usage", rows["synthesis"]["details"])

    def test_unserializable_usage_never_breaks_the_stage(self):
        """脏用量（NaN/对象）不能让 record_stage 的严格序列化把整条 run 搞挂。"""
        handlers = {
            "plan": lambda context: {"question_plan": {}},
            "level1_retrieval": lambda context: {"evidence": []},
            "logic_validation": lambda context: {"status": "passed"},
            "level1_draft": lambda context: {"draft_answer": "草稿",
                                             "token_usage": {"tokens_in": float("nan")}},
            "synthesis": lambda context: {"answer": "终稿",
                                          "token_usage": {"tokens_in": {"nested": 1}}},
            "citation_validation": lambda context: {"status": "ready"},
        }
        run_id = self._run_orchestrator(handlers)
        rows = {row["stage"]: row for row in self._stage_rows(run_id)}

        self.assertEqual("{}", rows["level1_draft"]["token_usage_json"], "NaN 不落库")
        self.assertEqual("{}", rows["synthesis"]["token_usage_json"], "非计数对象不落库")


if __name__ == "__main__":
    unittest.main()

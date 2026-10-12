#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端钉死：`model_raw_trace`（原始模型输出留痕）从**真实 synthesis** 到**真 sqlite**。

与 `test_qa_synthesis_sections_shape.py` 的分工（那边钉解析层与"直调 handler"）：
本文件每一条都走 **真 `QaOrchestrator` 全链**（其余阶段打桩；`synthesis` 是
`qa_pipeline.build_qa_stage_handlers` 产出的**真处理器**，只把模型客户端与流式取数打桩），
跑完再从**真 sqlite** 里把 `qa_stage_runs.details_json` / `output_hash` 读回来 ——
也就是"库里的那一行到底长什么样"。生产走的是**流式**首答 + **非流式**修复重试，
本文件第一条用例逐字复刻这条形态。

四颗钉子：
  1. 模型两次都返回坏形状（首答流式 / 修复非流式）→ `details_json.model_raw_trace` 是数组、
     长度 = **实际模型调用次数**、逐条 `raw_excerpt` **逐字**等于打桩原文（截断到 2000）；
     同时 `details["result"]`、`output_hash`、下游阶段看到的 synthesis 输出**都不带这个键**；
  2. synthesis **一次就过** → **该键不出现**（"形状正常不留痕"是设计，成功路径逐字不变）；
  3. 模型调用**本身**抛异常（流式读超时，= 生产里那条 `EVIDENCE_ONLY_NO_MODEL` 的路）→
     也必须留下**错误摘要**（这条曾是真漏的路径：没有原文，至少不能连"为什么"都没有）；
  4. `generate()` 的返回值**永远不带**该键（冻结契约 `additionalProperties=False`）。

第五颗钉子（第 5 节，与上面四颗同一套链）：模型**整段拒答**（`_is_model_refusal` 为真的两处
早返回）过去同样丢原文，现在也必须逐字留痕，**判定/记账/返回值一律照旧**。

全程隔离 sqlite + 打桩客户端，零模型 / 零嵌入 / 零网络端点。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

import qa_pipeline  # noqa: E402
import qa_synthesis  # noqa: E402
from qa_contracts import validate_final_answer  # noqa: E402
from qa_orchestrator import QaOrchestrator, _digest  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

QUESTION = "香港家族办公室税收优惠政策对内地高净值客户有什么影响？"
RAW_TRACE = qa_synthesis.RAW_TRACE_KEY

ANSWER_TEXT = "香港家族办公室税收优惠政策对符合条件的管理人给予利得税宽免 [1]。"
# 坏形状：数组（首元素 3 元组，历史上正是它抛出 "length 3; 2 is required"）
BAD_ARRAY = [["summary", "正文", "[1]"]]
# 修复重试用**另一份**坏原文（字符串），用来证明每条留痕各自对应它那一次调用
BAD_STRING = "summary 就是这一段"
REPAIR_MARK = "（修复重试的第二次原文）"


def _evidence(ref, *, authority=60, relation="supports"):
    return {
        "evidence_ref": ref, "source_type": "article",
        "title": "香港家族办公室税收优惠政策 %s" % ref,
        "source_url": "https://example.com/%s" % ref,
        "article_id": int(str(ref).split(":")[-1]),
        "content_excerpt": "香港家族办公室税收优惠政策对符合条件的管理人给予利得税宽免。",
        "published_at": "2026-10-09", "authority_level": authority, "score": 30.0,
        "retrieval_method": "keyword", "match_reason": "标题命中：家族办公室",
        "relationship": relation, "metadata": {"matched_keywords": ["家族办公室"]},
    }


def _claim():
    return {
        "claim_id": "c1",
        "text": "香港家族办公室税收优惠政策对内地高净值客户有影响",
        "claim_type": "current_fact", "confidence": 0.82,
        "valid_from": "2026-10-09", "valid_to": None, "scope": ["家族办公室"],
        "evidence_refs": ["article:1"], "needs_verification": True,
        "verification_status": "unverified",
    }


def _raw(sections, *, answer=ANSWER_TEXT):
    """打桩模型返回的原文：只有 sections 的形状是自变量，其余字段全部合法。"""
    return json.dumps({
        "contract_version": "unified-qa-v1", "status": "ready",
        "answer": answer, "sections": sections,
        "claims": ["c1"], "conflicts": [], "citations": ["article:1"],
    }, ensure_ascii=False)


class _FakeRetriever:
    def retrieve(self, plan, **kwargs):
        return {"evidence": [_evidence("article:1")], "stats": {"eligible": 1, "adopted": 1}}


class _FakeWebSearch:
    def search(self, queries, *, enabled=True, limit=8):
        return {"evidence": [], "status": "disabled", "providers": [], "errors": []}


class _StubProfile:
    provider_id = "stub"
    model_id = "stub"
    base_url = ""
    api_key = ""


class _StubRegistry:
    def resolve(self, role, provider_id, **kwargs):
        return _StubProfile()


class _StubFlags:
    def snapshot(self):
        return {"synthesis_enabled": True, "level2_enabled": True, "enabled": True}


class _StubClient:
    """非流式客户端桩（生产里它只服务**修复重试**那一次调用）：记录次数与超时，零网络。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, profile, messages, timeout=90):
        self.calls.append({"timeout": timeout})
        return self.payload


class _StubStream:
    """`qa_synthesis._stream_openai_json_content` 的替身：吐固定原文，或在首字前抛异常。"""

    def __init__(self, chunks=(), *, raises=None):
        self.chunks = list(chunks)
        self.raises = raises
        self.calls = []

    def __call__(self, profile, messages, *, timeout=90, usage_sink=None, include_usage=None):
        self.calls.append({"timeout": timeout, "include_usage": include_usage})
        if self.raises is not None:
            raise self.raises
        for chunk in self.chunks:
            yield chunk


class _OrchestratorChainBase(unittest.TestCase):
    """真 orchestrator + 真 synthesis 处理器 + 真 sqlite；其余阶段打桩。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "rawtrace.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())
        self.assertEqual(self.db.backend, "sqlite", "测试必须跑在隔离 sqlite 上")
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self.downstream = {}

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:  # noqa: BLE001
            pass
        self.temp_dir.cleanup()

    # ── 搭链 ────────────────────────────────────────────────────────────────
    def _handlers(self, *, stream, client):
        built = qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=_FakeRetriever(), web_search=_FakeWebSearch(),
            store=self.store, provider_registry=_StubRegistry(), feature_flags=_StubFlags(),
            final_synthesizer=qa_synthesis.QaFinalSynthesizer(model_client=client),
        )

        def citation_validation(context):
            # 下游阶段看到的 synthesis 输出：留痕键必须已经被 orchestrator 摘走。
            # 捕获之后**照样跑真处理器**（真过一遍冻结契约 additionalProperties=False）。
            self.downstream["synthesis_output"] = dict(context["outputs"]["synthesis"])
            return built["citation_validation"](context)

        return {
            "plan": lambda context: {
                "question": QUESTION, "standalone_question": QUESTION,
                "queries": ["香港家族办公室 税收优惠"], "entities": ["家族办公室"],
                "decomposition": {"is_multi_hop": False, "pattern": "single", "hop_count": 1,
                                  "hops": [{"id": "h1", "question": QUESTION, "depends_on": []}]}},
            "level1_retrieval": lambda context: {"evidence": [_evidence("article:1")],
                                                 "stats": {"adopted": 1}},
            "logic_validation": lambda context: {"status": "passed"},
            "level1_draft": lambda context: {
                "contract_version": "unified-qa-v1", "draft_answer": "草稿",
                "claims": [_claim()], "gaps": [], "evidence": [_evidence("article:1")]},
            # 真处理器（不是打桩）：留痕的收集与传播就在它里面
            "synthesis": built["synthesis"],
            "citation_validation": citation_validation,
        }

    def _execute(self, *, stream, client, key):
        run = self.store.create_run(
            {"question": QUESTION, "industry_pack_id": "family_office",
             "origin": "api", "mode": "fast"},
            owner_user_id="user:1", idempotency_key=key)
        handlers = self._handlers(stream=stream, client=client)
        with mock.patch.object(qa_synthesis, "_stream_openai_json_content", stream):
            outcome = QaOrchestrator(self.store, handlers).execute(run["id"])
        return str(run["id"]), outcome

    def _details(self, run_id, stage="synthesis"):
        """从真 sqlite 读回那一行（不是从内存里的对象上读）。"""
        with self.db.lock:
            row = self.db.connection.execute(
                "SELECT details_json, output_hash FROM qa_stage_runs"
                " WHERE run_id=? AND stage=?", (run_id, stage)).fetchone()
        self.assertIsNotNone(row, "库里必须有这一行")
        return json.loads(row[0] or "{}"), str(row[1] or "")


# ── 1. 两次都坏形状：留痕必须逐字落进 details_json，且不许污染 result / 下游 ─────────


class TwoBadAttemptsLandRawTraceInDetailsJsonTests(_OrchestratorChainBase):
    """生产形态：首答走流式、修复重试走非流式，两次都是坏形状。"""

    def _run_bad_twice(self, *, first_raw=None, repair_raw=None, key=None):
        first = first_raw if first_raw is not None else _raw(BAD_ARRAY)
        repair = repair_raw if repair_raw is not None else _raw(BAD_STRING, answer=ANSWER_TEXT + REPAIR_MARK)
        stream = _StubStream([first])
        client = _StubClient(payload=repair)
        run_id, outcome = self._execute(stream=stream, client=client,
                                       key=key or "raw-trace-bad-%s" % self.id())
        details, output_hash = self._details(run_id)
        return run_id, outcome, details, output_hash, stream, client, first, repair

    def test_bad_shape_twice_lands_verbatim_trace_in_details_json(self):
        """第一条用例（变异靶子）：留痕落库 + 逐字 + 不污染 result / 下游 / output_hash。"""
        _run, outcome, details, output_hash, stream, client, first, repair = self._run_bad_twice()
        self.assertTrue(outcome["success"], outcome)
        # 真模型调用次数：流式首答 1 次 + 非流式修复 1 次（与生产同形）
        self.assertEqual(1, len(stream.calls))
        self.assertEqual(1, len(client.calls))

        # ① 该键存在、是数组、长度 = attempt 次数
        traces = details.get(RAW_TRACE)
        self.assertIsInstance(traces, list, "留痕必须是数组")
        self.assertEqual([1, 2], [item["attempt"] for item in traces])
        self.assertEqual(len(stream.calls) + len(client.calls), len(traces),
                         "数组长度 = 实际模型调用次数")
        # ② 每个元素含 attempt / raw_excerpt / error，且 raw_excerpt 逐字等于打桩原文
        for item in traces:
            self.assertIsInstance(item["raw_excerpt"], str)
            self.assertTrue(item["error"], "每条留痕都要带 error 摘要")
            self.assertTrue("sections" in item["error"] or "QaContractError" in item["error"],
                            "错误摘要必须点得出问题所在：" + item["error"])
        self.assertEqual(first, traces[0]["raw_excerpt"], "第 1 条逐字等于流式首答的原文")
        self.assertEqual(repair, traces[1]["raw_excerpt"], "第 2 条逐字等于修复重试的原文")
        self.assertNotEqual(traces[0]["raw_excerpt"], traces[1]["raw_excerpt"],
                            "两次原文不同，留痕必须各自对应自己那一次调用")
        self.assertIn(REPAIR_MARK, traces[1]["raw_excerpt"])
        # ③ details 形状：留痕是同级的第 3 个键（打桩客户端没有 token 用量，所以没有 token_usage）
        self.assertEqual({"result", "performance", RAW_TRACE}, set(details))
        # ④ result / 下游 / output_hash 都不受影响
        self.assertNotIn(RAW_TRACE, details["result"],
                         "result 要当'已完成阶段输出'回放，不许夹带留痕")
        self.assertNotIn(RAW_TRACE, self.downstream["synthesis_output"],
                         "留痕流向下一步会撞上 additionalProperties=False")
        self.assertEqual(_digest(details["result"]), output_hash,
                         "output_hash 必须只由'摘掉留痕后的输出'决定")
        self.assertIn("SYNTHESIS_FALLBACK_USED",
                      details["result"]["sections"]["diagnostics"]["degradation_codes"])

    def test_long_raw_output_is_truncated_to_two_thousand_chars(self):
        long_raw = _raw(BAD_ARRAY, answer=ANSWER_TEXT + ("补充说明" * 800))
        self.assertGreater(len(long_raw), qa_synthesis._RAW_TRACE_CHARS)
        _run, _outcome, details, _hash, _stream, _client, _first, _repair = self._run_bad_twice(
            first_raw=long_raw, repair_raw=long_raw, key="raw-trace-long-%s" % self.id())
        self.assertEqual(2, len(details[RAW_TRACE]))
        for item in details[RAW_TRACE]:
            self.assertEqual(qa_synthesis._RAW_TRACE_CHARS, len(item["raw_excerpt"]))
            self.assertEqual(long_raw[:qa_synthesis._RAW_TRACE_CHARS], item["raw_excerpt"])


# ── 2. 一次就过：不该有留痕（成功路径逐字不变）─────────────────────────────────


class SingleGoodAttemptLeavesNoTraceTests(_OrchestratorChainBase):

    def test_success_path_has_no_trace_key_at_all(self):
        stream = _StubStream([_raw({"summary": "模型给的小结"})])
        client = _StubClient(payload=_raw({"summary": "模型给的小结"}))
        run_id, outcome = self._execute(stream=stream, client=client,
                                       key="raw-trace-good-%s" % self.id())
        self.assertTrue(outcome["success"], outcome)
        self.assertEqual(1, len(stream.calls), "形状正常时不许重试")
        self.assertEqual(0, len(client.calls), "一次就过 → 修复重试一次都不该发生")
        details, output_hash = self._details(run_id)
        self.assertNotIn(RAW_TRACE, details, "形状正常不留痕：这是设计，不是漏")
        self.assertEqual({"result", "performance"}, set(details))
        self.assertEqual(_digest(details["result"]), output_hash)
        self.assertNotIn(RAW_TRACE, self.downstream["synthesis_output"])


# ── 3. 模型调用本身抛异常（流式读超时）：至少要留下错误摘要 ──────────────────────


class ModelCallFailureLeavesErrorTraceTests(_OrchestratorChainBase):
    """B 机 run 13038dbc1700412faf3ea51e2058dcee 就是这条路：流式读超时 →
    `EVIDENCE_ONLY_NO_MODEL`，而 `details_json` 里**一条留痕都没有**。"""

    def test_stream_timeout_still_records_an_error_trace(self):
        stream = _StubStream(raises=requests.exceptions.ReadTimeout("simulated: 240s 无首字"))
        client = _StubClient(payload=_raw(BAD_ARRAY))
        run_id, outcome = self._execute(stream=stream, client=client,
                                       key="raw-trace-timeout-%s" % self.id())
        self.assertTrue(outcome["success"], "超时也要出答案（证据摘录）")
        self.assertEqual(0, len(client.calls), "流式就挂了，压根没走到修复重试")

        details, output_hash = self._details(run_id)
        traces = details.get(RAW_TRACE)
        self.assertIsInstance(traces, list, "模型调用真的挂了，正是最需要留痕的场景")
        self.assertEqual(1, len(traces))
        entry = traces[0]
        self.assertEqual(0, entry["attempt"], "0 = 调用本身就没回来，没形成可判定的 attempt")
        self.assertEqual("", entry["raw_excerpt"], "没有原文就空着，绝不伪造")
        self.assertIn("ReadTimeout", entry["error"], entry["error"])
        self.assertTrue(entry.get("note"), "要写清为什么没有原文")
        # 既有记账口径逐字不变：仍然是 EVIDENCE_ONLY_NO_MODEL
        codes = details["result"]["sections"]["diagnostics"]["degradation_codes"]
        self.assertIn("EVIDENCE_ONLY_NO_MODEL", codes)
        self.assertNotIn(RAW_TRACE, details["result"])
        self.assertNotIn(RAW_TRACE, self.downstream["synthesis_output"])
        self.assertEqual(_digest(details["result"]), output_hash)


# ── 4. `generate()` 的返回值永远不带留痕键 ──────────────────────────────────────


class GenerateReturnValueStaysSchemaCleanTests(unittest.TestCase):

    def test_sink_gets_traces_but_return_value_passes_the_frozen_contract(self):
        raw = _raw(BAD_ARRAY)
        sink: dict = {}
        answer = qa_synthesis.QaFinalSynthesizer(model_client=_StubClient(payload=raw)).generate(
            question=QUESTION,
            graph={"claims": [_claim()], "evidence": [_evidence("article:1")], "conflicts": []},
            level1={"gaps": []}, level2={"evidence_gaps": []}, degradation=[],
            profile=_StubProfile(), models={"synthesis": "stub"}, context_pack=None,
            raw_trace_sink=sink)
        self.assertNotIn(RAW_TRACE, answer, "返回值多带一个键就会撞上 additionalProperties=False")
        validate_final_answer(answer)  # 不抛即通过
        self.assertEqual([1, 2], [item["attempt"] for item in sink[RAW_TRACE]])
        for item in sink[RAW_TRACE]:
            self.assertEqual(raw[:qa_synthesis._RAW_TRACE_CHARS], item["raw_excerpt"])

    def test_good_shape_fills_nothing(self):
        sink: dict = {}
        answer = qa_synthesis.QaFinalSynthesizer(
            model_client=_StubClient(payload=_raw({"summary": "模型给的小结"}))).generate(
            question=QUESTION,
            graph={"claims": [_claim()], "evidence": [_evidence("article:1")], "conflicts": []},
            level1={"gaps": []}, level2={"evidence_gaps": []}, degradation=[],
            profile=_StubProfile(), models={"synthesis": "stub"}, context_pack=None,
            raw_trace_sink=sink)
        self.assertEqual({}, sink, "形状正常时不留痕（成功路径逐字不变）")
        self.assertNotIn(RAW_TRACE, answer)


# ── 5. 模型"整段拒答"：判定口径一字不改，但拒答原文**不许再丢** ────────────────────
#
# 背景：`generate()` 里 `_is_model_refusal(raw)` 为真的**两处早返回**（首答拒答 / 修复重试拒答）
# 过去只走 `fallback_final_answer`，`_publish_raw_trace` 在函数末尾 → 拒答原文被丢掉 ——
# 而"模型拒答"恰恰是最需要看原文的一条路。本类钉住：判定/记账/返回值一律照旧（码仍是
# `SYNTHESIS_MODEL_REFUSED`、仍走证据摘录、返回值不带留痕键），只是**多留一份原文**。

REFUSAL_PROSE = "抱歉，我无法回答这个问题。"
"""整段拒答散文（非 JSON、无大括号 → `_is_model_refusal` 判真），逐字比较用。"""

REFUSAL_REPAIR_PROSE = "抱歉，我无法按这个要求处理。"
"""修复重试那一次的拒答散文（与首答那份不同，用来证明两条留痕各对应自己那一次调用）。"""


class RefusalPathLandsRawTraceInDetailsJsonTests(_OrchestratorChainBase):
    """既有口径（一个字不改，这里只是钉住）：拒答 → run 级/阶段级码 = `EVIDENCE_ONLY_NO_MODEL`
    （"证据摘录"），原始拒答码只作 `detail`；答案正文带"触发原因：SYNTHESIS_MODEL_REFUSED"。"""

    def _stage_degradation(self, run_id, stage="synthesis"):
        run = self.store.get_run(run_id) or {}
        return [item for item in (run.get("degradation") or [])
                if isinstance(item, dict) and item.get("stage") == stage]

    def _assert_refusal_behavior_unchanged(self, run_id, details):
        """既有行为断言（不改口径）：仍走证据摘录、原始拒答码只作 detail、与兜底码互斥。"""
        codes = details["result"]["sections"]["diagnostics"]["degradation_codes"]
        self.assertIn(qa_pipeline.EVIDENCE_ONLY_NO_MODEL, codes, "拒答仍走既有的证据摘录路")
        self.assertNotIn("SYNTHESIS_FALLBACK_USED", codes,
                         "拒答那格与契约兜底那格必须互斥（行为不许被这次改动动到）")
        items = self._stage_degradation(run_id)
        self.assertEqual([qa_pipeline.EVIDENCE_ONLY_NO_MODEL], [item.get("code") for item in items])
        self.assertEqual("SYNTHESIS_MODEL_REFUSED", items[0].get("detail"),
                         "原始拒答码只作 detail —— 既有口径")
        self.assertIn("SYNTHESIS_MODEL_REFUSED", str(details["result"].get("answer") or ""),
                      "触发原因必须还在正文里（UI 一眼可见）")

    def test_first_attempt_refusal_lands_verbatim_trace_in_details_json(self):
        """首答（流式）整段拒答：`details_json.model_raw_trace` 里必须逐字留着那句拒答。"""
        stream = _StubStream([REFUSAL_PROSE])
        client = _StubClient(payload=REFUSAL_REPAIR_PROSE)
        run_id, outcome = self._execute(stream=stream, client=client,
                                       key="raw-trace-refusal-%s" % self.id())
        self.assertTrue(outcome["success"], outcome)
        self.assertEqual(1, len(stream.calls), "首答就走了拒答分支")
        self.assertEqual(0, len(client.calls), "拒答判定在最前 → 不许有修复重试")

        details, output_hash = self._details(run_id)
        traces = details.get(RAW_TRACE)
        self.assertIsInstance(traces, list, "模型整段拒答这条路过去一条原文留痕都没有")
        self.assertEqual([1], [item["attempt"] for item in traces])
        self.assertEqual(REFUSAL_PROSE, traces[0]["raw_excerpt"], "逐字等于打桩给的拒答原文")
        self.assertIn("SYNTHESIS_MODEL_REFUSED", traces[0]["error"])
        # 既有行为一字未改 + 留痕不许污染 result / 下游 / output_hash
        self._assert_refusal_behavior_unchanged(run_id, details)
        self.assertNotIn(RAW_TRACE, details["result"])
        self.assertNotIn(RAW_TRACE, self.downstream["synthesis_output"])
        self.assertEqual(_digest(details["result"]), output_hash)

    def test_repair_attempt_refusal_keeps_both_attempts_verbatim(self):
        """首答坏形状 → 修复重试整段拒答：两条原文都要在（第 1 次那份也不许因此丢）。"""
        stream = _StubStream([BAD_STRING])
        client = _StubClient(payload=REFUSAL_REPAIR_PROSE)
        run_id, outcome = self._execute(stream=stream, client=client,
                                       key="raw-trace-refusal-repair-%s" % self.id())
        self.assertTrue(outcome["success"], outcome)
        self.assertEqual(1, len(stream.calls))
        self.assertEqual(1, len(client.calls), "契约没过 → 一次修复重试")

        details, output_hash = self._details(run_id)
        traces = details.get(RAW_TRACE)
        self.assertIsInstance(traces, list, "修复重试拒答也一样不能丢原文")
        self.assertEqual([1, 2], [item["attempt"] for item in traces])
        self.assertEqual(BAD_STRING, traces[0]["raw_excerpt"], "第 1 次尝试的原文必须还在")
        self.assertEqual(REFUSAL_REPAIR_PROSE, traces[1]["raw_excerpt"], "拒答原文逐字留存")
        self.assertIn("SYNTHESIS_MODEL_REFUSED", traces[1]["error"])
        self.assertNotIn("SYNTHESIS_MODEL_REFUSED", traces[0]["error"],
                         "第 1 条记的是契约失败，不是拒答")
        self._assert_refusal_behavior_unchanged(run_id, details)
        self.assertNotIn(RAW_TRACE, details["result"])
        self.assertNotIn(RAW_TRACE, self.downstream["synthesis_output"])
        self.assertEqual(_digest(details["result"]), output_hash)

    def test_long_refusal_prose_is_truncated_to_two_thousand_chars(self):
        """拒答原文同样按既有常量截断（复用 `_RAW_TRACE_CHARS`，不另立口径）。"""
        long_refusal = "抱歉，我无法回答这个问题。" + ("与问题无关的铺垫说明" * 400)
        self.assertGreater(len(long_refusal), qa_synthesis._RAW_TRACE_CHARS)
        stream = _StubStream([long_refusal])
        client = _StubClient(payload=REFUSAL_REPAIR_PROSE)
        run_id, _outcome = self._execute(stream=stream, client=client,
                                        key="raw-trace-refusal-long-%s" % self.id())
        details, _output_hash = self._details(run_id)
        traces = details[RAW_TRACE]
        self.assertEqual(qa_synthesis._RAW_TRACE_CHARS, len(traces[0]["raw_excerpt"]))
        self.assertEqual(long_refusal[:qa_synthesis._RAW_TRACE_CHARS], traces[0]["raw_excerpt"])

    def test_single_good_attempt_still_leaves_no_trace(self):
        """反向守卫（既有设计）：形状正常、一次就过 → 该键不出现。"""
        stream = _StubStream([_raw({"summary": "模型给的小结"})])
        client = _StubClient(payload=_raw({"summary": "模型给的小结"}))
        run_id, outcome = self._execute(stream=stream, client=client,
                                       key="raw-trace-refusal-good-%s" % self.id())
        self.assertTrue(outcome["success"], outcome)
        self.assertEqual(1, len(stream.calls))
        self.assertEqual(0, len(client.calls), "一次就过 → 修复重试一次都不该发生")
        details, output_hash = self._details(run_id)
        self.assertNotIn(RAW_TRACE, details, "形状正常不留痕：这是设计，不是漏")
        self.assertEqual({"result", "performance"}, set(details))
        self.assertEqual(_digest(details["result"]), output_hash)

    def test_empty_raw_from_model_is_never_faked(self):
        """边界（真链路）：模型两次都回空串 → 留痕的 `raw_excerpt` 只能是空串，绝不伪造。"""
        stream = _StubStream([""])
        client = _StubClient(payload="")
        run_id, outcome = self._execute(stream=stream, client=client,
                                       key="raw-trace-empty-%s" % self.id())
        self.assertTrue(outcome["success"], outcome)
        details, _output_hash = self._details(run_id)
        traces = details.get(RAW_TRACE)
        self.assertIsInstance(traces, list)
        self.assertEqual([1, 2], [item["attempt"] for item in traces])
        for item in traces:
            self.assertEqual("", item["raw_excerpt"], "没有原文就空着")
            self.assertTrue(item["error"], "至少要写清为什么没有原文")
        self.assertNotIn("SYNTHESIS_MODEL_REFUSED",
                         details["result"]["sections"]["diagnostics"]["degradation_codes"],
                         "空串不是拒答：判定口径不许被这次改动动到")


class RefusalPathDirectCallTests(unittest.TestCase):
    """直调 `generate()`：返回值永远干净；空原文边界只在这条线上可达。"""

    def _generate(self, payload, sink, **kwargs):
        return qa_synthesis.QaFinalSynthesizer(model_client=_StubClient(payload=payload)).generate(
            question=QUESTION,
            graph={"claims": [_claim()], "evidence": [_evidence("article:1")], "conflicts": []},
            level1={"gaps": []}, level2={"evidence_gaps": []}, degradation=[],
            profile=_StubProfile(), models={"synthesis": "stub"}, context_pack=None,
            raw_trace_sink=sink, **kwargs)

    def test_refusal_return_value_stays_schema_clean(self):
        sink: dict = {}
        answer = self._generate(REFUSAL_PROSE, sink)
        self.assertNotIn(RAW_TRACE, answer, "返回值多带一个键就会撞上 additionalProperties=False")
        validate_final_answer(answer)  # 不抛即通过
        self.assertEqual(REFUSAL_PROSE, sink[RAW_TRACE][0]["raw_excerpt"])
        self.assertIn("SYNTHESIS_MODEL_REFUSED",
                      answer["sections"]["diagnostics"]["degradation_codes"])

    def test_empty_raw_boundary_writes_empty_excerpt_and_says_so(self):
        """空原文边界：真实谓词对空串恒 False（这条线不可达），所以把谓词钉成真来打新加的那行。

        要求：不抛异常、`raw_excerpt` 为空串、`error` 里说明"原文为空" —— 绝不拿别的东西冒充原文。
        """
        self.assertFalse(qa_synthesis._is_model_refusal(""),
                         "真实谓词对空串恒 False：这就是为什么这条线只能靠打桩触达")
        sink: dict = {}
        with mock.patch.object(qa_synthesis, "_is_model_refusal", lambda raw: True):
            answer = self._generate("", sink)
        self.assertNotIn(RAW_TRACE, answer)
        traces = sink[RAW_TRACE]
        self.assertEqual([1], [item["attempt"] for item in traces])
        self.assertEqual("", traces[0]["raw_excerpt"], "原文为空就写空串")
        self.assertIn("原文为空", traces[0]["error"], "必须说清为什么没有原文")


if __name__ == "__main__":
    unittest.main()

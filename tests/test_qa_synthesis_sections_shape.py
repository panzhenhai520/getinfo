#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：模型把 `sections` 返回成数组/字符串/null 时，不许再退化成模板答案。

背景（B 机 run db1fc148152e47178ce348f256dcad0b，134.9 秒，模型三次调用全 `finish=stop`、
零错误，却仍是模板答案）：
    `degradation_reasons` 原文 =
      "最终综合模型两次尝试都未通过契约校验，已改用证据约束的结构化重组。
       （ValueError: dictionary update sequence element #0 has length 3; 2 is required）"

真因（本文件钉住）：`QaFinalSynthesizer._lock_to_graph` 里
    sections = dict(parsed.get("sections") or {})
模型把 `sections` 返成**数组**时，只要首元素是 3 元组 / 3 键对象，`dict()` 就抛
`ValueError: dictionary update sequence element #0 has length 3; 2 is required`。
这条 `ValueError` 被上游 `except (QaContractError, KeyError, TypeError, ValueError)`
当成"契约校验未通过"，文案里却看不出**是哪个字段形状错了** —— 真因被掩盖成
"模型没答好"，而原始模型输出又没落库，只能靠猜。

本文件钉住三件事：
  1. **坏形状**（数组 / 字符串 / null）→ 抛的是**明确的** `QaContractError`（不是 ValueError），
     文案带实际形状 + 原文片段；两次都不过仍然走既定的"证据约束结构化重组"（语义不变）；
  2. **留痕**：两次尝试的原始模型输出挂 `model_raw_trace`，由 orchestrator 摘进
     `qa_stage_runs.details_json`（不新增列、不进 `details["result"]`、不改冻结 schema），
     且答案的 `degradation_reasons` 里带得上原文片段；
  3. **对照**：`sections` 形状正常时，行为逐字不变（只调一次模型、不降级、不带留痕键）。

全程隔离 sqlite + 打桩模型客户端，零模型 / 零嵌入 / 零网络端点。
"""
import json
import os
import sys
import tempfile
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_pipeline  # noqa: E402
import qa_synthesis  # noqa: E402
from qa_contracts import QaContractError  # noqa: E402
from qa_orchestrator import QaOrchestrator  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

QUESTION = "香港家族办公室税收优惠政策对内地高净值客户有什么影响？"
SCOPE = {"owner_user_id": "shaper", "session_id": "s1", "industry_pack_id": "auto"}

# 数组形状：首元素是 3 元组拿到的异常，与 B 机 run 里那条**逐字相同**
BAD_ARRAY = [["summary", "正文", "[1]"]]
# 同样能抛出"length 3"的另一种现实形状：首元素是 3 键对象
BAD_ARRAY_OBJECTS = [{"title": "核心结论", "content": "正文", "citations": ["[1]"]}]
BAD_STRING = "summary 就是这一段"
BAD_NULL = None

ANSWER_TEXT = "香港家族办公室税收优惠政策对符合条件的管理人给予利得税宽免 [1]。"


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


def _parsed(sections):
    """模型返回的 JSON 对象：只有 sections 的形状是自变量。"""
    return {
        "contract_version": "unified-qa-v1", "status": "ready",
        "answer": ANSWER_TEXT, "sections": sections,
        "claims": ["c1"], "conflicts": [], "citations": ["article:1"],
    }


def _lock(parsed, *, question=QUESTION):
    """直接跑 `_lock_to_graph`（模型返回字段的解析代码本体）。"""
    return qa_synthesis.QaFinalSynthesizer._lock_to_graph(
        parsed, {"claims": [_claim()], "evidence": [_evidence("article:1")], "conflicts": []},
        {"gaps": []}, {"evidence_gaps": []}, [], {"research": "RAG检索"},
        question=question, context_pack=None,
    )


# ── 1. 解析层：坏形状 → 明确的契约错误（不再是裸 ValueError）─────────────────────


class SectionsShapeGuardTests(unittest.TestCase):
    """坏形状必须被判成"契约不满足"，且报错文案自带形状与原文片段。"""

    def _assert_contract_error(self, sections, expected_shape):
        with self.assertRaises(QaContractError) as ctx:
            _lock(_parsed(sections))
        message = str(ctx.exception)
        self.assertIn("sections", message, message)
        self.assertIn(expected_shape, message, message)
        # 原文片段必须在：否则又变成"只能靠猜模型返回了什么形状"
        self.assertIn(qa_synthesis._value_excerpt(sections), message, message)
        return message

    def test_array_of_triples_is_contract_error_not_value_error(self):
        """首要嫌疑用例：正是它抛出过 'element #0 has length 3; 2 is required'。"""
        message = self._assert_contract_error(BAD_ARRAY, "数组")
        self.assertIn('["summary", "正文", "[1]"]', message)

    def test_array_of_three_key_objects_is_contract_error(self):
        """另一种现实形状（3 键对象）会抛出**逐字相同**的 length 3 异常。"""
        self._assert_contract_error(BAD_ARRAY_OBJECTS, "数组")

    def test_plain_string_is_contract_error(self):
        self._assert_contract_error(BAD_STRING, "字符串")

    def test_null_is_contract_error(self):
        self._assert_contract_error(BAD_NULL, "null")

    def test_missing_key_is_contract_error(self):
        """连字段都没给：同样是契约不满足，不许静默当空对象收下。"""
        parsed = _parsed({})
        parsed.pop("sections")
        with self.assertRaises(QaContractError) as ctx:
            _lock(parsed)
        self.assertIn("null", str(ctx.exception))

    def test_two_element_array_is_also_rejected(self):
        """旧实现里这种形状**不报错**，但会把数组静默错当字典用（shape confusion）。"""
        with self.assertRaises(QaContractError):
            _lock(_parsed([["summary", "正文"]]))

    def test_readable_sections_still_work_verbatim(self):
        """对照：对象形状照旧直接采用（含模型自有键），只叠加程序自己的三个键。"""
        result = _lock(_parsed({"summary": "模型给的小结", "timeline": "2026 年"}))
        self.assertEqual("模型给的小结", result["sections"]["summary"])
        self.assertEqual("2026 年", result["sections"]["timeline"])
        self.assertIn("conflicts_and_uncertainty", result["sections"])
        self.assertIn("risk_deep_dive", result["sections"])
        self.assertIn("diagnostics", result["sections"])
        self.assertNotIn(qa_synthesis.RAW_TRACE_KEY, result)


# ── 2. 生成层：两次不过 → 既定降级路径 + 降级原因带原文片段 + 原始输出留痕 ─────────


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
    """固定返回一份原文的模型客户端桩：记录调用次数与 timeout，零网络。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, profile, messages, timeout=90):
        self.calls.append({"timeout": timeout})
        return self.payload


class _GenerationBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "shape.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.assertEqual(self.db.backend, "sqlite", "测试必须跑在隔离 sqlite 上")
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self.run = self.store.create_run(
            {"industry_pack_id": "auto", "question": QUESTION, "mode": "standard"},
            owner_user_id="shaper", idempotency_key="shape-%s" % self.id())
        self.run_id = str(self.run["id"])

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def _context(self):
        outputs = {
            "plan": {"question": QUESTION, "standalone_question": QUESTION,
                     "queries": ["香港家族办公室 税收优惠"], "entities": ["家族办公室"],
                     "decomposition": {"is_multi_hop": False, "pattern": "single", "hop_count": 1,
                                       "hops": [{"id": "h1", "question": QUESTION, "depends_on": []}]}},
            "level1_draft": {
                "contract_version": "unified-qa-v1", "draft_answer": "草稿",
                "claims": [_claim()], "gaps": [], "evidence": [_evidence("article:1")]},
            "level1_retrieval": {"evidence": [_evidence("article:1")], "stats": {"adopted": 1}},
        }
        return {"request": {"question": QUESTION, "industry_pack_id": "auto", "mode": "standard"},
                "run": {"id": self.run_id, **SCOPE}, "outputs": outputs}

    def _synthesis(self, client):
        return qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=_FakeRetriever(), web_search=_FakeWebSearch(),
            store=self.store, provider_registry=_StubRegistry(), feature_flags=_StubFlags(),
            final_synthesizer=qa_synthesis.QaFinalSynthesizer(model_client=client),
        )["synthesis"]

    def _run(self, sections):
        """打桩模型两次都返回同一份坏形状原文，跑真正的 synthesis 阶段处理器。"""
        raw = json.dumps(_parsed(sections), ensure_ascii=False)
        client = _StubClient(payload=raw)
        context = self._context()
        self.assertNotIn("_emit_stage_event", context, "本用例只走非流式 model_client 路径")
        answer = self._synthesis(client)(context)
        return raw, client, answer


class BadShapeFallsBackWithTraceTests(_GenerationBase):
    """数组 / 字符串 / null 三种坏形状：不抛 ValueError、走降级、留下原文。"""

    def _assert_degrades_with_excerpt(self, sections, expected_shape):
        raw, client, answer = self._run(sections)

        # 1) 首答 + 一次修复重试，两次都没过契约 → 走既定的证据约束结构化重组
        self.assertEqual(len(client.calls), 2, "首答 + 一次修复重试")
        self.assertTrue(answer["degraded"], "契约不合格必须仍然降级")
        self.assertIn("SYNTHESIS_FALLBACK_USED",
                      answer["sections"]["diagnostics"]["degradation_codes"])

        # 2) 降级原因带上了**原始输出的片段**（不再只能靠猜）
        reasons = " ".join(answer["degradation_reasons"])
        self.assertIn(qa_synthesis._FALLBACK_FAILED_NOTE, reasons)
        self.assertIn("QaContractError", reasons, "异常类型必须点名（不再是被掩盖的裸 ValueError）")
        self.assertIn(expected_shape, reasons, reasons)
        self.assertIn("sections", reasons, "必须点名出问题的字段：" + reasons)
        self.assertIn(qa_synthesis._value_excerpt(sections), reasons,
                      "降级原因必须带上模型返回的原文片段：" + reasons)
        self.assertNotIn("ValueError", reasons, "不许再以裸 ValueError 的名义降级")

        # 3) 两次尝试的原始模型输出都留了痕（截断到 2000 字）
        traces = answer[qa_synthesis.RAW_TRACE_KEY]
        self.assertEqual([1, 2], [item["attempt"] for item in traces])
        for item in traces:
            self.assertEqual(raw[:qa_synthesis._RAW_TRACE_CHARS], item["raw_excerpt"])
            self.assertEqual(raw, item["raw_excerpt"], "本例原文很短，应逐字保留")
            self.assertIn("QaContractError", item["error"])
        return answer

    def test_array_sections_degrades_with_raw_excerpt(self):
        self._assert_degrades_with_excerpt(BAD_ARRAY, "数组")

    def test_array_of_three_key_objects_degrades_with_raw_excerpt(self):
        self._assert_degrades_with_excerpt(BAD_ARRAY_OBJECTS, "数组")

    def test_string_sections_degrades_with_raw_excerpt(self):
        self._assert_degrades_with_excerpt(BAD_STRING, "字符串")

    def test_null_sections_degrades_with_raw_excerpt(self):
        self._assert_degrades_with_excerpt(BAD_NULL, "null")

    def test_long_raw_output_is_truncated_to_two_thousand_chars(self):
        """留痕要截断：不能让一份超长原文把 details_json 撑爆。"""
        parsed = _parsed(BAD_ARRAY)
        parsed["answer"] = ANSWER_TEXT + ("补充说明" * 800)
        raw = json.dumps(parsed, ensure_ascii=False)
        self.assertGreater(len(raw), qa_synthesis._RAW_TRACE_CHARS)
        client = _StubClient(payload=raw)
        context = self._context()
        answer = self._synthesis(client)(context)
        traces = answer[qa_synthesis.RAW_TRACE_KEY]
        self.assertEqual([1, 2], [item["attempt"] for item in traces])
        for item in traces:
            self.assertEqual(qa_synthesis._RAW_TRACE_CHARS, len(item["raw_excerpt"]))
            self.assertEqual(raw[:qa_synthesis._RAW_TRACE_CHARS], item["raw_excerpt"])
        self.assertLess(len(raw), 20000, "原文本身仍然很长，截断确实生效")

    def test_answer_payload_itself_keeps_the_frozen_schema(self):
        """留痕键不是答案载荷的一部分：`validate_final_answer` 仍然逐字通过。"""
        from qa_contracts import validate_final_answer

        _raw, _client, answer = self._run(BAD_NULL)
        payload = {key: value for key, value in answer.items()
                   if key != qa_synthesis.RAW_TRACE_KEY}
        validated = validate_final_answer(payload)
        self.assertEqual("partial", validated["status"])
        self.assertTrue(validated["answer"])


class GeneratePayloadStaysSchemaCleanTests(_GenerationBase):
    """`generate()` 的返回值必须永远"能直接过冻结契约"——留痕只走 `raw_trace_sink`。

    既有用例 `test_qa_phase08_grounding.py::test_two_failed_attempts_are_recorded_not_erased`
    正是钉这一条：它直接调 `generate()` 然后 `validate_final_answer(result)`。
    所以原文留痕**不许**挂在返回值上。
    """

    def _generate(self, sink=None, payload=None):
        client = _StubClient(payload=payload or json.dumps(_parsed(BAD_ARRAY), ensure_ascii=False))
        answer = qa_synthesis.QaFinalSynthesizer(model_client=client).generate(
            question=QUESTION,
            graph={"claims": [_claim()], "evidence": [_evidence("article:1")], "conflicts": []},
            level1={"gaps": []}, level2={"evidence_gaps": []}, degradation=[],
            profile=_StubProfile(), models={"synthesis": "stub"}, context_pack=None,
            raw_trace_sink=sink)
        return client, answer

    def test_return_value_is_always_schema_clean(self):
        from qa_contracts import validate_final_answer

        _client, answer = self._generate()
        self.assertNotIn(qa_synthesis.RAW_TRACE_KEY, answer,
                         "返回值多带一个键就会撞上 additionalProperties=False")
        validated = validate_final_answer(answer)  # 不抛即通过
        self.assertTrue(validated["degraded"])

    def test_sink_receives_both_attempts_without_polluting_the_payload(self):
        sink: dict = {}
        client, answer = self._generate(sink=sink)
        self.assertEqual(2, len(client.calls))
        traces = sink[qa_synthesis.RAW_TRACE_KEY]
        self.assertEqual([1, 2], [item["attempt"] for item in traces])
        self.assertIn("QaContractError", traces[0]["error"])
        self.assertIn("sections", traces[0]["raw_excerpt"])
        self.assertNotIn(qa_synthesis.RAW_TRACE_KEY, answer, "留痕不许回流到载荷")

    def test_no_sink_means_no_trace_side_effect(self):
        """默认不传 sink：行为逐字不变，没有任何留痕副作用。"""
        client, _answer = self._generate(sink=None)
        self.assertEqual(2, len(client.calls))

    def test_success_path_never_fills_the_sink(self):
        sink: dict = {}
        good = json.dumps(_parsed({"summary": "模型给的小结"}), ensure_ascii=False)
        _client, answer = self._generate(sink=sink, payload=good)
        self.assertEqual({}, sink, "形状正常时不留痕（成功路径逐字不变）")
        self.assertNotIn(qa_synthesis.RAW_TRACE_KEY, answer)


class ReadableSectionsBehaviourUnchangedTests(_GenerationBase):
    """对照：sections 形状正常 → 行为逐字不变（只调一次、不降级、不带留痕键）。

    下面的期望值是**改动前跑出来的原文快照**：形状正常这条路上，本改动一个字都不该改。
    快照里那句 `已完成RAG检索和证据图综合。…` 是既有 `_build_structured_answer` 的产物
    （模型那句正文因为只有一行带引用、被既有 `_answer_lacks_line_citations` 判成
    "政策类问句缺行内引用"而顶掉）—— 这是**改动前就有**的行为，与本改动无关，照抄不改。
    """

    def _good_raw(self):
        return json.dumps(_parsed({"summary": "模型给的小结"}), ensure_ascii=False)

    def _projection(self, answer):
        return {
            "status": answer["status"],
            "degraded": answer["degraded"],
            "degradation_reasons": answer["degradation_reasons"],
            "degradation_codes": answer["sections"]["diagnostics"]["degradation_codes"],
            "answer": answer["answer"],
            "citations": answer["citations"],
            "claim_ids": [item["claim_id"] for item in answer["claims"]],
            "models": answer["models"],
            "sections": sorted(answer["sections"]),
            "summary": answer["sections"]["summary"],
            "conflicts": answer["conflicts"],
            "cutoff_at": answer["cutoff_at"],
        }

    def test_readable_shape_is_verbatim_unchanged(self):
        client = _StubClient(payload=self._good_raw())
        context = self._context()
        answer = self._synthesis(client)(context)

        # 一次就过：不触发修复重试
        self.assertEqual(len(client.calls), 1, "形状正常时不许重试")
        # 成功路径**一个留痕键都不加**（这正是"逐字不变"的关键）
        self.assertNotIn(qa_synthesis.RAW_TRACE_KEY, answer)
        # 载荷逐字等于改动前的快照
        self.assertEqual({
            "status": "ready",
            "degraded": False,
            "degradation_reasons": [],
            "degradation_codes": [],
            "answer": (
                "已完成RAG检索和证据图综合。\n\n核心结论：\n"
                "- 香港家族办公室税收优惠政策对内地高净值客户有影响 [1]\n\n"
                "风险拆解与应对：\n"
                "- 家族办公室服务流程：可能要求家族办公室重做架构体检、客户风险分层、"
                "跨专业协同和持续监控流程。 应对：将政策监控、客户架构盘点、税务测算、"
                "律师/税务师复核和客户沟通模板纳入标准服务流程。 [1]"
            ),
            "citations": ["article:1"],
            "claim_ids": ["c1"],
            "models": {"draft": "local", "research": "RAG检索", "synthesis": "local"},
            "sections": ["conflicts_and_uncertainty", "diagnostics", "risk_deep_dive", "summary"],
            "summary": "模型给的小结",
            "conflicts": [],
            "cutoff_at": "2026-10-09",
        }, self._projection(answer))

    def test_success_and_fallback_payloads_are_distinguishable(self):
        """成功路径不带留痕键、失败路径带 —— 两条路的载荷形状必须能区分。"""
        ok = self._synthesis(_StubClient(payload=self._good_raw()))(self._context())
        self.assertNotIn(qa_synthesis.RAW_TRACE_KEY, ok)
        bad = self._run(BAD_ARRAY)[2]
        self.assertIn(qa_synthesis.RAW_TRACE_KEY, bad)


# ── 3. 落库层：`model_raw_trace` 摘进 details_json，不进 result / 下游输出 ─────────


class RawTraceLandsInDetailsJsonTests(unittest.TestCase):
    """`qa_stage_runs.details_json` 是**既有载体**：零迁移、不新增列、不碰冻结 schema。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "details.sqlite3"))
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

    def _run(self, synthesis_payload, *, key):
        """跑真 orchestrator：证明留痕键真的被摘走并落进 details_json。"""
        def _citation_validation(context):
            # synthesis 输出会原样进入严格 schema 校验：留痕键必须已经被摘走
            self.captured["synthesis_output"] = dict(context["outputs"]["synthesis"])
            return {"status": "ready"}

        handlers = {
            "plan": lambda context: {"question_plan": {}},
            "level1_retrieval": lambda context: {"evidence": []},
            "logic_validation": lambda context: {"status": "passed"},
            "level1_draft": lambda context: {"draft_answer": "草稿"},
            "synthesis": lambda context: dict(synthesis_payload),
            "citation_validation": _citation_validation,
        }
        run = self.store.create_run(
            {"question": "测试问题", "industry_pack_id": "family_office",
             "origin": "api", "mode": "fast"},
            owner_user_id="user:1", idempotency_key=key)
        outcome = QaOrchestrator(self.store, handlers).execute(run["id"])
        self.assertTrue(outcome["success"], outcome)
        with self.db.lock:
            row = self.db.connection.execute(
                "SELECT details_json, output_hash FROM qa_stage_runs"
                " WHERE run_id=? AND stage='synthesis'", (run["id"],)).fetchone()
        return run["id"], json.loads(row[0] or "{}"), str(row[1])

    def _payload(self):
        return {
            "contract_version": "unified-qa-v1", "status": "partial", "answer": "兜底答案 [1]。",
            "sections": {"summary": "兜底"}, "claims": [], "conflicts": [], "evidence": [],
            "citations": [], "citation_map": {}, "cutoff_at": "", "degraded": True,
            "degradation_reasons": ["最终综合模型两次尝试都未通过契约校验。"], "models": {},
            qa_synthesis.RAW_TRACE_KEY: [
                {"attempt": 1, "raw_excerpt": '{"sections": [["a", "b", "c"]]}',
                 "error": "QaContractError: 最终综合的 sections 必须是对象"},
                {"attempt": 2, "raw_excerpt": '{"sections": [["a", "b", "c"]]}',
                 "error": "QaContractError: 最终综合的 sections 必须是对象"},
            ],
        }

    def test_trace_is_persisted_into_details_json(self):
        run_id, details, _hash = self._run(self._payload(), key="raw-trace-a-%s" % self.id())
        traces = details.get(qa_synthesis.RAW_TRACE_KEY)
        self.assertIsInstance(traces, list)
        self.assertEqual([1, 2], [item["attempt"] for item in traces])
        self.assertIn('"sections"', traces[0]["raw_excerpt"])

    def test_trace_is_stripped_from_result_and_downstream_output(self):
        run_id, details, _hash = self._run(self._payload(), key="raw-trace-b-%s" % self.id())
        self.assertNotIn(qa_synthesis.RAW_TRACE_KEY, details["result"],
                         "result 要当'已完成阶段输出'回放，不许夹带留痕键")
        self.assertNotIn(qa_synthesis.RAW_TRACE_KEY, self.captured["synthesis_output"],
                         "留痕键流向 citation_validation 会撞上 additionalProperties=False")
        # result 逐字等于"没有留痕键"的那份载荷
        expected = {key: value for key, value in self._payload().items()
                    if key != qa_synthesis.RAW_TRACE_KEY}
        self.assertEqual(expected, details["result"])
        # 留痕是 details 的**同级键**，与 token_usage 同一位置
        self.assertEqual({"performance", "result", qa_synthesis.RAW_TRACE_KEY},
                         set(details))

    def test_output_hash_ignores_the_trace(self):
        """留痕不许改变 output_hash（否则回放/幂等口径会被留痕带偏）。"""
        _run_a, details_a, hash_a = self._run(self._payload(), key="raw-trace-c-%s" % self.id())
        self.assertIn(qa_synthesis.RAW_TRACE_KEY, details_a)
        payload = self._payload()
        payload.pop(qa_synthesis.RAW_TRACE_KEY)
        _run_b, details_b, hash_b = self._run(payload, key="raw-trace-d-%s" % self.id())
        self.assertNotIn(qa_synthesis.RAW_TRACE_KEY, details_b)
        self.assertEqual(details_a["result"], details_b["result"], "result 逐字相同")
        self.assertEqual(hash_a, hash_b, "留痕键必须在 output_hash 之前摘掉")

    def test_payload_without_trace_keeps_details_verbatim(self):
        payload = self._payload()
        payload.pop(qa_synthesis.RAW_TRACE_KEY)
        _run, details, _hash = self._run(payload, key="raw-trace-e-%s" % self.id())
        self.assertNotIn(qa_synthesis.RAW_TRACE_KEY, details,
                         "没留痕就不加这个键（成功路径逐字不变）")
        self.assertEqual({"performance", "result"}, set(details))


if __name__ == "__main__":
    unittest.main()

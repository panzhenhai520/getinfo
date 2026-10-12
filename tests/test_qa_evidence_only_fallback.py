#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务 ③ 守门测试：**只在"模型真的失败/超时之后"**才降级成证据摘录。

用户口径（五条，全部钉在这里）：
  1. 默认仍然用模型（好答案优先）；
  2. 只有模型**真的失败或超时之后**才降级，**不许预防性跳过**；
  3. 必须**如实标注**"这条答案是证据摘录、未经模型综合"，让人一眼看得见；
  4. 判定放在**一处**（`qa_pipeline._evidence_only_fallback`），两个阶段口径一致；
  5. 两个阶段（level1_draft / synthesis）都要记账。

本文件钉住：
  · ① synthesis 真超时 → `EVIDENCE_ONLY_NO_MODEL` + 四处标注齐全 + run 级记账齐全 +
       模型**确实被调用过**（流式端点被请求过 1 次，不是预防性跳过）；
  · ② 熔断 → 仍然是 `PROVIDER_CIRCUIT_OPEN`，**不是**新码（优先级钉死，模型 0 次调用）；
  · ③ 契约两次都不过 → 仍是既有兜底码（`SYNTHESIS_FALLBACK_USED`），不是新码；
  · ④ 反向守卫：模型正常返回 → **绝不出现**新码、模型只被调用一次、无标注、无降级事件；
  · ⑤ level1 与 synthesis 同 helper、同码语义（两边真超时都出同一个新码）；
  · ⑥ 落库口径：`store.update_run(final_answer=...)` 之后固定文案**仍在** reasons 里
       （证明它没被 `_public_degradation_reasons` 的过滤正则吃掉，UI 看得见）。

全程隔离 sqlite + 打桩模型客户端/端点，零模型 / 零嵌入 / 零真网络。
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

import qa_level1  # noqa: E402
import qa_pipeline  # noqa: E402
import qa_synthesis  # noqa: E402
import requests  # noqa: E402
from qa_resilience import QaPersistentResilience  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

QUESTION = "香港家族办公室税收优惠政策对内地高净值客户有什么影响？"
SCOPE = {"owner_user_id": "evonly", "session_id": "s1", "industry_pack_id": "auto"}
NOTE = qa_pipeline.EVIDENCE_ONLY_NOTE
NEW_CODE = qa_pipeline.EVIDENCE_ONLY_NO_MODEL


def _evidence(ref="article:1", *, authority=60):
    return {
        "evidence_ref": ref, "source_type": "article",
        "title": "香港家族办公室税收优惠政策 %s" % ref,
        "source_url": "https://example.com/%s" % ref,
        "article_id": int(str(ref).split(":")[-1]),
        "content_excerpt": "香港家族办公室税收优惠政策对符合条件的管理人给予利得税宽免。",
        "published_at": "2026-10-09", "authority_level": authority, "score": 30.0,
        "retrieval_method": "keyword", "match_reason": "标题命中：家族办公室",
        "relationship": "supports", "metadata": {"matched_keywords": ["家族办公室"]},
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


GOOD_RAW = json.dumps({
    "contract_version": "unified-qa-v1", "status": "ready",
    "answer": "香港家族办公室税收优惠政策对符合条件的管理人给予利得税宽免 [1]。",
    "sections": {"summary": "模型给的小结"},
    "claims": ["c1"], "conflicts": [], "citations": ["article:1"],
}, ensure_ascii=False)

BAD_RAW = "这既不是 JSON 也没有大括号"


class _FakeRetriever:
    def retrieve(self, plan, **kwargs):
        return {"evidence": [_evidence()], "stats": {"eligible": 1, "adopted": 1}}


class _FakeWebSearch:
    def search(self, queries, *, enabled=True, limit=8):
        return {"evidence": [], "status": "disabled", "providers": [], "errors": []}


class _StubProfile:
    """本地端点桩：provider_id=local 才走流式（不需要 api_key），绝不联网。"""

    provider_id = "local"
    model_id = "stub"
    base_url = "http://127.0.0.1:9/v1"
    api_key = ""
    use_proxy = False


class _StubRegistry:
    def resolve(self, role, provider_id, **kwargs):
        return _StubProfile()


class _StubFlags:
    def snapshot(self):
        return {"synthesis_enabled": True, "level2_enabled": True, "enabled": True}


class _RecordingClient:
    """记录调用次数的模型客户端桩；`error` 非空时每次调用都抛它。"""

    def __init__(self, payload=GOOD_RAW, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def __call__(self, profile, messages, timeout=90):
        self.calls.append({"timeout": timeout})
        if self.error is not None:
            raise self.error
        return self.payload

    @property
    def last_usage(self):
        return {}


class _FakeStreamResponse:
    """流式响应桩：把一段完整原文当成一个 SSE delta 下发（零网络）。"""

    status_code = 200
    text = ""

    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None

    def close(self):
        return None

    def iter_lines(self):
        body = ('data: {"choices":[{"delta":{"content":%s}}]}'
                % json.dumps(self.content, ensure_ascii=False)).encode("utf-8")
        yield body
        yield b"data: [DONE]"


def _stream_post(content):
    """把 `requests.post` 换成"永远回这一段流式内容"的探针，并数调用次数。"""
    return mock.Mock(side_effect=lambda url, **kwargs: _FakeStreamResponse(content))


class _Base(unittest.TestCase):
    def setUp(self):
        try:
            from qa_observability import provider_allowed_hosts

            provider_allowed_hosts(force=True)
        except Exception:
            pass
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "evonly.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.assertEqual(self.db.backend, "sqlite", "测试必须跑在隔离 sqlite 上")
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self.run = self.store.create_run(
            {"industry_pack_id": "auto", "question": QUESTION, "mode": "standard"},
            owner_user_id="evonly", idempotency_key="evonly-%s" % self.id())
        self.run_id = str(self.run["id"])
        self.resilience = QaPersistentResilience(self.db)
        self.events = []

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    # ── 装配 ────────────────────────────────────────────────────────────────
    def _handlers(self, *, synthesis=None, level1=None):
        return qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=_FakeRetriever(), web_search=_FakeWebSearch(),
            store=self.store, provider_registry=_StubRegistry(), feature_flags=_StubFlags(),
            level1_generator=qa_level1.QaLevel1Generator(model_client=level1 or _RecordingClient()),
            final_synthesizer=qa_synthesis.QaFinalSynthesizer(model_client=synthesis or _RecordingClient()),
            resilience=self.resilience,
        )

    def _context(self, *, with_events=True):
        outputs = {
            "plan": {"question": QUESTION, "standalone_question": QUESTION,
                     "queries": ["香港家族办公室 税收优惠"], "entities": ["家族办公室"],
                     "decomposition": {"is_multi_hop": False, "pattern": "single", "hop_count": 1,
                                       "hops": [{"id": "h1", "question": QUESTION, "depends_on": []}]}},
            "level1_draft": {
                "contract_version": "unified-qa-v1", "draft_answer": "草稿",
                "claims": [_claim()], "gaps": [], "evidence": [_evidence()]},
            "level1_retrieval": {"evidence": [_evidence()], "stats": {"adopted": 1}},
        }
        context = {"request": {"question": QUESTION, "industry_pack_id": "auto", "mode": "standard"},
                   "run": {"id": self.run_id, **SCOPE}, "outputs": outputs}
        if with_events:
            context["_emit_stage_event"] = (
                lambda event_type, payload=None: self.events.append((event_type, dict(payload or {}))))
        return context

    def _stage_degradation(self, stage):
        run = self.store.get_run(self.run_id) or {}
        return [item for item in (run.get("degradation") or [])
                if isinstance(item, dict) and item.get("stage") == stage]

    def _degraded_events(self, stage):
        return [payload for event_type, payload in self.events
                if event_type == "degraded" and str(payload.get("stage") or "") == stage]

    @staticmethod
    def _codes(answer):
        return list(((answer.get("sections") or {}).get("diagnostics") or {}).get("degradation_codes") or [])


class SynthesisTimeoutTests(_Base):
    """① 模型真超时 → 新码 + 四处标注 + run 级记账 + 模型确实被调用过。"""

    def test_timeout_degrades_with_four_place_label(self):
        # 真超时：流式端点被请求时抛 requests.Timeout（与生产上"读超时"同一条路径）
        post = mock.Mock(side_effect=requests.exceptions.Timeout("read timeout"))
        with mock.patch.object(qa_synthesis.requests, "post", post):
            answer = self._handlers()["synthesis"](self._context())

        # 0) **模型确实被调用过**（这一步不是为了"提前跳过模型"）
        self.assertEqual(1, post.call_count, "必须真的发起过模型请求（不许预防性跳过）")

        # 1) 新码 + 既有码互斥
        codes = self._codes(answer)
        self.assertIn(NEW_CODE, codes)
        self.assertNotIn("SYNTHESIS_FAST_EVIDENCE_MODE", codes,
                         "旧码是'预防性跳过'，语义不同，不许复用")
        self.assertNotIn("SYNTHESIS_FALLBACK_USED", codes, "超时不许被标成契约兜底码")
        self.assertNotIn("PROVIDER_CIRCUIT_OPEN", codes, "没熔断就不许报熔断码")
        self.assertEqual("SYNTHESIS_FAST_EVIDENCE_MODE",
                         answer["sections"]["diagnostics"]["supersedes"],
                         "只在 diagnostics 里留一条与旧码的比对线索")

        # 2) 四处标注齐全
        self.assertEqual(NOTE, answer["degradation_reasons"][0],
                         "固定文案必须在 reasons 第 1 位（UI 只渲染前 6 条）")
        self.assertIn(NEW_CODE, self._codes(answer))
        self.assertTrue(answer["answer"].startswith(qa_pipeline.EVIDENCE_ONLY_HEADER),
                        "正文顶部必须有显式 header 块")
        self.assertEqual("partial", answer["status"])
        self.assertTrue(answer["degraded"])

        # 3) 生效超时也记进了阶段输出（任务 ②→③：传入值 = 生效值 = 判定值）
        self.assertEqual(120, answer["sections"]["diagnostics"]["model_timeouts"]["read_seconds"])

        # 4) run 级记账：同一新码 + 触发原因 + 生效超时
        items = self._stage_degradation("synthesis")
        self.assertEqual([NEW_CODE], [item.get("code") for item in items], items)
        self.assertEqual("NETWORK_UNREACHABLE", items[0].get("detail"),
                         "触发原因（真超时）只作 detail，不冒充新码")
        self.assertIn("read_seconds", json.dumps(items[0].get("model_timeouts") or {}))

        # 5) degraded 事件 + context 标记（两阶段同口径的两个落点）
        events = self._degraded_events("synthesis")
        self.assertEqual([NEW_CODE], [item.get("code") for item in events], events)
        self.assertEqual(NEW_CODE, self._stage_degraded_code("synthesis"))

    def _stage_degraded_code(self, stage):
        # 单独跑一遍，把 context 留在手上（上面那次 context 是临时对象）
        post = mock.Mock(side_effect=requests.exceptions.Timeout("read timeout"))
        context = self._context()
        with mock.patch.object(qa_synthesis.requests, "post", post):
            self._handlers()["synthesis"](context)
        return str(((context.get("_stage_degraded") or {}).get(stage) or {}).get("code") or "")


class LabelSurvivesFilterAndStorageTests(_Base):
    """⑥ 固定文案不许被过滤正则吃掉，落库后仍在 —— UI 上真的看得见。"""

    def test_public_reason_filter_would_eat_the_raw_message(self):
        """先证明"风险"是真的：过滤正则会吃掉超时类内部文案（否则本用例没意义）。"""
        filtered = qa_synthesis._public_degradation_reasons([
            {"stage": "synthesis", "code": "NETWORK_UNREACHABLE",
             "message": "综合模型响应超时，已改用证据约束重组。"},
        ])
        self.assertEqual([], filtered, "前提：这类内部文案确实会被有意丢掉")

    def test_stale_note_in_the_ledger_never_leaks_into_a_synthesized_answer(self):
        """反向守卫（防"二次说谎"）：run 级账本里的旧标注**不许**抄进模型综合过的答案。

        场景：上一次尝试（或上一阶段）把"本条回答由已核验证据摘录生成，未经模型综合"记进了
        run 级账本；这一次模型**正常返回**了答案。若账本文案被照搬进 `degradation_reasons`，
        用户就会看到"模型综合过的回答自称未经模型综合"——最恶劣的那种假话。
        """
        stale = {"stage": "level1_draft", "code": NEW_CODE,
                 "message": NOTE + "（NETWORK_UNREACHABLE）", "detail": "NETWORK_UNREACHABLE"}
        # 真链路：synthesis 阶段读的就是 `store.get_run()["degradation"]`（run 级账本）
        self.store.mark_degraded(self.run_id, stale)
        client = _RecordingClient(payload=GOOD_RAW)
        post = _stream_post(GOOD_RAW)

        with mock.patch.object(qa_synthesis.requests, "post", post):
            answer = self._handlers(synthesis=client)["synthesis"](self._context())

        codes = self._codes(answer)
        # 诊断码表照旧镜像 **run 级账本**（既有语义：本 run 出现过哪些降级），所以这里会有新码；
        # 要钉的是"**这句话**不许被抄进这次（模型综合过的）答案"。
        self.assertNotIn(NOTE, answer["degradation_reasons"],
                         "历史账本里的固定文案不许抄进这次（模型综合过的）答案")
        self.assertFalse(answer["degraded"], "答案级不许因为历史账本被标成降级")
        self.assertNotIn(qa_pipeline.EVIDENCE_ONLY_HEADER, str(answer["answer"]),
                         "模型综合过的正文不许插'未经模型综合'的 header")
        self.assertEqual([], self._degraded_events("synthesis"), "本阶段没有降级事件")
        self.assertNotEqual([NEW_CODE], [item.get("code") for item in self._stage_degradation("synthesis")],
                            "本阶段自己不该再记一条降级（账本里那条是预置的历史账）")

    def test_note_is_present_after_update_run(self):
        post = mock.Mock(side_effect=requests.exceptions.Timeout("read timeout"))
        handlers = self._handlers()
        with mock.patch.object(qa_synthesis.requests, "post", post):
            context = self._context()
            answer = handlers["synthesis"](context)
            final = handlers["citation_validation"]({**context, "outputs": {"synthesis": answer}})

        self.store.update_run(self.run_id, status="completed", stage="completed", final_answer=final)
        stored = self.store.get_run(self.run_id)
        reasons = [str(item) for item in stored["final_answer"]["degradation_reasons"]]

        self.assertEqual(NOTE, reasons[0], "落库后固定文案仍在第 1 位（UI 渲染的就是这里）")
        self.assertIn(NOTE, " ".join(reasons))
        self.assertTrue(stored["degraded"], "run 级也必须说降级（两本账不许打脸）")
        self.assertEqual(NEW_CODE, self._codes(stored["final_answer"])[
            self._codes(stored["final_answer"]).index(NEW_CODE)])
        self.assertTrue(str(stored["final_answer"]["answer"]).startswith(qa_pipeline.EVIDENCE_ONLY_HEADER))


class PriorityTests(_Base):
    """② 熔断优先 / ③ 契约两次不过 —— 都不许变成新码。"""

    def test_circuit_open_is_never_eaten_by_evidence_only(self):
        for _ in range(3):
            self.resilience.circuit_failure("provider:local")
        client = _RecordingClient()
        context = self._context()

        answer = self._handlers(synthesis=client)["synthesis"](context)

        self.assertEqual([], client.calls, "熔断打开时根本没调用模型")
        self.assertNotIn(NEW_CODE, self._codes(answer), "熔断绝不允许被吃成'证据摘录'")
        self.assertIn("PROVIDER_CIRCUIT_OPEN", self._codes(answer))
        self.assertEqual(["PROVIDER_CIRCUIT_OPEN"],
                         [item.get("code") for item in self._stage_degradation("synthesis")])
        self.assertNotIn("synthesis", context.get("_stage_degraded") or {},
                         "熔断走独立分支，不进证据摘录的判定")

    def test_two_failed_contract_attempts_keep_existing_code(self):
        client = _RecordingClient(payload=BAD_RAW)
        context = self._context()

        # 流式首答给一份坏原文；修复重试走非流式 model_client（同一份坏原文）→ 两次都不过契约
        post = _stream_post(BAD_RAW)
        with mock.patch.object(qa_synthesis.requests, "post", post):
            answer = self._handlers(synthesis=client)["synthesis"](context)

        self.assertEqual(1, post.call_count, "流式首答 1 次")
        self.assertEqual(1, len(client.calls), "契约没过 → 一次修复重试（非流式）")
        self.assertNotIn(NEW_CODE, self._codes(answer), "契约兜底不许冒充'模型真失败'")
        self.assertIn("SYNTHESIS_FALLBACK_USED", self._codes(answer))
        self.assertEqual(["SYNTHESIS_FALLBACK_USED"],
                         [item.get("code") for item in self._stage_degradation("synthesis")])
        # 如实标注照旧（这条答案**确实**是证据摘录），只是码沿用既有码
        self.assertEqual(NOTE, answer["degradation_reasons"][0])


class ModelRefusalTests(_Base):
    """⑤ 模型明确拒答 = "模型真的失败"（它读了证据但拒绝处理）→ 新码。"""

    def test_refusal_uses_new_code_not_contract_fallback(self):
        post = _stream_post("抱歉，我无法回答这个问题。")
        context = self._context()

        with mock.patch.object(qa_synthesis.requests, "post", post):
            answer = self._handlers()["synthesis"](context)

        self.assertEqual(1, post.call_count, "拒答也是「真的调用过模型」：不许预防性跳过")
        codes = self._codes(answer)
        self.assertIn(NEW_CODE, codes)
        self.assertNotIn("SYNTHESIS_FALLBACK_USED", codes,
                         "拒答这一格与'契约兜底'那格必须互斥")
        self.assertEqual(NOTE, answer["degradation_reasons"][0])
        items = self._stage_degradation("synthesis")
        self.assertEqual([NEW_CODE], [item.get("code") for item in items], items)
        self.assertEqual("SYNTHESIS_MODEL_REFUSED", items[0].get("detail"),
                         "原始拒答码只作 detail")


class ReverseGuardTests(_Base):
    """④ 默认仍然用模型：正常返回 → 不出现新码、只调用一次、无任何标注。"""

    def test_model_success_never_produces_new_code(self):
        client = _RecordingClient(payload=GOOD_RAW)
        context = self._context()

        post = _stream_post(GOOD_RAW)
        with mock.patch.object(qa_synthesis.requests, "post", post):
            answer = self._handlers(synthesis=client)["synthesis"](context)

        self.assertEqual(1, post.call_count, "正常返回只调用一次模型（流式首答）")
        self.assertEqual([], client.calls, "一次就过契约 → 不许有修复重试")
        self.assertNotIn(NEW_CODE, self._codes(answer))
        self.assertFalse(answer["degraded"], "好答案不许被标注降级")
        self.assertEqual([], answer["degradation_reasons"])
        self.assertNotIn(qa_pipeline.EVIDENCE_ONLY_HEADER, str(answer["answer"]))
        self.assertEqual([], self._stage_degradation("synthesis"), "run 级也不许留降级账")
        self.assertEqual([], self._degraded_events("synthesis"))
        self.assertNotIn("synthesis", context.get("_stage_degraded") or {})


class BothStagesSameRuleTests(_Base):
    """⑤ level1 与 synthesis 同 helper、同码语义（真超时都出同一个新码）。"""

    def test_level1_timeout_uses_same_code_and_label(self):
        client = _RecordingClient(error=requests.exceptions.Timeout("read timeout"))
        context = self._context()

        # 流式那一步也拦掉（`_stream_raw` 会把异常吞掉退回非流式）：全程零网络
        post = mock.Mock(side_effect=requests.exceptions.Timeout("read timeout"))
        with mock.patch.object(qa_synthesis.requests, "post", post):
            draft = self._handlers(level1=client)["level1_draft"](context)

        self.assertEqual(1, len(client.calls), "level1 必须真的调用过模型")
        items = self._stage_degradation("level1_draft")
        self.assertEqual([NEW_CODE], [item.get("code") for item in items], items)
        self.assertEqual("NETWORK_UNREACHABLE", items[0].get("detail"))
        self.assertTrue(str(draft["draft_answer"]).startswith(qa_pipeline.EVIDENCE_ONLY_HEADER),
                        "一级'这条答案'就是 draft_answer：标注落在正文顶部")
        self.assertEqual([NEW_CODE], [item.get("code") for item in self._degraded_events("level1_draft")])
        self.assertEqual(NEW_CODE, str((context.get("_stage_degraded") or {})["level1_draft"]["code"]))
        # 生效超时同样带出来（② 的乙案保证它就是真正传给 requests 的那个数）
        self.assertIn("stream_read_seconds", items[0].get("model_timeouts") or {})

    def test_level1_contract_exhaustion_now_gets_accounted(self):
        """顺手补的 ⑧ 空档：`LEVEL1_OUTPUT_INVALID` 原先**故意跳过** `mark_degraded`。"""
        client = _RecordingClient(payload=BAD_RAW)
        context = self._context()

        post = mock.Mock(side_effect=requests.exceptions.ConnectionError("no endpoint"))
        with mock.patch.object(qa_synthesis.requests, "post", post):
            draft = self._handlers(level1=client)["level1_draft"](context)

        self.assertEqual(2, len(client.calls), "首答 + 一次修复重试")
        items = self._stage_degradation("level1_draft")
        self.assertEqual(["LEVEL1_OUTPUT_INVALID"], [item.get("code") for item in items],
                         "④ 契约失败沿用既有码，但**必须记账**（不许再留空档）")
        self.assertTrue(str(draft["draft_answer"]).startswith(qa_pipeline.EVIDENCE_ONLY_HEADER))


if __name__ == "__main__":
    unittest.main()

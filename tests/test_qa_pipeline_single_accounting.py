#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qa_pipeline 的"同一件事两套实现"清理守门（用户拍板：一个完成的功能只留一条路径）。

本文件钉住两件事：
  ① **同一件事两个码只留一个**：运维关掉综合模型（`feature_flags.synthesis_enabled=False`）时，
     只出更具体的 `SYNTHESIS_OPERATIONS_DISABLED`（它说明了原因），**不**再顺手落一条
     `SYNTHESIS_FALLBACK_USED`。治法在**调用点**（不传 `reason`），因为
     `fallback_final_answer` 的"reason 非空就补一条码"是**共用机制**，别的调用点依赖它，
     不许为了这一处整体改掉。
     ⚠ 如实记录（不粉饰）：改前之所以在载荷里**只看得到一个码**，靠的是
     `fallback_final_answer` 按 `message` 文本去重这个**巧合**（传进去的 reason 与
     `item["message"]` 逐字相同）；文案一变两个码立刻并列出现
     —— 见 `test_the_shared_reason_mechanism_is_left_alone`。所以本组用例断的是
     "调用点不再传 reason"这个**结构**事实 + 载荷/账本只有一个码，而不是断"今天恰好没重复"。
  ② **熔断分支的记账合并到单点**：level1_draft / synthesis 两条熔断分支与
     `_evidence_only_fallback` **共用** `qa_pipeline._record_run_degradation` 这一个写点；
     同时**不许**因此多出 `degraded` 事件或 `_stage_degraded` 标记 —— 熔断是独立一格，
     此前（以及现在）都刻意没有这两样，码恒为 `PROVIDER_CIRCUIT_OPEN`。

全程隔离 sqlite + 打桩模型/端点：零模型、零嵌入、零真网络。
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
SCOPE = {"owner_user_id": "single-point", "session_id": "s1", "industry_pack_id": "auto"}
NEW_CODE = qa_pipeline.EVIDENCE_ONLY_NO_MODEL
OPS_ITEM = {
    "stage": "synthesis", "code": "SYNTHESIS_OPERATIONS_DISABLED",
    "message": "最终综合模型已由运维临时关闭，现返回已核验证据摘要。",
}
"""被守门的那一处降级项（与 `qa_pipeline` 里逐字相同）。"""


def _evidence(ref="article:1"):
    return {
        "evidence_ref": ref, "source_type": "article",
        "title": "香港家族办公室税收优惠政策 %s" % ref,
        "source_url": "https://example.com/%s" % ref,
        "article_id": int(str(ref).split(":")[-1]),
        "content_excerpt": "香港家族办公室税收优惠政策对符合条件的管理人给予利得税宽免。",
        "published_at": "2026-10-09", "authority_level": 60, "score": 30.0,
        "retrieval_method": "keyword", "match_reason": "标题命中：家族办公室",
        "relationship": "supports", "metadata": {"matched_keywords": ["家族办公室"]},
    }


def _claim():
    return {
        "claim_id": "c1", "text": "香港家族办公室税收优惠政策对内地高净值客户有影响",
        "claim_type": "current_fact", "confidence": 0.82,
        "valid_from": "2026-10-09", "valid_to": None, "scope": ["家族办公室"],
        "evidence_refs": ["article:1"], "needs_verification": True,
        "verification_status": "unverified",
    }


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


class _Flags:
    def __init__(self, *, synthesis_enabled=True):
        self._synthesis = bool(synthesis_enabled)

    def snapshot(self):
        return {"synthesis_enabled": self._synthesis, "level2_enabled": True, "enabled": True}


class _RecordingClient:
    """记录调用次数的模型客户端桩（本文件里它**一次都不许被调用**）。"""

    def __init__(self):
        self.calls = []

    def __call__(self, profile, messages, timeout=90):
        self.calls.append({"timeout": timeout})
        return ""

    @property
    def last_usage(self):
        return {}


class _Base(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "single-point.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self.run = self.store.create_run(
            {"industry_pack_id": "auto", "question": QUESTION, "mode": "standard"},
            owner_user_id="single-point", idempotency_key="single-%s" % self.id())
        self.run_id = str(self.run["id"])
        self.client = _RecordingClient()
        self.level1_client = _RecordingClient()

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    # ── 装配 ────────────────────────────────────────────────────────────────
    def _handlers(self, *, synthesis_enabled=True):
        return qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=_FakeRetriever(), web_search=_FakeWebSearch(),
            store=self.store, provider_registry=_StubRegistry(),
            feature_flags=_Flags(synthesis_enabled=synthesis_enabled),
            level1_generator=qa_level1.QaLevel1Generator(model_client=self.level1_client),
            final_synthesizer=qa_synthesis.QaFinalSynthesizer(model_client=self.client),
            resilience=QaPersistentResilience(self.db),
        )

    def _outputs(self):
        return {
            "plan": {"question": QUESTION, "standalone_question": QUESTION,
                     "queries": ["香港家族办公室 税收优惠"], "entities": ["家族办公室"],
                     "decomposition": {"is_multi_hop": False, "pattern": "single", "hop_count": 1,
                                       "hops": [{"id": "h1", "question": QUESTION, "depends_on": []}]}},
            "level1_draft": {"contract_version": "unified-qa-v1", "draft_answer": "草稿",
                             "claims": [_claim()], "gaps": [], "evidence": [_evidence()]},
            "level1_retrieval": {"evidence": [_evidence()], "stats": {"adopted": 1}},
        }

    def _context(self, *, with_events=True):
        context = {"request": {"question": QUESTION, "industry_pack_id": "auto",
                               "mode": "standard"},
                   "run": {"id": self.run_id, **SCOPE}, "outputs": self._outputs()}
        if with_events:
            self.events = []
            context["_emit_stage_event"] = (
                lambda event_type, payload=None: self.events.append((event_type, dict(payload or {}))))
        return context

    # ── 账本读取 ────────────────────────────────────────────────────────────
    def _stage_degradation(self, stage):
        run = self.store.get_run(self.run_id) or {}
        return [item for item in (run.get("degradation") or [])
                if isinstance(item, dict) and item.get("stage") == stage]

    def _run_codes(self, stage):
        return [str(item.get("code") or "") for item in self._stage_degradation(stage)]

    def _degraded_events(self, stage):
        return [payload for event_type, payload in self.events
                if event_type == "degraded" and str(payload.get("stage") or "") == stage]

    @staticmethod
    def _codes(answer):
        return list(((answer.get("sections") or {}).get("diagnostics") or {})
                    .get("degradation_codes") or [])

    def _open_circuit(self):
        """把 provider:local 打到熔断打开（与既有用例同一口径）。"""
        resilience = QaPersistentResilience(self.db)
        for _ in range(3):
            resilience.circuit_failure("provider:local")
        return resilience


# ── ① 同一件事两个码只留一个 ──────────────────────────────────────────────────


class OpsDisabledSingleCodeTests(_Base):
    """`SYNTHESIS_OPERATIONS_DISABLED` 与 `SYNTHESIS_FALLBACK_USED` 不许为同一件事同时落账。"""

    def test_ops_disabled_emits_exactly_one_code(self):
        seen = {}
        original = qa_pipeline.fallback_final_answer

        def _spy(*args, **kwargs):
            seen.update(kwargs)
            return original(*args, **kwargs)

        with mock.patch.object(qa_pipeline, "fallback_final_answer", side_effect=_spy):
            answer = self._handlers(synthesis_enabled=False)["synthesis"](self._context())

        # 结构事实：调用点**不传 reason** —— 传了就等于把"补一条 SYNTHESIS_FALLBACK_USED"
        # 的开关交给一个共用机制（下面 `test_the_shared_reason_mechanism_is_left_alone`
        # 证明那个机制一旦拿到不同的 reason 就真的会补码）。
        self.assertEqual("", seen.get("reason"),
                         "这一处不许传 reason：那会让同一件事落两个码")
        self.assertEqual(["SYNTHESIS_OPERATIONS_DISABLED"],
                         [str(item.get("code") or "") for item in (seen.get("degradation") or [])],
                         "更具体的那个码必须由 degradation 项带进载荷")

        # 载荷只有一个码，另一个码在整份载荷里一个字都不许出现
        self.assertEqual(["SYNTHESIS_OPERATIONS_DISABLED"], self._codes(answer))
        self.assertNotIn("SYNTHESIS_FALLBACK_USED", json.dumps(answer, ensure_ascii=False))
        self.assertEqual([OPS_ITEM["message"]], list(answer.get("degradation_reasons") or []),
                         "降级说明必须把原因说清楚（关掉综合模型），且只有这一条")

        # run 级账本同码、只一条
        self.assertEqual(["SYNTHESIS_OPERATIONS_DISABLED"], self._run_codes("synthesis"))
        self.assertEqual([], self.client.calls, "关掉就是关掉：模型一次都不许调用")
        self.assertEqual([], self._degraded_events("synthesis"))

    def test_the_shared_reason_mechanism_is_left_alone(self):
        """反向守卫：`fallback_final_answer` 的"reason 非空补一条码"是**共用**机制，不许改。

        这里直接钉住它：同一个降级项，reason 与 `item["message"]` **不同**时，两个码立刻并列
        出现 —— 这正是"调用点不许传 reason"的原因，也证明本次只改了调用点、没动共用机制。
        """
        graph = {"evidence": [_evidence()], "claims": [_claim()], "conflicts": []}
        payload = qa_synthesis.fallback_final_answer(
            graph=graph, level1={"gaps": [], "evidence": [_evidence()]},
            level2={"evidence_gaps": []}, degradation=[dict(OPS_ITEM)],
            models={"draft": "local", "research": "RAG检索", "synthesis": "local"},
            reason="综合模型未稳定返回。", question=QUESTION)

        self.assertEqual(["SYNTHESIS_OPERATIONS_DISABLED", "SYNTHESIS_FALLBACK_USED"],
                         self._codes(payload),
                         "共用机制仍然会补码（别的调用点依赖它）→ 调用点必须不传 reason")

    def test_same_reason_text_is_the_only_thing_that_used_to_hide_the_second_code(self):
        """如实记录改前的真实形态：reason 与 item["message"] **逐字相同**时载荷只有一个码。

        换句话说，改前"只看得到一个码"靠的是**文案去重**这个巧合，不是结构保证；
        本用例与 `test_ops_disabled_emits_exactly_one_code` 一起，把"曾经的巧合"留在案上。
        """
        graph = {"evidence": [_evidence()], "claims": [_claim()], "conflicts": []}
        payload = qa_synthesis.fallback_final_answer(
            graph=graph, level1={"gaps": [], "evidence": [_evidence()]},
            level2={"evidence_gaps": []}, degradation=[dict(OPS_ITEM)],
            models={"draft": "local", "research": "RAG检索", "synthesis": "local"},
            reason=OPS_ITEM["message"], question=QUESTION)

        self.assertEqual(["SYNTHESIS_OPERATIONS_DISABLED"], self._codes(payload),
                         "文案逐字相同时第二码被 message 去重吞掉（改前就是这个巧合）")


# ── ② 熔断分支的记账合并到单点 ────────────────────────────────────────────────


class CircuitSingleWritePointTests(_Base):
    """两条熔断分支必须走 `_record_run_degradation`，且行为一个字不变。"""

    def _spy_write_point(self):
        calls = []
        original = qa_pipeline._record_run_degradation

        def _spy(store_arg, context_arg, item_arg):
            calls.append(dict(item_arg))
            return original(store_arg, context_arg, item_arg)

        return calls, mock.patch.object(qa_pipeline, "_record_run_degradation", side_effect=_spy)

    def test_synthesis_circuit_uses_the_shared_write_point(self):
        resilience = self._open_circuit()
        contexts = []
        calls, patcher = self._spy_write_point()
        with patcher:
            context = self._context()
            contexts.append(context)
            answer = qa_pipeline.build_qa_stage_handlers(
                database=self.db, article_retriever=_FakeRetriever(), web_search=_FakeWebSearch(),
                store=self.store, provider_registry=_StubRegistry(), feature_flags=_Flags(),
                level1_generator=qa_level1.QaLevel1Generator(model_client=self.level1_client),
                final_synthesizer=qa_synthesis.QaFinalSynthesizer(model_client=self.client),
                resilience=resilience)["synthesis"](context)

        self.assertEqual([("synthesis", "PROVIDER_CIRCUIT_OPEN")],
                         [(item.get("stage"), item.get("code")) for item in calls],
                         "熔断的 run 级记账必须走统一写点（否则本列表为空 = 又回到自记账）")
        self.assertEqual(["PROVIDER_CIRCUIT_OPEN"], self._run_codes("synthesis"))
        self.assertEqual(["PROVIDER_CIRCUIT_OPEN"], self._codes(answer))
        self.assertNotIn(NEW_CODE, self._codes(answer), "熔断绝不许被吃成证据摘录")
        self.assertEqual([], self.client.calls, "熔断打开时根本没调用模型")
        # 合并写点**不许**顺带出这两样（熔断是独立一格）
        self.assertEqual([], self._degraded_events("synthesis"))
        self.assertNotIn("synthesis", contexts[0].get("_stage_degraded") or {})

    def test_level1_circuit_uses_the_shared_write_point(self):
        resilience = self._open_circuit()
        calls, patcher = self._spy_write_point()
        with patcher:
            context = self._context()
            answer = qa_pipeline.build_qa_stage_handlers(
                database=self.db, article_retriever=_FakeRetriever(), web_search=_FakeWebSearch(),
                store=self.store, provider_registry=_StubRegistry(), feature_flags=_Flags(),
                level1_generator=qa_level1.QaLevel1Generator(model_client=self.level1_client),
                final_synthesizer=qa_synthesis.QaFinalSynthesizer(model_client=self.client),
                resilience=resilience)["level1_draft"](context)

        self.assertEqual([("level1_draft", "PROVIDER_CIRCUIT_OPEN")],
                         [(item.get("stage"), item.get("code")) for item in calls],
                         "熔断的 run 级记账必须走统一写点（否则本列表为空 = 又回到自记账）")
        self.assertEqual(["PROVIDER_CIRCUIT_OPEN"], self._run_codes("level1_draft"))
        self.assertIn("本地模型当前响应过慢",
                      str(answer.get("draft_answer") or answer.get("draft") or ""),
                      "熔断分支照旧产出证据锚定草稿（行为不变）")
        self.assertEqual([], self.level1_client.calls, "熔断打开时根本没调用模型")
        self.assertEqual([], self._degraded_events("level1_draft"))
        self.assertNotIn("level1_draft", context.get("_stage_degraded") or {})

    def test_evidence_only_still_goes_through_the_same_write_point(self):
        """反向守卫：换成共用写点后，"证据摘录"那条路的事件与阶段标记一个都没少。"""
        post = mock.Mock(side_effect=requests.exceptions.Timeout("read timeout"))
        calls, patcher = self._spy_write_point()
        with patcher, mock.patch.object(qa_synthesis.requests, "post", post):
            context = self._context()
            answer = self._handlers()["synthesis"](context)

        self.assertEqual([("synthesis", NEW_CODE)],
                         [(item.get("stage"), item.get("code")) for item in calls])
        self.assertEqual([NEW_CODE], self._run_codes("synthesis"))
        self.assertEqual([NEW_CODE], self._codes(answer))
        self.assertEqual([NEW_CODE], [item.get("code") for item in self._degraded_events("synthesis")])
        self.assertEqual(NEW_CODE,
                         str(((context.get("_stage_degraded") or {}).get("synthesis") or {})
                             .get("code") or ""),
                         "统一记账点的 ③④ 两步（事件 + 阶段标记）不许被本次合并弄丢")


if __name__ == "__main__":
    unittest.main()

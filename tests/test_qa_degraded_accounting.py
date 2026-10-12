#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：降级记账一致性（方案① —— run 级派生自答案级 + 阶段级显式记账）。

B 机 174 runs 只读普查实测的缺陷（样例 run `d980edf5c1b44949acccbf212daeb3f5` /
`db1fc148152e47178ce348f256dcad0b`）：
  · 答案级 `degraded=true` 110 条，其中 **3 条** run 级却说没降级
    （`degraded=0` + `degradation_json=[]`，而 `qa_stage_runs` 里 synthesis /
    citation_validation 都是 completed）→ 两本账互相打脸；
  · **8 条** "`degraded=true` 但 `degradation_reasons=[]`" → 一个字不说为什么降级。

根因：`qa_synthesis.generate()` 的**内部兜底**只改答案载荷、**不抛异常**，于是
`qa_pipeline.synthesis()` 的"正常返回"这条路上 run 级一个字都没记。

本文件钉住（全程真 QaStore + 临时 sqlite 隔离，零模型 / 零嵌入 / 零网络端点）：
  T1 synthesis 正常返回但答案被标注降级 → run 级必须记账，且界面读得到；
  T2 终局写口 `update_run(final_answer=...)` 的结构派生（且只补一次）；
  T3 降级无原因 → 用诊断码回填，**不许**留下空 reasons（降级不许静默）；
  T4 反向守卫：正常答案不许被误标降级（载荷一个字都不改）；
  T5 经真 QaOrchestrator 跑一条 fast 全链 → run 级与答案级必须一致；
  T6 读侧口径：历史行（run 级漏记）在界面上照样看得见，且**不回写历史**；
  T7 `qa_realignment.run_signals` 用 run 级 `degradation` 兜 reasons（修不存在的键名 bug）。
"""
import json
import os
import sys
import tempfile
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_pipeline  # noqa: E402
import qa_synthesis  # noqa: E402
from qa_gateway import _public_run  # noqa: E402
from qa_observability import QaMetricsService  # noqa: E402
from qa_orchestrator import QaOrchestrator  # noqa: E402
from qa_realignment import assess_dissatisfaction, run_signals  # noqa: E402
from qa_resilience import QaPersistentResilience  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

QUESTION = "香港家族办公室税收优惠政策对内地高净值客户有什么影响？"
SCOPE = {"owner_user_id": "degraded-accounting", "session_id": "s1", "industry_pack_id": "auto"}
FALLBACK_REASON = "最终综合模型未稳定返回，已改用证据约束重组。"


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


def _claim(ref):
    return {
        "claim_id": "c1",
        "text": "香港家族办公室税收优惠政策对内地高净值客户有影响",
        "claim_type": "current_fact", "confidence": 0.82,
        "valid_from": "2026-10-09", "valid_to": None, "scope": ["家族办公室"],
        "evidence_refs": [ref], "needs_verification": True,
        "verification_status": "unverified",
    }


class _FakeRetriever:
    def __init__(self, evidence):
        self.evidence = list(evidence)

    def retrieve(self, plan, **kwargs):
        return {"evidence": [dict(item) for item in self.evidence],
                "stats": {"eligible": len(self.evidence), "adopted": len(self.evidence)}}


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


class DegradedAccountingCase(unittest.TestCase):
    """公共装置：真 QaStore + 临时 sqlite（每个用例一个库，互不干扰）。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "degraded.sqlite3"))
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())
        self.assertEqual(self.db.backend, "sqlite", "测试必须跑在隔离 sqlite 上")
        self.store = QaStore(self.db)
        self.store.ensure_schema()

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:  # noqa: BLE001
            pass
        self.temp_dir.cleanup()

    def _create_run(self, key):
        return self.store.create_run(
            {"industry_pack_id": "auto", "question": QUESTION, "mode": "standard"},
            owner_user_id=SCOPE["owner_user_id"], idempotency_key=key)

    def _raw_row(self, run_id):
        with self.db.lock:
            return self.db.connection.execute(
                "SELECT degraded, degradation_json FROM qa_runs WHERE id=?", (str(run_id),)).fetchone()

    def _fallback_payload(self):
        """`generate()` 内部兜底返回的**合法载荷**：degraded=true + 1 条可公开 reason。"""
        graph = {"evidence": [_evidence("article:1")], "claims": [_claim("article:1")], "conflicts": []}
        return qa_synthesis.fallback_final_answer(
            graph=graph,
            level1={"gaps": [], "evidence": [_evidence("article:1")], "claims": [_claim("article:1")]},
            level2={"evidence_gaps": []},
            degradation=[], models={"draft": "local", "research": "RAG检索", "synthesis": "local"},
            reason=FALLBACK_REASON, question=QUESTION)


class T1SynthesisNormalReturnAccountingTests(DegradedAccountingCase):
    """T1：`d980edf5` 的最小复现 —— synthesis 正常返回，答案降级，run 级必须记账。"""

    def _context(self, run_id):
        outputs = {
            "plan": {"question": QUESTION, "standalone_question": QUESTION,
                     "queries": ["香港家族办公室 税收优惠"], "entities": ["家族办公室"],
                     "decomposition": {"is_multi_hop": False, "pattern": "single", "hop_count": 1,
                                       "hops": [{"id": "h1", "question": QUESTION, "depends_on": []}]}},
            "level1_draft": {
                "contract_version": "unified-qa-v1", "draft_answer": "草稿",
                "claims": [_claim("article:1")], "gaps": [], "evidence": [_evidence("article:1")]},
            "level1_retrieval": {"evidence": [_evidence("article:1")], "stats": {"adopted": 1}},
        }
        return {"request": {"question": QUESTION, "industry_pack_id": "auto", "mode": "standard"},
                "run": {"id": run_id, **SCOPE}, "outputs": outputs}

    def test_synthesis_internal_fallback_is_recorded_on_run_level(self):
        run = self._create_run("t1-%s" % self.id())
        payload = self._fallback_payload()
        self.assertTrue(payload["degraded"], "前提：这份兜底载荷必须是降级答案")
        self.assertTrue(payload["degradation_reasons"], "前提：兜底载荷必须带一条 reason")

        calls = {"n": 0}

        def _fake_generate(self, **kwargs):
            # 打桩点：模拟"内部兜底"——**正常返回**，不抛异常（真缺陷的形态）
            calls["n"] += 1
            return json.loads(json.dumps(payload, ensure_ascii=False))

        original = qa_synthesis.QaFinalSynthesizer.generate
        qa_synthesis.QaFinalSynthesizer.generate = _fake_generate
        try:
            handlers = qa_pipeline.build_qa_stage_handlers(
                database=self.db, article_retriever=_FakeRetriever([_evidence("article:1")]),
                web_search=_FakeWebSearch(), store=self.store,
                provider_registry=_StubRegistry(), feature_flags=_StubFlags(),
                final_synthesizer=qa_synthesis.QaFinalSynthesizer(
                    model_client=lambda profile, messages, timeout=90: ""),
                resilience=QaPersistentResilience(self.db))
            answer = handlers["synthesis"](self._context(str(run["id"])))
        finally:
            qa_synthesis.QaFinalSynthesizer.generate = original

        self.assertEqual(1, calls["n"], "打桩必须真的被调用（否则这条用例什么都没验）")
        self.assertTrue(answer.get("degraded"))

        stored = self.store.get_run(str(run["id"]))
        self.assertTrue(stored["degraded"], "答案级降级 → run 级不许说没降级（本次要修的缺陷）")
        codes = [str(item.get("code") or "") for item in (stored["degradation"] or [])]
        self.assertIn("SYNTHESIS_FALLBACK_USED", codes, stored["degradation"])
        stages = [str(item.get("stage") or "") for item in (stored["degradation"] or [])]
        self.assertIn("synthesis", stages, "阶段级也要能看出是哪个阶段降级的")
        messages = " ".join(str(item.get("message") or "") for item in (stored["degradation"] or []))
        self.assertTrue(messages.strip(), "记账必须带 message（否则又是'降级但不说为什么'）")

        self.assertTrue(_public_run(stored)["degraded"], "界面上必须一眼看得见降级")
        row = self._raw_row(str(run["id"]))
        self.assertEqual(1, int(row["degraded"]), "库里那一列也必须真的是 1")


class T2FinalWritePortDerivationTests(DegradedAccountingCase):
    """T2：终局写口的结构派生（run 级 = 答案级派生 + 阶段级显式记账）。"""

    def test_update_run_derives_run_level_from_degraded_answer(self):
        run = self._create_run("t2-%s" % self.id())
        answer = {"contract_version": "unified-qa-v1", "status": "partial", "answer": "兜底答案 [1]。",
                  "sections": {"diagnostics": {"degradation_codes": ["SYNTHESIS_FALLBACK_USED"]}},
                  "degraded": True, "degradation_reasons": ["综合模型未稳定返回"]}

        self.store.update_run(str(run["id"]), status="completed", stage="completed", final_answer=answer)

        stored = self.store.get_run(str(run["id"]))
        self.assertTrue(stored["degraded"])
        self.assertTrue(stored["degradation"], "终局写口必须补上 run 级记账")
        item = stored["degradation"][0]
        self.assertEqual("final_answer", item.get("stage"))
        self.assertEqual("ANSWER_DEGRADED_NOT_RECORDED", item.get("code"))
        self.assertEqual("综合模型未稳定返回", item.get("message"))
        self.assertEqual(1, int(self._raw_row(str(run["id"]))["degraded"]))

        # 幂等：同一份载荷再写一次，不许把同一条记账追加成两条
        self.store.update_run(str(run["id"]), status="completed", stage="completed", final_answer=answer)
        self.assertEqual(1, len(self.store.get_run(str(run["id"]))["degradation"]))


class T3DegradedWithoutReasonTests(DegradedAccountingCase):
    """T3：`degraded=true` 而 `degradation_reasons=[]` —— 必须用诊断码回填，不许静默。"""

    def test_degraded_answer_without_reasons_backfills_from_diagnostic_codes(self):
        run = self._create_run("t3-%s" % self.id())
        answer = {"contract_version": "unified-qa-v1", "status": "partial", "answer": "答案 [1]。",
                  "sections": {"diagnostics": {"degradation_codes": ["NETWORK_UNREACHABLE"]}},
                  "degraded": True, "degradation_reasons": []}

        self.store.update_run(str(run["id"]), status="completed", stage="completed", final_answer=answer)

        stored = self.store.get_run(str(run["id"]))
        messages = [str(item.get("message") or "") for item in (stored["degradation"] or [])]
        self.assertTrue(messages, "run 级降级必须至少 1 条原因（本次要修的 8 条空 reasons）")
        self.assertIn("NETWORK_UNREACHABLE", " ".join(messages), messages)

        reasons = stored["final_answer"].get("degradation_reasons") or []
        self.assertTrue(reasons, "答案级也不许留空 reasons（自相矛盾必须被消除）")
        self.assertIn("NETWORK_UNREACHABLE", " ".join(str(item) for item in reasons), reasons)


class T4NormalAnswerNotMarkedTests(DegradedAccountingCase):
    """T4：反向守卫 —— 正常答案不许被误标成降级。"""

    def test_normal_answer_stays_not_degraded(self):
        run = self._create_run("t4-%s" % self.id())
        answer = {"contract_version": "unified-qa-v1", "status": "ready", "answer": "正常答案 [1]。",
                  "sections": {"summary": "正常"}, "claims": [], "conflicts": [], "evidence": [],
                  "citations": [], "citation_map": {}, "cutoff_at": "", "degraded": False}

        self.store.update_run(str(run["id"]), status="completed", stage="completed", final_answer=answer)

        stored = self.store.get_run(str(run["id"]))
        self.assertFalse(stored["degraded"], "正常答案不许被误标降级")
        self.assertEqual([], list(stored["degradation"] or []))
        self.assertEqual(0, int(self._raw_row(str(run["id"]))["degraded"]))
        self.assertNotIn("degradation_reasons", stored["final_answer"],
                         "正常答案的载荷必须逐字原样落库（不注入任何处理痕迹）")
        self.assertFalse(_public_run(stored)["degraded"])


class T5OrchestratorEndToEndTests(DegradedAccountingCase):
    """T5：经真 QaOrchestrator 跑一条 fast 全链（阶段桩）→ run 级与答案级必须一致。"""

    def test_full_chain_keeps_run_and_answer_level_in_agreement(self):
        payload = self._fallback_payload()

        def _synthesis(context):
            return dict(payload)

        def _citation_validation(context):
            # 真实现就是"把 synthesis 的结果当最终答案交出去"（只补 citations）
            return dict(context["outputs"]["synthesis"])

        handlers = {
            "plan": lambda context: {"question_plan": {}},
            "level1_retrieval": lambda context: {"evidence": [_evidence("article:1")], "stats": {"adopted": 1}},
            "logic_validation": lambda context: {"status": "passed"},
            "level1_draft": lambda context: {"draft_answer": "草稿"},
            "synthesis": _synthesis,
            "citation_validation": _citation_validation,
        }
        run = self.store.create_run(
            {"question": QUESTION, "industry_pack_id": "auto", "origin": "api", "mode": "fast"},
            owner_user_id=SCOPE["owner_user_id"], idempotency_key="t5-%s" % self.id())

        outcome = QaOrchestrator(self.store, handlers).execute(str(run["id"]))

        self.assertTrue(outcome["success"], outcome)
        stored = self.store.get_run(str(run["id"]))
        answer = stored["final_answer"]
        self.assertTrue(answer.get("degraded"), "前提：这条链交出的答案是降级答案")
        self.assertTrue(answer.get("degradation_reasons"))
        self.assertTrue(stored["degraded"], "终局跑完后 run 级不许说没降级")
        self.assertTrue(stored["degradation"])
        self.assertEqual(bool(stored["degraded"]), bool(answer.get("degraded")),
                         "run 级与答案级必须一致（这是本方案要钉死的不变量）")
        self.assertTrue(_public_run(stored)["degraded"])
        self.assertEqual("completed", str(stored["status"]),
                         "status 口径不改：completed = '跑完了'，与是否降级是两件事")


class T6ReadSideAndHistoryTests(DegradedAccountingCase):
    """T6：历史行（run 级漏记）靠读侧口径可见，且**不回写历史**。"""

    def _insert_historical_run(self, run_id):
        """照抄 B 机那 3 条的形态：答案 degraded=true，run 级 degraded=0 / degradation_json=[]。"""
        answer = {"contract_version": "unified-qa-v1", "status": "partial", "answer": "历史答案 [1]。",
                  "sections": {"diagnostics": {"degradation_codes": ["SYNTHESIS_FALLBACK_USED"]}},
                  "degraded": True, "degradation_reasons": ["最终综合模型未稳定返回。"]}
        with self.db.lock:
            self.db.connection.execute(
                """INSERT INTO qa_runs(
                       id, contract_version, session_id, owner_user_id, industry_pack_id,
                       origin, mode, question_hash, question_text, request_json, status,
                       current_stage, idempotency_key, final_answer_json, degraded,
                       degradation_json, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, "unified-qa-v1", "s1", SCOPE["owner_user_id"], "auto", "getinfo_ui",
                 "standard", "hash-" + run_id, QUESTION, "{}", "completed", "completed",
                 "idem-" + run_id, json.dumps(answer, ensure_ascii=False), 0, "[]",
                 "2026-10-09T00:00:00.000Z", "2026-10-09T00:00:00.000Z"),
            )
            self.db.connection.commit()

    def test_historical_mismatch_is_visible_and_never_rewritten(self):
        run_id = "historical-%s" % self.id()
        self._insert_historical_run(run_id)

        stored = self.store.get_run(run_id)
        self.assertFalse(stored["degraded"], "前提：历史行 run 级就是漏记的")
        self.assertTrue(_public_run(stored)["degraded"], "答案级降级必须在界面上看得见")
        self.assertEqual(1, int(QaMetricsService(self.db).snapshot()["degraded"]),
                         "指标口径必须与界面同口径（否则巡检照样看不见）")
        self.assertEqual(0, int(self._raw_row(run_id)["degraded"]),
                         "读侧只改读出来的值：历史行一个字节都不许回写")


class T8NoiseFilterStillFiltersTests(DegradedAccountingCase):
    """T8：第 7 条的证据 —— 回填**不会**把被有意过滤的内部噪声放回来。

    做法是走**真生成端**：`fallback_final_answer` 拿到的噪声文案会被
    `_public_degradation_reasons` 有意丢掉 → 载荷就成了"degraded=true 而 reasons=[]"
    （B 机 8 条空 reasons 的形态）。回填只允许把**公开诊断码**写进去，
    绝不允许把"暂时无法连接 local"这类内部噪声重新暴露给用户。
    """

    NOISE_MESSAGES = (
        "暂时无法连接 local 模型，已改用证据约束重组。",
        "综合模型响应超时，已改用证据约束重组。",
        "RAG增强检索暂不可用，已退回关键词检索。",
        "本地模型已保留原始证据片段，未做归纳。",
    )

    def test_backfill_uses_public_codes_and_never_resurrects_noise(self):
        for index, noise in enumerate(self.NOISE_MESSAGES):
            run = self._create_run("t8-%s-%s" % (index, self.id()))
            payload = qa_synthesis.fallback_final_answer(
                graph={"evidence": [_evidence("article:1")], "claims": [_claim("article:1")],
                       "conflicts": []},
                level1={"gaps": [], "evidence": [_evidence("article:1")]},
                level2={"evidence_gaps": []},
                degradation=[{"stage": "synthesis", "code": "SYNTHESIS_FALLBACK_USED",
                              "message": noise}],
                models={"draft": "local", "research": "RAG检索", "synthesis": "local"},
                reason="", question=QUESTION)

            # 前提复现：过滤正则**有意**把噪声文案吃掉了 → 降级却一条原因都没有
            self.assertTrue(payload["degraded"], noise)
            self.assertEqual([], payload["degradation_reasons"], noise)
            self.assertEqual(["SYNTHESIS_FALLBACK_USED"],
                             payload["sections"]["diagnostics"]["degradation_codes"], noise)

            self.store.update_run(str(run["id"]), status="completed", stage="completed",
                                  final_answer=payload)

            stored = self.store.get_run(str(run["id"]))
            reasons = [str(item) for item in stored["final_answer"]["degradation_reasons"]]
            self.assertTrue(reasons, "过滤后为空必须回填（否则又是'降级却不说为什么'）：%s" % noise)
            self.assertIn("SYNTHESIS_FALLBACK_USED", " ".join(reasons), reasons)
            joined = " ".join(reasons)
            for marker in ("暂时无法连接", "响应超时", "暂不可用", "已保留"):
                self.assertNotIn(marker, joined,
                                 "回填不许把被有意过滤的内部噪声放回来：%s" % marker)


class T7RealignmentFallbackReasonTests(DegradedAccountingCase):
    """T7：`run_signals` 读 `run["degradation"]`（修不存在的键名 bug）。"""

    def test_run_level_degradation_backfills_reasons(self):
        run = self._create_run("t7-%s" % self.id())
        self.store.mark_degraded(str(run["id"]), {
            "stage": "synthesis", "code": "SYNTHESIS_FALLBACK_USED", "message": FALLBACK_REASON})
        self.store.update_run(str(run["id"]), status="completed", stage="completed",
                              final_answer={"contract_version": "unified-qa-v1", "status": "partial",
                                            "answer": "答案 [1]。", "degraded": False})

        signals = run_signals(self.store, str(run["id"]))

        self.assertTrue(signals["degraded"])
        self.assertEqual([FALLBACK_REASON], signals["degradation_reasons"],
                         "答案级没有 reasons 时必须用 run 级 degradation 兜（旧写法读的是不存在的键）")
        detail = assess_dissatisfaction(signals)["detail"]["ANSWER_DEGRADED"]
        self.assertEqual([FALLBACK_REASON], detail["reasons"], "回执里也要给出可读原因")


if __name__ == "__main__":
    unittest.main()

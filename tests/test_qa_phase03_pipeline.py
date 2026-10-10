#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 03 · 接线验收（P03-01…P03-04 接进真实链路）。

单测证明"函数对"，这里证明"**真的接上了**、而且没破坏 Phase 02 的契约"：
  1. `level1_retrieval` / 多跳 / level2 的证据包真的被核验（每条带 verification，stats 里有核验回执）；
  2. **Phase 02 的 evidence_layer 回执键集一个字不变**（核验回执走兄弟键 stats["verification"]），
     关掉 `QA_VERIFIER_ENABLED=0` 时阶段返回结构与 Phase 02 逐字相同（可回滚）；
  3. 有反证的证据被闸门挡掉、且身份按 rejected 落 `qa_evidence_seen`；
  4. `conflict_review` 真的把**模型自评**的 claim 状态覆盖成规则核验结果，并落 `qa_claims`；
  5. 失败路径：store 坏掉 / 核验内部炸掉，证据一条不少、问答不中断。

隔离：`DATABASE_TYPE=sqlite` + 临时库；`setUp` 断言 `db.backend == 'sqlite'`。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_pipeline  # noqa: E402
import qa_verifier as verifier  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

SCOPE = {"owner_user_id": "u1", "session_id": "s1", "industry_pack_id": "auto"}
QUESTION = "宁德时代10月9日股价大涨的原因是什么？"
# Phase 02 冻结的 evidence_layer 回执键集（阶段 03 不许动它）
EVIDENCE_RECEIPT_KEYS = {
    "evidence_layer", "annotated", "seen_dropped", "dedupe_dropped", "recorded", "reason",
}


def _evidence(ref, *, title=None, content=None, **overrides):
    item = {
        "evidence_ref": ref,
        "source_type": "article",
        "title": title or ("宁德时代10月9日股价大涨的原因 %s" % ref),
        "source_url": "https://example.com/%s" % ref,
        "article_id": int(str(ref).split(":")[-1]),
        "content_excerpt": content or "10月9日，宁德时代股价大涨5.2%，主要因为固态电池量产消息与储能订单增长。",
        "published_at": "2026-10-09",
        "authority_level": 60,
        "score": 30.0,
        "retrieval_method": "keyword",
        "match_reason": "标题命中：宁德时代",
        "relationship": "supports",
        "metadata": {"matched_keywords": ["宁德时代"]},
    }
    item.update(overrides)
    return item


class _FakeRetriever:
    def __init__(self, evidence):
        self.evidence = list(evidence)
        self.calls = []

    def retrieve(self, plan, **kwargs):
        self.calls.append(dict(plan))
        return {"evidence": [dict(item) for item in self.evidence],
                "stats": {"eligible": len(self.evidence), "adopted": len(self.evidence)}}


class _FakeWebSearch:
    def search(self, queries, *, enabled=True, limit=8):
        return {"evidence": [], "status": "disabled", "providers": [], "errors": []}


class _PipelineBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "phase03.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.assertEqual(self.db.backend, "sqlite", "测试必须跑在隔离 sqlite 上")
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        verifier._STORE_CACHES.clear()

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def _row(self, sql, params=()):
        return self.db.connection.execute(sql, params).fetchone()

    def _apply(self, evidence, rejected=(), *, run_meta=None, question=QUESTION, plan=None):
        return qa_pipeline._apply_evidence_layer(
            list(evidence), rejected=list(rejected), question=question, plan=plan or {},
            run_meta=run_meta or {"id": "run-1", **SCOPE}, store=self.store,
            corpus_version="corpus-1")


class ApplyEvidenceLayerTests(_PipelineBase):
    def test_evidence_is_verified_and_annotated(self):
        kept, audit = self._apply([_evidence("article:1"),
                                   _evidence("article:2", source_url="https://other.com/2")])
        self.assertEqual(len(kept), 2)
        self.assertEqual(audit["verification"]["verifier"], verifier.VERIFIER_VERSION)
        self.assertEqual(audit["verification"]["checked"], 2)
        self.assertEqual(audit["verification"]["verdicts"].get("SUPPORTED"), 2)
        for item in kept:
            verification = item["metadata"]["evidence_layer"]["verification"]
            self.assertTrue(verification["verified"])
            self.assertIn(verification["verdict"], verifier.VERDICTS)
            self.assertTrue(verification["reason_text"])
        # 顶层键集不变（只看硬约束：既有契约字段一个不少、也不多出冻结外的字段）
        self.assertEqual(set(kept[0].keys()), set(_evidence("article:9").keys()))

    def test_irrelevant_evidence_is_not_supported(self):
        kept, audit = self._apply([_evidence("article:1",
                                             content="比亚迪销量创新高，芯片供应紧张，行业整体承压。")])
        verdict = kept[0]["metadata"]["evidence_layer"]["verification"]["verdict"]
        self.assertNotEqual(verdict, "SUPPORTED", "只有关键词像就不许判支持（MASTER_RULES 第 11 条）")
        self.assertEqual(audit["verification"]["verdicts"].get("UNVERIFIED"), 1)

    def test_refuted_evidence_is_gated_and_registered_rejected(self):
        good = _evidence("article:1")
        junk = _evidence("article:9", title="医保新规适用于民营医院",
                         content="医保新规适用于民营医院，报销比例下调。")
        kept, audit = self._apply([good, junk], question="医保新规不适用于民营医院")
        self.assertEqual([item["evidence_ref"] for item in kept], ["article:1"],
                         "有反证的证据不许进证据包")
        self.assertGreaterEqual(audit["verification"]["dropped"], 1)
        status = self._row(
            "SELECT status FROM qa_evidence_seen WHERE session_id='s1' AND evidence_ref='article:9'")
        self.assertIsNotNone(status, "被核验拒掉的证据必须留身份（MASTER_RULES 第 14 条）")
        self.assertEqual(str(status[0]), "rejected")

    def test_gate_never_empties_the_pack(self):
        """闸门把整批清空时退回原证据（并在审计里留痕），绝不给用户空证据包。"""
        junk = _evidence("article:9", content="医保新规适用于民营医院，报销比例下调。")
        kept, audit = self._apply([junk], question="医保新规不适用于民营医院")
        self.assertEqual(len(kept), 1)
        self.assertEqual(audit["verification"]["dropped"], 0)
        self.assertIn("gate_emptied_evidence_fallback", audit["verification"]["degraded"])

    def test_gate_off_keeps_refuted_evidence(self):
        os.environ["QA_VERIFIER_GATE"] = "off"
        try:
            junk = _evidence("article:9", content="医保新规适用于民营医院，报销比例下调。")
            kept, audit = self._apply([junk], question="医保新规不适用于民营医院")
            self.assertEqual(len(kept), 1)
            self.assertEqual(audit["verification"]["dropped"], 0)
        finally:
            os.environ.pop("QA_VERIFIER_GATE", None)

    def test_rerank_puts_verified_evidence_first(self):
        weak = _evidence("article:1", title="行业综述",
                         content="比亚迪销量创新高，芯片供应紧张。", source_url="https://a.com/1")
        strong = _evidence("article:2", title="宁德时代10月9日股价大涨",
                           content="10月9日，宁德时代股价大涨5.2%，因为固态电池量产消息。",
                           source_url="https://b.com/2")
        kept, audit = self._apply([weak, strong])
        self.assertEqual(kept[0]["evidence_ref"], "article:2")
        self.assertGreaterEqual(audit["verification"]["reordered"], 1)

    def test_phase02_receipt_keys_unchanged(self):
        _kept, audit = self._apply([_evidence("article:1")])
        receipt = qa_pipeline._evidence_layer_receipt(audit)
        self.assertEqual(set(receipt.keys()), EVIDENCE_RECEIPT_KEYS,
                         "Phase 02 的 evidence_layer 回执键集不许动（核验回执走兄弟键）")
        self.assertTrue(qa_pipeline._verification_receipt(audit))

    def test_disabled_verifier_restores_phase02_shape(self):
        os.environ["QA_VERIFIER_ENABLED"] = "0"
        try:
            kept, audit = self._apply([_evidence("article:1")])
            self.assertEqual(set(qa_pipeline._evidence_layer_receipt(audit).keys()),
                             EVIDENCE_RECEIPT_KEYS)
            self.assertEqual(qa_pipeline._verification_receipt(audit), {},
                             "关掉核验之后不许再往 stats 里塞核验回执")
            self.assertNotIn("verification", kept[0]["metadata"]["evidence_layer"])
        finally:
            os.environ.pop("QA_VERIFIER_ENABLED", None)

    def test_verifier_failure_never_breaks_evidence_layer(self):
        with mock.patch.object(verifier, "check_source", side_effect=RuntimeError("verifier down")):
            kept, audit = self._apply([_evidence("article:1")])
        self.assertEqual(len(kept), 1, "核验炸掉时证据一条不少")
        self.assertIn("verifier down", audit["verification"]["reason"])

    def test_broken_store_still_verifies(self):
        broken = mock.Mock()
        broken.record_seen_evidence.side_effect = RuntimeError("db down")
        broken.seen_evidence.side_effect = RuntimeError("db down")
        broken.get_verification_cache.side_effect = RuntimeError("db down")
        broken.put_verification_cache.side_effect = RuntimeError("db down")
        kept, audit = qa_pipeline._apply_evidence_layer(
            [_evidence("article:1")], rejected=[], question=QUESTION, plan={},
            run_meta={"id": "run-x", **SCOPE}, store=broken)
        self.assertEqual(len(kept), 1)
        self.assertTrue(kept[0]["metadata"]["evidence_layer"]["verification"]["verified"])
        self.assertIn("db down", audit["record_error"])


class VerificationCachePersistenceTests(_PipelineBase):
    """P03-04 的 cache：走真 store 落 `qa_retrieval_cache`（复用既有表，不动 schema）。"""

    def test_second_call_hits_persistent_cache(self):
        item = _evidence("article:1")
        first, _audit = self._apply([item])
        self.assertEqual(first[0]["metadata"]["evidence_layer"]["verification"]["cache"], "miss")
        rows = self._row("SELECT COUNT(*) FROM qa_retrieval_cache WHERE namespace='qa_verification'")
        self.assertGreaterEqual(int(rows[0]), 1, "核验结果没有落库")

        # 清掉进程内缓存，模拟换 worker：必须能从库里命中
        verifier._STORE_CACHES.clear()  # noqa: SLF001
        second, _audit2 = self._apply([item])
        self.assertEqual(second[0]["metadata"]["evidence_layer"]["verification"]["cache"], "hit")

    def test_cache_row_carries_verifier_version_and_expiry(self):
        self._apply([_evidence("article:1")])
        row = self._row("SELECT kb_version, expires_at, created_at FROM qa_retrieval_cache"
                        " WHERE namespace='qa_verification' LIMIT 1")
        self.assertEqual(str(row[0]), verifier.VERIFIER_VERSION)
        self.assertGreater(str(row[1]), str(row[2]))

    def test_cache_can_be_turned_off(self):
        os.environ["QA_VERIFIER_CACHE_PERSIST"] = "0"
        try:
            self._apply([_evidence("article:1")])
            rows = self._row("SELECT COUNT(*) FROM qa_retrieval_cache WHERE namespace='qa_verification'")
            self.assertEqual(int(rows[0]), 0, "关掉落库后不许再写缓存表")
        finally:
            os.environ.pop("QA_VERIFIER_CACHE_PERSIST", None)


class ClaimVerificationStageTests(_PipelineBase):
    """conflict_review 阶段：claim 级核验真的跑、真的覆盖模型自评、真的落库。"""

    def setUp(self):
        super().setUp()
        self.retriever = _FakeRetriever([_evidence("article:1")])
        self.handlers = qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=self.retriever, web_search=_FakeWebSearch(),
            store=self.store)

    def _context(self, *, status="qualified"):
        return {
            "request": {"question": QUESTION, "industry_pack_id": "auto", "mode": "standard"},
            "run": {"id": "run-cr", **SCOPE},
            "outputs": {
                "plan": {"question": QUESTION, "standalone_question": QUESTION,
                         "queries": ["宁德时代 股价"], "entities": ["宁德时代"], "decomposition": {}},
                "level1_draft": {
                    "contract_version": "unified-qa-v1",
                    "draft_answer": "草稿",
                    "claims": [{
                        "claim_id": "c1",
                        "text": "宁德时代10月9日股价大涨，因为固态电池量产消息与储能订单增长",
                        "claim_type": "current_fact", "confidence": 0.82,
                        "valid_from": "2026-10-09", "valid_to": None, "scope": ["宁德时代"],
                        "evidence_refs": ["article:1"], "needs_verification": True,
                        "verification_status": status,
                    }],
                    "entities": ["宁德时代"], "timeline_hints": [], "gaps": [],
                    "followup_queries": [], "evidence": [_evidence("article:1")], "citations": [],
                },
            },
        }

    def test_stage_overrides_model_self_assessment_and_persists(self):
        graph = self.handlers["conflict_review"](self._context(status="confirmed"))
        claim = graph["claims"][0]["claim"]
        self.assertEqual(claim["verification_status"], "confirmed",
                         "有直接证据支持的结论应判 confirmed（覆盖模型自评）")
        self.assertIn("verification", graph)
        self.assertEqual(graph["verification"]["stats"]["unsupported_claim_rate"], 0.0)
        row = self._row("SELECT verification_status, claim_text FROM qa_claims WHERE run_id='run-cr'")
        self.assertIsNotNone(row, "claim 核验结果没有落库")
        self.assertEqual(str(row[0]), "confirmed")
        self.assertTrue(str(row[1]))

    def test_unsupported_claim_is_downgraded_in_db(self):
        """模型说 confirmed，但证据根本不支持 → 库里必须是证据不足/未核验。"""
        context = self._context(status="confirmed")
        context["outputs"]["level1_draft"]["evidence"] = [
            _evidence("article:1", content="比亚迪销量创新高，芯片供应紧张，行业整体承压。")]
        graph = self.handlers["conflict_review"](context)
        status = graph["claims"][0]["claim"]["verification_status"]
        self.assertIn(status, {"unverified", "conflicted", "insufficient_evidence", "qualified"})
        self.assertNotEqual(status, "confirmed")
        row = self._row("SELECT verification_status FROM qa_claims WHERE run_id='run-cr'")
        self.assertNotEqual(str(row[0]), "confirmed", "库里仍是模型自评 = MASTER_RULES 第 11 条被绕过")
        self.assertGreater(graph["verification"]["stats"]["unsupported_claim_rate"], 0.0)

    def test_claim_without_evidence_is_insufficient(self):
        context = self._context()
        context["outputs"]["level1_draft"]["claims"][0]["evidence_refs"] = []
        graph = self.handlers["conflict_review"](context)
        self.assertEqual(graph["claims"][0]["claim"]["verification_status"], "insufficient_evidence")
        self.assertEqual(graph["verification"]["stats"]["claim_without_evidence"], 1)
        self.assertIn(verifier.REASON_CLAIM_NO_EVIDENCE, graph["claims"][0]["verification"]["reasons"])

    def test_dangling_reference_is_flagged(self):
        context = self._context()
        context["outputs"]["level1_draft"]["claims"][0]["evidence_refs"] = ["article:404"]
        graph = self.handlers["conflict_review"](context)
        node = graph["claims"][0]
        self.assertEqual(node["verification"]["pairs"][0]["evidence_ref"], "article:404")
        self.assertIn(verifier.REASON_MISSING_EVIDENCE, node["verification"]["pairs"][0]["reasons"])

    def test_stage_emits_verification_stats(self):
        events = []
        context = self._context()
        context["_emit_stage_event"] = lambda kind, payload: events.append((kind, payload))
        self.handlers["conflict_review"](context)
        payloads = [payload for kind, payload in events if kind == "stage_progress" and "verification" in payload]
        self.assertTrue(payloads, "核验统计没有进阶段事件（用户看不到核验结论）")
        self.assertIn("核验", payloads[0]["message"])


class LogicValidationWiringTests(_PipelineBase):
    def test_logic_reports_verification_without_changing_status(self):
        kept, _audit = self._apply([_evidence("article:1")])
        result = qa_pipeline._logic_validation(
            "为什么宁德时代股价大涨？", {"evidence": kept}, {"category": {"key": "causal"}})
        self.assertEqual(result["status"], "passed", "核验分布只报告，不许改逻辑校验状态")
        self.assertEqual(result["verification"]["checked"], 1)
        self.assertEqual(result["verification"]["supported"], 1)
        self.assertEqual(result["verification"]["not_verified"], 0)

    def test_unverified_evidence_is_reported_to_composer(self):
        plain = _evidence("article:1")  # 没经过核验的证据（没有 evidence_layer）
        result = qa_pipeline._logic_validation(
            "某问题", {"evidence": [plain]}, {"category": {"key": "fact_check"}})
        self.assertEqual(result["verification"]["checked"], 0)
        self.assertEqual(result["verification"]["not_verified"], 1)
        self.assertIn("不得当作已确证事实", result["verification"]["note"])

    def test_synthesis_prompt_carries_verification_block(self):
        kept, _audit = self._apply([_evidence("article:1")])
        logic = qa_pipeline._logic_validation(
            "为什么宁德时代股价大涨？", {"evidence": kept}, {"category": {"key": "causal"}})
        blocks = qa_pipeline._verification_prompt_blocks(
            {"outputs": {"logic_validation": logic}})
        self.assertIn("证据核验", blocks)
        self.assertIn("确认支持", blocks["证据核验"])
        self.assertEqual(blocks["证据核验"]["已核验"], 1)
        self.assertTrue(blocks["证据核验"]["要求"])

    def test_synthesis_prompt_reports_claim_verification(self):
        graph = {"verification": {"stats": {"claims": 3, "confirmed": 1, "qualified": 1,
                                            "conflicted": 0, "insufficient_evidence": 1,
                                            "unsupported_claim_rate": 0.6667}}}
        blocks = qa_pipeline._verification_prompt_blocks({"outputs": {"conflict_review": graph}})
        self.assertIn("结论核验", blocks)
        self.assertEqual(blocks["结论核验"]["无支持证据占比"], 0.6667)

    def test_question_for_synthesis_really_carries_the_blocks(self):
        """闭包级验收：`_question_for_synthesis` 真的把核验块拼进了生成端问题。"""
        kept, _audit = self._apply([_evidence("article:1")])
        logic = qa_pipeline._logic_validation(
            "为什么宁德时代股价大涨？", {"evidence": kept}, {"category": {"key": "causal"}})
        captured = {}

        def _spy(question, level1, level2=None, *, plan=None):
            captured["question"] = question
            return dict(level1), dict(level2 or {}), {}

        context = {
            "request": {"question": QUESTION, "industry_pack_id": "auto", "mode": "standard"},
            "run": {"id": "run-syn", **SCOPE},
            "outputs": {
                "plan": {"standalone_question": QUESTION, "entities": ["宁德时代"],
                         "question_plan": {"question_count": 1}},
                "logic_validation": logic,
                "level1_draft": {"claims": [], "evidence": []},
            },
        }
        handlers = qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=_FakeRetriever([]), web_search=_FakeWebSearch(),
            store=self.store)
        with mock.patch.object(qa_pipeline, "normalize_policy_claims", side_effect=_spy):
            handlers["conflict_review"](context)
        self.assertIn("证据核验", captured.get("question", ""),
                      "生成端问题里没有核验块：模型会把未核验内容当已确证事实")


class FullStageWiringTests(_PipelineBase):
    """level1_retrieval 阶段级：stats 里有核验回执、库里 seen 身份正确。"""

    def setUp(self):
        super().setUp()
        self.retriever = _FakeRetriever([
            _evidence("article:1", content="10月9日，宁德时代股价大涨5.2%，因为固态电池量产消息。"),
            _evidence("article:2", source_url="https://other.com/2",
                      content="比亚迪销量创新高，芯片供应紧张，行业整体承压。"),
        ])
        self.handlers = qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=self.retriever, web_search=_FakeWebSearch(),
            store=self.store)

    def _context(self, run_id="run-stage"):
        return {
            "request": {"question": QUESTION, "industry_pack_id": "auto",
                        "mode": "standard", "page_context": {}},
            "run": {"id": run_id, **SCOPE},
            "outputs": {"plan": {"question": QUESTION, "standalone_question": QUESTION,
                                 "queries": ["宁德时代 股价"], "entities": ["宁德时代"],
                                 "needs_local_articles": True, "needs_web": False,
                                 "decomposition": {}}},
        }

    def test_stage_stats_expose_verification(self):
        result = self.handlers["level1_retrieval"](self._context())
        stats = result["stats"]
        self.assertEqual(set(stats["evidence_layer"].keys()), EVIDENCE_RECEIPT_KEYS)
        self.assertEqual(stats["verification"]["verifier"], verifier.VERIFIER_VERSION)
        self.assertGreaterEqual(stats["verification"]["checked"], 1)
        self.assertEqual(result["evidence"][0]["evidence_ref"], "article:1", "高分证据应排前面")
        for item in result["evidence"]:
            self.assertIn("verification", item["metadata"]["evidence_layer"])

    def test_stage_disabled_matches_phase02_shape(self):
        os.environ["QA_VERIFIER_ENABLED"] = "0"
        try:
            result = self.handlers["level1_retrieval"](self._context("run-off"))
        finally:
            os.environ.pop("QA_VERIFIER_ENABLED", None)
        self.assertNotIn("verification", result["stats"])
        self.assertEqual(set(result["stats"]["evidence_layer"].keys()), EVIDENCE_RECEIPT_KEYS)
        for item in result["evidence"]:
            self.assertNotIn("verification", item["metadata"]["evidence_layer"])


if __name__ == "__main__":
    unittest.main()

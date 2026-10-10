#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 08 · 管线接线用例（`QA_CONTEXT_PACK` 默认关）。

全部在**隔离临时 sqlite** 上真跑（`setUp` 断言 `backend == 'sqlite'`），零模型/零嵌入端点。
钉住：
  1. **默认关 = 零行为变化**：`conflict_review` 返回的图键集与接线前逐字相同，没有 `context_pack`；
  2. **打开**：`graph["context_pack"]` 出现且过契约；回执（`receipt`）带预算/引用/上下文缺口；
  3. **免费持久化**：上下文包连同 selection trace 真的落进 `qa_stage_runs.details_json`
     （Phase 06 同样的手法，零新表零迁移）；
  4. **生成端真的拿到包**：`synthesis` 把 `context_pack` 传给综合器（有断言）；
  5. **fast 路径也组装**（没有 conflict_review 时在 synthesis 里补建，口径一致）；
  6. 失败路径：组装抛错只记账（`error`），绝不打断建图与出答案。
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

import qa_context_pack as cp  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_pipeline  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

QUESTION = "香港家族办公室税收优惠政策对内地高净值客户有什么影响？"
SCOPE = {"owner_user_id": "p08", "session_id": "s1", "industry_pack_id": "auto"}
GRAPH_KEYS = {"version", "claims", "evidence", "edges", "conflicts", "verification",
              "normalization_audit", "stats"}
FLAGS = ("QA_CONTEXT_PACK", "QA_GROUNDING_GATE", "QA_EVIDENCE_GRAPH", "QA_GAP_ANALYZER")


def _evidence(ref, *, authority=60, relation="supports"):
    return {
        "evidence_ref": ref, "source_type": "article", "title": "香港家族办公室税收优惠政策 %s" % ref,
        "source_url": "https://example.com/%s" % ref, "article_id": int(str(ref).split(":")[-1]),
        "content_excerpt": "香港家族办公室税收优惠政策对符合条件的管理人给予利得税宽免。",
        "published_at": "2026-10-09", "authority_level": authority, "score": 30.0,
        "retrieval_method": "keyword", "match_reason": "标题命中：家族办公室",
        "relationship": relation, "metadata": {"matched_keywords": ["家族办公室"]},
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


class _Base(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "phase08.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.assertEqual(self.db.backend, "sqlite", "测试必须跑在隔离 sqlite 上")
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self.run = self.store.create_run(
            {"industry_pack_id": "auto", "question": QUESTION, "mode": "standard"},
            owner_user_id="p08", idempotency_key="phase08-pipeline")
        self.run_id = str(self.run["id"])
        self.saved = {name: os.environ.pop(name, None) for name in FLAGS}
        self.handlers = qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=_FakeRetriever([_evidence("article:1")]),
            web_search=_FakeWebSearch(), store=self.store)

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def _context(self, *, level1_retrieval=None, evidence=None):
        items = evidence if evidence is not None else [_evidence("article:1")]
        outputs = {
            "plan": {"question": QUESTION, "standalone_question": QUESTION,
                     "queries": ["香港家族办公室 税收优惠"], "entities": ["家族办公室"],
                     "decomposition": {"is_multi_hop": False, "pattern": "single", "hop_count": 1,
                                       "hops": [{"id": "h1", "question": QUESTION,
                                                 "depends_on": []}]}},
            "level1_draft": {
                "contract_version": "unified-qa-v1", "draft_answer": "草稿",
                "claims": [{
                    "claim_id": "c1",
                    "text": "香港家族办公室税收优惠政策对内地高净值客户有影响",
                    "claim_type": "current_fact", "confidence": 0.82,
                    "valid_from": "2026-10-09", "valid_to": None, "scope": ["家族办公室"],
                    "evidence_refs": ["article:1"], "needs_verification": True,
                    "verification_status": "unverified"}],
                "gaps": [], "evidence": [dict(item) for item in items]},
        }
        outputs["level1_retrieval"] = level1_retrieval or {
            "evidence": [dict(item) for item in items], "stats": {"adopted": len(items)}}
        return {"request": {"question": QUESTION, "industry_pack_id": "auto", "mode": "standard"},
                "run": {"id": self.run_id, **SCOPE}, "outputs": outputs}


class DefaultOffTests(_Base):
    def test_flag_off_keeps_the_graph_untouched(self):
        graph_res = self.handlers["conflict_review"](self._context())
        self.assertEqual(set(graph_res.keys()), GRAPH_KEYS, "开关没开就不许新增键（回滚口径）")
        self.assertNotIn("context_pack", graph_res)

    def test_flag_off_keeps_the_synthesis_prompt_unchanged(self):
        """默认关：生成端拿到的 `context_pack` 必须是 None（提示逐字回到接线前）。"""
        captured = {}

        def _fake_generate(**kwargs):
            captured.update(kwargs)
            return {"contract_version": "unified-qa-v1", "status": "ready",
                    "answer": "答案", "sections": {}, "claims": [], "conflicts": [],
                    "evidence": [], "citations": [], "citation_map": {}, "cutoff_at": "",
                    "degraded": False, "degradation_reasons": [], "models": {}}

        handlers = qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=_FakeRetriever([_evidence("article:1")]),
            web_search=_FakeWebSearch(), store=self.store,
            provider_registry=_StubRegistry(), feature_flags=_StubFlags(),
            final_synthesizer=mock.Mock(generate=_fake_generate))
        context = self._context()
        context["outputs"]["plan"] = {"question": QUESTION, "standalone_question": QUESTION,
                                      "queries": [QUESTION], "entities": ["家族办公室"]}
        handlers["synthesis"](context)
        self.assertIn("context_pack", captured)
        self.assertIsNone(captured["context_pack"])


class FlagOnTests(_Base):
    def test_flag_on_adds_only_a_sibling_key(self):
        os.environ["QA_CONTEXT_PACK"] = "1"
        graph_res = self.handlers["conflict_review"](self._context())
        self.assertEqual(set(graph_res.keys()), GRAPH_KEYS | {"context_pack"},
                         "只许新增一个兄弟键")
        pack = graph_res["context_pack"]
        ok, note = validate("context_pack", pack)
        self.assertTrue(ok, note)
        self.assertEqual(pack["pack_version"], contracts.CONTEXT_PACK_VERSION)
        self.assertTrue(pack["citation_map"])
        self.assertIn("receipt", pack)
        self.assertEqual(pack["stats"]["retrieval_requested"], 0)

    def test_receipt_exposes_budget_and_gaps(self):
        os.environ["QA_CONTEXT_PACK"] = "1"
        pack = self.handlers["conflict_review"](self._context())["context_pack"]
        receipt = pack["receipt"]
        self.assertEqual(receipt["budget"]["estimator"], cp.ESTIMATOR_VERSION)
        self.assertGreater(receipt["budget"]["estimated_tokens_before"], 0)
        self.assertLessEqual(receipt["budget"]["estimated_tokens_after"],
                             receipt["budget"]["total"])
        self.assertIn("trim_reasons", receipt["budget"])
        self.assertTrue(receipt["sections"]["evidence_context"] >= 1)
        self.assertEqual(receipt["retrieval_requested"], 0)

    def test_pack_is_persisted_in_stage_details_without_a_new_table(self):
        """上下文包随阶段输出落进既有 `qa_stage_runs.details_json`（零新表、零迁移）。"""
        os.environ["QA_CONTEXT_PACK"] = "1"
        graph_res = self.handlers["conflict_review"](self._context())
        self.store.record_stage(self.run_id, "conflict_review", status="completed",
                                details={"result": graph_res})
        rows = self.db.connection.execute(
            "SELECT details_json FROM qa_stage_runs WHERE run_id=? AND stage='conflict_review'",
            (self.run_id,)).fetchall()
        self.assertEqual(len(rows), 1)
        details = json.loads(rows[0][0])
        persisted = (details.get("result") or {}).get("context_pack")
        self.assertTrue(persisted, "上下文包必须随阶段输出免费持久化（零迁移）")
        self.assertEqual(persisted["pack_id"], graph_res["context_pack"]["pack_id"])
        self.assertEqual(len(persisted["selection_trace"]),
                         len(graph_res["context_pack"]["selection_trace"]))
        self.assertEqual(persisted["citation_map"], graph_res["context_pack"]["citation_map"])
        # 零新表：库表清单里不许出现上下文相关的表
        tables = {str(row[0]) for row in self.db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertFalse([name for name in tables if name.startswith("qa_context")], tables)

    def test_receipt_is_emitted_to_the_stage_event(self):
        from qa_pipeline import _build_context_pack_layer

        os.environ["QA_CONTEXT_PACK"] = "1"
        pack = self.handlers["conflict_review"](self._context())["context_pack"]
        self.assertIn("pack_id", pack["receipt"])
        self.assertTrue(pack["receipt"]["budget"]["estimator"])
        self.assertTrue(callable(_build_context_pack_layer))

    def test_working_memory_comes_from_existing_receipts(self):
        os.environ["QA_CONTEXT_PACK"] = "1"
        context = self._context(level1_retrieval={
            "evidence": [_evidence("article:1")], "stats": {
                "adopted": 1, "verification": {"checked": 3, "supported": 2},
                "gap_loop": {"stop_reason": "ANSWERABLE"}}})
        pack = self.handlers["conflict_review"](context)["context_pack"]
        texts = [item["text"] for item in pack["items"] if item["kind"] == "working_memory"]
        self.assertTrue(any("已取到证据" in text for text in texts), texts)
        self.assertTrue(any("ANSWERABLE" in text for text in texts), texts)


class FailurePathTests(_Base):
    def test_pack_failure_does_not_break_the_graph(self):
        os.environ["QA_CONTEXT_PACK"] = "1"
        with mock.patch("qa_context_pack.build_context_pack",
                        side_effect=RuntimeError("boom")):
            graph_res = self.handlers["conflict_review"](self._context())
        self.assertEqual(set(graph_res.keys()), GRAPH_KEYS | {"context_pack"})
        self.assertIn("error", graph_res["context_pack"])
        self.assertIn("boom", graph_res["context_pack"]["error"])
        self.assertTrue(graph_res["claims"], "组装失败绝不能影响建图")

    def test_stage_handler_helper_reports_errors_instead_of_raising(self):
        from qa_pipeline import _build_context_pack_layer

        with mock.patch("qa_context_pack.build_context_pack",
                        side_effect=ValueError("bad input")):
            layer = _build_context_pack_layer({}, plan={}, request={}, run_meta={"id": "r"})
        self.assertEqual(layer["pack_version"], contracts.CONTEXT_PACK_VERSION)
        self.assertTrue(str(layer["error"]).startswith("ValueError"))


class FastPathTests(_Base):
    def test_synthesis_builds_the_pack_when_conflict_review_is_absent(self):
        """fast 模式不跑 conflict_review：synthesis 里补建，口径一致，并且真的传给生成端。"""
        os.environ["QA_CONTEXT_PACK"] = "1"
        captured = {}

        def _fake_generate(**kwargs):
            captured.update(kwargs)
            return {"contract_version": "unified-qa-v1", "status": "ready",
                    "answer": "答案 [1]", "sections": {}, "claims": [], "conflicts": [],
                    "evidence": [], "citations": [], "citation_map": {},
                    "cutoff_at": "", "degraded": False, "degradation_reasons": [],
                    "models": {}}

        handlers = qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=_FakeRetriever([_evidence("article:1")]),
            web_search=_FakeWebSearch(), store=self.store,
            provider_registry=_StubRegistry(), feature_flags=_StubFlags(),
            final_synthesizer=mock.Mock(generate=_fake_generate))
        context = self._context()
        context["request"]["mode"] = "fast"
        context["outputs"]["plan"] = {"question": QUESTION, "standalone_question": QUESTION,
                                      "queries": [QUESTION], "entities": ["家族办公室"]}
        handlers["synthesis"](context)
        pack = captured.get("context_pack")
        self.assertIsNotNone(pack, "fast 路径也必须组装上下文包并传给生成端")
        ok, note = validate("context_pack", pack)
        self.assertTrue(ok, note)
        self.assertTrue(pack["citation_map"], "fast 路径的包同样要有可回溯引用")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 10 · 管线接线用例（`QA_MEMORY_REVALIDATION` 默认关）。

全部在**隔离临时 sqlite** 上真跑（`setUp` 断言 `backend == 'sqlite'`），零模型/零嵌入端点。
钉住：
  1. **默认关 = 零行为变化**：`conflict_review` 的图键集与 Phase 09 之后逐字相同
     （没有 `memory_revalidation` 键）、`memory_validation` / `memory_contradiction` 一张行都不写；
  2. **打开**：只新增兄弟键 `graph["memory_revalidation"]`，回执过契约，且只在**召回命中**上跑
     （不发明检索：候选证据只来自本轮证据图）；
  3. 复验把命中改写成"本轮已复验"，`memory_context` 段的 grounding 如实反映，
     但**记忆正文仍不是证据**（`verified_scope=evidence_refs`，MASTER_RULES 11）；
  4. 两个开关互相独立：关掉记忆图开关时，复验开关不产生任何键与库行；
  5. 失败路径：复验层抛错只记账（`error`），证据图与答案照旧。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_graph_contracts as contracts  # noqa: E402
import qa_memory as memory  # noqa: E402
import qa_pipeline  # noqa: E402
import qa_phase09_fixtures as fx09  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

import qa_phase10_fixtures as fx  # noqa: E402

QUESTION = fx09.QUESTION
SCOPE = dict(fx09.SCOPE)
GRAPH_KEYS = {"version", "claims", "evidence", "edges", "conflicts", "verification",
              "normalization_audit", "stats"}
FLAGS = ("QA_MEMORY_GRAPH", "QA_MEMORY_REVALIDATION", "QA_CONTEXT_PACK", "QA_GROUNDING_GATE",
         "QA_EVIDENCE_GRAPH", "QA_GAP_ANALYZER")

FINAL_ANSWER = {"contract_version": "unified-qa-v1", "status": "ready", "answer": "答案 [1]",
                "sections": {}, "claims": [], "conflicts": [], "evidence": [], "citations": [],
                "citation_map": {}, "cutoff_at": "", "degraded": False,
                "degradation_reasons": [], "models": {}}


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
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "phase10.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.assertEqual(self.db.backend, "sqlite", "测试必须跑在隔离 sqlite 上")
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self.run = self.store.create_run(
            {"industry_pack_id": "auto", "question": QUESTION, "mode": "standard"},
            owner_user_id="p10", idempotency_key="phase10-pipeline")
        self.run_id = str(self.run["id"])
        self.saved = {name: os.environ.pop(name, None) for name in FLAGS}
        self.handlers = qa_pipeline.build_qa_stage_handlers(
            database=self.db,
            article_retriever=_FakeRetriever([fx09.verified_evidence("article:1")]),
            web_search=_FakeWebSearch(), store=self.store)

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        try:
            self.db.connection.close()
        except Exception:  # noqa: BLE001
            pass
        self.temp_dir.cleanup()

    def _context(self):
        evidence = [fx09.verified_evidence("article:1")]
        outputs = {
            "plan": {"question": QUESTION, "standalone_question": QUESTION,
                     "queries": ["香港家族办公室 税收优惠"], "entities": ["家族办公室"],
                     "decomposition": {"is_multi_hop": False, "pattern": "single",
                                       "hop_count": 1,
                                       "hops": [{"id": "h1", "question": QUESTION,
                                                 "depends_on": []}]}},
            "level1_draft": {"contract_version": "unified-qa-v1", "draft_answer": "草稿",
                             "claims": [{
                                 "claim_id": "c1", "text": fx09.CLAIM_TEXT,
                                 "claim_type": "policy", "confidence": 0.82,
                                 "valid_from": "2026-04-01", "valid_to": None,
                                 "scope": ["家族办公室"], "evidence_refs": ["article:1"],
                                 "needs_verification": True,
                                 "verification_status": "unverified"}],
                             "gaps": [], "evidence": evidence},
            "level1_retrieval": {"evidence": evidence, "stats": {"adopted": 1}},
        }
        return {"request": {"question": QUESTION, "industry_pack_id": "auto",
                            "mode": "standard"},
                "run": {"id": self.run_id, **SCOPE}, "outputs": outputs}

    def _handlers_with_synthesizer(self, captured=None):
        def _fake_generate(**kwargs):
            if captured is not None:
                captured.update(kwargs)
            return dict(FINAL_ANSWER)

        return qa_pipeline.build_qa_stage_handlers(
            database=self.db,
            article_retriever=_FakeRetriever([fx09.verified_evidence("article:1")]),
            web_search=_FakeWebSearch(), store=self.store,
            provider_registry=_StubRegistry(), feature_flags=_StubFlags(),
            final_synthesizer=mock.Mock(generate=_fake_generate))

    def _rows(self, table):
        return self.db.connection.execute("SELECT * FROM %s" % table).fetchall()

    def _seed_memory(self, *, freshness="VERSION_SENSITIVE", claim_type="policy",
                     content=fx09.CLAIM_TEXT, memory_id="MEM-left"):
        """落一条**需要复验**的记忆（并绑定 article:1 的已验证证据）。"""
        item = fx.memory_item(content, freshness=freshness, claim_type=claim_type,
                              memory_id=memory_id,
                              source_evidence_ids=["article:1"], evidence_refs=["article:1"])
        self.store.save_memory_item(item)
        evidence = fx09.verified_evidence("article:1")
        self.store.link_memory_evidence(memory_id, [memory.evidence_link_row(
            evidence, {"verdict": "SUPPORTED", "evidence_score": 0.8}, run_id=self.run_id,
            stage="level1_retrieval", corpus_version="corpus-p10")])
        return memory_id


class DefaultOffTests(_Base):
    def test_flag_off_keeps_the_graph_untouched(self):
        graph = self.handlers["conflict_review"](self._context())
        self.assertEqual(set(graph.keys()), GRAPH_KEYS, "开关没开就不许新增键（回滚口径）")
        self.assertNotIn("memory_revalidation", graph)

    def test_flag_off_writes_no_revalidation_rows(self):
        os.environ["QA_MEMORY_GRAPH"] = "1"
        self._seed_memory()
        graph = self.handlers["conflict_review"](self._context())
        self.assertIn("memory", graph)
        self.assertNotIn("memory_revalidation", graph)
        for table in ("memory_validation", "memory_contradiction"):
            self.assertEqual(len(self._rows(table)), 0, "%s 不该有行" % table)
        self.assertEqual(len(self._rows("memory_recall_log")), 1, "召回仍然照常留痕（P09 行为）")


class RevalidationWiringTests(_Base):
    def _with_flags(self):
        os.environ["QA_MEMORY_GRAPH"] = "1"
        os.environ["QA_MEMORY_REVALIDATION"] = "1"

    def test_flag_on_adds_only_a_sibling_key_with_a_valid_receipt(self):
        self._with_flags()
        self._seed_memory()
        graph = self.handlers["conflict_review"](self._context())
        self.assertEqual(set(graph.keys()), GRAPH_KEYS | {"memory", "memory_revalidation"},
                         "只许新增兄弟键")
        layer = graph["memory_revalidation"]
        ok, note = validate("memory_revalidation_report", {key: layer[key] for key in (
            "revalidation_version", "checked", "gate_decisions", "outcomes")})
        self.assertTrue(ok, note)
        self.assertEqual(layer["gate_version"], contracts.MEMORY_FRESHNESS_GATE_VERSION)
        self.assertIn("receipt", layer)
        self.assertEqual(layer["receipt"]["revalidated"], layer["revalidated"])

    def test_revalidation_runs_on_recalled_hints_and_persists(self):
        self._with_flags()
        memory_id = self._seed_memory()
        graph = self.handlers["conflict_review"](self._context())
        layer = graph["memory_revalidation"]
        hints = [hit["memory_id"] for hit in graph["memory"]["hits"]]
        self.assertIn(memory_id, hints, "这条记忆应当被召回（否则复验没有对象）")
        self.assertEqual(layer["checked"], len(hints))
        rows = self._rows("memory_validation")
        self.assertEqual(len(rows), len(hints))
        outcomes = {str(row["outcome"]) for row in rows}
        self.assertTrue(outcomes <= set(contracts.MEMORY_REVALIDATION_OUTCOMES))
        self.assertEqual(layer["revalidated"], sum(1 for row in rows if row["verified"]))

    def test_evidence_comes_from_the_current_graph_only(self):
        """零新增检索：候选证据只可能来自本轮证据图里的 article:1。"""
        self._with_flags()
        self._seed_memory()
        graph = self.handlers["conflict_review"](self._context())
        layer = graph["memory_revalidation"]
        self.assertGreater(layer["checked"], 0)
        for row in layer["validations"]:
            for ref in row["evidence_refs"]:
                self.assertEqual(ref, "article:1")
            self.assertLessEqual(row["candidates"], len(graph["evidence"]),
                                 "候选数不许超过本轮证据图里的证据数（没有新增检索）")

    def test_context_item_reflects_revalidation_without_claiming_evidence(self):
        self._with_flags()
        os.environ["QA_CONTEXT_PACK"] = "1"
        self._seed_memory(freshness="VERY_SHORT")
        graph = self.handlers["conflict_review"](self._context())
        section = graph["context_pack"]["sections"]["memory_context"]
        self.assertGreater(section["count"], 0)
        item = graph["context_pack"]["items"][
            [index for index, row in enumerate(graph["context_pack"]["items"])
             if row.get("section") == "memory_context"][0]]
        grounding = item["grounding"]
        self.assertTrue(grounding["hint"])
        if grounding["verified_evidence"]:
            self.assertEqual(grounding["verified_scope"], "evidence_refs")
            self.assertFalse(grounding["requires_revalidation"])
            self.assertIn("正文仍不是证据", grounding["reason"])
        self.assertNotIn("citation", item["metadata"].get("role", "memory_hint"))

    def test_flag_on_without_recall_hits_writes_nothing(self):
        self._with_flags()
        graph = self.handlers["conflict_review"](self._context())
        layer = graph["memory_revalidation"]
        self.assertEqual(layer["checked"], 0)
        self.assertEqual(len(self._rows("memory_validation")), 0)

    def test_revalidation_flag_alone_does_nothing(self):
        os.environ["QA_MEMORY_REVALIDATION"] = "1"      # 记忆图开关仍然关
        self._seed_memory()
        graph = self.handlers["conflict_review"](self._context())
        self.assertEqual(set(graph.keys()), GRAPH_KEYS)
        self.assertEqual(len(self._rows("memory_validation")), 0)


class FailurePathTests(_Base):
    def test_revalidation_error_is_recorded_and_does_not_break_the_graph(self):
        os.environ["QA_MEMORY_GRAPH"] = "1"
        os.environ["QA_MEMORY_REVALIDATION"] = "1"
        self._seed_memory()
        with mock.patch("qa_memory_revalidation.run_revalidation",
                        side_effect=RuntimeError("boom")):
            graph = self.handlers["conflict_review"](self._context())
        layer = graph["memory_revalidation"]
        self.assertIn("boom", layer["error"])
        self.assertEqual(layer["checked"], 0)
        self.assertEqual(set(graph.keys()), GRAPH_KEYS | {"memory", "memory_revalidation"},
                         "复验失败不许把证据图打断")

    def test_answer_still_generated_when_revalidation_explodes(self):
        os.environ["QA_MEMORY_GRAPH"] = "1"
        os.environ["QA_MEMORY_REVALIDATION"] = "1"
        self._seed_memory()
        captured = {}
        handlers = self._handlers_with_synthesizer(captured)
        with mock.patch("qa_memory_revalidation.revalidate_item",
                        side_effect=RuntimeError("boom")):
            result = handlers["synthesis"](self._context())
        self.assertEqual(result["answer"], FINAL_ANSWER["answer"])
        self.assertEqual(len(self._rows("memory_validation")), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

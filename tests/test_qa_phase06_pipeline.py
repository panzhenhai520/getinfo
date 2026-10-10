#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 06 · 管线接线用例（`QA_EVIDENCE_GRAPH` 默认关）。

要点（全部在隔离临时 sqlite 上真跑，不调 LLM/嵌入端点）：
  1. **默认关 = 零行为变化**：`conflict_review` 返回键集与"没有证据图层"逐字一致，
     库里的 claim/边/冲突行数也一样；
  2. **打开**：`graph["evidence_graph"]` 出现（**兄弟键**，claims/edges/conflicts 结构不动），
     过 `evidence_graph` 契约；矛盾裁决只回写冻结 `CONFLICT_SCHEMA` 允许的三个字段；
  3. **最终答案仍然合法**：裁决回写后走 `fallback_final_answer` + `validate_final_answer`
     必须通过（这是"没放宽冻结 schema"的端到端证明）；
  4. **阶段输出整体落库**：编排层把阶段输出写进 `qa_stage_runs.details_json`，
     所以证据图回执是免费持久化的（仓储 `load_persisted` 能读回）；
  5. 失败路径：建层抛错 → 回执里写明原因，图照旧落库、阶段不失败。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_contracts  # noqa: E402
import qa_pipeline  # noqa: E402
import qa_synthesis  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

SCOPE = {"owner_user_id": "p06", "session_id": "s1", "industry_pack_id": "auto"}
QUESTION = "A股10月9日大涨的原因是什么？"
GRAPH_KEYS = {"version", "claims", "evidence", "edges", "conflicts", "verification",
              "normalization_audit", "stats"}


def _evidence(ref, *, relation="supports", authority=60, content=None, published="2026-10-09"):
    return {
        "evidence_ref": ref, "source_type": "article", "title": "A股10月9日大涨的原因 %s" % ref,
        "source_url": "https://example.com/%s" % ref, "article_id": int(str(ref).split(":")[-1]),
        "content_excerpt": content or "10月9日，A股大涨3.84%，固态电池量产与储能订单增长是主因。",
        "published_at": published, "authority_level": authority, "score": 30.0,
        "retrieval_method": "keyword", "match_reason": "标题命中：A股",
        "relationship": relation, "metadata": {"matched_keywords": ["A股"]},
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


class _Base(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "phase06.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.assertEqual(self.db.backend, "sqlite", "测试必须跑在隔离 sqlite 上")
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self.run = self.store.create_run(
            {"industry_pack_id": "auto", "question": QUESTION, "mode": "standard"},
            owner_user_id="p06", idempotency_key="phase06-pipeline")
        self.run_id = str(self.run["id"])
        self.saved = os.environ.pop("QA_EVIDENCE_GRAPH", None)
        self.handlers = qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=_FakeRetriever([_evidence("article:1")]),
            web_search=_FakeWebSearch(), store=self.store)

    def tearDown(self):
        os.environ.pop("QA_EVIDENCE_GRAPH", None)
        if self.saved is not None:
            os.environ["QA_EVIDENCE_GRAPH"] = self.saved
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def _row(self, sql, params=()):
        return self.db.connection.execute(sql, params).fetchone()

    def _context(self, *, refs=("article:1",), evidence_items=None, claim_text=None):
        items = evidence_items if evidence_items is not None else [_evidence("article:1")]
        return {
            "request": {"question": QUESTION, "industry_pack_id": "auto", "mode": "standard"},
            "run": {"id": self.run_id, **SCOPE},
            "outputs": {
                "plan": {"question": QUESTION, "standalone_question": QUESTION,
                         "queries": ["A股 大涨"], "entities": ["A股"],
                         "decomposition": {"is_multi_hop": False, "pattern": "single",
                                           "hop_count": 1,
                                           "hops": [{"id": "h1", "question": QUESTION,
                                                     "depends_on": []}]}},
                "level1_draft": {
                    "contract_version": "unified-qa-v1", "draft_answer": "草稿",
                    "claims": [{
                        "claim_id": "c1",
                        "text": claim_text or "A股10月9日大涨3.84%，固态电池与储能订单是主因",
                        "claim_type": "current_fact", "confidence": 0.82,
                        "valid_from": "2026-10-09", "valid_to": None, "scope": ["A股"],
                        "evidence_refs": list(refs), "needs_verification": True,
                        "verification_status": "confirmed"}],
                    "entities": ["A股"], "timeline_hints": [], "gaps": [], "followup_queries": [],
                    "evidence": items, "citations": [item["evidence_ref"] for item in items],
                },
            },
        }

    def _counts(self):
        return tuple(int(self._row("SELECT count(*) FROM %s" % table)[0])
                     for table in ("qa_claims", "qa_claim_evidence", "qa_conflicts"))


class DefaultOffTests(_Base):
    def test_flag_off_keeps_the_stage_untouched(self):
        graph = self.handlers["conflict_review"](self._context())
        self.assertEqual(set(graph.keys()), GRAPH_KEYS,
                         "开关没开就不许新增键（回滚口径）")
        self.assertNotIn("evidence_graph", graph)
        self.assertEqual(self._counts(), (1, 1, 0))

    def test_flag_on_adds_only_a_sibling_key(self):
        os.environ["QA_EVIDENCE_GRAPH"] = "1"
        graph = self.handlers["conflict_review"](self._context())
        self.assertEqual(set(graph.keys()), GRAPH_KEYS | {"evidence_graph"})
        layer = graph["evidence_graph"]
        ok, note = validate("evidence_graph", layer)
        self.assertTrue(ok, note)
        self.assertEqual(layer["coverage"]["total_claims"], 1)
        self.assertEqual(layer["graph_version"], qa_evidence_graph_version())

    def test_receipt_is_emitted_to_the_stage_event(self):
        os.environ["QA_EVIDENCE_GRAPH"] = "1"
        events = []
        context = self._context()
        context["_emit_stage_event"] = lambda kind, payload: events.append((kind, payload))
        self.handlers["conflict_review"](context)
        payload = [item for kind, item in events
                   if kind == "stage_progress" and "evidence_graph" in item][0]
        self.assertIn("relation_distribution", payload["evidence_graph"])
        self.assertIn("coverage_definition", payload["claim_coverage"])


def qa_evidence_graph_version():
    import qa_evidence_graph

    return qa_evidence_graph.__dict__["EVIDENCE_GRAPH_VERSION"]


class ConflictRewriteTests(_Base):
    def _two_claim_context(self):
        """两条互相冲突的结论（一条权威高、一条低）→ 走 claim_conflict 裁决 + 回写。

        文本必须高度相似且只有否定差异（`qa_reasoning._conflict_type` 的口径：
        相似度 >= 0.28 且否定不一致才算 real_conflict），否则检不出冲突。
        """
        items = [_evidence("article:1", authority=100), _evidence("article:2", authority=20)]
        context = self._context(refs=("article:1",), evidence_items=items)
        context["outputs"]["level1_draft"]["claims"][0]["text"] = "A股10月9日大涨3.84%"
        level1 = context["outputs"]["level1_draft"]
        level1["claims"].append({
            "claim_id": "c2", "text": "A股10月9日没有大涨3.84%",
            "claim_type": "current_fact", "confidence": 0.6, "valid_from": "2026-10-09",
            "valid_to": None, "scope": ["A股"], "evidence_refs": ["article:2"],
            "needs_verification": True, "verification_status": "unverified"})
        return context

    def test_conflicts_are_rewritten_within_the_frozen_schema(self):
        os.environ["QA_EVIDENCE_GRAPH"] = "1"
        graph = self.handlers["conflict_review"](self._two_claim_context())
        self.assertTrue(graph["conflicts"], "两条相反结论必须被检出冲突")
        for conflict in graph["conflicts"]:
            self.assertEqual(set(conflict.keys()),
                             set(qa_contracts.CONFLICT_SCHEMA["properties"].keys()),
                             "回写不许新增键")
            self.assertIn(conflict["resolution"], ("resolved", "unresolved"))
            qa_contracts._validate(qa_contracts.CONFLICT_SCHEMA, conflict, "冲突")
        # 理由码与理由文本都记在图里（可复算）
        decisions = graph["evidence_graph"]["contradictions"]
        self.assertTrue(decisions)
        for item in decisions:
            self.assertIn(item["reason_code"],
                          qa_graph_contracts_codes())
            ok, note = validate("contradiction_decision", dict(item))
            self.assertTrue(ok, note)
        legacy = [item for item in decisions if item["kind"] == "claim_conflict"]
        self.assertTrue(legacy, "两条结论的冲突必须走 claim_conflict 裁决")

    def test_final_answer_still_validates_after_the_rewrite(self):
        os.environ["QA_EVIDENCE_GRAPH"] = "1"
        context = self._two_claim_context()
        graph = self.handlers["conflict_review"](context)
        answer = qa_synthesis.fallback_final_answer(
            graph=graph, level1=context["outputs"]["level1_draft"], level2={}, degradation=[],
            models={"draft": "rule"}, reason="测试", question=QUESTION)
        validated = qa_contracts.validate_final_answer(answer)
        self.assertEqual(len(validated["conflicts"]), len(graph["conflicts"]))

    def test_decisions_land_in_the_persisted_stage_details(self):
        os.environ["QA_EVIDENCE_GRAPH"] = "1"
        graph = self.handlers["conflict_review"](self._two_claim_context())
        self.store.record_stage(self.run_id, "conflict_review", status="completed",
                                details={"result": graph})
        import qa_evidence_graph as eg

        layer = eg.EvidenceGraphRepository(self.store).load_persisted(self.run_id)
        self.assertEqual(layer["graph_version"], graph["evidence_graph"]["graph_version"])
        self.assertEqual(layer["coverage"]["total_claims"],
                         graph["evidence_graph"]["coverage"]["total_claims"])


def qa_graph_contracts_codes():
    import qa_graph_contracts

    return qa_graph_contracts.CONTRADICTION_RESOLUTION_CODES


class FailurePathTests(_Base):
    def test_layer_failure_never_breaks_the_stage(self):
        os.environ["QA_EVIDENCE_GRAPH"] = "1"
        with mock.patch.object(qa_pipeline, "layer_from_graph",
                               side_effect=RuntimeError("graph exploded")):
            graph = self.handlers["conflict_review"](self._context())
        layer = graph["evidence_graph"]
        self.assertIn("error", layer)
        self.assertIn("RuntimeError", layer["error"])
        self.assertEqual(int(self._row("SELECT count(*) FROM qa_claims WHERE run_id=?",
                                       (self.run_id,))[0]), 1, "图照旧落库")

    def test_missing_plan_is_tolerated(self):
        os.environ["QA_EVIDENCE_GRAPH"] = "1"
        context = self._context()
        context["outputs"]["plan"] = None
        graph = self.handlers["conflict_review"](context)
        self.assertEqual(graph["evidence_graph"]["stats"]["plan_claims"], 0)

    def test_empty_evidence_is_honest(self):
        os.environ["QA_EVIDENCE_GRAPH"] = "1"
        context = self._context(refs=(), evidence_items=[])
        graph = self.handlers["conflict_review"](context)
        coverage = graph["evidence_graph"]["coverage"]
        self.assertEqual(coverage["claims_without_evidence"], 1)
        self.assertEqual(coverage["claim_coverage"], 0.0)


if __name__ == "__main__":
    unittest.main()

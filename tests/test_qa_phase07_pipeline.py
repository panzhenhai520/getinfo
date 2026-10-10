#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 07 · 管线接线用例（`QA_GAP_ANALYZER` 默认关）。

全部在**隔离临时 sqlite** 上真跑（`setUp` 断言 `backend == 'sqlite'`），零模型/零嵌入端点。
钉住：
  1. **默认关 = 零行为变化**：`level1_retrieval` 返回键集与 `stats` 键集逐字不变，
     `qa_reasoning_traces` 的缺口三列保持空/0（回滚口径）；
  2. **打开**：`stats["gap_loop"]`（兄弟键）+ `gap_loop_summary` 出现，过 `gap_loop` 契约；
  3. **缺口三列真正落库**：`qa_reasoning_traces` 的 `gap_id` / `new_claims` / `resolved_gap`
     不再恒为空/0（Phase 01 埋的坑，本阶段填上），且同一个 run 的每条跳都有值；
  4. **NO_GAIN 端到端**：每一跳都只捞到同一批证据 → 连续无增益 → 停止原因 NO_GAIN；
  5. **UNRESOLVABLE_CONTRADICTION 端到端**：Phase 06 裁决 unresolved → conflict_review 的
     `gap_review.stop_reason` 就是它（引用真实裁决，不是编的）；
  6. **失败路径**：缺口分析抛错绝不能把检索阶段打断（回执里留痕即可）。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_gap_analyzer as gap  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_pipeline  # noqa: E402
import qa_synthesis  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402
from qa_phase04_corpus import DEFAULT_PACK, make_db, seed_standard_corpus  # noqa: E402
from qa_retrieval import ArticleRetriever  # noqa: E402
from qa_storage import QaStore  # noqa: E402

QUESTION = "香港家族办公室税收优惠政策对内地高净值客户有什么影响？"
SCOPE = {"owner_user_id": "p07", "session_id": "s1", "industry_pack_id": DEFAULT_PACK}
LEVEL1_RECEIPT_KEYS = {
    "queries", "evidence", "excluded", "stats", "graph", "search_status",
    "search_providers", "search_errors", "time_window", "cache",
}
EVIDENCE_RECEIPT_KEYS = {
    "evidence_layer", "annotated", "seen_dropped", "dedupe_dropped", "recorded", "reason",
}
GRAPH_KEYS = {"version", "claims", "evidence", "edges", "conflicts", "verification",
              "normalization_audit", "stats"}
FLAGS = ("QA_GAP_ANALYZER", "QA_EVIDENCE_GRAPH", "QA_NEXT_HOP_PLANNER",
         "QA_GAP_MAX_NEXT_HOPS", "QA_GAP_NO_GAIN_ROUNDS", "QA_EXECUTION_GRAPH")


class _FakeWebSearch:
    def search(self, queries, *, enabled=True, limit=8):
        return {"evidence": [], "status": "disabled", "providers": [], "errors": []}


class _Policy:
    ragflow_kb_id = "kb"
    ragflow_app_id = "app"
    max_evidence = 12
    max_queries_per_hop = 2
    standard_max_hops = 2
    deep_max_hops = 3
    research_timeout_seconds = 30


class _PolicyResolver:
    def resolve(self, industry_pack_id):
        return _Policy()

    def require_research_ready(self, industry_pack_id):
        return _Policy()


class _SameEvidenceRetriever:
    """每一跳都只返回同一批证据（用来构造真实的"连续无增益"）。"""

    def __init__(self, evidence):
        self.evidence = [dict(item) for item in evidence]
        self.calls = 0

    def retrieve(self, plan, **kwargs):
        self.calls += 1
        return {"evidence": [dict(item) for item in self.evidence],
                "stats": {"eligible": len(self.evidence), "adopted": len(self.evidence)}}


class _Base(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = make_db(os.path.join(self.temp_dir.name, "phase07-pipeline.sqlite3"))
        seed_standard_corpus(self.db, pack_id=DEFAULT_PACK)
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self.run = self.store.create_run(
            {"industry_pack_id": DEFAULT_PACK, "question": QUESTION, "mode": "standard"},
            owner_user_id="p07", idempotency_key="phase07-pipeline")
        self.run_id = str(self.run["id"])
        self._saved = {name: os.environ.pop(name, None) for name in FLAGS}

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        try:
            self.db.connection.close()
        except Exception:
            pass
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _handlers(self, retriever=None):
        return qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=retriever or ArticleRetriever(self.db),
            web_search=_FakeWebSearch(), store=self.store, policy_resolver=_PolicyResolver())

    def _context(self, *, hops=3):
        decomposition = {"is_multi_hop": bool(hops > 1), "pattern": "impact_chain",
                         "hop_count": hops, "dag_ok": True,
                         "hops": [{"id": "h1", "question": "香港家族办公室税收优惠政策",
                                   "depends_on": [], "carry": ["entities"],
                                   "purpose": "政策事实"},
                                  {"id": "h2", "question": "内地高净值客户 影响",
                                   "depends_on": ["h1"], "carry": ["entities"],
                                   "purpose": "传导"},
                                  {"id": "h3", "question": "高净值客户 配置 变化",
                                   "depends_on": ["h2"], "carry": ["entities"],
                                   "purpose": "结果"}][:max(1, hops)]}
        return {
            "request": {"question": QUESTION, "industry_pack_id": DEFAULT_PACK,
                        "mode": "standard", "page_context": {}},
            "run": {"id": self.run_id, **SCOPE},
            "outputs": {"plan": {
                "question": QUESTION, "standalone_question": QUESTION,
                "queries": [QUESTION], "entities": ["家族办公室"],
                "needs_local_articles": True, "needs_web": False,
                "category": {"key": "multi_hop", "label": "多跳传导类"},
                "decomposition": decomposition,
            }},
        }

    def _traces(self):
        return self.store.reasoning_traces(self.run_id)


class DefaultOffTests(_Base):
    def test_flag_off_keeps_the_stage_untouched(self):
        result = self._handlers()["level1_retrieval"](self._context())
        self.assertEqual(set(result.keys()), LEVEL1_RECEIPT_KEYS)
        self.assertNotIn("gap_loop", result["stats"], "开关没开就不许出现缺口回执（回滚口径）")
        self.assertNotIn("gap_loop_summary", result["stats"])
        self.assertEqual(set(result["stats"]["evidence_layer"].keys()), EVIDENCE_RECEIPT_KEYS)
        self.assertTrue(result["evidence"], "旧路径本身要能取到证据，比较才有意义")

    def test_flag_off_keeps_the_trace_columns_empty(self):
        self._handlers()["level1_retrieval"](self._context())
        traces = self._traces()
        self.assertTrue(traces, "多跳必须留下跳留痕（Phase 01 的 SearchTrace）")
        for row in traces:
            self.assertEqual(str(row["gap_id"] or ""), "")
            self.assertEqual(int(row["new_claims"] or 0), 0)
            self.assertEqual(int(row["resolved_gap"] or 0), 0)


class FlagOnTests(_Base):
    def test_flag_on_adds_only_a_sibling_receipt(self):
        os.environ["QA_GAP_ANALYZER"] = "1"
        result = self._handlers()["level1_retrieval"](self._context())
        self.assertEqual(set(result.keys()), LEVEL1_RECEIPT_KEYS,
                         "阶段返回键集被改动了（Phase 02 冻结）")
        receipt = result["stats"]["gap_loop"]
        ok, note = validate("gap_loop", receipt)
        self.assertTrue(ok, note)
        self.assertEqual(receipt["analyzer_version"], contracts.GAP_ANALYZER_VERSION)
        self.assertTrue(receipt["stop_reason"], "缺口循环必须给出停止原因")
        self.assertIn(receipt["stop_reason"], contracts.QA_STOP_REASONS)
        self.assertEqual(set(result["stats"]["evidence_layer"].keys()), EVIDENCE_RECEIPT_KEYS,
                         "证据层回执键集不许被缺口分析影响")
        summary = result["stats"]["gap_loop_summary"]
        self.assertEqual(summary["receipts"], 1)
        self.assertIn(receipt["stop_reason"], summary["stop_reasons_produced"])
        self.assertTrue(result["evidence"])

    def test_gap_columns_land_in_the_database(self):
        """Phase 01 埋的三列（gap_id/new_claims/resolved_gap）本阶段必须真的有值。"""
        os.environ["QA_GAP_ANALYZER"] = "1"
        result = self._handlers()["level1_retrieval"](self._context())
        traces = self._traces()
        self.assertTrue(traces)
        gap_ids = [str(row["gap_id"] or "") for row in traces]
        self.assertTrue(any(gap_ids), "至少有一跳要带上缺口 id：%s" % gap_ids)
        for row in traces:
            self.assertTrue(str(row["gap_id"] or "").startswith("G"),
                            "缺口 id 口径是 G+内容哈希：%s" % row["gap_id"])
        planned = [row for row in traces if str(row["sub_query_id"] or "").startswith("g")]
        for row in planned:
            self.assertEqual(int(row["resolved_gap"] or 0), 0,
                             "补充跳的 resolved_gap 由**下一轮**观察判定，本跳不预先邀功")
        receipt = result["stats"]["gap_loop"]
        self.assertGreaterEqual(receipt["stats"]["resolved_high_priority_total"], 0)
        self.assertEqual(receipt["stats"]["rounds"], len(receipt["rounds"]))
        self.assertEqual(len(receipt["rounds"]), len(traces),
                         "每跳一轮观察：轮数必须与留痕条数一致（口径可复算）")

    def test_rounds_carry_the_convergence_facts(self):
        os.environ["QA_GAP_ANALYZER"] = "1"
        receipt = self._handlers()["level1_retrieval"](self._context())["stats"]["gap_loop"]
        self.assertTrue(receipt["rounds"])
        for index, row in enumerate(receipt["rounds"]):
            ok, note = validate("gap_loop_round", row)
            self.assertTrue(ok, note)
            self.assertEqual(row["round_index"], index)
            self.assertIn("new_verified_claims", row)
            self.assertIn("resolved_high_priority_gaps", row)
        self.assertTrue(receipt["rounds"][0]["baseline"], "第 0 轮是基线（没有上一轮可比）")

    def test_gap_driven_hop_actually_runs(self):
        """§13：缺口 → 补充跳必须**真的发出去**（route 与缺口 id 都写进回执）。

        `QA_GAP_NO_GAIN_ROUNDS=5` 刻意把收敛门槛抬高：本用例要看的是"有高优缺口且还没收敛时，
        补充跳真的发出去"，而不是"收敛刹车"（那个在 NoGainEndToEndTests 里测）。
        """
        os.environ["QA_GAP_ANALYZER"] = "1"
        os.environ["QA_GAP_MAX_NEXT_HOPS"] = "1"
        os.environ["QA_GAP_NO_GAIN_ROUNDS"] = "5"
        receipt = self._handlers()["level1_retrieval"](self._context())["stats"]["gap_loop"]
        self.assertTrue(receipt["hops"], "有高优缺口时应该规划出补充跳：%s" % receipt["stats"])
        hop = receipt["hops"][0]
        self.assertTrue(hop["gap_id"].startswith("G"))
        self.assertIn(hop["route"], contracts.QA_RETRIEVAL_ROUTES)
        self.assertTrue(hop["plan_overrides"]["queries"])
        traces = self._traces()
        self.assertIn(hop["hop_id"], [str(row["sub_query_id"] or "") for row in traces],
                      "补充跳必须留下 SearchTrace（否则「为什么搜」无处可查）")


class NoGainEndToEndTests(_Base):
    def test_no_gain_stop_reason_is_produced_end_to_end(self):
        """每一跳都只捞到同一批证据 → 连续无增益 → 停止原因 NO_GAIN（§14 原话）。"""
        seed = ArticleRetriever(self.db).retrieve(
            {"question": QUESTION, "queries": [QUESTION], "entities": ["家族办公室"]},
            industry_pack_id=DEFAULT_PACK, page_context={}, limit=8)
        same = list(seed.get("evidence") or [])
        self.assertTrue(same, "语料本身要能取到证据，否则这个用例没有意义")
        os.environ["QA_GAP_ANALYZER"] = "1"
        os.environ["QA_GAP_NO_GAIN_ROUNDS"] = "2"
        result = self._handlers(_SameEvidenceRetriever(same))["level1_retrieval"](self._context())
        receipt = result["stats"]["gap_loop"]
        self.assertEqual(receipt["stop_reason"], contracts.QA_STOP_NO_GAIN)
        self.assertIn("new_verified_claims=0", receipt["stop_detail"])
        streaks = [row["no_gain_streak"] for row in receipt["rounds"]]
        self.assertGreaterEqual(max(streaks), 2, "连续无增益的逐轮事实必须留痕：%s" % streaks)
        self.assertGreater(sum(1 for row in receipt["rounds"] if row["no_gain"]), 0)
        self.assertTrue(receipt["stats"]["open_gaps"] >= 0)
        # 收敛之后不再发补充跳（把预算留给别的阶段）
        self.assertLessEqual(len(receipt["hops"]), gap.max_next_hops())

    def test_answerable_when_every_gap_is_closed(self):
        """证据充分（官方原文 + 独立来源）时停止原因是 ANSWERABLE —— 五值不是摆设。"""
        os.environ["QA_GAP_ANALYZER"] = "1"
        os.environ["QA_GAP_MAX_NEXT_HOPS"] = "0"
        os.environ["QA_GAP_PRIORITY_THRESHOLD"] = "0.99"
        try:
            receipt = self._handlers()["level1_retrieval"](self._context())["stats"]["gap_loop"]
        finally:
            os.environ.pop("QA_GAP_PRIORITY_THRESHOLD", None)
        self.assertEqual(receipt["stats"]["high_priority_open"], 0)
        self.assertEqual(receipt["stop_reason"], contracts.QA_STOP_ANSWERABLE)


class UnresolvableContradictionTests(_Base):
    """两条**同等分量**的相反结论（权威相同、发布时间相同、强度相近）→ Phase 06 裁决 unresolved
    → 缺口复核给出 UNRESOLVABLE_CONTRADICTION（这是该停止原因唯一的权威出口）。"""

    def _graph_context(self):
        context = self._context()
        context["outputs"]["plan"] = {
            "question": QUESTION, "standalone_question": QUESTION,
            "queries": [QUESTION], "entities": ["家族办公室"],
            "category": {"key": "multi_hop", "label": "多跳传导类"}}
        context["outputs"]["level1_draft"] = {
            "contract_version": "unified-qa-v1", "draft_answer": "草稿",
            "claims": [
                {"claim_id": "c1", "text": "香港家办税收优惠政策自2026年10月起生效",
                 "claim_type": "current_fact", "confidence": 0.8, "valid_from": "2026-10-01",
                 "valid_to": None, "scope": [], "evidence_refs": ["article:1"],
                 "needs_verification": True, "verification_status": "confirmed"},
                {"claim_id": "c2", "text": "香港家办税收优惠政策自2026年10月起没有生效",
                 "claim_type": "current_fact", "confidence": 0.8, "valid_from": "2026-10-01",
                 "valid_to": None, "scope": [], "evidence_refs": ["article:2"],
                 "needs_verification": True, "verification_status": "unverified"},
            ],
        }
        context["outputs"]["level1_retrieval"] = {"stats": {}}
        context["outputs"]["level2_research"] = {}
        # 两侧来源**同等权威**（都 50）、**同一天发布**、强度相近 → 规则裁决无一条可用
        context["outputs"]["level1_draft"]["evidence"] = [
            {"evidence_ref": "article:1", "source_type": "article", "title": "香港家办政策生效",
             "source_url": "https://example.com/1", "article_id": 1, "authority_level": 50,
             "content_excerpt": "香港家办税收优惠政策自2026年10月起生效，符合条件的可享利得税宽免。",
             "published_at": "2026-10-01", "score": 30.0, "retrieval_method": "keyword",
             "relationship": "supports", "metadata": {}},
            {"evidence_ref": "article:2", "source_type": "article", "title": "香港家办政策尚未生效",
             "source_url": "https://example.com/2", "article_id": 2, "authority_level": 50,
             "content_excerpt": "香港家办税收优惠政策自2026年10月起没有生效，仍在审议中。",
             "published_at": "2026-10-01", "score": 28.0, "retrieval_method": "keyword",
             "relationship": "contradicts", "metadata": {}},
        ]
        context["outputs"]["level1_draft"]["citations"] = ["article:1", "article:2"]
        return context

    def test_unresolved_decision_becomes_the_stop_reason(self):
        os.environ["QA_EVIDENCE_GRAPH"] = "1"
        os.environ["QA_GAP_ANALYZER"] = "1"
        graph = self._handlers()["conflict_review"](self._graph_context())
        self.assertEqual(set(graph.keys()), GRAPH_KEYS | {"evidence_graph"})
        unresolved = [item for item in graph["evidence_graph"]["contradictions"]
                      if item["resolution"] == "unresolved"]
        self.assertTrue(unresolved, "同等分量的相反结论必须裁成 unresolved：%s"
                        % [(item["kind"], item["resolution"], item["reason_code"])
                           for item in graph["evidence_graph"]["contradictions"]])
        for item in unresolved:
            self.assertIn(item["reason_code"], contracts.CONTRADICTION_UNRESOLVED_CODES)
        review = graph["evidence_graph"]["gap_review"]
        self.assertEqual(review["stop_reason"], contracts.QA_STOP_UNRESOLVABLE_CONTRADICTION)
        self.assertEqual(review["stats"]["unresolved_contradictions"], len(unresolved))
        self.assertEqual(review["unresolved_contradictions"][0]["reason_code"],
                         unresolved[0]["reason_code"])
        self.assertIn("evidence_graph_review", review["stop_source"])

    def test_flag_off_keeps_conflict_review_untouched(self):
        graph = self._handlers()["conflict_review"](self._graph_context())
        self.assertEqual(set(graph.keys()), GRAPH_KEYS)

    def test_final_answer_still_validates_with_the_gap_review(self):
        os.environ["QA_EVIDENCE_GRAPH"] = "1"
        os.environ["QA_GAP_ANALYZER"] = "1"
        context = self._graph_context()
        graph = self._handlers()["conflict_review"](context)
        answer = qa_synthesis.fallback_final_answer(
            graph=graph, level1=context["outputs"]["level1_draft"], level2={}, degradation=[],
            models={"draft": "rule"}, reason="测试", question=QUESTION)
        import qa_contracts

        validated = qa_contracts.validate_final_answer(answer)
        self.assertEqual(len(validated["conflicts"]), len(graph["conflicts"]))


class SingleHopTests(_Base):
    """§13：缺口驱动的下一跳对**单跳**问题最有用（首跳没解掉的缺口只能靠补充跳）。"""

    def test_flag_off_keeps_single_hop_untouched(self):
        result = self._handlers()["level1_retrieval"](self._context(hops=1))
        self.assertEqual(set(result.keys()), LEVEL1_RECEIPT_KEYS)
        self.assertNotIn("gap_loop", result["stats"])
        self.assertEqual(self._traces(), [], "没开缺口循环时单跳问题不写多跳留痕")

    def test_flag_on_runs_the_gap_loop_for_single_hop(self):
        os.environ["QA_GAP_ANALYZER"] = "1"
        os.environ["QA_GAP_MAX_NEXT_HOPS"] = "1"
        os.environ["QA_GAP_NO_GAIN_ROUNDS"] = "5"
        result = self._handlers()["level1_retrieval"](self._context(hops=1))
        receipt = result["stats"]["gap_loop"]
        ok, note = validate("gap_loop", receipt)
        self.assertTrue(ok, note)
        self.assertEqual(receipt["stage"], "multi_hop")
        self.assertTrue(receipt["rounds"], "单跳也要有基线轮")
        self.assertTrue(receipt["hops"], "单跳问题的缺口应该换来补充跳：%s" % receipt["stats"])
        traces = self._traces()
        self.assertTrue(traces)
        self.assertTrue(any(str(row["gap_id"] or "").startswith("G") for row in traces),
                        "单跳的留痕也要带缺口 id：%s"
                        % [(row["sub_query_id"], row["gap_id"]) for row in traces])
        self.assertTrue(result["evidence"])


class FailurePathTests(_Base):
    def test_analyzer_error_never_breaks_retrieval(self):
        os.environ["QA_GAP_ANALYZER"] = "1"
        with mock.patch.object(gap.GapLoopState, "observe",
                               side_effect=RuntimeError("缺口分析炸了")):
            result = self._handlers()["level1_retrieval"](self._context())
        self.assertEqual(set(result.keys()), LEVEL1_RECEIPT_KEYS)
        self.assertTrue(result["evidence"], "缺口分析坏了，证据包照旧要出来")
        receipt = result["stats"]["gap_loop"]
        self.assertTrue(receipt["errors"], "失败必须留痕：%s" % receipt)
        self.assertIn("RuntimeError", receipt["errors"][0]["error"])
        self.assertTrue(receipt["stop_reason"], "即便分析坏了也要给停止原因（不许空着）")

    def test_planner_error_stops_the_extra_hops_only(self):
        os.environ["QA_GAP_ANALYZER"] = "1"
        os.environ["QA_GAP_NO_GAIN_ROUNDS"] = "5"   # 先别让收敛刹车拦住规划器调用
        events = []
        with mock.patch.object(gap.GapLoopState, "next_hops",
                               side_effect=RuntimeError("规划器炸了")):
            context = self._context()
            context["_emit_stage_event"] = lambda kind, payload=None: events.append(payload or {})
            result = self._handlers()["level1_retrieval"](context)
        self.assertTrue(result["evidence"])
        receipt = result["stats"]["gap_loop"]
        self.assertEqual(receipt["hops"], [])
        messages = " ".join(str(item.get("message") or "") for item in events)
        self.assertIn("规划失败", messages)

    def test_review_error_never_breaks_conflict_review(self):
        os.environ["QA_EVIDENCE_GRAPH"] = "1"
        os.environ["QA_GAP_ANALYZER"] = "1"
        with mock.patch.object(qa_pipeline, "review_gap_graph",
                               side_effect=RuntimeError("复核炸了")):
            graph = self._handlers()["conflict_review"](
                UnresolvableContradictionTests._graph_context(self))
        review = graph["evidence_graph"]["gap_review"]
        self.assertIn("error", review)
        self.assertIn("RuntimeError", review["error"])
        import qa_contracts

        self.assertEqual(int(self.db.connection.execute(
            "SELECT count(*) FROM qa_claims WHERE run_id=?", (self.run_id,)).fetchone()[0]), 2,
            "图照旧落库")


if __name__ == "__main__":
    unittest.main()

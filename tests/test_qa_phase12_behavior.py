#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 12 · **经验记忆真的改变后续行为** + 管线接线用例。

这是 P12-02/P12-03/P12-06 的"不是死数据"证明：
  1. `build_guidance` 把失败/策略记忆转成可执行的通道偏好与提前提示，每条都带
     memory_id/理由码（可追溯）；`SUPPRESS` 的失败进 `avoid_routes`，
     `ALLOW_RETRY` 的只出现在 warnings 里（**版本变了就自动停止压制**）；
  2. `apply_route_guidance` **只重排缺口自己建议过的通道**（绝不发明通道），
     并给出逐条 `route_changes`（from/to/reason）—— 这就是前后对比的原始证据；
  3. **前后对比数字**：同一批缺口 + 同一份失败记忆，在"同一环境"（压制）与
     "版本已变"（放行）两种口径下，被改变的缺口数与"重复踩坑"数显著不同；
  4. 缺口循环钩子 `GapLoopState.next_hops(route_guidance=...)`：缺省 None 时行为与
     Phase 07 逐字相同（返回键集不变），回调抛错被吞掉并保留原缺口；
  5. 管线接线：`QA_EXPERIENCE_MEMORY` 默认关 → 一个键都不新增、一行都不写；
     打开后 `stats["experience_memory"]` 兄弟键出现、记忆落库、召回仍是 MEMORY_HINT。
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_gap_analyzer as gap  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_memory_experience as exp  # noqa: E402
import qa_pipeline  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

import qa_phase09_fixtures as fx09  # noqa: E402
import qa_phase12_fixtures as fx  # noqa: E402

NOW = "2026-10-11T00:00:00Z"
ENV_TOKEN = "rcfg:0123456789abcdef"


def _failure(kind="FAILED_ROUTE", route="keyword", **overrides):
    row = {"failure_id": "F-%s-%s" % (kind, route or "none"), "kind": kind, "route": route,
           "domain": "", "corpus_version": fx.KB_VERSION,
           "retrieval_config_version": ENV_TOKEN, "observed_at": NOW,
           "task_type": "MULTI_HOP", "domain": ""}
    row.update(overrides)
    return row


def _strategy(combination=("keyword",), **overrides):
    meta = {"combination": list(combination), "task_type": "MULTI_HOP", "observations": 3,
            "successes": 2, "success_rate": 2 / 3, "min_samples": 2, "success_floor": 0.6,
            "success_definition": "该通道那一次至少产出 1 条 SUPPORTED 证据",
            "routes_detail": {route: {"observations": 3, "successes": 2, "success_rate": 2 / 3}
                              for route in combination},
            "run_ids": ["run-a", "run-b"]}
    meta.update(overrides)
    return {"memory_id": "MEM-STRATEGY", "metadata": meta}


class GuidanceTests(unittest.TestCase):
    def test_suppressed_failures_block_the_route_and_warn(self):
        guidance = exp.build_guidance(failures=[_failure(route="keyword")],
                                      task_type="MULTI_HOP",
                                      corpus_version=fx.KB_VERSION,
                                      retrieval_config_version=ENV_TOKEN, now=NOW)
        self.assertEqual(guidance["avoid_routes"], ["keyword"])
        self.assertEqual(guidance["stats"]["suppressed"], 1)
        codes = {row["code"] for row in guidance["warnings"]}
        self.assertEqual(codes, {"FAILURE_MEMORY_AVOID"})
        self.assertIn("已避开", guidance["warnings"][0]["text"])
        self.assertTrue(guidance["requires_revalidation"])
        self.assertIs(guidance["verified_evidence"], False)
        ok, note = validate("experience_guidance", guidance)
        self.assertTrue(ok, note)

    def test_version_change_turns_suppression_into_a_retry_warning(self):
        guidance = exp.build_guidance(failures=[_failure(route="keyword")],
                                      task_type="MULTI_HOP",
                                      corpus_version="kb-NEW",
                                      retrieval_config_version=ENV_TOKEN, now=NOW)
        self.assertEqual(guidance["avoid_routes"], [])
        self.assertEqual(guidance["stats"]["allowed_retry"], 1)
        warning = guidance["warnings"][0]
        self.assertEqual(warning["code"], "FAILURE_MEMORY_RETRY_ALLOWED")
        self.assertEqual(warning["reason"], "CORPUS_VERSION_CHANGED")
        self.assertIn("允许重试", warning["text"])

    def test_unbound_failure_is_never_suppressed(self):
        guidance = exp.build_guidance(failures=[_failure(corpus_version="",
                                                         retrieval_config_version="")],
                                      task_type="MULTI_HOP", corpus_version=fx.KB_VERSION,
                                      retrieval_config_version=ENV_TOKEN, now=NOW)
        self.assertEqual(guidance["avoid_routes"], [])
        self.assertEqual(guidance["warnings"][0]["reason"], "VERSION_UNBOUND")

    def test_strategy_creates_preferences_with_reason(self):
        guidance = exp.build_guidance(strategies=[_strategy(("keyword",))],
                                      task_type="MULTI_HOP", corpus_version=fx.KB_VERSION,
                                      retrieval_config_version=ENV_TOKEN, now=NOW)
        self.assertEqual(guidance["prefer_routes"], ["keyword"])
        codes = {row["code"] for row in guidance["warnings"]}
        self.assertEqual(codes, {"STRATEGY_MEMORY_PREFER"})
        self.assertIn("有效率", guidance["warnings"][0]["text"])

    def test_strategy_of_another_task_type_is_ignored(self):
        guidance = exp.build_guidance(strategies=[_strategy(task_type="OTHER")],
                                      task_type="MULTI_HOP", now=NOW)
        self.assertEqual(guidance["prefer_routes"], [])

    def test_empty_recall_yields_empty_guidance(self):
        guidance = exp.build_guidance(task_type="MULTI_HOP", now=NOW)
        self.assertEqual((guidance["prefer_routes"], guidance["avoid_routes"]), ([], []))
        self.assertEqual(guidance["warnings"], [])

    def test_receipt_is_counted_only(self):
        guidance = exp.build_guidance(failures=[_failure(), _failure(route="web")],
                                      strategies=[_strategy()], task_type="MULTI_HOP",
                                      corpus_version=fx.KB_VERSION,
                                      retrieval_config_version=ENV_TOKEN, now=NOW)
        receipt = exp.guidance_receipt(guidance)
        self.assertEqual(receipt["warnings"], len(guidance["warnings"]))
        self.assertEqual(receipt["suppressed"], 2)
        self.assertNotIn("经验提示", str(receipt), "回执不带提示正文")


class ApplyGuidanceTests(unittest.TestCase):
    def test_avoid_reorders_within_the_suggested_set_only(self):
        gaps = [fx.gap("G1", routes=("keyword", "semantic", "web"))]
        guidance = {"prefer_routes": [], "avoid_routes": ["keyword"]}
        applied, receipt = exp.apply_route_guidance(gaps, guidance)
        self.assertEqual(applied[0]["suggested_routes"], ["semantic", "web"])
        self.assertEqual(applied[0]["route_guidance"]["original"],
                         ["keyword", "semantic", "web"])
        self.assertEqual(receipt["changed"], 1)
        self.assertEqual(receipt["route_changes"][0]["reason"], "FAILURE_AVOIDED")
        self.assertEqual(receipt["route_changes"][0]["from"], "keyword")
        self.assertEqual(receipt["route_changes"][0]["to"], "semantic")
        # 原缺口**不许被就地改写**（应用的是副本）
        self.assertEqual(gaps[0]["suggested_routes"], ["keyword", "semantic", "web"])

    def test_prefer_promotes_only_existing_suggestions(self):
        gaps = [fx.gap("G1", routes=("semantic", "keyword", "web"))]
        applied, receipt = exp.apply_route_guidance(gaps, {"prefer_routes": ["keyword"]})
        self.assertEqual(applied[0]["suggested_routes"][0], "keyword")
        self.assertEqual(receipt["route_changes"][0]["reason"], "STRATEGY_PREFERRED")
        # 偏好里有一个不在建议集合里的通道 → 它不会被硬塞进来
        applied, _receipt = exp.apply_route_guidance(
            [fx.gap("G2", routes=("semantic", "web"))], {"prefer_routes": ["graph_attribute"]})
        self.assertEqual(applied[0]["suggested_routes"], ["semantic", "web"])

    def test_all_avoided_keeps_the_original_order_and_flags_it(self):
        applied, receipt = exp.apply_route_guidance(
            [fx.gap("G1", routes=("keyword", "web"))],
            {"avoid_routes": ["keyword", "web"]})
        self.assertEqual(applied[0]["suggested_routes"], ["keyword", "web"])
        self.assertTrue(applied[0]["route_guidance"]["no_alternative"])
        self.assertEqual(receipt["changed"], 0)

    def test_first_route_already_preferred_means_no_churn(self):
        applied, receipt = exp.apply_route_guidance(
            [fx.gap("G1", routes=("keyword", "semantic"))], {"prefer_routes": ["keyword"]})
        self.assertEqual(applied[0]["suggested_routes"], ["keyword", "semantic"])
        self.assertEqual(receipt["changed"], 0)

    def test_before_after_counts_on_a_real_gap_set(self):
        """前后对比：同一批缺口，压制生效 vs 版本已变，被改变的缺口数不同。"""
        gaps = [fx.gap("G%d" % index, routes=("keyword", "semantic", "web"))
                for index in range(10)]
        suppressed = exp.build_guidance(failures=[_failure(route="keyword")],
                                        task_type="MULTI_HOP", corpus_version=fx.KB_VERSION,
                                        retrieval_config_version=ENV_TOKEN, now=NOW)
        changed = exp.build_guidance(failures=[_failure(route="keyword")],
                                     task_type="MULTI_HOP", corpus_version="kb-NEW",
                                     retrieval_config_version=ENV_TOKEN, now=NOW)
        _before, receipt_suppressed = exp.apply_route_guidance(gaps, suppressed)
        _after, receipt_changed = exp.apply_route_guidance(gaps, changed)
        self.assertEqual(receipt_suppressed["changed"], 10)
        self.assertEqual(receipt_changed["changed"], 0)
        self.assertEqual(receipt_suppressed["changed_by_reason"], {"FAILURE_AVOIDED": 10})

    def test_duplicate_route_values_and_empty_gap(self):
        applied, receipt = exp.apply_route_guidance(
            [{"gap_id": "G0", "suggested_routes": []}, {"gap_id": "G1", "suggested_routes": ["web"]}],
            {"avoid_routes": ["web"]})
        self.assertEqual(applied[0]["suggested_routes"], [])
        self.assertEqual(applied[1]["suggested_routes"], ["web"])
        self.assertEqual(receipt["changed"], 0)


class GapLoopHookTests(unittest.TestCase):
    def _state(self, gap_row=None):
        state = gap.GapLoopState(category="MULTI_HOP", plan={"question": fx.QUESTION},
                                 budget_seconds=0.0)
        row = dict(gap_row or fx.gap("G1"))
        row["status"] = "open"
        state.gaps = [row]
        return state

    def test_default_none_is_verbatim_phase07(self):
        baseline = self._state()
        baseline_result = baseline.next_hops(plan={"question": fx.QUESTION}, limit=1)
        self.assertEqual(baseline.last_guidance, {})
        self.assertEqual(baseline_result["hops"][0]["route"], "semantic")
        # 传了回调但回调不改任何东西 → 返回结构与"没有回调"逐字相同（键集与内容）
        hooked = self._state()

        def passthrough(gaps):
            adjusted, receipt = exp.apply_route_guidance(gaps, {})
            return adjusted, receipt

        hooked_result = hooked.next_hops(plan={"question": fx.QUESTION}, limit=1,
                                         route_guidance=passthrough)
        self.assertEqual(sorted(hooked_result), sorted(baseline_result))
        self.assertEqual(hooked_result["hops"], baseline_result["hops"])
        self.assertEqual(hooked.last_guidance["changed"], 0)

    def test_callback_can_change_the_chosen_route(self):
        state = self._state()

        def guidance(gaps):
            adjusted, receipt = exp.apply_route_guidance(gaps, {"prefer_routes": ["web"]})
            return adjusted, receipt

        result = state.next_hops(plan={"question": fx.QUESTION}, limit=1,
                                 route_guidance=guidance)
        self.assertEqual(result["hops"][0]["route"], "web")
        self.assertEqual(state.last_guidance["changed"], 1)
        self.assertEqual(state.last_guidance["route_changes"][0]["reason"], "STRATEGY_PREFERRED")
        # 指导回执走 `last_guidance` 属性，**不改进既有回执结构**（键集与 Phase 07 相同）
        baseline_keys = sorted(self._state().next_hops(plan={"question": fx.QUESTION}, limit=1))
        self.assertEqual(sorted(result), baseline_keys)

    def test_guidance_only_reorders_within_the_suggested_routes(self):
        state = self._state(fx.gap("G1", routes=("semantic", "keyword")))

        def guidance(gaps):
            return exp.apply_route_guidance(gaps, {"prefer_routes": ["web"]})

        result = state.next_hops(plan={"question": fx.QUESTION}, limit=1,
                                 route_guidance=guidance)
        self.assertEqual(result["hops"][0]["routes"], ["semantic", "keyword"])
        self.assertEqual(result["hops"][0]["route"], "semantic")

    def test_callback_error_is_swallowed_and_original_gaps_kept(self):
        state = self._state()

        def boom(gaps):
            raise RuntimeError("指导坏了")

        result = state.next_hops(plan={"question": fx.QUESTION}, limit=1, route_guidance=boom)
        self.assertEqual(result["hops"][0]["route"], "semantic")
        self.assertIn("指导坏了", state.last_guidance["error"])


class PipelineWiringTests(unittest.TestCase):
    QUESTION = fx09.QUESTION
    FLAGS = ("QA_EXPERIENCE_MEMORY", "QA_GAP_ANALYZER", "QA_MEMORY_GRAPH",
             "QA_MEMORY_REVALIDATION", "QA_CONTEXT_PACK", "QA_EVIDENCE_GRAPH", "QA_EVIDENCE_LAYER")

    class _Retriever:
        def __init__(self, evidence):
            self.evidence = [dict(item) for item in evidence]

        def retrieve(self, plan, **kwargs):
            return {"evidence": [dict(item) for item in self.evidence],
                    "stats": {"eligible": len(self.evidence), "adopted": len(self.evidence),
                              "keyword_candidates": 7}}

    class _Web:
        def search(self, queries, *, enabled=True, limit=8):
            return {"evidence": [], "status": "disabled", "providers": [], "errors": []}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.tmp.name, "p12-pipeline.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.assertEqual(self.db.backend, "sqlite", "测试必须跑在隔离 sqlite 上")
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self.run = self.store.create_run(
            {"industry_pack_id": "auto", "question": self.QUESTION, "mode": "standard"},
            owner_user_id="p12", idempotency_key="phase12-pipeline")
        self.run_id = str(self.run["id"])
        self.saved = {name: os.environ.pop(name, None) for name in self.FLAGS}

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        try:
            self.db.connection.close()
        except Exception:      # noqa: BLE001
            pass
        self.tmp.cleanup()

    def _handlers(self):
        return qa_pipeline.build_qa_stage_handlers(
            database=self.db,
            article_retriever=self._Retriever([fx09.verified_evidence("article:1")]),
            web_search=self._Web(), store=self.store)

    def _plan(self):
        return {"question": self.QUESTION, "queries": ["香港家族办公室 税收优惠"],
                "entities": ["家族办公室"], "category": {"key": "MULTI_HOP"},
                "decomposition": {"is_multi_hop": False, "pattern": "single", "hop_count": 1,
                                  "hops": [{"id": "h1", "question": self.QUESTION,
                                            "depends_on": []}]}}

    def _context(self):
        evidence = [fx09.verified_evidence("article:1")]
        claims = [{"claim_id": "c1", "text": fx09.CLAIM_TEXT, "claim_type": "policy",
                   "confidence": 0.8, "valid_from": "2026-04-01", "valid_to": None,
                   "scope": ["家族办公室"], "evidence_refs": ["article:1"],
                   "needs_verification": True, "verification_status": "unverified"}]
        return {"request": {"question": self.QUESTION, "industry_pack_id": "auto",
                            "mode": "standard"},
                "run": {"id": self.run_id, "owner_user_id": "p12", "session_id": "s1",
                        "industry_pack_id": "auto"},
                "outputs": {"plan": self._plan(),
                            "level1_draft": {"contract_version": "unified-qa-v1",
                                             "draft_answer": "草稿", "claims": claims,
                                             "gaps": [], "evidence": evidence},
                            "level1_retrieval": {"evidence": evidence,
                                                 "stats": {"adopted": 1},
                                                 "excluded": {"quality_gate": 3},
                                                 "queries": [self.QUESTION],
                                                 "search_status": "completed",
                                                 "cache": {"hit": False,
                                                           "kb_version": fx.KB_VERSION}}}}

    def _memory_rows(self):
        return self.store.load_memory_items(include_all_scopes=True)

    def test_switch_defaults_to_off(self):
        self.assertFalse(exp.experience_memory_enabled())
        self.assertFalse(qa_pipeline._experience_loop_enabled(store=self.store))

    def test_off_means_no_rows_and_no_keys(self):
        handlers = self._handlers()
        result = handlers["level1_retrieval"](self._context())
        self.assertNotIn("experience_memory", result.get("stats") or {})
        receipt = qa_pipeline._write_experience_after_answer(
            self._context(), graph={"version": "qa-evidence-graph-v1", "claims": [],
                                    "evidence": [], "edges": [], "conflicts": [], "stats": {}},
            run_meta={"id": self.run_id, "owner_user_id": "p12", "session_id": "s1",
                      "industry_pack_id": "auto"}, store=self.store)
        self.assertEqual(receipt, {})
        self.assertEqual(self._memory_rows(), [])

    def test_on_writes_memories_through_the_gate(self):
        os.environ["QA_EXPERIENCE_MEMORY"] = "1"
        context = self._context()
        graph = {"version": "qa-evidence-graph-v1",
                 "claims": [{"canonical_id": "c1", "plan_only": False,
                             "claim": {"claim_id": "c1", "text": fx09.CLAIM_TEXT,
                                       "claim_type": "policy", "confidence": 0.8,
                                       "evidence_refs": ["article:1"]}}],
                 "evidence": [fx09.verified_evidence("article:1")],
                 "edges": [{"claim_id": "c1", "evidence_ref": "article:1",
                            "graph_relation": "SUPPORTS", "relationship": "supports"}],
                 "conflicts": [], "stats": {}}
        receipt = qa_pipeline._write_experience_after_answer(
            context, graph=graph,
            run_meta={"id": self.run_id, "owner_user_id": "p12", "session_id": "s1",
                      "industry_pack_id": "auto", "question_text": self.QUESTION,
                      "mode": "standard"}, store=self.store)
        self.assertIn("receipt", receipt)
        self.assertTrue(receipt["receipt"]["experience_version"])
        self.assertTrue(receipt["trace"]["trace_fingerprint"].startswith("XTF"))
        self.assertIn("EPISODIC_RESEARCH", receipt["receipt"]["persisted_by_type"])
        rows = self._memory_rows()
        self.assertTrue(rows)
        self.assertTrue(all(row["memory_type"] in contracts.MEMORY_PRODUCIBLE_TYPES_BY_PHASE["P12"]
                            for row in rows))
        for row in rows:
            self.assertEqual(row["scope"], "ORGANIZATION")
            self.assertIs(row["metadata"]["hint_only"], True)

    def test_trace_marks_missing_sections_instead_of_inventing(self):
        os.environ["QA_EXPERIENCE_MEMORY"] = "1"
        trace = qa_pipeline._experience_trace(self._context(), graph={},
                                              run_meta={"id": self.run_id,
                                                        "owner_user_id": "p12"},
                                              store=self.store)
        for name in ("verification", "gaps", "stop", "sources"):
            self.assertIn(name, trace["missing_sections"])

    def test_planning_guidance_is_empty_without_memories(self):
        os.environ["QA_EXPERIENCE_MEMORY"] = "1"
        guidance = qa_pipeline._planning_guidance(
            run_meta={"id": self.run_id, "owner_user_id": "p12", "session_id": "s1",
                      "industry_pack_id": "auto"},
            plan=self._plan(), store=self.store, question=self.QUESTION,
            corpus_version=fx.KB_VERSION, retrieval_config_version=ENV_TOKEN)
        self.assertEqual(guidance, {})

    def test_startup_error_in_the_experience_layer_is_contained(self):
        os.environ["QA_EXPERIENCE_MEMORY"] = "1"
        broken = QaStore(self.db)

        def _boom(*args, **kwargs):
            raise RuntimeError("库坏了")

        broken.stage_runs = _boom
        receipt = qa_pipeline._write_experience_after_answer(
            self._context(), graph={}, run_meta={"id": self.run_id, "owner_user_id": "p12"},
            store=broken)
        self.assertIn("receipt", receipt, "读阶段行失败只让 trace 少一段，不该整段失败")
        self.assertTrue(receipt["trace"]["trace_fingerprint"].startswith("XTF"))
        self.assertIn("skills", receipt["trace"]["missing_sections"])
        # 检索段仍然来自本轮回执（query 是真的），写门照常跑完
        self.assertEqual(receipt["receipt"]["traces"], 1)
        self.assertEqual(receipt["receipt"]["error"], "")


if __name__ == "__main__":
    unittest.main()

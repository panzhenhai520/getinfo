#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 11 · 管线接线用例（`QA_SKILL_ROUTER` / `QA_SKILL_TELEMETRY` 默认关）。

全部在**隔离临时 sqlite** 上真跑（`setUp` 断言 `backend == 'sqlite'`），零模型/零嵌入端点。
钉住：
  1. **默认关 = 零行为变化**：`conflict_review` 的图键集与 Phase 10 之后逐字相同
     （没有 `skill_routing` / `skill_telemetry` 键）、`qa_stage_runs` 一行 `node_kind='skill'` 都没有；
  2. **打开**：只新增兄弟键 `graph["skill_routing"]`（回执过契约），
     `skill_context` 段被接上、条目**不进 citation_map**；
  3. 两个开关互相独立：只开路由不写库；只开遥测（路由关着）什么都不发生；
  4. 遥测打开时按**真实结果**落库（`node_kind='skill'`），读回的成功率与记录一致；
  5. 失败路径：路由层抛错只记账（`error`），证据图与答案照旧；坏 store 只记错误不抛。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_TYPE", "sqlite")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_graph_contracts as contracts  # noqa: E402
import qa_pipeline  # noqa: E402
import qa_skills as skills  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

import qa_phase09_fixtures as fx09  # noqa: E402
import qa_phase11_fixtures as fx  # noqa: E402

QUESTION = fx09.QUESTION
SCOPE = {"owner_user_id": "p11", "session_id": "s11", "industry_pack_id": "auto"}
FLAGS = ("QA_SKILL_ROUTER", "QA_SKILL_TELEMETRY", "QA_MEMORY_GRAPH",
         "QA_MEMORY_REVALIDATION", "QA_CONTEXT_PACK", "QA_GROUNDING_GATE",
         "QA_EVIDENCE_GRAPH", "QA_GAP_ANALYZER")


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
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "phase11.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.assertEqual(self.db.backend, "sqlite", "测试必须跑在隔离 sqlite 上")
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self.run = self.store.create_run(
            {"industry_pack_id": "auto", "question": QUESTION, "mode": "standard"},
            owner_user_id="p11", idempotency_key="phase11-pipeline")
        self.run_id = str(self.run["id"])
        self.saved = {name: os.environ.pop(name, None) for name in FLAGS}

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
        self.temp_dir.cleanup()

    def _handlers(self, *, gaps=None):
        """建 handlers；`gaps` 会通过 level1 的 `stats.gap_loop` 注入真实缺口回执。"""
        gap_rows = list(gaps or [])
        level1 = {"evidence": [fx09.verified_evidence("article:1")],
                  "stats": {"adopted": 1, "gap_loop": {"stop_reason": "", "rounds": [],
                                                       "gaps": gap_rows}}}
        self._level1 = level1
        return qa_pipeline.build_qa_stage_handlers(
            database=self.db,
            article_retriever=_FakeRetriever([fx09.verified_evidence("article:1")]),
            web_search=_FakeWebSearch(), store=self.store)

    def _context(self):
        claims = []
        evidence = []
        for index in range(3):
            ref = "article:%d" % (index + 1)
            evidence.append(fx09.verified_evidence(ref, claim_text=fx09.CLAIM_TEXT))
            claims.append({"claim_id": "c%d" % (index + 1), "text": fx09.CLAIM_TEXT,
                           "claim_type": "policy", "confidence": 0.82,
                           "valid_from": "2026-04-01", "valid_to": None,
                           "scope": ["家族办公室"], "evidence_refs": [ref],
                           "needs_verification": True, "verification_status": "unverified"})
        outputs = {
            "plan": {"question": QUESTION, "standalone_question": QUESTION,
                     "queries": ["香港家族办公室 税收优惠"], "entities": ["家族办公室"],
                     "interpretation": {"intent": "COMPARISON"},
                     "decomposition": {"is_multi_hop": False, "pattern": "single",
                                       "hop_count": 1,
                                       "hops": [{"id": "h1", "question": QUESTION,
                                                 "depends_on": []}]}},
            "level1_draft": {"contract_version": "unified-qa-v1", "draft_answer": "草稿",
                             "claims": claims, "gaps": [], "evidence": evidence},
            "level1_retrieval": getattr(self, "_level1", {"evidence": evidence,
                                                          "stats": {"adopted": 3}}),
        }
        return {"request": {"question": QUESTION, "industry_pack_id": "auto",
                            "mode": "standard"},
                "run": {"id": self.run_id, **SCOPE}, "outputs": outputs}

    def _rows(self, table, where="", params=()):
        sql = "SELECT * FROM %s" % table
        if where:
            sql += " WHERE " + where
        return self.db.connection.execute(sql, tuple(params)).fetchall()

    def _skill_rows(self):
        return self.store.skill_load_rows(run_id=self.run_id)


class DefaultOffTests(_Base):
    def test_graph_keys_unchanged_when_switches_off(self):
        handlers = self._handlers()
        graph = handlers["conflict_review"](self._context())
        self.assertNotIn("skill_routing", graph)
        self.assertNotIn("skill_telemetry", graph)
        self.assertNotIn("context_pack", graph)

    def test_no_skill_rows_written_when_switches_off(self):
        handlers = self._handlers()
        handlers["conflict_review"](self._context())
        self.assertEqual(self._skill_rows(), [])

    def test_switches_default_to_off(self):
        self.assertFalse(skills.skill_router_enabled())
        self.assertFalse(skills.skill_telemetry_enabled())


class RouterOnTests(_Base):
    def setUp(self):
        super().setUp()
        os.environ["QA_SKILL_ROUTER"] = "1"

    def test_sibling_key_is_added_and_validates(self):
        handlers = self._handlers(gaps=[fx.gap("G1", routes=("keyword",))])
        graph = handlers["conflict_review"](self._context())
        routing = graph["skill_routing"]
        self.assertIn("bm25_search", routing["selected"])
        ok, why = validate("skill_routing", routing)
        self.assertTrue(ok, why)
        self.assertIs(routing["requires_retrieval"], False)

    def test_task_type_comes_from_the_plan(self):
        handlers = self._handlers()
        graph = handlers["conflict_review"](self._context())
        self.assertEqual(graph["skill_routing"]["task_type"], "COMPARISON")
        self.assertIn("citation_verification",
                      [row["skill_id"] for row in graph["skill_routing"]["trace"]
                       if row["decision"] == "selected"])

    def test_router_alone_writes_nothing_to_the_database(self):
        handlers = self._handlers(gaps=[fx.gap("G1", routes=("keyword",))])
        handlers["conflict_review"](self._context())
        self.assertEqual(self._skill_rows(), [])

    def test_skill_section_is_filled_when_context_pack_is_on(self):
        os.environ["QA_CONTEXT_PACK"] = "1"
        handlers = self._handlers(gaps=[fx.gap("G1", routes=("keyword",))])
        graph = handlers["conflict_review"](self._context())
        pack = graph["context_pack"]
        section = pack["sections"]["skill_context"]
        self.assertGreaterEqual(section["count"], 1)
        self.assertEqual(section["loaded_skills"], sorted(graph["skill_routing"]["selected"]))
        self.assertNotIn("deferred_to", section)
        self.assertEqual(pack["stats"]["skill_in_citation_map"], 0)
        for item in pack["items"]:
            if item["section"] == "skill_context":
                self.assertNotIn(item["item_id"], set(pack["citation_map"]))

    def test_telemetry_is_computed_but_not_persisted_when_off(self):
        handlers = self._handlers(gaps=[fx.gap("G1", routes=("keyword",))])
        graph = handlers["conflict_review"](self._context())
        telemetry = graph["skill_telemetry"]
        self.assertGreater(telemetry["records"], 0)
        self.assertEqual(telemetry["persisted"], 0)
        self.assertEqual(self._skill_rows(), [])

    def test_router_error_is_recorded_and_does_not_break_the_graph(self):
        handlers = self._handlers()
        with mock.patch("qa_skills.route_skills", side_effect=RuntimeError("路由炸了")):
            graph = handlers["conflict_review"](self._context())
        self.assertIn("error", graph["skill_routing"])
        self.assertIn("RuntimeError", graph["skill_routing"]["error"])
        self.assertTrue(graph["claims"])
        self.assertTrue(graph["evidence"])


class TelemetryOnTests(_Base):
    def setUp(self):
        super().setUp()
        os.environ["QA_SKILL_ROUTER"] = "1"
        os.environ["QA_SKILL_TELEMETRY"] = "1"
        os.environ["QA_CONTEXT_PACK"] = "1"

    def test_rows_land_in_the_existing_stage_table_only(self):
        handlers = self._handlers(gaps=[fx.gap("G1", routes=("keyword",))])
        graph = handlers["conflict_review"](self._context())
        rows = self._skill_rows()
        self.assertEqual(graph["skill_telemetry"]["persisted"], len(rows))
        self.assertGreater(len(rows), 0)
        for row in rows:
            self.assertEqual(row["status"] in ("completed", "degraded", "failed", "skipped"),
                             True)
        tables = {str(item[0]) for item in self.db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertFalse([name for name in tables if name.startswith("skill")])

    def test_round_trip_rate_matches_the_records(self):
        handlers = self._handlers(gaps=[fx.gap("G1", routes=("keyword",))])
        graph = handlers["conflict_review"](self._context())
        rows = self._skill_rows()
        table = skills.performance_table(rows)
        summary = graph["skill_telemetry"]["overall"]
        self.assertEqual(summary["attempts"], len(rows))
        for name, row in table.items():
            self.assertEqual(row["attempts"],
                             len([item for item in rows if item["skill_id"] == name]))
        # 进包了 → 至少一条 ok；没进包的技能如实记 degraded
        self.assertGreaterEqual(summary["successes"], 1)

    def test_telemetry_report_is_recomputable_across_two_runs(self):
        handlers = self._handlers(gaps=[fx.gap("G1", routes=("keyword",))])
        first = handlers["conflict_review"](self._context())["skill_telemetry"]
        run2 = self.store.create_run(
            {"industry_pack_id": "auto", "question": QUESTION, "mode": "standard"},
            owner_user_id="p11", idempotency_key="phase11-pipeline-2")
        self.run_id = str(run2["id"])
        second = handlers["conflict_review"](self._context())["skill_telemetry"]
        self.assertEqual(first["records"], second["records"])
        self.assertEqual(first["overall"]["attempts"], second["overall"]["attempts"])

    def test_telemetry_off_means_no_rows_even_when_router_on(self):
        os.environ.pop("QA_SKILL_TELEMETRY", None)
        handlers = self._handlers(gaps=[fx.gap("G1", routes=("keyword",))])
        graph = handlers["conflict_review"](self._context())
        self.assertGreater(graph["skill_telemetry"]["records"], 0)
        self.assertEqual(self._skill_rows(), [])

    def test_broken_store_records_persist_errors_instead_of_raising(self):
        handlers = self._handlers(gaps=[fx.gap("G1", routes=("keyword",))])
        original = self.store.record_skill_load

        def _boom(*args, **kwargs):
            raise RuntimeError("库坏了")

        self.store.record_skill_load = _boom
        try:
            graph = handlers["conflict_review"](self._context())
        finally:
            self.store.record_skill_load = original
        self.assertTrue(graph["skill_telemetry"]["persist_errors"])
        self.assertEqual(graph["skill_telemetry"]["persisted"], 0)
        self.assertTrue(graph["claims"])


class SwitchIndependenceTests(_Base):
    def test_telemetry_switch_alone_does_nothing(self):
        os.environ["QA_SKILL_TELEMETRY"] = "1"
        handlers = self._handlers(gaps=[fx.gap("G1", routes=("keyword",))])
        graph = handlers["conflict_review"](self._context())
        self.assertNotIn("skill_routing", graph)
        self.assertNotIn("skill_telemetry", graph)
        self.assertEqual(self._skill_rows(), [])

    def test_router_on_with_context_pack_off_still_routes(self):
        os.environ["QA_SKILL_ROUTER"] = "1"
        handlers = self._handlers(gaps=[fx.gap("G1", routes=("keyword",))])
        graph = handlers["conflict_review"](self._context())
        self.assertIn("skill_routing", graph)
        self.assertNotIn("context_pack", graph)
        self.assertIn("bm25_search", graph["skill_routing"]["selected"])


class FastPathTests(_Base):
    """fast 模式不跑 conflict_review：`synthesis` 里补建的图同样做技能路由与遥测。"""

    def _synthesis_handlers(self):
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

        final = {"contract_version": "unified-qa-v1", "status": "ready", "answer": "答案 [1]",
                 "sections": {}, "claims": [], "conflicts": [], "evidence": [],
                 "citations": [], "citation_map": {}, "cutoff_at": "", "degraded": False,
                 "degradation_reasons": [], "models": {}}
        return qa_pipeline.build_qa_stage_handlers(
            database=self.db,
            article_retriever=_FakeRetriever([fx09.verified_evidence("article:1")]),
            web_search=_FakeWebSearch(), store=self.store,
            provider_registry=_StubRegistry(), feature_flags=_StubFlags(),
            final_synthesizer=mock.Mock(generate=lambda **kwargs: dict(final)))

    def test_fast_path_routes_and_records_when_switches_on(self):
        os.environ["QA_SKILL_ROUTER"] = "1"
        os.environ["QA_SKILL_TELEMETRY"] = "1"
        os.environ["QA_CONTEXT_PACK"] = "1"
        handlers = self._handlers(gaps=[fx.gap("G1", routes=("keyword",))])
        handlers.update(self._synthesis_handlers())
        context = self._context()
        context["outputs"].pop("conflict_review", None)
        handlers["synthesis"](context)
        rows = self._skill_rows()
        self.assertTrue(rows, "fast 路径也要落遥测")

    def test_fast_path_unchanged_when_switches_off(self):
        handlers = self._handlers(gaps=[fx.gap("G1", routes=("keyword",))])
        handlers.update(self._synthesis_handlers())
        context = self._context()
        context["outputs"].pop("conflict_review", None)
        handlers["synthesis"](context)
        self.assertEqual(self._skill_rows(), [])


class ContractAlignmentTests(_Base):
    def test_router_receipt_uses_the_frozen_reason_codes(self):
        os.environ["QA_SKILL_ROUTER"] = "1"
        handlers = self._handlers(gaps=[fx.gap("G1", routes=("keyword", "semantic"))])
        graph = handlers["conflict_review"](self._context())
        for row in graph["skill_routing"]["trace"]:
            self.assertIn(row["reason"], contracts.SKILL_SELECTION_REASONS)

    def test_receipt_does_not_leak_the_trace(self):
        os.environ["QA_SKILL_ROUTER"] = "1"
        handlers = self._handlers(gaps=[fx.gap("G1", routes=("keyword",))])
        graph = handlers["conflict_review"](self._context())
        receipt = graph["skill_routing"]["receipt"]
        self.assertNotIn("trace", receipt)
        self.assertEqual(receipt["selected"], graph["skill_routing"]["selected"])


if __name__ == "__main__":
    unittest.main()

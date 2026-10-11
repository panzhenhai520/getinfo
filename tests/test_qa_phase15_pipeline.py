#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 15 · 管线接线用例（记忆安全挂在既有召回链路上）。

全部在**隔离临时 sqlite** 上真跑（`setUp` 断言 `backend == 'sqlite'`），零模型/零嵌入端点。
钉住：
  1. **默认关 = 零行为变化**：`QA_MEMORY_GRAPH=0` 时图键集与 Phase 09 之前逐字相同；
  2. 打开后**只新增一个兄弟键** `memory`，记忆安全回执挂在**嵌套子键** `memory_safety` 上
     （回滚口径：关掉记忆开关，一个键都不新增）；
  3. **跨轮次隔离在真实召回链路上生效**：上一轮（另一个 run id）写下的当前状态记忆
     不进这一轮的命中；同一轮次里它照旧可召回；
  4. 写门的**作用域强制**真的改库里的 `scope`（当前状态类被收到轮次作用域）；
  5. 审计事件落既有 `qa_audit_events`，且**载荷里没有正文**（MASTER_RULES 18）；
  6. 失败路径：安全模块抛错只记账，图和答案都不受影响。
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
import qa_memory_safety as ms  # noqa: E402
import qa_pipeline  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

import qa_phase09_fixtures as fx  # noqa: E402

QUESTION = fx.QUESTION
SCOPE = dict(fx.SCOPE)
GRAPH_KEYS = {"version", "claims", "evidence", "edges", "conflicts", "verification",
              "normalization_audit", "stats"}
FLAGS = ("QA_MEMORY_GRAPH", "QA_CONTEXT_PACK", "QA_GROUNDING_GATE", "QA_EVIDENCE_GRAPH",
         "QA_GAP_ANALYZER", "QA_MEMORY_AUDIT", "QA_ENCOUNTER_ISOLATION")
STATE_CLAIM = fx.CLAIM_TEXT + "；当前状态：体温 39 度，正在服用某药物"


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


FINAL_ANSWER = {"contract_version": "unified-qa-v1", "status": "ready", "answer": "答案 [1]",
                "sections": {}, "claims": [], "conflicts": [], "evidence": [], "citations": [],
                "citation_map": {}, "cutoff_at": "", "degraded": False,
                "degradation_reasons": [], "models": {}}


class _Base(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "phase15.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.assertEqual(self.db.backend, "sqlite", "测试必须跑在隔离 sqlite 上")
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self.run = self.store.create_run(
            {"industry_pack_id": "auto", "question": QUESTION, "mode": "standard"},
            owner_user_id="p09", idempotency_key="phase15-pipeline")
        self.run_id = str(self.run["id"])
        self.saved = {name: os.environ.pop(name, None) for name in FLAGS}
        self.handlers = qa_pipeline.build_qa_stage_handlers(
            database=self.db,
            article_retriever=_FakeRetriever([fx.verified_evidence("article:1")]),
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

    def _context(self):
        evidence = [fx.verified_evidence("article:1")]
        outputs = {
            "plan": {"question": QUESTION, "standalone_question": QUESTION,
                     "queries": ["香港家族办公室 税收优惠"], "entities": ["家族办公室"],
                     "decomposition": {"is_multi_hop": False, "pattern": "single",
                                       "hop_count": 1, "depends_on": []}},
            "level1_draft": {"contract_version": "unified-qa-v1", "draft_answer": "草稿",
                             "claims": [{
                                 "claim_id": "c1", "text": fx.CLAIM_TEXT,
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

    def _seed_memory(self, *, run_id=None, scope="PATIENT_LONGITUDINAL", claim_text=None,
                     session_id=None, claim_type="policy"):
        """用**真实的管线写入路径**（`write_memories_from_graph`）种一条记忆。

        `claim_type="current_fact"` 就是"当前状态类内容"的既有口径（Phase 09 的
        `classify_freshness` 把 current_fact 判成 SHORT；P15-01 判成 CURRENT_STATE）——
        不必改正文，只改声明类型，核验结论仍是 SUPPORTED（测的是作用域，不是核验）。
        """
        run_meta = {"id": run_id or self.run_id, "owner_user_id": "p09",
                    "session_id": session_id or SCOPE["session_id"],
                    "industry_pack_id": "auto"}
        receipt = memory.write_memories_from_graph(
            self.store, graph=fx.graph(claim_text=claim_text or fx.CLAIM_TEXT,
                                       claim_type=claim_type),
            run_meta=run_meta, scope=scope)
        self.assertTrue(receipt["persisted"], receipt["decisions"])
        return receipt

    def _audit_rows(self):
        return self.db.connection.execute(
            "SELECT event_type, payload_json FROM qa_audit_events").fetchall()


class DefaultOffTests(_Base):
    def test_flag_off_keeps_the_graph_untouched(self):
        graph = self.handlers["conflict_review"](self._context())
        self.assertEqual(set(graph.keys()), GRAPH_KEYS, "开关没开就不许新增键（回滚口径）")
        self.assertNotIn("memory", graph)


class RecallSafetyTests(_Base):
    def test_flag_on_adds_only_the_memory_sibling_with_a_nested_safety_receipt(self):
        os.environ["QA_MEMORY_GRAPH"] = "1"
        graph = self.handlers["conflict_review"](self._context())
        self.assertEqual(set(graph.keys()), GRAPH_KEYS | {"memory"},
                         "记忆安全只许挂在 memory 的**嵌套子键**里")
        receipt = graph["memory"]
        safety = receipt["memory_safety"]
        self.assertEqual(safety["version"], contracts.MEMORY_SAFETY_VERSION)
        for key in ("hits_before", "hits_after", "excluded", "scope", "isolation",
                    "longitudinal", "settings"):
            self.assertIn(key, safety)
        self.assertEqual(safety["isolation"]["leaks"], [])
        self.assertIsInstance(safety["longitudinal"]["stale_rate"], (float, type(None)))

    def test_previous_run_state_does_not_enter_this_run(self):
        os.environ["QA_MEMORY_GRAPH"] = "1"
        self._seed_memory(run_id="run-earlier", scope="ENCOUNTER", claim_type="current_fact")
        stored = self.store.load_memory_items(include_all_scopes=True)[0]
        self.assertTrue(stored["scope_key"].endswith(":run-earlier"),
                        "写侧必须把轮次维（run id）编进作用域键：%s" % stored["scope_key"])
        receipt = self.handlers["conflict_review"](self._context())["memory"]
        self.assertEqual(receipt["hits"], [], "上一轮的当前状态不许进这一轮的命中")

    def test_same_run_memory_is_still_recallable(self):
        os.environ["QA_MEMORY_GRAPH"] = "1"
        self._seed_memory(run_id=self.run_id, scope="ENCOUNTER", claim_type="current_fact")
        receipt = self.handlers["conflict_review"](self._context())["memory"]
        self.assertTrue(receipt["hits"], "同一轮次里的记忆必须照旧可召回")
        for hit in receipt["hits"]:
            self.assertTrue(hit["hint"] and hit["requires_revalidation"])
            self.assertFalse(hit["verified_evidence"], "MASTER_RULES 11：提示不是证据")
            self.assertIn("longitudinal", hit)

    def test_scope_enforcement_rewrites_the_scope_in_the_database(self):
        self._seed_memory(scope="PATIENT_LONGITUDINAL", claim_type="current_fact")
        items = self.store.load_memory_items(include_all_scopes=True)
        stored = [row for row in items if row["memory_type"] == "VERIFIED_CLAIM"][0]
        self.assertEqual(stored["scope"], "ENCOUNTER",
                         "当前状态类的长期记忆必须被收到轮次作用域")
        decisions = [row for row in self.store.memory_write_decisions(run_id=self.run_id)
                     if row["memory_type"] == "VERIFIED_CLAIM"]
        enforcement = decisions[0]["metadata"]["scope_enforcement"]
        self.assertEqual(enforcement["decision"], "NARROW")
        self.assertEqual(enforcement["narrowed_to"], "ENCOUNTER")

    def test_audit_events_are_recorded_without_content(self):
        os.environ["QA_MEMORY_GRAPH"] = "1"
        self._seed_memory(run_id=self.run_id, scope="ENCOUNTER", claim_type="current_fact")
        self.handlers["conflict_review"](self._context())
        rows = self._audit_rows()
        kinds = {str(row[0]) for row in rows}
        self.assertTrue(kinds <= set(contracts.MEMORY_AUDIT_EVENT_KINDS), kinds)
        self.assertIn("ENCOUNTER_ISOLATED", kinds)
        for _kind, payload in rows:
            self.assertNotIn(STATE_CLAIM[:12], str(payload),
                             "审计载荷里不许出现正文（P15-05）")

    def test_replay_verifies_the_pipeline_run(self):
        os.environ["QA_MEMORY_GRAPH"] = "1"
        self.handlers["conflict_review"](self._context())
        report = ms.verify_replay(self.store, self.run_id)
        self.assertTrue(report["replay_matches"], report["mismatches"])
        self.assertEqual(report["run_id"], self.run_id)
        self.assertIn("execution->evidence", [hop["hop"] for hop in report["hops"]])

    def test_safety_module_failure_is_contained(self):
        os.environ["QA_MEMORY_GRAPH"] = "1"
        with mock.patch.object(ms, "apply_recall_safety", side_effect=RuntimeError("boom")):
            graph = self.handlers["conflict_review"](self._context())
        self.assertIn("memory", graph)
        self.assertIn("error", graph["memory"]["memory_safety"])
        self.assertEqual(set(graph.keys()), GRAPH_KEYS | {"memory"})


class WriteAxisTests(_Base):
    def test_candidate_graph_write_uses_the_run_as_the_encounter(self):
        receipt = self.store and memory.write_memories_from_graph(
            self.store, graph=fx.graph(), run_meta={"id": self.run_id, "owner_user_id": "p09",
                                                    "session_id": "s1",
                                                    "industry_pack_id": "auto"})
        self.assertTrue(receipt["persisted"])
        items = self.store.load_memory_items(include_all_scopes=True)
        for item in items:
            if item["scope"] == "ENCOUNTER":
                self.assertTrue(item["scope_key"].endswith(":%s" % self.run_id))

    def test_explicit_empty_encounter_keeps_the_phase09_bucket(self):
        memory.write_memories_from_graph(
            self.store, graph=fx.graph(), run_meta={"id": self.run_id, "owner_user_id": "p09",
                                                   "session_id": "s1",
                                                   "industry_pack_id": "auto"},
            scope="ENCOUNTER", encounter_id="")
        item = self.store.load_memory_items(include_all_scopes=True)[0]
        self.assertEqual(item["scope_key"], "encounter:auto:s1",
                         "显式给空轮次 = 回到 Phase 09 的会话级轮次桶（兼容口径）")


if __name__ == "__main__":
    unittest.main()

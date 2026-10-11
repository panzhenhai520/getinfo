#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 13 · **管线接线用例**（`QA_FINAL_VERIFIER` 默认关）。

全部在**隔离临时 sqlite** 上真跑（`setUp` 断言 `backend == 'sqlite'`），零模型/零嵌入端点。

钉住：
  1. **默认关 = 逐字回滚**：`citation_validation` 的返回键集与取值与接线前**逐字相同**，
     `qa_stage_runs` 里没有 `final_verify` 行，也没有事件；
  2. **打开**：真的写一行 `stage=final_verify`（**零迁移**复用既有表），事件里带终验回执，
     答案仍过**冻结** `FINAL_ANSWER_SCHEMA`，图上多一个兄弟键；
  3. **证据不足**时答案的 `status` 真的变成 `insufficient_evidence`；
  4. **局部重试**的每轮各写一行（attempt=轮次），`retrieval_calls` 恒 0；
  5. 失败路径：终验内部抛错只记账（`degradation_reasons` 追加一条），答案原样交付。
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

import qa_final_verifier as fv  # noqa: E402
import qa_pipeline  # noqa: E402
import qa_phase13_fixtures as fx  # noqa: E402
from qa_contracts import QA_CONTRACT_VERSION, validate_final_answer  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

QUESTION = fx.QUESTION
FLAGS = ("QA_FINAL_VERIFIER", "QA_FINAL_VERIFY_RETRY_MAX", "QA_CONTEXT_PACK",
         "QA_EVIDENCE_GRAPH", "QA_GAP_ANALYZER", "QA_EXECUTION_GRAPH")


class _FakeRetriever:
    def retrieve(self, plan, **kwargs):
        return {"evidence": [], "stats": {}}


class _FakeWebSearch:
    def search(self, queries, *, enabled=True, limit=8):
        return {"evidence": [], "status": "disabled", "providers": [], "errors": []}


class _Base(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "phase13.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.assertEqual(self.db.backend, "sqlite", "测试必须跑在隔离 sqlite 上")
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self.run = self.store.create_run(
            {"industry_pack_id": "auto", "question": QUESTION, "mode": "deep"},
            owner_user_id="p13", idempotency_key="phase13-pipeline")
        self.run_id = str(self.run["id"])
        self.saved = {name: os.environ.pop(name, None) for name in FLAGS}
        self.handlers = qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=_FakeRetriever(),
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

    def _context(self, *, answer=None, graph=None, events=None):
        graph = graph if graph is not None else fx.verified_graph_and_answer()[0]
        answer = answer if answer is not None else fx.verified_graph_and_answer()[1]
        return {
            "request": {"question": QUESTION, "industry_pack_id": "auto", "mode": "deep"},
            "run": {"id": self.run_id, "owner_user_id": "p13", "session_id": "s1",
                    "industry_pack_id": "auto"},
            "outputs": {"conflict_review": graph, "synthesis": dict(answer),
                        "level1_retrieval": {"evidence": graph.get("evidence") or [],
                                             "stats": {"adopted": len(graph.get("evidence") or []),
                                                       "gap_loop": {"stop_reason": "ANSWERABLE"}}},
                        "plan": {"question": QUESTION, "standalone_question": QUESTION,
                                 "queries": [QUESTION], "entities": ["家族办公室"]}},
            "_emit_stage_event": ((lambda name, payload: events.append({"type": name,
                                                                       "payload": payload}))
                                  if events is not None else None),
        }


class DefaultOffTests(_Base):
    def test_flag_off_returns_the_same_answer(self):
        context = self._context()
        result = self.handlers["citation_validation"](context)
        expected = dict(context["outputs"]["synthesis"])
        citations = list(expected.get("citations") or [])
        citations.extend(ref for claim in expected.get("claims") or []
                         for ref in claim.get("evidence_refs") or [])
        citations.extend(ref for conflict in expected.get("conflicts") or []
                         for ref in conflict.get("evidence_refs") or [])
        expected["citations"] = list(dict.fromkeys(citations))
        self.assertEqual(result, validate_final_answer(expected), "开关关着时逐字回到接线前")
        self.assertEqual(self.store.final_verification_rows(run_id=self.run_id), [])
        self.assertNotIn("final_verification", context["outputs"]["conflict_review"])

    def test_flag_off_emits_no_event(self):
        events = []
        self.handlers["citation_validation"](self._context(events=events))
        self.assertEqual([event for event in events
                          if "final_verification" in json.dumps(event, ensure_ascii=False)], [])


class FlagOnTests(_Base):
    def setUp(self):
        super().setUp()
        os.environ["QA_FINAL_VERIFIER"] = "1"

    def test_answer_is_final_verified_and_recorded(self):
        events = []
        context = self._context(events=events)
        result = self.handlers["citation_validation"](context)
        validate_final_answer(result)
        rows = self.store.final_verification_rows(run_id=self.run_id)
        self.assertEqual(len(rows), 1, "首轮终验必须写一行（零迁移复用 qa_stage_runs）")
        row = rows[0]
        self.assertEqual(row["stage"], "final_verify")
        self.assertEqual(row["round"], 1)
        self.assertEqual(row["outcome"], "pass")
        self.assertEqual(row["verified_claims"], 1)
        self.assertEqual(row["retrieval_calls"], 0)
        self.assertEqual(row["final_verifier_version"], fv.FINAL_VERIFIER_VERSION)
        self.assertEqual(row["status"], "completed")
        # 事件里带终验回执（Phase 16 的可观测性输入）
        receipt = [event["payload"]["final_verification"] for event in events
                   if isinstance(event.get("payload"), dict)
                   and "final_verification" in event["payload"]]
        self.assertEqual(len(receipt), 1)
        self.assertEqual(receipt[0]["outcome"], "pass")
        self.assertNotIn("answer", receipt[0], "回执不带正文")

    def test_graph_gets_a_sibling_key(self):
        context = self._context()
        self.handlers["citation_validation"](context)
        self.assertIn("final_verification", context["outputs"]["conflict_review"])
        self.assertEqual(context["outputs"]["conflict_review"]["final_verification"]["outcome"],
                         "pass")

    def test_retry_rounds_each_get_a_row(self):
        os.environ["QA_FINAL_VERIFY_RETRY_MAX"] = "1"
        graph, answer = fx.verified_graph_and_answer(status="unverified", verdict="UNVERIFIED")
        result = self.handlers["citation_validation"](self._context(graph=graph, answer=answer))
        validate_final_answer(result)
        rows = self.store.final_verification_rows(run_id=self.run_id)
        self.assertEqual([row["round"] for row in rows], [1, 2],
                         "首轮 + 局部重试后的复验各一行")
        self.assertEqual(rows[0]["outcome"], "fail")
        self.assertEqual(rows[1]["outcome"], "pass")
        self.assertEqual(rows[1]["retries"][0]["scope"], ["composer", "verifier"])
        self.assertEqual(rows[1]["retries"][0]["retrieval_calls"], 0)
        self.assertEqual(result["status"], "ready")

    def test_insufficient_evidence_changes_the_visible_answer(self):
        empty = {"contract_version": QA_CONTRACT_VERSION, "status": "ready",
                 "answer": "本轮没有取到证据。", "sections": {}, "claims": [], "conflicts": [],
                 "evidence": [], "citations": [], "citation_map": {}, "cutoff_at": "",
                 "degraded": False, "degradation_reasons": [], "models": {}}
        result = self.handlers["citation_validation"](
            self._context(answer=empty, graph={"claims": [], "evidence": [], "conflicts": [],
                                               "edges": []}))
        validate_final_answer(result)
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertTrue(result["answer"].startswith(fv.INSUFFICIENT_HEADER))
        rows = self.store.final_verification_rows(run_id=self.run_id)
        self.assertTrue(rows[-1]["insufficient"])
        self.assertIn("NO_VERIFIED_CLAIM", rows[-1]["insufficient_reasons"])

    def test_fast_path_rebuilds_the_graph_from_the_store(self):
        """快速路径没有 `conflict_review` 输出：按 Phase 06 的读侧口径从库里重建，并写明来源。"""
        self.store.persist_reasoning_graph(self.run_id, fx.verified_graph_and_answer()[0])
        context = self._context()
        context["outputs"].pop("conflict_review")
        result = self.handlers["citation_validation"](context)
        validate_final_answer(result)
        rows = self.store.final_verification_rows(run_id=self.run_id)
        self.assertEqual(rows[0]["stage"], "final_verify")
        self.assertTrue(rows[0]["claims"] >= 1, "重建出来的图必须真的喂进终验")

    def test_internal_failure_delivers_the_answer_anyway(self):
        """终验内部抛错（组装炸了）：答案原样交付，但必须**显式披露**，不许静默降级。"""
        context = self._context()
        with mock.patch.object(fv, "compose_answer", side_effect=RuntimeError("boom")):
            result = self.handlers["citation_validation"](context)
        validate_final_answer(result)
        self.assertEqual(result["answer"], context["outputs"]["synthesis"]["answer"])
        self.assertTrue(any(qa_pipeline.FINAL_VERIFY_FAILED_NOTE in reason
                            for reason in result["degradation_reasons"]),
                        result["degradation_reasons"])
        self.assertEqual(self.store.final_verification_rows(run_id=self.run_id), [],
                         "没跑完就不许写终验行（不许假装核验过）")

    def test_wiring_level_failure_also_delivers_the_answer(self):
        """接线层抛错（比终验更外层）：同一条兜底文案 + 同一份降级记录。"""
        context = self._context()
        with mock.patch.object(qa_pipeline, "_final_verification_step",
                               side_effect=RuntimeError("wiring")):
            result = self.handlers["citation_validation"](context)
        validate_final_answer(result)
        self.assertEqual(result["answer"], context["outputs"]["synthesis"]["answer"])
        self.assertTrue(any(qa_pipeline.FINAL_VERIFY_FAILED_NOTE in reason
                            for reason in result["degradation_reasons"]),
                        result["degradation_reasons"])
        run = self.store.get_run(self.run_id) or {}
        self.assertTrue(run.get("degradation"), "降级必须记进 run（可观测）")


if __name__ == "__main__":
    unittest.main()

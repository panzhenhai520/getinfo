#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 04 · 管线接线回归（level1_retrieval 上的舰队开关）。

要点（全部在隔离临时 sqlite 上真跑，不连真库、不调 LLM/嵌入端点）：
  1. **默认关**：`QA_HUNTER_FLEET` 不设 → 走既有 `ArticleRetriever.retrieve()`，
     阶段返回键集与 stats 键集必须与"没有舰队"逐字一致（零行为变化的硬证据）；
  2. **打开**：走并行舰队，`stats["hunter_fleet"]` 出现（兄弟键，Phase 02 冻结的
     `stats["evidence_layer"]` 键集一个字不动），证据包仍然过既有闸门/去重/证据层；
  3. **失败回落**：舰队抛错 → 自动回落到既有检索，证据包不受影响，回执里写明原因；
  4. **多跳不受影响**：`_run_multi_hop` 仍然拿既有 retriever（舰队只覆盖首跳）。
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_hunter_fleet as fleet_module  # noqa: E402
import qa_pipeline  # noqa: E402
from qa_phase04_corpus import DEFAULT_PACK, make_db, seed_standard_corpus  # noqa: E402
from qa_retrieval import ArticleRetriever  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

QUESTION = "香港家族办公室税收优惠政策对内地高净值客户有什么影响？"
SCOPE = {"owner_user_id": "p04", "session_id": "s1", "industry_pack_id": DEFAULT_PACK}

# 阶段返回键集（Phase 02 接线时的口径，Phase 04 不许动它）
LEVEL1_RECEIPT_KEYS = {
    "queries", "evidence", "excluded", "stats", "graph", "search_status",
    "search_providers", "search_errors", "time_window", "cache",
}
# Phase 02 冻结的证据层回执键集
EVIDENCE_RECEIPT_KEYS = {
    "evidence_layer", "annotated", "seen_dropped", "dedupe_dropped", "recorded", "reason",
}


class _FakeWebSearch:
    def search(self, queries, *, enabled=True, limit=8):
        return {"evidence": [], "status": "disabled", "providers": [], "errors": []}


class _BrokenFleet:
    """舰队桩：直接抛错，用来验证"回落既有检索"这条失败路径。"""

    def retrieve(self, plan, **kwargs):
        raise RuntimeError("舰队炸了")


class _Policy:
    ragflow_kb_id = "kb"
    ragflow_app_id = "app"
    max_evidence = 12
    max_queries_per_hop = 2
    standard_max_hops = 1
    deep_max_hops = 2
    research_timeout_seconds = 30


class _PolicyResolver:
    def resolve(self, industry_pack_id):
        return _Policy()

    def require_research_ready(self, industry_pack_id):
        return _Policy()


class FleetWiringTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = make_db(os.path.join(self.temp_dir.name, "phase04-pipeline.sqlite3"))
        self.ids = seed_standard_corpus(self.db, pack_id=DEFAULT_PACK)
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self.retriever = ArticleRetriever(self.db)
        self._saved_env = os.environ.pop("QA_HUNTER_FLEET", None)

    def tearDown(self):
        if self._saved_env is not None:
            os.environ["QA_HUNTER_FLEET"] = self._saved_env
        else:
            os.environ.pop("QA_HUNTER_FLEET", None)
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def _handlers(self, **kwargs):
        return qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=self.retriever, web_search=_FakeWebSearch(),
            store=self.store, policy_resolver=_PolicyResolver(), **kwargs)

    def _context(self):
        return {
            "request": {"question": QUESTION, "industry_pack_id": DEFAULT_PACK,
                        "mode": "standard", "page_context": {}},
            "run": {"id": "run-p04", **SCOPE},
            "outputs": {"plan": {
                "question": QUESTION, "standalone_question": QUESTION,
                "queries": [QUESTION], "entities": ["家族办公室"],
                "needs_local_articles": True, "needs_web": False,
            }},
        }

    def test_default_off_keeps_the_old_path_byte_for_byte(self):
        result = self._handlers()["level1_retrieval"](self._context())
        self.assertEqual(set(result.keys()), LEVEL1_RECEIPT_KEYS)
        self.assertNotIn("hunter_fleet", result["stats"],
                         "开关没开就不该出现舰队回执（回滚口径）")
        self.assertEqual(set(result["stats"]["evidence_layer"].keys()), EVIDENCE_RECEIPT_KEYS)
        self.assertTrue(result["evidence"], "旧路径本身要能取到证据，比较才有意义")

    def test_flag_on_runs_the_fleet_and_adds_a_sibling_receipt(self):
        os.environ["QA_HUNTER_FLEET"] = "1"
        result = self._handlers()["level1_retrieval"](self._context())
        self.assertEqual(set(result.keys()), LEVEL1_RECEIPT_KEYS,
                         "阶段返回键集被改动了（Phase 02 冻结）")
        fleet_receipt = result["stats"]["hunter_fleet"]
        self.assertEqual(fleet_receipt["hunters_total"], 5)
        self.assertIn("by_hunter", fleet_receipt)
        self.assertIn("counts", fleet_receipt)
        self.assertEqual(set(fleet_receipt["by_hunter"].keys()),
                         {"bm25", "semantic", "graph", "structured", "query_expansion"})
        self.assertEqual(set(result["stats"]["evidence_layer"].keys()), EVIDENCE_RECEIPT_KEYS,
                         "证据层回执键集不许被舰队影响")
        self.assertTrue(result["evidence"], "舰队路径也必须能取到证据")
        self.assertTrue(result["time_window"] is not None)

    def test_evidence_package_is_contract_valid_on_the_fleet_path(self):
        os.environ["QA_HUNTER_FLEET"] = "1"
        result = self._handlers()["level1_retrieval"](self._context())
        for item in result["evidence"]:
            layer = (item.get("metadata") or {}).get("evidence_layer")
            self.assertIsInstance(layer, dict, "舰队证据同样要过证据层标注")
            self.assertTrue(layer.get("span", {}).get("quote"))
        ids = [item.get("article_id") for item in result["evidence"] if item.get("article_id")]
        self.assertEqual(len(ids), len(set(ids)), "证据包里不许有重复文章")

    def test_injected_fleet_is_used_and_its_language_is_respected(self):
        calls = []

        class _Spy:
            def retrieve(self, plan, **kwargs):
                calls.append(dict(kwargs))
                return {"evidence": [], "stats": {"hunter_fleet": {"spy": True}},
                        "excluded": {}, "time_window": {}, "graph": {}, "hunters": []}

        result = self._handlers(hunter_fleet=_Spy())["level1_retrieval"](self._context())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["industry_pack_id"], DEFAULT_PACK)
        self.assertEqual(result["stats"]["hunter_fleet"], {"spy": True})

    def test_fleet_failure_falls_back_without_losing_evidence(self):
        result = self._handlers(hunter_fleet=_BrokenFleet())["level1_retrieval"](self._context())
        self.assertEqual(result["stats"]["hunter_fleet"]["fallback"],
                         "ArticleRetriever.retrieve")
        self.assertIn("舰队炸了", result["stats"]["hunter_fleet"]["error"])
        self.assertTrue(result["evidence"], "回落路径必须照常给出证据")

    def test_real_default_fleet_on_temp_corpus(self):
        """真装配（不走桩）：`build_default_fleet` 在管线里跑通一次。"""
        fleet = fleet_module.build_default_fleet(database=self.db, retriever=self.retriever)
        result = self._handlers(hunter_fleet=fleet)["level1_retrieval"](self._context())
        receipt = result["stats"]["hunter_fleet"]
        by_hunter = receipt["by_hunter"]
        self.assertEqual(by_hunter["bm25"]["status"], "ok")
        self.assertEqual(by_hunter["semantic"]["status"], "degraded",
                         "临时库没有向量 → 语义通道降级（真实现状）")
        self.assertTrue(receipt["pool"]["pool_rows"] >= 3)

    def test_multihop_still_uses_the_plain_retriever(self):
        """多跳每一跳仍走既有 retriever（舰队本阶段只覆盖首跳）——不许悄悄换掉它。"""
        hops = []

        class _RecordingRetriever(ArticleRetriever):
            def retrieve(self, plan, **kwargs):
                hops.append(str(plan.get("question") or ""))
                return super().retrieve(plan, **kwargs)

        retriever = _RecordingRetriever(self.db)
        handlers = qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=retriever, web_search=_FakeWebSearch(),
            store=self.store, policy_resolver=_PolicyResolver(),
            hunter_fleet=fleet_module.build_default_fleet(database=self.db, retriever=retriever))
        context = self._context()
        context["outputs"]["plan"]["decomposition"] = {
            "is_multi_hop": True, "pattern": "chain",
            "hops": [{"id": "h1", "question": QUESTION, "depends_on": [], "purpose": "主检索"},
                     {"id": "h2", "question": "香港家族办公室 利得税宽免 条件",
                      "depends_on": ["h1"], "purpose": "跳到宽免条件"}],
        }
        handlers["level1_retrieval"](context)
        self.assertIn("香港家族办公室 利得税宽免 条件", hops,
                      "多跳的后续跳必须仍由既有 retriever 执行")

    def test_fleet_receipt_does_not_leak_into_the_frozen_evidence_receipt(self):
        os.environ["QA_HUNTER_FLEET"] = "1"
        result = self._handlers()["level1_retrieval"](self._context())
        evidence_layer = result["stats"]["evidence_layer"]
        self.assertNotIn("hunter_fleet", evidence_layer)
        self.assertNotIn("hunters", evidence_layer)


if __name__ == "__main__":
    unittest.main()

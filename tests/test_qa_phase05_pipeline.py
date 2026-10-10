#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 05 · 管线接线用例（`QA_EXECUTION_GRAPH` 默认关）。

要点（全部在隔离临时 sqlite 上真跑，不调 LLM/嵌入端点）：
  1. **默认关 = 零行为变化**：阶段返回键集与 `stats` 键集必须与"没有执行图"逐字一致；
  2. **打开**：走执行图，`stats["execution_graph"]` 出现（**兄弟键**，Phase 02 冻结的
     `stats["evidence_layer"]` 六键与 Phase 03 的 `stats["verification"]` 一个字不动）；
  3. **节点落库**：`QA_EXECUTION_GRAPH_NODE_RUNS=1` 时 `qa_stage_runs` 出现 `node:<node_id>`
     行，`node_id`/`node_kind`/`parent_node_id` 都填上（Phase 01 的列）；
  4. **预算真的传下去**：图的 `hop_budget_seconds` 会传进 `_run_multi_hop`（超预算就停）；
  5. **失败路径**：建图抛错 → 回执里写明原因并回落到既有行为，证据包不受影响。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_execution_graph as graph_module  # noqa: E402
import qa_pipeline  # noqa: E402
from qa_phase04_corpus import DEFAULT_PACK, make_db, seed_standard_corpus  # noqa: E402
from qa_retrieval import ArticleRetriever  # noqa: E402
from qa_storage import QaStore  # noqa: E402

QUESTION = "香港家族办公室税收优惠政策对内地高净值客户有什么影响？"
SCOPE = {"owner_user_id": "p05", "session_id": "s1", "industry_pack_id": DEFAULT_PACK}

LEVEL1_RECEIPT_KEYS = {
    "queries", "evidence", "excluded", "stats", "graph", "search_status",
    "search_providers", "search_errors", "time_window", "cache",
}
EVIDENCE_RECEIPT_KEYS = {
    "evidence_layer", "annotated", "seen_dropped", "dedupe_dropped", "recorded", "reason",
}
GRAPH_FLAGS = ("QA_EXECUTION_GRAPH", "QA_EXECUTION_GRAPH_NODE_RUNS",
               "QA_EXECUTION_GRAPH_BUDGET_SECONDS", "QA_HUNTER_FLEET")


class _FakeWebSearch:
    def search(self, queries, *, enabled=True, limit=8):
        return {"evidence": [], "status": "disabled", "providers": [], "errors": []}


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


class GraphWiringTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = make_db(os.path.join(self.temp_dir.name, "phase05-pipeline.sqlite3"))
        self.ids = seed_standard_corpus(self.db, pack_id=DEFAULT_PACK)
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self.retriever = ArticleRetriever(self.db)
        self.run = self.store.create_run(
            {"industry_pack_id": DEFAULT_PACK, "question": QUESTION, "mode": "standard"},
            owner_user_id="p05", idempotency_key="phase05-pipeline")
        self._saved = {name: os.environ.pop(name, None) for name in GRAPH_FLAGS}

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

    def _handlers(self, **kwargs):
        return qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=self.retriever, web_search=_FakeWebSearch(),
            store=self.store, policy_resolver=_PolicyResolver(), **kwargs)

    def _context(self, question=QUESTION):
        return {
            "request": {"question": question, "industry_pack_id": DEFAULT_PACK,
                        "mode": "standard", "page_context": {}},
            "run": {"id": str(self.run["id"]), **SCOPE},
            "outputs": {"plan": {
                "question": question, "standalone_question": question,
                "queries": [question], "entities": ["家族办公室"],
                "needs_local_articles": True, "needs_web": False,
                "category": {"key": "multi_hop", "label": "多跳传导类"},
            }},
        }

    def test_default_off_keeps_the_old_path_byte_for_byte(self):
        result = self._handlers()["level1_retrieval"](self._context())
        self.assertEqual(set(result.keys()), LEVEL1_RECEIPT_KEYS)
        self.assertNotIn("execution_graph", result["stats"],
                         "开关没开就不该出现执行图回执（回滚口径）")
        self.assertEqual(set(result["stats"]["evidence_layer"].keys()), EVIDENCE_RECEIPT_KEYS)
        self.assertTrue(result["evidence"], "旧路径本身要能取到证据，比较才有意义")

    def test_flag_on_adds_a_sibling_receipt(self):
        os.environ["QA_EXECUTION_GRAPH"] = "1"
        result = self._handlers()["level1_retrieval"](self._context())
        self.assertEqual(set(result.keys()), LEVEL1_RECEIPT_KEYS,
                         "阶段返回键集被改动了（Phase 02 冻结）")
        receipt = result["stats"]["execution_graph"]
        self.assertEqual(receipt["path"], "standard")
        self.assertTrue(receipt["contract_ok"])
        self.assertGreaterEqual(receipt["node_counts"]["runnable"], 5)
        self.assertTrue(receipt["budget"]["per_node"])
        self.assertTrue(receipt["stop_reason"])
        self.assertEqual(receipt["hop_count"], 3, "该问题在既有分解器下是 3 跳")
        self.assertTrue(receipt["dag_ok"])
        self.assertEqual(set(result["stats"]["evidence_layer"].keys()), EVIDENCE_RECEIPT_KEYS,
                         "证据层回执键集不许被执行图影响")
        self.assertTrue(result["evidence"])
        # 运行期账本：检索/核验/重排跑过，节点都记上了
        runtime = receipt["runtime"]
        self.assertGreaterEqual(runtime["executed_count"], 3)
        executed = {item["node_id"] for item in runtime["nodes"] if item["status"] != "pending"}
        self.assertIn("standard.retrieve", executed)
        self.assertIn("standard.verify", executed)
        self.assertIn("standard.rerank", executed)

    def test_hop_receipts_land_in_the_ledger(self):
        os.environ["QA_EXECUTION_GRAPH"] = "1"
        context = self._context()
        context["outputs"]["plan"]["decomposition"] = {
            "is_multi_hop": True, "pattern": "impact_chain",
            "hops": [{"id": "h1", "question": QUESTION, "depends_on": [], "purpose": "主检索"},
                     {"id": "h2", "question": "香港家族办公室 利得税宽免 条件",
                      "depends_on": ["h1"], "purpose": "跳到宽免条件"},
                     {"id": "h3", "question": "内地高净值客户 影响",
                      "depends_on": ["h2"], "purpose": "受影响方"}],
        }
        result = self._handlers()["level1_retrieval"](context)
        runtime = result["stats"]["execution_graph"]["runtime"]
        executed = {item["node_id"] for item in runtime["nodes"] if item["status"] != "pending"}
        self.assertIn("standard.hop.h2", executed)
        self.assertIn("standard.hop.h3", executed)
        self.assertEqual(result["stats"]["execution_graph"]["hop_count"], 3)

    def test_node_runs_are_written_only_when_asked(self):
        os.environ["QA_EXECUTION_GRAPH"] = "1"
        self._handlers()["level1_retrieval"](self._context())
        rows = self.store.stage_runs(str(self.run["id"]))
        self.assertFalse([row for row in rows if str(row.get("stage") or "").startswith("node:")],
                         "没开 node-runs 开关就不许写节点行")

    def test_node_runs_land_in_phase01_columns(self):
        os.environ["QA_EXECUTION_GRAPH"] = "1"
        os.environ["QA_EXECUTION_GRAPH_NODE_RUNS"] = "1"
        result = self._handlers()["level1_retrieval"](self._context())
        node_runs = result["stats"]["execution_graph"]["node_runs"]
        self.assertGreaterEqual(node_runs["written_count"], 3)
        rows = [row for row in self.store.stage_runs(str(self.run["id"]))
                if str(row.get("stage") or "").startswith("node:")]
        by_node = {row["node_id"]: row for row in rows}
        self.assertIn("standard.retrieve", by_node)
        self.assertEqual(by_node["standard.retrieve"]["node_kind"], "retrieve")
        self.assertEqual(by_node["standard.verify"]["node_kind"], "verify")
        self.assertTrue(all(str(row["node_id"]) for row in rows))

    def test_hop_budget_from_the_graph_is_passed_down(self):
        """图的 hop_budget_seconds 必须真的进 `_run_multi_hop`（否则预算只是纸面数字）。"""
        os.environ["QA_EXECUTION_GRAPH"] = "1"
        seen = {}
        original = qa_pipeline._run_multi_hop

        def _spy(*args, **kwargs):
            seen.update(kwargs)
            return original(*args, **kwargs)

        with mock.patch.object(qa_pipeline, "_run_multi_hop", side_effect=_spy):
            context = self._context()
            context["outputs"]["plan"]["decomposition"] = {
                "is_multi_hop": True, "pattern": "impact_chain",
                "hops": [{"id": "h1", "question": QUESTION, "depends_on": [], "purpose": "主检索"},
                         {"id": "h2", "question": "香港家族办公室 利得税宽免 条件",
                          "depends_on": ["h1"], "purpose": "跳到宽免条件"}],
            }
            result = self._handlers()["level1_retrieval"](context)
        self.assertIn("budget_seconds", seen, "执行图打开时多跳预算必须由图给出")
        self.assertEqual(seen["budget_seconds"],
                         result["stats"]["execution_graph"]["budget"]["hop_budget_seconds"])

    def test_graph_failure_falls_back_without_losing_evidence(self):
        os.environ["QA_EXECUTION_GRAPH"] = "1"

        def _boom(*args, **kwargs):
            raise RuntimeError("建图炸了")

        with mock.patch.object(graph_module, "build_execution_graph", side_effect=_boom):
            result = self._handlers()["level1_retrieval"](self._context())
        receipt = result["stats"]["execution_graph"]
        self.assertIn("建图炸了", receipt["error"])
        self.assertEqual(receipt["fallback"], "无执行图（既有行为不变）")
        self.assertTrue(result["evidence"], "建图失败必须不影响证据包")
        self.assertTrue(result["stats"]["evidence_layer"])

    def test_bad_policy_resolver_does_not_break_the_graph(self):
        os.environ["QA_EXECUTION_GRAPH"] = "1"

        class _BrokenResolver:
            def resolve(self, industry_pack_id):
                raise RuntimeError("行业包读不到")

            def require_research_ready(self, industry_pack_id):
                raise RuntimeError("行业包读不到")

        handlers = qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=self.retriever, web_search=_FakeWebSearch(),
            store=self.store, policy_resolver=_BrokenResolver())
        result = handlers["level1_retrieval"](self._context())
        receipt = result["stats"]["execution_graph"]
        self.assertTrue(receipt["contract_ok"], "取不到 policy 也要能出图（用默认预算）")
        self.assertEqual(receipt["budget"]["policy_ceiling_seconds"], 0.0)

    def test_graph_budget_env_is_respected(self):
        os.environ["QA_EXECUTION_GRAPH"] = "1"
        os.environ["QA_EXECUTION_GRAPH_BUDGET_SECONDS"] = "240"
        result = self._handlers()["level1_retrieval"](self._context())
        receipt = result["stats"]["execution_graph"]
        self.assertEqual(receipt["budget"]["total_seconds"], 240.0)

    def test_no_endpoint_or_model_calls_in_the_graph_module(self):
        """硬约束复核：新模块不许出现外部调用痕迹。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "qa_execution_graph.py"), encoding="utf-8") as handle:
            source = handle.read()
        for token in ("http://", "https://", "requests.get", "requests.post", "openai",
                      "/v1/chat", "_embed_question"):
            self.assertNotIn(token, source, "执行图模块里出现外部调用痕迹：%s" % token)


if __name__ == "__main__":
    unittest.main()

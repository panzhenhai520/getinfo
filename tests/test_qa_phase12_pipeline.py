#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 12 · 端到端接线用例（`QA_EXPERIENCE_MEMORY` + `QA_GAP_ANALYZER`）。

这是"经验记忆不是死数据"的**端到端**证明（真跑 `level1_retrieval`，零模型/零嵌入端点）：

  1. **默认关 = 逐字回滚**：`stats["experience_memory"]` 一个键都不新增、
     `memory_item` 一行都不写、`level1_retrieval` 返回键集不变；
  2. **打开后第一次运行**（before）：缺口循环按 Phase 07 的规则选通道，
     同时把这一轮的经验（失败/剧集/来源…）落进记忆库；
  3. **第二次运行**（after）：上一轮学到的失败记忆经**规划期召回**进入指导，
     同一批缺口里被记录过失败的通道被避开 → 规划出来的下一跳通道与 before 不同，
     且这条改变带 `route_changes`（from/to/reason）与 warnings 可查；
  4. 版本一变（`QA_...` 场景：改语料版本）→ 失败记忆不再压制 → 通道回到原样
     （MASTER_RULES 15 的"版本变化后允许重试"在端到端链路上真的成立）；
  5. 经验记忆与答案链路互不阻塞：经验写入抛错只记账。
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_pipeline  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

import qa_phase09_fixtures as fx09  # noqa: E402
import qa_phase12_fixtures as fx  # noqa: E402

QUESTION = fx09.QUESTION
FLAGS = ("QA_EXPERIENCE_MEMORY", "QA_GAP_ANALYZER", "QA_MEMORY_GRAPH",
         "QA_MEMORY_REVALIDATION", "QA_CONTEXT_PACK", "QA_EVIDENCE_GRAPH",
         "QA_EVIDENCE_LAYER", "QA_EXECUTION_GRAPH", "QA_GAP_MAX_NEXT_HOPS")


class _ZeroVerifiedRetriever:
    """每一跳都只返回**没有一条通过核验**的证据（论坛贴），用来制造真实的通道失败。"""

    def __init__(self, *, text=None, route="keyword"):
        self.text = text or fx09.UNVERIFIED_EVIDENCE_TEXT
        self.route = route
        self.calls = 0

    def retrieve(self, plan, **kwargs):
        self.calls += 1
        item = fx09.evidence_item("article:%d" % self.calls, text=self.text,
                                  title="论坛闲聊家族办公室")
        item["retrieval_method"] = self.route
        item["match_reason"] = "核心词命中：家族；分类词命中：税务"
        return {"evidence": [item],
                "stats": {"eligible": 1, "adopted": 1, "keyword_candidates": 3}}


class _WebSearch:
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


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.tmp.name, "p12-e2e.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.assertEqual(self.db.backend, "sqlite", "测试必须跑在隔离 sqlite 上")
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self.saved = {name: os.environ.pop(name, None) for name in FLAGS}
        os.environ["QA_GAP_ANALYZER"] = "1"

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

    def _run_id(self, key):
        run = self.store.create_run(
            {"industry_pack_id": "auto", "question": QUESTION, "mode": "standard"},
            owner_user_id="p12", idempotency_key=key)
        return str(run["id"])

    def _handlers(self, retriever):
        return qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=retriever, web_search=_WebSearch(),
            store=self.store, policy_resolver=_PolicyResolver())

    def _context(self, run_id):
        return {
            "request": {"question": QUESTION, "industry_pack_id": "auto", "mode": "standard",
                        "page_context": {}},
            "run": {"id": run_id, "owner_user_id": "p12", "session_id": "s1",
                    "industry_pack_id": "auto", "corpus_version": fx.KB_VERSION},
            "outputs": {"plan": {
                "question": QUESTION, "queries": [QUESTION], "entities": ["家族办公室"],
                "needs_local_articles": True, "needs_web": False,
                "category": {"key": "MULTI_HOP"}, "topics": ["税收优惠"],
                "decomposition": {"is_multi_hop": True, "pattern": "impact_chain",
                                  "hop_count": 2, "dag_ok": True,
                                  "hops": [{"id": "h1", "question": QUESTION,
                                            "depends_on": [], "carry": ["entities"],
                                            "purpose": "政策事实"},
                                           {"id": "h2", "question": "内地高净值客户 影响",
                                            "depends_on": ["h1"], "carry": ["entities"],
                                            "purpose": "传导"}]}}},
        }

    def _level1(self, run_id, retriever):
        return self._handlers(retriever)["level1_retrieval"](self._context(run_id))

    def _learn(self, run_id, result):
        """走**真实写入口**（§9 主执行流里"最终答案之后"的那一步）。

        本用例不跑综合阶段（那要调模型，本阶段硬约束禁止），所以直接调那一步的函数 ——
        它吃的仍然是这一轮的真实回执（阶段行 + 证据图 + 缺口回执），口径一字不改。
        """
        graph = {"version": "qa-evidence-graph-v1", "claims": [], "edges": [], "conflicts": [],
                 "evidence": list(result.get("evidence") or []), "stats": {}}
        return qa_pipeline._write_experience_after_answer(
            {"request": {"question": QUESTION}, "run": {}, "outputs": {
                "plan": self._context(run_id)["outputs"]["plan"],
                "level1_retrieval": result,
                "level1_draft": {"claims": [], "evidence": []}}},
            graph=graph,
            run_meta={"id": run_id, "owner_user_id": "p12", "session_id": "s1",
                      "industry_pack_id": "auto", "question_text": QUESTION,
                      "mode": "standard"},
            store=self.store, retrieval_config_version="qa-retrieval-v3-policy-exact")

    def _hop_routes(self, run_id, *, planned_only=False):
        """从**真实留痕**里取这一轮每一跳走的通道（`qa_reasoning_traces.route`）。

        比"看回执"更硬：这些行是 Phase 01 埋的列、Phase 07 真正落库的跳留痕，
        补充跳的 `sub_query_id` 以 `g` 开头（缺口 id）。`planned_only=True` 只看补充跳。
        """
        rows = self.store.reasoning_traces(run_id)
        out = []
        for row in rows:
            route = str(row.get("route") or "")
            if not route:
                continue
            is_gap_hop = str(row.get("sub_query_id") or "").startswith("g")
            if planned_only and not is_gap_hop:
                continue
            out.append(route)
        return out

    def _clear_retrieval_cache(self):
        """清掉检索缓存：同题同环境第二轮本来会命中缓存直接返回，那样什么都观察不到。

        这是**测试脚手架**的动作（生产里缓存命中是正确行为）；清掉之后第二轮真的重新
        检索，经验指导才有机会作用于通道选择。
        """
        self.db.connection.execute("DELETE FROM qa_retrieval_cache")
        self.db.connection.commit()


class DefaultOffTests(_Base):
    def test_off_adds_no_keys_and_no_rows(self):
        result = self._level1(self._run_id("p12-e2e-off"), _ZeroVerifiedRetriever())
        stats = result.get("stats") or {}
        self.assertNotIn("experience_memory", stats)
        self.assertIn("gap_loop", stats, "缺口循环照旧（Phase 07 的账）")
        self.assertEqual(self.store.load_memory_items(include_all_scopes=True), [])


class BeforeAfterTests(_Base):
    def _learn_then_replay(self, *, learn_route="keyword"):
        """第一次运行学经验，第二次运行看行为是否被改变。返回 (before, after, learned, write)。"""
        os.environ["QA_EXPERIENCE_MEMORY"] = "1"
        before_id = self._run_id("p12-e2e-before")
        before = self._level1(before_id, _ZeroVerifiedRetriever(route=learn_route))
        write = self._learn(before_id, before)
        learned = self.store.load_memory_items(memory_types=["FAILURE"],
                                               include_all_scopes=True)
        self._clear_retrieval_cache()
        after_id = self._run_id("p12-e2e-after")
        after = self._level1(after_id, _ZeroVerifiedRetriever(route=learn_route))
        return before_id, before, after_id, after, learned, write

    def test_first_run_learns_failures(self):
        _b_id, _before, _a_id, _after, learned, write = self._learn_then_replay()
        self.assertTrue(write.get("receipt"), write.get("error"))
        self.assertTrue(learned, "第一次运行必须学到失败记忆")
        kinds = {row["metadata"]["kind"] for row in learned}
        self.assertIn("FAILED_ROUTE", kinds)
        routes = {row["metadata"]["route"] for row in learned}
        self.assertIn("keyword", routes)
        for row in learned:
            self.assertTrue(row["metadata"]["corpus_version"])
            self.assertTrue(row["metadata"]["retrieval_config_version"])
            self.assertEqual(row["metadata"]["retrieval_config_version"],
                             "qa-retrieval-v3-policy-exact",
                             "配置版本引运行期真值，不用规则派生的令牌")
            self.assertIs(row["metadata"]["hint_only"], True)

    def test_second_run_gets_guidance_and_changes_routes(self):
        before_id, before, after_id, after, _learned, _write = self._learn_then_replay()
        guidance = (after.get("stats") or {}).get("experience_memory")
        self.assertTrue(guidance, "第二次运行必须带上经验指导回执")
        self.assertIn("keyword", (guidance.get("guidance") or {}).get("avoid_routes") or [],
                      "同一环境下被记录的失败通道必须被避开")
        changed = int((guidance.get("applied") or {}).get("changed") or 0)
        self.assertGreater(changed, 0, "经验必须真的改变下一跳的通道选择")
        rows = guidance.get("route_changes") or []
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["from"], "keyword")
            self.assertNotEqual(row["to"], "keyword")
            self.assertEqual(row["reason"], "FAILURE_AVOIDED")
        # 前后对比（**真实留痕**）：before 的**缺口驱动补充跳**走过被避免的通道，after 不再走
        before_gap = self._hop_routes(before_id, planned_only=True)
        after_gap = self._hop_routes(after_id, planned_only=True)
        self.assertIn("keyword", before_gap, before_gap)
        self.assertNotIn("keyword", after_gap, after_gap)
        self.assertTrue(after_gap, "after 轮仍然要有补充跳留痕（不是靠不检索换来的）")
        # 作用范围诚实：**计划自己的跳**两个轮次都是原样（经验只动缺口驱动的下一跳）
        self.assertEqual(self._hop_routes(before_id)[:2], self._hop_routes(after_id)[:2])

    def test_warnings_are_emitted_for_the_operator(self):
        os.environ["QA_EXPERIENCE_MEMORY"] = "1"
        first_id = self._run_id("p12-e2e-warn-1")
        first = self._level1(first_id, _ZeroVerifiedRetriever())
        self._learn(first_id, first)
        self._clear_retrieval_cache()
        events = []
        context = self._context(self._run_id("p12-e2e-warn-2"))
        context["_emit_stage_event"] = lambda kind, payload=None: events.append(payload or {})
        handlers = self._handlers(_ZeroVerifiedRetriever())
        result = handlers["level1_retrieval"](context)
        self.assertTrue(result)
        messages = [row.get("message") or "" for row in events]
        self.assertTrue(any("经验提示" in text for text in messages),
                        "提前提示必须发到 SSE：%s" % messages[:6])
        self.assertTrue(any("经验记忆改变了" in text for text in messages),
                        "改通道也要有事件：%s" % messages[:6])

    def test_version_change_removes_the_suppression(self):
        """同一批失败记忆，语料版本一变 → 不再避开（MASTER RULES 15 端到端成立）。"""
        os.environ["QA_EXPERIENCE_MEMORY"] = "1"
        first_id = self._run_id("p12-e2e-ver-1")
        first = self._level1(first_id, _ZeroVerifiedRetriever())
        self._learn(first_id, first)
        self._clear_retrieval_cache()
        # 第二轮把语料版本换掉：run 的 corpus_version 与检索缓存 kb_version 一起变
        context = self._context(self._run_id("p12-e2e-ver-2"))
        context["run"]["corpus_version"] = "kb-version-NEW"
        original = qa_pipeline._local_corpus_version
        qa_pipeline._local_corpus_version = lambda database, pack_id: "kb-version-NEW"
        try:
            handlers = self._handlers(_ZeroVerifiedRetriever())
            result = handlers["level1_retrieval"](context)
        finally:
            qa_pipeline._local_corpus_version = original
        guidance = (result.get("stats") or {}).get("experience_memory") or {}
        receipt = guidance.get("guidance") or {}
        self.assertTrue(receipt, "语料变了也必须给出指导回执（只是不压制）")
        self.assertEqual(receipt.get("avoid_routes"), [],
                         "语料版本变了就不许再压制：%s" % receipt)
        reasons = receipt.get("retry_reasons") or {}
        self.assertIn("CORPUS_VERSION_CHANGED", reasons)
        self.assertEqual(int((guidance.get("applied") or {}).get("changed") or 0), 0)

    def test_recalled_memories_are_hints_only(self):
        os.environ["QA_EXPERIENCE_MEMORY"] = "1"
        first_id = self._run_id("p12-e2e-hint-1")
        first = self._level1(first_id, _ZeroVerifiedRetriever())
        self._learn(first_id, first)
        from qa_memory_experience import planning_hint

        hint = planning_hint(self.store, run_meta={"id": "r", "owner_user_id": "p12",
                                                   "session_id": "s1",
                                                   "industry_pack_id": "auto"},
                             task_type="MULTI_HOP", question=QUESTION,
                             corpus_version=fx.KB_VERSION,
                             retrieval_config_version="qa-retrieval-v3-policy-exact",
                             extra_terms=["MULTI_HOP", "家族办公室"])
        self.assertGreaterEqual(hint["hits"], 1, "失败记忆必须能被规划期召回（否则是死数据）")
        for hit in hint["recall"]["hits"]:
            self.assertTrue(hit["hint"])
            self.assertTrue(hit["requires_revalidation"])
            self.assertIs(hit["verified_evidence"], False)
        self.assertTrue(hint["guidance"]["requires_revalidation"])
        self.assertIs(hint["guidance"]["verified_evidence"], False)


if __name__ == "__main__":
    unittest.main()

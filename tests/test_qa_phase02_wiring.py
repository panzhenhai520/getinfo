#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 02 · 缺口 1（多跳逐跳 + level2 接入证据层）的接线验收。

单测证明"函数对"，这里证明"**另外两条路径也真的接上了**"（全部用隔离临时 sqlite + 桩，
不连真库、不调 LLM）：
  1. 多跳**每一跳**取回的证据都带 `metadata.evidence_layer`，且 provenance 保留逐跳口径
     （`stage=multi_hop` + route + round_index），不会被随后的整批标注覆盖掉；
  2. seen 身份按 (owner_user_id, session_id, industry_pack_id) 落库；逐跳候选登记成中性
     `seen`（没过闸门，不冒充 confirmed）；
  3. 跨轮去重：上一轮被拒的来源，在多跳的下一轮里同样被拦住，且身份仍是 rejected；
  4. 回执结构一字不变：阶段返回键集与接线前**逐字相同**，多跳回执只并入既有
     `stats["evidence_layer"]`；拿不到作用域三元组时只记 `skipped_scope`，绝不报错；
  5. level2（RAGFlow 研究）：产出的证据同样标注（route=semantic）+ 按作用域登记 seen。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_evidence as evidence_layer  # noqa: E402
import qa_pipeline  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

SCOPE = {"owner_user_id": "u1", "session_id": "s1", "industry_pack_id": "health"}
QUESTION = "医保新规对民营医院有什么影响？"

# 接线前的阶段返回键集（缺口 1 不许动它：多跳/level2 的回执只并入既有 stats["evidence_layer"]）
LEVEL1_RECEIPT_KEYS = {
    "queries", "evidence", "excluded", "stats", "graph", "search_status",
    "search_providers", "search_errors", "time_window", "cache",
}
# 成功路径的 level2 返回键集（同上，逐字比对）
LEVEL2_RECEIPT_KEYS = {
    "queries", "query_trace", "evidence", "excluded", "stats", "kb_status",
    "request_ids", "health", "cache", "rag_mode", "enhanced",
}
# 接线前的 stats["evidence_layer"] 键集：只有真的跳过登记时才多一条 skipped_scope
EVIDENCE_RECEIPT_KEYS = {
    "evidence_layer", "annotated", "seen_dropped", "dedupe_dropped", "recorded", "reason",
}

HOP2 = "医保新规 民营医院 报销比例"
HOP3 = "医保新规 结算细则 定点医院"


def _evidence(ref, *, title=None, content=None, article_id=None, **overrides):
    """与 tests/test_qa_phase02_pipeline.py 同口径的证据桩（正文命中问题实词，能过相关性闸门）。"""
    article_id = article_id if article_id is not None else int(str(ref).split(":")[-1])
    item = {
        "evidence_ref": ref,
        "source_type": "article",
        "title": title or ("医保新规对民营医院的影响 %s" % ref),
        "source_url": "https://example.com/%s" % ref,
        "article_id": article_id,
        "content_excerpt": content or "医保新规对民营医院的影响：报销比例下调，结算方式调整。",
        "published_at": "2026-10-01",
        "authority_level": 60,
        "score": 30.0,
        "retrieval_method": "keyword",
        "match_reason": "标题命中：医保新规",
        "relationship": "supports",
        "metadata": {"matched_keywords": ["医保新规"]},
    }
    item.update(overrides)
    return item


class _HopRetriever:
    """按 plan["question"] 分跳返回不同证据的检索桩（顺带记录每次调用）。"""

    def __init__(self, by_question):
        self.by_question = dict(by_question)
        self.calls = []

    def retrieve(self, plan, **kwargs):
        question = str(plan.get("question") or "")
        self.calls.append(question)
        items = [dict(item) for item in self.by_question.get(question, [])]
        return {"evidence": items, "stats": {"eligible": len(items), "adopted": len(items)}}


class _FakeWebSearch:
    def search(self, queries, *, enabled=True, limit=8):
        return {"evidence": [], "status": "disabled", "providers": [], "errors": []}


class _FakePolicy:
    ragflow_kb_id = "kb-test"
    ragflow_app_id = "app-test"
    max_evidence = 12
    max_queries_per_hop = 2
    standard_max_hops = 1
    deep_max_hops = 2
    research_timeout_seconds = 30


class _FakePolicyResolver:
    def resolve(self, industry_pack_id):
        return _FakePolicy()

    def require_research_ready(self, industry_pack_id):
        return _FakePolicy()


class _NoKbPolicyResolver:
    """没配知识库的策略桩：`resolve` 抛错 → `_kb_not_configured` 判 True。"""

    def resolve(self, industry_pack_id):
        raise RuntimeError("no kb config")

    def require_research_ready(self, industry_pack_id):
        raise RuntimeError("no kb config")


class _FakeRagflowClient:
    """RAGFlow 客户端桩：只回假 chunk，不发任何网络请求。"""

    def __init__(self, chunks):
        self.kb_id = "kb-test"
        self.chunks = list(chunks)
        self.queries = []

    def health_check(self):
        return {"ready": True, "kb_version": "kb-test-v1"}

    def search_dataset(self, query, top_n=8, threshold=0.2):
        self.queries.append(str(query))
        return {"chunks": [dict(chunk) for chunk in self.chunks], "request_id": "req-search"}

    def dataset_status(self):
        return {"total": len(self.chunks), "parsing": 0, "request_id": "req-status"}


class _FakeFeatureFlags:
    def __init__(self, snapshot=None):
        self._snapshot = dict(snapshot or {"level2_enabled": True})

    def snapshot(self):
        return dict(self._snapshot)


class _WiringBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "phase02-wiring.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.store = QaStore(self.db)
        self.store.ensure_schema()

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def _row(self, sql, params=()):
        return self.db.connection.execute(sql, tuple(params)).fetchone()

    def _count(self, sql, params=()):
        return int(self._row(sql, params)[0])


class MultiHopEvidenceLayerWiringTests(_WiringBase):
    """多跳每一跳的证据层接线（缺口 1）。"""

    HOP1_EVIDENCE = [_evidence("article:101", title="医保新规对民营医院的影响分析",
                               content="医保新规对民营医院的影响：报销比例下调。")]
    HOP2_EVIDENCE = [_evidence("article:202", title="医保新规报销比例调整说明",
                               content="医保新规对民营医院的报销比例与结算方式有新要求。")]
    HOP3_EVIDENCE = [_evidence("article:303", title="医保新规定点医院结算细则",
                               content="医保新规定点医院的结算细则明确，民营医院需重新申报。")]

    def setUp(self):
        super().setUp()
        self.retriever = _HopRetriever({
            QUESTION: self.HOP1_EVIDENCE,
            HOP2: self.HOP2_EVIDENCE,
            HOP3: self.HOP3_EVIDENCE,
        })
        self.handlers = qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=self.retriever, web_search=_FakeWebSearch(),
            store=self.store, feature_flags=_FakeFeatureFlags())

    def _context(self, run_id="run-1", *, run_meta=None, retriever=None):
        plan = {
            "question": QUESTION,
            "standalone_question": QUESTION,
            "queries": ["医保新规 民营医院"],
            "entities": ["医保新规"],
            "needs_local_articles": True,
            "needs_web": False,
            "decomposition": {
                "is_multi_hop": True,
                "pattern": "chain",
                "hops": [
                    {"id": "h1", "question": QUESTION, "depends_on": [], "purpose": "主检索"},
                    {"id": "h2", "question": HOP2, "depends_on": ["h1"], "purpose": "跳到报销比例"},
                    {"id": "h3", "question": HOP3, "depends_on": ["h2"], "purpose": "跳到结算细则"},
                ],
            },
        }
        return {
            "request": {"question": QUESTION, "industry_pack_id": "health",
                        "mode": "standard", "page_context": {}},
            "run": run_meta if run_meta is not None else {"id": run_id, **SCOPE},
            "outputs": {"plan": plan},
        }

    @staticmethod
    def _by_ref(result):
        return {str(item.get("evidence_ref") or ""): item for item in result["evidence"]}

    def test_every_hop_evidence_carries_hop_provenance(self):
        """每一跳都产生带证据层的证据，且 provenance 是**那一跳**的（不被整批标注覆盖）。"""
        result = self.handlers["level1_retrieval"](self._context())
        self.assertEqual(len(self.retriever.calls), 3, "三跳都要真的检索过")
        by_ref = self._by_ref(result)
        for ref in ("article:101", "article:202", "article:303"):
            self.assertIn(ref, by_ref, "跳 %s 的证据没进最终证据包" % ref)
            self.assertIn("evidence_layer", by_ref[ref]["metadata"])
        for ref in ("article:202", "article:303"):
            provenance = by_ref[ref]["metadata"]["evidence_layer"]["provenance"]
            self.assertEqual(provenance["stage"], "multi_hop",
                             "%s 的逐跳 provenance 被后一次整批标注覆盖了" % ref)
            self.assertEqual(provenance["run_id"], "run-1")
        # 第 1 跳复用主检索结果，走 level1 的整批标注（口径不变）
        self.assertEqual(
            by_ref["article:101"]["metadata"]["evidence_layer"]["provenance"]["stage"],
            "level1_retrieval")
        # 指纹与"现算"一致：同来源同问题得到同一个身份
        self.assertEqual(
            by_ref["article:202"]["metadata"]["evidence_layer"]["source_fingerprint"],
            evidence_layer.source_fingerprint(self.HOP2_EVIDENCE[0]))

    def test_stage_writes_seen_identities_in_scope(self):
        """seen 身份按作用域落库（第 2/3 跳的来源必须有身份）。"""
        self.handlers["level1_retrieval"](self._context())
        hop2_key = evidence_layer.source_fingerprint(self.HOP2_EVIDENCE[0])
        hop3_key = evidence_layer.source_fingerprint(self.HOP3_EVIDENCE[0])
        rows = self.db.connection.execute(
            "SELECT source_fingerprint, status FROM qa_evidence_seen"
            " WHERE owner_user_id='u1' AND session_id='s1' AND industry_pack_id='health'"
        ).fetchall()
        statuses = {str(row[0]): str(row[1]) for row in rows}
        self.assertIn(hop2_key, statuses, "逐跳候选没有登记 seen 身份")
        self.assertIn(hop3_key, statuses)
        self.assertGreaterEqual(len(statuses), 3)

    def test_hop_candidates_are_registered_as_neutral_seen(self):
        """逐跳候选登记成中性 seen（没过闸门不冒充 confirmed）——直接验接线函数本身。"""
        audit: dict = {}
        kept = qa_pipeline._wire_hop_evidence_layer(
            self.HOP2_EVIDENCE, question=QUESTION, plan={},
            run_meta={"id": "run-hop", **SCOPE}, store=self.store, round_index=0,
            corpus_version="corpus-1", route="keyword", audit=audit)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["metadata"]["evidence_layer"]["provenance"]["stage"], "multi_hop")
        self.assertEqual(kept[0]["metadata"]["evidence_layer"]["provenance"]["route"], "keyword")
        key = evidence_layer.source_fingerprint(self.HOP2_EVIDENCE[0])
        row = self._row(
            "SELECT status, span_fingerprint, first_run_id, last_run_id,"
            " owner_user_id, session_id, industry_pack_id"
            " FROM qa_evidence_seen WHERE source_fingerprint=?", (key,))
        self.assertIsNotNone(row, "逐跳候选没有登记 seen 身份")
        self.assertEqual(str(row[0]), "seen")
        self.assertEqual((str(row[4]), str(row[5]), str(row[6])), ("u1", "s1", "health"))
        self.assertEqual(str(row[2]), "run-hop")
        self.assertTrue(str(row[1]), "span 指纹没落库")
        self.assertGreaterEqual(int(audit.get("recorded") or 0), 1)

    def test_seen_identity_does_not_leak_across_scopes(self):
        self.handlers["level1_retrieval"](self._context())
        self.assertEqual(self._count("SELECT COUNT(*) FROM qa_evidence_seen WHERE session_id='s2'"),
                         0, "seen 身份串到了别的会话")

    def test_previously_rejected_source_is_dropped_in_hop(self):
        """跨轮去重：上一轮被拒的来源，本轮多跳里同样被拦住（身份仍是 rejected）。"""
        junk = _evidence("article:909", title="活动花絮 点赞转发赢好礼")
        self.retriever.by_question[HOP2] = [junk]
        junk_key = evidence_layer.source_fingerprint(junk)
        self.store.record_seen_evidence(
            records=[{"source_fingerprint": junk_key, "source_type": "article",
                      "evidence_ref": junk["evidence_ref"], "status": "rejected"}],
            run_id="run-old", **SCOPE)

        result = self.handlers["level1_retrieval"](self._context("run-new"))

        self.assertNotIn("article:909", self._by_ref(result), "上一轮被拒的来源又进了证据包")
        self.assertGreaterEqual(result["stats"]["evidence_layer"]["seen_dropped"], 1,
                                "回执里没有记下跨轮去重的条数")
        status = self._row(
            "SELECT status FROM qa_evidence_seen WHERE source_fingerprint=?"
            " AND owner_user_id='u1' AND session_id='s1'", (junk_key,))
        self.assertEqual(str(status[0]), "rejected", "逐跳重见不得把 rejected 身份降级成 seen")

    def test_every_hop_writes_reasoning_trace_with_route_and_counts(self):
        """每一跳都要真的写进 qa_reasoning_traces，且带 route 与候选/采纳计数（Phase 01 F-7）。

        守的是真端到端踩到的坑：`_trace_recorder` 曾引用作用域里不存在的 `run`，
        每跳都撞 NameError 又被 `except Exception: return` 吞掉 → 留痕表永远是空的。
        """
        self.handlers["level1_retrieval"](self._context())
        rows = self.db.connection.execute(
            "SELECT hop_index,sub_query_id,route,results,accepted,rejected,status,round_index"
            " FROM qa_reasoning_traces ORDER BY round_index,hop_index").fetchall()
        self.assertEqual(len(rows), 3, "三跳必须各留一条痕（实际 %d 条）" % len(rows))
        for row in rows:
            self.assertTrue(str(row[2] or "").strip(), "留痕缺 route")
            self.assertGreaterEqual(int(row[3]), int(row[4]), "results 必须 >= accepted")
        self.assertEqual([int(row[0]) for row in rows], [0, 1, 2])
        self.assertEqual({str(row[1]) for row in rows}, {"h1", "h2", "h3"})

    def test_receipt_keys_are_unchanged_and_hop_audit_is_merged(self):
        """回执键集与接线前逐字相同；多跳回执并入既有 stats["evidence_layer"]。"""
        result = self.handlers["level1_retrieval"](self._context())
        self.assertEqual(set(result.keys()), LEVEL1_RECEIPT_KEYS)
        summary = result["stats"]["evidence_layer"]
        self.assertEqual(set(summary.keys()), EVIDENCE_RECEIPT_KEYS)
        self.assertEqual(summary["evidence_layer"], "qa-evidence-v1")
        # 逐跳标注 + 整批标注都算进去（所以不会小于最终证据条数）
        self.assertGreaterEqual(summary["annotated"], len(result["evidence"]))
        # 三跳（各登记一次）+ 整批（采纳 + 被拒）：登记条数至少覆盖最终证据包
        self.assertGreaterEqual(summary["recorded"], len(result["evidence"]))

    def test_missing_scope_records_skipped_scope_without_error(self):
        """拿不到作用域三元组：只标注、只记 skipped_scope，不报错、不落库。"""
        result = self.handlers["level1_retrieval"](
            self._context("run-noscope", run_meta={"id": "run-noscope"}))
        summary = result["stats"]["evidence_layer"]
        self.assertGreaterEqual(int(summary.get("skipped_scope") or 0), 1)
        self.assertEqual(set(summary.keys()), EVIDENCE_RECEIPT_KEYS | {"skipped_scope"})
        self.assertEqual(self._count("SELECT COUNT(*) FROM qa_evidence_seen"), 0)
        for item in result["evidence"]:
            self.assertIn("evidence_layer", item["metadata"], "跳过登记不等于跳过标注")

    def test_broken_store_never_breaks_multi_hop(self):
        """失败路径：store 完全坏掉时多跳照跑，证据一条不少，回执键集不变。"""
        broken = mock.Mock()
        broken.record_seen_evidence.side_effect = RuntimeError("db down")
        broken.seen_evidence.side_effect = RuntimeError("db down")
        handlers = qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=self.retriever, web_search=_FakeWebSearch(),
            store=broken, feature_flags=_FakeFeatureFlags())
        result = handlers["level1_retrieval"](self._context("run-broken"))
        self.assertEqual(set(result.keys()), LEVEL1_RECEIPT_KEYS)
        for ref in ("article:101", "article:202", "article:303"):
            self.assertIn(ref, self._by_ref(result), "store 坏掉时证据不许少")
        # 查不到 seen 就按"没见过"处理：不误丢证据
        self.assertEqual(result["stats"]["evidence_layer"]["seen_dropped"], 0)
        self.assertEqual(self._count("SELECT COUNT(*) FROM qa_evidence_seen"), 0)


class Level2EvidenceLayerWiringTests(_WiringBase):
    """level2（RAGFlow 研究）接入证据层（缺口 1）。"""

    CHUNKS = [{
        "content_with_weight": "医保新规对民营医院的影响：报销比例下调，定点医院结算方式调整，"
                               "民营医院需按新规重新申报。",
        "document_id": "doc-1",
        "chunk_id": "chunk-1",
        "document_name": "医保新规原文与解读",
        "url": "https://example.com/policy/1",
        "similarity": 0.93,
        "position": 0,
        "metadata": {"authority_level": 90},
    }]

    def setUp(self):
        super().setUp()
        self.client = _FakeRagflowClient(self.CHUNKS)
        self.handlers = qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=_HopRetriever({}), web_search=_FakeWebSearch(),
            store=self.store, feature_flags=_FakeFeatureFlags(),
            policy_resolver=_FakePolicyResolver(),
            ragflow_client_factory=lambda policy: self.client)

    def _context(self, run_id="run-l2"):
        return {
            "request": {"question": QUESTION, "industry_pack_id": "health",
                        "mode": "standard", "page_context": {}},
            "run": {"id": run_id, **SCOPE},
            "outputs": {
                "plan": {"question": QUESTION, "standalone_question": QUESTION,
                         "queries": ["医保新规 民营医院"], "needs_ragflow": True},
                "level1_draft": {"claims": [], "evidence": []},
            },
        }

    def test_ragflow_evidence_is_annotated_and_registered(self):
        result = self.handlers["level2_retrieval"](self._context())
        self.assertTrue(result["evidence"], "RAGFlow 证据被闸门全部筛掉了（桩数据要能过闸门）")
        seen_keys = []
        for item in result["evidence"]:
            layer = item["metadata"]["evidence_layer"]
            self.assertEqual(layer["provenance"]["stage"], "level2_retrieval")
            self.assertEqual(layer["provenance"]["route"], "semantic",
                             "RAGFlow dataset 检索必须归到 QA_ROUTE_SEMANTIC")
            self.assertEqual(layer["provenance"]["run_id"], "run-l2")
            self.assertTrue(layer["span"]["quote"])
            seen_keys.append(layer["source_fingerprint"])
        rows = self.db.connection.execute(
            "SELECT source_fingerprint, status FROM qa_evidence_seen"
            " WHERE owner_user_id='u1' AND session_id='s1' AND industry_pack_id='health'"
        ).fetchall()
        statuses = {str(row[0]): str(row[1]) for row in rows}
        self.assertTrue(set(seen_keys) & set(statuses), "level2 证据没有登记 seen 身份")
        self.assertEqual(self._count(
            "SELECT COUNT(*) FROM qa_evidence_seen WHERE source_type='ragflow_chunk'"), 1)

    def test_level2_receipt_keys_are_unchanged(self):
        result = self.handlers["level2_retrieval"](self._context())
        self.assertEqual(set(result.keys()), LEVEL2_RECEIPT_KEYS)
        self.assertEqual(set(result["stats"]["evidence_layer"].keys()), EVIDENCE_RECEIPT_KEYS)
        self.assertEqual(result["stats"]["evidence_layer"]["evidence_layer"], "qa-evidence-v1")

    def test_kb_not_configured_fallback_does_not_touch_evidence_layer(self):
        """没配知识库 → 走 PG fallback（不碰证据层登记），既有行为不变。"""
        handlers = qa_pipeline.build_qa_stage_handlers(
            database=self.db, article_retriever=_HopRetriever({}), web_search=_FakeWebSearch(),
            store=self.store, feature_flags=_FakeFeatureFlags(),
            policy_resolver=_NoKbPolicyResolver(),
            ragflow_client_factory=lambda policy: self.client)
        result = handlers["level2_retrieval"](self._context())
        self.assertFalse(result.get("enhanced"))
        self.assertEqual(result.get("evidence"), [])
        self.assertEqual(self._count("SELECT COUNT(*) FROM qa_evidence_seen"), 0)


class BatchDedupeKeepsRejectedIdentityTests(unittest.TestCase):
    """整批路径：被跨轮去重丢掉的来源必须**保持 rejected**，不能降级成中性 seen。

    为什么钉这个：early 版本把它登记成 witnessed=skipped（中性 seen），
    结果下一轮不再丢它、只能靠闸门再拒一次 —— 跨轮去重等于白做，
    而且与逐跳路径的口径不一致。这里用源码级断言防止被改回去。
    """

    def test_batch_path_records_skipped_as_rejected(self):
        import inspect

        import qa_pipeline

        source = inspect.getsource(qa_pipeline._apply_evidence_layer)
        # 只在"代码行"上断言（注释里会引用这句写法做说明，不能误伤）
        code_lines = [line for line in source.split("\n")
                      if not line.strip().startswith("#")]
        code = "\n".join(code_lines)
        self.assertIn("rejected=[*rejected_items, *skipped]", code,
                      "整批路径必须把被去重丢掉的来源按 rejected 再登记（保持身份）")
        self.assertNotIn("witnessed=skipped", code,
                         "整批路径不得把 skipped 登记成中性 seen（会把 rejected 降级）")

    def test_hop_path_also_keeps_rejected(self):
        import inspect

        import qa_pipeline

        source = inspect.getsource(qa_pipeline._apply_evidence_layer)
        self.assertIn("rejected=[*rejected_items, *skipped],\n                    witnessed=kept",
                      source.replace("\r\n", "\n"),
                      "逐跳路径同样是 rejected 再登记 + 通过者中性 seen")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 04 · P04-06 并行风扇 / 超时 / 重试 / 回退 / 预算 / 部分结果。

这里证明的是**编排语义**，不是某个 Hunter 的检索质量：
  1. 有界并发：`max_workers` 真的限制同时在跑的 Hunter 数（并发增益可测量）；
  2. 单 Hunter 超时不拖垮整条检索：超时 → 重试一次（§28 的 RETRY 1）→ 仍失败就标记降级；
  3. 失败隔离：一个 Hunter 抛错/超时，其它 Hunter 的证据照常返回；
  4. 回退（§28 FALLBACK）：语义通道降级时声明 fallback=bm25，兜底已在舰队里成功就标记"已被兜底"；
  5. 总预算 + 部分结果回退：预算耗尽 → `partial=True` / `stop_reason=BUDGET_EXHAUSTED`，
     已完成 Hunter 的证据照常返回，未完成的记 `budget_exhausted`；
  6. 扇入：既有四键去重口径 + 图证据名额（与既有 retrieve() 同一条公式）；
  7. 与既有 `retrieve()` 同形状的输出（键集一致 + 多一个 hunters）。
"""
import os
import sys
import tempfile
import time
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_hunter_fleet as fleet_module  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402
from qa_hunter_fleet import HunterFleet, HunterTask, merge_hunter_evidence  # noqa: E402
from qa_hunters import BaseHunter, hunter_outcome  # noqa: E402
from qa_phase04_corpus import (  # noqa: E402
    DEFAULT_PACK,
    add_classification,
    add_article,
    make_db,
    seed_standard_corpus,
)
from qa_retrieval import ArticleRetriever  # noqa: E402

QUESTION = "香港家族办公室税收优惠政策对内地高净值客户有什么影响？"


def _evidence(ref, *, article_id=None, score=30.0, title=None, content=None) -> dict:
    if article_id is None:
        tail = str(ref).split(":")[-1]
        article_id = int(tail) if tail.isdigit() else None
    return {
        "evidence_ref": ref,
        "source_type": "article",
        "title": title if title is not None else ("标题 %s" % ref),
        "source_url": "https://example.com/%s" % ref,
        "article_id": article_id,
        # 正文必须逐条不同：既有去重口径含"内容指纹"，同正文会被判重复（那不是这里要测的）
        "content_excerpt": (content if content is not None
                            else "正文 %s，讲的是家族办公室税收优惠。" % ref),
        "published_at": "2026-10-01",
        "authority_level": 50,
        "score": score,
        "retrieval_method": "keyword",
        "match_reason": "测试",
        "relationship": "supports",
        "metadata": {},
    }


class _FakeHunter(BaseHunter):
    """可控 Hunter：延迟 / 证据 / 抛错 / 降级 / 首次失败第二次成功。"""

    def __init__(self, hunter_id, *, delay=0.0, evidence=(), status="ok", error="",
                 raises=None, fallback="", fail_first=False):
        self.hunter_id = hunter_id
        self.delay = float(delay)
        self._evidence = [dict(item) for item in evidence]
        self.status = status
        self.error = error
        self.raises = raises
        self.fallback_hunter = fallback
        self.fail_first = bool(fail_first)
        self.calls = 0

    def run(self, request):
        self.calls += 1
        started = time.monotonic()
        if self.delay:
            time.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        latency = int((time.monotonic() - started) * 1000)
        if self.fail_first and self.calls == 1:
            return hunter_outcome(self.hunter_id, status="error", reason_code="first_attempt",
                                  error="首次失败", latency_ms=latency)
        return hunter_outcome(self.hunter_id, status=self.status, evidence=self._evidence,
                              error=self.error, latency_ms=latency,
                              reason_code="" if self.status in ("ok", "empty") else "degraded_test")


class _RealCorpusBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = make_db(os.path.join(self.temp_dir.name, "phase04-fleet.sqlite3"))
        self.retriever = ArticleRetriever(self.db)

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def request(self, **overrides):
        payload = {"question": QUESTION, "queries": [QUESTION], "entities": ["家族办公室"],
                   "needs_local_articles": True}
        kwargs = {"industry_pack_id": DEFAULT_PACK, "limit": 12}
        kwargs.update(overrides)
        return fleet_module.HunterRequest.from_plan(payload, **kwargs)


class FanOutTests(_RealCorpusBase):
    def test_hunters_run_in_parallel_with_a_bounded_pool(self):
        hunters = [_FakeHunter(hid, delay=0.25) for hid in ("bm25", "graph", "structured")]
        fleet = HunterFleet(hunters, max_workers=3, per_hunter_timeout=5.0, total_budget=10.0,
                            retries=0)
        result = fleet.run(self.request())
        stats = result["stats"]
        self.assertEqual(stats["counts"].get("ok"), 3)
        self.assertEqual(stats["max_workers"], 3)
        self.assertGreater(stats["parallelism_gain_ms"], 100,
                           "3×0.25s 的通道并发跑，墙钟必须明显小于串行和")
        self.assertGreaterEqual(stats["hunter_ms_sum"], 700)
        self.assertFalse(result["partial"])
        self.assertTrue(validate("hunter_fleet_result", result)[0])
    def test_worker_bound_is_respected(self):
        hunters = [_FakeHunter(hid, delay=0.2) for hid in ("bm25", "graph", "structured")]
        fleet = HunterFleet(hunters, max_workers=1, per_hunter_timeout=5.0, total_budget=10.0,
                            retries=0)
        started = time.monotonic()
        result = fleet.run(self.request())
        elapsed = time.monotonic() - started
        self.assertEqual(result["stats"]["max_workers"], 1)
        self.assertGreaterEqual(elapsed, 0.55, "并发度=1 时必须接近串行（0.6s）")
        self.assertLess(result["stats"]["parallelism_gain_ms"], 200)

    def test_timeout_retries_once_then_marks_degraded(self):
        hunter = _FakeHunter("bm25", delay=0.4)
        fleet = HunterFleet([hunter], max_workers=1, per_hunter_timeout=0.08,
                            total_budget=5.0, retries=1)
        result = fleet.run(self.request())
        outcome = result["hunters"][0]
        self.assertEqual(outcome["status"], "timeout")
        self.assertEqual(outcome["reason_code"], "hunter_timeout")
        self.assertEqual(outcome["attempts"], 2, "§28：超时先 RETRY 1")
        self.assertTrue(outcome["timed_out"])
        self.assertEqual(hunter.calls, 2, "重试必须真的再跑一次")

    def test_retry_can_succeed_on_the_second_attempt(self):
        hunter = _FakeHunter("bm25", fail_first=True, evidence=[_evidence("article:1")])
        fleet = HunterFleet([hunter], max_workers=1, per_hunter_timeout=2.0,
                            total_budget=5.0, retries=1)
        result = fleet.run(self.request())
        outcome = result["hunters"][0]
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["attempts"], 2)
        self.assertEqual(len(result["evidence"]), 1)

    def test_error_isolation_keeps_other_hunters_evidence(self):
        good = _FakeHunter("bm25", evidence=[_evidence("article:1")])
        boom = _FakeHunter("graph", raises=RuntimeError("图炸了"))
        result = HunterFleet([good, boom], max_workers=2, retries=0).run(self.request())
        by_id = {item["hunter_id"]: item for item in result["hunters"]}
        self.assertEqual(by_id["graph"]["status"], "error")
        self.assertIn("图炸了", by_id["graph"]["error"])
        self.assertEqual(by_id["bm25"]["status"], "ok")
        self.assertEqual(len(result["evidence"]), 1, "一个通道挂了不许丢别的通道的证据")
        self.assertEqual(result["stats"]["counts"]["error"], 1)

    def test_disabled_task_is_skipped(self):
        fleet = HunterFleet([HunterTask(_FakeHunter("bm25", evidence=[_evidence("article:1")]),
                                        enabled=False)],
                            max_workers=2, retries=0)
        result = fleet.run(self.request())
        self.assertEqual(result["hunters"][0]["status"], "skipped")
        self.assertEqual(result["evidence"], [])
        self.assertFalse(result["partial"])

    def test_budget_exhaustion_returns_partial_results(self):
        fast = _FakeHunter("bm25", evidence=[_evidence("article:1")])
        slow = _FakeHunter("graph", delay=0.8, evidence=[_evidence("edge:e1")])
        fleet = HunterFleet([fast, slow], max_workers=2, per_hunter_timeout=10.0,
                            total_budget=0.25, retries=0)
        result = fleet.run(self.request())
        self.assertTrue(result["partial"], "预算耗尽必须标 partial")
        self.assertEqual(result["stop_reason"], "BUDGET_EXHAUSTED")
        self.assertEqual(result["stats"]["finished_before_budget"], 1)
        refs = [item["evidence_ref"] for item in result["evidence"]]
        self.assertEqual(refs, ["article:1"], "已完成的通道结果必须照常返回（部分结果回退）")
        by_id = {item["hunter_id"]: item for item in result["hunters"]}
        self.assertEqual(by_id["graph"]["status"], "timeout")
        self.assertEqual(by_id["graph"]["reason_code"], "budget_exhausted")
        self.assertTrue(validate("hunter_fleet_result", result)[0])

    def test_fallback_marks_satisfied_when_the_target_already_succeeded(self):
        bm25 = _FakeHunter("bm25", evidence=[_evidence("article:1")])
        semantic = _FakeHunter("semantic", status="degraded", fallback="bm25")
        result = HunterFleet([bm25, semantic], max_workers=2, retries=0).run(self.request())
        by_id = {item["hunter_id"]: item for item in result["hunters"]}
        fallback = by_id["semantic"]["stats"]["fallback"]
        self.assertEqual(fallback["hunter"], "bm25")
        self.assertTrue(fallback["satisfied"])
        self.assertEqual(fallback["evidence"], 1)
        self.assertEqual(by_id["semantic"]["failure_policy"], "FALLBACK")

    def test_fallback_reports_when_target_also_failed(self):
        bm25 = _FakeHunter("bm25", raises=RuntimeError("bm25 也挂"))
        semantic = _FakeHunter("semantic", status="degraded", fallback="bm25")
        result = HunterFleet([bm25, semantic], max_workers=2, retries=0).run(self.request())
        by_id = {item["hunter_id"]: item for item in result["hunters"]}
        self.assertFalse(by_id["semantic"]["stats"]["fallback"]["satisfied"])

    def test_safe_run_itself_raising_is_still_isolated(self):
        class _Broken(_FakeHunter):
            def safe_run(self, request):
                raise RuntimeError("连 safe_run 都炸")

        broken = _Broken("graph")
        good = _FakeHunter("bm25", evidence=[_evidence("article:1")])
        result = HunterFleet([good, broken], max_workers=2, retries=0).run(self.request())
        by_id = {item["hunter_id"]: item for item in result["hunters"]}
        self.assertEqual(by_id["graph"]["status"], "error")
        self.assertEqual(by_id["graph"]["reason_code"], "hunter_exception")
        self.assertEqual(len(result["evidence"]), 1)


class MergeTests(unittest.TestCase):
    def _make(self, hunter_id, evidence, status="ok"):
        return hunter_outcome(hunter_id, status=status, evidence=evidence)

    def test_same_article_from_two_hunters_is_deduped(self):
        outcomes = [self._make("bm25", [_evidence("article:1", score=40)]),
                    self._make("semantic", [_evidence("article:1", score=90)])]
        merged = merge_hunter_evidence(outcomes, limit=12)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["retrieval_method"], "keyword", "首条胜出（与既有去重口径一致）")

    def test_article_bucket_is_sorted_by_score(self):
        outcomes = [self._make("bm25", [_evidence("article:1", score=10),
                                       _evidence("article:2", score=40)])]
        merged = merge_hunter_evidence(outcomes, limit=12)
        self.assertEqual([item["evidence_ref"] for item in merged], ["article:2", "article:1"])

    def test_lexical_items_rank_before_semantic_ones(self):
        """词面 vs 语义**不混排**：BM25 分与余弦量纲不同，混排会把词面命中挤出去。"""
        outcomes = [self._make("bm25", [_evidence("article:1", score=6.0)]),
                    self._make("semantic", [_evidence("article:2", score=99.0)])]
        merged = merge_hunter_evidence(outcomes, limit=12)
        self.assertEqual([item["evidence_ref"] for item in merged], ["article:1", "article:2"])

    def test_semantic_still_fills_the_slots_lexical_left_empty(self):
        outcomes = [self._make("bm25", []),
                    self._make("semantic", [_evidence("article:5", score=88.0)])]
        merged = merge_hunter_evidence(outcomes, limit=12)
        self.assertEqual([item["evidence_ref"] for item in merged], ["article:5"])

    def test_lexical_wins_when_both_channels_return_the_same_article(self):
        outcomes = [self._make("semantic", [_evidence("article:1", score=99.0)]),
                    self._make("bm25", [_evidence("article:1", score=6.0)])]
        merged = merge_hunter_evidence(outcomes, limit=12)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["retrieval_method"], "keyword")

    def test_graph_evidence_slot_cap_matches_the_existing_formula(self):
        graph_items = [_evidence("edge:e%d" % index, article_id=None) for index in range(10)]
        outcomes = [self._make("graph", graph_items)]
        merged = merge_hunter_evidence(outcomes, limit=12)
        self.assertEqual(len(merged), fleet_module.graph_slot_cap(12))
        self.assertEqual(fleet_module.graph_slot_cap(12), 4, "与 retrieve() 里的名额公式一致")

    def test_page_evidence_is_pinned_first(self):
        page = _evidence("page:7", article_id=7, score=1000)
        outcomes = [self._make("bm25", [_evidence("article:1", score=99)])]
        merged = merge_hunter_evidence(outcomes, limit=12, page_evidence=[page])
        self.assertEqual(merged[0]["evidence_ref"], "page:7")

    def test_query_expansion_outcome_never_enters_the_evidence_pool(self):
        outcomes = [self._make("query_expansion", [_evidence("article:1")])]
        self.assertEqual(merge_hunter_evidence(outcomes, limit=12), [])


class FleetRetrieveIntegrationTests(_RealCorpusBase):
    RETRIEVE_KEYS = {"queries", "evidence", "excluded", "stats", "time_window", "graph"}

    def setUp(self):
        super().setUp()
        self.ids = seed_standard_corpus(self.db, pack_id=DEFAULT_PACK)

    def test_retrieve_shape_matches_the_existing_retriever(self):
        fleet = fleet_module.build_default_fleet(database=self.db, retriever=self.retriever)
        result = fleet.retrieve({"question": QUESTION, "queries": [QUESTION],
                                 "entities": ["家族办公室"], "needs_local_articles": True},
                                industry_pack_id=DEFAULT_PACK, limit=12)
        self.assertTrue(self.RETRIEVE_KEYS.issubset(set(result.keys())), result.keys())
        self.assertEqual(set(result.keys()) - self.RETRIEVE_KEYS, {"hunters"})
        self.assertTrue(result["evidence"], "真语料上必须能取到证据")
        self.assertIn("hunter_fleet", result["stats"])
        self.assertTrue(validate("hunter_fleet_result", {
            "contract_version": "qa-hunter-v1", "hunters": result["hunters"],
            "evidence": result["evidence"], "stats": result["stats"]["hunter_fleet"]})[0])
        for hunter in result["hunters"]:
            ok, note = validate("hunter_result", hunter)
            self.assertTrue(ok, "%s 的 Hunter 回执不合契约：%s" % (hunter["hunter_id"], note))
        article_ids = [int(item["article_id"]) for item in result["evidence"]
                       if item.get("article_id")]
        self.assertEqual(len(article_ids), len(set(article_ids)), "证据包里不许有重复文章")

    def test_pool_is_loaded_once_across_all_hunters(self):
        fleet = fleet_module.build_default_fleet(database=self.db, retriever=self.retriever)
        result = fleet.retrieve({"question": QUESTION, "queries": [QUESTION],
                                 "needs_local_articles": True},
                                industry_pack_id=DEFAULT_PACK, limit=12)
        pool = result["stats"]["hunter_fleet"]["pool"]
        self.assertEqual(pool["pool_loads"], 1, "候选池必须一次加载、多 Hunter 共用")
        self.assertGreaterEqual(pool["pool_rows"], 3)

    def test_page_context_article_is_pinned_first(self):
        fleet = fleet_module.build_default_fleet(database=self.db, retriever=self.retriever)
        result = fleet.retrieve({"question": QUESTION, "queries": [QUESTION],
                                 "needs_local_articles": True},
                                industry_pack_id=DEFAULT_PACK, limit=12,
                                page_context={"article_id": self.ids["sibling"]})
        self.assertEqual(result["evidence"][0]["evidence_ref"],
                         "page:%d" % self.ids["sibling"])
        self.assertEqual(result["evidence"][0]["relationship"], "context")
        denied = result["excluded"]["page_context"]
        self.assertEqual(denied, [], "池子里的文章不该被判 not_found")

    def test_unknown_page_context_article_is_reported_not_raised(self):
        fleet = fleet_module.build_default_fleet(database=self.db, retriever=self.retriever)
        result = fleet.retrieve({"question": QUESTION, "queries": [QUESTION],
                                 "needs_local_articles": True},
                                industry_pack_id=DEFAULT_PACK, limit=12,
                                page_context={"article_id": 999999})
        self.assertEqual(result["excluded"]["page_context"],
                         [{"article_id": 999999, "reason": "not_found_or_not_authorized"}])

    def test_semantic_channel_degrades_but_bm25_still_delivers(self):
        """库里没有向量时的真实现状：语义降级 → 兜底 BM25，证据照常出。"""
        fleet = fleet_module.build_default_fleet(database=self.db, retriever=self.retriever)
        result = fleet.retrieve({"question": QUESTION, "queries": [QUESTION],
                                 "needs_local_articles": True},
                                industry_pack_id=DEFAULT_PACK, limit=12)
        by_id = {item["hunter_id"]: item for item in result["hunters"]}
        self.assertEqual(by_id["semantic"]["status"], "degraded")
        self.assertEqual(by_id["semantic"]["reason_code"], "no_vectors")
        self.assertTrue(by_id["semantic"]["stats"]["fallback"]["satisfied"])
        self.assertEqual(by_id["bm25"]["status"], "ok")
        self.assertGreaterEqual(len(result["evidence"]), 1)

    def test_excluded_counters_are_carried_from_the_pool(self):
        fleet = fleet_module.build_default_fleet(database=self.db, retriever=self.retriever)
        result = fleet.retrieve({"question": QUESTION, "queries": [QUESTION],
                                 "needs_local_articles": True},
                                industry_pack_id=DEFAULT_PACK, limit=12)
        self.assertIn("quality_gate", result["excluded"])
        self.assertIn("policy_exact", result["excluded"])

    def test_quality_gate_still_filters_unadmitted_articles(self):
        """闸门回归：没通过行业相关性闸门的文章不许进舰队证据包。"""
        blocked = add_article(self.db, url="https://example.com/p04/blocked",
                              title="家族办公室税收优惠政策内部解读",
                              content="家族办公室税收优惠政策内部解读，家族办公室税收优惠。",
                              publish_date="2026-10-05", keywords=["家族办公室"])
        add_classification(self.db, blocked, pack_id=DEFAULT_PACK, keywords=["家族办公室"],
                           admitted=False)
        fleet = fleet_module.build_default_fleet(database=self.db, retriever=self.retriever)
        result = fleet.retrieve({"question": QUESTION, "queries": [QUESTION],
                                 "needs_local_articles": True},
                                industry_pack_id=DEFAULT_PACK, limit=12)
        refs = [item["evidence_ref"] for item in result["evidence"]]
        self.assertNotIn("article:%d" % blocked, refs,
                         "舰队复用候选池 → 闸门必须照样生效")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 09 · P09-04 `Recall API` 用例。

钉住：
  1. §11 `MemoryRecallScore` 六项**逐项可复算**（手算一例对上），同输入同输出；
  2. §2.1 + MASTER_RULES 11/12：**每一条命中都是 MEMORY_HINT**——
     `hint=True` / `requires_revalidation=True` / `verified_evidence=False`，一条都不许例外；
  3. §10 硬规则：`status != ACTIVE` 一律不进命中（只进计数）；
  4. §14 作用域：会话级记忆跨会话不可见；`planning` 模式只召回策略/失败/查询模式类；
  5. 召回留痕（`memory_recall_log`）+ 使用统计（召回计 recalled、"用过"才计 reuse_count）；
  6. 失败路径：没有 store / store 抛错都退化成"没有可召回的记忆"，不冒泡。
"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_graph_contracts as contracts  # noqa: E402
import qa_memory as memory  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402

import qa_phase09_fixtures as fx  # noqa: E402

QUESTION = "香港家族办公室税收优惠政策对内地高净值客户有什么影响？"


def _seed(store, graph=None, **kwargs):
    """用真实写门落几条记忆（不是手写行），返回 (receipt, memory_ids)。"""
    candidates = memory.memory_candidates_from_graph(graph or fx.graph(), **kwargs)
    receipt = memory.apply_write_gate(store, candidates, run_id="run-p09")
    return receipt, list(receipt["memory_ids"])


def _raw_item(store, *, content, memory_type="VERIFIED_CLAIM", status="ACTIVE",
              scope="PATIENT_LONGITUDINAL", confidence=0.8, freshness="LONG",
              session_id="s1", industry_pack_id="auto", owner_user_id="p09",
              evidence_refs=("article:9",), last_verified_at="2026-10-11T00:00:00.000Z",
              valid_until=""):
    """直接落一条记忆（用于构造 P12 的类型 / 非 ACTIVE 状态 / 其它作用域等边界）。"""
    item = {
        "memory_id": memory.memory_id_for(scope=scope, memory_type=memory_type,
                                          canonical_content=content,
                                          owner_user_id=owner_user_id, session_id=session_id,
                                          industry_pack_id=industry_pack_id),
        "memory_type": memory_type, "canonical_content": content,
        "content_fingerprint": memory.content_fingerprint(content), "confidence": confidence,
        "freshness_class": freshness, "status": status, "scope": scope,
        "scope_key": memory.scope_key(scope, owner_user_id=owner_user_id, session_id=session_id,
                                      industry_pack_id=industry_pack_id),
        "owner_user_id": owner_user_id, "session_id": session_id,
        "industry_pack_id": industry_pack_id, "entity_ids": [],
        "source_evidence_ids": list(evidence_refs), "last_verified_at": last_verified_at,
        "valid_until": valid_until, "version": 1, "decay_score": 0.5, "metadata": {},
    }
    store.save_memory_item(item)
    store.link_memory_evidence(item["memory_id"], [{
        "evidence_ref": evidence_refs[0], "source_fingerprint": "SF", "span_fingerprint": "SP",
        "verdict": "SUPPORTED", "evidence_score": 0.9, "metadata": {"article_id": 1}}])
    return item["memory_id"]


class ScoreTests(unittest.TestCase):
    def test_recall_score_is_recomputable_by_hand(self):
        factors = {"semantic_relevance": 0.8, "task_applicability": 1.0, "confidence": 0.5,
                   "freshness": 0.5, "historical_utility": 0.5, "contradiction_risk": 0.1}
        expected = round(0.8 * 1.0 * 0.5 * 0.5 * 0.5 - 0.1, 8)
        self.assertEqual(memory.recall_score(factors), expected)

    def test_score_clamps_and_zeroes_on_missing_factors(self):
        self.assertEqual(memory.recall_score({}), 0.0)
        self.assertEqual(memory.recall_score({"semantic_relevance": 1.0}), 0.0)
        self.assertGreaterEqual(memory.recall_score(
            {"semantic_relevance": 1.0, "task_applicability": 0.0, "confidence": 1.0,
             "freshness": 1.0, "historical_utility": 1.0}), 0.0)

    def test_semantic_relevance_is_lexical_coverage_and_deterministic(self):
        item = {"canonical_content": "香港家族办公室税收优惠政策对合资格管理人给予宽免"}
        terms = {"家族办公室", "税收优惠"}
        first = memory.semantic_relevance(terms, item)
        second = memory.semantic_relevance(terms, item)
        self.assertEqual(first, second)
        self.assertEqual(first, 1.0)
        self.assertEqual(memory.semantic_relevance({"不存在的词"}, item), 0.0)

    def test_task_applicability_filters_by_mode_and_pack(self):
        planning = memory.task_applicability("planning", {"memory_type": "STRATEGY"})
        self.assertEqual(planning["value"], 1.0)
        evidence = memory.task_applicability("planning", {"memory_type": "VERIFIED_CLAIM"})
        self.assertEqual(evidence["value"], 0.0, "planning 模式不许召回事实类（§10）")
        other_pack = memory.task_applicability(
            "evidence", {"memory_type": "VERIFIED_CLAIM", "scope": "PATIENT_LONGITUDINAL",
                         "industry_pack_id": "another"}, industry_pack_id="auto")
        self.assertLess(other_pack["value"], 1.0)


class HintInvariantTests(unittest.TestCase):
    def test_every_hit_is_a_memory_hint(self):
        with fx.temp_store() as (_database, store):
            _receipt, ids = _seed(store, industry_pack_id="auto", session_id="s1")
            self.assertTrue(ids)
            receipt = memory.recall(store, mode="evidence", query=QUESTION,
                                    owner_user_id="p09", session_id="s1",
                                    industry_pack_id="auto", run_id="run-p09")
            self.assertTrue(receipt["hits"], "刚写进去的记忆应当能被召回")
            for hit in receipt["hits"]:
                self.assertTrue(hit["hint"])
                self.assertTrue(hit["requires_revalidation"], "记忆提示恒需重验（Phase 10 才做）")
                self.assertFalse(hit["verified_evidence"], "记忆永远不是已验证证据")
                self.assertEqual(hit["hint_version"], contracts.MEMORY_RECALL_HINT_VERSION)
                ok, note = validate("memory_recall_hit", hit)
                self.assertTrue(ok, note)
            ok, note = validate("memory_recall_receipt", {key: receipt[key] for key in (
                "recall_version", "mode", "channels", "hits", "counts")})
            self.assertTrue(ok, note)
            self.assertTrue(receipt["contract_ok"])

    def test_hit_explain_mentions_that_it_is_not_evidence(self):
        with fx.temp_store() as (_database, store):
            _seed(store, industry_pack_id="auto", session_id="s1")
            receipt = memory.recall(store, mode="evidence", query=QUESTION,
                                    owner_user_id="p09", session_id="s1",
                                    industry_pack_id="auto")
            self.assertIn("这不是证据", receipt["hits"][0]["explain"])


class StatusAndScopeTests(unittest.TestCase):
    def test_non_active_memories_are_excluded_and_counted(self):
        with fx.temp_store() as (_database, store):
            _raw_item(store, content="香港家族办公室税收优惠已失效的旧结论", status="SUPERSEDED")
            _raw_item(store, content="香港家族办公室税收优惠被撤销的结论", status="REVOKED")
            receipt = memory.recall(store, mode="evidence", query="香港家族办公室税收优惠",
                                    owner_user_id="p09", session_id="s1",
                                    industry_pack_id="auto")
            self.assertEqual(receipt["hits"], [])
            self.assertEqual(receipt["counts"]["excluded_by_status"], 2)

    def test_session_scope_does_not_leak_across_sessions(self):
        with fx.temp_store() as (_database, store):
            _raw_item(store, content="香港家族办公室本轮会话的临时结论", scope="SESSION",
                      session_id="s1")
            same = memory.recall(store, mode="evidence", query="香港家族办公室临时结论",
                                 owner_user_id="p09", session_id="s1", industry_pack_id="auto")
            other = memory.recall(store, mode="evidence", query="香港家族办公室临时结论",
                                  owner_user_id="p09", session_id="s2", industry_pack_id="auto")
            self.assertEqual(len(same["hits"]), 1)
            self.assertEqual(other["hits"], [], "会话级记忆不许跨会话（§14）")

    def test_longitudinal_memory_is_visible_in_another_session_of_the_same_pack(self):
        with fx.temp_store() as (_database, store):
            _raw_item(store, content="香港家族办公室税收优惠的长期结论",
                      scope="PATIENT_LONGITUDINAL", session_id="s1")
            other = memory.recall(store, mode="evidence", query="香港家族办公室税收优惠长期结论",
                                  owner_user_id="p09", session_id="s2", industry_pack_id="auto")
            self.assertEqual(len(other["hits"]), 1)
            elsewhere = memory.recall(store, mode="evidence", query="香港家族办公室税收优惠长期结论",
                                      owner_user_id="p09", session_id="s2",
                                      industry_pack_id="other-pack")
            self.assertEqual(elsewhere["hits"], [], "换了行业包就不该看见")

    def test_global_knowledge_is_visible_everywhere(self):
        with fx.temp_store() as (_database, store):
            _raw_item(store, content="家族办公室的定义与历史沿革", scope="GLOBAL_KNOWLEDGE")
            receipt = memory.recall(store, mode="evidence", query="家族办公室的定义与历史沿革",
                                    owner_user_id="p09", session_id="s9",
                                    industry_pack_id="any-pack")
            self.assertEqual(len(receipt["hits"]), 1)


class ModeTests(unittest.TestCase):
    def test_planning_mode_recalls_only_planning_types(self):
        with fx.temp_store() as (_database, store):
            _raw_item(store, content="多跳问题先用图谱邻居扩展查询词", memory_type="STRATEGY")
            _raw_item(store, content="香港家族办公室税收优惠的结论事实",
                      memory_type="VERIFIED_CLAIM")
            planning = memory.recall(store, mode="planning", query="图谱邻居扩展查询词",
                                     owner_user_id="p09", session_id="s1", industry_pack_id="auto")
            self.assertEqual([hit["memory_type"] for hit in planning["hits"]], ["STRATEGY"])
            evidence = memory.recall(store, mode="evidence", query="香港家族办公室税收优惠的结论",
                                     owner_user_id="p09", session_id="s1", industry_pack_id="auto")
            self.assertEqual([hit["memory_type"] for hit in evidence["hits"]], ["VERIFIED_CLAIM"])

    def test_planning_mode_returns_nothing_when_no_strategy_memories_exist(self):
        """真实数据现在就是这样：策略/失败记忆属 Phase 12，本阶段规划召回为空且**有解释**。"""
        with fx.temp_store() as (_database, store):
            _seed(store, industry_pack_id="auto", session_id="s1")
            receipt = memory.recall(store, mode="planning", query=QUESTION,
                                    owner_user_id="p09", session_id="s1", industry_pack_id="auto")
            self.assertEqual(receipt["hits"], [])
            self.assertEqual(receipt["stats"]["types"], ["STRATEGY", "FAILURE", "QUERY_PATTERN"])
            self.assertGreater(receipt["counts"]["excluded_by_type"], 0)


class OrderingAndLimitTests(unittest.TestCase):
    def test_ranking_is_deterministic_and_ties_break_on_memory_id(self):
        with fx.temp_store() as (_database, store):
            _raw_item(store, content="香港家族办公室税收优惠结论甲")
            _raw_item(store, content="香港家族办公室税收优惠结论乙")
            first = memory.recall(store, mode="evidence", query="香港家族办公室税收优惠",
                                  owner_user_id="p09", session_id="s1", industry_pack_id="auto")
            second = memory.recall(store, mode="evidence", query="香港家族办公室税收优惠",
                                   owner_user_id="p09", session_id="s1", industry_pack_id="auto")
            self.assertEqual([hit["memory_id"] for hit in first["hits"]],
                             [hit["memory_id"] for hit in second["hits"]])
            scores = [hit["score"] for hit in first["hits"]]
            self.assertEqual(scores, sorted(scores, reverse=True))

    def test_limit_and_min_score(self):
        with fx.temp_store() as (_database, store):
            for index in range(3):
                _raw_item(store, content="香港家族办公室税收优惠结论 %d" % index)
            limited = memory.recall(store, mode="evidence", query="香港家族办公室税收优惠",
                                    owner_user_id="p09", session_id="s1",
                                    industry_pack_id="auto", limit=2)
            self.assertEqual(len(limited["hits"]), 2)
            none = memory.recall(store, mode="evidence", query="香港家族办公室税收优惠",
                                 owner_user_id="p09", session_id="s1", industry_pack_id="auto",
                                 min_score=0.99)
            self.assertEqual(none["hits"], [])
            self.assertGreater(none["counts"]["below_threshold"], 0)


class LoggingAndUsageTests(unittest.TestCase):
    def test_recall_is_logged_and_usage_is_split_from_reuse(self):
        with fx.temp_store() as (_database, store):
            _receipt, ids = _seed(store, industry_pack_id="auto", session_id="s1")
            receipt = memory.recall(store, mode="evidence", query=QUESTION,
                                    owner_user_id="p09", session_id="s1",
                                    industry_pack_id="auto", run_id="run-p09")
            rows = _database.connection.execute(
                "SELECT mode, hits, top_score FROM memory_recall_log").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(str(rows[0][0]), "evidence")
            self.assertEqual(int(rows[0][1]), len(receipt["hits"]))
            usage = store.memory_usage([hit["memory_id"] for hit in receipt["hits"]])
            for memory_id in usage:
                self.assertGreaterEqual(usage[memory_id]["recalled"], 1)
                self.assertEqual(usage[memory_id]["used"], 0, "召回不等于用过")
            self.assertEqual(memory.mark_memory_used(store, [ids[0]], helped=True), 1)
            after = store.memory_usage([ids[0]])
            self.assertEqual(after[ids[0]]["used"], 1)
            self.assertEqual(after[ids[0]]["helped"], 1)

    def test_historical_utility_reflects_counts(self):
        item = {"memory_id": "MEM1", "recall_count": 5, "reuse_count": 0}
        fresh = memory.historical_utility(item)
        used = memory.historical_utility({"memory_id": "MEM1", "recall_count": 5, "reuse_count": 3})
        self.assertGreater(used["value"], fresh["value"])
        self.assertLessEqual(used["value"], 1.0)
        # 新记忆中性 0.8（"没历史"不等于"历史表现差"）；反复召回却没人用才降到 0.5
        self.assertEqual(memory.historical_utility({})["value"], 0.8, "新记忆中性，不罚也不奖")
        ignored = memory.historical_utility({"memory_id": "MEM1", "recall_count": 5,
                                             "reuse_count": 0})
        self.assertEqual(ignored["value"], 0.5, "召回 5 次从没被用过 → 历史效用低")


class FailurePathTests(unittest.TestCase):
    def test_no_store_returns_an_empty_receipt(self):
        receipt = memory.recall(None, mode="evidence", query=QUESTION)
        self.assertEqual(receipt["hits"], [])
        self.assertEqual(receipt["counts"]["considered"], 0)
        self.assertTrue(receipt["contract_ok"])

    def test_store_errors_degrade_to_empty(self):
        with fx.temp_store() as (_database, store):
            with mock.patch.object(store, "load_memory_items", side_effect=RuntimeError("boom")):
                receipt = memory.recall(store, mode="evidence", query=QUESTION)
            self.assertEqual(receipt["hits"], [])
            self.assertEqual(receipt["counts"]["considered"], 0)

    def test_recall_log_failure_does_not_break_the_result(self):
        with fx.temp_store() as (_database, store):
            _seed(store, industry_pack_id="auto", session_id="s1")
            with mock.patch.object(store, "record_memory_recall", side_effect=RuntimeError("boom")):
                receipt = memory.recall(store, mode="evidence", query=QUESTION,
                                        owner_user_id="p09", session_id="s1",
                                        industry_pack_id="auto")
            self.assertTrue(receipt["hits"])


class ContextItemTests(unittest.TestCase):
    def test_memory_items_are_hints_in_the_memory_section(self):
        with fx.temp_store() as (_database, store):
            _seed(store, industry_pack_id="auto", session_id="s1")
            receipt = memory.recall(store, mode="evidence", query=QUESTION,
                                    owner_user_id="p09", session_id="s1", industry_pack_id="auto")
            items = memory.memory_context_items(receipt)
            self.assertTrue(items)
            for item in items:
                self.assertEqual(item["kind"], "memory")
                self.assertEqual(item["section"], "memory_context")
                self.assertEqual(item["source_stage"], "memory_graph")
                self.assertIn(memory.MEMORY_HINT_MARK, item["text"])
                self.assertTrue(item["grounding"]["hint"])
                self.assertTrue(item["grounding"]["requires_revalidation"])
                self.assertFalse(item["grounding"]["verified_evidence"])
                ok, note = validate("context_item", item)
                self.assertTrue(ok, note)
            self.assertEqual(items[0]["metadata"]["role"], "memory_hint")

    def test_empty_receipt_yields_no_items(self):
        self.assertEqual(memory.memory_context_items({}), [])
        self.assertEqual(memory.memory_context_items({"hits": []}), [])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 12 · P12-02 `Failure Memory` + P12-06 `version-aware retry`。

钉住：
  1. 四类失败**都能从 trace 真的判出来**（无效 query / 失败 route / 误导来源 / 语义漂移），
     且每条都带 §3.3 + MASTER_RULES 15 要求的五个绑定字段（corpus/config/通道/查询指纹/时间）；
  2. **版本号不许编**：`run.corpus_version` 优先，空则用检索回执里真实观测到的
     `cache.kb_version`，都没有就是 `VERSION_UNBOUND`（并因此一律允许重试）；
  3. 失败判据只吃**真实信号**：候选数不等于采纳数；`search_status='disabled'` 的通道
     不算"试过"；`vector_prefilter.enabled=False` 不算"语义通道零产出"；
  4. P12-06 的六条理由码**逐条可达**：同一环境才 `SUPPRESS`，版本变了/没绑定/过期一律
     `ALLOW_RETRY`（MASTER_RULES 15：版本变化后允许重试）；
  5. 失败记忆的正文**不含时间**（时间进 metadata）→ 同一失败在不同抓取时刻内容寻址一致；
  6. 落库后：`FAILED_ON` 关系指向真实通道/域，证据链接来自真实证据行。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_graph_contracts as contracts  # noqa: E402
import qa_memory_experience as exp  # noqa: E402

import qa_phase12_fixtures as fx  # noqa: E402

NOW = "2026-10-11T00:00:00Z"


def _zero_adoption_trace(**overrides):
    empty = {"version": "qa-evidence-graph-v1", "claims": [], "evidence": [], "edges": [],
             "conflicts": [], "stats": {}}
    return fx.trace(graph=empty,
                    level1_receipt=fx.level1(adopted=0, candidates=20, web_adopted=0,
                                             graph_adopted=0, **overrides.pop("level1", {})),
                    gap_receipt=fx.gap_receipt(stop_reason="NO_GAIN"), **overrides)


def _weak_evidence_trace(*, verdict="REFUTED"):
    """一条只命中非标题族、且（构造注入）被判反驳的证据：语义漂移 + 误导来源的原料。"""
    item = fx.fx09.evidence_item("article:9", text=fx.fx09.UNVERIFIED_EVIDENCE_TEXT,
                                 title="论坛闲聊家族办公室")
    item["match_reason"] = "核心词命中：家族；分类词命中：税务"
    item["retrieval_method"] = "keyword"
    graph = fx.graph_with(refs=("article:9",), evidence_text=fx.fx09.UNVERIFIED_EVIDENCE_TEXT,
                          titles=("论坛闲聊家族办公室",), verdict=verdict)
    graph["evidence"][0]["match_reason"] = item["match_reason"]
    return fx.trace(graph=graph,
                    gap_receipt=fx.gap_receipt(stop_reason="UNRESOLVABLE_CONTRADICTION"))


class FailureDetectionTests(unittest.TestCase):
    def test_failed_route_zero_adoption(self):
        rows = exp.detect_failures(_zero_adoption_trace())
        kinds = {(row["kind"], row["route"]) for row in rows}
        for route in ("keyword", "web", "graph"):
            self.assertIn(("FAILED_ROUTE", route), kinds)
        keyword = next(row for row in rows if row["kind"] == "FAILED_ROUTE" and row["route"] == "keyword")
        self.assertEqual(keyword["adopted"], 0)
        self.assertEqual(keyword["rejected"], 150, "候选被闸门拒掉的条数也如实记")
        self.assertIn("没有采纳任何证据", keyword["detail"])
        ok, note = contracts.validate("failure_memory", keyword)
        self.assertTrue(ok, note)

    def test_failed_route_zero_verified_yield(self):
        rows = exp.detect_failures(_weak_evidence_trace())
        failed = [row for row in rows if row["kind"] == "FAILED_ROUTE"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["adopted"], 1)
        self.assertEqual(failed[0]["verified"], 0)
        self.assertIn("没有一条通过核验", failed[0]["detail"])

    def test_invalid_query_is_per_query_and_honest_about_attribution(self):
        trace = _zero_adoption_trace()
        rows = [row for row in exp.detect_failures(trace) if row["kind"] == "INVALID_QUERY"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["query"], fx.QUESTION)
        self.assertEqual(row["query_fingerprint"], exp.query_fingerprint(fx.QUESTION))
        self.assertIn("该查询所在的一轮检索", row["detail"])
        self.assertNotIn("这个查询无效", row["detail"])

    def test_query_fingerprint_is_content_addressed(self):
        self.assertEqual(exp.query_fingerprint(" 香港 家族办公室 "),
                         exp.query_fingerprint("香港 家族办公室"))
        self.assertNotEqual(exp.query_fingerprint("香港 家族办公室"),
                            exp.query_fingerprint("香港 家族办公室 税收"))

    def test_false_positive_source_from_refuted_evidence(self):
        rows = [row for row in exp.detect_failures(_weak_evidence_trace())
                if row["kind"] == "FALSE_POSITIVE_SOURCE"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["domain"], "example.com")
        self.assertEqual(rows[0]["rejected"], 1)
        self.assertIn("看着相关但结论相反", rows[0]["detail"])

    def test_semantic_drift_from_weak_match_families(self):
        rows = [row for row in exp.detect_failures(_weak_evidence_trace())
                if row["kind"] == "SEMANTIC_DRIFT"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["route"], "keyword")
        self.assertIn("只命中非标题族", rows[0]["detail"])
        self.assertIn("分类词命中", rows[0]["detail"])

    def test_title_hit_evidence_is_not_drift(self):
        trace = fx.trace(gap_receipt=fx.gap_receipt())
        self.assertEqual([row for row in exp.detect_failures(trace)
                          if row["kind"] == "SEMANTIC_DRIFT"], [])

    def test_disabled_channels_are_not_treated_as_failed(self):
        trace = fx.trace(level1_receipt=fx.level1(web_adopted=0, search_status="disabled",
                                                  graph_adopted=0))
        routes = {row["route"] for row in exp.detect_failures(trace)
                  if row["kind"] == "FAILED_ROUTE"}
        self.assertNotIn("web", routes, "通道没开不算失败")
        self.assertIn("graph", routes, "graph_adopted 有键就是跑过（0 采纳 = 失败）")

    def test_vector_prefilter_off_is_not_a_semantic_failure(self):
        trace = fx.trace(level1_receipt=fx.level1(vector_variants=2, vector_enabled=False))
        self.assertNotIn("semantic", trace["retrieval"]["routes"],
                         "enabled=false 就是没跑，不许记成零产出")
        on = fx.trace(level1_receipt=fx.level1(vector_variants=2, vector_enabled=True))
        self.assertIn("semantic", on["retrieval"]["routes"])
        self.assertTrue(on["retrieval"]["routes"]["semantic"]["attempted"])
        # 跑过且零采纳 → 真的判一条 FAILED_ROUTE(semantic)
        self.assertIn("semantic", {row["route"] for row in exp.detect_failures(on)
                                   if row["kind"] == "FAILED_ROUTE"})

    def test_every_failure_carries_the_binding_fields(self):
        for row in exp.detect_failures(_zero_adoption_trace()):
            for field in contracts.FAILURE_BINDING_FIELDS:
                self.assertIn(field, row, field)
            self.assertIn(row["corpus_version"], (fx.KB_VERSION, ""))
            self.assertEqual(row["corpus_version"], fx.KB_VERSION)
            self.assertTrue(row["retrieval_config_version"].startswith("rcfg:"))
            self.assertTrue(row["binding_source"])
            self.assertTrue(row["observed_at"])

    def test_deterministic_and_time_free_content(self):
        first = exp.detect_failures(_zero_adoption_trace())
        second = exp.detect_failures(_zero_adoption_trace())
        self.assertEqual([row["failure_id"] for row in first],
                         [row["failure_id"] for row in second])
        self.assertEqual([exp.failure_content(row) for row in first],
                         [exp.failure_content(row) for row in second])
        shifted = exp.detect_failures(fx.trace(
            graph=_zero_adoption_trace()["evidence_index"] and
            {"version": "qa-evidence-graph-v1", "claims": [], "evidence": [], "edges": [],
             "conflicts": [], "stats": {}},
            level1_receipt=fx.level1(adopted=0, candidates=20, web_adopted=0, graph_adopted=0),
            gap_receipt=fx.gap_receipt(stop_reason="NO_GAIN")))
        self.assertEqual([exp.failure_content(row) for row in shifted],
                         [exp.failure_content(row) for row in first],
                         "时间不进正文（进 metadata），跨时刻内容寻址一致")


class RetryPolicyTests(unittest.TestCase):
    def _failure(self, **overrides):
        row = {"failure_id": "F1", "kind": "FAILED_ROUTE", "route": "keyword",
               "corpus_version": "kb-1", "retrieval_config_version": "rcfg:aaa",
               "observed_at": NOW}
        row.update(overrides)
        return row

    def test_same_environment_suppresses(self):
        decision = exp.retry_decision(self._failure(), corpus_version="kb-1",
                                      retrieval_config_version="rcfg:aaa", now=NOW)
        self.assertEqual((decision["decision"], decision["reason"]),
                         ("SUPPRESS", "SAME_ENVIRONMENT"))
        ok, note = contracts.validate("retry_decision", decision)
        self.assertTrue(ok, note)

    def test_corpus_version_change_allows_retry(self):
        decision = exp.retry_decision(self._failure(), corpus_version="kb-2",
                                      retrieval_config_version="rcfg:aaa", now=NOW)
        self.assertEqual((decision["decision"], decision["reason"]),
                         ("ALLOW_RETRY", "CORPUS_VERSION_CHANGED"))
        self.assertEqual(decision["corpus_version_recorded"], "kb-1")
        self.assertEqual(decision["corpus_version_current"], "kb-2")

    def test_config_change_allows_retry_even_with_same_corpus(self):
        decision = exp.retry_decision(self._failure(), corpus_version="kb-1",
                                      retrieval_config_version="rcfg:bbb", now=NOW)
        self.assertEqual((decision["decision"], decision["reason"]),
                         ("ALLOW_RETRY", "RETRIEVAL_CONFIG_CHANGED"))

    def test_unbound_allows_retry(self):
        unbound = self._failure(corpus_version="", retrieval_config_version="")
        decision = exp.retry_decision(unbound, corpus_version="kb-1",
                                      retrieval_config_version="rcfg:aaa", now=NOW)
        self.assertEqual((decision["decision"], decision["reason"]),
                         ("ALLOW_RETRY", "VERSION_UNBOUND"))
        # 记录侧记过的每一条轴都必须能被当前环境确认：记了配置但当前配置未知 → 证明不了 → 不许压制
        for recorded, current in (({"corpus_version": "kb-1", "retrieval_config_version": "rcfg:aaa"},
                                   {"corpus_version": "kb-1", "retrieval_config_version": ""}),
                                  ({"corpus_version": "kb-1", "retrieval_config_version": "rcfg:aaa"},
                                   {"corpus_version": "", "retrieval_config_version": ""})):
            decision = exp.retry_decision(self._failure(**recorded), now=NOW, **current)
            self.assertEqual((decision["decision"], decision["reason"]),
                             ("ALLOW_RETRY", "VERSION_UNBOUND"), (recorded, current))
        # 记录侧只记了语料、当前语料一致 → 这条轴证明相同 → 可以压制（没记的轴不参与）
        only_corpus = self._failure(retrieval_config_version="")
        decision = exp.retry_decision(only_corpus, corpus_version="kb-1",
                                      retrieval_config_version="rcfg:aaa", now=NOW)
        self.assertEqual((decision["decision"], decision["reason"]),
                         ("SUPPRESS", "SAME_ENVIRONMENT"))
        # 但记录侧只记了语料、当前语料不一样 → 版本变了 → 允许重试
        decision = exp.retry_decision(only_corpus, corpus_version="kb-9",
                                      retrieval_config_version="rcfg:aaa", now=NOW)
        self.assertEqual(decision["reason"], "CORPUS_VERSION_CHANGED")

    def test_aged_out_allows_retry(self):
        decision = exp.retry_decision(self._failure(), corpus_version="kb-1",
                                      retrieval_config_version="rcfg:aaa",
                                      now="2026-12-01T00:00:00Z")
        self.assertEqual((decision["decision"], decision["reason"]),
                         ("ALLOW_RETRY", "FAILURE_AGED_OUT"))
        self.assertGreater(decision["age_days"], decision["max_age_days"])

    def test_no_failure_record_allows_retry(self):
        decision = exp.retry_decision({}, corpus_version="kb-1",
                                      retrieval_config_version="rcfg:aaa", now=NOW)
        self.assertEqual((decision["decision"], decision["reason"]),
                         ("ALLOW_RETRY", "NO_FAILURE_RECORD"))

    def test_every_reason_code_is_reachable(self):
        seen = {
            exp.retry_decision(self._failure(), corpus_version="kb-1",
                               retrieval_config_version="rcfg:aaa", now=NOW)["reason"],
            exp.retry_decision(self._failure(), corpus_version="kb-2",
                               retrieval_config_version="rcfg:aaa", now=NOW)["reason"],
            exp.retry_decision(self._failure(), corpus_version="kb-1",
                               retrieval_config_version="rcfg:bbb", now=NOW)["reason"],
            exp.retry_decision(self._failure(corpus_version=""), corpus_version="kb-1",
                               retrieval_config_version="rcfg:aaa", now=NOW)["reason"],
            exp.retry_decision(self._failure(corpus_version="kb-1",
                                             retrieval_config_version="rcfg:aaa"),
                               corpus_version="kb-1", retrieval_config_version="",
                               now=NOW)["reason"],
            exp.retry_decision(self._failure(), corpus_version="kb-1",
                               retrieval_config_version="rcfg:aaa",
                               now="2027-01-01T00:00:00Z")["reason"],
            exp.retry_decision({}, corpus_version="kb-1",
                               retrieval_config_version="rcfg:aaa", now=NOW)["reason"],
        }
        self.assertEqual(seen, set(contracts.RETRY_REASONS))

    def test_max_age_is_configurable(self):
        os.environ["QA_EXPERIENCE_FAILURE_MAX_AGE_DAYS"] = "1"
        try:
            self.assertEqual(exp.failure_max_age_days(), 1.0)
            decision = exp.retry_decision(self._failure(), corpus_version="kb-1",
                                          retrieval_config_version="rcfg:aaa",
                                          now="2026-10-13T00:00:00Z")
            self.assertEqual(decision["reason"], "FAILURE_AGED_OUT")
        finally:
            os.environ.pop("QA_EXPERIENCE_FAILURE_MAX_AGE_DAYS", None)


class FailureWriteTests(unittest.TestCase):
    def test_failure_memory_binds_relations_and_evidence(self):
        trace = _weak_evidence_trace()
        with fx.temp_store("p12-failure.sqlite3") as (_database, store):
            receipt = exp.write_experience_memories(store, traces=[trace],
                                                    run_meta=fx.run_meta())
            self.assertGreaterEqual(receipt["persisted_by_type"].get("FAILURE", 0), 2)
            items = store.load_memory_items(memory_types=["FAILURE"], include_all_scopes=True)
            self.assertTrue(items)
            for item in items:
                self.assertTrue(item["canonical_content"].startswith("经验·"))
                self.assertIs(item["metadata"]["hint_only"], True)
                relations = store.memory_relations(memory_ids=[item["memory_id"]])
                self.assertTrue(relations, "失败记忆必须写 FAILED_ON 关系（可追溯）")
                links = store.memory_evidence(memory_ids=[item["memory_id"]])
                self.assertTrue(links, "失败记忆要绑真实证据（%s）" % item["memory_id"])
                for row in links:
                    self.assertTrue(row["source_fingerprint"])
            kinds = {item["metadata"]["kind"] for item in items}
            self.assertTrue({"FAILED_ROUTE", "SEMANTIC_DRIFT"} <= kinds, kinds)

    def test_idempotent_second_write_merges(self):
        # 用**有证据**的一轮（零采纳轮没有证据行，被 NO_EVIDENCE_LINK 拦下，见 gate 用例）
        trace = _weak_evidence_trace()
        with fx.temp_store("p12-failure-idem.sqlite3") as (_database, store):
            exp.write_experience_memories(store, traces=[trace], run_meta=fx.run_meta())
            before = len(store.load_memory_items(memory_types=["FAILURE"],
                                                 include_all_scopes=True))
            second = exp.write_experience_memories(store, traces=[trace], run_meta=fx.run_meta())
            after = len(store.load_memory_items(memory_types=["FAILURE"],
                                                include_all_scopes=True))
            self.assertGreater(before, 0)
            self.assertEqual(before, after)
            self.assertGreaterEqual(second["gate"]["merged"], 1)

    def test_zero_evidence_round_is_rejected_honestly(self):
        """零证据轮（一条证据都没取到）的经验记不下来：如实拒收并留痕，落库记忆可查出处。"""
        with fx.temp_store("p12-failure-zero.sqlite3") as (_database, store):
            receipt = exp.write_experience_memories(store, traces=[_zero_adoption_trace()],
                                                    run_meta=fx.run_meta())
            decisions = [row for row in receipt["decisions"] if row["memory_type"] == "FAILURE"]
            self.assertTrue(decisions)
            reasons = {row["reason"] for row in decisions}
            self.assertTrue(reasons <= {"NO_EVIDENCE_LINK", "UTILITY_BELOW_FLOOR"}, reasons)
            self.assertEqual(store.load_memory_items(memory_types=["FAILURE"],
                                                     include_all_scopes=True), [])


if __name__ == "__main__":
    unittest.main()

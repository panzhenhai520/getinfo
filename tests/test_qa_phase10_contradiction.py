#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 10 · P10-04 `MemoryContradiction` + P10-05 `SUPERSEDED_BY` 用例。

钉住：
  1. 矛盾裁决**只有 Phase 06 一个决策点**：回执里的 `decider` 必须是
     `rule:qa-contradiction-resolver-v1`，`reason_code` 必须落在 Phase 06 的理由码表里；
  2. 两族矛盾都真能检出：`memory_memory`（同主体、否定极性相反且指向同一话题）与
     `memory_evidence`（P10-03 判 REFUTED 的那条证据）；
  3. §11 "禁止最新自动覆盖"：只有 `NEWER_VERSION_PRECEDES` 才 SUPERSEDE，
     权威/质量/独立性/强度裁决走的都是 `CONTRADICTED`；
  4. 未消解（`unresolved`）→ 双方都 `CONTRADICTED`（status!=ACTIVE 不作证据）；
  5. `SCOPE_DIFFERENCE` → `KEEP_BOTH`（两份都成立，不动状态）；
  6. `SUPERSEDED_BY` 不许悬空：后继不是记忆时退化为 `CONTRADICTED` 并记 `supersede_degraded`；
  7. 取代写三件东西（字段 + `SUPERSEDES` 边 + 版本行），且**幂等**；
  8. 取代链可复算：悬空 / 环 / 字段有边无 都能被 `supersession_chains()` 查出来；
  9. 失败路径：`REVOKED` 不许被取代、不许自己取代自己、脏数据不抛异常。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/tests")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests"))

import qa_graph_contracts as contracts  # noqa: E402
import qa_memory_revalidation as mr  # noqa: E402

import qa_phase10_fixtures as fx  # noqa: E402


class ContradictionDetectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = fx.temp_store()
        self.database, self.store = self._tmp.__enter__()
        self.assertEqual(self.database.backend, "sqlite", "测试必须跑在隔离 sqlite 上")

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_negated_pair_is_detected_and_superseded_by_time(self):
        left, right = fx.contradiction_pair(self.store)
        report = mr.detect_memory_contradictions(self.store, run_id="run-1", now=fx.NOW)
        self.assertEqual(report["candidates"], 1)
        self.assertEqual(report["by_kind"], {"memory_memory": 1})
        self.assertEqual(report["by_resolution"], {"resolved": 1})
        self.assertEqual(report["by_reason_code"], {"NEWER_VERSION_PRECEDES": 1})
        self.assertEqual(report["by_status_action"], {"SUPERSEDE": 1})
        self.assertEqual(report["supersessions"], 1)
        row = report["contradictions"][0]
        self.assertEqual(row["decider"], "rule:qa-contradiction-resolver-v1",
                         "裁决必须来自 Phase 06 的规则裁决器（单一真源）")
        self.assertIn(row["reason_code"], contracts.CONTRADICTION_RESOLUTION_CODES)
        self.assertEqual(fx.load(self.store, "MEM-left")["status"], "SUPERSEDED")
        self.assertEqual(fx.load(self.store, "MEM-left")["superseded_by"], "MEM-right")
        self.assertEqual(fx.load(self.store, "MEM-right")["status"], "ACTIVE")
        relations = self.store.memory_relations(["MEM-right"], relations=["SUPERSEDES"])
        self.assertEqual([row["target_memory_id"] for row in relations], ["MEM-left"])

    def test_contradiction_rows_are_persisted_and_idempotent(self):
        fx.contradiction_pair(self.store)
        first = mr.detect_memory_contradictions(self.store, run_id="run-1", now=fx.NOW)
        versions = len(fx.version_rows(self.store, "MEM-left"))
        second = mr.detect_memory_contradictions(self.store, run_id="run-1", now=fx.NOW)
        self.assertEqual(len(fx.contradiction_ids(self.store)), 1)
        self.assertEqual(first["persisted"], 1)
        self.assertEqual(second["persisted"], 1)
        self.assertEqual(len(fx.version_rows(self.store, "MEM-left")), versions,
                         "第二遍不许重复取代（幂等）")
        self.assertEqual(second["supersessions"], 0)
        stored = self.store.memory_contradictions()
        self.assertEqual(stored[0]["reason_code"], "NEWER_VERSION_PRECEDES")
        self.assertEqual(stored[0]["status_action"], "SUPERSEDE")

    def test_same_subject_without_opposite_polarity_is_not_a_contradiction(self):
        fx.write_items(self.store, [
            fx.memory_item(fx.SUPPORTED_CLAIM, entity_ids=["hk"], memory_id="MEM-a"),
            fx.memory_item(fx.OTHER_CLAIM, entity_ids=["hk"], memory_id="MEM-b"),
        ])
        report = mr.detect_memory_contradictions(self.store, run_id="run-1", now=fx.NOW)
        self.assertEqual(report["candidates"], 0, "同一主体但极性一致 → 不是矛盾")

    def test_different_subjects_are_not_paired(self):
        fx.write_items(self.store, [
            fx.memory_item(fx.SUPPORTED_CLAIM, entity_ids=["hk"], memory_id="MEM-a",
                           scope="GLOBAL_KNOWLEDGE"),
            fx.memory_item(fx.NEGATED_CLAIM, entity_ids=["sg"], memory_id="MEM-b",
                           scope="ORGANIZATION"),
        ])
        report = mr.detect_memory_contradictions(self.store, run_id="run-1", now=fx.NOW)
        self.assertEqual(report["candidates"], 0, "不同主体 + 不同作用域 → 不配对")

    def test_index_memories_never_pair_with_claims(self):
        """真机踩过的假矛盾：**实体记忆只是索引**，不许与"提到该实体且带否定词"的断言配对。"""
        fx.write_items(self.store, [
            fx.memory_item("私募基金", memory_type="ENTITY", entity_ids=["私募基金"],
                           claim_type="entity", memory_id="MEM-entity"),
            fx.memory_item("私募基金备案新规不予适用于创业投资基金", memory_type="VERIFIED_CLAIM",
                           entity_ids=["私募基金"], valid_from="2026-04-01",
                           memory_id="MEM-claim"),
        ])
        report = mr.detect_memory_contradictions(self.store, run_id="run-1", now=fx.NOW)
        self.assertEqual(report["candidates"], 0, "索引类记忆不参与对错判定")
        self.assertIn("VERIFIED_CLAIM", report["eligible_types"])
        self.assertNotIn("ENTITY", report["eligible_types"])
        self.assertEqual(fx.load(self.store, "MEM-entity")["status"], "ACTIVE")
        self.assertEqual(fx.load(self.store, "MEM-claim")["status"], "ACTIVE")

    def test_eligible_types_are_configurable(self):
        os.environ["QA_MEMORY_CONTRADICTION_TYPES"] = "ENTITY"
        try:
            self.assertEqual(mr.contradiction_types(), ("ENTITY",))
            fx.write_items(self.store, [
                fx.memory_item("私募基金", memory_type="ENTITY", entity_ids=["私募基金"],
                               memory_id="MEM-entity"),
                fx.memory_item("私募基金备案新规不予适用于创业投资基金",
                               memory_type="VERIFIED_CLAIM", entity_ids=["私募基金"],
                               memory_id="MEM-claim"),
            ])
            report = mr.detect_memory_contradictions(self.store, run_id="run-1", now=fx.NOW)
            self.assertEqual(report["eligible_types"], ["ENTITY"])
            self.assertEqual(report["candidates"], 0, "白名单换成 ENTITY 后断言类反而被排除")
        finally:
            os.environ.pop("QA_MEMORY_CONTRADICTION_TYPES", None)

    def test_shared_entity_without_shared_proposition_is_not_a_contradiction(self):
        """真机踩过的第二类假矛盾：两条**不相干的长段落**共享几个公共实体词 → 不许配对。

        真机上"已检索到参考资料《…》其内容载明：…"这类长结论共享私募基金/股权投资等词，
        长文里出现一次"不予"就被 `check_negation` 锚定 —— 词面重叠只有 0.10，必须被门槛挡住。
        """
        fx.write_items(self.store, [
            fx.memory_item("已检索到参考资料《私募基金备案案例》，其内容载明：协会不予备案"
                           "某类产品。", entity_ids=["私募基金", "股权投资", "创业投资"],
                           valid_from="2026-04-01", memory_id="MEM-long-a"),
            fx.memory_item("已检索到参考资料《投后管理实务》，其内容载明：A股IPO上市后投资人"
                           "的减持退出路径与注意事项。",
                           entity_ids=["私募基金", "股权投资", "投后管理"],
                           valid_from="2026-09-01", memory_id="MEM-long-b"),
        ])
        report = mr.detect_memory_contradictions(self.store, run_id="run-1", now=fx.NOW)
        self.assertEqual(report["candidates"], 0,
                         "主体（实体 Jaccard）与命题（词面重叠）必须同时达标才算矛盾")
        self.assertEqual(fx.load(self.store, "MEM-long-a")["status"], "ACTIVE")
        self.assertEqual(fx.load(self.store, "MEM-long-b")["status"], "ACTIVE")

    def test_overlap_and_jaccard_thresholds_are_configurable(self):
        os.environ["QA_MEMORY_CONTRADICTION_OVERLAP"] = "0.05"
        os.environ["QA_MEMORY_CONTRADICTION_JACCARD"] = "0.6"
        try:
            self.assertEqual(mr.contradiction_overlap(), 0.05)
            self.assertEqual(mr.contradiction_jaccard(), 0.6)
            fx.write_items(self.store, [
                fx.memory_item("已检索到参考资料《私募基金备案案例》，其内容载明：协会不予备案"
                               "某类产品。", entity_ids=["私募基金", "股权投资", "创业投资"],
                               valid_from="2026-04-01", memory_id="MEM-long-a"),
                fx.memory_item("已检索到参考资料《投后管理实务》，其内容载明：A股IPO上市后投资人"
                               "的减持退出路径与注意事项。",
                               entity_ids=["私募基金", "股权投资", "投后管理"],
                               valid_from="2026-09-01", memory_id="MEM-long-b"),
            ])
            report = mr.detect_memory_contradictions(self.store, run_id="run-1", now=fx.NOW)
            self.assertEqual(report["candidates"], 0, "实体 Jaccard 2/4 = 0.5 < 0.6 → 仍不配对")
        finally:
            os.environ.pop("QA_MEMORY_CONTRADICTION_OVERLAP", None)
            os.environ.pop("QA_MEMORY_CONTRADICTION_JACCARD", None)

    def test_memories_without_entities_need_verbatim_overlap(self):
        """两侧都没有实体信息时，只认"近乎逐字相同"的命题（否则一次否定词就能造假矛盾）。"""
        fx.write_items(self.store, [
            fx.memory_item(fx.SUPPORTED_CLAIM, entity_ids=(), valid_from="2026-01-01",
                           memory_id="MEM-noent-a"),
            fx.memory_item(fx.NEGATED_CLAIM, entity_ids=(), valid_from="2026-09-01",
                           memory_id="MEM-noent-b"),
            fx.memory_item("已检索到参考资料《投后管理实务》，其内容载明：A股IPO上市后投资人的"
                           "减持退出路径与注意事项。", entity_ids=(), valid_from="2026-09-02",
                           memory_id="MEM-noent-c"),
        ])
        report = mr.detect_memory_contradictions(self.store, run_id="run-1", now=fx.NOW)
        self.assertEqual(report["candidates"], 1, "只有近乎逐字相同的那一对才算矛盾")
        self.assertEqual(report["contradictions"][0]["left_memory_id"], "MEM-noent-a")
        self.assertEqual(report["contradictions"][0]["right_memory_id"], "MEM-noent-b")

    def test_revalidation_timestamp_is_not_used_as_effective_date(self):
        """`last_verified_at` 是"上次复验时间"，不是"生效时间"：不许拿它压过 valid_from。"""
        left = mr.side_metrics(item=fx.memory_item(valid_from="2026-01-01",
                                                   last_verified_at="2026-10-11T00:00:00Z"))
        right = mr.side_metrics(item=fx.memory_item(valid_from="2026-09-01",
                                                    last_verified_at="2026-10-05T00:00:00Z"))
        self.assertTrue(left["claim_latest"].startswith("2026-01-01"))
        self.assertTrue(right["claim_latest"].startswith("2026-09-01"))
        self.assertLess(left["claim_latest"], right["claim_latest"])

    def test_scope_difference_keeps_both(self):
        fx.write_items(self.store, [
            fx.memory_item(fx.SUPPORTED_CLAIM, entity_ids=["hk"], memory_id="MEM-a",
                           scope="GLOBAL_KNOWLEDGE"),
            fx.memory_item(fx.NEGATED_CLAIM, entity_ids=["hk"], memory_id="MEM-b",
                           scope="ORGANIZATION"),
        ])
        report = mr.detect_memory_contradictions(self.store, run_id="run-1", now=fx.NOW)
        self.assertEqual(report["candidates"], 1)
        row = report["contradictions"][0]
        self.assertEqual(row["conflict_type"], "scope_difference")
        self.assertEqual(row["reason_code"], "SCOPE_DIFFERENCE")
        self.assertEqual(row["status_action"], "KEEP_BOTH")
        self.assertEqual(fx.load(self.store, "MEM-a")["status"], "ACTIVE")
        self.assertEqual(fx.load(self.store, "MEM-b")["status"], "ACTIVE")

    def test_unresolved_conflict_contradicts_both_sides(self):
        # 同一生效时间 → 时间规则不动；两边指标完全对称 → 条条都不满足 → NO_DECISIVE_RULE
        fx.write_items(self.store, [
            fx.memory_item(fx.SUPPORTED_CLAIM, entity_ids=["hk"], valid_from="2026-04-01",
                           memory_id="MEM-a"),
            fx.memory_item(fx.NEGATED_CLAIM, entity_ids=["hk"], valid_from="2026-04-01",
                           memory_id="MEM-b"),
        ])
        report = mr.detect_memory_contradictions(self.store, run_id="run-1", now=fx.NOW)
        row = report["contradictions"][0]
        self.assertEqual(row["resolution"], "unresolved")
        self.assertEqual(row["reason_code"], "NO_DECISIVE_RULE")
        self.assertEqual(row["status_action"], "CONTRADICT")
        self.assertEqual(fx.load(self.store, "MEM-a")["status"], "CONTRADICTED")
        self.assertEqual(fx.load(self.store, "MEM-b")["status"], "CONTRADICTED")
        self.assertEqual(report["by_status_action"], {"CONTRADICT": 1})
        targets = {action["memory_id"] for action in row["status_actions"]}
        self.assertEqual(targets, {"MEM-a", "MEM-b"})

    def test_unresolved_status_is_configurable(self):
        os.environ["QA_MEMORY_UNRESOLVED_STATUS"] = "STALE"
        try:
            self.assertEqual(mr.unresolved_conflict_status(), "STALE")
            fx.write_items(self.store, [
                fx.memory_item(fx.SUPPORTED_CLAIM, entity_ids=["hk"], valid_from="2026-04-01",
                               memory_id="MEM-a"),
                fx.memory_item(fx.NEGATED_CLAIM, entity_ids=["hk"], valid_from="2026-04-01",
                               memory_id="MEM-b"),
            ])
            mr.detect_memory_contradictions(self.store, run_id="run-1", now=fx.NOW)
            self.assertEqual(fx.load(self.store, "MEM-a")["status"], "STALE")
        finally:
            os.environ.pop("QA_MEMORY_UNRESOLVED_STATUS", None)

    def test_authority_advantage_contradicts_the_loser_instead_of_superseding(self):
        # 同一生效时间（时间规则不动）→ 走权威度规则 → 只 CONTRADICTED，不 SUPERSEDE
        fx.write_items(self.store, [
            fx.memory_item(fx.SUPPORTED_CLAIM, entity_ids=["hk"], valid_from="2026-04-01",
                           memory_id="MEM-a"),
            fx.memory_item(fx.NEGATED_CLAIM, entity_ids=["hk"], valid_from="2026-04-01",
                           memory_id="MEM-b", confidence=0.9),
        ])
        links = [{"memory_id": "MEM-a", "evidence_ref": "article:1", "source_fingerprint": "SF-A",
                  "verdict": "SUPPORTED", "evidence_score": 0.2,
                  "metadata": {"authority_level": 10, "published_at": "2026-04-01"}}]
        receipts = []
        left = fx.load(self.store, "MEM-a")
        right = fx.load(self.store, "MEM-b")
        receipts.append(mr.adjudicate_memory_contradiction(
            "memory_memory", left_item=left, left_links=links, right_item=right,
            right_links=[{**links[0], "memory_id": "MEM-b", "source_fingerprint": "SF-B",
                          "evidence_score": 0.9,
                          "metadata": {"authority_level": 90, "published_at": "2026-04-01"}}],
            run_id="run-1"))
        row = receipts[0]
        self.assertEqual(row["reason_code"], "AUTHORITY_ADVANTAGE")
        self.assertEqual(row["winner"], "right")
        self.assertEqual(row["status_action"], "CONTRADICT")
        self.assertEqual(row["status_target"], "CONTRADICTED")

    def test_pair_cap_is_respected(self):
        fx.write_items(self.store, [
            fx.memory_item(fx.SUPPORTED_CLAIM, entity_ids=["hk"], memory_id="MEM-a",
                           valid_from="2026-01-01"),
            fx.memory_item(fx.NEGATED_CLAIM, entity_ids=["hk"], memory_id="MEM-b",
                           valid_from="2026-02-01"),
            fx.memory_item(fx.SUPPORTED_CLAIM_2, entity_ids=["hk"], memory_id="MEM-c",
                           valid_from="2026-03-01"),
        ])
        original = mr.MAX_CONTRADICTION_PAIRS
        mr.MAX_CONTRADICTION_PAIRS = 1
        try:
            report = mr.detect_memory_contradictions(self.store, run_id="run-1", now=fx.NOW)
            self.assertEqual(report["candidates"], 1, "配对上限必须生效（确定性上限）")
        finally:
            mr.MAX_CONTRADICTION_PAIRS = original


class MemoryEvidenceContradictionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = fx.temp_store()
        self.database, self.store = self._tmp.__enter__()
        self.assertEqual(self.database.backend, "sqlite", "测试必须跑在隔离 sqlite 上")

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_refuted_revalidation_becomes_memory_evidence_contradiction(self):
        fx.write_items(self.store, [fx.memory_item(freshness="VERSION_SENSITIVE",
                                                   claim_type="policy", memory_id="MEM-left")])
        refuting = fx.verified_evidence("article:2",
                                        claim_text="香港家族办公室税收优惠政策不予给予"
                                                   "合资格基金管理人利得税宽免",
                                        text="香港家族办公室税收优惠政策不予给予合资格基金管理人"
                                             "利得税宽免。", published_at="2026-10-09")
        validation = mr.revalidate_item(self.store, fx.load(self.store, "MEM-left"),
                                        current_evidence=[refuting], run_id="run-1", now=fx.NOW)
        self.assertEqual(validation["outcome"], "REFUTED")
        self.assertEqual(validation["refuting_evidence_ref"], "article:2")
        report = mr.detect_memory_contradictions(self.store, items=[fx.load(self.store, "MEM-left")],
                                                 validations=[validation], run_id="run-1",
                                                 now=fx.NOW)
        self.assertEqual(report["candidates"], 1)
        row = report["contradictions"][0]
        self.assertEqual(row["kind"], "memory_evidence")
        self.assertEqual(row["right_evidence_ref"], "article:2")
        self.assertIn(row["reason_code"], contracts.CONTRADICTION_RESOLUTION_CODES)
        self.assertEqual(row["status_action"], "CONTRADICT")
        self.assertEqual(fx.load(self.store, "MEM-left")["status"], "CONTRADICTED")
        stored = self.store.memory_contradictions()
        self.assertEqual(stored[0]["kind"], "memory_evidence")
        self.assertEqual(stored[0]["right_evidence_ref"], "article:2")

    def test_supersede_degrades_when_successor_is_evidence_not_memory(self):
        item = fx.memory_item(valid_from="2026-04-01", memory_id="MEM-left",
                              freshness="VERSION_SENSITIVE", claim_type="policy")
        evidence = fx.evidence_item("article:2", published_at="2026-10-09")
        verification = {"verdict": "REFUTED", "score": 0.9,
                        "checks": [{"check": "numbers", "applicable": False}]}
        row = mr.adjudicate_memory_contradiction(
            "memory_evidence", left_item=item, left_links=[], right_evidence=evidence,
            right_verification=verification, evidence_ref="article:2", run_id="run-1")
        self.assertEqual(row["reason_code"], "NEWER_VERSION_PRECEDES")
        self.assertTrue(row["supersede_degraded"])
        self.assertEqual(row["status_action"], "CONTRADICT")
        self.assertEqual(row["status_target"], "CONTRADICTED")
        self.assertIn("SUPERSEDED_BY 不允许悬空", row["rationale"])


class SupersessionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = fx.temp_store()
        self.database, self.store = self._tmp.__enter__()
        self.assertEqual(self.database.backend, "sqlite", "测试必须跑在隔离 sqlite 上")
        fx.write_items(self.store, [
            fx.memory_item(fx.SUPPORTED_CLAIM, memory_id="MEM-old", valid_from="2026-01-01"),
            fx.memory_item(fx.NEGATED_CLAIM, memory_id="MEM-new", valid_from="2026-09-01"),
            fx.memory_item(fx.SUPPORTED_CLAIM_2, memory_id="MEM-other", valid_from="2026-05-01"),
        ])

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_supersede_writes_field_edge_and_version(self):
        receipt = mr.supersede_memory(self.store, "MEM-old", "MEM-new",
                                      reason="NEWER_VERSION_PRECEDES",
                                      rationale="较新的一方用于当前结论", run_id="run-1")
        self.assertTrue(receipt["changed"])
        self.assertEqual(receipt["relation"], "SUPERSEDES")
        self.assertEqual(receipt["relation_rows"], 1)
        self.assertTrue(receipt["contract_ok"])
        row = fx.load(self.store, "MEM-old")
        self.assertEqual((row["status"], row["superseded_by"]), ("SUPERSEDED", "MEM-new"))
        versions = fx.version_rows(self.store, "MEM-old")
        self.assertEqual(versions[-1]["change"], "SUPERSEDE")
        edges = self.store.memory_relations(["MEM-new"], relations=["SUPERSEDES"])
        self.assertEqual([edge["target_memory_id"] for edge in edges], ["MEM-old"])

    def test_supersede_is_idempotent(self):
        self.assertTrue(mr.supersede_memory(self.store, "MEM-old", "MEM-new")["changed"])
        versions = len(fx.version_rows(self.store, "MEM-old"))
        edges = len(self.store.memory_relations(["MEM-new"], relations=["SUPERSEDES"]))
        again = mr.supersede_memory(self.store, "MEM-old", "MEM-new")
        self.assertFalse(again["changed"])
        self.assertEqual(len(fx.version_rows(self.store, "MEM-old")), versions)
        self.assertEqual(len(self.store.memory_relations(["MEM-new"], relations=["SUPERSEDES"])),
                         edges, "幂等：不许重复写关系边")

    def test_second_different_successor_does_not_overwrite_the_chain(self):
        """§11 禁止"最新自动覆盖"：取代链单调，已指向别的后继就不许改写。"""
        mr.supersede_memory(self.store, "MEM-old", "MEM-new")
        versions = len(fx.version_rows(self.store, "MEM-old"))
        other = mr.supersede_memory(self.store, "MEM-old", "MEM-other")
        self.assertFalse(other["changed"])
        self.assertEqual(other["error"], "ALREADY_SUPERSEDED_BY_OTHER")
        self.assertEqual(other["superseded_by"], "MEM-new")
        self.assertEqual(fx.load(self.store, "MEM-old")["superseded_by"], "MEM-new")
        self.assertEqual(len(fx.version_rows(self.store, "MEM-old")), versions)

    def test_one_memory_gets_a_single_canonical_successor(self):
        """同一轮的既有反义对（MEM-old 老 / MEM-new 新）只写**一行**取代版本。"""
        report = mr.detect_memory_contradictions(self.store, run_id="run-1", now=fx.NOW)
        self.assertGreaterEqual(report["candidates"], 1)
        # MEM-old（最老）与 MEM-other（次老）都被 MEM-new 取代；**各只写一行**
        for memory_id in ("MEM-old", "MEM-other"):
            self.assertEqual(len(fx.version_rows(self.store, memory_id)), 2,
                             "%s 只许追加一行取代版本" % memory_id)
            self.assertEqual(fx.load(self.store, memory_id)["superseded_by"], "MEM-new")
        self.assertEqual(report["supersessions"], 2)
        applied = [action for row in report["contradictions"] for action in row["status_actions"]]
        self.assertTrue(applied, "取代动作要落进回执（可审计）")

    def test_revoked_memory_cannot_be_superseded(self):
        self.store.revoke_memory_items(["MEM-old"], reason="MANUAL_REVOKE")
        receipt = mr.supersede_memory(self.store, "MEM-old", "MEM-new")
        self.assertFalse(receipt["changed"])
        self.assertIn("REVOKED", receipt["error"])
        self.assertEqual(fx.load(self.store, "MEM-old")["status"], "REVOKED")

    def test_self_supersede_is_rejected(self):
        receipt = mr.supersede_memory(self.store, "MEM-old", "MEM-old")
        self.assertFalse(receipt["changed"])
        self.assertTrue(receipt["error"])

    def test_unknown_reason_falls_back_to_manual(self):
        receipt = mr.supersede_memory(self.store, "MEM-other", "MEM-new", reason="MADE_UP")
        self.assertEqual(receipt["reason"], "MANUAL_SUPERSEDE")

    def test_chain_audit_detects_field_without_edge(self):
        mr.supersede_memory(self.store, "MEM-old", "MEM-new")
        self.store.database.connection.execute(
            "DELETE FROM memory_relation WHERE relation='SUPERSEDES'")
        self.store.database.connection.commit()
        audit = mr.supersession_chains(self.store)
        issues = {row["issue"] for row in audit["problems"]}
        self.assertIn("SUPERSEDED_BY_FIELD_WITHOUT_EDGE", issues)

    def test_chain_audit_detects_dangling_and_cycle(self):
        mr.supersede_memory(self.store, "MEM-old", "MEM-new")
        self.store.database.connection.execute(
            "UPDATE memory_item SET superseded_by='MEM-ghost' WHERE memory_id='MEM-old'")
        self.store.database.connection.commit()
        audit = mr.supersession_chains(self.store)
        issues = {row["issue"] for row in audit["problems"]}
        self.assertIn("SUPERSESSION_DANGLING", issues)
        # 造一个环：MEM-new 被 MEM-other 取代、MEM-other 又被 MEM-new 取代
        mr.supersede_memory(self.store, "MEM-new", "MEM-other")
        self.store.database.connection.execute(
            "UPDATE memory_item SET superseded_by='MEM-new' WHERE memory_id='MEM-other'")
        self.store.database.connection.execute(
            "UPDATE memory_item SET status='SUPERSEDED' WHERE memory_id='MEM-other'")
        self.store.database.connection.commit()
        audit = mr.supersession_chains(self.store)
        issues = {row["issue"] for row in audit["problems"]}
        self.assertIn("SUPERSESSION_CYCLE", issues)

    def test_chain_audit_reports_clean_chain(self):
        mr.supersede_memory(self.store, "MEM-old", "MEM-new")
        audit = mr.supersession_chains(self.store, memory_ids=["MEM-old", "MEM-new"])
        self.assertEqual(audit["problems"], [])
        self.assertEqual(audit["chains"][0]["chain"], ["MEM-old", "MEM-new"])
        self.assertEqual(audit["superseded"], 1)

    def test_dirty_supersession_target_does_not_raise(self):
        receipt = mr.supersede_memory(self.store, "MEM-old", "")
        self.assertFalse(receipt["changed"])
        self.assertTrue(receipt["error"])
        receipt = mr.supersede_memory(self.store, "MEM-ghost", "MEM-new")
        self.assertFalse(receipt["changed"])
        self.assertEqual(receipt["error"], "记忆不存在")


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 10 · P10-03 `MEMORY_HINT revalidation` 用例。

钉住：
  1. 复验的判定器**只有 Phase 03**（`verify_evidence_item`）：判 SUPPORTED 才 `REVALIDATED`，
     判 REFUTED 就 `REFUTED`，判不到就 `UNVERIFIED` —— 没有第二套质量判断；
  2. `REVALIDATED` 的语义被 `verified_scope="evidence_refs"` 钉死（MASTER_RULES 11），
     并且真的把**新证据绑定行**写进 `memory_evidence_link`、刷新 `last_verified_at`/置信/TTL；
  3. 状态回退只发生在 `STALE`/`EXPIRED → ACTIVE`（P09 契约写明"回退要 Phase 10 的 revalidation"）；
  4. 闸门 ALLOW 时**不刷新时间戳**（否则衰减永远不走）；
  5. 高危钩子：高危且未复验成功 → `ACTIVE → STALE`（理由 `HIGH_RISK_UNREVALIDATED`）；
  6. **幂等**：同一时钟跑第二遍，0 写入、0 新版本行、0 新留痕行；
  7. 注入的判定器**只能否决提升**，坏掉的判定器回落到规则并记账；
  8. 失败路径：缺记忆 / 脏数据 / 库不可用都不抛异常。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_graph_contracts as contracts  # noqa: E402
import qa_memory_revalidation as mr  # noqa: E402

import qa_phase10_fixtures as fx  # noqa: E402

IRRELEVANT_TEXT = "论坛里随便聊了聊天气与球赛，完全没有提到任何政策安排。"


def _refuting_evidence(evidence_ref="article:2"):
    return fx.verified_evidence(evidence_ref, claim_text="香港家族办公室税收优惠政策不予给予"
                                                         "合资格基金管理人利得税宽免",
                                text="香港家族办公室税收优惠政策不予给予合资格基金管理人"
                                     "利得税宽免。")


class RevalidationOutcomeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = fx.temp_store()
        self.database, self.store = self._tmp.__enter__()
        self.assertEqual(self.database.backend, "sqlite", "测试必须跑在隔离 sqlite 上")

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def _seed(self, *, freshness="VERSION_SENSITIVE", claim_type="policy", status="ACTIVE",
              memory_id="MEM-left", content=None):
        item = fx.memory_item(content or fx.SUPPORTED_CLAIM, freshness=freshness,
                              claim_type=claim_type, status=status, memory_id=memory_id)
        fx.write_items(self.store, [item])
        return fx.load(self.store, memory_id)

    def test_supported_evidence_promotes_and_rebinds(self):
        item = self._seed()
        before = fx.load(self.store, "MEM-left")
        receipt = mr.revalidate_item(
            self.store, before, current_evidence=[fx.verified_evidence("article:1")],
            run_id="run-1", now=fx.NOW)
        self.assertEqual(receipt["outcome"], "REVALIDATED")
        self.assertEqual(receipt["reason"], "EVIDENCE_STILL_SUPPORTS")
        self.assertTrue(receipt["verified_evidence"])
        self.assertEqual(receipt["verified_scope"], "evidence_refs")
        self.assertIn("SUPPORTED", receipt["verdicts"])
        self.assertTrue(receipt["contract_ok"])
        after = fx.load(self.store, "MEM-left")
        self.assertGreaterEqual(after["confidence"], before["confidence"])
        self.assertNotEqual(after["last_verified_at"], before["last_verified_at"])
        self.assertEqual(after["valid_until"][:10], "2028-04-03", "VERSION_SENSITIVE 的 TTL 540 天")
        self.assertTrue(fx.links_of(self.store, "MEM-left"))
        self.assertEqual(len(fx.version_rows(self.store, "MEM-left")), 2,
                         "复验成功必须追加一行版本（不物理覆盖）")

    def test_stale_and_expired_are_resurrected(self):
        # 两条记忆必须内容不同（`UNIQUE(scope_key, memory_type, content_fingerprint)` 是 P09 的硬约束）
        for status, content in (("STALE", fx.SUPPORTED_CLAIM),
                                ("EXPIRED", fx.SUPPORTED_CLAIM_2)):
            memory_id = "MEM-%s" % status
            self._seed(status=status, memory_id=memory_id, content=content)
            receipt = mr.revalidate_item(
                self.store, fx.load(self.store, memory_id),
                current_evidence=[fx.verified_evidence("article:1")], run_id="run-1", now=fx.NOW)
            self.assertEqual(receipt["outcome"], "REVALIDATED")
            self.assertTrue(receipt["promoted"])
            self.assertEqual(receipt["status_after"], "ACTIVE")
            self.assertEqual(fx.load(self.store, memory_id)["status"], "ACTIVE")

    def test_no_supported_verdict_is_never_promoted(self):
        self._seed()
        receipt = mr.revalidate_item(
            self.store, fx.load(self.store, "MEM-left"),
            current_evidence=[fx.verified_evidence("article:99", claim_text="球赛很精彩",
                                                   text=IRRELEVANT_TEXT)],
            run_id="run-1", now=fx.NOW)
        self.assertIn(receipt["outcome"], ("UNVERIFIED", "NO_CANDIDATE_EVIDENCE"))
        self.assertFalse(receipt["verified_evidence"])
        self.assertEqual(fx.load(self.store, "MEM-left")["status"], "STALE",
                         "高危未复验成功 → 钩子降级为 STALE（不许留在 ACTIVE）")

    def test_refuting_evidence_yields_refuted(self):
        self._seed()
        receipt = mr.revalidate_item(self.store, fx.load(self.store, "MEM-left"),
                                     current_evidence=[_refuting_evidence()],
                                     run_id="run-1", now=fx.NOW)
        self.assertEqual(receipt["outcome"], "REFUTED")
        self.assertEqual(receipt["reason"], "EVIDENCE_REFUTES")
        self.assertFalse(receipt["verified_evidence"])
        self.assertIn("REFUTED", receipt["verdicts"])

    def test_no_candidate_evidence_is_reported_not_faked(self):
        self._seed()
        receipt = mr.revalidate_item(self.store, fx.load(self.store, "MEM-left"),
                                     current_evidence=[], run_id="run-1", now=fx.NOW)
        self.assertEqual(receipt["outcome"], "NO_CANDIDATE_EVIDENCE")
        self.assertEqual(receipt["reason"], "NO_EVIDENCE_AVAILABLE")
        self.assertEqual(receipt["candidates"], 0)

    def test_fresh_allowed_memory_is_not_revalidated_and_not_touched(self):
        item = fx.memory_item(freshness="LONG", claim_type="background", memory_id="MEM-fresh")
        fx.write_items(self.store, [item])
        before = fx.load(self.store, "MEM-fresh")
        receipt = mr.revalidate_item(self.store, before,
                                     current_evidence=[fx.verified_evidence("article:1")],
                                     run_id="run-1", now=fx.NOW)
        self.assertEqual(receipt["outcome"], "REFRESHED_NO_CHANGE")
        self.assertFalse(receipt["written"])
        after = fx.load(self.store, "MEM-fresh")
        self.assertEqual(after["last_verified_at"], before["last_verified_at"],
                         "闸门 ALLOW 不许刷新时间戳（否则衰减永远不走）")
        self.assertEqual(len(fx.version_rows(self.store, "MEM-fresh")), 1)
        self.assertEqual(receipt["evidence_refs"], [])

    def test_revoked_memory_is_skipped(self):
        self._seed()
        self.store.revoke_memory_items(["MEM-left"], reason="MANUAL_REVOKE")
        receipt = mr.revalidate_item(self.store, fx.load(self.store, "MEM-left"),
                                     current_evidence=[fx.verified_evidence("article:1")],
                                     run_id="run-1", now=fx.NOW)
        self.assertEqual(receipt["outcome"], "SKIPPED_NOT_ACTIVE")
        self.assertEqual(receipt["reason"], "MEMORY_NOT_ACTIVE")
        self.assertEqual(fx.load(self.store, "MEM-left")["status"], "REVOKED")

    def test_decay_below_floor_is_blocked(self):
        item = fx.memory_item(freshness="SHORT", claim_type="background",
                              last_verified_at="2015-01-01T00:00:00Z",
                              created_at="2015-01-01T00:00:00Z", memory_id="MEM-old")
        fx.write_items(self.store, [item])
        receipt = mr.revalidate_item(self.store, fx.load(self.store, "MEM-old"),
                                     current_evidence=[fx.verified_evidence("article:1")],
                                     run_id="run-1", now=fx.NOW)
        self.assertEqual(receipt["outcome"], "BLOCKED_BY_GATE")
        self.assertEqual(receipt["reason"], "GATE_BLOCKED")

    def test_idempotent_on_second_run(self):
        self._seed(status="STALE")
        first = mr.revalidate_item(self.store, fx.load(self.store, "MEM-left"),
                                   current_evidence=[fx.verified_evidence("article:1")],
                                   run_id="run-1", now=fx.NOW)
        versions_after_first = len(fx.version_rows(self.store, "MEM-left"))
        second = mr.revalidate_item(self.store, fx.load(self.store, "MEM-left"),
                                    current_evidence=[fx.verified_evidence("article:1")],
                                    run_id="run-1", now=fx.NOW)
        self.assertTrue(first["written"])
        self.assertFalse(second["written"], "同钟重放不许再写库（补丁与库里逐字段相同）")
        self.assertEqual(len(fx.version_rows(self.store, "MEM-left")), versions_after_first,
                         "第二遍不许追加版本行")
        # 第一遍把 STALE 放回了 ACTIVE，所以第二遍的 status_before 不同 → 是一条**新**留痕；
        # 这不是不幂等，而是"状态真的变了"的如实记录（下面一条用例钉死不变状态下的单行）
        self.assertNotEqual(second["validation_id"], first["validation_id"])
        self.assertEqual(len(fx.revalidation_ids(self.store)), 2)

    def test_unchanged_state_replays_to_the_same_validation_row(self):
        # 闸门 ALLOW 的记忆：重放既不改状态、也不改时间戳 → 同一条留痕（真正的不变状态）
        item = fx.memory_item(freshness="LONG", claim_type="background", memory_id="MEM-left")
        fx.write_items(self.store, [item])
        first = mr.revalidate_item(self.store, fx.load(self.store, "MEM-left"),
                                   current_evidence=[fx.verified_evidence("article:1")],
                                   run_id="run-1", now=fx.NOW)
        rows_after_first = len(fx.revalidation_ids(self.store))
        versions_after_first = len(fx.version_rows(self.store, "MEM-left"))
        second = mr.revalidate_item(self.store, fx.load(self.store, "MEM-left"),
                                    current_evidence=[fx.verified_evidence("article:1")],
                                    run_id="run-1", now=fx.NOW)
        self.assertEqual(second["validation_id"], first["validation_id"])
        self.assertEqual(len(fx.revalidation_ids(self.store)), rows_after_first)
        self.assertFalse(second["written"])
        self.assertEqual(len(fx.version_rows(self.store, "MEM-left")), versions_after_first)

    def test_high_risk_downgrade_is_not_repeated_on_replay(self):
        self._seed(memory_id="MEM-left")          # 高危 + 无候选证据
        first = mr.revalidate_item(self.store, fx.load(self.store, "MEM-left"),
                                   current_evidence=[], run_id="run-1", now=fx.NOW)
        self.assertTrue(first["high_risk_hook"]["applied"])
        versions_after_first = len(fx.version_rows(self.store, "MEM-left"))
        second = mr.revalidate_item(self.store, fx.load(self.store, "MEM-left"),
                                    current_evidence=[], run_id="run-1", now=fx.NOW)
        self.assertFalse(second["high_risk_hook"]["applied"])
        self.assertEqual(second["high_risk_hook"]["action"], "ALREADY_NOT_ACTIVE")
        self.assertFalse(second["written"])
        self.assertEqual(len(fx.version_rows(self.store, "MEM-left")), versions_after_first,
                         "已降级的记忆不许被重复降级（幂等）")
        self.assertEqual(fx.load(self.store, "MEM-left")["status"], "STALE")

    def test_high_risk_hook_downgrades_unrevalidated_memory(self):
        self._seed()                                     # policy + VERSION_SENSITIVE = 高危
        receipt = mr.revalidate_item(self.store, fx.load(self.store, "MEM-left"),
                                     current_evidence=[], run_id="run-1", now=fx.NOW)
        self.assertTrue(receipt["high_stakes"])
        self.assertEqual(receipt["outcome"], "NO_CANDIDATE_EVIDENCE")
        self.assertTrue(receipt["high_risk_hook"]["applied"])
        self.assertEqual(receipt["high_risk_hook"]["action"], "DOWNGRADE_TO_STALE")
        self.assertEqual(receipt["status_after"], "STALE")
        self.assertEqual(fx.load(self.store, "MEM-left")["status"], "STALE")

    def test_high_risk_revalidated_memory_stays_active(self):
        self._seed()
        receipt = mr.revalidate_item(self.store, fx.load(self.store, "MEM-left"),
                                     current_evidence=[fx.verified_evidence("article:1")],
                                     run_id="run-1", now=fx.NOW)
        self.assertEqual(receipt["high_risk_hook"]["action"], "REVALIDATED_HIGH_STAKES")
        self.assertFalse(receipt["high_risk_hook"]["applied"])
        self.assertEqual(fx.load(self.store, "MEM-left")["status"], "ACTIVE")

    def test_non_high_stakes_unverified_memory_is_not_downgraded(self):
        item = fx.memory_item(freshness="SHORT", claim_type="background", memory_id="MEM-plain")
        fx.write_items(self.store, [item])
        receipt = mr.revalidate_item(self.store, fx.load(self.store, "MEM-plain"),
                                     current_evidence=[], run_id="run-1", now=fx.NOW)
        self.assertFalse(receipt["high_stakes"])
        self.assertFalse(receipt["high_risk_hook"]["applied"])
        self.assertEqual(fx.load(self.store, "MEM-plain")["status"], "ACTIVE")

    def test_outcome_and_reason_are_always_in_the_contract_enums(self):
        self._seed()
        cases = [
            [fx.verified_evidence("article:1")], [_refuting_evidence()], [],
            [fx.verified_evidence("article:99", claim_text="球赛很精彩", text=IRRELEVANT_TEXT)],
        ]
        for evidence in cases:
            receipt = mr.revalidate_item(self.store, fx.load(self.store, "MEM-left"),
                                         current_evidence=evidence, run_id="r", now=fx.NOW)
            self.assertIn(receipt["outcome"], contracts.MEMORY_REVALIDATION_OUTCOMES)
            self.assertIn(receipt["reason"], contracts.MEMORY_REVALIDATION_REASONS)
            self.assertTrue(receipt["contract_ok"], receipt.get("contract_error"))


class JudgeInjectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = fx.temp_store()
        self.database, self.store = self._tmp.__enter__()
        self.assertEqual(self.database.backend, "sqlite", "测试必须跑在隔离 sqlite 上")
        fx.write_items(self.store, [fx.memory_item(freshness="VERSION_SENSITIVE",
                                                   claim_type="policy", memory_id="MEM-left")])

    def tearDown(self):
        self._tmp.__exit__(None, None, None)
        mr._REVALIDATION_JUDGES.pop("test_veto", None)
        mr._REVALIDATION_JUDGES.pop("test_broken", None)
        os.environ.pop("QA_MEMORY_REVALIDATION_JUDGE", None)

    def test_injected_judge_can_veto_promotion(self):
        mr.register_revalidation_judge("test_veto", lambda payload: {
            "outcome": "UNVERIFIED", "reason": "EVIDENCE_INSUFFICIENT"})
        receipt = mr.revalidate_item(
            self.store, fx.load(self.store, "MEM-left"),
            current_evidence=[fx.verified_evidence("article:1")], run_id="run-1", now=fx.NOW,
            judge=mr._REVALIDATION_JUDGES["test_veto"])
        self.assertEqual(receipt["outcome"], "UNVERIFIED")
        self.assertFalse(receipt["verified_evidence"])
        self.assertEqual(receipt["judge"], "injected:rule")

    def test_broken_judge_falls_back_to_rules_and_records_it(self):
        def broken(payload):
            raise RuntimeError("boom")

        mr.register_revalidation_judge("test_broken", broken)
        receipt = mr.revalidate_item(
            self.store, fx.load(self.store, "MEM-left"),
            current_evidence=[fx.verified_evidence("article:1")], run_id="run-1", now=fx.NOW,
            judge=broken)
        self.assertEqual(receipt["outcome"], "REVALIDATED", "坏判定器必须回落到规则")
        self.assertIn("judge_error", receipt["judge_fallback"])

    def test_unregistered_env_judge_falls_back(self):
        os.environ["QA_MEMORY_REVALIDATION_JUDGE"] = "not_registered"
        try:
            receipt = mr.revalidate_item(
                self.store, fx.load(self.store, "MEM-left"),
                current_evidence=[fx.verified_evidence("article:1")], run_id="run-1", now=fx.NOW)
            self.assertEqual(receipt["outcome"], "REVALIDATED")
            self.assertIn("judge_not_registered", receipt["judge_fallback"])
        finally:
            os.environ.pop("QA_MEMORY_REVALIDATION_JUDGE", None)

    def test_default_judge_is_rule_and_registry_is_empty(self):
        self.assertEqual(mr.judge_name(), "rule")
        self.assertEqual(mr.revalidation_judges(), ())
        self.assertFalse(mr.revalidation_enabled(), "管线开关默认必须关（回滚口径）")


class BatchRevalidationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = fx.temp_store()
        self.database, self.store = self._tmp.__enter__()
        self.assertEqual(self.database.backend, "sqlite", "测试必须跑在隔离 sqlite 上")

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_batch_distributions_and_deterministic_order(self):
        fx.write_items(self.store, [
            fx.memory_item(freshness="VERSION_SENSITIVE", claim_type="policy", memory_id="MEM-b"),
            fx.memory_item(content=fx.OTHER_CLAIM, freshness="LONG", claim_type="background",
                           memory_id="MEM-a"),
            fx.memory_item(content=fx.NEGATED_CLAIM, freshness="SHORT", claim_type="finance",
                           memory_id="MEM-c"),
        ])
        report = mr.revalidate(self.store, memory_ids=["MEM-c", "MEM-b", "MEM-a"],
                               current_evidence=[fx.verified_evidence("article:1")],
                               run_id="run-1", now=fx.NOW)
        self.assertEqual(report["checked"], 3)
        self.assertTrue(report["contract_ok"], report.get("contract_error"))
        self.assertEqual(sorted(report["outcomes"]), sorted(set(report["outcomes"])))
        self.assertEqual(sum(report["gate_decisions"].values()), 3)
        self.assertEqual(sum(report["outcomes"].values()), 3)
        self.assertEqual([row["memory_id"] for row in report["validations"]],
                         ["MEM-a", "MEM-b", "MEM-c"], "批量入口按 id 排序 → 同输入同输出")
        again = mr.revalidate(self.store, memory_ids=["MEM-c", "MEM-b", "MEM-a"],
                              current_evidence=[fx.verified_evidence("article:1")],
                              run_id="run-1", now=fx.NOW)
        self.assertEqual(again["outcomes"], report["outcomes"])

    def test_missing_memory_is_recorded_not_silently_dropped(self):
        report = mr.revalidate(self.store, memory_ids=["MEM-nope"], run_id="run-1", now=fx.NOW)
        self.assertEqual(report["checked"], 1)
        self.assertEqual(report["validations"][0].get("error"), "MEMORY_NOT_FOUND")

    def test_hints_are_rewritten_only_when_revalidated(self):
        fx.write_items(self.store, [
            fx.memory_item(freshness="VERSION_SENSITIVE", claim_type="policy", memory_id="MEM-b"),
            fx.memory_item(content=fx.SUPPORTED_CLAIM_2, freshness="LONG",
                           claim_type="background", memory_id="MEM-fresh"),
        ])
        hints = [{"memory_id": "MEM-b", "canonical_content": fx.SUPPORTED_CLAIM,
                  "evidence_refs": [], "requires_revalidation": True, "verified_evidence": False,
                  "status": "ACTIVE"},
                 {"memory_id": "MEM-fresh", "canonical_content": fx.SUPPORTED_CLAIM_2,
                  "evidence_refs": ["article:1"], "requires_revalidation": True,
                  "verified_evidence": False, "status": "ACTIVE"}]
        report = mr.revalidate(self.store, hints=hints,
                               current_evidence=[fx.verified_evidence("article:1")],
                               run_id="run-1", now=fx.NOW)
        by_id = {row["memory_id"]: row for row in report["hints"]}
        self.assertTrue(by_id["MEM-b"]["verified_evidence"])
        self.assertFalse(by_id["MEM-b"]["requires_revalidation"])
        self.assertEqual(by_id["MEM-b"]["verified_scope"], "evidence_refs")
        self.assertIn("article:1", by_id["MEM-b"]["evidence_refs"])
        self.assertFalse(by_id["MEM-fresh"]["verified_evidence"],
                         "闸门 ALLOW 不算「复验成功」（hint 语义不变）")
        self.assertTrue(by_id["MEM-fresh"]["requires_revalidation"])

    def test_run_revalidation_uses_graph_evidence_and_reports_receipt(self):
        from qa_phase09_fixtures import graph as build_graph
        fx.memory_with_evidence(self.store, freshness="VERSION_SENSITIVE", claim_type="policy",
                                memory_id="MEM-left")     # 绑定行带 corpus-p10
        payload = build_graph()          # 图里的 article:1 就是这条记忆绑定的那份证据
        report = mr.run_revalidation(self.store, memory_ids=["MEM-left"], graph=payload,
                                     run_meta={"id": "run-1", "corpus_version": "corpus-p09"},
                                     now=fx.NOW)
        self.assertEqual(report["checked"], 1)
        self.assertEqual(report["outcomes"], {"REVALIDATED": 1},
                         "候选证据来自 graph['evidence']（零新增检索）")
        self.assertEqual(report["revalidated"], 1)
        self.assertEqual(report["source_version"]["changed"], 1,
                         "证据行的 corpus_version 与 run 的不同 → 来源版本变更被检出")
        self.assertEqual(report["source_version"]["reasons"], {"CORPUS_VERSION_CHANGED": 1,
                                                              "NO_VERSION_TOKEN": 1},
                         "正文里没有显式版本标记 → 如实记 NO_VERSION_TOKEN（不猜）")
        receipt = mr.revalidation_receipt(report)
        self.assertEqual(receipt["checked"], 1)
        self.assertIn("contradictions", receipt)
        self.assertNotIn("validations", receipt, "回执不含正文/明细（给 stats 用）")

    def test_dirty_memory_row_does_not_break_the_batch(self):
        self.store.save_memory_item(fx.memory_item(memory_id="MEM-dirty"))
        self.store.database.connection.execute(
            "UPDATE memory_item SET freshness_class='NOPE', status='JUNK' WHERE memory_id='MEM-dirty'")
        self.store.database.connection.commit()
        report = mr.revalidate(self.store, memory_ids=["MEM-dirty"], run_id="run-1", now=fx.NOW)
        self.assertEqual(report["checked"], 1)
        self.assertTrue(report["validations"][0]["contract_ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 10 · P10-01…P10-06 契约用例。

钉住（全部是**等值断言**，不放宽任何既有契约）：
  1. P10 的六个口径版本号与 §10/§11 的硬规则枚举逐字入契约；
  2. 时效闸门的十一个理由码、复验的七个出口、撤销的六个理由码都是闭集合，
     且模块里实际用到的取值**全部落在枚举内**（防止"随便编一个理由码"）；
  3. 矛盾理由码 = **Phase 06 的同一张表**（单一真源，不另立一套质量判断）；
  4. `MEMORY_REVALIDATION_TRANSITIONS` 里没有任何 `REVOKED → *`（撤销是终态，不可复活）；
  5. 新增的七个 schema 都能被 `validate()` 认出来，且非法取值会被拦；
  6. MASTER_RULES 11 的机器形态：记忆正文不是证据（`verified_scope` 恒为 `evidence_refs`）；
  7. 九条记忆关系**没有被新增**（§1.4 的取值域不因 Phase 10 而漂移）。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_graph_contracts as contracts  # noqa: E402
import qa_memory_revalidation as mr  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402

import qa_phase10_fixtures as fx  # noqa: E402


class ContractEnumTests(unittest.TestCase):
    def test_version_constants_are_pinned(self):
        self.assertEqual(contracts.MEMORY_FRESHNESS_GATE_VERSION, "qa-memory-freshness-gate-v1")
        self.assertEqual(contracts.MEMORY_SOURCE_VERSION_VERSION, "qa-memory-source-version-v1")
        self.assertEqual(contracts.MEMORY_REVALIDATION_VERSION, "qa-memory-revalidation-v1")
        self.assertEqual(contracts.MEMORY_CONTRADICTION_VERSION, "qa-memory-contradiction-v1")
        self.assertEqual(contracts.MEMORY_SUPERSESSION_VERSION, "qa-memory-supersession-v1")
        self.assertEqual(contracts.MEMORY_REVOKE_VERSION, "qa-memory-revoke-v1")

    def test_gate_decisions_and_reasons_are_closed_sets(self):
        self.assertEqual(contracts.MEMORY_FRESHNESS_DECISIONS, ("ALLOW", "REVALIDATE", "BLOCK"))
        self.assertEqual(len(set(contracts.MEMORY_FRESHNESS_REASONS)),
                         len(contracts.MEMORY_FRESHNESS_REASONS))
        for code in ("NOT_ACTIVE", "VALID_UNTIL_PASSED", "SOURCE_VERSION_CHANGED",
                     "HIGH_STAKES_DEFAULT", "FRESHNESS_REQUIRED", "FRESH_AND_VERIFIED"):
            self.assertIn(code, contracts.MEMORY_FRESHNESS_REASONS)

    def test_revalidation_outcomes_match_the_implemented_writer(self):
        self.assertEqual(tuple(mr.VALIDATION_WRITE_OUTCOMES),
                         tuple(contracts.MEMORY_REVALIDATION_OUTCOMES))

    def test_contradiction_codes_are_phase06_codes(self):
        self.assertEqual(tuple(contracts.MEMORY_CONTRADICTION_RESOLUTION_CODES),
                         tuple(contracts.CONTRADICTION_RESOLUTION_CODES))
        self.assertEqual(contracts.MEMORY_CONTRADICTION_OUTCOMES, ("resolved", "unresolved"))

    def test_revoke_reasons_are_closed(self):
        self.assertEqual(len(contracts.MEMORY_REVOKE_REASONS), 6)
        self.assertEqual(mr._REVOKE_SELECTORS,
                         ("source_fingerprint", "entity_key", "session_id", "memory_ids"))

    def test_no_new_memory_relation_names(self):
        # §1.4 的九个关系取值域不因 Phase 10 漂移（SUPERSEDED_BY 用字段 + SUPERSEDES 边表达）
        self.assertEqual(contracts.MEMORY_RELATIONS,
                         ("ABOUT", "DERIVED_FROM", "VALIDATED_BY", "SUPERSEDES", "CONTRADICTS",
                          "EXPIRED_BY", "HELPED_RESOLVE", "FAILED_ON", "APPLIES_TO"))
        self.assertIn("SUPERSEDES", contracts.MEMORY_RELATIONS)
        self.assertNotIn("SUPERSEDED_BY", contracts.MEMORY_RELATIONS)


class TransitionTableTests(unittest.TestCase):
    def test_revoked_is_terminal(self):
        # 允许 ("REVOKED","REVOKED") 这个自环（NO_CHANGE），但不许任何真出边
        for (before, after) in contracts.MEMORY_REVALIDATION_TRANSITIONS:
            if before == "REVOKED":
                self.assertEqual(after, "REVOKED", "REVOKED 是终态，不许有任何真迁移")
        self.assertEqual(contracts.MEMORY_REVALIDATION_TRANSITIONS[("REVOKED", "REVOKED")],
                         "NO_CHANGE")
        self.assertIn(("EXPIRED", "REVOKED"), contracts.MEMORY_REVALIDATION_TRANSITIONS)

    def test_revalidation_can_resurrect_stale_and_expired_only(self):
        self.assertIn(("STALE", "ACTIVE"), contracts.MEMORY_REVALIDATION_TRANSITIONS)
        self.assertIn(("EXPIRED", "ACTIVE"), contracts.MEMORY_REVALIDATION_TRANSITIONS)
        self.assertNotIn(("REVOKED", "ACTIVE"), contracts.MEMORY_REVALIDATION_TRANSITIONS)
        self.assertNotIn(("CONTRADICTED", "ACTIVE"), contracts.MEMORY_REVALIDATION_TRANSITIONS)

    def test_supersede_is_reachable_only_from_active_or_stale(self):
        supersede = {row for row in contracts.MEMORY_REVALIDATION_TRANSITIONS
                     if row[1] == "SUPERSEDED" and row[0] != row[1]}
        self.assertEqual(supersede, {("ACTIVE", "SUPERSEDED"), ("STALE", "SUPERSEDED")})

    def test_transition_reasons_are_non_empty_strings(self):
        for key, value in contracts.MEMORY_REVALIDATION_TRANSITIONS.items():
            self.assertIsInstance(value, str)
            self.assertTrue(value.strip(), "迁移理由码不许为空：%s" % (key,))

    def test_phase09_lifecycle_table_is_untouched(self):
        # P09 的表是冻结的；P10 另立一张（两张表分开维护是契约的一部分）
        self.assertEqual(contracts.MEMORY_LIFECYCLE_TRANSITIONS[("ACTIVE", "STALE")],
                         "DECAY_BELOW_STALE_FLOOR")
        self.assertNotIn(("ACTIVE", "SUPERSEDED"), contracts.MEMORY_LIFECYCLE_TRANSITIONS)


class SchemaRegistrationTests(unittest.TestCase):
    def test_all_phase10_schemas_are_registered(self):
        cases = {
            "memory_freshness_decision": {
                "gate_version": contracts.MEMORY_FRESHNESS_GATE_VERSION, "memory_id": "MEM1",
                "decision": "ALLOW", "reason": "FRESH_AND_VERIFIED"},
            "memory_source_version": {
                "source_version_check": contracts.MEMORY_SOURCE_VERSION_VERSION,
                "memory_id": "MEM1", "changed": False, "reasons": ["SOURCE_VERSION_STABLE"]},
            "memory_revalidation": {
                "validation_id": "MVAL1", "revalidation_version": contracts.MEMORY_REVALIDATION_VERSION,
                "memory_id": "MEM1", "outcome": "REVALIDATED",
                "reason": "EVIDENCE_STILL_SUPPORTS", "verified_evidence": True,
                "verified_scope": "evidence_refs"},
            "memory_contradiction": {
                "contradiction_id": "MCT1",
                "contradiction_version": contracts.MEMORY_CONTRADICTION_VERSION,
                "kind": "memory_memory", "left_memory_id": "MEM1", "resolution": "resolved",
                "reason_code": "NEWER_VERSION_PRECEDES", "status_action": "SUPERSEDE"},
            "memory_supersession": {
                "supersession_id": "MSUP1",
                "supersession_version": contracts.MEMORY_SUPERSESSION_VERSION,
                "memory_id": "MEM1", "superseded_by": "MEM2", "relation": "SUPERSEDES",
                "reason": "NEWER_VERSION_PRECEDES"},
            "memory_revoke_receipt": {
                "revoke_version": contracts.MEMORY_REVOKE_VERSION,
                "reason": "SOURCE_CONTAMINATED", "checked": 1, "revoked": []},
            "memory_revalidation_report": {
                "revalidation_version": contracts.MEMORY_REVALIDATION_VERSION, "checked": 0,
                "gate_decisions": {}, "outcomes": {}},
        }
        for name, payload in cases.items():
            ok, note = validate(name, payload)
            self.assertTrue(ok, "%s: %s" % (name, note))

    def test_illegal_enum_values_are_rejected(self):
        ok, note = validate("memory_revalidation", {
            "validation_id": "x", "revalidation_version": "v", "memory_id": "m",
            "outcome": "MADE_UP", "reason": "EVIDENCE_STILL_SUPPORTS",
            "verified_evidence": True, "verified_scope": "evidence_refs"})
        self.assertFalse(ok)
        self.assertIn("outcome", note)
        ok, note = validate("memory_freshness_decision", {
            "gate_version": "v", "memory_id": "m", "decision": "MAYBE", "reason": "NOT_ACTIVE"})
        self.assertFalse(ok)

    def test_missing_required_fields_are_rejected(self):
        for name, payload in (("memory_freshness_decision", {"memory_id": "m"}),
                              ("memory_revalidation", {"memory_id": "m"}),
                              ("memory_contradiction", {"kind": "memory_memory"}),
                              ("memory_revoke_receipt", {"reason": "MANUAL_REVOKE"})):
            ok, note = validate(name, payload)
            self.assertFalse(ok, "%s 缺必需字段却通过了" % name)

    def test_describe_mentions_phase10_sets(self):
        text = contracts.describe()
        for keyword in ("记忆类型", "时效闸门出口", "复验出口", "记忆矛盾种类", "撤销理由"):
            self.assertIn(keyword, text)


class MasterRulesInvariantTests(unittest.TestCase):
    def test_verified_scope_is_evidence_refs_only(self):
        """MASTER_RULES 11：复验提升的是**证据引用**，记忆正文永远不是证据。"""
        with fx.temp_store() as (database, store):
            self.assertEqual(database.backend, "sqlite")
            item = fx.memory_with_evidence(store, freshness="VERY_SHORT")
            receipt = mr.revalidate_item(
                store, fx.load(store, item["memory_id"]),
                current_evidence=[fx.verified_evidence("article:1")], run_id="run-1", now=fx.NOW)
            self.assertEqual(receipt["verified_scope"], "evidence_refs")
            self.assertTrue(receipt["verified_evidence"])
            self.assertIn("记忆正文", mr.__doc__)

    def test_injected_judge_cannot_promote_without_phase03_supported(self):
        """注入的判定器**只能否决提升**：没有 Phase 03 的 SUPPORTED 就不许 verified_evidence。"""
        def always_promote(payload):      # 恶意/坏掉的判定器：一律说"提升"
            return {"outcome": "REVALIDATED"}

        mr.register_revalidation_judge("test_always_promote", always_promote)
        try:
            with fx.temp_store() as (database, store):
                item = fx.memory_with_evidence(store, freshness="VERY_SHORT")
                # 候选证据与结论无关 → Phase 03 判不出 SUPPORTED → 出口不可能是 REVALIDATED
                irrelevant = fx.verified_evidence(
                    "article:99", claim_text="某论坛讨论球赛与天气",
                    text="论坛里随便聊了聊天气和球赛，与家族办公室税务安排无关。")
                receipt = mr.revalidate_item(
                    store, fx.load(store, item["memory_id"]), current_evidence=[irrelevant],
                    run_id="run-1", now=fx.NOW, judge=always_promote)
                self.assertNotEqual(receipt["outcome"], "REVALIDATED")
                self.assertFalse(receipt["verified_evidence"])
        finally:
            mr._REVALIDATION_JUDGES.pop("test_always_promote", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)

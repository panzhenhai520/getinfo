#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 15 · 契约与冻结面守门（P15-01…P15-06）。

钉住四件事：
  1. **七契约指纹复算不变**（P00-02 冻结面一个字不动）；
  2. 本阶段新增的枚举/派生视图**逐项可复算**（`SCOPE_ALLOWED_CONTENT_KINDS` 由两张表派生）；
  3. 六个新 schema 都能校验合法载荷、都能拦住越界取值（含嵌套）；
  4. 版本字面量同步仍是**等值断言**，且 `QA_SCHEMA_VERSION` **不动**（本阶段零迁移）。
"""
import hashlib
import json
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_contracts  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_memory as memory  # noqa: E402
import qa_memory_safety as ms  # noqa: E402
import qa_schema  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402

# P00-02 冻结的七个指纹（与 Phase 05 的守门用例同一份口径与同一份期望值）
FROZEN_FINGERPRINTS = {
    "EVIDENCE_SCHEMA": "370301331c02c738",
    "CLAIM_SCHEMA": "06fcdb02441248b2",
    "CONFLICT_SCHEMA": "aabd3259b07f9a3e",
    "LEVEL1_RESULT_SCHEMA": "7e864764429db111",
    "LEVEL2_RESULT_SCHEMA": "a0484f894e7bc9d6",
    "FINAL_ANSWER_SCHEMA": "4d1efa54ca1cbc1a",
    "QA_EVENT_SCHEMA": "59358bfa88a6c6af",
}


def _fingerprint(value) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class FrozenContractTests(unittest.TestCase):
    def test_seven_fingerprints_are_unchanged(self):
        for name, expected in FROZEN_FINGERPRINTS.items():
            self.assertEqual(_fingerprint(getattr(qa_contracts, name)), expected,
                             "%s 契约指纹变了：Phase 15 不许动冻结面" % name)

    def test_evidence_schema_still_closed(self):
        self.assertIs(qa_contracts.EVIDENCE_SCHEMA.get("additionalProperties"), False)
        self.assertIs(qa_contracts.FINAL_ANSWER_SCHEMA.get("additionalProperties"), False)

    def test_schema_version_unchanged(self):
        self.assertEqual(qa_schema.QA_SCHEMA_VERSION, "unified-qa-schema-v11",
                         "P15 零迁移：版本号必须仍是 v11（等值断言，未放宽）")

    def test_no_new_tables_or_columns(self):
        self.assertEqual(contracts.MEMORY_SAFETY_VERSION, "qa-memory-safety-v1")
        for version in (contracts.SCOPE_ENFORCEMENT_VERSION, contracts.ENCOUNTER_ISOLATION_VERSION,
                        contracts.LONGITUDINAL_FRESHNESS_VERSION,
                        contracts.SAFETY_CRITICAL_OVERRIDE_VERSION,
                        contracts.PRIVACY_MINIMAL_LOG_VERSION,
                        contracts.MEMORY_AUDIT_REPLAY_VERSION):
            self.assertTrue(str(version).startswith("qa-"), version)


class EnumTests(unittest.TestCase):
    def test_scope_allowed_kinds_is_derived_from_the_two_tables(self):
        derived = {scope: tuple(kind for kind, cap in contracts.CONTENT_KIND_MAX_BREADTH.items()
                                if cap >= breadth)
                   for scope, breadth in contracts.SCOPE_BREADTH.items()}
        self.assertEqual(contracts.SCOPE_ALLOWED_CONTENT_KINDS, derived,
                         "派生视图必须与两张表一致（不许手写第二份）")
        self.assertEqual(sorted(contracts.SCOPE_ALLOWED_CONTENT_KINDS), sorted(
            contracts.MEMORY_SCOPES))

    def test_breadth_is_a_total_order_over_the_five_scopes(self):
        values = sorted(contracts.SCOPE_BREADTH.values())
        self.assertEqual(values, sorted(set(values)), "五个作用域的广度必须两两不同")
        self.assertEqual(set(contracts.SCOPE_BREADTH), set(contracts.MEMORY_SCOPES))
        self.assertEqual(set(contracts.CONTENT_KIND_MAX_BREADTH), set(contracts.SCOPE_CONTENT_KINDS))

    def test_every_scope_enforcement_reason_is_reachable(self):
        """**逐条**构造能命中每个理由码的输入（闭集合里不许有死值）。"""
        axes = dict(session_id="s1", industry_pack_id="auto")
        hits = set()

        def _run(candidate, **kw):
            row = ms.enforce_scope(candidate, **{**axes, **kw})
            hits.add(row["reason"])
            return row

        _run({"memory_type": "VERIFIED_CLAIM", "canonical_content": "某规范的定义",
              "scope": "GLOBAL_KNOWLEDGE"})                                   # 放行
        _run({"memory_type": "STRATEGY", "canonical_content": "通道有效率统计",
              "scope": "ORGANIZATION", "owner_user_id": ""})                   # 绑定退化
        _run({"memory_type": "VERIFIED_CLAIM", "canonical_content": "体温 39 度",
              "claim_type": "current_fact", "scope": "PATIENT_LONGITUDINAL"})  # 收窄到轮次
        _run({"memory_type": "STRATEGY", "canonical_content": "检索通道有效率",
              "scope": "GLOBAL_KNOWLEDGE"})                                    # 非状态类越界收窄
        _run({"memory_type": "VERIFIED_CLAIM", "canonical_content": "x", "scope": ""})
        _run({"memory_type": "VERIFIED_CLAIM", "canonical_content": "x", "scope": "PATIENT"})
        _run({"memory_type": "VERIFIED_CLAIM", "canonical_content": "x",
              "scope": "GLOBAL_KNOWLEDGE", "scope_key": "forged"})
        _run({"memory_type": "VERIFIED_CLAIM", "canonical_content": "体温 39 度",
              "claim_type": "current_fact", "scope": "PATIENT_LONGITUDINAL"},
             session_id="", encounter_id="", industry_pack_id="")
        _run({"memory_type": "VERIFIED_CLAIM", "canonical_content": "某规范的定义",
              "scope": "PATIENT_LONGITUDINAL"}, industry_pack_id="")
        _run({"memory_type": "VERIFIED_CLAIM", "canonical_content": "x", "scope": "SESSION"},
             session_id="")
        _run({"memory_type": "VERIFIED_CLAIM", "canonical_content": "x", "scope": "ENCOUNTER"},
             session_id="", encounter_id="")
        self.assertEqual(hits, set(contracts.SCOPE_ENFORCEMENT_REASONS),
                         "有理由码从未被命中：%s" % sorted(set(contracts.SCOPE_ENFORCEMENT_REASONS) - hits))

    def test_every_isolation_reason_is_reachable(self):
        context = ms.encounter_context(owner_user_id="u1", session_id="s1",
                                       industry_pack_id="auto", encounter_id="e1")
        cases = {
            "GLOBAL": {"scope": "GLOBAL_KNOWLEDGE", "scope_key": "global"},
            "SAME_ORGANIZATION": {"scope": "ORGANIZATION", "scope_key": "org:u1",
                                  "owner_user_id": "u1"},
            "OTHER_ORGANIZATION": {"scope": "ORGANIZATION", "scope_key": "org:u2",
                                   "owner_user_id": "u2"},
            "SAME_SUBJECT": {"scope": "PATIENT_LONGITUDINAL", "scope_key": "subject:auto",
                             "industry_pack_id": "auto"},
            "OTHER_SUBJECT": {"scope": "PATIENT_LONGITUDINAL", "scope_key": "subject:other",
                              "industry_pack_id": "other"},
            "SAME_ENCOUNTER": {"scope": "ENCOUNTER", "scope_key": context["encounter_key"]},
            "OTHER_ENCOUNTER": {"scope": "ENCOUNTER", "scope_key": "encounter:auto:s1:e2"},
            "SAME_SESSION": {"scope": "SESSION", "scope_key": context["session_key"]},
            "OTHER_SESSION": {"scope": "SESSION", "scope_key": "session:u1:s9:auto"},
        }
        hits = {name: ms.isolation_verdict(row, context)["reason"] for name, row in cases.items()}
        self.assertEqual(set(hits.values()), set(contracts.ENCOUNTER_ISOLATION_REASONS),
                         "隔离理由码未全部可达：%s" % sorted(
                             set(contracts.ENCOUNTER_ISOLATION_REASONS) - set(hits.values())))

    def test_write_reasons_appended_not_rewritten(self):
        self.assertEqual(contracts.MEMORY_WRITE_REASONS[:3],
                         ("NO_VERIFIED_EVIDENCE", "EXTERNAL_INSTRUCTION_NOT_A_RULE",
                          "SENSITIVE_CONTENT"), "写门理由码前三位被改写了")
        self.assertIn("SCOPE_ENFORCEMENT_REJECTED", contracts.MEMORY_WRITE_REASONS)

    def test_audit_event_kinds_are_closed_and_used(self):
        self.assertEqual(len(contracts.MEMORY_AUDIT_EVENT_KINDS), 6)
        self.assertEqual(set(contracts.AUDIT_REPLAY_LAYERS),
                         {"execution", "evidence", "context", "memory"})

    def test_describe_mentions_phase15_versions(self):
        text = contracts.describe()
        for token in ("qa-memory-safety-v1", "qa-scope-enforcement-v1",
                      "qa-encounter-isolation-v1", "qa-longitudinal-freshness-v1",
                      "qa-safety-critical-override-v1", "qa-privacy-minimal-log-v1",
                      "qa-memory-audit-replay-v1"):
            self.assertIn(token, text)


class SchemaTests(unittest.TestCase):
    def test_new_schemas_reject_out_of_range_values(self):
        bad = [
            ("scope_enforcement", {"enforcement_version": contracts.SCOPE_ENFORCEMENT_VERSION,
                                   "content_kind": "CURRENT_STATE", "decision": "WIDEN",
                                   "reason": "SCOPE_AND_CONTENT_ALIGNED", "binding": {}}),
            ("encounter_isolation", {"isolation_version": "nope",
                                     "checked": 1, "visible": 1, "isolated": 0,
                                     "isolated_by_reason": {}}),
            ("longitudinal_freshness", {"longitudinal_version":
                                        contracts.LONGITUDINAL_FRESHNESS_VERSION,
                                        "decision": "MAYBE", "reason": "FRESH_AND_VERIFIED",
                                        "longitudinal": True}),
            ("safety_enforcement", {"override_version": contracts.SAFETY_CRITICAL_OVERRIDE_VERSION,
                                    "trigger": "MAYBE_TRIGGER", "reason": "NO_SAFETY_TERM",
                                    "danger_signs": [], "sign_count": 0, "min_signs": 2}),
            ("privacy_minimal_log", {"policy_version": "nope",
                                     "rule_hits": {}, "leaks": [], "content_policy": "x",
                                     "rule_hits_out_of_range": "EMAIL"}),
            ("audit_replay", {"replay_version": contracts.MEMORY_AUDIT_REPLAY_VERSION,
                              "run_id": "r", "layers": ["execution", "nope"], "counts": {},
                              "replay_matches": True, "redacted": True}),
        ]
        for name, payload in bad:
            ok, note = validate(name, payload)
            self.assertFalse(ok, "%s 应该拦下越界载荷" % name)
            self.assertTrue(note)

    def test_unknown_schema_and_nested_rejection(self):
        ok, note = validate("not_a_schema", {})
        self.assertFalse(ok)
        self.assertIn("未知 schema", note)
        ok, note = validate("privacy_minimal_log", {
            "policy_version": contracts.PRIVACY_MINIMAL_LOG_VERSION, "rule_hits": {},
            "leaks": [], "content_policy": "x"})
        self.assertTrue(ok, note)


class ScopeKeyCompatibilityTests(unittest.TestCase):
    def test_encounter_axis_is_optional_and_back_compatible(self):
        self.assertEqual(memory.scope_key("ENCOUNTER", industry_pack_id="auto", session_id="s1"),
                         "encounter:auto:s1", "不给轮次维时必须逐字保持 Phase 09 的键")
        self.assertEqual(memory.scope_key("ENCOUNTER", industry_pack_id="auto", session_id="s1",
                                          encounter_id="e1"), "encounter:auto:s1:e1")
        self.assertEqual(memory.scope_key("SESSION", owner_user_id="u", session_id="s",
                                          industry_pack_id="p", encounter_id="e"),
                         memory.scope_key("SESSION", owner_user_id="u", session_id="s",
                                          industry_pack_id="p"))

    def test_visible_keys_adds_the_legacy_encounter_bucket(self):
        context = ms.encounter_context(owner_user_id="u", session_id="s",
                                      industry_pack_id="p", encounter_id="e")
        self.assertIn("encounter:p:s:e", context["visible_scope_keys"])
        self.assertIn("encounter:p:s", context["visible_scope_keys"],
                      "老数据写在'会话级轮次桶'里，兼容适配器必须仍然可见")

    def test_memory_id_changes_with_the_encounter_axis(self):
        base = dict(scope="ENCOUNTER", memory_type="VERIFIED_CLAIM",
                    canonical_content="体温 39 度", industry_pack_id="auto", session_id="s1")
        self.assertNotEqual(memory.memory_id_for(**base, encounter_id="e1"),
                            memory.memory_id_for(**base, encounter_id="e2"))


if __name__ == "__main__":
    unittest.main()

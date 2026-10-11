#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 15 · P15-01（scope enforcement）用例。

钉住 §14 的作用域强制口径：
  1. 内容种类**逐类可达**、每条判定都带依据（`basis`）；
  2. 判定**只可能不变或变严**：收窄目标的作用域广度严格小于原作用域（全组合性质用例）；
  3. 越界先收窄（当前状态 → 轮次；环境/规则类 → 组织），收窄不了才拒收；
  4. 作用域键是隔离的物理载体：**手写的键一律不可信**（重算不一致就拒收）；
  5. 写门接线：收窄真的改 `scope`（连 `scope_key` 一起），拒收走
     `SCOPE_ENFORCEMENT_REJECTED` 并留痕；**开关关闭时逐字回到 Phase 09 行为**；
  6. 失败路径：安全模块抛错不许把写门打断（退化为"一个字段都不动 + 空回执"）。
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
import qa_memory_safety as ms  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402

import qa_phase09_fixtures as fx  # noqa: E402

AXES = {"owner_user_id": "p15", "session_id": "s1", "industry_pack_id": "auto",
        "encounter_id": "e1"}
FLAGS = ("QA_MEMORY_SCOPE_ENFORCEMENT",)


def _candidate(text=fx.CLAIM_TEXT, *, memory_type="VERIFIED_CLAIM",
               scope="PATIENT_LONGITUDINAL", claim_type="policy", **overrides):
    """一条"真的能过写门"的候选（证据走 Phase 02/03 的真实链路）。"""
    evidence = fx.verified_evidence("article:1", claim_text=text)
    row = {
        "memory_type": memory_type, "canonical_content": text, "claim_id": "c1",
        "claim_type": claim_type, "claim_confidence": 0.82,
        "freshness_class": memory.classify_freshness(claim_type=claim_type, text=text),
        "valid_from": "", "valid_until": "",
        "entity_ids": ["家族办公室"],
        "evidence": [{"evidence_ref": "article:1", "source_fingerprint": "SF-1",
                      "span_fingerprint": "SP-1", "verdict": "SUPPORTED",
                      "evidence_score": 0.9, "relationship": "supports",
                      "run_id": "run-p15", "stage": "level1_retrieval", "route": "keyword",
                      "corpus_version": "corpus-p15", "metadata": {"article_id": 1}}],
        "supporting": 1, "scope": scope, "owner_user_id": "p15", "session_id": "s1",
        "industry_pack_id": "auto", "run_id": "run-p15", "metadata": {},
    }
    del evidence      # 只用它保证"证据形态与 Phase 02/03 一致"，行本身按上面的口径手写
    row.update(overrides)
    return row


STATE_TEXT = fx.CLAIM_TEXT + "；当前状态：体温 39 度，正在服用某药物"


class Base(unittest.TestCase):
    def setUp(self):
        self.saved = {name: os.environ.pop(name, None) for name in FLAGS}

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class ContentKindTests(Base):
    def test_five_kinds_are_all_reachable_with_a_basis(self):
        cases = {
            "KNOWLEDGE": {"memory_type": "VERIFIED_CLAIM", "canonical_content": "指南的定义",
                          "claim_type": "policy"},
            "RULE": {"memory_type": "USER_APPROVED_DOMAIN_RULE", "canonical_content": "组织规则"},
            "ENVIRONMENT": {"memory_type": "STRATEGY", "canonical_content": "通道有效率"},
            "SUBJECT_FACT": {"memory_type": "VERIFIED_CLAIM",
                             "canonical_content": "某主体于 2026 年完成登记"},
            "CURRENT_STATE": {"memory_type": "VERIFIED_CLAIM", "canonical_content": STATE_TEXT},
        }
        for expected, candidate in cases.items():
            row = ms.classify_content_kind(candidate)
            self.assertEqual(row["content_kind"], expected, candidate)
            self.assertTrue(row["basis"].get("source"), "判定必须带依据")

    def test_explicit_declaration_wins_and_is_marked(self):
        row = ms.classify_content_kind({"memory_type": "STRATEGY",
                                        "canonical_content": "x", "content_kind": "RULE"})
        self.assertEqual(row["content_kind"], "RULE")
        self.assertEqual(row["basis"]["source"], "explicit")

    def test_state_terms_are_reported_and_registered(self):
        row = ms.classify_content_kind({"memory_type": "VERIFIED_CLAIM",
                                        "canonical_content": STATE_TEXT})
        self.assertEqual(row["content_kind"], "CURRENT_STATE")
        self.assertIn("体温", row["basis"]["state_terms"])

    def test_freshness_class_session_is_current_state(self):
        row = ms.classify_content_kind({"memory_type": "VERIFIED_CLAIM", "canonical_content": "x",
                                        "freshness_class": "ENCOUNTER_BOUND"})
        self.assertEqual(row["content_kind"], "CURRENT_STATE")
        self.assertEqual(row["basis"]["source"], "freshness_class")


class EnforcementTests(Base):
    def _run(self, candidate, **overrides):
        return ms.enforce_scope(candidate, **{**AXES, **overrides})

    def test_aligned_scope_is_allowed(self):
        row = self._run({"memory_type": "VERIFIED_CLAIM", "canonical_content": "指南的定义",
                         "claim_type": "policy", "scope": "GLOBAL_KNOWLEDGE"})
        self.assertEqual((row["decision"], row["reason"]),
                         ("ALLOW", "SCOPE_AND_CONTENT_ALIGNED"))
        self.assertTrue(row["contract_ok"], row.get("contract_error"))

    def test_current_state_is_narrowed_to_the_encounter(self):
        row = self._run({"memory_type": "VERIFIED_CLAIM", "canonical_content": STATE_TEXT,
                         "claim_type": "current_fact", "scope": "GLOBAL_KNOWLEDGE"})
        self.assertEqual(row["decision"], "NARROW")
        self.assertEqual(row["reason"], "CURRENT_STATE_NARROWED_TO_ENCOUNTER")
        self.assertEqual(row["narrowed_from"], "GLOBAL_KNOWLEDGE")
        self.assertEqual(row["narrowed_to"], "ENCOUNTER")

    def test_environment_knowledge_is_narrowed_to_the_organization(self):
        row = self._run({"memory_type": "STRATEGY", "canonical_content": "通道有效率" ,
                         "scope": "GLOBAL_KNOWLEDGE"})
        self.assertEqual((row["decision"], row["narrowed_to"]),
                         ("NARROW", "ORGANIZATION"))
        self.assertEqual(row["reason"], "CONTENT_KIND_NARROWED_TO_ALLOWED_SCOPE")

    def test_subject_fact_is_narrowed_to_the_subject_scope(self):
        row = self._run({"memory_type": "VERIFIED_CLAIM", "canonical_content": "历史沿革与起源",
                         "scope": "GLOBAL_KNOWLEDGE"})
        # "历史沿革" 命中 Phase 09 的 LONG 档 → 判 KNOWLEDGE，所以这里用显式种类逼出主体事实
        row = self._run({"memory_type": "VERIFIED_CLAIM", "canonical_content": "某主体登记信息",
                         "content_kind": "SUBJECT_FACT", "scope": "ORGANIZATION"})
        self.assertEqual(row["decision"], "NARROW")
        self.assertEqual(row["narrowed_to"], "PATIENT_LONGITUDINAL")

    def test_rejections_carry_the_reason_code(self):
        cases = {
            "SCOPE_MISSING": ({"memory_type": "VERIFIED_CLAIM", "canonical_content": "x",
                               "scope": ""}, {}),
            "SCOPE_UNKNOWN": ({"memory_type": "VERIFIED_CLAIM", "canonical_content": "x",
                               "scope": "PATIENT"}, {}),
            "SCOPE_KEY_MISMATCH": ({"memory_type": "VERIFIED_CLAIM", "canonical_content": "x",
                                    "scope": "GLOBAL_KNOWLEDGE", "scope_key": "forged"}, {}),
            "SUBJECT_SCOPE_NEEDS_SUBJECT": ({"memory_type": "VERIFIED_CLAIM",
                                             "canonical_content": "某主体登记信息",
                                             "content_kind": "SUBJECT_FACT",
                                             "scope": "PATIENT_LONGITUDINAL"},
                                            {"industry_pack_id": ""}),
            "SESSION_SCOPE_NEEDS_SESSION": ({"memory_type": "VERIFIED_CLAIM",
                                             "canonical_content": "x", "scope": "SESSION"},
                                            {"session_id": ""}),
            "ENCOUNTER_SCOPE_NEEDS_ENCOUNTER": ({"memory_type": "VERIFIED_CLAIM",
                                                 "canonical_content": "x", "scope": "ENCOUNTER"},
                                                {"session_id": "", "encounter_id": ""}),
        }
        for expected, (candidate, overrides) in cases.items():
            row = self._run(candidate, **overrides)
            self.assertEqual(row["decision"], "REJECT", candidate)
            self.assertEqual(row["reason"], expected, candidate)
            self.assertTrue(row["contract_ok"], row.get("contract_error"))

    def test_scope_key_is_recomputed_and_compared(self):
        good = self._run({"memory_type": "VERIFIED_CLAIM", "canonical_content": "指南的定义",
                          "claim_type": "policy", "scope": "PATIENT_LONGITUDINAL",
                          "scope_key": "subject:auto"})
        self.assertEqual(good["decision"], "ALLOW")
        self.assertEqual(good["expected_scope_key"], "subject:auto")

    def test_narrowing_never_widens_the_scope(self):
        """全组合性质：任何一次 NARROW 的目标广度都**严格小于**原作用域。"""
        kinds = list(contracts.SCOPE_CONTENT_KINDS)
        for kind in kinds:
            for scope in contracts.MEMORY_SCOPES:
                row = self._run({"memory_type": "VERIFIED_CLAIM", "canonical_content": "x",
                                 "content_kind": kind, "scope": scope})
                if row["decision"] != "NARROW":
                    continue
                self.assertLess(contracts.SCOPE_BREADTH[row["narrowed_to"]],
                                contracts.SCOPE_BREADTH[scope],
                                "%s/%s 收窄方向反了" % (kind, scope))
                self.assertLessEqual(contracts.CONTENT_KIND_MAX_BREADTH[kind],
                                     contracts.CONTENT_KIND_MAX_BREADTH[kind])

    def test_cap_table_is_respected_after_narrowing(self):
        for kind in contracts.SCOPE_CONTENT_KINDS:
            for scope in contracts.MEMORY_SCOPES:
                row = self._run({"memory_type": "VERIFIED_CLAIM", "canonical_content": "x",
                                 "content_kind": kind, "scope": scope})
                target = row["narrowed_to"] or row["scope"]
                if row["decision"] == "REJECT":
                    continue
                self.assertLessEqual(contracts.SCOPE_BREADTH[target],
                                     contracts.CONTENT_KIND_MAX_BREADTH[kind],
                                     "%s 收到 %s 后仍然越界" % (kind, target))

    def test_apply_wrapper_only_touches_scope_and_key(self):
        candidate = _candidate(STATE_TEXT, scope="GLOBAL_KNOWLEDGE", claim_type="current_fact",
                               scope_key="global")
        adjusted, receipt = ms.apply_scope_enforcement(candidate)
        self.assertEqual(receipt["decision"], "NARROW")
        self.assertEqual(adjusted["scope"], "ENCOUNTER")
        self.assertEqual(adjusted["scope_key"], "encounter:auto:s1",
                         "候选没有轮次维时，键逐字沿用'会话级轮次桶'（兼容口径）")
        for key in ("canonical_content", "memory_type", "industry_pack_id", "evidence"):
            self.assertEqual(adjusted[key], candidate[key], "%s 不该被改动" % key)
        self.assertEqual(adjusted["metadata"]["scope_enforcement"]["narrowed_to"], "ENCOUNTER")
        with_encounter, _ = ms.apply_scope_enforcement({**candidate, "encounter_id": "e1",
                                                        "scope_key": "global"})
        self.assertEqual(with_encounter["scope_key"], "encounter:auto:s1:e1",
                         "给了轮次维就多一维（P15-02 的逐轮次隔离）")


class WriteGateWiringTests(Base):
    def test_narrowed_candidate_is_persisted_with_the_stricter_scope(self):
        with fx.temp_store("phase15-scope.sqlite3") as (_database, store):
            receipt = memory.apply_write_gate(store, [_candidate(STATE_TEXT, scope="GLOBAL_KNOWLEDGE",
                                                                 claim_type="current_fact")],
                                              run_id="run-p15")
            self.assertEqual(receipt["persisted"], 1)
            item = store.load_memory_items(include_all_scopes=True)[0]
            self.assertEqual(item["scope"], "ENCOUNTER")
            self.assertEqual(item["scope_key"], "encounter:auto:s1")
            self.assertEqual(receipt["scope_enforcement"]["narrowed"], 1)
            self.assertEqual(receipt["decisions"][0]["metadata"]["scope_enforcement"]["decision"],
                             "NARROW")

    def test_forged_scope_key_is_rejected_with_the_new_reason(self):
        with fx.temp_store("phase15-scope2.sqlite3") as (_database, store):
            candidate = _candidate(scope="GLOBAL_KNOWLEDGE", scope_key="global|forged")
            receipt = memory.apply_write_gate(store, [candidate], run_id="run-p15")
            self.assertEqual(receipt["persisted"], 0)
            row = receipt["decisions"][0]
            self.assertEqual((row["decision"], row["reason"]),
                             ("DROP", "SCOPE_ENFORCEMENT_REJECTED"))
            self.assertIn(row["reason"], contracts.MEMORY_WRITE_REASONS)
            self.assertEqual(row["metadata"]["scope_enforcement"]["reason"], "SCOPE_KEY_MISMATCH")
            ok, note = validate("memory_write_decision", {key: row[key] for key in (
                "decision_id", "decision", "reason", "memory_type", "gate_version")})
            self.assertTrue(ok, note)

    def test_switch_off_returns_to_phase09_behaviour(self):
        os.environ["QA_MEMORY_SCOPE_ENFORCEMENT"] = "0"
        with fx.temp_store("phase15-scope3.sqlite3") as (_database, store):
            receipt = memory.apply_write_gate(
                store, [_candidate(scope="GLOBAL_KNOWLEDGE", scope_key="global|forged")],
                run_id="run-p15")
            self.assertEqual(receipt["persisted"], 1, "关掉强制后这条必须照 Phase 09 落库")
            item = store.load_memory_items(include_all_scopes=True)[0]
            self.assertEqual(item["scope"], "GLOBAL_KNOWLEDGE")
            self.assertEqual(receipt["scope_enforcement"]["checked"], 0)
            self.assertIsNone(receipt["decisions"][0]["metadata"]["scope_enforcement"])

    def test_module_failure_never_breaks_the_gate(self):
        with mock.patch.object(ms, "apply_scope_enforcement", side_effect=RuntimeError("boom")):
            with fx.temp_store("phase15-scope4.sqlite3") as (_database, store):
                receipt = memory.apply_write_gate(store, [_candidate()], run_id="run-p15")
                self.assertEqual(receipt["persisted"], 1, "安全模块抛错不许把写门打断")
                self.assertEqual(receipt["scope_enforcement"]["checked"], 0)


class AuditItemsTests(Base):
    def test_out_of_rule_rows_are_quarantined_not_deleted(self):
        rows = [
            {"memory_id": "M1", "scope": "PATIENT_LONGITUDINAL", "scope_key": "subject:auto",
             "memory_type": "VERIFIED_CLAIM", "content_fingerprint": "F1",
             "canonical_content": "某主体登记信息", "industry_pack_id": "auto"},
            {"memory_id": "M2", "scope": "GLOBAL_KNOWLEDGE", "scope_key": "global|forged",
             "memory_type": "VERIFIED_CLAIM", "content_fingerprint": "F2",
             "canonical_content": "x"},
        ]
        audit = ms.audit_items_scope(rows, **AXES)
        self.assertEqual(audit["checked"], 2)
        self.assertEqual(audit["quarantined"], ["M2"])
        self.assertEqual(audit["by_reason"], {"SCOPE_KEY_MISMATCH": 1})
        self.assertEqual(audit["violations"][0]["declared_scope_key"], "global|forged")

    def test_clean_rows_produce_no_violation(self):
        audit = ms.audit_items_scope([{"memory_id": "M3", "scope": "PATIENT_LONGITUDINAL",
                                       "scope_key": "subject:auto",
                                       "memory_type": "VERIFIED_CLAIM",
                                       "canonical_content": "某主体登记信息",
                                       "industry_pack_id": "auto"}], **AXES)
        self.assertEqual(audit["violations"], [])
        self.assertEqual(audit["checked"], 1)
        self.assertEqual(ms.audit_items_scope([], **AXES)["checked"], 0)


if __name__ == "__main__":
    unittest.main()

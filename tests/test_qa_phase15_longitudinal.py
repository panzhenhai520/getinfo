#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 15 · P15-03（longitudinal freshness）用例。

钉住 §14 的"长期病史可以长期保留，但**使用时检查时间**"：
  1. `decision` 三值**逐字复用** Phase 10 的闸门（不另造枚举）、`reason` 透传闸门理由码；
  2. 长期这一档只补**两条**时间规则：超窗口 → 复验；生效时间未到 → 不许用（BLOCK）；
  3. 非长期作用域原样透传（一个字段都不改），`reason` 标 `NOT_LONGITUDINAL`；
  4. 窗口可配（`QA_LONGITUDINAL_WINDOW_DAYS`），且**关掉开关即回到 Phase 10 行为**；
  5. 报告口径可复算（分母、比率、按档分布），空集合写 `None` 而不是 0。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_graph_contracts as contracts  # noqa: E402
import qa_memory as memory  # noqa: E402
import qa_memory_revalidation as mr  # noqa: E402
import qa_memory_safety as ms  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402

NOW = "2026-10-11T00:00:00.000Z"
FLAGS = ("QA_LONGITUDINAL_FRESHNESS", "QA_LONGITUDINAL_WINDOW_DAYS")


def _item(**overrides):
    row = {
        "memory_id": "MEM-longitudinal", "memory_type": "VERIFIED_CLAIM",
        "canonical_content": "某主体长期登记信息（历史沿革类事实）",
        "confidence": 0.8, "freshness_class": "LONG", "status": "ACTIVE",
        "scope": "PATIENT_LONGITUDINAL",
        "scope_key": "subject:auto", "industry_pack_id": "auto",
        "valid_from": "", "valid_until": "", "last_verified_at": "2026-10-01T00:00:00.000Z",
        "source_evidence_ids": ["article:1"], "decay_score": 0.9,
    }
    row.update(overrides)
    return row


class Base(unittest.TestCase):
    def setUp(self):
        self.saved = {name: os.environ.pop(name, None) for name in FLAGS}

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class GateReuseTests(Base):
    def test_decision_enum_is_the_phase10_one(self):
        row = ms.longitudinal_check(_item(), now=NOW)
        self.assertIn(row["decision"], contracts.MEMORY_FRESHNESS_DECISIONS)
        self.assertIn(row["decision"], mr.MEMORY_FRESHNESS_DECISIONS
                      if hasattr(mr, "MEMORY_FRESHNESS_DECISIONS")
                      else contracts.MEMORY_FRESHNESS_DECISIONS)
        self.assertTrue(row["contract_ok"], row.get("contract_error"))

    def test_non_longitudinal_scope_passes_the_gate_through_untouched(self):
        item = _item(scope="SESSION", scope_key="session:p15:s1:auto")
        row = ms.longitudinal_check(item, now=NOW)
        gate = mr.freshness_gate(item, now=NOW)
        self.assertFalse(row["longitudinal"])
        self.assertEqual(row["reason"], "NOT_LONGITUDINAL")
        self.assertEqual(row["decision"], gate["decision"], "非长期作用域不许改闸门结论")
        self.assertEqual(row["gate_reason"], gate["reason"])

    def test_terminal_status_keeps_the_gate_reason(self):
        row = ms.longitudinal_check(_item(status="REVOKED"), now=NOW)
        self.assertEqual(row["decision"], "BLOCK")
        self.assertEqual(row["reason"], "NOT_ACTIVE")
        self.assertEqual(row["gate_reason"], "NOT_ACTIVE")

    def test_gate_failure_degrades_to_a_conservative_receipt(self):
        """闸门模块不可用时：保守判 REVALIDATE 并记账，不抛错、不假装 ALLOW。"""
        import unittest.mock as mock

        with mock.patch.object(ms, "_freshness_gate",
                               return_value={"gate_version": "", "decision": "REVALIDATE",
                                             "reason": "", "error": "boom", "age_days": 0.0}):
            row = ms.longitudinal_check(_item(), now=NOW)
        self.assertEqual(row["decision"], "REVALIDATE")
        self.assertTrue(row["contract_ok"], row.get("contract_error"))


class LongitudinalWindowTests(Base):
    def test_age_beyond_the_window_is_escalated_to_revalidation(self):
        item = _item(last_verified_at="2025-01-01T00:00:00.000Z")   # ≈648 天
        gate = mr.freshness_gate(item, now=NOW)
        self.assertEqual(gate["decision"], "ALLOW", "先确认闸门本身是放行的（否则测的不是本规则）")
        row = ms.longitudinal_check(item, now=NOW)
        self.assertEqual(row["decision"], "REVALIDATE")
        self.assertEqual(row["reason"], "AGE_BEYOND_LONGITUDINAL_WINDOW")
        self.assertGreater(row["age_days"], row["window_days"])
        self.assertEqual(row["gate_reason"], "FRESH_AND_VERIFIED")

    def test_window_is_configurable(self):
        os.environ["QA_LONGITUDINAL_WINDOW_DAYS"] = "2000"
        row = ms.longitudinal_check(_item(last_verified_at="2025-01-01T00:00:00.000Z"), now=NOW)
        self.assertEqual(ms.longitudinal_window_days(), 2000.0)
        self.assertEqual(row["decision"], "ALLOW")
        self.assertEqual(row["reason"], "FRESH_AND_VERIFIED")

    def test_fresh_longitudinal_memory_stays_allowed(self):
        row = ms.longitudinal_check(_item(), now=NOW)
        self.assertEqual((row["decision"], row["reason"]), ("ALLOW", "FRESH_AND_VERIFIED"))

    def test_valid_from_in_the_future_blocks_the_use(self):
        row = ms.longitudinal_check(_item(valid_from="2027-01-01"), now=NOW)
        self.assertEqual(row["decision"], "BLOCK")
        self.assertEqual(row["reason"], "VALID_FROM_IN_FUTURE")
        self.assertEqual(row["settings"]["extra_rule"], "valid_from_in_future")

    def test_switch_off_returns_to_phase10_behaviour(self):
        os.environ["QA_LONGITUDINAL_FRESHNESS"] = "0"
        item = _item(last_verified_at="2025-01-01T00:00:00.000Z")
        row = ms.longitudinal_check(item, now=NOW)
        self.assertEqual(row["decision"], "ALLOW")
        self.assertEqual(row["reason"], "FRESH_AND_VERIFIED")
        self.assertIn("disabled_note", row["settings"])
        self.assertEqual(row["settings"]["extra_rule"], "")

    def test_future_valid_from_also_needs_the_switch(self):
        os.environ["QA_LONGITUDINAL_FRESHNESS"] = "0"
        row = ms.longitudinal_check(_item(valid_from="2027-01-01"), now=NOW)
        self.assertNotEqual(row["reason"], "VALID_FROM_IN_FUTURE")


class ReportTests(Base):
    def test_report_counts_and_rate_are_recomputable(self):
        rows = [_item(memory_id="M1"),
                _item(memory_id="M2", last_verified_at="2025-01-01T00:00:00.000Z"),
                _item(memory_id="M3", valid_from="2027-01-01"),
                _item(memory_id="M4", scope="SESSION", scope_key="session:p15:s1:auto")]
        report = ms.longitudinal_report(rows, now=NOW)
        self.assertEqual(report["checked"], 4)
        self.assertEqual(report["longitudinal"], 3, "非长期作用域不计入长期分母")
        self.assertEqual(report["longitudinal_decisions"], {"ALLOW": 1, "REVALIDATE": 1, "BLOCK": 1})
        self.assertEqual(report["beyond_window"], 1)
        self.assertEqual(report["not_yet_effective"], 1)
        self.assertAlmostEqual(report["stale_rate"], round(2 / 3.0, 6), places=6)

    def test_empty_input_gives_none_rate(self):
        report = ms.longitudinal_report([], now=NOW)
        self.assertEqual(report["checked"], 0)
        self.assertIsNone(report["stale_rate"], "空集合的比率必须写 None，不许编 0")

    def test_every_check_is_contract_valid(self):
        report = ms.longitudinal_report([_item(), _item(memory_id="M2", status="STALE")], now=NOW)
        for check in report["checks"]:
            ok, note = validate("longitudinal_freshness", {key: check[key] for key in (
                "longitudinal_version", "decision", "reason", "longitudinal")})
            self.assertTrue(ok, note)
            self.assertIn(check["reason"], list(contracts.MEMORY_FRESHNESS_REASONS)
                          + list(contracts.LONGITUDINAL_FRESHNESS_REASONS))


if __name__ == "__main__":
    unittest.main()

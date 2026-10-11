#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 15 · P15-04（SafetyCritical override）用例。

钉住 §22 的 Hard Safety Trigger 与 MASTER_RULES 第 17 条
（**安全规则优先于 cost/latency/user-burden**）：
  1. 三档触发**逐档可达**：危险信号**成组**（硬）/ 单个安全词（软，走 Phase 14 的
     0.6 效用比覆盖，口径一个字不改）/ 无；
  2. 硬触发**真的换赢家**：普通效用赢家让位给安全动作，并把被忽略的三项代价逐条写进回执；
  3. 硬触发下**追问负担上限与效用下限不拦安全动作**（§22 原话的机器形态），
     但**权限/来源/适用性三道闸门照旧**（安全不能凭空变出权限或数据源）；
  4. 开关关闭 → 逐字回到 Phase 14 行为（含回执键集与赢家）；
  5. 失败路径：没有可用安全动作时如实记 `NO_SAFETY_ACTION`，不硬凑。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_acquisition as ac  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_memory_safety as ms  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402

ALL_SOURCES = {"kb": True, "emr": True, "db": True, "graph": True, "web": True, "patient": True}
PERMISSIONS = {"patient_contact": True, "emr_read": True}
FLAGS = ("QA_SAFETY_HARD_TRIGGER", "QA_SAFETY_MIN_DANGER_SIGNS", "QA_ACQ_SAFETY_RATIO",
         "QA_ACQ_UTILITY_FLOOR")

# 两个危险信号成组（事故 + 召回），且文本里没有别的安全词
DANGER_TEXT = "该批次产品发生安全事故并触发召回"
SINGLE_TEXT = "该批次产品存在安全隐患"


def _gap(text=DANGER_TEXT, *, missing="NO_EVIDENCE", priority=0.6, safety_critical=None, **extra):
    gap = {
        "gap_id": "G-%d" % (abs(hash(text)) % 10000), "claim_id": "c1", "missing": missing,
        "priority": priority, "band": "high", "status": "open",
        "suggested_queries": ["%s 依据" % text], "suggested_routes": ["keyword"],
        "reason": "test", "claim_text": text, "scope": [],
        "priority_factors": {}, "evidence_requirement": {},
    }
    if safety_critical is not None:
        gap["safety_critical"] = safety_critical
    gap.update(extra)
    return gap


class Base(unittest.TestCase):
    def setUp(self):
        self.saved = {name: os.environ.pop(name, None) for name in FLAGS}

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _plan(self, gaps, **kwargs):
        return ac.plan_acquisition(gaps, sources=dict(ALL_SOURCES),
                                   permissions=dict(PERMISSIONS), **kwargs)


class TriggerTests(Base):
    def test_three_triggers_are_all_reachable(self):
        hard = ms.safety_trigger(_gap(DANGER_TEXT))
        soft = ms.safety_trigger(_gap(SINGLE_TEXT))
        none = ms.safety_trigger(_gap("该批次产品的上市时间安排"))
        self.assertEqual(hard["trigger"], "HARD_SAFETY_TRIGGER")
        self.assertEqual(hard["reason"], "DANGER_SIGN_COMBINATION")
        self.assertGreaterEqual(hard["sign_count"], hard["min_signs"])
        self.assertEqual(soft["trigger"], "SOFT_SAFETY_TRIGGER")
        self.assertEqual(soft["reason"], "SINGLE_SAFETY_TERM")
        self.assertEqual(none["trigger"], "NO_TRIGGER")
        self.assertEqual(none["reason"], "NO_SAFETY_TERM")

    def test_danger_signs_are_deduplicated_and_ordered(self):
        gap = _gap("安全事故与召回；事故与召回都提到了")
        signs = ms.danger_signs(gap)
        self.assertEqual(signs, sorted(set(signs), key=signs.index))
        self.assertEqual(len(signs), len(set(signs)))
        self.assertIn("事故", signs)
        self.assertIn("召回", signs)

    def test_min_signs_is_configurable(self):
        os.environ["QA_SAFETY_MIN_DANGER_SIGNS"] = "4"
        self.assertEqual(ms.safety_trigger(_gap(DANGER_TEXT))["trigger"], "SOFT_SAFETY_TRIGGER",
                         "DANGER_TEXT 命中三个信号（安全/事故/召回），阈值抬到 4 就降为软触发")
        os.environ["QA_SAFETY_MIN_DANGER_SIGNS"] = "1"
        self.assertEqual(ms.safety_trigger(_gap(SINGLE_TEXT))["trigger"], "HARD_SAFETY_TRIGGER")


class OverrideTests(Base):
    def test_hard_trigger_swaps_the_utility_winner_and_records_cost(self):
        plan = self._plan([_gap(DANGER_TEXT)])
        self.assertTrue(plan["safety_enforcement"], "硬触发必须留下回执")
        receipt = plan["safety_enforcement"]["receipts"][0]
        self.assertEqual(receipt["trigger"], "HARD_SAFETY_TRIGGER")
        self.assertEqual(receipt["reason"], "DANGER_SIGN_COMBINATION")
        self.assertTrue(receipt["overrode_utility"])
        self.assertEqual(receipt["ignored_cost_terms"], list(ms.IGNORED_COST_TERMS))
        self.assertNotEqual(receipt["winner_before"], receipt["winner_after"])
        self.assertIn(receipt["winner_after"], ("ask_patient", "retrieve_emr", "query_database"),
                      "硬触发时安全动作（直接问当事者 / 查内部登记）必须赢")
        self.assertEqual(plan["stats"]["hard_safety_triggers"], 1)
        self.assertEqual(plan["stats"]["hard_safety_overrides"], 1)
        ok, note = validate("safety_enforcement", {key: receipt[key] for key in (
            "override_version", "trigger", "reason", "danger_signs", "sign_count", "min_signs")})
        self.assertTrue(ok, note)
        # 被换掉的那条要留下"为什么被换"的痕迹
        swapped = [row for row in plan["candidates"]
                   if row.get("skip_reason") == "SAFETY_OVERRIDE"
                   and (row.get("factors") or {}).get("safety_override_kind")
                   == "HARD_SAFETY_TRIGGER"]
        self.assertTrue(swapped)
        self.assertEqual(swapped[0]["factors"]["replaced_by"], receipt["winner_after"])
        self.assertIn(swapped[0]["skip_reason"], contracts.ACQUISITION_SKIP_REASONS)

    def test_single_safety_term_keeps_the_phase14_soft_behaviour(self):
        plan = self._plan([_gap(SINGLE_TEXT)])
        self.assertEqual(plan["safety_enforcement"], {}, "单个安全词不产生硬触发回执")
        self.assertEqual(plan["stats"]["hard_safety_triggers"], 0)
        with mock_off():
            baseline = self._plan([_gap(SINGLE_TEXT)])
        self.assertEqual(plan["selected_gaps"], baseline["selected_gaps"])

    def test_cost_gates_are_bypassed_only_for_safety_actions(self):
        # 把常规动作的效用下限抬到极高：普通动作全被拦，安全动作仍然可以赢
        os.environ["QA_ACQ_UTILITY_FLOOR"] = "0.99"
        plan = self._plan([_gap(DANGER_TEXT)])
        bypassed = [row for row in plan["candidates"] if row.get("cost_gates_bypassed")]
        self.assertTrue(bypassed, "硬触发下安全动作必须绕过效用下限与负担上限")
        self.assertTrue(all(row["action"] in ("ask_patient", "retrieve_emr", "query_database")
                            for row in bypassed))
        self.assertFalse([row for row in bypassed
                          if row.get("skip_reason") in ("UTILITY_FLOOR", "OVER_USER_BURDEN")],
                         "被绕过的两道闸门不许再作为淘汰理由出现")
        self.assertTrue([row for row in bypassed if row["decision"] == "selected"],
                        "至少有一个安全动作真的被选中")
        self.assertTrue(plan["safety_enforcement"]["receipts"])

    def test_permissions_still_win(self):
        # 安全动作的两条来源（内部登记 / 直接问当事者）都不可用 → 不许硬凑
        sources = {**ALL_SOURCES, "db": False, "emr": False, "patient": False}
        plan = ac.plan_acquisition([_gap(DANGER_TEXT)], sources=sources,
                                   permissions={"patient_contact": False, "emr_read": False})
        receipt = plan["safety_enforcement"]["receipts"][0]
        self.assertEqual(receipt["reason"], "NO_SAFETY_ACTION",
                         "没有可用安全动作时如实记账，不许硬凑")
        self.assertFalse(receipt["overrode_utility"])
        self.assertEqual(receipt["ignored_cost_terms"], list(ms.IGNORED_COST_TERMS))

    def test_already_safety_action_is_recorded_without_override(self):
        """本来赢家就是安全动作时：记 ALREADY_SAFETY_ACTION，不动它。"""
        plan = self._plan([_gap(DANGER_TEXT, missing="MISSING_COUNTEREVIDENCE")])
        receipts = plan["safety_enforcement"]["receipts"]
        self.assertEqual(len(receipts), 1)
        self.assertIn(receipts[0]["reason"],
                      ("DANGER_SIGN_COMBINATION", "ALREADY_SAFETY_ACTION", "NO_SAFETY_ACTION"))
        ok, note = validate("safety_enforcement", {key: receipts[0][key] for key in (
            "override_version", "trigger", "reason", "danger_signs", "sign_count", "min_signs")})
        self.assertTrue(ok, note)


class _off:
    def __enter__(self):
        self.saved = os.environ.get("QA_SAFETY_HARD_TRIGGER")
        os.environ["QA_SAFETY_HARD_TRIGGER"] = "0"

    def __exit__(self, *exc):
        if self.saved is None:
            os.environ.pop("QA_SAFETY_HARD_TRIGGER", None)
        else:
            os.environ["QA_SAFETY_HARD_TRIGGER"] = self.saved
        return False


def mock_off():
    return _off()


class SwitchOffTests(Base):
    def test_switch_off_is_byte_identical_to_phase14(self):
        """关掉硬触发：回执键集与赢家与 Phase 14 逐字相同（回滚口径）。"""
        rows = [_gap(DANGER_TEXT), _gap(SINGLE_TEXT, missing="LOW_AUTHORITY")]
        with mock_off():
            plan = ac.plan_acquisition(rows, sources=dict(ALL_SOURCES),
                                       permissions=dict(PERMISSIONS))
        self.assertEqual(plan["safety_enforcement"], {})
        self.assertEqual(plan["stats"]["hard_safety_triggers"], 0)
        self.assertEqual(plan["stats"]["hard_safety_overrides"], 0)
        self.assertNotIn("HARD_SAFETY_TRIGGER",
                         str([row.get("factors") for row in plan["candidates"]]))
        receipt = ac.acquisition_receipt(plan)
        self.assertEqual(receipt["safety_enforcement"], {})
        self.assertIn("safety_enforcement", receipt, "空回执键也在（键集稳定）")

    def test_receipt_carries_the_safety_block_when_triggered(self):
        plan = self._plan([_gap(DANGER_TEXT)])
        receipt = ac.acquisition_receipt(plan)
        self.assertEqual(receipt["safety_enforcement"]["hard_triggers"], 1)
        self.assertIn("ignored_cost_terms", receipt["safety_enforcement"]["settings"])


class ReceiptFactoryTests(Base):
    def test_receipt_factory_validates_and_clamps(self):
        row = ms.safety_enforcement_receipt(_gap(DANGER_TEXT), winner_before="retrieve_document",
                                            winner_after="ask_patient",
                                            reason="DANGER_SIGN_COMBINATION",
                                            overrode_utility=True)
        self.assertTrue(row["contract_ok"], row.get("contract_error"))
        ok, note = validate("safety_enforcement", {key: row[key] for key in (
            "override_version", "trigger", "reason", "danger_signs", "sign_count", "min_signs")})
        self.assertTrue(ok, note)
        bad = ms.safety_enforcement_receipt(_gap(DANGER_TEXT), winner_before="not_an_action",
                                            reason="NOT_A_REASON")
        self.assertEqual(bad["winner_before"], "")
        self.assertIn(bad["reason"], contracts.SAFETY_TRIGGER_REASONS)

    def test_plan_receipt_is_empty_without_hard_triggers(self):
        self.assertEqual(ms.plan_safety_receipt([]), {})
        self.assertEqual(ms.plan_safety_receipt([{"trigger": "NO_TRIGGER"}]), {})


if __name__ == "__main__":
    unittest.main()

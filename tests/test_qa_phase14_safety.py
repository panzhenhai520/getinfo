#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 14 · P14-06（clinical/safety weights 与 Safety Override）用例。

钉住 §21/§22 与 MASTER_RULES 第 17 条（**医疗安全规则优先于 cost/latency/user-burden**）：
  1. §21 的三个临床因子（`clinical_risk` / `safety_weight` / `diagnostic_discrimination`）
     全部由缺口自身的既有字段派生，逐项可复算；
  2. `SafetyCritical=true` → `Importance` 抬到与 Phase 07 **同一档**（`SAFETY_PRIORITY_FLOOR`）；
  3. §22 的安全覆盖**真的能换掉赢家**：普通效用赢家（文档检索）让位给"直接问当事者"，
     并逐条记 `SAFETY_OVERRIDE`；
  4. 覆盖**有条件**（`safety_ratio`）：安全动作效用不够时不许硬换，否则会把普通问题拖成追问；
  5. §22 原话"不能因为 UserBurden 高就降低追问优先级"：安全覆盖下问诊的负担项被压到该档下界；
  6. 非安全缺口**逐字不变**（零行为变化）。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_acquisition as ac  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_phase14_fixtures as fx  # noqa: E402

ALL_SOURCES = {"kb": True, "emr": True, "db": True, "graph": True, "web": True, "patient": True}
PERMISSIONS = {"patient_contact": True, "emr_read": True}
FLAGS = ("QA_ACQ_SAFETY_RATIO",)


class _Base(unittest.TestCase):
    def setUp(self):
        self.saved = {name: os.environ.pop(name, None) for name in FLAGS}

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class ClinicalWeightTests(_Base):
    def test_three_factors_are_derived_from_the_gap(self):
        gap = fx.gap(missing="MISSING_COUNTEREVIDENCE", priority=0.8, safety_critical=True)
        clinical = ac.clinical_weights(gap)
        self.assertEqual(sorted(clinical), sorted(
            list(contracts.ACQUISITION_CLINICAL_FACTORS) + ["basis"]))
        self.assertEqual(clinical["safety_weight"], 1.0)
        self.assertAlmostEqual(clinical["clinical_risk"], 0.8, places=6)
        self.assertAlmostEqual(clinical["diagnostic_discrimination"], 1.0, places=6)
        self.assertEqual(clinical["basis"]["safety_critical"], True)

    def test_non_safety_gap_has_no_safety_weight(self):
        clinical = ac.clinical_weights(fx.gap(missing="NO_EVIDENCE", priority=0.9))
        self.assertEqual(clinical["safety_weight"], 0.0)
        self.assertAlmostEqual(clinical["clinical_risk"], 0.9 * 0.6, places=6)
        self.assertAlmostEqual(clinical["diagnostic_discrimination"], 0.3, places=6)

    def test_discrimination_ranks_falsification_highest(self):
        values = {missing: ac.clinical_weights(fx.gap(missing=missing))
                  ["diagnostic_discrimination"]
                  for missing in ("MISSING_COUNTEREVIDENCE", "CONTRADICTION",
                                  "AMBIGUOUS_ENTITY", "NO_EVIDENCE")}
        self.assertEqual(values["MISSING_COUNTEREVIDENCE"], 1.0)
        self.assertEqual(values["CONTRADICTION"], 1.0)
        self.assertEqual(values["AMBIGUOUS_ENTITY"], 0.6)
        self.assertEqual(values["NO_EVIDENCE"], 0.3,
                         "拿到'某条证据'的区分度最低（能证伪的才最有区分度）")

    def test_importance_is_floored_for_safety_critical(self):
        gap = fx.gap(missing="NO_EVIDENCE", priority=0.2, safety_critical=True)
        importance = ac.importance_of(gap)
        self.assertGreaterEqual(importance["value"], contracts.SAFETY_PRIORITY_FLOOR)
        self.assertTrue(importance["safety_override"])
        plain = ac.importance_of(fx.gap(missing="NO_EVIDENCE", priority=0.2))
        self.assertLess(plain["value"], contracts.SAFETY_PRIORITY_FLOOR)
        self.assertFalse(plain["safety_override"])


class SafetyOverrideTests(_Base):
    # 安全池 = 交互动作（问当事者）+ 内部登记/病历（db/emr）。本部署没有 EMR 通道，
    # 所以"问诊能赢"的场景要显式把 EMR 关掉（否则查病历更划算 —— 那也是 §21 的正确行为，
    # 见 test_emr_wins_when_its_channel_exists）。
    KB_AND_PATIENT = {"kb": True, "emr": False, "db": False, "graph": False,
                      "web": False, "patient": True}

    def _plan(self, *, safety_critical=True, missing="NO_EVIDENCE",
              sources=None, permissions=None, **kwargs):
        return ac.plan_acquisition([fx.gap(missing=missing, priority=0.9,
                                           safety_critical=safety_critical)],
                                   sources=sources or self.KB_AND_PATIENT,
                                   permissions=dict(permissions or PERMISSIONS), **kwargs)

    def test_safety_critical_flips_the_winner_and_records_it(self):
        plan = self._plan()
        selected = [row for row in plan["candidates"] if row["decision"] == "selected"]
        self.assertEqual([row["action"] for row in selected], ["ask_patient"])
        self.assertEqual(selected[0]["source"], "patient")
        losers = {row["action"]: row for row in plan["candidates"]
                  if row["decision"] == "skipped"}
        self.assertEqual(losers["retrieve_document"]["skip_reason"], "SAFETY_OVERRIDE")
        self.assertTrue(losers["retrieve_document"]["factors"]["safety_override"])
        self.assertEqual(losers["retrieve_document"]["factors"]["replaced_by"], "ask_patient")
        self.assertEqual(plan["stats"]["safety_overrides"], 1)
        self.assertEqual(plan["stats"]["safety_critical"], 6,
                         "安全关键缺口上的每条候选都带这个标记（它是缺口的属性）")

    def test_emr_wins_when_its_channel_exists(self):
        """§21 原话："是查病历更划算，还是查知识库，还是直接问患者最有效" —— 查病历真的会赢。"""
        plan = self._plan(sources={"kb": True, "emr": True, "db": False, "graph": False,
                                   "web": False, "patient": True},
                          permissions={"patient_contact": True, "emr_read": True})
        selected = [row for row in plan["candidates"] if row["decision"] == "selected"]
        self.assertEqual([row["action"] for row in selected], ["retrieve_emr"])
        losers = {row["action"]: row["skip_reason"] for row in plan["candidates"]
                  if row["decision"] == "skipped"}
        self.assertEqual(losers["retrieve_document"], "SAFETY_OVERRIDE")

    def test_plain_gap_keeps_the_utility_winner(self):
        plan = self._plan(safety_critical=False)
        selected = [row for row in plan["candidates"] if row["decision"] == "selected"]
        self.assertEqual([row["action"] for row in selected], ["retrieve_document"])
        self.assertEqual(plan["stats"]["safety_overrides"], 0)

    def test_override_is_conditional_on_the_safety_ratio(self):
        os.environ["QA_ACQ_SAFETY_RATIO"] = "0.99"
        plan = self._plan()
        selected = [row for row in plan["candidates"] if row["decision"] == "selected"]
        self.assertEqual([row["action"] for row in selected], ["retrieve_document"],
                         "安全动作效用不够时不许硬换（否则沾安全词就无视一切效用）")
        self.assertEqual(plan["stats"]["safety_overrides"], 0)
        self.assertEqual(ac.safety_ratio(), 0.99)

    def test_user_burden_is_floored_under_the_override(self):
        gap = fx.gap(missing="NO_EVIDENCE", priority=0.9, safety_critical=True)
        plain = fx.gap(missing="NO_EVIDENCE", priority=0.9, safety_critical=False)
        safety_utility = ac.action_utility("ask_patient", gap, safety_override=True)
        plain_utility = ac.action_utility("ask_patient", plain, safety_override=False)
        self.assertEqual(safety_utility["factors"]["user_burden_label"], "medium")
        self.assertAlmostEqual(safety_utility["factors"]["user_burden"],
                               ac.BURDEN_COST["low"], places=6)
        self.assertTrue(safety_utility["factors"]["burden_floor_applied"])
        self.assertAlmostEqual(plain_utility["factors"]["user_burden"],
                               ac.BURDEN_COST["medium"], places=6)
        self.assertGreater(safety_utility["utility"], plain_utility["utility"],
                           "§22：不能因为 UserBurden 高就降低追问优先级")

    def test_safety_never_fabricates_a_non_applicable_action(self):
        """安全覆盖也不许把"问人也拿不到"的知识型缺口变成问诊。"""
        plan = self._plan(missing="LOW_AUTHORITY",
                          sources={"kb": True, "emr": False, "db": True, "graph": False,
                                   "web": False, "patient": True})
        selected = [row for row in plan["candidates"] if row["decision"] == "selected"]
        self.assertEqual([row["action"] for row in selected], ["query_database"])
        losers = {row["action"]: row["skip_reason"] for row in plan["candidates"]
                  if row["decision"] == "skipped"}
        self.assertEqual(losers["ask_patient"], "NOT_APPLICABLE_TO_GAP")

    def test_override_never_loses_the_action_list(self):
        plan = self._plan()
        self.assertEqual(plan["selected"], ["ask_patient"])
        self.assertEqual(plan["by_source"], {"patient": 1})
        self.assertTrue(plan["contract_ok"], plan.get("contract_error"))


if __name__ == "__main__":
    unittest.main()

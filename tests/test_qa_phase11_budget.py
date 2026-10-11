#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 11 · P11-04（permission/cost/latency）用例。

钉住：
  1. 三个闸门（条数 / 成本 / 延迟）**各自都能单独把技能拦下来**，理由码唯一且可复算；
  2. **默认拒绝**：`emr_read` / `patient_contact` 不在默认授予集合里，
     `emr_search` / `patient_inquiry` 默认就被拒（而且被拒**不消耗**预算额度）；
  3. 权限判在预算之前（顺序写死）：被拒的通道不会把别的技能挤掉；
  4. 旋钮可配可回滚：`QA_SKILL_*` 环境变量改了，闸门跟着变；
  5. 非法权限位一律丢弃（拼错的权限绝不能变成提权）；
  6. 预算账面的 `snapshot()` 与契约 `skill_budget` 对得上，且逐项可复算。
"""
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_graph_contracts as contracts  # noqa: E402
import qa_skills as skills  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402

import qa_phase11_fixtures as fx  # noqa: E402


class EnvKnobMixin:
    """环境变量旋钮的临时覆盖（每个用例自己清理，避免互相污染）。"""

    def _set_env(self, **values):
        originals = {}
        for name, value in values.items():
            originals[name] = os.environ.get(name)
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = str(value)
        self.addCleanup(self._restore_env, originals)

    def _restore_env(self, originals):
        for name, value in originals.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class PermissionGateTests(EnvKnobMixin, unittest.TestCase):
    def test_default_denies_emr_and_patient_contact(self):
        budget = fx.budget()
        self.assertEqual(budget.granted_permissions,
                         tuple(contracts.SKILL_PERMISSIONS[:1]) + ("graph_read", "db_read",
                                                                   "web_access"))
        self.assertNotIn("emr_read", budget.granted_permissions)
        self.assertNotIn("patient_contact", budget.granted_permissions)
        registry = fx.registry()
        ok, why, _detail = budget.check(registry.get("emr_search"))
        self.assertFalse(ok)
        self.assertEqual(why, "PERMISSION_DENIED")
        ok, why, _detail = budget.check(registry.get("patient_inquiry"))
        self.assertFalse(ok)
        self.assertEqual(why, "PERMISSION_DENIED")

    def test_granting_the_permission_opens_the_gate(self):
        budget = fx.budget(granted_permissions=("emr_read",), max_latency_ms=60000.0)
        ok, why, _detail = budget.check(fx.registry().get("emr_search"))
        self.assertTrue(ok, why)

    def test_denied_permission_does_not_consume_budget(self):
        budget = fx.budget(max_skills=2)
        # 先试一个会被拒的技能
        ok, why, _detail = budget.check(fx.registry().get("emr_search"))
        self.assertFalse(ok)
        self.assertEqual(budget.used_cost_units, 0.0)
        self.assertEqual(budget.used_latency_ms, 0.0)
        self.assertEqual(budget.selected, [])
        # 然后一个合法的技能照样装得下（没被"顺带挤掉"）
        budget.consume(fx.registry().get("bm25_search"))
        self.assertEqual(budget.selected, ["bm25_search"])

    def test_partial_permission_grant_still_denied(self):
        budget = fx.budget(granted_permissions=("corpus_read", "graph_read"))
        ok, why, _detail = budget.check(fx.registry().get("web_search"))
        self.assertFalse(ok)
        self.assertEqual(why, "PERMISSION_DENIED")

    def test_illegal_permission_names_are_dropped_not_widened(self):
        self._set_env(QA_SKILL_PERMISSIONS="corpus_read,root_all,patient_contact,,")
        granted = skills.skill_granted_permissions()
        self.assertEqual(granted, ("corpus_read", "patient_contact"))
        self.assertNotIn("root_all", granted)

    def test_default_permission_set_when_env_absent(self):
        self._set_env(QA_SKILL_PERMISSIONS=None)
        self.assertEqual(skills.skill_granted_permissions(),
                         tuple(skills.DEFAULT_GRANTED_PERMISSIONS))


class CostGateTests(unittest.TestCase):
    def test_cost_units_are_monotone_and_readable(self):
        self.assertEqual(skills.skill_cost_units({"cost_class": "free"}), 0.0)
        self.assertEqual(skills.skill_cost_units({"cost_class": "cheap"}), 0.25)
        self.assertEqual(skills.skill_cost_units({"cost_class": "moderate"}), 1.0)
        self.assertEqual(skills.skill_cost_units({"cost_class": "expensive"}), 4.0)
        self.assertEqual(skills.skill_cost_units({}), 0.0)

    def test_cost_ceiling_blocks_the_next_skill(self):
        budget = fx.budget(max_skills=5, max_cost_units=1.0, max_latency_ms=60000.0,
                           granted_permissions=("corpus_read", "graph_read", "db_read"))
        budget.consume(fx.registry().get("semantic_search"))       # moderate = 1.0
        ok, why, detail = budget.check(fx.registry().get("graph_traversal"))
        self.assertFalse(ok)
        self.assertEqual(why, "OVER_COST_BUDGET")
        self.assertIn("1.00", detail)
        budget.mark_exhausted(why)
        self.assertEqual(budget.exhausted, ["OVER_COST_BUDGET"])

    def test_free_skills_never_touch_the_cost_budget(self):
        budget = fx.budget(max_skills=5, max_cost_units=0.0, max_latency_ms=60000.0)
        ok, why, _detail = budget.check(fx.registry().get("causal_reasoning"))
        self.assertTrue(ok, why)
        budget.consume(fx.registry().get("causal_reasoning"))
        self.assertEqual(budget.used_cost_units, 0.0)

    def test_cost_budget_is_configurable(self):
        budget = fx.budget(max_skills=5, max_cost_units=0.1, max_latency_ms=60000.0)
        ok, why, _detail = budget.check(fx.registry().get("bm25_search"))
        self.assertFalse(ok)
        self.assertEqual(why, "OVER_COST_BUDGET")


class LatencyGateTests(unittest.TestCase):
    def test_latency_units_are_monotone(self):
        self.assertEqual(skills.skill_latency_ms({"latency_class": "instant"}), 10.0)
        self.assertEqual(skills.skill_latency_ms({"latency_class": "slow"}), 12000.0)
        self.assertEqual(skills.skill_latency_ms({}), 0.0)

    def test_default_budget_excludes_the_slow_web_channel(self):
        # 这是**故意**的：外部检索是慢且贵的通道，不该在普通任务上被动加载
        budget = fx.budget()
        ok, why, detail = budget.check(fx.registry().get("web_search"))
        self.assertFalse(ok)
        self.assertEqual(why, "OVER_LATENCY_BUDGET")
        self.assertIn("12000", detail)
        self.assertLess(skills.DEFAULT_MAX_LATENCY_MS, skills.LATENCY_UNITS_MS["slow"])

    def test_raising_the_latency_budget_admits_it(self):
        budget = fx.budget(max_latency_ms=20000.0)
        ok, why, _detail = budget.check(fx.registry().get("web_search"))
        self.assertTrue(ok, why)

    def test_accumulated_latency_blocks_the_third_skill(self):
        budget = fx.budget(max_skills=5, max_cost_units=100.0, max_latency_ms=5000.0)
        budget.consume(fx.registry().get("semantic_search"))       # normal = 4000
        budget.consume(fx.registry().get("bm25_search"))           # fast = 400
        ok, why, _detail = budget.check(fx.registry().get("graph_traversal"))  # normal = 4000
        self.assertFalse(ok)
        self.assertEqual(why, "OVER_LATENCY_BUDGET")
        self.assertEqual(budget.used_latency_ms, 4400.0)


class CountAndStatusGateTests(unittest.TestCase):
    def test_skill_count_ceiling(self):
        budget = fx.budget(max_skills=1, max_cost_units=100.0, max_latency_ms=60000.0)
        budget.consume(fx.registry().get("bm25_search"))
        ok, why, detail = budget.check(fx.registry().get("semantic_search"))
        self.assertFalse(ok)
        self.assertEqual(why, "OVER_SKILL_COUNT")
        self.assertIn("bm25_search", detail)

    def test_zero_skills_means_nothing_loads(self):
        budget = fx.budget(max_skills=0)
        ok, why, _detail = budget.check(fx.registry().get("causal_reasoning"))
        self.assertFalse(ok)
        self.assertEqual(why, "OVER_SKILL_COUNT")

    def test_disabled_beats_every_other_gate(self):
        budget = fx.budget(max_skills=0, granted_permissions=())
        skill = dict(fx.registry().get("web_search"), status="disabled")
        ok, why, _detail = budget.check(skill)
        self.assertFalse(ok)
        self.assertEqual(why, "DISABLED")

    def test_deprecated_only_loads_when_explicit(self):
        budget = fx.budget(max_latency_ms=60000.0)
        skill = dict(fx.registry().get("bm25_search"), status="deprecated")
        ok, why, _detail = budget.check(skill)
        self.assertFalse(ok)
        self.assertEqual(why, "DEPRECATED_SKIPPED")
        ok, why, _detail = budget.check(skill, explicit=True)
        self.assertTrue(ok, why)

    def test_explicit_flag_does_not_grant_permissions(self):
        budget = fx.budget(max_latency_ms=60000.0)
        ok, why, _detail = budget.check(fx.registry().get("emr_search"), explicit=True)
        self.assertFalse(ok)
        self.assertEqual(why, "PERMISSION_DENIED")

    def test_dedupe_decision(self):
        budget = fx.budget(max_latency_ms=60000.0)
        budget.consume(fx.registry().get("bm25_search"))
        ok, why, _detail = budget.check(fx.registry().get("bm25_search"))
        self.assertFalse(ok)
        self.assertEqual(why, "MINIMAL_SET_DEDUPE")


class BudgetSnapshotTests(unittest.TestCase):
    def test_snapshot_matches_the_contract(self):
        budget = fx.budget()
        budget.consume(fx.registry().get("bm25_search"))
        budget.mark_exhausted("OVER_LATENCY_BUDGET")
        snapshot = budget.snapshot()
        ok, why = validate("skill_budget", snapshot)
        self.assertTrue(ok, why)
        self.assertEqual(snapshot["budget_version"], contracts.SKILL_BUDGET_VERSION)
        self.assertEqual(snapshot["used_skills"], 1)
        self.assertEqual(snapshot["used_cost_units"], 0.25)
        self.assertEqual(snapshot["used_latency_ms"], 400.0)
        self.assertEqual(snapshot["exhausted"], ["OVER_LATENCY_BUDGET"])
        self.assertEqual(snapshot["selected"], ["bm25_search"])

    def test_mark_exhausted_is_idempotent(self):
        budget = fx.budget()
        budget.mark_exhausted("OVER_COST_BUDGET")
        budget.mark_exhausted("OVER_COST_BUDGET")
        self.assertEqual(budget.exhausted, ["OVER_COST_BUDGET"])

    def test_budget_never_goes_negative_or_fractional_drift(self):
        budget = fx.budget(max_cost_units=100.0, max_latency_ms=100000.0)
        for name in ("bm25_search", "semantic_search", "graph_traversal", "sql_query",
                     "web_search", "causal_reasoning"):
            budget.consume(fx.registry().get(name))
        self.assertEqual(budget.used_cost_units, 0.25 + 1.0 + 1.0 + 0.25 + 4.0 + 0.0)
        self.assertEqual(budget.used_latency_ms, 400.0 + 4000.0 + 4000.0 + 400.0 + 12000.0 + 10.0)


class EnvDrivenBudgetTests(EnvKnobMixin, unittest.TestCase):
    def test_env_knobs_change_the_gates(self):
        self._set_env(QA_SKILL_MAX_SKILLS="1", QA_SKILL_MAX_COST_UNITS="0.5",
                      QA_SKILL_MAX_LATENCY_MS="500")
        budget = skills.SkillBudget()
        self.assertEqual(budget.max_skills, 1)
        self.assertEqual(budget.max_cost_units, 0.5)
        self.assertEqual(budget.max_latency_ms, 500.0)

    def test_illegal_env_values_fall_back_to_defaults(self):
        self._set_env(QA_SKILL_MAX_SKILLS="abc", QA_SKILL_MAX_COST_UNITS="",
                      QA_SKILL_MAX_LATENCY_MS="-5")
        self.assertEqual(skills.skill_max_skills(), skills.DEFAULT_MAX_SKILLS)
        self.assertEqual(skills.skill_max_cost_units(), skills.DEFAULT_MAX_COST_UNITS)
        self.assertEqual(skills.skill_max_latency_ms(), 0.0)   # 负数钳到 0（不是回落默认）

    def test_threshold_knobs(self):
        self._set_env(QA_SKILL_MIN_SUCCESS_RATE="0.5", QA_SKILL_BOOST_SUCCESS_RATE="0.6",
                      QA_SKILL_MIN_SAMPLES="5")
        self.assertEqual(skills.skill_min_success_rate(), 0.5)
        self.assertEqual(skills.skill_boost_success_rate(), 0.6)
        self.assertEqual(skills.skill_min_samples(), 5)
        self.assertEqual(contracts.DEFAULT_SKILL_MIN_SAMPLES, 3)

    def test_explicit_budget_arguments_win_over_env(self):
        self._set_env(QA_SKILL_MAX_SKILLS="9")
        budget = skills.SkillBudget(max_skills=2)
        self.assertEqual(budget.max_skills, 2)


if __name__ == "__main__":
    unittest.main()

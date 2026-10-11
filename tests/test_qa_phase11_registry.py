#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 11 · P11-02（Registry）用例。

钉住：
  1. 内置目录就是 §8 的 11 条声明，注册回执可复算（指纹 + 分类计数 + 通道表）；
  2. **拒收要记账**：未知技能标识、缺 required 字段、越界枚举、重名（未显式 replace）、
     非法注册来源 —— 五条失败路径全部 `ok=False` 且进 `rejections()`，绝不静默吞掉；
  3. 内置技能**删不掉**（没有 unregister）：停用只能改 `status`（可审计、可回滚）；
  4. 注册表不执行任何技能：模块里没有"调用/请求/端点"痕迹（P11-02 的边界）；
  5. 覆盖注册只影响被覆盖的那一条，其余声明与指纹的可复算性不破坏。
"""
import copy
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_graph_contracts as contracts  # noqa: E402
import qa_skills as skills  # noqa: E402

import qa_phase11_fixtures as fx  # noqa: E402


class BuiltinCatalogTests(unittest.TestCase):
    def test_catalog_is_the_spec_tree_in_order(self):
        registry = fx.registry()
        self.assertEqual(registry.ids(), contracts.SKILL_IDS)
        self.assertEqual([row["skill_id"] for row in registry.skills()], list(contracts.SKILL_IDS))
        self.assertEqual(registry.rejections(), [])

    def test_six_retrieval_and_five_reasoning(self):
        rows = fx.registry().skills()
        kinds = [row["kind"] for row in rows]
        self.assertEqual(kinds.count("retrieval"), 6)
        self.assertEqual(kinds.count("reasoning"), 5)

    def test_receipt_counts_are_recomputable(self):
        receipt = fx.registry().receipt()
        self.assertEqual(receipt["registry_version"], contracts.SKILL_REGISTRY_VERSION)
        self.assertEqual(receipt["schema_version"], contracts.SKILL_SCHEMA_VERSION)
        self.assertEqual(receipt["declared"], 11)
        self.assertEqual(sum(receipt["by_kind"].values()), 11)
        self.assertEqual(sum(receipt["by_cost"].values()), 11)
        self.assertEqual(sum(receipt["by_latency"].values()), 11)
        self.assertEqual(len(receipt["routes"]), 11)
        self.assertEqual(receipt["fingerprint"], fx.registry().fingerprint())
        self.assertEqual(receipt["rejections"], [])

    def test_catalog_returns_a_copy(self):
        first = skills.skill_catalog()
        first[0]["cost_class"] = "expensive"
        self.assertNotEqual(skills.skill_catalog()[0]["cost_class"], "expensive")
        self.assertEqual(skills.BUILTIN_SKILLS[0]["cost_class"], "cheap")

    def test_by_route_and_for_route(self):
        registry = fx.registry()
        self.assertEqual([row["skill_id"] for row in registry.by_route("keyword")],
                         ["bm25_search"])
        self.assertEqual(registry.for_route("graph_attribute"), "graph_traversal")
        self.assertEqual(registry.for_route("no_such_route"), "")
        # 空串不是通道：本部署没有这条通道的技能不会被反查出来
        self.assertEqual(registry.by_route(""), [])

    def test_get_returns_a_copy(self):
        registry = fx.registry()
        row = registry.get("bm25_search")
        row["cost_class"] = "free"
        self.assertEqual(registry.get("bm25_search")["cost_class"], "cheap")
        self.assertEqual(registry.get("no_such_skill"), {})


class RegistrationFailurePathTests(unittest.TestCase):
    def test_unknown_skill_id_is_rejected_and_recorded(self):
        registry = fx.registry()
        result = registry.register({"skill_id": "telepathy", "description": "读心",
                                    "input_schema": {}, "output_schema": {},
                                    "preconditions": [], "cost_class": "free",
                                    "latency_class": "instant", "permissions": [],
                                    "version": "qa-skill-schema-v1"})
        self.assertFalse(result["ok"])
        self.assertIn("未知技能标识", result["reason"])
        self.assertEqual(len(registry.rejections()), 1)
        self.assertNotIn("telepathy", registry.ids())

    def test_missing_required_field_is_rejected(self):
        registry = fx.registry()
        payload = {key: value for key, value in fx.registry().get("bm25_search").items()
                   if key != "preconditions"}
        result = registry.register(payload, source="injected", replace=True)
        self.assertFalse(result["ok"])
        self.assertIn("SKILL_SCHEMA", result["reason"])
        # 原来的声明**没有被破坏**（拒收是原子操作）
        self.assertEqual(registry.get("bm25_search")["cost_class"], "cheap")

    def test_illegal_enum_is_rejected(self):
        registry = fx.registry()
        payload = fx.registry().get("bm25_search")
        payload["permissions"] = ["root_all"]
        result = registry.register(payload, source="injected", replace=True)
        self.assertFalse(result["ok"])
        payload = fx.registry().get("bm25_search")
        payload["cost_class"] = "very_cheap"
        self.assertFalse(registry.register(payload, source="injected", replace=True)["ok"])

    def test_duplicate_without_replace_is_rejected(self):
        registry = fx.registry()
        payload = registry.get("bm25_search")
        result = registry.register(payload, source="injected")
        self.assertFalse(result["ok"])
        self.assertIn("已注册", result["reason"])

    def test_illegal_source_is_rejected(self):
        registry = fx.registry()
        result = registry.register(registry.get("bm25_search"), source="magic")
        self.assertFalse(result["ok"])
        self.assertIn("非法注册来源", result["reason"])

    def test_all_rejections_are_recorded_with_reason(self):
        registry = fx.registry()
        registry.register({"skill_id": "nope"})
        registry.register(registry.get("bm25_search"))
        self.assertEqual(len(registry.rejections()), 2)
        for row in registry.rejections():
            self.assertTrue(row["reason"].strip())


class ReplaceAndLifecycleTests(unittest.TestCase):
    def test_replace_only_touches_the_named_skill(self):
        registry = fx.registry()
        before = registry.fingerprint()
        payload = registry.get("web_search")
        payload["cost_class"] = "moderate"
        payload["version"] = "qa-skill-schema-v1+custom"
        result = registry.register(payload, source="injected", replace=True)
        self.assertTrue(result["ok"])
        self.assertNotEqual(result["fingerprint"], before)
        self.assertEqual(registry.get("web_search")["cost_class"], "moderate")
        self.assertEqual(registry.get("bm25_search")["cost_class"], "cheap")
        self.assertEqual(registry.get("web_search")["source"], "injected")
        self.assertEqual([row["skill_id"] for row in registry.skills()],
                         list(contracts.SKILL_IDS))

    def test_there_is_no_unregister(self):
        registry = fx.registry()
        self.assertFalse(hasattr(registry, "unregister"))
        self.assertFalse(hasattr(registry, "remove"))

    def test_disabled_skill_is_still_declared(self):
        registry = fx.registry()
        payload = registry.get("emr_search")
        payload["status"] = "disabled"
        self.assertTrue(registry.register(payload, source="injected", replace=True)["ok"])
        self.assertEqual(registry.get("emr_search")["status"], "disabled")
        self.assertIn("emr_search", registry.ids())

    def test_registry_is_deterministic_across_instances(self):
        first = fx.registry()
        second = fx.registry()
        self.assertEqual(first.fingerprint(), second.fingerprint())
        self.assertEqual(first.receipt()["routes"], second.receipt()["routes"])
        self.assertEqual(first.receipt(),
                         second.receipt())

    def test_fingerprint_is_stable_under_key_order(self):
        rows = skills.skill_catalog()
        shuffled = [dict(reversed(list(copy.deepcopy(row).items()))) for row in rows]
        self.assertEqual(skills.catalog_fingerprint(rows), skills.catalog_fingerprint(shuffled))


class RegistryBoundaryTests(unittest.TestCase):
    def test_registry_does_not_execute_or_call_anything(self):
        # P11-02 的能力边界：注册表只登记声明，执行发生在既有检索链路里
        source = open(os.path.join(REPO_ROOT, "qa_skills.py"), encoding="utf-8").read()
        for marker in ("subprocess", "os.system", "eval(", "exec("):
            self.assertNotIn(marker, source, "注册表不许执行外部动作：%s" % marker)

    def test_builtin_declarations_have_preconditions(self):
        for row in fx.registry().skills():
            self.assertTrue(row["preconditions"], "%s 必须声明前置条件" % row["skill_id"])

    def test_instruction_template_is_present_for_every_builtin(self):
        for row in fx.registry().skills():
            self.assertTrue(str(row["instruction_template"]).strip())


class InteractionSlotTests(unittest.TestCase):
    """交互类技能位**只声明不实现**（归 P14-07）：留给 Phase 14 挂载。"""

    def test_slot_is_declared_and_unmounted(self):
        slot = contracts.SKILL_INTERACTION_SLOT
        self.assertEqual(slot["skill_id"], "patient_inquiry")
        self.assertEqual(slot["owner_phase"], "P14-07")
        self.assertEqual(contracts.SKILL_INTERACTION_SLOT_OWNER, "P14-07")
        self.assertIs(slot["mounted"], False)
        self.assertIn("P14-07", slot["note"])

    def test_slot_skill_exists_in_the_registry(self):
        self.assertIn(contracts.SKILL_INTERACTION_SLOT["skill_id"], contracts.SKILL_IDS)
        self.assertIn(contracts.SKILL_INTERACTION_SLOT["skill_id"], fx.registry().ids())

    def test_receipt_carries_the_slot(self):
        receipt = fx.registry().receipt()
        self.assertEqual(receipt["interaction_slot"]["owner_phase"], "P14-07")
        self.assertIs(receipt["interaction_slot"]["mounted"], False)

    def test_phase11_does_not_implement_the_interaction_flow(self):
        # 越界会被判 FAIL：本阶段模块里不许出现 P14-07 的判定/流程标识
        source = open(os.path.join(REPO_ROOT, "qa_skills.py"), encoding="utf-8").read().casefold()
        for marker in ("repeat_question", "repeated_question", "strategy_change",
                       "same_question", "duplicate_question", "ask_count", "repeat_count"):
            self.assertNotIn(marker, source,
                             "Phase 11 不许实现 P14-07 的重复提问识别：%s" % marker)

    def test_slot_is_not_a_capability_of_the_declared_skill(self):
        # 技能声明里**不能**出现 P14-07 预留的能力（预留位 ≠ 已实现）
        skill = fx.registry().get("patient_inquiry")
        for name in contracts.SKILL_INTERACTION_SLOT["capabilities_not_implemented_here"]:
            self.assertNotIn(name, skill["capabilities"])

    def test_no_p14_07_modules_or_tables(self):
        import qa_schema

        for table in qa_schema.QA_REQUIRED_TABLES:
            self.assertNotIn("repeat", table.casefold())
            self.assertNotIn("strategy_change", table.casefold())


if __name__ == "__main__":
    unittest.main()

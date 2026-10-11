#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 11 · P11-01（Skill schema/version）+ 冻结契约守卫用例。

钉住：
  1. §8 的 11 个技能标识与九个声明字段**逐字**入契约（缺一个字段就注册不进去）；
  2. P11 的六个口径版本号是闭集合（换口径=换版本号）；
  3. 成本/延迟/权限/理由码/加载阶段/结局都是闭集合，且模块里实际用到的取值全部落在枚举内；
  4. **七个冻结 schema 指纹一字不变**、`EVIDENCE_SCHEMA.additionalProperties` 仍 False；
  5. 通道 ↔ 技能的双向映射与 Phase 07 的 `GAP_ROUTE_RULES` 同源（不出现冻结枚举外的通道值）；
  6. 零端点守卫（AST 级）：`qa_skills.py` 不 import 任何网络/模型库、源码零 http(s) 字面量。
"""
import ast
import hashlib
import json
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_contracts  # noqa: E402
import qa_gap_analyzer as gap_analyzer  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_schema  # noqa: E402
import qa_skills as skills  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402

import qa_phase11_fixtures as fx  # noqa: E402

FROZEN_FINGERPRINTS = {
    "EVIDENCE_SCHEMA": "370301331c02c738",
    "CLAIM_SCHEMA": "06fcdb02441248b2",
    "CONFLICT_SCHEMA": "aabd3259b07f9a3e",
    "LEVEL1_RESULT_SCHEMA": "7e864764429db111",
    "LEVEL2_RESULT_SCHEMA": "a0484f894e7bc9d6",
    "FINAL_ANSWER_SCHEMA": "4d1efa54ca1cbc1a",
    "QA_EVENT_SCHEMA": "59358bfa88a6c6af",
}

SPEC_SKILLS = ("bm25_search", "semantic_search", "graph_traversal", "sql_query", "emr_search",
               "web_search", "causal_reasoning", "clinical_evidence", "contradiction_resolution",
               "citation_verification", "patient_inquiry")

SPEC_DECLARED_FIELDS = ("skill_id", "description", "input_schema", "output_schema",
                        "preconditions", "cost_class", "latency_class", "permissions", "version")

NETWORK_MODULES = ("requests", "urllib3", "httpx", "aiohttp", "socket", "http.client",
                   "openai", "anthropic", "zhipuai", "dashscope", "sentence_transformers",
                   "transformers", "torch")


def _fingerprint(value) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _imports(path: str) -> set:
    tree = ast.parse(open(path, encoding="utf-8").read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _module_level_imports(path: str) -> set:
    """只取**模块级** import（不含函数体内的惰性 import）。

    为什么要区分：`qa_skills.skill_context_items()` 里有一处 `from qa_context_pack import
    make_context_item` 的**惰性**导入（与 Phase 09 的 `memory_context_items()` 同一手法）——
    它在函数体内，不会造成模块级循环依赖；判断"有没有环"必须只看模块级。
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class SkillSchemaTests(unittest.TestCase):
    def test_skill_ids_are_verbatim_from_spec(self):
        self.assertEqual(contracts.SKILL_IDS, SPEC_SKILLS)
        self.assertEqual(len(contracts.SKILL_IDS), 11)
        self.assertEqual(len(set(contracts.SKILL_IDS)), 11)

    def test_nine_declared_fields_are_all_required(self):
        required = list(contracts.SKILL_SCHEMA["required"])
        self.assertEqual(sorted(required), sorted(SPEC_DECLARED_FIELDS))
        self.assertEqual(len(required), 9)

    def test_builtin_catalog_matches_contract(self):
        rows = skills.skill_catalog()
        self.assertEqual([row["skill_id"] for row in rows], list(SPEC_SKILLS))
        for row in rows:
            ok, why = skills.validate_skill(row)
            self.assertTrue(ok, "%s 的声明过不了契约：%s" % (row.get("skill_id"), why))

    def test_every_declared_field_is_present_and_non_empty(self):
        for row in skills.skill_catalog():
            for name in SPEC_DECLARED_FIELDS:
                self.assertIn(name, row, "技能 %s 缺字段 %s" % (row.get("skill_id"), name))
                self.assertIsNotNone(row[name])
            self.assertTrue(str(row["description"]).strip())
            self.assertTrue(str(row["version"]).strip())
            self.assertTrue(row["permissions"] is not None)

    def test_version_constants_are_pinned(self):
        self.assertEqual(contracts.SKILL_SCHEMA_VERSION, "qa-skill-schema-v1")
        self.assertEqual(contracts.SKILL_REGISTRY_VERSION, "qa-skill-registry-v1")
        self.assertEqual(contracts.SKILL_ROUTER_VERSION, "qa-skill-router-v1")
        self.assertEqual(contracts.SKILL_BUDGET_VERSION, "qa-skill-budget-v1")
        self.assertEqual(contracts.SKILL_INSTRUCTION_VERSION, "qa-skill-instruction-v1")
        self.assertEqual(contracts.SKILL_TELEMETRY_VERSION, "qa-skill-telemetry-v1")
        for value in (contracts.SKILL_SCHEMA_VERSION, contracts.SKILL_REGISTRY_VERSION,
                      contracts.SKILL_ROUTER_VERSION, contracts.SKILL_BUDGET_VERSION,
                      contracts.SKILL_INSTRUCTION_VERSION, contracts.SKILL_TELEMETRY_VERSION):
            self.assertTrue(value.startswith("qa-skill-"))

    def test_instruction_version_constant_matches_module(self):
        self.assertEqual(contracts.SKILL_INSTRUCTION_VERSION, skills.SKILL_INSTRUCTION_VERSION)
        self.assertEqual(contracts.SKILL_TELEMETRY_VERSION, skills.SKILL_TELEMETRY_VERSION)

    def test_closed_enums(self):
        self.assertEqual(len(set(contracts.SKILL_IDS)), len(contracts.SKILL_IDS))
        for name in ("SKILL_KINDS", "SKILL_COST_CLASSES", "SKILL_LATENCY_CLASSES",
                     "SKILL_PERMISSIONS", "SKILL_STATUSES", "SKILL_SOURCES",
                     "SKILL_SELECTION_REASONS", "SKILL_ROUTER_DECISIONS", "SKILL_LOAD_STAGES",
                     "SKILL_LOAD_OUTCOMES", "SKILL_TELEMETRY_REASONS"):
            values = getattr(contracts, name)
            self.assertEqual(len(set(values)), len(values), "%s 有重复取值" % name)
            self.assertTrue(values, "%s 不能为空" % name)

    def test_cost_and_latency_units_cover_all_classes(self):
        self.assertEqual(sorted(skills.COST_UNITS), sorted(contracts.SKILL_COST_CLASSES))
        self.assertEqual(sorted(skills.LATENCY_UNITS_MS), sorted(contracts.SKILL_LATENCY_CLASSES))
        # 成本单调递增（档位必须能排序，否则"更便宜"无从判定）
        ordered = [skills.COST_UNITS[name] for name in contracts.SKILL_COST_CLASSES]
        self.assertEqual(ordered, sorted(ordered))
        ordered = [skills.LATENCY_UNITS_MS[name] for name in contracts.SKILL_LATENCY_CLASSES]
        self.assertEqual(ordered, sorted(ordered))


class RouteAlignmentTests(unittest.TestCase):
    def test_route_by_id_only_uses_frozen_routes_or_empty(self):
        for skill_id, route in contracts.SKILL_ROUTE_BY_ID.items():
            self.assertIn(skill_id, contracts.SKILL_IDS)
            self.assertIn(route, tuple(contracts.QA_RETRIEVAL_ROUTES) + ("",))

    def test_route_lookup_covers_all_seven_routes(self):
        self.assertEqual(sorted(contracts.SKILL_FOR_ROUTE),
                         sorted(contracts.QA_RETRIEVAL_ROUTES))
        for route, skill_id in contracts.SKILL_FOR_ROUTE.items():
            self.assertIn(skill_id, contracts.SKILL_IDS)
            # 反向回查必须回到声明通道，或回到一个**别名**通道（`page_context` 与 `keyword`
            # 都落到 `bm25_search`，与 Phase 07 的 `HUNTER_BY_ROUTE` 完全一致）
            aliases = tuple(name for name, value in contracts.SKILL_FOR_ROUTE.items()
                            if value == skill_id)
            self.assertIn(contracts.SKILL_ROUTE_BY_ID[skill_id], aliases,
                          "%s 的双向映射不一致" % route)

    def test_route_lookup_agrees_with_phase07_hunter_by_route(self):
        # Phase 07 已经定过"通道由谁跑"；Phase 11 的技能落点必须与它同源
        for route, hunters in gap_analyzer.HUNTER_BY_ROUTE.items():
            self.assertIn(route, contracts.SKILL_FOR_ROUTE)
            self.assertTrue(hunters, "Phase 07 的通道 %s 没有 Hunter" % route)

    def test_gap_type_rules_are_known_gaps_and_reasoning_skills(self):
        for missing, names in contracts.SKILL_GAP_TYPE_RULES.items():
            self.assertIn(missing, contracts.QA_GAP_TYPES)
            for name in names:
                self.assertIn(name, contracts.SKILL_IDS)

    def test_task_type_rules_are_known_intents(self):
        for name in contracts.SKILL_TASK_TYPE_RULES:
            self.assertIn(name, contracts.QUERY_INTENTS)
        for names in contracts.SKILL_TASK_TYPE_RULES.values():
            for skill_id in names:
                self.assertIn(skill_id, contracts.SKILL_IDS)

    def test_retrieval_skills_all_have_a_route_in_this_deployment(self):
        # 六个检索型技能里，五个落在真实通道上；`emr_search` 如实标空（本部署没有该通道）
        routes = {}
        for row in skills.skill_catalog():
            if row["kind"] != "retrieval":
                continue
            routes[row["skill_id"]] = row["route"]
        self.assertEqual(routes["bm25_search"], "keyword")
        self.assertEqual(routes["semantic_search"], "semantic")
        self.assertEqual(routes["graph_traversal"], "graph")
        self.assertEqual(routes["sql_query"], "policy_exact")
        self.assertEqual(routes["web_search"], "web")
        self.assertEqual(routes["emr_search"], "")

    def test_reasoning_skills_never_produce_evidence_and_have_no_route(self):
        for row in skills.skill_catalog():
            if row["kind"] == "reasoning":
                self.assertFalse(row["produces_evidence"])
                self.assertEqual(row["route"], "")


class SchemaRegistrationTests(unittest.TestCase):
    def test_all_phase11_schemas_are_registered(self):
        cases = {
            "skill": {"skill_id": "bm25_search", "description": "词面检索",
                      "input_schema": {}, "output_schema": {}, "preconditions": [],
                      "cost_class": "cheap", "latency_class": "fast",
                      "permissions": ["corpus_read"], "version": "qa-skill-schema-v1"},
            "skill_selection": {"router_version": contracts.SKILL_ROUTER_VERSION,
                                "skill_id": "bm25_search", "decision": "selected",
                                "reason": "GAP_ROUTE_MATCH"},
            "skill_routing": {"router_version": contracts.SKILL_ROUTER_VERSION,
                              "selected": ["bm25_search"], "trace": [], "budget": {}},
            "skill_instruction": {"instruction_version": contracts.SKILL_INSTRUCTION_VERSION,
                                  "skill_id": "bm25_search", "text": "指令",
                                  "is_evidence": False, "requires_revalidation": True,
                                  "in_citation_map": False},
            "skill_budget": {"budget_version": contracts.SKILL_BUDGET_VERSION, "max_skills": 3,
                             "granted_permissions": ["corpus_read"]},
            "skill_load_record": {"telemetry_version": contracts.SKILL_TELEMETRY_VERSION,
                                  "record_id": "SL1", "skill_id": "bm25_search",
                                  "stage_reached": "routed", "outcome": "skipped"},
            "skill_performance": {"telemetry_version": contracts.SKILL_TELEMETRY_VERSION,
                                  "skill_id": "bm25_search", "attempts": 0, "successes": 0,
                                  "success_rate": 0.0},
        }
        for name, payload in cases.items():
            ok, why = validate(name, payload)
            self.assertTrue(ok, "%s 过不了自校验：%s" % (name, why))

    def test_illegal_enum_values_are_rejected(self):
        cases = [
            ("skill", {"skill_id": "not_a_skill", "description": "x", "input_schema": {},
                       "output_schema": {}, "preconditions": [], "cost_class": "cheap",
                       "latency_class": "fast", "permissions": [], "version": "v1"}),
            ("skill", {"skill_id": "bm25_search", "description": "x", "input_schema": {},
                       "output_schema": {}, "preconditions": [], "cost_class": "very_cheap",
                       "latency_class": "fast", "permissions": [], "version": "v1"}),
            ("skill", {"skill_id": "bm25_search", "description": "x", "input_schema": {},
                       "output_schema": {}, "preconditions": [], "cost_class": "cheap",
                       "latency_class": "fast", "permissions": ["root_all"], "version": "v1"}),
            ("skill_selection", {"router_version": "v1", "skill_id": "bm25_search",
                                 "decision": "maybe", "reason": "GAP_ROUTE_MATCH"}),
            ("skill_selection", {"router_version": "v1", "skill_id": "bm25_search",
                                 "decision": "selected", "reason": "MADE_UP_REASON"}),
            ("skill_load_record", {"telemetry_version": "v1", "record_id": "SL1",
                                   "skill_id": "bm25_search", "stage_reached": "teleported",
                                   "outcome": "ok"}),
            ("skill_instruction", {"instruction_version": "v1", "skill_id": "bm25_search",
                                   "text": "t", "is_evidence": False,
                                   "requires_revalidation": True, "in_citation_map": False,
                                   "section": "evidence_context"}),
        ]
        for name, payload in cases:
            ok, _why = validate(name, payload)
            self.assertFalse(ok, "%s 本应被拦下：%r" % (name, payload))

    def test_missing_required_fields_are_rejected(self):
        ok, why = validate("skill", {"skill_id": "bm25_search"})
        self.assertFalse(ok)
        self.assertIn("必需字段", why)
        ok, why = validate("skill", {"skill_id": "bm25_search", "description": "x",
                                     "input_schema": {}, "output_schema": {},
                                     "cost_class": "cheap", "latency_class": "fast",
                                     "permissions": [], "version": "v1"})
        self.assertFalse(ok)
        self.assertIn("preconditions", why)
        ok, why = validate("skill_routing", {"router_version": "v1"})
        self.assertFalse(ok)

    def test_describe_mentions_phase11_sets(self):
        text = contracts.describe()
        self.assertIn("qa-skill-registry-v1", text)
        self.assertIn("qa-skill-router-v1", text)
        self.assertIn("技能 11", text)


class FrozenContractTests(unittest.TestCase):
    def test_seven_schema_fingerprints_unchanged(self):
        for name, expected in FROZEN_FINGERPRINTS.items():
            self.assertEqual(_fingerprint(getattr(qa_contracts, name)), expected,
                             "%s 的冻结指纹变了（P00-02 契约被改动）" % name)

    def test_evidence_schema_is_still_closed(self):
        self.assertFalse(qa_contracts.EVIDENCE_SCHEMA.get("additionalProperties", True))
        self.assertFalse(qa_contracts.FINAL_ANSWER_SCHEMA.get("additionalProperties", True))

    def test_frozen_enums_untouched(self):
        self.assertEqual(len(contracts.QA_RETRIEVAL_ROUTES), 7)
        self.assertEqual(len(contracts.QA_FAILURE_POLICIES), 5)
        self.assertEqual(len(contracts.QA_STOP_REASONS), 5)
        self.assertEqual(len(contracts.EVIDENCE_STATUSES), 5)
        self.assertEqual(len(contracts.QA_GAP_TYPES), 10)

    def test_phase08_context_contract_untouched(self):
        self.assertEqual(len(contracts.CONTEXT_SECTIONS), 9)
        self.assertEqual(len(contracts.CONTEXT_ITEM_KINDS), 10)
        self.assertEqual(len(contracts.CONTEXT_GAP_TYPES), 6)
        self.assertIn("LOAD_SKILL", contracts.CONTEXT_GAP_ACTIONS)
        self.assertEqual(len(contracts.CONTEXT_GAP_ACTIONS), 4)

    def test_sources_extended_additively(self):
        # Phase 11 只追加 `skill_registry`：Phase 08 的前 7 位与 Phase 09 的第 8 位逐字不变
        self.assertEqual(contracts.CONTEXT_ITEM_SOURCES[:7],
                         ("evidence_graph", "evidence", "plan", "verification", "gap_analyzer",
                          "request", "config"))
        self.assertEqual(contracts.CONTEXT_ITEM_SOURCES[7], "memory_graph")
        self.assertEqual(contracts.CONTEXT_ITEM_SOURCES[8], "skill_registry")

    def test_no_schema_version_bump_and_no_new_tables(self):
        # Phase 11 **不建表、不加列**：遥测复用既有 `qa_stage_runs`
        self.assertEqual(qa_schema.QA_SCHEMA_VERSION, "unified-qa-schema-v9")
        joined = " ".join(qa_schema.QA_TABLE_DDL).casefold()
        for name in ("skill_performance_memory", "skill_load_log", "skill_registry",
                     "skill_performance"):
            self.assertNotIn("create table if not exists %s " % name, joined)
            self.assertNotIn("create table if not exists %s(" % name, joined)
        for table in qa_schema.QA_REQUIRED_TABLES:
            self.assertFalse(table.startswith("skill"), "不许新增 skill_* 表：%s" % table)

    def test_switches_default_off(self):
        for name in ("QA_SKILL_ROUTER", "QA_SKILL_TELEMETRY"):
            os.environ.pop(name, None)
        self.assertFalse(skills.skill_router_enabled())
        self.assertFalse(skills.skill_telemetry_enabled())
        os.environ["QA_SKILL_ROUTER"] = "1"
        try:
            self.assertTrue(skills.skill_router_enabled())
        finally:
            os.environ.pop("QA_SKILL_ROUTER", None)


class DependencyGuardTests(unittest.TestCase):
    def test_module_imports_no_network_or_model_libraries(self):
        imported = _imports(os.path.join(REPO_ROOT, "qa_skills.py"))
        for name in imported:
            root = name.split(".")[0]
            self.assertNotIn(root, NETWORK_MODULES, "qa_skills 不许 import %s" % name)

    def test_source_has_no_http_literals_or_model_clients(self):
        source = open(os.path.join(REPO_ROOT, "qa_skills.py"), encoding="utf-8").read()
        for marker in ("http://", "https://", "embedding_client", "_embed_question",
                       "requests.", "urllib", "openai", "OpenAI("):
            self.assertNotIn(marker, source, "源码里出现 %s（零端点约束）" % marker)

    def test_no_wall_clock_or_randomness(self):
        source = open(os.path.join(REPO_ROOT, "qa_skills.py"), encoding="utf-8").read()
        for marker in ("datetime.now", "time.time", "random.", "uuid4", "utcnow"):
            self.assertNotIn(marker, source, "确定性口径不许用 %s" % marker)

    def test_context_pack_does_not_import_skills_at_module_level_for_cycles(self):
        # qa_context_pack 从**契约模块**取 SKILL_HINT_POLICY（不 import qa_skills），
        # qa_skills 也不在**模块级** import qa_context_pack（只在函数体内惰性导入）——
        # 依赖图双向都没有环。
        self.assertNotIn("qa_skills", _module_level_imports(
            os.path.join(REPO_ROOT, "qa_context_pack.py")))
        self.assertNotIn("qa_context_pack", _module_level_imports(
            os.path.join(REPO_ROOT, "qa_skills.py")))
        # 惰性导入确实存在（不是"因为没接线所以没环"）
        self.assertIn("qa_context_pack", _imports(os.path.join(REPO_ROOT, "qa_skills.py")))

    def test_hint_policy_has_a_single_definition(self):
        # 唯一定义在契约模块，两边都取它（避免两份政策文本漂移）
        self.assertEqual(skills.SKILL_HINT_POLICY, contracts.SKILL_HINT_POLICY)
        source = open(os.path.join(REPO_ROOT, "qa_context_pack.py"), encoding="utf-8").read()
        self.assertNotIn("SKILL_HINT_POLICY = (", source)
        self.assertIn("SKILL_HINT_POLICY", source)


class RegistryFingerprintStabilityTests(unittest.TestCase):
    def test_fingerprint_is_content_addressed(self):
        self.assertEqual(skills.catalog_fingerprint(), skills.catalog_fingerprint())
        first = fx.registry().fingerprint()
        second = fx.registry().fingerprint()
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("SK"))
        self.assertEqual(len(first), 22)

    def test_fingerprint_changes_when_a_declaration_changes(self):
        rows = skills.skill_catalog()
        before = skills.catalog_fingerprint(rows)
        rows[0]["cost_class"] = "expensive"
        self.assertNotEqual(before, skills.catalog_fingerprint(rows))


if __name__ == "__main__":
    unittest.main()

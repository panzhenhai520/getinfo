#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 14 · P14-01 契约守门用例。

钉住（这些断言**只许变强，不许放宽**）：
  1. **冻结七契约指纹复算不变**（EVIDENCE / CLAIM / CONFLICT / LEVEL1 / LEVEL2 / FINAL_ANSWER /
     QA_EVENT 与 P00-02 逐字相同），`FINAL_ANSWER_SCHEMA.additionalProperties` 仍为 False；
  2. §13/§21 的**六个动作逐字**进契约，§21/§22 的三个临床因子逐字进契约；
  3. Phase 14 新增的 8 个 schema 都注册进 `validate()`，且能被真实产物过；
  4. 动作→通道/来源/Hunter 三张映射**只吃既有枚举**（Phase 01 的七个通道、Phase 04 的 Hunter、
     Phase 11 的权限位），没有新造取值；
  5. **跨阶段契约变更已声明**：`QA_SCHEMA_VERSION` 由 v10 → **v11**（新增一张表
     `qa_realignment_decisions`），`QA_ADDED_COLUMNS_V11` 为空（零 ADD COLUMN）；
     断言仍是**等值断言**（`== v11`），只是字面量随阶段推进而同步；
  6. 两个新开关**默认关**；零网络 / 零模型：两个新模块不 import 网络或模型库、源码零 http(s)。
"""
import ast
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_acquisition as ac  # noqa: E402
import qa_contracts  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_phase14_fixtures as fx  # noqa: E402
import qa_realignment as rl  # noqa: E402
import qa_schema  # noqa: E402

FROZEN_FINGERPRINTS = {
    "EVIDENCE": "370301331c02c738",
    "CLAIM": "06fcdb02441248b2",
    "CONFLICT": "aabd3259b07f9a3e",
    "LEVEL1": "7e864764429db111",
    "LEVEL2": "a0484f894e7bc9d6",
    "FINAL_ANSWER": "4d1efa54ca1cbc1a",
    "QA_EVENT": "59358bfa88a6c6af",
}
NETWORK_MODULES = {"requests", "urllib", "urllib3", "httpx", "socket", "aiohttp", "http"}
MODEL_CLIENTS = {"openai", "anthropic", "dashscope", "zhipuai", "transformers", "torch"}
NEW_SCHEMAS = ("acquisition_action", "acquisition_plan", "patient_inquiry",
               "context_gap_no_repeat", "realignment_option", "realignment_interaction",
               "realignment_config", "realignment_receipt")


def _root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fingerprint(schema) -> str:
    import hashlib
    import json

    return hashlib.sha256(json.dumps(schema, ensure_ascii=False, sort_keys=True)
                          .encode("utf-8")).hexdigest()[:16]


class FrozenContractTests(unittest.TestCase):
    def test_seven_fingerprints_recompute_unchanged(self):
        actual = {
            "EVIDENCE": _fingerprint(qa_contracts.EVIDENCE_SCHEMA),
            "CLAIM": _fingerprint(qa_contracts.CLAIM_SCHEMA),
            "CONFLICT": _fingerprint(qa_contracts.CONFLICT_SCHEMA),
            "LEVEL1": _fingerprint(qa_contracts.LEVEL1_RESULT_SCHEMA),
            "LEVEL2": _fingerprint(qa_contracts.LEVEL2_RESULT_SCHEMA),
            "FINAL_ANSWER": _fingerprint(qa_contracts.FINAL_ANSWER_SCHEMA),
            "QA_EVENT": _fingerprint(qa_contracts.QA_EVENT_SCHEMA),
        }
        self.assertEqual(actual, FROZEN_FINGERPRINTS, "Phase 14 不许改冻结契约")

    def test_final_answer_schema_still_strict(self):
        self.assertFalse(qa_contracts.FINAL_ANSWER_SCHEMA["additionalProperties"])
        for key in ("acquisition", "realignment", "inquiries", "patient_inquiry"):
            self.assertNotIn(key, qa_contracts.FINAL_ANSWER_SCHEMA["properties"],
                             "获取/交互回执只能走兄弟键，不许塞进冻结的最终答案契约")

    def test_phase01_enums_unchanged(self):
        self.assertEqual(len(contracts.QA_RETRIEVAL_ROUTES), 7)
        self.assertEqual(len(contracts.QA_FAILURE_POLICIES), 5)
        self.assertEqual(len(contracts.QA_STOP_REASONS), 5)


class ActionSchemaTests(unittest.TestCase):
    def test_six_actions_are_verbatim(self):
        self.assertEqual(contracts.ACQUISITION_ACTIONS,
                         ("retrieve_document", "retrieve_emr", "graph_traverse",
                          "query_database", "web_search", "ask_patient"))
        self.assertEqual(contracts.ACQUISITION_UTILITY_FACTORS,
                         ("expected_gap_reduction", "importance", "evidence_reliability",
                          "latency", "cost", "user_burden"))
        self.assertEqual(contracts.ACQUISITION_CLINICAL_FACTORS,
                         ("clinical_risk", "safety_weight", "diagnostic_discrimination"))

    def test_mappings_only_use_existing_enums(self):
        for action, route in contracts.ACQUISITION_ACTION_ROUTES.items():
            self.assertIn(action, contracts.ACQUISITION_ACTIONS)
            self.assertIn(route, list(contracts.QA_RETRIEVAL_ROUTES) + [""],
                          "动作通道只允许既有七个通道或空串")
        for action, hunters in contracts.ACQUISITION_ACTION_HUNTERS.items():
            self.assertIn(action, contracts.ACQUISITION_ACTIONS)
            for hunter in hunters:
                self.assertIn(hunter, contracts.QA_HUNTER_IDS)
        self.assertEqual(contracts.ACQUISITION_ACTION_SOURCE["ask_patient"], "patient")
        self.assertEqual(sorted(contracts.ACQUISITION_ACTION_SOURCE.values()),
                         sorted(contracts.ACQUISITION_SOURCES),
                         "动作→来源必须覆盖全部来源取值（推导视图与集合对齐）")

    def test_interaction_actions_are_derived_view(self):
        derived = tuple(action for action in contracts.ACQUISITION_ACTIONS
                        if contracts.ACQUISITION_ACTION_ROUTES.get(action) == ""
                        and action == "ask_patient")
        self.assertEqual(derived, contracts.ACQUISITION_INTERACTION_ACTIONS)
        self.assertEqual(ac.action_kind("ask_patient"), "interaction")
        self.assertEqual(ac.action_kind("retrieve_document"), "retrieval")
        self.assertEqual(ac.action_route("ask_patient"), "")
        self.assertEqual(ac.action_route("retrieve_emr"), "")

    def test_safety_floor_is_the_same_number_as_phase07(self):
        self.assertEqual(contracts.SAFETY_PRIORITY_FLOOR, contracts.SAFETY_OVERRIDE_PRIORITY,
                         "P14 的安全覆盖档位必须与 Phase 07 同值（不许两套阈值）")

    def test_skip_reasons_are_closed_and_reachable(self):
        self.assertEqual(len(contracts.ACQUISITION_SKIP_REASONS), 10)
        self.assertEqual(len(set(contracts.ACQUISITION_SKIP_REASONS)),
                         len(contracts.ACQUISITION_SKIP_REASONS))
        self.assertIn("CONTEXT_GAP_NO_REPEAT", contracts.ACQUISITION_SKIP_REASONS)
        self.assertIn("SAFETY_OVERRIDE", contracts.ACQUISITION_SKIP_REASONS)
        self.assertEqual(contracts.CONTEXT_GAP_NO_REPEAT_REASONS,
                         ("EVIDENCE_ALREADY_IN_GRAPH", "EVIDENCE_ALREADY_IN_PACK",
                          "SPAN_TRUNCATED_ONLY", "COUNTEREVIDENCE_ALREADY_IN_GRAPH",
                          "CONFLICT_IS_ADJUDICATED"))

    def test_new_schemas_are_registered_and_pass_real_payloads(self):
        for name in NEW_SCHEMAS:
            ok, note = contracts.validate(name, {})
            self.assertFalse(ok, "%s 必须真的在 validate() 注册表里" % name)
            self.assertNotIn("未知 schema", note)
        plan = ac.plan_acquisition([fx.gap()])
        self.assertTrue(plan["contract_ok"], plan.get("contract_error"))
        option = ac.plan_acquisition([fx.gap()])  # 选项表由 realignment 产出
        del option
        for row in rl.options_payload():
            ok, note = contracts.validate("realignment_option", row)
            self.assertTrue(ok, note)
        inquiry = ac.patient_inquiry_candidate(fx.gap(missing="MISSING_TIME_LINK"))
        ok, note = contracts.validate("patient_inquiry", inquiry)
        self.assertTrue(ok, note)

    def test_describe_mentions_phase14_versions(self):
        text = contracts.describe()
        for version in (contracts.ACQUISITION_ACTION_SCHEMA_VERSION,
                        contracts.ACQUISITION_UTILITY_VERSION,
                        contracts.PATIENT_INQUIRY_VERSION,
                        contracts.CONTEXT_GAP_NO_REPEAT_VERSION,
                        contracts.CLINICAL_SAFETY_WEIGHTS_VERSION,
                        contracts.REALIGNMENT_VERSION):
            self.assertIn(version, text)
        self.assertIn("获取动作 6", text)
        self.assertIn("获取来源 6", text)
        self.assertIn("思路选项 8", text)
        self.assertIn("重复提问触发判据 2", text)


class SchemaMigrationTests(unittest.TestCase):
    def test_schema_version_is_v11_and_columns_are_zero(self):
        self.assertEqual(qa_schema.QA_SCHEMA_VERSION, "unified-qa-schema-v11",
                         "Phase 14 的跨阶段契约变更：v10 → v11（一张新表）")
        self.assertEqual(qa_schema.QA_ADDED_COLUMNS_V10, ())
        self.assertEqual(qa_schema.QA_ADDED_COLUMNS_V11, (), "v11 只许新建表，不许 ADD COLUMN")

    def test_new_table_is_declared_and_indexed(self):
        blob = " ".join(qa_schema.QA_TABLE_DDL)
        self.assertIn("CREATE TABLE IF NOT EXISTS qa_realignment_decisions ", blob)
        self.assertIn("qa_realignment_decisions", qa_schema.QA_REQUIRED_TABLES)
        self.assertIn("idx_qa_realignment_scope", " ".join(qa_schema.QA_INDEX_DDL))
        ddl = [statement for statement in qa_schema.QA_TABLE_DDL
               if "qa_realignment_decisions" in statement][0]
        for column in ("scope_key", "topic_key", "asked_count", "interaction_index",
                       "triggers_json", "evidence_json", "similarity_json", "decision",
                       "option_id", "config_json", "applied_keys_json", "previous_run_id",
                       "run_id", "last_asked_at", "ttl_until", "UNIQUE(scope_key, topic_key)"):
            self.assertIn(column, ddl, "v11 建表必须提供 %s（回执与幂等都要用）" % column)

    def test_writer_columns_match_the_ddl(self):
        from qa_storage import QaStore

        ddl = [statement for statement in qa_schema.QA_TABLE_DDL
               if "qa_realignment_decisions" in statement][0]
        for column in QaStore._REALIGNMENT_COLUMNS:
            self.assertIn(column, ddl, "写入列清单与建表列必须逐字一致：%s" % column)


class FlagTests(unittest.TestCase):
    def test_flags_default_off(self):
        saved = {name: os.environ.pop(name, None)
                 for name in ("QA_ACQUISITION", "QA_ASK_PATIENT",
                              "QA_REPEAT_QUESTION_ENABLED", "QA_REPEAT_QUESTION_MAX_INTERACTIONS")}
        try:
            self.assertFalse(ac.acquisition_enabled(), "P14 获取开关默认关（回滚口径）")
            self.assertFalse(ac.ask_patient_enabled(), "问诊路由默认关（本仓库无患者通道）")
            self.assertFalse(rl.realignment_enabled(), "P14-07 默认关（回滚口径）")
            self.assertEqual(rl.max_interactions(), 1, "同一议题默认只交互一次")
            os.environ["QA_ACQUISITION"] = "1"
            os.environ["QA_REPEAT_QUESTION_ENABLED"] = "1"
            os.environ["QA_REPEAT_QUESTION_MAX_INTERACTIONS"] = "99"
            self.assertTrue(ac.acquisition_enabled())
            self.assertTrue(rl.realignment_enabled())
            self.assertEqual(rl.max_interactions(), 10, "上限被钳制在 10（不许无限追问）")
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_settings_are_recomputable(self):
        settings = ac.acquisition_settings()
        for key in ("utility_floor", "max_user_burden", "route_fit_step", "route_fit_floor",
                    "epsilon", "weights"):
            self.assertIn(key, settings)
        self.assertEqual(sorted(settings["weights"]),
                         ["clinical_risk", "diagnostic", "priority", "safety"])
        other = rl.realignment_settings()
        for key in ("overlap_threshold", "window_turns", "unresolved_rate_threshold",
                    "ttl_hours", "max_interactions", "subject_jaccard_floor"):
            self.assertIn(key, other)
        self.assertEqual(other["env"], list(contracts.REPEAT_QUESTION_SETTINGS_ENV))


class GuardTests(unittest.TestCase):
    def test_no_network_or_model_imports(self):
        for name in ("qa_acquisition.py", "qa_realignment.py"):
            tree = ast.parse(open(os.path.join(_root(), name), encoding="utf-8").read())
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            self.assertEqual(imported & NETWORK_MODULES, set(), "%s 不许引入网络依赖" % name)
            self.assertEqual(imported & MODEL_CLIENTS, set(), "%s 不许引入模型客户端" % name)

    def test_source_has_no_http_literals(self):
        for name in ("qa_acquisition.py", "qa_realignment.py"):
            source = open(os.path.join(_root(), name), encoding="utf-8").read().casefold()
            self.assertNotIn("http://", source)
            self.assertNotIn("https://", source)

    def test_pipeline_only_imports_switches_and_entries(self):
        source = open(os.path.join(_root(), "qa_pipeline.py"), encoding="utf-8").read()
        self.assertIn("from qa_acquisition import (", source)
        self.assertIn("from qa_realignment import (", source)
        self.assertIn("if _realignment_enabled():", source)
        self.assertIn("if _acquisition_enabled() and gap_analyzer_enabled():", source)

    def test_memory_and_skill_hints_never_enter_citation_map(self):
        """MASTER_RULES 11：问诊候选与交互文案恒不是证据。"""
        for row in ac.plan_acquisition([fx.gap(missing="MISSING_TIME_LINK")],
                                       sources=ac.deployment_sources(ask_patient=True),
                                       permissions={"patient_contact": True})["candidates"]:
            if str(row.get("action")) != "ask_patient":
                continue
            inquiry = row.get("inquiry") or {}
            self.assertIs(inquiry.get("is_evidence"), False)
            self.assertIs(inquiry.get("in_citation_map"), False)
            self.assertIs(inquiry.get("requires_revalidation"), True)


if __name__ == "__main__":
    unittest.main()

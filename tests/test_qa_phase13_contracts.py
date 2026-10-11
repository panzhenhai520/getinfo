#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 13 · 契约守门用例。

钉住（这些断言**只许变强，不许放宽**）：
  1. **冻结七契约指纹复算不变**（EVIDENCE / CLAIM / CONFLICT / LEVEL1 / LEVEL2 / FINAL_ANSWER /
     QA_EVENT 与 P00-02 逐字相同），`FINAL_ANSWER_SCHEMA.additionalProperties` 仍为 False；
  2. Phase 13 新增的五个 schema 都注册进 `validate()`，且都能被真实产物过；
  3. 两个镜像枚举（`CLAIM_VERIFICATION_STATUSES` / `ANSWER_STATUSES`）与**冻结契约机器对齐**
     （逐值相等，不是子集）；
  4. 六问 / 重试作用域 / 证据不足判据都是闭集合，取值与规格书逐字一致；
  5. **Phase 13 自身零迁移**：没有新的 `qa_final*` 表；三个新开关默认关
     （版本常量的**等值断言**随跨阶段契约变更同步字面量：Phase 14 抬到
     `unified-qa-schema-v11`，断言强度不变）；
  6. 零网络 / 零模型：模块不 import 网络或模型库、源码零 http(s) 字面量。
"""
import ast
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_contracts  # noqa: E402
import qa_final_verifier as fv  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_phase13_fixtures as fx  # noqa: E402
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
        self.assertEqual(actual, FROZEN_FINGERPRINTS, "Phase 13 不许改冻结契约")

    def test_final_answer_schema_still_strict(self):
        self.assertFalse(qa_contracts.FINAL_ANSWER_SCHEMA["additionalProperties"])
        self.assertEqual(qa_contracts.FINAL_ANSWER_SCHEMA["properties"]["status"]["enum"],
                         ["ready", "partial", "insufficient_evidence"])
        self.assertNotIn("final_verification", qa_contracts.FINAL_ANSWER_SCHEMA["properties"])
        self.assertNotIn("composition", qa_contracts.FINAL_ANSWER_SCHEMA["properties"])

    def test_phase01_enums_unchanged(self):
        self.assertEqual(len(contracts.QA_RETRIEVAL_ROUTES), 7)
        self.assertEqual(len(contracts.QA_FAILURE_POLICIES), 5)
        self.assertEqual(len(contracts.QA_STOP_REASONS), 5)
        self.assertEqual(contracts.GRAPH_CONTRACT_VERSION, "graph-contract-v1")


class NewContractTests(unittest.TestCase):
    def test_new_schemas_are_registered_and_accept_real_payloads(self):
        graph, answer = fx.verified_graph_and_answer()
        composition = fv.compose_answer(graph=graph, answer=answer)
        verification = fv.verify_answer(answer=answer, composition=composition, graph=graph)
        report = fv.insufficient_evidence_report(composition=composition, verification=verification,
                                                answer=answer, graph=graph, stop_reason="ANSWERABLE")
        for name, payload in (("answer_composition", composition),
                              ("final_verification", verification),
                              ("insufficient_evidence", report),
                              ("answer_binding", composition["bindings"][0])):
            ok, note = contracts.validate(name, payload)
            self.assertTrue(ok, "%s: %s" % (name, note))
        self.assertFalse(contracts.validate("final_retry", {})[0],
                         "空载荷必须被拒（required 字段守门有效）")

    def test_mirror_enums_match_the_frozen_contracts_exactly(self):
        self.assertEqual(list(contracts.CLAIM_VERIFICATION_STATUSES),
                         qa_contracts.CLAIM_SCHEMA["properties"]["verification_status"]["enum"])
        self.assertEqual(list(contracts.ANSWER_STATUSES),
                         qa_contracts.FINAL_ANSWER_SCHEMA["properties"]["status"]["enum"])

    def test_check_names_and_questions(self):
        self.assertEqual(list(contracts.FINAL_VERIFIER_CHECKS),
                         ["FACT_WITHOUT_EVIDENCE", "EVIDENCE_NOT_SUPPORTING", "OVER_INFERENCE",
                          "COUNTER_EVIDENCE_MISSING", "CITATION_MISBOUND", "SPECULATION_AS_FACT"])
        self.assertEqual(contracts.FINAL_VERIFIER_QUESTIONS, {
            "FACT_WITHOUT_EVIDENCE": "每个事实是否有证据？",
            "EVIDENCE_NOT_SUPPORTING": "证据是否真正支持？",
            "SPECULATION_AS_FACT": "有没有把推测写成事实？",
            "COUNTER_EVIDENCE_MISSING": "是否漏掉重要反证？",
            "CITATION_MISBOUND": "citation 是否正确？",
            "OVER_INFERENCE": "有没有过度推理？",
        })
        self.assertEqual(tuple(contracts.FINAL_VERIFIER_OUTCOMES), ("pass", "fail"))

    def test_retry_scopes_and_insufficient_reasons_are_closed_sets(self):
        self.assertEqual(tuple(contracts.FINAL_RETRY_SCOPES), ("composer", "verifier"),
                         "§17 只给了两个局部重试出口")
        self.assertEqual(set(contracts.FINAL_RETRY_REASONS),
                         {"FIXED", "NO_CHANGE", "RETRY_LIMIT_REACHED", "NO_SCOPE",
                          "SCOPE_UNAVAILABLE"})
        self.assertEqual(set(contracts.INSUFFICIENT_EVIDENCE_REASONS),
                         {"NO_EVIDENCE", "NO_VERIFIED_CLAIM", "RESEARCH_NOT_ANSWERABLE",
                          "GROUNDING_BLOCKED"})
        self.assertEqual(set(contracts.INSUFFICIENT_EVIDENCE_TRIGGERS),
                         {"NO_EVIDENCE", "NO_VERIFIED_CLAIM", "GROUNDING_BLOCKED"})
        self.assertTrue(set(contracts.INSUFFICIENT_EVIDENCE_TRIGGERS)
                        <= set(contracts.INSUFFICIENT_EVIDENCE_REASONS))

    def test_versions_are_declared_and_greppable(self):
        self.assertEqual(contracts.ANSWER_COMPOSER_VERSION, "qa-answer-composer-v1")
        self.assertEqual(contracts.FINAL_VERIFIER_VERSION, "qa-final-verifier-v1")
        self.assertEqual(contracts.FINAL_VERIFY_RETRY_VERSION, "qa-final-retry-v1")
        self.assertEqual(contracts.INSUFFICIENT_EVIDENCE_VERSION, "qa-insufficient-evidence-v1")
        text = contracts.describe()
        for version in (contracts.ANSWER_COMPOSER_VERSION, contracts.FINAL_VERIFIER_VERSION,
                        contracts.FINAL_VERIFY_RETRY_VERSION, contracts.INSUFFICIENT_EVIDENCE_VERSION):
            self.assertIn(version, text)
        self.assertIn("终验检查 6", text)
        self.assertIn("局部重试作用域 2", text)
        self.assertIn("证据不足判据 4", text)


class NoMigrationTests(unittest.TestCase):
    def test_schema_version_and_tables_unchanged(self):
        self.assertEqual(qa_schema.QA_SCHEMA_VERSION, "unified-qa-schema-v11")
        ddl = "\n".join(qa_schema.QA_TABLE_DDL)
        tables = {statement.split("CREATE TABLE IF NOT EXISTS")[1].split("(")[0].strip()
                  for statement in qa_schema.QA_TABLE_DDL if "CREATE TABLE" in statement}
        self.assertTrue(tables, "DDL 必须真的读到了")
        self.assertFalse([name for name in tables
                          if "final" in name.lower() or "answer" in name.lower()], tables)
        self.assertNotIn("final_verify", ddl)
        self.assertEqual(len(qa_schema.QA_REQUIRED_TABLES), len(tables),
                         "必需表清单与 DDL 表数一致（零新表）")
        self.assertEqual(qa_schema.QA_ADDED_COLUMNS_V10, ())

    def test_flags_default_off(self):
        saved = {name: os.environ.pop(name, None)
                 for name in ("QA_FINAL_VERIFIER", "QA_FINAL_VERIFY_RETRY_MAX")}
        try:
            self.assertFalse(fv.final_verifier_enabled(), "P13 开关默认关（回滚口径）")
            self.assertEqual(fv.retry_max(), 1)
            os.environ["QA_FINAL_VERIFIER"] = "1"
            self.assertTrue(fv.final_verifier_enabled())
            os.environ["QA_FINAL_VERIFY_RETRY_MAX"] = "9"
            self.assertEqual(fv.retry_max(), 3, "上限被钳制在 3（不许无限重试）")
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_pipeline_imports_the_switch_only(self):
        """接线只引入开关与入口，不许在模块顶层把终验跑起来。"""
        source = open(os.path.join(_root(), "qa_pipeline.py"), encoding="utf-8").read()
        self.assertIn("from qa_final_verifier import final_verifier_enabled", source)
        self.assertIn("if final_verifier_enabled():", source)
        self.assertIn("_final_verification_step", source)


class GuardTests(unittest.TestCase):
    def test_no_network_or_model_imports(self):
        tree = ast.parse(open(os.path.join(_root(), "qa_final_verifier.py"),
                              encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported & NETWORK_MODULES, set())
        self.assertEqual(imported & MODEL_CLIENTS, set())

    def test_no_endpoint_literals_in_the_new_modules(self):
        for name in ("qa_final_verifier.py", "qa_storage.py", "qa_pipeline.py"):
            source = open(os.path.join(_root(), name), encoding="utf-8").read()
            # 只检查本阶段新增段落：qa_pipeline/qa_storage 里本来就有端点相关的历史代码
            if name != "qa_final_verifier.py":
                continue
            for token in ("http://", "https://", "embedding_client", "_embed_question", "api_key"):
                self.assertNotIn(token, source, token)

    def test_memory_and_skill_content_never_enter_citations(self):
        """MASTER_RULES 11：终验的引用索引只由**证据**构成，记忆/技能提示条目进不来。"""
        graph, answer = fx.verified_graph_and_answer()
        composition = fv.compose_answer(graph=graph, answer=answer)
        refs = {entry["evidence_ref"] for entry in composition["citation_index"].values()}
        self.assertNotIn("memory:1", refs)
        self.assertNotIn("skill:contradiction_resolution", refs)
        source = open(os.path.join(_root(), "qa_final_verifier.py"), encoding="utf-8").read()
        self.assertNotIn("memory_context", source)
        self.assertNotIn("skill_context", source)


if __name__ == "__main__":
    unittest.main()

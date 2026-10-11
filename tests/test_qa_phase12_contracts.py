#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 12 · P12-01…P12-06 契约段用例。

钉住：
  1. §3.2~§3.6 的五类经验记忆 + `QUERY_PATTERN` 的版本/闭集合**逐字**入契约
     （剧集五要素、四类失败、六因子、四档可靠性、重试两出口六理由码）；
  2. **类型归属单一真源**：`MEMORY_PRODUCIBLE_TYPES_BY_PHASE` 是
     `MEMORY_TYPE_OWNER_PHASE` 的派生视图（多一个键/少一个键/值不同都挂）；
  3. 八个新 schema 都能被 `validate()` 认出来，非法取值一律被拦；
  4. 七契约指纹**复算不变**、`EVIDENCE_SCHEMA.additionalProperties` 仍为 False、
     通道枚举仍 7 值、停止原因仍 5 值；
  5. 新模块零网络库、零 http(s) 字面量、零模型/嵌入端点调用点（GPU 硬约束）；
  6. `describe()` 把 Phase 12 的版本号与闭集合大小报出来。
"""
import ast
import hashlib
import json
import os
import re
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_contracts  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402

FROZEN_FINGERPRINTS = {
    "EVIDENCE_SCHEMA": "370301331c02c738",
    "CLAIM_SCHEMA": "06fcdb02441248b2",
    "CONFLICT_SCHEMA": "aabd3259b07f9a3e",
    "LEVEL1_RESULT_SCHEMA": "7e864764429db111",
    "LEVEL2_RESULT_SCHEMA": "a0484f894e7bc9d6",
    "FINAL_ANSWER_SCHEMA": "4d1efa54ca1cbc1a",
    "QA_EVENT_SCHEMA": "59358bfa88a6c6af",
}
NEW_MODULES = ("qa_memory_experience.py",)


def _fingerprint(value) -> str:
    """与 Phase 09~11 的守门用例**同一手法**：sort_keys + sha256 前 16 位。"""
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _imports(path):
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module.split(".")[0])
    return set(names)


class ClosedSetTests(unittest.TestCase):
    def test_episode_sections_are_the_five_spec_items(self):
        self.assertEqual(contracts.EPISODE_SECTIONS,
                         ("task_type", "decomposition", "key_gaps", "effective_paths",
                          "stop_reason"))

    def test_failure_kinds_are_the_four_spec_items(self):
        self.assertEqual(contracts.FAILURE_KINDS,
                         ("INVALID_QUERY", "FAILED_ROUTE", "FALSE_POSITIVE_SOURCE",
                          "SEMANTIC_DRIFT"))
        # §3.3 + MASTER_RULES 15：失败必须绑定 corpus/config/时间
        for field in ("corpus_version", "retrieval_config_version", "observed_at"):
            self.assertIn(field, contracts.FAILURE_BINDING_FIELDS)

    def test_retry_closed_sets(self):
        self.assertEqual(contracts.RETRY_DECISIONS, ("ALLOW_RETRY", "SUPPRESS"))
        self.assertEqual(set(contracts.RETRY_REASONS),
                         {"NO_FAILURE_RECORD", "SAME_ENVIRONMENT", "CORPUS_VERSION_CHANGED",
                          "RETRIEVAL_CONFIG_CHANGED", "VERSION_UNBOUND", "FAILURE_AGED_OUT"})
        # 唯一能压制的理由是"环境没变"
        self.assertEqual([reason for reason in contracts.RETRY_REASONS
                          if reason == "SAME_ENVIRONMENT"], ["SAME_ENVIRONMENT"])

    def test_source_reliability_factors_are_the_six(self):
        self.assertEqual(contracts.SOURCE_RELIABILITY_FACTORS,
                         ("authority", "verified_yield", "rejection_rate", "recency",
                          "corroboration", "volume"))
        self.assertEqual(contracts.SOURCE_RELIABILITY_BANDS,
                         ("HIGH", "MEDIUM", "LOW", "UNKNOWN"))

    def test_experience_recall_types_follow_the_planning_rule(self):
        # §10：Planning Recall 在 Planner 前召回 Strategy/Failure/QueryPattern
        for name in ("STRATEGY", "FAILURE", "QUERY_PATTERN"):
            self.assertIn(name, contracts.EXPERIENCE_RECALL_TYPES)
        self.assertIn("EPISODIC_RESEARCH", contracts.EXPERIENCE_RECALL_TYPES)

    def test_write_reasons_extended_additively(self):
        # P12 只**追加**两个理由码，P09 的取值一个字不动
        self.assertEqual(contracts.MEMORY_WRITE_REASONS[:3],
                         ("NO_VERIFIED_EVIDENCE", "EXTERNAL_INSTRUCTION_NOT_A_RULE",
                          "SENSITIVE_CONTENT"))
        self.assertIn("NO_TRACE_BINDING", contracts.MEMORY_WRITE_REASONS)
        self.assertIn("BELOW_SAMPLE_FLOOR", contracts.MEMORY_WRITE_REASONS)
        self.assertIn("TYPE_DEFERRED_TO_PHASE_12", contracts.MEMORY_WRITE_REASONS)


class OwnerPhaseSingleSourceTests(unittest.TestCase):
    def test_derived_map_matches_the_owner_map_key_by_key(self):
        flat = {name: phase for phase, rows in contracts.MEMORY_PRODUCIBLE_TYPES_BY_PHASE.items()
                for name in rows}
        self.assertEqual(set(flat) | {"MEMORY_ITEM"}, set(contracts.MEMORY_TYPE_OWNER_PHASE))
        for name, phase in flat.items():
            self.assertEqual(phase, contracts.MEMORY_TYPE_OWNER_PHASE[name], name)

    def test_p12_owns_exactly_the_six_experience_types(self):
        self.assertEqual(contracts.MEMORY_PRODUCIBLE_TYPES_BY_PHASE["P12"],
                         ("EPISODIC_RESEARCH", "STRATEGY", "FAILURE", "QUERY_PATTERN",
                          "SOURCE", "SKILL_PERFORMANCE"))
        self.assertEqual(contracts.EXPERIENCE_PRODUCER_PHASES, ("P09", "P12"))
        self.assertNotIn("P15", contracts.EXPERIENCE_PRODUCER_PHASES)


class SchemaTests(unittest.TestCase):
    def _cases(self):
        return {
            "experience_memory": {"experience_version": "v1", "run_id": "r1",
                                  "produced": {"FAILURE": 2}, "decisions": []},
            "episode_memory": {"episode_version": "v1", "run_id": "r1",
                               "sections": {"task_type": "SIMPLE_FACT"},
                               "summary": "经验·剧集：…", "summarizer": "rule:trace-v1"},
            "failure_memory": {"failure_version": "v1", "kind": "FAILED_ROUTE",
                               "route": "keyword", "corpus_version": "kb1",
                               "retrieval_config_version": "rcfg:1",
                               "observed_at": "2026-10-11T00:00:00Z"},
            "strategy_memory": {"strategy_version": "v1", "task_type": "SIMPLE_FACT",
                                "combination": ["keyword"], "observations": 3, "successes": 3,
                                "min_samples": 2, "success_definition": "…"},
            "source_memory": {"source_version": "v1", "domain": "example.com",
                              "reliability": 0.7, "band": "MEDIUM", "factors": {},
                              "observations": 3},
            "skill_performance_memory": {"skill_memory_version": "v1", "skill_id": "bm25_search",
                                         "attempts": 3, "successes": 2, "min_samples": 3,
                                         "success_definition": "…", "source": "qa_stage_runs"},
            "retry_decision": {"retry_version": "v1", "decision": "SUPPRESS",
                               "reason": "SAME_ENVIRONMENT"},
            "experience_guidance": {"guidance_version": "v1", "prefer_routes": ["keyword"],
                                    "avoid_routes": ["web"], "warnings": [],
                                    "requires_revalidation": True, "verified_evidence": False},
        }

    def test_every_new_schema_validates(self):
        for name, payload in self._cases().items():
            ok, note = contracts.validate(name, payload)
            self.assertTrue(ok, "%s: %s" % (name, note))

    def test_out_of_domain_values_are_rejected(self):
        cases = self._cases()
        bad = dict(cases["failure_memory"], kind="MADE_UP_KIND")
        self.assertFalse(contracts.validate("failure_memory", bad)[0])
        bad = dict(cases["source_memory"], band="PERFECT")
        self.assertFalse(contracts.validate("source_memory", bad)[0])
        bad = dict(cases["retry_decision"], reason="BECAUSE_I_SAID_SO")
        self.assertFalse(contracts.validate("retry_decision", bad)[0])
        bad = dict(cases["skill_performance_memory"], skill_id="invented_skill")
        self.assertFalse(contracts.validate("skill_performance_memory", bad)[0])
        bad = dict(cases["episode_memory"], stop_reason="SOMETHING_ELSE")
        self.assertFalse(contracts.validate("episode_memory", bad)[0])

    def test_guidance_is_always_a_hint(self):
        schema = contracts.EXPERIENCE_GUIDANCE_SCHEMA
        self.assertIn("requires_revalidation", schema["required"])
        self.assertIn("verified_evidence", schema["required"])


class FrozenContractTests(unittest.TestCase):
    def test_seven_fingerprints_unchanged(self):
        for name, fingerprint in FROZEN_FINGERPRINTS.items():
            self.assertEqual(_fingerprint(getattr(qa_contracts, name)), fingerprint, name)

    def test_frozen_enums_untouched(self):
        self.assertEqual(len(contracts.QA_RETRIEVAL_ROUTES), 7)
        self.assertEqual(len(contracts.QA_STOP_REASONS), 5)
        self.assertEqual(len(contracts.QA_FAILURE_POLICIES), 5)
        self.assertIs(qa_contracts.EVIDENCE_SCHEMA["additionalProperties"], False)

    def test_versions_are_recorded(self):
        self.assertEqual(contracts.EXPERIENCE_MEMORY_VERSION, "qa-experience-memory-v1")
        self.assertEqual(contracts.EPISODE_MEMORY_VERSION, "qa-episode-summary-v1")
        self.assertEqual(contracts.FAILURE_MEMORY_VERSION, "qa-failure-memory-v1")
        self.assertEqual(contracts.STRATEGY_MEMORY_VERSION, "qa-strategy-memory-v1")
        self.assertEqual(contracts.SOURCE_MEMORY_VERSION, "qa-source-reliability-v1")
        self.assertEqual(contracts.SKILL_PERFORMANCE_MEMORY_VERSION,
                         "qa-skill-performance-memory-v1")
        self.assertEqual(contracts.RETRY_POLICY_VERSION, "qa-version-aware-retry-v1")
        self.assertEqual(contracts.EXPERIENCE_GUIDANCE_VERSION, "qa-experience-guidance-v1")

    def test_describe_reports_phase12(self):
        text = contracts.describe()
        self.assertIn(contracts.EXPERIENCE_MEMORY_VERSION, text)
        self.assertIn("剧集要素", text)
        self.assertIn("失败种类", text)
        self.assertIn("重试出口", text)
        self.assertIn("来源档", text)


class GuardTests(unittest.TestCase):
    def test_new_module_has_no_network_or_model_dependencies(self):
        for name in NEW_MODULES:
            imported = _imports(os.path.join(REPO_ROOT, name))
            for forbidden in ("requests", "urllib3", "httpx", "socket", "aiohttp",
                              "openai", "transformers", "torch", "numpy"):
                self.assertNotIn(forbidden, imported, "%s 不许 import %s" % (name, forbidden))
            with open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
                source = handle.read()
            self.assertEqual(re.findall(r"https?://", source), [],
                             "%s 里不许出现 http(s) 字面量" % name)
            for call in ("chat_api", "embed", "embedding_client"):
                self.assertNotIn(call + "(", source, "%s 不许出现模型/嵌入调用点" % name)

    def test_new_module_never_writes_the_database_directly(self):
        """唯一写门：新模块里不许出现 INSERT/UPDATE/DELETE 或 store.save_* 直写。"""
        with open(os.path.join(REPO_ROOT, "qa_memory_experience.py"), encoding="utf-8") as handle:
            source = handle.read()
        for statement in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
            self.assertNotIn(statement, source)
        # 允许的写侧调用只有写门与两张投影表
        self.assertIn("apply_write_gate", source)
        self.assertIn("save_source_reliability", source)
        self.assertIn("save_skill_performance", source)


if __name__ == "__main__":
    unittest.main()

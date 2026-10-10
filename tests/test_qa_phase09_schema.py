#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 09 · P09-01 `memory schema/version/relations` 用例。

全部在**隔离临时 sqlite** 上真跑（`setUp` 断言 `backend == 'sqlite'`），零模型/零嵌入端点。
钉住：
  1. v7 → v8 只**新增八张 memory_* 表**、零 ADD COLUMN（`QA_ADDED_COLUMNS_V8 == ()`），
     清库建表与老库补建两条路径产出同一套表；
  2. §2.3 要求的记忆字段一个不少；§1.4 的十类型/九关系、§2.3 六状态、§14 五作用域**逐字**入契约；
  3. 仓储语义：内容寻址 id、**版本不覆盖**（每次变更追加 memory_version）、
     作用域隔离（同内容不同作用域是两条记忆）、幂等 upsert（链接/关系/决策/召回日志）；
  4. 失败路径：空 id、库不可用一律返回 `error` 而不是抛异常。
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_graph_contracts as contracts  # noqa: E402
import qa_memory as memory  # noqa: E402
import qa_schema  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402

import qa_phase09_fixtures as fx  # noqa: E402

MEMORY_TABLES = ("memory_item", "memory_version", "memory_entity_link", "memory_evidence_link",
                 "memory_relation", "memory_recall_log", "memory_write_decision",
                 "memory_usage_stat")


def _tables(database):
    return {str(row[0]) for row in database.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _columns(database, table):
    return {str(row[1]) for row in database.connection.execute(
        "PRAGMA table_info(%s)" % table).fetchall()}


class SchemaVersionTests(unittest.TestCase):
    def test_version_is_v8_and_no_added_columns(self):
        self.assertEqual(qa_schema.QA_SCHEMA_VERSION, "unified-qa-schema-v8")
        self.assertEqual(qa_schema.QA_ADDED_COLUMNS_V8, (), "v8 只允许新建表，不许 ADD COLUMN")
        self.assertEqual(len(qa_schema.QA_ADDED_COLUMNS_V6), 15, "Phase 01 的列清单不许被动过")

    def test_required_tables_include_the_eight_memory_tables(self):
        for name in MEMORY_TABLES:
            self.assertIn(name, qa_schema.QA_REQUIRED_TABLES)

    def test_fresh_ddl_creates_memory_tables_and_indexes(self):
        with fx.temp_store() as (database, _store):
            tables = _tables(database)
            for name in MEMORY_TABLES:
                self.assertIn(name, tables, "清库路径没建出 %s" % name)
            indexes = {str(row[0]) for row in database.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
            self.assertIn("idx_memory_item_scope", indexes)
            self.assertIn("idx_memory_evidence_ref", indexes)

    def test_old_library_upgrade_recreates_the_memory_tables(self):
        """老库（v7 形态）执行 ensure_qa_tables 时按 CREATE TABLE IF NOT EXISTS 自动补建。"""
        with fx.temp_store() as (database, store):
            for name in MEMORY_TABLES:
                database.connection.execute("DROP TABLE %s" % name)
            database.connection.commit()
            store.ensure_schema()
            tables = _tables(database)
            for name in MEMORY_TABLES:
                self.assertIn(name, tables, "老库升级没补建 %s" % name)

    def test_memory_item_columns_cover_the_architecture_fields(self):
        """§2.3 要求的字段一个不能少（含 §12 的 scope / created_from_session_id）。"""
        with fx.temp_store() as (database, _store):
            columns = _columns(database, "memory_item")
        for column in ("memory_id", "memory_type", "canonical_content", "confidence",
                       "freshness_class", "valid_from", "valid_until", "last_verified_at",
                       "status", "scope", "created_from_session_id", "created_at",
                       "entity_ids_json", "source_evidence_ids_json", "superseded_by",
                       "reuse_count", "version"):
            self.assertIn(column, columns, "memory_item 缺列 %s" % column)


class ContractEnumTests(unittest.TestCase):
    def test_types_match_architecture_node_types(self):
        self.assertEqual(len(contracts.MEMORY_TYPES), 10)
        for name in ("MEMORY_ITEM", "VERIFIED_CLAIM", "ENTITY", "EPISODIC_RESEARCH", "STRATEGY",
                     "FAILURE", "QUERY_PATTERN", "SOURCE", "SKILL_PERFORMANCE",
                     "USER_APPROVED_DOMAIN_RULE"):
            self.assertIn(name, contracts.MEMORY_TYPES)

    def test_statuses_scopes_relations_and_freshness_are_verbatim(self):
        self.assertEqual(list(contracts.MEMORY_STATUSES),
                         ["ACTIVE", "STALE", "SUPERSEDED", "CONTRADICTED", "EXPIRED", "REVOKED"])
        self.assertEqual(list(contracts.MEMORY_SCOPES),
                         ["GLOBAL_KNOWLEDGE", "ORGANIZATION", "PATIENT_LONGITUDINAL",
                          "ENCOUNTER", "SESSION"])
        self.assertEqual(list(contracts.MEMORY_RELATIONS),
                         ["ABOUT", "DERIVED_FROM", "VALIDATED_BY", "SUPERSEDES", "CONTRADICTS",
                          "EXPIRED_BY", "HELPED_RESOLVE", "FAILED_ON", "APPLIES_TO"])
        for name in ("LONG", "VERSION_SENSITIVE", "MEDIUM", "SHORT", "VERY_SHORT", "SESSION",
                     "ENCOUNTER_BOUND"):
            self.assertIn(name, contracts.MEMORY_FRESHNESS_CLASSES)

    def test_write_decisions_and_factor_sets(self):
        self.assertEqual(list(contracts.MEMORY_WRITE_DECISIONS),
                         ["DROP", "SESSION_ONLY", "PERSIST", "PERSIST_WITH_TTL"])
        self.assertEqual(len(contracts.MEMORY_WRITE_FACTORS), 7)
        self.assertEqual(len(contracts.MEMORY_RECALL_FACTORS), 6)
        self.assertEqual(list(contracts.MEMORY_WRITE_FACTORS),
                         ["reuse_probability", "confidence", "stability", "information_value",
                          "privacy_risk", "staleness_risk", "duplication_penalty"])
        self.assertEqual(list(contracts.MEMORY_RECALL_FACTORS),
                         ["semantic_relevance", "task_applicability", "confidence", "freshness",
                          "historical_utility", "contradiction_risk"])

    def test_owner_phase_table_covers_every_type(self):
        for name in contracts.MEMORY_TYPES:
            self.assertIn(name, contracts.MEMORY_TYPE_OWNER_PHASE)
            self.assertTrue(contracts.MEMORY_TYPE_OWNER_PHASE[name].startswith("P"))
        self.assertEqual(set(contracts.MEMORY_PRODUCIBLE_TYPES), {"VERIFIED_CLAIM", "ENTITY"})
        self.assertEqual(contracts.MEMORY_TYPE_OWNER_PHASE["STRATEGY"], "P12")
        self.assertEqual(contracts.MEMORY_TYPE_OWNER_PHASE["USER_APPROVED_DOMAIN_RULE"], "P15")


class IdentifierTests(unittest.TestCase):
    def test_content_addressed_ids_are_stable(self):
        first = memory.memory_id_for(scope="SESSION", memory_type="VERIFIED_CLAIM",
                                     canonical_content=" 同一段内容 ")
        second = memory.memory_id_for(scope="SESSION", memory_type="VERIFIED_CLAIM",
                                      canonical_content="同一段内容")
        third = memory.memory_id_for(scope="SESSION", memory_type="VERIFIED_CLAIM",
                                     canonical_content="另一段内容")
        self.assertEqual(first, second)
        self.assertNotEqual(first, third)

    def test_scope_keys_are_deterministic_and_session_bound(self):
        first = memory.scope_key("SESSION", owner_user_id="u1", session_id="s1",
                                 industry_pack_id="auto")
        second = memory.scope_key("SESSION", owner_user_id="u1", session_id="s2",
                                  industry_pack_id="auto")
        self.assertNotEqual(first, second, "当前状态类记忆必须绑会话（§14）")
        self.assertEqual(memory.scope_key("GLOBAL_KNOWLEDGE"), "global")
        self.assertEqual(memory.visible_scope_keys(owner_user_id="u1", session_id="s1",
                                                   industry_pack_id="auto")[0], "global")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database, self.store = fx.store_for(self.temp_dir.name)
        self.item = {
            "memory_id": memory.memory_id_for(scope="PATIENT_LONGITUDINAL",
                                              memory_type="VERIFIED_CLAIM",
                                              canonical_content="香港家族办公室税收优惠",
                                              industry_pack_id="auto"),
            "memory_type": "VERIFIED_CLAIM", "canonical_content": "香港家族办公室税收优惠",
            "content_fingerprint": memory.content_fingerprint("香港家族办公室税收优惠"),
            "confidence": 0.7, "freshness_class": "VERSION_SENSITIVE",
            "last_verified_at": "2026-10-11T00:00:00.000Z", "status": "ACTIVE",
            "scope": "PATIENT_LONGITUDINAL",
            "scope_key": memory.scope_key("PATIENT_LONGITUDINAL", industry_pack_id="auto"),
            "industry_pack_id": "auto", "entity_ids": ["家族办公室"],
            "source_evidence_ids": ["article:1"], "valid_from": "2026-04-01", "valid_until": "",
            "created_from_session_id": "s1", "created_from_run_id": "r1", "version": 1,
            "decay_score": 0.5, "change": "CREATE", "metadata": {"factors": {}},
        }

    def tearDown(self):
        try:
            self.database.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def test_save_is_idempotent_and_versions_are_appended(self):
        first = self.store.save_memory_item(self.item)
        self.assertTrue(first["created"])
        self.assertEqual(first["version"], 1)
        second = self.store.save_memory_item({**self.item, "confidence": 0.9, "change": "REFRESH"})
        self.assertFalse(second["created"])
        self.assertEqual(second["version"], 2, "同一个记忆再写一次必须追加版本而不是覆盖")
        rows = self.database.connection.execute(
            "SELECT version, change FROM memory_version WHERE memory_id=? ORDER BY version",
            (self.item["memory_id"],)).fetchall()
        self.assertEqual([int(row[0]) for row in rows], [1, 2])
        self.assertEqual(len(self.store.load_memory_items(memory_ids=[self.item["memory_id"]])), 1)

    def test_same_content_in_another_scope_is_another_memory(self):
        other = dict(self.item)
        other["scope"] = "SESSION"
        other["scope_key"] = memory.scope_key("SESSION", owner_user_id="u1", session_id="s1",
                                              industry_pack_id="auto")
        other["memory_id"] = memory.memory_id_for(scope="SESSION", memory_type="VERIFIED_CLAIM",
                                                  canonical_content=self.item["canonical_content"],
                                                  owner_user_id="u1", session_id="s1",
                                                  industry_pack_id="auto")
        self.store.save_memory_item(self.item)
        self.store.save_memory_item(other)
        self.assertEqual(len(self.store.load_memory_items(include_all_scopes=True)), 2)

    def test_evidence_links_and_relations_are_upserted(self):
        self.store.save_memory_item(self.item)
        link = {"evidence_ref": "article:1", "source_fingerprint": "SF1", "span_fingerprint": "SP1",
                "run_id": "r1", "verdict": "SUPPORTED", "metadata": {"article_id": 1}}
        self.assertEqual(self.store.link_memory_evidence(self.item["memory_id"], [link]), 1)
        self.assertEqual(self.store.link_memory_evidence(self.item["memory_id"], [link]), 1)
        self.assertEqual(len(self.store.memory_evidence(memory_ids=[self.item["memory_id"]])), 1)
        rows = [{"memory_id": self.item["memory_id"], "relation": "DERIVED_FROM",
                 "target_kind": "evidence", "target_ref": "article:1", "weight": 1.0}]
        self.assertEqual(self.store.add_memory_relation(rows), 1)
        self.assertEqual(self.store.add_memory_relation(rows), 1)
        self.assertEqual(len(self.store.memory_relations([self.item["memory_id"]])), 1)
        self.assertIn(self.store.memory_relations([self.item["memory_id"]])[0]["relation"],
                      contracts.MEMORY_RELATIONS)

    def test_write_decision_and_recall_log_are_upserted(self):
        decision = {"decision_id": "MWD1", "decision": "DROP", "reason": "UTILITY_BELOW_FLOOR",
                    "factors": {"utility": 0.01}, "gate_version": "v1"}
        self.assertEqual(self.store.record_memory_write_decision([decision]), 1)
        self.assertEqual(self.store.record_memory_write_decision([decision]), 1)
        self.assertEqual(len(self.store.memory_write_decisions()), 1)
        self.assertTrue(self.store.record_memory_recall(
            {"recall_id": "MRL1", "mode": "evidence", "hits": 2, "top_score": 0.4,
             "counts": {"lexical": 2}}))
        self.assertTrue(self.store.record_memory_recall(
            {"recall_id": "MRL1", "mode": "evidence", "hits": 3, "top_score": 0.5,
             "counts": {"lexical": 3}}))
        row = self.database.connection.execute(
            "SELECT count(*), max(hits) FROM memory_recall_log").fetchone()
        self.assertEqual((int(row[0]), int(row[1])), (1, 3))

    def test_usage_stat_and_reuse_count(self):
        self.store.save_memory_item(self.item)
        self.store.bump_memory_usage([self.item["memory_id"]], recalled=1)
        self.store.bump_memory_usage([self.item["memory_id"]], used=1, reuse=True)
        usage = self.store.memory_usage([self.item["memory_id"]])
        self.assertEqual(usage[self.item["memory_id"]]["recalled"], 1)
        self.assertEqual(usage[self.item["memory_id"]]["used"], 1)
        row = self.store.load_memory_items(memory_ids=[self.item["memory_id"]])[0]
        self.assertEqual(int(row["recall_count"]), 1)
        self.assertEqual(int(row["reuse_count"]), 1)

    def test_memory_stats_shape(self):
        self.store.save_memory_item(self.item)
        stats = self.store.memory_stats()
        self.assertEqual(stats["items"], 1)
        self.assertEqual(stats["by_type"].get("VERIFIED_CLAIM"), 1)
        self.assertEqual(stats["by_status"].get("ACTIVE"), 1)
        self.assertEqual(stats["error"], "")

    def test_failure_paths_do_not_raise(self):
        self.assertEqual(self.store.save_memory_item({"memory_type": "X"})["error"], "memory_id 为空")
        self.assertEqual(self.store.link_memory_evidence("m1", [{"evidence_ref": ""}]), 0)
        self.assertEqual(self.store.add_memory_relation([{"memory_id": ""}]), 0)
        self.assertEqual(self.store.record_memory_write_decision([{"decision": "DROP"}]), 0)
        self.assertFalse(self.store.record_memory_recall({"mode": "evidence"}))
        bad = self.store.update_memory_status("missing", status="STALE")
        self.assertEqual(bad["error"], "记忆不存在")
        closed = self.store.load_memory_items(scope_keys=["nope"])
        self.assertEqual(closed, [])


class ContractValidationTests(unittest.TestCase):
    def test_validate_memory_item_accepts_a_real_item_and_rejects_bad_scope(self):
        good = {"memory_id": "MEM1", "memory_type": "VERIFIED_CLAIM",
                "canonical_content": "内容", "confidence": 0.5, "freshness_class": "LONG",
                "status": "ACTIVE", "scope": "SESSION"}
        ok, note = memory.validate_memory_item(good)
        self.assertTrue(ok, note)
        ok, note = memory.validate_memory_item({**good, "scope": "PATIENT"})
        self.assertFalse(ok)
        ok, note = memory.validate_memory_item({**good, "memory_type": "NOT_A_TYPE"})
        self.assertFalse(ok)
        ok, note = validate("memory_item", good)
        self.assertTrue(ok, note)


if __name__ == "__main__":
    unittest.main()

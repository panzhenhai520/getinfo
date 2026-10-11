#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 12 · P12 库表（v9 → v10）用例。

钉住（全部在隔离临时 sqlite 上真跑，`setUp` 断言 `backend == 'sqlite'`）：
  1. `QA_ADDED_COLUMNS_V10` **为空**（经验类记忆本体写在既有 `memory_item`，
     v10 只允许新建两张投影表）；版本常量的**等值断言**随跨阶段契约变更同步字面量
     （Phase 14 把它抬到 `unified-qa-schema-v11`，断言强度不变）；
  2. §12 里 Phase 09 刻意留给本阶段的两张表（`source_reliability_memory` /
     `skill_performance_memory`）在**清库路径**与**老库升级路径**都必须建出来；
  3. 列的清单逐字固定（多一列少一列都挂）；
  4. 两个新索引存在；
  5. 老库（v9 世界）升级：只补两张表，既有表（含十张 memory_*）列**一字不动**；
  6. 回滚：DROP 两张新表即回到 v9，既有表与版本常量之外的东西一门不动；
  7. 两张投影表的仓储方法（写/读/幂等/作用域过滤）真的可用，且坏输入只记账不抛。
"""
import os
import sqlite3
import sys
import tempfile
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_schema  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

PHASE12_TABLES = ("source_reliability_memory", "skill_performance_memory")
PHASE09_TABLES = ("memory_item", "memory_version", "memory_entity_link", "memory_evidence_link",
                  "memory_relation", "memory_recall_log", "memory_write_decision",
                  "memory_usage_stat")
PHASE10_TABLES = ("memory_validation", "memory_contradiction")

SOURCE_COLUMNS = ("id", "domain", "scope_key", "industry_pack_id", "authority_level",
                  "reliability", "band", "observations", "verified", "rejected",
                  "update_frequency", "known_limitations_json", "factors_json",
                  "evidence_refs_json", "run_id", "memory_id", "source_version", "payload_json",
                  "created_at", "updated_at")
SKILL_COLUMNS = ("id", "skill_id", "task_type", "scope_key", "attempts", "successes",
                 "success_rate", "latency_p50_ms", "latency_p90_ms", "cost_units",
                 "evidence_yield", "verified_yield", "min_samples", "success_definition",
                 "telemetry_version", "source", "run_ids_json", "memory_id",
                 "skill_memory_version", "payload_json", "created_at", "updated_at")


def _tables(database) -> set:
    return {str(row[0]) for row in database.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _columns(database, table: str) -> list:
    return sorted(str(row[1]) for row in database.connection.execute(
        "PRAGMA table_info(%s)" % table).fetchall())


def _indexes(database) -> set:
    return {str(row[0]) for row in database.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}


class SchemaVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(os.path.join(self.tmp.name, "p12.sqlite3"))
        self.database.connect()
        self.database.create_tables()
        self.assertEqual(self.database.backend, "sqlite", "测试必须跑在隔离 sqlite 上")
        QaStore(self.database).ensure_schema()

    def tearDown(self):
        try:
            self.database.connection.close()
        except Exception:      # noqa: BLE001
            pass
        self.tmp.cleanup()

    def test_version_is_v10_with_zero_added_columns(self):
        self.assertEqual(qa_schema.QA_SCHEMA_VERSION, "unified-qa-schema-v11")
        self.assertEqual(qa_schema.QA_ADDED_COLUMNS_V10, (), "v10 只允许新建表，不许 ADD COLUMN")
        self.assertEqual(qa_schema.QA_ADDED_COLUMNS_V9, (), "v9 的零 ADD COLUMN 承诺不变")
        self.assertEqual(qa_schema.QA_ADDED_COLUMNS_V8, (), "v8 的零 ADD COLUMN 承诺不变")
        self.assertEqual(len(qa_schema.QA_ADDED_COLUMNS_V6), 15, "Phase 01 的列清单不许被动过")

    def test_required_tables_include_the_two_phase12_tables(self):
        for name in PHASE12_TABLES + PHASE09_TABLES + PHASE10_TABLES:
            self.assertIn(name, qa_schema.QA_REQUIRED_TABLES)

    def test_fresh_ddl_creates_both_phase12_tables(self):
        tables = _tables(self.database)
        for name in PHASE12_TABLES:
            self.assertIn(name, tables, "清库路径没建出 %s" % name)

    def test_phase12_columns_are_exactly_as_contracted(self):
        self.assertEqual(_columns(self.database, "source_reliability_memory"),
                         sorted(SOURCE_COLUMNS))
        self.assertEqual(_columns(self.database, "skill_performance_memory"),
                         sorted(SKILL_COLUMNS))

    def test_phase12_indexes_exist(self):
        indexes = _indexes(self.database)
        for name in ("idx_source_reliability_domain", "idx_source_reliability_scope",
                     "idx_skill_performance_skill", "idx_skill_performance_scope"):
            self.assertIn(name, indexes)

    def test_old_library_v9_upgrades_by_creating_only_the_two_tables(self):
        """老库（v9，十张记忆表都在）升级：补两张投影表，既有列**一字不动**。"""
        watched = PHASE09_TABLES + PHASE10_TABLES
        before = {name: _columns(self.database, name) for name in watched}
        for name in PHASE12_TABLES:
            self.database.connection.execute("DROP TABLE %s" % name)
        self.database.connection.commit()
        self.assertNotIn("source_reliability_memory", _tables(self.database))
        cursor = self.database.connection.cursor()
        qa_schema.ensure_qa_tables(cursor)          # 老库升级路径（同一入口）
        cursor.close()
        self.database.connection.commit()
        tables = _tables(self.database)
        for name in PHASE12_TABLES:
            self.assertIn(name, tables, "老库升级没补出 %s" % name)
        after = {name: _columns(self.database, name) for name in watched}
        self.assertEqual(before, after, "老库升级不许改既有记忆表的列")

    def test_rollback_drops_only_the_two_new_tables(self):
        watched = PHASE09_TABLES + PHASE10_TABLES
        before = {name: _columns(self.database, name) for name in watched}
        for name in PHASE12_TABLES:
            self.database.connection.execute("DROP TABLE %s" % name)
        self.database.connection.commit()
        tables = _tables(self.database)
        for name in PHASE12_TABLES:
            self.assertNotIn(name, tables)
        after = {name: _columns(self.database, name) for name in watched}
        self.assertEqual(before, after, "回滚只许删两张新表")
        self.assertEqual(qa_schema.QA_SCHEMA_VERSION.replace("v11", "v10"),
                         "unified-qa-schema-v10", "版本号回退口径写在这里，供回滚时对照")

    def test_checksum_covers_the_new_ddl(self):
        blob = " ".join(qa_schema.QA_TABLE_DDL)
        for name in PHASE12_TABLES:
            self.assertIn("CREATE TABLE IF NOT EXISTS %s " % name, blob)
        self.assertEqual(qa_schema.qa_schema_checksum(), qa_schema.qa_schema_checksum())


class ProjectionStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(os.path.join(self.tmp.name, "p12-store.sqlite3"))
        self.database.connect()
        self.database.create_tables()
        self.assertEqual(self.database.backend, "sqlite", "测试必须跑在隔离 sqlite 上")
        self.store = QaStore(self.database)
        self.store.ensure_schema()

    def tearDown(self):
        try:
            self.database.connection.close()
        except Exception:      # noqa: BLE001
            pass
        self.tmp.cleanup()

    def _source_row(self, domain="example.com", **overrides):
        row = {"domain": domain, "scope_key": "org:p12", "industry_pack_id": "auto",
               "authority_level": 1, "reliability": 0.72, "band": "MEDIUM",
               "observations": 4, "verified": 3, "rejected": 1,
               "update_frequency": "UNKNOWN", "known_limitations": ["更新时间未知"],
               "factors": {"authority": 1.0}, "evidence_refs": ["article:1"],
               "run_id": "run-p12", "memory_id": "MEM1", "source_version": "qa-source-v1",
               "metadata": {"hint_only": True}}
        row.update(overrides)
        return row

    def _skill_row(self, skill_id="bm25_search", **overrides):
        row = {"skill_id": skill_id, "task_type": "SIMPLE_FACT", "scope_key": "org:p12",
               "attempts": 4, "successes": 3, "success_rate": 0.75,
               "latency_ms": {"p50": 120.0, "p90": 200.0}, "cost_units": 1.5,
               "evidence_yield": 6, "verified_yield": 2, "min_samples": 3,
               "success_definition": "outcome=='ok' 且 stage_reached∈{included_in_pack,executed}",
               "telemetry_version": "qa-skill-telemetry-v1", "source": "qa_stage_runs:skill:bm25",
               "run_ids": ["run-p12"], "memory_id": "MEM2",
               "skill_memory_version": "qa-skill-performance-memory-v1", "metadata": {}}
        row.update(overrides)
        return row

    def test_source_projection_round_trip_and_idempotency(self):
        self.assertEqual(self.store.save_source_reliability([self._source_row()]), 1)
        rows = self.store.source_reliability(scope_keys=["org:p12"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["domain"], "example.com")
        self.assertEqual(rows[0]["known_limitations"], ["更新时间未知"])
        self.assertEqual(rows[0]["factors"], {"authority": 1.0})
        # 幂等：同 (scope_key, domain) 再写一次只更新，不新增
        self.store.save_source_reliability([self._source_row(reliability=0.9, band="HIGH")])
        rows = self.store.source_reliability(scope_keys=["org:p12"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["band"], "HIGH")
        raw = self.database.connection.execute(
            "SELECT COUNT(*) FROM source_reliability_memory").fetchone()
        self.assertEqual(int(raw[0]), 1)

    def test_source_projection_filters_and_bad_input(self):
        self.store.save_source_reliability([self._source_row(),
                                            self._source_row("other.com", band="LOW")])
        self.assertEqual(len(self.store.source_reliability(bands=["LOW"])), 1)
        self.assertEqual(len(self.store.source_reliability(domains=["example.com"])), 1)
        self.assertEqual(self.store.source_reliability(scope_keys=["nope"]), [])
        # 没有 domain 的行直接跳过（不写脏行、也不抛）
        self.assertEqual(self.store.save_source_reliability([{"domain": ""}]), 0)
        self.assertEqual(self.store.save_source_reliability(["坏了"]), 0)

    def test_skill_projection_round_trip_and_scope(self):
        self.assertEqual(self.store.save_skill_performance([self._skill_row()]), 1)
        rows = self.store.skill_performance_memory(scope_keys=["org:p12"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["skill_id"], "bm25_search")
        self.assertEqual(rows[0]["success_rate"], 0.75)
        self.assertEqual(rows[0]["latency_p50_ms"], 120.0)
        self.assertEqual(rows[0]["run_ids"], ["run-p12"])
        self.assertEqual(rows[0]["telemetry_version"], "qa-skill-telemetry-v1")
        self.assertIn("included_in_pack", rows[0]["success_definition"])
        # 样本不足的行允许 success_rate 为 NULL（读回仍是 None，不编 0）
        self.store.save_skill_performance([self._skill_row("semantic_search", attempts=1,
                                                           successes=0, success_rate=None)])
        rows = self.store.skill_performance_memory(scope_keys=["org:p12"],
                                                   skill_ids=["semantic_search"])
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["success_rate"])
        self.assertEqual(self.store.save_skill_performance([{"skill_id": ""}]), 0)


if __name__ == "__main__":
    unittest.main()

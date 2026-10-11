#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 10 · P10-01/P10-04 的库表用例（v8 → v9）。

全部在**隔离临时 sqlite** 上真跑（`setUp` 断言 `backend == 'sqlite'`），零模型/零嵌入端点。
钉住：
  1. v8 → v9 只**新增两张表**（`memory_validation` / `memory_contradiction`）、
     零 ADD COLUMN（`QA_ADDED_COLUMNS_V9 == ()`）；
  2. **老库（v8）补建**路径：先按 v8 的八张记忆表建库、再跑 `ensure_qa_tables()`，
     两张新表必须被补出来，且既有八张表的列**一个都没变**；
  3. **回滚口径**：DROP 两张新表 + 版本号改回 v8 之后，八张旧表与既有 QA 表仍完整可用；
  4. 两张新表的索引都在（验收/运维查询不全表扫）；
  5. 失败路径：库不可用时，P10 的读写方法一律返回空/带 error，而不是抛异常。
"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_schema  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

import qa_phase10_fixtures as fx  # noqa: E402

PHASE10_TABLES = ("memory_validation", "memory_contradiction")
PHASE09_TABLES = ("memory_item", "memory_version", "memory_entity_link", "memory_evidence_link",
                  "memory_relation", "memory_recall_log", "memory_write_decision",
                  "memory_usage_stat")


def _tables(database):
    return {str(row[0]) for row in database.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _columns(database, table):
    return [str(row[1]) for row in database.connection.execute(
        "PRAGMA table_info(%s)" % table).fetchall()]


def _indexes(database):
    return {str(row[0]) for row in database.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}


class SchemaVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(os.path.join(self.tmp.name, "p10.sqlite3"))
        self.database.connect()
        self.database.create_tables()
        self.assertEqual(self.database.backend, "sqlite", "测试必须跑在隔离 sqlite 上")
        # QA 表由 `QaStore.ensure_schema()` 建（与真实链路一致：create_tables 只管业务表）
        QaStore(self.database).ensure_schema()

    def tearDown(self):
        try:
            self.database.connection.close()
        except Exception:  # noqa: BLE001
            pass
        self.tmp.cleanup()

    def test_version_is_v9_with_zero_added_columns(self):
        self.assertEqual(qa_schema.QA_SCHEMA_VERSION, "unified-qa-schema-v10")
        self.assertEqual(qa_schema.QA_ADDED_COLUMNS_V9, (), "v9 只允许新建表，不许 ADD COLUMN")
        self.assertEqual(qa_schema.QA_ADDED_COLUMNS_V8, (), "v8 的零 ADD COLUMN 承诺不变")
        self.assertEqual(len(qa_schema.QA_ADDED_COLUMNS_V6), 15, "Phase 01 的列清单不许被动过")

    def test_required_tables_include_the_two_phase10_tables(self):
        for name in PHASE10_TABLES:
            self.assertIn(name, qa_schema.QA_REQUIRED_TABLES)
        for name in PHASE09_TABLES:
            self.assertIn(name, qa_schema.QA_REQUIRED_TABLES)

    def test_fresh_ddl_creates_both_phase10_tables(self):
        tables = _tables(self.database)
        for name in PHASE10_TABLES:
            self.assertIn(name, tables, "清库路径没建出 %s" % name)

    def test_phase10_columns_are_exactly_as_contracted(self):
        validation_columns = _columns(self.database, "memory_validation")
        for column in ("validation_id", "memory_id", "run_id", "trace_id", "outcome", "reason",
                       "gate_decision", "gate_reason", "status_before", "status_after",
                       "verdicts_json", "evidence_refs_json", "verified", "promoted",
                       "high_stakes", "judge", "revalidation_version", "gate_version",
                       "payload_json", "created_at"):
            self.assertIn(column, validation_columns)
        contradiction_columns = _columns(self.database, "memory_contradiction")
        for column in ("contradiction_id", "kind", "conflict_type", "left_memory_id",
                       "right_memory_id", "right_evidence_ref", "resolution", "reason_code",
                       "decider", "status_action", "rationale", "run_id",
                       "contradiction_version", "payload_json", "created_at"):
            self.assertIn(column, contradiction_columns)

    def test_phase10_indexes_exist(self):
        indexes = _indexes(self.database)
        for name in ("idx_memory_validation_memory", "idx_memory_validation_run",
                     "idx_memory_contradiction_left", "idx_memory_contradiction_run"):
            self.assertIn(name, indexes)

    def test_old_library_v8_upgrades_by_creating_only_the_two_tables(self):
        """老库（v8，只有八张记忆表）升级：两张新表补建，既有列**一字不动**。"""
        before = {name: _columns(self.database, name) for name in PHASE09_TABLES}
        self.database.connection.execute("DROP TABLE memory_contradiction")
        self.database.connection.execute("DROP TABLE memory_validation")
        self.database.connection.commit()
        self.assertNotIn("memory_validation", _tables(self.database))
        cursor = self.database.connection.cursor()
        qa_schema.ensure_qa_tables(cursor)      # 老库升级路径（与 Phase 09 的同一入口）
        cursor.close()
        self.database.connection.commit()
        tables = _tables(self.database)
        for name in PHASE10_TABLES:
            self.assertIn(name, tables, "老库升级没补出 %s" % name)
        after = {name: _columns(self.database, name) for name in PHASE09_TABLES}
        self.assertEqual(before, after, "老库升级不许改既有记忆表的列")

    def test_rollback_drops_only_the_two_new_tables(self):
        """回滚口径：DROP 两张新表 + 版本号改回 v8 —— 既有表与记忆能力全部保留。"""
        self.database.connection.execute("DROP TABLE memory_contradiction")
        self.database.connection.execute("DROP TABLE memory_validation")
        self.database.connection.commit()
        tables = _tables(self.database)
        for name in PHASE09_TABLES:
            self.assertIn(name, tables, "回滚不许伤到 %s" % name)
        self.assertNotIn("memory_validation", tables)
        store = QaStore(self.database)
        store.ensure_schema()
        self.assertEqual(store.memory_stats().get("error"), "")
        self.assertEqual(store.memory_revalidation_stats()["validations"], 0)


class RepositoryBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database, self.store = fx.store_for(self.tmp.name, "p10-repo.sqlite3")
        self.assertEqual(self.database.backend, "sqlite", "测试必须跑在隔离 sqlite 上")

    def tearDown(self):
        try:
            self.database.connection.close()
        except Exception:  # noqa: BLE001
            pass
        self.tmp.cleanup()

    def test_validation_rows_are_idempotent_by_validation_id(self):
        row = {"validation_id": "MVAL1", "memory_id": "MEM1", "run_id": "r1",
               "outcome": "REVALIDATED", "reason": "EVIDENCE_STILL_SUPPORTS",
               "gate_decision": "REVALIDATE", "gate_reason": "HIGH_STAKES_DEFAULT",
               "status_before": "ACTIVE", "status_after": "ACTIVE", "verdicts": {"SUPPORTED": 1},
               "evidence_refs": ["article:1"], "verified_evidence": True, "promoted": False,
               "revalidation_version": "qa-memory-revalidation-v1"}
        self.assertEqual(self.store.record_memory_validation([row]), 1)
        self.assertEqual(self.store.record_memory_validation([row]), 1)
        rows = self.store.memory_validations()
        self.assertEqual(len(rows), 1, "同 validation_id 只许留一行（幂等）")
        self.assertEqual(rows[0]["verdicts"], {"SUPPORTED": 1})
        self.assertEqual(rows[0]["evidence_refs"], ["article:1"])

    def test_validation_filters_by_outcome_and_run(self):
        for index, outcome in enumerate(("REVALIDATED", "REFUTED", "UNVERIFIED")):
            self.store.record_memory_validation([{
                "validation_id": "MVAL%d" % index, "memory_id": "MEM%d" % index, "run_id": "r1",
                "outcome": outcome, "reason": "EVIDENCE_STILL_SUPPORTS"}])
        self.assertEqual(len(self.store.memory_validations(outcomes=["REFUTED"])), 1)
        self.assertEqual(len(self.store.memory_validations(run_id="r1")), 3)
        self.assertEqual(len(self.store.memory_validations(run_id="nope")), 0)

    def test_contradiction_rows_are_idempotent_and_filterable(self):
        row = {"contradiction_id": "MCT1", "kind": "memory_memory", "conflict_type": "claim_conflict",
               "left_memory_id": "MEM1", "right_memory_id": "MEM2", "resolution": "resolved",
               "reason_code": "NEWER_VERSION_PRECEDES", "decider": "rule:qa-contradiction-resolver-v1",
               "status_action": "SUPERSEDE", "rationale": "较新的一方用于当前结论",
               "run_id": "r1", "contradiction_version": "qa-memory-contradiction-v1"}
        self.assertEqual(self.store.record_memory_contradiction([row]), 1)
        self.assertEqual(self.store.record_memory_contradiction([row]), 1)
        self.assertEqual(len(self.store.memory_contradictions()), 1)
        self.assertEqual(len(self.store.memory_contradictions(memory_ids=["MEM2"])), 1,
                         "右方命中也要能查到")
        self.assertEqual(len(self.store.memory_contradictions(resolutions=["unresolved"])), 0)

    def test_revalidation_stats_group_by_every_dimension(self):
        self.store.record_memory_validation([{
            "validation_id": "MVAL1", "memory_id": "MEM1", "run_id": "r1",
            "outcome": "REVALIDATED", "reason": "EVIDENCE_STILL_SUPPORTS",
            "gate_decision": "REVALIDATE", "verified_evidence": True, "promoted": True}])
        self.store.record_memory_contradiction([{
            "contradiction_id": "MCT1", "left_memory_id": "MEM1", "resolution": "unresolved",
            "reason_code": "NO_DECISIVE_RULE", "status_action": "CONTRADICT", "run_id": "r1"}])
        stats = self.store.memory_revalidation_stats(run_id="r1")
        self.assertEqual(stats["error"], "")
        self.assertEqual(stats["validations"], 1)
        self.assertEqual(stats["by_outcome"], {"REVALIDATED": 1})
        self.assertEqual(stats["by_reason"], {"EVIDENCE_STILL_SUPPORTS": 1})
        self.assertEqual(stats["verified"], 1)
        self.assertEqual(stats["promoted"], 1)
        self.assertEqual(stats["contradictions"], 1)
        self.assertEqual(stats["by_resolution"], {"unresolved": 1})

    def test_failure_paths_return_error_instead_of_raising(self):
        self.assertEqual(self.store.record_memory_validation([{"memory_id": "x"}]), 0)
        self.assertEqual(self.store.record_memory_contradiction([{"left_memory_id": "x"}]), 0)
        self.assertEqual(self.store.memory_validations(memory_ids=[]), [])
        broken = self.store.apply_memory_revalidation("", reason="x")
        self.assertTrue(broken["error"])
        broken = self.store.apply_memory_revalidation("MEM-not-exist", reason="x")
        self.assertEqual(broken["error"], "记忆不存在")
        broken = self.store.mark_memory_superseded("MEM1", superseded_by="MEM1")
        self.assertTrue(broken["error"])
        broken = self.store.mark_memory_superseded("", superseded_by="MEM2")
        self.assertTrue(broken["error"])
        self.assertEqual(self.store.revoke_memory_items([], reason="MANUAL_REVOKE")["checked"], 0)

    def test_superseded_field_and_version_row_are_written(self):
        fx.write_items(self.store, [fx.memory_item(memory_id="MEM-old"),
                                    fx.memory_item(content=fx.NEGATED_CLAIM, memory_id="MEM-new")])
        applied = self.store.mark_memory_superseded("MEM-old", superseded_by="MEM-new",
                                                    reason="NEWER_VERSION_PRECEDES", run_id="r1")
        self.assertTrue(applied["changed"])
        row = fx.load(self.store, "MEM-old")
        self.assertEqual(row["status"], "SUPERSEDED")
        self.assertEqual(row["superseded_by"], "MEM-new")
        versions = fx.version_rows(self.store, "MEM-old")
        self.assertEqual(versions[-1]["change"], "SUPERSEDE")
        self.assertEqual(versions[-1]["reason"], "NEWER_VERSION_PRECEDES")
        again = self.store.mark_memory_superseded("MEM-old", superseded_by="MEM-new",
                                                 reason="NEWER_VERSION_PRECEDES")
        self.assertFalse(again["changed"], "同后继重复取代必须幂等")
        self.assertEqual(len(fx.version_rows(self.store, "MEM-old")), len(versions))


if __name__ == "__main__":
    unittest.main(verbosity=2)

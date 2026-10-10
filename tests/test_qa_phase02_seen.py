#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 02 · P02-03（seen 与 confirmed）的落库与作用域验收。

钉住四件事：
  1. 库表结构 v6 → v7：新增 **一张表**（qa_evidence_seen），**不新增列**，老库靠
     `CREATE TABLE IF NOT EXISTS` 自动补建；既有列与既有数据一字不动；
  2. 作用域：seen 身份按 (owner_user_id, session_id, industry_pack_id) 精确隔离，
     任何一维不同都不共享——这是这一层最危险的错（A 会话的拒收误伤 B 会话）；
  3. 身份语义：被拒证据也留身份（MASTER_RULES 第 14 条）；confirmed 一旦达成不被
     后续 rejected 覆盖；重复登记是覆盖 + 计数，不是插新行；
  4. 失败路径：存储层异常一律吞掉并回报 `error`，绝不打断问答；`forget` 不允许
     在没有任何作用域键时清空全局。
  5. Phase 02 缺口 2：seen 身份的 TTL / 清理入口（默认关的开关 + 按时间删的保守口径）。
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_evidence as evidence_layer  # noqa: E402
from qa_schema import (  # noqa: E402
    QA_ADDED_COLUMNS_V6, QA_INDEX_DDL, QA_REQUIRED_TABLES, QA_SCHEMA_VERSION,
    QA_TABLE_DDL, ensure_qa_tables,
)
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

SCOPE = {"owner_user_id": "u1", "session_id": "s1", "industry_pack_id": "health"}


def _columns(connection, table):
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _item(ref="article:1", **overrides):
    item = {
        "evidence_ref": ref, "source_type": "article", "title": "标题 " + ref,
        "source_url": "https://example.com/" + ref, "content_excerpt": "正文 " + ref,
        "article_id": None, "metadata": {},
    }
    item.update(overrides)
    return item


class SchemaV7Tests(unittest.TestCase):
    def test_schema_version_is_v7(self):
        """Phase 02 的 seen 表在 v7 落地；Phase 09 起版本号为 **v8**（新增八张 memory_* 表）。

        断言仍是**字面量钉死**（不是"大于等于"）：版本漂移必须当场红；
        本条只做版本同步，v7 的列清单断言一条都没放松。
        """
        self.assertEqual(QA_SCHEMA_VERSION, "unified-qa-schema-v8")

    def test_v6_columns_are_still_declared(self):
        """v6 → v7 只是新增一张表：Phase 01 的 ADD COLUMN 清单一个都不能少。"""
        self.assertTrue(QA_ADDED_COLUMNS_V6)
        for table_name, column_name, definition in QA_ADDED_COLUMNS_V6:
            ddl = next(sql for sql in QA_TABLE_DDL
                       if f"CREATE TABLE IF NOT EXISTS {table_name} " in sql)
            self.assertIn("%s %s" % (column_name, definition), ddl)

    def test_seen_table_is_declared_and_required(self):
        self.assertIn("qa_evidence_seen", QA_REQUIRED_TABLES)
        ddl = next(sql for sql in QA_TABLE_DDL
                   if "CREATE TABLE IF NOT EXISTS qa_evidence_seen " in sql)
        for column in ("owner_user_id", "session_id", "industry_pack_id", "source_fingerprint",
                       "span_fingerprint", "evidence_ref", "source_type", "status",
                       "seen_count", "rejected_count", "first_run_id", "last_run_id",
                       "round_index", "first_seen_at", "last_seen_at"):
            self.assertIn(column, ddl, "建表文本缺列：%s" % column)
        self.assertIn("UNIQUE(owner_user_id, session_id, industry_pack_id, source_fingerprint)", ddl)
        self.assertTrue(any("qa_evidence_seen" in sql for sql in QA_INDEX_DDL))

    def test_fresh_db_gets_seen_table(self):
        connection = sqlite3.connect(":memory:")
        try:
            ensure_qa_tables(connection.cursor())
            ensure_qa_tables(connection.cursor())  # 幂等：重复跑不许抛
            self.assertIn("source_fingerprint", _columns(connection, "qa_evidence_seen"))
        finally:
            connection.close()

    def test_legacy_v6_db_is_upgraded_without_new_columns(self):
        """老库（v6）升级：只补建一张表，既有表列集与既有数据一个字不动。"""
        connection = sqlite3.connect(":memory:")
        try:
            ensure_qa_tables(connection.cursor())
            before = {table: _columns(connection, table)
                      for table in ("qa_evidence", "qa_runs", "qa_claim_evidence")}
            connection.execute("DROP TABLE qa_evidence_seen")
            connection.execute(
                "INSERT INTO qa_runs(id,contract_version,industry_pack_id,origin,mode,"
                "question_hash,status,created_at,updated_at)"
                " VALUES('old','unified-qa-v1','health','api','standard','h','completed','t','t')")

            ensure_qa_tables(connection.cursor())
            ensure_qa_tables(connection.cursor())  # 幂等

            self.assertIn("source_fingerprint", _columns(connection, "qa_evidence_seen"))
            for table, columns in before.items():
                self.assertEqual(_columns(connection, table), columns, "%s 的列集被改动了" % table)
            row = connection.execute("SELECT status FROM qa_runs WHERE id='old'").fetchone()
            self.assertEqual(row, ("completed",))
        finally:
            connection.close()


class SeenRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "phase02.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.store = QaStore(self.db)
        self.store.ensure_schema()

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def _record(self, status, fingerprint, scope=None, **overrides):
        record = {"source_fingerprint": fingerprint, "span_fingerprint": "span-" + fingerprint,
                  "evidence_ref": "article:1", "source_type": "article", "status": status}
        record.update(overrides)
        return self.store.record_seen_evidence(records=[record], run_id="run-1",
                                               round_index=0, **(scope or SCOPE))

    def test_rejected_evidence_keeps_its_identity(self):
        """MASTER_RULES 第 14 条：被拒证据仍属于 seen（旧实现只留计数不留身份）。"""
        summary = self._record("rejected", "fp-junk")
        self.assertEqual((summary["recorded"], summary["rejected"]), (1, 1))
        self.assertEqual(
            self.store.seen_evidence(source_fingerprints=["fp-junk"], **SCOPE), {"fp-junk": "rejected"})

    def test_scope_isolation_on_all_three_axes(self):
        self._record("rejected", "fp-1")
        for axis, value in (("owner_user_id", "u2"), ("session_id", "s2"),
                            ("industry_pack_id", "auto")):
            other = {**SCOPE, axis: value}
            self.assertEqual(
                self.store.seen_evidence(source_fingerprints=["fp-1"], **other), {},
                "%s 变化后仍读到了别的作用域的 seen 身份（串味）" % axis)

    def test_confirmed_wins_over_rejected_and_counts_accumulate(self):
        self._record("rejected", "fp-1")
        self._record("confirmed", "fp-1")
        self._record("rejected", "fp-1")
        self.assertEqual(
            self.store.seen_evidence(source_fingerprints=["fp-1"], **SCOPE), {"fp-1": "confirmed"})
        row = dict(self.db.connection.execute(
            "SELECT seen_count, rejected_count, first_run_id, last_run_id"
            " FROM qa_evidence_seen").fetchone())
        self.assertEqual(row["seen_count"], 3)
        self.assertEqual(row["rejected_count"], 2)
        self.assertEqual(row["first_run_id"], "run-1")
        self.assertEqual(self.db.connection.execute(
            "SELECT COUNT(*) FROM qa_evidence_seen").fetchone()[0], 1, "重复登记必须覆盖，不许插新行")

    def test_empty_fingerprint_is_skipped(self):
        summary = self.store.record_seen_evidence(
            records=[{"source_fingerprint": "", "status": "seen"}, {"status": "seen"}], **SCOPE)
        self.assertEqual((summary["recorded"], summary["skipped"]), (0, 2))

    def test_unknown_status_falls_back_to_seen(self):
        self.store.record_seen_evidence(
            records=[{"source_fingerprint": "fp-x", "status": "胡说"}], **SCOPE)
        self.assertEqual(
            self.store.seen_evidence(source_fingerprints=["fp-x"], **SCOPE), {"fp-x": "seen"})

    def test_lookup_returns_empty_without_keys(self):
        self.assertEqual(self.store.seen_evidence(source_fingerprints=[], **SCOPE), {})

    def test_store_error_is_swallowed_and_reported(self):
        """失败路径：存储层异常不许冒泡（留痕绝不拖累问答）。"""
        with mock.patch.object(self.store, "ensure_schema", side_effect=RuntimeError("boom")):
            summary = self.store.record_seen_evidence(
                records=[{"source_fingerprint": "fp-1", "status": "confirmed"}], **SCOPE)
        self.assertEqual(summary["recorded"], 0)
        self.assertIn("boom", summary["error"])

    def test_forget_requires_scope_unless_explicit(self):
        self._record("rejected", "fp-1")
        self._record("rejected", "fp-2")
        self.assertEqual(self.store.forget_seen_evidence(), 0, "无作用域键时不许全局清空")
        self.assertEqual(
            self.store.forget_seen_evidence(owner_user_id="u1", source_fingerprints=["fp-1"]), 1)
        self.assertEqual(self.store.seen_evidence(source_fingerprints=["fp-1"], **SCOPE), {})
        self.assertEqual(self.store.seen_evidence(source_fingerprints=["fp-2"], **SCOPE),
                         {"fp-2": "rejected"})
        self.assertEqual(self.store.forget_seen_evidence(session_id="s1"), 1)
        self.assertEqual(self.store.seen_evidence(source_fingerprints=["fp-2"], **SCOPE), {})


class SeenAdapterTests(unittest.TestCase):
    """qa_evidence 的适配层：把证据转身份、登记、查询、过滤。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "phase02-adapter.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.store = QaStore(self.db)
        self.store.ensure_schema()

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def test_seen_records_prefers_annotated_fingerprint(self):
        annotated = evidence_layer.annotate_evidence(_item())
        rows = evidence_layer.seen_records([annotated], status="confirmed")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_fingerprint"],
                         annotated["metadata"]["evidence_layer"]["source_fingerprint"])
        self.assertEqual(rows[0]["span_fingerprint"],
                         annotated["metadata"]["evidence_layer"]["fingerprint"])
        # 未标注的证据也能算身份（现算），且同一来源得到同一个键
        raw_rows = evidence_layer.seen_records([_item()], status="seen")
        self.assertEqual(raw_rows[0]["source_fingerprint"], rows[0]["source_fingerprint"])

    def test_seen_records_deduplicates_and_skips_junk(self):
        rows = evidence_layer.seen_records([_item(), _item(), None, "x"], status="seen")
        self.assertEqual(len(rows), 1)

    def test_record_seen_round_trip_and_lookup(self):
        accepted = [evidence_layer.annotate_evidence(_item("article:1"))]
        rejected = [_item("article:2"), _item("article:3")]
        summary = evidence_layer.record_seen(self.store, scope=SCOPE, accepted=accepted,
                                             rejected=rejected, run_id="run-1")
        self.assertEqual((summary["confirmed"], summary["rejected"]), (1, 2))
        seen = evidence_layer.load_seen(self.store, scope=SCOPE, items=accepted + rejected)
        self.assertEqual(len(seen), 3)
        self.assertEqual(
            seen[evidence_layer.source_fingerprint(_item("article:2"))], "rejected")

    def test_record_seen_never_raises(self):
        """失败路径：store 完全不配合（没有方法）也必须安全返回。"""
        summary = evidence_layer.record_seen(object(), scope=SCOPE, accepted=[_item()])
        self.assertTrue(summary["error"])
        self.assertEqual(summary["recorded"], 0)
        self.assertEqual(evidence_layer.load_seen(object(), scope=SCOPE, items=[_item()]), {})

    def test_filter_seen_modes(self):
        seen = {"fp-junk": "rejected", "fp-good": "confirmed"}
        junk = {"evidence_ref": "article:9", "metadata": {"evidence_layer": {
            "source_fingerprint": "fp-junk"}}}
        good = {"evidence_ref": "article:8", "metadata": {"evidence_layer": {
            "source_fingerprint": "fp-good"}}}
        fresh = {"evidence_ref": "article:7", "metadata": {"evidence_layer": {
            "source_fingerprint": "fp-new"}}}

        kept, audit = evidence_layer.filter_seen([junk, good, fresh], seen, mode="rejected")
        self.assertEqual([item["evidence_ref"] for item in kept], ["article:8", "article:7"])
        self.assertEqual(audit["dropped_count"], 1)
        self.assertEqual(audit["dropped"][0]["previous_status"], "rejected")
        self.assertTrue(kept[0]["metadata"]["evidence_layer"]["repeat"])
        self.assertEqual(kept[0]["metadata"]["evidence_layer"]["previous_status"], "confirmed")

        kept_all, audit_all = evidence_layer.filter_seen([junk, good, fresh], seen, mode="all")
        self.assertEqual([item["evidence_ref"] for item in kept_all], ["article:7"])
        self.assertEqual(audit_all["dropped_count"], 2)

        kept_off, _audit_off = evidence_layer.filter_seen([junk, good, fresh], seen, mode="off")
        self.assertEqual(len(kept_off), 3)

    def test_filter_seen_tolerates_unknown_mode_and_empty_input(self):
        kept, audit = evidence_layer.filter_seen([_item()], {}, mode="胡说")
        self.assertEqual(audit["mode"], "rejected")
        self.assertEqual(len(kept), 1)
        self.assertEqual(evidence_layer.filter_seen([None, "x"], {})[0], [])

    def test_seen_dedupe_mode_defaults_to_rejected(self):
        os.environ.pop("QA_EVIDENCE_SEEN_DEDUPE", None)
        self.assertEqual(evidence_layer.seen_dedupe_mode(), "rejected")
        os.environ["QA_EVIDENCE_SEEN_DEDUPE"] = "all"
        try:
            self.assertEqual(evidence_layer.seen_dedupe_mode(), "all")
        finally:
            os.environ.pop("QA_EVIDENCE_SEEN_DEDUPE", None)


class SeenPruneTests(unittest.TestCase):
    """Phase 02（缺口 2）：seen 身份的 TTL / 清理策略。

    钉住四件事：
      1. 过期行被删（按 `last_seen_at`，老行缺该值时退到 `first_seen_at`）、未过期行保留；
      2. 只按时间删，不按作用域删——别的作用域的**新鲜**行不会因为本作用域有过期行而被牵连；
      3. 开关 `QA_EVIDENCE_SEEN_PRUNE_ENABLED` **默认关**：关着时一条都不删；
      4. 保留期 `QA_EVIDENCE_SEEN_TTL_DAYS` 默认 30、最小 1；失败路径只回报 error 不抛。
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "phase02-prune.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self._env_backup = {key: os.environ.get(key) for key in (
            "QA_EVIDENCE_SEEN_PRUNE_ENABLED", "QA_EVIDENCE_SEEN_TTL_DAYS")}
        for key in self._env_backup:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def _record(self, fingerprint, scope=None, *, status="rejected"):
        return self.store.record_seen_evidence(
            records=[{"source_fingerprint": fingerprint, "span_fingerprint": "span-" + fingerprint,
                      "evidence_ref": "article:1", "source_type": "article", "status": status}],
            run_id="run-1", round_index=0, **(scope or SCOPE))

    def _age(self, fingerprint, days, *, column="last_seen_at"):
        """把某行的见证时间改到 N 天前（格式与 _now() 一致，直接字符串比较）。"""
        moment = (datetime.now(timezone.utc) - timedelta(days=days)) \
            .isoformat(timespec="milliseconds").replace("+00:00", "Z")
        with self.db.lock:
            self.db.connection.execute(
                "UPDATE qa_evidence_seen SET %s=? WHERE source_fingerprint=?" % column,
                (moment, fingerprint))
            self.db.connection.commit()

    def _keys(self):
        return {str(row[0]) for row in self.db.connection.execute(
            "SELECT source_fingerprint FROM qa_evidence_seen").fetchall()}

    def test_expired_rows_are_deleted_and_fresh_rows_kept(self):
        self._record("fp-old")
        self._record("fp-new")
        self._age("fp-old", 31)
        self.assertEqual(self.store.prune_seen_evidence(older_than_days=30), 1)
        self.assertEqual(self._keys(), {"fp-new"})

    def test_prune_is_age_based_across_scopes(self):
        """只按时间删：别的作用域的新鲜行不会被本作用域的过期行牵连。"""
        other = {"owner_user_id": "u2", "session_id": "s2", "industry_pack_id": "auto"}
        self._record("fp-old-mine")
        self._record("fp-fresh-mine")
        self._record("fp-fresh-other", other)
        self._age("fp-old-mine", 10)
        self.assertEqual(self.store.prune_seen_evidence(older_than_days=7), 1)
        self.assertEqual(self._keys(), {"fp-fresh-mine", "fp-fresh-other"})

    def test_first_seen_at_is_the_fallback_for_missing_last_seen_at(self):
        self._record("fp-legacy")
        self._age("fp-legacy", 40, column="first_seen_at")
        with self.db.lock:
            self.db.connection.execute(
                "UPDATE qa_evidence_seen SET last_seen_at='' WHERE source_fingerprint='fp-legacy'")
            self.db.connection.commit()
        self.assertEqual(self.store.prune_seen_evidence(older_than_days=30), 1)
        self.assertEqual(self._keys(), set())

    def test_explicit_days_win_and_minimum_is_one_day(self):
        self._record("fp-recent")
        self.assertEqual(self.store.prune_seen_evidence(older_than_days=30), 0)
        # 负数/0 一律按最小 1 天处理：不许出现"传 0 就把全表清空"的事故
        self.assertEqual(self.store.prune_seen_evidence(older_than_days=0), 0)
        self._age("fp-recent", 2)
        self.assertEqual(self.store.prune_seen_evidence(older_than_days=0), 1)

    def test_ttl_default_and_floor_from_env(self):
        self.assertEqual(evidence_layer.seen_ttl_days(), 30)
        os.environ["QA_EVIDENCE_SEEN_TTL_DAYS"] = "7"
        self.assertEqual(evidence_layer.seen_ttl_days(), 7)
        os.environ["QA_EVIDENCE_SEEN_TTL_DAYS"] = "0"
        self.assertEqual(evidence_layer.seen_ttl_days(), 1, "最小 1 天")
        os.environ["QA_EVIDENCE_SEEN_TTL_DAYS"] = "胡说"
        self.assertEqual(evidence_layer.seen_ttl_days(), 30, "坏值退回默认值")

    def test_switch_is_off_by_default_and_deletes_nothing(self):
        """开关默认关：这是新表，先观察一段时间再开（关着时维护入口照旧安全调用）。"""
        self.assertFalse(evidence_layer.seen_prune_enabled())
        self._record("fp-old")
        self._age("fp-old", 90)
        summary = evidence_layer.prune_seen_evidence(self.store)
        self.assertEqual(summary, {"enabled": False, "deleted": 0, "ttl_days": 30})
        self.assertEqual(self._keys(), {"fp-old"}, "开关关着时一条都不许删")

        os.environ["QA_EVIDENCE_SEEN_PRUNE_ENABLED"] = "1"
        os.environ["QA_EVIDENCE_SEEN_TTL_DAYS"] = "30"
        self.assertTrue(evidence_layer.seen_prune_enabled())
        summary = evidence_layer.prune_seen_evidence(self.store)
        self.assertEqual((summary["enabled"], summary["deleted"], summary["ttl_days"]), (True, 1, 30))
        self.assertEqual(self._keys(), set())

    def test_prune_never_raises_on_broken_store(self):
        os.environ["QA_EVIDENCE_SEEN_PRUNE_ENABLED"] = "1"
        summary = evidence_layer.prune_seen_evidence(object())
        self.assertEqual(summary["deleted"], 0)
        self.assertTrue(summary["error"])


if __name__ == "__main__":
    unittest.main()

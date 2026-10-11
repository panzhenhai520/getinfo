#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 01 · F-5 / F-7 / F-8 的落库验收。

钉住四件事（对应 tracking/ACCEPTANCE_MATRIX.md 的 P01-02 / P01-03 / P01-04）：
  1. 版本四元组：`qa_runs` 有 corpus_version/model_version/prompt_version/config_hash，
     且 `create_run` 真的写进去（同一份配置得到同一个 config_hash）；
  2. SearchTrace 字段：`qa_reasoning_traces` 有 gap_id/route/results/accepted/rejected/
     new_claims/resolved_gap，新旧签名都能写、新字段真的落库，且能过 `qa_graph_contracts`
     的 search_trace 校验；
  3. node-run 载体：`qa_stage_runs` 有 node_id/node_kind/parent_node_id/round_index，
     `record_stage` 不传时 node_id 默认 == stage、node_kind 默认 execution；
  4. 老库升级只走 ADD COLUMN（DEFAULT 兜底），且新建库路径的列与升级清单**逐字一致**。
"""
import os
import sqlite3
import sys
import tempfile
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from qa_graph_contracts import QA_RETRIEVAL_ROUTES, validate as validate_contract  # noqa: E402
from qa_schema import (  # noqa: E402
    QA_ADDED_COLUMNS_V6, QA_SCHEMA_VERSION, QA_TABLE_DDL, ensure_qa_tables,
)
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402


def _request(question="医保新规对民营医院有什么影响？", **overrides):
    payload = {
        "session_id": "s-1", "question": question, "industry_pack_id": "health",
        "origin": "api", "mode": "standard", "draft_provider": "local",
    }
    payload.update(overrides)
    return payload


def _columns(connection, table):
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _ddl_of(table):
    for statement in QA_TABLE_DDL:
        if f"CREATE TABLE IF NOT EXISTS {table} " in statement:
            return statement
    raise AssertionError("建表文本里找不到表 %s" % table)


def _new_columns(table):
    return [(column, definition) for name, column, definition in QA_ADDED_COLUMNS_V6 if name == table]


def _legacy_v5_ddl(table):
    """升级前的 v5 建表文本：把当前 DDL 里 Phase 01 新加的那些行删掉。"""
    import re

    ddl = _ddl_of(table)
    for column, _definition in _new_columns(table):
        ddl = re.sub(r"(?m)^\s*%s\s+[^\n]*\n" % re.escape(column), "", ddl)
    return ddl


class Phase01SchemaVersionTests(unittest.TestCase):
    def test_schema_version_is_v7(self):
        """库表结构版本锚点：v5 → v6（Phase 01）→ v7（Phase 02 新增 qa_evidence_seen 表）
        → **v8**（Phase 09 新增八张 memory_* 表，见 D-033）→ **v9**（Phase 10 新增
  memory_validation / memory_contradiction 两张表，见 D-037）。

        这里刻意仍然钉死字面量（而不是"大于等于"）：版本号漂移必须当场红。
        Phase 02/09 都只加了**表**、没加列，Phase 01 的升级清单由下面的
        `test_fresh_ddl_and_upgrade_list_agree` 逐条守住；v7 的表/列细节见
        `tests/test_qa_phase02_seen.py`、v8 的八张记忆表见 `tests/test_qa_phase09_schema.py`、
v9 的两张复验/矛盾表见 `tests/test_qa_phase10_schema.py`。
        """
        self.assertEqual(QA_SCHEMA_VERSION, "unified-qa-schema-v9")

    def test_fresh_ddl_and_upgrade_list_agree(self):
        """新建库路径（建表文本）与老库升级路径（ADD COLUMN 清单）必须逐字一致。

        两条路径漂移的后果很隐蔽：老库有列、新库没有（或反过来），
        只有换机器/重建库的时候才炸。
        """
        for table_name, column_name, definition in QA_ADDED_COLUMNS_V6:
            ddl = _ddl_of(table_name)
            self.assertIn("%s %s" % (column_name, definition), ddl,
                          "%s.%s 的建表文本与升级清单不一致" % (table_name, column_name))

    def test_fresh_db_has_new_columns_with_defaults(self):
        """全新库：三张新列齐备，且不写值时取到空串/0（老调用方零改动即安全）。"""
        connection = sqlite3.connect(":memory:")
        try:
            ensure_qa_tables(connection.cursor())
            ensure_qa_tables(connection.cursor())  # 幂等：重复跑不许抛
            runs = _columns(connection, "qa_runs")
            traces = _columns(connection, "qa_reasoning_traces")
            stages = _columns(connection, "qa_stage_runs")
            for column in ("corpus_version", "model_version", "prompt_version", "config_hash"):
                self.assertIn(column, runs)
            for column in ("gap_id", "route", "results", "accepted", "rejected",
                           "new_claims", "resolved_gap"):
                self.assertIn(column, traces)
            for column in ("node_id", "node_kind", "parent_node_id", "round_index"):
                self.assertIn(column, stages)

            connection.execute(
                "INSERT INTO qa_stage_runs(run_id,stage,attempt,status,started_at)"
                " VALUES('r1','plan',1,'running','2026-01-01T00:00:00Z')")
            connection.execute(
                "INSERT INTO qa_reasoning_traces(run_id,hop_index,created_at,updated_at)"
                " VALUES('r1',0,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
            connection.execute(
                "INSERT INTO qa_runs(id,contract_version,industry_pack_id,origin,mode,"
                "question_hash,status,created_at,updated_at)"
                " VALUES('r1','unified-qa-v1','health','api','standard','h','queued','t','t')")
            stage = connection.execute(
                "SELECT node_id,node_kind,parent_node_id,round_index FROM qa_stage_runs").fetchone()
            self.assertEqual(stage, ("", "", "", 0))
            trace = connection.execute(
                "SELECT gap_id,route,results,accepted,rejected,new_claims,resolved_gap"
                " FROM qa_reasoning_traces").fetchone()
            self.assertEqual(trace, ("", "", 0, 0, 0, 0, 0))
            run = connection.execute(
                "SELECT corpus_version,model_version,prompt_version,config_hash FROM qa_runs").fetchone()
            self.assertEqual(run, ("", "", "", ""))
        finally:
            connection.close()

    def test_legacy_v5_db_is_upgraded_by_add_column(self):
        """老库路径：只 ADD COLUMN，既有列与既有数据一字不动。

        老库形状由**当前建表文本删掉 Phase 01 新列**得到（= 升级前的 v5 表），
        这样既真实又不会因为手抄建表文本而跟实现漂移。
        """
        connection = sqlite3.connect(":memory:")
        try:
            for table in ("qa_runs", "qa_reasoning_traces", "qa_stage_runs"):
                connection.execute(_legacy_v5_ddl(table))
            self.assertFalse({"corpus_version", "config_hash"} & _columns(connection, "qa_runs"))
            self.assertFalse({"node_id", "node_kind"} & _columns(connection, "qa_stage_runs"))
            connection.execute(
                "INSERT INTO qa_runs(id,contract_version,industry_pack_id,origin,mode,"
                "question_hash,status,created_at,updated_at)"
                " VALUES('old','unified-qa-v1','health','api','standard','h','completed','t','t')")

            ensure_qa_tables(connection.cursor())
            ensure_qa_tables(connection.cursor())  # 幂等：重复升级不许抛

            self.assertTrue(set(c for c, _ in _new_columns("qa_runs")) <= _columns(connection, "qa_runs"))
            self.assertTrue(set(c for c, _ in _new_columns("qa_reasoning_traces")) <=
                            _columns(connection, "qa_reasoning_traces"))
            self.assertTrue(set(c for c, _ in _new_columns("qa_stage_runs")) <=
                            _columns(connection, "qa_stage_runs"))
            # 老数据还在，老列语义没变，新列一律取默认值（可回滚：留着不读即可）
            row = connection.execute(
                "SELECT status,corpus_version,config_hash FROM qa_runs WHERE id='old'").fetchone()
            self.assertEqual(row, ("completed", "", ""))
        finally:
            connection.close()


class Phase01StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "phase01.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self._saved_rounds = getattr(config, "QA_RECURSION_MAX_ROUNDS", 1)
        config.QA_RECURSION_MAX_ROUNDS = 0

    def tearDown(self):
        config.QA_RECURSION_MAX_ROUNDS = self._saved_rounds
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    # ── F-5 版本四元组 ────────────────────────────────────────────────────────
    def test_create_run_writes_version_quadruple(self):
        run = self.store.create_run(
            _request(), owner_user_id="u1", idempotency_key="idem-1",
            corpus_version="corpus-abc", model_version="qwen3-8b",
            prompt_version="qa-research-notes-v3", config_hash="cfg-1234",
        )
        saved = self.store.get_run(run["id"], owner_user_id="u1")
        self.assertEqual(saved["corpus_version"], "corpus-abc")
        self.assertEqual(saved["model_version"], "qwen3-8b")
        self.assertEqual(saved["prompt_version"], "qa-research-notes-v3")
        self.assertEqual(saved["config_hash"], "cfg-1234")

    def test_create_run_derives_versions_and_keeps_hash_stable(self):
        """不传版本参数（老调用方写法）也必须能建 run，并尽力推导出稳定的版本值。"""
        first = self.store.create_run(_request(), owner_user_id="u1", idempotency_key="idem-a")
        second = self.store.create_run(_request(), owner_user_id="u1", idempotency_key="idem-b")
        self.assertEqual(first["model_version"], "")
        self.assertEqual(len(first["config_hash"]), 16)
        # 同一份配置 → 同一个 config_hash（时间戳/run_id/问题文本都不参与）
        self.assertEqual(first["config_hash"], second["config_hash"])
        self.assertEqual(first["prompt_version"], second["prompt_version"])
        self.assertEqual(first["corpus_version"], second["corpus_version"])
        # 语料指纹与检索缓存 kb_version 同源：拿不到库时是 "unknown"，拿到就是 24 位哈希
        self.assertTrue(first["corpus_version"] in ("unknown", "") or len(first["corpus_version"]) == 24)

    def test_create_run_idempotency_still_works(self):
        """既有幂等语义不许被新列破坏：同 (owner, idempotency_key) 只建一次。"""
        first = self.store.create_run(_request(), owner_user_id="u1", idempotency_key="same")
        second = self.store.create_run(_request(question="换了个问法"), owner_user_id="u1",
                                       idempotency_key="same")
        self.assertTrue(first["_created"])
        self.assertFalse(second["_created"])
        self.assertEqual(first["id"], second["id"])

    # ── F-7 SearchTrace 字段 ─────────────────────────────────────────────────
    def test_reasoning_trace_old_signature_keeps_defaults(self):
        """老签名（不传新字段）必须照旧可写，新列落默认值。"""
        self.store.record_reasoning_trace("run-1", hop_index=0, sub_query_id="h1",
                                          sub_query="A 的事实", status="ok")
        row = self.store.reasoning_traces("run-1")[0]
        self.assertEqual(row["gap_id"], "")
        self.assertEqual(row["route"], "")
        self.assertEqual((row["results"], row["accepted"], row["rejected"]), (0, 0, 0))
        self.assertEqual((row["new_claims"], row["resolved_gap"]), (0, 0))

    def test_reasoning_trace_new_fields_round_trip_and_are_idempotent(self):
        self.store.record_reasoning_trace(
            "run-2", hop_index=0, sub_query_id="h1", sub_query="A 的事实", status="ok",
            gap_id="G8", route="graph", results=12, accepted=2, rejected=10,
            new_claims=1, resolved_gap=1, round_index=1, latency_ms=480,
        )
        row = self.store.reasoning_traces("run-2")[0]
        self.assertEqual(row["gap_id"], "G8")
        self.assertEqual(row["route"], "graph")
        self.assertEqual((row["results"], row["accepted"], row["rejected"]), (12, 2, 10))
        self.assertEqual((row["new_claims"], row["resolved_gap"]), (1, 1))
        self.assertIn(row["route"], QA_RETRIEVAL_ROUTES)
        # 留痕行能直接当成 SearchTrace 契约载荷（缺字段/越界枚举会被 validate 拦）
        ok, detail = validate_contract("search_trace", {
            "hop_index": row["hop_index"], "round_index": row["round_index"],
            "sub_query": row["sub_query"], "route": row["route"],
            "results": row["results"], "accepted": row["accepted"],
            "rejected": row["rejected"], "new_claims": row["new_claims"],
            "resolved_gap": row["resolved_gap"], "gap_id": row["gap_id"],
            "latency_ms": row["latency_ms"],
        })
        self.assertTrue(ok, detail)

        # 幂等：同 (run, round, hop) 再写一次是覆盖，新字段也跟着更新
        self.store.record_reasoning_trace(
            "run-2", hop_index=0, sub_query_id="h1", sub_query="A 的事实（改）",
            status="empty", route="web", results=3, accepted=0, rejected=3, round_index=1,
        )
        traces = self.store.reasoning_traces("run-2")
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0]["sub_query"], "A 的事实（改）")
        self.assertEqual(traces[0]["route"], "web")
        self.assertEqual((traces[0]["results"], traces[0]["accepted"], traces[0]["rejected"]),
                         (3, 0, 3))
        self.assertEqual(traces[0]["gap_id"], "")  # 覆盖时未传 → 回到默认值

    # ── F-8 node-run 载体 ────────────────────────────────────────────────────
    def test_record_stage_autofills_node_run(self):
        self.store.record_stage("run-3", "level1_retrieval", status="running")
        row = [item for item in self.store.stage_runs("run-3") if item["stage"] == "level1_retrieval"][0]
        self.assertEqual(row["node_id"], "level1_retrieval")
        self.assertEqual(row["node_kind"], "execution")
        self.assertEqual(row["parent_node_id"], "")
        self.assertEqual(row["round_index"], 0)

    def test_record_stage_explicit_node_wins_and_survives_status_updates(self):
        """阶段 05 传真 node_id 后，后续只更新状态的调用不许把它打回 stage。"""
        self.store.record_stage("run-4", "level2_research", status="running",
                                node_id="evidence_hunter", node_kind="execution",
                                parent_node_id="plan_root", round_index=2)
        self.store.record_stage("run-4", "level2_research", status="completed")
        row = [item for item in self.store.stage_runs("run-4") if item["stage"] == "level2_research"][0]
        self.assertEqual(row["node_id"], "evidence_hunter")
        self.assertEqual(row["parent_node_id"], "plan_root")
        self.assertEqual(row["round_index"], 2)
        self.assertEqual(row["status"], "completed")


class Phase01MultiHopTraceTests(unittest.TestCase):
    """F-7 的填充点：`_run_multi_hop` 的留痕回调要带上 route/results/accepted/rejected。"""

    def setUp(self):
        self._saved_budget = getattr(config, "QA_MULTI_HOP_BUDGET_SECONDS", 25)
        self._saved_rounds = getattr(config, "QA_RECURSION_MAX_ROUNDS", 1)
        config.QA_MULTI_HOP_BUDGET_SECONDS = 25
        config.QA_RECURSION_MAX_ROUNDS = 0

    def tearDown(self):
        config.QA_MULTI_HOP_BUDGET_SECONDS = self._saved_budget
        config.QA_RECURSION_MAX_ROUNDS = self._saved_rounds

    @staticmethod
    def _evidence(ref, method, source_type="article"):
        return {"evidence_ref": ref, "source_type": source_type, "title": ref,
                "content_excerpt": "证据正文", "source_url": "https://example.com/x",
                "retrieval_method": method, "metadata": {}}

    def test_recorder_carries_route_and_candidate_counts(self):
        from qa_pipeline import _run_multi_hop

        class _Retriever:
            def retrieve(self, plan, **kwargs):
                question = str(plan.get("question") or "")
                if "民营医院压力" in question:
                    return {"evidence": [self_items[0], self_items[1]],
                            "stats": {"eligible": 4, "adopted": 2}}
                return {"evidence": [], "stats": {}}

        self_items = [self._evidence("page:9", "page_context", "page_context"),
                      self._evidence("article:7", "keyword")]
        first_local = {"evidence": [self._evidence("article:1", "keyword")],
                       "stats": {"eligible": 6, "adopted": 1}}
        plan = {
            "question": "2026年医保新规对民营医院有什么影响？",
            "entities": ["医保"],
            "decomposition": {"is_multi_hop": True, "hops": [
                {"id": "h1", "question": "2026年医保新规", "depends_on": []},
                {"id": "h2", "question": "民营医院压力", "depends_on": ["h1"]},
            ]},
        }
        entries = []
        merged, receipts = _run_multi_hop(
            _Retriever(), plan, first_local, {}, pack_id="health", limit=8,
            trace_recorder=lambda entry: entries.append(dict(entry)),
        )
        self.assertEqual([entry["hop_index"] for entry in entries], [0, 1])
        self.assertEqual(entries[0]["route"], "keyword")
        self.assertEqual((entries[0]["results"], entries[0]["accepted"], entries[0]["rejected"]),
                         (6, 1, 5))
        # 两条证据里 page_context 与 keyword 各一条 → 同票按首次出现顺序取胜出者
        self.assertEqual(entries[1]["route"], "page_context")
        self.assertEqual((entries[1]["results"], entries[1]["accepted"], entries[1]["rejected"]),
                         (4, 2, 2))
        for entry in entries:
            self.assertIn(entry["route"], QA_RETRIEVAL_ROUTES)
            self.assertEqual(entry["rejected"], entry["results"] - entry["accepted"])
        # 既有返回结构不许被新字段污染：回执键集与升级前逐字相同
        self.assertEqual(set(receipts[0]), {
            "hop_id", "hop_index", "question", "depends_on", "purpose", "evidence",
            "status", "carry_terms", "latency_ms"})
        self.assertEqual(set(receipts[1]), {
            "hop_id", "hop_index", "question", "depends_on", "purpose", "evidence", "added",
            "status", "carry_terms", "latency_ms"})
        self.assertNotIn("route", receipts[0])
        self.assertEqual(len(merged["evidence"]), 3)


if __name__ == "__main__":
    unittest.main()

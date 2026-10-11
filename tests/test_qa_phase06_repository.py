#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 06 · P06-01 证据图仓储/API（隔离临时 sqlite 上真跑）。

钉住的东西：
  1. **写库复用既有 `persist_reasoning_graph`**：claim/边/冲突分别落 `qa_claims` /
     `qa_claim_evidence` / `qa_conflicts`，**零新表、零迁移**；
  2. **读回对称**：`load_reasoning_graph()` → `graph_from_rows()` 能重建出同口径的结论图，
     claim 的核验 pairs 从 `payload_json` 里原样回来（关系可复算的前提）；
  3. **只读 API**：`build()/coverage()/relation_distribution()/snapshot()` 不写库、
     载荷过 `evidence_graph` 契约、有上限截断；
  4. **回放视图**：`load_persisted()` 读 `qa_stage_runs.details.result.evidence_graph`
     （编排层把阶段输出整体落库，所以 P06 的回执是免费持久化的）；
  5. 失败路径：脏 `payload_json` / 不存在的 run / 缺 store 都不许抛异常打断调用方。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_evidence_graph as eg  # noqa: E402
import qa_schema  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402
from qa_phase06_fixtures import claim_node, evidence, graph_of  # noqa: E402
from qa_storage import QaStore  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

QUESTION = "A股10月9日大涨的原因是什么？"


class _Base(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "phase06.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.assertEqual(self.db.backend, "sqlite", "测试必须跑在隔离 sqlite 上")
        self.store = QaStore(self.db)
        self.store.ensure_schema()
        self.run = self.store.create_run(
            {"industry_pack_id": "auto", "question": QUESTION, "mode": "standard"},
            owner_user_id="p06", idempotency_key="phase06-repo")
        self.run_id = str(self.run["id"])
        self.repo = eg.EvidenceGraphRepository(self.store)

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def _graph(self):
        left = claim_node("c1", text="A股10月9日大涨3.84%", refs=["e1", "e2"],
                          status="confirmed", authority=100, pairs=[
                              {"evidence_ref": "e1", "verdict": "SUPPORTED", "score": 0.7},
                              {"evidence_ref": "e2", "verdict": "REFUTED", "score": 0.3}])
        right = claim_node("c2", text="A股10月9日下跌", refs=["e3"], status="unverified",
                           authority=20, pairs=[
                               {"evidence_ref": "e3", "verdict": "UNVERIFIED", "score": 0.0}])
        graph = graph_of([left, right],
                         [evidence("e1"), evidence("e2", relation="contradicts"),
                          evidence("e3")])
        self._seed_evidence(graph["evidence"])
        return graph

    def _row(self, sql, params=()):
        return self.db.connection.execute(sql, params).fetchone()

    def _seed_evidence(self, items):
        """按生产写路径（`persist_level1_result` 里的那条 INSERT）落 qa_evidence 行。

        Phase 06 **不负责**写证据表（那是 Phase 02 的写路径），仓储只需要能读回它。
        """
        for item in items:
            self.db.connection.execute(
                "INSERT OR REPLACE INTO qa_evidence(run_id,evidence_ref,source_type,article_id,"
                "source_url,source_title,published_at,authority_level,content_hash,payload_json,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (self.run_id, item["evidence_ref"], item["source_type"], item.get("article_id"),
                 item.get("source_url"), item.get("title"), item.get("published_at"),
                 item.get("authority_level"), "",
                 json.dumps(item, ensure_ascii=False), "2026-10-10T00:00:00Z"))
        self.db.connection.commit()


class PersistAndLoadTests(_Base):
    def test_save_uses_the_existing_storage_and_no_new_tables(self):
        graph = self._graph()
        written = self.repo.save(self.run_id, graph)
        self.assertEqual(written, 2)
        self.assertEqual(int(self._row("SELECT count(*) FROM qa_claims WHERE run_id=?",
                                       (self.run_id,))[0]), 2)
        self.assertEqual(int(self._row("SELECT count(*) FROM qa_claim_evidence WHERE run_id=?",
                                       (self.run_id,))[0]), 3)
        blob = " ".join(qa_schema.QA_TABLE_DDL)
        for marker in ("qa_evidence_graph", "qa_claim_relations", "qa_contradiction"):
            self.assertNotIn(marker, blob, "Phase 06 不许新建表")
        self.assertEqual(qa_schema.QA_SCHEMA_VERSION, "unified-qa-schema-v9", "Phase 06 自身零迁移；v9 由 Phase 09 的八张 memory_* 表 + Phase 10 的两张复验/矛盾表引入")

    def test_load_rebuilds_the_same_graph_shape(self):
        graph = self._graph()
        self.repo.save(self.run_id, graph)
        rebuilt = self.repo.load(self.run_id)
        self.assertEqual(len(rebuilt["claims"]), 2)
        by_id = {node["canonical_id"]: node for node in rebuilt["claims"]}
        self.assertEqual(set(by_id), {"c1", "c2"})
        self.assertEqual(by_id["c1"]["claim"]["evidence_refs"], ["e1", "e2"],
                         "引用集从库里的边重建")
        pairs = {pair["evidence_ref"]: pair["verdict"]
                 for pair in by_id["c1"]["verification"]["pairs"]}
        self.assertEqual(pairs, {"e1": "SUPPORTED", "e2": "REFUTED"},
                         "核验结论必须从 payload_json 原样读回（否则关系无法复算）")
        self.assertEqual(by_id["c1"]["claim"]["verification_status"], "confirmed")
        self.assertEqual(len(rebuilt["evidence"]), 3)

    def test_rebuild_keeps_the_relation_derivation_stable(self):
        """写库再读回后重算，关系分布与 coverage 必须与写之前逐字一致（幂等可复算）。"""
        graph = self._graph()
        before = eg.build_layer(graph)["stats"]["relation_distribution"]
        self.repo.save(self.run_id, graph)
        layer = self.repo.build(self.run_id)
        self.assertEqual(layer["stats"]["relation_distribution"], before)
        self.assertEqual(layer["coverage"]["total_claims"], 2)
        self.assertAlmostEqual(layer["coverage"]["claim_coverage"], 0.5)

    def test_conflicts_round_trip_and_keep_the_frozen_keys(self):
        graph = self._graph()
        graph["conflicts"] = [{
            "conflict_id": "conflict:abc", "subject": "A股涨跌", "conflict_type": "real_conflict",
            "claim_ids": ["c1", "c2"], "evidence_refs": ["e1", "e3"],
            "resolution": "unresolved", "rationale": "旧口径", "rule_version": "qa-adjudication-v1"}]
        self.repo.save(self.run_id, graph)
        rebuilt = self.repo.load(self.run_id)
        self.assertEqual(len(rebuilt["conflicts"]), 1)
        self.assertEqual(set(rebuilt["conflicts"][0].keys()),
                         {"conflict_id", "subject", "conflict_type", "claim_ids",
                          "evidence_refs", "resolution", "rationale", "rule_version"})


class ReadOnlyApiTests(_Base):
    def setUp(self):
        super().setUp()
        self.repo.save(self.run_id, self._graph())
        self.before = self._counts()

    def _counts(self):
        return tuple(int(self._row("SELECT count(*) FROM %s" % table)[0])
                     for table in ("qa_claims", "qa_claim_evidence", "qa_conflicts", "qa_stage_runs"))

    def test_build_coverage_and_distribution_do_not_write(self):
        layer = self.repo.build(self.run_id)
        self.assertEqual(self.repo.coverage(self.run_id)["total_claims"], 2)
        self.assertTrue(self.repo.relation_distribution(self.run_id))
        self.assertEqual(layer["graph_version"], eg.__dict__["EVIDENCE_GRAPH_VERSION"])
        self.assertEqual(self._counts(), self.before, "只读 API 不许写库")

    def test_snapshot_is_a_bounded_public_view(self):
        snapshot = self.repo.snapshot(self.run_id, limit=1)
        self.assertEqual(snapshot["run_id"], self.run_id)
        self.assertEqual(len(snapshot["claims"]), 1)
        self.assertEqual(snapshot["truncated"]["claims"], 1)
        self.assertIn("coverage", snapshot)
        self.assertIn("stats", snapshot)
        self.assertLessEqual(len(snapshot["edges"]), 4)
        self.assertIn("graph_version", snapshot)

    def test_snapshot_prefers_the_persisted_layer(self):
        """编排层把阶段输出整体落库 → `load_persisted` 必须能读回当时那份图。"""
        persisted = dict(self.repo.build(self.run_id), run_id=self.run_id)
        persisted["stats"] = dict(persisted["stats"], marker="persisted")
        self.store.record_stage(self.run_id, "conflict_review", status="completed",
                                details={"result": {"claims": [], "evidence_graph": persisted}})
        loaded = self.repo.load_persisted(self.run_id)
        self.assertEqual(loaded["stats"]["marker"], "persisted")
        self.assertEqual(self.repo.snapshot(self.run_id)["stats"]["marker"], "persisted")

    def test_snapshot_falls_back_to_rebuild_when_nothing_was_persisted(self):
        snapshot = self.repo.snapshot(self.run_id)
        self.assertNotIn("marker", snapshot["stats"])
        self.assertEqual(snapshot["coverage"]["total_claims"], 2)

    def test_api_payload_passes_the_contract(self):
        layer = self.repo.build(self.run_id)
        ok, note = validate("evidence_graph", layer)
        self.assertTrue(ok, note)
        for node in layer["nodes"]:
            self.assertTrue(validate("evidence_graph_node", dict(node))[0])
        for item in layer["contradictions"]:
            self.assertTrue(validate("contradiction_decision", dict(item))[0], item)


class FailurePathTests(_Base):
    def test_missing_store_raises_a_clear_error(self):
        with self.assertRaises(ValueError):
            eg.EvidenceGraphRepository().load("whatever")

    def test_unknown_run_is_empty_not_an_exception(self):
        layer = eg.EvidenceGraphRepository(self.store).build("does-not-exist")
        self.assertEqual(layer["coverage"]["total_claims"], 0)
        self.assertEqual(layer["nodes"], [])

    def test_broken_payload_json_is_tolerated(self):
        self.db.connection.execute(
            "INSERT INTO qa_claims(run_id,claim_key,stage,claim_text,claim_type,confidence,"
            "valid_from,valid_to,scope_json,verification_status,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, "c-bad", "conflict_review", "坏行", "current_fact", 0.5,
             None, None, "[]", "unverified", "{not-json", "2026-10-10T00:00:00Z"))
        layer = self.repo.build(self.run_id)
        self.assertEqual(layer["coverage"]["total_claims"], 1)
        self.assertEqual(layer["claims"][0]["claim_id"], "c-bad")

    def test_graph_from_rows_tolerates_junk(self):
        graph = eg.graph_from_rows(claims=[None, {"claim_key": ""}], edges=[None, "x"],
                                   conflicts=[None], evidence=["y"], run_id="r")
        self.assertEqual(graph["claims"], [])
        self.assertEqual(graph["edges"], [])

    def test_load_reports_storage_errors_without_raising(self):
        broken = eg.EvidenceGraphRepository(type("S", (), {})())
        with self.assertRaises(AttributeError):
            broken.load("run")
        rows = self.store.load_reasoning_graph(self.run_id)
        self.assertEqual(rows["error"], "")


class StageDetailsTests(_Base):
    def test_stage_runs_store_the_layer_without_schema_changes(self):
        graph = self._graph()
        layer = eg.layer_from_graph(graph, run_id=self.run_id)
        graph["evidence_graph"] = layer
        self.store.record_stage(self.run_id, "conflict_review", status="completed",
                                details={"result": graph})
        row = self._row("SELECT details_json FROM qa_stage_runs WHERE run_id=? AND stage=?",
                        (self.run_id, "conflict_review"))
        details = json.loads(row[0])
        stored = details["result"]["evidence_graph"]
        self.assertEqual(stored["graph_version"], layer["graph_version"])
        self.assertTrue(stored["coverage"]["coverage_definition"])
        self.assertEqual(self.repo.load_persisted(self.run_id)["coverage"]["total_claims"], 2)


class GatewayApiTests(_Base):
    """P06-01 的 HTTP 只读 API：`GET /api/qa/v1/runs/<run_id>/evidence-graph`。"""

    def setUp(self):
        super().setUp()
        from flask import Flask

        import qa_bridge
        import qa_gateway

        self.app = Flask(__name__)
        self.app.register_blueprint(qa_gateway.qa_bp)
        qa_gateway.set_qa_gateway_service(
            qa_gateway.QaGatewayService(self.store, repository=None))
        self.addCleanup(lambda: qa_gateway.set_qa_gateway_service(None))

        class _Manager:
            def authenticate(self, token):
                return {"sub": "tester", "owner_user_id": "p06", "industry_pack_id": "auto"}

        patcher = mock.patch.object(qa_bridge, "get_qa_bridge_manager",
                                    return_value=_Manager())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo.save(self.run_id, self._graph())

    def _get(self, path):
        # 走 bridge 会话头（与 RAGFlow 侧同一入口），避免依赖 cookie 的版本差异
        return self.app.test_client().get(path, headers={"X-QA-Bridge-Session": "token"})

    def test_endpoint_returns_coverage_and_distribution(self):
        run_id = str(self.run["id"])
        response = self._get("/api/qa/v1/runs/%s/evidence-graph" % run_id)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["success"])
        payload = body["evidence_graph"]
        self.assertEqual(payload["graph_version"], eg.__dict__["EVIDENCE_GRAPH_VERSION"])
        self.assertEqual(payload["source"], "rebuilt", "库里没有回放层时按表重建")
        self.assertEqual(payload["coverage"]["total_claims"], 2)
        self.assertIn("relation_distribution", payload["stats"])
        self.assertEqual(len(payload["claims"]), 2)

    def test_endpoint_prefers_the_persisted_layer(self):
        run_id = str(self.run["id"])
        graph = self._graph()
        layer = eg.layer_from_graph(graph, run_id=run_id)
        graph["evidence_graph"] = layer
        self.store.record_stage(run_id, "conflict_review", status="completed",
                                details={"result": graph})
        payload = self._get("/api/qa/v1/runs/%s/evidence-graph" % run_id).get_json()
        self.assertEqual(payload["evidence_graph"]["source"], "persisted")

    def test_unknown_run_is_a_404(self):
        response = self._get("/api/qa/v1/runs/nope/evidence-graph")
        self.assertEqual(response.status_code, 404)

    def test_limit_is_bounded(self):
        run_id = str(self.run["id"])
        payload = self._get("/api/qa/v1/runs/%s/evidence-graph?limit=1" % run_id).get_json()
        self.assertEqual(len(payload["evidence_graph"]["claims"]), 1)


if __name__ == "__main__":
    unittest.main()

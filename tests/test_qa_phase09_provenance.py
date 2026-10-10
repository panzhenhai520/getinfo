#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 09 · P09-06 `provenance/vector+graph+relational` 用例。

钉住：
  1. **provenance 可回溯**：每条落库记忆都能回到 Phase 02 的来源指纹 + 最小 span；
     回溯不上的**逐条列进 untraceable**（不四舍五入成"都能回溯"）；
  2. **三通道召回**：relational（作用域/状态/类型）、graph（memory_relation 一跳）、
     vector（**库内已有向量**余弦，零端点调用）——每个通道都有可复算统计；
  3. 向量通道的边界写清楚：没有向量 / 没有词面种子 / 没有 numpy 都降级并给原因码；
  4. 图通道一跳扩展的分数口径固定（父分 × 边权 × 0.9），且有上限与去重；
  5. `memory_relation` 只写本阶段自有的四个关系（其余是 Phase 10/12 的账）。
"""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_graph_contracts as contracts  # noqa: E402
import qa_memory as memory  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402

import qa_phase09_fixtures as fx  # noqa: E402

QUESTION = "香港家族办公室税收优惠政策对内地高净值客户有什么影响？"
NOW = "2026-10-11T00:00:00.000Z"


def _seed_memory(store, content, *, memory_type="VERIFIED_CLAIM", scope="PATIENT_LONGITUDINAL",
                 session_id="s1", industry_pack_id="auto", article_id=1, evidence_ref="article:1",
                 status="ACTIVE"):
    item = {
        "memory_id": memory.memory_id_for(scope=scope, memory_type=memory_type,
                                          canonical_content=content, session_id=session_id,
                                          industry_pack_id=industry_pack_id),
        "memory_type": memory_type, "canonical_content": content,
        "content_fingerprint": memory.content_fingerprint(content), "confidence": 0.8,
        "freshness_class": "LONG", "status": status, "scope": scope,
        "scope_key": memory.scope_key(scope, session_id=session_id,
                                      industry_pack_id=industry_pack_id),
        "session_id": session_id, "industry_pack_id": industry_pack_id,
        "source_evidence_ids": [evidence_ref], "last_verified_at": "2026-10-10T00:00:00.000Z",
        "version": 1, "decay_score": 0.6, "metadata": {},
    }
    store.save_memory_item(item)
    store.link_memory_evidence(item["memory_id"], [{
        "evidence_ref": evidence_ref, "source_fingerprint": "SF-%s" % evidence_ref,
        "span_fingerprint": "SP-%s" % evidence_ref, "verdict": "SUPPORTED",
        "evidence_score": 0.9, "run_id": "run-p09", "stage": "level1_retrieval",
        "corpus_version": "corpus-p09", "metadata": {"article_id": article_id}}])
    return item["memory_id"]


class ProvenanceTests(unittest.TestCase):
    def test_gate_written_memories_are_fully_traceable(self):
        with fx.temp_store() as (_database, store):
            candidates = memory.memory_candidates_from_graph(fx.graph(), industry_pack_id="auto")
            memory.apply_write_gate(store, candidates, run_id="run-p09")
            report = memory.provenance_report(store)
            self.assertGreater(report["checked"], 0)
            self.assertEqual(report["traceable"], report["checked"], "写门落库的记忆必须 100% 可回溯")
            self.assertEqual(report["untraceable"], [])
            self.assertEqual(report["links"]["with_source_fingerprint"],
                             report["links"]["evidence"])
            self.assertEqual(report["links"]["with_span"], report["links"]["evidence"])
            self.assertGreater(report["links"]["verified_verdict"], 0)
            ok, note = validate("memory_provenance_report", {key: report[key] for key in (
                "provenance_version", "checked", "traceable", "links")})
            self.assertTrue(ok, note)

    def test_untraceable_memories_are_listed_not_rounded(self):
        with fx.temp_store() as (_database, store):
            memory_id = _seed_memory(store, "没有证据链接的记忆")
            store.link_memory_evidence  # 只落记忆，不落链接
            with mock.patch.object(store, "memory_evidence", return_value=[]):
                report = memory.provenance_report(store)
            self.assertEqual(report["traceable"], 0)
            self.assertEqual([row["memory_id"] for row in report["untraceable"]], [memory_id])
            self.assertEqual(report["untraceable"][0]["reason"], "NO_EVIDENCE_LINK")

    def test_incomplete_link_is_reported_as_incomplete(self):
        with fx.temp_store() as (_database, store):
            memory_id = _seed_memory(store, "来源指纹缺失的记忆")
            with mock.patch.object(store, "memory_evidence", return_value=[{
                    "memory_id": memory_id, "evidence_ref": "", "source_fingerprint": "",
                    "span_fingerprint": ""}]):
                report = memory.provenance_report(store)
            self.assertEqual(report["untraceable"][0]["reason"], "LINK_INCOMPLETE",
                             "链接在但缺来源/span 指纹 → 记 LINK_INCOMPLETE（两种缺法要分开）")

    def test_evidence_link_rows_carry_phase02_identities(self):
        with fx.temp_store() as (_database, store):
            candidates = memory.memory_candidates_from_graph(fx.graph(), industry_pack_id="auto")
            receipt = memory.apply_write_gate(store, candidates, run_id="run-p09")
            links = store.memory_evidence(memory_ids=receipt["memory_ids"])
            self.assertTrue(links)
            for link in links:
                self.assertTrue(link["source_fingerprint"])
                self.assertTrue(link["span_fingerprint"])
                self.assertEqual(link["verdict"], "SUPPORTED")
                self.assertGreater(float(link["evidence_score"]), 0)


class Phase02AlignmentTests(unittest.TestCase):
    """跨会话记忆必须与 Phase 02 的**证据身份**对齐：同一份指纹，不是两套。"""

    def test_memory_links_use_the_same_fingerprints_as_the_seen_table(self):
        import qa_evidence

        with fx.temp_store() as (_database, store):
            item = fx.verified_evidence("article:1")
            # Phase 02 的 seen 记录怎么算指纹，记忆链就必须用同一个值
            seen_rows = qa_evidence.seen_records([item], status="confirmed")
            self.assertTrue(seen_rows)
            expected_source = seen_rows[0]["source_fingerprint"]
            expected_span = seen_rows[0]["span_fingerprint"]
            candidates = memory.memory_candidates_from_graph(
                fx.graph(), industry_pack_id="auto", session_id="s1", run_id="run-p09")
            memory.apply_write_gate(store, candidates, run_id="run-p09")
            links = store.memory_evidence(memory_ids=[row["memory_id"] for row in
                                                      store.load_memory_items(
                                                          include_all_scopes=True)])
            self.assertTrue(links)
            matched = [link for link in links if link["evidence_ref"] == "article:1"]
            self.assertTrue(matched)
            for link in matched:
                self.assertEqual(link["source_fingerprint"], expected_source,
                                 "记忆的来源指纹必须与 Phase 02 seen 的口径逐字相同")
                self.assertEqual(link["span_fingerprint"], expected_span,
                                 "记忆的 span 指纹必须与 Phase 02 seen 的口径逐字相同")

    def test_seen_confirmed_and_remembered_are_three_distinct_sets(self):
        """§11 的三层集合：seen（见过）/ confirmed（核验通过）/ remembered（跨会话保留）。"""
        import qa_evidence

        with fx.temp_store() as (_database, store):
            item = fx.verified_evidence("article:1")
            unverified = fx.annotated_evidence("article:9", text=fx.UNVERIFIED_EVIDENCE_TEXT)
            # seen：两条都登记（含没有被核验的那条，MASTER_RULES 14）
            qa_evidence.record_seen(store, scope=fx.SCOPE, accepted=[item],
                                    witnessed=[unverified], run_id="run-p09")
            # `seen_evidence()` 是按指纹查（Phase 02 的既有口径），不是"列出全部"
            fingerprints = [row["source_fingerprint"] for row in
                            qa_evidence.seen_records([item], status="confirmed")
                            + qa_evidence.seen_records([unverified], status="seen")]
            seen = store.seen_evidence(owner_user_id=fx.SCOPE["owner_user_id"],
                                       session_id=fx.SCOPE["session_id"],
                                       industry_pack_id=fx.SCOPE["industry_pack_id"],
                                       source_fingerprints=fingerprints)
            self.assertEqual(len(seen), 2)
            self.assertEqual(seen[fingerprints[0]], "confirmed")
            candidates = memory.memory_candidates_from_graph(
                fx.graph(), industry_pack_id="auto", session_id="s1", run_id="run-p09")
            receipt = memory.apply_write_gate(store, candidates, run_id="run-p09")
            remembered = store.load_memory_items(include_all_scopes=True)
            self.assertTrue(remembered)
            self.assertLessEqual(len(remembered), receipt["candidates"],
                                 "remembered 只能比 confirmed 更少（不是所有 confirmed 都值得记住）")


class GraphChannelTests(unittest.TestCase):
    def test_one_hop_expansion_is_deterministic_and_discounted(self):
        with fx.temp_store() as (_database, store):
            parent = _seed_memory(store, "香港家族办公室税收优惠结论甲")
            # 子记忆的正文**和查询没有词面交集**：只能靠图关系被带进来
            child = _seed_memory(store, "另一项与查询用词无关的表述", article_id=2,
                                 evidence_ref="article:2")
            # 关系是**有向**的：从父记忆指向子记忆（图通道只沿正向一跳）
            store.add_memory_relation([{"memory_id": parent, "relation": "APPLIES_TO",
                                        "target_memory_id": child, "target_kind": "memory",
                                        "weight": 1.0}])
            receipt = memory.recall(store, mode="evidence", query="香港家族办公室税收优惠",
                                    owner_user_id="p09", session_id="s1", industry_pack_id="auto",
                                    now=NOW)
            by_id = {hit["memory_id"]: hit for hit in receipt["hits"]}
            self.assertIn(parent, by_id)
            self.assertIn(child, by_id, "图关系应当把相关记忆带进来")
            self.assertIn("graph", by_id[child]["channels"])
            self.assertAlmostEqual(by_id[child]["score"],
                                   round(by_id[parent]["score"] * 1.0
                                         * memory.GRAPH_EXPANSION_WEIGHT, 8), places=8)
            self.assertEqual(receipt["counts"]["graph"], 1)

    def test_graph_expansion_can_be_disabled(self):
        with fx.temp_store() as (_database, store):
            parent = _seed_memory(store, "香港家族办公室税收优惠结论甲")
            child = _seed_memory(store, "另一项与查询用词无关的表述", article_id=2,
                                 evidence_ref="article:2")
            store.add_memory_relation([{"memory_id": parent, "relation": "APPLIES_TO",
                                        "target_memory_id": child, "weight": 1.0}])
            receipt = memory.recall(store, mode="evidence", query="香港家族办公室税收优惠",
                                    owner_user_id="p09", session_id="s1", industry_pack_id="auto",
                                    graph_expansion=False, now=NOW)
            self.assertNotIn("graph", receipt["channels"])
            self.assertEqual(receipt["counts"]["graph"], 0)


class VectorChannelTests(unittest.TestCase):
    def _vectors(self):
        import numpy as np

        matrix = np.array([[1.0, 0.0], [0.99, 0.01], [0.0, 1.0]], dtype="float32")
        norm = np.linalg.norm(matrix, axis=1, keepdims=True)
        return [1, 2, 3], matrix / norm

    def test_vector_channel_finds_semantic_neighbours_without_endpoints(self):
        with fx.temp_store() as (_database, store):
            # 词面命中的那条（种子）+ 与它语义相近但**用词不同**的那条（只能靠向量带进来）
            lexical = _seed_memory(store, "香港家族办公室税收优惠结论甲", article_id=1)
            semantic = _seed_memory(store, "与甲表述不同但同义的说法", article_id=2,
                                    evidence_ref="article:2")
            ids, matrix = self._vectors()
            receipt = memory.recall(store, mode="evidence", query="香港家族办公室税收优惠",
                                    owner_user_id="p09", session_id="s1", industry_pack_id="auto",
                                    vectors=(ids, matrix), now=NOW)
            channels = {hit["memory_id"]: hit["channels"] for hit in receipt["hits"]}
            self.assertIn("lexical", channels.get(lexical, []))
            self.assertIn("vector", channels.get(semantic, []),
                          "向量通道应当把词面碰不到的语义近邻带进来")
            stats = receipt["stats"]["vector"]
            self.assertEqual(stats["vectors"], 3)
            self.assertGreater(stats["seeds"], 0)
            self.assertEqual(receipt["counts"]["vector"], 2)

    def test_vector_channel_degrades_with_reasons(self):
        with fx.temp_store() as (_database, store):
            _seed_memory(store, "香港家族办公室税收优惠结论甲")
            no_vectors = memory.vector_channel([{"memory_id": "MEM1"}], terms={"家族"},
                                               article_ids={}, vectors=(None, None))
            self.assertEqual(no_vectors["stats"]["reason"], "no_vectors")
            ids, matrix = self._vectors()
            no_seed = memory.vector_channel([{"memory_id": "MEM1"}], terms={"家族"},
                                            article_ids={}, vectors=(ids, matrix))
            self.assertEqual(no_seed["stats"]["reason"], "no_lexical_seed")
            bad_matrix = memory.vector_channel([{"memory_id": "MEM1"}], terms={"家族"},
                                               article_ids={"MEM1": [1]}, vectors=([1], None))
            self.assertEqual(bad_matrix["stats"]["reason"], "no_vectors")

    def test_loader_failure_degrades_quietly(self):
        self.assertEqual(list(memory._vectors_for(lambda: (_ for _ in ()).throw(RuntimeError()))), [[], None])

    def test_recall_records_the_vector_boundary_in_stats(self):
        with fx.temp_store() as (_database, store):
            _seed_memory(store, "香港家族办公室税收优惠结论甲")
            receipt = memory.recall(store, mode="evidence", query="香港家族办公室税收优惠",
                                    owner_user_id="p09", session_id="s1", industry_pack_id="auto",
                                    vectors=([], None), now=NOW)
            self.assertIn(receipt["stats"]["vector"]["reason"], ("no_vectors", "no_lexical_seed"))
            self.assertIn("不调用嵌入端点", receipt["stats"]["vector"].get("note", ""))


class RelationGuardTests(unittest.TestCase):
    def test_gate_only_writes_this_phase_relations(self):
        with fx.temp_store() as (_database, store):
            candidates = memory.memory_candidates_from_graph(fx.graph(), industry_pack_id="auto")
            receipt = memory.apply_write_gate(store, candidates, run_id="run-p09")
            rows = store.memory_relations(receipt["memory_ids"])
            self.assertTrue(rows)
            written = {str(row["relation"]) for row in rows}
            self.assertTrue(written <= {"ABOUT", "DERIVED_FROM", "VALIDATED_BY", "APPLIES_TO"},
                            "本阶段不许写 SUPERSEDES/CONTRADICTS/EXPIRED_BY（Phase 10 的账）")
            self.assertTrue(written <= set(contracts.MEMORY_RELATIONS))

    def test_contradiction_risk_reads_phase10_edges_when_they_exist(self):
        item = {"memory_id": "MEM1", "status": "ACTIVE"}
        none = memory.contradiction_risk(item, relations=[])
        self.assertEqual(none["value"], 0.0)
        risky = memory.contradiction_risk(item, relations=[{"relation": "CONTRADICTS",
                                                            "weight": 0.8}])
        self.assertAlmostEqual(risky["value"], 0.8)
        inactive = memory.contradiction_risk({"memory_id": "MEM1", "status": "SUPERSEDED"})
        self.assertGreater(inactive["value"], 0.0)


class ChannelReceiptTests(unittest.TestCase):
    def test_channels_are_reported_even_when_nothing_hits(self):
        with fx.temp_store() as (_database, store):
            receipt = memory.recall(store, mode="evidence", query=QUESTION,
                                    owner_user_id="p09", session_id="s1", industry_pack_id="auto")
            self.assertIn("relational", receipt["channels"])
            self.assertIn("lexical", receipt["channels"])
            self.assertEqual(receipt["hits"], [])
            self.assertEqual(receipt["counts"]["considered"], 0)

    def test_memory_receipt_is_content_free(self):
        with fx.temp_store() as (_database, store):
            _seed_memory(store, "香港家族办公室税收优惠结论甲")
            receipt = memory.recall(store, mode="evidence", query="香港家族办公室税收优惠",
                                    owner_user_id="p09", session_id="s1", industry_pack_id="auto")
            summary = memory.memory_receipt(receipt)
            self.assertEqual(summary["hits"], 1)
            self.assertEqual(summary["verified_evidence"], 0)
            self.assertEqual(summary["requires_revalidation"], 1)
            self.assertNotIn("canonical_content", str(summary), "回执里不许带记忆正文")


if __name__ == "__main__":
    unittest.main()

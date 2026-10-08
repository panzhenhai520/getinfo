#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 8 知识图谱归并单测：幂等、边覆盖率、跨文章聚合、邻域查询。

前提：图是**派生视图**（源表为准），所以这里钉四件事：
  1. 同一份源数据反复归并，结果逐行一致（幂等，不产生重复边/节点）；
  2. 边覆盖率按"边/事件行"算，且主体没进 canonical 表时用归一化兜底而不是丢边；
  3. 同一实体跨文章可聚合（article_count/event_count 累加）；
  4. 邻域查询能按跳数与时间过滤，并且每条边都能回溯到文章。
"""
import os
import tempfile
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

from intel_database import IntelRepository  # noqa: E402
from kg_builder import KnowledgeGraphBuilder, _edge_key, _norm_key  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402


class KnowledgeGraphTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "kg.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.db.analyze_article_spacetime_profile = lambda _article_id: None
        self.repo = IntelRepository(self.db)
        self.repo._ensure()
        self.builder = KnowledgeGraphBuilder(repository=self.repo)
        self.article_ids = self._seed_articles()

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def _seed_articles(self):
        ids = []
        for index, title in enumerate(("工信部发布规划", "某车企完成融资", "协会发布报告"), 1):
            # 正文必须逐篇不同：入库有"内容哈希+域名"去重，同正文会被合并成同一篇
            article_id = self.db.insert_article({
                "url": "https://example.com/kg/%d" % index,
                "title": title,
                "content": ("投资管理行业测试正文第 %d 篇。" % index) * 30,
                "publish_date": "2026-10-0%d" % index,
                "matched_keywords": ["投资管理"],
            })
            ids.append(article_id)
        self.assertEqual(len(set(ids)), 3, "三篇夹具文章必须是三条记录（否则去重会合并）")
        return ids

    def _seed_events(self):
        self.repo.replace_article_events(
            article_id=self.article_ids[0], content_hash="h1", industry_pack_id="invest_mgmt",
            events=[
                {"subject": "工业和信息化部", "action": "发布", "object": "智能网联规划",
                 "event_time": "2026-09-11", "event_type": "regulation",
                 "state_before": "征求意见中", "state_after": "已发布",
                 "entities": ["工业和信息化部", "智能网联规划"]},
                {"subject": "工业和信息化部", "action": "批复", "object": "试点城市",
                 "event_time": "2026-09-20", "event_type": "regulation"},
            ],
        )
        self.repo.replace_article_events(
            article_id=self.article_ids[1], content_hash="h2", industry_pack_id="invest_mgmt",
            events=[
                {"subject": "某车企", "action": "完成", "object": "C轮融资",
                 "event_time": "2026-08-01", "event_type": "transaction"},
            ],
        )
        # 第三篇：抽不出事件 → 占位行（不得进图）
        self.repo.replace_article_events(
            article_id=self.article_ids[2], content_hash="h3", industry_pack_id="invest_mgmt",
            events=[],
        )
        self.repo.save_subject_canonical(
            industry_pack_id="invest_mgmt",
            mappings=[("工业和信息化部", "工信部", _norm_key("工信部"))],
        )

    def test_placeholders_never_become_nodes(self):
        self._seed_events()
        summary = self.builder.build(pack_id="invest_mgmt", apply=True)
        self.assertEqual(summary["events"], 3, "占位行不得计入事件行")
        keys = self._nodes()
        self.assertNotIn("__no_event__", keys)
        self.assertIn(_norm_key("工信部"), keys)

    def test_canonical_subject_key_wins_over_raw_text(self):
        self._seed_events()
        self.builder.build(pack_id="invest_mgmt", apply=True)
        edges = self._edges()
        self.assertTrue(all(edge["src_key"] != _norm_key("工业和信息化部") for edge in edges),
                        "已归一的主体必须用 canonical 的 subject_key 建边")
        self.assertTrue(any(edge["src_key"] == _norm_key("工信部") for edge in edges))

    def test_edge_coverage_and_state_fields(self):
        self._seed_events()
        summary = self.builder.build(pack_id="invest_mgmt", apply=True)
        self.assertEqual(summary["edges"], 3)
        self.assertEqual(summary["edge_coverage"], 1.0)
        with_state = [edge for edge in self._edges() if edge["state_after"]]
        self.assertEqual(len(with_state), 1)
        self.assertEqual(with_state[0]["state_before"], "征求意见中")

    def test_build_is_idempotent(self):
        self._seed_events()
        first = self.builder.build(pack_id="invest_mgmt", apply=True)
        edges_after_first = sorted(edge["edge_key"] for edge in self._edges())
        nodes_after_first = sorted(self._nodes().items())
        second = self.builder.build(pack_id="invest_mgmt", apply=True)
        self.assertEqual(first["edges"], second["edges"])
        self.assertEqual(edges_after_first, sorted(edge["edge_key"] for edge in self._edges()))
        self.assertEqual(nodes_after_first, sorted(self._nodes().items()))

    def test_entity_aggregates_across_articles(self):
        self._seed_events()
        self.builder.build(pack_id="invest_mgmt", apply=True)
        rows = self.builder._fetch(
            "SELECT article_count, event_count FROM kg_nodes WHERE node_key=?",
            (_norm_key("工信部"),),
        )
        self.assertEqual(rows[0]["event_count"], 2)
        self.assertEqual(rows[0]["article_count"], 1)

    def test_neighborhood_returns_edges_and_evidence_refs(self):
        self._seed_events()
        self.builder.build(pack_id="invest_mgmt", apply=True)
        result = self.builder.neighborhood("工信部", pack_id="invest_mgmt", depth=1)
        self.assertIsNotNone(result["node"])
        self.assertEqual(len(result["edges"]), 2)
        for edge in result["edges"]:
            self.assertTrue(edge["evidence_ref"].startswith("article:"))
            self.assertEqual(edge["src_key"], _norm_key("工信部"))
        labels = {row["label"] for row in result["neighbors"]}
        self.assertIn("智能网联规划", labels)

    def test_neighborhood_time_filter(self):
        self._seed_events()
        self.builder.build(pack_id="invest_mgmt", apply=True)
        result = self.builder.neighborhood(
            "工信部", pack_id="invest_mgmt", since="2026-09-15")
        self.assertEqual(len(result["edges"]), 1)
        self.assertEqual(result["edges"][0]["event_time"], "2026-09-20")

    def test_neighborhood_is_pack_isolated(self):
        self._seed_events()
        self.builder.build(pack_id="invest_mgmt", apply=True)
        other = self.builder.neighborhood("工信部", pack_id="family_office")
        self.assertEqual(other["edges"], [])

    def test_unknown_node_is_empty_not_error(self):
        result = self.builder.neighborhood("不存在的实体", pack_id="invest_mgmt")
        self.assertIsNone(result["node"])
        self.assertEqual(result["edges"], [])

    def test_dry_run_writes_nothing(self):
        self._seed_events()
        summary = self.builder.build(pack_id="invest_mgmt", apply=False)
        self.assertEqual(summary["edges"], 3)
        self.assertFalse(summary["applied"])
        self.assertNotIn("written_edges", summary)
        self.assertEqual(self._edges(), [])

    def test_edge_key_ignores_action_case_and_spacing(self):
        self.assertEqual(
            _edge_key("a", " 完成 ", "b", 7),
            _edge_key("a", "完成", "b", 7),
        )
        self.assertNotEqual(_edge_key("a", "完成", "b", 7), _edge_key("a", "完成", "b", 8))

    # ── 辅助 ──
    def _nodes(self):
        rows = self.builder._fetch("SELECT node_key, label, node_type FROM kg_nodes")
        return {row["node_key"]: row["label"] for row in rows}

    def _edges(self):
        return self.builder._fetch(
            "SELECT edge_key, src_key, dst_key, action, event_time, state_before, state_after,"
            " confidence, evidence_ref, article_id FROM kg_edges ORDER BY edge_key"
        )


if __name__ == "__main__":
    unittest.main()

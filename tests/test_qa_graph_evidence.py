#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 8 扩展：属性边 + 图证据通道（检索侧配套）。

要钉住的四件事（都是产品口径，不是实现细节）：
  1. 主系表/数值类陈述要**真的进图**（单独一类 attribute 边），不再被当噪声丢掉；
  2. **有效期可检索**：valid_from/valid_to 决定"在查询时点这条属性是否成立"；
  3. 检索侧按边类型给权重：问"是什么定位"时属性边权重高，问"发生了什么"时事件边权重高；
  4. 图事实与它的源文章是**两条**证据（图事实的 article_id 留空），
     既保留可回溯（metadata.article_id / article_url），又不会被按 article_id 去重挤掉全文。
"""
import json
import os
import tempfile
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

from intel_database import IntelRepository  # noqa: E402
from kg_builder import KnowledgeGraphBuilder, _date_in_window, _edge_key, _node_key  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402

import qa_retrieval as qr  # noqa: E402


class AttributeEdgeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "attr.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.db.analyze_article_spacetime_profile = lambda _article_id: None
        self.repo = IntelRepository(self.db)
        self.repo._ensure()
        self.builder = KnowledgeGraphBuilder(repository=self.repo)
        self.article_ids = []
        for index, title in enumerate(("模型定位说明", "国家药监局公告"), 1):
            self.article_ids.append(self.db.insert_article({
                "url": "https://example.com/attr/%d" % index,
                "title": title,
                "content": ("测试正文第 %d 篇。" % index) * 30,
                "publish_date": "2026-10-0%d" % index,
                "matched_keywords": ["投资管理"],
            }))
        self.assertTrue(len(set(self.article_ids)) == 2)

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def _seed(self):
        # 事件 + 属性同一次抽取的产物（与 event_extract_service 落库口径一致）
        self.repo.replace_article_events(
            article_id=self.article_ids[0], content_hash="h1", industry_pack_id="ai_news",
            events=[{"subject": "某团队", "action": "发布", "object": "新版模型",
                     "event_time": "2026-09-30", "event_type": "release",
                     "state_before": "未发布", "state_after": "已发布"}],
        )
        self.repo.replace_article_attributes(
            article_id=self.article_ids[0], content_hash="h1", industry_pack_id="ai_news",
            attributes=[
                {"subject": "新一代模型", "attribute": "精度定位", "value": "高精度",
                 "value_type": "name", "valid_from": "2026",
                 "evidence_quote": "最近生产的模型是高精度的模型"},
                {"subject": "2027款模型", "attribute": "延迟定位", "value": "低延迟",
                 "value_type": "name", "valid_from": "2027-01",
                 "evidence_quote": "2027年发布的模型是低延迟模型"},
            ],
        )
        self.repo.replace_article_events(
            article_id=self.article_ids[1], content_hash="h2", industry_pack_id="ai_news",
            events=[{"subject": "国家药监局", "action": "批准", "object": "某药品",
                     "event_time": "2026-09-15", "event_type": "regulation"}],
        )

    def test_attributes_become_edges_and_value_nodes(self):
        self._seed()
        summary = self.builder.build(pack_id="ai_news", apply=True)
        self.assertEqual(summary["attributes"], 2)
        self.assertEqual(summary["attribute_edges"], 2)
        self.assertEqual(summary["event_edges"], 2)
        rows = self.builder._fetch(
            "SELECT relation_kind, attr_key, attr_value, valid_from FROM kg_edges"
            " WHERE relation_kind='attribute' ORDER BY attr_key")
        self.assertEqual([row["attr_key"] for row in rows], ["延迟定位", "精度定位"])
        self.assertEqual(rows[1]["valid_from"], "2026")
        # 属性值也建了节点，便于反查"哪些主体是高精度"
        value_nodes = self.builder._fetch(
            "SELECT node_key, label FROM kg_nodes WHERE node_type='value'")
        self.assertEqual({row["label"] for row in value_nodes}, {"高精度", "低延迟"})

    def test_attribute_edge_key_distinguishes_attributes(self):
        """同一主体在一篇文章里的两条属性不能互相覆盖。"""
        self._seed()
        self.builder.build(pack_id="ai_news", apply=True)
        keys = [row["edge_key"] for row in self.builder._fetch("SELECT edge_key FROM kg_edges")]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(keys), 4)
        self.assertNotEqual(
            _edge_key("a", "精度定位", "高精度", 1, relation_kind="attribute", attr_key="精度定位"),
            _edge_key("a", "延迟定位", "低延迟", 1, relation_kind="attribute", attr_key="延迟定位"),
        )

    def test_validity_window_filtering(self):
        self._seed()
        self.builder.build(pack_id="ai_news", apply=True)
        node = _node_key("新一代模型")
        rows = self.builder.edges_for_nodes([node], pack_id="ai_news", as_of="2026-10-09")
        self.assertEqual(len(rows), 1, "2026 时点只应看到精度定位（2027 那条还没生效）")
        self.assertEqual(rows[0]["attr_key"], "精度定位")

        future = self.builder.edges_for_nodes([_node_key("2027款模型")], pack_id="ai_news",
                                              as_of="2026-10-09")
        self.assertEqual(future, [], "2027 才生效的属性，在 2026 的查询里不该出现")
        later = self.builder.edges_for_nodes([_node_key("2027款模型")], pack_id="ai_news",
                                            as_of="2027-06-01")
        self.assertEqual(len(later), 1)
        self.assertEqual(later[0]["attr_value"], "低延迟")

    def test_date_window_helper_edges(self):
        self.assertTrue(_date_in_window("2026-10-09", "2026", "2027"))
        self.assertTrue(_date_in_window("2026-10-09", "", ""))
        self.assertFalse(_date_in_window("2028-01", "2026", "2027"))
        self.assertFalse(_date_in_window("2025-12", "2026-01", ""))
        self.assertTrue(_date_in_window("2026-10-09", "2026-10-09", "2026-10-09"))

    def test_neighborhood_can_filter_by_relation_kind(self):
        self._seed()
        self.builder.build(pack_id="ai_news", apply=True)
        events = self.builder.neighborhood("国家药监局", pack_id="ai_news",
                                          relation_kind="event")
        self.assertEqual(len(events["edges"]), 1)
        self.assertEqual(events["edges"][0]["relation_kind"], "event")
        attrs = self.builder.neighborhood("新一代模型", pack_id="ai_news",
                                          relation_kind="attribute", as_of="2026-10-09")
        self.assertEqual([edge["attr_key"] for edge in attrs["edges"]], ["精度定位"])


class GraphEvidenceChannelTests(unittest.TestCase):
    """检索侧的图证据通道：权重、有效期、可回溯、包隔离。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "graphqa.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.db.analyze_article_spacetime_profile = lambda _article_id: None
        self.repo = IntelRepository(self.db)
        self.repo._ensure()
        self.builder = KnowledgeGraphBuilder(repository=self.repo)
        self.article_id = self.db.insert_article({
            "url": "https://example.com/model/1",
            "title": "模型定位说明：高精度与低延迟",
            "content": "团队说明了两类模型的定位。" * 20,
            "publish_date": "2026-10-01",
            "matched_keywords": ["模型"],
        })
        self.repo.replace_article_attributes(
            article_id=self.article_id, content_hash="h1", industry_pack_id="ai_news",
            attributes=[{"subject": "新一代模型", "attribute": "精度定位", "value": "高精度",
                         "value_type": "name", "valid_from": "2026",
                         "evidence_quote": "最近生产的模型是高精度的模型"}],
        )
        self.repo.replace_article_events(
            article_id=self.article_id, content_hash="h1", industry_pack_id="ai_news",
            events=[{"subject": "新一代模型", "action": "发布", "object": "推理服务",
                     "event_time": "2026-09-20", "event_type": "release"}],
        )
        self.builder.build(pack_id="ai_news", apply=True)

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def _evidence(self, question, as_of=""):
        plan = {"question": question, "queries": [question], "entities": ["新一代模型"],
                "as_of": as_of}
        return qr.graph_evidence(plan, industry_pack_id="ai_news", limit=4, builder=self.builder)

    def test_attribute_intent_ranks_attribute_edge_first(self):
        result = self._evidence("新一代模型是什么定位")
        self.assertTrue(result["evidence"])
        first = result["evidence"][0]
        self.assertEqual(first["source_type"], "graph")
        self.assertEqual(first["metadata"]["relation_kind"], "attribute")
        self.assertIn("精度定位", first["content_excerpt"])
        self.assertIn("高精度", first["content_excerpt"])
        self.assertTrue(first["evidence_ref"].startswith("edge:"))

    def test_event_intent_ranks_event_edge_first(self):
        result = self._evidence("新一代模型最近发生了什么")
        self.assertEqual(result["evidence"][0]["metadata"]["relation_kind"], "event")
        self.assertIn("发布", result["evidence"][0]["content_excerpt"])

    def test_weights_are_reported_for_audit(self):
        result = self._evidence("新一代模型是什么定位")
        self.assertGreater(result["stats"]["weights"]["attribute"],
                           result["stats"]["weights"]["event"])
        self.assertEqual(result["stats"]["attribute"], 1)

    def test_validity_filters_future_attributes(self):
        self.repo.replace_article_attributes(
            article_id=self.article_id, content_hash="h1", industry_pack_id="ai_news",
            attributes=[{"subject": "下一代模型", "attribute": "延迟定位", "value": "低延迟",
                         "value_type": "name", "valid_from": "2027-01"}],
        )
        self.builder.build(pack_id="ai_news", apply=True)
        plan = {"question": "下一代模型是什么定位", "queries": ["下一代模型是什么定位"],
                "entities": ["下一代模型"], "as_of": "2026-10-09"}
        result = qr.graph_evidence(plan, industry_pack_id="ai_news", limit=4, builder=self.builder)
        self.assertEqual(result["evidence"], [], "2027 才生效的属性不该出现在 2026 的检索里")

    def test_evidence_stays_traceable_without_stealing_article_slot(self):
        result = self._evidence("新一代模型是什么定位")
        item = result["evidence"][0]
        self.assertIsNone(item["article_id"], "图事实不能占文章的 article_id（会被去重挤掉全文）")
        self.assertEqual(item["metadata"]["article_id"], self.article_id)
        self.assertTrue(item["metadata"]["article_url"])
        self.assertEqual(item["source_url"], "https://example.com/model/1")
        self.assertEqual(item["published_at"], "2026-10-01")

    def test_pack_isolation(self):
        plan = {"question": "新一代模型是什么定位", "queries": [], "entities": ["新一代模型"]}
        result = qr.graph_evidence(plan, industry_pack_id="family_office", limit=4,
                                   builder=self.builder)
        self.assertEqual(result["evidence"], [], "不得跨包取证据")

    def test_empty_graph_is_silent(self):
        plan = {"question": "某个图里没有的主体是什么", "queries": [], "entities": ["不存在的主体"]}
        result = qr.graph_evidence(plan, industry_pack_id="ai_news", limit=4, builder=self.builder)
        self.assertEqual(result["evidence"], [])
        self.assertIn("没有命中", result["stats"]["note"])

    def test_switch_off(self):
        previous = os.environ.get("QA_GRAPH_EVIDENCE_ENABLED")
        os.environ["QA_GRAPH_EVIDENCE_ENABLED"] = "0"
        try:
            result = self._evidence("新一代模型是什么定位")
            self.assertEqual(result["evidence"], [])
            self.assertIn("已关闭", result["stats"]["note"])
        finally:
            if previous is None:
                os.environ.pop("QA_GRAPH_EVIDENCE_ENABLED", None)
            else:
                os.environ["QA_GRAPH_EVIDENCE_ENABLED"] = previous


class EvidenceContractTests(unittest.TestCase):
    def test_graph_source_type_is_allowed(self):
        import qa_contracts

        schema = qa_contracts.EVIDENCE_SCHEMA["properties"]["source_type"]
        self.assertIn("graph", schema["enum"])


class AdmissionReportTests(unittest.TestCase):
    """覆盖率天花板账：四档准入口径的计数必须**按文章去重**（多包归属会重复计数）。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "admission.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.db.analyze_article_spacetime_profile = lambda _article_id: None
        self.repo = IntelRepository(self.db)
        self.repo._ensure()

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def _article(self, index, category, anchor, packs=("ai_news",)):
        article_id = self.db.insert_article({
            "url": "https://example.com/adm/%d" % index,
            "title": "准入测试 %d" % index,
            "content": "正文" * 30,
            "publish_date": "2026-10-0%d" % (index % 9 + 1),
            "matched_keywords": ["模型"],
        })
        details = json.dumps({"hits": {"anchor": ["模型"] if anchor else []}}, ensure_ascii=False)
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                for pack in packs:
                    cursor.execute(
                        "INSERT INTO article_intel_classifications(article_id, industry_pack_id,"
                        " industry_pack_version, classifier_version, article_content_hash,"
                        " rule_category, matched_keywords_json, topic_tags_json, final_category,"
                        " score_details_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (article_id, pack, "v1", "test", "h%d" % index, "other",
                         "[]", "[]", category, details),
                    )
                self.db.connection.commit()
            finally:
                cursor.close()
        return article_id

    def test_counts_are_deduplicated_across_packs(self):
        # 2 篇 trend/event + 锚点（当前口径）
        self._article(1, "event", True)
        self._article(2, "trend", True)
        # 1 篇 trend 但没命中锚点；1 篇 other 类但有锚点
        self._article(3, "trend", False)
        self._article(4, "other", True)
        # 1 篇同时归属两个包（同口径下只能算 1 篇）
        self._article(5, "event", True, packs=("ai_news", "family_office"))

        report = self.repo.admission_report(pack_id="")
        self.assertEqual(report["base"], 3, "trend|event + 锚点：1、2、5 共 3 篇")
        self.assertEqual(report["no_anchor"], 4, "放宽锚点：多出第 3 篇")
        self.assertEqual(report["with_other"], 4, "纳入 other：多出第 4 篇")
        self.assertEqual(report["widened"], 5)
        self.assertEqual(report["extra_if_with_other"], 1)

    def test_pack_filter(self):
        self._article(11, "event", True, packs=("ai_news",))
        self._article(12, "event", True, packs=("family_office",))
        self.assertEqual(self.repo.admission_report(pack_id="ai_news")["base"], 1)
        self.assertEqual(self.repo.admission_report(pack_id="family_office")["base"], 1)


if __name__ == "__main__":
    unittest.main()

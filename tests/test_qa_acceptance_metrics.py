#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""使用侧验收工具的指标口径测试（阶段 7/8 验收：命中 / 可回溯 / 接地 / 图证据）。

要点：**可回溯**必须真的能解析回原文（article:<id> / edge:<key>），
不能只看有没有 URL —— 否则"看起来有引用、其实点不开"会被误判成合格。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.qa_retrieval_acceptance as acceptance  # noqa: E402
from intel_database import IntelRepository  # noqa: E402
from kg_builder import KnowledgeGraphBuilder  # noqa: E402
from sqlite_database import SQLiteDatabase  # noqa: E402


class AcceptanceMetricTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = SQLiteDatabase(os.path.join(self.temp_dir.name, "acceptance.sqlite3"))
        self.db.connect()
        self.db.create_tables()
        self.db.analyze_article_spacetime_profile = lambda _article_id: None
        self.repo = IntelRepository(self.db)
        self.repo._ensure()
        self.article_id = self.db.insert_article({
            "url": "https://example.com/acc/1",
            "title": "比亚迪发布新款车型",
            "content": "比亚迪在发布会上宣布新款车型上市，续航提升。" * 20,
            "publish_date": "2026-10-01",
            "matched_keywords": ["比亚迪"],
        })
        self.repo.replace_article_events(
            article_id=self.article_id, content_hash="h1", industry_pack_id="automotive_industry",
            events=[{"subject": "比亚迪", "action": "发布", "object": "新款车型",
                     "event_time": "2026-10-01", "event_type": "release"}],
        )
        self.repo.replace_article_attributes(
            article_id=self.article_id, content_hash="h1", industry_pack_id="automotive_industry",
            attributes=[{"subject": "比亚迪", "attribute": "销量", "value": "第一",
                         "value_type": "text"}],
        )
        with self.db.lock:
            cursor = self.db.connection.cursor()
            try:
                cursor.execute(
                    "INSERT INTO article_intel_classifications(article_id, industry_pack_id,"
                    " industry_pack_version, classifier_version, article_content_hash,"
                    " rule_category, matched_keywords_json, topic_tags_json, final_category,"
                    " score_details_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (self.article_id, "automotive_industry", "v1", "test", "h1", "event",
                     json.dumps(["比亚迪"], ensure_ascii=False), "[]", "event",
                     json.dumps({"hits": {"anchor": ["比亚迪"]}}, ensure_ascii=False)),
                )
                self.db.connection.commit()
            finally:
                cursor.close()
        KnowledgeGraphBuilder(repository=self.repo).build(pack_id="automotive_industry", apply=True)
        # 工具内部走的是全局单例：这里把两个模块级引用都指到临时库，
        # 否则图证据会去查线上库（测试就测不到"图证据参与检索"这条路径）。
        import intel_database

        self._saved = (acceptance.sqlite_db, intel_database.sqlite_db)
        acceptance.sqlite_db = self.db
        intel_database.sqlite_db = self.db

    def tearDown(self):
        import intel_database

        acceptance.sqlite_db, intel_database.sqlite_db = self._saved
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def test_article_ref_and_edge_ref_resolve(self):
        cursor = self.db.connection.cursor()
        try:
            article = acceptance._fetch_ref(cursor, "article:%d" % self.article_id)
            self.assertTrue(article.get("url"), "article:<id> 必须能解析出原文链接")
            cursor.execute("SELECT edge_key FROM kg_edges LIMIT 1")
            edge_key = cursor.fetchone()["edge_key"]
            edge = acceptance._fetch_ref(cursor, "edge:%s" % edge_key)
            self.assertEqual(str(edge.get("edge_key")), edge_key)
            self.assertTrue((edge.get("article") or {}).get("url"),
                            "edge:<key> 必须能顺着 article_id 回到原文")
            self.assertEqual(acceptance._fetch_ref(cursor, "edge:not-exist"), {})
        finally:
            cursor.close()

    def test_resolve_evidence_requires_openable_url(self):
        cursor = self.db.connection.cursor()
        try:
            good = acceptance._resolve_evidence(cursor, {"evidence_ref": "article:%d" % self.article_id})
            self.assertTrue(good["ok"])
            bad = acceptance._resolve_evidence(cursor, {"evidence_ref": "article:999999"})
            self.assertFalse(bad["ok"], "解析不到的文章不能算可回溯")
            external = acceptance._resolve_evidence(
                cursor, {"evidence_ref": "web:1", "source_url": "https://example.com/x"})
            self.assertTrue(external["ok"], "联网证据自带 URL 时可算可回溯")
        finally:
            cursor.close()

    def test_grounding_uses_question_terms(self):
        evidence = [{"title": "比亚迪发布新款车型", "content_excerpt": "比亚迪新款上市",
                     "source_url": "https://example.com/acc/1"}]
        self.assertTrue(acceptance._grounded("比亚迪最近有什么动态？", evidence, ["比亚迪"]))
        self.assertFalse(acceptance._grounded("比亚迪最近有什么动态？", evidence, ["特斯拉"]))

    def test_evaluate_end_to_end_metrics(self):
        outcome = acceptance.evaluate(
            [{"id": "q1", "question": "比亚迪最近有什么动态？", "kind": "event",
              "industry_pack_id": "automotive_industry", "expect_terms": ["比亚迪"]}],
            limit=12, verbose=False,
        )
        summary = outcome["summary"]
        self.assertEqual(summary["questions"], 1)
        self.assertEqual(summary["hit_rate"], 1.0)
        self.assertEqual(summary["traceable_rate"], 1.0, "每条证据都要能点回原文")
        self.assertEqual(summary["grounded_rate"], 1.0)
        self.assertGreaterEqual(summary["graph_rate"], 1.0, "该问题应能用上图谱事实（事件边/属性边）")
        self.assertEqual(summary["errors"], 0)
        self.assertIn("event", summary["by_kind"])

    def test_empty_corpus_reports_miss_not_error(self):
        outcome = acceptance.evaluate(
            [{"id": "q9", "question": "某个语料里完全没有的主体最近有什么动态？", "kind": "event",
              "industry_pack_id": "automotive_industry", "expect_terms": ["完全不存在的主体"]}],
            limit=8, verbose=False,
        )
        summary = outcome["summary"]
        self.assertEqual(summary["errors"], 0, "取不到证据算「空」，不算报错")
        self.assertEqual(summary["hit_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 04 · P04-01…P04-05 单 Hunter 单元与失败路径测试。

覆盖（每条都要能单独跑）：
  · P04-01 BM25 Hunter：排序可复算（手算 BM25 对照）、时间闸门、空查询失败路径、
    分词复用（停用词/标题加权）、契约校验；
  · P04-02 Semantic Hunter：**零向量时降级**、离线质心查询向量能召回"词面不命中"的文章、
    注入 provider 的插拔点、绝不调 embedding 端点（源码级 + 行为级双重证据）；
  · P04-03 Graph Hunter adapter：纯适配（转发 plan/pack/limit/builder）、图库抛错降级；
  · P04-04 Structured adapter：政策登记表通道复用、元数据闸门（无过滤条件则不产证据）、
    外部 provider 注入且单源失败不影响其它源；
  · P04-05 Query Expansion：只产词不产证据（§8.5 硬约束）、词源可追溯、图谱邻居；
  · 通用：safe_run 把异常收敛成契约对象、单 Hunter 开关、全部结果过 HUNTER_RESULT_SCHEMA。

隔离：全部用临时 sqlite（`tests/qa_phase04_corpus.make_db` 里断言 backend=='sqlite'），
不连真库、不调任何模型/嵌入端点。
"""
import json
import math
import os
import sys
import tempfile
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qa_hunters as hunters  # noqa: E402
from qa_graph_contracts import validate  # noqa: E402
from qa_retrieval import ArticleRetriever  # noqa: E402
from qa_phase04_corpus import (  # noqa: E402
    DEFAULT_PACK,
    add_article,
    add_classification,
    add_embedding,
    add_events,
    add_policy_document,
    make_db,
    seed_standard_corpus,
)

QUESTION = "香港家族办公室税收优惠政策对内地高净值客户有什么影响？"


class _Base(unittest.TestCase):
    PACK = DEFAULT_PACK

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = make_db(os.path.join(self.temp_dir.name, "phase04-hunters.sqlite3"))
        self.retriever = ArticleRetriever(self.db)

    def tearDown(self):
        try:
            self.db.connection.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def request(self, *, question=QUESTION, plan=None, **overrides):
        payload = {
            "question": question,
            "queries": [question],
            "entities": ["家族办公室"],
            "needs_local_articles": True,
        }
        payload.update(plan or {})
        kwargs = {"industry_pack_id": self.PACK, "limit": 12}
        kwargs.update(overrides)
        return hunters.HunterRequest.from_plan(payload, **kwargs)


class BM25HunterTests(_Base):
    def setUp(self):
        super().setUp()
        self.ids = seed_standard_corpus(self.db, pack_id=self.PACK)
        self.hunter = hunters.BM25Hunter(self.retriever)

    def test_bm25_math_is_recomputable(self):
        """手算对照：idf = ln(1 + (N-df+0.5)/(df+0.5))，tf 饱和 + 长度归一。"""
        docs = [["家族办公室"] * 3, ["家族办公室"]]
        scores = hunters.bm25_scores(["家族办公室"], docs, k1=1.5, b=0.75)
        idf = math.log(1 + (2 - 2 + 0.5) / (2 + 0.5))
        avgdl = 2.0
        norm1 = 1 - 0.75 + 0.75 * (3 / avgdl)
        norm2 = 1 - 0.75 + 0.75 * (1 / avgdl)
        expected1 = idf * (3 * 2.5) / (3 + 1.5 * norm1)
        expected2 = idf * (1 * 2.5) / (1 + 1.5 * norm2)
        self.assertAlmostEqual(scores[0], expected1, places=9)
        self.assertAlmostEqual(scores[1], expected2, places=9)
        self.assertGreater(scores[0], scores[1], "词频高的文档分数必须更高")

    def test_bm25_ranks_the_relevant_article_first(self):
        outcome = self.hunter.run(self.request())
        self.assertTrue(outcome["ok"], outcome.get("reason_code"))
        self.assertEqual(outcome["route"], "keyword")
        refs = [item["evidence_ref"] for item in outcome["evidence"]]
        self.assertEqual(refs[0], "article:%d" % self.ids["primary"])
        self.assertNotIn("article:%d" % self.ids["unrelated"], refs,
                         "与问题无关的文章不该进 BM25 结果")
        for item in outcome["evidence"]:
            self.assertEqual(item["retrieval_method"], "bm25")
            self.assertTrue(item["content_excerpt"])
        self.assertTrue(validate("hunter_result", outcome)[0])

    def test_bm25_prunes_documents_without_any_query_term(self):
        outcome = self.hunter.run(self.request())
        stats = outcome["stats"]
        self.assertGreaterEqual(stats["pool_rows"], 3)
        self.assertLess(stats["candidates"], stats["pool_rows"],
                        "不含任何查询词的文档必须被剪枝（BM25 恒为 0，剪枝等价）")

    def test_empty_query_terms_is_a_clean_failure_path(self):
        request = hunters.HunterRequest.from_plan(
            {"question": "什么？", "queries": [""], "needs_local_articles": True},
            industry_pack_id=self.PACK, limit=5)
        outcome = self.hunter.run(request)
        self.assertEqual(outcome["status"], "empty")
        self.assertEqual(outcome["reason_code"], "no_query_terms")
        self.assertEqual(outcome["evidence"], [])
        self.assertTrue(validate("hunter_result", outcome)[0])

    def test_time_window_is_a_hard_filter(self):
        """问"2026年1月"，10 月的新文必须被时间闸门挡住（复用既有 apply_time_gate）。

        时间词取自 `queries`（与既有 `retrieve()` 同口径：`_qsrc = queries or [question]`），
        所以这里把时间写进查询式本身，避免测出"和既有通道不一样"的假象。
        """
        question = "2026年1月香港家族办公室政策有什么变化？"
        request = self.request(question=question, plan={
            "question": question, "queries": ["香港 家族办公室 政策 2026年1月"]})
        outcome = self.hunter.run(request)
        refs = [item["evidence_ref"] for item in outcome["evidence"]]
        self.assertNotIn("article:%d" % self.ids["primary"], refs)
        self.assertTrue(outcome["stats"]["time_gate"]["hard_filter"])
        self.assertEqual(outcome["stats"]["window"], "2026 年 1 月")

    def test_bm25_query_tokens_use_the_document_tokenizer(self):
        """查询与文档必须同一分词器；提问框架词（动态/情况）不许进 BM25 查询词。"""
        request = self.request(question="蔚来最近有什么动态？",
                               plan={"question": "蔚来最近有什么动态？",
                                     "queries": ["蔚来最近有什么动态？"]})
        tokens = hunters.bm25_query_tokens(request)
        self.assertIn("蔚来", tokens)
        for frame in ("动态", "新动态", "情况", "最近", "什么"):
            self.assertNotIn(frame, tokens, "提问框架词不该进 BM25 查询词：%s" % frame)
        # 与文档侧同一个分词器：文档里出现的写法必须能被切出来
        self.assertIn("蔚来", hunters._tokenize_doc("蔚来发布新款车型"))

    def test_generic_frame_word_must_not_outrank_the_entity(self):
        """回归：只含"框架词"的文章不许盖过含实体的文章（idf 必须用全池 df）。

        这是实测踩到的坑：idf 只在"被剪枝剩下的候选"上统计时，泛词因为稀有反而 idf 极高，
        把「限时权益」这类无关促销文顶到「蔚来」前面。
        """
        entity = add_article(self.db, url="https://example.com/p04/entity",
                             title="蔚来公布换电站扩建计划",
                             content="蔚来宣布扩建换电站网络，覆盖更多城市。" * 3,
                             publish_date="2026-10-01", keywords=["蔚来"])
        add_classification(self.db, entity, pack_id=self.PACK, keywords=["蔚来"])
        for index in range(6):
            other = add_article(self.db, url="https://example.com/p04/promo%d" % index,
                                title="限时权益价上市，整车终身免费质保 %d" % index,
                                content="购车门槛降低，限时权益启动。最近行情变化快。" * 3,
                                publish_date="2026-10-02", keywords=["购车"])
            add_classification(self.db, other, pack_id=self.PACK, keywords=["购车"])
        request = self.request(question="蔚来最近有什么动态？",
                               plan={"question": "蔚来最近有什么动态？",
                                     "queries": ["蔚来最近有什么动态？"]})
        outcome = self.hunter.run(request)
        refs = [item["evidence_ref"] for item in outcome["evidence"]]
        self.assertTrue(refs, outcome.get("reason_code"))
        self.assertEqual(refs[0], "article:%d" % entity,
                         "含实体的文章必须排在只含框架词的促销文之前")

    def test_doc_tokenizer_reuses_stopwords_and_weights_title(self):
        tokens = hunters._tokenize_doc("家族办公室的税收优惠政策是什么")
        self.assertNotIn("什么", tokens, "停用词必须沿用 qa_retrieval._STOP")
        self.assertIn("家族", tokens, "长 CJK 词要补 2/3 字 n-gram，和查询侧词表同族")
        row = {"id": 1, "title": "家族办公室", "content": "税收政策",
               "matched_keywords_json": "[]", "topic_tags_json": "[]"}
        doc = self.hunter.doc_tokens(self.PACK, row, "家族办公室 税收政策")
        self.assertGreaterEqual(doc.count("家族"), 3, "标题词必须在文档词表里被加权（标题×3）")
        self.assertIs(doc, self.hunter.doc_tokens(self.PACK, row, "家族办公室 税收政策"),
                      "同一篇文档第二次取词必须命中缓存（对象同一性）")


class SemanticHunterTests(_Base):
    def test_degrades_when_the_library_has_no_vectors(self):
        seed_standard_corpus(self.db, pack_id=self.PACK)
        hunter = hunters.SemanticHunter(self.retriever, database=self.db)
        outcome = hunter.run(self.request())
        self.assertEqual(outcome["status"], "degraded")
        self.assertEqual(outcome["reason_code"], "no_vectors")
        self.assertEqual(outcome["evidence"], [])
        self.assertFalse(outcome["ok"])
        self.assertIn("不调用 embedding 端点", outcome["stats"]["note"])
        self.assertTrue(validate("hunter_result", outcome)[0])

    def test_offline_pivot_vector_recalls_articles_without_query_terms(self):
        """核心能力：语义相近但**词面不命中**的文章也要被召回，并计入 semantic_only。"""
        question = "家族办公室税收优惠"
        seed = add_article(
            self.db, url="https://example.com/p04/seed", title="家族办公室税收优惠落地",
            content="家族办公室税收优惠落地：家族投资控权工具可享利得税宽免。",
            publish_date="2026-10-01", keywords=["家族办公室"])
        add_classification(self.db, seed, pack_id=self.PACK, keywords=["家族办公室"])
        # 邻居刻意不含任何查询词（族/办公室/税收/优惠 一律不出现），只能靠向量找回来
        neighbor = add_article(
            self.db, url="https://example.com/p04/neighbor", title="离岸信托架构与税务居民身份安排",
            content="离岸信托架构与税务居民身份的安排决定整体税负水平，需与豁免安排配套。",
            publish_date="2026-09-20", keywords=["离岸信托"])
        add_classification(self.db, neighbor, pack_id=self.PACK, keywords=["离岸信托"])
        unrelated = add_article(
            self.db, url="https://example.com/p04/far", title="车规芯片供应链观察",
            content="功率半导体价格回落。", publish_date="2026-06-01", keywords=["芯片"])
        add_classification(self.db, unrelated, pack_id=self.PACK, keywords=["芯片"])
        # 4 维向量：neighbor 与 seed 语义相近（余弦 0.98），unrelated 正交
        add_embedding(self.db, seed, [1.0, 0.0, 0.0, 0.0])
        add_embedding(self.db, neighbor, [0.98, 0.199, 0.0, 0.0])
        add_embedding(self.db, unrelated, [0.0, 0.0, 1.0, 0.0])
        hunter = hunters.SemanticHunter(self.retriever, database=self.db)
        outcome = hunter.run(self.request(question=question, limit=5))
        refs = [item["evidence_ref"] for item in outcome["evidence"]]
        self.assertEqual(refs[0], "article:%d" % seed)
        self.assertIn("article:%d" % neighbor, refs, "语义近似文章必须被召回")
        self.assertNotIn("article:%d" % unrelated, refs, "正交向量（余弦 0）必须被门槛挡住")
        self.assertEqual(outcome["stats"]["semantic_only"], 1,
                         "词面不命中、靠向量找回的条数必须如实计数")
        self.assertEqual(outcome["stats"]["pivot"], "lexical_centroid")
        by_ref = {item["evidence_ref"]: item for item in outcome["evidence"]}
        self.assertGreater(float(by_ref["article:%d" % seed]["score"]),
                           float(by_ref["article:%d" % neighbor]["score"]))

    def test_query_vector_provider_is_a_pluggable_seam(self):
        """将来有查询编码器（或缓存）时，注入 provider 即可切回真语义路径。"""
        import numpy as np

        seed = add_article(self.db, url="https://example.com/p04/s2", title="家族办公室政策解读",
                           content="家族办公室政策解读。", publish_date="2026-10-01",
                           keywords=["家族办公室"])
        add_classification(self.db, seed, pack_id=self.PACK, keywords=["家族办公室"])
        other = add_article(self.db, url="https://example.com/p04/s3", title="无关文章",
                            content="与问题无关。", publish_date="2026-10-01", keywords=["其他"])
        add_classification(self.db, other, pack_id=self.PACK, keywords=["其他"])
        add_embedding(self.db, seed, [1.0, 0.0])
        add_embedding(self.db, other, [1.0, 0.0])
        calls = []

        def provider(request):
            calls.append(request.question)
            return np.asarray([1.0, 0.0], dtype=np.float32)

        hunter = hunters.SemanticHunter(self.retriever, database=self.db,
                                        query_vector_provider=provider)
        outcome = hunter.run(self.request(limit=5))
        self.assertEqual(calls, [QUESTION])
        self.assertEqual(outcome["stats"]["pivot"], "injected_provider")
        self.assertEqual(len(outcome["evidence"]), 2, "注入 provider 后不再依赖词面种子")

    def test_vector_loader_failure_degrades_instead_of_raising(self):
        seed_standard_corpus(self.db, pack_id=self.PACK)

        def boom():
            raise RuntimeError("向量表读不了")

        hunter = hunters.SemanticHunter(self.retriever, database=self.db, vector_loader=boom)
        outcome = hunter.safe_run(self.request())
        self.assertEqual(outcome["status"], "error")
        self.assertEqual(outcome["reason_code"], "hunter_exception")
        self.assertIn("向量表读不了", outcome["error"])

    def test_no_endpoint_call_in_the_semantic_path(self):
        """行为级证据：语义通道只读库表，代码里没有 embedding 客户端调用点。"""
        source = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "qa_hunters.py"), encoding="utf-8").read()
        for marker in ("embedding_client", "_embed_question", "_semantic_top_articles",
                       "requests.", "httpx", "urllib3", "socket"):
            self.assertNotIn(marker, source,
                             "语义通道不许出现端点调用痕迹：%s" % marker)


class GraphHunterTests(_Base):
    def test_adapter_forwards_to_the_existing_graph_channel(self):
        calls = []

        def fake_graph(plan, *, industry_pack_id, limit, builder=None):
            calls.append({"plan": plan, "pack": industry_pack_id, "limit": limit,
                          "builder": builder})
            return {"evidence": [{"evidence_ref": "edge:e1", "source_type": "graph"}],
                    "stats": {"enabled": True, "used": 1, "event": 1, "attribute": 0}}

        marker = object()
        hunter = hunters.GraphHunter(builder=marker, graph_runner=fake_graph)
        outcome = hunter.run(self.request(limit=12))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["pack"], self.PACK)
        self.assertIs(calls[0]["builder"], marker, "builder 注入点必须原样转发")
        self.assertEqual(calls[0]["limit"], 4, "图证据名额沿用既有 min(4, limit//3) 口径")
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["stats"]["used"], 1)
        self.assertTrue(validate("hunter_result", outcome)[0])

    def test_graph_failure_degrades_without_raising(self):
        def boom(plan, **kwargs):
            raise RuntimeError("图库不可用")

        hunter = hunters.GraphHunter(graph_runner=boom)
        outcome = hunter.safe_run(self.request())
        self.assertEqual(outcome["status"], "error")
        self.assertIn("图库不可用", outcome["error"])
        self.assertEqual(outcome["evidence"], [])

    def test_real_graph_channel_end_to_end(self):
        """真链路：事件/属性行 → kg_builder 建图 → 图 Hunter 取回证据（不调模型）。"""
        from kg_builder import KnowledgeGraphBuilder

        ids = seed_standard_corpus(self.db, pack_id=self.PACK)
        builder = KnowledgeGraphBuilder(repository=self._repository())
        builder.build(pack_id=self.PACK, apply=True)
        hunter = hunters.GraphHunter(builder=builder)
        outcome = hunter.run(self.request(plan={"question": QUESTION,
                                                "queries": ["家族办公室 税收优惠"],
                                                "entities": ["香港特区政府", "家族办公室"]}))
        self.assertTrue(outcome["stats"]["enabled"], outcome["stats"].get("note"))
        self.assertGreaterEqual(len(outcome["evidence"]), 1, "事件/属性边应产出图证据")
        self.assertIn(str(ids["primary"]),
                      [str((item.get("metadata") or {}).get("article_id"))
                       for item in outcome["evidence"]])

    def _repository(self):
        from intel_database import IntelRepository

        return IntelRepository(self.db)


class StructuredHunterTests(_Base):
    def setUp(self):
        super().setUp()
        self.ids = seed_standard_corpus(self.db, pack_id=self.PACK)
        self.hunter = hunters.StructuredHunter(self.retriever)

    def test_normalize_structured_filters_whitelists_keys(self):
        filters = hunters.normalize_structured_filters({
            "doc_type": "official_policy,official_interpretation", "min_authority": "80",
            "domains": "gov.hk", "categories": ["event"], "未知键": "x", "min_authority_bad": "y"})
        self.assertEqual(filters["doc_types"], ["official_policy", "official_interpretation"])
        self.assertEqual(filters["min_authority"], 80)
        self.assertEqual(filters["domains"], ["gov.hk"])
        self.assertEqual(filters["categories"], ["event"])
        self.assertNotIn("未知键", filters)

    def test_policy_registry_channel_is_reused(self):
        """政策登记表走既有 `_policy_exact_evidence`：这里用桩证明"确实复用了它"。"""
        calls = []

        def fake_policy(plan, *, industry_pack_id, limit=6):
            calls.append({"pack": industry_pack_id, "limit": limit})
            return ([{"evidence_ref": "article:9", "article_id": 9, "source_type": "article",
                      "title": "政策原文", "source_url": "https://example.com/p9",
                      "content_excerpt": "政策原文", "score": 1051.0,
                      "retrieval_method": "policy_metadata_exact", "metadata": {}}],
                    {"policy_exact_gate": "applied", "adopted": 1})

        self.retriever._policy_exact_evidence = fake_policy
        outcome = self.hunter.run(self.request())
        self.assertEqual(calls[0]["pack"], self.PACK)
        self.assertEqual(outcome["stats"]["policy_registry"]["adopted"], 1)
        self.assertEqual([item["evidence_ref"] for item in outcome["evidence"]], ["article:9"])

    def test_metadata_gate_needs_explicit_filters(self):
        without = self.hunter.metadata_gate(self.request(), self.retriever._rows(self.PACK)[0])
        self.assertEqual(without[0], [], "没给过滤条件时元数据闸门不产证据（避免变成第二遍关键词检索）")
        request = self.request(structured_filters={"min_authority": 1, "categories": "event"})
        evidence, stats = self.hunter.metadata_gate(request, self.retriever._rows(self.PACK)[0])
        self.assertGreaterEqual(len(evidence), 1)
        self.assertEqual(stats["filters"]["categories"], ["event"])
        for item in evidence:
            self.assertEqual(item["retrieval_method"], "structured_metadata")
            self.assertIn("结构化字段命中", item["match_reason"])

    def test_metadata_gate_drops_rows_below_authority(self):
        request = self.request(structured_filters={"min_authority": 60})
        evidence, _stats = self.hunter.metadata_gate(request, self.retriever._rows(self.PACK)[0])
        self.assertEqual(evidence, [], "权威度不达标的行必须被挡掉")

    def test_external_provider_seam_and_failure_isolation(self):
        def good_provider(request):
            return [{"evidence_ref": "emr:1", "source_type": "internal_db", "title": "内部证据库",
                     "source_url": "https://example.com/emr/1", "content_excerpt": "内部记录",
                     "score": 10.0, "retrieval_method": "structured_metadata", "metadata": {}}]

        def bad_provider(request):
            raise RuntimeError("EMR 连接失败")

        self.retriever._policy_exact_evidence = lambda plan, **kw: ([], {"policy_exact_gate": "applied"})
        hunter = hunters.StructuredHunter(self.retriever, structured_providers=[bad_provider,
                                                                               good_provider])
        outcome = hunter.run(self.request())
        refs = [item["evidence_ref"] for item in outcome["evidence"]]
        self.assertIn("emr:1", refs, "一个源失败不许拖垮其它源（Failure Isolation）")
        providers = {item["name"]: item for item in outcome["stats"]["providers"]}
        self.assertIn("EMR 连接失败", providers["bad_provider"]["error"])
        self.assertEqual(providers["good_provider"]["items"], 1)

    def test_policy_document_row_produces_structured_evidence(self):
        """真数据：政策登记表行 + 政策锚点 → 精确命中（走既有 policy_exact 通道）。"""
        article_id = add_article(
            self.db, url="https://example.com/p04/policy",
            title="关于家族办公室税收优惠政策的公告", content="现将家族办公室税收优惠政策公告如下。",
            publish_date="2026-10-02", keywords=["家族办公室"])
        add_classification(self.db, article_id, pack_id=self.PACK, keywords=["家族办公室"])
        add_policy_document(self.db, article_id, doc_type="official_policy", doc_no="2026年第7号",
                            issuer="财政部", policy_title="家族办公室税收优惠政策公告",
                            authority_level=5)
        request = self.request(plan={
            "question": "财政部2026年第7号公告对家族办公室税收优惠是怎么规定的？",
            "queries": ["财政部 2026年第7号公告 家族办公室 税收优惠"],
            "policy_anchors": {"is_policy": True, "notices": [{"year": "2026", "number": "7",
                                                              "variants": ["2026年第7号"]}],
                               "issuers": ["财政部"], "topics": ["家族办公室税收优惠"]},
            "entities": ["家族办公室"]})
        outcome = self.hunter.run(request)
        self.assertEqual(outcome["stats"]["policy_registry"].get("policy_exact_gate"), "applied")
        refs = [item["evidence_ref"] for item in outcome["evidence"]]
        self.assertIn("article:%d" % article_id, refs, "政策登记表精确命中必须进结构化通道")


class QueryExpansionHunterTests(_Base):
    def setUp(self):
        super().setUp()
        self.ids = seed_standard_corpus(self.db, pack_id=self.PACK)
        self.hunter = hunters.QueryExpansionHunter(graph_neighbor_limit=4)

    def test_produces_terms_and_never_evidence(self):
        outcome = self.hunter.run(self.request())
        self.assertEqual(outcome["evidence"], [], "§8.5：Query Expansion 不允许产证据")
        self.assertGreater(len(outcome["terms"]), 1)
        sources = {item["source"] for item in outcome["terms"]}
        self.assertIn("token", sources)
        self.assertTrue(outcome["queries"], "必须给出扩展检索式")
        self.assertTrue(validate("hunter_result", outcome)[0])
        for item in outcome["terms"]:
            self.assertTrue(item["term"] and item["source"])

    def test_entity_aliases_and_traditional_forms_are_included(self):
        outcome = self.hunter.run(self.request(question="工信部对人形机器人标准化的最新表态？",
                                               plan={"question": "工信部对人形机器人标准化的最新表态？",
                                                     "queries": ["工信部 人形机器人 标准化"]}))
        terms = {item["term"] for item in outcome["terms"]}
        self.assertIn("MIIT", terms, "实体别名（既有 ENTITY_GROUPS）必须被用上")
        self.assertTrue(any(term == "人形機器人" for term in terms),
                        "繁体写法必须被展开")

    def test_graph_neighbors_are_used_as_related_entities(self):
        from kg_builder import KnowledgeGraphBuilder
        from intel_database import IntelRepository

        builder = KnowledgeGraphBuilder(repository=IntelRepository(self.db))
        builder.build(pack_id=self.PACK, apply=True)

        class _Neighborhood:
            enabled = True

            def neighborhood(self, node_key, **kwargs):
                return {"neighbors": [{"node_key": "家族信托", "label": "家族信托"}],
                        "edges": [], "stats": {}}

        hunter = hunters.QueryExpansionHunter(builder=_Neighborhood(), graph_neighbor_limit=4)
        outcome = hunter.run(self.request())
        labels = {item["term"]: item["source"] for item in outcome["terms"]}
        self.assertEqual(labels.get("家族信托"), "graph_neighbor")
        self.assertTrue(outcome["stats"]["enabled"])

    def test_graph_failure_does_not_break_expansion(self):
        class _Boom:
            def neighborhood(self, node_key, **kwargs):
                raise RuntimeError("图不可用")

        hunter = hunters.QueryExpansionHunter(builder=_Boom())
        outcome = hunter.safe_run(self.request())
        self.assertTrue(outcome["ok"], "图邻居取不到也要能出词（只是少了 graph_neighbor 来源）")
        self.assertIn("邻域查询失败", outcome["stats"]["graph_note"])


class HunterShellTests(_Base):
    def test_safe_run_turns_exceptions_into_contract_objects(self):
        class _Boom(hunters.BaseHunter):
            hunter_id = "bm25"

            def run(self, request):
                raise ValueError("炸了")

        outcome = _Boom().safe_run(self.request())
        self.assertEqual(outcome["status"], "error")
        self.assertEqual(outcome["reason_code"], "hunter_exception")
        self.assertIn("ValueError", outcome["error"])
        self.assertTrue(validate("hunter_result", outcome)[0])

    def test_single_hunter_switch_is_respected(self):
        seed_standard_corpus(self.db, pack_id=self.PACK)
        os.environ["QA_HUNTER_BM25_ENABLED"] = "0"
        try:
            outcome = hunters.BM25Hunter(self.retriever).safe_run(self.request())
        finally:
            os.environ.pop("QA_HUNTER_BM25_ENABLED", None)
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(outcome["reason_code"], "hunter_disabled")
        self.assertFalse(outcome["stats"]["enabled"])

    def test_every_hunter_declares_a_route_from_the_frozen_enum(self):
        from qa_graph_contracts import QA_RETRIEVAL_ROUTES

        for hunter in (hunters.BM25Hunter(self.retriever),
                       hunters.SemanticHunter(self.retriever, database=self.db),
                       hunters.GraphHunter(), hunters.StructuredHunter(self.retriever),
                       hunters.QueryExpansionHunter()):
            self.assertIn(hunter.route, list(QA_RETRIEVAL_ROUTES) + [""],
                          "Hunter 的 route 必须落在既有通道枚举里（不许扩枚举）")

    def test_retrieval_pool_loads_once_and_is_shared(self):
        seed_standard_corpus(self.db, pack_id=self.PACK)
        pool = hunters.RetrievalPool(self.retriever, self.PACK)
        request = self.request(pool=pool)
        hunters.BM25Hunter(self.retriever).run(request)
        hunters.SemanticHunter(self.retriever, database=self.db).run(request)
        hunters.StructuredHunter(self.retriever).run(request)
        self.assertEqual(pool.loads, 1, "候选池一次加载、多 Hunter 共用")
        self.assertGreaterEqual(pool.stats()["pool_rows"], 3)


if __name__ == "__main__":
    unittest.main()

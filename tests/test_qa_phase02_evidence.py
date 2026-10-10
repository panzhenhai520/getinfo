#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 02 · P02-01 / P02-02 / P02-04 的证据层验收。

钉住四件事：
  1. **冻结契约不许被动**：`qa_contracts.EVIDENCE_SCHEMA` 是 `additionalProperties: False`
     的历史指纹（P00-02 = 370301331c02c738），证据层新增的一切只能进 `metadata`；
  2. **最小 span 是真的能切**：`content_excerpt[start:end] == quote`、不超上限、定位不到实词
     就老实退回开头一段（不假装精准）；
  3. **provenance 能一路回溯**：run/stage/route/检索方式/时间 + source/chunk/span 链条；
  4. **fingerprint 跨轮稳定**：来源级指纹不随正文长度变化（否则跨 run 去重立刻失效），
     去重口径与改造前的 `qa_pipeline._dedupe_evidence` 逐字一致。
"""
import hashlib
import json
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_evidence as evidence_layer  # noqa: E402
from qa_graph_contracts import (  # noqa: E402
    EVIDENCE_LAYER_VERSION, EVIDENCE_STATUSES, EVIDENCE_STATUS_REFUTED,
    EVIDENCE_STATUS_SUPPORTED, EVIDENCE_STATUS_UNVERIFIED,
    validate as validate_contract,
)

# P00-02 冻结的七个契约指纹（这里只钉证据层最关心的那个）
FROZEN_EVIDENCE_FINGERPRINT = "370301331c02c738"


def _fingerprint(value) -> str:
    """与 tools/qa_baseline_inventory._fingerprint 同一算法（规范化 sha256 前 16 位）。"""
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _article_item(**overrides):
    item = {
        "evidence_ref": "article:12345",
        "source_type": "article",
        "title": "某地出台新能源补贴新政",
        "source_url": "https://example.com/a/1",
        "article_id": 12345,
        "content_excerpt": (
            "近日，某地发布《新能源汽车产业扶持办法》。办法明确，对本地整车企业给予一次性补贴。"
            "补贴标准按车型分档执行，最高不超过三万元。该办法自发布之日起施行，有效期三年。"
        ),
        "published_at": "2026-10-01",
        "authority_level": 60,
        "score": 42.5,
        "retrieval_method": "keyword",
        "match_reason": "标题命中：新能源补贴",
        "relationship": "supports",
        "metadata": {"matched_keywords": ["新能源补贴", "整车企业"], "topic_tags": ["汽车产业"]},
    }
    item.update(overrides)
    return item


def _graph_item(**overrides):
    item = {
        "evidence_ref": "edge:e1",
        "source_type": "graph",
        "title": "图谱属性：某车企 的补贴力度：三万元",
        "source_url": "https://example.com/a/1",
        "article_id": None,
        "content_excerpt": "某车企 的补贴力度：三万元；原文：对本地整车企业给予一次性补贴",
        "published_at": "2026-10-01",
        "authority_level": 50,
        "score": 65.0,
        "retrieval_method": "graph_attribute",
        "match_reason": "知识图谱属性边（主体命中问题：某车企）",
        "relationship": "supports",
        "metadata": {
            "graph_edge_key": "e1", "relation_kind": "attribute",
            "src_key": "某车企", "dst_key": "三万元", "attr_key": "补贴力度", "attr_value": "三万元",
            "valid_from": "2026-10-01", "valid_to": "2029-10-01",
        },
    }
    item.update(overrides)
    return item


class FrozenContractTests(unittest.TestCase):
    """P02-01：证据层**不许**动冻结契约——这是本阶段最容易踩的坑。"""

    def test_evidence_schema_fingerprint_unchanged(self):
        from qa_contracts import EVIDENCE_SCHEMA

        self.assertEqual(_fingerprint(EVIDENCE_SCHEMA), FROZEN_EVIDENCE_FINGERPRINT,
                         "EVIDENCE_SCHEMA 变了：历史上一放宽/收紧都造成过生产回归")

    def test_evidence_schema_is_still_strict(self):
        from qa_contracts import EVIDENCE_SCHEMA

        self.assertIs(EVIDENCE_SCHEMA["additionalProperties"], False)
        self.assertEqual(sorted(EVIDENCE_SCHEMA["required"]),
                         ["content_excerpt", "evidence_ref", "metadata", "source_type",
                          "source_url", "title"])

    def test_metadata_still_allows_extra_keys(self):
        """证据层唯一的落点：metadata。它必须是"任意对象"，否则标注会整条失败。"""
        from qa_contracts import EVIDENCE_SCHEMA

        self.assertEqual(EVIDENCE_SCHEMA["properties"]["metadata"], {"type": "object"})


class EvidenceObjectContractTests(unittest.TestCase):
    """P02-01：新 schema 真的会拦下缺字段/越界枚举（含嵌套）。"""

    def _layer(self, **overrides):
        layer = evidence_layer.annotate_evidence(
            _article_item(), run_id="run-1", stage="level1_retrieval")["metadata"]["evidence_layer"]
        layer.update(overrides)
        return layer

    def test_annotation_payload_matches_evidence_object_schema(self):
        ok, note = validate_contract("evidence_object", self._layer())
        self.assertTrue(ok, note)

    def test_missing_span_is_rejected(self):
        layer = self._layer()
        layer.pop("span")
        ok, note = validate_contract("evidence_object", layer)
        self.assertFalse(ok)
        self.assertIn("span", note)

    def test_bad_status_is_rejected(self):
        ok, note = validate_contract("evidence_object", self._layer(status="VERIFIED"))
        self.assertFalse(ok)
        self.assertIn("status", note)

    def test_nested_span_defect_is_caught(self):
        """嵌套字段缺 quote 必须被拦（递归校验，而不是只看顶层）。"""
        layer = self._layer()
        layer["span"] = {"start": 0, "end": 5}
        ok, note = validate_contract("evidence_object", layer)
        self.assertFalse(ok)
        self.assertIn("span.quote", note)

    def test_nested_entity_defect_is_caught(self):
        layer = self._layer(entities=[{"label": "缺 entity_key"}])
        ok, note = validate_contract("evidence_object", layer)
        self.assertFalse(ok)
        self.assertIn("entities[0].entity_key", note)

    def test_legacy_schemas_keep_their_old_behaviour(self):
        """递归校验是新增能力，不能改变既有五个 schema 的判定。"""
        self.assertTrue(validate_contract("execution_node", {"node_id": "plan"})[0])
        self.assertTrue(validate_contract("search_trace", {"hop_index": 0})[0])
        self.assertFalse(validate_contract("kg_edge", {"src_key": "a", "dst_key": "b"})[0])
        self.assertFalse(validate_contract("未知", {})[0])


class QuoteSpanTests(unittest.TestCase):
    """P02-01：最小证据 span。"""

    def test_offsets_are_self_consistent(self):
        content = _article_item()["content_excerpt"]
        span = evidence_layer.minimal_quote_span(content, ["补贴标准", "整车企业"])
        self.assertEqual(content[span["start"]:span["end"]], span["quote"])
        self.assertEqual(span["chars"], len(span["quote"]))
        self.assertLessEqual(span["chars"], evidence_layer.span_max_chars())

    def test_span_is_deterministic(self):
        content = _article_item()["content_excerpt"]
        first = evidence_layer.minimal_quote_span(content, ["整车企业"])
        second = evidence_layer.minimal_quote_span(content, ["整车企业"])
        self.assertEqual(first, second)

    def test_span_follows_password_the_longest_term(self):
        """最长实词最具体：命中它才算定位到了支持性最强的那一段。"""
        content = ("开头一堆背景说明。" + "无关内容。" * 40
                   + "对本地整车企业给予一次性补贴，最高不超过三万元。" + "尾注。" * 40)
        span = evidence_layer.minimal_quote_span(content, ["补贴", "整车企业"])
        self.assertIn("整车企业", span["quote"])
        self.assertNotIn("整车企业", span["quote"][:5])  # 不是恰好从开头截的
        self.assertEqual(span["source"], "query_terms")

    def test_equal_length_terms_take_the_earliest_occurrence(self):
        """同长实词的取舍规则写死：取最早出现的那一个（可预测，便于排查）。"""
        content = ("开头先说甲方案。" + "铺垫内容。" * 40
                   + "后来说乙方案。" + "收尾内容。" * 40)
        span = evidence_layer.minimal_quote_span(content, ["甲方案", "乙方案"])
        self.assertIn("甲方案", span["quote"])

    def test_span_snaps_to_sentence_boundary(self):
        """最小 span 要落在完整句子上：起点紧跟句末标点，终点就是句末标点。

        断言的是**性质**而不是某一段具体文本：窗口居中后落在哪里取决于实词位置，
        钉死"必须从第二句开始"是脆弱写法（内容一改就红），钉死边界性质才是真契约。
        """
        content = (("丙" * 200) + "。" + ("丁" * 30) + "。"
                   + "第二句才是关键：对整车企业给予一次性补贴。" + "第三句收尾。")
        span = evidence_layer.minimal_quote_span(content, ["一次性补贴"], max_chars=80)
        boundaries = "。！？；!?;…\n\r"
        self.assertTrue(span["quote"].endswith("。"), span["quote"])
        self.assertTrue(span["start"] == 0 or content[span["start"] - 1] in boundaries, span["quote"])
        self.assertIn("一次性补贴", span["quote"])
        self.assertEqual(content[span["start"]:span["end"]], span["quote"])

    def test_without_terms_falls_back_to_lead(self):
        content = "甲" * 900
        span = evidence_layer.minimal_quote_span(content, [])
        self.assertEqual(span["source"], "lead")
        self.assertEqual(span["start"], 0)
        self.assertLessEqual(span["chars"], evidence_layer.span_max_chars())

    def test_short_content_is_whole(self):
        span = evidence_layer.minimal_quote_span("很短的一段证据。", ["证据"])
        self.assertEqual(span["source"], "whole")
        self.assertEqual(span["quote"], "很短的一段证据。")

    def test_empty_content_is_safe(self):
        span = evidence_layer.minimal_quote_span("", ["随便"])
        self.assertEqual(span, {"start": 0, "end": 0, "quote": "", "source": "whole", "chars": 0})
        self.assertEqual(evidence_layer.minimal_quote_span(None, None)["quote"], "")

    def test_whitespace_only_window_never_returns_empty_quote(self):
        content = "命中词" + " " * 400
        span = evidence_layer.minimal_quote_span(content, ["命中词"], max_chars=80)
        self.assertTrue(span["quote"].strip())
        self.assertEqual(content[span["start"]:span["end"]], span["quote"])


class AnnotateEvidenceTests(unittest.TestCase):
    """P02-01 + P02-02：标注只加 metadata，顶层键集一字不变。"""

    def test_top_level_keys_are_untouched(self):
        item = _article_item()
        annotated = evidence_layer.annotate_evidence(item, run_id="run-1")
        self.assertEqual(set(annotated), set(item))
        self.assertNotIn("span", annotated)
        self.assertNotIn("status", annotated)
        self.assertIn("evidence_layer", annotated["metadata"])

    def test_annotated_evidence_passes_level1_contract(self):
        """最要紧的回归：服务端自造的（已标注）证据必须还能过冻结契约。"""
        from qa_contracts import validate_level1_result
        from qa_level1 import empty_level1_result

        annotated = evidence_layer.annotate_evidence_batch(
            [_article_item(), _graph_item()], run_id="run-1")
        result = validate_level1_result(empty_level1_result("草稿", annotated))
        self.assertEqual(len(result["evidence"]), 2)
        self.assertEqual(result["evidence"][0]["metadata"]["evidence_layer"]["status"],
                         EVIDENCE_STATUS_SUPPORTED)

    def test_status_mapping_covers_relationship(self):
        self.assertEqual(evidence_layer.evidence_status(_article_item(relationship="contradicts")),
                         EVIDENCE_STATUS_REFUTED)
        self.assertEqual(evidence_layer.evidence_status(_article_item(relationship="qualifies")),
                         "QUALIFIED")
        self.assertEqual(evidence_layer.evidence_status(_article_item(relationship="context")),
                         "CONTEXT")
        self.assertEqual(evidence_layer.evidence_status(_article_item(relationship=None)),
                         EVIDENCE_STATUS_UNVERIFIED)
        self.assertEqual(evidence_layer.evidence_status(_article_item(relationship="胡说")),
                         EVIDENCE_STATUS_UNVERIFIED)
        for status in ("SUPPORTED", "REFUTED", "QUALIFIED", "CONTEXT", "UNVERIFIED"):
            self.assertIn(status, EVIDENCE_STATUSES)

    def test_relationship_is_not_rewritten(self):
        """既有字段语义不许被证据层改掉（旧调用方还在读 relationship）。"""
        annotated = evidence_layer.annotate_evidence(_article_item(relationship="contradicts"))
        self.assertEqual(annotated["relationship"], "contradicts")
        self.assertEqual(annotated["metadata"]["evidence_layer"]["relationship"], "contradicts")

    def test_article_entities_come_from_existing_metadata_only(self):
        """不做新 NER：文章证据的实体只能来自命中的关键词/主题标签/问题实词。"""
        item = _article_item()
        item["relevance_hits"] = ["补贴"]
        entities = evidence_layer.evidence_entities(item)
        self.assertEqual([row["origin"] for row in entities],
                         ["matched_keyword", "matched_keyword", "topic_tag", "question_term"])
        self.assertEqual(entities[0]["entity_key"], evidence_layer.node_key("新能源补贴"))
        # 没被任何既有信息提到的东西，绝不出现在实体表里
        self.assertNotIn(evidence_layer.node_key("整车企业"), [row["entity_key"] for row in entities[2:]])

    def test_graph_entities_cover_both_ends(self):
        entities = evidence_layer.evidence_entities(_graph_item())
        self.assertEqual([row["origin"] for row in entities], ["graph_src", "graph_attr"])
        self.assertEqual(entities[0]["entity_type"], "entity")
        self.assertEqual(entities[1]["entity_type"], "value")

    def test_relations_only_for_graph_evidence(self):
        self.assertEqual(evidence_layer.evidence_relations(_article_item()), [])
        relations = evidence_layer.evidence_relations(_graph_item())
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0]["relation_kind"], "attribute")
        ok, note = validate_contract("evidence_relation", relations[0])
        self.assertTrue(ok, note)

    def test_invalid_relation_is_dropped(self):
        """失败路径：枚举越界的关系必须被丢掉，不许脏数据进证据图。"""
        bad = _graph_item(metadata={**_graph_item()["metadata"], "relation_kind": "causes"})
        self.assertEqual(evidence_layer.evidence_relations(bad), [])
        self.assertEqual(evidence_layer.annotate_evidence(bad)["metadata"]["evidence_layer"]["relations"], [])

    def test_missing_graph_endpoint_is_dropped(self):
        bad = _graph_item(metadata={**_graph_item()["metadata"], "src_key": ""})
        self.assertEqual(evidence_layer.evidence_relations(bad), [])

    def test_disabled_flag_returns_input_unchanged(self):
        os.environ["QA_EVIDENCE_LAYER_ENABLED"] = "0"
        try:
            item = _article_item()
            annotated = evidence_layer.annotate_evidence(item, run_id="run-1")
            self.assertEqual(annotated, item)
            self.assertNotIn("evidence_layer", annotated["metadata"])
        finally:
            os.environ.pop("QA_EVIDENCE_LAYER_ENABLED", None)

    def test_batch_skips_non_mappings(self):
        items = evidence_layer.annotate_evidence_batch([_article_item(), None, "字符串"])
        self.assertEqual(len(items), 1)


class ProvenanceTests(unittest.TestCase):
    """P02-02：证据 → 来源 → chunk → span 的链条必须完整。"""

    def test_provenance_chain(self):
        annotated = evidence_layer.annotate_evidence(
            _article_item(), run_id="run-9", stage="level1_retrieval", route="keyword",
            round_index=2, corpus_version="corpus-abc", retrieved_at="2026-10-09T00:00:00Z")
        layer = annotated["metadata"]["evidence_layer"]
        provenance = layer["provenance"]
        self.assertEqual(provenance["run_id"], "run-9")
        self.assertEqual(provenance["stage"], "level1_retrieval")
        self.assertEqual(provenance["route"], "keyword")
        self.assertEqual(provenance["round_index"], 2)
        self.assertEqual(provenance["corpus_version"], "corpus-abc")
        self.assertEqual(provenance["retrieved_at"], "2026-10-09T00:00:00Z")
        self.assertEqual(provenance["source"]["source_id"], "article:12345")
        self.assertEqual(layer["source"]["source_id"], provenance["source"]["source_id"])
        self.assertEqual(provenance["source"]["document_id"], "")
        self.assertEqual(provenance["chunk_id"], "")
        # span 与来源信息一起构成"可以从证据回到原文那一段"的最小闭环
        content = annotated["content_excerpt"]
        self.assertEqual(content[layer["span"]["start"]:layer["span"]["end"]], layer["span"]["quote"])

    def test_route_defaults_to_retrieval_method(self):
        provenance = evidence_layer.evidence_provenance(_graph_item())
        self.assertEqual(provenance["route"], "graph_attribute")
        self.assertEqual(provenance["source"]["source_id"], "edge:e1")

    def test_chunk_identity_prefers_document_plus_chunk(self):
        item = _article_item(source_type="ragflow_chunk", article_id=None,
                             document_id="doc-1", chunk_id="chunk-7", metadata={})
        source = evidence_layer.source_identity(item)
        self.assertEqual(source["source_id"], "chunk:doc-1#chunk-7")
        self.assertEqual(evidence_layer.evidence_provenance(item)["chunk_id"], "chunk-7")


class FingerprintTests(unittest.TestCase):
    """P02-04：指纹与去重。"""

    def test_source_fingerprint_is_stable_across_excerpt_lengths(self):
        """跨轮去重的前提：正文被截得更短，来源身份必须不变。"""
        long_item = _article_item()
        short_item = _article_item(content_excerpt=long_item["content_excerpt"][:60])
        self.assertEqual(evidence_layer.source_fingerprint(long_item),
                         evidence_layer.source_fingerprint(short_item))
        self.assertNotEqual(evidence_layer.source_fingerprint(_article_item(article_id=999)),
                            evidence_layer.source_fingerprint(long_item))

    def test_span_fingerprint_changes_with_span(self):
        """同一篇文章的不同段落 = 不同证据对象（span 级指纹必须不同）。"""
        long_content = (("无关背景。" * 30)
                        + "甲段：补贴标准按车型分档执行，最高不超过三万元。"
                        + ("中间铺垫。" * 30)
                        + "乙段：整车企业的申报材料需在年底前提交。"
                        + ("尾部说明。" * 30))
        item = _article_item(content_excerpt=long_content)
        first = evidence_layer.annotate_evidence(item, terms=["补贴标准"])
        second = evidence_layer.annotate_evidence(item, terms=["整车企业"])
        first_layer = first["metadata"]["evidence_layer"]
        second_layer = second["metadata"]["evidence_layer"]
        self.assertIn("补贴标准", first_layer["span"]["quote"])
        self.assertIn("整车企业", second_layer["span"]["quote"])
        self.assertNotEqual(first_layer["fingerprint"], second_layer["fingerprint"])
        # 但"来源"是同一个：来源级指纹一致，跨轮去重照旧生效
        self.assertEqual(first_layer["source_fingerprint"], second_layer["source_fingerprint"])

    def test_span_fingerprint_is_deterministic(self):
        first = evidence_layer.annotate_evidence(_article_item())["metadata"]["evidence_layer"]
        second = evidence_layer.annotate_evidence(_article_item())["metadata"]["evidence_layer"]
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(len(first["fingerprint"]), 24)

    def test_content_fingerprint_matches_legacy_rule(self):
        """与改造前 qa_pipeline._dedupe_evidence 里的算法逐字一致。"""
        item = _article_item()
        text = " ".join(str(item.get(key) or "")
                        for key in ("title", "content_excerpt", "excerpt", "content"))
        expected = hashlib.sha256(" ".join(text.casefold().split())[:600].encode("utf-8")).hexdigest()[:24]
        self.assertEqual(evidence_layer.content_fingerprint(item), expected)
        self.assertEqual(evidence_layer.content_fingerprint({"title": "", "content_excerpt": "   "}), "")

    def test_dedupe_evidence_items_keeps_legacy_semantics(self):
        """既有四条去重键逐条验证：ref / article_id / source_url / 内容指纹（标题+正文）。"""
        base = _article_item(content_excerpt="正文 X")
        items = [
            base,
            _article_item(title="换个标题，但同一篇文章 id/url", content_excerpt="正文 X"),  # id/url/ref 撞
            # 镜像站：id/url/ref 都不同、标题与正文一样 → 靠内容指纹拦下
            _article_item(article_id=None, source_url="", evidence_ref="article:2",
                          content_excerpt="正文 X"),
            # 另一篇真不同的文章必须留下
            _article_item(article_id=7, source_url="https://example.com/b/2",
                          evidence_ref="article:7", title="完全不同的文章",
                          content_excerpt="另一篇完全不同的正文。"),
        ]
        kept = evidence_layer.dedupe_evidence_items(items, limit=10)
        self.assertEqual([item["evidence_ref"] for item in kept], ["article:12345", "article:7"])

    def test_dedupe_evidence_items_keeps_distinct_urls_with_distinct_content(self):
        items = [
            _article_item(article_id=1, evidence_ref="article:1", source_url="https://e.com/1",
                          content_excerpt="正文一"),
            _article_item(article_id=2, evidence_ref="article:2", source_url="https://e.com/2",
                          content_excerpt="正文二"),
        ]
        self.assertEqual(len(evidence_layer.dedupe_evidence_items(items, limit=10)), 2)

    def test_dedupe_evidence_items_honours_limit(self):
        items = [_article_item(article_id=index, evidence_ref="article:%d" % index,
                               source_url="https://example.com/%d" % index,
                               content_excerpt="正文 %d" % index) for index in range(1, 6)]
        self.assertEqual(len(evidence_layer.dedupe_evidence_items(items, limit=2)), 2)

    def test_dedupe_by_fingerprint_drops_exact_repeats(self):
        first = evidence_layer.annotate_evidence(_article_item())
        duplicate = evidence_layer.annotate_evidence(_article_item())
        other = evidence_layer.annotate_evidence(_graph_item())
        kept, audit = evidence_layer.dedupe_by_fingerprint([first, duplicate, other])
        self.assertEqual(len(kept), 2)
        self.assertEqual(audit["dropped_count"], 1)
        self.assertEqual(audit["dropped"][0]["evidence_ref"], "article:12345")

    def test_dedupe_by_fingerprint_on_raw_items_without_layer(self):
        """没标注过的证据也能去重（指纹可现算），失败路径不抛异常。"""
        kept, audit = evidence_layer.dedupe_by_fingerprint([_article_item(), _article_item()])
        self.assertEqual((len(kept), audit["dropped_count"]), (1, 1))
        self.assertEqual(evidence_layer.dedupe_by_fingerprint([None, "x"])[0], [])


class NodeKeyEquivalenceTests(unittest.TestCase):
    """实体键与既有图谱口径必须等价（自带实现，不许漂移）。"""

    def test_matches_qa_retrieval_graph_node_key(self):
        from qa_retrieval import _graph_node_key

        for sample in ("央行（PBOC）2026", " 某 车企 ", "A股/港股", "", "新能源补贴政策", None):
            self.assertEqual(evidence_layer.node_key(sample), _graph_node_key(sample), sample)


class LayerVersionTests(unittest.TestCase):
    def test_layer_version_is_exported(self):
        self.assertEqual(EVIDENCE_LAYER_VERSION, "qa-evidence-v1")


if __name__ == "__main__":
    unittest.main()

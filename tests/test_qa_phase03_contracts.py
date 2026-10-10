#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph-rag-v2 通用包 Phase 03 · 契约与守门（不许放宽任何既有 schema 的严格性）。

这一份专测"边界"，不测业务判定：
  1. 七个**冻结契约**指纹复算必须与 P00-02 一字不差（EVIDENCE/CLAIM/CONFLICT/LEVEL1/LEVEL2/
     FINAL_ANSWER/QA_EVENT），`EVIDENCE_SCHEMA.additionalProperties` 仍然是 False；
  2. 核验结论只能落在 `metadata.evidence_layer.verification`（可选字段）——证据条目顶层键集
     不变，核验过的证据照样过 `validate_level1_result`；
  3. 核验结论的取值域与原因码单一事实源（verdict ∈ EVIDENCE_STATUSES、原因码都有中文解释）；
  4. 库表结构**本阶段不动**：`QA_SCHEMA_VERSION` 仍是 v7、`QA_ADDED_COLUMNS_V6` 没有新增、
     核验缓存复用既有 `qa_retrieval_cache` 表（新表/新列为零）。
"""
import hashlib
import json
import os
import sys
import unittest

os.environ.setdefault("DATABASE_TYPE", "sqlite")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_contracts  # noqa: E402
import qa_graph_contracts as contracts  # noqa: E402
import qa_schema  # noqa: E402
import qa_verifier as verifier  # noqa: E402
from qa_evidence import annotate_evidence  # noqa: E402
from qa_graph_contracts import EVIDENCE_STATUSES, validate  # noqa: E402
from qa_level1 import empty_level1_result  # noqa: E402

# P00-02 冻结的七个契约指纹（baseline/qa-baseline-inventory.json 同口径：规范化 JSON 的 sha256 前 16 位）
FROZEN_FINGERPRINTS = {
    "EVIDENCE_SCHEMA": "370301331c02c738",
    "CLAIM_SCHEMA": "06fcdb02441248b2",
    "CONFLICT_SCHEMA": "aabd3259b07f9a3e",
    "LEVEL1_RESULT_SCHEMA": "7e864764429db111",
    "LEVEL2_RESULT_SCHEMA": "a0484f894e7bc9d6",
    "FINAL_ANSWER_SCHEMA": "4d1efa54ca1cbc1a",
    "QA_EVENT_SCHEMA": "59358bfa88a6c6af",
}


def _fingerprint(value) -> str:
    """与 tools/qa_baseline_inventory.py 完全同口径（sort_keys + ensure_ascii=False）。"""
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _article_item(**overrides):
    item = {
        "evidence_ref": "article:1",
        "source_type": "article",
        "title": "宁德时代10月9日股价大涨原因解析",
        "source_url": "https://example.com/a",
        "article_id": 1,
        "content_excerpt": "10月9日，宁德时代股价大涨5.2%，主要因为固态电池量产消息与储能订单增长。",
        "published_at": "2026-10-09",
        "authority_level": 60,
        "score": 30.0,
        "retrieval_method": "keyword",
        "relationship": "supports",
        "metadata": {"matched_keywords": ["宁德时代"]},
    }
    item.update(overrides)
    return item


class FrozenContractTests(unittest.TestCase):
    def test_seven_schema_fingerprints_unchanged(self):
        for name, expected in FROZEN_FINGERPRINTS.items():
            schema = getattr(qa_contracts, name)
            self.assertEqual(_fingerprint(schema), expected,
                             "%s 的冻结指纹变了（P00-02 契约被改动）" % name)

    def test_evidence_schema_still_strict(self):
        self.assertIs(qa_contracts.EVIDENCE_SCHEMA.get("additionalProperties"), False,
                      "EVIDENCE_SCHEMA 不许放宽 additionalProperties")
        self.assertEqual(
            sorted(qa_contracts.EVIDENCE_SCHEMA["required"]),
            sorted(["evidence_ref", "source_type", "title", "source_url", "content_excerpt",
                    "metadata"]))

    def test_claim_schema_still_has_the_status_enum(self):
        enum = qa_contracts.CLAIM_SCHEMA["properties"]["verification_status"]["enum"]
        for value in ("unverified", "confirmed", "qualified", "conflicted", "insufficient_evidence"):
            self.assertIn(value, enum, "claim 状态枚举被改动了：%s" % value)


class VerificationSchemaTests(unittest.TestCase):
    def _verification(self, **overrides):
        value = {
            "verifier_version": verifier.VERIFIER_VERSION,
            "config_hash": verifier.config_hash(),
            "verdict": "SUPPORTED",
            "verified": True,
            "score": 0.71,
            "dimensions": {"relevance": 0.8},
            "reasons": [verifier.REASON_SUPPORTED],
            "reason_text": "结论的关键实词在证据片段里成段出现，判为支持。",
            "nli": {"backend": "rule", "entailment": 0.75},
            "checks": [{"check": "negation", "passed": True}],
            "text_source": "excerpt",
            "cache": "miss",
        }
        value.update(overrides)
        return value

    def test_schema_accepts_valid_and_rejects_bad_verdict(self):
        ok, note = validate("evidence_verification", self._verification())
        self.assertTrue(ok, note)
        ok, note = validate("evidence_verification", self._verification(verdict="BOGUS"))
        self.assertFalse(ok)
        self.assertIn("verdict", note)
        ok, note = validate("evidence_verification", {"verdict": "SUPPORTED"})
        self.assertFalse(ok, "缺 required 字段必须被拦")

    def test_verdict_enum_is_the_single_source_of_truth(self):
        enum = contracts.EVIDENCE_VERIFICATION_SCHEMA["properties"]["verdict"]["enum"]
        self.assertEqual(list(enum), list(EVIDENCE_STATUSES))
        self.assertEqual(list(verifier.VERDICTS), list(EVIDENCE_STATUSES))

    def test_verification_is_optional_on_evidence_object(self):
        layer = {"evidence_ref": "article:1", "status": "SUPPORTED",
                 "span": {"start": 0, "end": 1, "quote": "q"},
                 "fingerprint": "f", "source": {"source_id": "s", "source_type": "article"}}
        ok, note = validate("evidence_object", layer)
        self.assertTrue(ok, note)
        ok, note = validate("evidence_object", dict(layer, verification=self._verification()))
        self.assertTrue(ok, note)

    def test_verified_evidence_still_passes_level1_contract(self):
        """最要紧的回归：核验过的证据（含嵌套 verification）必须还能过冻结契约。"""
        annotated = annotate_evidence(_article_item())
        verified, audit = verifier.verify_evidence_batch(
            [annotated], claim_text="宁德时代10月9日股价大涨的原因",
            cache=verifier.VerificationCache())
        self.assertEqual(audit["checked"], 1)
        result = qa_contracts.validate_level1_result(empty_level1_result("草稿", verified))
        self.assertEqual(len(result["evidence"]), 1)
        self.assertEqual(result["evidence"][0]["metadata"]["evidence_layer"]["status"],
                         "SUPPORTED", "Phase 02 的 status 语义不许被核验层改掉")
        self.assertIn("verification", result["evidence"][0]["metadata"]["evidence_layer"])

    def test_top_level_keys_unchanged_after_verification(self):
        original = _article_item()
        verified, _audit = verifier.verify_evidence_batch(
            [annotate_evidence(original)], claim_text="宁德时代股价大涨",
            cache=verifier.VerificationCache())
        self.assertEqual(set(verified[0].keys()), set(original.keys()) - {"evidence_layer"},
                         "核验不许在证据条目顶层加字段")
        self.assertEqual(set(original.keys()), set(verified[0].keys()))

    def test_every_reason_code_is_registered_and_documented(self):
        self.assertEqual(len(set(verifier.VERIFICATION_REASONS)), len(verifier.VERIFICATION_REASONS))
        for code in verifier.VERIFICATION_REASONS:
            self.assertIn(code, verifier.REASON_TEXTS, "原因码 %s 没有中文解释" % code)

    def test_runtime_reasons_are_within_the_registry(self):
        """真跑一遍：跑出来的原因码必须都在登记表里（不许偷偷造新码）。"""
        cases = [
            ("宁德时代10月9日股价大涨，原因是固态电池量产", _article_item()),
            ("宁德时代股价大涨的原因", _article_item(
                content="比亚迪销量创新高，芯片供应紧张。", title="宁德时代与比亚迪的差距")),
            ("医保新规不适用于民营医院", _article_item(content="医保新规适用于民营医院。")),
            ("2025年补贴提高到30%", _article_item(content="2025年补贴提高到20%。")),
            ("", _article_item()),
        ]
        cache = verifier.VerificationCache()
        for claim, item in cases:
            result = verifier.verify_evidence_item(item, claim_text=claim, cache=cache)
            for code in result["reasons"]:
                self.assertIn(code, verifier.VERIFICATION_REASONS, "未登记的原因码：%s" % code)
            self.assertIn(result["verdict"], EVIDENCE_STATUSES)


class SchemaVersionGuardTests(unittest.TestCase):
    """库表结构：本阶段**不动**（核验缓存复用既有 qa_retrieval_cache 表）。"""

    def test_schema_version_unchanged(self):
        self.assertEqual(qa_schema.QA_SCHEMA_VERSION, "unified-qa-schema-v8",
                         "阶段 03 自身零库表变更；v8 是 Phase 09 的变更（八张 memory_* 表），本条仍钉死字面量")

    def test_no_new_table_or_column(self):
        self.assertIn("qa_retrieval_cache", qa_schema.QA_REQUIRED_TABLES)
        self.assertEqual(len(qa_schema.QA_ADDED_COLUMNS_V6), 15,
                         "ADD COLUMN 清单被改动了（阶段 03 声明零迁移）")
        self.assertNotIn("qa_verification", " ".join(qa_schema.QA_TABLE_DDL),
                         "不许为核验缓存新建表")

    def test_cache_namespace_is_distinct_from_retrieval(self):
        self.assertEqual(verifier.CACHE_NAMESPACE, "qa_verification")


if __name__ == "__main__":
    unittest.main()

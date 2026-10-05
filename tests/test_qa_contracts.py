#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest

from qa_contracts import (
    QA_CONTRACT_VERSION,
    QA_SSE_PROTOCOL_VERSION,
    QaContractError,
    normalize_qa_request,
    validate_final_answer,
    validate_level1_result,
    validate_level2_result,
    validate_qa_event,
)
from qa_sse import encode_qa_sse, qa_event


def evidence(ref="article:1"):
    return {
        "evidence_ref": ref,
        "source_type": "article",
        "title": "离岸信托政策原文",
        "source_url": "https://example.com/policy",
        "content_excerpt": "政策正文摘要",
        "published_at": "2026-01-01",
        "fetched_at": "2026-10-01T00:00:00Z",
        "article_id": 1,
        "ragflow_kb_id": None,
        "document_id": None,
        "chunk_id": None,
        "score": 0.91,
        "authority_level": 5,
        "retrieval_method": "keyword",
        "match_reason": "命中离岸信托",
        "relationship": "supports",
        "metadata": {},
    }


def claim(ref="article:1", claim_id="c1"):
    return {
        "claim_id": claim_id,
        "text": "该政策适用于部分离岸信托安排。",
        "claim_type": "current_fact",
        "confidence": 0.8,
        "valid_from": "2026-01-01",
        "valid_to": None,
        "scope": ["离岸信托"],
        "evidence_refs": [ref],
        "needs_verification": True,
        "verification_status": "unverified",
    }


class QaContractTests(unittest.TestCase):
    def test_two_origins_normalize_to_the_same_business_request(self):
        payload = {
            "session_id": "session-1",
            "question": "  这项政策有什么影响？  ",
            "industry_pack_id": "family_office",
            "draft_provider": "Gemini",
            "web_search": True,
            "page_context": {"article_id": 1},
        }
        getinfo = normalize_qa_request(payload, trusted_origin="getinfo_ui")
        ragflow = normalize_qa_request(payload, trusted_origin="ragflow_ui")
        self.assertEqual(getinfo["mode"], "standard")
        self.assertEqual(getinfo["draft_provider"], "gemini")
        self.assertEqual(getinfo["question"], "这项政策有什么影响？")
        self.assertEqual(
            {key: value for key, value in getinfo.items() if key != "origin"},
            {key: value for key, value in ragflow.items() if key != "origin"},
        )

    def test_request_rejects_privileged_fields_and_invalid_values(self):
        base = {"question": "政策？", "industry_pack_id": "family_office"}
        with self.assertRaisesRegex(QaContractError, "受保护字段"):
            normalize_qa_request({**base, "ragflow_kb_id": "another-tenant"})
        with self.assertRaisesRegex(QaContractError, "未知问答模式"):
            normalize_qa_request({**base, "mode": "auto"})
        with self.assertRaisesRegex(QaContractError, "问题不能为空"):
            normalize_qa_request({**base, "question": " "})
        with self.assertRaisesRegex(QaContractError, "真实问题"):
            normalize_qa_request({
                **base,
                "question": "以下是未经核验的旧会话片段，仅作为问题背景。请重新检索、核验并回答\n\n我的后续问题：",
            })
        with self.assertRaisesRegex(QaContractError, "真实问题"):
            normalize_qa_request({**base, "question": "请重新检索核验并回答"})
        self.assertEqual(
            normalize_qa_request({**base, "question": "政策？"})["question"],
            "政策？",
        )

    def test_level1_schema_and_reference_integrity(self):
        value = {
            "contract_version": QA_CONTRACT_VERSION,
            "draft_answer": "初步判断。",
            "claims": [claim()],
            "entities": ["离岸信托"],
            "timeline_hints": [],
            "gaps": ["需要核验实施范围"],
            "followup_queries": ["官方原文"],
            "evidence": [evidence()],
            "citations": ["article:1"],
        }
        self.assertEqual(validate_level1_result(value)["claims"][0]["claim_id"], "c1")
        broken = {**value, "claims": [claim("article:missing")]}
        with self.assertRaisesRegex(QaContractError, "不存在的 evidence_ref"):
            validate_level1_result(broken)
        with self.assertRaises(QaContractError):
            validate_level1_result({**value, "extra": "forbidden"})

    def test_level2_and_final_contracts(self):
        ev = evidence("ragflow:doc:chunk")
        ev["source_type"] = "ragflow_chunk"
        ev["ragflow_kb_id"] = "news"
        ev["document_id"] = "doc"
        ev["chunk_id"] = "chunk"
        c = claim("ragflow:doc:chunk", "l2-c1")
        c["verification_status"] = "confirmed"
        c["needs_verification"] = False
        level2 = {
            "contract_version": QA_CONTRACT_VERSION,
            "confirmed_claims": [c],
            "corrected_claims": [],
            "new_findings": [],
            "timeline": [],
            "horizontal_comparisons": [],
            "conflicts": [],
            "multi_hop_findings": [],
            "evidence_gaps": [],
            "evidence": [ev],
            "citations": ["ragflow:doc:chunk"],
        }
        self.assertEqual(len(validate_level2_result(level2)["confirmed_claims"]), 1)
        final = {
            "contract_version": QA_CONTRACT_VERSION,
            "status": "ready",
            "answer": "综合结论。",
            "sections": {"summary": "综合结论。"},
            "claims": [c],
            "conflicts": [],
            "evidence": [ev],
            "citations": ["ragflow:doc:chunk"],
            "cutoff_at": "2026-10-01T00:00:00Z",
            "degraded": False,
            "degradation_reasons": [],
            "models": {"draft": "local", "research": "ragflow", "synthesis": "local"},
        }
        self.assertEqual(validate_final_answer(final)["status"], "ready")

    def test_sse_event_is_versioned_and_encoded_for_resume(self):
        event = qa_event(
            event_type="stage_started",
            run_id="run-1",
            event_id=4,
            stage="level1_retrieval",
            payload={"message": "正在一级检索"},
        )
        self.assertEqual(validate_qa_event(event)["protocol_version"], QA_SSE_PROTOCOL_VERSION)
        encoded = encode_qa_sse(event)
        self.assertTrue(encoded.startswith("id: 4\nevent: stage_started\ndata: "))
        self.assertIn('"run_id":"run-1"', encoded)
        with self.assertRaises(QaContractError):
            validate_qa_event({**event, "type": "private_chain_of_thought"})


if __name__ == "__main__":
    unittest.main()

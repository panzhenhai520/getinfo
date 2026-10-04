#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Safe, bounded DTOs shared by both unified-QA front ends."""

from __future__ import annotations

import html
import re
from typing import Mapping

from qa_retrieval import canonical_http_url


def _plain(value, limit: int) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]{0,1000}>", "", text)
    text = " ".join(text.replace("\x00", " ").split())
    return text[:limit]


def evidence_card(item: Mapping) -> dict:
    excerpt = _plain(item.get("content_excerpt"), 500)
    return {
        "evidence_ref": str(item.get("evidence_ref") or ""),
        "source_type": str(item.get("source_type") or ""),
        "title": _plain(item.get("title"), 300),
        "source_url": canonical_http_url(str(item.get("source_url") or "")),
        "excerpt": excerpt[:500],
        "published_at": item.get("published_at"),
        "score": item.get("score"),
        "authority_level": item.get("authority_level"),
        "retrieval_method": item.get("retrieval_method"),
        "match_reason": _plain(item.get("match_reason"), 500),
        "relationship": str(item.get("relationship") or ""),
        "article_id": item.get("article_id"),
        "ragflow_kb_id": str(item.get("ragflow_kb_id") or ""),
        "document_id": str(item.get("document_id") or ""),
        "chunk_id": str(item.get("chunk_id") or ""),
        "metadata": {
            key: value for key, value in dict(item.get("metadata") or {}).items()
            if key in {
                "domain", "category", "matched_keywords", "topic_tags", "provider",
                "axis", "claim_id", "chunk_count", "position",
            }
        },
    }


def stage_event_payload(stage: str, output: Mapping) -> dict:
    value = dict(output or {})
    if stage == "plan":
        return {key: value.get(key) for key in (
            "intent", "queries", "entities", "topics", "time_scope", "research_axes",
            "needs_local_articles", "needs_web", "needs_ragflow", "high_risk_policy",
        )}
    if stage in {"level1_retrieval", "level2_retrieval"}:
        evidence = list(value.get("evidence") or [])
        return {
            "queries": list(value.get("queries") or [])[:8],
            "hit_count": len(evidence),
            "evidence": [evidence_card(item) for item in evidence[:30]],
            "stats": dict(value.get("stats") or {}),
            "excluded": dict(value.get("excluded") or {}),
            "search_status": value.get("search_status"),
        }
    if stage == "level1_draft":
        return {
            "draft_answer": str(value.get("draft_answer") or "")[:30000],
            "claims": list(value.get("claims") or [])[:60],
            "gaps": list(value.get("gaps") or [])[:40],
            "followup_queries": list(value.get("followup_queries") or [])[:20],
            "evidence": [evidence_card(item) for item in list(value.get("evidence") or [])[:30]],
        }
    if stage == "level2_research":
        return {
            "confirmed_claims": list(value.get("confirmed_claims") or [])[:80],
            "corrected_claims": list(value.get("corrected_claims") or [])[:80],
            "new_findings": list(value.get("new_findings") or [])[:80],
            "timeline": list(value.get("timeline") or [])[:100],
            "horizontal_comparisons": list(value.get("horizontal_comparisons") or [])[:100],
            "conflicts": list(value.get("conflicts") or [])[:60],
            "multi_hop_findings": list(value.get("multi_hop_findings") or [])[:60],
            "evidence_gaps": list(value.get("evidence_gaps") or [])[:60],
            "evidence": [evidence_card(item) for item in list(value.get("evidence") or [])[:50]],
        }
    if stage == "conflict_review":
        return {
            "version": value.get("version"),
            "claims": [
                {
                    "canonical_id": item.get("canonical_id"),
                    "text": str((item.get("claim") or {}).get("text") or "")[:1000],
                    "verification_status": (item.get("claim") or {}).get("verification_status"),
                    "authority_level": item.get("authority_level"),
                }
                for item in list(value.get("claims") or [])[:120]
            ],
            "conflicts": list(value.get("conflicts") or [])[:60],
            "stats": dict(value.get("stats") or {}),
        }
    if stage in {"synthesis", "citation_validation"}:
        return {
            **{key: value.get(key) for key in (
                "contract_version", "status", "answer", "sections", "claims", "conflicts",
                "citations", "cutoff_at", "degraded", "degradation_reasons", "models",
                "citation_map",
            )},
            "evidence": [evidence_card(item) for item in list(value.get("evidence") or [])[:80]],
        }
    return value


__all__ = ["evidence_card", "stage_event_payload"]

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Resolve fail-closed RAGFlow upload behavior for an industry pack."""

from __future__ import annotations

from typing import Optional

from industry_packs import IndustryPackLoader, industry_pack_loader
from ragflow_kb_registry import resolve_ragflow_kb_id


def industry_ragflow_policy(
    industry_pack_id: str,
    *,
    pack_loader: Optional[IndustryPackLoader] = None,
) -> dict:
    loader = pack_loader or industry_pack_loader
    pack = loader.load(str(industry_pack_id or "family_office"))
    policy = dict(pack.get("ragflow_policy") or {})
    return {
        "industry_pack_id": pack["id"],
        "upload_crawled_articles": bool(
            policy.get("upload_crawled_articles", pack["id"] == "family_office")
        ),
        "knowledge_base_key": str(
            policy.get("knowledge_base_key") or "news"
        ).strip(),
    }


def resolve_industry_ragflow_kb_id(
    industry_pack_id: str,
    requested_kb_id: str = "",
    *,
    pack_loader: Optional[IndustryPackLoader] = None,
) -> str:
    """Return an actual KB id only when this industry explicitly allows upload."""

    policy = industry_ragflow_policy(
        industry_pack_id,
        pack_loader=pack_loader,
    )
    if not policy["upload_crawled_articles"]:
        return ""
    return resolve_ragflow_kb_id(
        policy["knowledge_base_key"],
        industry_pack_id=policy["industry_pack_id"],
        purpose="article_upload",
        requested_kb_id=str(requested_kb_id or "").strip(),
    )

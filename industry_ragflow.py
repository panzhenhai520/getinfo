#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Resolve fail-closed RAGFlow upload behavior for an industry pack."""

from __future__ import annotations

from typing import Optional

from industry_packs import IndustryPackLoader, industry_pack_loader


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
    if policy["knowledge_base_key"] != "news":
        return ""
    try:
        from chat_api import _load_config

        configured_news_id = str(_load_config().get("ragflow_kb_id") or "").strip()
    except Exception:
        configured_news_id = ""
    if configured_news_id:
        return configured_news_id
    # Existing family-office schedules can still carry the concrete historical
    # News KB id. New industry packs fail closed when News is not configured.
    if policy["industry_pack_id"] == "family_office":
        return str(requested_kb_id or "").strip()
    return ""

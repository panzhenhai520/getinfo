#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""One authoritative project-keyword pool for schedules and dashboard gates."""

from __future__ import annotations

from collections.abc import Iterable

from financial_evidence import _normalize_text, _term_occurs
from industry_pack_runtime import ActiveIndustryCompositionService


DEFAULT_PROJECT_KEYWORDS = (
    "家族办公室,家族信托,家族财富,财富传承,家族传承,家族治理,"
    "家族宪章,家族信托架构,信托架构,家办,单一家族办公室,联合家族办公室,"
    "高净值,财富管理,资产隔离,传承安排,跨境传承,税务豁免,"
    "family office,family trust"
)

REMOVED_PROJECT_KEYWORDS = frozenset({
    "基金", "保险规划", "金融资本", "投资", "投资范围", "私募基金",
    "证监会", "基金经理", "家族客户", "家族投资控权",
})


def configured_project_keyword_snapshot(connection, *, pack_loader=None) -> dict:
    """Return the exact active published pack keywords and their provenance."""

    class _ConnectionDatabase:
        def __init__(self, value):
            import threading

            self.connection = value
            self.lock = threading.RLock()

        def _ensure_connection(self):
            return None

    runtime = ActiveIndustryCompositionService(
        _ConnectionDatabase(connection), pack_loader=pack_loader
    ).snapshot()
    return {
        "keywords": list(runtime["project_keywords"]),
        "industry_pack_id": runtime["active_industry_pack_id"],
        "industry_pack_version_id": runtime["active_industry_pack_version_id"],
        "activation_id": runtime["active_industry_activation_id"],
        "source": runtime["keyword_source"],
        "fields": runtime["keyword_fields"],
    }


def configured_project_keywords(connection, *, pack_loader=None) -> list[str]:
    """Match the active industry-pack keyword pool shown by `/batch-schedule`."""

    return configured_project_keyword_snapshot(
        connection, pack_loader=pack_loader
    )["keywords"]


def matched_project_keywords(keywords: Iterable[str], *texts: object) -> list[str]:
    """Return configured terms that actually occur in the article evidence."""

    normalized_texts = tuple(_normalize_text(text) for text in texts if str(text or "").strip())
    return [
        keyword
        for keyword in keywords
        if any(_term_occurs(keyword, text) for text in normalized_texts)
    ]

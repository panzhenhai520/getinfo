#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Server-owned unified-QA policy resolution."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Callable, Mapping


@dataclass(frozen=True)
class QaPolicy:
    industry_pack_id: str
    default_mode: str
    ragflow_kb_id: str
    ragflow_app_id: str
    synthesis_provider_id: str
    standard_max_hops: int
    deep_max_hops: int
    max_queries_per_hop: int
    max_evidence: int
    research_timeout_seconds: int

    def to_dict(self) -> dict:
        return asdict(self)


class QaPolicyError(ValueError):
    pass


def _integer(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


class QaPolicyResolver:
    def __init__(self, runtime_loader: Callable[[str], Mapping] | None = None):
        self.runtime_loader = runtime_loader

    def _runtime(self, pack_id: str) -> dict:
        if self.runtime_loader is not None:
            return dict(self.runtime_loader(pack_id) or {})
        try:
            from pack_tenant import pack_runtime

            return dict(pack_runtime(pack_id) or {})
        except Exception:
            return {}

    def resolve(self, industry_pack_id: str) -> QaPolicy:
        pack_id = str(industry_pack_id or "").strip()
        if not pack_id:
            raise QaPolicyError("industry_pack_id is required")
        runtime = self._runtime(pack_id)
        kb_id = str(runtime.get("ragflow_kb_id") or os.getenv("RAGFLOW_KB_ID", "")).strip()
        app_id = str(runtime.get("ragflow_app_id") or os.getenv("RAGFLOW_LLM_APP_ID", "")).strip()
        return QaPolicy(
            industry_pack_id=pack_id,
            default_mode="standard",
            ragflow_kb_id=kb_id,
            ragflow_app_id=app_id,
            synthesis_provider_id=str(
                runtime.get("qa_synthesis_provider")
                or os.getenv("QA_SYNTHESIS_PROVIDER", "local")
            ).strip().casefold() or "local",
            standard_max_hops=_integer(os.getenv("QA_STANDARD_MAX_HOPS", "1"), 1, 1, 2),
            deep_max_hops=_integer(os.getenv("QA_DEEP_MAX_HOPS", "2"), 2, 1, 2),
            max_queries_per_hop=_integer(os.getenv("QA_MAX_QUERIES_PER_HOP", "5"), 5, 1, 8),
            max_evidence=_integer(os.getenv("QA_MAX_EVIDENCE", "30"), 30, 5, 100),
            research_timeout_seconds=_integer(
                runtime.get("qa_research_timeout") or os.getenv("QA_RESEARCH_TIMEOUT_SECONDS", "90"),
                90,
                15,
                300,
            ),
        )

    def require_research_ready(self, industry_pack_id: str) -> QaPolicy:
        policy = self.resolve(industry_pack_id)
        missing = []
        if not policy.ragflow_kb_id:
            missing.append("ragflow_kb_id")
        if not policy.ragflow_app_id:
            missing.append("ragflow_app_id")
        if missing:
            raise QaPolicyError("二级研究配置缺失: " + ", ".join(missing))
        return policy


__all__ = ["QaPolicy", "QaPolicyError", "QaPolicyResolver"]

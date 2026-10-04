#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSE encoding helpers for the unified QA protocol."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Mapping

from qa_contracts import QA_SSE_PROTOCOL_VERSION, validate_qa_event


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def qa_event(*, event_type: str, run_id: str, event_id: int, stage: str, payload: Mapping | None = None) -> dict:
    return validate_qa_event(
        {
            "protocol_version": QA_SSE_PROTOCOL_VERSION,
            "type": str(event_type),
            "run_id": str(run_id),
            "event_id": int(event_id),
            "stage": str(stage),
            "timestamp": utc_now_text(),
            "payload": dict(payload or {}),
        }
    )


def encode_qa_sse(event: Mapping) -> str:
    normalized = validate_qa_event(event)
    body = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    return f"id: {normalized['event_id']}\nevent: {normalized['type']}\ndata: {body}\n\n"


__all__ = ["encode_qa_sse", "qa_event", "utc_now_text"]

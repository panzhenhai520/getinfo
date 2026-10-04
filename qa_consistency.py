#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic comparison helpers for the two unified-QA hosts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


VOLATILE_KEYS = frozenset({
    "id", "run_id", "job_id", "event_id", "timestamp", "created_at", "updated_at",
    "completed_at", "started_at", "fetched_at", "request_id", "request_ids",
    "session_id", "origin", "trace_id", "latency_ms", "duration_ms",
})


def normalize_business_output(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): normalize_business_output(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in VOLATILE_KEYS
        }
    if isinstance(value, list):
        normalized = [normalize_business_output(item) for item in value]
        if all(isinstance(item, Mapping) for item in normalized):
            return sorted(normalized, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=str))
        return normalized
    return value


def business_fingerprint(value: Any) -> str:
    body = json.dumps(normalize_business_output(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def compare_origin_outputs(getinfo_output: Any, ragflow_output: Any) -> dict:
    left = normalize_business_output(getinfo_output)
    right = normalize_business_output(ragflow_output)
    return {
        "equal": left == right,
        "getinfo_fingerprint": business_fingerprint(left),
        "ragflow_fingerprint": business_fingerprint(right),
    }


def verify_bundle_directory(path: str | Path) -> dict:
    root = Path(path)
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    mismatches = []
    for filename, metadata in (manifest.get("files") or {}).items():
        target = root / filename
        actual = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else "missing"
        if actual != metadata.get("sha256"):
            mismatches.append({"file": filename, "expected": metadata.get("sha256"), "actual": actual})
    return {
        "valid": not mismatches,
        "version": manifest.get("version"),
        "entry": manifest.get("entry"),
        "mismatches": mismatches,
    }


__all__ = [
    "VOLATILE_KEYS", "business_fingerprint", "compare_origin_outputs",
    "normalize_business_output", "verify_bundle_directory",
]

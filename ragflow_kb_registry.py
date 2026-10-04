#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Config-backed RAGFlow knowledge-base registry.

Industry packs reference a stable logical key such as ``news``.  This registry
maps that key to the concrete KB id used by upload and QA retrieval.  Keeping
the mapping here avoids hard-coding one global KB into every pack.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from typing import Mapping

import config


KB_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def normalize_kb_key(value: str, *, default: str = "news") -> str:
    key = str(value or "").strip().casefold()
    if not key:
        key = str(default or "news").strip().casefold()
    if not KB_KEY_PATTERN.fullmatch(key):
        raise ValueError("knowledge_base_key must match ^[a-z0-9][a-z0-9_-]{0,63}$")
    return key


def _registry_path() -> str:
    return os.path.join(config.APP_BASE_DIR, "config", "ragflow_kb_registry.json")


@lru_cache(maxsize=1)
def load_ragflow_kb_registry() -> dict:
    path = _registry_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        data = {}
    bases = data.get("knowledge_bases") if isinstance(data.get("knowledge_bases"), Mapping) else {}
    clean_bases = {}
    for key, raw in bases.items():
        try:
            normalized = normalize_kb_key(str(key))
        except ValueError:
            continue
        item = dict(raw or {}) if isinstance(raw, Mapping) else {}
        item["key"] = normalized
        item["enabled"] = bool(item.get("enabled", True))
        item["purposes"] = [str(value) for value in item.get("purposes") or [] if str(value).strip()]
        item["allowed_pack_ids"] = [str(value) for value in item.get("allowed_pack_ids") or [] if str(value).strip()]
        clean_bases[normalized] = item
    default_key = normalize_kb_key(str(data.get("default_key") or "news"))
    return {"version": int(data.get("version") or 1), "default_key": default_key, "knowledge_bases": clean_bases}


def clear_ragflow_kb_registry_cache() -> None:
    load_ragflow_kb_registry.cache_clear()


def get_ragflow_kb_entry(knowledge_base_key: str) -> dict:
    registry = load_ragflow_kb_registry()
    key = normalize_kb_key(knowledge_base_key, default=str(registry.get("default_key") or "news"))
    entry = dict((registry.get("knowledge_bases") or {}).get(key) or {})
    entry.setdefault("key", key)
    entry.setdefault("enabled", False)
    entry.setdefault("purposes", [])
    entry.setdefault("allowed_pack_ids", [])
    return entry


def _legacy_config_value(field: str) -> str:
    field = str(field or "").strip()
    if not field:
        return ""
    try:
        from chat_api import _load_config

        return str((_load_config() or {}).get(field) or "").strip()
    except Exception:
        return ""


def resolve_ragflow_kb_id(
    knowledge_base_key: str,
    *,
    industry_pack_id: str = "",
    purpose: str = "qa_retrieval",
    runtime_kb_id: str = "",
    requested_kb_id: str = "",
) -> str:
    """Resolve a logical KB key to a concrete KB id, failing closed."""

    entry = get_ragflow_kb_entry(knowledge_base_key)
    if not entry.get("enabled"):
        return ""
    purpose = str(purpose or "").strip()
    purposes = set(entry.get("purposes") or [])
    if purposes and purpose and purpose not in purposes:
        return ""
    pack_id = str(industry_pack_id or "").strip()
    allowed = set(str(item) for item in entry.get("allowed_pack_ids") or [])
    if allowed and "*" not in allowed and pack_id not in allowed:
        return ""

    for candidate in (
        runtime_kb_id,
        entry.get("kb_id"),
        _legacy_config_value(str(entry.get("legacy_config_field") or "")),
    ):
        resolved = str(candidate or "").strip()
        if resolved:
            return resolved
    for env_name in entry.get("env") or []:
        resolved = str(os.getenv(str(env_name), "") or "").strip()
        if resolved:
            return resolved
    if bool(entry.get("allow_requested_kb_id")):
        return str(requested_kb_id or "").strip()
    return ""


__all__ = [
    "KB_KEY_PATTERN",
    "clear_ragflow_kb_registry_cache",
    "get_ragflow_kb_entry",
    "load_ragflow_kb_registry",
    "normalize_kb_key",
    "resolve_ragflow_kb_id",
]

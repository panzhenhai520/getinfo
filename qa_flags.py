#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hot-reloadable, audited unified-QA rollout and operational switches."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Mapping

from qa_schema import ensure_qa_tables
from qa_security import redact_sensitive


DEFAULT_FLAGS = {
    "enabled": True,
    "getinfo_ui_enabled": True,
    "ragflow_ui_enabled": True,
    "level2_enabled": True,
    "synthesis_enabled": True,
    "allowed_user_ids": [],
    "allowed_pack_ids": ["family_office"],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().casefold() not in {"0", "false", "no", "off"}


class QaFeatureFlags:
    def __init__(self, database):
        self.database = database

    def _ensure(self):
        self.database._ensure_connection()
        with self.database.lock:
            cursor = self.database.connection.cursor()
            try:
                ensure_qa_tables(cursor)
                self.database.connection.commit()
            finally:
                cursor.close()

    def snapshot(self) -> dict[str, Any]:
        values = dict(DEFAULT_FLAGS)
        values.update({
            "enabled": _env_bool("UNIFIED_QA_ENABLED", values["enabled"]),
            "getinfo_ui_enabled": _env_bool("UNIFIED_QA_GETINFO_UI_ENABLED", values["getinfo_ui_enabled"]),
            "ragflow_ui_enabled": _env_bool("UNIFIED_QA_RAGFLOW_UI_ENABLED", values["ragflow_ui_enabled"]),
            "level2_enabled": _env_bool("UNIFIED_QA_LEVEL2_ENABLED", values["level2_enabled"]),
            "synthesis_enabled": _env_bool("UNIFIED_QA_SYNTHESIS_ENABLED", values["synthesis_enabled"]),
        })
        self._ensure()
        with self.database.lock:
            rows = self.database.connection.execute("SELECT flag_key,value_json FROM qa_feature_flags").fetchall()
        for row in rows:
            if str(row[0]) not in values:
                continue
            try:
                values[str(row[0])] = json.loads(row[1])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        values["allowed_user_ids"] = [str(item) for item in values.get("allowed_user_ids") or []][:200]
        values["allowed_pack_ids"] = [str(item) for item in values.get("allowed_pack_ids") or []][:100]
        for name in ("enabled", "getinfo_ui_enabled", "ragflow_ui_enabled", "level2_enabled", "synthesis_enabled"):
            values[name] = bool(values.get(name))
        return values

    def evaluate(self, *, owner_user_id: str, industry_pack_id: str, origin: str) -> dict:
        flags = self.snapshot()
        reasons = []
        if not flags["enabled"]:
            reasons.append("global_disabled")
        origin_flag = "ragflow_ui_enabled" if origin == "ragflow_ui" else "getinfo_ui_enabled"
        if not flags[origin_flag]:
            reasons.append(f"{origin}_disabled")
        users = set(flags["allowed_user_ids"])
        packs = set(flags["allowed_pack_ids"])
        if users and owner_user_id not in users and industry_pack_id not in packs:
            reasons.append("not_in_rollout_allowlist")
        return {"allowed": not reasons, "reasons": reasons, **flags}

    def update(self, changes: Mapping[str, Any], *, actor_id: str) -> dict:
        allowed = set(DEFAULT_FLAGS)
        clean = {}
        for key, value in dict(changes or {}).items():
            if key not in allowed:
                continue
            clean[key] = [str(item) for item in value][:200] if key.startswith("allowed_") and isinstance(value, list) else bool(value)
        self._ensure()
        now = _now()
        with self.database.lock:
            for key, value in clean.items():
                self.database.connection.execute(
                    """INSERT INTO qa_feature_flags(flag_key,value_json,updated_at,updated_by)
                    VALUES(?,?,?,?) ON CONFLICT(flag_key) DO UPDATE SET
                    value_json=excluded.value_json,updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
                    (key, json.dumps(value, ensure_ascii=False), now, str(actor_id)),
                )
            self.database.connection.execute(
                """INSERT INTO qa_audit_events(
                    trace_id,run_id,event_type,actor_id,origin,industry_pack_id,payload_json,created_at
                ) VALUES('','','feature_flags_changed',?,'admin','',?,?)""",
                (str(actor_id), json.dumps(redact_sensitive(clean), ensure_ascii=False), now),
            )
            self.database.connection.commit()
        return self.snapshot()


__all__ = ["DEFAULT_FLAGS", "QaFeatureFlags"]

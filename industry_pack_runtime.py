#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""One authoritative snapshot of the currently activated industry composition."""

from __future__ import annotations

from typing import Optional

import config
from industry_packs import IndustryPackLoader, industry_pack_loader
from sqlite_database import sqlite_db


class ActiveIndustryCompositionService:
    def __init__(self, database=None, *, pack_loader: Optional[IndustryPackLoader] = None):
        self.db = database or sqlite_db
        self.pack_loader = pack_loader or industry_pack_loader

    def _settings(self) -> dict[str, str]:
        self.db._ensure_connection()
        keys = (
            "active_industry_pack_id",
            "active_industry_pack_version_id",
            "active_industry_activation_id",
        )
        with self.db.lock:
            rows = self.db.connection.execute(
                """
                SELECT setting_key, setting_value FROM intel_runtime_settings
                WHERE setting_key IN (?, ?, ?)
                """,
                keys,
            ).fetchall()
        values = {str(row[0]): str(row[1]) for row in rows}
        values.setdefault(
            "active_industry_pack_id",
            str(config.INTEL_DEFAULT_INDUSTRY_PACK or "family_office"),
        )
        values.setdefault("active_industry_pack_version_id", "")
        values.setdefault("active_industry_activation_id", "")
        return values

    def snapshot(self) -> dict:
        settings = self._settings()
        pack_id = settings["active_industry_pack_id"]
        composition = self.pack_loader.compose(pack_id)
        version_text = settings["active_industry_pack_version_id"]
        activation_id = settings["active_industry_activation_id"]
        primary = composition["primary_pack"]
        keywords = []
        seen = set()
        # Only anchor terms form the project gate. Generic trend/event words
        # such as “报告” or “增长” must not admit unrelated finance articles.
        for field in ("core_keywords", "expanded_keywords"):
            for raw in primary.get(field) or []:
                value = str(raw or "").strip()
                key = value.casefold()
                if value and key not in seen:
                    seen.add(key)
                    keywords.append(value)
        return {
            **composition,
            "active_industry_pack_id": pack_id,
            "active_industry_pack_version_id": (
                int(version_text) if version_text.isdigit() else None
            ),
            "active_industry_activation_id": activation_id,
            "project_keywords": keywords,
            "keyword_source": "active_published_industry_pack",
            "keyword_fields": ["core_keywords", "expanded_keywords"],
        }

    def job_context(self, *, declaring_pack_id: str = "") -> dict:
        snapshot = self.snapshot()
        return {
            "activation_id": snapshot["active_industry_activation_id"],
            "primary_industry_pack_id": snapshot["active_industry_pack_id"],
            "industry_pack_version_id": snapshot["active_industry_pack_version_id"],
            "declaring_pack_id": str(
                declaring_pack_id or snapshot["active_industry_pack_id"]
            ),
        }

    def stamp_payload(self, payload: Optional[dict] = None, *, declaring_pack_id: str = "") -> dict:
        result = dict(payload or {})
        for key, value in self.job_context(declaring_pack_id=declaring_pack_id).items():
            result.setdefault(key, value)
        return result


active_industry_composition_service = ActiveIndustryCompositionService()


def industry_pack_snapshot(pack_id: str) -> dict:
    """按指定行业包构造运行时快照（关键词等），供多包采集使用。

    与 snapshot() 的关键词口径保持一致（只取 core_keywords + expanded_keywords，
    泛化的 trend/event 词不得单独证明行业相关）。
    版本/激活 id 留空：非全局活跃包没有“当前激活版本”的概念，由调用方按需补齐。
    """
    composition = industry_pack_loader.compose(str(pack_id))
    primary = composition["primary_pack"]
    keywords, seen = [], set()
    for field in ("core_keywords", "expanded_keywords"):
        for raw in primary.get(field) or []:
            value = str(raw or "").strip()
            key = value.casefold()
            if value and key not in seen:
                seen.add(key)
                keywords.append(value)
    return {
        **composition,
        "active_industry_pack_id": str(pack_id),
        "active_industry_pack_version_id": None,
        "active_industry_activation_id": "",
        "project_keywords": keywords,
        "keyword_source": "industry_pack_by_id",
        "keyword_fields": ["core_keywords", "expanded_keywords"],
    }


def effective_active_composition() -> dict:
    return active_industry_composition_service.snapshot()


def _identity_from_pack(pack_id: str) -> dict:
    """按指定行业包构造首屏身份（名称/能力取自该包 manifest）。"""
    pack = industry_pack_loader.load(pack_id)
    capabilities = pack.get("dashboard_capabilities") or {}
    return {
        "id": str(pack_id),
        "name": str(pack.get("name") or pack_id),
        "pack_version": str(pack.get("pack_version") or ""),
        "activation_id": "",
        "show_spatiotemporal_map": bool(capabilities.get("show_spatiotemporal_map", True)),
    }


def active_industry_identity() -> dict:
    """Small fail-safe snapshot suitable for server-rendering the first frame.

    多租户隔离：当前登录的是「行业包用户」时，首屏必须渲染其授权行业包——
    否则不同行业的用户登录后会看到同一个（全局激活的）行业包，
    与 API 层 _industry_pack_id 已经做的按用户强制取包口径不一致。
    """
    fallback_id = str(config.INTEL_DEFAULT_INDUSTRY_PACK or "family_office")
    try:
        from pack_tenant import current_pack_id_or_none
        owned_pack_id = current_pack_id_or_none()
    except Exception:
        owned_pack_id = None
    if owned_pack_id:
        try:
            return _identity_from_pack(str(owned_pack_id))
        except Exception:
            pass  # 该包不可用时落回全局逻辑，避免首屏报错
    # 管理员没有行业包绑定，默认会落到"全局激活包"，于是看到的是与自己无关的行业首页。
    # 允许管理员在会话里选定要查看的行业包（登录后先选包），选定后首屏与接口都按该包渲染。
    # 放在包用户之后：包用户永远以自身绑定为准，不受这个会话值影响。
    try:
        from flask import has_request_context, session as _session
        if has_request_context():
            chosen_pack_id = str(_session.get("admin_industry_pack_id") or "").strip()
            if chosen_pack_id:
                return _identity_from_pack(chosen_pack_id)
    except Exception:
        pass
    try:
        snapshot = active_industry_composition_service.snapshot()
        primary = snapshot["primary_pack"]
        capabilities = primary.get("dashboard_capabilities") or {}
        return {
            "id": str(snapshot["active_industry_pack_id"]),
            "name": str(primary.get("name") or snapshot["active_industry_pack_id"]),
            "pack_version": str(primary.get("pack_version") or ""),
            "activation_id": str(snapshot.get("active_industry_activation_id") or ""),
            "show_spatiotemporal_map": bool(
                capabilities.get("show_spatiotemporal_map", True)
            ),
        }
    except Exception:
        try:
            pack = industry_pack_loader.load(fallback_id)
            capabilities = pack.get("dashboard_capabilities") or {}
            return {
                "id": fallback_id,
                "name": str(pack.get("name") or fallback_id),
                "pack_version": str(pack.get("pack_version") or ""),
                "activation_id": "",
                "show_spatiotemporal_map": bool(
                    capabilities.get("show_spatiotemporal_map", True)
                ),
            }
        except Exception:
            return {
                "id": fallback_id,
                "name": fallback_id,
                "pack_version": "",
                "activation_id": "",
            }

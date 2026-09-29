#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Global source-authority profiles shared by every industry pack.

Authority is evidence metadata.  It is intentionally not imported by the
industry keyword scorer and must never contribute to industry admission.
"""

from __future__ import annotations

import copy
import json
import os
from functools import lru_cache
from typing import Dict, Mapping
from urllib.parse import urlsplit

import config


REGISTRY_PATH = os.path.join(
    config.APP_BASE_DIR, "config", "source_authority_levels.json"
)


class SourceAuthorityError(ValueError):
    pass


@lru_cache(maxsize=1)
def authority_registry() -> Dict:
    with open(REGISTRY_PATH, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    roles = payload.get("roles")
    if not isinstance(roles, dict) or "unclassified" not in roles:
        raise SourceAuthorityError("source authority registry requires roles")
    for key, profile in roles.items():
        if not isinstance(profile, dict):
            raise SourceAuthorityError(f"source role {key} must be an object")
        weight = profile.get("weight")
        if not isinstance(weight, int) or weight < 1 or weight > 5:
            raise SourceAuthorityError(f"source role {key} weight must be 1..5")
        if not isinstance(profile.get("authority_scopes"), list):
            raise SourceAuthorityError(
                f"source role {key} authority_scopes must be an array"
            )
        if not isinstance(profile.get("can_resolve_conflicts"), bool):
            raise SourceAuthorityError(
                f"source role {key} can_resolve_conflicts must be boolean"
            )
    return payload


def source_authority_profiles() -> Dict[str, Dict]:
    return copy.deepcopy(authority_registry()["roles"])


def normalize_source_role(value: object, *, strict: bool = False) -> str:
    registry = authority_registry()
    role = str(value or "").strip().casefold()
    role = str((registry.get("aliases") or {}).get(role) or role)
    if not role:
        return "unclassified"
    if role not in registry["roles"]:
        if strict:
            raise SourceAuthorityError(f"unsupported source_role: {role}")
        return "unclassified"
    return role


def canonical_publisher_key(url: object, explicit: object = "") -> str:
    configured = str(explicit or "").strip().casefold()
    if configured:
        return configured[:255]
    host = (urlsplit(str(url or "")).hostname or "").casefold().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def infer_source_role(source: Mapping[str, object]) -> str:
    """Conservative fallback for legacy manifests without an explicit role."""

    name = str(source.get("name") or source.get("source_name") or "").casefold()
    url = str(source.get("url") or source.get("source_url") or "")
    host = (urlsplit(url).hostname or "").casefold()
    content_type = str(source.get("content_type") or "").casefold()
    authority = int(source.get("authority_level") or 0)

    if (
        host.endswith(".gov")
        or ".gov." in host
        or host.endswith("gov.hk")
        or host.endswith("gov.cn")
        or any(
            marker in host
            for marker in ("hkma.gov.hk", "mas.gov.sg", "nfra.gov.cn")
        )
    ):
        return "government_regulator"
    if any(
        marker in name
        for marker in (
            "交易所",
            "exchange",
            "披露易",
            "edgar",
        )
    ):
        return "exchange_official"
    if any(
        marker in name
        for marker in (
            "协会",
            "学会",
            "association",
            "foundation",
            "step",
        )
    ):
        return "industry_association"
    if (
        ".edu." in host
        or host.endswith(".edu")
        or any(marker in name for marker in ("大学", "学院", "university"))
    ):
        return "academic_research"
    if any(
        marker in name
        for marker in (
            "律师",
            " law",
            "kpmg",
            "deloitte",
            "pwc",
            " ey",
            "顾问",
            "咨询",
        )
    ):
        return "professional_advisor"
    if any(
        marker in name
        for marker in (
            "bloomberg",
            "财新",
            "信报",
            "新闻",
            "时报",
            "参考报",
            "magazine",
            "report",
            "briefing",
            "36氪",
            "界面",
            "联合早报",
        )
    ) or content_type == "media":
        return "professional_trade_media"
    if content_type == "report" or any(
        marker in name
        for marker in ("research", "研究", "pitchbook", "preqin", "智库")
    ):
        return "independent_research"
    if content_type == "official" or authority >= 3:
        return "issuer_official"
    if any(marker in name for marker in ("律师", "会计", "资产管理", "财富管理")):
        return "professional_advisor"
    return "unclassified"


def resolve_source_authority(
    source: Mapping[str, object], *, strict: bool = False
) -> Dict:
    configured_role = source.get("source_role")
    role = normalize_source_role(
        configured_role if str(configured_role or "").strip() else infer_source_role(source),
        strict=strict,
    )
    role_profile = authority_registry()["roles"][role]
    raw_level = source.get("authority_level")
    if raw_level in (None, ""):
        level = int(role_profile["weight"])
    else:
        try:
            level = int(raw_level)
        except (TypeError, ValueError) as exc:
            raise SourceAuthorityError("authority_level must be an integer") from exc
        if level < 1 or level > 5:
            raise SourceAuthorityError("authority_level must be within 1..5")
    raw_scopes = source.get("authority_scope")
    if raw_scopes in (None, ""):
        scopes = list(role_profile.get("authority_scopes") or [])
    elif not isinstance(raw_scopes, list) or any(
        not isinstance(item, str) for item in raw_scopes
    ):
        raise SourceAuthorityError("authority_scope must be an array of strings")
    else:
        scopes = []
        for value in raw_scopes:
            clean = str(value or "").strip()
            if clean and clean not in scopes:
                scopes.append(clean)
    return {
        "source_role": role,
        "source_role_label": str(role_profile.get("label") or role),
        "authority_level": level,
        "default_authority_level": int(role_profile["weight"]),
        "evidence_class": str(role_profile.get("evidence_class") or "unknown"),
        "authority_scope": scopes,
        "can_resolve_conflicts": bool(
            role_profile.get("can_resolve_conflicts", False)
        ),
        "publisher_key": canonical_publisher_key(
            source.get("url") or source.get("source_url"),
            source.get("publisher_key"),
        ),
    }


def normalize_manifest_source(
    source: Mapping[str, object], *, strict: bool = False
) -> Dict:
    item = copy.deepcopy(dict(source))
    profile = resolve_source_authority(item, strict=strict)
    for key in (
        "source_role",
        "authority_level",
        "authority_scope",
        "publisher_key",
    ):
        item[key] = copy.deepcopy(profile[key])
    return item

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Fail-closed authorization profiles for financial providers and RSS sources."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import config


ROOT = Path(__file__).resolve().parent
CATALOG_PATH = ROOT / "config" / "financial_source_licenses.json"
VALID_ENVIRONMENTS = {"development", "test", "production"}
REQUIRED_PROFILE_FIELDS = {
    "source_type",
    "source_id",
    "owner_role",
    "legal_review_owner_role",
    "cost_type",
    "billing_owner_role",
    "credential",
    "permissions",
    "quota",
    "reviewed_on",
    "review_due_on",
    "authorization_status",
    "production_allowed",
    "production_approval_required",
    "evidence_urls",
}


class FinancialSourceAuthorizationError(PermissionError):
    def __init__(self, source_id: str, profile_id: str, reason: str):
        self.source_id = str(source_id or "")
        self.profile_id = str(profile_id or "")
        self.reason = str(reason or "authorization_denied")
        super().__init__(
            f"financial source authorization denied: {self.source_id} ({self.reason})"
        )


def _setting(settings, name: str, default=None):
    source = config if settings is None else settings
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _canonical_url(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("license source URL must be an absolute HTTP(S) URL")
    host = parsed.hostname.rstrip(".").casefold().encode("idna").decode("ascii")
    port = parsed.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parsed.path or "/"
    query = urlencode(parse_qsl(parsed.query, keep_blank_values=True), doseq=True)
    return urlunsplit((scheme, host, path, query, ""))


def _utc_date(value=None) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("authorization clock must be timezone-aware")
        return value.astimezone(timezone.utc).date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _validate_profile(profile_id: str, raw: Mapping[str, object]) -> dict:
    profile = dict(raw)
    missing = sorted(REQUIRED_PROFILE_FIELDS - set(profile))
    if missing:
        raise ValueError(
            f"license profile {profile_id} missing fields: {', '.join(missing)}"
        )
    if profile["source_type"] not in {"provider", "rss"}:
        raise ValueError(f"license profile {profile_id} has invalid source_type")
    if not str(profile["source_id"] or "").strip():
        raise ValueError(f"license profile {profile_id} has no source_id")
    reviewed_on = date.fromisoformat(str(profile["reviewed_on"]))
    review_due_on = date.fromisoformat(str(profile["review_due_on"]))
    if review_due_on < reviewed_on:
        raise ValueError(f"license profile {profile_id} review dates are reversed")
    permissions = profile["permissions"]
    if not isinstance(permissions, Mapping) or set(permissions) != {
        "cache", "display", "redistribute"
    }:
        raise ValueError(f"license profile {profile_id} permissions are incomplete")
    credential = profile["credential"]
    if not isinstance(credential, Mapping) or set(credential) != {
        "required", "type", "owner_role", "acquisition_url"
    }:
        raise ValueError(f"license profile {profile_id} credential responsibility is incomplete")
    if credential["required"] and not all(
        str(credential[key] or "").strip()
        for key in ("type", "owner_role", "acquisition_url")
    ):
        raise ValueError(f"license profile {profile_id} required credential is unowned")
    if not isinstance(profile["quota"], Mapping) or not profile["quota"]:
        raise ValueError(f"license profile {profile_id} quota is missing")
    if not isinstance(profile["evidence_urls"], list) or not profile["evidence_urls"]:
        raise ValueError(f"license profile {profile_id} evidence URLs are missing")
    for evidence_url in profile["evidence_urls"]:
        _canonical_url(str(evidence_url))
    if profile["source_type"] == "rss":
        canonical = _canonical_url(str(profile.get("source_url") or ""))
        if canonical != str(profile.get("canonical_source_url") or ""):
            raise ValueError(f"license profile {profile_id} RSS identity is not canonical")
    profile["profile_id"] = profile_id
    return profile


def load_license_catalog(path: str | Path = CATALOG_PATH) -> dict:
    catalog = json.loads(Path(path).read_text(encoding="utf-8"))
    if catalog.get("schema_version") != "financial-source-license-v1":
        raise ValueError("financial source license catalog version mismatch")
    profiles = catalog.get("profiles")
    if not isinstance(profiles, Mapping) or not profiles:
        raise ValueError("financial source license catalog has no profiles")
    normalized = {
        str(profile_id): _validate_profile(str(profile_id), raw)
        for profile_id, raw in profiles.items()
    }
    return {**catalog, "profiles": normalized}


def license_profile(profile_id: str, *, catalog=None) -> dict:
    profiles = (catalog or load_license_catalog())["profiles"]
    profile = profiles.get(str(profile_id or ""))
    if profile is None:
        raise ValueError(f"unknown financial source license profile: {profile_id}")
    return dict(profile)


def provider_license_profile(provider_id: str, *, provider_profile=None, catalog=None) -> dict:
    normalized = str(provider_id or "").strip()
    raw = provider_profile
    if raw is None:
        raw = json.loads(
            (ROOT / "config" / "financial_providers" / f"{normalized}.json").read_text(
                encoding="utf-8"
            )
        )
    profile = license_profile(str(raw.get("license_profile") or ""), catalog=catalog)
    if profile["source_type"] != "provider" or profile["source_id"] != normalized:
        raise ValueError(f"provider {normalized} license profile identity mismatch")
    if int(profile["quota"].get("daily_calls", -1)) != int(raw["daily_call_budget"]):
        raise ValueError(f"provider {normalized} license quota does not match runtime profile")
    return profile


def rss_license_profile(source_url: str, profile_id: str, *, catalog=None) -> dict:
    profile = license_profile(profile_id, catalog=catalog)
    if profile["source_type"] != "rss":
        raise ValueError(f"license profile {profile_id} is not an RSS profile")
    if profile["canonical_source_url"] != _canonical_url(source_url):
        raise ValueError(f"RSS source does not match license profile {profile_id}")
    return profile


def _environment(settings) -> str:
    value = str(
        _setting(settings, "FINANCIAL_LICENSE_ENVIRONMENT", "development")
        or "development"
    ).strip().casefold()
    return value if value in VALID_ENVIRONMENTS else "invalid"


def _approvals(settings) -> set[str]:
    raw = _setting(settings, "FINANCIAL_SOURCE_LICENSE_APPROVALS", "")
    if isinstance(raw, (list, tuple, set, frozenset)):
        values = raw
    else:
        values = str(raw or "").split(",")
    return {str(value).strip() for value in values if str(value).strip()}


def _decision(profile: dict, settings, *, at=None) -> dict:
    environment = _environment(settings)
    today = _utc_date(at)
    reason = "authorized"
    authorized = True
    if environment == "invalid":
        authorized, reason = False, "invalid_license_environment"
    elif today > date.fromisoformat(profile["review_due_on"]):
        authorized, reason = False, "license_review_expired"
    elif environment == "production" and not profile["production_allowed"]:
        authorized, reason = False, "production_use_forbidden"
    elif environment == "production" and profile["production_approval_required"]:
        approvals = _approvals(settings)
        if profile["profile_id"] not in approvals and profile["source_id"] not in approvals:
            authorized, reason = False, "production_approval_missing"
    elif environment != "production":
        reason = "non_production_profile_valid"
    return {
        "authorized": authorized,
        "reason": reason,
        "environment": environment,
        "profile_id": profile["profile_id"],
        "source_id": profile["source_id"],
        "review_due_on": profile["review_due_on"],
        "production_allowed": bool(profile["production_allowed"]),
    }


def provider_authorization_decision(
    provider_id: str, settings=None, *, at=None, provider_profile=None, catalog=None
) -> dict:
    profile = provider_license_profile(
        provider_id, provider_profile=provider_profile, catalog=catalog
    )
    return _decision(profile, settings, at=at)


def rss_authorization_decision(
    source_url: str, profile_id: str, settings=None, *, at=None, catalog=None
) -> dict:
    environment = _environment(settings)
    if not str(profile_id or "").strip():
        return {
            "authorized": environment != "production",
            "reason": (
                "non_production_unprofiled_source"
                if environment != "production"
                else "license_profile_missing"
            ),
            "environment": environment,
            "profile_id": "",
            "source_id": _canonical_url(source_url),
            "review_due_on": "",
            "production_allowed": False,
        }
    try:
        profile = rss_license_profile(source_url, profile_id, catalog=catalog)
    except (OSError, TypeError, ValueError):
        return {
            "authorized": False,
            "reason": "license_profile_invalid",
            "environment": environment,
            "profile_id": str(profile_id or ""),
            "source_id": _canonical_url(source_url),
            "review_due_on": "",
            "production_allowed": False,
        }
    return _decision(profile, settings, at=at)


def require_provider_authorization(
    provider_id: str, settings=None, *, at=None, provider_profile=None, catalog=None
) -> dict:
    decision = provider_authorization_decision(
        provider_id,
        settings,
        at=at,
        provider_profile=provider_profile,
        catalog=catalog,
    )
    if not decision["authorized"]:
        raise FinancialSourceAuthorizationError(
            decision["source_id"], decision["profile_id"], decision["reason"]
        )
    return decision


def require_rss_authorization(
    source_url: str, profile_id: str, settings=None, *, at=None, catalog=None
) -> dict:
    decision = rss_authorization_decision(
        source_url, profile_id, settings, at=at, catalog=catalog
    )
    if not decision["authorized"]:
        raise FinancialSourceAuthorizationError(
            decision["source_id"], decision["profile_id"], decision["reason"]
        )
    return decision

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Ordered, fail-closed rollout policy for financial capabilities.

The policy is configuration-only.  It never migrates or deletes data, so a
rollback changes effective behavior without requiring a database rollback.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone

import config


UTC = timezone.utc
FINANCIAL_ROLLOUT_VERSION = "financial-rollout-v1"
FINANCIAL_ROLLOUT_STAGES = (
    ("off", "全部关闭"),
    ("rss", "金融 RSS"),
    ("snapshot_readonly", "金融快照只读"),
    ("dashboard", "Dashboard 金融专区"),
    ("ai_fact", "AI 金融事实路由"),
    ("stock_research", "单股研究"),
    ("index_research", "指数与市场研究"),
    ("history_review", "历史会话 ×/÷ 金融核验"),
    ("auto_research", "自动研究"),
    ("simulation_backtest", "模拟与回测"),
)
ROLLOUT_STAGE_KEYS = tuple(item[0] for item in FINANCIAL_ROLLOUT_STAGES)
ROLLOUT_STAGE_LABELS = dict(FINANCIAL_ROLLOUT_STAGES)
ROLLOUT_STAGE_INDEX = {
    stage: index for index, stage in enumerate(ROLLOUT_STAGE_KEYS)
}
ROLLOUT_CAPABILITY_REQUIREMENTS = {
    stage: stage for stage in ROLLOUT_STAGE_KEYS if stage != "off"
}
LEGACY_ROLLOUT_STAGE = ROLLOUT_STAGE_KEYS[-1]


class FinancialRolloutError(ValueError):
    pass


def _setting(settings, key: str, default=None):
    if isinstance(settings, Mapping):
        return settings.get(key, default)
    return getattr(settings, key, default)


def _parse_utc(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("rollout clock must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_rollout_stage(value, *, strict: bool = False) -> str:
    stage = str(value or "").strip().casefold()
    if stage in ROLLOUT_STAGE_INDEX:
        return stage
    if strict:
        raise FinancialRolloutError(
            "FINANCIAL_ROLLOUT_STAGE 必须是: " + ", ".join(ROLLOUT_STAGE_KEYS)
        )
    return "off"


def _configured_stage(settings) -> tuple[str, bool, str]:
    # Missing values keep upgrades backward-compatible.  New managed installs
    # explicitly write ``off`` through .env.example/config management.
    raw = _setting(settings, "FINANCIAL_ROLLOUT_STAGE", LEGACY_ROLLOUT_STAGE)
    normalized = str(raw or "").strip().casefold()
    valid = normalized in ROLLOUT_STAGE_INDEX
    return (normalized if valid else "off", valid, str(raw or ""))


def _observation_seconds(value) -> int:
    try:
        parsed = int(value or 3600)
    except (TypeError, ValueError):
        parsed = 3600
    return max(60, min(604800, parsed))


def financial_rollout_state(settings=None, *, now: datetime | None = None) -> dict:
    source = config if settings is None else settings
    current, valid, configured = _configured_stage(source)
    current_index = ROLLOUT_STAGE_INDEX[current]
    changed_at = _parse_utc(
        _setting(source, "FINANCIAL_ROLLOUT_STAGE_CHANGED_AT", "")
    )
    captured = now or datetime.now(UTC)
    if captured.tzinfo is None or captured.utcoffset() is None:
        raise ValueError("rollout clock must be timezone-aware")
    captured = captured.astimezone(UTC)
    observation_seconds = _observation_seconds(
        _setting(source, "FINANCIAL_ROLLOUT_OBSERVATION_SECONDS", 3600)
    )
    observed_seconds = (
        max(0, int((captured - changed_at).total_seconds()))
        if changed_at is not None
        else None
    )
    capabilities = {
        capability: current_index >= ROLLOUT_STAGE_INDEX[required]
        for capability, required in ROLLOUT_CAPABILITY_REQUIREMENTS.items()
    }
    next_stage = (
        ROLLOUT_STAGE_KEYS[current_index + 1]
        if current_index + 1 < len(ROLLOUT_STAGE_KEYS)
        else None
    )
    return {
        "version": FINANCIAL_ROLLOUT_VERSION,
        "configured_stage": configured,
        "stage": current,
        "stage_label": ROLLOUT_STAGE_LABELS[current],
        "stage_index": current_index,
        "valid": valid,
        "reason": "configured" if valid else "invalid_stage_fail_closed",
        "capabilities": capabilities,
        "changed_at": utc_text(changed_at) if changed_at else "",
        "observed_seconds": observed_seconds,
        "observation_seconds": observation_seconds,
        "observation_complete": bool(
            current == "off"
            or observed_seconds is not None
            and observed_seconds >= observation_seconds
        ),
        "next_stage": next_stage,
        "next_stage_label": ROLLOUT_STAGE_LABELS.get(next_stage, ""),
        "rollback_requires_database": False,
        "advance_policy": "one_stage_after_healthy_observation",
    }


def rollout_capability_enabled(capability: str, settings=None) -> bool:
    normalized = str(capability or "").strip()
    if normalized not in ROLLOUT_CAPABILITY_REQUIREMENTS:
        raise FinancialRolloutError(f"未知金融灰度能力: {normalized}")
    return bool(financial_rollout_state(settings)["capabilities"][normalized])


def rollout_capability_reason(capability: str, settings=None) -> str:
    normalized = str(capability or "").strip()
    if rollout_capability_enabled(normalized, settings):
        return "enabled"
    required = ROLLOUT_CAPABILITY_REQUIREMENTS[normalized]
    return f"rollout_stage_{required}_not_reached"


def _metric(health: Mapping[str, object], family: str) -> Mapping[str, object]:
    metrics = health.get("metrics") if isinstance(health, Mapping) else {}
    value = metrics.get(family) if isinstance(metrics, Mapping) else {}
    return value if isinstance(value, Mapping) else {}


def rollout_stage_smoke(stage: str, health: Mapping[str, object]) -> dict:
    """Evaluate the stable-output smoke required before leaving ``stage``."""

    current = normalize_rollout_stage(stage, strict=True)
    index = ROLLOUT_STAGE_INDEX[current]
    checks = []

    def check(check_id: str, passed: bool, observed=None, expected=None):
        checks.append({
            "check": check_id,
            "passed": bool(passed),
            "observed": observed,
            "expected": expected,
        })

    status = str(health.get("status") or "unknown")
    check("health_status", status == "healthy", status, "healthy")

    if index >= ROLLOUT_STAGE_INDEX["rss"]:
        sources = _metric(health, "sources")
        items = sources.get("items") if isinstance(sources.get("items"), list) else []
        successful = [
            item for item in items
            if isinstance(item, Mapping)
            and item.get("enabled")
            and item.get("last_success_age_seconds") is not None
        ]
        check(
            "financial_rss_success",
            int(sources.get("enabled") or 0) > 0 and bool(successful),
            len(successful),
            ">=1 enabled source with a successful scan",
        )

    if index >= ROLLOUT_STAGE_INDEX["snapshot_readonly"]:
        providers = _metric(health, "providers")
        snapshots = _metric(health, "snapshots")
        check(
            "provider_available",
            int(providers.get("enabled") or 0) > 0,
            int(providers.get("enabled") or 0),
            ">=1",
        )
        check(
            "fresh_snapshot_available",
            int(snapshots.get("total") or 0) > 0
            and int(snapshots.get("stale") or 0) == 0,
            {
                "total": int(snapshots.get("total") or 0),
                "stale": int(snapshots.get("stale") or 0),
            },
            {"total": ">=1", "stale": 0},
        )

    if index >= ROLLOUT_STAGE_INDEX["stock_research"]:
        reports = _metric(health, "reports")
        coverage = reports.get("scope_coverage")
        coverage = coverage if isinstance(coverage, Mapping) else {}
        check(
            "stock_report_success",
            int(coverage.get("equity") or 0) > 0,
            int(coverage.get("equity") or 0),
            ">=1",
        )

    if index >= ROLLOUT_STAGE_INDEX["index_research"]:
        reports = _metric(health, "reports")
        coverage = reports.get("scope_coverage")
        coverage = coverage if isinstance(coverage, Mapping) else {}
        index_reports = int(coverage.get("index") or 0) + int(
            coverage.get("universe") or 0
        )
        check("index_report_success", index_reports > 0, index_reports, ">=1")

    if index >= ROLLOUT_STAGE_INDEX["history_review"]:
        verification = _metric(health, "verification")
        check(
            "verification_conflicts_clear",
            int(verification.get("pending_conflicts") or 0) == 0,
            int(verification.get("pending_conflicts") or 0),
            0,
        )

    if index >= ROLLOUT_STAGE_INDEX["auto_research"]:
        reports = _metric(health, "reports")
        budgets = _metric(health, "budgets")
        used = int(budgets.get("provider_calls_today") or 0)
        limit = int(budgets.get("provider_daily_limit") or 0)
        check(
            "automatic_research_stable",
            int(reports.get("failed_research_24h") or 0) == 0
            and (limit <= 0 or used < limit),
            {
                "failed_research_24h": int(reports.get("failed_research_24h") or 0),
                "provider_calls_today": used,
                "provider_daily_limit": limit,
            },
            "no failed research and budget below limit",
        )

    return {
        "stage": current,
        "stage_label": ROLLOUT_STAGE_LABELS[current],
        "passed": all(item["passed"] for item in checks),
        "checks": checks,
    }


def rollout_transition_decision(
    current_stage: str,
    target_stage: str,
    *,
    changed_at: str = "",
    observation_seconds: int = 3600,
    health: Mapping[str, object] | None = None,
    now: datetime | None = None,
) -> dict:
    """Allow immediate rollback and one-level healthy forward movement only."""

    current = normalize_rollout_stage(current_stage, strict=True)
    target = normalize_rollout_stage(target_stage, strict=True)
    current_index = ROLLOUT_STAGE_INDEX[current]
    target_index = ROLLOUT_STAGE_INDEX[target]
    captured = now or datetime.now(UTC)
    if captured.tzinfo is None or captured.utcoffset() is None:
        raise ValueError("rollout clock must be timezone-aware")
    captured = captured.astimezone(UTC)
    threshold = _observation_seconds(observation_seconds)
    activated = _parse_utc(changed_at)
    observed = (
        max(0, int((captured - activated).total_seconds()))
        if activated is not None
        else None
    )
    base = {
        "version": FINANCIAL_ROLLOUT_VERSION,
        "current_stage": current,
        "target_stage": target,
        "observation_seconds": threshold,
        "observed_seconds": observed,
        "rollback_requires_database": False,
    }
    if target_index == current_index:
        return {**base, "allowed": True, "action": "no_change", "reason": "same_stage"}
    if target_index < current_index:
        return {
            **base,
            "allowed": True,
            "action": "rollback",
            "reason": "rollback_is_immediate",
        }
    if target_index != current_index + 1:
        return {
            **base,
            "allowed": False,
            "action": "advance",
            "reason": "rollout_stage_skip_forbidden",
        }
    if current == "off" and target == "rss":
        return {
            **base,
            "allowed": True,
            "action": "advance",
            "reason": "bootstrap_rss_stage",
            "smoke": {"stage": "off", "passed": True, "checks": []},
        }
    if activated is None:
        return {
            **base,
            "allowed": False,
            "action": "advance",
            "reason": "rollout_observation_start_missing",
        }
    if observed < threshold:
        return {
            **base,
            "allowed": False,
            "action": "advance",
            "reason": "rollout_observation_incomplete",
        }
    smoke = rollout_stage_smoke(current, health or {})
    if not smoke["passed"]:
        return {
            **base,
            "allowed": False,
            "action": "advance",
            "reason": "rollout_smoke_failed",
            "smoke": smoke,
        }
    return {
        **base,
        "allowed": True,
        "action": "advance",
        "reason": "healthy_observation_complete",
        "smoke": smoke,
    }


__all__ = [
    "FINANCIAL_ROLLOUT_STAGES",
    "FINANCIAL_ROLLOUT_VERSION",
    "LEGACY_ROLLOUT_STAGE",
    "ROLLOUT_CAPABILITY_REQUIREMENTS",
    "ROLLOUT_STAGE_INDEX",
    "ROLLOUT_STAGE_KEYS",
    "FinancialRolloutError",
    "financial_rollout_state",
    "normalize_rollout_stage",
    "rollout_capability_enabled",
    "rollout_capability_reason",
    "rollout_stage_smoke",
    "rollout_transition_decision",
    "utc_text",
]

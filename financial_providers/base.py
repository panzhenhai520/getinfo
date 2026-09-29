#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Shared policy, budget and persistence primitives for embedded providers."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

from financial_config import FinancialCapabilityDisabled, require_financial_capability
from financial_instruments import InstrumentRecord, InstrumentRegistry
from financial_provider_contract import (
    FinancialDataKind,
    FinancialDataProvider,
    FinancialDataRequest,
    FinancialProviderResponse,
    PermissionDeniedError,
    RateLimitedError,
    UnsupportedAssetError,
)
from financial_source_license import (
    FinancialSourceAuthorizationError,
    provider_license_profile,
    require_provider_authorization,
)


PROFILE_DIR = Path(__file__).resolve().parents[1] / "config" / "financial_providers"


def load_profile(provider_id: str) -> Mapping[str, Any]:
    path = PROFILE_DIR / f"{str(provider_id).strip()}.json"
    profile = json.loads(path.read_text(encoding="utf-8"))
    if profile.get("provider_id") != provider_id:
        raise ValueError(f"provider profile id mismatch: {provider_id}")
    required = {
        "display_name",
        "access_tier",
        "license_profile",
        "provider_type",
        "priority",
        "daily_call_budget",
        "fallback_rank",
        "documentation_url",
        "attribution_text",
        "terms_note",
        "capabilities",
    }
    missing = sorted(required - set(profile))
    if missing:
        raise ValueError(f"provider profile missing fields: {', '.join(missing)}")
    if profile.get("default_enabled") is not False:
        raise ValueError("external provider profiles must be disabled by default")
    provider_license_profile(provider_id, provider_profile=profile)
    return profile


def setting_value(settings, name: str, default=None):
    if isinstance(settings, Mapping):
        return settings.get(name, default)
    return getattr(settings, name, default)


def bool_setting(settings, name: str, default: bool = False) -> bool:
    value = setting_value(settings, name, default)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class RegisteredFinancialProvider(FinancialDataProvider):
    """Base class that makes opt-in, quota and provenance rules unavoidable."""

    provider_key = ""

    def __init__(
        self,
        *,
        instrument_registry: InstrumentRegistry,
        settings,
        connection=None,
        clock=None,
    ):
        self.instruments = instrument_registry
        self.settings = settings
        self.connection = connection
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.profile = load_profile(self.provider_key)
        self._budget_lock = threading.Lock()
        self._budget_date: Optional[date] = None
        self._budget_calls = 0

    @property
    def provider_id(self) -> str:
        return self.provider_key

    @property
    def license_profile(self) -> str:
        return str(self.profile["license_profile"])

    @property
    def capabilities(self) -> Sequence[FinancialDataKind]:
        return tuple(FinancialDataKind(item) for item in self.profile["capabilities"])

    def _fetched_at(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("provider clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def _error_details(self, request: FinancialDataRequest, **details):
        return {
            "provider_id": self.provider_id,
            "endpoint": request.endpoint,
            "request_id": request.request_id,
            "details": details,
        }

    def _require_enabled(
        self, request: FinancialDataRequest, *, captured_at: Optional[datetime] = None
    ) -> None:
        try:
            require_financial_capability(self.provider_id, self.settings)
        except FinancialCapabilityDisabled as exc:
            raise PermissionDeniedError(
                f"{self.provider_id} provider is disabled or not configured",
                **self._error_details(request, gate_reason=exc.reason),
            ) from exc
        try:
            require_provider_authorization(
                self.provider_id,
                self.settings,
                at=captured_at or self._fetched_at(),
                provider_profile=self.profile,
            )
        except FinancialSourceAuthorizationError as exc:
            raise PermissionDeniedError(
                f"{self.provider_id} provider authorization is not valid",
                **self._error_details(
                    request,
                    gate_reason=exc.reason,
                    license_profile=exc.profile_id,
                ),
            ) from exc

    def _daily_budget(self) -> int:
        provider_override = setting_value(
            self.settings, f"{self.provider_id.upper()}_DAILY_CALL_BUDGET", None
        )
        profile_budget = int(self.profile["daily_call_budget"])
        if provider_override not in (None, ""):
            profile_budget = max(0, int(provider_override))
        global_budget = int(
            setting_value(
                self.settings,
                "FINANCIAL_PROVIDER_DAILY_CALL_BUDGET",
                profile_budget,
            )
        )
        return min(profile_budget, max(0, global_budget))

    def _consume_budget(
        self, request: FinancialDataRequest, fetched_at: datetime
    ) -> None:
        today = fetched_at.date()
        limit = self._daily_budget()
        with self._budget_lock:
            if self._budget_date != today:
                self._budget_date = today
                self._budget_calls = 0
            if self._budget_calls >= limit:
                raise RateLimitedError(
                    f"{self.provider_id} daily call budget exhausted",
                    **self._error_details(
                        request,
                        budget_scope="provider_instance_utc_day",
                        budget_limit=limit,
                    ),
                    retry_after_seconds=86400,
                )
            self._budget_calls += 1

    def _prepare_request(
        self,
        request: FinancialDataRequest,
        endpoint_kinds: Mapping[str, FinancialDataKind],
        *,
        mapping_required: bool = True,
        fetched_at: Optional[datetime] = None,
    ) -> Tuple[InstrumentRecord, str, datetime]:
        captured_at = fetched_at or self._fetched_at()
        self._require_enabled(request, captured_at=captured_at)
        expected = endpoint_kinds.get(request.endpoint)
        if expected is None:
            raise UnsupportedAssetError(
                f"unsupported {self.provider_id} endpoint: {request.endpoint}",
                **self._error_details(request, supported_endpoints=sorted(endpoint_kinds)),
            )
        if request.data_kind != expected:
            raise UnsupportedAssetError(
                "request data kind does not match provider endpoint",
                **self._error_details(
                    request,
                    expected_data_kind=expected.value,
                    actual_data_kind=request.data_kind.value,
                ),
            )
        try:
            instrument = self.instruments.get(int(request.instrument_id))
        except (TypeError, ValueError):
            instrument = None
        if instrument is None:
            raise UnsupportedAssetError(
                "instrument is not registered",
                **self._error_details(request, instrument_id=request.instrument_id),
            )
        provider_symbol = str(instrument.provider_mappings.get(self.provider_id) or "")
        if mapping_required and not provider_symbol:
            raise UnsupportedAssetError(
                "instrument has no provider mapping",
                **self._error_details(
                    request, canonical_symbol=instrument.canonical_symbol
                ),
            )
        self._consume_budget(request, captured_at)
        return instrument, provider_symbol, captured_at

    def _effective_enabled(self) -> bool:
        try:
            require_financial_capability(self.provider_id, self.settings)
            require_provider_authorization(
                self.provider_id,
                self.settings,
                at=self._fetched_at(),
                provider_profile=self.profile,
            )
            return True
        except (FinancialCapabilityDisabled, FinancialSourceAuthorizationError):
            return False

    def ensure_profile(self) -> int:
        if self.connection is None:
            raise RuntimeError("database connection is required to register provider")
        authorization = provider_license_profile(
            self.provider_id, provider_profile=self.profile
        )
        metadata = {
            "license_profile": self.license_profile,
            "license_owner_role": authorization["owner_role"],
            "license_cost_type": authorization["cost_type"],
            "license_permissions": authorization["permissions"],
            "license_review_due_on": authorization["review_due_on"],
            "documentation_url": self.profile["documentation_url"],
            "terms_note": self.profile["terms_note"],
            "sla": self.profile.get("sla", "none"),
            "daily_call_budget": self._daily_budget(),
            "fallback_rank": int(self.profile["fallback_rank"]),
            "default_enabled": False,
        }
        for key in (
            "package_version",
            "package_wheel_sha256",
            "package_url",
            "token_url",
            "api_base_url",
            "gamma_base_url",
            "clob_base_url",
        ):
            if key in self.profile:
                metadata[key] = self.profile[key]
        self.connection.execute(
            """
            INSERT INTO financial_provider_profiles(
                provider_key, display_name, provider_type, access_tier,
                capabilities_json, priority, is_enabled, health_status,
                terms_url, attribution_text, metadata_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 'unknown', ?, ?, ?)
            ON CONFLICT(provider_key) DO UPDATE SET
                display_name=excluded.display_name,
                provider_type=excluded.provider_type,
                access_tier=excluded.access_tier,
                capabilities_json=excluded.capabilities_json,
                priority=excluded.priority,
                is_enabled=excluded.is_enabled,
                terms_url=excluded.terms_url,
                attribution_text=excluded.attribution_text,
                metadata_json=excluded.metadata_json,
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (
                self.provider_id,
                self.profile["display_name"],
                self.profile["provider_type"],
                self.profile["access_tier"],
                json.dumps([item.value for item in self.capabilities]),
                int(self.profile["priority"]),
                int(self._effective_enabled()),
                self.profile.get("terms_url", ""),
                self.profile["attribution_text"],
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            ),
        )
        row = self.connection.execute(
            "SELECT id FROM financial_provider_profiles WHERE provider_key=?",
            (self.provider_id,),
        ).fetchone()
        return int(row[0])

    def persist_response(self, response: FinancialProviderResponse) -> Tuple[int, ...]:
        if self.connection is None:
            raise RuntimeError("database connection is required to persist provider data")
        profile_id = self.ensure_profile()
        snapshot_ids = []
        savepoint = f"{self.provider_id}_snapshot_write"
        self.connection.execute(f"SAVEPOINT {savepoint}")
        try:
            for record in response.records:
                instrument_id = int(record.instrument_id)
                payload = {
                    "provider_id": response.provider_id,
                    "endpoint": response.endpoint,
                    "license_profile": response.license_profile,
                    "data_kind": response.data_kind.value,
                    "metric": record.metric,
                    "value": record.value,
                    "normalized_payload": record.normalized_payload,
                    "adjustment": record.adjustment.value,
                    "quality_flags": list(record.quality_flags),
                    "lineage": record.lineage,
                }
                payload_text = json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                payload_digest = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
                identity = "|".join(
                    (
                        response.provider_id,
                        response.endpoint,
                        record.instrument_id,
                        record.metric,
                        utc_text(record.observed_at),
                        record.raw_response_hash,
                    )
                )
                snapshot_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                threshold = int(record.lineage.get("freshness_threshold_seconds") or 0)
                stale_after = (
                    utc_text(record.observed_at + timedelta(seconds=threshold))
                    if threshold
                    else ""
                )
                self.connection.execute(
                    """
                    INSERT INTO financial_data_snapshots(
                        snapshot_key, instrument_id, provider_profile_id, data_type,
                        interval_code, observed_at, fetched_at, market_status,
                        currency, timezone, stale_after, quality_status, payload_json,
                        payload_sha256, source_url, request_id
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULLIF(?, ''), ?, ?, ?, ?, ?)
                    ON CONFLICT(snapshot_key) DO UPDATE SET
                        fetched_at=excluded.fetched_at,
                        quality_status=excluded.quality_status,
                        request_id=excluded.request_id
                    """,
                    (
                        snapshot_key,
                        instrument_id,
                        profile_id,
                        response.data_kind.value,
                        str(record.normalized_payload.get("interval") or ""),
                        utc_text(record.observed_at),
                        utc_text(record.fetched_at),
                        record.market_status.value,
                        record.currency,
                        record.timezone,
                        stale_after,
                        f"normalized_{record.freshness_state.value}",
                        payload_text,
                        payload_digest,
                        record.source_url,
                        response.request_id,
                    ),
                )
                row = self.connection.execute(
                    "SELECT id FROM financial_data_snapshots WHERE snapshot_key=?",
                    (snapshot_key,),
                ).fetchone()
                snapshot_ids.append(int(row[0]))
            self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        return tuple(snapshot_ids)

    def fetch_and_persist(self, request: FinancialDataRequest) -> FinancialProviderResponse:
        response = self.fetch_validated(request)
        self.persist_response(response)
        return response

    def update_health(self, status: str, checked_at: datetime) -> None:
        if self.connection is None:
            return
        profile_id = self.ensure_profile()
        self.connection.execute(
            "UPDATE financial_provider_profiles SET health_status=?, "
            "last_health_check_at=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE id=?",
            (str(status), utc_text(checked_at), profile_id),
        )

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Explicit provider selection and fallback; disabled sources are never called."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

from financial_config import financial_capabilities
from financial_provider_contract import (
    DegradationInfo,
    FinancialDataRequest,
    FinancialProviderError,
    FinancialProviderResponse,
    PermissionDeniedError,
    RateLimitedError,
    TemporarilyUnavailableError,
)
from financial_resource_isolation import (
    ProviderAdmissionTimeout,
    ProviderCooldown,
    provider_admission_controller,
)
from financial_providers.base import PROFILE_DIR, load_profile
from financial_source_license import (
    FinancialSourceAuthorizationError,
    provider_license_profile,
    require_provider_authorization,
)


EMBEDDED_PROVIDER_IDS = (
    "akshare_cn",
    "tushare_cn",
    "yahoo",
    "alpha_vantage",
    "fred",
    "polymarket",
    "easyquotation",
    "official_evidence",
)
HARD_DISABLED_PATH = PROFILE_DIR / "disabled_social.json"
HARD_DISABLED_IDS = frozenset(
    item["provider_id"]
    for item in json.loads(HARD_DISABLED_PATH.read_text(encoding="utf-8"))["providers"]
)
PROVIDER_POLICIES = {
    provider_id: load_profile(provider_id) for provider_id in EMBEDDED_PROVIDER_IDS
}


class FinancialProviderRouter:
    """Lazily creates only enabled providers from an explicit candidate order."""

    def __init__(
        self,
        *,
        settings,
        factories: Mapping[str, Callable[[], object]],
        clock=None,
        admission_controller=None,
    ):
        forbidden = sorted(HARD_DISABLED_IDS.intersection(factories))
        if forbidden:
            raise ValueError(
                "hard-disabled providers cannot have runtime factories: "
                + ", ".join(forbidden)
            )
        unknown = sorted(set(factories) - set(PROVIDER_POLICIES))
        if unknown:
            raise ValueError("unregistered provider factories: " + ", ".join(unknown))
        self.settings = settings
        self.factories = dict(factories)
        self._instances = {}
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.admission_controller = (
            admission_controller or provider_admission_controller
        )

    def _enabled(self, provider_id: str) -> bool:
        state = financial_capabilities(self.settings)
        return bool(state["effective"].get(provider_id, False))

    def _provider(self, provider_id: str):
        if provider_id in HARD_DISABLED_IDS:
            raise PermissionDeniedError(
                "provider is hard-disabled by authorization policy",
                provider_id=provider_id,
                endpoint="provider_selection",
                request_id="provider-selection",
                details={"gate_reason": "authorization_and_terms_not_approved"},
            )
        if not self._enabled(provider_id):
            return None
        try:
            require_provider_authorization(
                provider_id,
                self.settings,
                at=self._clock(),
                provider_profile=PROVIDER_POLICIES[provider_id],
            )
        except FinancialSourceAuthorizationError as exc:
            raise PermissionDeniedError(
                "provider authorization is missing, expired or production-forbidden",
                provider_id=provider_id,
                endpoint="provider_selection",
                request_id="provider-selection",
                details={
                    "gate_reason": exc.reason,
                    "license_profile": exc.profile_id,
                },
            ) from exc
        factory = self.factories.get(provider_id)
        if factory is None:
            return None
        if provider_id not in self._instances:
            self._instances[provider_id] = factory()
        return self._instances[provider_id]

    def provider_availability(
        self, provider_ids: Sequence[str]
    ) -> Mapping[str, Mapping[str, object]]:
        """Return a side-effect-free eligibility view for an explicit chain."""

        state = financial_capabilities(self.settings)
        result = {}
        for raw_provider_id in provider_ids:
            provider_id = str(raw_provider_id or "").strip()
            if not provider_id or provider_id in result:
                continue
            reason = "enabled"
            available = True
            if provider_id in HARD_DISABLED_IDS:
                available = False
                reason = "authorization_and_terms_not_approved"
            elif provider_id not in PROVIDER_POLICIES:
                available = False
                reason = "provider_policy_unregistered"
            elif not bool(state["effective"].get(provider_id, False)):
                available = False
                reason = str(
                    state["reasons"].get(provider_id)
                    or "provider_capability_disabled"
                )
            elif provider_id not in self.factories:
                available = False
                reason = "provider_factory_unconfigured"
            else:
                try:
                    require_provider_authorization(
                        provider_id,
                        self.settings,
                        at=self._clock(),
                        provider_profile=PROVIDER_POLICIES[provider_id],
                    )
                except FinancialSourceAuthorizationError as exc:
                    available = False
                    reason = str(exc.reason)
            result[provider_id] = {
                "available": available,
                "reason": reason,
            }
        return result

    def fetch(
        self,
        request: FinancialDataRequest,
        *,
        candidate_provider_ids: Sequence[str],
        allow_fallback: bool,
    ) -> FinancialProviderResponse:
        candidates = tuple(dict.fromkeys(str(item).strip() for item in candidate_provider_ids))
        if not candidates:
            raise ValueError("candidate_provider_ids cannot be empty")
        if any(item in HARD_DISABLED_IDS for item in candidates):
            blocked = next(item for item in candidates if item in HARD_DISABLED_IDS)
            raise PermissionDeniedError(
                "provider is hard-disabled by authorization policy",
                provider_id=blocked,
                endpoint=request.endpoint,
                request_id=request.request_id,
                details={"gate_reason": "authorization_and_terms_not_approved"},
            )
        preferred = str(request.preferred_provider_id or candidates[0])
        if preferred not in candidates:
            candidates = (preferred,) + candidates
        if not allow_fallback:
            candidates = (preferred,)

        attempted = []
        last_error = None
        for provider_id in candidates:
            attempted.append(provider_id)
            provider = self._provider(provider_id)
            if provider is None:
                continue
            provider_request = replace(request, preferred_provider_id=provider_id)
            try:
                with self.admission_controller.slot(
                    provider_id,
                    timeout_seconds=int(
                        getattr(
                            self.settings,
                            "FINANCIAL_PROVIDER_ADMISSION_TIMEOUT_SECONDS",
                            self.settings.get(
                                "FINANCIAL_PROVIDER_ADMISSION_TIMEOUT_SECONDS",
                                5,
                            )
                            if isinstance(self.settings, Mapping)
                            else 5,
                        )
                    ),
                ):
                    response = provider.fetch_validated(provider_request)
            except ProviderCooldown as exc:
                last_error = RateLimitedError(
                    "provider cooldown is active",
                    provider_id=provider_id,
                    endpoint=request.endpoint,
                    request_id=request.request_id,
                    retry_after_seconds=exc.retry_after_seconds,
                    details={"gate_reason": "shared_provider_cooldown"},
                )
                if not allow_fallback:
                    raise last_error
                continue
            except ProviderAdmissionTimeout:
                last_error = TemporarilyUnavailableError(
                    "provider concurrency admission timed out",
                    provider_id=provider_id,
                    endpoint=request.endpoint,
                    request_id=request.request_id,
                    details={"gate_reason": "provider_concurrency_saturated"},
                )
                if not allow_fallback:
                    raise last_error
                continue
            except FinancialProviderError as exc:
                last_error = exc
                if isinstance(exc, RateLimitedError):
                    self.admission_controller.record_rate_limit(
                        provider_id, int(exc.retry_after_seconds or 1)
                    )
                if not allow_fallback:
                    raise
                continue
            if provider_id == preferred:
                return replace(response, request_id=request.request_id).validate_for(request)
            degraded = replace(
                response,
                request_id=request.request_id,
                degradation=DegradationInfo(
                    degraded=True,
                    reason=(
                        last_error.code.value
                        if last_error is not None
                        else "preferred_provider_disabled_or_unavailable"
                    ),
                    requested_provider_id=preferred,
                    actual_provider_id=provider_id,
                    attempted_provider_ids=tuple(attempted),
                ),
            )
            return degraded.validate_for(request)

        if last_error is not None:
            raise last_error
        raise PermissionDeniedError(
            "no explicitly enabled provider is available",
            provider_id=preferred,
            endpoint=request.endpoint,
            request_id=request.request_id,
            details={
                "gate_reason": "all_candidates_disabled_or_unconfigured",
                "candidate_provider_ids": list(candidates),
            },
        )

    def fetch_many(
        self,
        request: FinancialDataRequest,
        *,
        candidate_provider_ids: Sequence[str],
        max_workers: int = 4,
    ) -> tuple[tuple[FinancialProviderResponse, ...], tuple[dict, ...]]:
        """Fetch each provider independently and never cross-provider fallback.

        Network work may run concurrently, but persistence deliberately remains
        outside this method so callers can serialize writes under their own
        database lock.
        """

        candidates = tuple(
            dict.fromkeys(str(item or "").strip() for item in candidate_provider_ids)
        )
        candidates = tuple(item for item in candidates if item)
        if not candidates:
            raise ValueError("candidate_provider_ids cannot be empty")

        def fetch_one(provider_id: str):
            provider_request = replace(
                request,
                request_id=f"{request.request_id}-{provider_id}",
                preferred_provider_id=provider_id,
            )
            try:
                response = self.fetch(
                    provider_request,
                    candidate_provider_ids=(provider_id,),
                    allow_fallback=False,
                )
            except FinancialProviderError as exc:
                return None, {
                    "provider_id": provider_id,
                    "error_code": exc.code.value,
                    "error_type": type(exc).__name__,
                    "retryable": bool(exc.retryable),
                    "gate_reason": str(exc.details.get("gate_reason") or ""),
                }
            except Exception as exc:
                return None, {
                    "provider_id": provider_id,
                    "error_code": "provider_contract_failed",
                    "error_type": type(exc).__name__,
                    "retryable": False,
                    "gate_reason": "",
                }
            if response.provider_id != provider_id:
                return None, {
                    "provider_id": provider_id,
                    "error_code": "provider_identity_mismatch",
                    "error_type": "ProviderIdentityMismatch",
                    "retryable": False,
                    "gate_reason": "independent_provider_required",
                }
            return response, None

        responses_by_provider = {}
        errors_by_provider = {}
        workers = max(1, min(int(max_workers), len(candidates)))
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="financial-quote-source",
        ) as executor:
            futures = {
                executor.submit(fetch_one, provider_id): provider_id
                for provider_id in candidates
            }
            for future in as_completed(futures):
                provider_id = futures[future]
                response, error = future.result()
                if response is not None:
                    responses_by_provider[provider_id] = response
                elif error is not None:
                    errors_by_provider[provider_id] = error
        return (
            tuple(
                responses_by_provider[item]
                for item in candidates
                if item in responses_by_provider
            ),
            tuple(
                errors_by_provider[item]
                for item in candidates
                if item in errors_by_provider
            ),
        )

    def fetch_and_persist(
        self,
        request: FinancialDataRequest,
        *,
        candidate_provider_ids: Sequence[str],
        allow_fallback: bool,
    ) -> tuple[FinancialProviderResponse, tuple[int, ...]]:
        """Fetch through the explicit chain and persist with the chosen adapter.

        Persistence deliberately happens only after contract validation.  The
        selected provider instance owns normalization and snapshot identity, so
        the router does not duplicate vendor-specific storage logic.
        """
        response = self.fetch(
            request,
            candidate_provider_ids=candidate_provider_ids,
            allow_fallback=allow_fallback,
        )
        snapshot_ids = self.persist_response(response)
        if len(snapshot_ids) != len(response.records):
            raise RuntimeError(
                "provider snapshot count does not match normalized record count"
            )
        return response, snapshot_ids

    def persist_response(
        self, response: FinancialProviderResponse
    ) -> tuple[int, ...]:
        """Persist an already validated response through its selected adapter.

        Keeping this boundary public lets synchronous callers perform network
        I/O outside the application database lock and then serialize only the
        short persistence step.  The selected adapter still owns normalization
        and snapshot identity.
        """

        if not isinstance(response, FinancialProviderResponse):
            raise TypeError("response must be a FinancialProviderResponse")
        provider = self._instances.get(response.provider_id)
        persist = getattr(provider, "persist_response", None)
        if not callable(persist):
            raise RuntimeError(
                f"provider {response.provider_id} has no snapshot persistence boundary"
            )
        snapshot_ids = tuple(int(value) for value in persist(response))
        return snapshot_ids


def provider_policy_summary() -> Mapping[str, object]:
    return {
        "providers": {
            provider_id: {
                "default_enabled": profile["default_enabled"],
                "license_profile": profile["license_profile"],
                "daily_call_budget": profile["daily_call_budget"],
                "fallback_rank": profile["fallback_rank"],
                "capabilities": profile["capabilities"],
                "authorization": {
                    key: provider_license_profile(
                        provider_id, provider_profile=profile
                    )[key]
                    for key in (
                        "profile_id",
                        "owner_role",
                        "cost_type",
                        "billing_owner_role",
                        "permissions",
                        "quota",
                        "review_due_on",
                        "production_allowed",
                        "production_approval_required",
                    )
                },
            }
            for provider_id, profile in PROVIDER_POLICIES.items()
        },
        "hard_disabled": sorted(HARD_DISABLED_IDS),
    }

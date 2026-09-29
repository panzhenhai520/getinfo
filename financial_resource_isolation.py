#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Bounded in-process admission rules for financial workloads.

The controls in this module do not own a queue, process service, database or
network listener.  They make the existing worker and Provider paths explicit:
long financial work has a dedicated same-image worker lane, and every Provider
call enters one shared, bounded process-level admission controller.
"""

from __future__ import annotations

import math
import re
import threading
import time
from contextlib import contextmanager
from typing import Dict, Iterable, Tuple

import config


FINANCIAL_RESOURCE_ISOLATION_VERSION = "financial-resource-isolation-v1"

CORE_WORKER_JOB_TYPES: Tuple[str, ...] = (
    "classification",
    "enrich",
    "enrich_repair",
    "source_sync",
    "light_scan",
    "candidate_dispatch",
    "candidate_rescore",
    "topic_cluster",
    "report_ingest",
    "report_discover",
    "report_check",
    "industry_revalidate",
    "trend_aggregate",
    "embed_articles",
    "event_extract",
    "subject_normalize",
    "bertopic_cluster",
    "dynamic_convert",
    "pack_report",
    "task_cleanup",
    "financial_snapshot",
    "market_overview",
)

LONG_FINANCIAL_JOB_TYPES: Tuple[str, ...] = (
    "financial_research",
    "financial_verify",
    "paper_backtest",
)

ALL_ISOLATED_WORKER_JOB_TYPES = frozenset(
    CORE_WORKER_JOB_TYPES + LONG_FINANCIAL_JOB_TYPES
)

_SAFE_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


class ProviderAdmissionError(RuntimeError):
    """Base error for bounded Provider admission failures."""


class ProviderAdmissionTimeout(ProviderAdmissionError):
    pass


class ProviderCooldown(ProviderAdmissionError):
    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        super().__init__("provider cooldown is active")


class ArtifactIOTimeout(RuntimeError):
    pass


class ArtifactIOController:
    """Bounded slots for fsync-heavy checkpoint and report writes."""

    def __init__(self, max_concurrency: int):
        if isinstance(max_concurrency, bool) or int(max_concurrency) < 1:
            raise ValueError("artifact I/O max_concurrency must be positive")
        self.max_concurrency = int(max_concurrency)
        self.semaphore = threading.BoundedSemaphore(self.max_concurrency)
        self.lock = threading.Lock()
        self.active = 0
        self.waiting = 0

    @contextmanager
    def slot(self, *, timeout_seconds: float):
        with self.lock:
            self.waiting += 1
        acquired = self.semaphore.acquire(timeout=max(0.001, float(timeout_seconds)))
        with self.lock:
            self.waiting -= 1
            if acquired:
                self.active += 1
        if not acquired:
            raise ArtifactIOTimeout("artifact I/O admission timed out")
        try:
            yield
        finally:
            with self.lock:
                self.active -= 1
            self.semaphore.release()

    def snapshot(self) -> Dict[str, int]:
        with self.lock:
            return {
                "max_concurrency": self.max_concurrency,
                "active": self.active,
                "waiting": self.waiting,
            }


def validate_worker_lane_partition(registered_job_types: Iterable[str]) -> Dict[str, object]:
    registered = {str(item).strip() for item in registered_job_types if str(item).strip()}
    overlap = set(CORE_WORKER_JOB_TYPES).intersection(LONG_FINANCIAL_JOB_TYPES)
    missing = registered - ALL_ISOLATED_WORKER_JOB_TYPES
    stale = ALL_ISOLATED_WORKER_JOB_TYPES - registered
    return {
        "valid": not overlap and not missing and not stale,
        "core": list(CORE_WORKER_JOB_TYPES),
        "long_financial": list(LONG_FINANCIAL_JOB_TYPES),
        "overlap": sorted(overlap),
        "unassigned_registered": sorted(missing),
        "unknown_configured": sorted(stale),
    }


class ProviderAdmissionController:
    """Fair, bounded process-level slots with per-source cooldowns."""

    def __init__(
        self,
        max_concurrency: int,
        per_source_concurrency: int,
        *,
        monotonic=time.monotonic,
    ):
        if isinstance(max_concurrency, bool) or int(max_concurrency) < 1:
            raise ValueError("provider max_concurrency must be positive")
        if isinstance(per_source_concurrency, bool) or int(per_source_concurrency) < 1:
            raise ValueError("provider per_source_concurrency must be positive")
        self.max_concurrency = int(max_concurrency)
        self.per_source_concurrency = min(
            int(per_source_concurrency), self.max_concurrency
        )
        self.monotonic = monotonic
        self.condition = threading.Condition()
        self.active_total = 0
        self.active_by_source: Dict[str, int] = {}
        self.cooldown_until: Dict[str, float] = {}
        self.waiting = 0

    @staticmethod
    def _provider_id(value: str) -> str:
        provider_id = str(value or "").strip().casefold()
        if not _SAFE_PROVIDER_ID.fullmatch(provider_id):
            raise ValueError("provider_id is invalid")
        return provider_id

    def _cooldown_remaining(self, provider_id: str, now: float) -> int:
        until = float(self.cooldown_until.get(provider_id, 0.0))
        if until <= now:
            self.cooldown_until.pop(provider_id, None)
            return 0
        return max(1, int(math.ceil(until - now)))

    @contextmanager
    def slot(self, provider_id: str, *, timeout_seconds: float):
        normalized = self._provider_id(provider_id)
        timeout = max(0.001, float(timeout_seconds))
        deadline = self.monotonic() + timeout
        acquired = False
        with self.condition:
            remaining = self._cooldown_remaining(normalized, self.monotonic())
            if remaining:
                raise ProviderCooldown(remaining)
            self.waiting += 1
            try:
                while not acquired:
                    now = self.monotonic()
                    remaining = self._cooldown_remaining(normalized, now)
                    if remaining:
                        raise ProviderCooldown(remaining)
                    source_active = int(self.active_by_source.get(normalized, 0))
                    if (
                        self.active_total < self.max_concurrency
                        and source_active < self.per_source_concurrency
                    ):
                        self.active_total += 1
                        self.active_by_source[normalized] = source_active + 1
                        acquired = True
                        break
                    wait_seconds = deadline - now
                    if wait_seconds <= 0:
                        raise ProviderAdmissionTimeout(
                            "provider admission wait exceeded its bounded timeout"
                        )
                    self.condition.wait(timeout=min(0.05, wait_seconds))
            finally:
                self.waiting -= 1
        try:
            yield
        finally:
            if acquired:
                with self.condition:
                    self.active_total -= 1
                    source_active = int(self.active_by_source.get(normalized, 0)) - 1
                    if source_active > 0:
                        self.active_by_source[normalized] = source_active
                    else:
                        self.active_by_source.pop(normalized, None)
                    self.condition.notify_all()

    def record_rate_limit(self, provider_id: str, retry_after_seconds: int) -> None:
        normalized = self._provider_id(provider_id)
        retry_after = max(1, min(3600, int(retry_after_seconds or 1)))
        with self.condition:
            self.cooldown_until[normalized] = max(
                float(self.cooldown_until.get(normalized, 0.0)),
                self.monotonic() + retry_after,
            )
            self.condition.notify_all()

    def snapshot(self) -> Dict[str, object]:
        with self.condition:
            now = self.monotonic()
            cooldowns = {
                provider_id: self._cooldown_remaining(provider_id, now)
                for provider_id in tuple(self.cooldown_until)
            }
            return {
                "version": FINANCIAL_RESOURCE_ISOLATION_VERSION,
                "max_concurrency": self.max_concurrency,
                "per_source_concurrency": self.per_source_concurrency,
                "active_total": self.active_total,
                "active_by_source": dict(self.active_by_source),
                "waiting": self.waiting,
                "cooldown_seconds": {
                    key: value for key, value in cooldowns.items() if value > 0
                },
            }


provider_admission_controller = ProviderAdmissionController(
    config.FINANCIAL_PROVIDER_MAX_CONCURRENCY,
    config.FINANCIAL_PROVIDER_MAX_CONCURRENCY_PER_SOURCE,
)
artifact_io_controller = ArtifactIOController(
    config.FINANCIAL_ARTIFACT_IO_MAX_CONCURRENCY
)


__all__ = [
    "ALL_ISOLATED_WORKER_JOB_TYPES",
    "ArtifactIOController",
    "ArtifactIOTimeout",
    "CORE_WORKER_JOB_TYPES",
    "FINANCIAL_RESOURCE_ISOLATION_VERSION",
    "LONG_FINANCIAL_JOB_TYPES",
    "ProviderAdmissionController",
    "ProviderAdmissionError",
    "ProviderAdmissionTimeout",
    "ProviderCooldown",
    "artifact_io_controller",
    "provider_admission_controller",
    "validate_worker_lane_partition",
]

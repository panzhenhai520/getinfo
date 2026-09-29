#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Financial job registration on the existing persistent intel worker.

The dispatcher is deliberately an in-process seam: later financial stages
provide concrete runners, while queueing, leases, retries, cancellation and
heartbeats remain owned by ``IntelRepository``/``IntelWorker``.  No service,
port, scheduler or second queue is introduced here.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Callable, Dict, Mapping, Optional

from financial_config import financial_product_capabilities
from financial_rollout import rollout_capability_enabled, rollout_capability_reason


FINANCIAL_JOB_TYPES = (
    "financial_snapshot",
    "financial_research",
    "financial_verify",
    "market_overview",
    "paper_backtest",
)

FINANCIAL_JOB_CAPABILITIES = {
    "financial_snapshot": "financial_intelligence",
    "financial_research": "trading_agents",
    "financial_verify": "trading_agents",
    # A market overview is a deterministic digest of persisted snapshots.  It
    # belongs to the base financial-intelligence capability; only the later
    # multi-role ``financial_research`` job requires TradingAgents.
    "market_overview": "financial_intelligence",
    "paper_backtest": "simulation",
}


def _job_rollout_requirement(job_type: str, payload: Mapping) -> str:
    if job_type == "financial_snapshot":
        return "snapshot_readonly"
    if job_type == "market_overview":
        return "index_research"
    if job_type in {"financial_research", "financial_verify"}:
        if (
            str(payload.get("scope_type") or "").casefold() in {"universe", "market"}
            or str(payload.get("asset_type") or "").casefold() == "index"
            or payload.get("universe_id") is not None
            or bool(str(payload.get("universe_key") or "").strip())
        ):
            return "index_research"
        return "stock_research"
    return "simulation_backtest"


class FinancialJobExecutionError(RuntimeError):
    """Stable worker error with explicit retry semantics."""

    def __init__(self, message: str, *, error_code: str, retryable: bool):
        super().__init__(message)
        self.error_code = str(error_code)
        self.retryable = bool(retryable)


class FinancialJobCancelled(FinancialJobExecutionError):
    def __init__(self, reason: str = "job_cancelled"):
        super().__init__(
            "金融任务已取消",
            error_code=str(reason or "job_cancelled"),
            retryable=False,
        )


@dataclass
class FinancialJobContext:
    job_id: int
    job_type: str
    worker_id: str
    repository: object
    cancel_event: threading.Event

    def raise_if_cancelled(self) -> None:
        if not self.cancel_event.is_set():
            return
        job = self.repository.get_job(self.job_id) or {}
        if job.get("status") == "cancelled":
            raise FinancialJobCancelled("job_cancelled")
        if job.get("status") == "running" and job.get("lease_owner") != self.worker_id:
            raise FinancialJobCancelled("lease_lost")
        raise FinancialJobCancelled("worker_stopping")

    def wait(self, seconds: float) -> bool:
        """Cancellation-aware wait for provider/model backoff loops."""

        cancelled = self.cancel_event.wait(max(0.0, float(seconds)))
        if cancelled:
            self.raise_if_cancelled()
        return cancelled


FinancialRunner = Callable[[Mapping, FinancialJobContext], Mapping]


class FinancialJobDispatcher:
    """Capability-gated registry for the five planned financial job types."""

    def __init__(
        self,
        runners: Optional[Mapping[str, FinancialRunner]] = None,
        *,
        settings=None,
    ):
        self.settings = settings
        self.runners: Dict[str, FinancialRunner] = {}
        for job_type, runner in dict(runners or {}).items():
            self.register_runner(job_type, runner)

    def register_runner(self, job_type: str, runner: FinancialRunner) -> None:
        normalized = str(job_type or "").strip()
        if normalized not in FINANCIAL_JOB_TYPES:
            raise ValueError(f"未知金融任务类型: {normalized}")
        if not callable(runner):
            raise TypeError("financial runner must be callable")
        self.runners[normalized] = runner

    def register_with(self, worker) -> None:
        for job_type in FINANCIAL_JOB_TYPES:
            worker.register_handler(
                job_type,
                self._bound_handler(job_type),
                with_context=True,
            )

    def _bound_handler(self, job_type: str):
        def handler(payload: Mapping, context: FinancialJobContext) -> Mapping:
            return self.execute(job_type, payload, context)

        handler.__name__ = f"handle_{job_type}"
        return handler

    def execute(
        self,
        job_type: str,
        payload: Mapping,
        context: FinancialJobContext,
    ) -> Mapping:
        normalized = str(job_type or "").strip()
        if normalized not in FINANCIAL_JOB_TYPES:
            raise FinancialJobExecutionError(
                "未知金融任务类型",
                error_code="unknown_financial_job_type",
                retryable=False,
            )
        if not isinstance(payload, Mapping):
            raise FinancialJobExecutionError(
                "金融任务 payload 必须为对象",
                error_code="invalid_financial_job_payload",
                retryable=False,
            )
        context.raise_if_cancelled()
        capability = FINANCIAL_JOB_CAPABILITIES[normalized]
        capability_state = financial_product_capabilities(
            str(payload.get("industry_pack_id") or ""),
            settings=self.settings,
        )
        if not capability_state["effective"][capability]:
            return {
                "job_type": normalized,
                "status": "skipped",
                "reason": capability_state["reasons"][capability],
                "capability": capability,
            }
        rollout_requirement = _job_rollout_requirement(normalized, payload)
        if not rollout_capability_enabled(rollout_requirement, self.settings):
            return {
                "job_type": normalized,
                "status": "skipped",
                "reason": rollout_capability_reason(
                    rollout_requirement, self.settings
                ),
                "capability": capability,
                "rollout_requirement": rollout_requirement,
            }
        runner = self.runners.get(normalized)
        if runner is None:
            raise FinancialJobExecutionError(
                "金融任务执行器尚未注册",
                error_code="financial_job_runner_unavailable",
                retryable=False,
            )
        try:
            result = runner(dict(payload), context)
        except FinancialJobExecutionError:
            raise
        except Exception as exc:
            retryable = bool(getattr(exc, "retryable", True))
            error_code = str(getattr(exc, "error_code", "financial_job_failed"))
            raise FinancialJobExecutionError(
                f"金融任务执行失败: {error_code}",
                error_code=error_code,
                retryable=retryable,
            ) from exc
        context.raise_if_cancelled()
        if not isinstance(result, Mapping):
            raise FinancialJobExecutionError(
                "金融任务结果必须为对象",
                error_code="invalid_financial_job_result",
                retryable=False,
            )
        try:
            json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise FinancialJobExecutionError(
                "金融任务结果不是有限 JSON",
                error_code="invalid_financial_job_result",
                retryable=False,
            ) from exc
        return {
            **dict(result),
            "job_type": normalized,
            "status": str(result.get("status") or "completed"),
        }

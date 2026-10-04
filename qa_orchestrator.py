#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic, entry-point-neutral state machine for unified QA runs."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Callable, Mapping

from qa_errors import QaPublicError, classify_qa_error
from qa_sse import qa_event
from qa_resilience import STAGE_BUDGET_SECONDS


FULL_STAGES = (
    "plan",
    "level1_retrieval",
    "level1_draft",
    "level2_retrieval",
    "level2_research",
    "conflict_review",
    "synthesis",
    "citation_validation",
)
FAST_STAGES = (
    "plan",
    "level1_retrieval",
    "level1_draft",
    "synthesis",
    "citation_validation",
)
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
DEGRADABLE_STAGES = frozenset({"level2_retrieval", "level2_research"})


def _digest(value) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass
class QaStageFailure(RuntimeError):
    public_error: QaPublicError
    degradable: bool = False

    def __str__(self) -> str:
        return self.public_error.message


class QaCancelled(RuntimeError):
    pass


class QaOrchestrator:
    """Execute every origin through one state graph.

    Stage handlers receive a mutable execution context and return a mapping.
    Their result is stored under ``context["outputs"][stage]``.  A handler may
    raise ``QaStageFailure`` for an already classified failure; all other
    exceptions are converted to a safe public error.
    """

    def __init__(self, store, stage_handlers: Mapping[str, Callable] | None = None, *, audit_logger=None):
        self.store = store
        self.stage_handlers = dict(stage_handlers or {})
        if audit_logger is None:
            try:
                from qa_observability import QaAuditLogger

                audit_logger = QaAuditLogger(store.database)
            except Exception:
                audit_logger = None
        self.audit_logger = audit_logger

    def _audit(self, event_type: str, run: Mapping, payload=None) -> None:
        if self.audit_logger is None:
            return
        try:
            self.audit_logger.record(
                event_type, trace_id=str(run.get("id") or ""), run_id=str(run.get("id") or ""),
                actor_id=str(run.get("owner_user_id") or ""), origin=str(run.get("origin") or ""),
                industry_pack_id=str(run.get("industry_pack_id") or ""), payload=payload or {},
            )
        except Exception:
            pass

    @staticmethod
    def stage_plan(mode: str, origin: str = "") -> tuple[str, ...]:
        # origin is deliberately ignored: both products use the same graph.
        return FAST_STAGES if str(mode or "standard").casefold() == "fast" else FULL_STAGES

    def _emit(self, run_id: str, event_type: str, stage: str, payload=None) -> dict:
        event = qa_event(
            event_type=event_type,
            run_id=run_id,
            event_id=self.store.next_event_id(run_id),
            stage=stage,
            payload=payload or {},
        )
        return self.store.append_event(event)

    def _cancelled(self, run_id: str, external_cancel: Callable[[], bool] | None) -> bool:
        if external_cancel is not None and external_cancel():
            return True
        run = self.store.get_run(run_id)
        return not run or str(run.get("status")) == "cancelled"

    def _handler(self, stage: str) -> Callable:
        handler = self.stage_handlers.get(stage)
        if handler is None:
            raise RuntimeError(f"unified QA stage is not configured: {stage}")
        return handler

    def execute(self, run_id: str, *, external_cancel: Callable[[], bool] | None = None) -> dict:
        run = self.store.get_run(run_id)
        if not run:
            return {"success": False, "retryable": False, "error": "qa run not found"}
        status = str(run.get("status") or "")
        if status == "completed":
            return {"success": True, "run_id": run_id, "status": status, "replayed": True}
        if status in {"failed", "cancelled"}:
            return {"success": False, "retryable": False, "run_id": run_id, "status": status}

        request_payload = dict(run.get("request") or {})
        prior_stages = self.store.stage_runs(run_id)
        completed_outputs = {}
        completed_stages = set()
        for prior in prior_stages:
            if prior.get("status") in {"completed", "degraded"}:
                completed_stages.add(str(prior.get("stage") or ""))
                details = prior.get("details") or {}
                if isinstance(details.get("result"), Mapping):
                    completed_outputs[str(prior.get("stage") or "")] = dict(details["result"])
        context = {"run": run, "request": request_payload, "outputs": completed_outputs}
        stages = self.stage_plan(run.get("mode"), run.get("origin"))
        self.store.update_run(run_id, status="running", stage=stages[0])
        if not self.store.events_after(run_id):
            self._emit(run_id, "run_started", stages[0], {"mode": run.get("mode"), "stages": list(stages)})
            self._audit("run_started", run, {"mode": run.get("mode"), "stages": list(stages)})

        for stage in stages:
            if stage in completed_stages:
                continue
            if self._cancelled(run_id, external_cancel):
                self.store.update_run(run_id, status="cancelled", stage=stage)
                self.store.record_stage(run_id, stage, status="cancelled")
                self._emit(run_id, "done", stage, {"status": "cancelled"})
                return {"success": False, "retryable": False, "run_id": run_id, "status": "cancelled"}

            attempt = self.store.next_stage_attempt(run_id, stage)
            input_hash = _digest(context)
            self.store.update_run(run_id, status="running", stage=stage)
            self.store.record_stage(run_id, stage, status="running", attempt=attempt, input_hash=input_hash)
            self._emit(run_id, "stage_started", stage, {})
            def emit_stage_event(event_type: str, payload=None):
                return self._emit(run_id, event_type, stage, payload or {})

            context["_emit_stage_event"] = emit_stage_event
            started = time.monotonic()
            try:
                output = self._handler(stage)(context)
                elapsed_ms = int((time.monotonic() - started) * 1000)
                if not isinstance(output, Mapping):
                    raise TypeError(f"stage {stage} must return an object")
                output = dict(output)
                context["outputs"][stage] = output
                self.store.record_stage(
                    run_id,
                    stage,
                    status="completed",
                    attempt=attempt,
                    input_hash=input_hash,
                    output_hash=_digest(output),
                    details={
                        "result": output,
                        "performance": {
                            "elapsed_ms": elapsed_ms,
                            "budget_ms": int(STAGE_BUDGET_SECONDS.get(stage, 0) * 1000),
                            "over_budget": bool(STAGE_BUDGET_SECONDS.get(stage) and elapsed_ms > STAGE_BUDGET_SECONDS[stage] * 1000),
                        },
                    },
                )
                self._audit("stage_completed", run, {
                    "stage": stage, "elapsed_ms": elapsed_ms,
                    "over_budget": bool(STAGE_BUDGET_SECONDS.get(stage) and elapsed_ms > STAGE_BUDGET_SECONDS[stage] * 1000),
                })
                event_type = {
                    "level1_retrieval": "retrieval_result",
                    "level1_draft": "level1_ready",
                    "level2_retrieval": "retrieval_result",
                    "level2_research": "ragflow_research_progress",
                    "conflict_review": "conflict_detected",
                }.get(stage, "stage_completed")
                try:
                    from qa_ui_adapter import stage_event_payload

                    event_output = stage_event_payload(stage, output)
                except Exception:
                    event_output = output
                self._emit(run_id, event_type, stage, {"result": event_output})
                if stage == "plan":
                    try:
                        from qa_question_templates import render_question_plan_answer

                        plan_text = render_question_plan_answer(output.get("question_plan") or {})
                    except Exception:
                        plan_text = ""
                    if plan_text:
                        self._emit(
                            run_id,
                            "answer_delta",
                            stage,
                            {"delta": plan_text.rstrip() + "\n\n", "offset": 0, "source": "question_plan"},
                        )
                if stage == "synthesis" and str(output.get("answer") or "") and not context.get("_answer_delta_streamed"):
                    answer = str(output.get("answer") or "")
                    for offset in range(0, len(answer), 240):
                        self._emit(
                            run_id,
                            "answer_delta",
                            stage,
                            {"delta": answer[offset:offset + 240], "offset": offset},
                        )
            except QaStageFailure as exc:
                public = exc.public_error
                if exc.degradable and stage in DEGRADABLE_STAGES and run.get("mode") != "fast":
                    item = {"stage": stage, **public.to_event_payload(trace_id=run_id)}
                    context["outputs"][stage] = {"degraded": True, "error": item}
                    self.store.mark_degraded(run_id, item)
                    self.store.record_stage(run_id, stage, status="degraded", attempt=attempt, error_code=public.code, details={"result": context["outputs"][stage], **item})
                    self._emit(run_id, "degraded", stage, item)
                    self._audit("stage_degraded", run, {"stage": stage, "error_code": public.code})
                    continue
                self.store.record_stage(run_id, stage, status="failed", attempt=attempt, error_code=public.code, details=public.to_event_payload(trace_id=run_id))
                self.store.update_run(run_id, status="retry_wait" if public.retryable else "failed", stage=stage)
                self._emit(run_id, "error", stage, public.to_event_payload(trace_id=run_id))
                self._audit("stage_failed", run, {"stage": stage, "error_code": public.code, "retryable": public.retryable})
                return {"success": False, "retryable": public.retryable, "run_id": run_id, "status": "retry_wait" if public.retryable else "failed", "error": public.message, "error_code": public.code}
            except Exception as exc:
                public = classify_qa_error(exc, stage=stage)
                self.store.record_stage(run_id, stage, status="failed", attempt=attempt, error_code=public.code, details=public.to_event_payload(trace_id=run_id))
                self.store.update_run(run_id, status="retry_wait" if public.retryable else "failed", stage=stage)
                self._emit(run_id, "error", stage, public.to_event_payload(trace_id=run_id))
                self._audit("stage_failed", run, {"stage": stage, "error_code": public.code, "retryable": public.retryable})
                return {"success": False, "retryable": public.retryable, "run_id": run_id, "status": "retry_wait" if public.retryable else "failed", "error": public.message, "error_code": public.code}

        final_answer = dict(context["outputs"].get("citation_validation") or context["outputs"].get("synthesis") or {})
        self.store.update_run(run_id, status="completed", stage="completed", final_answer=final_answer)
        self._emit(run_id, "done", "completed", {"status": "completed", "final_answer": final_answer})
        self._audit("run_completed", run, {
            "status": final_answer.get("status"), "degraded": bool(final_answer.get("degraded")),
            "evidence_count": len(final_answer.get("evidence") or []),
            "citation_count": len(final_answer.get("citations") or []),
            "conflict_count": len(final_answer.get("conflicts") or []),
        })
        return {"success": True, "run_id": run_id, "status": "completed", "final_answer": final_answer}


__all__ = [
    "DEGRADABLE_STAGES",
    "FAST_STAGES",
    "FULL_STAGES",
    "QaCancelled",
    "QaOrchestrator",
    "QaStageFailure",
]

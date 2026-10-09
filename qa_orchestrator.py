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
    "logic_validation",
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
    "logic_validation",
    "level1_draft",
    "synthesis",
    "citation_validation",
)
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
# logic_validation 允许"降级通过"：多跳有缺口时必须继续出答案，
# 但缺口要在阶段结果里显式标注（不能悄悄跳过）。
DEGRADABLE_STAGES = frozenset({"level2_retrieval", "level2_research", "logic_validation"})


def _digest(value) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _stream_text_pieces(text: str, *, chunk_size: int = 42):
    source = str(text or "")
    if not source:
        return
    for block in source.splitlines(keepends=True):
        if not block:
            continue
        pieces = []
        start = 0
        for index, char in enumerate(block):
            if char in "。！？!?；;\n" and index + 1 - start >= 14:
                pieces.append(block[start:index + 1])
                start = index + 1
        if start < len(block):
            rest = block[start:]
            while len(rest) > chunk_size:
                pieces.append(rest[:chunk_size])
                rest = rest[chunk_size:]
            if rest:
                pieces.append(rest)
        for piece in pieces:
            if piece:
                yield piece


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
            # 阶段 01（F-5，仅注释、不改逻辑）：**当前 trace_id == run_id** ——
            # 一次 run 就是一条研究轨迹，`qa_audit_events` / `qa_reasoning_traces` 都用
            # run_id 关联（见 qa_schema 里两张表都有 run_id/trace_id 列）。
            # 两者暂时取同一个值（`str(run.get("id"))`），是因为到 Phase 16 之前没有
            # 独立于 run 的跨轮 trace（多轮追问会各起一个 run）。等引入真正的 trace 载体时，
            # 这里改成 run["trace_id"]（取不到再退回 run_id），下游按列读的代码不用动。
            self.audit_logger.record(
                event_type, trace_id=str(run.get("id") or ""), run_id=str(run.get("id") or ""),
                actor_id=str(run.get("owner_user_id") or ""), origin=str(run.get("origin") or ""),
                industry_pack_id=str(run.get("industry_pack_id") or ""), payload=payload or {},
            )
        except Exception:
            pass

    @staticmethod
    def stage_plan(mode: str, origin: str = "", *, level2_enabled: bool | None = None) -> tuple[str, ...]:
        """阶段链：fast 走精简链，standard 走全链。

        阶段 6-4：`level2_enabled=False` 时把 `level2_retrieval` / `level2_research` /
        `conflict_review` 从链上**直接摘掉**——它们的唯一输入是二级检索的产物，
        二级关掉后这些阶段只会空转（每次 run 都白记一条 stage 记录、白推一次事件），
        而且会让前端看到"跑了但没内容"的阶段。
        origin 故意忽略：两个产品共用同一条链。
        """
        if str(mode or "standard").casefold() == "fast":
            return FAST_STAGES
        if level2_enabled is False:
            return tuple(stage for stage in FULL_STAGES
                         if stage not in ("level2_retrieval", "level2_research", "conflict_review"))
        return FULL_STAGES

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
        # 阶段 6-4：二级检索关掉时，别让依赖它的阶段空转
        _level2_enabled: bool | None = None
        try:
            flags = getattr(self, "feature_flags", None)
            if flags is not None:
                snapshot = flags.snapshot() or {}
                if "level2_enabled" in snapshot:
                    _level2_enabled = bool(snapshot.get("level2_enabled"))
            elif run.get("level2_enabled") is not None:
                _level2_enabled = bool(run.get("level2_enabled"))
        except Exception:
            _level2_enabled = None
        stages = self.stage_plan(run.get("mode"), run.get("origin"), level2_enabled=_level2_enabled)
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
                # Phase 00 · P00-04（Cost 基线前置）：把阶段输出里的模型 token 用量**取出**，
                # 交给 record_stage 写进 qa_stage_runs.token_usage_json。
                # 必须从 output 里摘掉：level1_draft / synthesis 的输出会原样进入后续阶段的
                # **严格 schema 校验**（如 citation_validation 会对 synthesis 输出跑
                # validate_final_answer，additionalProperties=False），多带一个键就会把整条 run
                # 判失败；而 details["result"] 还要作为"已完成阶段输出"回放（见上面 completed_outputs），
                # 留着同样会把回放的 run 判失败。拿到的用量只进 details，不影响既有结果结构。
                token_usage = output.pop("token_usage", None)
                context["outputs"][stage] = output
                details = {
                    "result": output,
                    "performance": {
                        "elapsed_ms": elapsed_ms,
                        "budget_ms": int(STAGE_BUDGET_SECONDS.get(stage, 0) * 1000),
                        "over_budget": bool(STAGE_BUDGET_SECONDS.get(stage) and elapsed_ms > STAGE_BUDGET_SECONDS[stage] * 1000),
                    },
                }
                # 存在且非空才传；只认非负整数计数（拿不到/脏值一律不写，绝不用 0 冒充，
                # 也不让 NaN、嵌套对象这类内容进 record_stage 的严格 JSON 序列化）
                if isinstance(token_usage, Mapping) and token_usage:
                    clean_usage = {
                        str(key): int(value)
                        for key, value in token_usage.items()
                        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                    }
                    if clean_usage:
                        details["token_usage"] = clean_usage
                self.store.record_stage(
                    run_id,
                    stage,
                    status="completed",
                    attempt=attempt,
                    input_hash=input_hash,
                    output_hash=_digest(output),
                    details=details,
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
                    offset = 0
                    for piece in _stream_text_pieces(answer):
                        self._emit(
                            run_id,
                            "answer_delta",
                            stage,
                            {"delta": piece, "offset": offset, "source": "synthesis_fallback"},
                        )
                        offset += len(piece)
                        time.sleep(0.018)
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

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pure compatibility mappings between the legacy chat API and QA v1."""

from __future__ import annotations

from typing import Mapping


def legacy_request_to_qa(payload: Mapping, *, industry_pack_id: str) -> dict:
    messages = payload.get("messages") or []
    if not isinstance(messages, list):
        messages = []
    history_context = payload.get("history_context") or []
    if not isinstance(history_context, list):
        history_context = []
    question = str(payload.get("message") or payload.get("question") or "").strip()
    if not question:
        for item in reversed(messages):
            if isinstance(item, Mapping) and str(item.get("role")) == "user":
                question = str(item.get("content") or "").strip()
                if question:
                    break
    return {
        "session_id": str(payload.get("session_id") or payload.get("conversation_id") or ""),
        "question": question,
        "industry_pack_id": str(industry_pack_id or ""),
        "mode": str(payload.get("mode") or "standard"),
        "draft_provider": str(payload.get("provider") or payload.get("model_provider") or payload.get("model") or "local"),
        "web_search": bool(payload.get("web_search", payload.get("enable_web_search", False))),
        "page_context": dict(payload.get("page_context") or {}),
        "client_capabilities": ["qa-sse.v1"],
        "messages": list(messages),
        "history_context": list(history_context),
        "history_source_session_id": str(payload.get("history_source_session_id") or ""),
    }


def qa_event_to_legacy(event: Mapping) -> dict:
    event_type = str(event.get("type") or "")
    legacy_type = {
        "run_started": "status",
        "stage_started": "searching",
        "stage_progress": "searching",
        "retrieval_result": "retrieval",
        "level1_ready": "search_done",
        "ragflow_research_progress": "searching",
        "answer_delta": "chunk",
        "done": "done",
        "error": "error",
        "action_required": "error",
        "degraded": "status",
    }.get(event_type, "status")
    payload = dict(event.get("payload") or {})
    value = {
        "type": legacy_type,
        "qa_run_id": str(event.get("run_id") or ""),
        "event_id": int(event.get("event_id") or 0),
        "stage": str(event.get("stage") or ""),
        "data": payload,
    }
    if legacy_type == "chunk":
        value["content"] = str(payload.get("delta") or "")
    elif legacy_type == "done":
        final = payload.get("final_answer") or {}
        value["answer"] = str(final.get("answer") or "")
        value["final_answer"] = final
        value["result"] = final
    elif legacy_type == "error":
        value["message"] = str(payload.get("message") or "问答执行失败")
        value["code"] = str(payload.get("code") or "QA_FAILED")
        value["actions"] = list(payload.get("actions") or [])
    elif legacy_type == "retrieval":
        value["articles"] = list((payload.get("result") or {}).get("evidence") or [])
    return value


__all__ = ["legacy_request_to_qa", "qa_event_to_legacy"]

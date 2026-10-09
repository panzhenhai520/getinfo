#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Versioned contracts shared by every unified-question entry point.

The Gateway, getinfo UI adapter, and RAGFlow UI adapter must exchange these
shapes.  Model output is never trusted until it passes the relevant schema and
the evidence-reference integrity checks in this module.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Mapping

from jsonschema import Draft202012Validator


QA_CONTRACT_VERSION = "unified-qa-v1"
QA_SSE_PROTOCOL_VERSION = "qa-sse-v1"

QA_ORIGINS = ("getinfo_ui", "ragflow_ui", "api")
QA_MODES = ("standard", "deep", "fast")
QA_STAGES = (
    "plan",
    "level1_retrieval",
    "logic_validation",
    "level1_draft",
    "level2_retrieval",
    "level2_research",
    "conflict_review",
    "synthesis",
    "citation_validation",
    "completed",
)
QA_EVENT_TYPES = (
    "run_started",
    "stage_started",
    "stage_progress",
    "retrieval_result",
    "level1_ready",
    "ragflow_research_progress",
    "conflict_detected",
    "citation_added",
    "answer_delta",
    "action_required",
    "degraded",
    "stage_completed",
    "done",
    "error",
)

# 阶段 01（graph-rag-v2 通用包 F-9）：检索通道 / 失败策略 / 停止原因 / 审计事件类型
# 统一从 `qa_graph_contracts` 取（单一事实源），这里只做再导出，**取值一字不改**，
# 便于既有调用点从契约层 import，避免同一枚举在多处漂移。
from qa_graph_contracts import (  # noqa: E402  （放在契约常量之后，避免循环导入）
    GRAPH_CONTRACT_VERSION,
    QA_AUDIT_EVENT_TYPES,
    QA_FAILURE_POLICIES,
    QA_RETRIEVAL_ROUTES,
    QA_STOP_REASONS,
)

# 阶段的"四图角色"分组（阶段 01 F-9）：阶段名 ≠ 节点类型，但要让执行链能对上四图。
# 取值只做归类，不影响任何执行顺序（顺序仍由 qa_orchestrator.FULL_STAGES 决定）。
QA_STAGE_ROLES = {
    "plan": "context",
    "level1_retrieval": "evidence",
    "logic_validation": "evidence",
    "level1_draft": "execution",
    "level2_retrieval": "evidence",
    "level2_research": "execution",
    "conflict_review": "evidence",
    "synthesis": "execution",
    "citation_validation": "evidence",
    "completed": "execution",
}

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_SAFE_PACK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_FORBIDDEN_CLIENT_FIELDS = {
    "user_id",
    "pack_user_id",
    "ragflow_kb_id",
    "ragflow_app_id",
    "allowed_kb_ids",
    "credential_ref",
}
_LEGACY_TEMPLATE_MARKERS = (
    "以下是未经核验的旧会话片段",
    "以下是未经过新版深度核验的旧会话片段",
    "仅作为问题背景",
    "请重新检索、核验并回答",
    "请重新检索核验并回答",
    "我的后续问题",
)
_GENERIC_QUESTION_WORDS = {
    "请", "重新", "检索", "核验", "回答", "分析", "一下", "这个", "这些",
    "问题", "后续", "旧会话", "片段", "背景", "新版", "深度", "根据", "帮我",
}


class QaContractError(ValueError):
    """Raised when an API or model payload violates the unified contract."""


def _meaningful_question_tail(question: str) -> str:
    tail = str(question or "").strip()
    for marker in ("我的后续问题：", "我的后续问题:", "后续问题：", "后续问题:"):
        if marker in tail:
            tail = tail.rsplit(marker, 1)[-1].strip()
    return tail


def _looks_template_only_question(question: str) -> bool:
    text = re.sub(r"\s+", "", str(question or ""))
    if not text:
        return True
    marker_hits = sum(1 for marker in _LEGACY_TEMPLATE_MARKERS if marker.replace("、", "") in text.replace("、", ""))
    tail = _meaningful_question_tail(question)
    if marker_hits >= 2 and len(tail) < 8:
        return True
    normalized_tail = re.sub(r"[，。！？；：、,.!?:;\s]+", "", tail)
    if not normalized_tail:
        return True
    tokens = re.findall(r"[\u4e00-\u9fff]{1,}|[A-Za-z0-9][A-Za-z0-9._-]*", normalized_tail)
    joined = "".join(tokens)
    if marker_hits and len(joined) < 12:
        return True
    generic_chars = "".join(_GENERIC_QUESTION_WORDS)
    remaining = "".join(ch for ch in joined if ch not in generic_chars)
    generic_phrases = {
        "请重新检索核验并回答",
        "请重新检索核验",
        "重新检索核验",
        "请重新回答",
        "请分析一下",
        "帮我分析一下",
    }
    return joined in generic_phrases or (marker_hits > 0 and len(joined) <= 18 and len(remaining) < 3)


def _strict_object(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


EVIDENCE_SCHEMA = _strict_object(
    {
        "evidence_ref": {"type": "string", "minLength": 1, "maxLength": 200},
        "source_type": {
            "type": "string",
            # graph = 知识图谱派生事实（事件边/属性边）。它指向源文章但**不是**文章本身，
            # 所以与文章证据并存（见 qa_retrieval._graph_evidence 的说明）。
            "enum": ["page_context", "article", "ragflow_chunk", "web", "official", "graph"],
        },
        "title": {"type": "string", "minLength": 1, "maxLength": 1000},
        "source_url": {"type": "string", "maxLength": 4000},
        "content_excerpt": {"type": "string", "maxLength": 12000},
        "published_at": {"type": ["string", "null"], "maxLength": 80},
        "fetched_at": {"type": ["string", "null"], "maxLength": 80},
        "article_id": {"type": ["integer", "null"], "minimum": 1},
        "ragflow_kb_id": {"type": ["string", "null"], "maxLength": 200},
        "document_id": {"type": ["string", "null"], "maxLength": 200},
        "chunk_id": {"type": ["string", "null"], "maxLength": 200},
        "score": {"type": ["number", "null"], "minimum": 0},
        "authority_level": {"type": ["integer", "null"], "minimum": 0, "maximum": 100},
        # 发布时间精度（阶段 4 起证据条目自带）：契约必须放行，否则一级草稿校验会因
        # "Additional properties are not allowed" 整条失败降级——实测踩到，且只在
        # **证据来自文章**（article:<id>）时才触发。
        "published_at_utc": {"type": ["string", "null"], "maxLength": 80},
        "published_precision": {"type": ["string", "null"], "maxLength": 20},
        "published_timezone": {"type": ["string", "null"], "maxLength": 60},
        "published_time_note": {"type": ["string", "null"], "maxLength": 200},
        "excerpt_chars": {"type": ["integer", "null"], "minimum": 0},
        "retrieval_method": {"type": ["string", "null"], "maxLength": 80},
        "match_reason": {"type": ["string", "null"], "maxLength": 1000},
        "relationship": {
            "type": ["string", "null"],
            "enum": ["supports", "contradicts", "qualifies", "context", None],
        },
        "metadata": {"type": "object"},
    },
    ["evidence_ref", "source_type", "title", "source_url", "content_excerpt", "metadata"],
)

CLAIM_SCHEMA = _strict_object(
    {
        "claim_id": {"type": "string", "minLength": 1, "maxLength": 160},
        "text": {"type": "string", "minLength": 1, "maxLength": 4000},
        "claim_type": {
            "type": "string",
            "enum": ["current_fact", "historical_fact", "interpretation", "forecast", "background"],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "valid_from": {"type": ["string", "null"], "maxLength": 80},
        "valid_to": {"type": ["string", "null"], "maxLength": 80},
        "scope": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
            "maxItems": 20,
            "uniqueItems": True,
        },
        "evidence_refs": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
            "maxItems": 30,
            "uniqueItems": True,
        },
        "needs_verification": {"type": "boolean"},
        "verification_status": {
            "type": "string",
            "enum": ["unverified", "confirmed", "corrected", "qualified", "conflicted", "insufficient_evidence"],
        },
    },
    [
        "claim_id",
        "text",
        "claim_type",
        "confidence",
        "valid_from",
        "valid_to",
        "scope",
        "evidence_refs",
        "needs_verification",
        "verification_status",
    ],
)

CONFLICT_SCHEMA = _strict_object(
    {
        "conflict_id": {"type": "string", "minLength": 1, "maxLength": 160},
        "subject": {"type": "string", "minLength": 1, "maxLength": 2000},
        "conflict_type": {
            "type": "string",
            "enum": ["real_conflict", "time_change", "scope_difference", "method_difference", "opinion_difference"],
        },
        "claim_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 160},
            "minItems": 2,
            "maxItems": 20,
            "uniqueItems": True,
        },
        "evidence_refs": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
            "maxItems": 40,
            "uniqueItems": True,
        },
        "resolution": {"type": "string", "enum": ["resolved", "unresolved"]},
        "rationale": {"type": "string", "maxLength": 4000},
        "rule_version": {"type": ["string", "null"], "maxLength": 80},
    },
    ["conflict_id", "subject", "conflict_type", "claim_ids", "evidence_refs", "resolution", "rationale"],
)

LEVEL1_RESULT_SCHEMA = _strict_object(
    {
        "contract_version": {"const": QA_CONTRACT_VERSION},
        "draft_answer": {"type": "string", "maxLength": 30000},
        "claims": {"type": "array", "items": CLAIM_SCHEMA, "maxItems": 60},
        "entities": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
            "maxItems": 80,
            "uniqueItems": True,
        },
        "timeline_hints": {"type": "array", "items": {"type": "object"}, "maxItems": 40},
        "gaps": {"type": "array", "items": {"type": "string", "maxLength": 1000}, "maxItems": 40},
        "followup_queries": {"type": "array", "items": {"type": "string", "maxLength": 1000}, "maxItems": 20},
        "evidence": {"type": "array", "items": EVIDENCE_SCHEMA, "maxItems": 100},
        "citations": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
            "maxItems": 100,
            "uniqueItems": True,
        },
    },
    [
        "contract_version",
        "draft_answer",
        "claims",
        "entities",
        "timeline_hints",
        "gaps",
        "followup_queries",
        "evidence",
        "citations",
    ],
)

LEVEL2_RESULT_SCHEMA = _strict_object(
    {
        "contract_version": {"const": QA_CONTRACT_VERSION},
        "confirmed_claims": {"type": "array", "items": CLAIM_SCHEMA, "maxItems": 80},
        "corrected_claims": {"type": "array", "items": CLAIM_SCHEMA, "maxItems": 80},
        "new_findings": {"type": "array", "items": CLAIM_SCHEMA, "maxItems": 80},
        "timeline": {"type": "array", "items": {"type": "object"}, "maxItems": 100},
        "horizontal_comparisons": {"type": "array", "items": {"type": "object"}, "maxItems": 100},
        "conflicts": {"type": "array", "items": CONFLICT_SCHEMA, "maxItems": 60},
        "multi_hop_findings": {"type": "array", "items": {"type": "object"}, "maxItems": 60},
        "evidence_gaps": {"type": "array", "items": {"type": "string", "maxLength": 1000}, "maxItems": 60},
        "evidence": {"type": "array", "items": EVIDENCE_SCHEMA, "maxItems": 160},
        "citations": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
            "maxItems": 160,
            "uniqueItems": True,
        },
        "research_audit": {"type": "object"},
    },
    [
        "contract_version",
        "confirmed_claims",
        "corrected_claims",
        "new_findings",
        "timeline",
        "horizontal_comparisons",
        "conflicts",
        "multi_hop_findings",
        "evidence_gaps",
        "evidence",
        "citations",
    ],
)

FINAL_ANSWER_SCHEMA = _strict_object(
    {
        "contract_version": {"const": QA_CONTRACT_VERSION},
        "status": {"type": "string", "enum": ["ready", "partial", "insufficient_evidence"]},
        "answer": {"type": "string", "minLength": 1, "maxLength": 80000},
        "sections": {"type": "object"},
        "claims": {"type": "array", "items": CLAIM_SCHEMA, "maxItems": 120},
        "conflicts": {"type": "array", "items": CONFLICT_SCHEMA, "maxItems": 60},
        "evidence": {"type": "array", "items": EVIDENCE_SCHEMA, "maxItems": 200},
        "citations": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
            "maxItems": 200,
            "uniqueItems": True,
        },
        "citation_map": {
            "type": "object",
            "propertyNames": {"pattern": "^\\[\\d+\\]$"},
            "additionalProperties": {"type": "string", "minLength": 1, "maxLength": 200},
        },
        "cutoff_at": {"type": "string", "maxLength": 80},
        "degraded": {"type": "boolean"},
        "degradation_reasons": {"type": "array", "items": {"type": "string", "maxLength": 500}, "maxItems": 20},
        "models": {"type": "object"},
    },
    [
        "contract_version",
        "status",
        "answer",
        "sections",
        "claims",
        "conflicts",
        "evidence",
        "citations",
        "cutoff_at",
        "degraded",
        "degradation_reasons",
        "models",
    ],
)

QA_EVENT_SCHEMA = _strict_object(
    {
        "protocol_version": {"const": QA_SSE_PROTOCOL_VERSION},
        "type": {"type": "string", "enum": list(QA_EVENT_TYPES)},
        "run_id": {"type": "string", "minLength": 1, "maxLength": 160},
        "event_id": {"type": "integer", "minimum": 1},
        "stage": {"type": "string", "enum": list(QA_STAGES)},
        "timestamp": {"type": "string", "minLength": 1, "maxLength": 80},
        "payload": {"type": "object"},
    },
    ["protocol_version", "type", "run_id", "event_id", "stage", "timestamp", "payload"],
)


def _validate(schema: dict, value: Any, label: str) -> dict:
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda item: list(item.path))
    if errors:
        error = errors[0]
        path = ".".join(str(item) for item in error.absolute_path) or "$"
        raise QaContractError(f"{label} 不符合 {QA_CONTRACT_VERSION}: {path}: {error.message}")
    return copy.deepcopy(dict(value))


def _validate_reference_integrity(value: Mapping[str, Any], claim_fields: tuple[str, ...]) -> None:
    evidence_refs = {
        str(item.get("evidence_ref"))
        for item in value.get("evidence") or []
        if isinstance(item, Mapping) and item.get("evidence_ref")
    }
    referenced: set[str] = set(str(item) for item in value.get("citations") or [])
    for field in claim_fields:
        for claim in value.get(field) or []:
            referenced.update(str(item) for item in claim.get("evidence_refs") or [])
    for conflict in value.get("conflicts") or []:
        referenced.update(str(item) for item in conflict.get("evidence_refs") or [])
    missing = sorted(referenced - evidence_refs)
    if missing:
        raise QaContractError(f"引用了不存在的 evidence_ref: {', '.join(missing[:10])}")


def validate_level1_result(value: Mapping[str, Any]) -> dict:
    result = _validate(LEVEL1_RESULT_SCHEMA, value, "一级结果")
    _validate_reference_integrity(result, ("claims",))
    return result


def validate_level2_result(value: Mapping[str, Any]) -> dict:
    result = _validate(LEVEL2_RESULT_SCHEMA, value, "二级结果")
    _validate_reference_integrity(result, ("confirmed_claims", "corrected_claims", "new_findings"))
    return result


def validate_final_answer(value: Mapping[str, Any]) -> dict:
    result = _validate(FINAL_ANSWER_SCHEMA, value, "最终答案")
    _validate_reference_integrity(result, ("claims",))
    return result


def validate_qa_event(value: Mapping[str, Any]) -> dict:
    return _validate(QA_EVENT_SCHEMA, value, "SSE 事件")


def normalize_qa_request(payload: Mapping[str, Any], *, trusted_origin: str | None = None) -> dict:
    if not isinstance(payload, Mapping):
        raise QaContractError("请求体必须是 JSON 对象")
    forbidden = sorted(_FORBIDDEN_CLIENT_FIELDS.intersection(payload))
    if forbidden:
        raise QaContractError(f"客户端不得指定受保护字段: {', '.join(forbidden)}")

    question = str(payload.get("question") or "").strip()
    if not question:
        raise QaContractError("问题不能为空")
    if len(question) > 20000:
        raise QaContractError("问题长度不能超过 20000 个字符")
    if _looks_template_only_question(question):
        raise QaContractError("请填写真实问题，不能只提交“请重新检索核验”等模板语")

    session_id = str(payload.get("session_id") or "").strip()
    if session_id and not _SAFE_ID.fullmatch(session_id):
        raise QaContractError("session_id 格式无效")
    pack_id = str(payload.get("industry_pack_id") or "").strip()
    if not pack_id or not _SAFE_PACK.fullmatch(pack_id):
        raise QaContractError("industry_pack_id 格式无效")

    mode = str(payload.get("mode") or "standard").strip().casefold()
    if mode not in QA_MODES:
        raise QaContractError(f"未知问答模式: {mode}")
    origin = str(trusted_origin or payload.get("origin") or "getinfo_ui").strip().casefold()
    if origin not in QA_ORIGINS:
        raise QaContractError(f"未知问答入口: {origin}")

    provider = str(payload.get("draft_provider") or "local").strip().casefold()
    if not provider or not _SAFE_ID.fullmatch(provider):
        raise QaContractError("draft_provider 格式无效")
    page_context = payload.get("page_context") or {}
    if not isinstance(page_context, Mapping):
        raise QaContractError("page_context 必须是 JSON 对象")
    capabilities = payload.get("client_capabilities") or [QA_SSE_PROTOCOL_VERSION]
    if not isinstance(capabilities, list) or any(not isinstance(item, str) for item in capabilities):
        raise QaContractError("client_capabilities 必须是字符串数组")

    def _bounded_messages(field: str, *, max_messages: int = 16, max_chars: int = 16000) -> list[dict]:
        value = payload.get(field)
        if not isinstance(value, list):
            return []
        selected = []
        remaining = max(1000, int(max_chars or 16000))
        for item in reversed(value):
            if not isinstance(item, Mapping):
                continue
            role = str(item.get("role") or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            if len(content) > remaining:
                content = content[-remaining:]
            selected.append({"role": role, "content": content})
            remaining -= len(content)
            if len(selected) >= max_messages or remaining <= 0:
                break
        selected.reverse()
        return selected

    messages = _bounded_messages("messages")
    history_context = _bounded_messages("history_context", max_messages=24, max_chars=24000)
    history_source_session_id = str(payload.get("history_source_session_id") or "").strip()
    history_source_session_id = re.sub(r"[^A-Za-z0-9_.:-]", "", history_source_session_id)[:160]

    return {
        "contract_version": QA_CONTRACT_VERSION,
        "session_id": session_id,
        "question": question,
        "industry_pack_id": pack_id,
        "mode": mode,
        "draft_provider": provider,
        "web_search": bool(payload.get("web_search", False)),
        "page_context": copy.deepcopy(dict(page_context)),
        "client_capabilities": list(dict.fromkeys(capabilities))[:20],
        "origin": origin,
        "messages": messages,
        "history_context": history_context,
        "history_source_session_id": history_source_session_id,
    }


__all__ = [
    "CLAIM_SCHEMA",
    "CONFLICT_SCHEMA",
    "EVIDENCE_SCHEMA",
    "FINAL_ANSWER_SCHEMA",
    "LEVEL1_RESULT_SCHEMA",
    "LEVEL2_RESULT_SCHEMA",
    "QA_CONTRACT_VERSION",
    "QA_EVENT_SCHEMA",
    "QA_EVENT_TYPES",
    "QA_MODES",
    "QA_ORIGINS",
    "QA_SSE_PROTOCOL_VERSION",
    "QA_STAGES",
    "QaContractError",
    "normalize_qa_request",
    "validate_final_answer",
    "validate_level1_result",
    "validate_level2_result",
    "validate_qa_event",
]

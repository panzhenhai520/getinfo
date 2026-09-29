#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Resolve financial chat targets without guessing stable instrument identity."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Mapping, Optional, Sequence

from jsonschema import Draft202012Validator

from financial_instruments import stable_instrument_key


TARGET_RESOLUTION_SCHEMA_VERSION = "financial-target-resolution-v1"
TARGET_STATUSES = (
    "skipped",
    "no_target",
    "resolved",
    "clarification_required",
    "degraded",
)
TARGET_RESOLUTION_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "schema_version", "status", "targets", "candidate_count",
        "needs_clarification", "clarification", "resolution_source",
        "target_echo", "context_inherited", "resumed_from_route_key",
        "resume_context", "route_destination", "llm_used", "reason_codes",
    ],
    "properties": {
        "schema_version": {"const": TARGET_RESOLUTION_SCHEMA_VERSION},
        "status": {"enum": list(TARGET_STATUSES)},
        "targets": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "instrument_id", "instrument_key", "canonical_symbol", "display_name",
                    "asset_type", "market", "exchange", "currency", "country_code",
                    "share_class",
                ],
                "additionalProperties": False,
                "properties": {
                    "instrument_id": {"type": "integer", "minimum": 1},
                    "instrument_key": {"type": "string", "minLength": 1},
                    "canonical_symbol": {"type": "string", "minLength": 1},
                    "display_name": {"type": "string", "minLength": 1},
                    "asset_type": {"type": "string", "minLength": 1},
                    "market": {"type": "string", "minLength": 1},
                    "exchange": {"type": "string"},
                    "currency": {"type": "string"},
                    "country_code": {"type": "string"},
                    "share_class": {"type": ["string", "null"]},
                },
            },
        },
        "candidate_count": {"type": "integer", "minimum": 0},
        "needs_clarification": {"type": "boolean"},
        "clarification": {"type": "object"},
        "resolution_source": {"type": "string"},
        "target_echo": {"type": "string"},
        "context_inherited": {"type": "boolean"},
        "resumed_from_route_key": {"type": ["string", "null"]},
        "resume_context": {"type": "object"},
        "route_destination": {"type": "string"},
        "llm_used": {"type": "boolean"},
        "reason_codes": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}
_VALIDATOR = Draft202012Validator(TARGET_RESOLUTION_SCHEMA)

_BARE_SIX_DIGIT = re.compile(r"(?<![\d.])\d{6}(?![\d.]|\.(?:SH|SZ|OF))", re.I)
_CONTEXT_REFERENCE = re.compile(
    r"它|这只|该股|该基金|该指数|前者|后者|上述|继续|接着|再分析|"
    r"那(?:现在|今天|目前)?呢|那[^。！？?\n]{1,16}呢|"
    r"\bit\b|\bthis (?:stock|fund|index)\b|\bcontinue\b",
    re.I,
)

# Provider execution advances the persisted route status, but it does not
# invalidate the instrument the user explicitly resolved.  These outcomes may
# therefore supply same-session target context for a later pronoun/follow-up.
_CONFIRMED_TARGET_ROUTE_STATUSES = {
    "target_resolved",
    "clarification_resolved",
    "realtime_query_planned",
    "realtime_query_ready",
    "realtime_query_stale",
    "realtime_query_conflict",
    "realtime_query_unavailable",
    "full_research_planned",
    "full_research_cache_hit",
    "full_research_queued",
    "full_research_running",
    "full_research_mixed",
    "full_research_failed",
    "full_research_cancelled",
    "full_research_unavailable",
    "latest_bundle_planned",
    "latest_bundle_ready",
    "latest_bundle_partial",
    "latest_bundle_unavailable",
}


def validate_target_resolution(payload: Mapping[str, object]) -> dict:
    result = dict(payload)
    result["targets"] = _dedupe_targets(result.get("targets") or [])
    clarification = result.get("clarification")
    if isinstance(clarification, Mapping) and isinstance(
        clarification.get("options"), list
    ):
        result["clarification"] = {
            **clarification,
            "options": _dedupe_targets(clarification.get("options") or []),
        }
    _VALIDATOR.validate(result)
    return result


def _ensure_instrument_key(value: Mapping[str, object]) -> dict:
    target = dict(value)
    if not target.get("instrument_key"):
        target["instrument_key"] = stable_instrument_key(
            canonical_symbol=str(target.get("canonical_symbol") or ""),
            asset_type=str(target.get("asset_type") or ""),
            market=str(target.get("market") or ""),
            exchange=str(target.get("exchange") or ""),
            country_code=str(target.get("country_code") or ""),
        )
    return target


def _target(record) -> dict:
    return {
        "instrument_id": int(record.instrument_id),
        "instrument_key": str(record.instrument_key),
        "canonical_symbol": str(record.canonical_symbol),
        "display_name": str(record.display_name),
        "asset_type": str(record.asset_type),
        "market": str(record.market),
        "exchange": str(record.exchange),
        "currency": str(record.currency),
        "country_code": str(record.country_code),
        "share_class": record.share_class or None,
    }


def _dedupe_targets(values: Sequence[Mapping[str, object]]) -> list[dict]:
    result = []
    seen = set()
    for value in values:
        instrument_id = int(value["instrument_id"])
        if instrument_id in seen:
            continue
        seen.add(instrument_id)
        result.append(_ensure_instrument_key(value))
    return result


def _echo(targets: Sequence[Mapping[str, object]]) -> str:
    return "、".join(
        f"{item['display_name']}（{item['canonical_symbol']}）" for item in targets
    )


def _as_of_date(financial_intent: Mapping[str, object]) -> Optional[str]:
    as_of = financial_intent.get("as_of")
    if not isinstance(as_of, Mapping):
        return None
    for field in ("end_local", "end_utc", "resolved_at_utc"):
        if as_of.get(field):
            return str(as_of[field])[:10]
    return None


def _question_filters(question: str) -> dict:
    text = unicodedata.normalize("NFKC", str(question or ""))
    upper = text.upper()
    result = {"exchange": "", "asset_type": "", "share_class": "", "currency": ""}
    if re.search(r"上交所|沪市|上海证券交易所|\.SH\b", text, re.I):
        result["exchange"] = "XSHG"
    elif re.search(r"深交所|深市|深圳证券交易所|\.SZ\b", text, re.I):
        result["exchange"] = "XSHE"
    elif re.search(r"港交所|香港交易所|\.HK\b", text, re.I):
        result["exchange"] = "XHKG"
    elif re.search(r"纳斯达克|NASDAQ|\.US\b", upper, re.I):
        result["exchange"] = "US"

    if re.search(r"ETF|交易所买卖基金", text, re.I):
        result["asset_type"] = "etf"
    elif re.search(r"指数|大盘|\bindex\b", text, re.I):
        result["asset_type"] = "index"
    elif re.search(r"股票|个股|\bequity\b|\bstock\b", text, re.I):
        result["asset_type"] = "equity"
    elif re.search(r"基金(?!会)|\bfund\b", text, re.I):
        result["asset_type"] = "fund"

    if re.search(r"(?:A类|A份额|A股类|联接A)(?:\b|$)", upper):
        result["share_class"] = "A"
    elif re.search(r"(?:C类|C份额|C股类|联接C)(?:\b|$)", upper):
        result["share_class"] = "C"

    if re.search(r"人民币|\bCNY\b|\bRMB\b", upper):
        result["currency"] = "CNY"
    elif re.search(r"港币|港元|\bHKD\b", upper):
        result["currency"] = "HKD"
    elif re.search(r"美元|\bUSD\b", upper):
        result["currency"] = "USD"
    return result


def _matches_filters(target: Mapping[str, object], filters: Mapping[str, str]) -> bool:
    exchange = filters.get("exchange") or ""
    if exchange:
        values = {str(target.get("exchange") or ""), str(target.get("market") or "")}
        if exchange == "US":
            if not ({"US", "XNAS", "XNYS", "ARCX", "INDEX"} & values):
                return False
        elif exchange not in values:
            return False
    for field in ("asset_type", "share_class", "currency"):
        expected = str(filters.get(field) or "").upper()
        actual = str(target.get(field) or "").upper()
        if expected and expected != actual:
            return False
    return True


def _critical_field(candidates: Sequence[Mapping[str, object]]) -> str:
    if not candidates:
        return "exchange"
    asset_types = {str(item.get("asset_type") or "") for item in candidates}
    share_classes = {str(item.get("share_class") or "") for item in candidates}
    currencies = {str(item.get("currency") or "") for item in candidates}
    exchanges = {str(item.get("exchange") or item.get("market") or "") for item in candidates}
    if asset_types <= {"fund", "etf"} and len(share_classes) > 1:
        return "share_class"
    if asset_types <= {"fund", "etf"} and len(currencies) > 1:
        return "currency"
    if len(asset_types) > 1:
        return "asset_type"
    if len(exchanges) > 1:
        return "exchange"
    return "instrument"


def _clarification_question(
    matched_query: str,
    field: str,
    candidates: Sequence[Mapping[str, object]],
) -> str:
    options = _echo(candidates)
    if not candidates:
        return f"“{matched_query}”缺少可确认的交易所或完整标的名称，请补充完整代码（含后缀）或名称。"
    if field == "share_class":
        return f"“{matched_query}”包含不同基金份额，您指哪一份：{options}？"
    if field == "currency":
        return f"“{matched_query}”包含不同币种份额，您指哪一份：{options}？"
    if field == "asset_type":
        return f"“{matched_query}”同时对应不同资产类型，您指哪一个：{options}？"
    if field == "exchange":
        return f"“{matched_query}”缺少唯一交易所身份，您指哪一个：{options}？"
    return f"“{matched_query}”对应多个标的，您指哪一个：{options}？"


class SharedLLMTargetRanker:
    """Constrained semantic ranker over the already-existing local broker."""

    RESPONSE_SCHEMA = {
        "type": "object",
        "required": ["selected_instrument_id", "confidence", "attribute", "evidence_text"],
        "properties": {
            "selected_instrument_id": {"type": ["integer", "null"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "attribute": {
                "enum": ["exchange", "asset_type", "share_class", "currency", "instrument_name", "none"]
            },
            "evidence_text": {"type": "string"},
        },
        "additionalProperties": False,
    }

    def __init__(self, broker):
        self.broker = broker

    def __call__(self, question: str, candidates: Sequence[Mapping[str, object]], *, request_id: str):
        result = self.broker.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "Choose only when the user explicitly states a candidate discriminator. "
                        "Never infer exchange, fund share class, currency, or a bare six-digit code. "
                        "Return null when ambiguous; evidence_text must be copied from the user question."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"question": question, "candidates": list(candidates)},
                        ensure_ascii=False,
                    ),
                },
            ],
            profile="fast",
            priority="chat_clarification",
            role_key="financial_target_ranker",
            request_id=request_id,
            response_schema=self.RESPONSE_SCHEMA,
            max_tokens=256,
            temperature=0,
            timeout_seconds=30,
        )
        if not isinstance(result.parsed, Mapping):
            raise ValueError("target ranker returned no JSON object")
        return dict(result.parsed)


class FinancialTargetResolver:
    def __init__(self, instrument_registry, *, llm_ranker=None):
        self.instruments = instrument_registry
        self.llm_ranker = llm_ranker

    @staticmethod
    def empty(status: str, *, reason: str, destination: str = "normal_chat") -> dict:
        return validate_target_resolution(
            {
                "schema_version": TARGET_RESOLUTION_SCHEMA_VERSION,
                "status": status,
                "targets": [],
                "candidate_count": 0,
                "needs_clarification": False,
                "clarification": {},
                "resolution_source": "none",
                "target_echo": "",
                "context_inherited": False,
                "resumed_from_route_key": None,
                "resume_context": {},
                "route_destination": destination,
                "llm_used": False,
                "reason_codes": [reason],
            }
        )

    def _groups(self, financial_intent: Mapping[str, object]) -> list[dict]:
        as_of = _as_of_date(financial_intent)
        queries = []
        for candidate in financial_intent.get("candidates") or []:
            query = str(candidate.get("matched_query") or "").strip()
            if query and query not in queries:
                queries.append(query)
        groups = []
        for query in queries:
            resolution = self.instruments.resolve(query, as_of=as_of)
            if resolution.status == "not_found":
                continue
            groups.append(
                {
                    "query": query,
                    "status": resolution.status,
                    "targets": [_target(item.instrument) for item in resolution.candidates],
                }
            )
        return groups

    def _rank_with_llm(
        self,
        question: str,
        candidates: Sequence[Mapping[str, object]],
        *,
        request_id: str,
    ) -> tuple[Optional[dict], bool]:
        if self.llm_ranker is None or not candidates:
            return None, False
        try:
            ranked = dict(self.llm_ranker(question, candidates, request_id=request_id))
            Draft202012Validator(SharedLLMTargetRanker.RESPONSE_SCHEMA).validate(ranked)
            selected_id = ranked.get("selected_instrument_id")
            evidence = unicodedata.normalize("NFKC", str(ranked.get("evidence_text") or "")).strip()
            attribute = str(ranked.get("attribute") or "none")
            if selected_id is None or float(ranked["confidence"]) < 0.9 or not evidence:
                return None, True
            if evidence.casefold() not in unicodedata.normalize("NFKC", question).casefold():
                return None, True
            selected = next(
                (item for item in candidates if int(item["instrument_id"]) == int(selected_id)),
                None,
            )
            if selected is None:
                return None, True
            filters = _question_filters(evidence)
            if attribute == "instrument_name":
                if str(selected["display_name"]).casefold() not in question.casefold():
                    return None, True
            elif attribute not in filters or not filters.get(attribute):
                return None, True
            elif not _matches_filters(selected, {attribute: filters[attribute]}):
                return None, True
            return dict(selected), True
        except Exception:
            return None, True

    def _resolved(
        self,
        targets: Sequence[Mapping[str, object]],
        *,
        source: str,
        reason_codes: Sequence[str],
        inherited: bool = False,
        resumed_from: Optional[str] = None,
        resume_context: Optional[Mapping[str, object]] = None,
        llm_used: bool = False,
    ) -> dict:
        unique = _dedupe_targets(targets)
        return validate_target_resolution(
            {
                "schema_version": TARGET_RESOLUTION_SCHEMA_VERSION,
                "status": "resolved",
                "targets": unique,
                "candidate_count": len(unique),
                "needs_clarification": False,
                "clarification": {},
                "resolution_source": source,
                "target_echo": _echo(unique),
                "context_inherited": inherited,
                "resumed_from_route_key": resumed_from,
                "resume_context": dict(resume_context or {}),
                "route_destination": "financial_target_resolved",
                "llm_used": bool(llm_used),
                "reason_codes": list(reason_codes),
            }
        )

    def _clarification(
        self,
        matched_query: str,
        candidates: Sequence[Mapping[str, object]],
        *,
        request_id: str,
        financial_intent: Mapping[str, object],
        llm_used: bool,
    ) -> dict:
        unique = _dedupe_targets(candidates)
        field = _critical_field(unique)
        identity = hashlib.sha256(
            json.dumps(
                {
                    "request_id": request_id,
                    "query": matched_query,
                    "ids": [item["instrument_id"] for item in unique],
                    "field": field,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:20]
        clarification = {
            "clarification_id": f"target-clarification-{identity}",
            "status": "pending",
            "field": field,
            "matched_query": matched_query,
            "question": _clarification_question(matched_query, field, unique),
            "options": unique,
            "original_route_key": request_id,
            "original_intent": str(financial_intent.get("intent") or "unknown"),
            "original_as_of": financial_intent.get("as_of"),
            "resume_route_destination": "financial_target_resolved",
        }
        return validate_target_resolution(
            {
                "schema_version": TARGET_RESOLUTION_SCHEMA_VERSION,
                "status": "clarification_required",
                "targets": [],
                "candidate_count": len(unique),
                "needs_clarification": True,
                "clarification": clarification,
                "resolution_source": "ambiguous_instrument",
                "target_echo": "",
                "context_inherited": False,
                "resumed_from_route_key": None,
                "resume_context": {},
                "route_destination": "financial_clarification",
                "llm_used": bool(llm_used),
                "reason_codes": ["material_instrument_ambiguity", f"clarify:{field}"],
            }
        )

    def _resume_pending(
        self,
        question: str,
        current_groups: Sequence[Mapping[str, object]],
        prior_state: Mapping[str, object],
    ) -> Optional[dict]:
        clarification = prior_state.get("clarification")
        if not isinstance(clarification, Mapping) or clarification.get("status") != "pending":
            return None
        options = [dict(item) for item in clarification.get("options") or [] if isinstance(item, Mapping)]
        if not options:
            return None
        option_ids = {int(item["instrument_id"]) for item in options}
        explicit = _dedupe_targets(
            [
                target
                for group in current_groups
                for target in group.get("targets") or []
                if int(target["instrument_id"]) in option_ids
            ]
        )
        selected = []
        if len(explicit) == 1:
            selected = explicit
        else:
            filters = _question_filters(question)
            active_filters = {key: value for key, value in filters.items() if value}
            if active_filters:
                selected = [item for item in options if _matches_filters(item, active_filters)]
            if len(selected) != 1:
                ordinal = re.search(r"(?:第)?([一二两1]|[二2])个", question)
                if ordinal:
                    index = 0 if ordinal.group(1) in {"一", "1"} else 1
                    if index < len(options):
                        selected = [options[index]]
        if len(selected) != 1:
            return None
        route_key = str(prior_state.get("route_key") or clarification.get("original_route_key") or "")
        resume_context = {
            "original_route_key": route_key,
            "original_question": str(prior_state.get("raw_question") or ""),
            "original_intent": str(clarification.get("original_intent") or prior_state.get("intent") or "unknown"),
            "original_as_of": clarification.get("original_as_of"),
        }
        return self._resolved(
            selected,
            source="clarification_answer",
            reason_codes=["pending_clarification_resolved", "same_session_context"],
            inherited=True,
            resumed_from=route_key or None,
            resume_context=resume_context,
        )

    def resolve(
        self,
        question: str,
        financial_intent: Mapping[str, object],
        *,
        prior_state: Optional[Mapping[str, object]] = None,
        request_id: str = "",
    ) -> dict:
        text = unicodedata.normalize("NFKC", str(question or "")).strip()
        groups = self._groups(financial_intent)
        prior = dict(prior_state or {})

        if prior.get("route_status") == "clarification_required":
            resumed = self._resume_pending(text, groups, prior)
            if resumed is not None:
                return resumed

        is_financial = bool(financial_intent.get("is_financial"))
        if not is_financial:
            if (
                prior.get("route_status") in _CONFIRMED_TARGET_ROUTE_STATUSES
                and _CONTEXT_REFERENCE.search(text)
            ):
                previous = [
                    dict(item)
                    for item in prior.get("resolved_targets") or []
                    if isinstance(item, Mapping)
                ]
                if previous:
                    return self._resolved(
                        previous,
                        source="same_session_confirmed_target",
                        reason_codes=["context_reference", "same_session_context"],
                        inherited=True,
                        resumed_from=str(prior.get("route_key") or "") or None,
                        resume_context={
                            "original_route_key": str(prior.get("route_key") or ""),
                            "original_question": str(prior.get("raw_question") or ""),
                            "original_intent": str(prior.get("intent") or "unknown"),
                        },
                    )
            return self.empty("skipped", reason="not_financial")

        # A broad universe is not a single instrument and belongs to task 3.5.
        if financial_intent.get("universe") and not groups:
            return self.empty(
                "no_target",
                reason="universe_scope_has_no_single_target",
                destination="financial_scope_pending",
            )

        if not groups:
            if _BARE_SIX_DIGIT.search(text):
                return self._clarification(
                    _BARE_SIX_DIGIT.search(text).group(0),
                    [],
                    request_id=request_id,
                    financial_intent=financial_intent,
                    llm_used=False,
                )
            if (
                prior.get("route_status") in _CONFIRMED_TARGET_ROUTE_STATUSES
                and _CONTEXT_REFERENCE.search(text)
                and not financial_intent.get("universe")
            ):
                previous = [dict(item) for item in prior.get("resolved_targets") or []]
                if previous:
                    return self._resolved(
                        previous,
                        source="same_session_confirmed_target",
                        reason_codes=["context_reference", "same_session_context"],
                        inherited=True,
                        resumed_from=str(prior.get("route_key") or "") or None,
                    )
            return self.empty(
                "no_target",
                reason="no_instrument_mention",
                destination="financial_scope_pending",
            )

        filters = _question_filters(text)
        active_filters = {key: value for key, value in filters.items() if value}
        explicitly_resolved_ids = {
            int(group["targets"][0]["instrument_id"])
            for group in groups
            if group["status"] == "resolved" and len(group["targets"]) == 1
            and not re.fullmatch(r"\d{6}", str(group["query"]))
        }
        selected = []
        pending = None
        llm_used = False
        for group in groups:
            candidates = list(group["targets"])
            bare_code_group = bool(re.fullmatch(r"\d{6}", str(group["query"])))
            corroborated = [
                item for item in candidates if int(item["instrument_id"]) in explicitly_resolved_ids
            ]
            filtered = [item for item in candidates if _matches_filters(item, active_filters)]
            if len(corroborated) == 1:
                selected.extend(corroborated)
                continue
            if active_filters and len(filtered) == 1:
                selected.extend(filtered)
                continue
            if group["status"] == "resolved" and len(candidates) == 1 and not bare_code_group:
                selected.extend(candidates)
                continue
            if group["status"] == "resolved" and len(candidates) == 1 and bare_code_group:
                # A six-digit code is accepted only when another explicit name
                # or an exchange/asset discriminator confirms the identity.
                if active_filters:
                    selected.extend(candidates)
                    continue
                pending = (str(group["query"]), candidates)
                break

            strict_fund_ambiguity = _critical_field(candidates) in {"share_class", "currency"}
            bare_in_question = bool(_BARE_SIX_DIGIT.search(text))
            ranked = None
            used = False
            if not strict_fund_ambiguity and not bare_in_question:
                ranked, used = self._rank_with_llm(
                    text, candidates, request_id=request_id
                )
                llm_used = llm_used or used
            if ranked is not None:
                selected.append(ranked)
                continue
            pending = (str(group["query"]), candidates)
            break

        if pending is not None:
            return self._clarification(
                pending[0],
                pending[1],
                request_id=request_id,
                financial_intent=financial_intent,
                llm_used=llm_used,
            )
        if selected:
            result = self._resolved(
                selected,
                source="explicit_alias_or_filter",
                reason_codes=["stable_instrument_identity", "explicit_user_scope"],
                llm_used=llm_used,
            )
            pending_prior = prior.get("route_status") == "clarification_required"
            option_ids = {
                int(item["instrument_id"])
                for item in (prior.get("clarification") or {}).get("options") or []
            }
            selected_ids = {int(item["instrument_id"]) for item in result["targets"]}
            if pending_prior and selected_ids and selected_ids <= option_ids:
                route_key = str(prior.get("route_key") or "")
                return self._resolved(
                    result["targets"],
                    source="clarification_answer",
                    reason_codes=["pending_clarification_resolved", "same_session_context"],
                    inherited=True,
                    resumed_from=route_key or None,
                    resume_context={
                        "original_route_key": route_key,
                        "original_question": str(prior.get("raw_question") or ""),
                        "original_intent": str(prior.get("intent") or "unknown"),
                    },
                    llm_used=llm_used,
                )
            return result
        return self.empty("degraded", reason="target_resolution_failed_closed")


__all__ = [
    "FinancialTargetResolver",
    "SharedLLMTargetRanker",
    "TARGET_RESOLUTION_SCHEMA",
    "TARGET_RESOLUTION_SCHEMA_VERSION",
    "validate_target_resolution",
]

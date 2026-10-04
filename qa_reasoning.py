#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic claim-evidence graph, authority scoring and conflict adjudication."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Mapping
from urllib.parse import urlsplit


ADJUDICATION_VERSION = "qa-adjudication-v1"
_NEGATION_RE = re.compile(r"不适用|不包括|不得|没有|并非|无须|无需|禁止|未生效|not\s|no\s", re.I)
_NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s*(?:%|亿元|万元|元|天|年|个月|人|项)?")
_OFFICIAL_DOMAINS = {
    "mof.gov.cn", "chinatax.gov.cn", "gov.hk", "ird.gov.hk", "hkma.gov.hk",
    "mas.gov.sg", "nfra.gov.cn", "gov.cn", "legco.gov.hk",
}


def _normalized(text: str) -> str:
    value = re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", str(text or "").casefold())
    for word in ("根据", "表示", "认为", "指出", "公告", "文章", "相关", "目前", "其中"):
        value = value.replace(word, "")
    return value


def _shingles(text: str) -> set[str]:
    value = _normalized(text)
    if len(value) < 2:
        return {value} if value else set()
    return {value[index:index + 2] for index in range(len(value) - 1)}


def _similarity(left: str, right: str) -> float:
    a, b = _shingles(left), _shingles(right)
    return len(a & b) / max(1, len(a | b))


def _date(value) -> datetime | None:
    raw = str(value or "").strip()[:10]
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def authority_score(evidence: Mapping, *, now: datetime | None = None) -> tuple[int, list[str]]:
    now = now or datetime.now(timezone.utc)
    level = int(evidence.get("authority_level") or 1)
    flags = []
    metadata = evidence.get("metadata") if isinstance(evidence.get("metadata"), Mapping) else {}
    doc_type = str(evidence.get("doc_type") or metadata.get("doc_type") or "")
    source_role = str(evidence.get("source_role") or metadata.get("source_role") or "")
    domain = str(urlsplit(str(evidence.get("source_url") or "")).hostname or "").casefold()
    if doc_type == "official_policy" or source_role == "official_original":
        level = max(level, 100)
        flags.append("official_original")
    elif doc_type == "official_interpretation" or source_role == "official_reference":
        level = max(level, 90)
        flags.append("official_reference")
    elif doc_type == "professional_commentary":
        level = max(level, 50)
    elif doc_type == "ai_qa_summary":
        level = min(level, 10)
    elif domain in _OFFICIAL_DOMAINS or any(domain.endswith("." + item) for item in _OFFICIAL_DOMAINS):
        level = max(level, 100)
        flags.append("official_original")
    elif evidence.get("source_type") == "official":
        level = max(level, 100)
        flags.append("official_original")
    elif evidence.get("source_type") == "ragflow_chunk":
        level = max(level, 20)
    published = _date(evidence.get("published_at"))
    if published and published > now:
        flags.append("future_publication_date_ignored")
        level = max(1, level - 1)
    if not published:
        flags.append("publication_date_missing")
    # fetched_at is deliberately never used as legal/policy effective time.
    return max(1, level), list(dict.fromkeys(flags + [str(flag) for flag in metadata.get("authority_flags") or [] if str(flag)]))


def dedupe_evidence(items: list[Mapping]) -> tuple[list[dict], dict[str, str]]:
    kept, aliases = [], {}
    identities = {}
    for raw in items:
        item = dict(raw)
        ref = str(item.get("evidence_ref") or "")
        content_hash = hashlib.sha256(str(item.get("content_excerpt") or "").encode("utf-8")).hexdigest()
        identity = (
            str(item.get("document_id") or ""),
            str(item.get("source_url") or ""),
            content_hash,
        )
        # Prefer exact document/url identity; content fingerprint catches mirrors.
        keys = [identity, ("", "", content_hash)]
        existing = next((identities[key] for key in keys if key in identities), None)
        if existing:
            aliases[ref] = existing
            continue
        level, flags = authority_score(item)
        item["authority_level"] = level
        item["metadata"] = {**dict(item.get("metadata") or {}), "authority_flags": flags}
        kept.append(item)
        for key in keys:
            identities[key] = ref
        aliases[ref] = ref
    return kept, aliases


def _canonical_claims(level1: Mapping, level2: Mapping, aliases: Mapping[str, str]) -> list[dict]:
    inputs = []
    for stage, field in (
        ("level1", "claims"),
        ("level2_confirmed", "confirmed_claims"),
        ("level2_corrected", "corrected_claims"),
        ("level2_new", "new_findings"),
    ):
        for claim in level1.get(field, []) if stage == "level1" else level2.get(field, []):
            value = dict(claim)
            value["evidence_refs"] = list(dict.fromkeys(aliases.get(str(ref), str(ref)) for ref in value.get("evidence_refs") or []))
            inputs.append((stage, value))
    nodes = []
    for stage, claim in inputs:
        best = None
        for node in nodes:
            similarity = _similarity(node["claim"]["text"], claim.get("text"))
            scopes_a = set(node["claim"].get("scope") or [])
            scopes_b = set(claim.get("scope") or [])
            scope_compatible = not scopes_a or not scopes_b or bool(scopes_a & scopes_b)
            time_a, time_b = str(node["claim"].get("valid_from") or ""), str(claim.get("valid_from") or "")
            time_compatible = not time_a or not time_b or time_a == time_b
            if similarity >= 0.72 and scope_compatible and time_compatible and bool(_NEGATION_RE.search(node["claim"]["text"])) == bool(_NEGATION_RE.search(str(claim.get("text") or ""))):
                best = node
                break
        if best is None:
            nodes.append({
                "canonical_id": str(claim.get("claim_id") or f"claim-{len(nodes)+1}"),
                "claim": claim,
                "variants": [{"stage": stage, "claim_id": claim.get("claim_id"), "text": claim.get("text")}],
                "stages": [stage],
            })
            continue
        best["variants"].append({"stage": stage, "claim_id": claim.get("claim_id"), "text": claim.get("text")})
        if stage not in best["stages"]:
            best["stages"].append(stage)
        merged_refs = list(dict.fromkeys((best["claim"].get("evidence_refs") or []) + (claim.get("evidence_refs") or [])))
        # L2 variants supersede L1 confidence/status but never replace text with
        # a lower confidence variant.
        if stage.startswith("level2") and float(claim.get("confidence") or 0) >= float(best["claim"].get("confidence") or 0):
            best["claim"] = claim
            best["claim"]["claim_id"] = best["canonical_id"]
        best["claim"]["evidence_refs"] = merged_refs
    for node in nodes:
        node["claim"]["claim_id"] = node["canonical_id"]
    return nodes


def _claim_authority(claim: Mapping, evidence_by_ref: Mapping[str, Mapping]) -> int:
    return max((int(evidence_by_ref.get(str(ref), {}).get("authority_level") or 0) for ref in claim.get("evidence_refs") or []), default=0)


def _conflict_type(left: Mapping, right: Mapping) -> str | None:
    similarity = _similarity(left.get("text"), right.get("text"))
    if similarity < 0.28:
        return None
    scopes_left, scopes_right = set(left.get("scope") or []), set(right.get("scope") or [])
    if scopes_left and scopes_right and not (scopes_left & scopes_right):
        return "scope_difference"
    negated = bool(_NEGATION_RE.search(str(left.get("text") or ""))), bool(_NEGATION_RE.search(str(right.get("text") or "")))
    if negated[0] != negated[1]:
        return "real_conflict"
    numbers_left, numbers_right = set(_NUMBER_RE.findall(str(left.get("text") or ""))), set(_NUMBER_RE.findall(str(right.get("text") or "")))
    if numbers_left and numbers_right and numbers_left != numbers_right:
        return "method_difference"
    dates = (_date(left.get("valid_from")), _date(right.get("valid_from")))
    if all(dates) and dates[0] != dates[1] and similarity >= 0.55:
        return "time_change"
    # Different professional summaries are not user-facing conflicts unless
    # they contain a real contradiction above.  Keep them as parallel evidence.
    return None


def _adjudicate(kind: str, left: Mapping, right: Mapping, evidence_by_ref: Mapping[str, Mapping]) -> tuple[str, str]:
    if kind == "scope_difference":
        return "resolved", "两项说法适用范围不同，分别保留并明确限定范围。"
    if kind == "time_change":
        left_date, right_date = _date(left.get("valid_from")), _date(right.get("valid_from"))
        if left_date and right_date:
            newer = left if left_date > right_date else right
            return "resolved", f"按规则生效时间区分，新版本主张 {newer.get('claim_id')} 优先用于当前结论，旧版本保留在时间线。"
    if kind == "opinion_difference":
        return "unresolved", "专业文章之间存在解读角度差异，不能直接判定谁对谁错；需要回到官方原文或后续官方口径核验。"
    left_auth, right_auth = _claim_authority(left, evidence_by_ref), _claim_authority(right, evidence_by_ref)
    if abs(left_auth - right_auth) >= 2:
        return "resolved", "一边来自更权威的来源，另一边只是解读或背景材料；回答时以前者为准，后者只作为参考，不单独作为结论依据。"
    if kind == "method_difference":
        return "unresolved", "不同资料里的数字、税率、期限或计算口径不一致，不能取平均值；需要以官方原文或最新官方口径为准。"
    return "unresolved", "不同资料的说法还不能互相印证，回答时应优先采用官方原文；解读材料只作为参考，不当作确定结论。"


def build_claim_evidence_graph(level1: Mapping, level2: Mapping | None = None) -> dict:
    level2 = dict(level2 or {})
    evidence, aliases = dedupe_evidence(list(level1.get("evidence") or []) + list(level2.get("evidence") or []))
    evidence_by_ref = {str(item["evidence_ref"]): item for item in evidence}
    nodes = _canonical_claims(level1, level2, aliases)
    edges = []
    for node in nodes:
        claim = node["claim"]
        authority = _claim_authority(claim, evidence_by_ref)
        node["authority_level"] = authority
        for ref in claim.get("evidence_refs") or []:
            evidence_item = evidence_by_ref.get(str(ref))
            if not evidence_item:
                continue
            relation = str(evidence_item.get("relationship") or "supports")
            edges.append({
                "claim_id": node["canonical_id"], "evidence_ref": str(ref),
                "relationship": relation, "relevance_score": float(evidence_item.get("score") or claim.get("confidence") or 0),
                "published_at": evidence_item.get("published_at"), "scope": list(claim.get("scope") or []),
            })

    conflicts = []
    for index, left_node in enumerate(nodes):
        for right_node in nodes[index + 1:]:
            left, right = left_node["claim"], right_node["claim"]
            kind = _conflict_type(left, right)
            if not kind:
                continue
            resolution, rationale = _adjudicate(kind, left, right, evidence_by_ref)
            refs = list(dict.fromkeys((left.get("evidence_refs") or []) + (right.get("evidence_refs") or [])))
            conflict_id = "conflict:" + _hash_pair(left_node["canonical_id"], right_node["canonical_id"])
            conflicts.append({
                "conflict_id": conflict_id,
                "subject": f"{left.get('text')} / {right.get('text')}",
                "conflict_type": kind,
                "claim_ids": [left_node["canonical_id"], right_node["canonical_id"]],
                "evidence_refs": refs,
                "resolution": resolution,
                "rationale": rationale,
                "rule_version": ADJUDICATION_VERSION,
            })
    # Keep explicit L2 conflicts too; deterministic conflicts take precedence on id.
    existing = {item["conflict_id"] for item in conflicts}
    for item in level2.get("conflicts") or []:
        if item.get("conflict_id") not in existing:
            value = dict(item)
            value["evidence_refs"] = list(dict.fromkeys(aliases.get(str(ref), str(ref)) for ref in value.get("evidence_refs") or []))
            conflicts.append(value)
    return {
        "version": ADJUDICATION_VERSION,
        "claims": nodes,
        "evidence": evidence,
        "edges": edges,
        "conflicts": conflicts,
        "stats": {
            "claim_variants": sum(len(node["variants"]) for node in nodes),
            "canonical_claims": len(nodes), "evidence": len(evidence),
            "edges": len(edges), "conflicts": len(conflicts),
            "unresolved_conflicts": sum(item["resolution"] == "unresolved" for item in conflicts),
        },
    }


def _hash_pair(left: str, right: str) -> str:
    return hashlib.sha256("|".join(sorted((str(left), str(right)))).encode("utf-8")).hexdigest()[:20]


__all__ = ["ADJUDICATION_VERSION", "authority_score", "build_claim_evidence_graph", "dedupe_evidence"]

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Versioned human adjudication and explicit RAG publication gate."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Mapping, Sequence


FINANCIAL_ADJUDICATION_VERSION = "financial-conflict-adjudication-v1"
FINANCIAL_DECISIONS = {
    "keep_newer_verified",
    "keep_historical",
    "both_opinions",
    "reject_all",
}
LEGACY_DECISIONS = {"keep_a", "keep_b", "keep_both_pending"}
ALL_DECISIONS = FINANCIAL_DECISIONS | LEGACY_DECISIONS


def _mapping(value: object) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _array(value: object) -> list:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def conflict_payload_sha256(conflict: Mapping[str, object]) -> str:
    payload = {
        key: conflict.get(key)
        for key in (
            "key", "conflict_type", "source_a", "source_b", "claim_a", "claim_b",
            "evidence", "instrument", "as_of", "verdict", "reason",
        )
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def conflict_report_versions(conflict: Mapping[str, object]) -> list[dict]:
    refs = {}
    evidence = _mapping(conflict.get("evidence"))
    for side in ("a", "b"):
        for item in evidence.get(side) or []:
            if not isinstance(item, Mapping) or item.get("report_id") is None:
                continue
            try:
                report_id = int(item.get("report_id"))
                version = int(item.get("report_version"))
            except (TypeError, ValueError):
                continue
            if report_id <= 0 or version <= 0:
                continue
            run_id = str(item.get("research_run_id") or "")
            refs[(report_id, version)] = {
                "report_id": report_id,
                "report_version": version,
                "research_run_id": run_id,
            }
    return [refs[key] for key in sorted(refs)]


def _parse_time(value: object):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        result = datetime.fromisoformat(text)
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except ValueError:
        return None


def validate_financial_decision(conflict: Mapping[str, object], decision: str) -> None:
    if decision not in ALL_DECISIONS:
        raise ValueError("无效裁决")
    kind = str(conflict.get("conflict_type") or "")
    verdict = str(conflict.get("verdict") or "")
    if decision in LEGACY_DECISIONS:
        if kind and decision != "keep_both_pending":
            raise ValueError("金融冲突必须使用带有时点/观点语义的裁决")
        return
    if decision == "both_opinions" and kind != "opinion_difference":
        raise ValueError("只有观点差异可选择“两种观点并存”")
    if decision in {"keep_newer_verified", "keep_historical"}:
        as_of = _mapping(conflict.get("as_of"))
        left, right = _parse_time(as_of.get("a")), _parse_time(as_of.get("b"))
        if left is None or right is None or left == right:
            raise ValueError("双方没有可区分的有效时点")
        if decision == "keep_newer_verified" and verdict in {"pending_evidence", "conflicted"}:
            raise ValueError("待补证或同时点冲突不能标为“保留较新已核验”")


def _latest_report_version(connection, ref: Mapping[str, object]) -> int:
    run_id = str(ref.get("research_run_id") or "")
    if not run_id:
        row = connection.execute(
            "SELECT research_run_id FROM financial_final_reports WHERE id=?",
            (int(ref.get("report_id") or 0),),
        ).fetchone()
        run_id = str(row[0]) if row else ""
    if not run_id:
        return 0
    row = connection.execute(
        "SELECT COALESCE(MAX(report_version), 0) FROM financial_final_reports WHERE research_run_id=?",
        (run_id,),
    ).fetchone()
    return int(row[0] if row else 0)


def review_decision_versions(
    connection,
    conflicts: Sequence[Mapping[str, object]],
    decisions: Sequence[Mapping[str, object]],
) -> dict:
    by_key = {str(item.get("key") or ""): item for item in conflicts if isinstance(item, Mapping)}
    reviewed = []
    for decision in decisions:
        item = dict(decision)
        key = str(item.get("conflict_key") or "")
        conflict = by_key.get(key)
        stale_reasons = []
        if conflict is None:
            stale_reasons.append("conflict_removed_or_replaced")
        elif str(item.get("conflict_payload_sha256") or "") != conflict_payload_sha256(conflict):
            stale_reasons.append("conflict_payload_changed")
        stored_refs = _array(item.get("report_versions_json"))
        for ref in stored_refs:
            if not isinstance(ref, Mapping):
                continue
            latest = _latest_report_version(connection, ref)
            if latest > int(ref.get("report_version") or 0):
                stale_reasons.append(
                    f"report_updated:{int(ref.get('report_id') or 0)}:v{int(ref.get('report_version') or 0)}->v{latest}"
                )
        item["report_versions"] = stored_refs
        item["is_stale"] = bool(stale_reasons)
        item["stale_reasons"] = stale_reasons
        reviewed.append(item)
    current_by_key = {str(item.get("conflict_key") or ""): item for item in reviewed}
    missing = sorted(key for key in by_key if key not in current_by_key)
    stale = sorted(key for key, item in current_by_key.items() if item.get("is_stale"))
    pending = sorted(
        key
        for key, item in current_by_key.items()
        if str(item.get("decision") or "") == "keep_both_pending"
    )
    ready = bool(by_key) and not missing and not stale and not pending
    return {
        "schema_version": FINANCIAL_ADJUDICATION_VERSION,
        "decisions": reviewed,
        "missing_conflict_keys": missing,
        "stale_conflict_keys": stale,
        "pending_conflict_keys": pending,
        "ready_for_explicit_kb_confirmation": ready,
    }


def _selected_side(conflict: Mapping[str, object], *, newest: bool) -> str:
    as_of = _mapping(conflict.get("as_of"))
    left, right = _parse_time(as_of.get("a")), _parse_time(as_of.get("b"))
    if left is None or right is None or left == right:
        raise ValueError("无法根据时点选择裁决内容")
    if newest:
        return "a" if left > right else "b"
    return "a" if left < right else "b"


def _publication_pair(conflict: Mapping[str, object], decision: str) -> dict | None:
    if decision in {"reject_all", "keep_both_pending"}:
        return None
    instrument = _mapping(conflict.get("instrument"))
    label = " / ".join(
        value for value in (
            str(instrument.get("display_name") or ""),
            str(instrument.get("canonical_symbol") or ""),
        ) if value
    ) or "未绑定标的"
    kind = str(conflict.get("conflict_type") or "")
    as_of = _mapping(conflict.get("as_of"))
    if decision == "both_opinions":
        answer = (
            f"裁决：两种研究观点并存（不是已核验事实）。\n"
            f"观点 A：{conflict.get('claim_a') or ''}\n"
            f"时点 A：{as_of.get('a') or '未知'}\n"
            f"证据 A：{conflict.get('evidence_a') or '未提供'}\n"
            f"观点 B：{conflict.get('claim_b') or ''}\n"
            f"时点 B：{as_of.get('b') or '未知'}\n"
            f"证据 B：{conflict.get('evidence_b') or '未提供'}"
        )
    else:
        if decision == "keep_newer_verified":
            side = _selected_side(conflict, newest=True)
            prefix = "保留较新已核验内容"
        elif decision == "keep_historical":
            side = _selected_side(conflict, newest=False)
            prefix = "保留历史观点（不代表当前状态）"
        elif decision == "keep_a":
            side, prefix = "a", "人工选择 A"
        elif decision == "keep_b":
            side, prefix = "b", "人工选择 B"
        else:
            return None
        is_opinion = kind == "opinion_difference"
        type_note = "TradingAgents 研究观点，不是已核验事实" if is_opinion else "核验内容"
        answer = (
            f"裁决：{prefix}。\n"
            f"类型：{type_note}。\n"
            f"内容：{conflict.get(f'claim_{side}') or ''}\n"
            f"时点：{as_of.get(side) or '未知'}\n"
            f"证据：{conflict.get(f'evidence_{side}') or '未提供'}"
        )
    return {
        "q": f"金融冲突裁决：{label}（{kind or '待审阅'}）",
        "a": answer,
    }


def prepare_kb_publication(
    connection,
    operation: Mapping[str, object],
    decisions: Sequence[Mapping[str, object]],
    confirmation: Mapping[str, object],
) -> dict:
    result = _mapping(operation.get("result"))
    financial_review = _mapping(result.get("financial_review"))
    conflicts = [item for item in result.get("conflicts") or [] if isinstance(item, Mapping)]
    if not financial_review or not conflicts:
        return {"financial_review": False, "pairs": []}
    review = review_decision_versions(connection, conflicts, decisions)
    if not confirmation.get("confirmed"):
        raise PermissionError("金融裁决内容需要单独确认后才能写入知识库")
    if str(confirmation.get("operation_id") or "") != str(operation.get("operation_id") or ""):
        raise PermissionError("知识库确认与裁决操作不匹配")
    if not review["ready_for_explicit_kb_confirmation"]:
        raise ValueError("裁决不完整、尚未决定或已因新报告失效，不允许写入知识库")
    expected_versions = {
        str(item.get("conflict_key") or ""): int(item.get("decision_version") or 0)
        for item in review["decisions"]
    }
    try:
        confirmed_versions = {
            str(key): int(value or 0)
            for key, value in _mapping(confirmation.get("decision_versions")).items()
        }
    except (TypeError, ValueError) as exc:
        raise PermissionError("知识库确认的裁决版本无效") from exc
    if confirmed_versions != expected_versions:
        raise PermissionError("知识库确认的裁决版本已变更，请重新审阅并确认")
    conflicts_by_key = {str(item.get("key") or ""): item for item in conflicts}
    pairs = []
    for decision in review["decisions"]:
        conflict = conflicts_by_key[str(decision.get("conflict_key") or "")]
        pair = _publication_pair(conflict, str(decision.get("decision") or ""))
        if pair:
            pairs.append(pair)
    if not pairs:
        raise ValueError("所有冲突均被拒绝或保持待定，没有可写入知识库的内容")
    instrument_keys = set()
    canonical_symbols = set()
    display_names = set()
    as_of_values = []
    for conflict in conflicts:
        instrument = _mapping(conflict.get("instrument"))
        instrument_id = int(instrument.get("instrument_id") or 0)
        row = connection.execute(
            """
            SELECT canonical_symbol, display_name, asset_type, exchange,
                   country_code, market
            FROM financial_instruments WHERE id=?
            """,
            (instrument_id,),
        ).fetchone() if instrument_id else None
        if row is not None:
            from financial_instruments import stable_instrument_key

            try:
                instrument_keys.add(
                    stable_instrument_key(
                        canonical_symbol=str(row[0] or ""),
                        asset_type=str(row[2] or ""),
                        exchange=str(row[3] or ""),
                        country_code=str(row[4] or ""),
                        market=str(row[5] or ""),
                    )
                )
            except ValueError:
                pass
            if row[0]:
                canonical_symbols.add(str(row[0]))
            if row[1]:
                display_names.add(str(row[1]))
        for value in _mapping(conflict.get("as_of")).values():
            parsed = _parse_time(value)
            if parsed is not None:
                as_of_values.append(parsed)
    operation_id = str(operation.get("operation_id") or "")
    as_of = max(as_of_values) if as_of_values else datetime.now(timezone.utc)
    public_text = "\n\n".join(
        f"{item['q']}\n{item['a']}" for item in pairs
    )
    return {
        "financial_review": True,
        "pairs": pairs,
        "operation_id": operation_id,
        "decision_versions": expected_versions,
        "rag_metadata": {
            "content_kind": "human_adjudication",
            "instrument_key": sorted(instrument_keys)[0] if instrument_keys else "",
            "instrument_keys": sorted(instrument_keys),
            "universe_key": "" if instrument_keys else f"adjudication:{operation_id}",
            "canonical_symbol": ", ".join(sorted(canonical_symbols)),
            "display_name": ", ".join(sorted(display_names)),
            "as_of": as_of.isoformat().replace("+00:00", "Z"),
            "verdict": "explicit_human_publication",
            "temporal_status": "human_adjudicated_research",
            "operation_id": operation_id,
            "explicit_confirmation": True,
            "public_text": public_text,
        },
        "boundaries": {
            "explicit_confirmation": True,
            "raw_review_answer_published": False,
            "unverified_rating_published_as_fact": False,
            "source_evidence_mutated": False,
            "ragflow_is_candidate_discovery_only": True,
        },
    }


__all__ = [
    "ALL_DECISIONS",
    "FINANCIAL_ADJUDICATION_VERSION",
    "conflict_payload_sha256",
    "conflict_report_versions",
    "prepare_kb_publication",
    "review_decision_versions",
    "validate_financial_decision",
]

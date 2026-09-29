#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Ground financial × synthesis conflicts in snapshots and saved reports."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations
from typing import Mapping, Sequence

from financial_gcd_review import review_financial_history


FINANCIAL_SYNTHESIS_REVIEW_VERSION = "financial-synthesis-review-v1"
COMPARISON_MARKERS = ("比较", "对比", "对照", "哪个", "孰优", " vs ", " versus ")


def _mapping(value: object) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _array(value: object) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def _parse_json(value: object) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _utc(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except ValueError:
        return str(value or "")[:80]


def _target_map(row: Mapping[str, object]) -> dict[int, dict]:
    audit = _mapping(row.get("financial_audit"))
    result = {}
    for target in audit.get("targets") or []:
        if not isinstance(target, Mapping):
            continue
        try:
            instrument_id = int(target.get("instrument_id"))
        except (TypeError, ValueError):
            continue
        if instrument_id > 0:
            result[instrument_id] = {
                key: target.get(key)
                for key in (
                    "instrument_id", "canonical_symbol", "display_name", "asset_type",
                    "market", "exchange", "currency", "share_class",
                )
            }
    return result


def _route(row: Mapping[str, object]) -> dict:
    return _mapping(_mapping(row.get("financial_audit")).get("route"))


def _report_refs(row: Mapping[str, object]) -> list[dict]:
    result = []
    for artifact in _mapping(row.get("financial_audit")).get("artifacts") or []:
        if not isinstance(artifact, Mapping) or artifact.get("artifact_type") != "final_report":
            continue
        payload = _mapping(artifact.get("payload"))
        try:
            report_id = int(payload.get("report_id"))
            version = int(payload.get("report_version"))
        except (TypeError, ValueError):
            continue
        if report_id > 0 and version > 0:
            result.append({"report_id": report_id, "report_version": version})
    return result


def _source_id(row: Mapping[str, object]) -> int:
    try:
        return int(row.get("source_chat_history_id") or row.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _load_opinions(connection, rows: Sequence[Mapping[str, object]]) -> list[dict]:
    requested = defaultdict(lambda: {"versions": set(), "history_ids": set(), "sessions": set(), "targets": {}})
    for row in rows:
        targets = _target_map(row)
        for ref in _report_refs(row):
            item = requested[int(ref["report_id"])]
            item["versions"].add(int(ref["report_version"]))
            source_id = _source_id(row)
            if source_id > 0:
                item["history_ids"].add(source_id)
            session = str(row.get("source_session_id") or "").strip()
            if session:
                item["sessions"].add(session)
            item["targets"].update(targets)
    if not requested:
        return []
    ids = sorted(requested)
    placeholders = ",".join("?" for _ in ids)
    db_rows = connection.execute(
        f"""
        SELECT report.id, report.research_run_id, report.report_version,
               report.report_status, report.recommendation, report.confidence,
               report.title, report.executive_summary, report.report_json,
               report.observed_at, report.fetched_at, report.verified_at,
               run.instrument_id, instrument.canonical_symbol,
               instrument.display_name, instrument.asset_type,
               instrument.market, instrument.currency
        FROM financial_final_reports report
        JOIN financial_research_runs run ON run.id=report.research_run_id
        LEFT JOIN financial_instruments instrument ON instrument.id=run.instrument_id
        WHERE report.id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    opinions = []
    for row in db_rows:
        report_id = int(row[0])
        source = requested[report_id]
        version = int(row[2])
        instrument_id = int(row[12] or 0)
        if version not in source["versions"]:
            continue
        if source["targets"] and instrument_id not in source["targets"]:
            continue
        report_json = _parse_json(row[8])
        as_of = _utc(report_json.get("as_of") or row[9] or row[10] or row[11])
        snapshot_ids = []
        for value in report_json.get("snapshot_ids") or []:
            try:
                snapshot_id = int(value)
            except (TypeError, ValueError):
                continue
            if snapshot_id > 0:
                snapshot_ids.append(snapshot_id)
        opinions.append(
            {
                "report_id": report_id,
                "report_version": version,
                "research_run_id": str(row[1]),
                "report_status": str(row[3] or ""),
                "recommendation": str(row[4] or "insufficient_evidence"),
                "confidence": float(row[5]) if row[5] is not None else None,
                "title": str(row[6] or "")[:500],
                "executive_summary": str(row[7] or "")[:1200],
                "instrument": {
                    "instrument_id": instrument_id,
                    "canonical_symbol": str(row[13] or ""),
                    "display_name": str(row[14] or row[13] or ""),
                    "asset_type": str(row[15] or ""),
                    "market": str(row[16] or ""),
                    "currency": str(row[17] or ""),
                },
                "as_of": as_of,
                "observed_at": _utc(row[9]),
                "verified_at": _utc(row[11]),
                "evidence_coverage": report_json.get("evidence_coverage"),
                "market_status": str(report_json.get("market_status") or ""),
                "snapshot_ids": sorted(set(snapshot_ids)),
                "source_chat_history_ids": sorted(source["history_ids"]),
                "source_session_ids": sorted(source["sessions"]),
            }
        )
    latest = {}
    for opinion in sorted(opinions, key=lambda item: (str(item["as_of"]), item["report_id"])):
        latest[int(opinion["instrument"]["instrument_id"])] = opinion
    for opinion in opinions:
        current = latest.get(int(opinion["instrument"]["instrument_id"]))
        opinion["temporal_status"] = (
            "current_opinion"
            if current and current["report_id"] == opinion["report_id"]
            else "historical_opinion"
        )
    return sorted(opinions, key=lambda item: (item["instrument"]["instrument_id"], item["as_of"], item["report_id"]))


def _evidence_text(opinion: Mapping[str, object]) -> str:
    coverage = opinion.get("evidence_coverage")
    coverage_text = ""
    if isinstance(coverage, (int, float)):
        coverage_text = f"；证据覆盖 {float(coverage):.1%}"
    return (
        f"report#{int(opinion['report_id'])}/v{int(opinion['report_version'])}"
        f"；{opinion.get('report_status') or 'unknown'}"
        f"；as_of {opinion.get('as_of') or '未知'}{coverage_text}"
    )


def _structured_conflict(
    *, key: str, conflict_type: str, claim_a: str, claim_b: str,
    evidence_a: str, evidence_b: str, evidence: Mapping[str, object],
    instrument: Mapping[str, object], as_of: Mapping[str, object],
    verdict: str, reason: str, source_a: str = "", source_b: str = "",
) -> dict:
    return {
        "key": re.sub(r"[^a-z0-9_-]", "", key.casefold())[:48],
        "conflict_type": conflict_type,
        "source_a": source_a,
        "source_b": source_b,
        "claim_a": str(claim_a)[:1200],
        "evidence_a": str(evidence_a)[:1600],
        "claim_b": str(claim_b)[:1200],
        "evidence_b": str(evidence_b)[:1600],
        "status": "待人工裁决" if verdict != "pending_evidence" else "待补充证据",
        "claim": {"a": str(claim_a)[:1200], "b": str(claim_b)[:1200]},
        "evidence": dict(evidence),
        "instrument": dict(instrument),
        "as_of": dict(as_of),
        "verdict": verdict,
        "reason": str(reason)[:1800],
    }


def _fact_conflicts(fact_review: Mapping[str, object]) -> list[dict]:
    result = []
    for claim in fact_review.get("claims") or []:
        if not isinstance(claim, Mapping) or claim.get("status") != "conflicted":
            continue
        by_value = defaultdict(list)
        for evidence in claim.get("evidence") or []:
            if not isinstance(evidence, Mapping):
                continue
            value = evidence.get("value")
            by_value[str(value)].append(dict(evidence))
        alternatives = sorted(by_value, key=lambda value: float(value))
        if len(alternatives) < 2:
            continue
        first, rest = alternatives[0], alternatives[1:]
        metric = str(claim.get("metric") or "metric")
        currency = str(claim.get("currency") or "")
        instrument = {
            key: claim.get(key)
            for key in ("instrument_id", "canonical_symbol", "display_name", "asset_type")
        }
        evidence_a_items = by_value[first]
        evidence_b_items = [item for value in rest for item in by_value[value]]
        evidence_a = "；".join(
            f"snapshot#{item.get('snapshot_id')}/{item.get('provider_key')}" for item in evidence_a_items
        )
        evidence_b = "；".join(
            f"snapshot#{item.get('snapshot_id')}/{item.get('provider_key')}" for item in evidence_b_items
        )
        digest = hashlib.sha256(
            f"{claim.get('instrument_id')}|{metric}|{claim.get('as_of')}|{alternatives}".encode()
        ).hexdigest()[:12]
        result.append(
            _structured_conflict(
                key=f"fact_{digest}",
                conflict_type="fact_conflict",
                claim_a=f"{metric}={first} {currency}".strip(),
                claim_b=f"{metric}={' / '.join(rest)} {currency}".strip(),
                evidence_a=evidence_a,
                evidence_b=evidence_b,
                evidence={"a": evidence_a_items, "b": evidence_b_items},
                instrument=instrument,
                as_of={"a": claim.get("as_of"), "b": claim.get("as_of"), "period": claim.get("period")},
                verdict="conflicted",
                reason="同一标的、指标、币种、调整口径和有效时点/报告期的入库 Provider 数值超过容差；系统保留全部值，不自动选择。",
            )
        )
    return result


def _opinion_conflicts(opinions: Sequence[Mapping[str, object]]) -> list[dict]:
    result = []
    grouped = defaultdict(list)
    for opinion in opinions:
        grouped[int(_mapping(opinion.get("instrument")).get("instrument_id") or 0)].append(opinion)
    for instrument_id, items in grouped.items():
        if instrument_id <= 0:
            continue
        for left, right in combinations(items, 2):
            if str(left.get("recommendation")) == str(right.get("recommendation")):
                continue
            same_basis = bool(
                left.get("snapshot_ids")
                and set(left.get("snapshot_ids") or []) == set(right.get("snapshot_ids") or [])
            )
            key = f"opinion_{instrument_id}_{left['report_id']}_{right['report_id']}"
            reason = (
                "两份 TradingAgents 报告基于相同快照仍给出不同评级，这是观点/风险偏好差异，不是事实冲突。"
                if same_basis
                else "两份 TradingAgents 报告评级不同；各自时点和证据集保留，旧观点可以是历史有效观点，不被当作当前事实。"
            )
            result.append(
                _structured_conflict(
                    key=key,
                    conflict_type="opinion_difference",
                    claim_a=f"TradingAgents 评级：{left.get('recommendation')}",
                    claim_b=f"TradingAgents 评级：{right.get('recommendation')}",
                    evidence_a=_evidence_text(left),
                    evidence_b=_evidence_text(right),
                    evidence={"a": [dict(left)], "b": [dict(right)]},
                    instrument=_mapping(left.get("instrument")),
                    as_of={"a": left.get("as_of"), "b": right.get("as_of")},
                    verdict="opinion_difference",
                    reason=reason,
                    source_a=f"h{(left.get('source_chat_history_ids') or [0])[0]}",
                    source_b=f"h{(right.get('source_chat_history_ids') or [0])[0]}",
                )
            )
            result[-1]["temporal_context"] = {
                "a": left.get("temporal_status"),
                "b": right.get("temporal_status"),
                "same_fact_basis": same_basis,
            }
    return result


def _artifact_evidence(row: Mapping[str, object]) -> list[dict]:
    result = []
    for artifact in _mapping(row.get("financial_audit")).get("artifacts") or []:
        if not isinstance(artifact, Mapping):
            continue
        kind = str(artifact.get("artifact_type") or "")
        if kind not in {"snapshot", "final_report"}:
            continue
        result.append(
            {
                "artifact_type": kind,
                "artifact_ref": str(artifact.get("artifact_ref") or ""),
                "payload": _mapping(artifact.get("payload")),
            }
        )
    return result


def _enrich_model_conflicts(
    base_conflicts: Sequence[Mapping[str, object]],
    rows: Sequence[Mapping[str, object]],
) -> list[dict]:
    by_source = {f"h{_source_id(row)}": row for row in rows if _source_id(row) > 0}
    result = []
    for index, conflict in enumerate(base_conflicts, 1):
        if not isinstance(conflict, Mapping):
            continue
        source_a = str(conflict.get("source_a") or "")
        source_b = str(conflict.get("source_b") or "")
        row_a, row_b = by_source.get(source_a), by_source.get(source_b)
        targets_a = _target_map(row_a or {})
        targets_b = _target_map(row_b or {})
        shared = sorted(set(targets_a) & set(targets_b))
        instrument = targets_a[shared[0]] if shared else {}
        evidence_a = _artifact_evidence(row_a or {})
        evidence_b = _artifact_evidence(row_b or {})
        has_evidence = bool(evidence_a and evidence_b and shared)
        key = str(conflict.get("key") or f"model_{index}")
        result.append(
            _structured_conflict(
                key=key,
                conflict_type="opinion_difference" if has_evidence else "unverified_model_conflict",
                claim_a=str(conflict.get("claim_a") or "观点 A"),
                claim_b=str(conflict.get("claim_b") or "观点 B"),
                evidence_a=str(conflict.get("evidence_a") or "未提供证据"),
                evidence_b=str(conflict.get("evidence_b") or "未提供证据"),
                evidence={"a": evidence_a, "b": evidence_b},
                instrument=instrument,
                as_of={"a": _route(row_a or {}).get("server_now"), "b": _route(row_b or {}).get("server_now")},
                verdict="opinion_difference" if has_evidence else "pending_evidence",
                reason=(
                    "模型只识别到观点差异；快照/报告引用和标的一致，但不把评级当作事实。"
                    if has_evidence
                    else "模型提出了文本差异，但无法将双方同时绑定到同一标的和可核验证据，因此保持待裁决。"
                ),
                source_a=source_a,
                source_b=source_b,
            )
        )
    return result


def _scope_decision(rows: Sequence[Mapping[str, object]]) -> dict:
    financial_rows = [row for row in rows if isinstance(row.get("financial_audit"), Mapping)]
    target_sets = [set(_target_map(row)) for row in financial_rows if _target_map(row)]
    explicit_comparison = any(
        str(_route(row).get("intent") or "") == "comparison"
        or any(marker in f" {str(row.get('question') or '').casefold()} " for marker in COMPARISON_MARKERS)
        for row in financial_rows
    )
    shared = set.intersection(*target_sets) if len(target_sets) >= 2 else (target_sets[0] if target_sets else set())
    blocked = bool(len(target_sets) >= 2 and not shared and not explicit_comparison)
    return {
        "financial_answer_count": len(financial_rows),
        "target_sets": [sorted(items) for items in target_sets],
        "shared_instrument_ids": sorted(shared),
        "explicit_comparison": explicit_comparison,
        "cross_instrument_merge_blocked": blocked,
    }


def review_financial_synthesis(
    connection,
    cleaned_rows: Sequence[Mapping[str, object]],
    base_result: Mapping[str, object],
    *,
    server_now: datetime | None = None,
) -> dict:
    now = server_now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    fact_review = review_financial_history(connection, cleaned_rows, server_now=now)
    opinions = _load_opinions(connection, cleaned_rows)
    scope = _scope_decision(cleaned_rows)
    fact_conflicts = _fact_conflicts(fact_review)
    opinion_conflicts = _opinion_conflicts(opinions)
    model_conflicts = _enrich_model_conflicts(
        _array(base_result.get("conflicts")),
        cleaned_rows,
    )
    conflicts = [*fact_conflicts, *opinion_conflicts, *model_conflicts]
    if scope["cross_instrument_merge_blocked"]:
        recommended_relation = "none"
        conflicts = []
        relation_reason = "所选金融会话的稳定标的不同，且原问题未明确要求跨标的比较，已阻止误合并。"
    elif fact_conflicts or opinion_conflicts:
        recommended_relation = "oppositional"
        relation_reason = "同一标的存在已落地的事实冲突或 TradingAgents 观点差异。"
    elif scope["shared_instrument_ids"] and str(base_result.get("relation")) == "none":
        recommended_relation = "progressive"
        relation_reason = "多条回答绑定同一稳定标的，但时点不同，按时序审阅而不当作事实冲突。"
    else:
        recommended_relation = str(base_result.get("relation") or "none")
        relation_reason = "没有足够的结构化金融证据覆盖现有关系判定。"
    return {
        "schema_version": FINANCIAL_SYNTHESIS_REVIEW_VERSION,
        "reviewed_at": now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "base_relation": str(base_result.get("relation") or "none"),
        "recommended_relation": recommended_relation,
        "relation_reason": relation_reason,
        "scope": scope,
        "fact_review": fact_review,
        "opinions": opinions,
        "conflicts": conflicts,
        "conflict_counts": {
            "fact_conflict": len(fact_conflicts),
            "opinion_difference": len(opinion_conflicts),
            "pending_evidence": sum(1 for item in model_conflicts if item["verdict"] == "pending_evidence"),
        },
        "boundaries": {
            "model_wording_selects_winner": False,
            "fact_and_opinion_conflicts_separated": True,
            "historical_opinion_erased": False,
            "cross_instrument_implicit_merge": False,
            "provider_network_calls": 0,
            "real_order_execution": False,
        },
    }


__all__ = ["FINANCIAL_SYNTHESIS_REVIEW_VERSION", "review_financial_synthesis"]

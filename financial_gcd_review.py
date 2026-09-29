#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Evidence-first temporal review for the chat-history ÷ operation."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Mapping, Sequence


FINANCIAL_GCD_REVIEW_VERSION = "financial-gcd-review-v1"
CONFLICT_RELATIVE_TOLERANCE = 0.005
MAX_RECENT_SNAPSHOTS_PER_INSTRUMENT = 40


def _mapping(value: object) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _parse_json_object(value: object) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _parse_utc(value: object) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("timestamp is required")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    result = datetime.fromisoformat(text)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _finite(value: object):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _snapshot_ids(audit: Mapping[str, object]) -> list[int]:
    result = set()
    for artifact in audit.get("artifacts") or []:
        if not isinstance(artifact, Mapping) or artifact.get("artifact_type") != "snapshot":
            continue
        payload = _mapping(artifact.get("payload"))
        raw = payload.get("snapshot_id")
        if raw is None:
            ref = str(artifact.get("artifact_ref") or "")
            raw = ref.split(":", 1)[1] if ref.startswith("snapshot:") else None
        try:
            snapshot_id = int(raw)
        except (TypeError, ValueError):
            continue
        if snapshot_id > 0:
            result.add(snapshot_id)
    return sorted(result)


def _report_refs(audit: Mapping[str, object]) -> list[dict]:
    result = []
    seen = set()
    for artifact in audit.get("artifacts") or []:
        if not isinstance(artifact, Mapping) or artifact.get("artifact_type") != "final_report":
            continue
        payload = _mapping(artifact.get("payload"))
        try:
            report_id = int(payload.get("report_id"))
            version = int(payload.get("report_version"))
        except (TypeError, ValueError):
            continue
        identity = (report_id, version)
        if report_id <= 0 or version <= 0 or identity in seen:
            continue
        seen.add(identity)
        result.append(
            {
                "report_id": report_id,
                "report_version": version,
                "report_status": str(payload.get("report_status") or ""),
                "report_url": str(payload.get("report_url") or ""),
            }
        )
    return result


def _primary_metric(payload: Mapping[str, object], data_type: str):
    metric = str(payload.get("metric") or "").strip().casefold()
    aliases = {"price": "last_price", "close": "last_price", "last": "last_price"}
    metric = aliases.get(metric, metric)
    value = payload.get("value")
    scalar = _finite(value) if not isinstance(value, (Mapping, list, tuple)) else None
    normalized = _mapping(payload.get("normalized_payload"))
    if metric and scalar is None:
        scalar = _finite(normalized.get(metric))
    if scalar is None and str(data_type).casefold() == "quote":
        for key in ("last_price", "price", "close"):
            scalar = _finite(normalized.get(key))
            if scalar is not None:
                metric = "last_price"
                break
    if not metric and scalar is not None and str(data_type).casefold() == "quote":
        metric = "last_price"
    return metric, scalar


def _reported_period(payload: Mapping[str, object]):
    normalized = _mapping(payload.get("normalized_payload"))
    raw = None
    for key in ("period", "fiscal_period", "reporting_period", "report_period"):
        raw = payload.get(key)
        if raw not in (None, "", {}):
            break
        raw = normalized.get(key)
        if raw not in (None, "", {}):
            break
    if isinstance(raw, Mapping):
        clean = {
            key: str(raw.get(key) or "")
            for key in ("start", "end", "label")
            if str(raw.get(key) or "")
        }
        return clean
    text = str(raw or "").strip()
    return {"label": text} if text else {}


def _snapshot_record(row, server_now: datetime):
    payload_text = str(row[11] or "")
    if hashlib.sha256(payload_text.encode("utf-8")).hexdigest() != str(row[12] or ""):
        return None
    try:
        observed = _parse_utc(row[4])
        fetched = _parse_utc(row[5])
        payload = json.loads(payload_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping) or observed > server_now or fetched > server_now:
        return None
    metric, value = _primary_metric(payload, str(row[3] or ""))
    if not metric or value is None:
        return None
    stale_after = None
    if row[9]:
        try:
            stale_after = _parse_utc(row[9])
        except (TypeError, ValueError):
            stale_after = None
    adjustment = str(payload.get("adjustment") or "raw").strip().casefold() or "raw"
    currency = str(row[8] or payload.get("currency") or "").strip().upper()
    market_status = str(row[6] or "unknown").strip().casefold()
    quality_status = str(row[10] or "").strip().casefold()
    stale = quality_status.endswith(("_stale", "_historical"))
    if market_status not in {"closed", "post_close", "pre_open"}:
        stale = stale or (stale_after is not None and stale_after <= server_now)
    return {
        "snapshot_id": int(row[0]),
        "instrument_id": int(row[1]),
        "canonical_symbol": str(row[15] or ""),
        "display_name": str(row[16] or row[15] or ""),
        "asset_type": str(row[17] or ""),
        "data_type": str(row[3] or ""),
        "metric": metric,
        "value": value,
        "unit": str(payload.get("unit") or ("price" if metric == "last_price" else "")),
        "currency": currency,
        "adjustment": adjustment,
        "reported_period": _reported_period(payload),
        "observed_at": _utc_text(observed),
        "observed_dt": observed,
        "fetched_at": _utc_text(fetched),
        "stale_after": _utc_text(stale_after) if stale_after else "",
        "market_status": market_status,
        "quality_status": quality_status,
        "is_stale": bool(stale),
        "provider_key": str(row[14] or ""),
        "source_url": str(row[13] or ""),
        "payload_sha256": str(row[12] or ""),
    }


def _load_snapshots(connection, snapshot_ids: Sequence[int], server_now: datetime):
    if not snapshot_ids:
        return {}, []
    placeholders = ",".join("?" for _ in snapshot_ids)
    instrument_rows = connection.execute(
        f"SELECT DISTINCT instrument_id FROM financial_data_snapshots WHERE id IN ({placeholders})",
        list(snapshot_ids),
    ).fetchall()
    instrument_ids = sorted({int(row[0]) for row in instrument_rows if row[0] is not None})
    if not instrument_ids:
        return {}, []
    instrument_placeholders = ",".join("?" for _ in instrument_ids)
    snapshot_placeholders = ",".join("?" for _ in snapshot_ids)
    rows = connection.execute(
        f"""
        WITH ranked AS (
            SELECT snapshot.*,
                   ROW_NUMBER() OVER(
                       PARTITION BY snapshot.instrument_id
                       ORDER BY snapshot.observed_at DESC, snapshot.fetched_at DESC, snapshot.id DESC
                   ) AS recent_rank
            FROM financial_data_snapshots snapshot
            WHERE snapshot.instrument_id IN ({instrument_placeholders})
        )
        SELECT ranked.id, ranked.instrument_id, ranked.provider_profile_id,
               ranked.data_type, ranked.observed_at, ranked.fetched_at,
               ranked.market_status, ranked.timezone, ranked.currency,
               ranked.stale_after, ranked.quality_status,
               ranked.payload_json, ranked.payload_sha256, ranked.source_url,
               profile.provider_key, instrument.canonical_symbol,
               instrument.display_name, instrument.asset_type
        FROM ranked
        JOIN financial_provider_profiles profile ON profile.id=ranked.provider_profile_id
        JOIN financial_instruments instrument ON instrument.id=ranked.instrument_id
        WHERE ranked.recent_rank<=? OR ranked.id IN ({snapshot_placeholders})
        ORDER BY ranked.instrument_id, ranked.observed_at DESC,
                 ranked.fetched_at DESC, ranked.id DESC
        """,
        [*instrument_ids, MAX_RECENT_SNAPSHOTS_PER_INSTRUMENT, *snapshot_ids],
    ).fetchall()
    valid = []
    by_id = {}
    for row in rows:
        record = _snapshot_record(row, server_now)
        if record is None:
            continue
        valid.append(record)
        by_id[record["snapshot_id"]] = record
    return by_id, valid


def _dimension(record: Mapping[str, object]):
    return (
        int(record["instrument_id"]),
        str(record["metric"]),
        str(record["currency"]),
        str(record["adjustment"]),
    )


def _same_instant_key(record: Mapping[str, object]):
    period = _mapping(record.get("reported_period"))
    period_identity = json.dumps(period, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    effective_time = f"period:{period_identity}" if period else f"instant:{record['observed_at']}"
    return (*_dimension(record), effective_time)


def _values_conflict(values: Sequence[float]) -> bool:
    if len(values) < 2:
        return False
    low, high = min(values), max(values)
    tolerance = max(0.0000001, max(abs(low), abs(high)) * CONFLICT_RELATIVE_TOLERANCE)
    return high - low > tolerance


def _value_payload(values: Sequence[float]):
    ordered = sorted({round(float(value), 12) for value in values})
    if len(ordered) == 1:
        return ordered[0]
    return {"min": ordered[0], "max": ordered[-1], "alternatives": ordered}


def _merge_identical_validity_intervals(claims: Sequence[Mapping[str, object]]) -> list[dict]:
    """Merge identical facts only where their declared validity intervals overlap."""

    ordered = sorted(
        (dict(item) for item in claims),
        key=lambda item: (
            int(item["instrument_id"]),
            str(item["metric"]),
            str(item["currency"]),
            str(item["adjustment"]),
            str(item["as_of"]),
        ),
    )
    merged = []
    for claim in ordered:
        scalar = claim.get("value")
        if isinstance(scalar, Mapping) or str(claim.get("status")) == "conflicted":
            merged.append(claim)
            continue
        candidate = None
        for prior in reversed(merged):
            if isinstance(prior.get("value"), Mapping) or prior.get("status") == "conflicted":
                continue
            if (
                int(prior["instrument_id"]),
                str(prior["metric"]),
                str(prior["currency"]),
                str(prior["adjustment"]),
                float(prior["value"]),
            ) != (
                int(claim["instrument_id"]),
                str(claim["metric"]),
                str(claim["currency"]),
                str(claim["adjustment"]),
                float(scalar),
            ):
                continue
            try:
                prior_end = _parse_utc(_mapping(prior.get("period")).get("end"))
                claim_start = _parse_utc(_mapping(claim.get("period")).get("start"))
            except (TypeError, ValueError):
                continue
            if prior_end >= claim_start:
                candidate = prior
            break
        if candidate is None:
            merged.append(claim)
            continue
        prior_period = _mapping(candidate.get("period"))
        claim_period = _mapping(claim.get("period"))
        candidate["period"] = {
            "kind": "interval",
            "start": min(str(prior_period.get("start")), str(claim_period.get("start"))),
            "end": max(str(prior_period.get("end")), str(claim_period.get("end"))),
        }
        if str(claim.get("as_of")) >= str(candidate.get("as_of")):
            candidate["as_of"] = claim["as_of"]
            candidate["status"] = claim["status"]
            candidate["market_status"] = claim["market_status"]
        evidence_by_id = {
            int(item["snapshot_id"]): dict(item)
            for item in [*(candidate.get("evidence") or []), *(claim.get("evidence") or [])]
        }
        candidate["evidence"] = [evidence_by_id[key] for key in sorted(evidence_by_id)]
        candidate["source_chat_history_ids"] = sorted(
            {
                int(item)
                for item in [
                    *(candidate.get("source_chat_history_ids") or []),
                    *(claim.get("source_chat_history_ids") or []),
                ]
            }
        )
        candidate["source_session_ids"] = sorted(
            {
                str(item)
                for item in [
                    *(candidate.get("source_session_ids") or []),
                    *(claim.get("source_session_ids") or []),
                ]
                if str(item)
            }
        )
        for report in claim.get("report_refs") or []:
            if report not in candidate["report_refs"]:
                candidate["report_refs"].append(report)
    return merged


def review_financial_history(
    connection,
    cleaned_rows: Sequence[Mapping[str, object]],
    *,
    server_now: datetime | None = None,
) -> dict:
    """Review only facts backed by immutable snapshot references."""

    now = server_now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    snapshot_sources = defaultdict(
        lambda: {"history_ids": set(), "sessions": set(), "reports": [], "target_ids": set()}
    )
    financial_rows = []
    for row in cleaned_rows:
        audit = row.get("financial_audit")
        if not isinstance(audit, Mapping):
            continue
        financial_rows.append(row)
        reports = _report_refs(audit)
        target_ids = set()
        for target in audit.get("targets") or []:
            if not isinstance(target, Mapping):
                continue
            try:
                target_ids.add(int(target.get("instrument_id")))
            except (TypeError, ValueError):
                continue
        for snapshot_id in _snapshot_ids(audit):
            source = snapshot_sources[snapshot_id]
            source["target_ids"].update(item for item in target_ids if item > 0)
            try:
                source["history_ids"].add(int(row.get("source_chat_history_id") or row.get("id")))
            except (TypeError, ValueError):
                pass
            session_id = str(row.get("source_session_id") or "").strip()
            if session_id:
                source["sessions"].add(session_id)
            for report in reports:
                if report not in source["reports"]:
                    source["reports"].append(report)

    by_id, all_records = _load_snapshots(connection, sorted(snapshot_sources), now)
    latest_by_dimension = {}
    records_by_instant = defaultdict(list)
    for record in all_records:
        dimension = _dimension(record)
        if dimension not in latest_by_dimension:
            latest_by_dimension[dimension] = record
        records_by_instant[_same_instant_key(record)].append(record)

    selected_groups = defaultdict(list)
    valid_selected_snapshot_ids = set()
    for snapshot_id, source in snapshot_sources.items():
        record = by_id.get(snapshot_id)
        if record is None:
            continue
        if source["target_ids"] and int(record["instrument_id"]) not in source["target_ids"]:
            continue
        valid_selected_snapshot_ids.add(int(snapshot_id))
        selected_groups[_same_instant_key(record)].append((record, source))

    claims = []
    for instant_key, selected in selected_groups.items():
        representative = selected[0][0]
        verification_records = records_by_instant.get(instant_key) or [item[0] for item in selected]
        values = [float(item["value"]) for item in verification_records]
        conflicted = _values_conflict(values)
        latest = latest_by_dimension.get(_dimension(representative), representative)
        if conflicted:
            status = "conflicted"
        elif representative["observed_dt"] < latest["observed_dt"]:
            status = "historical"
        elif all(bool(item["is_stale"]) for item in verification_records):
            status = "stale"
        else:
            status = "verified_current"

        selected_source_ids = set()
        selected_sessions = set()
        report_refs = []
        selected_ids = {int(item[0]["snapshot_id"]) for item in selected}
        for _record, source in selected:
            selected_source_ids.update(source["history_ids"])
            selected_sessions.update(source["sessions"])
            for report in source["reports"]:
                if report not in report_refs:
                    report_refs.append(report)
        evidence = []
        for record in verification_records:
            source = snapshot_sources.get(int(record["snapshot_id"]), {})
            evidence.append(
                {
                    "snapshot_id": int(record["snapshot_id"]),
                    "provider_key": str(record["provider_key"]),
                    "source_url": str(record["source_url"]),
                    "payload_sha256": str(record["payload_sha256"]),
                    "observed_at": str(record["observed_at"]),
                    "fetched_at": str(record["fetched_at"]),
                    "value": float(record["value"]),
                    "selected_history_evidence": int(record["snapshot_id"]) in selected_ids,
                    "source_chat_history_ids": sorted(
                        int(item) for item in source.get("history_ids", set())
                    ),
                }
            )
        claims.append(
            {
                "instrument_id": int(representative["instrument_id"]),
                "canonical_symbol": str(representative["canonical_symbol"]),
                "display_name": str(representative["display_name"]),
                "asset_type": str(representative["asset_type"]),
                "metric": str(representative["metric"]),
                "value": _value_payload(values),
                "unit": str(representative["unit"]),
                "currency": str(representative["currency"]),
                "adjustment": str(representative["adjustment"]),
                "period": (
                    {"kind": "reported", **dict(representative["reported_period"])}
                    if representative["reported_period"]
                    else {
                        "kind": "instant",
                        "start": str(representative["observed_at"]),
                        "end": str(representative["stale_after"] or representative["observed_at"]),
                    }
                ),
                "as_of": str(representative["observed_at"]),
                "market_status": str(representative["market_status"]),
                "status": status,
                "evidence": evidence,
                "source_chat_history_ids": sorted(selected_source_ids),
                "source_session_ids": sorted(selected_sessions),
                "report_refs": report_refs,
            }
        )

    claims = _merge_identical_validity_intervals(claims)
    status_order = {"conflicted": 0, "stale": 1, "historical": 2, "verified_current": 3}
    claims.sort(
        key=lambda item: (
            status_order.get(str(item["status"]), 9),
            str(item["canonical_symbol"]),
            str(item["metric"]),
            str(item["currency"]),
            str(item["adjustment"]),
            str(item["as_of"]),
        )
    )
    counts = {name: 0 for name in status_order}
    for claim in claims:
        counts[claim["status"]] += 1
    unverified_source_answers = sum(
        1
        for row in financial_rows
        if not any(
            snapshot_id in valid_selected_snapshot_ids
            for snapshot_id in _snapshot_ids(_mapping(row.get("financial_audit")))
        )
    )
    return {
        "schema_version": FINANCIAL_GCD_REVIEW_VERSION,
        "reviewed_at": _utc_text(now),
        "source_financial_answer_count": len(financial_rows),
        "source_snapshot_reference_count": len(snapshot_sources),
        "unverified_source_answer_count": unverified_source_answers,
        "claims": claims,
        "status_counts": counts,
        "boundaries": {
            "original_sessions_deleted": False,
            "provider_network_calls": 0,
            "model_numeric_inference": False,
            "different_currency_compared": False,
            "different_adjustment_compared": False,
            "different_observation_times_compared": False,
        },
    }


def _cell(value: object) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def _display_value(value: object) -> str:
    if isinstance(value, Mapping):
        alternatives = value.get("alternatives") or []
        return " / ".join(str(item) for item in alternatives)
    return str(value)


def render_financial_gcd_review(review: Mapping[str, object]) -> str:
    labels = {
        "verified_current": "verified_current（已核验当前）",
        "historical": "historical（历史有效）",
        "stale": "stale（过期）",
        "conflicted": "conflicted（冲突）",
    }
    lines = [
        "金融事实时效审阅",
        "",
        f"服务器审阅时点：{_cell(review.get('reviewed_at'))}",
        "只保留可追溯到已入库快照的数值事实；冲突值全部展示，未自动选择。",
        "",
    ]
    claims = list(review.get("claims") or [])
    if not claims:
        lines.append("未从所选金融回答中找到可校验的 snapshot 数值事实；原会话仍保留。")
        return "\n".join(lines)
    lines.extend(
        [
            "|状态|标的|指标|数值|币种|调整口径|观测时点|证据|",
            "|---|---|---|---:|---|---|---|---|",
        ]
    )
    for claim in claims:
        evidence_bits = []
        for evidence in claim.get("evidence") or []:
            evidence_bits.append(
                f"snapshot#{int(evidence.get('snapshot_id') or 0)}/{_cell(evidence.get('provider_key'))}"
            )
        for report in claim.get("report_refs") or []:
            evidence_bits.append(
                f"report#{int(report.get('report_id') or 0)}/v{int(report.get('report_version') or 0)}"
            )
        lines.append(
            "|{status}|{instrument}|{metric}|{value}|{currency}|{adjustment}|{as_of}|{evidence}|".format(
                status=_cell(labels.get(str(claim.get("status")), claim.get("status"))),
                instrument=_cell(
                    f"{claim.get('display_name') or ''} {claim.get('canonical_symbol') or ''}"
                ),
                metric=_cell(claim.get("metric")),
                value=_cell(_display_value(claim.get("value"))),
                currency=_cell(claim.get("currency")),
                adjustment=_cell(claim.get("adjustment")),
                as_of=_cell(claim.get("as_of")),
                evidence=_cell("；".join(evidence_bits)),
            )
        )
    unverified = int(review.get("unverified_source_answer_count") or 0)
    if unverified:
        lines.extend(
            [
                "",
                f"另有 {unverified} 条金融回答没有 snapshot 引用，未将其中的数字当作已核验事实；请回到原会话审阅。",
            ]
        )
    return "\n".join(lines)


__all__ = [
    "FINANCIAL_GCD_REVIEW_VERSION",
    "render_financial_gcd_review",
    "review_financial_history",
]

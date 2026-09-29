#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Transactional links between legacy chat history and financial evidence."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Mapping, Sequence


FINANCIAL_CHAT_AUDIT_VERSION = "financial-chat-audit-v1"


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _object(value: object) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _array(value: object) -> list:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _route_row(connection, session_id: str, question: str, route_key: str):
    digest = hashlib.sha256(str(question).encode("utf-8")).hexdigest()
    if route_key:
        row = connection.execute(
            """
            SELECT id, route_key, session_id, intent, financial_attributes_json,
                   resolved_targets_json, route_status, route_destination,
                   server_now, server_timezone
            FROM chat_financial_routes
            WHERE route_key=? AND session_id=? AND question_sha256=?
            """,
            (str(route_key), str(session_id), digest),
        ).fetchone()
    else:
        row = connection.execute(
            """
            SELECT id, route_key, session_id, intent, financial_attributes_json,
                   resolved_targets_json, route_status, route_destination,
                   server_now, server_timezone
            FROM chat_financial_routes
            WHERE session_id=? AND question_sha256=?
            ORDER BY updated_at DESC, id DESC LIMIT 1
            """,
            (str(session_id), digest),
        ).fetchone()
    if row is None:
        return None
    attributes = _object(row[4])
    intent = _object(attributes.get("financial_intent"))
    if not intent.get("is_financial"):
        return None
    return {
        "id": int(row[0]),
        "route_key": str(row[1]),
        "session_id": str(row[2]),
        "intent": str(row[3]),
        "attributes": attributes,
        "targets": [dict(item) for item in _array(row[5]) if isinstance(item, Mapping)],
        "route_status": str(row[6]),
        "route_destination": str(row[7]),
        "server_now": str(row[8]),
        "server_timezone": str(row[9]),
    }


def _artifact_candidates(route: Mapping[str, object]) -> dict:
    attributes = route["attributes"]
    snapshots = set()
    runs = set()
    reports = set()

    realtime = _object(attributes.get("realtime_query"))
    for item in realtime.get("evidence") or []:
        if isinstance(item, Mapping) and item.get("snapshot_id") is not None:
            try:
                snapshots.add(int(item["snapshot_id"]))
            except (TypeError, ValueError):
                pass

    market = _object(attributes.get("market_scope"))
    market_report = _object(market.get("report"))
    for item in market_report.get("snapshot_ids") or []:
        try:
            snapshots.add(int(item))
        except (TypeError, ValueError):
            pass
    if market_report.get("research_run_id"):
        runs.add(str(market_report["research_run_id"]))
    if market_report.get("report_id") is not None:
        try:
            reports.add(int(market_report["report_id"]))
        except (TypeError, ValueError):
            pass

    full = _object(attributes.get("full_research"))
    for item in full.get("research_run_ids") or []:
        if str(item or "").strip():
            runs.add(str(item))
    for item in full.get("jobs") or []:
        if isinstance(item, Mapping) and str(item.get("research_run_id") or "").strip():
            runs.add(str(item["research_run_id"]))
    for item in full.get("reports") or []:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("research_run_id") or "").strip():
            runs.add(str(item["research_run_id"]))
        try:
            reports.add(int(item.get("report_id")))
        except (TypeError, ValueError):
            pass
        for source in item.get("source_refs") or []:
            if isinstance(source, Mapping) and source.get("snapshot_id") is not None:
                try:
                    snapshots.add(int(source["snapshot_id"]))
                except (TypeError, ValueError):
                    pass
    return {
        "snapshots": sorted(item for item in snapshots if item > 0),
        "runs": sorted(item for item in runs if item),
        "reports": sorted(item for item in reports if item > 0),
    }


def _upsert_artifact(
    connection,
    *,
    chat_history_id: int,
    chat_route_id: int,
    artifact_type: str,
    artifact_ref: str,
    payload: Mapping[str, object],
    research_run_id=None,
    final_report_id=None,
    snapshot_id=None,
) -> None:
    connection.execute(
        """
        INSERT INTO chat_financial_artifacts(
            chat_history_id, chat_route_id, artifact_type, artifact_ref,
            research_run_id, final_report_id, snapshot_id, payload_json
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_route_id, artifact_type, artifact_ref) DO UPDATE SET
            chat_history_id=excluded.chat_history_id,
            research_run_id=excluded.research_run_id,
            final_report_id=excluded.final_report_id,
            snapshot_id=excluded.snapshot_id,
            payload_json=excluded.payload_json
        """,
        (
            int(chat_history_id),
            int(chat_route_id),
            str(artifact_type),
            str(artifact_ref),
            research_run_id,
            final_report_id,
            snapshot_id,
            _canonical(dict(payload)),
        ),
    )


def attach_financial_audit(
    connection,
    *,
    chat_history_id: int,
    session_id: str,
    question: str,
    route_key: str = "",
) -> dict:
    """Attach allow-listed route/evidence references inside the caller transaction."""

    route = _route_row(connection, session_id, question, route_key)
    if route is None:
        return {"status": "skipped", "artifact_count": 0}
    count = 0
    route_payload = {
        "audit_version": FINANCIAL_CHAT_AUDIT_VERSION,
        "route_key": route["route_key"],
        "intent": route["intent"],
        "route_status": route["route_status"],
        "route_destination": route["route_destination"],
        "server_now": route["server_now"],
        "server_timezone": route["server_timezone"],
    }
    _upsert_artifact(
        connection,
        chat_history_id=chat_history_id,
        chat_route_id=route["id"],
        artifact_type="route_context",
        artifact_ref=f"route:{route['route_key']}",
        payload=route_payload,
    )
    count += 1
    for target in route["targets"]:
        try:
            instrument_id = int(target["instrument_id"])
        except (KeyError, TypeError, ValueError):
            continue
        _upsert_artifact(
            connection,
            chat_history_id=chat_history_id,
            chat_route_id=route["id"],
            artifact_type="instrument",
            artifact_ref=f"instrument:{instrument_id}",
            payload={
                key: target.get(key)
                for key in (
                    "instrument_id", "canonical_symbol", "display_name", "asset_type",
                    "market", "exchange", "currency", "share_class",
                )
            },
        )
        count += 1

    candidates = _artifact_candidates(route)
    for snapshot_id in candidates["snapshots"]:
        row = connection.execute(
            """
            SELECT snapshot.id, snapshot.observed_at, snapshot.fetched_at,
                   snapshot.market_status, snapshot.source_url, profile.provider_key
            FROM financial_data_snapshots snapshot
            JOIN financial_provider_profiles profile
              ON profile.id=snapshot.provider_profile_id
            WHERE snapshot.id=?
            """,
            (snapshot_id,),
        ).fetchone()
        if row is None:
            continue
        _upsert_artifact(
            connection,
            chat_history_id=chat_history_id,
            chat_route_id=route["id"],
            artifact_type="snapshot",
            artifact_ref=f"snapshot:{snapshot_id}",
            snapshot_id=snapshot_id,
            payload={
                "snapshot_id": snapshot_id,
                "observed_at": str(row[1] or ""),
                "fetched_at": str(row[2] or ""),
                "market_status": str(row[3] or ""),
                "source_url": str(row[4] or ""),
                "provider_key": str(row[5] or ""),
            },
        )
        count += 1

    for run_id in candidates["runs"]:
        row = connection.execute(
            """
            SELECT id, scope_type, status, requested_at, completed_at
            FROM financial_research_runs WHERE id=?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            continue
        _upsert_artifact(
            connection,
            chat_history_id=chat_history_id,
            chat_route_id=route["id"],
            artifact_type="research_run",
            artifact_ref=f"research_run:{run_id}",
            research_run_id=run_id,
            payload={
                "research_run_id": str(row[0]),
                "scope_type": str(row[1]),
                "status": str(row[2]),
                "requested_at": str(row[3] or ""),
                "completed_at": str(row[4] or ""),
            },
        )
        count += 1

    for report_id in candidates["reports"]:
        row = connection.execute(
            """
            SELECT id, research_run_id, report_version, report_status,
                   observed_at, fetched_at, verified_at
            FROM financial_final_reports WHERE id=?
            """,
            (report_id,),
        ).fetchone()
        if row is None:
            continue
        run_id = str(row[1])
        _upsert_artifact(
            connection,
            chat_history_id=chat_history_id,
            chat_route_id=route["id"],
            artifact_type="final_report",
            artifact_ref=f"final_report:{report_id}:v{int(row[2])}",
            research_run_id=run_id,
            final_report_id=report_id,
            payload={
                "report_id": report_id,
                "research_run_id": run_id,
                "report_version": int(row[2]),
                "report_status": str(row[3]),
                "observed_at": str(row[4] or ""),
                "fetched_at": str(row[5] or ""),
                "verified_at": str(row[6] or ""),
                "report_url": f"/api/financial/reports/{report_id}",
            },
        )
        count += 1
    return {
        "status": "attached",
        "route_key": route["route_key"],
        "artifact_count": count,
    }


def load_financial_audits(connection, chat_history_ids: Sequence[int]) -> dict[int, dict]:
    ids = sorted({int(item) for item in chat_history_ids if int(item) > 0})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"""
        SELECT artifact.chat_history_id, artifact.artifact_type,
               artifact.artifact_ref, artifact.payload_json,
               route.route_key, route.intent, route.route_status,
               route.route_destination, route.server_now, route.server_timezone,
               route.resolved_targets_json
        FROM chat_financial_artifacts artifact
        JOIN chat_financial_routes route ON route.id=artifact.chat_route_id
        WHERE artifact.chat_history_id IN ({placeholders})
        ORDER BY artifact.chat_history_id, artifact.id
        """,
        ids,
    ).fetchall()
    grouped = defaultdict(list)
    route_rows = {}
    for row in rows:
        history_id = int(row[0])
        grouped[history_id].append(
            {
                "artifact_type": str(row[1]),
                "artifact_ref": str(row[2]),
                "payload": _object(row[3]),
            }
        )
        route_rows.setdefault(history_id, row)
    result = {}
    for history_id, artifacts in grouped.items():
        row = route_rows[history_id]
        result[history_id] = {
            "schema_version": FINANCIAL_CHAT_AUDIT_VERSION,
            "route": {
                "route_key": str(row[4]),
                "intent": str(row[5]),
                "route_status": str(row[6]),
                "route_destination": str(row[7]),
                "server_now": str(row[8]),
                "server_timezone": str(row[9]),
            },
            "targets": [
                dict(item) for item in _array(row[10]) if isinstance(item, Mapping)
            ],
            "artifacts": artifacts,
        }
    return result


def delete_session_financial_audit(connection, session_id: str) -> int:
    cursor = connection.execute(
        "DELETE FROM chat_financial_routes WHERE session_id=?",
        (str(session_id),),
    )
    return max(0, int(cursor.rowcount or 0))


__all__ = [
    "FINANCIAL_CHAT_AUDIT_VERSION",
    "attach_financial_audit",
    "delete_session_financial_audit",
    "load_financial_audits",
]

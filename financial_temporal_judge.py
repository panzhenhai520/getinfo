#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Deterministic temporal adjudication for grounded financial claims.

The judge decides whether evidence is temporally usable at the application
server's request time.  It deliberately does not decide value conflicts or
investment opinions; those are separate stage 4 responsibilities.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone

from jsonschema import Draft202012Validator

from financial_market_clock import MarketClockService, RequestTimeContext


FINANCIAL_TEMPORAL_JUDGE_VERSION = "financial-temporal-judge-v1"
FINANCIAL_TEMPORAL_VERDICT_SCHEMA_VERSION = "financial-temporal-verdict-v1"
TEMPORAL_VERDICTS = (
    "verified_current",
    "verified_historical",
    "superseded",
    "stale",
    "insufficient_evidence",
)

DEFAULT_FRESHNESS_SECONDS = {
    "last_price": 300,
    "close": 300,
    "open": 300,
    "high": 300,
    "low": 300,
    "index_level": 300,
    "price_change": 300,
    "market_breadth": 300,
    "nav": 86_400,
    "announcement": 3_600,
    "announced_event": 3_600,
    "index_membership": 86_400,
    "default": 86_400,
}
REALTIME_METRICS = frozenset(
    {
        "last_price",
        "close",
        "open",
        "high",
        "low",
        "index_level",
        "price_change",
        "market_breadth",
        "volume",
        "turnover",
    }
)
VERSIONED_EVENT_TYPES = frozenset(
    {
        "restatement",
        "financial_restatement",
        "macro_revision",
        "revision",
        "split",
        "reverse_split",
        "corporate_action",
        "index_rebalance",
        "constituent_change",
    }
)
ACTIVE_MARKET_STATES = frozenset({"open", "auction", "after_hours"})
CLOSED_MARKET_STATES = frozenset({"closed", "pre_open", "lunch_break"})
MAX_CLOSED_MARKET_CARRY_SECONDS = 4 * 86_400


TEMPORAL_VERDICT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "schema_version",
        "judge_version",
        "claim_key",
        "claim_type",
        "metric",
        "verdict",
        "reason_codes",
        "request_time",
        "market_session",
        "selected_evidence_ids",
        "ignored_evidence_ids",
        "temporal_comparison",
        "boundaries",
    ],
    "properties": {
        "schema_version": {"const": FINANCIAL_TEMPORAL_VERDICT_SCHEMA_VERSION},
        "judge_version": {"const": FINANCIAL_TEMPORAL_JUDGE_VERSION},
        "claim_key": {"type": "string"},
        "claim_type": {"type": "string"},
        "metric": {"type": "string"},
        "verdict": {"enum": list(TEMPORAL_VERDICTS)},
        "reason_codes": {"type": "array", "items": {"type": "string"}},
        "request_time": {"type": "object"},
        "market_session": {"type": "object"},
        "selected_evidence_ids": {"type": "array", "items": {"type": "integer"}},
        "ignored_evidence_ids": {"type": "array", "items": {"type": "integer"}},
        "temporal_comparison": {"type": "object"},
        "boundaries": {"type": "object"},
    },
    "additionalProperties": False,
}
_VERDICT_VALIDATOR = Draft202012Validator(TEMPORAL_VERDICT_SCHEMA)


def _mapping(value: object) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _json_object(value: object) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _parse_utc(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _require_aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _integer(value: object) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _revision(value: object) -> tuple:
    """Return a deterministic comparable revision key without guessing dates."""
    if value in (None, ""):
        return ()
    if isinstance(value, bool):
        return ()
    if isinstance(value, (int, float)):
        return (1, float(value))
    text = str(value).strip()
    try:
        return (1, float(text))
    except ValueError:
        return (0, text.casefold())


def _period_identity(value: object) -> tuple:
    period = _mapping(value)
    if not period:
        return ()
    return tuple(
        (key, str(period.get(key) or ""))
        for key in ("kind", "start", "end", "label")
        if str(period.get(key) or "")
    )


def _is_reported_period(period: Mapping[str, object]) -> bool:
    return str(period.get("kind") or "").casefold() in {
        "reported",
        "fiscal_period",
        "quarter",
        "year",
    }


def _request_payload(
    request_context: RequestTimeContext | Mapping[str, object] | datetime,
) -> tuple[datetime, datetime, dict]:
    if isinstance(request_context, RequestTimeContext):
        server_now = request_context.server_now_utc
        requested_as_of = server_now
        payload = request_context.to_dict()
    elif isinstance(request_context, datetime):
        server_now = _require_aware(request_context, "request_context")
        requested_as_of = server_now
        payload = {
            "server_now_utc": _utc_text(server_now),
            "server_timezone": "UTC",
            "user_timezone": "UTC",
        }
    elif isinstance(request_context, Mapping):
        source = dict(request_context)
        server_now = _parse_utc(source.get("server_now_utc") or source.get("server_now"))
        if server_now is None:
            raise ValueError("request_context.server_now_utc is required")
        requested_as_of = _parse_utc(source.get("requested_as_of")) or server_now
        payload = {
            "server_now_utc": _utc_text(server_now),
            "server_timezone": str(source.get("server_timezone") or "UTC"),
            "user_timezone": str(source.get("user_timezone") or source.get("server_timezone") or "UTC"),
        }
    else:
        raise ValueError("request_context must be RequestTimeContext, mapping, or aware datetime")
    if requested_as_of > server_now:
        raise ValueError("requested_as_of cannot be later than server_now_utc")
    payload["requested_as_of"] = _utc_text(requested_as_of)
    return server_now, requested_as_of, payload


def _market_payload(value: Mapping[str, object] | None) -> dict:
    source = _mapping(value)
    return {
        "market_calendar_id": str(
            source.get("market_calendar_id") or source.get("calendar_id") or ""
        ),
        "market_session_state": str(
            source.get("market_session_state") or source.get("market_status") or "unknown"
        ).casefold(),
        "trading_date": str(source.get("trading_date") or ""),
        "session_open_utc": str(source.get("session_open_utc") or ""),
        "session_close_utc": str(source.get("session_close_utc") or ""),
        "calendar_source": str(source.get("calendar_source") or ""),
        "calendar_version": str(source.get("calendar_version") or ""),
        "reason": str(source.get("reason") or ""),
    }


def _evidence_time(value: Mapping[str, object]) -> datetime | None:
    return (
        _parse_utc(value.get("effective_at"))
        or _parse_utc(value.get("effective_from"))
        or _parse_utc(value.get("observed_at"))
    )


def _evidence_id(value: Mapping[str, object]) -> int | None:
    return _integer(value.get("evidence_id") if "evidence_id" in value else value.get("id"))


class FinancialTemporalJudge:
    """Classify one grounded claim using only persisted temporal metadata."""

    def __init__(
        self,
        *,
        freshness_seconds: Mapping[str, int] | None = None,
        closed_market_carry_seconds: int = MAX_CLOSED_MARKET_CARRY_SECONDS,
    ):
        self.freshness_seconds = dict(DEFAULT_FRESHNESS_SECONDS)
        for metric, value in (freshness_seconds or {}).items():
            threshold = int(value)
            if threshold <= 0:
                raise ValueError("freshness thresholds must be positive")
            self.freshness_seconds[str(metric).casefold()] = threshold
        self.closed_market_carry_seconds = int(closed_market_carry_seconds)
        if self.closed_market_carry_seconds <= 0:
            raise ValueError("closed_market_carry_seconds must be positive")

    def _threshold(self, metric: str, evidence: Sequence[Mapping[str, object]]) -> int:
        candidates = []
        for item in evidence:
            raw = item.get("freshness_threshold_seconds")
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value > 0:
                candidates.append(value)
        if candidates:
            return min(candidates)
        return int(
            self.freshness_seconds.get(
                metric.casefold(), self.freshness_seconds["default"]
            )
        )

    @staticmethod
    def _normalise_claim(claim: Mapping[str, object]) -> dict:
        source = dict(claim)
        normalized = _json_object(source.get("normalized_value_json"))
        result = {
            **normalized,
            **{
                key: source[key]
                for key in (
                    "id",
                    "claim_key",
                    "claim_type",
                    "statement",
                    "metric",
                    "value",
                    "unit",
                    "currency",
                    "period",
                    "as_of",
                    "revision",
                    "effective_to",
                    "effective_at",
                    "observed_at",
                )
                if source.get(key) not in (None, "")
            },
        }
        result["metric"] = str(result.get("metric") or "").strip().casefold()
        result["claim_key"] = str(result.get("claim_key") or "")
        result["claim_type"] = str(result.get("claim_type") or "fact").casefold()
        result["period"] = _mapping(result.get("period"))
        return result

    @staticmethod
    def _normalise_evidence(
        values: Sequence[Mapping[str, object]],
        server_now: datetime,
        requested_as_of: datetime,
    ) -> tuple[list[dict], list[int], list[str]]:
        accepted: list[dict] = []
        ignored: list[int] = []
        reasons: list[str] = []
        for raw in values:
            item = dict(raw)
            evidence_id = _evidence_id(item)
            if evidence_id is not None:
                item["evidence_id"] = evidence_id
            observed = _parse_utc(item.get("observed_at"))
            fetched = _parse_utc(item.get("fetched_at")) or observed
            effective = _evidence_time(item)
            if item.get("integrity_valid") is False:
                if evidence_id is not None:
                    ignored.append(evidence_id)
                reasons.append("evidence_integrity_failed")
                continue
            if observed is None and effective is None:
                if evidence_id is not None:
                    ignored.append(evidence_id)
                reasons.append("evidence_time_missing")
                continue
            if any(value is not None and value > server_now for value in (observed, fetched, effective)):
                if evidence_id is not None:
                    ignored.append(evidence_id)
                reasons.append("future_evidence_ignored")
                continue
            if any(
                value is not None and value > requested_as_of
                for value in (observed, fetched, effective)
            ):
                if evidence_id is not None:
                    ignored.append(evidence_id)
                reasons.append("post_request_evidence_ignored")
                continue
            item["observed_dt"] = observed or effective
            item["fetched_dt"] = fetched or observed or effective
            item["effective_dt"] = effective or observed
            item["effective_to_dt"] = _parse_utc(item.get("effective_to"))
            item["relationship"] = str(item.get("relationship") or "supports").casefold()
            item["event_type"] = str(item.get("event_type") or "").casefold()
            item["freshness_state"] = str(item.get("freshness_state") or "unknown").casefold()
            item["quality_status"] = str(item.get("quality_status") or "").casefold()
            item["period"] = _mapping(item.get("period"))
            accepted.append(item)
        accepted.sort(
            key=lambda item: (
                item.get("effective_dt") or datetime.min.replace(tzinfo=timezone.utc),
                item.get("observed_dt") or datetime.min.replace(tzinfo=timezone.utc),
                item.get("evidence_id") or 0,
            )
        )
        return accepted, sorted(set(ignored)), list(dict.fromkeys(reasons))

    @staticmethod
    def _superseding_evidence(
        claim: Mapping[str, object],
        evidence: Sequence[Mapping[str, object]],
        requested_as_of: datetime,
    ) -> list[dict]:
        claim_key = str(claim.get("claim_key") or "")
        claim_time = _parse_utc(claim.get("as_of") or claim.get("observed_at"))
        claim_revision = _revision(claim.get("revision"))
        claim_period = _period_identity(claim.get("period"))
        metric = str(claim.get("metric") or "")
        result = []
        for item in evidence:
            effective = item.get("effective_dt")
            if effective is not None and effective > requested_as_of:
                continue
            supersedes_key = str(item.get("supersedes_claim_key") or "")
            explicit = item.get("relationship") in {"supersedes", "revises"}
            explicit = explicit or bool(supersedes_key and supersedes_key == claim_key)
            event = item.get("event_type") in VERSIONED_EVENT_TYPES
            affected = {str(value).casefold() for value in item.get("affects_metrics") or []}
            event_affects_claim = event and (not affected or metric in affected)
            evidence_period = _period_identity(item.get("period"))
            newer_revision = bool(
                claim_revision
                and _revision(item.get("revision")) > claim_revision
                and (not claim_period or not evidence_period or evidence_period == claim_period)
            )
            after_claim = claim_time is None or effective is None or effective > claim_time
            if after_claim and (explicit or event_affects_claim or newer_revision):
                result.append(dict(item))
        return result

    def judge(
        self,
        claim: Mapping[str, object],
        evidence: Sequence[Mapping[str, object]],
        *,
        request_context: RequestTimeContext | Mapping[str, object] | datetime,
        market_session: Mapping[str, object] | None = None,
    ) -> dict:
        server_now, requested_as_of, request_payload = _request_payload(request_context)
        normalized_claim = self._normalise_claim(claim)
        metric = normalized_claim["metric"]
        claim_type = normalized_claim["claim_type"]
        market_payload = _market_payload(market_session)
        accepted, ignored_ids, input_reasons = self._normalise_evidence(
            evidence, server_now, requested_as_of
        )
        reason_codes = list(input_reasons)
        verdict = "insufficient_evidence"

        support = [item for item in accepted if item["relationship"] in {"supports", "confirms"}]
        selected_ids = sorted(
            {
                int(item["evidence_id"])
                for item in support
                if item.get("evidence_id") is not None
            }
        )
        superseding = self._superseding_evidence(normalized_claim, accepted, requested_as_of)
        claim_observed = _parse_utc(
            normalized_claim.get("as_of") or normalized_claim.get("observed_at")
        )
        period = _mapping(normalized_claim.get("period"))
        effective_to = (
            _parse_utc(normalized_claim.get("effective_to"))
            or _parse_utc(period.get("valid_to"))
            or (
                _parse_utc(period.get("end"))
                if str(period.get("kind") or "").casefold() in {"interval", "validity"}
                else None
            )
        )
        latest_observed = max(
            (item["observed_dt"] for item in support if item.get("observed_dt")),
            default=None,
        )
        newest_fetched = max(
            (item["fetched_dt"] for item in support if item.get("fetched_dt")),
            default=None,
        )
        threshold = self._threshold(metric, support)
        age_seconds = (
            max(0.0, (requested_as_of - latest_observed).total_seconds())
            if latest_observed is not None
            else None
        )
        historical_request = requested_as_of < server_now - timedelta(seconds=1)

        if claim_type != "fact":
            reason_codes.append("non_fact_not_temporally_verified")
        elif superseding:
            verdict = "superseded"
            reason_codes.append("newer_revision_or_effective_event")
            selected_ids = sorted(
                {
                    int(item["evidence_id"])
                    for item in superseding
                    if item.get("evidence_id") is not None
                }
            )
        elif effective_to is not None and effective_to < requested_as_of:
            verdict = "superseded"
            reason_codes.append("declared_validity_ended")
        elif not support:
            reason_codes.append("no_supporting_temporal_evidence")
        elif claim_observed is not None and claim_observed > requested_as_of:
            reason_codes.append("claim_is_future_at_requested_time")
        elif historical_request:
            verdict = "verified_historical"
            reason_codes.append("historical_request_time")
        elif _is_reported_period(period):
            verdict = "verified_historical"
            reason_codes.append("reported_period_fact")
        elif metric in REALTIME_METRICS:
            latest_for_claim = (
                claim_observed is None
                or latest_observed is None
                or claim_observed >= latest_observed
            )
            forced_stale = all(
                item.get("freshness_state") == "stale"
                or item.get("quality_status", "").endswith(("_stale", "_historical"))
                or (
                    _parse_utc(item.get("stale_after")) is not None
                    and _parse_utc(item.get("stale_after")) <= requested_as_of
                )
                for item in support
            )
            market_state = market_payload["market_session_state"]
            closed_carry = bool(
                latest_for_claim
                and not forced_stale
                and market_state in CLOSED_MARKET_STATES
                and age_seconds is not None
                and age_seconds <= self.closed_market_carry_seconds
            )
            if not latest_for_claim:
                verdict = "verified_historical"
                reason_codes.append("newer_observation_exists")
            elif forced_stale:
                verdict = "stale"
                reason_codes.append("provider_marked_stale")
            elif age_seconds is not None and age_seconds <= threshold:
                verdict = "verified_current"
                reason_codes.append("within_freshness_threshold")
            elif closed_carry:
                verdict = "verified_current"
                reason_codes.extend(
                    ["market_closed", "latest_closed_market_observation"]
                )
            else:
                verdict = "stale"
                reason_codes.append("freshness_threshold_exceeded")
        else:
            forced_stale = all(
                item.get("freshness_state") == "stale"
                or item.get("quality_status", "").endswith(("_stale", "_historical"))
                or (
                    _parse_utc(item.get("stale_after")) is not None
                    and _parse_utc(item.get("stale_after")) <= requested_as_of
                )
                for item in support
            )
            if forced_stale:
                verdict = "stale"
                reason_codes.append("provider_marked_stale")
            elif newest_fetched is None:
                reason_codes.append("fetch_time_missing")
            elif max(0.0, (server_now - newest_fetched).total_seconds()) > threshold:
                verdict = "stale"
                reason_codes.append("provider_refresh_threshold_exceeded")
            else:
                verdict = "verified_current"
                reason_codes.append("latest_effective_version")

        result = {
            "schema_version": FINANCIAL_TEMPORAL_VERDICT_SCHEMA_VERSION,
            "judge_version": FINANCIAL_TEMPORAL_JUDGE_VERSION,
            "claim_key": normalized_claim["claim_key"],
            "claim_type": claim_type,
            "metric": metric,
            "verdict": verdict,
            "reason_codes": list(dict.fromkeys(reason_codes)),
            "request_time": request_payload,
            "market_session": market_payload,
            "selected_evidence_ids": selected_ids,
            "ignored_evidence_ids": ignored_ids,
            "temporal_comparison": {
                "claim_observed_at": _utc_text(claim_observed),
                "claim_period": period,
                "claim_effective_to": _utc_text(effective_to),
                "latest_evidence_observed_at": _utc_text(latest_observed),
                "latest_evidence_fetched_at": _utc_text(newest_fetched),
                "freshness_age_seconds": age_seconds,
                "freshness_threshold_seconds": threshold,
                "supporting_evidence_count": len(support),
                "superseding_evidence_count": len(superseding),
            },
            "boundaries": {
                "value_conflict_decided": False,
                "investment_opinion_verified": False,
                "model_calls": 0,
                "network_calls": 0,
                "server_time_authoritative": True,
                "closed_market_is_explicit": market_payload["market_session_state"]
                in CLOSED_MARKET_STATES,
            },
        }
        _VERDICT_VALIDATOR.validate(result)
        return result


class FinancialTemporalJudgeService:
    """Load claim/evidence rows and append idempotent temporal verdict versions."""

    def __init__(
        self,
        database,
        *,
        judge: FinancialTemporalJudge | None = None,
        market_clock: MarketClockService | None = None,
    ):
        self.database = database
        self.judge = judge or FinancialTemporalJudge()
        self.market_clock = market_clock or MarketClockService()

    @property
    def connection(self):
        self.database._ensure_connection()
        return self.database.connection

    def _load_claim(self, claim_id: int):
        with self.database.lock:
            row = self.connection.execute(
                """
                SELECT claim.id, claim.claim_key, claim.claim_type, claim.subject,
                       claim.statement, claim.normalized_value_json, claim.unit,
                       claim.currency, claim.effective_at, claim.observed_at,
                       claim.verification_status, instrument.exchange,
                       instrument.market, instrument.asset_type
                FROM financial_claims claim
                JOIN financial_research_runs run ON run.id=claim.research_run_id
                LEFT JOIN financial_instruments instrument ON instrument.id=run.instrument_id
                WHERE claim.id=?
                """,
                (int(claim_id),),
            ).fetchone()
        if row is None:
            return None
        keys = (
            "id",
            "claim_key",
            "claim_type",
            "subject",
            "statement",
            "normalized_value_json",
            "unit",
            "currency",
            "effective_at",
            "observed_at",
            "verification_status",
            "exchange",
            "market",
            "asset_type",
        )
        return dict(zip(keys, row))

    def _load_evidence(self, claim_id: int) -> list[dict]:
        with self.database.lock:
            rows = self.connection.execute(
                """
                SELECT evidence.id, evidence.relationship, evidence.evidence_type,
                       evidence.evidence_json, evidence.observed_at,
                       evidence.fetched_at, evidence.snapshot_id,
                       evidence.article_id, evidence.source_url,
                       snapshot.observed_at, snapshot.fetched_at,
                       snapshot.market_status, snapshot.stale_after,
                       snapshot.quality_status, snapshot.payload_json,
                       snapshot.payload_sha256
                FROM financial_claim_evidence evidence
                LEFT JOIN financial_data_snapshots snapshot ON snapshot.id=evidence.snapshot_id
                WHERE evidence.claim_id=?
                ORDER BY evidence.id
                """,
                (int(claim_id),),
            ).fetchall()
        result = []
        for row in rows:
            metadata = _json_object(row[3])
            item = dict(metadata)
            item.update(
                {
                    "evidence_id": int(row[0]),
                    "relationship": str(row[1] or "supports"),
                    "evidence_type": str(row[2] or ""),
                    "snapshot_id": _integer(row[6]),
                    "article_id": _integer(row[7]),
                    "source_url": str(row[8] or ""),
                }
            )
            if row[6] is not None:
                payload_text = str(row[14] or "")
                item["integrity_valid"] = (
                    hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
                    == str(row[15] or "")
                )
                payload = _json_object(payload_text)
                normalized = _mapping(payload.get("normalized_payload"))
                lineage = _mapping(payload.get("lineage"))
                for key in (
                    "metric",
                    "period",
                    "revision",
                    "effective_from",
                    "effective_to",
                    "adjustment",
                    "event_type",
                    "affects_metrics",
                    "supersedes_claim_key",
                    "freshness_threshold_seconds",
                ):
                    if key not in item:
                        item[key] = payload.get(key, normalized.get(key, lineage.get(key)))
                item.update(
                    {
                        "observed_at": str(row[9] or row[4] or ""),
                        "fetched_at": str(row[10] or row[5] or ""),
                        "market_status": str(row[11] or "unknown"),
                        "stale_after": str(row[12] or ""),
                        "quality_status": str(row[13] or ""),
                    }
                )
            else:
                item["observed_at"] = str(row[4] or item.get("observed_at") or "")
                item["fetched_at"] = str(row[5] or item.get("fetched_at") or "")
            result.append(item)
        return result

    def _market_session(
        self,
        claim: Mapping[str, object],
        server_now: datetime,
    ) -> dict:
        calendar_id = str(claim.get("exchange") or "").upper()
        if calendar_id not in {"XSHG", "XSHE", "XHKG"}:
            return _market_payload(None)
        context = RequestTimeContext(
            server_now_utc=server_now,
            server_timezone="Asia/Hong_Kong",
            user_timezone="Asia/Hong_Kong",
        )
        return self.market_clock.market_state(calendar_id, context).to_dict()

    def judge_and_persist_claim(
        self,
        claim_id: int,
        *,
        server_now: datetime | None = None,
        requested_as_of: datetime | None = None,
        market_session: Mapping[str, object] | None = None,
    ) -> dict:
        claim = self._load_claim(int(claim_id))
        if claim is None:
            return {
                "status": "failed",
                "claim_id": int(claim_id),
                "error": "claim_not_found",
            }
        now = _require_aware(server_now or datetime.now(timezone.utc), "server_now")
        requested = _require_aware(requested_as_of, "requested_as_of") if requested_as_of else now
        request_context = {
            "server_now_utc": _utc_text(now),
            "requested_as_of": _utc_text(requested),
            "server_timezone": "Asia/Hong_Kong",
            "user_timezone": "Asia/Hong_Kong",
        }
        session = dict(market_session or self._market_session(claim, now))
        result = self.judge.judge(
            claim,
            self._load_evidence(int(claim_id)),
            request_context=request_context,
            market_session=session,
        )
        decision_payload = {
            "judge_version": result["judge_version"],
            "verdict": result["verdict"],
            "reason_codes": result["reason_codes"],
            "request_time": result["request_time"],
            "market_session": result["market_session"],
            "selected_evidence_ids": result["selected_evidence_ids"],
            "ignored_evidence_ids": result["ignored_evidence_ids"],
            "temporal_comparison": result["temporal_comparison"],
        }
        decision_hash = hashlib.sha256(_json(decision_payload).encode("utf-8")).hexdigest()
        rationale = _json({**decision_payload, "decision_hash": decision_hash})
        with self.database.lock:
            self.connection.execute("SAVEPOINT financial_temporal_judge")
            try:
                previous = self.connection.execute(
                    """
                    SELECT id, adjudication_version, rationale
                    FROM financial_verdicts
                    WHERE claim_id=? AND adjudicator=?
                    ORDER BY adjudication_version DESC LIMIT 1
                    """,
                    (int(claim_id), FINANCIAL_TEMPORAL_JUDGE_VERSION),
                ).fetchone()
                previous_payload = _json_object(previous[2]) if previous else {}
                if previous and previous_payload.get("decision_hash") == decision_hash:
                    verdict_id = int(previous[0])
                    version = int(previous[1])
                    persisted = False
                else:
                    version = int(
                        self.connection.execute(
                            "SELECT COALESCE(MAX(adjudication_version),0)+1 FROM financial_verdicts WHERE claim_id=?",
                            (int(claim_id),),
                        ).fetchone()[0]
                    )
                    cursor = self.connection.execute(
                        """
                        INSERT INTO financial_verdicts(
                            claim_id, adjudication_version, verdict, rationale,
                            selected_evidence_ids_json, conflicting_evidence_ids_json,
                            adjudicator, model_id, decided_at
                        ) VALUES(?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            int(claim_id),
                            version,
                            result["verdict"],
                            rationale,
                            _json(result["selected_evidence_ids"]),
                            "[]",
                            FINANCIAL_TEMPORAL_JUDGE_VERSION,
                            "",
                            _utc_text(now),
                        ),
                    )
                    verdict_id = int(cursor.lastrowid)
                    persisted = True
                if str(claim.get("claim_type") or "").casefold() == "fact":
                    self.connection.execute(
                        """
                        UPDATE financial_claims
                        SET verification_status=?, updated_at=?
                        WHERE id=?
                        """,
                        (result["verdict"], _utc_text(now), int(claim_id)),
                    )
                self.connection.execute("RELEASE SAVEPOINT financial_temporal_judge")
            except Exception:
                self.connection.execute("ROLLBACK TO SAVEPOINT financial_temporal_judge")
                self.connection.execute("RELEASE SAVEPOINT financial_temporal_judge")
                raise
        return {
            **result,
            "claim_id": int(claim_id),
            "verdict_id": verdict_id,
            "adjudication_version": version,
            "persisted": persisted,
        }


def validate_temporal_verdict(payload: Mapping[str, object]) -> dict:
    result = dict(payload)
    _VERDICT_VALIDATOR.validate(result)
    return result


__all__ = [
    "DEFAULT_FRESHNESS_SECONDS",
    "FINANCIAL_TEMPORAL_JUDGE_VERSION",
    "FINANCIAL_TEMPORAL_VERDICT_SCHEMA_VERSION",
    "FinancialTemporalJudge",
    "FinancialTemporalJudgeService",
    "TEMPORAL_VERDICTS",
    "TEMPORAL_VERDICT_SCHEMA",
    "validate_temporal_verdict",
]

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Versioned market/universe scopes for deterministic financial research."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

from financial_instruments import (
    DEFAULT_SEED_PATH as DEFAULT_INSTRUMENT_SEED_PATH,
    InstrumentRecord,
    InstrumentRegistry,
    normalize_alias,
    normalize_market,
)


DEFAULT_UNIVERSE_SEED_PATH = (
    Path(__file__).resolve().parent / "config" / "financial_universes.seed.json"
)
DEFAULT_UNIVERSE_KEYS = (
    "CN_XSHG_MARKET",
    "CN_XSHE_MARKET",
    "CN_A_MARKET",
    "HK_MARKET",
    "DEFAULT_MARKET_PULSE",
)
ALLOWED_ADMISSION_SOURCES = frozenset({"watchlist", "strategy", "user_request"})
_EXPLICIT_INSTRUMENT_CODE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"\d{6}(?:\.(?:SH|SZ|OF))?|"
    r"\d{1,5}\.HK|"
    r"[A-Z0-9]{1,12}\.(?:US|HK|SH|SZ|OF)"
    r")(?![A-Za-z0-9])",
    re.I,
)


def _iso_date(value, label: str, *, allow_empty: bool = False) -> str:
    if value in (None, ""):
        if allow_empty:
            return ""
        raise ValueError(f"{label} is required")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{label} datetime must be timezone-aware")
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO date") from exc


def _utc_text(value, label: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _json_object(value, label: str) -> Dict[str, object]:
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class UniverseRecord:
    universe_id: int
    universe_key: str
    display_name: str
    universe_type: str
    market: str
    source_provider_key: str
    constituent_as_of: str
    definition_version: str
    definition_effective_from: str
    definition_effective_to: str
    definition: Mapping[str, object]
    is_active: bool

    def to_dict(self) -> Dict[str, object]:
        return {
            "universe_id": self.universe_id,
            "universe_key": self.universe_key,
            "display_name": self.display_name,
            "universe_type": self.universe_type,
            "market": self.market,
            "source_provider_key": self.source_provider_key,
            "constituent_as_of": self.constituent_as_of or None,
            "definition_version": self.definition_version,
            "definition_effective_from": self.definition_effective_from,
            "definition_effective_to": self.definition_effective_to or None,
            "definition": dict(self.definition),
            "is_active": self.is_active,
        }


@dataclass(frozen=True)
class UniverseMember:
    instrument: InstrumentRecord
    weight: Optional[float]
    effective_from: str
    effective_to: str
    source_observed_at: str
    metadata: Mapping[str, object]

    @property
    def priority(self) -> int:
        try:
            return int(self.metadata.get("priority", 1000))
        except (TypeError, ValueError):
            return 1000

    def to_dict(self) -> Dict[str, object]:
        return {
            "instrument": self.instrument.to_dict(),
            "weight": self.weight,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to or None,
            "source_observed_at": self.source_observed_at or None,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class UniverseRoute:
    status: str
    universe_key: str = ""
    reason: str = ""
    requires_clarification: bool = False
    clarification_fields: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, object]:
        return {
            "status": self.status,
            "universe_key": self.universe_key or None,
            "reason": self.reason,
            "requires_clarification": self.requires_clarification,
            "clarification_fields": list(self.clarification_fields),
        }


@dataclass(frozen=True)
class CandidateAdmission:
    instrument_id: int
    admission_source: str
    decision: str
    reason: str


@dataclass(frozen=True)
class UniverseResearchPlan:
    plan_id: str
    universe: UniverseRecord
    as_of: str
    status: str
    selected_members: Tuple[UniverseMember, ...]
    omitted_members: Tuple[UniverseMember, ...]
    required_market_metrics: Tuple[str, ...]
    available_market_metrics: Tuple[str, ...]
    covered_instrument_ids: Tuple[int, ...]
    uncovered_items: Tuple[Mapping[str, object], ...]
    coverage_status: str
    currency_groups: Mapping[str, Tuple[int, ...]]
    sampling_policy: str
    member_budget: int
    requires_scope_clarification: bool = False

    def to_dict(self) -> Dict[str, object]:
        selected_count = len(self.selected_members)
        covered_count = len(self.covered_instrument_ids)
        coverage_ratio = (
            round(covered_count / selected_count, 6)
            if selected_count and self.coverage_status == "evaluated"
            else None
        )
        return {
            "plan_id": self.plan_id,
            "status": self.status,
            "as_of": self.as_of,
            "universe": self.universe.to_dict(),
            "constituent_basis": self.universe.definition.get("constituent_basis", ""),
            "scope_basis": self.universe.definition.get("scope_basis", ""),
            "sampling": {
                "policy": self.sampling_policy,
                "member_budget": self.member_budget,
                "total_member_count": selected_count + len(self.omitted_members),
                "selected_member_count": selected_count,
                "omitted_member_count": len(self.omitted_members),
                "sampled": bool(self.omitted_members),
            },
            "coverage": {
                "status": self.coverage_status,
                "covered_count": covered_count if self.coverage_status == "evaluated" else None,
                "coverage_ratio": coverage_ratio,
                "uncovered_items": [dict(item) for item in self.uncovered_items],
            },
            "required_market_metrics": list(self.required_market_metrics),
            "available_market_metrics": list(self.available_market_metrics),
            "currency_groups": {
                currency: list(instrument_ids)
                for currency, instrument_ids in self.currency_groups.items()
            },
            "selected_members": [member.to_dict() for member in self.selected_members],
            "omitted_members": [member.to_dict() for member in self.omitted_members],
            "requires_scope_clarification": self.requires_scope_clarification,
        }


class FinancialUniversePlanner:
    def __init__(self, connection, *, instrument_registry: Optional[InstrumentRegistry] = None):
        self.connection = connection
        self.instruments = instrument_registry or InstrumentRegistry(connection)
        self._assert_schema()

    def _assert_schema(self) -> None:
        required = {"financial_universes", "financial_universe_members"}
        present = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = sorted(required - present)
        if missing:
            raise RuntimeError(f"financial schema is not initialized: {', '.join(missing)}")

    @contextmanager
    def _atomic(self):
        self.connection.execute("SAVEPOINT financial_universe_write")
        try:
            yield
            self.connection.execute("RELEASE SAVEPOINT financial_universe_write")
        except Exception:
            self.connection.execute("ROLLBACK TO SAVEPOINT financial_universe_write")
            self.connection.execute("RELEASE SAVEPOINT financial_universe_write")
            raise

    @staticmethod
    def _definition_envelope(raw) -> Dict[str, object]:
        value = _json_object(raw, "definition_json")
        if value.get("schema_version") == 1 and isinstance(value.get("versions"), list):
            return value
        # Compatibility for a pre-versioned definition written by an early build.
        return {
            "schema_version": 1,
            "versions": [
                {
                    "definition_version": "legacy-v0",
                    "effective_from": "",
                    "effective_to": None,
                    "definition": value,
                }
            ],
        }

    @staticmethod
    def _select_definition(envelope: Mapping[str, object], as_of: str) -> Mapping[str, object]:
        matches = []
        for version in envelope.get("versions") or []:
            start = str(version.get("effective_from") or "")
            end = str(version.get("effective_to") or "")
            if (not start or start <= as_of) and (not end or end > as_of):
                matches.append(version)
        if len(matches) != 1:
            raise LookupError(f"no unique universe definition is effective at {as_of}")
        return matches[0]

    @staticmethod
    def _validate_market_definition(universe_type: str, definition: Mapping[str, object]) -> None:
        if universe_type not in {"market", "composite_market"}:
            return
        metrics = definition.get("required_market_metrics")
        if not isinstance(metrics, list) or not metrics:
            raise ValueError("market universe requires market breadth metrics")
        if definition.get("must_not_represent_market_with_single_equity") is not True:
            raise ValueError("market universe must reject single-equity representation")

    def upsert_universe(
        self,
        *,
        universe_key: str,
        display_name: str,
        universe_type: str,
        market: str,
        definition_version: str,
        definition: Mapping[str, object],
        effective_from,
        source_provider_key: str,
        is_active: bool = True,
    ) -> UniverseRecord:
        key = str(universe_key or "").strip().upper()
        name = str(display_name or "").strip()
        kind = str(universe_type or "").strip().lower()
        version = str(definition_version or "").strip()
        source = str(source_provider_key or "").strip()
        effective = _iso_date(effective_from, "effective_from")
        definition_object = _json_object(definition, "definition")
        if not all((key, name, kind, version, source)):
            raise ValueError(
                "universe_key, display_name, universe_type, definition_version, and source are required"
            )
        self._validate_market_definition(kind, definition_object)

        with self._atomic():
            row = self.connection.execute(
                "SELECT id, definition_json FROM financial_universes WHERE universe_key=?",
                (key,),
            ).fetchone()
            new_version = {
                "definition_version": version,
                "effective_from": effective,
                "effective_to": None,
                "definition": definition_object,
            }
            if row:
                universe_id = int(row[0])
                envelope = self._definition_envelope(row[1])
                versions = list(envelope.get("versions") or [])
                constituent_snapshots = list(
                    envelope.get("constituent_snapshots") or []
                )
                same_date = [
                    item
                    for item in versions
                    if str(item.get("effective_from") or "") == effective
                ]
                if same_date:
                    if _canonical_json(same_date[0]) != _canonical_json(new_version):
                        raise ValueError("a universe definition date is immutable")
                else:
                    latest = max(
                        (str(item.get("effective_from") or "") for item in versions),
                        default="",
                    )
                    if latest and effective <= latest:
                        raise ValueError("definition versions must be appended chronologically")
                    for item in versions:
                        if not item.get("effective_to"):
                            item["effective_to"] = effective
                    versions.append(new_version)
                envelope = {"schema_version": 1, "versions": versions}
                if constituent_snapshots:
                    envelope["constituent_snapshots"] = constituent_snapshots
                self.connection.execute(
                    """
                    UPDATE financial_universes
                    SET display_name=?, universe_type=?, market=?, definition_json=?,
                        source_provider_key=?, is_active=?,
                        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE id=?
                    """,
                    (
                        name,
                        kind,
                        normalize_market(market),
                        _canonical_json(envelope),
                        source,
                        int(bool(is_active)),
                        universe_id,
                    ),
                )
            else:
                envelope = {"schema_version": 1, "versions": [new_version]}
                self.connection.execute(
                    """
                    INSERT INTO financial_universes(
                        universe_key, display_name, universe_type, market,
                        definition_json, source_provider_key, is_active
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        name,
                        kind,
                        normalize_market(market),
                        _canonical_json(envelope),
                        source,
                        int(bool(is_active)),
                    ),
                )
                universe_id = int(
                    self.connection.execute("SELECT last_insert_rowid()").fetchone()[0]
                )
        return self.get_universe(key, as_of=effective)

    def get_universe(self, universe_key: str, *, as_of) -> UniverseRecord:
        key = str(universe_key or "").strip().upper()
        effective_date = _iso_date(as_of, "as_of")
        row = self.connection.execute(
            """
            SELECT id, universe_key, display_name, universe_type, market,
                   definition_json, source_provider_key, constituent_as_of, is_active
            FROM financial_universes WHERE universe_key=?
            """,
            (key,),
        ).fetchone()
        if not row:
            raise LookupError(f"unknown universe: {key}")
        envelope = self._definition_envelope(row[5])
        selected = self._select_definition(envelope, effective_date)
        snapshots = [
            item
            for item in envelope.get("constituent_snapshots") or []
            if str(item.get("effective_from") or "") <= effective_date
        ]
        selected_snapshot = max(
            snapshots,
            key=lambda item: str(item.get("effective_from") or ""),
            default=None,
        )
        fallback_constituent_as_of = str(row[7] or "")
        if fallback_constituent_as_of > effective_date:
            fallback_constituent_as_of = ""
        return UniverseRecord(
            universe_id=int(row[0]),
            universe_key=str(row[1]),
            display_name=str(row[2]),
            universe_type=str(row[3]),
            market=str(row[4]),
            source_provider_key=str(
                (selected_snapshot or {}).get("source_provider_key") or row[6]
            ),
            constituent_as_of=str(
                (selected_snapshot or {}).get("effective_from")
                or fallback_constituent_as_of
            ),
            definition_version=str(selected.get("definition_version") or ""),
            definition_effective_from=str(selected.get("effective_from") or ""),
            definition_effective_to=str(selected.get("effective_to") or ""),
            definition=_json_object(selected.get("definition"), "definition"),
            is_active=bool(row[8]),
        )

    def _member_payload(self, value: Mapping[str, object]) -> Dict[str, object]:
        if not isinstance(value, Mapping):
            raise ValueError("universe member must be an object")
        instrument_id = value.get("instrument_id")
        if instrument_id is None:
            canonical = str(value.get("canonical_symbol") or "").strip()
            if not canonical:
                raise ValueError("member instrument_id or canonical_symbol is required")
            instrument = self.instruments.get_by_canonical_symbol(canonical)
            if instrument is None:
                raise LookupError(f"unknown member instrument: {canonical}")
            instrument_id = instrument.instrument_id
        instrument = self.instruments.get(int(instrument_id))
        if instrument is None:
            raise LookupError(f"unknown member instrument_id: {instrument_id}")
        weight = value.get("weight")
        if weight is not None:
            weight = float(weight)
            if weight < 0:
                raise ValueError("member weight cannot be negative")
        metadata = _json_object(value.get("metadata"), "member metadata")
        for key in ("role", "priority", "admission_source"):
            if key in value:
                metadata[key] = value[key]
        return {"instrument": instrument, "weight": weight, "metadata": metadata}

    def sync_members(
        self,
        universe_key: str,
        members: Iterable[Mapping[str, object]],
        *,
        effective_from,
        source_observed_at: datetime,
        source_provider_key: str,
    ) -> Tuple[UniverseMember, ...]:
        effective = _iso_date(effective_from, "effective_from")
        observed = _utc_text(source_observed_at, "source_observed_at")
        source = str(source_provider_key or "").strip()
        if not source:
            raise ValueError("source_provider_key is required")
        universe = self.get_universe(universe_key, as_of=effective)
        normalized_members = [self._member_payload(item) for item in members]
        instrument_ids = [item["instrument"].instrument_id for item in normalized_members]
        if len(instrument_ids) != len(set(instrument_ids)):
            raise ValueError("universe snapshot contains duplicate instrument_id")
        latest_row = self.connection.execute(
            "SELECT constituent_as_of FROM financial_universes WHERE id=?",
            (universe.universe_id,),
        ).fetchone()
        latest = str((latest_row or [""])[0] or "")

        with self._atomic():
            if latest and effective < latest:
                raise ValueError("constituent snapshots must be appended chronologically")
            if latest == effective:
                current = self.members_as_of(universe_key, as_of=effective)
                current_signature = sorted(
                    (
                        item.instrument.instrument_id,
                        item.weight,
                        _canonical_json(item.metadata),
                    )
                    for item in current
                )
                incoming_signature = sorted(
                    (
                        item["instrument"].instrument_id,
                        item["weight"],
                        _canonical_json(item["metadata"]),
                    )
                    for item in normalized_members
                )
                if current_signature != incoming_signature:
                    raise ValueError("a constituent snapshot date is immutable")
                return current

            self.connection.execute(
                """
                UPDATE financial_universe_members
                SET effective_to=?
                WHERE universe_id=? AND effective_to IS NULL AND effective_from < ?
                """,
                (effective, universe.universe_id, effective),
            )
            for item in normalized_members:
                self.connection.execute(
                    """
                    INSERT INTO financial_universe_members(
                        universe_id, instrument_id, weight, effective_from,
                        source_observed_at, metadata_json
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        universe.universe_id,
                        item["instrument"].instrument_id,
                        item["weight"],
                        effective,
                        observed,
                        _canonical_json(item["metadata"]),
                    ),
                )
            envelope_row = self.connection.execute(
                "SELECT definition_json FROM financial_universes WHERE id=?",
                (universe.universe_id,),
            ).fetchone()
            envelope = self._definition_envelope(envelope_row[0])
            snapshots = list(envelope.get("constituent_snapshots") or [])
            snapshot_signature = [
                {
                    "instrument_id": item["instrument"].instrument_id,
                    "weight": item["weight"],
                    "metadata": item["metadata"],
                }
                for item in normalized_members
            ]
            snapshots.append(
                {
                    "effective_from": effective,
                    "source_provider_key": source,
                    "source_observed_at": observed,
                    "member_count": len(normalized_members),
                    "snapshot_sha256": hashlib.sha256(
                        _canonical_json(snapshot_signature).encode("utf-8")
                    ).hexdigest(),
                }
            )
            envelope["constituent_snapshots"] = snapshots
            self.connection.execute(
                """
                UPDATE financial_universes
                SET constituent_as_of=?, source_provider_key=?, definition_json=?,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id=?
                """,
                (effective, source, _canonical_json(envelope), universe.universe_id),
            )
        return self.members_as_of(universe_key, as_of=effective)

    def members_as_of(self, universe_key: str, *, as_of) -> Tuple[UniverseMember, ...]:
        effective = _iso_date(as_of, "as_of")
        universe = self.get_universe(universe_key, as_of=effective)
        rows = self.connection.execute(
            """
            SELECT instrument_id, weight, effective_from, effective_to,
                   source_observed_at, metadata_json
            FROM financial_universe_members
            WHERE universe_id=? AND effective_from<=?
              AND (effective_to IS NULL OR effective_to>?)
            """,
            (universe.universe_id, effective, effective),
        ).fetchall()
        result = []
        for row in rows:
            instrument = self.instruments.get(int(row[0]))
            if instrument is None:
                raise RuntimeError(f"universe references missing instrument_id: {row[0]}")
            result.append(
                UniverseMember(
                    instrument=instrument,
                    weight=float(row[1]) if row[1] is not None else None,
                    effective_from=str(row[2]),
                    effective_to=str(row[3] or ""),
                    source_observed_at=str(row[4] or ""),
                    metadata=_json_object(row[5], "member metadata_json"),
                )
            )
        result.sort(
            key=lambda item: (
                item.priority,
                -(item.weight if item.weight is not None else -1),
                item.instrument.canonical_symbol,
            )
        )
        return tuple(result)

    def load_controlled_seed(
        self,
        path: Path | str = DEFAULT_UNIVERSE_SEED_PATH,
        *,
        instrument_seed_path: Path | str = DEFAULT_INSTRUMENT_SEED_PATH,
    ) -> Tuple[UniverseRecord, ...]:
        self.instruments.load_controlled_seed(instrument_seed_path)
        seed = json.loads(Path(path).read_text(encoding="utf-8"))
        if seed.get("schema_version") != 1 or not seed.get("seed_version"):
            raise ValueError("unsupported universe seed schema")
        effective = _iso_date(seed.get("effective_from"), "effective_from")
        source = str(seed.get("source_provider_key") or "").strip()
        observed = datetime.combine(
            date.fromisoformat(effective), datetime.min.time(), tzinfo=timezone.utc
        )
        records = []
        for original in seed.get("universes") or []:
            item = dict(original)
            members = item.pop("members", [])
            record = self.upsert_universe(
                **item,
                effective_from=effective,
                source_provider_key=source,
            )
            self.sync_members(
                record.universe_key,
                members,
                effective_from=effective,
                source_observed_at=observed,
                source_provider_key=source,
            )
            records.append(record)
        return tuple(records)

    @staticmethod
    def resolve_expression(expression: str) -> UniverseRoute:
        text = str(expression or "").strip()
        compact = normalize_alias(text)
        # 显式证券代码优先于“港股/A股”等市场词，避免 9969.HK 被误当成
        # 整个香港市场。这里仅声明需要走标的解析，不猜测证券身份。
        if _EXPLICIT_INSTRUMENT_CODE.search(text):
            return UniverseRoute(
                "instrument_resolution_required",
                reason="explicit_or_ambiguous_instrument_code",
                requires_clarification=True,
                clarification_fields=("instrument",),
            )
        selection_terms = ("选一只股票", "选只股票", "推荐股票", "选一只基金", "推荐基金")
        if any(term in compact for term in selection_terms):
            return UniverseRoute(
                "resolved",
                "DEFAULT_MARKET_PULSE",
                "selection_request_uses_market_pulse_before_scope_clarification",
                True,
                ("market", "risk", "investment_scope"),
            )
        if any(
            term in compact
            for term in (
                "港股", "港市", "香港股市", "香港市场", "香港市場",
                "香港大盘", "香港大盤", "香港行情",
            )
        ):
            return UniverseRoute("resolved", "HK_MARKET", "explicit_hong_kong_market")
        if any(
            term in compact
            for term in (
                "上证大盘", "上證大盤", "上海大盘", "上海大盤",
                "上交所市场", "上交所市場", "上海股市", "沪市", "滬市",
            )
        ) or re.search(
            r"(?:上证|上證)(?!指数|指數|综指|綜指)"
            r"(?:今天|今日|现在|現在|怎么样|怎麼樣|如何|行情|大盘|大盤|市场|市場|$)",
            compact,
        ):
            return UniverseRoute("resolved", "CN_XSHG_MARKET", "explicit_xshg_market")
        if any(
            term in compact
            for term in (
                "深证大盘", "深證大盤", "深圳大盘", "深圳大盤",
                "深交所市场", "深交所市場", "深圳股市", "深市",
            )
        ) or re.search(
            r"(?:深证|深證)(?!指数|指數|成指)"
            r"(?:今天|今日|现在|現在|怎么样|怎麼樣|如何|行情|大盘|大盤|市场|市場|$)",
            compact,
        ):
            return UniverseRoute("resolved", "CN_XSHE_MARKET", "explicit_xshe_market")
        if any(
            term in compact
            for term in (
                "a股", "ａ股", "中国股市", "中國股市", "内地股市", "內地股市",
                "沪深股市", "滬深股市", "沪深两市", "滬深兩市",
            )
        ):
            return UniverseRoute("resolved", "CN_A_MARKET", "explicit_a_share_market")
        if any(
            term in compact
            for term in (
                "大盘", "大盤", "市场怎么样", "市場怎麼樣", "市场如何",
                "市場如何", "今天市场", "今日市场", "今天市場", "今日市場",
                "股市怎么样", "股市怎麼樣", "股市如何", "现在股市", "現在股市",
            )
        ):
            return UniverseRoute(
                "resolved", "DEFAULT_MARKET_PULSE", "unspecified_market_uses_default_scope"
            )
        return UniverseRoute("not_applicable", reason="no_default_market_scope_match")

    @staticmethod
    def evaluate_candidate(instrument_id: int, *, admission_source: str) -> CandidateAdmission:
        source = str(admission_source or "").strip().lower()
        if source in ALLOWED_ADMISSION_SOURCES:
            return CandidateAdmission(
                int(instrument_id), source, "eligible", f"explicit_scope_source:{source}"
            )
        return CandidateAdmission(
            int(instrument_id),
            source or "auto_detected",
            "candidate_only",
            "automatic_entity_detection_cannot_expand_research_scope",
        )

    def build_plan(
        self,
        universe_key: str,
        *,
        as_of,
        member_budget: int,
        available_instrument_ids: Optional[Iterable[int]] = None,
        available_market_metrics: Optional[Iterable[str]] = None,
        requires_scope_clarification: bool = False,
    ) -> UniverseResearchPlan:
        effective = _iso_date(as_of, "as_of")
        budget = int(member_budget)
        if budget <= 0:
            raise ValueError("member_budget must be positive")
        universe = self.get_universe(universe_key, as_of=effective)
        members = self.members_as_of(universe_key, as_of=effective)
        selected = members[:budget]
        omitted = members[budget:]
        required_metrics = tuple(
            str(value) for value in universe.definition.get("required_market_metrics") or []
        )
        available_metrics = tuple(sorted(set(available_market_metrics or [])))
        available_ids = (
            {int(value) for value in available_instrument_ids}
            if available_instrument_ids is not None
            else None
        )
        covered_ids = tuple(
            member.instrument.instrument_id
            for member in selected
            if available_ids is not None and member.instrument.instrument_id in available_ids
        )
        uncovered = [
            {
                "item_type": "instrument",
                "instrument_id": member.instrument.instrument_id,
                "canonical_symbol": member.instrument.canonical_symbol,
                "reason": "member_budget_truncated",
            }
            for member in omitted
        ]
        if available_ids is not None:
            uncovered.extend(
                {
                    "item_type": "instrument",
                    "instrument_id": member.instrument.instrument_id,
                    "canonical_symbol": member.instrument.canonical_symbol,
                    "reason": "provider_coverage_missing",
                }
                for member in selected
                if member.instrument.instrument_id not in available_ids
            )
        if available_market_metrics is not None:
            uncovered.extend(
                {
                    "item_type": "market_metric",
                    "metric": metric,
                    "reason": "provider_coverage_missing",
                }
                for metric in required_metrics
                if metric not in set(available_metrics)
            )
        currency_groups: Dict[str, list[int]] = {}
        for member in selected:
            currency_groups.setdefault(member.instrument.currency or "UNKNOWN", []).append(
                member.instrument.instrument_id
            )
        frozen_currency_groups = {
            key: tuple(value) for key, value in sorted(currency_groups.items())
        }
        identity = {
            "universe_key": universe.universe_key,
            "definition_version": universe.definition_version,
            "constituent_as_of": universe.constituent_as_of,
            "as_of": effective,
            "budget": budget,
            "members": [member.instrument.instrument_id for member in selected],
        }
        plan_id = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
        status = "empty" if not members else "ready"
        coverage_status = (
            "evaluated"
            if available_ids is not None or available_market_metrics is not None
            else "not_evaluated"
        )
        return UniverseResearchPlan(
            plan_id=plan_id,
            universe=universe,
            as_of=effective,
            status=status,
            selected_members=tuple(selected),
            omitted_members=tuple(omitted),
            required_market_metrics=required_metrics,
            available_market_metrics=available_metrics,
            covered_instrument_ids=covered_ids,
            uncovered_items=tuple(uncovered),
            coverage_status=coverage_status,
            currency_groups=frozen_currency_groups,
            sampling_policy=str(
                universe.definition.get("sampling_policy") or "priority_then_symbol"
            ),
            member_budget=budget,
            requires_scope_clarification=bool(requires_scope_clarification),
        )

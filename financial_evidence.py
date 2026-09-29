#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Resolve existing market snapshots and articles into research-run evidence.

The resolver only creates references.  Provider payloads stay in
``financial_data_snapshots`` and original documents stay in ``articles``.  The
two read pipelines are deliberately isolated so one can fail or recover without
discarding the other pipeline's last valid associations.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from intel_sources import canonicalize_source_url
from term_matching import normalize_text, term_occurs_in_normalized


STRUCTURED_KIND = "structured_snapshot"
DOCUMENT_KIND = "source_document"
UTC = timezone.utc


def _utc_text(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("time range values must be timezone-aware datetimes")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_stored_datetime(value: object) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _article_time_interval(row: Mapping[str, object]) -> Optional[Tuple[datetime, datetime, str]]:
    """Return an inclusive UTC interval without inventing sub-day publication time."""

    published_at = str(row.get("published_at_utc") or "").strip()
    precision = str(row.get("published_precision") or "").strip().casefold()
    if published_at:
        try:
            if precision in {"date", "day"} or re.fullmatch(r"\d{4}-\d{2}-\d{2}", published_at):
                day = date.fromisoformat(published_at[:10])
                timezone_name = str(row.get("published_timezone") or "UTC").strip() or "UTC"
                try:
                    local_zone = ZoneInfo(timezone_name)
                except ZoneInfoNotFoundError:
                    local_zone = UTC
                start = datetime.combine(day, time.min, tzinfo=local_zone).astimezone(UTC)
                end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=local_zone).astimezone(UTC)
                return start, end - timedelta(microseconds=1), "published_at_date"
            instant = _parse_stored_datetime(published_at)
            if instant is not None:
                return instant, instant, "published_at_utc"
        except ValueError:
            pass

    published = str(row.get("publish_date") or "").strip()
    if published:
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", published):
                day = date.fromisoformat(published)
                start = datetime.combine(day, time.min, tzinfo=UTC)
                return start, start + timedelta(days=1) - timedelta(microseconds=1), "publish_date_day"
            instant = _parse_stored_datetime(published)
            if instant is not None:
                return instant, instant, "publish_date"
        except ValueError:
            pass
    for field in ("first_crawled", "created_at", "updated_at"):
        instant = _parse_stored_datetime(row.get(field))
        if instant is not None:
            return instant, instant, field
    return None


def _normalize_text(value: object) -> str:
    return normalize_text(value)


def _safe_json_object(value: object) -> Dict[str, object]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value)) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _canonical_document_url(row: Mapping[str, object]) -> str:
    raw = str(row.get("canonical_url") or row.get("url") or "").strip()
    try:
        return canonicalize_source_url(raw)
    except (TypeError, ValueError):
        return raw.casefold()


def _term_occurs(term: str, haystack: str) -> bool:
    """Whole-word for Latin identifier terms, substring otherwise.

    Delegates to ``term_matching`` so the project keyword gate and the article
    keyword filter cannot drift apart; ``haystack`` is expected to be
    normalised already by the caller.
    """

    return term_occurs_in_normalized(normalize_text(term), haystack)


@dataclass(frozen=True)
class EvidenceItem:
    evidence_key: str
    evidence_kind: str
    reference_id: int
    instrument_id: Optional[int]
    universe_id: Optional[int]
    source_key: str
    source_url: str
    source_title: str
    observed_at: str
    fetched_at: str
    evidence_role: str
    match_method: str
    match_score: float
    match_terms: Tuple[str, ...]
    claim_capabilities: Tuple[str, ...]
    denied_claim_capabilities: Tuple[str, ...]
    payload: object = None
    content: str = ""

    def persistence_metadata(self) -> Dict[str, object]:
        return {
            "source_key": self.source_key,
            "canonical_source_url": self.source_url,
            "match_terms": list(self.match_terms),
            "claim_capabilities": list(self.claim_capabilities),
            "denied_claim_capabilities": list(self.denied_claim_capabilities),
            "content_copied": False,
            "payload_copied": False,
        }

    def to_dict(self) -> Dict[str, object]:
        return {
            "evidence_key": self.evidence_key,
            "evidence_kind": self.evidence_kind,
            "reference_id": self.reference_id,
            "instrument_id": self.instrument_id,
            "universe_id": self.universe_id,
            "source_key": self.source_key,
            "source_url": self.source_url,
            "source_title": self.source_title,
            "observed_at": self.observed_at,
            "fetched_at": self.fetched_at,
            "evidence_role": self.evidence_role,
            "match_method": self.match_method,
            "match_score": self.match_score,
            "match_terms": list(self.match_terms),
            "claim_capabilities": list(self.claim_capabilities),
            "denied_claim_capabilities": list(self.denied_claim_capabilities),
            "payload": self.payload,
            "content": self.content,
        }


class EvidenceResolver:
    """Fuse references for one existing research run without merging stores."""

    def __init__(self, connection, *, clock=None):
        self.connection = connection
        self.clock = clock or (lambda: datetime.now(UTC))
        self._assert_schema()

    def _assert_schema(self) -> None:
        required = {
            "articles",
            "financial_instruments",
            "financial_data_snapshots",
            "financial_research_runs",
            "financial_research_evidence",
        }
        present = {
            str(row[0])
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = sorted(required - present)
        if missing:
            raise RuntimeError(f"financial evidence schema is not initialized: {', '.join(missing)}")

    def _load_run(self, run_id: str) -> Dict[str, object]:
        row = self.connection.execute(
            """
            SELECT id, scope_type, instrument_id, universe_id, requested_at,
                   time_context_json
            FROM financial_research_runs WHERE id=?
            """,
            (str(run_id),),
        ).fetchone()
        if row is None:
            raise LookupError(f"financial research run not found: {run_id}")
        return dict(row)

    def _target_ids(
        self,
        run: Mapping[str, object],
        start_at: datetime,
        end_at: datetime,
        explicit_ids: Optional[Sequence[int]],
    ) -> Tuple[Tuple[int, ...], Optional[int]]:
        if run.get("instrument_id") is not None:
            authorized_ids = (int(run["instrument_id"]),)
        elif run.get("universe_id") is not None:
            rows = self.connection.execute(
                """
                SELECT instrument_id
                FROM financial_universe_members
                WHERE universe_id=?
                  AND (effective_from='' OR date(effective_from) <= date(?))
                  AND (effective_to IS NULL OR date(effective_to) >= date(?))
                ORDER BY instrument_id
                """,
                (int(run["universe_id"]), _utc_text(end_at), _utc_text(start_at)),
            ).fetchall()
            authorized_ids = tuple(int(row[0]) for row in rows)
        else:
            authorized_ids = ()
        if explicit_ids is not None:
            ids = tuple(sorted({int(value) for value in explicit_ids}))
            unauthorized = sorted(set(ids) - set(authorized_ids))
            if unauthorized and str(run.get("scope_type") or "") != "market":
                raise ValueError(
                    "explicit instrument_ids cannot expand the research run scope: "
                    f"{unauthorized}"
                )
        else:
            ids = authorized_ids
        if ids:
            placeholders = ",".join("?" for _ in ids)
            found = {
                int(row[0])
                for row in self.connection.execute(
                    f"SELECT id FROM financial_instruments WHERE id IN ({placeholders})",
                    ids,
                ).fetchall()
            }
            missing = sorted(set(ids) - found)
            if missing:
                raise LookupError(f"financial instruments not found: {missing}")
        universe_id = int(run["universe_id"]) if run.get("universe_id") is not None else None
        return ids, universe_id

    def _entity_terms(
        self,
        instrument_ids: Sequence[int],
        start_at: datetime,
        end_at: datetime,
        supplied: Optional[Mapping[int, Sequence[str]]],
    ) -> Dict[int, Tuple[str, ...]]:
        terms: Dict[int, set] = {instrument_id: set() for instrument_id in instrument_ids}
        if not instrument_ids:
            return {}
        placeholders = ",".join("?" for _ in instrument_ids)
        rows = self.connection.execute(
            f"""
            SELECT id, canonical_symbol, display_name, provider_mappings_json
            FROM financial_instruments WHERE id IN ({placeholders})
            """,
            tuple(instrument_ids),
        ).fetchall()
        for row in rows:
            instrument_id = int(row[0])
            terms[instrument_id].update((str(row[1]), str(row[2])))
            mappings = _safe_json_object(row[3])
            terms[instrument_id].update(str(value) for value in mappings.values() if value)
        alias_rows = self.connection.execute(
            f"""
            SELECT instrument_id, alias
            FROM financial_instrument_aliases
            WHERE instrument_id IN ({placeholders})
              AND (valid_from='' OR date(valid_from) <= date(?))
              AND (valid_to IS NULL OR date(valid_to) >= date(?))
            """,
            tuple(instrument_ids) + (_utc_text(end_at), _utc_text(start_at)),
        ).fetchall()
        for row in alias_rows:
            terms[int(row[0])].add(str(row[1]))
        for raw_id, supplied_terms in (supplied or {}).items():
            instrument_id = int(raw_id)
            if instrument_id in terms:
                terms[instrument_id].update(str(term) for term in supplied_terms if str(term).strip())
        return {
            instrument_id: tuple(sorted(values, key=lambda value: (-len(value), value)))
            for instrument_id, values in terms.items()
        }

    def _query_snapshots(
        self,
        instrument_ids: Sequence[int],
        universe_id: Optional[int],
    ) -> List[Mapping[str, object]]:
        clauses: List[str] = []
        parameters: List[object] = []
        if instrument_ids:
            placeholders = ",".join("?" for _ in instrument_ids)
            clauses.append(f"s.instrument_id IN ({placeholders})")
            parameters.extend(instrument_ids)
        if universe_id is not None:
            clauses.append("s.universe_id=?")
            parameters.append(universe_id)
        if not clauses:
            return []
        return [
            dict(row)
            for row in self.connection.execute(
                f"""
                SELECT s.id, s.instrument_id, s.universe_id, s.data_type,
                       s.observed_at, s.fetched_at, s.source_url, s.payload_json,
                       s.quality_status, p.provider_key
                FROM financial_data_snapshots s
                JOIN financial_provider_profiles p ON p.id=s.provider_profile_id
                WHERE ({' OR '.join(clauses)})
                ORDER BY s.observed_at DESC, s.id DESC
                """,
                tuple(parameters),
            ).fetchall()
        ]

    def _query_articles(self) -> List[Mapping[str, object]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT id, url, canonical_url, title, content, domain,
                       publish_date, published_at_utc, published_timezone,
                       published_precision, published_time_source,
                       first_crawled, created_at, updated_at,
                       matched_keywords, source_task_name
                FROM articles
                WHERE status='active'
                ORDER BY COALESCE(publish_date, first_crawled, created_at) DESC, id DESC
                """
            ).fetchall()
        ]

    @staticmethod
    def _source_allowed(source_key: str, source_keys: Optional[Iterable[str]]) -> bool:
        if source_keys is None:
            return True
        allowed = {_normalize_text(value).strip() for value in source_keys if str(value).strip()}
        return not allowed or _normalize_text(source_key).strip() in allowed

    def _resolve_snapshots(
        self,
        instrument_ids: Sequence[int],
        universe_id: Optional[int],
        start_at: datetime,
        end_at: datetime,
        source_keys: Optional[Iterable[str]],
    ) -> Tuple[List[EvidenceItem], int]:
        items: List[EvidenceItem] = []
        invalid_timestamps = 0
        for row in self._query_snapshots(instrument_ids, universe_id):
            observed = _parse_stored_datetime(row.get("observed_at"))
            fetched = _parse_stored_datetime(row.get("fetched_at"))
            if observed is None or fetched is None:
                invalid_timestamps += 1
                continue
            if observed < start_at or observed > end_at:
                continue
            provider = str(row.get("provider_key") or "")
            if not self._source_allowed(provider, source_keys):
                continue
            instrument_id = int(row["instrument_id"]) if row.get("instrument_id") is not None else None
            target = f"instrument:{instrument_id}" if instrument_id is not None else f"universe:{universe_id}"
            try:
                payload = json.loads(str(row.get("payload_json") or "{}"))
            except (TypeError, ValueError):
                payload = {"decode_error": True}
            items.append(
                EvidenceItem(
                    evidence_key=f"snapshot:{int(row['id'])}:{target}",
                    evidence_kind=STRUCTURED_KIND,
                    reference_id=int(row["id"]),
                    instrument_id=instrument_id,
                    universe_id=int(row["universe_id"]) if row.get("universe_id") is not None else universe_id,
                    source_key=provider,
                    source_url=str(row.get("source_url") or ""),
                    source_title=str(row.get("data_type") or "structured market snapshot"),
                    observed_at=_utc_text(observed),
                    fetched_at=_utc_text(fetched),
                    evidence_role="structured_market_fact",
                    match_method="direct_instrument" if instrument_id is not None else "direct_universe",
                    match_score=1.0,
                    match_terms=(),
                    claim_capabilities=("structured_market_metric", "quoted_value", "time_series_observation"),
                    denied_claim_capabilities=("announcement_text", "issuer_statement", "document_sentiment"),
                    payload=payload,
                )
            )
        return items, invalid_timestamps

    @staticmethod
    def _article_match(
        row: Mapping[str, object], terms: Sequence[str]
    ) -> Optional[Tuple[str, float, Tuple[str, ...]]]:
        fields = (
            ("title_entity", 1.0, _normalize_text(row.get("title"))),
            ("keyword_entity", 0.95, _normalize_text(row.get("matched_keywords"))),
            ("content_entity", 0.8, _normalize_text(row.get("content"))),
        )
        for method, score, haystack in fields:
            matches = tuple(term for term in terms if _term_occurs(term, haystack))
            if matches:
                return method, score, matches
        return None

    def _resolve_articles(
        self,
        entity_terms: Mapping[int, Sequence[str]],
        universe_id: Optional[int],
        start_at: datetime,
        end_at: datetime,
        source_keys: Optional[Iterable[str]],
    ) -> Tuple[List[EvidenceItem], int]:
        selected: Dict[Tuple[str, int], EvidenceItem] = {}
        invalid_timestamps = 0
        for row in self._query_articles():
            interval = _article_time_interval(row)
            if interval is None:
                invalid_timestamps += 1
                continue
            observed_start, observed_end, time_method = interval
            if observed_end < start_at or observed_start > end_at:
                continue
            source_key = str(row.get("domain") or row.get("source_task_name") or "")
            if not self._source_allowed(source_key, source_keys):
                continue
            canonical_url = _canonical_document_url(row)
            for instrument_id, terms in entity_terms.items():
                match = self._article_match(row, terms)
                if match is None:
                    continue
                method, score, matches = match
                observed = observed_start
                fetched = (
                    _parse_stored_datetime(row.get("first_crawled"))
                    or _parse_stored_datetime(row.get("created_at"))
                    or observed
                )
                item = EvidenceItem(
                    evidence_key=f"article:{int(row['id'])}:instrument:{instrument_id}",
                    evidence_kind=DOCUMENT_KIND,
                    reference_id=int(row["id"]),
                    instrument_id=instrument_id,
                    universe_id=universe_id,
                    source_key=source_key,
                    source_url=str(row.get("url") or canonical_url),
                    source_title=str(row.get("title") or ""),
                    observed_at=_utc_text(observed),
                    fetched_at=_utc_text(fetched),
                    evidence_role="original_source_document",
                    match_method=f"{method}+{time_method}",
                    match_score=score,
                    match_terms=matches,
                    claim_capabilities=("announcement_text", "reported_event", "document_sentiment"),
                    denied_claim_capabilities=("live_price", "latest_quote", "structured_market_metric"),
                    content=str(row.get("content") or ""),
                )
                identity = (canonical_url or f"article-id:{item.reference_id}", instrument_id)
                previous = selected.get(identity)
                if previous is None or (item.match_score, item.observed_at, item.reference_id) > (
                    previous.match_score,
                    previous.observed_at,
                    previous.reference_id,
                ):
                    selected[identity] = item
        return sorted(selected.values(), key=lambda item: (item.observed_at, item.reference_id), reverse=True), invalid_timestamps

    def _persist(
        self,
        run_id: str,
        items_by_kind: Mapping[str, Sequence[EvidenceItem]],
        successful_kinds: Iterable[str],
    ) -> Dict[str, int]:
        successful = set(successful_kinds)
        self.connection.execute("SAVEPOINT evidence_resolver_write")
        try:
            for kind in successful:
                current_keys = {item.evidence_key for item in items_by_kind.get(kind, ())}
                rows = self.connection.execute(
                    "SELECT evidence_key FROM financial_research_evidence WHERE research_run_id=? AND evidence_kind=?",
                    (run_id, kind),
                ).fetchall()
                for row in rows:
                    if str(row[0]) not in current_keys:
                        self.connection.execute(
                            "DELETE FROM financial_research_evidence WHERE research_run_id=? AND evidence_key=?",
                            (run_id, str(row[0])),
                        )
            for kind in successful:
                for item in items_by_kind.get(kind, ()):
                    self.connection.execute(
                        """
                        INSERT INTO financial_research_evidence(
                            research_run_id, evidence_key, evidence_kind, snapshot_id,
                            article_id, instrument_id, universe_id, evidence_role,
                            match_method, match_score, observed_at, metadata_json
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(research_run_id, evidence_key) DO UPDATE SET
                            evidence_kind=excluded.evidence_kind,
                            snapshot_id=excluded.snapshot_id,
                            article_id=excluded.article_id,
                            instrument_id=excluded.instrument_id,
                            universe_id=excluded.universe_id,
                            evidence_role=excluded.evidence_role,
                            match_method=excluded.match_method,
                            match_score=excluded.match_score,
                            observed_at=excluded.observed_at,
                            metadata_json=excluded.metadata_json,
                            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        """,
                        (
                            run_id,
                            item.evidence_key,
                            item.evidence_kind,
                            item.reference_id if item.evidence_kind == STRUCTURED_KIND else None,
                            item.reference_id if item.evidence_kind == DOCUMENT_KIND else None,
                            item.instrument_id,
                            item.universe_id,
                            item.evidence_role,
                            item.match_method,
                            item.match_score,
                            item.observed_at,
                            json.dumps(item.persistence_metadata(), ensure_ascii=False, sort_keys=True),
                        ),
                    )
            self.connection.execute("RELEASE SAVEPOINT evidence_resolver_write")
        except Exception:
            self.connection.execute("ROLLBACK TO SAVEPOINT evidence_resolver_write")
            self.connection.execute("RELEASE SAVEPOINT evidence_resolver_write")
            raise
        return {
            kind: int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM financial_research_evidence WHERE research_run_id=? AND evidence_kind=?",
                    (run_id, kind),
                ).fetchone()[0]
            )
            for kind in (STRUCTURED_KIND, DOCUMENT_KIND)
        }

    def resolve_for_run(
        self,
        run_id: str,
        *,
        start_at: Optional[datetime] = None,
        end_at: Optional[datetime] = None,
        instrument_ids: Optional[Sequence[int]] = None,
        entity_terms: Optional[Mapping[int, Sequence[str]]] = None,
        source_keys: Optional[Iterable[str]] = None,
        persist: bool = True,
    ) -> Dict[str, object]:
        server_now = self.clock()
        if not isinstance(server_now, datetime) or server_now.tzinfo is None or server_now.utcoffset() is None:
            raise ValueError("EvidenceResolver clock must return a timezone-aware datetime")
        resolved_end = end_at or server_now
        resolved_start = start_at or (resolved_end - timedelta(days=7))
        start_utc = datetime.fromisoformat(_utc_text(resolved_start).replace("Z", "+00:00"))
        end_utc = datetime.fromisoformat(_utc_text(resolved_end).replace("Z", "+00:00"))
        if start_utc > end_utc:
            raise ValueError("start_at must not be after end_at")

        run = self._load_run(run_id)
        targets, universe_id = self._target_ids(run, start_utc, end_utc, instrument_ids)
        terms = self._entity_terms(targets, start_utc, end_utc, entity_terms)
        normalized_source_keys = (
            tuple(str(value) for value in source_keys) if source_keys is not None else None
        )
        pipelines: Dict[str, Dict[str, object]] = {}
        items_by_kind: Dict[str, List[EvidenceItem]] = {STRUCTURED_KIND: [], DOCUMENT_KIND: []}
        successful: List[str] = []

        try:
            snapshot_items, invalid = self._resolve_snapshots(
                targets, universe_id, start_utc, end_utc, normalized_source_keys
            )
            items_by_kind[STRUCTURED_KIND] = snapshot_items
            successful.append(STRUCTURED_KIND)
            pipelines["snapshots"] = {
                "status": "ok",
                "item_count": len(snapshot_items),
                "invalid_timestamp_count": invalid,
                "error_code": None,
            }
        except sqlite3.Error:
            pipelines["snapshots"] = {
                "status": "failed",
                "item_count": 0,
                "invalid_timestamp_count": 0,
                "error_code": "snapshot_query_failed",
            }

        try:
            article_items, invalid = self._resolve_articles(
                terms, universe_id, start_utc, end_utc, normalized_source_keys
            )
            items_by_kind[DOCUMENT_KIND] = article_items
            successful.append(DOCUMENT_KIND)
            pipelines["articles"] = {
                "status": "ok",
                "item_count": len(article_items),
                "invalid_timestamp_count": invalid,
                "error_code": None,
            }
        except sqlite3.Error:
            pipelines["articles"] = {
                "status": "failed",
                "item_count": 0,
                "invalid_timestamp_count": 0,
                "error_code": "article_query_failed",
            }

        persisted_counts = {STRUCTURED_KIND: 0, DOCUMENT_KIND: 0}
        if persist:
            persisted_counts = self._persist(run_id, items_by_kind, successful)
        failed_count = sum(1 for pipeline in pipelines.values() if pipeline["status"] == "failed")
        overall_status = "complete" if failed_count == 0 else ("partial" if successful else "failed")
        structured = [item.to_dict() for item in items_by_kind[STRUCTURED_KIND]]
        documents = [item.to_dict() for item in items_by_kind[DOCUMENT_KIND]]
        return {
            "research_run_id": run_id,
            "status": overall_status,
            "server_now": _utc_text(server_now),
            "time_range": {"start_at": _utc_text(start_utc), "end_at": _utc_text(end_utc)},
            "target_instrument_ids": list(targets),
            "target_universe_id": universe_id,
            "pipelines": pipelines,
            "structured_evidence": structured,
            "document_evidence": documents,
            "persisted_counts": persisted_counts,
            "evidence_contract": {
                "articles_are_not_quotes": True,
                "snapshots_are_not_original_documents": True,
                "stores_remain_separate": True,
            },
        }

    def list_for_run(self, run_id: str) -> Dict[str, object]:
        """Re-resolve the persisted references against their original stores."""

        rows = self.connection.execute(
            """
            SELECT evidence_kind, snapshot_id, article_id, instrument_id,
                   universe_id, evidence_role, match_method, match_score,
                   observed_at, metadata_json
            FROM financial_research_evidence
            WHERE research_run_id=?
            ORDER BY evidence_kind, observed_at DESC, id DESC
            """,
            (str(run_id),),
        ).fetchall()
        structured: List[Dict[str, object]] = []
        documents: List[Dict[str, object]] = []
        missing_references: List[Dict[str, object]] = []
        for relation in rows:
            relation = dict(relation)
            metadata = _safe_json_object(relation.get("metadata_json"))
            if relation["evidence_kind"] == STRUCTURED_KIND:
                source = self.connection.execute(
                    """
                    SELECT s.id, s.data_type, s.observed_at, s.fetched_at,
                           s.source_url, s.payload_json, p.provider_key
                    FROM financial_data_snapshots s
                    JOIN financial_provider_profiles p ON p.id=s.provider_profile_id
                    WHERE s.id=?
                    """,
                    (relation["snapshot_id"],),
                ).fetchone()
                target = structured
            else:
                source = self.connection.execute(
                    """
                    SELECT id, title, content, url, domain, publish_date,
                           published_at_utc, published_timezone,
                           published_precision, published_time_source, first_crawled
                    FROM articles WHERE id=? AND status='active'
                    """,
                    (relation["article_id"],),
                ).fetchone()
                target = documents
            if source is None:
                missing_references.append(
                    {
                        "evidence_kind": relation["evidence_kind"],
                        "reference_id": relation["snapshot_id"] or relation["article_id"],
                    }
                )
                continue
            source_payload = dict(source)
            if relation["evidence_kind"] == STRUCTURED_KIND:
                try:
                    source_payload["payload"] = json.loads(source_payload.pop("payload_json"))
                except (TypeError, ValueError):
                    source_payload["payload"] = {"decode_error": True}
            source_payload.update(
                {
                    "instrument_id": relation["instrument_id"],
                    "universe_id": relation["universe_id"],
                    "evidence_role": relation["evidence_role"],
                    "match_method": relation["match_method"],
                    "match_score": relation["match_score"],
                    "metadata": metadata,
                }
            )
            target.append(source_payload)
        return {
            "research_run_id": str(run_id),
            "structured_evidence": structured,
            "document_evidence": documents,
            "missing_references": missing_references,
        }


__all__ = ["DOCUMENT_KIND", "STRUCTURED_KIND", "EvidenceItem", "EvidenceResolver"]

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Time- and evidence-gated financial memory over the existing RAGFlow KB.

RAGFlow is used only for semantic candidate discovery.  A candidate is not
trusted merely because the vector store returned it: its document identity is
resolved through the application's existing SQLite mappings, its current
claim/report state is rechecked, and the text exposed downstream is rebuilt
from the local projection saved before upload.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from jsonschema import Draft202012Validator

from financial_conflict_judge import FINANCIAL_CONFLICT_JUDGE_VERSION
from financial_instruments import stable_instrument_key
from financial_temporal_judge import DEFAULT_FRESHNESS_SECONDS


FINANCIAL_RAG_GATE_VERSION = "financial-rag-gate-v1"
FINANCIAL_RAG_METADATA_SCHEMA_VERSION = "financial-rag-document-v1"
FINANCIAL_RAG_RETRIEVAL_SCHEMA_VERSION = "financial-rag-retrieval-v1"
METADATA_BEGIN = "FINANCIAL_RAG_METADATA_BEGIN"
METADATA_END = "FINANCIAL_RAG_METADATA_END"

CONTENT_KINDS = frozenset(
    {"verified_fact", "historical_fact", "research_report", "human_adjudication"}
)
FACT_CONTENT_KINDS = frozenset({"verified_fact", "historical_fact"})
VERIFIED_CONFLICT_VERDICTS = frozenset(
    {"verified_consensus", "verified_authoritative"}
)
TERMINAL_REPORT_STATUSES = frozenset(
    {"verified", "generated_unverified", "degraded_unverified", "insufficient_evidence"}
)
RETRIEVAL_ROUTES = frozenset(
    {"current_fact", "historical_fact", "research", "mixed"}
)

FINANCIAL_RAG_RETRIEVAL_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "schema_version", "gate_version", "status", "route", "request_time",
        "chunks", "excluded", "boundaries",
    ],
    "properties": {
        "schema_version": {"const": FINANCIAL_RAG_RETRIEVAL_SCHEMA_VERSION},
        "gate_version": {"const": FINANCIAL_RAG_GATE_VERSION},
        "status": {"enum": ["ready", "empty", "degraded"]},
        "route": {"enum": sorted(RETRIEVAL_ROUTES)},
        "request_time": {"type": "object"},
        "chunks": {"type": "array", "items": {"type": "object"}},
        "excluded": {"type": "array", "items": {"type": "object"}},
        "boundaries": {"type": "object"},
    },
    "additionalProperties": False,
}
_RETRIEVAL_VALIDATOR = Draft202012Validator(FINANCIAL_RAG_RETRIEVAL_SCHEMA)


def _json_object(value: object) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _json_array(value: object) -> list:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _text(value: object, maximum: int = 4000) -> str:
    return str(value or "").strip()[:maximum]


def _finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _parse_utc(value: object) -> datetime | None:
    raw = _text(value, 100)
    if not raw:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raw += "T00:00:00Z"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _utc_text(value: object) -> str:
    parsed = value if isinstance(value, datetime) else _parse_utc(value)
    if not isinstance(parsed, datetime):
        return ""
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _safe_url(value: object) -> str:
    candidate = _text(value, 2000)
    if candidate.startswith("/api/financial/"):
        return candidate
    parsed = urlparse(candidate)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return ""
    return candidate


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _instrument_key(row: Mapping[str, object]) -> str:
    if row.get("instrument_key"):
        return _text(row.get("instrument_key"), 300)
    try:
        return stable_instrument_key(
            canonical_symbol=str(row.get("canonical_symbol") or ""),
            asset_type=str(row.get("asset_type") or ""),
            exchange=str(row.get("exchange") or ""),
            country_code=str(row.get("country_code") or ""),
            market=str(row.get("market") or ""),
        )
    except ValueError:
        return ""


def validate_financial_rag_metadata(value: Mapping[str, object]) -> dict:
    try:
        report_id = int(value.get("report_id") or 0)
        report_version = int(value.get("report_version") or 0)
        claim_id = int(value.get("claim_id") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("financial RAG metadata ids must be integers") from exc
    result = {
        "schema_version": FINANCIAL_RAG_METADATA_SCHEMA_VERSION,
        "content_kind": _text(value.get("content_kind"), 80),
        "instrument_key": _text(value.get("instrument_key"), 300),
        "instrument_keys": sorted(
            {_text(item, 300) for item in value.get("instrument_keys") or [] if _text(item, 300)}
        ),
        "universe_key": _text(value.get("universe_key"), 300),
        "canonical_symbol": _text(value.get("canonical_symbol"), 120),
        "display_name": _text(value.get("display_name"), 300),
        "as_of": _utc_text(value.get("as_of")),
        "effective_to": _utc_text(value.get("effective_to")),
        "verdict": _text(value.get("verdict"), 120),
        "temporal_status": _text(value.get("temporal_status"), 120),
        "report_id": report_id,
        "report_version": report_version,
        "claim_id": claim_id,
        "operation_id": _text(value.get("operation_id"), 200),
        "explicit_confirmation": bool(value.get("explicit_confirmation")),
        "public_url": _safe_url(value.get("public_url")),
        "public_text": _text(value.get("public_text"), 12000),
    }
    if result["content_kind"] not in CONTENT_KINDS:
        raise ValueError("unsupported financial RAG content_kind")
    if not result["as_of"]:
        raise ValueError("financial RAG metadata requires absolute as_of")
    if result["effective_to"]:
        start = _parse_utc(result["as_of"])
        end = _parse_utc(result["effective_to"])
        if start is None or end is None or end < start:
            raise ValueError("financial RAG effective_to precedes as_of")
    if not (result["instrument_key"] or result["instrument_keys"] or result["universe_key"]):
        raise ValueError("financial RAG metadata requires instrument or universe identity")
    if result["content_kind"] in FACT_CONTENT_KINDS:
        if result["claim_id"] < 1:
            raise ValueError("financial fact memory requires claim_id")
        if result["verdict"] not in VERIFIED_CONFLICT_VERDICTS:
            raise ValueError("financial fact memory requires verified conflict verdict")
    if result["content_kind"] == "verified_fact" and not result["effective_to"]:
        raise ValueError("current financial fact memory requires effective_to")
    if result["content_kind"] == "research_report" and result["report_id"] < 1:
        raise ValueError("financial report memory requires report_id")
    if result["content_kind"] == "human_adjudication":
        if not result["operation_id"] or not result["explicit_confirmation"]:
            raise ValueError("human adjudication memory requires explicit confirmation")
    return result


def financial_rag_metadata_block(metadata: Mapping[str, object]) -> str:
    """Serialize a visible, deterministic header before RAGFlow upload."""

    safe = validate_financial_rag_metadata(metadata)
    return f"{METADATA_BEGIN}\n{_canonical(safe)}\n{METADATA_END}\n"


def extract_financial_rag_metadata(content: object) -> dict:
    text = str(content or "")
    match = re.search(
        rf"(?:^|\n){re.escape(METADATA_BEGIN)}\n(.+?)\n{re.escape(METADATA_END)}(?:\n|$)",
        text,
        flags=re.DOTALL,
    )
    if match is None:
        return {}
    try:
        value = json.loads(match.group(1))
        return validate_financial_rag_metadata(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def strip_financial_rag_metadata(content: object) -> str:
    text = str(content or "")
    return re.sub(
        rf"(?:^|\n){re.escape(METADATA_BEGIN)}\n.+?\n{re.escape(METADATA_END)}\n?",
        "",
        text,
        count=1,
        flags=re.DOTALL,
    ).strip()


class FinancialRAGPublicationPlanner:
    """Build separately labelled report/current/history documents from SQLite."""

    def __init__(self, connection):
        self.connection = connection

    def _report(self, final_report_id: int) -> dict:
        row = self.connection.execute(
            """
            SELECT report.id, report.research_run_id, report.report_version,
                   report.report_status, report.recommendation, report.title,
                   report.executive_summary, report.report_markdown,
                   report.report_json, report.observed_at, report.fetched_at,
                   run.requested_at, run.instrument_id, run.universe_id,
                   run.scope_type,
                   instrument.canonical_symbol, instrument.display_name,
                   instrument.asset_type, instrument.market, instrument.exchange,
                   instrument.currency, instrument.country_code,
                   universe.universe_key, universe.display_name
            FROM financial_final_reports report
            JOIN financial_research_runs run ON run.id=report.research_run_id
            LEFT JOIN financial_instruments instrument ON instrument.id=run.instrument_id
            LEFT JOIN financial_universes universe ON universe.id=run.universe_id
            WHERE report.id=?
            """,
            (int(final_report_id),),
        ).fetchone()
        if row is None:
            raise ValueError("final_report_not_found")
        keys = (
            "report_id", "research_run_id", "report_version", "report_status",
            "recommendation", "title", "executive_summary", "report_markdown",
            "report_json", "observed_at", "fetched_at", "requested_at",
            "instrument_id", "universe_id", "scope_type", "canonical_symbol", "display_name",
            "asset_type", "market", "exchange", "currency", "country_code",
            "universe_key", "universe_name",
        )
        result = dict(zip(keys, row))
        result["report_json_value"] = _json_object(result["report_json"])
        if str(result["report_status"] or "").casefold() not in TERMINAL_REPORT_STATUSES:
            raise ValueError("report_not_terminal")
        result["instrument_key"] = _instrument_key(result)
        return result

    def _next_report_time(self, report: Mapping[str, object]) -> str:
        row = self.connection.execute(
            """
            SELECT observed_at, fetched_at, created_at
            FROM financial_final_reports
            WHERE research_run_id=? AND report_version>?
            ORDER BY report_version ASC LIMIT 1
            """,
            (str(report["research_run_id"]), int(report["report_version"])),
        ).fetchone()
        return _utc_text(next((item for item in row or () if item), ""))

    def _citations(self, selected_ids: Sequence[int]) -> list[dict]:
        ids = set()
        for item in selected_ids:
            try:
                value = int(item)
            except (TypeError, ValueError):
                continue
            if value > 0:
                ids.add(value)
        ids = sorted(ids)
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self.connection.execute(
            f"""
            SELECT evidence.id, evidence.snapshot_id, evidence.article_id,
                   evidence.source_url, snapshot.source_url, article.url,
                   evidence.source_title, profile.provider_key
            FROM financial_claim_evidence evidence
            LEFT JOIN financial_data_snapshots snapshot ON snapshot.id=evidence.snapshot_id
            LEFT JOIN articles article ON article.id=evidence.article_id
            LEFT JOIN financial_provider_profiles profile
              ON profile.id=COALESCE(evidence.provider_profile_id, snapshot.provider_profile_id)
            WHERE evidence.id IN ({placeholders})
            ORDER BY evidence.id
            """,
            ids,
        ).fetchall()
        result = []
        for row in rows:
            snapshot_id = int(row[1]) if row[1] is not None else 0
            url = _safe_url(row[3] or row[4] or row[5])
            if not url and snapshot_id:
                url = f"/api/financial/snapshots/{snapshot_id}"
            if not url:
                continue
            result.append(
                {
                    "evidence_id": int(row[0]),
                    "snapshot_id": snapshot_id,
                    "article_id": int(row[2]) if row[2] is not None else 0,
                    "url": url,
                    "label": _text(row[6] or row[7] or "金融事实证据", 300),
                }
            )
        return result

    def _facts(self, report: Mapping[str, object]) -> list[dict]:
        rows = self.connection.execute(
            """
            WITH latest_conflict AS (
                SELECT verdict.claim_id, verdict.verdict, verdict.rationale,
                       verdict.selected_evidence_ids_json
                FROM financial_verdicts verdict
                JOIN (
                    SELECT claim_id, MAX(adjudication_version) AS version
                    FROM financial_verdicts
                    WHERE adjudicator=? GROUP BY claim_id
                ) latest ON latest.claim_id=verdict.claim_id
                        AND latest.version=verdict.adjudication_version
                WHERE verdict.adjudicator=?
            )
            SELECT claim.id, claim.statement, claim.normalized_value_json,
                   claim.unit, claim.currency, claim.effective_at,
                   claim.observed_at, claim.verification_status,
                   conflict.verdict, conflict.rationale,
                   conflict.selected_evidence_ids_json
            FROM financial_claims claim
            JOIN latest_conflict conflict ON conflict.claim_id=claim.id
            WHERE claim.final_report_id=? AND claim.claim_type='fact'
              AND claim.verification_status IN ('verified_current','verified_historical')
              AND conflict.verdict IN ('verified_consensus','verified_authoritative')
            ORDER BY claim.id
            """,
            (
                FINANCIAL_CONFLICT_JUDGE_VERSION,
                FINANCIAL_CONFLICT_JUDGE_VERSION,
                int(report["report_id"]),
            ),
        ).fetchall()
        result = []
        for row in rows:
            normalized = _json_object(row[2])
            rationale = _json_object(row[9])
            selected = rationale.get("selected_evidence_ids") or _json_array(row[10])
            citations = self._citations(selected)
            if not citations:
                continue
            subject = _json_object(normalized.get("subject"))
            instrument_key = _text(subject.get("instrument_key"), 300) or str(
                report.get("instrument_key") or ""
            )
            as_of = _utc_text(
                normalized.get("as_of") or row[6] or row[5] or report.get("requested_at")
            )
            period = _json_object(normalized.get("period"))
            effective_to = _utc_text(
                normalized.get("effective_to") or period.get("valid_to")
            )
            if str(row[7]) == "verified_current" and not effective_to:
                start = _parse_utc(as_of)
                threshold = int(
                    DEFAULT_FRESHNESS_SECONDS.get(
                        _text(normalized.get("metric"), 120).casefold(),
                        DEFAULT_FRESHNESS_SECONDS["default"],
                    )
                )
                effective_to = _utc_text(
                    start + timedelta(seconds=threshold) if start else None
                )
            result.append(
                {
                    "claim_id": int(row[0]),
                    "statement": _text(row[1], 6000),
                    "metric": _text(normalized.get("metric") or "unspecified_metric", 120),
                    "value": normalized.get("value"),
                    "unit": _text(row[3], 80),
                    "currency": _text(row[4], 40),
                    "as_of": as_of,
                    "effective_to": effective_to,
                    "temporal_status": str(row[7]),
                    "verdict": str(row[8]),
                    "instrument_key": instrument_key,
                    "citations": citations,
                }
            )
        return result

    @staticmethod
    def _document(metadata: Mapping[str, object], body: str, *, key: str) -> dict:
        safe = validate_financial_rag_metadata(metadata)
        content = financial_rag_metadata_block(safe) + body.strip() + "\n"
        digest = _hash_text(content)
        kind = str(safe["content_kind"])
        file_name = f"financial-rag-{kind}-{key}-{digest[:12]}.md"
        return {
            "document_key": key,
            "document_name": file_name,
            "content": content,
            "content_sha256": digest,
            "metadata": safe,
        }

    def build_documents(self, final_report_id: int) -> list[dict]:
        report = self._report(int(final_report_id))
        report_json = report["report_json_value"]
        as_of = _utc_text(
            report_json.get("as_of")
            or report.get("observed_at")
            or report.get("fetched_at")
            or report.get("requested_at")
        )
        identity = str(report.get("instrument_key") or "")
        universe_key = str(
            report.get("universe_key") or f"scope:{report.get('scope_type') or 'market'}"
        )
        public_text = "\n".join(
            item
            for item in (
                f"{report.get('title') or 'TradingAgents 研究报告'}",
                f"评级：{report.get('recommendation') or 'insufficient_evidence'}",
                f"时点：{as_of}",
                f"摘要：{report.get('executive_summary') or ''}",
                "内容类型：TradingAgents 研究观点，不是已核验事实。",
            )
            if item
        )
        documents = [
            self._document(
                {
                    "content_kind": "research_report",
                    "instrument_key": identity,
                    "universe_key": universe_key,
                    "canonical_symbol": report.get("canonical_symbol"),
                    "display_name": report.get("display_name") or report.get("universe_name"),
                    "as_of": as_of,
                    "effective_to": self._next_report_time(report),
                    "verdict": "research_opinion",
                    "temporal_status": "report_as_of",
                    "report_id": report["report_id"],
                    "report_version": report["report_version"],
                    "public_url": f"/api/financial/reports/{int(report['report_id'])}",
                    "public_text": public_text,
                },
                (
                    "内容类型：TradingAgents 研究观点，不是已核验事实。\n\n"
                    + str(report.get("report_markdown") or public_text)
                ),
                key=f"r{int(report['report_id'])}-v{int(report['report_version'])}",
            )
        ]
        for fact in self._facts(report):
            kind = (
                "verified_fact"
                if fact["temporal_status"] == "verified_current"
                else "historical_fact"
            )
            citation_lines = "\n".join(
                f"- [{item['label']}]({item['url']})" for item in fact["citations"]
            )
            public_text = (
                f"{fact['statement']}\n"
                f"指标：{fact['metric']}；时点：{fact['as_of']}；"
                f"时效状态：{fact['temporal_status']}；多源裁决：{fact['verdict']}。"
            )
            documents.append(
                self._document(
                    {
                        "content_kind": kind,
                        "instrument_key": fact["instrument_key"],
                        "universe_key": universe_key,
                        "canonical_symbol": report.get("canonical_symbol"),
                        "display_name": report.get("display_name") or report.get("universe_name"),
                        "as_of": fact["as_of"],
                        "effective_to": fact["effective_to"],
                        "verdict": fact["verdict"],
                        "temporal_status": fact["temporal_status"],
                        "report_id": report["report_id"],
                        "report_version": report["report_version"],
                        "claim_id": fact["claim_id"],
                        "public_url": fact["citations"][0]["url"],
                        "public_text": public_text,
                    },
                    f"{public_text}\n\n证据：\n{citation_lines}",
                    key=f"r{int(report['report_id'])}-c{int(fact['claim_id'])}",
                )
            )
        return documents


class FinancialRAGRetrievalGate:
    """Discover with RAGFlow, then authorize with current local state."""

    def __init__(self, database, *, ragflow_client, kb_id: str):
        self.database = database
        self.ragflow_client = ragflow_client
        self.kb_id = str(kb_id or "").strip()

    @property
    def connection(self):
        if hasattr(self.database, "_ensure_connection"):
            self.database._ensure_connection()
            return self.database.connection
        return self.database

    def _registry(self) -> dict[str, dict]:
        registry = {}
        rows = self.connection.execute(
            """
            SELECT metadata_json FROM financial_artifacts
            WHERE ragflow_kb_id=? AND status='ready'
            """,
            (self.kb_id,),
        ).fetchall()
        for row in rows:
            metadata = _json_object(row[0])
            for raw in metadata.get("ragflow_documents") or []:
                if not isinstance(raw, Mapping) or str(raw.get("status")) != "uploaded":
                    continue
                try:
                    safe = validate_financial_rag_metadata(raw.get("metadata") or {})
                except (TypeError, ValueError):
                    continue
                record = {
                    **dict(raw),
                    "metadata": safe,
                    "registry_source": "financial_artifacts",
                }
                for identity in (raw.get("document_id"), raw.get("document_name")):
                    if identity:
                        registry[str(identity)] = record

        article_rows = self.connection.execute(
            """
            SELECT mapping.document_id, mapping.document_name, article.content
            FROM article_ragflow_documents mapping
            JOIN articles article ON article.id=mapping.article_id
            WHERE mapping.kb_id=? AND mapping.sync_status IN ('uploaded','parsed')
            """,
            (self.kb_id,),
        ).fetchall()
        for document_id, document_name, content in article_rows:
            metadata = extract_financial_rag_metadata(content)
            if metadata.get("content_kind") != "human_adjudication":
                continue
            record = {
                "document_id": str(document_id or ""),
                "document_name": str(document_name or ""),
                "content_sha256": _hash_text(str(content or "")),
                "metadata": metadata,
                "registry_source": "article_ragflow_documents",
            }
            for identity in (document_id, document_name):
                if identity:
                    registry[str(identity)] = record
        return registry

    @staticmethod
    def _chunk_identity(chunk: Mapping[str, object]) -> tuple[str, str]:
        document_id = _text(
            chunk.get("document_id") or chunk.get("doc_id") or chunk.get("docid"), 300
        )
        document_name = _text(
            chunk.get("document_name") or chunk.get("docnm_kwd") or chunk.get("doc_name"),
            500,
        )
        return document_id, document_name

    def _claim_state_reason(self, metadata: Mapping[str, object], route: str) -> str:
        claim_id = int(metadata.get("claim_id") or 0)
        row = self.connection.execute(
            "SELECT verification_status FROM financial_claims WHERE id=?",
            (claim_id,),
        ).fetchone()
        if row is None:
            return "claim_missing"
        status = str(row[0] or "")
        if route in {"current_fact", "mixed"} and status != "verified_current":
            return "claim_not_current"
        if route == "historical_fact" and status not in {
            "verified_current", "verified_historical"
        }:
            return "claim_not_historically_verified"
        verdict = self.connection.execute(
            """
            SELECT verdict FROM financial_verdicts
            WHERE claim_id=? AND adjudicator=?
            ORDER BY adjudication_version DESC LIMIT 1
            """,
            (claim_id, FINANCIAL_CONFLICT_JUDGE_VERSION),
        ).fetchone()
        if verdict is None or str(verdict[0]) not in VERIFIED_CONFLICT_VERDICTS:
            return "claim_conflict_not_verified"
        return ""

    def _report_state_reason(
        self,
        metadata: Mapping[str, object],
        requested_as_of: datetime,
    ) -> str:
        report_id = int(metadata.get("report_id") or 0)
        row = self.connection.execute(
            """
            SELECT report.research_run_id, report.report_version, report.report_status
            FROM financial_final_reports report WHERE report.id=?
            """,
            (report_id,),
        ).fetchone()
        if row is None:
            return "report_missing_or_revoked"
        if str(row[2] or "").casefold() not in TERMINAL_REPORT_STATUSES:
            return "report_missing_or_revoked"
        later = self.connection.execute(
            """
            SELECT observed_at, fetched_at, created_at
            FROM financial_final_reports
            WHERE research_run_id=? AND report_version>?
            ORDER BY report_version ASC
            """,
            (str(row[0]), int(row[1])),
        ).fetchall()
        for candidate in later:
            effective = _parse_utc(next((item for item in candidate if item), ""))
            if effective is not None and effective <= requested_as_of:
                return "newer_report_version_effective"
        return ""

    @staticmethod
    def _route_reason(kind: str, route: str) -> str:
        if route == "current_fact" and kind != "verified_fact":
            return "content_kind_not_current_fact"
        if route == "historical_fact" and kind not in FACT_CONTENT_KINDS:
            return "content_kind_not_historical_fact"
        if route == "research" and kind not in {"research_report", "human_adjudication"}:
            return "content_kind_not_research"
        if route == "mixed" and kind not in {
            "verified_fact", "research_report", "human_adjudication"
        }:
            return "content_kind_not_current_mixed"
        return ""

    @staticmethod
    def _time_reason(
        metadata: Mapping[str, object],
        *,
        route: str,
        requested_as_of: datetime,
        range_start: datetime,
        range_end: datetime,
    ) -> str:
        as_of = _parse_utc(metadata.get("as_of"))
        effective_to = _parse_utc(metadata.get("effective_to"))
        if as_of is None:
            return "document_as_of_missing"
        if route == "historical_fact":
            if as_of > range_end:
                return "document_after_requested_history"
            if effective_to is not None and effective_to < range_start:
                return "document_before_requested_history"
            return ""
        if as_of > requested_as_of:
            return "future_document"
        if effective_to is not None and effective_to <= requested_as_of:
            return "document_no_longer_effective"
        return ""

    def search(
        self,
        question: str,
        *,
        route: str,
        server_time_context: Mapping[str, object],
        time_range: Mapping[str, object] | None = None,
        instrument_keys: Sequence[str] = (),
        limit: int = 8,
    ) -> dict:
        route = str(route or "")
        if route not in RETRIEVAL_ROUTES:
            raise ValueError("unsupported financial RAG route")
        server_now = _parse_utc(server_time_context.get("server_now_utc"))
        requested = _parse_utc(
            server_time_context.get("requested_as_of")
            or server_time_context.get("server_now_utc")
        )
        if server_now is None or requested is None:
            raise ValueError("absolute application server time is required")
        resolved_range = dict(time_range or {})
        range_start = _parse_utc(resolved_range.get("start_utc")) or requested
        range_end = _parse_utc(resolved_range.get("end_utc")) or requested
        if range_end < range_start:
            raise ValueError("financial RAG time range is inverted")
        requested_keys = {str(item) for item in instrument_keys if str(item)}
        request_time = {
            "server_now_utc": _utc_text(server_now),
            "requested_as_of": _utc_text(requested),
            "range_start_utc": _utc_text(range_start),
            "range_end_utc": _utc_text(range_end),
            "clock_source": "application_server",
        }
        base = {
            "schema_version": FINANCIAL_RAG_RETRIEVAL_SCHEMA_VERSION,
            "gate_version": FINANCIAL_RAG_GATE_VERSION,
            "status": "empty",
            "route": route,
            "request_time": request_time,
            "chunks": [],
            "excluded": [],
            "boundaries": {
                "ragflow_is_candidate_discovery_only": True,
                "remote_chunk_text_used_as_fact": False,
                "local_sqlite_state_rechecked": True,
                "unmapped_documents_allowed": False,
                "model_calls": 0,
                "real_order_execution": False,
            },
        }
        if not self.kb_id:
            base["status"] = "degraded"
            base["excluded"].append(
                {"document_id": "", "document_name": "", "reason": "financial_ragflow_kb_not_configured"}
            )
            _RETRIEVAL_VALIDATOR.validate(base)
            return base
        try:
            response = self.ragflow_client.search_dataset(
                self.kb_id,
                str(question or ""),
                top_n=max(1, min(50, int(limit) * 3)),
            )
        except Exception:
            base["status"] = "degraded"
            base["excluded"].append(
                {"document_id": "", "document_name": "", "reason": "ragflow_unavailable"}
            )
            _RETRIEVAL_VALIDATOR.validate(base)
            return base
        chunks = response.get("chunks") if isinstance(response, Mapping) else []
        chunks = chunks if isinstance(chunks, list) else []
        registry = self._registry()
        accepted = []
        excluded = []
        seen = set()
        for chunk in chunks:
            if not isinstance(chunk, Mapping):
                continue
            document_id, document_name = self._chunk_identity(chunk)
            record = registry.get(document_id) or registry.get(document_name)
            if record is None:
                excluded.append(
                    {"document_id": document_id, "document_name": document_name, "reason": "unregistered_document"}
                )
                continue
            identity = str(record.get("document_id") or record.get("document_name") or "")
            if identity in seen:
                continue
            seen.add(identity)
            metadata = dict(record["metadata"])
            reason = self._route_reason(str(metadata.get("content_kind") or ""), route)
            keys = set(metadata.get("instrument_keys") or [])
            if metadata.get("instrument_key"):
                keys.add(str(metadata["instrument_key"]))
            if not reason and requested_keys and not (keys & requested_keys):
                reason = "instrument_scope_mismatch"
            if not reason:
                reason = self._time_reason(
                    metadata,
                    route=route,
                    requested_as_of=requested,
                    range_start=range_start,
                    range_end=range_end,
                )
            kind = str(metadata.get("content_kind") or "")
            if not reason and kind in FACT_CONTENT_KINDS:
                reason = self._claim_state_reason(metadata, route)
            if not reason and kind in FACT_CONTENT_KINDS | {"research_report"}:
                reason = self._report_state_reason(metadata, requested)
            if reason:
                excluded.append(
                    {"document_id": document_id, "document_name": document_name, "reason": reason}
                )
                continue
            score = _finite(
                chunk.get("similarity") or chunk.get("score") or chunk.get("vector_similarity")
            )
            accepted.append(
                {
                    "document_id": str(record.get("document_id") or document_id),
                    "document_name": str(record.get("document_name") or document_name),
                    "content_kind": kind,
                    "instrument_key": str(metadata.get("instrument_key") or ""),
                    "instrument_keys": list(metadata.get("instrument_keys") or []),
                    "universe_key": str(metadata.get("universe_key") or ""),
                    "as_of": str(metadata.get("as_of") or ""),
                    "effective_to": str(metadata.get("effective_to") or ""),
                    "verdict": str(metadata.get("verdict") or ""),
                    "temporal_status": str(metadata.get("temporal_status") or ""),
                    "report_id": int(metadata.get("report_id") or 0),
                    "report_version": int(metadata.get("report_version") or 0),
                    "claim_id": int(metadata.get("claim_id") or 0),
                    "public_url": str(metadata.get("public_url") or ""),
                    "content": str(metadata.get("public_text") or ""),
                    "retrieval_score": score,
                    "registry_source": str(record.get("registry_source") or ""),
                }
            )
            if len(accepted) >= max(1, min(50, int(limit))):
                break
        base["chunks"] = accepted
        base["excluded"] = excluded[:100]
        base["status"] = "ready" if accepted else "empty"
        _RETRIEVAL_VALIDATOR.validate(base)
        return base


__all__ = [
    "CONTENT_KINDS",
    "FINANCIAL_RAG_GATE_VERSION",
    "FINANCIAL_RAG_METADATA_SCHEMA_VERSION",
    "FINANCIAL_RAG_RETRIEVAL_SCHEMA",
    "FINANCIAL_RAG_RETRIEVAL_SCHEMA_VERSION",
    "FinancialRAGPublicationPlanner",
    "FinancialRAGRetrievalGate",
    "extract_financial_rag_metadata",
    "financial_rag_metadata_block",
    "strip_financial_rag_metadata",
    "validate_financial_rag_metadata",
]

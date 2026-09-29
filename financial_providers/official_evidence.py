#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Read-only adapter from the existing article store to official evidence."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timezone
from urllib.parse import urlsplit

from financial_provider_contract import (
    AdjustmentMode,
    FinancialDataKind,
    FinancialDataRecord,
    FinancialDataRequest,
    FinancialProviderResponse,
    FreshnessState,
    InvalidSymbolError,
    MarketStatus,
    TemporarilyUnavailableError,
    raw_response_hash,
)
from financial_providers.base import RegisteredFinancialProvider, setting_value, utc_text


ENDPOINT_KINDS = {"official_article": FinancialDataKind.NEWS}


def _timestamp(value, fetched_at):
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, time.min)
    if not isinstance(value, datetime):
        text = str(value).strip().replace("Z", "+00:00")
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            try:
                value = datetime.combine(date.fromisoformat(text[:10]), time.min)
            except ValueError:
                return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return min(value.astimezone(timezone.utc), fetched_at)


class OfficialEvidenceProvider(RegisteredFinancialProvider):
    provider_key = "official_evidence"

    def _official_domain(self, value: str, instrument=None) -> str:
        host = str(urlsplit(value).hostname or "").strip().casefold().rstrip(".")
        suffixes = list(self.profile["official_domain_suffixes"])
        if instrument is not None:
            suffixes.extend(instrument.metadata.get("official_domains") or ())
        for suffix in suffixes:
            normalized = str(suffix).casefold().rstrip(".")
            if host == normalized or host.endswith(f".{normalized}"):
                return host
        return ""

    def _article(self, request, article_id):
        if self.connection is None:
            raise TemporarilyUnavailableError(
                "existing SQLite article connection is required",
                **self._error_details(request, failure="database_connection_missing"),
            )
        table = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='articles'"
        ).fetchone()
        if not table:
            raise TemporarilyUnavailableError(
                "existing articles table is not initialized",
                **self._error_details(request, failure="articles_table_missing"),
            )
        row = self.connection.execute(
            """
            SELECT id, url, canonical_url, title, content, domain, publish_date,
                   content_hash, first_crawled, created_at, configured_url,
                   resolved_target_url
            FROM articles WHERE id=? AND status='active'
            """,
            (article_id,),
        ).fetchone()
        return row

    def fetch(self, request: FinancialDataRequest) -> FinancialProviderResponse:
        instrument, _mapping, fetched_at = self._prepare_request(
            request, ENDPOINT_KINDS, mapping_required=False
        )
        try:
            article_id = int(request.parameters.get("article_id"))
        except (TypeError, ValueError) as exc:
            raise InvalidSymbolError(
                "article_id is required for official document evidence",
                **self._error_details(request, parameter="article_id"),
            ) from exc
        if article_id <= 0:
            raise InvalidSymbolError(
                "article_id must be positive",
                **self._error_details(request, parameter="article_id"),
            )
        row = self._article(request, article_id)
        if not row:
            raise InvalidSymbolError(
                "official evidence article was not found or is inactive",
                **self._error_details(request, article_id=article_id),
            )
        (
            stored_id,
            url,
            canonical_url,
            title,
            content,
            stored_domain,
            publish_date,
            content_hash,
            first_crawled,
            created_at,
            configured_url,
            resolved_target_url,
        ) = row
        source_url = str(canonical_url or url or resolved_target_url or "").strip()
        official_domain = self._official_domain(source_url, instrument)
        if not official_domain:
            raise InvalidSymbolError(
                "article domain is not in the controlled official evidence allowlist",
                **self._error_details(
                    request,
                    article_id=article_id,
                    gate_reason="non_official_domain",
                ),
            )
        observed_at = (
            _timestamp(publish_date, fetched_at)
            or _timestamp(first_crawled, fetched_at)
            or _timestamp(created_at, fetched_at)
            or fetched_at
        )
        threshold = int(
            setting_value(self.settings, "FINANCIAL_NEWS_FRESHNESS_SECONDS", 3600)
        )
        age = max(0.0, (fetched_at - observed_at).total_seconds())
        if request.requested_as_of.astimezone(timezone.utc) < fetched_at:
            freshness = FreshnessState.HISTORICAL
        elif age <= threshold:
            freshness = FreshnessState.CURRENT
        elif age <= threshold * 2:
            freshness = FreshnessState.DELAYED
        else:
            freshness = FreshnessState.STALE
        content_text = str(content or "")
        digest = str(content_hash or "").strip() or hashlib.sha256(
            content_text.encode("utf-8")
        ).hexdigest()
        normalized = {
            "article_id": int(stored_id),
            "title": str(title or ""),
            "official_domain": official_domain,
            "stored_domain": str(stored_domain or ""),
            "publish_date": str(publish_date or ""),
            "content_hash": digest,
            "content_excerpt": content_text[:500],
            "configured_url": str(configured_url or ""),
            "resolved_target_url": str(resolved_target_url or ""),
            "instrument_symbol": instrument.canonical_symbol,
            "interval": "document",
            "semantic_role": "official_unstructured_evidence",
        }
        record = FinancialDataRecord(
            instrument_id=request.instrument_id,
            metric=request.metric,
            value=str(title or f"Official article {stored_id}"),
            unit="document",
            currency="",
            market_status=MarketStatus.UNKNOWN,
            observed_at=observed_at,
            fetched_at=fetched_at,
            timezone="UTC",
            freshness_state=freshness,
            requested_as_of=request.requested_as_of,
            raw_response_hash=raw_response_hash(
                {"article_id": stored_id, "content_hash": digest, "source_url": source_url}
            ),
            normalized_payload=normalized,
            adjustment=AdjustmentMode.NOT_APPLICABLE,
            quality_flags=(
                "official_domain_verified",
                "unstructured_document_not_quote",
                "numerical_claims_require_structured_snapshot_verification",
            ),
            source_url=source_url,
            provider_symbol=official_domain,
            normalizer_version="official-evidence-v1",
            lineage={
                "storage": "existing_articles_table",
                "article_id": int(stored_id),
                "network_call_performed": False,
                "freshness_threshold_seconds": threshold,
            },
        )
        return FinancialProviderResponse(
            provider_id=self.provider_id,
            endpoint=request.endpoint,
            license_profile=self.license_profile,
            request_id=request.request_id,
            data_kind=request.data_kind,
            records=(record,),
        )

    def health_probe(self, *, request_id: str, requested_at: datetime):
        status, error_type, count = "degraded_empty", None, 0
        try:
            if not self._effective_enabled():
                status = "disabled"
                self.update_health(status, requested_at)
                return {
                    "provider_id": self.provider_id,
                    "status": status,
                    "checked_at": utc_text(requested_at),
                    "official_article_count_sample": 0,
                    "error_type": None,
                    "network_call_performed": False,
                }
            if self.connection is None:
                raise RuntimeError("database connection missing")
            table = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='articles'"
            ).fetchone()
            if not table:
                raise RuntimeError("articles table missing")
            rows = self.connection.execute(
                "SELECT canonical_url, url FROM articles WHERE status='active' "
                "ORDER BY id DESC LIMIT 200"
            ).fetchall()
            count = sum(
                1 for canonical, url in rows if self._official_domain(canonical or url or "")
            )
            status = "healthy" if count else "degraded_empty"
        except Exception as exc:
            status, error_type = "unhealthy", type(exc).__name__
        self.update_health(status, requested_at)
        return {
            "provider_id": self.provider_id,
            "status": status,
            "checked_at": utc_text(requested_at),
            "official_article_count_sample": count,
            "error_type": error_type,
            "network_call_performed": False,
        }

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sqlite3
import unittest
from datetime import datetime, timezone

from financial_instruments import InstrumentRegistry
from financial_provider_contract import (
    FinancialDataKind,
    FinancialDataRequest,
    InvalidSymbolError,
    PermissionDeniedError,
)
from financial_providers.official_evidence import OfficialEvidenceProvider
from financial_schema import ensure_financial_tables


NOW = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)


ARTICLE_SCHEMA = """
CREATE TABLE articles (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL,
    canonical_url TEXT,
    title TEXT,
    content TEXT,
    domain TEXT,
    publish_date TEXT,
    content_hash TEXT,
    first_crawled TEXT,
    created_at TEXT,
    configured_url TEXT,
    resolved_target_url TEXT,
    status TEXT
)
"""


class OfficialEvidenceProviderTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.execute("PRAGMA foreign_keys=ON")
        ensure_financial_tables(self.connection.cursor())
        self.connection.execute(ARTICLE_SCHEMA)
        self.registry = InstrumentRegistry(self.connection)
        self.registry.load_controlled_seed()
        self.settings = {
            "FINANCIAL_INTELLIGENCE_ENABLED": True,
            "OFFICIAL_FINANCIAL_EVIDENCE_ENABLED": True,
            "FINANCIAL_PROVIDER_DAILY_CALL_BUDGET": 100,
            "FINANCIAL_NEWS_FRESHNESS_SECONDS": 3600,
        }

    def tearDown(self):
        self.connection.close()

    def _insert(self, article_id, url):
        self.connection.execute(
            "INSERT INTO articles(id,url,canonical_url,title,content,domain,publish_date,"
            "content_hash,first_crawled,created_at,configured_url,resolved_target_url,status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                article_id,
                url,
                url,
                "SFC announces market update with index value 12345",
                "The official document contains market value 12345; verify separately.",
                url.split("/")[2],
                "2026-07-31",
                "a" * 64,
                "2026-07-31T12:00:00Z",
                "2026-07-31T12:00:00Z",
                "https://www.sfc.hk/en/News-and-announcements/News",
                url,
                "active",
            ),
        )

    def _request(self, article_id):
        instrument = self.registry.get_by_canonical_symbol("0700.HK")
        return FinancialDataRequest(
            request_id=f"official-{article_id}",
            endpoint="official_article",
            instrument_id=str(instrument.instrument_id),
            metric="document",
            data_kind=FinancialDataKind.NEWS,
            requested_as_of=NOW,
            preferred_provider_id="official_evidence",
            parameters={"article_id": article_id},
        )

    def test_official_article_reuses_existing_db_and_never_promotes_numbers_to_quote(self):
        self._insert(1, "https://www.sfc.hk/en/News-and-announcements/example")
        provider = OfficialEvidenceProvider(
            instrument_registry=self.registry,
            settings=self.settings,
            connection=self.connection,
            clock=lambda: NOW,
        )
        response = provider.fetch_and_persist(self._request(1))
        record = response.records[0]
        self.assertEqual(record.normalized_payload["article_id"], 1)
        self.assertEqual(record.normalized_payload["official_domain"], "www.sfc.hk")
        self.assertIn("official_domain_verified", record.quality_flags)
        self.assertIn(
            "numerical_claims_require_structured_snapshot_verification",
            record.quality_flags,
        )
        self.assertEqual(record.lineage["network_call_performed"], False)
        self.assertEqual(record.unit, "document")
        snapshot = self.connection.execute(
            "SELECT data_type, source_url FROM financial_data_snapshots"
        ).fetchone()
        self.assertEqual(snapshot[0], "news")
        self.assertTrue(snapshot[1].startswith("https://www.sfc.hk/"))

    def test_non_official_domain_is_rejected(self):
        self._insert(2, "https://sfc.hk.evil.example/fake")
        provider = OfficialEvidenceProvider(
            instrument_registry=self.registry,
            settings=self.settings,
            connection=self.connection,
            clock=lambda: NOW,
        )
        with self.assertRaises(InvalidSymbolError) as rejected:
            provider.fetch_validated(self._request(2))
        self.assertEqual(rejected.exception.details["gate_reason"], "non_official_domain")
        count = self.connection.execute(
            "SELECT COUNT(*) FROM financial_data_snapshots"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_instrument_registry_can_authorize_controlled_issuer_domain(self):
        self._insert(4, "https://www.tencent.com/en-us/investors/announcement.html")
        provider = OfficialEvidenceProvider(
            instrument_registry=self.registry,
            settings=self.settings,
            connection=self.connection,
            clock=lambda: NOW,
        )
        response = provider.fetch_validated(self._request(4))
        self.assertEqual(
            response.records[0].normalized_payload["official_domain"],
            "www.tencent.com",
        )
        self.assertIn("official_domain_verified", response.records[0].quality_flags)

    def test_disabled_gate_runs_before_articles_table_access(self):
        other = sqlite3.connect(":memory:", isolation_level=None)
        try:
            ensure_financial_tables(other.cursor())
            registry = InstrumentRegistry(other)
            registry.load_controlled_seed()
            provider = OfficialEvidenceProvider(
                instrument_registry=registry,
                settings={
                    "FINANCIAL_INTELLIGENCE_ENABLED": True,
                    "OFFICIAL_FINANCIAL_EVIDENCE_ENABLED": False,
                },
                connection=other,
                clock=lambda: NOW,
            )
            instrument = registry.get_by_canonical_symbol("0700.HK")
            request = FinancialDataRequest(
                request_id="disabled-official",
                endpoint="official_article",
                instrument_id=str(instrument.instrument_id),
                metric="document",
                data_kind=FinancialDataKind.NEWS,
                requested_as_of=NOW,
                preferred_provider_id="official_evidence",
                parameters={"article_id": 1},
            )
            with self.assertRaises(PermissionDeniedError):
                provider.fetch_validated(request)
        finally:
            other.close()

    def test_health_distinguishes_empty_and_official_article_store_without_network(self):
        provider = OfficialEvidenceProvider(
            instrument_registry=self.registry,
            settings=self.settings,
            connection=self.connection,
            clock=lambda: NOW,
        )
        empty = provider.health_probe(request_id="official-empty", requested_at=NOW)
        self.assertEqual(empty["status"], "degraded_empty")
        self._insert(3, "https://www.hkex.com.hk/News/example")
        healthy = provider.health_probe(request_id="official-health", requested_at=NOW)
        self.assertEqual(healthy["status"], "healthy")
        self.assertFalse(healthy["network_call_performed"])


if __name__ == "__main__":
    unittest.main()

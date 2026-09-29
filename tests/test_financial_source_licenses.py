#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import config
from financial_provider_contract import (
    AdjustmentMode,
    FinancialDataKind,
    FinancialDataProvider,
    FinancialDataRecord,
    FinancialDataRequest,
    FinancialProviderResponse,
    FreshnessState,
    MarketStatus,
    PermissionDeniedError,
    raw_response_hash,
)
from financial_provider_router import EMBEDDED_PROVIDER_IDS, FinancialProviderRouter
from financial_providers.base import RegisteredFinancialProvider
from financial_source_license import (
    FinancialSourceAuthorizationError,
    load_license_catalog,
    provider_authorization_decision,
    rss_authorization_decision,
)
from intel_light_scanner import IntelLightScanner
from intel_sources import IntelSourceRegistry
from sqlite_database import SQLiteDatabase
from tools.check_financial_source_licenses import endpoint_reconciliation


NOW = datetime(2026, 8, 2, 8, 0, tzinfo=timezone.utc)


class _DummyProvider(FinancialDataProvider):
    def __init__(self, provider_id):
        self._provider_id = provider_id

    @property
    def provider_id(self):
        return self._provider_id

    @property
    def license_profile(self):
        return f"{self.provider_id}_fixture"

    @property
    def capabilities(self):
        return (FinancialDataKind.QUOTE,)

    def fetch(self, request):
        record = FinancialDataRecord(
            instrument_id=request.instrument_id,
            metric=request.metric,
            value=1.0,
            unit="price",
            currency="CNY",
            market_status=MarketStatus.UNKNOWN,
            observed_at=NOW,
            fetched_at=NOW,
            timezone="UTC",
            freshness_state=FreshnessState.UNKNOWN,
            requested_as_of=request.requested_as_of,
            raw_response_hash=raw_response_hash({"value": 1}),
            normalized_payload={"value": 1},
            adjustment=AdjustmentMode.RAW,
        )
        return FinancialProviderResponse(
            provider_id=self.provider_id,
            endpoint=request.endpoint,
            license_profile=self.license_profile,
            request_id=request.request_id,
            data_kind=request.data_kind,
            records=(record,),
        )


class _DirectTushareProvider(RegisteredFinancialProvider):
    provider_key = "tushare_cn"

    def fetch(self, request):
        raise AssertionError("authorization must run before transport")


class FinancialSourceLicenseTest(unittest.TestCase):
    def setUp(self):
        self.request = FinancialDataRequest(
            request_id="license-gate",
            endpoint="quote",
            instrument_id="1",
            metric="last_price",
            data_kind=FinancialDataKind.QUOTE,
            requested_as_of=NOW,
            preferred_provider_id="tushare_cn",
        )

    @staticmethod
    def _production_settings(*, approvals=""):
        return {
            "FINANCIAL_LICENSE_ENVIRONMENT": "production",
            "FINANCIAL_SOURCE_LICENSE_APPROVALS": approvals,
            "FINANCIAL_INTELLIGENCE_ENABLED": True,
            "TUSHARE_CN_ENABLED": True,
            "TUSHARE_TOKEN": "configured-test-token",
            "YAHOO_FINANCE_ENABLED": True,
        }

    def test_catalog_covers_every_embedded_provider_and_five_financial_rss(self):
        catalog = load_license_catalog()
        profiles = catalog["profiles"]
        providers = {
            item["source_id"] for item in profiles.values()
            if item["source_type"] == "provider"
        }
        rss = [item for item in profiles.values() if item["source_type"] == "rss"]
        self.assertEqual(providers, set(EMBEDDED_PROVIDER_IDS))
        self.assertEqual(len(rss), 5)
        self.assertTrue(all(item["permissions"]["redistribute"] is False for item in profiles.values()))

    def test_configured_provider_and_rss_endpoints_match_implementation(self):
        report = endpoint_reconciliation()
        self.assertEqual(set(report["providers"]), set(EMBEDDED_PROVIDER_IDS))
        self.assertEqual(len(report["rss"]), 5)
        self.assertTrue(all(item["matched"] for item in report["rss"].values()))
        self.assertFalse(any(
            item["missing_implementation_markers"]
            for item in report["providers"].values()
        ))

    def test_production_missing_approval_blocks_before_provider_construction(self):
        calls = []
        router = FinancialProviderRouter(
            settings=self._production_settings(),
            factories={"tushare_cn": lambda: calls.append("called")},
            clock=lambda: NOW,
        )
        with self.assertRaises(PermissionDeniedError) as raised:
            router.fetch(
                self.request,
                candidate_provider_ids=("tushare_cn",),
                allow_fallback=False,
            )
        self.assertEqual(calls, [])
        self.assertEqual(raised.exception.details["gate_reason"], "production_approval_missing")

    def test_explicit_production_approval_allows_entitled_provider(self):
        provider = _DummyProvider("tushare_cn")
        router = FinancialProviderRouter(
            settings=self._production_settings(approvals="tushare_cn"),
            factories={"tushare_cn": lambda: provider},
            clock=lambda: NOW,
        )
        response = router.fetch(
            self.request,
            candidate_provider_ids=("tushare_cn",),
            allow_fallback=False,
        )
        self.assertEqual(response.provider_id, "tushare_cn")

    def test_personal_or_academic_sources_remain_production_forbidden(self):
        for provider_id in ("akshare_cn", "yahoo", "easyquotation"):
            decision = provider_authorization_decision(
                provider_id,
                self._production_settings(approvals=provider_id),
                at=NOW,
            )
            self.assertFalse(decision["authorized"], provider_id)
            self.assertEqual(decision["reason"], "production_use_forbidden")

    def test_expired_review_is_disabled_even_with_production_approval(self):
        decision = provider_authorization_decision(
            "tushare_cn",
            self._production_settings(approvals="tushare_cn"),
            at=datetime(2027, 2, 2, tzinfo=timezone.utc),
        )
        self.assertFalse(decision["authorized"])
        self.assertEqual(decision["reason"], "license_review_expired")

    def test_direct_provider_use_cannot_bypass_production_authorization(self):
        provider = _DirectTushareProvider(
            instrument_registry=object(),
            settings=self._production_settings(),
            clock=lambda: NOW,
        )
        with self.assertRaises(PermissionDeniedError) as raised:
            provider._require_enabled(self.request, captured_at=NOW)
        self.assertEqual(raised.exception.details["gate_reason"], "production_approval_missing")

    def test_financial_rss_metadata_is_profiled_and_missing_profile_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = SQLiteDatabase(str(Path(directory) / "sources.sqlite3"))
            self.assertTrue(database.connect())
            self.assertTrue(database.create_tables())
            registry = IntelSourceRegistry(database)
            registry.ensure_pack_default_sources("financial_markets")
            sources = registry.list_sources(
                industry_pack_id="financial_markets", source_type="rss",
                page=1, per_page=100,
            )[0]
            self.assertEqual(len(sources), 5)
            self.assertTrue(all(item["metadata"]["license_profile"] for item in sources))

            cursor = database.connection.execute(
                """
                INSERT INTO intel_sources(
                    canonical_source_url, source_url, source_name, source_type,
                    is_enabled, metadata_json
                ) VALUES('https://unprofiled.invalid/feed.xml',
                         'https://unprofiled.invalid/feed.xml', 'unprofiled',
                         'rss', 0, ?)
                """,
                (json.dumps({"origin_pack_id": "financial_markets"}),),
            )
            source_id = int(cursor.lastrowid)
            database.connection.commit()
            with patch.object(config, "FINANCIAL_LICENSE_ENVIRONMENT", "production"), patch.object(
                config, "FINANCIAL_SOURCE_LICENSE_APPROVALS", ""
            ):
                with self.assertRaises(FinancialSourceAuthorizationError):
                    registry.update_source(source_id, is_enabled=True)
                scanner = IntelLightScanner(source_registry=registry)
                self.assertEqual(
                    rss_authorization_decision(
                        "https://unprofiled.invalid/feed.xml", "", config, at=NOW
                    )["reason"],
                    "license_profile_missing",
                )
            database.disconnect()

    def test_pack_registration_disables_unapproved_production_rss(self):
        with tempfile.TemporaryDirectory() as directory:
            database = SQLiteDatabase(str(Path(directory) / "registration.sqlite3"))
            self.assertTrue(database.connect())
            self.assertTrue(database.create_tables())
            registry = IntelSourceRegistry(database)
            with patch.object(
                config, "FINANCIAL_LICENSE_ENVIRONMENT", "production"
            ), patch.object(config, "FINANCIAL_SOURCE_LICENSE_APPROVALS", ""):
                registry.ensure_pack_default_sources("financial_markets")
                sources = registry.list_sources(
                    industry_pack_id="financial_markets",
                    source_type="rss",
                    page=1,
                    per_page=100,
                )[0]
                self.assertEqual(len(sources), 5)
                self.assertTrue(all(not item["is_enabled"] for item in sources))
                scanner = IntelLightScanner(source_registry=registry)
                self.assertEqual(
                    scanner._enabled_sources("financial_markets", None, 100), []
                )
            database.disconnect()

    def test_profiled_rss_still_requires_explicit_production_approval(self):
        catalog = load_license_catalog()
        profile = catalog["profiles"]["hkma_press_release_public_rss"]
        missing = rss_authorization_decision(
            profile["source_url"], profile["profile_id"],
            {"FINANCIAL_LICENSE_ENVIRONMENT": "production"}, at=NOW,
        )
        approved = rss_authorization_decision(
            profile["source_url"], profile["profile_id"],
            {
                "FINANCIAL_LICENSE_ENVIRONMENT": "production",
                "FINANCIAL_SOURCE_LICENSE_APPROVALS": profile["profile_id"],
            },
            at=NOW,
        )
        self.assertEqual(missing["reason"], "production_approval_missing")
        self.assertTrue(approved["authorized"])


if __name__ == "__main__":
    unittest.main()

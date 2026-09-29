import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from financial_instrument_discovery import (
    CatalogInstrumentDiscoverySource,
    FinancialInstrumentDiscoveryService,
    extract_instrument_entity,
)
from financial_instruments import InstrumentRegistry
from sqlite_database import SQLiteDatabase


UTC = timezone.utc
NOW = datetime(2026, 8, 3, 4, 0, tzinfo=UTC)
ENABLED = {
    "FINANCIAL_INTELLIGENCE_ENABLED": True,
    "FINANCIAL_INSTRUMENT_DISCOVERY_ENABLED": True,
    "FINANCIAL_INSTRUMENT_AUTO_PROMOTION_ENABLED": True,
}


class _FailingSource:
    source_key = "timed_out_provider"

    def search(self, query, *, requested_at, request_id):
        raise TimeoutError("fixture timeout")


class FinancialInstrumentDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(
            str(Path(self.temp_dir.name) / "instrument-discovery.sqlite3")
        )
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.connection = self.database.connection

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _service(self, *, assertions=None, settings=None, extra_sources=()):
        sources = (
            CatalogInstrumentDiscoverySource(assertions)
            if assertions is not None
            else CatalogInstrumentDiscoverySource.from_path()
        ,)
        return FinancialInstrumentDiscoveryService(
            self.connection,
            sources=(*sources, *extra_sources),
            settings=settings or ENABLED,
        )

    def _discover(self, service=None, question="SpaceX 股票最新信息", request_id="route-1"):
        return (service or self._service()).discover_and_promote(
            question,
            requested_at=NOW,
            request_id=request_id,
        )

    def test_spacex_is_verified_promoted_and_resolvable_without_seed_edit(self):
        result = self._discover()

        self.assertEqual(result["status"], "promoted")
        self.assertEqual(result["promoted_target"]["canonical_symbol"], "SPCX.US")
        self.assertEqual(result["promoted_target"]["exchange"], "XNAS")
        self.assertEqual(result["promoted_target"]["currency"], "USD")
        resolution = InstrumentRegistry(self.connection).resolve("SpaceX", as_of="2026-08-03")
        self.assertEqual(resolution.status, "resolved")
        self.assertEqual(resolution.candidates[0].instrument.canonical_symbol, "SPCX.US")

    def test_repeated_and_parallel_requests_are_idempotent(self):
        service = self._service()
        results = []

        def run(index):
            results.append(self._discover(service, request_id=f"parallel-{index}"))

        threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual([item["status"] for item in results], ["promoted", "promoted"])
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM financial_instruments WHERE canonical_symbol='SPCX.US'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM financial_instrument_candidates WHERE canonical_symbol='SPCX.US'"
            ).fetchone()[0],
            1,
        )

    def test_expired_historical_symbol_owner_is_not_promoted(self):
        current = list(CatalogInstrumentDiscoverySource.from_path().assertions)
        historical = {
            **current[0],
            "source_key": "historical_etf_directory",
            "canonical_symbol": "SPCK.US",
            "display_name": "The SPAC and New Issue ETF",
            "asset_type": "etf",
            "expires_at": "2022-01-01T00:00:00Z",
            "aliases": ["SPCX", "SPCK"],
        }
        result = self._discover(self._service(assertions=[historical, *current]))
        self.assertEqual(result["promoted_target"]["canonical_symbol"], "SPCX.US")

    def test_unknown_symbol_is_not_written_to_formal_registry(self):
        result = self._discover(question="SPCXQ 股票最新信息")
        self.assertEqual(result["status"], "not_found")
        self.assertIsNone(InstrumentRegistry(self.connection).get_by_canonical_symbol("SPCXQ.US"))

    def test_multiple_current_candidates_require_verification(self):
        assertions = list(CatalogInstrumentDiscoverySource.from_path().assertions)
        second = [{**item, "canonical_symbol": "SPCX.CA", "market": "CA", "exchange": "XTSE"}
                  for item in assertions]
        result = self._discover(self._service(assertions=[*assertions, *second]))
        self.assertEqual(result["status"], "verification_required")
        self.assertIn("multiple_current_candidates", result["reason_codes"])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM financial_instruments").fetchone()[0],
            0,
        )

    def test_provider_mapping_conflict_rejects_candidate(self):
        assertions = list(CatalogInstrumentDiscoverySource.from_path().assertions)
        conflict = {
            **assertions[-1],
            "source_key": "second_yahoo_review",
            "provider_symbol": "WRONG",
        }
        result = self._discover(self._service(assertions=[*assertions, conflict]))
        self.assertEqual(result["status"], "rejected")
        self.assertIn("provider_mapping_conflict", result["reason_codes"])
        self.assertIsNone(InstrumentRegistry(self.connection).get_by_canonical_symbol("SPCX.US"))

    def test_web_hint_alone_never_promotes(self):
        base = CatalogInstrumentDiscoverySource.from_path().assertions[0]
        web_hint = {
            **base,
            "source_key": "ordinary_web_search",
            "source_role": "web_hint",
            "source_type": "search_engine",
        }
        result = self._discover(self._service(assertions=[web_hint]))
        self.assertEqual(result["status"], "rejected")
        self.assertIn("authoritative_source_required", result["reason_codes"])
        self.assertIsNone(InstrumentRegistry(self.connection).get_by_canonical_symbol("SPCX.US"))

    def test_provider_timeout_keeps_candidate_out_of_formal_registry(self):
        assertions = [
            item
            for item in CatalogInstrumentDiscoverySource.from_path().assertions
            if item.get("source_role") != "provider"
        ]
        service = self._service(assertions=assertions, extra_sources=(_FailingSource(),))
        result = self._discover(service)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("approved_provider_mapping_required", result["reason_codes"])
        self.assertEqual(result["failed_sources"], ["timed_out_provider"])

    def test_auto_promotion_switch_keeps_verified_candidate_only(self):
        service = self._service(
            settings={**ENABLED, "FINANCIAL_INSTRUMENT_AUTO_PROMOTION_ENABLED": False}
        )
        result = self._discover(service)
        self.assertEqual(result["status"], "verified")
        self.assertIn("auto_promotion_disabled", result["reason_codes"])
        self.assertIsNone(InstrumentRegistry(self.connection).get_by_canonical_symbol("SPCX.US"))

    def test_gray2_repeated_discovery_is_idempotent_and_never_auto_promotes(self):
        service = self._service(
            settings={**ENABLED, "FINANCIAL_INSTRUMENT_AUTO_PROMOTION_ENABLED": False}
        )

        first = self._discover(service, request_id="gray2-first")
        second = self._discover(service, request_id="gray2-second")

        self.assertEqual([first["status"], second["status"]], ["verified", "verified"])
        candidate = self.connection.execute(
            """
            SELECT status, promoted_instrument_id, last_request_id
            FROM financial_instrument_candidates
            WHERE canonical_symbol='SPCX.US'
            """
        ).fetchone()
        self.assertEqual(candidate[0], "verified")
        self.assertIsNone(candidate[1])
        self.assertEqual(candidate[2], "gray2-second")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM financial_instrument_candidates "
                "WHERE canonical_symbol='SPCX.US'"
            ).fetchone()[0],
            1,
        )
        self.assertIsNone(InstrumentRegistry(self.connection).get_by_canonical_symbol("SPCX.US"))

    def test_entity_extraction_uses_only_user_text(self):
        self.assertEqual(extract_instrument_entity("请帮我查询 SpaceX 股票最新信息"), "SpaceX")
        self.assertEqual(extract_instrument_entity("SPCXQ 股票最新信息"), "SPCXQ")
        self.assertEqual(extract_instrument_entity("港股 9969.HK 最新情况"), "9969.HK")
        self.assertEqual(extract_instrument_entity("3119.HK 基金走势"), "3119.HK")
        self.assertEqual(extract_instrument_entity("NUVB 美股最新情况"), "NUVB")
        self.assertEqual(
            extract_instrument_entity("分析 NUVB.US 的基本面和风险"),
            "NUVB.US",
        )
        self.assertEqual(extract_instrument_entity(""), "")


if __name__ == "__main__":
    unittest.main()

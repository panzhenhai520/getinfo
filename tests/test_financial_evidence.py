import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from financial_evidence import (
    DOCUMENT_KIND,
    STRUCTURED_KIND,
    EvidenceResolver,
    _article_time_interval,
)
from sqlite_database import SQLiteDatabase


NOW = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
START = datetime(2026, 7, 30, 0, 0, tzinfo=timezone.utc)


class _FaultInjectingResolver(EvidenceResolver):
    fail_snapshots = False
    fail_articles = False

    def _query_snapshots(self, instrument_ids, universe_id):
        if self.fail_snapshots:
            raise sqlite3.OperationalError("simulated snapshot store outage")
        return super()._query_snapshots(instrument_ids, universe_id)

    def _query_articles(self):
        if self.fail_articles:
            raise sqlite3.OperationalError("simulated article store outage")
        return super()._query_articles()


class FinancialEvidenceResolverTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(str(Path(self.temp_dir.name) / "evidence.sqlite3"))
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.connection = self.database.connection
        self.tencent_id = self._instrument(
            "0700.HK",
            "腾讯控股",
            "HK",
            {"akshare_cn": "00700", "yahoo": "0700.HK"},
            ("腾讯", "騰訊", "腾讯控股有限公司"),
        )
        self.apple_id = self._instrument(
            "AAPL.US",
            "Apple Inc.",
            "US",
            {"yahoo": "AAPL"},
            ("Apple", "苹果公司"),
        )
        self.provider_id = int(
            self.connection.execute(
                """
                INSERT INTO financial_provider_profiles(
                    provider_key, display_name, provider_type, is_enabled
                ) VALUES('akshare_cn', 'AKShare CN', 'market_data', 1)
                """
            ).lastrowid
        )

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _instrument(self, symbol, name, market, mappings, aliases):
        instrument_id = int(
            self.connection.execute(
                """
                INSERT INTO financial_instruments(
                    canonical_symbol, display_name, asset_type, market,
                    provider_mappings_json
                ) VALUES(?, ?, 'equity', ?, ?)
                """,
                (symbol, name, market, json.dumps(mappings)),
            ).lastrowid
        )
        for alias in aliases:
            self.connection.execute(
                """
                INSERT INTO financial_instrument_aliases(
                    instrument_id, alias, alias_normalized, market
                ) VALUES(?, ?, ?, ?)
                """,
                (instrument_id, alias, alias.casefold(), market),
            )
        return instrument_id

    def _run(self, run_id="run-tencent", *, instrument_id=None, universe_id=None):
        scope = "instrument" if instrument_id is not None else "universe"
        self.connection.execute(
            """
            INSERT INTO financial_research_runs(
                id, trigger_type, scope_type, instrument_id, universe_id,
                requested_at
            ) VALUES(?, 'test', ?, ?, ?, '2026-07-31T08:00:00.000Z')
            """,
            (run_id, scope, instrument_id, universe_id),
        )
        return run_id

    def _snapshot(self, instrument_id, *, observed_at="2026-07-31T07:59:00.000Z"):
        payload = json.dumps(
            {"metric": "last_price", "value": 555.0, "currency": "HKD"},
            sort_keys=True,
        )
        return int(
            self.connection.execute(
                """
                INSERT INTO financial_data_snapshots(
                    snapshot_key, instrument_id, provider_profile_id, data_type,
                    observed_at, fetched_at, payload_json, payload_sha256,
                    source_url, quality_status
                ) VALUES(?, ?, ?, 'quote', ?, '2026-07-31T07:59:05.000Z', ?, ?,
                         'https://quote.example.test/0700', 'normalized_fresh')
                """,
                (
                    f"snapshot-{instrument_id}-{observed_at}",
                    instrument_id,
                    self.provider_id,
                    observed_at,
                    payload,
                    hashlib.sha256(payload.encode()).hexdigest(),
                ),
            ).lastrowid
        )

    def _article(
        self,
        url,
        title,
        content,
        *,
        canonical_url=None,
        publish_date="2026-07-31",
        domain="issuer.example.test",
    ):
        return int(
            self.connection.execute(
                """
                INSERT INTO articles(
                    url, canonical_url, title, content, domain, publish_date,
                    first_crawled, status, matched_keywords
                ) VALUES(?, ?, ?, ?, ?, ?, '2026-07-31T07:50:00Z', 'active', '')
                """,
                (url, canonical_url or url, title, content, domain, publish_date),
            ).lastrowid
        )

    def _resolver(self, cls=EvidenceResolver):
        return cls(self.connection, clock=lambda: NOW)

    def test_company_announcement_and_quote_are_parallel_typed_evidence(self):
        run_id = self._run(instrument_id=self.tencent_id)
        snapshot_id = self._snapshot(self.tencent_id)
        article_id = self._article(
            "https://issuer.example.test/announcement/1",
            "腾讯控股公布季度业绩，收市价555港元",
            "腾讯控股有限公司发布季度公告。",
        )

        result = self._resolver().resolve_for_run(run_id, start_at=START, end_at=NOW)

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["pipelines"]["snapshots"]["item_count"], 1)
        self.assertEqual(result["pipelines"]["articles"]["item_count"], 1)
        quote = result["structured_evidence"][0]
        document = result["document_evidence"][0]
        self.assertEqual(quote["reference_id"], snapshot_id)
        self.assertEqual(document["reference_id"], article_id)
        self.assertIn("structured_market_metric", quote["claim_capabilities"])
        self.assertIn("announcement_text", quote["denied_claim_capabilities"])
        self.assertIn("live_price", document["denied_claim_capabilities"])
        self.assertTrue(result["evidence_contract"]["articles_are_not_quotes"])

        listed = self._resolver().list_for_run(run_id)
        self.assertEqual(listed["structured_evidence"][0]["payload"]["value"], 555.0)
        self.assertIn("季度公告", listed["document_evidence"][0]["content"])
        metadata = listed["document_evidence"][0]["metadata"]
        self.assertFalse(metadata["content_copied"])
        self.assertFalse(metadata["payload_copied"])

    def test_name_only_news_matches_without_ticker_and_expired_news_is_excluded(self):
        run_id = self._run(instrument_id=self.tencent_id)
        current_id = self._article(
            "https://news.example.test/current",
            "腾讯控股推出新服务",
            "公司今天公布产品计划，正文没有证券代码。",
            domain="news.example.test",
        )
        self._article(
            "https://news.example.test/expired",
            "腾讯控股十年前的报道",
            "历史新闻。",
            publish_date="2016-01-01",
            domain="news.example.test",
        )

        result = self._resolver().resolve_for_run(run_id, start_at=START, end_at=NOW)

        self.assertEqual([item["reference_id"] for item in result["document_evidence"]], [current_id])
        self.assertTrue(result["document_evidence"][0]["match_method"].startswith("title_entity"))
        self.assertIn("腾讯控股", result["document_evidence"][0]["match_terms"])

    def test_publication_precision_and_source_timezone_are_preserved(self):
        instant = _article_time_interval(
            {
                "published_at_utc": "2026-03-09T13:30:00Z",
                "published_precision": "datetime",
                "published_timezone": "America/New_York",
            }
        )
        self.assertEqual(instant[0].isoformat(), "2026-03-09T13:30:00+00:00")
        self.assertEqual(instant[2], "published_at_utc")

        date_only = _article_time_interval(
            {
                "published_at_utc": "2026-03-09",
                "published_precision": "date",
                "published_timezone": "America/New_York",
            }
        )
        self.assertEqual(date_only[0].isoformat(), "2026-03-09T04:00:00+00:00")
        self.assertEqual(date_only[1].isoformat(), "2026-03-10T03:59:59.999999+00:00")
        self.assertEqual(date_only[2], "published_at_date")

    def test_one_multi_company_document_links_to_each_universe_member(self):
        universe_id = int(
            self.connection.execute(
                """
                INSERT INTO financial_universes(
                    universe_key, display_name, universe_type, market
                ) VALUES('global-tech', '全球科技', 'watchlist', 'GLOBAL')
                """
            ).lastrowid
        )
        for instrument_id in (self.tencent_id, self.apple_id):
            self.connection.execute(
                """
                INSERT INTO financial_universe_members(
                    universe_id, instrument_id, effective_from
                ) VALUES(?, ?, '2026-01-01')
                """,
                (universe_id, instrument_id),
            )
        run_id = self._run("run-universe", universe_id=universe_id)
        article_id = self._article(
            "https://news.example.test/tencent-apple",
            "腾讯与Apple扩大合作",
            "腾讯控股和苹果公司发布联合消息。",
            domain="news.example.test",
        )

        result = self._resolver().resolve_for_run(run_id, start_at=START, end_at=NOW)

        self.assertEqual(len(result["document_evidence"]), 2)
        self.assertEqual(
            {item["instrument_id"] for item in result["document_evidence"]},
            {self.tencent_id, self.apple_id},
        )
        self.assertEqual({item["reference_id"] for item in result["document_evidence"]}, {article_id})

    def test_duplicate_canonical_url_is_one_document_per_instrument(self):
        run_id = self._run(instrument_id=self.tencent_id)
        canonical = "https://news.example.test/company/result"
        first_id = self._article(
            f"{canonical}?utm_source=rss",
            "腾讯控股发布业绩",
            "腾讯控股公告。",
            canonical_url=canonical,
            domain="news.example.test",
        )
        second_id = self._article(
            f"{canonical}?utm_source=web",
            "腾讯控股发布业绩（网页版本）",
            "腾讯控股公告。",
            canonical_url=canonical,
            domain="news.example.test",
        )

        result = self._resolver().resolve_for_run(run_id, start_at=START, end_at=NOW)

        self.assertEqual(len(result["document_evidence"]), 1)
        self.assertEqual(result["document_evidence"][0]["reference_id"], max(first_id, second_id))
        self.assertEqual(result["persisted_counts"][DOCUMENT_KIND], 1)

    def test_explicit_targets_cannot_expand_an_instrument_run_scope(self):
        run_id = self._run(instrument_id=self.tencent_id)

        with self.assertRaisesRegex(ValueError, "cannot expand"):
            self._resolver().resolve_for_run(
                run_id,
                start_at=START,
                end_at=NOW,
                instrument_ids=(self.tencent_id, self.apple_id),
            )

    def test_each_pipeline_fails_and_recovers_without_deleting_other_evidence(self):
        run_id = self._run(instrument_id=self.tencent_id)
        self._snapshot(self.tencent_id)
        self._article(
            "https://issuer.example.test/recovery",
            "腾讯控股恢复测试公告",
            "腾讯控股公告。",
        )
        resolver = self._resolver(_FaultInjectingResolver)
        baseline = resolver.resolve_for_run(run_id, start_at=START, end_at=NOW)
        self.assertEqual(baseline["persisted_counts"], {STRUCTURED_KIND: 1, DOCUMENT_KIND: 1})

        resolver.fail_snapshots = True
        snapshot_outage = resolver.resolve_for_run(run_id, start_at=START, end_at=NOW)
        self.assertEqual(snapshot_outage["status"], "partial")
        self.assertEqual(snapshot_outage["pipelines"]["snapshots"]["error_code"], "snapshot_query_failed")
        self.assertEqual(snapshot_outage["persisted_counts"], {STRUCTURED_KIND: 1, DOCUMENT_KIND: 1})

        resolver.fail_snapshots = False
        resolver.fail_articles = True
        article_outage = resolver.resolve_for_run(run_id, start_at=START, end_at=NOW)
        self.assertEqual(article_outage["status"], "partial")
        self.assertEqual(article_outage["pipelines"]["articles"]["error_code"], "article_query_failed")
        self.assertEqual(article_outage["persisted_counts"], {STRUCTURED_KIND: 1, DOCUMENT_KIND: 1})

        resolver.fail_articles = False
        recovered = resolver.resolve_for_run(run_id, start_at=START, end_at=NOW)
        self.assertEqual(recovered["status"], "complete")
        self.assertEqual(recovered["persisted_counts"], {STRUCTURED_KIND: 1, DOCUMENT_KIND: 1})


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from financial_instruments import InstrumentRegistry
from financial_news_query import FinancialNewsQueryService
from financial_news_refresh import FinancialNewsRefreshCoordinator
from financial_official_news import (
    FinancialOfficialNewsRepository,
    HKEXNewsTitleSearchClient,
)
from sqlite_database import SQLiteDatabase


UTC = timezone.utc
NOW = datetime(2026, 8, 5, 7, 0, tzinfo=UTC)


class _Response:
    def __init__(self, text):
        self.text = text


class _HKEXHTTP:
    def __init__(self):
        self.urls = []

    def get(self, url, *, headers=None):
        self.urls.append((url, dict(headers or {})))
        if "prefix.do" in url:
            return _Response(
                'callback({"stockInfo":['
                '{"stockId":289,"code":"00165","name":"CHINA EB LTD"},'
                '{"stockId":999,"code":"01650","name":"OTHER"}]});'
            )
        return _Response(
            """
            <table><tbody>
              <tr>
                <td class="release-time">Release Time: 06/08/2026 08:00</td>
                <td class="stock-short-code">Stock Code: 00165</td>
                <td class="stock-short-name">Stock Short Name: CHINA EB LTD</td>
                <td><div class="doc-link"><a href="/listedco/listconews/sehk/2026/0806/future.pdf">FUTURE</a></div></td>
              </tr>
              <tr>
                <td class="release-time">Release Time: 03/08/2026 15:14</td>
                <td class="stock-short-code">Stock Code: 00165</td>
                <td class="stock-short-name">Stock Short Name: CHINA EB LTD</td>
                <td><div class="headline">Monthly Returns</div><div class="doc-link"><a href="/listedco/listconews/sehk/2026/0803/latest.pdf">MONTHLY RETURN</a></div></td>
              </tr>
              <tr>
                <td class="release-time">Release Time: 30/07/2026 18:20</td>
                <td class="stock-short-code">Stock Code: 00165</td>
                <td class="stock-short-name">Stock Short Name: CHINA EB LTD</td>
                <td><div class="headline">Inside Information</div><div class="doc-link"><a href="/listedco/listconews/sehk/2026/0730/profit.pdf">PROFIT WARNING</a></div></td>
              </tr>
              <tr>
                <td class="release-time">Release Time: 01/01/2020 12:00</td>
                <td class="stock-short-code">Stock Code: 00999</td>
                <td><div class="doc-link"><a href="/listedco/listconews/sehk/2020/0101/wrong.pdf">WRONG STOCK</a></div></td>
              </tr>
            </tbody></table>
            """
        )


class FinancialOfficialNewsTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(
            str(Path(self.temp_dir.name) / "official-news.sqlite3")
        )
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.target = InstrumentRegistry(self.database.connection).upsert_instrument(
            {
                "canonical_symbol": "0165.HK",
                "display_name": "CHINA EB LTD",
                "asset_type": "equity",
                "market": "XHKG",
                "exchange": "XHKG",
                "currency": "HKD",
                "country_code": "HK",
                "aliases": ["0165", "00165", "中国光大控股"],
            }
        ).to_dict()

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def test_hkex_client_resolves_exact_code_and_orders_only_eligible_rows(self):
        http = _HKEXHTTP()
        client = HKEXNewsTitleSearchClient(http_client=http)

        items = client.search(
            "0165.HK",
            cutoff=NOW,
            start=datetime(2026, 7, 29, tzinfo=UTC),
        )

        self.assertEqual([item["title"] for item in items], ["MONTHLY RETURN", "PROFIT WARNING"])
        self.assertEqual(items[0]["stock_code"], "00165")
        self.assertEqual(
            items[0]["url"],
            "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0803/latest.pdf",
        )
        self.assertIn("stockId=289", items[0]["source_url"])
        self.assertEqual(items[0]["published_at"], "2026-08-03T07:14:00.000Z")

    def test_repository_hashes_and_idempotently_updates_official_metadata(self):
        item = {
            "url": "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0730/profit.pdf",
            "source_url": "https://www1.hkexnews.hk/search/titlesearch.xhtml?stockId=289",
            "title": "PROFIT WARNING",
            "summary": "Stock Code: 00165；Stock Short Name: CHINA EB LTD",
            "stock_code": "00165",
            "stock_name": "CHINA EB LTD",
            "published_at": "2026-07-30T10:20:00.000Z",
            "published_timezone": "Asia/Hong_Kong",
        }
        repository = FinancialOfficialNewsRepository(self.database)

        first_id, first_created = repository.upsert(
            instrument_id=self.target["instrument_id"],
            source_id=7,
            source_key="HKEXnews",
            item=item,
            fetched_at=NOW,
        )
        second_id, second_created = repository.upsert(
            instrument_id=self.target["instrument_id"],
            source_id=7,
            source_key="HKEXnews",
            item=item,
            fetched_at=NOW,
        )

        self.assertEqual(first_id, second_id)
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        row = self.database.connection.execute(
            "SELECT quality_status, payload_json, payload_sha256 "
            "FROM financial_official_news_items WHERE id=?",
            (first_id,),
        ).fetchone()
        self.assertEqual(row[0], "verified_official_metadata")
        self.assertEqual(len(row[2]), 64)

    def test_coordinator_persists_last_official_item_and_query_returns_it(self):
        class OfficialClient:
            calls = []

            def search(self, _symbol, *, cutoff, start, max_items):
                self.calls.append((cutoff, start, max_items))
                return [{
                    "url": "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0720/profit.pdf",
                    "source_url": "https://www1.hkexnews.hk/search/titlesearch.xhtml?stockId=289",
                    "title": "PROFIT WARNING",
                    "summary": "Stock Code: 00165；Stock Short Name: CHINA EB LTD",
                    "stock_code": "00165",
                    "stock_name": "CHINA EB LTD",
                    "published_at": "2026-07-20T10:20:00.000Z",
                    "published_timezone": "Asia/Hong_Kong",
                }]

        class Search:
            def search(self, *_args, **_kwargs):
                return []

        client = OfficialClient()
        coordinator = FinancialNewsRefreshCoordinator(
            self.database,
            settings={
                "FINANCIAL_NEWS_DISCOVERY_MAX_SOURCES": 4,
                "FINANCIAL_NEWS_DISCOVERY_MAX_CANDIDATES": 12,
                "FINANCIAL_NEWS_REFRESH_TIMEOUT_SECONDS": 10,
            },
            official_news_client=client,
            search_client=Search(),
            clock=lambda: NOW,
        )

        refreshed = coordinator.refresh(
            target=self.target,
            cutoff_at_utc="2026-08-05T07:00:00Z",
            lookback_days=7,
            latest_available_only=True,
        )
        service = FinancialNewsQueryService(
            self.database,
            settings={
                "FINANCIAL_INTELLIGENCE_ENABLED": True,
                "FINANCIAL_LATEST_NEWS_ENABLED": True,
                "FINANCIAL_NEWS_LOOKBACK_DAYS": 7,
            },
            clock=lambda: NOW,
        )
        plan = service.plan(
            {"status": "resolved", "targets": [self.target]},
            {
                "server_now_utc": "2026-08-05T07:00:00Z",
                "server_timezone": "Asia/Hong_Kong",
                "user_timezone": "Asia/Hong_Kong",
            },
            {"channels": ["news"]},
        )
        result = service.execute(plan)

        self.assertEqual(refreshed["inserted_count"], 1, refreshed)
        self.assertIn(
            "controlled_hkexnews_latest_available_completed",
            refreshed["reason_codes"],
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["evidence"][0]["title"], "PROFIT WARNING")
        self.assertEqual(result["evidence"][0]["match_method"], "official_stock_code")
        self.assertEqual(result["evidence"][0]["recency_status"], "latest_available")


if __name__ == "__main__":
    unittest.main()

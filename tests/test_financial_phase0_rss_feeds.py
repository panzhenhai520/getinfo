#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import Mock, patch

_BOOTSTRAP_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = os.path.join(
    _BOOTSTRAP_TEMP_DIR.name,
    "bootstrap.sqlite3",
)
os.environ["INTEL_LLM_ENABLED"] = "false"
os.environ["CRAWL_REQUIRE_KEYWORD_MATCH"] = "false"

from industry_packs import IndustryPackLoader
from intel_sources import IntelSourceRegistry
from intel_http import ExternalFetchError, HTTPFetchResult, SafeHTTPClient, UnsafeExternalURLError
from rss_feed_contract import RSSFeedContractError, parse_rss_feed, validate_rss_feed_response
from sqlite_database import SQLiteDatabase


RSS_XML = b"""<?xml version="1.0"?>
<rss><channel><item><title>Market policy update</title>
<link>/news/market-policy</link><description><![CDATA[<p>Policy summary</p>]]></description>
<pubDate>Fri, 31 Jul 2026 09:00:00 +0800</pubDate></item></channel></rss>"""
ATOM_XML = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Index update</title>
<link href="https://official.example/index-update"/><summary>Index summary</summary>
<updated>2026-07-31T01:00:00Z</updated></entry></feed>"""


def _response(content=RSS_XML, content_type="application/rss+xml", status=200):
    return HTTPFetchResult(
        "https://official.example/feed.xml", status, content, content_type, "utf-8"
    )


class _HTTPResponse:
    def __init__(self, status=200, *, content=b"ok", location="", content_length=None):
        self.status_code = status
        self.url = "https://official.example/feed.xml"
        self.headers = {"Content-Type": "application/xml"}
        if location:
            self.headers["Location"] = location
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.encoding = "utf-8"
        self._content = content

    def close(self):
        return None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=65536):
        return iter((self._content,))


class FinancialRSSFeedContractTests(unittest.TestCase):
    def test_financial_pack_declares_five_rss_and_approved_hk_announcement_sources(self):
        sources = IndustryPackLoader().load("financial_markets")["default_sources"]
        rss_sources = [item for item in sources if item["source_type"] == "rss"]
        announcement_sources = [
            item for item in sources if item["source_type"] in {"list_page", "website"}
        ]
        self.assertEqual(len(rss_sources), 5)
        self.assertEqual(len(announcement_sources), 2)
        self.assertEqual({item["authority_level"] for item in sources}, {5})
        self.assertEqual({item["market"] for item in sources}, {"HK"})
        self.assertTrue(all(not item["api_key_required"] for item in rss_sources))
        self.assertEqual({item["access_cost"] for item in rss_sources}, {"free_public_rss"})
        self.assertEqual(
            {item["source_role"] for item in announcement_sources},
            {"exchange_official", "issuer_official"},
        )
        self.assertTrue(
            all(item["approval_status"] == "approved" for item in announcement_sources)
        )
        self.assertEqual(len({item["url"] for item in sources}), 7)

    def test_rss_and_atom_parse_absolute_links_clean_html_and_dates(self):
        rss = validate_rss_feed_response(_response())
        atom = validate_rss_feed_response(_response(ATOM_XML, "application/atom+xml"))
        self.assertEqual(rss["entry_count"], 1)
        self.assertEqual(rss["sample_url"], "https://official.example/news/market-policy")
        self.assertEqual(atom["entry_count"], 1)
        self.assertEqual(atom["latest_published_at"], "2026-07-31T01:00:00Z")
        self.assertEqual(parse_rss_feed(_response(), limit=1)[0]["summary"], "Policy summary")

    def test_official_announcement_sources_register_idempotently_with_audit_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            database = SQLiteDatabase(os.path.join(directory, "sources.sqlite3"))
            self.assertTrue(database.connect())
            self.assertTrue(database.create_tables())
            # This contract verifies the installation seed, not whatever
            # published draft another test may have placed in the process-wide
            # version store.
            registry = IntelSourceRegistry(
                database,
                pack_loader=IndustryPackLoader(use_published_store=False),
            )
            registry.ensure_pack_default_sources("financial_markets")
            registry.ensure_pack_default_sources("financial_markets")
            sources, _ = registry.list_sources(
                industry_pack_id="financial_markets", is_enabled=True, per_page=100
            )
            official = [
                item for item in sources
                if (item.get("metadata") or {}).get("source_role")
                in {"exchange_official", "issuer_official"}
            ]
            self.assertEqual(len(official), 2)
            self.assertEqual(
                {item["metadata"]["source_role"] for item in official},
                {"exchange_official", "issuer_official"},
            )
            self.assertTrue(
                all(item["metadata"]["approved_domains"] for item in official)
            )
            database.disconnect()

    def test_rejects_status_content_type_oversize_malformed_and_unsafe_xml(self):
        with self.assertRaises(RSSFeedContractError):
            parse_rss_feed(_response(status=403), limit=10)
        with self.assertRaises(RSSFeedContractError):
            parse_rss_feed(_response(content_type="text/html"), limit=10)
        with patch("rss_feed_contract.config.INTEL_SCAN_MAX_RESPONSE_BYTES", 10):
            with self.assertRaises(RSSFeedContractError):
                parse_rss_feed(_response(), limit=10)
        for content in (
            b"<rss><broken>",
            b'<!DOCTYPE rss [<!ENTITY x "unsafe">]><rss><channel/></rss>',
        ):
            with self.assertRaises(RSSFeedContractError):
                parse_rss_feed(_response(content), limit=10)

    def test_requires_an_entry_absolute_link_and_parseable_publish_time(self):
        empty = b"<rss><channel/></rss>"
        missing_date = RSS_XML.replace(
            b"<pubDate>Fri, 31 Jul 2026 09:00:00 +0800</pubDate>", b""
        )
        missing_link = RSS_XML.replace(b"<link>/news/market-policy</link>", b"")
        for content in (empty, missing_date, missing_link):
            with self.assertRaises(RSSFeedContractError):
                validate_rss_feed_response(_response(content))

    def test_http_client_blocks_oversize_403_and_private_redirect(self):
        public = lambda *_args: [(2, 1, 6, "", ("8.8.8.8", 443))]
        response = _HTTPResponse(content_length=999999)
        session = Mock()
        session.get.return_value = response
        with patch("intel_http.config.INTEL_SCAN_MAX_RESPONSE_BYTES", 100):
            with self.assertRaises(ExternalFetchError):
                SafeHTTPClient(session=session, resolver=public).get(response.url)

        session.get.return_value = _HTTPResponse(status=403)
        with self.assertRaises(RuntimeError):
            SafeHTTPClient(session=session, resolver=public).get(response.url)

        session.get.return_value = _HTTPResponse(
            status=302, location="http://127.0.0.1/private"
        )

        def redirect_resolver(host, *_args):
            address = "127.0.0.1" if host == "127.0.0.1" else "8.8.8.8"
            return [(2, 1, 6, "", (address, 443))]

        with self.assertRaises(UnsafeExternalURLError):
            SafeHTTPClient(session=session, resolver=redirect_resolver).get(response.url)


if __name__ == "__main__":
    unittest.main()

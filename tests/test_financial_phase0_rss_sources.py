#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

_BOOTSTRAP_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = os.path.join(_BOOTSTRAP_TEMP_DIR.name, "bootstrap.sqlite3")
os.environ["INTEL_LLM_ENABLED"] = "false"

from flask import Flask

import config
import intel_api
from intel_api import intel_bp
from intel_sources import IntelSourceRegistry, canonicalize_source_url
from sqlite_database import SQLiteDatabase
from tools.configure_financial_rss_sources import configure_sources


class FinancialRSSSourceRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "financial-sources.sqlite3")
        self.db = SQLiteDatabase(self.db_path)
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())
        self.registry = IntelSourceRegistry(self.db)
        self._insert_existing_disabled_sources()

    def tearDown(self):
        self.db.disconnect()
        self.temp_dir.cleanup()

    def _insert_existing_disabled_sources(self):
        urls = (
            "https://www.news.gov.hk/en/categories/finance/html/articlelist.rss.xml",
            "https://www.hkma.gov.hk/eng/other-information/rss/rss_press-release.xml",
        )
        for index, url in enumerate(urls, start=1):
            cursor = self.db.connection.execute(
                """
                INSERT INTO intel_sources (
                    canonical_source_url, source_url, source_name, source_type,
                    content_type, market, authority_level, polling_interval_minutes,
                    is_enabled, enabled_is_manual, metadata_json
                ) VALUES (?, ?, ?, 'rss', 'official', 'HK', 5, 1440, 0, 0, '{}')
                """,
                (canonicalize_source_url(url), url, f"existing-{index}"),
            )
            self.db.connection.execute(
                "INSERT INTO intel_source_industries (source_id, industry_pack_id, is_manual) VALUES (?, 'family_office', 0)",
                (int(cursor.lastrowid),),
            )
        self.db.connection.commit()

    def _admin_client(self):
        app = Flask(__name__)
        app.register_blueprint(intel_bp)
        return app.test_client()

    def test_register_enable_dedupe_and_manual_disable_protection(self):
        first = self.registry.ensure_pack_default_sources("financial_markets")
        self.assertEqual(first, {"sources_added": 5, "sources_attached": 7})
        sources, total = self.registry.list_sources(
            industry_pack_id="financial_markets", source_type="rss", page=1, per_page=100
        )
        self.assertEqual(total, 5)
        self.assertEqual(sum(int(item["is_enabled"]) for item in sources), 3)
        existing = [item for item in sources if "family_office" in item["industry_pack_ids"]]
        self.assertEqual(len(existing), 2)
        # v2 migrates ownership to the declaring financial pack while retaining
        # the historical family_office association for audit.
        self.assertEqual({item["metadata"]["origin_pack_id"] for item in existing}, {"financial_markets"})
        new_sources = [item for item in sources if "family_office" not in item["industry_pack_ids"]]
        self.assertEqual({item["metadata"]["origin_pack_id"] for item in new_sources}, {"financial_markets"})

        client = self._admin_client()
        with patch.object(intel_api, "intel_source_registry", self.registry), patch(
            "decorators.user_db.verify_session",
            return_value={"user_id": 1, "role": "admin"},
        ):
            headers = {"Authorization": "Bearer admin-token"}
            for source in sources:
                pack_ids = list(source["industry_pack_ids"])
                if "financial_markets" not in pack_ids:
                    pack_ids.append("financial_markets")
                response = client.patch(
                    f"/api/intel/sources/{source['id']}",
                    headers=headers,
                    json={"is_enabled": True, "industry_pack_ids": pack_ids},
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json()["source"]["enabled_is_manual"])

            protected_id = sources[0]["id"]
            disabled = client.patch(
                f"/api/intel/sources/{protected_id}",
                headers=headers,
                json={"is_enabled": False},
            )
            self.assertEqual(disabled.status_code, 200)

        repeated = self.registry.ensure_pack_default_sources("financial_markets")
        self.assertEqual(repeated, {"sources_added": 0, "sources_attached": 0})
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM intel_sources WHERE source_type='rss'"
            ).fetchone()[0],
            5,
        )
        self.registry.sync_legacy_sources(
            dry_run=False, default_industry_pack_id="financial_markets"
        )
        protected = self.registry.get_source(protected_id)
        self.assertFalse(protected["is_enabled"])
        self.assertTrue(protected["enabled_is_manual"])

        run_time = datetime.strptime(config.INTEL_LIGHT_SCAN_DAILY_TIME[:5], "%H:%M")
        due = self.registry.due_source_ids("financial_markets", run_time)
        self.assertNotIn(protected_id, due)
        self.assertEqual(len(due), 4)

    def test_rollout_tool_is_idempotent_and_respects_later_manual_disable(self):
        applied = configure_sources(
            self.db_path, pack_id="financial_markets", apply=True
        )
        self.assertEqual(applied["sources_added"], 5)
        self.assertEqual(len(applied["activated_source_ids"]), 5)
        protected_id = applied["activated_source_ids"][0]
        self.registry.update_source(protected_id, is_enabled=False)

        repeated = configure_sources(
            self.db_path, pack_id="financial_markets", apply=True
        )
        self.assertEqual(repeated["sources_added"], 0)
        self.assertIn(protected_id, repeated["skipped_manual_disabled_source_ids"])
        self.assertNotIn(protected_id, repeated["activated_source_ids"])
        self.assertFalse(self.registry.get_source(protected_id)["is_enabled"])


if __name__ == "__main__":
    unittest.main()

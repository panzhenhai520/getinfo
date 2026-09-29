#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import tempfile
import unittest
from unittest.mock import patch

_BOOTSTRAP_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = os.path.join(
    _BOOTSTRAP_TEMP_DIR.name,
    "bootstrap.sqlite3",
)
os.environ["INTEL_LLM_ENABLED"] = "false"

from flask import Flask

import intel_api
from intel_api import intel_bp
from intel_database import IntelRepository
from intel_sources import (
    IntelSourceRegistry,
    canonicalize_source_url,
    infer_content_attributes,
    infer_source_type,
)
from intel_worker import IntelWorker
from sqlite_database import SQLiteDatabase


class IntelStageTwoTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "stage2.sqlite3")
        self.db = SQLiteDatabase(self.db_path)
        self.assertTrue(self.db.connect())
        self.assertTrue(self.db.create_tables())
        self.repo = IntelRepository(self.db)
        self.registry = IntelSourceRegistry(self.db)
        self._insert_legacy_fixtures()

    def tearDown(self):
        self.db.disconnect()
        self.temp_dir.cleanup()

    def _insert_legacy_fixtures(self):
        cursor = self.db.connection.cursor()
        cursor.execute(
            """
            INSERT INTO managed_urls (
                url, name, description, domain, is_active, crawl_frequency
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "https://Example.com/news/?section=markets&utm_source=mail#top",
                "示例官方新闻",
                "市场新闻列表",
                "example.com",
                1,
                "daily",
            ),
        )
        self.managed_id = int(cursor.lastrowid)
        cursor.execute(
            """
            INSERT INTO managed_urls (
                url, name, description, domain, is_active, crawl_frequency
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "https://example.com/research",
                "示例研究报告",
                "研究与白皮书",
                "example.com",
                1,
                "weekly",
            ),
        )
        self.research_id = int(cursor.lastrowid)
        cursor.execute(
            """
            INSERT INTO scheduled_tasks (
                task_name, task_type, target_url, url_id, schedule_type,
                schedule_time, is_active
            ) VALUES (?, 'crawl', ?, ?, 'daily', '08:00:00', 1)
            """,
            (
                "新闻定时采集",
                "https://example.com/news/?section=markets&utm_medium=social",
                self.managed_id,
            ),
        )
        self.linked_task_id = int(cursor.lastrowid)
        cursor.execute(
            """
            INSERT INTO scheduled_tasks (
                task_name, task_type, target_url, url_id, schedule_type,
                schedule_time, is_active
            ) VALUES (?, 'crawl', ?, NULL, 'weekly', '09:00:00', 1)
            """,
            (
                "仅目标网址活动任务",
                "https://events.example.org/upcoming?campaign=keep&utm_campaign=drop",
            ),
        )
        self.target_only_task_id = int(cursor.lastrowid)
        self.db.connection.commit()
        cursor.close()

    def test_url_normalization_and_attribute_inference(self):
        self.assertEqual(
            canonicalize_source_url(
                "HTTPS://Example.COM:443/news/?section=markets&utm_source=x#latest"
            ),
            "https://example.com/news/?section=markets",
        )
        self.assertNotEqual(
            canonicalize_source_url("https://example.com/news/a"),
            canonicalize_source_url("https://example.com/news/b"),
        )
        self.assertEqual(infer_source_type("https://example.com/feed.xml"), "rss")
        self.assertEqual(infer_source_type("https://example.com/news"), "list_page")
        self.assertEqual(
            infer_content_attributes("https://www.gov.hk/news", "")[0],
            "official",
        )

    def test_active_pack_defaults_before_runtime_schema_exists(self):
        empty_db = SQLiteDatabase(os.path.join(self.temp_dir.name, "empty.sqlite3"))
        self.assertTrue(empty_db.connect())
        try:
            self.assertEqual(
                IntelRepository(empty_db).active_industry_pack_id(),
                "family_office",
            )
        finally:
            empty_db.disconnect()

    def test_dry_run_apply_pagination_idempotency_and_target_only_task(self):
        dry_run = self.registry.sync_legacy_sources(dry_run=True, page_size=1)
        self.assertEqual(dry_run["legacy_managed_urls"], 2)
        self.assertEqual(dry_run["legacy_scheduled_tasks"], 2)
        self.assertEqual(dry_run["errors"], 0)
        count = self.db.connection.execute("SELECT COUNT(*) FROM intel_sources").fetchone()[0]
        self.assertEqual(count, 0)

        applied = self.registry.sync_legacy_sources(dry_run=False, page_size=1)
        self.assertEqual(applied["sources_added"], 3)
        self.assertEqual(applied["origins_added"], 4)
        sources, total = self.registry.list_sources(
            industry_pack_id="family_office",
            page=1,
            per_page=100,
        )
        self.assertEqual(total, 3)
        event_source = next(
            source for source in sources if source["canonical_source_url"].startswith(
                "https://events.example.org/"
            )
        )
        self.assertEqual(event_source["origin_count"], 1)
        self.assertIn("campaign=keep", event_source["canonical_source_url"])
        self.assertNotIn("utm_campaign", event_source["canonical_source_url"])

        repeated = self.registry.sync_legacy_sources(dry_run=False, page_size=1)
        self.assertEqual(repeated["sources_added"], 0)
        self.assertEqual(repeated["origins_added"], 0)
        self.assertEqual(
            self.db.connection.execute("SELECT COUNT(*) FROM intel_sources").fetchone()[0],
            3,
        )

    def test_multiple_origins_manual_fields_and_safe_unlink(self):
        self.registry.sync_legacy_sources(dry_run=False, page_size=1)
        canonical = canonicalize_source_url(
            "https://example.com/news/?section=markets&utm_source=ignored"
        )
        row = self.db.connection.execute(
            "SELECT id FROM intel_sources WHERE canonical_source_url=?",
            (canonical,),
        ).fetchone()
        source_id = int(row["id"])
        origin_count = self.db.connection.execute(
            "SELECT COUNT(*) FROM intel_source_origins WHERE source_id=?",
            (source_id,),
        ).fetchone()[0]
        self.assertEqual(origin_count, 2)

        updated = self.registry.update_source(
            source_id,
            authority_level=1,
            is_enabled=False,
            industry_pack_ids=["ai_news", "healthcare_news"],
        )
        self.assertFalse(updated["is_enabled"])
        self.assertEqual(set(updated["industry_pack_ids"]), {"ai_news", "healthcare_news"})

        self.db.connection.execute(
            "UPDATE managed_urls SET name='同步后名称', is_active=1 WHERE id=?",
            (self.managed_id,),
        )
        self.registry.sync_legacy_sources(dry_run=False, page_size=1)
        protected = self.registry.get_source(source_id)
        self.assertEqual(protected["authority_level"], 1)
        self.assertFalse(protected["is_enabled"])
        self.assertEqual(
            set(protected["industry_pack_ids"]),
            {"ai_news", "healthcare_news"},
        )

        self.db.connection.execute(
            "DELETE FROM scheduled_tasks WHERE id=?",
            (self.linked_task_id,),
        )
        self.registry.sync_legacy_sources(dry_run=False, page_size=1)
        remaining = self.registry.get_source(source_id)
        self.assertIsNotNone(remaining)
        self.assertEqual(remaining["origin_count"], 1)

    def test_legacy_sync_never_disables_manifest_owned_source_without_origin(self):
        source_id = int(
            self.db.connection.execute(
                """
                INSERT INTO intel_sources(
                    canonical_source_url,source_url,source_name,source_type,
                    authority_level,is_enabled,metadata_json
                ) VALUES(
                    'https://pack.example.test/news',
                    'https://pack.example.test/news',
                    '行业包自有来源','list_page',3,1,'{}'
                )
                """
            ).lastrowid
        )
        self.db.connection.execute(
            """
            INSERT INTO intel_source_industries(
                source_id,industry_pack_id,ownership_type,is_active
            ) VALUES(?,'family_office','pack_owned',1)
            """,
            (source_id,),
        )
        self.db.connection.commit()

        self.registry.sync_legacy_sources(dry_run=False, page_size=1)

        enabled = self.db.connection.execute(
            "SELECT is_enabled FROM intel_sources WHERE id=?", (source_id,)
        ).fetchone()[0]
        self.assertEqual(int(enabled), 1)

    def test_worker_daily_dedupe_and_manual_job(self):
        worker = IntelWorker(
            repository=self.repo,
            source_registry=self.registry,
            worker_id="stage2-worker",
        )
        manual_job_id, _ = self.repo.enqueue_job(
            "source_sync",
            "stage2-manual-sync",
            {"manual": True, "page_size": 1},
        )
        with patch("intel_worker.config.INTEL_SOURCE_SYNC_ENABLED", False):
            stats = worker.run_once(job_types=["source_sync"], limit=10)
        self.assertEqual(stats["completed"], 1)
        self.assertEqual(self.repo.get_job(manual_job_id)["status"], "completed")

        with patch("intel_worker.config.INTEL_SOURCE_SYNC_ENABLED", True):
            first = worker.run_once(job_types=["source_sync"], limit=10)
            second = worker.run_once(job_types=["source_sync"], limit=10)
        self.assertEqual(first["completed"], 1)
        self.assertEqual(second["claimed"], 0)
        daily_count = self.db.connection.execute(
            "SELECT COUNT(*) FROM intel_jobs WHERE dedupe_key LIKE 'source-sync:daily:%'"
        ).fetchone()[0]
        self.assertEqual(daily_count, 1)

    def test_sources_api_auth_filters_update_and_sync_job(self):
        self.registry.sync_legacy_sources(dry_run=False, page_size=1)
        source_id = int(
            self.db.connection.execute(
                "SELECT id FROM intel_sources ORDER BY id LIMIT 1"
            ).fetchone()["id"]
        )
        app = Flask(__name__)
        app.register_blueprint(intel_bp)
        with patch.object(intel_api, "intel_source_registry", self.registry), patch.object(
            intel_api, "intel_repository", self.repo
        ):
            client = app.test_client()
            self.assertEqual(client.get("/api/intel/sources").status_code, 401)
            with patch(
                "decorators.user_db.verify_session",
                return_value={"user_id": 7, "role": "admin"},
            ):
                headers = {"Authorization": "Bearer admin-token"}
                listed = client.get(
                    "/api/intel/sources?industry_pack_id=family_office"
                    "&source_type=list_page&status=enabled",
                    headers=headers,
                )
                self.assertEqual(listed.status_code, 200)
                self.assertGreaterEqual(listed.get_json()["total"], 1)
                changed = client.patch(
                    f"/api/intel/sources/{source_id}",
                    headers=headers,
                    json={
                        "authority_level": 5,
                        "is_enabled": False,
                        "industry_pack_ids": ["ai_news", "short_video_news"],
                    },
                )
                self.assertEqual(changed.status_code, 200)
                self.assertFalse(changed.get_json()["source"]["is_enabled"])
                queued = client.post(
                    "/api/intel/sources/sync",
                    headers={**headers, "Idempotency-Key": "stage2-api"},
                    json={"batch_size": 1},
                )
                duplicate = client.post(
                    "/api/intel/sources/sync",
                    headers={**headers, "Idempotency-Key": "stage2-api"},
                    json={"batch_size": 1},
                )
                self.assertEqual(queued.status_code, 202)
                self.assertEqual(
                    queued.get_json()["job_id"],
                    duplicate.get_json()["job_id"],
                )

            with patch(
                "decorators.user_db.verify_session",
                return_value={"user_id": 8, "role": "user"},
            ):
                forbidden = client.patch(
                    f"/api/intel/sources/{source_id}",
                    headers={"Authorization": "Bearer user-token"},
                    json={"is_enabled": True},
                )
                self.assertEqual(forbidden.status_code, 403)


if __name__ == "__main__":
    unittest.main()

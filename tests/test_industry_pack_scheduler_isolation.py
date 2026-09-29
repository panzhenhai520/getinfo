import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from industry_pack_runtime import ActiveIndustryCompositionService
from industry_packs import IndustryPackLoader
from intel_database import IntelRepository
from intel_candidates import IntelCandidateRepository
from intel_worker import IntelWorker
from project_keyword_gate import configured_project_keyword_snapshot
from scheduler import TaskScheduler
from sqlite_database import SQLiteDatabase


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class IndustryPackSchedulerIsolationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(
            str(Path(self.temp_dir.name) / "scheduler-isolation.sqlite3")
        )
        self.assertTrue(self.database.connect())
        self.assertTrue(self.database.create_tables())
        self.repository = IntelRepository(self.database)
        self.loader = IndustryPackLoader(
            str(PROJECT_ROOT / "config" / "industry_packs"),
            use_published_store=False,
        )
        self.runtime = ActiveIndustryCompositionService(
            self.database, pack_loader=self.loader
        )
        self._set_active("education_news", "activation-education", 17)

    def tearDown(self):
        self.database.disconnect()
        self.temp_dir.cleanup()

    def _set_active(self, pack_id, activation_id, version_id):
        for key, value in (
            ("active_industry_pack_id", pack_id),
            ("active_industry_pack_version_id", version_id),
            ("active_industry_activation_id", activation_id),
        ):
            self.database.connection.execute(
                """
                INSERT INTO intel_runtime_settings(setting_key, setting_value)
                VALUES(?, ?) ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value=excluded.setting_value
                """,
                (key, str(value)),
            )
        self.database.connection.commit()

    def test_active_snapshot_replaces_legacy_task_and_url_keyword_union(self):
        self.database.connection.execute(
            """
            INSERT INTO scheduled_tasks(
                task_name, task_type, target_url, schedule_type, keywords,
                industry_pack_id, is_active
            ) VALUES('legacy','crawl','https://legacy.example','daily',
                     '旧行业泄漏词','family_office',1)
            """
        )
        self.database.connection.execute(
            """
            INSERT INTO managed_urls(url, name, is_active)
            VALUES('https://manual.example','旧网址泄漏词',1)
            """
        )
        self.database.connection.commit()

        snapshot = configured_project_keyword_snapshot(
            self.database.connection, pack_loader=self.loader
        )
        expected_pack = self.loader.load("education_news")
        expected = list(
            dict.fromkeys(
                expected_pack["core_keywords"] + expected_pack["expanded_keywords"]
            )
        )
        self.assertEqual(snapshot["industry_pack_id"], "education_news")
        self.assertEqual(snapshot["industry_pack_version_id"], 17)
        self.assertEqual(snapshot["activation_id"], "activation-education")
        self.assertEqual(snapshot["keywords"], expected)
        self.assertNotIn("旧行业泄漏词", snapshot["keywords"])
        self.assertNotIn("旧网址泄漏词", snapshot["keywords"])
        runtime = self.runtime.snapshot()
        self.assertEqual(
            runtime["effective_pack_ids"],
            ["education_news", "financial_markets"],
        )

    def test_worker_schedules_only_current_composition_and_one_shared_source_scan(self):
        source_registry = Mock()
        source_registry.pack_loader = self.loader
        source_registry.migrate_legacy_schedule_preferences.return_value = 0
        source_registry.due_source_ids.return_value = [51]
        source_registry.get_source.return_value = {
            "id": 51,
            "industry_pack_ids": ["financial_markets"],
            "consecutive_scan_failures": 0,
            "last_scan_status": "",
        }
        market_scheduler = Mock()
        market_scheduler.enqueue_due_jobs.return_value = {}
        worker = IntelWorker(
            repository=self.repository,
            source_registry=source_registry,
            financial_market_scheduler=market_scheduler,
            active_composition_service=self.runtime,
            worker_id="isolation-worker",
        )
        now = datetime(2026, 8, 6, 2, 0, tzinfo=timezone.utc)
        with patch("intel_worker.utc_now", return_value=now), patch(
            "intel_worker.config.INTEL_SOURCE_SYNC_ENABLED", True
        ), patch(
            "intel_worker.config.INTEL_LIGHT_SCANNER_ENABLED", True
        ), patch(
            "intel_worker.config.INTEL_CANDIDATE_DISPATCH_ENABLED", True
        ), patch(
            "intel_worker.config.INTEL_TOPIC_CLUSTER_ENABLED", True
        ):
            worker.enqueue_due_periodic_jobs()
            worker.enqueue_due_periodic_jobs()

        rows = self.database.connection.execute(
            "SELECT job_type, dedupe_key, payload_json FROM intel_jobs ORDER BY id"
        ).fetchall()
        payloads = [(row["job_type"], json.loads(row["payload_json"])) for row in rows]
        report_packs = {
            payload["industry_pack_id"]
            for job_type, payload in payloads
            if job_type == "report_check"
        }
        self.assertEqual(report_packs, {"education_news", "financial_markets"})
        topic_payloads = [payload for kind, payload in payloads if kind == "topic_cluster"]
        self.assertEqual(len(topic_payloads), 1)
        self.assertEqual(topic_payloads[0]["industry_pack_id"], "education_news")
        source_scans = [
            payload
            for kind, payload in payloads
            if kind == "light_scan" and payload.get("source_ids") == [51]
        ]
        self.assertEqual(len(source_scans), 1)
        self.assertEqual(source_scans[0]["declaring_pack_id"], "financial_markets")
        self.assertTrue(payloads)
        self.assertTrue(
            all(payload["activation_id"] == "activation-education" for _, payload in payloads)
        )

    def test_old_unclaimed_jobs_are_cancelled_while_running_job_keeps_old_identity(self):
        old_id, _ = self.repository.enqueue_job(
            "light_scan", "old-queued", {"industry_pack_id": "education_news"}
        )
        running_id, _ = self.repository.enqueue_job(
            "report_check", "old-running", {"industry_pack_id": "education_news"}
        )
        claimed = self.repository.claim_jobs(
            "worker-before-switch", job_types=["report_check"], limit=1
        )
        self.assertEqual([item["id"] for item in claimed], [running_id])

        self._set_active("healthcare_news", "activation-healthcare", 18)
        changed = self.repository.cancel_stale_activation_jobs(
            "activation-healthcare"
        )
        self.assertEqual(changed, 1)
        self.assertEqual(self.repository.get_job(old_id)["status"], "cancelled")
        running = self.repository.get_job(running_id)
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["payload"]["activation_id"], "activation-education")

        new_id, _ = self.repository.enqueue_job(
            "topic_cluster", "new-current", {"industry_pack_id": "healthcare_news"}
        )
        new_payload = self.repository.get_job(new_id)["payload"]
        self.assertEqual(new_payload["activation_id"], "activation-healthcare")
        self.assertEqual(new_payload["primary_industry_pack_id"], "healthcare_news")
        self.assertEqual(new_payload["industry_pack_version_id"], 18)

    def test_legacy_scheduler_definition_is_pack_scoped_and_uses_pack_keywords(self):
        active = self.runtime.snapshot()
        family_task = {
            "id": 1,
            "industry_pack_id": "family_office",
            "keywords": "旧行业泄漏词",
            "config": {},
        }
        self.assertIsNone(
            TaskScheduler._task_for_active_composition(family_task, active)
        )
        current = TaskScheduler._task_for_active_composition(
            {
                "id": 2,
                "industry_pack_id": "education_news",
                "keywords": "管理员任意输入词",
                "config": {},
            },
            active,
        )
        self.assertEqual(current["activation_id"], "activation-education")
        self.assertEqual(
            current["keywords"], ",".join(active["project_keywords"])
        )
        self.assertNotIn("管理员任意输入词", current["keywords"])

    def test_old_running_classification_cannot_enter_new_activation_view(self):
        self._set_active("healthcare_news", "activation-healthcare", 18)
        article_id = int(
            self.database.connection.execute(
                """
                INSERT INTO articles(
                    url, title, content, domain, publish_date, status,
                    content_hash, content_length
                ) VALUES(?, ?, ?, ?, ?, 'active', 'health-hash', 16)
                """,
                (
                    "https://health.example.test/policy",
                    "医疗健康政策更新",
                    "医疗健康行业政策发生重要变化。",
                    "health.example.test",
                    "2026-08-06",
                ),
            ).lastrowid
        )
        self.database.connection.commit()
        base = {
            "article_id": article_id,
            "industry_pack_id": "healthcare_news",
            "industry_pack_version": self.loader.load("healthcare_news")["pack_version"],
            "classifier_version": "isolation-test",
            "article_content_hash": "health-hash",
            "rule_category": "event",
            "rule_confidence": 1,
            "rule_reason": "fixture",
            "score_details": {"hits": {"anchor": ["医疗健康"]}},
            "matched_keywords": ["医疗健康"],
            "final_category": "event",
            "final_confidence": 1,
            "final_reason": "fixture",
        }
        self.repository.upsert_classification(
            {**base, "activation_id": "activation-education"}
        )
        articles, total, _ = self.repository.list_classified_articles(
            industry_pack_id="healthcare_news", time_range="30d"
        )
        self.assertEqual((articles, total), ([], 0))
        pending = self.repository.list_unclassified_articles("healthcare_news")
        self.assertIn(article_id, {item["id"] for item in pending})

        self.repository.upsert_classification(
            {**base, "activation_id": "activation-healthcare"}
        )
        articles, total, _ = self.repository.list_classified_articles(
            industry_pack_id="healthcare_news", time_range="30d"
        )
        self.assertEqual(total, 1)
        self.assertEqual(articles[0]["activation_id"], "activation-healthcare")
        self.repository.upsert_classification(
            {**base, "activation_id": "activation-education", "final_reason": "late-old"}
        )
        stored = self.database.connection.execute(
            """
            SELECT activation_id, final_reason
            FROM article_intel_classifications
            WHERE article_id=? AND industry_pack_id='healthcare_news'
            """,
            (article_id,),
        ).fetchone()
        self.assertEqual(tuple(stored), ("activation-healthcare", "fixture"))

    def test_late_candidate_from_old_scan_is_not_claimed_by_new_activation(self):
        candidates = IntelCandidateRepository(self.database)
        discovered = candidates.discover(
            {
                "url": "https://late.example.test/health",
                "title": "医疗健康行业政策更新",
                "summary": "医疗健康产业出现新事件。",
            },
            industry_pack_id="healthcare_news",
            activation_id="activation-education",
            observation_type="website",
        )
        self.assertTrue(discovered["should_queue"])
        self.assertEqual(
            candidates.claim_candidates(
                "new-worker", active_activation_id="activation-healthcare"
            ),
            [],
        )
        claimed = candidates.claim_candidates(
            "old-worker", active_activation_id="activation-education"
        )
        self.assertEqual([item["id"] for item in claimed], [discovered["candidate_id"]])
        self.assertEqual(claimed[0]["activation_id"], "activation-education")

    def test_failed_candidate_is_reactivated_only_after_a_new_activation_hit(self):
        candidates = IntelCandidateRepository(self.database)
        item = {
            "url": "https://retry.example.test/health-policy",
            "title": "医疗健康行业政策更新",
            "summary": "医疗健康产业出现新事件。",
        }
        first = candidates.discover(
            item,
            industry_pack_id="healthcare_news",
            activation_id="activation-education",
            observation_type="website",
        )
        claimed = candidates.claim_candidates(
            "old-worker", active_activation_id="activation-education"
        )
        self.assertEqual([row["id"] for row in claimed], [first["candidate_id"]])
        candidates.set_candidate_decision(
            first["candidate_id"],
            quality_status="passed",
            admission_status="review",
            admission_reason="old activation rejection",
            admission_confidence=0.1,
        )
        self.assertEqual(
            candidates.fail_candidate(
                first["candidate_id"], "old activation rejection", permanent=True
            ),
            "failed",
        )

        same_activation = candidates.discover(
            item,
            industry_pack_id="healthcare_news",
            activation_id="activation-education",
            observation_type="website",
        )
        self.assertFalse(same_activation["reactivated"])
        status = self.database.connection.execute(
            "SELECT status FROM intel_candidates WHERE id=?",
            (first["candidate_id"],),
        ).fetchone()["status"]
        self.assertEqual(status, "failed")

        new_activation = candidates.discover(
            item,
            industry_pack_id="healthcare_news",
            activation_id="activation-healthcare",
            observation_type="website",
        )
        self.assertTrue(new_activation["reactivated"])
        reset = self.database.connection.execute(
            """
            SELECT status, attempt_count, quality_status, admission_status,
                   admission_reason, last_error
            FROM intel_candidates WHERE id=?
            """,
            (first["candidate_id"],),
        ).fetchone()
        self.assertEqual(
            tuple(reset),
            ("queued", 0, "pending", "pending", "", ""),
        )
        claimed = candidates.claim_candidates(
            "new-worker", active_activation_id="activation-healthcare"
        )
        self.assertEqual([row["id"] for row in claimed], [first["candidate_id"]])
        self.assertEqual(claimed[0]["activation_id"], "activation-healthcare")


if __name__ == "__main__":
    unittest.main()
